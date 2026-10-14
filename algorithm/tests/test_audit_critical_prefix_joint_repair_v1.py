"""Small real DAG proofs; no official instance or optimization is executed."""
from copy import deepcopy
from time import perf_counter

import pytest

from algorithm.experiments import audit_critical_prefix_joint_repair_v1 as audit
from algorithm.experiments.audit_weighted_relay_ablation import reference_schedule, _geometry
from algorithm.experiments.audit_repooling import _owners

W = (.5, .25, .25)
S = (9., 100., 100.)


def fixture(eight=False):
    tasks = {1: ((0., 0.), (10., 0.), 100.), 2: ((0., 0.), (11., 0.), 100.),
        3: ((0., 0.), (4., 0.), 100.), 4: ((0., 0.), (0., 0.), 100.),
        5: ((0., 0.), (0., 0.), 100.), 6: ((0., 0.), (3., 0.), 100.),
        7: ((0., 0.), (0., 0.), 100.), 8: ((0., 0.), (0., 0.), 100.),
        9: ((0., 0.), (2., 0.), 100.)}
    legs = [dict(id=101, task_id=1, kind='RELAY_IN', relay_id=1), dict(id=102, task_id=1, kind='RELAY_OUT', relay_id=1),
            dict(id=103, task_id=2, kind='RELAY_IN', relay_id=1), dict(id=104, task_id=2, kind='RELAY_OUT', relay_id=1)]
    topology = dict(drone_count=3, capacity=2, max_tasks_per_drone=3, speed_km_per_min=1.,
        home_points=[[0., 0.]] * 3, stations=[[1, 0., 0.]], legs=legs)
    base = ((3, -3, 4, -4, 5, -5), (6, -6, 7, -7, 8, -8), (1, -1, 2, -2, 9, -9))
    raw = (base[0] + (101, -101), base[1] + (103, -103), (9, -9, 102, 104, -102, -104))
    if eight:
        for t in range(10, 15): tasks[t] = ((0., 0.), (float(t), 0.), 100.)
        base += tuple((t, -t) for t in range(10, 15));raw += tuple((t, -t) for t in range(10, 15))
        topology.update(drone_count=8, home_points=[[0., 0.]] * 8)
    family = dict(i=1, j=2, a=0, b=1, c=2, k1=3, k2=6, station_id=1, task_ids=[1, 2, 3, 6], pooled_task_ids=[1, 2])
    owners = _owners(raw, _geometry(tasks, topology)[2])
    move = dict(family=family, mode='RELAY', selected_task_ids=[1, 2],
        final_aircraft_by_task={str(t): owners[t] for t in (1, 2)}, actual_final_owners={str(t): owners[t] for t in sorted(tasks)})
    return tasks, topology, base, raw, move


def problem(data):
    from uav_dispatch import Point, Task, Problem, RelayStation, TransportLeg
    tasks, topology = data[:2]
    return Problem(tuple(Task(t, Point(*x[0]), Point(*x[1]), x[2]) for t, x in tasks.items()),
        drone_count=topology['drone_count'], max_tasks_per_drone=3, capacity=2, speed_km_per_min=1.,
        drone_homes=tuple(Point(*p) for p in topology['home_points']), relay_stations=(RelayStation(1, 0., 0.),),
        leg_registry={r['id']: TransportLeg(r['id'], r['task_id'], r['kind'], r['relay_id']) for r in topology['legs']},
        task_relay_candidates={1: (1,), 2: (1,)})


def closure(data=None, *, relay=True, cap=6):
    from algorithm.experiments.critical_prefix_closure_v1 import build_closure
    data = fixture() if data is None else data
    return build_closure(problem(data), data[2], data[3] if relay else data[2], pinned_task_ids=(1, 2) if relay else (),
        focus_task_ids=(1, 2), closure_limit=cap, deadline=perf_counter() + 3.)


def prove_closure(data, saved, relay=True):
    return audit.audit_closure(*data[:3], data[3] if relay else data[2], saved, pinned_task_ids=(1, 2) if relay else (),
        relay_enabled=relay, seed_move=data[4] if relay else None)


def key(score):
    values = [score[f] for f in ('late_count', 'total_lateness_min', 'distance_km')]
    return [sum(w * v / s for w, v, s in zip(W, values, S)), *values]


