"""Critical-prefix closure repair with state-local owner and route choices.

Geometric/clock position frontiers are bounded proposals, never objective
bounds. No first-task objective pruning occurs. Two-job coverage is restored
before a layer is compared by its actual global schedule.
"""
from __future__ import annotations

from itertools import permutations
from math import isfinite
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import critical_prefix_closure_v1 as closure
    import split_feeder_refinement_v4 as raw_tools
except ImportError:
    from . import critical_prefix_closure_v1 as closure
    from . import split_feeder_refinement_v4 as raw_tools

Deadline = closure.Deadline
weighted_key = raw_tools.weighted_key
SCOPE = 'critical_prefix_joint_repair'


def _check(deadline, stats):
    if perf_counter() >= deadline:
        stats['deadline_reached'] = True
        raise Deadline


def _owners(index):
    return {str(t): a for t, a in sorted(index.delivery_route_by_task.items())}


def _classes(p, original, current, focus, donors):
    before = {t for t, x in original.items() if x-p.task(t).deadline_min > 1e-9}
    after = {t for t, x in current.items() if x-p.task(t).deadline_min > 1e-9}
    result = {}
    for name, ids in (('pooled', set(focus)), ('donor', set(donors)-set(focus)),
            ('native', set(p.task_ids)-set(focus)-set(donors))):
        result[name+'_rescued_task_ids'] = sorted((before-after)&ids)
        result[name+'_newly_late_task_ids'] = sorted((after-before)&ids)
    return result


def _snapshot(p, state, checked, w, s, *, phase, base_schedule, initial_routes, seed_move, pins, focus, deadline, stats):
    routes, schedule = state['routes'], state['schedule']; index = build_plan_index(p, routes)
    family = seed_move['family'] if seed_move else None
    pair = tuple(sorted(pins)) if pins else tuple(focus[:2])
    shared = raw_tools._shared(p, routes, pair, index.delivery_route_by_task) if len(pair)==2 else 0.
    handoff = raw_tools._handoff(p, routes, schedule, family, index, shared) if pins else None
    donors = (family['k1'], family['k2']) if family else ()
    raw_index = build_plan_index(p,initial_routes)
    snapshot = dict(routes=[list(r) for r in routes], valid=True, complete=True, phase=phase,
        score=raw_tools._physical(checked.score), key=list(weighted_key(checked.score, w, s)),
        pin_scope=SCOPE, pinned_task_ids=sorted(pins), focus_task_ids=list(focus), original_seed_move=seed_move,
        event_times_min=[[schedule.event_times_min[a,k] for k in range(len(r))] for a,r in enumerate(routes)],
        delivery_times_min={str(t):x for t,x in sorted(schedule.delivery_times_min.items())},
        actual_final_owners=_owners(index), current_source_drop_availability_min=(handoff['source_availability_min'] if handoff else {}),
        current_shared_loaded_distance_km=shared, current_handoff=handoff,
        current_classifications=_classes(p,base_schedule.delivery_times_min,schedule.delivery_times_min,focus,donors),
        relay_task_count=len(index.relay_task_ids), cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        pinned_shape=closure.shape(p,routes,pinned_task_ids=pins,deadline=deadline), deadline_perf_counter=deadline)
    snapshot.update(post_global_score=snapshot['score'],
        changed_owner_task_ids=sorted(t for t,a in index.delivery_route_by_task.items() if a!=raw_index.delivery_route_by_task[t]),
        changed_event_sequence_aircraft=[a for a,(before,after) in enumerate(zip(initial_routes,routes)) if before!=after],
        state_id=state['id'],parent_state_id=state.get('parent_id'),inserted_task_order=list(state.get('order',())),
        owner_choices=list(state.get('owner_choices',())),insertions=list(state.get('insertions',())))
    snapshot['routes_sha256']=raw_tools._digest(snapshot['routes'])
    _check(deadline, stats)
    snapshot['full_verified_at_perf_counter'] = perf_counter()
    _check(deadline, stats)
    return snapshot


