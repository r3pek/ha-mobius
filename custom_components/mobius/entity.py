"""Helpers shared by the entity platforms."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Iterator, TypeVar

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MobiusRuntimeData, tank_device_identifier, resolve_tank_device_id
from .const import DOMAIN, CONF_SERIAL, CONF_DEVICES, CONF_MLPREFIX, CONF_PAN_ID
from .coordinator import MobiusDeviceCoordinator, derive_sw_version, derive_hw_version, device_display_name

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


def entry_tank_identifier(entry: ConfigEntry) -> tuple[str, str]:
    """Identifier of the entry's tank device (tank_device_identifier())."""
    return tank_device_identifier(entry.data.get(CONF_MLPREFIX), entry.data.get(CONF_PAN_ID))


def _device_info(serial: str, data: dict, address: str | None = None,
                 sw_version: str | None = None, hw_version: str | None = None,
                 via_device_id: str | None = None) -> DeviceInfo:
    """
    DeviceInfo of a Mobius device. Identified by serial (tank peers have no
    stored address; coordinator.py's _sync_device_registry_info() looks
    devices up the same way). `address`, when known, is only a connections
    hint. `via_device_id` is the registry id of the tank device
    (resolve_tank_device_id()).

    The device's own name is often empty, so the fallback includes the
    serial to tell identical models apart.
    """
    name = device_display_name(serial, data) or (f"Mobius device ({serial})" if serial else "Mobius device")
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        connections={("bluetooth", address)} if address else set(),
        name=name,
        manufacturer=data.get("manufacturer"),
        model=data.get("model"),
        serial_number=serial,
        sw_version=sw_version,
        hw_version=hw_version,
        via_device_id=via_device_id,
    )


def iter_entry_devices(
    hass: HomeAssistant, entry: ConfigEntry,
) -> Iterator[tuple[str, MobiusDeviceCoordinator, DeviceInfo, dict[str, Any]]]:
    """
    (serial, coordinator, device_info, data) for every device of the entry,
    with `data` the coordinator's current data. sw_version/hw_version in
    DeviceInfo are only used when entities are created; the registry is kept
    up to date by coordinator._sync_device_registry_info(). A device without
    a coordinator is skipped with a warning.
    """
    runtime: MobiusRuntimeData = entry.runtime_data
    via_device_id = resolve_tank_device_id(hass, entry.entry_id, entry_tank_identifier(entry))
    for device_record in entry.data.get(CONF_DEVICES, []):
        serial = device_record[CONF_SERIAL]
        coordinator = runtime.coordinators.get(serial)
        if coordinator is None:
            _LOGGER.warning(
                "No coordinator found for device %s in entry %s -- skipping its entities",
                serial, entry.entry_id,
            )
            continue
        data = coordinator.data or {}
        device_info = _device_info(
            serial, data, address=device_record.get(CONF_ADDRESS),
            sw_version=derive_sw_version(data.get("firmware_versions", {})),
            hw_version=derive_hw_version(data.get("hardware_info", {})),
            via_device_id=via_device_id,
        )
        yield serial, coordinator, device_info, data


async def async_run_on_device(
    coordinator: MobiusDeviceCoordinator, action: Callable[[Any], Awaitable[_T]], failure: str,
) -> _T:
    """Runs `action` on the coordinator's connected device (direct or
    relayed). Errors are raised as HomeAssistantError(f"{failure}: {err}").

    The action may have written a setting that is only read every
    STATIC_REFRESH_INTERVAL, so the next poll reads those again."""
    try:
        device = await coordinator.async_get_connected_device()
        result = await action(device)
    except HomeAssistantError:
        raise
    except Exception as err:
        raise HomeAssistantError(f"{failure}: {err}") from err
    finally:
        coordinator.configuration_cache.invalidate_static()
    return result


class MobiusAdvancedFeatureEntity(CoordinatorEntity[MobiusDeviceCoordinator]):
    """
    Base of the Advanced Features switches and selects: one field of
    coordinator.data["advanced_features"] (see python-mobius
    AdvancedFeatures), written with set_advanced_features(). Created only
    for devices that report the field; unique_id is "{serial}_{key}".

    Not created later for a device whose data was still empty at setup
    (see __init__.py's _async_ensure_sensors_exist(), which only covers
    sensors); such a device gets them after a reload.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MobiusDeviceCoordinator, serial: str, key: str,
                 icon: str, device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._key = key
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_translation_key = key
        self._attr_icon = icon
        self._attr_device_info = device_info

    def _current_raw(self):
        return ((self.coordinator.data or {}).get("advanced_features") or {}).get(self._key)

    @property
    def available(self) -> bool:
        return super().available and self._current_raw() is not None

    async def _async_set_raw(self, value) -> None:
        result = await async_run_on_device(
            self.coordinator, lambda device: device.set_advanced_features(**{self._key: value}),
            f"Failed to set {self._key} on {self._serial}",
        )
        error = result.get(self._key)
        if error is not None:
            raise HomeAssistantError(
                f"Device rejected setting {self._key} on {self._serial}: {error}"
            )
        await self.coordinator.async_request_refresh()


def build_advanced_feature_entities(coordinator, serial, device_info, data, classes: dict[str, type]) -> list:
    """One entity per class in `classes` ({advanced_features key: class})
    whose field the device reports."""
    features = data.get("advanced_features") or {}
    return [cls(coordinator, serial, device_info) for key, cls in classes.items() if features.get(key) is not None]
