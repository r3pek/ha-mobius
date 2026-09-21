"""Select entities: Advanced Features presets and the tank scene."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MobiusRuntimeData
from .coordinator import used_scenes
from .entity import (
    MobiusAdvancedFeatureEntity, build_advanced_feature_entities, entry_tank_identifier, iter_entry_devices,
)

# The app's preset choices (seconds / percent). set_advanced_features()
# accepts any value; these entities only offer the presets.
AUTO_DIM_TIMEOUT_OPTIONS = ["0", "30", "60", "300", "600", "1800", "3600"]
MAX_FAN_SPEED_OPTIONS = ["10", "20", "40", "60", "80", "100"]


class MobiusAdvancedFeatureSelect(MobiusAdvancedFeatureEntity, SelectEntity):
    """An Advanced Features field with preset options."""

    def __init__(self, coordinator, serial: str, key: str, icon: str, options: list[str],
                 device_info: DeviceInfo) -> None:
        super().__init__(coordinator, serial, key, icon, device_info)
        self._attr_options = options


class AutoDimTimeoutSelect(MobiusAdvancedFeatureSelect):
    """VorTech "Led Auto Dim": seconds until the status LED dims (0 =
    always on)."""

    def __init__(self, coordinator, serial, device_info):
        super().__init__(coordinator, serial, "auto_dim_timeout", "mdi:led-off", AUTO_DIM_TIMEOUT_OPTIONS, device_info)

    @property
    def current_option(self) -> str | None:
        value = self._current_raw()
        return str(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        await self._async_set_raw(int(option))


class MaxFanSpeedSelect(MobiusAdvancedFeatureSelect):
    """Radion "Max Fan Speed", in percent."""

    def __init__(self, coordinator, serial, device_info):
        super().__init__(coordinator, serial, "max_fan_speed", "mdi:fan", MAX_FAN_SPEED_OPTIONS, device_info)

    @property
    def current_option(self) -> str | None:
        value = self._current_raw()
        # Whole percent, so e.g. 57.0 (set outside the presets) shows as "57".
        return str(int(value)) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        await self._async_set_raw(float(option))


def _build_advanced_feature_selects(coordinator, serial, device_info, data) -> list[MobiusAdvancedFeatureSelect]:
    return build_advanced_feature_entities(coordinator, serial, device_info, data, {
        "auto_dim_timeout": AutoDimTimeoutSelect,
        "max_fan_speed": MaxFanSpeedSelect,
    })


class SceneSelectionSelect(SelectEntity):
    """
    Activates a scene on the whole tank, from the tank device: one
    start_scene(broadcast=True) write, sent to a device that has the scene
    configured.

    Options are the named scenes configured on any device of the tank (not
    every device necessarily has every scene), plus "None", which cancels
    the running scene on every device. Follows every coordinator of the
    tank.
    """

    NONE_OPTION = "None"

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, tank_identifier: tuple[str, str]) -> None:
        self.entity_description = SelectEntityDescription(
            key="scene_selection", translation_key="scene_selection", icon="mdi:palette",
        )
        self._attr_unique_id = f"{entry.entry_id}_scene_selection"
        self._attr_device_info = DeviceInfo(identifiers={tank_identifier})
        self._entry = entry
        self._unsub_callbacks: list = []

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        runtime: MobiusRuntimeData = self._entry.runtime_data
        for coordinator in runtime.coordinators.values():
            self._unsub_callbacks.append(coordinator.async_add_listener(self._handle_coordinator_update))

    async def async_will_remove_from_hass(self) -> None:
        for unsub in self._unsub_callbacks:
            unsub()
        self._unsub_callbacks.clear()
        await super().async_will_remove_from_hass()

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def _scene_name_to_id(self) -> dict[str, int]:
        runtime: MobiusRuntimeData = self._entry.runtime_data
        mapping: dict[str, int] = {}
        for coordinator in runtime.coordinators.values():
            for scene in used_scenes(coordinator.data or {}):
                mapping.setdefault(scene.name, scene.id)
        return mapping

    @property
    def options(self) -> list[str]:
        return [self.NONE_OPTION] + sorted(self._scene_name_to_id())

    @property
    def current_option(self) -> str | None:
        runtime: MobiusRuntimeData = self._entry.runtime_data
        id_to_name = {v: k for k, v in self._scene_name_to_id().items()}
        for coordinator in runtime.coordinators.values():
            active = (coordinator.data or {}).get("current_scene")
            if active is not None and active.id in id_to_name:
                return id_to_name[active.id]
        return self.NONE_OPTION

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """duration_remaining_seconds of the active scene; None when no scene
        is active."""
        runtime: MobiusRuntimeData = self._entry.runtime_data
        for coordinator in runtime.coordinators.values():
            active = (coordinator.data or {}).get("current_scene")
            if active is not None:
                return {"duration_remaining_seconds": active.duration_seconds}
        return None

    async def async_select_option(self, option: str) -> None:
        runtime: MobiusRuntimeData = self._entry.runtime_data

        if option == self.NONE_OPTION:
            # resume_schedule() writes OperationState without a group, so
            # every device is written.
            for coordinator in runtime.coordinators.values():
                try:
                    device = await coordinator.async_get_connected_device()
                    await device.resume_schedule()
                except Exception as err:
                    raise HomeAssistantError(
                        f"Failed to resume the normal schedule on {coordinator.serial}: {err}"
                    ) from err
                await coordinator.async_request_refresh()
            return

        scene_id = self._scene_name_to_id().get(option)
        if scene_id is None:
            raise HomeAssistantError(f"Unknown scene {option!r}")

        for coordinator in runtime.coordinators.values():
            scenes = (coordinator.data or {}).get("configured_scenes") or []
            if not any(s.id == scene_id for s in scenes):
                continue
            try:
                device = await coordinator.async_get_connected_device()
                await device.start_scene(scene_id, broadcast=True)
            except Exception as err:
                raise HomeAssistantError(f"Failed to activate scene {option!r}: {err}") from err
            for other in runtime.coordinators.values():
                await other.async_request_refresh()
            return

        raise HomeAssistantError(f"No connected device currently has scene {option!r} configured")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entities: list[SelectEntity] = [SceneSelectionSelect(entry, entry_tank_identifier(entry))]
    for serial, coordinator, device_info, data in iter_entry_devices(hass, entry):
        entities += _build_advanced_feature_selects(coordinator, serial, device_info, data)
    async_add_entities(entities)
