"""Geometric screening bounds versus real handoff schedules; no optimization."""
from dataclasses import replace
from hashlib import sha256
from itertools import permutations, product
import json
from pathlib import Path
from random import Random

import pytest

from uav_dispatch import Point, Problem, RelayStation, Score, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import mobile_geometry_bound as geometry
from algorithm.experiments import mobile_relay_refinement as mobile


def fixture(*, paired=False, asymmetric=False, speed=1.):
    if not paired:
        tasks = (Task(1, Point(0., 0.), Point(10., 0.), 12.),
                 Task(2, Point(10., 0.), Point(0., 0.), 12.),
                 Task(3, Point(0., 0.), Point(0., 0.), 14.),
                 Task(4, Point(10., 0.), Point(10., 0.), 14.))
        routes = ((1, -1, 3, -3), (2, -2, 4, -4))
    else:
        tasks = (Task(1, Point(0., 0.), Point(10., 0.), 10.),
                 Task(2, Point(1., 0.), Point(11., 0.), 11.),
                 Task(3, Point(10., 0.), Point(0., 0.), 10.),
                 Task(4, Point(11., 0.), Point(1., 0.), 11.),
                 Task(5, Point(0., 0.), Point(0., 0.), 20.),
                 Task(6, Point(1., 0.), Point(1., 0.), 20.),
                 Task(7, Point(10., 0.), Point(10., 0.), 20.),
                 Task(8, Point(11., 0.), Point(11., 0.), 20.))
        routes = ((1, 2, -1, -2, 5, 6, -5, -6), (3, 4, -3, -4, 7, 8, -7, -8))
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=4 if paired else 2,
                      capacity=2, speed_km_per_min=speed,
                      drone_homes=(Point(-2. if asymmetric else 0., 0.), Point(10., 0.)))
    return problem, routes


def specification(problem, routes, positions=(1, 1), groups=((1,), (2,)), received=None):
    schedule = GlobalRelayEvaluator(problem).evaluate(routes)
    cuts = []
    for owner, position in enumerate(positions):
        active = []
        for visit in routes[owner][:position]:
            if visit > 0:
                active.append(visit)
            else:
                active.remove(-visit)
        cuts.append(mobile._Cut(position, tuple(active),
            problem.point_for_visit(routes[owner][position - 1]) if position else problem.home_points()[owner],
            schedule.event_times_min[(owner, position - 1)] if position else 0.))
    received = received or (groups[1], groups[0])
    return mobile._Spec(0, 1, *cuts, *groups, *received, ())


def actual(problem, routes, spec, point):
    candidate, legs, descriptor = mobile._prototype(problem, routes, spec, point)
    changed = mobile._problem(problem, legs, descriptor)
    checked = evaluate_solution(changed, candidate, _force_full=True)
    schedule = GlobalRelayEvaluator(changed).evaluate(candidate)
    assert checked.valid and schedule.feasible and checked.score == schedule.score
    return checked.score, schedule, candidate, changed


def assert_components_below(lower, real):
    assert lower.late_count <= real.late_count
    assert lower.total_lateness_min <= real.total_lateness_min + 1e-10
    assert lower.distance_km <= real.distance_km + 1e-9


def test_lower_bound_is_below_complete_global_scores_for_many_real_meeting_points():
    problem, routes = fixture(asymmetric=True)
    spec = specification(problem, routes)
    result = geometry.lower_bound(problem, routes, spec)
    for x, y in product((-5., 0., 2., 5., 10., 15.), (-4., 0., 6.)):
        score, _, _, _ = actual(problem, routes, spec, Point(x, y))
        assert_components_below(result["score"], score)
    assert json.loads(json.dumps(result["metadata"]))["kind"] == "absolute_mobile_geometry_lower_bound"


