"""The Mobius integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN, MAX_CONCURRENT_CONNECTIONS, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX,
    TANK_REVALIDATION_INTERVAL, TANK_TIME_SYNC_INTERVAL, SOFT_REFRESH_RETRY_ATTEMPTS,
    SOFT_REFRESH_RETRY_DELAY,
)
from .coordinator import (
    MobiusDeviceCoordinator, _find_in_bluetooth_cache, discover_mesh_address, discover_tank_for_serial,
    parsed_advertisement,
)
from .gateway_registry import GatewayRegistry

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BUTTON]

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
    # The rest are populated once by sensor.py's own async_setup_entry(),
    # consulted later by _async_ensure_sensors_exist() below -- see that
    # function's own docstring for why it needs to exist at all.
    sensor_add_entities: Optional[AddEntitiesCallback] = None
    sensor_device_infos: dict[str, DeviceInfo] = field(default_factory=dict)
    created_sensor_unique_ids: set[str] = field(default_factory=set)


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
        parsed = parsed_advertisement(info.manufacturer_data)
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


async def _async_ensure_sensors_exist(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    sensor.py's own async_setup_entry() decides which type-specific
    entities to create for a device (pump vs light --
    OperationState/MotorSpeed/etc, or LightChannelIntensity/
    Calibration) from a ONE-TIME snapshot of that device's
    coordinator.data, taken right at setup. For a relayed (non-gateway)
    member of a tank specifically, that snapshot can still be empty at
    that exact moment: its own first read deliberately uses a soft,
    non-blocking async_refresh() rather than the gateway's own
    blocking, retried async_config_entry_first_refresh() (so one
    unreachable device can never hold up the rest of the tank's setup --
    see this module's own async_setup_entry()). If that one soft
    attempt happens to fail -- a relayed read on a freshly-starting
    integration, especially right after a Home Assistant restart when
    the Bluetooth cache itself may not be warm yet, is exactly the kind
    of thing that can transiently fail once -- the type-specific
    entities for that device simply never get created for this session
    at all, even though every later poll succeeds fine. The
    always-created generic entities (support tier, error state, etc)
    recover on their own once the coordinator succeeds, since they
    already exist and only their own `available` needs to flip; these
    don't, since entity creation itself only ever happens once per
    session.

    The fix is this: periodically check whether each known device's
    CURRENT data would produce any entities beyond what was already
    created, and add just the ones that are missing. Self-healing,
    deliberately, rather than trying to fully rule the original gap out
    at setup time -- a fixed retry count there narrows the window but
    can't fully close it (nothing guarantees the Bluetooth cache is warm
    again within any fixed number of attempts), while this keeps
    checking on every regular revalidation cycle for as long as it
    takes, with no risk of ever creating a duplicate (compared against
    already-created unique_ids every time, never assumed from whether
    this device's support was previously unknown).
    """
    runtime: MobiusRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is None or runtime.sensor_add_entities is None:
        return

    # Local import: sensor.py itself imports from this module
    # (MobiusRuntimeData, tank_device_identifier) -- a top-level import
    # here would be circular.
    from .sensor import _build_type_specific_entities, _build_advanced_feature_entities

    new_entities = []
    for serial, coordinator in runtime.coordinators.items():
        device_info = runtime.sensor_device_infos.get(serial)
        if device_info is None:
            continue  # this device's own sensor entities were never set up at all yet
        data = coordinator.data or {}
        support = data.get("support", "")
        if not support:
            continue  # still nothing to build from -- next cycle's own retry
        candidates = _build_type_specific_entities(coordinator, serial, device_info, support, data)
        # Independent of "support" entirely -- see _build_advanced_feature_
        # entities()'s own docstring for why gating this on pump/light type
        # would reintroduce exactly the per-device-type hardcoding this was
        # built to avoid. Still gated on the same "not support: continue"
        # above, though -- that's really just a proxy for "has this device's
        # coordinator ever completed a successful fetch at all", which
        # get_advanced_features() itself depends on regardless.
        candidates += _build_advanced_feature_entities(coordinator, serial, device_info, data)
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
    Periodic per-entry check, run every TANK_REVALIDATION_INTERVAL (see
    that constant's own docstring for the full reasoning behind its
    current value) -- reuses the entry's existing gateway connection (no
    new BLE connect/disconnect cycle in the common case, since the
    gateway is already connected most of the time for its own regular
    polling) to ask what else is currently on its Thread mesh, AND to
    keep every known member's own connection info current.

    Four real jobs, not three:

    1. RECOVERY, if there's currently no gateway at all for this tank
       (every candidate previously exhausted -- see gateway_registry.py's
       own PanGroup.recently_failed_gateways docstring -- or a single-
       device tank whose only member kept failing). registry.join() is
       reused rather than reimplementing election here: its own
       gateway_serial-is-None check already re-triggers a normal election
       for an EXISTING group exactly the same way it does for a brand-new
       one, so this just needs to call it, not duplicate its logic.
    2. PROACTIVE CACHE REFRESH, if there IS a gateway but its own
       connection isn't currently open: requests a one-shot active
       Bluetooth scan if that gateway isn't currently visible in Home
       Assistant's own advertisement cache either, before this same
       cycle goes on to actually try using the connection below -- see
       that check's own inline comment for the full reasoning (including
       why it's specifically gated on "not already connected").
    3. MAINTENANCE, once a gateway does exist: every known member's own
       mesh address and mesh-last-seen data gets refreshed from this
       same read, opportunistically -- not just the migration check this
       is otherwise scoped to. This is what gets a device whose own
       address was never successfully discovered back in on its own,
       since nothing else keeps retrying it. Deliberately best-effort per
       member (a peer simply not reported this particular round is left
       as whatever was already cached, not treated as an error) --
       matching the rest of this function's own "one bad round proves
       nothing, the next scheduled run is the retry" philosophy, not
       something that needs every member to succeed at once to be worth
       doing at all.
    4. ENSURING EVERY DEVICE'S OWN SENSOR ENTITIES ACTUALLY EXIST -- see
       _async_ensure_sensors_exist()'s own docstring (an entity that was
       never created at setup time, for a device whose own data legitimately
       wasn't ready yet at that exact moment, never gets a second chance
       otherwise). Deliberately independent of the gateway-connection
       logic below -- this only needs each device's own coordinator.data,
       already fetched on its own regular poll cycle regardless of
       whether that specific gateway connection right now is healthy.

    Migration detection itself is unchanged from before:

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
    if group is None:
        return

    # Independent of everything below -- see this function's own
    # docstring (job 4) and _async_ensure_sensors_exist()'s own for why
    # this doesn't need (or wait on) gateway connection health at all.
    await _async_ensure_sensors_exist(hass, entry)

    known_devices = entry.data.get(CONF_DEVICES, [])
    if group.gateway_serial is None:
        if not known_devices:
            return
        # Any one known member is enough -- the election this triggers
        # considers every current member of the group regardless of
        # which one's own join() call happened to be the one that
        # re-triggered it (see gateway_registry.py's own join(), which
        # only looks at group.members as a whole).
        recovery_serial = known_devices[0][CONF_SERIAL]
        recovery_rssi = (
            group.members[recovery_serial].rssi if recovery_serial in group.members else None
        )
        await registry.join(pan_id, recovery_serial, rssi=recovery_rssi)
        _LOGGER.debug(
            "Tank %r had no gateway at all -- re-triggered election (result "
            "picked up by that device's own next regular poll cycle)", entry.title,
        )
        return

    # Proactive: if the gateway's own connection isn't currently open,
    # check whether it's even visible in Home Assistant's own Bluetooth
    # cache right now, and request a one-shot active scan if not --
    # BEFORE this cycle's own attempt to actually use the connection
    # below, so a successful scan has a real chance to help THIS cycle
    # too, not just whichever coordinator happens to hit the same wall
    # next. A real, confirmed production incident is what this
    # addresses: a tank's own gateway going missing from that cache for
    # hours at a stretch, discovered only reactively, poll cycle after
    # poll cycle, once something actually needed to connect and failed.
    #
    # Deliberately gated on "connection not already open": a currently-
    # connected device legitimately stops advertising while connected
    # (busy talking to us, not free to advertise) -- checking the
    # advertisement cache for a healthy, already-connected gateway
    # would be a false alarm, not a real signal of trouble, and would
    # trigger an active scan every single cycle for no reason.
    if not group.gateway_connection.is_connected:
        if _find_in_bluetooth_cache(hass, group.gateway_serial) is None:
            _LOGGER.debug(
                "Tank %r's own gateway %r isn't currently connected, and wasn't found "
                "in Home Assistant's Bluetooth cache either -- requesting an active scan",
                entry.title, group.gateway_serial,
            )
            await bluetooth.async_request_active_scan(hass)

    try:
        mdevice = await group.gateway_connection.ensure_connected()
        peers = await mdevice.discover_mesh_peers_auto()
    except Exception as err:
        _LOGGER.debug(
            "Tank revalidation for %r failed (will retry at the next scheduled "
            "check, in %s): %s", entry.title, TANK_REVALIDATION_INTERVAL, err,
        )
        return

    known_serials = {d[CONF_SERIAL] for d in known_devices}
    now_utc = dt_util.utcnow()
    for peer in peers:
        if peer.serial not in known_serials:
            continue  # migration candidate, not a maintenance target -- handled below
        if peer.address is not None:
            registry.update_mesh_address(pan_id, peer.serial, peer.address)
        if peer.age is not None:
            registry.update_mesh_last_seen(
                pan_id, peer.serial, now_utc - timedelta(milliseconds=peer.age),
            )

    reported_serials = {p.serial for p in peers}
    _LOGGER.debug(
        "Tank %r revalidated: %d/%d known device(s) reported by the mesh this cycle%s",
        entry.title, len(known_serials & reported_serials), len(known_serials),
        "" if known_serials <= reported_serials
        else f" (missing: {sorted(known_serials - reported_serials)})",
    )

    # Local import -- avoids config_flow.py (and everything IT imports:
    # voluptuous, the bluetooth component's own discovery helpers, etc.)
    # being eagerly loaded every time this integration itself loads, the
    # same reasoning discovery.py's own discover_tank() already gives for
    # its own local imports elsewhere in this project.
    from .config_flow import (
        _find_entry_containing_serial, _merge_device_into_entry, _remove_device_from_entry,
    )

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


