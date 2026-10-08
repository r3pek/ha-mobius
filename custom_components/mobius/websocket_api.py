"""
Schedule-group resolution and WebSocket commands for the schedule editor
card. _resolve_tank_groups() decides which devices share a schedule and is
used by every schedule command, so they all see the same groups.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from mobius import (
    PumpMode, PumpParam, PrimitiveType, light_schedule_to_dict, pump_schedule_to_dict,
    light_schedule_to_mob, mob_to_light_schedule, pump_schedule_to_mob, mob_to_pump_schedule,
    supported_pump_modes, PUMP_MODE_PARAMS, primitive_type_from_name,
    Model, enum_or_none, pump_param_range,
)

from . import MobiusRuntimeData
from .const import DOMAIN
from .coordinator import MobiusDeviceCoordinator, used_scenes

_LOGGER = logging.getLogger(__name__)

# Every PumpMode except Undefined (checked against PUMP_MODE_PARAMS by the
# tests). The modes offered for a pump come from supported_pump_modes().
PUMP_MODE_NAMES = [m.name for m in PumpMode if m != PumpMode.Undefined]

_ERR = websocket_api.const


class ScheduleGroupError(Exception):
    """A request the schedule commands reject; `code` is a websocket_api
    ERR_* constant. Devices without schedule support are left out of the
    groups instead (a tank may mix both)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ScheduleGroupMember:
    device_id: str
    serial: str
    name: str
    # entity_ids the card follows through hass.states for live values
    # (looked up in the entity registry, since entity_ids can be renamed).
    # None when the entity doesn't exist.
    channel_entity_ids: dict[str, str | None] | None = None  # light only
    schedule_intensity_entity_id: str | None = None  # light only
    # light only -- LunarPhasesEnabledSwitch, read and toggled by the card.
    lunar_switch_entity_id: str | None = None
    speed_entity_id: str | None = None  # pump only
    flow_entity_id: str | None = None  # pump only
    mode_entity_id: str | None = None  # pump only -- CurrentPumpModeSensor
    # pump only -- whether a negative MaxSpeed/MinSpeed means reverse
    # rotation (AlpacaV1, see get_pump_reverse()); None before the first
    # successful poll.
    supports_reverse: bool | None = None


@dataclass
class ScheduleGroup:
    kind: str  # "light" or "pump"
    group_mask: int | None
    members: list[ScheduleGroupMember] = field(default_factory=list)
    channels: list[str] | None = None  # light only
    modes: list[str] | None = None  # pump only
    mode_params: dict[str, list[str]] | None = None  # pump only -- which params each of `modes` needs
    # pump only -- {mode: {param: {"min", "max", "step"}}}: the range the app
    # allows for each parameter (see python-mobius pump_param_range()).
    param_ranges: dict[str, dict[str, dict[str, int]]] | None = None
    active_scene: dict[str, Any] | None = None  # {"name": str, "duration_seconds": int} or None
    schedule_intensity: float | None = None  # light only -- 0.0-1.0, see Schedule1Intensity
    # light only -- True when the group's lights don't all have the same
    # channels (each light is still written only the channels it supports).
    mixed_channels: bool = False
    scene_entity_id: str | None = None  # tank-wide -- same value for every group on the same tank

    def as_dict(self) -> dict[str, Any]:
        def member_dict(m: ScheduleGroupMember) -> dict[str, Any]:
            d: dict[str, Any] = {"device_id": m.device_id, "serial": m.serial, "name": m.name}
            if self.kind == "light":
                d["channel_entity_ids"] = m.channel_entity_ids
                d["schedule_intensity_entity_id"] = m.schedule_intensity_entity_id
                d["lunar_switch_entity_id"] = m.lunar_switch_entity_id
            else:
                d["speed_entity_id"] = m.speed_entity_id
                d["flow_entity_id"] = m.flow_entity_id
                d["mode_entity_id"] = m.mode_entity_id
                d["supports_reverse"] = m.supports_reverse
            return d

        result: dict[str, Any] = {
            "kind": self.kind,
            "group_mask": self.group_mask,
            "active_scene": self.active_scene,
            "scene_entity_id": self.scene_entity_id,
            "members": [member_dict(m) for m in self.members],
        }
        if self.kind == "light":
            result["channels"] = self.channels
            result["schedule_intensity"] = self.schedule_intensity
            result["mixed_channels"] = self.mixed_channels
        else:
            result["modes"] = self.modes
            result["mode_params"] = self.mode_params
            result["param_ranges"] = self.param_ranges
        return result


