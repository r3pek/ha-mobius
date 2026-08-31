"""Switch entities for Mobius devices' own Advanced Features.

Replaces sensor.py's own former LocalControlEnabledSensor/
FanShutdownEnabledSensor -- both were read-only until python-mobius grew
set_advanced_features(). See CHANGELOG.md for the entity-removal warning
this replacement carries: the old sensor.* entities won't be re-created
after this update, and existing automations/dashboards referencing them
by entity_id need to be repointed at the new switch.* ones.

Known gap: unlike sensor.py's own entities, these aren't yet covered by
__init__.py's own _async_ensure_sensors_exist() late-discovery healing
for a relayed device whose data snapshot was still empty at initial
setup -- a device in that specific situation won't get these switches
until a full Home Assistant restart. Sensor.py's own docstring there
explains the underlying reason (a relayed device's own first read is
soft/non-blocking, so its data can genuinely be empty at the exact
moment platforms are set up); this platform doesn't yet plug into that
same healing mechanism.
"""

from __future__ import annotations

import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MobiusRuntimeData, tank_device_identifier, resolve_tank_device_id
from .const import CONF_SERIAL, CONF_DEVICES, CONF_MLPREFIX, CONF_PAN_ID
from .coordinator import MobiusDeviceCoordinator, derive_sw_version, derive_hw_version
from .sensor import _device_info

_LOGGER = logging.getLogger(__name__)


class MobiusAdvancedFeatureSwitch(CoordinatorEntity[MobiusDeviceCoordinator], SwitchEntity):
    """
    Base for both Advanced Features switches below -- LocalControlEnabled
    (VorTech's own "Local Control") and FanShutdownEnabled (Radion's own
    "Fan Shutdown"), gated per-attribute exactly like the sensors these
    replace (see coordinator.py's own comment on why
    get_advanced_features() is called unconditionally for every device,
    regardless of "support"; only added to a config entry if this
    specific attribute was actually present at setup time -- see
    async_setup_entry() below).

    In the CONFIGURATION category, not DIAGNOSTIC -- unlike the
    read-only sensors this replaces, this is now something a user
    actually sets, not just observes.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MobiusDeviceCoordinator, serial: str, key: str,
                 translation_key: str, icon: str, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._key = key
        # SERIAL-based, matching every other entity in this integration --
        # deliberately the SAME unique_id the sensor this replaces used,
        # since it's the same underlying setting, just now writable --
        # but note this does NOT migrate the old sensor.* entity itself:
        # switch/sensor are different HA domains, so the old entity_id
        # still needs manual removal. See this module's own docstring.
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_translation_key = translation_key
        self._attr_icon = icon
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        features = (self.coordinator.data or {}).get("advanced_features") or {}
        return super().available and features.get(self._key) is not None

    @property
    def is_on(self) -> bool | None:
        features = (self.coordinator.data or {}).get("advanced_features") or {}
        return features.get(self._key)

    async def _async_set(self, value: bool) -> None:
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

    async def async_turn_on(self, **kwargs) -> None:
        await self._async_set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._async_set(False)


class LocalControlEnabledSwitch(MobiusAdvancedFeatureSwitch):
    """VorTech-relevant (the app's own "Local Control" setting)."""

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "local_control_enabled",
            "local_control_enabled", "mdi:gesture-tap", device_info,
        )


class FanShutdownEnabledSwitch(MobiusAdvancedFeatureSwitch):
    """Radion-relevant (the app's own "Fan Shutdown" setting)."""

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "fan_shutdown_enabled",
            "fan_shutdown_enabled", "mdi:fan-off", device_info,
        )


def _build_advanced_feature_switches(coordinator, serial, device_info, data) -> list[MobiusAdvancedFeatureSwitch]:
    """Same per-attribute gating as sensor.py's own (now-removed)
    _build_advanced_feature_entities() for these two fields --
    AutoDimTimeout/MaxFanSpeed have their own equivalent in select.py."""
    features = data.get("advanced_features") or {}
    entities: list[MobiusAdvancedFeatureSwitch] = []
    if features.get("local_control_enabled") is not None:
        entities.append(LocalControlEnabledSwitch(coordinator, serial, device_info))
    if features.get("fan_shutdown_enabled") is not None:
        entities.append(FanShutdownEnabledSwitch(coordinator, serial, device_info))
    return entities


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

    entities: list[MobiusAdvancedFeatureSwitch] = []
    for device_record in device_records:
        serial = device_record[CONF_SERIAL]
        coordinator = runtime.coordinators.get(serial)
        if coordinator is None:
            _LOGGER.warning(
                "No coordinator found for device %s in entry %s -- skipping its advanced-feature switches",
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

        entities += _build_advanced_feature_switches(coordinator, serial, device_info, data)

    async_add_entities(entities)
