"""Research-only mobile exchange with selected delivery-position rebuilding.

Matched DIRECT control carries its original parcels and rebuilds exactly the
same number of final-delivery events. RELAY instead exchanges equal in-flight
groups at one synchronized continuous-coordinate meeting. All unrelated events
remain on their original aircraft in their original relative order.

Removing selected delivery events leaves unfinished routes that can exceed
capacity. Such routes are never sent to a scheduler or solution validator.
Integer capacity windows insert the pending deliveries before the next pickup
that would overflow capacity; only complete routes enter score comparisons.
"""
from __future__ import annotations

from itertools import permutations, product
from math import isclose, isfinite
from time import perf_counter

from uav_dispatch.model import Point, Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import mobile_relay_refinement as v1
except ImportError:
    from . import mobile_relay_refinement as v1


def _check(deadline,stats):
    if perf_counter()>=deadline:
        stats["deadline_reached"]=True
        raise v1._Deadline


def _task(problem,visit,new_legs):
    leg=new_legs.get(abs(visit)) or (problem.leg_registry or {}).get(abs(visit))
    return leg.task_id if leg else abs(visit)


def _without_selected(problem,routes,selected,new_legs):
    selected=set(selected)
    return tuple(tuple(v for v in route if _task(problem,v,new_legs) not in selected) for route in routes)


def _remove_deliveries(route,deliveries):
    """Return an unfinished event sequence plus original insertion anchors."""
    removed=set(deliveries)
    result,anchors=[],{}
    for visit in route:
        if visit in removed:
            anchors[visit]=len(result)
        else:
            result.append(visit)
    return tuple(result),anchors


def _capacity(route,capacity):
    load=0
    for visit in route:
        load+=1 if visit>0 else -1
        if not 0<=load<=capacity:
            return False
    return load==0


def _next_overflow(route,start,capacity):
    """An unresolved delivery must be inserted no later than this event gap."""
    load=0
    for position,visit in enumerate(route):
        load+=1 if visit>0 else -1
        if load<0:
            raise AssertionError("an unfinished delivery route has negative cargo")
        if load>capacity:
            return position if position>=start else -1
    return len(route)


def _clock_prefix(problem,route,aircraft,new_legs,point,synchronization):
    previous,clock=problem.home_points()[aircraft],0.
    times=[0.]
    for visit in route:
        p,_=v1._event(problem,visit,new_legs,point)
        clock+=previous.distance_to(p)/problem.speed_km_per_min
        leg=new_legs.get(abs(visit))
        if synchronization is not None and leg is not None and ((visit<0 and leg.kind=="RELAY_IN")
                                                                or (visit>0 and leg.kind=="RELAY_OUT")):
            clock=max(clock,synchronization)
        times.append(clock)
        previous=p
    return times


def _position_options(problem,route,aircraft,delivery,start,latest,new_legs,point,sync,
                      anchor,weights,scales,limit):
    if latest<start:
        return ()
    clocks=_clock_prefix(problem,route,aircraft,new_legs,point,sync)
    destination,task_id=v1._event(problem,delivery,new_legs,point)
    ranked=[]
    for position in range(start,latest+1):
        previous=(v1._event(problem,route[position-1],new_legs,point)[0] if position else problem.home_points()[aircraft])
        delta=previous.distance_to(destination)
        if position<len(route):
            following=v1._event(problem,route[position],new_legs,point)[0]
            delta+=destination.distance_to(following)-previous.distance_to(following)
        delivered=clocks[position]+previous.distance_to(destination)/problem.speed_km_per_min
        late=max(0.,delivered-problem.task(task_id).deadline_min)
        ranking=(weights[0]*int(late>1e-9)/scales[0]+weights[1]*late/scales[1]+weights[2]*delta/scales[2],
                 late,delta,position)
        ranked.append((ranking,position))
    ranked.sort()
    mandatory=(start,latest,max(start,min(latest,anchor)))
    chosen=list(dict.fromkeys(mandatory))[:limit]
    chosen.extend(position for _,position in ranked if position not in chosen)
    return tuple(chosen[:limit])


def _route_score(problem,route,aircraft,new_legs,point,sync):
    """Complete route score with actual new sync and relaxed old handoff waits."""
    previous,clock=problem.home_points()[aircraft],0.
    distance,lateness,count=0.,0.,0
    for visit in route:
        p,task_id=v1._event(problem,visit,new_legs,point)
        km=previous.distance_to(p)
        distance+=km
        clock+=km/problem.speed_km_per_min
        leg=new_legs.get(abs(visit))
        if sync is not None and leg is not None and ((visit<0 and leg.kind=="RELAY_IN")
                                                    or (visit>0 and leg.kind=="RELAY_OUT")):
            clock=max(clock,sync)
        if task_id is not None:
            late=max(0.,clock-problem.task(task_id).deadline_min)
            count+=late>1e-9
            lateness+=late
        previous=p
    return Score(count,lateness,distance)


