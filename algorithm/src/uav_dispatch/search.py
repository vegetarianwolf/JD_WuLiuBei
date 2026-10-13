"""Reusable route scoring and capacity-two pair insertion primitives."""

from __future__ import annotations

import os
from bisect import bisect_left
from dataclasses import dataclass, field
from functools import lru_cache
from heapq import nsmallest
from types import MappingProxyType
from typing import Any, Mapping, Sequence

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
    metadata: Mapping[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True, slots=True)
class InsertionResult:
    routes: Routes
    evaluation: SolutionEvaluation
    route_index: int
    pickup_position: int
    delivery_position: int


class _SurfaceLRU:
    """Solve-scoped route-snapshot cache shared across ALNS iterations.

    A ``RouteInsertionSurface`` is a pure function of ``(problem, route)``,
    so keying by the immutable route tuple only is safe.  The cache is
    owned by one :class:`RouteEvaluator` and therefore by one problem
    instance, which keeps rejected-candidate surfaces reusable across
    iterations without ever mixing problems.  Entries carry no task_id
    specific data.  FIFO eviction keeps the working set bounded.
    """

    __slots__ = ("problem", "maxsize", "cache", "hits", "misses", "counters")

    def __init__(
        self,
        problem: Problem,
        maxsize: int = 20_000,
        counters: SearchCounters | None = None,
    ) -> None:
        self.problem = problem
        self.maxsize = maxsize
        self.cache: dict[tuple[Route, int], RouteInsertionSurface] = {}
        self.hits = 0
        self.misses = 0
        self.counters = counters

    def get(
        self, route: Sequence[int], *, start_node: int = 0
    ) -> RouteInsertionSurface:
        key = (route if isinstance(route, tuple) else tuple(route), start_node)
        cached = self.cache.get(key)
        if cached is not None:
            self.hits += 1
            if self.counters is not None:
                self.counters.surface_cache_hit_count += 1
            return cached
        self.misses += 1
        if self.counters is not None:
            self.counters.surface_cache_miss_count += 1
            self.counters.surface_build_count += 1
        surface = route_insertion_surface(self.problem, key[0], start_node)
        if len(self.cache) >= self.maxsize:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = surface
        return surface


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
        self.surface_cache = _SurfaceLRU(problem, counters=self.counters)

        @lru_cache(maxsize=cache_size)
        def cached(route: Route, start_node: int) -> RouteMetrics:
            return self._evaluate_uncached(route, start_node)

        self._cached = cached

    def evaluate(
        self, route: Sequence[int], *, start_node: int = 0
    ) -> RouteMetrics:
        return self._cached(tuple(route), start_node)

    def _evaluate_uncached(
        self, route: Route, start_node: int
    ) -> RouteMetrics:
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
            leg_km = distance_fn(
                previous,
                visit,
                from_node=start_node if previous is None else None,
            )
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
    problem = evaluator.problem
    for route_index, route in enumerate(routes):
        score += evaluator.evaluate(
            route, start_node=problem.home_node(route_index)
        ).score
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
    """Capacity and distance summaries for fixed-position pair insertions.

    ``next_full_position[p]`` is the first position ``i >= p`` whose
    load-before value reaches ``capacity`` (``size`` when none exists).
    A pair insertion at ``(p, d)`` is capacity-feasible iff the shifted
    load in ``[p, d - 1]`` stays below capacity, i.e. iff no full-load
    position falls inside that range: ``d <= next_full_position[p]``.
    """

    route: Route
    loads_before: tuple[int, ...]
    capacity: int
    next_full_position: tuple[int, ...]

    @classmethod
    def build(cls, route: Sequence[int], capacity: int) -> "RouteProfile":
        materialized = tuple(route)
        loads = _load_before(materialized)
        size = len(loads)
        next_full = [size] * size
        first_full = size
        for position in range(size - 1, -1, -1):
            if loads[position] >= capacity:
                first_full = position
            next_full[position] = first_full
        return cls(materialized, loads, capacity, tuple(next_full))

    def can_insert(self, pickup_position: int, delivery_position: int) -> bool:
        return delivery_position <= self.next_full_position[pickup_position]

    def distance_delta(
        self,
        problem: Problem,
        task_id: int,
        pickup_position: int,
        delivery_position: int,
        *,
        start_visit: int | None = None,
        end_visit: int | None = None,
        route_start_node: int = 0,
    ) -> float:
        start = task_id if start_visit is None else start_visit
        end = -task_id if end_visit is None else end_visit
        route = self.route
        length = len(route)

        def edge(left: int | None, right: int | None) -> float:
            if right is None:
                return 0.0
            if left is None:
                return problem.distance(
                    None, right, from_node=route_start_node
                )
            return problem.distance(left, right)

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
    # Reference implementation.  The ALNS uses RouteProfile.next_full_position
    # for the corresponding O(1) query; this direct slice remains useful for
    # auditing and equivalence testing.
    return max(loads_before[pickup_position:delivery_position]) < capacity


