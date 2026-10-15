"""Synthetic complete-set transactions and the actual paid thin pipeline."""
from copy import deepcopy
from dataclasses import replace
from time import perf_counter

import pytest
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import new_late_set_repair_v1 as repair
from algorithm.experiments import new_late_set_native_refinement_v1 as wrapper
from algorithm.tests.test_relay_preserving_polish import fixture as original_fixture, real_weighted_runtime
from algorithm.tests.test_relay_repooling_exchange import topology

W,S=(.5,.25,.25),(9.,100.,100.)


def fixture():
    p,base=original_fixture(direct=True)
    p=replace(p,tasks=tuple(replace(t,deadline_min=5.) if t.id in (5,6) else t for t in p.tasks))
    raw=((3,-3,101,103,-101,-103,5,-5,6,-6),base[1],(102,104,-102,-104,9,-9))
    groups=[dict(group_id=1,i=1,j=2,a=0,b=0,c=2,station_id=1,source_type='joint_pair')]
    assert evaluate_solution(p,base,_force_full=True).valid
    assert evaluate_solution(p,raw,_force_full=True).valid
    return p,base,raw,groups


def run(p,base,raw,groups,**kwargs):
    with wrapper.joint.joint_structure_context():
        return repair.refine(p,base,raw,groups,deadline=kwargs.pop('deadline',perf_counter()+3.),
            weights=W,scales=S,seed=41,position_limit=kwargs.pop('position_limit',3),
            option_count=kwargs.pop('option_count',2),**kwargs)


def assert_complete(p,raw,groups,snap):
    rs=tuple(map(tuple,snap['routes']));ev=GlobalRelayEvaluator(p).evaluate(rs)
    full=evaluate_solution(p,rs,_force_full=True)
    assert full.valid and ev.feasible and repair.native._same_score(full.score,ev.score)
    assert snap['score']==snap['post_global_score']==repair.native.metrics._physical(ev.score)
    assert snap['event_times_min']==[[ev.event_times_min[a,k] for k in range(len(r))] for a,r in enumerate(rs)]
    assert snap['structure_signature']==wrapper.joint.joint_structure.signature(p,raw,groups)
    assert wrapper.joint.joint_structure.signature(p,rs,groups)==snap['structure_signature']
    assert snap['full_verified_at_perf_counter']<snap['deadline_perf_counter']
    assert snap['final_counts']==[3,3,3]


def evidence(p):
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in p.tasks}
    graph=topology(p);graph['task_relay_candidates']={str(t):list(p.task_relay_candidates[t]) for t in p.task_ids}
    return tasks,graph


def assert_public_focus(p,base,raw,groups,meta):
    from algorithm.experiments.audit_new_late_set_native_v1 import audit_focused_stage
    tasks,graph=evidence(p)
    assert audit_focused_stage(tasks,graph,base,raw,groups,meta,weights=W,scales=S,
        deadline=meta['deadline_perf_counter'])['passed']


def test_mandatory_same_owner_set_waits_for_complete_and_can_improve_after_costly_partial():
    p,base,raw,groups=fixture();result=run(p,base,raw,groups,donor_limit=0,max_donors=0,position_limit=12)
    m=result['metadata'];assert m['D_all']==m['D_native']==[5,6] and not m['D_protected']
    assert m['selection_completed'] and m['completed_variants']==m['started_variants']==1
    v=m['variants'][0];assert v['kind']=='same_owner_control' and v['removed_task_ids']==[5,6]
    assert v['quota_slots_by_aircraft']==[2,0,0] and v['assignment_by_task']=={'5':0,'6':0}
    assert v['removal_partial']['remaining_task_ids']==[5,6] and not v['removal_partial']['complete']
    assert v['insertion_orders']==[[5,6],[6,5]] and all(o['completed'] for o in v['orders'])
    assert m['accepted_improvements']>0 and m['final_key']<m['initial_key']
    assert set(m['post_repair_snapshot']['repaired_damage_task_ids'])=={5,6}
    assert m['complete_candidates']>len(v['saved_complete_checkpoints'])
    assert m['configuration']['partial_J_pruning'] is False
    best=m['post_repair_snapshot'];trace=best['insertion_trace'];partial=tuple(map(tuple,v['removal_partial']['routes']))
    first=trace[0];rows=list(partial);a=first['owner'];rows[a]=repair._insert(rows[a],first['task_id'],first['pickup_gap'],first['delivery_gap'])
    partial_G=GlobalRelayEvaluator(p).evaluate(partial);one_G=GlobalRelayEvaluator(p).evaluate(rows)
    assert repair.native._key(one_G.score,W,S)>repair.native._key(partial_G.score,W,S)
    assert_complete(p,raw,groups,best)
    assert m['input_snapshot']['group_metrics'][0]['receiver_waiting_min']>0
    assert best['group_metrics'][0]['second_take_min']!=m['input_snapshot']['group_metrics'][0]['second_take_min']
    assert_public_focus(p,base,raw,groups,m)


