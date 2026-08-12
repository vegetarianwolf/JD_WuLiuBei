"""Forward deadline propagation profiles for route insertions.

Propagation metrics are diagnostics and candidate tie-breakers.  The global
objective is the strict two-layer ``Score(late_count, distance_km)`` order;
lateness metrics below are descriptive diagnostics only.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence

from .model import Problem, Score
from .search import Route, RouteEvaluator


@dataclass(frozen=True, slots=True)
class RoutePropagationEvaluation:
    """Exact forward timing state for one open pickup-and-delivery route."""

    route: Route
    score: Score
    arrival_times_min: tuple[float, ...]
    cumulative_distance_km: tuple[float, ...]
    delivery_times_min: Mapping[int, float]
    lateness_by_task_min: Mapping[int, float]


@dataclass(frozen=True, slots=True)
class InsertionPropagationEvaluation:
    """Exact local delta plus delay propagated to pre-existing deliveries."""

    base: RoutePropagationEvaluation
    candidate: RoutePropagationEvaluation
    inserted_task_ids: tuple[int, ...]
    score_delta: Score
    delta_distance_km: float
    delta_late_count: int
    delta_lateness_min: float
    future_delivery_delay_min: float
    future_lateness_delta_min: float
    downstream_delivery_delays_min: Mapping[int, float]


def evaluate_route_propagation(
    route: Sequence[int],
    *,
    problem: Problem,
    evaluator: RouteEvaluator | None = None,
) -> RoutePropagationEvaluation:
    """Scan ``route`` forward and expose exact arrival/deadline propagation."""

    materialized = tuple(route)
    route_evaluator = evaluator or RouteEvaluator(problem)
    metrics = route_evaluator.evaluate(materialized)
    previous: int | None = None
    elapsed = 0.0
    distance = 0.0
    arrivals: list[float] = []
    cumulative_distance: list[float] = []
    for visit in materialized:
        leg = problem.distance(previous, visit)
        distance += leg
        elapsed += leg / problem.speed_km_per_min
        arrivals.append(elapsed)
        cumulative_distance.append(distance)
        previous = visit
    lateness_by_task = {
        task_id: max(
            0.0,
            delivered_at - problem.task(task_id).deadline_min,
        )
        for task_id, delivered_at in metrics.delivery_times_min.items()
    }
    return RoutePropagationEvaluation(
        route=materialized,
        score=metrics.score,
        arrival_times_min=tuple(arrivals),
        cumulative_distance_km=tuple(cumulative_distance),
        delivery_times_min=metrics.delivery_times_min,
        lateness_by_task_min=MappingProxyType(lateness_by_task),
    )


def evaluate_insertion_propagation(
    base_route: Sequence[int],
    candidate_route: Sequence[int],
    *,
    inserted_task_ids: Sequence[int],
    problem: Problem,
    evaluator: RouteEvaluator | None = None,
    base_evaluation: RoutePropagationEvaluation | None = None,
) -> InsertionPropagationEvaluation:
    """Compare an insertion with its base route and expose downstream effects."""

    route_evaluator = evaluator or RouteEvaluator(problem)
    materialized_base = tuple(base_route)
    base = base_evaluation or evaluate_route_propagation(
        materialized_base,
        problem=problem,
        evaluator=route_evaluator,
    )
    if base.route != materialized_base:
        raise ValueError("预计算传播基线与 base_route 不一致")
    candidate = evaluate_route_propagation(
        candidate_route,
        problem=problem,
        evaluator=route_evaluator,
    )
    inserted = tuple(sorted(set(inserted_task_ids)))
    candidate_tasks = set(candidate.delivery_times_min)
    missing_inserted = set(inserted) - candidate_tasks
    if missing_inserted:
        raise ValueError(f"候选路线缺少插入任务 {sorted(missing_inserted)}")
    missing_existing = set(base.delivery_times_min) - candidate_tasks
    if missing_existing:
        raise ValueError(f"候选路线丢失原任务 {sorted(missing_existing)}")

    downstream_delays: dict[int, float] = {}
    future_lateness_delta = 0.0
    for task_id, old_delivery in base.delivery_times_min.items():
        delay = candidate.delivery_times_min[task_id] - old_delivery
        if delay > 1e-9:
            downstream_delays[task_id] = delay
        lateness_delta = (
            candidate.lateness_by_task_min[task_id]
            - base.lateness_by_task_min[task_id]
        )
        future_lateness_delta += max(0.0, lateness_delta)

    score_delta = candidate.score - base.score
    delta_lateness_min = sum(
        candidate.lateness_by_task_min[task_id]
        - base.lateness_by_task_min[task_id]
        for task_id in base.lateness_by_task_min
    )
    return InsertionPropagationEvaluation(
        base=base,
        candidate=candidate,
        inserted_task_ids=inserted,
        score_delta=score_delta,
        delta_distance_km=score_delta.distance_km,
        delta_late_count=score_delta.late_count,
        delta_lateness_min=delta_lateness_min,
        future_delivery_delay_min=sum(downstream_delays.values()),
        future_lateness_delta_min=future_lateness_delta,
        downstream_delivery_delays_min=MappingProxyType(downstream_delays),
    )
