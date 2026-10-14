"""Finite fixed-station, availability-aware single-route timing tables.

These are local transport records, never certificates of full coverage or
feasible fleet quotas. Preview K overflow must be requested explicitly. Source
IN drop clocks omit no native/compensation flight; receiver clocks include all
real waits. No optimizer or full-solution evaluator is imported.
"""
from __future__ import annotations

from itertools import permutations
from math import isfinite
from time import perf_counter

from uav_dispatch.model import Score
try:
    import six_task_route_costs as frozen
except ImportError:
    from . import six_task_route_costs as frozen

Deadline=frozen.Deadline
_Deadline=Deadline
LATENESS_EPSILON_MIN=1e-9
SHARED_DISTANCE_EPSILON_KM=1e-10
weighted_key=frozen.weighted_key
zero_load_gaps=frozen.zero_load_gaps


def _check(deadline):
    if not isfinite(deadline):raise ValueError('finite absolute deadline required')
    if perf_counter()>=deadline:raise Deadline


def _count(stats,name,n=1):
    if stats is not None:stats[name]=stats.get(name,0)+n


def _parameters(p,a,task_ids,w,s,deadline,limit):
    _check(deadline)
    tasks=tuple(task_ids)
    if (p.capacity!=2 or p.distance_matrix_km is not None or not isfinite(p.speed_km_per_min)
            or p.speed_km_per_min<=0 or type(a) is not int or not 0<=a<p.drone_count
            or not tasks or len(set(tasks))!=len(tasks)
            or any(type(t) is not int or t not in p._task_by_id for t in tasks)
            or type(limit) is not int or not 1<=limit<=4096):
        raise ValueError('Euclidean C2, distinct known tasks, valid aircraft and finite limit required')
    weighted_key(Score(0,0.,0.),w,s)
    return tasks


def _native(p,route,excluded,deadline):
    route=frozen._native(p,route,deadline=deadline)
    if any(abs(v) in excluded for v in route):raise ValueError('selected task remains in native skeleton')
    return route


def route_cost(p,route,a,*,availability=None,selected_pair=(),deadline,
               allow_final_k_overflow=False,statistics=None):
    """All native N/L/D and OUT waits, even for explicitly marked previews."""
    _check(deadline)
    if type(allow_final_k_overflow) is not bool:raise ValueError('boolean preview K flag required')
    if type(a) is not int or not 0<=a<p.drone_count:raise ValueError('valid aircraft required')
    if p.capacity!=2 or p.distance_matrix_km is not None or not isfinite(p.speed_km_per_min) or p.speed_km_per_min<=0:raise ValueError('Euclidean C2 positive-speed geometry required')
    route,before,after=frozen._structure(p,route,deadline=deadline)
    available={} if availability is None else dict(availability)
    if any(type(t) is not int or t not in p._task_by_id or type(v) is bool
           or not isfinite(v) or v<0 for t,v in available.items()):
        raise ValueError('finite nonnegative actual task availability required')
    pair=frozenset(selected_pair)
    if len(pair) not in (0,2):raise ValueError('shared flight requires two distinct legs/tasks')
    previous=p.home_points()[a];clock=distance=shared=0.;held=set()
    clocks=[];waits=[];deliveries={};drops={}
    for visit in route:
        _check(deadline)
        point=p.point_for_visit(visit);length=previous.distance_to(point)
        if not isfinite(length) or length<0:raise ValueError('finite flight geometry required')
        distance+=length;clock+=length/p.speed_km_per_min
        if pair and pair<=held:shared+=length
        leg=(p.leg_registry or {}).get(abs(visit));task=leg.task_id if leg else abs(visit);wait=0.
        if visit>0:
            if leg and leg.kind=='RELAY_OUT':
                if task not in available:raise ValueError('OUT pickup lacks source availability')
                wait=max(0.,available[task]-clock);clock+=wait
            held.add(visit)
        else:
            held.remove(-visit)
            if leg and leg.kind=='RELAY_IN':
                drops[task]=clock
                if task in available and abs(available[task]-clock)>1e-8:raise ValueError('own IN drop differs from supplied clock')
            else:deliveries[task]=clock
        clocks.append(clock);waits.append(wait);previous=point
    overflow=max(0,len(deliveries)-p.max_tasks_per_drone)
    if overflow and not allow_final_k_overflow:raise ValueError('local final delivery count exceeds K; preview flag required')
    late=[clock-p.task(t).deadline_min for t,clock in deliveries.items()
          if clock-p.task(t).deadline_min>LATENESS_EPSILON_MIN]
    _check(deadline);_count(statistics,'route_cost_evaluations')
    return dict(route=route,aircraft=a,score=Score(len(late),sum(late),distance),event_times=tuple(clocks),
        event_times_min=tuple(clocks),delivery_times=deliveries,delivery_times_min=deliveries,
        availability=drops,drop_times_min=drops,waiting_min=tuple(waits),total_waiting_min=sum(waits),
        load_before=before,load_after=after,shared_loaded_distance_km=shared,
        final_delivery_count=len(deliveries),final_k_limit=p.max_tasks_per_drone,
        final_k_overflow_count=overflow,preview_only=allow_final_k_overflow,
        scoring_scope='single_aircraft_transport_route',complete_solution=False)


