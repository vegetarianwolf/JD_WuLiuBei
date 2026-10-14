"""Tiny real one-way relay gains with a matched quota-preserving DIRECT arm."""
from dataclasses import replace
import json
from time import perf_counter

import pytest

from uav_dispatch import Point,Problem,RelayStation,Task,TransportLeg
from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import quota_balanced_relay_exchange as exchange


def fixture(*,asymmetric=False,local_x=0.,local_deadline=100.,i_deadline=100.,capacity=2):
    tasks=(Task(1,Point(0.,0.),Point(10.,0.),i_deadline),
           Task(2,Point(0.,0.),Point(0.,0.),100.),
           Task(3,Point(local_x,0.),Point(local_x,0.),local_deadline),
           Task(4,Point(10.,0.),Point(10.,0.),100.))
    registry={}
    for task in tasks:
        leg=5+2*(task.id-1)
        registry[leg]=TransportLeg(leg,task.id,"RELAY_IN",1)
        registry[leg+1]=TransportLeg(leg+1,task.id,"RELAY_OUT",1)
    problem=Problem(tasks,drone_count=2,max_tasks_per_drone=2,capacity=capacity,
        speed_km_per_min=1.,drone_homes=(Point(-2. if asymmetric else 0.,0.),Point(10.,0.)),
        relay_stations=(RelayStation(1,5.,0.),),leg_registry=registry,
        task_relay_candidates={t.id:(1,) for t in tasks})
    return problem,((3,-3,1,-1),(4,-4,2,-2))


def run(problem,routes,**kwargs):
    return exchange.refine(problem,routes,deadline=perf_counter()+2.,weights=(.5,.25,.25),
        scales=(len(problem.tasks),100.,100.),**kwargs)


def counters():
    return dict(rejected_invalid_partial=0,rejected_partial_coverage=0,rejected_cycles=0,global_evaluations=0)


def topology(problem):
    return dict(stations=[[s.id,s.x,s.y] for s in problem.relay_stations],
        legs=[dict(id=l.id,task_id=l.task_id,kind=l.kind,relay_id=l.relay_id) for l in problem.leg_registry.values()],
        home_points=[[p.x,p.y] for p in problem.home_points()],drone_count=problem.drone_count,
        capacity=problem.capacity,max_tasks_per_drone=problem.max_tasks_per_drone,
        speed_km_per_min=problem.speed_km_per_min)


def test_true_one_way_relay_beats_strong_input_and_matching_direct_control():
    problem,routes=fixture()
    direct=run(problem,routes,relay_enabled=False)
    relay=run(problem,routes,relay_enabled=True)
    assert evaluate_solution(problem,routes).score.distance_km==20.
    assert direct["evaluation"].valid and direct["evaluation"].score.distance_km==20.
    assert relay["evaluation"].valid and relay["evaluation"].score.distance_km==15.
    assert relay["metadata"]["final_cross_uav_count"]==1
    assert direct["metadata"]["selected_pairs"]==relay["metadata"]["selected_pairs"]
    assert direct["metadata"]["configuration"]==relay["metadata"]["configuration"]
    assert "problem" not in relay and "problem" not in direct


def test_final_task_slots_are_saturated_and_only_i_relays_while_j_is_direct():
    problem,routes=fixture()
    result=run(problem,routes)
    final=build_plan_index(problem,result["routes"])
    assert [sum(owner==i for owner in final.delivery_route_by_task.values()) for i in range(2)]==[2,2]
    assert final.delivery_route_by_task[1]==1 and final.delivery_route_by_task[2]==0
    assert final.task_plans[1].mode=="RELAY" and final.task_plans[2].mode=="DIRECT"
    assert final.inbound_route_by_task[1]==0 and final.outbound_route_by_task[1]==1
    assert final.relay_task_ids==(1,)
    move=result["metadata"]["accepted_moves"][-1]
    assert move["i"]==1 and move["j"]==2 and move["j_mode"]=="DIRECT"
    assert exchange._remove(problem,result["routes"],(1,2))==exchange._remove(problem,routes,(1,2))


