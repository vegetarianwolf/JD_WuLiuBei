"""Global, synchronised evaluation of Swap-only solutions.

The Swap-only model couples drone timelines: two drones meet and exchange one
parcel each, so one drone may wait for the other.  Per-route scoring summed
over independent routes is therefore invalid here (the parcel custody crosses
drone boundaries and the timelines synchronise at meeting points).

This module builds the complete event-dependency DAG, propagates synchronised
arrival times in topological order, and rejects any solution whose dependency
graph contains a cycle (a waiting deadlock such as "A waits for B while B
indirectly waits for A").
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Mapping

from .model import Problem, Score
from .swap_model import SwapEvent, SwapSolution


@dataclass(frozen=True, slots=True)
class SwapEventTiming:
    """Synchronised timing of one executed swap."""

    swap_id: int
    drone_a: int
    drone_b: int
    task_a_to_b: int
    task_b_to_a: int
    meeting_node: int
    position_a: int
    position_b: int
    arrival_a_min: float
    arrival_b_min: float
    swap_time_min: float
    swap_end_min: float
    waiting_a_min: float
    waiting_b_min: float


@dataclass(frozen=True, slots=True)
class SwapSolutionEvaluation:
    """Metrics recomputed from a complete Swap-only solution.

    ``score`` is strictly ``(late_count, distance_km)``.  Waiting times and
    lateness metrics are descriptive diagnostics only.
    """

    valid: bool
    score: Score
    delivery_times_min: Mapping[int, float]
    total_lateness_min: float
    max_lateness_min: float
    violations: tuple[str, ...]
    route_distances_km: tuple[float, ...]
    route_completion_times_min: tuple[float, ...]
    waiting_time_min: float
    swap_timings: tuple[SwapEventTiming, ...]

    @property
    def on_time_rate(self) -> float:
        delivered = len(self.delivery_times_min)
        if delivered == 0:
            return 0.0
        return (delivered - self.score.late_count) / delivered

    @property
    def swap_count(self) -> int:
        return len(self.swap_timings)


# ---------------------------------------------------------------------------
# DAG construction and propagation (shared by evaluation and search helpers).
# ---------------------------------------------------------------------------

def _structural_checks(
    problem: Problem, solution: SwapSolution
) -> list[str]:
    """Task uniqueness, pickup ownership, swap structure and precedence."""

    violations: list[str] = []
    routes = solution.routes
    expected = set(problem.task_ids)

    pickup_drone: dict[int, int] = {}
    delivery_drone: dict[int, int] = {}
    pickup_positions: dict[int, tuple[int, int]] = {}
    delivery_positions: dict[int, tuple[int, int]] = {}
    for drone, route in enumerate(routes):
        for index, visit in enumerate(route):
            task_id = abs(visit)
            if task_id not in expected:
                violations.append(f"无人机 {drone + 1} 包含未知任务 {task_id}")
                continue
            if visit > 0:
                if task_id in pickup_drone:
                    violations.append(f"任务 {task_id} 被重复取件")
                pickup_drone[task_id] = drone
                pickup_positions[task_id] = (drone, index)
            else:
                if task_id in delivery_drone:
                    violations.append(f"任务 {task_id} 被重复送达")
                delivery_drone[task_id] = drone
                delivery_positions[task_id] = (drone, index)

    missing = sorted(expected - (set(pickup_drone) & set(delivery_drone)))
    if missing:
        violations.append(f"缺少任务的完整取送记录: {missing}")

    owned_counts: dict[int, int] = defaultdict(int)
    for task_id, drone in pickup_drone.items():
        owned_counts[drone] += 1
    for drone, count in sorted(owned_counts.items()):
        if count > problem.max_tasks_per_drone:
            violations.append(
                f"无人机 {drone + 1} 承接 {count} 个任务，超过上限 {problem.max_tasks_per_drone}"
            )

    swap_by_task: dict[int, SwapEvent] = {}
    for event in solution.swaps:
        if event.drone_a >= len(routes) or event.drone_b >= len(routes):
            violations.append(f"交换 {event.id} 引用了不存在的无人机")
        for task_id in (event.task_a_to_b, event.task_b_to_a):
            if task_id in swap_by_task:
                violations.append(f"任务 {task_id} 参与多次交换")
            else:
                swap_by_task[task_id] = event

    for event in solution.swaps:
        for task_id, giver, receiver, gap_giver, gap_receiver in (
            (
                event.task_a_to_b,
                event.drone_a,
                event.drone_b,
                event.position_a,
                event.position_b,
            ),
            (
                event.task_b_to_a,
                event.drone_b,
                event.drone_a,
                event.position_b,
                event.position_a,
            ),
        ):
            if task_id in pickup_positions:
                p_drone, p_index = pickup_positions[task_id]
                if p_drone != giver:
                    violations.append(
                        f"任务 {task_id} 的取件无人机与交换 {event.id} 不一致"
                    )
                elif p_index >= gap_giver:
                    violations.append(
                        f"任务 {task_id} 的取件必须早于交换 {event.id}"
                    )
            else:
                violations.append(f"任务 {task_id} 缺少取件记录")
            if task_id in delivery_positions:
                d_drone, d_index = delivery_positions[task_id]
                if d_drone != receiver:
                    violations.append(
                        f"任务 {task_id} 的送达无人机与交换 {event.id} 不一致"
                    )
                elif d_index < gap_receiver:
                    violations.append(
                        f"任务 {task_id} 的送达必须晚于交换 {event.id}"
                    )
            else:
                violations.append(f"任务 {task_id} 缺少送达记录")

    for task_id in sorted(expected):
        if task_id in swap_by_task:
            continue
        if (
            task_id in pickup_drone
            and task_id in delivery_drone
            and pickup_drone[task_id] != delivery_drone[task_id]
        ):
            violations.append(f"任务 {task_id} 未参与交换但由不同无人机取送")

    return violations


def _build_swap_dag(
    problem: Problem,
    solution: SwapSolution,
    swap_service_time_min: float,
) -> tuple[
    list[str],
    list[tuple[object, object, float, float, int]],
    set[object],
]:
    """Build the event-dependency DAG for a Swap-only solution.

    Returns ``(violations, edges, nodes)`` where each edge is
    ``(from_node, to_node, time_min, distance_km, drone)``.  Node keys are
    ``("start", d)``, ``("end", d)``, ``("visit", d, index)`` and
    ``("swap", swap_id)``.
    """

    violations: list[str] = []
    routes = solution.routes
    speed = problem.speed_km_per_min
    node_by_visit = problem._visit_to_node

    swap_at_gap: dict[int, dict[int, SwapEvent]] = defaultdict(dict)
    for event in solution.swaps:
        for drone, gap in (
            (event.drone_a, event.position_a),
            (event.drone_b, event.position_b),
        ):
            if gap in swap_at_gap[drone]:
                violations.append(
                    f"无人机 {drone + 1} 在位置 {gap} 存在多个交换"
                )
            swap_at_gap[drone][gap] = event

    def leg_distance(from_visit: int | None, to_visit: int) -> float:
        from_node = 0 if from_visit is None else node_by_visit[from_visit]
        return problem._distances[from_node][node_by_visit[to_visit]]

    edges: list[tuple[object, object, float, float, int]] = []
    for drone, route in enumerate(routes):
        length = len(route)
        gaps = swap_at_gap.get(drone, {})
        prev: object = ("start", drone)
        prev_visit: int | None = None
        for index in range(length):
            visit = route[index]
            if index in gaps:
                event = gaps[index]
                meeting = event.meeting_node
                dist_in = leg_distance(prev_visit, meeting)
                dist_out = leg_distance(meeting, visit)
                edges.append(
                    (
                        prev,
                        ("swap", event.id),
                        dist_in / speed,
                        dist_in,
                        drone,
                    )
                )
                edges.append(
                    (
                        ("swap", event.id),
                        ("visit", drone, index),
                        swap_service_time_min + dist_out / speed,
                        dist_out,
                        drone,
                    )
                )
            else:
                dist = leg_distance(prev_visit, visit)
                edges.append(
                    (prev, ("visit", drone, index), dist / speed, dist, drone)
                )
            prev = ("visit", drone, index)
            prev_visit = route[index]
        if length in gaps:
            event = gaps[length]
            meeting = event.meeting_node
            dist_in = leg_distance(prev_visit, meeting)
            edges.append(
                (prev, ("swap", event.id), dist_in / speed, dist_in, drone)
            )
            edges.append(
                (
                    ("swap", event.id),
                    ("end", drone),
                    swap_service_time_min,
                    0.0,
                    drone,
                )
            )
        else:
            edges.append((prev, ("end", drone), 0.0, 0.0, drone))

    nodes: set[object] = set()
    for from_node, to_node, _, _, _ in edges:
        nodes.add(from_node)
        nodes.add(to_node)
    return violations, edges, nodes


def _propagate(
    nodes: set[object],
    edges: list[tuple[object, object, float, float, int]],
    drone_count: int,
) -> tuple[
    dict[object, float],
    dict[int, float],
    dict[int, dict[int, float]],
    bool,
]:
    """Topological propagation with cycle detection.

    Returns ``(completion, distance_by_drone, swap_arrivals, cyclic)``.
    """

    out_edges: dict[object, list[tuple[object, float, float, int]]] = defaultdict(list)
    in_degree: dict[object, int] = defaultdict(int)
    for from_node, to_node, time_min, dist, drone in edges:
        out_edges[from_node].append((to_node, time_min, dist, drone))
        in_degree[to_node] += 1
    for node in nodes:
        in_degree.setdefault(node, 0)

    completion: dict[object, float] = {}
    for drone in range(drone_count):
        completion[("start", drone)] = 0.0
    distance_by_drone: dict[int, float] = defaultdict(float)
    swap_arrivals: dict[int, dict[int, float]] = defaultdict(dict)

    queue = deque(node for node in nodes if in_degree[node] == 0)
    processed = 0
    while queue:
        node = queue.popleft()
        processed += 1
        for to_node, time_min, dist, drone in out_edges.get(node, ()):
            arrival = completion.get(node, 0.0) + time_min
            distance_by_drone[drone] += dist
            if to_node[0] == "swap":
                swap_arrivals[to_node[1]][drone] = arrival
            if arrival > completion.get(to_node, float("-inf")):
                completion[to_node] = arrival
            in_degree[to_node] -= 1
            if in_degree[to_node] == 0:
                queue.append(to_node)
    return completion, distance_by_drone, swap_arrivals, processed != len(nodes)


def _successors(
    node: object,
    edges: list[tuple[object, object, float, float, int]],
) -> list[tuple[object, float, float, int]]:
    """Successor edges of a node as ``(to_node, time, distance, drone)``."""

    return [
        (to_node, time_min, dist, drone)
        for from_node, to_node, time_min, dist, drone in edges
        if from_node == node
    ]


# ---------------------------------------------------------------------------
# Public evaluation.
# ---------------------------------------------------------------------------

def evaluate_swap_solution(
    problem: Problem,
    solution: SwapSolution,
    *,
    swap_service_time_min: float = 0.0,
) -> SwapSolutionEvaluation:
    """Validate and score a Swap-only solution through its event DAG."""

    if not isfinite(swap_service_time_min) or swap_service_time_min < 0:
        raise ValueError("交换作业时间必须为非负有限值")
    if len(solution.routes) > problem.drone_count:
        raise ValueError("路线数超过无人机数")

    violations = _structural_checks(problem, solution)
    routes = solution.routes
    capacity = problem.capacity

    dag_violations, edges, nodes = _build_swap_dag(
        problem, solution, swap_service_time_min
    )
    violations.extend(dag_violations)
    completion, distance_by_drone, swap_arrivals, cyclic = _propagate(
        nodes, edges, len(routes)
    )
    if cyclic:
        violations.append("交换导致事件依赖环（同步死锁），方案非法")

    # Custody tracking, delivery times and swap timing in topological order.
    carried: dict[int, set[int]] = defaultdict(set)
    delivery_times: dict[int, float] = {}
    late_count = 0
    total_lateness = 0.0
    max_lateness = 0.0
    swap_timings: list[SwapEventTiming] = []
    waiting_time = 0.0
    swap_by_id: dict[int, SwapEvent] = {event.id: event for event in solution.swaps}

    in_degree2: dict[object, int] = {}
    for from_node, to_node, _, _, _ in edges:
        in_degree2[to_node] = in_degree2.get(to_node, 0) + 1
    for node in nodes:
        in_degree2.setdefault(node, 0)
    queue = deque(node for node in nodes if in_degree2[node] == 0)
    while queue:
        node = queue.popleft()
        kind = node[0]
        if kind == "visit":
            _, drone, index = node
            visit = routes[drone][index]
            task_id = abs(visit)
            if visit > 0:
                carried[drone].add(task_id)
                if len(carried[drone]) > capacity:
                    violations.append(
                        f"无人机 {drone + 1} 在取件任务 {task_id} 后载荷 {len(carried[drone])} 超过上限 {capacity}"
                    )
            else:
                if task_id not in carried[drone]:
                    violations.append(
                        f"任务 {task_id} 在无人机 {drone + 1} 送达但该机未持有包裹（瞬移）"
                    )
                else:
                    carried[drone].remove(task_id)
                delivered_at = completion[node]
                delivery_times[task_id] = delivered_at
                lateness = max(
                    0.0, delivered_at - problem.task(task_id).deadline_min
                )
                if lateness > 1e-9:
                    late_count += 1
                    total_lateness += lateness
                    max_lateness = max(max_lateness, lateness)
        elif kind == "swap":
            event = swap_by_id[node[1]]
            if event.task_a_to_b not in carried[event.drone_a]:
                violations.append(
                    f"交换 {event.id}: 无人机 {event.drone_a + 1} 未持有任务 {event.task_a_to_b}"
                )
            if event.task_b_to_a not in carried[event.drone_b]:
                violations.append(
                    f"交换 {event.id}: 无人机 {event.drone_b + 1} 未持有任务 {event.task_b_to_a}"
                )
            carried[event.drone_a].discard(event.task_a_to_b)
            carried[event.drone_a].add(event.task_b_to_a)
            carried[event.drone_b].discard(event.task_b_to_a)
            carried[event.drone_b].add(event.task_a_to_b)
            if len(carried[event.drone_a]) > capacity or len(
                carried[event.drone_b]
            ) > capacity:
                violations.append(f"交换 {event.id} 后载荷超过上限 {capacity}")

            arrivals = swap_arrivals.get(event.id, {})
            arrival_a = arrivals.get(event.drone_a, 0.0)
            arrival_b = arrivals.get(event.drone_b, 0.0)
            swap_time = max(arrival_a, arrival_b)
            waiting_a = max(0.0, arrival_b - arrival_a)
            waiting_b = max(0.0, arrival_a - arrival_b)
            waiting_time += waiting_a + waiting_b
            swap_timings.append(
                SwapEventTiming(
                    swap_id=event.id,
                    drone_a=event.drone_a,
                    drone_b=event.drone_b,
                    task_a_to_b=event.task_a_to_b,
                    task_b_to_a=event.task_b_to_a,
                    meeting_node=event.meeting_node,
                    position_a=event.position_a,
                    position_b=event.position_b,
                    arrival_a_min=arrival_a,
                    arrival_b_min=arrival_b,
                    swap_time_min=swap_time,
                    swap_end_min=swap_time + swap_service_time_min,
                    waiting_a_min=waiting_a,
                    waiting_b_min=waiting_b,
                )
            )
        for to_node, _, _, _ in _successors(node, edges):
            in_degree2[to_node] -= 1
            if in_degree2[to_node] == 0:
                queue.append(to_node)

    total_distance = sum(distance_by_drone.values())
    route_distances = tuple(
        distance_by_drone.get(drone, 0.0) for drone in range(len(routes))
    )
    route_completion_times = tuple(
        completion.get(("end", drone), 0.0) for drone in range(len(routes))
    )
    swap_timings.sort(key=lambda timing: timing.swap_id)

    return SwapSolutionEvaluation(
        valid=not violations,
        score=Score(late_count, total_lateness, total_distance),
        delivery_times_min=MappingProxyType(delivery_times),
        total_lateness_min=total_lateness,
        max_lateness_min=max_lateness,
        violations=tuple(violations),
        route_distances_km=route_distances,
        route_completion_times_min=route_completion_times,
        waiting_time_min=waiting_time,
        swap_timings=tuple(swap_timings),
    )


def gap_completion_times(
    problem: Problem,
    solution: SwapSolution,
    *,
    swap_service_time_min: float = 0.0,
) -> tuple[tuple[float, ...], ...]:
    """Per-drone ready time at every gap of the current routes.

    ``result[drone][gap]`` is the time the drone has completed every event
    before that gap (the arrival time at the visit right before it, or 0 for
    gap 0), taking any existing swaps into account.  Used by the search only as
    a cheap screening estimate; the final verdict always comes from
    :func:`evaluate_swap_solution`.
    """

    routes = solution.routes
    _, edges, nodes = _build_swap_dag(problem, solution, swap_service_time_min)
    completion, _, _, _ = _propagate(nodes, edges, len(routes))
    gap_times: list[tuple[float, ...]] = []
    for drone, route in enumerate(routes):
        times = [0.0]
        for index in range(len(route)):
            times.append(completion.get(("visit", drone, index), 0.0))
        gap_times.append(tuple(times))
    return tuple(gap_times)