def post(data, routes, relay=True, seed_move='default'):
    move = data[4] if seed_move == 'default' else seed_move
    raw = data[3] if relay else data[2]
    p = audit.audit_repair(*data[:3], raw, routes, move, relay_enabled=relay, focus_task_ids=(1, 2))
    fields = ('pinned_task_ids', 'focus_task_ids', 'actual_final_owners', 'event_times_min', 'delivery_times_min',
        'post_global_score', 'current_source_drop_availability_min', 'current_shared_loaded_distance_km',
        'current_handoff', 'current_classifications', 'changed_owner_task_ids', 'changed_event_sequence_aircraft')
    saved = dict(routes=list(map(list, routes)), valid=True, complete=True, pin_scope=audit.PIN_SCOPE,
        original_seed_move=move, score=p['score'], key=key(p['score']), full_verified_at_perf_counter=1., deadline_perf_counter=2.)
    saved.update({f: deepcopy(p[f]) for f in fields})
    return p, saved


def test_zero_damage_and_masked_source_are_real_separate_causal_directions():
    data = fixture();saved = closure(data, cap=3);p = prove_closure(data, saved)
    assert {3, 6} <= set(p['zero_delivery_damage_causal_task_ids'])
    rows = p['per_task_evidence']
    assert rows[3]['signed_improvement_min']['in_drop:1'] == 8.
    assert rows[6]['signed_improvement_min']['in_drop:2'] == 6.
    assert rows[6]['signed_improvement_min']['delivery:2'] == 0.
    assert p['quota_slots_by_aircraft'] == [1, 1, 1]


def test_deleted_DIRECT_target_is_undefined_not_zero_or_rescued():
    data = fixture();saved = closure(data, relay=False);p = prove_closure(data, saved, False)
    assert p['per_task_evidence'][1]['target_clock_after_min']['delivery:1'] is None
    saved['per_task_evidence'][0]['target_clock_after_min']['delivery:1'] = 0.
    with pytest.raises(ValueError): prove_closure(data, saved, False)


@pytest.mark.parametrize('field', ['signed_improvement_min', 'causal_prefix_task_ids', 'quota_slots_by_aircraft'])
def test_fake_prefix_effect_or_slots_cannot_be_self_certified(field):
    data = fixture();saved = closure(data)
    if field == 'signed_improvement_min': saved['per_task_evidence'][0][field]['in_drop:1'] = 0.
    elif field == 'causal_prefix_task_ids': saved['targets'][0][field] = []
    else: saved[field][0] += 1
    with pytest.raises(ValueError): prove_closure(data, saved)


def test_all_eight_donors_real_and_small_cap_does_not_claim_all_open():
    data = fixture(eight=True)
    saved = closure(data, cap=8);p = prove_closure(data, saved)
    assert p['actual_opened_aircraft'] == list(range(8)) and p['quota_slots_by_aircraft'] == [1] * 8
    tiny = closure(data, cap=2);assert not tiny['all_aircraft_opened'];prove_closure(data, tiny)
    tiny['all_aircraft_opened'] = True
    with pytest.raises(ValueError): prove_closure(data, tiny)


def test_RELAY_only_pins_two_plans_native_owners_and_order_may_change():
    data = fixture()
    # Tasks3/6 change aircraft while native ordering changes around the pins.
    routes = ((4, -4, 5, -5, 6, -6, 101, -101), (3, -3, 7, -7, 8, -8, 103, -103), data[3][2])
    p, saved = post(data, routes)
    assert p['pinned_task_ids'] == [1, 2] and p['changed_owner_task_ids'] == [3, 6]
    audit.audit_repair(*data[:3], data[3], routes, data[4], relay_enabled=True, telemetry=saved, weights=W, scales=S, focus_task_ids=(1, 2))


def test_DIRECT_zero_pins_releases_even_original_pooled_owner_and_sequence():
    data = fixture();routes = ((1, -1, 4, -4, 5, -5), data[2][1], (3, -3, 2, -2, 9, -9))
    p, saved = post(data, routes, False, None)
    assert p['pinned_task_ids'] == [] and p['changed_owner_task_ids'] == [1, 3]
    assert p['actual_final_owners']['1'] == 0
    audit.audit_repair(*data[:3], data[2], routes, None, relay_enabled=False, telemetry=saved, focus_task_ids=(1, 2), weights=W, scales=S)


@pytest.mark.parametrize('field', ['event_times_min', 'current_handoff', 'pinned_task_ids', 'table_score', 'full_verified_at_perf_counter'])
def test_current_full_clock_identity_historical_price_and_deadline_tampering_rejected(field):
    data = fixture();_, saved = post(data, data[3])
    if field == 'event_times_min': saved[field][0][0] += 1.
    elif field == 'current_handoff': saved[field]['second_take_min'] += 1.
    elif field == 'pinned_task_ids': saved[field] = []
    elif field == 'table_score': saved[field] = saved['score']
    else: saved[field] = 2.
    with pytest.raises(ValueError):
        audit.audit_repair(*data[:3], data[3], data[3], data[4], relay_enabled=True, telemetry=saved, focus_task_ids=(1, 2))


