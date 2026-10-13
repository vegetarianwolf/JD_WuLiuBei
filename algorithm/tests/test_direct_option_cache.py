"""Exact cross-iteration option reuse must preserve DIRECT search decisions."""

from dataclasses import replace
from random import Random

import pytest

from uav_dispatch import (
    ALNSConfig, Point, Problem, RelayStation, Task, TransportLeg,
    construct_regret_initial, evaluate_solution, solve_alns,
)
from uav_dispatch.alns import _DirectOptionCache, _RepairCache, _repair
from uav_dispatch.search import RouteEvaluator


def _problem(seed=0, *, drones=2, different_homes=False):
    rng = Random(seed)
    tasks = tuple(
        Task(
            task_id,
            Point(rng.uniform(0, 10), rng.uniform(0, 10)),
            Point(rng.uniform(0, 10), rng.uniform(0, 10)),
            rng.uniform(5, 70),
        )
        for task_id in range(1, 17)
    )
    return Problem(
        tasks, drone_count=drones, max_tasks_per_drone=16 // drones,
        drone_homes=(Point(0, 0), Point(10, 10)) if different_homes else None,
    )


def _assert_same_decisions(baseline, cached):
    assert baseline.routes == cached.routes
    assert baseline.evaluation == cached.evaluation
    assert baseline.iterations == cached.iterations
    for key in (
        "initial_score", "best_update_count", "accepted_solutions",
        "operator_uses", "operator_statistics", "operator_weights",
    ):
        assert baseline.metadata[key] == cached.metadata[key], key
    assert [(row[0], *row[2:]) for row in baseline.metadata["best_trajectory"]] == [
        (row[0], *row[2:]) for row in cached.metadata["best_trajectory"]
    ]


@pytest.mark.parametrize("seed", [3, 11])
@pytest.mark.parametrize("candidate_limit", [None, 12])
@pytest.mark.parametrize("drones", [1, 2])
def test_option_memo_preserves_fixed_iteration_routes_and_every_search_decision(
    seed, candidate_limit, drones,
):
    problem = _problem(seed, drones=drones, different_homes=drones == 2)
    initial = construct_regret_initial(problem, candidate_limit=candidate_limit)
    config = ALNSConfig(
        max_iterations=60, seed=seed, candidate_limit=candidate_limit,
        repair_rank_candidate_limit=8, repair_exact_candidate_limit=12,
        min_destroy_fraction=0.15, max_destroy_fraction=0.30,
    )
    baseline = solve_alns(problem, config=config, initial_routes=initial.routes)
    cached = solve_alns(
        problem, config=replace(config, enable_direct_option_cache=True),
        initial_routes=initial.routes,
    )
    _assert_same_decisions(baseline, cached)
    assert evaluate_solution(problem, cached.routes, _force_full=True).valid
    assert cached.metadata["direct_option_cache_active"] is True
    assert cached.metadata["direct_option_cache_hits"] > 0
    assert cached.metadata["direct_option_cache_size"] <= (
        cached.metadata["direct_option_cache_maxsize"]
    )
    assert cached.metadata["profiling"]["route_delta_evaluation_count"] < (
        baseline.metadata["profiling"]["route_delta_evaluation_count"]
    )


def test_repair_reuses_complete_options_after_new_pass_without_recomputing():
    problem = _problem(different_homes=True)
    initial = construct_regret_initial(problem, candidate_limit=12)
    removed = {1, 4, 7, 10}
    partial = tuple(
        tuple(visit for visit in route if abs(visit) not in removed)
        for route in initial.routes
    )
    memo = _DirectOptionCache(problem)
    evaluator = RouteEvaluator(problem)
    first = _repair(
        problem, evaluator, partial, removed, "regret3", 12,
        repair_rank_candidate_limit=8, repair_exact_candidate_limit=12,
        direct_option_cache=memo,
    )
    delta_calls = evaluator.counters.route_delta_evaluation_count
    misses = memo.misses
    second = _repair(
        problem, evaluator, partial, removed, "regret3", 12,
        repair_rank_candidate_limit=8, repair_exact_candidate_limit=12,
        direct_option_cache=memo,
    )
    assert first == second
    assert memo.hits > 0
    assert memo.misses == misses
    assert evaluator.counters.route_delta_evaluation_count == delta_calls


