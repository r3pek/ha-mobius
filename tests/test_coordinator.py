"""
Tests for MobiusConnectionManager (persistent, serial-resolved
connections, now shared per pan_id group via gateway_registry rather
than per-device) and MobiusDeviceCoordinator built on top of it -- one
coordinator per device, one merged status+schedule read per cycle,
gateway-vs-relayed dispatch, and graceful (grace-period, not immediate)
failure handling.
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius.const import DOMAIN, CONF_SERIAL, MARK_UNAVAILABLE_AFTER
from custom_components.mobius.coordinator import (
    MobiusConnectionManager, MobiusDeviceCoordinator, derive_sw_version, derive_hw_version,
    discover_mesh_address, discover_tank_for_serial,
)
from custom_components.mobius.gateway_registry import GatewayRegistry
from mobius import PrimitiveType, Tank, MeshPeer, Model

PUMP_SERIAL = "00000000000001"
PUMP_ADDRESS = "AA:AA:AA:AA:AA:01"
LIGHT_SERIAL = "FAKESERIAL0001"
PAN_ID = 0x3D0F

# Payload shaped after a real captured advertisement for this pump (see
# python-mobius's own tests) -- serial replaced with an obviously-fake
# placeholder of the same byte length.
REAL_PUMP_PAYLOAD = bytes.fromhex("2a0001000000000f3d3030303030303030303030303031")
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
    device.get_hardware_info = AsyncMock(return_value={"Revision": 2})
    return device


def _make_registry(hass) -> GatewayRegistry:
    semaphore = asyncio.Semaphore(2)
    return GatewayRegistry(hass, semaphore, election_settle_seconds=0.01)


# --------------------------------------------------------------------------
# MobiusConnectionManager._resolve_current_ble_device
# --------------------------------------------------------------------------

async def test_resolve_current_ble_device_matches_by_serial(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)

    other_payload = bytes.fromhex("2a0001000000000f3d3030303030303030303030303032")  # different serial
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
    """Multiple coordinators relaying through the same gateway calling
    ensure_connected() around the same time should only trigger one
    actual connect -- the lock exists specifically for this."""
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
# MobiusDeviceCoordinator -- gateway path (this device IS the gateway)
# --------------------------------------------------------------------------

async def test_gateway_coordinator_fetches_merged_status_and_schedule(hass):
    """The actual point of the single-tier merge: one read cycle produces
    BOTH status-shaped data (telemetry, operation_state) AND
    schedule-shaped data (schedule_point_count, current_pump_mode) --
    previously two separate coordinator tiers."""
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    fake_device = _make_fake_pump_device()
    group = registry.group(PAN_ID)
    with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device)):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data["support"] == "pump"
    assert coordinator.data["telemetry"] == {"speed": 447, "speed_percent": 44.7, "gph": 2272}
    assert coordinator.data["operation_state"] == "Schedule"
    assert coordinator.data["schedule_point_count"] == 11
    assert coordinator.data["current_pump_mode"] == "TidalSwell"


async def test_gateway_read_success_resets_registry_failure_counter(hass):
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    registry.group(PAN_ID).consecutive_gateway_failures = 2  # simulate prior failures
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    fake_device = _make_fake_pump_device()
    group = registry.group(PAN_ID)
    with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device)):
        await coordinator.async_refresh()

    assert registry.group(PAN_ID).consecutive_gateway_failures == 0


async def test_gateway_connect_failure_reports_to_registry_and_marks_disconnected(hass):
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    group = registry.group(PAN_ID)
    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(side_effect=UpdateFailed("not found")),
    ), patch.object(group.gateway_connection, "mark_disconnected") as mock_mark:
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert registry.group(PAN_ID).consecutive_gateway_failures == 1
    mock_mark.assert_called_once()


async def test_gateway_read_failure_after_successful_connect_also_reports_and_marks(hass):
    """The actual bug this covers: a read failing AFTER ensure_connected()
    already succeeded must still mark the connection disconnected and
    report a gateway failure -- not just a failure to connect in the
    first place."""
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    broken_device = MagicMock()
    broken_device.get_device_info = AsyncMock(side_effect=IOError("connection lost"))

    group = registry.group(PAN_ID)
    with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=broken_device)), \
         patch.object(group.gateway_connection, "mark_disconnected") as mock_mark:
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert registry.group(PAN_ID).consecutive_gateway_failures == 1
    mock_mark.assert_called_once()


# --------------------------------------------------------------------------
# MobiusDeviceCoordinator -- relayed path (a DIFFERENT device is gateway)
# --------------------------------------------------------------------------

async def test_relayed_coordinator_uses_cached_mesh_address(hass):
    registry = _make_registry(hass)
    await registry.join(PAN_ID, "gateway-serial", rssi=-50)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-80)  # weaker signal -- stays relayed
    address = bytes.fromhex("fd11223344556677000000fffe001234")
    registry.update_mesh_address(PAN_ID, PUMP_SERIAL, address)

    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    fake_gateway_device = MagicMock()
    fake_relayed_device = _make_fake_pump_device()
    group = registry.group(PAN_ID)

    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_gateway_device),
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_relayed_device,
    ) as mock_relayed_ctor, patch.object(
        coordinator, "_discover_own_mesh_address", AsyncMock(),
    ) as mock_discover:
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data["support"] == "pump"
    mock_relayed_ctor.assert_called_once()
    call_args = mock_relayed_ctor.call_args
    assert call_args[0][0] is fake_gateway_device
    assert call_args[0][1].address == address
    mock_discover.assert_not_called()  # cached -- no on-demand discovery needed


async def test_relayed_coordinator_discovers_address_on_demand_when_not_cached(hass):
    registry = _make_registry(hass)
    await registry.join(PAN_ID, "gateway-serial", rssi=-50)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-80)
    # No update_mesh_address() call -- not cached.

    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    address = bytes.fromhex("fd11223344556677000000fffe005678")
    fake_gateway_device = MagicMock()
    fake_relayed_device = _make_fake_pump_device()
    group = registry.group(PAN_ID)

    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_gateway_device),
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_relayed_device,
    ), patch.object(
        coordinator, "_discover_own_mesh_address", AsyncMock(return_value=address),
    ) as mock_discover:
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    mock_discover.assert_called_once()
    # Discovered address is cached for next time.
    assert registry.group(PAN_ID).members[PUMP_SERIAL].mesh_address == address


async def test_relayed_coordinator_fails_cleanly_when_address_undiscoverable(hass):
    registry = _make_registry(hass)
    await registry.join(PAN_ID, "gateway-serial", rssi=-50)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-80)

    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    fake_gateway_device = MagicMock()
    group = registry.group(PAN_ID)

    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_gateway_device),
    ), patch.object(
        coordinator, "_discover_own_mesh_address", AsyncMock(return_value=None),
    ):
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False


async def test_relayed_coordinator_failure_does_not_touch_gateway_connection_state(hass):
    """The actual point of this test: a relayed device's own failure must
    NOT mark the shared gateway connection disconnected or report a
    gateway failure -- only the gateway's own coordinator does that."""
    registry = _make_registry(hass)
    await registry.join(PAN_ID, "gateway-serial", rssi=-50)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-80)
    registry.update_mesh_address(PAN_ID, PUMP_SERIAL, bytes.fromhex("fd11223344556677000000fffe001234"))

    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    fake_gateway_device = MagicMock()
    broken_relayed_device = MagicMock()
    broken_relayed_device.get_device_info = AsyncMock(side_effect=IOError("relay failed"))
    group = registry.group(PAN_ID)

    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_gateway_device),
    ), patch.object(
        group.gateway_connection, "mark_disconnected",
    ) as mock_mark, patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=broken_relayed_device,
    ):
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    mock_mark.assert_not_called()
    assert registry.group(PAN_ID).consecutive_gateway_failures == 0


