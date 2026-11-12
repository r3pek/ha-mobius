"""Dosing pump (Versa) container entities: coordinator.data["dose"] built
from python-mobius DoseInfo, and the sensor/binary sensor/button/number
entities built from it. Decoding, the schedule targets and the container
writes themselves are python-mobius' (tests/test_dose.py there)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.exceptions import HomeAssistantError

from mobius import ContainerMonitorType, DoseHaltState, DoseInfo, DoseSensorType, MetadataSnapshot

from tests.fake_full_poll import full_poll_from_reads

from custom_components.mobius import coordinator as coord
from custom_components.mobius.binary_sensor import ContainerEmptyBinarySensor, _build_binary_sensors
from custom_components.mobius.button import RefillContainerButton, _build_refill_buttons
from custom_components.mobius.number import ContainerSizeNumber, _build_device_numbers
from custom_components.mobius.sensor import (
    CONTAINER_MONITOR_OPTIONS,
    DOSE_HALT_STATE_OPTIONS,
    ContainerDaysLeftSensor,
    ContainerLevelSensor,
    ContainerMonitorSensor,
    ContainerRemainingSensor,
    DoseDailyTargetSensor,
    DoseExpectedSoFarSensor,
    DoseHaltStateSensor,
    DosedTodaySensor,
    _build_dose_entities,
)

SERIAL = "DOSE0000000001"


# --- coordinator data -------------------------------------------------------


def test_dose_info_dict():
    assert coord._dose_info_dict(None) is None
    assert coord._dose_info_dict(DoseInfo(
        container_current_volume_ul=123_000, container_volume_ul=500_000, dispensed_today_ul=1500,
        daily_target_ul=2000, expected_so_far_ul=1000,
        container_monitor=ContainerMonitorType.Countdown, sensor_type=DoseSensorType.NoSensor,
        halt_state=DoseHaltState.Pause,
    )) == {
        "container_volume_ul": 500_000,
        "container_current_volume_ul": 123_000,
        "dispensed_today_ul": 1500,
        "daily_target_ul": 2000,
        "expected_so_far_ul": 1000,
        "container_monitor": "countdown",
        "dose_sensor_type": "none",
        "halt_state": "pause",
    }


def test_dose_info_dict_keeps_unknown_values_as_none():
    dose = coord._dose_info_dict(DoseInfo(container_current_volume_ul=0))
    assert dose["container_current_volume_ul"] == 0
    assert dose["container_monitor"] is None and dose["halt_state"] is None


# --- entities ---------------------------------------------------------------


def _coordinator(dose):
    coordinator = MagicMock()
    coordinator.data = {"dose": dose} if dose is not None else {}
    coordinator.last_update_success = True
    coordinator.async_request_refresh = AsyncMock()
    return coordinator


def test_dose_entities_are_only_built_for_devices_with_a_container():
    dose = {"container_volume_ul": 500_000, "container_current_volume_ul": 1}
    c = _coordinator(dose)
    keys = {e.unique_id.removeprefix(f"{SERIAL}_") for e in _build_dose_entities(c, SERIAL, {})}
    assert {"container_remaining", "container_level", "container_days_left", "dosed_today"} <= keys
    assert _build_binary_sensors(c, SERIAL, {}, {"dose": dose})
    assert _build_refill_buttons(c, SERIAL, {}, {"dose": dose})
    assert [e.unique_id for e in _build_device_numbers(c, SERIAL, {}, {"dose": dose})] == [f"{SERIAL}_container_size"]
    for build in (_build_binary_sensors, _build_refill_buttons, _build_device_numbers):
        assert build(_coordinator(None), SERIAL, {}, {"dose": None}) == []


def test_sensor_values():
    dose = {"container_volume_ul": 500_000, "container_current_volume_ul": 123_456,
            "dispensed_today_ul": 1500, "daily_target_ul": 2000}
    c = _coordinator(dose)
    assert ContainerRemainingSensor(c, SERIAL, {}).native_value == 123.5
    assert ContainerLevelSensor(c, SERIAL, {}).native_value == 25
    assert DosedTodaySensor(c, SERIAL, {}).native_value == 1.5
    assert DoseDailyTargetSensor(c, SERIAL, {}).native_value == 2.0
    assert ContainerDaysLeftSensor(c, SERIAL, {}).native_value == 61.7


def test_days_left_without_schedule_and_with_negative_remaining():
    assert ContainerDaysLeftSensor(_coordinator(
        {"container_current_volume_ul": 1000, "daily_target_ul": None}), SERIAL, {}).native_value is None
    assert ContainerDaysLeftSensor(_coordinator(
        {"container_current_volume_ul": -500, "daily_target_ul": 2000}), SERIAL, {}).native_value == 0


def test_container_empty_binary_sensor():
    assert ContainerEmptyBinarySensor(_coordinator({"container_current_volume_ul": 0}), SERIAL, {}).is_on is True
    assert ContainerEmptyBinarySensor(_coordinator({"container_current_volume_ul": 10}), SERIAL, {}).is_on is False
    assert ContainerEmptyBinarySensor(_coordinator({"container_current_volume_ul": None}), SERIAL, {}).is_on is None


async def test_refill_writes_on_the_first_press():
    device = MagicMock()
    device.refill_container = AsyncMock()
    c = _coordinator({"container_volume_ul": 500_000})
    c.async_get_connected_device = AsyncMock(return_value=device)

    await RefillContainerButton(c, SERIAL, {}).async_press()
    # The library reads the current container size itself.
    device.refill_container.assert_awaited_once_with()
    c.configuration_cache.invalidate_static.assert_called_once()
    c.async_request_refresh.assert_awaited_once()


async def test_refill_failure_is_a_home_assistant_error():
    device = MagicMock()
    device.refill_container = AsyncMock(side_effect=ValueError("the container size is unknown"))
    c = _coordinator({"container_volume_ul": None})
    c.async_get_connected_device = AsyncMock(return_value=device)

    with pytest.raises(HomeAssistantError, match="container size is unknown"):
        await RefillContainerButton(c, SERIAL, {}).async_press()
    device.refill_container.assert_awaited_once_with()


async def test_container_size_number_resets_the_remaining_volume_like_the_app():
    device = MagicMock()
    device.set_container_volume = AsyncMock()
    c = _coordinator({"container_volume_ul": 500_000})
    c.async_get_connected_device = AsyncMock(return_value=device)
    number = ContainerSizeNumber(c, SERIAL, "container_size", "mdi:cup-outline", {})

    assert number.native_value == 500.0
    await number.async_set_native_value(250)
    # set_container_volume() also sets the remaining volume (default).
    device.set_container_volume.assert_awaited_once_with(250_000)


def test_enum_states_match_the_sensor_options():
    assert [coord._enum_key(v) for v in ContainerMonitorType] == CONTAINER_MONITOR_OPTIONS
    assert [coord._enum_key(v) for v in DoseHaltState] == DOSE_HALT_STATE_OPTIONS


def test_only_the_no_members_map_to_none():
    assert coord._enum_key(ContainerMonitorType.NoMonitor) == "none"
    assert coord._enum_key(DoseSensorType.NoSensor) == "none"
    # Same int value (0) as the two above, but not a "no" member.
    assert coord._enum_key(DoseHaltState.Running) == "running"
    # A name that merely starts with "no" keeps its name.
    fake = MagicMock()
    fake.name = "Normal"
    assert coord._enum_key(fake) == "normal"


def test_expected_so_far_has_no_long_term_sum_statistics():
    entity = DoseExpectedSoFarSensor(_coordinator({"expected_so_far_ul": 1000}), SERIAL, {})
    assert entity.state_class is None
    assert DosedTodaySensor(_coordinator({}), SERIAL, {}).state_class == "total_increasing"
    c = _coordinator({"container_monitor": "countdown", "halt_state": "running"})
    for cls, options in ((ContainerMonitorSensor, CONTAINER_MONITOR_OPTIONS),
                         (DoseHaltStateSensor, DOSE_HALT_STATE_OPTIONS)):
        entity = cls(c, SERIAL, {})
        assert entity.device_class == "enum" and entity.options == options


def test_level_entities_are_unavailable_without_container_monitoring():
    dose = {"container_volume_ul": 500_000, "container_current_volume_ul": 0,
            "daily_target_ul": 2000, "container_monitor": "none"}
    c = _coordinator(dose)
    for entity in (ContainerRemainingSensor(c, SERIAL, {}), ContainerLevelSensor(c, SERIAL, {}),
                   ContainerDaysLeftSensor(c, SERIAL, {}), ContainerEmptyBinarySensor(c, SERIAL, {}),
                   RefillContainerButton(c, SERIAL, {})):
        assert entity.available is False, entity
    assert DosedTodaySensor(c, SERIAL, {}).available is True
    assert ContainerMonitorSensor(c, SERIAL, {}).available is True
    # Countdown, sensor and an unknown monitor type keep them available.
    for monitor in ("countdown", "sensor", None):
        dose["container_monitor"] = monitor
        assert ContainerRemainingSensor(c, SERIAL, {}).available is True
        assert RefillContainerButton(c, SERIAL, {}).available is True


def test_container_size_number_without_a_size():
    number = ContainerSizeNumber(_coordinator({"container_volume_ul": None}), SERIAL,
                                 "container_size", "mdi:cup-outline", {})
    assert number.native_value is None


# --- coordinator poll path ----------------------------------------------------

def _fake_dosing_pump(dose: DoseInfo) -> MagicMock:
    device = MagicMock()
    device.get_device_info = AsyncMock(return_value={
        "model_raw": 272, "model": "VersaVX1", "manufacturer": "EcoTech Marine",
        "name": "Versa", "serial": SERIAL,
        "primitive_type": "DoseV1", "error_state": "NoError", "mac_address": None,
    })
    device.get_metadata_batch = AsyncMock(return_value=MetadataSnapshot(
        advanced_features=None, calibration=None, hardware_info={}, firmware_versions={},
        supported_channels=[], error_state=None, epoch=None, local_time=None, tz_offset=None,
    ))
    device.get_dose_info = AsyncMock(return_value=dose)
    full_poll_from_reads(device)
    return device


async def test_a_poll_fills_dose_and_drops_the_support_note():
    dose = DoseInfo(
        container_current_volume_ul=123_000, container_volume_ul=500_000, dispensed_today_ul=1500,
        daily_target_ul=4000, expected_so_far_ul=1000,
        container_monitor=ContainerMonitorType.Countdown, sensor_type=DoseSensorType.NoSensor,
        halt_state=DoseHaltState.Running,
    )
    info, *_ = await coord._fetch_all(_fake_dosing_pump(dose))
    assert info["dose"]["container_current_volume_ul"] == 123_000
    assert info["dose"]["container_monitor"] == "countdown"
    assert info["dose"]["halt_state"] == "running"
    assert "support_note" not in info or not info["support_note"]
    assert _build_dose_entities(MagicMock(data=info), SERIAL, {})


async def test_a_poll_without_dose_info_keeps_the_support_note():
    info, *_ = await coord._fetch_all(_fake_dosing_pump(None))
    assert info["dose"] is None
    assert "no parser implemented" in info["support_note"]
