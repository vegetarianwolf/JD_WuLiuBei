from uav_dispatch import Point, Problem, Task
from uav_dispatch.pair_repair import pair_insertion_options, pair_regret_repair
from uav_dispatch.propagation_eval import (
    evaluate_insertion_propagation,
    evaluate_route_propagation,
)
from uav_dispatch.search import RouteEvaluator


def _downstream_delay_problem() -> Problem:
    matrix = [
        [0.0 if left == right else 100.0 for right in range(5)]
        for left in range(5)
    ]
    for left, right in ((0, 1), (1, 2), (2, 3), (3, 4), (0, 3)):
        matrix[left][right] = 1.0
        matrix[right][left] = 1.0
    return Problem(
        (
            Task(1, Point(0, 0), Point(0, 0), deadline_min=100),
            Task(2, Point(0, 0), Point(0, 0), deadline_min=2.5),
        ),
        drone_count=1,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1,
        distance_matrix_km=tuple(tuple(row) for row in matrix),
    )


def test_route_propagation_matches_the_exact_route_evaluator():
    problem = _downstream_delay_problem()
    route = (1, -1, 2, -2)

    propagation = evaluate_route_propagation(route, problem=problem)
    exact = RouteEvaluator(problem).evaluate(route)

    assert propagation.score == exact.score
    assert propagation.arrival_times_min == (1.0, 2.0, 3.0, 4.0)
    assert dict(propagation.delivery_times_min) == {1: 2.0, 2: 4.0}
    assert dict(propagation.lateness_by_task_min) == {1: 0.0, 2: 1.5}


def test_insertion_propagation_reports_future_delivery_delay_and_cost_deltas():
    problem = _downstream_delay_problem()

    impact = evaluate_insertion_propagation(
        (2, -2),
        (1, -1, 2, -2),
        inserted_task_ids=(1,),
        problem=problem,
    )

    assert impact.delta_distance_km == 2.0
    assert impact.delta_late_count == 1
    assert impact.delta_lateness_min == 1.5
    assert impact.future_delivery_delay_min == 2.0
    assert impact.future_lateness_delta_min == 1.5
    assert dict(impact.downstream_delivery_delays_min) == {2: 2.0}


def _lexicographic_pair_problem() -> Problem:
    matrix = [
        [0.0 if left == right else 100.0 for right in range(7)]
        for left in range(7)
    ]
    for left, right, distance in (
        (0, 1, 1.0),
        (1, 2, 1.0),
        (2, 3, 1.0),
        (3, 4, 1.0),
        (4, 5, 1.0),
        (5, 6, 1.0),
        (0, 5, 1.0),
        (6, 1, 20.0),
    ):
        matrix[left][right] = distance
        matrix[right][left] = distance
    return Problem(
        (
            Task(1, Point(0, 0), Point(0, 0), deadline_min=1_000),
            Task(2, Point(0, 0), Point(0, 0), deadline_min=1_000),
            Task(3, Point(0, 0), Point(0, 0), deadline_min=2.5),
        ),
        drone_count=1,
        max_tasks_per_drone=3,
        capacity=2,
        speed_km_per_min=1,
        distance_matrix_km=tuple(tuple(row) for row in matrix),
    )


def test_pair_repair_uses_propagation_without_overriding_lexicographic_score():
    problem = _lexicographic_pair_problem()
    evaluator = RouteEvaluator(problem)
    options = pair_insertion_options(
        problem,
        evaluator,
        ((3, -3),),
        (1, 2),
        candidate_limit=None,
        option_count=100,
    )

    bait = next(
        option
        for option in options
        if option.route == (1, -1, 2, -2, 3, -3)
    )
    assert bait.propagation.delta_distance_km == 4.0
    assert bait.propagation.delta_lateness_min == 3.5
    assert bait.propagation.future_delivery_delay_min == 4.0

    repaired = pair_regret_repair(
        problem,
        evaluator,
        ((3, -3),),
        (1, 2),
        candidate_limit=None,
    )
    assert repaired == ((3, -3, 1, -1, 2, -2),)
    assert evaluator.evaluate(repaired[0]).score.late_count == 0
    assert evaluator.evaluate(repaired[0]).score.distance_km == 25.0
