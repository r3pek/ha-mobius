"""Sensor entities for Mobius devices.

Read-only for now, deliberately -- kept in lockstep with what
python-mobius itself supports rather than getting ahead of it. Control
(scenes, schedule writes) will follow the same pattern once the underlying
library grows write support.
"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MobiusRuntimeData
from .const import DOMAIN
from .coordinator import MobiusScheduleCoordinator, MobiusStatusCoordinator


def _device_info(address: str, status_data: dict) -> DeviceInfo:
    custom_name = status_data.get("name")
    model = status_data.get("model")
    serial = status_data.get("serial")

    # The device's own configured "name" attribute is often blank (confirmed
    # on real hardware -- one of our test XR15 lights had an empty name).
    # Falling back to just the model name alone isn't enough to disambiguate
    # multiple identical devices (e.g. two XR15 lights would both show the
    # exact same name) -- append the serial number for a unique, meaningful
    # fallback that's traceable to the physical unit.
    if custom_name:
        name = custom_name
    elif model and serial:
        name = f"{model} ({serial})"
    elif model:
        name = model
    elif serial:
        name = f"Mobius device ({serial})"
    else:
        name = "Mobius device"

    return DeviceInfo(
        identifiers={(DOMAIN, address)},
        connections={("bluetooth", address)},
        name=name,
        manufacturer=status_data.get("manufacturer"),
        model=model,
        serial_number=serial,
    )


class MobiusStatusEntity(CoordinatorEntity[MobiusStatusCoordinator], SensorEntity):
    """Base for sensors fed by the fast status coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MobiusStatusCoordinator, address: str, key: str,
                 description: SensorEntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._address = address
        self._attr_unique_id = f"{address}_{key}"
        self._attr_device_info = _device_info(address, coordinator.data or {})

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None