def test_single_and_double_donors_release_exact_slots_and_all_orders_settle_U():
    p,base,raw,groups=fixture();result=run(p,base,raw,groups,donor_limit=2,max_donors=2,position_limit=1,option_count=1)
    m=result['metadata'];I=build_plan_index(p,raw)
    assert m['donor_directory']==sorted(m['donor_directory'],key=lambda r:(r['geometric_score'],r['current_owner'],r['task_id']))
    assert m['retained_donor_task_ids']==[r['task_id'] for r in m['donor_directory'][:2]]
    assert any(v['kind']=='double_donor_exchange' for v in m['variants'])
    for v in m['variants']:
        U=v['removed_task_ids'];assert {5,6}<=set(U) and not set(U)&{1,2}
        q=[sum(I.delivery_route_by_task[t]==a for t in U) for a in range(3)]
        assert q==v['quota_slots_by_aircraft']==v['removal_partial']['remaining_quota_slots']
        assert q==[sum(v['assignment_by_task'][str(t)]==a for t in U) for a in range(3)]
        assert v['completed'] and all(o['completed'] for o in v['orders'])
        assert all(set(o['task_ids'])==set(U) for o in v['orders'])
        for snap in v['saved_complete_checkpoints']:
            assert set(snap['removed_task_ids'])==set(U)
            assert {s['task_id'] for s in snap['insertion_trace']}==set(U)
            assert_complete(p,raw,groups,snap)
    assert m['frontier_positions_retained']==m['primary_frontier_positions_retained']+m['extra_original_position_branches']
    assert m['frontier_positions_omitted']==m['primary_frontier_positions_omitted']-m['extra_original_position_branches']
    assert_public_focus(p,base,raw,groups,m)


def test_anchor_reconstructs_raw_and_frontier_never_splits_protected_joint_gaps():
    p,base,raw,groups=fixture();initial=tuple(tuple(v for v in r if abs(v) not in {5,6}) for r in raw)
    for order in ((5,6),(6,5)):
        route=initial[0]
        for t in order:
            g,h=repair._original_option(route,raw[0],t);route=repair._insert(route,t,g,h)
        assert route==raw[0]
    assert repair._banned(p,raw[0],0,groups)=={5}
    assert repair._banned(p,raw[2],2,groups)=={1}
    result=run(p,base,raw,groups,donor_limit=0,max_donors=0,position_limit=1,option_count=1)
    assert result['metadata']['extra_original_position_branches']>0
    for s in result['metadata']['variants'][0]['saved_complete_checkpoints']:
        assert_complete(p,raw,groups,s)


def test_damage_cap_skips_whole_set_without_silent_truncation():
    p,base,raw,groups=fixture();result=run(p,base,raw,groups,max_damage=1)
    m=result['metadata'];assert m['D_native']==[5,6] and m['selection_completed']
    assert m['skipped_reason']=='new_late_native_set_exceeds_max_damage'
    assert not m['variants'] and result['routes']==raw and m['post_repair_snapshot'] is m['input_snapshot']


def test_full_validator_rejection_does_not_create_a_checkpoint_or_abort_other_completions(monkeypatch):
    p,base,raw,groups=fixture();original=repair.native._complete;calls=[0]
    def reject_one(*args,**kwargs):
        calls[0]+=1
        if calls[0]==2:raise ValueError('controlled full rejection')
        return original(*args,**kwargs)
    monkeypatch.setattr(repair.native,'_complete',reject_one)
    result=run(p,base,raw,groups,donor_limit=0,max_donors=0,position_limit=1,option_count=1)
    m=result['metadata']
    assert m['complete_validator_rejections']==1 and m['complete_candidates']>0
    assert m['complete_proposals_generated']==m['complete_candidates']+1
    assert result['evaluation'].valid and m['returned_checkpoint_checked_within_deadline']


