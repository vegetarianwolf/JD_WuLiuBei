"""Independent mandatory damage, real donor slots and complete restore proof."""
from copy import deepcopy
from dataclasses import replace
from time import perf_counter

import pytest

from algorithm.experiments import audit_new_late_set_native_v1 as audit
from algorithm.experiments import audit_multi_relay_native_polish_v1 as native
from algorithm.tests.test_audit_joint_source_native_v1 import fixture as joint_fixture
from algorithm.tests import test_new_late_set_repair_v1 as producer
from algorithm.tests.test_relay_preserving_polish import real_weighted_runtime


def fixture():
    data = list(joint_fixture());tasks = dict(data[0])
    tasks[3] = (*tasks[3][:2], 8.);tasks[4] = (*tasks[4][:2], 12.)
    data[0] = tasks
    return data


def test_newly_late_set_uses_original_full_clock_and_protected_jobs_stay_separate():
    d = fixture()
    with audit.joint.joint_source_context():
        ctx = native._context(d[0], d[1], d[3], [d[5]])
        original = native.reference_schedule(d[0], d[2], d[1]);raw = ctx['macro_reference']
        owners = native._owners(d[3], ctx['geometry'][2])
        rows = audit._damage(d[0], original, raw, owners, {1, 2})
    assert [x['task_id'] for x in rows] == [3, 4]
    assert rows[0]['base_delivery_min'] == 7. and rows[0]['raw_delivery_min'] == 11.
    # The official rule is subtraction >EPS, including real floating edges.
    task = {1: ((0., 0.), (0., 0.), 0.)};old = {'delivery_times_min': {'1': 0.}}
    for clock in (1e-9, 1e-9+1e-12):
        got = audit._damage(task, old, {'delivery_times_min': {'1': clock}}, {1: 0}, set())
        assert bool(got) == (clock > 1e-9)
    assert not audit._damage(task, old, {'delivery_times_min': {'1': 2.}}, {1: 0}, {1})


def test_donor_directory_and_complete_assignments_never_invent_other_aircraft_slots():
    d = fixture();owners = {1: 2, 2: 2, 3: 0, 4: 0, 5: 1, 6: 1, 7: 3, 8: 3}
    rows, kept, omitted = audit._donor_directory(d[0], [3, 4], owners, {1, 2}, 2)
    assert set(kept) | set(omitted) == {5, 6, 7, 8}
    assert [row['rank'] for row in rows] == list(range(4))
    variants = audit._variants([3, 4], kept, owners, 2)
    assert variants[0] == dict(donor_task_ids=[], removed_task_ids=[3, 4],
        assignment_by_task={'3': 0, '4': 0}, insertion_orders=[[3, 4], [4, 3]],
        variant_index=0,kind='same_owner_control',quota_slots_by_aircraft=[2,0,0,0],exchanges=[])
    for variant in variants:
        U = variant['removed_task_ids'];assignment = variant['assignment_by_task']
        assert {a: sum(owners[t] == a for t in U) for a in range(4)} == {
            a: sum(assignment[str(t)] == a for t in U) for a in range(4)}
        assert all(set(order) == set(U) for order in variant['insertion_orders'])
    assert any(len(v['donor_task_ids']) == 2 for v in variants)


@pytest.mark.parametrize('donor', [False, True])
def test_complete_control_and_cross_owner_donor_restore_all_damage_before_comparing(donor):
    d = fixture()
    with audit.joint.joint_source_context():
        ctx = native._context(d[0], d[1], d[3], [d[5]])
        if not donor:
            U, assignment, order = [3, 4], {'3': 0, '4': 0}, [4, 3]
            post = [list(r) for r in d[3]];post[0] = [101, 103, -101, -103, 4, -4, 3, -3]
            trace = [dict(task_id=4, owner=0, pickup_gap=4, delivery_gap=4), dict(task_id=3, owner=0, pickup_gap=6, delivery_gap=6)]
        else:
            U, assignment, order = [3, 4, 5], {'3': 1, '4': 0, '5': 0}, [3, 4, 5]
            post = [[101, 103, -101, -103, 4, -4, 5, -5], [3, -3, 6, -6], list(d[3][2]), list(d[3][3])]
            trace = [dict(task_id=3, owner=1, pickup_gap=0, delivery_gap=0), dict(task_id=4, owner=0, pickup_gap=4, delivery_gap=4), dict(task_id=5, owner=0, pickup_gap=6, delivery_gap=6)]
        for record in trace: record['origin'] = 'finite_geometry_early_pickup_early_delivery'
        result = audit._transition(d[0], d[1], d[3], [d[5]], U, post, assignment, order, trace, ctx)
        assert result['all_removed_tasks_restored']
        assert result['quota_slots_by_aircraft'] == ([2, 1, 0, 0] if donor else [2, 0, 0, 0])
        assert result['prefixes'][-1]['remaining_quota_slots'] == [0] * 4
        assert result['prefixes'][-1]['present_task_ids'] == list(range(1, 9))


