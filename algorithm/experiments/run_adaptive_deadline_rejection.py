"""Run paired A2 adaptive-deadline and temporary-rejection experiments."""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from uav_dispatch import (
    ALNSConfig,
    Point,
    Problem,
    SolverResult,
    construct_regret_initial,
    evaluate_solution,
    load_tasks_csv,
    solve_alns_core,
)
from uav_dispatch.cli import result_payload


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    ROOT / "algorithm" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)
DEFAULT_OUTPUT = ROOT / "results" / "adaptive_deadline_rejection"
DEFAULT_SEED_BASE = 2026080500


@dataclass(frozen=True, slots=True)
class AdaptiveVariant:
    """One explicit A2-based experiment configuration."""

    name: str
    label: str
    enable_late_risk_destroy: bool = False
    enable_rejection_pool: bool = False
    defer_rejected_tasks: bool = False
    enable_on_time_distance_objective: bool = False
    enable_soft_deadline: bool = False


def build_variants() -> tuple[AdaptiveVariant, ...]:
    """Return the baseline and all adaptive-deadline experiment variants."""

    return (
        AdaptiveVariant("baseline", "A2"),
        AdaptiveVariant(
            "experiment1",
            "A2 + late_risk_destroy",
            enable_late_risk_destroy=True,
        ),
        AdaptiveVariant(
            "experiment2",
            "A2 + rejection_pool",
            enable_rejection_pool=True,
        ),
        AdaptiveVariant(
            "experiment3",
            "A2 + soft_deadline",
            enable_soft_deadline=True,
        ),
        AdaptiveVariant(
            "experiment4",
            "A2 + late_risk_destroy + rejection_pool + soft_deadline",
            enable_late_risk_destroy=True,
            enable_rejection_pool=True,
            enable_soft_deadline=True,
        ),
        AdaptiveVariant(
            "experiment5",
            "A2 + late_risk_destroy + deferred sacrifice pool",
            enable_late_risk_destroy=True,
            enable_rejection_pool=True,
            defer_rejected_tasks=True,
            enable_on_time_distance_objective=True,
        ),
    )


def rotate_variants(
    variants: Sequence[AdaptiveVariant], seed_offset: int
) -> tuple[AdaptiveVariant, ...]:
    if not variants:
        return ()
    shift = seed_offset % len(variants)
    return tuple(variants[shift:]) + tuple(variants[:shift])


def _candidate_limit(args: argparse.Namespace) -> int | None:
    return None if args.candidate_limit == 0 else args.candidate_limit


