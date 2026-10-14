"""Quota-balanced joint repair of real damage and donor tasks around two pins.

Independent insertion costs propose an assignment; they are not additive
schedule costs. Every retained beam state is scheduled by the real global
DAG. Paired task layers share coverage, and finite forward/reverse cycles
and paired owner swaps preserve the exact number of freed slots per UAV.
"""
from __future__ import annotations

from itertools import product
from math import isfinite,isclose
from time import perf_counter

from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.search import RouteEvaluator,route_insertion_options
from uav_dispatch.validation import evaluate_solution
try:
    import relay_damage_closure as closure_builder
    import deadline_window_costs as costs
except ImportError:
    from . import relay_damage_closure as closure_builder
    from . import deadline_window_costs as costs

Deadline=costs.Deadline
weighted_key=costs.weighted_key


def _check(deadline,stats):
    if perf_counter()>=deadline:
        stats['deadline_reached']=True
        raise Deadline


def _physical(score):
    return dict(late_count=score.late_count,total_lateness_min=score.total_lateness_min,distance_km=score.distance_km)


def _task(p,v):
    leg=p.leg_registry.get(abs(v))
    return leg.task_id if leg else abs(v)


def _signature(p,routes,pins):
    index=build_plan_index(p,routes)
    return dict(plans=tuple((t,index.task_plans[t].mode,index.task_plans[t].relay_id,
        tuple(index.task_plans[t].leg_ids),index.delivery_route_by_task[t],index.inbound_route_by_task.get(t),
        index.outbound_route_by_task.get(t)) for t in sorted(pins)),
        events=tuple(tuple(v for v in r if _task(p,v) in pins) for r in routes),
        counts=tuple(sum(a==owner for owner in index.delivery_route_by_task.values()) for a in range(p.drone_count)),
        relay_tasks=tuple(sorted(index.relay_task_ids)))


def _shared(p,routes,pins,index):
    if len({index.delivery_route_by_task[t] for t in pins})!=1:return 0.
    a=index.delivery_route_by_task[next(iter(pins))]
    identifiers={leg for t in pins for leg in index.task_plans[t].leg_ids
        if index.task_plans[t].mode=='DIRECT' or p.leg_registry[leg].kind=='RELAY_OUT'}
    held=set();previous=p.home_points()[a];distance=0.
    for v in routes[a]:
        point=p.point_for_visit(v)
        if identifiers<=held:distance+=previous.distance_to(point)
        if v>0:held.add(v)
        else:held.remove(-v)
        previous=point
    return float(distance)


def _receiver_shape(p,routes,cross):
    if not cross:return None
    index=build_plan_index(p,routes)
    if any(t not in index.relay_task_ids or index.inbound_route_by_task[t]==index.outbound_route_by_task[t] for t in cross):
        raise ValueError('required cross plan changed')
    if len({index.outbound_route_by_task[t] for t in cross})!=1 or len({index.inbound_route_by_task[t] for t in cross})!=2 or len({index.task_plans[t].relay_id for t in cross})!=1:
        raise ValueError('two sources and a same-site receiver required')
    a=index.outbound_route_by_task[next(iter(cross))]
    ids={next(v for v in index.task_plans[t].leg_ids if p.leg_registry[v].kind=='RELAY_OUT') for t in cross}
    starts=sorted(routes[a].index(v) for v in ids)
    if starts[1]!=starts[0]+1:raise ValueError('OUT take pair split')
    held=set()
    for k,v in enumerate(routes[a]):
        if k==starts[0] and held:raise ValueError('receiver does not enter the two takes empty')
        if v>0:held.add(v)
        else:held.remove(-v)
    if _shared(p,routes,cross,index)<=1e-10:raise ValueError('no genuine positive double-loaded flight')
    return a


