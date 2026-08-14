"""Paired Direct-only versus Relay-aware ALNS comparison.

Every seed runs both variants on the same input with the same solver, the
same objective, and the same total wall-clock budget.  Variant order rotates
per seed to reduce system-load bias.  Judging is strictly lexicographic on
(late_count, total_lateness_min, distance_km) per paired seed.
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from uav_dispatch import (
    ALNSConfig,
    Problem,
    construct_regret_initial,
    evaluate_solution,
    load_tasks_csv,
    solve_alns_core,
)
from uav_dispatch.cli import result_payload
from uav_dispatch.relay import build_relay_network


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    ROOT / "algorithm" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)
DEFAULT_OUTPUT = ROOT / "algorithm" / "results" / "relay_comparison"
DEFAULT_SEED_BASE = 2026080500

VARIANTS = ("direct", "relay")
VARIANT_LABELS = {
    "direct": "Direct-only ALNS",
    "relay": "Relay-aware ALNS",
}


def _score_tuple(row: dict[str, Any]) -> tuple[int, float, float]:
    return (
        int(row["late_count"]),
        float(row["total_lateness_min"]),
        float(row["distance_km"]),
    )


def paired_lexicographic_comparison(
    rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    by_seed: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        by_seed.setdefault(int(row["seed"]), {})[row["variant"]] = row
    pairs = []
    left_win = 0
    tie = 0
    right_win = 0
    for seed in sorted(by_seed):
        direct = by_seed[seed].get("direct")
        relay = by_seed[seed].get("relay")
        if direct is None or relay is None:
            continue
        left_score = _score_tuple(direct)
        right_score = _score_tuple(relay)
        if right_score < left_score:
            winner = "relay"
            right_win += 1
        elif left_score < right_score:
            winner = "direct"
            left_win += 1
        else:
            winner = "tie"
            tie += 1
        pairs.append(
            {
                "seed": seed,
                "winner": winner,
                "direct_score": left_score,
                "relay_score": right_score,
                "late_count_delta_relay_minus_direct": (
                    right_score[0] - left_score[0]
                ),
                "total_lateness_delta_relay_minus_direct": (
                    right_score[1] - left_score[1]
                ),
                "distance_delta_relay_minus_direct": (
                    right_score[2] - left_score[2]
                ),
                "relay_task_count": relay.get("relay_task_count"),
                "cross_uav_handoff_count": relay.get(
                    "cross_uav_handoff_count"
                ),
                "iterations_per_second_relay": relay.get(
                    "iterations_per_second"
                ),
                "iterations_per_second_direct": direct.get(
                    "iterations_per_second"
                ),
            }
        )
    return {
        "left_variant": "direct",
        "right_variant": "relay",
        "direct_win": left_win,
        "tie": tie,
        "relay_win": right_win,
        "paired_count": len(pairs),
        "pairs": pairs,
    }


def _problem_for_variant(
    tasks, variant: str, args: argparse.Namespace
) -> Problem:
    if variant == "direct":
        network = build_relay_network(
            tasks,
            relay_count=0,
            candidates_per_task=args.relay_candidates_per_task,
            detour_ratio=args.relay_detour_ratio,
            location_seed=args.relay_location_seed,
        )
    else:
        network = build_relay_network(
            tasks,
            relay_count=args.relay_count,
            candidates_per_task=args.relay_candidates_per_task,
            detour_ratio=args.relay_detour_ratio,
            location_seed=args.relay_location_seed,
        )
    return Problem(
        tasks,
        drone_count=args.drones,
        max_tasks_per_drone=args.max_tasks,
        relay_stations=network.stations,
        leg_registry=network.leg_registry,
        task_relay_candidates=network.task_relay_candidates,
    )


def _run_variant(
    problem: Problem,
    variant: str,
    seed: int,
    args: argparse.Namespace,
    output_dir: Path,
) -> dict[str, Any]:
    construction_started = time.perf_counter()
    initial = construct_regret_initial(
        problem,
        candidate_limit=(
            None if args.candidate_limit == 0 else args.candidate_limit
        ),
    )
    construction_seconds = time.perf_counter() - construction_started
    solve_budget = max(
        0.0,
        args.wall_time_limit - construction_seconds - args.wall_safety_margin,
    )
    config = ALNSConfig(
        max_iterations=args.wall_max_iterations,
        time_limit_seconds=solve_budget,
        seed=seed,
        candidate_limit=None if args.candidate_limit == 0 else args.candidate_limit,
        relay_candidates_per_task=args.relay_candidates_per_task,
        relay_plan_beam=args.relay_plan_beam,
        relay_leg_beam=args.relay_leg_beam,
        relay_event_cap=args.relay_event_cap,
        relay_sample_every=args.relay_sample_every,
        relay_global_limit=args.relay_global_limit,
        relay_debug=args.relay_debug,
    )
    result = solve_alns_core(
        problem, config=config, initial_routes=initial.routes
    )
    evaluation = result.evaluation
    if not evaluation.valid:
        raise RuntimeError(
            f"{variant} seed {seed} 产出非法解: {evaluation.violations}"
        )
    relay_meta = result.metadata.get("relay", {})
    row = {
        "variant": variant,
        "seed": seed,
        "valid": evaluation.valid,
        "late_count": evaluation.score.late_count,
        "total_lateness_min": evaluation.score.total_lateness_min,
        "distance_km": evaluation.score.distance_km,
        "construction_seconds": construction_seconds,
        "runtime_seconds": result.runtime_seconds,
        "total_wall_seconds": construction_seconds + result.runtime_seconds,
        "iterations": result.iterations,
        "iterations_per_second": result.metadata.get(
            "iterations_per_second", 0.0
        ),
        "relay_count": relay_meta.get("relay_count", 0),
        "relay_task_count": relay_meta.get("relay_task_count", 0),
        "relay_share": relay_meta.get("relay_share", 0.0),
        "cross_uav_handoff_count": relay_meta.get(
            "cross_uav_handoff_count", 0
        ),
        "same_uav_relay_count": relay_meta.get("same_uav_relay_count", 0),
        "total_relay_waiting_time": relay_meta.get(
            "total_relay_waiting_time", 0.0
        ),
        "global_evaluation_count": result.metadata.get(
            "global_evaluation_count", 0
        ),
    }
    solution_file = (
        output_dir
        / "run_solutions"
        / f"wall_{variant}__seed_{seed}.json"
    )
    solution_file.parent.mkdir(parents=True, exist_ok=True)
    solution_file.write_text(
        json.dumps(
            result_payload(problem, result, source=args.input),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    row["solution_file"] = str(solution_file.relative_to(output_dir))
    print(
        f"[{variant:6s}] seed={seed} "
        f"score={evaluation.score} it/s={row['iterations_per_second']:.2f} "
        f"relay_tasks={row['relay_task_count']} "
        f"cross={row['cross_uav_handoff_count']}"
    )
    return row


def _summary(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries = []
    for variant in VARIANTS:
        variant_rows = [row for row in rows if row["variant"] == variant]
        if not variant_rows:
            continue
        summaries.append(
            {
                "variant": variant,
                "label": VARIANT_LABELS[variant],
                "runs": len(variant_rows),
                "mean_late_count": statistics.mean(
                    row["late_count"] for row in variant_rows
                ),
                "mean_total_lateness_min": statistics.mean(
                    row["total_lateness_min"] for row in variant_rows
                ),
                "mean_distance_km": statistics.mean(
                    row["distance_km"] for row in variant_rows
                ),
                "mean_iterations_per_second": statistics.mean(
                    row["iterations_per_second"] for row in variant_rows
                ),
                "mean_relay_task_count": statistics.mean(
                    row["relay_task_count"] for row in variant_rows
                ),
                "mean_cross_uav_handoff_count": statistics.mean(
                    row["cross_uav_handoff_count"] for row in variant_rows
                ),
            }
        )
    return summaries


def run(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "run_solutions").mkdir(parents=True, exist_ok=True)
    tasks = load_tasks_csv(args.input)
    if args.tasks is not None:
        tasks = tasks[: args.tasks]
    if args.tasks is not None and args.tasks <= 0:
        raise ValueError("--tasks 必须为正整数")

    rows: list[dict[str, Any]] = []
    seeds = [args.seed_base + offset for offset in range(args.wall_seed_count)]
    for seed in seeds:
        ordered = (
            list(VARIANTS)
            if seed % 2 == 0
            else list(reversed(VARIANTS))
        )
        for variant in ordered:
            problem = _problem_for_variant(tasks, variant, args)
            rows.append(
                _run_variant(problem, variant, seed, args, output_dir)
            )
            del problem
            gc.collect()

    comparison = paired_lexicographic_comparison(rows)
    summaries = _summary(rows)
    payload = {
        "manifest": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "input": str(args.input),
            "input_sha256": hashlib.sha256(
                Path(args.input).read_bytes()
            ).hexdigest(),
            "wall_time_limit": args.wall_time_limit,
            "wall_safety_margin": args.wall_safety_margin,
            "wall_seed_count": args.wall_seed_count,
            "seed_base": args.seed_base,
            "candidate_limit": args.candidate_limit,
            "relay_count": args.relay_count,
            "relay_candidates_per_task": args.relay_candidates_per_task,
            "relay_detour_ratio": args.relay_detour_ratio,
            "relay_plan_beam": args.relay_plan_beam,
            "relay_leg_beam": args.relay_leg_beam,
            "relay_event_cap": args.relay_event_cap,
            "relay_sample_every": args.relay_sample_every,
            "relay_global_limit": args.relay_global_limit,
            "relay_location_seed": args.relay_location_seed,
            "task_count": len(tasks),
            "drone_count": args.drones,
            "max_tasks_per_drone": args.max_tasks,
            "objective_order": [
                "late_count",
                "total_lateness_min",
                "distance_km",
            ],
        },
        "comparison": comparison,
        "summaries": summaries,
        "runs": rows,
    }
    (output_dir / "relay_comparison_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    fieldnames = sorted(
        {key for row in rows for key in row} - {"solution_file"}
    )
    with (output_dir / "relay_comparison_runs.csv").open(
        "w", newline="", encoding="utf-8"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {key: value for key, value in row.items() if key != "solution_file"}
            )
    return payload


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Direct-only 与 Relay-aware ALNS 的配对墙钟对比实验"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--output-dir", type=Path, default=DEFAULT_OUTPUT
    )
    parser.add_argument("--tasks", type=int)
    parser.add_argument("--drones", type=int, default=8)
    parser.add_argument("--max-tasks", type=int, default=25)
    parser.add_argument("--wall-seed-count", type=int, default=3)
    parser.add_argument("--seed-base", type=int, default=DEFAULT_SEED_BASE)
    parser.add_argument("--wall-time-limit", type=float, default=240.0)
    parser.add_argument("--wall-safety-margin", type=float, default=2.0)
    parser.add_argument("--wall-max-iterations", type=int, default=100_000)
    parser.add_argument("--candidate-limit", type=int, default=48)
    parser.add_argument("--relay-count", type=int, default=3)
    parser.add_argument("--relay-candidates-per-task", type=int, default=2)
    parser.add_argument("--relay-detour-ratio", type=float, default=1.3)
    parser.add_argument("--relay-plan-beam", type=int, default=4)
    parser.add_argument("--relay-leg-beam", type=int, default=6)
    parser.add_argument("--relay-event-cap", type=int, default=60)
    parser.add_argument("--relay-sample-every", type=int, default=4)
    parser.add_argument("--relay-global-limit", type=int, default=3)
    parser.add_argument("--relay-location-seed", type=int, default=42)
    parser.add_argument("--relay-debug", action="store_true")
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if args.quick:
        args.wall_seed_count = 1
        args.wall_time_limit = 10.0
        args.wall_safety_margin = 1.0
        args.tasks = 24
        args.drones = 2
        args.max_tasks = 12
    return args


def main() -> int:
    args = _parse_args()
    payload = run(args)
    comparison = payload["comparison"]
    print(
        f"paired: direct {comparison['direct_win']} | "
        f"tie {comparison['tie']} | relay {comparison['relay_win']} "
        f"over {comparison['paired_count']} seeds"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
