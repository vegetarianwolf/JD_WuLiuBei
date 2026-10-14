"""Small zero-cache three-aircraft physical repooling checks, no official solve."""
from dataclasses import replace
import json
from time import perf_counter

import pytest

from uav_dispatch import Point,RelayStation
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import mobile_repooling_refinement as method
from algorithm.tests.test_relay_repooling_exchange import fixture

WEIGHTS,SCALES=(.5,.25,.25),(6.,100.,100.)


def mobile_fixture():
    problem,routes=fixture()
    # Existing H detours below the corridor; tasks/homes/resources stay fixed.
    return replace(problem,relay_stations=(RelayStation(1,0.,-2.),)),routes


def run(problem,routes,**kwargs):
    return method.refine(problem,routes,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,
                         **dict(dict(max_groups=2,raw_spec_limit=8,point_limit=12,point_eval_limit=40,pattern_rounds=2),**kwargs))


def spec_for(problem,routes):
    raw=method.repool.refine(problem,routes,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES)
    snapshot=next(s for s in raw["metadata"]["full_candidate_snapshots"]
        if [s["move"][k] for k in ("i","j","k1","k2")]==[1,2,3,4] and s["score"]["distance_km"]<=44.)
    return method._spec(problem,snapshot)


def test_genuine_zero_cache_mobile_pair_beats_reasonable_input_and_matching_direct():
    problem,routes=mobile_fixture()
    result=run(problem,routes)
    direct=run(problem,routes,relay_enabled=False)
    assert result["evaluation"].valid and result["evaluation"].score.late_count==0
    assert result["evaluation"].score.distance_km<=42.
    assert result["evaluation"].score.distance_km<evaluate_solution(problem,routes).score.distance_km
    assert result["evaluation"].score.distance_km<direct["evaluation"].score.distance_km
    assert result["metadata"]["final_cross_uav_count"]==2
    assert result["metadata"]["selected_groups"]==direct["metadata"]["selected_groups"]
    assert result["metadata"]["selector_sha256"]==direct["metadata"]["selector_sha256"]
    assert "problem" in result and "problem" not in direct
    meeting=result["metadata"]["mobile_meetings"][0]
    assert meeting["ground_cache_min"]==0 and meeting["shared_double_loaded_km"]>0.
    assert meeting["receiver_load_before_takes"]==[0,1]
    assert meeting["receiver_load_after_takes"]==[1,2]
    assert meeting["arrival_times_min"][2]<=min(meeting["arrival_times_min"][:2])


def test_source_arrival_reversal_changes_actual_out_sequence_not_identity_sort():
    problem,routes=fixture(asymmetric=True,deadline=14.)
    spec=spec_for(problem,routes)
    manual=method._manual(problem,spec,Point(0.,0.))
    assert manual["physical"]["pickup_source_order"]==[1,0]
    assert manual["routes"][2][:2]==(spec.outgoing[1],spec.outgoing[0])
    changed,candidate,station=method._new_problem(problem,spec,Point(0.,0.),manual)
    meeting=method._meeting(changed,candidate,spec,station,manual)
    assert meeting["source_arrival_order"]==[2,1]
    assert meeting["source_drop_times_min"]==[12.,10.]
    assert meeting["receiver_take_times_min"]==[10.,12.]
    assert meeting["receiver_empty_wait_min"]==10. and meeting["receiver_loaded_wait_min"]==2.
    schedule=GlobalRelayEvaluator(changed).evaluate(candidate)
    assert schedule.score==manual["score"] and schedule.total_waiting_min==12.
    assert schedule.delivery_times_min[1]==13. and schedule.delivery_times_min[2]==14.


def test_receiver_late_ground_cache_candidate_is_rejected_before_new_model():
    problem,routes=fixture()
    problem=replace(problem,drone_homes=(*problem.drone_homes[:2],Point(0.,20.)))
    raw=method.repool.refine(problem,routes,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES)
    snapshot=next(s for s in raw["metadata"]["full_candidate_snapshots"]
        if [s["move"][k] for k in ("i","j","k1","k2")]==[1,2,3,4])
    spec=method._spec(problem,snapshot)
    assert method._manual(problem,spec,Point(0.,0.)) is None
    assert method.geometry.receiver_first_feasible(spec.context,Point(0.,0.))["arrival_times_min"]==[10.,10.,20.]


