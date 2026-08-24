"""End-to-end test: full config entry setup -> coordinators -> sensor
platform -> real Home Assistant entities with correct states.

MobiusDevice is mocked (no real BLE hardware available here), but the
canned data reuses real values captured from actual hardware during this
project's development, for both a pump and a light.
"""

import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.const import CONF_ADDRESS
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from mobius import PrimitiveType, Tank, MeshPeer, Model, LightIntensityResult, AdvancedFeatures

from custom_components.mobius import tank_device_identifier
from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX

PAN_ID = 0x3D0F
MLPREFIX_HEX = "fdaaaaaaaaaaaaaa"
PUMP_ADDRESS = "AA:AA:AA:AA:AA:01"
PUMP_SERIAL = "00000000000001"
LIGHT_ADDRESS = "AA:AA:AA:AA:AA:03"
LIGHT_SERIAL = "FAKESERIAL0001"

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
    device.serial = "00000000000001"
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 42, "model": "VorTechMP40wG3QD", "manufacturer": "EcoTech Marine",
        "name": "MP40QD Right", "serial": "00000000000001",
        "primitive_type": "VorTechV1", "error_state": "NoError", "mac_address": None,
    })
    device.get_pump_telemetry = AsyncMock(return_value={
        "speed": 447, "speed_percent": 44.7, "gph": 2272,
        "gph_reliable": True, "minimum_gph": 200, "maximum_gph": 2500,
    })
    device.get_operation_state = AsyncMock()
    device.get_operation_state.return_value.name = "Schedule"
    device.get_advanced_features = AsyncMock(return_value=None)
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
    device.get_hardware_info = AsyncMock(return_value={
        "Color": "Black", "Revision": 2, "ProductType": "VorTech", "RadioType": "QCA4020",
    })
    device.get_own_mesh_address = AsyncMock(
        return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")
    )
    return device


def _fake_light_device():
    device = MagicMock()
    device.serial = "FAKESERIAL0001"
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 179, "model": "RadionXR15wG6Pro", "manufacturer": "EcoTech Marine",
        "name": "", "serial": "FAKESERIAL0001",
        "primitive_type": "VisualV1", "error_state": "NoError", "mac_address": None,
    })
    device.identify_device_type = AsyncMock(return_value=PrimitiveType.VisualV1)
    device.get_advanced_features = AsyncMock(return_value=None)

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
    device.get_current_light_intensities = AsyncMock(
        return_value=LightIntensityResult(intensity_map, diagnostics={
            "insolation_active": False, "is_night_segment": False,
            "lunar_enabled": None, "scalar_source": "schedule_intensity", "scalar": 1.0,
        })
    )

    # Deliberately WITHOUT "Product OS" -- matches the confirmed real-world
    # scenario this fixture is meant to represent: at least some real
    # Radion lights simply don't report that FirmwareType at all (no
    # MainMicroOS index in their FirmwareVersion response), unlike pumps
    # which always have one. This is the actual bug derive_sw_version()'s
    # fallback chain exists to cover -- see its own tests in
    # test_coordinator.py for the fallback logic itself; this fixture
    # confirms the full setup flow picks up the fallback correctly too.
    device.get_firmware_versions = AsyncMock(return_value={
        "Product Bootloader": "1.0", "Radio Firmware": "1.5.103",
        "Filesystem": "1.1.0", "Radio OS": "1.5.103", "Radio": "3.1.0", "WLAN": "3.1.0",
    })
    device.get_hardware_info = AsyncMock(return_value={"Revision": 1})

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


