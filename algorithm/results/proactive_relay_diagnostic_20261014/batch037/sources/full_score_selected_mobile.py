"""Native full-score compensation selection outside the frozen mobile stack.

The new selector credits all score changes when C loses both compensation
requests, using their real joint removal rather than summing isolated removals.
It only selects bounded groups: physical insertion deltas, State.key, complete
global scores, mobile no-cache physics and adoption guards are unchanged.
DIRECT and RELAY share this selector and every finite search/cache bound.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter

try:
    import receiver_bundle_cached_mobile as cached
    import full_score_repool_selector as selection
except ImportError:
    from . import receiver_bundle_cached_mobile as cached
    from . import full_score_repool_selector as selection


def refine(problem,routes,*,deadline,weights,scales,seed=2026101509,relay_enabled=True,
           compensation_geometry_pool_limit=6,compensation_removal_pool_limit=6,
           receiver_bundle_memo_limit=8192,source_surface_limit=512,memo_limit=8192,
           max_groups=8,option_count=4,beam_width=8,full_candidates_per_group=8,
           raw_spec_limit=64,proposal_fraction=.55,point_limit=24,pattern_rounds=4,
           point_eval_limit=96,full_candidate_limit=32,receiver_limit=2,
           compensation_limit=3,position_limit=16,common_selector_records=None):
    """Run one common full-score selector with eight restored local hooks."""
    if (not isfinite(deadline) or type(relay_enabled) is not bool
            or any(type(v) is not int or v<1 for v in
                   (compensation_geometry_pool_limit,compensation_removal_pool_limit))):
        raise ValueError("finite deadline, boolean arm and positive compensation pool limits required")
    weights,scales=tuple(weights),tuple(scales)
    started=perf_counter()
    actual=cached.source.joint.frozen.repool
    patch_names=tuple(sorted(set(cached.source.joint.PATCH_NAMES)|set(cached.source.frontier.PATCH_NAMES)
                             |set(cached.bundles.PATCH_NAMES)|set(selection.PATCH_NAMES)))
    original={name:getattr(actual,name) for name in patch_names}
    selector_stats={}
    try:
        with selection.full_score_selector(actual,problem,weights=weights,scales=scales,
                deadline=deadline,statistics=selector_stats,geo_pool_limit=compensation_geometry_pool_limit,
                removal_pool_limit=compensation_removal_pool_limit):
            outcome=cached.refine(problem,routes,deadline=deadline,weights=weights,scales=scales,
                seed=seed,relay_enabled=relay_enabled,receiver_bundle_memo_limit=receiver_bundle_memo_limit,
                source_surface_limit=source_surface_limit,memo_limit=memo_limit,max_groups=max_groups,
                option_count=option_count,beam_width=beam_width,full_candidates_per_group=full_candidates_per_group,
                raw_spec_limit=raw_spec_limit,proposal_fraction=proposal_fraction,point_limit=point_limit,
                pattern_rounds=pattern_rounds,point_eval_limit=point_eval_limit,
                full_candidate_limit=full_candidate_limit,receiver_limit=receiver_limit,
                compensation_limit=compensation_limit,position_limit=position_limit,
                common_selector_records=common_selector_records)
    finally:
        restored=all(getattr(actual,name) is function for name,function in original.items())
        if not restored:
            for name,function in original.items():
                setattr(actual,name,function)
            raise RuntimeError("full-score selector/mobile contexts did not restore all frozen hooks")
    metadata=outcome["metadata"]
    metadata.update(method="Full-Score-Selected-Receiver-First-Mobile-Repooling" if relay_enabled else
        "Full-Score-Selected-Matched-Physical-DIRECT-Control",full_score_wrapped_method=metadata["method"],
        receiver_bundle_process_local_patch_names=metadata["process_local_patch_names"],
        full_score_selector_statistics=selector_stats,process_local_patch_names=list(patch_names),
        process_local_patches_restored=restored,
        full_score_selector_records_reused=common_selector_records is not None,
        full_score_selector_parameters=dict(compensation_geometry_pool_limit=compensation_geometry_pool_limit,
            compensation_removal_pool_limit=compensation_removal_pool_limit,
            receiver_bundle_memo_limit=receiver_bundle_memo_limit,source_surface_limit=source_surface_limit,
            memo_limit=memo_limit,max_groups=max_groups,option_count=option_count,beam_width=beam_width,
            full_candidates_per_group=full_candidates_per_group,raw_spec_limit=raw_spec_limit,
            proposal_fraction=proposal_fraction,point_limit=point_limit,pattern_rounds=pattern_rounds,
            point_eval_limit=point_eval_limit,full_candidate_limit=full_candidate_limit,
            receiver_limit=receiver_limit,compensation_limit=compensation_limit,position_limit=position_limit,
            selector_shared_by_both_arms=True,joint_C_removal_score_credit=True,
            physical_keys_and_deltas_unchanged=True,partial_incumbent_pruning=False),
        runtime_seconds=perf_counter()-started)
    return outcome
