"""
Tests for MobiusConnectionManager (persistent, serial-resolved
connections, now shared per pan_id group via gateway_registry rather
than per-device) and MobiusDeviceCoordinator built on top of it -- one
coordinator per device, one merged status+schedule read per cycle,
gateway-vs-relayed dispatch, and graceful (grace-period, not immediate)
failure handling.
"""

import asyncio
import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius.const import DOMAIN, CONF_SERIAL, MARK_UNAVAILABLE_AFTER, GATEWAY_FAILURE_THRESHOLD
from custom_components.mobius.coordinator import (
    MobiusConnectionManager, MobiusDeviceCoordinator, derive_sw_version, derive_hw_version,
    discover_mesh_address, discover_tank_for_serial,
)
from custom_components.mobius.gateway_registry import GatewayRegistry
from homeassistant.exceptions import HomeAssistantError
from mobius import PrimitiveType, Tank, MeshPeer, Model, MetadataSnapshot, SupportedAttribute
from mobius.relay import RelayedMobiusDevice

PUMP_SERIAL = "00000000000001"
PUMP_ADDRESS = "AA:AA:AA:AA:AA:01"
LIGHT_SERIAL = "FAKESERIAL0001"
PAN_ID = 0x3D0F

# Payload shaped after a real captured advertisement for this pump (see
# python-mobius's own tests) -- serial replaced with an obviously-fake
# placeholder of the same byte length.
REAL_PUMP_PAYLOAD = bytes.fromhex("2a0001000000000f3d3030303030303030303030303031")
# A real captured AI Axis 20 pump advertisement (serial swapped for a
# placeholder, same convention as the payload above) -- see
# python-mobius's own test_manufacturer.py for the full confirmation.
REAL_AI_AXIS_PAYLOAD = bytes.fromhex("0501010100000026f446414b4553455249414c41493031")
MOBIUS_COMPANY_ID_ECOTECH = 0x0202
MOBIUS_COMPANY_ID_AQUAILLUMINATION = 0x0001


def _fake_discovery_info(address: str, payload: bytes, company_id: int = MOBIUS_COMPANY_ID_ECOTECH):
    """A minimal stand-in for BluetoothServiceInfoBleak -- only the
    attributes MobiusConnectionManager._resolve_current_ble_device()
    actually reads."""
    info = MagicMock()
    info.address = address
    info.manufacturer_data = {company_id: payload}
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
    device.get_metadata_batch = AsyncMock(return_value=MetadataSnapshot(
        advanced_features=None, calibration=None,
        hardware_info={"Revision": 2},
        firmware_versions={
            "Radio": "4.0.21", "Radio Bootloader": "1.2",
            "Product OS": "2.1.5", "Product Bootloader": "1.0",
        },
        supported_channels=[], error_state=None, epoch=None, local_time=None, tz_offset=None,
    ))
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


async def test_resolve_current_ble_device_matches_aquaillumination_device(hass):
    """The actual bug this confirms is fixed: an AquaIllumination
    device (company ID 0x0001, not EcoTech Marine's 0x0202) used to
    come back from _find_in_bluetooth_cache() as not found at all,
    since only company ID 0x0202 was ever checked -- regardless of the
    device being perfectly visible in Home Assistant's own Bluetooth
    cache."""
    ai_serial = "FAKESERIALAI01"
    ai_address = "AA:AA:AA:AA:AA:09"
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, ai_serial, semaphore)

    discovered = [_fake_discovery_info(
        ai_address, REAL_AI_AXIS_PAYLOAD, company_id=MOBIUS_COMPANY_ID_AQUAILLUMINATION,
    )]

    with patch(
        "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
        return_value=discovered,
    ), patch(
        "custom_components.mobius.coordinator.bluetooth.async_ble_device_from_address",
        return_value=MagicMock(address=ai_address),
    ):
        result = await manager._resolve_current_ble_device()

    assert result is not None
    assert result.address == ai_address


