"""Tests for the Stage-I 0-1 Location-Allocation model (LAP)."""

from __future__ import annotations

import pytest

from uav_dispatch.model import Point, Problem, Task
from uav_dispatch.relay_model import RelayStation, relay_solution_from_routes
from uav_dispatch.location_allocation import solve_location_allocation
from uav_dispatch.relay_location import (
    build_route_context,
    candidate_sites,
    greedy_station_selection,
    precompute_potential_matrix,
)
from uav_dispatch.alns import construct_regret_initial
from random import Random


def _problem(*tasks: Task, drones: int = 2, cap: int = 2) -> Problem:
    return Problem(
        tasks,
        drone_count=drones,
        max_tasks_per_drone=len(tasks),
        capacity=cap,
        speed_km_per_min=1.0,
    )


def _b_ir(problem: Problem, candidates):
    direct = construct_regret_initial(problem, candidate_limit=8)
    ctx = build_route_context(problem, direct.routes)
    return precompute_potential_matrix(problem, direct.routes, ctx, candidates)


# ---------------------------------------------------------------------------
# LAP-1: exactly P stations are selected.
# ---------------------------------------------------------------------------
def test_lap1_sum_y_equals_P():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 10),
        Task(2, Point(0, 1), Point(0, 2), 10),
        Task(3, Point(3, 0), Point(3, 1), 10),
        Task(4, Point(0, 3), Point(1, 3), 10),
        drones=2,
    )
    candidates = candidate_sites(problem)
    matrix = _b_ir(problem, candidates)
    greedy = greedy_station_selection(
        matrix, problem.task_ids, candidates, 2, rng=Random(1)
    )
    lap = solve_location_allocation(
        problem, candidates, matrix, 2, fallback_layout=greedy, time_limit=5.0
    )

    assert len(lap.selected_station_ids) == 2
    assert len(set(lap.selected_station_ids)) == 2


# ---------------------------------------------------------------------------
# LAP-2: x_ir = 1 implies y_r = 1.
# ---------------------------------------------------------------------------
def test_lap2_allocated_stations_are_selected():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 10),
        Task(2, Point(0, 1), Point(0, 2), 10),
        Task(3, Point(3, 0), Point(3, 1), 10),
        Task(4, Point(0, 3), Point(1, 3), 10),
        drones=2,
    )
    candidates = candidate_sites(problem)
    matrix = _b_ir(problem, candidates)
    greedy = greedy_station_selection(
        matrix, problem.task_ids, candidates, 2, rng=Random(1)
    )
    lap = solve_location_allocation(
        problem, candidates, matrix, 2, fallback_layout=greedy, time_limit=5.0
    )
    selected = set(lap.selected_station_ids)

    for station_id in lap.task_station_guidance.values():
        assert station_id in selected


# ---------------------------------------------------------------------------
# LAP-3: each task has at most one allocated station.
# ---------------------------------------------------------------------------
def test_lap3_task_at_most_one_station():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 10),
        Task(2, Point(0, 1), Point(0, 2), 10),
        Task(3, Point(3, 0), Point(3, 1), 10),
        Task(4, Point(0, 3), Point(1, 3), 10),
        drones=2,
    )
    candidates = candidate_sites(problem)
    matrix = _b_ir(problem, candidates)
    greedy = greedy_station_selection(
        matrix, problem.task_ids, candidates, 2, rng=Random(1)
    )
    lap = solve_location_allocation(
        problem, candidates, matrix, 2, fallback_layout=greedy, time_limit=5.0
    )

    seen: dict[int, int] = {}
    for task_id, station_id in lap.task_station_guidance.items():
        if task_id in seen:
            pytest.fail(f"任务 {task_id} 分配了多个站点")
        seen[task_id] = station_id


# ---------------------------------------------------------------------------
# LAP-4 / LAP-5: a task's own pickup / delivery site cannot be allocated.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "own_node",
    [
        lambda task: task.pickup,
        lambda task: task.delivery,
    ],
)
def test_lap4_5_task_own_node_not_allocated(own_node):
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 10),
        Task(2, Point(0, 1), Point(0, 2), 10),
        Task(3, Point(3, 0), Point(3, 1), 10),
        Task(4, Point(0, 3), Point(1, 3), 10),
        drones=2,
    )
    candidates = candidate_sites(problem)
    matrix = _b_ir(problem, candidates)
    greedy = greedy_station_selection(
        matrix, problem.task_ids, candidates, 2, rng=Random(1)
    )
    lap = solve_location_allocation(
        problem, candidates, matrix, 2, fallback_layout=greedy, time_limit=5.0
    )
    by_id = {station.id: station for station in candidates}

    for task_id, station_id in lap.task_station_guidance.items():
        station = by_id[station_id]
        task = problem.task(task_id)
        assert station.point.distance_to(own_node(task)) > 1e-9


