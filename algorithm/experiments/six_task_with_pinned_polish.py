"""Keep a six-request sorting structure while ordinary tasks are reoptimized.

Raw evidence remains the frozen expanded six-task constructor's complete
source-bound output. Post snapshots are separate current global schedules;
they never claim that a raw separable route-table score still scores the
changed native routes. Each arm polishes its own best complete macros under
the same policy, then retains only a pre-deadline strict input improvement.
"""
from __future__ import annotations

from dataclasses import replace
from math import isclose,isfinite
from time import perf_counter

from uav_dispatch.relay import build_plan_index
from uav_dispatch.validation import evaluate_solution

try:
    import expanded_six_task_sorting as raw_method
    import six_task_preserving_polish as helper
except ImportError:
    from . import expanded_six_task_sorting as raw_method
    from . import six_task_preserving_polish as helper


_active=False


class _Deadline(Exception):
    pass


def _check(deadline,stats):
    if perf_counter()>=deadline:
        stats["deadline_reached"]=True
        raise _Deadline


def _decorate(snapshot,move,pins):
    if snapshot is None:return
    # Only tiny provenance fields are attached. Current physical values are
    # produced by the new helper, with no repeated topology or deep copy.
    snapshot.update(original_macro_move=move,pinned_task_ids=list(pins),pin_scope="six_task_sorting")
    snapshot["move"]={"semantic":"post_six_task_pinned_polish"}
    if "table_score" in snapshot:
        raise AssertionError("post-polish snapshot cannot reuse a raw route-table score")


def _decorate_helper(metadata,move,pins):
    for name in ("input_snapshot","first_complete_candidate","best_constructed_candidate","post_pin_snapshot"):
        _decorate(metadata.get(name),move,pins)
    for snapshot in metadata.get("full_candidate_snapshots",[]):_decorate(snapshot,move,pins)


