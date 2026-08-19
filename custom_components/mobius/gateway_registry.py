"""
Shared per-pan_id gateway registry.

Multiple physical devices sharing the same pan_id (Thread mesh/"tank",
confirmed via reverse engineering the app's own tank-grouping model --
see python-mobius's
documentation/09-thread-coap-relay.md) share ONE physical BLE connection
rather than each holding their own. One member of the group is the
"gateway" (owns a real MobiusConnectionManager, an actual BLE
connection); every other member relays through it via RelayedMobiusDevice
(wired in by coordinator.py, not this module -- this module only tracks
group membership and which serial is currently gateway).

## Gateway selection

Whichever device is first to register for a given pan_id becomes
gateway -- except when a group is brand new and multiple devices are
registering at roughly the same time (e.g. Home Assistant startup with
several config entries for the same tank loading concurrently), in which
case selection waits for a short settle window
(GATEWAY_ELECTION_SETTLE_SECONDS) so it can pick the best-RSSI candidate
among whoever showed up in that window, rather than whichever async task
happened to run first.

Selection happens once per group formation. A better-signal device
joining an ALREADY-established group later does not displace a working
gateway -- only GATEWAY_FAILURE_THRESHOLD consecutive gateway failures
(see record_gateway_failure()) or the gateway leaving the group
(leave()) triggers a change. Continuously reassigning
gateway based on signal strength alone would cause unnecessary
connection churn for a marginal benefit.

## Failover

If the gateway fails GATEWAY_FAILURE_THRESHOLD consecutive poll cycles,
another member is promoted immediately -- much faster than the general
per-device mark-unavailable threshold (MARK_UNAVAILABLE_AFTER, handled in
coordinator.py, not here), since a bad gateway takes its whole group
down with it. The demoted former gateway becomes a normal relayed member
of the newly-promoted gateway. If there's no other member to promote to,
the group is simply left without a gateway -- coordinator.py falls back
to each remaining member trying its own direct connection, the same as
if relay didn't exist.

## pan_id is not assumed fixed

A device's pan_id can change -- it can be physically moved to a
different tank. This registry itself doesn't detect that on its own;
__init__.py's own periodic tank revalidation does (comparing what a
tank's gateway currently reports its mesh peers to be against what
each entry's own CONF_DEVICES list says), and handles a confirmed move
at the CONFIG ENTRY level -- removing the device from its old entry's
device list and merging it into the new one's, then reloading both.
That reload is what actually moves the device between this registry's
own groups (the old entry's own teardown calls leave(), the new
entry's own setup calls join()), not any direct, single registry-level
operation.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from homeassistant.core import HomeAssistant

from .const import GATEWAY_ELECTION_SETTLE_SECONDS, GATEWAY_FAILURE_THRESHOLD, RELAY_FAILURE_THRESHOLD

if TYPE_CHECKING:
    from .coordinator import MobiusConnectionManager

_LOGGER = logging.getLogger(__name__)


def _format_mesh_address(address: Optional[bytes]) -> str:
    """Human-readable form for log messages only -- the real sensor-
    facing formatting lives in sensor.py's own MeshAddressSensor; this
    is deliberately a separate, simpler copy rather than a shared import,
    since a malformed address (wrong length -- shouldn't normally happen,
    but this is a logging path, not a data path, so it must never itself
    raise) should degrade to the raw hex rather than blow up the very
    debug logging meant to help diagnose a problem."""
    if address is None:
        return "unknown"
    try:
        return str(ipaddress.IPv6Address(address))
    except ValueError:
        return address.hex()


@dataclass
class MemberState:
    """One device's membership record within a PanGroup."""
    serial: str
    rssi: Optional[int] = None
    # Cached once discovered (see coordinator.py's address-discovery
    # background task) -- this device's own Thread mesh-local IPv6
    # address, needed to construct a RelayedMobiusDevice targeting it.
    mesh_address: Optional[bytes] = None
    # Refreshed on every one of the gateway's own poll cycles (every
    # POLL_INTERVAL -- see coordinator.py's own _fetch()), not a one-time
    # snapshot -- confirmed via reverse engineering the app's own
    # network-troubleshooting screen that the underlying value this is
    # computed from (each peer's own "how long since last heard from on
    # the mesh" duration) is itself a live, continuously-changing value,
    # not something meaningful to capture once and treat as static. An
    # absolute, already-computed timestamp (this device was last heard
    # from AT this moment), not the raw duration -- computed once, right
    # when the underlying duration is freshest, rather than a raw
    # duration paired with a separate poll timestamp for every consumer
    # to redo that subtraction itself.
    mesh_last_seen_at: Optional[datetime] = None
    # How many consecutive RELAYED reads to this specific member have
    # failed, through whatever gateway currently holds the group --
    # separate from PanGroup.consecutive_gateway_failures (the
    # gateway's own DIRECT read failing), since a real production
    # incident showed these are genuinely different failure modes: a
    # gateway can be perfectly healthy for its own reads, and for
    # relaying to SOME other members, while persistently failing to
    # relay to one specific target for 40+ minutes straight -- see
    # GatewayRegistry.record_relay_failure()'s own docstring for the
    # full reasoning and what this actually triggers once it reaches
    # RELAY_FAILURE_THRESHOLD.
    consecutive_relay_failures: int = 0


