"""Joint rescue evidence, unrestricted quota maps, and complete-clock guards."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from itertools import product
from time import perf_counter
import json

import pytest
from uav_dispatch import Point,Problem,RelayStation,Task,TransportLeg
from uav_dispatch.model import Score
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import deadline_window_repooling as method
from algorithm.tests.test_relay_repooling_exchange import topology

W,S=(.5,.25,.25),(9.,100.,100.)


def fixture():
    H=Point(0.,0.);A=Point(-4.,0.);B=Point(4.,0.)
    tasks=(Task(1,A,Point(0.,3.),9.),Task(2,B,Point(0.,4.),9.),
        Task(3,H,Point(2.,0.),7.),Task(4,H,Point(2.,0.),7.),
        Task(5,H,Point(-2.,0.),7.),Task(6,H,Point(-2.,0.),7.),
        Task(7,H,H,100.),Task(8,H,H,100.),Task(9,H,H,100.))
    registry={k:TransportLeg(k,t,kind,1) for t in (1,2)
        for k,kind in ((100+2*t,'RELAY_IN'),(101+2*t,'RELAY_OUT'))}
    p=Problem(tasks,drone_count=3,capacity=2,max_tasks_per_drone=3,speed_km_per_min=.9,
        drone_homes=(A,B,H),relay_stations=(RelayStation(1,0.,0.),),leg_registry=registry,
        task_relay_candidates={t.id:((1,) if t.id in (1,2) else ()) for t in tasks})
    base=((3,4,-3,-4,1,-1),(5,6,-5,-6,2,-2),(7,8,-7,-8,9,-9))
    assert evaluate_solution(p,base,_force_full=True).valid
    return p,base


def run(p,base,**kwargs):
    return method.refine(p,base,deadline=perf_counter()+4.,weights=W,scales=S,**kwargs)


def audit(p,base,result,arm):
    from algorithm.experiments.audit_deadline_window_repooling import audit_deadline_window_repooling_proposal
    from algorithm.experiments.audit_weighted_relay_ablation import audit_payload
    from algorithm.tests.test_audit_mobile_relay import canonical_payload
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in p.tasks}
    graph=topology(p);graph['task_relay_candidates']={str(t):list(p.task_relay_candidates.get(t,())) for t in p.task_ids}
    for s in result['metadata']['full_candidate_snapshots']:
        proof=audit_deadline_window_repooling_proposal(tasks,graph,base,s['routes'],s['move'],
            relay_enabled=arm,telemetry=s,weights=W,scales=S)
        assert proof['passed']
        audit_payload(tasks,graph,canonical_payload(tasks,graph,s['routes'],{}))


def test_real_selected_rescue_with_native_wait_clocks_beats_complete_twelve_map_control():
    p,base=fixture();before=topology(p)
    direct=run(p,base,relay_enabled=False);relay=run(p,base)
    dm,rm=direct['metadata'],relay['metadata']
    assert dm['selector_completed'] and rm['selector_completed']
    assert dm['selected_families']==rm['selected_families'] and dm['selector_sha256']==rm['selector_sha256']
    assert dm['selector_sha256']==sha256(json.dumps(dm['selected_families'],sort_keys=True,separators=(',',':')).encode()).hexdigest()
    assert relay['evaluation'].score.late_count==0
    assert relay['metadata']['final_cross_uav_count']==2
    assert relay['metadata']['final_key']<direct['metadata']['final_key']
    assert relay['metadata']['final_key']<relay['metadata']['initial_key']
    assert any(s['selected_rescued_task_ids']==[1,2] and not s['native_newly_late_task_ids'] for s in rm['full_candidate_snapshots'])
    for record in dm['family_records']:
        assert record['direct_map_enumeration_completed']
        assert len(record['direct_maps_considered'])==len(record['direct_maps_completed'])==12
        assert len({json.dumps(m) for m in record['direct_maps_completed']})==12
    for family in rm['selected_families']:
        preview=family['selector_window'];assert not preview['preview_complete_solution']
        assert preview['optimistic_missing_comp'] and preview['receiver_quota_overflow']==2
        assert preview['window']['source_availability_min']=={'1':pytest.approx(4/.9),'2':pytest.approx(4/.9)}
    assert topology(p)==before and 'problem' not in relay and 'problem' not in direct
    for result,arm in ((direct,False),(relay,True)):
        audit(p,base,result,arm);json.dumps(result['metadata'],allow_nan=False)
        for accepted in result['metadata']['accepted_moves']:
            assert accepted['full_verified_at_perf_counter']<accepted['accepted_at_perf_counter']<result['metadata']['deadline_perf_counter']
            assert accepted['move']['family'] in result['metadata']['selected_families']


def test_no_current_late_and_ineligible_partner_return_full_guard_without_fake_families():
    p,base=fixture();p=replace(p,tasks=tuple(replace(t,deadline_min=100.) for t in p.tasks))
    result=run(p,base)
    assert result['metadata']['selector_completed'] and result['metadata']['selector_late_anchors']==0
    assert result['metadata']['selected_families']==[] and result['routes']==base
    p,base=fixture();p=replace(p,task_relay_candidates={1:(1,),2:(),**{t:() for t in range(3,10)}})
    result=run(p,base)
    assert result['metadata']['selected_families']==[] and result['evaluation'].valid


def test_joint_front_door_keeps_partner_outside_both_old_destination_three_neighbour_gates():
    p,base=fixture();tasks=tuple(replace(t,delivery=Point(0.,3.+.01*(t.id-6))) if t.id>=7 else t for t in p.tasks)
    p=replace(p,tasks=tasks)
    owners={t:a for a,route in enumerate(base) for t in route if t>0}
    for i,j in ((1,2),(2,1)):
        neighbours=sorted((t for t in p.task_ids if owners[t]!=owners[i]),key=lambda t:(p.task(i).delivery.distance_to(p.task(t).delivery),t))[:3]
        assert j not in neighbours
    result=run(p,base,max_families=1)
    assert result['metadata']['selected_families'][0]['i']==1
    assert result['metadata']['selected_families'][0]['j']==2
    audit(p,base,result,True)


def test_actual_native_preview_penalty_changes_receiver_before_family_limit():
    p,base=fixture();H=Point(0.,0.);D=Point(0.,5.)
    tasks=tuple(replace(t,delivery=D,deadline_min=6. if t.id in (7,8) else 100.) if t.id>=7 else t for t in p.tasks)
    tasks+=tuple(Task(t,H,D,100.) for t in (10,11,12))
    p=replace(p,tasks=tasks,drone_count=4,drone_homes=(*p.drone_homes,H),
        task_relay_candidates={**p.task_relay_candidates,10:(),11:(),12:()})
    base=(*base,(10,11,-10,-11,12,-12))
    result=run(p,base,max_families=1)
    f=result['metadata']['selected_families'][0]
    assert f['c']==3
    assert f['selector_window']['preview_classifications']['native_newly_late_task_ids']==[]
    assert result['metadata']['configuration']['proxy_is_gain_or_lower_bound'] is False
    audit(p,base,result,True)


def test_actual_compensated_sources_receive_joint_latest_safe_bounds(monkeypatch):
    p,base=fixture();source=method.costs.source_options;calls=[]
    def capture(*args,**kwargs):
        calls.append(kwargs)
        return source(*args,**kwargs)
    monkeypatch.setattr(method.costs,'source_options',capture)
    result=run(p,base,max_families=1)
    assert calls and all(call['option_limit']==3 for call in calls)
    assert any(call.get('optimistic_missing_comp') for call in calls)
    complete=[call for call in calls if call.get('compensation_task_id') is not None]
    assert len(complete)==2 and all(1<=len(call['latest_drop_bounds'])<=4 for call in complete)
    bound=result['metadata']['selected_families'][0]['selector_window']['window']['joint_latest_second_take_min']
    assert all(call['latest_drop_bounds'][0]==bound for call in complete)


def test_front_door_joint_B_uses_actual_middle_source_not_only_two_extremes():
    from algorithm.tests.test_deadline_window_costs import fixture as source_fixture
    p=source_fixture();H=Point(0.,0.)
    p=replace(p,max_tasks_per_drone=4,tasks=tuple(
        replace(t,pickup=H,delivery=Point(-1.,0.),deadline_min=1.) if t.id==3 else
        replace(t,pickup=H,delivery=Point(-2.,0.),deadline_min=2.) if t.id==4 else t for t in p.tasks))
    args=dict(deadline=perf_counter()+1.,weights=W,scales=S,option_limit=3,position_limit=8)
    left=method.costs.source_options(p,(3,4,-3,-4,5,-5),0,1,9,latest_drop_bounds=(6.5,),**args)
    right=method.costs.source_options(p,(),1,2,9,latest_drop_bounds=(6.5,),**args)
    assert sorted(r['in_drop_time_min'] for r in left)==[2.,6.,22.]
    joined=method._source_pairs(left,right,[((1,),6.5)],W,S)
    assert any(a['in_drop_time_min']==6. and b['in_drop_time_min']<=6.5 for a,b in joined)
    safe=[(a,b) for a,b in joined if max(a['in_drop_time_min'],b['in_drop_time_min'])<=6.5]
    assert min(a['score'].late_count for a,b in safe)==1


def test_selected_rescue_channel_survives_better_J_and_native_only_N_representatives():
    p,base=fixture();original={t.id:(20. if t.id in (1,2) else 0.) for t in p.tasks}
    def candidate(name,score,clock):
        return dict(routes=((name,),),score=score,records=[dict(delivery_times={1:clock,2:30.})])
    jbest=candidate(1,Score(2,1.,1.),20.)
    nbest=candidate(2,Score(1,2.,2.),20.)
    rescue=candidate(3,Score(2,5.,5.),8.)
    kept=method._retain([jbest,nbest,rescue],3,p,original,{1,2},W,S)
    assert {c['routes'] for c in kept}=={jbest['routes'],nbest['routes'],rescue['routes']}


def test_full_direct_output_equals_independent_twelve_table_map_enumeration():
    p,base=fixture();result=run(p,base,relay_enabled=False,max_families=1)
    f=result['metadata']['selected_families'][0];selected=set(f['task_ids'])
    native=tuple(method._remove(r,selected) for r in base);original=evaluate_solution(p,base).delivery_times_min
    args=dict(deadline=perf_counter()+3.,weights=W,scales=S,option_limit=3,original_delivery_times=original)
    singles={(a,t):method.costs.direct_single_options(p,native[a],a,t,position_limit=8,**args)
        for a in (f['a'],f['b']) for t in f['task_ids']}
    pairs={pair:method.costs.direct_pair_options(p,native[f['c']],f['c'],pair,gap_limit=4,**args)
        for pair in __import__('itertools').combinations(f['task_ids'],2)}
    keys=[]
    for i in f['task_ids']:
        for j in f['task_ids']:
            if i==j:continue
            pair=tuple(t for t in f['task_ids'] if t not in (i,j))
            for records in product(singles[f['a'],i],singles[f['b'],j],pairs[pair]):
                score=sum((r['score'] for r in records),Score(0,0.,0.))
                keys.append(method.weighted_key(score,W,S))
    assert min(tuple(s['key']) for s in result['metadata']['full_candidate_snapshots'])==min(keys)
    assert result['metadata']['direct_final_maps_examined']==12


def test_selector_digest_overrun_and_full_snapshot_overrun_never_adopt(monkeypatch):
    p,base=fixture();digest=method._digest
    def slow(records):
        value=digest(records)
        if records:clock[0]=41.
        return value
    clock=[0.];monkeypatch.setattr(method,'perf_counter',lambda:clock[0]);monkeypatch.setattr(method,'_digest',slow)
    result=method.refine(p,base,deadline=40.,weights=W,scales=S)
    assert not result['metadata']['selector_completed'] and result['routes']==base
    monkeypatch.setattr(method,'_digest',digest);clock[0]=0.;snapshot=method._snapshot
    def late(*args,**kwargs):
        answer=snapshot(*args,**kwargs)
        if kwargs['phase']=='full_proposal':clock[0]=41.
        return answer
    monkeypatch.setattr(method,'_snapshot',late)
    result=method.refine(p,base,deadline=40.,weights=W,scales=S)
    assert result['metadata']['deadline_reached'] and not result['metadata']['accepted_moves']
    assert result['routes']==base and not result['metadata']['full_candidate_snapshots']


def test_expired_budget_never_creates_partial_final_or_selector_prefix():
    p,base=fixture();result=method.refine(p,base,deadline=perf_counter()-1.,weights=W,scales=S)
    assert result['evaluation'].valid and result['routes']==base
    assert not result['metadata']['selector_completed'] and not result['metadata']['selected_families']


@pytest.mark.parametrize('change',[dict(max_families=0),dict(base_pool_limit=1),dict(table_option_limit=4),dict(sigma=-1.)])
def test_invalid_bounds_rejected_before_search(change):
    p,base=fixture()
    with pytest.raises(ValueError):run(p,base,**change)
