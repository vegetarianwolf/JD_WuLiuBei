"""Run auditable direct and static-hub relay experiments.

The experiment budget covers the A2 + late-risk direct search performed inside
``solve_relay_alns``.  ``total_lateness_min`` is retained only as a diagnostic;
the comparison objective is on-time deliveries followed by total distance.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from uav_dispatch import Point, Problem, Task, evaluate_solution, load_tasks_csv
from uav_dispatch.relay import Event, RelayPlan, RelayProblem, TaskCountSemantics
from uav_dispatch.relay_alns import (
    RelayALNSConfig,
    RelaySolverResult,
    solve_relay_alns,
)
from uav_dispatch.relay_search import generate_flow_hubs
from uav_dispatch.relay_validation import evaluate_relay_plan


ROOT = Path(__file__).resolve().parents[2]
SOURCE_CODE_RELATIVE_PATHS = (
    "algorithm/src/uav_dispatch/relay.py",
    "algorithm/src/uav_dispatch/relay_validation.py",
    "algorithm/src/uav_dispatch/relay_search.py",
    "algorithm/src/uav_dispatch/relay_alns.py",
    "algorithm/experiments/run_relay_handoff.py",
)
SOURCE_CODE_CSV_FIELDS = {
    "algorithm/src/uav_dispatch/relay.py": "relay_py_sha256",
    "algorithm/src/uav_dispatch/relay_validation.py": (
        "relay_validation_py_sha256"
    ),
    "algorithm/src/uav_dispatch/relay_search.py": (
        "relay_search_py_sha256"
    ),
    "algorithm/src/uav_dispatch/relay_alns.py": "relay_alns_py_sha256",
    "algorithm/experiments/run_relay_handoff.py": "runner_py_sha256",
}
DEFAULT_INPUT = (
    ROOT
    / "algorithm"
    / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)
DEFAULT_OUTPUT = ROOT / "algorithm" / "results" / "relay_handoff"
DEFAULT_METHODS = (
    "baseline",
    "relay-disabled",
    "static-1hub",
    "static-multihub",
)
DEFAULT_SEEDS = (2026080500, 2026080501, 2026080502)


SUMMARY_FIELDS = (
    "method",
    "semantics",
    "task_count",
    "run_count",
    "evaluation_valid_run_count",
    "valid_run_count",
    "runtime_compliant_run_count",
    "official_semantics_compliant_run_count",
    "official_plan_compliant_run_count",
    "summary_eligible_run_count",
    "on_time_count_mean",
    "on_time_count_min",
    "on_time_count_max",
    "on_time_rate_mean",
    "on_time_rate_min",
    "on_time_rate_max",
    "distance_km_mean",
    "distance_km_min",
    "distance_km_max",
    "runtime_seconds_mean",
    "budget_seconds",
    "relay_count_mean",
)

RUN_FIELDS = (
    "method",
    "semantics",
    "task_count",
    "seed",
    "valid",
    "runtime_compliant",
    "official_semantics_compliant",
    "official_plan_compliant",
    "compliant",
    "semantics_extension",
    "source_snapshot_sha256",
    "source_snapshot_role",
    "relay_py_sha256",
    "relay_validation_py_sha256",
    "relay_search_py_sha256",
    "relay_alns_py_sha256",
    "runner_py_sha256",
    "input_sha256",
    "problem_sha256",
    "budget_seconds",
    "effective_search_time_limit_seconds",
    "on_time_count",
    "on_time_rate",
    "late_count",
    "total_lateness_min",
    "distance_km",
    "runtime_seconds",
    "iterations",
    "relay_count",
    "direct_task_count",
    "direct_event_equivalent",
    "base_search_used",
    "base_search_runtime_seconds",
    "base_search_iterations",
    "relay_search_disabled_reason",
    "hub_count",
    "handoff_service_min",
    "solution_file",
    "solution_sha256",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=("official",), default="official")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument(
        "--quick",
        action="store_true",
        help="Use a deterministic generated smoke-test dataset and short search.",
    )
    parser.add_argument(
        "--methods",
        nargs="+",
        choices=DEFAULT_METHODS,
        default=list(DEFAULT_METHODS),
    )
    parser.add_argument("--seeds", nargs="+", type=int)
    parser.add_argument("--tasks", type=int)
    parser.add_argument("--drones", type=int)
    parser.add_argument("--max-tasks", type=int)
    parser.add_argument("--time-limit", type=float)
    parser.add_argument("--wall-safety-margin", type=float)
    parser.add_argument(
        "--task-count-semantics",
        choices=tuple(item.value for item in TaskCountSemantics),
        default=TaskCountSemantics.STRICT_TOUCH.value,
    )
    parser.add_argument("--hub-count", type=int, default=4)
    parser.add_argument("--handoff-service-min", type=float, default=0.5)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    args = _parser().parse_args(argv)
    defaults = (
        {
            "seeds": [DEFAULT_SEEDS[0]],
            "tasks": 8,
            "drones": 4,
            "max_tasks": 2,
            "time_limit": 0.25,
            "wall_safety_margin": 0.02,
        }
        if args.quick
        else {
            "seeds": list(DEFAULT_SEEDS),
            "tasks": 200,
            "drones": 8,
            "max_tasks": 25,
            "time_limit": 240.0,
            "wall_safety_margin": 2.0,
        }
    )
    for name, value in defaults.items():
        if getattr(args, name) is None:
            setattr(args, name, value)
    return args


def _validate_args(args: argparse.Namespace) -> None:
    if len(args.methods) != len(set(args.methods)):
        raise ValueError("实验方法不能重复")
    if not args.seeds or len(args.seeds) != len(set(args.seeds)):
        raise ValueError("seeds 不能为空且不能重复")
    if min(args.tasks, args.drones, args.max_tasks, args.hub_count) <= 0:
        raise ValueError("任务数、无人机数、单机任务上限和交接点数必须为正")
    if "static-multihub" in args.methods and args.hub_count < 2:
        raise ValueError("static-multihub 的 hub-count 至少为 2")
    if args.tasks > args.drones * args.max_tasks:
        raise ValueError("无人机总任务槽位不足以覆盖实验任务")
    if args.time_limit <= 0:
        raise ValueError("总时间预算必须为正")
    if not 0 <= args.wall_safety_margin < args.time_limit:
        raise ValueError("墙钟安全余量必须非负且小于总预算")
    if args.handoff_service_min < 0:
        raise ValueError("交接操作时长不能为负")


def _quick_tasks(count: int) -> tuple[Task, ...]:
    """Build a small deterministic instance without pretending it is official."""

    tasks: list[Task] = []
    for index in range(count):
        task_id = index + 1
        lane = index % 4
        band = index // 4
        pickup = Point(1.0 + lane * 0.8, 0.5 + band * 0.7)
        delivery = Point(2.1 + lane * 0.7, 1.2 + band * 0.8)
        tasks.append(
            Task(
                task_id,
                pickup,
                delivery,
                deadline_min=4.0 + (index % 5) * 0.8,
            )
        )
    return tuple(tasks)


def _task_payload(tasks: Iterable[Task]) -> list[dict[str, Any]]:
    return [
        {
            "id": task.id,
            "pickup": [task.pickup.x, task.pickup.y],
            "delivery": [task.delivery.x, task.delivery.y],
            "deadline_min": task.deadline_min,
        }
        for task in tasks
    ]


def _json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_compatible(payload: Any) -> Any:
    """Materialize immutable nested metadata into JSON-compatible values."""

    if isinstance(payload, Mapping):
        return {
            str(key): _json_compatible(value)
            for key, value in payload.items()
        }
    if isinstance(payload, (tuple, list)):
        return [_json_compatible(value) for value in payload]
    return payload


def _source_code_hashes() -> dict[str, str]:
    """Fingerprint the execution source without relying on Git metadata."""

    return {
        relative_path: hashlib.sha256(
            (ROOT / relative_path).read_bytes()
        ).hexdigest()
        for relative_path in SOURCE_CODE_RELATIVE_PATHS
    }


def _compliance_manifest_fields() -> dict[str, Any]:
    """Describe compliance and aggregation fields in one canonical place."""

    return {
        "compliance_fields": {
            "compliant": "backward-compatible alias of runtime_compliant",
            "runtime_compliant": "runtime_seconds <= budget_seconds",
            "official_semantics_compliant": (
                "task_count_semantics == strict-touch and "
                "semantics_extension == false"
            ),
            "official_plan_compliant": (
                "the persisted plan is valid when independently "
                "re-evaluated under strict-touch semantics"
            ),
        },
        "summary_metric_filter": (
            "valid == true and runtime_compliant == true"
        ),
        "summary_count_fields": {
            "run_count": "all attempted runs",
            "evaluation_valid_run_count": "valid == true",
            "valid_run_count": (
                "valid == true and runtime_compliant == true"
            ),
            "runtime_compliant_run_count": (
                "runtime_compliant == true"
            ),
            "official_semantics_compliant_run_count": (
                "official_semantics_compliant == true"
            ),
            "official_plan_compliant_run_count": (
                "official_plan_compliant == true"
            ),
            "summary_eligible_run_count": (
                "valid == true and runtime_compliant == true"
            ),
        },
    }


def _load_problem(args: argparse.Namespace) -> tuple[Problem, str, str]:
    if args.quick:
        tasks = _quick_tasks(args.tasks)
        source = "generated:relay-quick-v1"
        input_sha256 = _json_sha256(_task_payload(tasks))
    else:
        if args.dataset != "official":  # pragma: no cover - argparse guards this
            raise ValueError(f"不支持的数据集: {args.dataset}")
        tasks_all = load_tasks_csv(args.input)
        if len(tasks_all) < args.tasks:
            raise ValueError(
                f"输入只有 {len(tasks_all)} 个任务，少于要求的 {args.tasks} 个"
            )
        tasks = tuple(tasks_all[: args.tasks])
        source = str(args.input)
        input_sha256 = hashlib.sha256(args.input.read_bytes()).hexdigest()
    problem = Problem(
        tasks,
        drone_count=args.drones,
        max_tasks_per_drone=args.max_tasks,
    )
    problem_sha256 = _json_sha256(
        {
            "tasks": _task_payload(problem.tasks),
            "drone_count": problem.drone_count,
            "max_tasks_per_drone": problem.max_tasks_per_drone,
            "capacity": problem.capacity,
            "speed_km_per_min": problem.speed_km_per_min,
            "depot": [problem.depot.x, problem.depot.y],
        }
    )
    return problem, source, input_sha256 + ":" + problem_sha256


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_csv(
    path: Path,
    rows: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _event_payload(event: Event, event_time_min: float) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": event.task_id,
        "event_type": event.event_type.value,
        "event_time_min": event_time_min,
    }
    if event.hub_id is not None:
        payload["hub_id"] = event.hub_id
    return payload


def _direct_equivalence_audit(
    problem: Problem, result: RelaySolverResult
) -> dict[str, Any]:
    if result.evaluation.relay_count:
        return {
            "checked": False,
            "equivalent": False,
            "reason": "plan_contains_relay_events",
        }
    try:
        signed_routes = result.plan.to_signed_routes()
    except ValueError:
        return {
            "checked": False,
            "equivalent": False,
            "reason": "plan_cannot_be_lowered_to_signed_routes",
        }
    legacy = evaluate_solution(problem, signed_routes)
    relay = result.evaluation
    delivery_deltas = [
        abs(legacy.delivery_times_min[task_id] - relay.delivery_times_min[task_id])
        for task_id in problem.task_ids
        if task_id in legacy.delivery_times_min
        and task_id in relay.delivery_times_min
    ]
    lateness_delta = abs(
        legacy.score.total_lateness_min - relay.score.total_lateness_min
    )
    distance_delta = abs(legacy.score.distance_km - relay.score.distance_km)
    equivalent = (
        legacy.valid == relay.valid
        and legacy.score.late_count == relay.score.late_count
        and lateness_delta <= 1e-9
        and distance_delta <= 1e-9
        and set(legacy.delivery_times_min) == set(relay.delivery_times_min)
        and max(delivery_deltas, default=0.0) <= 1e-9
        and len(legacy.route_distances_km) == len(relay.route_distances_km)
        and all(
            math.isclose(left, right, rel_tol=0.0, abs_tol=1e-9)
            for left, right in zip(
                legacy.route_distances_km,
                relay.route_distances_km,
                strict=True,
            )
        )
        and len(legacy.route_completion_times_min)
        == len(relay.route_completion_times_min)
        and all(
            math.isclose(left, right, rel_tol=0.0, abs_tol=1e-9)
            for left, right in zip(
                legacy.route_completion_times_min,
                relay.route_completion_times_min,
                strict=True,
            )
        )
    )
    return {
        "checked": True,
        "equivalent": equivalent,
        "late_count_delta": relay.score.late_count - legacy.score.late_count,
        "total_lateness_abs_delta_min": lateness_delta,
        "distance_abs_delta_km": distance_delta,
        "max_delivery_time_abs_delta_min": max(delivery_deltas, default=0.0),
        "legacy_score": {
            "late_count": legacy.score.late_count,
            "total_lateness_min": legacy.score.total_lateness_min,
            "distance_km": legacy.score.distance_km,
        },
        "event_score": {
            "late_count": relay.score.late_count,
            "total_lateness_min": relay.score.total_lateness_min,
            "distance_km": relay.score.distance_km,
        },
    }


def _relay_problem(
    method: str, problem: Problem, args: argparse.Namespace
) -> RelayProblem:
    if method == "baseline":
        hubs = ()
        max_handoffs = 0
    elif method == "relay-disabled":
        hubs = generate_flow_hubs(problem, 1)
        max_handoffs = 0
    elif method == "static-1hub":
        hubs = generate_flow_hubs(problem, 1)
        max_handoffs = 1
    elif method == "static-multihub":
        hubs = generate_flow_hubs(problem, args.hub_count)
        max_handoffs = 1
    else:  # pragma: no cover - parser and validation constrain methods
        raise ValueError(f"未知方法: {method}")
    return RelayProblem(
        problem,
        hubs=hubs,
        handoff_service_min=args.handoff_service_min,
        task_count_semantics=args.task_count_semantics,
        max_handoffs_per_task=max_handoffs,
    )


def _solution_payload(
    *,
    method: str,
    seed: int,
    source: str,
    input_sha256: str,
    problem_sha256: str,
    relay_problem: RelayProblem,
    result: RelaySolverResult,
    budget_seconds: float,
    effective_time_limit: float,
    runtime_compliant: bool,
    official_semantics_compliant: bool,
    official_plan_compliant: bool,
    official_plan_audit: Mapping[str, Any],
    source_code_sha256: Mapping[str, str],
    source_snapshot_sha256: str,
    source_snapshot_role: str,
    direct_event_equivalent: bool,
    direct_equivalence_audit: Mapping[str, Any],
) -> dict[str, Any]:
    evaluation = result.evaluation
    event_routes = [
        [
            _event_payload(event, event_time)
            for event, event_time in zip(route, times, strict=True)
        ]
        for route, times in zip(
            result.plan.routes, evaluation.event_times_min, strict=True
        )
    ]
    return {
        "experiment": {
            "method": method,
            "seed": seed,
            "semantics": relay_problem.task_count_semantics.value,
            "semantics_extension": relay_problem.semantics_extension,
            "source": source,
            "input_sha256": input_sha256,
            "problem_sha256": problem_sha256,
            "budget_seconds": budget_seconds,
            "effective_search_time_limit_seconds": effective_time_limit,
            "valid": evaluation.valid,
            "runtime_compliant": runtime_compliant,
            "official_semantics_compliant": official_semantics_compliant,
            "official_plan_compliant": official_plan_compliant,
            "compliant": runtime_compliant,
            "source_code_sha256": dict(source_code_sha256),
            "source_snapshot_sha256": source_snapshot_sha256,
            "source_snapshot_role": source_snapshot_role,
            "direct_event_equivalent": direct_event_equivalent,
        },
        "problem": {
            "task_count": len(relay_problem.base_problem.tasks),
            "drone_count": relay_problem.base_problem.drone_count,
            "max_tasks_per_drone": (
                relay_problem.base_problem.max_tasks_per_drone
            ),
            "capacity": relay_problem.base_problem.capacity,
            "speed_km_per_min": (
                relay_problem.base_problem.speed_km_per_min
            ),
            "tasks": _task_payload(relay_problem.base_problem.tasks),
            "handoff_service_min": relay_problem.handoff_service_min,
            "hubs": [
                {"id": hub.id, "x": hub.point.x, "y": hub.point.y}
                for hub in relay_problem.hubs
            ],
        },
        "score": {
            "on_time_count": (
                len(relay_problem.base_problem.tasks)
                - evaluation.score.late_count
            ),
            "on_time_rate": evaluation.on_time_rate,
            "distance_km": evaluation.score.distance_km,
        },
        "diagnostics": {
            "late_count": evaluation.score.late_count,
            "total_lateness_min": evaluation.score.total_lateness_min,
            "max_lateness_min": evaluation.max_lateness_min,
            "relay_count": evaluation.relay_count,
            "direct_task_count": evaluation.direct_task_count,
            "package_wait_times_min": dict(evaluation.package_wait_times_min),
            "uav_wait_times_min": dict(evaluation.uav_wait_times_min),
            "max_hub_inventory": evaluation.max_hub_inventory,
            "hub_peak_inventory": dict(evaluation.hub_peak_inventory),
            "physical_touch_counts": list(evaluation.physical_touch_counts),
            "primary_owner_counts": list(evaluation.primary_owner_counts),
            "delivery_times_min": dict(evaluation.delivery_times_min),
            "route_distances_km": list(evaluation.route_distances_km),
            "route_completion_times_min": list(
                evaluation.route_completion_times_min
            ),
            "violations": list(evaluation.violations),
        },
        "runtime_seconds": result.runtime_seconds,
        "iterations": result.iterations,
        "solver_metadata": _json_compatible(result.metadata),
        "official_plan_audit": _json_compatible(official_plan_audit),
        "direct_equivalence_audit": dict(direct_equivalence_audit),
        "event_routes": event_routes,
    }


def _summarize(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[
            (str(row["method"]), str(row["semantics"]), int(row["task_count"]))
        ].append(row)
    summaries: list[dict[str, Any]] = []
    for (method, semantics, task_count), group in sorted(grouped.items()):
        eligible = [
            row
            for row in group
            if bool(row["valid"]) and bool(row["runtime_compliant"])
        ]
        on_time_counts = [float(row["on_time_count"]) for row in eligible]
        on_time_rates = [float(row["on_time_rate"]) for row in eligible]
        distances = [float(row["distance_km"]) for row in eligible]
        runtimes = [float(row["runtime_seconds"]) for row in eligible]
        relays = [float(row["relay_count"]) for row in eligible]

        def mean_or_none(values: Sequence[float]) -> float | None:
            return statistics.fmean(values) if values else None

        def min_or_none(values: Sequence[float]) -> float | None:
            return min(values) if values else None

        def max_or_none(values: Sequence[float]) -> float | None:
            return max(values) if values else None

        summaries.append(
            {
                "method": method,
                "semantics": semantics,
                "task_count": task_count,
                "run_count": len(group),
                "evaluation_valid_run_count": sum(
                    bool(row["valid"]) for row in group
                ),
                "valid_run_count": len(eligible),
                "runtime_compliant_run_count": sum(
                    bool(row["runtime_compliant"]) for row in group
                ),
                "official_semantics_compliant_run_count": sum(
                    bool(row["official_semantics_compliant"])
                    for row in group
                ),
                "official_plan_compliant_run_count": sum(
                    bool(row["official_plan_compliant"])
                    for row in group
                ),
                "summary_eligible_run_count": len(eligible),
                "on_time_count_mean": mean_or_none(on_time_counts),
                "on_time_count_min": min_or_none(on_time_counts),
                "on_time_count_max": max_or_none(on_time_counts),
                "on_time_rate_mean": mean_or_none(on_time_rates),
                "on_time_rate_min": min_or_none(on_time_rates),
                "on_time_rate_max": max_or_none(on_time_rates),
                "distance_km_mean": mean_or_none(distances),
                "distance_km_min": min_or_none(distances),
                "distance_km_max": max_or_none(distances),
                "runtime_seconds_mean": mean_or_none(runtimes),
                "budget_seconds": float(group[0]["budget_seconds"]),
                "relay_count_mean": mean_or_none(relays),
            }
        )
    return summaries


def run(args: argparse.Namespace) -> dict[str, Any]:
    """Execute every method/seed pair and persist independently auditable output."""

    _validate_args(args)
    problem, source, hashes = _load_problem(args)
    input_sha256, problem_sha256 = hashes.split(":", maxsplit=1)
    output_dir: Path = args.output_dir
    solution_dir = output_dir / "run_solutions"
    effective_time_limit = args.time_limit - args.wall_safety_margin
    max_iterations = 24 if args.quick else 10_000
    source_code_sha256 = _source_code_hashes()
    source_snapshot_sha256 = _json_sha256(source_code_sha256)
    rows: list[dict[str, Any]] = []

    for seed in args.seeds:
        for method in args.methods:
            relay_problem = _relay_problem(method, problem, args)
            result = solve_relay_alns(
                relay_problem,
                config=RelayALNSConfig(
                    max_iterations=max_iterations,
                    time_limit_seconds=effective_time_limit,
                    seed=seed,
                ),
            )
            evaluation = result.evaluation
            runtime_compliant = result.runtime_seconds <= args.time_limit
            official_semantics_compliant = (
                relay_problem.task_count_semantics
                is TaskCountSemantics.STRICT_TOUCH
                and not relay_problem.semantics_extension
            )
            official_problem = RelayProblem(
                problem,
                hubs=relay_problem.hubs,
                handoff_service_min=relay_problem.handoff_service_min,
                task_count_semantics=TaskCountSemantics.STRICT_TOUCH,
                max_handoffs_per_task=relay_problem.max_handoffs_per_task,
            )
            official_plan_evaluation = evaluate_relay_plan(
                official_problem, result.plan
            )
            official_plan_compliant = official_plan_evaluation.valid
            official_plan_audit = {
                "task_count_semantics": TaskCountSemantics.STRICT_TOUCH.value,
                "valid": official_plan_evaluation.valid,
                "violations": list(official_plan_evaluation.violations),
                "physical_touch_counts": list(
                    official_plan_evaluation.physical_touch_counts
                ),
            }
            equivalence_audit = _direct_equivalence_audit(problem, result)
            direct_equivalent = bool(equivalence_audit["equivalent"])
            if method in {"baseline", "relay-disabled"} and not direct_equivalent:
                raise RuntimeError(
                    f"{method} seed {seed} 的事件层与 legacy 直接路线不等价"
                )
            solution_path = (
                solution_dir
                / f"{method}__{relay_problem.task_count_semantics.value}"
                f"__seed_{seed}.json"
            )
            solution_payload = _solution_payload(
                method=method,
                seed=seed,
                source=source,
                input_sha256=input_sha256,
                problem_sha256=problem_sha256,
                relay_problem=relay_problem,
                result=result,
                budget_seconds=args.time_limit,
                effective_time_limit=effective_time_limit,
                runtime_compliant=runtime_compliant,
                official_semantics_compliant=(
                    official_semantics_compliant
                ),
                official_plan_compliant=official_plan_compliant,
                official_plan_audit=official_plan_audit,
                source_code_sha256=source_code_sha256,
                source_snapshot_sha256=source_snapshot_sha256,
                source_snapshot_role="execution_source",
                direct_event_equivalent=direct_equivalent,
                direct_equivalence_audit=equivalence_audit,
            )
            _write_json(solution_path, solution_payload)
            metadata = result.metadata
            row = {
                "method": method,
                "semantics": relay_problem.task_count_semantics.value,
                "task_count": len(problem.tasks),
                "seed": seed,
                "valid": evaluation.valid,
                "runtime_compliant": runtime_compliant,
                "official_semantics_compliant": (
                    official_semantics_compliant
                ),
                "official_plan_compliant": official_plan_compliant,
                "compliant": runtime_compliant,
                "semantics_extension": relay_problem.semantics_extension,
                "source_snapshot_sha256": source_snapshot_sha256,
                "source_snapshot_role": "execution_source",
                **{
                    csv_field: source_code_sha256[relative_path]
                    for relative_path, csv_field in (
                        SOURCE_CODE_CSV_FIELDS.items()
                    )
                },
                "input_sha256": input_sha256,
                "problem_sha256": problem_sha256,
                "budget_seconds": args.time_limit,
                "effective_search_time_limit_seconds": effective_time_limit,
                "on_time_count": len(problem.tasks) - evaluation.score.late_count,
                "on_time_rate": evaluation.on_time_rate,
                "late_count": evaluation.score.late_count,
                "total_lateness_min": evaluation.score.total_lateness_min,
                "distance_km": evaluation.score.distance_km,
                "runtime_seconds": result.runtime_seconds,
                "iterations": result.iterations,
                "relay_count": evaluation.relay_count,
                "direct_task_count": evaluation.direct_task_count,
                "direct_event_equivalent": direct_equivalent,
                "base_search_used": metadata.get("base_search_used", False),
                "base_search_runtime_seconds": metadata.get(
                    "base_search_runtime_seconds", 0.0
                ),
                "base_search_iterations": metadata.get(
                    "base_search_iterations", 0
                ),
                "relay_search_disabled_reason": metadata.get(
                    "relay_search_disabled_reason", ""
                ),
                "hub_count": len(relay_problem.hubs),
                "handoff_service_min": relay_problem.handoff_service_min,
                "solution_file": str(solution_path.relative_to(output_dir)),
                "solution_sha256": hashlib.sha256(
                    solution_path.read_bytes()
                ).hexdigest(),
            }
            rows.append(row)
            print(
                f"[{len(rows)}/{len(args.methods) * len(args.seeds)}] "
                f"{method} seed={seed} on_time={row['on_time_count']}/"
                f"{row['task_count']} distance={row['distance_km']:.6f}km "
                f"runtime={row['runtime_seconds']:.3f}s relay={row['relay_count']}",
                flush=True,
            )

    summaries = _summarize(rows)
    _write_csv(output_dir / "relay_runs.csv", rows, RUN_FIELDS)
    _write_csv(output_dir / "relay_summary.csv", summaries, SUMMARY_FIELDS)
    payload = {
        "manifest": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "dataset": "quick" if args.quick else args.dataset,
            "source": source,
            "input_sha256": input_sha256,
            "problem_sha256": problem_sha256,
            "source_code_sha256": source_code_sha256,
            "source_snapshot_sha256": source_snapshot_sha256,
            "source_snapshot_role": "execution_source",
            "artifact_schema_version": 2,
            "artifact_generation_mode": "solver_run",
            "objective_order": ["on_time_count", "distance_km"],
            "diagnostic_only": ["late_count", "total_lateness_min"],
            "baseline_definition": {
                "family": "A2",
                "enable_late_risk_destroy": True,
                "objective_order": ["on_time_count", "distance_km"],
            },
            "budget_includes_direct_base_search": True,
            **_compliance_manifest_fields(),
            "methods": list(args.methods),
            "seeds": list(args.seeds),
            "task_count_semantics": args.task_count_semantics,
            "semantics_extension": (
                args.task_count_semantics
                == TaskCountSemantics.PRIMARY_OWNER.value
            ),
            "budget_seconds": args.time_limit,
            "wall_safety_margin_seconds": args.wall_safety_margin,
            "effective_search_time_limit_seconds": effective_time_limit,
        },
        "runs": rows,
        "summaries": summaries,
    }
    _write_json(output_dir / "relay_results.json", payload)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    run(_parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
