"""Reproduce the exact, single-drone, and 200-task fleet experiments."""

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
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from uav_dispatch import (
    ALNSConfig,
    Problem,
    SolverResult,
    construct_edd_adjacent,
    construct_greedy_initial,
    construct_nearest_adjacent,
    construct_regret_initial,
    load_tasks_csv,
    solve_alns,
    solve_exact,
)
from uav_dispatch.cli import result_payload


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = ROOT / "algorithm" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
DEFAULT_OUTPUT = ROOT / "algorithm" / "results"
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


def _score_tuple(result: SolverResult) -> tuple[int, float, float]:
    score = result.evaluation.score
    return score.late_count, score.total_lateness_min, score.distance_km


def _best_within_budget(
    results: list[SolverResult], budget_seconds: float
) -> SolverResult:
    """Return the lexicographic best run that met the total wall-clock budget."""

    compliant = [
        result for result in results if result.runtime_seconds <= budget_seconds
    ]
    if not compliant:
        raise RuntimeError(
            f"没有运行在 {budget_seconds:.3f} 秒时间预算内完成"
        )
    return min(compliant, key=_score_tuple)


def _with_total_runtime(
    result: SolverResult, initial: SolverResult | None
) -> SolverResult:
    if initial is None:
        return result
    metadata = dict(result.metadata)
    metadata["search_runtime_seconds"] = result.runtime_seconds
    metadata["initial_construction_seconds"] = initial.runtime_seconds
    if "time_to_best_seconds" in metadata:
        metadata["time_to_best_seconds"] += initial.runtime_seconds
    return replace(
        result,
        runtime_seconds=result.runtime_seconds + initial.runtime_seconds,
        metadata=metadata,
    )


def _record(
    rows: list[dict[str, Any]],
    *,
    scenario: str,
    method: str,
    problem: Problem,
    result: SolverResult,
    seed: int | None,
    oracle: SolverResult | None = None,
) -> None:
    evaluation = result.evaluation
    row: dict[str, Any] = {
        "scenario": scenario,
        "method": method,
        "seed": "" if seed is None else seed,
        "task_count": len(problem.tasks),
        "drone_count": problem.drone_count,
        "max_tasks_per_drone": problem.max_tasks_per_drone,
        "valid": evaluation.valid,
        "late_count": evaluation.score.late_count,
        "on_time_rate": evaluation.on_time_rate,
        "total_lateness_min": evaluation.score.total_lateness_min,
        "max_lateness_min": evaluation.max_lateness_min,
        "distance_km": evaluation.score.distance_km,
        "max_route_distance_km": evaluation.max_route_distance_km,
        "runtime_seconds": result.runtime_seconds,
        "iterations": result.iterations,
        "time_to_best_seconds": result.metadata.get(
            "time_to_best_seconds", result.runtime_seconds
        ),
        "accepted_solutions": result.metadata.get("accepted_solutions", ""),
        "route_pool_columns": result.metadata.get("route_pool_columns", ""),
    }
    if oracle is not None:
        oracle_score = oracle.evaluation.score
        row.update(
            {
                "oracle_late_count": oracle_score.late_count,
                "oracle_total_lateness_min": oracle_score.total_lateness_min,
                "oracle_distance_km": oracle_score.distance_km,
                "late_count_gap": evaluation.score.late_count
                - oracle_score.late_count,
                "lateness_gap_min": evaluation.score.total_lateness_min
                - oracle_score.total_lateness_min,
                "distance_gap_percent": (
                    100
                    * (evaluation.score.distance_km - oracle_score.distance_km)
                    / oracle_score.distance_km
                    if evaluation.score.late_count == oracle_score.late_count
                    and math.isclose(
                        evaluation.score.total_lateness_min,
                        oracle_score.total_lateness_min,
                        abs_tol=1e-9,
                    )
                    else ""
                ),
            }
        )
    rows.append(row)
    print(
        f"{scenario:12s} {method:24s} seed={str(seed):>10s} "
        f"score={_score_tuple(result)} runtime={result.runtime_seconds:.3f}s",
        flush=True,
    )


