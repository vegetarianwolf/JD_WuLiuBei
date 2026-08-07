"""Run paired ALNS-Core versus HALNS experiments on the official instance."""

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
from typing import Any, Iterable

from uav_dispatch import (
    ALNSConfig,
    Problem,
    SolverResult,
    construct_regret_initial,
    evaluate_solution,
    load_tasks_csv,
    solve_alns,
    solve_alns_core,
)
from uav_dispatch.cli import result_payload


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = (
    ROOT / "algorithm" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)
DEFAULT_PREVIOUS_RESULTS = ROOT / "algorithm" / "results" / "benchmark_results.json"
DEFAULT_OUTPUT = ROOT / "algorithm" / "results" / "alns_core_comparison"
CORE_METHOD = "C2-Lex-ALNS-Core"
HALNS_METHOD = "C2-Lex-HALNS"
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


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checkpoint_signature(
    args: argparse.Namespace,
    *,
    input_sha256: str,
    previous_results_sha256: str,
) -> dict[str, Any]:
    """Describe every input that can change a checkpointed experiment."""

    return {
        "schema_version": 1,
        "input": str(args.input.resolve()),
        "input_sha256": input_sha256,
        "previous_results": str(args.previous_results.resolve()),
        "previous_results_sha256": previous_results_sha256,
        "seed_base": args.seed_base,
        "equal_seed_count": args.equal_seed_count,
        "equal_iterations": args.equal_iterations,
        "wall_seed_count": args.wall_seed_count,
        "wall_time_limit": args.wall_time_limit,
        "wall_safety_margin": args.wall_safety_margin,
        "wall_max_iterations": args.wall_max_iterations,
        "candidate_limit": args.candidate_limit,
        "problem": {
            "drone_count": 8,
            "max_tasks_per_drone": 25,
            "capacity": 2,
            "speed_km_per_min": 0.9,
            "depot_km": [0.0, 0.0],
            "open_routes": True,
        },
    }


def _validate_previous_results(
    previous: dict[str, Any],
    *,
    input_sha256: str,
    candidate_limit: int,
) -> None:
    """Reject historical baselines that do not describe this experiment."""

    try:
        manifest = previous["manifest"]
        source_sha256 = manifest["data_audit"]["source_sha256"]
        assumptions = manifest["assumptions"]
        settings = manifest["settings"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("历史结果缺少必要的 manifest 来源信息") from error
    if source_sha256 != input_sha256:
        raise RuntimeError("历史结果与当前输入数据哈希不一致")
    expected_assumptions = {
        "depot_km": [0.0, 0.0],
        "speed_km_per_min": 0.9,
        "capacity": 2,
        "open_routes": True,
        "service_time_min": 0.0,
        "deadlines_are_soft": True,
    }
    for key, expected in expected_assumptions.items():
        if assumptions.get(key) != expected:
            raise RuntimeError(f"历史结果的建模假设 {key} 与当前实验不一致")
    if int(settings.get("candidate_limit", -1)) != candidate_limit:
        raise RuntimeError("历史结果的 candidate_limit 与当前实验不一致")


def _score(row: dict[str, Any]) -> tuple[int, float, float]:
    return (
        int(row["late_count"]),
        float(row["total_lateness_min"]),
        float(row["distance_km"]),
    )


def paired_lexicographic_comparison(
    rows: Iterable[dict[str, Any]],
    *,
    left_method: str,
    right_method: str,
) -> dict[str, Any]:
    """Compare matched seeds with the problem's strict score tuple."""

    by_seed: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        method = str(row["method"])
        if method not in {left_method, right_method}:
            continue
        by_seed.setdefault(int(row["seed"]), {})[method] = row

    pairs: list[dict[str, Any]] = []
    counts = {"left_win": 0, "tie": 0, "right_win": 0}
    for seed, methods in sorted(by_seed.items()):
        if left_method not in methods or right_method not in methods:
            continue
        left = methods[left_method]
        right = methods[right_method]
        left_score = _score(left)
        right_score = _score(right)
        if left_score < right_score:
            winner = left_method
            counts["left_win"] += 1
        elif right_score < left_score:
            winner = right_method
            counts["right_win"] += 1
        else:
            winner = "tie"
            counts["tie"] += 1
        pairs.append(
            {
                "seed": seed,
                "winner": winner,
                "left_score": left_score,
                "right_score": right_score,
                "late_count_delta_right_minus_left": right_score[0]
                - left_score[0],
                "total_lateness_delta_right_minus_left": right_score[1]
                - left_score[1],
                "distance_delta_right_minus_left": right_score[2]
                - left_score[2],
                "runtime_delta_right_minus_left": float(
                    right.get("runtime_seconds", 0.0)
                )
                - float(left.get("runtime_seconds", 0.0)),
            }
        )

    return {
        **counts,
        "paired_count": len(pairs),
        "left_method": left_method,
        "right_method": right_method,
        "pairs": pairs,
    }


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
    method: str,
    seed: int,
    result: SolverResult,
    source: str,
    budget_seconds: float | None = None,
    matches_previous_core_score: bool | str = "",
) -> dict[str, Any]:
    evaluation = result.evaluation
    search_runtime = float(
        result.metadata.get("search_runtime_seconds", result.runtime_seconds)
    )
    return {
        "scenario": scenario,
        "method": method,
        "seed": seed,
        "source": source,
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
        "route_pool_columns": result.metadata.get("route_pool_columns", ""),
        "matches_previous_core_score": matches_previous_core_score,
    }


