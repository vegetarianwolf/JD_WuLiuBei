"""Bounded adjacent swaps preserve full pickup/delivery and load semantics."""

from dataclasses import replace
from random import Random

import pytest

import uav_dispatch.alns as alns
from uav_dispatch import (
    ALNSConfig, Point, Problem, RelayStation, Task, TransportLeg,
    evaluate_solution, solve_alns,
)
from uav_dispatch.search import RouteEvaluator


def _positive_problem(deadline=30):
    return Problem((Task(1, Point(10, 0), Point(11, 0), deadline),
                    Task(2, Point(1, 0), Point(2, 0), deadline if deadline > 30 else 3)),
                   drone_count=2, max_tasks_per_drone=2)


@pytest.mark.parametrize("deadline", [30, 1000])
def test_first_improvement_reduces_full_score_without_touching_other_routes(deadline):
    problem = _positive_problem(deadline)
    routes = ((1, 2, -1, -2), ())
    stats = {"probes": 0, "position_checks": 0}
    polished = alns._adjacent_polish_pass(problem, RouteEvaluator(problem), routes, 0, statistics=stats)
    before = evaluate_solution(problem, routes, _force_full=True)
    after = evaluate_solution(problem, polished, _force_full=True)
    assert after.valid
    assert after.score < before.score
    assert polished == ((2, 1, -1, -2), ())
    assert polished[1] is routes[1]
    assert stats == {"probes": 1, "position_checks": 1}
    if deadline == 30:
        assert after.score.late_count < before.score.late_count
    else:
        assert after.score.late_count == before.score.late_count == 0
        assert after.score.distance_km < before.score.distance_km


def test_no_improvement_returns_original_routes_and_caps_checks_to_49():
    tasks = tuple(Task(i, Point(0, 0), Point(0, 0), 100) for i in range(1, 36))
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=35)
    route = tuple(v for i in range(1, 36) for v in (i, -i))
    routes = (route, ())
    stats = {"probes": 0, "position_checks": 0}
    polished = alns._adjacent_polish_pass(problem, RouteEvaluator(problem), routes, 0, statistics=stats)
    assert polished is routes
    assert stats["position_checks"] == 49
    assert stats["probes"] < 49


def test_invalid_load_and_same_task_swaps_are_rejected_before_evaluation(monkeypatch):
    tasks = tuple(Task(i, Point(0, 0), Point(0, 0), 100) for i in range(1, 4))
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=3)
    routes = ((1, 2, -1, 3, -2, -3), ())
    evaluator = RouteEvaluator(problem)
    evaluated = []
    original = evaluator.evaluate
    def capture(route, **kwargs):
        evaluated.append(route)
        return original(route, **kwargs)
    monkeypatch.setattr(evaluator, "evaluate", capture)
    assert alns._adjacent_polish_pass(problem, evaluator, routes, 0) is routes
    assert (1, 2, 3, -1, -2, -3) not in evaluated  # intermediate load would be 3
    for route in evaluated:
        assert evaluate_solution(problem, (route, ()), _force_full=True).valid
    adjacent = ((1, -1, 2, -2, 3, -3), ())
    evaluated.clear()
    assert alns._adjacent_polish_pass(problem, evaluator, adjacent, 0) is adjacent
    assert all(route[0] != -1 for route in evaluated)
    assert all(route.index(i) < route.index(-i) for route in evaluated for i in range(1, 4))


@pytest.mark.parametrize("seed", [0, 3, 11])
def test_random_legal_routes_keep_complete_pairs_capacity_and_all_tasks(seed):
    rng = Random(seed)
    tasks = tuple(Task(i, Point(rng.uniform(0, 10), rng.uniform(0, 10)),
                       Point(rng.uniform(0, 10), rng.uniform(0, 10)), rng.uniform(1, 50))
                  for i in range(1, 9))
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=4)
    routes = []
    for task_ids in (list(range(1, 5)), list(range(5, 9))):
        unpicked, onboard, route = task_ids[:], [], []
        while unpicked or onboard:
            options = [(False, i) for i in onboard]
            if len(onboard) < 2:
                options += [(True, i) for i in unpicked]
            pickup, task_id = rng.choice(options)
            if pickup:
                unpicked.remove(task_id)
                onboard.append(task_id)
                route.append(task_id)
            else:
                onboard.remove(task_id)
                route.append(-task_id)
        routes.append(tuple(route))
    routes = tuple(routes)
    for index in range(2):
        polished = alns._adjacent_polish_pass(problem, RouteEvaluator(problem), routes, index)
        checked = evaluate_solution(problem, polished, _force_full=True)
        assert checked.valid
        assert checked.score <= evaluate_solution(problem, routes, _force_full=True).score
        assert polished[1-index] == routes[1-index]
        assert sorted(polished[index]) == sorted(routes[index])


