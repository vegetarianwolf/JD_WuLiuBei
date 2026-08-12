"""Global, asynchronous evaluation of Relay solutions.

The Relay model couples drone timelines only through the precedence edge
``RELAY_DROP -> RELAY_PICK`` at each station.  Unlike the synchronous Swap
model, there is **no** shared ``("swap", id)`` meeting node: the first drone
drops the package and leaves immediately, the station stores it, and the
second drone picks it up later.  The two timelines are therefore only coupled
by ``t_pick = max(second_drone_arrival, t_drop + handling_time)``.

This module builds the complete event-dependency DAG with two independent
nodes per relay, propagates arrival times with a Kahn topological sort (any
remaining unprocessed node means a temporal dependency cycle and therefore an
infeasible solution), and tracks package custody across drones and stations.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from math import isfinite
from types import MappingProxyType
from typing import Mapping, Sequence

from .model import Problem, Score
from .relay_model import (
    RELAY_ENDPOINT_EPS,
    RELAY_HANDLING_TIME_MIN,
    RelaySolution,
    RelayStation,
    RelayTransfer,
    relays_by_task,
    station_for_id,
)


@dataclass(frozen=True, slots=True)
class RelayTransferTiming:
    """Asynchronous timing of one executed relay transfer."""

    relay_id: int
    task_id: int
    station_id: int
    first_drone: int
    second_drone: int
    drop_position: int
    pick_position: int
    drop_time_min: float
    ready_time_min: float
    pick_time_min: float
    delivery_time_min: float
    second_arrival_min: float
    storage_time_min: float
    receiver_wait_min: float


@dataclass(frozen=True, slots=True)
class RelaySolutionEvaluation:
    """Metrics recomputed from a complete Relay solution.

    ``score`` is strictly ``(late_count, distance_km)``.  All lateness,
    storage and waiting metrics are descriptive diagnostics only and never
    participate in the Score order.
    """

    valid: bool
    score: Score
    total_lateness_min: float
    max_lateness_min: float
    delivery_times_min: Mapping[int, float]
    pickup_times_min: Mapping[int, float]
    route_distances_km: tuple[float, ...]
    route_completion_times_min: tuple[float, ...]
    relay_count: int
    direct_task_count: int
    station_storage_time_total: float
    station_storage_time_mean: float
    station_storage_time_max: float
    receiver_wait_total: float
    receiver_wait_mean: float
    capacity_release_events: int
    early_release_gain_total: float
    station_max_inventory: Mapping[int, int]
    violations: tuple[str, ...]
    relay_timings: tuple[RelayTransferTiming, ...]

    @property
    def on_time_rate(self) -> float:
        delivered = len(self.delivery_times_min)
        if delivered == 0:
            return 0.0
        return (delivered - self.score.late_count) / delivered

    @property
    def max_route_distance_km(self) -> float:
        return max(self.route_distances_km, default=0.0)

    @property
    def max_route_completion_min(self) -> float:
        return max(self.route_completion_times_min, default=0.0)


def _structural_checks(
    problem: Problem,
    solution: RelaySolution,
    *,
    require_all_tasks: bool = True,
) -> list[str]:
    """Route-level and relay-structure checks independent of timing.

    When ``require_all_tasks`` is False the task-completeness check is
    skipped so a partial solution (e.g. mid-repair, with tasks still
    unplaced) can be structurally validated.  Relay consistency, capacity
    and timing checks still run.
    """

    violations: list[str] = []
    routes = solution.routes
    if len(routes) > problem.drone_count:
        violations.append(
            f"路线数 {len(routes)} 超过无人机数 {problem.drone_count}"
        )

    stations_by_id = {station.id: station for station in solution.stations}
    station_ids = {station.id for station in solution.stations}

    pickup_drone: dict[int, int] = {}
    delivery_drone: dict[int, int] = {}
    pickup_index: dict[int, int] = {}
    delivery_index: dict[int, int] = {}
    for drone, route in enumerate(routes):
        for index, visit in enumerate(route):
            task_id = abs(visit)
            if visit > 0:
                if task_id in pickup_drone:
                    violations.append(f"任务 {task_id} 被重复取件")
                pickup_drone[task_id] = drone
                pickup_index[task_id] = index
            else:
                if task_id in delivery_drone:
                    violations.append(f"任务 {task_id} 被重复送达")
                delivery_drone[task_id] = drone
                delivery_index[task_id] = index

    expected = set(problem.task_ids)
    if require_all_tasks:
        if set(pickup_drone) != expected or set(delivery_drone) != expected:
            missing = sorted(
                (expected - set(pickup_drone)) | (expected - set(delivery_drone))
            )
            violations.append(f"缺少任务的完整取送记录: {missing}")

    owned_counts: dict[int, int] = defaultdict(int)
    for task_id, drone in pickup_drone.items():
        owned_counts[drone] += 1
    for drone, count in sorted(owned_counts.items()):
        if count > problem.max_tasks_per_drone:
            violations.append(
                f"无人机 {drone + 1} 承接 {count} 个取件任务，超过上限 {problem.max_tasks_per_drone}"
            )

    # Direct cross-UAV delivery without a relay transfer is invalid.
    relay_tasks = set()
    for relay in solution.relays:
        if relay.task_id in relay_tasks:
            violations.append(f"任务 {relay.task_id} 存在多个中继转移")
        relay_tasks.add(relay.task_id)
        if relay.station_id not in station_ids:
            violations.append(
                f"中继 {relay.id} 引用不存在的中继站 {relay.station_id}"
            )
            continue
        station = stations_by_id[relay.station_id]
        if relay.task_id not in pickup_drone or relay.task_id not in delivery_drone:
            continue
        if pickup_drone[relay.task_id] != relay.first_drone:
            violations.append(
                f"中继 {relay.id}: 任务 {relay.task_id} 的取件无人机与 first 无人机不一致"
            )
        if delivery_drone[relay.task_id] != relay.second_drone:
            violations.append(
                f"中继 {relay.id}: 任务 {relay.task_id} 的送达无人机与 second 无人机不一致"
            )
        first_route = routes[relay.first_drone]
        second_route = routes[relay.second_drone]
        if relay.drop_position > len(first_route):
            violations.append(
                f"中继 {relay.id}: drop 位置 {relay.drop_position} 超出 first 路线长度"
            )
        elif relay.drop_position <= pickup_index.get(relay.task_id, -1):
            violations.append(
                f"中继 {relay.id}: drop 必须发生在取件之后"
            )
        if relay.pick_position > len(second_route):
            violations.append(
                f"中继 {relay.id}: pick 位置 {relay.pick_position} 超出 second 路线长度"
            )
        elif relay.pick_position > delivery_index.get(relay.task_id, len(second_route)):
            violations.append(
                f"中继 {relay.id}: pick 必须发生在送达之前"
            )

        # Station endpoint degeneracy: a task may not use its own P or D.
        task = problem.task(relay.task_id)
        if station.point.distance_to(task.pickup) <= RELAY_ENDPOINT_EPS:
            violations.append(
                f"中继 {relay.id}: 中继站与任务 {relay.task_id} 的取件点重合"
            )
        if station.point.distance_to(task.delivery) <= RELAY_ENDPOINT_EPS:
            violations.append(
                f"中继 {relay.id}: 中继站与任务 {relay.task_id} 的送达点重合"
            )

    for task_id in sorted(expected - relay_tasks):
        if (
            task_id in pickup_drone
            and task_id in delivery_drone
            and pickup_drone[task_id] != delivery_drone[task_id]
        ):
            violations.append(
                f"任务 {task_id} 未参与中继但由不同无人机取送"
            )

    # A single drone may not both drop and pick at the same route gap.
    gap_events: dict[tuple[int, int], list[str]] = defaultdict(list)
    for relay in solution.relays:
        gap_events[(relay.first_drone, relay.drop_position)].append("drop")
        gap_events[(relay.second_drone, relay.pick_position)].append("pick")
    for (drone, gap), kinds in sorted(gap_events.items()):
        if len(set(kinds)) > 1:
            violations.append(
                f"无人机 {drone + 1} 在位置 {gap} 同时存在中继放下与取走"
            )

    return violations


def _build_relay_dag(
    problem: Problem,
    solution: RelaySolution,
    handling_time_min: float,
) -> tuple[
    list[str],
    list[tuple[object, object, float, float, int]],
    set[object],
]:
    """Build the event-dependency DAG for a Relay solution.

    Returns ``(violations, edges, nodes)`` where each edge is
    ``(from_node, to_node, time_min, distance_km, drone)``.  Node keys are
    ``("start", d)``, ``("end", d)``, ``("visit", d, index)``,
    ``("relay_drop", relay_id)`` and ``("relay_pick", relay_id)``.
    """

    violations: list[str] = []
    routes = solution.routes
    speed = problem.speed_km_per_min
    node_by_visit = problem._visit_to_node
    stations_by_id = {station.id: station for station in solution.stations}

    drop_at_gap: dict[int, dict[int, RelayTransfer]] = defaultdict(dict)
    pick_at_gap: dict[int, dict[int, RelayTransfer]] = defaultdict(dict)
    for relay in solution.relays:
        if relay.station_id not in stations_by_id:
            continue
        drop_at_gap[relay.first_drone][relay.drop_position] = relay
        pick_at_gap[relay.second_drone][relay.pick_position] = relay

    def leg_distance(from_visit: int | None, to_visit: int) -> float:
        from_node = 0 if from_visit is None else node_by_visit[from_visit]
        return problem._distances[from_node][node_by_visit[to_visit]]

    def station_visit(station: RelayStation) -> int:
        return station.source_visit

    edges: list[tuple[object, object, float, float, int]] = []
    for drone, route in enumerate(routes):
        length = len(route)
        drops = drop_at_gap.get(drone, {})
        picks = pick_at_gap.get(drone, {})
        prev: object = ("start", drone)
        prev_visit: int | None = None
        for index in range(length):
            visit = route[index]
            drop = drops.get(index)
            pick = picks.get(index)
            if drop is not None:
                station = stations_by_id[drop.station_id]
                dist_in = leg_distance(prev_visit, station_visit(station))
                dist_out = leg_distance(station_visit(station), visit)
                edges.append(
                    (
                        prev,
                        ("relay_drop", drop.id),
                        dist_in / speed,
                        dist_in,
                        drone,
                    )
                )
                edges.append(
                    (
                        ("relay_drop", drop.id),
                        ("visit", drone, index),
                        dist_out / speed,
                        dist_out,
                        drone,
                    )
                )
            elif pick is not None:
                station = stations_by_id[pick.station_id]
                dist_in = leg_distance(prev_visit, station_visit(station))
                dist_out = leg_distance(station_visit(station), visit)
                edges.append(
                    (
                        prev,
                        ("relay_pick", pick.id),
                        dist_in / speed,
                        dist_in,
                        drone,
                    )
                )
                edges.append(
                    (
                        ("relay_pick", pick.id),
                        ("visit", drone, index),
                        dist_out / speed,
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
            prev_visit = visit
        if length in drops:
            event = drops[length]
            station = stations_by_id[event.station_id]
            dist_in = leg_distance(prev_visit, station_visit(station))
            edges.append(
                (prev, ("relay_drop", event.id), dist_in / speed, dist_in, drone)
            )
            edges.append(
                (
                    ("relay_drop", event.id),
                    ("end", drone),
                    0.0,
                    0.0,
                    drone,
                )
            )
        elif length in picks:
            event = picks[length]
            station = stations_by_id[event.station_id]
            dist_in = leg_distance(prev_visit, station_visit(station))
            edges.append(
                (prev, ("relay_pick", event.id), dist_in / speed, dist_in, drone)
            )
            edges.append(
                (
                    ("relay_pick", event.id),
                    ("end", drone),
                    0.0,
                    0.0,
                    drone,
                )
            )
        else:
            edges.append((prev, ("end", drone), 0.0, 0.0, drone))

    # Async precedence edge DROP -> PICK with the handling time as its weight.
    for relay in solution.relays:
        if relay.station_id not in stations_by_id:
            continue
        edges.append(
            (
                ("relay_drop", relay.id),
                ("relay_pick", relay.id),
                handling_time_min,
                0.0,
                relay.first_drone,
            )
        )

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
    dict[object, dict[object, float]],
    bool,
]:
    """Topological propagation with cycle detection.

    Returns ``(completion, distance_by_drone, incoming, cyclic)`` where
    ``incoming[node][from_node]`` is the arrival value contributed by that
    incoming edge (used to recover station wait diagnostics).
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
    incoming: dict[object, dict[object, float]] = defaultdict(dict)

    queue = deque(node for node in nodes if in_degree[node] == 0)
    processed = 0
    while queue:
        node = queue.popleft()
        processed += 1
        for to_node, time_min, dist, drone in out_edges.get(node, ()):
            arrival = completion.get(node, 0.0) + time_min
            distance_by_drone[drone] += dist
            if arrival > incoming[to_node].get(node, float("-inf")):
                incoming[to_node][node] = arrival
            if arrival > completion.get(to_node, float("-inf")):
                completion[to_node] = arrival
            in_degree[to_node] -= 1
            if in_degree[to_node] == 0:
                queue.append(to_node)
    return completion, distance_by_drone, incoming, processed != len(nodes)


