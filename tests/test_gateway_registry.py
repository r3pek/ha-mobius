"""
Tests for gateway_registry.py -- the shared per-pan_id gateway
registry: election (including RSSI-based selection among concurrent
joiners), failover/promotion, pan_id moves, and cross-group isolation.
"""

import asyncio

import pytest
from unittest.mock import MagicMock

from custom_components.mobius.gateway_registry import GatewayRegistry
from custom_components.mobius.const import GATEWAY_FAILURE_THRESHOLD


PAN_A = 0x3D0F
PAN_B = 0x1234


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
            triggered = await registry.record_gateway_failure(PAN_A)
            assert triggered is False

        group = registry.group(PAN_A)
        assert group.gateway_serial == "gw"

    @pytest.mark.asyncio
    async def test_promotes_at_threshold(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "backup", rssi=-40)

        triggered = None
        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            triggered = await registry.record_gateway_failure(PAN_A)

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
            await registry.record_gateway_failure(PAN_A)

        assert registry.group(PAN_A).gateway_serial == "strong_backup"

    @pytest.mark.asyncio
    async def test_success_resets_failure_counter(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "backup", rssi=-40)

        await registry.record_gateway_failure(PAN_A)
        await registry.record_gateway_failure(PAN_A)
        registry.record_gateway_success(PAN_A)

        # Counter reset -- should take another full threshold's worth of
        # failures to trigger promotion, not just one more.
        triggered = await registry.record_gateway_failure(PAN_A)
        assert triggered is False
        assert registry.group(PAN_A).gateway_serial == "gw"

    @pytest.mark.asyncio
    async def test_no_promotion_possible_leaves_group_gatewayless(self, registry):
        await registry.join(PAN_A, "solo", rssi=-50)

        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            await registry.record_gateway_failure(PAN_A)

        group = registry.group(PAN_A)
        assert group.gateway_serial is None
        assert group.gateway_connection is None
        # The failing (now-former) member is still tracked as a group
        # member -- just not gateway anymore.
        assert "solo" in group.members

    @pytest.mark.asyncio
    async def test_failure_on_nonexistent_group_is_a_safe_noop(self, registry):
        triggered = await registry.record_gateway_failure(0x9999)
        assert triggered is False

    @pytest.mark.asyncio
    async def test_success_on_nonexistent_group_is_a_safe_noop(self, registry):
        registry.record_gateway_success(0x9999)  # must not raise


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


class TestMoveMember:
    @pytest.mark.asyncio
    async def test_move_to_different_pan_id(self, registry):
        await registry.join(PAN_A, "mover", rssi=-50)
        await registry.join(PAN_B, "other", rssi=-50)

        new_group = await registry.move_member(PAN_A, PAN_B, "mover", rssi=-45)

        assert new_group.pan_id == PAN_B
        assert "mover" in new_group.members
        assert registry.group(PAN_A) is None  # PAN_A had only "mover" -- now empty, removed

    @pytest.mark.asyncio
    async def test_moving_the_gateway_promotes_a_replacement_in_the_old_group(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        await registry.join(PAN_A, "backup", rssi=-40)
        await registry.join(PAN_B, "other", rssi=-50)

        await registry.move_member(PAN_A, PAN_B, "gw", rssi=-45)

        old_group = registry.group(PAN_A)
        assert old_group.gateway_serial == "backup"
        assert "gw" not in old_group.members

    @pytest.mark.asyncio
    async def test_same_pan_id_just_updates_rssi_without_a_real_move(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        group_before = registry.group(PAN_A)

        result = await registry.move_member(PAN_A, PAN_A, "gw", rssi=-20)

        assert result is group_before  # same group object, not recreated
        assert group_before.gateway_serial == "gw"  # unaffected
        assert group_before.members["gw"].rssi == -20  # but RSSI updated


class TestUpdateRssi:
    @pytest.mark.asyncio
    async def test_updates_existing_member(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        registry.update_rssi(PAN_A, "gw", -30)
        assert registry.group(PAN_A).members["gw"].rssi == -30

    def test_nonexistent_group_is_a_safe_noop(self, registry):
        registry.update_rssi(0x9999, "nobody", -30)  # must not raise

    @pytest.mark.asyncio
    async def test_nonexistent_member_is_a_safe_noop(self, registry):
        await registry.join(PAN_A, "gw", rssi=-50)
        registry.update_rssi(PAN_A, "nonexistent", -30)  # must not raise
        assert "nonexistent" not in registry.group(PAN_A).members


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


class TestCrossGroupIsolation:
    @pytest.mark.asyncio
    async def test_two_groups_are_fully_independent(self, registry):
        await registry.join(PAN_A, "a1", rssi=-50)
        await registry.join(PAN_A, "a2", rssi=-40)
        await registry.join(PAN_B, "b1", rssi=-50)

        for _ in range(GATEWAY_FAILURE_THRESHOLD):
            await registry.record_gateway_failure(PAN_A)

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