def test_joint_two_task_transition_scores_real_current_DAG_and_consumes_dynamic_owner_slots():
    data = fixture();tasks, topology, _, raw, move = data
    parent = (tuple(v for v in raw[0] if abs(v) != 3), tuple(v for v in raw[1] if abs(v) != 6), raw[2])
    child = (parent[0] + (6, -6), parent[1] + (3, -3), raw[2])
    p = audit.audit_partial_state(tasks, topology, child, sorted(tasks), target_final_counts=[3] * 3,
        raw_routes=raw, pinned_task_ids=(1, 2), relay_enabled=True, seed_move=move,
        parent_routes=parent, parent_present_task_ids=sorted(set(tasks) - {3, 6}), inserted_task_order=[3, 6],
        owner_choices=[1, 0], insertions=[dict(task_id=3, owner=1, pickup_gap=6, delivery_gap=6), dict(task_id=6, owner=0, pickup_gap=6, delivery_gap=6)])
    assert p['transition']['owner_choices'] == [1, 0] and p['remaining_quota_slots'] == [0] * 3
    assert p['complete'] is False  # Even a full-coverage layer is not a full-validator certificate.


def test_partial_coverage_quota_forged_complete_and_partial_clock_rejected():
    data = fixture();tasks, topology, _, raw, move = data
    routes = (raw[0][2:], *raw[1:]);present = sorted(set(tasks) - {3})
    p = audit.audit_partial_state(tasks, topology, routes, present, target_final_counts=[3] * 3,
        raw_routes=raw, pinned_task_ids=(1, 2), relay_enabled=True, seed_move=move)
    saved = {f: deepcopy(p[f]) for f in ('routes', 'score', 'event_times_min', 'delivery_times_min', 'present_task_ids', 'remaining_task_ids', 'remaining_quota_slots', 'complete', 'scoring_scope')}
    saved['key'] = key(saved['score'])
    kwargs = dict(target_final_counts=[3] * 3, raw_routes=raw, pinned_task_ids=(1, 2), relay_enabled=True, seed_move=move, weights=W, scales=S)
    audit.audit_partial_state(tasks, topology, routes, present, telemetry=saved, **kwargs)
    for field, value in [('complete', True), ('remaining_quota_slots', [0, 1, 0]), ('event_times_min', [[0.], [], []])]:
        bad = deepcopy(saved);bad[field] = value
        with pytest.raises(ValueError): audit.audit_partial_state(tasks, topology, routes, present, telemetry=bad, **kwargs)


def test_actual_cycle_capacity_wrong_pin_and_incomplete_leaf_rejected():
    data = fixture();tasks, topology, base, raw, move = data
    cycle = ((102, -102, 103, -103), (104, -104, 101, -101), (3, -3, 4, -4, 5, -5, 6, -6, 7, -7, 8, -8, 9, -9))
    cycle_topology = {**topology, 'max_tasks_per_drone': 9}
    with pytest.raises(ValueError, match='cycle'): reference_schedule(tasks, cycle, cycle_topology)
    with pytest.raises(ValueError): audit.audit_repair(*data[:3], raw, (raw[0][2:], *raw[1:]), move, relay_enabled=True)
    changed = (raw[1], raw[0], raw[2])
    with pytest.raises(ValueError, match='pinned'): audit.audit_repair(*data[:3], raw, changed, move, relay_enabled=True)
    overloaded = ((3, 4, 5, -3, -4, -5, 101, -101), *raw[1:])
    with pytest.raises(ValueError, match='capacity'): audit.audit_repair(*data[:3], raw, overloaded, move, relay_enabled=True)


def test_worse_complete_leaf_cannot_claim_original_strong_adoption():
    data = fixture();_, saved = post(data, data[2], False, None);saved['accepted'] = True
    with pytest.raises(ValueError, match='strictly improve'):
        audit.audit_repair(*data[:3], data[2], data[2], None, relay_enabled=False, telemetry=saved, weights=W, scales=S, focus_task_ids=(1, 2))


def test_generic_wrapper_guard_has_no_fake_seed_and_requires_true_clocks():
    data = fixture();p = audit.audit_complete_state(*data[:2], data[2], target_final_counts=[3] * 3, relay_enabled=False)
    saved = {f: deepcopy(p[f]) for f in ('score', 'event_times_min', 'delivery_times_min', 'actual_final_owners',
        'cross_uav_count', 'relay_task_count', 'source_drop_availability_min', 'shared_loaded_distance_km', 'handoff')}
    saved.update(routes=list(map(list, data[2])), valid=True, complete=True, key=key(p['score']), focus_task_ids=[])
    audit.audit_complete_state(*data[:2], data[2], telemetry=saved, weights=W, scales=S)
    with pytest.raises(ValueError): audit.audit_complete_state(*data[:2], data[2], telemetry=saved, require_predeadline=True)