def _key(record,w,s):return weighted_key(record['score'],w,s),record['route']


def _retained(records,limit,w,s,*,source=False):
    unique={r['route']:r for r in records};ordered=sorted(unique.values(),key=lambda r:_key(r,w,s))
    if not ordered:return ()
    result=[ordered[0]]
    special=(min(ordered,key=lambda r:(r['in_drop_time_min'],_key(r,w,s))) if source else
             min(ordered,key=lambda r:(r['score'].late_count,r['score'].total_lateness_min,
                                      r['score'].distance_km,r['route'])))
    if special['route']!=result[0]['route'] and len(result)<limit:result.append(special)
    if source and len(result)<limit:
        feasible=[r for r in ordered if any(r['in_drop_time_min']<=b for b in r.get('latest_drop_bounds',()))]
        if feasible:
            safe=min(feasible,key=lambda r:_key(r,w,s))
            if all(safe['route']!=x['route'] for x in result):result.append(safe)
    if not source and len(result)<limit:
        rescue=min(ordered,key=lambda r:(-len(r.get('retention_rescued_task_ids',r.get('selected_rescued_task_ids',[]))),
            len(r.get('retention_new_late_task_ids',r.get('selected_new_late_task_ids',[]))),_key(r,w,s)))
        if all(rescue['route']!=x['route'] for x in result):result.append(rescue)
    for r in ordered:
        if len(result)>=limit:break
        if all(r['route']!=x['route'] for x in result):result.append(r)
    return tuple(result)


def _single_layouts(p,route,a,identifier,position_limit,deadline,latest_drop_bounds=()):
    """All legal P/D positions; finite true-geometry/early-clock shortlist."""
    base=route_cost(p,route,a,deadline=deadline)
    points=[p.point_for_visit(v) for v in route];home=p.home_points()[a]
    first,last=p.point_for_visit(identifier),p.point_for_visit(-identifier)
    loads=(*base['load_before'],0);n=len(route);items=[]
    for pickup in range(n+1):
        _check(deadline)
        if loads[pickup]>=2:continue
        left=home if pickup==0 else points[pickup-1]
        clock=0. if pickup==0 else base['event_times'][pickup-1]
        start=clock+left.distance_to(first)/p.speed_km_per_min
        for drop in range(pickup,n+1):
            _check(deadline)
            if loads[drop]>=2:break
            if pickup==drop:
                delta=left.distance_to(first)+first.distance_to(last)
                if drop<n:delta+=last.distance_to(points[drop])-left.distance_to(points[drop])
                end=start+first.distance_to(last)/p.speed_km_per_min
            else:
                right=points[pickup]
                before=left.distance_to(first)+first.distance_to(right)-left.distance_to(right)
                end=(base['event_times'][drop-1]+before/p.speed_km_per_min
                     +points[drop-1].distance_to(last)/p.speed_km_per_min)
                delta=before+points[drop-1].distance_to(last)
                if drop<n:delta+=last.distance_to(points[drop])-points[drop-1].distance_to(points[drop])
            items.append((delta,end,start,pickup,drop))
    # All positions were tested geometrically/capacity-wise. Keep bounded real
    # clock representatives before the expensive all-native score replay.
    chosen=[];seen=set()
    streams=(sorted(items),sorted(items,key=lambda x:(x[1],x[0],x[2],x[3:])),
             sorted(items,key=lambda x:(x[2],x[1],x[0],x[3:])))
    def add(item):
        if len(chosen)<position_limit and item[3:] not in seen:
            chosen.append(item);seen.add(item[3:])
    for stream in streams:
        if stream:add(stream[0])
    bound_candidates=[]
    for bound in latest_drop_bounds:
        _check(deadline)
        feasible=[item for item in items if item[1]<=bound]
        best=min(feasible,key=lambda x:(x[0],x[1],x[3:])) if feasible else None
        latest=min(feasible,key=lambda x:(-x[1],x[0],x[3:])) if feasible else None
        if best is not None:add(best);add(latest)
        bound_candidates.append((bound,len(feasible),best,latest))
    for item in streams[0]:
        if len(chosen)>=position_limit:break
        add(item)
    coverage=tuple(dict(bound_min=bound,legal_positions_before_bound=count,
        best_geometry_retained=best is not None and best[3:] in seen,
        latest_safe_retained=latest is not None and latest[3:] in seen,
        omitted_due_position_limit=bool(best is not None and (best[3:] not in seen or latest[3:] not in seen)))
        for bound,count,best,latest in bound_candidates)
    result=[]
    for _,end,start,pickup,drop in chosen[:position_limit]:
        _check(deadline)
        candidate=route[:pickup]+(identifier,)+route[pickup:drop]+(-identifier,)+route[drop:]
        result.append((candidate,pickup,drop+1,start,end,coverage))
    return tuple(result)


