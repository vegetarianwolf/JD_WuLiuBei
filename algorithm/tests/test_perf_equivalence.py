"""Equivalence tests for the performance refactors of the insertion engine.

Each refactoring must reproduce the pre-optimization semantics exactly:
the RouteProfile capacity query, the bounded candidate screening, and the
raw insertion-delta primitive are compared against independent reference
implementations on randomized inputs.
"""

import random

from uav_dispatch import (
    Point,
    Problem,
    RelayStation,
    Task,
    TransportLeg,
    evaluate_solution,
)
from uav_dispatch.counters import SearchCounters
from uav_dispatch.search import (
    RouteEvaluator,
    RouteProfile,
    _array_distance_delta,
    _build_distance_delta_arrays,
    _can_insert_pair,
    _delivery_event_count,
    _node_distance_delta,
    _pair_insertion_delta_raw,
    _pair_removal_delta_exact,
    _scored_distance_deltas,
    _select_candidate_positions,
    _visits_removal_delta_exact,
    pair_insertion_delta,
    route_insertion_surface,
)


def _random_legal_route(task_ids, rng, capacity=2):
    """Random capacity-legal pickup/delivery route over the given task ids."""
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


def _random_problem_and_route(seed):
    rng = random.Random(seed)
    old_count = rng.randint(1, 6)
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
    route = _random_legal_route(range(1, new_task_id), rng)
    return problem, route, new_task_id


def _random_removal_case(seed):
    rng = random.Random(seed)
    count = rng.randint(1, 6)
    node_count = 1 + 2 * count
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
        for task_id in range(1, count + 1)
    )
    problem = Problem(
        tasks,
        drone_count=1,
        max_tasks_per_drone=count,
        distance_matrix_km=tuple(tuple(row) for row in matrix),
    )
    route = _random_legal_route(range(1, count + 1), rng)
    task_id = rng.choice(list(range(1, count + 1)))
    return problem, route, task_id


def test_route_profile_next_full_matches_reference():
    """New O(1) can_insert must equal the reference slice for every (p, d)."""
    for seed in range(64):
        rng = random.Random(seed)
        capacity = rng.choice([2, 3])
        old_count = rng.randint(0, 8)
        route = (
            _random_legal_route(range(1, old_count + 1), rng, capacity)
            if old_count
            else ()
        )
        profile = RouteProfile.build(route, capacity)
        length = len(route)
        for pickup in range(length + 1):
            for delivery in range(pickup + 1, length + 2):
                assert profile.can_insert(
                    pickup, delivery
                ) == _can_insert_pair(
                    profile.loads_before, pickup, delivery, capacity
                )


