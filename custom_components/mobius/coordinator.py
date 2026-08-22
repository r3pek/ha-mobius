"""
Single unified data update coordinator per Mobius device, sharing BLE
connections across devices on the same pan_id (Thread mesh/"tank") via
gateway_registry.GatewayRegistry rather than each device holding its own
direct connection.

One coordinator per device, one ~30s poll cycle, fetching both status
and schedule data together.

## Gateway vs. relayed reads

Each poll cycle, the coordinator checks whether ITS OWN serial is
currently the gateway for its pan_id group (gateway_registry.PanGroup.
gateway_serial). If so, it reads directly over that group's shared
MobiusConnectionManager. If not, it reads through a RelayedMobiusDevice
wrapping that same connection, addressed to its own cached Thread
mesh-local IPv6 (see _resolve_own_mesh_peer() for the on-demand discovery
fallback if that isn't cached yet).

This check happens fresh on every poll cycle, not once at setup -- if
this device's group promotes a different gateway (see
gateway_registry.py's failover logic) between one cycle and the next,
the very next read from this coordinator automatically switches from
direct to relayed (or vice versa, if THIS device gets promoted TO
gateway), with no separate code path needed to handle the transition.

## Failure handling: graceful, not immediate

A single failed read doesn't immediately mark a device unavailable --
the coordinator keeps returning its last-known-good data for up to
MARK_UNAVAILABLE_AFTER (const.py) of consecutive failures before actually
raising UpdateFailed. Only a genuinely sustained outage results in
entities going unavailable. Reconnection itself isn't retried within the
same poll cycle -- a failed
read marks the connection disconnected so the NEXT ~30s cycle reconnects
fresh, and the grace period covers the gap in between; this is simpler
than an immediate in-cycle retry and, given the poll interval is already
short, doesn't meaningfully change how quickly a transient drop recovers.

Separately, when THIS device is the group's gateway, a failed read is
also reported to the registry (record_gateway_failure()) -- after
GATEWAY_FAILURE_THRESHOLD consecutive gateway-read failures (much sooner
than the 5-minute mark-unavailable grace period), the registry promotes
a different member to gateway, since a bad gateway takes its whole group
down with it. Relayed devices' own read failures are NOT reported to the
registry this way -- a single relayed device failing to read through an
otherwise-healthy gateway is much more likely to be specific to that
device/target than to the gateway itself, so only the gateway's own
direct connection health drives promotion.

Reconnection (the gateway's first connect, or after a detected drop)
always resolves the device's CURRENT address by serial number -- BLE
addresses are not guaranteed stable over time, confirmed via real
hardware and via the official app's own Peripheral class (identity is
serial-number-based, never address-based). See python-mobius's
documentation/12-device-identity-and-address-stability.md.

Deliberately does NOT use mobius.find_device_by_serial() for this --
that function runs its own independent BleakScanner, which conflicts
with Home Assistant's own shared Bluetooth manager. Instead,
MobiusConnectionManager reads Home Assistant's own already-running
Bluetooth cache (bluetooth.async_discovered_service_info()), the same
approach config_flow.py's manual-setup step already uses.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from datetime import timedelta
from typing import Any, Optional

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from mobius import (
    MobiusDevice, RelayedMobiusDevice, MeshPeer, PrimitiveType, Model, Tank,
    MOBIUS_COMPANY_IDS, MobiusAdvertisement, parse_manufacturer_data, discover_tank,
    LIGHT_PRIMITIVES, PUMP_PRIMITIVES_VERIFIED, PUMP_PRIMITIVES_EXPERIMENTAL,
    PRIMITIVE_SIZE, extract_short_address,
)

from .const import CONNECT_TIMEOUT, POLL_INTERVAL, MARK_UNAVAILABLE_AFTER, DOMAIN
from .gateway_registry import GatewayRegistry, PanGroup

_LOGGER = logging.getLogger(__name__)


def parsed_advertisement(manufacturer_data: dict) -> Optional[MobiusAdvertisement]:
    """
    Tries every confirmed Mobius company ID (python-mobius's own
    MOBIUS_COMPANY_IDS -- currently EcoTech Marine and
    AquaIllumination) against a BluetoothServiceInfoBleak's own
    manufacturer_data dict, returning the first one that parses. A real
    device advertises under exactly one company ID, never more than
    one at once. Shared here (not duplicated per call site) since
    fixing a real, confirmed bug once in one place -- rather than
    catching every place that used to hardcode a single company ID --
    is the whole point: a device advertising under any OTHER confirmed
    company ID used to come back unparsed everywhere in this
    integration, not just in one spot.
    """
    for company_id in MOBIUS_COMPANY_IDS:
        payload = manufacturer_data.get(company_id)
        if payload:
            parsed = parse_manufacturer_data(payload)
            if parsed is not None:
                return parsed
    return None


def _find_in_bluetooth_cache(hass: HomeAssistant, serial: str):
    """Searches Home Assistant's own Bluetooth cache once for a
    currently-visible advertisement matching serial, by manufacturer
    data. Returns the matching BluetoothServiceInfoBleak, or None if
    serial isn't currently present in the cache at all."""
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        parsed = parsed_advertisement(info.manufacturer_data)
        if parsed and parsed.serial == serial:
            return info
    return None


