import math

import pytest

from algorithm.experiments.generate_relay_scenarios import (
    SCENARIO_CODES,
    generate_scenario,
)


@pytest.mark.parametrize("code", SCENARIO_CODES)
def test_synthetic_scenarios_are_deterministic_euclidean_metrics(code) -> None:
    left = generate_scenario(code, task_count=12, seed=20260810)
    right = generate_scenario(code, task_count=12, seed=20260810)

    assert left == right
    assert len(left.tasks) == 12
    assert len(left.candidate_hubs) >= 8

    problem = left.to_problem(drone_count=2, max_tasks_per_drone=6)
    task = left.tasks[0]
    expected = math.hypot(
        task.pickup.x - task.delivery.x,
        task.pickup.y - task.delivery.y,
    )
    assert problem.distance(task.id, -task.id) == pytest.approx(expected)

    all_points = [
        point
        for item in left.tasks
        for point in (item.pickup, item.delivery)
    ]
    pair_distances = [
        first.distance_to(second)
        for index, first in enumerate(all_points)
        for second in all_points[index + 1 :]
        if first != second
    ]
    assert max(pair_distances) > 12.0
