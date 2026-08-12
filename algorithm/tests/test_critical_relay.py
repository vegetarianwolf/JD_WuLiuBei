"""Criticality-Guided Dynamic Relay — mechanism tests.

Proves, on constructed synthetic instances, that the unified post-repair
relay neighborhood really fires:

1. Delivery-Risk relay rescues a DIRECT-late task (late_count down).
2. Blocker-Release relay advances a critical pickup (t(P_i) earlier).
3. The two candidate sources merge + dedup on (task, donor, receiver, h):
   one candidate, two triggers, one exact DAG evaluation.
4. The contextual relay-point shortlist ranks points per (task, donor,
   receiver) — different triples can pick different h.
5. A no-risk solution produces no relay candidates.

Formal objective everywhere is (late_count, distance_km); pickup advance /
delivery gain are guidance only.
"""

from __future__ import annotations

from random import Random

from uav_dispatch.cooperative_alns import (
    CooperativeHALNSConfig,
    _blocker_release_candidates,
    _criticality_guided_dynamic_relay_once,
    _delivery_risk_candidates,
    _make_solution,
    _merge_relay_candidates,
)
from uav_dispatch.cooperative_model import (
    compute_pickup_times,
    pickup_slacks,
)
from uav_dispatch.cooperative_validation import (
    validate_cooperative_solution,
)
from uav_dispatch.dynamic_relay import (
    DynamicRelayPointPool,
    RelayTrigger,
)
from uav_dispatch.model import Point, Problem, Task
from uav_dispatch.search import RouteEvaluator, Routes


