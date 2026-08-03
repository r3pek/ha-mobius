"""
Tests for custom_components.mobius's top-level async_setup/
async_setup_entry/async_unload_entry -- focused on the fail-fast error
paths (missing serial, missing pan_id) not exercised by test_sensor.py's
full happy-path setup/unload coverage, plus that async_setup() creates
the shared gateway registry.
"""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import ConfigEntryError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius import async_setup, async_setup_entry
from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID
from custom_components.mobius.gateway_registry import GatewayRegistry

PUMP_ADDRESS = "E4:67:D8:17:84:83"
PUMP_SERIAL = "76517731952041"
PAN_ID = 0x3D0F


async def test_async_setup_creates_shared_gateway_registry(hass):
    assert await async_setup(hass, {})
    assert isinstance(hass.data[DOMAIN]["gateway_registry"], GatewayRegistry)


async def test_async_setup_is_idempotent(hass):
    """Called once per Home Assistant startup, but must not blow away an
    already-populated registry if somehow invoked twice (e.g. platform
    reload edge cases)."""
    await async_setup(hass, {})
    registry_first = hass.data[DOMAIN]["gateway_registry"]
    await async_setup(hass, {})
    assert hass.data[DOMAIN]["gateway_registry"] is registry_first


async def test_setup_entry_fails_fast_without_serial(hass):
    """Entries created before serial-based identity was added lack
    CONF_SERIAL entirely -- there's no safe way to connect without it, so
    this must fail clearly rather than attempt an address-only connect."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ADDRESS: PUMP_ADDRESS}, unique_id=PUMP_ADDRESS,
    )
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryError, match="serial"):
        await async_setup_entry(hass, entry)


async def test_setup_entry_fails_fast_without_pan_id(hass):
    """Entries created before pan_id-based gateway grouping was added
    have CONF_SERIAL but not CONF_PAN_ID -- there's no safe way to know
    which group to join without it, so this must also fail clearly rather
    than silently skip gateway grouping."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ADDRESS: PUMP_ADDRESS, CONF_SERIAL: PUMP_SERIAL},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryError, match="pan_id"):
        await async_setup_entry(hass, entry)


async def test_setup_entry_joins_registry_with_current_rssi(hass):
    """Confirms async_setup_entry() actually looks up the device's
    current RSSI (from Home Assistant's own Bluetooth cache) and passes
    it into the registry join -- needed for gateway election to have
    anything to work with."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: PUMP_ADDRESS, CONF_SERIAL: PUMP_SERIAL, CONF_PAN_ID: PAN_ID},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    from unittest.mock import MagicMock

    fake_device = MagicMock()
    fake_device.get_device_info = AsyncMock(return_value={
        "model_raw": 42, "model": "VorTechMP40wG3QD", "manufacturer": "EcoTech Marine",
        "name": "Pump", "serial": PUMP_SERIAL, "primitive_type": "VorTechV1",
        "error_state": "NoError", "mac_address": None,
    })
    fake_device.get_pump_telemetry = AsyncMock(return_value={"speed": 100, "speed_percent": 10.0, "gph": 500})
    fake_device.get_operation_state = AsyncMock()
    fake_device.get_operation_state.return_value.name = "Schedule"
    fake_point = MagicMock()
    fake_point.pump.mode.name = "TidalSwell"
    fake_point.pump.params = {}
    fake_device.get_pump_schedule = AsyncMock(return_value=[fake_point])
    fake_device.get_current_pump_block = AsyncMock(return_value=fake_point)
    fake_device.get_firmware_versions = AsyncMock(return_value={"Product OS": "1.0"})

    with patch(
        "custom_components.mobius._current_rssi", return_value=-42,
    ) as mock_rssi, patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ):
        await async_setup_entry(hass, entry)

    mock_rssi.assert_called_once_with(hass, PUMP_SERIAL)
    registry = hass.data[DOMAIN]["gateway_registry"]
    group = registry.group(PAN_ID)
    assert group is not None
    assert group.members[PUMP_SERIAL].rssi == -42
