"""Research-only one-way relay plus a quota-balancing whole DIRECT request.

For i initially finalized by A and j by B, both whole plans are removed. The
new final map is i->B,j->A. DIRECT control rebuilds both requests directly.
The relay extension can instead collect i on A, transfer it through one
existing registered station, and finalize it on B; j stays a whole DIRECT
request on A. Thus j need not detour through a reciprocal exchange station.

Incomplete relay legs never reach the global scheduler. IN_A and OUT_B are
inserted atomically as a whole i plan. Partial beam scores compare the same
present original-task set, and only fully checked complete solutions can be
adopted. All unrelated events keep their aircraft and relative order.
"""
from __future__ import annotations

from dataclasses import dataclass
from heapq import heappush,heapreplace
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
    return tuple(tuple(v for v in route if _task(problem,v) not in selected) for route in routes)


def _legs(problem):
    result={}
    for task_id,legs in problem._legs_by_task.items():
        stations={}
        for leg in legs:
            stations.setdefault(leg.relay_id,{})[leg.kind]=leg.id
        result[task_id]={station:(parts["RELAY_IN"],parts["RELAY_OUT"])
            for station,parts in stations.items() if set(parts)=={"RELAY_IN","RELAY_OUT"}}
    return result


@dataclass(frozen=True,slots=True)
class _Pair:
    i: int
    j: int
    a: int
    b: int
    priority: tuple


@dataclass(frozen=True,slots=True)
class _Worst:
    pair: _Pair
    def __lt__(self,other):
        return self.pair.priority>other.pair.priority


def _pairs(problem,routes,checked,weights,scales,seed,limit,deadline,stats):
    index=build_plan_index(problem,routes)
    direct=sorted(index.direct_task_ids)
    homes=problem.home_points()
    heaps=[]
    for i in direct:
        _check(deadline,stats)
        ti,a=problem.task(i),index.delivery_route_by_task[i]
        for j in direct:
            b=index.delivery_route_by_task[j]
            if a==b:
                continue
            tj=problem.task(j)
            # Search-order heuristic only. The whole i plan is globally timed
            # before it survives a beam; no geometric value is a proof bound.
            geography=(ti.delivery.distance_to(homes[b])+tj.pickup.distance_to(homes[a])
                       -ti.delivery.distance_to(homes[a])-tj.pickup.distance_to(homes[b]))
            risk=sum(weights[0]*int(checked.delivery_times_min[t]>problem.task(t).deadline_min+1e-9)/scales[0]
                     +weights[1]*max(0.,checked.delivery_times_min[t]-problem.task(t).deadline_min)/scales[1]
                     for t in (i,j))
            priority=(weights[2]*geography/scales[2]-.5*risk,
                      abs(ti.deadline_min-tj.deadline_min),(i*31+j*17+seed)%104729,i,j)
            pair=_Pair(i,j,a,b,priority)
            stats["pairs_generated"]+=1
            if len(heaps)<limit:
                heappush(heaps,_Worst(pair))
            elif priority<heaps[0].pair.priority:
                heapreplace(heaps,_Worst(pair))
    return tuple(item.pair for item in sorted(heaps,key=lambda item:item.pair.priority))


def _partial(problem,routes,present,global_evaluator,stats):
    """Present tasks must each have complete DIRECT or paired relay service."""
    try:
        index=build_plan_index(problem,routes)
    except (ValueError,KeyError):
        stats["rejected_invalid_partial"]+=1
        return None
    if set(index.task_plans)!=set(present):
        stats["rejected_partial_coverage"]+=1
        return None
    for aircraft,route in enumerate(routes):
        load=0
        for visit in route:
            load+=1 if visit>0 else -1
            if not 0<=load<=problem.capacity:
                stats["rejected_invalid_partial"]+=1
                return None
        if load or sum(owner==aircraft for owner in index.delivery_route_by_task.values())>problem.max_tasks_per_drone:
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
    mode: str
    station: int|None


def _diverse(states,width):
    ranked=sorted(states,key=lambda s:(s.key,s.mode,s.station or 0,s.routes))
    heads,other,families,seen=[],[],set(),set()
    for state in ranked:
        if state.routes in seen:
            continue
        seen.add(state.routes)
        family=(state.mode,state.station)
        if family not in families:
            heads.append(state)
            families.add(family)
        else:
            other.append(state)
    # Preserve a complete DIRECT and a relay even when multiple stations rank
    # before DIRECT. Remaining slots retain station and position alternatives.
    mandatory=[]
    for mode in ("DIRECT","RELAY"):
        match=next((state for state in heads if state.mode==mode),None)
        if match is not None:
            mandatory.append(match)
    remaining=[state for state in heads if state not in mandatory]+other
    return tuple((mandatory+remaining)[:width])


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


