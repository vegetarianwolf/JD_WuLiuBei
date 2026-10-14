"""Tiny packing/capacity-window and matched DIRECT-control reconstruction tests."""
from time import perf_counter
import json

import pytest

from uav_dispatch import Point
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import mobile_delivery_reconstruction as reconstruction
from algorithm.experiments import mobile_relay_refinement as v1
from algorithm.tests.test_mobile_relay_refinement import fixture


def run(problem,routes,**kwargs):
    return reconstruction.refine(problem,routes,deadline=perf_counter()+3.,weights=(.5,.25,.25),
        scales=(len(problem.tasks),100.,100.),max_specs=128,max_groups=24,**kwargs)


def spec_fixture(problem,routes,*,paired=False):
    scheduled=GlobalRelayEvaluator(problem).evaluate(routes)
    position=2 if paired else 1
    left=next(c for c in v1._cuts(problem,routes[0],0,scheduled) if c.position==position)
    right=next(c for c in v1._cuts(problem,routes[1],1,scheduled) if c.position==position)
    a,b=(1,2) if paired else (1,),(3,4) if paired else (2,)
    return v1._Spec(0,1,left,right,a,b,b,a,(0.,))


def stats():
    return dict(deadline_reached=False,rejected_capacity_layouts=0,rejected_capacity_windows=0,
        complete_route_layouts=0,delivery_position_probes=0,complete_joint_candidates=0)


def test_two_piece_capacity_windows_rebuild_full_load_without_validating_missing_d():
    problem,routes=fixture(paired=True)
    unfinished,anchors=reconstruction._remove_deliveries(routes[0],(-1,-2))
    assert not reconstruction._capacity(unfinished,2)
    assert reconstruction._next_overflow(unfinished,2,2)==2
    layouts=reconstruction._layouts(problem,unfinished,0,(-1,-2),2,anchors,{},None,None,
        (.5,.25,.25),(8.,100.,100.),10,12,perf_counter()+1.,stats())
    assert layouts
    for route in layouts:
        assert reconstruction._capacity(route,2)
        assert set(route)==set(routes[0])
        # The first delivery precedes the next untouched pickup, and the
        # second delivery restores space before the following pickup.
        assert min(route.index(-1),route.index(-2))<route.index(5)
        assert max(route.index(-1),route.index(-2))<route.index(6)
        assert tuple(v for v in route if abs(v) not in (1,2))==(5,6,-5,-6)


def test_true_two_by_two_reconstruction_retains_interleaved_cargo_packing():
    problem,routes=fixture(paired=True)
    spec=spec_fixture(problem,routes,paired=True)
    records=reconstruction._rebuild(problem,routes,spec,Point(5.,0.),True,(.5,.25,.25),(8.,100.,100.),
        dict(position_limit=12,layout_limit=6,joint_limit=12),perf_counter()+1.,stats())
    assert records
    key,candidate,legs,descriptor=records[0]
    changed=v1._problem(problem,legs,descriptor)
    checked=evaluate_solution(changed,candidate,_force_full=True)
    assert checked.valid and checked.score.distance_km==28.
    assert key==v1.weighted_key(checked.score,(.5,.25,.25),(8.,100.,100.))
    # Newly received cargo interleaves with the untouched local pickups rather
    # than forcing all received deliveries to finish as an isolated block.
    out_for_task4=next(l.id for l in legs.values() if l.kind=="RELAY_OUT" and l.task_id==4)
    assert any(row[1][0].index(5)<row[1][0].index(-out_for_task4)<row[1][0].index(6) for row in records)
    physical=v1._physical_meetings(changed,candidate,[descriptor],GlobalRelayEvaluator(changed).evaluate(candidate))
    assert physical[0]["group_size"]==2
    assert reconstruction._without_selected(problem,routes,{1,2,3,4},{})==reconstruction._without_selected(
        problem,candidate,{1,2,3,4},legs)


def test_direct_control_really_reorders_own_delivery_and_returns_no_problem_key():
    problem,routes=fixture()
    result=run(problem,routes,relay_enabled=False)
    assert "problem" not in result
    assert result["evaluation"].valid and result["evaluation"].score.distance_km==20.
    assert result["metadata"]["final_cross_uav_count"]==0
    before=build_plan_index(problem,routes)
    after=build_plan_index(problem,result["routes"])
    assert dict(after.delivery_route_by_task)==dict(before.delivery_route_by_task)
    assert result["routes"][0].index(-1)>result["routes"][0].index(-3)
    assert result["routes"][1].index(-2)>result["routes"][1].index(-4)


def test_full_refine_preserves_final_k_resources_and_independent_mobile_physics():
    from algorithm.experiments.audit_mobile_relay import audit_mobile_routes,_bind_meetings
    problem,routes=fixture(paired=True)
    result=run(problem,routes)
    changed=result["problem"]
    assert result["evaluation"].valid and result["evaluation"].score.distance_km<40.
    assert result["metadata"]["final_cross_uav_count"]>=2
    assert changed.home_points()==problem.home_points() and changed.tasks==problem.tasks
    index=build_plan_index(changed,result["routes"])
    assert [sum(owner==i for owner in index.delivery_route_by_task.values()) for i in range(2)]==[4,4]
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    reference=audit_mobile_routes(tasks,v1.topology(problem),v1.topology(changed),result["routes"])
    assert reference["score"]["distance_km"]==result["evaluation"].score.distance_km
    _bind_meetings(result["metadata"]["mobile_meetings"],reference["meetings"])