def config_for_variant(
    args: argparse.Namespace,
    variant: AdaptiveVariant,
    *,
    seed: int,
    max_iterations: int,
    time_limit_seconds: float | None,
) -> ALNSConfig:
    """Materialize every A2 flag so defaults cannot blur the comparison."""

    return ALNSConfig(
        max_iterations=max_iterations,
        time_limit_seconds=time_limit_seconds,
        seed=seed,
        candidate_limit=_candidate_limit(args),
        enable_assignment_destroy=True,
        enable_deadline_risk=True,
        enable_late_risk_destroy=variant.enable_late_risk_destroy,
        late_risk_lateness_weight=args.late_risk_lateness_weight,
        late_risk_deadline_weight=args.late_risk_deadline_weight,
        late_risk_detour_weight=args.late_risk_detour_weight,
        enable_rejection_pool=variant.enable_rejection_pool,
        defer_rejected_tasks=variant.defer_rejected_tasks,
        rejection_pool_fraction=args.rejection_pool_fraction,
        enable_on_time_distance_objective=(
            variant.enable_on_time_distance_objective
        ),
        enable_soft_deadline=variant.enable_soft_deadline,
        soft_deadline_beta=args.soft_deadline_beta,
        risk_aware_lateness_lambda=args.risk_aware_lateness_lambda,
        enable_vnd=False,
        enable_cluster_repair=False,
        enable_ejection=False,
        enable_route_pool=False,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="A2 adaptive deadline / rejection pool paired experiments"
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--methods",
        nargs="+",
        choices=tuple(variant.name for variant in build_variants()),
        default=[variant.name for variant in build_variants()],
    )
    parser.add_argument("--seed-base", type=int, default=DEFAULT_SEED_BASE)
    parser.add_argument("--equal-seed-count", type=int, default=0)
    parser.add_argument("--equal-iterations", type=int, default=400)
    parser.add_argument("--wall-seed-count", type=int, default=3)
    parser.add_argument("--wall-time-limit", type=float, default=240.0)
    parser.add_argument("--wall-safety-margin", type=float, default=2.0)
    parser.add_argument("--wall-max-iterations", type=int, default=10_000)
    parser.add_argument("--candidate-limit", type=int, default=48)
    parser.add_argument("--drones", type=int, default=8)
    parser.add_argument("--max-tasks", type=int, default=25)
    parser.add_argument("--capacity", type=int, default=2)
    parser.add_argument("--speed-km-per-min", type=float, default=0.9)
    parser.add_argument("--depot-x", type=float, default=0.0)
    parser.add_argument("--depot-y", type=float, default=0.0)
    parser.add_argument("--late-risk-lateness-weight", type=float, default=0.5)
    parser.add_argument("--late-risk-deadline-weight", type=float, default=0.3)
    parser.add_argument("--late-risk-detour-weight", type=float, default=0.2)
    parser.add_argument("--rejection-pool-fraction", type=float, default=0.10)
    parser.add_argument(
        "--soft-deadline-beta",
        type=float,
        choices=(0.15, 0.20, 0.30),
        default=0.20,
    )
    parser.add_argument("--risk-aware-lateness-lambda", type=float, default=1.0)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="严格校验并恢复 output-dir 中的逐运行检查点",
    )
    return parser


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return _parser().parse_args(argv)


@dataclass(frozen=True, slots=True)
class _Scenario:
    name: str
    seed_count: int
    max_iterations: int
    budget_seconds: float | None


