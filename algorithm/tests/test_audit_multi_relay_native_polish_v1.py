"""Independent current multi-group clocks with genuinely free native tasks."""
from copy import deepcopy
from time import perf_counter

import pytest

from algorithm.experiments import audit_multi_relay_native_polish_v1 as audit
from algorithm.tests.test_multi_relay_native_polish_v1 import fixture, run, instrument_solver, W, S
from algorithm.experiments import multi_relay_native_polish_v1 as polish
from algorithm.tests.test_relay_preserving_polish import real_weighted_runtime
from algorithm.tests.test_relay_repooling_exchange import topology


def data():
    p, routes, groups = fixture()
    tasks = {t.id: ((t.pickup.x, t.pickup.y), (t.delivery.x, t.delivery.y), t.deadline_min) for t in p.tasks}
    graph = topology(p)
    graph['task_relay_candidates'] = {str(t): list(p.task_relay_candidates[t]) for t in p.task_ids}
    return tasks, graph, routes, groups


def key(score):
    values = [score[k] for k in ('late_count', 'total_lateness_min', 'distance_km')]
    return [sum(w * v / s for w, v, s in zip(W, values, S)), *values]


def saved_snapshot(case, routes):
    proof = audit.audit_complete_state(*case, routes)
    fields = ('pin_scope', 'groups', 'pinned_task_ids', 'structure_signature', 'final_counts', 'group_count',
        'input_routes_sha256', 'group_metrics', 'score', 'post_global_score', 'event_times_min', 'delivery_times_min',
        'actual_final_owners', 'current_source_drop_availability_min', 'current_shared_double_loaded_km_by_group',
        'selected_native_classifications', 'changed_owner_task_ids', 'changed_event_sequence_aircraft',
        'relay_task_count', 'cross_uav_count', 'routes_sha256')
    saved = {k: deepcopy(proof[k]) for k in fields}
    saved.update(routes=[list(r) for r in routes], phase='complete_native_repair', valid=True, complete=True,
        key=key(proof['score']), full_verified_at_perf_counter=1., deadline_perf_counter=2.,
        parent_routes_sha256=audit._digest(case[2]))
    return saved


@pytest.fixture
def actual_result(real_weighted_runtime):
    p, routes, groups = fixture()
    result = run(real_weighted_runtime, p, routes, groups)
    return (*data(), result)


def prove_result(case, metadata=None):
    meta = case[-1]['metadata'] if metadata is None else metadata
    return audit.audit_metadata(*case[:4], meta, weights=W, scales=S, deadline=meta['deadline_perf_counter'])


def test_actual_archived_weighted_loop_native_owner_changes_and_all_group_waits(actual_result):
    proof = prove_result(actual_result);meta = actual_result[-1]['metadata']
    assert proof['passed'] and proof['initialization_completed']
    assert proof['saved_partial_entity_references'] and len(proof['saved_complete_entity_references']) >= 2
    assert proof['internal_best_checkpoints_replayed'] == meta['accepted_improvements']
    assert not proof['paid_macro_construction_provenance_verified']
    assert not proof['all_unrecorded_probes_entity_replayed']
    snapshots = [s for s in meta['full_candidate_snapshots'] if s['changed_owner_task_ids']]
    assert snapshots
    saved = snapshots[0]
    independent = audit.audit_complete_state(*actual_result[:4], saved['routes'], telemetry=saved, weights=W, scales=S)
    assert independent['changed_owner_task_ids'] and independent['final_counts'] == [3, 3, 3]
    assert independent['pin_signature'] == meta['pin_signature']
    assert len(independent['group_metrics']) == 2
    assert independent['group_metrics'] != meta['input_snapshot']['group_metrics']


def test_native_interleaving_inside_source_carry_and_between_receiver_deliveries_is_legal():
    case = data();post = [list(r) for r in case[2]]
    post[0].remove(7);post[0].remove(-7);p = post[0].index(-206) + 1;post[0][p:p] = [7, -7]
    post[1].remove(5);post[1].remove(-5);p = post[1].index(205) + 1;post[1][p:p] = [5, -5]
    saved = saved_snapshot(case, post)
    result = audit.audit_complete_state(*case, post, telemetry=saved, weights=W, scales=S)
    assert result['passed'] and result['cross_uav_count'] == 4
    assert result['changed_event_sequence_aircraft'] == [0, 1]
    assert result['changed_owner_task_ids'] == []