@dataclass
class PanGroup:
    """One pan_id's worth of shared gateway state."""
    pan_id: int
    gateway_serial: Optional[str] = None
    gateway_connection: Optional["MobiusConnectionManager"] = None
    members: dict[str, MemberState] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Only meaningful while non-zero; reset whenever gateway_serial
    # changes (a fresh gateway starts with a clean slate). Tracked here
    # rather than on MemberState since it's specifically about the
    # CURRENT gateway's connection health, not a property of any device
    # in the abstract.
    consecutive_gateway_failures: int = 0
    # Every serial that's been promoted to gateway and then itself gone
    # on to fail GATEWAY_FAILURE_THRESHOLD times, since the last time
    # ANY gateway actually succeeded (see record_gateway_success(), which
    # clears this) or since this set grew to cover every member (see
    # _best_candidate()'s own handling of that case). This set is what
    # prevents promotion from just picking the single best-RSSI member
    # excluding only the one CURRENTLY failing -- for a tank where two
    # devices both have much better RSSI than the other two, that would
    # mean failures ping-pong forever between exactly those two
    # best-RSSI devices (fail, promote the other; it fails too, promote
    # back to the first one, since excluding only "the one failing
    # right now" doesn't stop it being immediately re-eligible), with
    # the other two members never getting tried at all. This set breaks
    # that cycle: every member gets a real turn before anyone is
    # reconsidered.
    recently_failed_gateways: set[str] = field(default_factory=set)
    _electing: bool = False
    _gateway_elected: asyncio.Event = field(default_factory=asyncio.Event)

    def member_rssi_items(self, exclude_serials: Optional[set[str]] = None):
        exclude_serials = exclude_serials or set()
        return [
            (serial, m.rssi) for serial, m in self.members.items()
            if serial not in exclude_serials
        ]


