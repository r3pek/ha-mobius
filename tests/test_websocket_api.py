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
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.mobius import MobiusRuntimeData, tank_device_identifier
from custom_components.mobius.const import DOMAIN
from custom_components.mobius.coordinator import MobiusDeviceCoordinator
from custom_components.mobius.gateway_registry import GatewayRegistry
from custom_components.mobius.websocket_api import (
    _resolve_tank_groups, _member_device_id, _resolve_member, ScheduleGroupError, PUMP_MODE_NAMES,
    handle_resolve_schedule_groups, handle_read_schedule_group,
    handle_export_schedule_group_mob, handle_parse_schedule_mob,
    _serial_for_master_hex, _master_hex_for_serial, _translate_master_to_parent_serial,
    _tank_active_scene, _sensor_entity_id, _scene_entity_id,
)
from mobius import (
    SchedulePoint, LightPrimitive, VisualID, PumpSchedulePoint, PumpPrimitiveValue, PumpMode, PumpParam,
    ActiveScene, Scene, SceneID, PrimitiveType, supported_pump_modes,
    light_schedule_to_mob, pump_schedule_to_mob,
)

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
    return {
        "support": "pump", "name": name, "model": "VorTechMP40wG3QD",
        "primitive_type": "VorTechV1",
    }


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


async def test_group_members_are_sorted_by_serial_regardless_of_insertion_order(hass):
    """members[0] is what a card would treat as "the representative
    device" for anything that needs one specific member rather than
    the whole group (e.g. which device's own live sensors to chart) --
    it must be a genuinely stable, deterministic pick, not whatever
    order runtime.coordinators happened to be populated in."""
    entry, tank_device_id = _setup_tank(hass, {
        # Deliberately inserted in reverse-alphabetical order.
        "SN3": _light_data("Third", 999),
        "SN1": _light_data("First", 999),
        "SN2": _light_data("Second", 999),
    })
    groups = _resolve_tank_groups(hass, tank_device_id)
    assert [m.serial for m in groups[0].members] == ["SN1", "SN2", "SN3"]


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


async def test_light_group_includes_schedule_intensity_from_representative_member(hass):
    data = _light_data("Left", None)
    data["schedule_intensity"] = 0.588
    entry, tank_device_id = _setup_tank(hass, {"SN1": data})

    groups = _resolve_tank_groups(hass, tank_device_id)
    assert groups[0].schedule_intensity == 0.588
    assert groups[0].as_dict()["schedule_intensity"] == 0.588


