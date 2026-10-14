"""Paid raw seeds, genuine pooled channel, native joint repair and leaf guards."""
from copy import deepcopy
from dataclasses import replace
from time import perf_counter
import json

import pytest
from uav_dispatch.relay import GlobalRelayEvaluator
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import window_damage_repair as method
from algorithm.tests.test_deadline_window_repooling import fixture,W,S
from algorithm.tests.test_relay_repooling_exchange import topology
from algorithm.tests.test_relay_damage_closure import fixture as eight_fixture


def run(p,base,**kwargs):
    return method.refine(p,base,deadline=perf_counter()+4.,weights=W,scales=S,max_families=1,
        base_pool_limit=8,max_rounds=1,closure_limit=8,position_limit=8,**kwargs)


def audit(p,base,result,arm):
    from algorithm.experiments.audit_window_damage_repair import audit_window_damage_repair_proposal,audit_window_damage_closure,audit_window_damage_partial
    from algorithm.experiments.audit_weighted_relay_ablation import audit_payload
    from algorithm.tests.test_audit_mobile_relay import canonical_payload
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in p.tasks};graph=topology(p)
    graph['task_relay_candidates']={str(t):list(p.task_relay_candidates.get(t,())) for t in p.task_ids}
    for record in result['metadata']['polish_records']:
        h=record['helper_metadata'];snapshots=[record['post_polish']]
        snapshots += [h.get(name) for name in ('input_snapshot','post_repair_snapshot','first_complete_candidate','best_constructed_candidate')]
        snapshots += h['full_candidate_snapshots']
        for c in h['closure_records']:
            proof=audit_window_damage_closure(tasks,graph,c['before_routes'],c['closure'],pinned_task_ids=h['pinned_task_ids'])
            assert proof['passed']
            for layer in c['layers']:
                for partial in layer['beam_state_snapshots']:
                    proof=audit_window_damage_partial(tasks,graph,partial['routes'],layer['present_task_ids'],telemetry=partial,
                        weights=W,scales=S,target_final_counts=c['closure']['raw_final_counts'])
                    assert proof['passed']
            snapshots += [point['full_snapshot'] for point in c['complete_checkpoints']]
        for snapshot in filter(None,snapshots):
            proof=audit_window_damage_repair_proposal(tasks,graph,base,record['macro_routes'],snapshot['routes'],
                record['original_macro_move'],relay_enabled=arm,telemetry=snapshot,weights=W,scales=S)
            assert proof['passed']
            assert snapshot['move']=={'semantic':'post_window_damage_repair'} and 'table_score' not in snapshot and 'window' not in snapshot
            audit_payload(tasks,graph,canonical_payload(tasks,graph,snapshot['routes'],{}))


@pytest.mark.parametrize('arm',[False,True])
def test_real_paid_pipeline_all_current_evidence_and_independent_raw_to_post_audit(arm):
    p,base=fixture();before=topology(p);result=run(p,base,relay_enabled=arm);m=result['metadata']
    assert result['evaluation'].valid and m['selector_completed'] and m['seed_selection_completed']
    assert 1<=m['polish_calls']<=2 and m['seed_selection_records']
    assert all(s['pooled_rescued_task_ids'] for s in m['seed_selection_records'])
    assert all(r['helper_metadata']['complete_valid_candidates']>0 for r in m['polish_records'])
    assert all(r['finished_within_leaf_deadline'] for r in m['polish_records'])
    assert result['metadata']['final_key']<=result['metadata']['initial_key']
    assert 'problem' not in result and topology(p)==before
    audit(p,base,result,arm);json.dumps(m,allow_nan=False)


def test_both_arms_keep_shared_selector_same_policy_and_native_freedom():
    p,base=fixture();d=run(p,base,relay_enabled=False);r=run(p,base)
    dm,rm=d['metadata'],r['metadata']
    assert dm['selected_families']==rm['selected_families'] and dm['selector_sha256']==rm['selector_sha256']
    assert dm['configuration']==rm['configuration']
    assert dm['configuration']['genuine_pooled_channel_has_no_J_margin_gate']


