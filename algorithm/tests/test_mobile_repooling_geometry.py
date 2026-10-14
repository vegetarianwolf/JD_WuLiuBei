"""Tiny geometry and complete DAG checks; no optimization is started."""
from itertools import product
import json
from random import Random
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import mobile_repooling_geometry as geometry


def context(incoming=((-1., 0.), (1., 0.), (0., 0.)),
            outgoing=((-1., 2.), (1., 2.), (0., 3.)), times=(0., 0., 0.), speed=1.):
    return geometry.prepare_geometry(incoming, outgoing, times, speed=speed)


def physical_candidate(ctx, point):
    """Embed independent fixed prefixes in real IN/OUT and DIRECT routes."""
    a, b, c = ctx.incoming_points
    va, vb, vc = ctx.outgoing_points
    speed, (ta, tb, tc) = ctx.speed, ctx.prefix_times
    tasks = [Task(1, a, vc, 100.), Task(2, b, Point(vc.x + 1., vc.y), 100.),
             Task(5, c, c, 100.)]
    if va is not None:
        tasks.append(Task(3, va, Point(va.x+.7, va.y), 100.))
    if vb is not None:
        tasks.append(Task(4, vb, Point(vb.x+.9, vb.y), 100.))
    registry = {101: TransportLeg(101, 1, "RELAY_IN", 1),
                102: TransportLeg(102, 1, "RELAY_OUT", 1),
                103: TransportLeg(103, 2, "RELAY_IN", 1),
                104: TransportLeg(104, 2, "RELAY_OUT", 1)}
    homes = tuple(Point(u.x-t*speed, u.y) for u, t in zip(ctx.incoming_points, ctx.prefix_times))
    problem = Problem(tuple(tasks), drone_count=3, max_tasks_per_drone=3, capacity=2,
        speed_km_per_min=speed, drone_homes=homes, relay_stations=(RelayStation(1,point.x,point.y),),
        leg_registry=registry, task_relay_candidates={1:(1,),2:(1,)})
    timing = geometry.receiver_first_feasible(ctx,point)
    takes = tuple(102 if i == 0 else 104 for i in timing["pickup_source_order"])
    routes = ((101,-101)+((3,-3) if va is not None else ()),
              (103,-103)+((4,-4) if vb is not None else ()),
              (5,-5)+takes+(-102,-104))
    const = speed*(ta+tb+tc) + (.7 if va is not None else 0.) + (.9 if vb is not None else 0.) + 1.
    return problem,routes,const


def test_receiver_first_existence_exact_conditions_and_focus_witness():
    ctx = context(times=(0.,.3,.5))
    result = geometry.receiver_first_possible(ctx)
    assert result["possible"] and result["source_at_recipient_focus_min"] == [1.,1.3]
    assert geometry.receiver_first_feasible(ctx,ctx.incoming_points[2])["feasible"]
    no = context(times=(0.,0.,1.001))
    assert not geometry.receiver_first_possible(no)["possible"]
    assert geometry.points(no,deadline=perf_counter()+1.)["points"] == ()
    assert geometry.project_to_feasible_star(no,Point(100.,100.))["point"] is None
    for x,y in product((-100.,-1.,0.,1.,100.),repeat=2):
        assert not geometry.receiver_first_feasible(no,Point(x,y))["feasible"]


def test_physical_asynchronous_pickups_source_leave_times_and_split_receiver_wait():
    ctx = context(times=(2.,0.,0.))
    point = Point(0.,0.)
    timing = geometry.receiver_first_feasible(ctx,point)
    assert timing["arrival_times_min"] == [3.,1.,0.]
    assert timing["pickup_source_order"] == [1,0]
    assert timing["receiver_wait_split_min"] == [1.,2.]
    assert timing["recipient_waiting_loads"] == [0,1]
    assert timing["source_departure_times_min"] == [3.,1.]
    problem,routes,_ = physical_candidate(ctx,point)
    full = evaluate_solution(problem,routes,_force_full=True)
    scheduled = GlobalRelayEvaluator(problem).evaluate(routes)
    assert full.valid and scheduled.feasible and full.score == scheduled.score
    assert scheduled.event_times_min[(0,1)] == 3.
    assert scheduled.event_times_min[(1,1)] == 1.
    assert scheduled.event_times_min[(2,2)] == 1.
    assert scheduled.event_times_min[(2,3)] == 3.
    assert scheduled.total_waiting_min == 3.
    assert timing["ground_cache_min"] == 0.


def test_equal_prefixes_coincident_foci_and_tie_pickup_order():
    ctx = context(incoming=((0.,0.),)*3,times=(2.,2.,2.),speed=2.)
    timing = geometry.receiver_first_feasible(ctx,Point(3.,4.))
    assert timing["feasible"] and timing["arrival_times_min"] == [4.5]*3
    assert timing["pickup_source_order"] == [0,1]
    assert timing["receiver_wait_split_min"] == [0.,0.]
    projected = geometry.project_to_feasible_star(ctx,Point(100.,100.))
    assert projected["point"] == Point(100.,100.) and projected["metadata"]["fraction"] == 1.


