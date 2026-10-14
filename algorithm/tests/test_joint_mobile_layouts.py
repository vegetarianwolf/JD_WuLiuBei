"""Small layout-retention witnesses and audited mobile integration."""
from dataclasses import replace
import json
from time import perf_counter
from types import SimpleNamespace

import pytest

from uav_dispatch import Point,Problem,RelayStation,Task,TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.search import RouteEvaluator

from algorithm.experiments import joint_mobile_layouts as joint
from algorithm.experiments import mobile_repooling_refinement as mobile
from algorithm.experiments import relay_repooling_exchange as repool
from algorithm.experiments.audit_mobile_repooling import audit_mobile_repooling_routes
from algorithm.tests.test_mobile_repooling_refinement import mobile_fixture


WEIGHTS,SCALES=(.5,.25,.25),(7.,100.,100.)


def early_fixture(*,stations=1):
    tasks=(Task(1,Point(-10.,0.),Point(20.,0.),100.),
           Task(2,Point(10.,0.),Point(21.,0.),100.),
           Task(3,Point(0.,0.),Point(-10.,0.),100.),
           Task(4,Point(0.,0.),Point(10.,0.),100.),
           Task(5,Point(-10.,0.),Point(-10.,0.),100.),
           Task(6,Point(10.,0.),Point(10.,0.),100.),
           Task(7,Point(20.,0.),Point(20.,0.),100.))
    sites=tuple(RelayStation(h,20.+h-1.,0.) for h in range(1,stations+1))
    registry={}
    counter=101
    for task in tasks:
        for site in sites:
            registry[counter]=TransportLeg(counter,task.id,"RELAY_IN",site.id)
            registry[counter+1]=TransportLeg(counter+1,task.id,"RELAY_OUT",site.id)
            counter+=2
    problem=Problem(tasks,drone_count=3,max_tasks_per_drone=3,capacity=2,speed_km_per_min=1.,
        drone_homes=(Point(-10.,0.),Point(10.,0.),Point(0.,0.)),relay_stations=sites,
        leg_registry=registry,task_relay_candidates={t.id:tuple(s.id for s in sites) for t in tasks})
    base=((5,-5,1,-1),(6,-6,2,-2),(3,4,-3,-4,7,-7))
    group=repool._Group(1,2,0,1,2,3,4,())
    states=[]
    legs=repool._legs(problem)
    for site in sites:
        incoming=(legs[1][site.id][0],legs[2][site.id][0])
        outgoing=(legs[1][site.id][1],legs[2][site.id][1])
        source=((5,-5,incoming[0],-incoming[0],3,-3),(6,-6,incoming[1],-incoming[1],4,-4))
        for early in (False,True):
            takes=outgoing+tuple(-v for v in outgoing)
            receiver=takes+(7,-7) if early else (7,-7)+takes
            routes=source+(receiver,)
            scheduled=GlobalRelayEvaluator(problem).evaluate(routes)
            assert scheduled.feasible
            states.append(repool._State(routes,repool.weighted_key(scheduled.score,WEIGHTS,SCALES),site.id))
    return problem,base,group,tuple(states)


def fake_module(states=()):
    return SimpleNamespace(_rebuild=lambda *a,**k:states,_diverse=repool._diverse,
        _rank_receiver=repool._rank_receiver,_capacity=repool._capacity,_Deadline=repool._Deadline)


def start(module,problem,base,group,*,relay=True,beam=8,deadline=None,producer_stats=None):
    return module._rebuild(problem,base,group,repool._legs(problem),RouteEvaluator(problem),
        GlobalRelayEvaluator(problem),WEIGHTS,SCALES,relay,16,4,beam,
        perf_counter()+2. if deadline is None else deadline,{} if producer_stats is None else producer_stats)


def context(module,problem,*,deadline=None,stats=None,**kwargs):
    return joint.joint_layouts(module,problem,weights=WEIGHTS,scales=SCALES,
        deadline=perf_counter()+2. if deadline is None else deadline,statistics=stats,**kwargs)


def test_early_receiver_mobile_opportunity_survives_worse_real_fixed_site_key():
    problem,base,group,states=early_fixture()
    late,early=states
    assert late.key<early.key  # Old fixed-cache physical preference.
    late_bound=joint._layout_bound(problem,late,group,deadline=perf_counter()+1.)
    early_bound=joint._layout_bound(problem,early,group,deadline=perf_counter()+1.)
    assert not late_bound["possible"] and early_bound["possible"]
    assert joint._weighted(early_bound["score"],WEIGHTS,SCALES)[0]<late.key[0]
    module=fake_module(states)
    stats={}
    keys=tuple(s.key for s in states)
    with context(module,problem,stats=stats):
        start(module,problem,base,group)
        kept=module._diverse(states,2)
        assert kept[0] is late and early in kept
        assert tuple(s.key for s in states)==keys
        assert stats["mobile_family_retained"]>0
    assert stats["patches_restored"]