def _i_plans(problem,state,pair,legs,local,position_limit,option_count,relay_enabled,deadline,stats):
    for routes in _direct(problem,state,pair.i,pair.b,local,position_limit,option_count,deadline,stats):
        yield routes,"DIRECT",None
    if not relay_enabled:
        return
    for station,(inbound,outbound) in sorted(legs.get(pair.i,{}).items()):
        _check(deadline,stats)
        stats["stations_considered"]+=1
        stats["leg_insertion_calls"]+=2
        incoming=route_leg_insertion_options(problem,local,state.routes[pair.a],pair.a,inbound,-inbound,
            candidate_limit=position_limit,option_count=option_count,counts_toward_k=False,event_cap=None)
        _check(deadline,stats)
        outgoing=route_leg_insertion_options(problem,local,state.routes[pair.b],pair.b,outbound,-outbound,
            candidate_limit=position_limit,option_count=option_count,counts_toward_k=True,event_cap=None)
        _check(deadline,stats)
        for left in incoming:
            for right in outgoing:
                changed=list(state.routes)
                changed[pair.a],changed[pair.b]=left.route,right.route
                # Only this complete IN+OUT i plan can reach a global scorer.
                yield tuple(changed),"RELAY",station


def _rebuild(problem,base,pair,legs,local,global_evaluator,weights,scales,
             relay_enabled,position_limit,option_count,beam_width,deadline,stats):
    reduced=_remove(problem,base,(pair.i,pair.j))
    others=set(problem.task_ids)-{pair.i,pair.j}
    scheduled=_partial(problem,reduced,others,global_evaluator,stats)
    _check(deadline,stats)
    if scheduled is None:
        return ()
    empty=_State(reduced,weighted_key(scheduled.score,weights,scales),"DIRECT",None)
    complete=[]
    stages=[]
    for i_first in (True,False):
        _check(deadline,stats)
        first_task=pair.i if i_first else pair.j
        present=others|{first_task}
        partial=[]
        proposals=(_i_plans(problem,empty,pair,legs,local,position_limit,option_count,relay_enabled,deadline,stats)
                   if i_first else ((r,"DIRECT",None) for r in _direct(problem,empty,pair.j,pair.a,
                        local,position_limit,option_count,deadline,stats)))
        for routes,mode,station in proposals:
            _check(deadline,stats)
            scored=_partial(problem,routes,present,global_evaluator,stats)
            _check(deadline,stats)
            if scored is not None:
                partial.append(_State(routes,weighted_key(scored.score,weights,scales),mode,station))
        survivors=_diverse(partial,beam_width)
        stages.append(dict(first_task=first_task,present_task_count=len(present),
            complete_plan_states=len(partial),beam_states=len(survivors),
            relay_station_families=sorted({s.station for s in survivors if s.mode=="RELAY"})))
        for state in survivors:
            proposals=(((r,state.mode,state.station) for r in _direct(problem,state,pair.j,pair.a,
                            local,position_limit,option_count,deadline,stats)) if i_first else
                       _i_plans(problem,state,pair,legs,local,position_limit,option_count,relay_enabled,deadline,stats))
            for routes,mode,station in proposals:
                _check(deadline,stats)
                scored=_partial(problem,routes,set(problem.task_ids),global_evaluator,stats)
                _check(deadline,stats)
                if scored is not None:
                    stats["complete_scheduled_candidates"]+=1
                    complete.append(_State(routes,weighted_key(scored.score,weights,scales),mode,station))
    stats["pair_records"].append(dict(i=pair.i,j=pair.j,from_aircraft=[pair.a,pair.b],
        final_aircraft=[pair.b,pair.a],stages=stages,complete_candidates=len(complete),
        best_scheduled_key=list(min(complete,key=lambda s:s.key).key) if complete else None))
    return tuple(complete)


def _snapshot(problem,routes,checked,weights,scales,*,phase,move=None):
    index=build_plan_index(problem,routes)
    return dict(routes=[list(r) for r in routes],score=dict(late_count=checked.score.late_count,
        total_lateness_min=checked.score.total_lateness_min,distance_km=checked.score.distance_km),
        key=list(weighted_key(checked.score,weights,scales)),valid=True,complete=True,phase=phase,move=move,
        cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        relay_task_count=len(index.relay_task_ids))


