"""End-to-end tests for number.py's PollIntervalNumber -- the
user-configurable poll interval, attached to the synthetic TANK device
(see __init__.py's own tank_device_identifier()), affecting every
device on that tank uniformly."""

import contextlib
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from pytest_homeassistant_custom_component.common import MockConfigEntry

from mobius import Tank, MetadataSnapshot

from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES
from custom_components.mobius.number import MIN_POLL_INTERVAL_SECONDS, MAX_POLL_INTERVAL_SECONDS

PAN_ID = 0x3D0F
PUMP_ADDRESS = "AA:AA:AA:AA:AA:01"
PUMP_SERIAL = "00000000000001"


def _minimal_pump_device():
    device = MagicMock()
    device.serial = PUMP_SERIAL
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 42, "model": "VorTechMP40wG3QD", "manufacturer": "EcoTech Marine",
        "name": "MP40QD Right", "serial": PUMP_SERIAL,
        "primitive_type": "VorTechV1", "error_state": "NoError", "mac_address": None,
    })
    device.get_pump_telemetry = AsyncMock(return_value={
        "speed": 447, "speed_percent": 44.7, "gph": 2272,
        "gph_reliable": True, "minimum_gph": 200, "maximum_gph": 2500,
    })
    device.get_operation_state = AsyncMock()
    device.get_operation_state.return_value.name = "Schedule"
    fake_point = MagicMock()
    fake_point.pump.mode.name = "TidalSwell"
    fake_point.pump.params = {}
    device.get_pump_schedule = AsyncMock(return_value=[fake_point] * 11)
    device.get_current_pump_block = AsyncMock(return_value=fake_point)
    device.get_metadata_batch = AsyncMock(return_value=MetadataSnapshot(
        advanced_features=None, calibration=None, hardware_info={}, firmware_versions={},
        supported_channels=[], error_state=None, epoch=None, local_time=None, tz_offset=None,
    ))
    device.get_own_mesh_address = AsyncMock(
        return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")
    )
    return device


@contextlib.asynccontextmanager
async def _pump_entry(hass):
    from unittest.mock import patch

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, "address": PUMP_ADDRESS}],
        },
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)
    device = _minimal_pump_device()

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, device


@pytest.mark.asyncio
async def test_poll_interval_number_created_on_tank_device_with_default(hass):
    """Attached to the synthetic TANK device (confirmed by the
    entity_id coming from the tank's own name, "Mock Title", not the
    pump's own), defaulting to POLL_INTERVAL's own 30s."""
    async with _pump_entry(hass) as (entry, device):
        state = hass.states.get("number.mock_title_poll_interval")
        assert state is not None
        assert float(state.state) == 30.0
        assert state.attributes["min"] == MIN_POLL_INTERVAL_SECONDS
        assert state.attributes["max"] == MAX_POLL_INTERVAL_SECONDS


@pytest.mark.asyncio
async def test_setting_poll_interval_updates_every_coordinator(hass):
    """The actual point of this whole entity: changing the value
    updates update_interval on every one of this tank's own
    coordinators (MobiusRuntimeData.coordinators), not just its own
    displayed state."""
    async with _pump_entry(hass) as (entry, device):
        await hass.services.async_call(
            "number", "set_value",
            {"entity_id": "number.mock_title_poll_interval", "value": 60},
            blocking=True,
        )

        state = hass.states.get("number.mock_title_poll_interval")
        assert float(state.state) == 60.0

        runtime = entry.runtime_data
        assert len(runtime.coordinators) == 1
        for coordinator in runtime.coordinators.values():
            assert coordinator.update_interval == timedelta(seconds=60)
