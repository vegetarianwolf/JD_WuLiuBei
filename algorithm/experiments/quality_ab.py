"""Quality A/B: legacy full-eval candidate ranking vs incremental delta ranking.

Run with the same seed and iteration budget in both modes:
    UAV_DISPATCH_LEGACY_CANDIDATE_EVAL=0 python -m experiments.quality_ab ...
    UAV_DISPATCH_LEGACY_CANDIDATE_EVAL=1 python -m experiments.quality_ab ...
"""

from __future__ import annotations

import argparse
import json
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
)
from uav_dispatch.relay import build_relay_network  # noqa: E402

DEFAULT_INPUT = (
    ROOT / "algorithm" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--seed", type=int, default=2026080500)
    parser.add_argument("--relay", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    tasks = load_tasks_csv(DEFAULT_INPUT)
    network = build_relay_network(
        tasks,
        relay_count=3 if args.relay else 0,
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
    initial = construct_regret_initial(problem, candidate_limit=48)
    config = ALNSConfig(
        max_iterations=args.iterations,
        seed=args.seed,
        candidate_limit=48,
    )
    result = solve_alns(problem, config=config, initial_routes=initial.routes)
    score = result.evaluation.score
    payload = {
        "seed": args.seed,
        "iterations": args.iterations,
        "relay": args.relay,
        "late_count": score.late_count,
        "total_lateness_min": score.total_lateness_min,
        "distance_km": score.distance_km,
        "runtime_seconds": result.runtime_seconds,
        "relay_task_count": result.metadata.get("relay", {}).get(
            "relay_task_count", 0
        ),
        "cross_uav_handoff_count": result.metadata.get("relay", {}).get(
            "cross_uav_handoff_count", 0
        ),
        "profiling": dict(result.metadata.get("profiling", {})),
    }
    if args.json:
        print(json.dumps(payload))
    else:
        print(payload)


if __name__ == "__main__":
    main()
