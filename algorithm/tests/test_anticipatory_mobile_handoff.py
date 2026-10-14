"""Tiny physical handoff checks; never invokes an official optimizer."""
from dataclasses import replace
import json
from time import perf_counter
from types import SimpleNamespace

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
from uav_dispatch.cli import result_payload

from algorithm.experiments import anticipatory_mobile_handoff as handoff
from algorithm.experiments.audit_anticipatory_mobile import audit_anticipatory_payload


WEIGHTS, SCALES = (.5, .25, .25), (4., 100., 100.)


def fixture(*, receiver_home=30., long_courier=False, registry=False, speed=1.):
    tasks = (Task(1, Point(0., 0.), Point(30., 0.), 100.),
             Task(2, Point(20., 0.), Point(31., 0.), 100.),
             Task(3, Point(0., 0.), Point(100. if long_courier else 10., 0.), 100.),
             Task(4, Point(100. if long_courier else 10., 0.), Point(20., 0.), 200.))
    stations = (RelayStation(1, -100., 100.), RelayStation(4, 100., -100.))
    legs = {}
    if registry:
        for task in tasks:
            for station in stations:
                i = 20 + len(legs)
                legs[i] = TransportLeg(i, task.id, "RELAY_IN", station.id)
                legs[i + 1] = TransportLeg(i + 1, task.id, "RELAY_OUT", station.id)
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=2, capacity=2,
        speed_km_per_min=speed, drone_homes=(Point(0., 0.), Point(receiver_home, 0.)),
        relay_stations=stations, leg_registry=legs,
        task_relay_candidates={t.id: (1, 4) for t in tasks} if registry else {})
    return problem, ((3, -3, 4, -4), (1, 2, -1, -2))


def run(problem, routes, **kwargs):
    args = dict(deadline=perf_counter() + 2., weights=WEIGHTS, scales=SCALES)
    args.update(kwargs)
    return handoff.refine(problem, routes, **args)


def fixed_candidate(problem, routes, point=Point(20., 0.)):
    group = handoff._Group(1, (1, 2), (2,), ())
    legs, by_task, station = handoff._new_legs(problem, group.tasks)
    receiving = handoff._receiver_options(problem, routes, group, by_task, legs,
        WEIGHTS, SCALES, 100, perf_counter() + 2., {"capacity_pruned": 0,
        "receiver_nonempty_cuts_pruned": 0, "receiver_options_generated": 0})
    receiver = next(o for o in receiving if o.first_position == 0)
    sending = handoff._sender_options(problem, routes, 0, group, by_task, legs,
        WEIGHTS, SCALES, 100, perf_counter() + 2., {"capacity_pruned": 0, "sender_options_generated": 0})
    sender = next(o for o in sending if (o.first_position, o.handoff_position, o.pickup_order) == (1, 4, (1, 2)))
    descriptor = handoff._descriptor(group, 0, sender, receiver, point, station)
    candidate = (sender.route, receiver.route)
    changed_problem = handoff.mobile._problem(problem, legs, descriptor)
    return changed_problem, candidate, descriptor, legs, sender, receiver


def test_two_dispersed_pickups_with_anticipatory_empty_receiver_save_distance():
    problem, routes = fixture()
    assert evaluate_solution(problem, routes, _force_full=True).score.distance_km == 81.
    result = run(problem, routes)
    assert result["evaluation"].valid and result["evaluation"].score.distance_km == 31.
    assert result["evaluation"].score.late_count == 0
    assert result["metadata"]["new_cross_uav_count"] == 2
    meeting = result["metadata"]["mobile_meetings"][0]
    assert meeting["own_task_groups"] == [[1, 2], []]
    assert meeting["meeting_type"] == meeting["type"] == "receiver_arrives_first"
    assert meeting["arrival_times_min"] == [30., 0.]
    assert meeting["synchronization_min"] == 30.
    assert meeting["aircraft_waiting_min"] == [0., 30.]
    assert meeting["receiver_waiting_load"] == 0
    assert {e["route_index"] for e in meeting["drop_events"]} == {0}
    assert {e["route_index"] for e in meeting["pickup_events"]} == {1}
    assert {e["physical_time_min"] for e in meeting["drop_events"] + meeting["pickup_events"]} == {30.}


