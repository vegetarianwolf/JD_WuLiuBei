"""Bounded, verified deadline-region seeds for mobile exchange coordinates.

For a fixed swapped suffix, one future delivery's optimistic deadline is the
intersection of two ellipses. Pairing a target on each UAV gives four convex
constraints and can preserve/save both routes' deadlines at once. Dykstra
projection is only a candidate generator: every returned point is checked by
the original sum-of-distance inequalities, independently of convergence.
The formula is exact for DIRECT input and a heuristic if old relay waits exist.
No full candidate is accepted, no optimizer is started, and failure to find a
seed does not certify infeasibility of the whole mobile scheduling problem.
"""
from __future__ import annotations

from itertools import product
from math import hypot, isfinite, sqrt
from time import perf_counter

from uav_dispatch.model import Point
try:
    import mobile_relay_refinement as v1
except ImportError:
    from . import mobile_relay_refinement as v1


class _Deadline(Exception):
    pass


def _check(deadline, stats):
    if perf_counter() >= deadline:
        stats["deadline_reached"] = True
        raise _Deadline


def _residual(point, constraint):
    return (point.distance_to(constraint["focus_a"])
            + point.distance_to(constraint["focus_b"]) - constraint["limit_km"])


def _project_segment(point, a, b):
    dx, dy = b.x-a.x, b.y-a.y
    norm = dx*dx+dy*dy
    ratio = max(0., min(1., ((point.x-a.x)*dx+(point.y-a.y)*dy)/norm)) if norm else 0.
    return Point(a.x+ratio*dx, a.y+ratio*dy)


def _project_ellipse(point, constraint, *, deadline, stats, iterations, tolerance):
    """Euclidean projection, with finite bisection and degenerate segments."""
    _check(deadline, stats)
    first, second, limit = constraint["focus_a"], constraint["focus_b"], constraint["limit_km"]
    distance = first.distance_to(second)
    if limit < distance-tolerance:
        return None
    if _residual(point, constraint) <= 0.:
        return point
    if limit <= distance+tolerance:
        return _project_segment(point, first, second)
    center = v1._mid(first, second)
    a, c = limit/2., distance/2.
    b = sqrt(max(0., a*a-c*c))
    if b <= 1e-14:
        return _project_segment(point, first, second)
    ex, ey = ((second.x-first.x)/distance, (second.y-first.y)/distance) if distance else (1., 0.)
    x, y = (point.x-center.x)*ex+(point.y-center.y)*ey, -(point.x-center.x)*ey+(point.y-center.y)*ex
    aa, bb = a*a, b*b
    def value(multiplier):
        return aa*x*x/(aa+multiplier)**2+bb*y*y/(bb+multiplier)**2
    lower, upper = 0., max(aa, bb, a*abs(x), b*abs(y), 1.)
    for _ in range(iterations):
        _check(deadline, stats)
        if value(upper) <= 1.:
            break
        upper *= 2.
    for _ in range(iterations):
        _check(deadline, stats)
        middle = (lower+upper)/2.
        if value(middle) > 1.:
            lower = middle
        else:
            upper = middle
    xx, yy = aa*x/(aa+upper), bb*y/(bb+upper)
    return Point(center.x+xx*ex-yy*ey, center.y+xx*ey+yy*ex)


def _box_project(point, box):
    return Point(max(box[0], min(box[1], point.x)), max(box[2], min(box[3], point.y)))


def _feasible(point, constraints, box, tolerance):
    residuals = [_residual(point, constraint) for constraint in constraints]
    violation = max(box[0]-point.x, point.x-box[1], box[2]-point.y, point.y-box[3], 0.)
    return max((*residuals, violation)) <= tolerance, residuals


def _project_intersection(seed, constraints, box, *, deadline, stats,
                          max_iterations, projection_iterations, tolerance):
    point, corrections = _box_project(seed, box), [(0., 0.) for _ in range(len(constraints)+1)]
    for iteration in range(max_iterations):
        _check(deadline, stats)
        for index in range(len(corrections)):
            _check(deadline, stats)
            dx, dy = corrections[index]
            adjusted = Point(point.x+dx, point.y+dy)
            if index == len(constraints):
                projected = _box_project(adjusted, box)
            else:
                projected = _project_ellipse(adjusted, constraints[index], deadline=deadline,
                    stats=stats, iterations=projection_iterations, tolerance=tolerance)
            stats["projection_calls"] += 1
            if projected is None:
                return None, iteration+1
            corrections[index] = (adjusted.x-projected.x, adjusted.y-projected.y)
            point = projected
        valid, residuals = _feasible(point, constraints, box, tolerance)
        if valid:
            return (point, residuals), iteration+1
    return None, max_iterations


