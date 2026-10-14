"""Tiny non-monotone reconstruction tests; no official instance is solved."""
from dataclasses import replace
import json
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import relay_cluster_beam as beam


def fixture(*, context=False):
    tasks = [Task(1, Point(0., 0.), Point(10., 0.), 100.),
             Task(2, Point(1., 0.), Point(11., 0.), 100.),
             Task(3, Point(0., 0.), Point(0., 0.), 100.),
             Task(4, Point(0., 0.), Point(0., 0.), 100.)]
    if context:
        tasks += [Task(5, Point(0., 0.), Point(5., 0.), 100.),
                  Task(6, Point(5., 0.), Point(10., 0.), 100.)]
    registry = {}
    for task in tasks:
        inbound = len(tasks) + 1 + 2 * (task.id - 1)
        registry[inbound] = TransportLeg(inbound, task.id, "RELAY_IN", 1)
        registry[inbound + 1] = TransportLeg(inbound + 1, task.id, "RELAY_OUT", 1)
    problem = Problem(tuple(tasks), drone_count=2, max_tasks_per_drone=3 if context else 2,
        capacity=2, speed_km_per_min=1., drone_homes=(Point(0., 0.), Point(10., 0.)),
        relay_stations=(RelayStation(1, 5., 0.),), leg_registry=registry,
        task_relay_candidates={task.id: (1,) for task in tasks})
    routes = ((3, -3, 4, -4), (1, 2, -1, -2))
    if context:
        routes = ((5, 3, -3, 4, -4, -5), (6, 1, -1, 2, -2, -6))
    return problem, routes


def run(problem, routes, **kwargs):
    return beam.refine(problem, routes, deadline=perf_counter() + 3.,
        weights=(.5, .25, .25), scales=(len(problem.tasks), 100., 100.),
        max_clusters=1, **kwargs)


def counters():
    return dict(partial_evaluations=0, rejected_invalid_partial=0,
        rejected_wrong_partial_coverage=0, rejected_cycles=0, deadline_reached=False,
        beam_stages=[])


def test_four_task_combined_relay_improves_and_preserves_saturated_k():
    problem, routes = fixture()
    result = run(problem, routes)
    assert evaluate_solution(problem, routes).score.distance_km == 21.
    assert result["evaluation"].valid and result["evaluation"].score.distance_km == 16.
    assert result["metadata"]["final_cross_uav_count"] == 2
    index = build_plan_index(problem, result["routes"])
    assert [sum(owner == i for owner in index.delivery_route_by_task.values()) for i in range(2)] == [2, 2]
    assert set(result["evaluation"].delivery_times_min) == set(problem.task_ids)
    for route in result["routes"]:
        load = 0
        for visit in route:
            load += 1 if visit > 0 else -1
            assert 0 <= load <= 2
        assert load == 0


def test_direct_control_retains_same_selector_limits_and_polish_rules():
    problem, routes = fixture()
    direct = run(problem, routes, relay_enabled=False)
    relay = run(problem, routes, relay_enabled=True)
    assert direct["metadata"]["selected_clusters"] == relay["metadata"]["selected_clusters"]
    assert direct["metadata"]["configuration"] == relay["metadata"]["configuration"]
    assert direct["evaluation"].valid and direct["evaluation"].score.distance_km == 21.
    assert not build_plan_index(problem, direct["routes"]).relay_task_ids
    assert direct["metadata"]["complete_proposals"] > 0


def test_partial_missing_tasks_are_internal_and_never_returned_as_a_solution(monkeypatch):
    problem, routes = fixture()
    observed = []
    original = beam._partial
    def partial(p, candidate, expected, evaluator, stats):
        observed.append(frozenset(expected))
        return original(p, candidate, expected, evaluator, stats)
    monkeypatch.setattr(beam, "_partial", partial)
    result = run(problem, routes)
    assert any(len(s) < 4 for s in observed)
    assert [len(s) for s in observed if len(s) < 4]
    assert result["evaluation"].valid and len(result["evaluation"].delivery_times_min) == 4
    for record in result["metadata"]["cluster_records"]:
        for candidate in record["complete_candidates"]:
            assert candidate["full_proposal"]["complete"]
            assert evaluate_solution(problem, candidate["full_proposal"]["routes"], _force_full=True).valid