@pytest.mark.parametrize('fault', ['missing_damage', 'wrong_owner', 'wrong_gap', 'unselected_moved'])
def test_partial_or_wrong_slot_result_cannot_look_like_complete_joint_repair(fault):
    d = fixture();post = [list(r) for r in d[3]];post[0] = [101, 103, -101, -103, 4, -4, 3, -3]
    assignment = {'3': 0, '4': 0};order = [4, 3]
    trace = [dict(task_id=4, owner=0, pickup_gap=4, delivery_gap=4), dict(task_id=3, owner=0, pickup_gap=6, delivery_gap=6)]
    for record in trace: record['origin'] = 'finite_geometry_early_pickup_early_delivery'
    if fault == 'missing_damage': order = [4];trace = trace[:1]
    elif fault == 'wrong_owner': assignment['3'] = 1;trace[1]['owner'] = 1
    elif fault == 'wrong_gap': trace[1]['delivery_gap'] = 99
    else: post[1] = [6, -6, 5, -5]
    with audit.joint.joint_source_context():
        ctx = native._context(d[0], d[1], d[3], [d[5]])
        with pytest.raises((ValueError, KeyError)):
            audit._transition(d[0], d[1], d[3], [d[5]], [3, 4], post, assignment, order, trace, ctx)


@pytest.fixture(scope='module')
def focused_artifacts():
    p,base,raw,groups=producer.fixture();tasks,graph=producer.evidence(p)
    control=producer.run(p,base,raw,groups,donor_limit=0,max_donors=0,position_limit=12)['metadata']
    donors=producer.run(p,base,raw,groups,donor_limit=2,max_donors=2,position_limit=1,option_count=1)['metadata']
    return tasks,graph,base,raw,groups,control,donors


def focus_proof(data,metadata):
    tasks,graph,base,raw,groups=data[:5]
    return audit.audit_focused_stage(tasks,graph,base,raw,groups,metadata,
        weights=producer.W,scales=producer.S,deadline=metadata['deadline_perf_counter'])


def test_actual_whole_damage_control_and_double_donor_metadata_are_independently_replayed(focused_artifacts):
    data=focused_artifacts
    for metadata in data[5:]:
        proof=focus_proof(data,metadata)
        assert proof['passed'] and proof['exact_damage_task_ids']==[5,6]
        assert proof['predeadline_checkpoint_verified'] and proof['saved_complete_entity_references']
        assert all(t['all_removed_tasks_restored'] for t in proof['complete_transition_references'])
        assert not proof['all_unrecorded_probes_entity_replayed']
    assert data[5]['accepted_improvements']>0
    assert data[5]['best_checkpoint_history']
    assert any(v['kind']=='double_donor_exchange' for v in data[6]['variants'])


@pytest.mark.parametrize('fault', ['omit_damage','false_donor_geometry','omit_variant','false_slots',
    'insertion_gap','restore_owner','false_partial_complete','false_full_clock','false_best_stamp','omit_history'])
def test_actual_metadata_mutations_cannot_hide_missing_cost_or_fake_complete_certificate(focused_artifacts,fault):
    metadata=deepcopy(focused_artifacts[6] if fault=='false_donor_geometry' else focused_artifacts[5])
    if fault=='omit_damage':metadata['D_native'].pop()
    elif fault=='false_donor_geometry':metadata['donor_directory'][0]['geometric_score']+=1.
    elif fault=='omit_variant':metadata['variant_directory'].pop()
    elif fault=='false_slots':metadata['variants'][0]['removal_partial']['remaining_quota_slots'][0]-=1
    elif fault=='insertion_gap':metadata['variants'][0]['saved_complete_checkpoints'][0]['insertion_trace'][0]['pickup_gap']=999
    elif fault=='restore_owner':metadata['variants'][0]['assignment_by_task']['5']=1
    elif fault=='false_partial_complete':metadata['variants'][0]['removal_partial']['complete']=True
    elif fault=='false_full_clock':metadata['variants'][0]['saved_complete_checkpoints'][0]['event_times_min'][0][0]+=1.
    elif fault=='false_best_stamp':metadata['best_checkpoint_history'][0]['selected_as_best_at_perf_counter']=metadata['deadline_perf_counter']
    else:metadata['best_checkpoint_history']=[]
    with pytest.raises((AssertionError,ValueError,KeyError)):
        focus_proof(focused_artifacts,metadata)


def test_zero_damage_cap_skip_and_expired_initialization_are_honest_full_set_paths():
    p,base,raw,groups=producer.fixture()
    for local,kwargs,reason,initialized in ((p,dict(max_damage=1),'new_late_native_set_exceeds_max_damage',True),
        (replace(p,tasks=tuple(replace(t,deadline_min=100.) for t in p.tasks)),{},'no_newly_late_native_tasks',True),
        (p,dict(deadline=perf_counter()-1.),None,False)):
        result=producer.run(local,base,raw,groups,**kwargs);tasks,graph=producer.evidence(local)
        metadata=result['metadata'];proof=audit.audit_focused_stage(tasks,graph,base,raw,groups,metadata,
            weights=producer.W,scales=producer.S,deadline=metadata['deadline_perf_counter'])
        assert proof['passed'] and proof['initialization_completed'] is initialized
        assert not metadata['variants'] and not metadata['accepted_improvements']
        if reason:assert metadata['skipped_reason']==reason
        else:assert not proof['predeadline_checkpoint_verified'] and result['evaluation'] is None


