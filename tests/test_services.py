"""
Tests for async_handle_write_schedule_group() and its own
_live_group_members()/_min_group_capacity() helpers -- see
services.py's own module docstring, and mobius-schedule-card-
implementation-plan.md's own "Validation rules" section for the
numbered rules referenced throughout (rule #5, the live
re-verification requirement, is this file's main focus).

Same setup approach as test_websocket_api.py: builds just enough
registry state directly (a Tank device, a real device entry per
serial, coordinators with .data set directly) rather than running a
full async_setup_entry().
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
from custom_components.mobius.services import (
    async_handle_write_schedule_group, _live_group_members, _min_group_capacity,
    _support_from_primitive_type, _untranslate_parent_serial_to_master,
)
from custom_components.mobius.websocket_api import _member_device_id
from mobius import SupportedAttribute, C2Attribute

PAN_ID = 0x3D0F


def _make_registry(hass) -> GatewayRegistry:
    return GatewayRegistry(hass, asyncio.Semaphore(2))


def _register_real_device(hass, entry, serial: str) -> str:
    device_registry = dr.async_get(hass)
    return device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, serial)},
    ).id


def _make_coordinator(hass, entry, registry, serial: str, data: dict, live_info: dict) -> MobiusDeviceCoordinator:
    coordinator = MobiusDeviceCoordinator(hass, entry, registry, serial, PAN_ID)
    coordinator.data = data
    fake_device = MagicMock()
    fake_device.get_device_info = AsyncMock(return_value=live_info)
    fake_device.get_supported_attributes = AsyncMock(return_value=[])
    fake_device.set_light_schedule = AsyncMock()
    fake_device.set_pump_schedule = AsyncMock()
    coordinator.async_get_connected_device = AsyncMock(return_value=fake_device)
    coordinator._test_fake_device = fake_device  # for direct assertions
    return coordinator


def _setup_tank(hass, serial_data_live: dict[str, tuple[dict, dict]]) -> tuple[MockConfigEntry, dict[str, str]]:
    """serial_data_live: serial -> (cached_data, live_get_device_info_result).
    Returns (entry, {serial: real_device_id})."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, unique_id=f"test-{PAN_ID:04x}")
    entry.add_to_hass(hass)

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={tank_device_identifier(None, PAN_ID)},
    )

    registry = _make_registry(hass)
    coordinators = {}
    device_ids = {}
    for serial, (data, live_info) in serial_data_live.items():
        device_ids[serial] = _register_real_device(hass, entry, serial)
        coordinators[serial] = _make_coordinator(hass, entry, registry, serial, data, live_info)

    entry.runtime_data = MobiusRuntimeData(coordinators=coordinators)
    return entry, device_ids


def _light_cached(name: str) -> dict:
    return {"support": "light", "name": name, "model": "RadionXR15wG6Pro"}


def _light_live(group_mask, primitive="VisualV1") -> dict:
    return {"primitive_type": primitive, "group_mask": group_mask}


def _pump_cached(name: str) -> dict:
    return {"support": "pump", "name": name, "model": "VorTechMP40wG3QD"}


def _pump_live(primitive="VorTechV1") -> dict:
    return {"primitive_type": primitive, "group_mask": None}


def _call(device_id: str, points: list) -> MagicMock:
    call = MagicMock()
    call.data = {"device_id": device_id, "points": points}
    return call


# --------------------------------------------------------------------------
# _support_from_primitive_type()
# --------------------------------------------------------------------------

def test_support_from_primitive_type_light():
    assert _support_from_primitive_type("VisualV1") == "light"


def test_support_from_primitive_type_pump():
    assert _support_from_primitive_type("VorTechV1") == "pump"


def test_support_from_primitive_type_experimental():
    assert _support_from_primitive_type("CoffeeV1") == "pump (experimental)"


def test_support_from_primitive_type_none_or_unknown():
    assert _support_from_primitive_type(None) == "unsupported"
    assert _support_from_primitive_type("NotReal") == "unsupported"


# --------------------------------------------------------------------------
# _live_group_members() -- the core of rule #5
# --------------------------------------------------------------------------

