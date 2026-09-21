"""Config flow for the Mobius integration.

Devices are found through Home Assistant's Bluetooth discovery (the
`bluetooth` matchers in manifest.json) or picked manually from the
discovered, unconfigured devices.

## Tank-aware discovery

One config entry represents one Thread mesh ("tank"), with every device on
it. When an unconfigured device is found:

1. If its serial is already in an entry, the flow aborts.
2. If its pan_id belongs to an existing entry, the device is added to that
   entry without asking (and the entry is reloaded).
3. Otherwise the device is connected to briefly and asked for its mesh
   peers (discover_tank(), via discover_tank_for_serial()). With more than
   one device on the mesh, one "add tank with N devices" confirmation is
   shown. With only itself, when it can't be reached, or when it isn't part
   of a Thread network (prefix None), it is added as a single-device
   ("ad-hoc") entry.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_clear_address_from_match_history,
    async_discovered_service_info,
    async_last_service_info,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from mobius import Tank, parse_advertisement

from .const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX
from .coordinator import discover_tank_for_serial

_LOGGER = logging.getLogger(__name__)


def _parsed_info_for(discovery: BluetoothServiceInfoBleak):
    return parse_advertisement(discovery.manufacturer_data)


def _title_for(discovery: BluetoothServiceInfoBleak) -> str:
    """Title of a single-device entry: "{model} ({serial})". The serial tells
    identical models apart and, unlike the address, doesn't change.
    """
    info = _parsed_info_for(discovery)
    if info is None:
        _LOGGER.debug(
            "_title_for() called without parseable manufacturer data for %s "
            "-- shouldn't normally happen, all call sites should already "
            "guarantee this",
            discovery.address,
        )
        return "Mobius device"
    if info.model and info.serial:
        return f"{info.model.name} ({info.serial})"
    if info.model:
        return info.model.name
    return "Mobius device"


def _title_for_tank(tank: Tank) -> str:
    """Suggested title of a tank entry (editable on the tank_confirm form)."""
    return f"Mobius Tank ({len(tank.peers)} devices)"


def _device_list_for_display(tank: Tank) -> str:
    """One "- {model} ({serial})" line per tank device, for the tank_confirm
    form.
    """
    lines = []
    for peer in tank.peers:
        model_name = peer.model.name if peer.model else f"unknown model ({peer.model_raw})"
        lines.append(f"- {model_name} ({peer.serial})")
    return "\n".join(lines)


def _configured_serials(hass: HomeAssistant) -> dict[str, ConfigEntry]:
    """Every configured device serial and the entry it belongs to."""
    serials: dict[str, ConfigEntry] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        for device in entry.data.get(CONF_DEVICES, []):
            serials.setdefault(device.get(CONF_SERIAL), entry)
    return serials


def _find_entry_containing_serial(hass: HomeAssistant, serial: str) -> ConfigEntry | None:
    """The entry (tank or ad-hoc) whose device list contains `serial`."""
    return _configured_serials(hass).get(serial)


def _find_entry_for_pan_id(hass: HomeAssistant, pan_id: int) -> ConfigEntry | None:
    """The entry tracking `pan_id`, if any."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_PAN_ID) == pan_id:
            return entry
    return None


def _update_entry_devices(hass: HomeAssistant, entry: ConfigEntry, devices: list[dict]) -> None:
    """Stores a new device list for `entry` and reloads the whole entry.
    hass.create_task() because callers include a timer callback that isn't
    guaranteed to run on the event loop thread."""
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DEVICES: devices})
    hass.create_task(hass.config_entries.async_reload(entry.entry_id))


async def _merge_device_into_entry(
    hass: HomeAssistant, entry: ConfigEntry, serial: str, address: str | None = None,
) -> None:
    """Adds a device to a tank entry and reloads it. `address` (the BLE
    address, display only) is only known when the device was found through
    its advertisement, not when it was found on the mesh."""
    device = {CONF_SERIAL: serial}
    if address is not None:
        device[CONF_ADDRESS] = address
    _update_entry_devices(hass, entry, [*entry.data.get(CONF_DEVICES, []), device])