def _state_evidence(state, remaining_tasks):
    schedule = state['schedule']
    return dict(state_id=state['id'], parent_state_id=state.get('parent_id'), routes=[list(r) for r in state['routes']],
        score=raw_tools._physical(schedule.score), key=list(state['key']),
        event_times_min=[[schedule.event_times_min[a,k] for k in range(len(r))] for a,r in enumerate(state['routes'])],
        delivery_times_min={str(t):x for t,x in sorted(schedule.delivery_times_min.items())},
        present_task_ids=sorted(schedule.delivery_times_min), remaining_task_ids=list(remaining_tasks),
        remaining_quota_slots=list(state['q']), inserted_task_order=list(state.get('order',())),
        owner_choices=list(state.get('owner_choices',())), insertions=list(state.get('insertions',())),
        complete=False, scoring_scope='partial_same_coverage_only')


def _frontier(p, state, task, aircraft, position_limit, option_count, deadline, stats, layer):
    route=state['routes'][aircraft];n=len(route);load=0;loads=[0]
    for v in route:load+=1 if v>0 else -1;loads.append(load)
    P,D=p.task(task).pickup,p.task(task).delivery;V=p.speed_km_per_min
    points=[p.point_for_visit(v) for v in route];items=[]
    for g in range(n+1):
        _check(deadline,stats)
        if loads[g]>=2:continue
        u=p.home_points()[aircraft] if g==0 else points[g-1]
        prefix=0. if g==0 else state['schedule'].event_times_min[aircraft,g-1]
        start=prefix+u.distance_to(P)/V
        for h in range(g,n+1):
            _check(deadline,stats)
            if loads[h]>=2:break
            if g==h:
                delta=u.distance_to(P)+P.distance_to(D)
                if h<n:delta+=D.distance_to(points[h])-u.distance_to(points[h])
                end=start+P.distance_to(D)/V
            else:
                first=u.distance_to(P)+P.distance_to(points[g])-u.distance_to(points[g])
                end=state['schedule'].event_times_min[aircraft,h-1]+first/V+points[h-1].distance_to(D)/V
                delta=first+points[h-1].distance_to(D)
                if h<n:delta+=D.distance_to(points[h])-points[h-1].distance_to(points[h])
            items.append((delta,start,end,g,h))
    streams=(sorted(items),sorted(items,key=lambda x:(x[1],x[0],x[2],x[3:])),
        sorted(items,key=lambda x:(x[2],x[0],x[1],x[3:])))
    shortlist=[];seen=set()
    for row in (*[s[0] for s in streams if s],*streams[0]):
        _check(deadline,stats)
        if row[3:] not in seen and len(shortlist)<position_limit:seen.add(row[3:]);shortlist.append(row)
    selected=[];seen=set()
    orders=(sorted(shortlist),sorted(shortlist,key=lambda x:(x[1],x[0],x[2],x[3:])),
        sorted(shortlist,key=lambda x:(x[2],x[0],x[1],x[3:])))
    for row in (*[s[0] for s in orders if s],*orders[0]):
        _check(deadline,stats)
        if row[3:] not in seen and len(selected)<option_count:seen.add(row[3:]);selected.append(row)
    for counter,value in (('legal_PD_positions',len(items)),('position_shortlist_retained',len(shortlist)),
            ('position_frontier_retained',len(selected)),('PD_positions_omitted',len(items)-len(selected))):
        stats[counter]+=value;layer[counter]=layer.get(counter,0)+value
    return [(route[:g]+(task,)+route[g:h]+(-task,)+route[h:],dict(task_id=task,owner=aircraft,
        pickup_gap=g,delivery_gap=h)) for _,_,_,g,h in selected]


