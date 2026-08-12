"""Tests for the global Swap-only validator (event DAG / synchronisation)."""

from __future__ import annotations

import pytest

from uav_dispatch.model import Point, Problem, Task
from uav_dispatch.swap_model import SwapEvent, SwapSolution, swap_solution_from_routes
from uav_dispatch.swap_validation import evaluate_swap_solution
from uav_dispatch.validation import evaluate_solution


def _problem(*tasks: Task, drone_count: int = 2, speed: float = 1.0) -> Problem:
    return Problem(
        tasks,
        drone_count=drone_count,
        max_tasks_per_drone=len(tasks),
        capacity=2,
        speed_km_per_min=speed,
    )


# ---------------------------------------------------------------------------
# Case 1: zero-swap solutions must agree exactly with the plain model.
# ---------------------------------------------------------------------------
def test_case1_no_swap_matches_plain_model():
    problem = _problem(
        Task(1, Point(1, 0), Point(3, 0), 10),
        Task(2, Point(0, 1), Point(0, 3), 10),
        Task(3, Point(2, 0), Point(2, 2), 20),
    )
    routes = ((1, -1, 3, -3), (2, -2))
    swap_eval = evaluate_swap_solution(problem, swap_solution_from_routes(routes))
    plain = evaluate_solution(problem, routes)

    assert swap_eval.valid
    assert swap_eval.score == plain.score
    assert swap_eval.delivery_times_min == plain.delivery_times_min
    assert swap_eval.total_lateness_min == plain.total_lateness_min
    assert swap_eval.swap_count == 0


# ---------------------------------------------------------------------------
# Case 2: A and B each carry one parcel and exchange them legally.
# ---------------------------------------------------------------------------
def test_case2_legal_one_for_one_exchange():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(0, 2), Point(0, 1), 100),
    )
    routes = ((1, -2), (2, -1))
    swap = SwapEvent(0, 0, 1, 1, 2, -2, 1, 1)
    solution = SwapSolution(routes, (swap,))

    result = evaluate_swap_solution(problem, solution)

    assert result.valid, result.violations
    assert result.swap_count == 1
    # A: depot->P1 (1) -> D2 (sqrt2 ~ 1.414) => arrival 2.414
    # B: depot->P2 (2) -> D2 (1)          => arrival 3
    timing = result.swap_timings[0]
    assert timing.arrival_a_min == pytest.approx(1 + 2 ** 0.5)
    assert timing.arrival_b_min == pytest.approx(3.0)
    assert timing.swap_time_min == pytest.approx(3.0)
    assert timing.waiting_a_min == pytest.approx(3.0 - (1 + 2 ** 0.5))
    assert timing.waiting_b_min == pytest.approx(0.0)
    # A delivers 2 at the meeting point (D2), B flies D2 -> D1 (sqrt5 ~ 2.236).
    assert result.delivery_times_min[2] == pytest.approx(3.0)
    assert result.delivery_times_min[1] == pytest.approx(3.0 + 5 ** 0.5)
    assert result.score.late_count == 0


# ---------------------------------------------------------------------------
# Case 3: one side has not picked up its parcel before the swap.
# ---------------------------------------------------------------------------
def test_case3_swap_before_pickup_is_rejected():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(0, 2), Point(0, 1), 100),
    )
    # B's pickup of task 2 happens at index 0 but the swap sits at gap 0.
    routes = ((1, -2), (2, -1))
    swap = SwapEvent(0, 0, 1, 1, 2, -2, 1, 0)
    solution = SwapSolution(routes, (swap,))

    result = evaluate_swap_solution(problem, solution)

    assert not result.valid
    assert any("取件必须早于交换" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Case 4: one side has already delivered the parcel it would hand over.
# ---------------------------------------------------------------------------
def test_case4_swap_after_delivery_is_rejected():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(0, 2), Point(0, 1), 100),
    )
    # A fully delivers task 1 before the swap, so it no longer holds parcel 1.
    routes = ((1, -1, -2), (2,))
    swap = SwapEvent(0, 0, 1, 1, 2, -2, 2, 1)
    solution = SwapSolution(routes, (swap,))

    result = evaluate_swap_solution(problem, solution)

    assert not result.valid
    assert any("未持有" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Case 5: the same task participates in two swaps.
# ---------------------------------------------------------------------------
def test_case5_task_in_two_swaps_is_rejected():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(0, 2), Point(0, 1), 100),
        Task(3, Point(0, 4), Point(0, 3), 100),
        Task(4, Point(1, 4), Point(1, 3), 100),
    )
    routes = ((1, -4, -2), (2, 3, -3, -1))
    # Task 1 appears in both swaps.
    swap_one = SwapEvent(0, 0, 1, 1, 2, -2, 1, 1)
    swap_two = SwapEvent(1, 0, 1, 1, 3, -3, 2, 3)
    solution = SwapSolution(routes, (swap_one, swap_two))

    result = evaluate_swap_solution(problem, solution)

    assert not result.valid
    assert any("参与多次交换" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Case 6: a one-way transfer (B never picked up the parcel it "gives").
# ---------------------------------------------------------------------------
def test_case6_one_way_transfer_is_rejected():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(0, 2), Point(0, 1), 100),
    )
    # B has no pickup of task 2 at all; the exchange cannot be bilateral.
    routes = ((1, -2), (-1,))
    swap = SwapEvent(0, 0, 1, 1, 2, -2, 1, 0)
    solution = SwapSolution(routes, (swap,))

    result = evaluate_swap_solution(problem, solution)

    assert not result.valid
    assert any("缺少" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Case 7: both drones exchanging the very same task is structurally illegal.
# ---------------------------------------------------------------------------
def test_case7_same_task_on_both_sides_is_rejected():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(0, 2), Point(0, 1), 100),
    )
    with pytest.raises(ValueError, match="同一个任务"):
        SwapEvent(0, 0, 1, 1, 1, -2, 1, 1)
    with pytest.raises(ValueError, match="不同无人机"):
        SwapEvent(0, 1, 1, 1, 2, -2, 1, 1)


