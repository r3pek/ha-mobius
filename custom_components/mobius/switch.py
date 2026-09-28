"""Switch entities: Advanced Features toggles, lunar phases and the tank
time sync."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MobiusRuntimeData
from .coordinator import MobiusDeviceCoordinator
from .entity import (
    MobiusAdvancedFeatureEntity, async_run_on_device, build_advanced_feature_entities,
    entry_tank_identifier, iter_entry_devices,
)


class MobiusAdvancedFeatureSwitch(MobiusAdvancedFeatureEntity, SwitchEntity):
    """A boolean Advanced Features field."""

    @property
    def is_on(self) -> bool | None:
        return self._current_raw()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set_raw(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set_raw(False)


class LocalControlEnabledSwitch(MobiusAdvancedFeatureSwitch):
    """VorTech "Local Control"."""

    def __init__(self, coordinator, serial, device_info):
        super().__init__(coordinator, serial, "local_control_enabled", "mdi:gesture-tap", device_info)


class FanShutdownEnabledSwitch(MobiusAdvancedFeatureSwitch):
    """Radion "Fan Shutdown"."""

    def __init__(self, coordinator, serial, device_info):
        super().__init__(coordinator, serial, "fan_shutdown_enabled", "mdi:fan-off", device_info)


def _build_advanced_feature_switches(coordinator, serial, device_info, data) -> list[MobiusAdvancedFeatureSwitch]:
    return build_advanced_feature_entities(coordinator, serial, device_info, data, {
        "local_control_enabled": LocalControlEnabledSwitch,
        "fan_shutdown_enabled": FanShutdownEnabledSwitch,
    })


class TimeSyncSwitch(SwitchEntity, RestoreEntity):
    """
    Enables the tank's automatic clock and time zone sync (__init__.py's
    _async_check_tank_time(), which reads MobiusRuntimeData.time_sync_enabled).
    On the tank device. A local setting, restored after a restart; on by
    default.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, tank_identifier: tuple[str, str]) -> None:
        self.entity_description = SwitchEntityDescription(
            key="time_sync_enabled", translation_key="time_sync_enabled", icon="mdi:clock-check-outline",
        )
        self._attr_unique_id = f"{entry.entry_id}_time_sync_enabled"
        self._attr_device_info = DeviceInfo(identifiers={tank_identifier})
        self._entry = entry
        self._attr_is_on = True

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._attr_is_on = last_state.state == "on"
        runtime: MobiusRuntimeData = self._entry.runtime_data
        runtime.time_sync_enabled = self._attr_is_on

    async def _async_set(self, value: bool) -> None:
        self._attr_is_on = value
        self._entry.runtime_data.time_sync_enabled = value
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


class LunarPhasesEnabledSwitch(CoordinatorEntity[MobiusDeviceCoordinator], SwitchEntity):
    """
    LunarPhasesEnabled (907) of one light: the app's "Lunar" chip in the
    light schedule editor. The current moon phase and day are attributes.

    The app's chip writes the value to every light of the tank; here each
    light has its own switch.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "lunar_phases_enabled"
    _attr_icon = "mdi:moon-waning-crescent"

    def __init__(self, coordinator: MobiusDeviceCoordinator, serial: str, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{serial}_lunar_phases_enabled"
        self._attr_device_info = device_info

    @property
    def is_on(self) -> bool | None:
        return (self.coordinator.data or {}).get("lunar_enabled")

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {"phase": data.get("moon_phase_name"), "phase_day": data.get("lunar_phase_day")}

    async def _async_set(self, value: bool) -> None:
        await async_run_on_device(
            self.coordinator, lambda device: device.set_lunar_enabled(value),
            f"Failed to set lunar_phases_enabled on {self._serial}",
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entities: list[SwitchEntity] = [TimeSyncSwitch(entry, entry_tank_identifier(entry))]
    for serial, coordinator, device_info, data in iter_entry_devices(hass, entry):
        entities += _build_advanced_feature_switches(coordinator, serial, device_info, data)
        # Lights that support LunarPhasesEnabled.
        if data.get("support") == "light" and data.get("lunar_supported"):
            entities.append(LunarPhasesEnabledSwitch(coordinator, serial, device_info))
    async_add_entities(entities)
