"""Sensor entities for Mobius devices.

Scene activation is a select (select.py); ConfiguredScenesSensor only shows
how many scene slots are used.
"""

from __future__ import annotations

import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import VolumeFlowRateConverter
from homeassistant.util import dt as dt_util

from mobius import format_mesh_address

from . import MobiusRuntimeData
from .const import DOMAIN, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX
from .coordinator import MobiusDeviceCoordinator, derive_sw_version, derive_hw_version, device_display_name, used_scenes
from .entity import entry_tank_identifier, iter_entry_devices

_LOGGER = logging.getLogger(__name__)


class MobiusEntity(CoordinatorEntity[MobiusDeviceCoordinator], SensorEntity):
    """Base of every per-device Mobius sensor. unique_id is
    "{serial}_{key}"."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MobiusDeviceCoordinator, serial: str, key: str,
                 description: SensorEntityDescription, device_info: DeviceInfo,
                 diagnostic: bool = False) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._serial = serial
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_device_info = device_info
        # Set on the entity rather than the description: some Home Assistant
        # versions returned a plain str from the description.
        if diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def data(self) -> dict:
        return self.coordinator.data or {}

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None


class _DataKeySensor(MobiusEntity):
    """A sensor whose state is coordinator.data[key]."""

    _key: str
    _icon: str
    _diagnostic = False

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, self._key,
            SensorEntityDescription(key=self._key, translation_key=self._key, icon=self._icon),
            device_info, diagnostic=self._diagnostic,
        )

    @property
    def native_value(self):
        return self.data.get(self._key)


class SupportTierSensor(_DataKeySensor):
    """Support tier of the device (light/pump/unsupported)."""
    _key, _icon, _diagnostic = "support", "mdi:list-status", True

    @property
    def extra_state_attributes(self):
        return {"support_note": self.data["support_note"]} if "support_note" in self.data else {}


class ErrorStateSensor(_DataKeySensor):
    _key, _icon, _diagnostic = "error_state", "mdi:alert-circle-outline", True


class OperationStateSensor(_DataKeySensor):
    """OperationState (Schedule/Scene/LiveDemo/OOB)."""
    _key, _icon = "operation_state", "mdi:state-machine"


class SchedulePointCountSensor(_DataKeySensor):
    _key, _icon, _diagnostic = "schedule_point_count", "mdi:calendar-clock", True


class CurrentPumpModeSensor(_DataKeySensor):
    """Mode of the active pump schedule block; its parameters are
    attributes."""
    _key, _icon = "current_pump_mode", "mdi:waves"

    @property
    def extra_state_attributes(self):
        return self.data.get("current_pump_params") or {}


class MotorSpeedSensor(MobiusEntity):
    """Pump speed as a percentage of maximum power (speed_percent, never
    negative). The signed raw value (negative = reverse) is an attribute.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "motor_speed",
            SensorEntityDescription(
                key="motor_speed", translation_key="motor_speed", icon="mdi:speedometer",
                native_unit_of_measurement="%", state_class=SensorStateClass.MEASUREMENT,
            ),
            device_info,
        )

    @property
    def native_value(self):
        telemetry = (self.coordinator.data or {}).get("telemetry") or {}
        return telemetry.get("speed_percent")

    @property
    def extra_state_attributes(self):
        telemetry = (self.coordinator.data or {}).get("telemetry") or {}
        raw = telemetry.get("speed")
        if raw is None:
            return {}
        return {"raw_signed_value": raw, "reverse_rotation": raw < 0}


