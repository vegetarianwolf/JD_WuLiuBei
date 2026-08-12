import pytest

from uav_dispatch import (
    ALNSConfig,
    Point,
    Problem,
    Task,
    evaluate_solution,
    solve_alns,
)
from uav_dispatch.pair_repair import pair_regret_repair
from uav_dispatch.search import RouteEvaluator


PAIR_ORDERS = (
    (1, -1, 2, -2),
    (1, 2, -1, -2),
    (1, 2, -2, -1),
    (2, -2, 1, -1),
    (2, 1, -2, -1),
    (2, 1, -1, -2),
)


def _problem_favoring(order: tuple[int, ...]) -> Problem:
    matrix = [
        [0.0 if left == right else 100.0 for right in range(5)]
        for left in range(5)
    ]

    def node(visit: int) -> int:
        return 2 * visit - 1 if visit > 0 else 2 * (-visit)

    path = (0,) + tuple(node(visit) for visit in order)
    for left, right in zip(path, path[1:]):
        matrix[left][right] = 1.0
        matrix[right][left] = 1.0
    return Problem(
        (
            Task(1, Point(0, 0), Point(0, 0), deadline_min=1_000),
            Task(2, Point(0, 0), Point(0, 0), deadline_min=1_000),
        ),
        drone_count=1,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1,
        distance_matrix_km=tuple(tuple(row) for row in matrix),
    )


@pytest.mark.parametrize("expected_order", PAIR_ORDERS)
def test_pair_repair_enumerates_each_capacity_two_precedence_order(
    expected_order: tuple[int, ...],
):
    problem = _problem_favoring(expected_order)

    repaired = pair_regret_repair(
        problem,
        RouteEvaluator(problem),
        ((),),
        (1, 2),
        candidate_limit=None,
    )

    assert repaired == (expected_order,)
    assert RouteEvaluator(problem).evaluate(repaired[0]).score.distance_km == 4


def test_pair_repair_falls_back_when_no_route_has_two_free_task_slots():
    tasks = tuple(
        Task(
            task_id,
            Point(float(task_id), 0),
            Point(float(task_id) + 0.5, 0),
            deadline_min=100,
        )
        for task_id in range(1, 7)
    )
    problem = Problem(
        tasks,
        drone_count=3,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1,
    )

    repaired = pair_regret_repair(
        problem,
        RouteEvaluator(problem),
        ((4, -4), (5, -5), (6, -6)),
        (1, 2, 3),
        candidate_limit=None,
    )

    assert evaluate_solution(problem, repaired).valid
    flattened = [visit for route in repaired for visit in route]
    for task_id in problem.task_ids:
        assert flattened.count(task_id) == 1
        assert flattened.count(-task_id) == 1


def test_pair_regret_is_registered_only_when_explicitly_enabled():
    problem = _problem_favoring((1, 2, -1, -2))
    disabled = solve_alns(
        problem,
        config=ALNSConfig(max_iterations=0, enable_pair_repair=False),
    )
    enabled = solve_alns(
        problem,
        config=ALNSConfig(max_iterations=0, enable_pair_repair=True),
    )

    disabled_repairs = {
        name
        for name in disabled.metadata["operator_uses"]
        if name.startswith("repair:")
    }
    enabled_repairs = {
        name
        for name in enabled.metadata["operator_uses"]
        if name.startswith("repair:")
    }
    assert enabled_repairs - disabled_repairs == {"repair:pair_regret"}
    assert disabled_repairs < enabled_repairs
    assert disabled.metadata["enable_pair_repair"] is False
    assert enabled.metadata["enable_pair_repair"] is True
