"""
Services for the schedule editor card -- see websocket_api.py's own
module docstring for the read side, and mobius-schedule-card-
implementation-plan.md's own "Layer 2" section for the full design.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from mobius import (
    PrimitiveType, LIGHT_PRIMITIVES, PUMP_PRIMITIVES_VERIFIED, PUMP_PRIMITIVES_EXPERIMENTAL,
    C2Attribute, light_schedule_from_dict, pump_schedule_from_dict,
)

from .coordinator import MobiusDeviceCoordinator
from .websocket_api import _resolve_member, ScheduleGroupError

_LOGGER = logging.getLogger(__name__)

SERVICE_WRITE_SCHEDULE_GROUP = "write_schedule_group"

WRITE_SCHEDULE_GROUP_SCHEMA = vol.Schema({
    vol.Required("device_id"): cv.string,
    vol.Required("points"): [dict],
})


def _support_from_primitive_type(primitive_type_name: str | None) -> str:
    """
    Same classification coordinator.py's own _fetch_all() already
    applies to a poll's own primitive_type, but computed from a FRESH
    get_device_info() read instead of (possibly stale) coordinator.data
    -- see _live_group_members()'s own docstring for why this write
    path can't just trust the cached value the way
    resolve_schedule_groups (the read/metadata side) reasonably does.
    """
    if not primitive_type_name:
        return "unsupported"
    try:
        primitive = PrimitiveType[primitive_type_name]
    except KeyError:
        return "unsupported"
    if primitive in LIGHT_PRIMITIVES:
        return "light"
    if primitive in PUMP_PRIMITIVES_VERIFIED:
        return "pump"
    if primitive in PUMP_PRIMITIVES_EXPERIMENTAL:
        return "pump (experimental)"
    return "unsupported"


async def _live_group_members(
    hass: HomeAssistant, target_serial: str, target_coordinator: MobiusDeviceCoordinator,
) -> tuple[str, list[tuple[str, MobiusDeviceCoordinator, object]]]:
    """
    Validation rule #5, the hard requirement: re-verify group
    membership immediately before writing, not just at load time (the
    resolve_schedule_groups command, by contrast, is fine using
    coordinator.data -- see that command's own docstring for why).
    Every candidate light gets its own FRESH get_device_info() call
    here, not the cached poll data, since group_mask could have
    changed since this tank's own last poll (up to POLL_INTERVAL
    seconds stale, or more if that device's own poll has been failing).

    Returns (support, members) where members is
    [(serial, coordinator, connected_device)] for the whole group the
    target currently belongs to -- always exactly one entry for a
    pump (no group concept at all), one-or-more for a light. The
    target's own connected_device is included and reused, not
    reconnected a second time.

    Raises HomeAssistantError if the target itself isn't currently
    reachable, or its own primitive has no real schedule support
    (validation rule #3, enforced here too since a stale/incorrect
    assumption about the target's own type would be worse in a write
    path than in the read-only resolution command).
    """
    target_device = await target_coordinator.async_get_connected_device()
    target_info = await target_device.get_device_info()
    support = _support_from_primitive_type(target_info.get("primitive_type"))

    if support in ("pump (experimental)", "unsupported"):
        raise HomeAssistantError(
            f"{target_serial} (support={support!r}) has no confirmed schedule support"
        )

    if support == "pump":
        return support, [(target_serial, target_coordinator, target_device)]

    # support == "light": find every OTHER light in the same tank
    # (runtime.coordinators is already scoped to one config entry --
    # see MobiusRuntimeData's own docstring -- so no separate pan_id
    # filter is needed here) whose OWN fresh group_mask matches the
    # target's fresh group_mask exactly. None group_mask is the
    # target's own exclusive group -- never matched against another
    # light's own None (see python-mobius's own 06-light-schedule.md
    # "Schedule groups" section).
    target_group_mask = target_info.get("group_mask")
    members: list[tuple[str, MobiusDeviceCoordinator, object]] = [
        (target_serial, target_coordinator, target_device),
    ]

    if target_group_mask is not None:
        entry = target_coordinator.config_entry
        runtime = entry.runtime_data
        for serial, coordinator in runtime.coordinators.items():
            if serial == target_serial:
                continue
            # Cached data is fine for THIS check only -- primitive_type
            # (and therefore whether a device is a light at all) is
            # permanent for a device's whole lifetime, unlike
            # group_mask, which is what actually needs the fresh
            # re-check below.
            if (coordinator.data or {}).get("support") != "light":
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


async def _min_group_capacity(members: list[tuple[str, MobiusDeviceCoordinator, object]], which: int) -> int | None:
    """
    Same philosophy as python-mobius's own CLI _min_group_capacity()
    (mobius.cli) -- the smallest Schedule{which} capacity across every
    live-verified member, so a too-large schedule can be rejected
    before writing to ANY of them, rather than partially succeeding
    across the group. None if no member reports a capacity at all.
    """
    schedule_attr = C2Attribute.Schedule1 if which == 1 else C2Attribute.Schedule2
    capacities = []
    for serial, coordinator, device in members:
        try:
            supported = await device.get_supported_attributes()
        except Exception:
            continue
        entry = next((s for s in supported if s.attr_id == int(schedule_attr)), None)
        if entry is not None:
            capacities.append(len(entry.indexes))
    return min(capacities) if capacities else None


async def async_handle_write_schedule_group(hass: HomeAssistant, call: ServiceCall) -> None:
    """
    Always Schedule1 -- see websocket_api.py's own
    handle_read_schedule_group() docstring for why Schedule2 is out of
    scope for now. Writes to every live-verified group member (all of
    them for a light group, the one device for a pump) -- never a
    single device out of a light group, matching this integration's
    own always-whole-group write behavior (the same rule
    python-mobius's own CLI already enforces for --load-schedule-file/
    --load-mob-file).
    """
    device_id = call.data["device_id"]
    points_dict = call.data["points"]

    try:
        target_serial, runtime, target_coordinator = _resolve_member(hass, device_id)
    except ScheduleGroupError as e:
        raise HomeAssistantError(e.message) from e

    support, members = await _live_group_members(hass, target_serial, target_coordinator)

    if support == "light":
        points = light_schedule_from_dict(points_dict)
    else:
        points = pump_schedule_from_dict(points_dict)

    min_capacity = await _min_group_capacity(members, which=1)
    if min_capacity is not None and len(points) > min_capacity:
        raise HomeAssistantError(
            f"{len(points)} point(s) given, but the smallest capacity across "
            f"{len(members)} device(s) in this group is {min_capacity} -- refusing "
            f"to write, since some group member(s) would reject this schedule "
            f"entirely (see python-mobius's own set_light_schedule()/"
            f"set_pump_schedule() docstrings)."
        )

    errors: list[str] = []
    for serial, coordinator, device in members:
        try:
            if support == "light":
                await device.set_light_schedule(points, which=1)
            else:
                await device.set_pump_schedule(points, which=1)
        except Exception as e:
            errors.append(f"{serial}: {e}")

    if errors:
        raise HomeAssistantError(
            f"Wrote to {len(members) - len(errors)}/{len(members)} device(s) "
            f"successfully; failed: {'; '.join(errors)}"
        )


def async_register_services(hass: HomeAssistant) -> None:
    """Called once from async_setup() -- see __init__.py."""
    hass.services.async_register(
        "mobius", SERVICE_WRITE_SCHEDULE_GROUP, lambda call: async_handle_write_schedule_group(hass, call),
        schema=WRITE_SCHEDULE_GROUP_SCHEMA,
    )