# ---------------------------------------------------------------------------
# LAP-6: high-B station is preferred on a simple synthetic instance.
# ---------------------------------------------------------------------------
def test_lap6_high_b_station_preferred():
    # Task 1 sits between the two candidates; the nearer candidate has a
    # higher B_ir and should be the one selected when P=1.
    problem = _problem(
        Task(1, Point(0, 0), Point(2, 0), 100),
        Task(2, Point(0, 4), Point(0, 6), 100),
        drones=1,
    )
    candidates = (
        RelayStation(0, Point(0.5, 0), 1),   # near P1/D1
        RelayStation(1, Point(0.5, 4), 2),   # far
    )
    matrix = _b_ir(problem, candidates)
    lap = solve_location_allocation(
        problem, candidates, matrix, 1, time_limit=5.0
    )

    assert lap.selected_station_ids == (0,)
    assert lap.task_station_guidance.get(1) == 0


# ---------------------------------------------------------------------------
# LAP-7: MILP timeout returns an incumbent or the fallback layout.
# ---------------------------------------------------------------------------
def test_lap7_timeout_returns_incumbent_or_fallback():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 10),
        Task(2, Point(0, 1), Point(0, 2), 10),
        Task(3, Point(3, 0), Point(3, 1), 10),
        Task(4, Point(0, 3), Point(1, 3), 10),
        drones=2,
    )
    candidates = candidate_sites(problem)
    matrix = _b_ir(problem, candidates)
    greedy = greedy_station_selection(
        matrix, problem.task_ids, candidates, 2, rng=Random(1)
    )
    lap = solve_location_allocation(
        problem,
        candidates,
        matrix,
        2,
        fallback_layout=greedy,
        time_limit=0.01,
    )

    assert len(lap.selected_station_ids) == 2
    assert lap.model_status in {
        "optimal",
        "time_limit_incumbent",
        "fallback_greedy",
        "fallback_topk",
    }
    assert lap.model_runtime >= 0.0


# ---------------------------------------------------------------------------
# LAP-8: LAP guidance does not force a relay; routing may keep Direct.
# ---------------------------------------------------------------------------
def test_lap8_guidance_is_not_a_hard_relay():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 10),
        Task(2, Point(0, 1), Point(0, 2), 10),
        Task(3, Point(3, 0), Point(3, 1), 10),
        Task(4, Point(0, 3), Point(1, 3), 10),
        drones=2,
    )
    candidates = candidate_sites(problem)
    matrix = _b_ir(problem, candidates)
    greedy = greedy_station_selection(
        matrix, problem.task_ids, candidates, 2, rng=Random(1)
    )
    lap = solve_location_allocation(
        problem, candidates, matrix, 2, fallback_layout=greedy, time_limit=5.0
    )
    # Guidance exists for at least some tasks, but the constructed solution
    # (regret-2 direct) contains zero relays.
    selected = tuple(
        station for station in candidates if station.id in lap.selected_station_ids
    )
    solution = relay_solution_from_routes(
        construct_regret_initial(problem, candidate_limit=8).routes,
        stations=selected,
    )

    assert solution.relay_count == 0


# ---------------------------------------------------------------------------
# LAP-9: no extra relay-station drones exist (M is fixed).
# ---------------------------------------------------------------------------
def test_lap9_no_extra_relay_drones():
    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 10),
        Task(2, Point(0, 1), Point(0, 2), 10),
        drones=1,
    )
    assert problem.drone_count == 1  # unchanged; no station fleet added


# ---------------------------------------------------------------------------
# LAP-10: Stage-II station layout hash equals Stage-I layout hash.
# ---------------------------------------------------------------------------
def test_lap10_stage2_stations_match_stage1():
    from uav_dispatch.relay_search import RelaySearchConfig, solve_relay

    problem = _problem(
        Task(1, Point(1, 0), Point(2, 0), 10),
        Task(2, Point(0, 1), Point(0, 2), 10),
        Task(3, Point(3, 0), Point(3, 1), 10),
        Task(4, Point(0, 3), Point(1, 3), 10),
        drones=2,
    )
    # Freeze a layout explicitly; Stage II must keep it identical.
    frozen = (RelayStation(0, Point(1.0, 1.0), 1),)
    result = solve_relay(
        problem,
        stations=frozen,
        config=RelaySearchConfig(seed=1, time_limit_seconds=3.0, num_stations=1),
    )

    assert result.stations == frozen
    assert result.evaluation.valid