def test_empty_and_protected_damage_are_explicit():
    p,base,raw,groups=fixture()
    empty=run(p,base,base,[],donor_limit=0,max_donors=0)['metadata']
    assert empty['D_all']==empty['D_native']==[] and empty['skipped_reason']=='no_newly_late_native_tasks'
    p=replace(p,tasks=tuple(replace(t,deadline_min=16.) if t.id==1 else t for t in p.tasks))
    delayed=(base[0]+(101,103,-101,-103),base[1],raw[2])
    result=run(p,base,delayed,groups)
    assert result['metadata']['D_all']==result['metadata']['D_protected']==[1]
    assert result['metadata']['D_native']==[] and not result['metadata']['variants']


def test_entry_and_after_initialization_expiry_only_return_existing_checkpoint(monkeypatch):
    p,base,raw,groups=fixture()
    expired=run(p,base,raw,groups,deadline=perf_counter()-1.)
    assert expired['evaluation'] is None and expired['routes']==raw
    assert not expired['metadata']['selection_completed'] and expired['metadata']['post_repair_snapshot'] is None
    start=perf_counter();clock=[start];original=repair._directory
    def end(*args,**kwargs):
        clock[0]=start+100.;return original(*args,**kwargs)
    monkeypatch.setattr(repair,'perf_counter',lambda:clock[0]);monkeypatch.setattr(repair,'_directory',end)
    result=run(p,base,raw,groups,deadline=start+99.)
    m=result['metadata'];assert m['deadline_reached'] and not m['selection_completed']
    assert m['returned_checkpoint_checked_within_deadline'] and m['post_repair_snapshot'] is m['input_snapshot']
    assert result['evaluation'].valid and result['routes']==raw


def test_expiry_before_publishing_better_complete_state_keeps_atomic_history(monkeypatch):
    p,base,raw,groups=fixture();actual=repair.native._snapshot;start=perf_counter()
    deadline=start+99.;clock=[start];unpublished=[]
    initial_key=repair.native._key(GlobalRelayEvaluator(p).evaluate(raw).score,W,S)
    def build(*args,**kwargs):
        snap=actual(*args,**kwargs)
        if kwargs.get('phase')=='complete_new_late_set_transaction' and tuple(snap['key'])<initial_key:
            unpublished.append(snap);clock[0]=deadline+1.
        return snap
    monkeypatch.setattr(repair.native,'_snapshot',build)
    monkeypatch.setattr(repair,'perf_counter',lambda:clock[0])
    result=run(p,base,raw,groups,deadline=deadline,donor_limit=0,max_donors=0)
    m=result['metadata'];assert unpublished and m['deadline_reached']
    assert m['accepted_improvements']==0 and not m['best_checkpoint_history']
    assert m['post_repair_snapshot'] is m['input_snapshot'] and result['routes']==raw
    assert all(s is not unpublished[0] for v in m['variants'] for s in v['saved_complete_checkpoints'])


@pytest.mark.parametrize('error',[ValueError,repair.Deadline])
def test_callable_context_restores_on_exception_and_deadline(error):
    p,base,_,_=fixture();module=wrapper.joint.wrapper.polish;before=module.polish_groups;receipt={}
    params=wrapper._parameters(.4,20.,4,2,12,3,4)
    with pytest.raises(error):
        with wrapper.focused_native_context(base,parameters=params,statistics=receipt):
            assert module.polish_groups is not before
            with pytest.raises(RuntimeError,match='nestable'):
                with wrapper.focused_native_context(base,parameters=params):pass
            raise error('controlled')
    assert module.polish_groups is before and not wrapper._active
    assert receipt['restored'] and receipt['hook_identity']==dict(installed=True,restored=True)


def test_native_init_expiry_does_not_relabel_focus_as_native_checkpoint(monkeypatch):
    p,base,raw,groups=fixture();module=wrapper.joint.wrapper.polish
    def no_native(p,rows,groups,**kwargs):
        return dict(routes=rows,evaluation=None,iterations=0,metadata=dict(input_snapshot=None,
            post_repair_snapshot=None,returned_checkpoint_checked_within_deadline=False,
            started_at_perf_counter=123.,runtime_seconds=0.))
    monkeypatch.setattr(module,'polish_groups',no_native)
    params=wrapper._parameters(.4,20.,0,0,3,2,4)
    with wrapper.joint.joint_structure_context(),wrapper.focused_native_context(base,parameters=params):
        result=module.polish_groups(p,raw,groups,deadline=perf_counter()+3.,weights=W,scales=S,seed=1,snapshot_limit=3)
    m=result['metadata'];assert result['routes']==raw and result['evaluation'] is None
    assert m['input_snapshot'] is m['post_repair_snapshot'] is None and m['started_at_perf_counter']==123.
    assert m['focused_stage']['focus_used'] and m['focused_stage']['native_initialization_failed']


