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
import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ADDRESS
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius import async_setup, async_setup_entry, tank_device_identifier
from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX
from mobius import MeshPeer, Model, Tank

MLPREFIX_BYTES = bytes.fromhex("fdaaaaaaaaaaaaaa")


def _fake_tank_for(serial: str, *other_serials: str) -> Tank:
    """A Tank as discover_tank_for_serial() would return for a real
    multi-device mesh -- own_peer (matching serial) first, then one
    MeshPeer per other_serials, each with a real, distinct 16-byte
    mesh address (not just a placeholder), matching what a real
    NetworkedThreadDevices-based discover_tank() call returns."""
    def _peer(s: str, suffix: int) -> MeshPeer:
        return MeshPeer(
            serial=s, model_raw=42, model=Model.VorTechMP40wG3QD,
            short_address=suffix, address=MLPREFIX_BYTES + suffix.to_bytes(8, "big"),
        )
    peers = [_peer(serial, 1)] + [_peer(s, i + 2) for i, s in enumerate(other_serials)]
    return Tank(prefix=MLPREFIX_BYTES, peers=peers)


def _fake_no_tank() -> Tank:
    """What discover_tank_for_serial() returns for a device that's
    reachable but not currently part of any provisioned Thread network
    -- the normal, expected case for a genuinely ad-hoc, single-device
    entry."""
    return Tank(prefix=None, peers=[])

from custom_components.mobius.gateway_registry import GatewayRegistry

PUMP_ADDRESS = "AA:AA:AA:AA:AA:01"
PUMP_SERIAL = "00000000000001"
LIGHT_SERIAL = "FAKESERIAL0001"
PAN_ID = 0x3D0F
MLPREFIX_HEX = "fdaaaaaaaaaaaaaa"


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
        "custom_components.mobius.discover_tank_for_serial", AsyncMock(return_value=_fake_no_tank()),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=None),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups",
        AsyncMock(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

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

    with patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=_fake_tank_for(PUMP_SERIAL)),
    ) as mock_discover, patch(
        "custom_components.mobius._current_rssi", return_value=-40,
    ), patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

    mock_discover.assert_called_once_with(hass, PUMP_SERIAL, hass.data[DOMAIN]["connection_semaphore"])
    registry = hass.data[DOMAIN]["gateway_registry"]
    assert registry.group(PAN_ID).members[PUMP_SERIAL].mesh_address == MLPREFIX_BYTES + (1).to_bytes(8, "big")


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
        "custom_components.mobius.discover_tank_for_serial", AsyncMock(return_value=_fake_no_tank()),
    ), patch(
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
        await hass.config_entries.async_setup(entry.entry_id)

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
        unique_id="fake-unique-id-secondary-failure-test",
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
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=_fake_tank_for(PUMP_SERIAL)),
    ), patch(
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
    ), patch(
        # LIGHT_SERIAL's own mesh address is never resolved in this
        # test, so its own soft refresh fails and retries for real
        # without this.
        "custom_components.mobius.asyncio.sleep", AsyncMock(),
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)

    assert result is True
    # LIGHT_SERIAL's own proactive discovery genuinely ran and failed --
    # this isn't testing a call that never happened.
    light_calls = [c for c in mock_discover.call_args_list if c[0][1] == LIGHT_SERIAL]
    assert len(light_calls) == 1
    assert registry.group(PAN_ID).members[LIGHT_SERIAL].mesh_address is None
    # PUMP_SERIAL's address came from the probe itself (the tank's own
    # peer report), not a separate discover_mesh_address() call.
    pump_calls = [c for c in mock_discover.call_args_list if c[0][1] == PUMP_SERIAL]
    assert len(pump_calls) == 0
    assert registry.group(PAN_ID).members[PUMP_SERIAL].mesh_address == MLPREFIX_BYTES + (1).to_bytes(8, "big")


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

    with patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=_fake_tank_for(PUMP_SERIAL)),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(),
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
        await hass.config_entries.async_setup(entry.entry_id)

    # discover_mesh_address is never called at all: PUMP_SERIAL's address
    # came from the probe's own tank report, LIGHT_SERIAL's was already
    # cached in the registry.
    mock_discover.assert_not_called()
    assert prebuilt_group.members[PUMP_SERIAL].mesh_address == MLPREFIX_BYTES + (1).to_bytes(8, "big")
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
        "custom_components.mobius.discover_tank_for_serial",
        # Only PUMP_SERIAL's own probe succeeds -- it's the only
        # candidate ever tried, since it has the stronger RSSI and the
        # probe stops at the first success. Its own tank report doesn't
        # include LIGHT_SERIAL as a peer, so LIGHT_SERIAL's own address
        # still needs (and gets) the direct fallback below.
        AsyncMock(side_effect=lambda hass, serial, semaphore: (
            _fake_tank_for(PUMP_SERIAL) if serial == PUMP_SERIAL else None
        )),
    ), patch(
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
    ), patch(
        # LIGHT_SERIAL is unreachable both ways in this test, by design
        # -- its own soft refresh fails and retries for real without this.
        "custom_components.mobius.asyncio.sleep", AsyncMock(),
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)

    # The entry itself sets up successfully -- the core point of this fix.
    assert result is True
    assert set(entry.runtime_data.coordinators.keys()) == {PUMP_SERIAL, LIGHT_SERIAL}
    # PUMP_SERIAL (the one that's actually reachable) succeeded.
    assert entry.runtime_data.coordinators[PUMP_SERIAL].last_update_success is True
    # LIGHT_SERIAL's own coordinator exists (its entities will simply show
    # unavailable) but its failure never propagated up to block setup.
    assert entry.runtime_data.coordinators[LIGHT_SERIAL].last_update_success is False


