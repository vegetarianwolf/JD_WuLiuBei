"""Shared, finite split-feeder families on the unchanged DIRECT responsibility map.

The cheap contiguous-IN catalogue and feeder shortlist only rank candidates.
Only its bounded retained families get the more general single-IN source
tables and exact complete-fleet relay-model clocks. These witnesses are shared
ranking evidence for DIRECT, never DIRECT routes or canonical adoption proof.
"""
from __future__ import annotations

from hashlib import sha256
from itertools import combinations,product
import json
from math import isfinite
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import deadline_window_costs as costs
except ImportError:
    from . import deadline_window_costs as costs

Deadline=costs.Deadline
SCOPE='complete_RELAY_model_common_ranking_not_DIRECT_candidate'


def _check(deadline):
    if type(deadline) is bool or not isfinite(deadline):raise ValueError('finite absolute deadline required')
    if perf_counter()>=deadline:raise Deadline


def _count(stats,name,n=1):stats[name]=stats.get(name,0)+n


def _physical(s):return dict(late_count=s.late_count,total_lateness_min=float(s.total_lateness_min),distance_km=float(s.distance_km))


def _digest(value):return sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _identity(f):return tuple(f[k] for k in ('i','j','a','b','c','station_id'))


def _late(p,t,clock):
    value=clock-p.task(t).deadline_min
    return value if value>1e-9 else 0.


def _station_legs(p,deadline):
    # Preserve both declared eligibility and the immutable leg registry.
    result={}
    for t in sorted(p.task_ids):
        for station in sorted(p.relay_stations,key=lambda h:h.id):
            _check(deadline)
            try:
                incoming=costs.frozen._leg(p,t,station.id,'RELAY_IN')
                outgoing=costs.frozen._leg(p,t,station.id,'RELAY_OUT')
            except (ValueError,KeyError):continue
            result[t,station.id]=(incoming,outgoing)
    return result


def _proxy_profile(p,route,a,t,H,base,deadline,stats):
    """Actual clock/distance for legal *contiguous* P, H insertions only."""
    pickup=p.task(t).pickup;home=p.home_points()[a];positions=[]
    for gap in range(len(route)+1):
        _check(deadline)
        load=base['load_before'][gap] if gap<len(route) else 0
        if load>=2:continue
        previous=home if gap==0 else p.point_for_visit(route[gap-1])
        clock=0. if gap==0 else base['event_times_min'][gap-1]
        distance=previous.distance_to(pickup)+pickup.distance_to(H)
        delta=distance
        if gap<len(route):
            following=p.point_for_visit(route[gap]);delta+=H.distance_to(following)-previous.distance_to(following)
        positions.append(dict(gap=gap,in_drop_time_min=clock+distance/p.speed_km_per_min,delta_distance_km=delta))
    _count(stats,'feeder_legal_contiguous_proxy_positions',len(positions))
    if not positions:return None
    physical=min(positions,key=lambda v:(v['delta_distance_km'],v['in_drop_time_min'],v['gap']))
    early=min(positions,key=lambda v:(v['in_drop_time_min'],v['delta_distance_km'],v['gap']))
    representatives=[physical]
    if early!=physical:representatives.append(early)
    return dict(aircraft=a,physical=physical,earliest=early,representatives=representatives,
        legal_proxy_positions=len(positions),scope='contiguous_IN_geometry_and_actual_prefix_clock_only_not_full_native_score')


def _feeders(profiles,limit):
    if not profiles:return ()
    ordered=sorted(profiles,key=lambda x:(x['physical']['delta_distance_km'],x['physical']['in_drop_time_min'],x['aircraft']))
    earliest=min(profiles,key=lambda x:(x['earliest']['in_drop_time_min'],x['earliest']['delta_distance_km'],x['aircraft']))
    chosen=[ordered[0]]
    if limit>1 and earliest['aircraft']!=chosen[0]['aircraft']:chosen.append(earliest)
    for value in ordered:
        if len(chosen)>=limit:break
        if all(value['aircraft']!=x['aircraft'] for x in chosen):chosen.append(value)
    return tuple(chosen)