# ---------------------------------------------------------------------------
# Case 8: overload (capacity > 2) anywhere in the solution is rejected.
# ---------------------------------------------------------------------------
def test_case8_capacity_exceeded_is_rejected():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(0, 2), Point(0, 1), 100),
        Task(3, Point(0, 4), Point(0, 3), 100),
        Task(4, Point(1, 4), Point(1, 3), 100),
        Task(5, Point(0, 5), Point(0, 6), 100),
    )
    # Drone A picks up three parcels -> load 3 > capacity 2.
    routes = ((1, 2, 3, -1, -2, -3), (4, -4, 5, -5))
    swap = SwapEvent(0, 0, 1, 4, 5, -5, 3, 2)
    solution = SwapSolution(routes, (swap,))

    result = evaluate_swap_solution(problem, solution)

    assert not result.valid
    assert any("载荷" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Case 9: synchronised waiting propagates into delivery times.
# ---------------------------------------------------------------------------
def test_case9_sync_waiting_propagates_times():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(0, 5), Point(0, 4), 100),
    )
    routes = ((1, -2), (2, -1))
    swap = SwapEvent(0, 0, 1, 1, 2, -2, 1, 1)
    solution = SwapSolution(routes, (swap,))

    result = evaluate_swap_solution(problem, solution)

    assert result.valid, result.violations
    # A arrives at D2 at 1 + sqrt(17); B arrives at 5 + 1 = 6.
    # The swap happens at 6 and A waits; both resume at the swap end.
    timing = result.swap_timings[0]
    assert timing.arrival_a_min == pytest.approx(1 + 17 ** 0.5)
    assert timing.arrival_b_min == pytest.approx(6.0)
    assert timing.waiting_a_min == pytest.approx(6.0 - (1 + 17 ** 0.5))
    assert timing.waiting_b_min == pytest.approx(0.0)
    assert result.delivery_times_min[2] == pytest.approx(6.0)
    # B: D2 -> D1 = sqrt(20).
    assert result.delivery_times_min[1] == pytest.approx(6.0 + 20 ** 0.5)
    assert result.waiting_time_min == pytest.approx(6.0 - (1 + 17 ** 0.5))


# ---------------------------------------------------------------------------
# Case 10: a swap arrangement that deadlocks must be rejected (DAG cycle).
# ---------------------------------------------------------------------------
def test_case10_deadlock_cycle_is_rejected():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(0, 2), Point(0, 1), 100),
        Task(3, Point(0, 4), Point(0, 3), 100),
        Task(4, Point(1, 4), Point(1, 3), 100),
    )
    # A timeline: +1 -> S1 -> +3 -> -4 -> S2 -> -2
    # B timeline: +2 -> S2 -> +4 -> -3 -> S1 -> -1
    # S2 -> B:+4 -> B:-3 -> S1 -> A:+3 -> A:-4 -> S2 is a cycle.
    routes = ((1, 3, -4, -2), (2, 4, -3, -1))
    swap_one = SwapEvent(0, 0, 1, 1, 4, -4, 1, 3)
    swap_two = SwapEvent(1, 0, 1, 3, 2, -2, 3, 1)
    solution = SwapSolution(routes, (swap_one, swap_two))

    result = evaluate_swap_solution(problem, solution)

    assert not result.valid
    assert any("依赖环" in item or "死锁" in item for item in result.violations)
