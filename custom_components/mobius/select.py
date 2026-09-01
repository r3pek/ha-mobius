"""Select entities for Mobius devices' own Advanced Features.

Replaces sensor.py's own former AutoDimTimeoutSensor/MaxFanSpeedSensor --
both were read-only until python-mobius grew set_advanced_features().
See CHANGELOG.md for the entity-removal warning this replacement
carries: the old sensor.* entities won't be re-created after this
update, and existing automations/dashboards referencing them by
entity_id need to be repointed at the new select.* ones.

Restricted to the app's own exact preset choices for each -- confirmed
directly from the app itself, not invented here -- rather than a
free-form number input: python-mobius's own set_advanced_features()
deliberately doesn't enforce this (it's a low-level library; see that
method's own docstring), but this integration is a user-facing UI, and
letting someone submit e.g. "1000%" for MaxFanSpeed or an arbitrary
timeout the app itself never offers isn't something a select-with-
fixed-options can even produce -- so no separate validation step is
needed here at all, only for the option list itself to be correct.

Known gap: unlike sensor.py's own entities, these aren't yet covered by
__init__.py's own _async_ensure_sensors_exist() late-discovery healing
for a relayed device whose data snapshot was still empty at initial
setup -- see switch.py's own module docstring for the full explanation,
which applies identically here.
"""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from mobius import SceneID

from . import MobiusRuntimeData, tank_device_identifier, resolve_tank_device_id
from .const import CONF_SERIAL, CONF_DEVICES, CONF_MLPREFIX, CONF_PAN_ID
from .coordinator import MobiusDeviceCoordinator, derive_sw_version, derive_hw_version
from .sensor import _device_info

_LOGGER = logging.getLogger(__name__)

# Confirmed directly from the app itself -- both are fixed chooser
# dialogs there, never a free-form numeric input. Kept as the exact
# seconds/percent values (as strings, for SelectEntity's own option
# list) rather than inventing separate machine-readable keys, since the
# value itself is already unambiguous.
AUTO_DIM_TIMEOUT_OPTIONS = ["0", "30", "60", "300", "600", "1800", "3600"]
MAX_FAN_SPEED_OPTIONS = ["10", "20", "40", "60", "80", "100"]


class MobiusAdvancedFeatureSelect(CoordinatorEntity[MobiusDeviceCoordinator], SelectEntity):
    """
    Base for both Advanced Features selects below. In the
    CONFIGURATION category, not DIAGNOSTIC -- unlike the read-only
    sensors this replaces, this is now something a user actually sets.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MobiusDeviceCoordinator, serial: str, key: str,
                 translation_key: str, icon: str, options: list[str],
                 device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._key = key
        # Same unique_id the sensor this replaces used -- see
        # switch.py's own docstring for why that does NOT migrate the
        # old sensor.* entity itself.
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._attr_options = options
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        features = (self.coordinator.data or {}).get("advanced_features") or {}
        return super().available and features.get(self._key) is not None

    def _current_raw(self):
        features = (self.coordinator.data or {}).get("advanced_features") or {}
        return features.get(self._key)

    async def _async_set_raw(self, value) -> None:
        try:
            device = await self.coordinator.async_get_connected_device()
            result = await device.set_advanced_features(**{self._key: value})
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to set {self._key} on {self._serial}: {err}"
            ) from err
        error = result.get(self._key)
        if error is not None:
            raise HomeAssistantError(
                f"Device rejected setting {self._key} on {self._serial}: {error}"
            )
        await self.coordinator.async_request_refresh()


class AutoDimTimeoutSelect(MobiusAdvancedFeatureSelect):
    """VorTech-relevant (the app's own "Led Auto Dim" setting) -- seconds
    until the device's own status LED dims (0 = "Always On"/never dims)."""

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "auto_dim_timeout",
            "auto_dim_timeout", "mdi:led-off", AUTO_DIM_TIMEOUT_OPTIONS, device_info,
        )

    @property
    def current_option(self) -> str | None:
        value = self._current_raw()
        return str(value) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        await self._async_set_raw(int(option))


