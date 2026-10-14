"""Process-local mobile-aware layout retention for frozen repooling beams.

All State.key values remain the original fixed-site global physical keys.
Only equally covered, atomic two-IN/two-OUT layouts receive an optimistic
mobile ranking. Four station physical heads are retained first; other slots
prefer a feasible mobile bound, early empty-recipient prefixes and diverse
source-drop frontiers. DIRECT rebuilding delegates the original functions.
"""
from __future__ import annotations

from contextlib import contextmanager
from math import isclose, isfinite
from time import perf_counter
from types import ModuleType

from uav_dispatch.model import Score
from uav_dispatch.relay import build_plan_index

try:
    import mobile_repooling_geometry as geometry
except ImportError:
    from algorithm.experiments import mobile_repooling_geometry as geometry


_active = False
PATCH_NAMES = ("_rebuild", "_diverse", "_rank_receiver")
SOURCE_FRONTIER_EVENT_BIN = 4
RECEIVER_TIME_BIN_COUNT = 4


def _weighted(score, weights, scales):
    physical = (score.late_count, score.total_lateness_min, score.distance_km)
    return (sum(w*v/s for w,v,s in zip(weights,physical,scales)),*physical)


def _task(problem, visit):
    leg = (problem.leg_registry or {}).get(abs(visit))
    return leg.task_id if leg is not None else abs(visit)


def _coverage(problem, routes):
    return frozenset(_task(problem,v) for r in routes for v in r)


def _distance(problem, route, aircraft):
    previous, distance = problem.home_points()[aircraft], 0.
    for visit in route:
        point = problem.point_for_visit(visit)
        distance += previous.distance_to(point)
        previous = point
    return distance