async def test_resolve_current_ble_device_returns_none_when_not_found(hass, caplog):
    """Confirms both the return value AND the diagnostic log this
    specifically exists for -- the single most useful line for
    distinguishing "device isn't visible at all" from "visible but
    couldn't connect" when debugging a device that's never coming up.
    Also confirms the active-scan fallback is actually attempted before
    giving up -- a real, confirmed production incident showed a device
    can go missing from Home Assistant's own Bluetooth cache for hours
    at a stretch, well past whatever passive-scanning cadence would
    normally rediscover it on its own."""
    caplog.set_level(logging.DEBUG, logger="custom_components.mobius")
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, "NONEXISTENT_SERIAL", semaphore)

    with patch(
        "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
        return_value=[],
    ), patch(
        "custom_components.mobius.coordinator.bluetooth.async_request_active_scan",
        AsyncMock(),
    ) as mock_active_scan, patch(
        "custom_components.mobius.coordinator.bluetooth.async_scanner_count",
        return_value=2,
    ):
        result = await manager._resolve_current_ble_device()

    assert result is None
    mock_active_scan.assert_awaited_once_with(hass)
    assert "NONEXISTENT_SERIAL not found in Home Assistant's own Bluetooth cache" in caplog.text
    assert "2 connectable scanner(s) currently registered" in caplog.text


async def test_resolve_current_ble_device_recovers_via_active_scan(hass, caplog):
    """The actual point of the whole fallback: a device not found on
    the first, fast cache-only pass but found immediately after
    requesting a one-shot active scan must still resolve successfully,
    not report not-found just because the FIRST pass alone missed it."""
    caplog.set_level(logging.DEBUG, logger="custom_components.mobius")
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)

    async def fake_active_scan(hass_arg):
        assert hass_arg is hass

    with patch(
        "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
        side_effect=[[], [_fake_discovery_info(PUMP_ADDRESS, REAL_PUMP_PAYLOAD)]],
    ), patch(
        "custom_components.mobius.coordinator.bluetooth.async_request_active_scan",
        fake_active_scan,
    ), patch(
        "custom_components.mobius.coordinator.bluetooth.async_ble_device_from_address",
        return_value=MagicMock(address=PUMP_ADDRESS),
    ):
        result = await manager._resolve_current_ble_device()

    assert result is not None
    assert result.address == PUMP_ADDRESS
    assert f"{PUMP_SERIAL} found after requesting a one-shot active scan" in caplog.text


# --------------------------------------------------------------------------
# MobiusConnectionManager.ensure_connected / mark_disconnected / disconnect
# --------------------------------------------------------------------------

async def test_is_connected_false_before_any_connection(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)
    assert manager.is_connected is False


async def test_is_connected_true_after_connecting(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)

    fake_device = MagicMock()
    fake_device.is_connected = True
    fake_device.connect = AsyncMock()

    with patch(
        "custom_components.mobius.coordinator.MobiusDevice", return_value=fake_device
    ), patch.object(
        manager, "_resolve_current_ble_device", AsyncMock(return_value=MagicMock())
    ):
        await manager.ensure_connected()

    assert manager.is_connected is True


async def test_is_connected_false_after_mark_disconnected(hass):
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)

    fake_device = MagicMock()
    fake_device.is_connected = True
    fake_device.connect = AsyncMock()

    with patch(
        "custom_components.mobius.coordinator.MobiusDevice", return_value=fake_device
    ), patch.object(
        manager, "_resolve_current_ble_device", AsyncMock(return_value=MagicMock())
    ):
        await manager.ensure_connected()
    manager.mark_disconnected()

    assert manager.is_connected is False


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


