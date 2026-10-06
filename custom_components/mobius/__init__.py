"""The Mobius integration."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from homeassistant.components import bluetooth, persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, CoreState, EVENT_HOMEASSISTANT_STARTED
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from mobius import parse_advertisement

from .const import (
    DOMAIN, MAX_CONCURRENT_CONNECTIONS, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX,
    TANK_REVALIDATION_INTERVAL, SOFT_REFRESH_RETRY_ATTEMPTS, CLOCK_DRIFT_THRESHOLD, TIME_SYNC_COOLDOWN,
    SOFT_REFRESH_RETRY_DELAY, MESH_PEER_REFRESH_INTERVAL,
)
from .coordinator import (
    MobiusDeviceCoordinator, _find_in_bluetooth_cache, advertised_as_bluetooth_only, async_tank_broadcast,
    discover_mesh_address, discover_tank_for_serial,
)
from .gateway_registry import GatewayRegistry, PanGroup

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR, Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SWITCH, Platform.SELECT, Platform.NUMBER,
]

# Config entries only; YAML configuration is rejected with a clear error.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def tank_device_identifier(mlprefix_hex: Optional[str], pan_id: int) -> tuple[str, str]:
    """
    Device-registry identifier of an entry's tank device: a device without
    entities that every Mobius device points to with via_device. Every
    entry has one, including a single ad-hoc device (the app also puts
    every device in a tank). Based on the mesh prefix when known, else on
    the pan_id.
    """
    if mlprefix_hex is not None:
        return (DOMAIN, f"tank_{mlprefix_hex}")
    return (DOMAIN, f"tank_panid_{pan_id:04x}")


def resolve_tank_device_id(hass: HomeAssistant, entry_id: str, tank_identifier: tuple[str, str]) -> Optional[str]:
    """Registry id of the tank device, for DeviceInfo.via_device_id. The
    tank device is registered before the platforms are set up."""
    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_device_by_identifier(tank_identifier, entry_id)
    return device_entry.id if device_entry is not None else None


@dataclass
class MobiusRuntimeData:
    """Runtime state of one config entry (one tank, one or more devices)."""
    coordinators: dict[str, MobiusDeviceCoordinator] = field(default_factory=dict)
    # Whether _async_check_tank_time() may set the clock (TimeSyncSwitch).
    time_sync_enabled: bool = True
    # Monotonic time of the last automatic clock sync (TIME_SYNC_COOLDOWN),
    # and whether one is running.
    last_time_sync: Optional[float] = None
    time_sync_running: bool = False
    # Monotonic time of the tank check's last mesh peer list read
    # (MESH_PEER_REFRESH_INTERVAL).
    last_mesh_refresh: Optional[float] = None
    # Set by sensor.py's async_setup_entry(); used by
    # _async_ensure_sensors_exist().
    sensor_add_entities: Optional[AddEntitiesCallback] = None
    sensor_device_infos: dict[str, DeviceInfo] = field(default_factory=dict)
    created_sensor_unique_ids: set[str] = field(default_factory=set)


def _shared_state(hass: HomeAssistant) -> tuple[asyncio.Semaphore, GatewayRegistry]:
    """The integration-wide connection semaphore and gateway registry,
    created on first use."""
    data = hass.data.setdefault(DOMAIN, {})
    semaphore = data.setdefault("connection_semaphore", asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS))
    registry = data.setdefault("gateway_registry", GatewayRegistry(hass, semaphore))
    return semaphore, registry


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Sets up the shared state, websocket commands, services and the
    Lovelace cards."""
    _shared_state(hass)

    # Imported here: these modules import from this one.
    from .websocket_api import async_register_websocket_commands
    async_register_websocket_commands(hass)

    from .services import async_register_services
    async_register_services(hass)

    # Card registration is optional and must not break setup (see
    # frontend/__init__.py). It needs http/frontend, so it runs once Home
    # Assistant has started.
    async def _setup_frontend(_event=None) -> None:
        from .frontend import JSModuleRegistration
        await JSModuleRegistration(hass).async_register()

    if hass.state == CoreState.running:
        await _setup_frontend()
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _setup_frontend)

    return True


def _current_rssi(hass: HomeAssistant, serial: str) -> int | None:
    """RSSI of the advertisement currently carrying `serial`, if any (used
    for gateway election)."""
    info = _find_in_bluetooth_cache(hass, serial)
    return info.rssi if info is not None else None


