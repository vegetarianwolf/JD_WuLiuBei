"""Mobile-aware layout generation over the frozen receiver-first driver.

The process-local context adds mobile geometry to bounded layout ranking,
while every State.key remains its real fixed-network global weighted key.
The four original station families remain physical baselines. Partial
geometry bounds only choose diversity slots; they never replace complete
manual/canonical scoring or authorize an incumbent adoption.

Both arms use identical selector and finite construction limits. DIRECT
delegates to the original physical ranking and creates no rendezvous resource.
RELAY keeps the frozen driver's raw fixed-site dominance guard, one-new-site
contract, arrival-order/zero-cache physics and complete pre-deadline checks.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter

try:
    import mobile_repooling_refinement as frozen
    import joint_mobile_layouts as layouts
except ImportError:
    from . import mobile_repooling_refinement as frozen
    from . import joint_mobile_layouts as layouts


PATCH_NAMES=("_rebuild","_diverse","_rank_receiver")


def refine(problem,routes,*,deadline,weights,scales,seed=2026101506,relay_enabled=True,
           max_groups=8,option_count=4,beam_width=8,full_candidates_per_group=8,
           raw_spec_limit=64,proposal_fraction=.55,point_limit=24,pattern_rounds=4,
           point_eval_limit=96,full_candidate_limit=32,receiver_limit=2,
           compensation_limit=3,position_limit=16,common_selector_records=None):
    """Generate joint layout/point proposals within one absolute worker budget."""
    if not isfinite(deadline) or type(relay_enabled) is not bool:
        raise ValueError("finite absolute deadline and boolean relay arm required")
    if any(type(v) is not int or not 1<=v<=8 for v in (beam_width,full_candidates_per_group)):
        raise ValueError("joint beam and complete candidate widths must be between one and eight")
    weights,scales=tuple(weights),tuple(scales)
    started=perf_counter()
    layout_stats={}
    actual_repool=frozen.repool
    originals={name:getattr(actual_repool,name) for name in PATCH_NAMES}
    try:
        with layouts.joint_layouts(actual_repool,problem,weights=weights,scales=scales,
                deadline=deadline,statistics=layout_stats):
            outcome=frozen.refine(problem,routes,deadline=deadline,weights=weights,scales=scales,
                seed=seed,relay_enabled=relay_enabled,max_groups=max_groups,option_count=option_count,
                beam_width=beam_width,full_candidates_per_group=full_candidates_per_group,
                raw_spec_limit=raw_spec_limit,proposal_fraction=proposal_fraction,point_limit=point_limit,
                pattern_rounds=pattern_rounds,point_eval_limit=point_eval_limit,
                full_candidate_limit=full_candidate_limit,receiver_limit=receiver_limit,
                compensation_limit=compensation_limit,position_limit=position_limit,
                common_selector_records=common_selector_records)
    finally:
        restored=all(getattr(actual_repool,name) is function for name,function in originals.items())
        if not restored:
            # Contain a failed context to this caller, before surfacing failure.
            for name,function in originals.items():
                setattr(actual_repool,name,function)
            raise RuntimeError("joint layout context did not restore the frozen repooling functions")
    metadata=outcome["metadata"]
    metadata.update(method="Joint-Layout-Receiver-First-Mobile-Repooling" if relay_enabled else
        "Joint-Layout-Matched-Physical-DIRECT-Control",frozen_driver_method=metadata["method"],
        joint_layout_statistics=layout_stats,joint_layout_patches_restored=restored,
        joint_layout_parameters=dict(max_groups=max_groups,option_count=option_count,beam_width=beam_width,
            full_candidates_per_group=full_candidates_per_group,raw_spec_limit=raw_spec_limit,
            proposal_fraction=proposal_fraction,point_limit=point_limit,pattern_rounds=pattern_rounds,
            point_eval_limit=point_eval_limit,full_candidate_limit=full_candidate_limit,
            receiver_limit=receiver_limit,compensation_limit=compensation_limit,position_limit=position_limit,
            direct_ranking="original_complete_physical_key",mobile_bounds_are_ranking_only=True),
        runtime_seconds=perf_counter()-started)
    return outcome