def expensive_genuine_raw():
    p,base,raw=eight_fixture()
    p=replace(p,tasks=tuple(replace(t,deadline_min={1:15.,2:15.,9:7.5,10:10.}.get(t.id,t.deadline_min)) for t in p.tasks))
    base=((5,-5,6,-6,1,-1),(7,-7,8,-8,2,-2),base[2],*base[3:])
    f=dict(i=1,j=2,a=0,b=1,c=2,k1=3,k2=4,station_id=1,task_ids=[1,2,3,4]);move=dict(family=f)
    e=evaluate_solution(p,raw,_force_full=True);scales=(15.,100.,1000.)
    snapshot=dict(routes=[list(r) for r in raw],move=move,key=list(method.raw_method.weighted_key(e.score,W,scales)))
    metadata=dict(input_snapshot={},selected_families=[f],selector_sha256='controlled_seed_fixture',selector_completed=True,
        selector_completed_at_perf_counter=0.,full_candidate_snapshots=[snapshot])
    fake=dict(routes=base,evaluation=evaluate_solution(p,base),iterations=0,metadata=metadata)
    return p,base,raw,scales,fake


def test_genuine_seed_above_old_margin_is_explored_but_worse_raw_is_not_adopted(monkeypatch):
    p,base,raw,scales,fake=expensive_genuine_raw();calls=[]
    initial=method.raw_method.weighted_key(evaluate_solution(p,base).score,W,scales)
    rawkey=method.raw_method.weighted_key(evaluate_solution(p,raw).score,W,scales)
    assert rawkey[0]-initial[0]>.015
    monkeypatch.setattr(method.raw_method,'refine',lambda *a,**k:dict(fake,metadata=deepcopy(fake['metadata'])))
    def fallback(p,b,r,**kwargs):
        calls.append(kwargs)
        return dict(routes=r,evaluation=evaluate_solution(p,r),iterations=0,
            metadata=dict(returned_checkpoint_checked_within_deadline=True))
    monkeypatch.setattr(method.repair,'repair_damage_closure',fallback)
    result=method.refine(p,base,deadline=perf_counter()+2.,weights=W,scales=scales)
    assert calls and result['metadata']['seed_selection_records'][0]['pooled_rescued_task_ids']==[1,2]
    assert result['routes']==base and not result['metadata']['accepted_moves']
    assert result['metadata']['polish_records'][0]['rejection_reason']=='not_better_than_guarded_strong_input'


def test_helper_overrun_and_modified_pin_cannot_replace_valid_original_guard(monkeypatch):
    p,base,raw,scales,fake=expensive_genuine_raw();clock=[perf_counter()];deadline=clock[0]+2.
    monkeypatch.setattr(method.raw_method,'refine',lambda *a,**k:dict(fake,metadata=deepcopy(fake['metadata'])))
    monkeypatch.setattr(method,'perf_counter',lambda:clock[0])
    def overrun(p,b,r,**kwargs):
        clock[0]=deadline+.1
        return dict(routes=r,iterations=0,metadata=dict(returned_checkpoint_checked_within_deadline=True))
    monkeypatch.setattr(method.repair,'repair_damage_closure',overrun)
    result=method.refine(p,base,deadline=deadline,weights=W,scales=scales)
    assert result['routes']==base and result['metadata']['deadline_reached'] and not result['metadata']['accepted_moves']


def test_expired_or_no_genuine_seed_returns_complete_guard_without_repair():
    p,base=fixture();expired=method.refine(p,base,deadline=perf_counter()-1.,weights=W,scales=S)
    assert expired['routes']==base and expired['evaluation'].valid and expired['metadata']['polish_calls']==0
    p=replace(p,tasks=tuple(replace(t,deadline_min=100.) for t in p.tasks))
    no=run(p,base)
    assert no['routes']==base and not no['metadata']['seed_selection_records'] and no['metadata']['polish_calls']==0


@pytest.mark.parametrize('change',[dict(proposal_fraction=1.),dict(candidate_limit=3),dict(guard_reserve_fraction=1.)])
def test_invalid_wrapper_budget_rejected(change):
    p,base=fixture()
    with pytest.raises(ValueError):run(p,base,**change)
