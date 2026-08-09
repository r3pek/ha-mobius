"""
Tests for custom_components.mobius's top-level async_setup/
async_setup_entry/async_unload_entry -- focused on the fail-fast error
paths (missing device list, missing pan_id) not exercised by
test_sensor.py's full happy-path setup/unload coverage, plus that
async_setup() creates the shared gateway registry, and the multi-device
tank behaviors (prefer_as_gateway, proactive discovery for every device
including the gateway, synthetic tank device registration).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius import async_setup, async_setup_entry, tank_device_identifier
from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX
from custom_components.mobius.gateway_registry import GatewayRegistry

PUMP_ADDRESS = "E4:67:D8:17:84:83"
PUMP_SERIAL = "76517731952041"
LIGHT_SERIAL = "7V4Z00F143RBED"
PAN_ID = 0x3D0F
MLPREFIX_HEX = "fd1c5ec780e35c01"


@pytest.fixture(autouse=True)
def _stub_periodic_tank_revalidation():
    """
    Every test in this file exercises async_setup_entry() directly
    (not via hass.config_entries.async_setup(), which would properly
    track and unload the entry at test teardown) -- meaning the real
    periodic timer async_setup_entry() registers via
    entry.async_on_unload()/async_track_time_interval() never gets its
    own unsub called, and the test harness (correctly) flags that as a
    lingering timer. This isn't a real bug: in actual Home Assistant
    usage, entry.async_on_unload() fires normally on unload/reload --
    it's specifically these tests' own shortcut of calling
    async_setup_entry() directly, bypassing that lifecycle, that leaves
    the timer dangling. Stubbing the registration itself out here is
    simpler and more robust than adding proper teardown to all 8+
    affected tests individually, and doesn't test anything about the
    periodic-revalidation feature itself, which has its own dedicated
    tests elsewhere.
    """
    with patch("custom_components.mobius.async_track_time_interval", return_value=lambda: None):
        yield


async def test_async_setup_creates_shared_gateway_registry(hass):
    assert await async_setup(hass, {})
    assert isinstance(hass.data[DOMAIN]["gateway_registry"], GatewayRegistry)


async def test_async_setup_is_idempotent(hass):
    """Called once per Home Assistant startup, but must not blow away an
    already-populated registry if somehow invoked twice (e.g. platform
    reload edge cases)."""
    await async_setup(hass, {})
    registry_first = hass.data[DOMAIN]["gateway_registry"]
    await async_setup(hass, {})
    assert hass.data[DOMAIN]["gateway_registry"] is registry_first


async def test_setup_entry_fails_fast_without_devices(hass):
    """Entries created before tank-aware, CONF_DEVICES-based entries
    existed (the old shape stored a single device's own serial/address
    directly at the top level) have no device list at all -- there's no
    safe, automatic way to migrate that shape forward, so this must fail
    clearly rather than silently produce a tank with zero devices."""
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_PAN_ID: PAN_ID}, unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryError, match="device"):
        await async_setup_entry(hass, entry)


async def test_setup_entry_fails_fast_without_pan_id(hass):
    """Entries created before pan_id-based gateway grouping was added
    have a device list but not CONF_PAN_ID -- there's no safe way to know
    which group to join without it, so this must also fail clearly rather
    than silently skip gateway grouping."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    with pytest.raises(ConfigEntryError, match="pan_id"):
        await async_setup_entry(hass, entry)


def _fake_pump_device():
    fake_device = MagicMock()
    fake_device.get_device_info = AsyncMock(return_value={
        "model_raw": 42, "model": "VorTechMP40wG3QD", "manufacturer": "EcoTech Marine",
        "name": "Pump", "serial": PUMP_SERIAL, "primitive_type": "VorTechV1",
        "error_state": "NoError", "mac_address": None,
    })
    fake_device.get_pump_telemetry = AsyncMock(return_value={"speed": 100, "speed_percent": 10.0, "gph": 500})
    fake_device.get_operation_state = AsyncMock()
    fake_device.get_operation_state.return_value.name = "Schedule"
    fake_point = MagicMock()
    fake_point.pump.mode.name = "TidalSwell"
    fake_point.pump.params = {}
    fake_device.get_pump_schedule = AsyncMock(return_value=[fake_point])
    fake_device.get_current_pump_block = AsyncMock(return_value=fake_point)
    fake_device.get_firmware_versions = AsyncMock(return_value={"Product OS": "1.0"})
    fake_device.get_hardware_info = AsyncMock(return_value={"Revision": 2})
    return fake_device


