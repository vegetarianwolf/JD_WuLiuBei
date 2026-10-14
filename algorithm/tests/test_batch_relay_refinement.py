"""Tiny exact fixtures for the experimental joint two-package relay block."""
from dataclasses import replace
from itertools import product
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import batch_relay_refinement as batch


def fixture(*, local_x=0., local_deadline=100., capacity=2):
    tasks = (Task(1, Point(0., 0.), Point(10., 0.), 100.),
             Task(2, Point(1., 0.), Point(11., 0.), 100.),
             Task(3, Point(local_x, 0.), Point(local_x, 0.), local_deadline),
             Task(4, Point(local_x, 0.), Point(local_x, 0.), local_deadline))
    registry, candidates = {}, {}
    for task in tasks:
        inbound = 5 + 2 * (task.id - 1)
        registry[inbound] = TransportLeg(inbound, task.id, "RELAY_IN", 1)
        registry[inbound + 1] = TransportLeg(inbound + 1, task.id, "RELAY_OUT", 1)
        candidates[task.id] = (1,)
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=2, capacity=capacity,
                      speed_km_per_min=1., drone_homes=(Point(0., 0.), Point(10., 0.)),
                      relay_stations=(RelayStation(1, 5., 0.),), leg_registry=registry,
                      task_relay_candidates=candidates)
    routes = ((3, -3, 4, -4), (1, 2, -1, -2))
    if capacity == 1:
        routes = ((3, -3, 4, -4), (1, -1, 2, -2))
    return problem, routes


def run(problem, routes, *, weights=(.5, .25, .25), scales=(4., 100., 100.), **kwargs):
    return batch.refine(problem, routes, deadline=perf_counter() + 1.,
                        weights=weights, scales=scales, **kwargs)


def _all_pair_insertions(route, leg):
    """Exhaustive tiny one-leg insertion, not an official optimizer run."""
    for pickup in range(len(route) + 1):
        with_pickup = route[:pickup] + (leg,) + route[pickup:]
        for delivery in range(pickup + 1, len(with_pickup) + 1):
            yield with_pickup[:delivery] + (-leg,) + with_pickup[delivery:]


def test_joint_two_task_move_improves_when_neither_single_relay_can():
    problem, routes = fixture()
    initial = evaluate_solution(problem, routes, _force_full=True)
    assert initial.valid and initial.score.distance_km == 21.
    # Each isolated transfer is scored over every tiny capacity-feasible
    # insertion, so this fixture exhibits an actual combined-move barrier.
    for task_id, inbound, outbound in ((1, 5, 6), (2, 7, 8)):
        reduced = batch._remove_tasks(problem, routes, (task_id,))
        feasible = []
        for a, b in product(_all_pair_insertions(reduced[0], inbound),
                            _all_pair_insertions(reduced[1], outbound)):
            checked = evaluate_solution(problem, (a, b), _force_full=True)
            if checked.valid:
                feasible.append(checked.score.distance_km)
        assert feasible and min(feasible) >= initial.score.distance_km
    result = run(problem, routes)
    assert result["evaluation"].valid
    assert result["evaluation"].score.distance_km == 16.
    assert result["evaluation"].score.late_count == 0
    assert result["metadata"]["new_cross_uav_count"] == 2
    assert result["metadata"]["accepted_improvements"] >= 1


def test_capacity_and_final_k_are_preserved_on_saturated_aircraft():
    problem, routes = fixture()
    original = build_plan_index(problem, routes)
    result = run(problem, routes)
    final = build_plan_index(problem, result["routes"])
    assert dict(final.delivery_route_by_task) == dict(original.delivery_route_by_task)
    assert set(final.relay_task_ids) == {1, 2}
    assert final.inbound_route_by_task[1] == final.inbound_route_by_task[2] == 0
    assert final.outbound_route_by_task[1] == final.outbound_route_by_task[2] == 1
    for route in result["routes"]:
        load, peak = 0, 0
        for visit in route:
            load += 1 if visit > 0 else -1
            peak = max(peak, load)
            assert 0 <= load <= problem.capacity
        assert load == 0 and peak <= 2
    assert [sum(r == i for r in final.delivery_route_by_task.values()) for i in range(2)] == [2, 2]
    assert result["metadata"]["final_responsibility_preserved"]


def test_whole_old_relay_plan_is_removed_before_replacement():
    problem, _ = fixture()
    # Old same-UAV cache uses all four transport events per task on UAV B.
    routes = ((3, -3, 4, -4), (5, 7, -5, -7, 6, 8, -6, -8))
    assert evaluate_solution(problem, routes, _force_full=True).valid
    assert batch._remove_tasks(problem, routes, (1, 2)) == ((3, -3, 4, -4), ())
    result = run(problem, routes)
    assert result["evaluation"].valid and result["evaluation"].score.distance_km == 16.
    assert result["metadata"]["initial_cross_uav_count"] == 0
    assert result["metadata"]["final_cross_uav_count"] == 2
    for leg_id in (5, 6, 7, 8):
        visits = [v for r in result["routes"] for v in r]
        assert visits.count(leg_id) == visits.count(-leg_id) == 1


