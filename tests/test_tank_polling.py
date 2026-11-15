"""
A tank's polls taking turns, setup not waiting for every device's first
read, the clock drift only from fresh and quick polls, and the schedule
card's reads served from the last poll.
"""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.util import dt as dt_util
from tests.common import MockConfigEntry

from mobius import C2Attribute, ConfigurationCache, FullPollResult, MetadataSnapshot, PrimitiveType, SchedulePoint
from mobius.frame import AttributeValue

from custom_components.mobius import MobiusRuntimeData, _clock_problems
from custom_components.mobius import coordinator as coord
from custom_components.mobius.const import CONF_DEVICES, CONF_MLPREFIX, CONF_PAN_ID, CONF_SERIAL, DOMAIN
from custom_components.mobius.coordinator import MobiusDeviceCoordinator
from custom_components.mobius.gateway_registry import GatewayRegistry
from custom_components.mobius.services import _after_write
from custom_components.mobius.websocket_api import _cached_schedule1, _schedule1

PAN_ID = 0x3D0F
GATEWAY = "GATEWAY0000001"
MEMBERS = ["MEMBER00000001", "MEMBER00000002", "MEMBER00000003"]
ADDRESS = bytes.fromhex("fd11223344556677000000fffe001234")


# --- polls take turns --------------------------------------------------------


