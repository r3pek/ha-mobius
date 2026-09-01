"""End-to-end tests for switch.py's LocalControlEnabled/FanShutdownEnabled
entities -- replacing sensor.py's own former, now-removed read-only
sensors of the same names, once python-mobius grew write support."""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from mobius import Tank, AdvancedFeatures, MetadataSnapshot

from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES

PAN_ID = 0x3D0F
PUMP_ADDRESS = "AA:AA:AA:AA:AA:01"
PUMP_SERIAL = "00000000000001"


def _minimal_pump_device(advanced_features_dict):
    """Just enough mocked out for setup to succeed -- these tests care
    about the switch entities specifically, not the rest of a pump's
    own sensor set (already covered by test_sensor.py)."""
    device = MagicMock()
    device.serial = PUMP_SERIAL
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 42, "model": "VorTechMP40wG3QD", "manufacturer": "EcoTech Marine",
        "name": "MP40QD Right", "serial": PUMP_SERIAL,
        "primitive_type": "VorTechV1", "error_state": "NoError", "mac_address": None,
    })
    device.get_pump_telemetry = AsyncMock(return_value={
        "speed": 447, "speed_percent": 44.7, "gph": 2272,
        "gph_reliable": True, "minimum_gph": 200, "maximum_gph": 2500,
    })
    device.get_operation_state = AsyncMock()
    device.get_operation_state.return_value.name = "Schedule"
    fake_point = MagicMock()
    fake_point.pump.mode.name = "TidalSwell"
    fake_point.pump.params = {}
    device.get_pump_schedule = AsyncMock(return_value=[fake_point] * 11)
    device.get_current_pump_block = AsyncMock(return_value=fake_point)
    device.get_metadata_batch = AsyncMock(return_value=MetadataSnapshot(
        advanced_features=AdvancedFeatures(**advanced_features_dict),
        calibration=None, hardware_info={}, firmware_versions={}, supported_channels=[],
        error_state=None, epoch=None, local_time=None, tz_offset=None,
    ))
    device.get_firmware_versions = AsyncMock(return_value={})
    device.get_hardware_info = AsyncMock(return_value={})
    device.get_own_mesh_address = AsyncMock(
        return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")
    )
    return device


@contextlib.asynccontextmanager
async def _pump_entry(hass, advanced_features_dict, set_advanced_features_mock=None):
    """Keeps the connection-mocking patches active for the WHOLE test
    body, not just initial setup -- a successful write triggers
    coordinator.async_request_refresh(), which reconnects and would
    otherwise fall through to real (unmocked) BLE discovery once the
    patches have already exited."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, "address": PUMP_ADDRESS}],
        },
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)
    device = _minimal_pump_device(advanced_features_dict)
    if set_advanced_features_mock is not None:
        device.set_advanced_features = set_advanced_features_mock

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, device


@pytest.mark.asyncio
async def test_local_control_switch_reflects_current_state(hass):
    """VorTech-relevant -- a pump reporting LocalControlEnabled=True
    should show up as an ON switch, in the CONFIGURATION category (not
    DIAGNOSTIC, unlike the read-only sensor this replaces). Entity_id
    suffix is "local_control" (from the entity's own friendly name,
    "Local control"), not "local_control_enabled" (the translation_key/
    unique_id suffix) -- confirmed by checking the real created entity,
    not assumed."""
    async with _pump_entry(hass, {
        "local_control_enabled": True, "auto_dim_timeout": None,
        "max_fan_speed": None, "fan_shutdown_enabled": None,
    }):
        state = hass.states.get("switch.mp40qd_right_local_control")
        assert state is not None
        assert state.state == "on"


@pytest.mark.asyncio
async def test_fan_shutdown_switch_not_created_when_unsupported_pump(hass):
    """A pump (VorTech-style) never reports FanShutdownEnabled (that's
    Radion-relevant) -- confirms no switch entity gets created for it,
    matching the same per-attribute gating the sensors this replaces
    already had."""
    async with _pump_entry(hass, {
        "local_control_enabled": True, "auto_dim_timeout": None,
        "max_fan_speed": None, "fan_shutdown_enabled": None,
    }):
        assert hass.states.get("switch.mp40qd_right_fan_shutdown") is None


@pytest.mark.asyncio
async def test_time_sync_switch_created_on_tank_device_and_defaults_on(hass):
    """Attached to the synthetic TANK device (see __init__.py's own
    tank_device_identifier()), not the pump itself -- confirmed by
    checking the entity_id, which comes from the tank's own name
    ("Mock Title" here), not the pump's own ("MP40QD Right")."""
    async with _pump_entry(hass, {
        "local_control_enabled": True, "auto_dim_timeout": None,
        "max_fan_speed": None, "fan_shutdown_enabled": None,
    }) as (entry, device):
        state = hass.states.get("switch.mock_title_time_sync")
        assert state is not None
        assert state.state == "on"
        assert entry.runtime_data.time_sync_enabled is True


@pytest.mark.asyncio
async def test_turning_off_time_sync_switch_disables_it_on_the_runtime_data(hass):
    """The actual point of this whole entity: _async_sync_tank_time()
    (in __init__.py) reads MobiusRuntimeData.time_sync_enabled before
    every scheduled write -- confirms toggling the switch off actually
    updates that shared field, not just the switch's own displayed
    state."""
    async with _pump_entry(hass, {
        "local_control_enabled": True, "auto_dim_timeout": None,
        "max_fan_speed": None, "fan_shutdown_enabled": None,
    }) as (entry, device):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.mock_title_time_sync"}, blocking=True,
        )

        state = hass.states.get("switch.mock_title_time_sync")
        assert state.state == "off"
        assert entry.runtime_data.time_sync_enabled is False

        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": "switch.mock_title_time_sync"}, blocking=True,
        )
        assert hass.states.get("switch.mock_title_time_sync").state == "on"
        assert entry.runtime_data.time_sync_enabled is True


@pytest.mark.asyncio
async def test_turning_on_switch_calls_set_advanced_features(hass):
    set_mock = AsyncMock(return_value={"local_control_enabled": None})
    async with _pump_entry(hass, {
        "local_control_enabled": False, "auto_dim_timeout": None,
        "max_fan_speed": None, "fan_shutdown_enabled": None,
    }, set_advanced_features_mock=set_mock):
        await hass.services.async_call(
            "switch", "turn_on",
            {"entity_id": "switch.mp40qd_right_local_control"},
            blocking=True,
        )

        set_mock.assert_awaited_once_with(local_control_enabled=True)


@pytest.mark.asyncio
async def test_device_rejected_write_raises_home_assistant_error(hass):
    """A real, expected outcome: the device itself rejects the write
    (e.g. unsupported/read-only on this specific device) -- confirms
    this surfaces as a HomeAssistantError, not a silent no-op or an
    unhandled exception."""
    set_mock = AsyncMock(return_value={"local_control_enabled": IOError("rejected")})
    async with _pump_entry(hass, {
        "local_control_enabled": False, "auto_dim_timeout": None,
        "max_fan_speed": None, "fan_shutdown_enabled": None,
    }, set_advanced_features_mock=set_mock):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "switch", "turn_on",
                {"entity_id": "switch.mp40qd_right_local_control"},
                blocking=True,
            )
