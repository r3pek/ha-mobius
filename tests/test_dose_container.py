"""Dosing pump (Versa) container entities: coordinator.data["dose"] built
from python-mobius DoseInfo, and the sensor/binary sensor/button/number
entities built from it. Decoding, the schedule targets and the container
writes themselves are python-mobius' (tests/test_dose.py there)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from homeassistant.exceptions import HomeAssistantError

from mobius import ContainerMonitorType, DoseHaltState, DoseInfo, DoseSensorType

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
    device.refill_container.assert_awaited_once_with(500_000)
    c.configuration_cache.invalidate_static.assert_called_once()
    c.async_request_refresh.assert_awaited_once()


async def test_refill_failure_is_a_home_assistant_error():
    device = MagicMock()
    device.refill_container = AsyncMock(side_effect=ValueError("the container size is unknown"))
    c = _coordinator({"container_volume_ul": None})
    c.async_get_connected_device = AsyncMock(return_value=device)

    with pytest.raises(HomeAssistantError, match="container size is unknown"):
        await RefillContainerButton(c, SERIAL, {}).async_press()
    device.refill_container.assert_awaited_once_with(None)


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