def direct_single_options(p,native_route,a,task_id,*,deadline,weights,scales,
                          option_limit=2,position_limit=16,gap_limit=None,original_delivery_times=None,rescue_task_ids=None,statistics=None):
    _parameters(p,a,(task_id,),weights,scales,deadline,option_limit)
    if type(position_limit) is not int or not 1<=position_limit<=4096:raise ValueError('positive bounded position limit required')
    if gap_limit is not None and (type(gap_limit) is not int or gap_limit<1):raise ValueError('positive gap limit required')
    native=_native(p,native_route,{task_id},deadline);records=[]
    for candidate,pickup,drop,_,_,_ in _single_layouts(p,native,a,task_id,position_limit,deadline):
        r=route_cost(p,candidate,a,deadline=deadline,statistics=statistics)
        r.update(record_kind='DIRECT_single',task_ids=(task_id,),pickup_position=pickup,delivery_position=drop,requested_gap_limit=gap_limit,position_limit=position_limit)
        _rescue_evidence(p,r,(task_id,),original_delivery_times,rescue_task_ids=rescue_task_ids)
        records.append(r);_count(statistics,'direct_single_candidates')
    result=_retained(records,option_limit,weights,scales)
    _check(deadline)
    return result


def source_options(p,native_route,a,task_id,station_id,*,deadline,weights,scales,
                   compensation_task_id=None,optimistic_missing_comp=True,
                   option_limit=2,position_limit=16,latest_drop_bounds=(),statistics=None):
    _parameters(p,a,(task_id,),weights,scales,deadline,option_limit)
    if type(optimistic_missing_comp) is not bool:raise ValueError('boolean missing compensation flag required')
    if type(position_limit) is not int or not 1<=position_limit<=4096:raise ValueError('positive bounded position limit required')
    latest_drop_bounds=tuple(latest_drop_bounds)
    if len(latest_drop_bounds)>4 or any(type(b) is bool or not isfinite(b) for b in latest_drop_bounds):
        raise ValueError('at most four finite latest-drop bounds required')
    leg=frozen._leg(p,task_id,station_id,'RELAY_IN')
    native=_native(p,native_route,{task_id},deadline)
    if compensation_task_id is None:
        skeletons=(native,)
    else:
        if compensation_task_id==task_id:raise ValueError('source and compensation must differ')
        skeletons=tuple(r['route'] for r in direct_single_options(p,native,a,compensation_task_id,
            deadline=deadline,weights=weights,scales=scales,option_limit=option_limit,
            position_limit=position_limit,statistics=statistics))
        optimistic_missing_comp=False
    records=[]
    for skeleton in skeletons:
        layouts=_single_layouts(p,skeleton,a,leg,position_limit,deadline,latest_drop_bounds)
        if layouts and latest_drop_bounds and statistics is not None:
            coverage=layouts[0][-1]
            _count(statistics,'source_latest_bound_checks',len(coverage))
            _count(statistics,'source_latest_bound_position_omissions',sum(c['omitted_due_position_limit'] for c in coverage))
            name='source_preview_bound_position_examples' if optimistic_missing_comp else 'source_complete_bound_position_examples'
            examples=statistics.setdefault(name,[])
            if len(examples)<12:examples.append(dict(task_id=task_id,aircraft=a,station_id=station_id,
                compensation_task_id=compensation_task_id,optimistic_missing_comp=optimistic_missing_comp,
                position_limit=position_limit,coverage=coverage))
        for candidate,pickup,drop,_,_,coverage in layouts:
            r=route_cost(p,candidate,a,deadline=deadline,statistics=statistics)
            r.update(record_kind='source_IN',task_ids=(task_id,),source_task_id=task_id,station_id=station_id,
                pickup_position=pickup,delivery_position=drop,in_drop_position=drop,
                in_drop_time_min=r['availability'][task_id],compensation_task_id=compensation_task_id,
                optimistic_missing_comp=optimistic_missing_comp,
                native_complete=not optimistic_missing_comp,latest_drop_bounds=latest_drop_bounds,
                source_bound_position_coverage=coverage,position_limit=position_limit)
            records.append(r);_count(statistics,'source_candidates')
    result=_retained(records,option_limit,weights,scales,source=True)
    _check(deadline)
    return result


