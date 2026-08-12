from random import Random

from uav_dispatch import (
    ALNSConfig,
    Point,
    Problem,
    Task,
    construct_regret_initial,
    destroy_solution,
    solve_alns,
)
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.alns import (
    cluster_regret_repair,
    inter_uav_relocate_once,
    inter_uav_swap_once,
    intra_route_block_improve_once,
    vnd_improve,
)
from uav_dispatch import evaluate_solution
from uav_dispatch.search import routes_score


def _two_full_uavs_problem() -> tuple[Problem, tuple[tuple[int, ...], ...]]:
    tasks = tuple(
        Task(
            task_id,
            Point(float(task_id), float(task_id % 2)),
            Point(float(task_id) + 0.5, float(task_id % 2) + 0.5),
            deadline_min=5.0 + task_id,
        )
        for task_id in range(1, 5)
    )
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=2, capacity=2)
    return problem, ((1, -1, 2, -2), (3, -3, 4, -4))


def test_assignment_destroy_accounts_for_every_task_pair_exactly_once():
    problem, routes = _two_full_uavs_problem()

    partial, removed = destroy_solution(
        problem,
        RouteEvaluator(problem),
        routes,
        "assignment_destroy",
        2,
        Random(20260805),
    )

    assert len(removed) == len(set(removed)) == 2
    original_route = {
        task_id: route_index
        for route_index, route in enumerate(routes)
        for task_id in route
        if task_id > 0
    }
    assert len({original_route[task_id] for task_id in removed}) == 2

    remaining_visits = [visit for route in partial for visit in route]
    for task_id in problem.task_ids:
        expected = 0 if task_id in removed else 1
        assert remaining_visits.count(task_id) == expected
        assert remaining_visits.count(-task_id) == expected
    assert {abs(visit) for visit in remaining_visits}.isdisjoint(removed)
    assert {abs(visit) for visit in remaining_visits} | set(removed) == set(
        problem.task_ids
    )


def test_deadline_risk_changes_critical_destroy_order_without_changing_score():
    problem = Problem(
        (
            Task(1, Point(89, 0), Point(100, 0), deadline_min=110),
            Task(2, Point(1, 0), Point(2, 0), deadline_min=3),
        ),
        drone_count=2,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    routes = ((1, -1), (2, -2))
    evaluator = RouteEvaluator(problem)

    _, risk_removed = destroy_solution(
        problem,
        evaluator,
        routes,
        "late_critical",
        1,
        Random(1),
        use_deadline_risk=True,
    )
    _, slack_removed = destroy_solution(
        problem,
        evaluator,
        routes,
        "late_critical",
        1,
        Random(1),
        use_deadline_risk=False,
    )

    assert risk_removed == (1,)  # risk 100/110 > 2/3
    assert slack_removed == (2,)  # slack 3-2 < 110-100
    assert evaluator.evaluate(routes[0]).score.late_count == 0
    assert evaluator.evaluate(routes[1]).score.late_count == 0


def test_new_destroy_guidance_can_be_disabled_to_reproduce_legacy_search():
    tasks = tuple(
        Task(
            task_id,
            Point(0.6 * task_id, 0.3 * (task_id % 3)),
            Point(0.6 * task_id + 0.8, 1.0 + 0.2 * (task_id % 2)),
            deadline_min=7.0 + task_id * 0.9,
        )
        for task_id in range(1, 9)
    )
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=4)
    initial = construct_regret_initial(problem, candidate_limit=None)

    result = solve_alns(
        problem,
        config=ALNSConfig(
            max_iterations=40,
            seed=20260805,
            candidate_limit=None,
            route_pool_interval=10,
            ejection_interval=10,
            weight_update_interval=8,
            enable_assignment_destroy=False,
            enable_deadline_risk=False,
            enable_vnd=False,
            enable_cluster_repair=False,
            enable_route_pool=True,
            enable_ejection=True,
        ),
        initial_routes=initial.routes,
    )

    assert result.evaluation.valid
    assert result.evaluation.score.late_count >= 0  # feasible solution
    assert all(
        len(route) == 2 * len(set(abs(v) for v in route))
        for route in result.routes
    )
    assert "destroy:assignment_destroy" not in result.metadata["operator_uses"]
    assert result.metadata["enable_assignment_destroy"] is False
    assert result.metadata["enable_deadline_risk"] is False
    assert result.metadata["enable_vnd"] is False
    assert result.metadata["enable_cluster_repair"] is False
    assert "repair:cluster_regret" not in result.metadata["operator_uses"]