async def _async_sync_tank_time(hass: HomeAssistant, entry: ConfigEntry, now=None) -> None:
    """
    Periodic per-entry write, run every TANK_TIME_SYNC_INTERVAL -- calls
    MobiusDevice.set_time_to_now() (Epoch, reserved-byte group=1) once
    against this tank's own current gateway. python-mobius's own
    docstring for that method has the full confirmation, but in short:
    a single write appears to genuinely propagate the new time to every
    OTHER device on the same mesh, confirmed via real hardware testing
    -- so this deliberately writes ONCE per tank, to whichever device
    is currently the gateway, matching the app's own approach, not
    every device individually.

    Mirrors _async_revalidate_tank()'s own resolution pattern exactly
    (registry lookup, pan_id, group) but is otherwise fully independent
    of it -- this doesn't touch mesh membership, sensor entities, or
    connection health checks, just the one write. Reuses the tank's own
    existing gateway connection (group.gateway_connection) -- no new
    BLE connect/disconnect cycle in the common case, since the gateway
    is already connected most of the time for its own regular polling.

    Runs for a single-device ("ad-hoc") tank too, not just genuine
    multi-tank setups -- a lone device's own clock can still drift on
    its own, with nothing else to propagate from/to, but that's still
    worth fixing on its own.

    A failed write (no gateway currently available, connection failure,
    device rejected the write) is logged and skipped, not retried
    immediately -- the next scheduled run acts as its own retry,
    matching the same "one bad round proves nothing" philosophy
    _async_revalidate_tank() itself already uses.
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
            "Tank %r: no gateway currently available for time sync this cycle "
            "-- will retry next scheduled run", entry.title,
        )
        return

    _LOGGER.debug(
        "Tank %r: syncing time via gateway %s (pan_id %#06x)",
        entry.title, group.gateway_serial, pan_id,
    )
    try:
        device = await group.gateway_connection.ensure_connected()
        await device.set_time_to_now()
    except Exception as err:
        _LOGGER.warning(
            "Tank %r: time sync via gateway %s failed (%s) -- will retry next "
            "scheduled run", entry.title, group.gateway_serial, err,
        )
        return
    _LOGGER.debug(
        "Tank %r: time sync via gateway %s succeeded", entry.title, group.gateway_serial,
    )


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

    # Two-phase setup, deliberately: first, PROBE devices (strongest
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
    # Keeping the ConfigEntryNotReady contract scoped to just the ONE
    # gateway-probe coordinator, rather than looping it across every
    # device in the tank, means one unreachable device out of several
    # can never abort the whole entry's setup and discard every other
    # device that already succeeded.
    #
    # Uses discover_tank_for_serial() for the probe, not the narrower
    # discover_mesh_address(): that one connection already learns the
    # WHOLE tank's peer list, with every peer's own mesh address
    # (discover_tank_for_serial() calls python-mobius's own
    # discover_tank(), which reads NetworkedThreadDevices over the
    # Thread mesh -- the same CoAP-relayed mechanism this integration's
    # own relay reads already depend on), not just the probed device's
    # own address. Now only a genuinely MISSING peer (not reported at
    # all by the probed device's own mesh view, however that happened)
    # falls back to a direct per-device connection, rather than opening
    # a separate, direct BLE connection to every device in the tank
    # just to learn each one's own address individually.
    rssi_by_serial = {d[CONF_SERIAL]: _current_rssi(hass, d[CONF_SERIAL]) for d in devices}
    devices_by_rssi = sorted(
        devices, key=lambda d: rssi_by_serial[d[CONF_SERIAL]] or -999, reverse=True,
    )
    _LOGGER.debug(
        "Probing %r's %d device(s) in RSSI order: %s",
        entry.title, len(devices),
        [(d[CONF_SERIAL], rssi_by_serial[d[CONF_SERIAL]]) for d in devices_by_rssi],
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
        _LOGGER.debug(
            "%s is the working device for %r -- its own mesh view reported %d peer(s): %s",
            working_serial, entry.title, len(tank.peers), sorted(addresses_by_serial),
        )
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
            attempt = 1
            # See SOFT_REFRESH_RETRY_ATTEMPTS's own docstring in const.py
            # for the full reasoning -- a bounded, real chance for a
            # transient first-attempt failure to actually resolve before
            # setup moves on, not a guarantee (that's what
            # _async_ensure_sensors_exist()'s own self-healing check is
            # for). Still soft throughout -- never raises, same as the
            # single attempt this replaces.
            while not coordinator.last_update_success and attempt <= SOFT_REFRESH_RETRY_ATTEMPTS:
                _LOGGER.debug(
                    "%s's own first soft refresh at setup failed -- retrying "
                    "(attempt %d/%d, %ss apart)", serial, attempt,
                    SOFT_REFRESH_RETRY_ATTEMPTS, SOFT_REFRESH_RETRY_DELAY,
                )
                await asyncio.sleep(SOFT_REFRESH_RETRY_DELAY)
                await coordinator.async_refresh()
                if coordinator.last_update_success:
                    _LOGGER.debug(
                        "%s came up on retry %d/%d -- would otherwise have started "
                        "unavailable this session for no real reason", serial, attempt,
                        SOFT_REFRESH_RETRY_ATTEMPTS,
                    )
                attempt += 1
            if not coordinator.last_update_success:
                # async_refresh() is deliberately soft here (see this
                # function's own docstring for why) -- it never raises,
                # so without this, a device failing its very first read
                # would be completely silent right up until whatever
                # eventually looks at its own entities and finds them
                # unavailable, with nothing in the logs explaining why.
                _LOGGER.debug(
                    "%s did not come up immediately (starts unavailable, "
                    "retries on the normal poll cycle)", serial,
                )
        coordinators[serial] = coordinator

    entry.runtime_data = MobiusRuntimeData(coordinators=coordinators)
    _LOGGER.debug(
        "%r set up: %d/%d device(s) immediately available (gateway: %s)",
        entry.title,
        sum(1 for c in coordinators.values() if c.last_update_success),
        len(coordinators), working_serial,
    )

    # Periodic membership re-check -- see _async_revalidate_tank()'s own
    # docstring for the full reasoning. async_on_unload() means this
    # gets cleanly canceled on unload/reload without any separate
    # bookkeeping here -- Home Assistant calls the returned unsub
    # callback automatically.
    #
    # hass.create_task, NOT hass.async_create_task -- confirmed via a
    # real warning Home Assistant itself raises for this exact pattern,
    # pointing at Home Assistant's own thread-safety documentation:
    # async_create_task is only safe to call from the event loop thread
    # itself; create_task is the version safe to call from any thread,
    # which this lambda -- run by async_track_time_interval, not always
    # guaranteed to be on the event loop thread depending on Home
    # Assistant's own internal scheduling -- needs.
    entry.async_on_unload(
        async_track_time_interval(
            hass,
            lambda now: hass.create_task(_async_revalidate_tank(hass, entry, now)),
            TANK_REVALIDATION_INTERVAL,
        )
    )

    # Same async_on_unload()/hass.create_task() reasoning as immediately
    # above -- a separate, independent periodic task (see
    # _async_sync_tank_time()'s own docstring), not a job folded into
    # the revalidation cycle above, since it runs on its own, much
    # longer interval.
    entry.async_on_unload(
        async_track_time_interval(
            hass,
            lambda now: hass.create_task(_async_sync_tank_time(hass, entry, now)),
            TANK_TIME_SYNC_INTERVAL,
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


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """
    Called once, specifically when an entry is PERMANENTLY deleted --
    unlike async_unload_entry() above, which also fires on every ordinary
    reload (an entry being torn down and immediately set back up again,
    e.g. after a merge/migration), so isn't the right place for anything
    that should only happen on an actual, deliberate removal.

    Confirmed via Home Assistant's own documentation, matching this
    exact scenario precisely: "When a configuration entry or device is
    removed from Home Assistant, trigger rediscovery of its address to
    make sure they are available to be set up without restarting Home
    Assistant." Without this, re-adding the SAME physical device(s)
    later could be silently blocked: this integration's own discovery
    step already has to clear an address's match history the first time
    it sees an advertisement without manufacturer data yet (see
    config_flow.py's own async_step_bluetooth()) -- but that only runs
    while a discovery flow is actively happening. Once a device is
    already configured, nothing else ever revisits its match history at
    all, so it would simply stay marked "already matched" forever,
    invisible to a fresh discovery flow, even though the config entry
    that used to represent it is now gone.

    Best-effort per device, not guaranteed: a device that isn't
    currently advertising (powered off, out of range) can't have its
    CURRENT address resolved right now -- reusing the same "search
    Home Assistant's own live Bluetooth cache by serial" approach
    coordinator.py's own MobiusConnectionManager._resolve_current_ble_
    device() already uses for reconnection, rather than relying on a
    stored address that may be stale or (for a tank peer specifically)
    was never stored at all (see const.py's own CONF_ADDRESS docstring).
    If the device isn't visible right now, this simply does nothing for
    it -- there's no address to act on, and no error worth raising over
    that; the device being physically present again is required for
    rediscovery to matter at all anyway.
    """
    known_serials = {d[CONF_SERIAL] for d in entry.data.get(CONF_DEVICES, [])}
    if not known_serials:
        return

    addresses_by_serial: dict[str, str] = {}
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        parsed = parsed_advertisement(info.manufacturer_data)
        if parsed and parsed.serial in known_serials:
            addresses_by_serial[parsed.serial] = info.address

    for serial, address in addresses_by_serial.items():
        bluetooth.async_rediscover_address(hass, address)
        _LOGGER.debug(
            "Cleared Bluetooth rediscovery for %s (%s) after its entry was removed",
            serial, address,
        )