async def test_a_tanks_devices_are_polled_one_at_a_time(hass):
    registry = GatewayRegistry(hass, asyncio.Semaphore(2), election_settle_seconds=0.01)
    await registry.join(PAN_ID, GATEWAY, rssi=-40)
    for serial in MEMBERS:
        await registry.join(PAN_ID, serial, rssi=-80)
        registry.update_mesh_address(PAN_ID, serial, ADDRESS)
    group = registry.group(PAN_ID)
    group.gateway_connection._device = MagicMock(is_connected=True)

    running = 0
    most_running = 0

    async def fake_fetch_all(device, **kwargs):
        nonlocal running, most_running
        running += 1
        most_running = max(most_running, running)
        await asyncio.sleep(0.01)
        running -= 1
        return {"support": "pump", "hardware_info": {}}, set(), True, PrimitiveType.VorTechV1, None

    coordinators = [MobiusDeviceCoordinator(hass, MagicMock(), registry, s, PAN_ID) for s in MEMBERS]
    with patch.object(group.gateway_connection, "ensure_connected", AsyncMock(return_value=MagicMock())), \
         patch("custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=MagicMock()), \
         patch("custom_components.mobius.coordinator._fetch_all", side_effect=fake_fetch_all):
        await asyncio.gather(*(c.async_refresh() for c in coordinators))

    assert all(c.last_update_success for c in coordinators)
    assert most_running == 1


# --- setup ------------------------------------------------------------------


async def test_setup_does_not_wait_for_the_other_devices_first_read(hass):
    """Only the device that decides setup is read during it; the others
    are read afterwards, so a slow or unreachable one can't hold up Home
    Assistant's startup."""
    from mobius import MeshPeer, Tank

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: "fd11223344556677",
              CONF_DEVICES: [{CONF_SERIAL: GATEWAY}, {CONF_SERIAL: MEMBERS[0]}]},
        unique_id="fd11223344556677",
    )
    entry.add_to_hass(hass)

    release = asyncio.Event()
    started = []

    async def slow_soft_refresh(coordinator, serial):
        started.append(serial)
        await release.wait()

    tank = Tank(prefix=ADDRESS[:8], peers=[MeshPeer(serial=GATEWAY, address=ADDRESS),
                                           MeshPeer(serial=MEMBERS[0], address=ADDRESS)])
    with patch("custom_components.mobius.discover_tank_for_serial", AsyncMock(return_value=tank)), \
         patch("custom_components.mobius._current_rssi", side_effect=lambda h, s: -40 if s == GATEWAY else -80), \
         patch("custom_components.mobius.advertised_as_bluetooth_only", return_value=False), \
         patch.object(MobiusDeviceCoordinator, "async_config_entry_first_refresh", AsyncMock()), \
         patch("custom_components.mobius._async_soft_first_refresh", side_effect=slow_soft_refresh), \
         patch("homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock()):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        # Setup returned while the other device's first read is still running.
        await asyncio.sleep(0)
        assert started == [MEMBERS[0]]
        assert set(entry.runtime_data.coordinators) == {GATEWAY, MEMBERS[0]}
        release.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    await hass.config_entries.async_unload(entry.entry_id)


# --- clock drift ------------------------------------------------------------


def _poll_device(epoch: int):
    device = MagicMock()
    device.serial = GATEWAY
    device.get_supported_attributes = AsyncMock(return_value=[])
    device.get_full_poll_batch = AsyncMock(return_value=FullPollResult(
        device_info={"serial": GATEWAY, "primitive_type": None},
        metadata=MetadataSnapshot(
            advanced_features=None, calibration=None, hardware_info={}, firmware_versions={},
            supported_channels=[], error_state=None, epoch=epoch, local_time=None, tz_offset=None,
        ),
        light_poll=None, pump_telemetry=None,
    ))
    return device


@pytest.mark.parametrize("poll_seconds, expected_drift", [(2.0, 100), (30.0, None)])
async def test_clock_drift_is_only_taken_from_a_quick_poll(poll_seconds, expected_drift):
    fake_time = MagicMock()
    fake_time.time.return_value = 1_000_100.4
    fake_time.monotonic.side_effect = [500.0, 500.0 + poll_seconds]
    with patch("custom_components.mobius.coordinator.time", fake_time):
        info, *_ = await coord._fetch_all(_poll_device(epoch=1_000_000))
    assert info["clock_drift"] == expected_drift


def test_kept_data_from_failed_polls_doesnt_count_for_the_clock_check(hass):
    current, kept = MagicMock(data_is_current=True), MagicMock(data_is_current=False)
    current.data = {"clock_drift": 0, "olson_tz": hass.config.time_zone}
    kept.data = {"clock_drift": 500, "olson_tz": "America/New_York"}
    runtime = MobiusRuntimeData(coordinators={GATEWAY: current, MEMBERS[0]: kept})

    assert _clock_problems(hass, runtime) == (None, None)

    kept.data_is_current = True
    worst, mismatch = _clock_problems(hass, runtime)
    assert worst == (MEMBERS[0], 500) and mismatch == (MEMBERS[0], "America/New_York")


# --- schedule reads ---------------------------------------------------------


def _schedule_coordinator(hass, polled_ago: timedelta, current: bool = True):
    coordinator = MagicMock()
    coordinator.hass = hass
    coordinator.serial = GATEWAY
    coordinator.data_is_current = current
    coordinator._last_success = dt_util.utcnow() - polled_ago
    coordinator.configuration_cache = ConfigurationCache()
    point = SchedulePoint.parse(bytes.fromhex("e00101") + bytes([2]) + (500).to_bytes(2, "little"))
    coordinator.configuration_cache.entries[int(C2Attribute.Schedule1)] = (
        0x1234, [AttributeValue(int(C2Attribute.Schedule1), 0, [point.encode()])],
    )
    live = MagicMock()
    live.get_light_schedule = AsyncMock(return_value=["live"])
    coordinator.async_get_connected_device = AsyncMock(return_value=live)
    coordinator.async_request_refresh = AsyncMock()
    return coordinator, point


async def test_the_card_gets_the_schedule_of_a_recent_poll(hass):
    coordinator, point = _schedule_coordinator(hass, timedelta(seconds=10))
    assert await _schedule1(coordinator, is_light=True) == [point]
    coordinator.async_get_connected_device.assert_not_awaited()


@pytest.mark.parametrize("polled_ago, current", [(timedelta(minutes=5), True), (timedelta(seconds=10), False)])
async def test_an_old_or_kept_schedule_is_read_from_the_device(hass, polled_ago, current):
    coordinator, _point = _schedule_coordinator(hass, polled_ago, current)
    assert _cached_schedule1(coordinator, is_light=True) is None
    assert await _schedule1(coordinator, is_light=True) == ["live"]


async def test_a_write_drops_the_cached_schedule_and_refreshes(hass):
    coordinator, _point = _schedule_coordinator(hass, timedelta(seconds=10))

    _after_write(coordinator)
    await hass.async_block_till_done()

    assert int(C2Attribute.Schedule1) not in coordinator.configuration_cache.entries
    assert await _schedule1(coordinator, is_light=True) == ["live"]
    coordinator.async_request_refresh.assert_awaited_once()