async def test_setup_entry_joins_registry_with_current_rssi(hass):
    """Confirms async_setup_entry() actually looks up the device's
    current RSSI (from Home Assistant's own Bluetooth cache) and passes
    it into the registry join -- needed for gateway election to have
    anything to work with."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}],
        },
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    fake_device = _fake_pump_device()
    address = bytes.fromhex("fd11223344556677000000fffe001234")

    with patch(
        "custom_components.mobius._current_rssi", return_value=-42,
    ) as mock_rssi, patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=address),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ):
        await async_setup_entry(hass, entry)

    mock_rssi.assert_called_once_with(hass, PUMP_SERIAL)
    registry = hass.data[DOMAIN]["gateway_registry"]
    group = registry.group(PAN_ID)
    assert group is not None
    assert group.members[PUMP_SERIAL].rssi == -42
    # The single device in a one-device entry trivially becomes gateway
    # either way, but this confirms prefer_as_gateway's effect wasn't
    # accidentally broken for the ordinary single-device case either.
    assert group.gateway_serial == PUMP_SERIAL


# --------------------------------------------------------------------------
# Proactive mesh address discovery at setup -- runs for EVERY device
# (gateway included -- a deliberate change; see __init__.py's own
# async_setup_entry() docstring for why the gateway needed this too, since
# nothing else in normal operation ever populates a gateway's own address
# in the registry) on every entry setup, which covers both a brand-new
# device AND every existing device on every Home Assistant restart.
# --------------------------------------------------------------------------

async def test_gateway_device_also_gets_proactive_discovery(hass):
    """The actual point of the deliberate change from an earlier version
    of this code (which skipped this for the gateway): the new
    MeshAddressSensor (sensor.py) needs every device's address cached,
    gateway included, since nothing else ever populates the gateway's
    own address in the registry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}],
        },
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    fake_device = _fake_pump_device()
    address = bytes.fromhex("fd11223344556677000000fffe001234")

    with patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=address),
    ) as mock_discover, patch(
        "custom_components.mobius._current_rssi", return_value=-40,
    ), patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await async_setup_entry(hass, entry)

    mock_discover.assert_called_once_with(hass, PUMP_SERIAL, hass.data[DOMAIN]["connection_semaphore"])
    registry = hass.data[DOMAIN]["gateway_registry"]
    assert registry.group(PAN_ID).members[PUMP_SERIAL].mesh_address == address


