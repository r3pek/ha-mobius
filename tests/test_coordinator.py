"""Coordinator tests. MobiusDevice itself is mocked (no real BLE hardware
available in CI), but the canned return values reuse real data captured
from actual hardware earlier in this project (a VorTech pump reporting
TidalSwell/MaxSpeed=300, speed=447, gph=2272 -- see python-mobius's own
tests and this repo's development history), so the shape and plausibility
of the data flowing through the coordinator is grounded in something real,
not invented.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed

from mobius import PrimitiveType

from custom_components.mobius.coordinator import (
    MobiusScheduleCoordinator,
    MobiusStatusCoordinator,
)

PUMP_ADDRESS = "E4:67:D8:17:84:83"


def _make_fake_pump_device():
    """An async-context-manager mock standing in for MobiusDevice, returning
    the same shape of data actually observed from a real VorTech pump."""
    device = MagicMock()
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 42,
        "model": "VorTechMP40wG3QD",
        "manufacturer": "EcoTech Marine",
        "name": "MP40QD Right",
        "serial": "76517731952041",
        "primitive_type": "VorTechV1",
        "error_state": "NoError",
        "mac_address": None,
    })
    device.get_pump_telemetry = AsyncMock(return_value={"speed": 447, "speed_percent": 44.7, "gph": 2272})
    device.get_operation_state = AsyncMock()
    device.get_operation_state.return_value.name = "Schedule"

    device.identify_device_type = AsyncMock(return_value=PrimitiveType.VorTechV1)

    fake_point = MagicMock()
    fake_point.pump.mode.name = "TidalSwell"
    fake_point.pump.params = {}
    device.get_pump_schedule = AsyncMock(return_value=[fake_point] * 11)
    device.get_current_pump_block = AsyncMock(return_value=fake_point)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=device)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


@pytest.fixture
def fake_ble_device():
    with patch(
        "custom_components.mobius.coordinator.bluetooth.async_ble_device_from_address",
        return_value=MagicMock(),
    ) as mock:
        yield mock


async def test_status_coordinator_fetches_pump_telemetry(hass, fake_ble_device):
    semaphore = __import__("asyncio").Semaphore(2)
    entry = MagicMock()
    coordinator = MobiusStatusCoordinator(hass, entry, PUMP_ADDRESS, semaphore)

    fake_device_ctx = _make_fake_pump_device()
    with patch("custom_components.mobius.coordinator.MobiusDevice", return_value=fake_device_ctx):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data["support"] == "pump"
    assert coordinator.data["model"] == "VorTechMP40wG3QD"
    assert coordinator.data["manufacturer"] == "EcoTech Marine"
    assert coordinator.data["telemetry"] == {"speed": 447, "speed_percent": 44.7, "gph": 2272}
    assert coordinator.data["operation_state"] == "Schedule"


async def test_schedule_coordinator_fetches_pump_schedule(hass, fake_ble_device):
    semaphore = __import__("asyncio").Semaphore(2)
    entry = MagicMock()
    coordinator = MobiusScheduleCoordinator(hass, entry, PUMP_ADDRESS, semaphore)

    fake_device_ctx = _make_fake_pump_device()
    with patch("custom_components.mobius.coordinator.MobiusDevice", return_value=fake_device_ctx):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data["schedule_point_count"] == 11
    assert coordinator.data["current_pump_mode"] == "TidalSwell"


async def test_coordinator_marks_unavailable_when_device_not_visible(hass, fake_ble_device):
    fake_ble_device.return_value = None  # simulate device out of BLE range
    semaphore = __import__("asyncio").Semaphore(2)
    entry = MagicMock()
    coordinator = MobiusStatusCoordinator(hass, entry, PUMP_ADDRESS, semaphore)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False


async def test_coordinator_handles_connection_errors_gracefully(hass, fake_ble_device):
    semaphore = __import__("asyncio").Semaphore(2)
    entry = MagicMock()
    coordinator = MobiusStatusCoordinator(hass, entry, PUMP_ADDRESS, semaphore)

    broken_ctx = MagicMock()
    broken_ctx.__aenter__ = AsyncMock(side_effect=IOError("no/invalid response from device"))
    broken_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("custom_components.mobius.coordinator.MobiusDevice", return_value=broken_ctx):
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False
