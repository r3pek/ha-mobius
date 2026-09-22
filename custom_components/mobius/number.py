"""
Number entities: the poll interval of a tank (on the tank device, created
for every entry) and the battery settings of pumps that support them.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from typing import Any, Optional

from homeassistant.components.number import (
    NumberDeviceClass, NumberEntity, NumberEntityDescription, NumberMode, RestoreNumber,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MobiusRuntimeData
from .const import POLL_INTERVAL
from .coordinator import MobiusDeviceCoordinator
from .entity import async_run_on_device, entry_tank_identifier, iter_entry_devices

_LOGGER = logging.getLogger(__name__)

# The maximum equals MARK_UNAVAILABLE_AFTER: a longer interval would make a
# device unavailable after a single failed poll.
MIN_POLL_INTERVAL_SECONDS = 10
MAX_POLL_INTERVAL_SECONDS = 300


class PollIntervalNumber(RestoreNumber):
    """
    Poll interval (seconds) of every coordinator of the tank. A change takes
    effect at each coordinator's next scheduled refresh. Restored after a
    restart; defaults to POLL_INTERVAL.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False
    _attr_native_min_value = MIN_POLL_INTERVAL_SECONDS
    _attr_native_max_value = MAX_POLL_INTERVAL_SECONDS
    _attr_native_step = 5
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry, tank_identifier: tuple[str, str]) -> None:
        self.entity_description = NumberEntityDescription(
            key="poll_interval", translation_key="poll_interval", icon="mdi:timer-cog-outline",
        )
        self._attr_unique_id = f"{entry.entry_id}_poll_interval"
        self._attr_device_info = DeviceInfo(identifiers={tank_identifier})
        self._entry = entry
        self._attr_native_value = POLL_INTERVAL.total_seconds()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        if last_data is not None and last_data.native_value is not None:
            self._attr_native_value = last_data.native_value
            self._apply_to_coordinators(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self._apply_to_coordinators(value)
        self.async_write_ha_state()

    def _apply_to_coordinators(self, seconds: float) -> None:
        runtime: MobiusRuntimeData = self._entry.runtime_data
        new_interval = timedelta(seconds=seconds)
        for coordinator in runtime.coordinators.values():
            coordinator.update_interval = new_interval
        _LOGGER.debug(
            "%s: poll interval set to %.0fs for %d device(s) on this tank "
            "-- takes effect from each one's own next scheduled refresh",
            self._entry.title, seconds, len(runtime.coordinators),
        )


class _PumpBatteryNumber(CoordinatorEntity[MobiusDeviceCoordinator], NumberEntity):
    """A pump battery setting stored in coordinator.data[group][field]
    (see coordinator.py), written through `_async_write()`."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.BOX
    _group: str
    _field: str
    _scale = 1.0  # native value = raw value × _scale

    def __init__(self, coordinator: MobiusDeviceCoordinator, serial: str, key: str,
                 icon: str, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._key = key
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_device_info = device_info

    def _group_data(self) -> Optional[dict]:
        return (self.coordinator.data or {}).get(self._group)

    @property
    def available(self) -> bool:
        return super().available and self._group_data() is not None

    @property
    def native_value(self) -> Optional[float]:
        group = self._group_data()
        return group[self._field] * self._scale if group else None

    async def async_set_native_value(self, value: float) -> None:
        await async_run_on_device(
            self.coordinator, lambda device: self._async_write(device, value),
            f"Failed to set {self._key} on {self._serial}",
        )
        await self.coordinator.async_request_refresh()

    async def _async_write(self, device, value: float) -> None:
        raise NotImplementedError


class BatteryBackupSpeedNumber(_PumpBatteryNumber):
    """Pump speed while running on battery ("Battery Backup Speed"), in
    whole percent, up to the pump's "Battery Backup Max Speed" (an
    attribute)."""

    _group, _field, _scale = "battery_backup", "speed", 0.1
    _attr_native_min_value = 0
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, serial, device_info):
        super().__init__(coordinator, serial, "battery_backup_speed", "mdi:battery-arrow-down-outline", device_info)

    @property
    def native_value(self) -> Optional[float]:
        group = self._group_data()
        return round(group["speed"] / 10) if group else None

    @property
    def native_max_value(self) -> float:
        group = self._group_data()
        # Whole percent, truncated like the app (498 → 49 %).
        return group["max_speed"] // 10 if group else 100

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        group = self._group_data()
        return {"max_speed": group["max_speed"] // 10} if group else {}

    async def _async_write(self, device, value: float) -> None:
        group = self._group_data()
        await device.set_battery_backup_speed(value, max_speed=group["max_speed"] if group else None)


class BoostedBatteryPowerNumber(_PumpBatteryNumber):
    """Boosted battery power, 0-100 %. The pump only accepts changes while
    it runs on battery."""

    _group, _field, _scale = "boosted_battery", "power", 0.1
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, serial, device_info):
        super().__init__(coordinator, serial, "boosted_battery_power", "mdi:battery-plus-outline", device_info)

    async def _async_write(self, device, value: float) -> None:
        await device.set_boosted_battery(power=value)


class _BoostedBatteryTimeNumber(_PumpBatteryNumber):
    _group = "boosted_battery"
    _attr_native_min_value = 0
    _attr_native_max_value = 3600
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_device_class = NumberDeviceClass.DURATION


class BoostedBatteryOnTimeNumber(_BoostedBatteryTimeNumber):
    """Boosted battery on time, 0-3600 s (only changeable on battery)."""

    _field = "on_time"

    def __init__(self, coordinator, serial, device_info):
        super().__init__(coordinator, serial, "boosted_battery_on_time", "mdi:timer-play-outline", device_info)

    async def _async_write(self, device, value: float) -> None:
        await device.set_boosted_battery(on_time=int(value))


class BoostedBatteryOffTimeNumber(_BoostedBatteryTimeNumber):
    """Boosted battery off time, 0-3600 s (only changeable on battery)."""

    _field = "off_time"

    def __init__(self, coordinator, serial, device_info):
        super().__init__(coordinator, serial, "boosted_battery_off_time", "mdi:timer-pause-outline", device_info)

    async def _async_write(self, device, value: float) -> None:
        await device.set_boosted_battery(off_time=int(value))


def _build_pump_battery_numbers(coordinator, serial, device_info, data) -> list[NumberEntity]:
    """Battery entities for a pump that reports the settings. Like the
    switches and selects, they're created at setup only."""
    entities: list[NumberEntity] = []
    if data.get("battery_backup"):
        entities.append(BatteryBackupSpeedNumber(coordinator, serial, device_info))
    if data.get("boosted_battery"):
        entities += [
            BoostedBatteryPowerNumber(coordinator, serial, device_info),
            BoostedBatteryOnTimeNumber(coordinator, serial, device_info),
            BoostedBatteryOffTimeNumber(coordinator, serial, device_info),
        ]
    return entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entities: list[NumberEntity] = [PollIntervalNumber(entry, entry_tank_identifier(entry))]
    for serial, coordinator, device_info, data in iter_entry_devices(hass, entry):
        entities += _build_pump_battery_numbers(coordinator, serial, device_info, data)
    async_add_entities(entities)
