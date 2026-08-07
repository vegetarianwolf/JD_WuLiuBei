from uav_dispatch import (
    Point,
    Problem,
    Task,
    construct_edd_adjacent,
    construct_greedy_initial,
    construct_nearest_adjacent,
)


def _problem():
    tasks = tuple(
        Task(
            task_id,
            Point(task_id, task_id % 2),
            Point(task_id + 0.5, 1 + task_id % 3),
            5 + task_id,
        )
        for task_id in range(1, 7)
    )
    return Problem(tasks, drone_count=2, max_tasks_per_drone=3)


def test_adjacent_baselines_and_full_position_greedy_return_valid_solutions():
    problem = _problem()

    results = (
        construct_edd_adjacent(problem),
        construct_nearest_adjacent(problem),
        construct_greedy_initial(problem, candidate_limit=None),
    )

    assert all(result.evaluation.valid for result in results)
    for result in results[:2]:
        for route in result.routes:
            assert all(route[index] == -route[index + 1] for index in range(0, len(route), 2))
