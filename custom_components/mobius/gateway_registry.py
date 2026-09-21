"""
Per-pan_id gateway registry.

Devices with the same pan_id (Thread mesh, "tank") share one BLE
connection. One member of each group is the gateway and owns the connection
(a MobiusConnectionManager); the other members are reached through it with
RelayedMobiusDevice (see coordinator.py). This module tracks group
membership and which member is the gateway.

## Gateway selection

A new group waits GATEWAY_ELECTION_SETTLE_SECONDS for members to join and
then picks the one with the best RSSI. A member that joins later never
replaces a working gateway; only GATEWAY_FAILURE_THRESHOLD consecutive
gateway failures, RELAY_FAILURE_THRESHOLD failed relays to one target, or
the gateway leaving the group change it.

## Failover

A failing gateway is replaced by the best-RSSI member that hasn't failed as
gateway in the current round (recently_failed_gateways), and becomes a
relayed member. When every member has failed once, a new round starts. A
group with no other member is left without a gateway.

## Moving between tanks

A device's pan_id can change when it is moved to another tank.
__init__.py's periodic tank revalidation detects this and moves the device
between config entries; reloading those entries calls leave() and join().
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from homeassistant.core import HomeAssistant

from mobius import format_mesh_address

from .const import GATEWAY_ELECTION_SETTLE_SECONDS, GATEWAY_FAILURE_THRESHOLD, RELAY_FAILURE_THRESHOLD

if TYPE_CHECKING:
    from .coordinator import MobiusConnectionManager

_LOGGER = logging.getLogger(__name__)


def _log_address(address: Optional[bytes]) -> str:
    return format_mesh_address(address) or "unknown"


@dataclass
class MemberState:
    """One device's membership in a PanGroup."""
    serial: str
    rssi: Optional[int] = None
    # Mesh-local IPv6 address, needed to relay to this device.
    mesh_address: Optional[bytes] = None
    # When the gateway last heard from this device on the mesh, updated on
    # every gateway poll.
    mesh_last_seen_at: Optional[datetime] = None
    # Consecutive failed relayed reads to this device (see
    # GatewayRegistry.record_relay_failure()).
    consecutive_relay_failures: int = 0


@dataclass
class PanGroup:
    """Gateway state of one pan_id."""
    pan_id: int
    gateway_serial: Optional[str] = None
    gateway_connection: Optional["MobiusConnectionManager"] = None
    members: dict[str, MemberState] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Consecutive failed reads of the current gateway; reset when the
    # gateway changes.
    consecutive_gateway_failures: int = 0
    # Members that failed as gateway in the current round. Excluded from
    # promotion so every member gets a turn instead of the two best-RSSI
    # members alternating. Only reset once every member has failed (see
    # _promote_away_from_current_gateway()), not on a success.
    recently_failed_gateways: set[str] = field(default_factory=set)
    # Incremented on every gateway assignment. A fetch records the value
    # when it starts; a failure reported under an older generation belongs
    # to a gateway that has already been replaced and is ignored.
    generation: int = 0
    _electing: bool = False
    _gateway_elected: asyncio.Event = field(default_factory=asyncio.Event)

    def member_rssi_items(self, exclude_serials: Optional[set[str]] = None):
        exclude_serials = exclude_serials or set()
        return [
            (serial, m.rssi) for serial, m in self.members.items()
            if serial not in exclude_serials
        ]

    def serial_for_mesh_suffix(self, suffix: bytes) -> Optional[str]:
        """The member whose mesh_address ends with `suffix` (the
        Sync/EcoSmartBack Master parameter holds the last 8 bytes of the
        parent pump's mesh address), or None if unknown."""
        for serial, member in self.members.items():
            if member.mesh_address is not None and member.mesh_address[-len(suffix):] == suffix:
                return serial
        return None


