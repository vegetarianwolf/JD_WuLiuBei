"""Capacity-2 lexicographic hybrid adaptive large-neighbourhood search."""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import permutations
from math import exp, isfinite
from random import Random
from time import perf_counter
from types import MappingProxyType
from typing import Iterable, Sequence

from .hypergraph_destroy import hypergraph_destroy
from .model import Problem, Score
from .pair_repair import PairRepairDeadlineReached, pair_regret_repair
from .search import (
    Route,
    RouteEvaluator,
    RouteInsertionOption,
    Routes,
    SolverResult,
    route_insertion_options,
    routes_score,
)
from .validation import evaluate_solution


DESTROY_OPERATORS = (
    "random",
    "worst_distance",
    "worst_lex",
    "spatial_related",
    "deadline_related",
    "late_critical",
    "route_segment",
    "capacity_conflict",
    "assignment_destroy",
    "route_clear",
    "hypergraph_destroy",
)
REPAIR_OPERATORS = (
    "greedy",
    "regret2",
    "regret3",
    "deadline",
    "slack",
    "cluster_regret",
    "pair_regret",
    "pickup_urgency",
)


class _SearchDeadlineReached(RuntimeError):
    """Abort an incomplete destroy/repair iteration at the wall-clock limit."""


@dataclass(frozen=True, slots=True)
class ALNSConfig:
    max_iterations: int = 400
    time_limit_seconds: float | None = None
    seed: int = 20260805
    candidate_limit: int | None = 48
    min_destroy_fraction: float = 0.04
    max_destroy_fraction: float = 0.10
    weight_update_interval: int = 40
    reaction_factor: float = 0.2
    minimum_weight: float = 0.05
    initial_temperature: float = 0.03
    minimum_temperature: float = 0.0005
    cooling_rate: float = 0.995
    route_pool_interval: int = 50
    route_pool_node_limit: int = 5_000
    ejection_interval: int = 25
    ejection_trials: int = 24
    enable_route_pool: bool = False
    enable_ejection: bool = True
    enable_assignment_destroy: bool = True
    enable_deadline_risk: bool = True
    enable_vnd: bool = False
    vnd_max_moves: int = 2
    vnd_task_limit: int = 12
    vnd_swap_pair_limit: int = 24
    vnd_block_window_limit: int = 16
    enable_cluster_repair: bool = False
    cluster_bundle_candidate_limit: int = 12
    cluster_pair_limit: int = 6
    enable_pair_repair: bool = False
    pair_candidate_limit: int = 8
    enable_hypergraph_destroy: bool = False

    def __post_init__(self) -> None:
        if self.max_iterations < 0:
            raise ValueError("迭代次数不能为负")
        if self.time_limit_seconds is not None and (
            not isfinite(self.time_limit_seconds) or self.time_limit_seconds <= 0
        ):
            raise ValueError("时间上限必须为正数")
        if self.candidate_limit is not None and self.candidate_limit < 4:
            raise ValueError("候选位置上限至少为 4")
        if not (
            isfinite(self.min_destroy_fraction)
            and isfinite(self.max_destroy_fraction)
            and 0 < self.min_destroy_fraction <= self.max_destroy_fraction <= 1
        ):
            raise ValueError("破坏比例必须满足 0 < min <= max <= 1")
        if self.weight_update_interval <= 0:
            raise ValueError("权重更新周期必须为正整数")
        if not isfinite(self.reaction_factor) or not 0 < self.reaction_factor <= 1:
            raise ValueError("权重反应系数必须在 (0, 1] 内")
        if not isfinite(self.cooling_rate) or not 0 < self.cooling_rate <= 1:
            raise ValueError("降温率必须在 (0, 1] 内")
        if (
            not isfinite(self.minimum_weight)
            or self.minimum_weight <= 0
            or not isfinite(self.initial_temperature)
            or not isfinite(self.minimum_temperature)
            or self.initial_temperature <= 0
            or self.minimum_temperature <= 0
            or self.minimum_temperature > self.initial_temperature
        ):
            raise ValueError("温度和最小权重必须为有限正数")
        if self.route_pool_interval < 0 or self.ejection_interval < 0:
            raise ValueError("强化周期不能为负")
        if self.route_pool_node_limit < 0 or self.ejection_trials < 0:
            raise ValueError("强化搜索预算不能为负")
        if min(
            self.vnd_max_moves,
            self.vnd_task_limit,
            self.vnd_swap_pair_limit,
            self.vnd_block_window_limit,
            self.cluster_bundle_candidate_limit,
            self.cluster_pair_limit,
            self.pair_candidate_limit,
        ) < 0:
            raise ValueError("VND、聚类修复与成对修复搜索预算不能为负")


def _task_ids_in_routes(routes: Sequence[Sequence[int]]) -> tuple[int, ...]:
    return tuple(visit for route in routes for visit in route if visit > 0)


def _remove_tasks(routes: Sequence[Sequence[int]], task_ids: Iterable[int]) -> Routes:
    removed = set(task_ids)
    return tuple(
        tuple(visit for visit in route if abs(visit) not in removed)
        for route in routes
    )


def _task_route_index(routes: Sequence[Sequence[int]]) -> dict[int, int]:
    return {
        visit: route_index
        for route_index, route in enumerate(routes)
        for visit in route
        if visit > 0
    }


def _choose_ranked(
    ranked: Sequence[int], count: int, rng: Random, exponent: float = 4.0
) -> list[int]:
    available = list(ranked)
    chosen: list[int] = []
    while available and len(chosen) < count:
        index = int((rng.random() ** exponent) * len(available))
        chosen.append(available.pop(index))
    return chosen


def _deadline_risk(delivered_at: float, deadline: float) -> float:
    """Return monotonic urgency without changing the lexicographic score."""

    if deadline <= 1e-12:
        return float("inf") if delivered_at > 1e-12 else 1.0
    return delivered_at / deadline


from .physical_lower_bound import pickup_slack as _canonical_pickup_slack


def _pickup_slack(
    problem: Problem, task_id: int, route: Sequence[int], evaluator: RouteEvaluator | None = None
) -> float:
    """Pickup slack for *task_id* on *route*.

    Negative = pickup already too late for on-time delivery.
    Uses RouteEvaluator cache when available, otherwise walks the route.
    """
    if evaluator is not None:
        metrics = evaluator.evaluate(route)
        pickup_t = metrics.pickup_times_min.get(task_id, float("inf"))
        if pickup_t == float("inf"):
            return 0.0
        return _canonical_pickup_slack(problem, task_id, pickup_t)
    # Fallback: walk the route (no evaluator cache available)
    elapsed = 0.0
    prev: int | None = None
    for visit in route:
        elapsed += problem.distance(prev, visit) / problem.speed_km_per_min
        prev = visit
        if visit == task_id:
            return _canonical_pickup_slack(problem, task_id, elapsed)
    return 0.0