def test_interval_and_round_robin_are_deterministic_and_do_not_touch_initial(monkeypatch):
    problem = _positive_problem(1000)
    initial = ((1, -1), (2, -2))
    observed = []
    original = alns._adjacent_polish_pass
    def capture(problem, evaluator, routes, route_index, **kwargs):
        observed.append(route_index)
        return original(problem, evaluator, routes, route_index, **kwargs)
    monkeypatch.setattr(alns, "_adjacent_polish_pass", capture)
    cfg = ALNSConfig(max_iterations=120, seed=3, relay_enabled=False, enable_adjacent_polish=True)
    result = solve_alns(problem, config=cfg, initial_routes=initial)
    assert observed == [0, 1, 0]
    assert result.metadata["adjacent_polish_passes"] == 3
    assert result.metadata["adjacent_polish_position_checks"] <= 3 * 49
    assert result.metadata["initial_score"] == (
        evaluate_solution(problem, initial, _force_full=True).score.late_count,
        evaluate_solution(problem, initial, _force_full=True).score.total_lateness_min,
        evaluate_solution(problem, initial, _force_full=True).score.distance_km,
    )


def _decisions(result):
    return (result.routes, result.evaluation, result.iterations,
            result.metadata["operator_statistics"], result.metadata["operator_weights"],
            result.metadata["operator_uses"], result.metadata["accepted_solutions"],
            [(p[0], *p[2:]) for p in result.metadata["best_trajectory"]])


def test_single_drone_flag_remains_inactive_and_preserves_search():
    original = _positive_problem()
    problem = Problem(original.tasks, drone_count=1, max_tasks_per_drone=2)
    initial = ((1, 2, -1, -2),)
    cfg = ALNSConfig(max_iterations=45, seed=3, relay_enabled=False)
    baseline = solve_alns(problem, config=cfg, initial_routes=initial)
    candidate = solve_alns(problem, config=replace(cfg, enable_adjacent_polish=True), initial_routes=initial)
    assert _decisions(candidate) == _decisions(baseline)
    assert not candidate.metadata["adjacent_polish_active"]
    assert candidate.metadata["adjacent_polish_passes"] == 0


def test_actual_relay_flag_remains_inactive_and_preserves_search():
    tasks = _positive_problem().tasks
    registry = {3: TransportLeg(3, 1, "RELAY_IN", 1), 4: TransportLeg(4, 1, "RELAY_OUT", 1),
                5: TransportLeg(5, 2, "RELAY_IN", 1), 6: TransportLeg(6, 2, "RELAY_OUT", 1)}
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=2,
                      relay_stations=(RelayStation(1, 5, 0),), leg_registry=registry,
                      task_relay_candidates={1: (1,), 2: (1,)})
    initial = ((1, -1), (2, -2))
    cfg = ALNSConfig(max_iterations=40, seed=3)
    baseline = solve_alns(problem, config=cfg, initial_routes=initial)
    candidate = solve_alns(problem, config=replace(cfg, enable_adjacent_polish=True), initial_routes=initial)
    assert _decisions(candidate) == _decisions(baseline)
    assert not candidate.metadata["adjacent_polish_active"]
    assert candidate.metadata["adjacent_polish_probes"] == 0
    assert candidate.metadata["profiling"] == baseline.metadata["profiling"]


def test_promoted_default_leaves_zero_iteration_construction_unchanged():
    problem = _positive_problem()
    baseline = solve_alns(problem, config=ALNSConfig(max_iterations=0, enable_adjacent_polish=False,
                                                    enable_terminal_polish=False))
    candidate = solve_alns(problem, config=ALNSConfig(max_iterations=0, enable_terminal_polish=False))
    assert candidate.routes == baseline.routes
    assert candidate.evaluation == baseline.evaluation
    assert candidate.metadata["adjacent_polish_passes"] == 0
    assert ALNSConfig().enable_adjacent_polish is True


def test_expired_deadline_does_not_evaluate_any_polish_candidate(monkeypatch):
    problem = _positive_problem()
    evaluator = RouteEvaluator(problem)
    monkeypatch.setattr(alns, "perf_counter", lambda: 2.)
    with pytest.raises(alns._SearchDeadlineReached):
        alns._adjacent_polish_pass(problem, evaluator, ((1, 2, -1, -2), ()), 0, deadline=1.)
    assert evaluator.counters.route_full_evaluation_count == 0