# ---------------------------------------------------------------------------
# Public evaluation.
# ---------------------------------------------------------------------------

def evaluate_relay_solution(
    problem: Problem,
    solution: RelaySolution,
    *,
    handling_time_min: float = RELAY_HANDLING_TIME_MIN,
    require_all_tasks: bool = True,
) -> RelaySolutionEvaluation:
    """Validate and score a Relay solution through its event DAG.

    ``handling_time_min`` defaults to 0 (the problem ignores other operation
    time).  The returned Score is the strict two-layer ``(late_count,
    distance_km)`` order; all lateness/storage/waiting metrics are diagnostics.

    ``require_all_tasks=False`` skips the task-completeness check so partial
    solutions (e.g. a solution still missing tasks mid-repair) can still be
    structurally evaluated.  This is what lets relay insertions be generated
    and compared during multi-mode repair.
    """

    if not isfinite(handling_time_min) or handling_time_min < 0:
        raise ValueError("中继作业时间必须为非负有限值")
    if len(solution.routes) > problem.drone_count:
        raise ValueError("路线数超过无人机数")

    violations = _structural_checks(
        problem, solution, require_all_tasks=require_all_tasks
    )
    routes = solution.routes
    capacity = problem.capacity

    dag_violations, edges, nodes = _build_relay_dag(
        problem, solution, handling_time_min
    )
    violations.extend(dag_violations)
    completion, distance_by_drone, incoming, cyclic = _propagate(
        nodes, edges, len(routes)
    )
    if cyclic:
        violations.append("中继导致事件依赖环（时序死锁），方案非法")

    relays_by_id = {relay.id: relay for relay in solution.relays}
    stations_by_id = {station.id: station for station in solution.stations}

    carried_by_uav: dict[int, set[int]] = defaultdict(set)
    stored_at_station: dict[int, set[int]] = defaultdict(set)
    station_max_inventory: dict[int, int] = defaultdict(int)
    delivery_times: dict[int, float] = {}
    pickup_times: dict[int, float] = {}
    late_count = 0
    total_lateness = 0.0
    max_lateness = 0.0
    relay_timings: list[RelayTransferTiming] = []
    storage_total = 0.0
    storage_max = 0.0
    wait_total = 0.0
    early_release_total = 0.0

    in_degree2: dict[object, int] = {}
    out_adj: dict[object, list[object]] = defaultdict(list)
    for from_node, to_node, _, _, _ in edges:
        in_degree2[to_node] = in_degree2.get(to_node, 0) + 1
        out_adj[from_node].append(to_node)
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
                carried_by_uav[drone].add(task_id)
                pickup_times[task_id] = completion[node]
                if len(carried_by_uav[drone]) > capacity:
                    violations.append(
                        f"无人机 {drone + 1} 在取件任务 {task_id} 后载荷 {len(carried_by_uav[drone])} 超过上限 {capacity}"
                    )
            else:
                if task_id not in carried_by_uav[drone]:
                    violations.append(
                        f"任务 {task_id} 在无人机 {drone + 1} 送达但该机未持有包裹（瞬移）"
                    )
                else:
                    carried_by_uav[drone].remove(task_id)
                delivered_at = completion[node]
                delivery_times[task_id] = delivered_at
                lateness = max(
                    0.0, delivered_at - problem.task(task_id).deadline_min
                )
                if lateness > 1e-9:
                    late_count += 1
                    total_lateness += lateness
                    max_lateness = max(max_lateness, lateness)
        elif kind == "relay_drop":
            relay = relays_by_id[node[1]]
            task_id = relay.task_id
            if task_id not in carried_by_uav[relay.first_drone]:
                violations.append(
                    f"中继 {relay.id}: 无人机 {relay.first_drone + 1} 未持有任务 {task_id}"
                )
            else:
                carried_by_uav[relay.first_drone].remove(task_id)
            stored_at_station[relay.station_id].add(task_id)
            if (
                len(stored_at_station[relay.station_id])
                > station_max_inventory[relay.station_id]
            ):
                station_max_inventory[relay.station_id] = len(
                    stored_at_station[relay.station_id]
                )
        elif kind == "relay_pick":
            relay = relays_by_id[node[1]]
            task_id = relay.task_id
            if task_id not in stored_at_station[relay.station_id]:
                violations.append(
                    f"中继 {relay.id}: 任务 {task_id} 不在中继站 {relay.station_id} 存储中"
                )
            else:
                stored_at_station[relay.station_id].remove(task_id)
            if task_id in carried_by_uav[relay.second_drone]:
                violations.append(
                    f"中继 {relay.id}: 任务 {task_id} 已在 second 无人机上（重复持有）"
                )
            carried_by_uav[relay.second_drone].add(task_id)
            if len(carried_by_uav[relay.second_drone]) > capacity:
                violations.append(
                    f"中继 {relay.id}: second 无人机 {relay.second_drone + 1} 在接收任务 {task_id} 后载荷超过上限 {capacity}"
                )
        for to_node in out_adj.get(node, ()):
            in_degree2[to_node] -= 1
            if in_degree2[to_node] == 0:
                queue.append(to_node)

    # Relay timing diagnostics (only meaningful when the structure is sound).
    for relay in sorted(solution.relays, key=lambda item: item.id):
        drop_time = completion.get(("relay_drop", relay.id), float("nan"))
        pick_time = completion.get(("relay_pick", relay.id), float("nan"))
        ready_time = drop_time + handling_time_min
        second_arrival = float("-inf")
        for from_node, arrival in incoming[("relay_pick", relay.id)].items():
            if from_node != ("relay_drop", relay.id):
                second_arrival = max(second_arrival, arrival)
        if second_arrival == float("-inf"):
            second_arrival = float("nan")
        storage_time = max(0.0, pick_time - drop_time)
        receiver_wait = max(0.0, ready_time - second_arrival)
        storage_total += storage_time
        storage_max = max(storage_max, storage_time)
        wait_total += receiver_wait
        early_release = max(
            0.0, problem.direct_completion_min(relay.task_id) - drop_time
        )
        early_release_total += early_release
        relay_timings.append(
            RelayTransferTiming(
                relay_id=relay.id,
                task_id=relay.task_id,
                station_id=relay.station_id,
                first_drone=relay.first_drone,
                second_drone=relay.second_drone,
                drop_position=relay.drop_position,
                pick_position=relay.pick_position,
                drop_time_min=drop_time,
                ready_time_min=ready_time,
                pick_time_min=pick_time,
                delivery_time_min=delivery_times.get(relay.task_id, float("nan")),
                second_arrival_min=second_arrival,
                storage_time_min=storage_time,
                receiver_wait_min=receiver_wait,
            )
        )

    total_distance = sum(distance_by_drone.values())
    route_distances = tuple(
        distance_by_drone.get(drone, 0.0) for drone in range(len(routes))
    )
    route_completion_times = tuple(
        completion.get(("end", drone), 0.0) for drone in range(len(routes))
    )
    relay_count = len(solution.relays)
    direct_task_count = len(problem.tasks) - relay_count
    station_storage_time_mean = (
        storage_total / relay_count if relay_count else 0.0
    )
    receiver_wait_mean = wait_total / relay_count if relay_count else 0.0

    return RelaySolutionEvaluation(
        valid=not violations,
        score=Score(late_count, total_lateness, total_distance),
        total_lateness_min=total_lateness,
        max_lateness_min=max_lateness,
        delivery_times_min=MappingProxyType(delivery_times),
        pickup_times_min=MappingProxyType(pickup_times),
        route_distances_km=route_distances,
        route_completion_times_min=route_completion_times,
        relay_count=relay_count,
        direct_task_count=direct_task_count,
        station_storage_time_total=storage_total,
        station_storage_time_mean=station_storage_time_mean,
        station_storage_time_max=storage_max,
        receiver_wait_total=wait_total,
        receiver_wait_mean=receiver_wait_mean,
        capacity_release_events=relay_count,
        early_release_gain_total=early_release_total,
        station_max_inventory=MappingProxyType(dict(station_max_inventory)),
        violations=tuple(violations),
        relay_timings=tuple(relay_timings),
    )


def relay_gap_completion_times(
    problem: Problem,
    solution: RelaySolution,
    handling_time_min: float = 0.0,
) -> tuple[dict[int, tuple[float, ...]], bool]:
    """Extract per-drone per-gap ready times from the relay DAG.

    Returns ``(gap_times, cyclic)`` where ``gap_times[drone][gap]`` is the
    completion time just before route index ``gap`` (including any relay
    events inserted at earlier gaps).  Search code uses this only as a cheap
    feasibility screen; the final verdict always comes from
    :func:`evaluate_relay_solution`.
    """

    _, edges, nodes = _build_relay_dag(problem, solution, handling_time_min)
    completion, _, _, cyclic = _propagate(nodes, edges, len(solution.routes))
    result: dict[int, tuple[float, ...]] = {}
    for drone, route in enumerate(solution.routes):
        times = [0.0]
        for index in range(len(route)):
            times.append(completion.get(("visit", drone, index), 0.0))
        result[drone] = tuple(times)
    return result, cyclic
