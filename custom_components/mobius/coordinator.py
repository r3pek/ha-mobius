"""
Data update coordinators for a single Mobius device, sharing ONE
persistent BLE connection (see MobiusConnectionManager) rather than each
tier connecting/disconnecting independently on every poll.

Two tiers, per the design discussion in documentation/: telemetry is
cheap (a couple of small GATT reads), schedule fetches are expensive
(multiple round trips for potentially many points), and schedules don't
change minute-to-minute anyway.
  - MobiusStatusCoordinator: fast (~10s), cheap reads -- identity + live
    pump telemetry (speed/GPH/operation state). No schedule fetch.
  - MobiusScheduleCoordinator: slow (~10min), expensive reads -- the full
    programmed schedule.

Reconnection (the first connect, or after a detected drop) always
resolves the device's CURRENT address by serial number -- these devices'
BLE addresses are not guaranteed stable over time, confirmed via real
hardware and via the official app's own Peripheral class (identity is
serial-number-based, never address-based). See python-mobius's
documentation/12-device-identity-and-address-stability.md.

Deliberately does NOT use mobius.find_device_by_serial() for this --
that function runs its own independent BleakScanner, which conflicts
with Home Assistant's own shared Bluetooth manager (the exact
connection-instability-inducing anti-pattern this integration has avoided
from the start). Instead, MobiusConnectionManager reads Home Assistant's
own already-running Bluetooth cache (bluetooth.async_discovered_service_info()),
the same approach config_flow.py's manual-setup step already uses.

Failure detection is REACTIVE, not proactive: a dropped connection is
only discovered when a scheduled read actually fails, not via a bleak
disconnect callback. Given the fast tier already polls every ~10s, this
is at most ~10s of staleness in exchange for meaningfully simpler code.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from mobius import (
    MobiusDevice, PrimitiveType, MOBIUS_COMPANY_ID, parse_manufacturer_data,
    LIGHT_PRIMITIVES, PUMP_PRIMITIVES_VERIFIED, PUMP_PRIMITIVES_EXPERIMENTAL,
    PRIMITIVE_SIZE,
)

from .const import CONNECT_TIMEOUT, FAST_POLL_INTERVAL, SLOW_POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)


class MobiusConnectionManager:
    """
    Owns a single persistent MobiusDevice connection for one physical
    device, shared by both coordinator tiers for that device -- the
    actual point of this class existing is that there's exactly one BLE
    connection per device, not one per poll per tier.
    """

    def __init__(self, hass: HomeAssistant, serial: str, semaphore: asyncio.Semaphore):
        self.hass = hass
        self.serial = serial
        self._semaphore = semaphore
        self._device: Optional[MobiusDevice] = None
        # Prevents two coordinators (status + schedule) from both trying
        # to reconnect the same device at the same time.
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
            # Re-check after acquiring the lock -- the other coordinator
            # may have already reconnected while we were waiting on it.
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


class MobiusCoordinatorBase(DataUpdateCoordinator[dict[str, Any]]):
    """Shared read/error-handling plumbing for both tiers, on top of a
    shared MobiusConnectionManager."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        connection_manager: MobiusConnectionManager,
        update_interval,
        name_suffix: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"mobius_{connection_manager.serial}_{name_suffix}",
            update_interval=update_interval,
        )
        self.config_entry = entry
        self._connection_manager = connection_manager

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            device = await self._connection_manager.ensure_connected()
            return await self._async_fetch(device)
        except UpdateFailed:
            raise
        except Exception as err:
            # A read failed even though we thought we were connected -- the
            # connection may have dropped since ensure_connected() last
            # checked. Reactive detection: try ONE reconnect + retry within
            # this same poll cycle before giving up for this cycle.
            _LOGGER.debug(
                "Read failed for %s (%s), attempting one reconnect",
                self._connection_manager.serial, err,
            )
            self._connection_manager.mark_disconnected()
            try:
                device = await self._connection_manager.ensure_connected()
                return await self._async_fetch(device)
            except Exception as err2:
                raise UpdateFailed(
                    f"Error communicating with {self._connection_manager.serial}: {err2}"
                ) from err2

    async def _async_fetch(self, device: MobiusDevice) -> dict[str, Any]:
        raise NotImplementedError


class MobiusStatusCoordinator(MobiusCoordinatorBase):
    """Fast tier: identity + live telemetry. No schedule fetch."""

    def __init__(self, hass, entry, connection_manager):
        super().__init__(hass, entry, connection_manager, FAST_POLL_INTERVAL, "status")

    async def _async_fetch(self, device: MobiusDevice) -> dict[str, Any]:
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
        return info


class MobiusScheduleCoordinator(MobiusCoordinatorBase):
    """Slow tier: schedule fetch + interpolation/block-lookup."""

    def __init__(self, hass, entry, connection_manager):
        super().__init__(hass, entry, connection_manager, SLOW_POLL_INTERVAL, "schedule")

    async def _async_fetch(self, device: MobiusDevice) -> dict[str, Any]:
        primitive = await device.identify_device_type()
        data: dict[str, Any] = {"primitive_type": primitive.name}

        # Use Home Assistant's configured timezone, not the container's
        # system time -- these can differ, and the interpolation/block
        # lookup is meaningless if "now" is wrong.
        now = dt_util.now()
        minute_of_day = now.hour * 60 + now.minute

        if primitive in LIGHT_PRIMITIVES:
            data["channels"] = [c.name for c in await device.get_supported_channels()]
            points = await device.get_light_schedule(which=1)
            data["schedule_point_count"] = len(points)
            current = await device.get_current_light_intensities(which=1, minute_of_day=minute_of_day)
            data["current_intensities"] = {ch.name: v for ch, v in current.items()}
            # Confirmed via real device testing AND via the app's own UI
            # gating (DeviceSettingsFragment.java) to be a light feature --
            # returns None for pumps, which is fine (the sensor built on
            # this is only added for light devices anyway). Belongs here
            # (slow tier) rather than the fast tier since calibration
            # status essentially never changes during normal operation.
            data["calibration"] = await device.get_calibration_info()

        elif primitive in PUMP_PRIMITIVES_VERIFIED or primitive in PUMP_PRIMITIVES_EXPERIMENTAL:
            points = await device.get_pump_schedule(which=1)
            data["schedule_point_count"] = len(points)
            block = await device.get_current_pump_block(which=1, minute_of_day=minute_of_day)
            if block:
                data["current_pump_mode"] = block.pump.mode.name
                data["current_pump_params"] = {
                    p.name: (v.hex() if isinstance(v, bytes) else (v.name if hasattr(v, "name") else v))
                    for p, v in block.pump.params.items()
                }

        return data
