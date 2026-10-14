"""Tiny dispersed-pickup courier transfers with fixed-station caching."""
from dataclasses import replace
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import directed_bundle_insertion as directed


WEIGHTS, SCALES = (.5, .25, .25), (4., 100., 100.)


def fixture(*, receiver_home=30., long_courier=False, four_stations=False):
    tasks = (Task(1, Point(0., 0.), Point(30., 0.), 100.),
             Task(2, Point(20., 0.), Point(31., 0.), 100.),
             Task(3, Point(0., 0.), Point(100. if long_courier else 10., 0.), 100.),
             Task(4, Point(100. if long_courier else 10., 0.), Point(20., 0.), 200.))
    stations = (RelayStation(1, 20., 0.),)
    if four_stations:
        stations += (RelayStation(2, -100., 100.), RelayStation(3, 100., 100.), RelayStation(4, 100., -100.))
    registry, counter = {}, 5
    for task in tasks:
        for station in stations:
            registry[counter] = TransportLeg(counter, task.id, "RELAY_IN", station.id)
            registry[counter + 1] = TransportLeg(counter + 1, task.id, "RELAY_OUT", station.id)
            counter += 2
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=2, capacity=2,
        speed_km_per_min=1., drone_homes=(Point(0., 0.), Point(receiver_home, 0.)),
        relay_stations=stations, leg_registry=registry,
        task_relay_candidates={t.id: tuple(s.id for s in stations) for t in tasks})
    return problem, ((3, -3, 4, -4), (1, 2, -1, -2))


def run(problem, routes, **kwargs):
    return directed.refine(problem, routes, deadline=perf_counter() + 1., weights=WEIGHTS, scales=SCALES, **kwargs)


def candidate_for(problem, routes, *, first=1, handoff=4, order=(1, 2)):
    legs = directed._leg_pairs(problem)
    incoming = tuple(legs[t][1][0] for t in order)
    sender = directed._sender_route(routes[0], *incoming, first, handoff)
    out = {t: legs[t][1][1] for t in (1, 2)}
    receiver = (out[1], out[2], -out[1], -out[2])
    return sender, receiver


def test_dispersed_pickup_wins_where_every_contiguous_zero_gap_block_does_not():
    problem, routes = fixture()
    initial = evaluate_solution(problem, routes, _force_full=True)
    assert initial.valid and initial.score.distance_km == 81.
    contiguous = []
    for handoff in (0, 2, 4):
        for order in ((1, 2), (2, 1)):
            candidate = candidate_for(problem, routes, first=handoff, handoff=handoff, order=order)
            checked = evaluate_solution(problem, candidate, _force_full=True)
            assert checked.valid
            contiguous.append(checked.score.distance_km)
    assert min(contiguous) >= initial.score.distance_km
    result = run(problem, routes)
    assert result["evaluation"].valid and result["evaluation"].score.distance_km == 41.
    assert result["evaluation"].score.late_count == 0
    assert result["metadata"]["new_cross_uav_count"] == 2
    assert any(m["sender_first_position"] < m["sender_handoff_position"]
               for m in result["metadata"]["accepted_moves"])


def test_first_new_pickup_can_share_capacity_with_original_load_one():
    problem, routes = fixture()
    candidate = candidate_for(problem, routes, first=1, handoff=4)
    # +3, +IN1, -3, +4, -4, +IN2, -IN1, -IN2.
    assert candidate[0] == (3, 5, -3, 4, -4, 7, -5, -7)
    loads = directed._loads(candidate[0])
    assert loads[1:4] == (1, 2, 1)
    assert max(loads) == 2 and loads[-1] == 0
    assert evaluate_solution(problem, candidate, _force_full=True).valid
    # A load-two original frontier must not receive an extra first package.
    busy = (3, 4, -3, -4)
    assert (1, 4) not in directed._sender_positions(busy)
    assert not directed._capacity_feasible(directed._sender_route(busy, 5, 7, 1, 4), 2)