def _targets(problem, routes, spec, deadline, stats, safety):
    """Extract all real final deliveries from the physically swapped suffix."""
    placeholder = v1._mid(spec.left.point, spec.right.point)
    prototype, legs, descriptor = v1._prototype(problem, routes, spec, placeholder)
    anchors = (spec.left.point, spec.right.point)
    times = (spec.left.actual_time, spec.right.actual_time)
    original_clocks = {}
    for aircraft, route in enumerate(routes):
        previous, clock = problem.home_points()[aircraft], 0.
        for visit in route:
            _check(deadline, stats)
            point, task = v1._event(problem, visit, {}, placeholder)
            clock += previous.distance_to(point)/problem.speed_km_per_min
            previous = point
            if task is not None:
                original_clocks[task] = clock
    median = None
    targets, terminals = [], []
    for side, (aircraft, cut) in enumerate(((spec.a,spec.left),(spec.b,spec.right))):
        suffix = prototype[aircraft][cut.position+2*len(spec.group_a):]
        if not suffix:
            continue
        first, _ = v1._event(problem, suffix[0], legs, placeholder)
        terminals.append(first)
        previous, elapsed = first, 0.
        for position, visit in enumerate(suffix):
            _check(deadline, stats)
            point, final_task = v1._event(problem, visit, legs, placeholder)
            elapsed += previous.distance_to(point)/problem.speed_km_per_min
            previous = point
            if final_task is None:
                continue
            target = problem.task(final_task).deadline_min-safety
            constraints = tuple(dict(focus_a=anchor, focus_b=first,
                limit_km=problem.speed_km_per_min*(target-start-elapsed))
                for anchor,start in zip(anchors,times))
            targets.append(dict(task_id=final_task, aircraft=aircraft, side=side,
                suffix_delivery_position=position, suffix_head=first, suffix_constant_min=elapsed,
                target_deadline_min=target, original_deadline_min=problem.task(final_task).deadline_min,
                original_no_wait_clock_min=original_clocks[final_task],
                minimum_formula_clock_min=max(start+anchor.distance_to(first)/problem.speed_km_per_min
                    for anchor,start in zip(anchors,times))+elapsed, constraints=constraints))
    if len(terminals) != 2:
        return (), (), None
    median = v1._geometric_median((*anchors,*terminals))
    sync = max(start+anchor.distance_to(median)/problem.speed_km_per_min for anchor,start in zip(anchors,times))
    for target in targets:
        clock = sync+median.distance_to(target["suffix_head"])/problem.speed_km_per_min+target["suffix_constant_min"]
        target["baseline_optimistic_clock_min"] = clock
        target["baseline_optimistic_lateness_min"] = clock-target["target_deadline_min"]
        target["eligible_deadline_rescue"] = (target["original_no_wait_clock_min"] > target["original_deadline_min"]+1e-9
            and target["minimum_formula_clock_min"] <= target["target_deadline_min"])
    targets.sort(key=lambda t:(not t["eligible_deadline_rescue"],
        abs(t["original_no_wait_clock_min"]-t["original_deadline_min"]),t["suffix_delivery_position"],t["task_id"]))
    return tuple(targets), (*anchors,*terminals), median


def _constraint_record(constraint):
    return dict(focus_a=[constraint["focus_a"].x,constraint["focus_a"].y],
                focus_b=[constraint["focus_b"].x,constraint["focus_b"].y], limit_km=constraint["limit_km"])


