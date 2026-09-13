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
from homeassistant.helpers import device_registry as dr, entity_registry as er

from mobius import (
    PumpMode, PumpParam, SceneID, PrimitiveType, light_schedule_to_dict, pump_schedule_to_dict,
    light_schedule_to_mob, mob_to_light_schedule, pump_schedule_to_mob, mob_to_pump_schedule,
    supported_pump_modes, PUMP_MODE_PARAMS,
)

from . import MobiusRuntimeData
from .const import DOMAIN
from .coordinator import MobiusDeviceCoordinator

_LOGGER = logging.getLogger(__name__)

# Every real PumpMode -- mirrors PUMP_MODE_PARAMS in python-mobius's own
# constants.py (every mode with at least one param defined there, i.e.
# every real mode other than the Undefined(0) placeholder). Not used
# directly for a group's own `modes` field any more -- see
# supported_pump_modes() below, which filters this down per pump. Kept
# as a reference constant (validated in test_websocket_api.py) that
# the full set itself hasn't drifted from what PumpMode actually defines.
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
    # This member's own entity_ids for live-updating values (current
    # channel intensity per channel + schedule intensity for a light;
    # speed/flow for a pump) -- resolved via the entity registry, not
    # guessed/reconstructed, since entity_id is a person-renameable,
    # display-facing identifier unrelated to unique_id in general. A
    # card reads these directly via hass.states for anything that
    # changes live, rather than re-calling resolve_schedule_groups on
    # every update -- this call is for structural metadata that rarely
    # changes, not a live-data feed itself. None entries throughout
    # (e.g. flow_entity_id when gph_reliable hasn't been confirmed
    # yet) mean that entity doesn't exist right now, not an error.
    channel_entity_ids: dict[str, str | None] | None = None  # light only
    schedule_intensity_entity_id: str | None = None  # light only
    speed_entity_id: str | None = None  # pump only
    flow_entity_id: str | None = None  # pump only
    mode_entity_id: str | None = None  # pump only -- CurrentPumpModeSensor


@dataclass
class ScheduleGroup:
    kind: str  # "light" or "pump"
    group_mask: int | None
    members: list[ScheduleGroupMember] = field(default_factory=list)
    channels: list[str] | None = None  # light only
    modes: list[str] | None = None  # pump only
    mode_params: dict[str, list[str]] | None = None  # pump only -- which params each of `modes` needs
    active_scene: dict[str, Any] | None = None  # {"name": str, "duration_seconds": int} or None
    schedule_intensity: float | None = None  # light only -- 0.0-1.0, see Schedule1Intensity
    scene_entity_id: str | None = None  # tank-wide -- same value for every group on the same tank

    def as_dict(self) -> dict[str, Any]:
        def member_dict(m: ScheduleGroupMember) -> dict[str, Any]:
            d: dict[str, Any] = {"device_id": m.device_id, "serial": m.serial, "name": m.name}
            if self.kind == "light":
                d["channel_entity_ids"] = m.channel_entity_ids
                d["schedule_intensity_entity_id"] = m.schedule_intensity_entity_id
            else:
                d["speed_entity_id"] = m.speed_entity_id
                d["flow_entity_id"] = m.flow_entity_id
                d["mode_entity_id"] = m.mode_entity_id
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
        else:
            result["modes"] = self.modes
            result["mode_params"] = self.mode_params
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


def _sensor_entity_id(hass: HomeAssistant, serial: str, key: str) -> str | None:
    """
    The real entity_id for a sensor.py entity, given the same
    (serial, key) pair its own unique_id is built from there
    (f"{serial}_{key}" -- see MobiusEntity's own __init__). A card
    reading live values (current channel intensity, schedule
    intensity, pump speed/flow) needs the real entity_id to watch via
    hass.states, not a guessed/reconstructed one -- entity_id is a
    person-renameable, display-facing identifier, unrelated to
    unique_id in general, so this goes through the registry rather
    than assuming any naming relationship between the two. None if
    this entity doesn't exist (e.g. FlowRateSensor is only created
    once gph_reliable is confirmed true -- see sensor.py's own
    async_setup_entry()).
    """
    entity_registry = er.async_get(hass)
    return entity_registry.async_get_entity_id("sensor", DOMAIN, f"{serial}_{key}")