def _classifications(p,deliveries,original,sigma):
    if original is None:return [],[],[]
    official=[];damaged=[];safe=[]
    for task,clock in deliveries.items():
        previous=original.get(task,original.get(str(task)))
        if previous is None:continue
        if type(previous) is bool or not isfinite(previous) or previous<0:raise ValueError('finite original delivery clock required')
        due=p.task(task).deadline_min
        if previous-due>LATENESS_EPSILON_MIN:
            if clock-due<=LATENESS_EPSILON_MIN:official.append(task)
            if clock<=due+LATENESS_EPSILON_MIN-sigma:safe.append(task)
        elif clock-due>LATENESS_EPSILON_MIN:damaged.append(task)
    return sorted(official),sorted(damaged),sorted(safe)



def _rescue_evidence(p,record,tasks,original,sigma=0.,rescue_task_ids=None):
    recovered,damaged,safe=_classifications(p,record['delivery_times'],original,sigma)
    selected=set(tasks)
    mask=tuple(tasks) if rescue_task_ids is None else tuple(rescue_task_ids)
    if any(type(t) is not int or t not in p._task_by_id for t in mask):raise ValueError('known rescue-retention task IDs required')
    record.update(retention_rescue_task_ids=sorted(set(mask)),
        retention_rescued_task_ids=sorted(set(recovered)&set(mask)),
        retention_new_late_task_ids=sorted(set(damaged)&set(mask)),original_delivery_clock_evidence_supplied=original is not None,
        official_rescued_task_ids=recovered,official_new_late_task_ids=damaged,
        selected_rescued_task_ids=sorted(set(recovered)&selected),
        selected_new_late_task_ids=sorted(set(damaged)&selected),
        native_rescued_task_ids=sorted(set(recovered)-selected),
        native_new_late_task_ids=sorted(set(damaged)-selected),safe_rescued_task_ids=safe,
        selected_safe_rescued_task_ids=sorted(set(safe)&selected))


