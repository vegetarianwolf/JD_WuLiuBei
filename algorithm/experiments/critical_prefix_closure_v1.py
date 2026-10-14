"""Real causal-prefix previews and real whole-task quota slots.

Partial schedules describe exactly their present task set. They are never
full-validator certificates or bounds for the missing tasks' completed J.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter

from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import deadline_window_costs as costs
    import relay_damage_closure as damage
except ImportError:
    from . import deadline_window_costs as costs
    from . import relay_damage_closure as damage

Deadline=costs.Deadline
EPS=1e-9
SCOPE='physical_DAG_preview_on_exact_present_task_set_only'


def _check(deadline,statistics=None):
    if deadline is not None:
        if type(deadline) is bool or not isfinite(deadline):raise ValueError('finite absolute deadline or None required')
        if perf_counter()>=deadline:
            if statistics is not None:statistics['deadline_reached']=True
            raise Deadline


def _count(stats,key,n=1):
    if stats is not None:stats[key]=stats.get(key,0)+n


def _ids(p,values,label):
    result=tuple(values)
    if len(set(result))!=len(result) or any(type(t) is not int or t not in p._task_by_id for t in result):
        raise ValueError('distinct known '+label+' IDs required')
    return result


def _pins(p,values):
    result=_ids(p,values,'pinned task')
    if len(result) not in (0,2):raise ValueError('zero DIRECT pins or two actual relay pins required')
    return frozenset(result)


def _task(p,v):
    leg=p.leg_registry.get(abs(v))
    return leg.task_id if leg else abs(v)


def _layout(p,routes,deadline=None):
    routes=tuple(tuple(r) for r in routes)
    if len(routes)!=p.drone_count:raise ValueError('one route per actual aircraft required')
    known=set(p.task_ids);registry=p.leg_registry or {};known_visits=known|registry.keys()
    for route in routes:
        held=set();seen=set()
        for v in route:
            _check(deadline)
            if type(v) is not int or not v or abs(v) not in known_visits:raise ValueError('known signed transport visits required')
            if v>0:
                if v in seen:raise ValueError('duplicate pickup')
                seen.add(v);held.add(v)
                if len(held)>p.capacity:raise ValueError('actual route capacity exceeded')
            else:
                if -v not in held:raise ValueError('delivery without held pickup')
                held.remove(-v)
        if held:raise ValueError('open transport cargo at route end')
    index=build_plan_index(p,routes)
    if not set(index.task_plans)<=known:raise ValueError('unknown task plan')
    counts=[sum(a==v for v in index.delivery_route_by_task.values()) for a in range(p.drone_count)]
    if any(n>p.max_tasks_per_drone for n in counts):raise ValueError('actual final responsibility cap exceeded')
    return routes,index,counts


def _shape(p,routes,index,pins):
    if set(index.relay_task_ids)!=set(pins):raise ValueError('actual relay set differs from pinned relay identities')
    if not pins:return dict(required_cross_task_ids=[],receiver=None,source_aircraft=[],station_id=None,
        take_positions=[],shared_loaded_distance_km=0.)
    if any(t not in index.relay_task_ids or index.inbound_route_by_task[t]==index.outbound_route_by_task[t] for t in pins):
        raise ValueError('required pin lost actual cross-aircraft handoff')
    sources={index.inbound_route_by_task[t] for t in pins};receivers={index.outbound_route_by_task[t] for t in pins}
    stations={index.task_plans[t].relay_id for t in pins}
    if len(sources)!=2 or len(receivers)!=1 or len(stations)!=1 or sources&receivers:
        raise ValueError('two distinct sources and a third same-site receiver required')
    c=next(iter(receivers));out={next(v for v in index.task_plans[t].leg_ids if p.leg_registry[v].kind=='RELAY_OUT') for t in pins}
    positions=sorted(routes[c].index(v) for v in out)
    if positions[1]!=positions[0]+1:raise ValueError('receiver OUT starts must remain consecutive')
    held=set();previous=p.home_points()[c];shared=0.
    for k,v in enumerate(routes[c]):
        point=p.point_for_visit(v)
        if k==positions[0] and held:raise ValueError('receiver must enter the two OUT starts empty')
        if out<=held:shared+=previous.distance_to(point)
        if v>0:held.add(v)
        else:held.remove(-v)
        previous=point
    if shared<=1e-10:raise ValueError('positive actual two-parcel shared flight required')
    return dict(required_cross_task_ids=sorted(pins),receiver=c,source_aircraft=sorted(sources),
        station_id=next(iter(stations)),take_positions=positions,shared_loaded_distance_km=float(shared))


def shape(problem,routes,pinned_task_ids=(),*,deadline=None):
    pins=_pins(problem,pinned_task_ids);routes,index,_=_layout(problem,routes,deadline)
    result=_shape(problem,routes,index,pins);_check(deadline)
    return result


def _signature(p,routes,index,counts,pins):
    return dict(plans=[[t,index.task_plans[t].mode,index.task_plans[t].relay_id,
        list(index.task_plans[t].leg_ids),index.delivery_route_by_task[t],index.inbound_route_by_task.get(t),
        index.outbound_route_by_task.get(t)] for t in sorted(pins)],
        selected_events=[[v for v in r if _task(p,v) in pins] for r in routes],
        final_counts=list(counts),relay_task_ids=sorted(index.relay_task_ids))


def signature(problem,routes,pinned_task_ids=(),*,deadline=None):
    pins=_pins(problem,pinned_task_ids);routes,index,counts=_layout(problem,routes,deadline)
    _shape(problem,routes,index,pins)
    result=_signature(problem,routes,index,counts,pins);_check(deadline)
    return result


def partial_preview(problem,routes,*,pinned_task_ids=(),expected_signature,remaining_task_ids,
                    deadline,remaining_quota_slots=None,global_evaluator=None,statistics=None):
    """Check the actual present-set DAG, never silently ignore full invalidity."""
    _check(deadline,statistics);pins=_pins(problem,pinned_task_ids)
    remaining=_ids(problem,remaining_task_ids,'remaining task')
    if pins&set(remaining):raise ValueError('pinned relay plan cannot be missing')
    routes,index,counts=_layout(problem,routes,deadline)
    present=set(problem.task_ids)-set(remaining)
    if set(index.task_plans)!=present:raise ValueError('actual partial coverage differs from exact present_task set')
    actual=_signature(problem,routes,index,counts,pins)
    if not isinstance(expected_signature,dict) or any(actual[k]!=expected_signature.get(k) for k in ('plans','selected_events','relay_task_ids')):
        raise ValueError('pinned modes/legs/roles/site/selected subsequences changed')
    expected=expected_signature.get('final_counts')
    if (not isinstance(expected,(tuple,list)) or len(expected)!=problem.drone_count
        or any(type(n) is not int or not 0<=n<=problem.max_tasks_per_drone for n in expected)
        or sum(expected)!=len(problem.task_ids)):
        raise ValueError('genuine full expected responsibility counts required')
    quota=[n-k for n,k in zip(expected,counts)]
    if any(q<0 for q in quota) or sum(quota)!=len(remaining):raise ValueError('remaining real final-owner quotas are inconsistent')
    if remaining_quota_slots is not None and list(remaining_quota_slots)!=quota:raise ValueError('supplied quota slots differ from actual present owners')
    current_shape=_shape(problem,routes,index,pins)
    evaluator=GlobalRelayEvaluator(problem) if global_evaluator is None else global_evaluator
    if getattr(evaluator,'problem',None) is not problem:raise ValueError('global evaluator belongs to another problem')
    _check(deadline,statistics);schedule=evaluator.evaluate(routes);_count(statistics,'partial_global_evaluations')
    _check(deadline,statistics)
    if not schedule.feasible or set(schedule.delivery_times_min)!=present:raise ValueError('actual partial DAG is infeasible or has wrong coverage')
    if not all(isfinite(v) for v in (schedule.score.total_lateness_min,schedule.score.distance_km,*schedule.event_times_min.values())):
        raise ValueError('nonfinite actual partial clock or score')
    return dict(routes=routes,index=index,score=schedule.score,schedule=schedule,global_schedule=schedule,
        present_task_ids=sorted(present),remaining_task_ids=sorted(remaining),final_owners=dict(index.delivery_route_by_task),
        final_counts=counts,remaining_quota_slots=quota,shape=current_shape,complete=False,complete_solution=False,
        full_task_coverage=not remaining,standard_full_validator_used=False,scoring_scope=SCOPE)


def preview_evidence(preview):
    schedule=preview['global_schedule'];routes=preview['routes'];score=preview['score']
    return dict(routes=[list(r) for r in routes],present_task_ids=preview['present_task_ids'],
        remaining_task_ids=preview['remaining_task_ids'],final_owners={str(t):a for t,a in sorted(preview['final_owners'].items())},
        final_counts=preview['final_counts'],remaining_quota_slots=preview['remaining_quota_slots'],shape=preview['shape'],
        score=dict(late_count=score.late_count,total_lateness_min=float(score.total_lateness_min),distance_km=float(score.distance_km)),
        event_times_min=[[schedule.event_times_min[a,k] for k in range(len(r))] for a,r in enumerate(routes)],
        delivery_times_min={str(t):v for t,v in sorted(schedule.delivery_times_min.items())},
        complete=False,complete_solution=False,full_task_coverage=preview['full_task_coverage'],
        standard_full_validator_used=False,scoring_scope=SCOPE)


def _delivery_node(p,routes,index,t):
    a=index.delivery_route_by_task[t];plan=index.task_plans[t]
    identifier=t if plan.mode=='DIRECT' else next(v for v in plan.leg_ids if p.leg_registry[v].kind=='RELAY_OUT')
    return a,routes[a].index(-identifier)


def _arrival(p,routes,schedule,current_shape):
    c=current_shape['receiver'];g=current_shape['take_positions'][0]
    H=next(s.point for s in p.relay_stations if s.id==current_shape['station_id'])
    before=p.home_points()[c] if g==0 else p.point_for_visit(routes[c][g-1])
    return (0. if g==0 else schedule.event_times_min[c,g-1])+before.distance_to(H)/p.speed_km_per_min


def _ancestors(p,routes,index,node,eligible,*,include_node=False,deadline=None):
    seen=set();todo=[node] if include_node else ([] if node[1]==0 else [(node[0],node[1]-1)])
    # A final delivery's predecessor walk will encounter OUT pickup dependency.
    while todo:
        _check(deadline);a,k=todo.pop()
        if (a,k) in seen:continue
        seen.add((a,k));v=routes[a][k]
        if k:todo.append((a,k-1))
        leg=p.leg_registry.get(v) if v>0 else None
        if leg and leg.kind=='RELAY_OUT':
            source=index.inbound_route_by_task[leg.task_id]
            incoming=next(x for x in index.task_plans[leg.task_id].leg_ids if p.leg_registry[x].kind=='RELAY_IN')
            todo.append((source,routes[source].index(-incoming)))
    return sorted({_task(p,routes[a][k]) for a,k in seen}&eligible)


def _targets(p,routes,index,schedule,pins,focus,eligible,critical_native_limit,deadline):
    result=[];current_shape=_shape(p,routes,index,pins)
    if pins:
        for t in sorted(pins):
            a=index.inbound_route_by_task[t];incoming=next(v for v in index.task_plans[t].leg_ids if p.leg_registry[v].kind=='RELAY_IN')
            k=routes[a].index(-incoming)
            result.append(dict(target_id='in_drop:'+str(t),type='in_drop',task_id=t,aircraft=a,position=k,
                clock_min=schedule.event_times_min[a,k],causal_prefix_task_ids=_ancestors(p,routes,index,(a,k),eligible,deadline=deadline),ranking_only=True))
        c=current_shape['receiver'];g=current_shape['take_positions'][0]
        result.append(dict(target_id='receiver_arrival',type='receiver_arrival',task_id=None,aircraft=c,position=g,
            clock_min=_arrival(p,routes,schedule,current_shape),
            causal_prefix_task_ids=_ancestors(p,routes,index,(c,g),eligible,deadline=deadline),ranking_only=True))
    chosen=list(dict.fromkeys((*focus,*sorted(pins))))
    critical=sorted(eligible-set(chosen),key=lambda t:(abs(schedule.delivery_times_min[t]-p.task(t).deadline_min),p.task(t).deadline_min,t))
    chosen.extend(critical[:critical_native_limit])
    for t in chosen:
        a,k=_delivery_node(p,routes,index,t)
        result.append(dict(target_id='delivery:'+str(t),type='delivery',task_id=t,aircraft=a,position=k,
            clock_min=schedule.delivery_times_min[t],deadline_min=p.task(t).deadline_min,
            selection_origin='focus' if t in focus or t in pins else 'nearest_actual_deadline_margin',
            causal_prefix_task_ids=_ancestors(p,routes,index,(a,k),eligible,deadline=deadline),ranking_only=True))
    return result


def _target_clocks(p,preview,targets):
    routes=preview['routes'];index=preview['index'];schedule=preview['global_schedule'];result={};undefined=[]
    for target in targets:
        key=target['target_id'];t=target['task_id']
        if target['type']=='delivery':
            if t not in schedule.delivery_times_min:result[key]=None;undefined.append(key)
            else:result[key]=schedule.delivery_times_min[t]
        elif target['type']=='in_drop':
            a=index.inbound_route_by_task[t];incoming=next(v for v in index.task_plans[t].leg_ids if p.leg_registry[v].kind=='RELAY_IN')
            result[key]=schedule.event_times_min[a,routes[a].index(-incoming)]
        else:result[key]=_arrival(p,routes,schedule,preview['shape'])
    return result,undefined


def _whole_remove(p,routes,tasks):
    removed=set(tasks)
    return tuple(tuple(v for v in route if _task(p,v) not in removed) for route in routes)


def _complete(p,routes,evaluator,deadline,stats):
    _check(deadline,stats);routes,index,counts=_layout(p,routes,deadline)
    checked=evaluate_solution(p,routes,_force_full=True);_check(deadline,stats)
    if not checked.valid or set(index.task_plans)!=set(p.task_ids):raise ValueError('valid real complete base/seed required')
    schedule=evaluator.evaluate(routes);_check(deadline,stats)
    if not schedule.feasible or set(schedule.delivery_times_min)!=set(p.task_ids):raise ValueError('complete actual global schedule required')
    return routes,index,counts,schedule


def build_closure(problem,base_routes,seed_routes,*,pinned_task_ids=(),focus_task_ids=(),expected_signature=None,
                  closure_limit=12,variant=0,deadline,partner_depth=1,critical_native_limit=2,statistics=None):
    """All eligible single-whole previews, real donor-first slots, no fake comp."""
    started=perf_counter();stats={} if statistics is None else statistics
    if not isinstance(stats,dict):raise ValueError('mutable statistics dictionary required')
    stats.update(completed=False,deadline_reached=False)
    try:
        _check(deadline,stats);pins=_pins(problem,pinned_task_ids);focus=_ids(problem,focus_task_ids,'focus')
        if (type(closure_limit) is not int or closure_limit<0 or type(variant) is not int or variant<0
            or type(partner_depth) is not int or partner_depth!=1
            or type(critical_native_limit) is not int or not 0<=critical_native_limit<=8):raise ValueError('bounded nonnegative closure/variant/critical limit required')
        evaluator=GlobalRelayEvaluator(problem)
        base,base_index,base_counts,base_schedule=_complete(problem,base_routes,evaluator,deadline,stats)
        raw,index,raw_counts,schedule=_complete(problem,seed_routes,evaluator,deadline,stats)
        if base_index.relay_task_ids:raise ValueError('genuine original base must be pure DIRECT')
        if base_counts!=raw_counts:raise ValueError('seed final-owner counts differ from genuine base')
        _shape(problem,raw,index,pins)
        actual_signature=_signature(problem,raw,index,raw_counts,pins)
        if expected_signature is not None and expected_signature!=actual_signature:raise ValueError('supplied original seed signature differs')
        expected_signature=actual_signature
        eligible=set(index.direct_task_ids)-pins
        if eligible!=set(problem.task_ids)-pins:raise ValueError('every unpinned task must be a complete whole DIRECT')
        targets=_targets(problem,raw,index,schedule,pins,focus,eligible,critical_native_limit,deadline)
        old_graph,*_=damage._links(problem,base,pins,(),deadline)
        graph,*_=damage._links(problem,raw,pins,(),deadline)
        partners=damage._partners(graph);old_partners=damage._partners(old_graph)
        rows={}
        for t in sorted(eligible):
            _check(deadline,stats)
            preview=partial_preview(problem,_whole_remove(problem,raw,(t,)),pinned_task_ids=pins,
                expected_signature=expected_signature,remaining_task_ids=(t,),deadline=deadline,
                global_evaluator=evaluator,statistics=stats)
            after,undefined=_target_clocks(problem,preview,targets)
            changes={target['target_id']:(None if after[target['target_id']] is None else target['clock_min']-after[target['target_id']]) for target in targets}
            now=schedule.delivery_times_min[t];before=base_schedule.delivery_times_min[t];due=problem.task(t).deadline_min
            old_late=before-due if before-due>EPS else 0.;late=now-due if now-due>EPS else 0.
            causal=[target['target_id'] for target in targets if t in target['causal_prefix_task_ids']]
            gains=[max(0.,v) for v in changes.values() if v is not None]
            rows[t]=dict(task_id=t,base_aircraft=base_index.delivery_route_by_task[t],raw_aircraft=index.delivery_route_by_task[t],
                base_delivery_min=before,raw_delivery_min=now,base_lateness_min=old_late,raw_lateness_min=late,
                delta_lateness_min=late-old_late,positive_delta_lateness_min=max(0.,late-old_late),newly_late=old_late==0. and late>0.,
                signed_delivery_delay_min=now-before,positive_delivery_delay_min=max(0.,now-before),
                causal_target_ids=causal,target_clock_after_min=after,signed_improvement_min=changes,undefined_targets=undefined,
                maximum_positive_target_gain_min=max(gains,default=0.),total_positive_target_gain_min=sum(gains),
                preview_present_task_ids=preview['present_task_ids'],preview_final_counts=preview['final_counts'],
                preview_remaining_quota_slots=preview['remaining_quota_slots'],preview_complete=False,
                preview_scope=SCOPE,raw_co_loaded_partners=[dict(task_id=q,distance_km=d) for q,d in sorted(partners.get(t,{}).items())],
                base_co_loaded_partners=[dict(task_id=q,distance_km=d) for q,d in sorted(old_partners.get(t,{}).items())],selection_reasons=[])
        def rank(t):
            r=rows[t]
            return (-r['maximum_positive_target_gain_min'],-r['total_positive_target_gain_min'],-int(r['newly_late']),
                -r['positive_delta_lateness_min'],-r['positive_delivery_delay_min'],t)
        effective=min(closure_limit,len(eligible));selected=set();events=[];donors=[]
        by_owner={a:sorted((t for t in eligible if index.delivery_route_by_task[t]==a),key=rank) for a in range(problem.drone_count)}
        def choose(t,reason):
            if t in selected or len(selected)>=effective:return False
            selected.add(t);rows[t]['selection_reasons'].append(reason)
            events.append(dict(task_id=t,raw_aircraft=index.delivery_route_by_task[t],reason=reason,selection_index=len(events)))
            return True
        # Open every possible aircraft before optional expansion consumes cap.
        for a in range(problem.drone_count):
            _check(deadline,stats);order=damage._rotate_head(by_owner[a],variant)
            if order and len(selected)<effective:
                t=order[0];choose(t,'real_aircraft_donor_reservation')
                donors.append(dict(aircraft=a,task_id=t,rank_index=by_owner[a].index(t),ranked_task_ids=by_owner[a]))
        direction=[]
        for target in targets:
            key=target['target_id'];candidates=[t for t in target['causal_prefix_task_ids'] if rows[t]['signed_improvement_min'][key] is not None]
            ordered=sorted(candidates,key=lambda t:(-rows[t]['signed_improvement_min'][key],rank(t)))
            ordered=damage._rotate_head(ordered,variant)
            representative=ordered[0] if ordered else None
            if representative is not None:choose(representative,'causal_prefix_target:'+key)
            direction.append(dict(target_id=key,representative_task_id=representative,ranked_task_ids=ordered,
                positive_single_removal_exists=any(rows[t]['signed_improvement_min'][key]>0. for t in candidates)))
        core={t for t in eligible if rows[t]['newly_late'] or rows[t]['positive_delta_lateness_min']>0.}
        linked={q for t in core|selected for q in set(partners.get(t,{}))|set(old_partners.get(t,{})) if q in eligible}
        categories=[('newly_late',sorted((t for t in eligible if rows[t]['newly_late']),key=rank)),
            ('positive_lateness_or_long_suffix',sorted((t for t in eligible if rows[t]['positive_delta_lateness_min']>0.),key=rank)),
            ('co_load_partner',sorted(linked,key=rank)),
            ('positive_delivery_delay',sorted((t for t in eligible if rows[t]['positive_delivery_delay_min']>0.),key=rank))]
        while len(selected)<effective:
            _check(deadline,stats);changed=False
            for name,queue in categories:
                while queue and queue[0] in selected:queue.pop(0)
                if queue:changed=choose(queue.pop(0),name) or changed
                if len(selected)>=effective:break
            if not changed:break
        # One common insertion order, independent of any beam state's owner
        # choice. It does not preassign the repaired tasks to aircraft.
        removed=sorted(selected,key=rank);partial=_whole_remove(problem,raw,removed)
        preview=partial_preview(problem,partial,pinned_task_ids=pins,expected_signature=expected_signature,
            remaining_task_ids=removed,deadline=deadline,global_evaluator=evaluator,statistics=stats)
        q=preview['remaining_quota_slots'];opened=[a for a,n in enumerate(q) if n]
        unopened={str(a):('no_eligible_whole_DIRECT_task' if not by_owner[a] else 'closure_limit_exhausted_before_real_donor')
            for a in range(problem.drone_count) if a not in opened}
        for entry in direction:entry['representative_retained']=entry['representative_task_id'] in selected
        pin_counts=[sum(index.delivery_route_by_task[t]==a for t in pins) for a in range(problem.drone_count)]
        for t,row in rows.items():row.update(removed=t in selected,repair_order_index=removed.index(t) if t in selected else None)
        _check(deadline,stats)
        result=dict(removed_task_ids=removed,partial_routes=[list(r) for r in partial],quota_slots_by_aircraft=q,remaining_quota_slots=q,
            raw_final_counts=raw_counts,pin_final_counts=pin_counts,remaining_native_final_counts=[n-k for n,k in zip(preview['final_counts'],pin_counts)],
            actual_opened_aircraft=opened,unopened_aircraft_reasons=unopened,all_aircraft_opened=len(opened)==problem.drone_count,
            all_eligible_aircraft_opened=set(opened)=={a for a,t in by_owner.items() if t},eligible_native_count=len(eligible),
            eligible_task_ids=sorted(eligible),omitted_task_ids=sorted(eligible-selected),eligible_omitted_count=len(eligible-selected),
            closure_limit=closure_limit,effective_closure_limit=effective,truncated=len(selected)<len(eligible),variant=variant,partner_depth=1,
            pinned_task_ids=sorted(pins),focus_task_ids=list(focus),expected_signature=expected_signature,targets=targets,
            critical_native_limit=critical_native_limit,critical_native_policy='nearest_absolute_actual_delivery_deadline_margin',
            per_task_evidence=[rows[t] for t in sorted(rows)],donor_reservations=donors,selection_events=events,
            prefix_direction_representatives=direction,prefix_candidate_task_ids=sorted(set().union(*(set(t['causal_prefix_task_ids']) for t in targets)) if targets else set()),
            partial_preview=preview_evidence(preview),complete=False,complete_solution=False,global_objective_bound=False,
            scoring_scope='causal_single_whole_removal_ranking_and_real_quota_slots_only')
        _check(deadline,stats);stats.update(completed=True,eligible_single_whole_previews=len(rows),runtime_seconds=perf_counter()-started)
        result['statistics']=dict(stats);_check(deadline,stats)
        return result
    except Deadline:
        stats.update(completed=False,deadline_reached=True,runtime_seconds=perf_counter()-started)
        raise
