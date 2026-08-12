"""Tests for the Swap-only explicit data structures."""

from __future__ import annotations

import pytest

from uav_dispatch.model import Point, Problem, Task
from uav_dispatch.search import RouteEvaluator, routes_score
from uav_dispatch.swap_model import (
    SwapEvent,
    SwapSolution,
    gaps_for_route,
    meeting_node_label,
    swap_solution_from_routes,
)


def _problem() -> Problem:
    return Problem(
        (
            Task(1, Point(1, 0), Point(2, 0), 10),
            Task(2, Point(1, 1), Point(2, 1), 10),
            Task(3, Point(0, 2), Point(3, 2), 10),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1,
    )


def test_swap_event_requires_two_distinct_drones():
    with pytest.raises(ValueError, match="不同无人机"):
        SwapEvent(0, 1, 1, 1, 2, 1, 1, 1)


def test_swap_event_rejects_same_task_exchange():
    with pytest.raises(ValueError, match="同一个任务"):
        SwapEvent(0, 0, 1, 1, 1, 1, 1, 1)


def test_swap_event_rejects_non_positive_tasks_and_negative_positions():
    with pytest.raises(ValueError, match="正整数"):
        SwapEvent(0, 0, 1, -1, 2, 1, 1, 1)
    with pytest.raises(ValueError, match="不能为负"):
        SwapEvent(0, 0, 1, 1, 2, 1, -1, 1)


def test_swap_event_meeting_node_cannot_be_the_depot():
    with pytest.raises(ValueError, match="不能是起点"):
        SwapEvent(0, 0, 1, 1, 2, 0, 1, 1)


def test_swap_solution_from_routes_has_no_swaps():
    problem = _problem()
    routes = ((1, -1), (2, -2))
    solution = swap_solution_from_routes(routes)

    assert solution.swap_count == 0
    assert solution.routes == routes
    # A zero-swap solution must evaluate identically to the plain model.
    evaluator = RouteEvaluator(problem)
    plain = routes_score(evaluator, routes)
    assert plain == evaluator.evaluate((1, -1)).score + evaluator.evaluate(
        (2, -2)
    ).score


def test_swap_solution_rejects_duplicate_swap_ids():
    routes = ((1, 2, -2, -1), ())
    first = SwapEvent(0, 0, 1, 1, 2, 1, 1, 1)
    duplicate = SwapEvent(0, 0, 1, 1, 2, 1, 1, 1)
    with pytest.raises(ValueError, match="重复"):
        SwapSolution(routes, (first, duplicate))


def test_swap_solution_requires_at_least_one_route():
    with pytest.raises(ValueError, match="至少包含一条路线"):
        SwapSolution(())


def test_meeting_node_labels():
    assert meeting_node_label(37) == "P37"
    assert meeting_node_label(-143) == "D143"


def test_gaps_for_route():
    assert gaps_for_route((1, 2, -2, -1)) == (0, 1, 2, 3, 4)
    assert gaps_for_route(()) == (0,)
