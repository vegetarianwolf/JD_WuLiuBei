"""Command-line interface for solving and serialising dispatch instances."""

from __future__ import annotations

import argparse
import json
import sys
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
    solve_relay_staged,
)
from .deployment import (
    DemandConfig,
    DeploymentPlan,
    compute_dynamic_uav_homes,
    deployment_distribution,
)
from .exact import solve_exact
from .io import load_tasks_csv
from .model import Point, Problem, Score, Task
from .relay import (
    GlobalRelayEvaluator,
    RelayNetwork,
    build_plan_index,
    build_relay_network,
    compute_dynamic_station_count,
    relay_candidate_coverage,
    relay_statistics,
)
from .search import SolverResult


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


def _relay_count_arg(value: str) -> int | str:
    """``--relay-count`` accepts an integer or ``auto``."""

    if value.strip().lower() == "auto":
        return "auto"
    try:
        return int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "--relay-count 必须是整数或 auto"
        ) from None


def resolve_relay_count(
    tasks: Sequence[Task],
    drone_count: int,
    relay_count: int | str,
    *,
    location_seed: int = 42,
    location_method: str = "weighted_kmedoids",
) -> int:
    """Resolve ``relay_count`` (int) or ``"auto"`` to a concrete station
    count, deriving the count from the task distribution when requested."""

    if relay_count != "auto":
        return int(relay_count)
    return compute_dynamic_station_count(
        tasks,
        drone_count,
        location_seed=location_seed,
        location_method=location_method,
    )


def build_drone_homes(
    drone_count: int,
    network: RelayNetwork,
    mode: str,
    *,
    tasks: Sequence[Task] = (),
    depot: Point | None = None,
    speed_km_per_min: float = 0.9,
    config: DemandConfig | None = None,
) -> tuple[int | None, ...] | None:
    """Per-drone initial homes: ``None``=origin, ``int``=relay station id.

    ``mode == "origin"`` keeps every drone at the origin (returns ``None``).
    ``mode == "stations"`` computes a task-demand-driven dynamic deployment
    across the origin plus every relay station: each candidate may receive
    0, 1, 2, ... UAVs (never a fixed one-per-station rule), and the returned
    tuple always has length ``drone_count``.  Deployment points are the
    relay sites, but deployment is fully decoupled from relay semantics: a
    station with 0 UAVs is still a perfectly usable relay handoff point.
    """

    if mode == "origin":
        return None
    plan = compute_dynamic_uav_homes(
        tuple(tasks),
        tuple(network.stations),
        drone_count,
        depot if depot is not None else Point(0.0, 0.0),
        speed_km_per_min=speed_km_per_min,
        config=config,
    )
    return plan.homes


def _log_deployment(plan: DeploymentPlan) -> None:
    """Print the demand-driven deployment table to stderr."""

    print("Dynamic UAV deployment", file=sys.stderr)
    print("----------------------", file=sys.stderr)
    for node in plan.nodes:
        name = "Depot" if node.home is None else f"Relay_{node.home}"
        print(
            f"{name:<12s} score={node.demand_score:8.2f}   "
            f"uavs={node.allocated_uavs}",
            file=sys.stderr,
        )
    total = sum(node.allocated_uavs for node in plan.nodes)
    print(f"total={total} UAVs", file=sys.stderr)


def _deployment_summary(problem: Problem) -> dict[str, Any]:
    """Deployment distribution for the result document (audit only)."""

    homes = (
        problem.drone_homes
        if problem.drone_homes is not None
        else (None,) * problem.drone_count
    )
    return {
        "distribution": deployment_distribution(homes),
        "homes": [
            (
                "origin"
                if home is None
                else (
                    f"point_{index}({home.x:.3f},{home.y:.3f})"
                    if isinstance(home, Point)
                    else f"station_{home}"
                )
            )
            for index, home in enumerate(homes)
        ],
    }


