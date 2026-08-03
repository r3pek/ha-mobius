"""The Mobius integration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from mobius import MOBIUS_COMPANY_ID, parse_manufacturer_data

from .const import DOMAIN, MAX_CONCURRENT_CONNECTIONS, CONF_SERIAL, CONF_PAN_ID
from .coordinator import MobiusDeviceCoordinator
from .gateway_registry import GatewayRegistry

PLATFORMS: list[Platform] = [Platform.SENSOR]


@dataclass
class MobiusRuntimeData:
    coordinator: MobiusDeviceCoordinator


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up integration-wide shared state (the connection semaphore
    and the gateway registry -- both genuinely global, shared across
    every config entry, not per-entry)."""
    hass.data.setdefault(DOMAIN, {})
    semaphore = hass.data[DOMAIN].setdefault(
        "connection_semaphore", asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)
    )
    hass.data[DOMAIN].setdefault("gateway_registry", GatewayRegistry(hass, semaphore))
    return True


def _current_rssi(hass: HomeAssistant, serial: str) -> int | None:
    """Best-effort RSSI lookup from Home Assistant's own Bluetooth cache
    for whichever address is currently advertising this serial -- used
    only for initial gateway election (see gateway_registry.py); not
    finding one just means this device's join() proceeds without RSSI
    info, matching the registry's own graceful fallback."""
    for info in bluetooth.async_discovered_service_info(hass, connectable=True):
        payload = info.manufacturer_data.get(MOBIUS_COMPANY_ID)
        if not payload:
            continue
        parsed = parse_manufacturer_data(payload)
        if parsed and parsed.serial == serial:
            return info.rssi
    return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Mobius from a config entry (one physical device)."""
    hass.data.setdefault(DOMAIN, {})
    semaphore = hass.data[DOMAIN].setdefault(
        "connection_semaphore", asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)
    )
    registry: GatewayRegistry = hass.data[DOMAIN].setdefault(
        "gateway_registry", GatewayRegistry(hass, semaphore)
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

    pan_id = entry.data.get(CONF_PAN_ID)
    if pan_id is None:
        # Entries created before pan_id-based gateway grouping was added.
        # Same reasoning as the serial check above -- there's no safe way
        # to know which group this device belongs to without it.
        raise ConfigEntryError(
            f"This Mobius device ({serial}) was set up before pan_id-based device "
            "grouping was added and is missing its pan_id. Please remove and re-add it."
        )

    rssi = _current_rssi(hass, serial)
    await registry.join(pan_id, serial, rssi)

    coordinator = MobiusDeviceCoordinator(hass, entry, registry, serial, pan_id)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = MobiusRuntimeData(coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry. If this device was its group's gateway,
    leaving the registry promotes a replacement (and disconnects the old
    gateway connection) automatically -- see gateway_registry.leave()."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    runtime: MobiusRuntimeData | None = getattr(entry, "runtime_data", None)
    if runtime is not None:
        registry: GatewayRegistry | None = hass.data.get(DOMAIN, {}).get("gateway_registry")
        if registry is not None:
            await registry.leave(runtime.coordinator.pan_id, runtime.coordinator.serial)

    return unload_ok