def _delivery_risk_problem() -> Problem:
    """A's route detours through task 2 before delivering task 1, so task 1
    is DIRECT-late; relaying task 1 at h = P3 to B delivers D1 on time."""
    return Problem(
        tasks=(
            Task(1, Point(0, 0), Point(100, 0), 110),   # delivery-late
            Task(2, Point(0, 100), Point(0, 101), 300),  # A's detour
            Task(3, Point(95, 0), Point(95, 10), 300),   # h = P3 near corridor
            Task(4, Point(0, 30), Point(0, 31), 300),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1.0,
    )


def _dual_trigger_problem() -> Problem:
    """Task 1 is simultaneously delivery-late (A detours through task 2)
    and an upstream blocker of the critical pickup of task 5.  Both relay
    guidances emit the same (task 1, donor 0, receiver 1, h) candidates."""
    return Problem(
        tasks=(
            Task(1, Point(0, 0), Point(100, 0), 110),
            Task(2, Point(0, 100), Point(0, 101), 300),
            Task(3, Point(95, 0), Point(95, 10), 300),
            Task(4, Point(0, 30), Point(0, 31), 300),
            Task(5, Point(110, 50), Point(110, 60), 200),  # critical pickup
        ),
        drone_count=2,
        max_tasks_per_drone=3,
        capacity=2,
        speed_km_per_min=1.0,
    )


def _blocker_relay_problem() -> Problem:
    """Task 1 (P_j -> D_j -> P_i) blocks critical pickup of task 2."""
    return Problem(
        tasks=(
            Task(1, Point(0, 0), Point(100, 0), 200),
            Task(2, Point(55, 0), Point(55, 10), 100),
            Task(3, Point(20, 10), Point(50, 0), 200),
            Task(4, Point(25, 20), Point(25, 30), 200),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1.0,
    )


# =========================================================================
# 1. Delivery-Risk Relay
# =========================================================================


class TestDeliveryRiskRelay:
    def test_direct_late_rescued_by_relay(self):
        problem = _delivery_risk_problem()
        routes: Routes = ((1, 2, -2, -1), (3, -3, 4, -4))
        sol = _make_solution(problem, routes)
        before = validate_cooperative_solution(problem, sol)
        assert before.valid
        assert before.late_count >= 1  # task 1 is DIRECT-late

        cfg = CooperativeHALNSConfig(
            seed=1, enable_ownership=False,
            enable_delivery_risk_relay=True, enable_blocker_relay=False,
        )
        pool = DynamicRelayPointPool(problem)
        new_sol, stats = _criticality_guided_dynamic_relay_once(
            problem, sol, cfg, Random(1), None, pool
        )
        assert stats["delivery_risk_tasks_detected"] >= 1
        assert stats["candidates_before_dedup"] > 0
        assert stats["applied"]
        assert RelayTrigger.DELIVERY_RISK in stats["applied_triggers"]

        after = validate_cooperative_solution(problem, new_sol)
        assert after.valid
        assert after.late_count < before.late_count  # late_count down
        assert len(new_sol.relays) == 1
        # the rescued task is now RELAY
        assert new_sol.task_services[1].mode.name == "RELAY"
        assert new_sol.task_services[1].relay_event is not None
        # used relay point is in active_stations
        used = {r.station_id for r in new_sol.relays}
        assert {st.id for st in new_sol.active_stations} == used


# =========================================================================
# 2. Blocker-Release Relay
# =========================================================================


class TestBlockerReleaseRelay:
    def test_critical_pickup_advances(self):
        problem = _blocker_relay_problem()
        routes: Routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        sol = _make_solution(problem, routes)
        old_pickup = compute_pickup_times(problem, sol.routes)[2]

        cfg = CooperativeHALNSConfig(
            seed=1, enable_ownership=False,
            enable_delivery_risk_relay=False, enable_blocker_relay=True,
            relay_candidate_limit=3, relay_max_evaluations=12,
        )
        pool = DynamicRelayPointPool(problem)
        new_sol, stats = _criticality_guided_dynamic_relay_once(
            problem, sol, cfg, Random(1), None, pool
        )
        assert stats["blockers_detected"] >= 1
        assert stats["candidates_before_dedup"] > 0
        assert stats["applied"]
        assert RelayTrigger.BLOCKER_RELEASE in stats["applied_triggers"]
        assert stats["applied_pickup_advance"] > 1e-6

        new_pickup = compute_pickup_times(problem, new_sol.routes)[2]
        assert new_pickup < old_pickup - 1e-6  # t(P_i) clearly earlier
        val = validate_cooperative_solution(problem, new_sol)
        assert val.valid
        # the blocker task is now served by relay
        assert new_sol.task_services[1].mode.name == "RELAY"


# =========================================================================
# 3. Dual-trigger dedup
# =========================================================================


class TestDualTriggerDedup:
    def test_same_candidate_merged_with_two_triggers(self):
        problem = _dual_trigger_problem()
        routes: Routes = ((1, 2, -2, -1, 5, -5), (3, -3, 4, -4))
        sol = _make_solution(problem, routes)
        val = validate_cooperative_solution(problem, sol)
        cfg = CooperativeHALNSConfig(seed=1, enable_ownership=False)
        pool = DynamicRelayPointPool(problem)
        evaluator = RouteEvaluator(problem)

        dr, _ = _delivery_risk_candidates(
            problem, sol, cfg, pool,
            dict(val.delivery_times_min), dict(val.pickup_times_min),
        )
        br, _ = _blocker_release_candidates(
            problem, sol, cfg, evaluator, pool, pickup_slacks(problem, sol.routes)
        )
        assert dr and br
        dr_keys = {c.dedup_key for c in dr}
        br_keys = {c.dedup_key for c in br}
        common = dr_keys & br_keys
        assert common  # the same (task, donor, receiver, h) from both sources

        merged = _merge_relay_candidates(dr + br)
        assert len(merged) < len(dr) + len(br)  # dedup actually removed
        merged_by_key = {c.dedup_key: c for c in merged}
        for key in common:
            assert merged_by_key[key].triggers == frozenset(
                {RelayTrigger.DELIVERY_RISK, RelayTrigger.BLOCKER_RELEASE}
            )

    def test_neighborhood_dedup_counts_and_one_eval(self):
        problem = _dual_trigger_problem()
        routes: Routes = ((1, 2, -2, -1, 5, -5), (3, -3, 4, -4))
        sol = _make_solution(problem, routes)
        cfg = CooperativeHALNSConfig(
            seed=1, enable_ownership=False,
            relay_candidate_limit=1, relay_max_evaluations=8,
        )
        pool = DynamicRelayPointPool(problem)
        new_sol, stats = _criticality_guided_dynamic_relay_once(
            problem, sol, cfg, Random(1), None, pool
        )
        assert stats["candidates_before_dedup"] > stats["candidates_after_dedup"]
        assert stats["applied"]
        assert len(stats["applied_triggers"]) == 2  # dual trigger
        assert stats["exact_evaluations"] >= 1


# =========================================================================
# 4. Contextual relay-point ranking
# =========================================================================


class TestContextualRelayPoint:
    def test_shortlist_ranks_per_triple(self):
        problem = _blocker_relay_problem()
        routes: Routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        pool = DynamicRelayPointPool(problem)
        scored = pool.shortlist_for_scored(routes, 1, 0, 1, top_k=3)
        assert len(scored) == 3
        scores = [s for s, _ in scored]
        assert scores == sorted(scores)  # ascending contextual score
        # the first-ranked point is a real candidate (not an endpoint)
        task = problem.task(1)
        st = scored[0][1]
        assert st.point.distance_to(task.pickup) > 1e-9
        assert st.point.distance_to(task.delivery) > 1e-9

    def test_relay_uses_p_union_d(self):
        problem = _delivery_risk_problem()
        pool = DynamicRelayPointPool(problem)
        # Catalog is exactly the deduplicated P ∪ D nodes.
        nodes = set()
        for task in problem.tasks:
            nodes.add((round(task.pickup.x, 6), round(task.pickup.y, 6)))
            nodes.add((round(task.delivery.x, 6), round(task.delivery.y, 6)))
        assert pool.site_count == len(nodes)
        for station in pool.all_sites:
            assert (round(station.point.x, 6), round(station.point.y, 6)) in nodes


# =========================================================================
# 5. No-risk case: no candidates
# =========================================================================


class TestNoRiskCase:
    def test_no_candidates_when_no_risk(self):
        problem = Problem(
            tasks=(
                Task(1, Point(0, 0), Point(10, 0), 60),
                Task(2, Point(0, 1), Point(10, 1), 60),
                Task(3, Point(0, 2), Point(10, 2), 60),
                Task(4, Point(0, 3), Point(10, 3), 60),
            ),
            drone_count=2, max_tasks_per_drone=2, capacity=2,
        )
        routes: Routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        sol = _make_solution(problem, routes)
        val = validate_cooperative_solution(problem, sol)
        assert val.valid and val.late_count == 0
        cfg = CooperativeHALNSConfig(seed=1, enable_ownership=False)
        pool = DynamicRelayPointPool(problem)
        new_sol, stats = _criticality_guided_dynamic_relay_once(
            problem, sol, cfg, Random(1), None, pool
        )
        assert stats["candidates_before_dedup"] == 0
        assert stats["blockers_detected"] == 0
        assert stats["delivery_risk_tasks_detected"] == 0
        assert not stats["applied"]
        assert new_sol is sol
