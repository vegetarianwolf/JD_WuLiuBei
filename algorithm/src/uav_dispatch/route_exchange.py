"""Bounded simultaneous reassignment of complete DIRECT route suffixes.

The fleet can be full (eight UAVs with 25 tasks each).  Exchanging equal
numbers of tasks then changes assignment without needing a spare task slot.
Cuts are permitted only at zero load, so no pickup/delivery pair is split.
"""

from __future__ import annotations

from itertools import combinations
from time import perf_counter

from .model import Problem
from .search import Route, RouteEvaluator, Routes
from .validation import evaluate_solution


def _nonempty_suffix_cuts(route: Route) -> dict[int, int]:
    """Map suffix task count to its zero-load cut, including the whole route."""

    remaining = sum(visit < 0 for visit in route)
    cuts = {remaining: 0} if remaining else {}
    load = 0
    for position, visit in enumerate(route, 1):
        load += 1 if visit > 0 else -1
        remaining -= visit < 0
        if load == 0 and remaining:
            cuts[remaining] = position
    return cuts


def bounded_tail_exchange(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    deadline: float | None = None,
    pair_offset: int = 0,
    max_pairs: int = 4,
    statistics: dict[str, int] | None = None,
) -> Routes:
    """Improve at most ``max_pairs`` deterministic pairs of DIRECT routes.

    Each pair accepts its first strict canonical full-solution improvement.
    Equal suffix task counts preserve *each* route's task count, including
    underfilled fleets.  Every route keeps its own home when it receives a
    different suffix; capacity and precedence follow from zero-load cuts.

    ``pair_offset`` indexes the lexicographically ordered unordered pairs,
    with wraparound.  No random draws are consumed.  When the deadline is
    reached, return all improvements fully checked *before* the deadline;
    never adopt a partially checked or over-budget move.
    """

    if max_pairs < 0:
        raise ValueError("后缀交换的路线对上限不能为负")
    if evaluator.problem is not problem:
        raise ValueError("路线评分器与交换问题必须一致")

    def record(key: str) -> None:
        if statistics is not None:
            statistics[key] = statistics.get(key, 0) + 1

    def expired() -> bool:
        if deadline is not None and perf_counter() >= deadline:
            record("deadline_reached")
            return True
        return False

    if expired() or max_pairs == 0 or len(routes) < 2:
        return routes
    registry = problem.leg_registry or {}
    if any(abs(visit) in registry for route in routes for visit in route):
        raise ValueError("等量后缀交换仅支持 DIRECT 路线")

    baseline = evaluate_solution(problem, routes, _force_full=True)
    record("full_checks")
    if expired():
        return routes
    if not baseline.valid:
        raise ValueError(f"后缀交换的输入解非法: {baseline.violations}")
    current, current_score = routes, baseline.score
    pairs = tuple(combinations(range(len(routes)), 2))
    for offset in range(min(max_pairs, len(pairs))):
        if expired():
            return current
        left, right = pairs[(pair_offset + offset) % len(pairs)]
        record("pair_checks")
        a, b = current[left], current[right]
        a_cuts, b_cuts = _nonempty_suffix_cuts(a), _nonempty_suffix_cuts(b)
        counts = sorted(a_cuts.keys() & b_cuts.keys())
        if not counts:
            continue
        a_home, b_home = problem.home_node(left), problem.home_node(right)
        pair_score = (
            evaluator.evaluate(a, start_node=a_home).score
            + evaluator.evaluate(b, start_node=b_home).score
        )
        for count in counts:
            if expired():
                return current
            record("cut_checks")
            a_cut, b_cut = a_cuts[count], b_cuts[count]
            # Exchanging complete routes at the same home only relabels UAVs;
            # skip that symmetry (including floating-point sum-order noise).
            if a_cut == b_cut == 0 and a_home == b_home:
                continue
            new_a, new_b = a[:a_cut] + b[b_cut:], b[:b_cut] + a[a_cut:]
            record("probes")
            candidate_pair_score = (
                evaluator.evaluate(new_a, start_node=a_home).score
                + evaluator.evaluate(new_b, start_node=b_home).score
            )
            if expired():
                return current
            if candidate_pair_score >= pair_score:
                continue
            changed = list(current)
            changed[left], changed[right] = new_a, new_b
            candidate = tuple(changed)
            checked = evaluate_solution(problem, candidate, _force_full=True)
            record("full_checks")
            if expired():
                return current
            if checked.valid and checked.score < current_score:
                current, current_score = candidate, checked.score
                record("improvements")
                break
    return current