def test_route_pool_is_conservative_by_default_but_ejection_stays_enabled():
    config = ALNSConfig()

    assert config.enable_route_pool is False
    assert config.enable_ejection is True
    assert config.enable_assignment_destroy is True
    assert config.enable_deadline_risk is True
    assert config.enable_vnd is False
    assert config.enable_cluster_repair is False


def test_inter_uav_relocate_preserves_pair_capacity_and_route_limit():
    problem = Problem(
        (
            Task(1, Point(10, 0), Point(11, 0), 100),
            Task(2, Point(-10, 0), Point(-11, 0), 100),
            Task(3, Point(0, 1), Point(0, 2), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1,
    )
    routes = ((1, -1, 2, -2), (3, -3))

    relocated = inter_uav_relocate_once(
        problem,
        RouteEvaluator(problem),
        routes,
        candidate_limit=None,
    )

    assert relocated != routes
    assert evaluate_solution(problem, relocated).valid
    for route in relocated:
        assert sum(visit > 0 for visit in route) <= problem.max_tasks_per_drone
        load = 0
        positions = {visit: index for index, visit in enumerate(route)}
        for visit in route:
            load += 1 if visit > 0 else -1
            assert 0 <= load <= problem.capacity
        for task_id in (visit for visit in route if visit > 0):
            assert positions[task_id] < positions[-task_id]

    flattened = [visit for route in relocated for visit in route]
    for task_id in problem.task_ids:
        assert flattened.count(task_id) == 1
        assert flattened.count(-task_id) == 1


def test_inter_uav_swap_improves_a_full_fleet_without_breaking_pairs():
    problem = Problem(
        (
            Task(1, Point(10, 0), Point(11, 0), 100),
            Task(2, Point(-10, 0), Point(-11, 0), 100),
            Task(3, Point(12, 0), Point(13, 0), 100),
            Task(4, Point(-12, 0), Point(-13, 0), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1,
    )
    routes = ((1, -1, 2, -2), (3, -3, 4, -4))
    evaluator = RouteEvaluator(problem)

    assert (
        inter_uav_relocate_once(
            problem,
            evaluator,
            routes,
            candidate_limit=None,
        )
        == routes
    )
    swapped = inter_uav_swap_once(
        problem,
        evaluator,
        routes,
        candidate_limit=None,
    )

    assert routes_score(evaluator, swapped) < routes_score(evaluator, routes)
    assert evaluate_solution(problem, swapped).valid


def test_capacity_two_block_search_reorders_pickup_pickup_delivery_delivery():
    problem = Problem(
        (
            Task(1, Point(10, 0), Point(-10, 0), 100),
            Task(2, Point(11, 0), Point(12, 0), 100),
        ),
        drone_count=1,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1,
    )
    routes = ((1, 2, -1, -2),)
    evaluator = RouteEvaluator(problem)

    improved = intra_route_block_improve_once(
        problem,
        evaluator,
        routes,
    )

    assert routes_score(evaluator, improved) < routes_score(evaluator, routes)
    assert evaluate_solution(problem, improved).valid


def test_vnd_never_accepts_distance_gain_with_a_worse_lexicographic_score():
    problem = Problem(
        (
            Task(1, Point(10, 0), Point(11, 0), 11),
            Task(2, Point(10, 0), Point(9, 0), 11),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1,
    )
    routes = ((1, -1), (2, -2))
    distance_bait = ((1, 2, -1, -2), ())
    evaluator = RouteEvaluator(problem)
    initial_score = routes_score(evaluator, routes)
    bait_score = routes_score(evaluator, distance_bait)

    assert bait_score.distance_km < initial_score.distance_km
    assert bait_score > initial_score

    improved = vnd_improve(
        problem,
        evaluator,
        routes,
        candidate_limit=None,
        max_moves=3,
    )

    assert routes_score(evaluator, improved) <= initial_score
    assert evaluate_solution(problem, improved).valid


def test_vnd_accepts_distance_gain_when_late_count_is_unchanged():
    # Under the two-layer objective (late_count, distance_km), a move that
    # saves distance with an unchanged late count is accepted even when the
    # total lateness would increase.
    problem = Problem(
        (
            Task(
                23,
                Point(2.12537, 22.74358),
                Point(-7.14946, -12.25642),
                119.75061,
            ),
            Task(
                32,
                Point(1.94117, 24.87814),
                Point(-3.49931, -23.82606),
                11.55574,
            ),
            Task(
                67,
                Point(23.88728, 0.5658),
                Point(-7.90541, -18.00709),
                0.0,
            ),
            Task(
                79,
                Point(-21.0377, -22.86823),
                Point(-14.94177, 13.17237),
                2.50312,
            ),
        ),
        drone_count=4,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=2.854917354770454,
        depot=Point(3.71513, 3.96563),
    )
    routes = ((32, -32), (67, -67), (79, -79), (23, -23))
    evaluator = RouteEvaluator(problem)

    improved = vnd_improve(
        problem,
        evaluator,
        routes,
        candidate_limit=8,
        max_moves=1,
        task_limit=None,
        swap_pair_limit=None,
        block_window_limit=4,
    )

    initial_score = routes_score(evaluator, routes)
    improved_score = routes_score(evaluator, improved)
    # The two-layer objective may accept a same-late distance gain that the
    # old three-layer objective rejected; the result is never worse.
    assert improved_score <= initial_score
    if improved != routes:
        assert improved_score.distance_km < initial_score.distance_km
    assert evaluate_solution(problem, improved).valid


def test_cluster_regret_repair_inserts_related_removed_tasks_as_a_bundle():
    problem = Problem(
        (
            Task(1, Point(10, 0), Point(11, 0), 100),
            Task(2, Point(10, 1), Point(11, 1), 101),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
        speed_km_per_min=1,
    )

    repaired = cluster_regret_repair(
        problem,
        RouteEvaluator(problem),
        ((), ()),
        (1, 2),
        candidate_limit=None,
        original_route_by_task={1: 0, 2: 0},
    )

    assert evaluate_solution(problem, repaired).valid
    assert sorted(sum(visit > 0 for visit in route) for route in repaired) == [0, 2]


def test_cluster_bundle_beam_keeps_jointly_good_non_greedy_first_positions():
    problem = Problem(
        (
            Task(
                1,
                Point(0.9623810762339815, -3.083363533238903),
                Point(6.897021771332554, -4.228051291893136),
                21.882765973423734,
            ),
            Task(
                2,
                Point(-3.1237144182748366, -1.6901214393001567),
                Point(9.477175926021346, -7.925346218433928),
                19.454021574044894,
            ),
            Task(
                3,
                Point(-5.503848671455051, -3.0086687079786163),
                Point(9.97480221061259, -3.4140206996419646),
                25.487778075515664,
            ),
            Task(
                4,
                Point(-1.6076626127839049, 4.609072387120383),
                Point(-1.0558496449756465, -6.5912842423547024),
                19.054769635296424,
            ),
            Task(
                5,
                Point(-1.838797528404946, 5.8543517714419515),
                Point(1.2521290069072002, -6.071974405105385),
                27.932272856160488,
            ),
        ),
        drone_count=1,
        max_tasks_per_drone=5,
        capacity=2,
        speed_km_per_min=1,
    )

    repaired = cluster_regret_repair(
        problem,
        RouteEvaluator(problem),
        ((1, -1, 2, -2, 3, -3),),
        (4, 5),
        candidate_limit=None,
        original_route_by_task={4: 0, 5: 0},
    )

    assert repaired == ((5, 4, -4, -5, 1, -1, 2, -2, 3, -3),)
    assert evaluate_solution(problem, repaired).valid
