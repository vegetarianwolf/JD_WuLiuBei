"""One-factor sensitivity analysis for Direct + Relay + Station.

The final protocol uses one fixed seed and five levels for each of three
parameters: fleet size, deadline tightness and station count.  All runs use
the combined scenario and the 300 s relay/station allowance.  Historical
results are never imported.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence

from uav_dispatch import Task, load_tasks_csv

from algorithm.experiments.run_scenario_comparison import (
    DEFAULT_INPUT,
    SCENARIOS,
    _config as scenario_alns_config,
    _environment_manifest,
    _git_state,
    _optional_sha256,
    _sha256,
    _solver_source_sha256,
    run_scenario,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "algorithm" / "results" / "final_sensitivity"
DEFAULT_SEED = 2026081701
FORMAL_PROTOCOL_CONFIG: dict[str, int | float] = {
    "seed": DEFAULT_SEED,
    "tasks": 200,
    "base_seconds": 240.0,
    "bonus_seconds": 60.0,
    "safety_margin_seconds": 0.05,
    "max_iterations": 10_000_000,
    "candidate_limit": 48,
}

SENSITIVITY_LEVELS: dict[str, tuple[int | float, ...]] = {
    "drone_count": (8, 9, 10, 12, 16),
    "deadline_multiplier": (0.8, 0.9, 1.0, 1.1, 1.2),
    "station_count": (2, 3, 4, 5, 6),
}
COMBINED_SCENARIO = next(
    scenario
    for scenario in SCENARIOS
    if scenario["id"] == "direct_relay_stations"
)


def scale_deadlines(
    tasks: Sequence[Task], multiplier: float
) -> tuple[Task, ...]:
    """Return the same requests with uniformly scaled latest-delivery times."""

    if multiplier <= 0:
        raise ValueError("截止期倍率必须为正数")
    return tuple(
        Task(
            task.id,
            task.pickup,
            task.delivery,
            task.deadline_min * multiplier,
        )
        for task in tasks
    )


def _levels(quick: bool) -> dict[str, tuple[int | float, ...]]:
    if not quick:
        return SENSITIVITY_LEVELS
    return {
        name: (values[0], values[len(values) // 2], values[-1])
        for name, values in SENSITIVITY_LEVELS.items()
    }


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    scalar = [
        {
            key: value
            for key, value in row.items()
            if not isinstance(value, (dict, list, tuple))
        }
        for row in rows
    ]
    if not scalar:
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(scalar[0]))
        writer.writeheader()
        writer.writerows(scalar)


def _write_report(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    lines = [
        "# 三机制组合灵敏度分析",
        "",
        "所有试验仅运行 Direct + Relay + Station，采用同一随机种子和"
        "（逾期任务数、总逾期时间、总航程）三级词典序目标。",
        "",
        "| 参数 | 水平 | 无人机 | 截止期倍率 | 中继站 | 逾期数 | 总逾期/min | 航程/km | Relay任务 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['parameter']} | {row['level']} | {row['drone_count']} | "
            f"{row['deadline_multiplier']} | {row['requested_station_count']} | "
            f"{row['late_count']} | {row['total_lateness_min']:.3f} | "
            f"{row['distance_km']:.3f} | {row['relay_task_count']} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--tasks", type=int, default=200)
    parser.add_argument("--base-seconds", type=float, default=240.0)
    parser.add_argument("--bonus-seconds", type=float, default=60.0)
    parser.add_argument("--safety-margin-seconds", type=float, default=0.05)
    parser.add_argument("--max-iterations", type=int, default=10_000_000)
    parser.add_argument("--candidate-limit", type=int, default=48)
    parser.add_argument("--quick", action="store_true")
    return parser


def validate_formal_protocol(args: argparse.Namespace) -> None:
    """Reject a non-final configuration unless the run is marked quick."""

    if args.quick:
        return
    mismatches = [
        f"{name}={getattr(args, name)!r}（应为 {expected!r}）"
        for name, expected in FORMAL_PROTOCOL_CONFIG.items()
        if getattr(args, name) != expected
    ]
    if not args.input.is_file():
        mismatches.append(f"input={args.input!s}（文件不存在）")
    elif hashlib.sha256(args.input.read_bytes()).digest() != hashlib.sha256(
        DEFAULT_INPUT.read_bytes()
    ).digest():
        mismatches.append("input_sha256 与官方数据不一致")
    if mismatches:
        raise ValueError("正式敏感性协议参数不可覆盖：" + "；".join(mismatches))


def build_manifest(
    args: argparse.Namespace,
    *,
    task_count: int,
) -> dict[str, Any]:
    """Build the versioned, self-auditing sensitivity manifest."""

    candidate_limit = (
        None if args.candidate_limit == 0 else args.candidate_limit
    )
    combined_config = asdict(
        scenario_alns_config(
            COMBINED_SCENARIO,
            seed=args.seed,
            solve_seconds=args.base_seconds + args.bonus_seconds,
            max_iterations=args.max_iterations,
            candidate_limit=candidate_limit,
            home_aware=True,
        )
    )
    combined_config.pop("seed")
    combined_config.pop("time_limit_seconds")
    combined_config["scenario_relay"] = True
    git_commit, git_dirty = _git_state()
    environment = _environment_manifest()
    scenario_script = (
        ROOT / "algorithm" / "experiments" / "run_scenario_comparison.py"
    )
    return {
        "schema_version": 1,
        "protocol": "combined_mode_one_factor_sensitivity",
        "formal": not args.quick,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "experiment_script_sha256": _sha256(Path(__file__)),
        "scenario_script_sha256": _sha256(scenario_script),
        "solver_source_sha256": _solver_source_sha256(),
        "pyproject_sha256": _optional_sha256(ROOT / "pyproject.toml"),
        "requirements_dev_sha256": _optional_sha256(
            ROOT / "requirements-dev.txt"
        ),
        "environment": environment,
        "python": environment["python_version"],
        "platform": environment["platform"],
        "input": str(args.input),
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "tasks": task_count,
        "seed": args.seed,
        "base_seconds": args.base_seconds,
        "bonus_seconds": args.bonus_seconds,
        "effective_seconds_per_run": args.base_seconds + args.bonus_seconds,
        "levels": _levels(args.quick),
        "scenario": COMBINED_SCENARIO,
        "solver_config": {
            "max_iterations": args.max_iterations,
            "candidate_limit": candidate_limit,
            "safety_margin_seconds": args.safety_margin_seconds,
            "max_tasks_per_drone": 25,
            "capacity": 2,
            "speed_km_per_min": 0.9,
            "depot_km": [0.0, 0.0],
            "relay_candidates_per_task": 2,
            "relay_detour_ratio": 2.0,
            "relay_location_seed": 42,
            "relay_location_method": "weighted_kmedoids",
            "baseline": {
                "drone_count": 8,
                "deadline_multiplier": 1.0,
                "station_count": 4,
            },
            "time_limit_policy": (
                "effective budget - initial construction - safety margin; "
                "exact per-run limit is stored in solution metadata"
            ),
            "input_transform": (
                "official task prefix; deadline_min multiplied by each "
                "runs[].deadline_multiplier"
            ),
            "alns_combined": combined_config,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    validate_formal_protocol(args)
    if args.quick:
        args.tasks = min(args.tasks, 24)
        args.base_seconds = min(args.base_seconds, 0.4)
        args.bonus_seconds = min(args.bonus_seconds, 0.2)
    source_tasks = load_tasks_csv(args.input)[: args.tasks]
    if len(source_tasks) != args.tasks:
        raise ValueError("输入任务数量不足")
    args.output_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for parameter, levels in _levels(args.quick).items():
        for level in levels:
            drones = int(level) if parameter == "drone_count" else 8
            deadline_multiplier = (
                float(level) if parameter == "deadline_multiplier" else 1.0
            )
            station_count = int(level) if parameter == "station_count" else 4
            tasks = scale_deadlines(source_tasks, deadline_multiplier)
            cell_dir = args.output_dir / f"{parameter}__{level}"
            print(
                f"[sensitivity] {parameter}={level} "
                f"(drones={drones}, deadline×{deadline_multiplier}, "
                f"stations={station_count})",
                flush=True,
            )
            row = run_scenario(
                tasks,
                COMBINED_SCENARIO,
                seed=args.seed,
                output_dir=cell_dir,
                input_path=args.input,
                drones=drones,
                max_tasks=25,
                base_seconds=args.base_seconds,
                bonus_seconds=args.bonus_seconds,
                safety_margin_seconds=args.safety_margin_seconds,
                max_iterations=args.max_iterations,
                candidate_limit=(
                    None if args.candidate_limit == 0 else args.candidate_limit
                ),
                relay_count=station_count,
                relay_location_seed=42,
            )
            row.update(
                {
                    "parameter": parameter,
                    "level": level,
                    "drone_count": drones,
                    "deadline_multiplier": deadline_multiplier,
                    "requested_station_count": station_count,
                    "cell_output_dir": cell_dir.relative_to(
                        args.output_dir
                    ).as_posix(),
                }
            )
            rows.append(row)

    manifest = build_manifest(args, task_count=len(source_tasks))
    (args.output_dir / "runs.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(args.output_dir / "runs.csv", rows)
    _write_report(args.output_dir / "REPORT.md", rows)
    print(f"结果已写入 {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