# --------------------------------------------------------------------------
# No gateway available for the group
# --------------------------------------------------------------------------

async def test_fails_cleanly_when_group_has_no_gateway(hass):
    registry = _make_registry(hass)
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)
    # Deliberately never joined -- registry.group(PAN_ID) is None.

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False


# --------------------------------------------------------------------------
# Graceful (grace-period) failure handling -- avoid unavailable on a
# single/transient failure
# --------------------------------------------------------------------------

async def test_transient_failure_within_grace_period_keeps_last_known_data(hass):
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    fake_device = _make_fake_pump_device()
    group = registry.group(PAN_ID)
    with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device)):
        await coordinator.async_refresh()
    assert coordinator.last_update_success
    first_data = coordinator.data

    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(side_effect=UpdateFailed("transient")),
    ):
        await coordinator.async_refresh()

    # Still "successful" from HA's perspective (entities stay available),
    # serving the last-known-good data rather than going unavailable.
    assert coordinator.last_update_success
    assert coordinator.data is first_data


async def test_failure_past_grace_period_marks_unavailable(hass):
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    fake_device = _make_fake_pump_device()
    group = registry.group(PAN_ID)
    with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device)):
        await coordinator.async_refresh()
    assert coordinator.last_update_success

    # Simulate the grace period having already elapsed.
    coordinator._last_success = dt_util.utcnow() - MARK_UNAVAILABLE_AFTER - timedelta(seconds=1)

    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(side_effect=UpdateFailed("still down")),
    ):
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False


