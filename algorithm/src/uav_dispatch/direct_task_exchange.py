"""Bounded equal-count exchanges without requiring zero-load route cuts.

Both signed visits of one DIRECT task replace both signed visits of another
task at their original positions. The event signs, load profile and per-UAV
task count stay unchanged, including interleaved pickup/delivery routes.
"""
from __future__ import annotations

from bisect import bisect_left
from time import perf_counter
from typing import MutableMapping

from .model import Problem, Score
from .search import Route, RouteEvaluator, Routes
from .validation import evaluate_solution


MAX_TASK_CANDIDATES = 256
MAX_ANCHORS = 64
SPATIAL_WINDOW = 2
MAX_PARTNER_ROUTES = 7
PARTNERS_PER_ANCHOR = 4


def _positions(route: Route) -> dict[int, tuple[int, int]]:
    pickups = {visit: position for position, visit in enumerate(route) if visit > 0}
    return {abs(visit): (pickups[abs(visit)], position)
            for position, visit in enumerate(route) if visit < 0}


def _replacement_distance_delta(
    problem: Problem, route: Route, home: int, positions: tuple[int, int],
    old: int, new: int,
) -> float:
    """Exactly score changed incident edges, counting shared edges once."""
    changed_edges = {edge for position in positions for edge in (position, position + 1)
                     if edge < len(route)}

    def replacement(visit: int) -> int:
        return (new if visit > 0 else -new) if abs(visit) == old else visit

    delta = 0.0
    for edge in changed_edges:
        before = route[edge - 1] if edge else None
        after = route[edge]
        delta += (problem.distance(None if before is None else replacement(before),
                                   replacement(after), from_node=home if edge == 0 else None)
                  - problem.distance(before, after, from_node=home if edge == 0 else None))
    return delta


def _physical(score: Score) -> tuple[int, float, float]:
    return score.late_count, score.total_lateness_min, score.distance_km


