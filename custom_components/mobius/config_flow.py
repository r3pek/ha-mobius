"""Config flow for the Mobius integration.

Supports both automatic Bluetooth discovery (triggered by Home Assistant's
own bluetooth integration matching the `bluetooth` matchers in
manifest.json) and manual setup listing any already-discovered-but-
unconfigured Mobius devices.

## Tank-aware discovery

One config entry represents one Thread mesh/"tank" (see gateway_registry.
py's own docstring for why pan_id is the established local proxy for
this), not one device -- a real setup with N devices on the same tank
gets ONE entry with N devices in it, not N separate entries. When a new,
unconfigured device is discovered:

1. If its serial is already part of some existing entry -- already
   configured, abort (matches the old per-device dedup, just checking a
   list now instead of a single serial-based unique_id).
2. If its pan_id is already tracked by an existing entry, but its own
   serial isn't yet in that entry's device list -- this is the MERGE
   case: we already have a tank, this is one more device on it, not a
   new tank. Silently adds this device to that entry's data and reloads
   it -- no prompt at all, matching that this should feel automatic once
   the tank itself is already configured.
3. Otherwise, this could be a genuinely new tank (or a standalone,
   never-provisioned device -- see below). Connects briefly to ask what
   ELSE is on this device's own Thread mesh (mobius.discovery.
   discover_tank(), via discover_tank_for_serial()):
   - Found more than one device on it -- shows ONE "add tank with N
     devices" confirm, not N separate confirms.
   - Found only itself (or couldn't connect, or this device genuinely
     isn't part of any provisioned Thread network at all -- python-
     mobius's discover_tank() returns prefix=None for that last case,
     see its own docstring) -- falls back to the original, unchanged
     single-device confirm flow. This is the ad-hoc case: a device not
     (yet) provisioned into a tank by the Mobius app itself still gets
     added, just as its own standalone entry rather than forcing it to
     wait for that provisioning to happen first.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_last_service_info,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from mobius import MOBIUS_COMPANY_ID, parse_manufacturer_data, Tank

from .const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX, CONF_AGE, MAX_CONCURRENT_CONNECTIONS
from .coordinator import discover_tank_for_serial

_LOGGER = logging.getLogger(__name__)


def _parsed_info_for(discovery: BluetoothServiceInfoBleak):
    payload = discovery.manufacturer_data.get(MOBIUS_COMPANY_ID)
    return parse_manufacturer_data(payload) if payload else None


def _title_for(discovery: BluetoothServiceInfoBleak) -> str:
    """
    A single (ad-hoc) device entry's title. Uses serial, not MAC
    address, for the same reason _device_info() in sensor.py already
    does: identical-model devices (e.g. two XR15 lights) need a real
    disambiguator, and unlike MAC address, serial won't go stale if the
    device's address later changes (this title is set once at entry
    creation and never auto-updated -- see python-mobius's
    documentation/12-device-identity-and-address-stability.md).

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


def _title_for_tank(tank: Tank) -> str:
    """A multi-device tank entry's default title -- matches the "one
    integration entry = one hub with N child devices" grouping this is
    all in service of (the LG ThinQ-style UI reference this whole
    feature was designed against). Renameable afterward like any other
    entry/device title -- this is just the sensible starting point, not
    meant to be the permanent name."""
    return f"Mobius Tank ({len(tank.peers)} devices)"


def _find_entry_containing_serial(hass: HomeAssistant, serial: str) -> ConfigEntry | None:
    """Is this serial already part of ANY existing entry's device list
    (tank or ad-hoc, both use the same CONF_DEVICES shape)? Replaces the
    old direct unique_id-based dedup, since a config entry's own
    unique_id is now tank-scoped (mlprefix hex) or, for an ad-hoc entry,
    still serial-based -- either way, checking membership in the actual
    device list is what's needed now, not comparing against unique_id
    directly."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        for device in entry.data.get(CONF_DEVICES, []):
            if device.get(CONF_SERIAL) == serial:
                return entry
    return None


def _find_entry_for_pan_id(hass: HomeAssistant, pan_id: int) -> ConfigEntry | None:
    """Is this pan_id already tracked by an existing entry at all
    (regardless of whether this specific serial is in it yet)? The
    merge case -- see this module's own docstring -- is precisely "yes,
    but the serial isn't in it yet"."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(CONF_PAN_ID) == pan_id:
            return entry
    return None


