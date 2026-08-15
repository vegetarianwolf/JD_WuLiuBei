"""Run paired feature-ladder experiments for problem-specific ALNS neighborhoods.

The default experiment reproduces the historical seed budgets: five seeds at
400 iterations and three seeds at a 240-second total wall-clock budget.  Every
variant starts from the same deterministic regret-construction solution.
The assignment step replaces legacy route-clear so the destroy-pool size stays
fixed; later steps are cumulative.
"""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import json
import math
import platform
import statistics
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import uav_dispatch.alns as alns_module
from uav_dispatch import (
    ALNSConfig,
    Problem,
    SolverResult,
    construct_regret_initial,
    evaluate_solution,
    load_tasks_csv,
    solve_alns,
)
from uav_dispatch.cli import result_payload


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    ROOT / "algorithm" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)
DEFAULT_OUTPUT = ROOT / "algorithm" / "results" / "neighborhood_ablation"
DEFAULT_SEED_BASE = 2026080500
EXPERIMENT_SOURCE = Path(__file__).resolve()
ALNS_SOURCE = Path(alns_module.__file__).resolve()
FEATURE_FLAGS = (
    "enable_assignment_destroy",
    "enable_deadline_risk",
    "enable_vnd",
    "enable_cluster_repair",
    "enable_ejection",
    "enable_route_pool",
)
T_CRITICAL_95 = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
}


@dataclass(frozen=True, slots=True)
class AblationVariant:
    """One feature-ladder level and its explicit ALNS feature flags."""

    code: str
    name: str
    label: str
    enable_assignment_destroy: bool = False
    enable_deadline_risk: bool = False
    enable_vnd: bool = False
    enable_cluster_repair: bool = False
    enable_ejection: bool = False
    enable_route_pool: bool = False

    @property
    def method(self) -> str:
        return f"{self.code} {self.label}"

    def flags(self) -> dict[str, bool]:
        return {field: bool(getattr(self, field)) for field in FEATURE_FLAGS}


def build_variants(
    include_hybrid_tuning: bool = False,
) -> tuple[AblationVariant, ...]:
    """Return the A0--A4 feature ladder, optionally followed by A5--A6."""

    variants = (
        AblationVariant("A0", "baseline_core", "baseline_core"),
        AblationVariant(
            "A1",
            "assignment",
            "+assignment",
            enable_assignment_destroy=True,
        ),
        AblationVariant(
            "A2",
            "risk",
            "+risk",
            enable_assignment_destroy=True,
            enable_deadline_risk=True,
        ),
        AblationVariant(
            "A3",
            "vnd",
            "+VND",
            enable_assignment_destroy=True,
            enable_deadline_risk=True,
            enable_vnd=True,
        ),
        AblationVariant(
            "A4",
            "cluster",
            "+cluster",
            enable_assignment_destroy=True,
            enable_deadline_risk=True,
            enable_vnd=True,
            enable_cluster_repair=True,
        ),
    )
    if not include_hybrid_tuning:
        return variants
    return variants + (
        AblationVariant(
            "A5",
            "ejection",
            "+ejection",
            enable_assignment_destroy=True,
            enable_deadline_risk=True,
            enable_vnd=True,
            enable_cluster_repair=True,
            enable_ejection=True,
        ),
        AblationVariant(
            "A6",
            "route_pool",
            "+route_pool",
            enable_assignment_destroy=True,
            enable_deadline_risk=True,
            enable_vnd=True,
            enable_cluster_repair=True,
            enable_ejection=True,
            enable_route_pool=True,
        ),
    )


