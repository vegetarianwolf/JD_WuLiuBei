"""Command-line interface for solving and serialising dispatch instances."""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .alns import (
    ALNSConfig,
    construct_edd_adjacent,
    construct_greedy_initial,
    construct_nearest_adjacent,
    construct_regret_initial,
    solve_alns,
    solve_alns_core,
)
from .exact import solve_exact
from .io import load_tasks_csv
from .model import Point, Problem, Score
from .relay import Hub, RelayProblem, TaskCountSemantics
from .relay_alns import RelayALNSConfig, RelaySolverResult, solve_relay_alns
from .relay_search import generate_flow_hubs
from .search import RouteEvaluator, SolverResult


def _jsonable(value: Any) -> Any:
    if isinstance(value, Score):
        return {
            "late_count": value.late_count,
            "total_lateness_min": value.total_lateness_min,
            "distance_km": value.distance_km,
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_jsonable(item) for item in value]
    return value


def result_payload(
    problem: Problem,
    result: SolverResult,
    *,
    source: str | Path,
) -> dict[str, Any]:
    """Create a self-contained, JSON-serialisable result document."""

    evaluation = result.evaluation
    route_evaluator = RouteEvaluator(problem)
    routes = []
    for route_index, route in enumerate(result.routes):
        route_metrics = route_evaluator.evaluate(route)
        routes.append(
            {
                "drone_id": route_index + 1,
                "task_count": sum(visit > 0 for visit in route),
                "distance_km": evaluation.route_distances_km[route_index],
                "completion_time_min": evaluation.route_completion_times_min[
                    route_index
                ],
                "late_count": route_metrics.score.late_count,
                "total_lateness_min": route_metrics.score.total_lateness_min,
                "on_time_task_count": sum(visit > 0 for visit in route)
                - route_metrics.score.late_count,
                "encoded_visits": list(route),
                "visits": [
                    {
                        "task_id": abs(visit),
                        "action": "pickup" if visit > 0 else "delivery",
                    }
                    for visit in route
                ],
            }
        )
    return {
        "source": str(source),
        "problem": {
            "task_count": len(problem.tasks),
            "drone_count": problem.drone_count,
            "max_tasks_per_drone": problem.max_tasks_per_drone,
            "capacity": problem.capacity,
            "speed_km_per_min": problem.speed_km_per_min,
            "depot_km": [problem.depot.x, problem.depot.y],
            "open_routes": True,
            "service_time_min": 0.0,
            "objective_order": [
                "late_count",
                "total_lateness_min",
                "distance_km",
            ],
        },
        "valid": evaluation.valid,
        "violations": list(evaluation.violations),
        "score": _jsonable(evaluation.score),
        "on_time_rate": evaluation.on_time_rate,
        "max_lateness_min": evaluation.max_lateness_min,
        "max_route_distance_km": evaluation.max_route_distance_km,
        "max_route_completion_min": evaluation.max_route_completion_min,
        "runtime_seconds": result.runtime_seconds,
        "iterations": result.iterations,
        "metadata": _jsonable(result.metadata),
        "delivery_times_min": {
            str(task_id): delivered_at
            for task_id, delivered_at in sorted(
                evaluation.delivery_times_min.items()
            )
        },
        "routes": routes,
    }


