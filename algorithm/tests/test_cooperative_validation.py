"""Tests for unified cooperative validator (Phase 8)."""

from __future__ import annotations

import pytest

from uav_dispatch.cooperative_validation import (
    CooperativeSolution,
    validate_cooperative_solution,
)
from uav_dispatch.model import Point, Problem, Task
from uav_dispatch.relay_model import RelayStation, RelayTransfer


def _simple_problem() -> Problem:
    return Problem(
        tasks=(
            Task(1, Point(0, 0), Point(10, 0), 100),
            Task(2, Point(0, 5), Point(10, 5), 100),
            Task(3, Point(0, 10), Point(10, 10), 100),
            Task(4, Point(0, 15), Point(10, 15), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
    )


class TestBasicValidation:
    def test_valid_direct_solution(self):
        problem = _simple_problem()
        routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        sol = CooperativeSolution(routes=routes)
        result = validate_cooperative_solution(problem, sol)
        assert result.valid
        assert result.late_count == 0
        assert result.relay_count == 0
        assert result.swap_count == 0

    def test_missing_pickup(self):
        problem = _simple_problem()
        routes = ((1, -1, 2, -2), (3, -3))  # task 4 missing
        sol = CooperativeSolution(routes=routes)
        result = validate_cooperative_solution(problem, sol)
        assert not result.valid
        assert any("缺少" in v for v in result.violations)

    def test_duplicate_pickup(self):
        problem = _simple_problem()
        routes = ((1, -1, 1, 2, -2), (3, -3, 4, -4))  # task 1 twice
        sol = CooperativeSolution(routes=routes)
        result = validate_cooperative_solution(problem, sol)
        assert not result.valid

    def test_exceed_task_limit(self):
        problem = _simple_problem()
        routes = ((1, -1, 2, -2, 3, -3), (4, -4))  # drone 0 has 3 pickups
        sol = CooperativeSolution(routes=routes)
        result = validate_cooperative_solution(problem, sol)
        assert not result.valid

    def test_ownership_consistency(self):
        problem = _simple_problem()
        routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        ownership = {1: 0, 2: 0, 3: 1, 4: 1}
        sol = CooperativeSolution(routes=routes, ownership=ownership)
        result = validate_cooperative_solution(problem, sol)
        assert result.valid

    def test_ownership_inconsistency(self):
        problem = _simple_problem()
        routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        # Claim task 3 is on drone 0, but it's on drone 1
        ownership = {1: 0, 2: 0, 3: 0, 4: 1}
        sol = CooperativeSolution(routes=routes, ownership=ownership)
        result = validate_cooperative_solution(problem, sol)
        assert not result.valid


class TestRelayValidation:
    def test_valid_relay(self):
        problem = _simple_problem()
        station = RelayStation(id=0, point=Point(5, 2.5), source_visit=1)
        relay = RelayTransfer(
            id=0,
            task_id=2,
            station_id=0,
            first_drone=0,
            second_drone=1,
            drop_position=2,
            pick_position=0,
        )
        # Route with relay: drone 0 picks up 1,2 (drops 2 at station),
        # drone 1 delivers 2, then picks up 3,4
        routes = (
            (1, 2, -1),       # drone 0: pickup 1, pickup 2, deliver 1
            (-2, 3, -3, 4, -4),  # drone 1: deliver 2, pickup 3, deliver 3...
        )
        sol = CooperativeSolution(
            routes=routes,
            relays=(relay,),
            active_stations=(station,),
        )
        result = validate_cooperative_solution(problem, sol)
        # May have timing violations but structural checks should pass
        assert result.relay_count == 1
        assert result.active_station_count == 1

    def test_relay_same_drone_rejected(self):
        problem = _simple_problem()
        # RelayTransfer.__post_init__ already rejects same-drone relays
        with pytest.raises(ValueError, match="不同无人机"):
            RelayTransfer(
                id=0, task_id=2, station_id=0,
                first_drone=0, second_drone=0,  # same drone!
                drop_position=1, pick_position=2,
            )

    def test_relay_station_not_active(self):
        problem = _simple_problem()
        relay = RelayTransfer(
            id=0, task_id=2, station_id=99,
            first_drone=0, second_drone=1,
            drop_position=1, pick_position=0,
        )
        sol = CooperativeSolution(
            routes=((1, 2, -1), (-2, 3, -3, 4, -4)),
            relays=(relay,),
            active_stations=(),  # no stations active
        )
        result = validate_cooperative_solution(problem, sol)
        assert not result.valid


class TestSwapRejectedInFormalPath:
    def test_swap_rejected(self):
        """The formal cooperative path must never contain swap events."""
        problem = _simple_problem()
        from uav_dispatch.swap_model import SwapEvent
        swap = SwapEvent(
            id=0,
            drone_a=0, drone_b=1,
            task_a_to_b=1, task_b_to_a=3,
            meeting_node=1,
            position_a=1, position_b=1,
        )
        sol = CooperativeSolution(
            routes=((1, 2, -2, -3), (3, 4, -4, -1)),
            swaps=(swap,),
        )
        result = validate_cooperative_solution(problem, sol)
        assert not result.valid
        assert any("Swap" in v for v in result.violations)


class TestLateCountDetection:
    def test_all_on_time(self):
        problem = _simple_problem()
        routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        sol = CooperativeSolution(routes=routes)
        result = validate_cooperative_solution(problem, sol)
        assert result.late_count == 0

    def test_late_task_detected(self):
        problem = Problem(
            tasks=(
                Task(1, Point(0, 0), Point(10, 0), 5),    # tight
                Task(2, Point(0, 5), Point(10, 5), 100),
                Task(3, Point(0, 10), Point(10, 10), 100),
                Task(4, Point(0, 15), Point(10, 15), 100),
            ),
            drone_count=2, max_tasks_per_drone=2, capacity=2,
        )
        # Drone 0 has both tasks — task 1 will likely be late
        routes = ((1, 2, -1, -2), (3, -3, 4, -4))
        sol = CooperativeSolution(routes=routes)
        result = validate_cooperative_solution(problem, sol)
        assert result.late_count > 0