def test_home_empty_flights_and_true_transfer_wait_are_globally_counted():
    problem,routes=fixture(asymmetric=True)
    result=run(problem,routes)
    schedule=GlobalRelayEvaluator(problem).evaluate(result["routes"])
    assert result["evaluation"].valid and schedule.score==result["evaluation"].score
    assert schedule.score.distance_km==17.
    assert schedule.total_waiting_min==2.
    assert schedule.delivery_times_min[1]==12.
    assert schedule.delivery_times_min[2]==2.


def test_independent_official_format_dag_replay_matches_complete_solution():
    from algorithm.experiments.audit_weighted_relay_ablation import reference_schedule
    problem,routes=fixture(asymmetric=True)
    result=run(problem,routes)
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    reference=reference_schedule(tasks,result["routes"],topology(problem))
    assert reference["score"]["distance_km"]==17.
    assert reference["relay_statistics"]["cross_uav_handoff_count"]==1
    assert reference["relay_statistics"]["total_relay_waiting_time"]==2.


def test_global_scorer_never_receives_an_incomplete_relay_leg(monkeypatch):
    problem,routes=fixture()
    original=exchange.GlobalRelayEvaluator.evaluate
    observed=[]
    def evaluate(self,candidate):
        index=build_plan_index(self.problem,candidate)
        assert all(plan.mode=="DIRECT" or len(plan.leg_ids)==2 for plan in index.task_plans.values())
        observed.append(len(index.task_plans))
        return original(self,candidate)
    monkeypatch.setattr(exchange.GlobalRelayEvaluator,"evaluate",evaluate)
    result=run(problem,routes)
    assert 2 in observed and 3 in observed and 4 in observed
    assert result["evaluation"].valid and len(result["evaluation"].delivery_times_min)==4
    stats=counters()
    assert exchange._partial(problem,((3,-3,5,-5),(4,-4)),{1,3,4},
        GlobalRelayEvaluator(problem),stats) is None
    assert stats["global_evaluations"]==0 and stats["rejected_invalid_partial"]==1


def test_cycles_with_an_existing_opposite_handoff_are_rejected():
    problem,_=fixture()
    # New i=1 travels A->B, old task3 B->A. Each receive precedes the other
    # aircraft's drop: complete plans, legal loads, actual dependency cycle.
    candidate=((10,-10,2,-2,5,-5),(4,-4,6,-6,9,-9))
    stats=counters()
    assert exchange._partial(problem,candidate,{1,2,3,4},GlobalRelayEvaluator(problem),stats) is None
    assert stats["rejected_cycles"]==1


def test_weighted_acceptance_can_choose_a_more_late_but_shorter_complete_plan():
    problem,routes=fixture(local_x=2.,local_deadline=2.,i_deadline=14.,capacity=1)
    on_time=run(problem,routes)
    distance=exchange.refine(problem,routes,deadline=perf_counter()+2.,weights=(.001,.001,.998),
                            scales=(1.,100.,100.))
    assert on_time["evaluation"].valid and on_time["evaluation"].score.late_count==0
    assert distance["evaluation"].valid and distance["evaluation"].score.late_count==1
    assert distance["evaluation"].score.distance_km<on_time["evaluation"].score.distance_km
    assert distance["metadata"]["final_key"]<distance["metadata"]["initial_key"]


def test_capacity_one_remains_a_valid_one_way_exchange():
    problem,routes=fixture(capacity=1)
    result=run(problem,routes)
    assert result["evaluation"].valid and result["evaluation"].score.distance_km==15.
    for route in result["routes"]:
        load=0
        for visit in route:
            load+=1 if visit>0 else -1
            assert 0<=load<=1


