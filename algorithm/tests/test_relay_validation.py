"""Tests for the global asynchronous Relay validator (event DAG / custody)."""

from __future__ import annotations

import pytest

from uav_dispatch.model import Point, Problem, Task
from uav_dispatch.relay_model import (
    RelaySolution,
    RelayStation,
    RelayTransfer,
    relay_solution_from_routes,
)
from uav_dispatch.relay_validation import evaluate_relay_solution
from uav_dispatch.validation import evaluate_solution


def _problem(
    *tasks: Task,
    drone_count: int = 2,
    speed: float = 1.0,
    max_tasks_per_drone: int | None = None,
) -> Problem:
    return Problem(
        tasks,
        drone_count=drone_count,
        max_tasks_per_drone=(
            len(tasks) if max_tasks_per_drone is None else max_tasks_per_drone
        ),
        capacity=2,
        speed_km_per_min=speed,
    )


# ---------------------------------------------------------------------------
# Case 1: a direct-only RelaySolution must agree exactly with the plain model.
# ---------------------------------------------------------------------------
def test_case1_direct_only_matches_plain_model():
    problem = _problem(
        Task(1, Point(1, 0), Point(3, 0), 10),
        Task(2, Point(0, 1), Point(0, 3), 10),
        Task(3, Point(2, 0), Point(2, 2), 20),
    )
    routes = ((1, -1, 3, -3), (2, -2))
    station = RelayStation(0, Point(5, 5), -3)
    relay_eval = evaluate_relay_solution(
        problem, relay_solution_from_routes(routes, stations=(station,))
    )
    plain = evaluate_solution(problem, routes)

    assert relay_eval.valid, relay_eval.violations
    assert relay_eval.score == plain.score
    assert relay_eval.delivery_times_min == plain.delivery_times_min
    assert relay_eval.total_lateness_min == plain.total_lateness_min
    assert relay_eval.relay_count == 0
    assert relay_eval.direct_task_count == 3


# ---------------------------------------------------------------------------
# Case 2: A pickup -> A relay drop -> B relay pick -> B delivery, valid.
# ---------------------------------------------------------------------------
def _case2_solution():
    problem = _problem(
        Task(1, Point(1, 0), Point(6, 0), 100),
        Task(2, Point(0, 1), Point(0, 2), 100),
    )
    station = RelayStation(0, Point(0, 2), -2)
    relay = RelayTransfer(0, 1, 0, 0, 1, 1, 2)
    routes = ((1,), (2, -2, -1))
    return problem, RelaySolution(routes, (relay,), (station,))


def test_case2_async_pickup_drop_pick_delivery():
    problem, solution = _case2_solution()
    result = evaluate_relay_solution(problem, solution)

    assert result.valid, result.violations
    assert result.relay_count == 1
    # A: depot->P1 (1) -> station (sqrt5) => drop 3.236
    # B: depot->P2 (1) -> D2 (1) => 2.0, then station, then D1 (sqrt40).
    assert result.delivery_times_min[2] == pytest.approx(2.0)
    assert result.delivery_times_min[1] == pytest.approx(
        1 + 5 ** 0.5 + 40 ** 0.5
    )
    assert result.score.late_count == 0
    timing = result.relay_timings[0]
    assert timing.drop_time_min == pytest.approx(1 + 5 ** 0.5)
    assert timing.pick_time_min == pytest.approx(1 + 5 ** 0.5)
    assert timing.delivery_time_min == pytest.approx(1 + 5 ** 0.5 + 40 ** 0.5)


# ---------------------------------------------------------------------------
# Case 3: A drops first and leaves; B arrives later and picks without waiting.
# ---------------------------------------------------------------------------
def test_case3_second_drone_arrives_later_no_wait():
    problem = _problem(
        Task(1, Point(1, 0), Point(6, 0), 100),
        Task(2, Point(9, 0), Point(9, 1), 100),
    )
    station = RelayStation(0, Point(9, 1), -2)
    relay = RelayTransfer(0, 1, 0, 0, 1, 1, 2)
    solution = RelaySolution(((1,), (2, -2, -1)), (relay,), (station,))

    result = evaluate_relay_solution(problem, solution)

    assert result.valid, result.violations
    timing = result.relay_timings[0]
    assert timing.drop_time_min == pytest.approx(1 + 65 ** 0.5)
    assert timing.pick_time_min == pytest.approx(10.0)
    assert timing.receiver_wait_min == pytest.approx(0.0)
    assert timing.storage_time_min == pytest.approx(
        10.0 - (1 + 65 ** 0.5)
    )


