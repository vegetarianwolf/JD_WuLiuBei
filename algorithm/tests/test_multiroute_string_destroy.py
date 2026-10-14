"""Whole-task string removal frees slots without breaking pickup semantics."""

from dataclasses import replace
from random import Random

import pytest

from uav_dispatch import (
    ALNSConfig, Point, Problem, RelayStation, Task, TransportLeg,
    construct_regret_initial, destroy_solution, evaluate_solution, solve_alns,
)
from uav_dispatch.alns import DESTROY_OPERATORS, _multiroute_string_tasks
from uav_dispatch.search import RouteEvaluator


def _problem(sizes=(4, 4, 4), *, co_located=False):
    tasks = tuple(
        Task(i, Point(0 if co_located else i, 0),
             Point(0 if co_located else i + .5, 1), 8 + i)
        for i in range(1, sum(sizes) + 1)
    )
    problem = Problem(tasks, drone_count=len(sizes), max_tasks_per_drone=max(sizes))
    routes, first = [], 1
    for size in sizes:
        # Alternate adjacent pairs and capacity-two overlaps, exercising
        # the difference between event contiguity and pickup-task order.
        route = []
        for offset in range(0, size, 2):
            left = first + offset
            if offset + 1 < size:
                route.extend((left, left + 1, -left, -left - 1))
            else:
                route.extend((left, -left))
        routes.append(tuple(route))
        first += size
    return problem, tuple(routes)


@pytest.mark.parametrize("sizes", [(4, 4, 4), (1, 1, 5), (0, 2, 3), (5,), (0, 0, 4)])
@pytest.mark.parametrize("seed", [0, 3, 11])
def test_strings_remove_exact_complete_pairs_and_preserve_route_constraints(sizes, seed):
    problem, routes = _problem(sizes)
    assert evaluate_solution(problem, routes, _force_full=True).valid
    for requested in range(1, len(problem.tasks) + 3):
        partial, removed = destroy_solution(
            problem, RouteEvaluator(problem), routes, "multiroute_string", requested, Random(seed),
        )
        assert len(removed) == len(set(removed)) == min(requested, len(problem.tasks))
        selected_routes = []
        for index, (old, reduced) in enumerate(zip(routes, partial)):
            assert reduced == tuple(v for v in old if abs(v) not in removed)
            order = [v for v in old if v > 0]
            positions = [i for i, task in enumerate(order) if task in removed]
            if positions:
                selected_routes.append(index)
                assert positions == list(range(positions[0], positions[-1] + 1))
            picked, onboard = set(), set()
            for visit in reduced:
                if visit > 0:
                    assert visit not in picked
                    picked.add(visit)
                    onboard.add(visit)
                    assert len(onboard) <= problem.capacity
                else:
                    assert -visit in onboard
                    onboard.remove(-visit)
            assert not onboard
        if requested >= 2 and sum(bool(route) for route in routes) >= 2:
            assert len(selected_routes) >= 2
        for task in problem.task_ids:
            visits = [v for route in partial for v in route]
            expected = int(task not in removed)
            assert visits.count(task) == visits.count(-task) == expected


def test_two_normal_route_strings_are_balanced_and_spatially_nearby():
    problem, routes = _problem((4, 4, 4))
    # Pin the spatial seed to task 1; routes 0 and 1 are the nearest routes.
    class SeedOne(Random):
        def choice(self, values):
            assert 1 in values
            return 1
    partial, removed = destroy_solution(
        problem, RouteEvaluator(problem), routes, "multiroute_string", 5, SeedOne(2),
    )
    sizes = [sum(task in removed for task in route if task > 0) for route in routes]
    assert sizes == [3, 2, 0]
    assert 1 in removed
    assert partial[2] == routes[2]


def test_seed_route_and_seed_are_retained_when_all_spatial_gaps_tie():
    problem, routes = _problem((4, 4, 4), co_located=True)
    class SeedTwelve(Random):
        def choice(self, values):
            return 12
    _, removed = destroy_solution(
        problem, RouteEvaluator(problem), routes, "multiroute_string", 2, SeedTwelve(2),
    )
    assert 12 in removed
    assert sum(task >= 9 for task in removed) == 1
    assert sum(task <= 4 for task in removed) == 1