async def _find_in_bluetooth_cache_with_active_scan_fallback(hass: HomeAssistant, serial: str):
    """Like _find_in_bluetooth_cache() above, but if the fast, cache-
    only lookup comes back empty, requests a one-shot active scan
    sweep and tries once more before giving up entirely -- shared by
    _resolve_current_ble_device() and discover_mesh_address() below,
    both of which need this exact same fallback sequence.

    Confirmed via Home Assistant's own documentation (bluetooth.
    async_request_active_scan) this is exactly the intended use case --
    "for config flow discovery and other one-shot probes" -- not
    something reserved only for initial setup-time discovery. A real,
    confirmed production incident is what this addresses: a device
    (in that case, the group's own gateway) can go missing from Home
    Assistant's own Bluetooth cache for hours at a stretch, well past
    whatever passive-scanning cadence would normally rediscover it.

    Concurrent callers across every coordinator needing to resolve the
    SAME device around the same time -- confirmed: exactly what
    happens when a shared gateway goes missing, since every relayed
    coordinator's own poll also needs to resolve it -- dedupe to a
    single, shared scan window on Home Assistant's own side (per that
    same documentation), so calling this from every coordinator's own
    failed resolution doesn't cause redundant, overlapping active-scan
    storms.
    """
    info = _find_in_bluetooth_cache(hass, serial)
    if info is not None:
        return info

    await bluetooth.async_request_active_scan(hass)
    info = _find_in_bluetooth_cache(hass, serial)
    if info is not None:
        _LOGGER.debug("%s found after requesting a one-shot active scan", serial)
    return info


