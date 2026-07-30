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
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.data_entry_flow import FlowResult

from mobius import MOBIUS_COMPANY_ID, parse_manufacturer_data

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _title_for(discovery: BluetoothServiceInfoBleak) -> str:
    payload = discovery.manufacturer_data.get(MOBIUS_COMPANY_ID)
    info = parse_manufacturer_data(payload) if payload else None
    if info and info.model:
        return f"{info.model.name} ({discovery.address})"
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

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm a Bluetooth-discovered device before creating the entry."""
        assert self._discovery_info is not None
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
        return self.async_create_entry(
            title=_title_for(discovery),
            data={CONF_ADDRESS: discovery.address},
        )
