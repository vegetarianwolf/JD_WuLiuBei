"""Conflict-hypergraph destroy operator for capacity-two UAV routes."""

from __future__ import annotations

from dataclasses import dataclass
from random import Random
from typing import Sequence

from .interaction_graph import (
    PairInteraction,
    TaskInteractionGraph,
    build_interaction_graph,
    rank_interaction_tasks,
)
from .model import Problem
from .search import RouteEvaluator, Routes


@dataclass(frozen=True, slots=True)
class ConflictHyperedge:
    """A capacity-two pair whose joint service creates useful search tension."""

    task_ids: tuple[int, int]
    deadline_conflict_risk: float
    distance_penalty_km: float
    capacity_overlap: bool
    edge_weight: float

    @classmethod
    def from_interaction(cls, edge: PairInteraction) -> "ConflictHyperedge":
        return cls(
            task_ids=edge.task_ids,
            deadline_conflict_risk=edge.deadline_conflict_risk,
            distance_penalty_km=max(0.0, -edge.distance_saving_km),
            capacity_overlap=edge.capacity_overlap,
            edge_weight=edge.edge_weight,
        )

    @property
    def is_conflicting(self) -> bool:
        return (
            self.deadline_conflict_risk > 0.0
            or self.distance_penalty_km > 0.0
            or self.capacity_overlap
        )

    @property
    def rank_key(self) -> tuple[float, ...]:
        return (
            self.deadline_conflict_risk,
            float(self.capacity_overlap),
            self.distance_penalty_km,
            self.edge_weight,
            -float(self.task_ids[0]),
            -float(self.task_ids[1]),
        )


def _rank_conflict_hyperedges(
    graph: TaskInteractionGraph,
    present_task_ids: set[int],
    rng: Random,
) -> tuple[ConflictHyperedge, ...]:
    ranked = [
        (ConflictHyperedge.from_interaction(edge), rng.random())
        for edge in graph.edges.values()
        if set(edge.task_ids) <= present_task_ids
    ]
    ranked.sort(
        key=lambda item: (item[0].rank_key, item[1]),
        reverse=True,
    )
    return tuple(edge for edge, _ in ranked)


def hypergraph_destroy(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Sequence[Sequence[int]],
    count: int,
    rng: Random,
) -> tuple[Routes, tuple[int, ...]]:
    """Remove high-conflict hyperedges while keeping every partial route legal."""

    materialized = tuple(tuple(route) for route in routes)
    for route in materialized:
        evaluator.evaluate(route)
    present = {
        visit for route in materialized for visit in route if visit > 0
    }
    if not present:
        return materialized, ()
    target = min(max(1, count), len(present))
    graph = build_interaction_graph(problem, materialized)
    hyperedges = _rank_conflict_hyperedges(graph, present, rng)

    selected: list[int] = []
    selected_set: set[int] = set()
    for edge in hyperedges:
        if len(selected) + 2 > target:
            break
        if not edge.is_conflicting:
            break
        if selected_set.isdisjoint(edge.task_ids):
            selected.extend(edge.task_ids)
            selected_set.update(edge.task_ids)
        if len(selected) == target:
            break

    if len(selected) < target:
        for task_id in rank_interaction_tasks(graph):
            if task_id in present and task_id not in selected_set:
                selected.append(task_id)
                selected_set.add(task_id)
            if len(selected) == target:
                break

    partial: Routes = tuple(
        tuple(visit for visit in route if abs(visit) not in selected_set)
        for route in materialized
    )
    for route in partial:
        evaluator.evaluate(route)
    return partial, tuple(selected)