def refine(problem,routes,*,deadline,weights,scales,seed=2026101513,relay_enabled=True,
           alns_module=None,polish_config=None,proposal_fraction=.4,candidate_limit=2,proposal_margin=.015,
           guard_reserve_seconds=.03,guard_reserve_fraction=.05,max_stations=4,families_per_station=64,
           source_pair_pool_limit=4,in_candidates=2,in_gap_limit=4,out_gap_limit=4,direct_gap_limit=4,
           full_candidates_per_family=2,polish_initial_temperature=.0025,
           polish_minimum_temperature=.00001,polish_cooling_rate=.97,polish_max_iterations=1000000):
    """Use one absolute budget for selection, raw tables and two native repairs."""
    global _active
    if _active:raise RuntimeError("nested six-task pinned facades are unsupported")
    started=perf_counter();weights,scales=tuple(weights),tuple(scales)
    temperature=(polish_initial_temperature,polish_minimum_temperature,polish_cooling_rate)
    if (not isfinite(deadline) or type(relay_enabled) is not bool or len(weights)!=3 or len(scales)!=3
            or any(not isfinite(v) or v<=0 for v in (*weights,*scales)) or not isclose(sum(weights),1.)
            or not isfinite(proposal_fraction) or not 0<proposal_fraction<1
            or type(candidate_limit) is not int or not 1<=candidate_limit<=2
            or not isfinite(proposal_margin) or proposal_margin<0
            or not isfinite(guard_reserve_seconds) or guard_reserve_seconds<0
            or not isfinite(guard_reserve_fraction) or not 0<=guard_reserve_fraction<1
            or any(type(v) is bool or not isfinite(v) or v<=0 for v in temperature)
            or polish_minimum_temperature>polish_initial_temperature or polish_cooling_rate>1
            or type(polish_max_iterations) is not int or not 1<=polish_max_iterations<=1000000):
        raise ValueError("finite weighted budget, bounded candidates and cold profile required")
    if alns_module is None:
        from uav_dispatch import alns as alns_module
    if polish_config is not None and not isinstance(polish_config,alns_module.ALNSConfig):
        raise ValueError("polish_config must use the actual worker ALNSConfig")
    config=replace(polish_config if polish_config is not None else alns_module.ALNSConfig(),
        initial_temperature=polish_initial_temperature,minimum_temperature=polish_minimum_temperature,
        cooling_rate=polish_cooling_rate,max_iterations=polish_max_iterations)
    base=tuple(tuple(r) for r in routes)
    if len(base)>problem.drone_count:raise ValueError("too many initial routes")
    base+=((),)*(problem.drone_count-len(base))
    checked=evaluate_solution(problem,base,_force_full=True)
    if not checked.valid or build_plan_index(problem,base).relay_task_ids:
        raise ValueError("six-task pinned polish requires a valid pure DIRECT initial solution")
    constructor=raw_method.frozen
    initial_key=best_key=constructor.weighted_key(checked.score,weights,scales)
    best,best_checked=base,checked
    selector_original=constructor._select_families
    alns_originals={name:getattr(alns_module,name) for name in helper.PATCH_NAMES}
    adapter=getattr(alns_module,"_weighted_adapter",None)
    adapter_state=(adapter.active,adapter.coefficients) if adapter is not None else None
    score_type=getattr(alns_module,"Score",helper.Score)
    score_originals={name:getattr(score_type,name) for name in ("__lt__","__le__","__gt__","__ge__")}
    _active=True
    try:
        now=perf_counter();proposal_deadline=now+max(0.,deadline-now)*proposal_fraction
        raw=raw_method.refine(problem,base,deadline=proposal_deadline,weights=weights,scales=scales,seed=seed,
            relay_enabled=relay_enabled,max_stations=max_stations,families_per_station=families_per_station,
            in_candidates=in_candidates,in_gap_limit=in_gap_limit,out_gap_limit=out_gap_limit,
            direct_gap_limit=direct_gap_limit,full_candidates_per_family=full_candidates_per_family,
            source_pair_pool_limit=source_pair_pool_limit)
        source=raw["metadata"]
        stats=dict(method="Six-Task-Pinned-Ordinary-Polish",seed=seed,relay_enabled=relay_enabled,
            raw_stage=source,input_snapshot=source["input_snapshot"],selected_families=source["selected_families"],
            selector_sha256=source["selector_sha256"],selector_completed=source["selector_completed"],
            selector_completed_at_perf_counter=source["selector_completed_at_perf_counter"],
            deadline_perf_counter=deadline,proposal_deadline_perf_counter=proposal_deadline,
            proposal_runtime_seconds=perf_counter()-now,raw_deadline_reached=source["deadline_reached"],
            deadline_reached=False,initial_key=list(initial_key),final_key=list(initial_key),
            full_candidate_snapshots=[],first_complete_candidate=None,best_constructed_candidate=None,
            accepted_moves=[],accepted_improvements=0,polish_records=[],polish_candidates=[],polish_calls=0,
            ranking_full_evaluations=0,wrapper_full_evaluations=1,polish_iterations=0,
            polish_skipped_margin=0,polish_skipped_duplicate=0,polish_not_selected=0,
            polish_skipped_timeout=0,polish_rejected_results=0,polish_not_run=0,
            final_cross_uav_count=0,configuration=dict(proposal_fraction=proposal_fraction,
                candidate_limit=candidate_limit,proposal_margin=proposal_margin,
                guard_reserve_seconds=guard_reserve_seconds,guard_reserve_fraction=guard_reserve_fraction,
                candidate_ranking="actual_complete_weighted_key_then_routes",selected_macro_identities_paired=False,
                max_stations=max_stations,families_per_station=families_per_station,source_pair_pool_limit=source_pair_pool_limit,
                in_candidates=in_candidates,in_gap_limit=in_gap_limit,out_gap_limit=out_gap_limit,
                direct_gap_limit=direct_gap_limit,full_candidates_per_family=full_candidates_per_family,
                pinned_task_count=6,ordinary_tasks_direct_only=True,pin_scope="six_task_sorting"),
            cold_polish_profile=dict(initial_temperature=config.initial_temperature,
                minimum_temperature=config.minimum_temperature,cooling_rate=config.cooling_rate,max_iterations=config.max_iterations))
        try:
            _check(deadline,stats)
            if raw.get("problem",problem) is not problem:raise AssertionError("raw sorting changed fixed Problem")
            raw_routes=tuple(tuple(r) for r in raw["routes"])
            raw_checked=evaluate_solution(problem,raw_routes,_force_full=True);stats["wrapper_full_evaluations"]+=1
            _check(deadline,stats)
            raw_key=constructor.weighted_key(raw_checked.score,weights,scales)
            if raw_checked.valid and raw_key<best_key:
                macro=next((s for s in source["full_candidate_snapshots"] if tuple(tuple(r) for r in s["routes"])==raw_routes),None)
                if macro is None:raise AssertionError("raw winner has no source-bound complete macro")
                constructor._verify(problem,base,raw_routes,macro["move"],relay_enabled)
                timestamp=perf_counter()
                if timestamp>=deadline:raise _Deadline
                best,best_checked,best_key=raw_routes,raw_checked,raw_key
                stats["accepted_improvements"]+=1
                stats["accepted_moves"].append(dict(kind="raw_macro",full_snapshot=macro,
                    original_macro_move=macro["move"],macro_routes=macro["routes"],
                    full_verified_at_perf_counter=timestamp,accepted_at_perf_counter=timestamp))
            ranked=[];seen=set()
            for macro in source["full_candidate_snapshots"]:
                _check(deadline,stats)
                candidate=tuple(tuple(r) for r in macro["routes"])
                if candidate in seen:stats["polish_skipped_duplicate"]+=1;continue
                seen.add(candidate)
                full=evaluate_solution(problem,candidate,_force_full=True);stats["ranking_full_evaluations"]+=1
                _check(deadline,stats)
                if not full.valid:raise AssertionError("raw complete snapshot is invalid under fixed input")
                key=constructor.weighted_key(full.score,weights,scales)
                if key[0]>initial_key[0]+proposal_margin:stats["polish_skipped_margin"]+=1;continue
                ranked.append((key,candidate,macro))
            selected=sorted(ranked,key=lambda r:(r[0],r[1]))[:candidate_limit]
            stats["polish_not_selected"]=len(ranked)-len(selected)
            stats["polish_candidates"]=[dict(key=list(k),original_macro_move=macro["move"]) for k,_,macro in selected]
            for number,(key,candidate,macro) in enumerate(selected):
                _check(deadline,stats)
                leaf_started=perf_counter();leaf_end=min(deadline,leaf_started+(deadline-leaf_started)/(len(selected)-number))
                reserve=min(guard_reserve_seconds,(leaf_end-leaf_started)*guard_reserve_fraction)
                helper_deadline=leaf_end-reserve;move=macro["move"]
                pins=tuple(sorted(move["family"]["task_ids"]));cross=pins if relay_enabled else ()
                expected=helper._signature(problem,candidate,frozenset(pins))
                stats["polish_calls"]+=1
                result=helper.polish_pinned_macro(problem,candidate,deadline=helper_deadline,weights=weights,scales=scales,
                    seed=seed+number+1,pinned_task_ids=pins,required_cross_task_ids=cross,comparison_key=initial_key,
                    proposal_margin=proposal_margin,config=config,alns_module=alns_module)
                helper_metadata=result["metadata"]
                _decorate_helper(helper_metadata,move,pins)
                stats["polish_iterations"]+=result.get("iterations",0)
                record=dict(full_proposal=macro,macro_routes=macro["routes"],original_macro_move=move,
                    pinned_task_ids=list(pins),pin_scope="six_task_sorting",helper_metadata=helper_metadata,post_polish=None,
                    adopted=False,finished_within_leaf_deadline=False,rejection_reason=None,
                    leaf_started_at_perf_counter=leaf_started,leaf_deadline_perf_counter=leaf_end,
                    helper_deadline_perf_counter=helper_deadline,guard_reserve_seconds=reserve)
                stats["polish_records"].append(record)
                if result.get("problem",problem) is not problem:
                    record["rejection_reason"]="fixed_problem_changed";stats["polish_rejected_results"]+=1;continue
                proposed=tuple(tuple(r) for r in result["routes"])
                try:
                    stats["wrapper_full_evaluations"]+=1
                    full,shape=helper._validate(problem,proposed,frozenset(pins),expected,frozenset(cross))
                except (ValueError,KeyError,IndexError):
                    record["rejection_reason"]="invalid_complete_or_six_pin_shape";stats["polish_rejected_results"]+=1;continue
                verified_at=perf_counter();record["full_verified_at_perf_counter"]=verified_at
                if verified_at>=leaf_end:
                    record["rejection_reason"]="wrapper_full_check_after_leaf_deadline";stats["polish_skipped_timeout"]+=1
                    _check(deadline,stats);continue
                post=helper._snapshot(problem,proposed,full,weights,scales,phase="post_six_task_pinned_polish",shape=shape)
                _decorate(post,move,pins);record["post_polish"]=post
                if perf_counter()>=leaf_end:
                    record["rejection_reason"]="wrapper_snapshot_after_leaf_deadline";stats["polish_skipped_timeout"]+=1
                    _check(deadline,stats);continue
                record["finished_within_leaf_deadline"]=True
                stats["full_candidate_snapshots"].append(post)
                if stats["first_complete_candidate"] is None:stats["first_complete_candidate"]=post
                if stats["best_constructed_candidate"] is None or tuple(post["key"])<tuple(stats["best_constructed_candidate"]["key"]):
                    stats["best_constructed_candidate"]=post
                if not helper_metadata.get("returned_checkpoint_checked_within_deadline",False):
                    record["rejection_reason"]="helper_checkpoint_not_verified_before_deadline";stats["polish_skipped_timeout"]+=1;continue
                key=constructor.weighted_key(full.score,weights,scales)
                if not helper_metadata.get("adoptable",False) or key>=best_key:
                    record["rejection_reason"]="no_strict_improvement_over_guarded_incumbent";continue
                accepted_at=perf_counter()
                if accepted_at>=leaf_end:
                    record["rejection_reason"]="adoption_after_leaf_deadline";stats["polish_skipped_timeout"]+=1
                    _check(deadline,stats);continue
                best,best_checked,best_key=proposed,full,key;record["adopted"]=True
                stats["accepted_improvements"]+=1
                stats["accepted_moves"].append(dict(kind="six_pinned_polish",full_snapshot=post,
                    original_macro_move=move,macro_routes=macro["routes"],
                    full_verified_at_perf_counter=verified_at,accepted_at_perf_counter=accepted_at))
        except _Deadline:
            stats["deadline_reached"]=perf_counter()>=deadline
        index=build_plan_index(problem,best)
        stats["deadline_reached"] |= perf_counter()>=deadline
        stats.update(final_key=list(best_key),polish_not_run=max(0,len(stats["polish_candidates"])-stats["polish_calls"]),
            final_cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids))
    finally:
        selector_restored=constructor._select_families is selector_original
        alns_restored=all(getattr(alns_module,n) is function for n,function in alns_originals.items())
        objective_restored=(adapter is None or (adapter.active,adapter.coefficients)==adapter_state)
        objective_restored &= all(getattr(score_type,n) is function for n,function in score_originals.items())
        _active=False
        if not selector_restored or not alns_restored or not objective_restored:
            constructor._select_families=selector_original
            for n,function in alns_originals.items():setattr(alns_module,n,function)
            for n,function in score_originals.items():setattr(score_type,n,function)
            if adapter is not None:adapter.active,adapter.coefficients=adapter_state
            raise RuntimeError("six-task raw selector/ALNS/weighted objective were not restored")
    stats.update(raw_selector_patch_restored=selector_restored,pinned_alns_patches_restored=alns_restored,
        weighted_objective_state_restored=objective_restored,pinned_alns_patch_names=list(helper.PATCH_NAMES),
        runtime_seconds=perf_counter()-started)
    return dict(routes=best,evaluation=best_checked,iterations=raw["iterations"]+stats["polish_iterations"],metadata=stats)