async def test_probe_tries_devices_in_rssi_order_not_list_order(hass, caplog):
    """The probe phase tries the STRONGEST-signal device first, not just
    whichever happens to be first in CONF_DEVICES -- confirms a
    consistently-unreachable device listed first doesn't get retried
    forever while a perfectly reachable one (listed later) sits unused.
    Also confirms the probe SEQUENCE itself is logged up front -- the
    direct diagnostic for a report like "only some of a tank's devices
    ever seem to get a real connection attempt"."""
    caplog.set_level(logging.DEBUG, logger="custom_components.mobius")
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
    probed_order = []

    async def fake_discover_tank_for_serial(hass, serial, semaphore):
        probed_order.append(serial)
        return _fake_tank_for(PUMP_SERIAL) if serial == PUMP_SERIAL else None

    async def fake_ensure_connected(self):
        return fake_pump_device

    with patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(side_effect=fake_discover_tank_for_serial),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=None),
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
    ), patch(
        # LIGHT_SERIAL's own mesh address is never resolved in this test
        # (discover_mesh_address above returns None unconditionally), so
        # its own soft refresh fails and retries for real without this.
        "custom_components.mobius.asyncio.sleep", AsyncMock(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

    # PUMP_SERIAL (stronger RSSI) was probed FIRST, despite being listed
    # second in CONF_DEVICES.
    assert probed_order[0] == PUMP_SERIAL
    # And it actually becomes the registry's gateway -- confirms
    # prefer_as_gateway is passed for the probe-winning device, not
    # whichever one happens to be first in CONF_DEVICES.
    registry = hass.data[DOMAIN]["gateway_registry"]
    assert registry.group(PAN_ID).gateway_serial == PUMP_SERIAL
    # The full RSSI-ordered probe sequence, logged before any connection
    # is even attempted -- confirms this doesn't just show the winner,
    # but the actual order/RSSI every device was considered in.
    assert f"[('{PUMP_SERIAL}', -30), ('{LIGHT_SERIAL}', -95)]" in caplog.text
    assert f"{PUMP_SERIAL} is the working device" in caplog.text


async def test_soft_refresh_retries_and_recovers_from_a_transient_first_failure(hass, caplog):
    """The setup-time half of the fix for a real, confirmed production
    bug (see _async_ensure_sensors_exist()'s own docstring for the
    self-healing half): a relayed device's own first soft refresh
    failing transiently shouldn't mean it starts unavailable for the
    whole session if a retry, moments later, would have succeeded fine.
    Also confirms this is actually visible in the logs -- a real,
    confirmed gap this used to have: only the final give-up case was
    ever logged, nothing for "failed once, retrying" or "recovered on
    retry"."""
    caplog.set_level(logging.DEBUG, logger="custom_components.mobius")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX_HEX,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}, {CONF_SERIAL: LIGHT_SERIAL}],
        },
        unique_id=MLPREFIX_HEX,
    )
    entry.add_to_hass(hass)

    fake_pump_device = _fake_pump_device()
    fetch_all_calls = {"n": 0}

    async def flaky_fetch_all(device, minute_of_day_now=None):
        # PUMP_SERIAL (the gateway) always succeeds; LIGHT_SERIAL's own
        # relayed fetch fails on its first call, then succeeds on retry.
        if device is fake_pump_device:
            return {
                "support": "pump", "operation_state": "Schedule",
                "telemetry": {"speed_percent": 10.0, "gph": 500},
                "current_pump_mode": "TidalSwell", "current_pump_params": {},
                "schedule_point_count": 1, "firmware_versions": {}, "hardware_info": {},
            }
        fetch_all_calls["n"] += 1
        if fetch_all_calls["n"] == 1:
            raise IOError("transient relay hiccup")
        return {"support": "light", "channels": [], "current_intensities": {}, "calibration": None}

    with patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=_fake_tank_for(PUMP_SERIAL, LIGHT_SERIAL)),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=None),
    ), patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_pump_device),
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=MagicMock(),
    ), patch(
        "custom_components.mobius.coordinator._fetch_all", AsyncMock(side_effect=flaky_fetch_all),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ), patch(
        "custom_components.mobius.asyncio.sleep", AsyncMock(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

    assert entry.runtime_data.coordinators[LIGHT_SERIAL].last_update_success is True
    assert entry.runtime_data.coordinators[LIGHT_SERIAL].data["support"] == "light"
    assert fetch_all_calls["n"] == 2  # confirms a retry genuinely happened, not just luck
    assert f"{LIGHT_SERIAL}'s own first soft refresh at setup failed" in caplog.text
    assert f"{LIGHT_SERIAL} came up on retry" in caplog.text


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

async def test_multi_device_tank_setup_makes_only_one_connection_not_n_plus_one(hass):
    """The actual core point of this whole change: for a genuine
    multi-device tank where the probed device's own tank report already
    includes every OTHER device's mesh address too (a real
    NetworkedThreadDevices/CoAP read, the same Thread mesh data this
    integration's own relay reads already depend on), NO per-device
    discover_mesh_address() fallback connection should happen at all --
    just the one probe connection for the whole tank. An earlier version
    of this code opened a SEPARATE, direct BLE connection to every
    device individually just to learn each one's address, even though
    the one connection already made to establish the gateway had
    already reported all of them for free -- on a brand-new N-device
    tank, that meant N+1 total connections during setup instead of just
    1."""
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

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=_fake_tank_for(PUMP_SERIAL, LIGHT_SERIAL)),
    ) as mock_discover_tank, patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(),
    ) as mock_discover_address, patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_device,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

    # Exactly ONE connection for the whole tank -- the probe itself.
    assert mock_discover_tank.call_count == 1
    # And NOT ONE, direct, per-device connection for the other device --
    # its address came from the probe's own tank report instead.
    mock_discover_address.assert_not_called()

    registry = hass.data[DOMAIN]["gateway_registry"]
    group = registry.group(PAN_ID)
    assert group.members[PUMP_SERIAL].mesh_address == MLPREFIX_BYTES + (1).to_bytes(8, "big")
    assert group.members[LIGHT_SERIAL].mesh_address == MLPREFIX_BYTES + (2).to_bytes(8, "big")


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

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=_fake_tank_for(PUMP_SERIAL, LIGHT_SERIAL)),
    ), patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_device,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

    assert set(entry.runtime_data.coordinators.keys()) == {PUMP_SERIAL, LIGHT_SERIAL}



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

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=_fake_tank_for(PUMP_SERIAL, LIGHT_SERIAL)),
    ), patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_device,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

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

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial", AsyncMock(return_value=_fake_no_tank()),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=None),
    ), patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await hass.config_entries.async_setup(entry.entry_id)

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
    discover_mesh_peers_auto() -- avoids any real BLE connection.
    is_connected is set True on the underlying fake device -- matching
    _async_revalidate_tank()'s own documented common case ("the gateway
    is already connected most of the time for its own regular polling")
    -- so its own proactive Bluetooth-cache check (gated on NOT already
    connected) doesn't fire for tests that aren't specifically about
    that behavior; see TestRevalidateProactiveCacheCheck below for
    tests that deliberately set is_connected False instead."""
    semaphore = asyncio.Semaphore(2)
    registry = GatewayRegistry(hass, semaphore, election_settle_seconds=0.01)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["gateway_registry"] = registry
    hass.data[DOMAIN]["connection_semaphore"] = semaphore

    connection = MobiusConnectionManager(hass, gateway_serial, semaphore)
    fake_device = MagicMock()
    fake_device.is_connected = True
    fake_device.discover_mesh_peers_auto = AsyncMock(return_value=peers_to_return)
    connection.ensure_connected = AsyncMock(return_value=fake_device)
    connection._device = fake_device

    group = PanGroup(pan_id=pan_id, gateway_serial=gateway_serial, gateway_connection=connection)
    group.members[gateway_serial] = MemberState(serial=gateway_serial)
    registry._groups[pan_id] = group
    return registry, group


def _fake_peer(serial: str) -> MeshPeer:
    return MeshPeer(
        serial=serial, model_raw=42, model=Model.VorTechMP40wG3QD,
        short_address=0x1234, address=b"\x00" * 16,
    )


async def test_revalidate_skips_cleanly_if_no_group_exists_at_all(hass):
    """No group at all (nobody has ever called join() for this pan_id)
    -- skips this run quietly, nothing to check against or recover.
    Different from "a group exists but has no gateway right now" -- see
    test_revalidate_recovers_a_gatewayless_tank below for that case,
    which actively tries to recover rather than just skipping."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["gateway_registry"] = GatewayRegistry(hass, asyncio.Semaphore(2))
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    await _async_revalidate_tank(hass, entry)  # must not raise


