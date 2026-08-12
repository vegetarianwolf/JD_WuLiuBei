"""Reusable route scoring and capacity-two pair insertion primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from typing import Mapping, Sequence

from .model import Problem, Score, SolutionEvaluation
from .validation import evaluate_solution


Route = tuple[int, ...]
Routes = tuple[Route, ...]


@dataclass(frozen=True, slots=True)
class RouteMetrics:
    score: Score
    delivery_times_min: Mapping[int, float]
    pickup_times_min: Mapping[int, float]
    late_task_ids: tuple[int, ...]
    total_lateness_min: float
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

    def __init__(self, problem: Problem, cache_size: int = 50_000) -> None:
        self.problem = problem

        @lru_cache(maxsize=cache_size)
        def cached(route: Route) -> RouteMetrics:
            return self._evaluate_uncached(route)

        self._cached = cached

    def evaluate(self, route: Sequence[int]) -> RouteMetrics:
        return self._cached(tuple(route))

    def _evaluate_uncached(self, route: Route) -> RouteMetrics:
        seen_pickups: set[int] = set()
        seen_deliveries: set[int] = set()
        previous: int | None = None
        distance = 0.0
        elapsed = 0.0
        load = 0
        pickup_times: dict[int, float] = {}
        delivery_times: dict[int, float] = {}
        late_ids: list[int] = []
        total_lateness = 0.0
        max_lateness = 0.0

        for visit in route:
            task_id = abs(visit)
            if task_id not in self.problem._task_by_id:
                raise ValueError(f"未知任务 {task_id}")
            leg = self.problem.distance(previous, visit)
            distance += leg
            elapsed += leg / self.problem.speed_km_per_min
            previous = visit
            if visit > 0:
                if task_id in seen_pickups:
                    raise ValueError(f"任务 {task_id} 重复取件")
                seen_pickups.add(task_id)
                pickup_times[task_id] = elapsed
                load += 1
                if load > self.problem.capacity:
                    raise ValueError("路线超过载荷上限")
            else:
                if task_id not in seen_pickups or task_id in seen_deliveries:
                    raise ValueError(f"任务 {task_id} 的取送顺序非法")
                seen_deliveries.add(task_id)
                load -= 1
                delivery_times[task_id] = elapsed
                lateness = max(
                    0.0, elapsed - self.problem.task(task_id).deadline_min
                )
                if lateness > 1e-9:
                    late_ids.append(task_id)
                    total_lateness += lateness
                    max_lateness = max(max_lateness, lateness)
        if load != 0 or seen_pickups != seen_deliveries:
            raise ValueError("路线必须包含完整的取送任务对")
        return RouteMetrics(
            score=Score(len(late_ids), total_lateness, distance),
            delivery_times_min=MappingProxyType(delivery_times),
            pickup_times_min=MappingProxyType(pickup_times),
            late_task_ids=tuple(late_ids),
            total_lateness_min=total_lateness,
            max_lateness_min=max_lateness,
        )


def routes_score(evaluator: RouteEvaluator, routes: Sequence[Sequence[int]]) -> Score:
    score = Score(0, 0.0)
    for route in routes:
        score += evaluator.evaluate(route).score
    return score


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
    ) -> float:
        route = self.route
        length = len(route)

        def edge(left: int | None, right: int | None) -> float:
            return 0.0 if right is None else problem.distance(left, right)

        before_pickup = None if pickup_position == 0 else route[pickup_position - 1]
        after_pickup = route[pickup_position] if pickup_position < length else None
        if delivery_position == pickup_position + 1:
            old = edge(before_pickup, after_pickup)
            new = (
                edge(before_pickup, task_id)
                + problem.distance(task_id, -task_id)
                + edge(-task_id, after_pickup)
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
            edge(before_pickup, task_id)
            + edge(task_id, after_pickup)
            + edge(before_delivery, -task_id)
            + edge(-task_id, after_delivery)
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


def route_insertion_options(
    problem: Problem,
    evaluator: RouteEvaluator,
    route: Sequence[int],
    route_index: int,
    task_id: int,
    *,
    candidate_limit: int | None,
    option_count: int = 3,
    profile_cache: "dict[Route, RouteProfile] | None" = None,
) -> tuple[RouteInsertionOption, ...]:
    """Return the best exact deltas after capacity-profile candidate pruning.

    ``profile_cache`` (optional) lets a caller reuse capacity profiles for
    unchanged routes across repeated insertion queries inside one repair
    pass — a significant speed-up for large instances.  It is a pure
    performance optimisation; the results are identical with or without it.
    """

    materialized = tuple(route)
    if sum(visit > 0 for visit in materialized) >= problem.max_tasks_per_drone:
        return ()
    if profile_cache is not None:
        profile = profile_cache.get(materialized)
        if profile is None:
            profile = RouteProfile.build(materialized)
            profile_cache[materialized] = profile
    else:
        profile = RouteProfile.build(materialized)
    length = len(materialized)
    positions = [
        (pickup, delivery)
        for pickup in range(length + 1)
        for delivery in range(pickup + 1, length + 2)
        if profile.can_insert(pickup, delivery, problem.capacity)
    ]
    if candidate_limit is not None and len(positions) > candidate_limit:
        limit = max(4, candidate_limit)
        by_distance = sorted(
            positions,
            key=lambda position: (
                profile.distance_delta(problem, task_id, *position),
                position,
            ),
        )
        by_early_delivery = sorted(
            positions,
            key=lambda position: (
                position[1],
                position[0],
                profile.distance_delta(problem, task_id, *position),
            ),
        )
        selected: list[tuple[int, int]] = []
        selected_set: set[tuple[int, int]] = set()
        for collection, quota in (
            (by_distance, max(1, limit // 2)),
            (by_early_delivery, max(1, limit // 3)),
        ):
            added = 0
            for position in collection:
                if position not in selected_set:
                    selected.append(position)
                    selected_set.add(position)
                    added += 1
                if added >= quota or len(selected) >= limit:
                    break
            if len(selected) >= limit:
                break
        for position in by_distance:
            if len(selected) >= limit:
                break
            if position not in selected_set:
                selected.append(position)
                selected_set.add(position)
        positions = selected[:limit]

    old_score = evaluator.evaluate(materialized).score
    options: list[RouteInsertionOption] = []
    for pickup_position, delivery_position in positions:
        candidate = insert_pair(
            materialized, task_id, pickup_position, delivery_position
        )
        delta = evaluator.evaluate(candidate).score - old_score
        options.append(
            RouteInsertionOption(
                delta,
                route_index,
                pickup_position,
                delivery_position,
                candidate,
            )
        )
    options.sort(
        key=lambda option: (
            option.delta,
            option.route_index,
            option.pickup_position,
            option.delivery_position,
            option.route,
        )
    )
    return tuple(options[:option_count])


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
        for option in route_insertion_options(
            problem,
            evaluator,
            route,
            route_index,
            task_id,
            candidate_limit=None,
            option_count=10_000,
        ):
            candidate_score = base_score + option.delta
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
