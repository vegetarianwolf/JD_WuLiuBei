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
from .deploy import k_means_points
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


class _EventTimesView(Mapping[tuple[int, int], float]):
    """Compact immutable ``(route, position) -> time`` mapping.

    Search candidates contain roughly two events per original task.  A dict
    with a freshly allocated tuple key per event dominated both allocation
    traffic and the retained size of the global evaluator's 20k-entry LRU.
    Route offsets plus one dense time tuple provide the same Mapping API with
    O(route_count + event_count) compact storage and O(1) lookup.
    """

    __slots__ = ("_offsets", "_lengths", "_times", "_size")

    def __init__(
        self,
        route_offsets: Sequence[int],
        route_lengths: Sequence[int],
        event_times: Sequence[float],
    ) -> None:
        self._offsets = tuple(route_offsets)
        self._lengths = tuple(route_lengths)
        self._times = tuple(event_times)
        self._size = sum(self._lengths)

    def __getitem__(self, key: tuple[int, int]) -> float:
        try:
            route_index, position = key
            valid = (
                isinstance(route_index, int)
                and isinstance(position, int)
                and route_index >= 0
                and route_index < len(self._offsets)
                and position >= 0
                and position < self._lengths[route_index]
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            raise KeyError(key)
        return self._times[self._offsets[route_index] + position]

    def __iter__(self):
        for route_index, length in enumerate(self._lengths):
            for position in range(length):
                yield (route_index, position)

    def __len__(self) -> int:
        return self._size


@dataclass(frozen=True, slots=True)
class RelayNetwork:
    """Everything the relay-aware problem needs, fixed before ALNS starts."""

    stations: tuple[RelayStation, ...]
    leg_registry: Mapping[int, TransportLeg]
    task_relay_candidates: Mapping[int, tuple[int, ...]]
    metadata: Mapping[str, object]


def _station_centers(
    sorted_tasks: Sequence[Task],
    count: int,
    location_method: str,
    location_seed: int,
) -> tuple[tuple[Point, ...], tuple[float, ...]]:
    """Place ``count`` station centres with the configured site method.

    Returns ``(centers, weights)``; ``weights`` is the weighted-k-medoids
    weight vector (empty for the k-means methods) so callers can keep the
    audit metadata.  Shared by :func:`build_relay_network` and
    :func:`compute_dynamic_station_count` so the automatic station-count
    search uses exactly the same placement as the real network.
    """

    midpoints = tuple(
        Point(
            (task.pickup.x + task.delivery.x) / 2.0,
            (task.pickup.y + task.delivery.y) / 2.0,
        )
        for task in sorted_tasks
    )
    weights: tuple[float, ...] = ()
    if location_method == "weighted_kmedoids":
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
        rng = Random(location_seed)
        centers = _weighted_k_medoids(midpoints, weights, count, rng)
    elif location_method == "kmeans_midpoint":
        centers = k_means_points(midpoints, count, seed=location_seed)
    else:  # "kmeans_pickup"
        centers = k_means_points(
            tuple(task.pickup for task in sorted_tasks),
            count,
            seed=location_seed,
        )
    return tuple(centers), weights


def compute_dynamic_station_count(
    tasks: Sequence[Task],
    drone_count: int,
    *,
    location_seed: int = 42,
    location_method: str = "weighted_kmedoids",
    min_stations: int = 1,
    max_stations: int | None = None,
    elbow_threshold: float = 0.05,
    coverage_target_km: float = 0.5,
) -> int:
    """Decide how many relay stations the task distribution warrants.

    A forward coverage search over the same station placement used by
    :func:`build_relay_network`:

    * if a single station already covers the midpoints within
      ``coverage_target_km`` on average, the tasks are concentrated and
      ``min_stations`` is returned (no over-building on tight clusters);
    * otherwise keep adding a station while the marginal relative gain in
      mean nearest-station distance is at least ``elbow_threshold`` of the
      one-station baseline, and stop once the gain flattens.

    Deterministic (the only randomness comes from the fixed placement seed).
    The returned count is capped by ``drone_count`` and by the number of
    distinct task midpoints.
    """

    if not isfinite(elbow_threshold) or elbow_threshold < 0:
        raise ValueError("elbow_threshold 必须是非负有限数")
    if not isfinite(coverage_target_km) or coverage_target_km <= 0:
        raise ValueError("coverage_target_km 必须为正数")
    if min_stations < 0:
        raise ValueError("min_stations 不能为负")
    if drone_count <= 0:
        raise ValueError("无人机数量必须为正整数")
    if location_method not in (
        "weighted_kmedoids",
        "kmeans_midpoint",
        "kmeans_pickup",
    ):
        raise ValueError(f"未知中继站选址方法 {location_method}")
    sorted_tasks = tuple(sorted(tasks, key=lambda task: task.id))
    if not sorted_tasks:
        return min_stations

    if max_stations is None:
        max_stations = max(min_stations, drone_count)
    max_stations = max(min_stations, int(max_stations))

    midpoints = tuple(
        Point(
            (task.pickup.x + task.delivery.x) / 2.0,
            (task.pickup.y + task.delivery.y) / 2.0,
        )
        for task in sorted_tasks
    )
    # k-means cannot place more centres than distinct points; the medoid
    # routine degrades gracefully but capping keeps the search meaningful.
    max_feasible = len({point for point in midpoints})
    if location_method == "kmeans_pickup":
        max_feasible = min(
            max_feasible, len({task.pickup for task in sorted_tasks})
        )
    max_stations = min(max_stations, max_feasible)
    if max_stations < min_stations:
        return min_stations

    def mean_nearest(count: int) -> float:
        centers, _ = _station_centers(
            sorted_tasks, count, location_method, location_seed
        )
        total = 0.0
        for midpoint in midpoints:
            total += min(
                midpoint.distance_to(center) for center in centers
            )
        return total / len(midpoints)

    baseline = mean_nearest(1)
    if baseline <= coverage_target_km:
        # Already covered by a single station: tasks are concentrated.
        return min_stations
    chosen = min_stations
    previous = mean_nearest(min_stations)
    if previous <= coverage_target_km:
        return min_stations
    for count in range(min_stations + 1, max_stations + 1):
        current = mean_nearest(count)
        if current <= coverage_target_km:
            chosen = count
            break
        improvement = (previous - current) / baseline
        if improvement < elbow_threshold:
            break
        chosen = count
        previous = current
    return chosen


def build_relay_network(
    tasks: Sequence[Task],
    *,
    relay_count: int,
    candidates_per_task: int = 2,
    detour_ratio: float = 1.5,
    location_seed: int = 42,
    fallback: bool = True,
    location_method: str = "weighted_kmedoids",
) -> RelayNetwork:
    """Place fixed relay stations with a configurable site-location method.

    The relay sites are the same points the fleet is pre-deployed to (drone
    homes), so deployment and relay share one set of points.  Supported
    ``location_method`` values:

    * ``"weighted_kmedoids"`` (default, legacy): weighted k-medoids on task
      midpoints, weight ``1 + normalized_distance + deadline_urgency``, so
      sites sit on the pickup->delivery corridors;
    * ``"kmeans_midpoint"``: k-means cluster means of the task midpoints
      (arbitrary coordinates, still corridor-aware);
    * ``"kmeans_pickup"``: k-means cluster means of the task pickups
      (deployment-friendly, most aggressive for relay).

    Each task then keeps its top ``candidates_per_task`` stations within
    ``detour_ratio``.  Tasks with no station inside the corridor fall back to
    their nearest station when ``fallback`` is set, so every task keeps at
    least one relay candidate.
    """

    if location_method not in (
        "weighted_kmedoids",
        "kmeans_midpoint",
        "kmeans_pickup",
    ):
        raise ValueError(f"未知中继站选址方法 {location_method}")
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
                    "location_method": location_method,
                    "fallback": fallback,
                }
            ),
        )

    centers, weights = _station_centers(
        sorted_tasks, relay_count, location_method, location_seed
    )
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
        chosen = within[:candidates_per_task]
        if not chosen and fallback:
            # Fallback to the nearest station when the corridor is empty,
            # so every task keeps at least one relay candidate.
            chosen = detours[:1]
        candidates[task.id] = tuple(relay_id for _, relay_id in chosen)

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
                "fallback": fallback,
                "stations": [
                    [station.id, station.x, station.y] for station in stations
                ],
                "location_method": location_method,
                "weights": (
                    [weight for weight in weights]
                    if location_method == "weighted_kmedoids"
                    else []
                ),
            }
        ),
    )