def test_over_budget_partial_state_cannot_displace_complete_incumbent(monkeypatch):
    problem, routes = fixture()
    clock = [0.]
    monkeypatch.setattr(beam, "perf_counter", lambda: clock[0])
    original = beam._partial
    def partial(*args, **kwargs):
        result = original(*args, **kwargs)
        if len(args[2]) == 2:
            clock[0] = 2.
        return result
    monkeypatch.setattr(beam, "_partial", partial)
    result = beam.refine(problem, routes, deadline=1., weights=(.5, .25, .25), scales=(4., 100., 100.))
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["metadata"]["complete_proposals"] == 0
    assert result["metadata"]["accepted_improvements"] == 0
    assert result["metadata"]["deadline_reached"]


@pytest.mark.parametrize("expiration", ["full", "polish"])
def test_over_budget_full_check_or_polish_never_adopts(monkeypatch, expiration):
    problem, routes = fixture()
    clock = [0.]
    monkeypatch.setattr(beam, "perf_counter", lambda: clock[0])
    original_full, original_polish, calls = beam.evaluate_solution, beam._polish, [0]
    def full(*args, **kwargs):
        result = original_full(*args, **kwargs)
        calls[0] += 1
        if expiration == "full" and calls[0] > 1:
            clock[0] = 2.
        return result
    def polish(*args, **kwargs):
        result = original_polish(*args, **kwargs)
        if expiration == "polish":
            clock[0] = 2.
        return result
    monkeypatch.setattr(beam, "evaluate_solution", full)
    monkeypatch.setattr(beam, "_polish", polish)
    result = beam.refine(problem, routes, deadline=1., weights=(.5, .25, .25), scales=(4., 100., 100.))
    assert result["routes"] == routes and result["metadata"]["accepted_improvements"] == 0
    assert result["metadata"]["deadline_reached"]


def test_present_complete_plan_and_real_cycle_are_checked_in_partial_scheduler():
    problem, _ = fixture()
    legs = beam._legs(problem)
    in1, out1 = legs[1][1]
    in2, out2 = legs[2][1]
    # Each aircraft waits to receive the other parcel before it can drop its
    # own: complete transport legs and legal load, but a real global cycle.
    cyclic = ((out2, -out2, in1, -in1), (out1, -out1, in2, -in2))
    stats = counters()
    assert beam._partial(problem, cyclic, {1, 2}, GlobalRelayEvaluator(problem), stats) is None
    assert stats["rejected_cycles"] == 1
    incomplete_leg = ((in1, -in1), ())
    assert beam._partial(problem, incomplete_leg, {1}, GlobalRelayEvaluator(problem), stats) is None
    assert stats["rejected_invalid_partial"] == 1


def test_rebuild_can_insert_at_load_one_without_moving_other_events():
    problem, routes = fixture(context=True)
    cluster = dict(route_indices=(0, 1), task_ids=(1, 2, 3, 4), original_pairs=((3, 4), (1, 2)))
    stats = counters()
    states = beam._rebuild(problem, routes, cluster, beam._legs(problem), RouteEvaluator(problem),
        GlobalRelayEvaluator(problem), (.5, .25, .25), (6., 100., 100.), True, 6, 20, 3,
        perf_counter() + 3., stats)
    assert states
    assert all(beam._preserves_other_events(problem, routes, state.routes, {1, 2, 3, 4}) for state in states)
    assert all(evaluate_solution(problem, state.routes, _force_full=True).valid for state in states)
    # Some new cargo shares a nonzero physical segment with the untouched
    # context task: insertion is genuinely inside an existing loaded interval.
    assert any(any(any(t in (5, 6) for t in pair) and any(t in (1, 2, 3, 4) for t in pair) and km > 0
                   for adjacency in beam._double_loaded_adjacency(problem, state.routes)
                   for pair, km in adjacency.items()) for state in states)


