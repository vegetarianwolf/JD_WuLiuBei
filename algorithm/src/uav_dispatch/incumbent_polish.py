"""Deterministic, bounded terminal refinement of a complete DIRECT incumbent.

This composes the existing canonically validated primitives; it performs no
ALNS iteration, random draw, or operator-weight update. At most two tail
rounds and two home-assignment calls are attempted.
"""
from __future__ import annotations

from time import perf_counter
from typing import MutableMapping, Sequence

from .home_assignment import reassign_route_homes
from .direct_task_exchange import bounded_task_exchange
from .model import Problem
from .route_exchange import bounded_tail_exchange
from .search import RouteEvaluator, Routes
from .validation import evaluate_solution


MAX_TAIL_PAIRS_PER_ROUND = 28
MAX_TAIL_ROUNDS = 2


def polish_direct_incumbent(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Sequence[Sequence[int]],
    *,
    deadline: float | None = None,
    max_pairs: int = MAX_TAIL_PAIRS_PER_ROUND,
    enable_task_exchange: bool = False,
    statistics: MutableMapping[str, object] | None = None,
) -> Routes:
    """Try home assignment, up to two improving tail rounds, then homes again.

    ``deadline`` is an absolute monotonic-clock deadline. Every primitive
    accepts only strict complete canonical-score improvements. A result
    returned after the wrapper's deadline is rejected, retaining the most
    recent *earlier* accepted checkpoint. The routine never waits out unused
    time and never exceeds 56 considered route pairs, even when a caller
    requests a larger ``max_pairs``. All counters are flat integers; child
    counters have ``home_`` and ``tail_`` prefixes. An optional final whole-task
    exchange uses the remaining budget, retaining its independently verified
    checkpoint. It leaves at most 2 ms for the caller's final canonical check.
    """
    original: Routes = tuple(tuple(route) for route in routes)
    if type(max_pairs) is not int or max_pairs < 0:
        raise ValueError("末端精化的路线对上限必须是非负整数")
    if evaluator.problem is not problem:
        raise ValueError("末端精化评分器与问题必须一致")
    if len(original) != problem.drone_count:
        raise ValueError("末端精化要求每架无人机恰好一条路线")

    def count(key: str, amount: int = 1) -> None:
        if statistics is not None:
            statistics[key] = int(statistics.get(key, 0)) + amount

    def expired() -> bool:
        if deadline is not None and perf_counter() >= deadline:
            count("deadline_stops")
            return True
        return False

    if expired():
        return original
    if any(abs(visit) in problem.leg_registry for route in original for visit in route):
        raise ValueError("末端精化仅支持完整 DIRECT 路线")
    baseline = evaluate_solution(problem, original, _force_full=True)
    count("canonical_input_checks")
    if expired():
        return original
    if not baseline.valid:
        raise ValueError(f"末端精化的输入解非法: {baseline.violations}")
    if len(original) < 2:
        count("single_drone_skips")
        return original
    count("passes")
    current = original

    def apply(operator, prefix: str, **kwargs) -> tuple[bool, bool]:
        nonlocal current
        if expired():
            return False, False
        count(prefix + "calls")
        work: dict[str, int] = {}
        candidate = operator(problem, evaluator, current, deadline=deadline, statistics=work, **kwargs)
        for key, value in work.items():
            count(prefix + key, value)
        if expired():
            return False, False
        if candidate == current:
            return True, False
        # Both primitives independently validate the full canonical score
        # before returning a changed route tuple. Their composition is
        # therefore monotone without adding another over-budget evaluation.
        current = candidate
        count("accepted_steps")
        return True, True

    within, _ = apply(reassign_route_homes, "home_")
    if not within:
        return current
    pair_limit = min(max_pairs, MAX_TAIL_PAIRS_PER_ROUND)
    if pair_limit:
        for _ in range(MAX_TAIL_ROUNDS):
            within, improved = apply(bounded_tail_exchange, "tail_", pair_offset=0, max_pairs=pair_limit)
            if not within:
                return current
            if not improved:
                count("tail_stagnation_stops")
                break
    apply(reassign_route_homes, "home_")
    if enable_task_exchange and not expired():
        count("task_exchange_calls")
        work: dict[str, object] = {}
        candidate = bounded_task_exchange(
            problem, evaluator, current,
            deadline=None if deadline is None else deadline - 0.002,
            statistics=work,
        )
        if statistics is not None:
            for key, value in work.items():
                if type(value) is int:
                    count("task_exchange_" + key, value)
                else:
                    statistics["task_exchange_" + key] = value
        # This primitive returns only checkpoints whose full canonical check
        # completed before its deadline, even when a later probe expired.
        if candidate != current:
            current = candidate
            count("accepted_steps")
    return current
