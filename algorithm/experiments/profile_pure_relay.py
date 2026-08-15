"""Ad-hoc cProfile of the exact pure_relay scenario (warmup=0).

Reproduces the scenario-comparison pure_relay configuration:
200 tasks / 8 UAV / K=25, 7 relay stations, candidates_per_task=2,
detour_ratio=2.0, location_seed=42, seed 2026080500, warmup=0.0.

Usage:
    python -m experiments.profile_pure_relay [--seconds 15]
"""

from __future__ import annotations

import argparse
import cProfile
import pstats
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "algorithm" / "src"))

from uav_dispatch import (  # noqa: E402
    ALNSConfig,
    Problem,
    construct_regret_initial,
    load_tasks_csv,
    solve_relay_staged,
)
from uav_dispatch.relay import build_relay_network  # noqa: E402

DEFAULT_INPUT = (
    ROOT / "algorithm" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)


def build_problem() -> Problem:
    tasks = load_tasks_csv(DEFAULT_INPUT)
    network = build_relay_network(
        tasks,
        relay_count=7,
        candidates_per_task=2,
        detour_ratio=2.0,
        location_seed=42,
    )
    return Problem(
        tasks,
        drone_count=8,
        max_tasks_per_drone=25,
        relay_stations=network.stations,
        leg_registry=network.leg_registry,
        task_relay_candidates=network.task_relay_candidates,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--seed", type=int, default=2026080500)
    parser.add_argument("--sort", default="tottime")
    parser.add_argument("--lines", type=int, default=35)
    parser.add_argument("--no-profile", action="store_true")
    args = parser.parse_args()

    problem = build_problem()
    initial = construct_regret_initial(problem, candidate_limit=48)
    config = ALNSConfig(
        max_iterations=100_000,
        time_limit_seconds=args.seconds,
        seed=args.seed,
        candidate_limit=48,
        relay_direct_warmup_fraction=0.0,
    )

    def run_solver():
        return solve_relay_staged(
            problem,
            config=config,
            initial_routes=initial.routes,
            core=True,
        )

    if args.no_profile:
        result = run_solver()
    else:
        profiler = cProfile.Profile()
        profiler.enable()
        result = run_solver()
        profiler.disable()
        stats = pstats.Stats(profiler)
        stats.sort_stats(args.sort)
        stats.print_stats(args.lines)

    print("== RESULT ==")
    print("score:", result.evaluation.score)
    print("iters:", result.iterations)
    print("it/s:", result.metadata.get("iterations_per_second"))
    print("global_eval:", result.metadata.get("global_evaluation_count"))
    print("refine_eval:", result.metadata.get("refine_candidates_evaluated"))
    print("refine_prune:", result.metadata.get("refine_candidates_pruned"))
    print("leg_cache_hits:", result.metadata.get("relay_leg_cache_hits"))
    print("leg_cache_misses:", result.metadata.get("relay_leg_cache_misses"))
    print("generated_relay:", result.metadata.get("generated_relay_options"))
    print("selected_relay:", result.metadata.get("selected_relay_options"))
    relay_meta = result.metadata.get("relay", {})
    if relay_meta:
        print(
            "relay_tasks:", relay_meta.get("relay_task_count"),
            "cross:", relay_meta.get("cross_uav_handoff_count"),
        )
    print("profiling:", dict(result.metadata.get("profiling", {})))


if __name__ == "__main__":
    main()
