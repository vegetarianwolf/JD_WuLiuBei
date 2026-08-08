"""Run a matched A2 versus capacity-aware HALNS wall-clock benchmark."""

from __future__ import annotations

import argparse
import csv
import gc
import hashlib
import importlib
import json
import math
import platform
import statistics
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

alns_module = importlib.import_module("uav_dispatch.alns")
hypergraph_module = importlib.import_module("uav_dispatch.hypergraph_destroy")
graph_module = importlib.import_module("uav_dispatch.interaction_graph")
pair_module = importlib.import_module("uav_dispatch.pair_repair")
propagation_module = importlib.import_module("uav_dispatch.propagation_eval")
search_module = importlib.import_module("uav_dispatch.search")
model_module = importlib.import_module("uav_dispatch.model")
validation_module = importlib.import_module("uav_dispatch.validation")
io_module = importlib.import_module("uav_dispatch.io")
cli_module = importlib.import_module("uav_dispatch.cli")
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
DEFAULT_OUTPUT = ROOT / "algorithm" / "results" / "capacity_halns"
DEFAULT_SEED_BASE = 2026080500
EXPERIMENT_SOURCE = Path(__file__).resolve()


@dataclass(frozen=True, slots=True)
class BenchmarkVariant:
    """One explicitly controlled A2-derived benchmark configuration."""

    code: str
    label: str
    enable_assignment_destroy: bool = True
    enable_deadline_risk: bool = True
    enable_vnd: bool = False
    enable_cluster_repair: bool = False
    enable_ejection: bool = False
    enable_route_pool: bool = False
    enable_pair_repair: bool = False
    enable_hypergraph_destroy: bool = False

    def flags(self) -> dict[str, bool]:
        return {
            name: bool(getattr(self, name))
            for name in (
                "enable_assignment_destroy",
                "enable_deadline_risk",
                "enable_vnd",
                "enable_cluster_repair",
                "enable_ejection",
                "enable_route_pool",
                "enable_pair_repair",
                "enable_hypergraph_destroy",
            )
        }


def build_variants() -> tuple[BenchmarkVariant, BenchmarkVariant]:
    """Return the A2 control and the two-operator capacity-aware treatment."""

    return (
        BenchmarkVariant("a2", "Current best A2 configuration"),
        BenchmarkVariant(
            "capacity_halns",
            "Capacity-aware HALNS",
            enable_pair_repair=True,
            enable_hypergraph_destroy=True,
        ),
    )


def rotate_variants(
    variants: Sequence[BenchmarkVariant],
    seed_offset: int,
) -> tuple[BenchmarkVariant, ...]:
    """Rotate paired run order by seed to reduce systematic order effects."""

    if not variants:
        return ()
    shift = seed_offset % len(variants)
    return tuple(variants[shift:]) + tuple(variants[:shift])


