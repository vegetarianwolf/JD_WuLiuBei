"""Quota-neutral two-source transport and a genuinely independent DIRECT arm."""
from dataclasses import replace
from time import perf_counter
import json

import pytest
from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import split_feeder_refinement as method
from algorithm.tests.test_relay_repooling_exchange import topology


W, S = (.5, .25, .25), (9., 100., 100.)


def fixture():
    left, right, H = Point(-4., 0.), Point(4., 0.), Point(0., 0.)
    tasks = [Task(1, left, Point(0., 3.), 9.), Task(2, right, Point(0., 4.), 9.)]
    tasks += [Task(t, left, left, 100.) for t in (3, 4, 5)]
    tasks += [Task(t, right, right, 100.) for t in (6, 7, 8)]
    tasks += [Task(9, H, H, 100.)]
    registry = {101: TransportLeg(101, 1, 'RELAY_IN', 1), 102: TransportLeg(102, 1, 'RELAY_OUT', 1),
        103: TransportLeg(103, 2, 'RELAY_IN', 1), 104: TransportLeg(104, 2, 'RELAY_OUT', 1)}
    p = Problem(tuple(tasks), drone_count=3, capacity=2, max_tasks_per_drone=3,
        speed_km_per_min=.9, drone_homes=(left, right, H), relay_stations=(RelayStation(1, 0., 0.),),
        leg_registry=registry, task_relay_candidates={1: (1,), 2: (1,)})
    base = ((3, -3, 4, -4, 5, -5), (6, -6, 7, -7, 8, -8), (9, -9, 1, 2, -1, -2))
    assert evaluate_solution(p, base, _force_full=True).valid
    return p, base


def run(p, base, **kwargs):
    return method.refine(p, base, deadline=perf_counter()+4., weights=W, scales=S,
        max_families=2, base_pool_limit=8, **kwargs)


def audit(p, base, result, relay_enabled):
    from algorithm.experiments.audit_split_feeder import audit_split_feeder_proposal, audit_split_feeder_family
    tasks = {t.id: ((t.pickup.x, t.pickup.y), (t.delivery.x, t.delivery.y), t.deadline_min) for t in p.tasks}
    graph = topology(p)
    graph['task_relay_candidates'] = {str(t): list(p.task_relay_candidates.get(t, ())) for t in p.task_ids}
    snapshots = result['metadata']['full_candidate_snapshots']
    assert snapshots
    for family in result['metadata'].get('selected_families', []):
        proof = audit_split_feeder_family(tasks, graph, base, family, weights=W, scales=S)
        assert proof['passed']
    for snapshot in snapshots:
        proof = audit_split_feeder_proposal(tasks, graph, base, snapshot['routes'], snapshot['move'],
            relay_enabled=relay_enabled, telemetry=snapshot, weights=W, scales=S)
        assert proof['passed']


def test_original_final_owner_kept_with_two_different_full_quota_feeders_and_real_rescue():
    p, base = fixture(); before = topology(p)
    result = run(p, base); m = result['metadata']; index = build_plan_index(p, result['routes'])
    assert result['evaluation'].valid and result['evaluation'].score.late_count == 0
    assert result['evaluation'].score.distance_km == 12.
    assert m['final_cross_uav_count'] == 2 and set(index.relay_task_ids) == {1, 2}
    assert index.delivery_route_by_task == build_plan_index(p, base).delivery_route_by_task
    assert {index.inbound_route_by_task[t] for t in (1, 2)} == {0, 1}
    assert all(sum(a == owner for owner in index.delivery_route_by_task.values()) == p.max_tasks_per_drone for a in range(3))
    assert m['accepted_moves'] and m['accepted_moves'][-1]['full_snapshot']['pooled_rescued_task_ids'] == [1, 2]
    for accepted in m['accepted_moves']:
        assert accepted['full_verified_at_perf_counter'] <= accepted['accepted_at_perf_counter'] < m['deadline_perf_counter']
    assert topology(p) == before and 'problem' not in result
    audit(p, base, result, True)
    json.dumps(m, allow_nan=False)


def test_direct_all_twelve_independent_donor_maps_and_shared_catalogue_are_real():
    p, base = fixture(); direct = run(p, base, relay_enabled=False); relay = run(p, base)
    dm, rm = direct['metadata'], relay['metadata']
    assert dm['selector_completed'] and rm['selector_completed']
    assert dm['selected_families'] == rm['selected_families'] and dm['selector_sha256'] == rm['selector_sha256']
    assert dm['configuration'] == rm['configuration']
    for record in dm['family_records']:
        maps = record['direct_maps_completed']
        assert record['direct_same_owner_pair_completed'] and record['direct_map_enumeration_completed']
        assert len(maps) == len({tuple(tuple(group) for group in mapping) for mapping in maps}) == 12
        assert record['direct_maps_considered'] == maps
    assert direct['evaluation'].valid and not build_plan_index(p, direct['routes']).relay_task_ids
    assert dm['final_key'] <= dm['initial_key']
    assert any(snapshot['move']['mode'] == 'DIRECT_DONOR_12MAP' for snapshot in dm['full_candidate_snapshots'])
    assert rm['final_key'] < dm['final_key']  # This finite toy is a mechanism witness, not an official result.
    audit(p, base, direct, False)