def test_changed_delivery_at_the_cut_uses_swapped_true_successor():
    problem, routes = fixture()
    spec = specification(problem, routes)
    result = geometry.lower_bound(problem, routes, spec)
    endpoints = result["metadata"]["terminals"]
    assert endpoints == dict(u_a=[0., 0.], v_a=[0., 0.], u_b=[10., 0.], v_b=[10., 0.])
    assert result["metadata"]["meeting_distance_lower_km"] == 20.
    assert result["score"].distance_km == pytest.approx(20. - geometry.DISTANCE_MARGIN_KM)
    # Each route's relaxed clock minimum uses a different R=v. They need not
    # jointly hold at a single R; each remains a valid individual lower bound.
    heads = result["metadata"]["route_bounds"]
    assert heads[0]["earliest_first_suffix_clock_min"] == 10.
    assert heads[1]["earliest_first_suffix_clock_min"] == 10.


def test_all_four_two_by_two_delivery_mappings_have_valid_component_bounds():
    problem, routes = fixture(paired=True)
    context = geometry.prepare_context(problem, routes)
    for receive_a, receive_b in product(permutations((3, 4)), permutations((1, 2))):
        spec = specification(problem, routes, (2, 2), ((1, 2), (3, 4)), (receive_a, receive_b))
        result = geometry.lower_bound(problem, routes, spec, context=context)
        assert result["metadata"]["terminals"]["v_a"] == [problem.task(receive_a[0]).delivery.x,
                                                          problem.task(receive_a[0]).delivery.y]
        for point in (Point(1., 0.), Point(5., 0.), Point(9., 3.)):
            score, _, _, _ = actual(problem, routes, spec, point)
            assert_components_below(result["score"], score)
    assert len(context.mapped) == 4  # Two per-aircraft mappings, reused across joint permutations.


def test_home_travel_prefix_time_and_nonunit_speed_are_used_not_cached_actual_cut_time():
    problem, routes = fixture(asymmetric=True, speed=.5)
    spec = specification(problem, routes)
    result = geometry.lower_bound(problem, routes, spec)
    assert result["metadata"]["no_wait_prefix_times_min"] == [4., 0.]
    assert result["metadata"]["speed_km_per_min"] == .5
    fake = replace(spec, left=replace(spec.left, actual_time=999.), right=replace(spec.right, actual_time=888.))
    assert geometry.lower_bound(problem, routes, fake)["score"] == result["score"]
    for point in (Point(0., 0.), Point(5., 0.), Point(10., 1.)):
        score, _, _, _ = actual(problem, routes, spec, point)
        assert_components_below(result["score"], score)


def test_unchanged_prefix_final_deliveries_are_counted_before_future_minima():
    problem, _ = fixture()
    problem = replace(problem, tasks=(*problem.tasks,
        Task(5, Point(-5., 0.), Point(0., 0.), 1.),
        Task(6, Point(15., 0.), Point(10., 0.), 1.)),
        drone_homes=(Point(-5., 0.), Point(15., 0.)), max_tasks_per_drone=3)
    routes = ((5, -5, 1, -1, 3, -3), (6, -6, 2, -2, 4, -4))
    spec = specification(problem, routes, (3, 3))
    result = geometry.lower_bound(problem, routes, spec)
    prefix = result["metadata"]["route_bounds"]
    assert [r["prefix_final_task_ids"] for r in prefix] == [[5], [6]]
    assert [r["prefix_final_delivery_count"] for r in prefix] == [1, 1]
    assert [r["prefix_late_count"] for r in prefix] == [1, 1]
    for point in (Point(0., 0.), Point(5., 0.), Point(10., 0.)):
        score, schedule, _, _ = actual(problem, routes, spec, point)
        assert schedule.delivery_times_min[5] == schedule.delivery_times_min[6] == 5.
        assert_components_below(result["score"], score)


