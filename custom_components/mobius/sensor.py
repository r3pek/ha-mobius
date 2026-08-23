"""Sensor entities for Mobius devices.

Read-only for now, deliberately -- kept in lockstep with what
python-mobius itself supports rather than getting ahead of it. Control
(scenes, schedule writes) will follow the same pattern once the underlying
library grows write support.
"""

from __future__ import annotations

import ipaddress
import logging

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.unit_conversion import VolumeFlowRateConverter
from homeassistant.util import dt as dt_util

from . import MobiusRuntimeData, tank_device_identifier
from .const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX

_LOGGER = logging.getLogger(__name__)
from .coordinator import MobiusDeviceCoordinator, derive_sw_version, derive_hw_version


def _device_info(serial: str, data: dict, address: str | None = None,
                  sw_version: str | None = None, hw_version: str | None = None,
                  via_device: tuple[str, str] | None = None) -> DeviceInfo:
    """
    identifiers is SERIAL-based, not BLE-address-based -- a real,
    necessary fix, not incidental to this integration's move to
    tank-aware, multi-device config entries: a tank peer never has any
    stored BLE address in the first place (see config_flow.py's own
    _async_create_tank_entry() docstring for why), so address can't be
    the identity for every device anymore. serial is the only
    identifier guaranteed present either way -- see python-mobius's own
    documentation/12-device-identity-and-address-stability.md for why
    it's the right one regardless, not just the only available option
    here. coordinator.py's own _sync_device_registry_info() must
    look this device up the same, serial-based way, or it would never
    find anything to update.

    address, if known (an ad-hoc device's own entry stores it; a tank
    peer's own entry doesn't), is used only for the connections hint,
    not identity.

    via_device, if given, is the synthetic tank device's own identifier
    (see __init__.py's tank_device_identifier()) -- produces the "one
    hub, N child devices" grouping this whole feature was designed
    against. None for a single, ad-hoc device (no tank to group under).
    """
    custom_name = data.get("name")
    model = data.get("model")

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
        identifiers={(DOMAIN, serial)},
        connections={("bluetooth", address)} if address else set(),
        name=name,
        manufacturer=data.get("manufacturer"),
        model=model,
        serial_number=serial,
        sw_version=sw_version,
        hw_version=hw_version,
        via_device=via_device,
    )


