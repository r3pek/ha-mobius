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
(move_member()/leave()) triggers a change. Continuously reassigning
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
different tank. Callers (coordinator.py) re-resolve pan_id on every
reconnect and call move_member() if it's changed, which is a
leave-then-join -- including re-running gateway promotion for the group
being left, if the device leaving was that group's gateway.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from homeassistant.core import HomeAssistant

from .const import GATEWAY_ELECTION_SETTLE_SECONDS, GATEWAY_FAILURE_THRESHOLD

if TYPE_CHECKING:
    from .coordinator import MobiusConnectionManager

_LOGGER = logging.getLogger(__name__)


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
    # _best_candidate()'s own handling of that case). A REAL, CONFIRMED
    # production bug lived here before this field existed: promotion
    # always just picked the single best-RSSI member excluding only the
    # one CURRENTLY failing -- for a tank where two devices both have
    # much better RSSI than the other two, that meant failures ping-
    # ponged forever between exactly those two best-RSSI devices (fail,
    # promote the other; it fails too, promote back to the first one,
    # since excluding only "the one failing right now" doesn't stop it
    # being immediately re-eligible) -- the other two members were never
    # tried even once, however many hours this went on for. This set is
    # what actually breaks that cycle: every member gets a real turn
    # before anyone is reconsidered.
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
            if group.gateway_serial is None and not group._electing:
                if prefer_as_gateway:
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
            self._assign_gateway(group, self._best_candidate(group))
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
            if not group.members:
                self._groups.pop(pan_id, None)

    async def move_member(
        self, old_pan_id: int, new_pan_id: int, serial: str, rssi: Optional[int] = None,
    ) -> PanGroup:
        """A device's pan_id changed (it moved to a different tank) --
        leave() the old group (promoting a new gateway there if needed)
        and join() the new one."""
        if old_pan_id == new_pan_id:
            # Not actually a move -- just update the RSSI on file, if any.
            group = self._group_for(new_pan_id)
            async with group.lock:
                if serial in group.members:
                    group.members[serial].rssi = rssi
            return group
        await self.leave(old_pan_id, serial)
        return await self.join(new_pan_id, serial, rssi)

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
            group.consecutive_gateway_failures = 0
            group.recently_failed_gateways.clear()

    async def record_gateway_failure(self, pan_id: int) -> bool:
        """
        Call when the CURRENT gateway's connection/read fails. Returns
        True if this triggered a promotion (the GATEWAY_FAILURE_THRESHOLDth
        consecutive failure), False otherwise -- mainly useful for
        logging/tests; the promotion itself already updates
        group.gateway_serial/gateway_connection, so callers don't need to
        branch on the return value to behave correctly.
        """
        group = self._groups.get(pan_id)
        if group is None:
            return False
        async with group.lock:
            group.consecutive_gateway_failures += 1
            if group.consecutive_gateway_failures < GATEWAY_FAILURE_THRESHOLD:
                return False

            failing_serial = group.gateway_serial
            old_connection = group.gateway_connection
            if failing_serial is not None:
                group.recently_failed_gateways.add(failing_serial)

            new_gateway = self._best_candidate(group, exclude_serials=group.recently_failed_gateways)
            if new_gateway is None:
                # Every member has now failed at least once since the
                # last success (see PanGroup's own docstring) -- rather
                # than getting stuck with nothing left to promote at
                # all, give everyone a clean slate and try again,
                # excluding only the one that JUST failed (no sense
                # immediately re-picking that one specifically, but
                # everyone else deserves a fresh look after a full
                # round).
                group.recently_failed_gateways = {failing_serial} if failing_serial is not None else set()
                new_gateway = self._best_candidate(group, exclude_serials=group.recently_failed_gateways)

            self._assign_gateway(group, new_gateway)
            _LOGGER.warning(
                "Gateway %r for pan_id %#06x failed %d consecutive times; promoted %r",
                failing_serial, pan_id, GATEWAY_FAILURE_THRESHOLD, new_gateway,
            )
            if old_connection is not None:
                await old_connection.disconnect()
            return True

    def update_rssi(self, pan_id: int, serial: str, rssi: Optional[int]) -> None:
        """Best-effort RSSI update for an existing member -- does nothing
        if the group or member doesn't (yet) exist. Not itself gated by
        the group lock: a slightly stale RSSI read losing a race against
        a concurrent join()/leave() is harmless (RSSI is only consulted
        during gateway selection, which does hold the lock)."""
        group = self._groups.get(pan_id)
        if group is not None and serial in group.members:
            group.members[serial].rssi = rssi

    def update_mesh_address(self, pan_id: int, serial: str, address: bytes) -> None:
        """Caches a member's Thread mesh-local IPv6 address (see
        coordinator.py's on-demand discovery fallback, and the dedicated
        background prefetch task) -- does nothing if the group or member
        doesn't (yet) exist. Not gated by the group lock, matching
        update_rssi()'s reasoning: a cached address is only ever read
        during a relay attempt, not during gateway selection, so losing a
        race against a concurrent join()/leave() is harmless."""
        group = self._groups.get(pan_id)
        if group is not None and serial in group.members:
            group.members[serial].mesh_address = address

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
