"""Less traffic per poll: the pump's operation state from the batch."""

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

from mobius import OperationState

from custom_components.mobius.coordinator import MobiusDeviceCoordinator

from tests.test_coordinator import PAN_ID, PUMP_SERIAL, _make_fake_pump_device, _make_registry


async def test_pump_operation_state_comes_from_the_batch(hass):
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    coordinator = MobiusDeviceCoordinator(hass, MagicMock(), registry, PUMP_SERIAL, PAN_ID)
    fake_device = _make_fake_pump_device()
    fake_device.get_full_poll_batch.return_value = dataclasses.replace(
        fake_device.get_full_poll_batch.return_value, operation_state=OperationState(2),
    )
    group = registry.group(PAN_ID)
    with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device)), \
            patch("custom_components.mobius.coordinator.advertised_as_bluetooth_only", return_value=None):
        await coordinator.async_refresh()  # first poll: individual reads
        fake_device.get_operation_state.reset_mock()
        await coordinator.async_refresh()  # full poll

    fake_device.get_operation_state.assert_not_awaited()
    assert coordinator.data["operation_state"] == OperationState(2).name
