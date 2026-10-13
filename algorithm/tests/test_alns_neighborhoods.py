from random import Random

from uav_dispatch import Point, Problem, Task, destroy_solution
from uav_dispatch.alns import DESTROY_OPERATORS, REPAIR_OPERATORS
from uav_dispatch.search import RouteEvaluator


def _two_full_uavs_problem() -> tuple[Problem, tuple[tuple[int, ...], ...]]:
    tasks = tuple(
        Task(
            task_id,
            Point(float(task_id), float(task_id % 2)),
            Point(float(task_id) + 0.5, float(task_id % 2) + 0.5),
            deadline_min=5.0 + task_id,
        )
        for task_id in range(1, 5)
    )
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=2, capacity=2)
    return problem, ((1, -1, 2, -2), (3, -3, 4, -4))


def test_assignment_destroy_accounts_for_every_task_pair_exactly_once():
    problem, routes = _two_full_uavs_problem()

    partial, removed = destroy_solution(
        problem,
        RouteEvaluator(problem),
        routes,
        "assignment_destroy",
        2,
        Random(20260805),
    )

    assert len(removed) == len(set(removed)) == 2
    original_route = {
        task_id: route_index
        for route_index, route in enumerate(routes)
        for task_id in route
        if task_id > 0
    }
    assert len({original_route[task_id] for task_id in removed}) == 2

    remaining_visits = [visit for route in partial for visit in route]
    for task_id in problem.task_ids:
        expected = 0 if task_id in removed else 1
        assert remaining_visits.count(task_id) == expected
        assert remaining_visits.count(-task_id) == expected
    assert {abs(visit) for visit in remaining_visits}.isdisjoint(removed)
    assert {abs(visit) for visit in remaining_visits} | set(removed) == set(
        problem.task_ids
    )


def test_deadline_risk_changes_critical_destroy_order_without_changing_score():
    problem = Problem(
        (
            Task(1, Point(89, 0), Point(100, 0), deadline_min=110),
            Task(2, Point(1, 0), Point(2, 0), deadline_min=3),
        ),
        drone_count=2,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    routes = ((1, -1), (2, -2))
    evaluator = RouteEvaluator(problem)

    _, risk_removed = destroy_solution(
        problem,
        evaluator,
        routes,
        "late_critical",
        1,
        Random(1),
        use_deadline_risk=True,
    )
    _, slack_removed = destroy_solution(
        problem,
        evaluator,
        routes,
        "late_critical",
        1,
        Random(1),
        use_deadline_risk=False,
    )

    assert risk_removed == (1,)  # risk 100/110 > 2/3
    assert slack_removed == (2,)  # slack 3-2 < 110-100
    assert evaluator.evaluate(routes[0]).score.late_count == 0
    assert evaluator.evaluate(routes[1]).score.late_count == 0


def test_alns_operator_catalog_is_exactly_the_final_nine_plus_five():
    assert DESTROY_OPERATORS == (
        "random",
        "worst_distance",
        "worst_lex",
        "spatial_related",
        "deadline_related",
        "late_critical",
        "route_segment",
        "capacity_conflict",
        "assignment_destroy",
    )
    assert REPAIR_OPERATORS == (
        "greedy",
        "regret2",
        "regret3",
        "deadline",
        "slack",
    )
