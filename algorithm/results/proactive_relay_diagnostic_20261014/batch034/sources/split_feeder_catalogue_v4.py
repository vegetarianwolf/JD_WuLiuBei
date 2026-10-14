"""Two-gap IN and separated OUT deliveries before catalogue truncation.

Continuous and separated pickup/drop layouts are priced before source/feeder
caps. Stable middle+tail indices avoid subtracting large lateness totals.
Complete route witnesses and the caller canonical guard remain separate.
"""
from __future__ import annotations

from itertools import combinations,product
from dataclasses import replace
from math import isfinite,ulp
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import split_feeder_catalogue as old
    import exact_suffix_delay as suffix
except ImportError:
    from . import split_feeder_catalogue as old
    from . import exact_suffix_delay as suffix

costs=old.costs
Deadline=costs.Deadline
SCOPE=old.SCOPE
EARLY_SCOPE='complete_two_gap_IN_and_separated_OUT_delivery_model_before_base_pool'
_check=old._check
_count=old._count
_physical=old._physical
_digest=old._digest
_identity=old._identity


def _index(record,due,gap,deadline):
    try:return suffix.index_direct_suffix(record,due,gap,deadline=deadline)
    except suffix.Deadline:raise Deadline from None


def _native_nl_error(index,shift,extra_margin=0.):
    """Compact equivalent of the frozen primitive's guarded fast path.

    Return None to demand a complete physical route_cost replay. Rich query
    dictionaries are avoided in millions of repeated conditional prices.
    """
    if shift<0 or not isfinite(shift):return None,'geometry_or_nonfinite_shift',float('inf')
    xs=index.suffix.raw_lateness_min;n=len(xs);lo=0;hi=n;eps=index.epsilon_min
    while lo<hi:
        mid=(lo+hi)//2
        if xs[mid]+shift>eps:hi=mid
        else:lo=mid+1
    scale=index.maximum_clock_min+shift
    if not isfinite(scale):return None,'nonfinite_clock',float('inf')
    margin=1e-10+extra_margin+4*(ulp(index.maximum_clock_min)+ulp(index.maximum_due_min)+ulp(scale))
    if any(abs(xs[k]+shift-eps)<=margin for k in (lo-1,lo) if 0<=k<n):return None,'deadline_boundary',float('inf')
    count=n-lo;late=0.;error=0.
    if count:
        raw=index.suffix.suffix_sums_min[lo];term=count*shift
        magnitude=max(abs(xs[lo]),abs(xs[-1]))+shift
        if raw is None or not isfinite(term) or not isfinite(magnitude):return None,'sum_magnitude',float('inf')
        late=raw+term
        if not isfinite(late) or late<0:return None,'sum_nonfinite_or_cancellation',float('inf')
        error=index.suffix.suffix_sum_error_bounds_min[lo]+ulp(term)+ulp(late)+count*ulp(magnitude)
    total=index.prefix_lateness_min+late
    error+=ulp(total)+ulp(index.prefix_lateness_min)
    if not isfinite(total) or error>1e-10:return None,'sum_error',error
    return (index.prefix_late_count+count,total),None,error


def _native_nl(index,shift):
    value,reason,_=_native_nl_error(index,shift)
    return value,reason


def _middle_index(record,due,g,h,template,deadline):
    """Original prefix before g, then only deliveries in [g,h)."""
    _check(deadline)
    rows=tuple((abs(v),record['event_times_min'][k],due[abs(v)])
        for k,v in enumerate(record['route']) if v<0 and g<=k<h)
    try:values=suffix.build_lateness_index((clock-limit for _,clock,limit in rows),deadline=deadline)
    except suffix.Deadline:raise Deadline from None
    return replace(template,suffix=values,suffix_deliveries=rows,
        maximum_clock_min=max((x[1] for x in rows),default=0.),
        maximum_due_min=max((x[2] for x in rows),default=0.))


def _two_zone_nl(middle,tail,sP,s12,timing_error):
    first,reason,e1=_native_nl_error(middle,sP,timing_error)
    if first is None:return None,reason
    last,reason,e2=_native_nl_error(tail,s12,timing_error)
    if last is None:return None,reason
    total=first[1]+last[1]
    # The arithmetic model's sum error and the physical-clock reassociation
    # allowance are both charged, rather than granting each band a new budget.
    error=e1+e2+ulp(total)+(len(middle.suffix_deliveries)+len(tail.suffix_deliveries))*timing_error
    if not isfinite(total) or error>1e-10:return None,'two_zone_sum_error'
    return (first[0]+last[0],total),None


def _key(N,L,D,w,s):return (sum(a*b/c for a,b,c in zip(w,(N,L,D),s)),N,L,D)


def _late(clock,due):
    value=clock-due
    return value if value>1e-9 else 0.


def _nonnegative_detour(delta,*lengths):
    """Only a declared ulp-scale Euclidean roundoff correction is allowed."""
    if not isfinite(delta):raise ValueError('finite Euclidean detour required')
    if delta>=0:return delta,False
    tolerance=64*ulp(max(1.,abs(delta),*(abs(x) for x in lengths)))
    if delta < -tolerance:raise ValueError('negative Euclidean detour exceeds roundoff tolerance')
    return 0.,True


