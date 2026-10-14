"""Tiny truthful source frontiers, memo equivalence and context restoration."""
from dataclasses import replace
import json
from time import perf_counter
from types import SimpleNamespace
import gc
import weakref

import pytest

from uav_dispatch import Point,Task
from uav_dispatch.relay import GlobalRelayEvaluator
from uav_dispatch.search import RouteEvaluator

from algorithm.experiments import joint_mobile_layouts as joint
from algorithm.experiments import mobile_repooling_refinement as mobile
from algorithm.experiments import relay_repooling_exchange as repool
from algorithm.experiments import source_frontier_cache as frontier
from algorithm.experiments.audit_mobile_repooling import audit_mobile_repooling_routes
from algorithm.tests.test_joint_mobile_layouts import early_fixture,WEIGHTS,SCALES
from algorithm.tests.test_mobile_repooling_refinement import mobile_fixture


def fake_module():
    return SimpleNamespace(**{name:getattr(repool,name) for name in frontier.PATCH_NAMES
        if name!="_rebuild"},_rebuild=lambda *a,**k:(),_Deadline=repool._Deadline)


def context(module,problem,*,stats=None,deadline=None,**kwargs):
    return frontier.source_frontier_cache(module,problem,weights=WEIGHTS,scales=SCALES,
        deadline=perf_counter()+2. if deadline is None else deadline,statistics=stats,**kwargs)


def start(module,problem,base,g,*,relay=True,deadline=None,pstats=None):
    return module._rebuild(problem,base,g,repool._legs(problem),RouteEvaluator(problem),GlobalRelayEvaluator(problem),
        WEIGHTS,SCALES,relay,16,4,8,perf_counter()+1.8 if deadline is None else deadline,{} if pstats is None else pstats)


def source_options(module,problem,evaluator,route,*,option_count=4,limit=4,surface=None):
    return module.route_leg_insertion_options(problem,evaluator,route,0,101,-101,
        candidate_limit=limit,option_count=option_count,counts_toward_k=False,event_cap=None,surface=surface)


def partial_stats():
    return dict(rejected_invalid_partial=0,rejected_partial_coverage=0,rejected_cycles=0,global_evaluations=0)


def test_earlier_actual_source_drop_outside_native_top_four_is_retained_with_true_delta():
    problem,base,g,_=early_fixture()
    # A native final deadline makes the cost of dropping early explicit, while
    # RELAY_IN's own completion is deliberately not a final delivery score.
    problem=replace(problem,tasks=tuple(replace(t,deadline_min=15.) if t.id==3 else t for t in problem.tasks))
    route=(5,-5,3,-3)
    evaluator=RouteEvaluator(problem)
    native=source_options(repool,problem,evaluator,route)
    assert all(option.end_time_min==50. for option in native)
    stats={};module=fake_module()
    with context(module,problem,stats=stats):
        start(module,problem,base,g)
        selected=source_options(module,problem,evaluator,route)
        assert native[0] in selected and min(o.end_time_min for o in selected)==30.
        old=evaluator.evaluate(route,start_node=problem.home_node(0)).score
        for option in selected:
            actual=evaluator.evaluate(option.route,start_node=problem.home_node(0)).score-old
            assert option.delta.late_count==actual.late_count
            assert option.delta.total_lateness_min==pytest.approx(actual.total_lateness_min)
            assert option.delta.distance_km==pytest.approx(actual.distance_km)
        early=min(selected,key=lambda o:o.end_time_min)
        assert early.delta.total_lateness_min>native[0].delta.total_lateness_min
        assert stats["earliest_drop_retained"]==stats["native_physical_best_retained"]==1
    assert stats["patches_restored"]


