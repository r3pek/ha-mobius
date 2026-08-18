"""
Tests for diagnostics.py. The single most important property tested
here isn't structural correctness (though that matters too) -- it's
that the returned dict is ACTUALLY json.dumps()-able end to end, the
same way Home Assistant's own diagnostics download mechanism uses it.
A structurally-correct dict that crashes on serialization would be
worse than no diagnostics support at all -- confirmed via a real,
similar issue other integrations have hit (core PR #141111).
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_MLPREFIX, CONF_DEVICES
from custom_components.mobius.diagnostics import async_get_config_entry_diagnostics, _json_safe

PUMP_SERIAL = "00000000000001"
LIGHT_SERIAL = "FAKESERIAL0001"
PAN_ID = 0x3D0F
MLPREFIX_HEX = "fdaaaaaaaaaaaaaa"


def _fake_pump_device():
    device = MagicMock()
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 42, "model": "VorTechMP40wG3QD", "manufacturer": "EcoTech Marine",
        "name": "MP40QD Right", "serial": PUMP_SERIAL, "primitive_type": "VorTechV1",
        "error_state": "NoError", "mac_address": "AA:BB:CC:DD:EE:FF",
    })
    device.get_pump_telemetry = AsyncMock(return_value={"speed": 447, "speed_percent": 44.7, "gph": 2272})
    device.get_operation_state = AsyncMock()
    device.get_operation_state.return_value.name = "Schedule"
    device.get_advanced_features = AsyncMock(return_value=None)
    fake_point = MagicMock()
    fake_point.pump.mode.name = "TidalSwell"
    fake_point.pump.params = {}
    device.get_pump_schedule = AsyncMock(return_value=[fake_point])
    device.get_current_pump_block = AsyncMock(return_value=fake_point)
    device.get_firmware_versions = AsyncMock(return_value={"Product OS": "1.0"})
    device.get_hardware_info = AsyncMock(return_value={"Revision": 2})
    return device


async def _setup_multi_device_tank_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX_HEX,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}, {CONF_SERIAL: LIGHT_SERIAL}],
        },
        unique_id=MLPREFIX_HEX,
        title="Living Room Reef",
    )
    entry.add_to_hass(hass)

    fake_device = _fake_pump_device()

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
    ) as mock_probe, patch(
        "custom_components.mobius.discover_mesh_address", AsyncMock(return_value=None),
    ), patch(
        "custom_components.mobius._current_rssi", return_value=-50,
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice",
        side_effect=Exception("LIGHT_SERIAL relay unreachable in this test"),
    ), patch(
        "homeassistant.config_entries.ConfigEntries.async_forward_entry_setups", AsyncMock(),
    ), patch(
        # LIGHT_SERIAL's own soft refresh above fails every attempt, by
        # design (see the RelayedMobiusDevice patch above) -- without
        # this, that means really waiting out SOFT_REFRESH_RETRY_DELAY
        # in wall-clock time for every test using this fixture.
        "custom_components.mobius.asyncio.sleep", AsyncMock(),
    ):
        from mobius import Tank, MeshPeer, Model
        mock_probe.return_value = Tank(
            prefix=bytes.fromhex(MLPREFIX_HEX),
            peers=[MeshPeer(
                serial=PUMP_SERIAL, model_raw=42, model=Model.VorTechMP40wG3QD,
                short_address=1, address=bytes.fromhex(MLPREFIX_HEX) + (1).to_bytes(8, "big"),
            )],
        )
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    return entry


async def test_diagnostics_output_is_actually_json_serializable(hass):
    """The core, critical property -- see this module's own docstring
    for why a structurally-plausible-looking dict isn't good enough on
    its own."""
    entry = await _setup_multi_device_tank_entry(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    # Must not raise -- this is the actual point of the test.
    serialized = json.dumps(diagnostics)
    assert serialized  # sanity: produced real, non-empty output


async def test_diagnostics_includes_gateway_and_member_state(hass):
    entry = await _setup_multi_device_tank_entry(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["registry"]["gateway_serial"] == PUMP_SERIAL
    devices_by_serial = {d["serial"]: d for d in diagnostics["devices"]}
    assert devices_by_serial[PUMP_SERIAL]["is_current_gateway"] is True
    assert devices_by_serial[LIGHT_SERIAL]["is_current_gateway"] is False


async def test_diagnostics_redacts_mac_addresses(hass):
    """Both the top-level CONF_ADDRESS an ad-hoc entry might store, and
    "mac_address" nested inside a device's own raw coordinator data --
    confirmed real via _fake_pump_device()'s own fixture above."""
    entry = await _setup_multi_device_tank_entry(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(diagnostics)

    assert "AA:BB:CC:DD:EE:FF" not in serialized
    devices_by_serial = {d["serial"]: d for d in diagnostics["devices"]}
    pump_data = devices_by_serial[PUMP_SERIAL]["coordinator"]["data"]
    assert pump_data["mac_address"] == "**REDACTED**"


async def test_diagnostics_does_not_redact_serial_numbers(hass):
    """Serial numbers are this integration's own actual identity
    mechanism -- redacting them would make the rest of the dump
    unreadable, and they're printed on the physical device itself, not
    a secret credential."""
    entry = await _setup_multi_device_tank_entry(hass)

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    serialized = json.dumps(diagnostics)

    assert PUMP_SERIAL in serialized
    assert LIGHT_SERIAL in serialized


async def test_diagnostics_handles_missing_registry_gracefully(hass):
    """Must not crash if called in some edge-case state (e.g. before
    this integration's own async_setup() has ever run) -- graceful
    None values throughout, not an exception."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX_HEX,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}],
        },
        unique_id=MLPREFIX_HEX,
    )
    entry.add_to_hass(hass)
    # Deliberately no runtime_data, no gateway_registry in hass.data.

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["registry"] is None
    assert diagnostics["devices"][0]["coordinator"] is None
    assert diagnostics["devices"][0]["is_current_gateway"] is None
    # Still fully serializable even in this degraded state.
    json.dumps(diagnostics)


async def test_diagnostics_shows_device_currently_visible_in_bluetooth_cache(hass):
    """The actual point of this whole addition: distinguishing "Home
    Assistant's own Bluetooth stack currently sees this device" from
    this integration's own, separately-tracked registry/coordinator
    state -- the direct way to tell "our cached state is stale" apart
    from "Home Assistant itself hasn't seen this device", which are
    different problems with different fixes."""
    entry = await _setup_multi_device_tank_entry(hass)

    fake_info = MagicMock()
    fake_info.address = "AA:BB:CC:DD:EE:FF"
    fake_info.rssi = -55
    fake_info.connectable = True
    fake_info.time = 1000.0
    fake_info.manufacturer_data = {
        0x0202: bytes.fromhex("2a0001000000000f3d") + PUMP_SERIAL.encode("ascii"),
    }

    with patch(
        "custom_components.mobius.diagnostics.bluetooth.async_discovered_service_info",
        return_value=[fake_info],
    ), patch(
        "custom_components.mobius.diagnostics.bluetooth.async_scanner_count", return_value=3,
    ), patch("custom_components.mobius.diagnostics.time.time", return_value=1042.5):
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    devices_by_serial = {d["serial"]: d for d in diagnostics["devices"]}
    pump_cache = devices_by_serial[PUMP_SERIAL]["bluetooth_cache"]
    assert pump_cache["found_by_serial"] is True
    assert pump_cache["rssi"] == -55
    assert pump_cache["seconds_since_last_advertisement"] == 42.5
    # LIGHT_SERIAL wasn't in the fake cache at all.
    assert devices_by_serial[LIGHT_SERIAL]["bluetooth_cache"] == {"found_by_serial": False}
    # The complementary "is there even a working scanner at all" check --
    # a real, confirmed production incident showed a device (that
    # tank's own gateway) missing from the cache for hours at a
    # stretch; this is what tells whether that's a whole-system problem
    # or specific to one device.
    assert diagnostics["bluetooth_connectable_scanners_registered"] == 3
    # The whole-cache sanity count reflects what was actually there.
    assert diagnostics["bluetooth_cache_total_connectable_devices"] == 1


async def test_diagnostics_redacts_the_bluetooth_cache_address_too(hass):
    """The address surfaced in bluetooth_cache is just as much a real
    BLE MAC as anything else this dump already redacts -- confirms it
    doesn't slip through as a new, separate field the existing
    redaction policy doesn't know about."""
    entry = await _setup_multi_device_tank_entry(hass)

    fake_info = MagicMock()
    fake_info.address = "AA:BB:CC:DD:EE:FF"
    fake_info.rssi = -55
    fake_info.connectable = True
    fake_info.time = 1000.0
    fake_info.manufacturer_data = {
        0x0202: bytes.fromhex("2a0001000000000f3d") + PUMP_SERIAL.encode("ascii"),
    }

    with patch(
        "custom_components.mobius.diagnostics.bluetooth.async_discovered_service_info",
        return_value=[fake_info],
    ):
        diagnostics = await async_get_config_entry_diagnostics(hass, entry)
        serialized = json.dumps(diagnostics)

    assert "AA:BB:CC:DD:EE:FF" not in serialized
    devices_by_serial = {d["serial"]: d for d in diagnostics["devices"]}
    assert devices_by_serial[PUMP_SERIAL]["bluetooth_cache"]["address"] == "**REDACTED**"


def test_json_safe_converts_bytes_to_hex():
    assert _json_safe(b"\x01\x02\xff") == "0102ff"


def test_json_safe_converts_nested_structures():
    class FakeEnum:
        name = "SomeValue"

    result = _json_safe({"a": b"\x01", "b": [FakeEnum(), 1, "text"], "c": {"nested": b"\x02"}})
    assert result == {"a": "01", "b": ["SomeValue", 1, "text"], "c": {"nested": "02"}}


def test_json_safe_falls_back_to_str_for_unrecognized_types():
    class Unrecognized:
        def __str__(self):
            return "unrecognized-repr"

    assert _json_safe(Unrecognized()) == "unrecognized-repr"
