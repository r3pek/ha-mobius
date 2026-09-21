"""
Poll interval of a tank's devices, on the tank device. Created for every
entry, including a single device.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.number import NumberEntityDescription, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import MobiusRuntimeData
from .const import POLL_INTERVAL
from .entity import entry_tank_identifier

_LOGGER = logging.getLogger(__name__)

# The maximum equals MARK_UNAVAILABLE_AFTER: a longer interval would make a
# device unavailable after a single failed poll.
MIN_POLL_INTERVAL_SECONDS = 10
MAX_POLL_INTERVAL_SECONDS = 300


class PollIntervalNumber(RestoreNumber):
    """
    Poll interval (seconds) of every coordinator of the tank. A change takes
    effect at each coordinator's next scheduled refresh. Restored after a
    restart; defaults to POLL_INTERVAL.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_should_poll = False
    _attr_native_min_value = MIN_POLL_INTERVAL_SECONDS
    _attr_native_max_value = MAX_POLL_INTERVAL_SECONDS
    _attr_native_step = 5
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry, tank_identifier: tuple[str, str]) -> None:
        self.entity_description = NumberEntityDescription(
            key="poll_interval", translation_key="poll_interval", icon="mdi:timer-cog-outline",
        )
        self._attr_unique_id = f"{entry.entry_id}_poll_interval"
        self._attr_device_info = DeviceInfo(identifiers={tank_identifier})
        self._entry = entry
        self._attr_native_value = POLL_INTERVAL.total_seconds()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last_data = await self.async_get_last_number_data()
        if last_data is not None and last_data.native_value is not None:
            self._attr_native_value = last_data.native_value
            self._apply_to_coordinators(self._attr_native_value)

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self._apply_to_coordinators(value)
        self.async_write_ha_state()

    def _apply_to_coordinators(self, seconds: float) -> None:
        runtime: MobiusRuntimeData = self._entry.runtime_data
        new_interval = timedelta(seconds=seconds)
        for coordinator in runtime.coordinators.values():
            coordinator.update_interval = new_interval
        _LOGGER.debug(
            "%s: poll interval set to %.0fs for %d device(s) on this tank "
            "-- takes effect from each one's own next scheduled refresh",
            self._entry.title, seconds, len(runtime.coordinators),
        )


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([PollIntervalNumber(entry, entry_tank_identifier(entry))])
