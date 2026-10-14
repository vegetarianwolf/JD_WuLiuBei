"""Tiny reciprocal-transfer semantics, objective and deadline checks."""
from dataclasses import replace
from time import perf_counter

import pytest

from algorithm.experiments import reciprocal_relay_refinement as reciprocal
from uav_dispatch import (
    GlobalRelayEvaluator, Point, Problem, RelayStation, Score, Task,
    TransportLeg, build_plan_index, evaluate_solution,
)

WEIGHTS, SCALES = (.5, .25, .25), (4., 100., 100.)


def fixture():
    problem = Problem((
        Task(1, Point(-10, 0), Point(-9, 0), 100),
        Task(2, Point(-9, 1), Point(10, 1), 100),
        Task(3, Point(10, 2), Point(9, 2), 100),
        Task(4, Point(9, 3), Point(-10, 3), 100),
    ), drone_count=2, max_tasks_per_drone=2, relay_stations=(RelayStation(1, 0, 1.5),),
        leg_registry={
            5: TransportLeg(5, 2, "RELAY_IN", 1),
            6: TransportLeg(6, 2, "RELAY_OUT", 1),
            7: TransportLeg(7, 4, "RELAY_IN", 1),
            8: TransportLeg(8, 4, "RELAY_OUT", 1),
        }, task_relay_candidates={2: (1,), 4: (1,)})
    return problem, ((1, -1, 2, -2), (3, -3, 4, -4))


def run(problem, routes, **kwargs):
    return reciprocal.refine(problem, routes, deadline=perf_counter() + 5,
                             weights=WEIGHTS, scales=SCALES, max_pairs=8, **kwargs)


def test_two_true_cross_transfers_exchange_full_k_slots_and_improve():
    problem, routes = fixture()
    before = evaluate_solution(problem, routes, _force_full=True)
    result = run(problem, routes)
    final = result["evaluation"]
    assert final.valid and final.score.distance_km < before.score.distance_km
    assert final.score.late_count == 0
    index = build_plan_index(problem, result["routes"])
    assert index.relay_task_ids == (2, 4)
    assert index.delivery_route_by_task[2] == 1
    assert index.delivery_route_by_task[4] == 0
    assert index.inbound_route_by_task[2] == 0
    assert index.inbound_route_by_task[4] == 1
    assert [sum(r == i for r in index.delivery_route_by_task.values()) for i in range(2)] == [2, 2]
    assert result["metadata"]["direct_moves"] == 0
    assert result["metadata"]["relay"]["cross_uav_handoff_count"] == 2
    assert result["metadata"]["reciprocal_search"]["accepted_improvements"] >= 1
    assert result["iterations"] <= 8


def test_macro_capacity_and_real_wait_propagation_include_other_tasks():
    problem, routes = fixture()
    problem = replace(problem, capacity=1)
    candidate = ((5, -5, 8, -8, 1, -1), (3, -3, 7, -7, 6, -6))
    schedule = GlobalRelayEvaluator(problem).evaluate(candidate)
    checked = evaluate_solution(problem, candidate, _force_full=True)
    assert checked.valid and schedule.feasible
    assert schedule.total_waiting_min > 0
    drop_i = schedule.event_times_min[(0, 1)]
    drop_j = schedule.event_times_min[(1, 3)]
    assert schedule.event_times_min[(0, 2)] == max(drop_i, drop_j)
    assert schedule.event_times_min[(1, 4)] == max(drop_i, drop_j)
    # Task 1 follows A's waiting handoff and must use the global clock.
    no_wait_end = checked.route_distances_km[0] / problem.speed_km_per_min
    assert checked.delivery_times_min[1] > no_wait_end
    result = run(problem, routes)
    assert result["evaluation"].valid
    for route in result["routes"]:
        load = 0
        for visit in route:
            load += 1 if visit > 0 else -1
            assert 0 <= load <= 1


def test_weighted_key_uses_frozen_weights_with_physical_tiebreak():
    fewer_late, shorter = Score(0, 0., 100.), Score(1, 1., 1.)
    assert reciprocal.weighted_key(fewer_late, (.98, .01, .01), SCALES) < reciprocal.weighted_key(shorter, (.98, .01, .01), SCALES)
    assert reciprocal.weighted_key(shorter, (.01, .01, .98), SCALES) < reciprocal.weighted_key(fewer_late, (.01, .01, .98), SCALES)
    # The scalar matches exactly, so the physical components decide.
    assert not (reciprocal.weighted_key(Score(0, 2., 0.), WEIGHTS, SCALES)
                < reciprocal.weighted_key(Score(0, 0., 2.), WEIGHTS, SCALES))
    assert reciprocal.weighted_key(Score(0, 0., 2.), WEIGHTS, SCALES) < reciprocal.weighted_key(Score(0, 2., 0.), WEIGHTS, SCALES)