def test_final_responsibility_full_k_and_all_other_encoded_events_are_preserved():
    problem, routes = fixture()
    result = run(problem, routes)
    original, final = build_plan_index(problem, routes), build_plan_index(problem, result["routes"])
    assert dict(final.delivery_route_by_task) == dict(original.delivery_route_by_task)
    assert [sum(i == owner for owner in final.delivery_route_by_task.values()) for i in range(2)] == [2, 2]
    assert tuple(v for v in result["routes"][0] if abs(v) in (3, 4)) == routes[0]
    assert final.inbound_route_by_task[1] == final.inbound_route_by_task[2] == 0
    assert final.outbound_route_by_task[1] == final.outbound_route_by_task[2] == 1
    assert result["metadata"]["final_responsibility_preserved"]
    assert result["metadata"]["accepted_control_only_moves"] == 0


def test_actual_station_approach_and_ten_minute_receiver_wait_are_counted():
    problem, routes = fixture()
    candidate = candidate_for(problem, routes)
    schedule = GlobalRelayEvaluator(problem).evaluate(candidate)
    assert schedule.feasible
    assert schedule.event_times_min[(0, 6)] == 20.  # Actual sender arrives after own deliveries.
    assert schedule.event_times_min[(1, 0)] == 20.  # Receiver travelled 10min, then waited 10min.
    assert schedule.total_waiting_min == 10.
    assert dict(schedule.delivery_times_min) == {1: 30., 2: 31., 3: 10., 4: 20.}
    checked = evaluate_solution(problem, candidate, _force_full=True)
    assert checked.valid and checked.score == schedule.score and checked.score.distance_km == 41.


def test_fixed_station_cache_allows_sender_to_leave_before_later_receiving_uav():
    problem, routes = fixture(receiver_home=50.)
    candidate = candidate_for(problem, routes)
    schedule = GlobalRelayEvaluator(problem).evaluate(candidate)
    assert schedule.feasible and schedule.total_waiting_min == 0.
    assert schedule.event_times_min[(0, 6)] == 20.
    assert schedule.event_times_min[(1, 0)] == 30.
    result = run(problem, routes)
    assert result["metadata"]["fixed_station_cache"]
    assert not result["metadata"]["mobile_rendezvous"]


def test_all_four_fixed_stations_are_probed_and_shortlist_sizes_are_declared():
    problem, routes = fixture(four_stations=True)
    result = run(problem, routes)
    assert result["evaluation"].valid and result["evaluation"].score.distance_km == 41.
    assert result["metadata"]["stations_probed"] == 4
    assert all(m["station"] == 1 for m in result["metadata"]["accepted_moves"])
    assert result["metadata"]["sender_limit"] == 8
    assert result["metadata"]["receiver_limit"] == 4
    assert result["metadata"]["combination_limit"] == 8


def test_existing_relay_dependency_can_make_distributed_pickup_cycle_and_is_rejected():
    tasks = tuple(Task(i, Point(0., 0.), Point(0., 0.), 100.) for i in range(1, 5))
    registry = {}
    for t in tasks:
        inbound = 5 + 2 * (t.id - 1)
        registry[inbound] = TransportLeg(inbound, t.id, "RELAY_IN", 1)
        registry[inbound + 1] = TransportLeg(inbound + 1, t.id, "RELAY_OUT", 1)
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=2, speed_km_per_min=1.,
        relay_stations=(RelayStation(1, 0., 0.),), leg_registry=registry,
        task_relay_candidates={t.id: (1,) for t in tasks})
    routes = ((10, -10, 4, -4), (1, 2, -1, 9, -9, -2))
    assert evaluate_solution(problem, routes, _force_full=True).valid
    # A cannot drop the new pair until its prefix OUT3 receives old IN3,
    # while B cannot reach old IN3 until receiving A's new pair.
    sender = directed._sender_route(routes[0], 5, 7, 0, 4)
    receiver = (6, 8, -6, 9, -9, -8)
    assert not GlobalRelayEvaluator(problem).evaluate((sender, receiver)).feasible
    result = run(problem, routes)
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["metadata"]["rejected_cycles"] > 0