def test_memo_separates_route_index_home_limits_option_count_and_snapshot():
    problem = _problem(different_homes=True)
    memo = _DirectOptionCache(problem)
    evaluator = RouteEvaluator(problem)
    routes = ((1, -1), (1, -1))
    queries = [
        (routes, 0, 8, 1),
        (routes, 0, 8, 3),
        (routes, 0, None, 3),
        (routes, 1, 8, 3),
        (((1, -1, 3, -3), routes[1]), 0, 8, 3),
    ]
    for snapshots, route_index, limit, option_count in queries:
        expected = _RepairCache(2).direct_options(
            problem, evaluator, snapshots, 2, route_index, limit, option_count, None,
        )
        first = _RepairCache(2, memo).direct_options(
            problem, evaluator, snapshots, 2, route_index, limit, option_count, None,
        )
        calls = evaluator.counters.route_delta_evaluation_count
        reused = _RepairCache(2, memo).direct_options(
            problem, evaluator, snapshots, 2, route_index, limit, option_count, None,
        )
        assert first == reused == expected
        assert evaluator.counters.route_delta_evaluation_count == calls
        assert all(option.route_index == route_index for option in reused)
    assert memo.misses == len(queries)
    assert memo.hits == len(queries)
    assert len(memo.data) == len(queries)


def test_empty_option_result_is_memoized_and_fifo_eviction_is_bounded():
    problem = _problem()
    full = tuple(visit for task_id in range(1, 9) for visit in (task_id, -task_id))
    memo = _DirectOptionCache(problem, maxsize=2)
    evaluator = RouteEvaluator(problem)
    for task_id in (9, 10, 11):
        result = _RepairCache(2, memo).direct_options(
            problem, evaluator, (full, ()), task_id, 0, 12, 3, None,
        )
        assert result == ()
        assert len(memo.data) <= 2
    assert memo.evictions == 1
    calls = evaluator.counters.route_delta_evaluation_count
    assert _RepairCache(2, memo).direct_options(
        problem, evaluator, (full, ()), 11, 0, 12, 3, None,
    ) == ()
    assert memo.hits == 1
    assert evaluator.counters.route_delta_evaluation_count == calls
    _RepairCache(2, memo).direct_options(
        problem, evaluator, (full, ()), 9, 0, 12, 3, None,
    )
    assert memo.misses == 4
    assert memo.evictions == 2


def test_solve_scoped_memo_refuses_another_problem():
    original = _problem(seed=1)
    other = _problem(seed=2)
    with pytest.raises(ValueError, match="不能跨问题复用"):
        _RepairCache(2, _DirectOptionCache(original)).direct_options(
            other, RouteEvaluator(other), ((), ()), 1, 0, 12, 3, None,
        )


@pytest.mark.parametrize("flag", ["enable_direct_option_cache", "enable_budget_cooling"])
def test_direct_experiments_are_inactive_during_actual_relay_search(flag):
    tasks = (
        Task(1, Point(-4, 0), Point(4, 0), 20),
        Task(2, Point(4, 1), Point(-4, 1), 25),
        Task(3, Point(2, 0), Point(3, 0), 20),
    )
    registry = {}
    for task in tasks:
        leg_id = 4 + 2 * (task.id - 1)
        registry[leg_id] = TransportLeg(leg_id, task.id, "RELAY_IN", 1)
        registry[leg_id + 1] = TransportLeg(leg_id + 1, task.id, "RELAY_OUT", 1)
    problem = Problem(
        tasks, drone_count=2, max_tasks_per_drone=2,
        relay_stations=(RelayStation(1, 0, 0),), leg_registry=registry,
        task_relay_candidates={task.id: (1,) for task in tasks},
    )
    initial = ((1, -1, 3, -3), (2, -2))
    config = ALNSConfig(max_iterations=6, seed=7, candidate_limit=12)
    baseline = solve_alns(problem, config=config, initial_routes=initial)
    cached = solve_alns(
        problem, config=replace(config, **{flag: True}),
        initial_routes=initial,
    )
    _assert_same_decisions(baseline, cached)
    assert cached.metadata["budget_cooling_active"] is False
    assert cached.metadata["direct_option_cache_active"] is False
    assert cached.metadata["direct_option_cache_hits"] == 0
    assert cached.metadata["direct_option_cache_misses"] == 0
    assert cached.metadata["direct_option_cache_size"] == 0
    assert baseline.metadata["profiling"] == cached.metadata["profiling"]


def test_option_cache_default_remains_disabled():
    assert ALNSConfig().enable_direct_option_cache is False