def _single_device_notification_id(entry: ConfigEntry) -> str:
    return f"{DOMAIN}_single_device_tank_{entry.entry_id}"


def _notify_single_device_tank(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    Posts a notification while a tank has exactly one device.

    The integration keeps a Bluetooth connection to one device of each tank
    (the gateway) and reaches the others through it. A connected device
    can't be reached by the Mobius app, so with a single device the app
    can't control it at all until this entry is disabled. With more
    devices the app can still use the others.

    Posted on every setup (so it comes back after a restart or a reload
    even if it was dismissed), and dismissed when the entry is unloaded or
    the tank gains a second device.
    """
    if len(entry.data.get(CONF_DEVICES, [])) != 1:
        _dismiss_single_device_notification(hass, entry)
        return
    persistent_notification.async_create(
        hass,
        title=f"Mobius: only one device in {entry.title}",
        message=(
            f"Mobius keeps a Bluetooth connection to one device of each tank, so the tank's other "
            f"devices can be reached through it. {entry.title} has only one device, so the Mobius "
            f"app can't connect to it while this integration is running.\n\n"
            f"To use the app with this device, disable this Mobius entry "
            f"(Settings > Devices & services > Mobius > the entry's menu > Disable) and enable it "
            f"again afterwards. Adding another device of the same tank also solves it: the app can "
            f"then use whichever device Mobius isn't connected to."
        ),
        notification_id=_single_device_notification_id(entry),
    )


def _dismiss_single_device_notification(hass: HomeAssistant, entry: ConfigEntry) -> None:
    persistent_notification.async_dismiss(hass, _single_device_notification_id(entry))


def _register_tank_device(hass: HomeAssistant, entry: ConfigEntry, mlprefix_hex: Optional[str], pan_id: int) -> None:
    """Registers (or updates) the entry's tank device (see
    tank_device_identifier())."""
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={tank_device_identifier(mlprefix_hex, pan_id)},
        name=entry.title,
        manufacturer="EcoTech Marine",
        model="Tank",
    )


def _entry_group(hass: HomeAssistant, entry: ConfigEntry) -> Optional[tuple[GatewayRegistry, int, PanGroup]]:
    """(registry, pan_id, group) of an entry, or None if any is missing."""
    registry: GatewayRegistry | None = hass.data.get(DOMAIN, {}).get("gateway_registry")
    pan_id = entry.data.get(CONF_PAN_ID)
    if registry is None or pan_id is None:
        return None
    group = registry.group(pan_id)
    return None if group is None else (registry, pan_id, group)


async def _async_ensure_sensors_exist(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    Creates type-specific sensors that are missing.

    sensor.py decides which pump or light sensors to create from each
    device's data at setup. A relayed device's first read is non-blocking
    and can fail, in which case its type-specific sensors are not created
    at setup. This compares the entities each device's current data would
    produce with the ones already created (by unique_id) and adds the
    missing ones. Runs on every tank revalidation.

    Switches and selects (switch.py/select.py) are not covered.
    """
    runtime: MobiusRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is None or runtime.sensor_add_entities is None:
        return

    # Imported here: sensor.py imports from this module.
    from .sensor import _build_type_specific_entities

    new_entities = []
    for serial, coordinator in runtime.coordinators.items():
        device_info = runtime.sensor_device_infos.get(serial)
        data = coordinator.data or {}
        support = data.get("support", "")
        if device_info is None or not support:
            continue
        candidates = _build_type_specific_entities(coordinator, serial, device_info, support, data)
        missing = [e for e in candidates if e.unique_id not in runtime.created_sensor_unique_ids]
        if missing:
            new_entities += missing
            runtime.created_sensor_unique_ids.update(e.unique_id for e in missing)

    if new_entities:
        _LOGGER.debug(
            "%r: creating %d sensor(s) that were missed at setup (that "
            "device's own data wasn't ready yet at that exact moment): %s",
            entry.title, len(new_entities), [e.unique_id for e in new_entities],
        )
        runtime.sensor_add_entities(new_entities)