def relay_result_payload(
    problem: RelayProblem,
    result: RelaySolverResult,
    *,
    source: str | Path,
) -> dict[str, Any]:
    """Create a relay-aware result without passing events to RouteEvaluator."""

    base = problem.base_problem
    evaluation = result.evaluation
    routes = []
    for route_index, (route, event_times) in enumerate(
        zip(result.plan.routes, evaluation.event_times_min, strict=True)
    ):
        routes.append(
            {
                "drone_id": route_index + 1,
                "physical_touch_count": evaluation.physical_touch_counts[
                    route_index
                ],
                "primary_owner_count": evaluation.primary_owner_counts[
                    route_index
                ],
                "distance_km": evaluation.route_distances_km[route_index],
                "completion_time_min": (
                    evaluation.route_completion_times_min[route_index]
                ),
                "events": [
                    {
                        "task_id": event.task_id,
                        "action": event.event_type.value,
                        **(
                            {"hub_id": event.hub_id}
                            if event.hub_id is not None
                            else {}
                        ),
                        "time_min": event_time,
                    }
                    for event, event_time in zip(
                        route, event_times, strict=True
                    )
                ],
            }
        )
    on_time_count = len(base.tasks) - evaluation.score.late_count
    return {
        "source": str(source),
        "problem": {
            "task_count": len(base.tasks),
            "drone_count": base.drone_count,
            "max_tasks_per_drone": base.max_tasks_per_drone,
            "capacity": base.capacity,
            "speed_km_per_min": base.speed_km_per_min,
            "depot_km": [base.depot.x, base.depot.y],
            "open_routes": True,
            "service_time_min": 0.0,
            "objective_order": ["late_count", "distance_km"],
            "diagnostic_metrics": ["total_lateness_min"],
        },
        "valid": evaluation.valid,
        "violations": list(evaluation.violations),
        "score": _jsonable(evaluation.score),
        "on_time_count": on_time_count,
        "on_time_rate": evaluation.on_time_rate,
        "runtime_seconds": result.runtime_seconds,
        "iterations": result.iterations,
        "metadata": _jsonable(result.metadata),
        "relay": {
            "mode": (
                "disabled"
                if problem.max_handoffs_per_task == 0
                else "static-buffered"
            ),
            "task_count_semantics": evaluation.task_count_semantics.value,
            "semantics_extension": evaluation.semantics_extension,
            "handoff_service_min": problem.handoff_service_min,
            "max_handoffs_per_task": problem.max_handoffs_per_task,
            "hubs": [
                {
                    "id": hub.id,
                    "point_km": [hub.point.x, hub.point.y],
                }
                for hub in problem.hubs
            ],
            "relay_count": evaluation.relay_count,
            "direct_task_count": evaluation.direct_task_count,
            "package_wait_min": evaluation.package_wait_min,
            "uav_wait_min": evaluation.uav_wait_min,
            "max_hub_inventory": evaluation.max_hub_inventory,
        },
        "delivery_times_min": {
            str(task_id): delivered_at
            for task_id, delivered_at in sorted(
                evaluation.delivery_times_min.items()
            )
        },
        "routes": routes,
    }


def _parse_hub(value: str) -> Hub:
    """Parse ``ID:X:Y`` into a fixed relay hub."""

    parts = value.rsplit(":", maxsplit=2)
    if len(parts) != 3 or not parts[0].strip():
        raise argparse.ArgumentTypeError("交接点必须使用 ID:X:Y 格式")
    try:
        point = Point(float(parts[1]), float(parts[2]))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("交接点坐标必须为数字") from exc
    return Hub(parts[0].strip(), point)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uav-dispatch",
        description="容量二、开放式物流无人机取送货调度求解器",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    solve = subparsers.add_parser("solve", help="求解 CSV 任务实例")
    solve.add_argument("--input", type=Path, required=True)
    solve.add_argument("--output", type=Path)
    solve.add_argument("--method", choices=(
        "exact",
        "edd",
        "nearest",
        "greedy",
        "regret2",
        "alns-core",
        "basic-alns",
        "halns",
        "relay-alns",
    ), default="halns")
    solve.add_argument("--tasks", type=int)
    solve.add_argument("--drones", type=int, default=8)
    solve.add_argument("--max-tasks", type=int, default=25)
    solve.add_argument("--capacity", type=int, default=2)
    solve.add_argument("--speed-km-per-min", type=float, default=0.9)
    solve.add_argument("--depot-x", type=float, default=0.0)
    solve.add_argument("--depot-y", type=float, default=0.0)
    solve.add_argument("--iterations", type=int, default=400)
    solve.add_argument("--time-limit", type=float)
    solve.add_argument("--seed", type=int, default=20260805)
    solve.add_argument("--candidate-limit", type=int, default=48)
    solve.add_argument("--exact-max-tasks", type=int, default=10)
    solve.add_argument(
        "--relay-hub",
        type=_parse_hub,
        action="append",
        default=[],
        help="固定缓冲交接点，格式 ID:X:Y；可重复指定",
    )
    solve.add_argument(
        "--relay-mode",
        choices=("disabled", "static-buffered"),
        default="static-buffered",
    )
    solve.add_argument("--relay-hub-count", type=int, default=1)
    solve.add_argument("--handoff-service-min", type=float, default=0.5)
    solve.add_argument("--max-handoffs-per-task", type=int, choices=(0, 1), default=1)
    solve.add_argument(
        "--relay-task-count-semantics",
        choices=tuple(item.value for item in TaskCountSemantics),
        default=TaskCountSemantics.STRICT_TOUCH.value,
    )
    solve.add_argument("--relay-task-sample-size", type=int, default=24)
    solve.add_argument("--relay-base-search-fraction", type=float, default=0.8)
    return parser


