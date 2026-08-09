from random import Random

import pytest

from uav_dispatch import (
    ALNSConfig,
    Point,
    Problem,
    SearchScore,
    Task,
    construct_regret_initial,
    destroy_solution,
    insert_task_best,
    soft_deadline,
    solve_alns_core,
    routes_search_score,
)
from uav_dispatch.search import RouteEvaluator, route_insertion_options, routes_score


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


def test_temporary_rejection_pool_is_bounded_reinserted_and_never_final() -> None:
    tasks = tuple(
        Task(
            task_id,
            Point(float(task_id), 0),
            Point(float(task_id) + 0.5, 0),
            deadline_min=0.5,
        )
        for task_id in range(1, 11)
    )
    problem = Problem(
        tasks,
        drone_count=1,
        max_tasks_per_drone=10,
        capacity=2,
        speed_km_per_min=1,
    )

    result = solve_alns_core(
        problem,
        config=ALNSConfig(
            max_iterations=1,
            seed=20260805,
            candidate_limit=None,
            enable_rejection_pool=True,
        ),
    )

    assert result.evaluation.valid
    assert result.metadata["rejection_pool_capacity"] == 1
    assert result.metadata["peak_rejected_count"] == 1
    assert result.metadata["rejection_events"] == 1
    assert result.metadata["reinserted_task_count"] == 1
    assert result.metadata["final_rejected_count"] == 0
    visits = [visit for route in result.routes for visit in route]
    for task_id in problem.task_ids:
        assert visits.count(task_id) == visits.count(-task_id) == 1


def test_rejection_pool_does_not_admit_distance_only_improvements() -> None:
    problem = Problem(
        tuple(
            Task(
                task_id,
                Point(float(task_id), 0),
                Point(float(task_id) + 0.25, 0),
                deadline_min=1_000,
            )
            for task_id in range(1, 11)
        ),
        drone_count=1,
        max_tasks_per_drone=10,
        speed_km_per_min=1,
    )

    result = solve_alns_core(
        problem,
        config=ALNSConfig(
            max_iterations=1,
            seed=20260805,
            candidate_limit=None,
            enable_rejection_pool=True,
        ),
    )

    assert result.evaluation.score.late_count == 0
    assert result.evaluation.score.total_lateness_min == 0
    assert result.metadata["rejection_attempts"] == 2
    assert result.metadata["rejection_events"] == 0
    assert result.metadata["peak_rejected_count"] == 0


def test_soft_deadline_adds_beta_service_slack_without_mutating_real_deadline() -> None:
    problem = Problem(
        (Task(1, Point(3, 4), Point(6, 8), deadline_min=20),),
        speed_km_per_min=2,
    )

    assert problem.direct_completion_min(1) == pytest.approx(5)
    assert soft_deadline(problem, 1, 0.15) == pytest.approx(20.75)
    assert soft_deadline(problem, 1, 0.20) == pytest.approx(21.0)
    assert soft_deadline(problem, 1, 0.30) == pytest.approx(21.5)
    assert problem.task(1).deadline_min == 20