async def test_pump_entry_setup_creates_expected_sensors(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}],
        },
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=_fake_pump_device()),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state.value == "loaded"

    # No "_tank_3d0f" suffix in the entity ID anymore -- the old "—
    # Tank {pan_id}" name suffix was removed once via_device grouping
    # (see __init__.py's tank_device_identifier()) took over showing
    # which tank a device belongs to visually, rather than baking it
    # into every entity's own name/ID.
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
    assert flow.attributes["flow_reliable"] is True
    assert flow.attributes["minimum_flow"] == 200
    assert flow.attributes["maximum_flow"] == 2500

    mode = hass.states.get("sensor.mp40qd_right_current_mode")
    assert mode is not None
    assert mode.state == "TidalSwell"

    support = hass.states.get("sensor.mp40qd_right_support_tier")
    assert support is not None
    assert support.state == "pump"

    point_count = hass.states.get("sensor.mp40qd_right_schedule_points")
    assert point_count is not None
    assert point_count.state == "11"

    # The actual point of these two: full breakdown available as
    # attributes, not just the single headline value already shown on
    # the device card.
    firmware = hass.states.get("sensor.mp40qd_right_firmware_version")
    assert firmware is not None
    assert firmware.state == "2.1.5"  # Product OS -- no "Firmware" label in this pump's fixture
    assert firmware.attributes["Radio"] == "4.0.21"
    assert firmware.attributes["Radio Bootloader"] == "1.2"
    assert firmware.attributes["Product Bootloader"] == "1.0"

    hardware = hass.states.get("sensor.mp40qd_right_hardware_revision")
    assert hardware is not None
    assert hardware.state == "2"
    assert hardware.attributes["Revision"] == 2
    # The actual point of this extended fixture: confirms decoded string
    # fields (from python-mobius>=0.3.0) pass through as-is, not
    # re-decoded or mangled -- an earlier version of this sensor tried
    # to int.from_bytes() these, which would have crashed on a string.
    assert hardware.attributes["Color"] == "Black"
    assert hardware.attributes["ProductType"] == "VorTech"
    assert hardware.attributes["RadioType"] == "QCA4020"

    # New: the mesh address sensor, populated via the gateway registry's
    # own cached MemberState.mesh_address -- see __init__.py's own
    # async_setup_entry() docstring for why the gateway device now gets
    # this proactively discovered too, not just relayed ones.
    mesh_address = hass.states.get("sensor.mp40qd_right_mesh_address")
    assert mesh_address is not None
    assert mesh_address.state == "fdaa:aaaa:aaaa:aaaa:0:ff:fe00:1234"

    # The actual new behavior: sw_version comes from the confirmed "Product
    # OS" label, and pumps don't support calibration (get_calibration_info()
    # returns None in the app's own confirmed real-hardware behavior), so
    # no calibration sensor should be created.
    device_registry = dr.async_get(hass)
    # SERIAL-based identifiers now, not address-based -- a real, necessary
    # fix, not incidental to tank-aware entries (see sensor.py's own
    # _device_info() docstring for why).
    device = device_registry.async_get_device(identifiers={(DOMAIN, PUMP_SERIAL)})
    assert device is not None
    assert device.sw_version == "2.1.5"
    assert hass.states.get("sensor.mp40qd_right_calibration") is None

    # Every entry now gets a synthetic tank device to link to, including
    # this single, ad-hoc entry -- via the pan_id-based fallback
    # identifier since there's no CONF_MLPREFIX here (see
    # tank_device_identifier()'s own docstring).
    assert device.via_device_id is not None
    tank_device = device_registry.async_get_device(identifiers={tank_device_identifier(None, PAN_ID)})
    assert tank_device is not None
    assert device.via_device_id == tank_device.id
    # Still no gateway-device sensor, and no mesh-prefix sensor either --
    # those two remain gated on an actual confirmed mesh prefix/genuine
    # multi-device gateway election, neither of which apply here even
    # though a tank device now exists (see sensor.py's own comment on
    # that condition).
    gateway_states = [s for s in hass.states.async_all("sensor") if "gateway_device" in s.entity_id]
    assert gateway_states == []


async def test_pump_entry_setup_skips_flow_sensor_when_gph_unreliable(hass):
    """The actual real-world bug this confirms is fixed: a pump (like a
    real, reported AI Axis 20) that reports a raw gph value but doesn't
    support a min/max flow range -- the app itself would never trust
    or display this value, so this integration must not create a
    sensor for it either, rather than showing a number confirmed (via
    the app's own spec sheet) to be wildly wrong."""
    device = _fake_pump_device()
    device.get_pump_telemetry = AsyncMock(return_value={
        "speed": 600, "speed_percent": 60.0, "gph": 1460,
        "gph_reliable": False, "minimum_gph": None, "maximum_gph": None,
    })

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}],
        },
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

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

    assert hass.states.get("sensor.mp40qd_right_estimated_flow") is None
    # Every OTHER pump sensor still gets created normally -- confirms
    # this is specifically about the flow sensor, not a broader failure.
    assert hass.states.get("sensor.mp40qd_right_motor_speed") is not None
    assert hass.states.get("sensor.mp40qd_right_current_mode") is not None