async def test_ensure_connected_logs_long_semaphore_waits(hass, caplog):
    """The direct instrument for confirming or ruling out
    MAX_CONCURRENT_CONNECTIONS contention as the cause of a device never
    getting a real connection attempt -- confirms this is actually
    measured and surfaced, not just theoretically possible to reason
    about from the semaphore's own configured size. time.monotonic() is
    mocked to simulate a long wait deterministically, rather than
    actually sleeping for it -- a real, slow, flaky way to test this."""
    caplog.set_level(logging.DEBUG, logger="custom_components.mobius")
    semaphore = asyncio.Semaphore(2)
    manager = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)

    fake_device = MagicMock()
    fake_device.is_connected = True
    fake_device.connect = AsyncMock()

    # Simulates a 5.3-second wait for the semaphore specifically --
    # monotonic() is called at exactly two points that matter here (the
    # wait-start snapshot, and the wait-end snapshot right after
    # acquiring the semaphore), then again for the connect-timing pair.
    with patch(
        "custom_components.mobius.coordinator.MobiusDevice", return_value=fake_device
    ), patch.object(
        manager, "_resolve_current_ble_device", AsyncMock(return_value=MagicMock())
    ), patch(
        "custom_components.mobius.coordinator.time.monotonic",
        side_effect=[0.0, 5.3, 5.3, 5.4],
    ):
        await manager.ensure_connected()

    assert "waited 5.3s for a free connection slot" in caplog.text


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


class TestSupportedAttributeIdCaching:
    """The actual new behavior get_metadata_batch() wiring introduces:
    a device's own confirmed attribute support essentially never
    changes across a session, so fetching it once and reusing it
    indefinitely avoids a whole extra get_supported_attributes()
    round-trip on every single poll, forever -- see _fetch_all()'s own
    docstring for the full reasoning."""

    async def test_first_poll_has_no_cache_and_populates_one(self, hass):
        registry = _make_registry(hass)
        await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
        entry = MagicMock()
        coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)
        assert coordinator._supported_attribute_ids is None

        fake_device = _make_fake_pump_device()
        fake_device.get_supported_attributes = AsyncMock(return_value=[
            SupportedAttribute(attr_id=300, indexes=[0]),
            SupportedAttribute(attr_id=301, indexes=[0]),
        ])
        group = registry.group(PAN_ID)
        with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device)):
            await coordinator.async_refresh()

        assert coordinator.last_update_success
        assert coordinator._supported_attribute_ids == {300, 301}
        fake_device.get_supported_attributes.assert_awaited_once()

    async def test_second_poll_reuses_the_cache_without_refetching(self, hass):
        registry = _make_registry(hass)
        await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
        entry = MagicMock()
        coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

        fake_device = _make_fake_pump_device()
        fake_device.get_supported_attributes = AsyncMock(return_value=[
            SupportedAttribute(attr_id=300, indexes=[0]),
        ])
        group = registry.group(PAN_ID)
        with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device)):
            await coordinator.async_refresh()  # first poll -- populates the cache
            await coordinator.async_refresh()  # second poll -- should reuse it

        # The actual point: only ONE call across BOTH polls, not two.
        fake_device.get_supported_attributes.assert_awaited_once()
        # And the cached set was genuinely what got passed through --
        # confirms this isn't just "never called again" by coincidence.
        fake_device.get_metadata_batch.assert_awaited_with(
            model=Model.VorTechMP40wG3QD, supported_attribute_ids={300},
        )

    async def test_cache_not_updated_on_a_failed_poll(self, hass):
        """A real, expected outcome: if a poll fails entirely, whatever
        was already cached should survive untouched -- there's no new,
        confirmed support information to replace it with."""
        registry = _make_registry(hass)
        await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
        entry = MagicMock()
        coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)
        coordinator._supported_attribute_ids = {999}  # pre-seed a fake prior cache

        broken_device = MagicMock()
        broken_device.get_device_info = AsyncMock(side_effect=IOError("connection lost"))
        group = registry.group(PAN_ID)
        with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=broken_device)):
            await coordinator.async_refresh()

        assert coordinator.last_update_success is False
        assert coordinator._supported_attribute_ids == {999}  # untouched


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


