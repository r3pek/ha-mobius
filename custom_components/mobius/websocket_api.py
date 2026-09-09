"""
Schedule-group resolution and WebSocket API for the schedule editor
card -- see mobius-schedule-card-implementation-plan.md's own "Layer 2"
section for the full design this implements.

Group-resolution logic (_resolve_tank_groups()) is shared by every
schedule endpoint (the resolve_schedule_groups command below, plus
read_schedule_group and the not-yet-added write service) -- one place
that decides "which devices belong together for schedule purposes,"
so every caller sees the exact same grouping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from mobius import PumpMode, light_schedule_to_dict, pump_schedule_to_dict

from . import MobiusRuntimeData
from .const import DOMAIN
from .coordinator import MobiusDeviceCoordinator

_LOGGER = logging.getLogger(__name__)

# Every PumpMode this integration is prepared to expose as a schedule
# editor option -- mirrors PUMP_MODE_PARAMS in python-mobius's own
# constants.py (every mode with at least one param defined there,
# i.e. every real mode other than the Undefined(0) placeholder).
PUMP_MODE_NAMES = [m.name for m in PumpMode if m != PumpMode.Undefined]


class ScheduleGroupError(Exception):
    """
    Raised by _resolve_tank_groups() for every validation failure this
    module needs to distinguish -- see mobius-schedule-card-
    implementation-plan.md's own "Validation rules" section (rules
    #1, #2, #4; rule #3 is handled by simply excluding an unsupported
    primitive's own device from every returned group, not raising,
    since a tank can legitimately mix schedulable and non-schedulable
    devices). `code` matches a websocket_api ERR_* constant so callers
    (the websocket command below, and the not-yet-added write service)
    can translate this directly without re-deriving which error kind
    applies.
    """
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ScheduleGroupMember:
    device_id: str
    serial: str
    name: str


@dataclass
class ScheduleGroup:
    kind: str  # "light" or "pump"
    group_mask: int | None
    members: list[ScheduleGroupMember] = field(default_factory=list)
    channels: list[str] | None = None  # light only
    modes: list[str] | None = None  # pump only

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "kind": self.kind,
            "group_mask": self.group_mask,
            "members": [
                {"device_id": m.device_id, "serial": m.serial, "name": m.name}
                for m in self.members
            ],
        }
        if self.kind == "light":
            result["channels"] = self.channels
        else:
            result["modes"] = self.modes
        return result


def _member_device_id(hass: HomeAssistant, entry_id: str, serial: str) -> str | None:
    """The real device's own device_registry ID -- identifiers are
    always exactly {(DOMAIN, serial)} for a real (non-Tank) device,
    see sensor.py's own _device_info(). None if this device hasn't
    made it into the registry yet (a coordinator can exist slightly
    before its own first successful poll populates the registry
    entry -- see coordinator.py's own _sync_device_registry_info()).

    Uses async_get_device_by_identifier() (needs entry_id, but is
    unambiguous within it), not the deprecated async_get_device() --
    same choice resolve_tank_device_id() already makes for the Tank
    device's own lookup in __init__.py, for the same reason."""
    device_registry = dr.async_get(hass)
    entry = device_registry.async_get_device_by_identifier((DOMAIN, serial), entry_id)
    return entry.id if entry is not None else None


def _member_name(serial: str, coordinator: MobiusDeviceCoordinator) -> str:
    """Mirrors sensor.py's own _device_info() naming fallback chain
    exactly -- a user's own custom name (data["name"]) first, then
    "<model> (<serial>)" once a model is known, else the serial alone
    (this coordinator's very first poll hasn't completed yet)."""
    data = coordinator.data or {}
    custom_name = data.get("name")
    if custom_name:
        return custom_name
    model = data.get("model")
    return f"{model} ({serial})" if model else serial


def _resolve_tank_groups(hass: HomeAssistant, tank_device_id: str) -> list[ScheduleGroup]:
    """
    Every distinct schedule group among a Tank's own member devices.
    Lights sharing an exact group_mask collapse into one group; a
    light with group_mask None is its own exclusive group (never
    collapsed with another None -- see 06-light-schedule.md's own
    "Schedule groups" section in python-mobius); a pump is always its
    own group of one, since pumps have no group concept at all. A
    device whose own primitive has no real/confirmed schedule support
    (support == "unsupported", or the still-experimental "pump
    (experimental)") is simply excluded from every returned group,
    rather than raising -- a tank can legitimately mix schedulable and
    non-schedulable devices (validation rule #3). Raises
    ScheduleGroupError for every other validation failure (rules #1,
    #2, #4).
    """
    device_registry = dr.async_get(hass)
    tank_entry = device_registry.async_get(tank_device_id)
    if tank_entry is None:
        raise ScheduleGroupError(
            websocket_api.const.ERR_NOT_FOUND, f"No device found for device_id {tank_device_id!r}",
        )

    # A Tank device's own identifiers are always exactly
    # (DOMAIN, "tank_...") -- see tank_device_identifier()'s own
    # docstring in __init__.py. A real device (light/pump) has
    # (DOMAIN, serial) instead -- reject that explicitly (validation
    # rule #1) rather than silently falling through to "no runtime
    # data" below, which would be a confusing error for the actual
    # mistake being made.
    is_tank = any(domain == DOMAIN and ident.startswith("tank_") for domain, ident in tank_entry.identifiers)
    if not is_tank:
        raise ScheduleGroupError(
            websocket_api.const.ERR_NOT_SUPPORTED,
            f"device_id {tank_device_id!r} is not a Tank device -- point at the Tank "
            f"device this light/pump belongs to instead (see its own 'via_device').",
        )

    if not tank_entry.config_entries:
        raise ScheduleGroupError(
            websocket_api.const.ERR_NOT_FOUND, f"Tank device {tank_device_id!r} has no config entry",
        )
    entry_id = next(iter(tank_entry.config_entries))
    entry = hass.config_entries.async_get_entry(entry_id)
    runtime: MobiusRuntimeData | None = getattr(entry, "runtime_data", None) if entry is not None else None
    if runtime is None:
        raise ScheduleGroupError(
            websocket_api.const.ERR_NOT_FOUND,
            f"Tank device {tank_device_id!r} has no runtime data yet (integration still starting up?)",
        )

    # group_mask -> members. A None group_mask must never collapse
    # multiple lights together -- each gets its own dict key via
    # id(coordinator) instead of the shared None value.
    light_groups: dict[object, list[tuple[str, MobiusDeviceCoordinator]]] = {}
    pump_singles: list[tuple[str, MobiusDeviceCoordinator]] = []

    for serial, coordinator in runtime.coordinators.items():
        data = coordinator.data or {}
        support = data.get("support")
        if support == "light":
            group_mask = data.get("group_mask")
            # A tuple sentinel, not id(coordinator) alone -- id()
            # returns a plain int, indistinguishable from a real
            # group_mask value below (isinstance(key, int) would be
            # true for BOTH, silently leaking a coordinator's own
            # id() out as a fake "group_mask" for an ungrouped light).
            key = group_mask if group_mask is not None else ("ungrouped", id(coordinator))
            light_groups.setdefault(key, []).append((serial, coordinator))
        elif support == "pump":
            pump_singles.append((serial, coordinator))
        # "pump (experimental)" and "unsupported" (and a coordinator
        # with no data yet at all) are deliberately excluded here --
        # validation rule #3.

    groups: list[ScheduleGroup] = []

    for key, members in light_groups.items():
        group_mask = key if isinstance(key, int) else None
        first_serial, first_coordinator = members[0]
        channels = (first_coordinator.data or {}).get("channels") or []
        groups.append(ScheduleGroup(
            kind="light", group_mask=group_mask, channels=channels,
            members=[
                ScheduleGroupMember(
                    device_id=_member_device_id(hass, entry_id, serial) or "",
                    serial=serial, name=_member_name(serial, coordinator),
                )
                for serial, coordinator in members
            ],
        ))

    for serial, coordinator in pump_singles:
        groups.append(ScheduleGroup(
            kind="pump", group_mask=None, modes=PUMP_MODE_NAMES,
            members=[ScheduleGroupMember(
                device_id=_member_device_id(hass, entry_id, serial) or "",
                serial=serial, name=_member_name(serial, coordinator),
            )],
        ))

    return groups


@websocket_api.websocket_command({
    vol.Required("type"): "mobius/resolve_schedule_groups",
    vol.Required("device_id"): str,
})
@websocket_api.async_response
async def handle_resolve_schedule_groups(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
) -> None:
    """
    Metadata only -- deliberately no schedule content (see
    mobius-schedule-card-implementation-plan.md's own design
    discussion for why: a separate read command avoids paying for a
    live schedule read on every resolution call, most of which are
    just "what groups exist," not "show me the points right now").
    An empty "groups" list is a valid, successful response -- a tank
    with nothing schedulable (rule #2) is not an error.
    """
    try:
        groups = _resolve_tank_groups(hass, msg["device_id"])
    except ScheduleGroupError as e:
        connection.send_error(msg["id"], e.code, e.message)
        return

    connection.send_result(msg["id"], {"groups": [g.as_dict() for g in groups]})


def _resolve_member(hass: HomeAssistant, device_id: str) -> tuple[str, MobiusRuntimeData, MobiusDeviceCoordinator]:
    """
    Given a REAL device's own device_id (a light or pump -- never a
    Tank device_id; the opposite direction from _resolve_tank_groups()'s
    own input), returns (serial, runtime, coordinator) for it. Raises
    ScheduleGroupError for every failure this needs to distinguish.
    """
    device_registry = dr.async_get(hass)
    device_entry = device_registry.async_get(device_id)
    if device_entry is None:
        raise ScheduleGroupError(websocket_api.const.ERR_NOT_FOUND, f"No device found for device_id {device_id!r}")

    is_tank = any(domain == DOMAIN and ident.startswith("tank_") for domain, ident in device_entry.identifiers)
    if is_tank:
        raise ScheduleGroupError(
            websocket_api.const.ERR_NOT_SUPPORTED,
            f"device_id {device_id!r} is a Tank device -- point at one of its own "
            f"member lights/pumps instead.",
        )

    serial = next((ident for domain, ident in device_entry.identifiers if domain == DOMAIN), None)
    if serial is None:
        raise ScheduleGroupError(
            websocket_api.const.ERR_NOT_SUPPORTED, f"device_id {device_id!r} is not a Mobius device",
        )

    if not device_entry.config_entries:
        raise ScheduleGroupError(websocket_api.const.ERR_NOT_FOUND, f"Device {device_id!r} has no config entry")
    entry_id = next(iter(device_entry.config_entries))
    entry = hass.config_entries.async_get_entry(entry_id)
    runtime: MobiusRuntimeData | None = getattr(entry, "runtime_data", None) if entry is not None else None
    if runtime is None:
        raise ScheduleGroupError(
            websocket_api.const.ERR_NOT_FOUND, f"Device {device_id!r} has no runtime data yet",
        )

    coordinator = runtime.coordinators.get(serial)
    if coordinator is None:
        raise ScheduleGroupError(websocket_api.const.ERR_NOT_FOUND, f"No coordinator found for device {device_id!r}")

    return serial, runtime, coordinator


@websocket_api.websocket_command({
    vol.Required("type"): "mobius/read_schedule_group",
    vol.Required("device_id"): str,
})
@websocket_api.async_response
async def handle_read_schedule_group(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
) -> None:
    """
    Always Schedule1 -- see mobius-schedule-card-implementation-plan.md's
    own design discussion for why Schedule2 is out of scope for now.
    Reads from the ONE named device only -- every member of a light
    group shares the same schedule, so any one member's own read
    already speaks for the whole group; no need to read from all of
    them just to answer "what does this group's schedule look like."
    """
    try:
        serial, runtime, coordinator = _resolve_member(hass, msg["device_id"])
    except ScheduleGroupError as e:
        connection.send_error(msg["id"], e.code, e.message)
        return

    support = (coordinator.data or {}).get("support")
    try:
        device = await coordinator.async_get_connected_device()
        if support == "light":
            points_dict = light_schedule_to_dict(await device.get_light_schedule(which=1))
        elif support == "pump":
            points_dict = pump_schedule_to_dict(await device.get_pump_schedule(which=1))
        else:
            connection.send_error(
                msg["id"], websocket_api.const.ERR_NOT_SUPPORTED,
                f"{serial} (support={support!r}) has no schedule support",
            )
            return
    except HomeAssistantError as e:
        connection.send_error(msg["id"], websocket_api.const.ERR_HOME_ASSISTANT_ERROR, str(e))
        return
    except Exception as e:
        connection.send_error(
            msg["id"], websocket_api.const.ERR_UNKNOWN_ERROR, f"Failed to read schedule from {serial}: {e}",
        )
        return

    connection.send_result(msg["id"], {"points": points_dict})


def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Called once from async_setup() -- see __init__.py."""
    websocket_api.async_register_command(hass, handle_resolve_schedule_groups)
    websocket_api.async_register_command(hass, handle_read_schedule_group)
