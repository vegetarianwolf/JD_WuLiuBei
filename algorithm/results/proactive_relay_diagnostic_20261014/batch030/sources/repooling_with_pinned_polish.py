"""Fixed-network repooling followed by bounded four-task-preserving polish.

The same wall-clock budget covers proposal generation, candidate validation,
and ordinary DIRECT ALNS polish. Each arm keeps its own two best complete
macros within a predeclared weighted margin; those identities need not match
across arms. The shared selector, limits and ranking rule are recorded, rather
than treating this as a paired comparison of identical chosen macros.

Raw snapshots retain the original all-other-events-fixed evidence. Polished
snapshots are stored separately with their raw macro routes: the remaining
tasks may change while four whole plans, their event order and final quotas
remain pinned. A worse complete macro returned by the helper is never an
adoption merely because it is valid or because its polish search completed.
"""
from __future__ import annotations

from copy import deepcopy
from math import isclose,isfinite
from time import perf_counter

from uav_dispatch.relay import build_plan_index
from uav_dispatch.validation import evaluate_solution

try:
    import relay_repooling_exchange as repool
except ImportError:
    from . import relay_repooling_exchange as repool
try:
    import relay_preserving_polish as pinned
except ImportError:
    from . import relay_preserving_polish as pinned


class _Deadline(Exception):
    pass


def _check(deadline,stats):
    if perf_counter()>=deadline:
        stats["deadline_reached"]=True
        raise _Deadline


def _macro_tasks(move):
    return tuple(move[k] for k in ("i","j","k1","k2"))


def _post_snapshot(problem,routes,checked,weights,scales,macro,relay_enabled):
    move=deepcopy(macro["move"])
    cross=(move["i"],move["j"]) if relay_enabled else ()
    shape=pinned._shape(problem,routes,frozenset(cross))
    if relay_enabled:
        shared=shape["shared_double_loaded_km"]
    else:
        shared=repool._shared_distance(problem,routes[move["c"]],move["i"],move["j"],move["c"])
    move["shared_double_loaded_km"]=shared
    return dict(**repool._snapshot(problem,routes,checked,weights,scales,
        phase="post_pinned_polish",move=move),pinned_shape=shape)


def _complete_pin_check(problem,macro,routes,checked,relay_enabled):
    if not checked.valid:
        return "invalid_complete_solution"
    tasks=frozenset(_macro_tasks(macro["move"]))
    try:
        if pinned._signature(problem,routes,tasks)!=pinned._signature(problem,macro["routes"],tasks):
            return "macro_pin_signature_changed"
        pinned._shape(problem,routes,frozenset((macro["move"]["i"],macro["move"]["j"])) if relay_enabled else frozenset())
    except (ValueError,KeyError):
        return "macro_relay_shape_changed"
    return None


