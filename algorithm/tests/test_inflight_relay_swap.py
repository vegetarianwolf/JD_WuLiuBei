"""Small physical in-flight relay fixtures; no official data or timed solver."""
from dataclasses import replace
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import inflight_relay_swap as inflight


WEIGHTS, SCALES = (.5, .25, .25), (4., 100., 100.)


def fixture(*, homes=None, first_deadline=100.):
    tasks = (Task(1, Point(-4., 0.), Point(4., 0.), first_deadline),
             Task(2, Point(4., 0.), Point(-4., 0.), 100.),
             Task(3, Point(-4., 0.), Point(-4., 0.), 100.),
             Task(4, Point(4., 0.), Point(4., 0.), 100.))
    registry = {}
    for task in tasks:
        inbound = 5 + 2 * (task.id - 1)
        registry[inbound] = TransportLeg(inbound, task.id, "RELAY_IN", 1)
        registry[inbound + 1] = TransportLeg(inbound + 1, task.id, "RELAY_OUT", 1)
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=2, capacity=2,
        speed_km_per_min=1., drone_homes=homes or (Point(-4., 0.), Point(4., 0.)),
        relay_stations=(RelayStation(1, 0., 0.),), leg_registry=registry,
        task_relay_candidates={t.id: (1,) for t in tasks})
    return problem, ((1, 3, -1, -3), (2, 4, -2, -4))


def run(problem, routes, **kwargs):
    return inflight.refine(problem, routes, deadline=perf_counter() + 1.,
                          weights=WEIGHTS, scales=SCALES, **kwargs)


def swap(problem, routes, first, second, cut_a, cut_b):
    index, legs = build_plan_index(problem, routes), inflight._leg_pairs(problem)
    a, b = index.delivery_route_by_task[first], index.delivery_route_by_task[second]
    in_i, out_i = legs[first][1]
    in_j, out_j = legs[second][1]
    candidate = list(routes)
    candidate[a] = inflight._changed_route(routes[a], first, in_i, out_j, cut_a)
    candidate[b] = inflight._changed_route(routes[b], second, in_j, out_i, cut_b)
    return tuple(candidate)


def test_cross_swap_reduces_distance_without_reassigning_or_reordering_other_jobs():
    problem, routes = fixture()
    initial = evaluate_solution(problem, routes, _force_full=True)
    result = run(problem, routes, max_pairs=1)
    assert initial.valid and initial.score.distance_km == 32.
    assert result["evaluation"].valid
    assert result["evaluation"].score.distance_km == 16.
    assert result["evaluation"].score.late_count == 0
    index = build_plan_index(problem, result["routes"])
    assert index.relay_task_ids == (1, 2)
    assert dict(index.delivery_route_by_task) == {1: 1, 2: 0, 3: 0, 4: 1}
    assert tuple(v for v in result["routes"][0] if abs(v) == 3) == (3, -3)
    assert tuple(v for v in result["routes"][1] if abs(v) == 4) == (4, -4)
    assert result["metadata"]["new_cross_uav_count"] == 2
    assert result["metadata"]["accepted_control_only_moves"] == 0
    assert all(m["preserved_all_other_job_routes_and_event_order"] for m in result["metadata"]["accepted_moves"])
    assert result["metadata"]["batches"] > 1


def test_carrying_two_per_uav_at_cut_preserves_load_two_and_saturated_k():
    problem, routes = fixture()
    candidate = swap(problem, routes, 1, 2, 2, 2)
    assert candidate == ((5, 3, -5, 8, -8, -3), (7, 4, -7, 6, -6, -4))
    checked = evaluate_solution(problem, candidate, _force_full=True)
    assert checked.valid and checked.score.distance_km == 16.
    index = build_plan_index(problem, candidate)
    assert [sum(owner == i for owner in index.delivery_route_by_task.values()) for i in range(2)] == [2, 2]
    for route in candidate:
        load = 0
        for position, event in enumerate(route):
            load += 1 if event > 0 else -1
            assert 0 <= load <= 2
            if position == 1:
                assert load == 2
            if position == 2:
                assert load == 1
            if position == 3:
                assert load == 2
        assert load == 0