def rotate_variants(
    variants: Sequence[AblationVariant], seed_offset: int
) -> tuple[AblationVariant, ...]:
    """Cycle run order by seed to reduce systematic warm-up/order bias."""

    if not variants:
        return ()
    shift = seed_offset % len(variants)
    return tuple(variants[shift:]) + tuple(variants[:shift])


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _routes_sha256(routes: Sequence[Sequence[int]]) -> str:
    encoded = json.dumps(
        [list(route) for route in routes],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_code_hashes() -> dict[str, dict[str, str]]:
    """Fingerprint both implementations that determine experiment results."""

    return {
        "experiment_script": {
            "path": str(EXPERIMENT_SOURCE),
            "sha256": _file_sha256(EXPERIMENT_SOURCE),
        },
        "alns_solver": {
            "path": str(ALNS_SOURCE.resolve()),
            "sha256": _file_sha256(ALNS_SOURCE),
        },
    }


def _candidate_limit(args: argparse.Namespace) -> int | None:
    return None if args.candidate_limit == 0 else args.candidate_limit


def _solver_budget_settings(args: argparse.Namespace) -> dict[str, Any]:
    """Return every configurable search budget used to build ALNSConfig."""

    return {
        "candidate_limit": _candidate_limit(args),
        "min_destroy_fraction": 0.04,
        "max_destroy_fraction": 0.10,
        "weight_update_interval": 40,
        "reaction_factor": 0.2,
        "minimum_weight": 0.05,
        "initial_temperature": 0.03,
        "minimum_temperature": 0.0005,
        "cooling_rate": 0.995,
        "vnd_max_moves": args.vnd_max_moves,
        "vnd_task_limit": args.vnd_task_limit,
        "vnd_swap_pair_limit": args.vnd_swap_pair_limit,
        "vnd_block_window_limit": args.vnd_block_window_limit,
        "cluster_bundle_candidate_limit": args.cluster_bundle_candidate_limit,
        "cluster_pair_limit": args.cluster_pair_limit,
        "ejection_interval": args.ejection_interval,
        "ejection_trials": args.ejection_trials,
        "route_pool_interval": args.route_pool_interval,
        "route_pool_node_limit": args.route_pool_node_limit,
    }


def config_for_variant(
    args: argparse.Namespace,
    variant: AblationVariant,
    *,
    seed: int,
    max_iterations: int,
    time_limit_seconds: float | None,
) -> ALNSConfig:
    """Build an explicit config so changing ALNS defaults cannot blur an ablation."""

    return ALNSConfig(
        max_iterations=max_iterations,
        time_limit_seconds=time_limit_seconds,
        seed=seed,
        **_solver_budget_settings(args),
        **variant.flags(),
    )


def _checkpoint_signature(
    args: argparse.Namespace,
    *,
    input_sha256: str,
    variants: Sequence[AblationVariant],
    initial_routes_sha256: str,
    source_code_hashes: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Describe inputs, flags and budgets that affect resumable runs."""

    return {
        "schema_version": 2,
        "input": str(args.input.resolve()),
        "input_sha256": input_sha256,
        "source_code": (
            _source_code_hashes()
            if source_code_hashes is None
            else source_code_hashes
        ),
        "initial": {
            "constructor": "construct_regret_initial",
            "routes_sha256": initial_routes_sha256,
        },
        "seeds": {
            "seed_base": args.seed_base,
            "equal_seed_count": args.equal_seed_count,
            "wall_seed_count": args.wall_seed_count,
        },
        "budgets": {
            "equal_iterations": args.equal_iterations,
            "wall_time_limit_seconds": args.wall_time_limit,
            "wall_safety_margin_seconds": args.wall_safety_margin,
            "wall_max_iterations": args.wall_max_iterations,
            **_solver_budget_settings(args),
        },
        "variants": [
            {
                "code": variant.code,
                "name": variant.name,
                "label": variant.label,
                "flags": variant.flags(),
            }
            for variant in variants
        ],
        "problem": {
            "drone_count": 8,
            "max_tasks_per_drone": 25,
            "capacity": 2,
            "speed_km_per_min": 0.9,
            "depot_km": [0.0, 0.0],
            "open_routes": True,
        },
    }


def _score(row: dict[str, Any]) -> tuple[int, float, float]:
    return (
        int(row["late_count"]),
        float(row["total_lateness_min"]),
        float(row["distance_km"]),
    )


def _with_initial_runtime(
    result: SolverResult, initial: SolverResult
) -> SolverResult:
    metadata = dict(result.metadata)
    metadata["search_runtime_seconds"] = result.runtime_seconds
    metadata["initial_construction_seconds"] = initial.runtime_seconds
    if "time_to_best_seconds" in metadata:
        metadata["time_to_best_seconds"] = (
            float(metadata["time_to_best_seconds"]) + initial.runtime_seconds
        )
    return replace(
        result,
        runtime_seconds=result.runtime_seconds + initial.runtime_seconds,
        metadata=metadata,
    )


def _row(
    *,
    scenario: str,
    variant: AblationVariant,
    seed: int,
    run_order: int,
    result: SolverResult,
    budget_seconds: float | None,
) -> dict[str, Any]:
    evaluation = result.evaluation
    search_runtime = float(
        result.metadata.get("search_runtime_seconds", result.runtime_seconds)
    )
    return {
        "scenario": scenario,
        "variant": variant.code,
        "variant_name": variant.name,
        "method": variant.method,
        "seed": seed,
        "run_order": run_order,
        **variant.flags(),
        "valid": evaluation.valid,
        "compliant": (
            True
            if budget_seconds is None
            else result.runtime_seconds <= budget_seconds
        ),
        "budget_seconds": "" if budget_seconds is None else budget_seconds,
        "late_count": evaluation.score.late_count,
        "on_time_rate": evaluation.on_time_rate,
        "total_lateness_min": evaluation.score.total_lateness_min,
        "max_lateness_min": evaluation.max_lateness_min,
        "distance_km": evaluation.score.distance_km,
        "max_route_distance_km": evaluation.max_route_distance_km,
        "runtime_seconds": result.runtime_seconds,
        "search_runtime_seconds": search_runtime,
        "initial_construction_seconds": result.metadata.get(
            "initial_construction_seconds", ""
        ),
        "iterations": result.iterations,
        "iterations_per_search_second": (
            result.iterations / search_runtime if search_runtime > 0 else 0.0
        ),
        "time_to_best_seconds": result.metadata.get(
            "time_to_best_seconds", result.runtime_seconds
        ),
        "accepted_solutions": result.metadata.get("accepted_solutions", ""),
        "vnd_calls": result.metadata.get("vnd_calls", ""),
        "vnd_improved_iterations": result.metadata.get(
            "vnd_improved_iterations", ""
        ),
        "route_pool_columns": result.metadata.get("route_pool_columns", ""),
    }


def _ci_half_width(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    critical = T_CRITICAL_95.get(len(values) - 1, 1.96)
    return critical * statistics.stdev(values) / math.sqrt(len(values))


def summarize_runs(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["scenario"]), str(row["variant"]))].append(row)

    numeric_fields = (
        "late_count",
        "on_time_rate",
        "total_lateness_min",
        "distance_km",
        "runtime_seconds",
        "search_runtime_seconds",
        "iterations",
        "iterations_per_search_second",
        "time_to_best_seconds",
    )
    summaries: list[dict[str, Any]] = []
    for (scenario, variant), group in sorted(grouped.items()):
        best = min(group, key=_score)
        summary: dict[str, Any] = {
            "scenario": scenario,
            "variant": variant,
            "variant_name": group[0]["variant_name"],
            "method": group[0]["method"],
            "runs": len(group),
            "valid_rate": sum(bool(row["valid"]) for row in group) / len(group),
            "compliant_rate": (
                sum(bool(row["compliant"]) for row in group) / len(group)
            ),
            "best_seed": best["seed"],
            "best_late_count": best["late_count"],
            "best_total_lateness_min": best["total_lateness_min"],
            "best_distance_km": best["distance_km"],
            "best_solution_file": best.get("solution_file", ""),
        }
        for field in numeric_fields:
            values = [
                float(row[field])
                for row in group
                if row.get(field, "") != ""
            ]
            if not values:
                continue
            summary[f"mean_{field}"] = statistics.fmean(values)
            summary[f"median_{field}"] = statistics.median(values)
            summary[f"sd_{field}"] = (
                statistics.stdev(values) if len(values) > 1 else 0.0
            )
            summary[f"ci95_half_width_{field}"] = _ci_half_width(values)
            summary[f"min_{field}"] = min(values)
            summary[f"max_{field}"] = max(values)
        summaries.append(summary)
    return summaries


def paired_lexicographic_comparison(
    rows: Iterable[dict[str, Any]],
    *,
    scenario: str,
    left_variant: str,
    right_variant: str,
    comparison_type: str,
) -> dict[str, Any]:
    """Compare two variants on matched seeds using only the final objective."""

    by_seed: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        if row["scenario"] != scenario:
            continue
        variant = str(row["variant"])
        if variant not in {left_variant, right_variant}:
            continue
        by_seed.setdefault(int(row["seed"]), {})[variant] = row

    counts = {"left_win": 0, "tie": 0, "right_win": 0}
    pairs: list[dict[str, Any]] = []
    for seed, variants in sorted(by_seed.items()):
        if left_variant not in variants or right_variant not in variants:
            continue
        left = variants[left_variant]
        right = variants[right_variant]
        left_score = _score(left)
        right_score = _score(right)
        if left_score < right_score:
            winner = left_variant
            counts["left_win"] += 1
        elif right_score < left_score:
            winner = right_variant
            counts["right_win"] += 1
        else:
            winner = "tie"
            counts["tie"] += 1
        pairs.append(
            {
                "seed": seed,
                "winner": winner,
                "left_score": list(left_score),
                "right_score": list(right_score),
                "late_count_delta_right_minus_left": (
                    right_score[0] - left_score[0]
                ),
                "total_lateness_delta_right_minus_left": (
                    right_score[1] - left_score[1]
                ),
                "distance_delta_right_minus_left": (
                    right_score[2] - left_score[2]
                ),
                "runtime_delta_right_minus_left": (
                    float(right["runtime_seconds"])
                    - float(left["runtime_seconds"])
                ),
                "iterations_per_search_second_delta_right_minus_left": (
                    float(right["iterations_per_search_second"])
                    - float(left["iterations_per_search_second"])
                ),
                "time_to_best_delta_right_minus_left": (
                    float(right["time_to_best_seconds"])
                    - float(left["time_to_best_seconds"])
                ),
            }
        )

    return {
        "comparison_id": f"{left_variant}_vs_{right_variant}",
        "comparison_type": comparison_type,
        "scenario": scenario,
        "left_variant": left_variant,
        "right_variant": right_variant,
        **counts,
        "paired_count": len(pairs),
        "pairs": pairs,
    }


def build_paired_comparisons(
    rows: Sequence[dict[str, Any]],
    variants: Sequence[AblationVariant],
    scenarios: Sequence[str],
) -> list[dict[str, Any]]:
    comparisons: list[dict[str, Any]] = []
    for scenario in scenarios:
        for left, right in zip(variants, variants[1:]):
            comparisons.append(
                paired_lexicographic_comparison(
                    rows,
                    scenario=scenario,
                    left_variant=left.code,
                    right_variant=right.code,
                    comparison_type="adjacent",
                )
            )
        comparisons.append(
            paired_lexicographic_comparison(
                rows,
                scenario=scenario,
                left_variant="A0",
                right_variant="A4",
                comparison_type="A0_vs_A4",
            )
        )
    return comparisons


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _run_solution_path(
    output_dir: Path, *, scenario: str, variant: str, seed: int
) -> Path:
    return (
        output_dir
        / "run_solutions"
        / f"{scenario}__{variant.lower()}__seed_{seed}.json"
    )


def _write_run_solution(
    output_dir: Path,
    *,
    problem: Problem,
    source: Path,
    scenario: str,
    variant: AblationVariant,
    seed: int,
    run_order: int,
    result: SolverResult,
) -> str:
    path = _run_solution_path(
        output_dir,
        scenario=scenario,
        variant=variant.code,
        seed=seed,
    )
    payload = result_payload(problem, result, source=source)
    payload["experiment"] = {
        "scenario": scenario,
        "variant": variant.code,
        "variant_name": variant.name,
        "method": variant.method,
        "run_order": run_order,
        "flags": variant.flags(),
    }
    _write_json(path, payload)
    return str(path.relative_to(output_dir))


def _checkpoint(
    output_dir: Path,
    rows: Sequence[dict[str, Any]],
    signature: dict[str, Any],
) -> None:
    _write_json(
        output_dir / "ablation_runs.partial.json",
        {"signature": signature, "rows": rows},
    )
    _write_csv(output_dir / "ablation_runs.partial.csv", rows)


def _load_checkpoint(
    path: Path,
    *,
    signature: dict[str, Any],
    output_dir: Path,
    problem: Problem,
    scenarios_and_seeds: dict[str, set[int]],
    variants: Sequence[AblationVariant],
) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("signature") != signature:
        raise RuntimeError("断点配置、特性开关、预算或输入哈希不一致")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("断点缺少运行记录")

    allowed_variants = {variant.code: variant for variant in variants}
    seen: set[tuple[str, str, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("断点包含非法运行记录")
        scenario = str(row.get("scenario", ""))
        variant_code = str(row.get("variant", ""))
        seed = int(row.get("seed", -1))
        key = (scenario, variant_code, seed)
        if key in seen:
            raise RuntimeError(f"断点包含重复运行 {key}")
        seen.add(key)
        if (
            scenario not in scenarios_and_seeds
            or seed not in scenarios_and_seeds[scenario]
            or variant_code not in allowed_variants
        ):
            raise RuntimeError(f"断点包含超出当前实验计划的运行 {key}")
        if row.get("valid") is not True or row.get("compliant") is not True:
            raise RuntimeError(f"断点运行 {key} 未通过合法性或时限检查")
        expected_flags = allowed_variants[variant_code].flags()
        if any(row.get(flag) is not value for flag, value in expected_flags.items()):
            raise RuntimeError(f"断点运行 {key} 的特性开关不一致")

        relative = Path(str(row.get("solution_file", "")))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(f"断点运行 {key} 的解文件路径非法")
        solution_path = output_dir / relative
        if not solution_path.is_file():
            raise RuntimeError(f"断点运行 {key} 缺少完整解文件")
        expected_sha256 = str(row.get("solution_sha256", ""))
        if not expected_sha256 or _file_sha256(solution_path) != expected_sha256:
            raise RuntimeError(f"断点运行 {key} 的解文件哈希不一致")

        solution = json.loads(solution_path.read_text(encoding="utf-8"))
        try:
            routes = tuple(
                tuple(int(visit) for visit in route["encoded_visits"])
                for route in solution["routes"]
            )
            solution_score = (
                int(solution["score"]["late_count"]),
                float(solution["score"]["total_lateness_min"]),
                float(solution["score"]["distance_km"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(
                f"断点运行 {key} 的解文件缺少完整路线或得分"
            ) from error
        independent = evaluate_solution(problem, routes)
        independent_score = (
            independent.score.late_count,
            independent.score.total_lateness_min,
            independent.score.distance_km,
        )
        if (
            solution.get("valid") is not True
            or not independent.valid
            or solution_score != _score(row)
            or independent_score != _score(row)
        ):
            raise RuntimeError(f"断点运行 {key} 与完整解文件不一致")
    return rows


def _comparison_csv_rows(
    comparisons: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    summaries: list[dict[str, Any]] = []
    pairs: list[dict[str, Any]] = []
    for comparison in comparisons:
        summary = {key: value for key, value in comparison.items() if key != "pairs"}
        summaries.append(summary)
        identity = {
            "comparison_id": comparison["comparison_id"],
            "comparison_type": comparison["comparison_type"],
            "scenario": comparison["scenario"],
            "left_variant": comparison["left_variant"],
            "right_variant": comparison["right_variant"],
        }
        for pair in comparison["pairs"]:
            rendered = {**identity, **pair}
            rendered["left_score"] = json.dumps(pair["left_score"])
            rendered["right_score"] = json.dumps(pair["right_score"])
            pairs.append(rendered)
    return summaries, pairs


def _validate_args(args: argparse.Namespace) -> None:
    if args.equal_seed_count < 0 or args.wall_seed_count < 0:
        raise ValueError("随机种子数量不能为负")
    if args.equal_iterations < 0 or args.wall_max_iterations <= 0:
        raise ValueError("迭代次数设置非法")
    if args.candidate_limit != 0 and args.candidate_limit < 4:
        raise ValueError("candidate_limit 必须为 0 或至少为 4")
    if (
        not math.isfinite(args.wall_time_limit)
        or args.wall_time_limit <= 0
        or not math.isfinite(args.wall_safety_margin)
        or args.wall_safety_margin < 0
        or args.wall_safety_margin >= args.wall_time_limit
    ):
        raise ValueError("墙钟预算与安全余量设置非法")
    budget_names = (
        "vnd_max_moves",
        "vnd_task_limit",
        "vnd_swap_pair_limit",
        "vnd_block_window_limit",
        "cluster_bundle_candidate_limit",
        "cluster_pair_limit",
        "ejection_interval",
        "ejection_trials",
        "route_pool_interval",
        "route_pool_node_limit",
    )
    if any(getattr(args, name) < 0 for name in budget_names):
        raise ValueError("VND、聚类修复与混合强化预算不能为负")


def _print_result(row: dict[str, Any]) -> None:
    print(
        f"{row['scenario']:26s} {row['method']:18s} "
        f"seed={row['seed']} order={row['run_order']} "
        f"score=({row['late_count']}, {row['total_lateness_min']:.6f}, "
        f"{row['distance_km']:.6f}) iterations={row['iterations']} "
        f"runtime={row['runtime_seconds']:.3f}s",
        flush=True,
    )


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the configured experiment and return the final JSON payload."""

    _validate_args(args)
    variants = build_variants(args.include_hybrid_tuning)
    tasks = load_tasks_csv(args.input)
    problem = Problem(tasks, drone_count=8, max_tasks_per_drone=25)
    initial = construct_regret_initial(
        problem, candidate_limit=_candidate_limit(args)
    )
    if not initial.evaluation.valid:
        raise RuntimeError("确定性初始解未通过可行性校验")

    equal_scenario = f"equal_iterations_{args.equal_iterations}"
    wall_scenario = f"equal_wall_clock_{args.wall_time_limit:g}s"
    equal_seeds = tuple(
        args.seed_base + offset for offset in range(args.equal_seed_count)
    )
    wall_seeds = tuple(
        args.seed_base + offset for offset in range(args.wall_seed_count)
    )
    scenarios_and_seeds = {
        equal_scenario: set(equal_seeds),
        wall_scenario: set(wall_seeds),
    }
    input_sha256 = _file_sha256(args.input)
    initial_routes_sha256 = _routes_sha256(initial.routes)
    signature = _checkpoint_signature(
        args,
        input_sha256=input_sha256,
        variants=variants,
        initial_routes_sha256=initial_routes_sha256,
    )
    partial_path = args.output_dir / "ablation_runs.partial.json"
    resumed_from_checkpoint = args.resume and partial_path.exists()
    rows = (
        _load_checkpoint(
            partial_path,
            signature=signature,
            output_dir=args.output_dir,
            problem=problem,
            scenarios_and_seeds=scenarios_and_seeds,
            variants=variants,
        )
        if resumed_from_checkpoint
        else []
    )
    completed = {
        (str(row["scenario"]), str(row["variant"]), int(row["seed"]))
        for row in rows
    }

    remaining_search_time = (
        args.wall_time_limit
        - initial.runtime_seconds
        - args.wall_safety_margin
    )
    if wall_seeds and remaining_search_time <= 0:
        raise RuntimeError("初始解构造已耗尽墙钟预算")

    experiment_plan = (
        (
            equal_scenario,
            equal_seeds,
            args.equal_iterations,
            None,
            None,
        ),
        (
            wall_scenario,
            wall_seeds,
            args.wall_max_iterations,
            remaining_search_time,
            args.wall_time_limit,
        ),
    )
    for scenario, seeds, max_iterations, search_limit, total_budget in experiment_plan:
        for seed_offset, seed in enumerate(seeds):
            ordered_variants = rotate_variants(variants, seed_offset)
            for run_order, variant in enumerate(ordered_variants, start=1):
                key = (scenario, variant.code, seed)
                if key in completed:
                    continue
                config = config_for_variant(
                    args,
                    variant,
                    seed=seed,
                    max_iterations=max_iterations,
                    time_limit_seconds=search_limit,
                )
                result = solve_alns(
                    problem,
                    config=config,
                    initial_routes=initial.routes,
                )
                result = _with_initial_runtime(result, initial)
                current_row = _row(
                    scenario=scenario,
                    variant=variant,
                    seed=seed,
                    run_order=run_order,
                    result=result,
                    budget_seconds=total_budget,
                )
                if not current_row["valid"]:
                    raise RuntimeError(
                        f"{scenario} {variant.code} seed={seed} 未产生合法解"
                    )
                if not current_row["compliant"]:
                    raise RuntimeError(
                        f"{scenario} {variant.code} seed={seed} 超出墙钟预算"
                    )
                current_row["solution_file"] = _write_run_solution(
                    args.output_dir,
                    problem=problem,
                    source=args.input,
                    scenario=scenario,
                    variant=variant,
                    seed=seed,
                    run_order=run_order,
                    result=result,
                )
                current_row["solution_sha256"] = _file_sha256(
                    args.output_dir / str(current_row["solution_file"])
                )
                rows.append(current_row)
                completed.add(key)
                _print_result(current_row)
                _checkpoint(args.output_dir, rows, signature)
                gc.collect()

    summaries = summarize_runs(rows)
    active_scenarios = [
        scenario
        for scenario, seeds in (
            (equal_scenario, equal_seeds),
            (wall_scenario, wall_seeds),
        )
        if seeds
    ]
    paired_comparisons = build_paired_comparisons(
        rows, variants, active_scenarios
    )
    comparison_summaries, comparison_pairs = _comparison_csv_rows(
        paired_comparisons
    )
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "input": str(args.input),
        "input_sha256": input_sha256,
        "source_code": signature["source_code"],
        "objective_order": [
            "late_count",
            "total_lateness_min",
            "distance_km",
        ],
        "problem": {
            "task_count": len(tasks),
            "drone_count": problem.drone_count,
            "max_tasks_per_drone": problem.max_tasks_per_drone,
            "capacity": problem.capacity,
            "speed_km_per_min": problem.speed_km_per_min,
            "depot_km": [problem.depot.x, problem.depot.y],
            "open_routes": True,
        },
        "initial": {
            "constructor": "construct_regret_initial",
            "routes_sha256": initial_routes_sha256,
            "score": [
                initial.evaluation.score.late_count,
                initial.evaluation.score.total_lateness_min,
                initial.evaluation.score.distance_km,
            ],
            "runtime_seconds": initial.runtime_seconds,
            "shared_by_every_run": True,
        },
        "variants": [
            {
                **asdict(variant),
                "method": variant.method,
                "flags": variant.flags(),
            }
            for variant in variants
        ],
        "settings": {
            "seed_base": args.seed_base,
            "equal_seed_count": args.equal_seed_count,
            "equal_iterations": args.equal_iterations,
            "wall_seed_count": args.wall_seed_count,
            "wall_time_limit_seconds": args.wall_time_limit,
            "wall_safety_margin_seconds": args.wall_safety_margin,
            "wall_max_iterations": args.wall_max_iterations,
            "effective_wall_search_limit_seconds": remaining_search_time,
            "include_hybrid_tuning": args.include_hybrid_tuning,
            "run_order": "cyclic rotation by seed within each scenario",
            "resumed_from_checkpoint": resumed_from_checkpoint,
            **_solver_budget_settings(args),
        },
        "checkpoint_signature": signature,
    }
    payload = {
        "manifest": manifest,
        "runs": rows,
        "summaries": summaries,
        "paired_comparisons": paired_comparisons,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(args.output_dir / "ablation_results.json", payload)
    _write_csv(args.output_dir / "ablation_runs.csv", rows)
    _write_csv(args.output_dir / "ablation_summary.csv", summaries)
    _write_csv(
        args.output_dir / "paired_comparisons.csv", comparison_summaries
    )
    _write_csv(
        args.output_dir / "paired_comparison_pairs.csv", comparison_pairs
    )

    for partial in (
        args.output_dir / "ablation_runs.partial.json",
        args.output_dir / "ablation_runs.partial.csv",
    ):
        partial.unlink(missing_ok=True)
    return payload


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed-base", type=int, default=DEFAULT_SEED_BASE)
    parser.add_argument("--equal-seed-count", type=int, default=5)
    parser.add_argument("--equal-iterations", type=int, default=400)
    parser.add_argument("--wall-seed-count", type=int, default=3)
    parser.add_argument("--wall-time-limit", type=float, default=240.0)
    parser.add_argument("--wall-safety-margin", type=float, default=2.0)
    parser.add_argument("--wall-max-iterations", type=int, default=10_000)
    parser.add_argument("--candidate-limit", type=int, default=48)
    parser.add_argument("--vnd-max-moves", type=int, default=2)
    parser.add_argument("--vnd-task-limit", type=int, default=12)
    parser.add_argument("--vnd-swap-pair-limit", type=int, default=24)
    parser.add_argument("--vnd-block-window-limit", type=int, default=16)
    parser.add_argument(
        "--cluster-bundle-candidate-limit", type=int, default=12
    )
    parser.add_argument("--cluster-pair-limit", type=int, default=6)
    parser.add_argument("--ejection-interval", type=int, default=25)
    parser.add_argument("--ejection-trials", type=int, default=24)
    parser.add_argument("--route-pool-interval", type=int, default=50)
    parser.add_argument("--route-pool-node-limit", type=int, default=5_000)
    parser.add_argument("--include-hybrid-tuning", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--quick", action="store_true")
    return parser


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = _parser().parse_args(argv)
    if args.quick:
        args.equal_seed_count = 1
        args.equal_iterations = 5
        args.wall_seed_count = 1
        args.wall_time_limit = 10.0
        args.wall_safety_margin = 0.5
        args.wall_max_iterations = 1_000
    return args


if __name__ == "__main__":
    run(_parse_args())
