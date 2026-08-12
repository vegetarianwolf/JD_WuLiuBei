"""Tests for balanced pickup ownership reassignment (Phase 2)."""

from __future__ import annotations

import pytest

from uav_dispatch.model import Point, Problem, Task
from uav_dispatch.ownership_reassignment import (
    balanced_pair_exchange,
    balanced_two_by_two_exchange,
    balanced_three_cycle,
    balanced_destroy_repair,
    compute_pickup_owner,
    compute_pickup_profile,
    identify_critical_tasks,
    find_upstream_blockers,
    validate_pickup_counts,
)
from uav_dispatch.search import RouteEvaluator, Routes


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _simple_problem() -> Problem:
    """Two drones, 4 tasks (2 each), capacity=2."""
    return Problem(
        tasks=(
            Task(1, Point(0, 0), Point(10, 0), 100),
            Task(2, Point(0, 1), Point(10, 1), 100),
            Task(3, Point(0, 2), Point(10, 2), 100),
            Task(4, Point(0, 3), Point(10, 3), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
    )


def _routes_ab() -> Routes:
    """Drone 0 picks up 1,2; Drone 1 picks up 3,4."""
    return (
        (1, -1, 2, -2),
        (3, -3, 4, -4),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPickupProfile:
    def test_profile_counts(self):
        routes = _routes_ab()
        profile = compute_pickup_profile(routes)
        assert profile == {0: 2, 1: 2}

    def test_owner_mapping(self):
        routes = _routes_ab()
        owner = compute_pickup_owner(routes)
        assert owner[1] == 0
        assert owner[2] == 0
        assert owner[3] == 1
        assert owner[4] == 1

    def test_validate_counts(self):
        routes = _routes_ab()
        assert validate_pickup_counts(routes, 2)
        # Exceeding limit
        bad = ((1, -1, 2, -2, 3, -3), (4, -4))
        assert not validate_pickup_counts(bad, 2)


class TestBalancedPairExchange:
    def test_basic_exchange(self):
        routes = _routes_ab()
        result = balanced_pair_exchange(routes, 0, 1, 1, 3)
        assert result is not None
        # After exchange: drone 0 owns {2, 3}, drone 1 owns {1, 4}
        owner = compute_pickup_owner(result)
        assert owner[1] == 1  # task 1 moved to drone 1
        assert owner[3] == 0  # task 3 moved to drone 0
        assert owner[2] == 0
        assert owner[4] == 1
        # Counts preserved
        profile = compute_pickup_profile(result)
        assert profile[0] == 2
        assert profile[1] == 2

    def test_same_drone_rejected(self):
        routes = _routes_ab()
        assert balanced_pair_exchange(routes, 0, 0, 1, 2) is None

    def test_wrong_owner_rejected(self):
        routes = _routes_ab()
        # task 3 is on drone 1, not drone 0
        assert balanced_pair_exchange(routes, 0, 1, 3, 1) is None


class TestBalanced2x2Exchange:
    def test_basic(self):
        # Need 4 tasks per drone for 2x2
        problem = Problem(
            tasks=tuple(
                Task(i, Point(0, i), Point(10, i), 100) for i in range(1, 9)
            ),
            drone_count=2,
            max_tasks_per_drone=4,
            capacity=2,
        )
        routes: Routes = (
            (1, -1, 2, -2, 3, -3, 4, -4),
            (5, -5, 6, -6, 7, -7, 8, -8),
        )
        result = balanced_two_by_two_exchange(routes, 0, 1, (1, 2), (5, 6))
        assert result is not None
        owner = compute_pickup_owner(result)
        assert owner[1] == 1
        assert owner[2] == 1
        assert owner[5] == 0
        assert owner[6] == 0
        profile = compute_pickup_profile(result)
        assert profile[0] == 4
        assert profile[1] == 4


class TestBalancedThreeCycle:
    def test_basic(self):
        problem = Problem(
            tasks=tuple(
                Task(i, Point(0, i), Point(10, i), 100) for i in range(1, 7)
            ),
            drone_count=3,
            max_tasks_per_drone=2,
            capacity=2,
        )
        routes: Routes = (
            (1, -1, 2, -2),
            (3, -3, 4, -4),
            (5, -5, 6, -6),
        )
        result = balanced_three_cycle(routes, 0, 1, 2, 1, 3, 5)
        assert result is not None
        owner = compute_pickup_owner(result)
        # task 1: drone 0→1, task 3: drone 1→2, task 5: drone 2→0
        assert owner[1] == 1
        assert owner[3] == 2
        assert owner[5] == 0
        profile = compute_pickup_profile(result)
        assert profile[0] == 2
        assert profile[1] == 2
        assert profile[2] == 2


class TestBalancedDestroyRepair:
    def test_basic(self):
        problem = _simple_problem()
        evaluator = RouteEvaluator(problem)
        routes = _routes_ab()
        result = balanced_destroy_repair(problem, evaluator, routes, [2, 3])
        assert result is not None
        owner = compute_pickup_owner(result)
        # All 4 tasks must be present somewhere
        assert set(owner.keys()) == {1, 2, 3, 4}
        profile = compute_pickup_profile(result)
        assert profile[0] <= 2
        assert profile[1] <= 2

    def test_empty_remove(self):
        problem = _simple_problem()
        evaluator = RouteEvaluator(problem)
        routes = _routes_ab()
        result = balanced_destroy_repair(problem, evaluator, routes, [])
        assert result is not None
        assert compute_pickup_profile(result) == {0: 2, 1: 2}


class TestCriticalTaskIdentification:
    def test_no_critical_when_all_on_time(self):
        problem = _simple_problem()
        evaluator = RouteEvaluator(problem)
        routes = _routes_ab()
        critical = identify_critical_tasks(problem, evaluator, routes, top_k=3)
        # All tasks have ample deadline (100 min), so slacks are positive
        assert all(slack >= 0 for _, slack, _ in critical)

    def test_late_task_is_critical(self):
        problem = Problem(
            tasks=(
                Task(1, Point(0, 0), Point(10, 0), 5),  # tight deadline
                Task(2, Point(0, 1), Point(10, 1), 100),
                Task(3, Point(0, 2), Point(10, 2), 100),
                Task(4, Point(0, 3), Point(10, 3), 100),
            ),
            drone_count=2,
            max_tasks_per_drone=2,
            capacity=2,
        )
        evaluator = RouteEvaluator(problem)
        routes: Routes = (
            (1, -1, 2, -2),
            (3, -3, 4, -4),
        )
        critical = identify_critical_tasks(problem, evaluator, routes, top_k=3)
        # Task 1 should be most critical (tightest deadline)
        assert critical[0][0] == 1


class TestFindUpstreamBlockers:
    def test_no_blockers_on_empty_prefix(self):
        problem = _simple_problem()
        evaluator = RouteEvaluator(problem)
        route = (1, -1, 2, -2)
        blockers = find_upstream_blockers(problem, evaluator, route, 1)
        # Task 1 is first pickup, no previous deliveries
        assert len(blockers) == 0

    def test_blocker_found(self):
        problem = _simple_problem()
        evaluator = RouteEvaluator(problem)
        # Task 1 delivery before task 2 pickup
        route = (1, -1, 2, -2)
        blockers = find_upstream_blockers(problem, evaluator, route, 2)
        # Task 1's delivery is before task 2's pickup → blocker
        assert len(blockers) == 1
        assert blockers[0][0] == 1
        assert blockers[0][1] > 0  # positive holding time
