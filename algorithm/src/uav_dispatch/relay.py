"""Relay station placement, handoff scheduling, and original-task plan indexing.

This module owns the fixed relay infrastructure:

* :func:`build_relay_network` runs once before the ALNS search and returns an
  immutable :class:`RelayNetwork`: station coordinates, the transport-leg
  registry (one RELAY_IN/RELAY_OUT pair per task per candidate station), and
  the per-task relay candidate cache.
* :class:`GlobalRelayEvaluator` schedules a complete candidate solution on a
  precedence DAG (route succession edges plus relay handoff edges) using
  topological longest-path earliest-time propagation; a cycle means deadlock
  and the candidate is infeasible.
* :func:`build_plan_index` derives the original-task plan layer from the
  routes, which remain the single source of truth.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from math import isfinite
from random import Random
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from .counters import SearchCounters
from .model import (
    Point,
    Problem,
    RelayStation,
    Score,
    Task,
    TaskPlan,
    TransportLeg,
)

Route = tuple[int, ...]
Routes = tuple[Route, ...]


@dataclass(frozen=True, slots=True)
class RelayNetwork:
    """Everything the relay-aware problem needs, fixed before ALNS starts."""

    stations: tuple[RelayStation, ...]
    leg_registry: Mapping[int, TransportLeg]
    task_relay_candidates: Mapping[int, tuple[int, ...]]
    metadata: Mapping[str, object]


def build_relay_network(
    tasks: Sequence[Task],
    *,
    relay_count: int,
    candidates_per_task: int = 2,
    detour_ratio: float = 1.5,
    location_seed: int = 42,
) -> RelayNetwork:
    """Place fixed relay stations with weighted k-medoids on task midpoints.

    The weight of task i is ``1 + alpha * normalized_distance +
    beta * deadline_urgency`` with alpha = beta = 1.0.  Stations are placed
    at medoid midpoints, then each task keeps its top ``candidates_per_task``
    stations within ``detour_ratio``; stations outside the corridor get no
    candidate at all (no fallback).
    """

    if relay_count < 0:
        raise ValueError("中继站数量不能为负")
    if candidates_per_task <= 0:
        raise ValueError("每个任务的中继候选数必须为正整数")
    if not isfinite(detour_ratio) or detour_ratio < 1.0:
        raise ValueError("绕行比阈值必须是不小于 1 的有限数值")

    sorted_tasks = tuple(sorted(tasks, key=lambda task: task.id))
    if relay_count == 0:
        return RelayNetwork(
            (),
            MappingProxyType({}),
            MappingProxyType({}),
            MappingProxyType(
                {
                    "relay_count": 0,
                    "candidates_per_task": candidates_per_task,
                    "detour_ratio": detour_ratio,
                    "location_seed": location_seed,
                }
            ),
        )

    rng = Random(location_seed)
    midpoints = tuple(
        Point(
            (task.pickup.x + task.delivery.x) / 2.0,
            (task.pickup.y + task.delivery.y) / 2.0,
        )
        for task in sorted_tasks
    )
    pair_distances = tuple(
        task.pickup.distance_to(task.delivery) for task in sorted_tasks
    )
    distance_min = min(pair_distances)
    distance_span = max(pair_distances) - distance_min
    deadline_max = max(task.deadline_min for task in sorted_tasks)
    deadline_span = deadline_max - min(
        task.deadline_min for task in sorted_tasks
    )
    weights = tuple(
        1.0
        + (
            0.0
            if distance_span <= 1e-12
            else (pair_distances[index] - distance_min) / distance_span
        )
        + (
            0.0
            if deadline_span <= 1e-12
            else (deadline_max - task.deadline_min) / deadline_span
        )
        for index, task in enumerate(sorted_tasks)
    )

    centers = _weighted_k_medoids(midpoints, weights, relay_count, rng)
    stations = tuple(
        RelayStation(index + 1, center.x, center.y)
        for index, center in enumerate(centers)
    )

    candidates: dict[int, tuple[int, ...]] = {}
    for task in sorted_tasks:
        direct_distance = max(
            task.pickup.distance_to(task.delivery), 1e-9
        )
        detours = [
            (
                (
                    task.pickup.distance_to(station.point)
                    + station.point.distance_to(task.delivery)
                )
                / direct_distance,
                station.id,
            )
            for station in stations
        ]
        detours.sort(key=lambda item: (item[0], item[1]))
        within = [
            item for item in detours if item[0] <= detour_ratio + 1e-12
        ]
        # No fallback: stations clearly outside the P-D corridor get no
        # relay candidates at all, keeping the search space bounded.
        candidates[task.id] = tuple(
            relay_id for _, relay_id in within[:candidates_per_task]
        )

    counter = max(task.id for task in sorted_tasks) + 1
    registry: dict[int, TransportLeg] = {}
    for task in sorted_tasks:
        for relay_id in candidates[task.id]:
            in_id = counter
            counter += 1
            out_id = counter
            counter += 1
            registry[in_id] = TransportLeg(in_id, task.id, "RELAY_IN", relay_id)
            registry[out_id] = TransportLeg(
                out_id, task.id, "RELAY_OUT", relay_id
            )

    return RelayNetwork(
        stations,
        MappingProxyType(registry),
        MappingProxyType(candidates),
        MappingProxyType(
            {
                "relay_count": relay_count,
                "candidates_per_task": candidates_per_task,
                "detour_ratio": detour_ratio,
                "location_seed": location_seed,
                "stations": [
                    [station.id, station.x, station.y] for station in stations
                ],
                "weights": [weight for weight in weights],
            }
        ),
    )


def _weighted_k_medoids(
    midpoints: Sequence[Point],
    weights: Sequence[float],
    k: int,
    rng: Random,
) -> list[Point]:
    """Deterministic greedy weighted k-medoids with Lloyd-style refinement."""

    count = len(midpoints)
    if count == 0:
        return []
    if k >= count:
        return list(midpoints)

    def gap(left: Point, right: Point) -> float:
        return left.distance_to(right)

    chosen = _greedy_medoids(midpoints, weights, k, rng)
    for _ in range(10):
        clusters: list[list[int]] = [[] for _ in chosen]
        for index in range(count):
            nearest_center = min(
                range(len(chosen)),
                key=lambda center: gap(
                    midpoints[index], midpoints[chosen[center]]
                ),
            )
            clusters[nearest_center].append(index)
        changed = False
        for center in range(len(chosen)):
            if not clusters[center]:
                continue
            best = min(
                clusters[center],
                key=lambda candidate: sum(
                    weights[member]
                    * gap(midpoints[candidate], midpoints[member])
                    for member in clusters[center]
                ),
            )
            if best != chosen[center]:
                chosen[center] = best
                changed = True
        if not changed:
            break
    return [midpoints[index] for index in chosen]


def _greedy_medoids(
    midpoints: Sequence[Point],
    weights: Sequence[float],
    k: int,
    rng: Random,
) -> list[int]:
    """Greedy farthest-first-style weighted k-medoids seeding."""

    count = len(midpoints)

    def gap(left: Point, right: Point) -> float:
        return left.distance_to(right)

    max_weight = max(weights)
    tied = [
        index
        for index, weight in enumerate(weights)
        if weight == max_weight
    ]
    chosen: list[int] = [rng.choice(tied)]
    while len(chosen) < k:
        best_index = -1
        best_value = (-1.0, -1.0, -1)
        for candidate in range(count):
            if candidate in chosen:
                continue
            nearest_candidate = min(
                gap(midpoints[candidate], midpoints[center])
                for center in chosen
            )
            gain = 0.0
            for index in range(count):
                if index in chosen:
                    continue
                nearest = min(
                    gap(midpoints[index], midpoints[center])
                    for center in chosen
                )
                gain += weights[index] * max(
                    0.0, nearest - gap(midpoints[index], midpoints[candidate])
                )
            value = (gain, -nearest_candidate, -candidate)
            if value > best_value:
                best_value = value
                best_index = candidate
        chosen.append(best_index)
    return chosen


@dataclass(frozen=True, slots=True)
class PlanIndex:
    """Derived original-task layer; routes stay the single source of truth."""

    task_plans: Mapping[int, TaskPlan]
    task_visits: Mapping[int, tuple[int, ...]]
    task_routes: Mapping[int, tuple[int, ...]]
    direct_task_ids: tuple[int, ...]
    relay_task_ids: tuple[int, ...]
    inbound_route_by_task: Mapping[int, int]
    outbound_route_by_task: Mapping[int, int]
    inbound_position_by_task: Mapping[int, int]
    outbound_position_by_task: Mapping[int, int]
    delivery_route_by_task: Mapping[int, int]


def build_plan_index(
    problem: Problem, routes: Iterable[Sequence[int]]
) -> PlanIndex:
    """Derive the immutable TaskPlan layer from the route representation."""

    materialized = tuple(tuple(route) for route in routes)
    registry = problem.leg_registry
    direct_visits: dict[int, list[int]] = {}
    direct_positions: dict[int, list[int]] = {}
    direct_routes: dict[int, set[int]] = {}
    leg_visits: dict[int, list[int]] = {}
    leg_positions: dict[int, list[int]] = {}
    leg_routes: dict[int, set[int]] = {}
    task_visits: dict[int, list[int]] = {}
    task_routes: dict[int, set[int]] = {}

    for route_index, route in enumerate(materialized):
        for position, visit in enumerate(route):
            leg = registry.get(abs(visit)) if registry else None
            if leg is not None:
                task_id = leg.task_id
                leg_visits.setdefault(leg.id, []).append(visit)
                leg_positions.setdefault(leg.id, []).append(position)
                leg_routes.setdefault(leg.id, set()).add(route_index)
            else:
                task_id = abs(visit)
                direct_visits.setdefault(task_id, []).append(visit)
                direct_positions.setdefault(task_id, []).append(position)
                direct_routes.setdefault(task_id, set()).add(route_index)
            task_visits.setdefault(task_id, []).append(visit)
            task_routes.setdefault(task_id, set()).add(route_index)

    plans: dict[int, TaskPlan] = {}
    inbound_route: dict[int, int] = {}
    outbound_route: dict[int, int] = {}
    inbound_position: dict[int, int] = {}
    outbound_position: dict[int, int] = {}
    delivery_route: dict[int, int] = {}
    direct_ids: list[int] = []
    relay_ids: list[int] = []

    for task_id in sorted(task_visits):
        direct = direct_visits.get(task_id, [])
        legs = {
            leg.id: leg_visits.get(leg.id, [])
            for leg in problem._legs_by_task.get(task_id, ())
        } if registry else {}
        has_direct = bool(direct)
        has_legs = any(legs.values())
        if has_direct and has_legs:
            raise ValueError(f"任务 {task_id} 同时存在 DIRECT 与 RELAY 访问")
        if has_direct:
            if sorted(direct) != [-task_id, task_id]:
                raise ValueError(f"任务 {task_id} 的取送访问不完整")
            if len(direct_routes.get(task_id, set())) != 1:
                raise ValueError(f"任务 {task_id} 的取送被拆分到不同无人机")
            if direct_positions[task_id][
                direct.index(task_id)
            ] > direct_positions[task_id][direct.index(-task_id)]:
                raise ValueError(f"任务 {task_id} 的取送顺序非法")
            plans[task_id] = TaskPlan(task_id, "DIRECT", None, ())
            delivery_route[task_id] = next(iter(direct_routes[task_id]))
            direct_ids.append(task_id)
        elif has_legs:
            in_legs = [
                leg
                for leg in problem._legs_by_task.get(task_id, ())
                if leg.kind == "RELAY_IN"
                and leg_visits.get(leg.id)
            ]
            out_legs = [
                leg
                for leg in problem._legs_by_task.get(task_id, ())
                if leg.kind == "RELAY_OUT"
                and leg_visits.get(leg.id)
            ]
            if len(in_legs) != 1 or len(out_legs) != 1:
                raise ValueError(
                    f"任务 {task_id} 必须恰好使用一对中转腿"
                )
            in_leg = in_legs[0]
            out_leg = out_legs[0]
            if in_leg.relay_id != out_leg.relay_id:
                raise ValueError(
                    f"任务 {task_id} 的入库腿与出库腿中继站不一致"
                )
            in_visits = leg_visits[in_leg.id]
            out_visits = leg_visits[out_leg.id]
            if (
                sorted(in_visits) != [-in_leg.id, in_leg.id]
                or sorted(out_visits) != [-out_leg.id, out_leg.id]
            ):
                raise ValueError(f"任务 {task_id} 的中转腿不完整")
            if len(leg_routes.get(in_leg.id, set())) != 1 or len(
                leg_routes.get(out_leg.id, set())
            ) != 1:
                raise ValueError(f"任务 {task_id} 的中转腿被拆分到不同无人机")
            if leg_positions[in_leg.id][in_visits.index(in_leg.id)] > (
                leg_positions[in_leg.id][in_visits.index(-in_leg.id)]
            ):
                raise ValueError(f"任务 {task_id} 的入库腿顺序非法")
            if leg_positions[out_leg.id][out_visits.index(out_leg.id)] > (
                leg_positions[out_leg.id][out_visits.index(-out_leg.id)]
            ):
                raise ValueError(f"任务 {task_id} 的出库腿顺序非法")
            plans[task_id] = TaskPlan(
                task_id, "RELAY", in_leg.relay_id, (in_leg.id, out_leg.id)
            )
            inbound_route[task_id] = next(iter(leg_routes[in_leg.id]))
            outbound_route[task_id] = next(iter(leg_routes[out_leg.id]))
            inbound_position[task_id] = leg_positions[in_leg.id][
                in_visits.index(-in_leg.id)
            ]
            outbound_position[task_id] = leg_positions[out_leg.id][
                out_visits.index(out_leg.id)
            ]
            delivery_route[task_id] = next(iter(leg_routes[out_leg.id]))
            relay_ids.append(task_id)
        else:
            raise ValueError(f"任务 {task_id} 没有任何取送访问")

    return PlanIndex(
        task_plans=MappingProxyType(plans),
        task_visits=MappingProxyType(
            {task_id: tuple(visits) for task_id, visits in task_visits.items()}
        ),
        task_routes=MappingProxyType(
            {task_id: tuple(routes_for_task) for task_id, routes_for_task in task_routes.items()}
        ),
        direct_task_ids=tuple(direct_ids),
        relay_task_ids=tuple(relay_ids),
        inbound_route_by_task=MappingProxyType(inbound_route),
        outbound_route_by_task=MappingProxyType(outbound_route),
        inbound_position_by_task=MappingProxyType(inbound_position),
        outbound_position_by_task=MappingProxyType(outbound_position),
        delivery_route_by_task=MappingProxyType(delivery_route),
    )


@dataclass(frozen=True, slots=True)
class GlobalSchedule:
    """Full scheduled evaluation of one candidate solution."""

    feasible: bool
    score: Score
    delivery_times_min: Mapping[int, float]
    event_times_min: Mapping[tuple[int, int], float]
    total_waiting_min: float
    max_waiting_min: float
    waiting_by_task: Mapping[int, float]
    violations: tuple[str, ...]


class GlobalRelayEvaluator:
    """Cross-UAV handoff scheduler with cycle detection and a bounded cache."""

    def __init__(
        self,
        problem: Problem,
        cache_size: int = 20_000,
        counters: SearchCounters | None = None,
    ) -> None:
        self.problem = problem
        self.counters = counters if counters is not None else SearchCounters()

        @lru_cache(maxsize=cache_size)
        def cached(routes: Routes) -> GlobalSchedule:
            return self._evaluate_uncached(routes)

        self._cached = cached

    def evaluate(self, routes: Iterable[Sequence[int]]) -> GlobalSchedule:
        return self._cached(tuple(tuple(route) for route in routes))

    def _evaluate_uncached(self, routes: Routes) -> GlobalSchedule:
        self.counters.relay_global_evaluation_count += 1
        problem = self.problem
        registry = problem.leg_registry
        violations: list[str] = []

        event_index: dict[tuple[int, int], int] = {}
        event_count = 0
        for route_index, route in enumerate(routes):
            for position in range(len(route)):
                event_index[(route_index, position)] = event_count
                event_count += 1
        source = event_count
        node_count = event_count + 1

        edges: list[list[tuple[int, float, bool]]] = [
            [] for _ in range(node_count)
        ]
        in_degree = [0] * node_count
        distance = 0.0

        for route_index, route in enumerate(routes):
            previous: int | None = None
            for position, visit in enumerate(route):
                current = event_index[(route_index, position)]
                predecessor = (
                    source
                    if position == 0
                    else event_index[(route_index, position - 1)]
                )
                leg_km = problem.distance(previous, visit)
                distance += leg_km
                edges[predecessor].append(
                    (current, leg_km / problem.speed_km_per_min, False)
                )
                in_degree[current] += 1
                previous = visit

        drop_event: dict[int, int] = {}
        pickup_event: dict[int, int] = {}
        if registry:
            for route_index, route in enumerate(routes):
                for position, visit in enumerate(route):
                    leg = registry.get(abs(visit))
                    if leg is None:
                        continue
                    event_id = event_index[(route_index, position)]
                    if visit < 0 and leg.kind == "RELAY_IN":
                        drop_event[leg.task_id] = event_id
                    elif visit > 0 and leg.kind == "RELAY_OUT":
                        pickup_event[leg.task_id] = event_id
            # Partial candidates during repair may contain only one side of
            # a handoff; completeness is validated elsewhere.  Only handoff
            # edges for tasks with both sides present participate in timing.
            for task_id in sorted(set(drop_event) & set(pickup_event)):
                start = drop_event[task_id]
                end = pickup_event[task_id]
                edges[start].append((end, 0.0, True))
                in_degree[end] += 1

        queue = deque(index for index in range(node_count) if in_degree[index] == 0)
        order: list[int] = []
        while queue:
            node = queue.popleft()
            order.append(node)
            for target, _, _ in edges[node]:
                in_degree[target] -= 1
                if in_degree[target] == 0:
                    queue.append(target)
        if len(order) != node_count:
            violations.append("交接优先图存在环（死锁），候选解不可行")

        earliest = [0.0] * node_count
        no_wait = [0.0] * node_count
        for node in order:
            base = earliest[node]
            travel_base = no_wait[node]
            for target, weight, is_handoff in edges[node]:
                candidate = base + weight
                if earliest[target] < candidate:
                    earliest[target] = candidate
                if not is_handoff and no_wait[target] < travel_base + weight:
                    no_wait[target] = travel_base + weight

        delivery_times: dict[int, float] = {}
        late_ids: list[int] = []
        total_lateness = 0.0
        max_lateness = 0.0
        waiting_by_task: dict[int, float] = {}
        for route_index, route in enumerate(routes):
            for position, visit in enumerate(route):
                leg = registry.get(abs(visit)) if registry else None
                if visit < 0:
                    if leg is not None:
                        if leg.kind != "RELAY_OUT":
                            continue
                        task_id = leg.task_id
                    else:
                        task_id = abs(visit)
                    event_id = event_index[(route_index, position)]
                    delivered_at = earliest[event_id]
                    delivery_times[task_id] = delivered_at
                    lateness = max(
                        0.0,
                        delivered_at - problem.task(task_id).deadline_min,
                    )
                    if lateness > 1e-9:
                        late_ids.append(task_id)
                        total_lateness += lateness
                        max_lateness = max(max_lateness, lateness)
        for task_id in set(drop_event) & set(pickup_event):
            pickup_id = pickup_event[task_id]
            waiting = max(0.0, earliest[pickup_id] - no_wait[pickup_id])
            if waiting > 1e-12:
                waiting_by_task[task_id] = waiting

        event_times: dict[tuple[int, int], float] = {}
        for route_index, route in enumerate(routes):
            for position in range(len(route)):
                event_times[(route_index, position)] = earliest[
                    event_index[(route_index, position)]
                ]

        total_waiting = sum(waiting_by_task.values())
        return GlobalSchedule(
            feasible=not violations,
            score=Score(len(late_ids), total_lateness, distance),
            delivery_times_min=MappingProxyType(delivery_times),
            event_times_min=MappingProxyType(event_times),
            total_waiting_min=total_waiting,
            max_waiting_min=max(waiting_by_task.values(), default=0.0),
            waiting_by_task=MappingProxyType(waiting_by_task),
            violations=tuple(violations),
        )


def relay_statistics(
    problem: Problem,
    routes: Sequence[Sequence[int]],
    schedule: GlobalSchedule,
    plan_index: PlanIndex,
) -> dict[str, object]:
    """Human-reportable relay metrics for JSON and console output."""

    relay_ids = plan_index.relay_task_ids
    direct_count = len(plan_index.direct_task_ids)
    relay_count = len(relay_ids)
    total_tasks = direct_count + relay_count
    cross_count = 0
    same_count = 0
    for task_id in relay_ids:
        if plan_index.inbound_route_by_task[task_id] != (
            plan_index.outbound_route_by_task[task_id]
        ):
            cross_count += 1
        else:
            same_count += 1
    total_waiting = schedule.total_waiting_min
    average_waiting = (
        total_waiting / relay_count if relay_count else 0.0
    )
    usage_per_station = []
    for station in problem.relay_stations:
        station_tasks = [
            task_id
            for task_id in relay_ids
            if plan_index.task_plans[task_id].relay_id == station.id
        ]
        waits = [
            schedule.waiting_by_task.get(task_id, 0.0)
            for task_id in station_tasks
        ]
        usage_per_station.append(
            {
                "station_id": station.id,
                "x": station.x,
                "y": station.y,
                "drop_count": len(station_tasks),
                "pickup_count": len(station_tasks),
                "task_count": len(station_tasks),
                "average_wait": sum(waits) / len(waits) if waits else 0.0,
            }
        )
    per_drone = []
    for route_index, route in enumerate(routes):
        route_distance = 0.0
        previous: int | None = None
        relay_drops = 0
        relay_pickups = 0
        delivered = set()
        for visit in route:
            route_distance += problem.distance(previous, visit)
            previous = visit
            leg = problem.leg_registry.get(abs(visit)) if problem.has_relays else None
            if leg is not None:
                if visit < 0 and leg.kind == "RELAY_IN":
                    relay_drops += 1
                elif visit > 0 and leg.kind == "RELAY_OUT":
                    relay_pickups += 1
                if visit < 0 and leg.kind == "RELAY_OUT":
                    delivered.add(leg.task_id)
            elif visit < 0:
                delivered.add(abs(visit))
        per_drone.append(
            {
                "drone_id": route_index + 1,
                "original_task_count": len(delivered),
                "transport_leg_count": len(route) // 2,
                "relay_drop_count": relay_drops,
                "relay_pickup_count": relay_pickups,
                "distance_km": route_distance,
            }
        )
    return {
        "relay_count": len(problem.relay_stations),
        "relay_coordinates": [
            [station.id, station.x, station.y]
            for station in problem.relay_stations
        ],
        "direct_task_count": direct_count,
        "relay_task_count": relay_count,
        "relay_share": relay_count / total_tasks if total_tasks else 0.0,
        "cross_uav_handoff_count": cross_count,
        "same_uav_relay_count": same_count,
        "total_relay_waiting_time": total_waiting,
        "average_relay_waiting_time": average_waiting,
        "max_relay_waiting_time": schedule.max_waiting_min,
        "relay_usage_per_station": usage_per_station,
        "per_drone": per_drone,
    }