class TestRevalidateProactiveCacheCheck:
    """The actual point of this whole addition: catching a device
    (specifically the tank's own gateway) missing from Home Assistant's
    own Bluetooth cache proactively, during this function's own regular
    1-minute cycle, rather than only reactively -- discovered only once
    something actually needed to connect and failed. A real, confirmed
    production incident showed the gateway going missing from that
    cache for hours at a stretch."""

    async def test_requests_active_scan_when_not_connected_and_not_in_cache(self, hass):
        registry, group = _make_registry_with_gateway(hass, PAN_ID, PUMP_SERIAL, [])
        group.gateway_connection._device = None  # not currently connected

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
            unique_id=PUMP_SERIAL,
        )
        entry.add_to_hass(hass)

        with patch(
            "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
            return_value=[],
        ), patch(
            "custom_components.mobius.bluetooth.async_request_active_scan", AsyncMock(),
        ) as mock_active_scan:
            await _async_revalidate_tank(hass, entry)

        mock_active_scan.assert_awaited_once_with(hass)

    async def test_skips_active_scan_when_already_connected(self, hass):
        """The key, protective case: a currently-connected gateway
        legitimately stops advertising while connected -- it must never
        be treated as "missing" just because it isn't in the
        advertisement cache right now."""
        registry, group = _make_registry_with_gateway(hass, PAN_ID, PUMP_SERIAL, [])
        # _make_registry_with_gateway's own default: already connected.

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
            unique_id=PUMP_SERIAL,
        )
        entry.add_to_hass(hass)

        with patch(
            "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
            return_value=[],  # would "find it missing" if the check ran at all
        ), patch(
            "custom_components.mobius.bluetooth.async_request_active_scan", AsyncMock(),
        ) as mock_active_scan:
            await _async_revalidate_tank(hass, entry)

        mock_active_scan.assert_not_called()

    async def test_skips_active_scan_when_gateway_found_in_cache(self, hass):
        """Not connected, but already visible in the cache -- nothing
        actually wrong, no need to request a scan."""
        registry, group = _make_registry_with_gateway(hass, PAN_ID, PUMP_SERIAL, [])
        group.gateway_connection._device = None

        fake_info = MagicMock()
        fake_info.manufacturer_data = {
            0x0202: bytes.fromhex("2a0001000000000f3d") + PUMP_SERIAL.encode("ascii"),
        }

        entry = MockConfigEntry(
            domain=DOMAIN,
            data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
            unique_id=PUMP_SERIAL,
        )
        entry.add_to_hass(hass)

        with patch(
            "custom_components.mobius.coordinator.bluetooth.async_discovered_service_info",
            return_value=[fake_info],
        ), patch(
            "custom_components.mobius.bluetooth.async_request_active_scan", AsyncMock(),
        ) as mock_active_scan:
            await _async_revalidate_tank(hass, entry)

        mock_active_scan.assert_not_called()


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
    # is_connected True -- this test is about ensure_connected() itself
    # raising, not about the separate proactive Bluetooth-cache check
    # (which is gated on NOT already connected); leaving it False here
    # would make this test depend on a real, unmocked Bluetooth manager
    # for something it isn't actually testing.
    connection._device = MagicMock(is_connected=True)
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


