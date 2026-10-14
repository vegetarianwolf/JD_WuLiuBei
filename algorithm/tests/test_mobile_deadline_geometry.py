"""Verified bounded geometric seed generation on tiny fixtures only."""
from dataclasses import replace
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, Task
from algorithm.experiments import mobile_deadline_geometry as geometry


def fixture():
    p=Problem((Task(1,Point(-4.,0.),Point(4.,0.),100.),
        Task(2,Point(4.,0.),Point(-4.,0.),100.),
        Task(3,Point(-4.,0.),Point(-4.,0.),8.1),
        Task(4,Point(4.,0.),Point(4.,0.),8.1)),drone_count=2,
        drone_homes=(Point(-4.,0.),Point(4.,0.)),max_tasks_per_drone=2,speed_km_per_min=1.)
    routes=((1,3,-1,-3),(2,4,-2,-4))
    a=geometry.v1._Cut(2,(1,3),Point(-4.,0.),0.)
    b=geometry.v1._Cut(2,(2,4),Point(4.,0.),0.)
    spec=geometry.v1._Spec(0,1,a,b,(1,),(2,),(2,),(1,),())
    return p,routes,spec


def stats():
    return dict(deadline_reached=False,projection_calls=0)


def test_returned_points_satisfy_every_real_ellipse_and_exact_direct_clock():
    p,routes,spec=fixture()
    result=geometry.deadline_points(p,routes,spec,deadline=perf_counter()+2.)
    assert result["points"] and result["metadata"]["formula_exact_for_direct_input"]
    assert set(result["metadata"]["eligible_deadline_rescues"])=={3,4}
    assert result["metadata"]["optimistic_future_late_count_individual_minima"]==0
    joint_records=[r for r in result["metadata"]["point_records"] if set(r["target_task_ids"])=={3,4}]
    # Some joint points may deduplicate earlier single-task seeds; target
    # sets still show that the four-ellipse intersection was processed.
    assert any(set(ids)=={3,4} for ids in result["metadata"]["target_sets"])
    for point,record in zip(result["points"],result["metadata"]["point_records"],strict=True):
        assert record["bounds_verified"]
        for c,residual in zip(record["constraints"],record["residuals_km"],strict=True):
            a,b=Point(*c["focus_a"]),Point(*c["focus_b"])
            actual=point.distance_to(a)+point.distance_to(b)-c["limit_km"]
            assert actual==pytest.approx(residual,abs=1e-12)
            assert actual<=result["metadata"]["feasibility_tolerance_km"]
        candidate,legs,descriptor=geometry.v1._prototype(p,routes,spec,point)
        changed=geometry.v1._problem(p,legs,descriptor)
        from uav_dispatch.validation import evaluate_solution
        full=evaluate_solution(changed,candidate,_force_full=True)
        assert full.valid
        for task,clock in record["optimistic_delivery_times_min"].items():
            assert full.delivery_times_min[int(task)]==pytest.approx(clock,abs=1e-10)


def test_shared_focus_is_a_real_feasible_seed_even_for_degenerate_ellipse():
    a,b=Point(-4.,0.),Point(4.,0.)
    constraint=dict(focus_a=a,focus_b=b,limit_km=8.)
    projected=geometry._project_ellipse(Point(0.,4.),constraint,deadline=perf_counter()+1.,
        stats=stats(),iterations=36,tolerance=1e-9)
    assert projected==Point(0.,0.)
    assert geometry._residual(projected,constraint)==0.
    shared=dict(focus_a=b,focus_b=b,limit_km=0.)
    assert geometry._project_ellipse(Point(2.,3.),shared,deadline=perf_counter()+1.,
        stats=stats(),iterations=36,tolerance=1e-9)==b


def test_projection_onto_non_degenerate_ellipse_uses_true_norm_check():
    constraint=dict(focus_a=Point(-1.,0.),focus_b=Point(1.,0.),limit_km=4.)
    point=geometry._project_ellipse(Point(3.,3.),constraint,deadline=perf_counter()+1.,
        stats=stats(),iterations=40,tolerance=1e-9)
    assert geometry._residual(point,constraint)<=1e-9
    assert abs(geometry._residual(point,constraint))<1e-7


def test_empty_individual_ellipse_returns_no_claim_of_global_infeasibility():
    c=dict(focus_a=Point(-4.,0.),focus_b=Point(4.,0.),limit_km=7.)
    assert geometry._project_ellipse(Point(0.,0.),c,deadline=perf_counter()+1.,
        stats=stats(),iterations=36,tolerance=1e-9) is None
    p,routes,spec=fixture()
    p=replace(p,tasks=tuple(replace(t,deadline_min=1.) for t in p.tasks))
    result=geometry.deadline_points(p,routes,spec,deadline=perf_counter()+1.)
    assert result["points"]==()
    assert result["metadata"]["individual_ellipse_empty"]==4
    assert "globally_infeasible" not in result["metadata"]