def _window(p,r,pair,station,available,gap,original,sigma,required,deadline):
    route=r['route'];point=next(s.point for s in p.relay_stations if s.id==station)
    previous=p.home_points()[r['aircraft']] if gap==0 else p.point_for_visit(route[gap-1])
    c=(0. if gap==0 else r['event_times'][gap-1])+previous.distance_to(point)/p.speed_km_per_min
    T=max(c,*(available[t] for t in pair))
    offsets={}
    for k,v in enumerate(route):
        _check(deadline)
        if k<=gap+1 or v>0:continue
        leg=(p.leg_registry or {}).get(-v);task=leg.task_id if leg else -v
        if leg is None or leg.kind=='RELAY_OUT':offsets[str(task)]=r['delivery_times'][task]-T
    official,damaged,safe=_classifications(p,r['delivery_times'],original,sigma)
    if required is None:
        # Default: try both selected deadlines and protect every old on-time
        # suffix delivery. A failed safety channel is still physically valid.
        targets=set(pair)
        if original is not None:
            for t in r['delivery_times']:
                _check(deadline)
                old=original.get(t,original.get(str(t)))
                if old is not None and old-p.task(t).deadline_min<=LATENESS_EPSILON_MIN and str(t) in offsets:targets.add(t)
    else:targets=set(required)
    if any(type(t) is not int or str(t) not in offsets for t in targets):raise ValueError('required on-time IDs must be suffix deliveries')
    limits={str(t):p.task(t).deadline_min+LATENESS_EPSILON_MIN-sigma-offsets[str(t)] for t in sorted(targets)}
    B=min(limits.values()) if limits else None
    return dict(station_id=station,task_ids=sorted(pair),take_order_task_ids=list(pair),
        source_availability_min={str(t):available[t] for t in sorted(pair)},receiver_arrival_min=c,
        second_take_min=T,selected_delivery_offsets_min={str(t):offsets[str(t)] for t in sorted(pair)},
        suffix_delivery_offsets_min=offsets,official_lateness_epsilon_min=LATENESS_EPSILON_MIN,
        sigma_min=sigma,required_on_time_task_ids=sorted(targets),latest_second_take_min_by_task=limits,
        joint_latest_second_take_min=B,safe_window_satisfied=(T<=B if B is not None else None),
        official_rescued_task_ids=official,official_new_late_task_ids=damaged,safe_rescued_task_ids=safe,
        selected_rescued_task_ids=sorted(set(official)&set(pair)),
        selected_new_late_task_ids=sorted(set(damaged)&set(pair)),
        native_rescued_task_ids=sorted(set(official)-set(pair)),native_new_late_task_ids=sorted(set(damaged)-set(pair)),
        selected_safe_rescued_task_ids=sorted(set(safe)&set(pair)),
        classification_scope='all_deliveries_on_this_receiver_route',
        failure_scope='declared_safety_channel_only_not_global_J_or_ALNS_bound')



def make_window(p,record,task_pair,station_id,availability,*,gap=None,
                original_delivery_times=None,sigma=0.,required_on_time_task_ids=None,deadline):
    """Bind a caller-specified actual OUT block to its source-aware window.

    Replays the current route rather than trusting supplied record clocks.
    Any K overflow remains an explicit local preview, never a fleet resource.
    """
    _check(deadline)
    pair=tuple(task_pair)
    if len(pair)!=2 or len(set(pair))!=2:raise ValueError('two distinct receiver tasks required')
    if type(sigma) is bool or not isfinite(sigma) or sigma<0:raise ValueError('finite nonnegative safety margin required')
    out=tuple(frozen._leg(p,t,station_id,'RELAY_OUT') for t in pair)
    route=tuple(record['route']);a=record['aircraft']
    gap=route.index(out[0]) if gap is None else gap
    if type(gap) is not int or route[gap:gap+2]!=out:raise ValueError('actual contiguous ordered OUT take block required')
    if any(abs(v) in (p.leg_registry or {}) and abs(v) not in out for v in route):
        raise ValueError('receiver window supports only its two OUT legs and native DIRECT')
    actual=route_cost(p,route,a,availability=availability,selected_pair=out,deadline=deadline,
        allow_final_k_overflow=bool(record.get('preview_only',False)))
    if actual['load_before'][gap:gap+2]!=(0,1):raise ValueError('receiver OUT take block must enter empty')
    if actual['shared_loaded_distance_km']<=SHARED_DISTANCE_EPSILON_KM:raise ValueError('positive shared flight required')
    result=_window(p,actual,pair,station_id,dict(availability),gap,original_delivery_times,
                   sigma,required_on_time_task_ids,deadline)
    _check(deadline)
    return result


window_for_record=make_window


