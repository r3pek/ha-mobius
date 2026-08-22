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
    async_clear_address_from_match_history,
    async_discovered_service_info,
    async_last_service_info,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from mobius import Tank

from .const import DOMAIN, CONF_SERIAL, CONF_PAN_ID, CONF_DEVICES, CONF_MLPREFIX, MAX_CONCURRENT_CONNECTIONS
from .coordinator import discover_tank_for_serial, parsed_advertisement

_LOGGER = logging.getLogger(__name__)


def _parsed_info_for(discovery: BluetoothServiceInfoBleak):
    return parsed_advertisement(discovery.manufacturer_data)


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
    feature was designed against). Just the SUGGESTED starting point,
    pre-filled but editable on the tank_confirm form itself (see
    async_step_tank_confirm()) -- not meant to be the only chance to
    name it, but there's no reason to make picking a real name wait
    until after setup either."""
    return f"Mobius Tank ({len(tank.peers)} devices)"


def _device_list_for_display(tank: Tank) -> str:
    """A human-readable, one-line-per-device listing for the
    tank_confirm form's own description -- so "found a tank" doesn't
    just assert a device count without showing what was actually found.
    Matches _title_for()'s own "{model} ({serial})" format for a single
    device, for consistency between the two confirm screens."""
    lines = []
    for peer in tank.peers:
        model_name = peer.model.name if peer.model else f"unknown model ({peer.model_raw})"
        lines.append(f"- {model_name} ({peer.serial})")
    return "\n".join(lines)


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
    hass: HomeAssistant, entry: ConfigEntry, serial: str, address: str | None = None,
) -> None:
    """Adds one more device to an already-configured tank entry and
    reloads it -- the whole entry, not just the new device (a simpler,
    if slightly less surgical, approach than trying to hot-add just the
    new device's own coordinator without disturbing the others; there's
    normally no entity actively "in use" mid-merge for this brief
    reconnect to matter in practice).

    address is optional -- present for the original discovery-time merge
    case (a fresh BLE advertisement always carries one), absent for the
    periodic tank-revalidation case (see __init__.py's own
    _async_revalidate_tank()), which learns about a migrated device via
    a mesh-level peer report, not a BLE advertisement, so there's no BLE
    MAC to store -- matches how a tank peer discovered via
    discover_tank() already never stores CONF_ADDRESS either way."""
    devices = list(entry.data.get(CONF_DEVICES, []))
    device = {CONF_SERIAL: serial}
    if address is not None:
        device[CONF_ADDRESS] = address
    devices.append(device)
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DEVICES: devices})
    # hass.create_task, not hass.async_create_task -- this runs from
    # more than one context (a fresh discovery-time config flow step,
    # always event-loop-safe, but also __init__.py's own periodic
    # revalidation timer, which a real, confirmed warning showed isn't
    # always guaranteed to be), so the version safe from any thread is
    # the right one here regardless of which caller this is.
    hass.create_task(hass.config_entries.async_reload(entry.entry_id))


async def _remove_device_from_entry(hass: HomeAssistant, entry: ConfigEntry, serial: str) -> None:
    """The other half of a migration (see __init__.py's own
    _async_revalidate_tank()) -- removes one device from a tank entry
    and reloads it, the same "reload the whole entry" approach
    _merge_device_into_entry() already uses for the same reasons. Not
    used for discovery-time merging at all (that only ever adds) --
    exists purely so a device that's since moved to a different,
    already-tracked tank can be cleanly taken out of its old one as
    part of that confirmed move, never on its own (see
    _async_revalidate_tank()'s own docstring for why a device simply
    going unreported is never, by itself, a reason to remove it)."""
    devices = [
        d for d in entry.data.get(CONF_DEVICES, []) if d.get(CONF_SERIAL) != serial
    ]
    hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_DEVICES: devices})
    # See _merge_device_into_entry()'s own comment just above for why
    # create_task, not async_create_task -- same reasoning applies here.
    hass.create_task(hass.config_entries.async_reload(entry.entry_id))


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
        if latest is not None and parsed_advertisement(latest.manufacturer_data) is not None:
            discovery_info = latest

        info = _parsed_info_for(discovery_info)
        if info is None:
            # Fail rather than proceed with an address-based identity that
            # could break later if this device's address changes before
            # we ever learn its serial (see python-mobius's documentation/
            # 12-device-identity-and-address-stability.md) -- serial is
            # required for reliable identity/reconnection, not optional.
            #
            # Confirmed via a real capture that these devices split their
            # info across multiple, rotating advertisement packets --
            # name plus a 128-bit service UUID alone already fills 29 of
            # the 31 bytes a legacy advertisement allows, leaving no room
            # for manufacturer data in the same packet. Home Assistant's own
            # match-history behavior means this step doesn't reliably
            # re-trigger on its own once a later, fuller advertisement
            # arrives: match history is keyed on which
            # fields/UUIDs have been seen for an address at all, not
            # whether their content has since changed, so a later
            # advertisement carrying manufacturer data for the first time
            # does NOT reliably re-trigger a fresh discovery step on its
            # own once this address has already matched via local_name
            # (see manifest.json's own "bluetooth" matchers -- local_name
            # and each confirmed company ID's own manufacturer_id are
            # all registered there independently). Clearing this address's own match history
            # here, rather than just aborting and hoping, is what
            # actually lets the next advertisement -- even one that,
            # superficially, looks like something already seen -- get a
            # real chance to trigger this step again.
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
        _LOGGER.debug(
            "Mesh scan for %s: %s",
            self._pending_serial,
            "unreachable" if tank is None
            else f"prefix={tank.prefix.hex() if tank.prefix else None}, {len(tank.peers)} peer(s)",
        )

        if tank is not None and tank.prefix is not None and len(tank.peers) > 1:
            # "name" is required here for a reason that has nothing to do
            # with this step's own form text (which deliberately doesn't
            # repeat it, to avoid the "Found N devices (Mobius Tank (N
            # devices))" redundancy this whole change was meant to fix) --
            # confirmed via Home Assistant's own developer docs: the
            # "Discovered" card shown in Settings > Devices & Services
            # BEFORE this form is even opened gets its own title from
            # title_placeholders["name"] (combined with a flow_title
            # template in strings.json -- see below), and if "name" isn't
            # present at all, that whole mechanism is silently ignored --
            # not just left blank, ignored entirely, falling back to the
            # bare integration name ("Mobius"/"Mobius") instead.
            self.context["title_placeholders"] = {
                "count": str(len(tank.peers)),
                "devices": _device_list_for_display(tank),
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

        Only overwrites self._discovery_info with the new snapshot when
        it's actually at least as good (comparing whether each snapshot's
        manufacturer data actually parses into a usable
        MobiusAdvertisement, under any confirmed company ID -- not just
        whether SOME bytes happen to be present under one specific
        company ID, which could be a garbled/partial payload that
        wouldn't actually be usable) -- a perfectly good initial
        snapshot (WITH manufacturer data) must never be silently
        downgraded to a worse one (WITHOUT it), which real BLE devices
        can otherwise cause: they often rotate between several
        different advertisement payloads, not all of which necessarily
        carry the same data every time, so HA's Bluetooth cache can have
        a more recent, but data-less, advertisement/scan-response packet
        for that same address at the exact moment this runs. This
        matters concretely for a confirm screen that would otherwise
        show "Mobius device"/"Mobius" instead of a real model/serial:
        the confirm screen runs AFTER a connection attempt (which itself
        takes real time, giving the device's advertisement plenty of
        opportunity to rotate to a different payload in the meantime).
        """
        assert self._discovery_info is not None
        latest = async_last_service_info(self.hass, self._discovery_info.address, connectable=True)
        if latest is None:
            return
        old_has_data = parsed_advertisement(self._discovery_info.manufacturer_data) is not None
        new_has_data = parsed_advertisement(latest.manufacturer_data) is not None
        if not new_has_data:
            # Never downgrade -- whatever we already have (even if it
            # also lacks data) is at least as good as this one.
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
        """Confirm a multi-device tank before creating its entry --
        lets the tank be named right here, rather than only via a
        separate rename afterward."""
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

    def _async_create_tank_entry(self, tank: Tank, title: str) -> FlowResult:
        """Creates a multi-device tank entry -- one entry, N devices,
        CONF_MLPREFIX set (the tank's own stable identity, used as this
        entry's unique_id and later as the synthetic tank device's own
        identifier for via_device grouping -- see __init__.py). Every
        peer's own address (see MeshPeer) is its Thread mesh-local IPv6,
        not a BLE MAC -- CONF_ADDRESS is deliberately not stored for
        tank peers the way it is for an ad-hoc entry's own device
        (display/debugging only, per const.py's own docstring; the
        coordinator layer resolves and reconnects by serial regardless).

        Does NOT store each peer's own "age" value (python-mobius's
        MeshPeer.age) at all -- confirmed via reverse engineering the
        app's own network-troubleshooting screen to be a live, constantly-
        changing duration (time since that peer was last heard from on
        the mesh), not a fixed value there's any point capturing once at
        setup time and keeping around unrefreshed. See sensor.py's own
        MeshAddressSensor for where this now actually lives instead (as
        its own "last_seen" attribute) -- refreshed on every regular
        poll cycle, not stored here at all.

        title comes from what was actually typed/kept on the
        tank_confirm form (see async_step_tank_confirm()) -- not always
        regenerated from _title_for_tank(tank) here, since the whole
        point of that form's own name field is letting the tank be
        named up front rather than only via a separate rename
        afterward."""
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
