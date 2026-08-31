"""End-to-end tests for select.py's AutoDimTimeout/MaxFanSpeed entities --
replacing sensor.py's own former, now-removed read-only sensors of the
same names, once python-mobius grew write support.

No separate "is this a valid value" test here -- a SelectEntity simply
cannot submit anything outside its own configured option list, so the
option list itself (AUTO_DIM_TIMEOUT_OPTIONS/MAX_FAN_SPEED_OPTIONS in
select.py, confirmed matching the app's own exact preset choices) IS
the validation.
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from mobius import Tank, AdvancedFeatures, MetadataSnapshot

from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES

PAN_ID = 0x3D0F
LIGHT_ADDRESS = "AA:AA:AA:AA:AA:03"
LIGHT_SERIAL = "FAKESERIAL0001"


def _minimal_light_device(advanced_features_dict):
    """Just enough mocked out for setup to succeed -- these tests care
    about the select entities specifically, not the rest of a light's
    own sensor set (already covered by test_sensor.py)."""
    device = MagicMock()
    device.serial = LIGHT_SERIAL
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 179, "model": "RadionXR15wG6Pro", "manufacturer": "EcoTech Marine",
        "name": "", "serial": LIGHT_SERIAL,
        "primitive_type": "VisualV1", "error_state": "NoError", "mac_address": None,
    })
    device.get_supported_channels = AsyncMock(return_value=[])
    device.get_light_schedule = AsyncMock(return_value=[])
    device.get_current_light_intensities = AsyncMock(return_value=MagicMock(diagnostics={
        "insolation_active": False, "is_night_segment": False,
        "lunar_enabled": None, "scalar_source": "schedule_intensity", "scalar": 1.0,
    }))
    device.get_metadata_batch = AsyncMock(return_value=MetadataSnapshot(
        advanced_features=AdvancedFeatures(**advanced_features_dict),
        calibration=None, hardware_info={}, firmware_versions={}, supported_channels=[],
        error_state=None, epoch=None, local_time=None, tz_offset=None,
    ))
    device.get_firmware_versions = AsyncMock(return_value={})
    device.get_hardware_info = AsyncMock(return_value={})
    calibration = MagicMock()
    calibration.completed = True
    calibration.date_of_last = 1756561525
    calibration.lower_bound = None
    calibration.upper_bound = None
    device.get_calibration_info = AsyncMock(return_value=calibration)
    device.get_own_mesh_address = AsyncMock(
        return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe005678")
    )
    return device


@contextlib.asynccontextmanager
async def _light_entry(hass, advanced_features_dict, set_advanced_features_mock=None):
    """Keeps the connection-mocking patches active for the WHOLE test
    body, not just initial setup -- see test_switch.py's own
    _pump_entry() for why (a successful write triggers
    coordinator.async_request_refresh(), which reconnects)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: LIGHT_SERIAL, "address": LIGHT_ADDRESS}],
        },
        unique_id=LIGHT_SERIAL,
    )
    entry.add_to_hass(hass)
    device = _minimal_light_device(advanced_features_dict)
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
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe005678")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, device


@pytest.mark.asyncio
async def test_max_fan_speed_select_reflects_current_state(hass):
    """Radion-relevant -- confirms the select's own current_option
    reads back one of the app's own confirmed presets."""
    async with _light_entry(hass, {
        "local_control_enabled": None, "auto_dim_timeout": None,
        "max_fan_speed": 60.0, "fan_shutdown_enabled": False,
    }):
        state = hass.states.get("select.radionxr15wg6pro_fakeserial0001_max_fan_speed")
        assert state is not None
        assert state.state == "60"
        assert set(state.attributes["options"]) == {"10", "20", "40", "60", "80", "100"}


@pytest.mark.asyncio
async def test_auto_dim_timeout_select_not_created_when_unsupported_light(hass):
    """A light (Radion-style) never reports AutoDimTimeout (that's
    VorTech-relevant) -- confirms no select entity gets created for it,
    matching the same per-attribute gating the sensors this replaces
    already had."""
    async with _light_entry(hass, {
        "local_control_enabled": None, "auto_dim_timeout": None,
        "max_fan_speed": 60.0, "fan_shutdown_enabled": False,
    }):
        assert hass.states.get("select.radionxr15wg6pro_fakeserial0001_led_auto_dim_timeout") is None


@pytest.mark.asyncio
async def test_selecting_option_calls_set_advanced_features_with_float(hass):
    """max_fan_speed uses python-mobius's own 0-100 percent convention
    -- confirms the string option gets converted to a float, not left
    as a string or converted to the raw permille/sentinel encoding
    (that conversion is python-mobius's own job, not this entity's)."""
    set_mock = AsyncMock(return_value={"max_fan_speed": None})
    async with _light_entry(hass, {
        "local_control_enabled": None, "auto_dim_timeout": None,
        "max_fan_speed": 60.0, "fan_shutdown_enabled": False,
    }, set_advanced_features_mock=set_mock):
        await hass.services.async_call(
            "select", "select_option",
            {"entity_id": "select.radionxr15wg6pro_fakeserial0001_max_fan_speed", "option": "100"},
            blocking=True,
        )

        set_mock.assert_awaited_once_with(max_fan_speed=100.0)


@pytest.mark.asyncio
async def test_device_rejected_write_raises_home_assistant_error(hass):
    """Same expected outcome as switch.py's own equivalent test --
    device rejection surfaces as a HomeAssistantError."""
    set_mock = AsyncMock(return_value={"max_fan_speed": IOError("rejected")})
    async with _light_entry(hass, {
        "local_control_enabled": None, "auto_dim_timeout": None,
        "max_fan_speed": 60.0, "fan_shutdown_enabled": False,
    }, set_advanced_features_mock=set_mock):
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                "select", "select_option",
                {"entity_id": "select.radionxr15wg6pro_fakeserial0001_max_fan_speed", "option": "80"},
                blocking=True,
            )