async def test_revalidate_recovers_a_gatewayless_tank(hass):
    """The other real, confirmed production issue this now addresses: a
    tank that's lost its gateway entirely (every candidate previously
    exhausted, or a single-device tank whose only member kept failing)
    used to just sit there forever, since nothing ever re-triggered
    election for an existing, already-gatewayless group. Confirms
    _async_revalidate_tank() actually recovers it now, via join()'s own
    already-tested re-election logic rather than reimplementing it."""
    semaphore = asyncio.Semaphore(2)
    registry = GatewayRegistry(hass, semaphore, election_settle_seconds=0.01)
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["gateway_registry"] = registry
    hass.data[DOMAIN]["connection_semaphore"] = semaphore
    # A group that exists (has a member) but currently has NO gateway --
    # the exact state left behind by, e.g., every candidate in a tank
    # exhausting itself.
    group = PanGroup(pan_id=PAN_ID, gateway_serial=None)
    group.members[PUMP_SERIAL] = MemberState(serial=PUMP_SERIAL, rssi=-50)
    registry._groups[PAN_ID] = group

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    await _async_revalidate_tank(hass, entry)

    # A gateway now exists again -- the only possible candidate, since
    # this group only ever had one member.
    assert registry.group(PAN_ID).gateway_serial == PUMP_SERIAL