class MobiusEntity(CoordinatorEntity[MobiusDeviceCoordinator], SensorEntity):
    """Base for every Mobius sensor -- one coordinator per device now
    (status and schedule data both come from the same read cycle), unlike
    the earlier two-tier design."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: MobiusDeviceCoordinator, serial: str, key: str,
                 description: SensorEntityDescription, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._serial = serial
        # SERIAL-based, not address-based -- see _device_info()'s own
        # docstring for why this had to change (a tank peer has no
        # stored address at all, so it couldn't be the basis for
        # unique_id for every device anymore either).
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_device_info = device_info

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None


class SupportTierSensor(MobiusEntity):
    """Diagnostic: which support tier this device falls into (light/pump/unsupported)."""

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "support",
            SensorEntityDescription(key="support", translation_key="support", icon="mdi:list-status"),
            device_info,
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


class ErrorStateSensor(MobiusEntity):
    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "error_state",
            SensorEntityDescription(
                key="error_state", translation_key="error_state", icon="mdi:alert-circle-outline",
            ),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("error_state")


class OperationStateSensor(MobiusEntity):
    """Pump/light devices only -- OperationState (Schedule/Scene/LiveDemo/OOB)."""

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "operation_state",
            SensorEntityDescription(
                key="operation_state", translation_key="operation_state", icon="mdi:state-machine",
            ),
            device_info,
        )

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("operation_state")


class MotorSpeedSensor(MobiusEntity):
    """Pump devices only. Confirmed (via the decompiled app's own display
    code -- see python-mobius documentation/03) to be a percentage of max
    pump power, not RPM. Uses speed_percent (always non-negative); the raw
    signed value (sign encodes reverse-rotation direction) is exposed as an
    attribute rather than the primary state."""

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
    """Pump devices only. Estimated flow (GPH), confirmed live-queried by the app.

    Only created when python-mobius's own get_pump_telemetry() reports
    gph_reliable=True for this device (see _build_type_specific_entities()'s
    own comment, and that method's docstring in python-mobius, for the
    full confirmation) -- a device the app itself wouldn't trust a raw
    gph reading for (no supported flow range) doesn't get this entity
    at all, rather than showing a number the app itself would never
    display.

    Exposes flow_reliable/minimum_flow/maximum_flow as this entity's own
    extra state attributes -- named generically ("flow", not "gph"),
    deliberately not matching python-mobius's own gph-prefixed dict keys
    one-to-one: this entity's own native_unit_of_measurement can be
    overridden per-entity to something other than gal/h (see below), and
    these attribute names shouldn't be locked to a specific unit that
    might no longer match what's actually displayed.

    minimum_flow/maximum_flow are actively converted (via
    VolumeFlowRateConverter, the same converter HA's own SensorEntity
    uses internally) to whatever unit is CURRENTLY effectively displayed
    -- i.e. self.unit_of_measurement, which accounts for a per-entity
    override, not native_unit_of_measurement, which never changes.
    Confirmed via a real, reported case: HA's own native_value -> state
    conversion (see below) does NOT extend to extra_state_attributes at
    all -- these are plain values an integration returns directly, with
    no framework involvement -- so without this, overriding this
    entity's own display unit (e.g. to L/h) converts the visible state
    correctly but silently leaves minimum_flow/maximum_flow in gal/h,
    unconverted and unlabeled as such.

    native_unit_of_measurement stays "gal/h" -- that's the actual native
    value the protocol reports, not a display preference.

    CORRECTION (verified against real HA source, homeassistant/components/
    sensor/__init__.py and homeassistant/util/unit_system.py): unlike
    temperature/length/pressure, `volume_flow_rate` is NOT one of the
    device classes tied to HA's system-wide Metric/US Customary toggle
    (Settings -> General -> Unit System) -- that toggle has no effect on
    this sensor at all. device_class=VOLUME_FLOW_RATE does register real
    conversion machinery (VolumeFlowRateConverter, confirmed present), but
    it's only invoked via a PER-ENTITY manual override stored in the entity
    registry (Settings -> Devices & Services -> Entities -> this entity ->
    gear icon -> "Unit of measurement"), not automatically from any
    system-wide preference. If you want L/h (or any other unit) displayed,
    set it there -- there's no code-level "default" to change.

    Confirmed 'gal/h' is a valid VOLUME_FLOW_RATE unit on HA 2026.06
    (current docs list it); it was NOT valid on HA 2025.1.4 (the version
    pinned by this repo's test harness) -- exact cutoff version between
    those two isn't pinned down, so if you're running something older than
    ~2026, double check this still validates.
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
            # HA's own native_value -> state unit conversion (see this
            # class's own docstring) does NOT extend to
            # extra_state_attributes -- these are plain values this
            # integration returns directly, with no involvement from HA's
            # conversion framework at all. self.unit_of_measurement (NOT
            # native_unit_of_measurement) is the currently-EFFECTIVE unit,
            # accounting for any per-entity override the user has set
            # (see this class's own docstring on how) -- converting these
            # two values here, manually, the same way HA's own `state`
            # property converts native_value, keeps them consistent with
            # whatever unit the entity's own state is actually showing.
            display_unit = self.unit_of_measurement
            native_unit = self.native_unit_of_measurement
            if minimum_flow is not None:
                minimum_flow = VolumeFlowRateConverter.convert(minimum_flow, native_unit, display_unit)
            if maximum_flow is not None:
                maximum_flow = VolumeFlowRateConverter.convert(maximum_flow, native_unit, display_unit)
            attributes["minimum_flow"] = minimum_flow
            attributes["maximum_flow"] = maximum_flow
        return attributes


class SchedulePointCountSensor(MobiusEntity):
    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "schedule_point_count",
            SensorEntityDescription(
                key="schedule_point_count", translation_key="schedule_point_count",
                icon="mdi:calendar-clock",
            ),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("schedule_point_count")


class CurrentPumpModeSensor(MobiusEntity):
    """Pump devices only -- the currently active schedule block's mode."""

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "current_pump_mode",
            SensorEntityDescription(
                key="current_pump_mode", translation_key="current_pump_mode", icon="mdi:waves",
            ),
            device_info,
        )

    @property
    def native_value(self):
        return (self.coordinator.data or {}).get("current_pump_mode")

    @property
    def extra_state_attributes(self):
        return (self.coordinator.data or {}).get("current_pump_params") or {}


