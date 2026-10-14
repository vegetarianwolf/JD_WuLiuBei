"""Nine-task weighted ALNS witnesses; no official input or optimizer run."""
from dataclasses import replace
import json
from random import Random
from time import perf_counter

import pytest

from uav_dispatch import Point, Task, TransportLeg, RelayStation
from uav_dispatch import alns
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import six_task_preserving_polish as polish
from algorithm.experiments import weighted_direct_objective as weighted
from algorithm.tests.test_six_task_route_costs import fixture as triangle
from algorithm.tests.test_relay_preserving_polish import (
    fake_module, real_weighted_runtime, relay_config, native_repair)


W, S = (.5,.25,.25), (9.,100.,100.)
PINS = tuple(range(1,7))


def fixture(*, direct=False):
    """Six sorting pins plus three badly owned native tasks, K=3 saturated."""
    p=triangle()
    native=tuple(Task(7+a,p.drone_homes[(a+1)%3],p.drone_homes[(a+1)%3],2.6)
                 for a in range(3))
    p=replace(p,tasks=p.tasks+native,max_tasks_per_drone=3)
    incoming=((102,104,-102,-104),(106,108,-106,-108),(110,112,-110,-112))
    outgoing=((107,111,-107,-111),(103,113,-103,-113),(105,109,-105,-109))
    if direct:
        core=((3,5,-3,-5),(1,6,-1,-6),(2,4,-2,-4))
    else:
        core=tuple(incoming[a]+outgoing[a] for a in range(3))
    routes=tuple(core[a]+(7+a,-(7+a)) for a in range(3))
    assert evaluate_solution(p,routes,_force_full=True).valid
    return p,routes


def context(module, problem, routes, *, direct=False, stats=None, deadline=None):
    return polish.pinned_search(module,problem,routes,
        deadline=perf_counter()+2. if deadline is None else deadline,
        weights=W,scales=S,pinned_task_ids=PINS,
        required_cross_task_ids=() if direct else PINS,statistics=stats)


def call_polish(module,p,r,*,direct=False,deadline=None,config=None):
    checked=evaluate_solution(p,r,_force_full=True)
    return polish.polish_pinned_macro(p,r,deadline=perf_counter()+2. if deadline is None else deadline,
        weights=W,scales=S,seed=19,pinned_task_ids=PINS,
        required_cross_task_ids=() if direct else PINS,
        comparison_key=polish.mobile.weighted_key(checked.score,W,S),
        config=config,alns_module=module)


def assert_snapshot(p,snapshot):
    routes=tuple(tuple(r) for r in snapshot['routes'])
    full=GlobalRelayEvaluator(p).evaluate(routes)
    assert full.feasible
    assert snapshot['post_global_score']==snapshot['score']==dict(
        late_count=full.score.late_count,total_lateness_min=full.score.total_lateness_min,distance_km=full.score.distance_km)
    assert snapshot['event_times_min']==[[full.event_times_min[(a,k)] for k in range(len(r))]
                                         for a,r in enumerate(routes)]
    assert snapshot['delivery_times_min']=={str(t):v for t,v in full.delivery_times_min.items()}
    assert snapshot['pin_scope']=='six_task_sorting' and snapshot['pinned_task_ids']==list(PINS)
    assert 'table_score' not in snapshot and 'topology' not in snapshot
    assert len(snapshot['actual_final_owners'])==9
    assert set(snapshot['current_shared_double_loaded_km_by_aircraft'])=={'0','1','2'}
    index=build_plan_index(p,routes)
    drops={str(t):full.event_times_min[(index.inbound_route_by_task[t],routes[index.inbound_route_by_task[t]].index(
        -next(i for i in index.task_plans[t].leg_ids if p.leg_registry[i].kind=='RELAY_IN')))]
        for t in index.relay_task_ids}
    assert snapshot['current_source_drop_availability_min']==drops


