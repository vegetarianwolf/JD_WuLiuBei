"""Independent joint source geometry, mixed groups and native partial physics."""
from copy import deepcopy

import pytest

from algorithm.experiments import audit_joint_source_native_v1 as audit
from algorithm.experiments import audit_multi_relay_accumulation_v1 as generic
from algorithm.experiments import audit_multi_relay_native_polish_v1 as native
from algorithm.tests.test_audit_multi_relay_accumulation_v1 import fixture as split_fixture
from algorithm.tests.test_relay_preserving_polish import real_weighted_runtime

_PAID_CASE = None


def fixture():
    tasks, graph, base, split, _, oldgroup, _ = split_fixture()
    tasks = dict(tasks);tasks[2] = ((2., 0.), (2., 1.), 100.)
    joint = dict(group_id=1, i=1, j=2, a=0, b=0, c=2, station_id=1, source_type='joint_pair')
    first = ((101, 103, -101, -103, 3, -3, 4, -4), base[1], (102, 104, -102, -104), base[3])
    new = dict(group_id=2, i=3, j=4, a=3, b=3, c=0, station_id=1, source_type='joint_pair')
    two = (first[0][:4] + (106, 108, -106, -108), first[1], first[2],
           base[3] + (107, 105, -105, -107))
    mixed = ((101, -101, 106, 108, -106, -108), split[1], split[2], two[3])
    return tasks, graph, base, first, two, joint, new, split, mixed, oldgroup


def test_one_joint_source_actual_doubleload_drop_clock_and_finalquota():
    d = fixture();stats = {}
    with audit.joint_source_context(stats):
        p = generic.audit_step(*d[:3], d[2], d[3], [], d[5])
    assert p['passed'] and p['cross_uav_count'] == 2 and p['final_tasks_per_aircraft'] == [2] * 4
    assert p['group_metrics'][0]['source_aircraft'] == [0, 0]
    assert p['group_metrics'][0]['source_availability_min'] == {'1': 4., '2': 4.}
    assert stats['unique_current_joint_source_proofs'][0]['shared_in_loaded_distance_km'] == 2.
    assert stats['unique_current_joint_source_proofs'][0]['drop_times_min'] == [4., 4.]
    assert stats['independent_functions_restored']


def test_two_joint_groups_share_aircraft_and_mixed_old_split_parent_retains_its_proof():
    d = fixture()
    with audit.joint_source_context():
        p = generic.audit_step(*d[:3], d[3], d[4], [d[5]], d[6])
        q = generic.audit_step(*d[:3], d[7], d[8], [d[9]], d[6])
    assert p['group_count'] == q['group_count'] == 2
    assert p['cross_uav_count'] == q['cross_uav_count'] == 4
    assert q['groups'][0] == d[9] and 'source_type' not in q['groups'][0]


def test_joint_source_pickup_with_other_native_cargo_and_native_service_between_pickups_is_legal():
    d = fixture();routes = [list(r) for r in d[3]]
    routes[0] = [3, 101, -3, 103, -101, -103, 4, -4]
    with audit.joint_source_context(stats := {}):
        assert generic.audit_state(*d[:3], routes, [d[5]])['passed']
    p = stats['unique_current_joint_source_proofs'][0]
    assert p['pickup_positions'] == [1, 3] and p['drop_positions'] == [4, 5]


@pytest.mark.parametrize('fault', ['unknown_type', 'untyped_same_source', 'different_sources', 'wrong_receiver', 'split_drop', 'overload', 'zero_shared'])
def test_source_identity_drop_capacity_and_positive_real_carry_are_not_relaxed(fault):
    d = fixture();tasks = deepcopy(d[0]);routes = [list(r) for r in d[3]];group = deepcopy(d[5])
    if fault == 'unknown_type': group['source_type'] = 'other'
    elif fault == 'untyped_same_source': del group['source_type']
    elif fault == 'different_sources': group['b'] = 1
    elif fault == 'wrong_receiver': group['c'] = 0
    elif fault == 'split_drop': routes[0] = [101, 103, -101, 3, -3, -103, 4, -4]
    elif fault == 'overload': routes[0] = [101, 103, 3, -3, -101, -103, 4, -4]
    else: tasks[2] = ((0., 0.), tasks[2][1], tasks[2][2])
    with audit.joint_source_context(), pytest.raises((ValueError, KeyError)):
        generic.audit_state(tasks, d[1], d[2], routes, [group])