async def test_pump_target_is_always_alone(hass):
    entry, device_ids = _setup_tank(hass, {"SN1": (_pump_cached("Pump"), _pump_live())})
    coordinator = entry.runtime_data.coordinators["SN1"]

    support, members = await _live_group_members(hass, "SN1", coordinator)

    assert support == "pump"
    assert [m[0] for m in members] == ["SN1"]


async def test_light_with_none_group_mask_is_alone_even_with_other_lights_present(hass):
    entry, device_ids = _setup_tank(hass, {
        "SN1": (_light_cached("Left"), _light_live(None)),
        "SN2": (_light_cached("Right"), _light_live(None)),
    })
    coordinator = entry.runtime_data.coordinators["SN1"]

    support, members = await _live_group_members(hass, "SN1", coordinator)

    assert support == "light"
    assert [m[0] for m in members] == ["SN1"]


async def test_lights_with_matching_live_group_mask_are_all_included(hass):
    entry, device_ids = _setup_tank(hass, {
        "SN1": (_light_cached("Left"), _light_live(12345)),
        "SN2": (_light_cached("Right"), _light_live(12345)),
    })
    coordinator = entry.runtime_data.coordinators["SN1"]

    support, members = await _live_group_members(hass, "SN1", coordinator)

    assert sorted(m[0] for m in members) == ["SN1", "SN2"]


async def test_the_key_rule_a_stale_cached_match_is_excluded_if_live_group_mask_differs(hass):
    """Rule #5, the whole point of this function: SN2 LOOKED like it
    was in the same group as of the last poll (if we'd trusted cached
    data), but its own FRESH read shows it's since been moved to a
    different group -- it must be excluded from this write."""
    entry, device_ids = _setup_tank(hass, {
        "SN1": (_light_cached("Left"), _light_live(12345)),
        "SN2": (_light_cached("Right"), _light_live(99999)),  # live: different group now
    })
    coordinator = entry.runtime_data.coordinators["SN1"]

    support, members = await _live_group_members(hass, "SN1", coordinator)

    assert [m[0] for m in members] == ["SN1"]


async def test_a_pump_is_never_included_in_a_light_group_even_with_a_matching_mask(hass):
    entry, device_ids = _setup_tank(hass, {
        "SN1": (_light_cached("Left"), _light_live(12345)),
        "SN2": (_pump_cached("Pump"), _pump_live()),
    })
    coordinator = entry.runtime_data.coordinators["SN1"]

    support, members = await _live_group_members(hass, "SN1", coordinator)

    assert [m[0] for m in members] == ["SN1"]


async def test_unreachable_candidate_is_excluded_not_fatal(hass):
    entry, device_ids = _setup_tank(hass, {
        "SN1": (_light_cached("Left"), _light_live(12345)),
        "SN2": (_light_cached("Right"), _light_live(12345)),
    })
    coordinator = entry.runtime_data.coordinators["SN1"]
    entry.runtime_data.coordinators["SN2"].async_get_connected_device = AsyncMock(
        side_effect=HomeAssistantError("unreachable"),
    )

    support, members = await _live_group_members(hass, "SN1", coordinator)

    assert [m[0] for m in members] == ["SN1"]


async def test_experimental_pump_target_raises(hass):
    entry, device_ids = _setup_tank(hass, {"SN1": (_pump_cached("Pump"), _pump_live(primitive="CoffeeV1"))})
    coordinator = entry.runtime_data.coordinators["SN1"]

    with pytest.raises(HomeAssistantError, match="no confirmed schedule support"):
        await _live_group_members(hass, "SN1", coordinator)


async def test_unsupported_target_raises(hass):
    entry, device_ids = _setup_tank(hass, {"SN1": ({"support": "unsupported"}, {"primitive_type": "DoseV1"})})
    coordinator = entry.runtime_data.coordinators["SN1"]

    with pytest.raises(HomeAssistantError, match="no confirmed schedule support"):
        await _live_group_members(hass, "SN1", coordinator)


# --------------------------------------------------------------------------
# _min_group_capacity()
# --------------------------------------------------------------------------