@pytest.mark.parametrize('direct',[False,True])
def test_full_nine_task_native_repair_may_reassign_without_moving_any_six_plan(direct):
    p,r=fixture(direct=direct);stats={};deadline=perf_counter()+2.
    old={name:getattr(alns,name) for name in polish.PATCH_NAMES}
    with context(alns,p,r,direct=direct,stats=stats,deadline=deadline):
        partial,removed=alns.destroy_solution(p,RouteEvaluator(p),r,'random',3,Random(19))
        assert set(removed)=={7,8,9}
        candidate=native_repair(alns,p,partial,removed,deadline)
        assert evaluate_solution(p,candidate,_force_full=True).valid
        assert polish._signature(p,candidate,PINS)==polish._signature(p,r,PINS)
        before=build_plan_index(p,r).delivery_route_by_task
        after=build_plan_index(p,candidate).delivery_route_by_task
        assert any(before[t]!=after[t] for t in (7,8,9))
        assert stats['direct_exact_global_evaluations']>0 and stats['complete_valid_candidates']==1
        assert_snapshot(p,stats['first_complete_candidate'])
    assert all(getattr(alns,n) is f for n,f in old.items()) and stats['patches_restored']


@pytest.mark.parametrize('direct',[False,True])
def test_real_weighted_loop_actual_three_free_tasks_and_cold_configuration(real_weighted_runtime,direct):
    module=real_weighted_runtime;p,r=fixture(direct=direct)
    original={n:getattr(module,n) for n in polish.PATCH_NAMES}
    score_comparators={n:getattr(module.Score,n) for n in ('__lt__','__le__','__gt__','__ge__')}
    cfg=module.ALNSConfig(max_iterations=2,relay_min_destroy_fraction=.34,
        relay_max_destroy_fraction=.34,repair_rank_candidate_limit=12,
        repair_exact_candidate_limit=12,direct_exact_top_k=4,
        initial_temperature=.0025,minimum_temperature=.00001,cooling_rate=.97)
    result=call_polish(module,p,r,direct=direct,config=cfg)
    stats=result['metadata']
    assert result['evaluation'].valid and result['iterations']==2
    assert stats['destroy_calls']==stats['repair_calls']==stats['complete_valid_candidates']==2
    assert stats['direct_plan_calls']>0 and stats['direct_exact_global_evaluations']>0
    before=build_plan_index(p,r).delivery_route_by_task
    after=build_plan_index(p,result['routes']).delivery_route_by_task
    assert any(before[t]!=after[t] for t in (7,8,9))
    assert polish._signature(p,result['routes'],PINS)==polish._signature(p,r,PINS)
    assert stats['configuration']['initial_temperature']==.0025
    assert stats['configuration']['minimum_temperature']==.00001
    assert stats['configuration']['cooling_rate']==.97
    assert stats['configuration']['max_iterations']==2 and not stats['configuration']['enable_budget_cooling']
    assert stats['patches_restored'] and stats['real_loop_globals_preflight_checked']
    assert all(getattr(module,n) is f for n,f in original.items())
    assert all(getattr(module.Score,n) is f for n,f in score_comparators.items())
    assert not weighted.active and not polish._active
    for field in ('input_snapshot','first_complete_candidate','best_constructed_candidate','post_pin_snapshot'):
        assert_snapshot(p,stats[field])
    for saved in stats['full_candidate_snapshots']:assert_snapshot(p,saved)
    json.dumps(stats,allow_nan=False)


@pytest.mark.parametrize('where',['IN_pickups','first_IN_loaded','IN_drops','OUT_deliveries'])
def test_native_interleaving_is_allowed_without_freezing_raw_four_event_blocks(where):
    p,r=fixture();core=r[0][:-2]
    if where=='IN_pickups':changed=core[:1]+(7,-7)+core[1:]
    elif where=='first_IN_loaded':changed=(7,core[0],-7)+core[1:]
    elif where=='IN_drops':changed=core[:3]+(7,-7)+core[3:]
    else:changed=core[:7]+(7,-7)+core[7:]
    candidate=(changed,*r[1:])
    checked,shape=polish._validate(p,candidate,PINS,polish._signature(p,r,PINS),set(PINS))
    assert checked.valid
    saved=polish._snapshot(p,candidate,checked,W,S,phase='tiny_interleave',shape=shape)
    assert_snapshot(p,saved)
    if where=='IN_drops':
        assert saved['current_source_drop_availability_min']['2']>saved['current_source_drop_availability_min']['1']
    if where in ('IN_pickups','first_IN_loaded','IN_drops'):
        assert GlobalRelayEvaluator(p).evaluate(candidate).total_waiting_min>0.


