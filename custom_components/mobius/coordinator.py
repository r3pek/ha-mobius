"""
Single unified data update coordinator per Mobius device, sharing BLE
connections across devices on the same pan_id (Thread mesh/"tank") via
gateway_registry.GatewayRegistry rather than each device holding its own
direct connection.

One coordinator per device, one ~30s poll cycle, fetching both status
and schedule data together -- see const.py's POLL_INTERVAL for why this
replaced the previous fast/slow two-tier split.

## Gateway vs. relayed reads

Each poll cycle, the coordinator checks whether ITS OWN serial is
currently the gateway for its pan_id group (gateway_registry.PanGroup.
gateway_serial). If so, it reads directly over that group's shared
MobiusConnectionManager. If not, it reads through a RelayedMobiusDevice
wrapping that same connection, addressed to its own cached Thread
mesh-local IPv6 (see _resolve_own_mesh_peer() for the on-demand discovery
fallback if that isn't cached yet).

This check happens fresh on every poll cycle, not once at setup -- if
this device's group promotes a different gateway (see
gateway_registry.py's failover logic) between one cycle and the next,
the very next read from this coordinator automatically switches from
direct to relayed (or vice versa, if THIS device gets promoted TO
gateway), with no separate code path needed to handle the transition.

## Failure handling: graceful, not immediate

A single failed read doesn't immediately mark a device unavailable --
the coordinator keeps returning its last-known-good data for up to
MARK_UNAVAILABLE_AFTER (const.py) of consecutive failures before actually
raising UpdateFailed. Only a genuinely sustained outage results in
entities going unavailable. Reconnection itself isn't retried within the
same poll cycle (unlike an earlier version of this module) -- a failed
read marks the connection disconnected so the NEXT ~30s cycle reconnects
fresh, and the grace period covers the gap in between; this is simpler
than an immediate in-cycle retry and, given the poll interval is already
short, doesn't meaningfully change how quickly a transient drop recovers.

Separately, when THIS device is the group's gateway, a failed read is
also reported to the registry (record_gateway_failure()) -- after
GATEWAY_FAILURE_THRESHOLD consecutive gateway-read failures (much sooner
than the 5-minute mark-unavailable grace period), the registry promotes
a different member to gateway, since a bad gateway takes its whole group
down with it. Relayed devices' own read failures are NOT reported to the
registry this way -- a single relayed device failing to read through an
otherwise-healthy gateway is much more likely to be specific to that
device/target than to the gateway itself, so only the gateway's own
direct connection health drives promotion.

Reconnection (the gateway's first connect, or after a detected drop)
always resolves the device's CURRENT address by serial number -- BLE
addresses are not guaranteed stable over time, confirmed via real
hardware and via the official app's own Peripheral class (identity is
serial-number-based, never address-based). See python-mobius's
documentation/12-device-identity-and-address-stability.md.

Deliberately does NOT use mobius.find_device_by_serial() for this --
that function runs its own independent BleakScanner, which conflicts
with Home Assistant's own shared Bluetooth manager. Instead,
MobiusConnectionManager reads Home Assistant's own already-running
Bluetooth cache (bluetooth.async_discovered_service_info()), the same
approach config_flow.py's manual-setup step already uses.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from mobius import (
    MobiusDevice, RelayedMobiusDevice, MeshPeer, PrimitiveType, Model,
    MOBIUS_COMPANY_ID, parse_manufacturer_data,
    LIGHT_PRIMITIVES, PUMP_PRIMITIVES_VERIFIED, PUMP_PRIMITIVES_EXPERIMENTAL,
    PRIMITIVE_SIZE, extract_short_address,
)

from .const import CONNECT_TIMEOUT, POLL_INTERVAL, MARK_UNAVAILABLE_AFTER, DOMAIN
from .gateway_registry import GatewayRegistry, PanGroup

_LOGGER = logging.getLogger(__name__)


class MobiusConnectionManager:
    """
    Owns a single persistent MobiusDevice connection for one physical
    device -- the gateway of a pan_id group. Shared (via
    gateway_registry.PanGroup.gateway_connection) by every coordinator
    for devices in that group, not just the gateway's own -- the actual
    point of this class existing is that there's exactly one real BLE
    connection per GROUP, not one per device.
    """

    def __init__(self, hass: HomeAssistant, serial: str, semaphore: asyncio.Semaphore):
        self.hass = hass
        self.serial = serial
        self._semaphore = semaphore
        self._device: Optional[MobiusDevice] = None
        # Prevents multiple coordinators relaying through this same
        # gateway from all trying to reconnect it at the same time.
        self._lock = asyncio.Lock()

    async def _resolve_current_ble_device(self):
        """
        Finds the BLEDevice currently advertising self.serial, by reading
        Home Assistant's own Bluetooth cache -- NOT by scanning
        independently. See this module's docstring for why.
        """
        for info in bluetooth.async_discovered_service_info(self.hass, connectable=True):
            payload = info.manufacturer_data.get(MOBIUS_COMPANY_ID)
            if not payload:
                continue
            parsed = parse_manufacturer_data(payload)
            if parsed and parsed.serial == self.serial:
                return bluetooth.async_ble_device_from_address(
                    self.hass, info.address, connectable=True
                )
        return None

    def mark_disconnected(self) -> None:
        """
        Forces the next ensure_connected() to reconnect from scratch, even
        if the underlying client's own is_connected might still say True
        momentarily -- used when a read fails unexpectedly, since that's a
        reliable sign something's wrong even if the client object hasn't
        fully updated its own state yet.
        """
        self._device = None

    async def ensure_connected(self) -> MobiusDevice:
        """Returns an already-connected MobiusDevice, reconnecting first
        (via serial, resolved from Home Assistant's own Bluetooth cache)
        if necessary."""
        if self._device is not None and self._device.is_connected:
            return self._device

        async with self._lock:
            # Re-check after acquiring the lock -- another coordinator
            # relaying through this gateway may have already reconnected
            # it while we were waiting.
            if self._device is not None and self._device.is_connected:
                return self._device

            ble_device = await self._resolve_current_ble_device()
            if ble_device is None:
                raise UpdateFailed(
                    f"No device currently advertising serial {self.serial!r} was "
                    "found in Home Assistant's Bluetooth cache"
                )

            async with self._semaphore:
                new_device = MobiusDevice(
                    ble_device, serial=self.serial, connect_timeout=CONNECT_TIMEOUT
                )
                try:
                    await new_device.connect()
                except Exception as err:
                    raise UpdateFailed(
                        f"Error connecting to {self.serial}: {err}"
                    ) from err

            self._device = new_device
            return self._device

    async def disconnect(self) -> None:
        if self._device is not None:
            try:
                await self._device.disconnect()
            except Exception:
                pass  # best-effort; we're tearing down anyway
            self._device = None


async def _fetch_all(device, minute_of_day_now=None) -> dict[str, Any]:
    """
    The actual read logic, merging what used to be two separate tiers
    (status: identity + live telemetry; schedule: programmed schedule +
    firmware version) into one. `device` can be a directly-connected
    MobiusDevice or a RelayedMobiusDevice -- identical either way, since
    RelayedMobiusDevice implements the same interface transparently.
    """
    info = await device.get_device_info()
    primitive_name = info.get("primitive_type")
    try:
        primitive = PrimitiveType[primitive_name] if primitive_name else None
    except KeyError:
        primitive = None

    if primitive in PUMP_PRIMITIVES_VERIFIED or primitive in PUMP_PRIMITIVES_EXPERIMENTAL:
        info["support"] = "pump" if primitive in PUMP_PRIMITIVES_VERIFIED else "pump (experimental)"
        info["telemetry"] = await device.get_pump_telemetry()
        info["operation_state"] = (await device.get_operation_state()).name
    elif primitive in LIGHT_PRIMITIVES:
        info["support"] = "light"
    else:
        info["support"] = "unsupported"
        size = PRIMITIVE_SIZE.get(primitive) if primitive else None
        info["support_note"] = (
            f"PrimitiveType {primitive_name!r} has no parser implemented "
            f"({size} byte primitive)." if size is not None else
            f"PrimitiveType {primitive_name!r} has no parser implemented."
        )

    # Use Home Assistant's configured timezone, not the container's system
    # time -- these can differ, and the interpolation/block lookup is
    # meaningless if "now" is wrong.
    now = dt_util.now()
    minute_of_day = now.hour * 60 + now.minute

    model_raw = info.get("model_raw")
    try:
        model = Model(model_raw) if model_raw is not None else None
    except ValueError:
        model = None
    info["firmware_versions"] = await device.get_firmware_versions(model=model)

    if primitive in LIGHT_PRIMITIVES:
        info["channels"] = [c.name for c in await device.get_supported_channels()]
        points = await device.get_light_schedule(which=1)
        info["schedule_point_count"] = len(points)
        current = await device.get_current_light_intensities(which=1, minute_of_day=minute_of_day)
        info["current_intensities"] = {ch.name: v for ch, v in current.items()}
        # Confirmed light-only via real device testing AND the app's own
        # UI gating -- returns None for pumps, which is fine (the sensor
        # built on this is only added for light devices anyway).
        info["calibration"] = await device.get_calibration_info()

    elif primitive in PUMP_PRIMITIVES_VERIFIED or primitive in PUMP_PRIMITIVES_EXPERIMENTAL:
        points = await device.get_pump_schedule(which=1)
        info["schedule_point_count"] = len(points)
        block = await device.get_current_pump_block(which=1, minute_of_day=minute_of_day)
        if block:
            info["current_pump_mode"] = block.pump.mode.name
            info["current_pump_params"] = {
                p.name: (v.hex() if isinstance(v, bytes) else (v.name if hasattr(v, "name") else v))
                for p, v in block.pump.params.items()
            }

    return info


class MobiusDeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """One coordinator per device. See this module's docstring for the
    gateway-vs-relayed and graceful-failure design."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, registry: GatewayRegistry,
        serial: str, pan_id: int,
    ):
        super().__init__(hass, _LOGGER, name=f"mobius_{serial}", update_interval=POLL_INTERVAL)
        self.config_entry = entry
        self.registry = registry
        self.serial = serial
        self.pan_id = pan_id
        self._last_success: Optional[Any] = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self._fetch()
            self._last_success = dt_util.utcnow()
            self._sync_sw_version(data)
            return data
        except Exception as err:
            now = dt_util.utcnow()
            if self._last_success is not None and (now - self._last_success) < MARK_UNAVAILABLE_AFTER:
                _LOGGER.debug(
                    "Read failed for %s (%s), within the %s grace period -- "
                    "keeping last-known-good data instead of going unavailable",
                    self.serial, err, MARK_UNAVAILABLE_AFTER,
                )
                return self.data
            raise UpdateFailed(f"Error communicating with {self.serial}: {err}") from err

    def _sync_sw_version(self, data: dict[str, Any]) -> None:
        """Keeps the device registry's sw_version in sync with reality --
        firmware changes are infrequent but real (a real device got an OTA
        update mid-development of this integration), so this needs to
        actually propagate, not just be captured once at setup and left
        stale forever after."""
        sw_version = (data.get("firmware_versions") or {}).get("Product OS")
        if not sw_version:
            return
        device_registry = dr.async_get(self.hass)
        device_entry = device_registry.async_get_device(
            identifiers={(DOMAIN, self.config_entry.data[CONF_ADDRESS])}
        )
        if device_entry is not None and device_entry.sw_version != sw_version:
            device_registry.async_update_device(device_entry.id, sw_version=sw_version)

    async def _fetch(self) -> dict[str, Any]:
        group = self.registry.group(self.pan_id)
        if group is None or group.gateway_serial is None:
            raise UpdateFailed(
                f"No gateway currently available for pan_id {self.pan_id:#06x}"
            )

        is_gateway = group.gateway_serial == self.serial
        try:
            if is_gateway:
                device = await group.gateway_connection.ensure_connected()
                data = await _fetch_all(device)
                self.registry.record_gateway_success(self.pan_id)
                return data
            else:
                gateway_device = await group.gateway_connection.ensure_connected()
                peer = await self._resolve_own_mesh_peer(group)
                relayed = RelayedMobiusDevice(gateway_device, peer)
                return await _fetch_all(relayed)
        except Exception:
            # A READ can fail even after ensure_connected() reported
            # success (the connection can drop in between) -- this needs
            # to mark the connection disconnected in that case too, not
            # just when ensure_connected() itself raises, or the next
            # poll cycle would keep reusing the same dead connection
            # forever instead of ever actually reconnecting.
            #
            # Only done when THIS device is the gateway -- see this
            # module's docstring for why a relayed device's own failure
            # doesn't touch the shared gateway connection's state at all
            # (it might be specific to this device/target, not the
            # gateway connection itself; the gateway's own coordinator
            # independently detects and handles its own connection health
            # on its own cycle regardless).
            if is_gateway:
                group.gateway_connection.mark_disconnected()
                await self.registry.record_gateway_failure(self.pan_id)
            raise

    async def _resolve_own_mesh_peer(self, group: PanGroup) -> MeshPeer:
        """Returns a MeshPeer for THIS coordinator's own device, using a
        cached mesh address if available (usually already populated by
        __init__.py's proactive discovery-at-setup step, which runs
        before the first refresh for any relayed device), or discovering
        it on demand via a brief direct connection if not."""
        member = group.members.get(self.serial)
        address = member.mesh_address if member else None

        if address is None:
            address = await self._discover_own_mesh_address()
            if address is None:
                raise UpdateFailed(
                    f"Could not determine Thread mesh address for {self.serial} "
                    "(needed to relay through the group's gateway)"
                )
            self.registry.update_mesh_address(self.pan_id, self.serial, address)

        return MeshPeer(
            serial=self.serial, model_raw=0, model=None,
            short_address=extract_short_address(address), address=address,
        )

    async def _discover_own_mesh_address(self) -> Optional[bytes]:
        """On-demand fallback for _resolve_own_mesh_peer() -- see
        discover_mesh_address() below for the actual logic, shared with
        __init__.py's proactive discovery-at-setup path."""
        return await discover_mesh_address(self.hass, self.serial)


