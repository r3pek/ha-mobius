"""
Tests for _resolve_tank_groups() and handle_resolve_schedule_groups()
-- see websocket_api.py's own module docstring, and
mobius-schedule-card-implementation-plan.md's own "Validation rules"
section for the numbered rules referenced throughout.

Deliberately doesn't run a full async_setup_entry() (which would need
to mock BLE discovery/connection end to end) -- _resolve_tank_groups()
only ever touches the device registry and coordinator.data, so this
builds just enough of that state directly: a Tank device entry, a real
device entry per serial, and MobiusDeviceCoordinator instances with
.data set directly rather than fetched from a real poll.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius import MobiusRuntimeData, tank_device_identifier
from custom_components.mobius.const import DOMAIN
from custom_components.mobius.coordinator import MobiusDeviceCoordinator
from custom_components.mobius.gateway_registry import GatewayRegistry
from custom_components.mobius.websocket_api import (
    _resolve_tank_groups, _member_device_id, _resolve_member, ScheduleGroupError, PUMP_MODE_NAMES,
    handle_resolve_schedule_groups, handle_read_schedule_group,
    _serial_for_master_hex, _master_hex_for_serial, _translate_master_to_parent_serial,
)
from mobius import SchedulePoint, LightPrimitive, VisualID, PumpSchedulePoint, PumpPrimitiveValue, PumpMode, PumpParam

PAN_ID = 0x3D0F


def _make_registry(hass) -> GatewayRegistry:
    return GatewayRegistry(hass, asyncio.Semaphore(2))


def _make_coordinator(hass, entry, registry, serial: str, data: dict) -> MobiusDeviceCoordinator:
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, serial, PAN_ID)
    coordinator.data = data
    return coordinator


def _register_real_device(hass, entry, serial: str) -> str:
    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, serial)},
    )
    return device_entry.id


def _setup_tank(hass, serial_and_data: dict[str, dict]) -> tuple[MockConfigEntry, str]:
    """Registers a Tank device, one real device entry per serial, and
    a MobiusRuntimeData with one coordinator per serial (data set
    directly, no real poll). Returns (entry, tank_device_id)."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=f"test-{PAN_ID:04x}")
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    tank_identifier = tank_device_identifier(None, PAN_ID)
    tank_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={tank_identifier},
    )

    registry = _make_registry(hass)
    coordinators = {}
    for serial, data in serial_and_data.items():
        _register_real_device(hass, entry, serial)
        coordinators[serial] = _make_coordinator(hass, entry, registry, serial, data)

    entry.runtime_data = MobiusRuntimeData(coordinators=coordinators)
    return entry, tank_device.id


def _light_data(name: str, group_mask, channels=None) -> dict:
    return {
        "support": "light", "name": name, "model": "RadionXR15wG6Pro",
        "group_mask": group_mask, "channels": channels or ["RoyalBlue", "CoolWhite"],
    }


def _pump_data(name: str) -> dict:
    return {"support": "pump", "name": name, "model": "VorTechMP40wG3QD"}


# --------------------------------------------------------------------------
# Validation rules #1, #2, #4 -- errors
# --------------------------------------------------------------------------

async def test_unknown_device_id_raises_not_found(hass):
    with pytest.raises(ScheduleGroupError) as exc_info:
        _resolve_tank_groups(hass, "does-not-exist")
    assert exc_info.value.code == "not_found"


async def test_a_real_device_id_is_rejected_not_a_tank(hass):
    """Rule #1 -- device_id must be the Tank, not a light/pump directly."""
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Light", 12345)})
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")

    with pytest.raises(ScheduleGroupError) as exc_info:
        _resolve_tank_groups(hass, real_device_id)
    assert exc_info.value.code == "not_supported"
    assert "not a Tank device" in exc_info.value.message


async def test_tank_with_no_runtime_data_raises_not_found(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id="no-runtime")
    entry.add_to_hass(hass)
    device_registry = dr.async_get(hass)
    tank_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={tank_device_identifier(None, PAN_ID)},
    )
    # entry.runtime_data deliberately never set.

    with pytest.raises(ScheduleGroupError) as exc_info:
        _resolve_tank_groups(hass, tank_device.id)
    assert exc_info.value.code == "not_found"


async def test_tank_with_nothing_schedulable_returns_empty_list(hass):
    """Rule #2 -- not an error, just an empty result."""
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": {"support": "unsupported", "name": "Doser"},
    })
    groups = _resolve_tank_groups(hass, tank_device_id)
    assert groups == []


# --------------------------------------------------------------------------
# Rule #3 -- exclusion, not an error
# --------------------------------------------------------------------------