@pytest.mark.parametrize('kind',['conversion','source','order','take_gap','quota','capacity','cycle','missing','extra_relay'])
def test_bad_complete_repair_rolls_back_without_acceptance_rng_or_reward(kind):
    p,r=fixture();candidate=list(r)
    if kind=='conversion':
        candidate[0]=tuple(v for v in candidate[0] if abs(v)!=102)
        candidate[1]=tuple(1 if v==103 else -1 if v==-103 else v for v in candidate[1])
    elif kind=='source':
        candidate[0]=tuple(v for v in candidate[0] if abs(v)!=102)
        candidate[1]=(102,-102)+candidate[1]
    elif kind=='order':candidate[0]=candidate[0][:6]+(-111,-107)+candidate[0][8:]
    elif kind=='take_gap':candidate[0]=candidate[0][:5]+(7,-7)+candidate[0][5:8]
    elif kind=='quota':
        p=replace(p,max_tasks_per_drone=4);candidate[0]=candidate[0][:-2];candidate[1]+=(7,-7)
        assert evaluate_solution(p,tuple(candidate),_force_full=True).valid
    elif kind=='capacity':candidate[0]=candidate[0][:4]+(7,)+candidate[0][4:8]+(-7,)
    elif kind=='cycle':candidate=[route[4:8]+route[:4]+route[8:] for route in candidate]
    elif kind=='missing':candidate[0]=candidate[0][:-1]
    else:
        legs=dict(p.leg_registry);legs[120]=TransportLeg(120,7,'RELAY_IN',9);legs[121]=TransportLeg(121,7,'RELAY_OUT',9)
        p=replace(p,leg_registry=legs,task_relay_candidates={**p.task_relay_candidates,7:(9,)})
        candidate[0]=candidate[0][:-2]+(120,-120)
        candidate[1]=candidate[1]+(121,-121)
    candidate=tuple(candidate)
    module=fake_module(destroy=lambda *a,**k:((),(7,8)),repair=lambda *a,**k:candidate)
    stats={}
    with context(module,p,r,stats=stats):
        partial,removed=module.destroy_solution(p,RouteEvaluator(p),r,'random',2,Random(3))
        assert module._repair(p,None,partial,removed,'greedy',20,deadline=None)==r
        score=evaluate_solution(p,r,_force_full=True).score;rng=Random(8);state=rng.getstate()
        assert not module._accept_worse(score,score,p,1.,rng) and rng.getstate()==state
        assert stats['rollback_candidates']==stats['rejected_acceptances']==1


def test_true_station_leg_change_and_zero_shared_flight_are_rejected():
    p,r=fixture();legs=dict(p.leg_registry)
    legs.update({501:TransportLeg(501,1,'RELAY_IN',10),502:TransportLeg(502,1,'RELAY_OUT',10)})
    q=replace(p,relay_stations=p.relay_stations+(RelayStation(10,1.,.5),),leg_registry=legs,
        task_relay_candidates={**p.task_relay_candidates,1:(9,10)})
    changed=tuple(tuple(501 if v==102 else -501 if v==-102 else 502 if v==103 else -502 if v==-103 else v for v in route) for route in r)
    assert evaluate_solution(q,changed,_force_full=True).valid
    with pytest.raises(ValueError,match='pinned modes'):
        polish._validate(q,changed,PINS,polish._signature(q,r,PINS),set(PINS))
    h=Point(p.relay_stations[0].x,p.relay_stations[0].y)
    zero=replace(p,tasks=tuple(replace(t,delivery=h) if t.id in (3,5) else t for t in p.tasks))
    assert evaluate_solution(zero,r,_force_full=True).valid
    with pytest.raises(ValueError,match='positive-distance'):
        polish._shape(zero,r,PINS)