def _joint_proxy_sources(left,right,stats):
    """Pareto compression of this proxy only, not of full native scores."""
    combinations=list(product(left,right));_count(stats,'proxy_source_combinations_considered',len(combinations))
    ordered=sorted(combinations,key=lambda pair:(max(x['in_drop_time_min'] for x in pair),
        sum(x['delta_distance_km'] for x in pair),pair[0]['gap'],pair[1]['gap']))
    kept=[];best_distance=float('inf')
    for pair in ordered:
        distance=sum(x['delta_distance_km'] for x in pair)
        if distance<best_distance:
            kept.append(pair);best_distance=distance
    _count(stats,'proxy_source_combinations_retained',len(kept))
    _count(stats,'proxy_source_combinations_dominated',len(combinations)-len(kept))
    return kept


def _receiver_proxy_choices(records,ready,stats):
    """At most three cheap gap axes; complete tables still time every gap."""
    if not records:return [],{}
    physical=min(records,key=lambda r:(r[6],r[1],r[0],r[2]))
    earliest=min(records,key=lambda r:(r[1],r[6],r[0],r[2]))
    safe=[r for r in records if max(ready,r[1])<=r[7]]
    critical=(min(safe,key=lambda r:(-r[1],r[6],r[0],r[2])) if safe else
              min(records,key=lambda r:(-r[7],r[6],r[1],r[0],r[2])))
    axes=dict(geometry_gap=physical[0],earliest_gap=earliest[0],
        selected_deadline_gap=critical[0],selected_deadline_proxy_feasible=bool(safe))
    chosen=set((physical[0],earliest[0],critical[0]));all_gaps={r[0] for r in records}
    _count(stats,'proxy_receiver_gap_instances_considered',len(all_gaps))
    _count(stats,'proxy_receiver_gap_instances_retained',len(chosen))
    _count(stats,'proxy_receiver_gap_instances_omitted',len(all_gaps-chosen))
    return [r for r in records if r[0] in chosen],axes


def _retain(records,limit,weights,scales,key='key'):
    """True weighted, minimum-N, pooled-rescue channels; no score mutation."""
    unique={tuple(tuple(r) for r in x['routes']):x for x in records}
    values=list(unique.values())
    if not values:return []
    order=lambda x:(tuple(x[key]),tuple(tuple(r) for r in x['routes']))
    ordered=sorted(values,key=order);chosen=[ordered[0]]
    heads=(min(values,key=lambda x:(x[key][1],x[key][2],x[key][3],order(x))),
           min(values,key=lambda x:(-len(x['pooled_rescued_task_ids']),len(x['pooled_newly_late_task_ids']),order(x))))
    for value in (*heads,*ordered):
        if len(chosen)>=limit:break
        if all(value['routes']!=old['routes'] for old in chosen):chosen.append(value)
    return chosen


def _station_pool(values,limit,*,exact):
    """Station round-robin, with objective/rescue channels inside each H."""
    by_station={}
    for f in values:by_station.setdefault(f['station_id'],[]).append(f)
    ranked={}
    for H,group in by_station.items():
        weighted=lambda f:(tuple(f['priority']),_identity(f))
        ordered=sorted(group,key=weighted)
        rescue=sorted(group,key=lambda f:(-f['pooled_rescue_count'],weighted(f)))
        minimum_n=sorted(group,key=lambda f:(tuple(f.get('minimum_N_key',f['priority']))[1:],weighted(f)))
        stream=[];seen=set()
        channels=(ordered,minimum_n,rescue) if exact else (ordered,rescue)
        for depth in range(max(len(v) for v in channels)):
            for channel in channels:
                if depth<len(channel) and _identity(channel[depth]) not in seen:
                    value=channel[depth];stream.append(value);seen.add(_identity(value))
        ranked[H]=stream
    selected=[];depth=0
    while len(selected)<limit:
        added=False
        for H in sorted(ranked):
            if depth<len(ranked[H]):
                selected.append(ranked[H][depth]);added=True
                if len(selected)>=limit:break
        if not added:break
        depth+=1
    return selected