def test_source_original_load_one_and_receiver_wait_include_every_native_delivery():
    p, base = fixture()
    p = replace(p, tasks=tuple(replace(t, deadline_min=0.) if t.id == 3 else
        replace(t, delivery=Point(8., 0.)) if t.id == 6 else t for t in p.tasks))
    result = run(p, base)
    family = next(f for f in result['metadata']['selected_families'] if (f['a'], f['b']) == (0, 1))
    routes = ((3, 101, -101, -3, 4, -4, 5, -5),
        (6, -6, 7, -7, 8, -8, 103, -103), (9, -9, 102, 104, -102, -104))
    evaluator = GlobalRelayEvaluator(p); schedule = evaluator.evaluate(routes)
    availability = {1: schedule.event_times_min[(0, 2)], 2: schedule.event_times_min[(1, 7)]}
    records = [method.costs.route_cost(p, r, a, availability=availability, deadline=perf_counter()+2.) for a, r in enumerate(base)]
    # Rebuild from the actual complete routes; no incomplete route clocks or free origin.
    replacements = {a: method.costs.route_cost(p, r, a, availability=availability, deadline=perf_counter()+2.) for a, r in enumerate(routes)}
    candidate = method._pack(p, records, family, replacements, 'RELAY')
    checked = evaluate_solution(p, routes, _force_full=True)
    assert checked.valid and schedule.total_waiting_min > 0
    assert replacements[0]['load_before'][1] == 1 and replacements[0]['load_after'][1] == 2
    method._verify(p, base, routes, candidate['move'], True)
    method._table_matches(p, candidate, checked, schedule)
    snapshot = method._snapshot(p, routes, checked, schedule, W, S, phase='full_proposal',
        move=candidate['move'], table_score=candidate['score'], original=evaluator.evaluate(base).delivery_times_min)
    assert snapshot['handoff']['receiver_waiting_min'] == pytest.approx(12./.9)
    assert snapshot['handoff']['ground_cache_by_task_min'] == {'1': 0., '2': 0.}
    assert snapshot['handoff']['take_times_min'][1]-snapshot['handoff']['take_times_min'][0] == pytest.approx(8./.9)
    assert snapshot['delivery_times_min']['3'] == pytest.approx(8./.9)
    audit(p, base, {'metadata': {'full_candidate_snapshots': [snapshot]}}, True)


def test_actual_shared_direct_uses_task_identifiers_and_excludes_native_travel():
    p, base = fixture()
    routes = (base[0], base[1], (9, -9, 1, 2, -1, -2))
    owners = build_plan_index(p, routes).delivery_route_by_task
    assert method._shared(p, routes, (1, 2), owners) == 5.
    assert method.costs.route_cost(p, routes[2], 2, deadline=perf_counter()+2.)['score'].distance_km == 18.


def test_family_must_match_true_original_owner_not_a_synthetic_old_base():
    p, base = fixture(); result = run(p, base); snapshot = result['metadata']['full_candidate_snapshots'][0]
    forged = ((1, -1, 4, -4, 5, -5), base[1], (9, -9, 3, 2, -3, -2))
    assert evaluate_solution(p, forged, _force_full=True).valid
    with pytest.raises(ValueError, match='actual original owners'):
        method._verify(p, forged, tuple(tuple(r) for r in snapshot['routes']), snapshot['move'], True)


def test_snapshot_crossing_deadline_cannot_replace_complete_original_guard(monkeypatch):
    p, base = fixture(); now = perf_counter(); clock = [now]; deadline = now+4.; original = method._snapshot
    monkeypatch.setattr(method, 'perf_counter', lambda: clock[0])
    def crossing(*args, **kwargs):
        value = original(*args, **kwargs)
        if kwargs['phase'] == 'full_proposal':
            clock[0] = deadline+.1
        return value
    monkeypatch.setattr(method, '_snapshot', crossing)
    result = method.refine(p, base, deadline=deadline, weights=W, scales=S, max_families=1, base_pool_limit=8)
    assert result['routes'] == base and result['evaluation'].valid
    assert result['metadata']['deadline_reached'] and not result['metadata']['accepted_moves']
    assert not result['metadata']['full_candidate_snapshots']


def test_expired_input_returns_original_without_a_catalogue_or_new_checkpoint():
    p, base = fixture()
    result = method.refine(p, base, deadline=perf_counter()-1., weights=W, scales=S)
    assert result['routes'] == base and result['evaluation'].valid
    assert result['metadata']['deadline_reached'] and not result['metadata']['selector_completed']
    assert not result['metadata']['accepted_moves']


@pytest.mark.parametrize('change', [dict(source_option_limit=0), dict(feeder_limit=0), dict(full_candidates_per_family=4)])
def test_invalid_finite_bounds(change):
    p, base = fixture()
    with pytest.raises(ValueError):
        run(p, base, **change)
