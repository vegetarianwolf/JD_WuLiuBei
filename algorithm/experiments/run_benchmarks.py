"""Final Section 5 experiments: exact oracle and relaxed multi-UAV scales."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from uav_dispatch import (
    ALNSConfig,
    Point,
    Problem,
    SolverResult,
    Task,
    construct_regret_initial,
    load_tasks_csv,
    solve_alns,
    solve_exact,
)
from uav_dispatch.cli import result_payload

from algorithm.experiments.run_scenario_comparison import (
    _environment_manifest,
    _git_state,
    _optional_sha256,
    _sha256,
    _solver_source_sha256,
)
from algorithm.experiments.run_sensitivity_analysis import scale_deadlines


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    ROOT / "algorithm" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)
DEFAULT_OUTPUT = ROOT / "algorithm" / "results" / "final_benchmarks"
EXACT_SIZES = (5, 8, 10, 12)
MULTISCALE_TASK_COUNTS = (50, 100, 150, 200)
RELAXED_DEADLINE_MULTIPLIER = 2.5
EXACT_ALNS_ITERATIONS = 1_000
QUICK_EXACT_ALNS_ITERATIONS = 100
MAX_TASKS_PER_DRONE = 25
FORMAL_PROTOCOL_CONFIG: dict[str, int | float] = {
    "seed": 2026081701,
    "scale_seconds": 30.0,
    "max_iterations": 10_000_000,
    "candidate_limit": 48,
}


def _score_tuple(result: SolverResult) -> tuple[int, float, float]:
    score = result.evaluation.score
    return score.late_count, score.total_lateness_min, score.distance_km


def _best_within_budget(
    results: list[SolverResult], budget_seconds: float
) -> SolverResult:
    """Return the lexicographic best run that met its wall-clock budget."""

    compliant = [
        result for result in results if result.runtime_seconds <= budget_seconds
    ]
    if not compliant:
        raise RuntimeError(
            f"没有运行在 {budget_seconds:.3f} 秒时间预算内完成"
        )
    return min(compliant, key=_score_tuple)


def report_example_problem() -> Problem:
    """Return the title's two-task distance-matrix example."""

    matrix = (
        (0, 5, 9, 8, 9),
        (5, 0, 4, 1, 5),
        (9, 4, 0, 4, 3),
        (8, 1, 4, 0, 2),
        (9, 5, 3, 2, 0),
    )
    tasks = (
        Task(1, Point(0, 0), Point(0, 0), 100),
        Task(2, Point(0, 0), Point(0, 0), 100),
    )
    return Problem(
        tasks,
        drone_count=1,
        max_tasks_per_drone=2,
        distance_matrix_km=matrix,
    )


def _row(
    *,
    experiment: str,
    method: str,
    problem: Problem,
    result: SolverResult,
    seed: int | None,
    deadline_multiplier: float,
    oracle: SolverResult | None = None,
) -> dict[str, Any]:
    score = result.evaluation.score
    row: dict[str, Any] = {
        "experiment": experiment,
        "method": method,
        "task_count": len(problem.tasks),
        "drone_count": problem.drone_count,
        "deadline_multiplier": deadline_multiplier,
        "seed": seed,
        "late_count": score.late_count,
        "total_lateness_min": score.total_lateness_min,
        "distance_km": score.distance_km,
        "runtime_seconds": result.runtime_seconds,
        "iterations": result.iterations,
        "valid": result.evaluation.valid,
        "operator_statistics": {
            key: dict(value)
            for key, value in result.metadata.get(
                "operator_statistics", {}
            ).items()
        },
    }
    if oracle is not None:
        exact_score = oracle.evaluation.score
        row.update(
            {
                "oracle_late_count": exact_score.late_count,
                "oracle_total_lateness_min": exact_score.total_lateness_min,
                "oracle_distance_km": exact_score.distance_km,
                "matches_oracle": score == exact_score,
            }
        )
    return row


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    scalar = [
        {
            key: value
            for key, value in row.items()
            if not isinstance(value, (dict, list, tuple))
        }
        for row in rows
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(scalar[0]))
        writer.writeheader()
        writer.writerows(scalar)


