"""
Data update coordinator, one per Mobius device.

Devices with the same pan_id (Thread mesh, "tank") share one BLE
connection, held by the group's gateway (see gateway_registry.py). Each poll
fetches status and schedule data together.

## Gateway vs. relayed reads

Every poll checks whether this device is currently its group's gateway
(PanGroup.gateway_serial). The gateway reads over the shared
MobiusConnectionManager directly; every other device reads through a
RelayedMobiusDevice on that connection, addressed to its cached mesh-local
address (discovered on demand if missing, see _resolve_own_mesh_peer()).
Because this is decided per poll, a gateway change takes effect on the next
poll.

## Failure handling

A failed read keeps returning the last good data until
MARK_UNAVAILABLE_AFTER has passed without a successful read; only then is
UpdateFailed raised. A failed read marks the connection disconnected so the
next poll reconnects.

Gateway read failures are reported to the registry
(record_gateway_failure()), which promotes another member after
GATEWAY_FAILURE_THRESHOLD failures. Relayed read failures are counted per
target (record_relay_failure(), RELAY_FAILURE_THRESHOLD) and never mark the
shared connection disconnected. When the registry decides on an automatic
restart, it runs in the background (async_run_restart()).

Reconnecting resolves the device's current address from its serial using
Home Assistant's Bluetooth cache (not an independent BleakScanner, which
would conflict with Home Assistant's Bluetooth manager). BLE addresses are
not stable; the serial is the device identity (see python-mobius
documentation/12-device-identity-and-address-stability.md).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional, TypeVar

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from mobius import (
    MobiusDevice, RelayedMobiusDevice, MeshPeer, PrimitiveType, Model, Tank,
    parse_advertisement, discover_tank,
    LIGHT_PRIMITIVES, PUMP_PRIMITIVES, PRIMITIVE_SIZE, extract_short_address, C2Attribute,
    PumpParam, enum_or_none, pump_params_to_dict, primitive_type_from_name, support_tier,
)

from .const import CONNECT_TIMEOUT, POLL_INTERVAL, MARK_UNAVAILABLE_AFTER, DOMAIN, BATCH_FAILURE_THRESHOLD
from .gateway_registry import GatewayRegistry, PanGroup, RestartAction

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


def _moon_phase_bucket(current_day: int) -> int:
    """Index (0-5) of the moon phase shown for `current_day` (0-29, days
    since new moon, see lunar_days_into_phase()). The app uses six phases:
    <=0 or >=29 new, 1-7 waxing crescent, 8-13 waxing gibbous, 14-15 full,
    16-21 waning gibbous, 22-28 waning crescent."""
    if current_day <= 0 or current_day >= 29:
        return 0
    if current_day <= 7:
        return 1
    if current_day <= 13:
        return 2
    if current_day <= 15:
        return 3
    if current_day <= 21:
        return 4
    return 5


_MOON_PHASE_ICONS = [
    "mdi:moon-new", "mdi:moon-waxing-crescent", "mdi:moon-waxing-gibbous",
    "mdi:moon-full", "mdi:moon-waning-gibbous", "mdi:moon-waning-crescent",
]
# English names used by the app. Used for an attribute value; the schedule
# card localizes the phase from the icon instead.
_MOON_PHASE_NAMES = [
    "New Moon", "Waxing Crescent", "Waxing Gibbous", "Full Moon", "Waning Gibbous", "Waning Crescent",
]


def moon_phase_icon(current_day: int) -> str:
    return _MOON_PHASE_ICONS[_moon_phase_bucket(current_day)]


def moon_phase_name(current_day: int) -> str:
    return _MOON_PHASE_NAMES[_moon_phase_bucket(current_day)]


def _find_in_bluetooth_cache(hass: HomeAssistant, serial: str):
    """The connectable BluetoothServiceInfoBleak currently advertising
    `serial`, or None."""
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        parsed = parse_advertisement(info.manufacturer_data)
        if parsed and parsed.serial == serial:
            return info
    return None


async def _find_in_bluetooth_cache_with_active_scan_fallback(hass: HomeAssistant, serial: str):
    """_find_in_bluetooth_cache(), requesting a one-shot active scan and
    looking again if the first lookup finds nothing. A device can be missing
    from the cache for a long time with passive scanning only. Home
    Assistant merges concurrent active-scan requests into one scan."""
    info = _find_in_bluetooth_cache(hass, serial)
    if info is not None:
        return info

    await bluetooth.async_request_active_scan(hass)
    info = _find_in_bluetooth_cache(hass, serial)
    if info is not None:
        _LOGGER.debug("%s found after requesting a one-shot active scan", serial)
    return info


async def _resolve_connectable_ble_device(hass: HomeAssistant, serial: str):
    """The connectable BLEDevice currently advertising `serial`, or None.
    Logs why it couldn't be found: not advertising at all (out of range,
    powered off, or no connectable scanner) vs. advertising but not
    connectable from here."""
    info = await _find_in_bluetooth_cache_with_active_scan_fallback(hass, serial)
    if info is None:
        _LOGGER.debug(
            "%s not found in Home Assistant's own Bluetooth cache, even after "
            "requesting an active scan (%d connectable scanner(s) currently registered)",
            serial, bluetooth.async_scanner_count(hass, connectable=True),
        )
        return None
    ble_device = bluetooth.async_ble_device_from_address(hass, info.address, connectable=True)
    if ble_device is None:
        _LOGGER.debug(
            "%s found in Home Assistant's advertisement cache at %s, but not "
            "currently connectable from here", serial, info.address,
        )
    else:
        _LOGGER.debug("%s currently advertising at %s", serial, info.address)
    return ble_device


async def _with_temporary_connection(
    hass: HomeAssistant, serial: str, semaphore: asyncio.Semaphore,
    action: Callable[[MobiusDevice], Awaitable[_T]], what: str,
) -> Optional[_T]:
    """
    Connects briefly to the device advertising `serial`, runs `action` on
    it and disconnects. Returns None if the device can't be reached or the
    action fails.

    `semaphore` must be the shared connection semaphore
    (MAX_CONCURRENT_CONNECTIONS): these connections are separate from the
    gateway connections and would otherwise exceed the adapter's
    connection limit, breaking the gateway's connection.
    """
    ble_device = await _resolve_connectable_ble_device(hass, serial)
    if ble_device is None:
        return None
    try:
        async with semaphore:
            async with MobiusDevice(ble_device, connect_timeout=CONNECT_TIMEOUT) as mdevice:
                return await action(mdevice)
    except Exception as err:
        _LOGGER.debug("%s failed for %s: %s", what, serial, err)
        return None


async def async_run_restart(hass: HomeAssistant, registry: GatewayRegistry, action: RestartAction) -> None:
    """
    Carries out a RestartAction from GatewayRegistry.record_relay_failure().
    Failures are logged, not raised: the escalation continues from the
    failure counts of later polls.

    Step 3 sends reboot_all() through the gateway. Steps 1 and 2 restart
    each device through a brief direct connection, since the gateway can't
    reach it; if that fails, through the gateway.
    """
    group = registry.group(action.pan_id)
    if group is None or group.gateway_connection is None:
        _LOGGER.warning(
            "Automatic restart for pan_id %#06x skipped: the tank has no gateway", action.pan_id,
        )
        return
    if action.tank:
        try:
            gateway_device = await group.gateway_connection.ensure_connected()
            await gateway_device.reboot_all()
            _LOGGER.info("Tank restart sent through %s (pan_id %#06x)", group.gateway_serial, action.pan_id)
        except Exception as err:
            _LOGGER.warning("Tank restart for pan_id %#06x failed: %s", action.pan_id, err)
        return
    for serial in action.serials:
        await _restart_member(hass, registry, group, serial)


async def _restart_member(hass: HomeAssistant, registry: GatewayRegistry, group: PanGroup, serial: str) -> None:
    """reboot() on one member, directly if possible, else through the gateway."""
    restarted = False

    async def reboot(mdevice: MobiusDevice) -> None:
        nonlocal restarted
        await mdevice.reboot()
        # Set before the disconnect: the device may drop the connection
        # while rebooting, which is not a failure.
        restarted = True

    await _with_temporary_connection(hass, serial, registry.semaphore, reboot, f"Restart of {serial}")
    if restarted:
        _LOGGER.info("Restarted %s through a direct connection", serial)
        return

    member = group.members.get(serial)
    if member is not None and member.mesh_address is not None and group.gateway_serial not in (None, serial):
        try:
            gateway_device = await group.gateway_connection.ensure_connected()
            peer = MeshPeer(
                serial=serial, model_raw=0, model=None,
                short_address=extract_short_address(member.mesh_address), address=member.mesh_address,
            )
            await RelayedMobiusDevice(gateway_device, peer).reboot()
            _LOGGER.info("Restarted %s through gateway %s", serial, group.gateway_serial)
            return
        except Exception as err:
            _LOGGER.debug("Restart of %s through gateway %s failed: %s", serial, group.gateway_serial, err)
    _LOGGER.warning("Automatic restart of %s failed: not reachable directly or through the gateway", serial)


class MobiusConnectionManager:
    """The persistent MobiusDevice connection of a pan_id group's gateway,
    shared by every coordinator in the group (PanGroup.gateway_connection)."""

    def __init__(self, hass: HomeAssistant, serial: str, semaphore: asyncio.Semaphore):
        self.hass = hass
        self.serial = serial
        self._semaphore = semaphore
        self._device: Optional[MobiusDevice] = None
        # Coordinators relaying through this gateway must not reconnect it
        # concurrently.
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        """Whether a connection is open. A connected device stops
        advertising, so callers use this to avoid treating a missing
        advertisement as a problem."""
        return self._device is not None and self._device.is_connected

    async def _resolve_current_ble_device(self):
        """The BLEDevice currently advertising self.serial, or None."""
        return await _resolve_connectable_ble_device(self.hass, self.serial)

    def mark_disconnected(self) -> None:
        """Forces the next ensure_connected() to reconnect, for when a read
        failed while the client may still report being connected."""
        self._device = None

    async def ensure_connected(self) -> MobiusDevice:
        """Returns the connected MobiusDevice, reconnecting first (address
        resolved from the serial via Home Assistant's Bluetooth cache) if
        needed."""
        if self.is_connected:
            return self._device

        async with self._lock:
            # Another coordinator may have reconnected while we waited.
            if self.is_connected:
                return self._device

            _LOGGER.debug("%s needs a fresh connection -- resolving its current address", self.serial)
            ble_device = await self._resolve_current_ble_device()
            if ble_device is None:
                raise UpdateFailed(
                    f"No device currently advertising serial {self.serial!r} was "
                    "found in Home Assistant's Bluetooth cache"
                )

            semaphore_wait_start = time.monotonic()
            async with self._semaphore:
                semaphore_wait_seconds = time.monotonic() - semaphore_wait_start
                if semaphore_wait_seconds > 1.0:
                    _LOGGER.debug(
                        "%s waited %.1fs for a free connection slot "
                        "(MAX_CONCURRENT_CONNECTIONS reached)",
                        self.serial, semaphore_wait_seconds,
                    )
                new_device = MobiusDevice(
                    ble_device, serial=self.serial, connect_timeout=CONNECT_TIMEOUT
                )
                connect_start = time.monotonic()
                try:
                    await new_device.connect()
                except Exception as err:
                    _LOGGER.debug(
                        "%s connection attempt failed after %.1fs: %s",
                        self.serial, time.monotonic() - connect_start, err,
                    )
                    raise UpdateFailed(
                        f"Error connecting to {self.serial}: {err}"
                    ) from err
                _LOGGER.debug(
                    "%s connected in %.1fs", self.serial, time.monotonic() - connect_start,
                )

            self._device = new_device
            return self._device

    async def disconnect(self) -> None:
        if self._device is not None:
            _LOGGER.debug("Disconnecting %s", self.serial)
            try:
                await self._device.disconnect()
            except Exception as err:
                _LOGGER.debug("Disconnecting %s raised (ignored, tearing down anyway): %s", self.serial, err)
            self._device = None


# Firmware labels tried, in order, for the device's sw_version. "Firmware"
# (the LED driver on a Radion) is what the app shows as the main version.
# "OS" and "QCA4020Firmware" are the raw FirmwareType names reported when the
# model is unknown (no manufacturer labels apply).
_SW_VERSION_LABEL_PRIORITY = [
    "Firmware", "Product OS", "Radio Firmware", "Radio OS", "Radio",
    "OS", "QCA4020Firmware",
]


def derive_sw_version(firmware_versions: dict) -> Optional[str]:
    """The first version found following _SW_VERSION_LABEL_PRIORITY."""
    for label in _SW_VERSION_LABEL_PRIORITY:
        version = firmware_versions.get(label)
        if version:
            return version
    return None


def derive_hw_version(hardware_info: dict) -> Optional[str]:
    """The HardwareInfo "Revision" value (an int) as a string."""
    raw = hardware_info.get("Revision")
    return None if raw is None else str(raw)


def device_display_name(serial: str, data: dict) -> Optional[str]:
    """The device's own name, else "{model} ({serial})", else None."""
    model = data.get("model")
    return data.get("name") or (f"{model} ({serial})" if model else None)


def used_scenes(data: dict) -> list:
    """The configured scenes in coordinator data, without empty slots."""
    return [s for s in data.get("configured_scenes") or [] if not s.is_empty]


def _format_pump_params(params: dict, group: Optional[PanGroup]) -> dict[str, object]:
    """Pump parameters for an attribute value (pump_params_to_dict()). The
    Sync/EcoSmartBack Master parameter (a mesh address suffix) is replaced by
    "ParentSerial", the serial it resolves to, like websocket_api.py does."""
    result: dict[str, object] = {}
    for name, value in pump_params_to_dict(params).items():
        if name == PumpParam.Master.name and group is not None:
            result["ParentSerial"] = group.serial_for_mesh_suffix(bytes.fromhex(value))
        else:
            result[name] = value
    return result


async def _fetch_all(
    device, minute_of_day_now=None, cached_supported_attribute_ids=None, batch_disabled=False,
    cached_primitive_type=None, cached_model=None, group: Optional[PanGroup] = None,
) -> tuple[dict[str, Any], set[int], bool, Optional[PrimitiveType], Optional[Model]]:
    """
    One poll: identity, telemetry, schedule and metadata. `device` is a
    MobiusDevice or a RelayedMobiusDevice.

    Returns (info, supported_attribute_ids, used_batch, primitive, model).
    Only `info` becomes coordinator.data; the rest is state the coordinator
    keeps between polls and must not appear in diagnostics.

    cached_supported_attribute_ids: the supported attribute set from an
      earlier poll (None to read it). Attribute support doesn't change at
      runtime.
    batch_disabled: passed as force_individual_reads once batching has
      failed BATCH_FAILURE_THRESHOLD times.
    cached_primitive_type / cached_model: known once the first poll
      succeeded (neither changes for a device). With both known, everything
      is read in one get_full_poll_batch() request; the first poll reads
      get_device_info() and then the separate metadata/light/pump reads.
    group: the device's PanGroup, used to resolve a pump's Master parameter
      to a serial. Optional.
    """
    now = dt_util.now()
    minute_of_day = now.hour * 60 + now.minute

    if cached_supported_attribute_ids is not None:
        supported_attribute_ids = supported_attribute_ids_to_cache = cached_supported_attribute_ids
    else:
        try:
            supported = await device.get_supported_attributes()
            supported_attribute_ids = supported_attribute_ids_to_cache = {s.attr_id for s in supported}
        except Exception as e:
            _LOGGER.warning(
                "get_supported_attributes() failed this poll (%s) -- proceeding with an "
                "empty set for this one poll only (most batched data will be missing this "
                "time); will retry fetching it fresh on the next poll rather than caching "
                "this failure permanently",
                e,
            )
            # Use an empty set for this poll only. Returning None (not an
            # empty set) makes the next poll read it again; an empty set
            # would be cached as "supports nothing".
            supported_attribute_ids = set()
            supported_attribute_ids_to_cache = None

    pump_schedule_points = None

    if cached_primitive_type is not None and cached_model is not None:
        primitive = cached_primitive_type
        model = cached_model
        full_poll = await device.get_full_poll_batch(
            primitive=primitive, model=model, which=1, minute_of_day=minute_of_day, now=now,
            supported_attribute_ids=supported_attribute_ids, force_individual_reads=batch_disabled,
        )
        used_batch = full_poll.used_batch
        info = dict(full_poll.device_info)
        info["primitive_type"] = primitive.name
        metadata = full_poll.metadata
        light_poll = full_poll.light_poll
        pump_telemetry_result = full_poll.pump_telemetry
        pump_schedule_points = full_poll.pump_schedule_points
        configured_scenes = full_poll.configured_scenes
        current_scene = full_poll.current_scene
        battery_backup = full_poll.battery_backup
        boosted_battery = full_poll.boosted_battery
    else:
        # First poll: learn primitive type and model.
        info = await device.get_device_info()
        primitive = primitive_type_from_name(info.get("primitive_type"))
        model = enum_or_none(Model, info.get("model_raw"))

        metadata = await device.get_metadata_batch(
            model=model, supported_attribute_ids=supported_attribute_ids,
            force_individual_reads=batch_disabled,
        )
        used_batch = metadata.used_batch
        light_poll = None
        pump_telemetry_result = None
        battery_backup = None
        boosted_battery = None
        if primitive in LIGHT_PRIMITIVES:
            light_poll = await device.get_light_poll_batch(
                which=1, minute_of_day=minute_of_day, now=now,
                supported_attribute_ids=supported_attribute_ids, force_individual_reads=batch_disabled,
            )
            used_batch = used_batch and light_poll.used_batch
        elif primitive in PUMP_PRIMITIVES:
            pump_schedule_points = await device.get_pump_schedule(which=1)
            pump_telemetry_result = await device.get_pump_telemetry(model=model, primitive=primitive)
            battery_backup = await device.get_battery_backup_info()
            boosted_battery = await device.get_boosted_battery_info()

        # Most devices don't support scenes; both reads fail soft.
        try:
            configured_scenes = await device.get_configured_scenes(primitive=primitive)
        except Exception:
            configured_scenes = []
        try:
            current_scene = await device.get_current_scene()
        except Exception:
            current_scene = None

    info["support"] = support_tier(primitive)
    if primitive in PUMP_PRIMITIVES:
        info["telemetry"] = pump_telemetry_result
        if info["telemetry"].get("gph") is not None and not info["telemetry"].get("gph_reliable"):
            _LOGGER.debug(
                "%s: gph reading (%s) is not considered reliable for this "
                "device (model=%s, primitive=%s) -- see get_pump_telemetry()'s "
                "own docstring in python-mobius for why; the flow sensor "
                "won't be created for this reason if it's missing",
                info.get("serial"), info["telemetry"]["gph"], model, primitive,
            )
        info["operation_state"] = (await device.get_operation_state()).name
        # "BatteryBackup" while the pump runs on battery.
        info["operation_mode"] = info["telemetry"].get("operation_mode")
        # None when the pump doesn't support the setting.
        info["battery_backup"] = asdict(battery_backup) if battery_backup else None
        info["boosted_battery"] = asdict(boosted_battery) if boosted_battery else None
    elif primitive not in LIGHT_PRIMITIVES:
        # Only possible on the first poll: once a primitive type is cached it
        # is a light or a pump.
        size = PRIMITIVE_SIZE.get(primitive) if primitive else None
        info["support_note"] = (
            f"PrimitiveType {info.get('primitive_type')!r} has no parser implemented"
            + (f" ({size} byte primitive)." if size is not None else ".")
        )

    # Device clock versus Home Assistant's (seconds; positive = device
    # behind) and its time zone, for _async_check_tank_time() in __init__.py.
    info["clock_drift"] = int(now.timestamp()) - metadata.epoch if metadata.epoch is not None else None
    info["olson_tz"] = metadata.olson_tz
    info["firmware_versions"] = metadata.firmware_versions
    info["hardware_info"] = metadata.hardware_info

    if primitive in LIGHT_PRIMITIVES:
        info["channels"] = [c.name for c in metadata.supported_channels]
        info["schedule_point_count"] = len(light_poll.schedule_points)
        current = light_poll.intensities
        info["current_intensities"] = {ch.name: v for ch, v in current.items()}
        # Which lunar/schedule branch produced the intensities.
        _LOGGER.debug("%s light intensity diagnostics: %s", device.serial, current.diagnostics)
        # The schedule-level dimmer setting (0.0-1.0). Not
        # diagnostics["scalar"], which during the night segment is the lunar
        # reduction.
        info["schedule_intensity"] = light_poll.schedule_intensity
        # LunarPhasesEnabled (the app's "Lunar" chip) and the current moon
        # phase, valid at any time of day (diagnostics["lunar_enabled"] is
        # only set during the night segment). All None when the light
        # doesn't support the attribute; the lunar switch and the card's moon
        # toggle are only offered when it does.
        lunar_supported = C2Attribute.LunarPhasesEnabled in supported_attribute_ids
        info["lunar_supported"] = lunar_supported
        info["lunar_enabled"] = light_poll.lunar_enabled if lunar_supported else None
        info["lunar_phase_day"] = light_poll.lunar_phase_day if lunar_supported else None
        info["moon_phase_icon"] = moon_phase_icon(light_poll.lunar_phase_day) if lunar_supported else None
        info["moon_phase_name"] = moon_phase_name(light_poll.lunar_phase_day) if lunar_supported else None
        # Calibration is a light feature.
        info["calibration"] = metadata.calibration

    elif primitive in PUMP_PRIMITIVES:
        info["schedule_point_count"] = len(pump_schedule_points)
        block = await device.get_current_pump_block(
            which=1, minute_of_day=minute_of_day, points=pump_schedule_points,
        )
        if block:
            info["current_pump_mode"] = block.pump.mode.name
            info["current_pump_params"] = _format_pump_params(block.pump.params, group)

    # Supported per attribute, on any device type; None when none of the
    # four attributes is supported.
    info["advanced_features"] = asdict(metadata.advanced_features) if metadata.advanced_features else None

    info["configured_scenes"] = configured_scenes
    info["current_scene"] = current_scene
    # When the running scene ends, so the remaining time can count down
    # between polls.
    info["current_scene_ends_at"] = (
        (dt_util.utcnow() + timedelta(seconds=current_scene.duration_seconds)).isoformat()
        if current_scene is not None else None
    )

    return info, supported_attribute_ids_to_cache, used_batch, primitive, model


class MobiusDeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """One coordinator per device. See the module docstring."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, registry: GatewayRegistry,
        serial: str, pan_id: int,
    ):
        super().__init__(hass, _LOGGER, name=f"mobius_{serial}", update_interval=POLL_INTERVAL)
        self.config_entry = entry
        self.registry = registry
        self.serial = serial
        self.pan_id = pan_id
        self._last_success: Optional[Any] = None
        # Supported attribute ids, read once (see _fetch_all()).
        self._supported_attribute_ids: Optional[set[int]] = None
        # Batch failure tracking (see _record_batch_result()). Reset when the
        # group's gateway changes, since batch failures can be specific to
        # one relay path.
        self._consecutive_batch_failures = 0
        self._batch_disabled = False
        # PanGroup.generation seen by the previous fetch; None before the
        # first one.
        self._last_seen_gateway_generation: Optional[int] = None
        # Read on the first successful poll; neither changes for a device.
        self._primitive_type: Optional[PrimitiveType] = None
        self._model: Optional[Model] = None
        # Vectra closed-loop mode (see VectraInfo). A setting, but read only
        # once since get_vectra_info() takes three reads; a change is picked
        # up after a reload.
        self._closed_loop: Optional[bool] = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self._fetch()
            self._last_success = dt_util.utcnow()
            self._sync_device_registry_info(data)
            return data
        except Exception as err:
            now = dt_util.utcnow()
            if self._last_success is not None and (now - self._last_success) < MARK_UNAVAILABLE_AFTER:
                _LOGGER.debug(
                    "Read failed for %s (%s), within the %s grace period -- "
                    "keeping last-known-good data instead of going unavailable",
                    self.serial, err, MARK_UNAVAILABLE_AFTER,
                )
                return self.data
            raise UpdateFailed(f"Error communicating with {self.serial}: {err}") from err

    def _sync_device_registry_info(self, data: dict[str, Any]) -> None:
        """
        Updates the device registry's sw_version, hw_version, name, model
        and manufacturer after every successful read, so firmware updates
        and a placeholder name set before the first read are corrected.
        A user-set name is stored in name_by_user and is not affected.

        The device is looked up by serial, like sensor.py's _device_info()
        builds its identifiers.
        """
        sw_version = derive_sw_version(data.get("firmware_versions") or {})
        hw_version = derive_hw_version(data.get("hardware_info") or {})
        model = data.get("model")
        manufacturer = data.get("manufacturer")
        name = device_display_name(self.serial, data)
        if not any([sw_version, hw_version, model, manufacturer, name]):
            return
        device_registry = dr.async_get(self.hass)
        device_entry = device_registry.async_get_device_by_identifier(
            (DOMAIN, self.serial), self.config_entry.entry_id,
        )
        if device_entry is None:
            return
        wanted = {
            "sw_version": sw_version, "hw_version": hw_version, "model": model,
            "manufacturer": manufacturer, "name": name,
        }
        updates = {k: v for k, v in wanted.items() if v and getattr(device_entry, k) != v}
        if updates:
            device_registry.async_update_device(device_entry.id, **updates)

    async def async_get_connected_device(self) -> "MobiusDevice":
        """
        A connected MobiusDevice for this device: the gateway connection if
        this device is the gateway, else a RelayedMobiusDevice through it.
        For one-off actions (buttons, services); unlike _fetch(), failures
        are not recorded against the gateway.

        Raises HomeAssistantError if the group has no gateway.
        """
        group = self.registry.group(self.pan_id)
        if group is None or group.gateway_serial is None:
            raise HomeAssistantError(
                f"No gateway currently available for pan_id {self.pan_id:#06x}"
            )
        gateway_device = await group.gateway_connection.ensure_connected()
        if group.gateway_serial == self.serial:
            return gateway_device
        peer = await self._resolve_own_mesh_peer(group)
        return RelayedMobiusDevice(gateway_device, peer)

    def _record_batch_result(self, used_batch: bool) -> None:
        """
        Counts consecutive polls whose batched read fell back to individual
        reads, and disables batching after BATCH_FAILURE_THRESHOLD of them.
        Once disabled nothing is counted: every poll then reports
        used_batch=False by design.
        """
        if self._batch_disabled:
            return
        _LOGGER.debug("%s: metadata fetch used_batch=%s", self.serial, used_batch)
        if used_batch:
            if self._consecutive_batch_failures > 0:
                _LOGGER.debug(
                    "%s: batched metadata read succeeded again after %d consecutive "
                    "failure(s) -- resetting the counter",
                    self.serial, self._consecutive_batch_failures,
                )
            self._consecutive_batch_failures = 0
            return

        self._consecutive_batch_failures += 1
        _LOGGER.debug(
            "%s: batched metadata read failed this poll (the individual-reads "
            "fallback was used instead, successfully) -- %d/%d consecutive",
            self.serial, self._consecutive_batch_failures, BATCH_FAILURE_THRESHOLD,
        )
        if self._consecutive_batch_failures >= BATCH_FAILURE_THRESHOLD:
            self._batch_disabled = True
            _LOGGER.debug(
                "%s: disabling batched metadata reads after %d consecutive failures -- "
                "using individual reads for every future poll on this device instead",
                self.serial, self._consecutive_batch_failures,
            )

    @property
    def supported_attribute_names(self) -> Optional[list[str]]:
        """
        The supported attributes as sorted C2Attribute names
        ("unknown(<id>)" for ids the enum doesn't know), for display as an
        entity attribute. None until the first successful poll.
        """
        if self._supported_attribute_ids is None:
            return None
        names = []
        for attr_id in sorted(self._supported_attribute_ids):
            attr = enum_or_none(C2Attribute, attr_id)
            names.append(attr.name if attr is not None else f"unknown({attr_id})")
        return names

    async def _fetch(self) -> dict[str, Any]:
        group = self.registry.group(self.pan_id)
        if group is None or group.gateway_serial is None:
            raise UpdateFailed(
                f"No gateway currently available for pan_id {self.pan_id:#06x}"
            )

        is_gateway = group.gateway_serial == self.serial
        # The gateway state this fetch acts on. A read through a torn-down
        # connection only fails at its timeout, possibly after another
        # gateway was promoted; such a failure is not recorded against the
        # new gateway (see PanGroup.generation).
        expected_generation = group.generation
        if self._last_seen_gateway_generation is not None and expected_generation != self._last_seen_gateway_generation:
            if self._batch_disabled or self._consecutive_batch_failures > 0:
                _LOGGER.debug(
                    "%s: gateway generation changed (%d -> %d) -- resetting batch-disabled "
                    "state (was disabled=%s, %d consecutive failure(s)) to give batching a "
                    "fresh chance under the new gateway",
                    self.serial, self._last_seen_gateway_generation, expected_generation,
                    self._batch_disabled, self._consecutive_batch_failures,
                )
            self._batch_disabled = False
            self._consecutive_batch_failures = 0
        self._last_seen_gateway_generation = expected_generation
        _LOGGER.debug(
            "%s polling as %s", self.serial, "gateway" if is_gateway else "relayed",
        )
        try:
            device = await self.async_get_connected_device()
            data, self._supported_attribute_ids, used_batch, self._primitive_type, self._model = await _fetch_all(
                device, cached_supported_attribute_ids=self._supported_attribute_ids,
                batch_disabled=self._batch_disabled,
                cached_primitive_type=self._primitive_type, cached_model=self._model, group=group,
            )
            self._record_batch_result(used_batch)
            self.registry.update_radio_type(
                self.pan_id, self.serial, (data.get("hardware_info") or {}).get("RadioType"),
            )
            if is_gateway:
                self.registry.record_gateway_success(self.pan_id)
                await self._refresh_mesh_last_seen(group, device)
            else:
                self.registry.record_relay_success(self.pan_id, self.serial)

            if self._primitive_type == PrimitiveType.VectraV1 and self._closed_loop is None:
                vectra_info = await device.get_vectra_info()  # None when unavailable
                if vectra_info is not None:
                    self._closed_loop = vectra_info.closed_loop
            data["closed_loop"] = self._closed_loop
        except Exception as err:
            _LOGGER.debug(
                "%s poll (%s) failed: %s", self.serial,
                "gateway" if is_gateway else "relayed", err,
            )
            if group.generation != expected_generation:
                # The gateway changed while this fetch was in flight: the
                # failure says nothing about the current gateway, and
                # group.gateway_connection is now a different connection, so
                # neither is touched.
                _LOGGER.debug(
                    "%s's own fetch started under generation %d, but pan_id %#06x is "
                    "now on generation %d -- not recording this failure against the "
                    "current gateway.",
                    self.serial, expected_generation, self.pan_id, group.generation,
                )
            elif is_gateway:
                # The connection may have dropped after ensure_connected()
                # succeeded; force a reconnect on the next poll.
                group.gateway_connection.mark_disconnected()
                await self.registry.record_gateway_failure(self.pan_id, expected_generation)
            else:
                # May be specific to this target, so the shared connection is
                # left alone; the failure counts towards
                # RELAY_FAILURE_THRESHOLD for this target.
                action = await self.registry.record_relay_failure(self.pan_id, self.serial, expected_generation)
                if action is not None:
                    self.hass.async_create_background_task(
                        async_run_restart(self.hass, self.registry, action),
                        f"mobius automatic restart for pan_id {self.pan_id:#06x}",
                    )
            raise

        # Written to the registry by the gateway's poll
        # (_refresh_mesh_last_seen()), so it is up to one poll old for
        # relayed devices.
        member = group.members.get(self.serial)
        data["mesh_last_seen_at"] = member.mesh_last_seen_at if member else None
        return data

    async def _refresh_mesh_last_seen(self, group: PanGroup, device: MobiusDevice) -> None:
        """Updates every member's "last heard on the mesh" time from one
        NetworkedThreadDevices read on the gateway. Failures are ignored and
        leave the previous values."""
        try:
            peers = await device.discover_networked_thread_devices()
        except Exception as err:
            _LOGGER.debug(
                "Could not refresh mesh last-seen data via gateway %s this cycle: %s",
                self.serial, err,
            )
            return
        now = dt_util.utcnow()
        for peer in peers:
            if peer.age is None:
                continue
            self.registry.update_mesh_last_seen(
                self.pan_id, peer.serial, now - timedelta(milliseconds=peer.age),
            )

    async def _resolve_own_mesh_peer(self, group: PanGroup) -> MeshPeer:
        """A MeshPeer for this device, from the cached mesh address (usually
        discovered at setup by __init__.py) or, failing that, discovered now
        through a brief direct connection."""
        member = group.members.get(self.serial)
        address = member.mesh_address if member else None

        if address is None:
            _LOGGER.debug(
                "%s has no cached mesh address yet -- discovering it on demand "
                "via a brief direct connection", self.serial,
            )
            address = await self._discover_own_mesh_address()
            if address is None:
                raise UpdateFailed(
                    f"Could not determine Thread mesh address for {self.serial} "
                    "(needed to relay through the group's gateway)"
                )
            self.registry.update_mesh_address(self.pan_id, self.serial, address)

        return MeshPeer(
            serial=self.serial, model_raw=0, model=None,
            short_address=extract_short_address(address), address=address,
        )

    async def _discover_own_mesh_address(self) -> Optional[bytes]:
        return await discover_mesh_address(self.hass, self.serial, self.registry.semaphore)


async def discover_mesh_address(hass: HomeAssistant, serial: str, semaphore: asyncio.Semaphore) -> Optional[bytes]:
    """The device's own mesh-local address, read through a brief direct
    connection. None if the device can't be reached (callers decide whether
    that is fatal). See _with_temporary_connection() for `semaphore`."""
    return await _with_temporary_connection(
        hass, serial, semaphore, lambda mdevice: mdevice.get_own_mesh_address(),
        "Mesh address discovery",
    )


async def discover_tank_for_serial(
    hass: HomeAssistant, serial: str, semaphore: asyncio.Semaphore,
) -> Optional[Tank]:
    """
    discover_tank() on the device advertising `serial`, through a brief
    direct connection (used by the config flow).

    None means the device couldn't be reached (try again later). A
    reachable device that isn't part of a Thread network returns
    Tank(prefix=None, peers=[]) instead.
    """
    return await _with_temporary_connection(hass, serial, semaphore, discover_tank, "Tank discovery")