def test_all_existing_registered_stations_are_considered_without_event_caps():
    problem,routes=fixture()
    stations=(*problem.relay_stations,RelayStation(2,6.,0.),RelayStation(3,4.,0.),RelayStation(4,5.,1.))
    registry=dict(problem.leg_registry)
    next_id=max(registry)+1
    for task in problem.tasks:
        for station in stations[1:]:
            registry[next_id]=TransportLeg(next_id,task.id,"RELAY_IN",station.id)
            registry[next_id+1]=TransportLeg(next_id+1,task.id,"RELAY_OUT",station.id)
            next_id+=2
    full=replace(problem,relay_stations=stations,leg_registry=registry,
                 task_relay_candidates={t.id:(1,2,3,4) for t in problem.tasks})
    result=run(full,routes)
    assert result["evaluation"].valid and result["metadata"]["configuration"]["event_cap"] is None
    assert any(record["stages"][0]["relay_station_families"]==[1,2,3,4]
               for record in result["metadata"]["pair_records"])
    assert full.home_points()==problem.home_points()


def test_expired_budget_returns_only_original_complete_incumbent():
    problem,routes=fixture()
    result=exchange.refine(problem,routes,deadline=perf_counter()-1.,weights=(.5,.25,.25),scales=(4.,100.,100.))
    assert result["routes"]==routes and result["evaluation"].valid
    assert result["iterations"]==0 and result["metadata"]["deadline_reached"]


@pytest.mark.parametrize("phase",["partial","full"])
def test_complete_candidate_finished_after_deadline_is_not_adopted(monkeypatch,phase):
    problem,routes=fixture()
    clock=[0.]
    monkeypatch.setattr(exchange,"perf_counter",lambda:clock[0])
    original_partial,original_full=exchange._partial,exchange.evaluate_solution
    calls=[0]
    def partial(*args,**kwargs):
        result=original_partial(*args,**kwargs)
        if phase=="partial" and len(args[2])==3:clock[0]=2.
        return result
    def full(*args,**kwargs):
        result=original_full(*args,**kwargs)
        calls[0]+=1
        if phase=="full" and calls[0]>1:clock[0]=2.
        return result
    monkeypatch.setattr(exchange,"_partial",partial)
    monkeypatch.setattr(exchange,"evaluate_solution",full)
    result=exchange.refine(problem,routes,deadline=1.,weights=(.5,.25,.25),scales=(4.,100.,100.))
    assert result["routes"]==routes and result["metadata"]["accepted_improvements"]==0
    assert result["metadata"]["deadline_reached"]
    if phase=="full":assert calls[0]>1


def test_complete_snapshot_schema_and_seed_are_reproducible_plain_json():
    problem,routes=fixture()
    first,second=run(problem,routes,seed=51),run(problem,routes,seed=51)
    assert first["routes"]==second["routes"]
    assert first["metadata"]["selected_pairs"]==second["metadata"]["selected_pairs"]
    assert first["metadata"]["first_complete_candidate"]==second["metadata"]["first_complete_candidate"]
    for snapshot in first["metadata"]["full_candidate_snapshots"]:
        assert snapshot["complete"] and snapshot["valid"]
        assert evaluate_solution(problem,snapshot["routes"],_force_full=True).valid
    json.dumps(first["metadata"],allow_nan=False)


def test_invalid_input_configuration_and_objective_are_rejected():
    problem,routes=fixture()
    for change in (dict(max_pairs=0),dict(position_limit=2),dict(beam_width=1),
                   dict(option_count=True),dict(relay_enabled=1)):
        with pytest.raises(ValueError):run(problem,routes,**change)
    with pytest.raises(ValueError,match="invalid initial"):
        run(problem,((1,-1),()))
    a,b=Score(0,0.,3.),Score(1,0.,0.)
    assert a<b and exchange.weighted_key(b,(.5,.25,.25),(1.,1.,1.))<exchange.weighted_key(a,(.5,.25,.25),(1.,1.,1.))