def test_sender_first_new_pickup_shares_load_one_and_full_k_is_preserved():
    problem, routes = fixture()
    changed, candidate, _, _, _, _ = fixed_candidate(problem, routes)
    assert candidate[0] == (3, 5, -3, 4, -4, 7, -5, -7)
    assert handoff._loads(candidate[0])[1:4] == (1, 2, 1)
    assert max(handoff._loads(candidate[0])) == 2
    assert evaluate_solution(changed, candidate, _force_full=True).valid
    before, after = build_plan_index(problem, routes), build_plan_index(changed, candidate)
    assert dict(before.delivery_route_by_task) == dict(after.delivery_route_by_task)
    assert [sum(owner == i for owner in after.delivery_route_by_task.values()) for i in (0, 1)] == [2, 2]
    assert tuple(v for v in candidate[0] if abs(v) in (3, 4)) == routes[0]
    for task in (1, 2):
        out = next(l.id for l in changed.leg_registry.values() if l.task_id == task and l.kind == "RELAY_OUT")
        assert changed.point_for_visit(-out) == problem.task(task).delivery


def test_early_sender_cache_drop_is_explicitly_rejected_despite_global_feasibility():
    problem, routes = fixture(receiver_home=50.)
    changed, candidate, descriptor, _, _, _ = fixed_candidate(problem, routes)
    schedule = GlobalRelayEvaluator(changed).evaluate(candidate)
    assert schedule.feasible and evaluate_solution(changed, candidate, _force_full=True).valid
    assert schedule.event_times_min[(0, 6)] == 20.
    assert schedule.event_times_min[(1, 0)] == 30.
    with pytest.raises(ValueError, match="receiver arrives after sender"):
        handoff._physical_meetings(changed, candidate, descriptor, schedule)
    valid_alternative = run(problem, routes)
    for meeting in valid_alternative["metadata"]["mobile_meetings"]:
        assert meeting["arrival_times_min"][1] <= meeting["arrival_times_min"][0]
        assert meeting["aircraft_waiting_min"][0] == 0.


def test_all_flights_receiver_wait_and_speed_match_full_global_score():
    problem, routes = fixture(speed=2.)
    changed, candidate, descriptor, legs, sender, receiver = fixed_candidate(problem, routes)
    schedule = GlobalRelayEvaluator(changed).evaluate(candidate)
    meetings = handoff._physical_meetings(changed, candidate, descriptor, schedule)
    assert meetings[0]["arrival_times_min"] == [10., 5.]
    assert schedule.total_waiting_min == 5.
    manual = (handoff._route_score(problem, candidate[0], 0, legs, Point(20., 0.))
              + handoff._route_score(problem, candidate[1], 1, legs, Point(20., 0.), 10.))
    assert manual == schedule.score == evaluate_solution(changed, candidate, _force_full=True).score
    assert schedule.delivery_times_min[1] == 15. and schedule.delivery_times_min[2] == 15.5
    assert sender.outgoing is None  # Open route: there is no invented home-return edge.


def test_single_package_supported_with_empty_receiver_and_original_sender_cargo():
    tasks = (Task(1, Point(0., 0.), Point(30., 0.), 100.),
             Task(2, Point(0., 0.), Point(10., 0.), 100.))
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=1, capacity=2,
        speed_km_per_min=1., drone_homes=(Point(0., 0.), Point(30., 0.)))
    routes = ((2, -2), (1, -1))
    result = run(problem, routes, max_group_size=1)
    assert result["evaluation"].valid
    assert result["evaluation"].score.distance_km < 40.
    assert result["metadata"]["new_cross_uav_count"] == 1
    meeting = result["metadata"]["mobile_meetings"][0]
    assert meeting["group_size"] == 1 and meeting["receiver_waiting_load"] == 0


def test_capacity_two_sender_frontiers_and_empty_receiver_cuts_are_enforced():
    assert (1, 4) not in tuple(handoff._sender_positions((3, 4, -3, -4), 2))
    assert (2, 4) not in tuple(handoff._sender_positions((3, 4, -3, -4), 1))
    problem, routes = fixture()
    original = (1, 3, -3, 2, -1, -2)
    owner_problem = replace(problem, max_tasks_per_drone=3)
    group = handoff._Group(1, (1, 2), (4,), ())
    legs, by_task, _ = handoff._new_legs(problem, group.tasks)
    stats = {"capacity_pruned": 0, "receiver_nonempty_cuts_pruned": 0, "receiver_options_generated": 0}
    options = handoff._receiver_options(owner_problem, ((), original), group, by_task, legs,
        WEIGHTS, SCALES, 100, perf_counter() + 2., stats)
    assert options and stats["receiver_nonempty_cuts_pruned"] == 1
    assert all(handoff._loads(o.route)[o.meeting_position] == 0 for o in options)


