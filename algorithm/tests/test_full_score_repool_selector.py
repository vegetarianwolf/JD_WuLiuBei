"""Exact compensation-score arithmetic and bounded shared tiny selection."""
from dataclasses import replace
from hashlib import sha256
import json
from time import perf_counter

import pytest

from uav_dispatch import Point,Problem,Task
from uav_dispatch.model import Score
from uav_dispatch.search import RouteEvaluator,route_insertion_options
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import full_score_repool_selector as selector
from algorithm.experiments import relay_repooling_exchange as repool
from algorithm.experiments import receiver_bundle_cache as bundle
from algorithm.experiments import source_frontier_cache as frontier
from algorithm.experiments import joint_mobile_layouts as joint
from algorithm.experiments import mobile_repooling_refinement as mobile
from algorithm.experiments.audit_mobile_repooling import audit_mobile_repooling_routes
from algorithm.tests.test_mobile_repooling_refinement import mobile_fixture


WEIGHTS,SCALES=(.5,.25,.25),(10.,100.,100.)


def counterexample(*,shared=False):
    tasks=(Task(1,Point(0.,0.),Point(20.,0.),100.),
           Task(2,Point(0.,0.),Point(21.,0.),100.),
           Task(3,Point(5. if shared else 1.,0.),Point(0.,0.),100.),
           Task(4,Point(5. if shared else 1.,0.),Point(0.,0.),100.),
           Task(5,Point(0.,0.),Point(0.,0.),100. if shared else 3.))
    problem=Problem(tasks,drone_count=3,max_tasks_per_drone=3,capacity=2,speed_km_per_min=1.,
                    drone_homes=(Point(0.,0.),)*3)
    receiver=(3,4,-3,-4,5,-5) if shared else (3,-3,4,-4,5,-5)
    return problem,((1,-1),(2,-2),receiver)


def larger_pool_fixture():
    problem,routes=counterexample()
    tasks=problem.tasks[:2]+tuple(Task(k,Point(float(k-2),0.),Point(float(k-2),0.),100.)
                                for k in range(3,9))+(Task(9,Point(10.,0.),Point(10.,0.),0.),)
    problem=replace(problem,tasks=tasks,max_tasks_per_drone=7)
    return problem,(routes[0],routes[1],tuple(v for k in range(3,10) for v in (k,-k)))


def producer_stats():return dict(receiver_bases_generated=0,selector_insertion_calls=0,deadline_reached=False)


def context(problem,stats=None,*,deadline=None,**kwargs):
    return selector.full_score_selector(repool,problem,weights=WEIGHTS,scales=SCALES,
        deadline=perf_counter()+2. if deadline is None else deadline,statistics=stats,**kwargs)


def select(problem,base,local=None,*,deadline=None,compensation_limit=3,positions=16,stats=None):
    return repool._select_groups(problem,base,evaluate_solution(problem,base,_force_full=True),
        RouteEvaluator(problem) if local is None else local,WEIGHTS,SCALES,1709,8,2,
        compensation_limit,positions,perf_counter()+1. if deadline is None else deadline,
        producer_stats() if stats is None else stats)


def test_joint_removal_prevents_double_credit_for_same_native_late_delivery():
    problem,base=counterexample();local=RouteEvaluator(problem);route=base[2]
    original=local.evaluate(route,start_node=problem.home_node(2)).score
    one=selector._removed_score(problem,route,2,(3,),local)-original
    other=selector._removed_score(problem,route,2,(4,),local)-original
    both=selector._removed_score(problem,route,2,(3,4),local)-original
    assert one==other==Score(-1,-1.,-2.)
    assert one+other==Score(-2,-2.,-4.)
    assert both==Score(-1,-1.,-4.)
    assert selector._key(one+other,WEIGHTS,SCALES)<selector._key(both,WEIGHTS,SCALES)


def test_joint_removal_captures_shared_detour_absent_from_both_single_deltas():
    problem,base=counterexample(shared=True);local=RouteEvaluator(problem);route=base[2]
    original=local.evaluate(route,start_node=problem.home_node(2)).score
    one=selector._removed_score(problem,route,2,(3,),local)-original
    other=selector._removed_score(problem,route,2,(4,),local)-original
    both=selector._removed_score(problem,route,2,(3,4),local)-original
    assert one==other==Score(0,0.,0.)
    assert both==Score(0,0.,-10.)


def test_removal_score_uses_actual_aircraft_home_and_native_deadline_threshold():
    problem,base=counterexample()
    problem=replace(problem,drone_homes=(Point(0.,0.),Point(0.,0.),Point(10.,0.)))
    local=RouteEvaluator(problem)
    actual=selector._removed_score(problem,base[2],2,(3,),local)
    direct=local.evaluate((4,-4,5,-5),start_node=problem.home_node(2)).score
    wrong=local.evaluate((4,-4,5,-5),start_node=problem.home_node(0)).score
    assert actual==direct and actual!=wrong
    assert actual==Score(1,7.,10.)


