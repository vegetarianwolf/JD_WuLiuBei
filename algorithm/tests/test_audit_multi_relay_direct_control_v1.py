"""Saved-entity DIRECT proof; no official instance or performance experiment."""
from copy import deepcopy
from time import perf_counter

import pytest

from algorithm.experiments import audit_multi_relay_direct_control_v1 as audit
from algorithm.experiments import conditional_donor_repair_v1 as repair
from algorithm.tests.test_audit_critical_prefix_joint_repair_v1 import fixture, problem, W, S
from algorithm.tests.test_audit_multi_relay_batch_v1 import archived_pair


def _snapshot(tasks, graph, routes, phase, *, focus=(), seed_routes=None, verified=None, deadline=None):
    proof = audit.physical.audit_complete_state(tasks, graph, routes, relay_enabled=False)
    fields = ('score', 'event_times_min', 'delivery_times_min', 'actual_final_owners', 'cross_uav_count',
              'relay_task_count', 'source_drop_availability_min', 'shared_loaded_distance_km', 'handoff')
    saved = {k: deepcopy(proof[k]) for k in fields}
    saved.update(routes=[list(r) for r in routes], key=audit._key(proof['score'], W, S), valid=True, complete=True,
        phase=phase, focus_task_ids=list(focus), seed_routes_sha256=None if seed_routes is None else audit._digest(seed_routes))
    if verified is not None: saved.update(full_verified_at_perf_counter=verified, deadline_perf_counter=deadline)
    return saved


@pytest.fixture(scope='module')
def saved_control():
    data = fixture();tasks, graph, base = data[:3]
    # A physically better all-DIRECT ALNS return, independently replayed.
    # This synthetic saved record tests provenance, not an ALNS performance claim.
    alns_routes = (base[0], base[1], (1, 2, -1, -2, 9, -9))
    now = perf_counter();started = now - .5;deadline = now + 3.15;configured = started + .01
    ref = audit.physical.audit_complete_state(tasks, graph, alns_routes, relay_enabled=False)
    focus = sorted(sorted(tasks, key=lambda t: (-(ref['delivery_times_min'][str(t)] - tasks[t][2]), t))[:2])
    bounds = dict(critical_limit=2, donor_limit=1, position_limit=2, option_count=1, beam_width=1, max_steps=1, snapshot_limit=2)
    outcome = repair.refine(problem(data), alns_routes, alns_routes, deadline=deadline - .15, weights=W, scales=S,
        seed=17, relay_enabled=False, focus_task_ids=focus, **bounds)
    returned = perf_counter()
    initial = _snapshot(tasks, graph, base, 'original_guard')
    snapshots = [_snapshot(tasks, graph, alns_routes, audit.ALNS_PHASE, verified=returned + .001, deadline=deadline),
        _snapshot(tasks, graph, outcome['routes'], audit.REPAIR_PHASE, focus=focus, seed_routes=alns_routes,
                  verified=returned + .002, deadline=deadline)]
    meta = dict(method=audit.METHOD, seed=17, relay_enabled=False, started_at_perf_counter=started,
        deadline_perf_counter=deadline, deadline_reached=False, input_snapshot=initial, initial_key=initial['key'],
        configuration=dict(alns_fraction=.65, no_relay_catalogue=True, pins_empty=True, all_tasks_native_free=True,
            final_guard='original_complete_input_strict_weighted', strongest_fresh_end_to_end_comparison=False,
            wrapper_verification_reserve_seconds=.15, repair_parameters=bounds),
        alns_record=dict(deadline_perf_counter=started + (deadline - started) * .65,
            configured_at_perf_counter=configured, started_at_perf_counter=configured + .001,
            returned_at_perf_counter=now - .1, routes=[list(r) for r in alns_routes], iterations=1,
            config=dict(max_iterations=10_000_000, seed=17, time_limit_seconds=max(.001, started + (deadline-started)*.65-configured),
                relay_enabled=False, enable_home_seed=True, enable_home_bias=True, enable_terminal_task_exchange=True, enable_lazy_repair=True),
            periodic_configuration=dict(audit.PERIODIC), objective_mode='frozen_weighted_ALNS', effective_coefficients=[w/s for w,s in zip(W,S)],
            periodic_task_exchange=dict(repair_calls=1, passes=0, probes=0, canonical_checks=0, improvements=0, runtime_seconds=0., events=[])),
        repair_record=dict(base_routes=[list(r) for r in alns_routes], base_routes_sha256=audit._digest(alns_routes),
            focus_task_ids=focus, deadline_perf_counter=deadline-.15, returned=True, metadata=outcome['metadata'],
            returned_routes=[list(r) for r in outcome['routes']], returned_at_perf_counter=returned),
        full_candidate_snapshots=snapshots, accepted_moves=[], runtime_seconds=returned-started)
    best = initial
    for saved in snapshots:
        if tuple(saved['key']) < tuple(best['key']):
            meta['accepted_moves'].append(dict(source=saved['phase'], full_snapshot=saved,
                full_verified_at_perf_counter=saved['full_verified_at_perf_counter'], accepted_at_perf_counter=returned+.003))
            best = saved
    meta.update(accepted_improvements=len(meta['accepted_moves']), final_snapshot=best, final_key=best['key'])
    return tasks, graph, base, meta


