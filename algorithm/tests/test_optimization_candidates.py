"""Small, deterministic checks for the separately gated repair candidates."""

from dataclasses import replace
from random import Random

import pytest

import uav_dispatch.alns as alns
from uav_dispatch import (
    ALNSConfig, GlobalRelayEvaluator, Point, Problem, RelayStation, Score,
    Task, TransportLeg, construct_regret_initial, evaluate_solution, solve_alns,
)
from uav_dispatch.search import RouteEvaluator, RouteInsertionOption, routes_score


def _fleet_problem(seed=0, *, drones=2, different_homes=False):
    rng = Random(seed)
    tasks = tuple(
        Task(
            task_id,
            Point(rng.uniform(0, 10), rng.uniform(0, 10)),
            Point(rng.uniform(0, 10), rng.uniform(0, 10)),
            0.0 if task_id == 1 else rng.uniform(8, 70),
        )
        for task_id in range(1, 21)
    )
    return Problem(
        tasks, drone_count=drones, max_tasks_per_drone=20 // drones,
        drone_homes=(Point(0, 0), Point(10, 10)) if different_homes else None,
    )


def _assert_same_search(left, right):
    assert left.routes == right.routes
    assert left.evaluation == right.evaluation
    assert left.iterations == right.iterations
    for key in (
        "initial_score", "best_update_count", "accepted_solutions",
        "operator_uses", "operator_statistics", "operator_weights",
    ):
        assert left.metadata[key] == right.metadata[key], key
    # Elapsed times and work counters intentionally differ after lazy ranking.
    assert [(point[0], *point[2:]) for point in left.metadata["best_trajectory"]] == [
        (point[0], *point[2:]) for point in right.metadata["best_trajectory"]
    ]


@pytest.mark.parametrize("seed", [0, 1])
@pytest.mark.parametrize("candidate_limit", [None, 12])
@pytest.mark.parametrize("deadline_risk", [False, True])
def test_lazy_repair_preserves_the_entire_fixed_iteration_search(
    seed, candidate_limit, deadline_risk,
):
    problem = _fleet_problem(seed)
    initial = construct_regret_initial(problem, candidate_limit=candidate_limit)
    config = ALNSConfig(
        max_iterations=35, seed=seed, candidate_limit=candidate_limit,
        min_destroy_fraction=0.2, max_destroy_fraction=0.35,
        enable_deadline_risk=deadline_risk,
        repair_rank_candidate_limit=8, repair_exact_candidate_limit=12,
    )
    baseline = solve_alns(problem, config=config, initial_routes=initial.routes)
    lazy = solve_alns(
        problem, config=replace(config, enable_lazy_repair=True),
        initial_routes=initial.routes,
    )

    _assert_same_search(baseline, lazy)
    assert lazy.evaluation.valid
    assert lazy.metadata["enable_lazy_repair"] is True
    assert baseline.metadata["enable_lazy_repair"] is False
    assert lazy.metadata["profiling"]["repair_rank_evaluation_count"] < (
        baseline.metadata["profiling"]["repair_rank_evaluation_count"]
    )


@pytest.mark.parametrize("strategy", ["deadline", "slack"])
@pytest.mark.parametrize("deadline_risk", [False, True])
@pytest.mark.parametrize("removed_count", [0, 1, 2, 8])
def test_lazy_repair_handles_shortlists_zero_deadlines_and_different_homes(
    strategy, deadline_risk, removed_count,
):
    problem = _fleet_problem(different_homes=True)
    initial = construct_regret_initial(problem, candidate_limit=12)
    removed = set(range(1, removed_count + 1))
    partial = tuple(
        tuple(visit for visit in route if abs(visit) not in removed)
        for route in initial.routes
    )
    repaired = []
    for lazy in (False, True):
        repaired.append(alns._repair(
            problem, RouteEvaluator(problem), partial, removed, strategy, 12,
            use_deadline_risk=deadline_risk,
            repair_rank_candidate_limit=8, repair_exact_candidate_limit=12,
            enable_lazy_repair=lazy,
        ))
    assert repaired[0] == repaired[1]
    assert evaluate_solution(problem, repaired[1], _force_full=True).valid


def test_candidate_flags_leave_initial_construction_unchanged_and_are_reported():
    problem = _fleet_problem()
    baseline = solve_alns(problem, config=ALNSConfig(max_iterations=0))
    candidates = solve_alns(problem, config=ALNSConfig(
        max_iterations=0, enable_lazy_repair=True,
        enable_route_distinct_regret=True,
    ))
    _assert_same_search(baseline, candidates)
    assert candidates.metadata["enable_lazy_repair"] is True
    assert candidates.metadata["enable_route_distinct_regret"] is True
    assert ALNSConfig().enable_route_distinct_regret is False
    assert ALNSConfig().enable_lazy_repair is False


def test_route_distinct_regret_keeps_single_drone_search_unchanged():
    problem = _fleet_problem(drones=1)
    initial = construct_regret_initial(problem, candidate_limit=12)
    config = ALNSConfig(max_iterations=30, candidate_limit=12, seed=7)
    baseline = solve_alns(problem, config=config, initial_routes=initial.routes)
    distinct = solve_alns(
        problem, config=replace(config, enable_route_distinct_regret=True),
        initial_routes=initial.routes,
    )
    _assert_same_search(baseline, distinct)


