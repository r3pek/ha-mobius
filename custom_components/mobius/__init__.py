"""The Mobius integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval

from mobius import MOBIUS_COMPANY_ID, parse_manufacturer_data

from .const import (
    DOMAIN, MAX_CONCURRENT_CONNECTIONS, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX,
    TANK_REVALIDATION_INTERVAL,
)
from .coordinator import MobiusDeviceCoordinator, discover_mesh_address, discover_tank_for_serial
from .gateway_registry import GatewayRegistry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]

# This integration is config-entry-only (config_flow: true in manifest.json,
# devices discovered via Bluetooth or added manually through the UI) -- no
# YAML configuration.yaml support at all. cv.config_entry_only_config_schema
# is the confirmed-correct helper for exactly this case: it both satisfies
# hassfest's requirement that any integration implementing async_setup
# define one of CONFIG_SCHEMA/PLATFORM_SCHEMA/PLATFORM_SCHEMA_BASE (or one
# of its helper equivalents), and gives a clear, real error if someone
# tries to configure this integration via YAML anyway, rather than a
# confusing failure.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def tank_device_identifier(mlprefix_hex: str) -> tuple[str, str]:
    """The synthetic tank device's own device-registry identifier -- a
    real device_registry entry with no coordinator/entities of its own,
    existing purely so every real device's own DeviceInfo can point
    via_device at it (see sensor.py), producing the same "one hub, N
    child devices" grouping this whole feature was designed against (a
    Home Assistant Bluetooth/DHCP/etc-discovered hub with sub-devices --
    not a Mobius-specific mechanism, see this integration's own design
    notes). Shared here (rather than inlined at each of the two call
    sites -- registration below, and via_device in sensor.py) so both
    sides can never drift apart on the exact identifier shape.
    """
    return (DOMAIN, f"tank_{mlprefix_hex}")


@dataclass
class MobiusRuntimeData:
    """One entry, one-or-more devices -- see const.py's own module-level
    docstring for the full CONF_DEVICES data shape this mirrors at
    runtime. Keyed by serial, matching how every other per-device lookup
    in this integration already works (gateway_registry.PanGroup.members,
    for instance)."""
    coordinators: dict[str, MobiusDeviceCoordinator] = field(default_factory=dict)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up integration-wide shared state (the connection semaphore
    and the gateway registry -- both genuinely global, shared across
    every config entry, not per-entry)."""
    hass.data.setdefault(DOMAIN, {})
    semaphore = hass.data[DOMAIN].setdefault(
        "connection_semaphore", asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)
    )
    hass.data[DOMAIN].setdefault("gateway_registry", GatewayRegistry(hass, semaphore))
    return True


def _current_rssi(hass: HomeAssistant, serial: str) -> int | None:
    """Best-effort RSSI lookup from Home Assistant's own Bluetooth cache
    for whichever address is currently advertising this serial -- used
    only for initial gateway election (see gateway_registry.py); not
    finding one just means this device's join() proceeds without RSSI
    info, matching the registry's own graceful fallback."""
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        payload = info.manufacturer_data.get(MOBIUS_COMPANY_ID)
        if not payload:
            continue
        parsed = parse_manufacturer_data(payload)
        if parsed and parsed.serial == serial:
            return info.rssi
    return None


def _register_tank_device(hass: HomeAssistant, entry: ConfigEntry, mlprefix_hex: str, device_count: int) -> None:
    """Registers (or updates) the synthetic tank device real devices'
    own DeviceInfo will point via_device at -- see tank_device_identifier()
    for why this exists at all. Idempotent: safe to call on every setup
    (including every Home Assistant restart, not just first-ever setup),
    since async_get_or_create() is itself idempotent. device_count isn't
    stored directly (it would just duplicate what the entry's own,
    renameable title already conveys by default, e.g. "Mobius Tank (2
    devices)") -- accepted as a parameter mainly so callers don't need
    to recompute len(devices) themselves, and to keep this function's
    signature self-documenting about what it needs to know."""
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={tank_device_identifier(mlprefix_hex)},
        name=entry.title,
        manufacturer="EcoTech Marine",
        model="Tank",
    )