RUN_FIELDS = (
    "scenario",
    "method",
    "label",
    "seed",
    "run_order",
    "valid",
    "compliant",
    "budget_seconds",
    "effective_search_time_limit_seconds",
    "late_count",
    "total_lateness_min",
    "distance_km",
    "runtime_seconds",
    "search_runtime_seconds",
    "initial_construction_seconds",
    "iterations",
    "enable_late_risk_destroy",
    "enable_rejection_pool",
    "defer_rejected_tasks",
    "enable_on_time_distance_objective",
    "rejection_attempts",
    "rejection_events",
    "reinserted_task_count",
    "peak_rejected_count",
    "final_rejected_count",
    "enable_soft_deadline",
    "soft_deadline_beta",
    "internal_search_score_enabled",
    "risk_aware_insertion_enabled",
    "risk_aware_lateness_lambda",
    "solution_file",
    "solution_sha256",
    "routes_sha256",
)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _routes_sha256(routes: Sequence[Sequence[int]]) -> str:
    encoded = json.dumps(
        [list(route) for route in routes],
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_csv(
    path: Path,
    rows: Sequence[dict[str, Any]],
    fields: Sequence[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    fieldnames = tuple(fields or sorted({key for row in rows for key in row}))
    if not fieldnames:
        temporary.write_text("", encoding="utf-8")
        temporary.replace(path)
        return
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _source_code_hashes() -> dict[str, str]:
    """Hash every Python source that can materially affect an experiment."""

    package_dir = ROOT / "algorithm" / "src" / "uav_dispatch"
    sources = [Path(__file__).resolve(), *sorted(package_dir.glob("*.py"))]
    return {
        str(path.relative_to(ROOT)): _file_sha256(path)
        for path in sources
    }


def _scenarios(args: argparse.Namespace) -> tuple[_Scenario, ...]:
    wall_label = f"{args.wall_time_limit:g}"
    return (
        _Scenario(
            "equal_iterations",
            args.equal_seed_count,
            args.equal_iterations,
            None,
        ),
        _Scenario(
            f"equal_wall_clock_{wall_label}s",
            args.wall_seed_count,
            args.wall_max_iterations,
            args.wall_time_limit,
        ),
    )


def _effective_search_limit(
    scenario: _Scenario,
    *,
    initial_runtime_seconds: float,
    safety_margin_seconds: float,
) -> float | None:
    if scenario.budget_seconds is None:
        return None
    limit = (
        scenario.budget_seconds
        - initial_runtime_seconds
        - safety_margin_seconds
    )
    if limit <= 0:
        raise ValueError("初始构造与安全余量已耗尽墙钟预算")
    return limit


def _materialized_runs(
    args: argparse.Namespace,
    variants: Sequence[AdaptiveVariant],
    *,
    initial_runtime_seconds: float,
) -> list[dict[str, Any]]:
    """Build the exact configs and run keys covered by a checkpoint."""

    materialized: list[dict[str, Any]] = []
    for scenario in _scenarios(args):
        search_limit = (
            _effective_search_limit(
                scenario,
                initial_runtime_seconds=initial_runtime_seconds,
                safety_margin_seconds=args.wall_safety_margin,
            )
            if scenario.seed_count
            else None
        )
        for seed_offset in range(scenario.seed_count):
            seed = args.seed_base + seed_offset
            for run_order, variant in enumerate(
                rotate_variants(variants, seed_offset), start=1
            ):
                config = config_for_variant(
                    args,
                    variant,
                    seed=seed,
                    max_iterations=scenario.max_iterations,
                    time_limit_seconds=search_limit,
                )
                materialized.append(
                    {
                        "scenario": scenario.name,
                        "method": variant.name,
                        "label": variant.label,
                        "seed": seed,
                        "run_order": run_order,
                        "budget_seconds": scenario.budget_seconds,
                        "config": asdict(config),
                    }
                )
    return materialized


def _checkpoint_signature(
    args: argparse.Namespace,
    *,
    variants: Sequence[AdaptiveVariant],
    input_sha256: str,
    initial_routes: Sequence[Sequence[int]],
    initial_runtime_seconds: float,
    task_count: int,
) -> dict[str, Any]:
    """Describe every input that can change a resumable run."""

    return {
        "schema_version": 1,
        "input": {
            "path": str(args.input.resolve()),
            "sha256": input_sha256,
        },
        "source_code_sha256": _source_code_hashes(),
        "initial_solution": {
            "constructor": "construct_regret_initial",
            "routes": [list(route) for route in initial_routes],
            "routes_sha256": _routes_sha256(initial_routes),
            "runtime_seconds_for_budget": initial_runtime_seconds,
        },
        "methods": [asdict(variant) for variant in variants],
        "seeds": {
            "seed_base": args.seed_base,
            "equal": [
                args.seed_base + offset
                for offset in range(args.equal_seed_count)
            ],
            "wall_clock": [
                args.seed_base + offset
                for offset in range(args.wall_seed_count)
            ],
        },
        "budgets": {
            "equal_iterations": args.equal_iterations,
            "wall_time_limit_seconds": args.wall_time_limit,
            "wall_safety_margin_seconds": args.wall_safety_margin,
            "wall_max_iterations": args.wall_max_iterations,
            "candidate_limit": _candidate_limit(args),
        },
        "problem": {
            "task_count": task_count,
            "drone_count": args.drones,
            "max_tasks_per_drone": args.max_tasks,
            "capacity": args.capacity,
            "speed_km_per_min": args.speed_km_per_min,
            "depot_km": [args.depot_x, args.depot_y],
            "open_routes": True,
        },
        "hyperparameters": {
            "late_risk_lateness_weight": args.late_risk_lateness_weight,
            "late_risk_deadline_weight": args.late_risk_deadline_weight,
            "late_risk_detour_weight": args.late_risk_detour_weight,
            "rejection_pool_fraction": args.rejection_pool_fraction,
            "soft_deadline_beta": args.soft_deadline_beta,
            "risk_aware_lateness_lambda": args.risk_aware_lateness_lambda,
        },
        "materialized_runs": _materialized_runs(
            args,
            variants,
            initial_runtime_seconds=initial_runtime_seconds,
        ),
    }


def _checkpoint_runtime_for_resume(
    checkpoint_path: Path,
    *,
    resume: bool,
    measured_runtime_seconds: float,
) -> float:
    """Reuse the original shared-constructor budget when resuming."""

    if not resume or not checkpoint_path.is_file():
        return measured_runtime_seconds
    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        value = payload["signature"]["initial_solution"][
            "runtime_seconds_for_budget"
        ]
        runtime_seconds = float(value)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("断点缺少合法的初始构造预算") from error
    if runtime_seconds < 0:
        raise RuntimeError("断点包含非法的初始构造预算")
    return runtime_seconds


def _score(row: dict[str, Any]) -> tuple[int, float, float]:
    return (
        int(row["late_count"]),
        float(row["total_lateness_min"]),
        float(row["distance_km"]),
    )


def _summarize(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["scenario"], row["method"]), []).append(row)
    summaries: list[dict[str, Any]] = []
    for (scenario, method), group in sorted(grouped.items()):
        best = min(group, key=_score)
        summaries.append(
            {
                "scenario": scenario,
                "method": method,
                "runs": len(group),
                "valid_rate": sum(bool(row["valid"]) for row in group)
                / len(group),
                "compliant_rate": sum(
                    bool(row["compliant"]) for row in group
                )
                / len(group),
                "mean_late_count": statistics.fmean(
                    float(row["late_count"]) for row in group
                ),
                "mean_total_lateness_min": statistics.fmean(
                    float(row["total_lateness_min"]) for row in group
                ),
                "mean_distance_km": statistics.fmean(
                    float(row["distance_km"]) for row in group
                ),
                "mean_runtime_seconds": statistics.fmean(
                    float(row["runtime_seconds"]) for row in group
                ),
                "mean_iterations": statistics.fmean(
                    float(row["iterations"]) for row in group
                ),
                "best_seed": best["seed"],
                "best_late_count": best["late_count"],
                "best_total_lateness_min": best["total_lateness_min"],
                "best_distance_km": best["distance_km"],
                "max_final_rejected_count": max(
                    int(row["final_rejected_count"]) for row in group
                ),
                "mean_peak_rejected_count": statistics.fmean(
                    float(row["peak_rejected_count"]) for row in group
                ),
                "max_peak_rejected_count": max(
                    int(row["peak_rejected_count"]) for row in group
                ),
            }
        )
    return summaries


def paired_comparisons(
    rows: Iterable[dict[str, Any]], baseline: str = "baseline"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Compare every method with A2 per seed using the official score tuple."""

    indexed = {
        (str(row["scenario"]), str(row["method"]), int(row["seed"])): row
        for row in rows
    }
    scenarios = sorted({key[0] for key in indexed})
    methods = sorted({key[1] for key in indexed if key[1] != baseline})
    summaries: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    for scenario in scenarios:
        baseline_by_seed = {
            seed: row
            for (row_scenario, method, seed), row in indexed.items()
            if row_scenario == scenario and method == baseline
        }
        for method in methods:
            method_by_seed = {
                seed: row
                for (row_scenario, row_method, seed), row in indexed.items()
                if row_scenario == scenario and row_method == method
            }
            common_seeds = sorted(set(baseline_by_seed).intersection(method_by_seed))
            if not common_seeds:
                continue
            wins = losses = ties = 0
            for seed in common_seeds:
                baseline_row = baseline_by_seed[seed]
                method_row = method_by_seed[seed]
                baseline_score = _score(baseline_row)
                method_score = _score(method_row)
                if method_score < baseline_score:
                    outcome = "win"
                    wins += 1
                elif method_score > baseline_score:
                    outcome = "loss"
                    losses += 1
                else:
                    outcome = "tie"
                    ties += 1
                pairs.append(
                    {
                        "scenario": scenario,
                        "baseline": baseline,
                        "method": method,
                        "seed": seed,
                        "outcome": outcome,
                        "baseline_late_count": baseline_score[0],
                        "method_late_count": method_score[0],
                        "late_count_delta": method_score[0] - baseline_score[0],
                        "baseline_total_lateness_min": baseline_score[1],
                        "method_total_lateness_min": method_score[1],
                        "total_lateness_delta_min": (
                            method_score[1] - baseline_score[1]
                        ),
                        "baseline_distance_km": baseline_score[2],
                        "method_distance_km": method_score[2],
                        "distance_delta_km": method_score[2] - baseline_score[2],
                    }
                )
            summaries.append(
                {
                    "scenario": scenario,
                    "baseline": baseline,
                    "method": method,
                    "pairs": len(common_seeds),
                    "wins": wins,
                    "losses": losses,
                    "ties": ties,
                }
            )
    return summaries, pairs


def _checkpoint(
    output_dir: Path,
    *,
    signature: dict[str, Any],
    rows: Sequence[dict[str, Any]],
) -> None:
    """Persist a complete run boundary for safe restart."""

    _write_json(
        output_dir / "adaptive_runs.partial.json",
        {"signature": signature, "rows": rows},
    )
    _write_csv(
        output_dir / "adaptive_runs.partial.csv",
        rows,
        RUN_FIELDS,
    )


def _safe_solution_path(output_dir: Path, relative_text: str) -> Path:
    relative = Path(relative_text)
    if (
        not relative.parts
        or relative.is_absolute()
        or ".." in relative.parts
        or relative.parts[0] != "run_solutions"
    ):
        raise RuntimeError("断点中的解文件相对路径非法")
    candidate = output_dir / relative
    try:
        candidate.resolve().relative_to(output_dir.resolve())
    except ValueError as error:
        raise RuntimeError("断点中的解文件路径越出输出目录") from error
    return candidate


def _validate_checkpoint_csv(
    path: Path,
    rows: Sequence[dict[str, Any]],
) -> None:
    if not path.is_file():
        raise RuntimeError("断点缺少 partial CSV")
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        csv_rows = list(reader)
        if tuple(reader.fieldnames or ()) != RUN_FIELDS:
            raise RuntimeError("断点 partial CSV 字段不一致")
    if len(csv_rows) != len(rows):
        raise RuntimeError("断点 JSON 与 CSV 运行数量不一致")
    for index, (csv_row, json_row) in enumerate(zip(csv_rows, rows)):
        expected = {
            field: "" if json_row.get(field) is None else str(json_row.get(field, ""))
            for field in RUN_FIELDS
        }
        if csv_row != expected:
            raise RuntimeError(f"断点 JSON 与 CSV 第 {index + 1} 行不一致")


def _load_checkpoint(
    checkpoint_path: Path,
    *,
    signature: dict[str, Any],
    output_dir: Path,
    problem: Problem,
) -> list[dict[str, Any]]:
    """Load only independently reproducible, in-plan completed runs."""

    try:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("断点 JSON 无法读取") from error
    if not isinstance(payload, dict) or payload.get("signature") != signature:
        raise RuntimeError("断点签名与当前输入、配置或源码不一致")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("断点缺少运行记录")

    planned = {
        (item["scenario"], item["method"], int(item["seed"])): item
        for item in signature["materialized_runs"]
    }
    seen: set[tuple[str, str, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("断点包含非法运行记录")
        try:
            key = (
                str(row["scenario"]),
                str(row["method"]),
                int(row["seed"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("断点运行键非法") from error
        if key in seen:
            raise RuntimeError(f"断点包含重复运行 {key}")
        seen.add(key)
        plan = planned.get(key)
        if plan is None:
            raise RuntimeError(f"断点包含计划外运行 {key}")
        config = plan["config"]
        if (
            row.get("run_order") != plan["run_order"]
            or row.get("label") != plan["label"]
            or row.get("budget_seconds") != plan["budget_seconds"]
            or row.get("effective_search_time_limit_seconds")
            != config["time_limit_seconds"]
        ):
            raise RuntimeError(f"断点运行 {key} 的计划字段不一致")
        if row.get("valid") is not True or row.get("compliant") is not True:
            raise RuntimeError(f"断点运行 {key} 未通过合法性或时限检查")
        for flag in (
            "enable_late_risk_destroy",
            "enable_rejection_pool",
            "defer_rejected_tasks",
            "enable_on_time_distance_objective",
            "enable_soft_deadline",
        ):
            if row.get(flag) is not config[flag]:
                raise RuntimeError(f"断点运行 {key} 的特性开关不一致")
        if (
            row.get("internal_search_score_enabled")
            is not config["enable_soft_deadline"]
            or row.get("risk_aware_insertion_enabled")
            is not config["enable_soft_deadline"]
        ):
            raise RuntimeError(f"断点运行 {key} 的内部搜索开关不一致")
        try:
            initial_runtime = float(row["initial_construction_seconds"])
            search_runtime = float(row["search_runtime_seconds"])
            total_runtime = float(row["runtime_seconds"])
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(f"断点运行 {key} 的运行时间非法") from error
        if (
            initial_runtime
            != signature["initial_solution"]["runtime_seconds_for_budget"]
            or total_runtime != initial_runtime + search_runtime
            or (
                plan["budget_seconds"] is not None
                and total_runtime > float(plan["budget_seconds"])
            )
        ):
            raise RuntimeError(f"断点运行 {key} 的运行时间或预算不一致")
        if int(row.get("final_rejected_count", -1)) != 0:
            raise RuntimeError(f"断点运行 {key} 留有未插回任务")
        peak_rejected = int(row.get("peak_rejected_count", -1))
        pool_cap = (
            int(len(problem.tasks) * float(config["rejection_pool_fraction"]))
            if config["enable_rejection_pool"]
            else 0
        )
        if peak_rejected < 0 or peak_rejected > pool_cap:
            raise RuntimeError(f"断点运行 {key} 的临时拒绝池越界")

        solution_path = _safe_solution_path(
            output_dir, str(row.get("solution_file", ""))
        )
        if not solution_path.is_file():
            raise RuntimeError(f"断点运行 {key} 缺少完整解文件")
        solution_sha256 = str(row.get("solution_sha256", ""))
        if (
            not solution_sha256
            or _file_sha256(solution_path) != solution_sha256
        ):
            raise RuntimeError(f"断点运行 {key} 的解文件哈希不一致")
        try:
            solution = json.loads(solution_path.read_text(encoding="utf-8"))
            routes = tuple(
                tuple(int(visit) for visit in route["encoded_visits"])
                for route in solution["routes"]
            )
            solution_score = (
                int(solution["score"]["late_count"]),
                float(solution["score"]["total_lateness_min"]),
                float(solution["score"]["distance_km"]),
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            OSError,
            json.JSONDecodeError,
        ) as error:
            raise RuntimeError(f"断点运行 {key} 的完整解无法解析") from error
        independent = evaluate_solution(problem, routes)
        independent_score = (
            independent.score.late_count,
            independent.score.total_lateness_min,
            independent.score.distance_km,
        )
        experiment = solution.get("experiment", {})
        metadata = solution.get("metadata", {})
        if (
            solution.get("valid") is not True
            or not independent.valid
            or solution_score != _score(row)
            or independent_score != _score(row)
            or _routes_sha256(routes) != row.get("routes_sha256")
            or solution.get("runtime_seconds") != row.get("runtime_seconds")
            or solution.get("iterations") != row.get("iterations")
            or experiment.get("scenario") != key[0]
            or experiment.get("method") != key[1]
            or experiment.get("label") != plan["label"]
            or experiment.get("run_order") != plan["run_order"]
            or experiment.get("config") != config
            or int(metadata.get("final_rejected_count", 0)) != 0
            or int(metadata.get("peak_rejected_count", 0))
            != peak_rejected
        ):
            raise RuntimeError(f"断点运行 {key} 与完整解或计划不一致")

    _validate_checkpoint_csv(
        output_dir / "adaptive_runs.partial.csv",
        rows,
    )
    return rows


def _validate_args(args: argparse.Namespace) -> None:
    if min(
        args.equal_seed_count,
        args.equal_iterations,
        args.wall_seed_count,
        args.wall_max_iterations,
    ) < 0:
        raise ValueError("实验 seed 数和迭代预算不能为负")
    if args.wall_seed_count and (
        args.wall_time_limit <= 0
        or args.wall_safety_margin < 0
        or args.wall_safety_margin >= args.wall_time_limit
    ):
        raise ValueError("墙钟预算必须为正且安全余量必须小于总预算")
    if args.candidate_limit != 0 and args.candidate_limit < 4:
        raise ValueError("候选位置上限必须为 0 或至少 4")
    if len(args.methods) != len(set(args.methods)):
        raise ValueError("实验方法不能重复")


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Run selected paired variants and persist auditable route/CSV/JSON results."""

    _validate_args(args)
    available = {variant.name: variant for variant in build_variants()}
    variants = tuple(available[name] for name in args.methods)
    tasks = load_tasks_csv(args.input)
    problem = Problem(
        tasks,
        drone_count=args.drones,
        max_tasks_per_drone=args.max_tasks,
        capacity=args.capacity,
        speed_km_per_min=args.speed_km_per_min,
        depot=Point(args.depot_x, args.depot_y),
    )
    initial = construct_regret_initial(problem, candidate_limit=_candidate_limit(args))
    output_dir: Path = args.output_dir
    solution_dir = output_dir / "run_solutions"
    checkpoint_path = output_dir / "adaptive_runs.partial.json"
    initial_runtime = _checkpoint_runtime_for_resume(
        checkpoint_path,
        resume=args.resume,
        measured_runtime_seconds=initial.runtime_seconds,
    )
    signature = _checkpoint_signature(
        args,
        variants=variants,
        input_sha256=_file_sha256(args.input),
        initial_routes=initial.routes,
        initial_runtime_seconds=initial_runtime,
        task_count=len(problem.tasks),
    )
    rows = (
        _load_checkpoint(
            checkpoint_path,
            signature=signature,
            output_dir=output_dir,
            problem=problem,
        )
        if args.resume and checkpoint_path.is_file()
        else []
    )
    completed = {
        (str(row["scenario"]), str(row["method"]), int(row["seed"]))
        for row in rows
    }
    plans = signature["materialized_runs"]
    if rows:
        print(
            f"[resume] 已严格校验 {len(rows)}/{len(plans)} 个已完成运行",
            flush=True,
        )

    for plan in plans:
        key = (plan["scenario"], plan["method"], int(plan["seed"]))
        if key in completed:
            continue
        variant = available[plan["method"]]
        config = ALNSConfig(**plan["config"])
        result = solve_alns_core(
            problem,
            config=config,
            initial_routes=initial.routes,
        )
        evaluation = evaluate_solution(problem, result.routes)
        if not evaluation.valid or evaluation.score != result.evaluation.score:
            raise RuntimeError(f"{variant.name} seed {plan['seed']} 的独立校验失败")

        final_rejected_count = int(
            result.metadata.get("final_rejected_count", 0)
        )
        peak_rejected_count = int(result.metadata.get("peak_rejected_count", 0))
        pool_cap = (
            int(len(problem.tasks) * config.rejection_pool_fraction)
            if config.enable_rejection_pool
            else 0
        )
        if final_rejected_count != 0:
            raise RuntimeError(
                f"{variant.name} seed {plan['seed']} 最终仍有拒绝任务"
            )
        if peak_rejected_count < 0 or peak_rejected_count > pool_cap:
            raise RuntimeError(
                f"{variant.name} seed {plan['seed']} 的临时拒绝池越界"
            )

        total_runtime = initial_runtime + result.runtime_seconds
        budget_seconds = plan["budget_seconds"]
        compliant = (
            True
            if budget_seconds is None
            else total_runtime <= float(budget_seconds)
        )
        if not compliant:
            raise RuntimeError(
                f"{variant.name} seed {plan['seed']} 总运行时间 "
                f"{total_runtime:.6f}s 超过预算 {budget_seconds}s"
            )

        rendered_result = SolverResult(
            routes=result.routes,
            evaluation=evaluation,
            runtime_seconds=total_runtime,
            iterations=result.iterations,
            metadata=result.metadata,
        )
        solution_path = (
            solution_dir
            / f"{plan['scenario']}__{variant.name}__seed_{plan['seed']}.json"
        )
        solution_payload = result_payload(
            problem, rendered_result, source=args.input
        )
        solution_payload["experiment"] = {
            "scenario": plan["scenario"],
            "method": variant.name,
            "label": variant.label,
            "run_order": plan["run_order"],
            "config": plan["config"],
        }
        _write_json(solution_path, solution_payload)
        relative_solution = str(solution_path.relative_to(output_dir))
        row = {
            "scenario": plan["scenario"],
            "method": variant.name,
            "label": variant.label,
            "seed": plan["seed"],
            "run_order": plan["run_order"],
            "valid": evaluation.valid,
            "compliant": compliant,
            "budget_seconds": budget_seconds,
            "effective_search_time_limit_seconds": config.time_limit_seconds,
            "late_count": evaluation.score.late_count,
            "total_lateness_min": evaluation.score.total_lateness_min,
            "distance_km": evaluation.score.distance_km,
            "runtime_seconds": total_runtime,
            "search_runtime_seconds": result.runtime_seconds,
            "initial_construction_seconds": initial_runtime,
            "iterations": result.iterations,
            "enable_late_risk_destroy": config.enable_late_risk_destroy,
            "enable_rejection_pool": config.enable_rejection_pool,
            "defer_rejected_tasks": config.defer_rejected_tasks,
            "enable_on_time_distance_objective": (
                config.enable_on_time_distance_objective
            ),
            "rejection_attempts": result.metadata.get("rejection_attempts", 0),
            "rejection_events": result.metadata.get("rejection_events", 0),
            "reinserted_task_count": result.metadata.get(
                "reinserted_task_count", 0
            ),
            "peak_rejected_count": peak_rejected_count,
            "final_rejected_count": final_rejected_count,
            "enable_soft_deadline": config.enable_soft_deadline,
            "soft_deadline_beta": config.soft_deadline_beta,
            "internal_search_score_enabled": result.metadata.get(
                "internal_search_score_enabled", False
            ),
            "risk_aware_insertion_enabled": result.metadata.get(
                "risk_aware_insertion_enabled", False
            ),
            "risk_aware_lateness_lambda": config.risk_aware_lateness_lambda,
            "solution_file": relative_solution,
            "solution_sha256": _file_sha256(solution_path),
            "routes_sha256": _routes_sha256(result.routes),
        }
        rows.append(row)
        completed.add(key)
        _checkpoint(output_dir, signature=signature, rows=rows)
        print(
            f"[{len(rows)}/{len(plans)}] {plan['scenario']} "
            f"{variant.name} seed={plan['seed']} score={_score(row)} "
            f"runtime={total_runtime:.3f}s iterations={result.iterations}",
            flush=True,
        )
        gc.collect()

    summaries = _summarize(rows)
    comparison_summaries, comparison_pairs = paired_comparisons(rows)
    _write_csv(output_dir / "adaptive_runs.csv", rows, RUN_FIELDS)
    _write_csv(output_dir / "adaptive_summary.csv", summaries)
    _write_csv(
        output_dir / "paired_comparisons.csv",
        comparison_summaries,
    )
    _write_csv(
        output_dir / "paired_comparison_pairs.csv",
        comparison_pairs,
    )
    payload = {
        "manifest": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "objective_order": [
                "late_count",
                "total_lateness_min",
                "distance_km",
            ],
            "checkpoint_signature": signature,
            "resume_requested": args.resume,
        },
        "runs": rows,
        "summaries": summaries,
        "paired_comparisons": comparison_summaries,
        "paired_comparison_pairs": comparison_pairs,
    }
    _write_json(output_dir / "adaptive_results.json", payload)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    run(_parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
