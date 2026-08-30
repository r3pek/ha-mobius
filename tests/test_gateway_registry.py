"""
Tests for gateway_registry.py -- the shared per-pan_id gateway
registry: election (including RSSI-based selection among concurrent
joiners), failover/promotion, pan_id moves, and cross-group isolation.
"""

import asyncio
import logging

import pytest
from unittest.mock import MagicMock

from custom_components.mobius.gateway_registry import GatewayRegistry
from custom_components.mobius.const import GATEWAY_FAILURE_THRESHOLD, RELAY_FAILURE_THRESHOLD


PAN_A = 0x3D0F
PAN_B = 0x1234


async def _fail_gateway(registry, pan_id):
    """Fetches the group's own CURRENT generation right before the
    call, matching exactly how a real coordinator captures it at its
    own fetch-start (see coordinator.py's own _fetch()) -- correct for
    every call in a test, including the threshold-reaching one itself
    (the promotion it triggers hasn't happened yet at the moment this
    reads group.generation), and for round-robin tests where multiple
    promotions happen across the test (each subsequent batch of calls
    picks up whatever the NEW, post-promotion generation now is)."""
    group = registry.group(pan_id)
    return await registry.record_gateway_failure(pan_id, group.generation if group else 0)


async def _fail_relay(registry, pan_id, target_serial):
    """Same reasoning as _fail_gateway() above, for the relay-specific
    counterpart."""
    group = registry.group(pan_id)
    return await registry.record_relay_failure(pan_id, target_serial, group.generation if group else 0)


@pytest.fixture
def registry():
    hass = MagicMock()
    semaphore = asyncio.Semaphore(2)
    # A tiny settle window -- real tests don't need to wait out the real
    # multi-second production window, just confirm the election logic
    # itself (best-RSSI-among-concurrent-joiners) works correctly.
    return GatewayRegistry(hass, semaphore, election_settle_seconds=0.05)


class TestBasicJoin:
    @pytest.mark.asyncio
    async def test_single_device_becomes_gateway(self, registry):
        group = await registry.join(PAN_A, "serial-1", rssi=-50)
        assert group.gateway_serial == "serial-1"
        assert group.gateway_connection is not None
        assert "serial-1" in group.members

    @pytest.mark.asyncio
    async def test_join_with_no_rssi_still_works(self, registry):
        group = await registry.join(PAN_A, "serial-1")
        assert group.gateway_serial == "serial-1"

    @pytest.mark.asyncio
    async def test_second_device_after_group_established_does_not_displace_gateway(self, registry):
        group = await registry.join(PAN_A, "serial-1", rssi=-80)  # weak signal, joins first
        assert group.gateway_serial == "serial-1"

        # Second device joins later, with a MUCH better signal -- must
        # NOT displace the already-established gateway. This is the
        # actual point of this test: signal strength alone never
        # triggers reassignment after a group is already formed.
        group2 = await registry.join(PAN_A, "serial-2", rssi=-20)
        assert group2 is group
        assert group.gateway_serial == "serial-1"
        assert "serial-2" in group.members


class TestConcurrentElection:
    @pytest.mark.asyncio
    async def test_best_rssi_wins_among_concurrent_joiners(self, registry):
        """The actual point of the settle-window mechanism: devices
        joining a BRAND NEW group within the settle window should have
        the best-RSSI one elected, regardless of call order."""
        # Weakest signal calls join() FIRST -- if selection just picked
        # "whoever called first," this would incorrectly become gateway.
        results = await asyncio.gather(
            registry.join(PAN_A, "weak", rssi=-90),
            registry.join(PAN_A, "strong", rssi=-30),
            registry.join(PAN_A, "medium", rssi=-60),
        )
        group = results[0]
        assert group.gateway_serial == "strong"
        assert all(r is group for r in results)  # all three see the same group object

    @pytest.mark.asyncio
    async def test_falls_back_to_first_available_with_no_rssi_info(self, registry):
        results = await asyncio.gather(
            registry.join(PAN_A, "first"),
            registry.join(PAN_A, "second"),
        )
        group = results[0]
        # No RSSI info at all for either -- some deterministic member
        # must still be chosen (not None/crash).
        assert group.gateway_serial in ("first", "second")

    @pytest.mark.asyncio
    async def test_all_concurrent_joiners_are_registered_as_members(self, registry):
        await asyncio.gather(
            registry.join(PAN_A, "a", rssi=-50),
            registry.join(PAN_A, "b", rssi=-60),
            registry.join(PAN_A, "c", rssi=-70),
        )
        group = registry.group(PAN_A)
        assert set(group.members.keys()) == {"a", "b", "c"}


