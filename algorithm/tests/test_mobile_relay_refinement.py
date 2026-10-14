"""Tiny physical airborne rendezvous tests; no official instance is solved."""
from dataclasses import replace
import json
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import mobile_relay_refinement as mobile


def fixture(*, paired=False, far_station=True, asymmetric=False):
    if not paired:
        tasks=(Task(1,Point(0.,0.),Point(10.,0.),100.),
               Task(2,Point(10.,0.),Point(0.,0.),100.),
               Task(3,Point(0.,0.),Point(0.,0.),100.),
               Task(4,Point(10.,0.),Point(10.,0.),100.))
        routes=((1,-1,3,-3),(2,-2,4,-4))
    else:
        tasks=(Task(1,Point(0.,0.),Point(10.,0.),100.),
               Task(2,Point(1.,0.),Point(11.,0.),100.),
               Task(3,Point(10.,0.),Point(0.,0.),100.),
               Task(4,Point(11.,0.),Point(1.,0.),100.),
               Task(5,Point(0.,0.),Point(0.,0.),100.),
               Task(6,Point(1.,0.),Point(1.,0.),100.),
               Task(7,Point(10.,0.),Point(10.,0.),100.),
               Task(8,Point(11.,0.),Point(11.,0.),100.))
        routes=((1,2,-1,-2,5,6,-5,-6),(3,4,-3,-4,7,8,-7,-8))
    registry={}
    if far_station:
        for task in tasks:
            leg=len(tasks)+1+2*(task.id-1)
            registry[leg]=TransportLeg(leg,task.id,"RELAY_IN",1)
            registry[leg+1]=TransportLeg(leg+1,task.id,"RELAY_OUT",1)
    problem=Problem(tasks,drone_count=2,max_tasks_per_drone=4 if paired else 2,capacity=2,
        speed_km_per_min=1.,drone_homes=(Point(-2. if asymmetric else 0.,0.),Point(10.,0.)),
        relay_stations=(RelayStation(1,5.,100.),) if far_station else (),
        leg_registry=registry,task_relay_candidates={t.id:(1,) for t in tasks} if far_station else {})
    return problem,routes


def run(problem,routes,**kwargs):
    return mobile.refine(problem,routes,deadline=perf_counter()+3.,weights=(.5,.25,.25),
                         scales=(len(problem.tasks),100.,100.),**kwargs)


def prototype(problem,routes,*,paired=False,point=Point(5.,0.)):
    schedule=GlobalRelayEvaluator(problem).evaluate(routes)
    position=2 if paired else 1
    left=next(c for c in mobile._cuts(problem,routes[0],0,schedule) if c.position==position)
    right=next(c for c in mobile._cuts(problem,routes[1],1,schedule) if c.position==position)
    a,b=(1,2) if paired else (1,),(3,4) if paired else (2,)
    spec=mobile._Spec(0,1,left,right,a,b,b,a,(0.,))
    candidate,legs,descriptor=mobile._prototype(problem,routes,spec,point)
    changed=mobile._problem(problem,legs,descriptor)
    return candidate,legs,descriptor,changed


def test_mobile_rendezvous_avoids_fixed_station_detour_and_improves_true_cross():
    problem,routes=fixture()
    initial=evaluate_solution(problem,routes,_force_full=True)
    result=run(problem,routes)
    assert initial.score.distance_km==40.
    assert result["evaluation"].valid and result["evaluation"].score.distance_km==20.
    assert result["metadata"]["final_cross_uav_count"]==2
    assert result["metadata"]["accepted_improvements"]>=1
    assert len(result["problem"].relay_stations)==len(problem.relay_stations)+1
    assert result["problem"].relay_stations[0]==problem.relay_stations[0]
    assert result["metadata"]["model_extension"]=="continuous_airborne_rendezvous"
    assert result["metadata"]["mobile_meetings"][0]["point"][1]==0.


def test_resource_conservation_final_k_and_unchanged_other_events():
    problem,routes=fixture()
    result=run(problem,routes)
    changed=result["problem"]
    assert changed.tasks==problem.tasks and changed.home_points()==problem.home_points()
    assert (changed.drone_count,changed.capacity,changed.max_tasks_per_drone)==(2,2,2)
    assert all(changed.leg_registry[leg]==value for leg,value in problem.leg_registry.items())
    index=build_plan_index(changed,result["routes"])
    assert [sum(owner==i for owner in index.delivery_route_by_task.values()) for i in range(2)]==[2,2]
    assert index.delivery_route_by_task[1]==1 and index.delivery_route_by_task[2]==0
    assert index.delivery_route_by_task[3]==0 and index.delivery_route_by_task[4]==1
    for old,new in zip(routes,result["routes"]):
        assert tuple(v for v in old if abs(v) not in (1,2))==tuple(v for v in new
            if ((changed.leg_registry[abs(v)].task_id if abs(v) in changed.leg_registry else abs(v)) not in (1,2)))