# ---------------------------------------------------------------------------
# Case 4: B arrives first at the station and waits for the drop.
# ---------------------------------------------------------------------------
def test_case4_second_drone_arrives_first_waits():
    problem, solution = _case2_solution()
    result = evaluate_relay_solution(problem, solution)

    assert result.valid, result.violations
    timing = result.relay_timings[0]
    ready = 1 + 5 ** 0.5
    assert timing.second_arrival_min == pytest.approx(2.0)
    assert timing.receiver_wait_min == pytest.approx(ready - 2.0)
    assert timing.storage_time_min == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Case 5: the first drone's relay drop releases one unit of load.
# ---------------------------------------------------------------------------
def test_case5_first_drone_drop_releases_capacity():
    problem = _problem(
        Task(1, Point(1, 0), Point(6, 0), 100),
        Task(2, Point(0, 1), Point(0, 2), 100),
        Task(3, Point(3, 0), Point(4, 0), 100),
        drone_count=2,
        max_tasks_per_drone=2,
    )
    station = RelayStation(0, Point(0, 2), -2)
    # A carries task 1 (relay) and task 3 (direct); the drop happens between
    # the two pickups so the load goes 2 -> 1 -> 2 within capacity.
    relay = RelayTransfer(0, 1, 0, 0, 1, 1, 2)
    routes = ((1, 3, -3), (2, -2, -1))
    solution = RelaySolution(routes, (relay,), (station,))

    result = evaluate_relay_solution(problem, solution)

    assert result.valid, result.violations
    assert result.capacity_release_events == 1


# ---------------------------------------------------------------------------
# Case 6: the second drone's relay pick adds one unit of load.
# ---------------------------------------------------------------------------
def test_case6_second_drone_pick_adds_capacity():
    problem, solution = _case2_solution()
    result = evaluate_relay_solution(problem, solution)

    assert result.valid, result.violations
    # B picks task 1 at the station and delivers it, so its load goes 0 -> 1.
    assert result.delivery_times_min[1] > result.relay_timings[0].pick_time_min


# ---------------------------------------------------------------------------
# Case 7: a relay pick that pushes the second drone over capacity is invalid.
# ---------------------------------------------------------------------------
def test_case7_capacity_over_two_invalid():
    problem = _problem(
        Task(1, Point(1, 0), Point(6, 0), 100),
        Task(2, Point(0, 1), Point(10, 1), 100),
        Task(3, Point(0, 2), Point(10, 2), 100),
        Task(4, Point(9, 9), Point(9, 10), 100),
        drone_count=2,
        max_tasks_per_drone=3,
    )
    station = RelayStation(0, Point(9, 10), -4)
    # B already carries tasks 2 and 3 (load 2) when it picks task 1 -> 3 > 2.
    relay = RelayTransfer(0, 1, 0, 0, 1, 1, 2)
    routes = ((1,), (2, 3, -1, -2, -3))
    solution = RelaySolution(routes, (relay,), (station,))

    result = evaluate_relay_solution(problem, solution)

    assert not result.valid
    assert any("超过上限" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Case 8: a relay whose pick depends on a drop that itself depends on the
# pick creates a temporal dependency cycle (temporal infeasibility).
# ---------------------------------------------------------------------------
def _cycle_solution():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(0, 1), Point(0, 2), 100),
    )
    station_a = RelayStation(0, Point(5, 5), 1)
    station_b = RelayStation(1, Point(6, 6), 2)
    relay_a = RelayTransfer(0, 1, 0, 0, 1, 2, 1)
    relay_b = RelayTransfer(1, 2, 1, 1, 0, 2, 1)
    # Drone 0: relay_b pick (gap 1) ... relay_a drop (gap 2)
    # Drone 1: relay_a pick (gap 1) ... relay_b drop (gap 2)
    # Edges: drop_a -> pick_a -> (route) -> drop_b -> pick_b -> (route) -> drop_a
    routes = ((1, 2, -2), (2, 1, -1))
    return problem, RelaySolution(routes, (relay_a, relay_b), (station_a, station_b))