def _historical_halns_row(
    row: dict[str, Any], *, provenance: str
) -> dict[str, Any]:
    return {
        "scenario": "equal_iterations_400",
        "method": HALNS_METHOD,
        "seed": int(row["seed"]),
        "source": provenance,
        "valid": bool(row["valid"]),
        "compliant": float(row["runtime_seconds"]) <= 240.0,
        "budget_seconds": "",
        "late_count": int(row["late_count"]),
        "on_time_rate": float(row["on_time_rate"]),
        "total_lateness_min": float(row["total_lateness_min"]),
        "max_lateness_min": float(row["max_lateness_min"]),
        "distance_km": float(row["distance_km"]),
        "max_route_distance_km": float(row["max_route_distance_km"]),
        "runtime_seconds": float(row["runtime_seconds"]),
        "search_runtime_seconds": "",
        "initial_construction_seconds": "",
        "iterations": int(row["iterations"]),
        "iterations_per_search_second": "",
        "time_to_best_seconds": float(row["time_to_best_seconds"]),
        "accepted_solutions": int(row["accepted_solutions"]),
        "route_pool_columns": int(row["route_pool_columns"]),
        "matches_previous_core_score": "",
    }


def _ci_half_width(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    critical = T_CRITICAL_95.get(len(values) - 1, 1.96)
    return critical * statistics.stdev(values) / math.sqrt(len(values))


def _summaries(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["scenario"]), str(row["method"]))].append(row)
    fields = (
        "late_count",
        "on_time_rate",
        "total_lateness_min",
        "distance_km",
        "runtime_seconds",
        "iterations",
        "iterations_per_search_second",
        "time_to_best_seconds",
    )
    summaries: list[dict[str, Any]] = []
    for (scenario, method), group in sorted(grouped.items()):
        best = min(group, key=_score)
        summary: dict[str, Any] = {
            "scenario": scenario,
            "method": method,
            "runs": len(group),
            "valid_rate": sum(bool(row["valid"]) for row in group) / len(group),
            "compliant_rate": sum(bool(row["compliant"]) for row in group)
            / len(group),
            "best_late_count": best["late_count"],
            "best_total_lateness_min": best["total_lateness_min"],
            "best_distance_km": best["distance_km"],
        }
        for field in fields:
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


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _run_solution_path(
    output_dir: Path,
    *,
    scenario: str,
    method: str,
    seed: int,
) -> Path:
    method_slug = "".join(
        character.lower() if character.isalnum() else "_"
        for character in method
    ).strip("_")
    return (
        output_dir
        / "run_solutions"
        / f"{scenario}__{method_slug}__seed_{seed}.json"
    )


