"""The Mobius integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from mobius import Model

from .const import DOMAIN, MAX_CONCURRENT_CONNECTIONS, CONF_SERIAL
from .coordinator import MobiusConnectionManager, MobiusScheduleCoordinator, MobiusStatusCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


@dataclass
class MobiusRuntimeData:
    connection: MobiusConnectionManager
    status: MobiusStatusCoordinator
    schedule: MobiusScheduleCoordinator
    # Fetched once at setup, not re-polled -- firmware essentially never
    # changes during normal operation, unlike everything the coordinators
    # track. {label: version_string}, using the confirmed EcoTech display
    # labels where the model is recognized (see python-mobius's
    # get_firmware_versions()).
    firmware_versions: dict = field(default_factory=dict)


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

    serial = entry.data.get(CONF_SERIAL)
    if serial is None:
        # Entries created before serial-based identity was added won't
        # have this. There's no safe way to connect without it (address
        # alone isn't reliable -- see documentation/
        # 12-device-identity-and-address-stability.md), so ask for a clean
        # re-setup rather than falling back to the old address-only path.
        raise ConfigEntryError(
            f"This Mobius device (address {entry.data.get(CONF_ADDRESS)}) was set up "
            "before serial-based device identity was added and is missing its serial "
            "number. Please remove and re-add it."
        )

    connection = MobiusConnectionManager(hass, serial, semaphore)
    status_coordinator = MobiusStatusCoordinator(hass, entry, connection)
    schedule_coordinator = MobiusScheduleCoordinator(hass, entry, connection)

    await status_coordinator.async_config_entry_first_refresh()
    await schedule_coordinator.async_config_entry_first_refresh()

    firmware_versions: dict = {}
    try:
        device = await connection.ensure_connected()
        model_raw = (status_coordinator.data or {}).get("model_raw")
        try:
            model = Model(model_raw) if model_raw is not None else None
        except ValueError:
            model = None
        firmware_versions = await device.get_firmware_versions(model=model)
    except Exception:  # noqa: BLE001 -- best-effort; missing firmware info
        # shouldn't block setup of everything else.
        pass

    entry.runtime_data = MobiusRuntimeData(
        connection=connection, status=status_coordinator, schedule=schedule_coordinator,
        firmware_versions=firmware_versions,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry, closing its persistent connection."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime: MobiusRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is not None:
        await runtime.connection.disconnect()
    return unload_ok