async def discover_mesh_address(hass: HomeAssistant, serial: str) -> Optional[bytes]:
    """
    Connects directly and briefly to whichever device is currently
    advertising `serial` (resolved via Home Assistant's own Bluetooth
    cache, matching MobiusConnectionManager's own resolution) to read its
    own Thread mesh-local address. Returns None (not an exception) if the
    device can't currently be found/reached -- callers that need to
    surface this as a real failure (e.g. MobiusDeviceCoordinator's
    on-demand fallback, when relay genuinely can't proceed without an
    address) do so themselves; __init__.py's proactive call at setup time
    treats a None here as "will retry later" rather than fatal, since the
    coordinator's own on-demand fallback covers it if this attempt
    doesn't pan out.
    """
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        payload = info.manufacturer_data.get(MOBIUS_COMPANY_ID)
        if not payload:
            continue
        parsed = parse_manufacturer_data(payload)
        if not parsed or parsed.serial != serial:
            continue
        ble_device = bluetooth.async_ble_device_from_address(hass, info.address, connectable=True)
        if ble_device is None:
            return None
        try:
            async with MobiusDevice(ble_device, connect_timeout=CONNECT_TIMEOUT) as mdevice:
                return await mdevice.get_own_mesh_address()
        except Exception as err:
            _LOGGER.debug("Mesh address discovery failed for %s: %s", serial, err)
            return None
    return None
