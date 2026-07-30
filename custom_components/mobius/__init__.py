"""The Mobius integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN, MAX_CONCURRENT_CONNECTIONS
from .coordinator import MobiusScheduleCoordinator, MobiusStatusCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


@dataclass
class MobiusRuntimeData:
    status: MobiusStatusCoordinator
    schedule: MobiusScheduleCoordinator


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up integration-wide shared state (the connection semaphore)."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(
        "connection_semaphore", asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mobius from a config entry (one physical device)."""
    hass.data.setdefault(DOMAIN, {})
    semaphore = hass.data[DOMAIN].setdefault(
        "connection_semaphore", asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)
    )
    address = entry.data[CONF_ADDRESS]

    status_coordinator = MobiusStatusCoordinator(hass, entry, address, semaphore)
    schedule_coordinator = MobiusScheduleCoordinator(hass, entry, address, semaphore)

    await status_coordinator.async_config_entry_first_refresh()
    await schedule_coordinator.async_config_entry_first_refresh()

    entry.runtime_data = MobiusRuntimeData(status=status_coordinator, schedule=schedule_coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