class FlowRateSensor(MobiusEntity):
    """Pump flow (GPH). Only created when get_pump_telemetry() reports
    gph_reliable (the app doesn't show flow otherwise).

    The native unit is gal/h. volume_flow_rate isn't affected by Home
    Assistant's unit system; another unit can be chosen per entity. The
    flow_reliable/minimum_flow/maximum_flow attributes are converted to the
    unit currently displayed, because Home Assistant only converts the state.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "flow_rate",
            SensorEntityDescription(
                key="flow_rate", translation_key="flow_rate",
                device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
                native_unit_of_measurement="gal/h", state_class=SensorStateClass.MEASUREMENT,
            ),
            device_info,
        )

    @property
    def native_value(self):
        telemetry = (self.coordinator.data or {}).get("telemetry") or {}
        return telemetry.get("gph")

    @property
    def extra_state_attributes(self):
        telemetry = (self.coordinator.data or {}).get("telemetry") or {}
        minimum_flow = telemetry.get("minimum_gph")
        maximum_flow = telemetry.get("maximum_gph")
        attributes = {"flow_reliable": telemetry.get("gph_reliable")}
        if minimum_flow is not None or maximum_flow is not None:
            # Converted manually to the displayed unit (see the class docstring).
            display_unit = self.unit_of_measurement
            native_unit = self.native_unit_of_measurement
            if minimum_flow is not None:
                minimum_flow = VolumeFlowRateConverter.convert(minimum_flow, native_unit, display_unit)
            if maximum_flow is not None:
                maximum_flow = VolumeFlowRateConverter.convert(maximum_flow, native_unit, display_unit)
            attributes["minimum_flow"] = minimum_flow
            attributes["maximum_flow"] = maximum_flow
        return attributes


def _used_scenes(coordinator) -> list:
    return used_scenes(coordinator.data or {})


class ConfiguredScenesSensor(MobiusEntity):
    """Number of used scene slots; total_slots and a compact list of the used
    scenes are attributes.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "configured_scenes",
            SensorEntityDescription(
                key="configured_scenes", translation_key="configured_scenes", icon="mdi:palette-swatch",
            ),
            device_info, diagnostic=True,
        )

    @property
    def native_value(self):
        return len(_used_scenes(self.coordinator))

    @property
    def extra_state_attributes(self):
        all_scenes = (self.coordinator.data or {}).get("configured_scenes") or []
        return {
            "total_slots": len(all_scenes),
            "scenes": [
                f"{s.index}: {s.name} ({s.scene_type.name if s.scene_type else 'custom'})"
                for s in _used_scenes(self.coordinator)
            ],
        }


class LightChannelIntensitySensor(MobiusEntity):
    """Current intensity of one light channel, in whole percent.

    The Brightness channel is shown as "Point" (the app's name for it), so it
    isn't confused with ScheduleIntensitySensor; its key and unique_id stay
    "brightness".
    """

    _DISPLAY_NAME_OVERRIDES = {"Brightness": "Point"}

    def __init__(self, coordinator, serial, device_info, channel_name: str):
        self._channel_name = channel_name
        display_name = self._DISPLAY_NAME_OVERRIDES.get(channel_name, channel_name)
        super().__init__(
            coordinator, serial, f"intensity_{channel_name.lower()}",
            SensorEntityDescription(
                key=f"intensity_{channel_name.lower()}",
                translation_key="channel_intensity",
                translation_placeholders={"channel": display_name},
                icon="mdi:brightness-percent",
                native_unit_of_measurement="%",
                state_class="measurement",
                suggested_display_precision=0,
            ),
            device_info,
        )

    @property
    def native_value(self):
        current = (self.coordinator.data or {}).get("current_intensities") or {}
        raw = current.get(self._channel_name)
        return round(raw / 10) if raw is not None else None


class ScheduleIntensitySensor(MobiusEntity):
    """Schedule1Intensity: the schedule-level dimmer applied on top of every
    channel (see python-mobius 06-light-schedule.md), in percent. Used by the
    schedule card to follow the value live. lunar_enabled and moon_phase_icon
    are attributes.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "schedule_intensity",
            SensorEntityDescription(
                key="schedule_intensity", translation_key="schedule_intensity",
                icon="mdi:brightness-6", native_unit_of_measurement="%",
                state_class="measurement", suggested_display_precision=0,
            ),
            device_info,
        )

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {"lunar_enabled": data.get("lunar_enabled"), "moon_phase_icon": data.get("moon_phase_icon")}

    @property
    def native_value(self):
        fraction = (self.coordinator.data or {}).get("schedule_intensity")
        return round(fraction * 100) if fraction is not None else None


class CalibrationSensor(MobiusEntity):
    """Whether light calibration has completed; the last calibration time and
    calibrated bounds are attributes. Only created when calibration data is
    available at setup (pumps don't support it).
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "calibration",
            SensorEntityDescription(key="calibration", translation_key="calibration", icon="mdi:tune"),
            device_info, diagnostic=True,
        )

    @property
    def available(self) -> bool:
        return super().available and (self.coordinator.data or {}).get("calibration") is not None

    @property
    def native_value(self):
        calibration = (self.coordinator.data or {}).get("calibration")
        return calibration.completed if calibration else None

    @property
    def extra_state_attributes(self):
        calibration = (self.coordinator.data or {}).get("calibration")
        if calibration is None:
            return {}
        attrs = {"last_calibration_time": dt_util.utc_from_timestamp(calibration.date_of_last)}
        if calibration.lower_bound is not None:
            attrs["lower_bound"] = calibration.lower_bound
        if calibration.upper_bound is not None:
            attrs["upper_bound"] = calibration.upper_bound
        return attrs


