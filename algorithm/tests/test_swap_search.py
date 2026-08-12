"""Tests for the stage-2 swap search (operator S1 and three-layer objective)."""

from __future__ import annotations

from random import Random

import pytest

from uav_dispatch.alns import ALNSConfig
from uav_dispatch.model import Point, Problem, Score, Task
from uav_dispatch.swap_model import SwapSolution, swap_solution_from_routes
from uav_dispatch.swap_search import (
    SwapConfig,
    _gap_options,
    build_swap_candidate,
    late_rescue_swap,
    solve_swap,
    solve_swap_stage2,
)
from uav_dispatch.swap_validation import evaluate_swap_solution


def _rescue_problem() -> Problem:
    return Problem(
        (
            Task(1, Point(1, 0), Point(0, 4), 10),
            Task(2, Point(0, 1), Point(1, 1), 20),
            Task(3, Point(2, 0), Point(9, 0), 15),
        ),
        drone_count=2,
        max_tasks_per_drone=3,
        capacity=2,
        speed_km_per_min=1,
    )


def test_build_swap_candidate_produces_a_valid_swapped_solution():
    problem = _rescue_problem()
    base = swap_solution_from_routes(((1, 3, -3, -1), (2, -2)))

    candidate = build_swap_candidate(base, 1, 2, 0, 1, 2, 1, -2, 0)

    assert candidate is not None
    assert candidate.routes == ((1, 3, -2, -3), (2, -1))
    evaluation = evaluate_swap_solution(problem, candidate)
    assert evaluation.valid
    # A: depot->P1 (1) -> P3 (1) -> D2 (sqrt2). B: depot->P2 (1) -> D2 (1).
    # Swap at max(1+1+sqrt2, 2); B then flies D2 -> D1 (sqrt10).
    assert evaluation.delivery_times_min[1] == pytest.approx(
        1 + 1 + 2 ** 0.5 + 10 ** 0.5
    )


def test_gap_options_use_the_shrunk_route_length():
    # Removing -1 shrinks the route by one, so the max gap is len(route) - 1.
    routes = ((1, 3, -3, -1), (2, -2))
    assert _gap_options(routes, 0, 1, 12) == (1, 2, 3)
    assert _gap_options(routes, 1, 2, 12) == (1,)


def test_gap_options_respect_pickup_position():
    routes = ((1, 3, -3, -1), (2, -2))
    # Pickup of task 3 is at index 1, so gaps start after it.
    assert _gap_options(routes, 0, 3, 12) == (2, 3)


def test_late_rescue_swap_rescues_a_late_task():
    problem = _rescue_problem()
    base = swap_solution_from_routes(((1, 3, -3, -1), (2, -2)))
    base_evaluation = evaluate_swap_solution(problem, base)
    assert base_evaluation.score.late_count == 1

    config = SwapConfig(seed=1, max_iterations=1)
    result = late_rescue_swap(problem, config, base, base_evaluation, Random(1))

    assert result is not None
    candidate, evaluation = result
    assert evaluation.valid
    assert evaluation.score.late_count == 0
    assert evaluation.delivery_times_min[1] <= problem.task(1).deadline_min + 1e-9
    assert candidate.swap_count == 1


def test_stage2_never_worsens_the_baseline():
    problem = _rescue_problem()
    routes = ((1, 3, -3, -1), (2, -2))
    baseline_evaluation = evaluate_swap_solution(
        problem, swap_solution_from_routes(routes)
    )

    final = solve_swap_stage2(problem, routes, SwapConfig(seed=1, max_iterations=3))

    assert final.evaluation.valid
    assert final.evaluation.score <= baseline_evaluation.score
    assert final.evaluation.score.late_count == 0


def test_stage2_keeps_baseline_when_no_swap_helps():
    problem = _rescue_problem()
    routes = ((1, -1), (2, -2, 3, -3))
    final = solve_swap_stage2(problem, routes, SwapConfig(seed=1, max_iterations=2))
    assert final.evaluation.valid
    assert len(final.swaps) == 0


# ---------------------------------------------------------------------------
# Two-layer objective semantics used by every acceptance rule.
# ---------------------------------------------------------------------------
def test_case11_fewer_late_wins_even_with_more_distance():
    fewer_late_longer = Score(1, 1_000.0)
    more_late_shorter = Score(2, 1.0)
    assert fewer_late_longer < more_late_shorter


def test_case12_tie_late_count_compares_distance_only():
    # Same late count: distance breaks the tie; total lateness never enters
    # the Score order (it is a diagnostic only).
    assert Score(2, 0.0, 20.0) < Score(2, 0.0, 30.0)
    assert not (Score(2, 0.0, 30.0) < Score(2, 0.0, 20.0))
    assert Score(5, 0.0, 50.0) < Score(6, 0.0, 1.0)


def test_case13_equal_two_layer_scores_are_equivalent():
    first = Score(3, 100.0)
    second = Score(3, 100.0)
    assert first == second
    assert not (first < second)
    assert not (second < first)


def test_late_rescue_swap_rejects_invalid_geometry():
    problem = _rescue_problem()
    base = swap_solution_from_routes(((1, 3, -3, -1), (2, -2)))
    base_evaluation = evaluate_swap_solution(problem, base)
    # gap_a must be strictly after the pickup of task 1 (index 0).
    bad = build_swap_candidate(base, 1, 2, 0, 1, 0, 1, -2, 0)
    assert bad is None


def test_solve_swap_two_stage_returns_swap_result():
    problem = _rescue_problem()
    result = solve_swap(
        problem,
        alns_config=ALNSConfig(max_iterations=2, seed=1),
        swap_config=SwapConfig(seed=1, max_iterations=2),
        time_limit_seconds=10.0,
        swap_time_share=2.0,
        safety_margin_seconds=1.0,
    )

    assert result.evaluation.valid
    assert result.metadata["method"] == "C2-Lex-SWAP"
    assert result.runtime_seconds <= 10.0 + 1.0
    # Re-evaluation through the swap validator must agree with the result.
    re_eval = evaluate_swap_solution(
        problem, SwapSolution(result.routes, result.swaps)
    )
    assert re_eval.score == result.evaluation.score


def test_solve_swap_rejects_insufficient_stage1_budget():
    problem = _rescue_problem()
    with pytest.raises(ValueError, match="Stage-1"):
        solve_swap(
            problem,
            time_limit_seconds=2.0,
            swap_time_share=2.0,
            safety_margin_seconds=1.0,
        )
