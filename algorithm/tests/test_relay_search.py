from uav_dispatch import Point, Problem, Task
from uav_dispatch.relay_search import generate_flow_hubs


def test_flow_hubs_are_deterministic_centres_of_task_corridors() -> None:
    problem = Problem(
        (
            Task(1, Point(-10, -1), Point(-10, 1), 100),
            Task(2, Point(-8, -1), Point(-8, 1), 100),
            Task(3, Point(8, -1), Point(8, 1), 100),
            Task(4, Point(10, -1), Point(10, 1), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
    )

    hubs = generate_flow_hubs(problem, 2)

    assert tuple(hub.id for hub in hubs) == ("flow-1", "flow-2")
    assert tuple(hub.point for hub in hubs) == (Point(-9, 0), Point(9, 0))