def _ci_half_width(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    critical = T_CRITICAL_95.get(len(values) - 1, 1.96)
    return critical * statistics.stdev(values) / math.sqrt(len(values))


def _summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["scenario"], row["method"])].append(row)
    summaries = []
    numeric_fields = (
        "late_count",
        "on_time_rate",
        "total_lateness_min",
        "max_lateness_min",
        "distance_km",
        "max_route_distance_km",
        "runtime_seconds",
        "time_to_best_seconds",
    )
    for (scenario, method), group in sorted(groups.items()):
        best = min(
            group,
            key=lambda row: (
                row["late_count"],
                row["total_lateness_min"],
                row["distance_km"],
            ),
        )
        summary: dict[str, Any] = {
            "scenario": scenario,
            "method": method,
            "runs": len(group),
            "valid_rate": sum(bool(row["valid"]) for row in group) / len(group),
            "best_late_count": best["late_count"],
            "best_total_lateness_min": best["total_lateness_min"],
            "best_distance_km": best["distance_km"],
        }
        for field in numeric_fields:
            values = [float(row[field]) for row in group]
            summary[f"mean_{field}"] = statistics.fmean(values)
            summary[f"sd_{field}"] = statistics.stdev(values) if len(values) > 1 else 0.0
            summary[f"ci95_half_width_{field}"] = _ci_half_width(values)
        summaries.append(summary)
    return summaries


def _paired_lex_counts(rows: list[dict[str, Any]], scenario: str) -> dict[str, int]:
    paired: dict[int, dict[str, tuple[int, float, float]]] = defaultdict(dict)
    for row in rows:
        if row["scenario"] != scenario or row["seed"] == "":
            continue
        if row["method"] not in {"C2-Lex-ALNS", "C2-Lex-HALNS"}:
            continue
        paired[int(row["seed"])][row["method"]] = (
            int(row["late_count"]),
            float(row["total_lateness_min"]),
            float(row["distance_km"]),
        )
    counts = {"halns_win": 0, "tie": 0, "halns_loss": 0}
    for methods in paired.values():
        if len(methods) != 2:
            continue
        full = methods["C2-Lex-HALNS"]
        basic = methods["C2-Lex-ALNS"]
        if full < basic:
            counts["halns_win"] += 1
        elif full > basic:
            counts["halns_loss"] += 1
        else:
            counts["tie"] += 1
    return counts