async def test_gateway_failure_stale_after_concurrent_promotion_is_dropped(hass):
    """The actual fix for a real, confirmed production incident: two
    devices in one tank, gateway election alternating indefinitely
    (see gateway_registry.py's own TestGenerationFencing for the full
    incident writeup and the exact self-relay log line it produced).
    Simulates a promotion happening WHILE this coordinator's own fetch
    is already in flight -- confirms the resulting failure is
    recognized as stale (via the generation captured at fetch-start)
    and dropped entirely: neither the NEW gateway's own failure
    counter nor its connection's mark_disconnected() should be
    touched by a failure that predates the promotion.

    Watches MobiusConnectionManager.mark_disconnected() at the CLASS
    level, not on a specific instance -- the new gateway's own
    connection object doesn't exist yet at patch-setup time (it's only
    created by the promotion that happens mid-test), and watching a
    specific pre-existing instance would only prove mark_disconnected()
    wasn't called on THAT one, trivially true regardless of whether the
    fix works, since the bug this fix prevents calls it on the NEW
    connection instead."""
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    await registry.join(PAN_ID, "other", rssi=-40)
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    broken_device = MagicMock()
    broken_device.get_device_info = AsyncMock(side_effect=IOError("connection lost"))

    group = registry.group(PAN_ID)
    old_gateway_connection = group.gateway_connection

    async def _ensure_connected_but_promote_first(*args, **kwargs):
        # Simulates a DIFFERENT coordinator's own concurrent promotion
        # completing while THIS fetch was already past its own
        # generation-capture point, but before its own read actually
        # fails -- exactly the real incident's own timing.
        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            await registry.record_gateway_failure(PAN_ID, group.generation)
        return broken_device

    with patch.object(
        old_gateway_connection, "ensure_connected", side_effect=_ensure_connected_but_promote_first,
    ), patch.object(MobiusConnectionManager, "mark_disconnected") as mock_mark:
        await coordinator.async_refresh()

    # The promotion above already happened -- confirm it actually did,
    # or this test isn't exercising the scenario it claims to.
    assert registry.group(PAN_ID).gateway_serial == "other"
    new_gateway_connection = registry.group(PAN_ID).gateway_connection
    assert new_gateway_connection is not old_gateway_connection

    # The actual point: this coordinator's own (now-stale) failure must
    # NOT have touched the NEW gateway's state at all -- neither its
    # failure counter nor mark_disconnected(), on EITHER connection
    # object (class-level patch catches both).
    assert registry.group(PAN_ID).consecutive_gateway_failures == 0
    mock_mark.assert_not_called()


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
    gateway failure -- only the gateway's own coordinator does that. It
    DOES still count toward a separate, per-target relay-failure tally
    though (see gateway_registry.py's own TestRelayFailover for the full
    reasoning) -- confirmed here too, not just in that lower-level suite,
    since this is what actually proves _fetch() itself is wired up to
    call it, not just that the registry method works in isolation."""
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
    assert registry.group(PAN_ID).members[PUMP_SERIAL].consecutive_relay_failures == 1


async def test_relayed_coordinator_success_resets_its_own_relay_failure_count(hass):
    registry = _make_registry(hass)
    await registry.join(PAN_ID, "gateway-serial", rssi=-50)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-80)
    registry.update_mesh_address(PAN_ID, PUMP_SERIAL, bytes.fromhex("fd11223344556677000000fffe001234"))
    # Simulates some earlier relay trouble that hadn't yet reached the
    # promotion threshold -- confirms a real, successful read clears it.
    registry.group(PAN_ID).members[PUMP_SERIAL].consecutive_relay_failures = 2

    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    fake_gateway_device = MagicMock()
    fake_relayed_device = _make_fake_pump_device()
    group = registry.group(PAN_ID)

    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_gateway_device),
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_relayed_device,
    ):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert registry.group(PAN_ID).members[PUMP_SERIAL].consecutive_relay_failures == 0


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
# async_get_connected_device() -- the shared gateway-vs-relay resolution
# _fetch() itself uses, factored out for one-off actions against a
# specific device (e.g. a button press) that need the same resolution
# without duplicating it or triggering _fetch()'s own poll-cycle
# bookkeeping (record_gateway_success()/record_relay_success()/etc).
# --------------------------------------------------------------------------

async def test_async_get_connected_device_returns_direct_connection_when_gateway(hass):
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    fake_device = _make_fake_pump_device()
    group = registry.group(PAN_ID)
    assert group.gateway_serial == PUMP_SERIAL  # confirms this really is the gateway case
    with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device)):
        device = await coordinator.async_get_connected_device()

    assert device is fake_device


async def test_async_get_connected_device_returns_relayed_connection_when_not_gateway(hass):
    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)  # elected gateway
    await registry.join(PAN_ID, LIGHT_SERIAL, rssi=-80)  # NOT the gateway
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, LIGHT_SERIAL, PAN_ID)

    fake_gateway_device = _make_fake_pump_device()
    group = registry.group(PAN_ID)
    assert group.gateway_serial == PUMP_SERIAL  # confirms LIGHT_SERIAL really is relayed here
    fake_mesh_address = bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe005678")
    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_gateway_device),
    ), patch.object(
        coordinator, "_resolve_own_mesh_peer", AsyncMock(return_value=MeshPeer(
            serial=LIGHT_SERIAL, model_raw=0, model=None,
            short_address=0x5678, address=fake_mesh_address,
        )),
    ):
        device = await coordinator.async_get_connected_device()

    assert isinstance(device, RelayedMobiusDevice)
    assert device.gateway is fake_gateway_device
    assert device.peer.serial == LIGHT_SERIAL


async def test_async_get_connected_device_raises_if_no_gateway_available(hass):
    registry = _make_registry(hass)
    entry = MagicMock()
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)
    # Deliberately never joined -- registry.group(PAN_ID) is None.

    with pytest.raises(HomeAssistantError, match="No gateway currently available"):
        await coordinator.async_get_connected_device()


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
    fake_device.get_metadata_batch = AsyncMock(return_value=MetadataSnapshot(
        advanced_features=None, calibration=None,
        hardware_info={"Revision": 2},  # a board revision
        firmware_versions={
            "Radio": "4.0.22", "Radio Bootloader": "1.2",
            "Product OS": "2.2.0",  # the NEW version, after the OTA update
            "Product Bootloader": "1.0",
        },
        supported_channels=[], error_state=None, epoch=None, local_time=None, tz_offset=None,
    ))

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


async def test_coordinator_corrects_device_name_once_real_data_arrives(hass):
    """The actual, real, reported bug this fixes: a device whose own
    first read at setup failed gets registered with sensor.py's own
    generic "Mobius device (SERIAL)" fallback name (DeviceInfo is only
    consulted once, at entity-creation time) -- and since the entity-
    healing fix (_async_ensure_sensors_exist()) only ever creates the
    missing ENTITIES, not update the device's own registry entry, that
    name was staying stuck at the fallback forever, even once the
    entities themselves recovered correctly. Confirms this coordinator-
    level, every-successful-poll sync (already existing for sw_version/
    hw_version) now also corrects name/model/manufacturer the same way,
    the moment real data actually arrives -- not just at initial setup."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ADDRESS: PUMP_ADDRESS, CONF_SERIAL: PUMP_SERIAL},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, PUMP_SERIAL)},
        name=f"Mobius device ({PUMP_SERIAL})",  # the generic fallback, matching a failed first read
    )
    assert device_entry.name == f"Mobius device ({PUMP_SERIAL})"
    assert device_entry.model is None

    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)

    # _make_fake_pump_device()'s own get_device_info() reports a real
    # name ("MP40QD Right"), model, and manufacturer -- the read this
    # test simulates finally succeeding.
    fake_device = _make_fake_pump_device()

    group = registry.group(PAN_ID)
    with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device)):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    updated_device = device_registry.async_get(device_entry.id)
    assert updated_device.name == "MP40QD Right"
    assert updated_device.model == "VorTechMP40wG3QD"
    assert updated_device.manufacturer == "EcoTech Marine"