def test_real_loop_shape_rollback_has_no_sa_acceptance_or_operator_credit(real_weighted_runtime,monkeypatch):
    module=real_weighted_runtime;p,r=fixture();bad=(r[0][:5]+(7,-7)+r[0][5:8],*r[1:])
    assert evaluate_solution(p,bad,_force_full=True).valid
    module.__dict__['_tiny_bad']=bad
    exec('def _tiny_bad_repair(*args,**kwargs):\n    return _tiny_bad\n',module.__dict__)
    monkeypatch.setattr(module,'_repair',module._tiny_bad_repair)
    result=call_polish(module,p,r,config=module.ALNSConfig(max_iterations=3,
        relay_min_destroy_fraction=.34,relay_max_destroy_fraction=.34))
    stats=result['metadata']
    assert result['routes']==r and stats['rollback_candidates']==stats['rejected_acceptances']==3
    assert stats['alns_summary']['accepted_solutions']==0
    assert all(v['accepted']==0 and v['total_reward']==0. for v in stats['alns_summary']['operator_statistics'].values())


def test_nested_exception_expired_and_foreign_globals_restore_everything(real_weighted_runtime,monkeypatch):
    p,r=fixture();module=fake_module();old={n:getattr(module,n) for n in polish.PATCH_NAMES}
    with pytest.raises(RuntimeError,match='witness'):
        with context(module,p,r):
            with pytest.raises(RuntimeError,match='nested'):
                with context(module,p,r):pass
            raise RuntimeError('witness')
    assert all(getattr(module,n) is f for n,f in old.items()) and not polish._active
    module.solve_alns=lambda *a,**k:pytest.fail('expired helper must not run solver')
    result=call_polish(module,p,r,deadline=perf_counter()-1.)
    assert result['routes']==r and result['metadata']['skipped_reason']=='expired_absolute_deadline'
    assert not result['metadata']['returned_checkpoint_checked_within_deadline']
    real=real_weighted_runtime
    monkeypatch.setattr(real,'_repair',lambda *a,**k:r)
    old={n:getattr(real,n) for n in polish.PATCH_NAMES}
    with pytest.raises(ValueError,match='same ALNS module'):call_polish(real,p,r)
    assert all(getattr(real,n) is f for n,f in old.items()) and not weighted.active


def test_snapshot_clock_mismatch_and_incomplete_pin_set_cannot_be_used():
    p,r=fixture();checked=evaluate_solution(p,r,_force_full=True)
    _,shape=polish._validate(p,r,PINS,polish._signature(p,r,PINS),PINS)
    forged=replace(checked,score=replace(checked.score,distance_km=checked.score.distance_km+1.))
    with pytest.raises(ValueError,match='clocks disagree'):
        polish._snapshot(p,r,forged,W,S,phase='forged',shape=shape)
    for pins,cross in ((PINS[:2],PINS[:2]),(PINS,PINS[:2]),(PINS,(*PINS,7))):
        with pytest.raises(ValueError,match='exactly six'):
            with polish.pinned_search(alns,p,r,deadline=perf_counter()+1.,weights=W,scales=S,
                pinned_task_ids=pins,required_cross_task_ids=cross):pass


@pytest.mark.parametrize('stage',['repair','full_check','snapshot'])
def test_any_new_checkpoint_must_finish_all_checks_and_clocks_before_deadline(monkeypatch,stage):
    p,r=fixture();clock=[0.];calls=[0]
    monkeypatch.setattr(polish,'perf_counter',lambda:clock[0])
    # Whole native rotation is a valid improving candidate, so an overrun must
    # reject an actual useful checkpoint rather than an already worse route.
    mapping={7:9,8:7,9:8}
    candidate=tuple(tuple((1 if v>0 else -1)*mapping[abs(v)] if abs(v) in mapping else v for v in route) for route in r)
    assert evaluate_solution(p,candidate,_force_full=True).score < evaluate_solution(p,r,_force_full=True).score
    def repair(*args,**kwargs):
        if stage=='repair':clock[0]=2.
        return candidate
    if stage=='full_check':
        real=polish.evaluate_solution
        def full(*args,**kwargs):
            value=real(*args,**kwargs);calls[0]+=1
            if calls[0]>1:clock[0]=2.
            return value
        monkeypatch.setattr(polish,'evaluate_solution',full)
    if stage=='snapshot':
        real=polish._snapshot
        def snapshot(*args,**kwargs):
            value=real(*args,**kwargs);calls[0]+=1
            if calls[0]>1:clock[0]=2.
            return value
        monkeypatch.setattr(polish,'_snapshot',snapshot)
    module=fake_module(destroy=lambda *a,**k:((),(7,8,9)),repair=repair)
    with context(module,p,r,deadline=1.) as state:
        partial,removed=module.destroy_solution(p,None,r,'random',3,Random(1))
        with pytest.raises(alns._SearchDeadlineReached):
            module._repair(p,None,partial,removed,'greedy',20)
        assert state['best_routes']==r and state['best_checked_at_perf_counter'] is None
    assert not polish._active


