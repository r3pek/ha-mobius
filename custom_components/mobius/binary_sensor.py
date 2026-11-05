"""Binary sensors: whether a pump is running on its battery."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import MobiusDeviceCoordinator
from .entity import async_add_device_entities


class RunningOnBatteryBinarySensor(CoordinatorEntity[MobiusDeviceCoordinator], BinarySensorEntity):
    """
    On while the pump runs on its battery (its operation mode is
    "BatteryBackup"). Created for pumps with battery backup settings. The
    pumps don't report whether a battery is connected, only whether they
    are running on it.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "running_on_battery"

    def __init__(self, coordinator: MobiusDeviceCoordinator, serial: str, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{serial}_running_on_battery"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool | None:
        mode = (self.coordinator.data or {}).get("operation_mode")
        return None if mode is None else mode == "BatteryBackup"

    @property
    def icon(self) -> str:
        return "mdi:battery-charging-outline" if self.is_on else "mdi:power-plug-outline"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_device_entities(hass, entry, async_add_entities, _build_binary_sensors)


def _build_binary_sensors(coordinator, serial, device_info, data) -> list[BinarySensorEntity]:
    return [RunningOnBatteryBinarySensor(coordinator, serial, device_info)] if data.get("battery_backup") else []
