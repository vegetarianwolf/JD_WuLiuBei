"""Pickup-Aware Cooperative HALNS — formal architecture tests.

These tests prove on *constructed synthetic instances* that the formal
Cooperative-HALNS structure behaves correctly:

- ServiceMode is exactly {DIRECT, RELAY} (no SWAP).
- Balanced Ownership Reassignment operates on DIRECT tasks with max-regret
  first (Regret-2) ordering and an honest ``enable_ownership`` toggle.
- ``routes + relays`` is the single source of truth; TaskServiceState is
  rebuilt from it (pickup_owner == relay.first_drone, etc.).
- Relay K/Q semantics and async drop <= pick are preserved.
- Relay endpoint degeneracy (h == P_i / h == D_i) is rejected.

The dynamic-relay mechanism tests (delivery-risk / blocker-release / dual
trigger / no-risk) live in ``test_critical_relay.py``.
"""

from __future__ import annotations

from random import Random

import pytest

from uav_dispatch.cooperative_alns import (
    CooperativeHALNSConfig,
    _criticality_guided_dynamic_relay_once,
    _make_solution,
    _pickup_risk_rank,
    _remove_task_services,
    _repair_direct,
    _repair_ownership_targeted,
    _select_highest_regret_task,
    build_destroy_pool,
    build_repair_pool,
    solve_cooperative_halns,
)
from uav_dispatch.cooperative_model import (
    ServiceMode,
    rebuild_services,
)
from uav_dispatch.cooperative_validation import (
    CooperativeSolution,
    validate_cooperative_solution,
)
from uav_dispatch.dynamic_relay import (
    DynamicRelayPointPool,
)
from uav_dispatch.model import Point, Problem, Score, Task
from uav_dispatch.ownership_reassignment import find_upstream_blockers
from uav_dispatch.relay_model import RelayStation
from uav_dispatch.search import RouteEvaluator, Routes


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_problem_2d4t() -> Problem:
    """2 drones, 4 tasks, capacity=2, K=2, all loose."""
    return Problem(
        tasks=(
            Task(1, Point(0, 0), Point(10, 0), 60),
            Task(2, Point(0, 1), Point(10, 1), 60),
            Task(3, Point(0, 2), Point(10, 2), 60),
            Task(4, Point(0, 3), Point(10, 3), 60),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
    )


def _routes_2d_balanced() -> Routes:
    return ((1, -1, 2, -2), (3, -3, 4, -4))


def _imbalanced_ownership_problem() -> Problem:
    """Drone 0 holds one very tight task + one loose; drone 1 all loose."""
    return Problem(
        tasks=(
            Task(1, Point(0, 0), Point(100, 0), 15),   # very tight
            Task(2, Point(0, 0), Point(5, 0), 200),     # loose
            Task(3, Point(0, 1), Point(100, 1), 15),    # very tight
            Task(4, Point(0, 2), Point(5, 2), 200),     # loose
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1.0,
    )


def _blocker_relay_problem() -> Problem:
    """Drone A: loose far task 1 then critical task 2.  Station at D3.

    Relaying blocker task 1 to drone B lets drone A reach P2 much earlier.
    """
    return Problem(
        tasks=(
            Task(1, Point(0, 0), Point(100, 0), 200),   # blocker, far delivery
            Task(2, Point(55, 0), Point(55, 10), 100),  # critical pickup
            Task(3, Point(20, 10), Point(50, 0), 200),  # D3 = station node (50,0)
            Task(4, Point(25, 20), Point(25, 30), 200),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1.0,
    )


# =========================================================================
# 1. ServiceMode: exactly {DIRECT, RELAY} — no SWAP
# =========================================================================


class TestServiceMode:
    def test_only_direct_and_relay(self):
        assert ServiceMode.DIRECT is not None
        assert ServiceMode.RELAY is not None
        with pytest.raises(AttributeError):
            _ = ServiceMode.SWAP  # type: ignore[attr-defined]

    def test_pickup_owner_property(self):
        problem = _make_problem_2d4t()
        sol = _make_solution(problem, _routes_2d_balanced())
        for task_id in problem.task_ids:
            state = sol.task_services[task_id]
            assert state.mode == ServiceMode.DIRECT
            assert state.pickup_owner == state.primary_owner
            assert state.primary_owner == state.delivery_owner
            assert state.relay_event is None
        val = validate_cooperative_solution(problem, sol)
        assert val.valid

    def test_state_rebuild_routes_plus_relays(self):
        """routes + relays rebuild correct DIRECT / RELAY TaskServiceState."""
        problem = _blocker_relay_problem()
        routes: Routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        sol = _make_solution(problem, routes)
        cfg = CooperativeHALNSConfig(
            seed=1, enable_ownership=False,
            relay_candidate_limit=3, relay_max_evaluations=12,
        )
        pool = DynamicRelayPointPool(problem)
        relayed, stats = _criticality_guided_dynamic_relay_once(
            problem, sol, cfg, Random(1), None, pool
        )
        assert stats["applied"]
        rebuilt = rebuild_services(relayed.routes, relayed.relays, ())
        for tid, state in relayed.task_services.items():
            ref = rebuilt[tid]
            assert state.mode == ref.mode
            assert state.primary_owner == ref.primary_owner
            assert state.delivery_owner == ref.delivery_owner
        # RELAY task: pickup_owner == relay.first_drone, delivery == second.
        for tid, state in relayed.task_services.items():
            if state.mode == ServiceMode.RELAY:
                assert state.relay_event is not None
                assert state.pickup_owner == state.relay_event.first_drone
                assert state.delivery_owner == state.relay_event.second_drone
        val = validate_cooperative_solution(problem, relayed)
        assert val.valid


# =========================================================================
# 2. Ownership Regret-2: MAX regret first
# =========================================================================


class TestOwnershipRegret:
    def test_select_highest_regret_task(self):
        # A: regret (2 late, 5 km); B: regret (0, 1 km).  Standard Regret-2
        # places the task whose second-best is much worse than its best FIRST.
        chosen = _select_highest_regret_task(
            [
                (1, (2.0, 5.0), Score(0, 0.0, 10.0)),
                (2, (0.0, 1.0), Score(0, 0.0, 8.0)),
            ]
        )
        assert chosen == 1  # A first (max regret)

    def test_regret_breaks_by_best_delta_then_task_id(self):
        # Same regret for A and B: min best-delta wins, then min task id.
        chosen = _select_highest_regret_task(
            [
                (2, (1.0, 1.0), Score(0, 0.0, 5.0)),
                (1, (1.0, 1.0), Score(0, 0.0, 3.0)),
            ]
        )
        assert chosen == 1

    def test_repair_ownership_targeted_still_changes_owner(self):
        problem = _imbalanced_ownership_problem()
        evaluator = RouteEvaluator(problem)
        routes: Routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        sol = _make_solution(problem, routes)
        cfg = CooperativeHALNSConfig(seed=1, candidate_limit=48)
        partial, removed = _remove_task_services(problem, sol, [1, 3])
        moves = {1: 1, 3: 0}
        repaired, stats = _repair_ownership_targeted(
            problem, evaluator, partial, removed, cfg, Random(1), None, moves
        )
        assert repaired is not None
        assert repaired.task_services[1].primary_owner == 1
        assert repaired.task_services[3].primary_owner == 0
        assert stats["candidates"] > 0
        assert stats["feasible"] > 0
        val = validate_cooperative_solution(problem, repaired)
        assert val.valid


# =========================================================================
# 3. Ownership toggle: enable_ownership=False
# =========================================================================


class TestOwnershipToggle:
    def test_pools_exclude_ownership_when_disabled(self):
        off = CooperativeHALNSConfig(enable_ownership=False)
        on = CooperativeHALNSConfig(enable_ownership=True)
        assert "ownership" not in build_destroy_pool(off)
        assert "ownership_targeted" not in build_repair_pool(off)
        assert "ownership" in build_destroy_pool(on)
        assert "ownership_targeted" in build_repair_pool(on)

    def test_ownership_calls_zero_when_disabled(self):
        problem = _imbalanced_ownership_problem()
        sol = _make_solution(problem, ((1, -1, 2, -2), (3, -3, 4, -4)))
        result = solve_cooperative_halns(
            problem,
            config=CooperativeHALNSConfig(
                seed=3,
                max_iterations=25,
                time_limit_seconds=None,
                verbose=False,
                enable_ownership=False,
                enable_relay=False,
            ),
            initial_solution=sol,
        )
        assert result.validation.valid
        assert result.op_stats["ownership"]["calls"] == 0
        assert result.ownership_calls == 0
        assert result.ownership_accepted == 0


# =========================================================================
# 4. Relay K semantics: RELAY_PICK does not consume K
# =========================================================================


class TestKRelaySemantics:
    def test_receiver_pick_does_not_consume_k(self):
        problem = _blocker_relay_problem()
        routes: Routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        sol = _make_solution(problem, routes)
        cfg = CooperativeHALNSConfig(
            seed=1, enable_ownership=False,
            relay_candidate_limit=3, relay_max_evaluations=12,
        )
        pool = DynamicRelayPointPool(problem)
        relayed, stats = _criticality_guided_dynamic_relay_once(
            problem, sol, cfg, Random(1), None, pool
        )
        assert stats["applied"]
        val = validate_cooperative_solution(problem, relayed)
        assert val.valid
        # The relayed task's +pickup stays on the first drone (K counts only
        # original CUSTOMER_PICK events); the receiver only carries -delivery.
        for relay in relayed.relays:
            assert relay.task_id in (
                v for v in relayed.routes[relay.first_drone] if v > 0
            )
            assert relay.task_id not in (
                v for v in relayed.routes[relay.second_drone] if v > 0
            )
            assert -relay.task_id in relayed.routes[relay.second_drone]
        # Total pickups per drone still within K.
        for drone in range(problem.drone_count):
            count = sum(v > 0 for v in relayed.routes[drone])
            assert count <= problem.max_tasks_per_drone
        # Sum of pickups equals task count.
        total_pickups = sum(
            sum(v > 0 for v in route) for route in relayed.routes
        )
        assert total_pickups == len(problem.task_ids)


# =========================================================================
# 5. Relay Q semantics: +1 / -1 / +1 / -1 load propagation
# =========================================================================


class TestQRelaySemantics:
    def test_receiver_over_capacity_rejected(self):
        """A RELAY_PICK that pushes the receiver over Q is invalid."""
        problem = Problem(
            tasks=(
                Task(1, Point(1, 0), Point(6, 0), 100),
                Task(2, Point(0, 1), Point(10, 1), 100),
                Task(3, Point(0, 2), Point(10, 2), 100),
                Task(4, Point(9, 9), Point(9, 10), 100),
            ),
            drone_count=2,
            max_tasks_per_drone=3,
            capacity=2,
            speed_km_per_min=1.0,
        )
        station = RelayStation(0, Point(9, 10), -4)
        from uav_dispatch.relay_model import RelayTransfer as RT
        relay = RT(0, 1, 0, 0, 1, 1, 2)
        routes = ((1,), (2, 3, -1, -2, -3))
        sol = CooperativeSolution(
            routes=routes,
            relays=(relay,),
            active_stations=(station,),
        )
        val = validate_cooperative_solution(problem, sol)
        assert not val.valid
        assert any("超过上限" in v for v in val.violations)

    def test_valid_relay_load_propagation(self):
        """Valid relay: donor drop releases load, receiver pick adds load."""
        problem = Problem(
            tasks=(
                Task(1, Point(1, 0), Point(6, 0), 100),
                Task(2, Point(0, 1), Point(0, 2), 100),
            ),
            drone_count=2,
            max_tasks_per_drone=2,
            capacity=2,
            speed_km_per_min=1.0,
        )
        station = RelayStation(0, Point(0, 2), -2)
        from uav_dispatch.relay_model import RelayTransfer as RT
        relay = RT(0, 1, 0, 0, 1, 1, 2)
        sol = CooperativeSolution(
            routes=((1,), (2, -2, -1)),
            relays=(relay,),
            active_stations=(station,),
        )
        val = validate_cooperative_solution(problem, sol)
        assert val.valid, val.violations
        assert val.late_count == 0
        # RELAY_PICK is +1 load but does not consume a K slot.
        assert sum(v > 0 for v in sol.routes[1]) == 1  # only task 2's pickup


# =========================================================================
# 6. Async relay: drop <= pick (not required equal)
# =========================================================================


class TestAsyncRelay:
    def test_drop_before_pick_not_equal(self):
        """A drops first and leaves; B arrives later — drop < pick is legal."""
        problem = Problem(
            tasks=(
                Task(1, Point(1, 0), Point(6, 0), 100),
                Task(2, Point(9, 0), Point(9, 1), 100),
            ),
            drone_count=2,
            max_tasks_per_drone=2,
            capacity=2,
            speed_km_per_min=1.0,
        )
        station = RelayStation(0, Point(9, 1), -2)
        from uav_dispatch.relay_model import RelayTransfer as RT
        relay = RT(0, 1, 0, 0, 1, 1, 2)
        sol = CooperativeSolution(
            routes=((1,), (2, -2, -1)),
            relays=(relay,),
            active_stations=(station,),
        )
        val = validate_cooperative_solution(problem, sol)
        assert val.valid, val.violations
        from uav_dispatch.relay_validation import evaluate_relay_solution
        ev = evaluate_relay_solution(problem, sol.to_relay_solution())
        timing = ev.relay_timings[0]
        assert timing.drop_time_min <= timing.pick_time_min + 1e-9
        assert timing.drop_time_min < timing.pick_time_min  # async, not equal


# =========================================================================
# 7. Relay endpoint degeneracy: reject h == P_i and h == D_i
# =========================================================================


class TestRelayEndpoint:
    def test_shortlist_rejects_own_endpoints(self):
        problem = _make_problem_2d4t()
        routes = _routes_2d_balanced()
        pool = DynamicRelayPointPool(problem)
        points = pool.shortlist_for(routes, 1, 0, 1, top_k=100)
        # No point may coincide with P1 or D1.
        p1 = problem.task(1).pickup
        d1 = problem.task(1).delivery
        for st in points:
            assert st.point.distance_to(p1) > 1e-9
            assert st.point.distance_to(d1) > 1e-9

    def test_relay_validation_rejects_endpoint(self):
        problem = _make_problem_2d4t()
        station = RelayStation(0, Point(0, 0), 1)  # exactly P1
        from uav_dispatch.relay_model import RelayTransfer as RT
        relay = RT(0, 1, 0, 0, 1, 1, 2)
        sol = CooperativeSolution(
            routes=((1,), (2, -2, -1)),
            relays=(relay,),
            active_stations=(station,),
        )
        val = validate_cooperative_solution(problem, sol)
        assert not val.valid
        assert any("重合" in v for v in val.violations)


# =========================================================================
# 8. Relay can be torn down back to DIRECT (relay_structure destroy)
# =========================================================================


class TestRelayToDirect:
    def test_relay_destroy_then_direct_repair(self):
        problem = _blocker_relay_problem()
        evaluator = RouteEvaluator(problem)
        routes: Routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        sol = _make_solution(problem, routes)
        cfg = CooperativeHALNSConfig(
            seed=1, enable_ownership=False,
            relay_candidate_limit=3, relay_max_evaluations=12,
        )
        pool = DynamicRelayPointPool(problem)
        relayed, stats = _criticality_guided_dynamic_relay_once(
            problem, sol, cfg, Random(1), None, pool
        )
        assert stats["applied"]
        assert relayed.task_services[1].mode == ServiceMode.RELAY

        # Destroy the relay task and repair it as DIRECT again.
        partial, removed = _remove_task_services(problem, relayed, [1])
        assert 1 in removed
        assert len(partial.relays) == 0
        restored = _repair_direct(
            problem, evaluator, partial, removed, cfg, Random(1), None
        )
        assert restored is not None
        assert restored.task_services[1].mode == ServiceMode.DIRECT
        assert restored.relay_count == 0
        val = validate_cooperative_solution(problem, restored)
        assert val.valid


# =========================================================================
# 9. best is never worse than initial under (late_count, distance_km)
# =========================================================================


class TestBestNeverWorse:
    def test_final_leq_initial(self):
        problem = _blocker_relay_problem()
        sol = _make_solution(problem, ((1, -1, 2, -2), (3, -3, 4, -4)))
        cfg = CooperativeHALNSConfig(
            seed=1, time_limit_seconds=4.0, max_iterations=50,
            enable_ownership=True, enable_relay=True,
        )
        result = solve_cooperative_halns(problem, config=cfg,
                                         initial_solution=sol)
        assert result.validation.valid
        # strict lexicographic (late_count, distance_km)
        assert result.best_score <= result.initial_score
        assert result.best_score.late_count <= result.initial_score.late_count


# =========================================================================
# 10. total_lateness never enters the formal Score order
# =========================================================================


class TestObjectivePurity:
    def test_lateness_not_in_score_compare(self):
        a = Score(2, 999.0, 10.0)
        b = Score(2, 0.0, 20.0)
        # Same late_count; distance decides; total_lateness is ignored.
        assert a < b
        assert not (b < a)
        c = Score(3, 0.0, 1.0)
        d = Score(2, 999.0, 500.0)
        # Fewer late tasks always wins even with far more lateness.
        assert d < c

    def test_score_equality_ignores_lateness(self):
        a = Score(5, 100.0, 50.0)
        b = Score(5, 1.0, 50.0)
        # The formal order only sees (late_count, distance_km).
        assert not (a < b)
        assert not (b < a)
        assert a <= b
        assert b <= a


# =========================================================================
# 11. Blocker detection stays correct
# =========================================================================


class TestBlockerDetection:
    def test_blocker_found(self):
        problem = _make_problem_2d4t()
        evaluator = RouteEvaluator(problem)
        route = (1, -1, 2, -2)
        blockers = find_upstream_blockers(problem, evaluator, route, 2)
        assert len(blockers) == 1
        assert blockers[0][0] == 1
        assert blockers[0][1] > 0


# =========================================================================
# 12. Near-zero slack ranks before deeply negative slack
# =========================================================================


class TestPickupRiskRanking:
    def test_borderline_before_deeply_negative(self):
        slacks = {1: -40.0, 2: -1.0, 3: 5.0, 4: 60.0}
        ranked = _pickup_risk_rank(slacks, [1, 2, 3, 4])
        assert ranked.index(2) < ranked.index(1)
        assert ranked.index(3) < ranked.index(1)