class MobiusConnectionManager:
    """
    Owns a single persistent MobiusDevice connection for one physical
    device -- the gateway of a pan_id group. Shared (via
    gateway_registry.PanGroup.gateway_connection) by every coordinator
    for devices in that group, not just the gateway's own -- the actual
    point of this class existing is that there's exactly one real BLE
    connection per GROUP, not one per device.
    """

    def __init__(self, hass: HomeAssistant, serial: str, semaphore: asyncio.Semaphore):
        self.hass = hass
        self.serial = serial
        self._semaphore = semaphore
        self._device: Optional[MobiusDevice] = None
        # Prevents multiple coordinators relaying through this same
        # gateway from all trying to reconnect it at the same time.
        self._lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        """Whether this manager currently holds an open connection --
        exposed publicly (rather than callers reaching into the private
        _device attribute directly) specifically so __init__.py's own
        periodic revalidation can tell "genuinely not connected, worth
        checking Home Assistant's own Bluetooth cache for" apart from
        "already connected, so of course it isn't currently advertising
        (a real, healthy device stops advertising while connected) --
        checking the cache for it here would be a false alarm, not a
        real signal of trouble."""
        return self._device is not None and self._device.is_connected

    async def _resolve_current_ble_device(self):
        """
        Finds the BLEDevice currently advertising self.serial, by reading
        Home Assistant's own Bluetooth cache -- NOT by scanning
        independently. See this module's docstring for why.
        """
        info = await _find_in_bluetooth_cache_with_active_scan_fallback(self.hass, self.serial)
        if info is not None:
            _LOGGER.debug("%s currently advertising at %s", self.serial, info.address)
            return bluetooth.async_ble_device_from_address(
                self.hass, info.address, connectable=True
            )
        # The single most useful line for diagnosing "can't connect to
        # any device at all" -- confirms whether this device is even
        # visible to Home Assistant's own Bluetooth stack right now, as
        # distinct from being visible but failing to actually connect
        # (logged separately, in ensure_connected() below). These are
        # different problems with different causes (out of range/
        # powered off vs. a proxy connection-limit/timeout issue), and
        # without this line they're indistinguishable from the logs
        # alone. The connectable-scanner count is included too -- 0
        # means nothing local could ever generate a connectable
        # BLEDevice at all right now, a whole-system problem this
        # integration has no way to fix on its own, as distinct from
        # this one device specifically being out of range/powered off.
        _LOGGER.debug(
            "%s not found in Home Assistant's own Bluetooth cache, even after "
            "requesting an active scan (%d connectable scanner(s) currently registered)",
            self.serial, bluetooth.async_scanner_count(self.hass, connectable=True),
        )
        return None

    def mark_disconnected(self) -> None:
        """
        Forces the next ensure_connected() to reconnect from scratch, even
        if the underlying client's own is_connected might still say True
        momentarily -- used when a read fails unexpectedly, since that's a
        reliable sign something's wrong even if the client object hasn't
        fully updated its own state yet.
        """
        self._device = None

    async def ensure_connected(self) -> MobiusDevice:
        """Returns an already-connected MobiusDevice, reconnecting first
        (via serial, resolved from Home Assistant's own Bluetooth cache)
        if necessary."""
        if self._device is not None and self._device.is_connected:
            return self._device

        async with self._lock:
            # Re-check after acquiring the lock -- another coordinator
            # relaying through this gateway may have already reconnected
            # it while we were waiting.
            if self._device is not None and self._device.is_connected:
                return self._device

            _LOGGER.debug("%s needs a fresh connection -- resolving its current address", self.serial)
            ble_device = await self._resolve_current_ble_device()
            if ble_device is None:
                raise UpdateFailed(
                    f"No device currently advertising serial {self.serial!r} was "
                    "found in Home Assistant's Bluetooth cache"
                )

            # How long THIS specific attempt actually waited for a free
            # connection slot -- the single most direct way to confirm
            # or rule out MAX_CONCURRENT_CONNECTIONS contention as the
            # cause of a device never seeming to get a real connection
            # attempt at all, rather than guessing from the semaphore's
            # own configured size alone.
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


# Priority order for picking a single "main" firmware version to display
# as a device's sw_version.
#
# "Firmware" first, not "Product OS" -- confirmed by directly comparing
# against what the official app itself displays for a real Radion light:
# a "Firmware" label (FirmwareType.LEDClusterMicro/Esp32*Firmware --
# the light's actual LED-driver microcontroller) -- not
# "Product OS" (FirmwareType.MainMicroOS) -- is what the app treats as
# primary.
#
# Falls through this list rather than assuming any one label is always
# present (some devices, or some firmware versions, may not report
# every component) -- the first one found wins, matching "the most
# main-firmware-like thing this device actually reported" rather than
# picking arbitrarily among what's left.
#
# "OS" and "QCA4020Firmware" specifically: kept as a fallback for the
# model=None/unrecognized-model case, where get_firmware_versions()
# itself has no manufacturer to key off and falls back to the raw
# FirmwareType enum name -- these two are what AquaIllumination-brand
# devices (AI Prime/Axis/etc) reported as their raw enum names before
# python-mobius gained proper AquaIllumination-brand labels
# (FIRMWARE_TYPE_LABELS_NON_ETM), which map "OS" -> "Product OS" and
# "QCA4020Firmware" -> "Firmware" -- both already earlier in this same
# list, so a device with a recognized model now matches one of those
# instead and never reaches these two at all. Harmless to keep for the
# unrecognized-model fallback path.
_SW_VERSION_LABEL_PRIORITY = [
    "Firmware", "Product OS", "Radio Firmware", "Radio OS", "Radio",
    "OS", "QCA4020Firmware",
]