def test_raw_capacity_surface_scoring_is_bounded_and_early_slots_remain_legal():
    problem,base,g,_=early_fixture()
    route=(5,-5,3,-3)
    module=fake_module();stats={};local=RouteEvaluator(problem)
    with context(module,problem,stats=stats,raw_position_limit=8):
        start(module,problem,base,g)
        selected=source_options(module,problem,local,route)
        assert selected and stats["raw_surface_truncations"]==1
        assert stats["raw_positions_exactly_scored"]<=8
        assert stats["capacity_positions_seen"]>8
        for option in selected:local.evaluate(option.route,start_node=problem.home_node(0))


def test_source_memo_keys_include_evaluator_route_bounds_and_explicit_surface_positions():
    problem,base,g,_=early_fixture()
    module=fake_module();stats={};local=RouteEvaluator(problem);route=(5,-5,3,-3)
    with context(module,problem,stats=stats):
        start(module,problem,base,g)
        first=source_options(module,problem,local,route)
        assert source_options(module,problem,local,route) is first
        assert stats["source_memo_hits"]==1
        source_options(module,problem,RouteEvaluator(problem),route)
        source_options(module,problem,local,route,limit=8)
        surface=local.surface_cache.get(route,start_node=problem.home_node(0))
        source_options(module,problem,local,route,surface=replace(surface,positions=surface.positions[:4]))
        assert stats["source_actual_calls"]==4


def test_bad_surface_cannot_reuse_valid_surface_cache_even_with_same_positions():
    problem,base,g,_=early_fixture()
    module=fake_module();local=RouteEvaluator(problem);route=(5,-5,3,-3)
    surface=local.surface_cache.get(route,start_node=problem.home_node(0))
    with context(module,problem):
        start(module,problem,base,g)
        source_options(module,problem,local,route,surface=surface)
        for changed in (replace(surface,route=tuple(reversed(route))),
                        replace(surface,arrival=tuple(t+1. for t in surface.arrival))):
            with pytest.raises(ValueError,match="canonical"):
                source_options(module,problem,local,route,surface=changed)


def test_identity_key_keeps_object_alive_and_never_uses_recycled_integer_identity():
    class Object:pass
    value=Object();weak=weakref.ref(value)
    key=frontier._Identity(value);cache={key:"checked"}
    assert frontier._Identity(value)==key and cache[frontier._Identity(value)]=="checked"
    del value;gc.collect()
    assert weak() is not None
    other=Object()
    assert frontier._Identity(other)!=key


def test_partial_exact_routes_coverage_problem_and_evaluator_memo_equivalence():
    problem,base,g,states=early_fixture()
    module=fake_module();stats={};pstats=partial_stats();evaluator=GlobalRelayEvaluator(problem)
    routes=states[0].routes;present=set(problem.task_ids)
    with context(module,problem,stats=stats):
        start(module,problem,base,g)
        first=module._partial(problem,routes,present,evaluator,pstats)
        actual_calls=pstats["global_evaluations"]
        second=module._partial(problem,routes,list(reversed(sorted(present))),evaluator,pstats)
        assert second is first and pstats["global_evaluations"]==actual_calls
        assert module._partial(problem,routes,present-{3},evaluator,pstats) is None
        assert module._partial(problem,routes,present-{3},evaluator,pstats) is None
        assert stats["partial_cached_rejections"]==1
        module._partial(problem,routes,present,GlobalRelayEvaluator(problem),pstats)
        other=replace(problem,speed_km_per_min=2.)
        module._partial(other,routes,present,GlobalRelayEvaluator(other),pstats)
        assert stats["partial_actual_calls"]==4
        assert stats["partial_memo_hits"]==2
        assert stats["partial_actual_global_calls"]==3


def test_compensation_insertion_changes_full_routes_and_misses_partial_memo():
    problem,base,g,states=early_fixture()
    module=fake_module();stats={};pstats=partial_stats();evaluator=GlobalRelayEvaluator(problem)
    routes=states[1].routes
    partial=tuple(tuple(v for v in r if abs(v) not in (3,4)) for r in routes)
    with context(module,problem,stats=stats):
        start(module,problem,base,g)
        module._partial(problem,partial,set(problem.task_ids)-{3,4},evaluator,pstats)
        module._partial(problem,routes,set(problem.task_ids),evaluator,pstats)
        assert stats["partial_actual_calls"]==2 and stats["partial_memo_hits"]==0