def _pickup_time(
    problem: Problem, route: Sequence[int], task_id: int, evaluator: RouteEvaluator | None = None
) -> float:
    """Pickup time for *task_id* on *route* (minutes elapsed)."""
    if evaluator is not None:
        metrics = evaluator.evaluate(route)
        return metrics.pickup_times_min.get(task_id, float("inf"))
    # Fallback
    elapsed = 0.0
    prev: int | None = None
    for visit in route:
        elapsed += problem.distance(prev, visit) / problem.speed_km_per_min
        prev = visit
        if visit == task_id:
            return elapsed
    return float("inf")


def _score_strictly_better(
    candidate: Score,
    incumbent: Score,
    *,
    tolerance: float = 1e-9,
) -> bool:
    """Compare local moves lexicographically while ignoring round-off noise.

    The formal objective is the two-layer tuple ``(late_count, distance_km)``.
    """

    if not candidate < incumbent:
        return False
    if candidate.late_count != incumbent.late_count:
        return True
    return candidate.distance_km < incumbent.distance_km - tolerance


def destroy_solution(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    operator: str,
    count: int,
    rng: Random,
    *,
    use_deadline_risk: bool = True,
) -> tuple[Routes, tuple[int, ...]]:
    """Remove complete task pairs using one named destroy operator."""

    task_ids = list(_task_ids_in_routes(routes))
    count = min(max(1, count), len(task_ids))
    route_by_task = _task_route_index(routes)

    if operator == "hypergraph_destroy":
        return hypergraph_destroy(
            problem,
            evaluator,
            routes,
            count,
            rng,
        )
    if operator == "random":
        removed = rng.sample(task_ids, count)
    elif operator == "assignment_destroy":
        non_empty_indices = [
            route_index for route_index, route in enumerate(routes) if route
        ]
        route_metrics = {
            route_index: evaluator.evaluate(routes[route_index])
            for route_index in non_empty_indices
        }

        def task_priority(task_id: int) -> tuple[float, ...]:
            route_index = route_by_task[task_id]
            route = routes[route_index]
            metrics = route_metrics[route_index]
            reduced = tuple(visit for visit in route if abs(visit) != task_id)
            gain = metrics.score - evaluator.evaluate(reduced).score
            delivered_at = metrics.delivery_times_min[task_id]
            deadline = problem.task(task_id).deadline_min
            lateness = max(0.0, delivered_at - deadline)
            risk = (
                _deadline_risk(delivered_at, deadline)
                if use_deadline_risk
                else 0.0
            )
            utilization_gap = 1.0 - (
                sum(visit > 0 for visit in route)
                / problem.max_tasks_per_drone
            )
            return (
                float(gain.late_count),
                lateness,
                risk,
                gain.distance_km,
                utilization_gap,
                -deadline,
                -task_id,
            )

        def route_priority(route_index: int) -> tuple[float, ...]:
            route = routes[route_index]
            metrics = route_metrics[route_index]
            route_tasks = tuple(visit for visit in route if visit > 0)
            max_risk = max(
                (
                    _deadline_risk(
                        metrics.delivery_times_min[task_id],
                        problem.task(task_id).deadline_min,
                    )
                    for task_id in route_tasks
                ),
                default=0.0,
            )
            if not use_deadline_risk:
                max_risk = 0.0
            utilization_gap = 1.0 - len(route_tasks) / problem.max_tasks_per_drone
            return (
                float(metrics.score.late_count),
                max_risk,
                utilization_gap,
                metrics.score.distance_km / max(1, len(route_tasks)),
                -route_index,
            )

        ranked_routes = sorted(
            non_empty_indices,
            key=route_priority,
            reverse=True,
        )
        # A full fleet can change assignment only when at least two UAVs expose
        # a free task slot.  Spread the mandatory removals before filling the
        # rest from the most inefficient selected routes.
        route_count = min(
            len(ranked_routes),
            count,
            max(2, round(count ** 0.5)),
        )
        selected_routes = _choose_ranked(
            ranked_routes, route_count, rng, exponent=2.5
        )
        removed = []
        for route_index in selected_routes:
            local_ranked = sorted(
                (visit for visit in routes[route_index] if visit > 0),
                key=task_priority,
                reverse=True,
            )
            removed.extend(_choose_ranked(local_ranked, 1, rng, exponent=3.0))

        selected_pool = sorted(
            (
                task_id
                for route_index in selected_routes
                for task_id in routes[route_index]
                if task_id > 0 and task_id not in removed
            ),
            key=task_priority,
            reverse=True,
        )
        if len(selected_pool) < count - len(removed):
            selected_pool.extend(
                sorted(
                    (
                        task_id
                        for task_id in task_ids
                        if task_id not in removed
                        and task_id not in selected_pool
                    ),
                    key=task_priority,
                    reverse=True,
                )
            )
        removed.extend(
            _choose_ranked(
                selected_pool,
                count - len(removed),
                rng,
                exponent=3.0,
            )
        )
    elif operator in {"worst_distance", "worst_lex"}:
        ranked: list[tuple[Score | float, int]] = []
        for task_id in task_ids:
            route_index = route_by_task[task_id]
            route = routes[route_index]
            reduced = tuple(visit for visit in route if abs(visit) != task_id)
            old_score = evaluator.evaluate(route).score
            new_score = evaluator.evaluate(reduced).score
            gain = old_score - new_score
            key: Score | float = gain if operator == "worst_lex" else gain.distance_km
            ranked.append((key, task_id))
        ranked.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        removed = _choose_ranked([item[1] for item in ranked], count, rng)
    elif operator in {"spatial_related", "deadline_related"}:
        seed = rng.choice(task_ids)
        seed_task = problem.task(seed)

        def relatedness(task_id: int) -> tuple[float, int]:
            task = problem.task(task_id)
            deadline_gap = abs(task.deadline_min - seed_task.deadline_min)
            spatial_gap = task.pickup.distance_to(seed_task.pickup) + task.delivery.distance_to(
                seed_task.delivery
            )
            same_route_bonus = -2.0 if route_by_task[task_id] == route_by_task[seed] else 0.0
            if operator == "deadline_related":
                value = deadline_gap + 0.15 * spatial_gap + same_route_bonus
            else:
                value = spatial_gap + 0.15 * deadline_gap + same_route_bonus
            return value, task_id

        ranked_ids = [task_id for _, task_id in sorted(map(relatedness, task_ids))]
        removed = _choose_ranked(ranked_ids, count, rng, exponent=2.5)
    elif operator == "late_critical":
        urgency: list[tuple[float, int]] = []
        for route in routes:
            metrics = evaluator.evaluate(route)
            for task_id, delivered_at in metrics.delivery_times_min.items():
                deadline = problem.task(task_id).deadline_min
                if use_deadline_risk:
                    urgency.append(
                        (-_deadline_risk(delivered_at, deadline), task_id)
                    )
                else:
                    urgency.append((deadline - delivered_at, task_id))
        urgency.sort()
        removed = _choose_ranked([item[1] for item in urgency], count, rng, 3.0)
    elif operator == "route_segment":
        non_empty = [route for route in routes if route]
        route = rng.choice(non_empty)
        start = rng.randrange(len(route))
        ordered = []
        for offset in range(len(route)):
            task_id = abs(route[(start + offset) % len(route)])
            if task_id not in ordered:
                ordered.append(task_id)
            if len(ordered) == count:
                break
        remaining = [task for task in task_ids if task not in ordered]
        rng.shuffle(remaining)
        removed = (ordered + remaining)[:count]
    elif operator == "capacity_conflict":
        conflicts: list[tuple[int, int]] = []
        for route in routes:
            load = 0
            full_positions: set[int] = set()
            positions: dict[int, list[int]] = {}
            for index, visit in enumerate(route):
                load += 1 if visit > 0 else -1
                positions.setdefault(abs(visit), []).append(index)
                if load == problem.capacity:
                    full_positions.add(index)
            for task_id, interval in positions.items():
                overlap = sum(interval[0] <= pos < interval[-1] for pos in full_positions)
                conflicts.append((overlap, task_id))
        conflicts.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        removed = _choose_ranked([item[1] for item in conflicts], count, rng)
    elif operator == "route_clear":
        candidates = [route for route in routes if route]
        route = rng.choice(candidates)
        local = list(dict.fromkeys(abs(visit) for visit in route))
        rng.shuffle(local)
        remaining = [task for task in task_ids if task not in local]
        rng.shuffle(remaining)
        removed = (local + remaining)[:count]
    else:
        raise ValueError(f"未知破坏算子 {operator}")

    unique_removed = tuple(dict.fromkeys(removed))
    return _remove_tasks(routes, unique_removed), unique_removed