@pytest.mark.parametrize("strategy", ["regret2", "regret3"])
@pytest.mark.parametrize("drone_count", [2, 3])
def test_route_distinct_regret_uses_other_drones_and_preserves_scarcity_priority(
    monkeypatch, strategy, drone_count,
):
    # Three cheap positions on one route hide task 1's deadline cost on all
    # other routes. Task 2 has a larger within-route distance regret only.
    fewer_routes_than_k = strategy == "regret3" and drone_count == 2
    problem = Problem(
        tuple(Task(i, Point(i, 0), Point(i + 0.5, 0),
                   30 - i if fewer_routes_than_k else 20 + i)
              for i in range(1, drone_count + 3)),
        drone_count=drone_count, max_tasks_per_drone=3,
    )
    partial = tuple((i, -i) for i in range(3, drone_count + 3))
    first_inserted = []
    observed_option_counts = []

    def options(self, problem, evaluator, routes, task_id, route_index,
                candidate_limit, option_count, deadline):
        inserted = {abs(v) for route in routes for v in route} & {1, 2}
        if inserted and not first_inserted:
            first_inserted.append(next(iter(inserted)))
        observed_option_counts.append(option_count)
        route = routes[route_index]
        result = []
        for index, position in enumerate((0, 2, len(route))):
            if route_index == 0:
                delta = Score(0, 0, (1, 2, 3)[index] if task_id == 1 else (1, 10, 20)[index])
            else:
                delta = Score(int(task_id == 1), 1 if task_id == 1 else 0,
                              50 + 10 * route_index + index)
            inserted_route = route[:position] + (task_id, -task_id) + route[position:]
            result.append(RouteInsertionOption(
                delta, route_index, position, position + 1, inserted_route,
            ))
        return tuple(result[:option_count])

    monkeypatch.setattr(alns._RepairCache, "direct_options", options)
    choices = []
    for distinct in (False, True):
        first_inserted.clear()
        repaired = alns._repair(
            problem, RouteEvaluator(problem), partial, (1, 2), strategy, None,
            enable_route_distinct_regret=distinct,
        )
        choices.append(first_inserted[0])
        assert evaluate_solution(problem, repaired, _force_full=True).valid
    # With only two routes regret3 keeps the existing scarcity rule: both
    # tasks have fewer than k alternatives, so the earlier deadline wins.
    # Do not silently turn regret3 into regret2 by reducing k to fleet size.
    assert choices == ([2, 2] if fewer_routes_than_k else [2, 1])
    # A stage must not ask a cache keyed without option_count for fewer
    # options and then reuse that truncated entry for exact insertion.
    assert set(observed_option_counts) == {3 if strategy == "regret3" else 2}


def _waiting_problem(pickup_x):
    problem = Problem(
        (
            Task(1, Point(pickup_x, 0), Point(1, 0), 0.5),
            Task(2, Point(1, 0), Point(2, 0), 5),
            Task(3, Point(0, 0), Point(1, 0), 100),
        ),
        drone_count=3, max_tasks_per_drone=2, speed_km_per_min=1,
        relay_stations=(RelayStation(1, 0, 0),),
        leg_registry={
            4: TransportLeg(4, 1, "RELAY_IN", 1),
            5: TransportLeg(5, 1, "RELAY_OUT", 1),
        },
        task_relay_candidates={1: (1,)},
    )
    return problem, ((4, -4), (5, -5, 2, -2), (3, -3))


def _relay_config():
    return alns._RelaySearchConfig(
        enabled=True, candidates_per_task=2, plan_beam=4,
        leg_candidate_limit=4, leg_beam=5, event_cap=60, debug=False,
    )


def test_relay_refine_does_not_prune_global_improvement_hidden_by_waiting():
    problem, routes = _waiting_problem(10)
    evaluator = RouteEvaluator(problem)
    global_evaluator = GlobalRelayEvaluator(problem)
    schedule = global_evaluator.evaluate(routes)
    config = _relay_config()
    repaired = alns._relay_refine_pass(
        problem, evaluator, global_evaluator, routes,
        relay_cfg=config, task_limit=None, deadline=None,
    )
    evaluation = evaluate_solution(problem, repaired, _force_full=True)

    assert schedule.total_waiting_min == 20
    assert schedule.score == Score(2, 37.5, 23)
    # This non-improving local delta was pruned by the old implementation.
    assert routes_score(evaluator, repaired) - routes_score(evaluator, routes) == Score(0, 0, 0)
    assert evaluation.valid
    assert evaluation.score == Score(1, 20.5, 23)
    assert config.refine_candidates_pruned == 0
    assert config.refine_candidates_evaluated == 1


def test_relay_refine_preserves_pruning_when_no_handoff_waits():
    problem, routes = _waiting_problem(0)
    global_evaluator = GlobalRelayEvaluator(problem)
    assert global_evaluator.evaluate(routes).total_waiting_min == 0
    config = _relay_config()
    repaired = alns._relay_refine_pass(
        problem, RouteEvaluator(problem), global_evaluator, routes,
        relay_cfg=config, task_limit=None, deadline=None,
    )
    assert repaired == routes
    assert config.refine_candidates_pruned == 2
    assert evaluate_solution(problem, repaired, _force_full=True).valid