def _layouts(problem,unfinished,aircraft,deliveries,start,anchors,new_legs,point,sync,
             weights,scales,position_limit,layout_limit,deadline,stats):
    """Enumerate g<=2 complete layouts using valid-prefix capacity windows.

    Before inserting the next pending D, positions must precede the first raw
    overflow. Earlier completed prefixes stay feasible. Remaining overload in
    the suffix is allowed only while another selected D remains unresolved.
    After the last insertion the entire load scan must be valid and end at 0.
    """
    complete={}
    def recurse(route,order,depth,minimum):
        _check(deadline,stats)
        if depth==len(order):
            if not _capacity(route,problem.capacity):
                stats["rejected_capacity_layouts"]+=1
                return
            key=v1.weighted_key(_route_score(problem,route,aircraft,new_legs,point,sync),weights,scales)
            complete.setdefault(route,key)
            stats["complete_route_layouts"]+=1
            return
        delivery=order[depth]
        latest=_next_overflow(route,minimum,problem.capacity)
        if latest<minimum:
            stats["rejected_capacity_windows"]+=1
            return
        options=_position_options(problem,route,aircraft,delivery,minimum,latest,new_legs,point,sync,
            anchors.get(delivery,minimum)+depth,weights,scales,position_limit)
        for position in options:
            _check(deadline,stats)
            stats["delivery_position_probes"]+=1
            changed=route[:position]+(delivery,)+route[position:]
            recurse(changed,order,depth+1,position+1)
    for order in permutations(deliveries):
        recurse(unfinished,order,0,start)
    ranked=sorted(complete,key=lambda r:(complete[r],r))
    return tuple(ranked[:layout_limit])


def _synchronization(problem,routes,new_legs,point,descriptor):
    arrivals=[]
    for aircraft in descriptor["aircraft"]:
        previous,clock=problem.home_points()[aircraft],0.
        for visit in routes[aircraft]:
            p,_=v1._event(problem,visit,new_legs,point)
            clock+=previous.distance_to(p)/problem.speed_km_per_min
            if visit<0 and abs(visit) in new_legs and new_legs[-visit].kind=="RELAY_IN":
                break
            previous=p
        arrivals.append(clock)
    return max(arrivals)


def _template(problem,base,spec,point,relay_enabled):
    if relay_enabled:
        proto,legs,descriptor=v1._prototype(problem,base,spec,point)
        registry=legs
    else:
        proto,legs,descriptor=base,{},None
        registry={}
    unfinished=list(proto)
    records=[]
    for aircraft,cut,own,incoming in ((spec.a,spec.left,spec.group_a,spec.receive_a),
                                       (spec.b,spec.right,spec.group_b,spec.receive_b)):
        if relay_enabled:
            selected=set(incoming)
            deliveries=tuple(v for v in proto[aircraft] if v<0 and abs(v) in registry
                and registry[-v].kind=="RELAY_OUT" and registry[-v].task_id in selected)
            start=cut.position+2*len(own)
        else:
            deliveries=tuple(-t for t in own)
            start=cut.position
        unfinished[aircraft],anchors=_remove_deliveries(proto[aircraft],deliveries)
        records.append((aircraft,deliveries,start,anchors))
    sync=_synchronization(problem,proto,legs,point,descriptor) if relay_enabled else None
    return tuple(unfinished),legs,descriptor,sync,tuple(records)


def _combined_score(problem,routes,new_legs,point,descriptor):
    if descriptor is not None:
        return v1._fast_lower_score(problem,routes,new_legs,point,descriptor)
    return sum((_route_score(problem,r,i,{},None,None) for i,r in enumerate(routes)),Score(0,0.,0.))


