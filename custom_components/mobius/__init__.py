"""The Mobius integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval

from mobius import MOBIUS_COMPANY_ID, parse_manufacturer_data

from .const import (
    DOMAIN, MAX_CONCURRENT_CONNECTIONS, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX,
    TANK_REVALIDATION_INTERVAL,
)
from .coordinator import MobiusDeviceCoordinator, discover_mesh_address
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

    coordinators: dict[str, MobiusDeviceCoordinator] = {}
    for index, device in enumerate(devices):
        serial = device[CONF_SERIAL]
        rssi = _current_rssi(hass, serial)
        # The FIRST device in CONF_DEVICES is always the one the config
        # flow actually connected to, to run discover_tank() in the
        # first place (python-mobius's own discover_tank() always
        # returns the connected device's own info first in its peers
        # list -- see its docstring -- and _async_create_tank_entry()
        # in config_flow.py builds CONF_DEVICES straight from that same
        # order) -- so it's the one with direct, fresh proof of
        # reachability, worth preferring as gateway over an untested
        # peer purely by RSSI (see GatewayRegistry.join()'s own
        # docstring for prefer_as_gateway's full reasoning). Only
        # matters for a genuinely brand-new group; harmless no-op
        # (ignored) for an already-established one, or for an ad-hoc
        # single-device entry (trivially becomes gateway either way).
        group = await registry.join(pan_id, serial, rssi, prefer_as_gateway=(index == 0))

        # Proactively discover and cache this device's own mesh address
        # BEFORE its coordinator's first refresh -- runs every time this
        # entry is set up, which covers both a brand-new device AND
        # every existing device on every Home Assistant restart, not
        # just first-ever setup. Avoids the first poll cycle having to
        # pay for both address discovery and the actual relay read
        # together. A failure here isn't fatal: it's just treated as
        # "will retry via the coordinator's own on-demand fallback," not
        # raised.
        #
        # Deliberately NOT gated on "only if relayed, not this group's
        # gateway" the way an earlier version of this was -- the new
        # MeshAddressSensor (see sensor.py) needs this cached for EVERY
        # device, including the gateway, which otherwise never gets its
        # own address populated into the registry at all (nothing else
        # ever calls update_mesh_address() for a group's own gateway,
        # since relay itself has no need to know the gateway's address).
        # A real, accepted trade-off: this costs the gateway device an
        # extra brief connect/disconnect here, separate from the direct
        # connection its own coordinator will make moments later for its
        # first real poll -- happens once at setup/restart, not on every
        # poll cycle, so the redundant connection is bounded, not
        # ongoing.
        if group.members[serial].mesh_address is None:
            address = await discover_mesh_address(hass, serial, semaphore)
            if address is not None:
                registry.update_mesh_address(pan_id, serial, address)
            else:
                _LOGGER.debug(
                    "Could not proactively discover mesh address for %s at setup -- "
                    "will retry on the next poll cycle", serial,
                )

        coordinator = MobiusDeviceCoordinator(hass, entry, registry, serial, pan_id)
        await coordinator.async_config_entry_first_refresh()
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