def test_geometry_update_uses_actual_completed_successor_not_meeting_placeholder():
    problem,routes=fixture(paired=True)
    scheduled=GlobalRelayEvaluator(problem).evaluate(routes)
    left=next(c for c in v1._cuts(problem,routes[0],0,scheduled) if c.position==2)
    right=next(c for c in v1._cuts(problem,routes[1],1,scheduled) if c.position==2)
    spec=v1._Spec(0,1,left,right,(1,),(3,),(3,),(1,),(0.,))
    records=reconstruction._rebuild(problem,routes,spec,Point(5.,0.),True,(.5,.25,.25),(8.,100.,100.),
        dict(position_limit=12,layout_limit=4,joint_limit=4),perf_counter()+1.,stats())
    _,candidate,legs,descriptor=records[0]
    endpoints=reconstruction._new_terminal_points(problem,candidate,legs,Point(5.,0.),descriptor)
    for (aircraft,end) in zip(descriptor["aircraft"],endpoints):
        takes=[p for p,v in enumerate(candidate[aircraft]) if v>0 and v in legs and legs[v].kind=="RELAY_OUT"]
        actual_visit=candidate[aircraft][max(takes)+1]
        assert end[1]==v1._event(problem,actual_visit,legs,Point(5.,0.))[0]
        assert actual_visit not in {l.id for l in legs.values() if l.kind=="RELAY_OUT"}


def test_all_scheduler_and_full_checker_inputs_are_complete_capacity_valid(monkeypatch):
    problem,routes=fixture(paired=True)
    original=reconstruction.evaluate_solution
    inspected=[]
    def checker(p,candidate,**kwargs):
        assert all(reconstruction._capacity(r,p.capacity) for r in candidate)
        result=original(p,candidate,**kwargs)
        assert result.valid and set(result.delivery_times_min)==set(p.task_ids)
        inspected.append(candidate)
        return result
    monkeypatch.setattr(reconstruction,"evaluate_solution",checker)
    result=run(problem,routes)
    assert len(inspected)>1 and result["evaluation"].valid
    assert result["metadata"]["global_evaluations"]<=result["metadata"]["configuration"]["full_candidate_limit"]


@pytest.mark.parametrize("phase",["layout","full"])
def test_over_deadline_layout_or_full_candidate_is_never_adopted(monkeypatch,phase):
    problem,routes=fixture()
    clock=[0.]
    monkeypatch.setattr(reconstruction,"perf_counter",lambda:clock[0])
    monkeypatch.setattr(v1,"perf_counter",lambda:clock[0])
    original_layout,original_full=reconstruction._layouts,reconstruction.evaluate_solution
    calls=[0]
    def layouts(*args,**kwargs):
        result=original_layout(*args,**kwargs)
        if phase=="layout":clock[0]=2.
        return result
    def full(*args,**kwargs):
        result=original_full(*args,**kwargs)
        calls[0]+=1
        if phase=="full" and calls[0]>1:clock[0]=2.
        return result
    monkeypatch.setattr(reconstruction,"_layouts",layouts)
    monkeypatch.setattr(reconstruction,"evaluate_solution",full)
    result=reconstruction.refine(problem,routes,deadline=1.,weights=(.5,.25,.25),scales=(4.,100.,100.),max_specs=16)
    assert result["problem"] is problem and result["routes"]==routes
    assert result["metadata"]["deadline_reached"] and result["metadata"]["accepted_improvements"]==0
    if phase=="full":assert calls[0]>1


def test_snapshots_have_complete_models_and_post_polish_never_worsens_proposal():
    problem,routes=fixture(paired=True)
    result=run(problem,routes)
    stats=result["metadata"]
    assert stats["first_complete_candidate"] is not None and stats["best_constructed_candidate"] is not None
    for group in stats["group_records"]:
        for record in group["proposals"]:
            first,last=record["full_proposal"],record["post_polish"]
            assert first["valid"] and first["complete"] and "topology" in first
            if last is not None:
                assert last["valid"] and last["complete"] and last["key"]<=first["key"]
    assert stats["final_key"]<=stats["initial_key"]
    json.dumps(stats,allow_nan=False)


def test_seed_reproducibility_and_invalid_config_are_rejected():
    problem,routes=fixture()
    first,second=run(problem,routes,polish=False),run(problem,routes,polish=False)
    assert first["routes"]==second["routes"]
    assert first["metadata"]["mobile_meetings"]==second["metadata"]["mobile_meetings"]
    for options in (dict(relay_enabled=1),dict(position_limit=0),dict(layout_limit=False),
                    dict(joint_limit=0),dict(proposal_margin=-1.),dict(polish=1)):
        with pytest.raises(ValueError):run(problem,routes,**options)
    with pytest.raises(ValueError,match="invalid initial"):
        run(problem,((1,-1),()))