async def _async_revalidate_tank(hass: HomeAssistant, entry: ConfigEntry, now=None) -> None:
    """
    Periodic per-entry membership check, run every TANK_REVALIDATION_INTERVAL
    (see that constant's own docstring for why this is deliberately
    infrequent) -- reuses the entry's existing gateway connection (no new
    BLE connect/disconnect cycle in the common case, since the gateway is
    already connected most of the time for its own regular polling) to
    ask what else is currently on its Thread mesh.

    Three real, considered outcomes:

    - A tracked device now reported on a DIFFERENT, already-known entry's
      own mesh is auto-migrated: removed from THIS entry, added to that
      one, both reloaded. Matches the same "merge, don't re-prompt"
      philosophy discovery-time merging already uses (see config_flow.
      py's own module docstring) -- this is the same underlying event (a
      device belongs somewhere else now), just detected later instead of
      at first advertisement.
    - A tracked device that simply isn't reported anymore is left
      exactly where it is -- deliberately, permanently NEVER auto-
      removed by this function. A single absence from one scan proves
      nothing on its own (a transient connection issue, the device
      briefly out of range, mid-reboot) and this maintenance task isn't
      the right place to make that call -- MARK_UNAVAILABLE_AFTER
      already handles "hasn't responded in a while" without deleting
      anything, and that's as far as this goes without a person
      deciding to remove it themselves.
    - A completely new, never-before-seen serial reported on this mesh
      is ignored here entirely -- discovering brand-new devices is
      already config_flow.py's own job, triggered by that device's own
      Bluetooth advertisement, not this task's.

    A failed check (gateway unreachable, read timeout, anything) is
    logged and skipped, not retried immediately -- the next scheduled
    run acts as its own retry, matching the same "one bad read isn't
    itself actionable" reasoning GATEWAY_FAILURE_THRESHOLD's own
    docstring gives elsewhere in this integration.
    """
    registry: GatewayRegistry | None = hass.data.get(DOMAIN, {}).get("gateway_registry")
    if registry is None:
        return
    pan_id = entry.data.get(CONF_PAN_ID)
    if pan_id is None:
        return
    group = registry.group(pan_id)
    if group is None or group.gateway_serial is None:
        _LOGGER.debug(
            "Skipping tank revalidation for %r -- no gateway currently elected", entry.title,
        )
        return

    try:
        mdevice = await group.gateway_connection.ensure_connected()
        peers = await mdevice.discover_mesh_peers_auto()
    except Exception as err:
        _LOGGER.debug(
            "Tank revalidation for %r failed (will retry at the next scheduled "
            "check, in %s): %s", entry.title, TANK_REVALIDATION_INTERVAL, err,
        )
        return

    # Local import -- avoids config_flow.py (and everything IT imports:
    # voluptuous, the bluetooth component's own discovery helpers, etc.)
    # being eagerly loaded every time this integration itself loads, the
    # same reasoning discovery.py's own discover_tank() already gives for
    # its own local imports elsewhere in this project.
    from .config_flow import (
        _find_entry_containing_serial, _merge_device_into_entry, _remove_device_from_entry,
    )

    known_serials = {d[CONF_SERIAL] for d in entry.data.get(CONF_DEVICES, [])}
    for peer in peers:
        if peer.serial in known_serials:
            continue  # already tracked here -- nothing to do
        other_entry = _find_entry_containing_serial(hass, peer.serial)
        if other_entry is None or other_entry.entry_id == entry.entry_id:
            # Either a genuinely new device this task doesn't handle, or
            # (shouldn't normally happen, since known_serials already
            # excludes it) already tracked right here.
            continue
        _LOGGER.info(
            "Device %s found on %r's mesh but was tracked under %r -- migrating it",
            peer.serial, entry.title, other_entry.title,
        )
        await _remove_device_from_entry(hass, other_entry, peer.serial)
        await _merge_device_into_entry(hass, entry, peer.serial)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mobius from a config entry -- one Thread mesh/"tank" (see
    gateway_registry.py's own docstring for why pan_id is the
    established local proxy for this), which may hold one or more
    physical devices (CONF_DEVICES) -- not necessarily one, the way a
    single ad-hoc device's own entry still uses the exact same shape
    with a one-element list (see config_flow.py's own module docstring
    for the full merge/tank/ad-hoc design)."""
    hass.data.setdefault(DOMAIN, {})
    semaphore = hass.data[DOMAIN].setdefault(
        "connection_semaphore", asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)
    )
    registry: GatewayRegistry = hass.data[DOMAIN].setdefault(
        "gateway_registry", GatewayRegistry(hass, semaphore)
    )

    devices = entry.data.get(CONF_DEVICES)
    if not devices:
        # Entries created before tank-aware, CONF_DEVICES-based entries
        # existed (the old shape stored a single device's own
        # CONF_SERIAL/CONF_ADDRESS directly at the top level, not nested
        # under a list at all). There's no safe, automatic way to migrate
        # that shape forward, so ask for a clean re-setup rather than
        # guessing.
        raise ConfigEntryError(
            f"This Mobius entry ({entry.title!r}) was set up before tank-aware, "
            "multi-device config entries were added and is missing its device "
            "list. Please remove and re-add it."
        )

    pan_id = entry.data.get(CONF_PAN_ID)
    if pan_id is None:
        # Entries created before pan_id-based gateway grouping was added.
        # Same reasoning as the CONF_DEVICES check above -- there's no
        # safe way to know which group these devices belong to without it.
        raise ConfigEntryError(
            f"This Mobius entry ({entry.title!r}) was set up before pan_id-based "
            "device grouping was added and is missing its pan_id. Please remove "
            "and re-add it."
        )

    mlprefix_hex = entry.data.get(CONF_MLPREFIX)
    # Only registers a synthetic tank ("hub") device -- and therefore
    # only gets real devices' own via_device grouping under it, see
    # sensor.py -- for a genuine multi-device tank. A single ad-hoc
    # device (no confirmed tank prefix at all, or a tank entry that
    # currently only has one device in it e.g. right after the first of
    # a two-device tank was added but before the second was merged in)
    # skips this entirely: a "hub" with one child device (or none real
    # yet) would just be UI noise, not useful grouping.
    if mlprefix_hex is not None and len(devices) > 1:
        _register_tank_device(hass, entry, mlprefix_hex, len(devices))

    # A REAL, CONFIRMED PRODUCTION BUG lived in an earlier version of
    # this function: coordinator.async_config_entry_first_refresh() was
    # awaited in a loop, once per device. That method's whole contract
    # is "raise ConfigEntryNotReady if this fails" -- fine for the ONE
    # coordinator a typical integration has, but here it meant ANY
    # single device out of several failing (even the very last one)
    # raised out of this function entirely, aborting the WHOLE entry's
    # setup -- discarding every other device that had already
    # succeeded moments earlier. Confirmed via a real log: a tank with
    # 4 devices repeatedly failed to load at all, a DIFFERENT device
    # timing out on each retry, because every retry re-ran this same
    # loop from the very start.
    #
    # Fixed with a two-phase setup: first, PROBE devices (strongest
    # RSSI first, not just CONF_DEVICES' own stored order, so a single
    # consistently-unreachable device is never retried forever while a
    # perfectly reachable one sits right there unused) via
    # discover_mesh_address() -- a real, minimal connect-and-read, not
    # just registry bookkeeping -- until one actually succeeds. Only
    # THAT device is committed as gateway and gated on
    # async_config_entry_first_refresh() -- the one case this entry's
    # own readiness legitimately SHOULD depend on: if literally nothing
    # in the tank is reachable, there's genuinely nothing to set up
    # yet. Every other device uses the soft async_refresh() (per Home
    # Assistant's own developer docs: "If you do not want to retry
    # setup on failure, use coordinator.async_refresh() instead") --
    # its own failure, even on its very first read, does not raise, so
    # it can never block the rest of the tank. That device's own
    # entities simply start unavailable and retry on the normal poll
    # cycle, exactly like any other transient failure after setup.
    #
    # Uses discover_tank_for_serial() for the probe, not the narrower
    # discover_mesh_address() an earlier version of this used -- a real,
    # unnecessary inefficiency: that one connection already learns the
    # WHOLE tank's peer list, with every peer's own mesh address
    # (discover_tank_for_serial() calls python-mobius's own
    # discover_tank(), which reads NetworkedThreadDevices over the
    # Thread mesh -- the same CoAP-relayed mechanism this integration's
    # own relay reads already depend on), not just the probed device's
    # own address. An earlier version of this function ignored that and
    # went on to open a SEPARATE, direct BLE connection to every OTHER
    # device in the tank too, just to learn each one's address
    # individually -- on a brand-new tank, that meant N+1 total
    # connections during setup for an N-device tank, when the mesh
    # already handed us N-1 of those addresses for free from the first
    # one. Now only a genuinely MISSING peer (not reported at all by the
    # probed device's own mesh view, however that happened) falls back
    # to a direct per-device connection.
    rssi_by_serial = {d[CONF_SERIAL]: _current_rssi(hass, d[CONF_SERIAL]) for d in devices}
    devices_by_rssi = sorted(
        devices, key=lambda d: rssi_by_serial[d[CONF_SERIAL]] or -999, reverse=True,
    )
    working_serial: str | None = None
    addresses_by_serial: dict[str, bytes] = {}
    last_probe_error: Exception | None = None
    for device in devices_by_rssi:
        candidate_serial = device[CONF_SERIAL]
        try:
            tank = await discover_tank_for_serial(hass, candidate_serial, semaphore)
        except Exception as err:  # pragma: no cover -- discover_tank_for_serial is already defensive
            last_probe_error = err
            tank = None
        if tank is None:
            # Genuinely couldn't reach this candidate at all -- try the
            # next one. Deliberately NOT also checking tank.prefix here:
            # a non-None Tank with prefix=None means "reached this
            # device fine, it just isn't currently part of any
            # provisioned Thread network" -- completely normal and
            # expected for an ad-hoc, single-device entry (the entire
            # reason it's ad-hoc rather than a tank in the first place),
            # and even for a genuine tank entry, this device is still
            # BLE-reachable regardless -- which is what actually matters
            # for committing to it below. If it turns out it can't
            # relay to its peers because of this, that surfaces as
            # those peers' own coordinators failing softly (see below),
            # not as this whole probe failing.
            _LOGGER.debug(
                "Could not reach %s while looking for a working device to set up "
                "%r with -- trying the next one", candidate_serial, entry.title,
            )
            continue
        working_serial = candidate_serial
        addresses_by_serial = {peer.serial: peer.address for peer in tank.peers}
        break

    if working_serial is None:
        raise ConfigEntryNotReady(
            f"Could not connect to any of {len(devices)} device(s) in {entry.title!r}"
            + (f": {last_probe_error}" if last_probe_error else "")
        )

    coordinators: dict[str, MobiusDeviceCoordinator] = {}
    # working_serial's own device is processed FIRST, regardless of its
    # position in CONF_DEVICES -- a REAL, subtle bug lived in processing
    # devices in plain CONF_DEVICES order: if a different, non-working
    # device happened to be listed first, ITS join() call (without
    # prefer_as_gateway) would trigger the normal RSSI-based election
    # before working_serial's own preferred join() ever ran -- by the
    # time that one arrived, group._electing was already true, so
    # prefer_as_gateway got silently ignored, racing against (and
    # sometimes losing to) the settle-window election instead of
    # reliably using the device this whole probe just confirmed working.
    ordered_devices = sorted(devices, key=lambda d: d[CONF_SERIAL] != working_serial)
    for device in ordered_devices:
        serial = device[CONF_SERIAL]
        rssi = rssi_by_serial[serial]
        group = await registry.join(pan_id, serial, rssi, prefer_as_gateway=(serial == working_serial))

        if group.members[serial].mesh_address is None:
            address = addresses_by_serial.get(serial)
            if address is not None:
                # Already known -- either the probed device's own
                # address, or one of its peers' addresses the same
                # single connection already reported. No extra
                # connection needed either way.
                registry.update_mesh_address(pan_id, serial, address)
            else:
                # Genuinely not reported by the probe -- fall back to a
                # direct connection for this one device specifically.
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
            await coordinator.async_refresh()
        coordinators[serial] = coordinator

    entry.runtime_data = MobiusRuntimeData(coordinators=coordinators)

    # Periodic membership re-check -- see _async_revalidate_tank()'s own
    # docstring for the full reasoning. async_on_unload() means this
    # gets cleanly canceled on unload/reload without any separate
    # bookkeeping here -- Home Assistant calls the returned unsub
    # callback automatically.
    entry.async_on_unload(
        async_track_time_interval(
            hass,
            lambda now: hass.async_create_task(_async_revalidate_tank(hass, entry, now)),
            TANK_REVALIDATION_INTERVAL,
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry. For each of its devices, leaving the
    registry promotes a replacement gateway (and disconnects the old
    gateway connection) automatically if that device was its group's
    gateway -- see gateway_registry.leave()."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    runtime: MobiusRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is not None:
        registry: GatewayRegistry | None = hass.data.get(DOMAIN, {}).get("gateway_registry")
        if registry is not None:
            for coordinator in runtime.coordinators.values():
                await registry.leave(coordinator.pan_id, coordinator.serial)

    return unload_ok
