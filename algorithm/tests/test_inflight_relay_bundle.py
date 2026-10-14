"""Tiny actual-frontier 2<->2 relay swaps, coverage and timing checks."""
from dataclasses import replace
from itertools import permutations, product
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import inflight_relay_bundle as bundle


WEIGHTS, SCALES = (.5, .25, .25), (6., 100., 100.)


def fixture(*, homes=None, first_deadline=100., four_stations=False):
    tasks = (Task(1, Point(-4., 0.), Point(4., 0.), first_deadline),
             Task(2, Point(4., 0.), Point(-4., 0.), 100.),
             Task(3, Point(-3., 0.), Point(3., 0.), 100.),
             Task(4, Point(3., 0.), Point(-3., 0.), 100.),
             Task(5, Point(-4., 0.), Point(-4., 0.), 100.),
             Task(6, Point(4., 0.), Point(4., 0.), 100.))
    stations = (RelayStation(1, 0., 0.),)
    if four_stations:
        stations += (RelayStation(2, 100., 100.), RelayStation(3, -100., 100.), RelayStation(4, 100., -100.))
    registry, counter = {}, 7
    for task in tasks:
        for station in stations:
            registry[counter] = TransportLeg(counter, task.id, "RELAY_IN", station.id)
            registry[counter + 1] = TransportLeg(counter + 1, task.id, "RELAY_OUT", station.id)
            counter += 2
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=3, capacity=2,
        speed_km_per_min=1., drone_homes=homes or (Point(-4., 0.), Point(4., 0.)),
        relay_stations=stations, leg_registry=registry,
        task_relay_candidates={t.id: tuple(s.id for s in stations) for t in tasks})
    return problem, ((1, 3, -1, -3, 5, -5), (2, 4, -2, -4, 6, -6))


def run(problem, routes, **kwargs):
    return bundle.refine(problem, routes, deadline=perf_counter() + 1., weights=WEIGHTS, scales=SCALES, **kwargs)


def groups_for(problem, routes):
    return bundle._groups(problem, routes, build_plan_index(problem, routes), bundle._leg_pairs(problem))


def candidate_for(problem, routes, received_a=(4, 2), received_b=(3, 1), station=1):
    groups, legs = groups_for(problem, routes), bundle._leg_pairs(problem)
    left = next(g for g in groups if g.owner == 0)
    right = next(g for g in groups if g.owner == 1)
    return (bundle._changed_route(problem, routes[0], left, received_a, left.cuts[0], station, legs),
            bundle._changed_route(problem, routes[1], right, received_b, right.cuts[0], station, legs))


def test_actual_cargo_groups_exclude_nonoverlapping_arbitrary_task_pairs():
    problem, routes = fixture()
    groups = groups_for(problem, routes)
    assert [(g.owner, g.tasks, g.cuts) for g in groups] == [(0, (1, 3), (2,)), (1, (2, 4), (2,))]
    adjacent = ((1, -1, 3, -3, 5, -5), (2, -2, 4, -4, 6, -6))
    assert not groups_for(problem, adjacent)
    result = run(problem, adjacent)
    assert result["routes"] == adjacent and result["metadata"]["pair_probes"] == 0


def test_four_real_cross_transfers_preserve_other_jobs_and_full_final_k():
    problem, routes = fixture()
    initial = evaluate_solution(problem, routes, _force_full=True)
    result = run(problem, routes)
    assert initial.valid and initial.score.distance_km == 32.
    assert result["evaluation"].valid and result["evaluation"].score.distance_km == 16.
    assert result["evaluation"].score.late_count == 0
    index = build_plan_index(problem, result["routes"])
    assert index.relay_task_ids == (1, 2, 3, 4)
    assert dict(index.delivery_route_by_task) == {1: 1, 2: 0, 3: 1, 4: 0, 5: 0, 6: 1}
    assert [sum(owner == i for owner in index.delivery_route_by_task.values()) for i in range(2)] == [3, 3]
    assert tuple(v for v in result["routes"][0] if abs(v) == 5) == (5, -5)
    assert tuple(v for v in result["routes"][1] if abs(v) == 6) == (6, -6)
    assert result["metadata"]["new_cross_uav_count"] == 4
    assert result["metadata"]["accepted_control_only_moves"] == 0