def _all_options_for_task(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    task_id: int,
    candidate_limit: int | None,
    option_count: int,
    cache: dict[tuple[int, int, Route, int | None, int], tuple[RouteInsertionOption, ...]],
    deadline: float | None = None,
) -> tuple[RouteInsertionOption, ...]:
    options: list[RouteInsertionOption] = []
    for route_index, route in enumerate(routes):
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        key = (task_id, route_index, route, candidate_limit, option_count)
        route_options = cache.get(key)
        if route_options is None:
            route_options = route_insertion_options(
                problem,
                evaluator,
                route,
                route_index,
                task_id,
                candidate_limit=candidate_limit,
                option_count=option_count,
            )
            cache[key] = route_options
        options.extend(route_options)
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


@dataclass(frozen=True, slots=True)
class _BundleInsertionOption:
    delta: Score
    route_index: int
    route: Route
    task_ids: tuple[int, int]


def _bundle_insertion_options(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    task_ids: tuple[int, int],
    *,
    candidate_limit: int | None,
    bundle_candidate_limit: int,
    deadline: float | None,
) -> tuple[_BundleInsertionOption, ...]:
    local_limit = (
        bundle_candidate_limit
        if candidate_limit is None
        else min(candidate_limit, bundle_candidate_limit)
    )
    beam_width = max(2, min(6, bundle_candidate_limit))
    options: dict[tuple[int, Route], _BundleInsertionOption] = {}
    for route_index, route in enumerate(routes):
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        task_count = sum(visit > 0 for visit in route)
        if task_count + 2 > problem.max_tasks_per_drone:
            continue
        old_score = evaluator.evaluate(route).score
        for first_task, second_task in (task_ids, task_ids[::-1]):
            # Keep every position candidate for the first task so that a
            # jointly-good, non-greedy first position is never pruned; only
            # the second insertion keeps a bounded beam.
            first_options = route_insertion_options(
                problem,
                evaluator,
                route,
                route_index,
                first_task,
                candidate_limit=local_limit,
                option_count=None,
            )
            for first in first_options:
                if deadline is not None and perf_counter() >= deadline:
                    raise _SearchDeadlineReached
                second_options = route_insertion_options(
                    problem,
                    evaluator,
                    first.route,
                    route_index,
                    second_task,
                    candidate_limit=local_limit,
                    option_count=beam_width,
                )
                for second in second_options:
                    option = _BundleInsertionOption(
                        evaluator.evaluate(second.route).score - old_score,
                        route_index,
                        second.route,
                        tuple(sorted(task_ids)),
                    )
                    key = (route_index, second.route)
                    existing = options.get(key)
                    if existing is None or option.delta < existing.delta:
                        options[key] = option
    return tuple(
        sorted(
            options.values(),
            key=lambda option: (
                option.delta,
                option.route_index,
                option.route,
            ),
        )
    )


def cluster_regret_repair(
    problem: Problem,
    evaluator: RouteEvaluator,
    partial_routes: Routes,
    removed_task_ids: Iterable[int],
    *,
    candidate_limit: int | None,
    original_route_by_task: dict[int, int] | None = None,
    use_deadline_risk: bool = True,
    bundle_candidate_limit: int = 12,
    pair_limit: int = 6,
    deadline: float | None = None,
) -> Routes:
    """Repair related task pairs jointly with a bounded bundle-regret rule."""

    routes = tuple(tuple(route) for route in partial_routes)
    remaining = set(removed_task_ids)
    original_routes = original_route_by_task or {}

    def relatedness(left_task: int, right_task: int) -> tuple[float, int]:
        left = problem.task(left_task)
        right = problem.task(right_task)
        same_route_bonus = (
            -2.0
            if left_task in original_routes
            and right_task in original_routes
            and original_routes[left_task] == original_routes[right_task]
            else 0.0
        )
        value = (
            left.pickup.distance_to(right.pickup)
            + 0.5 * left.delivery.distance_to(right.delivery)
            + 0.15 * abs(left.deadline_min - right.deadline_min)
            + same_route_bonus
        )
        return value, right_task

    while len(remaining) >= 2:
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        candidate_pairs: set[tuple[int, int]] = set()
        for task_id in sorted(remaining):
            partner = min(
                (other for other in remaining if other != task_id),
                key=lambda other: relatedness(task_id, other),
            )
            candidate_pairs.add(tuple(sorted((task_id, partner))))
        ranked_pairs = sorted(
            candidate_pairs,
            key=lambda pair: (relatedness(*pair), pair),
        )[:pair_limit]

        choices: list[
            tuple[tuple[float, ...], _BundleInsertionOption]
        ] = []
        for pair in ranked_pairs:
            options = _bundle_insertion_options(
                problem,
                evaluator,
                routes,
                pair,
                candidate_limit=candidate_limit,
                bundle_candidate_limit=bundle_candidate_limit,
                deadline=deadline,
            )
            if not options:
                continue
            best = options[0]
            alternative = next(
                (
                    option
                    for option in options[1:]
                    if option.route_index != best.route_index
                ),
                None,
            )
            if alternative is None:
                regret = (float("inf"), float("inf"))
            else:
                alternative_delta = alternative.delta
                regret = (
                    float(
                        alternative_delta.late_count - best.delta.late_count
                    ),
                    alternative_delta.distance_km - best.delta.distance_km,
                )
            metrics = evaluator.evaluate(best.route)
            risk = max(
                (
                    _deadline_risk(
                        metrics.delivery_times_min[task_id],
                        problem.task(task_id).deadline_min,
                    )
                    for task_id in pair
                ),
                default=0.0,
            )
            choices.append(
                (
                    (
                        regret[0],
                        risk if use_deadline_risk else 0.0,
                        regret[1],
                        -relatedness(*pair)[0],
                        -float(pair[0]),
                        -float(pair[1]),
                    ),
                    best,
                )
            )
        if not choices:
            break
        _, chosen = max(choices, key=lambda item: item[0])
        mutable = list(routes)
        mutable[chosen.route_index] = chosen.route
        routes = tuple(mutable)
        remaining.difference_update(chosen.task_ids)

    if remaining:
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        routes = _repair(
            problem,
            evaluator,
            routes,
            remaining,
            "regret2",
            candidate_limit,
            use_deadline_risk=use_deadline_risk,
            deadline=deadline,
        )
    return routes


