"""Independent, full-route constraint and metric validation."""

from __future__ import annotations

from math import isfinite
from types import MappingProxyType
from typing import Iterable, Sequence

from .model import Problem, Score, SolutionEvaluation
from .relay import GlobalRelayEvaluator, build_plan_index


def evaluate_solution(
    problem: Problem,
    routes: Iterable[Sequence[int]],
    _force_full: bool = False,
) -> SolutionEvaluation:
    """Recompute every constraint and metric without incremental search caches.

    Direct tasks keep the legacy same-drone pickup/delivery semantics.  Relay
    tasks are validated with the immutable plan layer and the global handoff
    schedule (drop before pickup, no deadlock cycle, final delivery deadline).
    """

    materialized = tuple(tuple(route) for route in routes)
    violations: list[str] = []
    if len(materialized) > problem.drone_count:
        violations.append(
            f"路线数 {len(materialized)} 超过无人机数 {problem.drone_count}"
        )

    registry = problem.leg_registry
    all_pickups: dict[int, int] = {}
    all_deliveries: dict[int, int] = {}
    total_distance = 0.0
    route_distances: list[float] = []
    route_completion_times: list[float] = []
    unknown_visits = False

    for drone_index, route in enumerate(materialized, start=1):
        picked: set[int] = set()
        delivered: set[int] = set()
        leg_starts: set[int] = set()
        leg_ends: set[int] = set()
        route_tasks: set[int] = set()
        load = 0
        elapsed = 0.0
        previous: int | None = None
        route_distance = 0.0

        for position, visit in enumerate(route, start=1):
            visit_id = abs(visit)
            leg = registry.get(visit_id) if registry else None
            if leg is None and visit_id not in problem._task_by_id:
                violations.append(
                    f"无人机 {drone_index} 位置 {position} 包含未知任务 {visit_id}"
                )
                unknown_visits = True
                continue

            leg_km = problem.distance(
                previous,
                visit,
                from_node=(
                    problem.drone_home_nodes[drone_index - 1]
                    if previous is None
                    else None
                ),
            )
            if not isfinite(leg_km):
                violations.append(
                    f"无人机 {drone_index} 位置 {position} 的航段距离不是有限数值"
                )
                continue
            total_distance += leg_km
            route_distance += leg_km
            elapsed += leg_km / problem.speed_km_per_min
            previous = visit

            if leg is not None:
                if visit > 0:
                    if visit_id in leg_starts:
                        violations.append(f"中转腿 {visit_id} 被重复起点")
                    leg_starts.add(visit_id)
                    load += 1
                    if load > problem.capacity:
                        violations.append(
                            f"无人机 {drone_index} 位置 {position} 载荷 {load} "
                            f"超过上限 {problem.capacity}"
                        )
                    if leg.kind == "RELAY_IN":
                        task_id = leg.task_id
                        all_pickups[task_id] = (
                            all_pickups.get(task_id, 0) + 1
                        )
                else:
                    if visit_id not in leg_starts:
                        violations.append(f"中转腿 {visit_id} 在起点之前结束")
                    if visit_id in leg_ends:
                        violations.append(f"中转腿 {visit_id} 被重复结束")
                    leg_ends.add(visit_id)
                    load -= 1
                    if load < 0:
                        violations.append(
                            f"无人机 {drone_index} 位置 {position} 载荷变为负数"
                        )
                    if leg.kind == "RELAY_OUT":
                        task_id = leg.task_id
                        route_tasks.add(task_id)
                        if task_id in delivered:
                            violations.append(f"任务 {task_id} 被重复送达")
                        delivered.add(task_id)
                        all_deliveries[task_id] = (
                            all_deliveries.get(task_id, 0) + 1
                        )
                continue

            task_id = visit_id
            if visit > 0:
                if task_id in picked:
                    violations.append(f"任务 {task_id} 被重复取件")
                picked.add(task_id)
                all_pickups[task_id] = all_pickups.get(task_id, 0) + 1
                load += 1
                if load > problem.capacity:
                    violations.append(
                        f"无人机 {drone_index} 位置 {position} 载荷 {load} "
                        f"超过上限 {problem.capacity}"
                    )
            else:
                if task_id not in picked:
                    violations.append(f"任务 {task_id} 在取件之前送达")
                if task_id in delivered:
                    violations.append(f"任务 {task_id} 被重复送达")
                delivered.add(task_id)
                route_tasks.add(task_id)
                all_deliveries[task_id] = all_deliveries.get(task_id, 0) + 1
                load -= 1
                if load < 0:
                    violations.append(
                        f"无人机 {drone_index} 位置 {position} 载荷变为负数"
                    )

        if load != 0:
            violations.append(f"无人机 {drone_index} 路线结束时载荷为 {load}")
        if leg_starts != leg_ends:
            violations.append(
                f"无人机 {drone_index} 路线包含不完整的中转腿"
            )
        if len(route_tasks) > problem.max_tasks_per_drone:
            violations.append(
                f"无人机 {drone_index} 承接 {len(route_tasks)} 个任务，"
                f"超过上限 {problem.max_tasks_per_drone}"
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
                f"任务 {task_id} 出现次数非法: 取件 {pickup_count}，"
                f"送达 {delivery_count}"
            )

    delivery_times: dict[int, float] = {}
    if (
        not _force_full
        and not problem.has_relays
        and not unknown_visits
        and not violations
    ):
        # Direct fast path: with no relay legs the plan index and the
        # global handoff schedule add nothing; delivery times from the
        # route scan are already exact (same arithmetic as the walk
        # above).  route_completion_times were appended during the walk.
        # Invalid solutions fall through to the full path, which keeps
        # the relay-schedule violation wording bit-identical.
        for drone_index, route in enumerate(materialized):
            elapsed = 0.0
            previous: int | None = None
            for visit in route:
                elapsed += (
                    problem.distance(
                        previous,
                        visit,
                        from_node=(
                            problem.drone_home_nodes[drone_index]
                            if previous is None
                            else None
                        ),
                    )
                    / problem.speed_km_per_min
                )
                previous = visit
                if visit < 0:
                    delivery_times[abs(visit)] = elapsed
    else:
        plan_index = None
        try:
            plan_index = build_plan_index(problem, materialized)
        except ValueError as exc:
            violations.append(str(exc))

        schedule = None
        if not unknown_visits:
            try:
                schedule = GlobalRelayEvaluator(problem).evaluate(
                    materialized
                )
            except (KeyError, ValueError) as exc:
                violations.append(f"全局调度失败: {exc}")

        if schedule is not None:
            violations.extend(schedule.violations)
            delivery_times.update(schedule.delivery_times_min)
            for route_index, route in enumerate(materialized):
                last_time = 0.0
                for position in range(len(route)):
                    last_time = schedule.event_times_min.get(
                        (route_index, position), last_time
                    )
                route_completion_times[route_index] = last_time
            if plan_index is not None:
                for task_id in plan_index.relay_task_ids:
                    drop_key = (
                        plan_index.inbound_route_by_task[task_id],
                        plan_index.inbound_position_by_task[task_id],
                    )
                    pickup_key = (
                        plan_index.outbound_route_by_task[task_id],
                        plan_index.outbound_position_by_task[task_id],
                    )
                    drop_time = schedule.event_times_min.get(drop_key)
                    pickup_time = schedule.event_times_min.get(pickup_key)
                    if (
                        drop_time is not None
                        and pickup_time is not None
                        and drop_time > pickup_time + 1e-9
                    ):
                        violations.append(
                            f"任务 {task_id} 中转取件时间早于卸货时间"
                        )
        elif plan_index is not None and not plan_index.relay_task_ids:
            # Direct-only fallback: times from the route scan are exact.
            # Unknown visits are skipped exactly like the walk above.
            for drone_index, route in enumerate(materialized):
                elapsed = 0.0
                previous: int | None = None
                for visit in route:
                    visit_id = abs(visit)
                    leg = registry.get(visit_id) if registry else None
                    if (
                        leg is None
                        and visit_id not in problem._task_by_id
                    ):
                        continue
                    elapsed += (
                        problem.distance(
                            previous,
                            visit,
                            from_node=(
                                problem.drone_home_nodes[drone_index]
                                if previous is None
                                else None
                            ),
                        )
                        / problem.speed_km_per_min
                    )
                    previous = visit
                    if visit < 0:
                        delivery_times[abs(visit)] = elapsed

    late_count = 0
    total_lateness = 0.0
    max_lateness = 0.0
    for task_id, delivered_at in delivery_times.items():
        lateness = max(0.0, delivered_at - problem.task(task_id).deadline_min)
        if lateness > 1e-9:
            late_count += 1
            total_lateness += lateness
            max_lateness = max(max_lateness, lateness)

    return SolutionEvaluation(
        valid=not violations,
        score=Score(late_count, total_lateness, total_distance),
        delivery_times_min=MappingProxyType(delivery_times),
        max_lateness_min=max_lateness,
        violations=tuple(violations),
        route_distances_km=tuple(route_distances),
        route_completion_times_min=tuple(route_completion_times),
    )