async def test_experimental_pump_excluded(hass):
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": {"support": "pump (experimental)", "name": "NYOS Quantum"},
    })
    assert _resolve_tank_groups(hass, tank_device_id) == []


async def test_unsupported_device_excluded_but_others_still_included(hass):
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": _light_data("Light", None),
        "SN2": {"support": "unsupported", "name": "Doser"},
    })
    groups = _resolve_tank_groups(hass, tank_device_id)
    assert len(groups) == 1
    assert groups[0].kind == "light"


# --------------------------------------------------------------------------
# Grouping logic itself
# --------------------------------------------------------------------------

async def test_two_lights_matching_group_mask_collapse_into_one_group(hass):
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": _light_data("Left", 12345),
        "SN2": _light_data("Right", 12345),
    })
    groups = _resolve_tank_groups(hass, tank_device_id)
    assert len(groups) == 1
    assert groups[0].kind == "light"
    assert groups[0].group_mask == 12345
    assert {m.serial for m in groups[0].members} == {"SN1", "SN2"}


async def test_two_lights_different_group_mask_stay_separate(hass):
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": _light_data("Left", 111),
        "SN2": _light_data("Right", 222),
    })
    groups = _resolve_tank_groups(hass, tank_device_id)
    assert len(groups) == 2
    assert {g.group_mask for g in groups} == {111, 222}


async def test_two_lights_with_none_group_mask_stay_separate(hass):
    """The key reminder: group_mask None means "this light's own
    exclusive group," never "matches every other ungrouped light" --
    two ungrouped lights must NOT collapse together just because they
    share the same None value."""
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": _light_data("Left", None),
        "SN2": _light_data("Right", None),
    })
    groups = _resolve_tank_groups(hass, tank_device_id)
    assert len(groups) == 2
    for g in groups:
        assert g.group_mask is None
        assert len(g.members) == 1


async def test_pump_is_always_its_own_group(hass):
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": _pump_data("Pump A"),
        "SN2": _pump_data("Pump B"),
    })
    groups = _resolve_tank_groups(hass, tank_device_id)
    assert len(groups) == 2
    for g in groups:
        assert g.kind == "pump"
        assert g.group_mask is None
        assert len(g.members) == 1


async def test_mixed_tank_with_grouped_lights_and_a_pump(hass):
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": _light_data("Left", 999),
        "SN2": _light_data("Right", 999),
        "SN3": _pump_data("Pump"),
    })
    groups = _resolve_tank_groups(hass, tank_device_id)
    kinds = sorted(g.kind for g in groups)
    assert kinds == ["light", "pump"]
    light_group = next(g for g in groups if g.kind == "light")
    assert len(light_group.members) == 2


# --------------------------------------------------------------------------
# Rendering metadata
# --------------------------------------------------------------------------

async def test_light_group_includes_channels(hass):
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": _light_data("Left", None, channels=["RoyalBlue", "Violet", "CoolWhite"]),
    })
    groups = _resolve_tank_groups(hass, tank_device_id)
    assert groups[0].channels == ["RoyalBlue", "Violet", "CoolWhite"]
    assert groups[0].as_dict()["channels"] == ["RoyalBlue", "Violet", "CoolWhite"]
    assert "modes" not in groups[0].as_dict()