async def test_flow_sensor_converts_min_max_to_the_effective_display_unit(hass):
    """The actual real-world bug this confirms is fixed: HA's own
    native_value -> state unit conversion does NOT extend to
    extra_state_attributes -- a real, reported case had a user override
    this entity's own display unit to L/h, and the visible state
    correctly converted, but minimum_flow/maximum_flow silently stayed
    in gal/h, unconverted. Uses PropertyMock to directly simulate the
    effective (possibly per-entity-overridden) unit HA itself would
    report via self.unit_of_measurement, rather than going through the
    full entity-registry-options machinery for a per-entity override --
    this is what the fix actually reads, so this is what needs to
    change for the test to mean anything."""
    from custom_components.mobius.sensor import FlowRateSensor

    fake_coordinator = MagicMock()
    fake_coordinator.data = {
        "telemetry": {
            "gph": 2272, "gph_reliable": True,
            "minimum_gph": 200, "maximum_gph": 2500,
        }
    }

    sensor = FlowRateSensor(fake_coordinator, PUMP_SERIAL, device_info=MagicMock())

    with patch.object(
        type(sensor), "unit_of_measurement",
        new_callable=lambda: property(lambda self: "L/h"),
    ):
        attrs = sensor.extra_state_attributes

    # Exact values from HA's own VolumeFlowRateConverter.convert(): 200
    # gal/h == 757.0823568 L/h, 2500 gal/h == 9463.52946 L/h.
    assert attrs["minimum_flow"] == pytest.approx(757.0823568)
    assert attrs["maximum_flow"] == pytest.approx(9463.52946)
    assert attrs["flow_reliable"] is True


async def test_mesh_address_sensor_carries_last_seen_as_an_attribute(hass):
    """End-to-end confirmation that mesh last-seen is now a plain
    attribute of the mesh_address sensor -- not its own entity (folded
    in, see MeshAddressSensor's own docstring for why) -- and shows a
    real, freshly-computed timestamp, refreshed on every regular poll
    cycle rather than a one-time snapshot captured at setup."""
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
    fake_device.discover_networked_thread_devices = AsyncMock(return_value=[
        MeshPeer(
            serial=PUMP_SERIAL, model_raw=42, model=Model.VorTechMP40wG3QD,
            short_address=0x1234, address=b"\x00" * 16, age=10000,  # 10 real seconds ago
        ),
    ])

    frozen_now = dt_util.utcnow()
    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")),
    ), patch(
        "custom_components.mobius.coordinator.dt_util.utcnow", return_value=frozen_now,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # No standalone mesh-last-seen entity at all anymore.
    assert hass.states.get("sensor.mp40qd_right_mesh_last_seen") is None

    state = hass.states.get("sensor.mp40qd_right_mesh_address")
    assert state is not None
    last_seen = state.attributes["last_seen"]
    # A tolerance, not exact equality -- last_seen is a real datetime
    # object here (an attribute, not a TIMESTAMP-device-class sensor
    # state string), so no truncation to account for -- still using a
    # tolerance rather than exact equality just to keep this robust to
    # sub-millisecond scheduling jitter in the test itself.
    expected = frozen_now - timedelta(seconds=10)
    assert abs((last_seen - expected).total_seconds()) < 1


async def test_light_entry_setup_creates_channel_sensors(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: LIGHT_SERIAL, CONF_ADDRESS: LIGHT_ADDRESS}],
        },
        unique_id=LIGHT_SERIAL,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=_fake_light_device()),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe005678")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state.value == "loaded"

    royal_blue = hass.states.get("sensor.radionxr15wg6pro_fakeserial0001_royalblue_intensity")
    assert royal_blue is not None
    assert royal_blue.state == "100"  # 1000 permille / 10 = 100% (whole number, not 100.0)

    cool_white = hass.states.get("sensor.radionxr15wg6pro_fakeserial0001_coolwhite_intensity")
    assert cool_white is not None
    assert cool_white.state == "24"  # 240 permille / 10 = 24% (whole number, not 24.0)

    support = hass.states.get("sensor.radionxr15wg6pro_fakeserial0001_support_tier")
    assert support is not None
    assert support.state == "light"

    # The actual point of this assertion: this light's fixture reports no
    # "Product OS" at all (see _fake_light_device()'s comment) -- sw_version
    # must still be populated via derive_sw_version()'s fallback chain,
    # not silently left empty, confirming the real bug fix end-to-end
    # through the full setup flow. Also confirms a calibration sensor is
    # created, since calibration is confirmed real/populated for lights
    # (unlike pumps).
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, LIGHT_SERIAL)})
    assert device is not None
    assert device.sw_version == "1.5.103"

    calibration = hass.states.get("sensor.radionxr15wg6pro_fakeserial0001_calibration")
    assert calibration is not None
    assert calibration.state == "True"
    assert "lower_bound" not in calibration.attributes  # fixture sets it to None -> omitted
    assert "last_calibration_time" in calibration.attributes

    # Same real-world scenario as above (no "Firmware"/"Product OS" label
    # at all) -- confirms the full breakdown sensor surfaces the
    # fallback-derived value too, plus every other reported component as
    # an attribute.
    firmware = hass.states.get("sensor.radionxr15wg6pro_fakeserial0001_firmware_version")
    assert firmware is not None
    assert firmware.state == "1.5.103"
    assert firmware.attributes["Filesystem"] == "1.1.0"
    assert firmware.attributes["WLAN"] == "3.1.0"

    hardware = hass.states.get("sensor.radionxr15wg6pro_fakeserial0001_hardware_revision")
    assert hardware is not None
    assert hardware.state == "1"


