"""Tiny physical tests for whole-plan relay-first construction, no official run."""
from dataclasses import replace
import json
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import relay_first_constructor as construction


def fixture(*, capacity=2):
    tasks = (Task(1, Point(0., 0.), Point(10., 0.), 100.),
             Task(2, Point(1., 0.), Point(11., 0.), 100.),
             Task(3, Point(0., 0.), Point(0., 0.), 100.),
             Task(4, Point(0., 0.), Point(0., 0.), 100.))
    registry = {}
    for task in tasks:
        inbound = 5 + 2 * (task.id - 1)
        registry[inbound] = TransportLeg(inbound, task.id, "RELAY_IN", 1)
        registry[inbound + 1] = TransportLeg(inbound + 1, task.id, "RELAY_OUT", 1)
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=2, capacity=capacity,
                      speed_km_per_min=1., drone_homes=(Point(0., 0.), Point(10., 0.)),
                      relay_stations=(RelayStation(1, 5., 0.),), leg_registry=registry,
                      task_relay_candidates={task.id: (1,) for task in tasks})
    routes = ((3, -3, 4, -4), (1, 2, -1, -2))
    if capacity == 1:
        routes = ((3, -3, 4, -4), (1, -1, 2, -2))
    return problem, routes


def run(problem, routes, **kwargs):
    return construction.refine(problem, routes, deadline=perf_counter() + 2.,
        weights=(.5, .25, .25), scales=(4., 100., 100.), **kwargs)


def test_dynamic_aircraft_can_do_local_deliveries_before_true_cross_batch():
    problem, routes = fixture()
    initial = evaluate_solution(problem, routes, _force_full=True)
    result = run(problem, routes)
    assert initial.score.distance_km == 21.
    assert result["evaluation"].valid and result["evaluation"].score.distance_km == 16.
    assert result["metadata"]["final_cross_uav_count"] == 2
    index = build_plan_index(problem, result["routes"])
    assert index.inbound_route_by_task[1] == index.inbound_route_by_task[2] == 0
    assert index.outbound_route_by_task[1] == index.outbound_route_by_task[2] == 1
    # The collector also completes its own final-delivery quota, and the two
    # local jobs occur before it flies empty to collect the remote deliveries.
    assert index.delivery_route_by_task[3] == index.delivery_route_by_task[4] == 0
    assert result["routes"][0].index(-4) < result["routes"][0].index(5)


def test_full_coverage_capacity_k_and_drop_before_pickup_are_physical():
    problem, routes = fixture()
    result = run(problem, routes)
    index = build_plan_index(problem, result["routes"])
    schedule = GlobalRelayEvaluator(problem).evaluate(result["routes"])
    assert schedule.feasible and schedule.score == result["evaluation"].score
    assert set(schedule.delivery_times_min) == {1, 2, 3, 4}
    assert [sum(owner == i for owner in index.delivery_route_by_task.values()) for i in range(2)] == [2, 2]
    for route in result["routes"]:
        load = 0
        for visit in route:
            load += 1 if visit > 0 else -1
            assert 0 <= load <= 2
        assert load == 0
    for task_id in index.relay_task_ids:
        inbound, outbound = index.task_plans[task_id].leg_ids
        donor, receiver = index.inbound_route_by_task[task_id], index.outbound_route_by_task[task_id]
        drop = schedule.event_times_min[(donor, result["routes"][donor].index(-inbound))]
        pickup = schedule.event_times_min[(receiver, result["routes"][receiver].index(outbound))]
        assert drop <= pickup


def test_entire_construction_can_be_worse_without_overwriting_incumbent():
    problem, routes = fixture()
    initial = evaluate_solution(problem, routes, _force_full=True)
    result = run(problem, routes, max_proposals=1, relay_credits=(.5,))
    snapshot = result["metadata"]["first_complete_candidate"]
    assert snapshot is not None and snapshot["valid"] and snapshot["complete"]
    assert snapshot["cross_uav_count"] == 4
    assert snapshot["key"][0] > result["metadata"]["initial_key"][0]
    assert result["routes"] == routes and result["evaluation"].score == initial.score
    assert result["metadata"]["accepted_improvements"] == 0
    assert evaluate_solution(problem, snapshot["routes"], _force_full=True).valid
    json.dumps(result["metadata"], allow_nan=False)


def test_expired_deadline_returns_only_checked_input():
    problem, routes = fixture()
    result = construction.refine(problem, routes, deadline=perf_counter() - 1.,
        weights=(.5, .25, .25), scales=(4., 100., 100.))
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["iterations"] == 0 and result["metadata"]["deadline_reached"]
    assert result["metadata"]["first_complete_candidate"] is None


