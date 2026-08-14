"""Relay destroy-schedule ablation: 4-10% vs 3-6% vs 3-6%+periodic large.

Usage:
    python -m experiments.destroy_ablation --seconds 45 --seed 2026080500
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "algorithm" / "src"))

from uav_dispatch import (  # noqa: E402
    ALNSConfig,
    Problem,
    construct_regret_initial,
    load_tasks_csv,
    solve_alns,
)
from uav_dispatch.relay import build_relay_network  # noqa: E402

DEFAULT_INPUT = (
    ROOT / "algorithm" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)


def run_variant(problem: Problem, config: ALNSConfig, label: str) -> dict:
    initial = construct_regret_initial(problem, candidate_limit=48)
    result = solve_alns(problem, config=config, initial_routes=initial.routes)
    score = result.evaluation.score
    relay = result.metadata.get("relay", {})
    return {
        "variant": label,
        "late_count": score.late_count,
        "total_lateness_min": score.total_lateness_min,
        "distance_km": score.distance_km,
        "iterations": result.iterations,
        "iterations_per_second": result.metadata.get(
            "iterations_per_second", 0.0
        ),
        "relay_task_count": relay.get("relay_task_count", 0),
        "cross_uav_handoff_count": relay.get("cross_uav_handoff_count", 0),
        "profiling": dict(result.metadata.get("profiling", {})),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=45.0)
    parser.add_argument("--seed", type=int, default=2026080500)
    args = parser.parse_args()

    tasks = load_tasks_csv(DEFAULT_INPUT)
    network = build_relay_network(
        tasks,
        relay_count=3,
        candidates_per_task=2,
        detour_ratio=1.3,
        location_seed=42,
    )
    problem = Problem(
        tasks,
        drone_count=8,
        max_tasks_per_drone=25,
        relay_stations=network.stations,
        leg_registry=network.leg_registry,
        task_relay_candidates=network.task_relay_candidates,
    )
    base = ALNSConfig(
        max_iterations=100_000,
        time_limit_seconds=args.seconds,
        seed=args.seed,
        candidate_limit=48,
    )
    variants = [
        (
            "relay_destroy_4_10",
            replace(
                base,
                relay_min_destroy_fraction=0.04,
                relay_max_destroy_fraction=0.10,
                relay_large_destroy_interval=0,
            ),
        ),
        (
            "relay_destroy_3_6",
            replace(
                base,
                relay_min_destroy_fraction=0.03,
                relay_max_destroy_fraction=0.06,
                relay_large_destroy_interval=0,
            ),
        ),
        (
            "relay_destroy_3_6_periodic_large",
            base,  # defaults: 3-6% + every-30 large 6-10%
        ),
    ]
    rows = []
    for label, config in variants:
        rows.append(run_variant(problem, config, label))
    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