def test_direct_helper_bounds_oversized_requests_and_handles_empty_routes():
    problem, routes = _problem((1, 1, 5))
    removed = _multiroute_string_tasks(problem, routes, problem.task_ids, 999, Random(3))
    assert len(removed) == len(set(removed)) == len(problem.tasks)
    assert set(removed) == set(problem.task_ids)
    assert _multiroute_string_tasks(problem, ((), (), ()), problem.task_ids, 999, Random(3)) == []
    assert _multiroute_string_tasks(problem, routes, problem.task_ids, 0, Random(3)) == []
    assert _multiroute_string_tasks(problem, ((), (), ()), (), 999, Random(3)) == []


def _decisions(result):
    return (
        result.routes, result.evaluation, result.iterations,
        result.metadata["operator_statistics"], result.metadata["operator_weights"],
        result.metadata["operator_uses"], result.metadata["accepted_solutions"],
        [(p[0], *p[2:]) for p in result.metadata["best_trajectory"]],
    )


def test_single_drone_flag_does_not_change_any_search_decision():
    problem, initial = _problem((8,))
    config = ALNSConfig(max_iterations=25, seed=3, relay_enabled=False)
    baseline = solve_alns(problem, config=config, initial_routes=initial)
    candidate = solve_alns(
        problem, config=replace(config, enable_multiroute_string_destroy=True), initial_routes=initial,
    )
    assert _decisions(baseline) == _decisions(candidate)
    assert candidate.metadata["multiroute_string_active"] is False
    assert "destroy:multiroute_string" not in candidate.metadata["operator_uses"]


def test_actual_relay_flag_is_inactive_and_preserves_every_search_decision():
    tasks = (Task(1, Point(-3, 0), Point(3, 0), 30),
             Task(2, Point(3, 1), Point(-3, 1), 30))
    registry = {
        3: TransportLeg(3, 1, "RELAY_IN", 1),
        4: TransportLeg(4, 1, "RELAY_OUT", 1),
        5: TransportLeg(5, 2, "RELAY_IN", 1),
        6: TransportLeg(6, 2, "RELAY_OUT", 1),
    }
    problem = Problem(
        tasks, drone_count=2, max_tasks_per_drone=2,
        relay_stations=(RelayStation(1, 0, 0),), leg_registry=registry,
        task_relay_candidates={1: (1,), 2: (1,)},
    )
    config = ALNSConfig(max_iterations=5, seed=11)
    initial = ((1, -1), (2, -2))
    baseline = solve_alns(problem, config=config, initial_routes=initial)
    candidate = solve_alns(
        problem, config=replace(config, enable_multiroute_string_destroy=True), initial_routes=initial,
    )
    assert _decisions(baseline) == _decisions(candidate)
    assert candidate.metadata["multiroute_string_active"] is False
    assert baseline.metadata["profiling"] == candidate.metadata["profiling"]


def test_flag_leaves_initial_construction_unchanged_and_adds_independent_weight():
    problem, _ = _problem((4, 4, 4))
    baseline = solve_alns(problem, config=ALNSConfig(max_iterations=0, enable_terminal_polish=False))
    candidate = solve_alns(problem, config=ALNSConfig(
        max_iterations=0, enable_multiroute_string_destroy=True, enable_terminal_polish=False,
    ))
    assert candidate.routes == baseline.routes
    assert candidate.evaluation == baseline.evaluation
    assert candidate.metadata["initial_score"] == baseline.metadata["initial_score"]
    assert candidate.metadata["multiroute_string_active"] is True
    assert candidate.metadata["operator_uses"]["destroy:multiroute_string"] == 0
    assert candidate.metadata["operator_weights"]["destroy:multiroute_string"] == 1
    assert len(DESTROY_OPERATORS) == 9
    assert len(candidate.metadata["operator_uses"]) == 15


def test_small_search_remains_valid_and_retains_a_complete_incumbent():
    problem, initial = _problem((4, 4, 4))
    config = ALNSConfig(max_iterations=35, seed=3, enable_multiroute_string_destroy=True,
                        relay_enabled=False, min_destroy_fraction=.2, max_destroy_fraction=.4)
    result = solve_alns(problem, config=config, initial_routes=initial)
    assert result.metadata["operator_uses"]["destroy:multiroute_string"] > 0
    assert evaluate_solution(problem, result.routes, _force_full=True).valid
    assert result.evaluation.score <= evaluate_solution(problem, initial, _force_full=True).score


def test_string_destroy_default_is_disabled():
    assert ALNSConfig().enable_multiroute_string_destroy is False
