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

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MobiusRuntimeData, tank_device_identifier
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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Same device_record loop shape as sensor.py's own async_setup_entry()."""
    runtime: MobiusRuntimeData = entry.runtime_data
    mlprefix_hex = entry.data.get(CONF_MLPREFIX)
    pan_id = entry.data.get(CONF_PAN_ID)
    device_records = entry.data.get(CONF_DEVICES, [])
    via_device = tank_device_identifier(mlprefix_hex, pan_id)

    entities: list[MobiusAdvancedFeatureSelect] = []
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
            via_device=via_device,
        )

        entities += _build_advanced_feature_selects(coordinator, serial, device_info, data)

    async_add_entities(entities)