def test_two_joint_groups_can_form_real_global_cycle_despite_closed_local_cargo():
    d = fixture();group = dict(d[6], a=2, b=2)
    cycle = ((106, 108, -106, -108, 101, 103, -101, -103), d[2][1],
             (102, 104, -102, -104, 107, 105, -105, -107), d[2][3])
    with audit.joint_source_context(), pytest.raises(ValueError, match='cycle'):
        generic.audit_state(*d[:3], cycle, [d[5], group])


def test_native_post_allows_owner_changes_and_true_partial_slots_while_joint_identity_persists():
    d = fixture();post = [list(r) for r in d[3]]
    post[0] = [5 if v == 3 else -5 if v == -3 else v for v in post[0]]
    post[1] = [3 if v == 5 else -3 if v == -5 else v for v in post[1]]
    with audit.joint_source_context():
        p = native.audit_complete_state(d[0], d[1], d[3], [d[5]], post)
        partial = [[v for v in r if abs(v) not in {3, 7}] for r in post]
        q = native.audit_partial_state(d[0], d[1], d[3], [d[5]], partial, [t for t in d[0] if t not in {3, 7}])
    assert p['changed_owner_task_ids'] == [3, 5] and p['final_counts'] == [2] * 4
    assert q['remaining_quota_slots'] == [0, 1, 0, 1] and not q['complete_solution']


def test_exception_nested_context_and_no_change_always_restore_original_auditor_functions():
    before = {n: getattr(generic, n) for n in audit.PATCH_NAMES};stats = {}
    with pytest.raises(RuntimeError, match='intentional'):
        with audit.joint_source_context(stats):
            with pytest.raises(RuntimeError, match='Nested'):
                with audit.joint_source_context(): pass
            raise RuntimeError('intentional')
    assert stats['independent_functions_restored'] and all(getattr(generic, n) is f for n, f in before.items())
    d = fixture()
    with pytest.raises(ValueError): generic.audit_state(*d[:3], d[3], [d[5]])


def test_current_source_global_drop_or_wait_telemetry_cannot_be_forged():
    d = fixture()
    with audit.joint_source_context():
        p = generic.audit_state(*d[:3], d[3], [d[5]])
        fields = ('groups', 'group_metrics', 'score', 'post_global_score', 'actual_final_owners', 'event_times_min',
            'delivery_times_min', 'current_source_drop_availability_min', 'current_shared_double_loaded_km_by_group',
            'selected_native_classifications', 'cross_uav_count', 'relay_task_count', 'routes_sha256')
        saved = {k: deepcopy(p[k]) for k in fields}
        score = p['score'];values = [score[k] for k in ('late_count', 'total_lateness_min', 'distance_km')]
        saved.update(routes=[list(r) for r in d[3]], valid=True, complete=True, pin_scope=generic.SCOPE,
            key=[sum(w * v / s for w, v, s in zip((.5, .25, .25), values, (8., 100., 100.))), *values],
            full_verified_at_perf_counter=1., deadline_perf_counter=2.)
        saved['current_source_drop_availability_min']['1'] += .1
        with pytest.raises(ValueError):
            generic.audit_state(*d[:3], d[3], [d[5]], telemetry=saved, weights=(.5, .25, .25), scales=(8., 100., 100.))


