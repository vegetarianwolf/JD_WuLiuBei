"""Finite synthetic route clocks; no official optimizer or global scorer."""
from dataclasses import replace
from itertools import combinations
from math import sqrt
from time import perf_counter

import pytest

from uav_dispatch.model import Point, Problem, RelayStation, Score, Task, TransportLeg
from algorithm.experiments import six_task_route_costs as costs


W, S = (.5,.25,.25), (6.,100.,100.)


def fixture():
    a,b,c = Point(0.,0.),Point(2.,0.),Point(1.,sqrt(3.))
    points = (a,b,c)
    pairs = ((0,1),(0,2),(1,0),(1,2),(2,0),(2,1))
    tasks = tuple(Task(t,points[x],points[y],2.6) for t,(x,y) in enumerate(pairs,1))
    registry = {identifier:TransportLeg(identifier,t,kind,9) for t in range(1,7)
                for identifier,kind in ((100+2*t,"RELAY_IN"),(101+2*t,"RELAY_OUT"))}
    return Problem(tasks,drone_count=3,capacity=2,max_tasks_per_drone=2,speed_km_per_min=1.,
        drone_homes=points,relay_stations=(RelayStation(9,1.,sqrt(3.)/3.),),leg_registry=registry,
        task_relay_candidates={t:(9,) for t in range(1,7)})


def args():
    return dict(deadline=perf_counter()+2.,weights=W,scales=S)


def ins(problem, aircraft=0, pair=(1,2), native=()):
    return costs.in_options(problem,native,aircraft,pair,9,**args())


def test_triangle_true_local_route_tables_obtain_six_task_cross_sorting_mechanism():
    problem=fixture()
    sources=[ins(problem,a,pair)[0] for a,pair in enumerate(((1,2),(3,4),(5,6)))]
    available={t:clock for r in sources for t,clock in r["availability"].items()}
    radius=2./sqrt(3.)
    assert all(clock==pytest.approx(radius) for clock in available.values())
    recipients=((3,5),(1,6),(2,4))
    outputs=[costs.out_option(problem,source,a,recipients[a],9,available,**args())
             for a,source in enumerate(sources)]
    assert all(r is not None and not r["complete_solution"] for r in outputs)
    assert sum(r["score"].late_count for r in outputs)==0
    assert sum(r["score"].distance_km for r in outputs)==pytest.approx(6.*radius)
    assert all(r["shared_loaded_distance_km"]==pytest.approx(radius) for r in outputs)
    assert all(all(time==pytest.approx(2.*radius) for time in r["delivery_times"].values()) for r in outputs)
    assert all(r["load_before"][r["pickup_position"]]==0 for r in outputs)


def test_triangle_all_ninety_bounded_direct_final_maps_still_need_two_edges_per_uav():
    problem=fixture()
    table={(a,pair):costs.direct_option(problem,(),a,pair,**args())
           for a in range(3) for pair in combinations(range(1,7),2)}
    scores=[]
    tasks=set(range(1,7))
    for first in combinations(sorted(tasks),2):
        for second in combinations(sorted(tasks-set(first)),2):
            third=tuple(sorted(tasks-set(first)-set(second)))
            scores.append(table[0,first]["score"]+table[1,second]["score"]+table[2,third]["score"])
    assert len(scores)==90
    assert min(s.late_count for s in scores)==3
    assert min(s.distance_km for s in scores)==pytest.approx(12.)


def test_in_source_score_preserves_physical_best_and_early_drop_and_never_counts_in_lateness():
    problem=fixture()
    native=Task(7,Point(0.,0.),Point(-1.,0.),1.1)
    problem=replace(problem,tasks=tuple(replace(t,deadline_min=.001) for t in problem.tasks)+(native,),
                    max_tasks_per_drone=3)
    options=ins(problem,native=(7,-7))
    assert len(options)==2
    best,early=options
    assert best["score"].late_count==0 and early["score"].late_count==1
    assert max(early["availability"].values())<max(best["availability"].values())
    assert set(best["delivery_times"])=={7}
    assert set(best["availability"])=={1,2}
    assert best["event_times"][best["in_end_position"]]==max(best["availability"].values())