class MobiusScheduleEntity(CoordinatorEntity[MobiusScheduleCoordinator], SensorEntity):
    """Base for sensors fed by the slow schedule coordinator."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MobiusScheduleCoordinator, address: str, key: str,
                 description: SensorEntityDescription, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._address = address
        self._attr_unique_id = f"{address}_{key}"
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None


# ---- status-tier entities --------------------------------------------------

class SupportTierSensor(MobiusStatusEntity):
    """Diagnostic: which support tier this device falls into (light/pump/unsupported)."""

    def __init__(self, coordinator, address):
        super().__init__(
            coordinator, address, "support",
            SensorEntityDescription(key="support", translation_key="support"),
        )
        # Set directly rather than via SensorEntityDescription -- observed
        # HA (at least 2025.1.4) returning a plain str instead of the
        # EntityCategory enum when set through entity_description in some
        # cases; this path is documented as reliable regardless.
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("support")

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        attrs = {}
        if "support_note" in data:
            attrs["support_note"] = data["support_note"]
        return attrs


class ErrorStateSensor(MobiusStatusEntity):
    def __init__(self, coordinator, address):
        super().__init__(
            coordinator, address, "error_state",
            SensorEntityDescription(key="error_state", translation_key="error_state"),
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("error_state")


class OperationStateSensor(MobiusStatusEntity):
    """Pump/light devices only -- OperationState (Schedule/Scene/LiveDemo/OOB)."""

    def __init__(self, coordinator, address):
        super().__init__(
            coordinator, address, "operation_state",
            SensorEntityDescription(key="operation_state", translation_key="operation_state"),
        )

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("operation_state")


class MotorSpeedSensor(MobiusStatusEntity):
    """Pump devices only. Raw MotorSpeed value -- unit/scale not confirmed against
    real firmware documentation, reported as-is (see documentation/03 in python-mobius)."""

    def __init__(self, coordinator, address):
        super().__init__(
            coordinator, address, "motor_speed",
            SensorEntityDescription(key="motor_speed", translation_key="motor_speed",
                                     state_class=SensorStateClass.MEASUREMENT),
        )

    @property
    def native_value(self):
        telemetry = (self.coordinator.data or {}).get("telemetry") or {}
        return telemetry.get("speed")


class FlowRateSensor(MobiusStatusEntity):
    """Pump devices only. Estimated flow (GPH), confirmed live-queried by the app.

    native_unit_of_measurement stays "gal/h" -- that's the actual native
    value the protocol reports, not a display preference. With device_class
    set, Home Assistant automatically converts the *displayed* value to
    whatever unit system the instance is configured for (Settings -> General
    -> Unit System) -- e.g. shows L/h automatically for Metric instances --
    without us hardcoding a "default" unit or doing manual conversion math
    that could itself introduce an error. Confirmed 'gal/h' is a valid
    VOLUME_FLOW_RATE unit on HA 2026.06 (current docs list it); it was NOT
    valid on HA 2025.1.4 (the version pinned by this repo's test harness) --
    exact cutoff version between those two isn't pinned down, so if you're
    running something older than ~2026, double check this still validates.
    """

    def __init__(self, coordinator, address):
        super().__init__(
            coordinator, address, "flow_rate",
            SensorEntityDescription(
                key="flow_rate", translation_key="flow_rate",
                device_class=SensorDeviceClass.VOLUME_FLOW_RATE,
                native_unit_of_measurement="gal/h", state_class=SensorStateClass.MEASUREMENT,
            ),
        )

    @property
    def native_value(self):
        telemetry = (self.coordinator.data or {}).get("telemetry") or {}
        return telemetry.get("gph")


# ---- schedule-tier entities -------------------------------------------------

class SchedulePointCountSensor(MobiusScheduleEntity):
    def __init__(self, coordinator, address, device_info):
        super().__init__(
            coordinator, address, "schedule_point_count",
            SensorEntityDescription(
                key="schedule_point_count", translation_key="schedule_point_count",
            ),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("schedule_point_count")


class CurrentPumpModeSensor(MobiusScheduleEntity):
    """Pump devices only -- the currently active schedule block's mode."""

    def __init__(self, coordinator, address, device_info):
        super().__init__(
            coordinator, address, "current_pump_mode",
            SensorEntityDescription(key="current_pump_mode", translation_key="current_pump_mode"),
            device_info,
        )

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("current_pump_mode")

    @property
    def extra_state_attributes(self):
        return (self.coordinator.data or {}).get("current_pump_params") or {}


class LightChannelIntensitySensor(MobiusScheduleEntity):
    """Light devices only -- one entity per channel, current interpolated intensity in %."""

    def __init__(self, coordinator, address, device_info, channel_name: str):
        self._channel_name = channel_name
        super().__init__(
            coordinator, address, f"intensity_{channel_name.lower()}",
            SensorEntityDescription(
                key=f"intensity_{channel_name.lower()}",
                translation_key="channel_intensity",
                translation_placeholders={"channel": channel_name},
                native_unit_of_measurement="%",
                state_class="measurement",
            ),
            device_info,
        )

    @property
    def native_value(self):
        current = (self.coordinator.data or {}).get("current_intensities") or {}
        raw = current.get(self._channel_name)
        return round(raw / 10, 1) if raw is not None else None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up sensors for a Mobius config entry."""
    runtime: MobiusRuntimeData = entry.runtime_data
    address = entry.data[CONF_ADDRESS]
    status = runtime.status
    schedule = runtime.schedule

    entities: list[SensorEntity] = [
        SupportTierSensor(status, address),
        ErrorStateSensor(status, address),
    ]

    support = (status.data or {}).get("support", "")

    if support.startswith("pump"):
        entities += [
            OperationStateSensor(status, address),
            MotorSpeedSensor(status, address),
            FlowRateSensor(status, address),
        ]

    device_info = _device_info(address, status.data or {})
    entities.append(SchedulePointCountSensor(schedule, address, device_info))

    if support.startswith("pump"):
        entities.append(CurrentPumpModeSensor(schedule, address, device_info))
    elif support == "light":
        channel_names = (schedule.data or {}).get("channels") or []
        for name in channel_names:
            entities.append(LightChannelIntensitySensor(schedule, address, device_info, name))

    async_add_entities(entities)