async def _async_revalidate_tank(hass: HomeAssistant, entry: ConfigEntry, now=None) -> None:
    """
    Periodic per-entry check (TANK_REVALIDATION_INTERVAL):

    1. Creates sensors missed at setup (_async_ensure_sensors_exist()).
    2. Without a gateway, re-triggers the gateway election through
       registry.join(). Bluetooth-only devices take no part in this or the
       steps below.
    3. If the gateway isn't connected and isn't in Home Assistant's
       Bluetooth cache, requests an active scan before using it.
    4. Every MESH_PEER_REFRESH_INTERVAL: reads the mesh peers through the
       gateway and updates the mesh address and last-seen time of every
       known member.
    5. Moves a device that is now on this tank's mesh but belongs to
       another entry into this entry (both entries are reloaded).

    Devices missing from the mesh are never removed (they become
    unavailable after MARK_UNAVAILABLE_AFTER), and new devices are left to
    Bluetooth discovery. A failed check is retried on the next run.
    """
    await _async_ensure_sensors_exist(hass, entry)

    found = _entry_group(hass, entry)
    if found is None:
        return
    registry, pan_id, group = found

    # Bluetooth-only devices are never on the mesh: they are neither
    # expected in its peer list nor candidates for the election below.
    runtime: MobiusRuntimeData | None = getattr(entry, "runtime_data", None)
    bluetooth_only = {
        serial for serial, c in (runtime.coordinators.items() if runtime is not None else []) if c.bluetooth_only
    }
    known_devices = [d for d in entry.data.get(CONF_DEVICES, []) if d[CONF_SERIAL] not in bluetooth_only]
    if group.gateway_serial is None:
        if not known_devices:
            return
        # join() of any member re-runs the election over all members.
        recovery_serial = known_devices[0][CONF_SERIAL]
        recovery_member = group.members.get(recovery_serial)
        await registry.join(pan_id, recovery_serial, rssi=recovery_member.rssi if recovery_member else None)
        _LOGGER.debug(
            "Tank %r had no gateway at all -- re-triggered election (result "
            "picked up by that device's own next regular poll cycle)", entry.title,
        )
        return

    # The mesh peer list is read less often than the rest of the check runs
    # (a failed read is retried at the next check).
    if runtime is not None and runtime.last_mesh_refresh is not None and (
        time.monotonic() - runtime.last_mesh_refresh < MESH_PEER_REFRESH_INTERVAL.total_seconds()
    ):
        return

    # A connected device doesn't advertise, so the cache is only checked
    # while the gateway is disconnected.
    if not group.gateway_connection.is_connected and _find_in_bluetooth_cache(hass, group.gateway_serial) is None:
        _LOGGER.debug(
            "Tank %r's own gateway %r isn't currently connected, and wasn't found "
            "in Home Assistant's Bluetooth cache either -- requesting an active scan",
            entry.title, group.gateway_serial,
        )
        await bluetooth.async_request_active_scan(hass)

    try:
        mdevice = await group.gateway_connection.ensure_connected()
        # raise_errors: a failed read must not look like an empty mesh.
        peers = await mdevice.discover_mesh_peers_auto(raise_errors=True)
    except Exception as err:
        _LOGGER.debug(
            "Tank revalidation for %r: reading the mesh peer list from gateway %r failed "
            "(retried at the next check, in %s): %s",
            entry.title, group.gateway_serial, TANK_REVALIDATION_INTERVAL, err or type(err).__name__,
        )
        return
    if runtime is not None:
        runtime.last_mesh_refresh = time.monotonic()

    known_serials = {d[CONF_SERIAL] for d in known_devices}
    now_utc = dt_util.utcnow()
    for peer in peers:
        if peer.serial not in known_serials:
            continue
        if peer.address is not None:
            registry.update_mesh_address(pan_id, peer.serial, peer.address)
        if peer.age is not None:
            registry.update_mesh_last_seen(
                pan_id, peer.serial, now_utc - timedelta(milliseconds=peer.age),
            )

    # Entities showing mesh data (the mesh address sensor's last_seen)
    # update now rather than at their device's next poll.
    if runtime is not None:
        for coordinator in runtime.coordinators.values():
            coordinator.async_update_listeners()

    reported_serials = {p.serial for p in peers}
    _LOGGER.debug(
        "Tank %r revalidated: %d/%d known device(s) reported by the mesh this cycle%s",
        entry.title, len(known_serials & reported_serials), len(known_serials),
        "" if known_serials <= reported_serials
        else f" (missing: {sorted(known_serials - reported_serials)})",
    )

    # Imported here to avoid loading config_flow.py with the integration.
    from .config_flow import (
        _find_entry_containing_serial, _merge_device_into_entry, _remove_device_from_entry,
    )

    for peer in peers:
        if peer.serial in known_serials:
            continue
        other_entry = _find_entry_containing_serial(hass, peer.serial)
        if other_entry is None or other_entry.entry_id == entry.entry_id:
            continue  # a new device (left to discovery)
        _LOGGER.info(
            "Device %s found on %r's mesh but was tracked under %r -- migrating it",
            peer.serial, entry.title, other_entry.title,
        )
        await _remove_device_from_entry(hass, other_entry, peer.serial)
        await _merge_device_into_entry(hass, entry, peer.serial)