async def test_relayed_device_gets_proactive_discovery_at_setup(hass):
    """A device that ISN'T going to be gateway should have its mesh
    address discovered and cached before the first poll cycle, not left
    entirely to the coordinator's on-demand fallback."""
    # Pre-populate the registry with an existing gateway for this pan_id,
    # so the entry being set up here joins as a relayed member instead.
    hass.data.setdefault(DOMAIN, {})
    semaphore = hass.data[DOMAIN].setdefault("connection_semaphore", asyncio.Semaphore(2))
    registry = GatewayRegistry(hass, semaphore, election_settle_seconds=0.01)
    hass.data[DOMAIN]["gateway_registry"] = registry
    await registry.join(PAN_ID, "existing-gateway-serial", rssi=-30)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}],
        },
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    address = bytes.fromhex("fd11223344556677000000fffe001234")
    fake_relayed_device = _fake_pump_device()

    with patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=address),
    ) as mock_discover, patch(
        "custom_components.mobius._current_rssi", return_value=-80,
    ), patch.object(
        registry.group(PAN_ID).gateway_connection, "ensure_connected", AsyncMock(return_value=object()),
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_relayed_device,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await async_setup_entry(hass, entry)

    mock_discover.assert_called_once()
    assert mock_discover.call_args[0][0] is hass
    assert mock_discover.call_args[0][1] == PUMP_SERIAL
    assert registry.group(PAN_ID).members[PUMP_SERIAL].mesh_address == address


async def test_secondary_device_proactive_discovery_failure_does_not_break_setup(hass):
    """A None result (device not currently reachable for its OWN
    proactive mesh-address caching -- distinct from the probe phase
    that already found a different, working device) must not raise or
    otherwise derail setup -- the coordinator's own on-demand fallback
    covers it on a later poll, and this device's own soft
    coordinator.async_refresh() (see async_setup_entry()'s own
    docstring) doesn't propagate failure upward regardless."""
    hass.data.setdefault(DOMAIN, {})
    semaphore = hass.data[DOMAIN].setdefault("connection_semaphore", asyncio.Semaphore(2))
    registry = GatewayRegistry(hass, semaphore, election_settle_seconds=0.01)
    hass.data[DOMAIN]["gateway_registry"] = registry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [
                {CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS},
                {CONF_SERIAL: LIGHT_SERIAL},
            ],
        },
        unique_id=PAN_ID,
    )
    entry.add_to_hass(hass)

    fake_device = _fake_pump_device()
    working_address = bytes.fromhex("fd11223344556677000000fffe001234")

    async def fake_discover_mesh_address(hass, serial, semaphore):
        # PUMP_SERIAL is the one the probe phase finds working; LIGHT_SERIAL
        # can't be reached at all, for its own proactive caching in the
        # main loop specifically.
        return working_address if serial == PUMP_SERIAL else None

    with patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(side_effect=fake_discover_mesh_address),
    ) as mock_discover, patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice",
        return_value=fake_device,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        result = await async_setup_entry(hass, entry)

    assert result is True
    # LIGHT_SERIAL's own proactive discovery genuinely ran and failed --
    # this isn't testing a call that never happened.
    light_calls = [c for c in mock_discover.call_args_list if c[0][1] == LIGHT_SERIAL]
    assert len(light_calls) == 1
    assert registry.group(PAN_ID).members[LIGHT_SERIAL].mesh_address is None
    # And PUMP_SERIAL -- the one the probe phase found working -- IS cached.
    assert registry.group(PAN_ID).members[PUMP_SERIAL].mesh_address == working_address


async def test_secondary_device_skips_redundant_discovery_when_already_cached(hass):
    """Avoids redundant work for the MAIN loop's secondary devices (not
    the probe phase, which always freshly tests each candidate's CURRENT
    reachability before committing to a gateway -- see async_setup_entry()'s
    own docstring for why that must never trust a possibly-stale cached
    address instead). If a non-probe-winning device's own mesh address is
    already cached (e.g. a pre-existing group from a sibling entry or an
    earlier partial setup), don't re-discover it in the main loop."""
    from custom_components.mobius.gateway_registry import PanGroup, MemberState
    from custom_components.mobius.coordinator import MobiusConnectionManager

    hass.data.setdefault(DOMAIN, {})
    semaphore = hass.data[DOMAIN].setdefault("connection_semaphore", asyncio.Semaphore(2))
    registry = GatewayRegistry(hass, semaphore, election_settle_seconds=0.01)
    hass.data[DOMAIN]["gateway_registry"] = registry

    cached_address = bytes.fromhex("fd11223344556677000000fffe005678")
    prebuilt_group = PanGroup(
        pan_id=PAN_ID, gateway_serial=PUMP_SERIAL,
        gateway_connection=MobiusConnectionManager(hass, PUMP_SERIAL, semaphore),
    )
    prebuilt_group.members[PUMP_SERIAL] = MemberState(serial=PUMP_SERIAL)
    # LIGHT_SERIAL (the secondary device) already has a cached address --
    # this is what must NOT trigger a redundant discover_mesh_address()
    # call in the main loop.
    prebuilt_group.members[LIGHT_SERIAL] = MemberState(serial=LIGHT_SERIAL, mesh_address=cached_address)
    prebuilt_group._gateway_elected.set()
    registry._groups[PAN_ID] = prebuilt_group

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [
                {CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS},
                {CONF_SERIAL: LIGHT_SERIAL},
            ],
        },
        unique_id="fake-unique-id-for-secondary-cache-test",
    )
    entry.add_to_hass(hass)

    fake_pump_device = _fake_pump_device()
    working_address = bytes.fromhex("fd11223344556677000000fffe001234")

    async def fake_discover_mesh_address(hass, serial, semaphore):
        # Only PUMP_SERIAL's own discovery should ever be called at all
        # -- once, for the probe phase.
        assert serial == PUMP_SERIAL
        return working_address

    with patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(side_effect=fake_discover_mesh_address),
    ) as mock_discover, patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_pump_device),
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice",
        return_value=_fake_pump_device(),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await async_setup_entry(hass, entry)

    # discover_mesh_address was called exactly once -- for PUMP_SERIAL's
    # own probe -- never for LIGHT_SERIAL, whose address was already
    # cached.
    assert mock_discover.call_count == 1
    assert mock_discover.call_args[0][1] == PUMP_SERIAL
    assert prebuilt_group.members[LIGHT_SERIAL].mesh_address == cached_address


