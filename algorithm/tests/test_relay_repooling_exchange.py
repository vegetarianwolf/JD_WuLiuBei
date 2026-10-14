"""Small physical witnesses for two-source pooling on a third aircraft."""
from dataclasses import replace
from hashlib import sha256
from math import hypot
from time import perf_counter
import json

import pytest

from uav_dispatch import Point,Problem,RelayStation,Task,TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import relay_repooling_exchange as repool


def fixture(*,asymmetric=False,capacity=2,deadline=12.,same_destination=False):
    left=-12. if asymmetric else -10.
    tasks=(Task(1,Point(-10.,0.),Point(0.,1.),deadline),
           Task(2,Point(10.,0.),Point(0.,1. if same_destination else 2.),deadline),
           Task(3,Point(0.,0.),Point(-10.,0.),100.),
           Task(4,Point(0.,0.),Point(10.,0.),100.),
           Task(5,Point(left,0.),Point(left,0.),100.),
           Task(6,Point(10.,0.),Point(10.,0.),100.))
    registry={}
    for t in tasks:
        leg=7+2*(t.id-1)
        registry[leg]=TransportLeg(leg,t.id,"RELAY_IN",1)
        registry[leg+1]=TransportLeg(leg+1,t.id,"RELAY_OUT",1)
    problem=Problem(tasks,drone_count=3,max_tasks_per_drone=2,capacity=capacity,speed_km_per_min=1.,
        drone_homes=(Point(left,0.),Point(10.,0.),Point(0.,0.)),
        relay_stations=(RelayStation(1,0.,0.),),leg_registry=registry,
        task_relay_candidates={t.id:(1,) for t in tasks})
    return problem,((5,-5,1,-1),(6,-6,2,-2),(3,4,-3,-4))


def run(problem,routes,**kwargs):
    return repool.refine(problem,routes,deadline=perf_counter()+3.,weights=(.5,.25,.25),
        scales=(len(problem.tasks),100.,100.),**kwargs)


def group():
    return repool._Group(1,2,0,1,2,3,4,())


def counters():
    return dict(rejected_invalid_partial=0,rejected_partial_coverage=0,rejected_cycles=0,global_evaluations=0)


def topology(problem):
    return dict(stations=[[s.id,s.x,s.y] for s in problem.relay_stations],
        legs=[dict(id=l.id,task_id=l.task_id,kind=l.kind,relay_id=l.relay_id) for l in problem.leg_registry.values()],
        home_points=[[p.x,p.y] for p in problem.home_points()],drone_count=problem.drone_count,
        capacity=problem.capacity,max_tasks_per_drone=problem.max_tasks_per_drone,
        speed_km_per_min=problem.speed_km_per_min)


def test_real_two_source_pooling_beats_original_and_same_final_map_direct():
    problem,routes=fixture()
    relay=run(problem,routes)
    direct=run(problem,routes,relay_enabled=False)
    initial=evaluate_solution(problem,routes)
    assert initial.valid and initial.score.late_count==0
    assert initial.score.distance_km==pytest.approx(30.+hypot(10.,1.)+hypot(10.,2.))
    assert relay["evaluation"].valid and relay["evaluation"].score.late_count==0
    assert relay["evaluation"].score.distance_km==42.
    assert relay["metadata"]["final_cross_uav_count"]==2
    assert relay["metadata"]["selected_groups"]==direct["metadata"]["selected_groups"]
    assert relay["metadata"]["selector_sha256"]==direct["metadata"]["selector_sha256"]
    assert relay["metadata"]["configuration"]==direct["metadata"]["configuration"]
    # The best complete DIRECT proposal for this same four-task final map is
    # retained even if it is worse than its original incumbent.
    fixed=[s for s in direct["metadata"]["full_candidate_snapshots"] if s["move"]["i"]==1 and s["move"]["j"]==2
           and s["move"]["k1"]==3 and s["move"]["k2"]==4]
    assert fixed
    best=min(fixed,key=lambda s:s["key"])
    assert best["score"]["distance_km"]==pytest.approx(80.2977895133337)
    assert best["score"]["late_count"]==2
    assert relay["evaluation"].score.distance_km<direct["evaluation"].score.distance_km
    assert "problem" not in relay and "problem" not in direct