class GatewayRegistry:
    """
    hass.data-stored singleton (one per Home Assistant instance, not per
    config entry) tracking every PanGroup. See this module's docstring
    for the full design.
    """

    def __init__(
        self, hass: HomeAssistant, semaphore: asyncio.Semaphore,
        election_settle_seconds: float = GATEWAY_ELECTION_SETTLE_SECONDS,
    ):
        self.hass = hass
        self.semaphore = semaphore
        self._election_settle_seconds = election_settle_seconds
        self._groups: dict[int, PanGroup] = {}

    def _group_for(self, pan_id: int) -> PanGroup:
        return self._groups.setdefault(pan_id, PanGroup(pan_id=pan_id))

    def group(self, pan_id: int) -> Optional[PanGroup]:
        """Read-only lookup -- returns None if no group exists for this
        pan_id (nobody has called join() for it yet, or the group was
        removed after its last member left)."""
        return self._groups.get(pan_id)

    async def join(
        self, pan_id: int, serial: str, rssi: Optional[int] = None,
        prefer_as_gateway: bool = False,
    ) -> PanGroup:
        """
        Registers `serial` as a member of `pan_id`'s group, creating the
        group if it doesn't exist yet. Always returns a group with
        gateway_serial/gateway_connection populated -- waits for
        election to complete if this call triggered (or arrived during)
        a brand-new group's settle window.

        `prefer_as_gateway`: set when the caller already has direct,
        recent proof this specific device is reachable (e.g. the config
        flow just connected to it to run discover_tank()) -- skips the
        normal RSSI-based settle-window election entirely for a
        brand-new group and assigns this serial gateway immediately,
        rather than waiting GATEWAY_ELECTION_SETTLE_SECONDS to maybe
        pick a different, equally-untested member by RSSI alone. Only
        has any effect on a genuinely brand-new group (gateway_serial is
        still None and no election is already in flight) -- a join()
        for an already-established group ignores this, since an
        existing working gateway is never displaced just because a
        later joiner asks to be preferred (same reasoning
        GATEWAY_FAILURE_THRESHOLD's own docstring gives for why
        signal-strength alone doesn't churn an established gateway).
        """
        group = self._group_for(pan_id)
        async with group.lock:
            existing = group.members.get(serial)
            if existing is not None:
                # Update rssi in place rather than replacing the whole
                # MemberState -- a fresh MemberState() would silently
                # reset mesh_address back to None on every join(),
                # including a normal Home Assistant restart (which
                # re-joins every already-known device), forcing a
                # redundant rediscovery connection for something that
                # was almost certainly still accurate. Confirmed via a
                # real test exposing this: the "skip rediscovery if
                # already cached" optimization in __init__.py's own
                # async_setup_entry() never actually took effect,
                # because join() itself had already thrown the cached
                # value away by the time that check ran.
                existing.rssi = rssi
            else:
                group.members[serial] = MemberState(serial=serial, rssi=rssi)
                _LOGGER.debug(
                    "%s joined pan_id %#06x (rssi=%s, %d member(s) now)",
                    serial, pan_id, rssi, len(group.members),
                )
            if group.gateway_serial is None and not group._electing:
                if prefer_as_gateway:
                    _LOGGER.debug(
                        "%s preferred as gateway for pan_id %#06x -- skipping RSSI election "
                        "(direct connectivity already confirmed)", serial, pan_id,
                    )
                    self._assign_gateway(group, serial)
                    group._gateway_elected.set()
                else:
                    group._electing = True
                    asyncio.ensure_future(self._elect_initial_gateway(group))

        if not group._gateway_elected.is_set():
            await group._gateway_elected.wait()

        return group

    async def _elect_initial_gateway(self, group: PanGroup) -> None:
        await asyncio.sleep(self._election_settle_seconds)
        async with group.lock:
            if group.gateway_serial is not None:
                return  # shouldn't happen (only one election runs per group), but defensive
            winner = self._best_candidate(group)
            _LOGGER.debug(
                "Gateway election for pan_id %#06x settled: %r elected from %s",
                group.pan_id, winner, dict(group.member_rssi_items()),
            )
            self._assign_gateway(group, winner)
            group._gateway_elected.set()

    def _best_candidate(self, group: PanGroup, exclude_serials: Optional[set[str]] = None) -> Optional[str]:
        """Highest known RSSI among current members (excluding
        exclude_serials); falls back to "first available" (dict insertion
        order) if no member has RSSI info at all."""
        candidates = group.member_rssi_items(exclude_serials=exclude_serials)
        if not candidates:
            return None
        with_rssi = [c for c in candidates if c[1] is not None]
        if with_rssi:
            return max(with_rssi, key=lambda c: c[1])[0]
        return candidates[0][0]

    def _assign_gateway(self, group: PanGroup, serial: Optional[str]) -> None:
        """Internal: must be called with group.lock held. Sets up (or
        clears, if serial is None) group.gateway_serial/gateway_connection
        and resets the failure counter for the new gateway."""
        # Local import to avoid a circular import -- coordinator.py
        # imports GatewayRegistry.
        from .coordinator import MobiusConnectionManager

        group.gateway_serial = serial
        group.consecutive_gateway_failures = 0
        group.gateway_connection = (
            MobiusConnectionManager(self.hass, serial, self.semaphore)
            if serial is not None else None
        )

    async def leave(self, pan_id: int, serial: str) -> None:
        """
        Removes `serial` from its group. If it was the gateway, promotes
        another member (see _best_candidate()) -- if no other member
        exists, the group's gateway is cleared (nothing left to be
        gateway of). If the group ends up with no members at all, it's
        removed entirely.
        """
        group = self._groups.get(pan_id)
        if group is None:
            return
        async with group.lock:
            group.members.pop(serial, None)
            if group.gateway_serial == serial:
                old_connection = group.gateway_connection
                new_gateway = self._best_candidate(group, exclude_serials=group.recently_failed_gateways)
                self._assign_gateway(group, new_gateway)
                if old_connection is not None:
                    await old_connection.disconnect()
                _LOGGER.info(
                    "Gateway for pan_id %#06x (was %r) is leaving; promoted %r",
                    pan_id, serial, new_gateway,
                )
            else:
                _LOGGER.debug(
                    "%s left pan_id %#06x (not the gateway, %d member(s) remain)",
                    serial, pan_id, len(group.members),
                )
            if not group.members:
                self._groups.pop(pan_id, None)

    def record_gateway_success(self, pan_id: int) -> None:
        """Call on every successful gateway read -- resets the
        consecutive-failure counter, and clears recently_failed_gateways
        (see PanGroup's own docstring for why that set exists at all):
        a real, successful read means the tank's back to healthy, so
        there's no reason to keep excluding devices that failed during
        whatever earlier trouble just ended -- they get a clean slate to
        be considered again if this gateway ever fails in the future."""
        group = self._groups.get(pan_id)
        if group is not None:
            if group.consecutive_gateway_failures > 0:
                # Only logged when this actually resets a real streak,
                # not on every single ordinary success -- that happens
                # every poll cycle for a healthy gateway and would be
                # pure noise. A recovery after N failures is exactly the
                # kind of intermittent-trouble signal worth keeping.
                _LOGGER.debug(
                    "Gateway %r for pan_id %#06x recovered after %d consecutive failure(s)",
                    group.gateway_serial, pan_id, group.consecutive_gateway_failures,
                )
            group.consecutive_gateway_failures = 0
            group.recently_failed_gateways.clear()

    async def _promote_away_from_current_gateway(self, group: PanGroup, reason: str) -> Optional[str]:
        """Shared promotion logic: excludes the current gateway (and
        every other recently-failed one) from consideration, assigns
        the next-best candidate, and disconnects the old connection.
        Called with group.lock already held by the caller -- both
        record_gateway_failure() and record_relay_failure() below reuse
        this exact same machinery, since "route around a bad gateway"
        is the same operation either way, just triggered by two
        genuinely different symptoms (see RELAY_FAILURE_THRESHOLD's own
        docstring for why those are kept as separate counters upstream
        of this shared call)."""
        failing_serial = group.gateway_serial
        old_connection = group.gateway_connection
        if failing_serial is not None:
            group.recently_failed_gateways.add(failing_serial)

        new_gateway = self._best_candidate(group, exclude_serials=group.recently_failed_gateways)
        if new_gateway is None:
            # Every member has now failed at least once since the last
            # success (see PanGroup's own docstring) -- rather than
            # getting stuck with nothing left to promote at all, give
            # everyone a clean slate and try again, excluding only the
            # one that JUST failed (no sense immediately re-picking that
            # one specifically, but everyone else deserves a fresh look
            # after a full round).
            _LOGGER.debug(
                "Every member of pan_id %#06x has now failed since the last success -- "
                "giving everyone a clean slate (excluding only %r, which just failed)",
                group.pan_id, failing_serial,
            )
            group.recently_failed_gateways = {failing_serial} if failing_serial is not None else set()
            new_gateway = self._best_candidate(group, exclude_serials=group.recently_failed_gateways)

        self._assign_gateway(group, new_gateway)
        _LOGGER.warning(
            "Gateway %r for pan_id %#06x %s; promoted %r",
            failing_serial, group.pan_id, reason, new_gateway,
        )
        if old_connection is not None:
            await old_connection.disconnect()
        return new_gateway

    async def record_gateway_failure(self, pan_id: int) -> bool:
        """
        Call when the CURRENT gateway's OWN connection/read fails. Returns
        True if this triggered a promotion (the GATEWAY_FAILURE_THRESHOLDth
        consecutive failure), False otherwise -- mainly useful for
        logging/tests; the promotion itself already updates
        group.gateway_serial/gateway_connection, so callers don't need to
        branch on the return value to behave correctly.

        For a RELAYED read to some other member failing, through a
        gateway whose own reads are still succeeding, see
        record_relay_failure() below instead -- a genuinely different
        symptom, deliberately not funneled through this same counter.
        """
        group = self._groups.get(pan_id)
        if group is None:
            return False
        async with group.lock:
            group.consecutive_gateway_failures += 1
            if group.consecutive_gateway_failures < GATEWAY_FAILURE_THRESHOLD:
                # Every individual failure logged, not just the one that
                # eventually triggers promotion -- a real, confirmed gap
                # in earlier debugging this session: without this, only
                # the FINAL failure in a run is ever visible, making it
                # impossible to tell from the logs alone how long trouble
                # had actually been building, or how often it happens
                # without quite reaching the threshold.
                _LOGGER.debug(
                    "Gateway %r for pan_id %#06x failed (%d/%d consecutive)",
                    group.gateway_serial, pan_id,
                    group.consecutive_gateway_failures, GATEWAY_FAILURE_THRESHOLD,
                )
                return False

            await self._promote_away_from_current_gateway(
                group, f"failed {GATEWAY_FAILURE_THRESHOLD} consecutive times",
            )
            return True

    async def record_relay_failure(self, pan_id: int, target_serial: str) -> bool:
        """
        Call when a RELAYED read to target_serial fails, through a
        gateway whose OWN reads are still succeeding -- see
        RELAY_FAILURE_THRESHOLD's own docstring in const.py for the full
        reasoning behind why this exists as a separate mechanism from
        record_gateway_failure() above, and why forcing a different
        gateway is genuinely the best recovery lever available here, not
        just the easiest one.

        Returns True if this triggered a promotion, matching
        record_gateway_failure()'s own return-value convention.
        """
        group = self._groups.get(pan_id)
        if group is None:
            return False
        async with group.lock:
            member = group.members.get(target_serial)
            if member is None:
                return False
            member.consecutive_relay_failures += 1
            if member.consecutive_relay_failures < RELAY_FAILURE_THRESHOLD:
                _LOGGER.debug(
                    "Relay to %s via gateway %r for pan_id %#06x failed (%d/%d consecutive)",
                    target_serial, group.gateway_serial, pan_id,
                    member.consecutive_relay_failures, RELAY_FAILURE_THRESHOLD,
                )
                return False

            await self._promote_away_from_current_gateway(
                group, f"failed to relay to {target_serial!r} {RELAY_FAILURE_THRESHOLD} consecutive times",
            )
            # A fresh start for every member's own relay-failure count,
            # not just target_serial's -- the failure was specific to
            # the OLD gateway's own route, which may not still be
            # relevant at all under the newly-promoted one.
            for other_member in group.members.values():
                other_member.consecutive_relay_failures = 0
            return True

    def record_relay_success(self, pan_id: int, target_serial: str) -> None:
        """Call on every successful RELAYED read -- resets that specific
        target's own consecutive-failure counter, matching
        record_gateway_success()'s own reasoning: a real, successful
        relay means there's no reason to keep counting whatever earlier
        trouble just ended against this target."""
        group = self._groups.get(pan_id)
        if group is not None and target_serial in group.members:
            member = group.members[target_serial]
            if member.consecutive_relay_failures > 0:
                _LOGGER.debug(
                    "Relay to %s via gateway %r for pan_id %#06x recovered after %d "
                    "consecutive failure(s)",
                    target_serial, group.gateway_serial, pan_id, member.consecutive_relay_failures,
                )
            member.consecutive_relay_failures = 0

    def update_mesh_address(self, pan_id: int, serial: str, address: bytes) -> None:
        """Caches a member's Thread mesh-local IPv6 address (see
        coordinator.py's on-demand discovery fallback, and the dedicated
        background prefetch task) -- does nothing if the group or member
        doesn't (yet) exist. Not gated by the group lock: a cached
        address is only ever read during a relay attempt, not during
        gateway selection, so losing a race against a concurrent
        join()/leave() is harmless."""
        group = self._groups.get(pan_id)
        if group is not None and serial in group.members:
            member = group.members[serial]
            if member.mesh_address != address:
                # Only logged on an actual CHANGE -- this is called every
                # poll cycle for every member (coordinator.py's own
                # _fetch(), plus __init__.py's own periodic
                # revalidation), so logging every unchanged confirmation
                # would be pure noise. A None -> known transition
                # specifically is the "device came back" recovery signal
                # worth surfacing -- see the real "Could not determine
                # Thread mesh address" production error this addresses.
                _LOGGER.debug(
                    "Mesh address for %s (pan_id %#06x): %s -> %s",
                    serial, pan_id,
                    _format_mesh_address(member.mesh_address), _format_mesh_address(address),
                )
            member.mesh_address = address

    def update_mesh_last_seen(self, pan_id: int, serial: str, last_seen_at: datetime) -> None:
        """Caches a member's own, freshly-computed "last heard from on
        the mesh" timestamp -- see coordinator.py's own _fetch(), which
        calls this for every peer in one shot on each of the gateway's
        own poll cycles, not per-member. Same reasoning as
        update_mesh_address() for not gating this by the group lock: only
        ever read for display, never during gateway selection itself."""
        group = self._groups.get(pan_id)
        if group is not None and serial in group.members:
            group.members[serial].mesh_last_seen_at = last_seen_at
