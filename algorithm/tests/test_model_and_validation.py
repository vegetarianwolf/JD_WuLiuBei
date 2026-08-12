from pathlib import Path
from math import inf, nan

import pytest

from uav_dispatch import Point, Problem, Score, Task, evaluate_solution, load_tasks_csv


DATA_FILE = (
    Path(__file__).parents[2]
    / "data" / "raw" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)


def test_loads_the_supplied_200_task_csv_with_its_excel_encoding():
    tasks = load_tasks_csv(DATA_FILE)

    assert len(tasks) == 200
    assert tasks[0] == Task(1, Point(3.62, 7.66), Point(6.12, 5.19), 30.0)
    assert tasks[-1].id == 200
    assert tasks[-1].deadline_min == pytest.approx(34.3)


def test_open_route_uses_15_mps_and_does_not_add_a_return_leg():
    task = Task(1, Point(3, 4), Point(6, 8), deadline_min=10)
    problem = Problem((task,), drone_count=1, max_tasks_per_drone=1)

    result = evaluate_solution(problem, ((1, -1),))

    assert result.valid
    assert result.score.distance_km == pytest.approx(10.0)
    assert result.delivery_times_min[1] == pytest.approx(10 / 0.9)
    assert result.score.late_count == 1
    assert result.total_lateness_min == pytest.approx((10 / 0.9) - 10)


def test_full_validator_rejects_precedence_capacity_and_missing_tasks():
    tasks = (
        Task(1, Point(1, 0), Point(4, 0), 100),
        Task(2, Point(2, 0), Point(5, 0), 100),
        Task(3, Point(3, 0), Point(6, 0), 100),
    )
    problem = Problem(tasks, drone_count=1, max_tasks_per_drone=3, capacity=2)

    precedence = evaluate_solution(problem, ((-1, 1, 2, -2, 3, -3),))
    capacity = evaluate_solution(problem, ((1, 2, 3, -1, -2, -3),))
    missing = evaluate_solution(problem, ((1, -1, 2, -2),))

    assert not precedence.valid
    assert any("取件之前" in item for item in precedence.violations)
    assert not capacity.valid
    assert any("载荷" in item for item in capacity.violations)
    assert not missing.valid
    assert any("缺少任务" in item for item in missing.violations)


def test_lexicographic_score_never_trades_a_late_order_for_distance():
    fewer_late_but_longer = Score(0, 1000.0)
    more_late_but_shorter = Score(1, 1.0)

    assert fewer_late_but_longer < more_late_but_shorter


def test_score_breaks_late_count_ties_by_distance_only():
    # Two-layer objective (late_count, distance_km): total_lateness never
    # participates in the Score order; distance breaks late-count ties.
    # CASE 19: fewer late wins even with much more distance.
    assert Score(5, 0.0, 100.0) < Score(6, 0.0, 1.0)
    # CASE 20: same late count -> shorter distance wins regardless of the
    # (diagnostic-only) total lateness.
    assert Score(5, 0.0, 50.0) < Score(5, 0.0, 60.0)
    assert not (Score(5, 0.0, 60.0) < Score(5, 0.0, 50.0))
    # total_lateness does NOT break a tie when (late_count, distance) equal.
    assert not (Score(5, 20.0, 30.0) < Score(5, 10.0, 30.0))
    assert not (Score(5, 10.0, 30.0) < Score(5, 20.0, 30.0))
    assert Score(2, 0.0, 20.0) < Score(2, 0.0, 30.0)


@pytest.mark.parametrize(
    "builder",
    [
        lambda: Point(nan, 0),
        lambda: Point(0, inf),
        lambda: Task(1, Point(0, 0), Point(1, 1), nan),
        lambda: Problem(
            (Task(1, Point(0, 0), Point(1, 1), 10),),
            speed_km_per_min=inf,
        ),
        lambda: Problem(
            (Task(1, Point(0, 0), Point(1, 1), 10),),
            distance_matrix_km=((0, nan, 1), (nan, 0, 1), (1, 1, 0)),
        ),
    ],
)
def test_model_rejects_non_finite_numeric_inputs(builder):
    with pytest.raises(ValueError):
        builder()


def test_explicit_matrix_node_order_is_stable_for_unsorted_tasks():
    matrix = (
        (0, 5, 9, 8, 9),
        (5, 0, 4, 1, 5),
        (9, 4, 0, 4, 3),
        (8, 1, 4, 0, 2),
        (9, 5, 3, 2, 0),
    )
    task_1 = Task(1, Point(0, 0), Point(0, 0), 100)
    task_2 = Task(2, Point(0, 0), Point(0, 0), 100)

    problem = Problem(
        (task_2, task_1),
        max_tasks_per_drone=2,
        distance_matrix_km=matrix,
    )

    assert problem.task_ids == (1, 2)
    assert problem.distance(None, 1) == 5
    assert problem.distance(None, 2) == 8