def prove(case, meta=None, parameters=None):
    tasks, graph, base, saved = case
    saved = saved if meta is None else meta
    return audit.audit_metadata(tasks, graph, base, saved, weights=W, scales=S,
        deadline=saved['deadline_perf_counter'], parameters=parameters)


def test_actual_helper_all_native_free_provenance_clocks_and_complete_adoption(saved_control):
    result = prove(saved_control)
    assert result['passed'] and result['wrapper_candidates_replayed'] == 2
    assert result['repair_metadata_reference']['initialization_completed']
    assert result['accepted_improvements'] >= 1
    assert result['final_reference']['final_tasks_per_aircraft'] == [3, 3, 3]
    assert 'core_reference' not in result['final_reference']
    assert not result['all_discarded_ALNS_or_exchange_entities_replayed']


def test_actual_frozen_weighted_archive_result_crosses_independent_public_audit(archived_pair):
    data = archived_pair
    answers = [v for v in data['answers'].values() if v['metadata'].get('relay_enabled') is False]
    assert len(answers) == 1
    for answer in answers:
        meta = answer['metadata']
        proof = audit.audit_metadata(data['tasks'], data['graph'], data['base'], meta,
            weights=W, scales=S, deadline=meta['deadline_perf_counter'])
        assert proof['passed'] and proof['final_routes_sha256'] == audit._digest(answer['routes'])
        assert proof['actual_ALNS_reference'] is not None
        assert proof['final_reference']['final_tasks_per_aircraft'] == [3, 3, 3]


@pytest.mark.parametrize('tamper', ['ALNS_clock', 'owner', 'repair_base', 'focus', 'config_seed', 'config_time',
    'configured_missing', 'subdeadline', 'coefficients', 'periodic', 'objective', 'relay_flag', 'repair_parameters',
    'helper_seed', 'helper_old_base', 'candidate_source', 'late_verified', 'late_adopt', 'false_adopt', 'false_final'])
def test_forged_physics_provenance_budget_or_adoption_is_rejected(saved_control, tamper):
    meta = deepcopy(saved_control[3]);a=meta['alns_record'];r=meta['repair_record'];s=meta['full_candidate_snapshots']
    if tamper == 'ALNS_clock': s[0]['event_times_min'][2][0] += 1.
    elif tamper == 'owner': s[0]['actual_final_owners']['1'] = 0
    elif tamper == 'repair_base': r['base_routes'] = deepcopy(meta['input_snapshot']['routes'])
    elif tamper == 'focus': r['focus_task_ids'] = [8, 9]
    elif tamper == 'config_seed': a['config']['seed'] += 1
    elif tamper == 'config_time': a['config']['time_limit_seconds'] += 1.
    elif tamper == 'configured_missing': a.pop('configured_at_perf_counter')
    elif tamper == 'subdeadline': a['deadline_perf_counter'] += 1.
    elif tamper == 'coefficients': a['effective_coefficients'][0] += .1
    elif tamper == 'periodic': a['periodic_configuration']['interval'] = 1
    elif tamper == 'objective': a['objective_mode'] = 'lexicographic'
    elif tamper == 'relay_flag': a['config']['relay_enabled'] = True
    elif tamper == 'repair_parameters': meta['configuration']['repair_parameters']['max_steps'] += 1
    elif tamper == 'helper_seed': r['metadata']['seed'] += 1
    elif tamper == 'helper_old_base': r['metadata']['input_snapshot']['routes'] = deepcopy(meta['input_snapshot']['routes'])
    elif tamper == 'candidate_source': s[1]['seed_routes_sha256'] = '0' * 64
    elif tamper == 'late_verified': s[0]['full_verified_at_perf_counter'] = meta['deadline_perf_counter']
    elif tamper == 'late_adopt': meta['accepted_moves'][0]['accepted_at_perf_counter'] = meta['deadline_perf_counter']
    elif tamper == 'false_adopt': meta['accepted_moves'] = [];meta['accepted_improvements'] = 0
    elif tamper == 'false_final': meta['final_snapshot'] = deepcopy(meta['input_snapshot'])
    with pytest.raises(ValueError): prove(saved_control, meta)


