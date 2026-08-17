import random

import pytest

from uav_dispatch import (
    Point,
    Problem,
    Task,
    evaluate_solution,
    insert_task_best,
    solve_exact,
)


def test_exact_solver_reproduces_the_reports_11_km_example():
    # Node order is depot, P1, D1, P2, D2.  The non-zero distances are the
    # symmetric matrix implied by the six route totals in the report.
    matrix = (
        (0, 5, 9, 8, 9),
        (5, 0, 4, 1, 5),
        (9, 4, 0, 4, 3),
        (8, 1, 4, 0, 2),
        (9, 5, 3, 2, 0),
    )
    tasks = (
        Task(1, Point(0, 0), Point(0, 0), 100),
        Task(2, Point(0, 0), Point(0, 0), 100),
    )
    problem = Problem(
        tasks,
        drone_count=1,
        max_tasks_per_drone=2,
        distance_matrix_km=matrix,
    )

    result = solve_exact(problem)

    assert result.routes == ((1, 2, -2, -1),)
    assert result.evaluation.valid
    assert result.evaluation.score.distance_km == pytest.approx(11.0)


def test_best_pair_insertion_uses_capacity_two_without_forcing_direct_service():
    tasks = (
        Task(1, Point(1, 0), Point(4, 0), 100),
        Task(2, Point(2, 0), Point(3, 0), 100),
    )
    problem = Problem(tasks, drone_count=1, max_tasks_per_drone=2)

    inserted = insert_task_best(problem, ((1, -1),), task_id=2)
    evaluation = evaluate_solution(problem, inserted.routes)

    assert evaluation.valid
    assert inserted.routes == ((1, 2, -2, -1),)
    assert evaluation.score.distance_km == pytest.approx(4.0)


def test_exact_solver_respects_lexicographic_deadline_priority():
    tasks = (
        Task(1, Point(1, 0), Point(10, 0), deadline_min=10 / 0.9),
        Task(2, Point(-1, 0), Point(-2, 0), deadline_min=100),
    )
    problem = Problem(tasks, drone_count=1, max_tasks_per_drone=2)

    result = solve_exact(problem)

    assert result.evaluation.score.late_count == 0
    assert result.evaluation.score.distance_km == pytest.approx(22.0)


def test_exact_solver_rejects_relay_or_non_origin_semantics():
    tasks = (Task(1, Point(1, 0), Point(2, 0), 100),)
    non_origin = Problem(
        tasks,
        drone_count=1,
        max_tasks_per_drone=1,
        drone_homes=(Point(1, 1),),
    )
    with pytest.raises(ValueError, match="原点起飞"):
        solve_exact(non_origin)

    from uav_dispatch import RelayStation, TransportLeg

    relay = Problem(
        tasks,
        drone_count=1,
        max_tasks_per_drone=1,
        relay_stations=(RelayStation(1, 0.5, 0.0),),
        leg_registry={
            2: TransportLeg(2, 1, "RELAY_IN", 1),
            3: TransportLeg(3, 1, "RELAY_OUT", 1),
        },
        task_relay_candidates={1: (1,)},
    )
    with pytest.raises(ValueError, match="Direct"):
        solve_exact(relay)


def test_best_insertion_rejects_more_routes_than_available_drones():
    tasks = (
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(3, 0), Point(4, 0), 100),
    )
    problem = Problem(tasks, drone_count=1, max_tasks_per_drone=2)

    with pytest.raises(ValueError, match="路线数"):
        insert_task_best(problem, ((1, -1), ()), task_id=2)


def test_exact_pareto_dp_matches_direct_enumeration_on_random_instances():
    def enumerate_routes(problem):
        best = None

        def visit(route, unpicked, onboard):
            nonlocal best
            if not unpicked and not onboard:
                evaluation = evaluate_solution(problem, (tuple(route),))
                candidate = (evaluation.score, tuple(route))
                if best is None or candidate < best:
                    best = candidate
                return
            if len(onboard) < problem.capacity:
                for task_id in sorted(unpicked):
                    visit(
                        route + [task_id],
                        unpicked - {task_id},
                        onboard | {task_id},
                    )
            for task_id in sorted(onboard):
                visit(route + [-task_id], unpicked, onboard - {task_id})

        visit([], set(problem.task_ids), set())
        return best

    for seed in range(5):
        rng = random.Random(seed)
        tasks = tuple(
            Task(
                task_id,
                Point(rng.uniform(-3, 3), rng.uniform(-3, 3)),
                Point(rng.uniform(-3, 3), rng.uniform(-3, 3)),
                rng.uniform(3, 18),
            )
            for task_id in range(1, 5)
        )
        problem = Problem(tasks, max_tasks_per_drone=4)

        expected_score, expected_route = enumerate_routes(problem)
        actual = solve_exact(problem)

        assert actual.evaluation.score == expected_score
        assert actual.routes[0] == expected_route


def test_exact_dominance_never_discards_a_slightly_earlier_future_clock():
    delta = 5e-13
    size = 9
    matrix = [[1000.0] * size for _ in range(size)]
    for index in range(size):
        matrix[index][index] = 0.0
    for left, right, distance in (
        (0, 1, 1.0),
        (1, 2, 1.0),
        (2, 3, 4.0),
        (3, 4, 4.0 + delta),
        (0, 3, 4.0),
        (3, 1, 4.0),
        (2, 4, 1.0),
        (4, 5, 1.0),
        (5, 6, 1.0),
        (6, 7, 1.0),
        (7, 8, 1.0),
    ):
        matrix[left][right] = distance
    tasks = (
        Task(1, Point(0, 0), Point(0, 0), 5.0),
        Task(2, Point(0, 0), Point(0, 0), 100.0),
        Task(3, Point(0, 0), Point(0, 0), 12 - 1e-9 + delta / 2),
        Task(4, Point(0, 0), Point(0, 0), 14 - 1e-9 + delta / 2),
    )
    problem = Problem(
        tasks,
        max_tasks_per_drone=4,
        speed_km_per_min=1.0,
        distance_matrix_km=tuple(tuple(row) for row in matrix),
    )

    result = solve_exact(problem)

    assert result.routes == ((2, 1, -1, -2, 3, -3, 4, -4),)
    assert result.evaluation.score.late_count == 1
    assert result.evaluation.score.total_lateness_min == pytest.approx(4.0)
    assert result.evaluation.score.distance_km == pytest.approx(14.0)