class FirmwareVersionSensor(MobiusEntity):
    """The firmware version shown as the device's sw_version (see
    derive_sw_version()), with every firmware component and the supported
    attribute names as attributes.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "firmware_version",
            SensorEntityDescription(
                key="firmware_version", translation_key="firmware_version", icon="mdi:chip",
            ),
            device_info, diagnostic=True,
        )

    @property
    def native_value(self):
        return derive_sw_version((self.coordinator.data or {}).get("firmware_versions") or {})

    @property
    def extra_state_attributes(self):
        attrs = dict((self.coordinator.data or {}).get("firmware_versions") or {})
        names = self.coordinator.supported_attribute_names
        if names is not None:
            attrs["supported_attributes"] = names
        return attrs


class HardwareRevisionSensor(MobiusEntity):
    """The hardware revision shown as hw_version, with every HardwareRevision
    field (labels for the enum fields) as attributes.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "hardware_revision",
            SensorEntityDescription(
                key="hardware_revision", translation_key="hardware_revision", icon="mdi:developer-board",
            ),
            device_info, diagnostic=True,
        )

    @property
    def native_value(self):
        return derive_hw_version((self.coordinator.data or {}).get("hardware_info") or {})

    @property
    def extra_state_attributes(self):
        return (self.coordinator.data or {}).get("hardware_info") or {}


class MeshAddressSensor(MobiusEntity):
    """The device's mesh-local IPv6 address, from the gateway registry (address
    discovery isn't part of the poll data). Unknown until discovered, which
    for a relayed device can happen after its other sensors are available.
    The time the device was last heard on the mesh is the last_seen attribute.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "mesh_address",
            SensorEntityDescription(
                key="mesh_address", translation_key="mesh_address", icon="mdi:ip-network-outline",
            ),
            device_info, diagnostic=True,
        )

    @property
    def native_value(self):
        group = self.coordinator.registry.group(self.coordinator.pan_id)
        if group is None:
            return None
        member = group.members.get(self.coordinator.serial)
        return format_mesh_address(member.mesh_address) if member is not None else None

    @property
    def extra_state_attributes(self):
        # From the registry, updated by the tank check's mesh read (which
        # then updates the tank's entities), not by this device's poll.
        group = self.coordinator.registry.group(self.coordinator.pan_id)
        member = group.members.get(self.coordinator.serial) if group is not None else None
        if member is None or member.mesh_last_seen_at is None:
            return {}
        return {"last_seen": member.mesh_last_seen_at}


class MeshPrefixSensor(SensorEntity):
    """The tank's shared mesh-local prefix, on the tank device. The value is
    fixed in the config entry, so there is nothing to poll.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, mlprefix_hex: str, tank_identifier: tuple[str, str]) -> None:
        self.entity_description = SensorEntityDescription(
            key="mesh_prefix", translation_key="mesh_prefix", icon="mdi:lan",
        )
        self._attr_unique_id = f"{entry.entry_id}_mesh_prefix"
        self._attr_device_info = DeviceInfo(identifiers={tank_identifier})
        self._mlprefix_hex = mlprefix_hex

    @property
    def native_value(self):
        return self._mlprefix_hex