def result_payload(
    problem: Problem,
    result: SolverResult,
    *,
    source: str | Path,
) -> dict[str, Any]:
    """Create a self-contained, JSON-serialisable result document."""

    evaluation = result.evaluation
    plan_index = build_plan_index(problem, result.routes)
    schedule = GlobalRelayEvaluator(problem).evaluate(result.routes)
    routes = []
    for route_index, route in enumerate(result.routes):
        delivered_task_ids: set[int] = set()
        for visit in route:
            if visit >= 0:
                continue
            leg = (
                problem.leg_registry.get(abs(visit))
                if problem.has_relays
                else None
            )
            if leg is None:
                delivered_task_ids.add(abs(visit))
            elif leg.kind == "RELAY_OUT":
                delivered_task_ids.add(leg.task_id)
        late_task_ids = {
            task_id
            for task_id in delivered_task_ids
            if evaluation.delivery_times_min[task_id]
            > problem.task(task_id).deadline_min + 1e-9
        }
        route_lateness = sum(
            max(
                0.0,
                evaluation.delivery_times_min[task_id]
                - problem.task(task_id).deadline_min,
            )
            for task_id in sorted(delivered_task_ids)
        )
        delivered_tasks = len(delivered_task_ids)
        relay_drops = 0
        relay_pickups = 0
        visits = []
        for position, visit in enumerate(route):
            point = problem.point_for_visit(visit)
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
                        "location_km": [point.x, point.y],
                        "arrival_time_min": schedule.event_times_min[
                            (route_index, position)
                        ],
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
                        "location_km": [point.x, point.y],
                        "arrival_time_min": schedule.event_times_min[
                            (route_index, position)
                        ],
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
                # Per-route timeliness must use the global relay schedule.
                # A local route walk ignores cross-UAV handoff waiting and
                # can under-report both fields for relay-aware solutions.
                "late_count": len(late_task_ids),
                "total_lateness_min": route_lateness,
                "on_time_task_count": delivered_tasks - len(late_task_ids),
                "on_time_rate": (
                    (delivered_tasks - len(late_task_ids)) / delivered_tasks
                    if delivered_tasks
                    else 1.0
                ),
                "encoded_visits": list(route),
                "visits": visits,
            }
        )
    relay_block: dict[str, Any]
    task_plans: dict[str, Any]
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
            "drone_homes": [
                (
                    "origin"
                    if home is None
                    else (
                        f"point_{index}({home.x:.3f},{home.y:.3f})"
                        if isinstance(home, Point)
                        else f"station_{home}"
                    )
                )
                for index, home in enumerate(
                    problem.drone_homes
                    if problem.drone_homes is not None
                    else (None,) * problem.drone_count
                )
            ],
            "deployment": _deployment_summary(problem),
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
    solve.add_argument(
        "--relay-count",
        type=_relay_count_arg,
        default="auto",
        help="中继站数量；auto=按任务分布动态确定（默认），或传入具体整数"
        "（0 表示无中继）",
    )
    solve.add_argument("--relay-candidates-per-task", type=int, default=2)
    solve.add_argument(
        "--drone-homes",
        choices=("origin", "stations"),
        default="stations",
        help="无人机初始部署：origin=全部在原点；stations=每中继站 1 架 + 原点 1 架"
        "（部署点与中继站共用同一批点）",
    )
    solve.add_argument(
        "--relay-location-method",
        choices=("weighted_kmedoids", "kmeans_midpoint", "kmeans_pickup"),
        default="weighted_kmedoids",
        help="中继站选址方法（站点同时是无人机预部署点）",
    )
    solve.add_argument(
        "--enable-home-seed",
        action="store_true",
        help="构造初始解时先给每架无人机插入离其起点最近的任务（起点感知播种）",
    )
    solve.add_argument(
        "--enable-home-bias",
        action="store_true",
        help="构造初始解时在词典序插入评分中加入 home→pickup 距离作为平局偏向"
        "（仅影响初始构造，不进最终目标）",
    )
    solve.add_argument(
        "--disable-home-aware-init",
        action="store_true",
        help="stations 场景关闭 home 感知初始构造（就近播种 + home 偏向）",
    )
    solve.add_argument(
        "--enable-home-displaced",
        action="store_true",
        help="启用 home_displaced 破坏算子：优先移除被分配到离自己起点较远的机的任务",
    )
    solve.add_argument(
        "--relay-direct-warmup",
        type=float,
        default=0.80,
        help="中继搜索前用于 DIRECT 预热的总预算比例；0 表示纯中继搜索",
    )
    solve.add_argument("--relay-detour-ratio", type=float, default=2.0)
    solve.add_argument("--relay-plan-beam", type=int, default=4)
    solve.add_argument("--relay-leg-beam", type=int, default=5)
    solve.add_argument("--relay-event-cap", type=int, default=60)
    solve.add_argument("--relay-sample-every", type=int, default=3)
    solve.add_argument("--relay-global-limit", type=int, default=5)
    solve.add_argument("--relay-location-seed", type=int, default=42)
    solve.add_argument(
        "--relay-probe-fraction",
        type=float,
        default=0.25,
        help="每轮生成中继 beam 的任务比例",
    )
    solve.add_argument("--relay-probe-min", type=int, default=1)
    solve.add_argument("--relay-probe-max-tasks", type=int, default=2)
    solve.add_argument("--relay-seed-task-limit", type=int, default=200)
    solve.add_argument(
        "--disable-relay-seed",
        action="store_true",
        help="关闭构造后的中继播种 pass",
    )
    solve.add_argument(
        "--disable-relay-refine",
        action="store_true",
        help="关闭中继跨机精化 pass",
    )
    solve.add_argument("--relay-refine-interval", type=int, default=5)
    solve.add_argument("--relay-refine-task-limit", type=int, default=16)
    solve.add_argument("--relay-auto-widen", action="store_true")
    solve.add_argument("--disable-relay", action="store_true")
    solve.add_argument("--relay-debug", action="store_true")
    return parser