def test_unload_two_before_loading_two_keeps_original_pickup_points():
    problem, routes = fixture()
    candidate = candidate_for(problem, routes)
    for owner, route in enumerate(candidate):
        load = 0
        for position, visit in enumerate(route):
            load += 1 if visit > 0 else -1
            assert 0 <= load <= 2
            if position == 1:
                assert load == 2
            if position == 3:
                assert load == 0
            if position == 5:
                assert load == 2
        assert load == 0
        for position in (0, 1):
            assert problem._node_for_visit(route[position]) == problem._node_for_visit(routes[owner][position])
    assert evaluate_solution(problem, candidate, _force_full=True).valid


def test_both_permutations_per_uav_are_scored_and_best_delivery_mapping_is_retained():
    problem, routes = fixture()
    checked = {}
    for received_a, received_b in product(permutations((2, 4)), permutations((1, 3))):
        candidate = candidate_for(problem, routes, received_a, received_b)
        evaluation = evaluate_solution(problem, candidate, _force_full=True)
        assert evaluation.valid
        checked[(received_a, received_b)] = evaluation.score.distance_km
    assert len(checked) == 4 and len(set(checked.values())) > 1
    result = run(problem, routes)
    assert result["evaluation"].score.distance_km == min(checked.values())
    move = result["metadata"]["accepted_moves"][-1]
    assert move["received_a"] == [4, 2] and move["received_b"] == [3, 1]
    assert result["metadata"]["mappings_probed"] == 4


def test_all_four_registered_stations_are_considered_without_changing_homes():
    problem, routes = fixture(four_stations=True)
    result = run(problem, routes)
    assert result["evaluation"].valid and result["evaluation"].score.distance_km == 16.
    assert result["metadata"]["stations_probed"] == 16  # Four mappings x four stations.
    assert all(move["station"] == 1 for move in result["metadata"]["accepted_moves"])
    assert problem.drone_homes == (Point(-4., 0.), Point(4., 0.))
    assert result["metadata"]["lower_bound_pruned"] > 0


def test_real_prefix_travel_and_waiting_propagate_to_all_four_received_packages():
    problem, routes = fixture(homes=(Point(-8., 0.), Point(4., 0.)))
    candidate = candidate_for(problem, routes)
    schedule = GlobalRelayEvaluator(problem).evaluate(candidate)
    assert schedule.feasible
    assert schedule.event_times_min[(0, 2)] == 8.
    assert schedule.event_times_min[(1, 2)] == 4.
    assert schedule.event_times_min[(1, 4)] == 8.
    assert schedule.total_waiting_min == 4.
    assert dict(schedule.delivery_times_min) == {1: 12., 2: 12., 3: 11., 4: 11., 5: 12., 6: 12.}
    checked = evaluate_solution(problem, candidate, _force_full=True)
    assert checked.valid and checked.score == schedule.score and checked.score.distance_km == 20.


def test_complete_future_old_relay_events_keep_their_route_and_order():
    problem, routes = fixture()
    legs = bundle._leg_pairs(problem)
    in5, out5 = legs[5][1]
    routes = (routes[0][:-2] + (in5, -in5, out5, -out5), routes[1])
    assert evaluate_solution(problem, routes, _force_full=True).valid
    candidate = candidate_for(problem, routes)
    assert tuple(v for v in candidate[0] if abs(v) in (in5, out5)) == (in5, -in5, out5, -out5)
    assert evaluate_solution(problem, candidate, _force_full=True).valid
    assert build_plan_index(problem, candidate).task_plans[5].leg_ids == (in5, out5)


def test_existing_dependency_can_make_bundle_exchange_cycle_and_is_rejected():
    tasks = tuple(Task(i, Point(0., 0.), Point(0., 0.), 100.) for i in range(1, 6))
    registry, counter = {}, 6
    for task in tasks:
        registry[counter] = TransportLeg(counter, task.id, "RELAY_IN", 1)
        registry[counter + 1] = TransportLeg(counter + 1, task.id, "RELAY_OUT", 1)
        counter += 2
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=3, speed_km_per_min=1.,
        relay_stations=(RelayStation(1, 0., 0.),), leg_registry=registry,
        task_relay_candidates={t.id: (1,) for t in tasks})
    routes = ((15, -15, 1, 3, -1, -3), (2, 4, -2, 14, -14, -4))
    assert evaluate_solution(problem, routes, _force_full=True).valid
    result = run(problem, routes)
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["metadata"]["rejected_cycles"] == 4
    assert result["metadata"]["accepted_improvements"] == 0