def refine(problem,routes,*,deadline,weights,scales,seed=2026101502,relay_enabled=True,
           max_pairs=64,position_limit=16,option_count=3,beam_width=8,full_candidates_per_pair=2):
    """One-way relay/whole-request trade over fixed, bounded selected pairs.

    Both arms use the same selector, two rebuilding orders, DIRECT insertion
    bounds, beam width and full candidate count. RELAY additionally has i's
    registered station modes. Every proposal starts from the same input; the
    original incumbent is guarded by a complete full check before deadline.
    The model, station/home coordinates, registry and fleet are unchanged.
    """
    weights,scales=tuple(weights),tuple(scales)
    limits=(max_pairs,position_limit,option_count,beam_width,full_candidates_per_pair)
    if (len(weights)!=3 or len(scales)!=3 or any(not isfinite(x) or x<=0 for x in (*weights,*scales))
            or not isclose(sum(weights),1.) or not isfinite(deadline)
            or not isinstance(relay_enabled,bool)
            or any(not isinstance(x,int) or isinstance(x,bool) or x<1 for x in limits)
            or position_limit<4 or beam_width<2):
        raise ValueError("finite normalized objective/deadline, boolean relay mode, and positive bounded options required")
    started=perf_counter()
    base=tuple(tuple(r) for r in routes)
    if len(base)>problem.drone_count:
        raise ValueError("too many routes")
    base+=((),)*(problem.drone_count-len(base))
    checked=evaluate_solution(problem,base,_force_full=True)
    if not checked.valid:
        raise ValueError(f"invalid initial solution: {checked.violations}")
    best,best_checked=base,checked
    initial_key=best_key=weighted_key(checked.score,weights,scales)
    stats=dict(method="Quota-Balanced-One-Way-Relay-Exchange",seed=seed,relay_enabled=relay_enabled,
        pairs_generated=0,pairs_examined=0,global_evaluations=0,full_evaluations=1,
        direct_insertion_calls=0,leg_insertion_calls=0,stations_considered=0,
        rejected_cycles=0,rejected_invalid_partial=0,rejected_partial_coverage=0,
        rejected_invalid_full=0,complete_scheduled_candidates=0,complete_proposals=0,
        accepted_improvements=0,deadline_reached=False,pair_records=[],
        input_snapshot=_snapshot(problem,base,checked,weights,scales,phase="input"),
        first_complete_candidate=None,best_constructed_candidate=None,
        full_candidate_snapshots=[],accepted_moves=[],
        configuration=dict(max_pairs=max_pairs,position_limit=position_limit,option_count=option_count,
            beam_width=beam_width,full_candidates_per_pair=full_candidates_per_pair,
            event_cap=None,candidate_stations="all_existing_registered_stations"))
    local,global_evaluator,legs=RouteEvaluator(problem),GlobalRelayEvaluator(problem),_legs(problem)
    try:
        _check(deadline,stats)
        pairs=_pairs(problem,base,checked,weights,scales,seed,max_pairs,deadline,stats)
        stats["selected_pairs"]=[dict(i=p.i,j=p.j,a=p.a,b=p.b) for p in pairs]
        stats["shortlist_coverage"]=dict(distinct_original_task_pairs=len({tuple(sorted((p.i,p.j))) for p in pairs}),
            distinct_directed_aircraft_pairs=len({(p.a,p.b) for p in pairs}))
        for pair in pairs:
            _check(deadline,stats)
            stats["pairs_examined"]+=1
            complete=_rebuild(problem,base,pair,legs,local,global_evaluator,weights,scales,relay_enabled,
                position_limit,option_count,beam_width,deadline,stats)
            # Do not polish/gate partial scores against input. Preserve one
            # fully reconstructed DIRECT and one relay family when available.
            ranked=_diverse(complete,full_candidates_per_pair)
            for state in ranked:
                _check(deadline,stats)
                full=evaluate_solution(problem,state.routes,_force_full=True)
                stats["full_evaluations"]+=1
                _check(deadline,stats)
                if not full.valid:
                    stats["rejected_invalid_full"]+=1
                    continue
                index=build_plan_index(problem,state.routes)
                if index.delivery_route_by_task[pair.i]!=pair.b or index.delivery_route_by_task[pair.j]!=pair.a:
                    raise AssertionError("quota-balanced final responsibility mapping was not restored")
                if _remove(problem,state.routes,(pair.i,pair.j))!=_remove(problem,base,(pair.i,pair.j)):
                    raise AssertionError("an unrelated event changed aircraft or relative order")
                move=dict(i=pair.i,j=pair.j,from_aircraft=[pair.a,pair.b],
                          final_aircraft=[pair.b,pair.a],mode=state.mode,station_id=state.station,
                          j_mode="DIRECT")
                snapshot=_snapshot(problem,state.routes,full,weights,scales,phase="full_proposal",move=move)
                _check(deadline,stats)
                stats["complete_proposals"]+=1
                stats["full_candidate_snapshots"].append(snapshot)
                if stats["first_complete_candidate"] is None:
                    stats["first_complete_candidate"]=snapshot
                if stats["best_constructed_candidate"] is None or tuple(snapshot["key"])<tuple(stats["best_constructed_candidate"]["key"]):
                    stats["best_constructed_candidate"]=snapshot
                key=weighted_key(full.score,weights,scales)
                if key<best_key:
                    _check(deadline,stats)
                    best,best_checked,best_key=state.routes,full,key
                    stats["accepted_improvements"]+=1
                    stats["accepted_moves"].append(dict(**move,full_snapshot=snapshot))
    except _Deadline:
        pass
    index=build_plan_index(problem,best)
    stats.update(initial_key=list(initial_key),final_key=list(best_key),
        final_cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        runtime_seconds=perf_counter()-started)
    return dict(routes=best,evaluation=best_checked,iterations=stats["pairs_examined"],metadata=stats)
