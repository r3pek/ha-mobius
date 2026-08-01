"""Config flow for the Mobius integration.

Supports both automatic Bluetooth discovery (triggered by Home Assistant's
own bluetooth integration matching the `bluetooth` matchers in
manifest.json) and manual setup listing any already-discovered-but-
unconfigured Mobius devices.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_last_service_info,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResult

from mobius import MOBIUS_COMPANY_ID, parse_manufacturer_data

from .const import DOMAIN, CONF_SERIAL

_LOGGER = logging.getLogger(__name__)


def _parsed_info_for(discovery: BluetoothServiceInfoBleak):
    payload = discovery.manufacturer_data.get(MOBIUS_COMPANY_ID)
    return parse_manufacturer_data(payload) if payload else None


def _title_for(discovery: BluetoothServiceInfoBleak) -> str:
    payload = discovery.manufacturer_data.get(MOBIUS_COMPANY_ID)
    if not payload:
        _LOGGER.debug(
            "No manufacturer data (company id %#06x) in advertisement for %s; "
            "keys present: %s",
            MOBIUS_COMPANY_ID, discovery.address, list(discovery.manufacturer_data.keys()),
        )
        return f"Mobius device ({discovery.address})"
    info = parse_manufacturer_data(payload)
    if info and info.model:
        return f"{info.model.name} ({discovery.address})"
    _LOGGER.debug(
        "Manufacturer data present for %s but didn't parse into a known model "
        "(payload length %d, expected 23): %s",
        discovery.address, len(payload), payload.hex(),
    )
    return f"Mobius device ({discovery.address})"


class MobiusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Mobius."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> FlowResult:
        """Handle a device discovered by Home Assistant's Bluetooth integration."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {"name": _title_for(discovery_info)}
        return await self.async_step_bluetooth_confirm()

    def _refresh_discovery_info(self) -> None:
        """
        The BluetoothServiceInfoBleak snapshot from the initial discovery
        trigger can have incomplete manufacturer data -- e.g. if HA matched
        on the local_name matcher before a scan-response merge completed.
        Re-fetch whatever HA's Bluetooth manager currently has cached for
        this address, which by the time the confirm screen renders is
        usually more complete, and use it if it's actually better.
        """
        assert self._discovery_info is not None
        latest = async_last_service_info(self.hass, self._discovery_info.address, connectable=True)
        if latest is None:
            return
        old_has_data = bool(self._discovery_info.manufacturer_data.get(MOBIUS_COMPANY_ID))
        new_has_data = bool(latest.manufacturer_data.get(MOBIUS_COMPANY_ID))
        if new_has_data and not old_has_data:
            _LOGGER.debug(
                "Refreshed discovery info for %s: initial snapshot had no "
                "manufacturer data, cached snapshot does",
                self._discovery_info.address,
            )
        self._discovery_info = latest

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm a Bluetooth-discovered device before creating the entry."""
        assert self._discovery_info is not None
        self._refresh_discovery_info()
        self.context["title_placeholders"] = {"name": _title_for(self._discovery_info)}

        if user_input is not None:
            return self._async_create_entry(self._discovery_info)

        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manual setup: offer any discovered-but-unconfigured Mobius devices."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self._async_create_entry(self._discovered_devices[address])

        current_addresses = self._async_current_ids()
        self._discovered_devices = {
            discovery.address: discovery
            for discovery in async_discovered_service_info(self.hass)
            if discovery.address not in current_addresses
            and discovery.name
            and "mobius" in discovery.name.lower()
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
        info = _parsed_info_for(discovery)
        if info is None:
            # Shouldn't normally happen -- _refresh_discovery_info() already
            # tries to get a fuller snapshot before this point -- but the
            # serial is now required (the connection/coordinator layer
            # resolves and reconnects to devices by serial, not address --
            # see python-mobius's documentation/
            # 12-device-identity-and-address-stability.md), so abort
            # cleanly rather than create an entry that could never connect.
            return self.async_abort(reason="no_manufacturer_data")
        return self.async_create_entry(
            title=_title_for(discovery),
            data={CONF_ADDRESS: discovery.address, CONF_SERIAL: info.serial},
        )