def test_receiver_memo_uses_entire_pure_receiver_input_and_preserves_outer_early_choice():
    problem,base,g,states=early_fixture()
    stats={};joint_stats={};pstats={"receiver_local_evaluations":0}
    original={name:getattr(repool,name) for name in set(joint.PATCH_NAMES)|set(frontier.PATCH_NAMES)}
    local=RouteEvaluator(problem)
    routes=tuple(state.routes[2] for state in states)
    selected=(102,104)
    with joint.joint_layouts(repool,problem,weights=WEIGHTS,scales=SCALES,deadline=perf_counter()+2.,statistics=joint_stats):
        # Establish a real valid scope without expensive native reconstruction.
        old_rebuild=repool._rebuild
        with frontier.source_frontier_cache(repool,problem,weights=WEIGHTS,scales=SCALES,
            deadline=perf_counter()+2.,statistics=stats):
            producer={**partial_stats(),"stations_considered":0,"leg_insertion_calls":0,
                "receiver_relay_layouts":0,"receiver_local_evaluations":0,"direct_insertion_calls":0,
                "group_records":[],"complete_scheduled_candidates":0,"deadline_reached":False}
            start(repool,problem,base,g,pstats=producer)
            first=repool._rank_receiver(problem,routes,2,selected,local,WEIGHTS,SCALES,16,4,perf_counter()+1.,pstats)
            local_calls=pstats["receiver_local_evaluations"]
            second=repool._rank_receiver(problem,tuple(reversed(routes)),2,selected,local,
                WEIGHTS,SCALES,16,4,perf_counter()+1.,pstats)
            assert second is first and pstats["receiver_local_evaluations"]==local_calls
            assert any(route.index(102)==0 for route in first)
            assert stats["receiver_memo_hits"]>0
            repool._rank_receiver(problem,routes,2,selected,local,WEIGHTS,SCALES,16,2,perf_counter()+1.,pstats)
        assert repool._rebuild is old_rebuild
    assert all(getattr(repool,name) is function for name,function in original.items())


def test_direct_source_choices_delegate_and_memo_does_not_change_direct_partial_scores():
    problem,base,g,_=early_fixture()
    module=fake_module();stats={};local=RouteEvaluator(problem);route=(5,-5,3,-3)
    native=source_options(repool,problem,local,route)
    with context(module,problem,stats=stats):
        start(module,problem,base,g,relay=False)
        assert source_options(module,problem,local,route)==native
        assert stats["source_delegations"]==1 and stats["source_actual_calls"]==0
        evaluator=GlobalRelayEvaluator(problem);pstats=partial_stats()
        original=repool._partial(problem,base,problem.task_ids,evaluator,pstats)
        cached=module._partial(problem,base,problem.task_ids,evaluator,pstats)
        assert original.score==cached.score


def test_cache_eviction_nested_and_exception_restore_every_function():
    problem,base,g,states=early_fixture()
    module=fake_module();original={name:getattr(module,name) for name in frontier.PATCH_NAMES};stats={}
    with pytest.raises(RuntimeError,match="deliberate"):
        with context(module,problem,stats=stats,memo_limit=1):
            start(module,problem,base,g)
            pstats=partial_stats();evaluator=GlobalRelayEvaluator(problem)
            for state in states:module._partial(problem,state.routes,problem.task_ids,evaluator,pstats)
            assert stats["memo_evictions"]>0
            with pytest.raises(RuntimeError,match="nested"):
                with context(module,problem):pass
            raise RuntimeError("deliberate")
    assert all(getattr(module,name) is function for name,function in original.items())
    assert stats["patches_restored"] and not frontier._active


