"""Truthful bounded native-damage sets with real per-aircraft quota slots.

All metrics come from two complete schedules on the same Problem. Selection
priorities, home-distance migration proxies and co-load links are heuristics;
they never score a repaired fleet or certify a partial solution as complete.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter

from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import deadline_window_costs as costs
except ImportError:
    from . import deadline_window_costs as costs

Deadline=costs.Deadline
LATENESS_EPSILON_MIN=1e-9
COMPENSATION_NEIGHBOR_EVENT_RADIUS=2


def _check(deadline):
    if deadline is not None:
        if type(deadline) is bool or not isfinite(deadline):raise ValueError('finite absolute deadline or None required')
        if perf_counter()>=deadline:raise Deadline


def _task(problem,visit):
    leg=(problem.leg_registry or {}).get(abs(visit))
    return leg.task_id if leg is not None else abs(visit)


def _counts(index,count):
    return [sum(a==owner for a in index.delivery_route_by_task.values()) for owner in range(count)]


def _complete(problem,routes,evaluator,deadline):
    _check(deadline)
    routes=tuple(tuple(r) for r in routes)
    if len(routes)!=problem.drone_count:raise ValueError('one complete route per aircraft required')
    checked=evaluate_solution(problem,routes,_force_full=True)
    _check(deadline)
    if not checked.valid:raise ValueError('invalid complete closure input: '+str(checked.violations))
    schedule=evaluator.evaluate(routes)
    _check(deadline)
    index=build_plan_index(problem,routes)
    if not schedule.feasible or set(index.task_plans)!=set(problem.task_ids):raise ValueError('complete task coverage required')
    return routes,index,schedule


def _links(problem,routes,pins,compensation,deadline):
    """Actual positive-flight co-load graph and event-neighbor evidence."""
    graph={};carried={};neighbors={};pin_positions={};delivery_positions={}
    for a,route in enumerate(routes):
        held=set();previous=problem.home_points()[a]
        for position,visit in enumerate(route):
            _check(deadline)
            task=_task(problem,visit);point=problem.point_for_visit(visit)
            distance=previous.distance_to(point)
            if len(held)==2 and distance>0.:
                pair=tuple(sorted({_task(problem,v) for v in held}))
                if len(pair)==2:graph[pair]=graph.get(pair,0.)+distance
            if task in pins:
                pin_positions.setdefault(a,[]).append(position)
                for identifier in held:
                    other=_task(problem,identifier)
                    if other not in pins:carried.setdefault(other,[]).append(dict(aircraft=a,position=position,
                        pinned_task_id=task,pinned_visit=visit,held_before_event=True))
            if task in compensation:
                for p in range(max(0,position-COMPENSATION_NEIGHBOR_EVENT_RADIUS),
                               min(len(route),position+COMPENSATION_NEIGHBOR_EVENT_RADIUS+1)):
                    other=_task(problem,route[p])
                    if other!=task and other not in pins:neighbors.setdefault(other,set()).add(task)
            if visit>0:held.add(visit)
            else:
                held.remove(-visit)
                leg=(problem.leg_registry or {}).get(-visit)
                if leg is None or leg.kind=='RELAY_OUT':delivery_positions[task]=(a,position)
            previous=point
    return graph,carried,neighbors,pin_positions,delivery_positions


def _partners(graph):
    result={}
    for (a,b),distance in graph.items():
        result.setdefault(a,{})[b]=distance;result.setdefault(b,{})[a]=distance
    return result


def _rotate_head(values,variant):
    values=list(values);head=min(3,len(values))
    if head:
        n=variant%head;values=values[n:head]+values[:n]+values[head:]
    return values


def _damage_key(row):
    return (-int(row['newly_late']),-row['positive_delta_lateness_min'],
        -int(row['is_supplied_compensation']),-row['positive_delivery_delay_min'],row['task_id'])


def remove_closure(problem,routes,removed_ids,*,pinned_task_ids=(),deadline=None):
    """Remove complete whole DIRECT pairs only; slots are real removed owners."""
    _check(deadline)
    routes=tuple(tuple(r) for r in routes);removed=tuple(removed_ids);pins=frozenset(pinned_task_ids)
    if len(routes)!=problem.drone_count:raise ValueError('one route per aircraft required')
    if len(set(removed))!=len(removed) or any(type(t) is not int or t not in problem._task_by_id for t in removed):
        raise ValueError('distinct known whole DIRECT removal IDs required')
    if set(removed)&pins:raise ValueError('pinned task cannot be removed')
    index=build_plan_index(problem,routes)
    if not set(removed)<=set(index.direct_task_ids):raise ValueError('only whole DIRECT tasks may be removed')
    for t in removed:
        _check(deadline)
        owner=index.delivery_route_by_task[t]
        if sum(v==t for v in routes[owner])!=1 or sum(v==-t for v in routes[owner])!=1:
            raise ValueError('whole DIRECT removal requires unique paired events')
    chosen=set(removed);slots=[0]*problem.drone_count
    for t in removed:slots[index.delivery_route_by_task[t]]+=1
    partial=tuple(tuple(v for v in route if _task(problem,v) not in chosen) for route in routes)
    _check(deadline)
    return dict(routes=partial,partial_routes=[list(r) for r in partial],removed_task_ids=list(removed),
        quota_slots_by_aircraft=slots,quota_slots=list(slots),complete_solution=False,
        scoring_scope='partial_transport_routes_after_whole_DIRECT_removal')


def build_closure(problem,base_routes,raw_routes,pinned_task_ids,*,closure_limit=16,
                  partner_depth=1,variant=0,deadline=None,compensation_task_ids=()):
    """Open donor slots first, then interleave damage/comp/partner priorities.

    ``removed_task_ids`` is one deterministic repair order shared by all beam
    states. With a small cap, unavailable aircraft stay explicitly unopened.
    Supplied compensation IDs are provenance supplied by the caller; owner
    changes alone cannot identify unchanged-role compensation in DIRECT maps.
    """
    started=perf_counter();_check(deadline)
    if (type(closure_limit) is not int or closure_limit<0
            or type(partner_depth) is not int or not 0<=partner_depth<=4
            or type(variant) is not int or variant<0):
        raise ValueError('nonnegative closure limit/variant and partner depth 0..4 required')
    pins=frozenset(pinned_task_ids);compensation=tuple(compensation_task_ids)
    if len(pins)!=2 or any(type(t) is not int or t not in problem._task_by_id for t in pins):
        raise ValueError('exactly two distinct known pinned tasks required')
    if (len(set(compensation))!=len(compensation) or set(compensation)&pins
            or any(type(t) is not int or t not in problem._task_by_id for t in compensation)):
        raise ValueError('distinct known unpinned compensation IDs required')
    evaluator=GlobalRelayEvaluator(problem)
    base,base_index,base_schedule=_complete(problem,base_routes,evaluator,deadline)
    raw,index,schedule=_complete(problem,raw_routes,evaluator,deadline)
    if base_index.relay_task_ids:raise ValueError('real base must be pure DIRECT')
    if set(index.relay_task_ids) not in (set(),set(pins)):
        raise ValueError('all raw relays must be exactly the two pinned tasks or none')
    if any(index.inbound_route_by_task[t]==index.outbound_route_by_task[t] for t in index.relay_task_ids):
        raise ValueError('pinned relay plans must be true cross-aircraft')
    eligible=set(index.direct_task_ids)-pins
    if eligible!=set(problem.task_ids)-pins:raise ValueError('every unpinned task must be whole DIRECT')
    count=problem.drone_count;raw_counts=_counts(index,count)
    if raw_counts!=_counts(base_index,count):raise ValueError('raw per-aircraft final quotas differ from real base')
    if not set(compensation)<=eligible:raise ValueError('compensation must be eligible whole DIRECT')
    supplied_comp=set(compensation)
    inferred=sorted(t for t in eligible if base_index.delivery_route_by_task[t]!=index.delivery_route_by_task[t])
    # Never present route-inferred owner changes as proven k1/k2 identities.
    neighborhood_comp=supplied_comp if supplied_comp else set(inferred)
    base_graph,_,_,_,_=_links(problem,base,pins,(),deadline)
    graph,carried,neighbors,pin_positions,delivery_positions=_links(problem,raw,pins,neighborhood_comp,deadline)
    old_partners,new_partners=_partners(base_graph),_partners(graph)
    homes=problem.home_points();rows={}
    for t in sorted(eligible):
        _check(deadline)
        task=problem.task(t);owner=index.delivery_route_by_task[t];old_owner=base_index.delivery_route_by_task[t]
        old=base_schedule.delivery_times_min[t];now=schedule.delivery_times_min[t]
        old_late=old-task.deadline_min;new_late=now-task.deadline_min
        old_late=old_late if old_late>LATENESS_EPSILON_MIN else 0.
        new_late=new_late if new_late>LATENESS_EPSILON_MIN else 0.
        other=[(homes[a].distance_to(task.pickup),a) for a in range(count) if a!=owner]
        best_other=min(other) if other else (homes[owner].distance_to(task.pickup),None)
        old_co=old_partners.get(t,{});new_co=new_partners.get(t,{})
        position=delivery_positions[t][1]
        rows[t]=dict(task_id=t,base_aircraft=old_owner,raw_aircraft=owner,owner_changed=old_owner!=owner,
            deadline_min=task.deadline_min,base_delivery_min=old,raw_delivery_min=now,
            base_lateness_min=old_late,raw_lateness_min=new_late,delta_lateness_min=new_late-old_late,
            positive_delta_lateness_min=max(0.,new_late-old_late),newly_late=old_late==0. and new_late>0.,
            positive_delivery_delay_min=max(0.,now-old),is_supplied_compensation=t in supplied_comp,
            is_inferred_compensation_candidate=t in inferred,compensation_neighbor_task_ids=sorted(neighbors.get(t,set())),
            carried_at_pinned_events=carried.get(t,[]),raw_delivery_position=position,
            after_first_pinned_event=(owner in pin_positions and position>min(pin_positions[owner])),
            base_co_loaded_partners=[dict(task_id=q,distance_km=d) for q,d in sorted(old_co.items())],
            raw_co_loaded_partners=[dict(task_id=q,distance_km=d) for q,d in sorted(new_co.items())],
            base_co_loaded_distance_km=sum(old_co.values()),raw_co_loaded_distance_km=sum(new_co.values()),
            home_pickup_relocation_proxy_km=homes[owner].distance_to(task.pickup)-best_other[0],
            nearest_other_home_aircraft=best_other[1],selection_reasons=[])
    effective=min(closure_limit,len(eligible));selected=set();events=[];donors=[]
    by_owner={a:sorted(t for t in eligible if index.delivery_route_by_task[t]==a) for a in range(count)}
    # Real slots are reserved before optional closure expansion consumes cap.
    owner_order=sorted(range(count),key=lambda a:(
        -sum(rows[t]['positive_delta_lateness_min'] for t in by_owner[a]),
        -sum(rows[t]['newly_late'] for t in by_owner[a]),(a-variant)%max(1,count)))
    unopened={}
    def select(t,reason):
        if t not in selected and len(selected)<effective:
            selected.add(t);rows[t]['selection_reasons'].append(reason)
            events.append(dict(task_id=t,raw_aircraft=index.delivery_route_by_task[t],reason=reason,selection_index=len(events)))
            return True
        return False
    for a in owner_order:
        _check(deadline)
        ranked=sorted(by_owner[a],key=lambda t:(-rows[t]['positive_delta_lateness_min'],
            -int(rows[t]['newly_late']),-rows[t]['positive_delivery_delay_min'],
            -rows[t]['home_pickup_relocation_proxy_km'],t))
        if not ranked:
            unopened[str(a)]='no_unpinned_whole_DIRECT_task';continue
        candidate=_rotate_head(ranked,variant)[0]
        if len(selected)>=effective:
            unopened[str(a)]='closure_limit_exhausted_before_donor_reservation';continue
        select(candidate,'aircraft_donor_reservation')
        donors.append(dict(aircraft=a,task_id=candidate,rank_index=ranked.index(candidate),
            ranked_task_ids=ranked,rotation_head_size=min(3,len(ranked))))
    core={t for t in eligible if rows[t]['newly_late'] or rows[t]['positive_delta_lateness_min']>0.
          or t in neighborhood_comp or rows[t]['carried_at_pinned_events']}
    adjacency={t:set(old_partners.get(t,{}))|set(new_partners.get(t,{})) for t in eligible}
    distance={t:0 for t in core};front=set(core)
    for depth in range(1,partner_depth+1):
        _check(deadline);following=set()
        for t in sorted(front):
            _check(deadline)
            for q in sorted(adjacency.get(t,set())&eligible):
                if q not in distance:distance[q]=depth;following.add(q)
        front=following
    categories={
        'newly_late':sorted((t for t in eligible if rows[t]['newly_late']),key=lambda t:_damage_key(rows[t])),
        'positive_delta_lateness':sorted((t for t in eligible if rows[t]['positive_delta_lateness_min']>0.),
            key=lambda t:(-rows[t]['positive_delta_lateness_min'],-int(rows[t]['newly_late']),t)),
        'compensation':sorted(neighborhood_comp,key=lambda t:_damage_key(rows[t])),
        'co_load_partner':sorted((t for t,d in distance.items() if d>0),
            key=lambda t:(distance[t],-sum(new_partners.get(t,{}).values()),_damage_key(rows[t]))),
        'carried_at_pinned_event':sorted(carried.keys()&eligible,key=lambda t:_damage_key(rows[t])),
        'compensation_neighbor':sorted(neighbors.keys()&eligible,key=lambda t:_damage_key(rows[t])),
        'positive_delivery_delay':sorted((t for t in eligible if rows[t]['positive_delivery_delay_min']>0.),key=lambda t:_damage_key(rows[t]))}
    queues={name:_rotate_head(values,variant) for name,values in categories.items()};offsets={name:0 for name in queues}
    while len(selected)<effective:
        _check(deadline);added=False
        for name,queue in queues.items():
            _check(deadline)
            while offsets[name]<len(queue) and queue[offsets[name]] in selected:offsets[name]+=1
            if offsets[name]<len(queue):
                t=queue[offsets[name]];offsets[name]+=1
                added=select(t,name) or added
            if len(selected)>=effective:break
        if not added:break
    ranked_all=sorted(eligible,key=lambda t:_damage_key(rows[t]))
    # Unused cap need not be padded with unrelated tasks after all true damage
    # and declared partners are exhausted: only donor slots are compulsory.
    repair_order=sorted(selected,key=lambda t:_damage_key(rows[t]))
    removed=remove_closure(problem,raw,repair_order,pinned_task_ids=pins,deadline=deadline)
    slots=removed['quota_slots_by_aircraft'];opened=[a for a,q in enumerate(slots) if q]
    for a in range(count):
        if a not in opened and str(a) not in unopened:unopened[str(a)]='no_actual_native_slot_released'
    pin_counts=[sum(index.delivery_route_by_task[t]==a for t in pins) for a in range(count)]
    remaining_native=[sum(index.delivery_route_by_task[t]==a for t in eligible-selected) for a in range(count)]
    if any(raw_counts[a]!=pin_counts[a]+remaining_native[a]+slots[a] for a in range(count)):
        raise AssertionError('actual donor quota conservation failed')
    candidate_pool=set().union(*(set(v) for v in categories.values()))|{d['task_id'] for d in donors}
    omitted=sorted(eligible-selected);expansion_omitted=sorted(candidate_pool-selected)
    for t,row in rows.items():
        _check(deadline)
        row.update(removed=t in selected,repair_order_index=repair_order.index(t) if t in selected else None,
            partner_expansion_depth=distance.get(t),category_membership=[name for name,v in categories.items() if t in v])
    finished=perf_counter();_check(deadline)
    result=dict(removed_task_ids=repair_order,removed_task_ids_sorted=sorted(selected),
        quota_slots_by_aircraft=list(slots),quota_slots=list(slots),partial_routes=removed['partial_routes'],
        actual_opened_aircraft=opened,all_aircraft_opened=len(opened)==count,
        unopened_aircraft_reasons={str(a):unopened[str(a)] for a in range(count) if a not in opened},
        eligible_native_count=len(eligible),closure_limit=closure_limit,effective_closure_limit=effective,
        partner_depth=partner_depth,variant=variant,pinned_task_ids=sorted(pins),
        raw_final_counts=raw_counts,pin_final_counts=pin_counts,remaining_native_final_counts=remaining_native,
        omitted_task_ids=omitted,eligible_not_removed_task_ids=list(omitted),expansion_omitted_task_ids=expansion_omitted,
        truncation=dict(limit_reached=len(selected)>=effective,eligible_omitted_count=len(omitted),
            expansion_truncated=bool(expansion_omitted) and len(selected)>=effective,
            expansion_omitted_count=len(expansion_omitted)),
        evidence=dict(per_task=[rows[t] for t in sorted(rows)],donor_reservations=donors,
            donor_reservation_aircraft_order=owner_order,selection_events=events,ranking_order=ranked_all,
            category_rankings=categories,category_cycle_order=list(categories),
            compensation_task_ids=list(compensation),inferred_compensation_candidate_task_ids=inferred,
            compensation_identity_scope='caller_supplied' if compensation else 'owner_change_candidates_only_not_proven_compensation',
            compensation_neighbor_event_radius=COMPENSATION_NEIGHBOR_EVENT_RADIUS,
            base_score=dict(late_count=base_schedule.score.late_count,total_lateness_min=base_schedule.score.total_lateness_min,distance_km=base_schedule.score.distance_km),
            raw_score=dict(late_count=schedule.score.late_count,total_lateness_min=schedule.score.total_lateness_min,distance_km=schedule.score.distance_km),
            raw_total_waiting_min=schedule.total_waiting_min,
            pin_event_subsequences=[[v for v in r if _task(problem,v) in pins] for r in raw],
            ranking_is_gain_or_bound=False,all_damage_metrics_use_current_full_DAG=True,
            repair_order_policy='newlate_then_positive_deltaL_then_comp_then_delivery_delay_then_task_id',
            donor_policy='reserve_one_actual_native_per_aircraft_before_round_robin_damage_expansion'),
        started_at_perf_counter=started,finished_at_perf_counter=finished,runtime_seconds=finished-started,
        complete_solution=False,scoring_scope='closure_selection_and_partial_transport_routes_only')
    finished=perf_counter();_check(deadline)
    result.update(finished_at_perf_counter=finished,runtime_seconds=finished-started)
    return result