def test_removal_pool_keeps_late_task_outside_original_geometry_six():
    problem,base=larger_pool_fixture();stats={}
    with context(problem,stats,geo_pool_limit=6,removal_pool_limit=1):
        groups=select(problem,base)
    record=next(r for r in stats["ranking_records"] if (r["i"],r["j"],r["c"])==(1,2,2))
    for side in record["side_pools"]:
        assert 9 not in side["geometry_pool"]
        assert side["removal_pool"]==[9] and 9 in side["union_pool"]
        assert len(side["union_pool"])==7
    assert len(groups)<=8 and stats["removal_pool_truncations"]>0
    assert stats["selector_completed"] and stats["patches_restored"]


def test_all_directed_pairs_scored_before_family_limit_and_real_score_decompositions():
    problem,base=counterexample();stats={};local=RouteEvaluator(problem)
    with context(problem,stats):groups=select(problem,base,local,compensation_limit=1)
    assert len(groups)==1
    record=stats["ranking_records"][0]
    assert record["directed_pairs"]==6 and len(record["retained_pairs"])==1
    assert stats["directed_pairs_scored"]==6 and stats["family_pair_truncations"]==1
    g=groups[0];proof=record["retained_pairs"][0]
    source_a=tuple(v for v in base[g.a] if abs(v)!=g.i)
    source_b=tuple(v for v in base[g.b] if abs(v)!=g.j)
    a=route_insertion_options(problem,local,source_a,g.a,g.k1,candidate_limit=16,option_count=1)[0].delta
    b=route_insertion_options(problem,local,source_b,g.b,g.k2,candidate_limit=16,option_count=1)[0].delta
    c=selector._removed_score(problem,base[g.c],g.c,(g.k1,g.k2),local)-local.evaluate(
        base[g.c],start_node=problem.home_node(g.c)).score
    assert proof["source_insert_delta_a"]==list(selector._physical(a))
    assert proof["source_insert_delta_b"]==list(selector._physical(b))
    assert proof["joint_C_removal_delta"]==list(selector._physical(c))
    assert proof["ranking_delta"]==list(selector._physical(a+b+c))
    assert proof["ranking_key"]==list(selector._key(a+b+c,WEIGHTS,SCALES))
    # The C joint score is applied once, not added after two single credits.
    singles=sum((selector._removed_score(problem,base[g.c],g.c,(k,),local)-local.evaluate(
        base[g.c],start_node=problem.home_node(g.c)).score for k in (g.k1,g.k2)),Score(0,0.,0.))
    assert c!=singles


def test_exact_memo_reuses_same_inputs_but_separates_routes_local_and_position_limit():
    problem,base=counterexample();stats={};local=RouteEvaluator(problem)
    with context(problem,stats):
        first=select(problem,base,local)
        counts=(stats["single_removal_calls"],stats["pair_removal_calls"],stats["insertion_calls"])
        assert select(problem,base,local)==first
        assert counts==(stats["single_removal_calls"],stats["pair_removal_calls"],stats["insertion_calls"])
        assert stats["single_removal_hits"] and stats["pair_removal_hits"] and stats["insertion_hits"]
        select(problem,base,RouteEvaluator(problem))
        assert stats["single_removal_calls"]>counts[0]
        insert_before=stats["insertion_calls"]
        select(problem,base,local,positions=8)
        assert stats["insertion_calls"]>insert_before
        changed=(base[0],base[1],(4,-4,3,-3,5,-5))
        before=stats["single_removal_calls"]
        select(problem,changed,local)
        assert stats["single_removal_calls"]>before


@pytest.mark.parametrize("during_pair",[False,True])
def test_mid_selector_deadline_raises_without_returning_complete_prefix(monkeypatch,during_pair):
    problem,base=counterexample();clock=[0.];stats={};producer=producer_stats()
    monkeypatch.setattr(selector,"perf_counter",lambda:clock[0])
    original=selector._removed_score
    def timed(p,route,a,chosen,local):
        result=original(p,route,a,chosen,local)
        if (len(chosen)==2)==during_pair:clock[0]=12.
        return result
    monkeypatch.setattr(selector,"_removed_score",timed)
    native=repool._select_groups
    with context(problem,stats,deadline=20.):
        with pytest.raises(repool._Deadline):select(problem,base,deadline=11.,stats=producer)
        assert not stats["selector_completed"] and stats["selected_groups"]==[]
        assert stats["deadline_reached"] and producer["deadline_reached"]
        assert stats["families_processed"]==0
    assert repool._select_groups is native and stats["patches_restored"]