def _repair(
    problem: Problem,
    evaluator: RouteEvaluator,
    partial_routes: Routes,
    removed_task_ids: Iterable[int],
    strategy: str,
    candidate_limit: int | None,
    use_deadline_risk: bool = False,
    original_route_by_task: dict[int, int] | None = None,
    cluster_bundle_candidate_limit: int = 12,
    cluster_pair_limit: int = 6,
    pair_candidate_limit: int = 8,
    deadline: float | None = None,
) -> Routes:
    if strategy == "cluster_regret":
        return cluster_regret_repair(
            problem,
            evaluator,
            partial_routes,
            removed_task_ids,
            candidate_limit=candidate_limit,
            original_route_by_task=original_route_by_task,
            use_deadline_risk=use_deadline_risk,
            bundle_candidate_limit=cluster_bundle_candidate_limit,
            pair_limit=cluster_pair_limit,
            deadline=deadline,
        )
    if strategy == "pair_regret":
        return pair_regret_repair(
            problem,
            evaluator,
            partial_routes,
            removed_task_ids,
            candidate_limit=candidate_limit,
            pair_candidate_limit=pair_candidate_limit,
            deadline=deadline,
        )
    routes = tuple(tuple(route) for route in partial_routes)
    remaining = set(removed_task_ids)
    cache: dict[
        tuple[int, int, Route, int | None, int], tuple[RouteInsertionOption, ...]
    ] = {}
    regret_k = 3 if strategy == "regret3" else 2

    while remaining:
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        option_count = regret_k if strategy.startswith("regret") else 1
        per_task: dict[int, tuple[RouteInsertionOption, ...]] = {}
        for task_id in sorted(remaining):
            options = _all_options_for_task(
                problem,
                evaluator,
                routes,
                task_id,
                candidate_limit,
                option_count,
                cache,
                deadline,
            )
            if not options:
                raise RuntimeError(f"修复阶段无法重新插入任务 {task_id}")
            per_task[task_id] = options

        def predicted_risk(task_id: int) -> float:
            if not use_deadline_risk:
                return 0.0
            choice = per_task[task_id][0]
            delivered_at = evaluator.evaluate(choice.route).delivery_times_min[
                task_id
            ]
            return _deadline_risk(
                delivered_at,
                problem.task(task_id).deadline_min,
            )

        def risk_guided_min(key):
            shortlist = sorted(remaining, key=key)[:3]
            return min(
                shortlist,
                key=lambda task_id: (-predicted_risk(task_id), key(task_id)),
            )

        if strategy == "greedy":
            def greedy_key(task_id: int):
                return (
                    per_task[task_id][0].delta,
                    problem.task(task_id).deadline_min,
                    task_id,
                )

            chosen_task = min(remaining, key=greedy_key)
        elif strategy in {"deadline", "slack"}:
            if strategy == "deadline":
                order_key = lambda task_id: (
                    problem.task(task_id).deadline_min,
                    task_id,
                )
            else:
                order_key = lambda task_id: (
                    problem.task(task_id).deadline_min
                    - problem.direct_completion_min(task_id),
                    task_id,
                )
            chosen_task = (
                risk_guided_min(order_key)
                if use_deadline_risk
                else min(remaining, key=order_key)
            )
        elif strategy in {"regret2", "regret3"}:

            def regret_key(
                task_id: int,
            ) -> tuple[float, float, float, int]:
                options = per_task[task_id]
                if len(options) < regret_k:
                    regret = (float("inf"), float("inf"))
                else:
                    best = options[0].delta
                    alternative = options[regret_k - 1].delta
                    regret = (
                        float(alternative.late_count - best.late_count),
                        alternative.distance_km - best.distance_km,
                    )
                # pickup_slack < 0 → pickup already too late; boost priority.
                slack = _pickup_slack(problem, task_id, options[0].route, evaluator)
                return (
                    regret[0],           # late_count regret
                    max(0.0, -slack),    # pickup urgency (0 = on-time, >0 = late)
                    regret[1],           # distance_km regret
                    -task_id,
                )

            chosen_task = max(remaining, key=regret_key)
        elif strategy == "pickup_urgency":
            order_key = lambda task_id: (
                _pickup_slack(problem, task_id, per_task[task_id][0].route, evaluator),
                task_id,
            )
            chosen_task = min(remaining, key=order_key)
        else:
            raise ValueError(f"未知修复算子 {strategy}")

        choice = per_task[chosen_task][0]
        mutable = list(routes)
        mutable[choice.route_index] = choice.route
        routes = tuple(mutable)
        remaining.remove(chosen_task)
    return routes


def construct_regret_initial(
    problem: Problem, *, candidate_limit: int | None = 48
) -> SolverResult:
    """Construct a complete deterministic regret-2 initial solution."""

    started = perf_counter()
    evaluator = RouteEvaluator(problem)
    routes: Routes = tuple(() for _ in range(problem.drone_count))
    routes = _repair(
        problem,
        evaluator,
        routes,
        problem.task_ids,
        "regret2",
        candidate_limit,
    )
    evaluation = evaluate_solution(problem, routes)
    if not evaluation.valid:
        raise RuntimeError(f"初始解非法: {evaluation.violations}")
    return SolverResult(
        routes,
        evaluation,
        runtime_seconds=perf_counter() - started,
        metadata=MappingProxyType({"method": "regret2"}),
    )


def _finish_constructor(
    problem: Problem, routes: Routes, started: float, method: str
) -> SolverResult:
    evaluation = evaluate_solution(problem, routes)
    if not evaluation.valid:
        raise RuntimeError(f"{method} 初始解非法: {evaluation.violations}")
    return SolverResult(
        routes,
        evaluation,
        runtime_seconds=perf_counter() - started,
        metadata=MappingProxyType({"method": method}),
    )


def construct_edd_adjacent(problem: Problem) -> SolverResult:
    """Earliest-deadline-first baseline with adjacent pickup-delivery pairs."""

    started = perf_counter()
    evaluator = RouteEvaluator(problem)
    routes: Routes = tuple(() for _ in range(problem.drone_count))
    for task in sorted(problem.tasks, key=lambda item: (item.deadline_min, item.id)):
        choices: list[tuple[Score, int, Route]] = []
        for route_index, route in enumerate(routes):
            if sum(visit > 0 for visit in route) >= problem.max_tasks_per_drone:
                continue
            candidate = route + (task.id, -task.id)
            delta = evaluator.evaluate(candidate).score - evaluator.evaluate(route).score
            choices.append((delta, route_index, candidate))
        if not choices:
            raise RuntimeError(f"任务 {task.id} 没有可用路线槽位")
        _, route_index, candidate = min(choices)
        mutable = list(routes)
        mutable[route_index] = candidate
        routes = tuple(mutable)
    return _finish_constructor(problem, routes, started, "edd-adjacent")