# --------------------------------------------------------------------------
# The core setup-reliability fix: ONE unreachable device must never block
# the whole entry's setup. A REAL, CONFIRMED production bug lived here --
# coordinator.async_config_entry_first_refresh() (raises ConfigEntryNotReady
# on failure) was awaited once per device in a loop, so ANY single device
# out of several failing -- even the last of many, even on a retry after
# every other device had already succeeded -- aborted the ENTIRE entry's
# setup. Every subsequent retry re-ran the whole loop from scratch,
# discarding devices that had already succeeded. A tank with one
# consistently-unreachable device (poor relay range to that one target,
# mid-reboot, etc) could never fully set up at all -- confirmed via a real
# log showing a different device failing on each retry, with the entry
# stuck in "not ready yet" indefinitely.
# --------------------------------------------------------------------------

async def test_one_unreachable_device_does_not_block_the_whole_tank(hass):
    """The actual bug fix, end to end: LIGHT_SERIAL can't be reached at
    all (its own discover_mesh_address AND its own coordinator refresh
    both fail), but PUMP_SERIAL is reachable -- the entry must still
    set up successfully, with LIGHT_SERIAL's own coordinator simply
    starting in a failed (not blocking) state."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX_HEX,
            CONF_DEVICES: [
                {CONF_SERIAL: PUMP_SERIAL}, {CONF_SERIAL: LIGHT_SERIAL},
            ],
        },
        unique_id=MLPREFIX_HEX,
    )
    entry.add_to_hass(hass)

    fake_pump_device = _fake_pump_device()
    working_address = bytes.fromhex("fd11223344556677000000fffe001234")

    async def fake_discover_mesh_address(hass, serial, semaphore):
        if serial == PUMP_SERIAL:
            return working_address
        return None  # LIGHT_SERIAL is completely unreachable

    async def fake_ensure_connected(self):
        # The gateway connection manager's own serial tells us which
        # device this is for.
        if self.serial == PUMP_SERIAL:
            return fake_pump_device
        raise Exception("LIGHT_SERIAL is unreachable")

    with patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(side_effect=fake_discover_mesh_address),
    ), patch(
        "custom_components.mobius._current_rssi",
        side_effect=lambda hass, serial: -40 if serial == PUMP_SERIAL else -90,
    ), patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        fake_ensure_connected,
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice",
        side_effect=Exception("LIGHT_SERIAL cannot be relayed to either"),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        result = await async_setup_entry(hass, entry)

    # The entry itself sets up successfully -- the core point of this fix.
    assert result is True
    assert set(entry.runtime_data.coordinators.keys()) == {PUMP_SERIAL, LIGHT_SERIAL}
    # PUMP_SERIAL (the one that's actually reachable) succeeded.
    assert entry.runtime_data.coordinators[PUMP_SERIAL].last_update_success is True
    # LIGHT_SERIAL's own coordinator exists (its entities will simply show
    # unavailable) but its failure never propagated up to block setup.
    assert entry.runtime_data.coordinators[LIGHT_SERIAL].last_update_success is False


async def test_probe_tries_devices_in_rssi_order_not_list_order(hass):
    """The probe phase tries the STRONGEST-signal device first, not just
    whichever happens to be first in CONF_DEVICES -- confirms a
    consistently-unreachable device listed first doesn't get retried
    forever while a perfectly reachable one (listed later) sits unused."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX_HEX,
            CONF_DEVICES: [
                {CONF_SERIAL: LIGHT_SERIAL},  # listed FIRST, but weak/unreachable
                {CONF_SERIAL: PUMP_SERIAL},   # listed second, but strong/reachable
            ],
        },
        unique_id=MLPREFIX_HEX,
    )
    entry.add_to_hass(hass)

    fake_pump_device = _fake_pump_device()
    working_address = bytes.fromhex("fd11223344556677000000fffe001234")
    probed_order = []

    async def fake_discover_mesh_address(hass, serial, semaphore):
        probed_order.append(serial)
        return working_address if serial == PUMP_SERIAL else None

    async def fake_ensure_connected(self):
        return fake_pump_device

    with patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(side_effect=fake_discover_mesh_address),
    ), patch(
        "custom_components.mobius._current_rssi",
        # PUMP_SERIAL has the much stronger signal, despite being listed second.
        side_effect=lambda hass, serial: -30 if serial == PUMP_SERIAL else -95,
    ), patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        fake_ensure_connected,
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_pump_device,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await async_setup_entry(hass, entry)

    # PUMP_SERIAL (stronger RSSI) was probed FIRST, despite being listed
    # second in CONF_DEVICES.
    assert probed_order[0] == PUMP_SERIAL