def _write_report(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    lines = [
        "# 第5章算法验证结果",
        "",
        "## 精确求解与小规模对照",
        "",
        "| 算例 | 方法 | 任务 | 逾期数 | 总逾期/min | 航程/km | 时间/s | 与精确解一致 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        if row["deadline_multiplier"] != 1.0:
            continue
        lines.append(
            f"| {row['experiment']} | {row['method']} | {row['task_count']} | "
            f"{row['late_count']} | {row['total_lateness_min']:.3f} | "
            f"{row['distance_km']:.3f} | {row['runtime_seconds']:.4f} | "
            f"{row.get('matches_oracle', '—')} |"
        )
    lines.extend(
        [
            "",
            "## 宽松截止期多机规模实验",
            "",
            "截止期统一乘以2.5，仅用于验证算法在非紧张多机算例上的规模适应性。",
            "",
            "| 任务 | 无人机 | 逾期数 | 总逾期/min | 航程/km | 时间/s | 迭代 |",
            "|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in rows:
        if row["deadline_multiplier"] == 1.0:
            continue
        lines.append(
            f"| {row['task_count']} | {row['drone_count']} | "
            f"{row['late_count']} | {row['total_lateness_min']:.3f} | "
            f"{row['distance_km']:.3f} | {row['runtime_seconds']:.2f} | "
            f"{row['iterations']} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed", type=int, default=2026081701)
    parser.add_argument("--scale-seconds", type=float, default=30.0)
    parser.add_argument("--max-iterations", type=int, default=10_000_000)
    parser.add_argument("--candidate-limit", type=int, default=48)
    parser.add_argument("--quick", action="store_true")
    return parser


def validate_formal_protocol(args: argparse.Namespace) -> None:
    """Reject non-final Section 5 settings unless explicitly marked quick."""

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
        raise ValueError("正式第5章协议参数不可覆盖：" + "；".join(mismatches))


def build_manifest(
    args: argparse.Namespace,
    *,
    exact_sizes: Sequence[int],
    scale_counts: Sequence[int],
    input_task_count: int,
) -> dict[str, Any]:
    """Build the versioned, self-auditing Section 5 manifest."""

    exact_alns = asdict(
        ALNSConfig(
            max_iterations=(
                QUICK_EXACT_ALNS_ITERATIONS
                if args.quick
                else EXACT_ALNS_ITERATIONS
            ),
            seed=args.seed,
            candidate_limit=None,
        )
    )
    multiscale_alns = asdict(
        ALNSConfig(
            max_iterations=args.max_iterations,
            time_limit_seconds=args.scale_seconds,
            seed=args.seed,
            candidate_limit=args.candidate_limit,
        )
    )
    git_commit, git_dirty = _git_state()
    environment = _environment_manifest()
    return {
        "schema_version": 1,
        "protocol": "section5_exact_and_relaxed_multiscale",
        "formal": not args.quick,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "experiment_script_sha256": _sha256(Path(__file__)),
        "solver_source_sha256": _solver_source_sha256(),
        "pyproject_sha256": _optional_sha256(ROOT / "pyproject.toml"),
        "requirements_dev_sha256": _optional_sha256(
            ROOT / "requirements-dev.txt"
        ),
        "environment": environment,
        "python": environment["python_version"],
        "platform": environment["platform"],
        "input": str(args.input),
        "input_sha256": _sha256(args.input),
        "input_task_count": input_task_count,
        "exact_sizes": list(exact_sizes),
        "multiscale_task_counts": list(scale_counts),
        "relaxed_deadline_multiplier": RELAXED_DEADLINE_MULTIPLIER,
        "scale_seconds": args.scale_seconds,
        "seed": args.seed,
        "operator_count": 14,
        "solver_config": {
            "exact_method": "Pareto-label dynamic programming",
            "exact_max_tasks": max(EXACT_SIZES),
            "exact_initial_method": "regret2",
            "multiscale_initial_method": "regret2",
            "max_tasks_per_drone": MAX_TASKS_PER_DRONE,
            "drone_count_policy": (
                "ceil(task_count / max_tasks_per_drone)"
            ),
            "capacity": 2,
            "speed_km_per_min": 0.9,
            "depot_km": [0.0, 0.0],
            "input_transform": (
                "official task prefix; deadline_min multiplied by "
                "relaxed_deadline_multiplier for multiscale runs"
            ),
            "time_limit_policy": (
                "scale_seconds includes initial construction; ALNS receives "
                "the positive remaining budget"
            ),
            "exact_alns": exact_alns,
            "multiscale_alns": multiscale_alns,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    validate_formal_protocol(args)
    if not math.isfinite(args.scale_seconds) or args.scale_seconds <= 0:
        raise ValueError("规模实验时间预算必须为有限正数")
    if args.quick:
        args.scale_seconds = min(args.scale_seconds, 0.5)
    tasks = load_tasks_csv(args.input)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    solution_dir = args.output_dir / "solutions"
    solution_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []

    example = report_example_problem()
    example_exact = solve_exact(example)
    rows.append(
        _row(
            experiment="title_example_n2",
            method="Pareto-DP",
            problem=example,
            result=example_exact,
            seed=None,
            deadline_multiplier=1.0,
            oracle=example_exact,
        )
    )

    exact_sizes = EXACT_SIZES[:2] if args.quick else EXACT_SIZES
    for size in exact_sizes:
        problem = Problem(
            tasks[:size], drone_count=1, max_tasks_per_drone=size
        )
        oracle = solve_exact(problem, max_tasks=max(EXACT_SIZES))
        rows.append(
            _row(
                experiment=f"official_prefix_n{size}",
                method="Pareto-DP",
                problem=problem,
                result=oracle,
                seed=None,
                deadline_multiplier=1.0,
                oracle=oracle,
            )
        )
        initial = construct_regret_initial(problem, candidate_limit=None)
        heuristic = solve_alns(
            problem,
            config=ALNSConfig(
                max_iterations=(
                    QUICK_EXACT_ALNS_ITERATIONS
                    if args.quick
                    else EXACT_ALNS_ITERATIONS
                ),
                seed=args.seed,
                candidate_limit=None,
            ),
            initial_routes=initial.routes,
        )
        rows.append(
            _row(
                experiment=f"official_prefix_n{size}",
                method="C2-Lex-ALNS",
                problem=problem,
                result=heuristic,
                seed=args.seed,
                deadline_multiplier=1.0,
                oracle=oracle,
            )
        )

    scale_counts = MULTISCALE_TASK_COUNTS[:2] if args.quick else MULTISCALE_TASK_COUNTS
    for task_count in scale_counts:
        relaxed = scale_deadlines(
            tasks[:task_count], RELAXED_DEADLINE_MULTIPLIER
        )
        drone_count = math.ceil(task_count / MAX_TASKS_PER_DRONE)
        problem = Problem(
            relaxed,
            drone_count=drone_count,
            max_tasks_per_drone=MAX_TASKS_PER_DRONE,
        )
        wall_started = time.perf_counter()
        initial = construct_regret_initial(
            problem, candidate_limit=args.candidate_limit
        )
        remaining = max(
            1e-6,
            args.scale_seconds - (time.perf_counter() - wall_started),
        )
        result = solve_alns(
            problem,
            config=ALNSConfig(
                max_iterations=args.max_iterations,
                time_limit_seconds=remaining,
                seed=args.seed,
                candidate_limit=args.candidate_limit,
            ),
            initial_routes=initial.routes,
        )
        rows.append(
            _row(
                experiment=f"relaxed_multiuav_n{task_count}",
                method="C2-Lex-ALNS",
                problem=problem,
                result=result,
                seed=args.seed,
                deadline_multiplier=RELAXED_DEADLINE_MULTIPLIER,
            )
        )
        solution_path = solution_dir / f"relaxed_multiuav_n{task_count}.json"
        solution_path.write_text(
            json.dumps(
                result_payload(problem, result, source=str(args.input)),
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(
            f"[relaxed n={task_count:3d}] drones={drone_count} "
            f"score={result.evaluation.score} runtime={result.runtime_seconds:.1f}s",
            flush=True,
        )

    manifest = build_manifest(
        args,
        exact_sizes=exact_sizes,
        scale_counts=scale_counts,
        input_task_count=len(tasks),
    )
    payload = {"manifest": manifest, "runs": rows}
    (args.output_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(args.output_dir / "runs.csv", rows)
    _write_report(args.output_dir / "REPORT.md", rows)
    print(f"结果已写入 {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