def test_early_aircraft_holds_cargo_until_synchronized_drop_and_take():
    problem,routes=fixture(asymmetric=True)
    candidate,legs,descriptor,changed=prototype(problem,routes)
    schedule=GlobalRelayEvaluator(changed).evaluate(candidate)
    meetings=mobile._physical_meetings(changed,candidate,[descriptor],schedule)
    meeting=meetings[0]
    assert meeting["arrival_times_min"]==[7.,5.]
    assert meeting["synchronization_min"]==7.
    assert meeting["aircraft_waiting_min"]==[0.,2.]
    assert all(event["physical_time_min"]==7. for event in meeting["drop_events"]+meeting["pickup_events"])
    early=next(e for e in meeting["drop_events"] if e["route_index"]==1)
    assert schedule.event_times_min[(1,early["position"])]==5.
    assert schedule.event_times_min[(1,meeting["pickup_events"][-1]["position"])]==7.
    assert evaluate_solution(changed,candidate,_force_full=True).valid


def test_two_by_two_exchange_is_one_hop_contiguous_and_capacity_two():
    problem,routes=fixture(paired=True)
    candidate,legs,descriptor,changed=prototype(problem,routes,paired=True)
    checked=evaluate_solution(changed,candidate,_force_full=True)
    assert checked.valid and checked.score.distance_km<evaluate_solution(problem,routes).score.distance_km
    schedule=GlobalRelayEvaluator(changed).evaluate(candidate)
    meeting=mobile._physical_meetings(changed,candidate,[descriptor],schedule)[0]
    assert meeting["group_size"]==2 and len(meeting["drop_events"])==len(meeting["pickup_events"])==4
    assert meeting["arrival_times_min"]==[5.,7.] and meeting["aircraft_waiting_min"]==[2.,0.]
    index=build_plan_index(changed,candidate)
    assert len(index.relay_task_ids)==4
    for task_id in index.relay_task_ids:
        assert len(index.task_plans[task_id].leg_ids)==2
    for route in candidate:
        load=0
        for visit in route:
            load+=1 if visit>0 else -1
            assert 0<=load<=2
        assert load==0


def test_fast_complete_score_matches_global_without_old_waits():
    problem,routes=fixture(paired=True)
    candidate,legs,descriptor,changed=prototype(problem,routes,paired=True)
    lower=mobile._fast_lower_score(problem,candidate,legs,Point(5.,0.),descriptor)
    checked=evaluate_solution(changed,candidate,_force_full=True)
    assert checked.valid and lower==checked.score


def test_swapped_successor_endpoints_and_geometric_candidates_are_physical():
    intersection=mobile._intersections(Point(0.,0.),Point(10.,10.),Point(0.,10.),Point(10.,0.))
    assert intersection==(Point(5.,5.),)
    overlap=mobile._intersections(Point(0.,0.),Point(10.,0.),Point(3.,0.),Point(8.,0.))
    assert overlap==(Point(3.,0.),Point(5.5,0.),Point(8.,0.))
    assert mobile._geometric_median((Point(0.,0.),)*3+(Point(10.,0.),)).distance_to(Point(0.,0.))<1e-6
    assert mobile._earliest_meeting(Point(0.,0.),Point(10.,0.),2.,0.,1.)==Point(4.,0.)
    assert mobile._earliest_meeting(Point(0.,0.),Point(10.,0.),20.,0.,1.)==Point(0.,0.)
    problem,routes=fixture()
    schedule=GlobalRelayEvaluator(problem).evaluate(routes)
    left=mobile._cuts(problem,routes[0],0,schedule)[0]
    mapping=mobile._slot_map(routes[0],left,(1,),(2,))
    # The cut's next event is its old delivery slot, so the appropriate new
    # successor is the other parcel's destination, here (0,0), not (10,0).
    assert mapping[1]==2 and problem.task(mapping[-routes[0][left.position]]).delivery==Point(0.,0.)