def _scene_entity_id(hass: HomeAssistant, entry_id: str) -> str | None:
    """The real entity_id for this tank's own select.scene_selection
    entity (see select.py's own SceneSelectionSelect, unique_id
    f"{entry_id}_scene_selection") -- tank-wide, not per-serial, unlike
    _sensor_entity_id() above. None if scenes aren't supported at all
    on this tank (the entity is only created when at least one device
    actually reports configured_scenes -- see select.py's own
    async_setup_entry())."""
    entity_registry = er.async_get(hass)
    return entity_registry.async_get_entity_id("select", DOMAIN, f"{entry_id}_scene_selection")


def _member_name(serial: str, coordinator: MobiusDeviceCoordinator) -> str:
    """The device's own name if it's been set (via the app), otherwise
    the bare serial -- explicit product decision for the schedule
    editor card specifically (unlike sensor.py's own _device_info(),
    which falls back to "<model> (<serial>)" for its own, different
    purpose: a Home Assistant device registry entry always needs SOME
    display name, whereas here the serial alone reads cleanly as an
    identifier on its own)."""
    data = coordinator.data or {}
    custom_name = data.get("name")
    if custom_name:
        return custom_name
    return serial


def _tank_active_scene(runtime: MobiusRuntimeData) -> dict[str, Any] | None:
    """
    Same name-resolution logic as select.py's own scene-selection
    entity (SceneSelect._scene_name_to_id()/current_option()) -- a
    scene has no single canonical name of its own on the wire, only
    an id, so the name has to come from whichever device's own
    ConfiguredScenes slot happens to define that id. Checks every
    coordinator's own current_scene (None if that device is running
    its normal schedule, not a scene -- see get_current_scene()'s own
    docstring in python-mobius) and returns the first active one
    found, tank-wide, since a scene activation writes to every device
    on the tank in one go (this integration's own SceneSelect entity
    already assumes -- and this function inherits the same assumption
    -- that they'd all agree; a device that's fallen out of sync would
    only be caught by noticing its own state elsewhere, not here).
    None if no device on this tank currently has a scene active.
    """
    id_to_name: dict[int, str] = {}
    for coordinator in runtime.coordinators.values():
        for scene in (coordinator.data or {}).get("configured_scenes") or []:
            if scene.scene_type == SceneID.EmptyScene and not scene.name:
                continue
            id_to_name.setdefault(scene.id, scene.name)

    for coordinator in runtime.coordinators.values():
        active = (coordinator.data or {}).get("current_scene")
        if active is not None:
            return {
                "name": id_to_name.get(active.id, f"Scene {active.id}"),
                "duration_seconds": active.duration_seconds,
            }
    return None


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
    active_scene = _tank_active_scene(runtime)
    scene_entity_id = _scene_entity_id(hass, entry_id)

    for key, members in light_groups.items():
        # Sorted by serial -- members[0] (used both here for channels
        # and by the card for "which device's own live sensors to
        # chart") must be a genuinely stable, deterministic choice,
        # not whatever order runtime.coordinators happened to be
        # populated in (config-entry device-list order, not sorted or
        # otherwise guaranteed). The schedule itself is identical
        # across every member by construction (the same write goes to
        # all of them), but each device's own LIVE reported state can
        # legitimately diverge -- LED aging, a device that's fallen
        # out of sync, a manual override on just one of them -- and a
        # card charting "whichever one happened to be first" would
        # silently flip which device it's showing across unrelated
        # code changes or even just a HA restart, with no way for
        # anyone to know that's what changed if the numbers moved.
        members = sorted(members, key=lambda m: m[0])
        group_mask = key if isinstance(key, int) else None
        first_serial, first_coordinator = members[0]
        channels = (first_coordinator.data or {}).get("channels") or []
        schedule_intensity = (first_coordinator.data or {}).get("schedule_intensity")
        groups.append(ScheduleGroup(
            kind="light", group_mask=group_mask, channels=channels, active_scene=active_scene,
            schedule_intensity=schedule_intensity, scene_entity_id=scene_entity_id,
            members=[
                ScheduleGroupMember(
                    device_id=_member_device_id(hass, entry_id, serial) or "",
                    serial=serial, name=_member_name(serial, coordinator),
                    channel_entity_ids={
                        ch: _sensor_entity_id(hass, serial, f"intensity_{ch.lower()}")
                        for ch in ((coordinator.data or {}).get("channels") or [])
                    },
                    schedule_intensity_entity_id=_sensor_entity_id(hass, serial, "schedule_intensity"),
                )
                for serial, coordinator in members
            ],
        ))

    for serial, coordinator in pump_singles:
        primitive_name = (coordinator.data or {}).get("primitive_type")
        primitive = PrimitiveType[primitive_name] if primitive_name else None
        closed_loop = (coordinator.data or {}).get("closed_loop")
        pump_modes = supported_pump_modes(primitive, closed_loop) if primitive else []
        groups.append(ScheduleGroup(
            kind="pump", group_mask=None,
            modes=[m.name for m in pump_modes],
            mode_params={m.name: _mode_param_names(m) for m in pump_modes},
            active_scene=active_scene,
            scene_entity_id=scene_entity_id,
            members=[ScheduleGroupMember(
                device_id=_member_device_id(hass, entry_id, serial) or "",
                serial=serial, name=_member_name(serial, coordinator),
                speed_entity_id=_sensor_entity_id(hass, serial, "motor_speed"),
                flow_entity_id=_sensor_entity_id(hass, serial, "flow_rate"),
                mode_entity_id=_sensor_entity_id(hass, serial, "current_pump_mode"),
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


def _serial_for_master_hex(master_hex: str, coordinator: MobiusDeviceCoordinator) -> str | None:
    """
    Sync/EcoSmartBack's own "Master" param is the last 8 bytes of the
    parent pump's own mesh address (see python-mobius's own
    07-pump-schedule.md), hex-encoded by pump_schedule_to_dict() --
    meaningless to a person editing a schedule. Translates it back to
    whichever known device in this same tank it refers to, via
    GatewayRegistry's own shared serial_for_mesh_suffix() (also used by
    coordinator.py for the same resolution, so both sides of this
    translation stay in sync with each other automatically).
    """
    group = coordinator.registry.group(coordinator.pan_id)
    if group is None:
        return None
    return group.serial_for_mesh_suffix(bytes.fromhex(master_hex))


def _master_hex_for_serial(serial: str, coordinator: MobiusDeviceCoordinator) -> str | None:
    """The reverse of _serial_for_master_hex() -- the hex string
    write_schedule_group() should encode as "Master" for the pump
    named by `serial`. None if that device's own mesh address isn't
    known yet (it's never been reached, or reached only through a
    relay path that hasn't resolved its own mesh address -- see
    MemberState's own docstring in gateway_registry.py)."""
    group = coordinator.registry.group(coordinator.pan_id)
    if group is None:
        return None
    member = group.members.get(serial)
    if member is None or member.mesh_address is None:
        return None
    return member.mesh_address[-8:].hex()


def _mode_param_names(mode: PumpMode) -> list[str]:
    """The param names a card should expect for this mode's own point
    data -- Master translated to ParentSerial, matching the same
    translation _translate_master_to_parent_serial() applies to a
    real point's own params before it ever reaches the card. Without
    this, mode_params would say "Master" while every actual point
    the card ever sees says "ParentSerial" for that same field."""
    return ["ParentSerial" if p == PumpParam.Master else p.name for p in PUMP_MODE_PARAMS.get(mode, [])]


def _translate_master_to_parent_serial(points_dict: list[dict], coordinator: MobiusDeviceCoordinator) -> None:
    """
    Mutates `points_dict` in place: for every Sync/EcoSmartBack point,
    replaces its own "Master" param (a raw hex string) with
    "ParentSerial" (a serial number, or None if it can't currently be
    resolved to a known device) -- the card should never need to know
    about raw mesh addresses. The reverse (_untranslate_parent_serial_
    to_master(), in services.py) undoes this before a write.
    """
    for point in points_dict:
        params = point.get("params", {})
        if "Master" in params:
            master_hex = params.pop("Master")
            params["ParentSerial"] = _serial_for_master_hex(master_hex, coordinator)


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
            _translate_master_to_parent_serial(points_dict, coordinator)
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


@websocket_api.websocket_command({
    vol.Required("type"): "mobius/export_schedule_group_mob",
    vol.Required("device_id"): str,
})
@websocket_api.async_response
async def handle_export_schedule_group_mob(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
) -> None:
    """
    Builds the actual .mob (Template) file content server-side via
    python-mobius's own light_schedule_to_mob()/pump_schedule_to_mob()
    -- the card never touches primitiveData encoding itself, only the
    finished JSON, matching how read_schedule_group/write_schedule_group
    already keep every wire-format detail server-side. Always Schedule1,
    same as read_schedule_group.
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
            points = await device.get_light_schedule(which=1)
            mob = light_schedule_to_mob(points, serial)
        elif support == "pump":
            info = await device.get_device_info()
            primitive_type = PrimitiveType[info["primitive_type"]]
            points = await device.get_pump_schedule(which=1)
            mob = pump_schedule_to_mob(points, primitive_type, serial)
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
            msg["id"], websocket_api.const.ERR_UNKNOWN_ERROR, f"Failed to export schedule from {serial}: {e}",
        )
        return

    connection.send_result(msg["id"], {"mob": mob})


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
    The read half of import: given a .mob file's own already-parsed
    JSON (the card reads the local file itself, this never touches a
    filesystem), decodes it server-side via python-mobius's own
    mob_to_light_schedule()/mob_to_pump_schedule() and returns points
    in the exact same dict shape read_schedule_group already uses --
    the card treats a freshly-read schedule and a freshly-imported one
    identically from here on, populating the same editor either way.
    Doesn't write anything -- the card still calls write_schedule_group
    itself once the person reviews and saves, same as any other edit.
    `device_id` is only used to determine light vs pump (which
    mob_to_*_schedule() function applies); a mismatched primitive type
    in the file itself surfaces as a normal parse failure below.
    """
    try:
        serial, runtime, coordinator = _resolve_member(hass, msg["device_id"])
    except ScheduleGroupError as e:
        connection.send_error(msg["id"], e.code, e.message)
        return

    support = (coordinator.data or {}).get("support")
    try:
        if support == "light":
            points_dict = light_schedule_to_dict(mob_to_light_schedule(msg["mob"]))
        elif support == "pump":
            _, points = mob_to_pump_schedule(msg["mob"])
            points_dict = pump_schedule_to_dict(points)
            _translate_master_to_parent_serial(points_dict, coordinator)
        else:
            connection.send_error(
                msg["id"], websocket_api.const.ERR_NOT_SUPPORTED,
                f"{serial} (support={support!r}) has no schedule support",
            )
            return
    except Exception as e:
        connection.send_error(
            msg["id"], websocket_api.const.ERR_INVALID_FORMAT, f"Couldn't parse this .mob file: {e}",
        )
        return

    connection.send_result(msg["id"], {"points": points_dict})


def async_register_websocket_commands(hass: HomeAssistant) -> None:
    """Called once from async_setup() -- see __init__.py."""
    websocket_api.async_register_command(hass, handle_resolve_schedule_groups)
    websocket_api.async_register_command(hass, handle_read_schedule_group)
    websocket_api.async_register_command(hass, handle_export_schedule_group_mob)
    websocket_api.async_register_command(hass, handle_parse_schedule_mob)
