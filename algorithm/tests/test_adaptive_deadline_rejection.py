from random import Random

import pytest

from uav_dispatch import (
    ALNSConfig,
    Point,
    Problem,
    Task,
    construct_regret_initial,
    destroy_solution,
    solve_alns_core,
)
from uav_dispatch.search import RouteEvaluator


def test_late_risk_destroy_removes_the_highest_risk_complete_pair() -> None:
    problem = Problem(
        (
            Task(1, Point(6, 0), Point(12, 0), deadline_min=10),
            Task(2, Point(9, 0), Point(18, 0), deadline_min=20),
            Task(3, Point(14, 0), Point(28, 0), deadline_min=30),
        ),
        drone_count=3,
        max_tasks_per_drone=1,
        capacity=2,
        speed_km_per_min=1,
    )
    routes = ((1, -1), (2, -2), (3, -3))

    partial, removed = destroy_solution(
        problem,
        RouteEvaluator(problem),
        routes,
        "late_risk_destroy",
        1,
        Random(20260805),
    )

    # Task 1 has the largest prescribed weighted risk: it is late, has the
    # tightest deadline, and all three single-task routes have equal detour gain.
    assert removed == (1,)
    assert partial == ((), (2, -2), (3, -3))
    assert all(
        route.count(task_id) == route.count(-task_id)
        for route in partial
        for task_id in problem.task_ids
    )


def test_late_risk_destroy_feature_flag_controls_the_operator_pool() -> None:
    problem = Problem(
        (
            Task(1, Point(1, 0), Point(2, 0), deadline_min=3),
            Task(2, Point(2, 0), Point(3, 0), deadline_min=4),
        ),
        drone_count=1,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1,
    )
    initial = construct_regret_initial(problem, candidate_limit=None)

    disabled = solve_alns_core(
        problem,
        config=ALNSConfig(max_iterations=0, enable_late_risk_destroy=False),
        initial_routes=initial.routes,
    )
    enabled = solve_alns_core(
        problem,
        config=ALNSConfig(max_iterations=0, enable_late_risk_destroy=True),
        initial_routes=initial.routes,
    )

    assert "destroy:late_risk_destroy" not in disabled.metadata["operator_uses"]
    assert "destroy:late_risk_destroy" in enabled.metadata["operator_uses"]
    assert disabled.metadata["enable_late_risk_destroy"] is False
    assert enabled.metadata["enable_late_risk_destroy"] is True


def test_late_risk_weights_are_validated_and_reported() -> None:
    with pytest.raises(ValueError, match="late-risk"):
        ALNSConfig(late_risk_lateness_weight=-0.1)

    problem = Problem(
        (Task(1, Point(1, 0), Point(2, 0), deadline_min=3),),
        drone_count=1,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    result = solve_alns_core(
        problem,
        config=ALNSConfig(
            max_iterations=0,
            enable_late_risk_destroy=True,
            late_risk_lateness_weight=0.6,
            late_risk_deadline_weight=0.25,
            late_risk_detour_weight=0.15,
        ),
    )

    assert result.metadata["late_risk_weights"] == (0.6, 0.25, 0.15)