def _rebuild(problem,base,spec,point,relay_enabled,weights,scales,limits,deadline,stats):
    unfinished,legs,descriptor,sync,records=_template(problem,base,spec,point,relay_enabled)
    choices=[]
    for aircraft,deliveries,start,anchors in records:
        layouts=_layouts(problem,unfinished[aircraft],aircraft,deliveries,start,anchors,legs,point,sync,
            weights,scales,limits["position_limit"],limits["layout_limit"],deadline,stats)
        choices.append(layouts)
    combined=[]
    for left,right in product(*choices):
        _check(deadline,stats)
        candidate=list(unfinished)
        candidate[spec.a],candidate[spec.b]=left,right
        candidate=tuple(candidate)
        if not _capacity(left,problem.capacity) or not _capacity(right,problem.capacity):
            raise AssertionError("unfinished capacity-violating route reached complete comparison")
        selected=set(spec.group_a+spec.group_b)
        if _without_selected(problem,base,selected,{})!=_without_selected(problem,candidate,selected,legs):
            raise AssertionError("rebuilding changed an unrelated event's aircraft/order")
        score=_combined_score(problem,candidate,legs,point,descriptor)
        stats["complete_joint_candidates"]+=1
        combined.append((v1.weighted_key(score,weights,scales),candidate,legs,descriptor))
    combined.sort(key=lambda r:(r[0],r[1]))
    return tuple(combined[:limits["joint_limit"]])


def _new_terminal_points(problem,routes,new_legs,point,descriptor):
    """Read genuine successors after the complete take block, never placeholders."""
    ends=[]
    for aircraft in descriptor["aircraft"]:
        route=routes[aircraft]
        drops=[p for p,v in enumerate(route) if v<0 and abs(v) in new_legs and new_legs[-v].kind=="RELAY_IN"]
        takes=[p for p,v in enumerate(route) if v>0 and abs(v) in new_legs and new_legs[v].kind=="RELAY_OUT"]
        first=min(drops)
        predecessor=v1._event(problem,route[first-1],new_legs,point)[0] if first else problem.home_points()[aircraft]
        next_position=max(takes)+1
        if next_position>=len(route):
            raise AssertionError("a newly taken mobile parcel has no delivery successor")
        successor=v1._event(problem,route[next_position],new_legs,point)[0]
        prefix_clock=_clock_prefix(problem,route[:first],aircraft,new_legs,point,None)[-1]
        ends.append((predecessor,successor,prefix_clock))
    return ends


def _update_geometry(problem,routes,legs,descriptor,point,weights,scales,point_limit,deadline,stats):
    terminals=_new_terminal_points(problem,routes,legs,point,descriptor)
    points=v1._points(terminals[0][0],terminals[0][1],terminals[1][0],terminals[1][1],
        terminals[0][2],terminals[1][2],problem.speed_km_per_min,point_limit)
    candidates=[]
    for new_point in (point,*points):
        _check(deadline,stats)
        changed_descriptor={**descriptor,"point":[new_point.x,new_point.y]}
        score=v1._fast_lower_score(problem,routes,legs,new_point,changed_descriptor)
        stats["coordinate_update_probes"]+=1
        candidates.append((v1.weighted_key(score,weights,scales),new_point,changed_descriptor))
    return min(candidates,key=lambda r:(r[0],r[1].x,r[1].y))


def _checked_candidate(problem,routes,legs,descriptor,weights,scales,deadline,stats):
    _check(deadline,stats)
    if stats["global_evaluations"]>=stats["configuration"]["full_candidate_limit"]:
        return None
    changed=v1._problem(problem,legs,descriptor) if descriptor is not None else problem
    if descriptor is not None:
        stats["new_problems_built"]+=1
    _check(deadline,stats)
    scheduled=GlobalRelayEvaluator(changed).evaluate(routes)
    stats["global_evaluations"]+=1
    _check(deadline,stats)
    if not scheduled.feasible:
        stats["rejected_cycles"]+=1
        return None
    checked=evaluate_solution(changed,routes,_force_full=True)
    stats["full_evaluations"]+=1
    _check(deadline,stats)
    if not checked.valid:
        stats["rejected_invalid_complete"]+=1
        return None
    physical=v1._physical_meetings(changed,routes,[descriptor],scheduled) if descriptor is not None else []
    snapshot=v1._snapshot(changed,routes,checked,weights,scales,physical)
    _check(deadline,stats)
    stats["full_candidate_snapshots"].append(snapshot)
    return changed,checked,snapshot


