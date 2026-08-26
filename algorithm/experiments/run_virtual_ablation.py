"""Synthetic Section 5 algorithm comparison and ALNS component ablation.

The generated instances are explicitly virtual.  They are deterministic,
contain no operational observations, and are used only to test algorithmic
mechanisms under controlled changes.  Every ALNS variant starts from the same
regret-2 solution and receives the same search budget and solver seed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
import time
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from random import Random
from typing import Any, Iterable, Iterator, Mapping, Sequence

import uav_dispatch.alns as alns_module
from uav_dispatch import (
    ALNSConfig,
    Point,
    Problem,
    SolverResult,
    Task,
    construct_edd_adjacent,
    construct_greedy_initial,
    construct_nearest_adjacent,
    construct_regret_initial,
    solve_alns,
)

from algorithm.experiments.run_scenario_comparison import (
    _environment_manifest,
    _git_state,
    _optional_sha256,
    _sha256,
    _solver_source_sha256,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "algorithm" / "results" / "virtual_ablation"
INSTANCE_SIZES = (30, 60, 90)
INSTANCE_SEEDS = tuple(range(2026082601, 2026082606))
MAX_TASKS_PER_DRONE = 15
MAX_ITERATIONS = 1_000
CANDIDATE_LIMIT = 32
SPEED_KM_PER_MIN = 0.9
CAPACITY = 2
REGION_KM = 20.0
CLUSTER_CENTRES = ((3.0, 3.0), (17.0, 3.0), (3.0, 17.0), (17.0, 17.0))


BASE_DESTROY = tuple(alns_module.DESTROY_OPERATORS)
BASE_REPAIR = tuple(alns_module.REPAIR_OPERATORS)


VARIANTS: dict[str, dict[str, Any]] = {
    "full_alns": {
        "label": "完整C2-Lex-ALNS",
        "description": "14个算子、截止期风险引导和自适应权重全部开启",
        "destroy": BASE_DESTROY,
        "repair": BASE_REPAIR,
        "deadline_risk": True,
        "adaptive_weights": True,
    },
    "no_time_awareness": {
        "label": "去除时间感知",
        "description": (
            "关闭截止期风险引导，并移除deadline_related、late_critical、"
            "deadline和slack算子"
        ),
        "destroy": tuple(
            name for name in BASE_DESTROY
            if name not in {"deadline_related", "late_critical"}
        ),
        "repair": tuple(
            name for name in BASE_REPAIR if name not in {"deadline", "slack"}
        ),
        "deadline_risk": False,
        "adaptive_weights": True,
    },
    "no_capacity_conflict": {
        "label": "去除容量冲突破坏",
        "description": "移除capacity_conflict破坏算子，其余设置不变",
        "destroy": tuple(
            name for name in BASE_DESTROY if name != "capacity_conflict"
        ),
        "repair": BASE_REPAIR,
        "deadline_risk": True,
        "adaptive_weights": True,
    },
    "no_assignment_destroy": {
        "label": "去除任务重分配破坏",
        "description": "移除assignment_destroy破坏算子，其余设置不变",
        "destroy": tuple(
            name for name in BASE_DESTROY if name != "assignment_destroy"
        ),
        "repair": BASE_REPAIR,
        "deadline_risk": True,
        "adaptive_weights": True,
    },
    "uniform_operator_weights": {
        "label": "关闭自适应权重",
        "description": "保留14个算子，但整个搜索期间保持均匀选择权重",
        "destroy": BASE_DESTROY,
        "repair": BASE_REPAIR,
        "deadline_risk": True,
        "adaptive_weights": False,
    },
}


METHOD_LABELS = {
    "nearest_adjacent": "最近邻成对贪心",
    "edd_adjacent": "最早截止期成对贪心",
    "greedy_full_position": "全位置词典序贪心",
    "full_alns": "完整C2-Lex-ALNS",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--search-seconds",
        type=float,
        help="可选的探索性墙钟上限；正式结果不设置该参数",
    )
    parser.add_argument("--max-iterations", type=int, default=MAX_ITERATIONS)
    parser.add_argument("--candidate-limit", type=int, default=CANDIDATE_LIMIT)
    parser.add_argument("--quick", action="store_true")
    return parser


def _clip(value: float) -> float:
    return min(REGION_KM, max(0.0, value))


def generate_virtual_tasks(task_count: int, seed: int) -> tuple[Task, ...]:
    """Generate clustered pickup-delivery requests in a 20 km square."""

    rng = Random(seed)
    depot = Point(REGION_KM / 2.0, REGION_KM / 2.0)
    tasks: list[Task] = []
    for task_id in range(1, task_count + 1):
        pickup_centre_index = rng.randrange(len(CLUSTER_CENTRES))
        if rng.random() < 0.70:
            delivery_choices = [
                index for index in range(len(CLUSTER_CENTRES))
                if index != pickup_centre_index
            ]
            delivery_centre_index = rng.choice(delivery_choices)
        else:
            delivery_centre_index = pickup_centre_index
        pickup_centre = CLUSTER_CENTRES[pickup_centre_index]
        delivery_centre = CLUSTER_CENTRES[delivery_centre_index]
        pickup = Point(
            _clip(rng.gauss(pickup_centre[0], 1.6)),
            _clip(rng.gauss(pickup_centre[1], 1.6)),
        )
        delivery = Point(
            _clip(rng.gauss(delivery_centre[0], 1.8)),
            _clip(rng.gauss(delivery_centre[1], 1.8)),
        )
        solo_completion = (
            depot.distance_to(pickup) + pickup.distance_to(delivery)
        ) / SPEED_KM_PER_MIN
        # The mixed slack band produces both urgent and flexible requests.
        # It is independent of every tested algorithm.
        if rng.random() < 0.35:
            slack = rng.uniform(28.0, 70.0)
        else:
            slack = rng.uniform(70.0, 145.0)
        deadline = solo_completion + slack
        tasks.append(Task(task_id, pickup, delivery, deadline))
    return tuple(tasks)


def _instance_payload(
    tasks: Sequence[Task], *, task_count: int, seed: int, drone_count: int
) -> dict[str, Any]:
    task_rows = [
        {
            "id": task.id,
            "pickup": [task.pickup.x, task.pickup.y],
            "delivery": [task.delivery.x, task.delivery.y],
            "deadline_min": task.deadline_min,
        }
        for task in tasks
    ]
    canonical = json.dumps(
        task_rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return {
        "instance_id": f"virtual_n{task_count}_seed{seed}",
        "task_count": task_count,
        "instance_seed": seed,
        "drone_count": drone_count,
        "max_tasks_per_drone": MAX_TASKS_PER_DRONE,
        "capacity": CAPACITY,
        "speed_km_per_min": SPEED_KM_PER_MIN,
        "region_km": [REGION_KM, REGION_KM],
        "depot_km": [REGION_KM / 2.0, REGION_KM / 2.0],
        "task_sha256": hashlib.sha256(canonical).hexdigest(),
        "tasks": task_rows,
    }


@contextmanager
def _operator_registry(
    destroy: Sequence[str], repair: Sequence[str]
) -> Iterator[None]:
    old_destroy = alns_module.DESTROY_OPERATORS
    old_repair = alns_module.REPAIR_OPERATORS
    alns_module.DESTROY_OPERATORS = tuple(destroy)
    alns_module.REPAIR_OPERATORS = tuple(repair)
    try:
        yield
    finally:
        alns_module.DESTROY_OPERATORS = old_destroy
        alns_module.REPAIR_OPERATORS = old_repair


def _score_tuple(result: SolverResult) -> tuple[int, float, float]:
    score = result.evaluation.score
    return score.late_count, score.total_lateness_min, score.distance_km


def _routes_sha256(routes: Sequence[Sequence[int]]) -> str:
    canonical = json.dumps(
        [list(route) for route in routes],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _run_row(
    *,
    problem: Problem,
    instance_id: str,
    instance_seed: int,
    method_id: str,
    label: str,
    result: SolverResult,
    construction_seconds: float = 0.0,
    solver_seed: int | None = None,
    active_destroy: Sequence[str] = (),
    active_repair: Sequence[str] = (),
    variant_description: str = "",
    initial_routes_sha256: str | None = None,
) -> dict[str, Any]:
    score = result.evaluation.score
    operator_stats = {
        key: dict(value)
        for key, value in result.metadata.get("operator_statistics", {}).items()
    }
    operator_weights = dict(result.metadata.get("operator_weights", {}))
    return {
        "instance_id": instance_id,
        "task_count": len(problem.tasks),
        "instance_seed": instance_seed,
        "solver_seed": solver_seed,
        "drone_count": problem.drone_count,
        "method_id": method_id,
        "method_label": label,
        "variant_description": variant_description,
        "initial_routes_sha256": initial_routes_sha256,
        "late_count": score.late_count,
        "late_rate": score.late_count / len(problem.tasks),
        "total_lateness_min": score.total_lateness_min,
        "lateness_per_task_min": score.total_lateness_min / len(problem.tasks),
        "distance_km": score.distance_km,
        "distance_per_task_km": score.distance_km / len(problem.tasks),
        "construction_seconds": construction_seconds,
        "search_seconds": result.runtime_seconds,
        "total_runtime_seconds": construction_seconds + result.runtime_seconds,
        "iterations": result.iterations,
        "valid": result.evaluation.valid,
        "active_destroy_operators": list(active_destroy),
        "active_repair_operators": list(active_repair),
        "operator_statistics": operator_stats,
        "operator_weights": operator_weights,
        "routes": [list(route) for route in result.routes],
    }


def _mean_sd(values: Iterable[float]) -> tuple[float, float]:
    series = list(values)
    return (
        statistics.fmean(series),
        statistics.stdev(series) if len(series) > 1 else 0.0,
    )


def _method_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    comparison_ids = tuple(METHOD_LABELS)
    comparison_rows = [row for row in rows if row["method_id"] in comparison_ids]
    by_instance: dict[str, list[Mapping[str, Any]]] = {}
    for row in comparison_rows:
        by_instance.setdefault(str(row["instance_id"]), []).append(row)
    win_cells: dict[str, int] = {method_id: 0 for method_id in comparison_ids}
    for selected in by_instance.values():
        best = min(
            (
                int(row["late_count"]),
                float(row["total_lateness_min"]),
                float(row["distance_km"]),
            )
            for row in selected
        )
        for row in selected:
            score = (
                int(row["late_count"]),
                float(row["total_lateness_min"]),
                float(row["distance_km"]),
            )
            if score == best:
                win_cells[str(row["method_id"])] += 1
    result: list[dict[str, Any]] = []
    for method_id in comparison_ids:
        selected = [row for row in comparison_rows if row["method_id"] == method_id]
        late_mean, late_sd = _mean_sd(float(row["late_rate"]) for row in selected)
        lateness_mean, lateness_sd = _mean_sd(
            float(row["lateness_per_task_min"]) for row in selected
        )
        distance_mean, distance_sd = _mean_sd(
            float(row["distance_per_task_km"]) for row in selected
        )
        runtime_mean, runtime_sd = _mean_sd(
            float(row["total_runtime_seconds"]) for row in selected
        )
        result.append(
            {
                "method_id": method_id,
                "method_label": METHOD_LABELS[method_id],
                "runs": len(selected),
                "lexicographic_best_cells": win_cells[method_id],
                "mean_late_rate": late_mean,
                "sd_late_rate": late_sd,
                "mean_lateness_per_task_min": lateness_mean,
                "sd_lateness_per_task_min": lateness_sd,
                "mean_distance_per_task_km": distance_mean,
                "sd_distance_per_task_km": distance_sd,
                "mean_total_runtime_seconds": runtime_mean,
                "sd_total_runtime_seconds": runtime_sd,
                "valid_rate": statistics.fmean(
                    1.0 if bool(row["valid"]) else 0.0 for row in selected
                ),
            }
        )
    return result


def _ablation_summary(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    selected_rows = [row for row in rows if row["method_id"] in VARIANTS]
    full_by_instance = {
        str(row["instance_id"]): row
        for row in selected_rows
        if row["method_id"] == "full_alns"
    }
    result: list[dict[str, Any]] = []
    for variant_id, spec in VARIANTS.items():
        variant_rows = [row for row in selected_rows if row["method_id"] == variant_id]
        full_wins = ties = variant_wins = 0
        delta_late: list[float] = []
        delta_lateness: list[float] = []
        delta_distance: list[float] = []
        delta_late_rate_pp: list[float] = []
        delta_lateness_per_task: list[float] = []
        delta_distance_per_task: list[float] = []
        for row in variant_rows:
            full = full_by_instance[str(row["instance_id"])]
            full_score = (
                int(full["late_count"]),
                float(full["total_lateness_min"]),
                float(full["distance_km"]),
            )
            variant_score = (
                int(row["late_count"]),
                float(row["total_lateness_min"]),
                float(row["distance_km"]),
            )
            if full_score < variant_score:
                full_wins += 1
            elif full_score > variant_score:
                variant_wins += 1
            else:
                ties += 1
            delta_late.append(float(row["late_count"]) - float(full["late_count"]))
            delta_lateness.append(
                float(row["total_lateness_min"])
                - float(full["total_lateness_min"])
            )
            delta_distance.append(
                float(row["distance_km"]) - float(full["distance_km"])
            )
            delta_late_rate_pp.append(
                100.0 * (float(row["late_rate"]) - float(full["late_rate"]))
            )
            delta_lateness_per_task.append(
                float(row["lateness_per_task_min"])
                - float(full["lateness_per_task_min"])
            )
            delta_distance_per_task.append(
                float(row["distance_per_task_km"])
                - float(full["distance_per_task_km"])
            )
        late_rate_mean, late_rate_sd = _mean_sd(
            float(row["late_rate"]) for row in variant_rows
        )
        result.append(
            {
                "variant_id": variant_id,
                "variant_label": spec["label"],
                "description": spec["description"],
                "runs": len(variant_rows),
                "full_wins": full_wins,
                "ties": ties,
                "variant_wins": variant_wins,
                "mean_delta_late_count_vs_full": statistics.fmean(delta_late),
                "mean_delta_total_lateness_min_vs_full": statistics.fmean(
                    delta_lateness
                ),
                "mean_delta_distance_km_vs_full": statistics.fmean(
                    delta_distance
                ),
                "mean_delta_late_rate_percentage_points_vs_full": statistics.fmean(
                    delta_late_rate_pp
                ),
                "mean_delta_lateness_per_task_min_vs_full": statistics.fmean(
                    delta_lateness_per_task
                ),
                "mean_delta_distance_per_task_km_vs_full": statistics.fmean(
                    delta_distance_per_task
                ),
                "mean_late_rate": late_rate_mean,
                "sd_late_rate": late_rate_sd,
                "valid_rate": statistics.fmean(
                    1.0 if bool(row["valid"]) else 0.0 for row in variant_rows
                ),
            }
        )
    return result


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    scalar_rows = [
        {
            key: value
            for key, value in row.items()
            if not isinstance(value, (dict, list, tuple))
        }
        for row in rows
    ]
    if not scalar_rows:
        raise ValueError(f"没有可写入{path.name}的结果")
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(scalar_rows[0]),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(scalar_rows)


def _write_report(
    path: Path,
    method_summary: Sequence[Mapping[str, Any]],
    ablation_summary: Sequence[Mapping[str, Any]],
) -> None:
    lines = [
        "# 第5章虚拟数据算法对比与消融实验",
        "",
        "> 本文件全部结果来自确定性生成的虚拟实例，不是业务实测数据。",
        "",
        "## 算法对比",
        "",
        "| 方法 | 运行 | 四方法中最佳单元 | 平均逾期率 | 平均单任务逾期/min | 平均单任务航程/km | 有效率 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in method_summary:
        lines.append(
            f"| {row['method_label']} | {row['runs']} | "
            f"{row['lexicographic_best_cells']} | "
            f"{100 * float(row['mean_late_rate']):.2f}% | "
            f"{float(row['mean_lateness_per_task_min']):.3f} | "
            f"{float(row['mean_distance_per_task_km']):.3f} | "
            f"{100 * float(row['valid_rate']):.1f}% |"
        )
    lines.extend(
        [
            "",
            "## ALNS组件消融",
            "",
        "| 变体 | 完整胜/平/变体胜 | 平均Δ逾期率/百分点 | 平均Δ单任务逾期/min | 平均Δ单任务航程/km | 有效率 |",
        "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in ablation_summary:
        lines.append(
            f"| {row['variant_label']} | {row['full_wins']}/"
            f"{row['ties']}/{row['variant_wins']} | "
            f"{float(row['mean_delta_late_rate_percentage_points_vs_full']):+.3f} | "
            f"{float(row['mean_delta_lateness_per_task_min_vs_full']):+.3f} | "
            f"{float(row['mean_delta_distance_per_task_km_vs_full']):+.3f} | "
            f"{100 * float(row['valid_rate']):.1f}% |"
        )
    lines.extend(
        [
            "",
            "> Δ=关闭组件变体−完整ALNS；三项差值先按任务数归一化，再对15个规模—种子单元等权平均。胜/平/负按严格字典序判定，低层差值不能越过高层目标单独判胜。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.search_seconds is not None and (
        not math.isfinite(args.search_seconds) or args.search_seconds <= 0
    ):
        raise ValueError("搜索预算必须为有限正数")
    if args.max_iterations <= 0:
        raise ValueError("最大迭代数必须为正数")
    if args.candidate_limit < 4:
        raise ValueError("候选位置上限至少为4")
    sizes = INSTANCE_SIZES[:1] if args.quick else INSTANCE_SIZES
    seeds = INSTANCE_SEEDS[:2] if args.quick else INSTANCE_SEEDS
    max_iterations = min(args.max_iterations, 12) if args.quick else args.max_iterations
    search_seconds = (
        min(args.search_seconds, 0.15)
        if args.quick and args.search_seconds is not None
        else args.search_seconds
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)

    instances: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    for task_count in sizes:
        drone_count = math.ceil(task_count / MAX_TASKS_PER_DRONE)
        for instance_seed in seeds:
            tasks = generate_virtual_tasks(task_count, instance_seed)
            problem = Problem(
                tasks,
                drone_count=drone_count,
                max_tasks_per_drone=MAX_TASKS_PER_DRONE,
                capacity=CAPACITY,
                speed_km_per_min=SPEED_KM_PER_MIN,
                depot=Point(REGION_KM / 2.0, REGION_KM / 2.0),
            )
            instance = _instance_payload(
                tasks,
                task_count=task_count,
                seed=instance_seed,
                drone_count=drone_count,
            )
            instances.append(instance)
            instance_id = str(instance["instance_id"])

            constructors = (
                ("nearest_adjacent", construct_nearest_adjacent),
                ("edd_adjacent", construct_edd_adjacent),
                (
                    "greedy_full_position",
                    lambda value: construct_greedy_initial(
                        value, candidate_limit=args.candidate_limit
                    ),
                ),
            )
            for method_id, constructor in constructors:
                result = constructor(problem)
                runs.append(
                    _run_row(
                        problem=problem,
                        instance_id=instance_id,
                        instance_seed=instance_seed,
                        method_id=method_id,
                        label=METHOD_LABELS[method_id],
                        result=result,
                    )
                )

            initial_started = time.perf_counter()
            initial = construct_regret_initial(
                problem, candidate_limit=args.candidate_limit
            )
            construction_seconds = time.perf_counter() - initial_started
            initial_routes_sha256 = _routes_sha256(initial.routes)
            solver_seed = instance_seed
            base_config = ALNSConfig(
                max_iterations=max_iterations,
                time_limit_seconds=search_seconds,
                seed=solver_seed,
                candidate_limit=args.candidate_limit,
                relay_enabled=False,
            )
            for variant_id, spec in VARIANTS.items():
                config = replace(
                    base_config,
                    enable_deadline_risk=bool(spec["deadline_risk"]),
                    weight_update_interval=(
                        base_config.weight_update_interval
                        if spec["adaptive_weights"]
                        else max_iterations + 1
                    ),
                )
                with _operator_registry(spec["destroy"], spec["repair"]):
                    result = solve_alns(
                        problem,
                        config=config,
                        initial_routes=initial.routes,
                    )
                runs.append(
                    _run_row(
                        problem=problem,
                        instance_id=instance_id,
                        instance_seed=instance_seed,
                        method_id=variant_id,
                        label=str(spec["label"]),
                        result=result,
                        construction_seconds=construction_seconds,
                        solver_seed=solver_seed,
                        active_destroy=spec["destroy"],
                        active_repair=spec["repair"],
                        variant_description=str(spec["description"]),
                        initial_routes_sha256=initial_routes_sha256,
                    )
                )
            full_row = next(
                row for row in reversed(runs)
                if row["instance_id"] == instance_id
                and row["method_id"] == "full_alns"
            )
            print(
                f"[{instance_id}] full="
                f"({full_row['late_count']}, "
                f"{float(full_row['total_lateness_min']):.2f}, "
                f"{float(full_row['distance_km']):.2f})",
                flush=True,
            )

    method_summary = _method_summary(runs)
    ablation_summary = _ablation_summary(runs)
    git_commit, git_dirty = _git_state()
    environment = _environment_manifest()
    manifest = {
        "schema_version": 1,
        "protocol": "section5_virtual_algorithm_and_component_ablation",
        "formal": False,
        "data_kind": "synthetic_virtual",
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
        "instance_sizes": list(sizes),
        "instance_seeds": list(seeds),
        "instance_count": len(instances),
        "run_count": len(runs),
        "budget_mode": (
            "fixed_iterations" if search_seconds is None else "wall_clock"
        ),
        "search_budget_seconds": search_seconds,
        "candidate_limit": args.candidate_limit,
        "max_iterations": max_iterations,
        "paired_design": (
            "same generated instance, initial-route SHA-256, solver seed and "
            + (
                "fixed iteration budget for every ALNS variant"
                if search_seconds is None
                else "wall-clock search budget for every ALNS variant"
            )
        ),
        "generator": {
            "region_km": [REGION_KM, REGION_KM],
            "depot_km": [REGION_KM / 2.0, REGION_KM / 2.0],
            "cluster_centres_km": [list(item) for item in CLUSTER_CENTRES],
            "cross_cluster_delivery_probability": 0.70,
            "pickup_jitter_sd_km": 1.6,
            "delivery_jitter_sd_km": 1.8,
            "urgent_task_probability": 0.35,
            "urgent_slack_min": [28.0, 70.0],
            "flexible_slack_min": [70.0, 145.0],
            "drone_count_policy": "ceil(task_count / 15)",
            "max_tasks_per_drone": MAX_TASKS_PER_DRONE,
            "capacity": CAPACITY,
            "speed_km_per_min": SPEED_KM_PER_MIN,
        },
        "method_ids": list(METHOD_LABELS),
        "variants": {
            variant_id: {
                **{
                    key: value
                    for key, value in spec.items()
                    if key not in {"destroy", "repair"}
                },
                "destroy": list(spec["destroy"]),
                "repair": list(spec["repair"]),
            }
            for variant_id, spec in VARIANTS.items()
        },
        "interpretation_boundary": (
            "virtual algorithm stress test only; not operational demand, "
            "flight, cost, safety or causal business evidence"
        ),
    }
    payload = {
        "manifest": manifest,
        "instances": instances,
        "runs": runs,
        "method_summary": method_summary,
        "ablation_summary": ablation_summary,
    }
    (args.output_dir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_csv(args.output_dir / "runs.csv", runs)
    _write_csv(args.output_dir / "method_summary.csv", method_summary)
    _write_csv(args.output_dir / "ablation_summary.csv", ablation_summary)
    _write_report(
        args.output_dir / "REPORT.md", method_summary, ablation_summary
    )
    checksum_files = (
        "results.json",
        "manifest.json",
        "runs.csv",
        "method_summary.csv",
        "ablation_summary.csv",
        "REPORT.md",
    )
    checksums = {
        filename: _sha256(args.output_dir / filename)
        for filename in checksum_files
    }
    (args.output_dir / "checksums.json").write_text(
        json.dumps(checksums, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"结果已写入 {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