def test_saturated_quota_sources_and_positive_shared_cargo_segment():
    problem,routes=fixture()
    result=run(problem,routes)
    index=build_plan_index(problem,result["routes"])
    assert [sum(a==owner for owner in index.delivery_route_by_task.values()) for a in range(3)]==[2,2,2]
    assert [index.delivery_route_by_task[t] for t in (1,2,3,4)]==[2,2,0,1]
    assert [index.inbound_route_by_task[t] for t in (1,2)]==[0,1]
    assert index.relay_task_ids==(1,2)
    assert all(index.task_plans[t].mode=="DIRECT" for t in (3,4))
    assert repool._remove(problem,result["routes"],(1,2,3,4))==repool._remove(problem,routes,(1,2,3,4))
    assert result["metadata"]["accepted_moves"][-1]["shared_double_loaded_km"]==1.
    assert repool._verify_structure(problem,routes,result["routes"],group(),1,True)==1.


def test_empty_flight_and_actual_early_receiver_wait_are_included():
    problem,routes=fixture(asymmetric=True,deadline=14.)
    result=run(problem,routes)
    schedule=GlobalRelayEvaluator(problem).evaluate(result["routes"])
    assert result["evaluation"].valid and schedule.score==result["evaluation"].score
    assert schedule.score.distance_km==44.
    assert schedule.total_waiting_min==12.
    assert schedule.delivery_times_min[1]==13. and schedule.delivery_times_min[2]==14.
    assert result["metadata"]["final_cross_uav_count"]==2


def test_independent_stdlib_geometry_dag_replay_and_all_snapshots():
    from algorithm.experiments.audit_weighted_relay_ablation import reference_schedule
    problem,routes=fixture()
    result=run(problem,routes)
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    for snapshot in result["metadata"]["full_candidate_snapshots"]:
        reference=reference_schedule(tasks,snapshot["routes"],topology(problem))
        for key in snapshot["score"]:
            assert reference["score"][key]==pytest.approx(snapshot["score"][key])
        assert reference["relay_statistics"]["cross_uav_handoff_count"]==2
        assert snapshot["valid"] and snapshot["complete"]
    final=reference_schedule(tasks,result["routes"],topology(problem))
    assert final["score"]["distance_km"]==42.
    assert final["relay_statistics"]["total_relay_waiting_time"]==10.


def test_new_independent_repooling_auditor_binds_both_arms_all_complete_snapshots():
    from algorithm.experiments.audit_repooling import audit_repooling_proposal
    problem,routes=fixture()
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    for relay_enabled in (False,True):
        result=run(problem,routes,relay_enabled=relay_enabled)
        assert result["metadata"]["full_candidate_snapshots"]
        for snapshot in result["metadata"]["full_candidate_snapshots"]:
            audit=audit_repooling_proposal(tasks,topology(problem),routes,snapshot["routes"],snapshot["move"],
                                          relay_enabled=relay_enabled)
            assert audit["passed"]
            assert audit["actual_cross_uav_count"]==(2 if relay_enabled else 0)
            assert audit["final_tasks_per_aircraft"]==[2,2,2]
            assert audit["shared_loaded_distance_km"]==pytest.approx(snapshot["move"]["shared_double_loaded_km"])
            for key in snapshot["score"]:
                assert audit["score"][key]==pytest.approx(snapshot["score"][key])


def test_global_partial_never_receives_half_relays_or_wrong_task_set(monkeypatch):
    problem,routes=fixture()
    original=repool.GlobalRelayEvaluator.evaluate
    present=[]
    def evaluate(self,candidate):
        index=build_plan_index(self.problem,candidate)
        assert all(p.mode=="DIRECT" or len(p.leg_ids)==2 for p in index.task_plans.values())
        present.append(set(index.task_plans))
        return original(self,candidate)
    monkeypatch.setattr(repool.GlobalRelayEvaluator,"evaluate",evaluate)
    result=run(problem,routes)
    assert result["evaluation"].valid
    assert {5,6} in present and {1,2,5,6} in present and {3,4,5,6} in present
    stats=counters()
    assert repool._partial(problem,((5,-5,7,-7),(6,-6),()),{1,5,6},GlobalRelayEvaluator(problem),stats) is None
    assert stats["global_evaluations"]==0
    stats=counters()
    assert repool._partial(problem,((5,-5),(6,-6),()),{1,5,6},GlobalRelayEvaluator(problem),stats) is None
    assert stats["rejected_partial_coverage"]==1
    for record in result["metadata"]["group_records"]:
        for pipeline in record["pipelines"]:
            assert pipeline["stages"][-1]["present_tasks"]==list(problem.task_ids)