def _source_heads(records,limit,bound):
    if not records:return [],[]
    weighted=lambda r:(r['delta_key'],r['ready'],r['gap'])
    heads=[('weighted',min(records,key=weighted)),('earliest',min(records,key=lambda r:(r['ready'],weighted(r)))),
           ('minimum_native_N',min(records,key=lambda r:(r['delta_key'][1:],weighted(r))))]
    safe=[r for r in records if r['ready']<=bound]
    if safe:heads.append(('selected_deadline',min(safe,key=weighted)))
    chosen=[];omitted=[]
    for name,record in heads:
        if any((r['gap'],r['drop_gap'])==(record['gap'],record['drop_gap']) for r in chosen):continue
        if len(chosen)<limit:chosen.append(record)
        else:omitted.append(name)
    for record in sorted(records,key=weighted):
        if len(chosen)>=limit:break
        if all((r['gap'],r['drop_gap'])!=(record['gap'],record['drop_gap']) for r in chosen):chosen.append(record)
    return chosen,omitted


def _feeder_heads(profiles,limit):
    if not profiles:return [],[]
    weighted=lambda f:(f['best']['delta_key'],f['aircraft'])
    heads=[('weighted_delta',min(profiles,key=weighted)),
           ('earliest',min(profiles,key=lambda f:(min(r['ready'] for r in f['records']),weighted(f)))),
           ('minimum_native_delta_N',min(profiles,key=lambda f:(min(r['delta_key'][1:] for r in f['records']),weighted(f))))]
    safe=[f for f in profiles if any(r['ready']<=f['deadline_bound'] for r in f['records'])]
    if safe:heads.append(('selected_deadline',min(safe,key=lambda f:(min(r['delta_key'] for r in f['records'] if r['ready']<=f['deadline_bound']),f['aircraft']))))
    chosen=[];omitted=[]
    for name,value in heads:
        if any(f['aircraft']==value['aircraft'] for f in chosen):continue
        if len(chosen)<limit:chosen.append(value)
        else:omitted.append(name)
    for value in sorted(profiles,key=weighted):
        if len(chosen)>=limit:break
        if all(f['aircraft']!=value['aircraft'] for f in chosen):chosen.append(value)
    return chosen,omitted


def _best_three(values,key,pooled):
    if not values:return []
    heads=(min(values,key=key),min(values,key=lambda x:(key(x)[1:],key(x))),min(values,key=lambda x:(pooled(x),key(x))))
    result=[]
    for value in heads:
        if all(value is not prior for prior in result):result.append(value)
    return result


def _as_source(record,t,H,gap,drop_gap):
    result=dict(record)
    result.update(source_task_id=t,task_ids=(t,),station_id=H,pickup_position=gap,in_drop_position=drop_gap+1,
        in_drop_time_min=record['availability'][t],native_complete=True,optimistic_missing_comp=False,
        compensation_task_id=None,latest_drop_bounds=(),source_bound_position_coverage=(),
        retention_origin='early_exact_two_gap_seed',original_pickup_gap=gap,original_drop_gap=drop_gap)
    return result


def _source_json(record,t,a,H,w,s):
    result=old._source_json(record,t,a,H,w,s)
    g=record['pickup_position'];h=record['in_drop_position']-1
    result.update(original_pickup_gap=g,original_drop_gap=h,
        source_layout_type='contiguous' if g==h else 'noncontiguous')
    return result