async def test_pump_group_includes_modes(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump")})
    groups = _resolve_tank_groups(hass, tank_device_id)
    assert groups[0].modes == PUMP_MODE_NAMES
    assert groups[0].as_dict()["modes"] == PUMP_MODE_NAMES
    assert "channels" not in groups[0].as_dict()


async def test_member_dict_has_device_id_serial_and_name(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left Radion", None)})
    groups = _resolve_tank_groups(hass, tank_device_id)
    member = groups[0].as_dict()["members"][0]
    assert member["serial"] == "SN1"
    assert member["name"] == "Left Radion"
    assert member["device_id"] == _member_device_id(hass, entry.entry_id, "SN1")


async def test_member_name_falls_back_to_bare_serial_when_unnamed(hass):
    """Explicit product decision: the bare serial, not "<model>
    (<serial>)" -- a device that's never been named via the app
    should read as just its own serial in the schedule editor."""
    unnamed = {"support": "light", "model": "RadionXR15wG6Pro", "group_mask": None,
               "channels": ["RoyalBlue"]}  # no "name" key at all
    entry, tank_device_id = _setup_tank(hass, {"SN1": unnamed})
    groups = _resolve_tank_groups(hass, tank_device_id)
    assert groups[0].as_dict()["members"][0]["name"] == "SN1"


# --------------------------------------------------------------------------
# The websocket command itself
# --------------------------------------------------------------------------

async def test_websocket_command_sends_result_with_groups(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left", None)})
    connection = MagicMock()
    msg = {"id": 1, "type": "mobius/resolve_schedule_groups", "device_id": tank_device_id}

    # @websocket_api.async_response turns the function into a sync
    # @callback that schedules the real work as a background task
    # (see decorators.py's own async_response()) -- calling it
    # directly wouldn't even be awaitable. __wrapped__ (from
    # @functools.wraps, which async_response uses) is the actual
    # async handler underneath.
    await handle_resolve_schedule_groups.__wrapped__(hass, connection, msg)

    connection.send_result.assert_called_once()
    call_args = connection.send_result.call_args[0]
    assert call_args[0] == 1
    assert len(call_args[1]["groups"]) == 1
    connection.send_error.assert_not_called()


async def test_websocket_command_sends_error_for_unknown_device(hass):
    connection = MagicMock()
    msg = {"id": 2, "type": "mobius/resolve_schedule_groups", "device_id": "nope"}

    await handle_resolve_schedule_groups.__wrapped__(hass, connection, msg)

    connection.send_error.assert_called_once()
    error_args = connection.send_error.call_args[0]
    assert error_args[0] == 2
    assert error_args[1] == "not_found"
    connection.send_result.assert_not_called()


# --------------------------------------------------------------------------
# _resolve_member()
# --------------------------------------------------------------------------

async def test_resolve_member_not_found_for_unknown_device(hass):
    with pytest.raises(ScheduleGroupError) as exc_info:
        _resolve_member(hass, "does-not-exist")
    assert exc_info.value.code == "not_found"


async def test_resolve_member_rejects_a_tank_device_id(hass):
    """The opposite direction from _resolve_tank_groups()'s own rule
    #1 -- read/write take a real device's own device_id, never the
    Tank's."""
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left", None)})

    with pytest.raises(ScheduleGroupError) as exc_info:
        _resolve_member(hass, tank_device_id)
    assert exc_info.value.code == "not_supported"
    assert "is a Tank device" in exc_info.value.message


async def test_resolve_member_returns_serial_runtime_coordinator(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left", None)})
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")

    serial, runtime, coordinator = _resolve_member(hass, real_device_id)

    assert serial == "SN1"
    assert runtime is entry.runtime_data
    assert coordinator is entry.runtime_data.coordinators["SN1"]


# --------------------------------------------------------------------------
# handle_read_schedule_group()
# --------------------------------------------------------------------------

def _fake_light_device(points):
    device = MagicMock()
    device.get_light_schedule = AsyncMock(return_value=points)
    return device


def _fake_pump_device(points):
    device = MagicMock()
    device.get_pump_schedule = AsyncMock(return_value=points)
    return device


async def test_read_schedule_group_light(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left", None)})
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")
    coordinator = entry.runtime_data.coordinators["SN1"]

    points = [SchedulePoint(480, SchedulePoint.FLAG_ACTIVE, LightPrimitive({VisualID.RoyalBlue: 500}))]
    coordinator.async_get_connected_device = AsyncMock(return_value=_fake_light_device(points))

    connection = MagicMock()
    msg = {"id": 1, "type": "mobius/read_schedule_group", "device_id": real_device_id}
    await handle_read_schedule_group.__wrapped__(hass, connection, msg)

    connection.send_result.assert_called_once()
    result = connection.send_result.call_args[0][1]
    assert result["points"][0]["time_minutes"] == 480
    connection.send_error.assert_not_called()


async def test_read_schedule_group_pump(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump")})
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")
    coordinator = entry.runtime_data.coordinators["SN1"]

    points = [PumpSchedulePoint(0, SchedulePoint.FLAG_ACTIVE,
                                 PumpPrimitiveValue(PumpMode.ConstantSpeed, {PumpParam.MaxSpeed: 750}))]
    coordinator.async_get_connected_device = AsyncMock(return_value=_fake_pump_device(points))

    connection = MagicMock()
    msg = {"id": 2, "type": "mobius/read_schedule_group", "device_id": real_device_id}
    await handle_read_schedule_group.__wrapped__(hass, connection, msg)

    connection.send_result.assert_called_once()
    result = connection.send_result.call_args[0][1]
    assert result["points"][0]["mode"] == "ConstantSpeed"
    connection.send_error.assert_not_called()


async def test_read_schedule_group_unsupported_device(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": {"support": "unsupported", "name": "Doser"}})
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")
    coordinator = entry.runtime_data.coordinators["SN1"]
    coordinator.async_get_connected_device = AsyncMock(return_value=MagicMock())

    connection = MagicMock()
    msg = {"id": 3, "type": "mobius/read_schedule_group", "device_id": real_device_id}
    await handle_read_schedule_group.__wrapped__(hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "not_supported"
    connection.send_result.assert_not_called()


async def test_read_schedule_group_connection_failure(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left", None)})
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")
    coordinator = entry.runtime_data.coordinators["SN1"]
    coordinator.async_get_connected_device = AsyncMock(
        side_effect=HomeAssistantError("No gateway currently available"),
    )

    connection = MagicMock()
    msg = {"id": 4, "type": "mobius/read_schedule_group", "device_id": real_device_id}
    await handle_read_schedule_group.__wrapped__(hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "home_assistant_error"
    connection.send_result.assert_not_called()


async def test_read_schedule_group_unknown_device(hass):
    connection = MagicMock()
    msg = {"id": 5, "type": "mobius/read_schedule_group", "device_id": "nope"}
    await handle_read_schedule_group.__wrapped__(hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "not_found"
    connection.send_result.assert_not_called()


# --------------------------------------------------------------------------
# Master hex <-> serial translation (Sync/EcoSmartBack's own "parent pump")
# --------------------------------------------------------------------------

async def test_serial_for_master_hex_finds_matching_device(hass):
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": _pump_data("Pump A"),
        "SN2": _pump_data("Pump B"),
    })
    coordinator = entry.runtime_data.coordinators["SN1"]
    registry = coordinator.registry
    await registry.join(PAN_ID, "SN1", rssi=-50)
    await registry.join(PAN_ID, "SN2", rssi=-50)
    registry.group(PAN_ID).members["SN2"].mesh_address = bytes.fromhex("fdaaaaaaaaaaaaaa0000000000000042")

    assert _serial_for_master_hex("0000000000000042", coordinator) == "SN2"


async def test_serial_for_master_hex_returns_none_when_unmatched(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump A")})
    coordinator = entry.runtime_data.coordinators["SN1"]
    registry = coordinator.registry
    await registry.join(PAN_ID, "SN1", rssi=-50)

    assert _serial_for_master_hex("ffffffffffffffff", coordinator) is None


async def test_master_hex_for_serial_returns_last_8_bytes(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump A"), "SN2": _pump_data("Pump B")})
    coordinator = entry.runtime_data.coordinators["SN1"]
    registry = coordinator.registry
    await registry.join(PAN_ID, "SN1", rssi=-50)
    await registry.join(PAN_ID, "SN2", rssi=-50)
    registry.group(PAN_ID).members["SN2"].mesh_address = bytes.fromhex("fdaaaaaaaaaaaaaa0000000000000042")

    assert _master_hex_for_serial("SN2", coordinator) == "0000000000000042"


async def test_master_hex_for_serial_returns_none_when_address_unknown(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump A"), "SN2": _pump_data("Pump B")})
    coordinator = entry.runtime_data.coordinators["SN1"]
    registry = coordinator.registry
    await registry.join(PAN_ID, "SN1", rssi=-50)
    await registry.join(PAN_ID, "SN2", rssi=-50)
    # SN2's own mesh_address deliberately left None (never reached yet).

    assert _master_hex_for_serial("SN2", coordinator) is None


async def test_translate_master_to_parent_serial_mutates_pump_points(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump A"), "SN2": _pump_data("Pump B")})
    coordinator = entry.runtime_data.coordinators["SN1"]
    registry = coordinator.registry
    await registry.join(PAN_ID, "SN1", rssi=-50)
    await registry.join(PAN_ID, "SN2", rssi=-50)
    registry.group(PAN_ID).members["SN2"].mesh_address = bytes.fromhex("fdaaaaaaaaaaaaaa0000000000000042")

    points_dict = [
        {"time_minutes": 0, "flags": 1, "mode": "Sync",
         "params": {"MaxSpeed": 500, "PhaseShift": 90, "Master": "0000000000000042"}},
        {"time_minutes": 60, "flags": 1, "mode": "ConstantSpeed", "params": {"MaxSpeed": 300}},
    ]
    _translate_master_to_parent_serial(points_dict, coordinator)

    assert points_dict[0]["params"]["ParentSerial"] == "SN2"
    assert "Master" not in points_dict[0]["params"]
    assert points_dict[1]["params"] == {"MaxSpeed": 300}  # untouched, no Master at all


async def test_translate_master_to_parent_serial_none_when_unresolvable(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump A")})
    coordinator = entry.runtime_data.coordinators["SN1"]
    registry = coordinator.registry
    await registry.join(PAN_ID, "SN1", rssi=-50)

    points_dict = [{"time_minutes": 0, "flags": 1, "mode": "Sync",
                    "params": {"MaxSpeed": 500, "PhaseShift": 90, "Master": "ffffffffffffffff"}}]
    _translate_master_to_parent_serial(points_dict, coordinator)

    assert points_dict[0]["params"]["ParentSerial"] is None