def test_start_after_leaf_deadline_clips_child_deadline_and_returns_raw_unchecked(monkeypatch):
    p,base,raw,groups=fixture();module=wrapper.joint.wrapper.polish
    leaf=perf_counter()-1.;clock=leaf+2.;seen=[]
    def expired_native(p,rows,groups,**kwargs):
        seen.append(rows)
        return dict(routes=rows,evaluation=None,iterations=0,metadata=dict(input_snapshot=None,
            post_repair_snapshot=None,returned_checkpoint_checked_within_deadline=False))
    monkeypatch.setattr(module,'polish_groups',expired_native)
    monkeypatch.setattr(wrapper,'perf_counter',lambda:clock)
    params=wrapper._parameters(.4,20.,0,0,3,2,4)
    with wrapper.joint.joint_structure_context(),wrapper.focused_native_context(base,parameters=params):
        result=module.polish_groups(p,raw,groups,deadline=leaf,weights=W,scales=S,seed=1,snapshot_limit=3)
    stage=result['metadata']['focused_stage']
    assert stage['focus_started_at_perf_counter']>leaf and stage['focus_deadline_perf_counter']==leaf
    assert not stage['focus_used'] and stage['native_initialization_failed']
    assert stage['metadata']['selection_completed'] is False and stage['metadata']['input_snapshot'] is None
    assert seen==[raw] and result['routes']==raw and result['evaluation'] is None


@pytest.mark.parametrize('fraction',[0.,1.,float('inf')])
def test_invalid_focused_allocation_before_patch(fraction):
    before=wrapper.joint.wrapper.polish.polish_groups
    with pytest.raises(ValueError):wrapper._parameters(fraction,20.,4,2,12,3,4)
    assert wrapper.joint.wrapper.polish.polish_groups is before and not wrapper._active


def test_real_paid_raw_focus_then_archived_weighted_native_without_clock_rewrite(real_weighted_runtime):
    p,base,_,_=fixture();p=replace(p,task_relay_candidates={t:(1,) if t in (1,2) else () for t in p.task_ids},
        leg_registry={n:l for n,l in p.leg_registry.items() if l.task_id in (1,2)})
    before=wrapper.joint.wrapper.polish.polish_groups
    result=wrapper.refine(p,base,deadline=perf_counter()+3.,weights=W,scales=S,seed=51,
        max_groups=1,beam_width=1,max_families=1,source_option_limit=1,receiver_option_limit=1,
        position_limit=3,snapshot_limit=3,polish_max_iterations=2,alns_module=real_weighted_runtime,
        focus_position_limit=3,focus_option_count=2,focus_donor_limit=1,focus_max_donors=1)
    m=result['metadata'];assert m['seed_records'] and m['polish_records']
    leaf=m['polish_records'][0];nm=leaf['metadata'];stage=nm['focused_stage'];fm=stage['metadata']
    assert fm['initialization_completed'] and nm['initialization_completed'] and nm['iterations']==2
    assert stage['focus_deadline_perf_counter']==stage['focus_started_at_perf_counter']+min(20.,
        max(0.,leaf['deadline_perf_counter']-stage['focus_started_at_perf_counter'])*.4)
    assert stage['native_input_routes_sha256']==fm['post_repair_snapshot']['routes_sha256']
    assert nm['input_snapshot']['routes_sha256']==stage['native_input_routes_sha256']
    assert nm['started_at_perf_counter']>=fm['started_at_perf_counter']+fm['runtime_seconds']
    assert result['evaluation'].valid and m['final_key']<=m['initial_key']
    assert m['damage_extension']['restored'] and m['extension']['restored']
    assert wrapper.joint.wrapper.polish.polish_groups is before and not wrapper._active
    assert not real_weighted_runtime._weighted_adapter.active
    from algorithm.experiments.audit_new_late_set_native_v1 import _audit_metadata
    tasks,graph=evidence(p)
    params={**m['configuration']['raw_parameters'],'raw_fraction':.4,'polish_max_iterations':2,
        'polish_initial_temperature':.0025,'polish_minimum_temperature':.00001,'polish_cooling_rate':.97,
        **m['damage_extension']['parameters']}
    manifest=dict(weights=W,scales=S,method_parameters={'multi_native_relay':params},
        accumulation_fixed_catalogue_parameters=dict(feeder_limit=3,base_pool_limit=64))
    assert _audit_metadata(tasks,graph,base,m,relay=True,manifest=manifest)['passed']
