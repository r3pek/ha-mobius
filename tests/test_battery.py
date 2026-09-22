"""Pump battery settings (number.py) and the running-on-battery binary
sensor (binary_sensor.py)."""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from mobius import Tank, MetadataSnapshot
from mobius.pump_status import BatteryBackupInfo, BoostedBatteryInfo

from custom_components.mobius.const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES

PAN_ID = 0x3D0F
PUMP_SERIAL = "00000000000001"
SPEED = "number.mp40_right_battery_backup_speed"
BOOSTED_POWER = "number.mp40_right_boosted_battery_power"
ON_BATTERY = "binary_sensor.mp40_right_running_on_battery"


def _pump(battery_backup=None, boosted_battery=None, operation_mode="Reefcrest"):
    device = MagicMock()
    device.serial = PUMP_SERIAL
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 42, "model": "VorTechMP40wG3QD", "manufacturer": "EcoTech Marine",
        "name": "MP40 Right", "serial": PUMP_SERIAL,
        "primitive_type": "VorTechV1", "error_state": "NoError", "mac_address": None,
    })
    device.get_pump_telemetry = AsyncMock(return_value={
        "speed": 447, "speed_percent": 44.7, "gph": 2272, "gph_reliable": True,
        "minimum_gph": 200, "maximum_gph": 2500, "operation_mode": operation_mode,
    })
    device.get_battery_backup_info = AsyncMock(return_value=battery_backup)
    device.get_boosted_battery_info = AsyncMock(return_value=boosted_battery)
    device.set_battery_backup_speed = AsyncMock()
    device.set_boosted_battery = AsyncMock()
    device.get_operation_state = AsyncMock()
    device.get_operation_state.return_value.name = "Schedule"
    point = MagicMock()
    point.pump.mode.name = "ReefCrest"
    point.pump.params = {}
    device.get_pump_schedule = AsyncMock(return_value=[point])
    device.get_current_pump_block = AsyncMock(return_value=point)
    device.get_metadata_batch = AsyncMock(return_value=MetadataSnapshot(
        advanced_features=None, calibration=None, hardware_info={}, firmware_versions={},
        supported_channels=[], error_state=None, epoch=None, local_time=None, tz_offset=None,
    ))
    return device


@contextlib.asynccontextmanager
async def _setup(hass, device):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_PAN_ID: PAN_ID, CONF_DEVICES: [{CONF_SERIAL: PUMP_SERIAL, "address": "AA:AA:AA:AA:AA:01"}]},
        unique_id=PUMP_SERIAL,
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.mobius.coordinator.MobiusConnectionManager.ensure_connected",
        AsyncMock(return_value=device),
    ), patch(
        "custom_components.mobius.discover_tank_for_serial", AsyncMock(return_value=Tank(prefix=None, peers=[])),
    ), patch(
        "custom_components.mobius.discover_mesh_address",
        AsyncMock(return_value=bytes.fromhex("fdaaaaaaaaaaaaaa000000fffe001234")),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry


@pytest.mark.asyncio
async def test_no_battery_entities_without_battery_settings(hass):
    async with _setup(hass, _pump()):
        assert hass.states.get(SPEED) is None
        assert hass.states.get(BOOSTED_POWER) is None
        assert hass.states.get(ON_BATTERY) is None


@pytest.mark.asyncio
async def test_battery_backup_speed_number(hass):
    device = _pump(battery_backup=BatteryBackupInfo(speed=200, max_speed=498))
    async with _setup(hass, device):
        state = hass.states.get(SPEED)
        assert float(state.state) == 20
        assert state.attributes["max"] == 49
        assert state.attributes["max_speed"] == 49

        await hass.services.async_call(
            "number", "set_value", {"entity_id": SPEED, "value": 35}, blocking=True,
        )
        device.set_battery_backup_speed.assert_awaited_once_with(35.0, max_speed=498)
        # No boosted battery attributes on this pump.
        assert hass.states.get(BOOSTED_POWER) is None


@pytest.mark.asyncio
async def test_running_on_battery_binary_sensor(hass):
    device = _pump(battery_backup=BatteryBackupInfo(speed=200, max_speed=498), operation_mode="BatteryBackup")
    async with _setup(hass, device):
        assert hass.states.get(ON_BATTERY).state == "on"


@pytest.mark.asyncio
async def test_running_on_battery_binary_sensor_off_on_mains(hass):
    device = _pump(battery_backup=BatteryBackupInfo(speed=200, max_speed=498))
    async with _setup(hass, device):
        assert hass.states.get(ON_BATTERY).state == "off"


@pytest.mark.asyncio
async def test_boosted_battery_numbers_and_rejected_write(hass):
    device = _pump(
        battery_backup=BatteryBackupInfo(speed=200, max_speed=498),
        boosted_battery=BoostedBatteryInfo(power=800, on_time=30, off_time=60),
    )
    device.set_boosted_battery = AsyncMock(
        side_effect=ValueError("boosted battery settings can only be changed while the pump runs on battery"),
    )
    async with _setup(hass, device):
        assert float(hass.states.get(BOOSTED_POWER).state) == 80
        assert float(hass.states.get("number.mp40_right_boosted_battery_on_time").state) == 30
        assert float(hass.states.get("number.mp40_right_boosted_battery_off_time").state) == 60

        with pytest.raises(HomeAssistantError, match="runs on battery"):
            await hass.services.async_call(
                "number", "set_value", {"entity_id": BOOSTED_POWER, "value": 50}, blocking=True,
            )
        device.set_boosted_battery.assert_awaited_once_with(power=50.0)