def test_out_wait_and_every_native_suffix_deadline_use_actual_source_availability():
    problem=fixture()
    problem=replace(problem,tasks=problem.tasks+(Task(7,Point(0.,0.),Point(0.,0.),5.),),max_tasks_per_drone=3)
    source=ins(problem)[0]
    available={**source["availability"],3:10.,5:20.}
    route=source["route"]+(107,111,-107,-111,7,-7)
    result=costs.route_cost(problem,route,0,availability=available,selected_pair=(107,111),deadline=perf_counter()+1.)
    assert result["event_times"][4:6]==(10.,20.)
    assert result["delivery_times"][7]==pytest.approx(20.+2./sqrt(3.))
    assert result["score"].late_count==3
    assert result["total_waiting_min"]==pytest.approx(20.-2./sqrt(3.))
    assert result["load_before"][4:6]==(0,1)
    assert result["availability"]==source["availability"]


def test_out_options_only_use_empty_gaps_after_all_own_in_drops_and_keep_real_prefix():
    problem=fixture()
    source=ins(problem)[0]
    available={**source["availability"],3:10.,5:20.}
    stats={}
    result=costs.out_option(problem,source,0,(3,5),9,available,statistics=stats,**args())
    assert result["pickup_position"]>source["in_end_position"]
    assert result["route"][:source["in_end_position"]+1]==source["route"][:source["in_end_position"]+1]
    assert result["availability"]==source["availability"]
    assert result["load_before"][result["pickup_position"]]==0
    assert stats["out_candidates"]<=8


def test_direct_has_six_precedence_orders_and_true_home_distance():
    problem=fixture()
    stats={}
    record=costs.direct_option(problem,(),0,(1,2),statistics=stats,**args())
    assert stats["direct_candidates"]==6
    assert record["score"].distance_km==pytest.approx(4.)
    assert len(record["delivery_times"])==2 and not record["availability"]
    # Source B's home matters: serving A's two requests first flies B->A.
    other=costs.direct_option(problem,(),1,(1,2),**args())
    assert other["score"].distance_km==pytest.approx(6.)


@pytest.mark.parametrize("late,count",[(.5e-9,0),(1.e-9,0),(1.5e-9,1)])
def test_official_lateness_threshold_masks_both_count_and_total(late,count):
    problem=Problem((Task(1,Point(0.,0.),Point(0.,0.),0.),),drone_count=1,capacity=2,
                    max_tasks_per_drone=1,speed_km_per_min=1.,drone_homes=(Point(late,0.),))
    record=costs.route_cost(problem,(1,-1),0,deadline=perf_counter()+1.)
    assert record["score"].late_count==count
    assert record["score"].total_lateness_min==(late if count else 0.)


@pytest.mark.parametrize("route,error",[((1,1,-1,-1),"duplicate"),((-1,1),"precedes"),
    ((1,),"undelivered"),((1,2,3,-1,-2,-3),"capacity"),((999,-999),"unknown"),
    ((True,-1),"integer"),((1,-1,102,-102),"mixes")])
def test_local_transport_structure_rejects_invalid_routes(route,error):
    with pytest.raises(ValueError,match=error):
        costs.route_cost(fixture(),route,0,deadline=perf_counter()+1.)


def test_zero_load_gap_frontier_is_validated_and_bounded_by_actual_cargo():
    problem=fixture()
    assert costs.zero_load_gaps(problem,(1,2,-1,-2,3,-3))==(0,4,6)
    assert costs.zero_load_gaps(problem,(1,2,-1,-2,3,-3),start=1)==(4,6)
    with pytest.raises(ValueError):costs.zero_load_gaps(problem,(1,-2))