async def test_coordinator_does_not_touch_registry_name_when_already_correct(hass):
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_ADDRESS: PUMP_ADDRESS, CONF_SERIAL: PUMP_SERIAL},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, PUMP_SERIAL)},
        name="MP40QD Right",  # already correct, matching _make_fake_pump_device()'s own report
        model="VorTechMP40wG3QD",
        manufacturer="EcoTech Marine",
        sw_version="2.1.5",
        hw_version="2",
    )

    registry = _make_registry(hass)
    await registry.join(PAN_ID, PUMP_SERIAL, rssi=-50)
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP_SERIAL, PAN_ID)
    fake_device = _make_fake_pump_device()

    group = registry.group(PAN_ID)
    with patch.object(
        group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_device),
    ), patch.object(
        device_registry, "async_update_device",
    ) as mock_update:
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    mock_update.assert_not_called()


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

    def test_ai_axis_pump_uses_os_label(self):
        """A real, reported production gap: AquaIllumination-brand
        devices report an entirely different label set than EcoTech-
        brand ones -- "OS" here, matching neither "Firmware" nor
        "Product OS" -- so sw_version came back None despite
        firmware_versions itself being fully populated (confirmed via
        the sensor's own extra_state_attributes still showing every
        individual firmware component correctly). Real values, from an
        actual AI Axis 40."""
        versions = {"OS": "2.3.15", "Bootloader": "1.5.0"}
        assert derive_sw_version(versions) == "2.3.15"

    def test_ai_prime_light_falls_back_to_qca4020_firmware_label(self):
        """Same gap, the light-side variant: no "OS" label at all here
        (that's pump-specific), so this needs its own fallback. Order
        (OS checked first, in the priority list above this one) confirmed
        directly against the app's own TDevice.json() -- its own
        diagnostics-export method picks exactly this same priority for
        its own single "main version" field, since a device only ever
        reports one of the two labels, never both. Real values, from an
        actual AI Prime 16HD."""
        versions = {
            "QCA4020Firmware": "1.5.69", "QCA4020FileSystem": "1.1.0",
            "QCA4020M4F": "1.5.69", "QCA4020M0": "3.1.0", "QCA4020WLAN": "3.1.0",
        }
        assert derive_sw_version(versions) == "1.5.69"


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

    async def test_returns_none_if_device_not_currently_advertising(self, hass, caplog):
        """Distinguishable from discover_tank()'s own Tank(prefix=None,
        peers=[]) -- this None specifically means "couldn't even find/
        reach the device right now," not "reached it, but it has no
        tank." Callers must not conflate the two. Also confirms this
        logs the same diagnostic detail its two sibling functions
        (_resolve_current_ble_device, discover_mesh_address) already
        do -- a real, confirmed gap this used to have: silently
        returning None with no logging at all, unlike either of them."""
        caplog.set_level(logging.DEBUG, logger="custom_components.mobius")
        semaphore = asyncio.Semaphore(1)

        with patch(
            "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
            return_value=[],
        ), patch(
            "custom_components.mobius.coordinator.bluetooth.async_request_active_scan",
            AsyncMock(),
        ), patch(
            "custom_components.mobius.coordinator.bluetooth.async_scanner_count",
            return_value=1,
        ):
            result = await discover_tank_for_serial(hass, PUMP_SERIAL, semaphore)

        assert result is None
        assert f"{PUMP_SERIAL} not found in Home Assistant's own Bluetooth cache" in caplog.text
        assert "1 connectable scanner(s) currently registered" in caplog.text

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