def _source_json(record,t,a,H,w,s):
    return dict(task_id=t,aircraft=a,station_id=H,route=list(record['route']),score=_physical(record['score']),
        key=list(costs.weighted_key(record['score'],w,s)),event_times_min=list(record['event_times_min']),
        delivery_times_min={str(k):v for k,v in sorted(record['delivery_times_min'].items())},
        source_drop_availability_min={str(k):v for k,v in record['availability'].items()},
        in_drop_time_min=record['in_drop_time_min'],in_drop_position=record['in_drop_position'],
        pickup_position=record['pickup_position'],load_before=list(record['load_before']),load_after=list(record['load_after']),
        final_delivery_count=record['final_delivery_count'],native_complete=True,optimistic_missing_comp=False,
        compensation_task_id=None,latest_drop_bounds=list(record['latest_drop_bounds']),
        source_bound_position_coverage=list(record['source_bound_position_coverage']),
        coverage_scope='finite_position_retention_only_not_global_feasibility_or_J_bound')


def _build(p,routes,*,deadline,weights,scales,seed,max_families,base_pool_limit,source_position_limit,
           source_option_limit,table_option_limit,feeder_limit,sigma,stats):
    _check(deadline);w,s=tuple(weights),tuple(scales)
    costs.weighted_key(Score(0,0.,0.),w,s)
    limits=((max_families,128),(base_pool_limit,4096),(source_position_limit,512),(source_option_limit,3),(table_option_limit,3),(feeder_limit,64))
    if (p.drone_count<3 or p.capacity!=2 or p.distance_matrix_km is not None
        or any(type(x) is not int or not 1<=x<=upper for x,upper in limits)
        or base_pool_limit<max_families or type(seed) is not int or type(sigma) is bool or not isfinite(sigma) or sigma<0):
        raise ValueError('finite Euclidean C2 catalogue with >=3 aircraft and bounded common limits required')
    base=tuple(tuple(r) for r in routes)
    if len(base)!=p.drone_count:raise ValueError('one genuine complete DIRECT route per aircraft required')
    full=evaluate_solution(p,base,_force_full=True);_check(deadline)
    index=build_plan_index(p,base)
    if not full.valid or index.relay_task_ids:raise ValueError('valid complete pure DIRECT catalogue source required')
    native=[costs.route_cost(p,r,a,deadline=deadline) for a,r in enumerate(base)]
    original={t:v for r in native for t,v in r['delivery_times_min'].items()}
    baseline=Score(sum(x['score'].late_count for x in native),sum(x['score'].total_lateness_min for x in native),sum(x['score'].distance_km for x in native))
    owners=index.delivery_route_by_task;by_owner={a:sorted(t for t,v in owners.items() if v==a) for a in range(p.drone_count)}
    homes=p.home_points();stations={h.id:h.point for h in p.relay_stations};legs=_station_legs(p,deadline);profiles={}
    for (t,H) in sorted(legs):
        choices=[]
        for a in range(p.drone_count):
            _check(deadline)
            if a==owners[t]:continue
            if not by_owner[a]:
                _count(stats,'feeder_profiles_without_real_DIRECT_donor_omitted');continue
            _count(stats,'feeder_task_station_profiles_considered')
            value=_proxy_profile(p,base[a],a,t,stations[H],native[a],deadline,stats)
            if value is not None:choices.append(value)
        profiles[t,H]=_feeders(choices,feeder_limit)
        _count(stats,'feeder_profiles_retained',len(profiles[t,H]));_count(stats,'feeder_profiles_omitted',len(choices)-len(profiles[t,H]))
    candidates=[]
    for c in range(p.drone_count):
        for i,j in combinations(by_owner[c],2):
            _check(deadline);_count(stats,'same_owner_task_pairs_considered')
            skeleton=tuple(v for v in base[c] if abs(v) not in (i,j))
            empty=costs.route_cost(p,skeleton,c,deadline=deadline)
            gaps=costs.zero_load_gaps(p,skeleton,deadline=deadline)
            for H in sorted(stations):
                if (i,H) not in legs or (j,H) not in legs:continue
                _count(stats,'eligible_pair_station_count');point=stations[H]
                receiver=[]
                for gap in gaps:
                    previous=homes[c] if gap==0 else p.point_for_visit(skeleton[gap-1])
                    arrival=(0. if gap==0 else empty['event_times_min'][gap-1])+previous.distance_to(point)/p.speed_km_per_min
                    for first,last in ((i,j),(j,i)):
                        shared=point.distance_to(p.task(first).delivery)
                        if shared<=1e-10:continue
                        tail=shared+p.task(first).delivery.distance_to(p.task(last).delivery)
                        delta=previous.distance_to(point)+tail
                        if gap<len(skeleton):
                            following=p.point_for_visit(skeleton[gap]);delta+=p.task(last).delivery.distance_to(following)-previous.distance_to(following)
                        off_first,off_last=shared/p.speed_km_per_min,tail/p.speed_km_per_min
                        bound=min(p.task(first).deadline_min+1e-9-off_first,p.task(last).deadline_min+1e-9-off_last)
                        receiver.append((gap,arrival,first,last,off_first,off_last,delta,bound))
                for pa,pb in product(profiles[i,H],profiles[j,H]):
                    if pa['aircraft']==pb['aircraft']:continue
                    best=None;most_rescued=0
                    native_N=baseline.late_count-native[c]['score'].late_count+empty['score'].late_count
                    native_L=baseline.total_lateness_min-native[c]['score'].total_lateness_min+empty['score'].total_lateness_min
                    native_D=baseline.distance_km-native[c]['score'].distance_km+empty['score'].distance_km
                    for left,right in _joint_proxy_sources(pa['representatives'],pb['representatives'],stats):
                        choices,axes=_receiver_proxy_choices(receiver,max(left['in_drop_time_min'],right['in_drop_time_min']),stats)
                        for gap,arrival,first,last,off_first,off_last,delta,_ in choices:
                            _check(deadline)
                            T=max(left['in_drop_time_min'],right['in_drop_time_min'],arrival)
                            times={first:T+off_first,last:T+off_last}
                            late=[_late(p,t,times[t]) for t in (i,j)]
                            N=native_N+sum(v>0 for v in late);L=native_L+sum(late)
                            D=native_D+delta+left['delta_distance_km']+right['delta_distance_km']
                            # Parameters are validated once at entry. Preserve the
                            # frozen sum(w*v/s) arithmetic without millions of
                            # repeated Score objects/weight validation calls.
                            key=(sum(weight*value/scale for weight,value,scale in zip(w,(N,L,D),s)),N,L,D)
                            rescued=sorted(t for t in (i,j) if _late(p,t,original[t]) and not _late(p,t,times[t]))
                            _count(stats,'joint_proxy_positions_scored')
                            most_rescued=max(most_rescued,len(rescued))
                            if best is None or (key,tuple(-t for t in rescued),gap,first)<(tuple(best['priority']),tuple(-t for t in best['proxy']['pooled_rescued_task_ids']),best['proxy']['receiver_gap'],best['proxy']['delivery_order_task_ids'][0]):
                                best=dict(i=i,j=j,a=pa['aircraft'],b=pb['aircraft'],c=c,station_id=H,priority=list(key),pooled_rescue_count=len(rescued),
                                    proxy=dict(source_proxy_records=[left,right],receiver_gap=gap,delivery_order_task_ids=[first,last],second_take_min=T,
                                        pooled_rescued_task_ids=rescued,receiver_gap_axes=axes,
                                        score_omits='source-native and receiver-native insertion/delay losses; ranking only'))
                    if best is not None:
                        best['pooled_rescue_count']=most_rescued
                        candidates.append(best);_count(stats,'base_families_proxy_scored')
    _check(deadline)
    pooled=_station_pool(candidates,base_pool_limit,exact=False)
    stats.update(base_families_retained=len(pooled),base_families_not_full_scored=len(candidates)-len(pooled),
        available_proxy_stations=sorted({f['station_id'] for f in candidates}),retained_proxy_stations=sorted({f['station_id'] for f in pooled}))
    stats['proxy_stations_omitted_due_pool_limit']=sorted(set(stats['available_proxy_stations'])-set(stats['retained_proxy_stations']))
    source_cache={};source_json={};donor_cache={};scored=[]
    def source(t,a,H):
        key=(t,a,H)
        if key not in source_cache:
            bound=p.task(t).deadline_min+1e-9-stations[H].distance_to(p.task(t).delivery)/p.speed_km_per_min
            source_cache[key]=costs.source_options(p,base[a],a,t,H,deadline=deadline,weights=w,scales=s,
                optimistic_missing_comp=False,option_limit=source_option_limit,position_limit=source_position_limit,
                latest_drop_bounds=(bound,),statistics=stats.setdefault('route_cost_statistics',{}))
            source_json['%d:%d:%d'%key]=[_source_json(r,t,a,H,w,s) for r in source_cache[key]]
            _count(stats,'full_source_tables_built')
        return source_cache[key]
    for family in pooled:
        _check(deadline);i,j,a,b,c,H=(family[k] for k in ('i','j','a','b','c','station_id'))
        skeleton=tuple(v for v in base[c] if abs(v) not in (i,j));witnesses=[]
        for left,right in product(source(i,a,H),source(j,b,H)):
            available={i:left['in_drop_time_min'],j:right['in_drop_time_min']}
            for receiver in costs.receiver_options(p,skeleton,c,(i,j),H,available,deadline=deadline,weights=w,scales=s,
                    option_limit=table_option_limit,original_delivery_times=original,sigma=sigma,statistics=stats.setdefault('route_cost_statistics',{})):
                _check(deadline);current=list(native);current[a]=left;current[b]=right;current[c]=receiver
                score=Score(sum(x['score'].late_count for x in current),sum(x['score'].total_lateness_min for x in current),sum(x['score'].distance_km for x in current))
                deliveries={t:v for record in current for t,v in record['delivery_times_min'].items()}
                if set(deliveries)!=set(p.task_ids):raise AssertionError('complete relay model lost task coverage')
                if [r['final_delivery_count'] for r in current]!=[r['final_delivery_count'] for r in native]:raise AssertionError('split feeders changed final responsibilities')
                rescued=sorted(t for t in (i,j) if _late(p,t,original[t]) and not _late(p,t,deliveries[t]))
                lost=sorted(t for t in (i,j) if not _late(p,t,original[t]) and _late(p,t,deliveries[t]))
                witnesses.append(dict(routes=[list(r['route']) for r in current],table_score=_physical(score),key=list(costs.weighted_key(score,w,s)),
                    event_times_min=[list(r['event_times_min']) for r in current],delivery_times_min={str(t):v for t,v in sorted(deliveries.items())},
                    source_drop_availability_min={str(t):v for t,v in available.items()},shared_loaded_distance_km=float(receiver['shared_loaded_distance_km']),
                    actual_final_owners={str(t):v for t,v in sorted(owners.items())},window=receiver['window'],complete_RELAY_model=True,
                    canonical_full_checked=False,scoring_scope=SCOPE,pooled_rescued_task_ids=rescued,pooled_newly_late_task_ids=lost))
                _count(stats,'complete_relay_model_witnesses_scored')
        if not witnesses:continue
        retained=_retain(witnesses,table_option_limit,w,s);best=min(retained,key=lambda x:(tuple(x['key']),x['routes']))
        f={k:family[k] for k in ('i','j','a','b','c','station_id')}
        f.update(pooled_task_ids=[i,j],priority=best['key'],pooled_rescue_count=max(len(x['pooled_rescued_task_ids']) for x in retained),
            minimum_N_key=min((x['key'] for x in retained),key=lambda key:(key[1],key[2],key[3],key[0])),
            relay_witnesses=retained,source_table_keys=['%d:%d:%d'%(i,a,H),'%d:%d:%d'%(j,b,H)],
            proxy=family['proxy'],ranking_scope=SCOPE)
        scored.append(f);_count(stats,'full_family_tables_scored')
    final=_station_pool(scored,max_families,exact=True)
    def donor(a):
        if a not in donor_cache:
            records=[]
            for t in by_owner[a]:
                _check(deadline);deleted=tuple(v for v in base[a] if abs(v)!=t)
                after=costs.route_cost(p,deleted,a,deadline=deadline)
                delta=Score(after['score'].late_count-native[a]['score'].late_count,
                    after['score'].total_lateness_min-native[a]['score'].total_lateness_min,after['score'].distance_km-native[a]['score'].distance_km)
                records.append((costs.weighted_key(delta,w,s),t,after))
            if not records:return None
            key,t,after=min(records,key=lambda x:(x[0],x[1]))
            donor_cache[a]=dict(task_id=t,aircraft=a,candidate_count=len(records),baseline_score=_physical(native[a]['score']),
                deleted_route=list(after['route']),deletion_score=_physical(after['score']),deletion_delta_key=list(key),
                policy='best_exact_single_whole_DIRECT_deletion_weighted_credit; DIRECT-only donor; unchanged in RELAY')
        return donor_cache[a]
    families=[];exported={}
    for family in final:
        _check(deadline);one,two=donor(family['a']),donor(family['b'])
        if one is None or two is None:
            _count(stats,'families_without_real_DIRECT_donor_omitted');continue
        family=dict(family,k1=one['task_id'],k2=two['task_id'],donor_evidence=[one,two])
        family['task_ids']=sorted((family['i'],family['j'],family['k1'],family['k2']))
        family['source_tables']={key:source_json[key] for key in family['source_table_keys']}
        exported.update(family['source_tables']);families.append(family)
    families.sort(key=lambda f:(tuple(f['priority']),_identity(f)))
    configuration=dict(max_families=max_families,base_pool_limit=base_pool_limit,source_position_limit=source_position_limit,
        source_option_limit=source_option_limit,table_option_limit=table_option_limit,feeder_limit=feeder_limit,sigma=sigma,
        feeder_proxy='all_legal_contiguous_IN_gaps_geometry+earliest_then_finite_feeder_shortlist',
        base_pool='station_round_robin_proxy_weighted+pooled_rescue',final_pool='station_round_robin_true_weighted+minN+pooled_rescue',
        source_tables='lazy_exact_finite_position_tables_only_for_proxy_base_pool',donors='exact_single_task_deletion_credit_only_for_DIRECT',
        proxy_source_compression='Pareto(ready=max(source_drop_clocks),sum_source_deltaD); proxy only',
        proxy_receiver_gap_limit=3,proxy_receiver_gap_axes=['geometry','earliest','selected_deadline_latest_safe_or_most_relaxed'],
        complete_receiver_tables='all_actual_empty_gaps_times_two_delivery_orders',
        full_witnesses_are_DIRECT_candidates=False,canonical_adoption_by_caller=True)
    selector=_digest(families);catalogue=_digest(dict(families=families,base_routes=base,weights=w,scales=s,configuration=configuration))
    stats.update(final_families=len(families),final_stations=sorted({f['station_id'] for f in families}),
        full_scored_families_not_retained=len(scored)-len(families))
    stats['full_scored_stations_not_retained']=sorted({f['station_id'] for f in scored}-set(stats['final_stations']))
    result=dict(families=families,source_tables=exported,selector_sha256=selector,catalogue_sha256=catalogue,
        completed=True,completed_at_perf_counter=None,statistics=stats,configuration=configuration,seed=seed,
        complete_RELAY_model=True,canonical_full_checked=False,scoring_scope=SCOPE)
    _check(deadline);result['completed_at_perf_counter']=perf_counter();_check(deadline)
    return result


def build_catalogue(problem,base_routes,*,deadline,weights,scales,seed=2026101516,max_families=16,
                    base_pool_limit=64,source_position_limit=8,source_option_limit=3,
                    table_option_limit=3,feeder_limit=3,sigma=0.,statistics=None):
    """Return only a completed shared catalogue; expiry raises the common Deadline."""
    started=perf_counter();stats={} if statistics is None else statistics
    if not isinstance(stats,dict):raise ValueError('statistics must be a mutable dictionary')
    stats.update(completed=False,deadline_reached=False,seed=seed)
    try:
        result=_build(problem,base_routes,deadline=deadline,weights=weights,scales=scales,seed=seed,
            max_families=max_families,base_pool_limit=base_pool_limit,source_position_limit=source_position_limit,
            source_option_limit=source_option_limit,table_option_limit=table_option_limit,feeder_limit=feeder_limit,sigma=sigma,stats=stats)
        stats.update(completed=True,runtime_seconds=perf_counter()-started)
        _check(deadline)
        return result
    except Deadline:
        stats.update(completed=False,deadline_reached=True,runtime_seconds=perf_counter()-started)
        raise
