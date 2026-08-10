"""
Diagnostics support for Mobius -- lets a user download a JSON dump
(Settings > Devices & Services > this integration > this entry's own
three-dot menu > Download diagnostics) covering exactly the state
that's mattered most for real debugging of this integration's own
issues so far: which device currently holds the gateway connection,
each member's own registry-tracked health (rssi, mesh address,
consecutive gateway failures), and each device's own latest
coordinator data/error. A user attaching this to a bug report replaces
having to manually describe (or a maintainer having to manually ask
for) exactly this information turn by turn.
"""

from __future__ import annotations

import dataclasses
import ipaddress
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from .const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_MLPREFIX, CONF_DEVICES, CONF_AGE, CONF_DISCOVERED_AT
from .gateway_registry import GatewayRegistry

# BLE MAC addresses -- both the top-level CONF_ADDRESS an ad-hoc device's
# own entry may store, and "mac_address" (a real key within the raw
# get_device_info() payload itself, confirmed present even though this
# integration's own code never reads it directly) -- device-identifying,
# matching how Home Assistant's own Bluetooth-based integrations already
# treat MAC addresses in their own diagnostics. Deliberately NOT
# redacting serial numbers: they're this whole integration's actual
# identity mechanism (see python-mobius's own documentation/12-device-
# identity-and-address-stability.md), needed to make sense of anything
# else in this dump at all, and are printed on the physical device
# itself, not a secret credential.
TO_REDACT = {CONF_ADDRESS, "mac_address"}


def _json_safe(value: Any) -> Any:
    """
    Recursively converts anything in coordinator.data that plain
    json.dumps() (what Home Assistant's own diagnostics download
    actually uses) can't handle on its own -- confirmed via a real,
    similar issue other integrations have hit (core PR #141111,
    "asdict() should be called on dataclass instances" -- serialization
    isn't automatic or foolproof, it has to be handled deliberately).
    bytes -> hex string (matching how this integration's own sensors
    already display mesh addresses elsewhere -- see sensor.py's own
    MeshAddressSensor for the IPv6-formatted version specifically,
    kept simple/raw here instead since this is a raw debug dump, not a
    user-facing display), enums/anything with a plain .name -> that
    name, dataclasses -> a plain dict, anything else unrecognized ->
    str() as a last resort rather than letting the whole download
    crash on one unexpected field.
    """
    if isinstance(value, bytes):
        return value.hex()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "name") and isinstance(getattr(value, "name"), str):
        return value.name
    return str(value)


def _mesh_address_str(address: bytes | None) -> str | None:
    if address is None:
        return None
    return str(ipaddress.IPv6Address(address))


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return diagnostics for one Mobius config entry (a tank, or a
    single ad-hoc device -- the exact same shape either way, just with
    one device instead of several)."""
    registry: GatewayRegistry | None = hass.data.get(DOMAIN, {}).get("gateway_registry")
    pan_id = entry.data.get(CONF_PAN_ID)
    group = registry.group(pan_id) if registry is not None and pan_id is not None else None

    runtime = getattr(entry, "runtime_data", None)
    coordinators = runtime.coordinators if runtime is not None else {}

    devices_diag = []
    for device_record in entry.data.get(CONF_DEVICES, []):
        serial = device_record.get(CONF_SERIAL)
        coordinator = coordinators.get(serial)
        member = group.members.get(serial) if group is not None else None

        coordinator_diag = None
        if coordinator is not None:
            last_exception = coordinator.last_exception
            coordinator_diag = {
                "last_update_success": coordinator.last_update_success,
                "last_exception": repr(last_exception) if last_exception is not None else None,
                "data": _json_safe(coordinator.data),
            }

        devices_diag.append({
            "serial": serial,
            "stored_age": device_record.get(CONF_AGE),
            "is_current_gateway": (group.gateway_serial == serial) if group is not None else None,
            "registry_rssi": member.rssi if member is not None else None,
            "registry_mesh_address": _mesh_address_str(member.mesh_address) if member is not None else None,
            "coordinator": coordinator_diag,
        })

    diagnostics = {
        "entry_data": dict(entry.data),
        "pan_id_hex": f"0x{pan_id:04X}" if pan_id is not None else None,
        "mlprefix": entry.data.get(CONF_MLPREFIX),
        "discovered_at": entry.data.get(CONF_DISCOVERED_AT),
        "registry": {
            "gateway_serial": group.gateway_serial if group is not None else None,
            "consecutive_gateway_failures": group.consecutive_gateway_failures if group is not None else None,
        } if group is not None else None,
        "devices": devices_diag,
    }
    return async_redact_data(diagnostics, TO_REDACT)
