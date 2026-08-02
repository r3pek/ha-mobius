"""End-to-end test: full config entry setup -> coordinators -> sensor
platform -> real Home Assistant entities with correct states.

MobiusDevice is mocked (no real BLE hardware available here), but the
canned data reuses real values captured from actual hardware during this
project's development, for both a pump and a light.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from mobius import PrimitiveType

from custom_components.mobius.const import DOMAIN, CONF_SERIAL

PUMP_ADDRESS = "E4:67:D8:17:84:83"
PUMP_SERIAL = "76517731952041"
LIGHT_ADDRESS = "84:25:3F:AF:F0:C2"
LIGHT_SERIAL = "7V4Z00F143RBED"

# Real 13-channel set and a subset of real interpolated intensities,
# captured live from an actual Radion XR15 G6 Pro during development.
REAL_LIGHT_CHANNELS = [
    "Violet", "CoolWhite", "WarmWhite", "Blue", "UV", "RoyalBlue", "Green",
    "Red", "MoonlightBlue", "MoonlightWhite", "Brightness",
    "StormProbability", "CloudProbability",
]
REAL_LIGHT_INTENSITIES = {
    "Brightness": 1000.0, "CoolWhite": 240.0, "Blue": 1000.0, "RoyalBlue": 1000.0,
    "Green": 240.0, "Red": 240.0, "UV": 1000.0, "WarmWhite": 240.0, "Violet": 1000.0,
    "MoonlightWhite": 0.0, "MoonlightBlue": 0.0, "StormProbability": 0.0, "CloudProbability": 0.0,
}


def _fake_pump_device():
    device = MagicMock()
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 42, "model": "VorTechMP40wG3QD", "manufacturer": "EcoTech Marine",
        "name": "MP40QD Right", "serial": "76517731952041",
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
    device.get_firmware_versions = AsyncMock(return_value={
        "Radio": "4.0.21", "Radio Bootloader": "1.2",
        "Product OS": "2.1.5", "Product Bootloader": "1.0",
    })
    return device


def _fake_light_device():
    device = MagicMock()
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 179, "model": "RadionXR15wG6Pro", "manufacturer": "EcoTech Marine",
        "name": "", "serial": "7V4Z00F143RBED",
        "primitive_type": "VisualV1", "error_state": "NoError", "mac_address": None,
    })
    device.identify_device_type = AsyncMock(return_value=PrimitiveType.VisualV1)

    channel_objs = []
    for name in REAL_LIGHT_CHANNELS:
        c = MagicMock()
        c.name = name
        channel_objs.append(c)
    device.get_supported_channels = AsyncMock(return_value=channel_objs)
    device.get_light_schedule = AsyncMock(return_value=[MagicMock()] * 9)

    intensity_map = {}
    for name, value in REAL_LIGHT_INTENSITIES.items():
        key = MagicMock()
        key.name = name
        intensity_map[key] = value
    device.get_current_light_intensities = AsyncMock(return_value=intensity_map)

    device.get_firmware_versions = AsyncMock(return_value={
        "Product OS": "1.0", "Product Bootloader": "1.0", "Radio Firmware": "3.1.0",
        "Filesystem": "1", "Radio OS": "2.0", "Radio": "2.0", "WLAN": "1.1",
    })

    calibration = MagicMock()
    calibration.completed = True
    calibration.date_of_last = 1756561525
    calibration.lower_bound = None
    calibration.upper_bound = None
    device.get_calibration_info = AsyncMock(return_value=calibration)
    return device


async def test_pump_entry_setup_creates_expected_sensors(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: PUMP_ADDRESS, CONF_SERIAL: PUMP_SERIAL},
        unique_id=PUMP_ADDRESS,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=_fake_pump_device()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state.value == "loaded"

    speed = hass.states.get("sensor.mp40qd_right_motor_speed")
    assert speed is not None
    assert speed.state == "44.7"
    assert speed.attributes["unit_of_measurement"] == "%"
    assert speed.attributes["raw_signed_value"] == 447
    assert speed.attributes["reverse_rotation"] is False

    flow = hass.states.get("sensor.mp40qd_right_estimated_flow")
    assert flow is not None
    assert flow.state == "2272"
    assert flow.attributes["unit_of_measurement"] == "gal/h"

    mode = hass.states.get("sensor.mp40qd_right_current_mode")
    assert mode is not None
    assert mode.state == "TidalSwell"

    support = hass.states.get("sensor.mp40qd_right_support_tier")
    assert support is not None
    assert support.state == "pump"

    point_count = hass.states.get("sensor.mp40qd_right_schedule_points")
    assert point_count is not None
    assert point_count.state == "11"

    # The actual new behavior: sw_version comes from the confirmed "Product
    # OS" label, and pumps don't support calibration (get_calibration_info()
    # returns None in the app's own confirmed real-hardware behavior), so
    # no calibration sensor should be created.
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, PUMP_ADDRESS)})
    assert device is not None
    assert device.sw_version == "2.1.5"
    assert hass.states.get("sensor.mp40qd_right_calibration") is None


async def test_light_entry_setup_creates_channel_sensors(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: LIGHT_ADDRESS, CONF_SERIAL: LIGHT_SERIAL},
        unique_id=LIGHT_ADDRESS,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=_fake_light_device()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state.value == "loaded"

    royal_blue = hass.states.get("sensor.radionxr15wg6pro_7v4z00f143rbed_royalblue_intensity")
    assert royal_blue is not None
    assert royal_blue.state == "100.0"  # 1000 permille / 10 = 100.0%

    cool_white = hass.states.get("sensor.radionxr15wg6pro_7v4z00f143rbed_coolwhite_intensity")
    assert cool_white is not None
    assert cool_white.state == "24.0"  # 240 permille / 10 = 24.0%

    support = hass.states.get("sensor.radionxr15wg6pro_7v4z00f143rbed_support_tier")
    assert support is not None
    assert support.state == "light"

    # The actual new behavior: sw_version + a calibration sensor, since
    # calibration is confirmed real/populated for lights (unlike pumps).
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, LIGHT_ADDRESS)})
    assert device is not None
    assert device.sw_version == "1.0"

    calibration = hass.states.get("sensor.radionxr15wg6pro_7v4z00f143rbed_calibration")
    assert calibration is not None
    assert calibration.state == "True"
    assert "lower_bound" not in calibration.attributes  # fixture sets it to None -> omitted
    assert "last_calibration_time" in calibration.attributes


async def test_entry_unload_removes_entities_and_disconnects(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_ADDRESS: PUMP_ADDRESS, CONF_SERIAL: PUMP_SERIAL},
        unique_id=PUMP_ADDRESS,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=_fake_pump_device()),
    ), patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.disconnect",
        AsyncMock(),
    ) as mock_disconnect:
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert hass.states.get("sensor.mp40qd_right_motor_speed") is not None

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state.value == "not_loaded"
    # The actual new behavior: unload must close the persistent connection,
    # not just unload the sensor platform (which the old architecture
    # didn't need to do at all, since there was never anything persistent
    # to close).
    mock_disconnect.assert_awaited_once()


def test_identical_model_devices_get_distinct_names_via_serial():
    """The actual scenario this was built for: two identical-model devices
    (e.g. two XR15 lights) with blank configured names must not collide on
    device name. Unit-tests _device_info() directly rather than running two
    full config entries through the Bluetooth stack (which introduces
    unrelated teardown flakiness with pytest-socket when running two live
    entries in one test)."""
    from custom_components.mobius.sensor import _device_info

    status_a = {"name": "", "model": "RadionXR15wG6Pro", "manufacturer": "EcoTech Marine",
                "serial": "7V4Z00F149RBF3"}
    status_b = {"name": "", "model": "RadionXR15wG6Pro", "manufacturer": "EcoTech Marine",
                "serial": "7V4Z00F143RBED"}

    info_a = _device_info("84:25:3F:AF:F0:A2", status_a)
    info_b = _device_info("84:25:3F:AF:F0:C2", status_b)

    assert info_a["name"] != info_b["name"]
    assert "7V4Z00F149RBF3" in info_a["name"]
    assert "7V4Z00F143RBED" in info_b["name"]
