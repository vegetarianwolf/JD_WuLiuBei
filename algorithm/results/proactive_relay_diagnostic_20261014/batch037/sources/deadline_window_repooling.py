"""Fixed-H joint deadline windows, with an independent twelve-map DIRECT arm.

Window records are search evidence, not fleet-feasible partial solutions or J
bounds. All selected plans are rebuilt with compensation; only canonical full
plans may replace the unchanged strong DIRECT input before the shared deadline.
"""
from __future__ import annotations

from bisect import bisect_right
from hashlib import sha256
from heapq import heappush,heapreplace
from itertools import combinations,product
import json
from math import isfinite,isclose
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import deadline_window_costs as costs
except ImportError:
    from . import deadline_window_costs as costs

Deadline=costs.Deadline
weighted_key=costs.weighted_key
EPS=1e-9


def _check(deadline,stats):
    if perf_counter()>=deadline:
        stats['deadline_reached']=True
        raise Deadline


def _physical(score):
    return dict(late_count=score.late_count,total_lateness_min=score.total_lateness_min,distance_km=score.distance_km)


def _digest(records):
    return sha256(json.dumps(records,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _task(p,v):
    leg=p.leg_registry.get(abs(v))
    return leg.task_id if leg else abs(v)


def _remove(route,selected):return tuple(v for v in route if abs(v) not in selected)


def _classes(p,original,current,selected):
    old={t for t,clock in original.items() if clock-p.task(t).deadline_min>EPS}
    new={t for t,clock in current.items() if clock-p.task(t).deadline_min>EPS}
    return dict(selected_rescued_task_ids=sorted((old-new)&selected),native_rescued_task_ids=sorted((old-new)-selected),
        selected_newly_late_task_ids=sorted((new-old)&selected),native_newly_late_task_ids=sorted((new-old)-selected))


def _retain(candidates,limit,p,original,selected,w,s):
    unique={c['routes']:c for c in candidates};ordered=sorted(unique.values(),key=lambda c:(weighted_key(c['score'],w,s),c['routes']))
    if not ordered:return []
    def rescue(c):
        clocks={t:v for r in c['records'] for t,v in r['delivery_times'].items()}
        cls=_classes(p,original,clocks,selected)
        return (-len(cls['selected_rescued_task_ids']),len(cls['selected_newly_late_task_ids']),weighted_key(c['score'],w,s),c['routes'])
    representatives=(ordered[0],min(ordered,key=lambda c:(c['score'].late_count,weighted_key(c['score'],w,s),c['routes'])),min(ordered,key=rescue))
    result=[]
    for c in (*representatives,*ordered):
        if len(result)>=limit:break
        if all(c['routes']!=old['routes'] for old in result):result.append(c)
    return result


class _Worst:
    def __init__(self,key,value):self.key,self.value=key,value
    def __lt__(self,other):return self.key>other.key


def _offer(heap,key,value,limit):
    entry=_Worst(key,value)
    if len(heap)<limit:heappush(heap,entry)
    elif key<heap[0].key:heapreplace(heap,entry)


def _family_rescue_key(family):
    preview=family['selector_window'];cls=preview['preview_classifications']
    net=len(cls['selected_newly_late_task_ids'])+len(cls['native_newly_late_task_ids'])-len(cls['selected_rescued_task_ids'])-len(cls['native_rescued_task_ids'])
    return (net,-len(cls['selected_rescued_task_ids']),*family['priority'])


def _family_channels(families,limit):
    """Share finite slots between full-native preview J and net rescue rank."""
    weighted=[];rescue=[]
    for key,target in ((lambda f:f['priority'],weighted),(_family_rescue_key,rescue)):
        sorted_groups=[sorted(group,key=key) for group in families if group]
        for depth in range(max((len(group) for group in sorted_groups),default=0)):
            target.extend(sorted((group[depth] for group in sorted_groups if len(group)>depth),key=key))
    result=[];seen=set()
    for ordered,count in ((weighted,limit//2),(rescue,limit-limit//2),(weighted,limit),(rescue,limit)):
        added=0
        for family in ordered:
            identity=tuple(family[k] for k in ('i','j','c','station_id','k1','k2'))
            if identity in seen:continue
            if added>=count or len(result)>=limit:break
            result.append(family);seen.add(identity);added+=1
    return result


def _source_pairs(left,right,requirements,w,s):
    coefficients=tuple(weight/scale for weight,scale in zip(w,s))
    def key(r):
        score=r['score'];values=(score.late_count,score.total_lateness_min,score.distance_km)
        return (sum(c*v for c,v in zip(coefficients,values)),*values),r['route']
    early=lambda r:(r['in_drop_time_min'],key(r))
    choices=[(min(left,key=early),min(right,key=early)),(min(left,key=key),min(right,key=key))]
    for _,bound in requirements:
        eligible_left=[r for r in left if r['in_drop_time_min']<=bound]
        eligible_right=[r for r in right if r['in_drop_time_min']<=bound]
        if eligible_left and eligible_right:choices.append((min(eligible_left,key=key),min(eligible_right,key=key)))
    result=[];seen=set()
    for pair in choices:
        identity=tuple(r['route'] for r in pair)
        if identity not in seen:result.append(pair);seen.add(identity)
    return result


def _select_families(p,base,base_records,checked,w,s,seed,*,deadline,max_families,base_pool_limit,
                     source_position_limit,source_option_limit,compensation_pool_limit,compensation_limit,
                     sigma,stats):
    """All late anchors/all foreign partners/all C/H precede finite ranking."""
    owners=build_plan_index(p,base).delivery_route_by_task
    by_owner={a:tuple(sorted(t for t in p.task_ids if owners[t]==a)) for a in range(p.drone_count)}
    original=checked.delivery_times_min;late={t for t,v in original.items() if v-p.task(t).deadline_min>EPS}
    coefficients=tuple(weight/scale for weight,scale in zip(w,s))
    homes=p.home_points();registry={(leg.task_id,leg.relay_id,leg.kind):leg.id for leg in p.leg_registry.values()}
    source={};gaps={};geometry={};heaps=([],[]);seen_pairs=set()
    for station in p.relay_stations:
        H=station.point
        for a,route in enumerate(base):
            items=[]
            for gap in costs.zero_load_gaps(p,route,deadline=deadline):
                left=homes[a] if gap==0 else p.point_for_visit(route[gap-1])
                prefix=0. if gap==0 else base_records[a]['event_times'][gap-1]
                c=prefix+left.distance_to(H)/p.speed_km_per_min
                items.append((c,gap))
            gaps[(station.id,a)]=tuple(sorted(items))
        for t in sorted(p.task_ids):
            _check(deadline,stats)
            if (p.task_relay_candidates and station.id not in p.task_relay_candidates.get(t,())) or any(
                    (t,station.id,kind) not in registry for kind in ('RELAY_IN','RELAY_OUT')):continue
            source[(t,station.id)]=costs.source_options(p,_remove(base[owners[t]],{t}),owners[t],t,station.id,
                deadline=deadline,weights=w,scales=s,option_limit=source_option_limit,
                position_limit=source_position_limit,optimistic_missing_comp=True,
                latest_drop_bounds=(p.task(t).deadline_min+EPS-H.distance_to(p.task(t).delivery)/p.speed_km_per_min,),
                statistics=stats['route_cost_statistics'])
            stats['selector_source_tables']+=1
    stats['selector_late_anchors']=len(late)
    for anchor in sorted(late):
        for partner in sorted(p.task_ids):
            _check(deadline,stats)
            if owners[anchor]==owners[partner]:continue
            i,j=sorted((anchor,partner))
            if (i,j) in seen_pairs:continue
            seen_pairs.add((i,j));a,b=owners[i],owners[j]
            for station in p.relay_stations:
                H=station.point;left=source.get((i,station.id),());right=source.get((j,station.id),())
                if not left or not right:continue
                order_contexts=[]
                for order in ((i,j),(j,i)):
                    d1,d2=(p.task(t).delivery for t in order)
                    offsets={order[0]:H.distance_to(d1)/p.speed_km_per_min,
                        order[1]:(H.distance_to(d1)+d1.distance_to(d2))/p.speed_km_per_min}
                    requirements=[]
                    for t in (i,j):
                        if t not in late:continue
                        Q=tuple(sorted({t}|({i,j}-late)))
                        requirements.append((Q,min(p.task(q).deadline_min+EPS-sigma-offsets[q] for q in Q)))
                    combos=[]
                    for ri,rj in _source_pairs(left,right,requirements,w,s):
                        delta=ri['score']+rj['score']-base_records[a]['score']-base_records[b]['score']
                        combos.append((ri,rj,(delta.late_count,delta.total_lateness_min,delta.distance_km)))
                    order_contexts.append((order,d1,d2,offsets,requirements,combos))
                for c in range(p.drone_count):
                    _check(deadline,stats)
                    if c in (a,b) or len(by_owner[c])<2:continue
                    stats['selector_joint_bases_generated']+=1
                    best=None;clock_gaps=gaps[(station.id,c)];route=base[c]
                    for order,d1,d2,offsets,requirements,combos in order_contexts:
                        geo_key=(station.id,c,order[1])
                        if geo_key not in geometry:
                            def extra(item):
                                _,g=item;u=homes[c] if g==0 else p.point_for_visit(route[g-1])
                                return u.distance_to(H)+(d2.distance_to(p.point_for_visit(route[g]))-u.distance_to(p.point_for_visit(route[g])) if g<len(route) else 0.)
                            geometry[geo_key]=min(clock_gaps,key=lambda item:(extra(item),item))
                        for ri,rj,source_delta in combos:
                            stats['selector_source_window_combinations']+=1
                            arrival=max(ri['in_drop_time_min'],rj['in_drop_time_min'])
                            choices={clock_gaps[0],geometry[geo_key]}
                            for _,B in requirements:
                                if arrival<=B:
                                    k=bisect_right(clock_gaps,(B,len(route)+1))-1
                                    if k>=0:choices.add(clock_gaps[k])
                            for clock,gap in sorted(choices):
                                _check(deadline,stats)
                                T=max(clock,arrival);delivered={t:T+offsets[t] for t in (i,j)}
                                rescued=[t for t in (i,j) if t in late and delivered[t]-p.task(t).deadline_min<=EPS]
                                damaged=[t for t in (i,j) if t not in late and delivered[t]-p.task(t).deadline_min>EPS]
                                safe=[(Q,B) for Q,B in requirements if T<=B]
                                required,B=(max(safe,key=lambda x:(len(x[0]),x[1])) if safe else requirements[0])
                                u=homes[c] if gap==0 else p.point_for_visit(route[gap-1])
                                distance=u.distance_to(H)+H.distance_to(d1)+d1.distance_to(d2)
                                if gap<len(route):distance+=d2.distance_to(p.point_for_visit(route[gap]))-u.distance_to(p.point_for_visit(route[gap]))
                                selected_late=[v-p.task(t).deadline_min for t,v in delivered.items() if v-p.task(t).deadline_min>EPS]
                                proxy=(coefficients[0]*(source_delta[0]+len(selected_late))
                                    +coefficients[1]*(source_delta[1]+sum(selected_late))
                                    +coefficients[2]*(source_delta[2]+distance))
                                tie=(i*31+j*17+c*7+station.id*101+seed)%104729
                                value=dict(i=i,j=j,a=a,b=b,c=c,station_id=station.id,source_records=(ri,rj),
                                    gap=gap,order=order,required=required,B=B,arrival=clock,T=T,
                                    rescued=rescued,damaged=damaged,proxy=proxy)
                                key=(len(damaged)-len(rescued),-len(rescued),proxy,-(B-T),tie,i,j,c,station.id,gap,*order)
                                if best is None or key<best[0]:best=(key,value)
                                stats['selector_joint_windows_ranked']+=1
                    if best is None:continue
                    key,value=best
                    physical=(value['proxy'],key[0],*key[4:])
                    _offer(heaps[1],physical,value,base_pool_limit//2)
                    if value['rescued'] and not value['damaged']:
                        _offer(heaps[0],key,value,base_pool_limit-base_pool_limit//2)
    pooled={tuple(v.value[k] for k in ('i','j','c','station_id')):v.value for heap in heaps for v in heap}
    stats['selector_base_pool_retained']=len(pooled)
    families=[];removal_cache={};single_cache={}
    for value in sorted(pooled.values(),key=lambda v:(-len(v['rescued']),v['proxy'],v['i'],v['j'],v['c'],v['station_id'])):
        _check(deadline,stats)
        i,j,a,b,c,H=(value[k] for k in ('i','j','a','b','c','station_id'))
        ri,rj=value['source_records'];availability={i:ri['in_drop_time_min'],j:rj['in_drop_time_min']}
        out=tuple(registry[(t,H,'RELAY_OUT')] for t in (i,j));order=tuple(registry[(t,H,'RELAY_OUT')] for t in value['order'])
        gap=value['gap'];preview=base[c][:gap]+out+tuple(-v for v in order)+base[c][gap:]
        rec=costs.route_cost(p,preview,c,availability=availability,selected_pair=out,deadline=deadline,
            allow_final_k_overflow=True,statistics=stats['route_cost_statistics'])
        window=costs.window_for_record(p,rec,(i,j),H,availability,gap=gap,original_delivery_times=original,
            sigma=sigma,required_on_time_task_ids=value['required'],deadline=deadline)
        witness=dict(source_preview_routes={str(i):list(ri['route']),str(j):list(rj['route'])},
            receiver_gap_index=gap,receiver_preview_route=list(preview),delivery_order_task_ids=list(value['order']),
            optimistic_missing_comp=True,receiver_quota_overflow=rec['final_k_overflow_count'],
            preview_complete_solution=False,window=window)
        preview_delta=ri['score']+rj['score']+rec['score']-base_records[a]['score']-base_records[b]['score']-base_records[c]['score']
        preview_total=sum((r['score'] for r in base_records),Score(0,0.,0.))+preview_delta
        preview_deliveries=dict(original)
        preview_deliveries.update({t:v for r in (ri,rj,rec) for t,v in r['delivery_times'].items()})
        witness.update(preview_route_table_score=_physical(preview_total),preview_ranking_delta=_physical(preview_delta),
            preview_classifications=_classes(p,original,preview_deliveries,{i,j}),
            initial_index_proxy_omits_receiver_native_delay=value['proxy'])
        def removed(chosen):
            key=(c,tuple(sorted(chosen)))
            if key not in removal_cache:
                removal_cache[key]=costs.route_cost(p,_remove(base[c],set(chosen)),c,deadline=deadline,
                    statistics=stats['route_cost_statistics'])['score']-base_records[c]['score']
            return removal_cache[key]
        removal_pool=sorted(by_owner[c],key=lambda k:(weighted_key(removed((k,)),w,s),k))[:compensation_pool_limit]
        sides=[]
        for own,target in ((i,a),(j,b)):
            geo=sorted(by_owner[c],key=lambda k:(p.task(k).pickup.distance_to(homes[target])+p.task(k).delivery.distance_to(homes[target]),k))[:compensation_pool_limit]
            options={}
            for k in sorted(set(geo)|set(removal_pool)):
                _check(deadline,stats);key=(target,own,k)
                if key not in single_cache:
                    found=costs.direct_single_options(p,_remove(base[target],{own}),target,k,deadline=deadline,
                        weights=w,scales=s,option_limit=1,position_limit=source_position_limit,
                        original_delivery_times=original,statistics=stats['route_cost_statistics'])
                    single_cache[key]=found[0]['score']-costs.route_cost(p,_remove(base[target],{own}),target,
                        deadline=deadline,statistics=stats['route_cost_statistics'])['score'] if found else None
                if single_cache[key] is not None:options[k]=single_cache[key]
            sides.append(options)
        choices=[]
        for k1,delta_a in sides[0].items():
            for k2,delta_b in sides[1].items():
                _check(deadline,stats)
                if k1==k2:continue
                credit=weighted_key(delta_a+delta_b+removed((k1,k2)),w,s)[0]
                priority=[weighted_key(preview_delta,w,s)[0]+credit,
                    (i*31+j*17+c*7+H*101+seed)%104729,i,j,c,H,k1,k2]
                choices.append(dict(i=i,j=j,a=a,b=b,c=c,k1=k1,k2=k2,station_id=H,task_ids=sorted((i,j,k1,k2)),
                    priority=priority,selector_window=witness))
                stats['selector_compensation_pairs_ranked']+=1
        families.append(sorted(choices,key=lambda f:f['priority'])[:compensation_limit**2])
    _check(deadline,stats)
    selected=_family_channels(families,max_families)
    _check(deadline,stats)
    return selected


def _candidate(base_records,family,records,final,relay,window=None):
    all_records=list(base_records)
    for a,r in zip((family['a'],family['b'],family['c']),records):all_records[a]=r
    availability={str(t):v for r in records for t,v in r['availability'].items()} if relay else {}
    owners={str(t):a for a,r in enumerate(all_records) for t in r['delivery_times']}
    shared=records[2]['shared_loaded_distance_km']
    move=dict(family=family,mode='RELAY' if relay else 'DIRECT',final_task_groups=[list(g) for g in final],
        final_aircraft_by_task={str(t):a for a,g in zip((family['a'],family['b'],family['c']),final) for t in g},
        actual_final_owners=owners,source_drop_availability_min=availability,shared_loaded_distance_km=shared,window=window)
    return dict(routes=tuple(r['route'] for r in all_records),records=all_records,move=move,
        score=sum((r['score'] for r in all_records),Score(0,0.,0.)))


def _relay(p,base_records,native,f,w,s,original,*,deadline,source_position_limit,source_option_limit,
           table_option_limit,receiver_gap_limit,sigma,full_limit,stats):
    sources=[]
    H=next(station.point for station in p.relay_stations if station.id==f['station_id'])
    late={t for t in (f['i'],f['j']) if original[t]-p.task(t).deadline_min>EPS}
    bounds=[f['selector_window']['window']['joint_latest_second_take_min']]
    for order in ((f['i'],f['j']),(f['j'],f['i'])):
        first,second=(p.task(t).delivery for t in order)
        offsets={order[0]:H.distance_to(first)/p.speed_km_per_min,
            order[1]:(H.distance_to(first)+first.distance_to(second))/p.speed_km_per_min}
        for target in sorted(late):
            required={target}|({f['i'],f['j']}-late)
            B=min(p.task(t).deadline_min+EPS-sigma-offsets[t] for t in required)
            if B not in bounds:bounds.append(B)
    bounds=tuple(v for v in bounds if v is not None)[:4]
    stats['family_records'][-1]['source_latest_drop_bounds_min']=list(bounds)
    for t,k,a in ((f['i'],f['k1'],f['a']),(f['j'],f['k2'],f['b'])):
        sources.append(costs.source_options(p,native[a],a,t,f['station_id'],compensation_task_id=k,
            optimistic_missing_comp=False,deadline=deadline,weights=w,scales=s,option_limit=source_option_limit,
            position_limit=source_position_limit,latest_drop_bounds=bounds,statistics=stats['route_cost_statistics']))
    complete=[]
    for first,second in product(*sources):
        _check(deadline,stats);availability={**first['availability'],**second['availability']}
        receivers=costs.receiver_options(p,native[f['c']],f['c'],(f['i'],f['j']),f['station_id'],availability,
            deadline=deadline,weights=w,scales=s,option_limit=table_option_limit,gap_limit=receiver_gap_limit,
            original_delivery_times=original,sigma=sigma,statistics=stats['route_cost_statistics'])
        for receiver in receivers:
            complete.append(_candidate(base_records,f,(first,second,receiver),((f['k1'],),(f['k2'],),(f['i'],f['j'])),True,receiver['window']))
            stats['complete_table_candidates']+=1
    return _retain(complete,full_limit,p,original,{f['i'],f['j']},w,s)


def _direct(p,base_records,native,f,w,s,original,*,deadline,source_position_limit,table_option_limit,direct_gap_limit,full_limit,stats):
    aircraft=(f['a'],f['b'],f['c']);tasks=tuple(f['task_ids']);singles={};pairs={};complete=[]
    coverage=stats['family_records'][-1]
    for a in aircraft[:2]:
        for t in tasks:
            singles[(a,t)]=costs.direct_single_options(p,native[a],a,t,deadline=deadline,weights=w,scales=s,
                option_limit=table_option_limit,position_limit=source_position_limit,
                original_delivery_times=original,rescue_task_ids=(f['i'],f['j']),statistics=stats['route_cost_statistics'])
    for pair in combinations(tasks,2):
        pairs[pair]=costs.direct_pair_options(p,native[f['c']],f['c'],pair,deadline=deadline,weights=w,scales=s,
            option_limit=table_option_limit,gap_limit=direct_gap_limit,
            original_delivery_times=original,rescue_task_ids=(f['i'],f['j']),statistics=stats['route_cost_statistics'])
    for t in tasks:
        for q in tasks:
            _check(deadline,stats)
            if t==q:continue
            pair=tuple(k for k in tasks if k not in (t,q));stats['direct_final_maps_examined']+=1
            mapping=[[t],[q],list(pair)]
            coverage['direct_maps_considered'].append(mapping)
            for records in product(singles[(f['a'],t)],singles[(f['b'],q)],pairs[pair]):
                _check(deadline,stats)
                complete.append(_candidate(base_records,f,records,((t,),(q,),pair),False))
                stats['complete_table_candidates']+=1
            coverage['direct_maps_completed'].append(mapping)
    coverage['direct_map_enumeration_completed']=True
    return _retain(complete,full_limit,p,original,{f['i'],f['j']},w,s)


def _verify(p,base,routes,move,relay):
    before=build_plan_index(p,base);index=build_plan_index(p,routes);f=move['family'];selected=set(f['task_ids'])
    if (before.delivery_route_by_task[f['i']]!=f['a'] or before.delivery_route_by_task[f['j']]!=f['b']
        or any(before.delivery_route_by_task[k]!=f['c'] for k in (f['k1'],f['k2']))):raise AssertionError('invalid original responsibility')
    for a in range(p.drone_count):
        if sum(v==a for v in before.delivery_route_by_task.values())!=sum(v==a for v in index.delivery_route_by_task.values()):raise AssertionError('quota changed')
        if tuple(v for v in base[a] if abs(v) not in selected)!=tuple(v for v in routes[a] if _task(p,v) not in selected):raise AssertionError('native events changed')
    if {str(t):a for t,a in index.delivery_route_by_task.items()}!=move['actual_final_owners']:raise AssertionError('actual owner mismatch')
    if any(index.delivery_route_by_task[int(t)]!=a for t,a in move['final_aircraft_by_task'].items()):raise AssertionError('selected map mismatch')
    if set(index.relay_task_ids)!=({f['i'],f['j']} if relay else set()):raise AssertionError('relay identities changed')
    if relay:
        for t,a in ((f['i'],f['a']),(f['j'],f['b'])):
            if index.inbound_route_by_task[t]!=a or index.outbound_route_by_task[t]!=f['c'] or index.task_plans[t].relay_id!=f['station_id']:raise AssertionError('relay roles changed')
        ids=tuple(next(v for v in index.task_plans[t].leg_ids if p.leg_registry[v].kind=='RELAY_OUT') for t in (f['i'],f['j']))
        route=routes[f['c']];positions=sorted(route.index(v) for leg in ids for v in (leg,-leg))
        chunk=route[positions[0]:positions[0]+4]
        if positions!=list(range(positions[0],positions[0]+4)) or any(v<0 for v in chunk[:2]) or any(v>0 for v in chunk[2:]) or sum(1 if v>0 else -1 for v in route[:positions[0]])!=0:raise AssertionError('receiver block changed')
        if move['shared_loaded_distance_km']<=1e-10:raise AssertionError('no positive joint flight')


def _snapshot(p,routes,full,schedule,w,s,*,phase,move=None,table_score=None,original=None):
    index=build_plan_index(p,routes);selected=set(move['family']['task_ids']) if move else set()
    classes=_classes(p,original or schedule.delivery_times_min,schedule.delivery_times_min,selected)
    pooled={move['family']['i'],move['family']['j']} if move else set()
    return dict(routes=[list(r) for r in routes],score=_physical(full.score),key=list(weighted_key(full.score,w,s)),
        valid=True,complete=True,phase=phase,move=move,
        cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        relay_task_count=len(index.relay_task_ids),table_score=_physical(table_score if table_score is not None else full.score),
        event_times_min=[[schedule.event_times_min[(a,k)] for k in range(len(r))] for a,r in enumerate(routes)],
        delivery_times_min={str(t):v for t,v in sorted(schedule.delivery_times_min.items())},
        actual_final_owners={str(t):a for t,a in sorted(index.delivery_route_by_task.items())},
        source_drop_availability_min=move['source_drop_availability_min'] if move else {},
        shared_loaded_distance_km=move['shared_loaded_distance_km'] if move else 0.,window=move['window'] if move else None,
        pooled_rescued_task_ids=sorted(set(classes['selected_rescued_task_ids'])&pooled),
        compensation_rescued_task_ids=sorted(set(classes['selected_rescued_task_ids'])-pooled),**classes)


def refine(problem,routes,*,deadline,weights,scales,seed=2026101514,relay_enabled=True,max_families=16,
           base_pool_limit=64,source_position_limit=8,source_option_limit=3,table_option_limit=3,
           compensation_pool_limit=6,compensation_limit=3,receiver_gap_limit=4,direct_gap_limit=4,
           full_candidates_per_family=3,sigma=0.):
    started=perf_counter();w,s=tuple(weights),tuple(scales)
    bounds=((max_families,64),(base_pool_limit,256),(source_position_limit,512),(source_option_limit,4),
        (table_option_limit,3),(compensation_pool_limit,16),(compensation_limit,4),(receiver_gap_limit,64),
        (direct_gap_limit,64),(full_candidates_per_family,3))
    if (not isfinite(deadline) or type(relay_enabled) is not bool or len(w)!=3 or len(s)!=3
        or any(not isfinite(v) or v<=0 for v in (*w,*s)) or not isclose(sum(w),1.)
        or any(type(v) is not int or not 1<=v<=upper for v,upper in bounds)
        or base_pool_limit<2 or type(sigma) is bool or not isfinite(sigma) or sigma<0):raise ValueError('finite weighted objective and bounded search required')
    base=tuple(tuple(r) for r in routes)
    if len(base)>problem.drone_count:raise ValueError('too many routes')
    base+=((),)*(problem.drone_count-len(base));checked=evaluate_solution(problem,base,_force_full=True)
    if (not checked.valid or build_plan_index(problem,base).relay_task_ids or problem.capacity!=2
        or not problem.has_relays or problem.distance_matrix_km is not None):raise ValueError('valid pure DIRECT Euclidean C2 fixed-network input required')
    evaluator=GlobalRelayEvaluator(problem);schedule=evaluator.evaluate(base);original=schedule.delivery_times_min
    initial_key=best_key=weighted_key(checked.score,w,s);best,best_checked=base,checked
    stats=dict(method='Fixed-H-Deadline-Window-Repooling' if relay_enabled else 'Fixed-H-Window-Independent-Twelve-Map-DIRECT',
        seed=seed,relay_enabled=relay_enabled,deadline_perf_counter=deadline,deadline_reached=False,active_stage='initial',timeout_stage=None,
        selector_completed=False,selector_completed_at_perf_counter=None,selector_sha256=_digest([]),selected_families=[],
        selector_source_tables=0,selector_late_anchors=0,selector_joint_bases_generated=0,selector_joint_windows_ranked=0,
        selector_source_window_combinations=0,
        selector_base_pool_retained=0,selector_compensation_pairs_ranked=0,route_cost_statistics={},
        full_candidate_snapshots=[],first_complete_candidate=None,best_constructed_candidate=None,accepted_moves=[],accepted_improvements=0,
        complete_table_candidates=0,full_evaluations=0,direct_final_maps_examined=0,families_examined=0,family_records=[],
        initial_key=list(initial_key),final_key=list(initial_key),final_cross_uav_count=0,
        input_snapshot=_snapshot(problem,base,checked,schedule,w,s,phase='input'),
        configuration=dict(max_families=max_families,base_pool_limit=base_pool_limit,source_position_limit=source_position_limit,
            source_option_limit=source_option_limit,table_option_limit=table_option_limit,compensation_pool_limit=compensation_pool_limit,
            compensation_limit=compensation_limit,receiver_gap_limit=receiver_gap_limit,direct_gap_limit=direct_gap_limit,
            requested_receiver_gap_limit_legacy_hint=receiver_gap_limit,requested_direct_gap_limit_legacy_hint=direct_gap_limit,
            gap_limits_ignored_for_pre_scoring_prefilter=True,
            full_candidates_per_family=full_candidates_per_family,sigma_min=sigma,late_anchors='all_current_late',partners='all_foreign_owner',
            source_latest_drop_bound_limit=4,source_index_latest_bound='own_first_delivery_deadline',
            source_index_pair_join='physical+earliest+best_source_options_meeting_each_joint_B',
            source_full_latest_bounds='selected_joint_preview_bound_first_then_both_delivery_order_rescue_channels',
            candidate_third_channel='pooled_i_j_rescue_excluding_compensation',
            receivers='all_other_aircraft_with_two_final_tasks',stations='all_existing_eligible_fixed',
            selector_gap_channels='earliest+latest_feasible_by_bisect+physical_minimum',proxy_is_gain_or_lower_bound=False,
            final_family_channels='half_actual_full_native_preview_J_plus_comp_credit+half_net_preview_rescue',
            receiver_all_empty_gaps_scored=True,direct_pair_all_empty_gaps_scored=True,direct_final_map_count=12,selected_macro_identities_paired=False,
            unselected_events_fixed=True,resources_and_homes_unchanged=True,polish_enabled=False))
    try:
        _check(deadline,stats);stats['active_stage']='base_route_tables'
        records=[costs.route_cost(problem,r,a,deadline=deadline,statistics=stats['route_cost_statistics']) for a,r in enumerate(base)]
        stats['active_stage']='shared_window_selector'
        families=_select_families(problem,base,records,checked,w,s,seed,deadline=deadline,max_families=max_families,
            base_pool_limit=base_pool_limit,source_position_limit=source_position_limit,source_option_limit=source_option_limit,
            compensation_pool_limit=compensation_pool_limit,compensation_limit=compensation_limit,sigma=sigma,stats=stats)
        digest=_digest(families);completed=perf_counter()
        if completed>=deadline:raise Deadline
        stats.update(selected_families=families,selector_sha256=digest,selector_completed=True,selector_completed_at_perf_counter=completed)
        for family in families:
            _check(deadline,stats);stats['active_stage']='complete_route_tables';stats['families_examined']+=1
            selected=set(family['task_ids']);native=tuple(_remove(r,selected) for r in base)
            record=dict(family=family,complete_candidates=0,complete_table_enumeration_completed=False,
                direct_maps_considered=[],direct_maps_completed=[],direct_map_enumeration_completed=False)
            stats['family_records'].append(record)
            arguments=dict(deadline=deadline,source_position_limit=source_position_limit,table_option_limit=table_option_limit,
                full_limit=full_candidates_per_family,stats=stats)
            complete=(_relay(problem,records,native,family,w,s,original,source_option_limit=source_option_limit,
                receiver_gap_limit=receiver_gap_limit,sigma=sigma,**arguments) if relay_enabled else
                _direct(problem,records,native,family,w,s,original,direct_gap_limit=direct_gap_limit,**arguments))
            record.update(complete_candidates=len(complete),complete_table_enumeration_completed=True)
            for candidate in complete:
                _check(deadline,stats);stats['active_stage']='canonical_full_guard'
                full=evaluate_solution(problem,candidate['routes'],_force_full=True);stats['full_evaluations']+=1
                canonical=evaluator.evaluate(candidate['routes'])
                if not full.valid or not canonical.feasible:raise AssertionError('local tables produced an invalid full candidate')
                _verify(problem,base,candidate['routes'],candidate['move'],relay_enabled)
                score=candidate['score']
                if (score.late_count!=full.score.late_count or abs(score.total_lateness_min-full.score.total_lateness_min)>1e-8
                    or abs(score.distance_km-full.score.distance_km)>1e-8):raise AssertionError('table/global score mismatch')
                for a,record in enumerate(candidate['records']):
                    if len(record['event_times'])!=len(candidate['routes'][a]) or any(abs(v-canonical.event_times_min[(a,k)])>1e-8 for k,v in enumerate(record['event_times'])):raise AssertionError('table/global clock mismatch')
                snapshot=_snapshot(problem,candidate['routes'],full,canonical,w,s,phase='full_proposal',move=candidate['move'],
                    table_score=score,original=original)
                verified=perf_counter()
                if verified>=deadline:raise Deadline
                stats['full_candidate_snapshots'].append(snapshot)
                if stats['first_complete_candidate'] is None:stats['first_complete_candidate']=snapshot
                if stats['best_constructed_candidate'] is None or tuple(snapshot['key'])<tuple(stats['best_constructed_candidate']['key']):stats['best_constructed_candidate']=snapshot
                key=weighted_key(full.score,w,s)
                if key<best_key:
                    accepted=perf_counter()
                    if accepted>=deadline:raise Deadline
                    best,best_checked,best_key=candidate['routes'],full,key;stats['accepted_improvements']+=1
                    stats['accepted_moves'].append(dict(move=candidate['move'],full_snapshot=snapshot,
                        full_verified_at_perf_counter=verified,accepted_at_perf_counter=accepted))
    except Deadline:
        stats['deadline_reached']=True;stats['timeout_stage']=stats['active_stage']
    index=build_plan_index(problem,best)
    stats.update(final_key=list(best_key),final_cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),runtime_seconds=perf_counter()-started)
    return dict(routes=best,evaluation=best_checked,iterations=stats['complete_table_candidates'],metadata=stats)