async def test_light_intensity_diagnostics_are_logged(hass, caplog):
    """A real, confirmed need this addresses directly: whether the
    app's own displayed intensity matches this library's own computed
    value can depend on branches (is this the dusk-to-night segment,
    is the lunar-phase toggle actually enabled on this specific
    device) that are otherwise invisible from the final number alone
    -- python-mobius's own get_current_light_intensities() already
    surfaces exactly this via its own .diagnostics; this confirms it
    actually reaches the logs, not just existing as an unused,
    theoretically-available field."""
    caplog.set_level(logging.DEBUG, logger="custom_components.mobius")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: LIGHT_SERIAL, CONF_ADDRESS: LIGHT_ADDRESS}],
        },
        unique_id=LIGHT_SERIAL,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=_fake_light_device()),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe005678")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert f"{LIGHT_SERIAL} light intensity diagnostics:" in caplog.text
    assert "'scalar_source': 'schedule_intensity'" in caplog.text


async def test_entry_unload_removes_entities_and_disconnects(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}],
        },
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=_fake_pump_device()),
    ), patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.disconnect",
        AsyncMock(),
    ) as mock_disconnect, patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")),
    ):
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


async def test_multi_device_tank_entry_wires_via_device_and_prefix_sensor(hass):
    """End-to-end: a genuine multi-device tank entry ends up with both
    real devices' own DeviceInfo pointing via_device at the synthetic
    tank device, and a MeshPrefixSensor attached to that tank device
    itself -- the actual "one hub, N child devices" UI grouping this
    whole feature was designed against."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID, CONF_MLPREFIX: MLPREFIX_HEX,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL}, {CONF_SERIAL: LIGHT_SERIAL}],
        },
        unique_id=MLPREFIX_HEX,
        title="Mobius Tank (2 devices)",
    )
    entry.add_to_hass(hass)

    def _fake_connect(*args, **kwargs):
        # Whichever serial this connection is for, return the matching
        # fixture -- both devices' coordinators share this same mocked
        # ensure_connected, so it needs to distinguish between them.
        return _fake_pump_device()

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(side_effect=_fake_connect),
    ), patch(
        "custom_components.mobius.coordinator.RelayedMobiusDevice",
        return_value=_fake_pump_device(),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=Tank(prefix=bytes.fromhex(MLPREFIX_HEX), peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state.value == "loaded"

    device_registry = dr.async_get(hass)
    tank_device = device_registry.async_get_device(identifiers={tank_device_identifier(MLPREFIX_HEX, PAN_ID)})
    assert tank_device is not None

    pump_device = device_registry.async_get_device(identifiers={(DOMAIN, PUMP_SERIAL)})
    light_device = device_registry.async_get_device(identifiers={(DOMAIN, LIGHT_SERIAL)})
    assert pump_device is not None
    assert light_device is not None
    assert pump_device.via_device_id == tank_device.id
    assert light_device.via_device_id == tank_device.id

    # No "discovered at"/age-based sensor at all -- removed entirely.
    # Real hardware testing (two consecutive scans of the same gateway)
    # showed the underlying MeshPeer.age value both increasing and
    # decreasing between runs for the same physical device, disproving
    # any time-since-last-seen interpretation, with no actual evidence
    # anywhere for what the field really represents.
    assert not [s for s in hass.states.async_all("sensor") if "discovered_at" in s.entity_id]

    # The prefix sensor is on the tank device, not any per-device entity
    # -- shared, tank-level data.
    prefix_states = [
        s for s in hass.states.async_all("sensor")
        if s.attributes.get("device_class") is None and "mesh_prefix" in s.entity_id
    ]
    assert len(prefix_states) == 1
    assert prefix_states[0].state == MLPREFIX_HEX

    # The gateway device sensor, also on the tank device -- shows the
    # actual gateway's own configured name (from _fake_pump_device()'s
    # own get_device_info() mock, since PUMP_SERIAL -- processed first,
    # no RSSI signal available in this test -- becomes the gateway).
    gateway_states = [
        s for s in hass.states.async_all("sensor")
        if s.attributes.get("device_class") is None and "gateway_device" in s.entity_id
    ]
    assert len(gateway_states) == 1
    assert gateway_states[0].state == "MP40QD Right"
    assert gateway_states[0].attributes["serial"] == PUMP_SERIAL


def test_identical_model_devices_get_distinct_names_via_serial():
    """The actual scenario this was built for: two identical-model devices
    (e.g. two XR15 lights) with blank configured names must not collide on
    device name. Unit-tests _device_info() directly rather than running two
    full config entries through the Bluetooth stack (which introduces
    unrelated teardown flakiness with pytest-socket when running two live
    entries in one test)."""
    from custom_components.mobius.sensor import _device_info

    status_a = {"name": "", "model": "RadionXR15wG6Pro", "manufacturer": "EcoTech Marine"}
    status_b = {"name": "", "model": "RadionXR15wG6Pro", "manufacturer": "EcoTech Marine"}

    # serial is now the identifying parameter, not read from the status
    # dict -- see _device_info()'s own docstring for why (a tank peer
    # has no stored address, so serial had to become the one thing every
    # device is guaranteed to provide).
    info_a = _device_info("FAKESERIAL0002", status_a)
    info_b = _device_info("FAKESERIAL0001", status_b)

    assert info_a["name"] != info_b["name"]
    assert "FAKESERIAL0002" in info_a["name"]
    assert "FAKESERIAL0001" in info_b["name"]


class TestGatewayDeviceSensor:
    """Unit-level tests for GatewayDeviceSensor's own logic --
    instantiated directly (bypassing full config-entry setup) since
    what's actually being tested is the fallback-naming chain and
    reacting to a changed gateway_serial, not the full setup flow
    (already covered separately by the end-to-end multi-device test
    above)."""

    def _make_sensor(self, gateway_serial, coordinators):
        from unittest.mock import MagicMock
        from custom_components.mobius.sensor import GatewayDeviceSensor

        sensor = object.__new__(GatewayDeviceSensor)
        sensor._pan_id = 0x3D0F
        sensor._coordinators = coordinators
        registry = MagicMock()
        group = MagicMock()
        group.gateway_serial = gateway_serial
        registry.group.return_value = group
        sensor._registry = registry
        return sensor

    def test_shows_the_gateway_devices_own_configured_name(self):
        coordinator = MagicMock()
        coordinator.data = {"name": "Living Room Pump", "model": "VorTechMP40wG3QD"}
        sensor = self._make_sensor("SERIAL1", {"SERIAL1": coordinator})
        assert sensor.native_value == "Living Room Pump"
        assert sensor.extra_state_attributes == {"serial": "SERIAL1"}

    def test_falls_back_to_model_and_serial_when_no_configured_name(self):
        """Matches _device_info()'s own fallback chain -- a blank
        configured name (confirmed real on actual hardware, see
        _fake_light_device()'s own fixture comment elsewhere in this
        file) shouldn't show as a blank sensor value."""
        coordinator = MagicMock()
        coordinator.data = {"name": "", "model": "RadionXR15wG6Pro"}
        sensor = self._make_sensor("SERIAL2", {"SERIAL2": coordinator})
        assert sensor.native_value == "RadionXR15wG6Pro (SERIAL2)"

    def test_falls_back_to_bare_serial_when_no_data_at_all(self):
        """Shouldn't normally happen (every device in CONF_DEVICES
        always gets its own coordinator), but must not crash if this
        integration genuinely has nothing for the reported gateway yet
        (e.g. right at startup, before that device's own first poll)."""
        sensor = self._make_sensor("SERIAL3", {})
        assert sensor.native_value == "SERIAL3"

    def test_none_when_no_gateway_elected_yet(self):
        sensor = self._make_sensor(None, {})
        assert sensor.native_value is None
        assert sensor.extra_state_attributes is None

    def test_reflects_a_changed_gateway_serial(self):
        """The actual point of listening to every coordinator, not just
        one -- confirms the sensor's own value follows gateway_serial
        live, not a value captured once at creation time."""
        coordinator_a = MagicMock()
        coordinator_a.data = {"name": "Pump A"}
        coordinator_b = MagicMock()
        coordinator_b.data = {"name": "Pump B"}
        sensor = self._make_sensor("SERIAL_A", {"SERIAL_A": coordinator_a, "SERIAL_B": coordinator_b})
        assert sensor.native_value == "Pump A"

        # Simulates a failover -- the registry's own gateway_serial
        # changes; the sensor is a plain property read, so it reflects
        # this the very next time anything reads it (which is exactly
        # what its own coordinator-update listeners trigger in real use
        # -- see async_added_to_hass()).
        sensor._registry.group.return_value.gateway_serial = "SERIAL_B"
        assert sensor.native_value == "Pump B"


