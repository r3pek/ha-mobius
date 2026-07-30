"""
Data update coordinators for a single Mobius device.

Two tiers, per documentation/ discussion of BLE connection cost:
  - MobiusStatusCoordinator: fast (~60s), cheap reads -- identity + live
    pump telemetry (speed/GPH/operation state). No schedule fetch.
  - MobiusScheduleCoordinator: slow (~10min), expensive reads -- the full
    programmed schedule, which doesn't change minute-to-minute anyway.

Both share a single connection-count-limiting semaphore across the whole
integration (not just per-device) -- even 4 devices showed real BLE
connection instability during development of the underlying python-mobius
library, so this deliberately throttles how many simultaneous connection
attempts the integration makes.

NOTE: written against Home Assistant's current documented Bluetooth APIs
(bluetooth.async_ble_device_from_address, DataUpdateCoordinator) as of
mid-2026 developer docs, but not yet exercised against a running Home
Assistant instance -- please report back anything that doesn't work as
described so this can be corrected.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from mobius import (
    MobiusDevice, PrimitiveType,
    LIGHT_PRIMITIVES, PUMP_PRIMITIVES_VERIFIED, PUMP_PRIMITIVES_EXPERIMENTAL,
    PRIMITIVE_SIZE,
)

from .const import CONNECT_TIMEOUT, FAST_POLL_INTERVAL, SLOW_POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)


class MobiusCoordinatorBase(DataUpdateCoordinator[dict[str, Any]]):
    """Shared connect/semaphore/error-handling plumbing for both tiers."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        address: str,
        semaphore: asyncio.Semaphore,
        update_interval,
        name_suffix: str,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"mobius_{address}_{name_suffix}",
            update_interval=update_interval,
        )
        self.address = address
        self.config_entry = entry
        self._semaphore = semaphore

    async def _async_update_data(self) -> dict[str, Any]:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            raise UpdateFailed(
                f"Device {self.address} is not currently visible to Home Assistant's Bluetooth stack"
            )

        async with self._semaphore:
            try:
                async with MobiusDevice(ble_device, connect_timeout=CONNECT_TIMEOUT) as device:
                    return await self._async_fetch(device)
            except Exception as err:  # noqa: BLE001 -- deliberately broad: any
                # failure here should degrade to "unavailable", not crash HA
                raise UpdateFailed(f"Error communicating with {self.address}: {err}") from err

    async def _async_fetch(self, device: MobiusDevice) -> dict[str, Any]:
        raise NotImplementedError


class MobiusStatusCoordinator(MobiusCoordinatorBase):
    """Fast tier: identity + live telemetry. No schedule fetch."""

    def __init__(self, hass, entry, address, semaphore):
        super().__init__(hass, entry, address, semaphore, FAST_POLL_INTERVAL, "status")

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

    def __init__(self, hass, entry, address, semaphore):
        super().__init__(hass, entry, address, semaphore, SLOW_POLL_INTERVAL, "schedule")

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