def test_expired_memo_hit_and_raw_stage_deadline_are_checked_first(monkeypatch):
    problem,base,g,states=early_fixture()
    clock=[0.];monkeypatch.setattr(frontier,"perf_counter",lambda:clock[0])
    module=fake_module();stats={};pstats=partial_stats();evaluator=GlobalRelayEvaluator(problem)
    with context(module,problem,deadline=20.,stats=stats):
        start(module,problem,base,g,deadline=11.,pstats=pstats)
        module._partial(problem,states[0].routes,problem.task_ids,evaluator,pstats)
        clock[0]=12.
        with pytest.raises(repool._Deadline):
            module._partial(problem,states[0].routes,problem.task_ids,evaluator,pstats)
        assert stats["partial_memo_hits"]==0 and stats["deadline_reached"] and pstats["deadline_reached"]


def test_receiver_cache_hit_checks_earlier_call_deadline_and_all_local_inputs(monkeypatch):
    problem,base,g,states=early_fixture()
    clock=[0.];monkeypatch.setattr(frontier,"perf_counter",lambda:clock[0])
    module=fake_module();calls=[]
    module._rank_receiver=lambda p,routes,*a,**k:(calls.append(1) or tuple(routes))
    local=RouteEvaluator(problem);routes=tuple(s.routes[2] for s in states);stats={}
    with context(module,problem,deadline=20.,stats=stats):
        start(module,problem,base,g,deadline=20.)
        invoke=lambda evaluator,w,options,end:module._rank_receiver(problem,routes,2,(102,104),
            evaluator,w,SCALES,16,options,end,{})
        invoke(local,WEIGHTS,4,15.)
        invoke(local,WEIGHTS,4,15.)
        invoke(local,(.6,.2,.2),4,15.)
        invoke(RouteEvaluator(problem),WEIGHTS,4,15.)
        invoke(local,WEIGHTS,2,15.)
        assert len(calls)==4 and stats["receiver_memo_hits"]==1
        clock[0]=2.
        with pytest.raises(repool._Deadline):invoke(local,WEIGHTS,4,1.)
        assert stats["receiver_memo_hits"]==1


def test_true_tiny_joint_mobile_pipeline_with_source_frontiers_and_cache_is_independently_audited():
    problem,base=mobile_fixture()
    weights,scales=(.5,.25,.25),(6.,100.,100.)
    stats={};original={name:getattr(repool,name) for name in set(joint.PATCH_NAMES)|set(frontier.PATCH_NAMES)}
    with joint.joint_layouts(repool,problem,weights=weights,scales=scales,deadline=perf_counter()+3.):
        with frontier.source_frontier_cache(repool,problem,weights=weights,scales=scales,
            deadline=perf_counter()+3.,statistics=stats):
            result=mobile.refine(problem,base,deadline=perf_counter()+2.8,weights=weights,scales=scales,
                max_groups=1,option_count=4,beam_width=8,full_candidates_per_group=8,raw_spec_limit=8,
                point_limit=8,pattern_rounds=1,point_eval_limit=16,full_candidate_limit=8)
    assert result["evaluation"].valid and result["metadata"]["mobile_meetings"]
    assert stats["source_actual_calls"]>0 and stats["partial_memo_hits"]>0 and stats["receiver_memo_hits"]>0
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    audit=audit_mobile_repooling_routes(tasks,mobile.mobile.topology(problem),mobile.mobile.topology(result["problem"]),
        result["routes"],base_routes=base,move=result["metadata"]["mobile_meetings"][-1]["repooling_move"])
    assert audit["passed"] and audit["mobile_ground_cache_min"]==0.
    assert all(getattr(repool,name) is function for name,function in original.items())
    for snap in result["metadata"]["raw_stage"]["full_candidate_snapshots"]:
        score=GlobalRelayEvaluator(problem).evaluate(snap["routes"]).score
        assert snap["key"]==list(repool.weighted_key(score,weights,scales))
    json.dumps(stats,allow_nan=False)