@pytest.mark.parametrize("expiration", ["construction", "confirmation"])
def test_over_budget_complete_candidate_is_not_adopted(monkeypatch, expiration):
    problem, routes = fixture()
    clock = [0.]
    monkeypatch.setattr(construction, "perf_counter", lambda: clock[0])
    original_construct, original_check = construction._construct, construction.evaluate_solution
    calls = [0]
    def construct(*args, **kwargs):
        result = original_construct(*args, **kwargs)
        if expiration == "construction":
            clock[0] = 2.
        return result
    def check(*args, **kwargs):
        result = original_check(*args, **kwargs)
        calls[0] += 1
        if expiration == "confirmation" and calls[0] > 1:
            clock[0] = 2.
        return result
    monkeypatch.setattr(construction, "_construct", construct)
    monkeypatch.setattr(construction, "evaluate_solution", check)
    result = construction.refine(problem, routes, deadline=1.,
        weights=(.5, .25, .25), scales=(4., 100., 100.))
    assert result["routes"] == routes and result["metadata"]["accepted_improvements"] == 0
    assert result["metadata"]["first_complete_candidate"] is None
    assert result["metadata"]["deadline_reached"]


def test_capacity_one_constructs_only_single_package_jobs():
    problem, routes = fixture(capacity=1)
    result = run(problem, routes, max_proposals=1)
    snapshot = result["metadata"]["first_complete_candidate"]
    assert result["evaluation"].valid
    assert snapshot is not None and all(len(job["tasks"]) == 1 for job in snapshot["jobs"])
    assert evaluate_solution(problem, snapshot["routes"], _force_full=True).valid


def test_no_relay_registry_preserves_input_without_an_alternate_solver():
    problem, routes = fixture()
    plain = replace(problem, leg_registry={}, task_relay_candidates={})
    result = run(plain, routes)
    assert result["routes"] == routes and result["iterations"] == 0
    assert result["metadata"]["block_evaluations"] == 0


def test_direct_control_has_same_whole_plan_construction_without_relay_credit():
    problem, routes = fixture()
    result = run(problem, routes, relay_enabled=False, max_proposals=2, relay_credits=(.5,))
    assert result["metadata"]["complete_proposals"] == 2
    assert result["metadata"]["relay_enabled"] is False
    assert all(row["cross_uav_count"] == row["relay_task_count"] == 0
               for row in result["metadata"]["proposal_metrics"])
    assert result["metadata"]["proposal_metrics"][1]["configuration"]["ownership"] == "balanced_destination_clusters"
    assert result["evaluation"].valid
    assert not build_plan_index(problem, result["routes"]).relay_task_ids


def test_geometric_production_shortlist_can_expand_only_to_registered_legs():
    problem, routes = fixture()
    expanded = replace(problem, task_relay_candidates={task.id: () for task in problem.tasks})
    result = run(expanded, routes)
    assert result["evaluation"].valid and result["metadata"]["final_cross_uav_count"] == 2
    assert result["metadata"]["candidate_stations"] == "all_existing_registered_stations"
    assert problem.leg_registry == expanded.leg_registry


def test_destination_balancing_preserves_each_original_final_k_count():
    problem, routes = fixture()
    result = run(problem, routes, max_proposals=2)
    assert result["metadata"]["complete_proposals"] == 2
    assert result["metadata"]["proposal_metrics"][1]["configuration"]["ownership"] == "balanced_destination_clusters"
    snapshot = result["metadata"]["best_constructed_candidate"]
    index = build_plan_index(problem, snapshot["routes"])
    assert [sum(owner == i for owner in index.delivery_route_by_task.values()) for i in range(2)] == [2, 2]


def test_clocks_include_handoff_wait_empty_travel_and_downstream_delivery():
    problem, routes = fixture()
    slow_donor = replace(problem, drone_homes=(Point(-10., 0.), Point(10., 0.)))
    result = run(slow_donor, routes, max_proposals=1, relay_credits=(.5,))
    snapshot = result["metadata"]["first_complete_candidate"]
    schedule = GlobalRelayEvaluator(slow_donor).evaluate(snapshot["routes"])
    assert schedule.feasible and schedule.total_waiting_min > 0.
    checked = evaluate_solution(slow_donor, snapshot["routes"], _force_full=True)
    assert checked.score.distance_km == snapshot["score"]["distance_km"]
    assert checked.score.distance_km > 10.
    for job in snapshot["jobs"]:
        assert max(checked.delivery_times_min[t] for t in job["tasks"]) == pytest.approx(job["final_completion_min"])


def test_snapshot_and_seed_are_reproducible_and_plain_json():
    problem, routes = fixture()
    first, second = run(problem, routes, seed=71), run(problem, routes, seed=71)
    assert first["routes"] == second["routes"]
    assert first["metadata"]["proposal_metrics"] == second["metadata"]["proposal_metrics"]
    assert first["metadata"]["first_complete_candidate"] == second["metadata"]["first_complete_candidate"]
    json.dumps(first["metadata"], allow_nan=False)


def test_invalid_initial_or_configuration_is_rejected():
    problem, routes = fixture()
    with pytest.raises(ValueError, match="invalid initial"):
        run(problem, ((1, -1), ()))
    for options in (dict(max_proposals=0), dict(partner_limit=0), dict(anchor_limit=True),
                    dict(relay_credits=(-.1,)), dict(relay_credits=())):
        with pytest.raises(ValueError):
            run(problem, routes, **options)