async def test_setup_raises_not_ready_only_if_every_device_is_unreachable(hass):
    """The one case this entry's own readiness legitimately SHOULD
    depend on: if truly nothing in the tank can be reached at all,
    there's genuinely nothing to set up yet."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX_HEX,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}, {CONF_SERIAL: LIGHT_SERIAL}],
        },
        unique_id=MLPREFIX_HEX,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=None),
    ), patch(
        "custom_components.mobius._current_rssi", return_value=-90,
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)

    # No coordinators were ever committed to -- this entry has nothing
    # usable at all yet.
    assert not hasattr(entry, "runtime_data") or entry.runtime_data is None


# --------------------------------------------------------------------------
# Multi-device tank entries -- one entry, N coordinators, plus the
# synthetic tank device for via_device grouping.
# --------------------------------------------------------------------------

async def test_multi_device_tank_creates_one_coordinator_per_device(hass):
    """The core point of tank-aware entries: a single entry with N
    devices ends up with N coordinators in runtime.coordinators, not
    just one."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX_HEX,
            CONF_DEVICES: [
                {CONF_SERIAL: PUMP_SERIAL}, {CONF_SERIAL: LIGHT_SERIAL},
            ],
        },
        unique_id=MLPREFIX_HEX,
    )
    entry.add_to_hass(hass)

    fake_device = _fake_pump_device()
    address = bytes.fromhex("fd11223344556677000000fffe001234")

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=address),
    ), patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_device,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await async_setup_entry(hass, entry)

    assert set(entry.runtime_data.coordinators.keys()) == {PUMP_SERIAL, LIGHT_SERIAL}