async def test_first_ever_failure_with_no_prior_success_raises_immediately(hass):
    """No last-known-good data exists yet -- nothing to gracefully fall
    back to, so this must fail immediately rather than somehow succeed
    with nothing."""
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    group = registry.group(PAN_ID)
    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(side_effect=UpdateFailed("never connected")),
    ):
        await coordinator.async_refresh()

    assert coordinator.last_update_success is False


# --------------------------------------------------------------------------
# Firmware version: re-fetched every cycle (not just once at setup -- real
# devices got an OTA update mid-development of this integration) and
# synced to the device registry when it changes.
# --------------------------------------------------------------------------

async def test_coordinator_syncs_device_registry_sw_and_hw_version_on_change(hass):
    """The actual point of this fix: a firmware/hardware version that
    changes between polls must propagate to the device registry, not
    just be captured once and left stale."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ADDRESS: PUMP_ADDRESS, CONF_SERIAL: PUMP_SERIAL},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, PUMP_SERIAL)},
        sw_version="2.1.5",  # the OLD versions
        hw_version="1",
    )
    assert device_entry.sw_version == "2.1.5"
    assert device_entry.hw_version == "1"

    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    fake_device = _make_fake_pump_device()
    fake_device.get_firmware_versions = AsyncMock(return_value={
        "Radio": "4.0.22", "Radio Bootloader": "1.2",
        "Product OS": "2.2.0",  # the NEW version, after the OTA update
        "Product Bootloader": "1.0",
    })
    fake_device.get_hardware_info = AsyncMock(return_value={"Revision": 2})  # a board revision

    group = registry.group(PAN_ID)
    with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device)):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    updated_device = device_registry.async_get(device_entry.id)
    assert updated_device.sw_version == "2.2.0"
    assert updated_device.hw_version == "2"


async def test_coordinator_does_not_touch_registry_when_versions_unchanged(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ADDRESS: PUMP_ADDRESS, CONF_SERIAL: PUMP_SERIAL},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, PUMP_SERIAL)},
        sw_version="2.1.5",
        hw_version="2",
    )

    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    # _make_fake_pump_device() reports "2.1.5"/Revision=2 -- same as
    # already registered above.
    fake_device = _make_fake_pump_device()

    group = registry.group(PAN_ID)
    with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device)):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    updated_device = device_registry.async_get(device_entry.id)
    assert updated_device.sw_version == "2.1.5"  # unchanged, as expected


# --------------------------------------------------------------------------
# derive_sw_version() -- picks a "main" firmware version to display,
# falling through a priority list rather than assuming "Product OS" is
# always present. Real hardware testing found at least some Radion
# lights don't report that specific FirmwareType at all, which left
# sw_version silently empty for those devices with a single hardcoded
# lookup -- these tests cover the actual bug scenario, not just the
# already-working "Product OS present" case.
# --------------------------------------------------------------------------

class TestDeriveSwVersion:
    def test_prefers_firmware_over_product_os_when_both_present(self):
        """The actual current behavior: "Firmware" (the light's real LED-
        driver microcontroller, FirmwareType.LEDClusterMicro/
        Esp32*Firmware) is confirmed via direct comparison against the
        official app's own display to be what it treats as primary --
        not "Product OS" (FirmwareType.MainMicroOS), even when both are
        present. See _SW_VERSION_LABEL_PRIORITY's comment for why an
        earlier version of this logic had these the other way around."""
        versions = {
            "Firmware": "1.0.2", "Product OS": "0.0.12", "Product Bootloader": "3.3.0",
            "Radio Firmware": "1.5.103", "Radio OS": "1.5.103", "Radio": "3.1.0",
        }
        assert derive_sw_version(versions) == "1.0.2"

    def test_prefers_product_os_when_no_firmware_label(self):
        versions = {
            "Product OS": "2.1.5", "Radio Firmware": "1.5.103", "Radio OS": "1.5.103",
        }
        assert derive_sw_version(versions) == "2.1.5"

    def test_falls_back_to_radio_firmware_when_no_product_os(self):
        """The actual real-hardware scenario this was built for: a light
        that reports Radio Firmware/Filesystem/Radio OS/Radio/WLAN but no
        Product OS at all (no MainMicroOS FirmwareType in its response)."""
        versions = {
            "Radio Firmware": "1.5.103", "Filesystem": "1.1.0",
            "Radio OS": "1.5.103", "Radio": "3.1.0", "WLAN": "3.1.0",
        }
        assert derive_sw_version(versions) == "1.5.103"

    def test_falls_back_to_radio_os_when_no_product_os_or_radio_firmware(self):
        versions = {"Radio OS": "1.5.103", "WLAN": "3.1.0"}
        assert derive_sw_version(versions) == "1.5.103"

    def test_falls_back_to_radio_as_last_resort(self):
        versions = {"WLAN": "3.1.0", "Radio": "3.1.0"}
        assert derive_sw_version(versions) == "3.1.0"

    def test_returns_none_when_nothing_in_priority_list_present(self):
        versions = {"Product Bootloader": "1.0", "Radio Bootloader": "1.2"}
        assert derive_sw_version(versions) is None

    def test_returns_none_for_empty_dict(self):
        assert derive_sw_version({}) is None

    def test_ignores_empty_string_values(self):
        """A present-but-empty value shouldn't be treated as "found" --
        must fall through to the next candidate."""
        versions = {"Product OS": "", "Radio Firmware": "1.5.103"}
        assert derive_sw_version(versions) == "1.5.103"


class TestDeriveHwVersion:
    """Requires python-mobius>=0.3.0's get_hardware_info() shape: Revision
    is already a plain int (no confirmed enum meaning for it), and
    Color/ProductType/RadioType/MotorType are already decoded label
    strings (see that library's own tests for the decoding itself) --
    derive_hw_version() just picks out and stringifies Revision, nothing
    more, so these tests confirm that narrow behavior against the actual
    current input shape rather than a shape python-mobius no longer
    produces."""

    def test_returns_revision_as_a_plain_string(self):
        assert derive_hw_version({"Revision": 2}) == "2"

    def test_handles_larger_revision_values(self):
        assert derive_hw_version({"Revision": 258}) == "258"

    def test_ignores_other_hardware_info_fields(self):
        info = {"Color": "White", "Revision": 3, "ProductType": "VorTech"}
        assert derive_hw_version(info) == "3"

    def test_returns_none_when_no_revision_field(self):
        assert derive_hw_version({"Color": "White"}) is None

    def test_returns_none_for_empty_dict(self):
        assert derive_hw_version({}) is None

    def test_revision_zero_is_a_real_value_not_treated_as_missing(self):
        """The actual point of this test: Revision=0 is a legitimate
        value (e.g. a first hardware revision), not an absent field --
        must not be conflated with "not present" the way a falsy-value
        check would incorrectly do."""
        assert derive_hw_version({"Revision": 0}) == "0"


# --------------------------------------------------------------------------
# discover_mesh_address() -- confirmed via real-world testing to be a real
# bug when it didn't share MobiusConnectionManager's connection semaphore:
# on-demand/proactive mesh discovery connects independently of any gateway
# connection, and without throttling through the same semaphore, a burst
# of discovery calls could exceed the real BLE adapter's actual
# concurrent-connection capacity even while MAX_CONCURRENT_CONNECTIONS
# appeared respected elsewhere -- manifesting as the gateway's own
# otherwise-healthy connection failing, triggering unnecessary failover
# (observed in production as repeated "Gateway ... failed 3 consecutive
# times" flapping between the same two devices).
# --------------------------------------------------------------------------

class TestDiscoverMeshAddressSemaphore:
    async def test_concurrent_calls_are_throttled_by_the_shared_semaphore(self, hass):
        """The actual point of the fix: concurrent discover_mesh_address()
        calls must never exceed the semaphore's permit count, confirmed
        by tracking the actual number running at once rather than just
        checking the semaphore object was touched."""
        semaphore = asyncio.Semaphore(1)  # only one connection attempt at a time
        concurrent_count = {"current": 0, "max_seen": 0}

        class FakeMobiusDevice:
            def __init__(self, ble_device, connect_timeout=None):
                pass

            async def __aenter__(self):
                concurrent_count["current"] += 1
                concurrent_count["max_seen"] = max(concurrent_count["max_seen"], concurrent_count["current"])
                await asyncio.sleep(0.05)  # simulate a real connection attempt taking time
                return self

            async def __aexit__(self, *args):
                concurrent_count["current"] -= 1

            async def get_own_mesh_address(self):
                return bytes.fromhex("fd11223344556677000000fffe001234")

        discovered = [_fake_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)]

        with patch(
            "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
            return_value=discovered,
        ), patch(
            "custom_components.mobius.coordinator.bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ), patch(
            "custom_components.mobius.coordinator.MobiusDevice", FakeMobiusDevice,
        ):
            await asyncio.gather(*[
                discover_mesh_address(hass, PUMP_SERIAL, semaphore) for _ in range(4)
            ])

        # With a 1-permit semaphore, at most 1 connection attempt should
        # ever have been in flight at once, regardless of 4 concurrent callers.
        assert concurrent_count["max_seen"] == 1

    async def test_does_not_serialize_beyond_the_semaphores_actual_limit(self, hass):
        """Confirms the fix isn't over-throttling either -- a 2-permit
        semaphore should allow up to 2 concurrent connection attempts,
        not force full serialization down to 1."""
        semaphore = asyncio.Semaphore(2)
        concurrent_count = {"current": 0, "max_seen": 0}

        class FakeMobiusDevice:
            def __init__(self, ble_device, connect_timeout=None):
                pass

            async def __aenter__(self):
                concurrent_count["current"] += 1
                concurrent_count["max_seen"] = max(concurrent_count["max_seen"], concurrent_count["current"])
                await asyncio.sleep(0.05)
                return self

            async def __aexit__(self, *args):
                concurrent_count["current"] -= 1

            async def get_own_mesh_address(self):
                return bytes.fromhex("fd11223344556677000000fffe001234")

        discovered = [_fake_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)]

        with patch(
            "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
            return_value=discovered,
        ), patch(
            "custom_components.mobius.coordinator.bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ), patch(
            "custom_components.mobius.coordinator.MobiusDevice", FakeMobiusDevice,
        ):
            await asyncio.gather(*[
                discover_mesh_address(hass, PUMP_SERIAL, semaphore) for _ in range(4)
            ])

        assert concurrent_count["max_seen"] == 2

    async def test_shares_the_same_semaphore_object_as_the_gateway_connection(self, hass):
        """Confirms GatewayRegistry.semaphore is the actual attribute
        callers should use -- a plain attribute access, not a private one
        that discovery calls would have no legitimate way to reach."""
        from custom_components.mobius.gateway_registry import GatewayRegistry

        semaphore = asyncio.Semaphore(2)
        registry = GatewayRegistry(hass, semaphore, election_settle_seconds=0.01)
        assert registry.semaphore is semaphore

        group = await registry.join(0x3D0F, PUMP_SERIAL, rssi=-50)
        # The gateway's own MobiusConnectionManager was constructed with
        # this exact semaphore object -- confirms it's genuinely shared,
        # not a separate equally-sized-but-different instance.
        assert group.gateway_connection._semaphore is semaphore


# --------------------------------------------------------------------------
# discover_tank_for_serial() -- the config flow's own "is this device
# part of a multi-device tank" call. Same connection-resolution pattern
# as discover_mesh_address() above (including sharing the connection
# semaphore for the same reasons), just calling
# mobius.discovery.discover_tank() once connected instead.
# --------------------------------------------------------------------------

class TestDiscoverTankForSerial:
    async def test_finds_device_and_returns_its_tank(self, hass):
        semaphore = asyncio.Semaphore(1)
        expected_tank = Tank(
            prefix=bytes.fromhex("fd11223344556677"),
            peers=[MeshPeer(
                serial=PUMP_SERIAL, model_raw=42, model=Model.VorTechMP40wG3QD,
                short_address=0x1234, address=bytes.fromhex("fd1122334455667700000000000012"),
            )],
        )

        class FakeMobiusDevice:
            def __init__(self, ble_device, connect_timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        discovered = [_fake_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)]

        with patch(
            "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
            return_value=discovered,
        ), patch(
            "custom_components.mobius.coordinator.bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ), patch(
            "custom_components.mobius.coordinator.MobiusDevice", FakeMobiusDevice,
        ), patch(
            "custom_components.mobius.coordinator.discover_tank",
            AsyncMock(return_value=expected_tank),
        ):
            result = await discover_tank_for_serial(hass, PUMP_SERIAL, semaphore)

        assert result is expected_tank

    async def test_returns_none_if_device_not_currently_advertising(self, hass):
        """Distinguishable from discover_tank()'s own Tank(prefix=None,
        peers=[]) -- this None specifically means "couldn't even find/
        reach the device right now," not "reached it, but it has no
        tank." Callers must not conflate the two."""
        semaphore = asyncio.Semaphore(1)

        with patch(
            "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
            return_value=[],
        ):
            result = await discover_tank_for_serial(hass, PUMP_SERIAL, semaphore)

        assert result is None

    async def test_returns_none_on_connection_failure_rather_than_raising(self, hass):
        semaphore = asyncio.Semaphore(1)

        class FailingMobiusDevice:
            def __init__(self, ble_device, connect_timeout=None):
                pass

            async def __aenter__(self):
                raise IOError("could not connect")

        discovered = [_fake_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)]

        with patch(
            "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
            return_value=discovered,
        ), patch(
            "custom_components.mobius.coordinator.bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ), patch(
            "custom_components.mobius.coordinator.MobiusDevice", FailingMobiusDevice,
        ):
            result = await discover_tank_for_serial(hass, PUMP_SERIAL, semaphore)

        assert result is None

    async def test_shares_the_connection_semaphore(self, hass):
        """Same real-world-confirmed reasoning as
        TestDiscoverMeshAddressSemaphore above -- this connects
        independently of any gateway connection, so it must be throttled
        by the same shared semaphore or it can exceed the real adapter's
        actual concurrent-connection capacity."""
        semaphore = asyncio.Semaphore(1)
        concurrent_count = {"current": 0, "max_seen": 0}

        class FakeMobiusDevice:
            def __init__(self, ble_device, connect_timeout=None):
                pass

            async def __aenter__(self):
                concurrent_count["current"] += 1
                concurrent_count["max_seen"] = max(concurrent_count["max_seen"], concurrent_count["current"])
                await asyncio.sleep(0.05)
                return self

            async def __aexit__(self, *args):
                concurrent_count["current"] -= 1

        discovered = [_fake_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)]

        with patch(
            "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
            return_value=discovered,
        ), patch(
            "custom_components.mobius.coordinator.bluetooth.async_ble_device_from_address",
            return_value=MagicMock(),
        ), patch(
            "custom_components.mobius.coordinator.MobiusDevice", FakeMobiusDevice,
        ), patch(
            "custom_components.mobius.coordinator.discover_tank",
            AsyncMock(return_value=Tank(prefix=None, peers=[])),
        ):
            await asyncio.gather(*[
                discover_tank_for_serial(hass, PUMP_SERIAL, semaphore) for _ in range(4)
            ])

        assert concurrent_count["max_seen"] == 1