def test_all_stations_retained_by_beam_and_fleet_geometry_unchanged():
    problem,routes=fixture()
    stations=(*problem.relay_stations,RelayStation(2,0.,.2),RelayStation(3,0.,-.2),RelayStation(4,.2,0.))
    registry=dict(problem.leg_registry)
    next_id=max(registry)+1
    for t in problem.tasks:
        for h in stations[1:]:
            registry[next_id]=TransportLeg(next_id,t.id,"RELAY_IN",h.id)
            registry[next_id+1]=TransportLeg(next_id+1,t.id,"RELAY_OUT",h.id)
            next_id+=2
    extended=replace(problem,relay_stations=stations,leg_registry=registry,
                     task_relay_candidates={t.id:(1,2,3,4) for t in problem.tasks})
    result=run(extended,routes,max_groups=2)
    assert result["evaluation"].valid
    assert any(stage["station_families"]==[1,2,3,4] for record in result["metadata"]["group_records"]
               for pipeline in record["pipelines"] for stage in pipeline["stages"])
    assert extended.home_points()==problem.home_points()
    assert result["metadata"]["configuration"]["event_cap"] is None
    by_group={}
    for snapshot in result["metadata"]["full_candidate_snapshots"]:
        move=snapshot["move"]
        key=tuple(move[k] for k in ("i","j","a","b","c","k1","k2"))
        by_group.setdefault(key,set()).add(move["station_id"])
    assert any(stations=={1,2,3,4} for stations in by_group.values())


def test_relay_arm_does_not_adopt_a_direct_rebuild_when_no_pooling_possible():
    problem,routes=fixture(capacity=1)
    # Build an initially legal C1 native route rather than the C2 input.
    routes=(routes[0],routes[1],(3,-3,4,-4))
    result=run(problem,routes)
    assert result["routes"]==routes and result["evaluation"].valid
    assert result["metadata"]["complete_proposals"]==0
    assert result["metadata"]["accepted_improvements"]==0


def test_weighted_key_and_final_candidate_guard_use_frozen_scales():
    problem,routes=fixture(deadline=10.)
    result=repool.refine(problem,routes,deadline=perf_counter()+3.,weights=(.1,.1,.8),scales=(6.,100.,100.))
    assert result["evaluation"].valid and result["metadata"]["final_key"]<result["metadata"]["initial_key"]
    score=result["evaluation"].score
    assert result["metadata"]["final_key"][0]==pytest.approx(.1*score.late_count/6.+.1*score.total_lateness_min/100.+.8*score.distance_km/100.)


@pytest.mark.parametrize("phase",["expired","partial","full"])
def test_never_accept_work_finished_after_deadline(monkeypatch,phase):
    problem,routes=fixture()
    clock=[2. if phase=="expired" else 0.]
    monkeypatch.setattr(repool,"perf_counter",lambda:clock[0])
    original_partial,original_full=repool._partial,repool.evaluate_solution
    calls=[0]
    def partial(*args,**kwargs):
        result=original_partial(*args,**kwargs)
        if phase=="partial" and len(args[2])>2:clock[0]=2.
        return result
    def full(*args,**kwargs):
        result=original_full(*args,**kwargs)
        calls[0]+=1
        if phase=="full" and calls[0]>1:clock[0]=2.
        return result
    monkeypatch.setattr(repool,"_partial",partial)
    monkeypatch.setattr(repool,"evaluate_solution",full)
    result=repool.refine(problem,routes,deadline=1.,weights=(.5,.25,.25),scales=(6.,100.,100.))
    assert result["routes"]==routes and result["metadata"]["accepted_improvements"]==0
    assert result["metadata"]["deadline_reached"]
    if phase=="expired":
        assert result["metadata"]["selected_groups"]==[]
        assert result["metadata"]["selector_sha256"]==sha256(b"[]").hexdigest()
        assert not result["metadata"]["selector_completed"]
    if phase=="full":assert calls[0]>1


