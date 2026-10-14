"""Fixed-station full-credit repooling followed by four-task-pinned polish.

Eight process-local construction hooks retain early source layouts and exact
native removal credit. The unchanged pinned facade then polishes each arm's
own best complete macros. Original four-station geometry, complete physical
keys and the input improvement guard remain unchanged; no mobile point search
or new Problem is used. Other tasks may change only in the traced polish stage.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter

try:
    import repooling_with_pinned_polish as frozen
    import full_score_repool_selector as selection
    import receiver_bundle_cache as bundles
    import joint_mobile_layouts as layouts
    import source_frontier_cache as frontier
except ImportError:
    from . import repooling_with_pinned_polish as frozen
    from . import full_score_repool_selector as selection
    from . import receiver_bundle_cache as bundles
    from . import joint_mobile_layouts as layouts
    from . import source_frontier_cache as frontier


def refine(problem,routes,*,deadline,weights,scales,seed=2026101510,relay_enabled=True,
           proposal_fraction=.35,candidate_limit=2,proposal_margin=.015,
           guard_reserve_seconds=.03,guard_reserve_fraction=.05,alns_module=None,polish_config=None,
           max_groups=8,receiver_limit=2,compensation_limit=3,position_limit=16,
           option_count=4,beam_width=8,full_candidates_per_group=8,common_selector_records=None,
           compensation_geometry_pool_limit=6,compensation_removal_pool_limit=6,
           receiver_bundle_memo_limit=8192,source_surface_limit=512,memo_limit=8192):
    """Compose frozen construction and real weighted polish in one budget.

    Both arms share selector/bounds and the same full-key candidate ranking
    policy. Their two chosen macro identities may differ. The supplied ALNS
    module is the worker's actual weighted runtime; all five polish hooks and
    eight construction hooks must be restored before returning or raising.
    """
    if (not isfinite(deadline) or type(relay_enabled) is not bool
            or any(type(v) is not int or v<1 for v in
                (receiver_bundle_memo_limit,source_surface_limit,memo_limit))
            or source_surface_limit<4
            or any(type(v) is not int or not 1<=v<=32 for v in
                (compensation_geometry_pool_limit,compensation_removal_pool_limit))
            or any(type(v) is not int or not 1<=v<=8 for v in (beam_width,full_candidates_per_group))):
        raise ValueError("finite deadline, boolean arm and bounded selector/cache/layout limits required")
    weights,scales=tuple(weights),tuple(scales)
    started=perf_counter()
    actual=frozen.repool
    patch_names=tuple(sorted(set(selection.PATCH_NAMES)|set(bundles.PATCH_NAMES)
                             |set(layouts.PATCH_NAMES)|set(frontier.PATCH_NAMES)))
    originals={name:getattr(actual,name) for name in patch_names}
    if alns_module is None:
        from uav_dispatch import alns as alns_module
    alns_originals={name:getattr(alns_module,name) for name in frozen.pinned.PATCH_NAMES}
    adapter=getattr(alns_module,"_weighted_adapter",None)
    adapter_state=(adapter.active,adapter.coefficients) if adapter is not None else None
    score_type=getattr(alns_module,"Score",frozen.pinned.Score)
    score_originals={name:getattr(score_type,name) for name in ("__lt__","__le__","__gt__","__ge__")}
    selector_stats,bundle_stats,layout_stats,frontier_stats={},{},{},{}
    try:
        with selection.full_score_selector(actual,problem,weights=weights,scales=scales,deadline=deadline,
                statistics=selector_stats,geo_pool_limit=compensation_geometry_pool_limit,
                removal_pool_limit=compensation_removal_pool_limit):
            with bundles.receiver_bundle_cache(actual,problem,weights=weights,scales=scales,deadline=deadline,
                    statistics=bundle_stats,memo_limit=receiver_bundle_memo_limit):
                with layouts.joint_layouts(actual,problem,weights=weights,scales=scales,deadline=deadline,
                        statistics=layout_stats):
                    with frontier.source_frontier_cache(actual,problem,weights=weights,scales=scales,
                            deadline=deadline,statistics=frontier_stats,raw_position_limit=source_surface_limit,
                            memo_limit=memo_limit):
                        outcome=frozen.refine(problem,routes,deadline=deadline,weights=weights,scales=scales,
                            seed=seed,relay_enabled=relay_enabled,proposal_fraction=proposal_fraction,
                            candidate_limit=candidate_limit,proposal_margin=proposal_margin,
                            guard_reserve_seconds=guard_reserve_seconds,guard_reserve_fraction=guard_reserve_fraction,
                            alns_module=alns_module,polish_config=polish_config,max_groups=max_groups,
                            receiver_limit=receiver_limit,compensation_limit=compensation_limit,
                            position_limit=position_limit,option_count=option_count,beam_width=beam_width,
                            full_candidates_per_group=full_candidates_per_group,
                            common_selector_records=common_selector_records)
    finally:
        raw_restored=all(getattr(actual,name) is function for name,function in originals.items())
        alns_restored=all(getattr(alns_module,name) is function for name,function in alns_originals.items())
        weighted_restored=(adapter is None or (adapter.active,adapter.coefficients)==adapter_state)
        weighted_restored &= all(getattr(score_type,name) is function for name,function in score_originals.items())
        if not raw_restored or not alns_restored or not weighted_restored:
            for name,function in originals.items():setattr(actual,name,function)
            for name,function in alns_originals.items():setattr(alns_module,name,function)
            for name,function in score_originals.items():setattr(score_type,name,function)
            if adapter is not None:adapter.active,adapter.coefficients=adapter_state
            raise RuntimeError("full-credit construction/pinned-polish hooks were not all restored")
    if "problem" in outcome:
        raise AssertionError("fixed-network pinned facade returned a dynamic Problem")
    metadata=outcome["metadata"]
    metadata.update(method="Full-Credit-Fixed-Network-Pinned-Repooling" if relay_enabled else
        "Full-Credit-Fixed-Network-Pinned-DIRECT-Control",full_credit_wrapped_method=metadata["method"],
        full_score_selector_statistics=selector_stats,receiver_bundle_statistics=bundle_stats,
        joint_layout_statistics=layout_stats,source_frontier_statistics=frontier_stats,
        full_score_selector_records_reused=common_selector_records is not None,
        process_local_patch_names=list(patch_names),process_local_patches_restored=raw_restored,
        pinned_alns_patch_names=list(frozen.pinned.PATCH_NAMES),pinned_alns_patches_restored=alns_restored,
        weighted_objective_state_restored=weighted_restored,
        full_credit_parameters=dict(proposal_fraction=proposal_fraction,candidate_limit=candidate_limit,
            proposal_margin=proposal_margin,guard_reserve_seconds=guard_reserve_seconds,
            guard_reserve_fraction=guard_reserve_fraction,max_groups=max_groups,receiver_limit=receiver_limit,
            compensation_limit=compensation_limit,position_limit=position_limit,option_count=option_count,
            beam_width=beam_width,full_candidates_per_group=full_candidates_per_group,
            compensation_geometry_pool_limit=compensation_geometry_pool_limit,
            compensation_removal_pool_limit=compensation_removal_pool_limit,
            receiver_bundle_memo_limit=receiver_bundle_memo_limit,source_surface_limit=source_surface_limit,
            memo_limit=memo_limit,context_order="selector_bundle_joint_frontier_then_pinned_alns",
            fixed_network=True,coordinate_search=False,selector_shared_by_both_arms=True,
            joint_C_removal_score_credit=True,physical_keys_and_deltas_unchanged=True,
            partial_incumbent_pruning=False,selected_macro_identities_paired=False),
        runtime_seconds=perf_counter()-started)
    return outcome