def _reference_select(scored, limit):
    """Inline copy of the pre-optimization full-sort screening."""
    by_distance = sorted(
        scored, key=lambda item: (item[0], item[1], item[2])
    )
    by_early_delivery = sorted(
        scored, key=lambda item: (item[2], item[1], item[0])
    )
    selected = []
    selected_set = set()
    for collection, quota in (
        (by_distance, max(1, limit // 2)),
        (by_early_delivery, max(1, limit // 3)),
    ):
        added = 0
        for item in collection:
            position = (item[1], item[2])
            if position not in selected_set:
                selected.append(item)
                selected_set.add(position)
                added += 1
            if added >= quota or len(selected) >= limit:
                break
        if len(selected) >= limit:
            break
    for item in by_distance:
        if len(selected) >= limit:
            break
        position = (item[1], item[2])
        if position not in selected_set:
            selected.append(item)
            selected_set.add(position)
    return selected


def test_bounded_selection_matches_full_sort_reference():
    """nsmallest-based screening must select exactly the full-sort subset."""
    for seed in range(64):
        rng = random.Random(seed)
        count = rng.randint(20, 200)
        grid = [
            (pickup, delivery)
            for pickup in range(30)
            for delivery in range(pickup + 1, 30)
        ]
        rng.shuffle(grid)
        positions = grid[:count]
        tie_pool = [round(rng.uniform(-10.0, 10.0), 2) for _ in range(3)]
        scored = [
            (
                rng.choice(tie_pool)
                if rng.random() < 0.6
                else round(rng.uniform(-10.0, 10.0), 4),
                pickup,
                delivery,
            )
            for pickup, delivery in positions
        ]
        early_positions = tuple(
            sorted(positions, key=lambda item: (item[1], item[0]))
        )
        delta_by_position = {
            (pickup, delivery): delta
            for delta, pickup, delivery in scored
        }

        def delta_of(pickup: int, delivery: int) -> float:
            return delta_by_position[(pickup, delivery)]

        limit = rng.choice([4, 8, 24, 48])
        assert (
            _select_candidate_positions(
                scored, limit, early_positions, delta_of
            )
            == _reference_select(scored, limit)
        )


def test_distance_delta_arrays_match_node_function():
    """Array-decomposed deltas must equal _node_distance_delta bit-for-bit
    on every feasible position of random routes."""
    for seed in range(32):
        problem, route, new_task_id = _random_problem_and_route(seed)
        surface = route_insertion_surface(problem, route)
        distances = problem._distances
        visit_nodes = surface.visit_nodes
        start_node = problem._node_for_visit(new_task_id)
        end_node = problem._node_for_visit(-new_task_id)
        arrays = _build_distance_delta_arrays(
            distances, visit_nodes, start_node, end_node
        )
        for pickup, delivery in surface.positions:
            expected = _node_distance_delta(
                distances,
                visit_nodes,
                start_node,
                end_node,
                pickup,
                delivery,
            )
            assert _array_distance_delta(arrays, pickup, delivery) == expected


def test_scored_distance_deltas_match_closure_reference():
    """The inlined full-position scan must equal the former delta_of closure
    bit-for-bit on every feasible position of random routes."""
    for seed in range(32):
        problem, route, new_task_id = _random_problem_and_route(seed)
        surface = route_insertion_surface(problem, route)
        arrays = _build_distance_delta_arrays(
            problem._distances,
            surface.visit_nodes,
            problem._node_for_visit(new_task_id),
            problem._node_for_visit(-new_task_id),
            route_start_node=problem.home_node(0),
        )
        (
            pickup_in,
            pickup_pair,
            old_pickup_edge,
            delivery_in,
            delivery_out,
            old_delivery_edge,
            d_ss,
        ) = arrays

        def delta_of(pickup_position: int, delivery_position: int) -> float:
            if delivery_position == pickup_position + 1:
                return (
                    pickup_in[pickup_position]
                    + d_ss
                    + delivery_out[delivery_position]
                ) - old_pickup_edge[pickup_position]
            return (
                (pickup_pair[pickup_position]
                 + delivery_in[delivery_position])
                + delivery_out[delivery_position]
            ) - (
                old_pickup_edge[pickup_position]
                + old_delivery_edge[delivery_position]
            )

        expected = [
            (delta_of(pickup, delivery), pickup, delivery)
            for pickup, delivery in surface.positions
        ]
        assert _scored_distance_deltas(arrays, surface.positions) == expected


def test_early_delivery_positions_order():
    for seed in range(16):
        problem, route, _ = _random_problem_and_route(seed)
        surface = route_insertion_surface(problem, route)
        expected = tuple(
            sorted(surface.positions, key=lambda item: (item[1], item[0]))
        )
        assert surface.positions_by_early_delivery == expected


def test_direct_validator_fast_path_matches_full():
    """The Direct fast path must reproduce the full relay-aware validation
    exactly, including invalid solutions and unknown visits."""
    invalid_cases = (
        ((1, -1), (2,)),
        ((1, 1, -1, -1),),
        ((2, -2, 1, -1),),
        ((99,),),
        ((-1, 1),),
        ((1, 2, 3, -1),),
        ((1,), (-1,)),
        (),
    )
    for seed in range(32):
        rng = random.Random(seed)
        task_count = rng.randint(1, 6)
        tasks = tuple(
            Task(
                task_id,
                Point(task_id, 0),
                Point(task_id, 0.25),
                rng.uniform(1, 100),
            )
            for task_id in range(1, task_count + 1)
        )
        problem = Problem(
            tasks, drone_count=2, max_tasks_per_drone=task_count, capacity=2
        )
        legal = tuple(
            _random_legal_route(range(1, task_count + 1), rng)
            for _ in range(2)
        )
        for routes in (legal, *invalid_cases):
            fast = evaluate_solution(problem, routes)
            full = evaluate_solution(problem, routes, _force_full=True)
            assert fast.valid == full.valid
            assert fast.score == full.score
            assert dict(fast.delivery_times_min) == dict(
                full.delivery_times_min
            )
            assert fast.max_lateness_min == full.max_lateness_min
            assert fast.violations == full.violations
            assert fast.route_distances_km == full.route_distances_km
            assert (
                fast.route_completion_times_min
                == full.route_completion_times_min
            )


def test_raw_delta_matches_public_wrapper():
    """The allocation-free primitive must equal the public wrapper on every
    feasible position of random routes."""
    for seed in range(32):
        problem, route, new_task_id = _random_problem_and_route(seed)
        surface = route_insertion_surface(problem, route)
        distances = problem._distances
        speed = problem.speed_km_per_min
        start_node = problem._node_for_visit(new_task_id)
        end_node = problem._node_for_visit(-new_task_id)
        deadline = problem.task(new_task_id).deadline_min
        counters = SearchCounters()
        for pickup, delivery in surface.positions:
            delta_distance = _node_distance_delta(
                distances,
                surface.visit_nodes,
                start_node,
                end_node,
                pickup,
                delivery,
            )
            raw = _pair_insertion_delta_raw(
                surface,
                start_node,
                end_node,
                pickup,
                delivery,
                is_delivery=True,
                deadline=deadline,
                delta_distance=delta_distance,
                speed=speed,
                distances=distances,
                counters=counters,
            )
            public = pair_insertion_delta(
                problem,
                surface,
                new_task_id,
                -new_task_id,
                pickup,
                delivery,
                is_delivery=True,
                task_id=new_task_id,
                counters=counters,
                delta_distance=delta_distance,
            )
            assert raw[0] == public.delta.late_count
            assert raw[1] == public.delta.total_lateness_min
            assert raw[2] == public.delta.distance_km
            assert raw[3] == public.start_time_min
            assert raw[4] == public.end_time_min


def test_surface_delivery_event_count_matches_reference():
    for seed in range(16):
        problem, route, _ = _random_problem_and_route(seed)
        surface = route_insertion_surface(problem, route)
        assert surface.delivery_event_count == _delivery_event_count(
            problem, route
        )


def test_removal_delta_matches_full_evaluation():
    """Incremental removal gain must equal score(full) - score(reduced)
    computed by full route evaluation, bit-for-bit, on random routes."""
    for seed in range(32):
        problem, route, task_id = _random_removal_case(seed)
        evaluator = RouteEvaluator(problem)
        surface = route_insertion_surface(problem, route)
        pickup = route.index(task_id)
        delivery = route.index(-task_id)
        late_count, lateness, distance_delta = _pair_removal_delta_exact(
            surface,
            pickup,
            delivery,
            speed=problem.speed_km_per_min,
            distances=problem._distances,
            registry=problem.leg_registry,
        )
        reduced = tuple(
            visit for visit in route if abs(visit) != task_id
        )
        expected = (
            evaluator.evaluate(route).score
            - evaluator.evaluate(reduced).score
        )
        assert late_count == expected.late_count
        assert distance_delta == expected.distance_km
        assert lateness == expected.total_lateness_min


def test_multi_visit_removal_matches_full_evaluation():
    """Removing one or two complete direct pairs incrementally must equal
    the full-evaluation difference bit-for-bit."""
    for seed in range(24):
        problem, route, task_id = _random_removal_case(seed)
        rng = random.Random(seed + 1000)
        other = [
            candidate
            for candidate in range(1, len(problem.tasks) + 1)
            if candidate != task_id
        ]
        visits = {task_id, -task_id}
        if other and len(route) >= 4 and rng.random() < 0.7:
            second = rng.choice(other)
            visits |= {second, -second}
        positions = tuple(
            sorted(index for index, visit in enumerate(route) if visit in visits)
        )
        surface = route_insertion_surface(problem, route)
        evaluator = RouteEvaluator(problem)
        late_count, lateness, distance_delta = _visits_removal_delta_exact(
            surface,
            positions,
            speed=problem.speed_km_per_min,
            distances=problem._distances,
            registry=problem.leg_registry,
        )
        removed = set(positions)
        reduced = tuple(
            visit
            for index, visit in enumerate(route)
            if index not in removed
        )
        expected = (
            evaluator.evaluate(route).score
            - evaluator.evaluate(reduced).score
        )
        assert late_count == expected.late_count
        assert distance_delta == expected.distance_km
        assert lateness == expected.total_lateness_min


def test_relay_leg_removal_matches_full_evaluation():
    """Removing a relay leg pair incrementally must equal the
    full-evaluation difference bit-for-bit, for both leg kinds."""
    tasks = (
        Task(1, Point(1.0, 0.0), Point(8.0, 0.0), 30.0),
        Task(2, Point(0.0, 1.0), Point(6.0, 1.0), 40.0),
        Task(3, Point(0.0, -1.0), Point(7.0, -1.0), 40.0),
    )
    stations = (RelayStation(1, 4.0, 3.0),)
    registry = {
        101: TransportLeg(101, 1, "RELAY_IN", 1),
        102: TransportLeg(102, 1, "RELAY_OUT", 1),
    }
    problem = Problem(
        tasks,
        drone_count=1,
        max_tasks_per_drone=3,
        relay_stations=stations,
        leg_registry=registry,
        task_relay_candidates={1: (1,)},
    )
    for route, removed_abs in (
        ((101, -101, 2, -2), 101),
        ((102, -102, 3, -3), 102),
        # Remove only the direct pair: the relay task's delivery record
        # (RELAY_OUT end, keyed by downstream task id) must survive the
        # re-walk unchanged.
        ((102, -102, 3, -3), 3),
        ((101, -101, 2, -2), 2),
    ):
        pickup = route.index(removed_abs)
        delivery = route.index(-removed_abs)
        surface = route_insertion_surface(problem, route)
        evaluator = RouteEvaluator(problem)
        late_count, lateness, distance_delta = _visits_removal_delta_exact(
            surface,
            (pickup, delivery),
            speed=problem.speed_km_per_min,
            distances=problem._distances,
            registry=problem.leg_registry,
        )
        reduced = tuple(
            visit for visit in route if abs(visit) != removed_abs
        )
        expected = (
            evaluator.evaluate(route).score
            - evaluator.evaluate(reduced).score
        )
        assert late_count == expected.late_count
        assert distance_delta == expected.distance_km
        assert lateness == expected.total_lateness_min