def test_original_pickups_stay_physical_and_original_delivery_slots_change_coordinates():
    problem, routes = fixture()
    candidate = swap(problem, routes, 1, 2, 2, 2)
    assert problem._points[problem._node_for_visit(candidate[0][0])] == problem.task(1).pickup
    assert problem._points[problem._node_for_visit(candidate[1][0])] == problem.task(2).pickup
    assert problem._points[problem._node_for_visit(candidate[0][4])] == problem.task(2).delivery
    assert problem._points[problem._node_for_visit(candidate[1][4])] == problem.task(1).delivery
    # Unrelated positions retain their exact physical locations and order.
    assert candidate[0][1] == 3 and candidate[0][-1] == -3
    assert candidate[1][1] == 4 and candidate[1][-1] == -4


def test_actual_station_travel_and_handoff_wait_propagate_to_all_deliveries():
    problem, routes = fixture(homes=(Point(-8., 0.), Point(4., 0.)))
    candidate = swap(problem, routes, 1, 2, 2, 2)
    schedule = GlobalRelayEvaluator(problem).evaluate(candidate)
    checked = evaluate_solution(problem, candidate, _force_full=True)
    assert checked.valid and schedule.feasible
    assert schedule.event_times_min[(0, 2)] == 8.  # A travels home->pickup->station.
    assert schedule.event_times_min[(1, 2)] == 4.  # B's own drop is earlier.
    assert schedule.event_times_min[(1, 3)] == 8.  # It waits before taking A's package.
    assert schedule.total_waiting_min == 4.
    assert dict(schedule.delivery_times_min) == {1: 12., 2: 12., 3: 12., 4: 12.}
    assert checked.score == schedule.score and checked.score.distance_km == 20.


def test_another_old_relay_leg_can_remain_onboard_unchanged_at_exchange_cut():
    problem, _ = fixture()
    routes = ((1, 9, -1, -9, 10, -10), (2, 4, -2, -4))
    initial = evaluate_solution(problem, routes, _force_full=True)
    assert initial.valid
    candidate = swap(problem, routes, 1, 2, 2, 2)
    assert candidate[0][:4] == (5, 9, -5, 8)
    assert tuple(v for v in candidate[0] if abs(v) in (9, 10)) == (9, -9, 10, -10)
    checked = evaluate_solution(problem, candidate, _force_full=True)
    assert checked.valid and checked.score.distance_km < initial.score.distance_km
    plan = build_plan_index(problem, candidate).task_plans[3]
    assert plan.mode == "RELAY" and plan.leg_ids == (9, 10)


def test_new_reciprocal_dependency_cycle_is_rejected_even_with_existing_leg_onboard():
    tasks = tuple(Task(i, Point(0., 0.), Point(0., 0.), 100.) for i in range(1, 4))
    registry = {}
    for task in tasks:
        registry[2 * task.id + 2] = TransportLeg(2 * task.id + 2, task.id, "RELAY_IN", 1)
        registry[2 * task.id + 3] = TransportLeg(2 * task.id + 3, task.id, "RELAY_OUT", 1)
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=2, speed_km_per_min=1.,
        relay_stations=(RelayStation(1, 0., 0.),), leg_registry=registry,
        task_relay_candidates={t.id: (1,) for t in tasks})
    routes = ((9, -9, 1, -1), (2, 8, -8, -2))
    assert evaluate_solution(problem, routes, _force_full=True).valid
    cyclic = swap(problem, routes, 1, 2, 3, 1)
    assert not GlobalRelayEvaluator(problem).evaluate(cyclic).feasible
    result = run(problem, routes)
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["metadata"]["rejected_cycles"] > 0
    assert result["metadata"]["accepted_improvements"] == 0


def test_weighted_decision_can_trade_a_deadline_for_a_large_distance_gain():
    problem, routes = fixture(homes=(Point(-8., 0.), Point(40., 0.)), first_deadline=20.)
    initial = evaluate_solution(problem, routes, _force_full=True)
    ordinary = run(problem, routes)
    distance_priority = inflight.refine(problem, routes, deadline=perf_counter() + 1.,
        weights=(.001, .001, .998), scales=(1., 1., 1.))
    assert initial.score.late_count == ordinary["evaluation"].score.late_count == 0
    assert distance_priority["evaluation"].score.late_count == 1
    assert distance_priority["evaluation"].score.distance_km < initial.score.distance_km
    assert distance_priority["metadata"]["final_key"][0] < distance_priority["metadata"]["initial_key"][0]