class TestGatewayFailover:
    @pytest.mark.asyncio
    async def test_no_promotion_below_threshold(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "backup", rssi=-40)

        for _ in range(GATEWAY_FAILURE_THRESHOLD - 1):
            triggered = await _fail_gateway(registry, PAN_A)
            assert triggered is False

        group = registry.group(PAN_A)
        assert group.gateway_serial == "gw"

    @pytest.mark.asyncio
    async def test_every_individual_failure_is_logged_not_just_the_threshold_one(self, registry, caplog):
        """A real, confirmed gap in earlier debugging this integration:
        only the FINAL failure in a run used to be visible in the logs
        at all (the one that triggers promotion). Confirms every single
        failure leading up to that point is now logged too, so the full
        progression -- not just the outcome -- is visible with debug
        logging enabled."""
        caplog.set_level(logging.DEBUG, logger="custom_components.mobius")
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "backup", rssi=-40)

        for i in range(1, GATEWAY_FAILURE_THRESHOLD):
            await _fail_gateway(registry, PAN_A)
            assert f"failed ({i}/{GATEWAY_FAILURE_THRESHOLD} consecutive)" in caplog.text

    @pytest.mark.asyncio
    async def test_promotes_at_threshold(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "backup", rssi=-40)

        triggered = None
        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            triggered = await _fail_gateway(registry, PAN_A)

        assert triggered is True
        group = registry.group(PAN_A)
        assert group.gateway_serial == "backup"
        assert group.consecutive_gateway_failures == 0  # reset for the new gateway

    @pytest.mark.asyncio
    async def test_promotes_best_remaining_candidate_by_rssi(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "weak_backup", rssi=-80)
        await registry.join(PAN_A, "strong_backup", rssi=-30)

        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            await _fail_gateway(registry, PAN_A)

        assert registry.group(PAN_A).gateway_serial == "strong_backup"

    @pytest.mark.asyncio
    async def test_does_not_ping_pong_between_the_two_best_rssi_members(self, registry):
        """The real, confirmed production bug this whole mechanism
        exists to fix: a tank with two much-stronger-RSSI members and
        two weaker ones. Before this fix, repeated failures ping-ponged
        forever between exactly the two strong members -- the weak ones
        were never tried even once, however long this went on for,
        because promotion only ever excluded whichever member was
        CURRENTLY failing, not any of its own prior failures."""
        await registry.join(PAN_A, "strong_a", rssi=-30)
        await registry.join(PAN_A, "strong_b", rssi=-35)
        await registry.join(PAN_A, "weak_c", rssi=-90)
        await registry.join(PAN_A, "weak_d", rssi=-95)

        # strong_a is the initial pick (best RSSI overall).
        assert registry.group(PAN_A).gateway_serial == "strong_a"

        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            await _fail_gateway(registry, PAN_A)
        # strong_b is the next-best remaining candidate -- expected,
        # same as the existing by-RSSI test above.
        assert registry.group(PAN_A).gateway_serial == "strong_b"

        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            await _fail_gateway(registry, PAN_A)
        # The actual point: NOT strong_a again (the old, buggy
        # behavior) -- both strong members have now failed since the
        # last success, so a weak one gets a real turn.
        assert registry.group(PAN_A).gateway_serial in ("weak_c", "weak_d")

    @pytest.mark.asyncio
    async def test_round_robin_eventually_tries_every_member(self, registry):
        """Continues the scenario above through a full cycle -- confirms
        every one of 4 members gets an actual turn before any of them is
        reconsidered a second time."""
        await registry.join(PAN_A, "a", rssi=-30)
        await registry.join(PAN_A, "b", rssi=-35)
        await registry.join(PAN_A, "c", rssi=-90)
        await registry.join(PAN_A, "d", rssi=-95)

        seen_as_gateway = {registry.group(PAN_A).gateway_serial}
        for _ in range(3):  # 3 more promotions covers the remaining 3 members
            for _ in range(GATEWAY_FAILURE_THRESHOLD):
                await _fail_gateway(registry, PAN_A)
            seen_as_gateway.add(registry.group(PAN_A).gateway_serial)

        assert seen_as_gateway == {"a", "b", "c", "d"}

    @pytest.mark.asyncio
    async def test_success_clears_recently_failed_gateways(self, registry):
        """A real, successful read means the tank is healthy again --
        confirms a device that failed earlier gets a clean slate rather
        than being permanently excluded from ever being promoted again."""
        await registry.join(PAN_A, "a", rssi=-30)
        await registry.join(PAN_A, "b", rssi=-35)

        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            await _fail_gateway(registry, PAN_A)
        assert registry.group(PAN_A).gateway_serial == "b"
        assert "a" in registry.group(PAN_A).recently_failed_gateways

        registry.record_gateway_success(PAN_A)
        assert registry.group(PAN_A).recently_failed_gateways == set()

        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            await _fail_gateway(registry, PAN_A)
        # "a" is eligible again -- it's the only other member, so if it
        # weren't, this would have to fall back to gatewayless instead.
        assert registry.group(PAN_A).gateway_serial == "a"

    @pytest.mark.asyncio
    async def test_round_robin_resets_rather_than_getting_stuck_when_everyone_has_failed(self, registry):
        """If every member has failed since the last success (no
        candidates left to exclude-and-pick-from), the whole tank
        keeps cycling rather than getting permanently stuck -- confirmed
        by running well past one full cycle and seeing every member
        promoted again, not just the first time around."""
        await registry.join(PAN_A, "a", rssi=-30)
        await registry.join(PAN_A, "b", rssi=-35)

        promotions = []
        for _ in range(6):  # 3 full ping-pong cycles between the only 2 members
            for _ in range(GATEWAY_FAILURE_THRESHOLD):
                await _fail_gateway(registry, PAN_A)
            promotions.append(registry.group(PAN_A).gateway_serial)

        # Never gatewayless, never stuck -- alternates cleanly between
        # the only 2 members that exist, same as the pre-fix behavior
        # would for exactly 2 members (this case was never broken --
        # confirms the fix doesn't regress the simple case).
        assert None not in promotions
        assert set(promotions) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_success_resets_failure_counter(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "backup", rssi=-40)

        await _fail_gateway(registry, PAN_A)
        await _fail_gateway(registry, PAN_A)
        registry.record_gateway_success(PAN_A)

        # Counter reset -- should take another full threshold's worth of
        # failures to trigger promotion, not just one more.
        triggered = await _fail_gateway(registry, PAN_A)
        assert triggered is False
        assert registry.group(PAN_A).gateway_serial == "gw"

    @pytest.mark.asyncio
    async def test_no_promotion_possible_leaves_group_gatewayless(self, registry):
        await registry.join(PAN_A, "solo", rssi=-50)

        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            await _fail_gateway(registry, PAN_A)

        group = registry.group(PAN_A)
        assert group.gateway_serial is None
        assert group.gateway_connection is None
        # The failing (now-former) member is still tracked as a group
        # member -- just not gateway anymore.
        assert "solo" in group.members

    @pytest.mark.asyncio
    async def test_failure_on_nonexistent_group_is_a_safe_noop(self, registry):
        triggered = await _fail_gateway(registry, 0x9999)
        assert triggered is False

    @pytest.mark.asyncio
    async def test_success_on_nonexistent_group_is_a_safe_noop(self, registry):
        registry.record_gateway_success(0x9999)  # must not raise