async def _remove_device_from_entry(hass: HomeAssistant, entry: ConfigEntry, serial: str) -> None:
    """Removes a device from a tank entry and reloads it (used when the
    device has moved to another tank, see __init__.py's
    _async_revalidate_tank())."""
    _update_entry_devices(
        hass, entry, [d for d in entry.data.get(CONF_DEVICES, []) if d.get(CONF_SERIAL) != serial],
    )


class MobiusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Mobius."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}
        self._discovered_tank: Tank | None = None
        self._pending_serial: str | None = None
        self._pending_pan_id: int | None = None

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle a device discovered by Home Assistant's Bluetooth integration."""
        # The cached advertisement may be more complete than the one that
        # triggered discovery.
        latest = async_last_service_info(self.hass, discovery_info.address, connectable=True)
        if latest is not None and parse_advertisement(latest.manufacturer_data) is not None:
            discovery_info = latest

        info = _parsed_info_for(discovery_info)
        if info is None:
            # The serial is required (devices are identified by serial, not address).
            # These devices spread their data over several advertisements (name plus
            # service UUID fill a legacy advertisement), so manufacturer data may not
            # have been seen yet. Home Assistant only re-triggers discovery for new
            # matchers, not for new content, so the address is cleared from the match
            # history to let a later advertisement start discovery again.
            async_clear_address_from_match_history(self.hass, discovery_info.address)
            _LOGGER.debug(
                "Bluetooth discovery for %s aborted: no manufacturer data yet "
                "(cleared match history so a later advertisement can retry)",
                discovery_info.address,
            )
            return self.async_abort(reason="no_manufacturer_data")

        if _find_entry_containing_serial(self.hass, info.serial) is not None:
            _LOGGER.debug("Bluetooth discovery for %s: already configured", info.serial)
            return self.async_abort(reason="already_configured")

        existing_tank_entry = _find_entry_for_pan_id(self.hass, info.pan_id)
        if existing_tank_entry is not None:
            _LOGGER.debug(
                "%s discovered with pan_id %#06x, matching existing tank %r -- merging",
                info.serial, info.pan_id, existing_tank_entry.title,
            )
            await _merge_device_into_entry(
                self.hass, existing_tank_entry, info.serial, discovery_info.address,
            )
            return self.async_abort(reason="merged_into_tank")

        # Temporary unique_id so a second concurrent flow for the same new pan_id
        # aborts while this one runs. The entry's real unique_id (mesh prefix or
        # serial) is set when it is created.
        await self.async_set_unique_id(f"pan-{info.pan_id}")

        self._discovery_info = discovery_info
        self._pending_serial = info.serial
        self._pending_pan_id = info.pan_id
        return await self.async_step_scan_tank()

    async def async_step_scan_tank(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Connects briefly to read the device's mesh peers, then continues with
        tank_confirm or bluetooth_confirm (see the module docstring).
        """
        assert self._discovery_info is not None
        # A flow can run before async_setup(); the connection semaphore must be
        # the shared one either way.
        from . import _shared_state  # the package module is already loaded
        semaphore, _registry = _shared_state(self.hass)
        tank = await discover_tank_for_serial(self.hass, self._pending_serial, semaphore)
        self._discovered_tank = tank
        _LOGGER.debug(
            "Mesh scan for %s: %s",
            self._pending_serial,
            "unreachable" if tank is None
            else f"prefix={tank.prefix.hex() if tank.prefix else None}, {len(tank.peers)} peer(s)",
        )

        if tank is not None and tank.prefix is not None and len(tank.peers) > 1:
            # "name" is required: the "Discovered" card takes its title from
            # title_placeholders["name"] (through flow_title in strings.json) and falls
            # back to the integration name without it.
            self.context["title_placeholders"] = {
                "count": str(len(tank.peers)),
                "devices": _device_list_for_display(tank),
                "name": _title_for_tank(tank),
            }
            return await self.async_step_tank_confirm()

        # Unreachable, not in a Thread network, or alone on it: single-device
        # entry.
        self.context["title_placeholders"] = {"name": _title_for(self._discovery_info)}
        return await self.async_step_bluetooth_confirm()

    def _refresh_discovery_info(self) -> None:
        """Replaces the stored discovery snapshot with the one currently cached for
        the address, but only if the cached one has parseable manufacturer data.
        Devices rotate advertisement payloads, so the latest one can lack data the
        original had.
        """
        assert self._discovery_info is not None
        latest = async_last_service_info(self.hass, self._discovery_info.address, connectable=True)
        if latest is None:
            return
        old_has_data = parse_advertisement(self._discovery_info.manufacturer_data) is not None
        new_has_data = parse_advertisement(latest.manufacturer_data) is not None
        if not new_has_data:
            return
        if not old_has_data:
            _LOGGER.debug(
                "Refreshed discovery info for %s: initial snapshot had no "
                "manufacturer data, cached snapshot does",
                self._discovery_info.address,
            )
        self._discovery_info = latest

    async def async_step_tank_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirms a multi-device tank and lets it be named."""
        assert self._discovered_tank is not None
        if user_input is not None:
            # The final unique_id: the mesh prefix.
            await self.async_set_unique_id(self._discovered_tank.prefix.hex())
            self._abort_if_unique_id_configured()
            return self._async_create_tank_entry(
                self._discovered_tank, user_input[CONF_NAME]
            )

        return self.async_show_form(
            step_id="tank_confirm",
            data_schema=vol.Schema({
                vol.Required(CONF_NAME, default=_title_for_tank(self._discovered_tank)): str,
            }),
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirms a single device (no tank found)."""
        assert self._discovery_info is not None
        # Refreshed for the title only.
        self._refresh_discovery_info()
        self.context["title_placeholders"] = {"name": _title_for(self._discovery_info)}

        if user_input is not None:
            info = _parsed_info_for(self._discovery_info)
            if info is not None:
                # The final unique_id: the serial.
                await self.async_set_unique_id(info.serial)
                self._abort_if_unique_id_configured()
            return self._async_create_entry(self._discovery_info)

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual setup: pick one of the discovered, unconfigured devices."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            discovery = self._discovered_devices[address]
            info = _parsed_info_for(discovery)
            if info is None:
                # The advertisement cache can change between showing the form and
                # submitting it.
                return self.async_abort(reason="no_manufacturer_data")

            # The device may belong to a tank that was set up through another device.
            existing_tank_entry = _find_entry_for_pan_id(self.hass, info.pan_id)
            if existing_tank_entry is not None:
                await _merge_device_into_entry(
                    self.hass, existing_tank_entry, info.serial, discovery.address,
                )
                return self.async_abort(reason="merged_into_tank")

            self._discovery_info = discovery
            self._pending_serial = info.serial
            self._pending_pan_id = info.pan_id
            return await self.async_step_scan_tank()

        already_configured_serials = _configured_serials(self.hass)
        self._discovered_devices = {
            discovery.address: discovery
            for discovery in async_discovered_service_info(self.hass)
            if discovery.name
            and "mobius" in discovery.name.lower()
            # Only devices with a known serial are offered.
            and (info := _parsed_info_for(discovery)) is not None
            and info.serial not in already_configured_serials
        }

        if not self._discovered_devices:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: _title_for(discovery)
                            for address, discovery in self._discovered_devices.items()
                        }
                    )
                }
            ),
        )

    def _async_create_entry(self, discovery: BluetoothServiceInfoBleak) -> FlowResult:
        """Creates a single-device entry (CONF_DEVICES with one device, no
        CONF_MLPREFIX).
        """
        info = _parsed_info_for(discovery)
        if info is None:
            # The serial is required to connect; don't create an unusable entry.
            return self.async_abort(reason="no_manufacturer_data")
        return self.async_create_entry(
            title=_title_for(discovery),
            data={
                CONF_PAN_ID: info.pan_id,
                CONF_DEVICES: [{CONF_SERIAL: info.serial, CONF_ADDRESS: discovery.address}],
            },
        )

    def _async_create_tank_entry(self, tank: Tank, title: str) -> FlowResult:
        """Creates a tank entry with every mesh peer and CONF_MLPREFIX. Peers have no
        BLE address to store (MeshPeer.address is the mesh IPv6), and MeshPeer.age
        is not stored (it is shown, refreshed, by the mesh address sensor).
        """
        assert tank.prefix is not None
        assert self._pending_pan_id is not None
        devices = [{CONF_SERIAL: peer.serial} for peer in tank.peers]
        return self.async_create_entry(
            title=title,
            data={
                CONF_PAN_ID: self._pending_pan_id,
                CONF_MLPREFIX: tank.prefix.hex(),
                CONF_DEVICES: devices,
            },
        )