def construct_nearest_adjacent(problem: Problem) -> SolverResult:
    """Distance-nearest adjacent-pair baseline."""

    started = perf_counter()
    evaluator = RouteEvaluator(problem)
    routes: Routes = tuple(() for _ in range(problem.drone_count))
    remaining = set(problem.task_ids)
    while remaining:
        choices: list[tuple[float, float, int, int, Route]] = []
        for task_id in sorted(remaining):
            for route_index, route in enumerate(routes):
                if sum(visit > 0 for visit in route) >= problem.max_tasks_per_drone:
                    continue
                candidate = route + (task_id, -task_id)
                delta = evaluator.evaluate(candidate).score - evaluator.evaluate(route).score
                choices.append(
                    (
                        delta.distance_km,
                        problem.task(task_id).deadline_min,
                        task_id,
                        route_index,
                        candidate,
                    )
                )
        if not choices:
            raise RuntimeError("最近邻构造没有可用路线槽位")
        _, _, task_id, route_index, candidate = min(choices)
        mutable = list(routes)
        mutable[route_index] = candidate
        routes = tuple(mutable)
        remaining.remove(task_id)
    return _finish_constructor(problem, routes, started, "nearest-adjacent")


def construct_greedy_initial(
    problem: Problem, *, candidate_limit: int | None = 48
) -> SolverResult:
    """EDD-ordered full-position lexicographic greedy insertion baseline."""

    started = perf_counter()
    evaluator = RouteEvaluator(problem)
    routes: Routes = tuple(() for _ in range(problem.drone_count))
    cache: dict[
        tuple[int, int, Route, int | None, int], tuple[RouteInsertionOption, ...]
    ] = {}
    for task in sorted(problem.tasks, key=lambda item: (item.deadline_min, item.id)):
        choices = _all_options_for_task(
            problem,
            evaluator,
            routes,
            task.id,
            candidate_limit,
            1,
            cache,
        )
        if not choices:
            raise RuntimeError(f"任务 {task.id} 没有可用插入位置")
        choice = choices[0]
        mutable = list(routes)
        mutable[choice.route_index] = choice.route
        routes = tuple(mutable)
    return _finish_constructor(problem, routes, started, "greedy-full-position")


@dataclass(frozen=True, slots=True)
class _RouteColumn:
    tasks: frozenset[int]
    route: Route
    score: Score


class _RoutePool:
    """Restricted set-partitioning reinforcement solved by bounded DFS."""

    def __init__(self, problem: Problem, evaluator: RouteEvaluator) -> None:
        self.problem = problem
        self.evaluator = evaluator
        self.columns: dict[frozenset[int], _RouteColumn] = {}
        self.last_nodes = 0

    def add(self, routes: Routes) -> None:
        for route in routes:
            tasks = frozenset(visit for visit in route if visit > 0)
            if not tasks:
                continue
            column = _RouteColumn(tasks, route, self.evaluator.evaluate(route).score)
            existing = self.columns.get(tasks)
            if existing is None or (column.score, column.route) < (
                existing.score,
                existing.route,
            ):
                self.columns[tasks] = column

    def recombine(
        self,
        incumbent: Routes,
        node_limit: int,
        deadline: float | None = None,
    ) -> Routes:
        self.add(incumbent)
        if deadline is not None and perf_counter() >= deadline:
            return incumbent
        columns = tuple(
            sorted(self.columns.values(), key=lambda col: (col.score, col.route))
        )
        by_task: dict[int, list[_RouteColumn]] = {
            task_id: [] for task_id in self.problem.task_ids
        }
        for column in columns:
            for task_id in column.tasks:
                by_task[task_id].append(column)

        incumbent_score = routes_score(self.evaluator, incumbent)
        best_score = incumbent_score
        best_routes = incumbent
        nodes = 0

        def dfs(
            uncovered: frozenset[int],
            selected: tuple[_RouteColumn, ...],
            partial_score: Score,
        ) -> None:
            nonlocal best_score, best_routes, nodes
            if nodes >= node_limit or (
                deadline is not None and perf_counter() >= deadline
            ):
                return
            nodes += 1
            if not uncovered:
                ordered = tuple(
                    column.route
                    for column in sorted(
                        selected,
                        key=lambda col: (min(col.tasks), col.route),
                    )
                )
                candidate = ordered + tuple(
                    () for _ in range(self.problem.drone_count - len(ordered))
                )
                if (partial_score, candidate) < (best_score, best_routes):
                    best_score = partial_score
                    best_routes = candidate
                return
            if len(selected) >= self.problem.drone_count or partial_score >= best_score:
                return

            task_id = min(
                uncovered,
                key=lambda task: sum(
                    column.tasks <= uncovered for column in by_task[task]
                ),
            )
            compatible = [
                column
                for column in by_task[task_id]
                if column.tasks <= uncovered
            ]
            for column in compatible:
                dfs(
                    uncovered - column.tasks,
                    selected + (column,),
                    partial_score + column.score,
                )
                if nodes >= node_limit or (
                    deadline is not None and perf_counter() >= deadline
                ):
                    break

        dfs(frozenset(self.problem.task_ids), (), Score(0, 0.0))
        self.last_nodes = nodes
        return best_routes


def _local_search_task_order(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    use_deadline_risk: bool,
) -> tuple[int, ...]:
    priorities: list[tuple[tuple[float, ...], int]] = []
    for route in routes:
        metrics = evaluator.evaluate(route)
        for task_id, delivered_at in metrics.delivery_times_min.items():
            deadline = problem.task(task_id).deadline_min
            reduced = tuple(visit for visit in route if abs(visit) != task_id)
            distance_gain = (
                metrics.score.distance_km
                - evaluator.evaluate(reduced).score.distance_km
            )
            priorities.append(
                (
                    (
                        _deadline_risk(delivered_at, deadline)
                        if use_deadline_risk
                        else 0.0,
                        max(0.0, delivered_at - deadline),
                        distance_gain,
                        -deadline,
                        -task_id,
                    ),
                    task_id,
                )
            )
    priorities.sort(reverse=True)
    return tuple(task_id for _, task_id in priorities)


