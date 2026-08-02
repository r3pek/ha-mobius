"""
Tests for MobiusConnectionManager (persistent, serial-resolved connections
shared between both coordinator tiers) and the coordinators built on it.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius.const import DOMAIN, CONF_SERIAL
from custom_components.mobius.coordinator import (
    MobiusConnectionManager,
    MobiusScheduleCoordinator,
    MobiusStatusCoordinator,
)
from mobius import PrimitiveType

PUMP_SERIAL = "76517731952041"
PUMP_ADDRESS = "E4:67:D8:17:84:83"

# Real captured payload for this pump (see python-mobius's own tests).
REAL_PUMP_PAYLOAD = bytes.fromhex("2a0001000000000f3d3736353137373331393532303431")
MOBIUS_COMPANY_ID = 0x0202


def _fake_discovery_info(address: str, payload: bytes):
    """A minimal stand-in for BluetoothServiceInfoBleak -- only the
    attributes MobiusConnectionManager._resolve_current_ble_device()
    actually reads."""
    info = MagicMock()
    info.address = address
    info.manufacturer_data = {MOBIUS_COMPANY_ID: payload}
    return info


def _make_fake_pump_device():
    device = MagicMock()
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 42, "model": "VorTechMP40wG3QD", "manufacturer": "EcoTech Marine",
        "name": "MP40QD Right", "serial": PUMP_SERIAL,
        "primitive_type": "VorTechV1", "error_state": "NoError", "mac_address": None,
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
    device.get_firmware_versions = AsyncMock(return_value={
        "Radio": "4.0.21", "Radio Bootloader": "1.2",
        "Product OS": "2.1.5", "Product Bootloader": "1.0",
    })
    return device


# --------------------------------------------------------------------------
# MobiusConnectionManager._resolve_current_ble_device
# --------------------------------------------------------------------------

async def test_resolve_current_ble_device_matches_by_serial(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)

    other_payload = bytes.fromhex("2a0001000000000f3d3736343935323231303539303139")  # different serial
    discovered = [
        _fake_discovery_info("AA:AA:AA:AA:AA:AA", other_payload),
        _fake_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD),
    ]

    with patch(
        "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
        return_value=discovered,
    ), patch(
        "custom_components.mobius.coordinator.bluetooth.async_ble_device_from_address",
        return_value=MagicMock(address=PUMP_ADDRESS),
    ) as mock_from_address:
        result = await manager._resolve_current_ble_device()

    assert result is not None
    assert result.address == PUMP_ADDRESS
    mock_from_address.assert_called_once_with(hass, PUMP_ADDRESS, connectable=True)


async def test_resolve_current_ble_device_returns_none_when_not_found(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, "NONEXISTENT_SERIAL", semaphore)

    with patch(
        "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
        return_value=[],
    ):
        result = await manager._resolve_current_ble_device()

    assert result is None


# --------------------------------------------------------------------------
# MobiusConnectionManager.ensure_connected / mark_disconnected / disconnect
# --------------------------------------------------------------------------

async def test_ensure_connected_connects_once_and_reuses(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)

    fake_device = MagicMock()
    fake_device.is_connected = True
    fake_device.connect = AsyncMock()

    with patch(
        "custom_components.mobius.coordinator.MobiusDevice", return_value=fake_device
    ) as mock_ctor, patch.object(
        manager, "_resolve_current_ble_device", AsyncMock(return_value=MagicMock())
    ):
        first = await manager.ensure_connected()
        second = await manager.ensure_connected()

    assert first is second  # reused, not reconnected
    fake_device.connect.assert_awaited_once()  # only connected once
    mock_ctor.assert_called_once()


async def test_ensure_connected_reconnects_after_mark_disconnected(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)

    fake_device_1 = MagicMock()
    fake_device_1.is_connected = True
    fake_device_1.connect = AsyncMock()
    fake_device_2 = MagicMock()
    fake_device_2.is_connected = True
    fake_device_2.connect = AsyncMock()

    with patch(
        "custom_components.mobius.coordinator.MobiusDevice",
        side_effect=[fake_device_1, fake_device_2],
    ), patch.object(
        manager, "_resolve_current_ble_device", AsyncMock(return_value=MagicMock())
    ):
        first = await manager.ensure_connected()
        manager.mark_disconnected()
        second = await manager.ensure_connected()

    assert first is fake_device_1
    assert second is fake_device_2
    assert first is not second


async def test_ensure_connected_raises_update_failed_when_not_resolvable(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)

    with patch.object(manager, "_resolve_current_ble_device", AsyncMock(return_value=None)):
        with pytest.raises(UpdateFailed):
            await manager.ensure_connected()


async def test_disconnect_calls_device_disconnect_and_clears_state(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)

    fake_device = MagicMock()
    fake_device.is_connected = True
    fake_device.connect = AsyncMock()
    fake_device.disconnect = AsyncMock()

    with patch("custom_components.mobius.coordinator.MobiusDevice", return_value=fake_device), \
         patch.object(manager, "_resolve_current_ble_device", AsyncMock(return_value=MagicMock())):
        await manager.ensure_connected()
        await manager.disconnect()

    fake_device.disconnect.assert_awaited_once()
    assert manager._device is None


async def test_concurrent_ensure_connected_only_connects_once(hass):
    """Both coordinators calling ensure_connected() around the same time
    should only trigger one actual connect -- the lock exists specifically
    for this."""
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)

    fake_device = MagicMock()
    fake_device.is_connected = False  # not connected until connect() runs

    async def slow_connect():
        await asyncio.sleep(0.05)
        fake_device.is_connected = True

    fake_device.connect = AsyncMock(side_effect=slow_connect)

    with patch("custom_components.mobius.coordinator.MobiusDevice", return_value=fake_device), \
         patch.object(manager, "_resolve_current_ble_device", AsyncMock(return_value=MagicMock())):
        results = await asyncio.gather(manager.ensure_connected(), manager.ensure_connected())

    assert results[0] is results[1]
    fake_device.connect.assert_awaited_once()


# --------------------------------------------------------------------------
# Coordinators using MobiusConnectionManager
# --------------------------------------------------------------------------

async def test_status_coordinator_fetches_pump_telemetry(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)
    entry = MagicMock()
    coordinator = MobiusStatusCoordinator(hass, entry, manager)

    fake_device = _make_fake_pump_device()
    with patch.object(manager, "ensure_connected", AsyncMock(return_value=fake_device)):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data["support"] == "pump"
    assert coordinator.data["telemetry"] == {"speed": 447, "speed_percent": 44.7, "gph": 2272}
    assert coordinator.data["operation_state"] == "Schedule"


async def test_schedule_coordinator_fetches_pump_schedule(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)
    entry = MagicMock()
    coordinator = MobiusScheduleCoordinator(hass, entry, manager)

    fake_device = _make_fake_pump_device()
    with patch.object(manager, "ensure_connected", AsyncMock(return_value=fake_device)):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data["schedule_point_count"] == 11
    assert coordinator.data["current_pump_mode"] == "TidalSwell"


async def test_coordinator_marks_unavailable_when_not_resolvable(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)
    entry = MagicMock()
    coordinator = MobiusStatusCoordinator(hass, entry, manager)

    with patch.object(manager, "ensure_connected", AsyncMock(side_effect=UpdateFailed("not found"))):
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False


async def test_coordinator_retries_once_on_read_failure_then_succeeds(hass):
    """The actual reactive-reconnect behavior: a read fails once (stale
    connection), coordinator marks disconnected and retries within the
    same poll cycle, succeeding the second time."""
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)
    entry = MagicMock()
    coordinator = MobiusStatusCoordinator(hass, entry, manager)

    broken_device = MagicMock()
    broken_device.get_device_info = AsyncMock(side_effect=IOError("connection lost"))
    good_device = _make_fake_pump_device()

    call_count = 0

    async def fake_ensure_connected():
        nonlocal call_count
        call_count += 1
        return broken_device if call_count == 1 else good_device

    with patch.object(manager, "ensure_connected", side_effect=fake_ensure_connected), \
         patch.object(manager, "mark_disconnected") as mock_mark:
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data["support"] == "pump"
    mock_mark.assert_called_once()
    assert call_count == 2


async def test_coordinator_fails_after_reconnect_also_fails(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)
    entry = MagicMock()
    coordinator = MobiusStatusCoordinator(hass, entry, manager)

    broken_device = MagicMock()
    broken_device.get_device_info = AsyncMock(side_effect=IOError("connection lost"))

    with patch.object(manager, "ensure_connected", AsyncMock(return_value=broken_device)), \
         patch.object(manager, "mark_disconnected"):
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False


# --------------------------------------------------------------------------
# Firmware version: re-fetched on the slow tier (not just once at setup --
# real devices got an OTA update mid-development of this integration) and
# synced to the device registry when it changes.
# --------------------------------------------------------------------------

async def test_schedule_coordinator_syncs_device_registry_sw_version_on_change(hass):
    """The actual point of this fix: a firmware version that changes
    between polls must propagate to the device registry, not just be
    captured once and left stale."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ADDRESS: PUMP_ADDRESS, CONF_SERIAL: PUMP_SERIAL},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, PUMP_ADDRESS)},
        sw_version="2.1.5",  # the OLD version
    )
    assert device_entry.sw_version == "2.1.5"

    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)
    coordinator = MobiusScheduleCoordinator(hass, entry, manager)

    fake_device = _make_fake_pump_device()
    fake_device.get_firmware_versions = AsyncMock(return_value={
        "Radio": "4.0.22", "Radio Bootloader": "1.2",
        "Product OS": "2.2.0",  # the NEW version, after the OTA update
        "Product Bootloader": "1.0",
    })

    with patch.object(manager, "ensure_connected", AsyncMock(return_value=fake_device)):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    updated_device = device_registry.async_get(device_entry.id)
    assert updated_device.sw_version == "2.2.0"


async def test_schedule_coordinator_does_not_touch_registry_when_unchanged(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ADDRESS: PUMP_ADDRESS, CONF_SERIAL: PUMP_SERIAL},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, PUMP_ADDRESS)},
        sw_version="2.1.5",
    )

    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)
    coordinator = MobiusScheduleCoordinator(hass, entry, manager)

    fake_device = _make_fake_pump_device()  # same "2.1.5" as already registered

    with patch.object(manager, "ensure_connected", AsyncMock(return_value=fake_device)):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    updated_device = device_registry.async_get(device_entry.id)
    assert updated_device.sw_version == "2.1.5"  # unchanged, as expected