def test_protocol_parameters_bind_actual_own_budget_and_unknown_parameters_fail(saved_control):
    assert prove(saved_control, parameters=dict(alns_fraction=.65, critical_limit=2, seed=17))['passed']
    for parameters in (dict(alns_fraction=.5), dict(donor_limit=3), dict(raw_fraction=.3)):
        with pytest.raises(ValueError): prove(saved_control, parameters=parameters)


def test_entry_expired_guard_needs_no_predeadline_input_certificate(saved_control):
    meta = deepcopy(saved_control[3]);meta['deadline_perf_counter'] = meta['started_at_perf_counter'] - 1.
    meta.update(alns_record=None, repair_record=None, full_candidate_snapshots=[], accepted_moves=[], accepted_improvements=0,
        deadline_reached=True, final_snapshot=deepcopy(meta['input_snapshot']), final_key=meta['input_snapshot']['key'])
    result = prove(saved_control, meta)
    assert result['wrapper_candidates_replayed'] == 0 and result['actual_ALNS_reference'] is None


def test_expired_helper_no_checkpoint_is_honest_and_cannot_introduce_consideration(saved_control):
    tasks, graph, base, source = saved_control;meta = deepcopy(source);r = meta['repair_record'];alns = meta['alns_record']['routes']
    data = fixture();out = repair.refine(problem(data), alns, alns, deadline=perf_counter()-1., weights=W, scales=S,
        seed=17, relay_enabled=False, focus_task_ids=r['focus_task_ids'], **meta['configuration']['repair_parameters'])
    r.update(metadata=out['metadata'], returned_routes=deepcopy(alns), deadline_perf_counter=out['metadata']['deadline_perf_counter'],
        post_consideration_skipped='no_helper_predeadline_complete_checkpoint')
    # Put this already-expired helper within its recorded outer budget.
    meta['deadline_perf_counter'] = r['deadline_perf_counter'] + .15
    outer = meta['deadline_perf_counter'];meta['started_at_perf_counter'] = r['deadline_perf_counter']-1.
    meta['alns_record'].update(configured_at_perf_counter=meta['started_at_perf_counter']+.01,
        started_at_perf_counter=meta['started_at_perf_counter']+.02, returned_at_perf_counter=r['deadline_perf_counter']-.2)
    meta['alns_record']['deadline_perf_counter'] = meta['started_at_perf_counter']+(outer-meta['started_at_perf_counter'])*.65
    meta['alns_record']['config']['time_limit_seconds'] = max(.001,meta['alns_record']['deadline_perf_counter']-meta['alns_record']['configured_at_perf_counter'])
    first = meta['full_candidate_snapshots'][0];first['deadline_perf_counter'] = outer
    first['full_verified_at_perf_counter'] = r['deadline_perf_counter']-.1
    meta['full_candidate_snapshots'] = [first];meta['final_snapshot'] = first;meta['final_key'] = first['key']
    meta['accepted_moves'] = [dict(source=first['phase'],full_snapshot=first,full_verified_at_perf_counter=first['full_verified_at_perf_counter'],
        accepted_at_perf_counter=first['full_verified_at_perf_counter']+.001)];meta['accepted_improvements']=1
    meta['deadline_reached']=True;r['returned_at_perf_counter']=max(r['returned_at_perf_counter'], meta['alns_record']['returned_at_perf_counter'])
    result = prove(saved_control, meta)
    assert not result['repair_metadata_reference']['initialization_completed']
    assert result['wrapper_candidates_replayed'] == 1
    meta['full_candidate_snapshots'].append(deepcopy(source['full_candidate_snapshots'][1]))
    with pytest.raises(ValueError): prove(saved_control, meta)
