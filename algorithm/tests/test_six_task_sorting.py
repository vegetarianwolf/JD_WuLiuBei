"""Six-task mechanism, independent ninety-map control and full clock audit."""
from dataclasses import replace
from itertools import combinations
from math import sqrt
from time import perf_counter
import json

import pytest

from uav_dispatch import Point,Task
from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import six_task_sorting as method
from algorithm.experiments.audit_six_task_sorting import audit_six_task_sorting_proposal
from algorithm.tests.test_six_task_route_costs import fixture,W,S
from algorithm.tests.test_relay_repooling_exchange import topology


def original(problem):
    return ((1,2,-1,-2),(3,4,-3,-4),(5,6,-5,-6))


def run(problem,base,**kwargs):
    return method.refine(problem,base,deadline=perf_counter()+3.,weights=W,scales=S,**kwargs)


def audit_snapshots(problem,base,result,arm):
    from algorithm.experiments.audit_weighted_relay_ablation import audit_payload
    from algorithm.tests.test_audit_mobile_relay import canonical_payload
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    graph=topology(problem);graph["task_relay_candidates"]={str(t):list(problem.task_relay_candidates.get(t,())) for t in problem.task_ids}
    for snapshot in result["metadata"]["full_candidate_snapshots"]:
        proof=audit_six_task_sorting_proposal(tasks,graph,base,snapshot["routes"],snapshot["move"],
            relay_enabled=arm,telemetry=snapshot,weights=W,scales=S)
        assert proof["passed"]
        audit_payload(tasks,graph,canonical_payload(tasks,graph,snapshot["routes"],{}))


def test_triangle_genuine_six_cross_sorting_beats_input_and_independently_free_direct_control():
    problem=fixture();base=original(problem)
    old=topology(problem)
    direct=run(problem,base,relay_enabled=False)
    relay=run(problem,base)
    assert direct["evaluation"].valid and relay["evaluation"].valid
    assert direct["evaluation"].score.late_count==3 and direct["evaluation"].score.distance_km==pytest.approx(12.)
    assert relay["evaluation"].score.late_count==0 and relay["evaluation"].score.distance_km==pytest.approx(12./sqrt(3.))
    assert relay["metadata"]["final_cross_uav_count"]==6
    assert relay["metadata"]["final_key"]<relay["metadata"]["initial_key"]
    assert direct["metadata"]["selected_families"]==relay["metadata"]["selected_families"]
    assert direct["metadata"]["selector_sha256"]==relay["metadata"]["selector_sha256"]
    assert direct["metadata"]["selector_completed"] and relay["metadata"]["selector_completed"]
    assert relay["metadata"]["relay_final_maps_examined"]==8*relay["metadata"]["in_combinations_examined"]
    assert direct["metadata"]["direct_pair_table_calls"]==45
    assert topology(problem)==old and "problem" not in relay and "problem" not in direct
    for result,arm in ((direct,False),(relay,True)):
        assert len(result["metadata"]["full_candidate_snapshots"])==2
        audit_snapshots(problem,base,result,arm)
        for accepted in result["metadata"]["accepted_moves"]:
            assert accepted["full_verified_at_perf_counter"]<result["metadata"]["deadline_perf_counter"]
            assert accepted["accepted_at_perf_counter"]<result["metadata"]["deadline_perf_counter"]
            assert accepted["move"]["family"] in result["metadata"]["selected_families"]
        json.dumps(result["metadata"],allow_nan=False)


def test_bitmask_top_two_equals_exhaustive_ninety_full_responsibility_partitions():
    problem=fixture();base=original(problem)
    result=run(problem,base,relay_enabled=False)
    family=result["metadata"]["selected_families"][0];tasks=set(family["task_ids"])
    table={(a,pair):method.costs.direct_option(problem,(),a,pair,deadline=perf_counter()+1.,
        gap_limit=4,weights=W,scales=S) for a in range(3) for pair in combinations(sorted(tasks),2)}
    leaves=[]
    for first in combinations(sorted(tasks),2):
        for second in combinations(sorted(tasks-set(first)),2):
            third=tuple(sorted(tasks-set(first)-set(second)))
            records=[table[a,pair] for a,pair in enumerate((first,second,third))]
            routes=tuple(r["route"] for r in records)
            score=sum((r["score"] for r in records),Score(0,0.,0.))
            leaves.append((method.weighted_key(score,W,S),routes))
    assert len(leaves)==90
    saved=[(tuple(s["key"]),tuple(tuple(r) for r in s["routes"])) for s in result["metadata"]["full_candidate_snapshots"]]
    assert saved==sorted(leaves)[:2]
    assert len(list(method._cross_maps(family["aircraft"],family["source_task_groups"])))==8