async def test_first_device_in_list_is_preferred_as_gateway(hass):
    """The first device in CONF_DEVICES is always the one the config
    flow actually connected to (see __init__.py's own async_setup_entry()
    docstring) -- confirms it actually becomes gateway, not whichever
    device happens to have the best RSSI."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX_HEX,
            CONF_DEVICES: [
                {CONF_SERIAL: PUMP_SERIAL}, {CONF_SERIAL: LIGHT_SERIAL},
            ],
        },
        unique_id=MLPREFIX_HEX,
    )
    entry.add_to_hass(hass)

    fake_device = _fake_pump_device()
    address = bytes.fromhex("fd11223344556677000000fffe001234")

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=address),
    ), patch(
        # LIGHT_SERIAL has the stronger RSSI -- if prefer_as_gateway
        # weren't working, plain RSSI-based election would pick it
        # instead of PUMP_SERIAL (the first-listed device).
        "custom_components.mobius._current_rssi",
        side_effect=lambda hass, serial: -10 if serial == LIGHT_SERIAL else -80,
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_device,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await async_setup_entry(hass, entry)

    registry = hass.data[DOMAIN]["gateway_registry"]
    assert registry.group(PAN_ID).gateway_serial == PUMP_SERIAL


async def test_multi_device_tank_registers_synthetic_tank_device(hass):
    """A genuine multi-device tank gets a synthetic device registered
    for via_device grouping -- see __init__.py's own
    _register_tank_device()/tank_device_identifier()."""
    from homeassistant.helpers import device_registry as dr

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX_HEX,
            CONF_DEVICES: [
                {CONF_SERIAL: PUMP_SERIAL}, {CONF_SERIAL: LIGHT_SERIAL},
            ],
        },
        unique_id=MLPREFIX_HEX,
        title="Mobius Tank (2 devices)",
    )
    entry.add_to_hass(hass)

    fake_device = _fake_pump_device()
    address = bytes.fromhex("fd11223344556677000000fffe001234")

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=address),
    ), patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_device,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await async_setup_entry(hass, entry)

    device_registry = dr.async_get(hass)
    tank_device = device_registry.async_get_device(identifiers={tank_device_identifier(MLPREFIX_HEX)})
    assert tank_device is not None
    assert tank_device.name == "Mobius Tank (2 devices)"


async def test_single_device_ad_hoc_entry_skips_synthetic_tank_device(hass):
    """A single, ad-hoc device (no CONF_MLPREFIX at all) must NOT get a
    synthetic tank device -- a "hub" with one (or zero real) child
    devices would just be UI noise, not useful grouping. Only checks
    what __init__.py itself directly controls (_register_tank_device()
    is called, or isn't) -- the real device's own registration happens
    via the sensor platform's own DeviceInfo, which async_forward_
    entry_setups is mocked away here, so it's out of scope for this
    specific test (covered instead by test_sensor.py's own full
    happy-path setup)."""
    from homeassistant.helpers import device_registry as dr

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}],
        },
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    fake_device = _fake_pump_device()
    address = bytes.fromhex("fd11223344556677000000fffe001234")

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=address),
    ), patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await async_setup_entry(hass, entry)

    device_registry = dr.async_get(hass)
    devices_for_entry = [
        d for d in device_registry.devices.values() if entry.entry_id in d.config_entries
    ]
    # No synthetic tank device was registered at all -- with the real
    # sensor platform mocked away, this list should be empty (not "the
    # one real device"), since nothing in __init__.py itself registers
    # the real device directly.
    assert devices_for_entry == []


# --------------------------------------------------------------------------
# CONFIG_SCHEMA -- required by hassfest for any integration implementing
# async_setup (confirmed via a real hassfest finding: "Integrations which
# implement 'async_setup' or 'setup' must define ... CONFIG_SCHEMA ...").
# This integration is config-entry-only (config_flow: true, no YAML
# configuration.yaml support), so cv.config_entry_only_config_schema is
# the confirmed-correct helper -- not just "define something to satisfy
# the linter", it also gives a real, clear error if someone tries to
# configure this integration via YAML anyway.
# --------------------------------------------------------------------------

def test_config_schema_is_config_entry_only(caplog):
    from custom_components.mobius import CONFIG_SCHEMA

    assert CONFIG_SCHEMA is not None
    # A bare {"mobius": {}} (as if someone wrote this in configuration.yaml)
    # must be flagged as unsupported -- confirms this actually enforces
    # "config entries only", not just present to silence hassfest without
    # doing anything. Confirmed via real behavior: this doesn't raise (an
    # earlier version of this test assumed it would) -- it logs a clear
    # error and creates a repairs issue instead, which is the actual,
    # more user-friendly way cv.config_entry_only_config_schema handles
    # this, surfaced in Home Assistant's own Repairs UI rather than
    # crashing startup outright.
    result = CONFIG_SCHEMA({DOMAIN: {}})
    assert result == {DOMAIN: {}}  # passed through unchanged, not stripped or rejected
    assert "does not support YAML setup" in caplog.text


def test_config_schema_allows_no_mobius_key_at_all():
    """The common case: mobius isn't mentioned in configuration.yaml at
    all (the expected setup, since it's added via the UI) -- must not be
    rejected just because the integration exists."""
    from custom_components.mobius import CONFIG_SCHEMA

    # Should not raise -- an empty overall config, or one where "mobius"
    # simply isn't a key, is exactly what's expected for a config-entry-
    # only integration.
    CONFIG_SCHEMA({})


# --------------------------------------------------------------------------
# _async_revalidate_tank() -- the periodic per-tank membership check. See
# its own docstring for the full reasoning; these tests confirm each of
# its three real outcomes (auto-migrate a device found on a different,
# already-tracked tank's mesh; never auto-remove one that's simply
# missing; ignore a genuinely new, untracked device) plus its fail-soft
# behavior.
# --------------------------------------------------------------------------

from custom_components.mobius import _async_revalidate_tank
from custom_components.mobius.const import CONF_MLPREFIX
from custom_components.mobius.coordinator import MobiusConnectionManager
from custom_components.mobius.gateway_registry import PanGroup, MemberState
from mobius import MeshPeer, Model

OTHER_PAN_ID = 0x1234
OTHER_MLPREFIX_HEX = "aabbccddeeff0011"


def _make_registry_with_gateway(hass, pan_id, gateway_serial, peers_to_return):
    """A GatewayRegistry with one already-elected group, whose gateway
    connection is mocked to return a controlled peer list from
    discover_mesh_peers_auto() -- avoids any real BLE connection."""
    semaphore = asyncio.Semaphore(2)
    registry = GatewayRegistry(hass, semaphore, election_settle_seconds=0.01)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["gateway_registry"] = registry
    hass.data[DOMAIN]["connection_semaphore"] = semaphore

    connection = MobiusConnectionManager(hass, gateway_serial, semaphore)
    fake_device = MagicMock()
    fake_device.discover_mesh_peers_auto = AsyncMock(return_value=peers_to_return)
    connection.ensure_connected = AsyncMock(return_value=fake_device)

    group = PanGroup(pan_id=pan_id, gateway_serial=gateway_serial, gateway_connection=connection)
    group.members[gateway_serial] = MemberState(serial=gateway_serial)
    registry._groups[pan_id] = group
    return registry, group


def _fake_peer(serial: str) -> MeshPeer:
    return MeshPeer(
        serial=serial, model_raw=42, model=Model.VorTechMP40wG3QD,
        short_address=0x1234, address=b"\x00" * 16,
    )


async def test_revalidate_skips_cleanly_if_no_gateway_elected(hass):
    """No group at all, or a group with no elected gateway yet -- both
    just skip this run quietly, nothing to check against."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["gateway_registry"] = GatewayRegistry(hass, asyncio.Semaphore(2))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    await _async_revalidate_tank(hass, entry)  # must not raise


async def test_revalidate_skips_cleanly_on_connection_failure(hass):
    """A failed check (gateway unreachable, read timeout) is logged and
    skipped, not raised -- the next scheduled run is its own retry."""
    from custom_components.mobius.gateway_registry import MemberState

    semaphore = asyncio.Semaphore(2)
    registry = GatewayRegistry(hass, semaphore, election_settle_seconds=0.01)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["gateway_registry"] = registry
    connection = MobiusConnectionManager(hass, PUMP_SERIAL, semaphore)
    connection.ensure_connected = AsyncMock(side_effect=Exception("boom"))
    group = PanGroup(pan_id=PAN_ID, gateway_serial=PUMP_SERIAL, gateway_connection=connection)
    group.members[PUMP_SERIAL] = MemberState(serial=PUMP_SERIAL)
    registry._groups[PAN_ID] = group

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    await _async_revalidate_tank(hass, entry)  # must not raise


async def test_revalidate_ignores_genuinely_new_untracked_device(hass):
    """A peer reported on this mesh that isn't tracked ANYWHERE (not
    this entry, not any other) is left alone entirely -- discovering
    brand-new devices is config_flow.py's own job, not this one's."""
    registry, group = _make_registry_with_gateway(
        hass, PAN_ID, PUMP_SERIAL, [_fake_peer("unrelated-untracked-serial")],
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    with patch("homeassistant.config_entries.ConfigEntries.async_reload", AsyncMock()) as mock_reload:
        await _async_revalidate_tank(hass, entry)

    mock_reload.assert_not_called()
    assert entry.data[CONF_DEVICES] == [{CONF_SERIAL: PUMP_SERIAL}]  # untouched


async def test_revalidate_never_removes_a_missing_device(hass):
    """The actual core guarantee from this feature's own design
    decision: a tracked device that simply isn't reported by this scan
    is left exactly where it is, permanently -- never auto-removed."""
    registry, group = _make_registry_with_gateway(hass, PAN_ID, PUMP_SERIAL, [])  # empty: nothing reported
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}, {CONF_SERIAL: LIGHT_SERIAL}],
        },
        unique_id=PAN_ID,
    )
    entry.add_to_hass(hass)

    with patch("homeassistant.config_entries.ConfigEntries.async_reload", AsyncMock()) as mock_reload:
        await _async_revalidate_tank(hass, entry)

    mock_reload.assert_not_called()
    stored_serials = {d[CONF_SERIAL] for d in entry.data[CONF_DEVICES]}
    assert stored_serials == {PUMP_SERIAL, LIGHT_SERIAL}  # both still there


