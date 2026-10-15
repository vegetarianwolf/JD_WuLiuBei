"""Paid split-feeder initialization followed by causal, dynamic-quota repair.

This is a matched historical-checkpoint mechanism experiment. Both arms pay
for their raw catalogue; it is not the strongest fresh DIRECT time allocation.
Only complete current schedules can improve the original input guard.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter

from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import split_feeder_refinement_v4 as raw_method
    import critical_prefix_joint_repair_v1 as repair
except ImportError:
    from . import split_feeder_refinement_v4 as raw_method
    from . import critical_prefix_joint_repair_v1 as repair

Deadline=raw_method.Deadline
key=raw_method.weighted_key
_digest=raw_method._digest


def _check(deadline):
    if perf_counter()>=deadline:raise Deadline


def _routes(snapshot):return tuple(tuple(r) for r in snapshot['routes'])


def _separated(p,snapshot):
    move=snapshot.get('move')
    if not move or move['mode']!='RELAY':return False
    f=move['family'];pair={f['i'],f['j']};route=_routes(snapshot)[f['c']]
    ends=[k for k,v in enumerate(route) if v<0 and raw_method._task(p,v) in pair]
    return len(ends)==2 and any(raw_method._task(p,v) not in pair for v in route[ends[0]+1:ends[1]])


def _seeds(p,base,raw,relay,limit,original):
    snapshots=raw['metadata']['full_candidate_snapshots']
    if relay:
        candidates=[s for s in snapshots if s.get('move',{}).get('mode')=='RELAY']
        groups=[('minimum_J_RELAY',candidates),('minimum_J_genuine_separated_RELAY',[s for s in candidates if _separated(p,s)])]
    else:
        # A bare guard is a genuine DIRECT seed. It has no invented family.
        guard=raw['metadata']['input_snapshot']
        candidates=[guard,*snapshots]
        unique={}
        for s in candidates:unique.setdefault(_routes(s),s)
        ordered=sorted(unique.values(),key=lambda s:(tuple(s['key']),_routes(s)))
        groups=[('independent_DIRECT_rank_'+str(n+1),[s]) for n,s in enumerate(ordered[:limit])]
    result=[];seen=set()
    fallback=sorted(p.task_ids,key=lambda t:(-(original[t]-p.task(t).deadline_min),t))[:2]
    for channel,values in groups:
        if not values:continue
        s=min(values,key=lambda x:(tuple(x['key']),_routes(x)));routes=_routes(s)
        if routes in seen:continue
        seen.add(routes);move=s.get('move');f=move['family'] if move else None
        focus=sorted((f['i'],f['j'])) if f else sorted(fallback)
        result.append(dict(channel=channel,raw_snapshot=s,routes_sha256=_digest(routes),
            seed_move=move,focus_task_ids=focus,pinned_task_ids=focus if relay else [],
            genuinely_separated_receiver=_separated(p,s)))
        if len(result)>=limit:break
    return result


def _pin_guard(p,seed,current,focus,relay):
    old=build_plan_index(p,seed);now=build_plan_index(p,current)
    before=tuple(sum(v==a for v in old.delivery_route_by_task.values()) for a in range(p.drone_count))
    after=tuple(sum(v==a for v in now.delivery_route_by_task.values()) for a in range(p.drone_count))
    if before!=after:raise ValueError('post repair changed final responsibility counts')
    if not relay:
        if now.relay_task_ids:raise ValueError('DIRECT repair introduced a relay')
        return now
    pair=set(focus)
    if set(now.relay_task_ids)!=pair:raise ValueError('post relay identity changed')
    for t in focus:
        if (old.task_plans[t]!=now.task_plans[t] or any(getattr(old,name).get(t)!=getattr(now,name).get(t)
                for name in ('delivery_route_by_task','inbound_route_by_task','outbound_route_by_task'))):
            raise ValueError('post actual relay plan or role changed')
    for a in range(p.drone_count):
        if tuple(v for v in seed[a] if raw_method._task(p,v) in pair)!=tuple(v for v in current[a] if raw_method._task(p,v) in pair):
            raise ValueError('selected relay event subsequence changed')
    c=now.delivery_route_by_task[focus[0]]
    outgoing={next(v for v in now.task_plans[t].leg_ids if p.leg_registry[v].kind=='RELAY_OUT') for t in focus}
    starts=sorted(current[c].index(v) for v in outgoing)
    if starts[1]!=starts[0]+1 or sum(1 if v>0 else -1 for v in current[c][:starts[0]])!=0:
        raise ValueError('receiver consecutive empty doubletake changed')
    if raw_method._shared(p,current,focus,now.delivery_route_by_task)<=1e-10:
        raise ValueError('post no positive actual shared flight')
    return now


def _snapshot(p,routes,full,schedule,w,s,*,phase,seed_record=None):
    index=build_plan_index(p,routes);focus=seed_record['focus_task_ids'] if seed_record else []
    relay=bool(index.relay_task_ids);family=seed_record['seed_move']['family'] if relay else None
    shared=raw_method._shared(p,routes,focus,index.delivery_route_by_task) if relay else 0.
    handoff=raw_method._handoff(p,routes,schedule,family,index,shared) if relay else None
    return dict(routes=[list(r) for r in routes],score=raw_method._physical(full.score),key=list(key(full.score,w,s)),
        valid=True,complete=True,phase=phase,focus_task_ids=list(focus),
        event_times_min=[[schedule.event_times_min[a,k] for k in range(len(r))] for a,r in enumerate(routes)],
        delivery_times_min={str(t):v for t,v in sorted(schedule.delivery_times_min.items())},
        actual_final_owners={str(t):a for t,a in sorted(index.delivery_route_by_task.items())},
        cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        relay_task_count=len(index.relay_task_ids),source_drop_availability_min=handoff['source_availability_min'] if handoff else {},
        shared_loaded_distance_km=shared,handoff=handoff,
        seed_routes_sha256=seed_record['routes_sha256'] if seed_record else None)


def refine(problem,routes,*,deadline,weights,scales,seed=2026101520,relay_enabled=True,
        raw_fraction=.35,repair_seed_limit=2,max_families=16,base_pool_limit=64,
        source_position_limit=8,source_option_limit=3,table_option_limit=3,feeder_limit=3,
        full_candidates_per_family=3,sigma=0.,closure_limit=12,partner_depth=1,
        beam_width=6,position_limit=12,option_count=3,max_rounds=2,snapshot_limit=6):
    started=perf_counter();w,s=tuple(weights),tuple(scales)
    if (type(deadline) is bool or not isfinite(deadline) or type(relay_enabled) is not bool
        or not 0<raw_fraction<1 or type(repair_seed_limit) is not int or not 1<=repair_seed_limit<=2):
        raise ValueError('finite deadline, real arm and bounded raw/seed allocation required')
    base=tuple(tuple(r) for r in routes)
    checked=evaluate_solution(problem,base,_force_full=True)
    if not checked.valid or build_plan_index(problem,base).relay_task_ids:raise ValueError('real complete pure DIRECT guard required')
    evaluator=GlobalRelayEvaluator(problem);baseline=evaluator.evaluate(base)
    best,best_checked,best_key=base,checked,key(checked.score,w,s)
    raw_parameters=dict(max_families=max_families,base_pool_limit=base_pool_limit,
        source_position_limit=source_position_limit,source_option_limit=source_option_limit,
        table_option_limit=table_option_limit,feeder_limit=feeder_limit,
        full_candidates_per_family=full_candidates_per_family,sigma=sigma)
    repair_parameters=dict(closure_limit=closure_limit,partner_depth=partner_depth,beam_width=beam_width,
        position_limit=position_limit,option_count=option_count,max_rounds=max_rounds,snapshot_limit=snapshot_limit)
    stats=dict(method='Causal-Prefix-Dynamic-Quota-Joint-Repair-V1',seed=seed,relay_enabled=relay_enabled,
        started_at_perf_counter=started,
        deadline_perf_counter=deadline,deadline_reached=False,raw_deadline_perf_counter=None,
        raw_metadata=None,raw_output_routes=None,raw_stage_returned_at_perf_counter=None,
        seed_records=[],repair_records=[],accepted_moves=[],accepted_improvements=0,
        full_candidate_snapshots=[],initial_key=list(best_key),final_key=list(best_key),
        input_snapshot=_snapshot(problem,base,checked,baseline,w,s,phase='original_guard'),
        configuration=dict(raw_method='split_feeder_refinement_v4',raw_fraction=raw_fraction,
            repair_seed_limit=repair_seed_limit,raw_parameters=raw_parameters,repair_parameters=repair_parameters,
            matched_shared_raw_diagnostic=True,strongest_fresh_end_to_end_comparison=False,
            final_guard='original_complete_input_strict_weighted',wrapper_verification_reserve_seconds=.15,
            direct_post_pins_empty=True,relay_post_pins_two_actual_plans=True,
            no_free_historical_relay_seed=True,resources_homes_weights_unchanged=True))
    final_snapshot=stats['input_snapshot'];iterations=0

    def consider(candidate,record,source):
        nonlocal best,best_checked,best_key,final_snapshot
        _check(deadline)
        current=tuple(tuple(r) for r in candidate)
        full=evaluate_solution(problem,current,_force_full=True);schedule=evaluator.evaluate(current)
        if not full.valid or not schedule.feasible:raise ValueError('wrapper received invalid complete candidate')
        _pin_guard(problem,_routes(record['raw_snapshot']),current,record['focus_task_ids'],relay_enabled)
        snapshot=_snapshot(problem,current,full,schedule,w,s,phase=source,seed_record=record)
        verified=perf_counter();_check(deadline)
        snapshot.update(full_verified_at_perf_counter=verified,deadline_perf_counter=deadline)
        stats['full_candidate_snapshots'].append(snapshot)
        candidate_key=key(full.score,w,s)
        if candidate_key<best_key:
            accepted=perf_counter();_check(deadline)
            best,best_checked,best_key,final_snapshot=current,full,candidate_key,snapshot
            stats['accepted_moves'].append(dict(source=source,seed_routes_sha256=record['routes_sha256'],
                full_snapshot=snapshot,full_verified_at_perf_counter=verified,accepted_at_perf_counter=accepted))
            stats['accepted_improvements']+=1

    try:
        _check(deadline)
        raw_deadline=started+(deadline-started)*raw_fraction
        stats['raw_deadline_perf_counter']=raw_deadline
        raw=raw_method.refine(problem,base,deadline=raw_deadline,weights=w,scales=s,seed=seed,
            relay_enabled=relay_enabled,**raw_parameters)
        iterations+=raw['iterations'];stats['raw_metadata']=raw['metadata']
        stats['raw_output_routes']=[list(r) for r in raw['routes']]
        stats['raw_stage_returned_at_perf_counter']=perf_counter();_check(deadline)
        selected=_seeds(problem,base,raw,relay_enabled,repair_seed_limit,baseline.delivery_times_min)
        stats['seed_records']=selected
        for number,record in enumerate(selected):
            _check(deadline)
            consider(_routes(record['raw_snapshot']),record,'raw_seed_'+str(number))
            remaining=len(selected)-number
            repair_deadline=perf_counter()+max(0.,deadline-.15-perf_counter())/remaining
            entry=dict(seed_index=number,channel=record['channel'],deadline_perf_counter=repair_deadline,
                returned=False,metadata=None,returned_routes=None)
            stats['repair_records'].append(entry)
            outcome=repair.refine(problem,base,_routes(record['raw_snapshot']),seed_move=record['seed_move'],
                focus_task_ids=tuple(record['focus_task_ids']),deadline=repair_deadline,weights=w,scales=s,
                seed=seed+number,relay_enabled=relay_enabled,**repair_parameters)
            iterations+=outcome.get('iterations',0)
            entry.update(returned=True,metadata=outcome['metadata'],returned_routes=[list(r) for r in outcome['routes']],
                returned_at_perf_counter=perf_counter())
            _check(deadline)
            checkpoint=outcome['metadata'].get('post_repair_snapshot')
            if outcome['metadata'].get('returned_checkpoint_checked_within_deadline') is not True:
                if checkpoint is not None:raise ValueError('unchecked helper return claims a checkpoint')
                entry['post_consideration_skipped']='no_helper_predeadline_complete_checkpoint'
                continue
            if (checkpoint is None or checkpoint.get('valid') is not True or checkpoint.get('complete') is not True
                or checkpoint['routes']!=entry['returned_routes']
                or checkpoint['routes_sha256']!=_digest(entry['returned_routes'])
                or checkpoint['full_verified_at_perf_counter']>=repair_deadline
                or checkpoint['deadline_perf_counter']!=repair_deadline):
                raise ValueError('helper returned route is not its actual predeadline complete checkpoint')
            consider(outcome['routes'],record,'repair_seed_'+str(number))
    except Deadline:
        stats['deadline_reached']=True
    stats.update(final_key=list(best_key),final_snapshot=final_snapshot,runtime_seconds=perf_counter()-started)
    return dict(routes=best,evaluation=best_checked,iterations=iterations,metadata=stats)