def _data_audit(tasks, source: Path) -> dict[str, Any]:
    service_points = []
    for task in tasks:
        service_points.append((f"P{task.id}", task.pickup))
        service_points.append((f"D{task.id}", task.delivery))
    pair_count = 0
    below_one = 0
    minimum = (float("inf"), "", "")
    maximum = (0.0, "", "")
    for left in range(len(service_points)):
        for right in range(left + 1, len(service_points)):
            pair_count += 1
            distance = service_points[left][1].distance_to(service_points[right][1])
            if distance < 1.0:
                below_one += 1
            if distance < minimum[0]:
                minimum = (distance, service_points[left][0], service_points[right][0])
            if distance > maximum[0]:
                maximum = (distance, service_points[left][0], service_points[right][0])
    problem = Problem(tuple(tasks), drone_count=8, max_tasks_per_drone=25)
    slacks = [
        task.deadline_min - problem.direct_completion_min(task.id) for task in tasks
    ]
    direct_distances = [task.pickup.distance_to(task.delivery) for task in tasks]
    return {
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "task_count": len(tasks),
        "deadline_min": min(task.deadline_min for task in tasks),
        "deadline_mean": statistics.fmean(task.deadline_min for task in tasks),
        "deadline_median": statistics.median(task.deadline_min for task in tasks),
        "deadline_max": max(task.deadline_min for task in tasks),
        "service_node_pair_count": pair_count,
        "service_node_pairs_below_1km": below_one,
        "service_node_pairs_below_1km_rate": below_one / pair_count,
        "minimum_service_pair_km": minimum[0],
        "minimum_service_pair": [minimum[1], minimum[2]],
        "maximum_service_pair_km": maximum[0],
        "maximum_service_pair": [maximum[1], maximum[2]],
        "direct_task_distance_min_km": min(direct_distances),
        "direct_task_distance_mean_km": statistics.fmean(direct_distances),
        "direct_task_distance_max_km": max(direct_distances),
        "direct_tasks_below_1km": [
            task.id
            for task, distance in zip(tasks, direct_distances)
            if distance < 1.0
        ],
        "minimum_solo_slack_min_at_depot_0_0": min(slacks),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run(args: argparse.Namespace) -> None:
    for name, value in (
        ("--single-time-limit", args.single_time_limit),
        ("--fleet-time-limit", args.fleet_time_limit),
    ):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} 必须是有限正数")

    tasks = load_tasks_csv(args.input)
    seeds = [args.seed_base + offset for offset in range(args.seed_count)]
    exact_sizes = [5, 8] if args.quick else [5, 8, 10, 12]
    fleet_iterations = 20 if args.quick else args.fleet_iterations
    single_iterations = 50 if args.quick else args.single_iterations
    extended_iterations = 0 if args.quick else args.extended_iterations
    rows: list[dict[str, Any]] = []

    for size in exact_sizes:
        scenario = f"exact_n{size}"
        problem = Problem(tasks[:size], drone_count=1, max_tasks_per_drone=size)
        oracle = solve_exact(problem, max_tasks=max(exact_sizes))
        _record(
            rows,
            scenario=scenario,
            method="Pareto-DP",
            problem=problem,
            result=oracle,
            seed=None,
            oracle=oracle,
        )
        initial = construct_regret_initial(problem, candidate_limit=None)
        _record(
            rows,
            scenario=scenario,
            method="Regret-2",
            problem=problem,
            result=initial,
            seed=None,
            oracle=oracle,
        )
        heuristic = solve_alns(
            problem,
            config=ALNSConfig(
                max_iterations=1000,
                seed=args.seed_base,
                candidate_limit=None,
                enable_route_pool=True,
                enable_ejection=True,
                enable_assignment_destroy=False,
                enable_deadline_risk=False,
                enable_vnd=False,
                enable_cluster_repair=False,
                route_pool_interval=50,
                ejection_interval=25,
            ),
            initial_routes=initial.routes,
        )
        heuristic = _with_total_runtime(heuristic, initial)
        _record(
            rows,
            scenario=scenario,
            method="C2-Lex-HALNS",
            problem=problem,
            result=heuristic,
            seed=args.seed_base,
            oracle=oracle,
        )

    def run_scale(
        scenario: str,
        problem: Problem,
        iterations: int,
        time_limit_seconds: float,
    ) -> tuple[SolverResult, list[SolverResult]]:
        deterministic: tuple[
            tuple[str, Callable[[], SolverResult]], ...
        ] = (
            ("EDD-adjacent", lambda: construct_edd_adjacent(problem)),
            ("Nearest-adjacent", lambda: construct_nearest_adjacent(problem)),
            (
                "Greedy-full-position",
                lambda: construct_greedy_initial(
                    problem, candidate_limit=args.candidate_limit
                ),
            ),
        )
        for method, builder in deterministic:
            _record(
                rows,
                scenario=scenario,
                method=method,
                problem=problem,
                result=builder(),
                seed=None,
            )
        initial = construct_regret_initial(
            problem, candidate_limit=args.candidate_limit
        )
        _record(
            rows,
            scenario=scenario,
            method="Regret-2",
            problem=problem,
            result=initial,
            seed=None,
        )
        remaining_search_seconds = max(
            1e-9, time_limit_seconds - initial.runtime_seconds
        )
        full_results = []
        for seed in seeds:
            for method, full in (
                ("C2-Lex-ALNS", False),
                ("C2-Lex-HALNS", True),
            ):
                result = solve_alns(
                    problem,
                    config=ALNSConfig(
                        max_iterations=iterations,
                        time_limit_seconds=remaining_search_seconds,
                        seed=seed,
                        candidate_limit=args.candidate_limit,
                        enable_route_pool=full,
                        enable_ejection=full,
                        enable_assignment_destroy=False,
                        enable_deadline_risk=False,
                        enable_vnd=False,
                        enable_cluster_repair=False,
                        route_pool_interval=50,
                        ejection_interval=25,
                    ),
                    initial_routes=initial.routes,
                )
                result = _with_total_runtime(result, initial)
                _record(
                    rows,
                    scenario=scenario,
                    method=method,
                    problem=problem,
                    result=result,
                    seed=seed,
                )
                if full:
                    full_results.append(result)
                gc.collect()
        return initial, full_results

    single_problem = Problem(tasks[:25], drone_count=1, max_tasks_per_drone=25)
    run_scale(
        "single_n25",
        single_problem,
        single_iterations,
        args.single_time_limit,
    )

    fleet_problem = Problem(tasks, drone_count=8, max_tasks_per_drone=25)
    fleet_initial, full_results = run_scale(
        "fleet_n200",
        fleet_problem,
        fleet_iterations,
        args.fleet_time_limit,
    )
    best_compliant = _best_within_budget(
        full_results, args.fleet_time_limit
    )
    if extended_iterations:
        extended = solve_alns(
            fleet_problem,
            config=ALNSConfig(
                max_iterations=extended_iterations,
                seed=args.seed_base,
                candidate_limit=args.candidate_limit,
                enable_route_pool=True,
                enable_ejection=True,
                enable_assignment_destroy=False,
                enable_deadline_risk=False,
                enable_vnd=False,
                enable_cluster_repair=False,
                route_pool_interval=50,
                ejection_interval=25,
            ),
            initial_routes=fleet_initial.routes,
        )
        extended = _with_total_runtime(extended, fleet_initial)
        full_results.append(extended)
        _record(
            rows,
            scenario="fleet_n200",
            method="C2-Lex-HALNS-extended",
            problem=fleet_problem,
            result=extended,
            seed=args.seed_base,
        )

    best_fleet = min(full_results, key=_score_tuple)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summaries = _summaries(rows)
    paired = {
        scenario: _paired_lex_counts(rows, scenario)
        for scenario in ("single_n25", "fleet_n200")
    }
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "input": str(args.input),
        "assumptions": {
            "depot_km": [0.0, 0.0],
            "speed_km_per_min": 0.9,
            "capacity": 2,
            "open_routes": True,
            "service_time_min": 0.0,
            "deadlines_are_soft": True,
        },
        "settings": {
            "seed_base": args.seed_base,
            "seed_count": args.seed_count,
            "seeds": seeds,
            "candidate_limit": args.candidate_limit,
            "single_iterations": single_iterations,
            "single_time_limit_seconds": args.single_time_limit,
            "fleet_iterations": fleet_iterations,
            "fleet_time_limit_seconds": args.fleet_time_limit,
            "extended_iterations": extended_iterations,
        },
        "data_audit": _data_audit(tasks, args.input),
        "paired_lexicographic_comparison": paired,
    }
    raw = {
        "manifest": manifest,
        "runs": rows,
        "summaries": summaries,
    }
    (args.output_dir / "benchmark_results.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_csv(args.output_dir / "benchmark_runs.csv", rows)
    _write_csv(args.output_dir / "benchmark_summary.csv", summaries)
    (args.output_dir / "best_fleet_solution.json").write_text(
        json.dumps(
            result_payload(fleet_problem, best_fleet, source=args.input),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "best_fleet_solution_compliant.json").write_text(
        json.dumps(
            result_payload(
                fleet_problem,
                best_compliant,
                source=args.input,
            ),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed-base", type=int, default=2026080500)
    parser.add_argument("--seed-count", type=int, default=5)
    parser.add_argument("--candidate-limit", type=int, default=48)
    parser.add_argument("--single-iterations", type=int, default=1500)
    parser.add_argument("--single-time-limit", type=float, default=120.0)
    parser.add_argument("--fleet-iterations", type=int, default=400)
    parser.add_argument("--fleet-time-limit", type=float, default=240.0)
    parser.add_argument("--extended-iterations", type=int, default=1500)
    parser.add_argument("--quick", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    run(_parse_args())