def test_weighted_j_can_accept_late_receiving_tasks_for_large_distance_saving():
    problem, routes = fixture(long_courier=True)
    initial = evaluate_solution(problem, routes, _force_full=True)
    ordinary = run(problem, routes)
    distance_priority = directed.refine(problem, routes, deadline=perf_counter() + 1.,
        weights=(.001, .001, .998), scales=(1., 1., 1.))
    assert initial.score.late_count == ordinary["evaluation"].score.late_count == 0
    assert initial.score.distance_km == 241.
    assert distance_priority["evaluation"].score.late_count == 2
    assert distance_priority["evaluation"].score.distance_km == 201.
    assert distance_priority["metadata"]["final_key"][0] < distance_priority["metadata"]["initial_key"][0]


def test_input_deadline_and_late_candidate_confirmation_do_not_adopt(monkeypatch):
    problem, routes = fixture()
    expired = directed.refine(problem, routes, deadline=perf_counter() - 1., weights=WEIGHTS, scales=SCALES)
    assert expired["routes"] == routes and expired["metadata"]["deadline_reached"]
    clock = [0.]
    original = directed.evaluate_solution
    monkeypatch.setattr(directed, "perf_counter", lambda: clock[0])
    def confirmation(p, candidate, **kwargs):
        checked = original(p, candidate, **kwargs)
        if tuple(tuple(r) for r in candidate) != routes:
            clock[0] = 2.
        return checked
    monkeypatch.setattr(directed, "evaluate_solution", confirmation)
    result = directed.refine(problem, routes, deadline=1., weights=WEIGHTS, scales=SCALES)
    assert result["routes"] == routes and result["metadata"]["accepted_improvements"] == 0
    assert result["metadata"]["deadline_reached"]


def test_global_evaluation_finishing_after_deadline_is_not_accepted(monkeypatch):
    problem, routes = fixture()
    clock = [0.]
    original = directed.GlobalRelayEvaluator.evaluate
    monkeypatch.setattr(directed, "perf_counter", lambda: clock[0])
    def evaluation(self, candidate):
        checked = original(self, candidate)
        if tuple(tuple(r) for r in candidate) != routes:
            clock[0] = 2.
        return checked
    monkeypatch.setattr(directed.GlobalRelayEvaluator, "evaluate", evaluation)
    result = directed.refine(problem, routes, deadline=1., weights=WEIGHTS, scales=SCALES)
    assert result["routes"] == routes and result["metadata"]["accepted_improvements"] == 0
    assert result["metadata"]["deadline_reached"]


def test_only_real_shared_frontier_pairs_and_registered_stations_are_eligible():
    problem, routes = fixture()
    separate = (routes[0], (1, -1, 2, -2))
    result = run(problem, separate)
    assert result["routes"] == separate and result["metadata"]["pair_probes"] == 0
    disabled = replace(problem, leg_registry={}, task_relay_candidates={})
    result = run(disabled, routes)
    assert result["routes"] == routes and result["iterations"] == 0


def test_bad_input_configuration_and_capacity_violation_are_rejected():
    problem, routes = fixture()
    with pytest.raises(ValueError, match="Invalid input"):
        run(replace(problem, capacity=1), routes)
    for changes in (dict(max_pairs=0), dict(weights=(1., 0., 0.)), dict(scales=(1., -1., 1.))):
        args = dict(deadline=perf_counter() + 1., weights=WEIGHTS, scales=SCALES)
        args.update(changes)
        with pytest.raises(ValueError):
            directed.refine(problem, routes, **args)


def test_checked_best_cross_only_statistics_and_seed_are_reproducible():
    problem, routes = fixture()
    first, second = run(problem, routes, seed=19), run(problem, routes, seed=19)
    assert first["routes"] == second["routes"]
    assert first["evaluation"].score == second["evaluation"].score
    assert first["metadata"]["global_evaluations"] == second["metadata"]["global_evaluations"]
    assert first["metadata"]["accepted_transfer_moves"] == first["metadata"]["accepted_improvements"]
    assert first["metadata"]["final_key"] <= first["metadata"]["initial_key"]
    assert first["metadata"]["neighborhood_exhausted"]