def _source_positions(p,record,a,t,H,incoming,due,deadline,w,s,indices,bands,stats):
    """Every capacity-legal original g<=h, before either finite source cap."""
    route=record['route'];n=len(route);V=p.speed_km_per_min
    points=[p.point_for_visit(v) for v in route];home=p.home_points()[a]
    P=p.task(t).pickup;station=next(x.point for x in p.relay_stations if x.id==H)
    loads=(*record['load_before'],0);base=record['score'];positions=[]
    def index(k):
        if (a,k) not in indices:indices[a,k]=_index(record,due,k,deadline)
        return indices[a,k]
    for g in range(n+1):
        _check(deadline)
        if loads[g]>=2:continue
        u=home if g==0 else points[g-1]
        prefix_clock=0. if g==0 else record['event_times_min'][g-1]
        pickup_clock=prefix_clock+u.distance_to(P)/V
        # For h>g, only the pickup edge is replaced at g. The ongoing clock
        # traverses every retained native flight in its actual addition order.
        if g<n:
            first_detour=u.distance_to(P)+P.distance_to(points[g])-u.distance_to(points[g])
            first_detour,first_roundoff=_nonnegative_detour(first_detour,base.distance_km,u.distance_to(P),P.distance_to(points[g]))
        else:first_detour=0.;first_roundoff=False
        progressive=pickup_clock;previous=P
        for h in range(g,n+1):
            _check(deadline)
            if loads[h]>=2:break
            if h==g:
                path=u.distance_to(P)+P.distance_to(station);delta=path
                if h<n:delta+=station.distance_to(points[h])-u.distance_to(points[h])
                delta,roundoff=_nonnegative_detour(delta,path,base.distance_km)
                deltaP,deltaH=delta,0.;ready=pickup_clock+P.distance_to(station)/V
            else:
                progressive+=previous.distance_to(points[h-1])/V;previous=points[h-1]
                second_detour=previous.distance_to(station)
                if h<n:second_detour+=station.distance_to(points[h])-previous.distance_to(points[h])
                deltaH,second_roundoff=_nonnegative_detour(second_detour,base.distance_km,previous.distance_to(station))
                deltaP=first_detour;delta=deltaP+deltaH;roundoff=first_roundoff or second_roundoff
                ready=progressive+previous.distance_to(station)/V
            if roundoff:_count(stats,'geometry_roundoff_corrections')
            sP=deltaP/V;s12=delta/V
            scale=max(1.,base.distance_km/V+s12,abs(ready),index(g).maximum_due_min)
            # Positive flight sums on both original/inserted paths, detour
            # subtraction/division and reassociation: intentionally conservative
            # ulp allowance, charged to both boundary and summed-L guards.
            timing_error=(2*n+16)*ulp(scale)+8*(ulp(sP)+ulp(s12))
            if roundoff:nl,reason=None,'geometry_roundoff'
            elif h==g:
                nl,reason,error=_native_nl_error(index(g),s12,timing_error)
                if nl is not None and error+len(index(g).suffix_deliveries)*timing_error>1e-10:
                    nl,reason=None,'physical_clock_sum_error'
            else:
                if (a,g,h) not in bands:
                    middle=_middle_index(record,due,g,h,index(g),deadline)
                    tail=replace(index(h),prefix_late_count=0,prefix_lateness_min=0.)
                    bands[a,g,h]=(middle,tail)
                nl,reason=_two_zone_nl(*bands[a,g,h],sP,s12,timing_error)
                _count(stats,'source_two_zone_native_queries',2)
            _count(stats,'source_native_price_queries')
            _count(stats,'source_contiguous_positions_priced' if h==g else 'source_noncontiguous_positions_priced')
            distance=base.distance_km+delta
            if nl is None:
                candidate=route[:g]+(incoming,)+route[g:h]+(-incoming,)+route[h:]
                actual=costs.route_cost(p,candidate,a,deadline=deadline)
                nl=(actual['score'].late_count,actual['score'].total_lateness_min)
                distance=actual['score'].distance_km;ready=actual['availability'][t]
                _count(stats,'source_native_float_route_replays');_count(stats,'float_replay_'+reason)
            delta_key=_key(nl[0]-base.late_count,nl[1]-base.total_lateness_min,distance-base.distance_km,w,s)
            positions.append(dict(gap=g,drop_gap=h,ready=ready,deltaD=delta,deltaP=deltaP,deltaH=deltaH,
                shift=s12,middle_shift=sP,N=nl[0],L=nl[1],D=distance,delta_key=delta_key,
                replay=reason is not None,geometry_roundoff=roundoff,timing_error_bound_min=timing_error))
    return positions


def _receiver_rows(p,record,a,pair,H,deadline):
    """All empty-g take heads and capacity-legal second-delivery gaps h."""
    route=record['route'];n=len(route);V=p.speed_km_per_min
    points=[p.point_for_visit(v) for v in route];home=p.home_points()[a]
    station=next(x.point for x in p.relay_stations if x.id==H)
    loads=(*record['load_before'],0);rows=[]
    for g in range(n+1):
        _check(deadline)
        if loads[g]!=0:continue
        u=home if g==0 else points[g-1]
        arrival=(0. if g==0 else record['event_times_min'][g-1])+u.distance_to(station)/V
        for first,last in (pair,pair[::-1]):
            Df=p.task(first).delivery;Dl=p.task(last).delivery
            shared=station.distance_to(Df)
            if shared<=1e-10:continue
            if g<n:
                raw=u.distance_to(station)+shared+Df.distance_to(points[g])-u.distance_to(points[g])
                separated_head,head_roundoff=_nonnegative_detour(raw,record['score'].distance_km,shared)
            else:separated_head=0.;head_roundoff=False
            previous=Df;steps=[]
            for h in range(g,n+1):
                _check(deadline)
                if loads[h]>1:break
                if h==g:
                    head=u.distance_to(station)+shared+Df.distance_to(Dl)
                    if g<n:head+=Dl.distance_to(points[g])-u.distance_to(points[g])
                    head,roundoff=_nonnegative_detour(head,record['score'].distance_km,shared)
                    tail=0.;flights=(Df.distance_to(Dl)/V,)
                else:
                    steps.append(previous.distance_to(points[h-1])/V);previous=points[h-1]
                    raw=previous.distance_to(Dl)
                    if h<n:raw+=Dl.distance_to(points[h])-previous.distance_to(points[h])
                    tail,tail_roundoff=_nonnegative_detour(raw,record['score'].distance_km)
                    head=separated_head;roundoff=head_roundoff or tail_roundoff
                    flights=(*steps,previous.distance_to(Dl)/V)
                rows.append((g,h,arrival,first,last,shared/V,flights,head,tail,roundoff))
    return rows


def _receiver_route(skeleton,out,pair,row):
    g,h,_,first,last,*_=row
    by_task=dict(zip(pair,out))
    return skeleton[:g]+out+(-by_task[first],)+skeleton[g:h]+(-by_task[last],)+skeleton[h:]