class TestLightChannelIntensitySensorRounding:
    """LightChannelIntensitySensor.native_value -- confirms actual
    rounding behavior, not just the whole-number/decimal string
    formatting difference. Instantiates the sensor directly (bypassing
    __init__/CoordinatorEntity setup entirely, via object.__new__) since
    native_value only reads self.coordinator.data -- no need for a full
    config entry / HA integration setup just to test one property."""

    def _make_sensor(self, current_intensities: dict):
        from custom_components.mobius.sensor import LightChannelIntensitySensor

        sensor = object.__new__(LightChannelIntensitySensor)
        sensor._channel_name = "RoyalBlue"
        sensor.coordinator = MagicMock()
        sensor.coordinator.data = {"current_intensities": current_intensities}
        return sensor

    def test_rounds_down_below_the_midpoint(self):
        # 247 permille -> 24.7% -> rounds to 25, not 24 or 24.7
        sensor = self._make_sensor({"RoyalBlue": 247.0})
        assert sensor.native_value == 25
        assert isinstance(sensor.native_value, int)

    def test_rounds_up_above_the_midpoint(self):
        # 243 permille -> 24.3% -> rounds to 24
        sensor = self._make_sensor({"RoyalBlue": 243.0})
        assert sensor.native_value == 24

    def test_evenly_divisible_value_is_still_a_true_int(self):
        # The actual point of this fix: even a value that already divides
        # evenly must come out as a real int (100), not a float (100.0) --
        # confirms the fix isn't just rounding differently, it's actually
        # returning int, not round(x, 0) which would still be a float.
        sensor = self._make_sensor({"RoyalBlue": 1000.0})
        assert sensor.native_value == 100
        assert isinstance(sensor.native_value, int)
        assert not isinstance(sensor.native_value, float)

    def test_returns_none_when_channel_not_present(self):
        sensor = self._make_sensor({})
        assert sensor.native_value is None