def refine(problem,routes,*,deadline,weights,scales,seed=2026101504,relay_enabled=True,
           proposal_fraction=.35,candidate_limit=2,proposal_margin=.015,
           guard_reserve_seconds=.03,guard_reserve_fraction=.05,alns_module=None,polish_config=None,
           max_groups=12,receiver_limit=2,compensation_limit=3,position_limit=16,
           option_count=2,beam_width=6,full_candidates_per_group=4,common_selector_records=None):
    """Polish at most two complete raw macros without changing their identities.

    Defaults are development parameters frozen before a diagnostic run.
    All selector work remains inside the shared budget unless the caller
    explicitly supplies reusable selector records. The runtime ALNS module
    defaults to the current worker's ``uav_dispatch.alns``; the helper verifies
    that it is the actual frozen weighted module receiving its local patches.
    The fixed Problem is never replaced, and no ``problem`` key is returned.
    """
    started=perf_counter()
    weights,scales=tuple(weights),tuple(scales)
    if (not isfinite(deadline) or len(weights)!=3 or len(scales)!=3
            or any(not isfinite(x) or x<=0 for x in (*weights,*scales)) or not isclose(sum(weights),1.)
            or not isinstance(relay_enabled,bool) or not isfinite(proposal_fraction) or not 0<proposal_fraction<1
            or type(candidate_limit) is not int or not 1<=candidate_limit<=2
            or not isfinite(proposal_margin) or proposal_margin<0
            or not isfinite(guard_reserve_seconds) or guard_reserve_seconds<0
            or not isfinite(guard_reserve_fraction) or not 0<=guard_reserve_fraction<1):
        raise ValueError("finite weighted objective/deadline and bounded proposal/polish budget required")
    base=tuple(tuple(r) for r in routes)
    if len(base)>problem.drone_count:
        raise ValueError("too many routes")
    base+=((),)*(problem.drone_count-len(base))
    checked=evaluate_solution(problem,base,_force_full=True)
    if not checked.valid:
        raise ValueError(f"invalid initial solution: {checked.violations}")
    if build_plan_index(problem,base).relay_task_ids:
        raise ValueError("this experiment requires a pure DIRECT initial solution")
    initial_key=best_key=repool.weighted_key(checked.score,weights,scales)
    best,best_checked=base,checked
    now=perf_counter()
    proposal_deadline=now+max(0.,deadline-now)*proposal_fraction
    # The frozen constructor supplies initialized empty selector/hash fields
    # even when this absolute proposal deadline is already expired.
    raw=repool.refine(problem,base,deadline=proposal_deadline,weights=weights,scales=scales,seed=seed,
        relay_enabled=relay_enabled,max_groups=max_groups,receiver_limit=receiver_limit,
        compensation_limit=compensation_limit,position_limit=position_limit,option_count=option_count,
        beam_width=beam_width,full_candidates_per_group=full_candidates_per_group,
        common_selector_records=common_selector_records)
    stats=deepcopy(raw["metadata"])
    stats.update(method="Repooling-With-Four-Task-Pinned-Ordinary-Polish",raw_method=raw["metadata"]["method"],
        raw_accepted_moves=deepcopy(raw["metadata"].get("accepted_moves",[])),accepted_moves=[],accepted_improvements=0,
        raw_deadline_reached=raw["metadata"].get("deadline_reached",False),deadline_reached=False,
        repooling_configuration=deepcopy(raw["metadata"]["configuration"]),
        configuration=dict(proposal_fraction=proposal_fraction,candidate_limit=candidate_limit,
            proposal_margin=proposal_margin,guard_reserve_seconds=guard_reserve_seconds,
            guard_reserve_fraction=guard_reserve_fraction,
            candidate_ranking="actual_complete_weighted_key_then_routes",
            repooling=deepcopy(raw["metadata"]["configuration"])),
        deadline_perf_counter=deadline,proposal_deadline_perf_counter=proposal_deadline,
        proposal_runtime_seconds=perf_counter()-now,initial_key=list(initial_key),final_key=list(initial_key),
        ranking_full_evaluations=0,wrapper_full_evaluations=1,polish_calls=0,
        polish_candidates=[],polish_records=[],polish_skipped_margin=0,polish_skipped_duplicate=0,
        polish_not_selected=0,polish_not_run=0,polish_completed_records=0,
        polish_skipped_timeout=0,polish_rejected_results=0,polish_failure_reasons={},
        polish_iterations=0,final_cross_uav_count=0)
    try:
        _check(deadline,stats)
        # Recheck the raw winner instead of adopting its returned evaluation.
        raw_routes=tuple(tuple(r) for r in raw["routes"])
        raw_checked=evaluate_solution(problem,raw_routes,_force_full=True)
        stats["wrapper_full_evaluations"]+=1
        _check(deadline,stats)
        raw_key=repool.weighted_key(raw_checked.score,weights,scales)
        if raw_checked.valid and raw_key<best_key:
            snapshot=next((s for s in stats["full_candidate_snapshots"] if tuple(tuple(r) for r in s["routes"])==raw_routes),None)
            if snapshot is None:
                raise AssertionError("raw constructor winner lacks complete macro evidence")
            repool._verify_structure(problem,base,raw_routes,
                repool._Group(*(snapshot["move"][k] for k in ("i","j","a","b","c","k1","k2")),()),
                snapshot["move"]["station_id"],relay_enabled)
            _check(deadline,stats)
            adopted_at=perf_counter()
            best,best_checked,best_key=raw_routes,raw_checked,raw_key
            stats["accepted_moves"].append(dict(**deepcopy(snapshot["move"]),macro_routes=deepcopy(snapshot["routes"]),
                full_snapshot=deepcopy(snapshot),kind="raw_macro",full_verified_at_perf_counter=adopted_at,
                accepted_at_perf_counter=adopted_at))
            stats["accepted_improvements"]+=1
        ranked=[]
        seen=set()
        # Source snapshots already have complete full checks; re-evaluation
        # explicitly binds the ranking to this same fixed model and objective.
        for snapshot in stats["full_candidate_snapshots"]:
            _check(deadline,stats)
            candidate=tuple(tuple(r) for r in snapshot["routes"])
            if candidate in seen:
                stats["polish_skipped_duplicate"]+=1
                continue
            seen.add(candidate)
            full=evaluate_solution(problem,candidate,_force_full=True)
            stats["ranking_full_evaluations"]+=1
            _check(deadline,stats)
            if not full.valid:
                stats["polish_rejected_results"]+=1
                continue
            key=repool.weighted_key(full.score,weights,scales)
            if key[0]>initial_key[0]+proposal_margin:
                stats["polish_skipped_margin"]+=1
                continue
            ranked.append((key,candidate,deepcopy(snapshot)))
        selected=sorted(ranked,key=lambda item:(item[0],item[1]))[:candidate_limit]
        stats["polish_not_selected"]=len(ranked)-len(selected)
        stats["polish_candidates"]=[dict(key=list(k),move=deepcopy(s["move"])) for k,_,s in selected]
        if alns_module is None and selected:
            from uav_dispatch import alns as alns_module
        for number,(key,candidate,macro) in enumerate(selected):
            _check(deadline,stats)
            leaf_started=perf_counter()
            leaf_end=min(deadline,leaf_started+(deadline-leaf_started)/(len(selected)-number))
            reserve=min(guard_reserve_seconds,(leaf_end-leaf_started)*guard_reserve_fraction)
            helper_deadline=leaf_end-reserve
            move=macro["move"]
            stats["polish_calls"]+=1
            result=pinned.polish_pinned_macro(problem,candidate,deadline=helper_deadline,weights=weights,scales=scales,
                seed=seed+number+1,pinned_task_ids=_macro_tasks(move),
                required_cross_task_ids=(move["i"],move["j"]) if relay_enabled else (),
                comparison_key=initial_key,proposal_margin=proposal_margin,alns_module=alns_module,config=polish_config)
            # Keep the helper's frozen evidence intact. A deep copy of its
            # repeated full topology snapshots would consume guard time.
            helper_metadata=result["metadata"]
            stats["polish_iterations"]+=result.get("iterations",0)
            record=dict(full_proposal=macro,macro_routes=deepcopy(macro["routes"]),post_polish=None,
                move=deepcopy(macro["move"]),helper_metadata=helper_metadata,
                adopted=False,finished_within_leaf_deadline=False,
                leaf_started_at_perf_counter=leaf_started,leaf_deadline_perf_counter=leaf_end,
                helper_deadline_perf_counter=helper_deadline,guard_reserve_seconds=reserve,
                rejection_reason=None)
            stats["polish_records"].append(record)
            if result.get("problem",problem) is not problem:
                record["rejection_reason"]="fixed_problem_changed"
                stats["polish_rejected_results"]+=1
                continue
            proposed=tuple(tuple(r) for r in result["routes"])
            full=evaluate_solution(problem,proposed,_force_full=True)
            stats["wrapper_full_evaluations"]+=1
            verified_at=perf_counter()
            record["full_verified_at_perf_counter"]=verified_at
            if verified_at>=leaf_end:
                record["rejection_reason"]="wrapper_full_check_after_leaf_deadline"
                stats["polish_skipped_timeout"]+=1
                if verified_at>=deadline:
                    _check(deadline,stats)
                continue
            reason=_complete_pin_check(problem,macro,proposed,full,relay_enabled)
            if reason is not None:
                record["rejection_reason"]=reason
                stats["polish_rejected_results"]+=1
                stats["polish_failure_reasons"][reason]=stats["polish_failure_reasons"].get(reason,0)+1
                continue
            post=_post_snapshot(problem,proposed,full,weights,scales,macro,relay_enabled)
            record["post_polish"]=post
            if perf_counter()>=leaf_end:
                record["rejection_reason"]="wrapper_pin_check_after_leaf_deadline"
                stats["polish_skipped_timeout"]+=1
                _check(deadline,stats)
                continue
            record["finished_within_leaf_deadline"]=True
            key=repool.weighted_key(full.score,weights,scales)
            if not helper_metadata.get("returned_checkpoint_checked_within_deadline",False):
                record["rejection_reason"]="helper_checkpoint_not_verified_before_deadline"
                stats["polish_skipped_timeout"]+=1
                continue
            if not helper_metadata.get("adoptable",False) or key>=best_key:
                record["rejection_reason"]="no_strict_improvement_over_guarded_incumbent"
                continue
            adopted_at=perf_counter()
            if adopted_at>=leaf_end:
                record["rejection_reason"]="adoption_after_leaf_deadline"
                stats["polish_skipped_timeout"]+=1
                _check(deadline,stats)
                continue
            best,best_checked,best_key=proposed,full,key
            record["adopted"]=True
            stats["accepted_improvements"]+=1
            stats["accepted_moves"].append(dict(**deepcopy(post["move"]),macro_routes=deepcopy(macro["routes"]),
                full_snapshot=post,kind="pinned_polish",full_verified_at_perf_counter=verified_at,
                accepted_at_perf_counter=adopted_at))
    except _Deadline:
        stats["polish_skipped_timeout"]+=max(0,len(stats["polish_candidates"])-stats["polish_calls"])
    index=build_plan_index(problem,best)
    stats.update(final_key=list(best_key),
        polish_not_run=max(0,len(stats["polish_candidates"])-stats["polish_calls"]),
        polish_completed_records=sum(r["finished_within_leaf_deadline"] for r in stats["polish_records"]),
        final_cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        runtime_seconds=perf_counter()-started)
    return dict(routes=best,evaluation=best_checked,
                iterations=raw["iterations"]+stats["polish_iterations"],metadata=stats)
