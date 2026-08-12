from uav_dispatch import Point, Problem, Task
from uav_dispatch.interaction_graph import (
    build_interaction_graph,
    evaluate_task_pair,
    rank_interaction_tasks,
)


def test_pair_evaluation_enumerates_every_capacity_two_precedence_order():
    task_a = Task(1, Point(1, 0), Point(3, 0), deadline_min=100)
    task_b = Task(2, Point(1, 1), Point(3, 1), deadline_min=100)

    interaction = evaluate_task_pair(task_a, task_b)

    assert len(interaction.feasible_orders) == 6
    assert interaction.pickup_delivery_feasibility == 1.0
    assert interaction.distance_saving_km > 0
    for order in interaction.feasible_orders:
        assert order.index(1) < order.index(-1)
        assert order.index(2) < order.index(-2)
        load = 0
        for visit in order:
            load += 1 if visit > 0 else -1
            assert 0 <= load <= 2


def test_graph_ranks_a_tight_capacity_overlap_ahead_of_a_slack_task():
    problem = Problem(
        (
            Task(1, Point(1, 0), Point(2, 0), deadline_min=2),
            Task(2, Point(-1, 0), Point(-2, 0), deadline_min=2),
            Task(3, Point(8, 0), Point(9, 0), deadline_min=100),
        ),
        drone_count=1,
        max_tasks_per_drone=3,
        capacity=2,
        speed_km_per_min=1,
    )

    graph = build_interaction_graph(
        problem,
        ((1, 2, -1, -2, 3, -3),),
    )

    assert graph.nodes == (1, 2, 3)
    assert len(graph.edges) == 3
    assert graph.edge(1, 2).deadline_conflict_risk > 0
    assert graph.edge(1, 2).capacity_overlap
    assert set(rank_interaction_tasks(graph)[:2]) == {1, 2}


def test_graph_uses_the_problem_distance_matrix_for_pair_savings():
    matrix = [
        [0.0 if left == right else 50.0 for right in range(5)]
        for left in range(5)
    ]
    for left, right, distance in (
        (0, 1, 5.0),
        (1, 2, 5.0),
        (0, 3, 5.0),
        (3, 4, 5.0),
        (1, 3, 1.0),
        (3, 2, 1.0),
        (2, 4, 1.0),
    ):
        matrix[left][right] = distance
        matrix[right][left] = distance
    problem = Problem(
        (
            Task(1, Point(0, 0), Point(0, 0), deadline_min=100),
            Task(2, Point(0, 0), Point(0, 0), deadline_min=100),
        ),
        drone_count=1,
        max_tasks_per_drone=2,
        speed_km_per_min=1,
        distance_matrix_km=tuple(tuple(row) for row in matrix),
    )

    graph = build_interaction_graph(problem, ((1, 2, -1, -2),))

    assert graph.edge(1, 2).distance_saving_km == 12.0
