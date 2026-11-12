"""Test helper: a fake device's get_full_poll_batch() built from its
per-attribute mocks (get_device_info(), get_metadata_batch(),
get_light_poll_batch() or the pump getters), so tests describe a device
once whichever read the integration uses."""

import inspect
from unittest.mock import AsyncMock

from mobius import (
    DOSE_PRIMITIVES, FullPollResult, LIGHT_PRIMITIVES, Model, PUMP_PRIMITIVES, PrimitiveType, enum_or_none,
    primitive_type_from_name,
)


async def _maybe(device, name):
    """device.<name>() if the test mocked it as a coroutine, else None."""
    method = getattr(device, name, None)
    if not isinstance(method, AsyncMock):
        return None
    result = method()
    return await result if inspect.isawaitable(result) else result


def full_poll_from_reads(device) -> None:
    async def full_poll(primitive=None, model=None, **_kwargs):
        info = dict(await device.get_device_info())
        if primitive is None:
            primitive = primitive_type_from_name(info.get("primitive_type"))
        if model is None:
            model = enum_or_none(Model, info.get("model_raw"))
        result = FullPollResult(
            device_info=info, metadata=await device.get_metadata_batch(),
            light_poll=await _maybe(device, "get_light_poll_batch") if primitive in LIGHT_PRIMITIVES else None,
            pump_telemetry=None, primitive=primitive, model=model,
            configured_scenes=await _maybe(device, "get_configured_scenes") or [],
            current_scene=await _maybe(device, "get_current_scene"),
        )
        if primitive in PUMP_PRIMITIVES:
            result.pump_telemetry = await device.get_pump_telemetry()
            result.pump_schedule_points = await device.get_pump_schedule()
            result.battery_backup = await _maybe(device, "get_battery_backup_info")
            result.boosted_battery = await _maybe(device, "get_boosted_battery_info")
            result.operation_state = await _maybe(device, "get_operation_state")
            if primitive == PrimitiveType.VectraV1:
                result.vectra_info = await _maybe(device, "get_vectra_info")
        if primitive in DOSE_PRIMITIVES:
            result.dose = await _maybe(device, "get_dose_info")
        return result

    device.get_full_poll_batch = AsyncMock(side_effect=full_poll)
    if not isinstance(getattr(device, "get_supported_attributes", None), AsyncMock):
        # No supported attributes listed: nothing optional is offered.
        device.get_supported_attributes = AsyncMock(return_value=[])
