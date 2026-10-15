"""Early source handoffs plus bounded exact memoization in joint layouts.

Joint mobile layout ranking is the outer context. The inner context augments
only the two selected source IN options and reuses identical partial/receiver
computations. Physical insertion deltas and State keys remain unchanged;
computational memoization does not create a physical parcel cache or prune a
partial score against the full incumbent. Both arms enable the same memo,
while DIRECT source options delegate to their original physical generator.
All final one-site/zero-cache/arrival-order/full-score guards stay in the
unchanged receiver-first mobile driver.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter

try:
    import joint_mobile_repooling as joint
    import source_frontier_cache as frontier
except ImportError:
    from . import joint_mobile_repooling as joint
    from . import source_frontier_cache as frontier


def refine(problem,routes,*,deadline,weights,scales,seed=2026101507,relay_enabled=True,
           source_surface_limit=512,memo_limit=8192,max_groups=8,option_count=4,
           beam_width=8,full_candidates_per_group=8,raw_spec_limit=64,proposal_fraction=.55,
           point_limit=24,pattern_rounds=4,point_eval_limit=96,full_candidate_limit=32,
           receiver_limit=2,compensation_limit=3,position_limit=16,common_selector_records=None):
    """Run five process-local hooks with explicit finite source/memo bounds."""
    if (not isfinite(deadline) or type(relay_enabled) is not bool
            or any(type(v) is not int or v<1 for v in (source_surface_limit,memo_limit))
            or source_surface_limit<4
            or any(type(v) is not int or not 1<=v<=8 for v in (beam_width,full_candidates_per_group))):
        raise ValueError("finite deadline, boolean arm and positive bounded source/memo/beam options required")
    weights,scales=tuple(weights),tuple(scales)
    started=perf_counter()
    actual_repool=joint.frozen.repool
    patch_names=tuple(sorted(set(joint.PATCH_NAMES)|set(frontier.PATCH_NAMES)))
    original={name:getattr(actual_repool,name) for name in patch_names}
    layout_stats,frontier_stats={},{}
    try:
        with joint.layouts.joint_layouts(actual_repool,problem,weights=weights,scales=scales,
                deadline=deadline,statistics=layout_stats):
            # Capture the current joint hooks, rather than bypassing them with
            # the raw originals. Exit inner before restoring the outer hooks.
            with frontier.source_frontier_cache(actual_repool,problem,weights=weights,scales=scales,
                    deadline=deadline,statistics=frontier_stats,raw_position_limit=source_surface_limit,
                    memo_limit=memo_limit):
                outcome=joint.frozen.refine(problem,routes,deadline=deadline,weights=weights,scales=scales,
                    seed=seed,relay_enabled=relay_enabled,max_groups=max_groups,option_count=option_count,
                    beam_width=beam_width,full_candidates_per_group=full_candidates_per_group,
                    raw_spec_limit=raw_spec_limit,proposal_fraction=proposal_fraction,point_limit=point_limit,
                    pattern_rounds=pattern_rounds,point_eval_limit=point_eval_limit,
                    full_candidate_limit=full_candidate_limit,receiver_limit=receiver_limit,
                    compensation_limit=compensation_limit,position_limit=position_limit,
                    common_selector_records=common_selector_records)
    finally:
        restored=all(getattr(actual_repool,name) is function for name,function in original.items())
        if not restored:
            for name,function in original.items():
                setattr(actual_repool,name,function)
            raise RuntimeError("source frontier contexts did not restore every frozen function")
    metadata=outcome["metadata"]
    metadata.update(method="Source-Frontier-Cached-Receiver-First-Mobile-Repooling" if relay_enabled else
        "Source-Frontier-Cached-Matched-Physical-DIRECT-Control",frozen_driver_method=metadata["method"],
        joint_layout_statistics=layout_stats,source_frontier_statistics=frontier_stats,
        process_local_patches_restored=restored,process_local_patch_names=list(patch_names),
        source_frontier_parameters=dict(source_surface_limit=source_surface_limit,memo_limit=memo_limit,
            max_groups=max_groups,option_count=option_count,beam_width=beam_width,
            full_candidates_per_group=full_candidates_per_group,raw_spec_limit=raw_spec_limit,
            proposal_fraction=proposal_fraction,point_limit=point_limit,pattern_rounds=pattern_rounds,
            point_eval_limit=point_eval_limit,full_candidate_limit=full_candidate_limit,
            receiver_limit=receiver_limit,compensation_limit=compensation_limit,position_limit=position_limit,
            memo_enabled_in_both_arms=True,context_order="joint_outer_source_frontier_inner",
            physical_keys_and_deltas_unchanged=True,partial_incumbent_pruning=False),
        runtime_seconds=perf_counter()-started)
    return outcome