async def test_revalidate_refreshes_known_members_mesh_address_and_last_seen(hass):
    """The actual core new maintenance behavior: every known member's
    own mesh address and mesh-last-seen data gets refreshed from this
    same read, not just checked for migration -- confirms a device
    whose address was never successfully discovered at setup (or went
    stale) gets a real, repeated chance to be found again."""
    frozen_now = dt_util.utcnow()
    peer = _fake_peer(LIGHT_SERIAL)
    peer.address = b"\xfd" + b"\x11" * 15
    peer.age = 5000  # 5 real seconds ago
    registry, group = _make_registry_with_gateway(hass, PAN_ID, PUMP_SERIAL, [peer])
    group.members[LIGHT_SERIAL] = MemberState(serial=LIGHT_SERIAL)  # no address cached yet

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}, {CONF_SERIAL: LIGHT_SERIAL}],
        },
        unique_id=PAN_ID,
    )
    entry.add_to_hass(hass)

    with patch("custom_components.mobius.dt_util.utcnow", return_value=frozen_now):
        await _async_revalidate_tank(hass, entry)

    member = registry.group(PAN_ID).members[LIGHT_SERIAL]
    assert member.mesh_address == peer.address
    assert member.mesh_last_seen_at == frozen_now - timedelta(milliseconds=5000)


async def test_revalidate_refresh_is_best_effort_leaves_unreported_member_alone(hass):
    """A known member simply not reported in this particular round (a
    real, expected outcome -- BLE info isn't always available every
    single check) keeps whatever was already cached, rather than being
    wiped or treated as an error -- matches this function's own
    established "one round proves nothing" philosophy."""
    registry, group = _make_registry_with_gateway(hass, PAN_ID, PUMP_SERIAL, [])  # nothing reported at all
    existing_address = b"\xfd" + b"\x22" * 15
    group.members[LIGHT_SERIAL] = MemberState(serial=LIGHT_SERIAL, mesh_address=existing_address)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}, {CONF_SERIAL: LIGHT_SERIAL}],
        },
        unique_id=PAN_ID,
    )
    entry.add_to_hass(hass)

    await _async_revalidate_tank(hass, entry)

    assert registry.group(PAN_ID).members[LIGHT_SERIAL].mesh_address == existing_address


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

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial", AsyncMock(return_value=_fake_no_tank()),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=None),
    ), patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ), patch(
        "custom_components.mobius.async_track_time_interval",
    ) as mock_track_interval:
        mock_track_interval.return_value = lambda: None
        await hass.config_entries.async_setup(entry.entry_id)

    assert mock_track_interval.call_count == 1
    call_args = mock_track_interval.call_args
    assert call_args[0][0] is hass
    assert call_args[0][2] == TANK_REVALIDATION_INTERVAL


