"""Dependency-free profiling counters shared across the solver modules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class SearchCounters:
    """Lightweight profiling counters shared by the whole search loop."""

    route_full_evaluation_count: int = 0
    route_delta_evaluation_count: int = 0
    route_materialization_count: int = 0
    relay_beam_build_count: int = 0
    relay_beam_cache_hit_count: int = 0
    relay_leg_cache_hit_count: int = 0
    relay_plans_for_task_count: int = 0
    relay_global_evaluation_count: int = 0
    repair_rank_evaluation_count: int = 0
    repair_exact_evaluation_count: int = 0
    surface_build_count: int = 0
    surface_cache_hit_count: int = 0
    surface_cache_miss_count: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "route_full_evaluation_count": self.route_full_evaluation_count,
            "route_delta_evaluation_count": self.route_delta_evaluation_count,
            "route_materialization_count": self.route_materialization_count,
            "relay_beam_build_count": self.relay_beam_build_count,
            "relay_beam_cache_hit_count": self.relay_beam_cache_hit_count,
            "relay_leg_cache_hit_count": self.relay_leg_cache_hit_count,
            "relay_plans_for_task_count": self.relay_plans_for_task_count,
            "relay_global_evaluation_count": self.relay_global_evaluation_count,
            "repair_rank_evaluation_count": self.repair_rank_evaluation_count,
            "repair_exact_evaluation_count": self.repair_exact_evaluation_count,
            "surface_build_count": self.surface_build_count,
            "surface_cache_hit_count": self.surface_cache_hit_count,
            "surface_cache_miss_count": self.surface_cache_miss_count,
        }
