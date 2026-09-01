"""
Number entity for configuring how often this tank's own devices are
polled -- attached to the synthetic TANK device (see __init__.py's
tank_device_identifier()/_register_tank_device()), not any one real
device, since it affects every device on the tank uniformly.

Unlike sensor.py's own MeshPrefixSensor/GatewayDeviceSensor, this is
created for EVERY entry, including a single, ad-hoc device with no
real mesh prefix -- a lone device still polls at an interval, exactly
the same as a genuine multi-device tank.
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

from . import MobiusRuntimeData, tank_device_identifier
from .const import CONF_MLPREFIX, CONF_PAN_ID, POLL_INTERVAL

_LOGGER = logging.getLogger(__name__)

# Floor: avoid hammering the mesh/relay with back-to-back polls. Ceiling:
# matches MARK_UNAVAILABLE_AFTER's own 5-minute grace window (coordinator.py)
# -- a poll interval longer than that would make "how long to tolerate
# consecutive failures before marking unavailable" meaningless, since a
# single missed poll would already exceed it.
MIN_POLL_INTERVAL_SECONDS = 10
MAX_POLL_INTERVAL_SECONDS = 300


class PollIntervalNumber(RestoreNumber):
    """
    User-configurable poll interval (seconds) for every device on this
    tank. Changing this updates update_interval on every one of this
    tank's own coordinators (MobiusRuntimeData.coordinators) -- takes
    effect from each coordinator's own NEXT scheduled refresh, not
    immediately: this deliberately doesn't reach into
    DataUpdateCoordinator's own private rescheduling internals
    (_schedule_refresh()/_unschedule_refresh()) for what would only be
    a one-time, marginal benefit (skipping a single already-pending
    refresh) at the cost of depending on undocumented behavior that
    could change across Home Assistant versions.

    Persisted via RestoreNumber so a custom interval survives a Home
    Assistant restart -- defaults to POLL_INTERVAL's own value (30s)
    if never changed before.
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
    mlprefix_hex = entry.data.get(CONF_MLPREFIX)
    pan_id = entry.data.get(CONF_PAN_ID)
    tank_identifier = tank_device_identifier(mlprefix_hex, pan_id)
    async_add_entities([PollIntervalNumber(entry, tank_identifier)])
