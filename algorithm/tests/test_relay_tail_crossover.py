"""Tiny physical cargo-transfer, timing and control tests; no official solving."""
from dataclasses import replace
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import relay_tail_crossover as crossover


WEIGHTS, SCALES = (.5, .25, .25), (4., 100., 100.)


def fixture(*, third_deadline=85.):
    tasks = (Task(1, Point(0., 0.), Point(0., 0.), 0.),
             Task(2, Point(10., 0.), Point(40., 0.), 30.),
             Task(3, Point(-10., 0.), Point(10., 0.), third_deadline),
             Task(4, Point(0., 0.), Point(0., 0.), 35.))
    registry = {}
    for task in tasks:
        inbound = 5 + 2 * (task.id - 1)
        registry[inbound] = TransportLeg(inbound, task.id, "RELAY_IN", 1)
        registry[inbound + 1] = TransportLeg(inbound + 1, task.id, "RELAY_OUT", 1)
    problem = Problem(tasks, drone_count=2, drone_homes=(Point(0., 0.), Point(10., 0.)),
                      max_tasks_per_drone=2, capacity=2, speed_km_per_min=1.,
                      relay_stations=(RelayStation(1, 0., 0.),), leg_registry=registry,
                      task_relay_candidates={t.id: (1,) for t in tasks})
    return problem, ((1, -1, 3, -3), (2, -2, 4, -4))


def run(problem, routes, **kwargs):
    return crossover.refine(problem, routes, deadline=perf_counter() + 1.,
                            weights=WEIGHTS, scales=SCALES, **kwargs)


def spec_for(problem, routes, *, cut_a, cut_b, station=1):
    evaluator = GlobalRelayEvaluator(problem)
    schedule = evaluator.evaluate(routes)
    cuts_a, _ = crossover._cuts(problem, routes[0], 0, schedule, enable_transfers=True)
    cuts_b, _ = crossover._cuts(problem, routes[1], 1, schedule, enable_transfers=True)
    a = next(c for c in cuts_a if c.position == cut_a)
    b = next(c for c in cuts_b if c.position == cut_b)
    return crossover._Spec(0, 1, a, b, station, 0., 0., ())


def test_empty_receiver_tail_crossover_improves_beyond_empty_only_control():
    problem, routes = fixture()
    initial = evaluate_solution(problem, routes, _force_full=True)
    control = run(problem, routes, enable_transfers=False, max_pairs=1)
    result = run(problem, routes, enable_transfers=True, max_pairs=1)
    assert initial.valid and initial.score.late_count == 1
    assert control["evaluation"].score.late_count == 1
    assert control["evaluation"].score.total_lateness_min == 15.
    assert result["evaluation"].valid and result["evaluation"].score.late_count == 0
    assert result["evaluation"].score.total_lateness_min == 0.
    assert result["evaluation"].score.distance_km == initial.score.distance_km == 100.
    assert result["metadata"]["accepted_transfer_moves"] >= 1
    assert result["metadata"]["final_cross_uav_count"] == 1
    assert result["metadata"]["new_cross_uav_count"] == 1
    assert result["metadata"]["batches"] > 1  # max_pairs is a batch size, not truncation.


def test_task_plan_conversion_and_full_final_k_conservation():
    problem, routes = fixture()
    result = run(problem, routes)
    index = build_plan_index(problem, result["routes"])
    assert index.task_plans[3].mode == "RELAY"
    assert index.inbound_route_by_task[3] == 0 and index.outbound_route_by_task[3] == 1
    assert dict(index.delivery_route_by_task) == {1: 0, 2: 1, 3: 1, 4: 0}
    assert [sum(i == route for i in index.delivery_route_by_task.values()) for route in range(2)] == [2, 2]
    assert result["routes"] == ((1, -1, 9, -9, 4, -4), (2, -2, 10, -10))
    for route in result["routes"]:
        load = 0
        for visit in route:
            load += 1 if visit > 0 else -1
            assert 0 <= load <= problem.capacity
        assert load == 0


def test_asynchronous_cache_uses_actual_prefix_travel_without_free_reposition():
    problem, routes = fixture()
    result = run(problem, routes)
    schedule = GlobalRelayEvaluator(problem).evaluate(result["routes"])
    # A picks at -10 then physically flies to S=0 by t=20; B finishes at
    # x=40,t=30 and physically flies 40km to S by t=70. Package cache=50.
    assert schedule.event_times_min[(0, 3)] == 20.
    assert schedule.event_times_min[(1, 2)] == 70.
    assert schedule.delivery_times_min[3] == 80.
    assert schedule.delivery_times_min[4] == 20.
    assert schedule.total_waiting_min == 0.
    assert sum(result["evaluation"].route_distances_km) == 100.


