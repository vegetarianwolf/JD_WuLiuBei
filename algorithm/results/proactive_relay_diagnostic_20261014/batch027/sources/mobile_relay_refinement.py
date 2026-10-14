"""Research-only synchronized airborne rendezvous for in-flight DIRECT cargo.

This is a new model extension, not a four-fixed-ground-station experiment.
One or two parcels per aircraft exchange final-delivery responsibility at one
continuous-coordinate rendezvous. Both aircraft retain their own cargo until
both arrive; they then drop their own group and take the other group together.
Every unrelated visit stays on its original aircraft in the same order.

The production relay DAG represents the same final clocks with early-drop
timestamps. ``mobile_meetings`` records the physical synchronized drop/take
times instead: delaying all early drops to T=max(arrivals) leaves all later
delivery clocks unchanged and needs neither ground caching nor extra capacity.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from heapq import heappush, heapreplace
from itertools import combinations, permutations
from math import hypot, isclose, isfinite
from time import perf_counter

from uav_dispatch.model import Point, RelayStation, Score, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution


class _Deadline(Exception):
    pass


def weighted_key(score, weights, scales):
    physical = (score.late_count, score.total_lateness_min, score.distance_km)
    return (sum(w * value / scale for w, value, scale in zip(weights, physical, scales)), *physical)


def _check(deadline, stats):
    if perf_counter() >= deadline:
        stats["deadline_reached"] = True
        raise _Deadline


def _mid(a, b):
    return Point((a.x + b.x) / 2., (a.y + b.y) / 2.)


def _cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _intersections(a, b, c, d):
    """One intersection or a finite set spanning a collinear overlap."""
    u, v, w = (b.x-a.x, b.y-a.y), (d.x-c.x, d.y-c.y), (c.x-a.x, c.y-a.y)
    denominator = _cross(u, v)
    if abs(denominator) > 1e-10:
        t, s = _cross(w, v) / denominator, _cross(w, u) / denominator
        if -1e-10 <= t <= 1.+1e-10 and -1e-10 <= s <= 1.+1e-10:
            return (Point(a.x + max(0., min(1., t)) * u[0], a.y + max(0., min(1., t)) * u[1]),)
        return ()
    def on(p, x, y):
        return (abs(_cross((p.x-x.x, p.y-x.y), (y.x-x.x, y.y-x.y))) <= 1e-9
                and min(x.x,y.x)-1e-9 <= p.x <= max(x.x,y.x)+1e-9
                and min(x.y,y.y)-1e-9 <= p.y <= max(x.y,y.y)+1e-9)
    points = [p for p in (a,b,c,d) if on(p,a,b) and on(p,c,d)]
    if not points:
        return ()
    points.sort(key=lambda p: (p.x,p.y))
    return points[0], _mid(points[0],points[-1]), points[-1]


def _geometric_median(points):
    """Modified Weiszfeld iteration handles coincident endpoints explicitly."""
    current = Point(sum(p.x for p in points)/len(points), sum(p.y for p in points)/len(points))
    for _ in range(32):
        distant = [(p,current.distance_to(p)) for p in points if current.distance_to(p) > 1e-10]
        coincident = len(points)-len(distant)
        if not distant:
            return current
        force = hypot(sum((p.x-current.x)/r for p,r in distant),
                      sum((p.y-current.y)/r for p,r in distant))
        if coincident and force <= coincident+1e-12:
            return current
        denominator = sum(1./r for _,r in distant)
        target = Point(sum(p.x/r for p,r in distant)/denominator,
                       sum(p.y/r for p,r in distant)/denominator)
        ratio = coincident/force if coincident else 0.
        updated = Point((1.-ratio)*target.x+ratio*current.x, (1.-ratio)*target.y+ratio*current.y)
        if updated.distance_to(current) < 1e-9:
            return updated
        current = updated
    return current


def _earliest_meeting(a, b, ta, tb, speed):
    distance = a.distance_to(b)
    if distance <= 1e-12:
        return a
    fraction = max(0., min(distance, (distance + speed*(tb-ta))/2.))/distance
    return Point(a.x + fraction*(b.x-a.x), a.y + fraction*(b.y-a.y))


def _points(a, b, c, d, ta, tb, speed, limit):
    median, earliest = _geometric_median((a,b,c,d)), _earliest_meeting(a,c,ta,tb,speed)
    pool = (*_intersections(a,b,c,d), median, earliest, _mid(a,b), _mid(c,d), a,b,c,d)
    unique = {}
    for p in pool:
        unique.setdefault((round(p.x,10),round(p.y,10)),p)
    ranked = sorted(unique.values(), key=lambda p: (
        a.distance_to(p)+p.distance_to(b)+c.distance_to(p)+p.distance_to(d),
        max(ta+a.distance_to(p)/speed,tb+c.distance_to(p)/speed),p.x,p.y))
    chosen = ranked[:max(1,limit-1)]
    if earliest not in chosen:
        chosen.append(earliest)
    return tuple(chosen[:limit])


@dataclass(frozen=True, slots=True)
class _Cut:
    position: int
    active: tuple
    point: Point
    actual_time: float


@dataclass(frozen=True, slots=True)
class _Spec:
    a: int
    b: int
    left: _Cut
    right: _Cut
    group_a: tuple
    group_b: tuple
    receive_a: tuple
    receive_b: tuple
    priority: tuple


@dataclass(frozen=True,slots=True)
class _Worst:
    spec: _Spec

    def __lt__(self,other):
        return self.spec.priority>other.spec.priority


def _cuts(problem, route, route_index, schedule):
    active, cuts = [], []
    registry = problem.leg_registry or {}
    for position in range(len(route)+1):
        direct = tuple(v for v in active if v not in registry)
        # Do not split an old relay leg or insert an exchange into an old
        # contiguous drop/take group between its last drop and first pickup.
        previous = registry.get(abs(route[position-1])) if position else None
        following = registry.get(abs(route[position])) if position<len(route) else None
        inside_transfer = ((previous is not None and route[position-1]<0 and previous.kind=="RELAY_IN")
                           or (following is not None and route[position]>0 and following.kind=="RELAY_OUT"))
        if direct and len(direct)==len(active) and not inside_transfer:
            point = problem.point_for_visit(route[position-1]) if position else problem.home_points()[route_index]
            clock = schedule.event_times_min[(route_index,position-1)] if position else 0.
            cuts.append(_Cut(position,direct,point,clock))
        if position==len(route):
            break
        if route[position]>0:
            active.append(route[position])
        else:
            active.remove(-route[position])
    return tuple(cuts)


def _slot_map(route, cut, own, received):
    slots = [v for v in route[cut.position:] if v<0 and -v in own]
    return {-old: new for old,new in zip(slots,received)}


def _replacement_score(problem, route, route_index, mapping):
    """Teleport-exchange lower bound: no rendezvous or old relay waits."""
    previous, clock, distance, count, lateness = problem.home_points()[route_index],0.,0.,0,0.
    registry = problem.leg_registry or {}
    for visit in route:
        replacement = mapping.get(-visit) if visit<0 else None
        point = problem.task(replacement).delivery if replacement else problem.point_for_visit(visit)
        km = previous.distance_to(point)
        clock += km/problem.speed_km_per_min
        distance += km
        if replacement:
            task_id = replacement
        elif visit<0:
            leg=registry.get(-visit)
            task_id = -visit if leg is None else leg.task_id if leg.kind=="RELAY_OUT" else None
        else:
            task_id=None
        if task_id is not None:
            late=max(0.,clock-problem.task(task_id).deadline_min)
            count += late>1e-9
            lateness += late
        previous=point
    return Score(count,lateness,distance)


def _specifications(problem,routes,schedule,weights,scales,seed,max_pairs,deadline,stats):
    cuts=[_cuts(problem,r,i,schedule) for i,r in enumerate(routes)]
    no_wait=[_replacement_score(problem,r,i,{}) for i,r in enumerate(routes)]
    baseline=sum(no_wait,Score(0,0.,0.))
    ranked,score_cache=[],{}
    def replacement(route_index,mapping):
        cache_key=(route_index,tuple(sorted(mapping.items())))
        if cache_key not in score_cache:
            score_cache[cache_key]=_replacement_score(problem,routes[route_index],route_index,mapping)
        return score_cache[cache_key]
    for a,b in combinations(range(len(routes)),2):
        for left in cuts[a]:
            for right in cuts[b]:
                _check(deadline,stats)
                for size in (1,2):
                    if min(len(left.active),len(right.active))<size:
                        continue
                    for ga in combinations(left.active,size):
                        for gb in combinations(right.active,size):
                            for receive_a in permutations(gb):
                                for receive_b in permutations(ga):
                                    ma,mb=_slot_map(routes[a],left,ga,receive_a),_slot_map(routes[b],right,gb,receive_b)
                                    lower=(baseline-no_wait[a]-no_wait[b]
                                           +replacement(a,ma)+replacement(b,mb))
                                    tie=(a*31+b*17+left.position*13+right.position*7+seed)%104729
                                    priority=(*weighted_key(lower,weights,scales),abs(left.actual_time-right.actual_time),tie,
                                              a,b,left.position,right.position,ga,gb,receive_a,receive_b)
                                    spec=_Spec(a,b,left,right,ga,gb,receive_a,receive_b,priority)
                                    if len(ranked)<max_pairs:
                                        heappush(ranked,_Worst(spec))
                                    elif priority<ranked[0].spec.priority:
                                        heapreplace(ranked,_Worst(spec))
                                    stats["specifications_generated"]+=1
    return tuple(item.spec for item in sorted(ranked,key=lambda item:item.spec.priority))


def _prototype(problem,routes,spec,point):
    station=max((s.id for s in problem.relay_stations),default=0)+1
    next_leg=max((*problem.task_ids,*(problem.leg_registry or {})),default=0)+1
    new_legs,by_task={},{}
    for task_id in spec.group_a+spec.group_b:
        by_task[task_id]=(next_leg,next_leg+1)
        new_legs[next_leg]=TransportLeg(next_leg,task_id,"RELAY_IN",station)
        new_legs[next_leg+1]=TransportLeg(next_leg+1,task_id,"RELAY_OUT",station)
        next_leg+=2
    changed=list(routes)
    for aircraft,cut,own,received in ((spec.a,spec.left,spec.group_a,spec.receive_a),
                                       (spec.b,spec.right,spec.group_b,spec.receive_b)):
        route=routes[aircraft]
        mapping=_slot_map(route,cut,own,received)
        prefix=tuple(by_task[v][0] if v>0 and v in own else v for v in route[:cut.position])
        suffix=tuple(-by_task[mapping[-v]][1] if v<0 and -v in mapping else v for v in route[cut.position:])
        drops=tuple(-by_task[t][0] for t in own)
        takes=tuple(by_task[t][1] for t in received)
        changed[aircraft]=prefix+drops+takes+suffix
    descriptor=dict(station_id=station,point=[point.x,point.y],aircraft=[spec.a,spec.b],
                    own_task_groups=[list(spec.group_a),list(spec.group_b)],group_size=len(spec.group_a),
                    original_cut_positions=[spec.left.position,spec.right.position])
    return tuple(changed),new_legs,descriptor


def _event(problem,visit,new_legs,point):
    leg=new_legs.get(abs(visit))
    if leg is not None:
        position=(problem.task(leg.task_id).pickup if leg.kind=="RELAY_IN" and visit>0
                  else problem.task(leg.task_id).delivery if leg.kind=="RELAY_OUT" and visit<0 else point)
    else:
        position=problem.point_for_visit(visit)
        leg=(problem.leg_registry or {}).get(abs(visit))
    final_task=(-visit if leg is None else leg.task_id if leg.kind=="RELAY_OUT" else None) if visit<0 else None
    return position,final_task


def _fast_lower_score(problem,routes,new_legs,point,descriptor):
    """Exact new synchronization, while deleting all old wait constraints.

    Removing old handoff edges gives a true max-plus clock lower bound. For a
    DIRECT input this is the exact complete candidate score, not a distance-only
    screen. No hypothetical score is accepted as a final checked solution.
    """
    speed=problem.speed_km_per_min
    arrivals=[]
    for aircraft in descriptor["aircraft"]:
        previous,clock=problem.home_points()[aircraft],0.
        for visit in routes[aircraft]:
            p,_=_event(problem,visit,new_legs,point)
            clock+=previous.distance_to(p)/speed
            if visit<0 and abs(visit) in new_legs and new_legs[-visit].kind=="RELAY_IN":
                break
            previous=p
        arrivals.append(clock)
    sync=max(arrivals)
    distance,lateness,count=0.,0.,0
    for aircraft,route in enumerate(routes):
        previous,clock=problem.home_points()[aircraft],0.
        for visit in route:
            p,task_id=_event(problem,visit,new_legs,point)
            km=previous.distance_to(p)
            distance+=km
            clock+=km/speed
            if abs(visit) in new_legs and ((visit<0 and new_legs[-visit].kind=="RELAY_IN")
                                         or (visit>0 and new_legs[visit].kind=="RELAY_OUT")):
                clock=max(clock,sync)
            if task_id is not None:
                late=max(0.,clock-problem.task(task_id).deadline_min)
                count+=late>1e-9
                lateness+=late
            previous=p
    return Score(count,lateness,distance)


def _problem(problem,new_legs,descriptor):
    registry={**(problem.leg_registry or {}),**new_legs}
    candidates={t:tuple(ids) for t,ids in (problem.task_relay_candidates or {}).items()}
    for task_id in descriptor["own_task_groups"][0]+descriptor["own_task_groups"][1]:
        candidates[task_id]=(*candidates.get(task_id,()),descriptor["station_id"])
    return replace(problem,relay_stations=(*problem.relay_stations,RelayStation(
        descriptor["station_id"],*descriptor["point"])),leg_registry=registry,task_relay_candidates=candidates)


def topology(problem):
    return dict(stations=[[s.id,s.x,s.y] for s in problem.relay_stations],
        legs=[dict(id=l.id,task_id=l.task_id,kind=l.kind,relay_id=l.relay_id)
              for _,l in sorted((problem.leg_registry or {}).items())],
        task_relay_candidates={str(t):list(ids) for t,ids in (problem.task_relay_candidates or {}).items()},
        home_points=[[p.x,p.y] for p in problem.home_points()],drone_count=problem.drone_count,
        max_tasks_per_drone=problem.max_tasks_per_drone,capacity=problem.capacity,
        speed_km_per_min=problem.speed_km_per_min,relay_allowed=True)


def _physical_meetings(problem,routes,descriptors,schedule):
    result=[]
    for descriptor in descriptors:
        station=descriptor["station_id"]
        drops,takes,arrivals=[],[],[]
        for aircraft,own in zip(descriptor["aircraft"],descriptor["own_task_groups"]):
            drop_positions=[pos for pos,v in enumerate(routes[aircraft]) if v<0
                and (leg:=(problem.leg_registry or {}).get(-v)) is not None
                and leg.relay_id==station and leg.kind=="RELAY_IN"]
            take_positions=[pos for pos,v in enumerate(routes[aircraft]) if v>0
                and (leg:=(problem.leg_registry or {}).get(v)) is not None
                and leg.relay_id==station and leg.kind=="RELAY_OUT"]
            block=drop_positions+take_positions
            if block!=list(range(block[0],block[0]+2*len(own))):
                raise AssertionError("mobile exchange is not contiguous drop-own then take-other")
            previous=block[0]-1
            clock=(schedule.event_times_min[(aircraft,previous)] if previous>=0 else 0.)
            arrival=clock+problem.distance(routes[aircraft][previous] if previous>=0 else None,
                routes[aircraft][block[0]],from_node=problem.home_node(aircraft) if previous<0 else None)/problem.speed_km_per_min
            arrivals.append(arrival)
            drops.extend(dict(route_index=aircraft,position=p,visit=routes[aircraft][p]) for p in drop_positions)
            takes.extend(dict(route_index=aircraft,position=p,visit=routes[aircraft][p]) for p in take_positions)
        synchronization=max(arrivals)
        if any(not isclose(schedule.event_times_min[(e["route_index"],e["position"])],synchronization,
                           abs_tol=1e-8,rel_tol=1e-10) for e in takes):
            raise AssertionError("mobile OUT pickups are not synchronized to max arrival")
        for event in drops+takes:
            event["physical_time_min"]=synchronization
        result.append({**descriptor, "cut_positions":[min(e["position"] for e in drops if e["route_index"]==a)
                                                       for a in descriptor["aircraft"]],
                       "arrival_times_min":arrivals,"synchronization_min":synchronization,
                       "aircraft_waiting_min":[synchronization-t for t in arrivals],
                       "drop_events":drops,"pickup_events":takes,
                       "physical_model":"both_aircraft_hold_own_cargo_until_synchronous_exchange"})
    return result


def _snapshot(problem,routes,checked,weights,scales,meetings):
    index=build_plan_index(problem,routes)
    return dict(routes=[list(r) for r in routes],topology=topology(problem),
        score=dict(late_count=checked.score.late_count,total_lateness_min=checked.score.total_lateness_min,
                   distance_km=checked.score.distance_km),key=list(weighted_key(checked.score,weights,scales)),
        valid=True,complete=True,mobile_meetings=meetings,
        cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        relay_task_count=len(index.relay_task_ids))


def refine(problem,routes,*,deadline,weights,scales,seed=2026101493,
           max_pairs=128,point_limit=6,max_moves=3,full_candidate_limit=12):
    """Return the checked incumbent and its possibly extended ``problem``.

    Only absolute optimistic weighted bounds prune full evaluations. Candidate
    points use replaced delivery-slot edges, rather than the old destinations.
    Every adopted change is fully checked before the absolute time deadline.
    New coordinates/legs belong only to actually adopted rendezvous episodes.
    """
    weights,scales=tuple(weights),tuple(scales)
    if (len(weights)!=3 or len(scales)!=3 or any(not isfinite(x) or x<=0 for x in (*weights,*scales))
        or not isclose(sum(weights),1.) or not isfinite(deadline)):
        raise ValueError("finite positive normalized weights/scales and deadline required")
    if any(not isinstance(x,int) or isinstance(x,bool) or x<1 for x in (max_pairs,point_limit,max_moves,full_candidate_limit)):
        raise ValueError("positive integer shortlist bounds required")
    started=perf_counter()
    best=tuple(tuple(r) for r in routes)
    if len(best)>problem.drone_count:
        raise ValueError("too many routes")
    best+=((),)*(problem.drone_count-len(best))
    checked=evaluate_solution(problem,best,_force_full=True)
    if not checked.valid:
        raise ValueError(f"invalid initial solution: {checked.violations}")
    initial_key=best_key=weighted_key(checked.score,weights,scales)
    stats=dict(method="Synchronous-Mobile-InFlight-Relay",model_extension="continuous_airborne_rendezvous",
        seed=seed,specifications_generated=0,specifications_examined=0,points_examined=0,
        optimistic_bounds_pruned=0,new_problems_built=0,global_evaluations=0,full_evaluations=1,
        accepted_improvements=0,rejected_cycles=0,rejected_invalid=0,deadline_reached=False,
        unsupported_non_euclidean_matrix=problem.distance_matrix_km is not None,
        mobile_meetings=[],first_complete_candidate=None,best_constructed_candidate=None,
        full_candidate_snapshots=[],accepted_moves=[],shortlist_coverage=[],
        input_snapshot=_snapshot(problem,best,checked,weights,scales,[]),
        configuration=dict(max_pairs=max_pairs,point_limit=point_limit,
            max_moves=max_moves,full_candidate_limit=full_candidate_limit))
    descriptors=[]
    try:
        _check(deadline,stats)
        if problem.distance_matrix_km is not None:
            raise _Unsupported
        for _ in range(max_moves):
            _check(deadline,stats)
            schedule=GlobalRelayEvaluator(problem).evaluate(best)
            specs=_specifications(problem,best,schedule,weights,scales,seed,max_pairs,deadline,stats)
            stats["shortlist_coverage"].append(dict(specification_count=len(specs),
                distinct_task_group_pairs=len({(s.a,s.b,tuple(sorted(s.group_a)),tuple(sorted(s.group_b))) for s in specs}),
                distinct_route_pairs=len({(s.a,s.b) for s in specs}),
                group_sizes=sorted({len(s.group_a) for s in specs})))
            improved=False
            for spec in specs:
                _check(deadline,stats)
                stats["specifications_examined"]+=1
                ma,mb=_slot_map(best[spec.a],spec.left,spec.group_a,spec.receive_a),_slot_map(best[spec.b],spec.right,spec.group_b,spec.receive_b)
                va=(problem.task(ma[-best[spec.a][spec.left.position]]).delivery
                    if best[spec.a][spec.left.position]<0 and -best[spec.a][spec.left.position] in ma
                    else problem.point_for_visit(best[spec.a][spec.left.position]))
                vb=(problem.task(mb[-best[spec.b][spec.right.position]]).delivery
                    if best[spec.b][spec.right.position]<0 and -best[spec.b][spec.right.position] in mb
                    else problem.point_for_visit(best[spec.b][spec.right.position]))
                points=_points(spec.left.point,va,spec.right.point,vb,spec.left.actual_time,spec.right.actual_time,
                               problem.speed_km_per_min,point_limit)
                for point in points:
                    _check(deadline,stats)
                    candidate,new_legs,descriptor=_prototype(problem,best,spec,point)
                    stats["points_examined"]+=1
                    lower=_fast_lower_score(problem,candidate,new_legs,point,descriptor)
                    _check(deadline,stats)
                    if weighted_key(lower,weights,scales)[0]>best_key[0]+1e-12:
                        stats["optimistic_bounds_pruned"]+=1
                        continue
                    if stats["new_problems_built"]>=full_candidate_limit:
                        break
                    changed_problem=_problem(problem,new_legs,descriptor)
                    stats["new_problems_built"]+=1
                    _check(deadline,stats)
                    global_score=GlobalRelayEvaluator(changed_problem).evaluate(candidate)
                    stats["global_evaluations"]+=1
                    _check(deadline,stats)
                    if not global_score.feasible:
                        stats["rejected_cycles"]+=1
                        continue
                    evaluated=evaluate_solution(changed_problem,candidate,_force_full=True)
                    stats["full_evaluations"]+=1
                    _check(deadline,stats)
                    if not evaluated.valid:
                        stats["rejected_invalid"]+=1
                        continue
                    physical=_physical_meetings(changed_problem,candidate,descriptors+[descriptor],global_score)
                    snapshot=_snapshot(changed_problem,candidate,evaluated,weights,scales,physical)
                    _check(deadline,stats)
                    stats["full_candidate_snapshots"].append(snapshot)
                    if stats["first_complete_candidate"] is None:
                        stats["first_complete_candidate"]=snapshot
                    if stats["best_constructed_candidate"] is None or tuple(snapshot["key"])<tuple(stats["best_constructed_candidate"]["key"]):
                        stats["best_constructed_candidate"]=snapshot
                    key=weighted_key(evaluated.score,weights,scales)
                    if key<best_key:
                        _check(deadline,stats)
                        problem,best,checked,best_key=changed_problem,candidate,evaluated,key
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
    except (_Deadline,_Unsupported):
        pass
    index=build_plan_index(problem,best)
    stats.update(initial_key=list(initial_key),final_key=list(best_key),
        final_cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        runtime_seconds=perf_counter()-started)
    return dict(problem=problem,routes=best,evaluation=checked,iterations=stats["specifications_examined"],metadata=stats)


class _Unsupported(Exception):
    pass
