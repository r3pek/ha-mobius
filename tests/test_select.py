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

from mobius import Tank, AdvancedFeatures, MetadataSnapshot, LightPollResult, LightIntensityResult

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
    device.get_light_poll_batch = AsyncMock(return_value=LightPollResult(
        schedule_points=[],
        intensities=LightIntensityResult({}, diagnostics={
            "insolation_active": False, "is_night_segment": False,
            "lunar_enabled": None, "scalar_source": "schedule_intensity", "scalar": 1.0,
        }),
    ))
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


# --------------------------------------------------------------------------
# SceneSelectionSelect -- unit tests, constructed directly against a mocked
# entry.runtime_data (multiple fake coordinators) rather than a full,
# multi-device async_setup_entry(), since the actual behavior worth
# confirming is the aggregation/selection logic itself.
# --------------------------------------------------------------------------

from custom_components.mobius import MobiusRuntimeData
from custom_components.mobius.select import SceneSelectionSelect
from mobius import Scene, ActiveScene, SceneID


def _fake_coordinator(serial, scenes=None, current_scene=None):
    coordinator = MagicMock()
    coordinator.serial = serial
    coordinator.data = {"configured_scenes": scenes or [], "current_scene": current_scene}
    coordinator.async_get_connected_device = AsyncMock(return_value=AsyncMock())
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


def _entry_with_coordinators(*coordinators):
    entry = MagicMock()
    entry.runtime_data = MobiusRuntimeData(coordinators={c.serial: c for c in coordinators})
    return entry


@pytest.mark.asyncio
async def test_options_are_the_union_across_every_device():
    """A scene configured only on the light and another only on the
    pump both show up -- matching how the app itself builds its own
    tank-wide scene list from separate, per-device reads."""
    light_scene = Scene(index=0, id=100, scene_type=None, name="Sunrise",
                         timeout=60, light=None, pump=None)
    pump_scene = Scene(index=0, id=int(SceneID.FeedMode), scene_type=SceneID.FeedMode,
                        name="Feed", timeout=30, light=None, pump=None)
    entry = _entry_with_coordinators(
        _fake_coordinator("light1", scenes=[light_scene]),
        _fake_coordinator("pump1", scenes=[pump_scene]),
    )
    select = SceneSelectionSelect(entry, ("mobius", "tank_1234"))

    assert select.options == ["None", "Feed", "Sunrise"]


@pytest.mark.asyncio
async def test_empty_scene_slots_are_excluded_from_options():
    empty_slot = Scene(index=0, id=int(SceneID.EmptyScene), scene_type=SceneID.EmptyScene,
                        name="", timeout=0, light=None, pump=None)
    entry = _entry_with_coordinators(_fake_coordinator("light1", scenes=[empty_slot]))
    select = SceneSelectionSelect(entry, ("mobius", "tank_1234"))

    assert select.options == ["None"]


@pytest.mark.asyncio
async def test_current_option_reflects_whichever_device_reports_it_active():
    pump_scene = Scene(index=0, id=int(SceneID.FeedMode), scene_type=SceneID.FeedMode,
                        name="Feed", timeout=30, light=None, pump=None)
    entry = _entry_with_coordinators(
        _fake_coordinator("light1", scenes=[pump_scene]),
        _fake_coordinator(
            "pump1", scenes=[pump_scene],
            current_scene=ActiveScene(id=int(SceneID.FeedMode), scene_type=SceneID.FeedMode, duration_seconds=25),
        ),
    )
    select = SceneSelectionSelect(entry, ("mobius", "tank_1234"))

    assert select.current_option == "Feed"


@pytest.mark.asyncio
async def test_current_option_defaults_to_none_when_nothing_active():
    entry = _entry_with_coordinators(_fake_coordinator("light1"))
    select = SceneSelectionSelect(entry, ("mobius", "tank_1234"))

    assert select.current_option == "None"


@pytest.mark.asyncio
async def test_selecting_a_scene_writes_only_to_the_device_that_has_it():
    """The actual point of start_scene()'s own broadcast=True: only ONE
    device needs to be written to at all."""
    pump_scene = Scene(index=0, id=int(SceneID.FeedMode), scene_type=SceneID.FeedMode,
                        name="Feed", timeout=30, light=None, pump=None)
    light_coord = _fake_coordinator("light1")  # does NOT have this scene
    pump_coord = _fake_coordinator("pump1", scenes=[pump_scene])
    entry = _entry_with_coordinators(light_coord, pump_coord)
    select = SceneSelectionSelect(entry, ("mobius", "tank_1234"))

    await select.async_select_option("Feed")

    light_coord.async_get_connected_device.assert_not_awaited()
    pump_coord.async_get_connected_device.assert_awaited_once()
    written_device = pump_coord.async_get_connected_device.return_value
    written_device.start_scene.assert_awaited_once_with(int(SceneID.FeedMode), broadcast=True)


@pytest.mark.asyncio
async def test_selecting_none_resumes_the_schedule_on_every_device():
    light_coord = _fake_coordinator("light1")
    pump_coord = _fake_coordinator("pump1")
    entry = _entry_with_coordinators(light_coord, pump_coord)
    select = SceneSelectionSelect(entry, ("mobius", "tank_1234"))

    await select.async_select_option("None")

    for coordinator in (light_coord, pump_coord):
        device = coordinator.async_get_connected_device.return_value
        device.resume_schedule.assert_awaited_once()


@pytest.mark.asyncio
async def test_selecting_an_unknown_scene_raises():
    entry = _entry_with_coordinators(_fake_coordinator("light1"))
    select = SceneSelectionSelect(entry, ("mobius", "tank_1234"))

    with pytest.raises(HomeAssistantError):
        await select.async_select_option("Nonexistent")