def receiver_options(p,native_route,a,task_pair,station_id,availability,*,deadline,weights,scales,
                     option_limit=2,gap_limit=4,original_delivery_times=None,sigma=1e-8,
                     required_on_time_task_ids=None,allow_final_k_overflow=False,statistics=None):
    pair=_parameters(p,a,task_pair,weights,scales,deadline,option_limit)
    if len(pair)!=2:raise ValueError('two receiver tasks required')
    if type(gap_limit) is not int or gap_limit<1:raise ValueError('positive gap bound required')
    if type(sigma) is bool or not isfinite(sigma) or sigma<0:raise ValueError('nonnegative finite safety margin required')
    native=_native(p,native_route,set(pair),deadline)
    out=tuple(frozen._leg(p,t,station_id,'RELAY_OUT') for t in pair)
    available=dict(availability)
    if any(t not in available or type(available[t]) is bool or not isfinite(available[t]) or available[t]<0 for t in pair):
        raise ValueError('finite actual source availability for both tasks required')
    gaps=zero_load_gaps(p,native,deadline=deadline);records=[]
    # Intentional: every real empty gap is timed with both delivery orders.
    # gap_limit is a retained-frontier policy/reporting bound, not a geometry
    # prefilter which could lose the latest rescuable gap before timing it.
    for gap in gaps:
        for order in (out,tuple(reversed(out))):
            _check(deadline)
            block=out+tuple(-v for v in order);candidate=native[:gap]+block+native[gap:]
            r=route_cost(p,candidate,a,availability=available,selected_pair=out,deadline=deadline,
                allow_final_k_overflow=allow_final_k_overflow,statistics=statistics)
            _count(statistics,'receiver_candidates')
            if r['shared_loaded_distance_km']<=SHARED_DISTANCE_EPSILON_KM:
                _count(statistics,'receiver_zero_shared_rejections');continue
            r.update(record_kind='receiver_OUT',task_ids=pair,station_id=station_id,pickup_position=gap,
                outbound_take_positions=(gap,gap+1),delivery_order_task_ids=tuple(p.leg_registry[v].task_id for v in order),
                window=_window(p,r,pair,station_id,available,gap,original_delivery_times,sigma,required_on_time_task_ids,deadline))
            _rescue_evidence(p,r,pair,original_delivery_times,sigma)
            records.append(r)
    latest=max((r['pickup_position'] for r in records if r['window']['selected_safe_rescued_task_ids']),default=None)
    selected_rescue_count=sum(bool(r['selected_rescued_task_ids']) for r in records)
    chosen=_retained(records,option_limit,weights,scales)
    for r in chosen:r.update(receiver_empty_gaps_evaluated=len(gaps),receiver_layouts_evaluated=len(records),
        latest_safe_rescue_gap_evaluated=latest,
        receiver_any_selected_rescue_candidate=bool(selected_rescue_count),
        receiver_selected_rescue_layout_count=selected_rescue_count,gap_policy='all_empty_gaps_exactly_timed_before_weighted_and_minN_retention',
        requested_gap_limit=gap_limit)
    _check(deadline)
    return chosen


def direct_pair_options(p,native_route,a,task_pair,*,deadline,weights,scales,
                        option_limit=2,gap_limit=4,original_delivery_times=None,rescue_task_ids=None,statistics=None):
    pair=_parameters(p,a,task_pair,weights,scales,deadline,option_limit)
    if len(pair)!=2 or type(gap_limit) is not int or not 1<=gap_limit<=4096:raise ValueError('two tasks and positive bounded gaps required')
    native=_native(p,native_route,set(pair),deadline);records=[]
    for block in permutations((pair[0],-pair[0],pair[1],-pair[1])):
        if block.index(pair[0])>block.index(-pair[0]) or block.index(pair[1])>block.index(-pair[1]):continue
        gaps=zero_load_gaps(p,native,deadline=deadline)
        for gap in gaps:
            _check(deadline)
            r=route_cost(p,native[:gap]+block+native[gap:],a,selected_pair=pair,deadline=deadline,statistics=statistics)
            r.update(record_kind='DIRECT_pair',task_ids=pair,pickup_position=gap,
                empty_gaps_evaluated=len(gaps),gap_policy='all_empty_gaps_exactly_timed_before_retention',
                requested_gap_limit=gap_limit)
            _rescue_evidence(p,r,pair,original_delivery_times,rescue_task_ids=rescue_task_ids)
            records.append(r);_count(statistics,'direct_pair_candidates')
    result=_retained(records,option_limit,weights,scales)
    _check(deadline)
    return result
