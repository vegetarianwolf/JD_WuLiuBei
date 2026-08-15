"""Quick profiling helper: cProfile a short relay/direct ALNS run.

Usage:
    python -m experiments.profile_run [--relay | --direct] [--seconds 30]

The default is ALNS Core for a mechanism-level Direct/Relay comparison.
Use ``--solver halns`` only when profiling each variant's production defaults;
HALNS enables ejection for Direct while Relay currently skips that operator.
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
    solve_alns,
    solve_alns_core,
    solve_relay_staged,
)
from uav_dispatch.relay import build_relay_network  # noqa: E402

DEFAULT_INPUT = (
    ROOT / "algorithm" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)


def build_problem(args: argparse.Namespace) -> Problem:
    tasks = load_tasks_csv(DEFAULT_INPUT)
    network = build_relay_network(
        tasks,
        relay_count=0 if args.direct else args.relay_count,
        candidates_per_task=1,
        detour_ratio=1.3,
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
    parser.add_argument("--relay", action="store_true")
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--relay-count", type=int, default=2)
    parser.add_argument(
        "--pure-relay",
        action="store_true",
        help="profile only the relay phase instead of the staged production solver",
    )
    parser.add_argument(
        "--solver",
        choices=("core", "halns"),
        default="core",
        help="core is the fair Direct/Relay operator comparison",
    )
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=2026080500)
    parser.add_argument(
        "--no-profile",
        action="store_true",
        help="run without cProfile to measure the real wall-clock it/s",
    )
    parser.add_argument(
        "--fixed-iterations",
        type=int,
        default=None,
        help="fixed-iteration quality run (disables the time limit)",
    )
    args = parser.parse_args()

    problem = build_problem(args)
    # Mirror the benchmark harness: build the initial solution without a
    # relay-aware constructor, then run the timed ALNS search on it.
    initial = construct_regret_initial(problem, candidate_limit=48)
    if args.fixed_iterations is not None:
        config = ALNSConfig(
            max_iterations=args.fixed_iterations,
            time_limit_seconds=None,
            seed=args.seed,
            candidate_limit=48,
        )
    else:
        config = ALNSConfig(
            max_iterations=100_000,
            time_limit_seconds=args.seconds,
            seed=args.seed,
            candidate_limit=48,
        )
    solver = solve_alns_core if args.solver == "core" else solve_alns

    def run_solver():
        if problem.has_relays and not args.pure_relay:
            return solve_relay_staged(
                problem,
                config=config,
                initial_routes=initial.routes,
                core=args.solver == "core",
            )
        return solver(problem, config=config, initial_routes=initial.routes)

    if args.no_profile:
        result = run_solver()
    else:
        profiler = cProfile.Profile()
        profiler.enable()
        result = run_solver()
        profiler.disable()
        stats = pstats.Stats(profiler)
        stats.sort_stats("cumulative")
        stats.print_stats(40)
    print("== RESULT ==")
    print("solver:", args.solver)
    print("staged_relay:", result.metadata.get("staged_relay", False))
    print(
        "score:", result.evaluation.score,
        "iters:", result.iterations,
        "it/s:", result.metadata.get("iterations_per_second"),
    )
    if result.metadata.get("relay"):
        relay_meta = result.metadata["relay"]
        print(
            "relay_tasks:", relay_meta.get("relay_task_count"),
            "cross:", relay_meta.get("cross_uav_handoff_count"),
            "wait:", relay_meta.get("total_relay_waiting_time"),
        )
    print("profiling:", dict(result.metadata.get("profiling", {})))


if __name__ == "__main__":
    main()