class GatewayRegistry:
    """One instance per Home Assistant instance (in hass.data), tracking every
    PanGroup. See the module docstring.
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
        """The group for `pan_id`, or None if nobody joined it (or its last
        member left).
        """
        return self._groups.get(pan_id)

    async def join(
        self, pan_id: int, serial: str, rssi: Optional[int] = None,
        prefer_as_gateway: bool = False,
    ) -> PanGroup:
        """Adds `serial` to the group of `pan_id`, creating the group if needed, and
        returns it once it has a gateway (waiting for the election of a new
        group).

        prefer_as_gateway: the caller just connected to this device (e.g. the
        config flow ran discover_tank() on it), so a new group makes it gateway
        right away instead of running the RSSI election. Ignored for a group that
        already has a gateway or an election in progress.
        """
        group = self._group_for(pan_id)
        async with group.lock:
            existing = group.members.get(serial)
            if existing is not None:
                # Keep the existing MemberState (and its cached mesh address) across
                # rejoins, e.g. after a restart.
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
                return
            winner = self._best_candidate(group)
            _LOGGER.debug(
                "Gateway election for pan_id %#06x settled: %r elected from %s",
                group.pan_id, winner, dict(group.member_rssi_items()),
            )
            self._assign_gateway(group, winner)
            group._gateway_elected.set()

    def _best_candidate(self, group: PanGroup, exclude_serials: Optional[set[str]] = None) -> Optional[str]:
        """The member with the highest known RSSI (excluding exclude_serials), or
        the first member if none has an RSSI.
        """
        candidates = group.member_rssi_items(exclude_serials=exclude_serials)
        if not candidates:
            return None
        with_rssi = [c for c in candidates if c[1] is not None]
        if with_rssi:
            return max(with_rssi, key=lambda c: c[1])[0]
        return candidates[0][0]

    def _assign_gateway(self, group: PanGroup, serial: Optional[str]) -> None:
        """Makes `serial` the gateway (None clears it), with a new connection
        manager and a reset failure counter, and increments group.generation even
        if the serial didn't change. Must be called with group.lock held.
        """
        # Imported here: coordinator.py imports this module.
        from .coordinator import MobiusConnectionManager

        group.gateway_serial = serial
        group.consecutive_gateway_failures = 0
        group.generation += 1
        group.gateway_connection = (
            MobiusConnectionManager(self.hass, serial, self.semaphore)
            if serial is not None else None
        )

    async def leave(self, pan_id: int, serial: str) -> None:
        """Removes `serial` from its group. If it was the gateway, the best
        remaining candidate becomes gateway (or none if no member is left). An
        empty group is removed.
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
        """Resets the gateway's consecutive-failure counter.
        recently_failed_gateways is kept (see PanGroup).
        """
        group = self._groups.get(pan_id)
        if group is not None:
            if group.consecutive_gateway_failures > 0:
                _LOGGER.debug(
                    "Gateway %r for pan_id %#06x recovered after %d consecutive failure(s)",
                    group.gateway_serial, pan_id, group.consecutive_gateway_failures,
                )
            group.consecutive_gateway_failures = 0

    async def _promote_away_from_current_gateway(self, group: PanGroup, reason: str) -> Optional[str]:
        """Replaces the current gateway (adding it to recently_failed_gateways) with
        the best candidate that hasn't failed this round, and disconnects the old
        connection. When every member has failed, a new round starts that
        excludes only the gateway that just failed. Must be called with
        group.lock held.
        """
        failing_serial = group.gateway_serial
        old_connection = group.gateway_connection
        if failing_serial is not None:
            group.recently_failed_gateways.add(failing_serial)

        new_gateway = self._best_candidate(group, exclude_serials=group.recently_failed_gateways)
        if new_gateway is None:
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

    async def record_gateway_failure(self, pan_id: int, expected_generation: int) -> bool:
        """Records a failed read of the gateway itself. After
        GATEWAY_FAILURE_THRESHOLD consecutive failures another member is promoted
        and True is returned.

        expected_generation is the group.generation captured when the failing
        fetch started; if the gateway has changed since, the failure is ignored
        (returns False).
        """
        group = self._groups.get(pan_id)
        if group is None:
            return False
        async with group.lock:
            if group.generation != expected_generation:
                _LOGGER.debug(
                    "Ignoring stale gateway failure for pan_id %#06x -- the group has "
                    "already moved on to generation %d (this failure was from generation "
                    "%d, whatever gateway was current back then)",
                    pan_id, group.generation, expected_generation,
                )
                return False
            group.consecutive_gateway_failures += 1
            if group.consecutive_gateway_failures < GATEWAY_FAILURE_THRESHOLD:
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

    async def record_relay_failure(self, pan_id: int, target_serial: str, expected_generation: int) -> bool:
        """Records a failed relayed read to `target_serial` through a gateway whose
        own reads succeed. After RELAY_FAILURE_THRESHOLD consecutive failures for
        the same target, another gateway is promoted (it may have a working route)
        and every member's relay failure count is reset. Returns True when a
        promotion happened. `expected_generation` as in record_gateway_failure().
        """
        group = self._groups.get(pan_id)
        if group is None:
            return False
        async with group.lock:
            if group.generation != expected_generation:
                _LOGGER.debug(
                    "Ignoring stale relay failure (target %s) for pan_id %#06x -- the "
                    "group has already moved on to generation %d (this failure was from "
                    "generation %d, whatever gateway was current back then)",
                    target_serial, pan_id, group.generation, expected_generation,
                )
                return False
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
            for other_member in group.members.values():
                other_member.consecutive_relay_failures = 0
            return True

    def record_relay_success(self, pan_id: int, target_serial: str) -> None:
        """Resets the relay failure count of `target_serial`."""
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
        """Stores a member's mesh-local address. Does nothing for an unknown group
        or member. Not locked: the address isn't used by gateway selection.
        """
        group = self._groups.get(pan_id)
        if group is not None and serial in group.members:
            member = group.members[serial]
            if member.mesh_address != address:
                _LOGGER.debug(
                    "Mesh address for %s (pan_id %#06x): %s -> %s",
                    serial, pan_id,
                    _log_address(member.mesh_address), _log_address(address),
                )
            member.mesh_address = address

    def update_mesh_last_seen(self, pan_id: int, serial: str, last_seen_at: datetime) -> None:
        """Stores when a member was last heard from on the mesh (see
        coordinator.py's _refresh_mesh_last_seen()). Not locked, like
        update_mesh_address().
        """
        group = self._groups.get(pan_id)
        if group is not None and serial in group.members:
            group.members[serial].mesh_last_seen_at = last_seen_at