def _receiver_prices(p,record,a,pair,H,rows,due,original,w,s,deadline,stats,*,fill_limit=0):
    """Return one source-aware priced frontier query; bounded local caches."""
    skeleton=record['route'];n=len(skeleton);V=p.speed_km_per_min
    out=tuple(costs.frozen._leg(p,t,H,'RELAY_OUT') for t in pair)
    indices={};bands={};ready_cache={};pair_cache={};shift_cache={}
    def index(g):
        if g not in indices:indices[g]=_index(record,due,g,deadline)
        return indices[g]
    def query(left,right):
        _check(deadline);available={pair[0]:left['ready'],pair[1]:right['ready']}
        ready=max(available.values());actual_pair=tuple(available.values())
        _count(stats,'receiver_ready_logical_requests')
        if actual_pair in pair_cache:
            _count(stats,'receiver_actual_pair_cache_hits');return pair_cache[actual_pair]
        if ready in ready_cache:
            _count(stats,'receiver_ready_cache_hits');return ready_cache[ready]
        weighted=minimum=pooled=None;any_replay=False;top=[]
        for number,row in enumerate(rows):
            _check(deadline)
            g,h,arrival,first,last,off1,flights,head,tail,roundoff=row
            # Match actual two successive OUT pickups, including the first
            # load1 wait. W enters each total native delay exactly once.
            take=arrival;wait=0.
            for task in pair:
                waiting=max(0.,available[task]-take);take+=waiting;wait+=waiting
            s1=head/V+wait;s2=(head+tail)/V+wait
            scale=max(1.,record['score'].distance_km/V+s2,abs(take),index(g).maximum_due_min)
            timing_error=(2*n+16)*ulp(scale)+8*(ulp(s1)+ulp(s2))
            cache_key=(number,s1,s2)
            _count(stats,'receiver_all_gap_order_conditional_prices')
            _count(stats,'receiver_contiguous_layout_prices' if g==h else 'receiver_separated_layout_prices')
            if cache_key in shift_cache:
                N,L,D=shift_cache[cache_key];replay=False;_count(stats,'receiver_native_shift_cache_hits')
            else:
                _count(stats,'receiver_native_price_queries')
                if roundoff:nl,reason=None,'geometry_roundoff'
                elif g==h:
                    nl,reason,error=_native_nl_error(index(g),s2,timing_error)
                    if nl is not None and error+len(index(g).suffix_deliveries)*timing_error>1e-10:
                        nl,reason=None,'physical_clock_sum_error'
                else:
                    if (g,h) not in bands:
                        bands[g,h]=(_middle_index(record,due,g,h,index(g),deadline),
                            replace(index(h),prefix_late_count=0,prefix_lateness_min=0.))
                    nl,reason=_two_zone_nl(*bands[g,h],s1,s2,timing_error)
                replay=reason is not None;D=record['score'].distance_km+head+tail
                if nl is None:
                    actual=costs.route_cost(p,_receiver_route(skeleton,out,pair,row),a,
                        availability=available,selected_pair=out,deadline=deadline)
                    late=[_late(clock,due[t]) for t,clock in actual['delivery_times_min'].items() if t not in pair]
                    nl=(sum(v>0 for v in late),sum(late));D=actual['score'].distance_km
                    _count(stats,'receiver_native_float_route_replays');_count(stats,'float_replay_'+reason)
                N,L=nl
                if not replay:shift_cache[cache_key]=(N,L,D)
            d1=take+off1;d2=d1
            for flight in flights:d2+=flight
            margin=1e-10+timing_error+4*(ulp(take)+ulp(d1)+ulp(d2)+ulp(due[first])+ulp(due[last]))
            if any(abs(clock-due[t]-1e-9)<=margin for t,clock in ((first,d1),(last,d2))):
                actual=costs.route_cost(p,_receiver_route(skeleton,out,pair,row),a,
                    availability=available,selected_pair=out,deadline=deadline)
                late=[_late(clock,due[t]) for t,clock in actual['delivery_times_min'].items() if t not in pair]
                N,L,D=sum(v>0 for v in late),sum(late),actual['score'].distance_km
                d1,d2=actual['delivery_times_min'][first],actual['delivery_times_min'][last]
                replay=True;_count(stats,'receiver_selected_boundary_route_replays')
            any_replay=any_replay or replay
            l1,l2=_late(d1,due[first]),_late(d2,due[last])
            key=_key(N+(l1>0)+(l2>0),L+l1+l2,D,w,s)
            rescued=tuple(t for t,clock in ((first,d1),(last,d2)) if _late(original[t],due[t]) and not _late(clock,due[t]))
            lost=tuple(t for t,clock in ((first,d1),(last,d2)) if not _late(original[t],due[t]) and _late(clock,due[t]))
            kr=(key,number);nr=(key[1:],key,number);pr=(-len(rescued),len(lost),key,number)
            extra=bool(fill_limit and (len(top)<fill_limit or kr<top[-1][0]))
            if weighted is None or kr<weighted[0] or nr<minimum[0] or pr<pooled[0] or extra:
                value=dict(row=row,T=take,wait=wait,middle_shift=s1,shift=s2,native_score=(N,L,D),
                    key=key,rescued=rescued,lost=lost,replay=replay,timing_error_bound_min=timing_error)
                if weighted is None or kr<weighted[0]:weighted=(kr,value)
                if minimum is None or nr<minimum[0]:minimum=(nr,value)
                if pooled is None or pr<pooled[0]:pooled=(pr,value)
                if extra:top=sorted((*top,(kr,value)),key=lambda x:x[0])[:fill_limit]
        chosen=[]
        for item in (weighted,minimum,pooled):
            if item is not None and all(item[1]['row']!=r['row'] for r in chosen):chosen.append(item[1])
        for item in top:
            if len(chosen)>=fill_limit:break
            if all(item[1]['row']!=r['row'] for r in chosen):chosen.append(item[1])
        if any_replay:
            pair_cache[actual_pair]=chosen;_count(stats,'receiver_boundary_pair_cache_entries')
        else:ready_cache[ready]=chosen
        return chosen
    return query