class GatewayDeviceSensor(SensorEntity):
    """Which device of the tank currently holds the connection (the gateway),
    on the tank device. Shows the gateway's name (else "{model} ({serial})",
    else the serial), with the serial as an attribute. Listens to every
    coordinator of the tank, since the gateway can change with failover.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(
        self, entry: ConfigEntry, pan_id: int, registry, coordinators: dict[str, MobiusDeviceCoordinator],
        tank_identifier: tuple[str, str],
    ) -> None:
        self.entity_description = SensorEntityDescription(
            key="gateway_device", translation_key="gateway_device", icon="mdi:router-wireless",
        )
        self._attr_unique_id = f"{entry.entry_id}_gateway_device"
        self._attr_device_info = DeviceInfo(identifiers={tank_identifier})
        self._pan_id = pan_id
        self._registry = registry
        self._coordinators = coordinators

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        for coordinator in self._coordinators.values():
            self.async_on_remove(coordinator.async_add_listener(self._handle_any_coordinator_update))
        # The gateway is known right after setup, so write the state now.
        self.async_write_ha_state()

    def _handle_any_coordinator_update(self) -> None:
        self.async_write_ha_state()

    def _gateway_serial(self) -> str | None:
        group = self._registry.group(self._pan_id)
        if group is None:
            return None
        return group.gateway_serial

    @property
    def native_value(self):
        serial = self._gateway_serial()
        if serial is None:
            return None
        coordinator = self._coordinators.get(serial)
        return device_display_name(serial, (coordinator.data if coordinator else None) or {}) or serial

    @property
    def extra_state_attributes(self):
        serial = self._gateway_serial()
        if serial is None:
            return None
        return {"serial": serial}


def _build_type_specific_entities(coordinator, serial, device_info, support, data) -> list[SensorEntity]:
    """The pump- or light-specific sensors for one device from its current
    data. Also used by __init__.py's _async_ensure_sensors_exist() to create
    sensors missed at setup.
    """
    if support.startswith("pump"):
        entities: list[SensorEntity] = [
            OperationStateSensor(coordinator, serial, device_info),
            MotorSpeedSensor(coordinator, serial, device_info),
            CurrentPumpModeSensor(coordinator, serial, device_info),
        ]
        # Only when gph is reliable (a missing value counts as not reliable;
        # _async_ensure_sensors_exist() adds the sensor later).
        if (data.get("telemetry") or {}).get("gph_reliable"):
            entities.append(FlowRateSensor(coordinator, serial, device_info))
        return entities
    elif support == "light":
        entities: list[SensorEntity] = []
        channel_names = data.get("channels") or []
        for name in channel_names:
            entities.append(LightChannelIntensitySensor(coordinator, serial, device_info, name))
        entities.append(ScheduleIntensitySensor(coordinator, serial, device_info))
        # Only for lights that report calibration data.
        if data.get("calibration") is not None:
            entities.append(CalibrationSensor(coordinator, serial, device_info))
        return entities
    return []


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Sets up the sensors of every device of the entry, plus the tank-level
    sensors for a multi-device tank."""
    runtime: MobiusRuntimeData = entry.runtime_data
    mlprefix_hex = entry.data.get(CONF_MLPREFIX)
    pan_id = entry.data.get(CONF_PAN_ID)
    registry = hass.data.get(DOMAIN, {}).get("gateway_registry")

    entities: list[SensorEntity] = []
    for serial, coordinator, device_info, data in iter_entry_devices(hass, entry):
        entities += [
            SupportTierSensor(coordinator, serial, device_info),
            ErrorStateSensor(coordinator, serial, device_info),
            SchedulePointCountSensor(coordinator, serial, device_info),
            FirmwareVersionSensor(coordinator, serial, device_info),
            HardwareRevisionSensor(coordinator, serial, device_info),
        ]
        # A Bluetooth-only device has no mesh address.
        if not coordinator.bluetooth_only:
            entities.append(MeshAddressSensor(coordinator, serial, device_info))
        # Devices without scene support report no slots at all.
        if data.get("configured_scenes"):
            entities.append(ConfiguredScenesSensor(coordinator, serial, device_info))

        type_specific = _build_type_specific_entities(coordinator, serial, device_info, data.get("support", ""), data)
        entities += type_specific
        # Kept for _async_ensure_sensors_exist() (see __init__.py).
        runtime.sensor_device_infos[serial] = device_info
        runtime.created_sensor_unique_ids.update(e.unique_id for e in type_specific)

    # Tank-level sensors: only meaningful with a mesh prefix and more than one
    # device.
    if mlprefix_hex is not None and len(entry.data.get(CONF_DEVICES, [])) > 1:
        tank_identifier = entry_tank_identifier(entry)
        entities.append(MeshPrefixSensor(entry, mlprefix_hex, tank_identifier))
        if registry is not None and pan_id is not None:
            entities.append(GatewayDeviceSensor(entry, pan_id, registry, runtime.coordinators, tank_identifier))

    async_add_entities(entities)
    # Stored last, so a failed setup leaves no callback for
    # _async_ensure_sensors_exist().
    runtime.sensor_add_entities = async_add_entities
