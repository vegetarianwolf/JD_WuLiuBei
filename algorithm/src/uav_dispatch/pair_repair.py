"""Capacity-two joint insertion and pair-regret repair.

Unlike a generic bundle repair, this module explicitly enumerates the six
linear extensions of two pickup-before-delivery pairs.  Candidate quality is
always an exact lexicographic ``Score`` delta.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import inf
from time import perf_counter
from typing import Iterable, Sequence

from .interaction_graph import evaluate_task_pair
from .model import Problem, Score
from .search import (
    Route,
    RouteEvaluator,
    Routes,
    route_insertion_options,
)


class PairRepairDeadlineReached(RuntimeError):
    """Abort an incomplete pair repair at the ALNS wall-clock limit."""


@dataclass(frozen=True, slots=True)
class PairInsertionOption:
    """One exact joint insertion of two tasks into a single route."""

    delta: Score
    route_index: int
    route: Route
    task_ids: tuple[int, int]
    pair_order: tuple[int, ...]
    insertion_position: int


def enumerate_pair_orders(task_a: int, task_b: int) -> tuple[tuple[int, ...], ...]:
    """Return the six precedence-feasible orders available at capacity two."""

    if task_a == task_b:
        raise ValueError("成对修复需要两个不同任务")
    left, right = sorted((task_a, task_b))
    return (
        (left, -left, right, -right),
        (left, right, -left, -right),
        (left, right, -right, -left),
        (right, -right, left, -left),
        (right, left, -right, -left),
        (right, left, -left, -right),
    )


def pair_insertion_options(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Sequence[Sequence[int]],
    task_ids: tuple[int, int],
    *,
    candidate_limit: int | None,
    option_count: int = 2,
    deadline: float | None = None,
) -> tuple[PairInsertionOption, ...]:
    """Enumerate bounded block insertions for all six capacity-two orders."""

    if problem.capacity != 2:
        raise ValueError("pair repair 仅适用于载荷上限为 2 的问题")
    pair = tuple(sorted(task_ids))
    orders = enumerate_pair_orders(*pair)
    options: list[PairInsertionOption] = []
    for route_index, raw_route in enumerate(routes):
        if deadline is not None and perf_counter() >= deadline:
            raise PairRepairDeadlineReached
        route = tuple(raw_route)
        if sum(visit > 0 for visit in route) + 2 > problem.max_tasks_per_drone:
            continue
        old_score = evaluator.evaluate(route).score
        for position in range(len(route) + 1):
            for pair_order in orders:
                candidate = route[:position] + pair_order + route[position:]
                try:
                    score = evaluator.evaluate(candidate).score
                except ValueError:
                    continue
                options.append(
                    PairInsertionOption(
                        delta=score - old_score,
                        route_index=route_index,
                        route=candidate,
                        task_ids=pair,
                        pair_order=pair_order,
                        insertion_position=position,
                    )
                )
    options.sort(
        key=lambda option: (
            option.delta,
            option.route_index,
            option.insertion_position,
            option.pair_order,
            option.route,
        )
    )
    limit = len(options) if candidate_limit is None else max(2, candidate_limit)
    return tuple(options[: min(limit, max(1, option_count))])


def _rank_candidate_pairs(
    problem: Problem,
    remaining: set[int],
    pair_candidate_limit: int | None,
) -> tuple[tuple[int, int], ...]:
    ranked: list[tuple[tuple[float, ...], tuple[int, int]]] = []
    for pair in combinations(sorted(remaining), 2):
        interaction = evaluate_task_pair(
            problem.task(pair[0]),
            problem.task(pair[1]),
            depot=problem.depot,
            speed_km_per_min=problem.speed_km_per_min,
        )
        compatibility = (
            interaction.deadline_conflict_risk,
            -interaction.distance_saving_km,
            float(pair[0]),
            float(pair[1]),
        )
        ranked.append((compatibility, pair))
    ranked.sort()
    if pair_candidate_limit is not None:
        ranked = ranked[:pair_candidate_limit]
    return tuple(pair for _, pair in ranked)


def _single_regret_repair(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    remaining: set[int],
    *,
    candidate_limit: int | None,
    deadline: float | None,
) -> Routes:
    while remaining:
        if deadline is not None and perf_counter() >= deadline:
            raise PairRepairDeadlineReached
        per_task = {}
        for task_id in sorted(remaining):
            options = []
            for route_index, route in enumerate(routes):
                options.extend(
                    route_insertion_options(
                        problem,
                        evaluator,
                        route,
                        route_index,
                        task_id,
                        candidate_limit=candidate_limit,
                        option_count=2,
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
            if not options:
                raise RuntimeError(f"成对修复无法重新插入任务 {task_id}")
            per_task[task_id] = tuple(options[:2])

        def regret_key(task_id: int) -> tuple[float, float, float, float, int]:
            options = per_task[task_id]
            if len(options) < 2:
                regret = (inf, inf, inf)
            else:
                best = options[0].delta
                alternative = options[1].delta
                regret = (
                    float(alternative.late_count - best.late_count),
                    alternative.total_lateness_min - best.total_lateness_min,
                    alternative.distance_km - best.distance_km,
                )
            return (
                regret[0],
                regret[1],
                regret[2],
                -problem.task(task_id).deadline_min,
                -task_id,
            )

        chosen_task = max(remaining, key=regret_key)
        choice = per_task[chosen_task][0]
        mutable = list(routes)
        mutable[choice.route_index] = choice.route
        routes = tuple(mutable)
        remaining.remove(chosen_task)
    return routes


def pair_regret_repair(
    problem: Problem,
    evaluator: RouteEvaluator,
    partial_routes: Routes,
    removed_task_ids: Iterable[int],
    *,
    candidate_limit: int | None,
    pair_candidate_limit: int | None = 8,
    deadline: float | None = None,
) -> Routes:
    """Jointly insert compatible task pairs, then regret-insert any remainder."""

    if problem.capacity != 2:
        raise ValueError("pair regret repair 仅适用于载荷上限为 2 的问题")
    routes = tuple(tuple(route) for route in partial_routes)
    remaining = set(removed_task_ids)
    while len(remaining) >= 2:
        choices: list[tuple[tuple[float, ...], PairInsertionOption]] = []
        for pair in _rank_candidate_pairs(
            problem,
            remaining,
            pair_candidate_limit,
        ):
            options = pair_insertion_options(
                problem,
                evaluator,
                routes,
                pair,
                candidate_limit=candidate_limit,
                option_count=2,
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
            regret = (
                (inf, inf, inf)
                if alternative is None
                else (
                    float(alternative.delta.late_count - best.delta.late_count),
                    alternative.delta.total_lateness_min
                    - best.delta.total_lateness_min,
                    alternative.delta.distance_km - best.delta.distance_km,
                )
            )
            interaction = evaluate_task_pair(
                problem.task(pair[0]),
                problem.task(pair[1]),
                depot=problem.depot,
                speed_km_per_min=problem.speed_km_per_min,
            )
            choices.append(
                (
                    (
                        regret[0],
                        regret[1],
                        regret[2],
                        -interaction.deadline_conflict_risk,
                        interaction.distance_saving_km,
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

    return _single_regret_repair(
        problem,
        evaluator,
        routes,
        remaining,
        candidate_limit=candidate_limit,
        deadline=deadline,
    )
