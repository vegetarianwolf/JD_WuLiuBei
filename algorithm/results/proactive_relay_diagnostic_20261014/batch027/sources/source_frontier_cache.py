"""Bounded truthful IN-frontier diversity and exact process-local memoization.

Enter inside the frozen joint-layout context. Existing physical best IN choice
is retained; additional choices use actual source pickup/drop clocks, without
changing any RouteInsertionOption.delta. Complete partial schedules and pure
receiver route rankings are memoized only under their full immutable inputs.
No partial geometry bound or receiver-first infeasibility pruning is added.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import replace
from math import isclose,isfinite
from time import perf_counter

from uav_dispatch.search import _node_distance_delta


_active=False
PATCH_NAMES=("_rebuild","route_leg_insertion_options","_partial","_rank_receiver")
TIME_BINS=4
PICKUP_EVENT_BIN=4


class _Identity:
    """Identity keys retain their objects, so IDs cannot be recycled by GC."""
    __slots__=("value",)
    def __init__(self,value):self.value=value
    def __hash__(self):return id(self.value)
    def __eq__(self,other):return isinstance(other,_Identity) and self.value is other.value


def _key(score,weights,scales):
    physical=(score.late_count,score.total_lateness_min,score.distance_km)
    return (sum(w*v/s for w,v,s in zip(weights,physical,scales)),*physical)


def _raw_times(problem,surface,aircraft,start,end,pickup,delivery):
    """O(1) true no-wait pickup/drop clock geometry; no objective shortcut."""
    distances,nodes=problem._distances,surface.visit_nodes
    home=problem.home_node(aircraft)
    before=home if pickup==0 else nodes[pickup-1]
    after=nodes[pickup] if pickup<len(nodes) else None
    first=distances[before][start]
    clock=(0. if pickup==0 else surface.arrival[pickup-1])+first/problem.speed_km_per_min
    if delivery==pickup+1:
        return clock,clock+distances[start][end]/problem.speed_km_per_min
    shift=(first+(distances[start][after] if after is not None else 0.)
           -(distances[before][after] if after is not None else 0.))/problem.speed_km_per_min
    drop=surface.arrival[delivery-2]+shift+distances[nodes[delivery-2]][end]/problem.speed_km_per_min
    return clock,drop


@contextmanager
def source_frontier_cache(repool_module,problem,*,weights,scales,deadline,statistics=None,
                          raw_position_limit=512,memo_limit=8192):
    """Restore all four supplied functions, including outer joint wrappers.

    Two selected RELAY_IN legs on pure-DIRECT source routes receive at most
    ``raw_position_limit`` additional raw local evaluations, besides the
    caller's original bounded native evaluation. Larger capacity surfaces
    retain geometric, earliest-drop and earliest-pickup slot representatives.
    DIRECT source choices delegate. Cache hits never increment actual producer
    evaluation counters; logical requests/hits are separately reported.
    """
    global _active
    if _active:raise RuntimeError("nested source-frontier contexts are unsupported")
    weights,scales=tuple(weights),tuple(scales)
    if (not isfinite(deadline) or len(weights)!=3 or len(scales)!=3
        or any(not isfinite(v) or v<=0 for v in (*weights,*scales)) or not isclose(sum(weights),1.)):
        raise ValueError("finite normalized weights/scales and deadline required")
    if problem.capacity!=2 or problem.distance_matrix_km is not None:
        raise ValueError("source frontier requires capacity two and Euclidean distances")
    if type(raw_position_limit) is not int or raw_position_limit<4 or type(memo_limit) is not int or memo_limit<1:
        raise ValueError("raw limit at least four and positive memo limit required")
    original={name:getattr(repool_module,name) for name in PATCH_NAMES}
    stats=statistics if statistics is not None else {}
    stats.update(source_requests=0,source_actual_calls=0,source_memo_hits=0,source_delegations=0,
        capacity_positions_seen=0,raw_positions_exactly_scored=0,raw_surface_truncations=0,
        source_native_delta_evaluations=0,source_raw_delta_evaluations=0,
        native_physical_best_retained=0,earliest_drop_retained=0,earliest_pickup_retained=0,
        partial_requests=0,partial_actual_calls=0,partial_actual_global_calls=0,partial_memo_hits=0,
        partial_cached_rejections=0,receiver_requests=0,receiver_actual_calls=0,receiver_memo_hits=0,
        memo_evictions=0,rebuild_calls=0,deadline_reached=False,patches_restored=False,source_records=[],
        configuration=dict(raw_position_limit=raw_position_limit,memo_limit=memo_limit,time_bins=TIME_BINS,
            pickup_event_bin=PICKUP_EVENT_BIN,source_policy="native_physical_best+earliest_drop+earliest_pickup+clock_cut_diversity",
            raw_overflow_policy="geometry_half+early_drop_quarter+early_pickup_quarter",
            partial_geometry_pruning=False,delta_fields_unchanged=True,
            receiver_source_prefix_used=False,cache_counters="actual calls and logical memo hits separately"))
    stats["configuration"]["native_baseline_scoring_separate"]="caller candidate_limit; additional raw surface <= raw_position_limit"
    scope=dict(group=None,relay=False,end=deadline,producer_stats=None)
    caches={name:OrderedDict() for name in ("source","partial","receiver")}
    def check(call_deadline=None):
        end=min(deadline,scope["end"]) if call_deadline is None else min(deadline,scope["end"],call_deadline)
        if perf_counter()>=end:
            stats["deadline_reached"]=True
            if scope["producer_stats"] is not None:scope["producer_stats"]["deadline_reached"]=True
            raise repool_module._Deadline
    def memo_get(name,key):
        cache=caches[name]
        if key not in cache:return False,None
        value=cache.pop(key);cache[key]=value
        return True,value
    def memo_put(name,key,value):
        cache=caches[name]
        if len(cache)>=memo_limit:
            cache.popitem(last=False);stats["memo_evictions"]+=1
        cache[key]=value
    def rebuild(p,base,g,legs,local,global_evaluator,w,s,relay_enabled,
                position_limit,option_count,beam_width,local_deadline,producer_stats):
        check()
        if p is not problem or tuple(w)!=weights or tuple(s)!=scales:
            raise ValueError("source-frontier scope problem/objective changed")
        scope.update(group=g,relay=relay_enabled,end=min(deadline,local_deadline),producer_stats=producer_stats)
        stats["rebuild_calls"]+=1
        try:
            return original["_rebuild"](p,base,g,legs,local,global_evaluator,w,s,relay_enabled,
                position_limit,option_count,beam_width,min(deadline,local_deadline),producer_stats)
        except BaseException:
            scope.update(group=None,relay=False,end=deadline,producer_stats=None)
            raise

    def source(p,evaluator,route,aircraft,start_visit,end_visit,*,candidate_limit,
               option_count=8,counts_toward_k=False,event_cap=60,surface=None):
        check();stats["source_requests"]+=1
        route=tuple(route)
        leg=(p.leg_registry or {}).get(abs(start_visit))
        g=scope["group"]
        eligible=bool(scope["relay"] and p is problem and g is not None and not counts_toward_k
            and start_visit>0 and end_visit==-start_visit and leg is not None and leg.kind=="RELAY_IN"
            and ((leg.task_id,aircraft) in ((g.i,g.a),(g.j,g.b)))
            and not any(abs(v) in (p.leg_registry or {}) for v in route)
            and leg.task_id not in {abs(v) for v in route})
        arguments=dict(candidate_limit=candidate_limit,option_count=option_count,
            counts_toward_k=counts_toward_k,event_cap=event_cap,surface=surface)
        if not eligible:
            stats["source_delegations"]+=1
            return original["route_leg_insertion_options"](p,evaluator,route,aircraft,start_visit,end_visit,**arguments)
        if evaluator.problem is not p:raise ValueError("source evaluator problem differs")
        canonical=evaluator.surface_cache.get(route,start_node=p.home_node(aircraft))
        if surface is not None:
            fields=("route","profile","visit_nodes","base_score","arrival","deliveries",
                    "delivery_positions","delivery_start_index","delivery_event_count")
            if any(getattr(surface,name)!=getattr(canonical,name) for name in fields):
                raise ValueError("source surface is not canonical for this route/home")
            if not set(surface.positions)<=set(canonical.positions):
                raise ValueError("source surface has capacity-infeasible positions")
        cache_key=(_Identity(p),_Identity(evaluator),route,aircraft,start_visit,end_visit,candidate_limit,option_count,
                   counts_toward_k,event_cap,weights,scales,raw_position_limit,
                   None if surface is None else _Identity(surface))
        hit,value=memo_get("source",cache_key)
        if hit:
            stats["source_memo_hits"]+=1;return value
        stats["source_actual_calls"]+=1
        before_delta=evaluator.counters.route_delta_evaluation_count
        native=original["route_leg_insertion_options"](p,evaluator,route,aircraft,start_visit,end_visit,**arguments)
        stats["source_native_delta_evaluations"]+=evaluator.counters.route_delta_evaluation_count-before_delta
        check()
        if not native:
            memo_put("source",cache_key,native);return native
        base_surface=surface if surface is not None else canonical
        if base_surface.route!=route:raise ValueError("source surface does not match route")
        positions=tuple(base_surface.positions)
        stats["capacity_positions_seen"]+=len(positions)
        start,end=p._node_for_visit(start_visit),p._node_for_visit(end_visit)
        candidates=[]
        for pickup,delivery in positions:
            check()
            times=_raw_times(p,base_surface,aircraft,start,end,pickup,delivery)
            distance=_node_distance_delta(p._distances,base_surface.visit_nodes,start,end,pickup,delivery,
                route_start_node=p.home_node(aircraft))
            candidates.append((distance,*times,pickup,delivery))
        if len(candidates)>raw_position_limit:
            stats["raw_surface_truncations"]+=1
            selected={(native[0].pickup_position,native[0].delivery_position)}
            streams=((sorted(candidates),raw_position_limit//2),
                (sorted(candidates,key=lambda x:(x[2],x[1],x[0],x[3],x[4])),raw_position_limit//4),
                (sorted(candidates,key=lambda x:(x[1],x[2],x[0],x[3],x[4])),raw_position_limit//4))
            for stream,limit in streams:
                added=0
                for item in stream:
                    if len(selected)>=raw_position_limit or added>=limit:break
                    pair=item[3],item[4]
                    if pair not in selected:selected.add(pair);added+=1
            for item in sorted(candidates):
                if len(selected)>=raw_position_limit:break
                selected.add((item[3],item[4]))
            positions=tuple(sorted(selected))
        limited=replace(base_surface,positions=positions,
            positions_by_early_delivery=tuple(sorted(positions,key=lambda x:(x[1],x[0]))))
        before_delta=evaluator.counters.route_delta_evaluation_count
        raw=original["route_leg_insertion_options"](p,evaluator,route,aircraft,start_visit,end_visit,
            candidate_limit=None,option_count=len(positions),counts_toward_k=counts_toward_k,
            event_cap=event_cap,surface=limited)
        stats["raw_positions_exactly_scored"]+=len(positions)
        stats["source_raw_delta_evaluations"]+=evaluator.counters.route_delta_evaluation_count-before_delta
        check()
        # Native options carry real deltas and native prefix clocks. Never
        # overwrite fields with a time reward or an optimistic geometry score.
        pool={option.route:option for option in (*native,*raw)}
        ordered=sorted(pool.values(),key=lambda o:(_key(o.delta,weights,scales),o.pickup_position,o.delivery_position,o.route))
        result=[native[0]]
        def add(option):
            if len(result)<option_count and all(option.route!=old.route for old in result):result.append(option)
        earliest_drop=min(raw,key=lambda o:(o.end_time_min,o.start_time_min,_key(o.delta,weights,scales),
            o.delivery_position,o.pickup_position,o.route))
        earliest_pickup=min(raw,key=lambda o:(o.start_time_min,o.end_time_min,_key(o.delta,weights,scales),
            o.pickup_position,o.delivery_position,o.route))
        add(earliest_drop);add(earliest_pickup)
        maximum=max(o.end_time_min for o in ordered)
        quantum=max(maximum/TIME_BINS,1e-9)
        family=lambda o:(min(TIME_BINS-1,int(o.end_time_min/quantum)),o.pickup_position//PICKUP_EVENT_BIN)
        used={family(o) for o in result}
        for option in ordered:
            if family(option) not in used:
                before=len(result);add(option)
                if len(result)>before:used.add(family(option))
        for option in ordered:add(option)
        value=tuple(result[:option_count])
        stats["native_physical_best_retained"]+=int(native[0] in value)
        stats["earliest_drop_retained"]+=int(earliest_drop in value)
        stats["earliest_pickup_retained"]+=int(earliest_pickup in value)
        if len(stats["source_records"])<24:
            stats["source_records"].append(dict(task_id=leg.task_id,aircraft=aircraft,station=leg.relay_id,
                native_best_drop_min=native[0].end_time_min,earliest_drop_min=earliest_drop.end_time_min,
                raw_positions=len(positions),capacity_positions=len(base_surface.positions),
                selected=[dict(pickup=o.pickup_position,drop=o.delivery_position,start_min=o.start_time_min,
                    end_min=o.end_time_min,physical_delta=[o.delta.late_count,o.delta.total_lateness_min,o.delta.distance_km]) for o in value]))
        memo_put("source",cache_key,value);return value

    def partial(p,routes,present,global_evaluator,producer_stats):
        check();stats["partial_requests"]+=1
        routes=tuple(tuple(r) for r in routes);present=frozenset(present)
        key=(_Identity(p),_Identity(global_evaluator),routes,present)
        hit,value=memo_get("partial",key)
        if hit:
            stats["partial_memo_hits"]+=1
            stats["partial_cached_rejections"]+=int(value is None)
            return value
        stats["partial_actual_calls"]+=1
        before=producer_stats.get("global_evaluations",0)
        value=original["_partial"](p,routes,present,global_evaluator,producer_stats)
        stats["partial_actual_global_calls"]+=producer_stats.get("global_evaluations",0)-before
        check();memo_put("partial",key,value);return value

    def receiver(p,routes,aircraft,selected,local,w,s,position_limit,option_count,local_deadline,producer_stats):
        check(local_deadline);stats["receiver_requests"]+=1
        routes=tuple(tuple(r) for r in routes)
        key=(_Identity(p),_Identity(local),tuple(sorted(set(routes))),aircraft,tuple(selected),tuple(w),tuple(s),
             position_limit,option_count,scope["relay"])
        hit,value=memo_get("receiver",key)
        if hit:
            stats["receiver_memo_hits"]+=1;return value
        stats["receiver_actual_calls"]+=1
        value=original["_rank_receiver"](p,routes,aircraft,selected,local,w,s,position_limit,
            option_count,min(deadline,scope["end"],local_deadline),producer_stats)
        check(local_deadline);memo_put("receiver",key,value);return value

    _active=True
    try:
        repool_module._rebuild=rebuild;repool_module.route_leg_insertion_options=source
        repool_module._partial=partial;repool_module._rank_receiver=receiver
        yield stats
    finally:
        for name,function in original.items():setattr(repool_module,name,function)
        for cache in caches.values():cache.clear()
        scope.update(group=None,relay=False,end=deadline,producer_stats=None)
        stats["patches_restored"]=True;_active=False
