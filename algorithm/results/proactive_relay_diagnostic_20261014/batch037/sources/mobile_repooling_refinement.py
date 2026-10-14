"""Two sources and one receiver: bounded, zero-cache mobile repooling.

Pure DIRECT input and fixed Euclidean homes/resources are required. Complete
fixed-site repooling macros supply native layouts; their four used relay legs
are replaced by four new legs at one continuous meeting point. C must arrive
empty before BOTH sources. Sources drop and depart on their own arrivals;
C takes in actual arrival order, waits empty/with one parcel, then flies with
both. The fixed site's ground-cache model is never used as physical evidence.
Only the winning one-site topology is returned; all candidates use the same
original Problem, and no additional/free predeployment is introduced.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass,replace
from math import isclose,isfinite
from time import perf_counter

from uav_dispatch.model import Point,RelayStation,TransportLeg,Score
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution

try:
    import relay_repooling_exchange as repool
    import mobile_relay_refinement as mobile
    import mobile_repooling_geometry as geometry
except ImportError:
    from . import relay_repooling_exchange as repool
    from . import mobile_relay_refinement as mobile
    from . import mobile_repooling_geometry as geometry


class _Deadline(Exception):
    pass


def _check(deadline,stats):
    if perf_counter()>=deadline:
        stats["deadline_reached"]=True
        raise _Deadline


@dataclass(frozen=True,slots=True)
class _Spec:
    routes: tuple
    move: dict
    incoming: tuple
    outgoing: tuple
    cuts: tuple
    context: object
    constant_distance: float
    prefix_deliveries: tuple
    suffix_deliveries: tuple
    constraints: tuple
    raw_snapshot: dict


def _point(problem,visit,spec,meeting):
    if visit in (-spec.incoming[0],-spec.incoming[1],spec.outgoing[0],spec.outgoing[1]):
        return meeting
    return problem.point_for_visit(visit)


def _spec(problem,snapshot):
    routes=tuple(tuple(r) for r in snapshot["routes"])
    move=snapshot["move"]
    tasks=(move["i"],move["j"])
    legs=repool._legs(problem)
    incoming=tuple(legs[t][move["station_id"]][0] for t in tasks)
    outgoing=tuple(legs[t][move["station_id"]][1] for t in tasks)
    aircraft=(move["a"],move["b"],move["c"])
    cuts=(routes[aircraft[0]].index(-incoming[0]),routes[aircraft[1]].index(-incoming[1]),
          routes[aircraft[2]].index(outgoing[0]))
    if routes[aircraft[2]][cuts[2]:cuts[2]+2]!=outgoing:
        raise ValueError("raw macro OUT starts are not contiguous in source task order")
    if sum(1 if v>0 else -1 for v in routes[aircraft[2]][:cuts[2]])!=0:
        raise ValueError("raw receiver is not empty before its take block")
    starts,ends,times=[],[],[]
    prefix_deliveries=[]
    suffix_deliveries=[]
    constraints=[]
    station=next(s.point for s in problem.relay_stations if s.id==move["station_id"])
    variable=0.
    for role,(a,cut) in enumerate(zip(aircraft,cuts)):
        previous=problem.home_points()[a]
        clock=0.
        for v in routes[a][:cut]:
            p=problem.point_for_visit(v)
            clock+=previous.distance_to(p)/problem.speed_km_per_min
            if v<0 and abs(v) in problem.task_ids:
                prefix_deliveries.append((abs(v),clock))
            previous=p
        starts.append(previous)
        times.append(clock)
        suffix=routes[a][cut+(2 if role==2 else 1):]
        end=problem.point_for_visit(suffix[0]) if suffix else None
        ends.append(end)
        variable+=previous.distance_to(station)+(station.distance_to(end) if end is not None else 0.)
        extra=0.
        old=end
        for v in suffix:
            p=problem.point_for_visit(v)
            if old is not None:
                extra+=old.distance_to(p)/problem.speed_km_per_min
            leg=(problem.leg_registry or {}).get(abs(v))
            if v<0 and (leg is None or leg.kind=="RELAY_OUT"):
                task=leg.task_id if leg else abs(v)
                suffix_deliveries.append((role,task,extra))
                for source in ((0,1,2) if role==2 else (role,)):
                    constraints.append(dict(prefix_source=source,outgoing=end,
                        residual_deadline_min=problem.task(task).deadline_min-extra))
            old=p
    schedule=GlobalRelayEvaluator(problem).evaluate(routes)
    # All other aircraft have DIRECT requests and no dependence on R.
    for a,route in enumerate(routes):
        if a not in aircraft:
            prefix_deliveries.extend((abs(v),schedule.delivery_times_min[abs(v)]) for v in route if v<0)
    context=geometry.prepare_geometry(starts,ends,times,speed=problem.speed_km_per_min)
    def urgency(constraint):
        source=constraint["prefix_source"]
        optimistic=times[source]+starts[source].distance_to(constraint["outgoing"])/problem.speed_km_per_min
        slack=constraint["residual_deadline_min"]-optimistic
        # Feasible, tight ellipse targets precede already impossible deadlines.
        # This only orders seeds, never removes a task from a full score.
        return (slack<0,abs(slack),source)
    constraints.sort(key=urgency)
    return _Spec(routes,deepcopy(move),incoming,outgoing,cuts,context,
        schedule.score.distance_km-variable,tuple(prefix_deliveries),tuple(suffix_deliveries),tuple(constraints),snapshot)


def _lower(problem,spec):
    distance=geometry.distance_lower_bound(spec.context,constant_distance=spec.constant_distance)
    clocks=geometry.clock_lower_bounds(spec.context)
    deliveries=list(spec.prefix_deliveries)
    deliveries.extend((t,clocks["first_future_clock_min"][role]+extra) for role,t,extra in spec.suffix_deliveries)
    # Conservative margins protect lateness's 1e-9 discontinuity and roundoff.
    count=sum(clock>problem.task(t).deadline_min+1e-9+1e-8 for t,clock in deliveries)
    lateness=sum(max(0.,clock-problem.task(t).deadline_min-1e-8) for t,clock in deliveries)
    return Score(count,lateness,max(0.,distance["distance_km"])),dict(distance=distance["metadata"],clocks=clocks["metadata"])


def _manual(problem,spec,point):
    physical=geometry.receiver_first_feasible(spec.context,point)
    if not physical["feasible"]:
        return None
    order=physical["pickup_source_order"]
    routes=list(spec.routes)
    c=spec.move["c"]
    cut=spec.cuts[2]
    routes[c]=routes[c][:cut]+tuple(spec.outgoing[k] for k in order)+routes[c][cut+2:]
    distance=0.
    deliveries={}
    clocks=[]
    near=False
    for a,route in enumerate(routes):
        previous=problem.home_points()[a]
        clock=0.
        events=[]
        for v in route:
            p=_point(problem,v,spec,point)
            length=previous.distance_to(p)
            distance+=length
            clock+=length/problem.speed_km_per_min
            if a==c and v in spec.outgoing:
                clock=max(clock,physical["arrival_times_min"][spec.outgoing.index(v)])
            leg=(problem.leg_registry or {}).get(abs(v))
            if v<0 and (leg is None or leg.kind=="RELAY_OUT"):
                task=leg.task_id if leg else abs(v)
                deliveries[task]=clock
                near|=abs(clock-problem.task(task).deadline_min-1e-9)<=1e-8
            events.append(clock)
            previous=p
        clocks.append(tuple(events))
    late=[max(0.,clock-problem.task(t).deadline_min) for t,clock in deliveries.items()]
    score=Score(sum(v>1e-9 for v in late),sum(v for v in late if v>1e-9),distance)
    # Both parcels must share a genuine positive-distance flight after take2.
    shared=0.
    held=set()
    previous=problem.home_points()[c]
    for v in routes[c]:
        p=_point(problem,v,spec,point)
        if held==set(spec.outgoing):
            shared+=previous.distance_to(p)
        if v in spec.outgoing:
            held.add(v)
        elif -v in spec.outgoing:
            held.discard(-v)
        previous=p
    if shared<=1e-10:
        return None
    return dict(routes=tuple(routes),score=score,physical=physical,event_times=tuple(clocks),
                delivery_times=deliveries,near_deadline_boundary=near,shared_double_loaded_km=shared)


def _new_problem(problem,spec,point,manual):
    station=max((s.id for s in problem.relay_stations),default=0)+1
    next_id=max((*problem.task_ids,*(problem.leg_registry or {})))+1
    registry=dict(problem.leg_registry or {})
    remap={}
    for task,old_in,old_out in zip((spec.move["i"],spec.move["j"]),spec.incoming,spec.outgoing):
        for kind,old in (("RELAY_IN",old_in),("RELAY_OUT",old_out)):
            registry[next_id]=TransportLeg(next_id,task,kind,station)
            remap[old]=next_id
            next_id+=1
    candidates={t:tuple(ids) for t,ids in (problem.task_relay_candidates or {}).items()}
    for t in (spec.move["i"],spec.move["j"]):
        candidates[t]=(*candidates.get(t,()),station)
    changed=replace(problem,relay_stations=(*problem.relay_stations,RelayStation(station,point.x,point.y)),
        leg_registry=registry,task_relay_candidates=candidates)
    routes=tuple(tuple((1 if v>0 else -1)*remap[abs(v)] if abs(v) in remap else v for v in r) for r in manual["routes"])
    return changed,routes,station


def _meeting(problem,routes,spec,station,manual):
    tasks=(spec.move["i"],spec.move["j"])
    sources=(spec.move["a"],spec.move["b"])
    c=spec.move["c"]
    parts=repool._legs(problem)
    incoming=tuple(parts[t][station][0] for t in tasks)
    outgoing=tuple(parts[t][station][1] for t in tasks)
    drops=[routes[a].index(-v) for a,v in zip(sources,incoming)]
    takes=[routes[c].index(v) for v in outgoing]
    order=manual["physical"]["pickup_source_order"]
    arrival=manual["physical"]["arrival_times_min"]
    first,second=min(arrival[:2]),max(arrival[:2])
    point=next(s.point for s in problem.relay_stations if s.id==station)
    source_loads=[sum(1 if v>0 else -1 for v in routes[a][:p]) for a,p in zip(sources,drops)]
    event=lambda t,a,p,v,time:dict(task_id=t,route_index=a,position=p,visit=v,physical_time_min=time)
    drop_events=[event(t,a,p,-v,time) for t,a,p,v,time in zip(tasks,sources,drops,incoming,arrival[:2])]
    pickup_events=[event(tasks[k],c,takes[k],outgoing[k],arrival[k]) for k in order]
    move={**spec.move,"mode":"RELAY","station_id":station,"shared_double_loaded_km":manual["shared_double_loaded_km"]}
    return dict(meeting_type="two_source_receiver_first_repooling",kind="two_source_receiver_first_repooling",
        station_id=station,point=[point.x,point.y],task_ids=list(tasks),source_task_ids=list(tasks),
        source_aircraft=list(sources),receiver_aircraft=c,aircraft=[*sources,c],own_task_groups=[[tasks[0]],[tasks[1]],[]],
        compensation_task_ids=[spec.move["k1"],spec.move["k2"]],source_drop_positions=drops,
        receiver_take_positions=sorted(takes),cut_positions=[*drops,min(takes)],arrival_times_min=list(arrival),
        source_arrival_order=[tasks[k] for k in order],take_order_task_ids=[tasks[k] for k in order],
        source_drop_times_min=list(arrival[:2]),receiver_take_times_min=[first,second],handoff_times_min=list(arrival[:2]),
        synchronization_min=second,receiver_empty_wait_min=first-arrival[2],receiver_loaded_wait_min=second-first,
        receiver_waiting_empty_min=first-arrival[2],receiver_waiting_one_parcel_min=second-first,
        receiver_load_before_takes=[0,1],receiver_load_after_takes=[1,2],source_load_before_drops=source_loads,
        aircraft_waiting_min=[0.,0.,second-arrival[2]],waiting_loads=[*source_loads,0],
        ground_cache_min=0.,shared_double_loaded_km=manual["shared_double_loaded_km"],
        drop_events=drop_events,pickup_events=pickup_events,repooling_move=move)


def _point_search(problem,spec,*,deadline,weights,scales,point_limit,pattern_rounds,point_eval_limit,bbox,stats):
    proposed=geometry.points(spec.context,deadline=deadline,limit=point_limit,deadline_constraints=spec.constraints)
    stats["geometry_seed_diagnostics"].append(proposed["metadata"])
    original=next(s.point for s in problem.relay_stations if s.id==spec.move["station_id"])
    # uC is the guaranteed feasible anchor when receiver-first is possible;
    # keep it even when the caller limits the seed list to a single point.
    seeds=tuple(dict.fromkeys((spec.context.incoming_points[2],original,*proposed["points"])))[:point_limit]
    seen={}
    valid=[]
    def probe(point):
        _check(deadline,stats)
        if not bbox[0]<=point.x<=bbox[1] or not bbox[2]<=point.y<=bbox[3]:
            return None
        key=(point.x,point.y)
        if key in seen:
            return seen[key]
        if len(seen)>=point_eval_limit:
            return None
        manual=_manual(problem,spec,point)
        seen[key]=None
        stats["points_examined"]+=1
        _check(deadline,stats)
        if manual is not None:
            item=(repool.weighted_key(manual["score"],weights,scales),point,manual)
            seen[key]=item
            valid.append(item)
            return item
        stats["points_rejected_physical"]+=1
        return None
    for point in seeds:
        probe(point)
    starts=sorted(valid,key=lambda item:(item[0],item[1].x,item[1].y))[:2]
    for current in starts:
        step=max(bbox[1]-bbox[0],bbox[3]-bbox[2])/8.
        for _ in range(pattern_rounds):
            _check(deadline,stats)
            candidates=[current]
            for dx,dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
                point=Point(min(bbox[1],max(bbox[0],current[1].x+dx*step)),
                            min(bbox[3],max(bbox[2],current[1].y+dy*step)))
                candidate=probe(point)
                if candidate is not None:
                    candidates.append(candidate)
            best=min(candidates,key=lambda item:(item[0],item[1].x,item[1].y))
            if best[0]<current[0]:
                current=best
            else:
                step/=2.
    stats["point_budget_cutoffs"]+=int(len(seen)>=point_eval_limit)
    return tuple(sorted(valid,key=lambda item:(item[0],item[1].x,item[1].y))[:2])


def refine(problem,routes,*,deadline,weights,scales,seed=2026101505,relay_enabled=True,
           proposal_fraction=.35,raw_spec_limit=48,point_limit=24,pattern_rounds=4,
           point_eval_limit=96,full_candidate_limit=32,max_groups=12,receiver_limit=2,
           compensation_limit=3,position_limit=16,option_count=2,beam_width=6,
           full_candidates_per_group=4,common_selector_records=None):
    weights,scales=tuple(weights),tuple(scales)
    limits=(raw_spec_limit,point_limit,pattern_rounds,point_eval_limit,full_candidate_limit)
    if (len(weights)!=3 or len(scales)!=3 or any(not isfinite(v) or v<=0 for v in (*weights,*scales))
            or not isclose(sum(weights),1.) or not isfinite(deadline) or type(relay_enabled) is not bool
            or not isfinite(proposal_fraction) or not 0<proposal_fraction<1
            or any(type(v) is not int or v<1 for v in limits)):
        raise ValueError("finite normalized objective/deadline and positive bounded geometry options required")
    if problem.capacity!=2 or problem.distance_matrix_km is not None:
        raise ValueError("mobile repooling requires capacity two and Euclidean distances")
    if common_selector_records is not None and any(r.get("i",0)>=r.get("j",0) for r in common_selector_records):
        raise ValueError("mobile source task identities must be in ascending order")
    started=perf_counter()
    base=tuple(tuple(r) for r in routes)
    base+=((),)*max(0,problem.drone_count-len(base))
    checked=evaluate_solution(problem,base,_force_full=True)
    if not checked.valid or build_plan_index(problem,base).relay_task_ids:
        raise ValueError("valid pure DIRECT initial solution required")
    initial_key=best_key=repool.weighted_key(checked.score,weights,scales)
    best,best_problem,best_checked=base,problem,checked
    now=perf_counter()
    raw_deadline=now+max(0.,deadline-now)*proposal_fraction if relay_enabled else deadline
    raw=repool.refine(problem,base,deadline=raw_deadline,weights=weights,scales=scales,seed=seed,
        relay_enabled=relay_enabled,max_groups=max_groups,receiver_limit=receiver_limit,
        compensation_limit=compensation_limit,position_limit=position_limit,option_count=option_count,
        beam_width=beam_width,full_candidates_per_group=full_candidates_per_group,common_selector_records=common_selector_records)
    if not relay_enabled:
        raw["metadata"].update(method="Mobile-Repooling-Matched-DIRECT-Control",model_extension="none",
            mobile_meetings=[],configuration=dict(raw["metadata"]["configuration"],proposal_fraction=proposal_fraction,
                direct_uses_total_deadline=True),runtime_seconds=perf_counter()-started)
        return raw
    raw_stats=deepcopy(raw["metadata"])
    for snapshot in raw_stats["full_candidate_snapshots"]:
        snapshot.update(topology=mobile.topology(problem),mobile_meetings=[])
    stats=dict(method="Three-Aircraft-Receiver-First-Mobile-Repooling",model_extension="continuous_zero_cache_receiver_first_rendezvous",
        seed=seed,relay_enabled=True,raw_stage=raw_stats,selected_groups=raw_stats["selected_groups"],
        selector_sha256=raw_stats["selector_sha256"],selector_completed=raw_stats["selector_completed"],
        configuration=dict(proposal_fraction=proposal_fraction,raw_spec_limit=raw_spec_limit,point_limit=point_limit,
            pattern_rounds=pattern_rounds,point_eval_limit=point_eval_limit,full_candidate_limit=full_candidate_limit,
            raw_repooling=raw_stats["configuration"],clock_margin_min=1e-8,distance_margin_km=1e-10,
            lateness_epsilon_min=1e-9,new_sites_per_returned_solution=1,
            deadline_seed_priority="feasible_tight_residual_triangle_slack"),
        input_snapshot=mobile._snapshot(problem,base,checked,weights,scales,[]),first_complete_candidate=None,
        best_constructed_candidate=None,full_candidate_snapshots=[],accepted_moves=[],mobile_meetings=[],
        specifications_examined=0,specifications_pruned_lower_bound=0,specifications_rejected_impossible=0,
        points_examined=0,points_rejected_physical=0,points_pruned_manual_score=0,point_budget_cutoffs=0,
        new_problems_built=0,global_evaluations=0,full_evaluations=1,accepted_improvements=0,
        deadline_reached=False,geometry_seed_diagnostics=[],spec_records=[],deadline_perf_counter=deadline)
    locations=(*problem.home_points(),*(p for t in problem.tasks for p in (t.pickup,t.delivery)))
    bbox=(min(p.x for p in locations),max(p.x for p in locations),min(p.y for p in locations),max(p.y for p in locations))
    try:
        _check(deadline,stats)
        raw_routes=tuple(tuple(r) for r in raw["routes"])
        raw_checked=evaluate_solution(problem,raw_routes,_force_full=True)
        stats["full_evaluations"]+=1
        _check(deadline,stats)
        raw_key=repool.weighted_key(raw_checked.score,weights,scales)
        if raw_checked.valid and raw_key<best_key:
            snapshot=next(s for s in raw_stats["full_candidate_snapshots"]
                if tuple(tuple(r) for r in s["routes"])==raw_routes)
            move=snapshot["move"]
            group=repool._Group(*(move[k] for k in ("i","j","a","b","c","k1","k2")),())
            repool._verify_structure(problem,base,raw_routes,group,move["station_id"],True)
            _check(deadline,stats)
            best,best_checked,best_key=raw_routes,raw_checked,raw_key
            stats["accepted_improvements"]+=1
            stats["accepted_moves"].append(dict(**move,kind="raw_fixed_macro",original_move=move,
                full_snapshot=snapshot,accepted_at_perf_counter=perf_counter()))
        specifications=[]
        seen=set()
        for snapshot in sorted(raw_stats["full_candidate_snapshots"],key=lambda s:(s["key"],s["routes"])):
            key=tuple(tuple(r) for r in snapshot["routes"])
            if key in seen:
                continue
            seen.add(key)
            spec=_spec(problem,snapshot)
            lower,diagnostic=_lower(problem,spec)
            specifications.append((repool.weighted_key(lower,weights,scales),spec,diagnostic))
            _check(deadline,stats)
            if len(specifications)>=raw_spec_limit:
                break
        for lower,spec,diagnostic in sorted(specifications,key=lambda item:(item[0],item[1].routes)):
            _check(deadline,stats)
            stats["specifications_examined"]+=1
            record=dict(move=spec.move,lower_key=list(lower),lower_diagnostics=diagnostic,
                incoming_points=[[p.x,p.y] for p in spec.context.incoming_points],prefix_times=list(spec.context.prefix_times))
            stats["spec_records"].append(record)
            if not geometry.receiver_first_possible(spec.context)["possible"]:
                stats["specifications_rejected_impossible"]+=1
                record["pruned"]="receiver_first_impossible"
                continue
            if lower[0]>best_key[0]+1e-12:
                stats["specifications_pruned_lower_bound"]+=1
                record["pruned"]="absolute_conservative_lower_bound"
                continue
            for key,point,manual in _point_search(problem,spec,deadline=deadline,weights=weights,scales=scales,
                    point_limit=point_limit,pattern_rounds=pattern_rounds,point_eval_limit=point_eval_limit,bbox=bbox,stats=stats):
                _check(deadline,stats)
                if key[0]>best_key[0]+1e-12 and not manual["near_deadline_boundary"]:
                    stats["points_pruned_manual_score"]+=1
                    continue
                if stats["new_problems_built"]>=full_candidate_limit:
                    break
                changed,candidate,station=_new_problem(problem,spec,point,manual)
                stats["new_problems_built"]+=1
                _check(deadline,stats)
                global_score=GlobalRelayEvaluator(changed).evaluate(candidate)
                stats["global_evaluations"]+=1
                full=evaluate_solution(changed,candidate,_force_full=True)
                stats["full_evaluations"]+=1
                _check(deadline,stats)
                if not global_score.feasible or not full.valid:
                    raise AssertionError("complete manual proposal failed canonical feasibility")
                index=build_plan_index(changed,candidate)
                expected={spec.move["i"]:spec.move["c"],spec.move["j"]:spec.move["c"],
                          spec.move["k1"]:spec.move["a"],spec.move["k2"]:spec.move["b"]}
                if any(index.delivery_route_by_task[t]!=a for t,a in expected.items()):
                    raise AssertionError("mobile final responsibility changed")
                if set(index.relay_task_ids)!={spec.move["i"],spec.move["j"]}:
                    raise AssertionError("mobile repooling must retain exactly two cross relay tasks")
                if repool._remove(changed,candidate,expected)!=repool._remove(problem,base,expected):
                    raise AssertionError("an unrelated event changed aircraft or relative order")
                if (full.score.late_count!=manual["score"].late_count
                        or not isclose(full.score.distance_km,manual["score"].distance_km,abs_tol=1e-8,rel_tol=1e-10)
                        or not isclose(full.score.total_lateness_min,manual["score"].total_lateness_min,abs_tol=1e-8,rel_tol=1e-10)):
                    raise AssertionError("manual physical score differs from full DAG")
                for a,route in enumerate(candidate):
                    for p,_ in enumerate(route):
                        if not isclose(global_score.event_times_min[(a,p)],manual["event_times"][a][p],abs_tol=1e-8,rel_tol=1e-10):
                            raise AssertionError("manual physical event clocks differ from canonical DAG")
                meeting=_meeting(changed,candidate,spec,station,manual)
                snapshot=mobile._snapshot(changed,candidate,full,weights,scales,[meeting])
                snapshot.update(phase="mobile_full_proposal",move=meeting["repooling_move"],
                    original_move=spec.move,manual_score=dict(late_count=manual["score"].late_count,
                        total_lateness_min=manual["score"].total_lateness_min,distance_km=manual["score"].distance_km),
                    canonical_manual_agreement=True)
                _check(deadline,stats)
                stats["full_candidate_snapshots"].append(snapshot)
                if stats["first_complete_candidate"] is None:
                    stats["first_complete_candidate"]=snapshot
                if stats["best_constructed_candidate"] is None or tuple(snapshot["key"])<tuple(stats["best_constructed_candidate"]["key"]):
                    stats["best_constructed_candidate"]=snapshot
                checked_key=repool.weighted_key(full.score,weights,scales)
                if checked_key<best_key:
                    _check(deadline,stats)
                    best,best_problem,best_checked,best_key=candidate,changed,full,checked_key
                    stats["accepted_improvements"]+=1
                    stats["mobile_meetings"]=[meeting]
                    stats["accepted_moves"].append(dict(**meeting["repooling_move"],original_move=spec.move,
                        kind="mobile_repooling",full_snapshot=snapshot,accepted_at_perf_counter=perf_counter()))
            if stats["new_problems_built"]>=full_candidate_limit:
                break
    except _Deadline:
        pass
    index=build_plan_index(best_problem,best)
    stats.update(initial_key=list(initial_key),final_key=list(best_key),
        final_cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        runtime_seconds=perf_counter()-started)
    return dict(problem=best_problem,routes=best,evaluation=best_checked,iterations=stats["specifications_examined"],metadata=stats)
