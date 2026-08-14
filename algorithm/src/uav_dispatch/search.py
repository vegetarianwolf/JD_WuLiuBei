"""Reusable route scoring and capacity-two pair insertion primitives."""

from __future__ import annotations

import os
from bisect import bisect_left
from dataclasses import dataclass, field
from functools import lru_cache
from heapq import nsmallest
from types import MappingProxyType
from typing import Mapping, Sequence

from .counters import SearchCounters
from .model import Problem, Score, SolutionEvaluation
from .validation import evaluate_solution


Route = tuple[int, ...]
Routes = tuple[Route, ...]

# Hidden escape hatch for A/B diagnostics: re-enable the pre-optimization
# full-route candidate evaluation (materializes and scores every candidate)
# by setting UAV_DISPATCH_LEGACY_CANDIDATE_EVAL=1.
_LEGACY_CANDIDATE_EVAL = os.environ.get("UAV_DISPATCH_LEGACY_CANDIDATE_EVAL") == "1"


@dataclass(frozen=True, slots=True)
class RouteMetrics:
    score: Score
    delivery_times_min: Mapping[int, float]
    late_task_ids: tuple[int, ...]
    max_lateness_min: float


@dataclass(frozen=True, slots=True)
class SolverResult:
    routes: Routes
    evaluation: SolutionEvaluation
    runtime_seconds: float = 0.0
    iterations: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict, compare=False)


@dataclass(frozen=True, slots=True)
class InsertionResult:
    routes: Routes
    evaluation: SolutionEvaluation
    route_index: int
    pickup_position: int
    delivery_position: int


class RouteEvaluator:
    """Exact route scorer with a bounded cache for the search loop."""

    def __init__(
        self,
        problem: Problem,
        cache_size: int = 50_000,
        counters: SearchCounters | None = None,
    ) -> None:
        self.problem = problem
        self.counters = counters if counters is not None else SearchCounters()
        self._has_legs = bool(problem.leg_registry)

        @lru_cache(maxsize=cache_size)
        def cached(route: Route) -> RouteMetrics:
            return self._evaluate_uncached(route)

        self._cached = cached

    def evaluate(self, route: Sequence[int]) -> RouteMetrics:
        return self._cached(tuple(route))

    def _evaluate_uncached(self, route: Route) -> RouteMetrics:
        self.counters.route_full_evaluation_count += 1
        problem = self.problem
        registry = problem.leg_registry
        capacity = problem.capacity
        speed = problem.speed_km_per_min
        distance_fn = problem.distance
        deadline_fn = problem.task
        seen_pickups: set[int] = set()
        seen_deliveries: set[int] = set()
        seen_leg_starts: set[int] = set()
        seen_leg_ends: set[int] = set()
        previous: int | None = None
        distance = 0.0
        elapsed = 0.0
        load = 0
        delivery_times: dict[int, float] = {}
        late_ids: list[int] = []
        total_lateness = 0.0
        max_lateness = 0.0

        for visit in route:
            visit_id = abs(visit)
            leg = registry.get(visit_id) if self._has_legs else None
            if leg is None and visit_id not in problem._task_by_id:
                raise ValueError(f"未知任务 {visit_id}")
            leg_km = distance_fn(previous, visit)
            distance += leg_km
            elapsed += leg_km / speed
            previous = visit
            if leg is not None:
                if visit > 0:
                    if visit_id in seen_leg_starts:
                        raise ValueError(f"中转腿 {visit_id} 重复起点")
                    seen_leg_starts.add(visit_id)
                    load += 1
                    if load > capacity:
                        raise ValueError("路线超过载荷上限")
                else:
                    if (
                        visit_id not in seen_leg_starts
                        or visit_id in seen_leg_ends
                    ):
                        raise ValueError(
                            f"中转腿 {visit_id} 的交接顺序非法"
                        )
                    seen_leg_ends.add(visit_id)
                    load -= 1
                    if load < 0:
                        raise ValueError("路线载荷变为负数")
                    if leg.kind == "RELAY_OUT":
                        task_id = leg.task_id
                        delivery_times[task_id] = elapsed
                        lateness = max(
                            0.0,
                            elapsed - deadline_fn(task_id).deadline_min,
                        )
                        if lateness > 1e-9:
                            late_ids.append(task_id)
                            total_lateness += lateness
                            max_lateness = max(max_lateness, lateness)
                continue
            # Original-task DIRECT semantics, identical to the legacy scorer.
            task_id = visit_id
            if visit > 0:
                if task_id in seen_pickups:
                    raise ValueError(f"任务 {task_id} 重复取件")
                seen_pickups.add(task_id)
                load += 1
                if load > capacity:
                    raise ValueError("路线超过载荷上限")
            else:
                if task_id not in seen_pickups or task_id in seen_deliveries:
                    raise ValueError(f"任务 {task_id} 的取送顺序非法")
                seen_deliveries.add(task_id)
                load -= 1
                delivery_times[task_id] = elapsed
                lateness = max(
                    0.0, elapsed - deadline_fn(task_id).deadline_min
                )
                if lateness > 1e-9:
                    late_ids.append(task_id)
                    total_lateness += lateness
                    max_lateness = max(max_lateness, lateness)
        if (
            load != 0
            or seen_pickups != seen_deliveries
            or seen_leg_starts != seen_leg_ends
        ):
            raise ValueError("路线必须包含完整的取送任务对")
        return RouteMetrics(
            score=Score(len(late_ids), total_lateness, distance),
            delivery_times_min=MappingProxyType(delivery_times),
            late_task_ids=tuple(late_ids),
            max_lateness_min=max_lateness,
        )