def test_global_waiting_and_other_aircraft_deadlines_are_scored():
    problem, routes = fixture(local_x=2., local_deadline=2.)
    result = run(problem, routes)
    schedule = GlobalRelayEvaluator(problem).evaluate(result["routes"])
    assert result["evaluation"].valid and result["evaluation"].score.late_count == 0
    # Local outbound clocks alone would be 10/11. Actual inbound arrival is
    # 9, so the receiving aircraft waits from 5 to 9 before completing 14/15.
    assert dict(schedule.delivery_times_min) == {1: 14., 2: 15., 3: 2., 4: 2.}
    assert schedule.event_times_min[(1, 0)] == 9.
    assert schedule.total_waiting_min == 4.
    assert result["evaluation"].score == schedule.score
    assert result["metadata"]["rejected_cycles"] > 0


def test_weighted_acceptance_can_choose_more_late_tasks_for_lower_j():
    problem, routes = fixture(local_x=2., local_deadline=2.)
    priority_on_time = run(problem, routes)
    priority_distance = run(problem, routes, weights=(.001, .001, .998), scales=(1., 1., 1.))
    assert priority_on_time["evaluation"].score.late_count == 0
    assert priority_on_time["evaluation"].score.distance_km == 20.
    assert priority_distance["evaluation"].score.late_count == 2
    assert priority_distance["evaluation"].score.distance_km == 19.
    assert priority_distance["metadata"]["final_key"][0] < priority_distance["metadata"]["initial_key"][0]


def test_physical_tie_keys_follow_scalar_score_without_score_ordering():
    coefficients = batch._coefficients((.5, .25, .25), (1., 1., 1.))
    worse_lex = batch.Score(1, 0., 0.)
    better_lex = batch.Score(0, 0., 3.)
    assert worse_lex > better_lex
    assert batch._key(worse_lex, coefficients) < batch._key(better_lex, coefficients)
    assert batch._key(batch.Score(0, 2., 0.), coefficients) < batch._key(worse_lex, coefficients)


def test_capacity_one_never_attempts_two_package_block():
    problem, routes = fixture(capacity=1)
    result = run(problem, routes)
    assert result["routes"] == routes
    assert result["evaluation"].valid
    assert result["iterations"] == result["metadata"]["global_evaluations"] == 0


def test_expired_deadline_returns_only_checked_input():
    problem, routes = fixture()
    result = batch.refine(problem, routes, deadline=perf_counter() - 1.,
                          weights=(.5, .25, .25), scales=(4., 100., 100.))
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["metadata"]["deadline_reached"]
    assert result["metadata"]["accepted_improvements"] == 0


@pytest.mark.parametrize("expired_during", ["global", "confirmation"])
def test_completed_candidate_after_deadline_is_not_adopted(monkeypatch, expired_during):
    problem, routes = fixture()
    clock = [0.]
    monkeypatch.setattr(batch, "perf_counter", lambda: clock[0])
    original_global = batch.GlobalRelayEvaluator.evaluate
    original_confirmation = batch.evaluate_solution
    def global_call(self, candidate):
        result = original_global(self, candidate)
        # Only complete replacement plans contain the two outbound legs.
        if expired_during == "global" and any(6 in r and 8 in r for r in candidate):
            clock[0] = 2.
        return result
    def confirmation(p, candidate, **kwargs):
        result = original_confirmation(p, candidate, **kwargs)
        if expired_during == "confirmation" and any(6 in r and 8 in r for r in candidate):
            clock[0] = 2.
        return result
    monkeypatch.setattr(batch.GlobalRelayEvaluator, "evaluate", global_call)
    monkeypatch.setattr(batch, "evaluate_solution", confirmation)
    result = batch.refine(problem, routes, deadline=1., weights=(.5, .25, .25), scales=(4., 100., 100.))
    assert result["routes"] == routes
    assert result["metadata"]["accepted_improvements"] == 0
    assert result["metadata"]["deadline_reached"]


def test_invalid_input_and_configuration_are_rejected():
    problem, routes = fixture()
    with pytest.raises(ValueError, match="Invalid input"):
        run(problem, ((1, 2, 3, -1, -2, -3, 4, -4), ()))
    for changes in (dict(weights=(1., 0., 0.)), dict(scales=(1., -1., 1.)), dict(max_pairs=0)):
        with pytest.raises(ValueError):
            run(problem, routes, **changes)


def test_no_common_station_or_no_relay_preserves_the_incumbent():
    problem, routes = fixture()
    disabled = replace(problem, leg_registry={}, task_relay_candidates={})
    result = run(disabled, routes)
    assert result["routes"] == routes and result["iterations"] == 0
    no_candidates = replace(problem, task_relay_candidates={t.id: () for t in problem.tasks})
    result = run(no_candidates, routes)
    assert result["routes"] == routes and result["metadata"]["pair_probes"] == 0


def test_seed_and_no_repeat_candidate_statistics_are_reproducible():
    problem, routes = fixture()
    first, second = run(problem, routes, seed=27), run(problem, routes, seed=27)
    assert first["routes"] == second["routes"]
    assert first["evaluation"].score == second["evaluation"].score
    assert first["metadata"]["global_evaluations"] == second["metadata"]["global_evaluations"]
    assert first["metadata"]["duplicate_candidates_skipped"] > 0
    assert first["iterations"] == first["metadata"]["global_evaluations"]
