"""Causal critical requests and destination-conditioned whole DIRECT donors.

All clock effects come from real present-task DAG previews. Contiguous-block
geometry and no-wait clocks only rank proposals; they are never joint scores,
feasibility certificates or bounds on a completed objective.
"""
from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import build_plan_index
try:
    import critical_prefix_closure_v1 as closure
    import deadline_window_costs as costs
except ImportError:
    from . import critical_prefix_closure_v1 as closure
    from . import deadline_window_costs as costs

Deadline=closure.Deadline
PROXY_SCOPE='contiguous_PD_geometry_and_no_wait_path_proposal_only_not_joint_score_or_bound'


def _check(deadline,stats=None):
    closure._check(deadline,stats)


def _digest(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _rank(row):
    return (-row['maximum_positive_target_gain_min'],-row['total_positive_target_gain_min'],
        -int(row['newly_late']),-row['positive_delta_lateness_min'],-row['positive_delivery_delay_min'],row['task_id'])


def _surface(p,route,aircraft,task,pins,deadline,stats):
    """All capacity-legal contiguous PD gaps, respecting adjacent pin takes."""
    _check(deadline,stats);route=tuple(route);P=p.task(task).pickup;D=p.task(task).delivery
    points=[p.point_for_visit(v) for v in route];home=p.home_points()[aircraft];V=p.speed_km_per_min
    out_positions=[k for k,v in enumerate(route) if v>0 and (leg:=p.leg_registry.get(v))
        and leg.task_id in pins and leg.kind=='RELAY_OUT']
    forbidden={out_positions[0]+1} if len(out_positions)==2 and out_positions[1]==out_positions[0]+1 else set()
    previous=home;prefix_distance=0.;load=0;items=[]
    for gap in range(len(route)+1):
        _check(deadline,stats)
        if load<=1 and gap not in forbidden:
            distance=previous.distance_to(P)+P.distance_to(D)
            if gap<len(route):distance+=D.distance_to(points[gap])-previous.distance_to(points[gap])
            # This deliberately omits every cross-aircraft wait. No arrival
            # estimate is reported as an actual event clock.
            delivery=(prefix_distance/V+previous.distance_to(P)/V)+P.distance_to(D)/V
            items.append(dict(gap=gap,distance_increment_km=float(distance),
                no_wait_delivery_proxy_min=float(delivery)))
        if gap<len(route):
            prefix_distance+=previous.distance_to(points[gap]);previous=points[gap]
            load+=1 if route[gap]>0 else -1
    if not items:raise ValueError('complete legal route must offer a contiguous whole-DIRECT gap')
    if load:raise ValueError('proxy route has open cargo')
    geometric=min(items,key=lambda x:(x['distance_increment_km'],x['no_wait_delivery_proxy_min'],x['gap']))
    early=min(items,key=lambda x:(x['no_wait_delivery_proxy_min'],x['distance_increment_km'],x['gap']))
    stats['proxy_positions_scored']=stats.get('proxy_positions_scored',0)+len(items)
    return dict(aircraft=aircraft,inserted_task_id=task,route_after_removal_sha256=_digest(route),
        legal_contiguous_gaps=len(items),positions_retained=len({geometric['gap'],early['gap']}),
        skeleton_distance_km=float(prefix_distance),geometric=geometric,earliest_delivery=early,
        forbidden_pin_take_internal_gaps=sorted(forbidden),scoring_scope=PROXY_SCOPE)


def propose_catalogue(problem,base_routes,current_routes,*,pinned_task_ids=(),focus_task_ids=(),
        expected_signature=None,deadline,weights,scales,critical_limit=6,donor_limit=3,statistics=None):
    """Return finite conditioned donors without making a joint-route claim."""
    started=perf_counter();stats={} if statistics is None else statistics
    if not isinstance(stats,dict):raise ValueError('mutable statistics dictionary required')
    stats.update(completed=False,deadline_reached=False,proxy_positions_scored=0,
        proxy_surface_cache_hits=0,proxy_surface_cache_misses=0)
    try:
        if deadline is None or type(deadline) is bool or not isfinite(deadline):raise ValueError('finite absolute deadline required')
        _check(deadline,stats)
        if (type(critical_limit) is not int or not 0<=critical_limit<=6 or type(donor_limit) is not int or not 0<=donor_limit<=3):
            raise ValueError('bounded critical_limit<=6 and donor_limit<=3 required')
        if problem.capacity!=2:raise ValueError('actual capacity two required')
        w,s=tuple(weights),tuple(scales);costs.weighted_key(Score(0,0.,0.),w,s)
        source=closure.build_closure(problem,base_routes,current_routes,pinned_task_ids=pinned_task_ids,
            focus_task_ids=focus_task_ids,expected_signature=expected_signature,closure_limit=0,
            partner_depth=1,critical_native_limit=2,variant=0,deadline=deadline)
        _check(deadline,stats)
        routes=tuple(tuple(r) for r in current_routes);pins=frozenset(source['pinned_task_ids'])
        index=build_plan_index(problem,routes);owners=index.delivery_route_by_task
        rows={r['task_id']:r for r in source['per_task_evidence']};ranking=sorted(rows,key=lambda t:_rank(rows[t]))
        critical=[];directions=[]
        for target in source['targets']:
            _check(deadline,stats);name=target['target_id']
            ordered=sorted((t for t in target['causal_prefix_task_ids'] if rows[t]['signed_improvement_min'][name] is not None),
                key=lambda t:(-rows[t]['signed_improvement_min'][name],_rank(rows[t])))
            representative=ordered[0] if ordered else None
            if representative is not None and representative not in critical and len(critical)<critical_limit:critical.append(representative)
            retained=representative in critical
            directions.append(dict(target_id=name,ranked_task_ids=ordered,representative_task_id=representative,
                signed_gain_min=None if representative is None else rows[representative]['signed_improvement_min'][name],
                representative_retained=retained,omitted_reason=None if retained else ('no_defined_causal_candidate' if representative is None else 'critical_limit')))
        for t in ranking:
            _check(deadline,stats)
            if t not in critical and len(critical)<critical_limit:critical.append(t)
        parent_hash=_digest(routes)
        by_owner={a:sorted(t for t in rows if owners[t]==a) for a in range(problem.drone_count)}
        old_distance=[sum((problem.home_points()[a] if k==0 else problem.point_for_visit(route[k-1])).distance_to(problem.point_for_visit(v))
            for k,v in enumerate(route)) for a,route in enumerate(routes)]
        cache={};directories=[];records=[]
        def surface(route,a,t):
            _check(deadline,stats);key=(a,tuple(route),t)
            if key in cache:stats['proxy_surface_cache_hits']+=1;return cache[key]
            stats['proxy_surface_cache_misses']+=1
            result=_surface(problem,route,a,t,pins,deadline,stats)
            if len(cache)>=4096:cache.pop(next(iter(cache)))
            cache[key]=result;return result
        for t in critical:
            _check(deadline,stats);a=owners[t];route_a=tuple(v for v in routes[a] if abs(v)!=t)
            for b in range(problem.drone_count):
                _check(deadline,stats)
                if b==a:continue
                proposals=[]
                for d in by_owner[b]:
                    _check(deadline,stats)
                    route_b=tuple(v for v in routes[b] if abs(v)!=d)
                    returned=surface(route_a,a,d);incoming=surface(route_b,b,t)
                    constant=returned['skeleton_distance_km']+incoming['skeleton_distance_km']-old_distance[a]-old_distance[b]
                    geometric=constant+returned['geometric']['distance_increment_km']+incoming['geometric']['distance_increment_km']
                    proposals.append(dict(donor_task_id=d,donor_return_surface=returned,critical_destination_surface=incoming,
                        geometric_net_distance_proxy_km=float(geometric),
                        critical_early_delivery_proxy_min=incoming['earliest_delivery']['no_wait_delivery_proxy_min'],
                        donor_return_early_delivery_proxy_min=returned['earliest_delivery']['no_wait_delivery_proxy_min'],
                        scoring_scope=PROXY_SCOPE,full_joint_score=None,complete_solution=False))
                maps={p['donor_task_id']:p for p in proposals}
                geometry=sorted(maps,key=lambda d:(maps[d]['geometric_net_distance_proxy_km'],
                    maps[d]['critical_early_delivery_proxy_min'],maps[d]['donor_return_early_delivery_proxy_min'],d))
                early_t=sorted(maps,key=lambda d:(maps[d]['critical_early_delivery_proxy_min'],maps[d]['geometric_net_distance_proxy_km'],d))
                early_d=sorted(maps,key=lambda d:(maps[d]['donor_return_early_delivery_proxy_min'],maps[d]['geometric_net_distance_proxy_km'],d))
                channels={'pair_geometry':geometry,'critical_early_no_wait':early_t,'donor_return_early_no_wait':early_d}
                kept=[];retained_by={}
                for name,order in channels.items():
                    _check(deadline,stats)
                    if order:
                        d=order[0]
                        if d in kept:retained_by[d].append(name)
                        elif len(kept)<donor_limit:kept.append(d);retained_by[d]=[name]
                for d in geometry:
                    _check(deadline,stats)
                    if d not in kept and len(kept)<donor_limit:kept.append(d);retained_by[d]=['geometry_fill']
                number=len(directories);directory=dict(directory_index=number,t=t,a=a,b=b,
                    parent_routes_sha256=parent_hash,eligible_donor_task_ids=by_owner[b],
                    retained_donor_task_ids=kept,omitted_donor_task_ids=sorted(set(maps)-set(kept)),
                    eligible_donor_count=len(maps),retained_donor_count=len(kept),donor_rankings=channels,
                    per_donor_evidence=[maps[d] for d in sorted(maps)],
                    coverage_scope='all_current_destination_whole_DIRECT_donors_scored_by_finite_contiguous_proxy_only',
                    omitted_scope='not_retained_by_declared_proxy_channels_not_proved_infeasible_or_dominated')
                directories.append(directory)
                for d in kept:
                    _check(deadline,stats);q=[0]*problem.drone_count;q[a]+=1;q[b]+=1
                    record=dict(record_index=len(records),donor_directory_index=number,t=t,a=a,b=b,donor_task_id=d,
                        task_ids=sorted((t,d)),parent_routes_sha256=parent_hash,
                        target_owner_by_task={str(t):b,str(d):a},removal_quota_slots_by_aircraft=q,
                        removed_task_ids=[t,d],retained_by_channels=retained_by[d],
                        critical_rank_index=ranking.index(t),critical_target_ids=rows[t]['causal_target_ids'],
                        critical_target_clock_improvements_min=rows[t]['signed_improvement_min'],
                        geometric_net_distance_proxy_km=maps[d]['geometric_net_distance_proxy_km'],
                        full_joint_score=None,complete=False,complete_solution=False,global_objective_bound=False)
                    record['proposal_sha256']=_digest({k:record[k] for k in ('parent_routes_sha256','t','a','b','donor_task_id','target_owner_by_task')})
                    records.append(record)
        _check(deadline,stats)
        result=dict(records=records,donor_directories=directories,critical_task_ids=critical,
            critical_direction_representatives=directions,
            critical_task_ranking=ranking,critical_omitted_task_ids=sorted(set(rows)-set(critical)),
            critical_evidence=[rows[t] for t in critical],source_preview=source,
            source_preview_scope='all_eligible_actual_single_whole_deletion_effects_not_a_removal_U_constraint',
            parent_routes_sha256=parent_hash,pinned_task_ids=sorted(pins),focus_task_ids=source['focus_task_ids'],
            deadline_perf_counter=deadline,
            expected_signature=source['expected_signature'],eligible_native_count=len(rows),
            configuration=dict(critical_limit=critical_limit,donor_limit=donor_limit,partner_depth=1,critical_native_limit=2,
                critical_policy='defined_causal_target_direction_heads_then_actual_gain_damage_delay_ID_fill',
                destination_policy='every_other_actual_aircraft',
                donor_channels=list(channels) if directories else ['pair_geometry','critical_early_no_wait','donor_return_early_no_wait'],
                source_surface='all_capacity_legal_contiguous_PD_gaps_excluding_internal_double_take_gap',
                surface_cache_limit=4096,weights=list(w),scales=list(s)),
            complete=False,complete_solution=False,global_objective_bound=False,completed=True)
        stats.update(completed=True,runtime_seconds=perf_counter()-started,critical_retained=len(critical),
            destination_directories=len(directories),conditional_records=len(records),
            conditional_donors_scored=sum(x['eligible_donor_count'] for x in directories),
            conditional_donors_omitted=sum(len(x['omitted_donor_task_ids']) for x in directories))
        result['statistics']=dict(stats);_check(deadline,stats)
        result['completed_at_perf_counter']=perf_counter();_check(deadline,stats)
        return result
    except Deadline:
        stats.update(completed=False,deadline_reached=True,runtime_seconds=perf_counter()-started)
        raise