def deadline_points(problem, routes, spec, *, deadline, max_tasks=8,
                    max_task_pairs=4, max_seeds=4, max_iterations=40,
                    projection_iterations=36, max_points=24,
                    deadline_safety_min=1e-7, feasibility_tolerance_km=1e-9):
    """Return verified ``points`` and reproducible per-task ellipse metadata.

    At most ``max_tasks`` individual task targets and ``max_task_pairs`` cross-
    route target pairs are projected from fixed deterministic seeds. An absent
    returned point is a bounded search result, not an optimality certificate.
    The caller must still score and fully validate the physical candidate.
    """
    bounds=(max_tasks,max_task_pairs,max_seeds,max_iterations,projection_iterations,max_points)
    if (any(type(value) is not int or value<1 for value in bounds) or not isfinite(deadline)
        or not isfinite(deadline_safety_min) or deadline_safety_min<0
        or not isfinite(feasibility_tolerance_km) or feasibility_tolerance_km<0):
        raise ValueError("positive integer bounds and finite nonnegative geometry tolerances required")
    if len(spec.group_a) != len(spec.group_b) or len(spec.group_a) not in (1,2):
        raise ValueError("reciprocal groups must each contain the same one or two tasks")
    stats=dict(deadline_reached=False, targets_examined=0, task_sets_examined=0,
        projection_calls=0, projection_iterations=0, individual_ellipse_empty=0,
        bounded_projection_failures=0, duplicate_points=0,
        formula_exact_for_direct_input=not any(abs(v) in (problem.leg_registry or {}) for r in routes for v in r),
        unsupported_non_euclidean_matrix=problem.distance_matrix_km is not None,
        deadline_safety_min=deadline_safety_min, feasibility_tolerance_km=feasibility_tolerance_km,
        configuration=dict(max_tasks=max_tasks,max_task_pairs=max_task_pairs,max_seeds=max_seeds,
            max_iterations=max_iterations,projection_iterations=projection_iterations,max_points=max_points),
        task_constraints=[], point_records=[], target_sets=[])
    points, seen = [], set()
    try:
        _check(deadline, stats)
        if stats["unsupported_non_euclidean_matrix"]:
            stats["formula_exact_for_direct_input"] = False
            return dict(points=(),metadata=stats)
        targets, terminals, median = _targets(problem,routes,spec,deadline,stats,deadline_safety_min)
        stats["eligible_deadline_rescues"]=[t["task_id"] for t in targets if t["eligible_deadline_rescue"]]
        stats["optimistic_future_late_count_individual_minima"]=sum(
            t["minimum_formula_clock_min"]>t["original_deadline_min"]+1e-9 for t in targets)
        stats["optimistic_future_lateness_min_individual_minima"]=sum(
            max(0.,t["minimum_formula_clock_min"]-t["original_deadline_min"]) for t in targets)
        feasible_targets=[]
        for target in targets:
            _check(deadline, stats)
            if any(c["limit_km"] < c["focus_a"].distance_to(c["focus_b"])-feasibility_tolerance_km
                   for c in target["constraints"]):
                stats["individual_ellipse_empty"]+=1
                continue
            feasible_targets.append(target)
        representatives=[next((t for t in feasible_targets if t["side"]==side),None) for side in (0,1)]
        selected=[t for t in representatives if t is not None][:max_tasks]
        selected=(selected+[t for t in feasible_targets if t not in selected])[:max_tasks]
        stats["targets_examined"]=len(targets)
        stats["task_constraints"]=[dict(task_id=t["task_id"], aircraft=t["aircraft"],
            suffix_delivery_position=t["suffix_delivery_position"],suffix_constant_min=t["suffix_constant_min"],
            target_deadline_min=t["target_deadline_min"],baseline_optimistic_clock_min=t["baseline_optimistic_clock_min"],
            original_no_wait_clock_min=t["original_no_wait_clock_min"],minimum_formula_clock_min=t["minimum_formula_clock_min"],
            eligible_deadline_rescue=t["eligible_deadline_rescue"],
            constraints=[_constraint_record(c) for c in t["constraints"]]) for t in selected]
        if not selected:
            return dict(points=(),metadata=stats)
        box=(min(p.x for p in terminals),max(p.x for p in terminals),
             min(p.y for p in terminals),max(p.y for p in terminals))
        task_sets=[(target,) for target in selected]
        pairs=sorted(product((t for t in selected if t["side"]==0),
                             (t for t in selected if t["side"]==1)),key=lambda pair:(
            sum(abs(t["baseline_optimistic_lateness_min"]) for t in pair),tuple(t["task_id"] for t in pair)))
        task_sets=task_sets+[tuple(pair) for pair in pairs[:max_task_pairs]]
        for task_set in task_sets:
            _check(deadline, stats)
            constraints=tuple(c for t in task_set for c in t["constraints"])
            seeds=[median,v1._earliest_meeting(spec.left.point,spec.right.point,
                spec.left.actual_time,spec.right.actual_time,problem.speed_km_per_min)]
            seeds.extend(t["suffix_head"] for t in task_set)
            seeds.append(v1._mid(spec.left.point,spec.right.point))
            unique=[]
            for seed in seeds:
                if seed not in unique:
                    unique.append(seed)
            stats["task_sets_examined"]+=1
            stats["target_sets"].append([t["task_id"] for t in task_set])
            for seed in unique[:max_seeds]:
                _check(deadline, stats)
                projected, iterations=_project_intersection(seed,constraints,box,deadline=deadline,
                    stats=stats,max_iterations=max_iterations,projection_iterations=projection_iterations,
                    tolerance=feasibility_tolerance_km)
                stats["projection_iterations"]+=iterations
                if projected is None:
                    stats["bounded_projection_failures"]+=1
                    continue
                point,residuals=projected
                # Recheck original inequalities after bounding/projection.
                valid,residuals=_feasible(point,constraints,box,feasibility_tolerance_km)
                if not valid:
                    stats["bounded_projection_failures"]+=1
                    continue
                identity=(round(point.x,11),round(point.y,11))
                if identity in seen:
                    stats["duplicate_points"]+=1
                    continue
                _check(deadline, stats)
                seen.add(identity)
                points.append(point)
                sync=max(spec.left.actual_time+spec.left.point.distance_to(point)/problem.speed_km_per_min,
                         spec.right.actual_time+spec.right.point.distance_to(point)/problem.speed_km_per_min)
                stats["point_records"].append(dict(point=[point.x,point.y],
                    target_task_ids=[t["task_id"] for t in task_set],residuals_km=residuals,
                    constraints=[_constraint_record(c) for c in constraints],
                    projected_box=list(box),synchronization_min=sync,
                    optimistic_delivery_times_min={str(t["task_id"]):sync+point.distance_to(t["suffix_head"])
                        /problem.speed_km_per_min+t["suffix_constant_min"] for t in task_set},
                    bounds_verified=True))
                if len(points)>=max_points:
                    return dict(points=tuple(points),metadata=stats)
    except _Deadline:
        pass
    return dict(points=tuple(points),metadata=stats)