def test_internal_search_score_is_lexicographic_and_deadline_weighted() -> None:
    problem = Problem(
        (
            Task(1, Point(2, 0), Point(5, 0), deadline_min=4),
            Task(2, Point(3, 0), Point(6, 0), deadline_min=5),
        ),
        drone_count=2,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    search_score = routes_search_score(
        RouteEvaluator(problem), ((1, -1), (2, -2))
    )

    assert search_score == SearchScore(2, pytest.approx(3), pytest.approx(11))
    assert SearchScore(0, 999, 999) < SearchScore(1, 0, 0)
    assert SearchScore(1, 2, 999) < SearchScore(1, 3, 0)
    assert SearchScore(1, 2, 10) < SearchScore(1, 2, 11)


def test_route_evaluator_can_disable_search_score_without_disabling_official_score() -> None:
    problem = Problem(
        (Task(1, Point(2, 0), Point(5, 0), deadline_min=4),),
        drone_count=1,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    evaluator = RouteEvaluator(problem, enable_search_score=False)

    assert routes_score(evaluator, ((1, -1),)).late_count == 1
    with pytest.raises(RuntimeError, match="search score"):
        routes_search_score(evaluator, ((1, -1),))
    with pytest.raises(RuntimeError, match="search score"):
        _ = evaluator.deadline_priorities


def test_route_insertion_skips_search_guidance_when_it_is_disabled() -> None:
    problem = Problem(
        (Task(1, Point(2, 0), Point(5, 0), deadline_min=4),),
        drone_count=1,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )

    options = route_insertion_options(
        problem,
        RouteEvaluator(problem, enable_search_score=False),
        (),
        0,
        1,
        candidate_limit=None,
    )

    assert len(options) == 1
    assert options[0].delta.late_count == 1
    assert options[0].weighted_lateness_delta == 0


def test_soft_deadline_flag_keeps_final_evaluation_on_the_real_deadline() -> None:
    with pytest.raises(ValueError, match="beta"):
        ALNSConfig(enable_soft_deadline=True, soft_deadline_beta=0.25)

    problem = Problem(
        (Task(1, Point(5, 0), Point(10, 0), deadline_min=9),),
        speed_km_per_min=1,
    )
    result = solve_alns_core(
        problem,
        config=ALNSConfig(
            max_iterations=0,
            enable_soft_deadline=True,
            soft_deadline_beta=0.20,
        ),
    )

    assert soft_deadline(problem, 1, 0.20) == 11
    assert result.evaluation.score.late_count == 1
    assert result.evaluation.score.total_lateness_min == 1
    assert result.metadata["enable_soft_deadline"] is True
    assert result.metadata["soft_deadline_beta"] == 0.20


def test_soft_deadline_changes_destroy_guidance_but_not_real_arrivals() -> None:
    problem = Problem(
        (
            Task(1, Point(100, 0), Point(200, 0), deadline_min=80),
            Task(2, Point(10, 0), Point(20, 0), deadline_min=18),
        ),
        drone_count=2,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    routes = ((1, -1), (2, -2))
    evaluator = RouteEvaluator(problem)

    _, real_removed = destroy_solution(
        problem,
        evaluator,
        routes,
        "late_risk_destroy",
        1,
        Random(1),
    )
    _, soft_removed = destroy_solution(
        problem,
        evaluator,
        routes,
        "late_risk_destroy",
        1,
        Random(1),
        soft_deadline_beta=0.30,
    )

    assert real_removed == (1,)
    assert soft_removed == (2,)
    assert evaluator.evaluate(routes[0]).delivery_times_min[1] == 200
    assert evaluator.evaluate(routes[1]).delivery_times_min[2] == 20


def test_risk_aware_insertion_spends_distance_to_reduce_weighted_lateness() -> None:
    problem = Problem(
        (
            Task(1, Point(0, 12), Point(5, 0), deadline_min=27),
            Task(2, Point(5, 0), Point(6, 0), deadline_min=5),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1,
    )
    base_routes = ((1, -1), ())

    inserted = insert_task_best(
        problem,
        base_routes,
        2,
        risk_aware=True,
        soft_deadline_beta=0.20,
        lateness_lambda=1.0,
    )

    assert inserted.route_index == 1
    assert inserted.routes == ((1, -1), (2, -2))
    assert inserted.evaluation.valid
    assert inserted.evaluation.score.distance_km == pytest.approx(31)


def test_soft_deadline_insertion_guides_every_delivery_on_the_changed_route() -> None:
    problem = Problem(
        (
            Task(1, Point(5, 0), Point(10, 0), deadline_min=9),
            Task(2, Point(0, 3), Point(0, 4), deadline_min=4),
        ),
        drone_count=1,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1,
    )
    evaluator = RouteEvaluator(problem)

    options = route_insertion_options(
        problem,
        evaluator,
        (1, -1),
        0,
        2,
        candidate_limit=None,
        option_count=100,
        risk_aware=True,
        soft_deadline_beta=0.30,
    )
    detour_before_existing_task = next(
        option for option in options if option.route == (2, -2, 1, -1)
    )

    # Task 1's true lateness rises from 1 to sqrt(41) + 1, but its guided
    # deadline is 12, so the all-delivery soft delta is sqrt(41) - 3.
    assert detour_before_existing_task.weighted_lateness_delta == pytest.approx(
        41**0.5 - 3
    )
    assert detour_before_existing_task.delta.total_lateness_min == pytest.approx(
        41**0.5 - 1
    )


def test_soft_deadline_enables_internal_score_and_risk_aware_repair() -> None:
    with pytest.raises(ValueError, match="lambda"):
        ALNSConfig(risk_aware_lateness_lambda=-1)

    problem = Problem(
        tuple(
            Task(
                task_id,
                Point(float(task_id), 0),
                Point(float(task_id) + 0.5, 0),
                deadline_min=2 + task_id,
            )
            for task_id in range(1, 5)
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        speed_km_per_min=1,
    )
    result = solve_alns_core(
        problem,
        config=ALNSConfig(
            max_iterations=1,
            candidate_limit=None,
            enable_soft_deadline=True,
            risk_aware_lateness_lambda=1.5,
        ),
    )

    assert result.evaluation.valid
    assert result.metadata["internal_search_score_enabled"] is True
    assert result.metadata["risk_aware_insertion_enabled"] is True
    assert result.metadata["risk_aware_lateness_lambda"] == 1.5
    assert result.metadata["search_objective_order"] == (
        "late_count",
        "weighted_lateness_min",
        "distance_km",
    )