def _solve(args: argparse.Namespace) -> SolverResult | RelaySolverResult:
    tasks = load_tasks_csv(args.input)
    if args.tasks is not None:
        if args.tasks <= 0:
            raise ValueError("--tasks 必须为正整数")
        tasks = tasks[: args.tasks]
    problem = Problem(
        tasks,
        drone_count=args.drones,
        max_tasks_per_drone=args.max_tasks,
        capacity=args.capacity,
        speed_km_per_min=args.speed_km_per_min,
        depot=Point(args.depot_x, args.depot_y),
    )
    candidate_limit = None if args.candidate_limit == 0 else args.candidate_limit
    if args.method == "relay-alns":
        if args.relay_hub_count <= 0:
            raise ValueError("--relay-hub-count 必须为正整数")
        relay_candidate_limit = (
            12 if candidate_limit is None else candidate_limit
        )
        hubs = tuple(args.relay_hub) or generate_flow_hubs(
            problem, args.relay_hub_count
        )
        relay_problem = RelayProblem(
            problem,
            hubs=hubs,
            handoff_service_min=args.handoff_service_min,
            task_count_semantics=args.relay_task_count_semantics,
            max_handoffs_per_task=(
                0
                if args.relay_mode == "disabled"
                else args.max_handoffs_per_task
            ),
        )
        relay_result = solve_relay_alns(
            relay_problem,
            config=RelayALNSConfig(
                max_iterations=args.iterations,
                time_limit_seconds=args.time_limit,
                seed=args.seed,
                candidate_limit=relay_candidate_limit,
                task_sample_size=args.relay_task_sample_size,
                base_search_fraction=args.relay_base_search_fraction,
            ),
        )
        payload = relay_result_payload(
            relay_problem, relay_result, source=args.input
        )
        result: SolverResult | RelaySolverResult = relay_result
    elif args.method == "exact":
        result = solve_exact(problem, max_tasks=args.exact_max_tasks)
    elif args.method == "edd":
        result = construct_edd_adjacent(problem)
    elif args.method == "nearest":
        result = construct_nearest_adjacent(problem)
    elif args.method == "greedy":
        result = construct_greedy_initial(
            problem, candidate_limit=candidate_limit
        )
    elif args.method == "regret2":
        result = construct_regret_initial(
            problem, candidate_limit=candidate_limit
        )
    else:
        config = ALNSConfig(
            max_iterations=args.iterations,
            time_limit_seconds=args.time_limit,
            seed=args.seed,
            candidate_limit=candidate_limit,
        )
        if args.method in {"alns-core", "basic-alns"}:
            result = solve_alns_core(problem, config=config)
        else:
            result = solve_alns(problem, config=config)
    if args.method != "relay-alns":
        payload = result_payload(problem, result, source=args.input)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "solve":
        _solve(args)
        return 0
    raise RuntimeError(f"未知命令 {args.command}")