def _validate(p,routes,pins,expected,cross):
    full=evaluate_solution(p,routes,_force_full=True)
    if not full.valid:raise ValueError('invalid complete repair')
    if _signature(p,routes,pins)!=expected:raise ValueError('pin roles/legs/order or final quotas changed')
    _receiver_shape(p,routes,cross)
    return full


def _classifications(p,original,current,pins,comp):
    old={t for t,v in original.items() if v-p.task(t).deadline_min>1e-9}
    now={t for t,v in current.items() if v-p.task(t).deadline_min>1e-9}
    native=set(p.task_ids)-pins-comp;result={}
    for name,ids in (('pooled',pins),('compensation',comp),('native',native)):
        result[name+'_rescued_task_ids']=sorted((old-now)&ids)
        result[name+'_newly_late_task_ids']=sorted((now-old)&ids)
    return result


def _snapshot(p,routes,full,schedule,w,s,*,base_schedule,move,phase):
    f=move['family'];pins=frozenset((f['i'],f['j']));comp=frozenset((f['k1'],f['k2']));index=build_plan_index(p,routes)
    available={};handoff=None;shared=_shared(p,routes,pins,index)
    if index.relay_task_ids:
        for t in sorted(pins):
            leg=next(v for v in index.task_plans[t].leg_ids if p.leg_registry[v].kind=='RELAY_IN')
            a=index.inbound_route_by_task[t];available[t]=schedule.event_times_min[(a,routes[a].index(-leg))]
        c=index.outbound_route_by_task[next(iter(pins))]
        outgoing={next(v for v in index.task_plans[t].leg_ids if p.leg_registry[v].kind=='RELAY_OUT'):t for t in pins}
        positions=sorted(routes[c].index(v) for v in outgoing);gap=positions[0]
        H=next(st.point for st in p.relay_stations if st.id==index.task_plans[next(iter(pins))].relay_id)
        previous=p.home_points()[c] if gap==0 else p.point_for_visit(routes[c][gap-1])
        arrival=(0. if gap==0 else schedule.event_times_min[(c,gap-1)])+previous.distance_to(H)/p.speed_km_per_min
        order=[outgoing[routes[c][k]] for k in positions];times=[schedule.event_times_min[(c,k)] for k in positions];T=times[-1]
        offsets={str(_task(p,v)):schedule.delivery_times_min[_task(p,v)]-T for k,v in enumerate(routes[c]) if v<0 and k>positions[-1]
            and (abs(v) not in p.leg_registry or p.leg_registry[abs(v)].kind=='RELAY_OUT')}
        handoff=dict(station_id=index.task_plans[next(iter(pins))].relay_id,task_ids=sorted(pins),
            source_aircraft=[index.inbound_route_by_task[t] for t in sorted(pins)],receiver_aircraft=c,
            source_availability_min={str(t):v for t,v in available.items()},receiver_arrival_min=arrival,
            take_order_task_ids=order,take_times_min=times,second_take_min=T,receiver_waiting_min=T-arrival,
            ground_cache_by_task_min={str(t):times[order.index(t)]-available[t] for t in sorted(pins)},
            suffix_delivery_offsets_min=offsets,shared_loaded_distance_km=shared)
    return dict(routes=[list(r) for r in routes],valid=True,complete=True,phase=phase,score=_physical(full.score),
        post_global_score=_physical(full.score),key=list(weighted_key(full.score,w,s)),
        cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        relay_task_count=len(index.relay_task_ids),pin_scope='window_damage_repair',pinned_task_ids=sorted(pins),
        released_compensation_task_ids=sorted(comp),original_macro_move=move,move=dict(semantic='post_window_damage_repair'),
        event_times_min=[[schedule.event_times_min[(a,k)] for k in range(len(r))] for a,r in enumerate(routes)],
        delivery_times_min={str(t):v for t,v in sorted(schedule.delivery_times_min.items())},
        actual_final_owners={str(t):a for t,a in sorted(index.delivery_route_by_task.items())},
        current_source_drop_availability_min={str(t):v for t,v in available.items()},
        current_shared_loaded_distance_km=shared,current_handoff=handoff,
        current_classifications=_classifications(p,base_schedule.delivery_times_min,schedule.delivery_times_min,pins,comp))