@pytest.fixture
def paid_case(real_weighted_runtime):
    global _PAID_CASE
    if _PAID_CASE is None:
        from algorithm.tests.test_joint_source_native_refinement_v1 import fixture as actual_fixture, run, W, S
        from algorithm.tests.test_relay_repooling_exchange import topology
        p, base = actual_fixture();result = run(real_weighted_runtime, p, base);meta = result['metadata']
        tasks = {t.id: ((t.pickup.x, t.pickup.y), (t.delivery.x, t.delivery.y), t.deadline_min) for t in p.tasks}
        graph = topology(p);graph['task_relay_candidates'] = {str(t): list(p.task_relay_candidates[t]) for t in p.task_ids}
        params = {**meta['configuration']['raw_parameters'], 'raw_fraction': .4, 'polish_max_iterations': 2,
            'polish_initial_temperature': .0025, 'polish_minimum_temperature': .00001, 'polish_cooling_rate': .97}
        manifest = dict(weights=W, scales=S, method_parameters={'multi_native_relay': params},
            accumulation_fixed_catalogue_parameters=dict(feeder_limit=3, base_pool_limit=64))
        _PAID_CASE = tasks, graph, base, meta, manifest
    return deepcopy(_PAID_CASE)


def prove(case):
    tasks, graph, base, metadata, manifest = case
    return audit._audit_metadata(tasks, graph, base, metadata, relay=True, manifest=manifest)


def test_actual_paid_joint_catalogue_native_partial_and_original_guard_use_one_frozen_callback(paid_case):
    proof = prove(paid_case);meta = paid_case[3]
    assert proof['passed'] and proof['raw_method_evidence']['catalogue_references']
    assert proof['native_leaf_evidence'] and proof['proposal_references']
    stats = proof['joint_source_extension_evidence']
    assert stats['independent_functions_restored'] and stats['unique_current_joint_source_proofs']
    assert all(p['shared_in_loaded_distance_km'] > 1e-10 and p['drop_times_min'][0] == p['drop_times_min'][1]
        for p in stats['unique_current_joint_source_proofs'])
    assert proof['final_routes_sha256'] == generic._digest(meta['final_snapshot']['routes'])
    assert all(entry['metadata']['iterations'] == 2 for entry in meta['polish_records'])
    assert not stats['discarded_structural_catalogue_candidates_claimed_globally_feasible']
    assert not hasattr(audit, 'audit')


@pytest.mark.parametrize('fault', ['joint_profile', 'source_shared', 'route_digest', 'drop_position',
    'current_clock', 'extension_restore', 'extension_elapsed', 'split_new_family', 'bool_pickup_order'])
def test_joint_early_proxy_compact_identity_current_clock_and_alias_receipt_cannot_drift(paid_case, fault):
    m = paid_case[3];cat = m['raw_metadata']['steps'][0]['parent_records'][0]['catalogue']
    if fault == 'joint_profile': cat['families'][0]['source_joint_profile']['estimated_drop_min'] += .1
    elif fault == 'source_shared': cat['complete_candidates'][0]['source_layouts'][0]['shared_in_loaded_distance_km'] += 1.
    elif fault == 'route_digest': cat['complete_candidates'][0]['routes_sha256'] = '0' * 64
    elif fault == 'drop_position': cat['complete_candidates'][0]['source_layouts'][0]['drop_positions'][0] += 1
    elif fault == 'current_clock': m['full_candidate_snapshots'][-1]['current_source_drop_availability_min']['1'] += .1
    elif fault == 'extension_restore': m['extension']['hook_identity']['raw.structure']['restored'] = False
    elif fault == 'extension_elapsed': m['extension']['elapsed_seconds'] += 1.
    elif fault == 'split_new_family': del cat['families'][0]['source_type']
    else:
        row = cat['complete_candidates'][0]['source_layouts'][0]
        row['pickup_order_task_ids'] = [True if t == 1 else t for t in row['pickup_order_task_ids']]
    originals = {n: getattr(generic, n) for n in audit.PATCH_NAMES}
    with pytest.raises((ValueError, KeyError, AssertionError)): prove(paid_case)
    assert not audit._active and all(getattr(generic, n) is fn for n, fn in originals.items())
