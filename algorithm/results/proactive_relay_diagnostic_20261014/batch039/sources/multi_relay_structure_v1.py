"""Finite current-state multi-group proposals and generic structural guards.

Only complete atomic IN/OUT candidates are exported. Geometry and parent
clock estimates rank proposals; every new/old relay dependency must still be
globally scheduled and fully validated by the driver.
"""
from __future__ import annotations

from hashlib import sha256
from itertools import combinations
import json
from math import isfinite
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import deadline_window_costs as costs
except ImportError:
    from . import deadline_window_costs as costs

Deadline=costs.Deadline
PROXY_SCOPE='current_parent_geometry_and_clock_estimates_only_not_global_price_or_bound'


def _check(deadline,stats=None):
    if deadline is not None:
        if type(deadline) is bool or not isfinite(deadline):raise ValueError('finite absolute deadline required')
        if perf_counter()>=deadline:
            if stats is not None:stats['deadline_reached']=True
            raise Deadline


def _digest(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _task(p,visit):
    leg=p.leg_registry.get(abs(visit));return leg.task_id if leg else abs(visit)


def _layout(p,routes,deadline):
    routes=tuple(tuple(r) for r in routes)
    if p.capacity!=2 or len(routes)!=p.drone_count:raise ValueError('actual fleet and capacity two required')
    known=set(p.task_ids)|set(p.leg_registry)
    for route in routes:
        held=set();seen=set()
        for v in route:
            _check(deadline)
            if type(v) is not int or not v or abs(v) not in known:raise ValueError('known signed transport visit required')
            if v>0:
                if v in seen:raise ValueError('duplicate transport pickup')
                seen.add(v);held.add(v)
                if len(held)>2:raise ValueError('actual capacity exceeded')
            else:
                if -v not in held:raise ValueError('delivery before its actual pickup')
                held.remove(-v)
        if held:raise ValueError('open cargo at route end')
    index=build_plan_index(p,routes)
    counts=[sum(a==v for v in index.delivery_route_by_task.values()) for a in range(p.drone_count)]
    if any(n>p.max_tasks_per_drone for n in counts):raise ValueError('actual final responsibility cap exceeded')
    return routes,index,counts


def _group(p,group,number):
    names=('group_id','i','j','a','b','c','station_id')
    if not isinstance(group,dict) or any(type(group.get(k)) is not int for k in names):raise ValueError('integer group descriptor required')
    g={k:group[k] for k in names}
    if (g['group_id']!=number or not g['i']<g['j'] or g['i'] not in p._task_by_id or g['j'] not in p._task_by_id
        or len({g['a'],g['b'],g['c']})!=3 or any(not 0<=g[k]<p.drone_count for k in ('a','b','c'))
        or g['station_id'] not in {s.id for s in p.relay_stations}):raise ValueError('distinct actual group tasks/roles/site required')
    return g


def _plans_signature(p,routes,index,counts,groups):
    protected={g[k] for g in groups for k in ('i','j')}
    return dict(groups=groups,protected_task_ids=sorted(protected),
        plans=[[t,index.task_plans[t].mode,index.task_plans[t].relay_id,list(index.task_plans[t].leg_ids),
            index.delivery_route_by_task[t],index.inbound_route_by_task.get(t),index.outbound_route_by_task.get(t)] for t in sorted(protected)],
        selected_events=[[v for v in route if _task(p,v) in protected] for route in routes],final_counts=counts)


def _shape(p,routes,index,counts,groups,expected_signature,require_complete,deadline):
    normalized=[_group(p,g,k+1) for k,g in enumerate(groups)]
    tasks=[g[x] for g in normalized for x in ('i','j')]
    if len(set(tasks))!=len(tasks):raise ValueError('relay groups must use disjoint tasks')
    if set(index.relay_task_ids)!=set(tasks):raise ValueError('actual relay task set differs from declared groups')
    if require_complete and set(index.task_plans)!=set(p.task_ids):raise ValueError('complete actual task coverage required')
    if set(index.direct_task_ids)!=set(index.task_plans)-set(tasks):raise ValueError('every ordinary task must remain whole DIRECT')
    metrics=[]
    for g in normalized:
        _check(deadline);i,j,a,b,c,H=(g[k] for k in ('i','j','a','b','c','station_id'))
        for t,source in ((i,a),(j,b)):
            if (index.task_plans[t].mode!='RELAY' or index.task_plans[t].relay_id!=H
                or index.inbound_route_by_task[t]!=source or index.outbound_route_by_task[t]!=c
                or index.delivery_route_by_task[t]!=c):raise ValueError('actual group plan/role/site differs')
        incoming=[next(x for x in index.task_plans[t].leg_ids if p.leg_registry[x].kind=='RELAY_IN') for t in (i,j)]
        outgoing=[next(x for x in index.task_plans[t].leg_ids if p.leg_registry[x].kind=='RELAY_OUT') for t in (i,j)]
        takes=sorted(routes[c].index(x) for x in outgoing)
        if takes[1]!=takes[0]+1:raise ValueError('each group needs adjacent double OUT starts')
        held=set();previous=p.home_points()[c];shared=0.
        for k,v in enumerate(routes[c]):
            _check(deadline);point=p.point_for_visit(v)
            if k==takes[0] and held:raise ValueError('each receiver must enter its group doubletake empty')
            if set(outgoing)<=held:shared+=previous.distance_to(point)
            if v>0:held.add(v)
            else:held.remove(-v)
            previous=point
        if shared<=1e-10:raise ValueError('each group needs positive actual shared loaded flight')
        metrics.append(dict(group=g,take_positions=takes,shared_loaded_distance_km=float(shared),
            in_leg_ids=incoming,out_leg_ids=outgoing,
            source_drop_positions=[routes[a].index(-incoming[0]),routes[b].index(-incoming[1])]))
    if expected_signature is not None:
        old=expected_signature.get('groups') if isinstance(expected_signature,dict) else None
        if not isinstance(old,list) or normalized[:len(old)]!=old:raise ValueError('old group descriptors changed')
        actual=_plans_signature(p,routes,index,counts,old)
        if actual!=expected_signature:raise ValueError('old plans/roles/site/subsequences or final counts changed')
    return dict(groups=metrics,group_count=len(groups),relay_task_ids=sorted(tasks),cross_uav_count=len(tasks),
        final_counts=counts,ordinary_task_ids=sorted(index.direct_task_ids),complete_structure=bool(require_complete),
        complete_solution=False,global_DAG_checked=False)


def shape(problem,routes,groups,*,expected_signature=None,deadline=None,require_complete=True):
    routes,index,counts=_layout(problem,routes,deadline)
    value=_shape(problem,routes,index,counts,list(groups),expected_signature,require_complete,deadline)
    _check(deadline);return value


def signature(problem,routes,groups,*,deadline=None):
    routes,index,counts=_layout(problem,routes,deadline)
    groups=[_group(problem,g,k+1) for k,g in enumerate(groups)]
    _shape(problem,routes,index,counts,groups,None,True,deadline)
    value=_plans_signature(problem,routes,index,counts,groups);_check(deadline);return value


def _leg(p,t,H,kind):
    if p.task_relay_candidates and H not in p.task_relay_candidates.get(t,()):return None
    found=[x for x,l in p.leg_registry.items() if l.task_id==t and l.relay_id==H and l.kind==kind]
    if len(found)>1:raise ValueError('unique task/site transport legs required')
    return found[0] if found else None


def _loads(route):
    ans=[0]
    for v in route:ans.append(ans[-1]+(1 if v>0 else -1))
    return ans


def _distance(p,route,a):
    previous=p.home_points()[a];D=0.
    for v in route:point=p.point_for_visit(v);D+=previous.distance_to(point);previous=point
    return D


def _forbidden(p,route,groups,a):
    result=set()
    for g in groups:
        if g['c']!=a:continue
        out=[_leg(p,t,g['station_id'],'RELAY_OUT') for t in (g['i'],g['j'])]
        positions=sorted(route.index(x) for x in out)
        if positions[1]!=positions[0]+1:raise ValueError('current group take adjacency broken')
        result.add(positions[0]+1)
    return result


def _retain_positions(rows,position_limit,option_limit,first_clock,second_clock,deadline):
    streams=(sorted(rows,key=lambda x:(x['delta_distance_km'],x[first_clock],x[second_clock],x['gap'],x['end_gap'],x.get('first_task_id',0))),
        sorted(rows,key=lambda x:(x[first_clock],x['delta_distance_km'],x['gap'],x['end_gap'],x.get('first_task_id',0))),
        sorted(rows,key=lambda x:(x[second_clock],x['delta_distance_km'],x['gap'],x['end_gap'],x.get('first_task_id',0))))
    def keep(limit,pools):
        output=[];seen=set()
        for row in (*[s[0] for s in pools if s],*pools[0]):
            _check(deadline);identity=(row['gap'],row['end_gap'],row.get('first_task_id'))
            if identity not in seen and len(output)<limit:seen.add(identity);output.append(row)
        return output
    shortlist=keep(position_limit,streams)
    finalstreams=tuple(sorted(shortlist,key=lambda x:(x['delta_distance_km'],x[first_clock],x[second_clock],x['gap'],x['end_gap'],x.get('first_task_id',0))) if k==0 else sorted(shortlist,key=lambda x:(x[first_clock if k==1 else second_clock],x['delta_distance_km'],x['gap'],x['end_gap'],x.get('first_task_id',0))) for k in range(3))
    return keep(option_limit,finalstreams)


def _in_rows(p,route,a,t,H,groups,parent_schedule,deadline,*,contiguous_only=False):
    incoming=_leg(p,t,H,'RELAY_IN')
    if incoming is None:return []
    n=len(route);points=[p.point_for_visit(v) for v in route];loads=_loads(route);banned=_forbidden(p,route,groups,a)
    P=p.task(t).pickup;R=next(s.point for s in p.relay_stations if s.id==H);V=p.speed_km_per_min;rows=[]
    for g in range(n+1):
        _check(deadline)
        if loads[g]>=2 or g in banned:continue
        u=p.home_points()[a] if g==0 else points[g-1]
        start=(0. if g==0 else parent_schedule.event_times_min[a,g-1])+u.distance_to(P)/V
        for h in range(g,g+1 if contiguous_only else n+1):
            _check(deadline)
            if loads[h]>=2:break
            if h in banned:continue
            if h==g:
                delta=u.distance_to(P)+P.distance_to(R)
                if h<n:delta+=R.distance_to(points[h])-u.distance_to(points[h])
                end=start+P.distance_to(R)/V
            else:
                first=u.distance_to(P)+P.distance_to(points[g])-u.distance_to(points[g])
                delta=first+points[h-1].distance_to(R)
                if h<n:delta+=R.distance_to(points[h])-points[h-1].distance_to(points[h])
                end=parent_schedule.event_times_min[a,h-1]+first/V+points[h-1].distance_to(R)/V
            rows.append(dict(gap=g,end_gap=h,delta_distance_km=float(delta),estimated_pickup_min=float(start),
                estimated_drop_min=float(end),in_leg_id=incoming,task_id=t,aircraft=a,station_id=H,scoring_scope=PROXY_SCOPE))
    return rows


def _out_rows(p,route,c,pair,H,groups,parent_routes,parent_schedule,estimated_availability,deadline,*,contiguous_only=False):
    i,j=pair;out=[_leg(p,t,H,'RELAY_OUT') for t in pair]
    if any(x is None for x in out):return []
    n=len(route);points=[p.point_for_visit(v) for v in route];loads=_loads(route);banned=_forbidden(p,route,groups,c)
    R=next(s.point for s in p.relay_stations if s.id==H);V=p.speed_km_per_min
    original={v:k for k,v in enumerate(parent_routes[c])};rows=[]
    for g in range(n+1):
        _check(deadline)
        if loads[g] or g in banned:continue
        u=p.home_points()[c] if not g else points[g-1]
        prefix=0. if not g else parent_schedule.event_times_min[c,original[route[g-1]]]
        arrival=prefix+u.distance_to(R)/V;T=max(*estimated_availability,arrival)
        for first,last in ((i,j),(j,i)):
            D1,D2=p.task(first).delivery,p.task(last).delivery
            if R.distance_to(D1)<=1e-10:continue
            firsttime=T+R.distance_to(D1)/V;middle_time=firsttime
            for h in range(g,g+1 if contiguous_only else n+1):
                _check(deadline)
                if loads[h]>=2:break
                if h>g:
                    middle_time+=(D1.distance_to(points[g]) if h==g+1 else points[h-2].distance_to(points[h-1]))/V
                if h in banned:continue
                if h==g:
                    delta=u.distance_to(R)+R.distance_to(D1)+D1.distance_to(D2)
                    if h<n:delta+=D2.distance_to(points[h])-u.distance_to(points[h])
                    end=firsttime+D1.distance_to(D2)/V
                else:
                    head=u.distance_to(R)+R.distance_to(D1)+D1.distance_to(points[g])-u.distance_to(points[g])
                    tail=points[h-1].distance_to(D2)
                    if h<n:tail+=D2.distance_to(points[h])-points[h-1].distance_to(points[h])
                    delta=head+tail
                    end=middle_time+points[h-1].distance_to(D2)/V
                rows.append(dict(gap=g,end_gap=h,first_task_id=first,last_task_id=last,delta_distance_km=float(delta),
                    estimated_first_delivery_min=float(firsttime),estimated_second_delivery_min=float(end),
                    estimated_receiver_arrival_min=float(arrival),estimated_second_take_min=float(T),
                    out_leg_ids=out,scoring_scope=PROXY_SCOPE))
    return rows


def _in_route(route,row):
    g,h,x=row['gap'],row['end_gap'],row['in_leg_id']
    return route[:g]+(x,)+route[g:h]+(-x,)+route[h:]


def _out_route(route,row,pair):
    g,h=row['gap'],row['end_gap'];out=dict(zip(pair,row['out_leg_ids']))
    return route[:g]+tuple(row['out_leg_ids'])+(-out[row['first_task_id']],)+route[g:h]+(-out[row['last_task_id']],)+route[h:]


def _family_key(row,early=False):
    return ((row['estimated_second_take_proxy_min'],row['geometry_proxy_delta_km']) if early else
        (row['geometry_proxy_delta_km'],row['estimated_second_take_proxy_min']))+tuple(row[k] for k in ('i','j','a','b','c','station_id'))


def _pool(rows,limit,deadline):
    geometry=sorted(rows,key=_family_key);early=sorted(rows,key=lambda x:_family_key(x,True));answer=[];seen=set()
    for depth in range(max(len(geometry),len(early))):
        for order in (geometry,early):
            _check(deadline)
            if depth>=len(order):continue
            row=order[depth];identity=tuple(row[k] for k in ('i','j','a','b','c','station_id'))
            if identity not in seen and len(answer)<limit:seen.add(identity);answer.append(row)
        if len(answer)>=limit:break
    return answer


def propose_catalogue(problem,parent_routes,groups,*,deadline,weights,scales,max_families=16,
        source_option_limit=3,receiver_option_limit=3,position_limit=12,feeder_limit=3,
        base_pool_limit=64,statistics=None):
    started=perf_counter();stats={} if statistics is None else statistics
    if not isinstance(stats,dict):raise ValueError('mutable statistics dictionary required')
    stats.update(completed=False,deadline_reached=False,source_profile_positions=0,source_profiles_scored=0,
        feeder_profiles_omitted=0,current_same_owner_pairs=0,role_site_families_proxy_scored=0,
        source_full_positions=0,receiver_full_positions=0,source_positions_omitted=0,receiver_positions_omitted=0,
        complete_atomic_candidates=0,duplicate_candidates_omitted=0,structural_candidates_rejected=0)
    try:
        if deadline is None or type(deadline) is bool or not isfinite(deadline):raise ValueError('finite absolute deadline required')
        bounds=((max_families,64),(source_option_limit,3),(receiver_option_limit,3),(position_limit,256),(feeder_limit,7),(base_pool_limit,512))
        if any(type(v) is not int or not 1<=v<=maximum for v,maximum in bounds) or max(source_option_limit,receiver_option_limit)>position_limit:
            raise ValueError('positive finite multi-group proposal bounds required')
        costs.weighted_key(Score(0,0.,0.),tuple(weights),tuple(scales));_check(deadline,stats)
        routes,index,counts=_layout(problem,parent_routes,deadline);current_shape=shape(problem,routes,groups,deadline=deadline)
        old_signature=signature(problem,routes,groups,deadline=deadline)
        checked=evaluate_solution(problem,routes,_force_full=True);_check(deadline,stats)
        schedule=GlobalRelayEvaluator(problem).evaluate(routes);_check(deadline,stats)
        if not checked.valid or not schedule.feasible:raise ValueError('actual complete feasible parent required')
        groups=old_signature['groups'];by_owner={a:sorted(t for t in index.direct_task_ids if index.delivery_route_by_task[t]==a) for a in range(problem.drone_count)}
        stations=sorted(problem.relay_stations,key=lambda s:s.id)
        station_caps={s.id:base_pool_limit//len(stations)+int(k<base_pool_limit%len(stations)) for k,s in enumerate(stations)}
        profiles={};feeders={};pools={s.id:[] for s in stations};source_cache={}
        def available(t,H):return _leg(problem,t,H,'RELAY_IN') is not None and _leg(problem,t,H,'RELAY_OUT') is not None
        def profile(t,H):
            key=(t,H)
            if key in feeders:return feeders[key]
            owner=index.delivery_route_by_task[t];options=[]
            for a in range(problem.drone_count):
                _check(deadline,stats)
                if a==owner:continue
                rows=_in_rows(problem,routes[a],a,t,H,groups,schedule,deadline,contiguous_only=True)
                stats['source_profile_positions']+=len(rows);stats['source_profiles_scored']+=1
                if not rows:continue
                geo=min(rows,key=lambda x:(x['delta_distance_km'],x['estimated_drop_min'],x['gap']))
                early=min(rows,key=lambda x:(x['estimated_drop_min'],x['delta_distance_km'],x['gap']))
                item=dict(aircraft=a,geometry=geo,earliest=early);options.append(item);profiles[t,a,H]=item
            geometry=sorted(options,key=lambda x:(x['geometry']['delta_distance_km'],x['geometry']['estimated_drop_min'],x['aircraft']))
            early=sorted(options,key=lambda x:(x['earliest']['estimated_drop_min'],x['earliest']['delta_distance_km'],x['aircraft']))
            kept=[]
            for item in (*[x[0] for x in (geometry,early) if x],*geometry):
                if item['aircraft'] not in kept and len(kept)<feeder_limit:kept.append(item['aircraft'])
            stats['feeder_profiles_omitted']+=len(options)-len(kept);feeders[key]=kept;return kept
        for c,tasks in by_owner.items():
            for i,j in combinations(tasks,2):
                _check(deadline,stats);stats['current_same_owner_pairs']+=1
                native=tuple(v for v in routes[c] if abs(v) not in (i,j));removed_delta=_distance(problem,native,c)-_distance(problem,routes[c],c)
                for station in stations:
                    H=station.id;_check(deadline,stats)
                    if not available(i,H) or not available(j,H):continue
                    for a in profile(i,H):
                        for b in profile(j,H):
                            _check(deadline,stats)
                            if a==b:continue
                            left,right=profiles[i,a,H]['geometry'],profiles[j,b,H]['geometry']
                            rows=_out_rows(problem,native,c,(i,j),H,groups,routes,schedule,
                                (left['estimated_drop_min'],right['estimated_drop_min']),deadline,contiguous_only=True)
                            if not rows:continue
                            best=min(rows,key=lambda x:(x['delta_distance_km'],x['estimated_second_take_min'],x['gap'],x['first_task_id']))
                            family=dict(i=i,j=j,a=a,b=b,c=c,station_id=H,
                                geometry_proxy_delta_km=float(left['delta_distance_km']+right['delta_distance_km']+removed_delta+best['delta_distance_km']),
                                estimated_second_take_proxy_min=best['estimated_second_take_min'],
                                source_profile_gaps=[left['gap'],right['gap']],receiver_profile_gap=best['gap'],
                                receiver_profile_first_task_id=best['first_task_id'],scoring_scope=PROXY_SCOPE)
                            stats['role_site_families_proxy_scored']+=1;pools[H].append(family)
                            if len(pools[H])>2*station_caps[H]:pools[H]=_pool(pools[H],station_caps[H],deadline)
        pools={H:_pool(rows,station_caps[H],deadline) for H,rows in pools.items()}
        families=[]
        for depth in range(max((len(x) for x in pools.values()),default=0)):
            for station in stations:
                _check(deadline,stats);rows=pools[station.id]
                if depth<len(rows) and len(families)<max_families:families.append(rows[depth])
        candidates=[];seen=set();receiver_cache={}
        for fi,f in enumerate(families):
            _check(deadline,stats);i,j,a,b,c,H=(f[k] for k in ('i','j','a','b','c','station_id'))
            selected=[]
            for t,owner in ((i,a),(j,b)):
                key=(t,owner,H)
                if key not in source_cache:
                    rows=_in_rows(problem,routes[owner],owner,t,H,groups,schedule,deadline)
                    chosen=_retain_positions(rows,position_limit,source_option_limit,'estimated_pickup_min','estimated_drop_min',deadline)
                    stats['source_full_positions']+=len(rows);stats['source_positions_omitted']+=len(rows)-len(chosen);source_cache[key]=chosen
                selected.append(source_cache[key])
            native=tuple(v for v in routes[c] if abs(v) not in (i,j))
            for left in selected[0]:
                for right in selected[1]:
                    _check(deadline,stats);cachekey=(i,j,c,H,left['estimated_drop_min'],right['estimated_drop_min'])
                    if cachekey not in receiver_cache:
                        rows=_out_rows(problem,native,c,(i,j),H,groups,routes,schedule,
                            (left['estimated_drop_min'],right['estimated_drop_min']),deadline)
                        chosen=_retain_positions(rows,position_limit,receiver_option_limit,'estimated_first_delivery_min','estimated_second_delivery_min',deadline)
                        stats['receiver_full_positions']+=len(rows);stats['receiver_positions_omitted']+=len(rows)-len(chosen);receiver_cache[cachekey]=chosen
                    for out in receiver_cache[cachekey]:
                        _check(deadline,stats);child=list(routes);child[a]=_in_route(routes[a],left);child[b]=_in_route(routes[b],right);child[c]=_out_route(native,out,(i,j));child=tuple(child)
                        new=dict(group_id=len(groups)+1,**{k:f[k] for k in ('i','j','a','b','c','station_id')})
                        try:shape(problem,child,[*groups,new],expected_signature=old_signature,deadline=deadline)
                        except (ValueError,KeyError,IndexError):stats['structural_candidates_rejected']+=1;continue
                        identity=_digest(child)
                        if identity in seen:stats['duplicate_candidates_omitted']+=1;continue
                        seen.add(identity);stats['complete_atomic_candidates']+=1
                        candidates.append(dict(routes=[list(r) for r in child],new_group=new,family_index=fi,
                            candidate_index=len(candidates),routes_sha256=identity,
                            source_layouts=[left,right],receiver_layout=out,
                            scoring_scope=PROXY_SCOPE,complete_atomic_transport_plans=True,full_global_checked=False))
        _check(deadline,stats)
        result=dict(families=families,complete_candidates=candidates,parent_routes_sha256=_digest(routes),
            parent_signature=old_signature,parent_shape=current_shape,
            metadata=dict(configuration=dict(max_families=max_families,source_option_limit=source_option_limit,
                receiver_option_limit=receiver_option_limit,position_limit=position_limit,feeder_limit=feeder_limit,
                base_pool_limit=base_pool_limit,station_pool_limits={str(H):v for H,v in station_caps.items()},
                family_pool_policy='bounded_geometry_and_estimated_take_heads_per_station_then_station_round_robin',
                source_position_policy='capacity_legal_all_g_le_h_geometry_parent_clock_frontier',
                receiver_position_policy='empty_doubletake_first_delivery_native_interval_second_delivery',
                scoring_scope=PROXY_SCOPE,uses_pure_DIRECT_suffix_prices=False),
                station_pool_counts={str(H):len(v) for H,v in pools.items()},ordinary_task_ids=sorted(index.direct_task_ids),
                current_group_count=len(groups),eligible_pair_scope='all_current_same_final_owner_whole_DIRECT_pairs',
                complete_candidate_scope='atomic_all_tasks_present_structural_only_driver_must_full_global_validate'),completed=True)
        stats.update(completed=True,runtime_seconds=perf_counter()-started)
        result['metadata']['statistics']=dict(stats);result['completed_at_perf_counter']=perf_counter();result['deadline_perf_counter']=deadline
        _check(deadline,stats);return result
    except Deadline:
        stats.update(completed=False,deadline_reached=True,runtime_seconds=perf_counter()-started);raise