def test_native_suffix_waits_and_complete_table_clocks_match_full_dag():
    problem=fixture()
    problem=replace(problem,tasks=problem.tasks+(Task(7,Point(0.,0.),Point(-1.,0.),1.1),),max_tasks_per_drone=3)
    base=((7,-7,1,2,-1,-2),(3,4,-3,-4),(5,6,-5,-6))
    family=dict(station_id=9,aircraft=[0,1,2],source_task_groups=[[1,2],[3,4],[5,6]],task_ids=list(range(1,7)),priority=[0.])
    native=method._native(problem,base,family)
    incoming=[method.costs.in_options(problem,native[a],a,tuple(pair),9,deadline=perf_counter()+1.,
        option_limit=2,gap_limit=4,weights=W,scales=S)[0] for a,pair in enumerate(family["source_task_groups"])]
    available={t:clock for record in incoming for t,clock in record["availability"].items()}
    final=((3,5),(1,6),(2,4))
    outputs=[method.costs.out_option(problem,record,a,final[a],9,available,deadline=perf_counter()+1.,
        gap_limit=4,weights=W,scales=S) for a,record in enumerate(incoming)]
    assert outputs[1]["total_waiting_min"]>1.9 and outputs[2]["total_waiting_min"]>1.9
    base_records=[method.costs.route_cost(problem,r,a,deadline=perf_counter()+1.) for a,r in enumerate(base)]
    candidate=method._candidate(base_records,family,outputs,final,True,available,problem)
    full=evaluate_solution(problem,candidate["routes"],_force_full=True)
    schedule=GlobalRelayEvaluator(problem).evaluate(candidate["routes"])
    assert full.valid and full.score.late_count==candidate["score"].late_count
    assert full.score.total_lateness_min==pytest.approx(candidate["score"].total_lateness_min)
    assert full.score.distance_km==pytest.approx(candidate["score"].distance_km)
    for a,record in enumerate(candidate["records"]):
        assert record["event_times"]==pytest.approx(tuple(schedule.event_times_min[a,p] for p in range(len(record["route"]))))
    snapshot=method._snapshot(problem,candidate["routes"],full,schedule,W,S,phase="full_proposal",move=candidate["move"],table_score=candidate["score"])
    audit_snapshots(problem,base,dict(metadata=dict(full_candidate_snapshots=[snapshot])),True)


def test_unchanged_fourth_aircraft_score_is_in_full_table_and_native_events_stay_put():
    problem=fixture()
    problem=replace(problem,tasks=problem.tasks+(Task(7,Point(10.,10.),Point(12.,10.),1.),),
        drone_count=4,drone_homes=(*problem.drone_homes,Point(10.,10.)))
    base=(*original(problem),(7,-7))
    result=run(problem,base)
    assert result["routes"][3]==(7,-7) and result["evaluation"].score.late_count==1
    for snapshot in result["metadata"]["full_candidate_snapshots"]:
        assert snapshot["table_score"]["late_count"]==1 and snapshot["delivery_times_min"]["7"]==2.
        assert snapshot["routes"][3]==[7,-7] and snapshot["table_score"]["distance_km"]>=2.
    audit_snapshots(problem,base,result,True)


def test_expired_and_full_check_overrun_never_adopt_unchecked_plan(monkeypatch):
    problem=fixture();base=original(problem)
    expired=method.refine(problem,base,deadline=perf_counter()-1.,weights=W,scales=S)
    assert expired["routes"]==base and not expired["metadata"]["selector_completed"]
    assert expired["metadata"]["selected_families"]==[]
    clock=[0.];calls=[0];original_check=method.evaluate_solution
    monkeypatch.setattr(method,"perf_counter",lambda:clock[0])
    monkeypatch.setattr(method.costs,"perf_counter",lambda:clock[0])
    def late_check(*args,**kwargs):
        checked=original_check(*args,**kwargs);calls[0]+=1
        if calls[0]>1:clock[0]=2.
        return checked
    monkeypatch.setattr(method,"evaluate_solution",late_check)
    result=method.refine(problem,base,deadline=1.,weights=W,scales=S)
    assert result["routes"]==base and result["metadata"]["accepted_improvements"]==0
    assert result["metadata"]["deadline_reached"] and result["metadata"]["timeout_stage"]=="canonical_full_guard"
    assert not result["metadata"]["full_candidate_snapshots"]


def test_explicit_empty_station_eligibility_never_enters_in_candidate_constructor():
    problem=fixture()
    problem=replace(problem,task_relay_candidates={**problem.task_relay_candidates,2:()})
    for arm in (False,True):
        result=run(problem,original(problem),relay_enabled=arm)
        assert result["metadata"]["selector_completed"] and result["metadata"]["selected_families"]==[]
        assert result["routes"]==original(problem) and not result["metadata"]["full_candidate_snapshots"]


@pytest.mark.parametrize("change",[dict(in_candidates=3),dict(in_gap_limit=0),dict(source_pair_pool_limit=True),
    dict(full_candidates_per_family=3),dict(direct_gap_limit=5)])
def test_unbounded_or_invalid_search_limits_rejected(change):
    problem=fixture()
    with pytest.raises(ValueError):run(problem,original(problem),**change)
