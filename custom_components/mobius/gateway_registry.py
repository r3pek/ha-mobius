"""
Shared per-pan_id gateway registry.

Multiple physical devices sharing the same pan_id (Thread mesh/"tank",
confirmed via Tank/CommGroup in the decompiled app -- see python-mobius's
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
    _electing: bool = False
    _gateway_elected: asyncio.Event = field(default_factory=asyncio.Event)

    def member_rssi_items(self, exclude_serial: Optional[str] = None):
        return [
            (serial, m.rssi) for serial, m in self.members.items()
            if serial != exclude_serial
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

    async def join(self, pan_id: int, serial: str, rssi: Optional[int] = None) -> PanGroup:
        """
        Registers `serial` as a member of `pan_id`'s group, creating the
        group if it doesn't exist yet. Always returns a group with
        gateway_serial/gateway_connection populated -- waits for
        election to complete if this call triggered (or arrived during)
        a brand-new group's settle window.
        """
        group = self._group_for(pan_id)
        async with group.lock:
            group.members[serial] = MemberState(serial=serial, rssi=rssi)
            if group.gateway_serial is None and not group._electing:
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

    def _best_candidate(self, group: PanGroup, exclude_serial: Optional[str] = None) -> Optional[str]:
        """Highest known RSSI among current members (excluding
        exclude_serial); falls back to "first available" (dict insertion
        order) if no member has RSSI info at all."""
        candidates = group.member_rssi_items(exclude_serial=exclude_serial)
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
                new_gateway = self._best_candidate(group)
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
        consecutive-failure counter."""
        group = self._groups.get(pan_id)
        if group is not None:
            group.consecutive_gateway_failures = 0

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
            new_gateway = self._best_candidate(group, exclude_serial=failing_serial)
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
