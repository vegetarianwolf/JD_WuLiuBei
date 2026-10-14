"""Cache complete recipient enumeration outside the frozen frontier driver.

Seven process-local hooks compose in a fixed order: complete receiver bundle
memo outside joint layout ranking and source/partial/receiver-rank memo inside.
The new cache reuses an identical pure-DIRECT recipient layout before its
expensive enumeration starts. It changes no candidate set, sequence, physical
delta, State.key, source selection, geometry model or adoption guard. DIRECT
uses the same memo limits and its original physical candidate generator.
Equivalence applies to each identical logical receiver request; saved compute
time may let the fixed wall-clock run reach more groups.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter

try:
    import source_frontier_mobile_repooling as source
    import receiver_bundle_cache as bundles
except ImportError:
    from . import source_frontier_mobile_repooling as source
    from . import receiver_bundle_cache as bundles


def refine(problem,routes,*,deadline,weights,scales,seed=2026101508,relay_enabled=True,
           receiver_bundle_memo_limit=8192,source_surface_limit=512,memo_limit=8192,
           max_groups=8,option_count=4,beam_width=8,full_candidates_per_group=8,
           raw_spec_limit=64,proposal_fraction=.55,point_limit=24,pattern_rounds=4,
           point_eval_limit=96,full_candidate_limit=32,receiver_limit=2,
           compensation_limit=3,position_limit=16,common_selector_records=None):
    """Reuse exact receiver outputs within a single finite wall-clock budget."""
    if (not isfinite(deadline) or type(relay_enabled) is not bool
            or type(receiver_bundle_memo_limit) is not int or receiver_bundle_memo_limit<1):
        raise ValueError("finite absolute deadline, boolean arm and positive receiver memo limit required")
    weights,scales=tuple(weights),tuple(scales)
    started=perf_counter()
    actual=source.joint.frozen.repool
    patch_names=tuple(sorted(set(source.joint.PATCH_NAMES)|set(source.frontier.PATCH_NAMES)|set(bundles.PATCH_NAMES)))
    original={name:getattr(actual,name) for name in patch_names}
    bundle_stats={}
    try:
        with bundles.receiver_bundle_cache(actual,problem,weights=weights,scales=scales,
                deadline=deadline,statistics=bundle_stats,memo_limit=receiver_bundle_memo_limit):
            outcome=source.refine(problem,routes,deadline=deadline,weights=weights,scales=scales,
                seed=seed,relay_enabled=relay_enabled,source_surface_limit=source_surface_limit,memo_limit=memo_limit,
                max_groups=max_groups,option_count=option_count,beam_width=beam_width,
                full_candidates_per_group=full_candidates_per_group,raw_spec_limit=raw_spec_limit,
                proposal_fraction=proposal_fraction,point_limit=point_limit,pattern_rounds=pattern_rounds,
                point_eval_limit=point_eval_limit,full_candidate_limit=full_candidate_limit,
                receiver_limit=receiver_limit,compensation_limit=compensation_limit,
                position_limit=position_limit,common_selector_records=common_selector_records)
    finally:
        restored=all(getattr(actual,name) is function for name,function in original.items())
        if not restored:
            for name,function in original.items():
                setattr(actual,name,function)
            raise RuntimeError("receiver/source/joint contexts did not restore all frozen hooks")
    metadata=outcome["metadata"]
    metadata.update(method="Receiver-Bundle-Cached-Source-Frontier-Mobile-Repooling" if relay_enabled else
        "Receiver-Bundle-Cached-Matched-Physical-DIRECT-Control",receiver_bundle_wrapped_method=metadata["method"],
        source_frontier_process_local_patch_names=metadata["process_local_patch_names"],
        receiver_bundle_statistics=bundle_stats,process_local_patch_names=list(patch_names),
        process_local_patches_restored=restored,
        receiver_bundle_parameters=dict(receiver_bundle_memo_limit=receiver_bundle_memo_limit,
            source_surface_limit=source_surface_limit,memo_limit=memo_limit,max_groups=max_groups,
            option_count=option_count,beam_width=beam_width,full_candidates_per_group=full_candidates_per_group,
            raw_spec_limit=raw_spec_limit,proposal_fraction=proposal_fraction,point_limit=point_limit,
            pattern_rounds=pattern_rounds,point_eval_limit=point_eval_limit,full_candidate_limit=full_candidate_limit,
            receiver_limit=receiver_limit,compensation_limit=compensation_limit,position_limit=position_limit,
            context_order="receiver_bundle_outer_joint_middle_source_frontier_inner",
            cache_enabled_in_both_arms=True,partial_incumbent_pruning=False,
            candidate_sets_and_order_unchanged=True,physical_keys_and_deltas_unchanged=True,
            equivalence_scope="identical_logical_receiver_request"),
        runtime_seconds=perf_counter()-started)
    return outcome