def test_case8_relay_pick_before_drop_is_infeasible():
    problem, solution = _cycle_solution()
    result = evaluate_relay_solution(problem, solution)

    assert not result.valid
    assert any("依赖环" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Case 9: one task with two relay transfers is invalid.
# ---------------------------------------------------------------------------
def test_case9_one_task_two_relays_invalid():
    problem = _problem(
        Task(1, Point(1, 0), Point(6, 0), 100),
        Task(2, Point(0, 1), Point(0, 2), 100),
        Task(3, Point(0, 3), Point(0, 4), 100),
        drone_count=2,
        max_tasks_per_drone=2,
    )
    station = RelayStation(0, Point(0, 2), -2)
    first = RelayTransfer(0, 1, 0, 0, 1, 1, 2)
    duplicate = RelayTransfer(1, 1, 0, 0, 1, 1, 2)
    routes = ((1, 2, -2), (3, -3, -1))
    solution = RelaySolution(routes, (first, duplicate), (station,))

    result = evaluate_relay_solution(problem, solution)

    assert not result.valid
    assert any("多个中继转移" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Case 10: a cross-UAV delivery without a RelayTransfer is invalid.
# ---------------------------------------------------------------------------
def test_case10_cross_uav_delivery_without_relay_invalid():
    problem = _problem(
        Task(1, Point(1, 0), Point(6, 0), 100),
        Task(2, Point(0, 1), Point(0, 2), 100),
    )
    station = RelayStation(0, Point(5, 5), -1)
    # Task 1 is picked by drone 0 but delivered by drone 1 with no transfer.
    routes = ((1,), (2, -2, -1))
    solution = RelaySolution(routes, (), (station,))

    result = evaluate_relay_solution(problem, solution)

    assert not result.valid
    assert any("未参与中继" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Case 11: a direct duplicate plus a relay duplicate for the same task.
# ---------------------------------------------------------------------------
def test_case11_direct_and_relay_duplicate_invalid():
    problem = _problem(
        Task(1, Point(1, 0), Point(6, 0), 100),
        Task(2, Point(0, 1), Point(0, 2), 100),
        drone_count=2,
        max_tasks_per_drone=2,
    )
    station = RelayStation(0, Point(0, 2), -2)
    relay = RelayTransfer(0, 1, 0, 0, 1, 1, 2)
    # Task 1 is fully served on drone 0 (direct) AND also has a transfer to
    # drone 1: duplicate ownership.
    routes = ((1, -1), (2, -2))
    solution = RelaySolution(routes, (relay,), (station,))

    result = evaluate_relay_solution(problem, solution)

    assert not result.valid


# ---------------------------------------------------------------------------
# Case 12: a temporal relay dependency cycle is invalid (Kahn detection).
# ---------------------------------------------------------------------------
def test_case12_relay_dependency_cycle_invalid():
    problem, solution = _cycle_solution()
    result = evaluate_relay_solution(problem, solution)

    assert not result.valid
    assert result.score.late_count == 0  # infeasible, never scored as late
    assert any("依赖环" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Case 13: only positive pickup events count toward the per-drone K limit.
# ---------------------------------------------------------------------------
def test_case13_task_count_counts_only_pickups():
    problem = _problem(
        Task(1, Point(1, 0), Point(6, 0), 100),
        Task(2, Point(0, 1), Point(0, 2), 100),
        Task(3, Point(0, 3), Point(0, 4), 100),
        drone_count=2,
        max_tasks_per_drone=2,
    )
    station = RelayStation(0, Point(0, 2), -2)
    relay = RelayTransfer(0, 1, 0, 0, 1, 1, 4)
    # Drone 1 picks up tasks 2 and 3 (2 tasks) and also delivers relay task 1.
    # Pickup-owner counting keeps drone 1 at 2 <= K=2; counting deliveries
    # would wrongly report 3 > 2.
    routes = ((1,), (2, 3, -2, -3, -1))
    solution = RelaySolution(routes, (relay,), (station,))

    result = evaluate_relay_solution(problem, solution)

    assert result.valid, result.violations


# ---------------------------------------------------------------------------
# Case 14/15: a task may not use its own pickup or delivery node as station.
# ---------------------------------------------------------------------------
def test_case14_own_pickup_as_station_invalid():
    problem = _problem(
        Task(1, Point(1, 0), Point(6, 0), 100),
        Task(2, Point(0, 1), Point(0, 2), 100),
    )
    station = RelayStation(0, Point(1, 0), 1)  # at P1, task 1's own pickup
    relay = RelayTransfer(0, 1, 0, 0, 1, 1, 2)
    solution = RelaySolution(((1,), (2, -2, -1)), (relay,), (station,))

    result = evaluate_relay_solution(problem, solution)

    assert not result.valid
    assert any("取件点重合" in item for item in result.violations)


def test_case15_own_delivery_as_station_invalid():
    problem = _problem(
        Task(1, Point(1, 0), Point(6, 0), 100),
        Task(2, Point(0, 1), Point(0, 2), 100),
    )
    station = RelayStation(0, Point(6, 0), -1)  # at D1, task 1's own delivery
    relay = RelayTransfer(0, 1, 0, 0, 1, 1, 2)
    solution = RelaySolution(((1,), (2, -2, -1)), (relay,), (station,))

    result = evaluate_relay_solution(problem, solution)

    assert not result.valid
    assert any("送达点重合" in item for item in result.violations)


# ---------------------------------------------------------------------------
# Case 16: direct late + pickup on time -> relay on time (direct rescue).
# ---------------------------------------------------------------------------
def _case16_problem():
    return _problem(
        Task(1, Point(1, 0), Point(4, 0), 8),
        Task(3, Point(10, 0), Point(11, 0), 100),
        Task(4, Point(2, 0), Point(2, 1), 100),
        drone_count=2,
        max_tasks_per_drone=2,
    )


def test_case16_direct_rescue_pickup_on_time_delivery_late():
    problem = _case16_problem()
    # Direct: drone 0 does 1 then a long detour (task 3) and only then
    # delivers task 1 at t=18 > deadline 8; pickup is at t=1 (on time).
    direct_routes = ((1, 3, -3, -1), (4, -4))
    direct = evaluate_solution(problem, direct_routes)
    assert direct.score.late_count == 1
    assert direct.delivery_times_min[1] == pytest.approx(18.0)

    # Relay: drone 0 drops task 1 early at the station; drone 1 delivers it.
    station = RelayStation(0, Point(2, 0), 4)
    relay = RelayTransfer(0, 1, 0, 0, 1, 1, 2)
    relay_routes = ((1, 3, -3), (4, -4, -1))
    relay_solution = RelaySolution(relay_routes, (relay,), (station,))
    result = evaluate_relay_solution(problem, relay_solution)

    assert result.valid, result.violations
    assert result.score.late_count == 0
    assert result.delivery_times_min[1] == pytest.approx(6.0)
    assert result.pickup_times_min[1] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Case 17: an upstream relay makes a downstream pickup earlier.
# ---------------------------------------------------------------------------
def _case17_18_problem():
    return _problem(
        Task(1, Point(1, 0), Point(20, 0), 100),
        Task(5, Point(2, 0), Point(3, 0), 100),
        Task(6, Point(4, 0), Point(5, 0), 30),
        Task(2, Point(0, 1), Point(0, 2), 100),
        drone_count=2,
        max_tasks_per_drone=3,
    )


def test_case17_upstream_relay_accelerates_downstream_pickup():
    problem = _case17_18_problem()
    direct_routes = ((1, 5, -5, -1, 6, -6), (2, -2))
    direct = evaluate_solution(problem, direct_routes)
    direct_pickup_6 = None
    # Reconstruct the direct pickup time of task 6 from the route.
    elapsed = 0.0
    previous = None
    for visit in direct_routes[0]:
        elapsed += problem.distance(previous, visit) / problem.speed_km_per_min
        previous = visit
        if visit == 6:
            direct_pickup_6 = elapsed
    assert direct_pickup_6 is not None and direct_pickup_6 > 30.0

    station = RelayStation(0, Point(2, 0), 5)
    relay = RelayTransfer(0, 1, 0, 0, 1, 1, 2)
    relay_routes = ((1, 5, -5, 6, -6), (2, -2, -1))
    relay_solution = RelaySolution(relay_routes, (relay,), (station,))
    result = evaluate_relay_solution(problem, relay_solution)

    assert result.valid, result.violations
    assert result.pickup_times_min[6] == pytest.approx(4.0)
    assert result.pickup_times_min[6] < direct_pickup_6


# ---------------------------------------------------------------------------
# Case 18: an upstream relay rescues a downstream late -> on-time task.
# ---------------------------------------------------------------------------
def test_case18_upstream_relay_rescues_downstream():
    problem = _case17_18_problem()
    direct_routes = ((1, 5, -5, -1, 6, -6), (2, -2))
    direct = evaluate_solution(problem, direct_routes)
    assert direct.score.late_count == 1  # task 6 delivered at t=37 > 30

    station = RelayStation(0, Point(2, 0), 5)
    relay = RelayTransfer(0, 1, 0, 0, 1, 1, 2)
    relay_routes = ((1, 5, -5, 6, -6), (2, -2, -1))
    relay_solution = RelaySolution(relay_routes, (relay,), (station,))
    result = evaluate_relay_solution(problem, relay_solution)

    assert result.valid, result.violations
    assert result.score.late_count == 0
    assert result.delivery_times_min[6] == pytest.approx(5.0)
    assert result.pickup_times_min[6] == pytest.approx(4.0)
    # Task 1 itself is delivered on time too (via the second drone).
    assert result.delivery_times_min[1] < 100.0


# ---------------------------------------------------------------------------
# Case 19/20: two-layer Score semantics (also covered in test_model).
# ---------------------------------------------------------------------------
def test_case19_fewer_late_wins_even_with_more_distance():
    from uav_dispatch import Score

    assert Score(5, 100.0) < Score(6, 1.0)


def test_case20_lateness_never_breaks_a_late_count_tie():
    from uav_dispatch import Score

    assert Score(5, 0.0, 50.0) < Score(5, 0.0, 60.0)


# ---------------------------------------------------------------------------
# Case 21/22: partial-solution evaluation (mid-repair) and the mechanism
# proof that a relay can rescue a downstream task that direct dispatch cannot.
# ---------------------------------------------------------------------------
def test_case21_require_all_tasks_false_allows_partial_evaluation():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(3, 0), Point(4, 0), 100),
        drone_count=2,
        max_tasks_per_drone=2,
    )
    # Partial solution: task 2 not placed anywhere yet (mid-repair state).
    partial = relay_solution_from_routes(((1, -1), ()), stations=())

    complete = evaluate_relay_solution(problem, partial)
    assert not complete.valid  # completeness check rejects the partial state

    relaxed = evaluate_relay_solution(
        problem, partial, require_all_tasks=False
    )
    assert relaxed.valid, relaxed.violations
    assert relaxed.score.late_count == 0


def test_case22_fresh_relay_options_generated_on_partial_solution():
    """Fresh relay insertion options must exist on a partial solution.

    This is the exact situation multi-mode repair is in (tasks still
    unplaced), and it used to silently return zero options because the full
    completeness check invalidated every candidate.
    """
    from uav_dispatch.relay_candidates import relay_insertion_options

    problem = _problem(
        Task(1, Point(0, 0), Point(4, 0), 100),
        Task(2, Point(5, 0), Point(6, 0), 100),
        Task(3, Point(1, 0), Point(2, 0), 100),
        Task(4, Point(7, 0), Point(8, 0), 100),
        drone_count=2,
        max_tasks_per_drone=4,
    )
    partial = relay_solution_from_routes(((1, -1, 2, -2), ()), stations=())
    station = RelayStation(0, Point(2, 0), 3)
    options = relay_insertion_options(
        problem,
        partial,
        4,
        stations=(station,),
        first_drone_limit=2,
        second_drone_limit=2,
        pickup_gap_limit=3,
        drop_gap_limit=3,
        pick_gap_limit=3,
        delivery_gap_limit=3,
        max_candidates=8,
        max_evaluations=200,
        require_all_tasks=False,
    )
    assert len(options) > 0
    assert all(option.evaluation.valid for option in options)


def test_case23_relay_rescues_downstream_large_detour():
    """Mechanism proof: a relay of a large-detour delivery rescues a
    downstream deadline that the direct route cannot meet."""
    problem = _problem(
        Task(1, Point(0, 10), Point(15, 0), 60),
        Task(2, Point(0, 20), Point(0, 21), 45),
        Task(3, Point(8, 2), Point(8, 1), 60),
        drone_count=2,
        max_tasks_per_drone=2,
    )
    direct = evaluate_solution(problem, ((1, -1, 2, -2), (3, -3)))
    assert direct.score.late_count == 1  # task 2 delivered at t=54 > 45

    station = RelayStation(0, Point(8, 2), 3)
    relay = RelayTransfer(0, 1, 0, 0, 1, 1, 1)
    relay_solution = RelaySolution(
        ((1, 2, -2), (3, -1, -3)), (relay,), (station,)
    )
    result = evaluate_relay_solution(problem, relay_solution)

    assert result.valid, result.violations
    assert result.relay_count == 1
    assert result.score.late_count == 0  # task 2 rescued by the upstream relay
    assert result.delivery_times_min[2] == pytest.approx(42.0114, abs=0.01)
    # Task 1 itself is still delivered on time by the second drone.
    assert result.delivery_times_min[1] < 60.0