def test_two_loaded_sides_unload_every_own_package_before_taking_other_side():
    problem, routes = fixture()
    spec = spec_for(problem, routes, cut_a=3, cut_b=3)
    changed = crossover._candidate(problem, routes, spec, crossover._leg_pairs(problem))
    assert changed == ((1, -1, 9, -9, 12, -12), (2, -2, 11, -11, 10, -10))
    checked = evaluate_solution(problem, changed, _force_full=True)
    assert checked.valid
    schedule = GlobalRelayEvaluator(problem).evaluate(changed)
    assert schedule.total_waiting_min == 50.
    assert schedule.delivery_times_min[3] == 80. and schedule.delivery_times_min[4] == 70.


def test_two_packages_each_way_never_exceeds_capacity_two():
    problem, routes = fixture()
    tasks = (Task(1, Point(0., 0.), Point(40., 0.), 100.),
             Task(2, Point(10., 0.), Point(-10., 0.), 100.),
             Task(3, Point(0., 0.), Point(40., 0.), 100.),
             Task(4, Point(10., 0.), Point(-10., 0.), 100.))
    problem = replace(problem, tasks=tasks)
    routes = ((1, 3, -1, -3), (2, 4, -2, -4))
    spec = spec_for(problem, routes, cut_a=2, cut_b=2)
    changed = crossover._candidate(problem, routes, spec, crossover._leg_pairs(problem))
    checked = evaluate_solution(problem, changed, _force_full=True)
    assert checked.valid
    for route in changed:
        load, peak = 0, 0
        for visit in route:
            load += 1 if visit > 0 else -1
            peak = max(peak, load)
            assert 0 <= load <= 2
        assert load == 0 and peak == 2
    assert len(build_plan_index(problem, changed).relay_task_ids) == 4


def test_unequal_two_and_one_package_exchange_unloads_before_loading():
    problem, _ = fixture()
    problem = replace(problem, tasks=(Task(1, Point(0., 0.), Point(40., 0.), 100.),
        Task(2, Point(10., 0.), Point(-10., 0.), 100.),
        Task(3, Point(0., 0.), Point(40., 0.), 100.),
        Task(4, Point(10., 0.), Point(-10., 0.), 100.)))
    routes = ((1, 3, -1, -3), (2, -2, 4, -4))
    spec = spec_for(problem, routes, cut_a=2, cut_b=1)
    assert len(spec.left.active) == 2 and len(spec.right.active) == 1
    candidate = crossover._candidate(problem, routes, spec, crossover._leg_pairs(problem))
    checked = evaluate_solution(problem, candidate, _force_full=True)
    assert checked.valid
    index = build_plan_index(problem, candidate)
    assert len(index.relay_task_ids) == 3
    assert [sum(i == route for i in index.delivery_route_by_task.values()) for route in range(2)] == [2, 2]
    for route in candidate:
        load = 0
        for event in route:
            load += 1 if event > 0 else -1
            assert 0 <= load <= 2
        assert load == 0


def test_two_loaded_domination_prune_is_limited_to_euclidean_model():
    problem, routes = fixture()
    result = run(problem, routes)
    assert result["metadata"]["both_loaded_structural_prune_active"]
    assert result["metadata"]["both_loaded_pairs_pruned"] > 0
    # A caller-supplied matrix is not assumed to satisfy triangle inequality.
    custom = replace(problem, distance_matrix_km=problem._distances)
    result = run(custom, routes)
    assert not result["metadata"]["both_loaded_structural_prune_active"]
    assert result["metadata"]["both_loaded_pairs_pruned"] == 0
    assert result["evaluation"].valid