def relay_candidate_coverage(
    task_relay_candidates: Mapping[int, tuple[int, ...]],
) -> float:
    """Fraction of tasks that keep at least one relay candidate station.

    ``1.0`` means every task can be served through relay; lower values mean
    part of the instance is direct-only under the current network geometry.
    """

    if not task_relay_candidates:
        return 0.0
    covered = sum(
        1 for candidates in task_relay_candidates.values() if candidates
    )
    return covered / len(task_relay_candidates)


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
        # Decode event semantics once per solve instead of consulting the
        # leg registry (and branching on leg.kind) twice for every event of
        # every candidate schedule.  1=relay drop, 2=relay pickup,
        # 3=final delivery.
        event_semantics = {
            -task.id: (3, task.id) for task in problem.tasks
        }
        for leg in problem.leg_registry.values():
            if leg.kind == "RELAY_IN":
                event_semantics[-leg.id] = (1, leg.task_id)
            else:
                event_semantics[leg.id] = (2, leg.task_id)
                event_semantics[-leg.id] = (3, leg.task_id)
        self._event_semantics = event_semantics
        self._deadline_by_task = {
            task.id: task.deadline_min for task in problem.tasks
        }
        # Per-route (travel_time, ...) decode cache.  Only one or two routes
        # change per ALNS iteration, so identical route snapshots repeat
        # heavily; caching the hot distance/semantics decode amortises it
        # across the ~50 global evaluations each iteration performs.
        self._route_layout_cache: dict[
            tuple[Route, int], tuple[float, ...]
        ] = {}

        @lru_cache(maxsize=cache_size)
        def cached(routes: Routes) -> GlobalSchedule:
            return self._evaluate_uncached(routes)

        self._cached = cached

    def evaluate(self, routes: Iterable[Sequence[int]]) -> GlobalSchedule:
        return self._cached(tuple(tuple(route) for route in routes))

    def _route_layout(
        self, route: Route, home_node: int
    ) -> tuple[float, ...]:
        """Cached per-route event leg distances in km."""
        key = (route, home_node)
        cached = self._route_layout_cache.get(key)
        if cached is not None:
            return cached
        problem = self.problem
        node_by_visit = problem._visit_node_map
        distances = problem._distances
        leg_km: list[float] = []
        previous_node = home_node
        for visit in route:
            current_node = node_by_visit[visit]
            leg_km.append(distances[previous_node][current_node])
            previous_node = current_node
        cached = tuple(leg_km)
        if len(self._route_layout_cache) >= 20_000:
            self._route_layout_cache.clear()
        self._route_layout_cache[key] = cached
        return cached

    def _build_event_layout(
        self, routes: Routes
    ) -> tuple[
        list[int],
        tuple[int, ...],
        int,
        int,
        int,
        list[float],
        list[int],
        list[int],
        list[int],
        list[int],
        dict[int, int],
        dict[int, int],
        float,
        list[int],
        list[int],
        list[int],
        list[int],
    ]:
        """Decode one candidate's precedence DAG into flat event arrays.

        Returns ``(route_offsets, route_lengths, event_count, source,
        node_count, travel_time, delivery_task, route_predecessor,
        route_successor, route_heads, drop_event, pickup_event, distance,
        in_degree, handoff_successor, handoff_pred, handoff_tasks)``.
        """
        problem = self.problem
        route_offsets: list[int] = []
        event_count = 0
        for route in routes:
            route_offsets.append(event_count)
            event_count += len(route)
        source = event_count
        node_count = event_count + 1

        travel_time = [0.0] * event_count
        delivery_task = [0] * event_count
        route_predecessor = [source] * event_count
        route_successor = [-1] * event_count
        route_heads: list[int] = []
        drop_event: dict[int, int] = {}
        pickup_event: dict[int, int] = {}
        distance = 0.0
        home_nodes = problem.drone_home_nodes
        speed = problem.speed_km_per_min
        event_semantics = self._event_semantics

        for route_index, route in enumerate(routes):
            offset = route_offsets[route_index]
            leg_km = self._route_layout(route, home_nodes[route_index])
            previous_event = source
            for position, km in enumerate(leg_km):
                current = offset + position
                route_predecessor[current] = previous_event
                if previous_event == source:
                    route_heads.append(current)
                else:
                    route_successor[previous_event] = current
                travel_time[current] = km / speed
                distance += km
                semantics = event_semantics.get(route[position])
                if semantics is not None:
                    kind, task_id = semantics
                    if kind == 1:
                        drop_event[task_id] = current
                    elif kind == 2:
                        pickup_event[task_id] = current
                    else:
                        delivery_task[current] = task_id
                previous_event = current

        # Every event has exactly one route-precedence edge.  A relay pickup
        # has one additional handoff edge; a relay drop has at most one
        # handoff successor.  Representing those two edge kinds separately
        # removes hundreds of short tuple/list allocations per evaluation.
        in_degree = [1] * event_count + [0]
        handoff_successor = [-1] * event_count
        handoff_pred = [-1] * event_count
        handoff_tasks = sorted(set(drop_event) & set(pickup_event))
        for task_id in handoff_tasks:
            start = drop_event[task_id]
            end = pickup_event[task_id]
            handoff_successor[start] = end
            handoff_pred[end] = start
            in_degree[end] += 1

        return (
            route_offsets,
            tuple(len(route) for route in routes),
            event_count,
            source,
            node_count,
            travel_time,
            delivery_task,
            route_predecessor,
            route_successor,
            route_heads,
            drop_event,
            pickup_event,
            distance,
            in_degree,
            handoff_successor,
            handoff_pred,
            handoff_tasks,
        )

    def _evaluate_uncached(self, routes: Routes) -> GlobalSchedule:
        self.counters.relay_global_evaluation_count += 1
        problem = self.problem
        violations: list[str] = []

        (
            route_offsets,
            route_lengths,
            event_count,
            source,
            node_count,
            travel_time,
            delivery_task,
            route_predecessor,
            route_successor,
            route_heads,
            drop_event,
            pickup_event,
            distance,
            in_degree,
            handoff_successor,
            _handoff_pred,
            handoff_tasks,
        ) = self._build_event_layout(routes)

        queue = deque((source,))
        processed = 0
        earliest = [0.0] * node_count
        while queue:
            node = queue.popleft()
            processed += 1
            base = earliest[node]
            if node == source:
                for target in route_heads:
                    candidate = travel_time[target]
                    if earliest[target] < candidate:
                        earliest[target] = candidate
                    in_degree[target] -= 1
                    if in_degree[target] == 0:
                        queue.append(target)
                continue

            target = route_successor[node]
            if target >= 0:
                candidate = base + travel_time[target]
                if earliest[target] < candidate:
                    earliest[target] = candidate
                in_degree[target] -= 1
                if in_degree[target] == 0:
                    queue.append(target)
            target = handoff_successor[node]
            if target >= 0:
                if earliest[target] < base:
                    earliest[target] = base
                in_degree[target] -= 1
                if in_degree[target] == 0:
                    queue.append(target)
        if processed != node_count:
            violations.append("交接优先图存在环（死锁），候选解不可行")

        delivery_times: dict[int, float] = {}
        late_count = 0
        total_lateness = 0.0
        waiting_by_task: dict[int, float] = {}
        deadline_by_task = self._deadline_by_task
        for event_id, task_id in enumerate(delivery_task):
            if task_id == 0:
                continue
            delivered_at = earliest[event_id]
            delivery_times[task_id] = delivered_at
            lateness = max(
                0.0,
                delivered_at - deadline_by_task[task_id],
            )
            if lateness > 1e-9:
                late_count += 1
                total_lateness += lateness
        for task_id in handoff_tasks:
            pickup_id = pickup_event[task_id]
            predecessor = route_predecessor[pickup_id]
            route_arrival = (
                (0.0 if predecessor == source else earliest[predecessor])
                + travel_time[pickup_id]
            )
            # Attribute only the wait introduced at this handoff.  Comparing
            # with a no-wait clock incorrectly charged all earlier waits on
            # the same route to every later relay pickup.
            waiting = max(0.0, earliest[pickup_id] - route_arrival)
            if waiting > 1e-12:
                waiting_by_task[task_id] = waiting

        event_times = _EventTimesView(
            route_offsets,
            route_lengths,
            earliest[:event_count],
        )

        total_waiting = sum(waiting_by_task.values())
        return GlobalSchedule(
            feasible=not violations,
            score=Score(late_count, total_lateness, distance),
            delivery_times_min=MappingProxyType(delivery_times),
            event_times_min=event_times,
            total_waiting_min=total_waiting,
            max_waiting_min=max(waiting_by_task.values(), default=0.0),
            waiting_by_task=MappingProxyType(waiting_by_task),
            violations=tuple(violations),
        )

    def evaluate_delta(
        self,
        base_routes: Routes,
        base_schedule: GlobalSchedule,
        candidate_routes: Routes,
    ) -> GlobalSchedule:
        """Evaluate a candidate that differs from ``base_routes`` in at most
        two routes, reusing the base schedule's event times for everything
        untouched by the change.

        Changed-route events plus every event reachable from them through
        route-succession or handoff edges are re-propagated with a dirty
        longest-path relaxation; unaffected events keep their base values
        bit-for-bit.  Falls back to the full evaluation when more than two
        routes change (the affected subgraph then covers most events) or
        when a cycle is detected.
        """
        if candidate_routes == base_routes:
            return base_schedule
        changed = [
            i
            for i in range(max(len(base_routes), len(candidate_routes)))
            if (base_routes[i] if i < len(base_routes) else ())
            != (candidate_routes[i] if i < len(candidate_routes) else ())
        ]
        if len(changed) > 2:
            return self.evaluate(candidate_routes)

        self.counters.relay_global_evaluation_count += 1
        problem = self.problem
        (
            route_offsets,
            route_lengths,
            event_count,
            source,
            node_count,
            travel_time,
            delivery_task,
            route_predecessor,
            route_successor,
            _route_heads,
            drop_event,
            pickup_event,
            distance,
            _in_degree,
            handoff_successor,
            handoff_pred,
            handoff_tasks,
        ) = self._build_event_layout(candidate_routes)

        changed_set = set(changed)
        # Seed unaffected events bit-for-bit from the base schedule.  The
        # base event-times view stores one dense flat time array indexed by
        # the base route offsets (a changed route's length can shift the
        # offsets of later routes, so index with the base view's own
        # offsets), which beats a per-event tuple-keyed mapping lookup.
        base_view = base_schedule.event_times_min
        base_times = base_view._times
        base_offsets = base_view._offsets
        earliest = [0.0] * node_count
        for route_index, route in enumerate(candidate_routes):
            if route_index in changed_set:
                continue
            offset = route_offsets[route_index]
            base_offset = base_offsets[route_index]
            for position in range(len(route)):
                earliest[offset + position] = base_times[
                    base_offset + position
                ]

        # Compute the affected closure first: every event reachable from the
        # changed routes through route-succession or handoff edges.  Only
        # those events are re-propagated; everything else keeps its base
        # value.  A cycle can only live inside the closure (the base was
        # feasible), so a node count mismatch after the restricted Kahn pass
        # means the candidate deadlocks.
        affected = [False] * node_count
        stack: list[int] = []
        for route_index in changed_set:
            offset = route_offsets[route_index]
            for position in range(route_lengths[route_index]):
                node = offset + position
                if not affected[node]:
                    affected[node] = True
                    stack.append(node)
        while stack:
            node = stack.pop()
            target = route_successor[node]
            if target >= 0 and not affected[target]:
                affected[target] = True
                stack.append(target)
            target = handoff_successor[node]
            if target >= 0 and not affected[target]:
                affected[target] = True
                stack.append(target)

        # Seed the incoming edges from outside the closure (the source start
        # and handoff predecessors on untouched routes).
        for route_index in changed_set:
            offset = route_offsets[route_index]
            for position in range(route_lengths[route_index]):
                node = offset + position
                pred = route_predecessor[node]
                if pred == source:
                    if earliest[node] < travel_time[node]:
                        earliest[node] = travel_time[node]
                hpred = handoff_pred[node]
                if hpred >= 0 and not affected[hpred]:
                    if earliest[node] < earliest[hpred]:
                        earliest[node] = earliest[hpred]

        affected_in_degree = [0] * node_count
        total_affected = 0
        queue: deque[int] = deque()
        for node in range(event_count):
            if not affected[node]:
                continue
            total_affected += 1
            pred = route_predecessor[node]
            if pred != source and affected[pred]:
                affected_in_degree[node] += 1
            hpred = handoff_pred[node]
            if hpred >= 0 and affected[hpred]:
                affected_in_degree[node] += 1
            if affected_in_degree[node] == 0:
                queue.append(node)

        processed_affected = 0
        while queue:
            node = queue.popleft()
            processed_affected += 1
            base = earliest[node]
            target = route_successor[node]
            if target >= 0:
                candidate = base + travel_time[target]
                if earliest[target] < candidate:
                    earliest[target] = candidate
                if affected[target]:
                    affected_in_degree[target] -= 1
                    if affected_in_degree[target] == 0:
                        queue.append(target)
            target = handoff_successor[node]
            if target >= 0:
                if earliest[target] < base:
                    earliest[target] = base
                if affected[target]:
                    affected_in_degree[target] -= 1
                    if affected_in_degree[target] == 0:
                        queue.append(target)
        if processed_affected != total_affected:
            # Cycle inside the affected subgraph: the partial earliest values
            # are not a valid schedule, so recompute with the full
            # cycle-detecting evaluator.
            return self.evaluate(candidate_routes)

        delivery_times: dict[int, float] = {}
        late_count = 0
        total_lateness = 0.0
        waiting_by_task: dict[int, float] = {}
        deadline_by_task = self._deadline_by_task
        for event_id, task_id in enumerate(delivery_task):
            if task_id == 0:
                continue
            delivered_at = earliest[event_id]
            delivery_times[task_id] = delivered_at
            lateness = max(
                0.0,
                delivered_at - deadline_by_task[task_id],
            )
            if lateness > 1e-9:
                late_count += 1
                total_lateness += lateness
        for task_id in handoff_tasks:
            pickup_id = pickup_event[task_id]
            predecessor = route_predecessor[pickup_id]
            route_arrival = (
                (0.0 if predecessor == source else earliest[predecessor])
                + travel_time[pickup_id]
            )
            waiting = max(0.0, earliest[pickup_id] - route_arrival)
            if waiting > 1e-12:
                waiting_by_task[task_id] = waiting

        event_times = _EventTimesView(
            route_offsets,
            route_lengths,
            earliest[:event_count],
        )

        total_waiting = sum(waiting_by_task.values())
        return GlobalSchedule(
            feasible=True,
            score=Score(late_count, total_lateness, distance),
            delivery_times_min=MappingProxyType(delivery_times),
            event_times_min=event_times,
            total_waiting_min=total_waiting,
            max_waiting_min=max(waiting_by_task.values(), default=0.0),
            waiting_by_task=MappingProxyType(waiting_by_task),
            violations=(),
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
        "relay_candidate_coverage": relay_candidate_coverage(
            problem.task_relay_candidates
        ),
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