def feasible_pair_positions(
    problem: Problem, route: Sequence[int]
) -> tuple[tuple[int, int], ...]:
    """Enumerate every precedence- and capacity-feasible pair position."""

    length = len(route)
    profile = RouteProfile.build(route, problem.capacity)
    positions: list[tuple[int, int]] = []
    for pickup_position in range(length + 1):
        delivery_limit = profile.next_full_position[pickup_position]
        for delivery_position in range(
            pickup_position + 1, delivery_limit + 1
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


def _build_distance_delta_arrays(
    distances,
    visit_nodes: tuple[int, ...],
    start_node: int,
    end_node: int,
    *,
    route_start_node: int = 0,
) -> tuple[list, list, list, list, list, list, float]:
    """O(n) pickup/delivery side arrays for O(1) pair distance deltas.

    Returns ``(pickup_in, pickup_pair, old_pickup_edge, delivery_in,
    delivery_out, old_delivery_edge, d_ss)``.  ``_array_distance_delta``
    reconstructs every delta with the exact operation order of
    :func:`_node_distance_delta`, so results are bit-identical.
    """

    length = len(visit_nodes)
    pickup_in = [0.0] * (length + 1)
    pickup_pair = [0.0] * (length + 1)
    old_pickup_edge = [0.0] * (length + 1)
    for pickup in range(length + 1):
        from_node = (
            route_start_node if pickup == 0 else visit_nodes[pickup - 1]
        )
        if pickup < length:
            after = visit_nodes[pickup]
            pickup_out = distances[start_node][after]
            old_pickup_edge[pickup] = distances[from_node][after]
        else:
            pickup_out = 0.0
            old_pickup_edge[pickup] = 0.0
        pickup_in[pickup] = distances[from_node][start_node]
        pickup_pair[pickup] = pickup_in[pickup] + pickup_out

    delivery_in = [0.0] * (length + 2)
    delivery_out = [0.0] * (length + 2)
    old_delivery_edge = [0.0] * (length + 2)
    for delivery in range(1, length + 2):
        after_index = delivery - 1
        if after_index < length:
            after = visit_nodes[after_index]
            delivery_out[delivery] = distances[end_node][after]
        else:
            delivery_out[delivery] = 0.0
        if delivery >= 2:
            before_delivery = visit_nodes[delivery - 2]
            delivery_in[delivery] = distances[before_delivery][end_node]
            if after_index < length:
                old_delivery_edge[delivery] = distances[before_delivery][
                    after
                ]

    return (
        pickup_in,
        pickup_pair,
        old_pickup_edge,
        delivery_in,
        delivery_out,
        old_delivery_edge,
        distances[start_node][end_node],
    )


def _array_distance_delta(
    arrays: tuple,
    pickup_position: int,
    delivery_position: int,
) -> float:
    """Distance delta from precomputed arrays (see _build_distance_delta_arrays)."""

    (
        pickup_in,
        pickup_pair,
        old_pickup_edge,
        delivery_in,
        delivery_out,
        old_delivery_edge,
        d_ss,
    ) = arrays
    if delivery_position == pickup_position + 1:
        return (
            pickup_in[pickup_position]
            + d_ss
            + delivery_out[delivery_position]
        ) - old_pickup_edge[pickup_position]
    return (
        (pickup_pair[pickup_position] + delivery_in[delivery_position])
        + delivery_out[delivery_position]
    ) - (
        old_pickup_edge[pickup_position]
        + old_delivery_edge[delivery_position]
    )


def _scored_distance_deltas(
    arrays: tuple, positions: Sequence[tuple[int, int]]
) -> list[tuple[float, int, int]]:
    """Full-position distance-delta scan, inlined for speed.

    ``arrays`` is the 7-tuple from :func:`_build_distance_delta_arrays`.
    Returns ``[(distance_delta, pickup, delivery), ...]`` in the input
    ``positions`` order.  The arithmetic below reproduces the former
    per-position ``delta_of`` closure exactly (same left-to-right grouping),
    so the values are bit-identical; the gain comes from removing one
    function call and one array-tuple unpack per position.
    """

    (
        pickup_in,
        pickup_pair,
        old_pickup_edge,
        delivery_in,
        delivery_out,
        old_delivery_edge,
        d_ss,
    ) = arrays
    scored = []
    append = scored.append
    for pickup_position, delivery_position in positions:
        if delivery_position == pickup_position + 1:
            append(
                (
                    pickup_in[pickup_position]
                    + d_ss
                    + delivery_out[delivery_position]
                    - old_pickup_edge[pickup_position],
                    pickup_position,
                    delivery_position,
                )
            )
        else:
            append(
                (
                    pickup_pair[pickup_position]
                    + delivery_in[delivery_position]
                    + delivery_out[delivery_position]
                    - (
                        old_pickup_edge[pickup_position]
                        + old_delivery_edge[delivery_position]
                    ),
                    pickup_position,
                    delivery_position,
                )
            )
    return scored


def _select_candidate_positions(
    scored: Sequence[tuple[float, int, int]],
    limit: int,
    early_positions: Sequence[tuple[int, int]],
    delta_of,
) -> list[tuple[float, int, int]]:
    """Bounded deterministic screening: distance + early-delivery + fill.

    Returns the selected ``(distance_delta, pickup, delivery)`` triples in
    exactly the order the previous full-sort implementation produced.
    ``early_positions`` must list the feasible positions in
    ``(delivery, pickup)`` lexicographic order; because every position pair
    is unique, the early-delivery ranking key ``(delivery, pickup, delta)``
    is decided by ``(delivery, pickup)`` alone, so its ``limit``-prefix
    equals the full sort's prefix and no second ``nsmallest`` pass is
    needed.  ``delta_of`` is invoked lazily, only for positions that are
    actually selected.
    """

    by_distance = nsmallest(limit, scored)
    by_distance.sort(key=lambda item: (item[0], item[1], item[2]))
    selected: list[tuple[float, int, int]] = []
    selected_set: set[tuple[int, int]] = set()

    # Distance quota first.
    added = 0
    for item in by_distance:
        position = (item[1], item[2])
        if position not in selected_set:
            selected.append(item)
            selected_set.add(position)
            added += 1
        if added >= max(1, limit // 2) or len(selected) >= limit:
            break

    # Early-delivery quota (positions already in (delivery, pickup) order).
    if len(selected) < limit:
        added = 0
        for pickup, delivery in early_positions[:limit]:
            position = (pickup, delivery)
            if position not in selected_set:
                selected.append(
                    (delta_of(pickup, delivery), pickup, delivery)
                )
                selected_set.add(position)
                added += 1
            if added >= max(1, limit // 3) or len(selected) >= limit:
                break

    # Distance fill-up.
    for item in by_distance:
        if len(selected) >= limit:
            break
        position = (item[1], item[2])
        if position not in selected_set:
            selected.append(item)
            selected_set.add(position)
    return selected


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
        surface = evaluator.surface_cache.get(
            route, start_node=problem.home_node(route_index)
        )
    materialized = surface.route
    if surface.delivery_event_count >= problem.max_tasks_per_drone:
        return ()
    positions = surface.positions
    # Distance deltas are computed once per position and reused both by the
    # candidate pruning and by the incremental score deltas below, via O(n)
    # decomposed side arrays (bit-identical to ``_node_distance_delta``).
    distances = problem._distances
    visit_nodes = surface.visit_nodes
    start_node = problem._node_for_visit(task_id)
    end_node = problem._node_for_visit(-task_id)
    route_start_node = problem.home_node(route_index)
    arrays = _build_distance_delta_arrays(
        distances,
        visit_nodes,
        start_node,
        end_node,
        route_start_node=route_start_node,
    )
    (
        pickup_in,
        pickup_pair,
        old_pickup_edge,
        delivery_in,
        delivery_out,
        old_delivery_edge,
        d_ss,
    ) = arrays
    deadline = problem.task(task_id).deadline_min
    speed = problem.speed_km_per_min

    def delta_of(pickup_position: int, delivery_position: int) -> float:
        if delivery_position == pickup_position + 1:
            return (
                pickup_in[pickup_position]
                + d_ss
                + delivery_out[delivery_position]
            ) - old_pickup_edge[pickup_position]
        return (
            (pickup_pair[pickup_position] + delivery_in[delivery_position])
            + delivery_out[delivery_position]
        ) - (
            old_pickup_edge[pickup_position]
            + old_delivery_edge[delivery_position]
        )

    # Full-position scan hit O(n^2) times per (task, route): the scan lives
    # in _scored_distance_deltas (inlined arithmetic, bit-identical to the
    # closure above) to keep the hot loop closure-call and unpack-free.
    scored = _scored_distance_deltas(arrays, positions)
    if candidate_limit is not None and len(positions) > candidate_limit:
        limit = max(4, candidate_limit)
        scored = _select_candidate_positions(
            scored, limit, surface.positions_by_early_delivery, delta_of
        )
        positions = tuple((item[1], item[2]) for item in scored)

    if _LEGACY_CANDIDATE_EVAL:
        # Pre-optimization behaviour for A/B diagnostics only.
        old_score = evaluator.evaluate(
            materialized, start_node=route_start_node
        ).score
        legacy_options: list[RouteInsertionOption] = []
        for pickup_position, delivery_position in positions:
            candidate = insert_pair(
                materialized, task_id, pickup_position, delivery_position
            )
            delta = (
                evaluator.evaluate(
                    candidate, start_node=route_start_node
                ).score
                - old_score
            )
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
                option.delta.late_count,
                option.delta.total_lateness_min,
                option.delta.distance_km,
                option.route_index,
                option.pickup_position,
                option.delivery_position,
                option.route,
            )
        )
        return tuple(legacy_options[:option_count])

    counters = evaluator.counters
    ranked: list[tuple[int, float, float, int, int, float, float]] = []
    for delta_distance, pickup_position, delivery_position in scored:
        (
            delta_late_count,
            delta_lateness,
            distance_delta,
            start_time,
            end_time,
        ) = _pair_insertion_delta_raw(
            surface,
            start_node,
            end_node,
            pickup_position,
            delivery_position,
            is_delivery=True,
            deadline=deadline,
            delta_distance=delta_distance,
            speed=speed,
            distances=distances,
            counters=counters,
            route_start_node=route_start_node,
        )
        ranked.append(
            (
                delta_late_count,
                delta_lateness,
                distance_delta,
                pickup_position,
                delivery_position,
                start_time,
                end_time,
            )
        )
    ranked.sort(
        key=lambda item: (item[0], item[1], item[2], item[3], item[4])
    )
    options: list[RouteInsertionOption] = []
    for (
        delta_late_count,
        delta_lateness,
        distance_delta,
        pickup_position,
        delivery_position,
        start_time,
        end_time,
    ) in ranked[:option_count]:
        candidate = insert_pair(
            materialized, task_id, pickup_position, delivery_position
        )
        counters.route_materialization_count += 1
        options.append(
            RouteInsertionOption(
                Score(delta_late_count, delta_lateness, distance_delta),
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
    """One delivery event of the base route, in positional order.

    ``old_slack_min`` is the raw (old_time - deadline) value, possibly
    negative; ``old_term_min`` and ``old_late`` are the thresholded
    lateness contribution used by the incremental delta loops.
    """

    position: int
    task_id: int
    deadline_min: float
    old_time_min: float
    old_slack_min: float
    old_term_min: float
    old_late: bool


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
    positions_by_early_delivery: tuple[tuple[int, int], ...]
    visit_nodes: tuple[int, ...]
    base_score: Score
    arrival: tuple[float, ...]
    deliveries: tuple[_DeliveryRecord, ...]
    delivery_positions: tuple[int, ...]
    delivery_start_index: tuple[int, ...]
    delivery_event_count: int


def route_insertion_surface(
    problem: Problem, route: Sequence[int], start_node: int = 0
) -> RouteInsertionSurface:
    materialized = tuple(route)
    profile = RouteProfile.build(materialized, problem.capacity)
    length = len(materialized)
    position_list: list[tuple[int, int]] = []
    for pickup in range(length + 1):
        delivery_limit = profile.next_full_position[pickup]
        for delivery in range(pickup + 1, delivery_limit + 1):
            position_list.append((pickup, delivery))
    positions = tuple(position_list)
    # (delivery, pickup) lexicographic order, enumerated directly without a
    # sort.  Since every position pair is unique, the early-delivery ranking
    # key (delivery, pickup, delta) is decided by (delivery, pickup) alone.
    early_position_list: list[tuple[int, int]] = []
    for delivery in range(1, length + 2):
        for pickup in range(delivery):
            if delivery <= profile.next_full_position[pickup]:
                early_position_list.append((pickup, delivery))
    positions_by_early_delivery = tuple(early_position_list)
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
        leg_km = distance_fn(
            previous,
            visit,
            from_node=start_node if previous is None else None,
        )
        distance += leg_km
        elapsed += leg_km / speed
        previous = visit
        arrival.append(elapsed)
        if leg is not None:
            if visit < 0 and leg.kind == "RELAY_OUT":
                task_id = leg.task_id
                deadline = deadline_fn(task_id).deadline_min
                slack = elapsed - deadline
                deliveries.append(
                    _DeliveryRecord(
                        position,
                        task_id,
                        deadline,
                        elapsed,
                        slack,
                        slack if slack > 1e-9 else 0.0,
                        slack > 1e-9,
                    )
                )
                lateness = max(0.0, slack)
                if lateness > 1e-9:
                    late_count += 1
                    total_lateness += lateness
            continue
        if visit < 0:
            task_id = visit_id
            deadline = deadline_fn(task_id).deadline_min
            slack = elapsed - deadline
            deliveries.append(
                _DeliveryRecord(
                    position,
                    task_id,
                    deadline,
                    elapsed,
                    slack,
                    slack if slack > 1e-9 else 0.0,
                    slack > 1e-9,
                )
            )
            lateness = max(0.0, slack)
            if lateness > 1e-9:
                late_count += 1
                total_lateness += lateness
    delivery_positions = tuple(record.position for record in deliveries)
    return RouteInsertionSurface(
        materialized,
        profile,
        positions,
        positions_by_early_delivery,
        visit_nodes,
        Score(late_count, total_lateness, distance),
        tuple(arrival),
        tuple(deliveries),
        delivery_positions,
        tuple(
            bisect_left(delivery_positions, position)
            for position in range(length + 1)
        ),
        len(deliveries),
    )


def _node_distance_delta(
    distances,
    visit_nodes: tuple[int, ...],
    start_node: int,
    end_node: int,
    pickup_position: int,
    delivery_position: int,
    *,
    route_start_node: int = 0,
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
    from_pickup = (
        route_start_node if before_pickup is None else before_pickup
    )
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


def _pair_insertion_delta_raw(
    surface: RouteInsertionSurface,
    start_node: int,
    end_node: int,
    pickup_position: int,
    delivery_position: int,
    *,
    is_delivery: bool,
    deadline: float | None,
    delta_distance: float,
    speed: float,
    distances,
    counters: SearchCounters | None = None,
    route_start_node: int = 0,
) -> tuple[int, float, float, float, float]:
    """Allocation-free insertion delta for one (start, end) pair position.

    Returns ``(delta_late_count, delta_lateness, delta_distance,
    start_time, end_time)`` without constructing a ``Score`` or an
    ``InsertionDeltaResult``.  Callers pre-resolve ``start_node``,
    ``end_node``, ``deadline``, ``speed`` and ``distances`` so the
    million-call ranking loops never re-enter ``Problem`` lookups.

    The arithmetic is identical to the former :func:`pair_insertion_delta`
    body: piecewise arrival shifts over cached prefix data, and a plain
    index walk over ``surface.deliveries`` (no tuple slice allocation).
    """

    if counters is not None:
        counters.route_delta_evaluation_count += 1
    route = surface.route
    length = len(route)
    visit_nodes = surface.visit_nodes

    before_pickup = (
        None if pickup_position == 0 else visit_nodes[pickup_position - 1]
    )
    after_pickup = (
        visit_nodes[pickup_position] if pickup_position < length else None
    )
    from_pickup = (
        route_start_node if before_pickup is None else before_pickup
    )
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
    # Two segment loops (first_shift / second_shift) replace the former
    # per-record branch and bisect; delivery record positions are always
    # < length, so no bound check is needed inside the loops.
    delta_late_count = 0
    delta_lateness = 0.0
    deliveries = surface.deliveries
    start_indices = surface.delivery_start_index
    count = len(deliveries)
    split_index = start_indices[split_position]
    if first_shift > 0.0:
        index = start_indices[affected_start]
        while index < split_index:
            record = deliveries[index]
            old_term = record.old_term_min
            new_late = record.old_slack_min + first_shift
            new_term = new_late if new_late > 1e-9 else 0.0
            if new_term > 0.0 and old_term <= 0.0:
                delta_late_count += 1
            delta_lateness += new_term - old_term
            index += 1
    else:
        index = split_index
    if second_shift > 0.0:
        while index < count:
            record = deliveries[index]
            old_term = record.old_term_min
            new_late = record.old_slack_min + second_shift
            new_term = new_late if new_late > 1e-9 else 0.0
            if new_term > 0.0 and old_term <= 0.0:
                delta_late_count += 1
            delta_lateness += new_term - old_term
            index += 1

    if is_delivery:
        late = end_time - deadline
        if late > 1e-9:
            delta_late_count += 1
            delta_lateness += late
    return (
        delta_late_count,
        delta_lateness,
        delta_distance,
        start_time,
        end_time,
    )


def _visits_removal_delta_exact(
    surface: RouteInsertionSurface,
    positions: Sequence[int],
    *,
    speed: float,
    distances,
    registry,
    route_start_node: int = 0,
) -> tuple[int, float, float]:
    """Removal gain ``score(full) - score(full minus the visits at
    ``positions``)`` computed bit-identically to full route evaluation.

    Works for DIRECT task pairs and for relay leg pairs: every visit at a
    removed position disappears, and only delivery records whose position
    is removed leave the lateness aggregate (RELAY_IN end visits produce
    no delivery record, so nothing is subtracted for them).  A RELAY_OUT
    delivery is keyed by its downstream task id — exactly like the full
    evaluator's ``delivery_times`` — so remaining relay deliveries stay
    comparable.  The reduced route is re-walked in visit order with the
    exact same accumulation order as
    :meth:`RouteEvaluator._evaluate_uncached`, then the reduced score is
    subtracted once — matching ``Score.__sub__`` bit-for-bit.
    """

    route = surface.route
    visit_nodes = surface.visit_nodes
    removed = set(positions)
    reduced_visits = tuple(
        visit
        for index, visit in enumerate(route)
        if index not in removed
    )
    reduced_nodes = tuple(
        node
        for index, node in enumerate(visit_nodes)
        if index not in removed
    )
    reduced_times: dict[int, float] = {}
    elapsed = 0.0
    total_distance = 0.0
    previous = route_start_node
    for visit, node in zip(reduced_visits, reduced_nodes):
        leg_km = distances[previous][node]
        total_distance += leg_km
        elapsed += leg_km / speed
        previous = node
        if visit < 0:
            visit_id = abs(visit)
            leg = registry.get(visit_id) if registry else None
            if leg is not None:
                if leg.kind == "RELAY_OUT":
                    reduced_times[leg.task_id] = elapsed
            else:
                reduced_times[visit_id] = elapsed

    reduced_late_count = 0
    reduced_total_lateness = 0.0
    for record in surface.deliveries:
        if record.position in removed:
            # A removed delivery event disappears from the reduced route.
            continue
        new_time = reduced_times.get(record.task_id)
        if new_time is None:
            continue
        lateness = max(0.0, new_time - record.deadline_min)
        if lateness > 1e-9:
            reduced_late_count += 1
        reduced_total_lateness += lateness
    return (
        surface.base_score.late_count - reduced_late_count,
        surface.base_score.total_lateness_min - reduced_total_lateness,
        surface.base_score.distance_km - total_distance,
    )


def _pair_removal_delta_exact(
    surface: RouteInsertionSurface,
    pickup_position: int,
    delivery_position: int,
    *,
    speed: float,
    distances,
    registry,
    route_start_node: int = 0,
) -> tuple[int, float, float]:
    """Single-pair removal gain (see :func:`_visits_removal_delta_exact`)."""

    return _visits_removal_delta_exact(
        surface,
        (pickup_position, delivery_position),
        speed=speed,
        distances=distances,
        registry=registry,
        route_start_node=route_start_node,
    )


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
    route_start_node: int = 0,
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

    Thin public wrapper around :func:`_pair_insertion_delta_raw`; the hot
    ranking loops call the raw primitive directly.
    """

    distances = problem._distances
    start_node = problem._node_for_visit(start_visit)
    end_node = problem._node_for_visit(end_visit)
    if delta_distance is None:
        delta_distance = _node_distance_delta(
            distances,
            surface.visit_nodes,
            start_node,
            end_node,
            pickup_position,
            delivery_position,
            route_start_node=route_start_node,
        )
    (
        delta_late_count,
        delta_lateness,
        distance_delta,
        start_time,
        end_time,
    ) = _pair_insertion_delta_raw(
        surface,
        start_node,
        end_node,
        pickup_position,
        delivery_position,
        is_delivery=is_delivery,
        deadline=(
            problem.task(task_id).deadline_min if is_delivery else None
        ),
        delta_distance=delta_distance,
        speed=problem.speed_km_per_min,
        distances=distances,
        counters=counters,
        route_start_node=route_start_node,
    )
    return InsertionDeltaResult(
        Score(delta_late_count, delta_lateness, distance_delta),
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
        route_start_node: int = 0,
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
            route_start_node=route_start_node,
        )


def _k_cheapest_positions(
    problem: Problem,
    surface: RouteInsertionSurface,
    start_node: int,
    end_node: int,
    limit: int,
    *,
    route_start_node: int = 0,
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
                    route_start_node=route_start_node,
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
                distances,
                visit_nodes,
                start_node,
                end_node,
                pickup,
                delivery,
                route_start_node=route_start_node,
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
        surface = evaluator.surface_cache.get(
            route, start_node=problem.home_node(route_index)
        )
    materialized = surface.route
    if (
        counts_toward_k
        and surface.delivery_event_count >= problem.max_tasks_per_drone
    ):
        return ()
    if event_cap is not None and len(materialized) + 2 > event_cap:
        return ()
    route_start_node = problem.home_node(route_index)
    if candidate_limit is not None and len(surface.positions) > candidate_limit:
        limit = max(2, candidate_limit)
        start_node = problem._node_for_visit(start_visit)
        end_node = problem._node_for_visit(end_visit)
        scored = _k_cheapest_positions(
            problem,
            surface,
            start_node,
            end_node,
            limit,
            route_start_node=route_start_node,
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
                    route_start_node=route_start_node,
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
        old_score = evaluator.evaluate(
            materialized, start_node=route_start_node
        ).score
        legacy_options = []
        for _, pickup_position, delivery_position in scored:
            candidate = insert_leg_pair(
                materialized,
                start_visit,
                end_visit,
                pickup_position,
                delivery_position,
            )
            delta = (
                evaluator.evaluate(
                    candidate, start_node=route_start_node
                ).score
                - old_score
            )
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
                option.delta.late_count,
                option.delta.total_lateness_min,
                option.delta.distance_km,
                option.route_index,
                option.pickup_position,
                option.delivery_position,
                option.route,
            )
        )
        return tuple(legacy_options[:option_count])
    counters = evaluator.counters
    speed = problem.speed_km_per_min
    deadline = problem.task(task_id).deadline_min if is_delivery else None
    ranked: list[tuple[int, float, float, int, int, float, float]] = []
    for delta_distance, pickup_position, delivery_position in scored:
        (
            delta_late_count,
            delta_lateness,
            distance_delta,
            start_time,
            end_time,
        ) = _pair_insertion_delta_raw(
            surface,
            start_node,
            end_node,
            pickup_position,
            delivery_position,
            is_delivery=is_delivery,
            deadline=deadline,
            delta_distance=delta_distance,
            speed=speed,
            distances=problem._distances,
            counters=counters,
            route_start_node=route_start_node,
        )
        ranked.append(
            (
                delta_late_count,
                delta_lateness,
                distance_delta,
                pickup_position,
                delivery_position,
                start_time,
                end_time,
            )
        )
    ranked.sort(
        key=lambda item: (item[0], item[1], item[2], item[3], item[4])
    )
    options: list[RouteInsertionOption] = []
    for (
        delta_late_count,
        delta_lateness,
        distance_delta,
        pickup_position,
        delivery_position,
        start_time,
        end_time,
    ) in ranked[:option_count]:
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
                Score(delta_late_count, delta_lateness, distance_delta),
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
        base_metrics = evaluator.evaluate(
            route, start_node=problem.home_node(route_index)
        )
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
                evaluator.evaluate(
                    option.route,
                    start_node=problem.home_node(route_index),
                ).score
                - base_metrics.score
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
