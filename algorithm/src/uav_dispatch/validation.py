"""Independent, full-route constraint and metric validation."""

from __future__ import annotations

from math import isfinite
from types import MappingProxyType
from typing import Iterable, Sequence

from .model import Problem, Score, SolutionEvaluation


def evaluate_solution(
    problem: Problem, routes: Iterable[Sequence[int]]
) -> SolutionEvaluation:
    """Recompute every constraint and metric without incremental search caches."""

    materialized = tuple(tuple(route) for route in routes)
    violations: list[str] = []
    if len(materialized) > problem.drone_count:
        violations.append(
            f"路线数 {len(materialized)} 超过无人机数 {problem.drone_count}"
        )

    all_pickups: dict[int, int] = {}
    all_deliveries: dict[int, int] = {}
    delivery_times: dict[int, float] = {}
    total_distance = 0.0
    total_lateness = 0.0
    max_lateness = 0.0
    late_count = 0
    route_distances: list[float] = []
    route_completion_times: list[float] = []

    for drone_index, route in enumerate(materialized, start=1):
        picked: set[int] = set()
        delivered: set[int] = set()
        route_tasks: set[int] = set()
        load = 0
        elapsed = 0.0
        previous: int | None = None
        route_distance = 0.0

        for position, visit in enumerate(route, start=1):
            task_id = abs(visit)
            if task_id not in problem._task_by_id:
                violations.append(
                    f"无人机 {drone_index} 位置 {position} 包含未知任务 {task_id}"
                )
                continue

            leg = problem.distance(previous, visit)
            if not isfinite(leg):
                violations.append(
                    f"无人机 {drone_index} 位置 {position} 的航段距离不是有限数值"
                )
                continue
            total_distance += leg
            route_distance += leg
            elapsed += leg / problem.speed_km_per_min
            previous = visit
            route_tasks.add(task_id)

            if visit > 0:
                if task_id in picked:
                    violations.append(f"任务 {task_id} 被重复取件")
                picked.add(task_id)
                all_pickups[task_id] = all_pickups.get(task_id, 0) + 1
                load += 1
                if load > problem.capacity:
                    violations.append(
                        f"无人机 {drone_index} 位置 {position} 载荷 {load} 超过上限 {problem.capacity}"
                    )
            else:
                if task_id not in picked:
                    violations.append(f"任务 {task_id} 在取件之前送达")
                if task_id in delivered:
                    violations.append(f"任务 {task_id} 被重复送达")
                delivered.add(task_id)
                all_deliveries[task_id] = all_deliveries.get(task_id, 0) + 1
                load -= 1
                if load < 0:
                    violations.append(
                        f"无人机 {drone_index} 位置 {position} 载荷变为负数"
                    )
                delivery_times[task_id] = elapsed
                lateness = max(0.0, elapsed - problem.task(task_id).deadline_min)
                if lateness > 1e-9:
                    late_count += 1
                    total_lateness += lateness
                    max_lateness = max(max_lateness, lateness)

        if load != 0:
            violations.append(f"无人机 {drone_index} 路线结束时载荷为 {load}")
        if len(route_tasks) > problem.max_tasks_per_drone:
            violations.append(
                f"无人机 {drone_index} 承接 {len(route_tasks)} 个任务，超过上限 {problem.max_tasks_per_drone}"
            )
        route_distances.append(route_distance)
        route_completion_times.append(elapsed)

    expected = set(problem.task_ids)
    missing_pickups = sorted(expected - set(all_pickups))
    missing_deliveries = sorted(expected - set(all_deliveries))
    if missing_pickups or missing_deliveries:
        missing = sorted(set(missing_pickups) | set(missing_deliveries))
        violations.append(f"缺少任务的完整取送记录: {missing}")

    for task_id in sorted(expected):
        pickup_count = all_pickups.get(task_id, 0)
        delivery_count = all_deliveries.get(task_id, 0)
        if pickup_count > 1 or delivery_count > 1:
            violations.append(
                f"任务 {task_id} 出现次数非法: 取件 {pickup_count}，送达 {delivery_count}"
            )

    return SolutionEvaluation(
        valid=not violations,
        score=Score(late_count, total_lateness, total_distance),
        delivery_times_min=MappingProxyType(delivery_times),
        max_lateness_min=max_lateness,
        violations=tuple(violations),
        route_distances_km=tuple(route_distances),
        route_completion_times_min=tuple(route_completion_times),
    )