def derive_sw_version(firmware_versions: dict) -> Optional[str]:
    """Picks a single version string to show as a device's sw_version,
    from whichever of _SW_VERSION_LABEL_PRIORITY's labels this device
    actually reported -- see that list's comment for why this isn't just
    a single hardcoded lookup."""
    for label in _SW_VERSION_LABEL_PRIORITY:
        version = firmware_versions.get(label)
        if version:
            return version
    return None


def derive_hw_version(hardware_info: dict) -> Optional[str]:
    """
    Picks a single string to show as a device's hw_version, from
    get_hardware_info()'s {HardwareInfo_name: value} dict. "Revision"
    (HardwareInfo.Revision) is the field name most directly matching
    "hardware revision" as a concept -- there's no other reasonable
    candidate among Color/ProductType/RadioType/MotorType/Segments,
    which describe entirely different things.

    Requires python-mobius>=0.3.0: as of that version, "Revision" is
    already a plain int (no confirmed enum meaning exists for it, unlike
    Color/ProductType/RadioType/MotorType, which that version decodes
    into their own confirmed label strings) -- just stringified here, not
    decoded from raw bytes.
    """
    raw = hardware_info.get("Revision")
    if raw is None:
        return None
    return str(raw)


async def _fetch_all(device, minute_of_day_now=None) -> dict[str, Any]:
    """
    The actual read logic, covering both status (identity + live
    telemetry) and schedule (programmed schedule + firmware version) in
    one pass. `device` can be a directly-connected
    MobiusDevice or a RelayedMobiusDevice -- identical either way, since
    RelayedMobiusDevice implements the same interface transparently.
    """
    info = await device.get_device_info()
    primitive_name = info.get("primitive_type")
    try:
        primitive = PrimitiveType[primitive_name] if primitive_name else None
    except KeyError:
        primitive = None

    # Needed earlier than firmware_versions() (its own, pre-existing use
    # below) now that get_pump_telemetry() also takes it -- see that
    # method's own docstring for why: distinguishing the Nero3/Nero5/
    # Nero7 exception from every other pump requires knowing the model,
    # not just the primitive type.
    model_raw = info.get("model_raw")
    try:
        model = Model(model_raw) if model_raw is not None else None
    except ValueError:
        model = None

    if primitive in PUMP_PRIMITIVES_VERIFIED or primitive in PUMP_PRIMITIVES_EXPERIMENTAL:
        info["support"] = "pump" if primitive in PUMP_PRIMITIVES_VERIFIED else "pump (experimental)"
        info["telemetry"] = await device.get_pump_telemetry(model=model, primitive=primitive)
        if info["telemetry"].get("gph") is not None and not info["telemetry"].get("gph_reliable"):
            _LOGGER.debug(
                "%s: gph reading (%s) is not considered reliable for this "
                "device (model=%s, primitive=%s) -- see get_pump_telemetry()'s "
                "own docstring in python-mobius for why; the flow sensor "
                "won't be created for this reason if it's missing",
                info.get("serial"), info["telemetry"]["gph"], model, primitive,
            )
        info["operation_state"] = (await device.get_operation_state()).name
    elif primitive in LIGHT_PRIMITIVES:
        info["support"] = "light"
    else:
        info["support"] = "unsupported"
        size = PRIMITIVE_SIZE.get(primitive) if primitive else None
        info["support_note"] = (
            f"PrimitiveType {primitive_name!r} has no parser implemented "
            f"({size} byte primitive)." if size is not None else
            f"PrimitiveType {primitive_name!r} has no parser implemented."
        )

    # Use Home Assistant's configured timezone, not the container's system
    # time -- these can differ, and the interpolation/block lookup is
    # meaningless if "now" is wrong.
    now = dt_util.now()
    minute_of_day = now.hour * 60 + now.minute

    info["firmware_versions"] = await device.get_firmware_versions(model=model)
    info["hardware_info"] = await device.get_hardware_info()

    if primitive in LIGHT_PRIMITIVES:
        info["channels"] = [c.name for c in await device.get_supported_channels()]
        points = await device.get_light_schedule(which=1)
        info["schedule_point_count"] = len(points)
        current = await device.get_current_light_intensities(which=1, minute_of_day=minute_of_day)
        info["current_intensities"] = {ch.name: v for ch, v in current.items()}
        # A real, confirmed need for this: the app applies a lunar-phase
        # reduction (or not) on top of the raw schedule-interpolated
        # value depending on both the current time (is this the
        # dusk-to-night segment of the schedule) and a per-device toggle
        # (lunar phases enabled) -- a mismatch against what the app
        # itself displays can come from either one being misjudged, and
        # those aren't distinguishable from the final intensity value
        # alone. python-mobius's own get_current_light_intensities()
        # already surfaces exactly this via its own .diagnostics, so
        # just log it -- without this, that information exists for one
        # call and is then gone, forcing a separate, manual diagnostic
        # script every time this needs debugging again.
        _LOGGER.debug("%s light intensity diagnostics: %s", device.serial, current.diagnostics)
        # Confirmed light-only via real device testing AND the app's own
        # UI gating -- returns None for pumps, which is fine (the sensor
        # built on this is only added for light devices anyway).
        info["calibration"] = await device.get_calibration_info()

    elif primitive in PUMP_PRIMITIVES_VERIFIED or primitive in PUMP_PRIMITIVES_EXPERIMENTAL:
        points = await device.get_pump_schedule(which=1)
        info["schedule_point_count"] = len(points)
        block = await device.get_current_pump_block(which=1, minute_of_day=minute_of_day)
        if block:
            info["current_pump_mode"] = block.pump.mode.name
            info["current_pump_params"] = {
                p.name: (v.hex() if isinstance(v, bytes) else (v.name if hasattr(v, "name") else v))
                for p, v in block.pump.params.items()
            }

    # Deliberately unconditional -- NOT gated to LIGHT_PRIMITIVES/
    # PUMP_PRIMITIVES the way most of the above is. The app's own
    # AdvancedFeatures screen covers VorTech (LocalControlEnabled/
    # AutoDimTimeout) and Radion (MaxFanSpeed/FanShutdownEnabled) under
    # one umbrella, gated per-attribute rather than per-device-type (see
    # python-mobius's own get_advanced_features() docstring for the full
    # confirmation) -- so this is called for every device, regardless of
    # what "support" ended up being above, and simply returns None for
    # whichever devices support none of the four underlying attributes.
    features = await device.get_advanced_features()
    info["advanced_features"] = asdict(features) if features else None

    return info


class MobiusDeviceCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """One coordinator per device. See this module's docstring for the
    gateway-vs-relayed and graceful-failure design."""

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
        """Keeps the device registry's sw_version/hw_version/name/model/
        manufacturer in sync with reality -- firmware changes are
        infrequent but real (a real device got an OTA update
        mid-development of this integration), so this needs to actually
        propagate, not just be captured once at setup and left stale
        forever after.

        name/model/manufacturer specifically: a real, confirmed gap the
        entity-healing fix (_async_ensure_sensors_exist(), in __init__.py)
        left behind. sensor.py's own _device_info() falls back to a
        generic "Mobius device (SERIAL)" name when a device's own first
        read at setup didn't have "model" yet -- and since DeviceInfo is
        only ever consulted at entity-CREATION time, not continuously,
        that fallback name was never getting corrected once real data
        actually arrived, even after the entities themselves recovered
        correctly. This is the fix: recomputed and re-synced on every
        successful read, the same as sw_version/hw_version already were.

        Safe to update .name unconditionally whenever it differs --
        confirmed directly against Home Assistant's own DeviceEntry:
        name and name_by_user are separate fields, and a user's own
        rename (via Home Assistant's UI) always goes into the latter,
        which this never touches and which always takes display
        precedence regardless of what .name itself holds.

        Looks the device up by SERIAL, not BLE address -- a real,
        necessary fix, not incidental to this integration's move to
        tank-aware, multi-device config entries: an entry's own data no
        longer has one single top-level address at all (multiple devices
        now share one entry), and a tank peer never has any stored
        address in the first place (see config_flow.py's own
        _async_create_tank_entry() docstring for why). serial is the
        only identifier guaranteed present for every device either way
        -- see python-mobius's own documentation/12-device-identity-and-
        address-stability.md for why it's the right one regardless, not
        just the only available option here. sensor.py's own
        _device_info() must build its own DeviceInfo.identifiers the
        same, serial-based way, or this lookup would never find anything."""
        sw_version = derive_sw_version(data.get("firmware_versions") or {})
        hw_version = derive_hw_version(data.get("hardware_info") or {})
        model = data.get("model")
        manufacturer = data.get("manufacturer")
        # Mirrors _device_info()'s own fallback chain exactly -- serial
        # is always known here (self.serial, never dependent on a
        # successful read), so "model and serial" is the only realistic
        # non-custom-name outcome once real data exists at all.
        custom_name = data.get("name")
        name = custom_name or (f"{model} ({self.serial})" if model else None)
        if not any([sw_version, hw_version, model, manufacturer, name]):
            return
        device_registry = dr.async_get(self.hass)
        device_entry = device_registry.async_get_device(
            identifiers={(DOMAIN, self.serial)}
        )
        if device_entry is None:
            return
        updates = {}
        if sw_version and device_entry.sw_version != sw_version:
            updates["sw_version"] = sw_version
        if hw_version and device_entry.hw_version != hw_version:
            updates["hw_version"] = hw_version
        if model and device_entry.model != model:
            updates["model"] = model
        if manufacturer and device_entry.manufacturer != manufacturer:
            updates["manufacturer"] = manufacturer
        if name and device_entry.name != name:
            updates["name"] = name
        if updates:
            device_registry.async_update_device(device_entry.id, **updates)

    async def async_get_connected_device(self) -> "MobiusDevice":
        """
        Resolves and returns an already-connected MobiusDevice (direct,
        if this coordinator's own device is currently the gateway) or
        RelayedMobiusDevice (otherwise) for THIS coordinator's own
        device -- the same resolution _fetch() itself does for its own
        regular poll cycle, factored out so a one-off action against
        this specific device (e.g. a button press -- see button.py)
        can reuse it without duplicating the gateway-vs-relay decision.

        Deliberately does NOT do any of _fetch()'s own poll-cycle
        bookkeeping (record_gateway_success()/record_relay_success()/
        mark_disconnected() etc, all specific to what a regular,
        recurring read failure should mean for the registry's own
        gateway-health tracking) -- a one-off action failing doesn't
        necessarily mean the same thing a poll failing does (e.g. a
        device can perfectly well reject a specific write while its
        connection is completely healthy), so a caller of this method
        is expected to handle its own failure differently, not treat
        it as a regular poll failure.

        Raises HomeAssistantError if no gateway is currently available
        for this device's own pan_id group at all.
        """
        group = self.registry.group(self.pan_id)
        if group is None or group.gateway_serial is None:
            raise HomeAssistantError(
                f"No gateway currently available for pan_id {self.pan_id:#06x}"
            )
        if group.gateway_serial == self.serial:
            return await group.gateway_connection.ensure_connected()
        gateway_device = await group.gateway_connection.ensure_connected()
        peer = await self._resolve_own_mesh_peer(group)
        return RelayedMobiusDevice(gateway_device, peer)

    async def _fetch(self) -> dict[str, Any]:
        group = self.registry.group(self.pan_id)
        if group is None or group.gateway_serial is None:
            raise UpdateFailed(
                f"No gateway currently available for pan_id {self.pan_id:#06x}"
            )

        is_gateway = group.gateway_serial == self.serial
        _LOGGER.debug(
            "%s polling as %s", self.serial, "gateway" if is_gateway else "relayed",
        )
        try:
            device = await self.async_get_connected_device()
            if is_gateway:
                data = await _fetch_all(device)
                self.registry.record_gateway_success(self.pan_id)
                await self._refresh_mesh_last_seen(group, device)
            else:
                data = await _fetch_all(device)
                self.registry.record_relay_success(self.pan_id, self.serial)
        except Exception as err:
            # A READ can fail even after ensure_connected() reported
            # success (the connection can drop in between) -- this needs
            # to mark the connection disconnected in that case too, not
            # just when ensure_connected() itself raises, or the next
            # poll cycle would keep reusing the same dead connection
            # forever instead of ever actually reconnecting.
            #
            # A relayed device's own failure never touches the shared
            # gateway CONNECTION's state (mark_disconnected()) -- it
            # might be specific to this one device/target, not the
            # gateway connection itself, which the gateway's own
            # coordinator already detects and handles independently on
            # its own cycle. It DOES still count toward a separate,
            # per-target failure tally (record_relay_failure()) though --
            # a real, confirmed production incident showed a gateway can
            # be perfectly healthy for its own reads, and for relaying to
            # OTHER members, while persistently failing to relay to one
            # specific target for 40+ minutes straight. See
            # RELAY_FAILURE_THRESHOLD's own docstring in const.py for why
            # this is a genuinely different symptom from the gateway's
            # own health, kept as a deliberately separate mechanism.
            _LOGGER.debug(
                "%s poll (%s) failed: %s", self.serial,
                "gateway" if is_gateway else "relayed", err,
            )
            if is_gateway:
                group.gateway_connection.mark_disconnected()
                await self.registry.record_gateway_failure(self.pan_id)
            else:
                await self.registry.record_relay_failure(self.pan_id, self.serial)
            raise

        # Every device -- gateway and relayed alike -- picks up its own,
        # most-recently-known value here, on every single poll cycle
        # (not just the gateway's own). Only the gateway itself actually
        # does the extra read above; a relayed device just reads
        # whatever the gateway's own last poll (up to one POLL_INTERVAL
        # old) already wrote to the shared registry -- avoids every
        # device in an N-device tank independently repeating the exact
        # same mesh-wide read every cycle for data that's identical
        # regardless of which device asks for it.
        member = group.members.get(self.serial)
        data["mesh_last_seen_at"] = member.mesh_last_seen_at if member else None
        return data

    async def _refresh_mesh_last_seen(self, group: PanGroup, device: MobiusDevice) -> None:
        """Refreshes every tank member's own "last heard from on the
        mesh" timestamp, from the SAME connection _fetch() just used for
        this device's own regular status read -- one extra attribute
        read, reused for every device in the tank, not one read per
        device. Deliberately non-fatal: this is supplementary
        information layered on top of a status read that already
        succeeded, so a failure here (a device that doesn't support this
        attribute, or a transient read error) must not undo that
        success or fail the whole poll cycle over it -- just leaves
        every member's own value at whatever it was already."""
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
        """Returns a MeshPeer for THIS coordinator's own device, using a
        cached mesh address if available (usually already populated by
        __init__.py's proactive discovery-at-setup step, which runs
        before the first refresh for any relayed device), or discovering
        it on demand via a brief direct connection if not."""
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
        """On-demand fallback for _resolve_own_mesh_peer() -- see
        discover_mesh_address() below for the actual logic, shared with
        __init__.py's proactive discovery-at-setup path."""
        return await discover_mesh_address(self.hass, self.serial, self.registry.semaphore)


