"""Button entities: reboot a device, restart every device of a tank."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MobiusRuntimeData
from .coordinator import MobiusDeviceCoordinator, async_tank_broadcast
from .entity import async_run_on_device, entry_tank_identifier, iter_entry_devices


class RebootButton(CoordinatorEntity[MobiusDeviceCoordinator], ButtonEntity):
    """Soft-reboots one device (reboot()).

    Pressing doesn't depend on the coordinator's availability: the
    connection is resolved independently, so a reachable device whose last
    poll failed can still be rebooted.
    """

    _attr_has_entity_name = True
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MobiusDeviceCoordinator, serial: str,
                 device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._serial = serial
        self._attr_unique_id = f"{serial}_reboot"
        self._attr_translation_key = "reboot"
        self._attr_device_info = device_info

    async def async_press(self) -> None:
        """Raises HomeAssistantError when there is no gateway, the connection
        fails or the device rejects the write."""
        await async_run_on_device(
            self.coordinator, lambda device: device.reboot(), f"Failed to reboot {self._serial}",
        )


class RestartAllButton(ButtonEntity):
    """
    Soft-reboots every device of the tank (reboot_all(), the app's "Restart
    all Devices"), from the tank device: one group write through the mesh
    and one to each Bluetooth-only device (async_tank_broadcast()).
    """

    _attr_has_entity_name = True
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, tank_identifier: tuple[str, str]) -> None:
        self._attr_unique_id = f"{entry.entry_id}_restart_all"
        self._attr_translation_key = "restart_all"
        self._attr_device_info = DeviceInfo(identifiers={tank_identifier})
        self._entry = entry

    async def async_press(self) -> None:
        """Raises HomeAssistantError when every device failed to send it."""
        runtime: MobiusRuntimeData = self._entry.runtime_data
        _sent, errors = await async_tank_broadcast(
            runtime.coordinators.values(), lambda device: device.reboot_all(),
        )
        if errors:
            raise HomeAssistantError(f"Failed to restart all devices on: {'; '.join(errors)}")


class RefreshAllDataButton(ButtonEntity):
    """
    Makes every device of the tank read everything again right away: the
    data that is otherwise only read when it changes (scenes, schedules) or
    every STATIC_REFRESH_INTERVAL (identity, firmware, settings), the
    supported attributes, and, at the tank's next check, the mesh peer list.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False
    _attr_icon = "mdi:refresh"

    def __init__(self, entry: ConfigEntry, tank_identifier: tuple[str, str]) -> None:
        self._attr_unique_id = f"{entry.entry_id}_refresh_all"
        self._attr_translation_key = "refresh_all"
        self._attr_device_info = DeviceInfo(identifiers={tank_identifier})
        self._entry = entry

    async def async_press(self) -> None:
        runtime: MobiusRuntimeData = self._entry.runtime_data
        runtime.last_mesh_refresh = None
        for coordinator in runtime.coordinators.values():
            coordinator.forget_cached_data()
        for coordinator in runtime.coordinators.values():
            await coordinator.async_request_refresh()


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    tank_identifier = entry_tank_identifier(entry)
    entities: list[ButtonEntity] = [RestartAllButton(entry, tank_identifier), RefreshAllDataButton(entry, tank_identifier)]
    for serial, coordinator, device_info, _data in iter_entry_devices(hass, entry):
        entities.append(RebootButton(coordinator, serial, device_info))
    async_add_entities(entities)