def inter_uav_relocate_once(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    candidate_limit: int | None,
    use_deadline_risk: bool = True,
    task_limit: int | None = 12,
    deadline: float | None = None,
) -> Routes:
    """Return the best bounded strict-improving one-pair inter-UAV relocate."""

    materialized = tuple(tuple(route) for route in routes)
    if all(
        sum(visit > 0 for visit in route) >= problem.max_tasks_per_drone
        for route in materialized
    ):
        return materialized
    current_score = routes_score(evaluator, materialized)
    best_score = current_score
    best_routes = materialized
    route_by_task = _task_route_index(materialized)
    ordered_tasks = _local_search_task_order(
        problem,
        evaluator,
        materialized,
        use_deadline_risk=use_deadline_risk,
    )
    if task_limit is not None:
        ordered_tasks = ordered_tasks[:task_limit]

    for task_id in ordered_tasks:
        if deadline is not None and perf_counter() >= deadline:
            return best_routes
        source_index = route_by_task[task_id]
        source = tuple(
            visit
            for visit in materialized[source_index]
            if abs(visit) != task_id
        )
        for target_index, target in enumerate(materialized):
            if target_index == source_index:
                continue
            if sum(visit > 0 for visit in target) >= problem.max_tasks_per_drone:
                continue
            if deadline is not None and perf_counter() >= deadline:
                return best_routes
            options = route_insertion_options(
                problem,
                evaluator,
                target,
                target_index,
                task_id,
                candidate_limit=candidate_limit,
                option_count=1,
            )
            if not options:
                continue
            candidate = list(materialized)
            candidate[source_index] = source
            candidate[target_index] = options[0].route
            candidate_routes = tuple(candidate)
            candidate_score = routes_score(evaluator, candidate_routes)
            if _score_strictly_better(candidate_score, best_score):
                best_score = candidate_score
                best_routes = candidate_routes
    return best_routes


def inter_uav_swap_once(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    candidate_limit: int | None,
    use_deadline_risk: bool = True,
    task_limit: int | None = 12,
    pair_limit: int | None = 24,
    deadline: float | None = None,
) -> Routes:
    """Return the best bounded strict-improving exchange of two UAV task pairs."""

    materialized = tuple(tuple(route) for route in routes)
    current_score = routes_score(evaluator, materialized)
    best_score = current_score
    best_routes = materialized
    route_by_task = _task_route_index(materialized)
    ordered_tasks = _local_search_task_order(
        problem,
        evaluator,
        materialized,
        use_deadline_risk=use_deadline_risk,
    )
    if task_limit is not None:
        ordered_tasks = ordered_tasks[:task_limit]

    evaluated_pairs = 0
    for left_position, left_task in enumerate(ordered_tasks):
        left_route_index = route_by_task[left_task]
        for right_task in ordered_tasks[left_position + 1 :]:
            right_route_index = route_by_task[right_task]
            if left_route_index == right_route_index:
                continue
            if pair_limit is not None and evaluated_pairs >= pair_limit:
                return best_routes
            if deadline is not None and perf_counter() >= deadline:
                return best_routes
            evaluated_pairs += 1
            left_reduced = tuple(
                visit
                for visit in materialized[left_route_index]
                if abs(visit) != left_task
            )
            right_reduced = tuple(
                visit
                for visit in materialized[right_route_index]
                if abs(visit) != right_task
            )
            right_into_left = route_insertion_options(
                problem,
                evaluator,
                left_reduced,
                left_route_index,
                right_task,
                candidate_limit=candidate_limit,
                option_count=1,
            )
            left_into_right = route_insertion_options(
                problem,
                evaluator,
                right_reduced,
                right_route_index,
                left_task,
                candidate_limit=candidate_limit,
                option_count=1,
            )
            if not right_into_left or not left_into_right:
                continue
            candidate = list(materialized)
            candidate[left_route_index] = right_into_left[0].route
            candidate[right_route_index] = left_into_right[0].route
            candidate_routes = tuple(candidate)
            candidate_score = routes_score(evaluator, candidate_routes)
            if _score_strictly_better(candidate_score, best_score):
                best_score = candidate_score
                best_routes = candidate_routes
    return best_routes


def intra_route_block_improve_once(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    use_deadline_risk: bool = True,
    window_limit: int | None = 16,
    deadline: float | None = None,
) -> Routes:
    """Reorder bounded capacity-2 PPDD blocks while preserving pair precedence."""

    materialized = tuple(tuple(route) for route in routes)
    if problem.capacity != 2:
        return materialized
    current_score = routes_score(evaluator, materialized)
    best_score = current_score
    best_routes = materialized
    ranked_windows: list[tuple[tuple[float, ...], int, int]] = []
    for route_index, route in enumerate(materialized):
        metrics = evaluator.evaluate(route)
        for start in range(max(0, len(route) - 3)):
            block = route[start : start + 4]
            if tuple(visit > 0 for visit in block) != (
                True,
                True,
                False,
                False,
            ):
                continue
            pickups = {visit for visit in block if visit > 0}
            deliveries = {abs(visit) for visit in block if visit < 0}
            if pickups != deliveries or len(pickups) != 2:
                continue
            risk = max(
                (
                    _deadline_risk(
                        metrics.delivery_times_min[task_id],
                        problem.task(task_id).deadline_min,
                    )
                    for task_id in pickups
                ),
                default=0.0,
            )
            ranked_windows.append(
                (
                    (
                        risk if use_deadline_risk else 0.0,
                        -float(route_index),
                        -float(start),
                    ),
                    route_index,
                    start,
                )
            )
    ranked_windows.sort(reverse=True)
    if window_limit is not None:
        ranked_windows = ranked_windows[:window_limit]

    for _, route_index, start in ranked_windows:
        if deadline is not None and perf_counter() >= deadline:
            return best_routes
        route = materialized[route_index]
        block = route[start : start + 4]
        for reordered in sorted(set(permutations(block))):
            if reordered == block:
                continue
            candidate_route = route[:start] + reordered + route[start + 4 :]
            try:
                evaluator.evaluate(candidate_route)
            except ValueError:
                continue
            candidate = list(materialized)
            candidate[route_index] = candidate_route
            candidate_routes = tuple(candidate)
            candidate_score = routes_score(evaluator, candidate_routes)
            if _score_strictly_better(candidate_score, best_score):
                best_score = candidate_score
                best_routes = candidate_routes
    return best_routes


def vnd_improve(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    candidate_limit: int | None,
    use_deadline_risk: bool = True,
    max_moves: int = 2,
    task_limit: int | None = 12,
    swap_pair_limit: int | None = 24,
    block_window_limit: int | None = 16,
    deadline: float | None = None,
) -> Routes:
    """Run bounded VND over relocate, swap, and capacity-2 block moves."""

    current = tuple(tuple(route) for route in routes)
    current_score = routes_score(evaluator, current)
    accepted_moves = 0
    neighborhood = 0
    while neighborhood < 3 and accepted_moves < max_moves:
        if deadline is not None and perf_counter() >= deadline:
            break
        if neighborhood == 0:
            candidate = inter_uav_relocate_once(
                problem,
                evaluator,
                current,
                candidate_limit=candidate_limit,
                use_deadline_risk=use_deadline_risk,
                task_limit=task_limit,
                deadline=deadline,
            )
        elif neighborhood == 1:
            candidate = inter_uav_swap_once(
                problem,
                evaluator,
                current,
                candidate_limit=candidate_limit,
                use_deadline_risk=use_deadline_risk,
                task_limit=task_limit,
                pair_limit=swap_pair_limit,
                deadline=deadline,
            )
        else:
            candidate = intra_route_block_improve_once(
                problem,
                evaluator,
                current,
                use_deadline_risk=use_deadline_risk,
                window_limit=block_window_limit,
                deadline=deadline,
            )
        candidate_score = routes_score(evaluator, candidate)
        if _score_strictly_better(candidate_score, current_score):
            current = candidate
            current_score = candidate_score
            accepted_moves += 1
            neighborhood = 0
        else:
            neighborhood += 1
    return current


