"""
Services used by the schedule editor card (the read side is in
websocket_api.py): write a schedule, or the schedule intensity, to a
device and every light in its schedule group.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from mobius import (
    min_schedule_capacity, light_schedule_from_dict, pump_schedule_from_dict, primitive_type_from_name, support_tier,
)

from .const import DOMAIN
from .coordinator import MobiusDeviceCoordinator
from .websocket_api import _resolve_member, ScheduleGroupError, _master_hex_for_serial

_LOGGER = logging.getLogger(__name__)

Member = tuple[str, MobiusDeviceCoordinator, Any]

SERVICE_WRITE_SCHEDULE_GROUP = "write_schedule_group"

WRITE_SCHEDULE_GROUP_SCHEMA = vol.Schema({
    vol.Required("device_id"): cv.string,
    vol.Required("points"): [dict],
})

SERVICE_SET_SCHEDULE_INTENSITY = "set_schedule_intensity"

SET_SCHEDULE_INTENSITY_SCHEMA = vol.Schema({
    vol.Required("device_id"): cv.string,
    vol.Required("intensity"): vol.All(vol.Coerce(float), vol.Range(min=0, max=100)),
})


def _support_from_primitive_type(primitive_type_name: str | None) -> str:
    """Support tier from a get_device_info() "primitive_type"."""
    return support_tier(primitive_type_from_name(primitive_type_name))


async def _live_group_members(
    hass: HomeAssistant, target_serial: str, target_coordinator: MobiusDeviceCoordinator,
) -> tuple[str, list[Member]]:
    """
    The target's support tier and the devices a write to it must reach, as
    [(serial, coordinator, connected_device)], starting with the target.

    Membership is checked with fresh get_device_info() reads right before
    writing, since group_mask may have changed since the last poll (the
    read-only resolve_schedule_groups command uses the cached data). A pump
    is always alone; a light includes every light of the same entry with
    exactly the same group_mask (a None group_mask never matches another
    light, see python-mobius 06-light-schedule.md, "Schedule groups").
    Lights that can't be re-checked are left out.

    Raises HomeAssistantError if the target can't be reached or has no
    schedule support.
    """
    target_device = await target_coordinator.async_get_connected_device()
    target_info = await target_device.get_device_info()
    support = _support_from_primitive_type(target_info.get("primitive_type"))

    if support in ("pump (experimental)", "unsupported"):
        raise HomeAssistantError(
            f"{target_serial} (support={support!r}) has no confirmed schedule support"
        )

    members: list[Member] = [(target_serial, target_coordinator, target_device)]
    target_group_mask = target_info.get("group_mask")
    if support == "pump" or target_group_mask is None:
        return support, members

    runtime = target_coordinator.config_entry.runtime_data
    for serial, coordinator in runtime.coordinators.items():
        # The cached support tier is enough here: it never changes.
        if serial == target_serial or (coordinator.data or {}).get("support") != "light":
            continue
        try:
            candidate_device = await coordinator.async_get_connected_device()
            candidate_info = await candidate_device.get_device_info()
        except Exception as e:
            _LOGGER.warning(
                "%s: could not re-verify group membership for %s (%s) -- "
                "excluding it from this write rather than risking a write "
                "based on stale membership",
                target_serial, serial, e,
            )
            continue
        if candidate_info.get("group_mask") == target_group_mask:
            members.append((serial, coordinator, candidate_device))

    return support, members


def _untranslate_parent_serial_to_master(points_dict: list[dict], coordinator: MobiusDeviceCoordinator) -> None:
    """
    The reverse of websocket_api.py's _translate_master_to_parent_serial():
    replaces each point's "ParentSerial" with the "Master" hex string
    pump_schedule_from_dict() expects, in place. Raises HomeAssistantError
    if a parent can't be resolved to a mesh address, rather than syncing the
    pump to the wrong device.
    """
    for point in points_dict:
        params = point.get("params", {})
        if "ParentSerial" in params:
            parent_serial = params.pop("ParentSerial")
            master_hex = _master_hex_for_serial(parent_serial, coordinator) if parent_serial else None
            if master_hex is None:
                raise HomeAssistantError(
                    f"Can't resolve parent pump {parent_serial!r}'s own mesh address -- "
                    f"it may not be part of this tank, or hasn't been reached yet."
                )
            params["Master"] = master_hex


async def _min_group_capacity(members: list[Member], which: int) -> int | None:
    """min_schedule_capacity() of `members`."""
    return await min_schedule_capacity([device for _serial, _coordinator, device in members], which)


async def _resolve_group(hass: HomeAssistant, device_id: str) -> tuple[str, MobiusDeviceCoordinator, str, list[Member]]:
    """(target_serial, target_coordinator, support, members) for a device
    registry id."""
    try:
        target_serial, _runtime, target_coordinator = _resolve_member(hass, device_id)
    except ScheduleGroupError as e:
        raise HomeAssistantError(e.message) from e
    support, members = await _live_group_members(hass, target_serial, target_coordinator)
    return target_serial, target_coordinator, support, members


async def _write_to_members(members: list[Member], write: Callable[[Any], Awaitable[None]], what: str) -> None:
    """Runs `write` on every member; raises HomeAssistantError listing the
    failures, if any."""
    errors: list[str] = []
    for serial, _coordinator, device in members:
        try:
            await write(device)
        except Exception as e:
            errors.append(f"{serial}: {e}")
    if errors:
        raise HomeAssistantError(
            f"{what} {len(members) - len(errors)}/{len(members)} device(s) "
            f"successfully; failed: {'; '.join(errors)}"
        )


async def async_handle_write_schedule_group(hass: HomeAssistant, call: ServiceCall) -> None:
    """Writes Schedule1 to the target and, for a light, every other light
    in its schedule group (never to only part of a group). Schedule2 isn't
    supported yet."""
    points_dict = call.data["points"]
    _target_serial, target_coordinator, support, members = await _resolve_group(hass, call.data["device_id"])

    if support == "light":
        points = light_schedule_from_dict(points_dict)
    else:
        _untranslate_parent_serial_to_master(points_dict, target_coordinator)
        points = pump_schedule_from_dict(points_dict)

    min_capacity = await _min_group_capacity(members, which=1)
    if min_capacity is not None and len(points) > min_capacity:
        raise HomeAssistantError(
            f"{len(points)} point(s) given, but the smallest capacity across "
            f"{len(members)} device(s) in this group is {min_capacity} -- refusing "
            f"to write, since some group member(s) would reject this schedule entirely."
        )

    if support == "light":
        await _write_to_members(members, lambda d: d.set_light_schedule(points, which=1), "Wrote to")
    else:
        await _write_to_members(members, lambda d: d.set_pump_schedule(points, which=1), "Wrote to")


async def async_handle_set_schedule_intensity(hass: HomeAssistant, call: ServiceCall) -> None:
    """
    Writes Schedule1Intensity (the schedule-level dimmer, see python-mobius
    06-light-schedule.md) to the target light and every light in its
    schedule group. `intensity` is 0-100 percent. Rejected for pumps.
    """
    target_serial, _coordinator, support, members = await _resolve_group(hass, call.data["device_id"])
    if support != "light":
        raise HomeAssistantError(
            f"{target_serial} (support={support!r}) has no schedule-level intensity control -- "
            f"this is a light-only concept."
        )
    fraction = call.data["intensity"] / 100.0
    await _write_to_members(members, lambda d: d.set_schedule_intensity(fraction, which=1), "Set intensity on")


def async_register_services(hass: HomeAssistant) -> None:
    """Registers the services (called from async_setup())."""
    hass.services.async_register(
        DOMAIN, SERVICE_WRITE_SCHEDULE_GROUP, lambda call: async_handle_write_schedule_group(hass, call),
        schema=WRITE_SCHEDULE_GROUP_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_SCHEDULE_INTENSITY, lambda call: async_handle_set_schedule_intensity(hass, call),
        schema=SET_SCHEDULE_INTENSITY_SCHEMA,
    )