def _witness(p,records,pair,H,available,original,owners,w,s,deadline):
    deliveries={t:v for r in records for t,v in r['delivery_times_min'].items()}
    score=Score(sum(r['score'].late_count for r in records),sum(r['score'].total_lateness_min for r in records),sum(r['score'].distance_km for r in records))
    c=owners[pair[0]];out=tuple(costs.frozen._leg(p,t,H,'RELAY_OUT') for t in pair)
    receiver=records[c]
    window=costs.make_window(p,receiver,pair,H,available,original_delivery_times=original,sigma=0.,deadline=deadline)
    return dict(routes=[list(r['route']) for r in records],table_score=_physical(score),key=list(costs.weighted_key(score,w,s)),
        event_times_min=[list(r['event_times_min']) for r in records],delivery_times_min={str(t):v for t,v in sorted(deliveries.items())},
        source_drop_availability_min={str(t):v for t,v in available.items()},shared_loaded_distance_km=float(receiver['shared_loaded_distance_km']),
        actual_final_owners={str(t):a for t,a in sorted(owners.items())},window=window,complete_RELAY_model=True,
        canonical_full_checked=False,scoring_scope=SCOPE,
        pooled_rescued_task_ids=sorted(t for t in pair if _late(original[t],p.task(t).deadline_min) and not _late(deliveries[t],p.task(t).deadline_min)),
        pooled_newly_late_task_ids=sorted(t for t in pair if not _late(original[t],p.task(t).deadline_min) and _late(deliveries[t],p.task(t).deadline_min)))