def _member_device_id(hass: HomeAssistant, entry_id: str, serial: str) -> str | None:
    """Device registry id of the device with `serial` (identifier
    (DOMAIN, serial)), or None if it isn't registered yet."""
    entry = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, serial), entry_id)
    return entry.id if entry is not None else None


def _sensor_entity_id(hass: HomeAssistant, serial: str, key: str, domain: str = "sensor") -> str | None:
    """entity_id of this integration's entity with unique_id
    f"{serial}_{key}" in `domain`, or None if it doesn't exist."""
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{serial}_{key}")


def _scene_entity_id(hass: HomeAssistant, entry_id: str) -> str | None:
    """entity_id of the tank's scene select (SceneSelectionSelect), or
    None."""
    return er.async_get(hass).async_get_entity_id("select", DOMAIN, f"{entry_id}_scene_selection")


def _member_name(serial: str, coordinator: MobiusDeviceCoordinator) -> str:
    """The device's own name, else its serial."""
    return (coordinator.data or {}).get("name") or serial


def _tank_active_scene(runtime: MobiusRuntimeData) -> dict[str, Any] | None:
    """
    {"name", "duration_seconds", "ends_at"} of the first active scene found on the
    tank (a scene is activated tank-wide), or None. The name comes from
    whichever device has that scene id configured.
    """
    id_to_name: dict[int, str] = {}
    for coordinator in runtime.coordinators.values():
        for scene in used_scenes(coordinator.data or {}):
            id_to_name.setdefault(scene.id, scene.name)

    for coordinator in runtime.coordinators.values():
        data = coordinator.data or {}
        active = data.get("current_scene")
        if active is not None:
            return {
                "name": id_to_name.get(active.id, f"Scene {active.id}"),
                "duration_seconds": active.duration_seconds,
                "ends_at": data.get("current_scene_ends_at"),
            }
    return None


def _is_tank_device(device_entry: dr.DeviceEntry) -> bool:
    """Tank devices have a (DOMAIN, "tank_...") identifier (see
    tank_device_identifier())."""
    return any(domain == DOMAIN and ident.startswith("tank_") for domain, ident in device_entry.identifiers)


def _device_runtime(hass: HomeAssistant, device_entry: dr.DeviceEntry, label: str) -> tuple[str, MobiusRuntimeData]:
    """(entry_id, runtime data) of the config entry a device belongs to."""
    entry_id = device_entry.config_entry_id
    if not entry_id:
        raise ScheduleGroupError(_ERR.ERR_NOT_FOUND, f"{label} has no config entry")
    entry = hass.config_entries.async_get_entry(entry_id)
    runtime: MobiusRuntimeData | None = getattr(entry, "runtime_data", None) if entry is not None else None
    if runtime is None:
        raise ScheduleGroupError(
            _ERR.ERR_NOT_FOUND, f"{label} has no runtime data yet (integration still starting up?)",
        )
    return entry_id, runtime


def _union_channels(channel_lists: list[list[str]]) -> list[str]:
    """The union of the given per-device channel lists, keeping the order in
    which channels first appear (the first device's channels, then any others
    later devices add)."""
    seen: list[str] = []
    for channels in channel_lists:
        for ch in channels:
            if ch not in seen:
                seen.append(ch)
    return seen