def test_only_adopted_new_resources_added_and_all_old_topology_is_preserved():
    problem, routes = fixture(registry=True)
    original_registry, old_stations = dict(problem.leg_registry), tuple(problem.relay_stations)
    result = run(problem, routes)
    changed = result["problem"]
    assert len(changed.relay_stations) == len(old_stations) + 1
    assert changed.relay_stations[:-1] == old_stations
    assert dict(problem.leg_registry) == original_registry
    assert all(changed.leg_registry[i] == leg for i, leg in original_registry.items())
    assert len(changed.leg_registry) == len(original_registry) + 4
    new_station = result["metadata"]["mobile_meetings"][0]["station_id"]
    assert new_station == 5
    assert changed.home_points() == problem.home_points()
    assert changed.capacity == problem.capacity and changed.max_tasks_per_drone == problem.max_tasks_per_drone
    for task in (1, 2):
        assert changed.task_relay_candidates[task] == (1, 4, 5)
    for task in (3, 4):
        assert changed.task_relay_candidates[task] == problem.task_relay_candidates[task]
    json.dumps(result["metadata"], allow_nan=False)
    snapshot = result["metadata"]["accepted_moves"][0]["full_snapshot"]
    assert snapshot["topology"] == handoff.mobile.topology(changed)
    assert snapshot["routes"] == [list(r) for r in result["routes"]]


def test_manual_coordinate_screen_matches_real_global_at_several_meeting_points():
    problem, routes = fixture()
    for point in (Point(15., 0.), Point(20., 0.), Point(25., 0.), Point(20., 3.), Point(30., 2.)):
        changed, candidate, descriptor, legs, sender, receiver = fixed_candidate(problem, routes, point)
        aa = sender.prefix_time + sender.incoming.distance_to(point)
        ab = receiver.prefix_time + receiver.incoming.distance_to(point)
        if ab > aa:
            continue
        manual = (handoff._route_score(problem, candidate[0], 0, legs, point)
                  + handoff._route_score(problem, candidate[1], 1, legs, point, aa))
        schedule = GlobalRelayEvaluator(changed).evaluate(candidate)
        assert handoff._physical_meetings(changed, candidate, descriptor, schedule)
        full = evaluate_solution(changed, candidate, _force_full=True)
        assert full.valid and manual.late_count == full.score.late_count
        assert manual.total_lateness_min == pytest.approx(full.score.total_lateness_min)
        assert manual.distance_km == pytest.approx(full.score.distance_km)


def test_weighted_objective_can_accept_more_late_tasks_with_large_distance_gain():
    problem, routes = fixture(long_courier=True)
    ordinary = run(problem, routes)
    distance = run(problem, routes, weights=(.001, .001, .998), scales=(1., 1., 1.))
    assert ordinary["evaluation"].score.late_count == 0
    assert distance["evaluation"].score.late_count > ordinary["evaluation"].score.late_count
    assert distance["evaluation"].score.distance_km < ordinary["evaluation"].score.distance_km
    assert distance["metadata"]["final_key"][0] < distance["metadata"]["initial_key"][0]


def test_expired_and_late_full_confirmation_never_adopt(monkeypatch):
    problem, routes = fixture()
    result = run(problem, routes, deadline=perf_counter() - 1.)
    assert result["routes"] == routes and result["problem"] is problem
    assert result["metadata"]["deadline_reached"]
    clock = [0.]
    original = handoff.evaluate_solution
    monkeypatch.setattr(handoff, "perf_counter", lambda: clock[0])
    def delayed(p, candidate, **kwargs):
        checked = original(p, candidate, **kwargs)
        if p is not problem:
            clock[0] = 2.
        return checked
    monkeypatch.setattr(handoff, "evaluate_solution", delayed)
    result = run(problem, routes, deadline=1.)
    assert result["routes"] == routes and result["problem"] is problem
    assert result["metadata"]["accepted_improvements"] == 0


