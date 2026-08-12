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


class MeshLastSeenSensor(MobiusEntity):
    """
    Diagnostic: the last real point in time this specific device was
    confirmed present on the Thread mesh, as far as the tank's gateway
    currently knows -- refreshed on every single poll cycle for every
    device (gateway and relayed alike, every ~POLL_INTERVAL -- see
    coordinator.py's own _fetch()), not a one-time snapshot.

    An earlier version of this integration had a similarly-named sensor
    built on a value whose own meaning turned out to be unconfirmed, and
    was removed entirely rather than risk showing something misleading.
    Reverse engineering the app's own network-troubleshooting screen
    later confirmed what that value actually is: a live, continuously-
    changing duration (time since last heard from on the mesh), not a
    fixed one-time snapshot -- which is exactly why THIS sensor is
    refreshed continuously too, rather than captured once at setup the
    way the earlier, incorrect design was.

    Shows as HA's own "unknown" state (native_value None) until this
    device's tank has had at least one successful gateway poll cycle
    report data for it -- for an ad-hoc device that isn't actually on a
    Thread mesh at all, this simply never populates, which is honest:
    there's genuinely nothing to show, not a bug. Doesn't go fully
    "unavailable" over this alone, though -- that's still governed by
    the base class's own available property (whether this device's own
    regular status poll itself is succeeding), same as every other
    sensor here.
    """

    def __init__(self, coordinator, serial, device_info):
        super().__init__(
            coordinator, serial, "mesh_last_seen",
            SensorEntityDescription(
                key="mesh_last_seen", translation_key="mesh_last_seen",
                device_class=SensorDeviceClass.TIMESTAMP,
            ),
            device_info,
        )
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return self.coordinator.data.get("mesh_last_seen_at") if self.coordinator.data else None

    @property
    def available(self) -> bool:
        # Deliberately NOT tied to coordinator.data (the base class's own
        # available property checks that) -- this sensor's own value
        # comes from the registry, not the poll cycle, so it can be
        # available/unavailable on its own schedule independent of
        # whether the most recent poll itself succeeded.
        return self.native_value is not None


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
            key="gateway_device", translation_key="gateway_device",
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


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up sensors for a Mobius config entry -- one or more devices
    now (see const.py's own module-level docstring for the CONF_DEVICES
    shape this mirrors), not always exactly one the way it used to be."""
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
            MeshLastSeenSensor(coordinator, serial, device_info),
        ]

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
        if registry is not None and pan_id is not None:
            entities.append(GatewayDeviceSensor(entry, pan_id, registry, runtime.coordinators, tank_identifier))

    async_add_entities(entities)