def test_true_partial_q_current_DAG_and_missing_pin_are_distinct_from_full_scores():
    case = data();partial = [[v for v in r if abs(v) not in {7, 9}] for r in case[2]]
    present = sorted(set(case[0]) - {7, 9})
    proof = audit.audit_partial_state(*case, partial, present)
    assert proof['remaining_quota_slots'] == [1, 0, 1] and proof['remaining_task_ids'] == [7, 9]
    assert not proof['complete_solution']
    saved = {k: deepcopy(v) for k, v in proof.items() if k not in ('passed', 'complete_solution')}
    saved.update(routes=partial, complete=False, key=key(proof['score']), checked_at_perf_counter=1.)
    assert audit.audit_partial_state(*case, partial, present, telemetry=saved, weights=W, scales=S, deadline=2.)['passed']
    for field, value in (('complete', True), ('remaining_quota_slots', [0, 1, 1]), ('checked_at_perf_counter', 2.)):
        bad = deepcopy(saved);bad[field] = value
        with pytest.raises(ValueError): audit.audit_partial_state(*case, partial, present, telemetry=bad, weights=W, scales=S, deadline=2.)
    registry = audit._geometry(case[0], case[1])[2]
    broken = [[v for v in r if audit._identity(v, registry) != 1] for r in partial]
    with pytest.raises(ValueError): audit.audit_partial_state(*case, broken, [t for t in present if t != 1])


@pytest.mark.parametrize('fault', ['take_gap', 'cycle', 'quota', 'new_relay'])
def test_full_native_freedom_never_releases_pin_shape_or_physical_constraints(fault):
    case = data();post = [list(r) for r in case[2]]
    if fault == 'take_gap':
        post[2].remove(9);post[2].remove(-9);post[2][1:1] = [9, -9]
    elif fault == 'cycle': post[0] = post[0][2:] + post[0][:2]
    elif fault == 'quota':
        post[0].remove(7);post[0].remove(-7);post[1] += [7, -7]
    else:
        post[0] = [213 if v == 7 else -213 if v == -7 else v for v in post[0]];post[1] += [214, -214]
    with pytest.raises(ValueError): audit.audit_complete_state(*case, post)


@pytest.mark.parametrize('fault', ['source_wait', 'old_source_clock', 'native_clock', 'owner', 'local_table', 'late_snapshot', 'false_selected_time'])
def test_current_all_group_metrics_cannot_be_borrowed_from_macro_or_forged(fault):
    case = data();saved = saved_snapshot(case, case[2])
    if fault == 'source_wait': saved['group_metrics'][0]['receiver_waiting_min'] += 1.
    elif fault == 'old_source_clock': saved['current_source_drop_availability_min']['1'] += 1.
    elif fault == 'native_clock': saved['delivery_times_min']['7'] += 1.
    elif fault == 'owner': saved['actual_final_owners']['7'] = 1
    elif fault == 'local_table': saved['table_score'] = saved['score']
    elif fault == 'late_snapshot': saved['full_verified_at_perf_counter'] = 2.
    else: saved['selected_as_best_at_perf_counter'] = .5
    with pytest.raises(ValueError): audit.audit_complete_state(*case, saved['routes'], telemetry=saved, weights=W, scales=S)


@pytest.mark.parametrize('fault', ['restoration', 'configuration', 'false_history', 'wrong_return', 'partial_q'])
def test_helper_metadata_binds_best_cache_restoration_and_samecoverage(actual_result, fault):
    meta = deepcopy(actual_result[-1]['metadata'])
    if fault == 'restoration': meta['objective_state_restored'] = False
    elif fault == 'configuration': meta['configuration']['relay_enabled'] = False
    elif fault == 'false_history': meta['best_checkpoint_history'].append(meta['input_snapshot'])
    elif fault == 'wrong_return': meta['post_repair_snapshot'] = None
    else: meta['partial_state_snapshots'][0]['remaining_quota_slots'][0] += 1
    with pytest.raises(ValueError): prove_result(actual_result, meta)


def test_expired_initialization_has_no_new_audited_checkpoint(real_weighted_runtime):
    p, macro, groups = fixture()
    result = run(real_weighted_runtime, p, macro, groups, deadline=perf_counter()-1.)
    case = (*data(), result)
    proof = prove_result(case)
    assert not proof['initialization_completed'] and not proof['predeadline_checkpoint_verified']
    bad = deepcopy(result['metadata']);bad['post_repair_snapshot'] = saved_snapshot(case[:4], macro)
    with pytest.raises(ValueError): prove_result(case, bad)


def test_actual_solver_cleanup_expiry_reuses_earlier_cache_without_new_snapshot(real_weighted_runtime, monkeypatch):
    module = real_weighted_runtime;p, macro, groups = fixture()
    clock = [perf_counter()];deadline = clock[0]+2.;solve = module.solve_alns
    def callback(problem, config, routes):
        result = solve(problem, config=config, initial_routes=routes)
        clock[0] = deadline+1.
        return result
    instrument_solver(monkeypatch, module, callback)
    monkeypatch.setattr(polish, 'perf_counter', lambda: clock[0])
    result = run(module, p, macro, groups, deadline=deadline)
    proof = prove_result((*data(), result))
    assert result['metadata']['deadline_reached'] and proof['predeadline_checkpoint_verified']
    assert result['metadata']['post_repair_snapshot']['full_verified_at_perf_counter'] < deadline