def test_four_station_physical_baselines_are_all_kept_before_mobile_slots():
    problem,base,group,states=early_fixture(stations=4)
    baseline={h:min((s for s in states if s.station==h),key=lambda s:s.key) for h in range(1,5)}
    module=fake_module(states)
    with context(module,problem):
        start(module,problem,base,group)
        kept=module._diverse(states,8)
        assert len(kept)==8
        assert all(any(s is expected for s in kept) for expected in baseline.values())
        assert {s.station for s in kept[:4]}=={1,2,3,4}
        for state in kept:
            actual=GlobalRelayEvaluator(problem).evaluate(state.routes).score
            assert state.key==repool.weighted_key(actual,WEIGHTS,SCALES)


def test_earlier_source_drop_frontier_survives_fixed_cache_preference_for_late_source():
    problem,base,group,states=early_fixture()
    expanded=list(states)
    for state in states:
        # Hold IN1 through compensation3's pickup/delivery. The fixed H trip
        # becomes shorter, but a mobile meeting at C's early focus is later.
        source=(5,-5,101,3,-3,-101)
        routes=(source,state.routes[1],state.routes[2])
        scheduled=GlobalRelayEvaluator(problem).evaluate(routes)
        assert scheduled.feasible
        expanded.append(repool._State(routes,repool.weighted_key(scheduled.score,WEIGHTS,SCALES),1))
    physical=min(expanded,key=lambda s:s.key)
    assert physical.routes[0].index(-101)==5
    best_mobile=min((s for s in expanded if joint._layout_bound(problem,s,group,deadline=perf_counter()+1.)["possible"]),
        key=lambda s:joint._weighted(joint._layout_bound(problem,s,group,deadline=perf_counter()+1.)["score"],WEIGHTS,SCALES))
    assert best_mobile.routes[0].index(-101)==3 and best_mobile.key>physical.key
    module=fake_module(tuple(expanded))
    with context(module,problem):
        start(module,problem,base,group)
        kept=module._diverse(tuple(expanded),3)
        assert physical in kept and best_mobile in kept
        assert any(s.routes[0].index(-101)==3 for s in kept)


def test_atomic_partial_without_compensations_uses_same_coverage_and_lower_bound():
    problem,base,group,states=early_fixture()
    partial=[]
    for state in states:
        routes=tuple(tuple(v for v in route if abs(v) not in (3,4)) for route in state.routes)
        actual=GlobalRelayEvaluator(problem).evaluate(routes)
        partial.append(repool._State(routes,repool.weighted_key(actual.score,WEIGHTS,SCALES),state.station))
    expected={1,2,5,6,7}
    for state in partial:
        bound=joint._layout_bound(problem,state,group,deadline=perf_counter()+1.)
        assert bound["coverage"]==expected
        snapshot=dict(routes=state.routes,move={**repool._group_record(group),"station_id":state.station})
        reference,_=mobile._lower(problem,mobile._spec(problem,snapshot))
        assert bound["score"].late_count==reference.late_count
        assert bound["score"].distance_km==pytest.approx(reference.distance_km)
        assert bound["score"].total_lateness_min==pytest.approx(reference.total_lateness_min)
    module=fake_module(tuple(partial))
    stats={}
    with context(module,problem,stats=stats):
        start(module,problem,base,group)
        assert len(module._diverse(tuple(partial),2))==2
        assert stats["ranking_records"][0]["present_tasks"]==sorted(expected)
        assert not stats["ranking_records"][0]["complete"]
        assert not stats["configuration"]["partial_bounds_used_for_full_incumbent_pruning"]


def test_different_partial_coverage_is_rejected_before_mobile_ranking():
    problem,base,group,states=early_fixture()
    changed=tuple(tuple(v for v in route if abs(v)!=3) for route in states[1].routes)
    partial=repool._State(changed,states[1].key,1)
    module=fake_module(states)
    stats={}
    with context(module,problem,stats=stats):
        start(module,problem,base,group)
        with pytest.raises(ValueError,match="identical present"):
            module._diverse((states[0],partial),2)
    assert stats["coverage_mismatches"]==1 and stats["patches_restored"]


def test_receiver_rank_preserves_old_physical_best_and_earliest_empty_take_cut():
    problem,base,group,states=early_fixture()
    legs=repool._legs(problem)
    selected=tuple(legs[t][1][1] for t in (1,2))
    receiver_routes=tuple(s.routes[2] for s in states)
    producer_stats={"receiver_local_evaluations":0}
    old=repool._rank_receiver(problem,receiver_routes,2,selected,RouteEvaluator(problem),
        WEIGHTS,SCALES,16,4,perf_counter()+1.,producer_stats)
    assert old[0].index(selected[0])==2
    module=fake_module(states)
    stats={}
    with context(module,problem,stats=stats):
        start(module,problem,base,group)
        kept=module._rank_receiver(problem,receiver_routes,2,selected,RouteEvaluator(problem),
            WEIGHTS,SCALES,16,4,perf_counter()+1.,producer_stats)
        assert old[0] in kept
        assert any(r.index(selected[0])==0 for r in kept)
        assert stats["lower_bound_evaluations"]==0  # Source IN options are not instantiated here.
        assert stats["receiver_early_cut_retained"]==1