async def test_pump_group_dict_has_no_schedule_intensity_key(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump")})
    groups = _resolve_tank_groups(hass, tank_device_id)
    assert "schedule_intensity" not in groups[0].as_dict()


async def test_pump_group_includes_modes(hass):
    """_pump_data()'s own fixture uses VorTechV1 -- modes must be
    filtered to what THAT primitive type actually supports, not the
    full PUMP_MODE_NAMES list (which nothing in production code uses
    for a group's own `modes` field any more -- see
    supported_pump_modes())."""
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump")})
    groups = _resolve_tank_groups(hass, tank_device_id)
    expected = [m.name for m in supported_pump_modes(PrimitiveType.VorTechV1)]
    assert groups[0].modes == expected
    assert groups[0].as_dict()["modes"] == expected
    assert len(expected) < len(PUMP_MODE_NAMES), "sanity check: VorTechV1 must NOT support every real mode"
    assert "channels" not in groups[0].as_dict()


async def test_pump_group_includes_mode_params_for_every_supported_mode(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump")})
    groups = _resolve_tank_groups(hass, tank_device_id)

    mode_params = groups[0].mode_params
    assert set(mode_params.keys()) == set(groups[0].modes)
    assert mode_params["ConstantSpeed"] == ["MaxSpeed"]
    assert mode_params["ShortPulse"] == ["MaxSpeed", "Time"]


async def test_sync_and_ecosmartback_are_distinct_entries_with_matching_params(hass):
    """They share the exact same parameter shape (MaxSpeed, PhaseShift,
    Master) -- confirming mode_params keeps them as two separate dict
    keys rather than collapsing them, since a person picking either
    one in the editor needs its own entry to look up, even though the
    fields shown would be identical either way."""
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump")})
    groups = _resolve_tank_groups(hass, tank_device_id)

    mode_params = groups[0].mode_params
    assert "Sync" in mode_params
    assert "EcoSmartBack" in mode_params
    assert mode_params["Sync"] == ["MaxSpeed", "PhaseShift", "Master"]
    assert mode_params["Sync"] == mode_params["EcoSmartBack"]


async def test_vectra_pump_group_reflects_closed_loop_true(hass):
    vectra_data = _pump_data("Vectra L2")
    vectra_data["primitive_type"] = "VectraV1"
    vectra_data["closed_loop"] = True
    entry, tank_device_id = _setup_tank(hass, {"SN1": vectra_data})

    groups = _resolve_tank_groups(hass, tank_device_id)

    assert groups[0].modes == [m.name for m in supported_pump_modes(PrimitiveType.VectraV1, closed_loop=True)]
    assert "Gyre" in groups[0].modes  # only offered in the closed-loop list


async def test_vectra_pump_group_reflects_closed_loop_false(hass):
    vectra_data = _pump_data("Vectra L2")
    vectra_data["primitive_type"] = "VectraV1"
    vectra_data["closed_loop"] = False
    entry, tank_device_id = _setup_tank(hass, {"SN1": vectra_data})

    groups = _resolve_tank_groups(hass, tank_device_id)

    assert groups[0].modes == [m.name for m in supported_pump_modes(PrimitiveType.VectraV1, closed_loop=False)]
    assert groups[0].modes == ["ConstantSpeed", "Feed"]


async def test_vectra_pump_group_with_unknown_closed_loop_gets_the_smaller_list(hass):
    """closed_loop absent entirely (get_vectra_info() itself failed,
    or hasn't resolved yet) -- must behave the same as a confirmed
    False, not as "unknown, so allow everything.\""""
    vectra_data = _pump_data("Vectra L2")
    vectra_data["primitive_type"] = "VectraV1"
    entry, tank_device_id = _setup_tank(hass, {"SN1": vectra_data})

    groups = _resolve_tank_groups(hass, tank_device_id)

    assert groups[0].modes == ["ConstantSpeed", "Feed"]


async def test_pump_group_modes_are_genuinely_json_serializable(hass):
    """supported_pump_modes() itself returns PumpMode enum members, not
    strings -- as_dict()'s own modes field must convert these, since
    an enum member isn't JSON-serializable at all and this crosses a
    real websocket boundary. Checks actual json.dumps() output, not
    just that the values happen to look like strings."""
    vectra_data = _pump_data("Vectra L2")
    vectra_data["primitive_type"] = "VectraV1"
    vectra_data["closed_loop"] = True
    entry, tank_device_id = _setup_tank(hass, {"SN1": vectra_data})

    groups = _resolve_tank_groups(hass, tank_device_id)

    serialized = json.dumps(groups[0].as_dict())
    modes = json.loads(serialized)["modes"]
    assert modes == ["ConstantSpeed", "ReefCrest", "Lagoon", "Gyre", "Feed"]
    assert all(isinstance(m, str) for m in modes)


def test_pump_mode_names_matches_every_real_mode_python_mobius_defines():
    """PUMP_MODE_NAMES is derived from PumpMode (`[m.name for m in
    PumpMode if m != PumpMode.Undefined]`), which protects it from
    manual-transcription drift -- but a derivation can still be wrong
    on its own terms (e.g. excluding the wrong member, or PumpMode
    itself changing without anyone noticing this list should grow).
    This checks it against an INDEPENDENT list transcribed directly
    from python-mobius's own documentation
    (documentation/07-pump-schedule.md's own mode/parameter table),
    not derived from PumpMode at all -- so a regression in either the
    enum or the derivation logic actually has something real to fail
    against, not just itself.

    All 15 real modes (Undefined(0) is a placeholder, not a real
    mode); note value 11 is genuinely unused/skipped between Sync(10)
    and EcoSmartBack(12).
    """
    expected = [
        "ConstantSpeed", "Lagoon", "ReefCrest", "NutrientTransport", "TidalSwell",
        "ShortPulse", "Gyre", "Transition", "ExpandingPulse", "Sync",
        "EcoSmartBack", "Feed", "BatteryBackup", "Random", "Pulse",
    ]
    assert PUMP_MODE_NAMES == expected
    assert len(PUMP_MODE_NAMES) == 15
    assert PumpMode.Undefined.name not in PUMP_MODE_NAMES


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


# --------------------------------------------------------------------------
# _tank_active_scene() -- distinguishing "a scene is active" from "the
# normal schedule is running"
# --------------------------------------------------------------------------

async def test_tank_active_scene_none_when_no_scene_active(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left", None)})
    assert _tank_active_scene(entry.runtime_data) is None


async def test_tank_active_scene_returns_name_from_configured_scenes(hass):
    data = _light_data("Left", None)
    data["configured_scenes"] = [
        Scene(index=0, id=5, scene_type=None, name="Feeding", timeout=0, light=None, pump=None),
    ]
    data["current_scene"] = ActiveScene(id=5, scene_type=None, duration_seconds=120)
    entry, tank_device_id = _setup_tank(hass, {"SN1": data})

    result = _tank_active_scene(entry.runtime_data)
    assert result == {"name": "Feeding", "duration_seconds": 120}


async def test_tank_active_scene_falls_back_to_generic_label_when_name_unknown(hass):
    data = _light_data("Left", None)
    data["configured_scenes"] = []  # this scene's own name isn't known to any device
    data["current_scene"] = ActiveScene(id=7, scene_type=None, duration_seconds=30)
    entry, tank_device_id = _setup_tank(hass, {"SN1": data})

    result = _tank_active_scene(entry.runtime_data)
    assert result == {"name": "Scene 7", "duration_seconds": 30}


async def test_tank_active_scene_skips_empty_scene_slots(hass):
    """An EmptyScene slot with no name must never surface as a real
    scene name -- matches select.py's own SceneSelect filtering."""
    data = _light_data("Left", None)
    data["configured_scenes"] = [
        Scene(index=0, id=0, scene_type=SceneID.EmptyScene, name="", timeout=0, light=None, pump=None),
    ]
    data["current_scene"] = None
    entry, tank_device_id = _setup_tank(hass, {"SN1": data})

    assert _tank_active_scene(entry.runtime_data) is None


async def test_resolve_tank_groups_includes_active_scene_in_every_group(hass):
    light_data = _light_data("Left", None)
    light_data["configured_scenes"] = [
        Scene(index=0, id=5, scene_type=None, name="Feeding", timeout=0, light=None, pump=None),
    ]
    light_data["current_scene"] = ActiveScene(id=5, scene_type=None, duration_seconds=120)
    pump_data = _pump_data("Pump")
    pump_data["configured_scenes"] = []
    pump_data["current_scene"] = None

    entry, tank_device_id = _setup_tank(hass, {"SN1": light_data, "SN2": pump_data})
    groups = _resolve_tank_groups(hass, tank_device_id)

    for g in groups:
        assert g.as_dict()["active_scene"] == {"name": "Feeding", "duration_seconds": 120}


# --------------------------------------------------------------------------
# handle_export_schedule_group_mob()
# --------------------------------------------------------------------------

async def test_export_schedule_group_mob_light(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left", None)})
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")
    coordinator = entry.runtime_data.coordinators["SN1"]

    points = [SchedulePoint(480, SchedulePoint.FLAG_ACTIVE, LightPrimitive({VisualID.RoyalBlue: 500}))]
    coordinator.async_get_connected_device = AsyncMock(return_value=_fake_light_device(points))

    connection = MagicMock()
    msg = {"id": 1, "type": "mobius/export_schedule_group_mob", "device_id": real_device_id}
    await handle_export_schedule_group_mob.__wrapped__(hass, connection, msg)

    connection.send_result.assert_called_once()
    mob = connection.send_result.call_args[0][1]["mob"]
    assert mob["schedules"][0]["primitiveType"] == int(PrimitiveType.VisualV1)
    connection.send_error.assert_not_called()


async def test_export_schedule_group_mob_pump(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump")})
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")
    coordinator = entry.runtime_data.coordinators["SN1"]

    points = [PumpSchedulePoint(0, SchedulePoint.FLAG_ACTIVE,
                                 PumpPrimitiveValue(PumpMode.ConstantSpeed, {PumpParam.MaxSpeed: 750}))]
    fake_device = _fake_pump_device(points)
    fake_device.get_device_info = AsyncMock(return_value={"primitive_type": "VorTechV1"})
    coordinator.async_get_connected_device = AsyncMock(return_value=fake_device)

    connection = MagicMock()
    msg = {"id": 2, "type": "mobius/export_schedule_group_mob", "device_id": real_device_id}
    await handle_export_schedule_group_mob.__wrapped__(hass, connection, msg)

    connection.send_result.assert_called_once()
    mob = connection.send_result.call_args[0][1]["mob"]
    assert mob["schedules"][0]["primitiveType"] == int(PrimitiveType.VorTechV1)
    connection.send_error.assert_not_called()


async def test_export_schedule_group_mob_unsupported_device(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": {"support": "unsupported", "name": "Doser"}})
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")
    coordinator = entry.runtime_data.coordinators["SN1"]
    coordinator.async_get_connected_device = AsyncMock(return_value=MagicMock())

    connection = MagicMock()
    msg = {"id": 3, "type": "mobius/export_schedule_group_mob", "device_id": real_device_id}
    await handle_export_schedule_group_mob.__wrapped__(hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "not_supported"
    connection.send_result.assert_not_called()


# --------------------------------------------------------------------------
# handle_parse_schedule_mob()
# --------------------------------------------------------------------------

async def test_parse_schedule_mob_light_round_trips(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left", None)})
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")

    original = [SchedulePoint(480, SchedulePoint.FLAG_ACTIVE, LightPrimitive({VisualID.RoyalBlue: 500}))]
    mob = light_schedule_to_mob(original, "Test")

    connection = MagicMock()
    msg = {"id": 4, "type": "mobius/parse_schedule_mob", "device_id": real_device_id, "mob": mob}
    await handle_parse_schedule_mob.__wrapped__(hass, connection, msg)

    connection.send_result.assert_called_once()
    points = connection.send_result.call_args[0][1]["points"]
    assert points[0]["time_minutes"] == 480
    assert points[0]["channels"]["RoyalBlue"] == 500
    connection.send_error.assert_not_called()


async def test_parse_schedule_mob_pump_translates_master_to_parent_serial(hass):
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": _pump_data("Pump A"),
        "SN2": _pump_data("Pump B"),
    })
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")
    coordinator = entry.runtime_data.coordinators["SN1"]
    registry = coordinator.registry
    await registry.join(PAN_ID, "SN1", rssi=-50)
    await registry.join(PAN_ID, "SN2", rssi=-50)
    registry.group(PAN_ID).members["SN2"].mesh_address = bytes.fromhex("fdaaaaaaaaaaaaaa0000000000000042")

    original = [PumpSchedulePoint(0, SchedulePoint.FLAG_ACTIVE,
                                   PumpPrimitiveValue(PumpMode.Sync, {
                                       PumpParam.MaxSpeed: 500, PumpParam.PhaseShift: 90,
                                       PumpParam.Master: bytes.fromhex("0000000000000042"),
                                   }))]
    mob = pump_schedule_to_mob(original, PrimitiveType.VorTechV1, "Test")

    connection = MagicMock()
    msg = {"id": 5, "type": "mobius/parse_schedule_mob", "device_id": real_device_id, "mob": mob}
    await handle_parse_schedule_mob.__wrapped__(hass, connection, msg)

    connection.send_result.assert_called_once()
    points = connection.send_result.call_args[0][1]["points"]
    assert points[0]["params"]["ParentSerial"] == "SN2"
    assert "Master" not in points[0]["params"]


async def test_parse_schedule_mob_invalid_content_returns_invalid_format(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left", None)})
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")

    connection = MagicMock()
    msg = {"id": 6, "type": "mobius/parse_schedule_mob", "device_id": real_device_id, "mob": {"not": "a real mob file"}}
    await handle_parse_schedule_mob.__wrapped__(hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "invalid_format"
    connection.send_result.assert_not_called()


async def test_parse_schedule_mob_unsupported_device(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": {"support": "unsupported", "name": "Doser"}})
    real_device_id = _member_device_id(hass, entry.entry_id, "SN1")

    connection = MagicMock()
    msg = {"id": 7, "type": "mobius/parse_schedule_mob", "device_id": real_device_id, "mob": {}}
    await handle_parse_schedule_mob.__wrapped__(hass, connection, msg)

    connection.send_error.assert_called_once()
    assert connection.send_error.call_args[0][1] == "not_supported"


# --------------------------------------------------------------------------
# Entity_id resolution for live-updating data (_sensor_entity_id,
# _scene_entity_id, and their wiring into resolve_schedule_groups)
# --------------------------------------------------------------------------

async def test_sensor_entity_id_resolves_a_registered_entity(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left", None)})
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        "sensor", DOMAIN, "SN1_intensity_royalblue", config_entry=entry,
        suggested_object_id="left_royalblue_intensity",
    )

    result = _sensor_entity_id(hass, "SN1", "intensity_royalblue")
    assert result == "sensor.left_royalblue_intensity"


async def test_sensor_entity_id_returns_none_when_not_registered(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left", None)})
    assert _sensor_entity_id(hass, "SN1", "intensity_royalblue") is None


async def test_scene_entity_id_resolves_a_registered_entity(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _light_data("Left", None)})
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        "select", DOMAIN, f"{entry.entry_id}_scene_selection", config_entry=entry,
        suggested_object_id="reef_tank_scene_selection",
    )

    assert _scene_entity_id(hass, entry.entry_id) == "select.reef_tank_scene_selection"


async def test_light_group_members_include_resolved_entity_ids(hass):
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": _light_data("Left", None, channels=["RoyalBlue", "Violet"]),
    })
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        "sensor", DOMAIN, "SN1_intensity_royalblue", config_entry=entry,
        suggested_object_id="left_royalblue_intensity",
    )
    entity_registry.async_get_or_create(
        "sensor", DOMAIN, "SN1_schedule_intensity", config_entry=entry,
        suggested_object_id="left_schedule_intensity",
    )
    # Violet's own sensor deliberately left unregistered -- confirms a
    # partially-resolved dict (one real entity_id, one None) rather
    # than an all-or-nothing failure.

    groups = _resolve_tank_groups(hass, tank_device_id)
    member = groups[0].as_dict()["members"][0]
    assert member["channel_entity_ids"] == {
        "RoyalBlue": "sensor.left_royalblue_intensity", "Violet": None,
    }
    assert member["schedule_intensity_entity_id"] == "sensor.left_schedule_intensity"


async def test_pump_group_members_include_resolved_entity_ids(hass):
    entry, tank_device_id = _setup_tank(hass, {"SN1": _pump_data("Pump")})
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        "sensor", DOMAIN, "SN1_motor_speed", config_entry=entry,
        suggested_object_id="pump_speed",
    )
    # flow_rate deliberately left unregistered (matches a real pump
    # whose gph_reliable hasn't been confirmed true yet).

    groups = _resolve_tank_groups(hass, tank_device_id)
    member = groups[0].as_dict()["members"][0]
    assert member["speed_entity_id"] == "sensor.pump_speed"
    assert member["flow_entity_id"] is None


async def test_scene_entity_id_is_the_same_across_every_group_on_the_tank(hass):
    entry, tank_device_id = _setup_tank(hass, {
        "SN1": _light_data("Left", None), "SN2": _pump_data("Pump"),
    })
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        "select", DOMAIN, f"{entry.entry_id}_scene_selection", config_entry=entry,
        suggested_object_id="reef_tank_scene_selection",
    )

    groups = _resolve_tank_groups(hass, tank_device_id)
    assert len(groups) == 2
    assert all(g.as_dict()["scene_entity_id"] == "select.reef_tank_scene_selection" for g in groups)
