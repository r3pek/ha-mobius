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
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import MobiusRuntimeData, tank_device_identifier
from .const import DOMAIN, CONF_SERIAL, CONF_DEVICES, CONF_MLPREFIX, CONF_AGE, CONF_DISCOVERED_AT

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
    here. coordinator.py's own _sync_device_registry_versions() must
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
            SensorEntityDescription(key="support", translation_key="support"),
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
            SensorEntityDescription(key="error_state", translation_key="error_state"),
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
            SensorEntityDescription(key="operation_state", translation_key="operation_state"),
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
                key="motor_speed", translation_key="motor_speed",
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


class SchedulePointCountSensor(MobiusEntity):
    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "schedule_point_count",
            SensorEntityDescription(
                key="schedule_point_count", translation_key="schedule_point_count",
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
            SensorEntityDescription(key="current_pump_mode", translation_key="current_pump_mode"),
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
            SensorEntityDescription(key="calibration", translation_key="calibration"),
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
            SensorEntityDescription(key="firmware_version", translation_key="firmware_version"),
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
    directly here, not re-decoded. An earlier version of this class
    manually decoded every field as a raw little-endian integer, which
    was correct only for Revision/Segments and actively wrong for the
    other four once python-mobius started returning real label strings
    instead of raw bytes for them.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "hardware_revision",
            SensorEntityDescription(key="hardware_revision", translation_key="hardware_revision"),
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
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "mesh_address",
            SensorEntityDescription(key="mesh_address", translation_key="mesh_address"),
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
        # the stdlib ipaddress module), not the flat, unrecognizable hex
        # string an earlier version of this sensor showed instead.
        return str(ipaddress.IPv6Address(member.mesh_address))

    @property
    def available(self) -> bool:
        # Deliberately NOT tied to coordinator.data (the base class's own
        # available property checks that) -- this sensor's own value
        # comes from the registry, not the poll cycle, so it can be
        # available/unavailable on its own schedule independent of
        # whether the most recent poll itself succeeded.
        return self.native_value is not None


class DiscoveryAgeSensor(MobiusEntity):
    """
    Diagnostic: this device's own "age" value AS OF THE ORIGINAL
    discover_tank() scan that found it (see const.py's own CONF_AGE
    docstring for the important caveat: confirmed present in the wire
    format, but its exact meaning isn't independently confirmed against
    real hardware, and this is a ONE-TIME snapshot, not continuously
    refreshed -- there's currently no ongoing way to refresh it short of
    a fresh discover_tank() scan). Only ever created for a tank peer
    that actually had one (see async_setup_entry() below) -- never for
    an ad-hoc device, which never successfully calls discover_tank() in
    the first place (see config_flow.py's own module docstring).

    discovered_at (if the tank had one recorded -- see const.py's own
    CONF_DISCOVERED_AT docstring) is exposed as an attribute, giving
    this otherwise-bare, ever-more-stale number a real anchor point:
    "age=14519" alone says nothing about whether that's from five
    minutes or five months ago.
    """

    def __init__(self, coordinator, serial, device_info, age: int, discovered_at: str | None = None):
        super().__init__(
            coordinator, serial, "discovery_age",
            SensorEntityDescription(key="discovery_age", translation_key="discovery_age"),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._age = age
        self._discovered_at = discovered_at

    @property
    def native_value(self):
        return self._age

    @property
    def extra_state_attributes(self):
        if self._discovered_at is None:
            return None
        return {"discovered_at": self._discovered_at}

    @property
    def available(self) -> bool:
        # A static, one-time snapshot -- always available once created,
        # regardless of the coordinator's own current poll health (unlike
        # every other sensor here, which IS tied to live poll data via
        # the base class's own available property).
        return True


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
            key="mesh_prefix", translation_key="mesh_prefix",
        )
        self._attr_unique_id = f"{entry.entry_id}_mesh_prefix"
        self._attr_device_info = DeviceInfo(identifiers={tank_identifier})
        self._mlprefix_hex = mlprefix_hex

    @property
    def native_value(self):
        return self._mlprefix_hex


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up sensors for a Mobius config entry -- one or more devices
    now (see const.py's own module-level docstring for the CONF_DEVICES
    shape this mirrors), not always exactly one the way it used to be."""
    runtime: MobiusRuntimeData = entry.runtime_data
    mlprefix_hex = entry.data.get(CONF_MLPREFIX)
    device_records = entry.data.get(CONF_DEVICES, [])

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
        # anyone who wants it. coordinator._sync_device_registry_versions()
        # also keeps the device registry in sync if either changes after
        # setup, using the same derivation logic.
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

        # Only added if this device actually had a confirmed discovery-
        # time age snapshot -- an ad-hoc device never has one (see
        # DiscoveryAgeSensor's own docstring), no point creating a
        # permanently-unavailable entity for one that never will.
        age = device_record.get(CONF_AGE)
        if age is not None:
            entities.append(DiscoveryAgeSensor(
                coordinator, serial, device_info, age,
                discovered_at=entry.data.get(CONF_DISCOVERED_AT),
            ))

        if support.startswith("pump"):
            entities += [
                OperationStateSensor(coordinator, serial, device_info),
                MotorSpeedSensor(coordinator, serial, device_info),
                FlowRateSensor(coordinator, serial, device_info),
                CurrentPumpModeSensor(coordinator, serial, device_info),
            ]
        elif support == "light":
            channel_names = data.get("channels") or []
            for name in channel_names:
                entities.append(LightChannelIntensitySensor(coordinator, serial, device_info, name))
            # Only added if calibration data was actually present at setup --
            # confirmed via real hardware that not all lights necessarily
            # support this, and there's no point creating a permanently
            # unavailable entity for one that doesn't.
            if data.get("calibration") is not None:
                entities.append(CalibrationSensor(coordinator, serial, device_info))

    # The tank-level prefix sensor, attached to the synthetic tank device
    # itself, not any one real device -- same condition as via_device
    # above (only for a genuine multi-device tank, matching
    # __init__.py's own _register_tank_device()).
    if tank_identifier is not None and len(device_records) > 1:
        entities.append(MeshPrefixSensor(entry, mlprefix_hex, tank_identifier))

    async_add_entities(entities)