def test_zero_common_flight_to_immediate_first_delivery_is_not_pooling():
    problem,routes=fixture()
    spec=spec_for(problem,routes)
    assert method.geometry.receiver_first_feasible(spec.context,Point(0.,1.))["feasible"]
    assert method._manual(problem,spec,Point(0.,1.)) is None


def test_new_topology_is_one_site_four_legs_only_and_old_resources_immutable():
    problem,routes=mobile_fixture()
    old=method.mobile.topology(problem)
    result=run(problem,routes)
    changed=result["problem"]
    assert len(changed.relay_stations)==len(problem.relay_stations)+1
    assert len(changed.leg_registry)==len(problem.leg_registry)+4
    assert changed.relay_stations[:-1]==problem.relay_stations
    assert all(changed.leg_registry[k]==v for k,v in problem.leg_registry.items())
    assert changed.tasks==problem.tasks and changed.home_points()==problem.home_points()
    assert method.mobile.topology(problem)==old
    index=build_plan_index(changed,result["routes"])
    assert [sum(a==v for v in index.delivery_route_by_task.values()) for a in range(3)]==[2,2,2]
    assert method.repool._remove(changed,result["routes"],(1,2,3,4))==method.repool._remove(problem,routes,(1,2,3,4))
    used={abs(v) for r in result["routes"] for v in r}
    assert set(changed.leg_registry)-set(problem.leg_registry)<=used


def test_manual_all_event_clocks_and_lower_bounds_match_full_euclidean_problem():
    problem,routes=fixture(asymmetric=True,deadline=14.)
    spec=spec_for(problem,routes)
    lower,_=method._lower(problem,spec)
    for point in (Point(0.,0.),Point(0.,.25),Point(1.,.5)):
        manual=method._manual(problem,spec,point)
        assert manual is not None
        changed,candidate,_=method._new_problem(problem,spec,point,manual)
        schedule=GlobalRelayEvaluator(changed).evaluate(candidate)
        assert schedule.feasible and schedule.score==manual["score"]
        assert lower.late_count<=schedule.score.late_count
        assert lower.total_lateness_min<=schedule.score.total_lateness_min+1e-8
        assert lower.distance_km<=schedule.score.distance_km+1e-10
        for a,route in enumerate(candidate):
            for p,_ in enumerate(route):
                assert schedule.event_times_min[(a,p)]==pytest.approx(manual["event_times"][a][p])


def test_expired_or_full_evaluation_over_deadline_never_adopts_new_resources(monkeypatch):
    problem,routes=mobile_fixture()
    result=method.refine(problem,routes,deadline=perf_counter()-1.,weights=WEIGHTS,scales=SCALES)
    assert result["routes"]==routes and result["problem"] is problem
    assert result["metadata"]["deadline_reached"]
    clock=[0.]
    monkeypatch.setattr(method,"perf_counter",lambda:clock[0])
    original=method.evaluate_solution
    def late_check(p,*args,**kwargs):
        value=original(p,*args,**kwargs)
        if len(p.relay_stations)>len(problem.relay_stations):clock[0]=2.
        return value
    # Constructor and geometry own real clocks; give only facade a controlled
    # absolute clock while replacing source with already checked tiny macros.
    raw=method.repool.refine(problem,routes,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES)
    monkeypatch.setattr(method.repool,"refine",lambda *a,**k:dict(raw,metadata=json.loads(json.dumps(raw["metadata"]))))
    original_points=method.geometry.points
    monkeypatch.setattr(method.geometry,"points",lambda ctx,**k:original_points(ctx,**{**k,"deadline":perf_counter()+1.}))
    monkeypatch.setattr(method,"evaluate_solution",late_check)
    result=method.refine(problem,routes,deadline=1.,weights=WEIGHTS,scales=SCALES)
    assert result["routes"]==routes and result["problem"] is problem
    assert result["metadata"]["accepted_improvements"]==0 and result["metadata"]["deadline_reached"]