def _solve(args: argparse.Namespace) -> SolverResult:
    tasks = load_tasks_csv(args.input)
    if args.tasks is not None:
        if args.tasks <= 0:
            raise ValueError("--tasks 必须为正整数")
        tasks = tasks[: args.tasks]
    relay_count = (
        0
        if args.disable_relay
        else resolve_relay_count(
            tasks,
            args.drones,
            args.relay_count,
            location_seed=args.relay_location_seed,
            location_method=args.relay_location_method,
        )
    )
    if args.relay_count == "auto" and not args.disable_relay:
        print(
            f"[relay] 动态建站数（需求驱动）: {relay_count}",
            file=sys.stderr,
        )
    network = build_relay_network(
        tasks,
        relay_count=relay_count,
        candidates_per_task=args.relay_candidates_per_task,
        detour_ratio=args.relay_detour_ratio,
        location_seed=args.relay_location_seed,
        location_method=args.relay_location_method,
    )
    if (
        relay_count > 0
        and args.relay_auto_widen
        and relay_candidate_coverage(network.task_relay_candidates) < 0.5
    ):
        # Auto-widen: thin coverage means most tasks are direct-only under
        # the current geometry, so retry with more stations and a looser
        # detour ratio.
        network = build_relay_network(
            tasks,
            relay_count=max(relay_count, 4),
            candidates_per_task=max(args.relay_candidates_per_task, 2),
            detour_ratio=max(args.relay_detour_ratio, 2.0),
            location_seed=args.relay_location_seed,
        )
        print(
            "[relay] 中继候选覆盖率过低，已自动放宽网络"
            f"（relay_count={relay_count}→{max(relay_count, 4)}，"
            f"detour_ratio={args.relay_detour_ratio}→"
            f"{max(args.relay_detour_ratio, 2.0)}）",
            file=sys.stderr,
        )
    depot = Point(args.depot_x, args.depot_y)
    deployment_plan: DeploymentPlan | None = None
    if args.drone_homes == "stations":
        deployment_plan = compute_dynamic_uav_homes(
            tasks,
            network.stations,
            args.drones,
            depot,
            speed_km_per_min=args.speed_km_per_min,
        )
        _log_deployment(deployment_plan)
    problem = Problem(
        tasks,
        drone_count=args.drones,
        max_tasks_per_drone=args.max_tasks,
        capacity=args.capacity,
        speed_km_per_min=args.speed_km_per_min,
        depot=depot,
        relay_stations=network.stations,
        leg_registry=network.leg_registry,
        task_relay_candidates=network.task_relay_candidates,
        drone_homes=(
            None if deployment_plan is None else deployment_plan.homes
        ),
    )
    if relay_count > 0:
        print(
            "[relay] 中继候选覆盖率: "
            f"{relay_candidate_coverage(problem.task_relay_candidates):.2f}",
            file=sys.stderr,
        )
    home_aware = (
        args.drone_homes == "stations" and not args.disable_home_aware_init
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
            relay_direct_warmup_fraction=args.relay_direct_warmup,
            relay_candidates_per_task=args.relay_candidates_per_task,
            relay_plan_beam=args.relay_plan_beam,
            relay_leg_beam=args.relay_leg_beam,
            relay_event_cap=args.relay_event_cap,
            relay_sample_every=args.relay_sample_every,
            relay_global_limit=args.relay_global_limit,
            relay_probe_fraction=args.relay_probe_fraction,
            relay_probe_min=args.relay_probe_min,
            relay_probe_max_tasks=args.relay_probe_max_tasks,
            enable_relay_seed=not args.disable_relay_seed,
            relay_seed_task_limit=args.relay_seed_task_limit,
            enable_relay_refine=not args.disable_relay_refine,
            relay_refine_interval=args.relay_refine_interval,
            relay_refine_task_limit=args.relay_refine_task_limit,
            relay_debug=args.relay_debug,
            enable_home_seed=args.enable_home_seed or home_aware,
            enable_home_displaced=args.enable_home_displaced,
            enable_home_bias=args.enable_home_bias or home_aware,
        )
        if args.method in {"alns-core", "basic-alns"}:
            result = (
                solve_relay_staged(problem, config=config, core=True)
                if problem.has_relays
                else solve_alns_core(problem, config=config)
            )
        else:
            result = (
                solve_relay_staged(problem, config=config)
                if problem.has_relays
                else solve_alns(problem, config=config)
            )
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