def test_cuts_count_only_final_deliveries_and_reject_active_existing_legs():
    problem, _ = fixture()
    routes = ((5, -5, 9, -9), (6, -6, 2, -2, 10, -10, 4, -4))
    # IN1 and IN3 on A; OUT1 and OUT3 on B. K on B would exceed 2,
    # but cut bookkeeping depends only on transport semantics here.
    schedule = GlobalRelayEvaluator(problem).evaluate(routes)
    cuts_a, rejected_a = crossover._cuts(problem, routes[0], 0, schedule, enable_transfers=True)
    cuts_b, rejected_b = crossover._cuts(problem, routes[1], 1, schedule, enable_transfers=True)
    assert rejected_a == 2 and rejected_b == 2
    assert [(c.position, c.final_done) for c in cuts_a] == [(0, 0), (2, 0), (4, 0)]
    assert next(c for c in cuts_b if c.position == 2).final_done == 1
    assert next(c for c in cuts_b if c.position == 6).final_done == 3
    assert all(all(t not in problem.leg_registry for t in c.active) for c in cuts_a + cuts_b)


def test_disabled_transfers_only_selects_zero_cargo_cuts_and_can_use_control_moves():
    problem, _ = fixture()
    problem = replace(problem, drone_homes=(Point(0., 0.), Point(10., 0.)),
                      tasks=(Task(1, Point(0., 0.), Point(0., 0.), 100.),
                             Task(2, Point(10., 0.), Point(10., 0.), 100.),
                             Task(3, Point(10., 0.), Point(10., 0.), 100.),
                             Task(4, Point(0., 0.), Point(0., 0.), 100.)))
    routes = ((1, -1, 3, -3), (2, -2, 4, -4))
    result = run(problem, routes, enable_transfers=False)
    assert result["evaluation"].valid and result["evaluation"].score.distance_km == 0.
    assert result["metadata"]["accepted_control_only_moves"] >= 1
    assert result["metadata"]["accepted_transfer_moves"] == 0
    assert result["metadata"]["final_cross_uav_count"] == 0
    assert all(m["station"] is None and not m["cross_tasks"] for m in result["metadata"]["accepted_moves"])


def test_local_lower_bound_never_includes_old_global_waiting(monkeypatch):
    problem, routes = fixture()
    result = run(problem, routes)
    # Start from the two-loaded-side candidate that has a 50min wait.
    loaded = crossover._candidate(problem, routes, spec_for(problem, routes, cut_a=3, cut_b=3),
                                  crossover._leg_pairs(problem))
    checked = evaluate_solution(problem, loaded, _force_full=True)
    assert checked.valid
    local = crossover.RouteEvaluator(problem)
    local_score = sum((local.evaluate(r, start_node=problem.home_node(i)).score.distance_km
                       for i, r in enumerate(loaded)))
    assert local_score == checked.score.distance_km
    assert sum(local.evaluate(r, start_node=problem.home_node(i)).score.total_lateness_min
               for i, r in enumerate(loaded)) < checked.score.total_lateness_min
    # The module uses the local absolute sum and remains checked/nonworsening
    # even from a state whose global and local clocks substantially differ.
    refined = run(problem, loaded)
    coeff = crossover._coefficients(WEIGHTS, SCALES)
    assert refined["evaluation"].valid
    assert crossover._key(refined["evaluation"].score, coeff) <= crossover._key(checked.score, coeff)


def test_scalar_order_and_physical_tie_break_are_explicit():
    coeff = crossover._coefficients((.5, .25, .25), (1., 1., 1.))
    assert crossover.Score(1, 0., 0.) > crossover.Score(0, 0., 3.)
    assert crossover._key(crossover.Score(1, 0., 0.), coeff) < crossover._key(crossover.Score(0, 0., 3.), coeff)
    assert crossover._key(crossover.Score(0, 2., 0.), coeff) < crossover._key(crossover.Score(1, 0., 0.), coeff)


def test_real_control_search_uses_weighted_tradeoff_instead_of_lexicographic_score():
    problem, routes = fixture()
    problem = replace(problem, tasks=(problem.tasks[0], problem.tasks[1],
        Task(3, Point(10., 0.), Point(10., 0.), 25.),
        Task(4, Point(0., 0.), Point(0., 0.), 100.)))
    initial = evaluate_solution(problem, routes, _force_full=True)
    assert initial.score.late_count == 0 and initial.score.distance_km == 80.
    standard = run(problem, routes, enable_transfers=False)
    distance_priority = crossover.refine(problem, routes, deadline=perf_counter() + 1.,
        weights=(.001, .001, .998), scales=(1., 1., 1.), enable_transfers=False)
    assert standard["evaluation"].score.late_count == 0
    assert distance_priority["evaluation"].score.late_count == 1
    assert distance_priority["evaluation"].score.distance_km == 60.
    assert distance_priority["metadata"]["final_key"][0] < distance_priority["metadata"]["initial_key"][0]


