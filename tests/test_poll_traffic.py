"""Less traffic per poll: rarely-changing attributes on a slower interval
(with a tank button to read everything again), the pump's operation state
from the batch, and the mesh peer list read by the tank check only."""

import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from mobius import OperationState

from custom_components.mobius import MobiusRuntimeData, _async_revalidate_tank
from custom_components.mobius.button import RefreshAllDataButton
from custom_components.mobius.const import (
    CONF_DEVICES, CONF_PAN_ID, CONF_SERIAL, DOMAIN, STATIC_REFRESH_INTERVAL,
)
from custom_components.mobius.coordinator import MobiusDeviceCoordinator
from custom_components.mobius.entity import async_run_on_device

from tests.test_coordinator import PAN_ID, PUMP_SERIAL, _make_fake_pump_device, _make_registry
from tests.test_init import _make_registry_with_gateway


async def test_coordinator_reads_static_attributes_every_static_refresh_interval(hass):
    coordinator = MobiusDeviceCoordinator(hass, MagicMock(), _make_registry(hass), PUMP_SERIAL, PAN_ID)
    assert coordinator.configuration_cache.static_refresh_interval == STATIC_REFRESH_INTERVAL.total_seconds()


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


async def test_a_write_makes_the_next_poll_read_the_static_attributes(hass):
    coordinator = MagicMock()
    coordinator.async_get_connected_device = AsyncMock(return_value=MagicMock())

    await async_run_on_device(coordinator, AsyncMock(return_value="ok"), "failed")

    coordinator.configuration_cache.invalidate_static.assert_called_once()


async def test_refresh_all_data_button_forgets_everything_and_polls_now(hass):
    coordinators = [MagicMock(async_request_refresh=AsyncMock()) for _ in range(2)]
    entry = MagicMock()
    entry.entry_id = "entry"
    entry.runtime_data = MobiusRuntimeData(coordinators={str(i): c for i, c in enumerate(coordinators)})
    entry.runtime_data.last_mesh_refresh = 123.0
    button = RefreshAllDataButton(entry, ("mobius", "tank"))

    await button.async_press()

    for coordinator in coordinators:
        coordinator.forget_cached_data.assert_called_once()
        coordinator.async_request_refresh.assert_awaited_once()
    assert entry.runtime_data.last_mesh_refresh is None


async def test_forget_cached_data_rereads_supported_attributes(hass):
    coordinator = MobiusDeviceCoordinator(hass, MagicMock(), _make_registry(hass), PUMP_SERIAL, PAN_ID)
    coordinator._supported_attribute_ids = {1, 2}
    coordinator.configuration_cache.supported_attributes_crc = 0x1234

    coordinator.forget_cached_data()

    assert coordinator._supported_attribute_ids is None
    assert coordinator.configuration_cache.supported_attributes_crc is None


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


async def test_tank_check_reports_a_failed_mesh_read_and_retries_at_the_next_check(hass, caplog):
    import logging
    caplog.set_level(logging.DEBUG, logger="custom_components.mobius")
    registry, group = _make_registry_with_gateway(hass, PAN_ID, PUMP_SERIAL, [])
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]})
    entry.add_to_hass(hass)
    entry.runtime_data = MobiusRuntimeData(coordinators={})
    mesh_read = group.gateway_connection._device.discover_mesh_peers_auto
    mesh_read.side_effect = IOError("NetworkedThreadDevices: timed out; peer arrays: unsupported")

    await _async_revalidate_tank(hass, entry)
    await _async_revalidate_tank(hass, entry)

    assert mesh_read.await_count == 2  # not held back for MESH_PEER_REFRESH_INTERVAL
    mesh_read.assert_awaited_with(raise_errors=True)
    assert entry.runtime_data.last_mesh_refresh is None
    assert "reading the mesh peer list from gateway" in caplog.text
    assert "NetworkedThreadDevices: timed out" in caplog.text
    assert "revalidated: 0/1" not in caplog.text