def _ejection_swap_improve(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    candidate_limit: int | None,
    rng: Random,
    max_trials: int,
    deadline: float | None = None,
) -> Routes:
    current_score = routes_score(evaluator, routes)
    best_routes = routes
    best_score = current_score
    route_by_task = _task_route_index(routes)
    late_tasks: list[tuple[float, int]] = []
    for route in routes:
        metrics = evaluator.evaluate(route)
        for task_id in metrics.late_task_ids:
            late_tasks.append(
                (
                    metrics.delivery_times_min[task_id]
                    - problem.task(task_id).deadline_min,
                    task_id,
                )
            )
    late_tasks.sort(reverse=True)
    trials = 0
    for _, urgent_task in late_tasks:
        if deadline is not None and perf_counter() >= deadline:
            return best_routes
        source_index = route_by_task[urgent_task]
        target_indices = [
            index for index in range(len(routes)) if index != source_index
        ]
        rng.shuffle(target_indices)
        for target_index in target_indices:
            if deadline is not None and perf_counter() >= deadline:
                return best_routes
            ejectable = [visit for visit in routes[target_index] if visit > 0]
            ejectable.sort(
                key=lambda task_id: (
                    problem.task(task_id).deadline_min,
                    task_id,
                ),
                reverse=True,
            )
            for ejected_task in ejectable[:6]:
                if deadline is not None and perf_counter() >= deadline:
                    return best_routes
                trials += 1
                source = tuple(
                    visit
                    for visit in routes[source_index]
                    if abs(visit) != urgent_task
                )
                target = tuple(
                    visit
                    for visit in routes[target_index]
                    if abs(visit) != ejected_task
                )
                urgent_options = route_insertion_options(
                    problem,
                    evaluator,
                    target,
                    target_index,
                    urgent_task,
                    candidate_limit=candidate_limit,
                    option_count=1,
                )
                ejected_options = route_insertion_options(
                    problem,
                    evaluator,
                    source,
                    source_index,
                    ejected_task,
                    candidate_limit=candidate_limit,
                    option_count=1,
                )
                if urgent_options and ejected_options:
                    candidate = list(routes)
                    candidate[target_index] = urgent_options[0].route
                    candidate[source_index] = ejected_options[0].route
                    candidate_routes = tuple(candidate)
                    score = routes_score(evaluator, candidate_routes)
                    if score < best_score:
                        best_score = score
                        best_routes = candidate_routes
                if trials >= max_trials:
                    return best_routes
    return best_routes


def _roulette(weights: dict[str, float], rng: Random) -> str:
    names = tuple(weights)
    values = [weights[name] for name in names]
    return rng.choices(names, weights=values, k=1)[0]


def _accept_worse(
    candidate: Score,
    current: Score,
    problem: Problem,
    temperature: float,
    rng: Random,
) -> bool:
    """SA acceptance under the strict two-layer lexicographic order.

    Formal order: ``(late_count, distance_km)``.
    - late_count decrease          → always accept.
    - late_count equal, distance ↓ → always accept.
    - late_count equal, distance ↑ → SA probability.
    - late_count increase          → **never** accept.
    """

    if candidate < current:
        return True
    if candidate.late_count > current.late_count:
        return False
    # late_count equal, distance_km >= current.distance_km
    delta = (candidate.distance_km - current.distance_km) / max(
        1.0, current.distance_km
    )
    if delta <= 0:
        return True
    return rng.random() < exp(-delta / max(temperature, 1e-12))