def test_refinement_does_not_use_production_lexicographic_order(monkeypatch):
    problem, routes = fixture()
    def forbidden(*args):
        raise AssertionError("Score ordering must not select this weighted candidate")
    monkeypatch.setattr(Score, "__lt__", forbidden)
    result = run(problem, routes)
    assert result["metadata"]["reciprocal_search"]["accepted_improvements"]


def test_removal_deletes_complete_existing_plans_on_all_aircraft():
    problem, routes = fixture()
    result = run(problem, routes)
    assert reciprocal._remove(problem, result["routes"], (2, 4)) == ((1, -1), (3, -3))
    assert set(result["evaluation"].delivery_times_min) == {1, 2, 3, 4}


def test_injected_deadlock_cannot_be_adopted(monkeypatch):
    problem, routes = fixture()
    cycle = ((1, -1, 8, -8, 5, -5), (3, -3, 6, -6, 7, -7))
    monkeypatch.setattr(reciprocal, "_candidates", lambda *args: ((cycle, 1),))
    result = run(problem, routes)
    assert result["routes"] == routes
    assert result["metadata"]["reciprocal_search"]["rejected_infeasible"] == 1
    assert result["metadata"]["reciprocal_search"]["accepted_improvements"] == 0


def test_expired_deadline_preserves_checked_initial_without_probing(monkeypatch):
    problem, routes = fixture()
    monkeypatch.setattr(reciprocal, "perf_counter", lambda: 2.)
    monkeypatch.setattr(reciprocal, "_candidates", lambda *args: pytest.fail("late probe"))
    result = reciprocal.refine(problem, routes, deadline=1., weights=WEIGHTS, scales=SCALES)
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["iterations"] == 0
    assert result["metadata"]["reciprocal_search"]["deadline_reached"] is True


def test_full_validation_finishing_after_deadline_cannot_replace_incumbent(monkeypatch):
    problem, routes = fixture()
    candidate = run(problem, routes)["routes"]
    clock, calls = [0.], [0]
    original = reciprocal.evaluate_solution
    def delayed(*args, **kwargs):
        value = original(*args, **kwargs)
        calls[0] += 1
        if calls[0] > 1:
            clock[0] = 2.
        return value
    monkeypatch.setattr(reciprocal, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(reciprocal, "evaluate_solution", delayed)
    monkeypatch.setattr(reciprocal, "_candidates", lambda *args: ((candidate, 1),))
    result = reciprocal.refine(problem, routes, deadline=1., weights=WEIGHTS, scales=SCALES)
    assert calls[0] == 2
    assert result["routes"] == routes
    assert result["metadata"]["reciprocal_search"]["accepted_improvements"] == 0
    assert result["metadata"]["reciprocal_search"]["deadline_reached"] is True


def test_zero_pair_limit_and_no_shared_station_preserve_valid_incumbent():
    problem, routes = fixture()
    first = reciprocal.refine(problem, routes, deadline=perf_counter()+5,
                             weights=WEIGHTS, scales=SCALES, max_pairs=0)
    assert first["routes"] == routes
    problem = replace(problem, task_relay_candidates={2: (), 4: ()})
    result = run(problem, routes)
    assert result["routes"] == routes and result["iterations"] == 0


@pytest.mark.parametrize("changes", [
    {"weights": (.5, .25, float("nan"))}, {"scales": (4., 0., 100.)},
    {"weights": (.5, .5, .5)}, {"deadline": float("inf")},
    {"gap_limit": 0}, {"max_pairs": -1},
])
def test_invalid_parameters_rejected(changes):
    problem, routes = fixture()
    args = dict(deadline=perf_counter()+5, weights=WEIGHTS, scales=SCALES)
    args.update(changes)
    with pytest.raises(ValueError):
        reciprocal.refine(problem, routes, **args)


def test_invalid_initial_is_rejected_even_if_deadline_is_expired():
    problem, _ = fixture()
    with pytest.raises(ValueError, match="initial"):
        reciprocal.refine(problem, ((1, -1), (3, -3)), deadline=0., weights=WEIGHTS, scales=SCALES)
