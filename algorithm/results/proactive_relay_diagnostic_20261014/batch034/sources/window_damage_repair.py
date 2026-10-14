"""Paid window seeds followed by damage-directed quota joint reconstruction."""
from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite,isclose
from time import perf_counter

from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import deadline_window_repooling as raw_method
    import damage_closure_repair as repair
except ImportError:
    from . import deadline_window_repooling as raw_method
    from . import damage_closure_repair as repair

Deadline=repair.Deadline
SEED_POLICY='genuine_pooled_first_positive_route_dedup_minJ_then_max_pooled_scan_remaining'


def _digest(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _check(deadline,stats):
    if perf_counter()>=deadline:
        stats['deadline_reached']=True
        raise Deadline


def refine(problem,routes,*,deadline,weights,scales,seed=2026101515,relay_enabled=True,
           proposal_fraction=.30,candidate_limit=2,guard_reserve_seconds=.03,guard_reserve_fraction=.05,
           closure_limit=16,partner_depth=1,beam_width=4,position_limit=16,option_count=2,
           global_option_probe_limit=4,max_rounds=2,snapshot_limit=8,max_families=16,base_pool_limit=64,
           source_position_limit=8,source_option_limit=3,table_option_limit=3,compensation_pool_limit=6,
           compensation_limit=3,receiver_gap_limit=4,direct_gap_limit=4,full_candidates_per_family=3,sigma=0.):
    started=perf_counter();w,s=tuple(weights),tuple(scales)
    limits=((closure_limit,64),(partner_depth,3),(beam_width,4),(position_limit,64),(option_count,2),
        (global_option_probe_limit,16),(max_rounds,4),(snapshot_limit,16))
    if (not isfinite(deadline) or type(relay_enabled) is not bool or len(w)!=3 or len(s)!=3
        or any(not isfinite(x) or x<=0 for x in (*w,*s)) or not isclose(sum(w),1.)
        or not isfinite(proposal_fraction) or not 0<proposal_fraction<1
        or type(candidate_limit) is not int or not 1<=candidate_limit<=2
        or not isfinite(guard_reserve_seconds) or guard_reserve_seconds<0
        or not isfinite(guard_reserve_fraction) or not 0<=guard_reserve_fraction<1
        or any(type(x) is not int or not 1<=x<=upper for x,upper in limits)
        or position_limit<4 or global_option_probe_limit<option_count):raise ValueError('finite weighted budget and bounded seed/joint policy required')
    base=tuple(tuple(r) for r in routes)
    if len(base)>problem.drone_count:raise ValueError('too many input routes')
    base+=((),)*(problem.drone_count-len(base));initial=evaluate_solution(problem,base,_force_full=True)
    if not initial.valid or build_plan_index(problem,base).relay_task_ids:raise ValueError('valid complete pure DIRECT source required')
    evaluator=GlobalRelayEvaluator(problem);base_schedule=evaluator.evaluate(base)
    initial_key=best_key=raw_method.weighted_key(initial.score,w,s);best,best_checked=base,initial
    now=perf_counter();proposal_deadline=now+max(0.,deadline-now)*proposal_fraction
    raw=raw_method.refine(problem,base,deadline=proposal_deadline,weights=w,scales=s,seed=seed,relay_enabled=relay_enabled,
        max_families=max_families,base_pool_limit=base_pool_limit,source_position_limit=source_position_limit,
        source_option_limit=source_option_limit,table_option_limit=table_option_limit,compensation_pool_limit=compensation_pool_limit,
        compensation_limit=compensation_limit,receiver_gap_limit=receiver_gap_limit,direct_gap_limit=direct_gap_limit,
        full_candidates_per_family=full_candidates_per_family,sigma=sigma)
    source=raw['metadata']
    stats=dict(method='Window-Damage-Closure-Quota-Repair',seed=seed,relay_enabled=relay_enabled,
        deadline_perf_counter=deadline,proposal_deadline_perf_counter=proposal_deadline,raw_stage=source,
        proposal_runtime_seconds=perf_counter()-now,input_snapshot=source['input_snapshot'],
        selected_families=source['selected_families'],selector_sha256=source['selector_sha256'],selector_completed=source['selector_completed'],
        selector_completed_at_perf_counter=source['selector_completed_at_perf_counter'],deadline_reached=False,
        initial_key=list(initial_key),final_key=list(initial_key),seed_selection_policy=SEED_POLICY,
        seed_selection_records=[],seed_selection_completed=False,seed_selection_completed_at_perf_counter=None,
        polish_records=[],polish_calls=0,polish_iterations=0,
        full_candidate_snapshots=[],first_complete_candidate=None,best_constructed_candidate=None,
        accepted_moves=[],accepted_improvements=0,ranking_full_evaluations=0,wrapper_full_evaluations=1,
        rejected_helper_results=0,skipped_timeout=0,final_cross_uav_count=0,pin_scope='window_damage_repair',
        configuration=dict(proposal_fraction=proposal_fraction,candidate_limit=candidate_limit,
            proposal_margin=None,genuine_pooled_channel_has_no_J_margin_gate=True,seed_policy=SEED_POLICY,
            guard_reserve_seconds=guard_reserve_seconds,guard_reserve_fraction=guard_reserve_fraction,
            closure_limit=closure_limit,partner_depth=partner_depth,beam_width=beam_width,position_limit=position_limit,
            option_count=option_count,global_option_probe_limit=global_option_probe_limit,max_rounds=max_rounds,snapshot_limit=snapshot_limit,
            selected_macro_identities_paired=False,ordinary_tasks_direct_only=True,polish_uses_ALNS=False,
            raw_parameters=dict(max_families=max_families,base_pool_limit=base_pool_limit,source_position_limit=source_position_limit,
                source_option_limit=source_option_limit,table_option_limit=table_option_limit,compensation_pool_limit=compensation_pool_limit,
                compensation_limit=compensation_limit,receiver_gap_limit=receiver_gap_limit,direct_gap_limit=direct_gap_limit,
                full_candidates_per_family=full_candidates_per_family,sigma=sigma)))
    try:
        _check(deadline,stats)
        if raw.get('problem',problem) is not problem:raise ValueError('raw changed fixed Problem')
        raw_routes=tuple(tuple(r) for r in raw['routes']);raw_checked=evaluate_solution(problem,raw_routes,_force_full=True)
        stats['wrapper_full_evaluations']+=1;_check(deadline,stats)
        if not raw_checked.valid:raise ValueError('invalid returned raw guard')
        raw_key=raw_method.weighted_key(raw_checked.score,w,s)
        if raw_key<best_key:
            macro=next((c for c in source['full_candidate_snapshots'] if tuple(tuple(r) for r in c['routes'])==raw_routes),None)
            if macro is None:raise ValueError('raw winner lacks complete source evidence')
            raw_method._verify(problem,base,raw_routes,macro['move'],relay_enabled)
            checked_at=perf_counter();_check(deadline,stats)
            accepted_at=perf_counter()
            if accepted_at>=deadline:raise Deadline
            best,best_checked,best_key=raw_routes,raw_checked,raw_key;stats['accepted_improvements']+=1
            stats['accepted_moves'].append(dict(kind='raw_macro',full_snapshot=macro,full_proposal=macro,
                macro_routes=macro['routes'],original_macro_move=macro['move'],full_verified_at_perf_counter=checked_at,
                accepted_at_perf_counter=accepted_at))
        candidates=[];seen=set()
        for n,macro in enumerate(source['full_candidate_snapshots']):
            _check(deadline,stats);candidate=tuple(tuple(r) for r in macro['routes'])
            if candidate in seen:continue
            full=evaluate_solution(problem,candidate,_force_full=True)
            stats['ranking_full_evaluations']+=1;_check(deadline,stats)
            if not full.valid:raise ValueError('invalid saved full raw seed')
            f=macro['move']['family'];pins=(f['i'],f['j'])
            rescued=sorted(t for t in pins if base_schedule.delivery_times_min[t]-problem.task(t).deadline_min>1e-9
                and full.delivery_times_min[t]-problem.task(t).deadline_min<=1e-9)
            if rescued:
                seen.add(candidate)
                candidates.append(dict(index=n,macro=macro,routes=candidate,key=raw_method.weighted_key(full.score,w,s),rescued=rescued))
        selected=[]
        if candidates:
            selected.append(min(candidates,key=lambda c:(c['key'],c['routes'])))
            remaining=[c for c in candidates if c['routes']!=selected[0]['routes']]
            if candidate_limit>1 and remaining:selected.append(min(remaining,key=lambda c:(-len(c['rescued']),c['key'],c['routes'])))
        for number,candidate in enumerate(selected):
            _check(deadline,stats)
            macro=candidate['macro'];move=macro['move'];pins=frozenset((move['family']['i'],move['family']['j']))
            stats['seed_selection_records'].append(dict(raw_snapshot_index=candidate['index'],full_proposal_sha256=_digest(macro),
                routes_sha256=_digest(macro['routes']),key=list(candidate['key']),pooled_rescued_task_ids=candidate['rescued'],
                selection_channel='minimum_true_J_genuine_pooled' if number==0 else 'maximum_pooled_then_J_distinct_route'))
        seed_completed=perf_counter()
        if seed_completed>=deadline:raise Deadline
        stats.update(seed_selection_completed=True,seed_selection_completed_at_perf_counter=seed_completed)
        for number,candidate in enumerate(selected):
            _check(deadline,stats);macro=candidate['macro'];move=macro['move'];pins=frozenset((move['family']['i'],move['family']['j']))
            cross=pins if relay_enabled else frozenset();expected=repair._signature(problem,candidate['routes'],pins)
            leaf_started=perf_counter();leaf_end=min(deadline,leaf_started+(deadline-leaf_started)/(len(selected)-number))
            reserve=min(guard_reserve_seconds,(leaf_end-leaf_started)*guard_reserve_fraction)
            helper_deadline=leaf_end-reserve;stats['polish_calls']+=1
            outcome=repair.repair_damage_closure(problem,base,candidate['routes'],original_macro_move=move,relay_enabled=relay_enabled,
                deadline=helper_deadline,weights=w,scales=s,seed=seed+number+1,closure_limit=closure_limit,partner_depth=partner_depth,
                beam_width=beam_width,position_limit=position_limit,option_count=option_count,global_option_probe_limit=global_option_probe_limit,
                max_rounds=max_rounds,snapshot_limit=snapshot_limit,variant=number)
            stats['polish_iterations']+=outcome.get('iterations',0)
            record=dict(full_proposal=macro,macro_routes=macro['routes'],original_macro_move=move,helper_metadata=outcome['metadata'],
                post_polish=None,adopted=False,finished_within_leaf_deadline=False,rejection_reason=None,
                leaf_started_at_perf_counter=leaf_started,leaf_deadline_perf_counter=leaf_end,
                helper_deadline_perf_counter=helper_deadline,guard_reserve_seconds=reserve)
            stats['polish_records'].append(record)
            if outcome.get('problem',problem) is not problem:
                record['rejection_reason']='fixed_problem_changed';stats['rejected_helper_results']+=1;continue
            proposed=tuple(tuple(r) for r in outcome['routes'])
            try:
                full=repair._validate(problem,proposed,pins,expected,cross);stats['wrapper_full_evaluations']+=1
                schedule=evaluator.evaluate(proposed)
                post=repair._snapshot(problem,proposed,full,schedule,w,s,base_schedule=base_schedule,move=move,phase='post_window_damage_repair')
            except (ValueError,KeyError,IndexError):
                record['rejection_reason']='invalid_complete_pin_or_physical_shape';stats['rejected_helper_results']+=1;continue
            verified=perf_counter();record['full_verified_at_perf_counter']=verified
            if verified>=leaf_end:
                record['rejection_reason']='full_check_or_snapshot_after_leaf_deadline';stats['skipped_timeout']+=1
                _check(deadline,stats);continue
            record.update(post_polish=post,finished_within_leaf_deadline=True)
            stats['full_candidate_snapshots'].append(post)
            if stats['first_complete_candidate'] is None:stats['first_complete_candidate']=post
            if stats['best_constructed_candidate'] is None or tuple(post['key'])<tuple(stats['best_constructed_candidate']['key']):stats['best_constructed_candidate']=post
            if not outcome['metadata'].get('returned_checkpoint_checked_within_deadline',False):
                record['rejection_reason']='helper_checkpoint_not_checked_before_deadline';stats['skipped_timeout']+=1;continue
            key=raw_method.weighted_key(full.score,w,s)
            if key>=best_key:
                record['rejection_reason']='not_better_than_guarded_strong_input';continue
            accepted=perf_counter()
            if accepted>=leaf_end:
                record['rejection_reason']='adoption_after_leaf_deadline';stats['skipped_timeout']+=1
                _check(deadline,stats);continue
            best,best_checked,best_key=proposed,full,key;record['adopted']=True;stats['accepted_improvements']+=1
            stats['accepted_moves'].append(dict(kind='damage_closure_repair',full_snapshot=post,full_proposal=macro,
                macro_routes=macro['routes'],original_macro_move=move,full_verified_at_perf_counter=verified,accepted_at_perf_counter=accepted))
    except Deadline:
        stats['deadline_reached']=True
    index=build_plan_index(problem,best)
    stats.update(final_key=list(best_key),final_cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        runtime_seconds=perf_counter()-started)
    return dict(routes=best,evaluation=best_checked,iterations=raw['iterations']+stats['polish_iterations'],metadata=stats)