def test_paid_actual_raw_focus_archived_native_chain_and_added_clock_contract(real_weighted_runtime):
    p,base,_,_=producer.fixture()
    p=replace(p,task_relay_candidates={t:(1,) if t in (1,2) else () for t in p.task_ids},
        leg_registry={n:l for n,l in p.leg_registry.items() if l.task_id in (1,2)})
    result=producer.wrapper.refine(p,base,deadline=perf_counter()+3.,weights=producer.W,scales=producer.S,seed=51,
        max_groups=1,beam_width=1,max_families=1,source_option_limit=1,receiver_option_limit=1,
        position_limit=3,snapshot_limit=3,polish_max_iterations=2,alns_module=real_weighted_runtime,
        focus_position_limit=3,focus_option_count=2,focus_donor_limit=1,focus_max_donors=1)
    metadata=result['metadata'];tasks,graph=producer.evidence(p)
    params={**metadata['configuration']['raw_parameters'],'raw_fraction':.4,'polish_max_iterations':2,
        'polish_initial_temperature':.0025,'polish_minimum_temperature':.00001,'polish_cooling_rate':.97,
        **metadata['damage_extension']['parameters']}
    manifest=dict(weights=producer.W,scales=producer.S,method_parameters={'multi_native_relay':params},
        accumulation_fixed_catalogue_parameters=dict(feeder_limit=3,base_pool_limit=64))
    before=deepcopy(manifest);original= audit.paid.polish_audit.audit_metadata
    proof=audit._audit_metadata(tasks,graph,base,metadata,relay=True,manifest=manifest)
    assert proof['passed'] and proof['focused_native_extension_evidence']['independent_metadata_hook_restored']
    assert manifest==before and audit.paid.polish_audit.audit_metadata is original
    leaf=metadata['polish_records'][0]
    assert leaf['metadata']['iterations']==2 and leaf['metadata']['focused_stage']['metadata']['initialization_completed']
    for fault in ('restoration','native_input','allocation','fraction','parameters'):
        bad=deepcopy(metadata)
        if fault=='restoration':bad['damage_extension']['restored']=False
        elif fault=='native_input':bad['polish_records'][0]['metadata']['focused_stage']['native_input_routes_sha256']='0'*64
        elif fault=='allocation':
            stage=bad['polish_records'][0]['metadata']['focused_stage'];stage['focus_started_at_perf_counter']=leaf['allocation_started_at_perf_counter']-1.
            stage['focus_deadline_perf_counter']=min(leaf['deadline_perf_counter'],stage['focus_started_at_perf_counter']+min(20.,
                max(0.,leaf['deadline_perf_counter']-stage['focus_started_at_perf_counter'])*.4))
        elif fault=='fraction':bad['polish_records'][0]['metadata']['focused_stage']['focus_deadline_perf_counter']+=.1
        else:bad['damage_extension']['parameters']['focus_max_damage']=0
        with pytest.raises((AssertionError,ValueError,KeyError)):
            audit._audit_metadata(tasks,graph,base,bad,relay=True,manifest=manifest)
        assert audit.paid.polish_audit.audit_metadata is original and not audit._active
    # An already-expired real leaf cannot turn a focused cache into a native
    # certificate; the clipped child deadline is still exactly the old leaf.
    _,_,raw,groups=producer.fixture();deadline=perf_counter()-1.
    focused_parameters=metadata['damage_extension']['parameters']
    with producer.wrapper.joint.joint_structure_context(),producer.wrapper.focused_native_context(base,parameters=focused_parameters):
        expired=producer.wrapper.joint.wrapper.polish.polish_groups(p,raw,groups,deadline=deadline,
            weights=producer.W,scales=producer.S,seed=1,alns_module=real_weighted_runtime,snapshot_limit=3)
    stage=expired['metadata']['focused_stage']
    assert stage['focus_started_at_perf_counter']>deadline and stage['focus_deadline_perf_counter']==deadline
    assert not stage['focus_used'] and stage['native_initialization_failed'] and expired['evaluation'] is None
    with audit.joint.joint_source_context(),audit._focused_audit_context(base,focused_parameters,{}) as references:
        audit.paid.polish_audit.audit_metadata(tasks,graph,raw,groups,expired['metadata'],
            weights=producer.W,scales=producer.S,deadline=deadline)
    assert not references[0]['focused_stage_evidence']['predeadline_checkpoint_verified']
    assert not references[0]['native_evidence']['predeadline_checkpoint_verified']
