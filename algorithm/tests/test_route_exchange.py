"""Simultaneous suffix exchange preserves full fleet and delivery constraints."""

from random import Random

import pytest

import uav_dispatch.route_exchange as exchange
from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg, evaluate_solution
from uav_dispatch.search import RouteEvaluator


def _task(task_id, x, *, deadline=1000):
    return Task(task_id, Point(x, 0), Point(x, 0), deadline)


def _crossed_problem(*, deadline=1000):
    problem = Problem(
        (_task(1, 10), _task(2, -11, deadline=deadline),
         _task(3, -10), _task(4, 11, deadline=deadline)),
        drone_count=2, max_tasks_per_drone=2,
    )
    return problem, ((1, -1, 2, -2), (3, -3, 4, -4))


@pytest.mark.parametrize("deadline", [15, 1000])
def test_exchange_improves_full_fleet_without_a_spare_task_slot(deadline):
    problem, routes = _crossed_problem(deadline=deadline)
    statistics = {}
    changed = exchange.bounded_tail_exchange(
        problem, RouteEvaluator(problem), routes, statistics=statistics,
    )
    before, after = (evaluate_solution(problem, r, _force_full=True) for r in (routes, changed))
    assert changed == ((1, -1, 4, -4), (3, -3, 2, -2))
    assert after.valid and after.score < before.score
    assert [sum(v < 0 for v in r) for r in changed] == [2, 2]
    assert statistics["improvements"] == 1
    assert statistics["full_checks"] == 2
    if deadline == 15:
        assert before.score.late_count == 2
        assert after.score.late_count == 0


def test_unequal_route_lengths_match_suffix_count_not_prefix_count():
    problem = Problem((_task(1, 9), _task(2, 10), _task(3, -11),
                       _task(4, -10), _task(5, 11)),
                      drone_count=2, max_tasks_per_drone=3)
    routes = ((1, -1, 2, -2, 3, -3), (4, -4, 5, -5))
    changed = exchange.bounded_tail_exchange(problem, RouteEvaluator(problem), routes)
    assert changed == ((1, -1, 2, -2, 5, -5), (4, -4, 3, -3))
    assert [sum(v < 0 for v in r) for r in changed] == [3, 2]
    assert evaluate_solution(problem, changed, _force_full=True).valid


def test_different_homes_are_used_for_whole_route_swap():
    problem = Problem((_task(1, 10, deadline=3), _task(2, -10, deadline=3)),
                      drone_count=2, max_tasks_per_drone=1,
                      drone_homes=(Point(-10, 0), Point(10, 0)))
    routes = ((1, -1), (2, -2))
    changed = exchange.bounded_tail_exchange(problem, RouteEvaluator(problem), routes)
    assert changed == ((2, -2), (1, -1))
    checked = evaluate_solution(problem, changed, _force_full=True)
    assert checked.valid and checked.score.late_count == 0 and checked.score.distance_km == 0


def test_zero_load_cuts_do_not_split_interleaved_pairs(monkeypatch):
    problem = Problem(tuple(_task(i, 0) for i in range(1, 7)),
                      drone_count=2, max_tasks_per_drone=3)
    routes = ((1, 2, -1, -2, 3, -3), (4, -4, 5, 6, -5, -6))
    evaluator = RouteEvaluator(problem)
    original = evaluator.evaluate
    checked_routes = []
    def capture(route, **kwargs):
        checked_routes.append(route)
        return original(route, **kwargs)
    monkeypatch.setattr(evaluator, "evaluate", capture)
    assert exchange.bounded_tail_exchange(problem, evaluator, routes) is routes
    # Only whole-route cuts have equal nonempty suffix counts in this example.
    assert set(checked_routes) == set(routes)
    for route in checked_routes:
        assert all(route.index(v) < route.index(-v) for v in route if v > 0)


def test_round_robin_offset_bounded_pairs_and_empty_route():
    problem = Problem((_task(1, 10), _task(2, -11), _task(3, -10), _task(4, 11)),
                      drone_count=3, max_tasks_per_drone=2)
    routes = ((1, -1, 2, -2), (), (3, -3, 4, -4))
    stats = {}
    assert exchange.bounded_tail_exchange(problem, RouteEvaluator(problem), routes,
                                         pair_offset=0, max_pairs=1, statistics=stats) is routes
    assert stats["pair_checks"] == 1
    assert stats.get("probes", 0) == 0
    changed = exchange.bounded_tail_exchange(problem, RouteEvaluator(problem), routes,
                                             pair_offset=4, max_pairs=1)
    assert changed == ((1, -1, 4, -4), (), (3, -3, 2, -2))
    assert changed[1] is routes[1]