# --------------------------------------------------------------------------
# Mesh last-seen refresh (MobiusDeviceCoordinator._refresh_mesh_last_seen)
# --------------------------------------------------------------------------

async def test_gateway_coordinator_refreshes_mesh_last_seen_for_every_member(hass):
    """The core point: ONE extra read on the gateway's own poll cycle
    populates the registry's own mesh_last_seen_at for EVERY tank
    member, not just the gateway's own -- including a member whose own
    coordinator never ran this cycle at all."""
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    await registry.join(PAN_ID, LIGHT_SERIAL, rssi=-60)
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    fake_device = _make_fake_pump_device()
    fake_device.discover_networked_thread_devices = AsyncMock(return_value=[
        MeshPeer(
            serial=PUMP_SERIAL, model_raw=42, model=Model.VorTechMP40wG3QD,
            short_address=0x1234, address=b"\x00" * 16, age=5000,  # 5 real seconds ago
        ),
        MeshPeer(
            serial=LIGHT_SERIAL, model_raw=179, model=Model.RadionXR15wG6Pro,
            short_address=0x5678, address=b"\x00" * 16, age=120000,  # 2 real minutes ago
        ),
    ])

    group = registry.group(PAN_ID)
    frozen_now = dt_util.utcnow()
    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device),
    ), patch("custom_components.mobius.coordinator.dt_util.utcnow", return_value=frozen_now):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    # The gateway's own coordinator data carries its own value.
    assert coordinator.data["mesh_last_seen_at"] == frozen_now - timedelta(milliseconds=5000)
    # AND the OTHER member's own registry entry was updated too, from
    # this SAME single read -- even though LIGHT_SERIAL's own
    # coordinator was never involved in this refresh at all.
    assert group.members[LIGHT_SERIAL].mesh_last_seen_at == frozen_now - timedelta(milliseconds=120000)