def test_old_inbound_is_not_final_old_outbound_is_final_and_old_waits_are_omitted():
    tasks = (Task(1, Point(0., 0.), Point(10., 0.), 20.),
             Task(2, Point(10., 0.), Point(0., 0.), 20.),
             Task(3, Point(30., 0.), Point(0., 0.), 2.),
             Task(4, Point(0., 0.), Point(0., 0.), 10.))
    problem = Problem(tasks, drone_count=3, max_tasks_per_drone=2, speed_km_per_min=1.,
        drone_homes=(Point(0., 0.), Point(10., 0.), Point(10., 0.)),
        relay_stations=(RelayStation(1, 0., 0.),),
        leg_registry={5: TransportLeg(5, 3, "RELAY_IN", 1), 6: TransportLeg(6, 3, "RELAY_OUT", 1)},
        task_relay_candidates={3: (1,)})
    routes = ((5, -5, 1, -1), (2, -2), (6, -6, 4, -4))
    spec = specification(problem, routes, (3, 1))
    result = geometry.lower_bound(problem, routes, spec)
    assert result["metadata"]["route_bounds"][0]["prefix_final_task_ids"] == []
    untouched = result["metadata"]["untouched_route_bounds"][0]
    assert untouched["route_index"] == 2 and untouched["lower_late_count"] == 1
    assert untouched["lower_lateness_min"] == pytest.approx(8. - geometry.CLOCK_MARGIN_MIN)
    assert geometry.prepare_context(problem, routes).layouts[2].finals == (None, 3, None, 4)
    for point in (Point(0., 0.), Point(5., 0.), Point(10., 4.)):
        score, schedule, _, _ = actual(problem, routes, spec, point)
        assert schedule.delivery_times_min[3] == schedule.delivery_times_min[4] == 60.
        assert result["score"].total_lateness_min < score.total_lateness_min - 99.
        assert_components_below(result["score"], score)


@pytest.mark.parametrize("deadline_delta", [0., -5e-10, -1e-9, -1.0001e-9, -1e-8])
def test_exact_deadline_and_late_threshold_rounding_cannot_increase_lower_late_count(deadline_delta):
    problem, routes = fixture()
    problem = replace(problem, tasks=tuple(replace(t, deadline_min=10. + deadline_delta)
                                          if t.id in (1, 2) else t for t in problem.tasks))
    spec = specification(problem, routes)
    lower = geometry.lower_bound(problem, routes, spec)["score"]
    score, _, _, _ = actual(problem, routes, spec, Point(5., 0.))
    assert_components_below(lower, score)


def test_random_small_geometries_and_real_complete_points_never_violate_the_bound():
    rng = Random(7103)
    for _ in range(12):
        point = lambda: Point(rng.uniform(-8., 8.), rng.uniform(-8., 8.))
        tasks = tuple(Task(i, point(), point(), rng.uniform(2., 35.)) for i in range(1, 5))
        problem = Problem(tasks, drone_count=2, max_tasks_per_drone=2, capacity=2,
            speed_km_per_min=rng.uniform(.3, 2.), drone_homes=(point(), point()))
        routes = ((1, 3, -1, -3), (2, 4, -2, -4))
        spec = specification(problem, routes, (2, 2))  # Subset exchange, other cargo remains aboard.
        result = geometry.lower_bound(problem, routes, spec)
        for _ in range(10):
            score, _, _, _ = actual(problem, routes, spec, point())
            assert_components_below(result["score"], score)


def test_existing_tiny_mobile_gain_is_not_pruned_by_the_new_bound():
    problem, routes = fixture()
    problem = replace(problem, tasks=tuple(replace(t, deadline_min=100.) for t in problem.tasks))
    spec = specification(problem, routes)
    point = Point(5., 0.)
    initial = evaluate_solution(problem, routes, _force_full=True).score
    improved, _, _, _ = actual(problem, routes, spec, point)
    lower = geometry.lower_bound(problem, routes, spec)["score"]
    weights, scales = (.5, .25, .25), (4., 100., 100.)
    assert improved.distance_km == 20. < initial.distance_km == 40.
    assert mobile.weighted_key(lower, weights, scales) <= mobile.weighted_key(improved, weights, scales)
    assert mobile.weighted_key(lower, weights, scales) < mobile.weighted_key(initial, weights, scales)


