"""Capacity-two task interaction graph primitives.

The graph is a search-guidance structure.  Its edge weights never replace the
strict :class:`~uav_dispatch.model.Score` ordering used to accept solutions.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache
from itertools import combinations, permutations
from types import MappingProxyType
from typing import Callable, Mapping, Sequence

from .model import Point, Problem, Score, Task


@dataclass(frozen=True, slots=True)
class PairInteraction:
    """Observable evidence describing how two capacity-two tasks interact."""

    task_ids: tuple[int, int]
    distance_saving_km: float
    deadline_conflict_risk: float
    pickup_delivery_feasibility: float
    feasible_orders: tuple[tuple[int, ...], ...]
    best_order: tuple[int, ...]
    separate_score: Score
    joint_score: Score
    edge_weight: float
    same_route: bool = False
    capacity_overlap: bool = False


@dataclass(frozen=True, slots=True)
class TaskInteractionGraph:
    """Immutable complete graph over the delivery tasks in one problem."""

    nodes: tuple[int, ...]
    edges: Mapping[tuple[int, int], PairInteraction]

    def edge(self, task_a: int, task_b: int) -> PairInteraction:
        if task_a == task_b:
            raise ValueError("自环不是任务交互边")
        return self.edges[tuple(sorted((task_a, task_b)))]

    def neighbors(self, task_id: int) -> tuple[PairInteraction, ...]:
        if task_id not in self.nodes:
            raise KeyError(task_id)
        return tuple(
            edge
            for pair, edge in self.edges.items()
            if task_id in pair
        )


def _score_order(
    task_by_id: dict[int, Task],
    order: tuple[int, ...],
    depot: Point,
    speed_km_per_min: float,
) -> Score:
    previous = depot
    distance = 0.0
    elapsed = 0.0
    late_count = 0
    total_lateness = 0.0
    for visit in order:
        task = task_by_id[abs(visit)]
        point = task.pickup if visit > 0 else task.delivery
        leg = previous.distance_to(point)
        distance += leg
        elapsed += leg / speed_km_per_min
        previous = point
        if visit < 0:
            lateness = max(0.0, elapsed - task.deadline_min)
            if lateness > 1e-9:
                late_count += 1
                total_lateness += lateness
    return Score(late_count, total_lateness, distance)


def _is_capacity_two_order(order: tuple[int, ...]) -> bool:
    picked: set[int] = set()
    delivered: set[int] = set()
    load = 0
    for visit in order:
        task_id = abs(visit)
        if visit > 0:
            if task_id in picked:
                return False
            picked.add(task_id)
            load += 1
        else:
            if task_id not in picked or task_id in delivered:
                return False
            delivered.add(task_id)
            load -= 1
        if load < 0 or load > 2:
            return False
    return load == 0 and picked == delivered


def _build_pair_interaction(
    left: Task,
    right: Task,
    score_order: Callable[[tuple[int, ...]], Score],
) -> PairInteraction:
    visits = (left.id, -left.id, right.id, -right.id)
    feasible_orders = tuple(
        order for order in permutations(visits) if _is_capacity_two_order(order)
    )
    scored_orders = tuple(
        (score_order(order), order) for order in feasible_orders
    )
    joint_score, best_order = min(scored_orders)
    separate_score = score_order((left.id, -left.id)) + score_order(
        (right.id, -right.id)
    )
    distance_saving = separate_score.distance_km - joint_score.distance_km
    extra_late = max(0, joint_score.late_count - separate_score.late_count)
    extra_lateness = max(
        0.0,
        joint_score.total_lateness_min - separate_score.total_lateness_min,
    )
    deadline_scale = max(1.0, min(left.deadline_min, right.deadline_min))
    deadline_conflict = float(extra_late) + extra_lateness / deadline_scale
    feasibility = len(feasible_orders) / 6.0
    distance_scale = max(1.0, separate_score.distance_km)
    # A larger edge weight means a more difficult joint interaction.  This is
    # only a destroy/repair ranking signal; solution quality remains Score.
    edge_weight = (
        deadline_conflict
        + max(0.0, -distance_saving) / distance_scale
        + (1.0 - feasibility)
        - 0.25 * max(0.0, distance_saving) / distance_scale
    )
    return PairInteraction(
        task_ids=(left.id, right.id),
        distance_saving_km=distance_saving,
        deadline_conflict_risk=deadline_conflict,
        pickup_delivery_feasibility=feasibility,
        feasible_orders=feasible_orders,
        best_order=best_order,
        separate_score=separate_score,
        joint_score=joint_score,
        edge_weight=edge_weight,
    )


@lru_cache(maxsize=50_000)
def _evaluate_task_pair_cached(
    task_a: Task,
    task_b: Task,
    depot: Point,
    speed_km_per_min: float,
) -> PairInteraction:
    if task_a.id == task_b.id:
        raise ValueError("任务交互边必须连接两个不同任务")
    if speed_km_per_min <= 0:
        raise ValueError("飞行速度必须为正数")

    left, right = sorted((task_a, task_b), key=lambda task: task.id)
    task_by_id = {left.id: left, right.id: right}
    return _build_pair_interaction(
        left,
        right,
        lambda order: _score_order(
            task_by_id,
            order,
            depot,
            speed_km_per_min,
        ),
    )


def evaluate_task_pair(
    task_a: Task,
    task_b: Task,
    *,
    depot: Point = Point(0.0, 0.0),
    speed_km_per_min: float = 0.9,
) -> PairInteraction:
    """Evaluate all six legal two-task pickup/delivery interleavings."""

    return _evaluate_task_pair_cached(
        task_a,
        task_b,
        depot,
        float(speed_km_per_min),
    )


def _evaluate_problem_pair(
    problem: Problem,
    task_a: Task,
    task_b: Task,
) -> PairInteraction:
    left, right = sorted((task_a, task_b), key=lambda task: task.id)

    def score_order(order: tuple[int, ...]) -> Score:
        previous: int | None = None
        distance = 0.0
        elapsed = 0.0
        late_count = 0
        total_lateness = 0.0
        for visit in order:
            leg = problem.distance(previous, visit)
            distance += leg
            elapsed += leg / problem.speed_km_per_min
            previous = visit
            if visit < 0:
                task = problem.task(-visit)
                lateness = max(0.0, elapsed - task.deadline_min)
                if lateness > 1e-9:
                    late_count += 1
                    total_lateness += lateness
        return Score(late_count, total_lateness, distance)

    return _build_pair_interaction(left, right, score_order)


def _solution_routes(solution: object) -> tuple[tuple[int, ...], ...]:
    raw_routes = getattr(solution, "routes", solution)
    if not isinstance(raw_routes, Sequence):
        raise TypeError("solution 必须提供路线序列")
    return tuple(tuple(route) for route in raw_routes)


def _route_context(
    routes: Sequence[Sequence[int]],
) -> tuple[dict[int, int], dict[int, tuple[int, int]]]:
    route_by_task: dict[int, int] = {}
    positions: dict[int, list[int]] = {}
    for route_index, route in enumerate(routes):
        for position, visit in enumerate(route):
            task_id = abs(visit)
            route_by_task[task_id] = route_index
            positions.setdefault(task_id, []).append(position)
    complete_positions = {
        task_id: (values[0], values[1])
        for task_id, values in positions.items()
        if len(values) == 2
    }
    return route_by_task, complete_positions


def build_interaction_graph(
    problem: Problem,
    solution: object,
) -> TaskInteractionGraph:
    """Build pair evidence and annotate interactions present in ``solution``."""

    routes = _solution_routes(solution)
    route_by_task, positions = _route_context(routes)
    edges: dict[tuple[int, int], PairInteraction] = {}
    for task_a, task_b in combinations(problem.tasks, 2):
        interaction = _evaluate_problem_pair(problem, task_a, task_b)
        same_route = (
            task_a.id in route_by_task
            and task_b.id in route_by_task
            and route_by_task[task_a.id] == route_by_task[task_b.id]
        )
        capacity_overlap = False
        if same_route and task_a.id in positions and task_b.id in positions:
            pickup_a, delivery_a = positions[task_a.id]
            pickup_b, delivery_b = positions[task_b.id]
            capacity_overlap = max(pickup_a, pickup_b) < min(
                delivery_a, delivery_b
            )
        overlap_conflict = (
            0.5 * (1.0 + interaction.deadline_conflict_risk)
            if capacity_overlap
            else 0.0
        )
        interaction = replace(
            interaction,
            same_route=same_route,
            capacity_overlap=capacity_overlap,
            edge_weight=interaction.edge_weight + overlap_conflict,
        )
        edges[interaction.task_ids] = interaction
    return TaskInteractionGraph(
        nodes=problem.task_ids,
        edges=MappingProxyType(edges),
    )


def rank_interaction_tasks(graph: TaskInteractionGraph) -> tuple[int, ...]:
    """Rank nodes by incident conflict mass, with deterministic tie breaks."""

    conflict_by_task = {task_id: 0.0 for task_id in graph.nodes}
    active_edges_by_task = {task_id: 0 for task_id in graph.nodes}
    for edge in graph.edges.values():
        conflict = max(0.0, edge.edge_weight)
        if edge.capacity_overlap:
            conflict += 0.5
        for task_id in edge.task_ids:
            conflict_by_task[task_id] += conflict
            active_edges_by_task[task_id] += int(conflict > 0.0)
    return tuple(
        sorted(
            graph.nodes,
            key=lambda task_id: (
                -conflict_by_task[task_id],
                -active_edges_by_task[task_id],
                task_id,
            ),
        )
    )