async def test_relayed_coordinator_picks_up_mesh_last_seen_from_registry(hass):
    """A relayed device's own coordinator does NOT do the extra read
    itself -- it just reads whatever the gateway's own last poll already
    wrote to the shared registry."""
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)  # becomes gateway
    await registry.join(PAN_ID, LIGHT_SERIAL, rssi=-90)
    group = registry.group(PAN_ID)
    known_last_seen = dt_util.utcnow() - timedelta(seconds=42)
    registry.update_mesh_last_seen(PAN_ID, LIGHT_SERIAL, known_last_seen)
    registry.update_mesh_address(PAN_ID, LIGHT_SERIAL, b"\xfd" + b"\x00" * 15)

    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, LIGHT_SERIAL, PAN_ID)
    fake_gateway_device = _make_fake_pump_device()
    # No discover_networked_thread_devices call expected for a relayed
    # device's own coordinator -- deliberately not mocked, so a call
    # would raise and get caught by the non-fatal handling, but the
    # assertion below confirms it wasn't even needed.
    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_gateway_device),
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_gateway_device,
    ):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data["mesh_last_seen_at"] == known_last_seen


async def test_mesh_last_seen_refresh_failure_is_non_fatal(hass):
    """Supplementary data layered on top of an already-successful status
    read -- a failure here must not undo that success."""
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    fake_device = _make_fake_pump_device()
    fake_device.discover_networked_thread_devices = AsyncMock(
        side_effect=Exception("mesh peer read failed this cycle"),
    )

    group = registry.group(PAN_ID)
    with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device)):
        await coordinator.async_refresh()

    # The main status read still succeeded.
    assert coordinator.last_update_success
    assert coordinator.data["support"] == "pump"
    # Just no mesh_last_seen_at this cycle.
    assert coordinator.data["mesh_last_seen_at"] is None