def test_existing_relay_cargo_is_never_used_for_second_hop():
    problem,routes=fixture()
    candidate,_,_,changed=prototype(problem,routes)
    schedule=GlobalRelayEvaluator(changed).evaluate(candidate)
    cuts=[mobile._cuts(changed,r,i,schedule) for i,r in enumerate(candidate)]
    assert all(all(v not in changed.leg_registry for v in c.active) for per_route in cuts for c in per_route)
    assert all(set(c.active).isdisjoint({1,2}) for per_route in cuts for c in per_route)


def test_expired_budget_does_not_change_problem_routes_or_topology():
    problem,routes=fixture()
    result=mobile.refine(problem,routes,deadline=perf_counter()-1.,weights=(.5,.25,.25),scales=(4.,100.,100.))
    assert result["problem"] is problem and result["routes"]==routes
    assert result["metadata"]["accepted_improvements"]==0
    assert result["metadata"]["deadline_reached"]


@pytest.mark.parametrize("expiration",["problem","global","full"])
def test_over_deadline_candidate_is_not_adopted(monkeypatch,expiration):
    problem,routes=fixture()
    clock=[0.]
    monkeypatch.setattr(mobile,"perf_counter",lambda:clock[0])
    original_problem=mobile._problem
    original_global=mobile.GlobalRelayEvaluator.evaluate
    original_full=mobile.evaluate_solution
    full_calls=[0]
    def build(*args):
        result=original_problem(*args)
        if expiration=="problem":clock[0]=2.
        return result
    def global_call(self,candidate):
        result=original_global(self,candidate)
        if expiration=="global" and len(self.problem.relay_stations)>len(problem.relay_stations):clock[0]=2.
        return result
    def full(*args,**kwargs):
        result=original_full(*args,**kwargs)
        full_calls[0]+=1
        if expiration=="full" and full_calls[0]>1:clock[0]=2.
        return result
    monkeypatch.setattr(mobile,"_problem",build)
    monkeypatch.setattr(mobile.GlobalRelayEvaluator,"evaluate",global_call)
    monkeypatch.setattr(mobile,"evaluate_solution",full)
    result=mobile.refine(problem,routes,deadline=1.,weights=(.5,.25,.25),scales=(4.,100.,100.))
    assert result["problem"] is problem and result["routes"]==routes
    assert result["metadata"]["accepted_improvements"]==0 and result["metadata"]["deadline_reached"]


def test_snapshots_bind_complete_topology_and_sync_metadata_as_plain_json():
    problem,routes=fixture()
    result=run(problem,routes)
    metadata=result["metadata"]
    assert metadata["full_candidate_snapshots"] and metadata["accepted_moves"]
    for snapshot in metadata["full_candidate_snapshots"]:
        assert snapshot["valid"] and snapshot["complete"] and snapshot["mobile_meetings"]
        assert snapshot["topology"]["home_points"]==[[0.,0.],[10.,0.]]
        assert snapshot["topology"]["drone_count"]==2
        assert len(snapshot["topology"]["stations"])==2
    assert metadata["shortlist_coverage"][0]["distinct_task_group_pairs"]>0
    assert metadata["shortlist_coverage"][0]["distinct_route_pairs"]==1
    json.dumps(metadata,allow_nan=False)


def test_tiny_solution_and_physical_metadata_pass_independent_geometry_dag_audit():
    from algorithm.experiments.audit_mobile_relay import audit_mobile_routes, _bind_meetings
    problem,routes=fixture()
    result=run(problem,routes)
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    audited=audit_mobile_routes(tasks,mobile.topology(problem),mobile.topology(result["problem"]),result["routes"])
    assert audited["score"]["distance_km"]==result["evaluation"].score.distance_km
    _bind_meetings(result["metadata"]["mobile_meetings"],audited["meetings"])


def test_unsupported_custom_metric_preserves_checked_input_without_invented_edges():
    problem,routes=fixture()
    custom=replace(problem,distance_matrix_km=problem._distances)
    result=run(custom,routes)
    assert result["problem"] is custom and result["routes"]==routes
    assert result["metadata"]["unsupported_non_euclidean_matrix"]
    assert result["metadata"]["new_problems_built"]==0


def test_invalid_inputs_configuration_and_seed_reproducibility():
    problem,routes=fixture()
    first,second=run(problem,routes),run(problem,routes)
    assert first["routes"]==second["routes"]
    assert first["metadata"]["mobile_meetings"]==second["metadata"]["mobile_meetings"]
    for options in (dict(max_pairs=0),dict(point_limit=True),dict(max_moves=0),dict(full_candidate_limit=0)):
        with pytest.raises(ValueError):run(problem,routes,**options)
    with pytest.raises(ValueError,match="invalid initial"):
        run(problem,((1,-1),()))