async def test_revalidate_auto_migrates_device_found_on_different_tracked_tank(hass):
    """The actual core new behavior: a device tracked under one entry,
    now reported on a DIFFERENT, already-tracked entry's own mesh, gets
    silently moved -- removed from the old entry, added to the new one,
    both reloaded. No prompt, matching the same "merge, don't
    re-prompt" philosophy discovery-time merging already uses."""
    # LIGHT_SERIAL is reported on PUMP's own tank's mesh now, despite
    # being tracked under a completely separate, pre-existing entry.
    registry, group = _make_registry_with_gateway(
        hass, PAN_ID, PUMP_SERIAL, [_fake_peer(LIGHT_SERIAL)],
    )
    this_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX_HEX, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
        unique_id=MLPREFIX_HEX,
        title="Tank A",
    )
    this_entry.add_to_hass(hass)
    other_entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: OTHER_PAN_ID, CONF_MLPREFIX: OTHER_MLPREFIX_HEX, CONF_DEVICES: [{CONF_SERIAL: LIGHT_SERIAL}]},
        unique_id=OTHER_MLPREFIX_HEX,
        title="Tank B",
    )
    other_entry.add_to_hass(hass)

    with patch("homeassistant.config_entries.ConfigEntries.async_reload", AsyncMock()) as mock_reload:
        await _async_revalidate_tank(hass, this_entry)

    # Removed from the old entry, added to the new one.
    assert {d[CONF_SERIAL] for d in other_entry.data[CONF_DEVICES]} == set()
    assert {d[CONF_SERIAL] for d in this_entry.data[CONF_DEVICES]} == {PUMP_SERIAL, LIGHT_SERIAL}
    # Both entries got reloaded -- confirms neither side is left stale.
    reloaded_ids = {call.args[0] for call in mock_reload.call_args_list}
    assert reloaded_ids == {this_entry.entry_id, other_entry.entry_id}


async def test_setup_entry_registers_periodic_revalidation(hass):
    """Confirms async_setup_entry() actually registers the periodic
    callback with the correct interval -- not just that
    _async_revalidate_tank() itself works in isolation."""
    from custom_components.mobius.const import TANK_REVALIDATION_INTERVAL

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    fake_device = _fake_pump_device()
    address = bytes.fromhex("fd11223344556677000000fffe001234")

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=address),
    ), patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ), patch(
        "custom_components.mobius.async_track_time_interval",
    ) as mock_track_interval:
        mock_track_interval.return_value = lambda: None
        await async_setup_entry(hass, entry)

    assert mock_track_interval.call_count == 1
    call_args = mock_track_interval.call_args
    assert call_args[0][0] is hass
    assert call_args[0][2] == TANK_REVALIDATION_INTERVAL