def _write_run_solution(
    output_dir: Path,
    *,
    problem: Problem,
    scenario: str,
    method: str,
    seed: int,
    result: SolverResult,
    source: Path,
) -> str:
    path = _run_solution_path(
        output_dir,
        scenario=scenario,
        method=method,
        seed=seed,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            result_payload(problem, result, source=source),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return str(path.relative_to(output_dir))


def _load_checkpoint(
    path: Path,
    *,
    signature: dict[str, Any],
    output_dir: Path,
    problem: Problem,
    equal_scenario: str,
    wall_scenario: str,
    equal_seeds: set[int],
    wall_seeds: set[int],
) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("signature") != signature:
        raise RuntimeError("断点配置与当前实验不一致，拒绝混用结果")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise RuntimeError("断点缺少运行记录")

    seen: set[tuple[str, str, int]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("断点包含非法运行记录")
        scenario = str(row.get("scenario", ""))
        method = str(row.get("method", ""))
        seed = int(row.get("seed", -1))
        key = (scenario, method, seed)
        if key in seen:
            raise RuntimeError(f"断点包含重复运行 {key}")
        seen.add(key)
        if scenario == equal_scenario:
            allowed = seed in equal_seeds
        elif scenario == wall_scenario:
            allowed = seed in wall_seeds
        else:
            allowed = False
        if not allowed or method not in {CORE_METHOD, HALNS_METHOD}:
            raise RuntimeError(f"断点包含超出当前配置的运行 {key}")
        if row.get("valid") is not True or row.get("compliant") is not True:
            raise RuntimeError(f"断点运行 {key} 未通过合法性或时限检查")

        if str(row.get("source", "")).startswith("new_"):
            relative = Path(str(row.get("solution_file", "")))
            if (
                not relative.parts
                or relative.is_absolute()
                or ".." in relative.parts
            ):
                raise RuntimeError(f"断点运行 {key} 的路线文件路径非法")
            solution_path = output_dir / relative
            if not solution_path.is_file():
                raise RuntimeError(f"断点运行 {key} 缺少完整路线文件")
            expected_sha256 = str(row.get("solution_sha256", ""))
            if (
                not expected_sha256
                or _file_sha256(solution_path) != expected_sha256
            ):
                raise RuntimeError(f"断点运行 {key} 的路线文件哈希不一致")
            solution = json.loads(solution_path.read_text(encoding="utf-8"))
            try:
                routes = [
                    tuple(int(visit) for visit in route["encoded_visits"])
                    for route in solution["routes"]
                ]
                solution_score = _score(
                    {
                        "late_count": solution["score"]["late_count"],
                        "total_lateness_min": solution["score"][
                            "total_lateness_min"
                        ],
                        "distance_km": solution["score"]["distance_km"],
                    }
                )
            except (KeyError, TypeError, ValueError) as error:
                raise RuntimeError(
                    f"断点运行 {key} 的路线文件缺少完整路线或得分"
                ) from error
            independent = evaluate_solution(problem, routes)
            if (
                solution.get("valid") is not True
                or not independent.valid
                or _score(
                    {
                        "late_count": independent.score.late_count,
                        "total_lateness_min": independent.score.total_lateness_min,
                        "distance_km": independent.score.distance_km,
                    }
                )
                != _score(row)
                or solution_score != _score(row)
            ):
                raise RuntimeError(f"断点运行 {key} 与路线文件得分不一致")
    return rows


def _checkpoint(
    output_dir: Path,
    rows: list[dict[str, Any]],
    signature: dict[str, Any],
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "comparison_runs.partial.json").write_text(
        json.dumps(
            {"signature": signature, "rows": rows},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if rows:
        _write_csv(output_dir / "comparison_runs.partial.csv", rows)


def _print_result(row: dict[str, Any]) -> None:
    print(
        f"{row['scenario']:24s} {row['method']:20s} "
        f"seed={row['seed']} score=({row['late_count']}, "
        f"{row['total_lateness_min']:.6f}, {row['distance_km']:.6f}) "
        f"iterations={row['iterations']} runtime={row['runtime_seconds']:.3f}s",
        flush=True,
    )


def run(args: argparse.Namespace) -> None:
    if args.equal_seed_count < 0 or args.wall_seed_count < 0:
        raise ValueError("随机种子数量不能为负")
    if args.equal_iterations < 0 or args.wall_max_iterations <= 0:
        raise ValueError("迭代次数设置非法")
    if (
        not math.isfinite(args.wall_time_limit)
        or args.wall_time_limit <= 0
        or not math.isfinite(args.wall_safety_margin)
        or args.wall_safety_margin < 0
        or args.wall_safety_margin >= args.wall_time_limit
    ):
        raise ValueError("墙钟预算与安全余量设置非法")

    tasks = load_tasks_csv(args.input)
    problem = Problem(tasks, drone_count=8, max_tasks_per_drone=25)
    input_sha256 = _file_sha256(args.input)
    previous_results_sha256 = _file_sha256(args.previous_results)
    previous = json.loads(args.previous_results.read_text(encoding="utf-8"))
    _validate_previous_results(
        previous,
        input_sha256=input_sha256,
        candidate_limit=args.candidate_limit,
    )
    previous_runs = previous["runs"]
    previous_core = {
        int(row["seed"]): row
        for row in previous_runs
        if row["scenario"] == "fleet_n200"
        and row["method"] == "C2-Lex-ALNS"
        and row["seed"] != ""
    }
    previous_halns = {
        int(row["seed"]): row
        for row in previous_runs
        if row["scenario"] == "fleet_n200"
        and row["method"] == HALNS_METHOD
        and row["seed"] != ""
    }
    equal_scenario = f"equal_iterations_{args.equal_iterations}"
    wall_scenario = f"equal_wall_clock_{args.wall_time_limit:g}s"
    equal_seeds = [
        args.seed_base + offset for offset in range(args.equal_seed_count)
    ]
    wall_seeds = [
        args.seed_base + offset for offset in range(args.wall_seed_count)
    ]
    checkpoint_signature = _checkpoint_signature(
        args,
        input_sha256=input_sha256,
        previous_results_sha256=previous_results_sha256,
    )
    historical_provenance = (
        f"previous_results:{previous_results_sha256[:12]}"
    )
    partial_path = args.output_dir / "comparison_runs.partial.json"
    resumed_from_checkpoint = args.resume and partial_path.exists()
    rows: list[dict[str, Any]] = (
        _load_checkpoint(
            partial_path,
            signature=checkpoint_signature,
            output_dir=args.output_dir,
            problem=problem,
            equal_scenario=equal_scenario,
            wall_scenario=wall_scenario,
            equal_seeds=set(equal_seeds),
            wall_seeds=set(wall_seeds),
        )
        if resumed_from_checkpoint
        else []
    )
    completed = {
        (str(row["scenario"]), str(row["method"]), int(row["seed"]))
        for row in rows
    }

    if equal_seeds:
        initial = construct_regret_initial(
            problem, candidate_limit=args.candidate_limit
        )
        for seed in equal_seeds:
            if seed not in previous_core or seed not in previous_halns:
                raise RuntimeError(f"历史结果缺少配对种子 {seed}")
            for method, historical in (
                ("Core", previous_core[seed]),
                ("HALNS", previous_halns[seed]),
            ):
                dimensions = (
                    int(historical.get("task_count", -1)),
                    int(historical.get("drone_count", -1)),
                    int(historical.get("max_tasks_per_drone", -1)),
                )
                expected_dimensions = (
                    len(problem.tasks),
                    problem.drone_count,
                    problem.max_tasks_per_drone,
                )
                if dimensions != expected_dimensions:
                    raise RuntimeError(
                        f"历史 {method} 种子 {seed} 的问题规模不一致"
                    )
            historical_iterations = int(previous_core[seed]["iterations"])
            if int(previous_halns[seed]["iterations"]) != historical_iterations:
                raise RuntimeError(f"历史配对种子 {seed} 的迭代数不一致")
            comparable_to_history = (
                args.equal_iterations == historical_iterations
            )
            core_key = (equal_scenario, CORE_METHOD, seed)
            historical_key = (equal_scenario, HALNS_METHOD, seed)
            if core_key in completed:
                if comparable_to_history and historical_key not in completed:
                    historical_row = _historical_halns_row(
                        previous_halns[seed],
                        provenance=historical_provenance,
                    )
                    historical_row["scenario"] = equal_scenario
                    rows.append(historical_row)
                    completed.add(historical_key)
                    _checkpoint(
                        args.output_dir,
                        rows,
                        checkpoint_signature,
                    )
                continue
            result = solve_alns_core(
                problem,
                config=ALNSConfig(
                    max_iterations=args.equal_iterations,
                    time_limit_seconds=None,
                    seed=seed,
                    candidate_limit=args.candidate_limit,
                ),
                initial_routes=initial.routes,
            )
            result = _with_initial_runtime(result, initial)
            matches: bool | str = ""
            if comparable_to_history:
                historical_score = _score(previous_core[seed])
                matches = _score(
                    {
                        "late_count": result.evaluation.score.late_count,
                        "total_lateness_min": result.evaluation.score.total_lateness_min,
                        "distance_km": result.evaluation.score.distance_km,
                    }
                ) == historical_score
                if not matches:
                    raise RuntimeError(
                        f"种子 {seed} 的 Core 得分未复现历史 Basic ALNS"
                    )
            current_row = _row(
                scenario=equal_scenario,
                method=CORE_METHOD,
                seed=seed,
                result=result,
                source="new_core_run",
                matches_previous_core_score=matches,
            )
            if not current_row["valid"]:
                raise RuntimeError(f"Core 种子 {seed} 未产生合法解")
            current_row["solution_file"] = _write_run_solution(
                args.output_dir,
                problem=problem,
                scenario=equal_scenario,
                method=CORE_METHOD,
                seed=seed,
                result=result,
                source=args.input,
            )
            current_row["solution_sha256"] = _file_sha256(
                args.output_dir / str(current_row["solution_file"])
            )
            rows.append(current_row)
            completed.add(core_key)
            if comparable_to_history:
                historical_row = _historical_halns_row(
                    previous_halns[seed],
                    provenance=historical_provenance,
                )
                historical_row["scenario"] = equal_scenario
                rows.append(historical_row)
                completed.add(historical_key)
            _print_result(current_row)
            _checkpoint(args.output_dir, rows, checkpoint_signature)
            gc.collect()

    if wall_seeds:
        wall_initial = construct_regret_initial(
            problem, candidate_limit=args.candidate_limit
        )
        remaining_search_time = (
            args.wall_time_limit
            - wall_initial.runtime_seconds
            - args.wall_safety_margin
        )
        if remaining_search_time <= 0:
            raise RuntimeError("初始解构造已耗尽墙钟预算")

        for offset, seed in enumerate(wall_seeds):
            order = (
                (CORE_METHOD, HALNS_METHOD)
                if offset % 2 == 0
                else (HALNS_METHOD, CORE_METHOD)
            )
            for method in order:
                run_key = (wall_scenario, method, seed)
                if run_key in completed:
                    continue
                config = ALNSConfig(
                    max_iterations=args.wall_max_iterations,
                    time_limit_seconds=remaining_search_time,
                    seed=seed,
                    candidate_limit=args.candidate_limit,
                )
                if method == CORE_METHOD:
                    result = solve_alns_core(
                        problem,
                        config=config,
                        initial_routes=wall_initial.routes,
                    )
                else:
                    result = solve_alns(
                        problem,
                        config=config,
                        initial_routes=wall_initial.routes,
                    )
                result = _with_initial_runtime(result, wall_initial)
                current_row = _row(
                    scenario=wall_scenario,
                    method=method,
                    seed=seed,
                    result=result,
                    source="new_paired_wall_clock_run",
                    budget_seconds=args.wall_time_limit,
                )
                if not current_row["valid"] or not current_row["compliant"]:
                    raise RuntimeError(
                        f"{method} 种子 {seed} 未产生墙钟预算内合法解"
                    )
                current_row["solution_file"] = _write_run_solution(
                    args.output_dir,
                    problem=problem,
                    scenario=wall_scenario,
                    method=method,
                    seed=seed,
                    result=result,
                    source=args.input,
                )
                current_row["solution_sha256"] = _file_sha256(
                    args.output_dir / str(current_row["solution_file"])
                )
                rows.append(current_row)
                completed.add(run_key)
                _print_result(current_row)
                _checkpoint(args.output_dir, rows, checkpoint_signature)
                gc.collect()

    summaries = _summaries(rows)
    comparisons = {
        scenario: paired_lexicographic_comparison(
            [row for row in rows if row["scenario"] == scenario],
            left_method=CORE_METHOD,
            right_method=HALNS_METHOD,
        )
        for scenario in (equal_scenario, wall_scenario)
        if {
            row["method"] for row in rows if row["scenario"] == scenario
        }
        >= {CORE_METHOD, HALNS_METHOD}
    }
    regression_matches = [
        bool(row["matches_previous_core_score"])
        for row in rows
        if row["method"] == CORE_METHOD
        and row["scenario"] == equal_scenario
        and row["matches_previous_core_score"] != ""
    ]
    manifest = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "input": str(args.input),
        "input_sha256": input_sha256,
        "previous_results": str(args.previous_results),
        "previous_results_sha256": previous_results_sha256,
        "historical_provenance": historical_provenance,
        "definition": {
            "core": (
                "Strict lexicographic ALNS with adaptive destroy/repair and "
                "simulated annealing; route pool and ejection disabled"
            ),
            "halns": "The current branch's ALNS plus route pool and ejection",
        },
        "problem": {
            "task_count": len(tasks),
            "drone_count": 8,
            "max_tasks_per_drone": 25,
            "capacity": 2,
            "speed_km_per_min": 0.9,
            "depot_km": [0.0, 0.0],
            "open_routes": True,
        },
        "settings": {
            "seed_base": args.seed_base,
            "equal_seed_count": args.equal_seed_count,
            "equal_iterations": args.equal_iterations,
            "wall_seed_count": args.wall_seed_count,
            "wall_time_limit_seconds": args.wall_time_limit,
            "wall_safety_margin_seconds": args.wall_safety_margin,
            "wall_max_iterations": args.wall_max_iterations,
            "candidate_limit": args.candidate_limit,
            "alternating_wall_run_order": True,
            "resumed_from_checkpoint": resumed_from_checkpoint,
        },
        "equal_iteration_core_regression_matches": regression_matches,
    }
    payload = {
        "manifest": manifest,
        "runs": rows,
        "summaries": summaries,
        "paired_comparisons": comparisons,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "comparison_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(args.output_dir / "comparison_runs.csv", rows)
    _write_csv(args.output_dir / "comparison_summary.csv", summaries)

    for scenario in (equal_scenario, wall_scenario):
        for method, filename in (
            (CORE_METHOD, "best_alns_core"),
            (HALNS_METHOD, "best_halns"),
        ):
            candidates = [
                row
                for row in rows
                if row["scenario"] == scenario
                and row["method"] == method
                and row.get("solution_file", "")
            ]
            if not candidates:
                continue
            best = min(
                candidates,
                key=lambda row: (_score(row), float(row["runtime_seconds"])),
            )
            source_path = args.output_dir / str(best["solution_file"])
            (args.output_dir / f"{filename}_{scenario}.json").write_text(
                source_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )

    for partial in (
        args.output_dir / "comparison_runs.partial.json",
        args.output_dir / "comparison_runs.partial.csv",
    ):
        partial.unlink(missing_ok=True)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--previous-results", type=Path, default=DEFAULT_PREVIOUS_RESULTS
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--seed-base", type=int, default=2026080500)
    parser.add_argument("--equal-seed-count", type=int, default=5)
    parser.add_argument("--equal-iterations", type=int, default=400)
    parser.add_argument("--wall-seed-count", type=int, default=3)
    parser.add_argument("--wall-time-limit", type=float, default=240.0)
    parser.add_argument("--wall-safety-margin", type=float, default=2.0)
    parser.add_argument("--wall-max-iterations", type=int, default=10_000)
    parser.add_argument("--candidate-limit", type=int, default=48)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.quick:
        args.equal_seed_count = 1
        args.equal_iterations = 20
        args.wall_seed_count = 1
        args.wall_time_limit = 10.0
        args.wall_safety_margin = 0.5
    return args


if __name__ == "__main__":
    run(_parse_args())
