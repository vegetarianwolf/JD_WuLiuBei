"""Release raw DIRECT compensation while preserving the true relay pair.

Two process-local facade aliases select the new two-task helper. The frozen
cold/full-credit constructor, weighted loop, finite budget and adoption guard
remain unchanged. Original macro descriptors are historical evidence; every
post-polish snapshot separately records its actual final owners.
"""
from __future__ import annotations

from copy import deepcopy
from time import perf_counter

from uav_dispatch.relay import build_plan_index

try:
    import cold_weighted_pinned_repooling as frozen
    import relay_pair_preserving_polish as pair_helper
except ImportError:
    from . import cold_weighted_pinned_repooling as frozen
    from . import relay_pair_preserving_polish as pair_helper


_active=False
FACADE_PATCH_NAMES=("pinned","_macro_tasks")


def _pair_tasks(move):
    return (move["i"],move["j"])


def _post_evidence(problem,snapshot,original_move):
    if snapshot is None:
        return
    index=build_plan_index(problem,snapshot["routes"])
    snapshot.update(original_macro_move=deepcopy(original_move),
        actual_final_owners={str(t):a for t,a in sorted(index.delivery_route_by_task.items())},
        pinned_task_ids=list(_pair_tasks(original_move)),
        released_compensation_task_ids=[original_move["k1"],original_move["k2"]],
        pin_scope="relay_pair_only")
    if snapshot.get("move") is not None:
        snapshot["move"].pop("final_aircraft",None)
        snapshot["move"]["semantic"]="post_pair_polish"
        snapshot["move"]["seed_identity_fields"]=["a","b","k1","k2","source_aircraft"]


def refine(problem,routes,*,deadline,weights,scales,seed=2026101511,relay_enabled=True,
           proposal_fraction=.35,candidate_limit=2,proposal_margin=.015,
           guard_reserve_seconds=.03,guard_reserve_fraction=.05,alns_module=None,polish_config=None,
           max_groups=8,receiver_limit=2,compensation_limit=3,position_limit=16,
           option_count=4,beam_width=8,full_candidates_per_group=8,common_selector_records=None,
           compensation_geometry_pool_limit=6,compensation_removal_pool_limit=6,
           receiver_bundle_memo_limit=8192,source_surface_limit=512,memo_limit=8192,
           polish_initial_temperature=.0025,polish_minimum_temperature=.00001,
           polish_cooling_rate=.97,polish_max_iterations=1000000):
    """Pin i/j, allowing every other task to undergo quota-preserving repair."""
    global _active
    if _active:
        raise RuntimeError("nested pair-pinned repooling facades are unsupported")
    facade=frozen.frozen.frozen
    original={name:getattr(facade,name) for name in FACADE_PATCH_NAMES}
    if original["pinned"]._active or pair_helper._active:
        raise RuntimeError("nested pair/four-task pinned helper is unsupported")
    started=perf_counter()
    _active=True
    try:
        facade.pinned=pair_helper
        facade._macro_tasks=_pair_tasks
        outcome=frozen.refine(problem,routes,deadline=deadline,weights=weights,scales=scales,
            seed=seed,relay_enabled=relay_enabled,proposal_fraction=proposal_fraction,candidate_limit=candidate_limit,
            proposal_margin=proposal_margin,guard_reserve_seconds=guard_reserve_seconds,
            guard_reserve_fraction=guard_reserve_fraction,alns_module=alns_module,polish_config=polish_config,
            max_groups=max_groups,receiver_limit=receiver_limit,compensation_limit=compensation_limit,
            position_limit=position_limit,option_count=option_count,beam_width=beam_width,
            full_candidates_per_group=full_candidates_per_group,common_selector_records=common_selector_records,
            compensation_geometry_pool_limit=compensation_geometry_pool_limit,
            compensation_removal_pool_limit=compensation_removal_pool_limit,
            receiver_bundle_memo_limit=receiver_bundle_memo_limit,source_surface_limit=source_surface_limit,
            memo_limit=memo_limit,polish_initial_temperature=polish_initial_temperature,
            polish_minimum_temperature=polish_minimum_temperature,polish_cooling_rate=polish_cooling_rate,
            polish_max_iterations=polish_max_iterations)
    finally:
        # These aliases are intentionally patched for the whole frozen call.
        # Restore the captured originals after all inner raw/ALNS contexts exit.
        aliases_intact=facade.pinned is pair_helper and facade._macro_tasks is _pair_tasks
        for name in reversed(FACADE_PATCH_NAMES):setattr(facade,name,original[name])
        restored=all(getattr(facade,name) is function for name,function in original.items())
        _active=False
        if not restored or not aliases_intact:
            raise RuntimeError("pair-pinned facade aliases were not restored")
    metadata=outcome["metadata"]
    for record in metadata["polish_records"]:
        move=deepcopy(record["move"])
        record.update(original_macro_move=move,pinned_task_ids=list(_pair_tasks(move)),
            released_compensation_task_ids=[move["k1"],move["k2"]],pin_scope="relay_pair_only")
        record["move"]["semantic"]="original_macro_before_pair_polish"
        _post_evidence(problem,record["post_polish"],move)
        helper=record["helper_metadata"]
        snapshots=[helper.get(k) for k in ("input_snapshot","first_complete_candidate","best_constructed_candidate","post_pin_snapshot")]
        snapshots+=helper.get("full_candidate_snapshots",[])
        for snapshot in snapshots:_post_evidence(problem,snapshot,move)
    for accepted in metadata["accepted_moves"]:
        post=accepted["full_snapshot"]
        original_move=(deepcopy(post["move"]) if accepted["kind"]=="raw_macro"
                       else post.get("original_macro_move"))
        if original_move is None:
            raise AssertionError("pair-pinned adoption lacks its original macro descriptor")
        accepted.update(original_macro_move=deepcopy(original_move),
            actual_final_owners={str(t):a for t,a in sorted(build_plan_index(problem,post["routes"]).delivery_route_by_task.items())},
            pinned_task_ids=list(_pair_tasks(original_move)),
            released_compensation_task_ids=[original_move["k1"],original_move["k2"]],
            semantic="original_raw_macro" if accepted["kind"]=="raw_macro" else "post_pair_polish",
            pin_scope="relay_pair_only")
        if accepted["kind"]=="pinned_polish":accepted.pop("final_aircraft",None)
    metadata.update(method="Two-Task-Pair-Pinned-Repooling" if relay_enabled else
        "Two-Task-Pair-Pinned-DIRECT-Control",pair_pinned_wrapped_method=metadata["method"],
        facade_patch_names=list(FACADE_PATCH_NAMES),facade_patches_restored=restored,
        facade_aliases_intact_during_call=aliases_intact,
        pair_pinned_parameters=dict(pinned_task_count=2,compensation_tasks_released=True,
            preserve_exact_final_quotas=True,other_tasks_direct_only=True,
            same_pin_count_and_budget_in_both_arms=True,post_move_semantic="post_pair_polish",
            raw_macro_source_proof_unchanged=True,fixed_network=True,new_geometry=False,
            strict_original_incumbent_guard=True),runtime_seconds=perf_counter()-started)
    return outcome