def _resolve_tank_groups(hass: HomeAssistant, tank_device_id: str) -> list[ScheduleGroup]:
    """
    The schedule groups of a tank device's members. Lights with the same
    group_mask form one group; a light with group_mask None is its own group
    (see python-mobius 06-light-schedule.md, "Schedule groups"); every pump
    is its own group. Devices without schedule support ("pump
    (experimental)", "unsupported", or no data yet) are left out. Raises
    ScheduleGroupError for an unknown device, a device that isn't a tank,
    or a tank that isn't set up.
    """
    tank_entry = dr.async_get(hass).async_get(tank_device_id)
    if tank_entry is None:
        raise ScheduleGroupError(_ERR.ERR_NOT_FOUND, f"No device found for device_id {tank_device_id!r}")
    if not _is_tank_device(tank_entry):
        raise ScheduleGroupError(
            _ERR.ERR_NOT_SUPPORTED,
            f"device_id {tank_device_id!r} is not a Tank device -- point at the Tank "
            f"device this light/pump belongs to instead (see its own 'via_device').",
        )
    entry_id, runtime = _device_runtime(hass, tank_entry, f"Tank device {tank_device_id!r}")

    # Lights by group_mask. Ungrouped lights get a unique tuple key, which
    # can't be mistaken for a real (int) group_mask.
    light_groups: dict[object, list[tuple[str, MobiusDeviceCoordinator]]] = {}
    pump_singles: list[tuple[str, MobiusDeviceCoordinator]] = []
    for serial, coordinator in runtime.coordinators.items():
        data = coordinator.data or {}
        support = data.get("support")
        if support == "light":
            group_mask = data.get("group_mask")
            key = group_mask if group_mask is not None else ("ungrouped", id(coordinator))
            light_groups.setdefault(key, []).append((serial, coordinator))
        elif support == "pump":
            pump_singles.append((serial, coordinator))

    groups: list[ScheduleGroup] = []
    active_scene = _tank_active_scene(runtime)
    scene_entity_id = _scene_entity_id(hass, entry_id)

    for key, members in light_groups.items():
        # Sorted by serial so the first member (whose live values the card
        # follows) is always the same device.
        members = sorted(members, key=lambda m: m[0])
        first_data = members[0][1].data or {}
        member_channel_sets = [set((c.data or {}).get("channels") or []) for _s, c in members]
        # The card shows every channel any light in the group has (the union);
        # each light is written only its own channels when saving. Ordered by
        # the first member's list, then any extra channels other members add.
        union_channels = _union_channels([(c.data or {}).get("channels") or [] for _s, c in members])
        mixed_channels = any(s != member_channel_sets[0] for s in member_channel_sets[1:])
        groups.append(ScheduleGroup(
            kind="light", group_mask=key if isinstance(key, int) else None,
            channels=union_channels, mixed_channels=mixed_channels, active_scene=active_scene,
            schedule_intensity=first_data.get("schedule_intensity"), scene_entity_id=scene_entity_id,
            members=[
                ScheduleGroupMember(
                    device_id=_member_device_id(hass, entry_id, serial) or "",
                    serial=serial, name=_member_name(serial, coordinator),
                    channel_entity_ids={
                        ch: _sensor_entity_id(hass, serial, f"intensity_{ch.lower()}")
                        for ch in ((coordinator.data or {}).get("channels") or [])
                    },
                    schedule_intensity_entity_id=_sensor_entity_id(hass, serial, "schedule_intensity"),
                    lunar_switch_entity_id=_sensor_entity_id(hass, serial, "lunar_phases_enabled", domain="switch"),
                )
                for serial, coordinator in members
            ],
        ))

    for serial, coordinator in pump_singles:
        data = coordinator.data or {}
        primitive = primitive_type_from_name(data.get("primitive_type"))
        pump_modes = supported_pump_modes(primitive, data.get("closed_loop")) if primitive else []
        model = enum_or_none(Model, data.get("model_raw"))
        groups.append(ScheduleGroup(
            kind="pump", group_mask=None,
            modes=[m.name for m in pump_modes],
            mode_params={m.name: _mode_param_names(m) for m in pump_modes},
            param_ranges={m.name: _mode_param_ranges(m, primitive, model) for m in pump_modes},
            active_scene=active_scene,
            scene_entity_id=scene_entity_id,
            members=[ScheduleGroupMember(
                device_id=_member_device_id(hass, entry_id, serial) or "",
                serial=serial, name=_member_name(serial, coordinator),
                speed_entity_id=_sensor_entity_id(hass, serial, "motor_speed"),
                flow_entity_id=_sensor_entity_id(hass, serial, "flow_rate"),
                mode_entity_id=_sensor_entity_id(hass, serial, "current_pump_mode"),
                supports_reverse=(primitive == PrimitiveType.AlpacaV1) if primitive else None,
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
    """The schedule groups of a tank (metadata only; the points are read
    with read_schedule_group). No groups is a valid result."""
    try:
        groups = _resolve_tank_groups(hass, msg["device_id"])
    except ScheduleGroupError as e:
        connection.send_error(msg["id"], e.code, e.message)
        return
    connection.send_result(msg["id"], {"groups": [g.as_dict() for g in groups]})


def _serial_for_master_hex(master_hex: str, coordinator: MobiusDeviceCoordinator) -> str | None:
    """The serial of the tank device a Sync/EcoSmartBack "Master" value
    (hex of the last 8 bytes of the parent pump's mesh address, see
    python-mobius 07-pump-schedule.md) refers to, or None."""
    group = coordinator.registry.group(coordinator.pan_id)
    if group is None:
        return None
    return group.serial_for_mesh_suffix(bytes.fromhex(master_hex))


def _master_hex_for_serial(serial: str, coordinator: MobiusDeviceCoordinator) -> str | None:
    """The reverse of _serial_for_master_hex(): the "Master" hex for the
    pump `serial`, or None if its mesh address isn't known."""
    group = coordinator.registry.group(coordinator.pan_id)
    if group is None:
        return None
    member = group.members.get(serial)
    if member is None or member.mesh_address is None:
        return None
    return member.mesh_address[-8:].hex()


def _mode_param_names(mode: PumpMode) -> list[str]:
    """Parameter names of `mode` as the card sees them (Master is
    replaced by ParentSerial, see _translate_master_to_parent_serial())."""
    return ["ParentSerial" if p == PumpParam.Master else p.name for p in PUMP_MODE_PARAMS.get(mode, [])]


def _mode_param_ranges(mode: PumpMode, primitive: PrimitiveType | None, model: Model | None) -> dict[str, dict[str, int]]:
    """{param name: {"min", "max", "step"}} for the parameters of `mode`
    that have a range."""
    ranges = {}
    for param in PUMP_MODE_PARAMS.get(mode, []):
        allowed = pump_param_range(param, primitive, mode, model)
        if allowed is not None:
            ranges[param.name] = {"min": allowed.minimum, "max": allowed.maximum, "step": allowed.step}
    return ranges


def _translate_master_to_parent_serial(points_dict: list[dict], coordinator: MobiusDeviceCoordinator) -> None:
    """Replaces each point's "Master" parameter with "ParentSerial" (the
    serial it refers to, or None), in place, so the card never deals with
    mesh addresses. services.py's _untranslate_parent_serial_to_master()
    is the reverse."""
    for point in points_dict:
        params = point.get("params", {})
        if "Master" in params:
            master_hex = params.pop("Master")
            params["ParentSerial"] = _serial_for_master_hex(master_hex, coordinator)


def _resolve_member(hass: HomeAssistant, device_id: str) -> tuple[str, MobiusRuntimeData, MobiusDeviceCoordinator]:
    """(serial, runtime, coordinator) of a light or pump device_id (not a
    tank). Raises ScheduleGroupError otherwise."""
    device_entry = dr.async_get(hass).async_get(device_id)
    if device_entry is None:
        raise ScheduleGroupError(_ERR.ERR_NOT_FOUND, f"No device found for device_id {device_id!r}")
    if _is_tank_device(device_entry):
        raise ScheduleGroupError(
            _ERR.ERR_NOT_SUPPORTED,
            f"device_id {device_id!r} is a Tank device -- point at one of its own "
            f"member lights/pumps instead.",
        )
    serial = next((ident for domain, ident in device_entry.identifiers if domain == DOMAIN), None)
    if serial is None:
        raise ScheduleGroupError(_ERR.ERR_NOT_SUPPORTED, f"device_id {device_id!r} is not a Mobius device")

    _entry_id, runtime = _device_runtime(hass, device_entry, f"Device {device_id!r}")
    coordinator = runtime.coordinators.get(serial)
    if coordinator is None:
        raise ScheduleGroupError(_ERR.ERR_NOT_FOUND, f"No coordinator found for device {device_id!r}")
    return serial, runtime, coordinator


async def _member_schedule_command(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
    handler: Callable[[str, MobiusDeviceCoordinator, bool], Awaitable[dict]],
    error_code: str, error_prefix: str, device_errors: bool = True,
) -> None:
    """
    Runs `handler(serial, coordinator, is_light)` for the light or pump in
    msg["device_id"] and sends its result. Devices without schedule support
    are rejected. A handler failure is sent as `error_code` with
    `error_prefix` (which may contain "{serial}"); with device_errors, a HomeAssistantError (e.g. no
    gateway) is sent as-is.
    """
    try:
        serial, _runtime, coordinator = _resolve_member(hass, msg["device_id"])
    except ScheduleGroupError as e:
        connection.send_error(msg["id"], e.code, e.message)
        return

    support = (coordinator.data or {}).get("support")
    if support not in ("light", "pump"):
        connection.send_error(
            msg["id"], _ERR.ERR_NOT_SUPPORTED, f"{serial} (support={support!r}) has no schedule support",
        )
        return
    error_prefix = error_prefix.format(serial=serial)
    try:
        result = await handler(serial, coordinator, support == "light")
    except HomeAssistantError as e:
        if not device_errors:
            connection.send_error(msg["id"], error_code, f"{error_prefix}: {e}")
        else:
            connection.send_error(msg["id"], _ERR.ERR_HOME_ASSISTANT_ERROR, str(e))
        return
    except Exception as e:
        connection.send_error(msg["id"], error_code, f"{error_prefix}: {e}")
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): "mobius/read_schedule_group",
    vol.Required("device_id"): str,
})
@websocket_api.async_response
async def handle_read_schedule_group(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
) -> None:
    """Schedule1 of the named device (every member of a light group has
    the same schedule, so one read is enough). Schedule2 isn't supported
    yet."""
    async def read(serial, coordinator, is_light):
        device = await coordinator.async_get_connected_device()
        if is_light:
            return {"points": light_schedule_to_dict(await device.get_light_schedule(which=1))}
        points_dict = pump_schedule_to_dict(await device.get_pump_schedule(which=1))
        _translate_master_to_parent_serial(points_dict, coordinator)
        return {"points": points_dict}

    await _member_schedule_command(
        hass, connection, msg, read, _ERR.ERR_UNKNOWN_ERROR, "Failed to read schedule from {serial}",
    )


@websocket_api.websocket_command({
    vol.Required("type"): "mobius/export_schedule_group_mob",
    vol.Required("device_id"): str,
})
@websocket_api.async_response
async def handle_export_schedule_group_mob(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
) -> None:
    """Schedule1 of the named device as .mob Template content, built with
    python-mobius (the card only receives the finished JSON)."""
    async def export(serial, coordinator, is_light):
        device = await coordinator.async_get_connected_device()
        if is_light:
            return {"mob": light_schedule_to_mob(await device.get_light_schedule(which=1), serial)}
        info = await device.get_device_info()
        primitive_type = PrimitiveType[info["primitive_type"]]
        points = await device.get_pump_schedule(which=1)
        return {"mob": pump_schedule_to_mob(points, primitive_type, serial)}

    await _member_schedule_command(
        hass, connection, msg, export, _ERR.ERR_UNKNOWN_ERROR,
        "Failed to export schedule from {serial}",
    )


@websocket_api.websocket_command({
    vol.Required("type"): "mobius/parse_schedule_mob",
    vol.Required("device_id"): str,
    vol.Required("mob"): dict,
})
@websocket_api.async_response
async def handle_parse_schedule_mob(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
) -> None:
    """
    Decodes a .mob file (already parsed JSON, read by the card) into the
    same points format as read_schedule_group. Writes nothing; the card
    saves with write_schedule_group. The device only selects light or pump
    decoding.
    """
    async def parse(serial, coordinator, is_light):
        if is_light:
            return {"points": light_schedule_to_dict(mob_to_light_schedule(msg["mob"]))}
        _, points = mob_to_pump_schedule(msg["mob"])
        points_dict = pump_schedule_to_dict(points)
        _translate_master_to_parent_serial(points_dict, coordinator)
        return {"points": points_dict}

    await _member_schedule_command(
        hass, connection, msg, parse, _ERR.ERR_INVALID_FORMAT, "Couldn't parse this .mob file",
        device_errors=False,
    )


def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Registers the commands (called from async_setup())."""
    for command in (
        handle_resolve_schedule_groups, handle_read_schedule_group,
        handle_export_schedule_group_mob, handle_parse_schedule_mob,
    ):
        websocket_api.async_register_command(hass, command)