def config_for_variant(
    args: argparse.Namespace,
    variant: BenchmarkVariant,
    *,
    seed: int,
    search_time_limit: float,
) -> ALNSConfig:
    """Build a fully matched config whose only treatment is the new operators."""

    candidate_limit = None if args.candidate_limit == 0 else args.candidate_limit
    return ALNSConfig(
        max_iterations=args.max_iterations,
        time_limit_seconds=search_time_limit,
        seed=seed,
        candidate_limit=candidate_limit,
        min_destroy_fraction=0.04,
        max_destroy_fraction=0.10,
        weight_update_interval=40,
        reaction_factor=0.2,
        minimum_weight=0.05,
        initial_temperature=0.03,
        minimum_temperature=0.0005,
        cooling_rate=0.995,
        enable_assignment_destroy=variant.enable_assignment_destroy,
        enable_deadline_risk=variant.enable_deadline_risk,
        enable_vnd=variant.enable_vnd,
        enable_cluster_repair=variant.enable_cluster_repair,
        enable_ejection=variant.enable_ejection,
        enable_route_pool=variant.enable_route_pool,
        enable_pair_repair=variant.enable_pair_repair,
        pair_candidate_limit=args.pair_candidate_limit,
        enable_hypergraph_destroy=variant.enable_hypergraph_destroy,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed-base", type=int, default=DEFAULT_SEED_BASE)
    parser.add_argument("--seed-count", type=int, default=3)
    parser.add_argument("--time-limit", type=float, default=240.0)
    parser.add_argument("--safety-margin", type=float, default=2.0)
    parser.add_argument("--max-iterations", type=int, default=10_000)
    parser.add_argument("--candidate-limit", type=int, default=48)
    parser.add_argument("--pair-candidate-limit", type=int, default=8)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--quick", action="store_true")
    return parser


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = _parser().parse_args(argv)
    if args.quick:
        args.seed_count = 1
        args.time_limit = 10.0
        args.safety_margin = 0.5
        args.max_iterations = 1_000
    return args


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
    sources = {
        "experiment_script": EXPERIMENT_SOURCE,
        "alns_solver": Path(alns_module.__file__).resolve(),
        "interaction_graph": Path(graph_module.__file__).resolve(),
        "pair_repair": Path(pair_module.__file__).resolve(),
        "propagation_eval": Path(propagation_module.__file__).resolve(),
        "hypergraph_destroy": Path(hypergraph_module.__file__).resolve(),
        "search": Path(search_module.__file__).resolve(),
        "model": Path(model_module.__file__).resolve(),
        "validation": Path(validation_module.__file__).resolve(),
        "io": Path(io_module.__file__).resolve(),
        "cli_serialization": Path(cli_module.__file__).resolve(),
    }
    return {
        name: {"path": str(path), "sha256": _file_sha256(path)}
        for name, path in sources.items()
    }


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    key: (
                        json.dumps(value, ensure_ascii=False, sort_keys=True)
                        if isinstance(value, (dict, list, tuple))
                        else value
                    )
                    for key, value in row.items()
                }
            )


def _validate_args(args: argparse.Namespace) -> None:
    if args.seed_count <= 0:
        raise ValueError("seed_count 必须为正整数")
    if args.max_iterations <= 0:
        raise ValueError("max_iterations 必须为正整数")
    if args.candidate_limit != 0 and args.candidate_limit < 4:
        raise ValueError("candidate_limit 必须为 0 或至少为 4")
    if args.pair_candidate_limit < 0:
        raise ValueError("pair_candidate_limit 不能为负")
    if (
        not math.isfinite(args.time_limit)
        or args.time_limit <= 0
        or not math.isfinite(args.safety_margin)
        or args.safety_margin < 0
        or args.safety_margin >= args.time_limit
    ):
        raise ValueError("墙钟预算与安全余量设置非法")


def _score(row: dict[str, Any]) -> tuple[int, float, float]:
    return (
        int(row["late_count"]),
        float(row["total_lateness_min"]),
        float(row["distance_km"]),
    )


def _with_initial_runtime(
    result: SolverResult,
    initial_runtime_seconds: float,
) -> SolverResult:
    metadata = dict(result.metadata)
    metadata["search_runtime_seconds"] = result.runtime_seconds
    metadata["initial_construction_seconds"] = initial_runtime_seconds
    if "time_to_best_seconds" in metadata:
        metadata["time_to_best_seconds"] = (
            float(metadata["time_to_best_seconds"]) + initial_runtime_seconds
        )
    return replace(
        result,
        runtime_seconds=result.runtime_seconds + initial_runtime_seconds,
        metadata=metadata,
    )


def _row_for_result(
    *,
    scenario: str,
    variant: BenchmarkVariant,
    seed: int,
    run_order: int,
    result: SolverResult,
    budget_seconds: float,
) -> dict[str, Any]:
    evaluation = result.evaluation
    search_runtime = float(result.metadata["search_runtime_seconds"])
    return {
        "scenario": scenario,
        "variant": variant.code,
        "variant_label": variant.label,
        "seed": seed,
        "run_order": run_order,
        **variant.flags(),
        "valid": evaluation.valid,
        "compliant": result.runtime_seconds <= budget_seconds,
        "budget_seconds": budget_seconds,
        "late_count": evaluation.score.late_count,
        "total_lateness_min": evaluation.score.total_lateness_min,
        "distance_km": evaluation.score.distance_km,
        "runtime_seconds": result.runtime_seconds,
        "search_runtime_seconds": search_runtime,
        "initial_construction_seconds": result.metadata[
            "initial_construction_seconds"
        ],
        "iterations": result.iterations,
        "iterations_per_search_second": (
            result.iterations / search_runtime if search_runtime > 0 else 0.0
        ),
        "time_to_best_seconds": result.metadata.get(
            "time_to_best_seconds", result.runtime_seconds
        ),
        "accepted_solutions": result.metadata.get("accepted_solutions", 0),
        "operator_uses": dict(result.metadata.get("operator_uses", {})),
        "operator_weights": dict(result.metadata.get("operator_weights", {})),
    }