def bounded_task_exchange(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    deadline: float | None = None,
    max_candidates: int = MAX_TASK_CANDIDATES,
    statistics: MutableMapping[str, object] | None = None,
) -> Routes:
    """Try at most 256 deterministically shortlisted whole-task exchanges.

    Up to 64 late/isolated anchors inspect at most 56 midpoint neighbours
    from at most seven other routes, retaining four partners each. Ranking
    uses the two tasks' old delivery
    clocks plus exact changed-edge distance; it is only a ranking heuristic,
    never an improvement veto. Score ordering follows the caller's objective.

    Only two changed routes are evaluated per probe. A promising pair must
    pass an independent complete canonical check before adoption. Expiration
    returns the last checkpoint whose canonical check ended before deadline.
    """
    if type(max_candidates) is not int or max_candidates < 0:
        raise ValueError("整任务交换的候选上限必须是非负整数")
    if evaluator.problem is not problem:
        raise ValueError("整任务交换评分器与问题必须一致")

    def count(key: str, amount: int = 1) -> None:
        if statistics is not None:
            statistics[key] = int(statistics.get(key, 0)) + amount

    def expired() -> bool:
        if deadline is not None and perf_counter() >= deadline:
            count("deadline_reached")
            return True
        return False

    if expired() or max_candidates == 0 or len(routes) < 2:
        return routes
    if len(routes) != problem.drone_count:
        raise ValueError("整任务交换要求每架无人机恰好一条路线")
    if any(abs(visit) in problem.leg_registry for route in routes for visit in route):
        raise ValueError("整任务交换仅支持完整 DIRECT 路线")
    baseline = evaluate_solution(problem, routes, _force_full=True)
    count("full_checks")
    if expired():
        return routes
    if not baseline.valid:
        raise ValueError(f"整任务交换的输入解非法: {baseline.violations}")
    current, current_score = routes, baseline.score
    if statistics is not None:
        statistics["before_score"] = statistics["after_score"] = _physical(current_score)
    ranking_started = perf_counter()

    metrics = []
    locations: dict[int, tuple[int, tuple[int, int]]] = {}
    for route_index, route in enumerate(current):
        if expired():
            return current
        metrics.append(evaluator.evaluate(route, start_node=problem.home_node(route_index)))
        locations.update({task: (route_index, pos) for task, pos in _positions(route).items()})

    midpoints = {task.id: ((task.pickup.x + task.delivery.x) * 0.5,
                           (task.pickup.y + task.delivery.y) * 0.5)
                 for task in problem.tasks}
    route_tasks: list[list[int]] = [[] for _ in current]
    for task, (route_index, _) in locations.items():
        route_tasks[route_index].append(task)
    axes = {route_index: [sorted((midpoints[task][axis], task) for task in tasks)
                          for axis in (0, 1)]
            for route_index, tasks in enumerate(route_tasks) if tasks}
    centroids = {route_index: tuple(sum(midpoints[task][axis] for task in tasks) / len(tasks)
                                   for axis in (0, 1))
                 for route_index, tasks in enumerate(route_tasks) if tasks}

    def anchor_priority(task: int) -> tuple[float, float, int, int]:
        route_index, positions = locations[task]
        route = current[route_index]
        clock = metrics[route_index].delivery_times_min[task]
        lateness = max(0.0, clock - problem.task(task).deadline_min)
        # Incident arc length measures isolation cheaply; duplicated edges
        # are removed, and the open route contributes no final return leg.
        incident = {edge for position in positions for edge in (position, position + 1)
                    if edge < len(route)}
        isolation = sum(problem.distance(route[edge - 1] if edge else None, route[edge],
                                         from_node=problem.home_node(route_index) if edge == 0 else None)
                        for edge in incident)
        return -lateness, -isolation, route_index, task

    anchors = sorted(locations, key=anchor_priority)[:MAX_ANCHORS]
    shortlisted: dict[tuple[int, int], Score] = {}
    for task in anchors:
        if expired():
            return current
        left, left_positions = locations[task]
        neighbours: set[int] = set()
        other_routes = sorted((route_index for route_index in axes if route_index != left),
                              key=lambda route_index: (
                                  sum((centroids[route_index][axis] - midpoints[task][axis]) ** 2
                                      for axis in (0, 1)), route_index))[:MAX_PARTNER_ROUTES]
        for route_index in other_routes:
            for axis, order in enumerate(axes[route_index]):
                index = bisect_left(order, (midpoints[task][axis], task))
                neighbours.update(other for _, other in
                                  order[max(0, index - SPATIAL_WINDOW):index + SPATIAL_WINDOW])
        partners = []
        for other in sorted(neighbours):
            if expired():
                return current
            right, right_positions = locations[other]
            if left == right:
                continue
            count("shortlist_probes")
            a_clock, b_clock = metrics[left].delivery_times_min[task], metrics[right].delivery_times_min[other]
            a_deadline, b_deadline = problem.task(task).deadline_min, problem.task(other).deadline_min
            old_late = (max(0.0, a_clock - a_deadline), max(0.0, b_clock - b_deadline))
            new_late = (max(0.0, b_clock - a_deadline), max(0.0, a_clock - b_deadline))
            distance_delta = (
                _replacement_distance_delta(problem, current[left], problem.home_node(left),
                                            left_positions, task, other)
                + _replacement_distance_delta(problem, current[right], problem.home_node(right),
                                              right_positions, other, task)
            )
            proxy = Score(sum(value > 1e-9 for value in new_late) - sum(value > 1e-9 for value in old_late),
                          sum(new_late) - sum(old_late), distance_delta)
            partners.append((proxy, other))
        for proxy, other in sorted(partners)[:PARTNERS_PER_ANCHOR]:
            pair = min(task, other), max(task, other)
            shortlisted[pair] = proxy
    candidate_pairs = sorted(shortlisted, key=lambda pair: (shortlisted[pair], pair))[
        :min(max_candidates, MAX_TASK_CANDIDATES)]
    count("shortlisted_candidates", len(candidate_pairs))
    if statistics is not None:
        statistics["ranking_seconds"] = perf_counter() - ranking_started

    for task, other in candidate_pairs:
        if expired():
            return current
        left, _ = locations[task]
        right, _ = locations[other]
        if left == right:
            count("same_route_skips")
            continue
        new_left = tuple((other if visit > 0 else -other) if abs(visit) == task else visit
                         for visit in current[left])
        new_right = tuple((task if visit > 0 else -task) if abs(visit) == other else visit
                          for visit in current[right])
        count("candidates")
        left_metrics = evaluator.evaluate(new_left, start_node=problem.home_node(left))
        if expired():
            return current
        right_metrics = evaluator.evaluate(new_right, start_node=problem.home_node(right))
        if expired():
            return current
        if left_metrics.score + right_metrics.score >= metrics[left].score + metrics[right].score:
            continue
        changed = list(current)
        changed[left], changed[right] = new_left, new_right
        candidate = tuple(changed)
        checked = evaluate_solution(problem, candidate, _force_full=True)
        count("full_checks")
        if expired():
            return current
        if checked.valid and checked.score < current_score:
            current, current_score = candidate, checked.score
            metrics[left], metrics[right] = left_metrics, right_metrics
            for route_index in (left, right):
                locations.update({tid: (route_index, pos) for tid, pos in _positions(current[route_index]).items()})
            count("improvements")
            if statistics is not None:
                statistics["after_score"] = _physical(current_score)
    return current