def test_expired_entry_context_exception_and_nested_restore_selector():
    problem,base=counterexample();original=repool._select_groups
    with context(problem,deadline=perf_counter()-1.):
        with pytest.raises(repool._Deadline):select(problem,base)
    assert repool._select_groups is original
    with pytest.raises(RuntimeError,match="deliberate"):
        with context(problem):
            with pytest.raises(RuntimeError,match="nested"):
                with context(problem):pass
            raise RuntimeError("deliberate")
    assert repool._select_groups is original and not selector._active


def test_completed_selector_reentry_after_raw_deadline_clears_old_completion(monkeypatch):
    problem,base=counterexample();clock=[0.];stats={};local=RouteEvaluator(problem);producer=producer_stats()
    monkeypatch.setattr(selector,"perf_counter",lambda:clock[0])
    with context(problem,stats,deadline=20.):
        select(problem,base,local,deadline=11.,stats=producer)
        assert stats["selector_completed"] and stats["selected_groups"]
        assert stats["selector_completed_at_perf_counter"]==0.
        clock[0]=12.
        with pytest.raises(repool._Deadline):select(problem,base,local,deadline=11.,stats=producer)
        assert not stats["selector_completed"] and not producer["selector_completed"]
        assert stats["selected_groups"]==[] and stats["selector_completed_at_perf_counter"] is None
        assert stats["selector_sha256"]==sha256(b"[]").hexdigest()


def test_other_problem_or_objective_delegates_native_selector_unchanged():
    problem,base=counterexample();other=replace(problem,speed_km_per_min=2.)
    expected=select(other,base);stats={};native=repool._select_groups
    checked=evaluate_solution(problem,base,_force_full=True);local=RouteEvaluator(problem)
    args=(problem,base,checked,local,(.6,.2,.2),SCALES,1709,8,2,3,16,perf_counter()+1.)
    objective_expected=native(*args,producer_stats())
    with context(problem,stats):
        assert select(other,base)==expected
        assert repool._select_groups(*args,producer_stats())==objective_expected
    assert stats["delegations"]==2 and stats["actual_selectors"]==0


def integrated(problem,base,relay):
    stats={};weights,scales=(.5,.25,.25),(6.,100.,100.)
    patches=(selector.PATCH_NAMES+bundle.PATCH_NAMES+frontier.PATCH_NAMES+joint.PATCH_NAMES)
    originals={name:getattr(repool,name) for name in patches}
    with selector.full_score_selector(repool,problem,weights=weights,scales=scales,
                                    deadline=perf_counter()+3.,statistics=stats):
        with bundle.receiver_bundle_cache(repool,problem,weights=weights,scales=scales,deadline=perf_counter()+3.):
            with joint.joint_layouts(repool,problem,weights=weights,scales=scales,deadline=perf_counter()+3.):
                with frontier.source_frontier_cache(repool,problem,weights=weights,scales=scales,deadline=perf_counter()+3.):
                    result=mobile.refine(problem,base,deadline=perf_counter()+2.8,weights=weights,scales=scales,
                        relay_enabled=relay,max_groups=1,option_count=4,beam_width=8,full_candidates_per_group=8,
                        raw_spec_limit=8,point_limit=8,pattern_rounds=1,point_eval_limit=16,full_candidate_limit=8)
    assert all(getattr(repool,name) is original for name,original in originals.items())
    return result,stats


def test_real_tiny_both_arms_share_full_completed_selector_hash_and_physical_audit():
    problem,base=mobile_fixture()
    direct,dstats=integrated(problem,base,False)
    relay,rstats=integrated(problem,base,True)
    assert direct["evaluation"].valid and relay["evaluation"].valid
    assert dstats["selector_completed"] and rstats["selector_completed"]
    assert dstats["selected_groups"]==rstats["selected_groups"]==relay["metadata"]["selected_groups"]
    assert dstats["selector_sha256"]==rstats["selector_sha256"]==relay["metadata"]["selector_sha256"]
    assert relay["metadata"]["selected_groups"]==direct["metadata"]["selected_groups"]
    assert relay["metadata"]["selector_completed"] and direct["metadata"]["selector_completed"]
    digest=sha256(json.dumps(rstats["selected_groups"],sort_keys=True,separators=(",",":")).encode()).hexdigest()
    assert digest==rstats["selector_sha256"]
    assert relay["metadata"]["mobile_meetings"] and relay["metadata"]["final_cross_uav_count"]==2
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    proof=audit_mobile_repooling_routes(tasks,mobile.mobile.topology(problem),mobile.mobile.topology(relay["problem"]),
        relay["routes"],base_routes=base,move=relay["metadata"]["mobile_meetings"][-1]["repooling_move"])
    assert proof["passed"] and proof["mobile_ground_cache_min"]==0.
    json.dumps(rstats,allow_nan=False)


def test_invalid_configuration_fails_before_patching():
    problem,_=counterexample();original=repool._select_groups
    for config in ({"geo_pool_limit":0},{"removal_pool_limit":True},{"geo_pool_limit":33}):
        with pytest.raises(ValueError):
            with context(problem,**config):pass
    assert repool._select_groups is original