def _hungarian(cost):
    """Minimum independent-cost proposal on an expanded finite slot matrix."""
    n=len(cost);u=[0.]*(n+1);v=[0.]*(n+1);owner=[0]*(n+1);way=[0]*(n+1)
    for i in range(1,n+1):
        owner[0]=i;j0=0;minimum=[float('inf')]*(n+1);used=[False]*(n+1)
        while True:
            used[j0]=True;i0=owner[j0];delta=float('inf');j1=0
            for j in range(1,n+1):
                if used[j]:continue
                cur=cost[i0-1][j-1]-u[i0]-v[j]
                if cur<minimum[j]:minimum[j]=cur;way[j]=j0
                if minimum[j]<delta:delta=minimum[j];j1=j
            if not isfinite(delta):return None
            for j in range(n+1):
                if used[j]:u[owner[j]]+=delta;v[j]-=delta
                else:minimum[j]-=delta
            j0=j1
            if not owner[j0]:break
        while True:
            j1=way[j0];owner[j0]=owner[j1];j0=j1
            if not j0:break
    result=[None]*n
    for j in range(1,n+1):result[owner[j]-1]=j-1
    return result


def _assignments(tasks,slots,owners,matrix):
    expanded=[a for a,q in enumerate(slots) for _ in range(q)];plans=[]
    matching=_hungarian([[matrix.get((t,a),float('inf')) for a in expanded] for t in tasks])
    if matching is not None:plans.append(('independent_cost_assignment',{t:expanded[k] for t,k in zip(tasks,matching)}))
    opened=[a for a,q in enumerate(slots) if q];chosen={a:next(t for t in tasks if owners[t]==a) for a in opened}
    for name,direction in (('all_opened_forward_cycle',1),('all_opened_reverse_cycle',-1)):
        assignment={t:owners[t] for t in tasks}
        for k,a in enumerate(opened):assignment[chosen[a]]=opened[(k+direction)%len(opened)]
        plans.append((name,assignment))
    assignment={t:owners[t] for t in tasks}
    for a,b in zip(opened[::2],opened[1::2]):assignment[chosen[a]],assignment[chosen[b]]=b,a
    plans.append(('disjoint_owner_pairs',assignment))
    result=[];seen=set()
    for name,assignment in plans:
        identity=tuple(assignment[t] for t in tasks)
        if identity not in seen and all(sum(v==a for v in assignment.values())==q for a,q in enumerate(slots)):
            result.append((name,assignment));seen.add(identity)
    return result