def _layout_bound(problem, state, group, *, deadline):
    """Absolute optimistic score for this state's PRESENT task set only.

    Pure coordinate walks avoid another GlobalEvaluator/Problem construction.
    The producer already checked every atomic partial state before the beam.
    Missing compensation tasks are simply absent, as in its physical key.
    """
    index = build_plan_index(problem,state.routes)
    if set(index.relay_task_ids) != {group.i,group.j}:
        return None
    if any(index.inbound_route_by_task[t] != a or index.outbound_route_by_task[t] != group.c
           for t,a in ((group.i,group.a),(group.j,group.b))):
        return None
    incoming, outgoing = [], []
    for task in (group.i,group.j):
        plan = index.task_plans[task]
        if plan.relay_id != state.station:
            return None
        incoming.append(next(l for l in plan.leg_ids if problem.leg_registry[l].kind == "RELAY_IN"))
        outgoing.append(next(l for l in plan.leg_ids if problem.leg_registry[l].kind == "RELAY_OUT"))
    aircraft = (group.a,group.b,group.c)
    cuts = (state.routes[group.a].index(-incoming[0]),state.routes[group.b].index(-incoming[1]),
            state.routes[group.c].index(outgoing[0]))
    receiver = state.routes[group.c]
    if receiver[cuts[2]:cuts[2]+2] != tuple(outgoing):
        return None
    if sum(1 if v>0 else -1 for v in receiver[:cuts[2]]) != 0:
        return None
    station = next(s.point for s in problem.relay_stations if s.id == state.station)
    starts,ends,times,prefix,suffix = [],[],[],[],[]
    variable, total_distance = 0.,0.
    for a, route in enumerate(state.routes):
        if perf_counter() >= deadline:
            return None
        total_distance += _distance(problem,route,a)
        if a not in aircraft:
            previous,clock = problem.home_points()[a],0.
            for visit in route:
                point = problem.point_for_visit(visit)
                clock += previous.distance_to(point)/problem.speed_km_per_min
                if visit < 0:
                    prefix.append((_task(problem,visit),clock))
                previous = point
    for role,(a,cut) in enumerate(zip(aircraft,cuts)):
        route = state.routes[a]
        previous,clock = problem.home_points()[a],0.
        for visit in route[:cut]:
            if perf_counter() >= deadline:
                return None
            point = problem.point_for_visit(visit)
            clock += previous.distance_to(point)/problem.speed_km_per_min
            leg = (problem.leg_registry or {}).get(abs(visit))
            if visit < 0 and (leg is None or leg.kind == "RELAY_OUT"):
                prefix.append((_task(problem,visit),clock))
            previous = point
        starts.append(previous)
        times.append(clock)
        tail = route[cut+(2 if role == 2 else 1):]
        end = problem.point_for_visit(tail[0]) if tail else None
        ends.append(end)
        variable += previous.distance_to(station) + (station.distance_to(end) if end is not None else 0.)
        previous,extra = end,0.
        for visit in tail:
            point = problem.point_for_visit(visit)
            if previous is not None:
                extra += previous.distance_to(point)/problem.speed_km_per_min
            leg = (problem.leg_registry or {}).get(abs(visit))
            if visit < 0 and (leg is None or leg.kind == "RELAY_OUT"):
                suffix.append((role,_task(problem,visit),extra))
            previous = point
    if ends[2] is None:
        return None
    context = geometry.prepare_geometry(starts,ends,times,speed=problem.speed_km_per_min)
    constant = total_distance-variable
    if constant < -1e-8:
        raise ValueError("negative fixed-layout distance constant")
    distance = geometry.distance_lower_bound(context,constant_distance=max(0.,constant))["distance_km"]
    clocks = geometry.clock_lower_bounds(context)["first_future_clock_min"]
    deliveries = prefix + [(task,clocks[role]+extra) for role,task,extra in suffix]
    # Prefix clocks are also guarded against a floating lateness discontinuity.
    late = [max(0.,clock-problem.task(task).deadline_min-1e-8) for task,clock in deliveries]
    score = Score(sum(value>1e-9 for value in late),sum(value for value in late if value>1e-9),distance)
    return dict(score=score,context=context,possible=geometry.receiver_first_possible(context)["possible"],
        prefix_times=tuple(times),cuts=cuts,coverage=frozenset(index.task_plans),
        source_frontier_family=(cuts[0]//SOURCE_FRONTIER_EVENT_BIN,cuts[1]//SOURCE_FRONTIER_EVENT_BIN),
        recipient_cut_family=cuts[2]//SOURCE_FRONTIER_EVENT_BIN)


@contextmanager
def joint_layouts(repool_module, problem, *, weights, scales, deadline, statistics=None,
                  max_lower_bound_evaluations_per_group=4096, record_limit=24):
    """Temporarily retain mobile opportunity without changing physical scores.

    The original rebuild keeps its exact pipelines, selector and candidate
    generation limits. ``_rebuild`` only establishes the current g/base scope;
    the corresponding final beam selection uses that same scope. At most eight
    beam slots are accepted. No partial bound is compared to the full incumbent.
    """
    global _active
    if _active:
        raise RuntimeError("nested joint-layout contexts are unsupported")
    weights,scales = tuple(weights),tuple(scales)
    if (not isfinite(deadline) or len(weights)!=3 or len(scales)!=3
        or any(not isfinite(v) or v<=0 for v in (*weights,*scales)) or not isclose(sum(weights),1.)):
        raise ValueError("finite normalized objective and deadline required")
    if problem.capacity!=2 or problem.distance_matrix_km is not None:
        raise ValueError("joint mobile layouts require Euclidean distances and capacity two")
    if type(max_lower_bound_evaluations_per_group) is not int or max_lower_bound_evaluations_per_group<1:
        raise ValueError("positive bounded lower-bound evaluation limit required")
    if type(record_limit) is not int or record_limit<0:
        raise ValueError("nonnegative record limit required")
    original = {name:getattr(repool_module,name) for name in PATCH_NAMES}
    if isinstance(repool_module,ModuleType) and original["_rebuild"].__globals__ is not repool_module.__dict__:
        raise ValueError("rebuild globals must be the same module receiving layout patches")
    stats = statistics if statistics is not None else {}
    stats.update(rebuild_calls=0,direct_rebuild_calls=0,mobile_beam_calls=0,direct_delegations=0,
        lower_bound_evaluations=0,lower_bound_cache_hits=0,lower_bound_limit_fallbacks=0,
        receiver_rank_calls=0,receiver_extra_local_evaluations=0,receiver_early_cut_retained=0,
        physical_station_heads_retained=0,mobile_family_retained=0,coverage_mismatches=0,
        ranking_records=[],receiver_records=[],patches_restored=False,deadline_reached=False,
        configuration=dict(max_lower_bound_evaluations_per_group=max_lower_bound_evaluations_per_group,
            record_limit=record_limit,beam_width_max=8,physical_station_heads_first=True,
            partial_bounds_used_for_full_incumbent_pruning=False,state_physical_keys_unchanged=True,
            source_frontier_event_bin=SOURCE_FRONTIER_EVENT_BIN,receiver_time_bin_count=RECEIVER_TIME_BIN_COUNT,
            receiver_selection="old_physical_local_best_then_earliest_take_then_distinct_time_bins"))
    scope = dict(group=None,base=None,relay_enabled=None,cache={},evaluations=0,
                 local_deadline=deadline,producer_stats=None)
    def check():
        if perf_counter()>=min(deadline,scope["local_deadline"]):
            stats["deadline_reached"]=True
            if scope["producer_stats"] is not None:scope["producer_stats"]["deadline_reached"]=True
            raise repool_module._Deadline
    def rebuild(p,base,g,legs,local,global_evaluator,w,s,relay_enabled,
                position_limit,option_count,beam_width,local_deadline,producer_stats):
        check()
        if p is not problem or tuple(w)!=weights or tuple(s)!=scales:
            raise ValueError("joint layout scope problem/objective changed")
        index = build_plan_index(problem,base)
        if index.relay_task_ids or set(index.task_plans)!=set(problem.task_ids):
            raise ValueError("joint layout scope requires a complete pure DIRECT base")
        if (len({g.a,g.b,g.c})!=3 or len({g.i,g.j,g.k1,g.k2})!=4
            or tuple(index.delivery_route_by_task[t] for t in (g.i,g.j,g.k1,g.k2))!=(g.a,g.b,g.c,g.c)):
            raise ValueError("joint group ownership does not match its DIRECT base")
        if not 1<=beam_width<=8:
            raise ValueError("joint beam width must be at most eight")
        scope.update(group=g,base=tuple(tuple(r) for r in base),relay_enabled=relay_enabled,cache={},evaluations=0,
                     local_deadline=min(deadline,local_deadline),producer_stats=producer_stats)
        stats["rebuild_calls"]+=1
        stats["direct_rebuild_calls"]+=int(not relay_enabled)
        try:
            result = original["_rebuild"](p,base,g,legs,local,global_evaluator,w,s,relay_enabled,
                position_limit,option_count,beam_width,min(deadline,local_deadline),producer_stats)
            check()
            # Keep this group scope for its immediate complete-state _diverse
            # call in original refine. The next _rebuild replaces it entirely.
            return result
        except BaseException:
            scope.update(group=None,base=None,relay_enabled=None,cache={},evaluations=0)
            raise

    def diverse(states,width):
        check()
        states=tuple(states)
        if not 1<=width<=8:
            raise ValueError("joint beam width must be at most eight")
        if not states or not scope["relay_enabled"] or scope["group"] is None or any(s.station is None for s in states):
            stats["direct_delegations"]+=1
            return original["_diverse"](states,width)
        coverage = {_coverage(problem,state.routes) for state in states}
        if len(coverage)!=1:
            stats["coverage_mismatches"]+=1
            raise ValueError("mobile beam ranking requires identical present-task coverage")
        ordered=sorted(states,key=lambda state:(state.key,state.station,state.routes))
        unique,seen=[],set()
        for state in ordered:
            if state.routes not in seen:
                seen.add(state.routes)
                unique.append(state)
        heads,stations=[],set()
        for state in unique:
            if state.station not in stations:
                heads.append(state)
                stations.add(state.station)
        result=heads[:width]
        stats["physical_station_heads_retained"]+=len(result)
        if len(result)==width:
            return tuple(result)
        ranked=[]
        for state in unique:
            check()
            cache_key=(state.station,state.routes)
            if cache_key in scope["cache"]:
                bound=scope["cache"][cache_key]
                stats["lower_bound_cache_hits"]+=1
            elif scope["evaluations"]>=max_lower_bound_evaluations_per_group:
                bound=None
                stats["lower_bound_limit_fallbacks"]+=1
            else:
                bound=_layout_bound(problem,state,scope["group"],deadline=min(deadline,scope["local_deadline"]))
                scope["cache"][cache_key]=bound
                scope["evaluations"]+=1
                stats["lower_bound_evaluations"]+=1
                check()
            if bound is not None and bound["coverage"]!=next(iter(coverage)):
                raise ValueError("geometry bound has different partial coverage")
            if bound is not None and bound["possible"]:
                ranked.append((_weighted(bound["score"],weights,scales),state,bound))
        ranked.sort(key=lambda item:(item[0],item[2]["prefix_times"][2],item[2]["cuts"],item[1].station,item[1].routes))
        existing={s.routes for s in result}
        added=[]
        def add(item,family):
            if len(result)<width and item is not None and item[1].routes not in existing:
                result.append(item[1]);existing.add(item[1].routes);added.append((item,family))
        add(next((x for x in ranked if x[1].routes not in existing),None),"best_mobile_lower_bound")
        early=sorted(ranked,key=lambda x:(x[2]["prefix_times"][2],x[0],x[2]["cuts"],x[1].routes))
        add(next((x for x in early if x[1].routes not in existing),None),"earliest_recipient_prefix")
        frontier={x[0][2]["source_frontier_family"] for x in added}
        for item in ranked:
            if item[2]["source_frontier_family"] not in frontier:
                before=len(result);add(item,"diverse_source_drop_frontier")
                if len(result)>before:frontier.add(item[2]["source_frontier_family"])
        for item in ranked:add(item,"mobile_lower_bound_fill")
        for state in unique:
            if len(result)<width and state.routes not in existing:
                result.append(state);existing.add(state.routes)
        stats["mobile_beam_calls"]+=1
        stats["mobile_family_retained"]+=len(added)
        if len(stats["ranking_records"])<record_limit:
            stats["ranking_records"].append(dict(present_tasks=sorted(next(iter(coverage))),
                complete=len(next(iter(coverage)))==len(problem.task_ids),physical_heads=[dict(station=s.station,key=list(s.key)) for s in heads[:width]],
                mobile_retained=[dict(station=item[1].station,physical_key=list(item[1].key),optimistic_key=list(item[0]),
                    prefix_times=list(item[2]["prefix_times"]),cuts=list(item[2]["cuts"]),family=family)
                    for item,family in added],input_states=len(states),retained=len(result)))
        return tuple(result)

    def rank_receiver(p,routes,aircraft,selected,local,w,s,position_limit,option_count,local_deadline,producer_stats):
        check()
        routes=tuple(routes)
        registry=p.leg_registry or {}
        relay=bool(scope["relay_enabled"] and scope["group"] is not None and aircraft==scope["group"].c
            and len(selected)==2 and all(t in registry and registry[t].kind=="RELAY_OUT" for t in selected))
        if not relay:
            return original["_rank_receiver"](p,routes,aircraft,selected,local,w,s,position_limit,
                option_count,min(deadline,local_deadline),producer_stats)
        stats["receiver_rank_calls"]+=1
        baseline=original["_rank_receiver"](p,routes,aircraft,selected,local,w,s,position_limit,
            option_count,min(deadline,local_deadline),producer_stats)
        check()
        if not baseline or option_count<2:
            return baseline
        representatives={}
        clock_cache={}
        for route in routes:
            check()
            if not repool_module._capacity(p,route):continue
            cut=route.index(selected[0])
            if route[cut:cut+2]!=tuple(selected):continue
            prefix=route[:cut]
            if sum(1 if v>0 else -1 for v in prefix)!=0:continue
            if prefix not in clock_cache:clock_cache[prefix]=_distance(p,prefix,aircraft)/p.speed_km_per_min
            distance=_distance(p,route,aircraft)
            current=representatives.get(prefix)
            item=(clock_cache[prefix],cut,distance,route)
            if current is None or (distance,route)<(current[2],current[3]):representatives[prefix]=item
        if not representatives:return baseline
        geometric=sorted(representatives.values(),key=lambda x:(x[0],x[1],x[2],x[3]))
        chosen=[baseline[0]]
        earliest=geometric[0]
        if earliest[3] not in chosen:chosen.append(earliest[3])
        maxclock=max(x[0] for x in geometric)
        quantum=max(maxclock/RECEIVER_TIME_BIN_COUNT,1e-9)
        def timebin(clock):return min(RECEIVER_TIME_BIN_COUNT-1,int(clock/quantum))
        used_bins={timebin(_distance(p,r[:r.index(selected[0])],aircraft)/p.speed_km_per_min) for r in chosen}
        extras=[]
        for item in sorted(geometric,key=lambda x:(x[2],x[0],x[1],x[3])):
            if len(chosen)+len(extras)>=option_count:break
            if timebin(item[0]) not in used_bins and item[3] not in chosen:
                extras.append(item);used_bins.add(timebin(item[0]))
        for item in geometric:
            if len(chosen)+len(extras)>=option_count:break
            if item[3] not in chosen and all(item[3]!=x[3] for x in extras):extras.append(item)
        for item in extras:
            check()
            scored=local.evaluate(item[3],start_node=p.home_node(aircraft))
            producer_stats["receiver_local_evaluations"]+=1
            stats["receiver_extra_local_evaluations"]+=1
            check()
            chosen.append(item[3])
        for route in baseline:
            if len(chosen)<option_count and route not in chosen:chosen.append(route)
        stats["receiver_early_cut_retained"]+=int(earliest[3] in chosen)
        if len(stats["receiver_records"])<record_limit:
            stats["receiver_records"].append(dict(aircraft=aircraft,station=registry[selected[0]].relay_id,
                option_count=option_count,retained_take_positions=[r.index(selected[0]) for r in chosen[:option_count]],
                earliest_take_time_min=earliest[0],physical_local_best_retained=baseline[0] in chosen,
                time_bin_width_min=quantum,distinct_take_prefixes=len(representatives)))
        return tuple(chosen[:option_count])

    _active=True
    try:
        repool_module._rebuild=rebuild;repool_module._diverse=diverse;repool_module._rank_receiver=rank_receiver
        yield stats
    finally:
        for name,function in original.items():setattr(repool_module,name,function)
        scope.update(group=None,base=None,relay_enabled=None,cache={},evaluations=0)
        stats["patches_restored"]=True
        _active=False
