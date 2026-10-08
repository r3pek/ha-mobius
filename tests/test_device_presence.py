"""Devices that disappear (powered off, out of range) and come back:

- only fresh advertisements count as a device advertising;
- a gateway that is gone is replaced at once, by a member advertising now;
- relayed polls don't reconnect a gateway that is down, and its failure is
  not counted against them;
- work aimed at a replaced gateway stops;
- absent devices are not polled, counted or restarted until seen again;
- a missing Mobius characteristic clears the service cache and retries.
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.exc import BleakCharacteristicNotFoundError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius import MobiusRuntimeData, _async_revalidate_tank, _async_watch_advertisements
from custom_components.mobius.const import CONF_DEVICES, CONF_PAN_ID, CONF_SERIAL, DOMAIN, RELAY_FAILURE_THRESHOLD
from custom_components.mobius.coordinator import (
    DeviceNotAdvertising, GatewayReplaced, MobiusConnectionManager, MobiusDeviceCoordinator,
    _connect, _find_in_bluetooth_cache,
)
from custom_components.mobius.gateway_registry import K32W_RADIO_LABEL

from tests.test_coordinator import (
    LIGHT_SERIAL, PAN_ID, PUMP_SERIAL, REAL_PUMP_PAYLOAD, _make_registry,
    _mark_gateway_connected,
)
from tests.test_init import _make_registry_with_gateway

COORDINATOR = "custom_components.mobius.coordinator"
PUMP2_SERIAL = "00000000000002"
LIGHT2_SERIAL = "FAKESERIAL0002"


def _info(serial_payload: bytes, address: str, received: float, rssi: int = -60):
    info = MagicMock()
    info.address = address
    info.rssi = rssi
    info.time = received
    info.manufacturer_data = {0x0202: serial_payload}
    return info


def _advertising(*serials):
    """Patch for is_advertising(): only `serials` advertise."""
    return patch(f"{COORDINATOR}.is_advertising", side_effect=lambda hass, serial: serial in serials)


def _live_rssi(rssi_by_serial):
    """Patch for advertising_rssi(): RSSI of the serials advertising now."""
    return patch(f"{COORDINATOR}.advertising_rssi", side_effect=lambda hass, serial: rssi_by_serial.get(serial))


async def _tank(hass, gateway=PUMP_SERIAL, members=((PUMP_SERIAL, -40), (PUMP2_SERIAL, -45), (LIGHT_SERIAL, -80), (LIGHT2_SERIAL, -82))):
    registry = _make_registry(hass)
    first = True
    for serial, rssi in members:
        await registry.join(PAN_ID, serial, rssi=rssi, prefer_as_gateway=first and serial == gateway)
        first = False
    group = registry.group(PAN_ID)
    for serial, _rssi in members:
        registry.update_mesh_address(PAN_ID, serial, bytes(15) + bytes([len(serial)]))
    return registry, group


# --------------------------------------------------------------------------
# Fresh advertisements only
# --------------------------------------------------------------------------

class TestAdvertisementAge:
    def test_an_old_advertisement_does_not_count(self, hass):
        stale = _info(REAL_PUMP_PAYLOAD, "AA:AA:AA:AA:AA:AA", received=1000.0)
        with patch(f"{COORDINATOR}.bluetooth.async_discovered_service_info", return_value=[stale]), \
                patch(f"{COORDINATOR}.bluetooth.MONOTONIC_TIME", return_value=1000.0 + 244):
            assert _find_in_bluetooth_cache(hass, PUMP_SERIAL) is None
            assert _find_in_bluetooth_cache(hass, PUMP_SERIAL, max_age=None) is stale

    def test_the_newest_of_several_addresses_wins(self, hass):
        older = _info(REAL_PUMP_PAYLOAD, "AA:AA:AA:AA:AA:AA", received=990.0)
        newer = _info(REAL_PUMP_PAYLOAD, "BB:BB:BB:BB:BB:BB", received=999.0)
        with patch(f"{COORDINATOR}.bluetooth.async_discovered_service_info", return_value=[newer, older][::-1]), \
                patch(f"{COORDINATOR}.bluetooth.MONOTONIC_TIME", return_value=1000.0):
            assert _find_in_bluetooth_cache(hass, PUMP_SERIAL) is newer


# --------------------------------------------------------------------------
# Choosing a gateway
# --------------------------------------------------------------------------

class TestGatewayChoice:
    async def test_members_advertising_now_win_over_better_stored_rssi(self, hass):
        registry, group = await _tank(hass)
        with _live_rssi({LIGHT_SERIAL: -81, LIGHT2_SERIAL: -70}):
            assert registry._best_candidate(group, exclude_serials={PUMP_SERIAL}) == LIGHT2_SERIAL

    async def test_k32w_rule_applies_among_members_advertising(self, hass):
        registry, group = await _tank(hass)
        registry.update_radio_type(PAN_ID, LIGHT2_SERIAL, K32W_RADIO_LABEL)
        with _live_rssi({LIGHT_SERIAL: -85, LIGHT2_SERIAL: -60}):
            assert registry._best_candidate(group, exclude_serials={PUMP_SERIAL}) == LIGHT_SERIAL

    async def test_absent_members_are_skipped_when_nobody_advertises(self, hass):
        registry, group = await _tank(hass)
        registry.update_mesh_peers(PAN_ID, {PUMP_SERIAL, LIGHT_SERIAL, LIGHT2_SERIAL}, time.monotonic())
        with _live_rssi({}):
            # PUMP2 has the best stored RSSI but isn't on the mesh.
            assert registry._best_candidate(group, exclude_serials={PUMP_SERIAL}) == LIGHT_SERIAL

    async def test_stored_rssi_is_used_when_nothing_else_is_known(self, hass):
        registry, group = await _tank(hass)
        with _live_rssi({}):
            assert registry._best_candidate(group, exclude_serials={PUMP_SERIAL}) == PUMP2_SERIAL


# --------------------------------------------------------------------------
# A gateway that is gone
# --------------------------------------------------------------------------

class TestGoneGateway:
    async def test_gone_promotes_at_once_and_retires_the_old_connection(self, hass):
        registry, group = await _tank(hass)
        old_connection = group.gateway_connection
        group.members[LIGHT_SERIAL].consecutive_relay_failures = 2

        with _live_rssi({LIGHT_SERIAL: -80}):
            promoted = await registry.record_gateway_failure(PAN_ID, group.generation, gone=True)

        assert promoted is True
        assert group.gateway_serial == LIGHT_SERIAL
        assert old_connection.retired
        assert group.members[LIGHT_SERIAL].consecutive_relay_failures == 0

    async def test_a_retired_connection_refuses_to_reconnect(self, hass):
        connection = MobiusConnectionManager(hass, PUMP_SERIAL, MagicMock())
        connection.retired = True
        with pytest.raises(GatewayReplaced):
            await connection.ensure_connected()

    async def test_a_gateway_not_advertising_raises_device_not_advertising(self, hass):
        connection = MobiusConnectionManager(hass, PUMP_SERIAL, MagicMock())
        with patch(f"{COORDINATOR}._resolve_connectable_ble_device", AsyncMock(return_value=None)):
            with pytest.raises(DeviceNotAdvertising):
                await connection.ensure_connected()

    async def test_gateway_poll_promotes_once_gone_for_long_enough(self, hass):
        registry, group = await _tank(hass)
        coordinator = MobiusDeviceCoordinator(hass, MagicMock(), registry, PUMP_SERIAL, PAN_ID)
        connection = group.gateway_connection

        with patch.object(connection, "ensure_connected", AsyncMock(side_effect=DeviceNotAdvertising("gone"))), \
                patch.object(connection, "lost_for", return_value=5.0), _live_rssi({LIGHT_SERIAL: -80}):
            await coordinator.async_refresh()
        assert group.gateway_serial == PUMP_SERIAL  # not gone long enough yet

        with patch.object(connection, "ensure_connected", AsyncMock(side_effect=DeviceNotAdvertising("gone"))), \
                patch.object(connection, "lost_for", return_value=25.0), _live_rssi({LIGHT_SERIAL: -80}):
            await coordinator.async_refresh()
        assert group.gateway_serial == LIGHT_SERIAL


# --------------------------------------------------------------------------
# Relayed polls while the gateway is down
# --------------------------------------------------------------------------

class TestRelayedPollsWhileGatewayDown:
    async def test_skipped_without_reconnecting_or_counting(self, hass):
        registry, group = await _tank(hass)
        coordinator = MobiusDeviceCoordinator(hass, MagicMock(), registry, LIGHT_SERIAL, PAN_ID)
        ensure = AsyncMock()

        with patch.object(group.gateway_connection, "ensure_connected", ensure):
            await coordinator.async_refresh()

        ensure.assert_not_awaited()
        assert not coordinator.last_update_success
        assert group.members[LIGHT_SERIAL].consecutive_relay_failures == 0

    async def test_relay_failure_of_a_device_not_advertising_is_held_until_the_peer_list_is_read(self, hass):
        registry, group = await _tank(hass)
        _mark_gateway_connected(group)
        entry = MagicMock()
        entry.runtime_data = MobiusRuntimeData(coordinators={})
        entry.runtime_data.last_mesh_refresh = 123.0
        coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP2_SERIAL, PAN_ID)

        with patch(f"{COORDINATOR}._fetch_all", AsyncMock(side_effect=IOError("timed out"))), \
                patch.object(coordinator, "_answers_small_read", AsyncMock(return_value=False)), _advertising():
            for _ in range(RELAY_FAILURE_THRESHOLD):
                await coordinator.async_refresh()

        assert group.members[PUMP2_SERIAL].consecutive_relay_failures == 0
        assert entry.runtime_data.last_mesh_refresh is None

    async def test_relay_failure_counts_when_a_fresh_peer_list_lists_the_device(self, hass):
        registry, group = await _tank(hass)
        _mark_gateway_connected(group)
        registry.update_mesh_peers(PAN_ID, {PUMP_SERIAL, PUMP2_SERIAL}, time.monotonic())
        coordinator = MobiusDeviceCoordinator(hass, MagicMock(), registry, PUMP2_SERIAL, PAN_ID)

        with patch(f"{COORDINATOR}._fetch_all", AsyncMock(side_effect=IOError("timed out"))), \
                patch.object(coordinator, "_answers_small_read", AsyncMock(return_value=False)), _advertising():
            await coordinator.async_refresh()

        assert group.members[PUMP2_SERIAL].consecutive_relay_failures == 1


# --------------------------------------------------------------------------
# Absent devices
# --------------------------------------------------------------------------

class TestAbsentDevices:
    async def test_absent_device_is_not_polled_and_its_failures_are_cleared(self, hass, caplog):
        registry, group = await _tank(hass)
        _mark_gateway_connected(group)
        registry.update_mesh_peers(PAN_ID, {PUMP_SERIAL, LIGHT_SERIAL, LIGHT2_SERIAL}, time.monotonic())
        group.members[PUMP2_SERIAL].consecutive_relay_failures = 2
        coordinator = MobiusDeviceCoordinator(hass, MagicMock(), registry, PUMP2_SERIAL, PAN_ID)
        fetch = AsyncMock()

        with patch(f"{COORDINATOR}._fetch_all", fetch), _advertising(PUMP_SERIAL, LIGHT_SERIAL):
            await coordinator.async_refresh()
            await coordinator.async_refresh()

        fetch.assert_not_awaited()
        assert coordinator.absent
        assert group.members[PUMP2_SERIAL].consecutive_relay_failures == 0
        infos = [r for r in caplog.records if r.levelname == "INFO" and r.message.startswith(f"{PUMP2_SERIAL} is absent")]
        assert len(infos) == 1  # logged once, not every poll

    async def test_device_advertising_is_not_absent_even_when_missing_from_the_peer_list(self, hass):
        registry, group = await _tank(hass)
        registry.update_mesh_peers(PAN_ID, {PUMP_SERIAL}, time.monotonic())
        with _advertising(LIGHT_SERIAL):
            assert not registry.is_absent(PAN_ID, LIGHT_SERIAL)
            assert registry.is_absent(PAN_ID, LIGHT2_SERIAL)

    async def test_nobody_is_absent_before_the_first_peer_list(self, hass):
        registry, _group = await _tank(hass)
        with _advertising():
            assert not registry.is_absent(PAN_ID, PUMP2_SERIAL)

    async def test_an_advertisement_brings_it_back_and_requests_a_poll(self, hass, caplog):
        registry, _group = await _tank(hass)
        entry = MagicMock()
        entry.runtime_data = MobiusRuntimeData(coordinators={})
        entry.runtime_data.last_mesh_refresh = 123.0
        coordinator = MobiusDeviceCoordinator(hass, entry, registry, PUMP2_SERIAL, PAN_ID)
        coordinator.absent = True

        with patch.object(coordinator, "async_request_refresh", AsyncMock()) as refresh:
            coordinator.async_seen_advertising()
            await hass.async_block_till_done()

        assert not coordinator.absent
        refresh.assert_awaited_once()
        assert entry.runtime_data.last_mesh_refresh is None
        assert f"{PUMP2_SERIAL} is back" in caplog.text

    async def test_advertisements_are_routed_to_their_coordinator(self, hass):
        coordinator = MagicMock()
        entry = MagicMock()
        callbacks = []

        def fake_register(hass_, cb, matcher, mode):
            callbacks.append(cb)
            return lambda: None

        with patch("custom_components.mobius.bluetooth.async_register_callback", side_effect=fake_register):
            _async_watch_advertisements(hass, entry, {PUMP_SERIAL: coordinator})

        callbacks[0](_info(REAL_PUMP_PAYLOAD, "AA:AA:AA:AA:AA:AA", received=0.0), None)
        coordinator.async_seen_advertising.assert_called_once()

    async def test_bluetooth_only_device_not_advertising_is_absent(self, hass):
        coordinator = MobiusDeviceCoordinator(
            hass, MagicMock(), _make_registry(hass), "BLADE000000001", PAN_ID, bluetooth_only=True,
        )
        with _advertising(), patch.object(coordinator.direct_connection, "ensure_connected", AsyncMock()) as ensure:
            await coordinator.async_refresh()
        ensure.assert_not_awaited()
        assert coordinator.absent


# --------------------------------------------------------------------------
# Tank check
# --------------------------------------------------------------------------

class TestTankCheck:
    def _entry(self, hass):
        entry = MockConfigEntry(domain=DOMAIN, data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]})
        entry.add_to_hass(hass)
        entry.runtime_data = MobiusRuntimeData(coordinators={})
        return entry

    async def test_does_not_reconnect_a_disconnected_gateway(self, hass):
        _registry, group = _make_registry_with_gateway(hass, PAN_ID, PUMP_SERIAL, [])
        group.gateway_connection._device.is_connected = False
        entry = self._entry(hass)

        with patch("custom_components.mobius._find_in_bluetooth_cache", return_value=None), \
                patch("custom_components.mobius.bluetooth.async_request_active_scan", AsyncMock()):
            await _async_revalidate_tank(hass, entry)

        group.gateway_connection.ensure_connected.assert_not_awaited()

    async def test_stores_the_peer_list(self, hass):
        peer = MagicMock(serial=PUMP_SERIAL, address=None, age=None)
        registry, group = _make_registry_with_gateway(hass, PAN_ID, PUMP_SERIAL, [peer])
        await _async_revalidate_tank(hass, self._entry(hass))
        assert group.mesh_serials == {PUMP_SERIAL}
        assert group.mesh_read_at is not None

    async def test_failure_names_the_gateway_it_read_from(self, hass, caplog):
        import logging
        caplog.set_level(logging.DEBUG, logger="custom_components.mobius")
        registry, group = _make_registry_with_gateway(hass, PAN_ID, PUMP_SERIAL, [])

        async def fail_and_replace(**_kwargs):
            group.gateway_serial = "SOMEONEELSE"
            raise IOError("gone")

        group.gateway_connection._device.discover_mesh_peers_auto = fail_and_replace
        await _async_revalidate_tank(hass, self._entry(hass))

        assert f"from gateway '{PUMP_SERIAL}' failed" in caplog.text


# --------------------------------------------------------------------------
# Missing characteristic: stale service cache
# --------------------------------------------------------------------------

class TestServiceCacheRetry:
    async def test_missing_characteristic_clears_the_cache_and_connects_again(self):
        attempts = []

        class FlakyDevice:
            def __init__(self, ble_device, serial=None, connect_timeout=None):
                pass

            async def connect(self):
                attempts.append(1)
                if len(attempts) == 1:
                    raise BleakCharacteristicNotFoundError("01ff0101-ba5e-f4ee-5ca1-eb1e5e4b1ce0")

        ble_device = MagicMock(address="84:25:3F:AF:F0:A2")
        with patch(f"{COORDINATOR}.MobiusDevice", FlakyDevice), \
                patch(f"{COORDINATOR}.clear_bluez_service_cache", AsyncMock(return_value=True)) as clear:
            device = await _connect(ble_device, LIGHT_SERIAL)

        assert isinstance(device, FlakyDevice)
        assert len(attempts) == 2
        clear.assert_awaited_once_with("84:25:3F:AF:F0:A2")

    async def test_other_errors_are_not_retried(self):
        class FailingDevice:
            def __init__(self, ble_device, serial=None, connect_timeout=None):
                pass

            async def connect(self):
                raise IOError("out of range")

        with patch(f"{COORDINATOR}.MobiusDevice", FailingDevice), \
                patch(f"{COORDINATOR}.clear_bluez_service_cache", AsyncMock()) as clear:
            with pytest.raises(IOError):
                await _connect(MagicMock(address="AA"), LIGHT_SERIAL)
        clear.assert_not_awaited()