async def discover_mesh_address(hass: HomeAssistant, serial: str, semaphore: asyncio.Semaphore) -> Optional[bytes]:
    """
    Connects directly and briefly to whichever device is currently
    advertising `serial` (resolved via Home Assistant's own Bluetooth
    cache, matching MobiusConnectionManager's own resolution) to read its
    own Thread mesh-local address. Returns None (not an exception) if the
    device can't currently be found/reached -- callers that need to
    surface this as a real failure (e.g. MobiusDeviceCoordinator's
    on-demand fallback, when relay genuinely can't proceed without an
    address) do so themselves; __init__.py's proactive call at setup time
    treats a None here as "will retry later" rather than fatal, since the
    coordinator's own on-demand fallback covers it if this attempt
    doesn't pan out.

    `semaphore` MUST be the same shared connection semaphore
    MobiusConnectionManager uses (MAX_CONCURRENT_CONNECTIONS, const.py)
    -- confirmed via real-world testing to be a real bug when this was
    missing: this connects independently of any gateway connection, and
    without sharing the same semaphore, a burst of on-demand discovery
    calls (e.g. several devices needing discovery around the same time,
    such as right after a gateway promotion, when the demoted former
    gateway needs its own mesh address for the first time) could exceed
    the real BLE adapter's actual concurrent-connection capacity even
    while appearing to respect MAX_CONCURRENT_CONNECTIONS, since this
    path wasn't throttled by it at all -- manifesting as the CURRENT
    gateway's own otherwise-healthy connection failing for reasons
    unrelated to the gateway itself, triggering unnecessary failover.
    """
    info = await _find_in_bluetooth_cache_with_active_scan_fallback(hass, serial)
    if info is not None:
        ble_device = bluetooth.async_ble_device_from_address(hass, info.address, connectable=True)
        if ble_device is None:
            # Confirmed present in Home Assistant's OWN advertisement
            # cache (matched by serial, above), but not connectable via
            # async_ble_device_from_address() -- a real, meaningfully
            # different situation from never having been seen at all
            # (the case below): the device is there, but nothing local
            # currently has a connectable path to it (e.g. its only
            # proxy right now is scan-only, or briefly unavailable).
            _LOGGER.debug(
                "%s found in Home Assistant's advertisement cache at %s, but not "
                "currently connectable from here", serial, info.address,
            )
            return None
        try:
            async with semaphore:
                async with MobiusDevice(ble_device, connect_timeout=CONNECT_TIMEOUT) as mdevice:
                    return await mdevice.get_own_mesh_address()
        except Exception as err:
            _LOGGER.debug("Mesh address discovery failed for %s: %s", serial, err)
            return None
    _LOGGER.debug(
        "%s not found in Home Assistant's own Bluetooth cache at all, even after "
        "requesting an active scan (%d connectable scanner(s) currently registered)",
        serial, bluetooth.async_scanner_count(hass, connectable=True),
    )
    return None