def test_global_work_finishing_after_deadline_cannot_create_resources(monkeypatch):
    problem, routes = fixture()
    clock = [0.]
    original = handoff.GlobalRelayEvaluator.evaluate
    monkeypatch.setattr(handoff, "perf_counter", lambda: clock[0])
    def delayed(self, candidate):
        checked = original(self, candidate)
        if self.problem is not problem:
            clock[0] = 2.
        return checked
    monkeypatch.setattr(handoff.GlobalRelayEvaluator, "evaluate", delayed)
    result = run(problem, routes, deadline=1.)
    assert result["routes"] == routes and result["problem"] is problem
    assert result["metadata"]["global_evaluations"] == 1


def test_custom_matrix_old_relay_inputs_and_bad_configuration_are_explicit():
    problem, routes = fixture()
    matrix = tuple(tuple(row) for row in problem._distances)
    result = run(replace(problem, distance_matrix_km=matrix), routes)
    assert result["routes"] == routes and result["metadata"]["unsupported_reason"] == "custom_distance_matrix"
    changed, candidate, _, _, _, _ = fixed_candidate(problem, routes)
    result = run(changed, candidate)
    assert result["routes"] == candidate and "pure_DIRECT" in result["metadata"]["unsupported_reason"]
    for changes in (dict(max_pairs=0), dict(point_limit=True), dict(max_group_size=3),
                    dict(weights=(1., 0., 0.)), dict(screening_fraction=1.)):
        with pytest.raises(ValueError):
            run(problem, routes, **changes)
    with pytest.raises(ValueError, match="invalid initial"):
        run(problem, ((3, 4, 1, -3, -4, -1), (2, -2)))


def test_checked_incumbent_and_shortlist_counts_reproduce_with_same_seed():
    problem, routes = fixture()
    one, two = run(problem, routes, seed=41), run(problem, routes, seed=41)
    assert one["routes"] == two["routes"] and one["evaluation"].score == two["evaluation"].score
    assert one["metadata"]["points_examined"] == two["metadata"]["points_examined"]
    config = one["metadata"]["configuration"]
    assert config["max_moves"] == 1 and config["sender_limit"] == 8 and config["receiver_limit"] == 4
    assert one["metadata"]["new_problems_built"] <= config["full_candidate_limit"]
    assert one["metadata"]["accepted_improvements"] == one["metadata"]["accepted_transfer_moves"] == 1
    assert one["metadata"]["accepted_control_only_moves"] == 0
    assert one["metadata"]["final_responsibility_preserved"]
    assert one["metadata"]["final_key"] <= one["metadata"]["initial_key"]


@pytest.mark.parametrize("receiver_home", [30., 50.])
def test_saved_result_is_bound_by_independent_physical_auditor(receiver_home):
    problem, routes = fixture(receiver_home=receiver_home, registry=True)
    result = run(problem, routes)
    artifact = SimpleNamespace(routes=result["routes"], evaluation=result["evaluation"],
        runtime_seconds=result["metadata"]["runtime_seconds"], iterations=result["iterations"],
        metadata=result["metadata"])
    payload = result_payload(result["problem"], artifact, source="tiny-anticipatory-candidate")
    tasks = {t.id: ((t.pickup.x, t.pickup.y), (t.delivery.x, t.delivery.y), t.deadline_min)
             for t in problem.tasks}
    audited = audit_anticipatory_payload(tasks, handoff.mobile.topology(problem),
        handoff.mobile.topology(result["problem"]), payload)
    assert audited["passed"] and audited["mobile_ground_cache_min"] == 0.
    assert audited["final_delivery_owner"] == {str(t): owner for t, owner in
        build_plan_index(problem, routes).delivery_route_by_task.items()}
    assert audited["score"]["distance_km"] == pytest.approx(result["evaluation"].score.distance_km)


def test_manual_score_obeys_production_lateness_threshold():
    tasks = (Task(1, Point(0., 0.), Point(1., 0.), 1. - 5e-10),)
    problem = Problem(tasks, drone_count=1, max_tasks_per_drone=1, speed_km_per_min=1.)
    score = handoff._route_score(problem, (1, -1), 0, {}, Point(0., 0.))
    checked = evaluate_solution(problem, ((1, -1),), _force_full=True)
    assert score == checked.score and score.late_count == 0 and score.total_lateness_min == 0.