def routes_score(evaluator: RouteEvaluator, routes: Sequence[Sequence[int]]) -> Score:
    score = Score(0, 0.0, 0.0)
    for route in routes:
        score += evaluator.evaluate(route).score
    return score


def _delivery_event_count(problem: Problem, route: Sequence[int]) -> int:
    """Number of tasks whose final delivery happens on this route (K=25)."""

    if not problem.leg_registry:
        return sum(visit < 0 for visit in route)
    count = 0
    for visit in route:
        if visit < 0:
            leg = problem.leg_registry.get(-visit)
            if leg is None or leg.kind == "RELAY_OUT":
                count += 1
    return count


def _load_before(route: Sequence[int]) -> tuple[int, ...]:
    loads = [0]
    load = 0
    for visit in route:
        load += 1 if visit > 0 else -1
        loads.append(load)
    return tuple(loads)


@dataclass(frozen=True, slots=True)
class RouteProfile:
    """Capacity and distance summaries for fixed-position pair insertions."""

    route: Route
    loads_before: tuple[int, ...]
    range_max: tuple[tuple[int, ...], ...]

    @classmethod
    def build(cls, route: Sequence[int]) -> "RouteProfile":
        materialized = tuple(route)
        loads = _load_before(materialized)
        size = len(loads)
        rows: list[tuple[int, ...]] = []
        for start in range(size):
            values = [-1] * (size + 1)
            current = -1
            for end in range(start + 1, size + 1):
                current = max(current, loads[end - 1])
                values[end] = current
            rows.append(tuple(values))
        return cls(materialized, loads, tuple(rows))

    def can_insert(
        self, pickup_position: int, delivery_position: int, capacity: int
    ) -> bool:
        return self.range_max[pickup_position][delivery_position] < capacity

    def distance_delta(
        self,
        problem: Problem,
        task_id: int,
        pickup_position: int,
        delivery_position: int,
        *,
        start_visit: int | None = None,
        end_visit: int | None = None,
    ) -> float:
        start = task_id if start_visit is None else start_visit
        end = -task_id if end_visit is None else end_visit
        route = self.route
        length = len(route)

        def edge(left: int | None, right: int | None) -> float:
            return 0.0 if right is None else problem.distance(left, right)

        before_pickup = None if pickup_position == 0 else route[pickup_position - 1]
        after_pickup = route[pickup_position] if pickup_position < length else None
        if delivery_position == pickup_position + 1:
            old = edge(before_pickup, after_pickup)
            new = (
                edge(before_pickup, start)
                + problem.distance(start, end)
                + edge(end, after_pickup)
            )
            return new - old

        before_delivery = route[delivery_position - 2]
        after_delivery_index = delivery_position - 1
        after_delivery = (
            route[after_delivery_index]
            if after_delivery_index < length
            else None
        )
        old = edge(before_pickup, after_pickup) + edge(
            before_delivery, after_delivery
        )
        new = (
            edge(before_pickup, start)
            + edge(start, after_pickup)
            + edge(before_delivery, end)
            + edge(end, after_delivery)
        )
        return new - old