def test_real_double_loaded_adjacency_measures_before_event_segments():
    problem, routes = fixture()
    shared = beam._double_loaded_adjacency(problem, routes)
    assert shared[0] == {}
    assert shared[1][(1, 2)] == 9.


def test_nonmonotone_full_proposals_and_post_polish_are_auditable_json():
    problem, routes = fixture(context=True)
    result = run(problem, routes, proposal_margin=1.)
    metadata = result["metadata"]
    assert metadata["complete_proposals"] > 0
    assert metadata["first_complete_candidate"] is not None
    assert metadata["best_constructed_candidate"] is not None
    for record in metadata["cluster_records"]:
        for candidate in record["complete_candidates"]:
            for field in ("full_proposal", "post_polish"):
                snapshot = candidate[field]
                assert snapshot is not None and snapshot["valid"] and snapshot["complete"]
                checked = evaluate_solution(problem, snapshot["routes"], _force_full=True)
                assert checked.valid
                assert checked.score.distance_km == snapshot["score"]["distance_km"]
            assert candidate["post_polish"]["key"] <= candidate["full_proposal"]["key"]
    assert metadata["final_key"] <= metadata["initial_key"]
    json.dumps(metadata, allow_nan=False)


def test_same_depth_beam_keeps_relay_despite_worse_scalar_and_receiver_diversity():
    empty = ((), ())
    states = [beam._State(((i,), ()), (float(i), 0, 0., 0.),
             ((1, recipient, mode, 1 if mode == "RELAY" else None, 1-recipient),))
             for i, (recipient, mode) in enumerate(((0, "DIRECT"), (1, "DIRECT"), (0, "RELAY"), (1, "RELAY")))]
    kept = beam._diverse(states, 4, (0, 1))
    assert len(kept) == 4
    assert {state.modes[0][1] for state in kept} == {0, 1}
    assert {state.modes[0][2] for state in kept} == {"DIRECT", "RELAY"}


def test_weighted_key_can_reverse_physical_lexicographic_order():
    physically_lex_better, scalar_better = Score(0, 0., 3.), Score(1, 0., 0.)
    assert physically_lex_better < scalar_better
    assert beam.weighted_key(scalar_better, (.5, .25, .25), (1., 1., 1.)) < beam.weighted_key(
        physically_lex_better, (.5, .25, .25), (1., 1., 1.))


def test_expired_budget_and_no_possible_four_task_cluster_preserve_incumbent():
    problem, routes = fixture()
    result = beam.refine(problem, routes, deadline=perf_counter() - 1.,
        weights=(.5, .25, .25), scales=(4., 100., 100.))
    assert result["routes"] == routes and result["metadata"]["complete_proposals"] == 0
    one = replace(problem, tasks=problem.tasks[:1], drone_count=1, drone_homes=(Point(0., 0.),),
                  leg_registry={}, task_relay_candidates={})
    result = run(one, ((1, -1),))
    assert result["routes"] == ((1, -1),) and result["iterations"] == 0


def test_seed_reproducibility_and_configuration_validation():
    problem, routes = fixture()
    first, second = run(problem, routes, polish_seconds=0.), run(problem, routes, polish_seconds=0.)
    assert first["routes"] == second["routes"]
    assert first["metadata"]["selected_clusters"] == second["metadata"]["selected_clusters"]
    assert first["metadata"]["first_complete_candidate"] == second["metadata"]["first_complete_candidate"]
    for changes in (dict(beam_width=1), dict(candidate_limit=2), dict(polish_seconds=-1.),
                    dict(proposal_margin=-.1), dict(relay_enabled=1)):
        with pytest.raises(ValueError):
            run(problem, routes, **changes)
    with pytest.raises(ValueError, match="invalid initial"):
        run(problem, ((1, -1), ()))