@pytest.mark.parametrize("seed", [0, 3, 11, 71])
def test_random_routes_preserve_capacity_precedence_counts_and_score(seed):
    rng = Random(seed)
    tasks = tuple(Task(i, Point(rng.uniform(-10, 10), rng.uniform(-10, 10)),
                       Point(rng.uniform(-10, 10), rng.uniform(-10, 10)), rng.uniform(10, 70))
                  for i in range(1, 13))
    problem = Problem(tasks, drone_count=3, max_tasks_per_drone=4,
                      drone_homes=(Point(-10, 1), Point(0, 0), Point(10, -1)))
    routes = []
    for start in (1, 5, 9):
        pending, onboard, route = list(range(start, start + 4)), [], []
        while pending or onboard:
            options = [(False, i) for i in onboard]
            if len(onboard) < 2:
                options += [(True, i) for i in pending]
            pickup, task_id = rng.choice(options)
            if pickup:
                pending.remove(task_id)
                onboard.append(task_id)
                route.append(task_id)
            else:
                onboard.remove(task_id)
                route.append(-task_id)
        routes.append(tuple(route))
    routes = tuple(routes)
    changed = exchange.bounded_tail_exchange(problem, RouteEvaluator(problem), routes, max_pairs=50)
    checked = evaluate_solution(problem, changed, _force_full=True)
    assert checked.valid
    assert checked.score <= evaluate_solution(problem, routes, _force_full=True).score
    assert [sum(v < 0 for v in r) for r in changed] == [4, 4, 4]


def test_expired_deadline_does_not_evaluate(monkeypatch):
    problem, routes = _crossed_problem()
    evaluator = RouteEvaluator(problem)
    monkeypatch.setattr(exchange, "perf_counter", lambda: 2.0)
    monkeypatch.setattr(exchange, "evaluate_solution", lambda *a, **k: pytest.fail("expired"))
    stats = {}
    assert exchange.bounded_tail_exchange(problem, evaluator, routes, deadline=1, statistics=stats) is routes
    assert stats == {"deadline_reached": 1}


def test_deadline_during_full_candidate_check_rejects_that_candidate(monkeypatch):
    problem, routes = _crossed_problem()
    now = [0.0]
    original = exchange.evaluate_solution
    def confirm(*args, **kwargs):
        checked = original(*args, **kwargs)
        if args[1] != routes:
            now[0] = 2.0
        return checked
    monkeypatch.setattr(exchange, "perf_counter", lambda: now[0])
    monkeypatch.setattr(exchange, "evaluate_solution", confirm)
    assert exchange.bounded_tail_exchange(problem, RouteEvaluator(problem), routes, deadline=1) is routes


def test_expiration_after_first_accepted_pair_keeps_its_checkpoint(monkeypatch):
    problem = Problem((_task(1, 10), _task(2, -11), _task(3, -10),
                       _task(4, 11), _task(5, 0)),
                      drone_count=3, max_tasks_per_drone=2)
    routes = ((1, -1, 2, -2), (3, -3, 4, -4), (5, -5))
    stats = {}
    # Expire only after the verified improvement has been recorded.
    monkeypatch.setattr(exchange, "perf_counter", lambda: 2.0 if stats.get("improvements") else 0.0)
    changed = exchange.bounded_tail_exchange(problem, RouteEvaluator(problem), routes,
                                             deadline=1, statistics=stats)
    assert changed == ((1, -1, 4, -4), (3, -3, 2, -2), (5, -5))
    assert stats["improvements"] == 1 and stats["deadline_reached"] == 1
    assert evaluate_solution(problem, changed, _force_full=True).valid


def test_full_canonical_check_can_veto_local_score_improvement(monkeypatch):
    problem, routes = _crossed_problem()
    baseline = evaluate_solution(problem, routes, _force_full=True)
    monkeypatch.setattr(exchange, "evaluate_solution", lambda *a, **k: baseline)
    stats = {}
    assert exchange.bounded_tail_exchange(problem, RouteEvaluator(problem), routes, statistics=stats) is routes
    assert stats["full_checks"] > 1 and stats.get("improvements", 0) == 0


def test_relay_events_are_rejected_but_unused_registry_is_allowed():
    problem = Problem((_task(1, 1), _task(2, -1)), drone_count=2,
                      relay_stations=(RelayStation(1, 0, 0),),
                      leg_registry={3: TransportLeg(3, 1, "RELAY_IN", 1),
                                    4: TransportLeg(4, 1, "RELAY_OUT", 1)},
                      task_relay_candidates={1: (1,)})
    evaluator = RouteEvaluator(problem)
    with pytest.raises(ValueError, match="DIRECT"):
        exchange.bounded_tail_exchange(problem, evaluator, ((3, -3), (4, -4, 2, -2)))
    direct = ((1, -1), (2, -2))
    assert exchange.bounded_tail_exchange(problem, evaluator, direct) is direct


def test_invalid_input_is_reported_and_disabled_pass_is_noop():
    problem, routes = _crossed_problem()
    evaluator = RouteEvaluator(problem)
    with pytest.raises(ValueError, match="输入解非法"):
        exchange.bounded_tail_exchange(problem, evaluator, ((1, -1), (3, -3, 4, -4)))
    with pytest.raises(ValueError, match="不能为负"):
        exchange.bounded_tail_exchange(problem, evaluator, routes, max_pairs=-1)
    assert exchange.bounded_tail_exchange(problem, evaluator, routes, max_pairs=0) is routes
