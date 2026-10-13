import random

from uav_dispatch import Point, Problem, Task, evaluate_solution, insert_task_best
from uav_dispatch.search import feasible_pair_positions, insert_pair


def _random_legal_route(task_ids, rng, capacity=2):
    unpicked = list(task_ids)
    onboard = []
    route = []
    while unpicked or onboard:
        actions = []
        if len(onboard) < capacity:
            actions.extend(("pickup", task_id) for task_id in unpicked)
        actions.extend(("delivery", task_id) for task_id in onboard)
        action, task_id = rng.choice(actions)
        if action == "pickup":
            unpicked.remove(task_id)
            onboard.append(task_id)
            route.append(task_id)
        else:
            onboard.remove(task_id)
            route.append(-task_id)
    return tuple(route)


def _slow_insert(route, task_id, pickup_position, delivery_position):
    result = list(route)
    result.insert(pickup_position, task_id)
    result.insert(delivery_position, -task_id)
    return tuple(result)


def test_random_pair_positions_match_the_independent_validator():
    for seed in range(32):
        rng = random.Random(seed)
        old_count = rng.randint(1, 6)
        new_task_id = old_count + 1
        tasks = tuple(
            Task(task_id, Point(task_id, 0), Point(task_id, 0.25), 10_000)
            for task_id in range(1, new_task_id + 1)
        )
        problem = Problem(
            tasks,
            drone_count=1,
            max_tasks_per_drone=len(tasks),
            capacity=2,
        )
        base_route = _random_legal_route(range(1, new_task_id), rng)
        advertised = set(feasible_pair_positions(problem, base_route))
        independently_valid = set()
        for pickup_position in range(len(base_route) + 1):
            for delivery_position in range(
                pickup_position + 1, len(base_route) + 2
            ):
                candidate = _slow_insert(
                    base_route,
                    new_task_id,
                    pickup_position,
                    delivery_position,
                )
                assert insert_pair(
                    base_route,
                    new_task_id,
                    pickup_position,
                    delivery_position,
                ) == candidate
                if evaluate_solution(problem, (candidate,)).valid:
                    independently_valid.add(
                        (pickup_position, delivery_position)
                    )
        assert advertised == independently_valid


def test_random_best_insertion_matches_full_route_bruteforce():
    for seed in range(24):
        rng = random.Random(seed)
        old_count = rng.randint(1, 5)
        new_task_id = old_count + 1
        node_count = 1 + 2 * new_task_id
        matrix = [[0.0] * node_count for _ in range(node_count)]
        for left in range(node_count):
            for right in range(left + 1, node_count):
                distance = rng.uniform(0.01, 20.0) + (
                    left * node_count + right
                ) * 1e-7
                matrix[left][right] = distance
                matrix[right][left] = distance
        tasks = tuple(
            Task(task_id, Point(0, 0), Point(0, 0), rng.uniform(1, 100))
            for task_id in range(1, new_task_id + 1)
        )
        problem = Problem(
            tasks,
            drone_count=1,
            max_tasks_per_drone=len(tasks),
            distance_matrix_km=tuple(tuple(row) for row in matrix),
        )
        base_route = _random_legal_route(range(1, new_task_id), rng)
        candidates = []
        for pickup_position in range(len(base_route) + 1):
            for delivery_position in range(
                pickup_position + 1, len(base_route) + 2
            ):
                candidate = _slow_insert(
                    base_route,
                    new_task_id,
                    pickup_position,
                    delivery_position,
                )
                evaluation = evaluate_solution(problem, (candidate,))
                if evaluation.valid:
                    candidates.append(
                        (
                            evaluation.score,
                            pickup_position,
                            delivery_position,
                            candidate,
                        )
                    )
        expected = min(candidates)
        actual = insert_task_best(problem, (base_route,), new_task_id)
        assert (
            actual.evaluation.score,
            actual.pickup_position,
            actual.delivery_position,
            actual.routes[0],
        ) == expected


def test_validator_rejects_a_task_split_across_two_drones():
    problem = Problem(
        (Task(1, Point(1, 0), Point(2, 0), 100),),
        drone_count=2,
        max_tasks_per_drone=1,
    )

    result = evaluate_solution(problem, ((1,), (-1,)))

    assert not result.valid
    assert any("路线结束时载荷" in item for item in result.violations)
    assert any("取件之前" in item for item in result.violations)
