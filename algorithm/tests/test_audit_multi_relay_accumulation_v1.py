"""Two disjoint real groups, shared aircraft and full cross-group clocks."""
from copy import deepcopy

import pytest

from algorithm.experiments import audit_multi_relay_accumulation_v1 as audit

W, S = (.5, .25, .25), (8., 100., 100.)


def fixture():
    H = (0., 0.)
    tasks = {1: (H, (1., 1.), 100.), 2: (H, (2., 1.), 100.), 3: ((4., 0.), (1., 0.), 100.),
             4: (H, (2., 0.), 100.), 5: (H, (5., 0.), 100.), 6: (H, H, 100.), 7: (H, H, 100.), 8: (H, H, 100.)}
    legs = [dict(id=100 + 2 * t - 1, task_id=t, kind='RELAY_IN', relay_id=1) for t in range(1, 5)]
    legs += [dict(id=100 + 2 * t, task_id=t, kind='RELAY_OUT', relay_id=1) for t in range(1, 5)]
    topology = dict(drone_count=4, capacity=2, max_tasks_per_drone=2, speed_km_per_min=1.,
        home_points=[[0., 0.]] * 4, stations=[[1, 0., 0.]], legs=legs)
    base = ((3, -3, 4, -4), (5, -5, 6, -6), (1, 2, -1, -2), (7, -7, 8, -8))
    first = ((101, -101, 3, -3, 4, -4), (5, -5, 6, -6, 103, -103), (102, 104, -102, -104), base[3])
    # New receiver0 waits for source3 on aircraft2; aircraft2 also receives old
    # group1 later. The old source1 now has a real upstream OUT dependency.
    second = ((106, 108, -106, -108, 101, -101), first[1],
              (105, -105, 102, 104, -102, -104), (7, -7, 8, -8, 107, -107))
    g1 = dict(group_id=1, i=1, j=2, a=0, b=1, c=2, station_id=1)
    g2 = dict(group_id=2, i=3, j=4, a=2, b=3, c=0, station_id=1)
    return tasks, topology, base, first, second, g1, g2


def key(score):
    values = [score[f] for f in ('late_count', 'total_lateness_min', 'distance_km')]
    return [sum(w * v / s for w, v, s in zip(W, values, S)), *values]


def snapshot(data, routes, groups):
    p = audit.audit_state(*data[:3], routes, groups)
    fields = ('groups', 'group_metrics', 'score', 'post_global_score', 'actual_final_owners', 'event_times_min',
        'delivery_times_min', 'current_source_drop_availability_min', 'current_shared_double_loaded_km_by_group',
        'selected_native_classifications', 'cross_uav_count', 'relay_task_count', 'routes_sha256')
    result = {field: deepcopy(p[field]) for field in fields}
    result.update(routes=list(map(list, routes)), pin_scope=audit.SCOPE, valid=True, complete=True, key=key(p['score']),
                  full_verified_at_perf_counter=1., deadline_perf_counter=2.)
    return result


def test_two_groups_share_aircraft_and_recompute_previous_source_wait_and_delivery():
    data = fixture();p = audit.audit_step(*data[:3], data[3], data[4], [data[5]], data[6])
    old = p['old_group_clock_changes'][0]
    assert old['before']['source_availability_min'] == {'1': 0., '2': 10.}
    assert old['after']['source_availability_min'] == {'1': 12., '2': 10.}
    assert old['before']['receiver_arrival_min'] == 0. and old['after']['receiver_arrival_min'] == 8.
    assert old['before']['receiver_waiting_min'] == 10. and old['after']['receiver_waiting_min'] == 4.
    assert old['after']['second_take_min'] == 12.
    assert p['cross_uav_count'] == 4 and p['final_tasks_per_aircraft'] == [2] * 4
    assert p['group_metrics'][1]['receiver_waiting_min'] == 8.


def test_first_group_step_and_entire_multi_group_snapshot_bind_actual_global_fields():
    data = fixture();saved = snapshot(data, data[4], [data[5], data[6]])
    saved.update(group_added=data[6], parent_routes_sha256=audit._digest(data[3]))
    assert audit.audit_step(*data[:3], data[2], data[3], [], data[5])['passed']
    assert audit.audit_step(*data[:3], data[3], data[4], [data[5]], data[6], telemetry=saved, weights=W, scales=S)['passed']


def test_real_cross_group_handoff_cycle_is_rejected_even_with_closed_local_cargo():
    data = fixture();bad = list(data[4]);bad[2] = (102, 104, -102, -104, 105, -105)
    with pytest.raises(ValueError, match='cycle'):
        audit.audit_state(*data[:3], bad, [data[5], data[6]])


def test_new_IN_in_an_old_doubletake_gap_is_rejected_without_capacity_excuse():
    data = fixture();bad = list(data[4]);bad[0] = (101, -101, 106, 108, -106, -108)
    bad[2] = (102, 105, -105, 104, -102, -104)
    with pytest.raises(ValueError, match='adjacent'):
        audit.audit_step(*data[:3], data[3], bad, [data[5]], data[6])


@pytest.mark.parametrize('fault', ['repeat_task', 'same_source', 'wrong_final_C', 'new_H', 'changed_old_role'])
def test_groups_cannot_reuse_jobs_add_resources_or_change_actual_roles(fault):
    data = fixture();groups = [deepcopy(data[5]), deepcopy(data[6])]
    if fault == 'repeat_task': groups[1]['i'] = 1
    elif fault == 'same_source': groups[1]['b'] = groups[1]['a']
    elif fault == 'wrong_final_C': groups[1]['c'] = 1
    elif fault == 'new_H': groups[1]['station_id'] = 2
    else: groups[0]['a'], groups[0]['b'] = groups[0]['b'], groups[0]['a']
    with pytest.raises(ValueError): audit.audit_state(*data[:3], data[4], groups)