class LightChannelIntensitySensor(MobiusEntity):
    """
    Light devices only -- one entity per channel, current interpolated
    intensity in %.

    Whole numbers, not decimals -- the underlying raw value is itself
    only ever a coarse permille figure (confirmed schedule/interpolation
    granularity), so a fractional percent doesn't represent any real
    additional precision; it's just noise. suggested_display_precision=0
    is a frontend display hint (a user could still override it per-entity
    in HA's own UI) -- native_value itself also returns a true int
    (round() with no second argument, not round(x, 0) which would still
    be a float like 100.0), so the underlying state/history is whole
    numbers too, not just the display.
    """

    def __init__(self, coordinator, serial, device_info, channel_name: str):
        self._channel_name = channel_name
        super().__init__(
            coordinator, serial, f"intensity_{channel_name.lower()}",
            SensorEntityDescription(
                key=f"intensity_{channel_name.lower()}",
                translation_key="channel_intensity",
                translation_placeholders={"channel": channel_name},
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


class CalibrationSensor(MobiusEntity):
    """
    Light devices only -- confirmed via real device testing AND the app's
    own UI gating (its own device category check) to be a light feature; pumps don't
    expose this (get_calibration_info() returns None for them, confirmed
    against real VorTech hardware). Only added to a config entry if
    calibration data was actually present at setup -- see
    async_setup_entry() below.

    State is whether calibration has completed (True/False); the last
    calibration date and calibrated speed bounds (if available) are
    exposed as attributes rather than separate entities, since they're
    supplementary detail to the main completed/not-completed status.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "calibration",
            SensorEntityDescription(key="calibration", translation_key="calibration", icon="mdi:tune"),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

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


class LocalControlEnabledSensor(MobiusEntity):
    """
    VorTech-relevant (per the app's own AdvancedFeatures screen), but
    deliberately not gated to pumps here -- see coordinator.py's own
    comment on why get_advanced_features() is called unconditionally
    for every device, regardless of "support". Only added to a config
    entry if this specific attribute was actually present at setup --
    see async_setup_entry() below and _build_advanced_feature_entities().
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "local_control_enabled",
            SensorEntityDescription(
                key="local_control_enabled", translation_key="local_control_enabled", icon="mdi:gesture-tap",
            ),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        features = (self.coordinator.data or {}).get("advanced_features") or {}
        return super().available and features.get("local_control_enabled") is not None

    @property
    def native_value(self):
        features = (self.coordinator.data or {}).get("advanced_features") or {}
        return features.get("local_control_enabled")


class AutoDimTimeoutSensor(MobiusEntity):
    """
    VorTech-relevant (the app's own "Led Auto Dim" setting) -- seconds
    until the device's own status LED dims, per the app's own preset
    options (0 = "Always On"/never dims). See python-mobius's own
    AdvancedFeatures docstring for the confirmed preset value list.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "auto_dim_timeout",
            SensorEntityDescription(
                key="auto_dim_timeout", translation_key="auto_dim_timeout", icon="mdi:led-off",
                native_unit_of_measurement=UnitOfTime.SECONDS,
            ),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        features = (self.coordinator.data or {}).get("advanced_features") or {}
        return super().available and features.get("auto_dim_timeout") is not None

    @property
    def native_value(self):
        features = (self.coordinator.data or {}).get("advanced_features") or {}
        return features.get("auto_dim_timeout")


class MaxFanSpeedSensor(MobiusEntity):
    """
    Radion-relevant (the app's own "Max Fan Speed" setting) -- already
    converted to percent by python-mobius's own get_advanced_features(),
    including its own -1/"unlimited" sentinel handling (see that
    method's own docstring) -- this entity never sees the raw permille
    encoding or the sentinel at all.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "max_fan_speed",
            SensorEntityDescription(
                key="max_fan_speed", translation_key="max_fan_speed", icon="mdi:fan",
                native_unit_of_measurement=PERCENTAGE, state_class=SensorStateClass.MEASUREMENT,
            ),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        features = (self.coordinator.data or {}).get("advanced_features") or {}
        return super().available and features.get("max_fan_speed") is not None

    @property
    def native_value(self):
        features = (self.coordinator.data or {}).get("advanced_features") or {}
        return features.get("max_fan_speed")


class FanShutdownEnabledSensor(MobiusEntity):
    """Radion-relevant (the app's own "Fan Shutdown" setting)."""

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "fan_shutdown_enabled",
            SensorEntityDescription(
                key="fan_shutdown_enabled", translation_key="fan_shutdown_enabled", icon="mdi:fan-off",
            ),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        features = (self.coordinator.data or {}).get("advanced_features") or {}
        return super().available and features.get("fan_shutdown_enabled") is not None

    @property
    def native_value(self):
        features = (self.coordinator.data or {}).get("advanced_features") or {}
        return features.get("fan_shutdown_enabled")


class FirmwareVersionSensor(MobiusEntity):
    """
    Diagnostic: the same headline value already shown as sw_version on
    Home Assistant's own built-in device info card (that label -- always
    "Firmware", not customizable per-integration -- comes from Home
    Assistant itself, not this entity), but as a first-class entity with
    the full per-component breakdown available as attributes -- e.g.
    "Radio Firmware"/"Filesystem"/"Radio OS"/"Radio"/"WLAN"/"Product OS"/
    "Product Bootloader" for a light, not just the single "Firmware"
    value derive_sw_version() picks as most representative. See
    coordinator.py's derive_sw_version() for the confirmed label priority.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "firmware_version",
            SensorEntityDescription(
                key="firmware_version", translation_key="firmware_version", icon="mdi:chip",
            ),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return derive_sw_version((self.coordinator.data or {}).get("firmware_versions") or {})

    @property
    def extra_state_attributes(self):
        return (self.coordinator.data or {}).get("firmware_versions") or {}


class HardwareRevisionSensor(MobiusEntity):
    """
    Diagnostic: the same headline value already shown as hw_version on
    Home Assistant's own built-in device info card (labeled "Hardware" --
    not customizable per-integration), but as a first-class entity with
    the full per-field breakdown available as attributes.

    Requires python-mobius>=0.3.0: as of that version,
    get_hardware_info() already decodes Color/ProductType/RadioType/
    MotorType into their own confirmed display label strings (e.g.
    "White"/"VorTech"/"QCA4020"/"VorTech MP40 G3" -- each is itself a
    confirmed enum with confirmed labels, see that library's
    mobius.constants), and Revision/Segments as plain integers -- used
    directly here, not re-decoded.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "hardware_revision",
            SensorEntityDescription(
                key="hardware_revision", translation_key="hardware_revision", icon="mdi:developer-board",
            ),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return derive_hw_version((self.coordinator.data or {}).get("hardware_info") or {})

    @property
    def extra_state_attributes(self):
        return (self.coordinator.data or {}).get("hardware_info") or {}


class MeshAddressSensor(MobiusEntity):
    """
    Diagnostic: this device's own Thread mesh-local IPv6 address, from
    the gateway registry's own live cache (gateway_registry.MemberState.
    mesh_address) -- NOT read from coordinator.data, since address
    discovery isn't part of the normal poll cycle's own fetched data.
    Populated for every device (gateway included) at setup -- see
    __init__.py's own async_setup_entry() docstring for why the gateway
    needed a deliberate fix to get this too, since nothing else in
    normal operation ever populates a gateway's own address in the
    registry (relay has no need to know it).

    Unavailable (native_value None) until that discovery has actually
    succeeded at least once -- for a relayed device, this can briefly
    lag behind the device's own other sensors becoming available (which
    only need the GATEWAY's connection to be up, not this specific
    device's own address to already be known) -- not a bug, just the
    two becoming available on slightly different schedules.

    Also carries this device's own "last seen on the mesh" timestamp as
    an attribute (last_seen), folded in here rather than as its own
    separate sensor entity, since the two are closely related diagnostic
    facts about the same underlying mesh connectivity, not independently
    meaningful enough to justify a whole extra entity each. Refreshed on
    the same schedule as every other poll-driven sensor (every poll
    cycle, for every device -- see coordinator.py's own _fetch()), read
    directly from coordinator.data here rather than the registry, since
    that's where it's actually written each cycle.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "mesh_address",
            SensorEntityDescription(
                key="mesh_address", translation_key="mesh_address", icon="mdi:ip-network-outline",
            ),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        group = self.coordinator.registry.group(self.coordinator.pan_id)
        if group is None:
            return None
        member = group.members.get(self.coordinator.serial)
        if member is None or member.mesh_address is None:
            return None
        # A real Thread mesh-local IPv6 address (16 raw bytes, confirmed
        # in python-mobius's own wire-format documentation) -- format it
        # as one (standard colon-separated, zero-compressed notation via
        # the stdlib ipaddress module), not raw hex.
        return str(ipaddress.IPv6Address(member.mesh_address))

    @property
    def extra_state_attributes(self):
        last_seen = (self.coordinator.data or {}).get("mesh_last_seen_at")
        if last_seen is None:
            return {}
        return {"last_seen": last_seen}


class MeshPrefixSensor(SensorEntity):
    """
    Diagnostic: the tank's own shared 8-byte Thread mesh-local prefix
    (see python-mobius's mobius.discovery.discover_tank()) -- attached
    to the synthetic TANK device itself (see __init__.py's
    tank_device_identifier()/_register_tank_device()), not any one real
    device, since it's shared by every device on the tank, not a
    per-device property. A plain SensorEntity, not MobiusEntity -- no
    coordinator of its own to poll (the value is fixed at tank-creation
    time and stored directly in the config entry, see config_flow.py's
    own _async_create_tank_entry()), so there's nothing to subscribe to
    for updates; always available once created.
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
    """
    Diagnostic: which of this tank's devices currently holds the actual
    BLE connection and relays for the others -- attached to the
    synthetic TANK device itself (see MeshPrefixSensor's own docstring
    for why), since which device this is can change over the tank's
    lifetime (gateway failover -- see gateway_registry.py's own
    GATEWAY_FAILURE_THRESHOLD) and isn't a property of any one real
    device.

    Shows the gateway device's own configured NAME, not its serial --
    that name is already fetched fresh on every single poll cycle (see
    coordinator.py's own _fetch_all(), which always calls
    get_device_info()), so a rename in the Mobius app itself shows up
    here within one normal poll interval, same as everywhere else in
    this integration -- no separate polling needed for this specifically.
    Falls back to "{model} ({serial})" if the device has no configured
    name (matching _device_info()'s own fallback chain), or to the bare
    serial if this integration doesn't have any data for that device at
    all yet (shouldn't normally happen, since every device in
    CONF_DEVICES always gets its own coordinator).

    Not a MobiusEntity/CoordinatorEntity tied to one single coordinator
    -- the gateway can be reported by whichever of the tank's devices
    happens to poll next, not always the same one, so this listens to
    ALL of the tank's own coordinators (via each one's own
    async_add_listener(), the same "notify on any update" mechanism
    DataUpdateCoordinator already exposes) rather than just one.
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
        # Written once immediately, rather than waiting for the first of
        # potentially several devices' own next poll cycle to complete --
        # the gateway is very likely already known right after setup
        # (registry.join() runs before any of this platform's own entities
        # are even created), so there's no reason to show unavailable
        # until then.
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
        data = (coordinator.data if coordinator else None) or {}
        name = data.get("name")
        if name:
            return name
        model = data.get("model")
        if model:
            return f"{model} ({serial})"
        return serial

    @property
    def extra_state_attributes(self):
        serial = self._gateway_serial()
        if serial is None:
            return None
        return {"serial": serial}


def _build_type_specific_entities(coordinator, serial, device_info, support, data) -> list[SensorEntity]:
    """The pump-or-light-specific entities for one device, given its
    CURRENT support/data snapshot -- split out from async_setup_entry()
    below so _async_ensure_sensors_exist() (in __init__.py) can reuse
    the exact same logic later, for a device whose data wasn't ready
    yet the first time this ran. See that function's own docstring for
    the full story of why a second call, later, with fresher data, is
    sometimes necessary at all."""
    if support.startswith("pump"):
        entities: list[SensorEntity] = [
            OperationStateSensor(coordinator, serial, device_info),
            MotorSpeedSensor(coordinator, serial, device_info),
            CurrentPumpModeSensor(coordinator, serial, device_info),
        ]
        # Confirmed via the app's own support-check logic for its live
        # flow gauge (see get_pump_telemetry()'s own docstring in
        # python-mobius): the app itself doesn't trust or display a raw
        # gph reading without a supported flow range (with a narrow
        # exception for a few old Nero pumps) -- there's no reason for
        # this integration to expose a sensor for a value the app
        # itself wouldn't show. gph_reliable missing entirely (no
        # telemetry fetched yet) is treated the same as False here --
        # _async_ensure_sensors_exist() (see __init__.py) picks this
        # entity up once real data confirms it's actually reliable.
        if (data.get("telemetry") or {}).get("gph_reliable"):
            entities.append(FlowRateSensor(coordinator, serial, device_info))
        return entities
    elif support == "light":
        entities: list[SensorEntity] = []
        channel_names = data.get("channels") or []
        for name in channel_names:
            entities.append(LightChannelIntensitySensor(coordinator, serial, device_info, name))
        # Only added if calibration data was actually present at setup --
        # confirmed via real hardware that not all lights necessarily
        # support this, and there's no point creating a permanently
        # unavailable entity for one that doesn't.
        if data.get("calibration") is not None:
            entities.append(CalibrationSensor(coordinator, serial, device_info))
        return entities
    return []


def _build_advanced_feature_entities(coordinator, serial, device_info, data) -> list[SensorEntity]:
    """The subset of AdvancedFeatures entities this specific device's
    CURRENT data actually supports -- split out the same way as
    _build_type_specific_entities() above, and for the same reason
    (reused by _async_ensure_sensors_exist() later). Deliberately takes
    no "support" parameter at all, unlike that function -- these four
    attributes are checked independently of pump/light type entirely
    (see coordinator.py's own comment on why get_advanced_features() is
    called unconditionally), so gating entity creation on "support"
    here would reintroduce exactly the per-device-type hardcoding this
    was built to avoid."""
    features = data.get("advanced_features") or {}
    entities: list[SensorEntity] = []
    if features.get("local_control_enabled") is not None:
        entities.append(LocalControlEnabledSensor(coordinator, serial, device_info))
    if features.get("auto_dim_timeout") is not None:
        entities.append(AutoDimTimeoutSensor(coordinator, serial, device_info))
    if features.get("max_fan_speed") is not None:
        entities.append(MaxFanSpeedSensor(coordinator, serial, device_info))
    if features.get("fan_shutdown_enabled") is not None:
        entities.append(FanShutdownEnabledSensor(coordinator, serial, device_info))
    return entities


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up sensors for a Mobius config entry -- one or more devices
    (see const.py's own module-level docstring for the CONF_DEVICES
    shape this mirrors)."""
    runtime: MobiusRuntimeData = entry.runtime_data
    mlprefix_hex = entry.data.get(CONF_MLPREFIX)
    pan_id = entry.data.get(CONF_PAN_ID)
    device_records = entry.data.get(CONF_DEVICES, [])
    registry = hass.data.get(DOMAIN, {}).get("gateway_registry")

    # via_device grouping only applies to a genuine multi-device tank --
    # matches __init__.py's own _register_tank_device() condition exactly
    # (a single ad-hoc device, or a tank entry that currently has only
    # one device merged into it, has no synthetic tank/"hub" device to
    # group under in the first place).
    tank_identifier = tank_device_identifier(mlprefix_hex) if mlprefix_hex is not None else None
    via_device = tank_identifier if tank_identifier is not None and len(device_records) > 1 else None

    entities: list[SensorEntity] = []
    for device_record in device_records:
        serial = device_record[CONF_SERIAL]
        coordinator = runtime.coordinators.get(serial)
        if coordinator is None:
            # Shouldn't normally happen -- __init__.py's own
            # async_setup_entry() builds runtime.coordinators from this
            # exact same device list -- but fail soft (skip this device's
            # entities) rather than crash the whole platform setup over
            # one unexpectedly-missing coordinator.
            _LOGGER.warning(
                "No coordinator found for device %s in entry %s -- skipping its entities",
                serial, entry.entry_id,
            )
            continue

        address = device_record.get(CONF_ADDRESS)
        data = coordinator.data or {}
        support = data.get("support", "")

        # See derive_sw_version()/_SW_VERSION_LABEL_PRIORITY in coordinator.py
        # for the confirmed label priority (device-reported "Firmware" first,
        # not "Product OS" -- confirmed by direct comparison against what the
        # official app itself displays) and why it's a fallback list rather
        # than a single hardcoded lookup. Not all firmware/hardware
        # components as separate sensors -- that would be sensor sprawl for
        # something that's fundamentally device info, not a changing value;
        # the full breakdown is available via python-mobius directly for
        # anyone who wants it. coordinator._sync_device_registry_info()
        # also keeps the device registry in sync afterward if any of these
        # (or name/model/manufacturer) change, using the same derivation
        # logic -- including fixing a device stuck at this method's own
        # generic "Mobius device (SERIAL)" fallback name (built right here,
        # below) once real data actually becomes available, since this is
        # only ever consulted once, at entity-creation time, not
        # continuously.
        sw_version = derive_sw_version(data.get("firmware_versions", {}))
        hw_version = derive_hw_version(data.get("hardware_info", {}))
        device_info = _device_info(
            serial, data, address=address, sw_version=sw_version, hw_version=hw_version,
            via_device=via_device,
        )

        entities += [
            SupportTierSensor(coordinator, serial, device_info),
            ErrorStateSensor(coordinator, serial, device_info),
            SchedulePointCountSensor(coordinator, serial, device_info),
            FirmwareVersionSensor(coordinator, serial, device_info),
            HardwareRevisionSensor(coordinator, serial, device_info),
            MeshAddressSensor(coordinator, serial, device_info),
        ]

        type_specific = _build_type_specific_entities(coordinator, serial, device_info, support, data)
        entities += type_specific
        advanced_features = _build_advanced_feature_entities(coordinator, serial, device_info, data)
        entities += advanced_features
        # Both consulted later by _async_ensure_sensors_exist() (in
        # __init__.py) -- see that function's own docstring for why it
        # needs to exist at all. sensor_device_infos specifically so it
        # never has to duplicate this function's own via_device/
        # tank_identifier logic just to build one when healing a device
        # whose data wasn't ready yet the first time this ran.
        runtime.sensor_device_infos[serial] = device_info
        runtime.created_sensor_unique_ids.update(e.unique_id for e in type_specific + advanced_features)

    # The tank-level prefix sensor, attached to the synthetic tank device
    # itself, not any one real device -- same condition as via_device
    # above (only for a genuine multi-device tank, matching
    # __init__.py's own _register_tank_device()).
    if tank_identifier is not None and len(device_records) > 1:
        entities.append(MeshPrefixSensor(entry, mlprefix_hex, tank_identifier))
        if registry is not None and pan_id is not None:
            entities.append(GatewayDeviceSensor(entry, pan_id, registry, runtime.coordinators, tank_identifier))

    async_add_entities(entities)
    # Stashed last, not first -- if anything above this point raised,
    # a partially-populated callback with no matching entities would be
    # worse than none at all for _async_ensure_sensors_exist() to find.
    runtime.sensor_add_entities = async_add_entities