def test_snapshot_plain_json_source_trace_and_invalid_resources():
    problem,routes=mobile_fixture()
    result=run(problem,routes)
    assert result["metadata"]["full_candidate_snapshots"]
    for snapshot in result["metadata"]["full_candidate_snapshots"]:
        assert snapshot["topology"] and snapshot["mobile_meetings"]
        assert snapshot["canonical_manual_agreement"]
        assert snapshot["original_move"]["station_id"]==1
    assert all(s["mobile_meetings"]==[] for s in result["metadata"]["raw_stage"]["full_candidate_snapshots"])
    json.dumps(result["metadata"],allow_nan=False)
    with pytest.raises(ValueError,match="capacity two"):
        run(replace(problem,capacity=1),(routes[0],routes[1],(3,-3,4,-4)))
    with pytest.raises(ValueError):run(problem,routes,point_eval_limit=0)


def test_better_original_fixed_macro_is_retained_when_mobile_search_yields_no_points(monkeypatch):
    problem,routes=fixture()
    monkeypatch.setattr(method,"_point_search",lambda *a,**k:())
    result=run(problem,routes)
    assert result["problem"] is problem and result["evaluation"].valid
    assert result["evaluation"].score.distance_km==42.
    assert result["metadata"]["mobile_meetings"]==[]
    assert result["metadata"]["accepted_moves"][-1]["kind"]=="raw_fixed_macro"
    assert result["metadata"]["accepted_moves"][-1]["full_snapshot"]["mobile_meetings"]==[]


def test_threshold_lateness_is_zero_inside_epsilon_and_not_hard_pruned():
    problem,routes=fixture(asymmetric=True,deadline=14.)
    problem=replace(problem,tasks=tuple(replace(t,deadline_min=(13. if t.id==1 else 14.)-.5e-9)
        if t.id in (1,2) else t for t in problem.tasks))
    spec=spec_for(problem,routes)
    manual=method._manual(problem,spec,Point(0.,0.))
    assert manual["near_deadline_boundary"]
    assert manual["score"].late_count==0 and manual["score"].total_lateness_min==0.
    changed,candidate,_=method._new_problem(problem,spec,Point(0.,0.),manual)
    assert evaluate_solution(changed,candidate,_force_full=True).score==manual["score"]


def test_single_seed_budget_preserves_known_receiver_prefix_anchor():
    problem,routes=mobile_fixture()
    result=run(problem,routes,point_limit=1,pattern_rounds=1,point_eval_limit=1)
    assert result["evaluation"].valid and result["evaluation"].score.distance_km==42.
    assert result["metadata"]["mobile_meetings"][0]["point"]==[0.,0.]


def test_independent_mobile_auditor_binds_all_new_and_raw_snapshots_and_meeting_metadata():
    from algorithm.experiments.audit_mobile_repooling import audit_mobile_repooling_routes,audit_mobile_repooling_payload
    from algorithm.tests.test_audit_mobile_relay import canonical_payload
    for problem,routes in (mobile_fixture(),fixture()):
        result=run(problem,routes)
        tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
        old=method.mobile.topology(problem)
        snapshots=result["metadata"]["full_candidate_snapshots"]+result["metadata"]["raw_stage"]["full_candidate_snapshots"]
        snapshots+=[m["full_snapshot"] for m in result["metadata"]["accepted_moves"]]
        for snapshot in snapshots:
            audit=audit_mobile_repooling_routes(tasks,old,snapshot["topology"],snapshot["routes"],
                base_routes=routes,move=snapshot["move"])
            assert audit["passed"] and audit["source_responsibility_proven"]
            assert audit["final_tasks_per_aircraft"]==[2,2,2]
            for key in snapshot["score"]:
                assert audit["score"][key]==pytest.approx(snapshot["score"][key])
            payload=canonical_payload(tasks,snapshot["topology"],snapshot["routes"],
                {"mobile_meetings":snapshot["mobile_meetings"]})
            assert audit_mobile_repooling_payload(tasks,old,snapshot["topology"],payload,
                base_routes=routes,move=snapshot["move"])["passed"]
