"""
Diagnostics download for a Mobius config entry: the gateway state, each
device's registry state (RSSI, mesh address), coordinator data and errors,
batch-read state, and whether Home Assistant's Bluetooth stack currently
sees each device.
"""

from __future__ import annotations

import dataclasses
import time
from datetime import datetime
from typing import Any, Optional

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from mobius import C2Attribute, ConfigurationCache, enum_or_none, format_mesh_address

from .const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_MLPREFIX, CONF_DEVICES
from .coordinator import _find_in_bluetooth_cache
from .gateway_registry import GatewayRegistry

# BLE MAC addresses (the stored CONF_ADDRESS and get_device_info()'s
# "mac_address") are redacted. Serial numbers are kept: devices are
# identified by them and they are printed on the device.
TO_REDACT = {CONF_ADDRESS, "mac_address"}


def _json_safe(value: Any) -> Any:
    """Makes coordinator data JSON serializable: bytes as hex, dataclasses
    as dicts, enums by name, anything else unknown as str()."""
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


def _isoformat(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _configuration_cache_snapshot(cache: ConfigurationCache) -> dict[str, Any]:
    """Which data the device's ConfigurationCache holds, with the checksum of each."""
    return {
        "entries": {
            enum_or_none(C2Attribute, attr_id).name if enum_or_none(C2Attribute, attr_id) else str(attr_id):
                f"0x{checksum:04X}"
            for attr_id, (checksum, _blocks) in sorted(cache.entries.items())
        },
        "supported_attributes_crc": (
            f"0x{cache.supported_attributes_crc:04X}" if cache.supported_attributes_crc is not None else None
        ),
    }


def _bluetooth_cache_snapshot(hass: HomeAssistant, serial: str, now: float) -> dict[str, Any]:
    """
    Whether Home Assistant's Bluetooth cache currently has an advertisement
    for `serial`, independent of this integration's own state. Matched by
    serial, so an advertisement without manufacturer data (these devices
    rotate several) can't be matched: "not found" means not identifiable
    right now.
    """
    info = _find_in_bluetooth_cache(hass, serial)
    if info is None:
        return {"found_by_serial": False}
    return {
        "found_by_serial": True,
        "address": info.address,
        "rssi": info.rssi,
        "connectable": info.connectable,
        "seconds_since_last_advertisement": round(now - info.time, 1),
    }


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Diagnostics of one config entry (tank or single device)."""
    registry: GatewayRegistry | None = hass.data.get(DOMAIN, {}).get("gateway_registry")
    pan_id = entry.data.get(CONF_PAN_ID)
    group = registry.group(pan_id) if registry is not None and pan_id is not None else None

    runtime = getattr(entry, "runtime_data", None)
    coordinators = runtime.coordinators if runtime is not None else {}

    now = time.time()
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
            "is_current_gateway": (group.gateway_serial == serial) if group is not None else None,
            "registry_rssi": member.rssi if member is not None else None,
            "registry_mesh_address": format_mesh_address(member.mesh_address) if member is not None else None,
            "registry_radio_type": member.radio_type if member is not None else None,
            "consecutive_relay_failures": member.consecutive_relay_failures if member is not None else None,
            "restart_recovering_until": _isoformat(member.recovering_until) if member is not None else None,
            "bluetooth_cache": _bluetooth_cache_snapshot(hass, serial, now),
            "batch_disabled": coordinator._batch_disabled if coordinator is not None else None,
            "consecutive_batch_failures": coordinator._consecutive_batch_failures if coordinator is not None else None,
            "configuration_cache": _configuration_cache_snapshot(coordinator.configuration_cache) if coordinator is not None else None,
            "supported_attributes": coordinator.supported_attribute_names if coordinator is not None else None,
            "coordinator": coordinator_diag,
        })

    # Both counts show whether Bluetooth itself is working (visible
    # connectable devices, and connectable scanners registered at all).
    connectable_count = sum(
        1 for _ in bluetooth.async_discovered_service_info(hass, connectable=True)
    )
    connectable_scanner_count = bluetooth.async_scanner_count(hass, connectable=True)

    diagnostics = {
        "entry_data": dict(entry.data),
        "pan_id_hex": f"0x{pan_id:04X}" if pan_id is not None else None,
        "mlprefix": entry.data.get(CONF_MLPREFIX),
        "registry": {
            "gateway_serial": group.gateway_serial if group is not None else None,
            "consecutive_gateway_failures": group.consecutive_gateway_failures if group is not None else None,
            "generation": group.generation if group is not None else None,
            "automatic_restarts_enabled": group.restart_policy.enabled,
            "restarted_individually": sorted(group.restarted_individually),
            "restart_step": group.restart_step,
            "last_relay_failure_at": _isoformat(group.last_relay_failure_at),
            "restart_lockout_until": _isoformat(group.restart_lockout_until),
        } if group is not None else None,
        "bluetooth_cache_total_connectable_devices": connectable_count,
        "bluetooth_connectable_scanners_registered": connectable_scanner_count,
        "devices": devices_diag,
    }
    return async_redact_data(diagnostics, TO_REDACT)
