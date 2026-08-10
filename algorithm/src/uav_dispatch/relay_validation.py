"""Independent fleet-wide validation for buffered relay plans."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from heapq import heappop, heappush
from math import isfinite
from types import MappingProxyType
from typing import Mapping

from .model import Score
from .relay import Event, EventType, RelayPlan, RelayProblem, TaskCountSemantics


_Node = tuple[int, int]


@dataclass(frozen=True, slots=True)
class RelayEvaluation:
    """Official metrics plus relay-specific feasibility diagnostics."""

    valid: bool
    score: Score
    violations: tuple[str, ...]
    delivery_times_min: Mapping[int, float]
    route_distances_km: tuple[float, ...]
    route_completion_times_min: tuple[float, ...]
    max_lateness_min: float
    relay_count: int
    direct_task_count: int
    package_wait_times_min: Mapping[int, float]
    uav_wait_times_min: Mapping[int, float]
    max_hub_inventory: int
    hub_peak_inventory: Mapping[str, int]
    physical_touch_counts: tuple[int, ...]
    primary_owner_counts: tuple[int, ...]
    task_count_semantics: TaskCountSemantics
    event_times_min: tuple[tuple[float, ...], ...]

    @property
    def package_wait_min(self) -> float:
        return sum(self.package_wait_times_min.values())

    @property
    def uav_wait_min(self) -> float:
        return sum(self.uav_wait_times_min.values())

    @property
    def semantics_extension(self) -> bool:
        return self.task_count_semantics is TaskCountSemantics.PRIMARY_OWNER

    @property
    def on_time_rate(self) -> float:
        delivered = len(self.delivery_times_min)
        if delivered == 0:
            return 0.0
        return (delivered - self.score.late_count) / delivered


def evaluate_relay_plan(
    problem: RelayProblem, plan: RelayPlan
) -> RelayEvaluation:
    """Recompute all relay constraints and metrics from an immutable plan."""

    routes = plan.routes
    base = problem.base_problem
    violations: list[str] = []
    if len(routes) > base.drone_count:
        violations.append(
            f"路线数 {len(routes)} 超过无人机数 {base.drone_count}"
        )

    events_by_task: dict[int, dict[EventType, list[_Node]]] = defaultdict(
        lambda: defaultdict(list)
    )
    physical_touch_counts = tuple(
        len(
            {
                event.task_id
                for event in route
                if event.task_id in base._task_by_id
            }
        )
        for route in routes
    )
    primary_owner_counts = tuple(
        len(
            {
                event.task_id
                for event in route
                if event.event_type is EventType.PICKUP
                and event.task_id in base._task_by_id
            }
        )
        for route in routes
    )

    selected_counts = (
        physical_touch_counts
        if problem.task_count_semantics is TaskCountSemantics.STRICT_TOUCH
        else primary_owner_counts
    )
    for route_index, count in enumerate(selected_counts, start=1):
        if count > base.max_tasks_per_drone:
            label = (
                "触达"
                if problem.task_count_semantics is TaskCountSemantics.STRICT_TOUCH
                else "主承接"
            )
            violations.append(
                f"无人机 {route_index} {label} {count} 个任务，超过上限 "
                f"{base.max_tasks_per_drone}"
            )

    for route_index, route in enumerate(routes):
        load = 0
        for event_index, event in enumerate(route):
            node = (route_index, event_index)
            if event.task_id not in base._task_by_id:
                violations.append(
                    f"无人机 {route_index + 1} 位置 {event_index + 1} "
                    "包含未知任务 "
                    f"{event.task_id}"
                )
            else:
                events_by_task[event.task_id][event.event_type].append(node)
            if event.event_type in {
                EventType.HANDOFF_DROP,
                EventType.HANDOFF_PICK,
            } and event.hub_id not in problem._hub_by_id:
                violations.append(
                    f"任务 {event.task_id} 引用了未知交接点 {event.hub_id!r}"
                )

            if event.event_type in {
                EventType.PICKUP,
                EventType.HANDOFF_PICK,
            }:
                load += 1
                if load > base.capacity:
                    violations.append(
                        f"无人机 {route_index + 1} 位置 {event_index + 1} 载荷 "
                        f"{load} 超过上限 {base.capacity}"
                    )
            else:
                load -= 1
                if load < 0:
                    violations.append(
                        f"无人机 {route_index + 1} 位置 {event_index + 1} "
                        "在取得包裹之前释放载荷"
                    )
        if load != 0:
            violations.append(
                f"无人机 {route_index + 1} 路线结束时载荷为 {load}"
            )

    expected = set(problem.task_ids)
    missing = sorted(expected - set(events_by_task))
    if missing:
        violations.append(f"缺少任务的完整取送记录: {missing}")

    relay_tasks: set[int] = set()
    direct_tasks: set[int] = set()
    relay_nodes: dict[int, tuple[_Node, _Node]] = {}
    for task_id in sorted(expected):
        grouped = events_by_task.get(task_id, {})
        counts = {
            event_type: len(grouped.get(event_type, ()))
            for event_type in EventType
        }
        direct = (
            counts[EventType.PICKUP] == 1
            and counts[EventType.DELIVERY] == 1
            and counts[EventType.HANDOFF_DROP] == 0
            and counts[EventType.HANDOFF_PICK] == 0
        )
        relay = all(counts[event_type] == 1 for event_type in EventType)
        if direct:
            direct_tasks.add(task_id)
            pickup_node = grouped[EventType.PICKUP][0]
            delivery_node = grouped[EventType.DELIVERY][0]
            if pickup_node[0] != delivery_node[0]:
                violations.append(
                    f"任务 {task_id} 的直接取送不在同一架无人机"
                )
            elif pickup_node[1] >= delivery_node[1]:
                violations.append(f"任务 {task_id} 在取件之前送达")
            continue
        if not relay:
            rendered = ", ".join(
                f"{event_type.value}={counts[event_type]}"
                for event_type in EventType
            )
            violations.append(f"任务 {task_id} 的事件组成非法: {rendered}")
            continue

        relay_tasks.add(task_id)
        if problem.max_handoffs_per_task == 0:
            violations.append(f"任务 {task_id} 不允许交接")
        pickup_node = grouped[EventType.PICKUP][0]
        drop_node = grouped[EventType.HANDOFF_DROP][0]
        pick_node = grouped[EventType.HANDOFF_PICK][0]
        delivery_node = grouped[EventType.DELIVERY][0]
        drop_event = routes[drop_node[0]][drop_node[1]]
        pick_event = routes[pick_node[0]][pick_node[1]]
        if drop_event.hub_id != pick_event.hub_id:
            violations.append(f"任务 {task_id} 的放件与取件交接点不一致")
        if pickup_node[0] != drop_node[0] or pickup_node[1] >= drop_node[1]:
            violations.append(f"任务 {task_id} 的取件与交接放件顺序非法")
        if pick_node[0] != delivery_node[0] or pick_node[1] >= delivery_node[1]:
            violations.append(f"任务 {task_id} 的交接取件与送达顺序非法")
        if drop_node[0] == pick_node[0]:
            violations.append(
                f"任务 {task_id} 的接力必须由两架不同无人机完成"
            )
        relay_nodes[task_id] = (drop_node, pick_node)

    all_nodes = [
        (route_index, event_index)
        for route_index, route in enumerate(routes)
        for event_index in range(len(route))
    ]
    adjacency: dict[_Node, list[tuple[_Node, float]]] = {
        node: [] for node in all_nodes
    }
    indegree: dict[_Node, int] = {node: 0 for node in all_nodes}
    earliest: dict[_Node, float] = {node: 0.0 for node in all_nodes}
    route_distances: list[float] = []

    def service_time(event: Event) -> float:
        return (
            problem.handoff_service_min
            if event.event_type is EventType.HANDOFF_DROP
            else 0.0
        )

    def safe_distance(
        from_event: Event | None,
        to_event: Event,
        route_index: int,
        event_index: int,
    ) -> float:
        try:
            distance = problem.distance(from_event, to_event)
        except (KeyError, ValueError):
            return 0.0
        if not isfinite(distance) or distance < 0:
            violations.append(
                f"无人机 {route_index + 1} 位置 {event_index + 1} "
                "的航段距离非法"
            )
            return 0.0
        return distance

    for route_index, route in enumerate(routes):
        route_distance = 0.0
        previous_event: Event | None = None
        previous_node: _Node | None = None
        for event_index, event in enumerate(route):
            node = (route_index, event_index)
            leg = safe_distance(previous_event, event, route_index, event_index)
            route_distance += leg
            travel_time = leg / base.speed_km_per_min
            if previous_node is None:
                earliest[node] = max(earliest[node], travel_time)
            else:
                weight = service_time(previous_event) + travel_time
                adjacency[previous_node].append((node, weight))
                indegree[node] += 1
            previous_event = event
            previous_node = node
        route_distances.append(route_distance)

    for task_id, (drop_node, pick_node) in relay_nodes.items():
        drop_event = routes[drop_node[0]][drop_node[1]]
        pick_event = routes[pick_node[0]][pick_node[1]]
        if (
            drop_event.hub_id in problem._hub_by_id
            and pick_event.hub_id in problem._hub_by_id
        ):
            adjacency[drop_node].append(
                (pick_node, problem.handoff_service_min)
            )
            indegree[pick_node] += 1

    queue: list[_Node] = []
    for node in all_nodes:
        if indegree[node] == 0:
            heappush(queue, node)
    visited: list[_Node] = []
    while queue:
        node = heappop(queue)
        visited.append(node)
        for successor, weight in adjacency[node]:
            earliest[successor] = max(
                earliest[successor], earliest[node] + weight
            )
            indegree[successor] -= 1
            if indegree[successor] == 0:
                heappush(queue, successor)
    acyclic = len(visited) == len(all_nodes)
    if not acyclic:
        violations.append("事件优先图存在跨无人机交接环")

    event_times = tuple(
        tuple(earliest[(route_index, event_index)] for event_index in range(len(route)))
        for route_index, route in enumerate(routes)
    )
    route_completion_times = tuple(
        (
            0.0
            if not route
            else earliest[(route_index, len(route) - 1)] + service_time(route[-1])
        )
        for route_index, route in enumerate(routes)
    )

    delivery_times: dict[int, float] = {}
    total_lateness = 0.0
    max_lateness = 0.0
    late_count = 0
    for task_id in sorted(expected):
        deliveries = events_by_task.get(task_id, {}).get(EventType.DELIVERY, ())
        if len(deliveries) != 1:
            continue
        delivered_at = earliest[deliveries[0]]
        delivery_times[task_id] = delivered_at
        lateness = max(0.0, delivered_at - problem.task(task_id).deadline_min)
        if lateness > 1e-9:
            late_count += 1
            total_lateness += lateness
            max_lateness = max(max_lateness, lateness)

    package_waits: dict[int, float] = {}
    uav_waits: dict[int, float] = {}
    inventory_events: dict[str, list[tuple[float, int, int]]] = defaultdict(list)
    if acyclic:
        for task_id, (drop_node, pick_node) in relay_nodes.items():
            drop_event = routes[drop_node[0]][drop_node[1]]
            pick_event = routes[pick_node[0]][pick_node[1]]
            if (
                drop_event.hub_id is None
                or drop_event.hub_id != pick_event.hub_id
                or drop_event.hub_id not in problem._hub_by_id
            ):
                continue
            package_ready = earliest[drop_node] + problem.handoff_service_min
            if pick_node[1] == 0:
                route_ready = problem.distance(None, pick_event) / base.speed_km_per_min
            else:
                predecessor_node = (pick_node[0], pick_node[1] - 1)
                predecessor = routes[predecessor_node[0]][predecessor_node[1]]
                route_ready = (
                    earliest[predecessor_node]
                    + service_time(predecessor)
                    + problem.distance(predecessor, pick_event)
                    / base.speed_km_per_min
                )
            picked_at = earliest[pick_node]
            package_waits[task_id] = max(0.0, picked_at - package_ready)
            uav_waits[task_id] = max(0.0, package_ready - route_ready)
            inventory_events[drop_event.hub_id].append((package_ready, 0, 1))
            inventory_events[drop_event.hub_id].append((picked_at, 1, -1))

    hub_peaks: dict[str, int] = {}
    for hub in problem.hubs:
        inventory = 0
        peak = 0
        for _, _, delta in sorted(inventory_events.get(hub.id, ())):
            inventory += delta
            peak = max(peak, inventory)
        hub_peaks[hub.id] = peak

    return RelayEvaluation(
        valid=not violations,
        score=Score(late_count, total_lateness, sum(route_distances)),
        violations=tuple(violations),
        delivery_times_min=MappingProxyType(delivery_times),
        route_distances_km=tuple(route_distances),
        route_completion_times_min=route_completion_times,
        max_lateness_min=max_lateness,
        relay_count=len(relay_tasks),
        direct_task_count=len(direct_tasks),
        package_wait_times_min=MappingProxyType(package_waits),
        uav_wait_times_min=MappingProxyType(uav_waits),
        max_hub_inventory=max(hub_peaks.values(), default=0),
        hub_peak_inventory=MappingProxyType(hub_peaks),
        physical_touch_counts=physical_touch_counts,
        primary_owner_counts=primary_owner_counts,
        task_count_semantics=problem.task_count_semantics,
        event_times_min=event_times,
    )
