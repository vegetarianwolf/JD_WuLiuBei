"""Bounded deadline-aware coordinate search for mobile reciprocal exchange.

The physical model, final checker and topology snapshots are inherited from
the frozen v1 research method. This version additionally optimizes rendezvous
coordinates over real complete task scores. Pattern points may be worse than
the incumbent while searching; only a complete verified final improvement can
be adopted. A deadline-count change on a search segment triggers bounded
bisection so the shortlist can include points just inside a time boundary.
"""
from __future__ import annotations

from math import isclose, isfinite, sqrt
from time import perf_counter

from uav_dispatch.model import Point
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import mobile_relay_refinement as v1
except ImportError:
    from . import mobile_relay_refinement as v1


def _check(deadline, stats):
    if perf_counter() >= deadline:
        stats["deadline_reached"] = True
        raise v1._Deadline


def _optimize_points(problem, routes, spec, initial_points, weights, scales, *,
                     deadline, stats, seeds, rounds, probe_limit, boundary_steps,
                     min_step, box):
    """Bounded pattern search ranked by complete physical optimistic keys.

    For a DIRECT checkpoint each probe score is exact. If old handoffs exist,
    their wait constraints are removed consistently, so probes rank a genuine
    optimistic score; adoption still uses the complete new handoff DAG.
    """
    cache, probes, boundary_probes = {}, 0, 0
    def probe(point):
        nonlocal probes, boundary_probes
        key = (round(point.x, 11), round(point.y, 11))
        if key in cache:
            return cache[key]
        _check(deadline, stats)
        if probes >= probe_limit:
            raise _ProbeLimit
        candidate, legs, descriptor = v1._prototype(problem, routes, spec, point)
        score = v1._fast_lower_score(problem, candidate, legs, point, descriptor)
        _check(deadline, stats)
        record = (v1.weighted_key(score, weights, scales), point, score)
        cache[key] = record
        probes += 1
        stats["coordinate_probes"] += 1
        return record
    def boundary(left, right):
        nonlocal boundary_probes
        if left[2].late_count == right[2].late_count:
            return
        first_count = left[2].late_count
        for _ in range(boundary_steps):
            middle = probe(v1._mid(left[1], right[1]))
            boundary_probes += 1
            stats["deadline_boundary_probes"] += 1
            if middle[2].late_count == first_count:
                left = middle
            else:
                right = middle
    xmin, xmax, ymin, ymax = box
    diagonal = sqrt((xmax-xmin)**2 + (ymax-ymin)**2)
    directions = ((1.,0.),(-1.,0.),(0.,1.),(0.,-1.),
                  (1./sqrt(2.),1./sqrt(2.)),(1./sqrt(2.),-1./sqrt(2.)),
                  (-1./sqrt(2.),1./sqrt(2.)),(-1./sqrt(2.),-1./sqrt(2.)))
    try:
        for point in initial_points:
            probe(point)
        starts = sorted(cache.values(), key=lambda r:(r[0],r[1].x,r[1].y))[:seeds]
        for start in starts:
            current, step = start, max(min_step, diagonal/4.)
            for _ in range(rounds):
                _check(deadline, stats)
                choices = [current]
                for dx,dy in directions:
                    point = Point(max(xmin,min(xmax,current[1].x+dx*step)),
                                  max(ymin,min(ymax,current[1].y+dy*step)))
                    neighbor = probe(point)
                    choices.append(neighbor)
                    boundary(current,neighbor)
                # All timely evaluated boundary points compete with the seeds,
                # rather than requiring the first station move to beat input J.
                selected = min((*choices,*cache.values()),key=lambda r:(r[0],r[1].x,r[1].y))
                if selected[0] < current[0]:
                    current = selected
                else:
                    step /= 2.
                if step < min_step:
                    break
    except _ProbeLimit:
        stats["coordinate_probe_cutoffs"] += 1
    records = sorted(cache.values(),key=lambda r:(r[0],r[1].x,r[1].y))
    return records,dict(probes=probes,boundary_probes=boundary_probes,
        initial_point_count=len(initial_points),search_box=list(box),
        best_point=[records[0][1].x,records[0][1].y] if records else None,
        best_optimistic_key=list(records[0][0]) if records else None)


