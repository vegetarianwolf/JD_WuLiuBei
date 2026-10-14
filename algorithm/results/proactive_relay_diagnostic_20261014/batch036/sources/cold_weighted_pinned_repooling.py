"""Colder weighted ordinary polish around the frozen four-task macro.

Only the ALNS temperature profile and iteration ceiling differ from the
full-credit pinned experiment. Both arms retain the same proposal budget,
selector, source layouts, caches, four pinned tasks and complete-score guard.
The lower temperature is a search hypothesis, not a change to the objective.
"""
from __future__ import annotations

from dataclasses import replace
from math import isfinite
from time import perf_counter

try:
    import full_credit_pinned_repooling as frozen
except ImportError:
    from . import full_credit_pinned_repooling as frozen


def refine(problem,routes,*,deadline,weights,scales,seed=2026101511,relay_enabled=True,
           proposal_fraction=.35,candidate_limit=2,proposal_margin=.015,
           guard_reserve_seconds=.03,guard_reserve_fraction=.05,alns_module=None,polish_config=None,
           max_groups=8,receiver_limit=2,compensation_limit=3,position_limit=16,
           option_count=4,beam_width=8,full_candidates_per_group=8,common_selector_records=None,
           compensation_geometry_pool_limit=6,compensation_removal_pool_limit=6,
           receiver_bundle_memo_limit=8192,source_surface_limit=512,memo_limit=8192,
           polish_initial_temperature=.0025,polish_minimum_temperature=.00001,
           polish_cooling_rate=.97,polish_max_iterations=1000000):
    """Use the actual worker ALNSConfig and preserve every other setting."""
    temperatures=(polish_initial_temperature,polish_minimum_temperature,polish_cooling_rate)
    if (any(type(x) is bool or not isfinite(x) or x<=0 for x in temperatures)
            or polish_minimum_temperature>polish_initial_temperature or polish_cooling_rate>1
            or type(polish_max_iterations) is not int or not 1<=polish_max_iterations<=1000000):
        raise ValueError("finite positive temperatures, Tmin<=T0, cooling<=1 and iterations in 1..1000000 required")
    if alns_module is None:
        from uav_dispatch import alns as alns_module
    if polish_config is not None and not isinstance(polish_config,alns_module.ALNSConfig):
        raise ValueError("polish_config must belong to the actual worker ALNS module")
    config=replace(polish_config if polish_config is not None else alns_module.ALNSConfig(),
        initial_temperature=polish_initial_temperature,minimum_temperature=polish_minimum_temperature,
        cooling_rate=polish_cooling_rate,max_iterations=polish_max_iterations)
    started=perf_counter()
    outcome=frozen.refine(problem,routes,deadline=deadline,weights=weights,scales=scales,
        seed=seed,relay_enabled=relay_enabled,proposal_fraction=proposal_fraction,
        candidate_limit=candidate_limit,proposal_margin=proposal_margin,guard_reserve_seconds=guard_reserve_seconds,
        guard_reserve_fraction=guard_reserve_fraction,alns_module=alns_module,polish_config=config,
        max_groups=max_groups,receiver_limit=receiver_limit,compensation_limit=compensation_limit,
        position_limit=position_limit,option_count=option_count,beam_width=beam_width,
        full_candidates_per_group=full_candidates_per_group,common_selector_records=common_selector_records,
        compensation_geometry_pool_limit=compensation_geometry_pool_limit,
        compensation_removal_pool_limit=compensation_removal_pool_limit,
        receiver_bundle_memo_limit=receiver_bundle_memo_limit,source_surface_limit=source_surface_limit,
        memo_limit=memo_limit)
    metadata=outcome["metadata"]
    metadata.update(method="Cold-Weighted-Fixed-Network-Pinned-Repooling" if relay_enabled else
        "Cold-Weighted-Fixed-Network-Pinned-DIRECT-Control",cold_weighted_wrapped_method=metadata["method"],
        cold_polish_profile=dict(initial_temperature=config.initial_temperature,
            minimum_temperature=config.minimum_temperature,cooling_rate=config.cooling_rate,
            max_iterations=config.max_iterations),
        cold_weighted_parameters=dict(polish_initial_temperature=polish_initial_temperature,
            polish_minimum_temperature=polish_minimum_temperature,polish_cooling_rate=polish_cooling_rate,
            polish_max_iterations=polish_max_iterations,temperature_unit="weighted_J",
            actual_worker_config=True,same_profile_in_both_arms=True,other_settings_preserved=True,
            unchanged_physical_objective=True,unchanged_adoption_guard=True),
        runtime_seconds=perf_counter()-started)
    return outcome