# --------------------------------------------------------------------------
# AdvancedFeatures sensors -- deliberately generic (per-attribute, not
# per-device-type): each of the four is created independently based on
# whether python-mobius's own get_advanced_features() actually returned a
# value for it, never hardcoded to "pumps get these two, lights get those
# two". See sensor.py's own _build_advanced_feature_entities() docstring.
# --------------------------------------------------------------------------

async def test_vortech_style_advanced_features_create_only_the_relevant_two(hass):
    """A pump reporting only LocalControlEnabled/AutoDimTimeout (the
    app's own VorTech-relevant subset) -- confirms MaxFanSpeed/
    FanShutdownEnabled sensors are NOT created for it, since this
    device never reported anything for those two."""
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
    fake_device.get_advanced_features = AsyncMock(return_value=AdvancedFeatures(
        local_control_enabled=True, auto_dim_timeout=300,
        max_fan_speed=None, fan_shutdown_enabled=None,
    ))

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    local_control = hass.states.get("sensor.mp40qd_right_local_control")
    assert local_control is not None
    assert local_control.state == "True"

    auto_dim = hass.states.get("sensor.mp40qd_right_led_auto_dim_timeout")
    assert auto_dim is not None
    assert auto_dim.state == "300"
    assert auto_dim.attributes["unit_of_measurement"] == "s"

    assert hass.states.get("sensor.mp40qd_right_max_fan_speed") is None
    assert hass.states.get("sensor.mp40qd_right_fan_shutdown") is None


