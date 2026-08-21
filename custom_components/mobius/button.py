"""Button entities for Mobius devices.

The first WRITE this integration sends -- sensor.py's own module
docstring already anticipated this moment ("Control ... will follow the
same pattern once the underlying library grows write support"), now that
python-mobius has reboot().
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MobiusRuntimeData, tank_device_identifier
from .const import CONF_SERIAL, CONF_DEVICES, CONF_MLPREFIX
from .coordinator import MobiusDeviceCoordinator, derive_sw_version, derive_hw_version
from .sensor import _device_info

_LOGGER = logging.getLogger(__name__)


class RebootButton(CoordinatorEntity[MobiusDeviceCoordinator], ButtonEntity):
    """
    Soft-reboots this specific device -- python-mobius's own reboot()
    (Reset attribute, ResetType.Soft), confirmed against real hardware
    directly connected; not yet confirmed via relay (see that
    library's own documentation/09-thread-coap-relay.md).

    Deliberately does NOT gate on coordinator.available/self.available
    the way every sensor does -- see async_press()'s own docstring for
    why a stale/momentarily-unavailable coordinator shouldn't block a
    user from attempting this specifically.
    """

    _attr_has_entity_name = True
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: MobiusDeviceCoordinator, serial: str,
                 device_info: DeviceInfo) -> None:
        super().__init__(coordinator)
        self._serial = serial
        # SERIAL-based, matching every other entity in this integration --
        # see MobiusEntity's own docstring in sensor.py for why.
        self._attr_unique_id = f"{serial}_reboot"
        self._attr_translation_key = "reboot"
        self._attr_device_info = device_info

    async def async_press(self) -> None:
        """
        Deliberately does NOT check self.available/coordinator.data
        first, unlike every sensor in this integration -- this entity's
        own availability isn't tied to whether the LAST regular poll
        happened to succeed. async_get_connected_device() does its own,
        independent connection resolution (gateway lookup, direct or
        relayed) regardless of the coordinator's own current data/
        availability state, so a device that's perfectly reachable but
        whose last scheduled poll happened to time out (a real,
        expected occurrence -- see RELAY_FAILURE_THRESHOLD's own
        docstring in const.py) shouldn't have its reboot button refuse
        to even try.

        Raises HomeAssistantError on any failure (no gateway currently
        available, connection failure, or the device itself rejecting
        the write) -- surfaced directly to the user by Home Assistant's
        own button-press UI, rather than failing silently.
        """
        try:
            device = await self.coordinator.async_get_connected_device()
            await device.reboot()
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                f"Failed to reboot {self._serial}: {err}"
            ) from err


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up one reboot button per device in this config entry -- same
    device_record loop shape as sensor.py's own async_setup_entry(),
    without any of that platform's own type-specific/advanced-feature
    entity logic, since every device gets exactly one of these."""
    runtime: MobiusRuntimeData = entry.runtime_data
    mlprefix_hex = entry.data.get(CONF_MLPREFIX)
    device_records = entry.data.get(CONF_DEVICES, [])

    tank_identifier = tank_device_identifier(mlprefix_hex) if mlprefix_hex is not None else None
    via_device = tank_identifier if tank_identifier is not None and len(device_records) > 1 else None

    entities: list[RebootButton] = []
    for device_record in device_records:
        serial = device_record[CONF_SERIAL]
        coordinator = runtime.coordinators.get(serial)
        if coordinator is None:
            # Same defensive skip as sensor.py's own async_setup_entry()
            # -- shouldn't normally happen, but a missing coordinator for
            # one device shouldn't crash the whole platform's own setup.
            _LOGGER.warning(
                "No coordinator found for device %s in entry %s -- skipping its reboot button",
                serial, entry.entry_id,
            )
            continue

        address = device_record.get(CONF_ADDRESS)
        data = coordinator.data or {}
        sw_version = derive_sw_version(data.get("firmware_versions", {}))
        hw_version = derive_hw_version(data.get("hardware_info", {}))
        device_info = _device_info(
            serial, data, address=address, sw_version=sw_version, hw_version=hw_version,
            via_device=via_device,
        )

        entities.append(RebootButton(coordinator, serial, device_info))

    async_add_entities(entities)