def test_scalar_key_keeps_physical_ties_without_using_score_lexicographic_order():
    coeff = inflight._coefficients((.5, .25, .25), (1., 1., 1.))
    assert inflight.Score(1, 0., 0.) > inflight.Score(0, 0., 3.)
    assert inflight._key(inflight.Score(1, 0., 0.), coeff) < inflight._key(inflight.Score(0, 0., 3.), coeff)
    assert inflight._key(inflight.Score(0, 2., 0.), coeff) < inflight._key(inflight.Score(1, 0., 0.), coeff)


def test_expired_deadline_returns_only_checked_input():
    problem, routes = fixture()
    result = inflight.refine(problem, routes, deadline=perf_counter() - 1., weights=WEIGHTS, scales=SCALES)
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["iterations"] == 0 and result["metadata"]["deadline_reached"]


@pytest.mark.parametrize("expired_during", ["global", "confirmation"])
def test_candidate_completed_after_deadline_is_not_adopted(monkeypatch, expired_during):
    problem, routes = fixture()
    clock = [0.]
    monkeypatch.setattr(inflight, "perf_counter", lambda: clock[0])
    original_global, original_confirm = inflight.GlobalRelayEvaluator.evaluate, inflight.evaluate_solution
    def global_call(self, candidate):
        checked = original_global(self, candidate)
        if expired_during == "global" and tuple(tuple(r) for r in candidate) != routes:
            clock[0] = 2.
        return checked
    def confirmation(p, candidate, **kwargs):
        checked = original_confirm(p, candidate, **kwargs)
        if expired_during == "confirmation" and tuple(tuple(r) for r in candidate) != routes:
            clock[0] = 2.
        return checked
    monkeypatch.setattr(inflight.GlobalRelayEvaluator, "evaluate", global_call)
    monkeypatch.setattr(inflight, "evaluate_solution", confirmation)
    result = inflight.refine(problem, routes, deadline=1., weights=WEIGHTS, scales=SCALES)
    assert result["routes"] == routes and result["metadata"]["accepted_improvements"] == 0
    assert result["metadata"]["deadline_reached"]


def test_memo_best_updates_are_reproducible_and_only_cross_moves_are_accepted():
    problem, routes = fixture()
    first, second = run(problem, routes, seed=17), run(problem, routes, seed=17)
    assert first["routes"] == second["routes"]
    assert first["evaluation"].score == second["evaluation"].score
    assert first["metadata"]["global_evaluations"] == second["metadata"]["global_evaluations"]
    assert first["metadata"]["accepted_transfer_moves"] == first["metadata"]["accepted_improvements"]
    assert first["metadata"]["final_key"] <= first["metadata"]["initial_key"]
    assert first["metadata"]["neighborhood_exhausted"]


def test_invalid_incumbent_and_objective_inputs_are_rejected():
    problem, routes = fixture()
    with pytest.raises(ValueError, match="Invalid input"):
        run(problem, ((1, 3, 2, -1, -3, -2, 4, -4), ()))
    for changes in (dict(weights=(1., 0., 0.)), dict(scales=(1., -1., 1.)), dict(max_pairs=0)):
        args = dict(deadline=perf_counter() + 1., weights=WEIGHTS, scales=SCALES)
        args.update(changes)
        with pytest.raises(ValueError):
            inflight.refine(problem, routes, **args)


def test_no_registered_common_station_never_invents_relay_moves():
    problem, routes = fixture()
    disabled = replace(problem, leg_registry={}, task_relay_candidates={})
    result = run(disabled, routes)
    assert result["routes"] == routes and result["iterations"] == 0
    no_candidates = replace(problem, task_relay_candidates={t.id: () for t in problem.tasks})
    result = run(no_candidates, routes)
    assert result["routes"] == routes and result["metadata"]["pair_probes"] == 0