# --------------------------------------------------------------------------
# async_remove_entry() -- triggers Bluetooth rediscovery for a permanently
# removed entry's own devices, so re-adding the same physical device later
# isn't silently blocked by stale match history. See __init__.py's own
# docstring for the full reasoning (confirmed via Home Assistant's own
# documentation, which recommends exactly this for exactly this scenario).
# --------------------------------------------------------------------------

def _fake_discovery(address: str, serial: str):
    """A minimal stand-in for BluetoothServiceInfoBleak -- only the two
    attributes async_remove_entry() actually reads."""
    payload = bytes.fromhex("2a0001000000000f3d") + serial.encode("ascii")
    info = MagicMock()
    info.address = address
    info.manufacturer_data = {0x0202: payload}  # MOBIUS_COMPANY_ID
    return info


async def test_remove_entry_triggers_rediscovery_for_currently_visible_devices(hass):
    from custom_components.mobius import async_remove_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mobius.bluetooth.async_discovered_service_info",
        return_value=[_fake_discovery(PUMP_ADDRESS, PUMP_SERIAL)],
    ), patch(
        "custom_components.mobius.bluetooth.async_rediscover_address",
    ) as mock_rediscover:
        await async_remove_entry(hass, entry)

    mock_rediscover.assert_called_once_with(hass, PUMP_ADDRESS)


async def test_remove_entry_skips_a_device_not_currently_advertising(hass):
    """Best-effort, not guaranteed -- a device that's powered off or out
    of range right now simply can't have its current address resolved,
    and that's not an error worth raising over."""
    from custom_components.mobius import async_remove_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mobius.bluetooth.async_discovered_service_info",
        return_value=[],  # nothing currently visible at all
    ), patch(
        "custom_components.mobius.bluetooth.async_rediscover_address",
    ) as mock_rediscover:
        await async_remove_entry(hass, entry)  # must not raise

    mock_rediscover.assert_not_called()


async def test_remove_entry_only_acts_on_this_entrys_own_devices(hass):
    """A different, unrelated device also currently visible must not get
    its own match history touched by this entry's own removal."""
    from custom_components.mobius import async_remove_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    unrelated_serial = "99999999999999"
    with patch(
        "custom_components.mobius.bluetooth.async_discovered_service_info",
        return_value=[
            _fake_discovery(PUMP_ADDRESS, PUMP_SERIAL),
            _fake_discovery("BB:BB:BB:BB:BB:BB", unrelated_serial),
        ],
    ), patch(
        "custom_components.mobius.bluetooth.async_rediscover_address",
    ) as mock_rediscover:
        await async_remove_entry(hass, entry)

    mock_rediscover.assert_called_once_with(hass, PUMP_ADDRESS)


