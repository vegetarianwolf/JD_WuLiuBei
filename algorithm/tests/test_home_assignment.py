"""Whole-route assignment preserves constraints and canonical incumbents."""

from dataclasses import replace
from itertools import permutations
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, Task
from uav_dispatch.home_assignment import reassign_route_homes
from uav_dispatch.model import Score
from uav_dispatch.relay import build_relay_network
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.validation import evaluate_solution


def _misassigned_problem():
    tasks = (
        Task(1, Point(10, 0), Point(11, 0), 2),
        Task(2, Point(0, 0), Point(1, 0), 2),
    )
    problem = Problem(
        tasks, drone_count=2, max_tasks_per_drone=1, capacity=2,
        speed_km_per_min=1, drone_homes=(Point(0, 0), Point(10, 0)),
    )
    return problem, ((1, -1), (2, -2))


def test_full_fleet_assignment_improves_on_time_count_without_changing_routes():
    problem, routes = _misassigned_problem()
    statistics = {}
    before = evaluate_solution(problem, routes, _force_full=True)
    result = reassign_route_homes(problem, RouteEvaluator(problem), routes, statistics=statistics)
    after = evaluate_solution(problem, result, _force_full=True)

    assert before.score.late_count == 2
    assert after.valid and after.score.late_count == 0
    assert result == tuple(reversed(routes))
    assert sorted(result) == sorted(routes)
    assert problem.drone_homes == (Point(0, 0), Point(10, 0))
    assert statistics["improvements"] == 1
    assert statistics["reassigned_routes"] == 2
    assert statistics["canonical_validations"] == 2


def test_dp_matches_exhaustive_assignment_with_repeated_homes_and_empty_route():
    problem = Problem(
        tuple(Task(i, Point(x, 0), Point(x + 1, 0), deadline)
              for i, x, deadline in ((1, 10, 2), (2, 0, 3), (3, 6, 8))),
        drone_count=4, max_tasks_per_drone=1, speed_km_per_min=1,
        drone_homes=(Point(0, 0), Point(0, 0), Point(10, 0), Point(7, 0)),
    )
    routes = ((1, -1), (3, -3), (), (2, -2))
    exact_score = min(evaluate_solution(problem, route_order, _force_full=True).score
                      for route_order in permutations(routes))
    result = reassign_route_homes(problem, RouteEvaluator(problem), routes)
    assert evaluate_solution(problem, result, _force_full=True).score == exact_score


def test_identical_homes_are_inactive_and_do_not_score(monkeypatch):
    problem, routes = _misassigned_problem()
    problem = replace(problem, drone_homes=(Point(0, 0), Point(0, 0)))
    evaluator = RouteEvaluator(problem)
    monkeypatch.setattr(evaluator, "evaluate", lambda *a, **k: pytest.fail("inactive scan"))
    statistics = {}
    assert reassign_route_homes(problem, evaluator, routes, statistics=statistics) == routes
    assert statistics == {"skipped": 1}


@pytest.mark.parametrize("routes", [((1, -1), (1, -1)), ((-1, 1), (2, -2)), ((1, -1), ())])
def test_invalid_complete_inputs_are_rejected(routes):
    problem, _ = _misassigned_problem()
    with pytest.raises(ValueError, match="非法初解"):
        reassign_route_homes(problem, RouteEvaluator(problem), routes)


def test_route_count_and_wrong_problem_evaluator_are_rejected():
    problem, routes = _misassigned_problem()
    with pytest.raises(ValueError, match="每架无人机"):
        reassign_route_homes(problem, RouteEvaluator(problem), routes[:1])
    other, _ = _misassigned_problem()
    with pytest.raises(ValueError, match="同一个问题"):
        reassign_route_homes(problem, RouteEvaluator(other), routes)


