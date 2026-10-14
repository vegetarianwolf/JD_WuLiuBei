"""Bounded three-aircraft, four-task, fixed-station repooling experiment.

Two destination-neighbouring requests i/j, originally on distinct A/B,
become the final responsibility of a third C. Two complete C requests
k1/k2 compensate A/B, preserving every aircraft's final delivery count.
The relay arm MUST collect i on A and j on B, then pool their two OUT
legs at one existing station on C; it never borrows a DIRECT candidate.
The matched DIRECT arm rebuilds the same final map using whole requests.

All partial global scores cover the identical task set within a pipeline.
IN/OUT plans are submitted atomically, never as half-relayed tasks. Local
geometry and local insertion deltas only rank finite proposals; none is
an absolute global objective bound. Only a complete full check and a
strict weighted-key improvement can replace the original incumbent.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from itertools import permutations
import json
from math import isclose,isfinite
from time import perf_counter

from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.search import RouteEvaluator,route_insertion_options,route_leg_insertion_options
from uav_dispatch.validation import evaluate_solution


class _Deadline(Exception):
    pass


def _check(deadline,stats):
    if perf_counter()>=deadline:
        stats["deadline_reached"]=True
        raise _Deadline


def weighted_key(score,weights,scales):
    physical=(score.late_count,score.total_lateness_min,score.distance_km)
    return (sum(w*x/s for w,x,s in zip(weights,physical,scales)),*physical)


def _task(problem,visit):
    leg=(problem.leg_registry or {}).get(abs(visit))
    return leg.task_id if leg else abs(visit)


def _remove(problem,routes,selected):
    selected=set(selected)
    return tuple(tuple(v for v in r if _task(problem,v) not in selected) for r in routes)


def _legs(problem):
    result={}
    for task_id,legs in problem._legs_by_task.items():
        stations={}
        for leg in legs:
            stations.setdefault(leg.relay_id,{})[leg.kind]=leg.id
        result[task_id]={h:(p["RELAY_IN"],p["RELAY_OUT"]) for h,p in stations.items()
                         if set(p)=={"RELAY_IN","RELAY_OUT"}}
    return result


def _distance(problem,route,aircraft):
    previous=problem.home_node(aircraft)
    result=0.
    for visit in route:
        node=problem._node_for_visit(visit)
        result+=problem._distances[previous][node]
        previous=node
    return result


def _capacity(problem,route):
    load=0
    for visit in route:
        load+=1 if visit>0 else -1
        if not 0<=load<=problem.capacity:
            return False
    return load==0


@dataclass(frozen=True,slots=True)
class _Group:
    i: int
    j: int
    a: int
    b: int
    c: int
    k1: int
    k2: int
    priority: tuple


def _group_record(g):
    return dict(i=g.i,j=g.j,a=g.a,b=g.b,c=g.c,k1=g.k1,k2=g.k2,
                final_aircraft=[g.c,g.c,g.a,g.b])


def _select_groups(problem,base,checked,local,weights,scales,seed,max_groups,
                   receiver_limit,compensation_limit,position_limit,deadline,stats):
    """Shared arm-independent geometric shortlist, then legal whole insertions."""
    index=build_plan_index(problem,base)
    owners=index.delivery_route_by_task
    by_owner={a:tuple(t for t in problem.task_ids if owners[t]==a) for a in range(problem.drone_count)}
    homes=problem.home_points()
    reduced_cache={}
    insertion_cache={}
    bases={}
    neighbour_pairs=set()
    for i in problem.task_ids:
        _check(deadline,stats)
        ti,a=problem.task(i),owners[i]
        neighbours=sorted((j for j in problem.task_ids if owners[j]!=a),
            key=lambda j:(ti.delivery.distance_to(problem.task(j).delivery),j))[:3]
        for j in neighbours:
            neighbour_pairs.add(tuple(sorted((i,j))))
    for i,j in sorted(neighbour_pairs):
        _check(deadline,stats)
        ti,a=problem.task(i),owners[i]
        tj,b=problem.task(j),owners[j]
        receivers=[]
        for c in range(problem.drone_count):
            if c in (a,b) or len(by_owner[c])<2:
                continue
            destinations=[homes[c],*(problem.task(t).delivery for t in by_owner[c])]
            affinity=min(ti.delivery.distance_to(p)+tj.delivery.distance_to(p) for p in destinations)
            receivers.append((affinity,c))
        for affinity,c in sorted(receivers)[:receiver_limit]:
            risk=sum(weights[0]*int(checked.delivery_times_min[t]>problem.task(t).deadline_min+1e-9)/scales[0]
                +weights[1]*max(0.,checked.delivery_times_min[t]-problem.task(t).deadline_min)/scales[1]
                for t in (i,j))
            priority=(weights[2]*(ti.delivery.distance_to(tj.delivery)+affinity)/scales[2]-.5*risk,
                      (i*31+j*17+c*7+seed)%104729,i,j,c)
            bases[(i,j,c)]=(priority,a,b)
            stats["receiver_bases_generated"]+=1
    # A fixed shared bound before expensive legal compensation ranking.
    shortlisted=sorted(bases.items(),key=lambda p:p[1][0])[:max_groups*2]
    families=[]
    for (i,j,c),(priority,a,b) in shortlisted:
        _check(deadline,stats)
        compensation=[]
        for removed,target in ((i,a),(j,b)):
            route=reduced_cache.setdefault(removed,tuple(v for v in base[target] if abs(v)!=removed))
            geometry=sorted(by_owner[c],key=lambda k:(
                problem.task(k).pickup.distance_to(homes[target])+problem.task(k).delivery.distance_to(homes[target]),k))
            ranked=[]
            for k in geometry[:max(6,2*compensation_limit)]:
                _check(deadline,stats)
                cache_key=(removed,c,k)
                if cache_key not in insertion_cache:
                    stats["selector_insertion_calls"]+=1
                    opts=route_insertion_options(problem,local,route,target,k,
                        candidate_limit=position_limit,option_count=1)
                    _check(deadline,stats)
                    if opts:
                        removal=_distance(problem,tuple(v for v in base[c] if abs(v)!=k),c)-_distance(problem,base[c],c)
                        delta=opts[0].delta
                        value=sum(w*x/s for w,x,s in zip(weights,
                            (delta.late_count,delta.total_lateness_min,delta.distance_km+removal),scales))
                        insertion_cache[cache_key]=value
                    else:
                        insertion_cache[cache_key]=None
                value=insertion_cache[cache_key]
                if value is not None:
                    ranked.append((value,k))
            compensation.append(sorted(ranked)[:compensation_limit])
        choices=[]
        for v1,k1 in compensation[0]:
            for v2,k2 in compensation[1]:
                if k1!=k2:
                    choices.append(_Group(i,j,a,b,c,k1,k2,(priority[0]+v1+v2,*priority[1:],k1,k2)))
        if choices:
            families.append(sorted(choices,key=lambda g:g.priority))
    # One representative per i/j/C first, then additional directed k1/k2
    # alternatives. In particular k1->A,k2->B and the reverse are distinct.
    selected=[]
    for depth in range(compensation_limit**2):
        layer=sorted((f[depth] for f in families if len(f)>depth),key=lambda g:g.priority)
        selected.extend(layer[:max_groups-len(selected)])
        if len(selected)>=max_groups:
            break
    return tuple(selected)


def _given_groups(problem,base,records,max_groups):
    """Validate reusable selector evidence without changing its fixed order."""
    index=build_plan_index(problem,base)
    owners=index.delivery_route_by_task
    if not isinstance(records,(tuple,list)) or len(records)>max_groups:
        raise ValueError("common selector records must be a bounded list")
    groups=[]
    seen=set()
    for order,record in enumerate(records):
        if not isinstance(record,dict):
            raise ValueError("invalid common selector record")
        try:
            values=tuple(record[k] for k in ("i","j","a","b","c","k1","k2"))
        except KeyError as error:
            raise ValueError("incomplete common selector record") from error
        if any(not isinstance(v,int) or isinstance(v,bool) for v in values):
            raise ValueError("selector identities must be integers")
        i,j,a,b,c,k1,k2=values
        if (len({i,j,k1,k2})!=4 or len({a,b,c})!=3
                or any(t not in owners for t in (i,j,k1,k2))
                or (owners[i],owners[j],owners[k1],owners[k2])!=(a,b,c,c)
                or values in seen or record.get("final_aircraft",[c,c,a,b])!=[c,c,a,b]):
            raise ValueError("common selector ownership/quota mapping is invalid")
        seen.add(values)
        groups.append(_Group(*values,(order,)))
    return tuple(groups)


def _partial(problem,routes,present,global_evaluator,stats):
    try:
        index=build_plan_index(problem,routes)
    except (ValueError,KeyError):
        stats["rejected_invalid_partial"]+=1
        return None
    if set(index.task_plans)!=set(present):
        stats["rejected_partial_coverage"]+=1
        return None
    for aircraft,route in enumerate(routes):
        if not _capacity(problem,route) or sum(a==aircraft for a in index.delivery_route_by_task.values())>problem.max_tasks_per_drone:
            stats["rejected_invalid_partial"]+=1
            return None
    stats["global_evaluations"]+=1
    scheduled=global_evaluator.evaluate(routes)
    if not scheduled.feasible:
        stats["rejected_cycles"]+=1
        return None
    if set(scheduled.delivery_times_min)!=set(present):
        stats["rejected_partial_coverage"]+=1
        return None
    return scheduled


@dataclass(frozen=True,slots=True)
class _State:
    routes: tuple
    key: tuple
    station: int|None


def _diverse(states,width):
    ranked=sorted(states,key=lambda s:(s.key,s.station or 0,s.routes))
    heads,other,families,seen=[],[],set(),set()
    for state in ranked:
        if state.routes in seen:
            continue
        seen.add(state.routes)
        if state.station not in families:
            heads.append(state)
            families.add(state.station)
        else:
            other.append(state)
    return tuple((heads+other)[:width])


def _direct(problem,state,task_id,aircraft,local,position_limit,option_count,deadline,stats):
    _check(deadline,stats)
    stats["direct_insertion_calls"]+=1
    options=route_insertion_options(problem,local,state.routes[aircraft],aircraft,task_id,
        candidate_limit=position_limit,option_count=option_count)
    _check(deadline,stats)
    for option in options:
        changed=list(state.routes)
        changed[aircraft]=option.route
        yield tuple(changed)


def _positions(problem,route,aircraft,point_visit,limit):
    load=0
    gaps=[]
    for position in range(len(route)+1):
        if load==0:
            left=problem.home_node(aircraft) if position==0 else problem._node_for_visit(route[position-1])
            target=problem._node_for_visit(point_visit)
            right=problem._node_for_visit(route[position]) if position<len(route) else None
            extra=problem._distances[left][target]
            if right is not None:
                extra+=problem._distances[target][right]-problem._distances[left][right]
            gaps.append((extra,position))
        if position<len(route):
            load+=1 if route[position]>0 else -1
    if len(gaps)<=limit:
        return tuple(p for _,p in sorted(gaps))
    earliest=min(p for _,p in gaps)
    return tuple(dict.fromkeys((earliest,*(p for _,p in sorted(gaps)))))[:limit]


def _negative_positions(problem,route,start,visit,aircraft,limit):
    """Drop by the first capacity violation of the temporarily missing D.

    Prefixes before the inserted take block are already legal. Removing a
    future D can temporarily overload the suffix; that suffix is never
    passed to a global evaluator. Inserting D before its first overflow
    makes that prefix legal, and the remaining selected D fixes the suffix.
    """
    load=0
    first_overflow=len(route)
    for position,value in enumerate(route):
        load+=1 if value>0 else -1
        if load>problem.capacity:
            first_overflow=position
            break
    candidates=[]
    for position in range(start,first_overflow+1):
        left=problem.home_node(aircraft) if position==0 else problem._node_for_visit(route[position-1])
        node=problem._node_for_visit(visit)
        delta=problem._distances[left][node]
        if position<len(route):
            right=problem._node_for_visit(route[position])
            delta+=problem._distances[node][right]-problem._distances[left][right]
        candidates.append((delta,position))
    order=tuple(dict.fromkeys((start,first_overflow,*(p for _,p in sorted(candidates)))))
    return order[:limit]


def _shared_distance(problem,route,first,second,aircraft):
    held=set()
    previous=problem.home_node(aircraft)
    result=0.
    for visit in route:
        node=problem._node_for_visit(visit)
        if held=={first,second}:
            result+=problem._distances[previous][node]
        if visit in (first,second):
            held.add(visit)
        elif visit in (-first,-second):
            held.discard(-visit)
        previous=node
    return result


def _rank_receiver(problem,routes,aircraft,selected,local,weights,scales,position_limit,
                   option_count,deadline,stats):
    unique={r for r in routes if _capacity(problem,r)}
    # Finite geometry shortlist, plus the earliest completion representative.
    # Local deltas are search ordering only; relay waits are handled globally.
    ordered=sorted(unique,key=lambda r:(_distance(problem,r,aircraft),r))
    if ordered:
        early=min(ordered,key=lambda r:(max(r.index(-t) for t in selected),r))
        ordered=[early,*[r for r in ordered if r!=early]]
    ranked=[]
    for route in ordered[:position_limit]:
        _check(deadline,stats)
        stats["receiver_local_evaluations"]+=1
        scored=local.evaluate(route,start_node=problem.home_node(aircraft))
        ranked.append((weighted_key(scored.score,weights,scales),route))
        _check(deadline,stats)
    return tuple(r for _,r in sorted(ranked)[:option_count])


def _receiver_direct(problem,state,g,local,weights,scales,position_limit,option_count,deadline,stats):
    route=state.routes[g.c]
    candidates=[]
    # Both flexible insertion orders, allowing C to collect, pool or serve
    # separately. These are local-only states until BOTH whole plans exist.
    for first,second in ((g.i,g.j),(g.j,g.i)):
        for first_routes in _direct(problem,state,first,g.c,local,position_limit,option_count,deadline,stats):
            intermediate=_State(first_routes,(),None)
            for full_routes in _direct(problem,intermediate,second,g.c,local,position_limit,option_count,deadline,stats):
                candidates.append(full_routes[g.c])
    # All six precedence-correct four-event orders, with the same gap bound.
    orders=(p for p in permutations((g.i,-g.i,g.j,-g.j))
            if p.index(g.i)<p.index(-g.i) and p.index(g.j)<p.index(-g.j))
    gaps=_positions(problem,route,g.c,g.i,position_limit)
    for order in orders:
        for gap in gaps:
            _check(deadline,stats)
            candidates.append(route[:gap]+order+route[gap:])
    return _rank_receiver(problem,candidates,g.c,(g.i,g.j),local,weights,scales,
                          position_limit,option_count,deadline,stats)


def _receiver_relay(problem,state,g,out_i,out_j,local,weights,scales,
                    position_limit,option_count,deadline,stats):
    route=state.routes[g.c]
    candidates=[]
    for gap in _positions(problem,route,g.c,out_i,position_limit):
        _check(deadline,stats)
        taken=route[:gap]+(out_i,out_j)+route[gap:]
        for first,second in ((out_i,out_j),(out_j,out_i)):
            for p1 in _negative_positions(problem,taken,gap+2,-first,g.c,position_limit):
                _check(deadline,stats)
                one=taken[:p1]+(-first,)+taken[p1:]
                for p2 in _negative_positions(problem,one,p1+1,-second,g.c,position_limit):
                    candidate=one[:p2]+(-second,)+one[p2:]
                    if _capacity(problem,candidate) and _shared_distance(problem,candidate,out_i,out_j,g.c)>1e-12:
                        candidates.append(candidate)
    stats["receiver_relay_layouts"]+=len(candidates)
    return _rank_receiver(problem,candidates,g.c,(out_i,out_j),local,weights,scales,
                          position_limit,option_count,deadline,stats)


def _bundle(problem,state,g,legs,local,weights,scales,relay_enabled,position_limit,
            option_count,deadline,stats):
    if not relay_enabled:
        for receiver in _receiver_direct(problem,state,g,local,weights,scales,position_limit,option_count,deadline,stats):
            changed=list(state.routes)
            changed[g.c]=receiver
            yield tuple(changed),None
        return
    if problem.capacity<2:
        return
    for station in sorted(set(legs.get(g.i,{}))&set(legs.get(g.j,{}))):
        _check(deadline,stats)
        stats["stations_considered"]+=1
        in_i,out_i=legs[g.i][station]
        in_j,out_j=legs[g.j][station]
        incoming=[]
        for aircraft,leg in ((g.a,in_i),(g.b,in_j)):
            stats["leg_insertion_calls"]+=1
            incoming.append(route_leg_insertion_options(problem,local,state.routes[aircraft],aircraft,leg,-leg,
                candidate_limit=position_limit,option_count=option_count,counts_toward_k=False,event_cap=None))
            _check(deadline,stats)
        receivers=_receiver_relay(problem,state,g,out_i,out_j,local,weights,scales,
                                  position_limit,option_count,deadline,stats)
        for left in incoming[0]:
            for right in incoming[1]:
                for receiver in receivers:
                    changed=list(state.routes)
                    changed[g.a],changed[g.b],changed[g.c]=left.route,right.route,receiver
                    # Both IN+OUT pairs are now complete and safe to submit.
                    yield tuple(changed),station


def _rebuild(problem,base,g,legs,local,global_evaluator,weights,scales,relay_enabled,
             position_limit,option_count,beam_width,deadline,stats):
    selected={g.i,g.j,g.k1,g.k2}
    others=set(problem.task_ids)-selected
    reduced=_remove(problem,base,selected)
    schedule=_partial(problem,reduced,others,global_evaluator,stats)
    _check(deadline,stats)
    if schedule is None:
        return ()
    empty=_State(reduced,weighted_key(schedule.score,weights,scales),None)
    complete=[]
    records=[]
    for order in (("bundle",g.k1,g.k2),("bundle",g.k2,g.k1),
                  (g.k1,g.k2,"bundle"),(g.k2,g.k1,"bundle")):
        survivors=(empty,)
        present=set(others)
        stages=[]
        for item in order:
            _check(deadline,stats)
            present.update((g.i,g.j) if item=="bundle" else (item,))
            proposals=[]
            for state in survivors:
                if item=="bundle":
                    options=_bundle(problem,state,g,legs,local,weights,scales,relay_enabled,
                                    position_limit,option_count,deadline,stats)
                else:
                    target=g.a if item==g.k1 else g.b
                    options=((r,state.station) for r in _direct(problem,state,item,target,local,
                                position_limit,option_count,deadline,stats))
                for routes,station in options:
                    _check(deadline,stats)
                    scored=_partial(problem,routes,present,global_evaluator,stats)
                    _check(deadline,stats)
                    if scored is not None:
                        proposals.append(_State(routes,weighted_key(scored.score,weights,scales),station))
            survivors=_diverse(proposals,beam_width)
            stages.append(dict(insert=item,present_tasks=sorted(present),scheduled_states=len(proposals),
                beam_states=len(survivors),station_families=sorted({s.station for s in survivors if s.station is not None})))
        complete.extend(survivors)
        records.append(dict(order=list(order),stages=stages,complete_candidates=len(survivors)))
    stats["group_records"].append(dict(**_group_record(g),pipelines=records,complete_candidates=len(complete)))
    stats["complete_scheduled_candidates"]+=len(complete)
    return tuple(complete)


def _snapshot(problem,routes,checked,weights,scales,*,phase,move=None):
    index=build_plan_index(problem,routes)
    return dict(routes=[list(r) for r in routes],score=dict(late_count=checked.score.late_count,
        total_lateness_min=checked.score.total_lateness_min,distance_km=checked.score.distance_km),
        key=list(weighted_key(checked.score,weights,scales)),valid=True,complete=True,phase=phase,move=move,
        cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        relay_task_count=len(index.relay_task_ids))


def _verify_structure(problem,base,routes,g,station,relay_enabled):
    index=build_plan_index(problem,routes)
    selected=(g.i,g.j,g.k1,g.k2)
    expected=(g.c,g.c,g.a,g.b)
    if any(index.delivery_route_by_task[t]!=a for t,a in zip(selected,expected)):
        raise AssertionError("four-task final responsibility mapping changed")
    before=build_plan_index(problem,base)
    if any(sum(a==owner for owner in index.delivery_route_by_task.values())!=
           sum(a==owner for owner in before.delivery_route_by_task.values()) for a in range(problem.drone_count)):
        raise AssertionError("final delivery quota was not restored")
    if _remove(problem,routes,selected)!=_remove(problem,base,selected):
        raise AssertionError("an unselected event changed aircraft or relative order")
    for t in (g.k1,g.k2):
        if index.task_plans[t].mode!="DIRECT":
            raise AssertionError("compensation must be a whole DIRECT request")
    shared=0.
    if relay_enabled:
        if station is None:
            raise AssertionError("relay arm cannot reuse DIRECT candidates")
        for t,source in ((g.i,g.a),(g.j,g.b)):
            if (index.task_plans[t].mode!="RELAY" or index.task_plans[t].relay_id!=station
                    or index.inbound_route_by_task[t]!=source or index.outbound_route_by_task[t]!=g.c):
                raise AssertionError("both original sources must relay to the same third aircraft/station")
        out_i,out_j=(_legs(problem)[t][station][1] for t in (g.i,g.j))
        route=routes[g.c]
        take=route.index(out_i)
        if take+1>=len(route) or route[take+1]!=out_j or sum(1 if v>0 else -1 for v in route[:take])!=0:
            raise AssertionError("receiver must take both OUT legs consecutively at an empty-load gap")
        shared=_shared_distance(problem,route,out_i,out_j,g.c)
        if shared<=1e-12:
            raise AssertionError("selected requests must share a positive-distance loaded flight")
    else:
        if any(index.task_plans[t].mode!="DIRECT" for t in (g.i,g.j)):
            raise AssertionError("DIRECT control acquired relay legs")
        shared=_shared_distance(problem,routes[g.c],g.i,g.j,g.c)
    return shared


def refine(problem,routes,*,deadline,weights,scales,seed=2026101503,relay_enabled=True,
           max_groups=12,receiver_limit=2,compensation_limit=3,position_limit=16,
           option_count=2,beam_width=6,full_candidates_per_group=4,common_selector_records=None):
    """Fixed-network repooling; input is deliberately restricted to pure DIRECT.

    Both arms share selector, final map, four reconstruction pipelines,
    insertion bounds, beam and complete-candidate limits. The receiver's
    bounded placement proposals differ only because one mode collects
    whole tasks and the other takes their OUT legs together at a station.
    All candidates start from the same original layout, not from a growing
    best checkpoint. Metadata records finite coverage, not optimality.
    """
    weights,scales=tuple(weights),tuple(scales)
    limits=(max_groups,receiver_limit,compensation_limit,position_limit,option_count,beam_width,full_candidates_per_group)
    if (len(weights)!=3 or len(scales)!=3 or any(not isfinite(x) or x<=0 for x in (*weights,*scales))
            or not isclose(sum(weights),1.) or not isfinite(deadline) or not isinstance(relay_enabled,bool)
            or any(not isinstance(x,int) or isinstance(x,bool) or x<1 for x in limits)
            or position_limit<4 or beam_width<4):
        raise ValueError("finite normalized objective/deadline, boolean relay mode, and positive bounded options required")
    started=perf_counter()
    base=tuple(tuple(r) for r in routes)
    if len(base)>problem.drone_count:
        raise ValueError("too many routes")
    base+=((),)*(problem.drone_count-len(base))
    checked=evaluate_solution(problem,base,_force_full=True)
    if not checked.valid:
        raise ValueError(f"invalid initial solution: {checked.violations}")
    if build_plan_index(problem,base).relay_task_ids:
        raise ValueError("this experiment requires a pure DIRECT initial solution")
    best,best_checked=base,checked
    initial_key=best_key=weighted_key(checked.score,weights,scales)
    stats=dict(method="Three-Aircraft-Two-Source-Relay-Repooling",seed=seed,relay_enabled=relay_enabled,
        receiver_bases_generated=0,groups_examined=0,selector_insertion_calls=0,
        direct_insertion_calls=0,leg_insertion_calls=0,stations_considered=0,
        receiver_local_evaluations=0,receiver_relay_layouts=0,global_evaluations=0,full_evaluations=1,
        rejected_invalid_partial=0,rejected_partial_coverage=0,rejected_cycles=0,rejected_invalid_full=0,
        complete_scheduled_candidates=0,complete_proposals=0,accepted_improvements=0,
        deadline_reached=False,group_records=[],selected_groups=[],input_snapshot=_snapshot(problem,base,checked,weights,scales,phase="input"),
        selector_sha256=sha256(b"[]").hexdigest(),selector_completed=False,
        first_complete_candidate=None,best_constructed_candidate=None,full_candidate_snapshots=[],accepted_moves=[],
        full_validation_timings=[],deadline_perf_counter=deadline,selector_reused=common_selector_records is not None,
        configuration=dict(max_groups=max_groups,receiver_limit=receiver_limit,compensation_limit=compensation_limit,
            position_limit=position_limit,option_count=option_count,beam_width=beam_width,
            full_candidates_per_group=full_candidates_per_group,event_cap=None,
            destination_neighbours=3,receiver_base_limit=max_groups*2,
            compensation_geometry_probe_limit=max(6,2*compensation_limit),
            candidate_stations="all_common_existing_registered_stations",
            receiver_complete_layout_limit=position_limit,receiver_zero_load_gap_limit=position_limit,
            receiver_negative_position_limit=position_limit,pipelines="bundle_first_and_compensation_first_both_orders",
            full_candidate_bound="same_total_full_candidates_per_group_in_both_arms"))
    local,global_evaluator,legs=RouteEvaluator(problem),GlobalRelayEvaluator(problem),_legs(problem)
    try:
        _check(deadline,stats)
        groups=(_select_groups(problem,base,checked,local,weights,scales,seed,max_groups,receiver_limit,
                               compensation_limit,position_limit,deadline,stats)
                if common_selector_records is None else _given_groups(problem,base,common_selector_records,max_groups))
        stats["selected_groups"]=[_group_record(g) for g in groups]
        stats["selector_sha256"]=sha256(json.dumps(stats["selected_groups"],sort_keys=True,separators=(",",":")).encode()).hexdigest()
        stats["selector_completed"]=True
        stats["shortlist_coverage"]=dict(groups=len(groups),task_pairs=len({(g.i,g.j) for g in groups}),
            receiver_families=len({(g.i,g.j,g.c) for g in groups}),aircraft_triples=len({(g.a,g.b,g.c) for g in groups}))
        for g in groups:
            _check(deadline,stats)
            stats["groups_examined"]+=1
            complete=_rebuild(problem,base,g,legs,local,global_evaluator,weights,scales,relay_enabled,
                              position_limit,option_count,beam_width,deadline,stats)
            # The shared default bound of four preserves four station heads.
            # An explicitly smaller bound is recorded coverage truncation.
            for state in _diverse(complete,full_candidates_per_group):
                _check(deadline,stats)
                full=evaluate_solution(problem,state.routes,_force_full=True)
                stats["full_evaluations"]+=1
                _check(deadline,stats)
                verified_at=perf_counter()
                if not full.valid:
                    stats["rejected_invalid_full"]+=1
                    continue
                shared=_verify_structure(problem,base,state.routes,g,state.station,relay_enabled)
                move=dict(**_group_record(g),mode="RELAY" if relay_enabled else "DIRECT",
                    station_id=state.station,source_aircraft=[g.a,g.b],receiver_aircraft=g.c,
                    compensation_mode="DIRECT",shared_double_loaded_km=shared)
                snapshot=_snapshot(problem,state.routes,full,weights,scales,phase="full_proposal",move=move)
                _check(deadline,stats)
                stats["complete_proposals"]+=1
                stats["full_candidate_snapshots"].append(snapshot)
                stats["full_validation_timings"].append(dict(snapshot_index=len(stats["full_candidate_snapshots"])-1,
                    full_verified_at_perf_counter=verified_at))
                if stats["first_complete_candidate"] is None:
                    stats["first_complete_candidate"]=snapshot
                if stats["best_constructed_candidate"] is None or tuple(snapshot["key"])<tuple(stats["best_constructed_candidate"]["key"]):
                    stats["best_constructed_candidate"]=snapshot
                key=weighted_key(full.score,weights,scales)
                if key<best_key:
                    _check(deadline,stats)
                    best,best_checked,best_key=state.routes,full,key
                    stats["accepted_improvements"]+=1
                    stats["accepted_moves"].append(dict(**move,full_snapshot=snapshot,
                        full_verified_at_perf_counter=verified_at,accepted_at_perf_counter=perf_counter()))
    except _Deadline:
        pass
    index=build_plan_index(problem,best)
    stats.update(initial_key=list(initial_key),final_key=list(best_key),
        final_cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        runtime_seconds=perf_counter()-started)
    return dict(routes=best,evaluation=best_checked,iterations=stats["groups_examined"],metadata=stats)