def test_exact_global_confirmation_overrun_is_never_returned(monkeypatch):
    p,r=fixture();clock=[0.];monkeypatch.setattr(polish,'perf_counter',lambda:clock[0])
    global_evaluator=GlobalRelayEvaluator(p);actual=global_evaluator.evaluate_delta
    def slow(*args,**kwargs):
        value=actual(*args,**kwargs);clock[0]=2.;return value
    monkeypatch.setattr(global_evaluator,'evaluate_delta',slow)
    partial=(r[0][:-2],*r[1:])
    option=alns.TaskInsertionOption(7,'DIRECT',None,polish.Score(0,0.,0.),
        direct_route_index=0,direct_route=r[0],direct_pickup_position=8,direct_delivery_position=9)
    with context(alns,p,r,deadline=1.):
        with pytest.raises(alns._SearchDeadlineReached):
            alns._apply_task_exact(p,RouteEvaluator(p),global_evaluator,partial,(option,),
                relay_cfg=relay_config(),deadline=1.)


def test_untrusted_solver_result_cannot_replace_checked_best_and_margin_does_not_accept_worse():
    from types import SimpleNamespace
    p,r=fixture();mapping={7:9,8:7,9:8}
    candidate=tuple(tuple((1 if v>0 else -1)*mapping[abs(v)] if abs(v) in mapping else v for v in route) for route in r)
    module=None
    def solve(p,config,initial_routes):
        partial,removed=module.destroy_solution(p,None,initial_routes,'random',3,Random(2))
        module._repair(p,None,partial,removed,'greedy',20)
        return SimpleNamespace(iterations=1,routes=((999,),))
    module=fake_module(destroy=lambda *a,**k:((),(7,8,9)),repair=lambda *a,**k:candidate,solve=solve)
    result=call_polish(module,p,r)
    assert result['routes']==candidate and result['metadata']['adoptable']
    assert result['metadata']['returned_checkpoint_checked_within_deadline']
    assert result['metadata']['returned_checkpoint_kind']=='new_predeadline_full_candidate'
    module.solve_alns=lambda *a,**k:pytest.fail('margin gate must not invoke search')
    key=polish.mobile.weighted_key(evaluate_solution(p,r,_force_full=True).score,W,S)
    gated=polish.polish_pinned_macro(p,r,deadline=perf_counter()+1.,weights=W,scales=S,
        seed=1,pinned_task_ids=PINS,required_cross_task_ids=PINS,comparison_key=(key[0]-.1,*key[1:]),
        proposal_margin=.015,alns_module=module)
    assert gated['routes']==r and not gated['metadata']['adoptable']
    assert gated['metadata']['skipped_reason']=='predeclared_weighted_proposal_margin'


def test_original_pin_selection_filtered_and_balanced_repair_slot_topups():
    p,r=fixture();stats={};module=fake_module(destroy=lambda *a,**k:((),PINS))
    with context(module,p,r,stats=stats):
        _,removed=module.destroy_solution(p,None,r,'random',3,Random(3))
        assert set(removed)=={7,8,9} and stats['selected_pins_filtered']==6 and stats['destroy_topups']==3
        current,removed=module.destroy_solution(p,None,r,'random',1,Random(3))
        assert current==r and removed==() and stats['skipped_no_balanced_destroy']==1
        assert module._repair(p,None,current,removed,'greedy',20)==r
        score=evaluate_solution(p,r,_force_full=True).score
        assert not module._accept_worse(score,score,p,1.,Random(1))