class MaxFanSpeedSelect(MobiusAdvancedFeatureSelect):
    """Radion-relevant (the app's own "Max Fan Speed" setting) --
    percent, already converted from the raw attribute's own permille/
    sentinel encoding by python-mobius itself (see
    AdvancedFeatures's own docstring there)."""

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "max_fan_speed",
            "max_fan_speed", "mdi:fan", MAX_FAN_SPEED_OPTIONS, device_info,
        )

    @property
    def current_option(self) -> str | None:
        value = self._current_raw()
        # Values are already whole percents at every one of the app's
        # own presets (10/20/40/60/80/100) -- int() here only guards
        # against an off-preset raw value (e.g. a device set outside
        # the app, or by python-mobius's own unrestricted write)
        # rendering as "57.0" instead of matching no option cleanly.
        return str(int(value)) if value is not None else None

    async def async_select_option(self, option: str) -> None:
        await self._async_set_raw(float(option))


def _build_advanced_feature_selects(coordinator, serial, device_info, data) -> list[MobiusAdvancedFeatureSelect]:
    """Same per-attribute gating as sensor.py's own (now-removed)
    _build_advanced_feature_entities() for these two fields --
    LocalControlEnabled/FanShutdownEnabled have their own equivalent in
    switch.py."""
    features = data.get("advanced_features") or {}
    entities: list[MobiusAdvancedFeatureSelect] = []
    if features.get("auto_dim_timeout") is not None:
        entities.append(AutoDimTimeoutSelect(coordinator, serial, device_info))
    if features.get("max_fan_speed") is not None:
        entities.append(MaxFanSpeedSelect(coordinator, serial, device_info))
    return entities


class SceneSelectionSelect(SelectEntity):
    """
    Activates a scene tank-wide from the tank's own synthetic device --
    one write, via start_scene()'s own broadcast=True default (see that
    method's own docstring in python-mobius for the confirmed mesh-
    isolation reasoning), sent to whichever ONE device actually has the
    selected scene configured.

    Options are the union of every genuinely named scene configured on
    ANY device coordinator in the tank, matching how the app itself
    builds its own tank-wide scene list (a scene can be configured on
    some devices and not others, e.g. if one ran out of slots -- see
    ConfiguredScenesSensor in sensor.py for that same per-device detail).
    A synthetic "None" option is always included first, meaning no
    scene is currently active; selecting it cancels whatever's running
    on every device, going back to each one's own normal schedule.

    Unlike every other tank-level entity so far (PollIntervalNumber,
    TimeSyncSwitch), this subscribes directly to every device
    coordinator's own updates, not just holding a fixed preference --
    its own options and current value both genuinely depend on data
    spanning every device in the tank.
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
            scenes = (coordinator.data or {}).get("configured_scenes") or []
            for scene in scenes:
                if scene.scene_type == SceneID.EmptyScene and not scene.name:
                    continue
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

    async def async_select_option(self, option: str) -> None:
        runtime: MobiusRuntimeData = self._entry.runtime_data

        if option == self.NONE_OPTION:
            # OperationState (what resume_schedule() writes) hasn't been
            # confirmed to propagate across the mesh from a single write
            # the way CurrentScene/Epoch have -- write to every device
            # individually rather than assume it does.
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
    """Same device_record loop shape as sensor.py's own async_setup_entry()."""
    runtime: MobiusRuntimeData = entry.runtime_data
    mlprefix_hex = entry.data.get(CONF_MLPREFIX)
    pan_id = entry.data.get(CONF_PAN_ID)
    device_records = entry.data.get(CONF_DEVICES, [])
    tank_identifier = tank_device_identifier(mlprefix_hex, pan_id)
    via_device_id = resolve_tank_device_id(hass, entry.entry_id, tank_identifier)

    entities: list[SelectEntity] = [SceneSelectionSelect(entry, tank_identifier)]
    for device_record in device_records:
        serial = device_record[CONF_SERIAL]
        coordinator = runtime.coordinators.get(serial)
        if coordinator is None:
            _LOGGER.warning(
                "No coordinator found for device %s in entry %s -- skipping its advanced-feature selects",
                serial, entry.entry_id,
            )
            continue

        address = device_record.get(CONF_ADDRESS)
        data = coordinator.data or {}
        sw_version = derive_sw_version(data.get("firmware_versions", {}))
        hw_version = derive_hw_version(data.get("hardware_info", {}))
        device_info = _device_info(
            serial, data, address=address, sw_version=sw_version, hw_version=hw_version,
            via_device_id=via_device_id,
        )

        entities += _build_advanced_feature_selects(coordinator, serial, device_info, data)

    async_add_entities(entities)