def _build(p,routes,deadline,w,s,seed,max_families,base_pool_limit,source_position_limit,source_option_limit,
           table_option_limit,feeder_limit,sigma,stats):
    _check(deadline);costs.weighted_key(Score(0,0.,0.),w,s)
    limits=((max_families,128),(base_pool_limit,4096),(source_position_limit,512),(source_option_limit,3),(table_option_limit,3),(feeder_limit,64))
    if (p.drone_count<3 or p.capacity!=2 or p.distance_matrix_km is not None or any(type(n) is not int or not 1<=n<=cap for n,cap in limits)
        or base_pool_limit<max_families or type(seed) is not int or type(sigma) is bool or not isfinite(sigma) or sigma!=0.):
        raise ValueError('bounded Euclidean C2 exact native catalogue with sigma0 required')
    base=tuple(tuple(r) for r in routes)
    if len(base)!=p.drone_count:raise ValueError('one full source route per aircraft required')
    full=evaluate_solution(p,base,_force_full=True);index=build_plan_index(p,base);_check(deadline)
    if not full.valid or index.relay_task_ids:raise ValueError('real complete pure DIRECT input required')
    native=[costs.route_cost(p,r,a,deadline=deadline) for a,r in enumerate(base)]
    owners=index.delivery_route_by_task;by_owner={a:sorted(t for t,v in owners.items() if v==a) for a in range(p.drone_count)}
    original={t:v for r in native for t,v in r['delivery_times_min'].items()};due={t:p.task(t).deadline_min for t in p.task_ids}
    baseline=(sum(r['score'].late_count for r in native),sum(r['score'].total_lateness_min for r in native),sum(r['score'].distance_km for r in native))
    homes=p.home_points();stations={h.id:h.point for h in p.relay_stations};legs=old._station_legs(p,deadline)
    source_indices={};source_bands={};profiles={}
    for (t,H) in sorted(legs):
        all_profiles=[];P=p.task(t).pickup;point=stations[H]
        bound=due[t]+1e-9-point.distance_to(p.task(t).delivery)/p.speed_km_per_min
        for a in range(p.drone_count):
            if a==owners[t] or not by_owner[a]:continue
            _check(deadline);_count(stats,'source_profiles_priced_before_feeder_limit')
            positions=_source_positions(p,native[a],a,t,H,legs[t,H][0],due,deadline,w,s,source_indices,source_bands,stats)
            chosen,omitted=_source_heads(positions,source_option_limit,bound)
            if not chosen:continue
            all_profiles.append(dict(aircraft=a,records=chosen,best=min(positions,key=lambda r:(r['delta_key'],r['gap'])),deadline_bound=bound))
            _count(stats,'source_positions_priced_before_option_limit',len(positions));_count(stats,'source_positions_not_retained',len(positions)-len(chosen))
            _count(stats,'source_position_channel_omissions',len(omitted))
        retained,omitted=_feeder_heads(all_profiles,feeder_limit);profiles[t,H]=retained
        _count(stats,'feeder_profiles_retained',len(retained));_count(stats,'feeder_profiles_omitted',len(all_profiles)-len(retained))
        _count(stats,'feeder_channel_omissions',len(omitted))
    families=[]
    for c in range(p.drone_count):
        for i,j in combinations(by_owner[c],2):
            _check(deadline);_count(stats,'same_owner_task_pairs_considered')
            skeleton=tuple(v for v in base[c] if abs(v) not in (i,j));empty=costs.route_cost(p,skeleton,c,deadline=deadline)
            for H,point in sorted(stations.items()):
                if (i,H) not in profiles or (j,H) not in profiles:continue
                rows=_receiver_rows(p,empty,c,(i,j),H,deadline)
                receivers=_receiver_prices(p,empty,c,(i,j),H,rows,due,original,w,s,deadline,stats)
                for pa,pb in product(profiles[i,H],profiles[j,H]):
                    a,b=pa['aircraft'],pb['aircraft']
                    if a==b:continue
                    weighted=Nhead=pool=None
                    for left,right in product(pa['records'],pb['records']):
                        _check(deadline)
                        for receiver in receivers(left,right):
                            C=receiver['key'];N=baseline[0]+left['N']-native[a]['score'].late_count+right['N']-native[b]['score'].late_count+C[1]-native[c]['score'].late_count
                            L=baseline[1]+left['L']-native[a]['score'].total_lateness_min+right['L']-native[b]['score'].total_lateness_min+C[2]-native[c]['score'].total_lateness_min
                            D=baseline[2]+left['D']-native[a]['score'].distance_km+right['D']-native[b]['score'].distance_km+C[3]-native[c]['score'].distance_km
                            key=_key(N,L,D,w,s);tie=(left['gap'],left['drop_gap'],right['gap'],right['drop_gap'],receiver['row'][0],receiver['row'][1],receiver['row'][3])
                            kr=(key,tie);nr=(key[1:],key,tie);pr=(-len(receiver['rescued']),len(receiver['lost']),key,tie)
                            if weighted is None or kr<weighted[0] or nr<Nhead[0] or pr<pool[0]:
                                value=dict(left=left,right=right,receiver=receiver,key=key)
                                if weighted is None or kr<weighted[0]:weighted=(kr,value)
                                if Nhead is None or nr<Nhead[0]:Nhead=(nr,value)
                                if pool is None or pr<pool[0]:pool=(pr,value)
                            _count(stats,'complete_early_fleet_prices_ranked')
                    if weighted is not None:
                        families.append(dict(i=i,j=j,a=a,b=b,c=c,station_id=H,priority=list(weighted[1]['key']),
                            minimum_N_key=list(Nhead[1]['key']),pooled_rescue_count=len(pool[1]['receiver']['rescued']),
                            _weighted=weighted[1],_rescue=pool[1]))
                        _count(stats,'base_families_exactly_priced_before_pool')
    pooled=old._station_pool(families,base_pool_limit,exact=True)
    stats.update(base_families_retained=len(pooled),base_families_not_general_full_scored=len(families)-len(pooled),
        available_early_stations=sorted({f['station_id'] for f in families}),retained_early_stations=sorted({f['station_id'] for f in pooled}))
    general_cache={};general_receiver_cache={};seed_cache={};scored=[]
    def seed_source(t,a,H,gap,drop_gap):
        key=(t,a,H,gap,drop_gap)
        if key not in seed_cache:
            incoming=legs[t,H][0];route=base[a][:gap]+(incoming,)+base[a][gap:drop_gap]+(-incoming,)+base[a][drop_gap:]
            seed_cache[key]=_as_source(costs.route_cost(p,route,a,deadline=deadline),t,H,gap,drop_gap)
        return seed_cache[key]
    for family in pooled:
        _check(deadline);i,j,a,b,c,H=(family[k] for k in ('i','j','a','b','c','station_id'));pair=(i,j)
        skeleton=tuple(v for v in base[c] if abs(v) not in pair);early=[];required={a:[],b:[]}
        for channel,priced in (('weighted',family['_weighted']),('pooled',family['_rescue'])):
            left=seed_source(i,a,H,priced['left']['gap'],priced['left']['drop_gap'])
            right=seed_source(j,b,H,priced['right']['gap'],priced['right']['drop_gap'])
            available={i:left['in_drop_time_min'],j:right['in_drop_time_min']};row=priced['receiver']['row']
            gap,delivery_gap,arrival,first,last,off1,flights,head,tail,roundoff=row
            out=(legs[i,H][1],legs[j,H][1]);route=_receiver_route(skeleton,out,pair,row)
            receiver=costs.route_cost(p,route,c,availability=available,selected_pair=out,deadline=deadline)
            current=list(native);current[a]=left;current[b]=right;current[c]=receiver
            witness=_witness(p,current,pair,H,available,original,owners,w,s,deadline)
            if witness['table_score']['late_count']!=priced['key'][1] or any(abs(x-y)>1e-8 for x,y in zip(witness['key'][2:],priced['key'][2:])):
                raise ValueError('early conditional price differs from actual complete two-gap model')
            native_late=[_late(clock,due[t]) for t,clock in receiver['delivery_times_min'].items() if t not in pair]
            T=receiver['event_times_min'][gap+1]
            evidence=dict(source_layouts=[dict(task_id=t,aircraft=owner,gap=price['gap'],drop_gap=price['drop_gap'],
                original_pickup_gap=price['gap'],original_drop_gap=price['drop_gap'],
                pickup_position=src['pickup_position'],in_drop_position=src['in_drop_position'],
                layout_type='contiguous' if price['gap']==price['drop_gap'] else 'noncontiguous',
                pickup_detour_distance_km=price['deltaP'],drop_detour_distance_km=price['deltaH'],
                middle_native_shift_min=price['middle_shift'],tail_native_shift_min=price['shift'],
                timing_error_bound_min=price['timing_error_bound_min'],
                in_drop_time_min=src['in_drop_time_min'],delta_distance_km=price['deltaD'],
                native_shift_min=price['shift'],native_score=_physical(src['score']),float_route_replay_used=bool(price['replay']),
                geometry_roundoff_corrected=bool(price['geometry_roundoff']))
                for t,owner,src,price in ((i,a,left,priced['left']),(j,b,right,priced['right']))],
                receiver_layout=dict(gap=gap,delivery_gap=delivery_gap,original_pickup_gap=gap,original_delivery_gap=delivery_gap,
                    layout_type='contiguous' if gap==delivery_gap else 'separated',
                    take_positions=[gap,gap+1],delivery_positions=[gap+2,delivery_gap+3],
                    delivery_order_task_ids=[first,last],receiver_arrival_min=arrival,second_take_min=T,
                    waiting_min=T-arrival,delta_distance_km=head+tail,
                    head_detour_distance_km=head,tail_detour_distance_km=tail,
                    middle_native_shift_min=priced['receiver']['middle_shift'],tail_native_shift_min=priced['receiver']['shift'],
                    timing_error_bound_min=priced['receiver']['timing_error_bound_min'],
                    native_shift_min=priced['receiver']['shift'],native_score=dict(late_count=sum(v>0 for v in native_late),
                        total_lateness_min=float(sum(native_late)),distance_km=float(receiver['score'].distance_km)),
                    float_route_replay_used=bool(priced['receiver']['replay']),geometry_roundoff_corrected=bool(roundoff)),
                full_score=witness['table_score'],key=witness['key'],selected_delivery_times_min={str(t):witness['delivery_times_min'][str(t)] for t in pair},
                pooled_rescued_task_ids=witness['pooled_rescued_task_ids'],pooled_newly_late_task_ids=witness['pooled_newly_late_task_ids'],
                priced_seed_routes=witness['routes'],pricing_scope=EARLY_SCOPE)
            early.append((channel,evidence,witness))
            for owner,src in ((a,left),(b,right)):
                if all(src['route']!=r['route'] for r in required[owner]):required[owner].append(src)
        surfaces={}
        for t,owner in ((i,a),(j,b)):
            cache_key=(t,owner,H)
            if cache_key not in general_cache:
                bound=due[t]+1e-9-stations[H].distance_to(p.task(t).delivery)/p.speed_km_per_min
                general_cache[cache_key]=costs.source_options(p,base[owner],owner,t,H,deadline=deadline,weights=w,scales=s,
                    optimistic_missing_comp=False,option_limit=source_option_limit,position_limit=source_position_limit,
                    latest_drop_bounds=(bound,),statistics=stats.setdefault('route_cost_statistics',{}))
            chosen=required[owner][:source_option_limit]
            for src in sorted(general_cache[cache_key],key=lambda r:(costs.weighted_key(r['score'],w,s),r['route'])):
                if len(chosen)>=source_option_limit:break
                if all(src['route']!=r['route'] for r in chosen):chosen.append(src)
            surfaces[owner]=chosen
        candidates=[x[2] for x in early if all(any(tuple(x[2]['routes'][owner])==r['route'] for r in surfaces[owner]) for owner in (a,b))]
        receiver_key=(i,j,c,H)
        if receiver_key not in general_receiver_cache:
            native_c=costs.route_cost(p,skeleton,c,deadline=deadline)
            rows=_receiver_rows(p,native_c,c,pair,H,deadline)
            general_receiver_cache[receiver_key]=_receiver_prices(p,native_c,c,pair,H,rows,due,original,w,s,deadline,stats,
                fill_limit=table_option_limit)
        general_receiver=general_receiver_cache[receiver_key]
        out=(legs[i,H][1],legs[j,H][1])
        for left,right in product(surfaces[a],surfaces[b]):
            available={i:left['in_drop_time_min'],j:right['in_drop_time_min']}
            priced_receivers=general_receiver(dict(ready=available[i]),dict(ready=available[j]))
            for price in priced_receivers[:table_option_limit]:
                receiver=costs.route_cost(p,_receiver_route(skeleton,out,pair,price['row']),c,
                    availability=available,selected_pair=out,deadline=deadline,statistics=stats.setdefault('route_cost_statistics',{}))
                current=list(native);current[a]=left;current[b]=right;current[c]=receiver
                candidates.append(_witness(p,current,pair,H,available,original,owners,w,s,deadline));_count(stats,'general_complete_witnesses_scored')
        retained=old._retain(candidates,table_option_limit,w,s);seed_retention=[]
        for channel,evidence,witness in early:
            source_kept=all(any(tuple(witness['routes'][owner])==r['route'] for r in surfaces[owner]) for owner in (a,b))
            kept=any(witness['routes']==r['routes'] for r in retained)
            seed_retention.append(dict(channel=channel,source_seed_routes_retained=source_kept,
                early_price_model_replayed=True,early_seed_full_scored=source_kept,early_seed_entered_candidate_pool=source_kept,
                receiver_seed_layout_full_scored=source_kept,
                witness_seed_route_retained=kept,retained_or_omitted_by_better_complete_channels=(
                    'retained_exact_layout' if kept else 'true_weighted_minN_pooled_representatives' if source_kept else 'source_cap_below_two_early_layouts'),
                early_key=witness['key'],best_retained_key=min(r['key'] for r in retained)))
        keys=['%d:%d:%d'%(i,a,H),'%d:%d:%d'%(j,b,H)]
        tables={key:[_source_json(r,t,owner,H,w,s) for r in surfaces[owner]] for key,t,owner in zip(keys,pair,(a,b))}
        best=min(retained,key=lambda x:(tuple(x['key']),x['routes']))
        scored.append(dict(i=i,j=j,a=a,b=b,c=c,station_id=H,pooled_task_ids=[i,j],source_table_keys=keys,source_tables=tables,
            relay_witnesses=retained,priority=best['key'],minimum_N_key=min((r['key'] for r in retained),key=lambda k:(k[1:],k[0])),
            pooled_rescue_count=max(len(r['pooled_rescued_task_ids']) for r in retained),ranking_scope=SCOPE,
            early_priority=early[0][1]['key'],early_pricing_evidence=early[0][1],early_rescue_pricing_evidence=early[1][1],
            seed_retention=seed_retention,source_surface_scope='family_specific_cap_including_required_early_sources'))
    final=old._station_pool(scored,max_families,exact=True);donors={}
    def donor(a):
        if a not in donors:
            values=[]
            for t in by_owner[a]:
                _check(deadline);record=costs.route_cost(p,tuple(v for v in base[a] if abs(v)!=t),a,deadline=deadline)
                before=native[a]['score'];after=record['score'];key=_key(after.late_count-before.late_count,after.total_lateness_min-before.total_lateness_min,after.distance_km-before.distance_km,w,s)
                values.append((key,t,record))
            key,t,after=min(values,key=lambda x:(x[0],x[1]));donors[a]=dict(task_id=t,aircraft=a,candidate_count=len(values),
                baseline_score=_physical(native[a]['score']),deleted_route=list(after['route']),deletion_score=_physical(after['score']),deletion_delta_key=list(key),
                policy='best_exact_single_whole_DIRECT_deletion_weighted_credit; DIRECT-only donor; unchanged in RELAY')
        return donors[a]
    exported={}
    for family in final:
        _check(deadline);one,two=donor(family['a']),donor(family['b']);family.update(k1=one['task_id'],k2=two['task_id'],donor_evidence=[one,two])
        family['task_ids']=sorted((family['i'],family['j'],family['k1'],family['k2']))
        for key,records in family['source_tables'].items():
            union=exported.setdefault(key,[])
            for record in records:
                if all(record['route']!=r['route'] for r in union):union.append(record)
    final.sort(key=lambda f:(tuple(f['priority']),_identity(f)))
    configuration=dict(max_families=max_families,base_pool_limit=base_pool_limit,source_position_limit=source_position_limit,
        source_option_limit=source_option_limit,table_option_limit=table_option_limit,feeder_limit=feeder_limit,sigma=sigma,
        early_pricing='all_capacity_legal_g_le_h_IN_native_delta_before_source_and_feeder; all_C_empty_g_capacity_legal_delivery_h_both_orders_before_base_pool',
        early_source_channels=['weighted_native_delta','earliest','minimum_native_delta_N','selected_deadline'],
        early_source_and_feeder_channels_capped=True,old_ready_distance_Pareto_used=False,
        source_surface_scope='family_specific_cap_including_required_early_sources',aggregate_source_tables_scope='inspection_union_not_a_single_shared_cap_surface',
        full_retention_channels=['weighted_key','minimum_late_count','pooled_rescue'],
        early_seed_policy='required_source_routes_within_cap; complete_seed_layouts_scored_before_standard_full_channels',
        source_general_position_limit_excludes_early_two_gap_seeds=True,
        source_capacity_scope='original_gap_loads_g_through_h_inclusive_le_one',
        native_price_scope='stable_middle_plus_tail_no_large_suffix_subtraction',
        early_source_ready='actual_flight_by_flight_prefix_no_hard_gap_length_limit',
        receiver_delivery_scope='empty_consecutive_double_take_first_delivery_then_load_le1_native_until_second_delivery',
        general_receiver_uses_same_separated_domain=True,
        uniform_price_is_global_bound=False)
    selector=_digest(final);catalogue=_digest(dict(families=final,base_routes=base,weights=w,scales=s,configuration=configuration))
    stats.update(final_families=len(final),final_stations=sorted({f['station_id'] for f in final}),general_full_families_scored=len(scored))
    result=dict(families=final,source_tables=exported,source_tables_scope=configuration['aggregate_source_tables_scope'],
        selector_sha256=selector,catalogue_sha256=catalogue,completed=True,completed_at_perf_counter=None,statistics=stats,
        configuration=configuration,seed=seed,complete_RELAY_model=True,canonical_full_checked=False,scoring_scope=SCOPE)
    _check(deadline);result['completed_at_perf_counter']=perf_counter();_check(deadline);return result


def build_catalogue(problem,base_routes,*,deadline,weights,scales,seed=2026101519,max_families=16,base_pool_limit=64,
        source_position_limit=8,source_option_limit=3,table_option_limit=3,feeder_limit=3,sigma=0.,statistics=None):
    started=perf_counter();stats={} if statistics is None else statistics
    if not isinstance(stats,dict):raise ValueError('mutable statistics dictionary required')
    stats.update(completed=False,deadline_reached=False,seed=seed)
    try:
        result=_build(problem,base_routes,deadline,tuple(weights),tuple(scales),seed,max_families,base_pool_limit,source_position_limit,
            source_option_limit,table_option_limit,feeder_limit,sigma,stats)
        stats.update(completed=True,runtime_seconds=perf_counter()-started);_check(deadline);return result
    except Deadline:
        stats.update(completed=False,deadline_reached=True,runtime_seconds=perf_counter()-started);raise