async def test_radion_style_advanced_features_create_only_the_relevant_two(hass):
    """The reverse combination -- a light reporting only MaxFanSpeed/
    FanShutdownEnabled (the app's own Radion-relevant subset) --
    confirms the same generic mechanism works for the opposite subset
    with no device-type-specific code path."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: LIGHT_SERIAL, CONF_ADDRESS: LIGHT_ADDRESS}],
        },
        unique_id=LIGHT_SERIAL,
    )
    entry.add_to_hass(hass)

    fake_device = _fake_light_device()
    fake_device.get_advanced_features = AsyncMock(return_value=AdvancedFeatures(
        local_control_enabled=None, auto_dim_timeout=None,
        max_fan_speed=60.0, fan_shutdown_enabled=False,
    ))

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=fake_device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe005678")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    max_fan = hass.states.get("sensor.radionxr15wg6pro_fakeserial0001_max_fan_speed")
    assert max_fan is not None
    assert max_fan.state == "60.0"
    assert max_fan.attributes["unit_of_measurement"] == "%"

    fan_shutdown = hass.states.get("sensor.radionxr15wg6pro_fakeserial0001_fan_shutdown")
    assert fan_shutdown is not None
    assert fan_shutdown.state == "False"

    assert hass.states.get("sensor.radionxr15wg6pro_fakeserial0001_local_control") is None
    assert hass.states.get("sensor.radionxr15wg6pro_fakeserial0001_auto_dim_timeout") is None


async def test_no_advanced_features_sensors_when_device_supports_none(hass):
    """The existing _fake_pump_device()/_fake_light_device() fixtures
    already mock get_advanced_features() to return None -- this just
    makes that expectation explicit for one of them, rather than
    relying only on the other tests never mentioning these sensors."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_PAN_ID: PAN_ID,
            CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, CONF_ADDRESS: PUMP_ADDRESS}],
        },
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)

    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=_fake_pump_device()),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial",
        AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    # Real entity ID suffixes (from the actual friendly names), not the
    # dataclass field names -- using the field names here would still
    # pass trivially (a wrong-but-also-nonexistent ID also returns None),
    # without actually confirming the real entity is absent.
    for key in ("local_control", "led_auto_dim_timeout", "max_fan_speed", "fan_shutdown"):
        assert hass.states.get(f"sensor.mp40qd_right_{key}") is None