async def discover_tank_for_serial(
    hass: HomeAssistant, serial: str, semaphore: asyncio.Semaphore,
) -> Optional[Tank]:
    """
    Connects directly and briefly to whichever device is currently
    advertising `serial` and calls python-mobius's
    mobius.discovery.discover_tank() on it -- the config flow's own way
    of answering "is this device part of a multi-device tank, and if so
    who else is on it" before deciding whether to offer a one-tank or
    one-device confirm. Same resolution/connection pattern as
    discover_mesh_address() above (including sharing the same connection
    semaphore, for the same real-world-confirmed reason that function's
    own docstring explains), just calling a different python-mobius
    function once connected.

    Returns None (not an exception, not an empty Tank) if the device
    can't currently be found/reached at all -- distinguishable from
    discover_tank()'s own Tank(prefix=None, peers=[]) return, which means
    "reached the device fine, but it isn't part of any provisioned
    Thread network" (the genuine "ad-hoc, no tank" case the config flow
    falls back to a single-device confirm for). Callers need to tell
    these apart: this function's None means "try again later, this
    device isn't currently reachable," not "this device has no tank."
    """
    info = await _find_in_bluetooth_cache_with_active_scan_fallback(hass, serial)
    if info is None:
        _LOGGER.debug(
            "%s not found in Home Assistant's own Bluetooth cache at all, even after "
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
        return None
    try:
        async with semaphore:
            async with MobiusDevice(ble_device, connect_timeout=CONNECT_TIMEOUT) as mdevice:
                return await discover_tank(mdevice)
    except Exception as err:
        _LOGGER.debug("Tank discovery failed for %s: %s", serial, err)
        return None