def test_disjoint_convex_constraints_never_return_unchecked_projection():
    constraints=(dict(focus_a=Point(-2.,0.),focus_b=Point(-2.,0.),limit_km=0.),
                 dict(focus_a=Point(2.,0.),focus_b=Point(2.,0.),limit_km=0.))
    value,iterations=geometry._project_intersection(Point(0.,0.),constraints,(-2.,2.,-1.,1.),
        deadline=perf_counter()+1.,stats=stats(),max_iterations=4,projection_iterations=20,tolerance=1e-9)
    assert value is None and iterations==4


def test_bbox_projection_is_followed_by_original_constraint_validation():
    c=(dict(focus_a=Point(3.,0.),focus_b=Point(3.,0.),limit_km=0.),)
    value,_=geometry._project_intersection(Point(3.,0.),c,(-1.,1.,-1.,1.),
        deadline=perf_counter()+1.,stats=stats(),max_iterations=3,projection_iterations=20,tolerance=1e-9)
    assert value is None


def test_all_loops_respect_absolute_deadline_and_never_return_late_point(monkeypatch):
    p,routes,spec=fixture()
    clock=[0.]
    monkeypatch.setattr(geometry,"perf_counter",lambda:clock[0])
    original=geometry._project_ellipse
    def slow(*args,**kwargs):
        point=original(*args,**kwargs)
        clock[0]=2.
        return point
    monkeypatch.setattr(geometry,"_project_ellipse",slow)
    result=geometry.deadline_points(p,routes,spec,deadline=1.)
    assert result["points"]==()
    assert result["metadata"]["deadline_reached"]
    expired=geometry.deadline_points(p,routes,spec,deadline=0.)
    assert expired["points"]==()


def test_bound_counts_and_seed_reproducibility():
    p,routes,spec=fixture()
    a=geometry.deadline_points(p,routes,spec,deadline=perf_counter()+2.,max_tasks=2,max_task_pairs=1,max_seeds=2,max_points=3)
    b=geometry.deadline_points(p,routes,spec,deadline=perf_counter()+2.,max_tasks=2,max_task_pairs=1,max_seeds=2,max_points=3)
    assert a["points"]==b["points"]
    assert a["metadata"]["point_records"]==b["metadata"]["point_records"]
    assert len(a["points"])<=3
    assert len(a["metadata"]["task_constraints"])<=2


def test_custom_distance_matrix_is_explicitly_unsupported_not_called_exact():
    p,routes,spec=fixture()
    p=replace(p,distance_matrix_km=p._distances)
    result=geometry.deadline_points(p,routes,spec,deadline=perf_counter()+1.)
    assert result["points"]==()
    assert result["metadata"]["unsupported_non_euclidean_matrix"]
    assert not result["metadata"]["formula_exact_for_direct_input"]


def test_existing_relay_future_formula_is_labelled_heuristic():
    from uav_dispatch import RelayStation, TransportLeg
    p,routes,_=fixture()
    p=replace(p,relay_stations=(RelayStation(7,0.,0.),),
        leg_registry={101:TransportLeg(101,3,"RELAY_IN",7),102:TransportLeg(102,3,"RELAY_OUT",7)},
        task_relay_candidates={3:(7,)})
    routes=((1,101,-101,102,-1,-102),routes[1])
    a=geometry.v1._Cut(1,(1,),Point(-4.,0.),0.)
    b=geometry.v1._Cut(2,(2,4),Point(4.,0.),0.)
    spec=geometry.v1._Spec(0,1,a,b,(1,),(2,),(2,),(1,),())
    result=geometry.deadline_points(p,routes,spec,deadline=perf_counter()+1.)
    assert not result["metadata"]["formula_exact_for_direct_input"]
    assert not result["metadata"]["unsupported_non_euclidean_matrix"]


@pytest.mark.parametrize("change",[{"max_tasks":0},{"max_iterations":False},
    {"deadline_safety_min":-1.},{"feasibility_tolerance_km":float("nan")}])
def test_invalid_bounds_fail_loudly(change):
    p,routes,spec=fixture()
    with pytest.raises(ValueError):
        geometry.deadline_points(p,routes,spec,deadline=perf_counter()+1.,**change)