def test_weighted_tradeoff_uses_explicit_j_instead_of_lexicographic_score():
    problem, routes = fixture(homes=(Point(-8., 0.), Point(40., 0.)), first_deadline=20.)
    initial = evaluate_solution(problem, routes, _force_full=True)
    ordinary = run(problem, routes)
    distance_priority = bundle.refine(problem, routes, deadline=perf_counter() + 1.,
        weights=(.001, .001, .998), scales=(1., 1., 1.))
    assert initial.score.late_count == ordinary["evaluation"].score.late_count == 0
    assert distance_priority["evaluation"].score.late_count == 1
    assert distance_priority["evaluation"].score.distance_km < initial.score.distance_km
    assert distance_priority["metadata"]["final_key"][0] < distance_priority["metadata"]["initial_key"][0]


def test_expired_deadline_retains_checked_input():
    problem, routes = fixture()
    result = bundle.refine(problem, routes, deadline=perf_counter() - 1., weights=WEIGHTS, scales=SCALES)
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["iterations"] == 0 and result["metadata"]["deadline_reached"]


@pytest.mark.parametrize("expired_during", ["global", "confirmation"])
def test_late_completed_candidate_is_not_adopted(monkeypatch, expired_during):
    problem, routes = fixture()
    clock = [0.]
    monkeypatch.setattr(bundle, "perf_counter", lambda: clock[0])
    original_global, original_confirm = bundle.GlobalRelayEvaluator.evaluate, bundle.evaluate_solution
    def global_call(self, candidate):
        result = original_global(self, candidate)
        if expired_during == "global" and tuple(tuple(r) for r in candidate) != routes:
            clock[0] = 2.
        return result
    def confirmation(p, candidate, **kwargs):
        result = original_confirm(p, candidate, **kwargs)
        if expired_during == "confirmation" and tuple(tuple(r) for r in candidate) != routes:
            clock[0] = 2.
        return result
    monkeypatch.setattr(bundle.GlobalRelayEvaluator, "evaluate", global_call)
    monkeypatch.setattr(bundle, "evaluate_solution", confirmation)
    result = bundle.refine(problem, routes, deadline=1., weights=WEIGHTS, scales=SCALES)
    assert result["routes"] == routes and result["metadata"]["accepted_improvements"] == 0
    assert result["metadata"]["deadline_reached"]


def test_initial_k_and_capacity_are_hard_constraints_and_bad_inputs_fail():
    problem, routes = fixture()
    with pytest.raises(ValueError):  # Problem rejects insufficient total K before search.
        run(replace(problem, max_tasks_per_drone=2), routes)
    with pytest.raises(ValueError, match="Invalid input"):
        run(replace(problem, capacity=1), routes)
    for changes in (dict(max_pairs=0), dict(weights=(1., 0., 0.)), dict(scales=(1., -1., 1.))):
        args = dict(deadline=perf_counter() + 1., weights=WEIGHTS, scales=SCALES)
        args.update(changes)
        with pytest.raises(ValueError):
            bundle.refine(problem, routes, **args)


def test_disabled_registry_never_creates_a_relay_and_checks_incumbent():
    problem, routes = fixture()
    disabled = replace(problem, leg_registry={}, task_relay_candidates={})
    result = run(disabled, routes)
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["iterations"] == 0


def test_best_guard_and_real_cross_only_statistics_are_reproducible():
    problem, routes = fixture()
    first, second = run(problem, routes, seed=11), run(problem, routes, seed=11)
    assert first["routes"] == second["routes"]
    assert first["evaluation"].score == second["evaluation"].score
    assert first["metadata"]["global_evaluations"] == second["metadata"]["global_evaluations"]
    assert first["metadata"]["accepted_transfer_moves"] == first["metadata"]["accepted_improvements"]
    assert first["metadata"]["final_key"] <= first["metadata"]["initial_key"]
    assert first["metadata"]["neighborhood_exhausted"]