def refine(problem, base_routes, raw_routes, *, seed_move=None, focus_task_ids=(), deadline, weights, scales,
        seed=2026101520, relay_enabled=True, closure_limit=12, partner_depth=1, beam_width=6,
        position_limit=12, option_count=3, max_rounds=2, snapshot_limit=6):
    started=perf_counter();w,s=tuple(weights),tuple(scales)
    bounds=((closure_limit,64),(partner_depth,4),(beam_width,12),(position_limit,256),(option_count,3),(max_rounds,8),(snapshot_limit,64))
    if (type(deadline) is bool or not isfinite(deadline) or type(relay_enabled) is not bool or type(seed) is not int
        or any(type(x) is not int or x<0 or x>m for x,m in bounds) or min(beam_width,position_limit,option_count,max_rounds,snapshot_limit)<1
        or option_count>position_limit or partner_depth!=1):raise ValueError('finite bounded dynamic quota search with one-hop partners required')
    weighted_key(Score(0,0.,0.),w,s)
    base=tuple(tuple(r) for r in base_routes);initial=tuple(tuple(r) for r in raw_routes)
    pins=frozenset((seed_move['family']['i'],seed_move['family']['j'])) if relay_enabled and seed_move else frozenset()
    if relay_enabled and len(pins)!=2:raise ValueError('two actual split-relay pins required')
    focus=tuple(sorted(set(focus_task_ids or pins)))
    if any(type(t) is not int or t not in problem._task_by_id for t in focus):raise ValueError('known focus task IDs required')
    stats=dict(method='Critical-Prefix-State-Local-Joint-Quota-Repair-V1',seed=seed,relay_enabled=relay_enabled,
        pin_scope=SCOPE,pinned_task_ids=sorted(pins),focus_task_ids=list(focus),original_seed_move=seed_move,
        deadline_perf_counter=deadline,deadline_reached=False,closure_records=[],input_snapshot=None,
        post_repair_snapshot=None,first_complete_candidate=None,best_constructed_candidate=None,full_candidate_snapshots=[],
        global_partial_evaluations=0,global_delta_evaluations=0,complete_proposals_generated=0,complete_leafs_full_verified=0,
        legal_PD_positions=0,position_shortlist_retained=0,position_frontier_retained=0,PD_positions_omitted=0,
        invalid_candidates=0,completed_rounds=0,accepted_improvements=0,returned_input_guard=True,
        returned_checkpoint_checked_within_deadline=False,configuration=dict(closure_limit=closure_limit,partner_depth=partner_depth,
            beam_width=beam_width,position_limit=position_limit,option_count=option_count,max_rounds=max_rounds,snapshot_limit=snapshot_limit,
            owner_policy='state_local_positive_remaining_quota_all_owners',task_layer_size=2,
            first_partial_J_pruning=False,position_policy='all_legal_PD_geometry_early_pickup_early_delivery_shortlist_then_frontier',
            position_scores_are_bounds=False,all_PD_positions_globally_scored=False,layer_comparison='identical_present_task_ids',
            ordinary_DIRECT_owner_and_order_free=True,new_relay_generation=False))
    stats['initialization_completed']=False
    try:
        _check(deadline,stats)
        base_checked=evaluate_solution(problem,base,_force_full=True);_check(deadline,stats)
        initial_checked=evaluate_solution(problem,initial,_force_full=True);_check(deadline,stats)
        if not base_checked.valid or not initial_checked.valid or build_plan_index(problem,base).relay_task_ids:
            raise ValueError('real pure DIRECT base and complete seed required')
        _check(deadline,stats)
        if seed_move is not None:raw_tools._verify(problem,base,initial,seed_move,relay_enabled)
        elif initial!=base:raise ValueError('a seed without raw provenance must be the original DIRECT guard')
        _check(deadline,stats)
        evaluator=GlobalRelayEvaluator(problem);base_schedule=evaluator.evaluate(base);_check(deadline,stats)
        schedule=evaluator.evaluate(initial);_check(deadline,stats)
        expected=closure.signature(problem,initial,pins,deadline=deadline)
        closure.shape(problem,initial,pinned_task_ids=pins,deadline=deadline)
        _check(deadline,stats);stats['initialization_completed']=True
    except Deadline:
        stats.update(skipped_reason='initialization_reached_absolute_deadline',runtime_seconds=perf_counter()-started,
            best_key=None,final_routes_sha256=None)
        return dict(routes=initial,evaluation=None,iterations=0,metadata=stats)
    serial=0;best=dict(id=serial,routes=initial,schedule=schedule,key=weighted_key(initial_checked.score,w,s),q=[0]*problem.drone_count)
    best_checked=initial_checked;best_snapshot=None
    def exact_state(parent,task,owner,route,insertion,layer):
        nonlocal serial
        _check(deadline,stats)
        routes=list(parent['routes']);routes[owner]=route;routes=tuple(routes)
        scored=evaluator.evaluate_delta(parent['routes'],parent['schedule'],routes);stats['global_delta_evaluations']+=1
        _check(deadline,stats)
        if not scored.feasible:return None
        try:closure.shape(problem,routes,pinned_task_ids=pins,deadline=deadline)
        except (ValueError,KeyError):stats['invalid_candidates']+=1;return None
        present=set(parent['schedule'].delivery_times_min)|{task}
        if set(scored.delivery_times_min)!=present:raise ValueError('dynamic branch changed task coverage')
        q=list(parent['q']);q[owner]-=1
        if min(q)<0:raise ValueError('dynamic owner exceeded real freed quota')
        serial+=1
        return dict(id=serial,routes=routes,schedule=scored,key=weighted_key(scored.score,w,s),q=q,
            insertions=(*parent.get('insertions',()),insertion),owner_choices=(*parent.get('owner_choices',()),owner))
    def checkpoint(state,record,force=False):
        nonlocal best,best_checked,best_snapshot
        if not force and state['key']>=best['key']:return
        _check(deadline,stats)
        try:
            full=evaluate_solution(problem,state['routes'],_force_full=True)
            if not full.valid or closure.signature(problem,state['routes'],pins,deadline=deadline)!=expected:
                stats['invalid_candidates']+=1;return
            score=state['schedule'].score
            if score.late_count!=full.score.late_count or abs(score.total_lateness_min-full.score.total_lateness_min)>1e-8 or abs(score.distance_km-full.score.distance_km)>1e-8:
                stats['invalid_candidates']+=1;return
            state['key']=weighted_key(full.score,w,s)
            snapshot=_snapshot(problem,state,full,w,s,phase='complete_joint_repair',base_schedule=base_schedule,
                initial_routes=initial,seed_move=seed_move,pins=pins,focus=focus,deadline=deadline,stats=stats)
        except (ValueError,KeyError,IndexError):
            stats['invalid_candidates']+=1;return
        stats['complete_leafs_full_verified']+=1
        record['complete_checkpoints'].append(dict(state_id=state['id'],key=snapshot['key'],
            routes=snapshot['routes'],parent_state_id=snapshot['parent_state_id'],
            inserted_task_order=snapshot['inserted_task_order'],owner_choices=snapshot['owner_choices'],
            insertions=snapshot['insertions'],
            full_verified_at_perf_counter=snapshot['full_verified_at_perf_counter'],
            changed_owner_task_ids=snapshot['changed_owner_task_ids']))
        if stats['first_complete_candidate'] is None:stats['first_complete_candidate']=snapshot
        if len(stats['full_candidate_snapshots'])<snapshot_limit:stats['full_candidate_snapshots'].append(snapshot)
        if state['key']<best['key']:
            _check(deadline,stats)
            snapshot['selected_as_best_at_perf_counter']=perf_counter()
            _check(deadline,stats)
            best,best_checked,best_snapshot=state,full,snapshot;stats['accepted_improvements']+=1
            stats['best_constructed_candidate']=snapshot;stats['returned_input_guard']=False
    try:
        _check(deadline,stats)
        best_snapshot=_snapshot(problem,best,initial_checked,w,s,phase='input',base_schedule=base_schedule,
            initial_routes=initial,seed_move=seed_move,pins=pins,focus=focus,deadline=deadline,stats=stats)
        stats['input_snapshot']=best_snapshot
        for round_number in range(max_rounds):
            _check(deadline,stats)
            before=best['routes']
            built=closure.build_closure(problem,base,before,pinned_task_ids=pins,focus_task_ids=focus,
                expected_signature=expected,closure_limit=closure_limit,partner_depth=partner_depth,variant=round_number,deadline=deadline)
            tasks=tuple(built['removed_task_ids']);partial=tuple(tuple(r) for r in built['partial_routes'])
            slots=list(built['quota_slots_by_aircraft'])
            preview=closure.partial_preview(problem,partial,pinned_task_ids=pins,expected_signature=expected,
                remaining_task_ids=tasks,remaining_quota_slots=slots,deadline=deadline,global_evaluator=evaluator)
            stats['global_partial_evaluations']+=1;serial+=1
            initial_state=dict(id=serial,routes=partial,schedule=preview['global_schedule'],key=weighted_key(preview['score'],w,s),q=slots)
            record=dict(round_index=round_number,before_routes=[list(r) for r in before],closure=built,
                initial_state=_state_evidence(initial_state,tasks),layers=[],complete_checkpoints=[],completed=False)
            stats['closure_records'].append(record);states=[initial_state]
            for offset in range(0,len(tasks),2):
                _check(deadline,stats);jobs=tasks[offset:offset+2];remaining=tasks[offset+len(jobs):]
                present=set(problem.task_ids)-set(remaining);expanded=[]
                layer=dict(layer_index=offset//2,task_ids=list(jobs),present_task_ids=sorted(present),
                    candidate_count=0,owner_branches={},retained_states=[],completed=False)
                record['layers'].append(layer)
                def second_children(one,second):
                    for other,free in enumerate(one['q']):
                        if free<=0:continue
                        for route2,step2 in _frontier(problem,one,second,other,position_limit,option_count,deadline,stats,layer):
                            two=exact_state(one,second,other,route2,step2,layer)
                            if two is not None:yield two
                for parent in states:
                    for order in permutations(jobs):
                        first=order[0]
                        for owner,q in enumerate(parent['q']):
                            if q<=0:continue
                            for route,step in _frontier(problem,parent,first,owner,position_limit,option_count,deadline,stats,layer):
                                one=exact_state({**parent,'insertions':(),'owner_choices':()},first,owner,route,step,layer)
                                if one is None:continue
                                if len(order)==1:leaves=[one]
                                else:
                                    leaves=second_children(one,order[1])
                                for leaf in leaves:
                                    if set(leaf['schedule'].delivery_times_min)!=present:raise ValueError('two-job layer mixed coverage')
                                    leaf.update(parent_id=parent['id'],order=order);expanded.append(leaf);layer['candidate_count']+=1
                                    identity=','.join(map(str,leaf['owner_choices']));layer['owner_branches'][identity]=layer['owner_branches'].get(identity,0)+1
                                    if not remaining:
                                        if any(leaf['q']):raise ValueError('complete task coverage left quota unfilled')
                                        stats['complete_proposals_generated']+=1
                                        checkpoint(leaf,record,force=not record['complete_checkpoints'])
                unique={}
                for state in expanded:
                    _check(deadline,stats);unique.setdefault(state['routes'],state)
                states=sorted(unique.values(),key=lambda x:(x['key'],x['routes']))[:beam_width]
                _check(deadline,stats)
                for state in states:
                    checked=closure.partial_preview(problem,state['routes'],pinned_task_ids=pins,expected_signature=expected,
                        remaining_task_ids=remaining,remaining_quota_slots=state['q'],deadline=deadline,global_evaluator=evaluator)
                    stats['global_partial_evaluations']+=1
                    if checked['score']!=state['schedule'].score:raise ValueError('retained partial actual DAG score differs')
                layer.update(retained_states=[_state_evidence(x,remaining) for x in states],completed=True)
                if not states:break
            _check(deadline,stats);record['search_finished']=True
            record['completed']=not tasks or (len(record['layers'])==(len(tasks)+1)//2 and bool(states))
            stats['completed_rounds']+=int(record['completed'])
            if not tasks:break
    except Deadline:stats['deadline_reached']=True
    stats.update(post_repair_snapshot=best_snapshot,best_key=list(best['key']),runtime_seconds=perf_counter()-started,
        returned_checkpoint_checked_within_deadline=best_snapshot is not None,
        final_routes_sha256=best_snapshot['routes_sha256'] if best_snapshot else None)
    return dict(routes=best['routes'],evaluation=best_checked,iterations=stats['global_delta_evaluations'],metadata=stats)
