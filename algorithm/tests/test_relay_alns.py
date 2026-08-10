import math

from uav_dispatch import Point, Problem, Task, evaluate_solution
from uav_dispatch.relay import Event, EventType, Hub, RelayPlan, RelayProblem
from uav_dispatch.relay_alns import (
    RelayALNSConfig,
    competition_key,
    solve_relay_alns,
)


def test_competition_key_contains_only_the_two_official_objectives() -> None:
    base = Problem(
        (Task(1, Point(1, 0), Point(2, 0), 0.5),),
        drone_count=1,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    evaluation = solve_relay_alns(
        RelayProblem(base),
        config=RelayALNSConfig(max_iterations=0),
        initial_routes=((1, -1),),
    ).evaluation

    assert competition_key(evaluation) == (
        evaluation.score.late_count,
        evaluation.score.distance_km,
    )


def test_fixed_iteration_relay_alns_is_adaptive_and_reproducible() -> None:
    base = Problem(
        (
            Task(1, Point(10, 0), Point(0, 10), 100),
            Task(2, Point(0, 9), Point(0, 11), 100),
            Task(3, Point(9, 0), Point(11, 0), 100),
        ),
        drone_count=3,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    problem = RelayProblem(
        base,
        hubs=(Hub("west", Point(0, 9)), Hub("east", Point(9, 0))),
        task_count_semantics="primary-owner",
    )
    config = RelayALNSConfig(
        max_iterations=24,
        seed=23,
        weight_update_interval=2,
        reaction_factor=0.5,
    )
    initial_routes = ((1, -1), (2, -2), (3, -3))

    first = solve_relay_alns(problem, config=config, initial_routes=initial_routes)
    second = solve_relay_alns(problem, config=config, initial_routes=initial_routes)

    assert first.plan == second.plan
    assert first.evaluation.score == second.evaluation.score
    assert first.iterations == second.iterations == 24
    assert first.metadata["operator_uses"] == second.metadata["operator_uses"]
    assert first.metadata["operator_weights"] == second.metadata["operator_weights"]
    assert set(first.metadata["operator_uses"]) == {
        "split",
        "merge",
        "change_receiver",
        "change_hub",
    }
    assert sum(first.metadata["operator_uses"].values()) == first.iterations
    assert sum(uses > 0 for uses in first.metadata["operator_uses"].values()) >= 2
    assert any(
        not math.isclose(weight, 1.0)
        for weight in first.metadata["operator_weights"].values()
    )
    assert all(
        math.isfinite(weight) and weight > 0
        for weight in first.metadata["operator_weights"].values()
    )
    assert first.metadata["operator_selection"] == "adaptive_weighted_roulette"
    assert first.metadata["acceptance_rule"] == (
        "simulated_annealing_normalized_lex_gap"
    )
    assert first.metadata["reward_scores"] == {
        "new_best": 8.0,
        "improved_current": 4.0,
        "equal_current": 2.0,
        "accepted_non_improving": 1.0,
        "rejected": 0.0,
    }
    assert first.metadata["returned_solution"] == "best_so_far"
    assert first.metadata["search_score_order"] == (
        "late_count",
        "distance_km",
        "total_lateness_min_tie_break_only",
    )


def test_non_improving_acceptance_can_escape_current_without_losing_best() -> None:
    base = Problem(
        (
            Task(1, Point(1, 0), Point(2, 0), 10_000),
            Task(2, Point(0, 1), Point(0, 2), 10_000),
        ),
        drone_count=2,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    problem = RelayProblem(
        base,
        hubs=(Hub("far", Point(100, 100)),),
        task_count_semantics="primary-owner",
    )
    initial_routes = ((1, -1), (2, -2))
    exploratory = solve_relay_alns(
        problem,
        config=RelayALNSConfig(
            max_iterations=1,
            seed=31,
            initial_temperature=1e12,
            cooling_rate=1.0,
            minimum_temperature=0.0,
        ),
        initial_routes=initial_routes,
    )
    greedy = solve_relay_alns(
        problem,
        config=RelayALNSConfig(
            max_iterations=1,
            seed=31,
            enable_non_improving_acceptance=False,
        ),
        initial_routes=initial_routes,
    )

    assert exploratory.plan.to_signed_routes() == initial_routes
    assert exploratory.metadata["accepted_non_improving"] == 1
    assert (
        exploratory.metadata["current_competition_key"]
        > exploratory.metadata["best_competition_key"]
    )
    assert exploratory.metadata["current_is_best"] is False
    assert greedy.metadata["accepted_non_improving"] == 0
    assert greedy.metadata["current_is_best"] is True


def test_zero_iteration_relay_solver_preserves_a_direct_warm_start() -> None:
    base = Problem(
        (Task(1, Point(1, 0), Point(2, 0), 10),),
        drone_count=2,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    problem = RelayProblem(
        base,
        hubs=(Hub("gate", Point(1.5, 0)),),
        task_count_semantics="primary-owner",
    )
    signed_routes = ((1, -1), ())

    result = solve_relay_alns(
        problem,
        config=RelayALNSConfig(max_iterations=0, seed=7),
        initial_routes=signed_routes,
    )

    assert result.plan.to_signed_routes() == signed_routes
    assert result.evaluation.valid
    assert result.evaluation.score == evaluate_solution(base, signed_routes).score
    assert result.iterations == 0
    assert result.metadata["task_count_semantics"] == "primary-owner"


def test_relay_solver_builds_a_complete_direct_start_when_none_is_supplied() -> None:
    base = Problem(
        (
            Task(1, Point(1, 0), Point(2, 0), 10),
            Task(2, Point(0, 1), Point(0, 2), 10),
        ),
        drone_count=2,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )

    result = solve_relay_alns(
        RelayProblem(base, task_count_semantics="strict-touch"),
        config=RelayALNSConfig(max_iterations=0, seed=3),
    )

    assert result.evaluation.valid
    assert result.evaluation.direct_task_count == 2
    assert result.evaluation.relay_count == 0


def test_relay_solver_can_split_a_task_when_buffering_improves_the_score() -> None:
    base = Problem(
        (
            Task(1, Point(10, 0), Point(0, 10), 100),
            Task(2, Point(0, 9), Point(0, 11), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    problem = RelayProblem(
        base,
        hubs=(Hub("gate", Point(0, 9)),),
        task_count_semantics="primary-owner",
    )
    direct_routes = ((1, -1), (2, -2))
    direct = evaluate_solution(base, direct_routes)

    result = solve_relay_alns(
        problem,
        config=RelayALNSConfig(max_iterations=20, seed=5),
        initial_routes=direct_routes,
    )

    assert result.evaluation.valid
    assert result.evaluation.relay_count == 1
    assert result.evaluation.score.late_count == direct.score.late_count
    assert result.evaluation.score.distance_km < direct.score.distance_km


def test_strict_touch_saturated_fleet_skips_mathematically_impossible_relay() -> None:
    base = Problem(
        (
            Task(1, Point(1, 0), Point(2, 0), 100),
            Task(2, Point(0, 1), Point(0, 2), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=1,
    )

    result = solve_relay_alns(
        RelayProblem(
            base,
            hubs=(Hub("gate", Point(1, 1)),),
            task_count_semantics="strict-touch",
        ),
        config=RelayALNSConfig(max_iterations=20, seed=2),
        initial_routes=((1, -1), (2, -2)),
    )

    assert result.evaluation.valid
    assert result.evaluation.relay_count == 0
    assert result.iterations == 0
    assert result.metadata["relay_search_disabled_reason"] == "strict_touch_saturated"


def test_solver_records_the_direct_base_search_inside_the_total_run() -> None:
    base = Problem(
        (
            Task(1, Point(1, 0), Point(2, 0), 100),
            Task(2, Point(0, 1), Point(0, 2), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=1,
    )

    result = solve_relay_alns(
        RelayProblem(base, task_count_semantics="strict-touch"),
        config=RelayALNSConfig(max_iterations=1, seed=11),
    )

    assert result.evaluation.valid
    assert result.metadata["base_search_used"] is True
    assert result.metadata["base_search_runtime_seconds"] >= 0
    assert result.metadata["base_search_objective_order"] == (
        "late_count",
        "distance_km",
    )


def test_relay_solver_can_merge_an_unhelpful_handoff_back_to_direct_service() -> None:
    base = Problem(
        (
            Task(1, Point(1, 0), Point(2, 0), 100),
            Task(2, Point(0, 1), Point(0, 2), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=1,
    )
    problem = RelayProblem(
        base,
        hubs=(Hub("far", Point(100, 100)),),
        task_count_semantics="primary-owner",
    )
    initial_plan = RelayPlan(
        (
            (
                Event(1, EventType.PICKUP),
                Event(1, EventType.HANDOFF_DROP, "far"),
            ),
            (
                Event(2, EventType.PICKUP),
                Event(2, EventType.DELIVERY),
                Event(1, EventType.HANDOFF_PICK, "far"),
                Event(1, EventType.DELIVERY),
            ),
        )
    )

    result = solve_relay_alns(
        problem,
        config=RelayALNSConfig(max_iterations=10, seed=13),
        initial_plan=initial_plan,
    )

    assert result.evaluation.valid
    assert result.evaluation.relay_count == 0
    assert result.metadata["accepted_merges"] == 1


def test_relay_solver_can_move_an_existing_handoff_to_a_better_static_hub() -> None:
    base = Problem(
        (
            Task(1, Point(10, 0), Point(0, 10), 100),
            Task(2, Point(0, 9), Point(0, 11), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    problem = RelayProblem(
        base,
        hubs=(
            Hub("far", Point(100, 100)),
            Hub("gate", Point(0, 9)),
        ),
        task_count_semantics="primary-owner",
    )
    initial_plan = RelayPlan(
        (
            (
                Event(1, EventType.PICKUP),
                Event(1, EventType.HANDOFF_DROP, "far"),
            ),
            (
                Event(2, EventType.PICKUP),
                Event(1, EventType.HANDOFF_PICK, "far"),
                Event(1, EventType.DELIVERY),
                Event(2, EventType.DELIVERY),
            ),
        )
    )

    result = solve_relay_alns(
        problem,
        config=RelayALNSConfig(max_iterations=10, seed=17),
        initial_plan=initial_plan,
    )

    assert result.evaluation.valid
    assert result.evaluation.relay_count == 1
    assert {
        event.hub_id
        for route in result.plan.routes
        for event in route
        if event.event_type in {EventType.HANDOFF_DROP, EventType.HANDOFF_PICK}
    } == {"gate"}
    assert result.metadata["accepted_hub_changes"] == 1


def test_relay_solver_can_change_the_receiving_drone() -> None:
    base = Problem(
        (
            Task(1, Point(10, 0), Point(0, 10), 300),
            Task(2, Point(100, 0), Point(100, 1), 300),
            Task(3, Point(0, 9), Point(0, 11), 300),
        ),
        drone_count=3,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    problem = RelayProblem(
        base,
        hubs=(Hub("gate", Point(0, 9)),),
        task_count_semantics="primary-owner",
    )
    initial_plan = RelayPlan(
        (
            (
                Event(1, EventType.PICKUP),
                Event(1, EventType.HANDOFF_DROP, "gate"),
            ),
            (
                Event(2, EventType.PICKUP),
                Event(2, EventType.DELIVERY),
                Event(1, EventType.HANDOFF_PICK, "gate"),
                Event(1, EventType.DELIVERY),
            ),
            (
                Event(3, EventType.PICKUP),
                Event(3, EventType.DELIVERY),
            ),
        )
    )

    result = solve_relay_alns(
        problem,
        config=RelayALNSConfig(max_iterations=10, seed=19),
        initial_plan=initial_plan,
    )

    receiver = next(
        route_index
        for route_index, route in enumerate(result.plan.routes)
        if any(
            event.task_id == 1 and event.event_type is EventType.HANDOFF_PICK
            for event in route
        )
    )
    assert result.evaluation.valid
    assert result.evaluation.relay_count == 1
    assert receiver == 2
    assert result.metadata["accepted_receiver_changes"] == 1
