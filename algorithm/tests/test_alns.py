import math

from uav_dispatch import (
    ALNSConfig,
    Point,
    Problem,
    Task,
    construct_regret_initial,
    evaluate_solution,
    solve_alns,
    solve_alns_core,
)
from uav_dispatch.alns import _RepairCache


def _fleet_problem() -> Problem:
    tasks = tuple(
        Task(
            task_id,
            Point(0.6 * task_id, 0.3 * (task_id % 3)),
            Point(0.6 * task_id + 0.8, 1.0 + 0.2 * (task_id % 2)),
            deadline_min=7.0 + task_id * 0.9,
        )
        for task_id in range(1, 9)
    )
    return Problem(tasks, drone_count=2, max_tasks_per_drone=4)


def test_regret_initial_covers_a_completely_full_fleet():
    problem = _fleet_problem()

    initial = construct_regret_initial(problem, candidate_limit=None)

    assert initial.evaluation.valid
    assert [sum(visit > 0 for visit in route) for route in initial.routes] == [4, 4]


def test_direct_defaults_use_the_validated_small_destroy_band():
    config = ALNSConfig()

    assert config.min_destroy_fraction == 0.03
    assert config.max_destroy_fraction == 0.08
    assert config.relay_direct_warmup_fraction == 0.80
    assert config.relay_candidates_per_task == 2
    assert config.relay_sample_every == 3
    assert config.relay_plan_beam == 4
    assert config.relay_leg_beam == 5
    assert config.relay_global_limit == 3
    assert config.relay_probe_fraction == 0.25
    assert config.relay_probe_max_tasks == 2
    assert config.enable_relay_seed is True
    assert config.enable_relay_refine is True


def test_repair_cache_does_not_expose_abandoned_package_api():
    """The rolled-back scored-package experiment must not leave a crash path."""

    cache = _RepairCache(route_count=1)

    assert not hasattr(cache, "direct_options_with_package")


def test_fixed_iteration_alns_is_reproducible_and_preserves_best_so_far():
    problem = _fleet_problem()
    initial = construct_regret_initial(problem, candidate_limit=None)
    config = ALNSConfig(
        max_iterations=40,
        time_limit_seconds=None,
        seed=20260805,
        candidate_limit=None,
        route_pool_interval=10,
        ejection_interval=10,
        weight_update_interval=8,
    )

    first = solve_alns(problem, config=config, initial_routes=initial.routes)
    second = solve_alns(problem, config=config, initial_routes=initial.routes)

    assert first.routes == second.routes
    assert first.evaluation.score == second.evaluation.score
    assert first.metadata["operator_uses"] == second.metadata["operator_uses"]
    trajectory = first.metadata["best_trajectory"]
    assert trajectory[0][0] == 0
    assert first.metadata["best_update_count"] == len(trajectory) - 1
    assert all(
        later[2:] < earlier[2:]
        for earlier, later in zip(trajectory, trajectory[1:])
    )
    assert first.evaluation.score <= initial.evaluation.score
    assert evaluate_solution(problem, first.routes).valid
    assert first.iterations == 40
    assert all(
        math.isfinite(weight) and weight > 0
        for weight in first.metadata["operator_weights"].values()
    )


def test_warm_start_with_unused_drones_is_padded_with_empty_routes():
    tasks = (
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(-1, 0), Point(-2, 0), 100),
    )
    problem = Problem(tasks, drone_count=2, max_tasks_per_drone=2)
    warm_start = ((1, -1, 2, -2),)

    result = solve_alns(
        problem,
        config=ALNSConfig(max_iterations=0),
        initial_routes=warm_start,
    )

    assert len(result.routes) == 2
    assert result.routes[1] == ()
    assert result.evaluation.valid


def test_alns_core_public_solver_disables_hybrid_reinforcement():
    problem = _fleet_problem()
    config = ALNSConfig(
        max_iterations=20,
        seed=20260805,
        candidate_limit=None,
        enable_route_pool=True,
        enable_ejection=True,
    )

    result = solve_alns_core(problem, config=config)

    assert result.evaluation.valid
    assert result.metadata["method"] == "C2-Lex-ALNS-Core"
    assert result.metadata["enable_route_pool"] is False
    assert result.metadata["enable_ejection"] is False
    assert result.metadata["route_pool_columns"] == 0


def test_expired_search_budget_does_not_start_an_iteration():
    problem = _fleet_problem()
    initial = construct_regret_initial(problem, candidate_limit=None)

    result = solve_alns(
        problem,
        config=ALNSConfig(
            max_iterations=100,
            time_limit_seconds=1e-9,
            candidate_limit=None,
        ),
        initial_routes=initial.routes,
    )

    assert result.evaluation.valid
    assert result.iterations == 0
