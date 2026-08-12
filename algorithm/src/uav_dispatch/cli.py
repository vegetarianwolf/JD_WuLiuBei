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
from .cooperative_search import (
    CooperativeConfig,
    CooperativeResult,
    solve_dynamic_cooperative,
)
from .cooperative_validation import CooperativeSolution
from .dynamic_relay import DynamicRelayConfig
from .exact import solve_exact
from .io import load_tasks_csv
from .model import Point, Problem, Score
from .search import RouteEvaluator, SolverResult
from .relay_model import RelaySolution, relay_solution_from_routes
from .relay_search import (
    RelaySearchConfig,
    RelaySolverResult,
    solve_relay,
)
from .swap_model import meeting_node_label
from .swap_search import SwapConfig, SwapSolverResult, solve_swap


def _jsonable(value: Any) -> Any:
    if isinstance(value, Score):
        return {
            "late_count": value.late_count,
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
                "total_lateness_min": route_metrics.total_lateness_min,
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
                "distance_km",
            ],
        },
        "valid": evaluation.valid,
        "violations": list(evaluation.violations),
        "score": _jsonable(evaluation.score),
        "on_time_rate": evaluation.on_time_rate,
        "total_lateness_min": evaluation.total_lateness_min,
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


def swap_result_payload(
    problem: Problem,
    result: SwapSolverResult,
    *,
    source: str | Path,
) -> dict[str, Any]:
    """Create a self-contained JSON document for a Swap-only solution."""

    evaluation = result.evaluation
    routes = []
    for route_index, route in enumerate(result.routes):
        routes.append(
            {
                "drone_id": route_index + 1,
                "task_count": sum(visit > 0 for visit in route),
                "pickup_count": sum(visit > 0 for visit in route),
                "delivery_count": sum(visit < 0 for visit in route),
                "distance_km": (
                    evaluation.route_distances_km[route_index]
                    if route_index < len(evaluation.route_distances_km)
                    else 0.0
                ),
                "completion_time_min": (
                    evaluation.route_completion_times_min[route_index]
                    if route_index
                    < len(evaluation.route_completion_times_min)
                    else 0.0
                ),
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
    metadata = dict(result.metadata)
    swaps = [
        {
            "swap_id": timing.swap_id,
            "drone_a": timing.drone_a,
            "drone_b": timing.drone_b,
            "task_a_to_b": timing.task_a_to_b,
            "task_b_to_a": timing.task_b_to_a,
            "meeting_node": meeting_node_label(timing.meeting_node),
            "position_a": timing.position_a,
            "position_b": timing.position_b,
            "arrival_a_min": timing.arrival_a_min,
            "arrival_b_min": timing.arrival_b_min,
            "swap_time_min": timing.swap_time_min,
            "swap_end_min": timing.swap_end_min,
            "waiting_a_min": timing.waiting_a_min,
            "waiting_b_min": timing.waiting_b_min,
        }
        for timing in evaluation.swap_timings
    ]
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
                "distance_km",
            ],
        },
        "valid": evaluation.valid,
        "violations": list(evaluation.violations),
        "score": _jsonable(evaluation.score),
        "on_time_rate": evaluation.on_time_rate,
        "total_lateness_min": evaluation.total_lateness_min,
        "max_lateness_min": evaluation.max_lateness_min,
        "waiting_time_min": evaluation.waiting_time_min,
        "max_route_distance_km": max(evaluation.route_distances_km, default=0.0),
        "max_route_completion_min": max(
            evaluation.route_completion_times_min, default=0.0
        ),
        "runtime_seconds": result.runtime_seconds,
        "iterations": result.iterations,
        "metadata": _jsonable(metadata),
        "delivery_times_min": {
            str(task_id): delivered_at
            for task_id, delivered_at in sorted(
                evaluation.delivery_times_min.items()
            )
        },
        "routes": routes,
        "swap_model": {
            "enabled": True,
            "swap_count": evaluation.swap_count,
            "swap_service_time_min": float(
                metadata.get("swap_service_time_min", 0.0)
            ),
            "max_swaps": int(metadata.get("max_swaps", 20)),
        },
        "swaps": swaps,
    }