def test_direct_rebuild_delegates_all_layout_and_receiver_ranking():
    problem,base,group,states=early_fixture()
    module=fake_module(states)
    stats={}
    with context(module,problem,stats=stats):
        start(module,problem,base,group,relay=False)
        assert module._diverse(states,4)==repool._diverse(states,4)
        direct_routes=(base[2],)
        pstats={"receiver_local_evaluations":0}
        expected=repool._rank_receiver(problem,direct_routes,2,(3,4),RouteEvaluator(problem),
            WEIGHTS,SCALES,16,4,perf_counter()+1.,pstats)
        actual=module._rank_receiver(problem,direct_routes,2,(3,4),RouteEvaluator(problem),
            WEIGHTS,SCALES,16,4,perf_counter()+1.,pstats)
        assert actual==expected
        assert stats["lower_bound_evaluations"]==stats["receiver_rank_calls"]==0


def test_geometry_budget_and_cache_are_explicit_and_cannot_change_physical_keys():
    problem,base,group,states=early_fixture(stations=4)
    module=fake_module(states)
    stats={}
    with context(module,problem,stats=stats,max_lower_bound_evaluations_per_group=2):
        start(module,problem,base,group)
        module._diverse(states,8)
        module._diverse(states,8)
        assert stats["lower_bound_evaluations"]==2
        assert stats["lower_bound_cache_hits"]>0 and stats["lower_bound_limit_fallbacks"]>0
    json.dumps(stats,allow_nan=False)


def test_nested_exception_and_normal_exit_restore_all_three_functions():
    problem,base,group,states=early_fixture()
    module=fake_module(states)
    original={name:getattr(module,name) for name in joint.PATCH_NAMES}
    stats={}
    with pytest.raises(RuntimeError,match="deliberate"):
        with context(module,problem,stats=stats):
            start(module,problem,base,group)
            with pytest.raises(RuntimeError,match="nested"):
                with context(module,problem):pass
            raise RuntimeError("deliberate")
    assert all(getattr(module,name) is function for name,function in original.items())
    assert not joint._active and stats["patches_restored"]
    with context(module,problem):pass
    assert all(getattr(module,name) is function for name,function in original.items())


def test_raw_stage_deadline_is_respected_instead_of_using_outer_geometry_budget(monkeypatch):
    problem,base,group,states=early_fixture()
    clock=[0.]
    monkeypatch.setattr(joint,"perf_counter",lambda:clock[0])
    module=fake_module(states)
    stats,pstats={},{}
    with context(module,problem,deadline=20.,stats=stats):
        start(module,problem,base,group,deadline=11.,producer_stats=pstats)
        clock[0]=12.
        with pytest.raises(repool._Deadline):module._diverse(states,2)
        assert stats["deadline_reached"] and pstats["deadline_reached"]
    assert stats["patches_restored"]


def test_unsupported_old_relay_matrix_and_wrong_group_base_are_rejected():
    problem,base,group,states=early_fixture()
    matrix=tuple(tuple(r) for r in problem._distances)
    with pytest.raises(ValueError,match="Euclidean"):
        with context(fake_module(states),replace(problem,distance_matrix_km=matrix)):pass
    module=fake_module(states)
    with context(module,problem):
        with pytest.raises(ValueError,match="pure DIRECT"):
            start(module,problem,states[0].routes,group)
        wrong=replace(group,a=1,b=0)
        with pytest.raises(ValueError,match="ownership"):
            start(module,problem,base,wrong)


def test_real_tiny_mobile_pipeline_preserves_truth_and_passes_independent_new_point_audit():
    problem,base=mobile_fixture()
    stats={}
    original={name:getattr(repool,name) for name in joint.PATCH_NAMES}
    weights,scales=(.5,.25,.25),(6.,100.,100.)
    with joint.joint_layouts(repool,problem,weights=weights,scales=scales,deadline=perf_counter()+2.,statistics=stats):
        result=mobile.refine(problem,base,deadline=perf_counter()+1.8,weights=weights,scales=scales,
            max_groups=1,option_count=4,beam_width=8,full_candidates_per_group=8,raw_spec_limit=8,
            point_limit=8,pattern_rounds=1,point_eval_limit=16,full_candidate_limit=8)
        assert stats["mobile_beam_calls"]>0 and stats["receiver_early_cut_retained"]>0
    assert result["evaluation"].valid and result["metadata"]["mobile_meetings"]
    assert all(getattr(repool,name) is function for name,function in original.items())
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    audit=audit_mobile_repooling_routes(tasks,mobile.mobile.topology(problem),
        mobile.mobile.topology(result["problem"]),result["routes"],base_routes=base,
        move=result["metadata"]["mobile_meetings"][-1]["repooling_move"])
    assert audit["passed"] and audit["mobile_ground_cache_min"]==0.
    for snapshot in result["metadata"]["raw_stage"]["full_candidate_snapshots"]:
        physical=GlobalRelayEvaluator(problem).evaluate(snapshot["routes"]).score
        assert snapshot["key"]==list(repool.weighted_key(physical,weights,scales))
    json.dumps(stats,allow_nan=False)