def test_swapping_complete_future_old_legs_can_make_global_cycle_and_is_rejected(monkeypatch):
    tasks = tuple(Task(i, Point(0., 0.), Point(1., 0.), 100.) for i in range(1, 4))
    registry = {}
    for task in tasks:
        registry[2 * task.id + 2] = TransportLeg(2 * task.id + 2, task.id, "RELAY_IN", 1)
        registry[2 * task.id + 3] = TransportLeg(2 * task.id + 3, task.id, "RELAY_OUT", 1)
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=3, speed_km_per_min=1.,
        relay_stations=(RelayStation(1, 0., 0.),), leg_registry=registry,
        task_relay_candidates={t.id: (1,) for t in tasks})
    routes = ((4, -4, 5, -5, 6, -6), (7, -7, 8, -8, 9, -9))
    assert evaluate_solution(problem, routes, _force_full=True).valid
    spec = spec_for(problem, routes, cut_a=4, cut_b=2, station=None)
    assert spec.left.final_done == spec.right.final_done == 1
    assert not spec.left.active and not spec.right.active
    candidate = crossover._candidate(problem, routes, spec, crossover._leg_pairs(problem))
    assert not GlobalRelayEvaluator(problem).evaluate(candidate).feasible
    assert not evaluate_solution(problem, candidate, _force_full=True).valid
    # Feed the one real cyclic proposal through the search acceptance path.
    monkeypatch.setattr(crossover, "_specifications", lambda *args, **kwargs: [spec])
    result = run(problem, routes, enable_transfers=False)
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["metadata"]["rejected_cycles"] == 1
    assert result["metadata"]["accepted_improvements"] == 0


def test_expired_deadline_does_not_search_or_adopt():
    problem, routes = fixture()
    result = crossover.refine(problem, routes, deadline=perf_counter() - 1., weights=WEIGHTS, scales=SCALES)
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["iterations"] == 0 and result["metadata"]["deadline_reached"]


@pytest.mark.parametrize("expired_during", ["global", "confirmation"])
def test_candidate_completing_after_deadline_is_not_adopted(monkeypatch, expired_during):
    problem, routes = fixture()
    clock = [0.]
    monkeypatch.setattr(crossover, "perf_counter", lambda: clock[0])
    original_global, original_confirm = crossover.GlobalRelayEvaluator.evaluate, crossover.evaluate_solution
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
    monkeypatch.setattr(crossover.GlobalRelayEvaluator, "evaluate", global_call)
    monkeypatch.setattr(crossover, "evaluate_solution", confirmation)
    result = crossover.refine(problem, routes, deadline=1., weights=WEIGHTS, scales=SCALES)
    assert result["routes"] == routes and result["metadata"]["accepted_improvements"] == 0
    assert result["metadata"]["deadline_reached"]


def test_identical_home_whole_route_permutation_is_skipped():
    problem, routes = fixture()
    problem = replace(problem, drone_homes=(Point(0., 0.), Point(0., 0.)))
    result = run(problem, routes, enable_transfers=False)
    assert result["metadata"]["symmetric_home_swaps_skipped"] > 0
    assert not any(m["cut_a"] == m["cut_b"] == 0 for m in result["metadata"]["accepted_moves"])


def test_invalid_incumbent_and_bad_configuration_fail_loudly():
    problem, routes = fixture()
    with pytest.raises(ValueError, match="Invalid input"):
        run(problem, ((1, -1, 3, -3, 2, -2, 4, -4), ()))
    for changes in (dict(max_pairs=0), dict(weights=(1., 0., 0.)), dict(scales=(1., -1., 1.))):
        kwargs = dict(deadline=perf_counter() + 1., weights=WEIGHTS, scales=SCALES)
        kwargs.update(changes)
        with pytest.raises(ValueError):
            crossover.refine(problem, routes, **kwargs)


def test_memo_and_weighted_incumbent_guard_remain_deterministic():
    problem, routes = fixture()
    first, second = run(problem, routes, seed=7), run(problem, routes, seed=7)
    assert first["routes"] == second["routes"]
    assert first["evaluation"].score == second["evaluation"].score
    assert first["metadata"]["global_evaluations"] == second["metadata"]["global_evaluations"]
    assert first["metadata"]["duplicate_candidates_skipped"] > 0
    assert first["metadata"]["final_key"] <= first["metadata"]["initial_key"]
    assert first["metadata"]["neighborhood_exhausted"]