def relay_result_payload(
    problem: Problem,
    result: RelaySolverResult,
    *,
    source: str | Path,
) -> dict[str, Any]:
    """Create a self-contained JSON document for a Relay solution."""

    evaluation = result.evaluation
    routes = []
    for route_index, route in enumerate(result.routes):
        routes.append(
            {
                "drone_id": route_index + 1,
                "task_count": sum(visit > 0 for visit in route),
                "delivery_count": sum(visit < 0 for visit in route),
                "distance_km": evaluation.route_distances_km[route_index],
                "completion_time_min": evaluation.route_completion_times_min[
                    route_index
                ],
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
    stations = [
        {
            "station_id": station.id,
            "source_visit": station.source_visit,
            "point_km": [station.point.x, station.point.y],
        }
        for station in result.stations
    ]
    relays = [
        {
            "relay_id": relay.id,
            "task_id": relay.task_id,
            "station_id": relay.station_id,
            "first_drone": relay.first_drone + 1,
            "second_drone": relay.second_drone + 1,
            "drop_position": relay.drop_position,
            "pick_position": relay.pick_position,
        }
        for relay in result.relays
    ]
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
                "distance_km",
            ],
        },
        "valid": evaluation.valid,
        "violations": list(evaluation.violations),
        "score": _jsonable(evaluation.score),
        "on_time_rate": evaluation.on_time_rate,
        "total_lateness_min": evaluation.total_lateness_min,
        "max_lateness_min": evaluation.max_lateness_min,
        "max_route_distance_km": evaluation.max_route_distance_km,
        "max_route_completion_min": evaluation.max_route_completion_min,
        "relay_count": evaluation.relay_count,
        "direct_task_count": evaluation.direct_task_count,
        "station_storage_time_total": evaluation.station_storage_time_total,
        "receiver_wait_total": evaluation.receiver_wait_total,
        "capacity_release_events": evaluation.capacity_release_events,
        "early_release_gain_total": evaluation.early_release_gain_total,
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
        "relay_model": {
            "enabled": True,
            "relay_count": evaluation.relay_count,
            "stations": stations,
            "relays": relays,
        },
    }


