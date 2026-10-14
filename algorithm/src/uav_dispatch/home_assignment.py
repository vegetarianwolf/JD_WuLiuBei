"""Bounded exact assignment of complete DIRECT routes to fixed UAV homes.

The deployment itself and every route's visit order stay fixed. For at most
12 identical UAVs, a subset dynamic program minimizes the additive route
Score over all assignments to the existing home slots. The resulting
permutation is accepted only after independent full-solution validation.
Relay routes are excluded because their clocks are coupled by handoffs.
"""

from __future__ import annotations

from time import perf_counter
from typing import MutableMapping, Sequence

from .model import Problem, Score
from .search import RouteEvaluator, Routes
from .validation import evaluate_solution


MAX_ASSIGNMENT_UAVS = 12


def reassign_route_homes(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Sequence[Sequence[int]],
    *,
    deadline: float | None = None,
    statistics: MutableMapping[str, int] | None = None,
) -> Routes:
    """Return a strictly better checked home assignment, or the input routes.

    ``deadline`` is an absolute ``perf_counter`` deadline. An incomplete or
    overdue scan returns the untouched input, including when time expires
    during full validation. All statistics are cumulative integer counters.
    A relay-capable Problem is allowed when these routes contain only DIRECT
    visits, which permits use inside the staged solver's DIRECT warmup.
    """

    original = tuple(tuple(route) for route in routes)

    def count(key: str, amount: int = 1) -> None:
        if statistics is not None:
            statistics[key] = statistics.get(key, 0) + amount

    def expired() -> bool:
        if deadline is not None and perf_counter() >= deadline:
            count("deadline_stops")
            return True
        return False

    if len(original) != problem.drone_count:
        raise ValueError("整路起点重配要求每架无人机恰好对应一条路线（可为空）")
    if evaluator.problem is not problem:
        raise ValueError("路线评价器必须属于同一个问题实例")
    if expired():
        return original
    size = len(original)
    registry = problem.leg_registry
    if (
        size < 2
        or size > MAX_ASSIGNMENT_UAVS
        or len(set(problem.drone_home_nodes)) < 2
        or any(abs(visit) in registry for route in original for visit in route)
    ):
        count("skipped")
        return original

    count("passes")
    # Validate complete coverage and K as well as each route's local order;
    # a route scorer alone cannot establish these whole-solution properties.
    if expired():
        return original
    checked_original = evaluate_solution(problem, original, _force_full=True)
    count("canonical_validations")
    if expired():
        return original
    if not checked_original.valid:
        raise ValueError(f"整路起点重配收到非法初解: {checked_original.violations}")

    costs: list[list[Score]] = []
    for route in original:
        row = []
        for home in problem.drone_home_nodes:
            if expired():
                return original
            row.append(evaluator.evaluate(route, start_node=home).score)
            count("matrix_evaluations")
            if expired():
                return original
        costs.append(row)

    # Mask selects source routes already placed; popcount(mask) identifies
    # the next fixed home slot. Keeping only the best Score is exact since
    # every future addition is independent of the order of previous routes.
    state_count = 1 << size
    scores: list[Score | None] = [None] * state_count
    previous: list[tuple[int, int] | None] = [None] * state_count
    scores[0] = Score(0, 0.0, 0.0)
    for mask in range(state_count - 1):
        if expired():
            return original
        score = scores[mask]
        if score is None:
            continue
        count("dp_states")
        home_index = mask.bit_count()
        for route_index in range(size):
            bit = 1 << route_index
            if mask & bit:
                continue
            next_mask = mask | bit
            candidate_score = score + costs[route_index][home_index]
            if scores[next_mask] is None or candidate_score < scores[next_mask]:
                scores[next_mask] = candidate_score
                previous[next_mask] = (mask, route_index)

    if expired():
        return original
    original_sum = Score(0, 0.0, 0.0)
    for index in range(size):
        original_sum += costs[index][index]
    if scores[-1] is None or scores[-1] >= original_sum:
        return original

    assignment = [0] * size
    mask = state_count - 1
    for home_index in range(size - 1, -1, -1):
        predecessor = previous[mask]
        if predecessor is None:
            raise RuntimeError("整路起点重配的动态规划路径不完整")
        mask, route_index = predecessor
        assignment[home_index] = route_index
    candidate = tuple(original[index] for index in assignment)
    if candidate == original or expired():
        return original

    # Additive route scores and canonical full scores may use different
    # floating-point summation order. Never let an apparent DP gain worsen
    # the canonical incumbent, even by one floating-point unit.
    checked_candidate = evaluate_solution(problem, candidate, _force_full=True)
    count("canonical_validations")
    if expired():
        return original
    if checked_candidate.valid and checked_candidate.score < checked_original.score:
        count("improvements")
        count("reassigned_routes", sum(i != source for i, source in enumerate(assignment)))
        return candidate
    count("canonical_rejections")
    return original