def test_expired_budget_returns_without_scoring_or_validation(monkeypatch):
    problem, routes = _misassigned_problem()
    monkeypatch.setattr("uav_dispatch.home_assignment.evaluate_solution", lambda *a, **k: pytest.fail("expired validation"))
    statistics = {}
    result = reassign_route_homes(problem, RouteEvaluator(problem), routes,
                                  deadline=perf_counter() - 1, statistics=statistics)
    assert result == routes
    assert statistics == {"deadline_stops": 1}


def test_expiry_during_matrix_construction_does_not_validate_candidate(monkeypatch):
    problem, routes = _misassigned_problem()
    ticks = iter((0, 0, 0, 0, 0, 2))
    monkeypatch.setattr("uav_dispatch.home_assignment.perf_counter", lambda: next(ticks))
    statistics = {}
    result = reassign_route_homes(problem, RouteEvaluator(problem), routes,
                                  deadline=1, statistics=statistics)
    assert result == routes
    assert statistics["canonical_validations"] == 1
    assert statistics["deadline_stops"] == 1


def test_expiry_during_final_validation_cannot_commit(monkeypatch):
    problem, routes = _misassigned_problem()
    time = [0.0]
    calls = []
    def checked(*args, **kwargs):
        calls.append(args[1])
        result = evaluate_solution(*args, **kwargs)
        if len(calls) == 2:
            time[0] = 2.0
        return result
    monkeypatch.setattr("uav_dispatch.home_assignment.perf_counter", lambda: time[0])
    monkeypatch.setattr("uav_dispatch.home_assignment.evaluate_solution", checked)
    result = reassign_route_homes(problem, RouteEvaluator(problem), routes, deadline=1)
    assert len(calls) == 2
    assert result == routes


def test_canonical_worsening_rejects_apparent_additive_improvement(monkeypatch):
    problem, routes = _misassigned_problem()
    original = evaluate_solution(problem, routes, _force_full=True)
    def checked(p, candidate, **kwargs):
        result = evaluate_solution(p, candidate, **kwargs)
        if candidate != routes:
            return replace(result, score=original.score + Score(0, 0, 1e-12))
        return result
    monkeypatch.setattr("uav_dispatch.home_assignment.evaluate_solution", checked)
    statistics = {}
    assert reassign_route_homes(problem, RouteEvaluator(problem), routes, statistics=statistics) == routes
    assert statistics["canonical_rejections"] == 1


def test_relay_capable_problem_allows_direct_warmup_but_skips_actual_relay():
    problem, routes = _misassigned_problem()
    network = build_relay_network(problem.tasks, relay_count=1, location_seed=1)
    problem = replace(problem, relay_stations=network.stations,
                      leg_registry=network.leg_registry,
                      task_relay_candidates=network.task_relay_candidates)
    assert reassign_route_homes(problem, RouteEvaluator(problem), routes) == tuple(reversed(routes))
    legs = sorted(problem.leg_registry.values(), key=lambda leg: (leg.task_id, leg.kind))
    inbound = next(leg for leg in legs if leg.task_id == 1 and leg.kind == "RELAY_IN")
    outbound = next(leg for leg in legs if leg.task_id == 1 and leg.kind == "RELAY_OUT")
    relay_routes = ((inbound.id, -inbound.id, 2, -2), (outbound.id, -outbound.id))
    assert evaluate_solution(problem, relay_routes, _force_full=True).valid
    statistics = {}
    assert reassign_route_homes(problem, RouteEvaluator(problem), relay_routes, statistics=statistics) == relay_routes
    assert statistics == {"skipped": 1}


def test_fleet_above_bound_is_skipped():
    problem = Problem(
        (Task(1, Point(10, 0), Point(11, 0), 2),), drone_count=13,
        drone_homes=tuple(Point(i, 0) for i in range(13)),
    )
    routes = ((1, -1),) + ((),) * 12
    statistics = {}
    assert reassign_route_homes(problem, RouteEvaluator(problem), routes, statistics=statistics) == routes
    assert statistics == {"skipped": 1}