def cooperative_result_payload(
    problem: Problem,
    result: "CooperativeResult",
    *,
    source: str | Path,
) -> dict[str, Any]:
    """Create a self-contained JSON document for a Dynamic-Coop solution."""

    val = result.validation
    sol = result.solution
    evaluator = RouteEvaluator(problem)
    routes_out = []
    for route_index, route in enumerate(sol.routes):
        metrics = evaluator.evaluate(route)
        max_delivery_task = max(
            (abs(v) for v in route if v < 0), default=0
        )
        completion = (
            metrics.delivery_times_min.get(max_delivery_task, 0.0)
            if max_delivery_task else 0.0
        )
        routes_out.append({
            "drone_id": route_index + 1,
            "task_count": sum(1 for v in route if v > 0),
            "distance_km": metrics.score.distance_km,
            "completion_time_min": completion,
            "late_count": metrics.score.late_count,
            "encoded_visits": list(route),
        })
    return {
        "source": str(source),
        "solver": "B1-Dynamic-Coop-HALNS",
        "problem": {
            "task_count": len(problem.tasks),
            "drone_count": problem.drone_count,
            "max_tasks_per_drone": problem.max_tasks_per_drone,
            "capacity": problem.capacity,
            "speed_km_per_min": problem.speed_km_per_min,
            "depot_km": [problem.depot.x, problem.depot.y],
            "objective_order": ["late_count", "distance_km"],
        },
        "valid": val.valid,
        "violations": list(val.violations),
        "score": {
            "late_count": val.late_count,
            "distance_km": val.distance_km,
        },
        "total_lateness_min": val.total_lateness_min,
        "runtime_seconds": result.runtime_seconds,
        "iterations": result.iterations,
        "diagnostics": {
            "stage1_late_count": result.stage1_late_count,
            "stage1_pickup_late_initial": result.stage1_pickup_late_initial,
            "stage1_pickup_late_final": result.stage1_pickup_late_final,
            "ownership_changes": result.ownership_changes,
            "pair_exchanges": result.pair_exchanges,
            "cycle_exchanges": result.cycle_exchanges,
            "relay_successes": result.relay_successes,
            "relay_attempts": result.relay_attempts,
            "relay_accepted": result.relay_accepted,
            "relay_count": result.relay_count,
            "blocker_relay_count": result.blocker_relay_count,
            "downstream_rescued": result.downstream_rescued,
            "active_station_count": result.active_station_count,
            "station_adds": result.station_adds,
            "station_drops": result.station_drops,
            "station_replaces": result.station_replaces,
            "swap_candidates": result.swap_candidates,
            "swap_accepted": result.swap_accepted,
            "final_swap_count": result.final_swap_count,
            "service_distribution": result.service_distribution,
        },
        "delivery_times_min": {
            str(tid): t
            for tid, t in sorted(val.delivery_times_min.items())
        },
        "routes": routes_out,
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
        "swap",
        "relay",
        "dynamic-coop",
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
    solve.add_argument("--swap-time-share", type=float, default=58.0)
    solve.add_argument("--swap-point-candidate-limit", type=int, default=12)
    solve.add_argument("--swap-partner-limit", type=int, default=12)
    solve.add_argument("--max-swaps", type=int, default=20)
    solve.add_argument("--max-swaps-per-drone", type=int, default=5)
    solve.add_argument("--swap-service-time-min", type=float, default=0.0)
    solve.add_argument("--relay-stations", type=int, default=4)
    solve.add_argument("--relay-handling-time", type=float, default=0.0)
    solve.add_argument("--relay-stage1-budget", type=float)
    # dynamic-coop flags
    solve.add_argument("--dynamic-relay", action="store_true", default=True)
    solve.add_argument("--dynamic-ownership", action="store_true", default=True)
    solve.add_argument("--batch-relay", action="store_true", default=True)
    solve.add_argument("--enable-swap", action="store_true", default=True)
    solve.add_argument("--min-active-stations", type=int, default=2)
    solve.add_argument("--max-active-stations", type=int, default=8)
    solve.add_argument("--station-refresh-interval", type=int, default=20)
    return parser


def _solve(args: argparse.Namespace) -> SolverResult:
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
        )
        if args.method in {"alns-core", "basic-alns"}:
            result = solve_alns_core(problem, config=config)
        elif args.method == "swap":
            swap_config = SwapConfig(
                seed=args.seed,
                swap_point_candidate_limit=args.swap_point_candidate_limit,
                swap_partner_limit=args.swap_partner_limit,
                max_swaps=args.max_swaps,
                max_swaps_per_drone=args.max_swaps_per_drone,
                swap_service_time_min=args.swap_service_time_min,
            )
            result = solve_swap(
                problem,
                alns_config=config,
                swap_config=swap_config,
                time_limit_seconds=args.time_limit,
                swap_time_share=args.swap_time_share,
            )
        elif args.method == "relay":
            relay_config = RelaySearchConfig(
                seed=args.seed,
                num_stations=args.relay_stations,
                handling_time_min=args.relay_handling_time,
                time_limit_seconds=args.time_limit,
            )
            if args.relay_stage1_budget is not None:
                relay_config = RelaySearchConfig(
                    seed=args.seed,
                    num_stations=args.relay_stations,
                    handling_time_min=args.relay_handling_time,
                    time_limit_seconds=args.time_limit,
                    module0_share=0.0,
                    stage1_share=min(
                        0.9, args.relay_stage1_budget / max(1.0, args.time_limit or 1.0)
                    ),
                )
            result = solve_relay(problem, config=relay_config)
        elif args.method == "dynamic-coop":
            coop_config = CooperativeConfig(
                seed=args.seed,
                time_limit_seconds=args.time_limit or 240.0,
                candidate_limit=args.candidate_limit,
                enable_swap=args.enable_swap,
                enable_batch_relay=args.batch_relay,
                enable_ownership_exchange=args.dynamic_ownership,
                dynamic_relay=DynamicRelayConfig(
                    min_active_stations=args.min_active_stations,
                    max_active_stations=args.max_active_stations,
                    station_refresh_interval=args.station_refresh_interval,
                ),
            )
            result = solve_dynamic_cooperative(problem, config=coop_config)
        else:
            result = solve_alns(problem, config=config)
    if isinstance(result, SwapSolverResult):
        payload = swap_result_payload(problem, result, source=args.input)
    elif isinstance(result, RelaySolverResult):
        payload = relay_result_payload(problem, result, source=args.input)
    elif isinstance(result, CooperativeResult):
        payload = cooperative_result_payload(problem, result, source=args.input)
    else:
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