def summarize_runs(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(str(row["variant"]), []).append(row)
    summaries: list[dict[str, Any]] = []
    for variant, group in sorted(groups.items()):
        best = min(group, key=_score)
        summary: dict[str, Any] = {
            "variant": variant,
            "variant_label": group[0]["variant_label"],
            "runs": len(group),
            "valid_rate": sum(bool(row["valid"]) for row in group) / len(group),
            "compliant_rate": sum(bool(row["compliant"]) for row in group)
            / len(group),
            "best_seed": best["seed"],
            "best_late_count": best["late_count"],
            "best_total_lateness_min": best["total_lateness_min"],
            "best_distance_km": best["distance_km"],
            "best_solution_file": best["solution_file"],
        }
        for field in (
            "late_count",
            "total_lateness_min",
            "distance_km",
            "runtime_seconds",
            "iterations",
        ):
            values = [float(row[field]) for row in group]
            summary[f"mean_{field}"] = statistics.fmean(values)
            summary[f"median_{field}"] = statistics.median(values)
            summary[f"min_{field}"] = min(values)
            summary[f"max_{field}"] = max(values)
        summaries.append(summary)
    return summaries


def paired_lexicographic_comparison(
    rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    by_seed: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        by_seed.setdefault(int(row["seed"]), {})[str(row["variant"])] = row
    counts = {"a2_win": 0, "tie": 0, "capacity_halns_win": 0}
    pairs: list[dict[str, Any]] = []
    for seed, variants in sorted(by_seed.items()):
        if set(variants) != {"a2", "capacity_halns"}:
            continue
        baseline = variants["a2"]
        treatment = variants["capacity_halns"]
        baseline_score = _score(baseline)
        treatment_score = _score(treatment)
        if baseline_score < treatment_score:
            winner = "a2"
            counts["a2_win"] += 1
        elif treatment_score < baseline_score:
            winner = "capacity_halns"
            counts["capacity_halns_win"] += 1
        else:
            winner = "tie"
            counts["tie"] += 1
        pairs.append(
            {
                "seed": seed,
                "winner": winner,
                "a2_score": list(baseline_score),
                "capacity_halns_score": list(treatment_score),
                "late_count_delta_new_minus_a2": (
                    treatment_score[0] - baseline_score[0]
                ),
                "total_lateness_delta_new_minus_a2": (
                    treatment_score[1] - baseline_score[1]
                ),
                "distance_delta_new_minus_a2": (
                    treatment_score[2] - baseline_score[2]
                ),
                "runtime_delta_new_minus_a2": (
                    float(treatment["runtime_seconds"])
                    - float(baseline["runtime_seconds"])
                ),
                "iterations_delta_new_minus_a2": (
                    int(treatment["iterations"])
                    - int(baseline["iterations"])
                ),
            }
        )
    return {
        **counts,
        "paired_count": len(pairs),
        "comparison_rule": [
            "late_count",
            "total_lateness_min",
            "distance_km",
        ],
        "pairs": pairs,
    }


def _format_budget(seconds: float) -> str:
    return f"{seconds:g}s"


def _solution_path(
    output_dir: Path,
    *,
    scenario: str,
    variant: str,
    seed: int,
) -> Path:
    return output_dir / "run_solutions" / f"{scenario}__{variant}__seed_{seed}.json"


def _write_solution(
    output_dir: Path,
    *,
    problem: Problem,
    source: Path,
    scenario: str,
    variant: BenchmarkVariant,
    seed: int,
    run_order: int,
    result: SolverResult,
) -> str:
    path = _solution_path(
        output_dir,
        scenario=scenario,
        variant=variant.code,
        seed=seed,
    )
    payload = result_payload(problem, result, source=source)
    payload["experiment"] = {
        "scenario": scenario,
        "variant": variant.code,
        "variant_label": variant.label,
        "run_order": run_order,
        "flags": variant.flags(),
    }
    _write_json(path, payload)
    return str(path.relative_to(output_dir))


def _signature(
    args: argparse.Namespace,
    *,
    input_sha256: str,
    initial_routes_sha256: str,
    variants: Sequence[BenchmarkVariant],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "input": str(args.input.resolve()),
        "input_sha256": input_sha256,
        "initial_routes_sha256": initial_routes_sha256,
        "source_code": _source_code_hashes(),
        "seed_base": args.seed_base,
        "seed_count": args.seed_count,
        "time_limit_seconds": args.time_limit,
        "safety_margin_seconds": args.safety_margin,
        "max_iterations": args.max_iterations,
        "candidate_limit": args.candidate_limit,
        "pair_candidate_limit": args.pair_candidate_limit,
        "variants": [
            {**asdict(variant), "flags": variant.flags()}
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


def _write_checkpoint(
    output_dir: Path,
    *,
    signature: dict[str, Any],
    initial_runtime_seconds: float,
    search_time_limit_seconds: float,
    rows: Sequence[dict[str, Any]],
) -> None:
    _write_json(
        output_dir / "capacity_halns_runs.partial.json",
        {
            "signature": signature,
            "initial_runtime_seconds": initial_runtime_seconds,
            "search_time_limit_seconds": search_time_limit_seconds,
            "rows": rows,
        },
    )
    _write_csv(output_dir / "capacity_halns_runs.partial.csv", rows)


def _load_checkpoint(
    path: Path,
    *,
    signature: dict[str, Any],
    output_dir: Path,
) -> tuple[list[dict[str, Any]], float, float]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("signature") != signature:
        raise RuntimeError("断点的输入、代码、随机种子、预算或特性开关不匹配")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("断点缺少运行记录")
    seen: set[tuple[str, int]] = set()
    for row in rows:
        key = (str(row.get("variant")), int(row.get("seed", -1)))
        if key in seen:
            raise RuntimeError(f"断点包含重复运行 {key}")
        seen.add(key)
        if row.get("valid") is not True or row.get("compliant") is not True:
            raise RuntimeError(f"断点运行 {key} 未通过合法性或时限检查")
        solution = output_dir / str(row.get("solution_file", ""))
        if not solution.is_file() or _file_sha256(solution) != row.get(
            "solution_sha256"
        ):
            raise RuntimeError(f"断点运行 {key} 缺少可验证的完整解")
    return (
        rows,
        float(payload["initial_runtime_seconds"]),
        float(payload["search_time_limit_seconds"]),
    )


def _report_markdown(payload: dict[str, Any]) -> str:
    manifest = payload["manifest"]
    rows = sorted(payload["runs"], key=lambda row: (row["seed"], row["run_order"]))
    summaries = {row["variant"]: row for row in payload["summaries"]}
    paired = payload["paired_comparison"]
    lines = [
        "# Capacity-Aware HALNS Benchmark",
        "",
        "## Protocol",
        "",
        (
            f"Fresh matched runs on `{manifest['input']}` with "
            f"{manifest['problem']['task_count']} tasks, 8 UAVs, capacity 2, "
            f"seeds {manifest['settings']['seeds']}, and a "
            f"{manifest['settings']['time_limit_seconds']:.0f}-second total "
            "wall-clock budget per run. Every run shares the same regret-2 "
            "initial solution. A2 enables assignment destroy and deadline risk; "
            "capacity-aware HALNS changes only pair regret repair and "
            "hypergraph destroy."
        ),
        "",
        "Results are compared strictly as `(late tasks, total lateness, distance)`; no weighted score is used.",
        "",
        "## Matched runs",
        "",
        "| Seed | Variant | Late tasks | Total lateness (min) | Distance (km) | Runtime (s) | Iterations |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['seed']} | {row['variant']} | {row['late_count']} | "
            f"{row['total_lateness_min']:.6f} | {row['distance_km']:.6f} | "
            f"{row['runtime_seconds']:.3f} | {row['iterations']} |"
        )
    lines.extend(
        [
            "",
            "## Aggregate comparison",
            "",
            "| Variant | Mean late tasks | Mean lateness (min) | Mean distance (km) | Mean runtime (s) | Mean iterations | Best lexicographic score |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for variant in ("a2", "capacity_halns"):
        row = summaries[variant]
        lines.append(
            f"| {variant} | {row['mean_late_count']:.3f} | "
            f"{row['mean_total_lateness_min']:.6f} | "
            f"{row['mean_distance_km']:.6f} | "
            f"{row['mean_runtime_seconds']:.3f} | "
            f"{row['mean_iterations']:.1f} | "
            f"({row['best_late_count']}, {row['best_total_lateness_min']:.6f}, "
            f"{row['best_distance_km']:.6f}) |"
        )
    lines.extend(
        [
            "",
            "## Paired lexicographic outcome",
            "",
            (
                f"Capacity-aware HALNS wins {paired['capacity_halns_win']} seed(s), "
                f"A2 wins {paired['a2_win']}, and {paired['tie']} tie(s)."
            ),
            "",
            "| Seed | Winner | Δ late tasks | Δ lateness (min) | Δ distance (km) | Δ runtime (s) | Δ iterations |",
            "|---:|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in paired["pairs"]:
        lines.append(
            f"| {row['seed']} | {row['winner']} | "
            f"{row['late_count_delta_new_minus_a2']} | "
            f"{row['total_lateness_delta_new_minus_a2']:.6f} | "
            f"{row['distance_delta_new_minus_a2']:.6f} | "
            f"{row['runtime_delta_new_minus_a2']:.3f} | "
            f"{row['iterations_delta_new_minus_a2']} |"
        )
    lines.extend(
        [
            "",
            "Deltas are capacity-aware HALNS minus A2. Means are descriptive only; paired winners use the strict objective tuple.",
            "",
        ]
    )
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Execute the matched benchmark and persist auditable, non-overwriting results."""

    _validate_args(args)
    final_path = args.output_dir / "capacity_halns_results.json"
    partial_path = args.output_dir / "capacity_halns_runs.partial.json"
    if final_path.exists():
        raise FileExistsError(f"拒绝覆盖已完成的基准结果: {final_path}")
    existing = list(args.output_dir.iterdir()) if args.output_dir.exists() else []
    if existing and not args.resume:
        raise FileExistsError(f"拒绝覆盖非空结果目录: {args.output_dir}")
    if args.resume and not partial_path.is_file():
        raise FileNotFoundError("--resume 需要匹配的 partial checkpoint")

    tasks = load_tasks_csv(args.input)
    problem = Problem(
        tasks,
        drone_count=8,
        max_tasks_per_drone=25,
        capacity=2,
        speed_km_per_min=0.9,
    )
    initial_candidate_limit = (
        None if args.candidate_limit == 0 else args.candidate_limit
    )
    initial = construct_regret_initial(
        problem,
        candidate_limit=initial_candidate_limit,
    )
    variants = build_variants()
    input_sha256 = _file_sha256(args.input)
    initial_routes_sha256 = _routes_sha256(initial.routes)
    signature = _signature(
        args,
        input_sha256=input_sha256,
        initial_routes_sha256=initial_routes_sha256,
        variants=variants,
    )

    if args.resume:
        rows, initial_runtime, search_time_limit = _load_checkpoint(
            partial_path,
            signature=signature,
            output_dir=args.output_dir,
        )
    else:
        rows = []
        initial_runtime = initial.runtime_seconds
        search_time_limit = (
            args.time_limit - initial_runtime - args.safety_margin
        )
        if search_time_limit <= 0:
            raise RuntimeError("初始解构造耗尽了墙钟预算")
        args.output_dir.mkdir(parents=True, exist_ok=True)
        _write_checkpoint(
            args.output_dir,
            signature=signature,
            initial_runtime_seconds=initial_runtime,
            search_time_limit_seconds=search_time_limit,
            rows=rows,
        )

    seeds = [args.seed_base + offset for offset in range(args.seed_count)]
    scenario = f"equal_wall_clock_{_format_budget(args.time_limit)}"
    completed = {
        (str(row["variant"]), int(row["seed"])) for row in rows
    }
    for seed_offset, seed in enumerate(seeds):
        for run_order, variant in enumerate(
            rotate_variants(variants, seed_offset),
            start=1,
        ):
            key = (variant.code, seed)
            if key in completed:
                continue
            config = config_for_variant(
                args,
                variant,
                seed=seed,
                search_time_limit=search_time_limit,
            )
            # Every independent run starts with a cold task-pair cache.  Any
            # graph construction therefore consumes that run's search budget.
            graph_module.clear_interaction_graph_cache()
            search_result = solve_alns(
                problem,
                config=config,
                initial_routes=initial.routes,
            )
            result = _with_initial_runtime(search_result, initial_runtime)
            independent = evaluate_solution(problem, result.routes)
            if not independent.valid or independent.score != result.evaluation.score:
                raise RuntimeError(f"{variant.code} seed {seed} 独立校验失败")
            row = _row_for_result(
                scenario=scenario,
                variant=variant,
                seed=seed,
                run_order=run_order,
                result=result,
                budget_seconds=args.time_limit,
            )
            if not row["valid"] or not row["compliant"]:
                raise RuntimeError(f"{variant.code} seed {seed} 非法或超时")
            row["solution_file"] = _write_solution(
                args.output_dir,
                problem=problem,
                source=args.input,
                scenario=scenario,
                variant=variant,
                seed=seed,
                run_order=run_order,
                result=result,
            )
            row["solution_sha256"] = _file_sha256(
                args.output_dir / row["solution_file"]
            )
            rows.append(row)
            completed.add(key)
            _write_checkpoint(
                args.output_dir,
                signature=signature,
                initial_runtime_seconds=initial_runtime,
                search_time_limit_seconds=search_time_limit,
                rows=rows,
            )
            print(
                f"{variant.code} seed={seed} score={_score(row)} "
                f"runtime={row['runtime_seconds']:.3f}s "
                f"iterations={row['iterations']}",
                flush=True,
            )
            gc.collect()

    summaries = summarize_runs(rows)
    paired = paired_lexicographic_comparison(rows)
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
            "task_count": len(problem.tasks),
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
            "runtime_seconds": initial_runtime,
            "shared_by_every_run": True,
        },
        "variants": [
            {**asdict(variant), "flags": variant.flags()}
            for variant in variants
        ],
        "settings": {
            "seeds": seeds,
            "seed_base": args.seed_base,
            "seed_count": args.seed_count,
            "time_limit_seconds": args.time_limit,
            "safety_margin_seconds": args.safety_margin,
            "effective_search_time_limit_seconds": search_time_limit,
            "max_iterations": args.max_iterations,
            "candidate_limit": args.candidate_limit,
            "pair_candidate_limit": args.pair_candidate_limit,
            "run_order": "two-variant cyclic rotation by seed",
            "interaction_cache": (
                "cleared before every run; graph construction is inside the "
                "search budget"
            ),
            "resumed_from_checkpoint": args.resume,
        },
        "checkpoint_signature": signature,
    }
    payload = {
        "manifest": manifest,
        "runs": rows,
        "summaries": summaries,
        "paired_comparison": paired,
    }
    _write_csv(args.output_dir / "capacity_halns_runs.csv", rows)
    _write_csv(args.output_dir / "capacity_halns_summary.csv", summaries)
    _write_csv(args.output_dir / "paired_comparisons.csv", paired["pairs"])
    (args.output_dir / "BENCHMARK_REPORT.md").write_text(
        _report_markdown(payload),
        encoding="utf-8",
    )
    # The final marker is deliberately last so --resume can recover from a
    # failure while producing any human-readable artifact above.
    _write_json(final_path, payload)
    for partial in (
        partial_path,
        args.output_dir / "capacity_halns_runs.partial.csv",
    ):
        partial.unlink(missing_ok=True)
    return payload


if __name__ == "__main__":
    run(_parse_args())