def solve_alns(
    problem: Problem,
    *,
    config: ALNSConfig | None = None,
    initial_routes: Sequence[Sequence[int]] | None = None,
) -> SolverResult:
    """Run C2-Lex-HALNS and independently validate its best-so-far result."""

    cfg = config or ALNSConfig()
    started = perf_counter()
    deadline = (
        None
        if cfg.time_limit_seconds is None
        else started + cfg.time_limit_seconds
    )
    rng = Random(cfg.seed)
    evaluator = RouteEvaluator(problem)
    if initial_routes is None:
        current = construct_regret_initial(
            problem, candidate_limit=cfg.candidate_limit
        ).routes
    else:
        current = tuple(tuple(route) for route in initial_routes)
        initial_validation = evaluate_solution(problem, current)
        if not initial_validation.valid:
            raise ValueError(f"传入的初始解非法: {initial_validation.violations}")
        current = current + tuple(
            () for _ in range(problem.drone_count - len(current))
        )
    current_score = routes_score(evaluator, current)
    initial_score = current_score
    best = current
    best_score = current_score
    time_to_best = perf_counter() - started

    destroy_operators = tuple(
        name
        for name in DESTROY_OPERATORS
        if (
            name != "route_clear"
            if cfg.enable_assignment_destroy
            else name != "assignment_destroy"
        )
        and (cfg.enable_hypergraph_destroy or name != "hypergraph_destroy")
    )
    destroy_weights = {name: 1.0 for name in destroy_operators}
    repair_operators = tuple(
        name
        for name in REPAIR_OPERATORS
        if (cfg.enable_cluster_repair or name != "cluster_regret")
        and (cfg.enable_pair_repair or name != "pair_regret")
    )
    repair_weights = {name: 1.0 for name in repair_operators}
    total_uses = {
        **{f"destroy:{name}": 0 for name in destroy_operators},
        **{f"repair:{name}": 0 for name in repair_operators},
    }
    segment_uses = {key: 0 for key in total_uses}
    segment_rewards = {key: 0.0 for key in total_uses}
    accepted_count = 0
    vnd_calls = 0
    vnd_improved_iterations = 0
    temperature = cfg.initial_temperature
    route_pool = _RoutePool(problem, evaluator) if cfg.enable_route_pool else None
    if route_pool is not None:
        route_pool.add(best)
    completed_iterations = 0

    for iteration in range(1, cfg.max_iterations + 1):
        if deadline is not None and perf_counter() >= deadline:
            break
        destroy_name = _roulette(destroy_weights, rng)
        repair_name = _roulette(repair_weights, rng)
        minimum_pairs = 1 if len(problem.tasks) == 1 else 2
        lower = max(
            minimum_pairs,
            round(len(problem.tasks) * cfg.min_destroy_fraction),
        )
        upper = max(lower, round(len(problem.tasks) * cfg.max_destroy_fraction))
        remove_count = rng.randint(lower, upper)
        original_route_by_task = _task_route_index(current)
        partial, removed = destroy_solution(
            problem,
            evaluator,
            current,
            destroy_name,
            remove_count,
            rng,
            use_deadline_risk=cfg.enable_deadline_risk,
        )
        if deadline is not None and perf_counter() >= deadline:
            break
        try:
            candidate = _repair(
                problem,
                evaluator,
                partial,
                removed,
                repair_name,
                cfg.candidate_limit,
                use_deadline_risk=cfg.enable_deadline_risk,
                original_route_by_task=original_route_by_task,
                cluster_bundle_candidate_limit=cfg.cluster_bundle_candidate_limit,
                cluster_pair_limit=cfg.cluster_pair_limit,
                pair_candidate_limit=cfg.pair_candidate_limit,
                deadline=deadline,
            )
        except (_SearchDeadlineReached, PairRepairDeadlineReached):
            break
        timed_out = deadline is not None and perf_counter() >= deadline
        if not timed_out and cfg.enable_vnd and cfg.vnd_max_moves > 0:
            repaired_score = routes_score(evaluator, candidate)
            candidate = vnd_improve(
                problem,
                evaluator,
                candidate,
                candidate_limit=cfg.candidate_limit,
                use_deadline_risk=cfg.enable_deadline_risk,
                max_moves=cfg.vnd_max_moves,
                task_limit=cfg.vnd_task_limit,
                swap_pair_limit=cfg.vnd_swap_pair_limit,
                block_window_limit=cfg.vnd_block_window_limit,
                deadline=deadline,
            )
            vnd_calls += 1
            if routes_score(evaluator, candidate) < repaired_score:
                vnd_improved_iterations += 1
        timed_out = timed_out or (
            deadline is not None and perf_counter() >= deadline
        )
        if (
            not timed_out
            and cfg.enable_ejection
            and cfg.ejection_interval > 0
            and iteration % cfg.ejection_interval == 0
        ):
            candidate = _ejection_swap_improve(
                problem,
                evaluator,
                candidate,
                cfg.candidate_limit,
                rng,
                cfg.ejection_trials,
                deadline,
            )
        timed_out = timed_out or (
            deadline is not None and perf_counter() >= deadline
        )
        candidate_score = routes_score(evaluator, candidate)
        timed_out = timed_out or (
            deadline is not None and perf_counter() >= deadline
        )
        improved_current = candidate_score < current_score
        accepted = False if timed_out else _accept_worse(
            candidate_score, current_score, problem, temperature, rng
        )
        reward = 0.0
        if accepted:
            current = candidate
            current_score = candidate_score
            accepted_count += 1
            reward = 4.0 if improved_current else 1.0
            if route_pool is not None:
                route_pool.add(current)
        if candidate_score < best_score:
            best = candidate
            best_score = candidate_score
            time_to_best = perf_counter() - started
            reward = 8.0
            if route_pool is not None:
                route_pool.add(best)

        destroy_key = f"destroy:{destroy_name}"
        repair_key = f"repair:{repair_name}"
        for key in (destroy_key, repair_key):
            total_uses[key] += 1
            segment_uses[key] += 1
            segment_rewards[key] += reward

        if timed_out:
            completed_iterations = iteration
            break

        if (
            route_pool is not None
            and cfg.route_pool_interval > 0
            and iteration % cfg.route_pool_interval == 0
        ):
            recombined = route_pool.recombine(
                best,
                cfg.route_pool_node_limit,
                deadline,
            )
            if deadline is not None and perf_counter() >= deadline:
                completed_iterations = iteration
                break
            recombined_score = routes_score(evaluator, recombined)
            if recombined_score < best_score:
                best = recombined
                best_score = recombined_score
                current = recombined
                current_score = recombined_score
                time_to_best = perf_counter() - started
                route_pool.add(best)

        if iteration % cfg.weight_update_interval == 0:
            for name in destroy_operators:
                key = f"destroy:{name}"
                if segment_uses[key]:
                    observed = segment_rewards[key] / segment_uses[key]
                    destroy_weights[name] = max(
                        cfg.minimum_weight,
                        (1 - cfg.reaction_factor) * destroy_weights[name]
                        + cfg.reaction_factor * observed,
                    )
            for name in repair_operators:
                key = f"repair:{name}"
                if segment_uses[key]:
                    observed = segment_rewards[key] / segment_uses[key]
                    repair_weights[name] = max(
                        cfg.minimum_weight,
                        (1 - cfg.reaction_factor) * repair_weights[name]
                        + cfg.reaction_factor * observed,
                    )
            segment_uses = {key: 0 for key in segment_uses}
            segment_rewards = {key: 0.0 for key in segment_rewards}

        temperature = max(
            cfg.minimum_temperature, temperature * cfg.cooling_rate
        )
        completed_iterations = iteration

    evaluation = evaluate_solution(problem, best)
    if not evaluation.valid:
        raise RuntimeError(f"ALNS 最终解非法: {evaluation.violations}")
    all_weights = {
        **{f"destroy:{key}": value for key, value in destroy_weights.items()},
        **{f"repair:{key}": value for key, value in repair_weights.items()},
    }
    if not all(isfinite(value) and value > 0 for value in all_weights.values()):
        raise RuntimeError("ALNS 算子权重出现非法数值")
    metadata = MappingProxyType(
        {
            "method": (
                "C2-Lex-ALNS-Core"
                if not cfg.enable_route_pool and not cfg.enable_ejection
                else "C2-Lex-HALNS"
            ),
            "seed": cfg.seed,
            "enable_route_pool": cfg.enable_route_pool,
            "enable_ejection": cfg.enable_ejection,
            "enable_assignment_destroy": cfg.enable_assignment_destroy,
            "enable_deadline_risk": cfg.enable_deadline_risk,
            "enable_vnd": cfg.enable_vnd,
            "enable_cluster_repair": cfg.enable_cluster_repair,
            "enable_pair_repair": cfg.enable_pair_repair,
            "enable_hypergraph_destroy": cfg.enable_hypergraph_destroy,
            "vnd_calls": vnd_calls,
            "vnd_improved_iterations": vnd_improved_iterations,
            "time_limit_seconds": cfg.time_limit_seconds,
            "initial_score": (
                initial_score.late_count,
                initial_score.distance_km,
            ),
            "time_to_best_seconds": time_to_best,
            "accepted_solutions": accepted_count,
            "operator_uses": MappingProxyType(dict(total_uses)),
            "operator_weights": MappingProxyType(all_weights),
            "route_pool_columns": len(route_pool.columns) if route_pool else 0,
            "route_pool_last_nodes": route_pool.last_nodes if route_pool else 0,
        }
    )
    return SolverResult(
        routes=best,
        evaluation=evaluation,
        runtime_seconds=perf_counter() - started,
        iterations=completed_iterations,
        metadata=metadata,
    )


def solve_alns_core(
    problem: Problem,
    *,
    config: ALNSConfig | None = None,
    initial_routes: Sequence[Sequence[int]] | None = None,
) -> SolverResult:
    """Run the adaptive LNS core without route-pool or ejection reinforcement."""

    core_config = replace(
        config or ALNSConfig(),
        enable_route_pool=False,
        enable_ejection=False,
    )
    return solve_alns(
        problem,
        config=core_config,
        initial_routes=initial_routes,
    )