def test_saved_audited_batch009_gain_is_not_pruned_without_running_any_official_solver():
    # Read-only receipt regression. Reconstruct only baseline geometry; use the
    # independently audited saved final score, not a new official evaluation.
    root = Path(__file__).resolve().parents[2]
    output = root / "algorithm/results/proactive_relay_diagnostic_20261014/batch009"
    manifest_bytes, runs_bytes = (output / "manifest.json").read_bytes(), (output / "runs.json").read_bytes()
    audit = json.loads((output / "audit.json").read_text())
    assert audit["passed"] and audit["source_manifest_sha256"] == sha256(manifest_bytes).hexdigest()
    assert audit["source_runs_sha256"] == sha256(runs_bytes).hexdigest()
    manifest = json.loads(manifest_bytes)
    from uav_dispatch.io import load_tasks_csv
    tasks = load_tasks_csv(root / manifest["input_path"])
    saved = json.loads((output / "phase_solutions/mobile__seed_2026101496__mobile.json").read_text())["metadata"]
    initial, final = saved["input_snapshot"], saved["accepted_moves"][-1]["full_snapshot"]
    topology = initial["topology"]
    problem = Problem(tasks, drone_count=topology["drone_count"], max_tasks_per_drone=topology["max_tasks_per_drone"],
        capacity=topology["capacity"], speed_km_per_min=topology["speed_km_per_min"],
        drone_homes=tuple(Point(*p) for p in topology["home_points"]))
    routes = tuple(tuple(r) for r in initial["routes"])
    meeting = final["mobile_meetings"][-1]
    a, b = meeting["aircraft"]
    ga, gb = map(tuple, meeting["own_task_groups"])
    ka, kb = meeting["original_cut_positions"]
    cut_a = mobile._Cut(ka, ga, problem.point_for_visit(routes[a][ka - 1]), 0.)
    cut_b = mobile._Cut(kb, gb, problem.point_for_visit(routes[b][kb - 1]), 0.)
    spec = mobile._Spec(a, b, cut_a, cut_b, ga, gb, gb, ga, ())
    lower = geometry.lower_bound(problem, routes, spec)["score"]
    real, baseline = Score(**final["score"]), Score(**initial["score"])
    assert_components_below(lower, real)
    assert mobile.weighted_key(real, manifest["weights"], manifest["scales"]) < mobile.weighted_key(baseline, manifest["weights"], manifest["scales"])
    assert mobile.weighted_key(lower, manifest["weights"], manifest["scales"]) < mobile.weighted_key(baseline, manifest["weights"], manifest["scales"])


def test_cached_context_is_reused_but_rejects_other_input_generations():
    problem, routes = fixture()
    spec = specification(problem, routes)
    context = geometry.prepare_context(problem, routes)
    first = geometry.lower_bound(problem, routes, spec, context=context)
    second = geometry.lower_bound(problem, routes, spec, context=context)
    assert first == second and len(context.mapped) == 2
    with pytest.raises(ValueError, match="another problem/routes"):
        geometry.lower_bound(replace(problem, speed_km_per_min=2.), routes, spec, context=context)
    with pytest.raises(ValueError, match="another problem/routes"):
        geometry.lower_bound(problem, (tuple(reversed(routes[0])), routes[1]), spec, context=context)


def test_nonmetric_matrix_and_invalid_cargo_spec_are_rejected():
    problem, routes = fixture()
    spec = specification(problem, routes)
    custom = replace(problem, distance_matrix_km=problem._distances)
    with pytest.raises(ValueError, match="Euclidean"):
        geometry.lower_bound(custom, routes, spec)
    with pytest.raises(ValueError, match="active"):
        geometry.lower_bound(problem, routes, replace(spec, group_a=(3,), receive_b=(3,)))
    with pytest.raises(ValueError, match="permutations"):
        geometry.lower_bound(problem, routes, replace(spec, receive_a=(1,)))
