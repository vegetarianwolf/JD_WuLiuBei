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
from .relay import (
    GlobalRelayEvaluator,
    build_plan_index,
    build_relay_network,
    relay_statistics,
)
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
        delivered_tasks = len(route_metrics.delivery_times_min)
        relay_drops = 0
        relay_pickups = 0
        visits = []
        for visit in route:
            leg = (
                problem.leg_registry.get(abs(visit))
                if problem.has_relays
                else None
            )
            if leg is not None:
                if visit > 0:
                    action = (
                        "relay_pickup" if leg.kind == "RELAY_OUT" else "pickup"
                    )
                    kind = (
                        "RELAY_PICKUP"
                        if leg.kind == "RELAY_OUT"
                        else "PICKUP"
                    )
                    if leg.kind == "RELAY_OUT":
                        relay_pickups += 1
                else:
                    action = (
                        "relay_drop" if leg.kind == "RELAY_IN" else "delivery"
                    )
                    kind = (
                        "RELAY_DROP" if leg.kind == "RELAY_IN" else "DELIVERY"
                    )
                    if leg.kind == "RELAY_IN":
                        relay_drops += 1
                visits.append(
                    {
                        "task_id": leg.task_id,
                        "leg_id": leg.id,
                        "action": action,
                        "kind": kind,
                        "relay_id": leg.relay_id,
                    }
                )
            else:
                visits.append(
                    {
                        "task_id": abs(visit),
                        "leg_id": None,
                        "action": "pickup" if visit > 0 else "delivery",
                        "kind": "PICKUP" if visit > 0 else "DELIVERY",
                        "relay_id": None,
                    }
                )
        routes.append(
            {
                "drone_id": route_index + 1,
                "task_count": delivered_tasks,
                "transport_leg_count": len(route) // 2,
                "relay_drop_count": relay_drops,
                "relay_pickup_count": relay_pickups,
                "distance_km": evaluation.route_distances_km[route_index],
                "completion_time_min": evaluation.route_completion_times_min[
                    route_index
                ],
                "late_count": route_metrics.score.late_count,
                "total_lateness_min": route_metrics.score.total_lateness_min,
                "on_time_task_count": delivered_tasks
                - route_metrics.score.late_count,
                "encoded_visits": list(route),
                "visits": visits,
            }
        )
    relay_block: dict[str, Any]
    task_plans: dict[str, Any]
    plan_index = build_plan_index(problem, result.routes)
    schedule = GlobalRelayEvaluator(problem).evaluate(result.routes)
    relay_block = _jsonable(
        relay_statistics(problem, result.routes, schedule, plan_index)
    )
    task_plans = {
        str(task_id): {
            "mode": plan.mode,
            "relay_id": plan.relay_id,
            "leg_ids": list(plan.leg_ids),
        }
        for task_id, plan in sorted(plan_index.task_plans.items())
    }
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
        "iterations_per_second": result.metadata.get(
            "iterations_per_second", 0.0
        ),
        "relay": relay_block,
        "task_plans": task_plans,
        "metadata": _jsonable(result.metadata),
        "delivery_times_min": {
            str(task_id): delivered_at
            for task_id, delivered_at in sorted(
                evaluation.delivery_times_min.items()
            )
        },
        "routes": routes,
    }


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
    solve.add_argument("--relay-count", type=int, default=3)
    solve.add_argument("--relay-candidates-per-task", type=int, default=2)
    solve.add_argument("--relay-detour-ratio", type=float, default=1.3)
    solve.add_argument("--relay-plan-beam", type=int, default=4)
    solve.add_argument("--relay-leg-beam", type=int, default=6)
    solve.add_argument("--relay-event-cap", type=int, default=60)
    solve.add_argument("--relay-sample-every", type=int, default=4)
    solve.add_argument("--relay-global-limit", type=int, default=3)
    solve.add_argument("--relay-location-seed", type=int, default=42)
    solve.add_argument("--disable-relay", action="store_true")
    solve.add_argument("--relay-debug", action="store_true")
    return parser


def _solve(args: argparse.Namespace) -> SolverResult:
    tasks = load_tasks_csv(args.input)
    if args.tasks is not None:
        if args.tasks <= 0:
            raise ValueError("--tasks 必须为正整数")
        tasks = tasks[: args.tasks]
    relay_count = 0 if args.disable_relay else args.relay_count
    network = build_relay_network(
        tasks,
        relay_count=relay_count,
        candidates_per_task=args.relay_candidates_per_task,
        detour_ratio=args.relay_detour_ratio,
        location_seed=args.relay_location_seed,
    )
    problem = Problem(
        tasks,
        drone_count=args.drones,
        max_tasks_per_drone=args.max_tasks,
        capacity=args.capacity,
        speed_km_per_min=args.speed_km_per_min,
        depot=Point(args.depot_x, args.depot_y),
        relay_stations=network.stations,
        leg_registry=network.leg_registry,
        task_relay_candidates=network.task_relay_candidates,
    )
    candidate_limit = None if args.candidate_limit == 0 else args.candidate_limit
    if args.method == "exact":
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
            relay_candidates_per_task=args.relay_candidates_per_task,
            relay_plan_beam=args.relay_plan_beam,
            relay_leg_beam=args.relay_leg_beam,
            relay_event_cap=args.relay_event_cap,
            relay_sample_every=args.relay_sample_every,
            relay_global_limit=args.relay_global_limit,
            relay_debug=args.relay_debug,
        )
        if args.method in {"alns-core", "basic-alns"}:
            result = solve_alns_core(problem, config=config)
        else:
            result = solve_alns(problem, config=config)
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