def _clock_problems(hass: HomeAssistant, runtime: "MobiusRuntimeData"):
    """(worst drift as (serial, seconds) or None, time zone mismatch as
    (serial, device zone) or None) across the tank's devices."""
    worst = None
    mismatch = None
    for serial, coordinator in runtime.coordinators.items():
        data = coordinator.data or {}
        drift = data.get("clock_drift")
        if drift is not None and abs(drift) > CLOCK_DRIFT_THRESHOLD:
            if worst is None or abs(drift) > abs(worst[1]):
                worst = (serial, drift)
        # None: the device doesn't report a time zone; "" means it isn't set.
        olson = data.get("olson_tz")
        if olson is not None and olson != hass.config.time_zone and mismatch is None:
            mismatch = (serial, olson)
    return worst, mismatch


async def _async_check_tank_time(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    Runs after every poll of any device of the tank. When a device's clock
    is more than CLOCK_DRIFT_THRESHOLD seconds off Home Assistant's, or its
    time zone differs from Home Assistant's, sets the tank's time zone and
    time with group writes through the mesh and to each Bluetooth-only
    device (async_tank_broadcast()), like the app's "set time to now". At most once per TIME_SYNC_COOLDOWN;
    skipped while the entry's TimeSyncSwitch is off.
    """
    runtime: MobiusRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is None or not runtime.time_sync_enabled or runtime.time_sync_running:
        return
    now = time.monotonic()
    if runtime.last_time_sync is not None and now - runtime.last_time_sync < TIME_SYNC_COOLDOWN.total_seconds():
        return

    worst, mismatch = _clock_problems(hass, runtime)
    if worst is None and mismatch is None:
        return
    if worst is not None:
        _LOGGER.info(
            "Tank %r: clock drift of %+d s detected on %s (threshold %d s) -- syncing the tank's time",
            entry.title, worst[1], worst[0], CLOCK_DRIFT_THRESHOLD,
        )
    if mismatch is not None:
        _LOGGER.info(
            "Tank %r: %s uses time zone %r instead of %r -- setting the tank's time zone",
            entry.title, mismatch[0], mismatch[1] or "(not set)", hass.config.time_zone,
        )

    async def sync(device) -> None:
        if mismatch is not None:
            try:
                await device.set_time_zone(hass.config.time_zone)
            except Exception as err:
                _LOGGER.warning(
                    "Tank %r: setting the time zone %r failed (%s)", entry.title, hass.config.time_zone, err,
                )
        await device.set_time_to_now()

    runtime.time_sync_running = True
    runtime.last_time_sync = now
    try:
        sent, errors = await async_tank_broadcast(runtime.coordinators.values(), sync)
    finally:
        runtime.time_sync_running = False
    if errors:
        _LOGGER.warning(
            "Tank %r: clock sync failed on %s -- will retry after %s",
            entry.title, "; ".join(errors), TIME_SYNC_COOLDOWN,
        )
    if sent:
        _LOGGER.info("Tank %r: clock synced via %s", entry.title, ", ".join(sent))


async def _async_soft_first_refresh(coordinator: MobiusDeviceCoordinator, serial: str) -> None:
    """First refresh of a non-gateway device: never raises, retried
    SOFT_REFRESH_RETRY_ATTEMPTS times. A device that still fails starts
    unavailable and recovers on the normal poll cycle."""
    await coordinator.async_refresh()
    for attempt in range(1, SOFT_REFRESH_RETRY_ATTEMPTS + 1):
        if coordinator.last_update_success:
            break
        _LOGGER.debug(
            "%s's own first soft refresh at setup failed -- retrying "
            "(attempt %d/%d, %ss apart)", serial, attempt,
            SOFT_REFRESH_RETRY_ATTEMPTS, SOFT_REFRESH_RETRY_DELAY,
        )
        await asyncio.sleep(SOFT_REFRESH_RETRY_DELAY)
        await coordinator.async_refresh()
        if coordinator.last_update_success:
            _LOGGER.debug("%s came up on retry %d/%d", serial, attempt, SOFT_REFRESH_RETRY_ATTEMPTS)
    if not coordinator.last_update_success:
        _LOGGER.debug(
            "%s did not come up immediately (starts unavailable, "
            "retries on the normal poll cycle)", serial,
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """
    Sets up one tank entry (one or more devices, CONF_DEVICES).

    Devices are probed in RSSI order with discover_tank_for_serial() until
    one is reachable. That device becomes the gateway and its first refresh
    decides whether the entry is ready (ConfigEntryNotReady if no device is
    reachable). The same connection returns the mesh address of every peer;
    only peers it didn't report get a direct connection. Every other device
    gets a non-blocking first refresh, so an unreachable device never blocks
    the rest of the tank.
    """
    semaphore, registry = _shared_state(hass)

    devices = entry.data.get(CONF_DEVICES)
    if not devices:
        # Entries from before tank entries stored one device at the top
        # level; they can't be migrated automatically.
        raise ConfigEntryError(
            f"This Mobius entry ({entry.title!r}) was set up before tank-aware, "
            "multi-device config entries were added and is missing its device "
            "list. Please remove and re-add it."
        )

    pan_id = entry.data.get(CONF_PAN_ID)
    if pan_id is None:
        raise ConfigEntryError(
            f"This Mobius entry ({entry.title!r}) was set up before pan_id-based "
            "device grouping was added and is missing its pan_id. Please remove "
            "and re-add it."
        )

    _register_tank_device(hass, entry, entry.data.get(CONF_MLPREFIX), pan_id)
    _notify_single_device_tank(hass, entry)

    rssi_by_serial = {d[CONF_SERIAL]: _current_rssi(hass, d[CONF_SERIAL]) for d in devices}
    # Bluetooth-only devices (not on the mesh) are set up separately below:
    # never probed as gateway, never joined to the registry's group.
    bluetooth_only = {d[CONF_SERIAL] for d in devices if advertised_as_bluetooth_only(hass, d[CONF_SERIAL])}
    mesh_devices = [d for d in devices if d[CONF_SERIAL] not in bluetooth_only]
    devices_by_rssi = sorted(
        mesh_devices, key=lambda d: rssi_by_serial[d[CONF_SERIAL]] or -999, reverse=True,
    )
    _LOGGER.debug(
        "Probing %r's %d device(s) in RSSI order: %s",
        entry.title, len(devices_by_rssi),
        [(d[CONF_SERIAL], rssi_by_serial[d[CONF_SERIAL]]) for d in devices_by_rssi],
    )
    working_serial: str | None = None
    addresses_by_serial: dict[str, bytes] = {}
    last_probe_error: Exception | None = None
    for device in devices_by_rssi:
        candidate_serial = device[CONF_SERIAL]
        try:
            tank = await discover_tank_for_serial(hass, candidate_serial, semaphore)
        except Exception as err:  # pragma: no cover -- discover_tank_for_serial doesn't raise
            last_probe_error = err
            tank = None
        # A Tank with prefix=None (device not in a Thread network) still
        # means the device is reachable, which is all that matters here.
        if tank is None:
            _LOGGER.debug(
                "Could not reach %s while looking for a working device to set up "
                "%r with -- trying the next one", candidate_serial, entry.title,
            )
            continue
        working_serial = candidate_serial
        addresses_by_serial = {peer.serial: peer.address for peer in tank.peers}
        _LOGGER.debug(
            "%s is the working device for %r -- its own mesh view reported %d peer(s): %s",
            working_serial, entry.title, len(tank.peers), sorted(addresses_by_serial),
        )
        break

    if working_serial is None and mesh_devices:
        raise ConfigEntryNotReady(
            f"Could not connect to any of {len(mesh_devices)} device(s) in {entry.title!r}"
            + (f": {last_probe_error}" if last_probe_error else "")
        )

    coordinators: dict[str, MobiusDeviceCoordinator] = {}
    # The working device joins first: another member joining first would
    # start the RSSI election, and prefer_as_gateway would then be ignored.
    ordered_devices = sorted(mesh_devices, key=lambda d: d[CONF_SERIAL] != working_serial)
    for device in ordered_devices:
        serial = device[CONF_SERIAL]
        group = await registry.join(pan_id, serial, rssi_by_serial[serial], prefer_as_gateway=(serial == working_serial))

        if group.members[serial].mesh_address is None:
            address = addresses_by_serial.get(serial)
            if address is None:
                address = await discover_mesh_address(hass, serial, semaphore)
            if address is not None:
                registry.update_mesh_address(pan_id, serial, address)
            else:
                _LOGGER.debug(
                    "Could not proactively discover mesh address for %s at setup -- "
                    "will retry on the next poll cycle", serial,
                )

        coordinator = MobiusDeviceCoordinator(hass, entry, registry, serial, pan_id)
        if serial == working_serial:
            await coordinator.async_config_entry_first_refresh()
        else:
            await _async_soft_first_refresh(coordinator, serial)
        coordinators[serial] = coordinator

    for device in devices:
        serial = device[CONF_SERIAL]
        if serial not in bluetooth_only:
            continue
        coordinator = MobiusDeviceCoordinator(hass, entry, registry, serial, pan_id, bluetooth_only=True)
        if not mesh_devices and not coordinators:
            # A tank of Bluetooth-only devices: the first one decides
            # whether the entry is ready.
            await coordinator.async_config_entry_first_refresh()
        else:
            await _async_soft_first_refresh(coordinator, serial)
        coordinators[serial] = coordinator

    entry.runtime_data = MobiusRuntimeData(coordinators=coordinators)
    _LOGGER.debug(
        "%r set up: %d/%d device(s) immediately available (gateway: %s)",
        entry.title,
        sum(1 for c in coordinators.values() if c.last_update_success),
        len(coordinators), working_serial,
    )

    # Periodic task, cancelled on unload. hass.create_task() (not
    # async_create_task()) because the interval callback isn't guaranteed to
    # run on the event loop thread.
    entry.async_on_unload(
        async_track_time_interval(
            hass, lambda now: hass.create_task(_async_revalidate_tank(hass, entry, now)),
            TANK_REVALIDATION_INTERVAL,
        )
    )
    # The clock is checked after every poll of any device of the tank.
    for coordinator in coordinators.values():
        entry.async_on_unload(
            coordinator.async_add_listener(lambda: hass.async_create_task(_async_check_tank_time(hass, entry)))
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unloads an entry. Each device leaves the registry, which promotes a
    new gateway (and disconnects the old one) when needed; a Bluetooth-only
    device closes its own connection."""
    # The warning only applies while the entry is running; disabling it is
    # one of the ways out of it.
    _dismiss_single_device_notification(hass, entry)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    runtime: MobiusRuntimeData | None = getattr(entry, "runtime_data", None)
    registry: GatewayRegistry | None = hass.data.get(DOMAIN, {}).get("gateway_registry")
    if runtime is not None and registry is not None:
        for coordinator in runtime.coordinators.values():
            if coordinator.bluetooth_only:
                await coordinator.direct_connection.disconnect()
            else:
                await registry.leave(coordinator.pan_id, coordinator.serial)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    On permanent removal (not on reload), clears Home Assistant's Bluetooth
    match history for the entry's devices, so they are discovered again
    without a restart. Only devices currently advertising (found by serial
    in the Bluetooth cache) can be handled.
    """
    _dismiss_single_device_notification(hass, entry)

    known_serials = {d[CONF_SERIAL] for d in entry.data.get(CONF_DEVICES, [])}
    if not known_serials:
        return

    addresses_by_serial: dict[str, str] = {}
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        parsed = parse_advertisement(info.manufacturer_data)
        if parsed and parsed.serial in known_serials:
            addresses_by_serial[parsed.serial] = info.address

    for serial, address in addresses_by_serial.items():
        bluetooth.async_rediscover_address(hass, address)
        _LOGGER.debug(
            "Cleared Bluetooth rediscovery for %s (%s) after its entry was removed",
            serial, address,
        )