def _can_insert_pair(
    loads_before: Sequence[int], pickup_position: int, delivery_position: int, capacity: int
) -> bool:
    # Reference implementation.  The ALNS uses RouteProfile.range_max for the
    # corresponding O(1) query; this direct slice remains useful for auditing.
    return max(loads_before[pickup_position:delivery_position]) < capacity


def feasible_pair_positions(
    problem: Problem, route: Sequence[int]
) -> tuple[tuple[int, int], ...]:
    """Enumerate every precedence- and capacity-feasible pair position."""

    length = len(route)
    profile = RouteProfile.build(route)
    positions: list[tuple[int, int]] = []
    for pickup_position in range(length + 1):
        for delivery_position in range(pickup_position + 1, length + 2):
            if profile.can_insert(
                pickup_position, delivery_position, problem.capacity
            ):
                positions.append((pickup_position, delivery_position))
    return tuple(positions)


@dataclass(frozen=True, slots=True)
class RouteInsertionOption:
    delta: Score
    route_index: int
    pickup_position: int
    delivery_position: int
    route: Route
    start_time_min: float | None = None
    end_time_min: float | None = None


def route_insertion_options(
    problem: Problem,
    evaluator: RouteEvaluator,
    route: Sequence[int],
    route_index: int,
    task_id: int,
    *,
    candidate_limit: int | None,
    option_count: int = 3,
    surface: RouteInsertionSurface | None = None,
) -> tuple[RouteInsertionOption, ...]:
    """Return the best exact deltas after capacity-profile candidate pruning.

    Candidate ranking is fully incremental: every position gets a lightweight
    insertion delta from the prebuilt ``surface`` and only the top
    ``option_count`` survivors materialize a real route tuple.  No candidate
    route is ever passed through :meth:`RouteEvaluator.evaluate`.
    """

    if surface is None:
        surface = route_insertion_surface(problem, route)
    materialized = surface.route
    if (
        _delivery_event_count(problem, materialized)
        >= problem.max_tasks_per_drone
    ):
        return ()
    positions = list(surface.positions)
    # Distance deltas are computed once per position and reused both by the
    # candidate pruning and by the incremental score deltas below.
    distances = problem._distances
    visit_nodes = surface.visit_nodes
    start_node = problem._node_for_visit(task_id)
    end_node = problem._node_for_visit(-task_id)
    scored = [
        (
            _node_distance_delta(
                distances,
                visit_nodes,
                start_node,
                end_node,
                pickup_position,
                delivery_position,
            ),
            pickup_position,
            delivery_position,
        )
        for pickup_position, delivery_position in positions
    ]
    if candidate_limit is not None and len(positions) > candidate_limit:
        limit = max(4, candidate_limit)
        by_distance = sorted(
            scored, key=lambda item: (item[0], item[1], item[2])
        )
        by_early_delivery = sorted(
            scored, key=lambda item: (item[2], item[1], item[0])
        )
        selected: list[tuple[int, int]] = []
        selected_set: set[tuple[int, int]] = set()
        for collection, quota in (
            (by_distance, max(1, limit // 2)),
            (by_early_delivery, max(1, limit // 3)),
        ):
            added = 0
            for _, pickup_position, delivery_position in collection:
                position = (pickup_position, delivery_position)
                if position not in selected_set:
                    selected.append(position)
                    selected_set.add(position)
                    added += 1
                if added >= quota or len(selected) >= limit:
                    break
            if len(selected) >= limit:
                break
        for _, pickup_position, delivery_position in by_distance:
            if len(selected) >= limit:
                break
            position = (pickup_position, delivery_position)
            if position not in selected_set:
                selected.append(position)
                selected_set.add(position)
        positions = selected[:limit]
        scored_by_position = {
            (pickup_position, delivery_position): distance_delta
            for distance_delta, pickup_position, delivery_position in scored
        }
        scored = [
            (scored_by_position[position], position[0], position[1])
            for position in positions
        ]

    if _LEGACY_CANDIDATE_EVAL:
        # Pre-optimization behaviour for A/B diagnostics only.
        old_score = evaluator.evaluate(materialized).score
        legacy_options: list[RouteInsertionOption] = []
        for pickup_position, delivery_position in positions:
            candidate = insert_pair(
                materialized, task_id, pickup_position, delivery_position
            )
            delta = evaluator.evaluate(candidate).score - old_score
            legacy_options.append(
                RouteInsertionOption(
                    delta,
                    route_index,
                    pickup_position,
                    delivery_position,
                    candidate,
                )
            )
        legacy_options.sort(
            key=lambda option: (
                option.delta,
                option.route_index,
                option.pickup_position,
                option.delivery_position,
                option.route,
            )
        )
        return tuple(legacy_options[:option_count])

    counters = evaluator.counters
    ranked: list[tuple[Score, int, int, float, float]] = []
    for delta_distance, pickup_position, delivery_position in scored:
        result = pair_insertion_delta(
            problem,
            surface,
            task_id,
            -task_id,
            pickup_position,
            delivery_position,
            is_delivery=True,
            task_id=task_id,
            counters=counters,
            delta_distance=delta_distance,
        )
        ranked.append(
            (
                result.delta,
                pickup_position,
                delivery_position,
                result.start_time_min,
                result.end_time_min,
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    options: list[RouteInsertionOption] = []
    for delta, pickup_position, delivery_position, start_time, end_time in ranked[
        :option_count
    ]:
        candidate = insert_pair(
            materialized, task_id, pickup_position, delivery_position
        )
        counters.route_materialization_count += 1
        options.append(
            RouteInsertionOption(
                delta,
                route_index,
                pickup_position,
                delivery_position,
                candidate,
                start_time,
                end_time,
            )
        )
    return tuple(options)


def insert_pair(
    route: Sequence[int], task_id: int, pickup_position: int, delivery_position: int
) -> Route:
    with_pickup = tuple(route[:pickup_position]) + (task_id,) + tuple(
        route[pickup_position:]
    )
    return (
        with_pickup[:delivery_position]
        + (-task_id,)
        + with_pickup[delivery_position:]
    )


def insert_leg_pair(
    route: Sequence[int],
    start_visit: int,
    end_visit: int,
    pickup_position: int,
    delivery_position: int,
) -> Route:
    """Insert one relay transport leg (start/end visit pair) into a route."""

    with_pickup = tuple(route[:pickup_position]) + (start_visit,) + tuple(
        route[pickup_position:]
    )
    return (
        with_pickup[:delivery_position]
        + (end_visit,)
        + with_pickup[delivery_position:]
    )


@dataclass(frozen=True, slots=True)
class _DeliveryRecord:
    """One delivery event of the base route, in positional order."""

    position: int
    task_id: int
    deadline_min: float
    old_time_min: float


@dataclass(frozen=True, slots=True)
class RouteInsertionSurface:
    """Static snapshot of one route, built once per route version.

    Everything a lightweight insertion delta needs is cached here:
    capacity-feasible pair positions, visit node ids, the exact base score,
    per-event arrival times, and the base route's delivery events.
    """

    route: Route
    profile: RouteProfile
    positions: tuple[tuple[int, int], ...]
    visit_nodes: tuple[int, ...]
    base_score: Score
    arrival: tuple[float, ...]
    deliveries: tuple[_DeliveryRecord, ...]
    delivery_positions: tuple[int, ...]


def route_insertion_surface(
    problem: Problem, route: Sequence[int]
) -> RouteInsertionSurface:
    materialized = tuple(route)
    profile = RouteProfile.build(materialized)
    length = len(materialized)
    positions = tuple(
        (pickup, delivery)
        for pickup in range(length + 1)
        for delivery in range(pickup + 1, length + 2)
        if profile.can_insert(pickup, delivery, problem.capacity)
    )
    visit_nodes = tuple(
        problem._node_for_visit(visit) for visit in materialized
    )
    # One sequential walk reproduces the RouteEvaluator arithmetic exactly:
    # distance accumulation, per-event elapsed times and delivery records.
    registry = problem.leg_registry
    distance_fn = problem.distance
    speed = problem.speed_km_per_min
    deadline_fn = problem.task
    previous: int | None = None
    distance = 0.0
    elapsed = 0.0
    arrival: list[float] = []
    deliveries: list[_DeliveryRecord] = []
    late_count = 0
    total_lateness = 0.0
    for position, visit in enumerate(materialized):
        visit_id = abs(visit)
        leg = registry.get(visit_id) if registry else None
        if leg is None and visit_id not in problem._task_by_id:
            raise ValueError(f"未知任务 {visit_id}")
        leg_km = distance_fn(previous, visit)
        distance += leg_km
        elapsed += leg_km / speed
        previous = visit
        arrival.append(elapsed)
        if leg is not None:
            if visit < 0 and leg.kind == "RELAY_OUT":
                task_id = leg.task_id
                deadline = deadline_fn(task_id).deadline_min
                deliveries.append(
                    _DeliveryRecord(position, task_id, deadline, elapsed)
                )
                lateness = max(0.0, elapsed - deadline)
                if lateness > 1e-9:
                    late_count += 1
                    total_lateness += lateness
            continue
        if visit < 0:
            task_id = visit_id
            deadline = deadline_fn(task_id).deadline_min
            deliveries.append(
                _DeliveryRecord(position, task_id, deadline, elapsed)
            )
            lateness = max(0.0, elapsed - deadline)
            if lateness > 1e-9:
                late_count += 1
                total_lateness += lateness
    return RouteInsertionSurface(
        materialized,
        profile,
        positions,
        visit_nodes,
        Score(late_count, total_lateness, distance),
        tuple(arrival),
        tuple(deliveries),
        tuple(record.position for record in deliveries),
    )


def _node_distance_delta(
    distances,
    visit_nodes: tuple[int, ...],
    start_node: int,
    end_node: int,
    pickup_position: int,
    delivery_position: int,
) -> float:
    """Distance delta from precomputed node ids (same arithmetic order as
    :meth:`RouteProfile.distance_delta`, hence identical float results)."""

    route_nodes = visit_nodes
    length = len(route_nodes)
    before_pickup = (
        None if pickup_position == 0 else route_nodes[pickup_position - 1]
    )
    after_pickup = (
        route_nodes[pickup_position] if pickup_position < length else None
    )
    from_pickup = 0 if before_pickup is None else before_pickup
    old_pickup_edge = (
        0.0 if after_pickup is None else distances[from_pickup][after_pickup]
    )
    pickup_in = distances[from_pickup][start_node]
    pickup_out = (
        0.0 if after_pickup is None else distances[start_node][after_pickup]
    )
    if delivery_position == pickup_position + 1:
        new = (
            pickup_in
            + distances[start_node][end_node]
            + (0.0 if after_pickup is None else distances[end_node][after_pickup])
        )
        return new - old_pickup_edge
    before_delivery = route_nodes[delivery_position - 2]
    after_delivery_index = delivery_position - 1
    after_delivery = (
        route_nodes[after_delivery_index]
        if after_delivery_index < length
        else None
    )
    old_delivery_edge = (
        0.0
        if after_delivery is None
        else distances[before_delivery][after_delivery]
    )
    old = old_pickup_edge + old_delivery_edge
    new = (
        pickup_in
        + pickup_out
        + distances[before_delivery][end_node]
        + (0.0 if after_delivery is None else distances[end_node][after_delivery])
    )
    return new - old


@dataclass(frozen=True, slots=True)
class InsertionDeltaResult:
    """Incremental score delta plus the inserted pair's event times."""

    delta: Score
    start_time_min: float
    end_time_min: float


def pair_insertion_delta(
    problem: Problem,
    surface: RouteInsertionSurface,
    start_visit: int,
    end_visit: int,
    pickup_position: int,
    delivery_position: int,
    *,
    is_delivery: bool,
    task_id: int,
    counters: SearchCounters | None = None,
    delta_distance: float | None = None,
) -> InsertionDeltaResult:
    """Lightweight insertion delta for one (start, end) pair position.

    Never materializes the candidate route and never re-simulates the base
    route.  The distance delta reuses the exact node-arithmetic of
    ``_node_distance_delta`` (pass ``delta_distance`` to skip recomputation
    in ranking loops that already know it); arrival times are derived from
    cached prefix data via piecewise shifts (insertions never make events
    earlier, so every affected time only moves later).

    ``is_delivery`` marks an end event that completes an original task
    (DIRECT delivery or RELAY_OUT); RELAY_IN drops contribute no lateness.
    """

    if counters is not None:
        counters.route_delta_evaluation_count += 1
    route = surface.route
    length = len(route)
    speed = problem.speed_km_per_min
    distances = problem._distances
    visit_nodes = surface.visit_nodes
    start_node = problem._node_for_visit(start_visit)
    end_node = problem._node_for_visit(end_visit)
    if delta_distance is None:
        delta_distance = _node_distance_delta(
            distances,
            visit_nodes,
            start_node,
            end_node,
            pickup_position,
            delivery_position,
        )

    before_pickup = (
        None if pickup_position == 0 else visit_nodes[pickup_position - 1]
    )
    after_pickup = (
        visit_nodes[pickup_position] if pickup_position < length else None
    )
    from_pickup = 0 if before_pickup is None else before_pickup
    pickup_in = distances[from_pickup][start_node]
    pickup_out = (
        0.0 if after_pickup is None else distances[start_node][after_pickup]
    )
    old_pickup_edge = (
        0.0 if after_pickup is None else distances[from_pickup][after_pickup]
    )
    pickup_shift = (
        pickup_in + pickup_out - old_pickup_edge
    ) / speed
    time_before = (
        0.0
        if pickup_position == 0
        else surface.arrival[pickup_position - 1]
    )
    start_time = time_before + pickup_in / speed

    if delivery_position == pickup_position + 1:
        # Adjacent pair: pickup and delivery replace one single old edge.
        delivery_in = distances[start_node][end_node]
        delivery_out = (
            0.0
            if after_pickup is None
            else distances[end_node][after_pickup]
        )
        delivery_shift = (delivery_in + delivery_out - pickup_out) / speed
        end_time = start_time + delivery_in / speed
        total_shift = pickup_shift + delivery_shift
        affected_start = pickup_position
        first_shift = total_shift
        split_position = length
        second_shift = total_shift
    else:
        before_delivery = visit_nodes[delivery_position - 2]
        after_delivery_index = delivery_position - 1
        after_delivery = (
            visit_nodes[after_delivery_index]
            if after_delivery_index < length
            else None
        )
        delivery_in = distances[before_delivery][end_node]
        delivery_out = (
            0.0
            if after_delivery is None
            else distances[end_node][after_delivery]
        )
        old_delivery_edge = (
            0.0
            if after_delivery is None
            else distances[before_delivery][after_delivery]
        )
        delivery_shift = (delivery_in + delivery_out - old_delivery_edge) / speed
        end_time = (
            surface.arrival[delivery_position - 2]
            + pickup_shift
            + delivery_in / speed
        )
        affected_start = pickup_position
        first_shift = pickup_shift
        split_position = delivery_position - 1
        second_shift = pickup_shift + delivery_shift

    # Shifted base deliveries: only events at/after the pickup insertion move.
    delta_late_count = 0
    delta_lateness = 0.0
    deliveries = surface.deliveries
    delivery_positions = surface.delivery_positions
    start_index = bisect_left(delivery_positions, affected_start)
    for record in deliveries[start_index:]:
        position = record.position
        if position >= length:
            break
        shift = first_shift if position < split_position else second_shift
        if shift <= 0.0:
            continue
        old_late = record.old_time_min - record.deadline_min
        old_term = old_late if old_late > 1e-9 else 0.0
        new_late = record.old_time_min + shift - record.deadline_min
        new_term = new_late if new_late > 1e-9 else 0.0
        if new_term > 0.0 and old_term <= 0.0:
            delta_late_count += 1
        delta_lateness += new_term - old_term

    if is_delivery:
        deadline = problem.task(task_id).deadline_min
        late = end_time - deadline
        if late > 1e-9:
            delta_late_count += 1
            delta_lateness += late
    return InsertionDeltaResult(
        Score(delta_late_count, delta_lateness, delta_distance),
        start_time,
        end_time,
    )


class InsertionDeltaEvaluator:
    """Public wrapper around :func:`pair_insertion_delta` (same engine).

    Used for both DIRECT and RELAY legs because both reduce to one
    (start node, end node) pair insertion.
    """

    def __init__(
        self, problem: Problem, counters: SearchCounters | None = None
    ) -> None:
        self.problem = problem
        self.counters = counters

    def evaluate(
        self,
        surface: RouteInsertionSurface,
        start_visit: int,
        end_visit: int,
        pickup_position: int,
        delivery_position: int,
        *,
        is_delivery: bool,
        task_id: int,
    ) -> InsertionDeltaResult:
        return pair_insertion_delta(
            self.problem,
            surface,
            start_visit,
            end_visit,
            pickup_position,
            delivery_position,
            is_delivery=is_delivery,
            task_id=task_id,
            counters=self.counters,
        )


def _k_cheapest_positions(
    problem: Problem,
    surface: RouteInsertionSurface,
    start_node: int,
    end_node: int,
    limit: int,
) -> list[tuple[float, int, int]]:
    """Top-k capacity-feasible positions by node-based distance delta.

    Returns ``(distance_delta, pickup, delivery)`` triples so the caller can
    reuse the distance deltas for the incremental score evaluation.  A
    manual bounded heap avoids the closure cost of ``heapq.nsmallest``
    inside the hot repair loop.
    """

    distances = problem._distances
    visit_nodes = surface.visit_nodes
    positions = surface.positions
    if len(positions) <= limit:
        return [
            (
                _node_distance_delta(
                    distances,
                    visit_nodes,
                    start_node,
                    end_node,
                    pickup,
                    delivery,
                ),
                pickup,
                delivery,
            )
            for pickup, delivery in positions
        ]
    # Deterministic cheapest subset equivalent to sorting by
    # (delta, -index, pickup, delivery): heapq.nsmallest compares the full
    # (delta, -index, pickup, delivery) tuples lexicographically.
    items = [
        (
            _node_distance_delta(
                distances, visit_nodes, start_node, end_node, pickup, delivery
            ),
            -index,
            pickup,
            delivery,
        )
        for index, (pickup, delivery) in enumerate(positions)
    ]
    top = nsmallest(limit, items)
    top.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    return [(item[0], item[2], item[3]) for item in top]


def route_leg_insertion_options(
    problem: Problem,
    evaluator: RouteEvaluator,
    route: Sequence[int],
    route_index: int,
    start_visit: int,
    end_visit: int,
    *,
    candidate_limit: int | None,
    option_count: int = 8,
    counts_toward_k: bool = False,
    event_cap: int | None = 60,
    surface: RouteInsertionSurface | None = None,
) -> tuple[RouteInsertionOption, ...]:
    """Best local deltas for inserting one relay leg into one route.

    ``counts_toward_k`` is False for RELAY_IN legs (they do not consume a
    K=25 delivery-responsibility slot) and True for RELAY_OUT legs.
    ``event_cap`` is a soft pruning budget only, never a hard constraint.
    A prebuilt ``surface`` avoids re-enumerating capacity-feasible
    positions when many legs share the same route snapshot.
    """

    if surface is None:
        surface = route_insertion_surface(problem, route)
    materialized = surface.route
    if (
        counts_toward_k
        and _delivery_event_count(problem, materialized)
        >= problem.max_tasks_per_drone
    ):
        return ()
    if event_cap is not None and len(materialized) + 2 > event_cap:
        return ()
    if candidate_limit is not None and len(surface.positions) > candidate_limit:
        limit = max(2, candidate_limit)
        start_node = problem._node_for_visit(start_visit)
        end_node = problem._node_for_visit(end_visit)
        scored = _k_cheapest_positions(
            problem, surface, start_node, end_node, limit
        )
    else:
        distances = problem._distances
        visit_nodes = surface.visit_nodes
        start_node = problem._node_for_visit(start_visit)
        end_node = problem._node_for_visit(end_visit)
        scored = [
            (
                _node_distance_delta(
                    distances,
                    visit_nodes,
                    start_node,
                    end_node,
                    pickup_position,
                    delivery_position,
                ),
                pickup_position,
                delivery_position,
            )
            for pickup_position, delivery_position in surface.positions
        ]
    end_leg = problem.leg_registry.get(abs(end_visit))
    is_delivery = end_leg is not None and end_leg.kind == "RELAY_OUT"
    task_id = end_leg.task_id if end_leg is not None else abs(end_visit)
    if _LEGACY_CANDIDATE_EVAL:
        # Pre-optimization behaviour for A/B diagnostics only.
        old_score = evaluator.evaluate(materialized).score
        legacy_options = []
        for _, pickup_position, delivery_position in scored:
            candidate = insert_leg_pair(
                materialized,
                start_visit,
                end_visit,
                pickup_position,
                delivery_position,
            )
            delta = evaluator.evaluate(candidate).score - old_score
            legacy_options.append(
                RouteInsertionOption(
                    delta,
                    route_index,
                    pickup_position,
                    delivery_position,
                    candidate,
                )
            )
        legacy_options.sort(
            key=lambda option: (
                option.delta,
                option.route_index,
                option.pickup_position,
                option.delivery_position,
                option.route,
            )
        )
        return tuple(legacy_options[:option_count])
    counters = evaluator.counters
    ranked: list[tuple[Score, int, int, float, float]] = []
    for delta_distance, pickup_position, delivery_position in scored:
        result = pair_insertion_delta(
            problem,
            surface,
            start_visit,
            end_visit,
            pickup_position,
            delivery_position,
            is_delivery=is_delivery,
            task_id=task_id,
            counters=counters,
            delta_distance=delta_distance,
        )
        ranked.append(
            (
                result.delta,
                pickup_position,
                delivery_position,
                result.start_time_min,
                result.end_time_min,
            )
        )
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    options: list[RouteInsertionOption] = []
    for delta, pickup_position, delivery_position, start_time, end_time in ranked[
        :option_count
    ]:
        candidate = insert_leg_pair(
            materialized,
            start_visit,
            end_visit,
            pickup_position,
            delivery_position,
        )
        counters.route_materialization_count += 1
        options.append(
            RouteInsertionOption(
                delta,
                route_index,
                pickup_position,
                delivery_position,
                candidate,
                start_time,
                end_time,
            )
        )
    return tuple(options)


def insert_task_best(
    problem: Problem, routes: Sequence[Sequence[int]], task_id: int
) -> InsertionResult:
    """Insert a complete task pair at the lexicographically best positions."""

    materialized: Routes = tuple(tuple(route) for route in routes)
    if len(materialized) > problem.drone_count:
        raise ValueError(
            f"输入路线数 {len(materialized)} 超过无人机数 {problem.drone_count}"
        )
    if task_id not in problem._task_by_id:
        raise ValueError(f"未知任务 {task_id}")
    if any(task_id in route or -task_id in route for route in materialized):
        raise ValueError(f"任务 {task_id} 已经在路线中")

    evaluator = RouteEvaluator(problem)
    base_score = routes_score(evaluator, materialized)
    best: tuple[Score, int, int, int, Route] | None = None
    for route_index in range(problem.drone_count):
        route = materialized[route_index] if route_index < len(materialized) else ()
        task_count = sum(1 for visit in route if visit > 0)
        if task_count >= problem.max_tasks_per_drone:
            continue
        base_metrics = evaluator.evaluate(route)
        for option in route_insertion_options(
            problem,
            evaluator,
            route,
            route_index,
            task_id,
            candidate_limit=None,
            option_count=10_000,
        ):
            # Exact scoring: insert_task_best is an off-line primitive used
            # by constructors and tests, never inside the hot search loop.
            candidate_score = base_score + (
                evaluator.evaluate(option.route).score - base_metrics.score
            )
            key = (
                candidate_score,
                route_index,
                option.pickup_position,
                option.delivery_position,
                option.route,
            )
            if best is None or key < best:
                best = key
    if best is None:
        raise ValueError(f"任务 {task_id} 没有可用的路线槽位")

    _, route_index, pickup_position, delivery_position, new_route = best
    expanded = list(materialized)
    while len(expanded) < problem.drone_count:
        expanded.append(())
    expanded[route_index] = new_route
    new_routes: Routes = tuple(expanded)
    return InsertionResult(
        routes=new_routes,
        evaluation=evaluate_solution(problem, new_routes),
        route_index=route_index,
        pickup_position=pickup_position,
        delivery_position=delivery_position,
    )