def test_json_evidence_and_selector_are_seed_reproducible():
    problem,routes=fixture()
    first,second=run(problem,routes,seed=91),run(problem,routes,seed=91)
    assert first["routes"]==second["routes"]
    assert first["metadata"]["selector_sha256"]==second["metadata"]["selector_sha256"]
    assert first["metadata"]["first_complete_candidate"]==second["metadata"]["first_complete_candidate"]
    json.dumps(first["metadata"],allow_nan=False)
    for move in first["metadata"]["accepted_moves"]:
        assert move["full_verified_at_perf_counter"]<=move["accepted_at_perf_counter"]<first["metadata"]["deadline_perf_counter"]


def test_shared_selector_can_be_frozen_and_reused_across_arms():
    problem,routes=fixture()
    selected=run(problem,routes)["metadata"]["selected_groups"]
    direct=run(problem,routes,relay_enabled=False,common_selector_records=selected)
    relay=run(problem,routes,common_selector_records=selected)
    assert direct["metadata"]["selector_sha256"]==relay["metadata"]["selector_sha256"]
    assert direct["metadata"]["selector_reused"] and relay["metadata"]["selector_reused"]
    assert direct["metadata"]["selector_completed"] and relay["metadata"]["selector_completed"]
    assert direct["metadata"]["selector_insertion_calls"]==0
    assert relay["evaluation"].score.distance_km==42.
    broken=[dict(selected[0],c=selected[0]["a"])]
    with pytest.raises(ValueError,match="ownership"):
        run(problem,routes,common_selector_records=broken)


def test_actual_handoff_cycle_is_rejected_before_adoption():
    problem,_=fixture()
    candidate=((10,-10,7,-7,5,-5),(8,-8,9,-9,6,-6),(3,4,-3,-4))
    stats=counters()
    assert repool._partial(problem,candidate,set(problem.task_ids),GlobalRelayEvaluator(problem),stats) is None
    assert stats["rejected_cycles"]==1


def test_zero_distance_common_take_drop_is_not_reported_as_pooling():
    problem,_=fixture()
    changed=replace(problem,tasks=tuple(replace(t,delivery=Point(0.,0.)) if t.id in (1,2) else t for t in problem.tasks))
    local=repool.RouteEvaluator(changed)
    state=repool._State(((),(),()),(),None)
    stats=dict(receiver_relay_layouts=0,receiver_local_evaluations=0,deadline_reached=False)
    assert repool._receiver_relay(changed,state,group(),8,10,local,(.5,.25,.25),(6.,100.,100.),
                                 16,2,perf_counter()+1.,stats)==()


def test_direct_shared_cargo_distance_is_saved_as_actual_geometry():
    problem,routes=fixture()
    result=run(problem,routes,relay_enabled=False)
    for snapshot in result["metadata"]["full_candidate_snapshots"]:
        move=snapshot["move"]
        actual=repool._shared_distance(problem,snapshot["routes"][move["c"]],move["i"],move["j"],move["c"])
        assert move["shared_double_loaded_km"]==pytest.approx(actual)


def test_invalid_or_relay_input_and_invalid_configuration_rejected():
    problem,routes=fixture()
    for change in (dict(max_groups=0),dict(receiver_limit=True),dict(position_limit=3),
                   dict(beam_width=3),dict(relay_enabled=1)):
        with pytest.raises(ValueError):run(problem,routes,**change)
    with pytest.raises(ValueError,match="invalid initial"):
        run(problem,((),(),()))
    result=run(problem,routes)
    with pytest.raises(ValueError,match="pure DIRECT"):
        run(problem,result["routes"])


def test_capacity_windows_can_retain_unselected_interleaving():
    problem,routes=fixture()
    # A native C task starts after the two common takes, so both Ds must be
    # placed by its pickup overflow, not by validating a missing-D suffix.
    local=repool.RouteEvaluator(problem)
    state=repool._State(((),(),(5,-5)),(),None)
    stats=dict(receiver_relay_layouts=0,receiver_local_evaluations=0,deadline_reached=False)
    candidates=repool._receiver_relay(problem,state,group(),8,10,local,(.5,.25,.25),(6.,100.,100.),
        16,2,perf_counter()+1.,stats)
    assert candidates
    for candidate in candidates:
        assert repool._capacity(problem,candidate)
        assert tuple(v for v in candidate if abs(v) not in (8,10))==(5,-5)
        assert candidate.index(10)==candidate.index(8)+1
        assert repool._shared_distance(problem,candidate,8,10,2)>0.
