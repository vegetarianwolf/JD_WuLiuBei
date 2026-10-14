"""Paid raw initialization and conditionally selected complete native swaps.

A matched historical-checkpoint mechanism probe. Local success is not the
requested far-superiority result; it does not add further relay groups.
"""
from __future__ import annotations
from math import isfinite
from time import perf_counter
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    from . import critical_prefix_refinement_v1 as previous
    from . import conditional_donor_repair_v1 as repair
except ImportError:
    import critical_prefix_refinement_v1 as previous
    import conditional_donor_repair_v1 as repair
raw_method=previous.raw_method
Deadline=previous.Deadline
key=previous.key
_digest=previous._digest
_check=previous._check
_routes=previous._routes
_seeds=previous._seeds
_pin_guard=previous._pin_guard
_snapshot=previous._snapshot
_separated=previous._separated


def refine(problem,routes,*,deadline,weights,scales,seed=2026101521,relay_enabled=True,
        raw_fraction=.35,repair_seed_limit=2,max_families=16,base_pool_limit=64,
        source_position_limit=8,source_option_limit=3,table_option_limit=3,feeder_limit=3,
        full_candidates_per_family=3,sigma=0.,critical_limit=6,donor_limit=3,
        beam_width=4,position_limit=12,option_count=3,max_steps=2,snapshot_limit=6):
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
    repair_parameters=dict(critical_limit=critical_limit,donor_limit=donor_limit,beam_width=beam_width,
        position_limit=position_limit,option_count=option_count,max_steps=max_steps,snapshot_limit=snapshot_limit)
    stats=dict(method='Conditional-Donor-Complete-Exchange-Refinement-V1',seed=seed,relay_enabled=relay_enabled,
        started_at_perf_counter=started,
        deadline_perf_counter=deadline,deadline_reached=False,raw_deadline_perf_counter=None,
        raw_metadata=None,raw_output_routes=None,raw_stage_returned_at_perf_counter=None,
        seed_records=[],repair_records=[],accepted_moves=[],accepted_improvements=0,
        full_candidate_snapshots=[],initial_key=list(best_key),final_key=list(best_key),
        input_snapshot=_snapshot(problem,base,checked,baseline,w,s,phase='original_guard'),
        configuration=dict(repair_method='conditional_donor_repair_v1',raw_method='split_feeder_refinement_v4',raw_fraction=raw_fraction,
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
