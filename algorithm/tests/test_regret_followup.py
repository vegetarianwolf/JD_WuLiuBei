"""Mechanism regressions for scarce-route fallback and regret portfolios."""

from dataclasses import replace
from random import Random

import pytest

import uav_dispatch.alns as alns
from uav_dispatch import (
    ALNSConfig, Point, Problem, RelayStation, Task, TransportLeg,
    construct_regret_initial, evaluate_solution, solve_alns,
)
from uav_dispatch.search import RouteEvaluator


def _problem(seed=1, *, drones=2):
    rng = Random(seed)
    tasks = tuple(
        Task(i, Point(rng.uniform(0, 10), rng.uniform(0, 10)),
             Point(rng.uniform(0, 10), rng.uniform(0, 10)),
             rng.uniform(3, 50))
        for i in range(1, 9)
    )
    return Problem(tasks, drone_count=drones, max_tasks_per_drone=8 // drones)


def _decision_state(result):
    return (
        result.routes, result.evaluation, result.iterations,
        result.metadata["initial_score"], result.metadata["accepted_solutions"],
        dict(result.metadata["operator_uses"]),
        dict(result.metadata["operator_statistics"]),
        dict(result.metadata["operator_weights"]),
        tuple((point[0], *point[2:]) for point in result.metadata["best_trajectory"]),
    )


def _repair(problem, partial, removed, strategy, *, distinct, fallback):
    evaluator = RouteEvaluator(problem)
    routes = alns._repair(
        problem, evaluator, partial, removed, strategy, None,
        enable_route_distinct_regret=distinct,
        enable_regret_position_fallback=fallback,
    )
    assert evaluate_solution(problem, routes, _force_full=True).valid
    return routes, evaluator.counters


@pytest.mark.parametrize("strategy", ["regret2", "regret3"])
def test_scarce_routes_fall_back_to_the_unchanged_position_ranking(strategy):
    problem = _problem()
    # Eight tasks fill 2 x K=4. Every pending task can use exactly the same
    # single route, so route scarcity alone cannot prioritize those tasks.
    partial = ((4, -4), (5, -5, 6, -6, 7, -7, 8, -8))
    positional, _ = _repair(problem, partial, (1, 2, 3), strategy,
                            distinct=False, fallback=False)
    _, old_counters = _repair(problem, partial, (1, 2, 3), strategy,
                              distinct=True, fallback=False)
    fallback, counters = _repair(problem, partial, (1, 2, 3), strategy,
                                 distinct=True, fallback=True)

    assert fallback == positional
    assert old_counters.regret_all_infinite_round_count == 3
    assert counters.regret_distinct_round_count == 3
    assert counters.regret_distinct_ranking_count == 6
    assert counters.regret_distinct_shortage_count == 6
    assert counters.regret_position_fallback_count == 6
    assert counters.regret_position_shortage_count == 0
    assert counters.regret_all_infinite_round_count == 0


def test_fallback_keeps_real_position_shortage_infinite():
    problem = Problem(
        (Task(1, Point(1, 0), Point(2, 0), 10),
         Task(2, Point(3, 0), Point(4, 0), 10)),
        drone_count=2, max_tasks_per_drone=1,
    )
    routes, counters = _repair(problem, ((), (2, -2)), (1,), "regret3",
                               distinct=True, fallback=True)
    assert routes == ((1, -1), (2, -2))
    assert counters.regret_position_fallback_count == 1
    assert counters.regret_position_shortage_count == 1
    assert counters.regret_all_infinite_round_count == 1


@pytest.mark.parametrize("strategy", ["regret2", "regret3"])
def test_enough_available_routes_keep_the_distinct_ranking(strategy):
    problem = Problem(
        tuple(Task(i, Point(i, 0), Point(i + 1, 0), 10 + i)
              for i in range(1, 5)),
        drone_count=3, max_tasks_per_drone=2,
    )
    partial = ((1, -1), (2, -2), (3, -3))
    original, _ = _repair(problem, partial, (4,), strategy,
                          distinct=True, fallback=False)
    fallback, counters = _repair(problem, partial, (4,), strategy,
                                 distinct=True, fallback=True)
    assert fallback == original
    assert counters.regret_distinct_ranking_count == 1
    assert counters.regret_distinct_shortage_count == 0
    assert counters.regret_position_fallback_count == 0
    assert counters.regret_all_infinite_round_count == 0


def test_fallback_does_not_activate_distinct_regret_by_itself():
    problem = _problem()
    initial = construct_regret_initial(problem, candidate_limit=12)
    config = ALNSConfig(max_iterations=35, seed=3, candidate_limit=12)
    baseline = solve_alns(problem, config=config, initial_routes=initial.routes)
    fallback = solve_alns(
        problem, config=replace(config, enable_regret_position_fallback=True),
        initial_routes=initial.routes,
    )
    assert _decision_state(fallback) == _decision_state(baseline)
    assert fallback.metadata["profiling"]["regret_distinct_round_count"] == 0


@pytest.mark.parametrize("fallback", [False, True])
def test_portfolio_retains_position_operators_and_adapts_distinct_operators(
    monkeypatch, fallback,
):
    problem = _problem()
    initial = construct_regret_initial(problem, candidate_limit=12)
    selected = iter(("regret2", "route_regret2", "regret3", "route_regret3"))
    monkeypatch.setattr(alns, "_roulette", lambda weights, rng:
                        "random" if "random" in weights else next(selected))
    original_repair = alns._repair
    observed = []

    def recording_repair(*args, **kwargs):
        observed.append((args[4], kwargs["enable_route_distinct_regret"],
                         kwargs["enable_regret_position_fallback"]))
        return original_repair(*args, **kwargs)

    monkeypatch.setattr(alns, "_repair", recording_repair)
    result = solve_alns(
        problem, initial_routes=initial.routes,
        config=ALNSConfig(
            max_iterations=4, candidate_limit=12, weight_update_interval=1,
            enable_route_distinct_regret=True, enable_regret_portfolio=True,
            enable_regret_position_fallback=fallback,
        ),
    )
    assert observed == [
        ("regret2", False, fallback), ("regret2", True, fallback),
        ("regret3", False, fallback), ("regret3", True, fallback),
    ]
    assert result.evaluation.valid
    assert result.metadata["regret_portfolio_active"] is True
    assert len(result.metadata["operator_statistics"]) == 16
    for name in ("regret2", "route_regret2", "regret3", "route_regret3"):
        key = "repair:" + name
        stats = result.metadata["operator_statistics"][key]
        assert stats["uses"] == result.metadata["operator_uses"][key] == 1
        assert 0 <= stats["best_improvements"] <= stats["current_improvements"] <= stats["accepted"]
        assert result.metadata["operator_weights"][key] == pytest.approx(
            max(0.05, 0.8 + 0.2 * stats["total_reward"])
        )
    assert alns.REPAIR_OPERATORS == ("greedy", "regret2", "regret3", "deadline", "slack")


@pytest.mark.parametrize("portfolio,fallback", [(False, True), (True, False), (True, True)])
def test_followup_flags_never_change_initial_construction(portfolio, fallback):
    problem = _problem()
    baseline = solve_alns(problem, config=ALNSConfig(max_iterations=0))
    candidate = solve_alns(problem, config=ALNSConfig(
        max_iterations=0, enable_route_distinct_regret=True,
        enable_regret_portfolio=portfolio,
        enable_regret_position_fallback=fallback,
    ))
    assert candidate.routes == baseline.routes
    assert candidate.evaluation == baseline.evaluation
    assert candidate.metadata["initial_score"] == baseline.metadata["initial_score"]
    assert candidate.metadata["enable_regret_portfolio"] is portfolio
    assert candidate.metadata["enable_regret_position_fallback"] is fallback
    assert candidate.metadata["profiling"]["regret_distinct_round_count"] == 0


@pytest.mark.parametrize("portfolio,fallback", [(False, True), (True, False), (True, True)])
def test_single_drone_keeps_operator_pool_and_complete_search_decisions(portfolio, fallback):
    problem = _problem(drones=1)
    initial = construct_regret_initial(problem, candidate_limit=12)
    config = ALNSConfig(max_iterations=35, seed=4, candidate_limit=12)
    baseline = solve_alns(problem, config=config, initial_routes=initial.routes)
    candidate = solve_alns(problem, initial_routes=initial.routes, config=replace(
        config, enable_route_distinct_regret=True,
        enable_regret_portfolio=portfolio,
        enable_regret_position_fallback=fallback,
    ))
    assert _decision_state(candidate) == _decision_state(baseline)
    assert candidate.metadata["regret_portfolio_active"] is False
    assert len(candidate.metadata["operator_uses"]) == 14


def test_relay_search_does_not_gain_direct_portfolio_operators():
    problem = Problem(
        (Task(1, Point(1, 0), Point(2, 0), 10),
         Task(2, Point(3, 0), Point(4, 0), 10)),
        drone_count=2, max_tasks_per_drone=1,
        relay_stations=(RelayStation(1, 0, 0),),
        leg_registry={3: TransportLeg(3, 1, "RELAY_IN", 1),
                      4: TransportLeg(4, 1, "RELAY_OUT", 1)},
        task_relay_candidates={1: (1,)},
    )
    initial = ((1, -1), (2, -2))
    config = ALNSConfig(max_iterations=4, enable_relay_seed=False, enable_relay_refine=False)
    baseline = solve_alns(problem, config=config, initial_routes=initial)
    candidate = solve_alns(problem, initial_routes=initial, config=replace(
        config, enable_regret_portfolio=True, enable_regret_position_fallback=True,
    ))
    assert _decision_state(candidate) == _decision_state(baseline)
    assert candidate.metadata["regret_portfolio_active"] is False
    assert len(candidate.metadata["operator_uses"]) == 14


def test_followup_switches_default_off_and_counters_are_reported():
    config = ALNSConfig(max_iterations=0)
    assert config.enable_regret_portfolio is False
    assert config.enable_regret_position_fallback is False
    result = solve_alns(_problem(), config=config)
    assert result.metadata["enable_regret_portfolio"] is False
    assert result.metadata["enable_regret_position_fallback"] is False
    assert all(result.metadata["profiling"][name] == 0 for name in (
        "regret_distinct_round_count", "regret_distinct_ranking_count",
        "regret_distinct_shortage_count", "regret_position_fallback_count",
        "regret_position_shortage_count", "regret_all_infinite_round_count",
    ))