def test_only_recipient_focus_feasible_when_opposite_source_bounds_are_tight():
    ctx = context(times=(0.,0.,1.))
    assert geometry.receiver_first_possible(ctx)["possible"]
    for seed in (Point(1.,0.),Point(-1.,0.),Point(0.,10.),Point(4.,7.)):
        point = geometry.project_to_feasible_star(ctx,seed)["point"]
        assert geometry.receiver_first_feasible(ctx,point)["feasible"]
        assert point.distance_to(Point(0.,0.)) < 1e-12


def test_nonconvex_region_star_projection_is_verified_but_not_global_optimum():
    ctx = context(incoming=((-1.,0.),(-1.,0.),(0.,0.)),times=(.5,.5,0.))
    assert geometry.receiver_first_feasible(ctx,Point(-2.,3.))["feasible"]
    assert geometry.receiver_first_feasible(ctx,Point(-2.,-3.))["feasible"]
    assert not geometry.receiver_first_feasible(ctx,Point(-2.,0.))["feasible"]
    projected = geometry.project_to_feasible_star(ctx,Point(-2.,0.))
    assert projected["point"].x == pytest.approx(-.75)
    assert geometry.receiver_first_feasible(ctx,projected["point"])["feasible"]
    assert not projected["metadata"]["globally_optimal"]
    c = ctx.incoming_points[2]
    for source in (0,1):
        residuals = []
        for fraction in (0.,.2,.4,.6,.8,1.):
            p = Point(-2.*fraction,3.*fraction)
            arrivals = geometry.receiver_first_feasible(ctx,p)["arrival_times_min"]
            residuals.append(arrivals[2]-arrivals[source])
        assert all(a <= b + 1e-12 for a,b in zip(residuals,residuals[1:]))


@pytest.mark.parametrize("open_source", [None,0,1])
def test_terminal_matching_and_clock_bounds_below_complete_real_scores(open_source):
    rng = Random(8)
    for _ in range(8):
        incoming = tuple(Point(rng.uniform(-3.,3.),rng.uniform(-3.,3.)) for _ in range(3))
        outgoing = [Point(rng.uniform(-3.,3.),rng.uniform(-3.,3.)) for _ in range(3)]
        if open_source is not None:
            outgoing[open_source] = None
        ctx = geometry.prepare_geometry(incoming,outgoing,(rng.uniform(0.,2.),rng.uniform(0.,2.),0.),speed=1.7)
        clocks = geometry.clock_lower_bounds(ctx)
        for _ in range(6):
            seed = Point(rng.uniform(-5.,5.),rng.uniform(-5.,5.))
            point = geometry.project_to_feasible_star(ctx,seed)["point"]
            problem,routes,constant = physical_candidate(ctx,point)
            scheduled = GlobalRelayEvaluator(problem).evaluate(routes)
            full = evaluate_solution(problem,routes,_force_full=True)
            assert full.valid and scheduled.feasible
            lower = geometry.distance_lower_bound(ctx,constant_distance=constant)
            assert lower["distance_km"] <= full.score.distance_km + 1e-12
            for side in (0,1):
                if outgoing[side] is not None:
                    assert clocks["first_future_clock_min"][side] <= scheduled.event_times_min[(side,2)] + 1e-12
                else:
                    assert clocks["first_future_clock_min"][side] is None
            assert clocks["first_future_clock_min"][2] <= scheduled.event_times_min[(2,4)] + 1e-12
            json.dumps(lower["metadata"],allow_nan=False)


def test_odd_open_tail_bound_is_near_perfect_and_no_invented_return_home():
    ctx = context(outgoing=(None,(1.,2.),(0.,3.)))
    lower = geometry.distance_lower_bound(ctx)
    assert lower["metadata"]["matching_count"] == 15
    assert lower["metadata"]["unmatched_terminal"] is not None
    assert "vA" not in lower["metadata"]["terminal_labels"]
    two_open = context(outgoing=(None,None,(0.,3.)))
    assert geometry.distance_lower_bound(two_open)["metadata"]["matching_count"] == 3


def test_deterministic_points_keep_anchor_even_with_limit_one_and_are_all_physical():
    ctx = context(times=(.2,.3,0.))
    one = geometry.points(ctx,deadline=perf_counter()+1.,limit=1)
    assert one["points"] == (ctx.incoming_points[2],)
    first = geometry.points(ctx,deadline=perf_counter()+1.,limit=8)
    second = geometry.points(ctx,deadline=perf_counter()+1.,limit=8)
    assert first["points"] == second["points"]
    assert first["points"][0] == ctx.incoming_points[2]
    assert all(geometry.receiver_first_feasible(ctx,p)["feasible"] for p in first["points"])
    assert len(first["points"]) <= 8
    assert not first["metadata"]["globally_optimal"]
    json.dumps(first["metadata"],allow_nan=False)


