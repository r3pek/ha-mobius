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
    """
    The config entry's title (shown in the discovery card and the
    integrations list). Uses serial, not MAC address, for the same reason
    _device_info() in sensor.py already does: identical-model devices
    (e.g. two XR15 lights) need a real disambiguator, and unlike MAC
    address, serial won't go stale if the device's address later changes
    (this title is set once at entry creation and never auto-updated --
    see python-mobius's documentation/12-device-identity-and-address-
    stability.md).

    All real call sites now guarantee parseable manufacturer data by the
    time this is called (the fail-fast fixes elsewhere in this file abort
    before ever reaching a title-display point without it) -- the
    fallback below is just defensive, not something expected to trigger
    in practice.
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
        # Check whether Home Assistant's own cache already has a fuller
        # snapshot than what was passed in (the initial discovery_info can
        # have incomplete manufacturer data -- e.g. matched via the
        # local_name matcher before a scan-response merge completed).
        latest = async_last_service_info(self.hass, discovery_info.address, connectable=True)
        if latest is not None and latest.manufacturer_data.get(MOBIUS_COMPANY_ID):
            discovery_info = latest

        info = _parsed_info_for(discovery_info)
        if info is None:
            # Fail rather than proceed with an address-based identity that
            # could break later if this device's address changes before
            # we ever learn its serial (see python-mobius's documentation/
            # 12-device-identity-and-address-stability.md) -- serial is
            # required for reliable identity/reconnection, not optional.
            # No special retry logic needed here: Home Assistant's own
            # Bluetooth integration will naturally re-trigger this step on
            # a later, more complete advertisement (typically within
            # seconds, given how often these devices advertise).
            return self.async_abort(reason="no_manufacturer_data")

        await self.async_set_unique_id(info.serial)
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
        # Only refreshes for a nicer title (e.g. showing the real model
        # instead of a generic "Mobius device (address)") -- unique_id is
        # already guaranteed serial-based by async_step_bluetooth(), which
        # now aborts rather than proceeding without one, so there's
        # nothing to re-check here.
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
            discovery = self._discovered_devices[address]
            info = _parsed_info_for(discovery)
            if info is None:
                # Shouldn't normally happen -- the dropdown below already
                # only offers devices we could identify -- but the
                # underlying advertisement data is a live cache that could
                # theoretically have changed between showing the form and
                # submitting it. Same "fail rather than proceed with an
                # unreliable identity" preference as async_step_bluetooth().
                return self.async_abort(reason="no_manufacturer_data")
            await self.async_set_unique_id(info.serial, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self._async_create_entry(discovery)

        # BUG FIX: this used to compare discovery.address against
        # self._async_current_ids() -- but since unique_id is now
        # serial-based (not address-based, see async_step_bluetooth()),
        # that set contains SERIAL numbers, not addresses. Comparing a MAC
        # address against a set of serials never matches anything, so
        # already-configured devices were never actually being excluded
        # here. Compare serial against serial instead.
        current_serials = self._async_current_ids()
        self._discovered_devices = {
            discovery.address: discovery
            for discovery in async_discovered_service_info(self.hass)
            if discovery.name
            and "mobius" in discovery.name.lower()
            # Only offer devices we can actually identify a serial for --
            # matches the same fail-fast preference as the automatic
            # discovery flow, applied here by simply not listing them
            # rather than letting you pick one that would then abort.
            and (info := _parsed_info_for(discovery)) is not None
            and info.serial not in current_serials
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
