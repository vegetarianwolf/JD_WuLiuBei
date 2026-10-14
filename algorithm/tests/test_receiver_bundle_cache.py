"""Whole-receiver memo equivalence; only tiny synthetic routes are used."""
from dataclasses import replace
import json
from time import perf_counter
from types import SimpleNamespace

import pytest

from uav_dispatch.search import RouteEvaluator
from uav_dispatch.relay import GlobalRelayEvaluator

from algorithm.experiments import receiver_bundle_cache as bundle
from algorithm.experiments import relay_repooling_exchange as repool
from algorithm.experiments import joint_mobile_layouts as joint
from algorithm.experiments import source_frontier_cache as frontier
from algorithm.experiments import mobile_repooling_refinement as mobile
from algorithm.experiments.audit_mobile_repooling import audit_mobile_repooling_routes
from algorithm.tests.test_joint_mobile_layouts import early_fixture,WEIGHTS,SCALES
from algorithm.tests.test_mobile_repooling_refinement import mobile_fixture


def pstats():
    return dict(receiver_relay_layouts=0,receiver_local_evaluations=0,direct_insertion_calls=0,deadline_reached=False)


def state_for(base,group,*,other=False,receiver=None):
    routes=[tuple(v for v in r if abs(v) not in (group.i,group.j,group.k1,group.k2)) for r in base]
    if other:routes[group.a]=tuple(reversed(routes[group.a]));routes[group.b]=()
    if receiver is not None:routes[group.c]=tuple(receiver)
    return repool._State(tuple(routes),(999.,) if other else (),None)


def call(module,mode,problem,state,g,local,stats,*,deadline=None,weights=WEIGHTS,options=4,positions=12):
    args=(local,weights,SCALES,positions,options,perf_counter()+1. if deadline is None else deadline,stats)
    if mode=="relay":
        legs=repool._legs(problem)
        return module._receiver_relay(problem,state,g,legs[g.i][1][1],legs[g.j][1][1],*args)
    return module._receiver_direct(problem,state,g,*args)


def context(module,problem,stats=None,*,deadline=None,**kwargs):
    return bundle.receiver_bundle_cache(module,problem,weights=WEIGHTS,scales=SCALES,
        deadline=perf_counter()+2. if deadline is None else deadline,statistics=stats,**kwargs)


@pytest.mark.parametrize("mode",["relay","direct"])
def test_entire_enumeration_reuses_equal_receiver_despite_different_source_state(mode):
    problem,base,g,_=early_fixture()
    first,other=state_for(base,g),state_for(base,g,other=True)
    local=RouteEvaluator(problem)
    native=call(repool,mode,problem,first,g,local,pstats())
    stats,producer={},pstats()
    with context(repool,problem,stats):
        value=call(repool,mode,problem,first,g,local,producer)
        counters=dict(producer)
        repeated=call(repool,mode,problem,other,g,local,producer)
        assert value==native and repeated is value
        assert producer==counters
        assert stats[mode+"_logical_calls"]==2
        assert stats[mode+"_actual_calls"]==stats[mode+"_cache_hits"]==1
        assert isinstance(value,tuple) and all(isinstance(r,tuple) for r in value)
    assert stats["patches_restored"]


def test_complete_group_objective_route_evaluator_and_limits_are_in_cache_key():
    problem,base,g,_=early_fixture()
    state=state_for(base,g);local=RouteEvaluator(problem);stats={}
    with context(repool,problem,stats):
        call(repool,"relay",problem,state,g,local,pstats())
        call(repool,"relay",problem,state,g,local,pstats())
        call(repool,"relay",problem,state,replace(g,priority=(123.,)),local,pstats())
        call(repool,"relay",problem,state,g,RouteEvaluator(problem),pstats())
        call(repool,"relay",problem,state,g,local,pstats(),weights=(.6,.2,.2))
        call(repool,"relay",problem,state,g,local,pstats(),options=2)
        call(repool,"relay",problem,state_for(base,g,receiver=()),g,local,pstats())
        assert stats["relay_cache_hits"]==1 and stats["relay_actual_calls"]==6


def test_dynamic_inner_rank_policy_identity_prevents_reusing_old_policy(monkeypatch):
    problem,base,g,_=early_fixture();state=state_for(base,g);local=RouteEvaluator(problem);stats={}
    original=repool._rank_receiver
    with context(repool,problem,stats):
        call(repool,"relay",problem,state,g,local,pstats())
        def reverse(*args,**kwargs):return tuple(reversed(original(*args,**kwargs)))
        monkeypatch.setattr(repool,"_rank_receiver",reverse)
        changed=call(repool,"relay",problem,state,g,local,pstats())
        assert stats["relay_actual_calls"]==2 and stats["relay_cache_hits"]==0
        assert call(repool,"relay",problem,state,g,local,pstats()) is changed


def test_selected_out_legs_and_problem_identity_cannot_reuse_old_receiver_bundle():
    problem,base,g,_=early_fixture(stations=2)
    state=state_for(base,g);local=RouteEvaluator(problem);stats={}
    with context(repool,problem,stats):
        call(repool,"relay",problem,state,g,local,pstats())
        legs=repool._legs(problem)
        repool._receiver_relay(problem,state,g,legs[g.i][2][1],legs[g.j][2][1],local,
            WEIGHTS,SCALES,12,4,perf_counter()+1.,pstats())
        assert stats["relay_actual_calls"]==2 and stats["relay_cache_hits"]==0
        other=replace(problem,speed_km_per_min=2.)
        # This context is explicitly scoped to one problem. Other identities
        # safely delegate rather than receiving any cached geometry/routes.
        call(repool,"relay",other,state,g,RouteEvaluator(other),pstats())
        assert stats["relay_delegations"]==1 and stats["relay_actual_calls"]==3