def test_deadline_ellipse_tags_are_rechecked_after_nonconvex_star_projection():
    ctx = context(incoming=((-1.,0.),(-1.,0.),(0.,0.)),
        outgoing=((2.,0.),(2.,0.),(-2.,0.)),times=(.5,.5,0.))
    constraints = tuple(dict(prefix_source=source,outgoing=Point(-2.,0.),residual_deadline_min=2.5)
                        for source in (0,1))
    result = geometry.points(ctx,deadline=perf_counter()+1.,limit=20,deadline_constraints=constraints)
    assert result["points"]
    for point, record in zip(result["points"],result["metadata"]["point_records"]):
        assert geometry.receiver_first_feasible(ctx,point)["feasible"]
        for set_id in record["verified_deadline_sets"]:
            target = result["metadata"]["deadline_sets"][set_id]
            assert all(point.distance_to(Point(*c["focus_a"]))+point.distance_to(Point(*c["focus_b"]))
                <= c["limit_km"]+1e-9 for c in target["constraints"])


def test_geometric_median_is_only_a_seed_not_a_distance_lower_bound():
    ctx = context()
    lower = geometry.distance_lower_bound(ctx)
    assert lower["metadata"]["geometric_median_is_lower_bound"] is False
    assert lower["metadata"]["jointly_attainable_not_claimed"]
    assert geometry.clock_lower_bounds(ctx)["metadata"]["jointly_attainable_not_claimed"]


def test_two_critical_deadline_sets_share_bounded_joint_projection_and_caller_order():
    ctx = context()
    constraints = (dict(prefix_source=0,outgoing=ctx.outgoing_points[0],residual_deadline_min=7.),
                   dict(prefix_source=2,outgoing=ctx.outgoing_points[0],residual_deadline_min=7.),
                   dict(prefix_source=0,outgoing=ctx.outgoing_points[2],residual_deadline_min=8.),
                   dict(prefix_source=1,outgoing=ctx.outgoing_points[2],residual_deadline_min=8.))
    result = geometry.points(ctx,deadline=perf_counter()+1.,deadline_constraints=constraints,
        max_joint_deadline_sets=1)
    stats = result["metadata"]
    assert stats["joint_deadline_sets_examined"] == [[0,1]]
    assert stats["deadline_sets"][0]["outgoing"] == [-1.,2.]
    assert stats["deadline_sets"][1]["outgoing"] == [0.,3.]
    assert all(geometry.receiver_first_feasible(ctx,p)["feasible"] for p in result["points"])
    assert any(record["verified_deadline_sets"] == [0,1] for record in stats["point_records"])


def test_clock_safety_margin_does_not_overcount_exact_deadline_lateness():
    ctx = context()
    clocks = geometry.clock_lower_bounds(ctx)
    raw = clocks["metadata"]["raw_first_future_clock_min"]
    for side in range(3):
        optimistic = clocks["first_future_clock_min"][side]
        for deadline in (raw[side],raw[side]-5e-10,raw[side]+5e-10):
            assert max(0.,optimistic-deadline) <= geometry.LATENESS_EPSILON_MIN
    assert geometry.distance_lower_bound(ctx)["metadata"]["conservative_distance_margin_km"] == 1e-10


def test_expired_projection_and_point_search_do_not_return_unchecked_points():
    ctx = context()
    assert geometry.project_to_feasible_star(ctx,Point(4.,5.),deadline=perf_counter()-1.)["point"] is None
    result = geometry.points(ctx,deadline=perf_counter()-1.)
    assert result["points"] == () and result["metadata"]["deadline_reached"]


def test_custom_matrix_nonfinite_data_and_bad_bounded_configuration_are_rejected():
    with pytest.raises(ValueError,match="custom distance"):
        geometry.prepare_geometry(((0.,0.),)*3,((1.,1.),)*3,(0.,0.,0.),speed=1.,distance_matrix_km=((0.,),))
    for changes in (dict(speed=0.),dict(prefix_times=(0.,float("inf"),0.)),
                    dict(outgoing_points=(None,None,None)),dict(incoming_points=((0.,0.),)*2)):
        args = dict(incoming_points=((0.,0.),)*3,outgoing_points=((1.,1.),)*3,prefix_times=(0.,0.,0.),speed=1.)
        args.update(changes)
        with pytest.raises(ValueError):
            geometry.prepare_geometry(**args)
    with pytest.raises(ValueError):
        geometry.points(context(),deadline=perf_counter()+1.,limit=True)