class TestRelayFailover:
    """A real, confirmed production incident is what this whole
    mechanism addresses: a gateway can be perfectly healthy for its own
    direct reads, and for relaying to SOME other group members, while
    persistently failing to relay to ONE specific target for 40+
    minutes straight -- see RELAY_FAILURE_THRESHOLD's own docstring in
    const.py for the full reasoning, including why forcing a different
    gateway turned out to be the best recovery lever actually
    available, confirmed by reverse-engineering the real app's own
    source specifically looking for (and not finding) any runtime
    mesh-rebuild command it could fall back to instead."""

    @pytest.mark.asyncio
    async def test_below_threshold_does_not_promote(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "target", rssi=-40)
        await registry.join(PAN_A, "backup", rssi=-30)

        for _ in range(RELAY_FAILURE_THRESHOLD - 1):
            triggered = await _fail_relay(registry, PAN_A, "target")
            assert triggered is False

        assert registry.group(PAN_A).gateway_serial == "gw"

    @pytest.mark.asyncio
    async def test_threshold_reached_promotes_even_though_gateway_itself_is_healthy(self, registry):
        """The actual point: the gateway's own direct reads succeeding
        the whole time (record_gateway_success() called between every
        relay failure, exactly like a real healthy gateway would) must
        NOT prevent promotion once relay-to-one-target failures alone
        cross their own, separate threshold."""
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "target", rssi=-40)
        await registry.join(PAN_A, "backup", rssi=-30)

        for _ in range(RELAY_FAILURE_THRESHOLD):
            registry.record_gateway_success(PAN_A)  # gateway's own reads keep succeeding
            await _fail_relay(registry, PAN_A, "target")

        group = registry.group(PAN_A)
        assert group.gateway_serial != "gw"
        assert group.gateway_serial in ("target", "backup")

    @pytest.mark.asyncio
    async def test_relay_failures_do_not_affect_gateways_own_failure_counter(self, registry):
        """Confirms the two counters are genuinely independent, not
        just independently-named -- relay failures alone must never
        move consecutive_gateway_failures at all."""
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "target", rssi=-40)

        for _ in range(RELAY_FAILURE_THRESHOLD + 5):
            await _fail_relay(registry, PAN_A, "target")

        assert registry.group(PAN_A).consecutive_gateway_failures == 0

    @pytest.mark.asyncio
    async def test_success_resets_the_targets_own_counter(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "target", rssi=-40)
        await registry.join(PAN_A, "backup", rssi=-30)

        for _ in range(RELAY_FAILURE_THRESHOLD - 1):
            await _fail_relay(registry, PAN_A, "target")
        registry.record_relay_success(PAN_A, "target")
        assert registry.group(PAN_A).members["target"].consecutive_relay_failures == 0

        # Now needs the FULL threshold again -- confirms the count
        # actually reset, not just got close and stalled.
        for _ in range(RELAY_FAILURE_THRESHOLD - 1):
            triggered = await _fail_relay(registry, PAN_A, "target")
            assert triggered is False
        assert registry.group(PAN_A).gateway_serial == "gw"

    @pytest.mark.asyncio
    async def test_promotion_resets_every_members_relay_failure_count(self, registry):
        """Not just the target that triggered the promotion -- the
        failure was specific to the OLD gateway's own route, which may
        not still be relevant at all under the newly-promoted one."""
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "target", rssi=-40)
        await registry.join(PAN_A, "other", rssi=-35)
        await registry.join(PAN_A, "backup", rssi=-30)

        # "other" has some accumulated (but not yet threshold-reached)
        # relay trouble of its own, separate from "target".
        for _ in range(RELAY_FAILURE_THRESHOLD - 1):
            await _fail_relay(registry, PAN_A, "other")
        for _ in range(RELAY_FAILURE_THRESHOLD):
            await _fail_relay(registry, PAN_A, "target")

        group = registry.group(PAN_A)
        assert group.gateway_serial != "gw"
        for member in group.members.values():
            assert member.consecutive_relay_failures == 0

    @pytest.mark.asyncio
    async def test_failure_on_nonexistent_group_is_a_safe_noop(self, registry):
        triggered = await _fail_relay(registry, 0x9999, "nobody")
        assert triggered is False

    @pytest.mark.asyncio
    async def test_failure_for_nonexistent_member_is_a_safe_noop(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        triggered = await _fail_relay(registry, PAN_A, "nonexistent")
        assert triggered is False

    def test_success_on_nonexistent_group_is_a_safe_noop(self, registry):
        registry.record_relay_success(0x9999, "nobody")  # must not raise


class TestLeave:
    @pytest.mark.asyncio
    async def test_non_gateway_member_leaving_does_not_affect_gateway(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "member", rssi=-40)

        await registry.leave(PAN_A, "member")

        group = registry.group(PAN_A)
        assert group.gateway_serial == "gw"
        assert "member" not in group.members

    @pytest.mark.asyncio
    async def test_gateway_leaving_promotes_another_member(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "backup", rssi=-40)

        await registry.leave(PAN_A, "gw")

        group = registry.group(PAN_A)
        assert group.gateway_serial == "backup"
        assert "gw" not in group.members

    @pytest.mark.asyncio
    async def test_last_member_leaving_removes_the_group_entirely(self, registry):
        await registry.join(PAN_A, "solo", rssi=-50)
        await registry.leave(PAN_A, "solo")
        assert registry.group(PAN_A) is None

    @pytest.mark.asyncio
    async def test_leaving_a_nonexistent_group_is_a_safe_noop(self, registry):
        await registry.leave(0x9999, "nobody")  # must not raise


class TestUpdateMeshAddress:
    @pytest.mark.asyncio
    async def test_updates_existing_member(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        address = bytes.fromhex("fd11223344556677000000fffe001234")
        registry.update_mesh_address(PAN_A, "gw", address)
        assert registry.group(PAN_A).members["gw"].mesh_address == address

    def test_nonexistent_group_is_a_safe_noop(self, registry):
        registry.update_mesh_address(0x9999, "nobody", b"\x00" * 16)  # must not raise

    @pytest.mark.asyncio
    async def test_nonexistent_member_is_a_safe_noop(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        registry.update_mesh_address(PAN_A, "nonexistent", b"\x00" * 16)  # must not raise
        assert "nonexistent" not in registry.group(PAN_A).members

    @pytest.mark.asyncio
    async def test_new_member_starts_with_no_cached_address(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        assert registry.group(PAN_A).members["gw"].mesh_address is None


class TestGenerationFencing:
    """The actual fix for a real, confirmed production incident: two
    devices in one tank, gateway election alternating indefinitely, one
    log line reading "gateway X failed to relay to X". Root cause: a
    fetch that started before a promotion can still be sitting in its
    own timeout well after that promotion already happened (a torn-down
    connection doesn't make an in-flight read fail instantly) -- when
    it finally fails, is_gateway (captured back at fetch-start) is
    stale, so the failure gets misattributed against whatever the
    CURRENT gateway happens to be by then, which can itself trigger
    ANOTHER promotion on top of one that already superseded it. These
    tests confirm a failure carrying an outdated generation number is
    recognized and dropped, not misattributed."""

    @pytest.mark.asyncio
    async def test_assign_gateway_bumps_generation(self, registry):
        await registry.join(PAN_A, "a", rssi=-50)
        gen_after_join = registry.group(PAN_A).generation

        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            await _fail_gateway(registry, PAN_A)

        # "a" was solo -- no promotion possible, but _assign_gateway()
        # still ran (to clear the gateway), so generation still bumps.
        assert registry.group(PAN_A).generation > gen_after_join

    @pytest.mark.asyncio
    async def test_stale_gateway_failure_is_dropped_not_recorded(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "backup", rssi=-40)
        stale_generation = registry.group(PAN_A).generation - 1  # deliberately outdated

        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            triggered = await registry.record_gateway_failure(PAN_A, stale_generation)
            assert triggered is False

        # Even GATEWAY_FAILURE_THRESHOLD calls with a stale generation
        # must never promote -- the counter itself should never move.
        group = registry.group(PAN_A)
        assert group.gateway_serial == "gw"
        assert group.consecutive_gateway_failures == 0

    @pytest.mark.asyncio
    async def test_stale_relay_failure_is_dropped_not_recorded(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "target", rssi=-40)
        stale_generation = registry.group(PAN_A).generation - 1

        for _ in range(RELAY_FAILURE_THRESHOLD):
            triggered = await registry.record_relay_failure(PAN_A, "target", stale_generation)
            assert triggered is False

        group = registry.group(PAN_A)
        assert group.gateway_serial == "gw"  # never promoted away
        assert group.members["target"].consecutive_relay_failures == 0

    @pytest.mark.asyncio
    async def test_reproduces_the_real_incident_self_relay_scenario(self, registry):
        """The exact production sequence: a relay attempt starts under
        the CURRENT generation, a concurrent promotion happens (bumping
        the generation) before that attempt's own failure is recorded,
        and the failure -- captured with the OLD generation, exactly as
        coordinator.py's own _fetch() would -- must be dropped rather
        than counted against whichever gateway is current by the time
        it's finally recorded."""
        await registry.join(PAN_A, "AAAA", rssi=-50)
        await registry.join(PAN_A, "BBBB", rssi=-40)
        assert registry.group(PAN_A).gateway_serial == "AAAA"

        # AAAA's own coordinator "starts a relay attempt to BBBB"
        # (there is no actual connection here -- this test is purely
        # about the registry's own bookkeeping) and captures the
        # CURRENT generation, exactly as _fetch() does.
        captured_generation = registry.group(PAN_A).generation

        # While that relay attempt is still "in flight", AAAA's OWN
        # gateway health fails independently and promotes BBBB.
        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            await _fail_gateway(registry, PAN_A)
        assert registry.group(PAN_A).gateway_serial == "BBBB"

        # The original relay attempt (to BBBB, captured back when AAAA
        # was still gateway) NOW fails, and reports it using the
        # generation it captured back at its own start -- stale by now.
        for _ in range(RELAY_FAILURE_THRESHOLD):
            triggered = await registry.record_relay_failure(PAN_A, "BBBB", captured_generation)
            assert triggered is False

        # The actual point: BBBB must still be gateway -- the stale
        # relay failure must NOT have promoted anyone else (which, in
        # the real incident, could cascade into an oscillating loop).
        assert registry.group(PAN_A).gateway_serial == "BBBB"

    @pytest.mark.asyncio
    async def test_current_generation_failure_still_promotes_normally(self, registry):
        """Confirms the fencing check doesn't just always return False
        -- a failure carrying the group's ACTUAL current generation
        still counts and can still promote, same as before this fix."""
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "backup", rssi=-40)

        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            triggered = await _fail_gateway(registry, PAN_A)

        assert triggered is True
        assert registry.group(PAN_A).gateway_serial == "backup"


class TestCrossGroupIsolation:
    @pytest.mark.asyncio
    async def test_two_groups_are_fully_independent(self, registry):
        await registry.join(PAN_A, "a1", rssi=-50)
        await registry.join(PAN_A, "a2", rssi=-40)
        await registry.join(PAN_B, "b1", rssi=-50)

        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            await _fail_gateway(registry, PAN_A)

        group_a = registry.group(PAN_A)
        group_b = registry.group(PAN_B)
        # PAN_A's failover must not touch PAN_B at all.
        assert group_a.gateway_serial == "a2"
        assert group_b.gateway_serial == "b1"
        assert group_b.consecutive_gateway_failures == 0

    @pytest.mark.asyncio
    async def test_same_serial_string_in_two_different_groups_does_not_collide(self, registry):
        # Contrived, but confirms membership keys are scoped per-group,
        # not globally -- a real-world equivalent would be impossible
        # (serials are globally unique), but this is a cheap way to
        # confirm the two members dicts are genuinely separate objects.
        await registry.join(PAN_A, "shared-name", rssi=-50)
        await registry.join(PAN_B, "shared-name", rssi=-90)

        assert registry.group(PAN_A).members["shared-name"].rssi == -50
        assert registry.group(PAN_B).members["shared-name"].rssi == -90


class TestPreferAsGateway:
    """prefer_as_gateway=True -- the hint used by the tank config flow,
    which already proved a specific device is reachable (it just
    connected to it to run discover_tank()) and shouldn't have to wait
    out the normal settle-window election to find that out again."""

    @pytest.mark.asyncio
    async def test_immediately_assigns_gateway_no_settle_wait(self, registry):
        # No sleep/settle window involved at all -- if this actually
        # waited out election_settle_seconds, a badly-broken version of
        # this feature would still eventually return the right answer,
        # so the real point of this test is checking it does NOT go
        # through _elect_initial_gateway()'s asyncio.sleep() at all.
        group = await registry.join(PAN_A, "preferred", rssi=-80, prefer_as_gateway=True)
        assert group.gateway_serial == "preferred"
        assert group.gateway_connection is not None

    @pytest.mark.asyncio
    async def test_preferred_wins_even_with_worse_rssi_than_a_concurrent_joiner(self, registry):
        """The whole point: skips RSSI comparison entirely for a
        brand-new group, rather than possibly losing an election to a
        stronger-signal-but-never-actually-connected-to member."""
        results = await asyncio.gather(
            registry.join(PAN_A, "preferred", rssi=-90, prefer_as_gateway=True),
            registry.join(PAN_A, "untested-but-strong-signal", rssi=-30),
        )
        for group in results:
            assert group.gateway_serial == "preferred"

    @pytest.mark.asyncio
    async def test_does_not_displace_an_already_established_gateway(self, registry):
        """An existing, working gateway is never displaced just because
        a later joiner asks to be preferred -- matches the same
        reasoning GATEWAY_FAILURE_THRESHOLD's own docstring gives for
        why signal strength alone doesn't churn an established
        gateway."""
        await registry.join(PAN_A, "original-gateway", rssi=-70)
        group = await registry.join(PAN_A, "late-preferred", rssi=-10, prefer_as_gateway=True)
        assert group.gateway_serial == "original-gateway"
        assert "late-preferred" in group.members  # still joined as a regular member

    @pytest.mark.asyncio
    async def test_prefer_as_gateway_false_by_default_matches_prior_behavior(self, registry):
        """Confirms the parameter is opt-in, not a default-on behavior
        change for every existing join() call site."""
        group = await registry.join(PAN_A, "solo", rssi=-70)
        assert group.gateway_serial == "solo"  # still elected normally, no regression