class _ProbeLimit(Exception):
    pass


def refine(problem,routes,*,deadline,weights,scales,seed=2026101496,
           max_pairs=256,point_limit=6,max_moves=3,full_candidate_limit=16,
           pattern_seeds=2,pattern_rounds=6,probe_limit=112,boundary_steps=10,
           min_step=.005,full_points_per_group=2):
    """Optimize a fixed bounded set of physical mobile exchange coordinates."""
    weights,scales=tuple(weights),tuple(scales)
    if (len(weights)!=3 or len(scales)!=3 or any(not isfinite(x) or x<=0 for x in (*weights,*scales))
            or not isclose(sum(weights),1.) or not isfinite(deadline)):
        raise ValueError("finite positive normalized weights/scales and deadline required")
    limits=(max_pairs,point_limit,max_moves,full_candidate_limit,pattern_seeds,
            pattern_rounds,probe_limit,boundary_steps,full_points_per_group)
    if any(not isinstance(x,int) or isinstance(x,bool) or x<1 for x in limits) or not isfinite(min_step) or min_step<=0:
        raise ValueError("positive integer bounds and finite positive min_step required")
    started=perf_counter()
    best=tuple(tuple(r) for r in routes)
    if len(best)>problem.drone_count:
        raise ValueError("too many routes")
    best+=((),)*(problem.drone_count-len(best))
    checked=evaluate_solution(problem,best,_force_full=True)
    if not checked.valid:
        raise ValueError(f"invalid initial solution: {checked.violations}")
    initial_key=best_key=v1.weighted_key(checked.score,weights,scales)
    stats=dict(method="Deadline-Aware-Synchronous-Mobile-Relay",model_extension="continuous_airborne_rendezvous",
        seed=seed,specifications_generated=0,specifications_examined=0,points_examined=0,
        optimistic_bounds_pruned=0,new_problems_built=0,global_evaluations=0,full_evaluations=1,
        accepted_improvements=0,rejected_cycles=0,rejected_invalid=0,deadline_reached=False,
        unsupported_non_euclidean_matrix=problem.distance_matrix_km is not None,
        coordinate_probes=0,deadline_boundary_probes=0,coordinate_probe_cutoffs=0,
        point_searches=[],mobile_meetings=[],first_complete_candidate=None,best_constructed_candidate=None,
        full_candidate_snapshots=[],accepted_moves=[],shortlist_coverage=[],
        input_snapshot=v1._snapshot(problem,best,checked,weights,scales,[]),
        configuration=dict(max_pairs=max_pairs,point_limit=point_limit,max_moves=max_moves,
            full_candidate_limit=full_candidate_limit,pattern_seeds=pattern_seeds,
            pattern_rounds=pattern_rounds,probe_limit=probe_limit,boundary_steps=boundary_steps,
            min_step=min_step,full_points_per_group=full_points_per_group))
    descriptors=[]
    try:
        _check(deadline,stats)
        if problem.distance_matrix_km is not None:
            raise v1._Unsupported
        for _ in range(max_moves):
            _check(deadline,stats)
            schedule=GlobalRelayEvaluator(problem).evaluate(best)
            specs=v1._specifications(problem,best,schedule,weights,scales,seed,max_pairs,deadline,stats)
            stats["shortlist_coverage"].append(dict(specification_count=len(specs),
                distinct_task_group_pairs=len({(s.a,s.b,tuple(sorted(s.group_a)),tuple(sorted(s.group_b))) for s in specs}),
                distinct_route_pairs=len({(s.a,s.b) for s in specs}),group_sizes=sorted({len(s.group_a) for s in specs})))
            improved=False
            for spec in specs:
                _check(deadline,stats)
                stats["specifications_examined"]+=1
                ma=v1._slot_map(best[spec.a],spec.left,spec.group_a,spec.receive_a)
                mb=v1._slot_map(best[spec.b],spec.right,spec.group_b,spec.receive_b)
                def successor(aircraft,cut,mapping):
                    visit=best[aircraft][cut.position]
                    return problem.task(mapping[-visit]).delivery if visit<0 and -visit in mapping else problem.point_for_visit(visit)
                va,vb=successor(spec.a,spec.left,ma),successor(spec.b,spec.right,mb)
                points=v1._points(spec.left.point,va,spec.right.point,vb,spec.left.actual_time,spec.right.actual_time,
                                 problem.speed_km_per_min,point_limit)
                # Projection onto the terminal convex hull cannot increase any
                # terminal distance. Its enclosing box is a bounded superset;
                # no fine grid over the complete service area is introduced.
                terminals=(spec.left.point,va,spec.right.point,vb)
                box=(min(p.x for p in terminals),max(p.x for p in terminals),
                     min(p.y for p in terminals),max(p.y for p in terminals))
                records,trace=_optimize_points(problem,best,spec,points,weights,scales,
                    deadline=deadline,stats=stats,seeds=pattern_seeds,rounds=pattern_rounds,
                    probe_limit=probe_limit,boundary_steps=boundary_steps,min_step=min_step,box=box)
                stats["point_searches"].append(dict(task_groups=[list(spec.group_a),list(spec.group_b)],
                    aircraft=[spec.a,spec.b],cut_positions=[spec.left.position,spec.right.position],**trace))
                for lower_key,point,lower in records[:full_points_per_group]:
                    _check(deadline,stats)
                    stats["points_examined"]+=1
                    if lower_key[0]>best_key[0]+1e-12:
                        stats["optimistic_bounds_pruned"]+=1
                        continue
                    if stats["new_problems_built"]>=full_candidate_limit:
                        break
                    candidate,new_legs,descriptor=v1._prototype(problem,best,spec,point)
                    changed=v1._problem(problem,new_legs,descriptor)
                    stats["new_problems_built"]+=1
                    _check(deadline,stats)
                    global_score=GlobalRelayEvaluator(changed).evaluate(candidate)
                    stats["global_evaluations"]+=1
                    _check(deadline,stats)
                    if not global_score.feasible:
                        stats["rejected_cycles"]+=1
                        continue
                    full=evaluate_solution(changed,candidate,_force_full=True)
                    stats["full_evaluations"]+=1
                    _check(deadline,stats)
                    if not full.valid:
                        stats["rejected_invalid"]+=1
                        continue
                    physical=v1._physical_meetings(changed,candidate,descriptors+[descriptor],global_score)
                    snapshot=v1._snapshot(changed,candidate,full,weights,scales,physical)
                    _check(deadline,stats)
                    stats["full_candidate_snapshots"].append(snapshot)
                    if stats["first_complete_candidate"] is None:
                        stats["first_complete_candidate"]=snapshot
                    if stats["best_constructed_candidate"] is None or tuple(snapshot["key"])<tuple(stats["best_constructed_candidate"]["key"]):
                        stats["best_constructed_candidate"]=snapshot
                    key=v1.weighted_key(full.score,weights,scales)
                    if key<best_key:
                        _check(deadline,stats)
                        problem,best,checked,best_key=changed,candidate,full,key
                        descriptors.append(descriptor)
                        stats["mobile_meetings"]=physical
                        stats["accepted_moves"].append(dict(station_id=descriptor["station_id"],full_snapshot=snapshot))
                        stats["accepted_improvements"]+=1
                        improved=True
                        break
                if improved or stats["new_problems_built"]>=full_candidate_limit:
                    break
            if not improved or stats["new_problems_built"]>=full_candidate_limit:
                break
    except (v1._Deadline,v1._Unsupported):
        pass
    index=build_plan_index(problem,best)
    stats.update(initial_key=list(initial_key),final_key=list(best_key),
        final_cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        runtime_seconds=perf_counter()-started)
    return dict(problem=problem,routes=best,evaluation=checked,iterations=stats["specifications_examined"],metadata=stats)