def refine(problem,routes,*,deadline,weights,scales,seed=2026101498,relay_enabled=True,
           max_specs=256,max_groups=24,point_limit=6,position_limit=10,layout_limit=3,
           joint_limit=4,full_candidates_per_group=2,full_candidate_limit=24,
           proposal_margin=.0075,polish=True):
    """Rebuild only selected deliveries, then one geometry update and polish.

    Matching DIRECT uses the same selected cargo groups, capacity-window
    position/layout/joint bounds, margin and final delivery-polish pass. All
    alternatives start from the same input; only the best checked complete key
    wins. DIRECT returns no ``problem`` key. RELAY returns only the topology of
    the winning candidate, or the unchanged input if nothing improved.
    """
    weights,scales=tuple(weights),tuple(scales)
    bounds=(max_specs,max_groups,point_limit,position_limit,layout_limit,joint_limit,
            full_candidates_per_group,full_candidate_limit)
    if (len(weights)!=3 or len(scales)!=3 or any(not isfinite(x) or x<=0 for x in (*weights,*scales))
            or not isclose(sum(weights),1.) or not isfinite(deadline)
            or any(not isinstance(x,int) or isinstance(x,bool) or x<1 for x in bounds)
            or not isinstance(relay_enabled,bool) or not isinstance(polish,bool)
            or not isfinite(proposal_margin) or proposal_margin<0):
        raise ValueError("finite normalized objective/deadline and positive bounds/boolean modes required")
    started=perf_counter()
    base=tuple(tuple(r) for r in routes)
    if len(base)>problem.drone_count:
        raise ValueError("too many routes")
    base+=((),)*(problem.drone_count-len(base))
    checked=evaluate_solution(problem,base,_force_full=True)
    if not checked.valid:
        raise ValueError(f"invalid initial solution: {checked.violations}")
    best,best_problem,best_checked=base,problem,checked
    initial_key=best_key=v1.weighted_key(checked.score,weights,scales)
    limits=dict(position_limit=position_limit,layout_limit=layout_limit,joint_limit=joint_limit)
    stats=dict(method="Mobile-Selected-Delivery-Reconstruction",relay_enabled=relay_enabled,seed=seed,
        model_extension="continuous_airborne_rendezvous" if relay_enabled else "direct_delivery_reordering",
        specifications_generated=0,specifications_examined=0,groups_examined=0,
        delivery_position_probes=0,complete_route_layouts=0,complete_joint_candidates=0,
        rejected_capacity_layouts=0,rejected_capacity_windows=0,coordinate_update_probes=0,
        global_evaluations=0,full_evaluations=1,new_problems_built=0,accepted_improvements=0,
        rejected_cycles=0,rejected_invalid_complete=0,rejected_over_margin=0,deadline_reached=False,
        unsupported_non_euclidean_matrix=problem.distance_matrix_km is not None,
        first_complete_candidate=None,best_constructed_candidate=None,full_candidate_snapshots=[],
        accepted_moves=[],mobile_meetings=[],group_records=[],
        input_snapshot=v1._snapshot(problem,base,checked,weights,scales,[]),
        configuration=dict(max_specs=max_specs,max_groups=max_groups,point_limit=point_limit,
            **limits,full_candidates_per_group=full_candidates_per_group,full_candidate_limit=full_candidate_limit,
            proposal_margin=proposal_margin,polish=polish))
    try:
        _check(deadline,stats)
        if problem.distance_matrix_km is not None:
            raise v1._Unsupported
        scheduled=GlobalRelayEvaluator(problem).evaluate(base)
        specs=v1._specifications(problem,base,scheduled,weights,scales,seed,max_specs,deadline,stats)
        seen=set()
        for spec in specs:
            _check(deadline,stats)
            group_key=(spec.a,spec.b,spec.left.position,spec.right.position,spec.group_a,spec.group_b)
            if group_key in seen:
                continue
            seen.add(group_key)
            if stats["groups_examined"]>=max_groups:
                break
            stats["groups_examined"]+=1
            stats["specifications_examined"]+=1
            record=dict(aircraft=[spec.a,spec.b],own_task_groups=[list(spec.group_a),list(spec.group_b)],
                        cut_positions=[spec.left.position,spec.right.position],proposals=[])
            stats["group_records"].append(record)
            if relay_enabled:
                ma=v1._slot_map(base[spec.a],spec.left,spec.group_a,spec.receive_a)
                mb=v1._slot_map(base[spec.b],spec.right,spec.group_b,spec.receive_b)
                def successor(aircraft,cut,mapping):
                    v=base[aircraft][cut.position]
                    return problem.task(mapping[-v]).delivery if v<0 and -v in mapping else problem.point_for_visit(v)
                points=v1._points(spec.left.point,successor(spec.a,spec.left,ma),spec.right.point,
                    successor(spec.b,spec.right,mb),spec.left.actual_time,spec.right.actual_time,
                    problem.speed_km_per_min,point_limit)
            else:
                points=(None,)
            pool=[]
            for point in points:
                _check(deadline,stats)
                pool.extend(_rebuild(problem,base,spec,point,relay_enabled,weights,scales,limits,deadline,stats))
            pool.sort(key=lambda r:(r[0],r[1]))
            unique=[]
            routes_seen=set()
            for candidate in pool:
                point=tuple(candidate[3]["point"]) if candidate[3] is not None else None
                marker=(candidate[1],point)
                if marker not in routes_seen:
                    unique.append(candidate)
                    routes_seen.add(marker)
            for lower_key,candidate,legs,descriptor in unique[:full_candidates_per_group]:
                _check(deadline,stats)
                if stats["global_evaluations"]>=full_candidate_limit:
                    break
                if lower_key[0]>initial_key[0]+proposal_margin+1e-12:
                    stats["rejected_over_margin"]+=1
                    continue
                result=_checked_candidate(problem,candidate,legs,descriptor,weights,scales,deadline,stats)
                if result is None:
                    continue
                current_problem,current_checked,snapshot=result
                if stats["first_complete_candidate"] is None:
                    stats["first_complete_candidate"]=snapshot
                if stats["best_constructed_candidate"] is None or tuple(snapshot["key"])<tuple(stats["best_constructed_candidate"]["key"]):
                    stats["best_constructed_candidate"]=snapshot
                proposal=dict(full_proposal=snapshot,post_geometry=None,post_polish=None,adopted=False)
                record["proposals"].append(proposal)
                current_routes,current_descriptor=candidate,descriptor
                current_key=v1.weighted_key(current_checked.score,weights,scales)
                if current_key[0]>initial_key[0]+proposal_margin+1e-12:
                    stats["rejected_over_margin"]+=1
                    continue
                if relay_enabled and stats["global_evaluations"]<full_candidate_limit:
                    point=Point(*descriptor["point"])
                    _,new_point,new_descriptor=_update_geometry(problem,current_routes,legs,descriptor,point,
                        weights,scales,point_limit,deadline,stats)
                    if new_point!=point:
                        revised=_checked_candidate(problem,current_routes,legs,new_descriptor,weights,scales,deadline,stats)
                        if revised is not None and v1.weighted_key(revised[1].score,weights,scales)<current_key:
                            current_problem,current_checked,snapshot=revised
                            current_descriptor=new_descriptor
                            current_key=v1.weighted_key(current_checked.score,weights,scales)
                    proposal["post_geometry"]=v1._snapshot(current_problem,current_routes,current_checked,weights,scales,
                        v1._physical_meetings(current_problem,current_routes,[current_descriptor],
                            GlobalRelayEvaluator(current_problem).evaluate(current_routes)))
                if polish and stats["global_evaluations"]<full_candidate_limit:
                    point=Point(*current_descriptor["point"]) if current_descriptor else None
                    polished=_rebuild(problem,base,spec,point,relay_enabled,weights,scales,limits,deadline,stats)
                    for _,new_routes,new_legs,new_descriptor in polished[:1]:
                        if new_routes==current_routes:
                            continue
                        revised=_checked_candidate(problem,new_routes,new_legs,new_descriptor,weights,scales,deadline,stats)
                        if revised is not None and v1.weighted_key(revised[1].score,weights,scales)<current_key:
                            current_problem,current_checked,snapshot=revised
                            current_routes,current_descriptor=new_routes,new_descriptor
                            legs=new_legs
                            current_key=v1.weighted_key(current_checked.score,weights,scales)
                meetings=(v1._physical_meetings(current_problem,current_routes,[current_descriptor],
                    GlobalRelayEvaluator(current_problem).evaluate(current_routes)) if current_descriptor else [])
                final_snapshot=v1._snapshot(current_problem,current_routes,current_checked,weights,scales,meetings)
                proposal["post_polish"]=final_snapshot
                _check(deadline,stats)
                if current_key<best_key:
                    best,best_problem,best_checked,best_key=current_routes,current_problem,current_checked,current_key
                    stats["mobile_meetings"]=meetings
                    stats["accepted_improvements"]+=1
                    proposal["adopted"]=True
                    stats["accepted_moves"].append(dict(full_snapshot=final_snapshot))
            if stats["global_evaluations"]>=full_candidate_limit:
                break
    except (v1._Deadline,v1._Unsupported):
        pass
    index=build_plan_index(best_problem,best)
    stats.update(initial_key=list(initial_key),final_key=list(best_key),
        final_cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        runtime_seconds=perf_counter()-started)
    result=dict(routes=best,evaluation=best_checked,iterations=stats["groups_examined"],metadata=stats)
    if relay_enabled:
        result["problem"]=best_problem
    return result
