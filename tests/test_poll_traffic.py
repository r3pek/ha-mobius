"""Less traffic per poll: the pump's operation state from the batch, and
the mesh peer list read by the tank check only."""

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from mobius import OperationState

from custom_components.mobius import MobiusRuntimeData, _async_revalidate_tank
from custom_components.mobius.const import CONF_DEVICES, CONF_PAN_ID, CONF_SERIAL, DOMAIN
from custom_components.mobius.coordinator import MobiusDeviceCoordinator

from tests.test_coordinator import PAN_ID, PUMP_SERIAL, _make_fake_pump_device, _make_registry
from tests.test_init import _make_registry_with_gateway


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


async def test_tank_check_reads_the_mesh_peer_list_once_per_interval(hass):
    registry, group = _make_registry_with_gateway(hass, PAN_ID, PUMP_SERIAL, [])
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]})
    entry.add_to_hass(hass)
    entry.runtime_data = MobiusRuntimeData(coordinators={})
    mesh_read = group.gateway_connection._device.discover_mesh_peers_auto

    await _async_revalidate_tank(hass, entry)
    await _async_revalidate_tank(hass, entry)
    assert mesh_read.await_count == 1

    entry.runtime_data.last_mesh_refresh -= 301
    await _async_revalidate_tank(hass, entry)
    assert mesh_read.await_count == 2
