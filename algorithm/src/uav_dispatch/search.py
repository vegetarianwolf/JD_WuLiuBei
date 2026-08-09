"""Reusable route scoring and capacity-two pair insertion primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from math import isfinite
from types import MappingProxyType
from typing import Mapping, Sequence

from .model import Problem, Score, SolutionEvaluation
from .validation import evaluate_solution


Route = tuple[int, ...]
Routes = tuple[Route, ...]


def soft_deadline(problem: Problem, task_id: int, beta: float) -> float:
    """Return an immutable search-only deadline with service-time slack."""

    if not isfinite(beta) or beta < 0:
        raise ValueError("soft deadline beta 必须为有限非负数")
    return problem.task(task_id).deadline_min + beta * problem.direct_completion_min(
        task_id
    )


@dataclass(frozen=True, order=True, slots=True)
class SearchScore:
    """Search-only lexicographic guidance; never used as the official score."""

    late_count: int
    weighted_lateness_min: float
    distance_km: float


@dataclass(frozen=True, slots=True)
class RouteMetrics:
    score: Score
    weighted_lateness_min: float
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
        *,
        enable_search_score: bool = True,
    ) -> None:
        self.problem = problem
        self._deadline_priorities: Mapping[int, float] | None = None
        if enable_search_score:
            deadlines = tuple(task.deadline_min for task in problem.tasks)
            minimum_deadline = min(deadlines)
            maximum_deadline = max(deadlines)
            deadline_span = maximum_deadline - minimum_deadline
            self._deadline_priorities = MappingProxyType(
                {
                    task.id: (
                        1.0
                        if deadline_span <= 1e-12
                        else 1.0
                        + (maximum_deadline - task.deadline_min) / deadline_span
                    )
                    for task in problem.tasks
                }
            )

        @lru_cache(maxsize=cache_size)
        def cached(route: Route) -> RouteMetrics:
            return self._evaluate_uncached(route)

        self._cached = cached

        @lru_cache(maxsize=cache_size)
        def cached_guided(route: Route, beta: float) -> float:
            priorities = self.deadline_priorities
            metrics = self._cached(route)
            return sum(
                priorities[task_id]
                * max(
                    0.0,
                    delivered_at - soft_deadline(problem, task_id, beta),
                )
                for task_id, delivered_at in metrics.delivery_times_min.items()
            )

        self._cached_guided = cached_guided

    @property
    def search_score_enabled(self) -> bool:
        return self._deadline_priorities is not None

    @property
    def deadline_priorities(self) -> Mapping[int, float]:
        """Return search priorities, rejecting accidental use when disabled."""

        if self._deadline_priorities is None:
            raise RuntimeError("search score guidance is disabled")
        return self._deadline_priorities

    def evaluate(self, route: Sequence[int]) -> RouteMetrics:
        return self._cached(tuple(route))

    def guided_weighted_lateness(
        self, route: Sequence[int], soft_deadline_beta: float
    ) -> float:
        """Score every delivery against soft deadlines in a separate cache."""

        if not isfinite(soft_deadline_beta) or soft_deadline_beta < 0:
            raise ValueError("soft deadline beta 必须为有限非负数")
        if not self.search_score_enabled:
            raise RuntimeError("search score guidance is disabled")
        return self._cached_guided(tuple(route), soft_deadline_beta)

    def _evaluate_uncached(self, route: Route) -> RouteMetrics:
        seen_pickups: set[int] = set()
        seen_deliveries: set[int] = set()
        previous: int | None = None
        distance = 0.0
        elapsed = 0.0
        load = 0
        delivery_times: dict[int, float] = {}
        late_ids: list[int] = []
        total_lateness = 0.0
        weighted_lateness = 0.0
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
                    if self._deadline_priorities is not None:
                        weighted_lateness += (
                            self._deadline_priorities[task_id] * lateness
                        )
                    max_lateness = max(max_lateness, lateness)
        if load != 0 or seen_pickups != seen_deliveries:
            raise ValueError("路线必须包含完整的取送任务对")
        return RouteMetrics(
            score=Score(len(late_ids), total_lateness, distance),
            weighted_lateness_min=weighted_lateness,
            delivery_times_min=MappingProxyType(delivery_times),
            late_task_ids=tuple(late_ids),
            max_lateness_min=max_lateness,
        )


def routes_score(evaluator: RouteEvaluator, routes: Sequence[Sequence[int]]) -> Score:
    score = Score(0, 0.0, 0.0)
    for route in routes:
        score += evaluator.evaluate(route).score
    return score


def routes_search_score(
    evaluator: RouteEvaluator, routes: Sequence[Sequence[int]]
) -> SearchScore:
    """Aggregate the strict internal objective without changing official scoring."""

    if not evaluator.search_score_enabled:
        raise RuntimeError("search score guidance is disabled")
    late_count = 0
    weighted_lateness = 0.0
    distance = 0.0
    for route in routes:
        metrics = evaluator.evaluate(route)
        late_count += metrics.score.late_count
        weighted_lateness += metrics.weighted_lateness_min
        distance += metrics.score.distance_km
    return SearchScore(late_count, weighted_lateness, distance)


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
    weighted_lateness_delta: float = 0.0
    risk_aware_cost: float = 0.0

    def ranking_key(self, risk_aware: bool) -> tuple[object, ...]:
        if risk_aware:
            return (
                self.delta.late_count,
                self.risk_aware_cost,
                self.weighted_lateness_delta,
                self.delta.distance_km,
                self.route_index,
                self.pickup_position,
                self.delivery_position,
                self.route,
            )
        return (
            self.delta,
            self.route_index,
            self.pickup_position,
            self.delivery_position,
            self.route,
        )


def route_insertion_options(
    problem: Problem,
    evaluator: RouteEvaluator,
    route: Sequence[int],
    route_index: int,
    task_id: int,
    *,
    candidate_limit: int | None,
    option_count: int = 3,
    risk_aware: bool = False,
    soft_deadline_beta: float | None = None,
    lateness_lambda: float = 1.0,
) -> tuple[RouteInsertionOption, ...]:
    """Return the best exact deltas after capacity-profile candidate pruning."""

    materialized = tuple(route)
    if sum(visit > 0 for visit in materialized) >= problem.max_tasks_per_drone:
        return ()
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

    if not isfinite(lateness_lambda) or lateness_lambda < 0:
        raise ValueError("risk-aware lateness lambda 必须为有限非负数")
    guided_beta = soft_deadline_beta
    if (risk_aware or guided_beta is not None) and not evaluator.search_score_enabled:
        raise RuntimeError("search score guidance is disabled")
    old_metrics = evaluator.evaluate(materialized)
    old_score = old_metrics.score
    old_guided_weighted_lateness = (
        None
        if guided_beta is None
        else evaluator.guided_weighted_lateness(
            materialized, guided_beta
        )
    )
    options: list[RouteInsertionOption] = []
    for pickup_position, delivery_position in positions:
        candidate = insert_pair(
            materialized, task_id, pickup_position, delivery_position
        )
        candidate_metrics = evaluator.evaluate(candidate)
        delta = candidate_metrics.score - old_score
        weighted_lateness_delta = (
            candidate_metrics.weighted_lateness_min
            - old_metrics.weighted_lateness_min
        )
        if guided_beta is not None and old_guided_weighted_lateness is not None:
            weighted_lateness_delta = (
                evaluator.guided_weighted_lateness(
                    candidate, guided_beta
                )
                - old_guided_weighted_lateness
            )
        options.append(
            RouteInsertionOption(
                delta,
                route_index,
                pickup_position,
                delivery_position,
                candidate,
                weighted_lateness_delta,
                delta.distance_km
                + lateness_lambda * weighted_lateness_delta,
            )
        )
    options.sort(key=lambda option: option.ranking_key(risk_aware))
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
    problem: Problem,
    routes: Sequence[Sequence[int]],
    task_id: int,
    *,
    risk_aware: bool = False,
    soft_deadline_beta: float | None = None,
    lateness_lambda: float = 1.0,
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
    best_key: tuple[object, ...] | None = None
    best_choice: tuple[int, int, int, Route] | None = None
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
            risk_aware=risk_aware,
            soft_deadline_beta=soft_deadline_beta,
            lateness_lambda=lateness_lambda,
        ):
            candidate_score = base_score + option.delta
            key = (
                option.ranking_key(True)
                if risk_aware
                else (
                    candidate_score,
                    route_index,
                    option.pickup_position,
                    option.delivery_position,
                    option.route,
                )
            )
            if best_key is None or key < best_key:
                best_key = key
                best_choice = (
                    route_index,
                    option.pickup_position,
                    option.delivery_position,
                    option.route,
                )
    if best_choice is None:
        raise ValueError(f"任务 {task_id} 没有可用的路线槽位")

    route_index, pickup_position, delivery_position, new_route = best_choice
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
