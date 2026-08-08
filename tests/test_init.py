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
from homeassistant.exceptions import ConfigEntryError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius import async_setup, async_setup_entry, tank_device_identifier
from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX
from custom_components.mobius.gateway_registry import GatewayRegistry

PUMP_ADDRESS = "E4:67:D8:17:84:83"
PUMP_SERIAL = "76517731952041"
LIGHT_SERIAL = "7V4Z00F143RBED"
PAN_ID = 0x3D0F
MLPREFIX_HEX = "fd1c5ec780e35c01"


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


async def test_proactive_discovery_failure_does_not_break_setup_flow(hass):
    """A None result (device not currently reachable) from the proactive
    discovery step itself must not raise or otherwise derail setup --
    the coordinator's own on-demand fallback covers it on a later poll.
    Setup DOES still fail overall here (ConfigEntryNotReady), but for the
    ordinary reason (the coordinator's own first read also has nothing to
    connect to in this scenario) -- not because of anything specific to
    the discovery step failing."""
    from homeassistant.exceptions import ConfigEntryNotReady

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
    entry.mock_state(hass, entry.state.SETUP_IN_PROGRESS)

    with patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=None),
    ) as mock_discover, patch(
        "custom_components.mobius._current_rssi", return_value=-80,
    ), patch.object(
        registry.group(PAN_ID).gateway_connection, "ensure_connected", AsyncMock(side_effect=Exception("boom")),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, entry)

    # The discovery step itself ran and returned None cleanly -- the
    # failure came from the coordinator's own first read, not from
    # mishandling a None discovery result.
    mock_discover.assert_called_once()
    assert mock_discover.call_args[0][0] is hass
    assert mock_discover.call_args[0][1] == PUMP_SERIAL
    assert registry.group(PAN_ID).members[PUMP_SERIAL].mesh_address is None


async def test_proactive_discovery_skipped_when_already_cached(hass):
    """Avoids redundant work -- if a mesh address is already cached by
    the time join() returns, don't re-discover it. Mocks join() directly
    (rather than pre-populating through a real join() call first) since a
    second join() for the same serial would legitimately overwrite
    whatever was there before -- this is testing __init__.py's own check
    of the returned group's state, not the registry's join() semantics
    themselves (covered separately in test_gateway_registry.py)."""
    from custom_components.mobius.gateway_registry import PanGroup, MemberState

    hass.data.setdefault(DOMAIN, {})
    semaphore = hass.data[DOMAIN].setdefault("connection_semaphore", asyncio.Semaphore(2))
    registry = GatewayRegistry(hass, semaphore, election_settle_seconds=0.01)
    hass.data[DOMAIN]["gateway_registry"] = registry

    cached_address = bytes.fromhex("fd11223344556677000000fffe005678")
    from custom_components.mobius.coordinator import MobiusConnectionManager
    prebuilt_group = PanGroup(
        pan_id=PAN_ID, gateway_serial="existing-gateway-serial",
        gateway_connection=MobiusConnectionManager(hass, "existing-gateway-serial", semaphore),
    )
    prebuilt_group.members[PUMP_SERIAL] = MemberState(serial=PUMP_SERIAL, mesh_address=cached_address)
    registry._groups[PAN_ID] = prebuilt_group

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}],
        },
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    fake_gateway_device = object()
    fake_relayed_device = _fake_pump_device()

    with patch.object(
        registry, "join", AsyncMock(return_value=prebuilt_group),
    ), patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(),
    ) as mock_discover, patch(
        "custom_components.mobius._current_rssi", return_value=-80,
    ), patch.object(
        prebuilt_group.gateway_connection, "ensure_connected", AsyncMock(return_value=fake_gateway_device),
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice", return_value=fake_relayed_device,
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ):
        await async_setup_entry(hass, entry)

    mock_discover.assert_not_called()
    assert prebuilt_group.members[PUMP_SERIAL].mesh_address == cached_address


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
