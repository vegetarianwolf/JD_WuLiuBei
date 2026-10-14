"""Independent paid DIRECT search, without a relay catalogue or relay pins.

Requires the same frozen weighted ALNS runtime as the research worker. The
historical-checkpoint diagnostic remains separate from final fresh evidence.
"""
from __future__ import annotations
from contextlib import nullcontext
from dataclasses import replace,asdict
from math import isfinite
from time import perf_counter
from uav_dispatch import alns
from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import weighted_direct_objective as weighted
    import weighted_periodic_exchange as periodic
    import critical_prefix_refinement_v1 as tools
    import conditional_donor_repair_v1 as repair
except ImportError:
    from . import weighted_direct_objective as weighted
    from . import weighted_periodic_exchange as periodic
    from . import critical_prefix_refinement_v1 as tools
    from . import conditional_donor_repair_v1 as repair


def refine(problem,routes,*,deadline,weights,scales,seed,relay_enabled=False,
        alns_fraction=.65,critical_limit=6,donor_limit=3,position_limit=12,
        option_count=3,beam_width=4,max_steps=3,snapshot_limit=6):
    started=perf_counter();w,s=tuple(weights),tuple(scales)
    if relay_enabled is not False or not isfinite(deadline) or not 0<alns_fraction<1:
        raise ValueError('Independent DIRECT requires a finite deadline and no relays')
    if getattr(alns,'_weighted_adapter',None) is not weighted:
        raise ValueError('Use the frozen weighted ALNS archive, not an unadapted search runtime')
    if weighted.active and tuple(weighted.coefficients)!=tuple(x/y for x,y in zip(w,s)):
        raise ValueError('Active weighted ALNS objective does not match this control')
    base=tuple(tuple(r) for r in routes);checked=evaluate_solution(problem,base,_force_full=True)
    if not checked.valid or build_plan_index(problem,base).relay_task_ids:
        raise ValueError('Complete pure DIRECT input required')
    evaluator=GlobalRelayEvaluator(problem);initial_schedule=evaluator.evaluate(base)
    initial=tools._snapshot(problem,base,checked,initial_schedule,w,s,phase='original_guard')
    best,best_checked,best_key,final=base,checked,tools.key(checked.score,w,s),initial
    stats=dict(method='Independent-Weighted-DIRECT-ALNS-And-Complete-Exchange-V1',seed=seed,
        relay_enabled=False,started_at_perf_counter=started,deadline_perf_counter=deadline,
        deadline_reached=False,input_snapshot=initial,initial_key=list(best_key),final_key=list(best_key),
        alns_record=None,repair_record=None,full_candidate_snapshots=[],accepted_moves=[],accepted_improvements=0,
        configuration=dict(alns_fraction=alns_fraction,no_relay_catalogue=True,pins_empty=True,
            all_tasks_native_free=True,final_guard='original_complete_input_strict_weighted',
            strongest_fresh_end_to_end_comparison=False,wrapper_verification_reserve_seconds=.15,
            repair_parameters=dict(critical_limit=critical_limit,donor_limit=donor_limit,position_limit=position_limit,
                option_count=option_count,beam_width=beam_width,max_steps=max_steps,snapshot_limit=snapshot_limit)))
    iterations=0
    def consider(candidate,phase,focus=(),source_routes=None):
        nonlocal best,best_checked,best_key,final
        tools._check(deadline)
        current=tuple(tuple(r) for r in candidate)
        full=evaluate_solution(problem,current,_force_full=True);schedule=evaluator.evaluate(current)
        if not full.valid or not schedule.feasible or build_plan_index(problem,current).relay_task_ids:
            raise ValueError('DIRECT control returned an invalid or non-DIRECT complete plan')
        record=None if source_routes is None else dict(focus_task_ids=list(focus),seed_move=None,
            routes_sha256=tools._digest(source_routes))
        snapshot=tools._snapshot(problem,current,full,schedule,w,s,phase=phase,seed_record=record)
        verified=perf_counter();tools._check(deadline)
        snapshot.update(full_verified_at_perf_counter=verified,deadline_perf_counter=deadline)
        stats['full_candidate_snapshots'].append(snapshot)
        current_key=tools.key(full.score,w,s)
        if current_key<best_key:
            accepted=perf_counter();tools._check(deadline)
            best,best_checked,best_key,final=current,full,current_key,snapshot
            stats['accepted_moves'].append(dict(source=phase,full_snapshot=snapshot,
                full_verified_at_perf_counter=verified,accepted_at_perf_counter=accepted))
            stats['accepted_improvements']+=1
        return full,schedule
    try:
        tools._check(deadline)
        plain=replace(problem,leg_registry={},task_relay_candidates={})
        sub_deadline=started+(deadline-started)*alns_fraction
        configured=perf_counter()
        cfg=alns.ALNSConfig(max_iterations=10_000_000,seed=seed,
            time_limit_seconds=max(.001,sub_deadline-configured),relay_enabled=False,
            enable_home_seed=True,enable_home_bias=True,enable_terminal_task_exchange=True,enable_lazy_repair=True)
        periodic_stats={};begin=perf_counter()
        periodic_configuration=dict(interval=120,max_candidates=16,anchors_per_route=4,pair_budget_seconds=.008)
        context=nullcontext() if weighted.active else weighted.objective(Score,s,w)
        with context:
            effective_coefficients=tuple(weighted.coefficients)
            with periodic.periodic_exchange(alns,statistics=periodic_stats,**periodic_configuration):
                outcome=alns.solve_alns(plain,config=cfg,initial_routes=base)
        iterations+=outcome.iterations
        stats['alns_record']=dict(deadline_perf_counter=sub_deadline,started_at_perf_counter=begin,
            returned_at_perf_counter=perf_counter(),routes=[list(r) for r in outcome.routes],config=asdict(cfg),
            configured_at_perf_counter=configured,periodic_configuration=periodic_configuration,
            objective_mode='frozen_weighted_ALNS',effective_coefficients=list(effective_coefficients),
            iterations=outcome.iterations,periodic_task_exchange=periodic_stats)
        full,schedule=consider(outcome.routes,'independent_DIRECT_ALNS')
        repair_base=tuple(tuple(r) for r in outcome.routes)
        focus=tuple(sorted(sorted(problem.task_ids,key=lambda t:(-(schedule.delivery_times_min[t]-problem.task(t).deadline_min),t))[:2]))
        repair_deadline=deadline-.15
        entry=dict(base_routes=[list(r) for r in repair_base],base_routes_sha256=tools._digest(repair_base),
            focus_task_ids=list(focus),deadline_perf_counter=repair_deadline,returned=False,metadata=None,returned_routes=None)
        stats['repair_record']=entry
        outcome=repair.refine(problem,repair_base,repair_base,seed_move=None,focus_task_ids=focus,
            deadline=repair_deadline,weights=w,scales=s,seed=seed,relay_enabled=False,
            **stats['configuration']['repair_parameters'])
        iterations+=outcome['iterations']
        meta=outcome['metadata'];entry.update(returned=True,metadata=meta,
            returned_routes=[list(r) for r in outcome['routes']],returned_at_perf_counter=perf_counter())
        tools._check(deadline)
        checkpoint=meta.get('post_repair_snapshot')
        if meta.get('returned_checkpoint_checked_within_deadline') is True:
            if (not checkpoint or checkpoint.get('valid') is not True or checkpoint.get('complete') is not True
                or checkpoint['routes']!=entry['returned_routes'] or checkpoint['routes_sha256']!=tools._digest(outcome['routes'])
                or checkpoint['full_verified_at_perf_counter']>=repair_deadline or checkpoint['deadline_perf_counter']!=repair_deadline):
                raise ValueError('Independent DIRECT repair has no matching predeadline complete checkpoint')
            consider(outcome['routes'],'independent_DIRECT_complete_exchange',focus,repair_base)
        else:
            if checkpoint is not None:raise ValueError('Unchecked DIRECT repair fabricated a checkpoint')
            entry['post_consideration_skipped']='no_helper_predeadline_complete_checkpoint'
    except tools.Deadline:
        stats['deadline_reached']=True
    stats.update(final_snapshot=final,final_key=list(best_key),runtime_seconds=perf_counter()-started)
    return dict(routes=best,evaluation=best_checked,iterations=iterations,metadata=stats)
