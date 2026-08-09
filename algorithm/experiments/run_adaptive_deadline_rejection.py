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
    enable_soft_deadline: bool = False


def build_variants() -> tuple[AdaptiveVariant, ...]:
    """Return the Phase 2 variants; later phases extend this same registry."""

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
        rejection_pool_fraction=args.rejection_pool_fraction,
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
    return parser


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return _parser().parse_args(argv)


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
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], fields: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


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
            }
        )
    return summaries


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
    rows: list[dict[str, Any]] = []

    scenarios = (
        (
            "equal_iterations",
            args.equal_seed_count,
            args.equal_iterations,
            None,
        ),
        (
            "equal_wall_clock_240s",
            args.wall_seed_count,
            args.wall_max_iterations,
            args.wall_time_limit,
        ),
    )
    for scenario, seed_count, max_iterations, total_time_limit in scenarios:
        for seed_offset in range(seed_count):
            seed = args.seed_base + seed_offset
            for run_order, variant in enumerate(
                rotate_variants(variants, seed_offset), start=1
            ):
                if total_time_limit is None:
                    search_time_limit = None
                else:
                    search_time_limit = (
                        total_time_limit
                        - initial.runtime_seconds
                        - args.wall_safety_margin
                    )
                    if search_time_limit <= 0:
                        raise ValueError("初始构造与安全余量已耗尽墙钟预算")
                config = config_for_variant(
                    args,
                    variant,
                    seed=seed,
                    max_iterations=max_iterations,
                    time_limit_seconds=search_time_limit,
                )
                result = solve_alns_core(
                    problem,
                    config=config,
                    initial_routes=initial.routes,
                )
                evaluation = evaluate_solution(problem, result.routes)
                if not evaluation.valid or evaluation.score != result.evaluation.score:
                    raise RuntimeError(
                        f"{variant.name} seed {seed} 的独立校验失败"
                    )
                total_runtime = initial.runtime_seconds + result.runtime_seconds
                rendered_result = SolverResult(
                    routes=result.routes,
                    evaluation=evaluation,
                    runtime_seconds=total_runtime,
                    iterations=result.iterations,
                    metadata=result.metadata,
                )
                solution_path = (
                    solution_dir
                    / f"{scenario}__{variant.name}__seed_{seed}.json"
                )
                solution_payload = result_payload(
                    problem, rendered_result, source=args.input
                )
                solution_payload["experiment"] = {
                    "scenario": scenario,
                    "method": variant.name,
                    "label": variant.label,
                    "run_order": run_order,
                    "config": asdict(config),
                }
                _write_json(solution_path, solution_payload)
                rows.append(
                    {
                        "scenario": scenario,
                        "method": variant.name,
                        "label": variant.label,
                        "seed": seed,
                        "run_order": run_order,
                        "valid": evaluation.valid,
                        "late_count": evaluation.score.late_count,
                        "total_lateness_min": evaluation.score.total_lateness_min,
                        "distance_km": evaluation.score.distance_km,
                        "runtime_seconds": total_runtime,
                        "search_runtime_seconds": result.runtime_seconds,
                        "iterations": result.iterations,
                        "enable_late_risk_destroy": (
                            config.enable_late_risk_destroy
                        ),
                        "enable_rejection_pool": config.enable_rejection_pool,
                        "rejection_attempts": result.metadata.get(
                            "rejection_attempts", 0
                        ),
                        "rejection_events": result.metadata.get(
                            "rejection_events", 0
                        ),
                        "reinserted_task_count": result.metadata.get(
                            "reinserted_task_count", 0
                        ),
                        "peak_rejected_count": result.metadata.get(
                            "peak_rejected_count", 0
                        ),
                        "final_rejected_count": result.metadata.get(
                            "final_rejected_count", 0
                        ),
                        "enable_soft_deadline": config.enable_soft_deadline,
                        "soft_deadline_beta": config.soft_deadline_beta,
                        "internal_search_score_enabled": result.metadata.get(
                            "internal_search_score_enabled", False
                        ),
                        "risk_aware_insertion_enabled": result.metadata.get(
                            "risk_aware_insertion_enabled", False
                        ),
                        "risk_aware_lateness_lambda": (
                            config.risk_aware_lateness_lambda
                        ),
                        "solution_file": str(solution_path.relative_to(output_dir)),
                        "routes_sha256": _routes_sha256(result.routes),
                    }
                )
                gc.collect()

    summaries = _summarize(rows)
    run_fields = (
        "scenario",
        "method",
        "label",
        "seed",
        "run_order",
        "valid",
        "late_count",
        "total_lateness_min",
        "distance_km",
        "runtime_seconds",
        "search_runtime_seconds",
        "iterations",
        "enable_late_risk_destroy",
        "enable_rejection_pool",
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
        "routes_sha256",
    )
    summary_fields = (
        "scenario",
        "method",
        "runs",
        "mean_late_count",
        "mean_total_lateness_min",
        "mean_distance_km",
        "mean_runtime_seconds",
        "mean_iterations",
        "best_seed",
        "best_late_count",
        "best_total_lateness_min",
        "best_distance_km",
    )
    _write_csv(output_dir / "adaptive_runs.csv", rows, run_fields)
    _write_csv(output_dir / "adaptive_summary.csv", summaries, summary_fields)
    payload = {
        "manifest": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "input": str(args.input.resolve()),
            "input_sha256": _file_sha256(args.input),
            "initial_routes_sha256": _routes_sha256(initial.routes),
            "objective_order": [
                "late_count",
                "total_lateness_min",
                "distance_km",
            ],
            "methods": [asdict(variant) for variant in variants],
            "seed_base": args.seed_base,
            "equal_seed_count": args.equal_seed_count,
            "equal_iterations": args.equal_iterations,
            "wall_seed_count": args.wall_seed_count,
            "wall_time_limit_seconds": args.wall_time_limit,
            "wall_safety_margin_seconds": args.wall_safety_margin,
        },
        "runs": rows,
        "summaries": summaries,
    }
    _write_json(output_dir / "adaptive_results.json", payload)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    run(_parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