def test_unknown_availability_wrong_own_drop_and_missing_out_times_are_rejected():
    problem=fixture();source=ins(problem)[0]
    with pytest.raises(ValueError,match="lacks source"):
        costs.route_cost(problem,(107,-107),0,deadline=perf_counter()+1.)
    with pytest.raises(ValueError,match="nonnegative"):
        costs.route_cost(problem,(),0,availability={3:-1.},deadline=perf_counter()+1.)
    with pytest.raises(ValueError,match="own IN drop"):
        costs.route_cost(problem,source["route"],0,availability={1:99.},deadline=perf_counter()+1.)


def test_out_cannot_consume_own_pair_or_use_forged_frontier_or_station():
    problem=fixture();source=ins(problem)[0];available={**source["availability"],3:10.,5:10.}
    with pytest.raises(ValueError):costs.out_option(problem,source,0,(1,3),9,available,**args())
    with pytest.raises(ValueError,match="frontier"):
        costs.out_option(problem,{**source,"in_end_position":0},0,(3,5),9,available,**args())
    with pytest.raises(ValueError):
        costs.out_option(problem,{**source,"source_task_pair":(3,4)},0,(3,5),9,available,**args())
    with pytest.raises(ValueError):costs.in_options(problem,(),0,(1,2),99,**args())
    with pytest.raises(ValueError,match="remains"):
        costs.direct_option(problem,(1,-1),0,(1,2),**args())


def test_custom_distance_matrix_and_route_final_K_overflow_are_not_silently_scored():
    problem=fixture()
    matrix=tuple(tuple(row) for row in problem._distances)
    with pytest.raises(ValueError,match="Euclidean"):
        costs.route_cost(replace(problem,distance_matrix_km=matrix),(),0,deadline=perf_counter()+1.)
    with pytest.raises(ValueError,match="exceeds K"):
        costs.route_cost(problem,(1,-1,2,-2,3,-3),0,deadline=perf_counter()+1.)


def test_absolute_deadline_applies_to_candidates_and_local_prefix_walk(monkeypatch):
    problem=fixture()
    with pytest.raises(costs.Deadline):
        costs.in_options(problem,(),0,(1,2),9,deadline=perf_counter()-1.,weights=W,scales=S)
    calls=[0]
    def clock():
        calls[0]+=1
        return 0. if calls[0]<4 else 2.
    monkeypatch.setattr(costs,"perf_counter",clock)
    with pytest.raises(costs.Deadline):
        costs.route_cost(problem,(1,-1,2,-2),0,deadline=1.)


def test_zero_joint_flight_is_rejected_without_faking_a_delivery_or_infeasibility_claim():
    problem=fixture()
    h=Point(1.,sqrt(3.)/3.)
    problem=replace(problem,tasks=tuple(replace(t,delivery=h) if t.id in (3,5) else t for t in problem.tasks))
    source=ins(problem)[0]
    available={**source["availability"],3:1.,5:1.}
    stats={}
    result=costs.out_option(problem,source,0,(3,5),9,available,statistics=stats,**args())
    assert result is None and stats["out_zero_shared_rejections"]==2
    near=Point(h.x+1e-11,h.y)
    problem=replace(problem,tasks=tuple(replace(t,delivery=near) if t.id in (3,5) else t for t in problem.tasks))
    assert costs.out_option(problem,source,0,(3,5),9,available,**args()) is None


def test_missing_native_gap_or_incorrect_station_eligibility_is_not_silently_relaxed():
    problem=fixture()
    blocked=replace(problem,task_relay_candidates={t:((9,) if t!=1 else ()) for t in range(1,7)})
    with pytest.raises(ValueError,match="eligible"):
        ins(blocked)
    with pytest.raises(ValueError,match="gap limit"):
        costs.direct_option(problem,(),0,(1,2),gap_limit=5,**args())