@pytest.mark.parametrize('fault', ['old_wait', 'old_drop', 'native_clock', 'shared', 'overdue', 'raw_price'])
def test_current_clocks_native_delay_and_old_price_or_deadline_cannot_be_forged(fault):
    data = fixture();saved = snapshot(data, data[4], [data[5], data[6]])
    if fault == 'old_wait': saved['group_metrics'][0]['receiver_waiting_min'] = 10.
    elif fault == 'old_drop': saved['current_source_drop_availability_min']['1'] = 0.
    elif fault == 'native_clock': saved['delivery_times_min']['5'] += 1.
    elif fault == 'shared': saved['current_shared_double_loaded_km_by_group']['2'] += 1.
    elif fault == 'overdue': saved['full_verified_at_perf_counter'] = 2.
    else: saved['table_score'] = saved['score']
    with pytest.raises(ValueError): audit.audit_state(*data[:3], data[4], [data[5], data[6]], telemetry=saved, weights=W, scales=S)


def test_native_owner_change_outside_new_pair_cannot_hide_behind_equal_quota():
    data = fixture();bad = tuple(tuple(7 if v == 5 else -7 if v == -5 else 5 if v == 7 else -5 if v == -7 else v for v in r) for r in data[4])
    with pytest.raises(ValueError, match='original final owner'):
        audit.audit_step(*data[:3], data[3], bad, [data[5]], data[6])


def test_independent_geometry_is_not_revalidated_per_service_event(monkeypatch):
    data = fixture();calls = [0];actual = audit._geometry
    def counted(*args):
        calls[0] += 1;return actual(*args)
    monkeypatch.setattr(audit, '_geometry', counted)
    audit.audit_step(*data[:3], data[3], data[4], [data[5]], data[6])
    assert calls[0] == 1  # Frozen reference_schedule has its own single entry validation.


def real_metadata():
    from time import perf_counter
    from algorithm.experiments.multi_relay_accumulation_v1 import refine
    from algorithm.tests.test_multi_relay_accumulation_v1 import fixture as model_fixture
    from algorithm.tests.test_relay_repooling_exchange import topology
    p, base, *_ = model_fixture();deadline = perf_counter() + 4.
    result = refine(p, base, deadline=deadline, weights=W, scales=S, max_groups=2, beam_width=2,
        max_families=2, source_option_limit=1, receiver_option_limit=1, position_limit=2, snapshot_limit=1)
    tasks = {t.id: ((t.pickup.x, t.pickup.y), (t.delivery.x, t.delivery.y), t.deadline_min) for t in p.tasks}
    return tasks, topology(p), base, result['metadata']


def prove_metadata(data):
    return audit.audit_metadata(*data[:3], data[3], weights=W, scales=S, deadline=data[3]['deadline_perf_counter'])


def test_real_current_state_catalogue_complete_beam_and_strict_original_guard_replay():
    data = real_metadata();p = prove_metadata(data)
    assert p['saved_complete_states_replayed'] >= 2 and p['complete_transition_edges_replayed'] >= 1
    assert p['catalogue_references'] and not p['all_probes_entity_replayed']
    assert p['returned_routes_sha256'] == data[3]['post_repair_snapshot']['routes_sha256']
    for step in data[3]['steps']:
        for parent in step['parent_records']:
            catalogue = parent['catalogue']
            if catalogue is None: continue
            assert catalogue['candidate_representation'] == 'parent_plus_layouts_and_routes_sha256'
            assert catalogue['complete_candidates_contain_routes'] is False
            assert all('routes' not in row for row in catalogue['complete_candidates'])
    assert all(ref['reconstructed_route_digests_bound'] for ref in p['catalogue_references'])
    assert not p['all_catalogue_proposals_claimed_globally_feasible']


@pytest.mark.parametrize('fault', ['source_estimate', 'family_proxy', 'best_guard', 'signature'])
def test_false_current_candidate_proxy_old_signature_or_guard_chain_is_rejected(fault):
    data = real_metadata();m = data[3]
    if fault == 'best_guard': m['best_key'][0] -= 1.
    else:
        c = m['steps'][0]['parent_records'][0]['catalogue']
        if fault == 'source_estimate': c['complete_candidates'][0]['source_layouts'][0]['estimated_drop_min'] += 1.
        elif fault == 'family_proxy': c['families'][0]['geometry_proxy_delta_km'] += 1.
        else: c['parent_signature']['final_counts'][0] += 1
    with pytest.raises(ValueError): prove_metadata(data)


@pytest.mark.parametrize('fault', ['route_digest', 'actual_gap', 'alternate_fleet', 'representation'])
def test_compact_candidates_require_actual_parent_layout_and_exact_reconstructed_identity(fault):
    data = real_metadata();c = data[3]['steps'][0]['parent_records'][0]['catalogue']
    candidate = c['complete_candidates'][0]
    if fault == 'route_digest': candidate['routes_sha256'] = '0' * 64
    elif fault == 'actual_gap': candidate['source_layouts'][0]['end_gap'] += 1
    elif fault == 'alternate_fleet': candidate['routes'] = list(map(list, data[2]))
    else: c['complete_candidates_contain_routes'] = True
    with pytest.raises(ValueError): prove_metadata(data)
