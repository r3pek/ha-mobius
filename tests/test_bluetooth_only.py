"""Bluetooth-only devices (Blades): polled over a connection of their own,
opened for each poll, never part of the registry's group, and reached
separately by tank-wide writes."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius import MobiusRuntimeData
from custom_components.mobius.const import CONF_DEVICES, CONF_MLPREFIX, CONF_PAN_ID, CONF_SERIAL, DOMAIN
from custom_components.mobius.coordinator import MobiusDeviceCoordinator, async_tank_broadcast
from custom_components.mobius.gateway_registry import GatewayRegistry

from tests.test_coordinator import _make_fake_light_device
from tests.test_init import MLPREFIX_HEX, PAN_ID, PUMP_SERIAL, _fake_pump_device, _fake_tank_for

BLADE_SERIAL = "BLADE000000001"
BLADE2_SERIAL = "BLADE000000002"


def _registry(hass, slots=1) -> GatewayRegistry:
    return GatewayRegistry(hass, asyncio.Semaphore(slots), election_settle_seconds=0.01)


# --------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------

async def test_poll_connects_and_disconnects_every_time(hass):
    registry = _registry(hass)
    coordinator = MobiusDeviceCoordinator(hass, MagicMock(), registry, BLADE_SERIAL, PAN_ID, bluetooth_only=True)
    device = _make_fake_light_device()
    connection = coordinator.direct_connection

    with patch.object(connection, "ensure_connected", AsyncMock(return_value=device)) as ensure, \
            patch.object(connection, "disconnect", AsyncMock()) as disconnect:
        await coordinator.async_refresh()
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert ensure.await_count == 2
    ensure.assert_awaited_with(acquire_semaphore=False)
    assert disconnect.await_count == 2
    assert registry.group(PAN_ID) is None  # never joined


async def test_polls_of_several_devices_never_overlap(hass):
    registry = _registry(hass)
    active, peak = 0, 0

    def make(serial):
        coordinator = MobiusDeviceCoordinator(hass, MagicMock(), registry, serial, PAN_ID, bluetooth_only=True)

        async def slow_connect(acquire_semaphore=True):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            return _make_fake_light_device()

        async def disconnect():
            nonlocal active
            active -= 1

        coordinator.direct_connection.ensure_connected = slow_connect
        coordinator.direct_connection.disconnect = disconnect
        return coordinator

    first, second = make(BLADE_SERIAL), make(BLADE2_SERIAL)
    await asyncio.gather(first.async_refresh(), second.async_refresh())

    assert first.last_update_success and second.last_update_success
    assert peak == 1


async def test_failed_poll_still_disconnects(hass):
    coordinator = MobiusDeviceCoordinator(hass, MagicMock(), _registry(hass), BLADE_SERIAL, PAN_ID, bluetooth_only=True)
    connection = coordinator.direct_connection

    with patch.object(connection, "ensure_connected", AsyncMock(side_effect=IOError("out of range"))), \
            patch.object(connection, "disconnect", AsyncMock()) as disconnect:
        await coordinator.async_refresh()

    assert not coordinator.last_update_success
    disconnect.assert_awaited_once()


async def test_mesh_member_switches_once_advertised_as_bluetooth_only(hass):
    registry = _registry(hass, slots=2)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-60)
    await registry.join(PAN_ID, BLADE_SERIAL, rssi=-40)
    registry.group(PAN_ID).gateway_serial = PUMP_SERIAL
    coordinator = MobiusDeviceCoordinator(hass, MagicMock(), registry, BLADE_SERIAL, PAN_ID)

    with patch("custom_components.mobius.coordinator.advertised_as_bluetooth_only", return_value=True), \
            patch("custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
                  AsyncMock(return_value=_make_fake_light_device())), \
            patch("custom_components.mobius.coordinator.MobiusConnectionManager.disconnect", AsyncMock()):
        await coordinator.async_refresh()

    assert coordinator.bluetooth_only
    assert coordinator.last_update_success
    assert BLADE_SERIAL not in registry.group(PAN_ID).members


# --------------------------------------------------------------------------
# Tank-wide writes
# --------------------------------------------------------------------------

def _coordinator(serial, bluetooth_only=False, fails=False, is_gateway=False):
    coordinator = MagicMock(serial=serial, pan_id=PAN_ID, bluetooth_only=bluetooth_only)
    device = MagicMock(name=f"device_{serial}")
    device.write = AsyncMock(side_effect=IOError("timed out") if fails else None)
    coordinator.async_get_connected_device = AsyncMock(return_value=device)
    coordinator.registry.group.return_value.gateway_serial = serial if is_gateway else "other"
    return coordinator, device


async def test_broadcast_goes_once_through_the_mesh_and_to_each_bluetooth_only_device():
    pump, pump_device = _coordinator("pump")
    gateway, gateway_device = _coordinator("gateway", is_gateway=True)
    blade1, blade1_device = _coordinator("blade1", bluetooth_only=True)
    blade2, blade2_device = _coordinator("blade2", bluetooth_only=True)

    sent, errors = await async_tank_broadcast([pump, gateway, blade1, blade2], lambda d: d.write())

    assert sent == ["gateway", "blade1", "blade2"]
    assert errors == []
    gateway_device.write.assert_awaited_once()
    pump_device.write.assert_not_awaited()


async def test_broadcast_reports_failures_but_still_reaches_the_rest():
    pump, _ = _coordinator("pump", fails=True)
    other, _ = _coordinator("other")
    blade, _ = _coordinator("blade", bluetooth_only=True, fails=True)

    sent, errors = await async_tank_broadcast([pump, other, blade], lambda d: d.write())

    assert sent == ["other"]
    assert errors == ["blade: timed out"]


async def test_broadcast_reports_the_mesh_when_every_mesh_device_fails():
    pump, _ = _coordinator("pump", fails=True)
    blade, _ = _coordinator("blade", bluetooth_only=True)

    sent, errors = await async_tank_broadcast([pump, blade], lambda d: d.write())

    assert sent == ["blade"]
    assert errors == ["pump: timed out"]


async def test_broadcast_applies_filter():
    pump, pump_device = _coordinator("pump")
    blade, blade_device = _coordinator("blade", bluetooth_only=True)

    sent, _errors = await async_tank_broadcast([pump, blade], lambda d: d.write(), applies=lambda c: c is blade)

    assert sent == ["blade"]
    pump_device.write.assert_not_awaited()


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

async def _setup(hass, serials, bluetooth_only_serials, tank_serials):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX_HEX, CONF_DEVICES: [{CONF_SERIAL: s} for s in serials]},
        unique_id=MLPREFIX_HEX,
    )
    entry.add_to_hass(hass)
    discover = AsyncMock(return_value=_fake_tank_for(*tank_serials) if tank_serials else None)
    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=_fake_pump_device()),
    ), patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.disconnect", AsyncMock(),
    ), patch(
        "custom_components.mobius.advertised_as_bluetooth_only",
        side_effect=lambda hass_, serial: serial in bluetooth_only_serials,
    ), patch(
        "custom_components.mobius.discover_tank_for_serial", discover,
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=None),
    ), patch(
        "custom_components.mobius._current_rssi",
        side_effect=lambda hass_, serial: -30 if serial in bluetooth_only_serials else -70,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry, discover


async def test_mixed_tank_keeps_bluetooth_only_devices_out_of_the_mesh_group(hass):
    entry, discover = await _setup(hass, [PUMP_SERIAL, BLADE_SERIAL], {BLADE_SERIAL}, [PUMP_SERIAL])

    runtime: MobiusRuntimeData = entry.runtime_data
    assert runtime.coordinators[BLADE_SERIAL].bluetooth_only
    assert not runtime.coordinators[PUMP_SERIAL].bluetooth_only
    registry = hass.data[DOMAIN]["gateway_registry"]
    group = registry.group(PAN_ID)
    assert set(group.members) == {PUMP_SERIAL}
    assert group.gateway_serial == PUMP_SERIAL
    # The Blade has the best RSSI but is never probed as the gateway.
    assert [c.args[1] for c in discover.await_args_list] == [PUMP_SERIAL]


async def test_bluetooth_only_tank_sets_up_without_a_mesh_group(hass):
    entry, discover = await _setup(hass, [BLADE_SERIAL, BLADE2_SERIAL], {BLADE_SERIAL, BLADE2_SERIAL}, [])

    assert all(c.bluetooth_only for c in entry.runtime_data.coordinators.values())
    assert hass.data[DOMAIN]["gateway_registry"].group(PAN_ID) is None
    discover.assert_not_awaited()