async def test_unload_entry_does_not_trigger_rediscovery(hass):
    """The critical distinction from async_remove_entry() -- an ordinary
    unload (which also happens on every routine reload, e.g. after a
    merge/migration) must NOT clear match history, or a device would get
    spuriously re-offered for discovery on every single reload."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    fake_device = _fake_pump_device()
    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial", AsyncMock(return_value=_fake_no_tank()),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=None),
    ), patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ):
        await hass.config_entries.async_setup(entry.entry_id)

    with patch(
        "custom_components.mobius.bluetooth.async_rediscover_address",
    ) as mock_rediscover:
        await hass.config_entries.async_unload(entry.entry_id)

    mock_rediscover.assert_not_called()


# --------------------------------------------------------------------------
# _async_ensure_sensors_exist() -- the self-healing check for a real,
# confirmed production bug: sensor.py's own async_setup_entry() decides
# which type-specific entities to create from a ONE-TIME snapshot of
# coordinator.data, taken at setup. For a relayed (non-gateway) device
# specifically, that snapshot can still be empty at that exact moment (its
# own first read is a soft, non-blocking refresh -- see
# SOFT_REFRESH_RETRY_ATTEMPTS's own docstring in const.py) -- these tests
# confirm the periodic backstop that catches whatever the retry there
# doesn't.
# --------------------------------------------------------------------------

from custom_components.mobius import _async_ensure_sensors_exist, MobiusRuntimeData
from custom_components.mobius.sensor import _build_type_specific_entities


def _make_runtime_for_healing_test(hass, entry, serial, data, already_created=()):
    """A MobiusRuntimeData with one coordinator whose data is exactly
    as given, a stashed (mocked) sensor_add_entities callback, and a
    fake device_info -- everything _async_ensure_sensors_exist() needs,
    without going through a real sensor.py platform setup at all."""
    coordinator = MagicMock()
    coordinator.data = data
    runtime = MobiusRuntimeData(coordinators={serial: coordinator})
    runtime.sensor_add_entities = MagicMock()
    runtime.sensor_device_infos = {serial: MagicMock()}
    runtime.created_sensor_unique_ids = set(already_created)
    entry.runtime_data = runtime
    return runtime


class TestEnsureSensorsExist:
    async def test_creates_missing_type_specific_entities_when_data_becomes_available(self, hass):
        entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="healing-test-1")
        entry.add_to_hass(hass)
        data = {"support": "pump", "telemetry": {"speed_percent": 50, "gph": 1200}}
        runtime = _make_runtime_for_healing_test(hass, entry, PUMP_SERIAL, data)

        await _async_ensure_sensors_exist(hass, entry)

        runtime.sensor_add_entities.assert_called_once()
        added = runtime.sensor_add_entities.call_args[0][0]
        added_unique_ids = {e.unique_id for e in added}
        assert added_unique_ids == {
            f"{PUMP_SERIAL}_operation_state", f"{PUMP_SERIAL}_motor_speed",
            f"{PUMP_SERIAL}_flow_rate", f"{PUMP_SERIAL}_current_pump_mode",
        }
        assert runtime.created_sensor_unique_ids == added_unique_ids

    async def test_does_not_recreate_already_created_entities(self, hass):
        entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="healing-test-2")
        entry.add_to_hass(hass)
        data = {"support": "pump", "telemetry": {}}
        already = {
            f"{PUMP_SERIAL}_operation_state", f"{PUMP_SERIAL}_motor_speed",
            f"{PUMP_SERIAL}_flow_rate", f"{PUMP_SERIAL}_current_pump_mode",
        }
        runtime = _make_runtime_for_healing_test(hass, entry, PUMP_SERIAL, data, already_created=already)

        await _async_ensure_sensors_exist(hass, entry)

        runtime.sensor_add_entities.assert_not_called()
        assert runtime.created_sensor_unique_ids == already  # unchanged, not duplicated

    async def test_creates_only_the_specific_missing_entity_not_the_whole_type(self, hass):
        """The subtler version of the same bug: support was already
        correctly detected as "light" and its other entities already
        created, but calibration data specifically wasn't present yet
        at that exact moment -- confirms only the missing Calibration
        entity gets added, not duplicates of the already-created ones."""
        entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="healing-test-3")
        entry.add_to_hass(hass)
        data = {
            "support": "light", "channels": ["RoyalBlue"], "current_intensities": {"RoyalBlue": 500},
            "calibration": MagicMock(completed=True, date_of_last=0, lower_bound=None, upper_bound=None),
        }
        already = {f"{LIGHT_SERIAL}_intensity_royalblue"}  # calibration NOT yet in this set
        runtime = _make_runtime_for_healing_test(hass, entry, LIGHT_SERIAL, data, already_created=already)

        await _async_ensure_sensors_exist(hass, entry)

        runtime.sensor_add_entities.assert_called_once()
        added = runtime.sensor_add_entities.call_args[0][0]
        assert {e.unique_id for e in added} == {f"{LIGHT_SERIAL}_calibration"}

    async def test_skips_devices_whose_data_still_is_not_ready(self, hass):
        entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="healing-test-4")
        entry.add_to_hass(hass)
        runtime = _make_runtime_for_healing_test(hass, entry, PUMP_SERIAL, {})  # support still missing

        await _async_ensure_sensors_exist(hass, entry)

        runtime.sensor_add_entities.assert_not_called()
        assert runtime.created_sensor_unique_ids == set()

    async def test_noop_if_sensor_platform_never_set_up_yet(self, hass):
        """runtime.sensor_add_entities is None until sensor.py's own
        async_setup_entry() has actually run once -- must not raise if
        this somehow runs before that's happened."""
        entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="healing-test-5")
        entry.add_to_hass(hass)
        entry.runtime_data = MobiusRuntimeData(coordinators={})

        await _async_ensure_sensors_exist(hass, entry)  # must not raise

    async def test_noop_if_runtime_data_does_not_exist_yet(self, hass):
        entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="healing-test-6")
        entry.add_to_hass(hass)
        # entry.runtime_data deliberately never set at all.

        await _async_ensure_sensors_exist(hass, entry)  # must not raise