async def test_min_group_capacity_returns_smallest(hass):
    entry, device_ids = _setup_tank(hass, {
        "SN1": (_light_cached("Left"), _light_live(1)),
        "SN2": (_light_cached("Right"), _light_live(1)),
    })
    c1 = entry.runtime_data.coordinators["SN1"]
    c2 = entry.runtime_data.coordinators["SN2"]
    c1._test_fake_device.get_supported_attributes = AsyncMock(
        return_value=[SupportedAttribute(attr_id=int(C2Attribute.Schedule1), indexes=list(range(25)))],
    )
    c2._test_fake_device.get_supported_attributes = AsyncMock(
        return_value=[SupportedAttribute(attr_id=int(C2Attribute.Schedule1), indexes=list(range(10)))],
    )
    members = [("SN1", c1, c1._test_fake_device), ("SN2", c2, c2._test_fake_device)]

    assert await _min_group_capacity(members, which=1) == 10


# --------------------------------------------------------------------------
# async_handle_write_schedule_group() -- the full service
# --------------------------------------------------------------------------

def _light_points_dict():
    return [{"time_minutes": 480, "flags": 1, "channels": {"RoyalBlue": 500}}]


def _pump_points_dict():
    return [{"time_minutes": 0, "flags": 1, "mode": "ConstantSpeed", "params": {"MaxSpeed": 750}}]


async def test_write_pump_writes_to_the_one_device(hass):
    entry, device_ids = _setup_tank(hass, {"SN1": (_pump_cached("Pump"), _pump_live())})
    coordinator = entry.runtime_data.coordinators["SN1"]

    await async_handle_write_schedule_group(hass, _call(device_ids["SN1"], _pump_points_dict()))

    coordinator._test_fake_device.set_pump_schedule.assert_called_once()
    coordinator._test_fake_device.set_light_schedule.assert_not_called()


async def test_write_light_group_writes_to_every_member(hass):
    entry, device_ids = _setup_tank(hass, {
        "SN1": (_light_cached("Left"), _light_live(1)),
        "SN2": (_light_cached("Right"), _light_live(1)),
    })
    c1 = entry.runtime_data.coordinators["SN1"]
    c2 = entry.runtime_data.coordinators["SN2"]

    await async_handle_write_schedule_group(hass, _call(device_ids["SN1"], _light_points_dict()))

    c1._test_fake_device.set_light_schedule.assert_called_once()
    c2._test_fake_device.set_light_schedule.assert_called_once()


async def test_write_never_reaches_a_light_whose_live_group_no_longer_matches(hass):
    entry, device_ids = _setup_tank(hass, {
        "SN1": (_light_cached("Left"), _light_live(1)),
        "SN2": (_light_cached("Right"), _light_live(999)),  # different live group
    })
    c1 = entry.runtime_data.coordinators["SN1"]
    c2 = entry.runtime_data.coordinators["SN2"]

    await async_handle_write_schedule_group(hass, _call(device_ids["SN1"], _light_points_dict()))

    c1._test_fake_device.set_light_schedule.assert_called_once()
    c2._test_fake_device.set_light_schedule.assert_not_called()


async def test_write_rejects_unsupported_target(hass):
    entry, device_ids = _setup_tank(hass, {"SN1": ({"support": "unsupported"}, {"primitive_type": "DoseV1"})})

    with pytest.raises(HomeAssistantError, match="no confirmed schedule support"):
        await async_handle_write_schedule_group(hass, _call(device_ids["SN1"], _light_points_dict()))


async def test_write_capacity_check_aborts_before_any_write(hass):
    entry, device_ids = _setup_tank(hass, {
        "SN1": (_light_cached("Left"), _light_live(1)),
        "SN2": (_light_cached("Right"), _light_live(1)),
    })
    c1 = entry.runtime_data.coordinators["SN1"]
    c2 = entry.runtime_data.coordinators["SN2"]
    c1._test_fake_device.get_supported_attributes = AsyncMock(
        return_value=[SupportedAttribute(attr_id=int(C2Attribute.Schedule1), indexes=list(range(25)))],
    )
    c2._test_fake_device.get_supported_attributes = AsyncMock(
        return_value=[SupportedAttribute(attr_id=int(C2Attribute.Schedule1), indexes=[0])],  # capacity 1
    )
    two_points = [_light_points_dict()[0], _light_points_dict()[0]]  # 2 points, exceeds capacity 1

    with pytest.raises(HomeAssistantError, match="smallest capacity"):
        await async_handle_write_schedule_group(hass, _call(device_ids["SN1"], two_points))

    c1._test_fake_device.set_light_schedule.assert_not_called()
    c2._test_fake_device.set_light_schedule.assert_not_called()