def repair_damage_closure(problem,base_routes,macro_routes,*,original_macro_move,relay_enabled,deadline,weights,scales,
        seed=2026101515,closure_limit=16,partner_depth=1,beam_width=4,position_limit=16,option_count=2,
        global_option_probe_limit=4,max_rounds=2,snapshot_limit=8,variant=0):
    started=perf_counter();w,s=tuple(weights),tuple(scales)
    limits=((closure_limit,64),(partner_depth,3),(beam_width,4),(position_limit,64),(option_count,2),
        (global_option_probe_limit,16),(max_rounds,4),(snapshot_limit,16))
    if (not isfinite(deadline) or type(relay_enabled) is not bool or len(w)!=3 or len(s)!=3
        or any(not isfinite(x) or x<=0 for x in (*w,*s)) or not isclose(sum(w),1.)
        or any(type(x) is not int or not 1<=x<=upper for x,upper in limits) or position_limit<4
        or global_option_probe_limit<option_count or type(variant) is not int or variant<0):raise ValueError('finite weighted deadline and bounded joint repair required')
    base=tuple(tuple(r) for r in base_routes);raw=tuple(tuple(r) for r in macro_routes);f=original_macro_move['family']
    pins=frozenset((f['i'],f['j']));cross=pins if relay_enabled else frozenset()
    initial=evaluate_solution(problem,raw,_force_full=True)
    if not initial.valid or set(build_plan_index(problem,raw).relay_task_ids)!=cross:raise ValueError('valid complete macro with only its two pinned relays required')
    expected=_signature(problem,raw,pins);_receiver_shape(problem,raw,cross)
    base_checked=evaluate_solution(problem,base,_force_full=True)
    if not base_checked.valid or build_plan_index(problem,base).relay_task_ids:raise ValueError('valid true DIRECT source required')
    global_evaluator=GlobalRelayEvaluator(problem);local=RouteEvaluator(problem)
    baseline=global_evaluator.evaluate(base);raw_schedule=global_evaluator.evaluate(raw)
    best,best_checked,best_schedule=raw,initial,raw_schedule;best_key=weighted_key(initial.score,w,s)
    stats=dict(method='Damage-Closure-Quota-Joint-Repair',seed=seed,pin_scope='window_damage_repair',deadline_perf_counter=deadline,
        deadline_reached=False,pinned_task_ids=sorted(pins),released_compensation_task_ids=sorted((f['k1'],f['k2'])),
        closure_records=[],full_candidate_snapshots=[],first_complete_candidate=None,best_constructed_candidate=None,
        global_partial_evaluations=0,global_incremental_evaluations=0,position_options_probed=0,joint_pair_expansions=0,
        complete_leaf_checks=0,complete_valid_candidates=0,rejected_complete_leaves=0,accepted_improvements=0,returned_checkpoint_checked_within_deadline=False,
        input_snapshot=None,post_repair_snapshot=None,best_checked_at_perf_counter=None,initial_key=list(best_key),final_key=list(best_key),
        configuration=dict(closure_limit=closure_limit,partner_depth=partner_depth,beam_width=beam_width,position_limit=position_limit,
            option_count=option_count,global_option_probe_limit=global_option_probe_limit,max_rounds=max_rounds,snapshot_limit=snapshot_limit,
            assignments='independent-cost quota proposal+all-opened forward/reverse cycles+disjoint pairs',
            assignment_costs_are_actual_joint_scores_or_bounds=False,next_task_order='shared_closure_order_two_task_layers',
            partial_comparison='identical_present_task_set',polish_uses_ALNS=False))
    try:
        _check(deadline,stats)
        stats['input_snapshot']=_snapshot(problem,raw,initial,raw_schedule,w,s,base_schedule=baseline,move=original_macro_move,phase='damage_macro_input')
        input_verified=perf_counter()
        if input_verified>=deadline:raise Deadline
        stats['returned_checkpoint_checked_within_deadline']=True
        best_snapshot=stats['input_snapshot'];stats['best_checked_at_perf_counter']=input_verified
        stats['post_repair_snapshot']=best_snapshot
        for round_index in range(max_rounds):
            _check(deadline,stats)
            before=best;owners=build_plan_index(problem,before).delivery_route_by_task
            closure=closure_builder.build_closure(problem,base,before,pins,closure_limit=closure_limit,partner_depth=partner_depth,
                variant=variant+round_index,deadline=deadline,compensation_task_ids=(f['k1'],f['k2']))
            tasks=tuple(closure['removed_task_ids']);slots=tuple(closure['quota_slots_by_aircraft'])
            if len(set(tasks))!=len(tasks) or pins&set(tasks) or sum(slots)!=len(tasks):raise ValueError('invalid closure task/slot evidence')
            index=build_plan_index(problem,before)
            if any(t not in index.direct_task_ids for t in tasks):raise ValueError('closure may remove only whole unpinned DIRECT tasks')
            if slots!=tuple(sum(owners[t]==a for t in tasks) for a in range(problem.drone_count)):raise ValueError('closure quota differs from real removed owners')
            partial=tuple(tuple(v for v in r if _task(problem,v) not in tasks) for r in before)
            scheduled=global_evaluator.evaluate(partial);stats['global_partial_evaluations']+=1
            if not scheduled.feasible or set(scheduled.delivery_times_min)!=set(problem.task_ids)-set(tasks):raise ValueError('invalid same-coverage partial')
            _receiver_shape(problem,partial,cross)
            record=dict(before_routes=[list(r) for r in before],closure=closure,partial_routes=[list(r) for r in partial],
                quota_slots_by_aircraft=list(slots),owner_assignments=[],layers=[],complete_leaf_count=0,
                complete_checkpoints=[],rejected_complete_leaves=[],initial_assignment_families=[],completed_assignment_families=[],
                finished=False,started_at_perf_counter=perf_counter(),finished_at_perf_counter=None)
            stats['closure_records'].append(record)
            if not tasks:record.update(finished=True,finished_at_perf_counter=perf_counter());continue
            cache={}
            def options(routes,schedule,task,a):
                _check(deadline,stats);key=(routes,task,a)
                if key in cache:return cache[key]
                local_options=route_insertion_options(problem,local,routes[a],a,task,candidate_limit=position_limit,
                    option_count=global_option_probe_limit)
                ranked=[]
                for option in local_options:
                    _check(deadline,stats);changed=list(routes);changed[a]=option.route;candidate=tuple(changed)
                    try:_receiver_shape(problem,candidate,cross)
                    except ValueError:continue
                    current=global_evaluator.evaluate_delta(routes,schedule,candidate)
                    stats['global_incremental_evaluations']+=1;stats['position_options_probed']+=1
                    _check(deadline,stats)
                    if current.feasible:ranked.append((weighted_key(current.score,w,s),candidate,current))
                result=tuple(sorted(ranked,key=lambda x:(x[0],x[1]))[:option_count]);cache[key]=result;return result
            matrix={}
            for task in tasks:
                for a,q in enumerate(slots):
                    if not q:continue
                    possible=options(partial,scheduled,task,a)
                    if possible:matrix[(task,a)]=possible[0][0][0]-weighted_key(scheduled.score,w,s)[0]
            plans=_assignments(tasks,slots,owners,matrix)
            record['initial_assignment_families']=list(range(len(plans)))
            for name,assignment in plans:
                record['owner_assignments'].append(dict(name=name,owner_assignment={str(t):a for t,a in assignment.items()},
                    changed_owner_task_ids=[t for t in tasks if assignment[t]!=owners[t]],
                    actual_assigned_aircraft=sorted(set(assignment.values()))))
            states=[dict(routes=partial,schedule=scheduled,key=weighted_key(scheduled.score,w,s),plan=n,remaining=list(slots)) for n in range(len(plans))]
            present=set(problem.task_ids)-set(tasks)
            for start in range(0,len(tasks),2):
                group=tasks[start:start+2];expanded=[]
                for state in states:
                    frontier=[state]
                    for task in group:
                        following=[]
                        for current in frontier:
                            owner=plans[current['plan']][1][task]
                            if current['remaining'][owner]<=0:raise AssertionError('joint assignment exceeded freed quota')
                            for key,routes,schedule in options(current['routes'],current['schedule'],task,owner):
                                remaining=list(current['remaining']);remaining[owner]-=1
                                following.append(dict(routes=routes,schedule=schedule,key=key,plan=current['plan'],remaining=remaining))
                        frontier=following
                    expanded.extend(frontier);stats['joint_pair_expansions']+=len(frontier)
                present.update(group)
                for state in expanded:
                    if set(state['schedule'].delivery_times_min)!=present:raise AssertionError('joint beam mixed task coverage')
                ordered=sorted(expanded,key=lambda x:(x['key'],x['routes'],x['plan']));heads=[];used=set()
                for state in ordered:
                    if state['plan'] not in used:heads.append(state);used.add(state['plan'])
                states=heads[:beam_width]
                for state in ordered:
                    if len(states)>=beam_width:break
                    if all((state['routes'],state['plan'])!=(old['routes'],old['plan']) for old in states):states.append(state)
                record['layers'].append(dict(inserted_task_ids=list(group),present_task_ids=sorted(present),
                    candidates=len(expanded),beam_states=len(states),assignment_families=sorted({state['plan'] for state in states}),
                    beam_state_snapshots=[dict(routes=[list(r) for r in state['routes']],score=_physical(state['schedule'].score),
                        key=list(state['key']),remaining_quota_slots=list(state['remaining']),assignment_index=state['plan'],
                        event_times_min=[[state['schedule'].event_times_min[(a,k)] for k in range(len(r))] for a,r in enumerate(state['routes'])],
                        delivery_times_min={str(t):v for t,v in sorted(state['schedule'].delivery_times_min.items())},
                        scoring_scope='partial_same_coverage_only') for state in states]))
                _check(deadline,stats)
                if not states:break
            for state in sorted(states,key=lambda x:(x['key'],x['routes'])):
                if any(state['remaining']):continue
                _check(deadline,stats);stats['complete_leaf_checks']+=1
                try:full=_validate(problem,state['routes'],pins,expected,cross)
                except (ValueError,KeyError,IndexError) as error:
                    stats['rejected_complete_leaves']+=1
                    record['rejected_complete_leaves'].append(dict(assignment_index=state['plan'],reason=str(error)))
                    continue
                schedule=global_evaluator.evaluate(state['routes'])
                if not schedule.feasible:raise ValueError('full repaired DAG infeasible')
                snapshot=_snapshot(problem,state['routes'],full,schedule,w,s,base_schedule=baseline,move=original_macro_move,phase='damage_complete_candidate')
                verified=perf_counter()
                if verified>=deadline:raise Deadline
                stats['complete_valid_candidates']+=1;record['complete_leaf_count']+=1
                record['completed_assignment_families'].append(state['plan'])
                actual=build_plan_index(problem,state['routes']).delivery_route_by_task
                changed=[t for t in tasks if actual[t]!=owners[t]]
                involved={f['a'],f['b'],f['c']}
                record['complete_checkpoints'].append(dict(full_snapshot=snapshot,full_verified_at_perf_counter=verified,
                    assignment_index=state['plan'],changed_owner_task_ids=changed,
                    owner_change_outside_macro_aircraft_task_ids=[t for t in changed if owners[t] not in involved or actual[t] not in involved]))
                if len(stats['full_candidate_snapshots'])<snapshot_limit:stats['full_candidate_snapshots'].append(snapshot)
                if stats['first_complete_candidate'] is None:stats['first_complete_candidate']=snapshot
                if stats['best_constructed_candidate'] is None or tuple(snapshot['key'])<tuple(stats['best_constructed_candidate']['key']):stats['best_constructed_candidate']=snapshot
                key=weighted_key(full.score,w,s)
                if key<best_key:
                    _check(deadline,stats);best,best_checked,best_schedule,best_key=state['routes'],full,schedule,key
                    stats['accepted_improvements']+=1;stats['best_checked_at_perf_counter']=verified
                    best_snapshot=snapshot;stats['post_repair_snapshot']=best_snapshot
            record.update(finished=True,finished_at_perf_counter=perf_counter())
        _check(deadline,stats)
    except Deadline:
        stats['deadline_reached']=True
    for record in stats['closure_records']:
        record['uncompleted_assignment_families']=sorted(set(record['initial_assignment_families'])-set(record['completed_assignment_families']))
    stats.update(final_key=list(best_key),runtime_seconds=perf_counter()-started,returned_initial_macro=best==raw,
        returned_checkpoint_kind='initial_complete_macro' if best==raw else 'new_predeadline_complete_repair')
    return dict(routes=best,evaluation=best_checked,iterations=stats['complete_leaf_checks'],metadata=stats)
