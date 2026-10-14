"""Tiny deadline-position and physical tests for bounded mobile coordinate search."""
from time import perf_counter
import json

import pytest

from uav_dispatch import Point, Problem, Task
from uav_dispatch.relay import GlobalRelayEvaluator
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import mobile_relay_refinement as v1
from algorithm.experiments import mobile_relay_refinement_v2 as v2
from algorithm.tests.test_mobile_relay_refinement import fixture


def run(problem,routes,**kwargs):
    return v2.refine(problem,routes,deadline=perf_counter()+3.,weights=(.5,.25,.25),
                     scales=(len(problem.tasks),100.,100.),max_pairs=16,**kwargs)


def position_fixture():
    tasks=(Task(1,Point(0.,0.),Point(10.,10.),100.),
           Task(2,Point(10.,0.),Point(0.,10.),14.8),
           Task(3,Point(0.,10.),Point(0.,10.),100.),
           Task(4,Point(10.,10.),Point(10.,10.),100.))
    problem=Problem(tasks,drone_count=2,max_tasks_per_drone=2,capacity=2,speed_km_per_min=1.,
                    drone_homes=(Point(-1.,0.),Point(10.,0.)))
    routes=((1,-1,3,-3),(2,-2,4,-4))
    schedule=GlobalRelayEvaluator(problem).evaluate(routes)
    left=next(c for c in v1._cuts(problem,routes[0],0,schedule) if c.position==1)
    right=next(c for c in v1._cuts(problem,routes[1],1,schedule) if c.position==1)
    spec=v1._Spec(0,1,left,right,(1,),(2,),(2,),(1,),(0.,))
    points=v1._points(left.point,Point(0.,10.),right.point,Point(10.,10.),1.,0.,1.,6)
    return problem,routes,spec,points


def search_stats():
    return dict(deadline_reached=False,coordinate_probes=0,deadline_boundary_probes=0,
                coordinate_probe_cutoffs=0)


def test_deadline_boundary_search_beats_best_finite_geometry_point():
    problem,routes,spec,points=position_fixture()
    old_scores=[]
    for point in points:
        candidate,legs,descriptor=v1._prototype(problem,routes,spec,point)
        old_scores.append(v1._fast_lower_score(problem,candidate,legs,point,descriptor))
    old_best=min(old_scores,key=lambda s:v1.weighted_key(s,(.5,.25,.25),(4.,100.,100.)))
    assert old_best.late_count==1
    stats=search_stats()
    records,trace=v2._optimize_points(problem,routes,spec,points,(.5,.25,.25),(4.,100.,100.),
        deadline=perf_counter()+3.,stats=stats,seeds=2,rounds=12,probe_limit=240,
        boundary_steps=16,min_step=.001,box=(0.,10.,0.,10.))
    best=records[0]
    assert best[2].late_count==0
    assert best[0]<v1.weighted_key(old_best,(.5,.25,.25),(4.,100.,100.))
    assert stats["deadline_boundary_probes"]>0 and trace["probes"]<=240
    candidate,legs,descriptor=v1._prototype(problem,routes,spec,best[1])
    changed=v1._problem(problem,legs,descriptor)
    checked=evaluate_solution(changed,candidate,_force_full=True)
    assert checked.valid and checked.score==best[2]


def test_complete_mobile_result_preserves_resources_and_passes_independent_audit():
    from algorithm.experiments.audit_mobile_relay import audit_mobile_routes,_bind_meetings
    problem,routes=fixture()
    result=run(problem,routes)
    assert result["evaluation"].valid and result["evaluation"].score.distance_km==20.
    assert result["metadata"]["final_cross_uav_count"]==2
    assert result["problem"].home_points()==problem.home_points()
    assert result["problem"].tasks==problem.tasks
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    reference=audit_mobile_routes(tasks,v1.topology(problem),v1.topology(result["problem"]),result["routes"])
    assert reference["score"]["distance_km"]==20.
    _bind_meetings(result["metadata"]["mobile_meetings"],reference["meetings"])
    json.dumps(result["metadata"],allow_nan=False)


def test_per_group_probe_bound_and_box_are_real_enforced_limits():
    problem,routes,spec,points=position_fixture()
    stats=search_stats()
    records,trace=v2._optimize_points(problem,routes,spec,points,(.5,.25,.25),(4.,100.,100.),
        deadline=perf_counter()+3.,stats=stats,seeds=2,rounds=100,probe_limit=9,
        boundary_steps=100,min_step=.000001,box=(0.,10.,0.,10.))
    assert trace["probes"]==9 and stats["coordinate_probes"]==9
    assert stats["coordinate_probe_cutoffs"]==1
    assert all(0.<=r[1].x<=10. and 0.<=r[1].y<=10. for r in records)


def test_expired_and_mid_coordinate_budget_do_not_adopt_unchecked_points(monkeypatch):
    problem,routes=fixture()
    result=v2.refine(problem,routes,deadline=perf_counter()-1.,weights=(.5,.25,.25),scales=(4.,100.,100.))
    assert result["problem"] is problem and result["routes"]==routes
    clock=[0.]
    monkeypatch.setattr(v2,"perf_counter",lambda:clock[0])
    monkeypatch.setattr(v1,"perf_counter",lambda:clock[0])
    original,fast_calls=v1._fast_lower_score,[0]
    def fast(*args,**kwargs):
        result=original(*args,**kwargs)
        fast_calls[0]+=1
        clock[0]=2.
        return result
    monkeypatch.setattr(v1,"_fast_lower_score",fast)
    result=v2.refine(problem,routes,deadline=1.,weights=(.5,.25,.25),scales=(4.,100.,100.),max_pairs=16)
    assert result["problem"] is problem and result["routes"]==routes
    assert result["metadata"]["deadline_reached"] and result["metadata"]["accepted_improvements"]==0
    assert fast_calls[0]==1


def test_full_confirmation_completed_after_deadline_is_not_adopted(monkeypatch):
    problem,routes=fixture()
    clock=[0.]
    monkeypatch.setattr(v2,"perf_counter",lambda:clock[0])
    monkeypatch.setattr(v1,"perf_counter",lambda:clock[0])
    original,calls=v2.evaluate_solution,[0]
    def check(*args,**kwargs):
        result=original(*args,**kwargs)
        calls[0]+=1
        if calls[0]>1:clock[0]=2.
        return result
    monkeypatch.setattr(v2,"evaluate_solution",check)
    result=v2.refine(problem,routes,deadline=1.,weights=(.5,.25,.25),scales=(4.,100.,100.),max_pairs=16)
    assert result["problem"] is problem and result["routes"]==routes
    assert result["metadata"]["accepted_improvements"]==0 and result["metadata"]["deadline_reached"]
    assert calls[0]>1


def test_seed_reproducibility_and_config_validation():
    problem,routes=fixture()
    first,second=run(problem,routes),run(problem,routes)
    assert first["routes"]==second["routes"]
    assert first["metadata"]["mobile_meetings"]==second["metadata"]["mobile_meetings"]
    assert first["metadata"]["coordinate_probes"]==second["metadata"]["coordinate_probes"]
    for change in (dict(pattern_seeds=0),dict(pattern_rounds=False),dict(probe_limit=0),
                   dict(boundary_steps=0),dict(min_step=0.),dict(full_points_per_group=0)):
        with pytest.raises(ValueError):run(problem,routes,**change)
    with pytest.raises(ValueError,match="invalid initial"):
        run(problem,((1,-1),()))