async def test_write_partial_failure_raises_with_details_but_does_not_stop_other_writes(hass):
    entry, device_ids = _setup_tank(hass, {
        "SN1": (_light_cached("Left"), _light_live(1)),
        "SN2": (_light_cached("Right"), _light_live(1)),
    })
    c1 = entry.runtime_data.coordinators["SN1"]
    c2 = entry.runtime_data.coordinators["SN2"]
    c2._test_fake_device.set_light_schedule = AsyncMock(side_effect=IOError("device rejected write"))

    with pytest.raises(HomeAssistantError, match="1/2 device"):
        await async_handle_write_schedule_group(hass, _call(device_ids["SN1"], _light_points_dict()))

    c1._test_fake_device.set_light_schedule.assert_called_once()


# --------------------------------------------------------------------------
# ParentSerial -> Master reverse translation (Sync/EcoSmartBack)
# --------------------------------------------------------------------------

async def test_untranslate_parent_serial_to_master_converts_correctly(hass):
    entry, device_ids = _setup_tank(hass, {
        "SN1": (_pump_cached("Pump A"), _pump_live()),
        "SN2": (_pump_cached("Pump B"), _pump_live()),
    })
    coordinator = entry.runtime_data.coordinators["SN1"]
    registry = coordinator.registry
    await registry.join(PAN_ID, "SN1", rssi=-50)
    await registry.join(PAN_ID, "SN2", rssi=-50)
    registry.group(PAN_ID).members["SN2"].mesh_address = bytes.fromhex("fdaaaaaaaaaaaaaa0000000000000042")

    points_dict = [{"time_minutes": 0, "flags": 1, "mode": "Sync",
                    "params": {"MaxSpeed": 500, "PhaseShift": 90, "ParentSerial": "SN2"}}]
    _untranslate_parent_serial_to_master(points_dict, coordinator)

    assert points_dict[0]["params"]["Master"] == "0000000000000042"
    assert "ParentSerial" not in points_dict[0]["params"]


async def test_untranslate_raises_when_parent_serial_unresolvable(hass):
    entry, device_ids = _setup_tank(hass, {"SN1": (_pump_cached("Pump A"), _pump_live())})
    coordinator = entry.runtime_data.coordinators["SN1"]
    registry = coordinator.registry
    await registry.join(PAN_ID, "SN1", rssi=-50)

    points_dict = [{"time_minutes": 0, "flags": 1, "mode": "Sync",
                    "params": {"MaxSpeed": 500, "PhaseShift": 90, "ParentSerial": "UNKNOWN"}}]

    with pytest.raises(HomeAssistantError, match="Can't resolve parent pump"):
        _untranslate_parent_serial_to_master(points_dict, coordinator)


async def test_write_sync_pump_schedule_end_to_end(hass):
    """The full flow: a card sends ParentSerial (a serial number), and
    the device actually receives a real Master mesh address byte
    string, not the serial itself."""
    entry, device_ids = _setup_tank(hass, {
        "SN1": (_pump_cached("Pump A"), _pump_live()),
        "SN2": (_pump_cached("Pump B"), _pump_live()),
    })
    c1 = entry.runtime_data.coordinators["SN1"]
    registry = c1.registry
    await registry.join(PAN_ID, "SN1", rssi=-50)
    await registry.join(PAN_ID, "SN2", rssi=-50)
    registry.group(PAN_ID).members["SN2"].mesh_address = bytes.fromhex("fdaaaaaaaaaaaaaa0000000000000042")

    points = [{"time_minutes": 0, "flags": 1, "mode": "Sync",
               "params": {"MaxSpeed": 500, "PhaseShift": 90, "ParentSerial": "SN2"}}]
    await async_handle_write_schedule_group(hass, _call(device_ids["SN1"], points))

    c1._test_fake_device.set_pump_schedule.assert_called_once()
    written_points = c1._test_fake_device.set_pump_schedule.call_args[0][0]
    from mobius import PumpParam
    assert written_points[0].pump.params[PumpParam.Master] == bytes.fromhex("0000000000000042")