def test_nonpure_receiver_or_unhashable_group_delegates_instead_of_cache(monkeypatch):
    problem,base,g,_=early_fixture();state=state_for(base,g);local=RouteEvaluator(problem);stats={}
    calls=[]
    fake=SimpleNamespace(_receiver_relay=lambda *a:(calls.append(1) or ((1,-1),)),
        _receiver_direct=lambda *a:(calls.append(1) or ((1,-1),)),_capacity=repool._capacity,_Deadline=repool._Deadline)
    relayed=state_for(base,g,receiver=(101,-101))
    badpair=state_for(base,g,receiver=(5,-6))
    with context(fake,problem,stats):
        for candidate,group in ((relayed,g),(badpair,g),(state,replace(g,priority=({},)))):
            call(fake,"relay",problem,candidate,group,local,pstats())
            call(fake,"relay",problem,candidate,group,local,pstats())
        assert stats["relay_delegations"]==6 and stats["relay_cache_hits"]==0


def test_cache_hits_and_slow_actual_enumeration_respect_call_local_deadline(monkeypatch):
    problem,base,g,_=early_fixture();state=state_for(base,g);local=RouteEvaluator(problem)
    clock=[0.];monkeypatch.setattr(bundle,"perf_counter",lambda:clock[0])
    fake=SimpleNamespace(_receiver_relay=lambda *a:((1,-1),),_receiver_direct=lambda *a:((1,-1),),
        _capacity=repool._capacity,_Deadline=repool._Deadline)
    stats={};producer=pstats()
    with context(fake,problem,stats,deadline=20.):
        call(fake,"relay",problem,state,g,local,producer,deadline=11.)
        clock[0]=12.
        with pytest.raises(repool._Deadline):call(fake,"relay",problem,state,g,local,producer,deadline=11.)
        assert stats["relay_cache_hits"]==0 and producer["deadline_reached"]
    clock[0]=0.
    def slow(*args):clock[0]=12.;return ((1,-1),)
    fake._receiver_direct=slow
    with context(fake,problem,deadline=20.):
        with pytest.raises(repool._Deadline):call(fake,"direct",problem,state,g,local,pstats(),deadline=11.)


def test_lru_limits_exception_nested_and_normal_exit_restore_both_functions():
    problem,base,g,_=early_fixture();state=state_for(base,g);local=RouteEvaluator(problem);stats={}
    originals={name:getattr(repool,name) for name in bundle.PATCH_NAMES}
    with pytest.raises(RuntimeError,match="deliberate"):
        with context(repool,problem,stats,memo_limit=1):
            call(repool,"relay",problem,state,g,local,pstats())
            call(repool,"relay",problem,state_for(base,g,receiver=()),g,local,pstats())
            assert stats["memo_evictions"]==1
            with pytest.raises(RuntimeError,match="nested"):
                with context(repool,problem):pass
            raise RuntimeError("deliberate")
    assert stats["patches_restored"] and not bundle._active
    assert all(getattr(repool,name) is function for name,function in originals.items())
    with context(repool,problem):pass
    assert all(getattr(repool,name) is function for name,function in originals.items())


def integrated(problem,base,*,cache):
    weights,scales=(.5,.25,.25),(6.,100.,100.)
    stats={}
    def run():
        with joint.joint_layouts(repool,problem,weights=weights,scales=scales,deadline=perf_counter()+3.):
            with frontier.source_frontier_cache(repool,problem,weights=weights,scales=scales,deadline=perf_counter()+3.):
                return mobile.refine(problem,base,deadline=perf_counter()+2.8,weights=weights,scales=scales,
                    max_groups=1,option_count=4,beam_width=8,full_candidates_per_group=8,raw_spec_limit=8,
                    point_limit=8,pattern_rounds=1,point_eval_limit=16,full_candidate_limit=8)
    if cache:
        with bundle.receiver_bundle_cache(repool,problem,weights=weights,scales=scales,
            deadline=perf_counter()+3.,statistics=stats):result=run()
    else:result=run()
    return result,stats


def test_full_tiny_pipeline_identical_with_or_without_entire_receiver_cache_and_independent_audit():
    problem,base=mobile_fixture()
    uncached,_=integrated(problem,base,cache=False)
    cached,stats=integrated(problem,base,cache=True)
    assert cached["routes"]==uncached["routes"]
    assert cached["evaluation"].score==uncached["evaluation"].score
    assert cached["metadata"]["selected_groups"]==uncached["metadata"]["selected_groups"]
    assert cached["metadata"]["mobile_meetings"]==uncached["metadata"]["mobile_meetings"]
    left=cached["metadata"]["raw_stage"]["full_candidate_snapshots"]
    right=uncached["metadata"]["raw_stage"]["full_candidate_snapshots"]
    assert [(s["routes"],s["key"]) for s in left]==[(s["routes"],s["key"]) for s in right]
    assert stats["relay_cache_hits"]>0 and stats["relay_actual_calls"]<stats["relay_logical_calls"]
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    proof=audit_mobile_repooling_routes(tasks,mobile.mobile.topology(problem),mobile.mobile.topology(cached["problem"]),
        cached["routes"],base_routes=base,move=cached["metadata"]["mobile_meetings"][-1]["repooling_move"])
    assert proof["passed"] and proof["mobile_ground_cache_min"]==0.
    assert stats["patches_restored"]
    json.dumps(stats,allow_nan=False)


def test_bad_context_parameters_rejected_before_any_patch():
    problem,_,_,_=early_fixture()
    originals={name:getattr(repool,name) for name in bundle.PATCH_NAMES}
    with pytest.raises(ValueError):
        with context(repool,problem,memo_limit=0):pass
    assert all(getattr(repool,name) is function for name,function in originals.items())