async def _merge_device_into_entry(
    hass: HomeAssistant, entry: ConfigEntry, serial: str, address: str,
) -> None:
    """Adds one more device to an already-configured tank entry and
    reloads it -- the whole entry, not just the new device (a simpler,
    if slightly less surgical, approach than trying to hot-add just the
    new device's own coordinator without disturbing the others; there's
    normally no entity actively "in use" mid-merge for this brief
    reconnect to matter in practice)."""
    devices = list(entry.data.get(CONF_DEVICES, []))
    devices.append({CONF_SERIAL: serial, CONF_ADDRESS: address})
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DEVICES: devices})
    hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))


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

        if _find_entry_containing_serial(self.hass, info.serial) is not None:
            return self.async_abort(reason="already_configured")

        existing_tank_entry = _find_entry_for_pan_id(self.hass, info.pan_id)
        if existing_tank_entry is not None:
            await _merge_device_into_entry(
                self.hass, existing_tank_entry, info.serial, discovery_info.address,
            )
            return self.async_abort(reason="merged_into_tank")

        # Deduplicates CONCURRENT discovery flows for the same
        # not-yet-tracked pan_id -- e.g. two devices from the same
        # brand-new tank both advertising and triggering this step
        # around the same time, before either has been confirmed. This
        # is deliberately NOT the entry's eventual real unique_id (that's
        # set later, in _async_create_tank_entry()/_async_create_entry()
        # -- mlprefix hex for a tank, serial for ad-hoc); it exists only
        # to make the SECOND concurrent flow for this pan_id abort against
        # the FIRST one still in progress, via async_set_unique_id's own
        # default raise_on_progress=True. Once either flow completes (or
        # is abandoned), this in-progress registration disappears on its
        # own -- it never becomes a real entry's unique_id, so it can't
        # collide with _find_entry_for_pan_id() above on a later,
        # separate discovery.
        await self.async_set_unique_id(f"pan-{info.pan_id}")

        self._discovery_info = discovery_info
        self._pending_serial = info.serial
        self._pending_pan_id = info.pan_id
        return await self.async_step_scan_tank()

    async def async_step_scan_tank(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """
        Connects briefly to ask what else is on this device's own Thread
        mesh (see this module's own docstring for the full decision
        tree). No form of its own -- this step exists purely to do that
        connection attempt as a distinct, named step (visible in logs/
        traces if something goes wrong here specifically) before
        branching to whichever confirm screen actually applies.
        """
        assert self._discovery_info is not None
        # Ensures the shared connection semaphore exists rather than
        # assuming async_setup() has already run and populated it --
        # a config flow isn't guaranteed to run after setup has fully
        # completed (config flows can be triggered very early in Home
        # Assistant's own startup sequence). Same setdefault pattern
        # __init__.py's own async_setup()/async_setup_entry() use, so
        # whichever one actually runs first, they end up sharing the
        # exact same semaphore object either way -- not two separate
        # ones that would defeat the whole point of a SHARED throttle.
        semaphore = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            "connection_semaphore", asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)
        )
        tank = await discover_tank_for_serial(self.hass, self._pending_serial, semaphore)
        self._discovered_tank = tank

        if tank is not None and tank.prefix is not None and len(tank.peers) > 1:
            self.context["title_placeholders"] = {
                "count": str(len(tank.peers)),
                "name": _title_for_tank(tank),
            }
            return await self.async_step_tank_confirm()

        # Couldn't connect at all, connected but this device isn't part
        # of any provisioned Thread network (prefix is None -- see
        # discover_tank()'s own docstring), or a "tank" of exactly one
        # (itself) -- all fall back to the original single-device flow,
        # unchanged. A lone device on its own Thread network and a
        # genuinely never-provisioned device look identical from here,
        # and both get the same, simplest treatment: add it standalone.
        self.context["title_placeholders"] = {"name": _title_for(self._discovery_info)}
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

    async def async_step_tank_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm a multi-device tank before creating its entry."""
        assert self._discovered_tank is not None
        if user_input is not None:
            # Overwrites the provisional "pan-{pan_id}" dedup ID from
            # async_step_bluetooth() with the tank's real, final identity
            # -- mlprefix hex, more stable than pan_id for a permanent
            # entry identity (see const.py's own CONF_MLPREFIX docstring).
            # _abort_if_unique_id_configured() here is a defensive-only
            # safety net; the real dedup already happened earlier via
            # _find_entry_containing_serial()/_find_entry_for_pan_id().
            await self.async_set_unique_id(self._discovered_tank.prefix.hex())
            self._abort_if_unique_id_configured()
            return self._async_create_tank_entry(self._discovered_tank)

        self._set_confirm_only()
        return self.async_show_form(
            step_id="tank_confirm",
            description_placeholders=self.context["title_placeholders"],
        )

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm a single, ad-hoc (no tank found) device before creating the entry."""
        assert self._discovery_info is not None
        # Only refreshes for a nicer title (e.g. showing the real model
        # instead of a generic "Mobius device (address)") -- unique_id is
        # set from the (already-confirmed-parseable) serial in
        # _async_create_entry(), so there's nothing to re-check here.
        self._refresh_discovery_info()
        self.context["title_placeholders"] = {"name": _title_for(self._discovery_info)}

        if user_input is not None:
            info = _parsed_info_for(self._discovery_info)
            if info is not None:
                # Overwrites the provisional "pan-{pan_id}" dedup ID from
                # async_step_bluetooth() with this device's real, final
                # identity -- its own serial, matching the original,
                # pre-tank-aware behavior exactly for this ad-hoc case.
                # _abort_if_unique_id_configured() here is a defensive-only
                # safety net; the real dedup already happened earlier via
                # _find_entry_containing_serial().
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

            # Same merge check async_step_bluetooth() does -- the dropdown
            # already excludes devices whose OWN serial is configured, but
            # not ones whose pan_id matches an existing tank they're not
            # yet a member of (that tank might only have been discovered/
            # confirmed via a DIFFERENT device's own automatic discovery
            # flow, with this one never having triggered async_step_
            # bluetooth() at all if it was already visible when that
            # happened).
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

        # Excludes devices already part of ANY existing entry (tank or
        # ad-hoc) -- matches async_step_bluetooth()'s own
        # _find_entry_containing_serial() check, just applied here as a
        # filter on what's offered rather than an abort after picking one.
        already_configured_serials = {
            device.get(CONF_SERIAL)
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            for device in entry.data.get(CONF_DEVICES, [])
        }
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
        """Creates an ad-hoc, single-device entry -- same CONF_DEVICES
        shape a multi-device tank entry uses, just with one device in
        it, and no CONF_MLPREFIX (there's no confirmed tank prefix to
        store -- see this module's own docstring for why a device not
        provisioned into a tank yet, or a lone device on its own Thread
        network, both end up here)."""
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
            data={
                CONF_PAN_ID: info.pan_id,
                CONF_DEVICES: [{CONF_SERIAL: info.serial, CONF_ADDRESS: discovery.address}],
            },
        )

    def _async_create_tank_entry(self, tank: Tank) -> FlowResult:
        """Creates a multi-device tank entry -- one entry, N devices,
        CONF_MLPREFIX set (the tank's own stable identity, used as this
        entry's unique_id and later as the synthetic tank device's own
        identifier for via_device grouping -- see __init__.py). Every
        peer's own address (see MeshPeer) is its Thread mesh-local IPv6,
        not a BLE MAC -- CONF_ADDRESS is deliberately not stored for
        tank peers the way it is for an ad-hoc entry's own device
        (display/debugging only, per const.py's own docstring; the
        coordinator layer resolves and reconnects by serial regardless).
        Each peer's CONF_AGE (if the underlying MeshPeer had one -- see
        that field's own docstring for the important caveat that it's a
        one-time discovery snapshot, not live data) is stored too, for
        display -- see const.py's own CONF_AGE docstring."""
        assert tank.prefix is not None
        assert self._pending_pan_id is not None
        devices = []
        for peer in tank.peers:
            device = {CONF_SERIAL: peer.serial}
            if peer.age is not None:
                device[CONF_AGE] = peer.age
            devices.append(device)
        return self.async_create_entry(
            title=_title_for_tank(tank),
            data={
                CONF_PAN_ID: self._pending_pan_id,
                CONF_MLPREFIX: tank.prefix.hex(),
                CONF_DEVICES: devices,
            },
        )
