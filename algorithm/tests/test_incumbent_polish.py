"""Bounded terminal composition keeps canonical incumbents and deadlines."""
from random import Random

import pytest

import uav_dispatch.incumbent_polish as polish
from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg, evaluate_solution
from uav_dispatch.search import RouteEvaluator


def problem_and_routes():
    tasks = tuple(Task(i, Point(x, 0), Point(x, 0), 15.)
                  for i, x in enumerate((10., -11., -10., 11.), 1))
    return Problem(tasks, drone_count=2, max_tasks_per_drone=2), ((1, -1, 2, -2), (3, -3, 4, -4))


def test_terminal_tail_recovers_late_tasks_and_never_changes_task_count():
    problem, routes = problem_and_routes()
    stats = {}
    changed = polish.polish_direct_incumbent(problem, RouteEvaluator(problem), routes, statistics=stats)
    before, after = (evaluate_solution(problem, r, _force_full=True) for r in (routes, changed))
    assert after.valid and after.score < before.score
    assert (before.score.late_count, after.score.late_count) == (2, 0)
    assert [sum(v < 0 for v in r) for r in changed] == [2, 2]
    assert stats["home_calls"] == 2 and stats["tail_calls"] == 2
    assert stats["tail_stagnation_stops"] == 1
    assert all(type(v) is int for v in stats.values())


def test_home_assignment_uses_existing_home_slots_and_complete_canonical_score():
    problem = Problem((Task(1, Point(10, 0), Point(10, 0), 1.),
                       Task(2, Point(-10, 0), Point(-10, 0), 1.)),
                      drone_count=2, max_tasks_per_drone=1,
                      drone_homes=(Point(-10, 0), Point(10, 0)))
    routes = ((1, -1), (2, -2))
    changed = polish.polish_direct_incumbent(problem, RouteEvaluator(problem), routes)
    assert changed == ((2, -2), (1, -1))
    checked = evaluate_solution(problem, changed, _force_full=True)
    assert checked.valid and checked.score.late_count == 0 and checked.score.distance_km == 0


def test_no_improvement_stops_tail_after_one_round_and_still_attempts_final_home(monkeypatch):
    problem, routes = problem_and_routes()
    events = []
    def home(p, e, r, **kwargs):
        events.append("home")
        return r
    def tail(p, e, r, **kwargs):
        events.append("tail")
        assert kwargs["max_pairs"] == 28
        return r
    monkeypatch.setattr(polish, "reassign_route_homes", home)
    monkeypatch.setattr(polish, "bounded_tail_exchange", tail)
    assert polish.polish_direct_incumbent(problem, RouteEvaluator(problem), routes, max_pairs=999) == routes
    assert events == ["home", "tail", "home"]


def test_fixed_maximum_two_rounds_and_56_pairs_with_large_request(monkeypatch):
    tasks = tuple(Task(i, Point(0, 0), Point(0, 0), 100.) for i in range(1, 17))
    problem = Problem(tasks, drone_count=8, max_tasks_per_drone=2)
    routes = tuple((i, -i, i + 1, -(i + 1)) for i in range(1, 17, 2))
    requests = []
    def tail(p, e, r, **kwargs):
        requests.append(kwargs["max_pairs"])
        # Force the composition to exercise its count cap independently of
        # the already separately tested primitive's scoring contract.
        return r[1:] + r[:1]
    monkeypatch.setattr(polish, "bounded_tail_exchange", tail)
    polish.polish_direct_incumbent(problem, RouteEvaluator(problem), routes, max_pairs=999)
    assert requests == [28, 28] and sum(requests) == 56


def test_expired_deadline_never_calls_primitives_or_validation(monkeypatch):
    problem, routes = problem_and_routes()
    monkeypatch.setattr(polish, "perf_counter", lambda: 2.)
    monkeypatch.setattr(polish, "evaluate_solution", lambda *a, **k: pytest.fail("expired validation"))
    assert polish.polish_direct_incumbent(problem, RouteEvaluator(problem), routes, deadline=1.) == routes


def test_overdue_child_result_is_rejected_but_prior_checkpoint_survives(monkeypatch):
    problem, routes = problem_and_routes()
    first = ((1, -1, 4, -4), (3, -3, 2, -2))
    now, calls = [0.], []
    def home(p, e, r, **kwargs):
        calls.append("home")
        assert evaluate_solution(p, first, _force_full=True).score < evaluate_solution(p, r, _force_full=True).score
        return first
    def tail(p, e, r, **kwargs):
        calls.append("tail")
        now[0] = 2.
        return tuple(reversed(first))
    monkeypatch.setattr(polish, "perf_counter", lambda: now[0])
    monkeypatch.setattr(polish, "reassign_route_homes", home)
    monkeypatch.setattr(polish, "bounded_tail_exchange", tail)
    changed = polish.polish_direct_incumbent(problem, RouteEvaluator(problem), routes, deadline=1.)
    assert changed == first and calls == ["home", "tail"]


@pytest.mark.parametrize("seed", [3, 11])
def test_real_composition_is_deterministic_and_nonworsening_on_underfilled_fleet(seed):
    rng = Random(seed)
    tasks = tuple(Task(i, Point(rng.uniform(-5, 5), rng.uniform(-5, 5)),
                       Point(rng.uniform(-5, 5), rng.uniform(-5, 5)), rng.uniform(3, 25))
                  for i in range(1, 8))
    problem = Problem(tasks, drone_count=3, max_tasks_per_drone=3,
                      drone_homes=(Point(-5, 0), Point(0, 0), Point(5, 0)))
    routes = ((1, 2, -1, -2, 3, -3), (4, -4, 5, -5), (6, -6, 7, -7))
    a = polish.polish_direct_incumbent(problem, RouteEvaluator(problem), routes)
    b = polish.polish_direct_incumbent(problem, RouteEvaluator(problem), routes)
    before, after = (evaluate_solution(problem, r, _force_full=True) for r in (routes, a))
    assert a == b and after.valid and after.score <= before.score
    assert sorted(sum(v < 0 for v in r) for r in a) == [2, 2, 3]


def test_actual_relay_routes_are_rejected_and_invalid_direct_input_is_rejected():
    problem = Problem((Task(1, Point(1, 0), Point(2, 0), 10.),), drone_count=2,
                      relay_stations=(RelayStation(1, 0, 0),),
                      leg_registry={2: TransportLeg(2, 1, "RELAY_IN", 1),
                                    3: TransportLeg(3, 1, "RELAY_OUT", 1)},
                      task_relay_candidates={1: (1,)})
    with pytest.raises(ValueError, match="DIRECT"):
        polish.polish_direct_incumbent(problem, RouteEvaluator(problem), ((2, -2), (3, -3)))
    with pytest.raises(ValueError, match="输入解非法"):
        polish.polish_direct_incumbent(problem, RouteEvaluator(problem), ((-1, 1), ()))


def test_single_drone_remains_unchanged():
    problem = Problem((Task(1, Point(1, 0), Point(2, 0), 10.),))
    routes = ((1, -1),)
    stats = {}
    assert polish.polish_direct_incumbent(problem, RouteEvaluator(problem), routes, statistics=stats) == routes
    assert stats["single_drone_skips"] == 1 and "home_calls" not in stats
