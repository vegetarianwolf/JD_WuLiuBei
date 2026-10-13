"""Deterministic regressions for canonical best-so-far across relay startup."""

from dataclasses import replace

import pytest

import uav_dispatch.alns as alns
from uav_dispatch import (
    ALNSConfig, GlobalRelayEvaluator, Point, Problem, RelayStation, Score, Task,
    TransportLeg, build_plan_index, evaluate_solution, relay_statistics,
)
from uav_dispatch.search import RouteEvaluator, SolverResult


def _one_task_relay(station_x):
    problem = Problem(
        (Task(1, Point(0, 0), Point(1, 0), 100),),
        drone_count=2, max_tasks_per_drone=1,
        relay_stations=(RelayStation(1, station_x, 0),),
        leg_registry={
            2: TransportLeg(2, 1, "RELAY_IN", 1),
            3: TransportLeg(3, 1, "RELAY_OUT", 1),
        },
        task_relay_candidates={1: (1,)},
    )
    return problem, ((1, -1), ()), ((2, -2), (3, -3))


def _score_tuple(score):
    return (score.late_count, score.total_lateness_min, score.distance_km)


def _relay_config():
    return alns._RelaySearchConfig(
        enabled=True, candidates_per_task=2, plan_beam=4,
        leg_candidate_limit=4, leg_beam=5, event_cap=60, debug=False,
    )


@pytest.mark.parametrize("station_x", [0.25, 1e-13, 0.0])
def test_relay_seed_rejects_worse_and_exactly_tied_shortlist_candidates(monkeypatch, station_x):
    problem, direct, relay = _one_task_relay(station_x)
    monkeypatch.setattr(alns, "_all_plans_for_task", lambda *args, **kwargs: [object()])
    # A candidate can beat the truncated reinsertion shortlist while still
    # being worse than the task's original location (or only tying it).
    monkeypatch.setattr(
        alns, "_apply_task_exact",
        lambda *args, **kwargs: (relay, Score(0, 0, 0.5), "RELAY"),
    )
    assert evaluate_solution(problem, relay).score >= evaluate_solution(problem, direct).score
    result = alns._relay_seed_pass(
        problem, RouteEvaluator(problem), GlobalRelayEvaluator(problem), direct,
        relay_cfg=_relay_config(),
        task_limit=None, deadline=None,
    )
    assert result == direct


def test_seed_strict_improvement_and_initial_score_keep_original_incumbent(monkeypatch):
    problem = Problem(
        (
            Task(1, Point(-10, 0), Point(-9, 0), 100),
            Task(2, Point(-9, 1), Point(10, 1), 100),
            Task(3, Point(10, 2), Point(9, 2), 100),
        ), drone_count=2, max_tasks_per_drone=2,
        relay_stations=(RelayStation(1, 0.5, 1),),
        leg_registry={
            4: TransportLeg(4, 2, "RELAY_IN", 1),
            5: TransportLeg(5, 2, "RELAY_OUT", 1),
        }, task_relay_candidates={2: (1,)},
    )
    direct = ((1, -1), (2, -2, 3, -3))
    relay = ((1, -1, 4, -4), (5, -5, 3, -3))
    initial = evaluate_solution(problem, direct, _force_full=True)
    improved = evaluate_solution(problem, relay, _force_full=True)
    assert initial.valid and improved.valid and improved.score < initial.score
    monkeypatch.setattr(alns, "_all_plans_for_task", lambda *args, **kwargs: [object()])
    monkeypatch.setattr(alns, "_apply_task_exact", lambda *args, **kwargs: (relay, improved.score, "RELAY"))
    result = alns.solve_alns(
        problem, initial_routes=direct,
        config=ALNSConfig(max_iterations=0, enable_relay_refine=False),
    )
    assert result.routes == relay
    assert result.metadata["initial_score"] == _score_tuple(initial.score)
    assert [point[2:] for point in result.metadata["best_trajectory"]] == [
        _score_tuple(initial.score), _score_tuple(improved.score),
    ]
    assert result.metadata["best_update_count"] == 1


def test_seed_full_confirmation_cannot_accept_after_deadline(monkeypatch):
    problem, direct, relay = _one_task_relay(0.25)
    monkeypatch.setattr(alns, "_all_plans_for_task", lambda *args, **kwargs: [object()])
    monkeypatch.setattr(alns, "_apply_task_exact", lambda *args, **kwargs: (relay, Score(0, 0, 0.5), "RELAY"))
    clock_values = iter([0.0, 2.0])
    monkeypatch.setattr(alns, "perf_counter", lambda: next(clock_values))
    with pytest.raises(alns._SearchDeadlineReached):
        alns._relay_seed_pass(
            problem, RouteEvaluator(problem), GlobalRelayEvaluator(problem), direct,
            relay_cfg=_relay_config(), task_limit=None, deadline=1.0,
        )


@pytest.mark.parametrize("stage", ["seed", "refine"])
def test_equal_startup_candidate_does_not_create_a_false_best_update(monkeypatch, stage):
    problem, direct, relay = _one_task_relay(0.0)
    monkeypatch.setattr(alns, f"_relay_{stage}_pass", lambda *args, **kwargs: relay)
    result = alns.solve_alns(
        problem, initial_routes=direct,
        config=ALNSConfig(
            max_iterations=0, enable_relay_seed=stage == "seed",
            enable_relay_refine=stage == "refine",
        ),
    )
    assert result.routes == relay
    assert result.evaluation.score == Score(0, 0, 1.0)
    assert result.metadata["best_update_count"] == 0
    assert result.metadata["best_trajectory"][-1][2:] == (0, 0.0, 1.0)


@pytest.mark.parametrize("stage", ["seed", "refine"])
@pytest.mark.parametrize("interrupt", [False, True])
def test_relay_startup_cannot_discard_supplied_incumbent(monkeypatch, stage, interrupt):
    problem, direct, relay = _one_task_relay(0.25)

    def pass_result(*args, **kwargs):
        if interrupt:
            raise alns._SearchDeadlineReached
        return relay

    monkeypatch.setattr(alns, f"_relay_{stage}_pass", pass_result)
    result = alns.solve_alns(
        problem, initial_routes=direct,
        config=ALNSConfig(
            max_iterations=0,
            enable_relay_seed=stage == "seed",
            enable_relay_refine=stage == "refine",
        ),
    )
    expected = evaluate_solution(problem, direct, _force_full=True)
    assert result.routes == direct
    assert result.evaluation == expected
    assert result.metadata["initial_score"] == _score_tuple(expected.score)
    assert result.metadata["best_trajectory"][-1][2:] == _score_tuple(expected.score)
    assert result.metadata["best_update_count"] == 0
    assert result.metadata["relay"]["relay_task_count"] == 0


def test_best_update_requires_canonical_improvement_even_when_search_score_improves(monkeypatch):
    problem = Problem(
        (Task(1, Point(0, 0), Point(1, 0), 100),),
        drone_count=2, max_tasks_per_drone=1,
        drone_homes=(Point(0, 0), Point(1e-13, 0)),
    )
    direct, worse = ((1, -1), ()), ((), (1, -1))
    monkeypatch.setattr(alns, "_repair", lambda *args, **kwargs: worse)
    monkeypatch.setattr(
        alns, "routes_score",
        lambda evaluator, routes: Score(0, 0, 1.0 - 1e-13 if routes == worse else 1.0),
    )
    result = alns.solve_alns(
        problem, initial_routes=direct, config=ALNSConfig(max_iterations=1),
    )
    assert 1.0 < evaluate_solution(problem, worse).score.distance_km < 1.0 + 1e-12
    assert result.routes == direct
    assert result.evaluation.score.distance_km == 1.0
    assert result.metadata["accepted_solutions"] == 1
    assert result.metadata["best_update_count"] == 0
    for stats in result.metadata["operator_statistics"].values():
        assert 0 <= stats["best_improvements"] <= stats["current_improvements"] <= stats["accepted"]


@pytest.mark.parametrize("station_x, retained", [(0.25, True), (1e-13, True), (0.0, False)])
def test_staged_full_comparison_retains_warmup_and_rebuilds_selected_route_metadata(monkeypatch, station_x, retained):
    problem, direct, relay = _one_task_relay(station_x)
    calls = []

    def phase_result(problem, *, config, initial_routes):
        calls.append((config.relay_enabled, initial_routes))
        routes = relay if config.relay_enabled else direct
        evaluation = evaluate_solution(problem, routes, _force_full=True)
        schedule = GlobalRelayEvaluator(problem).evaluate(routes)
        score = _score_tuple(evaluation.score)
        # Exported phase scores cannot replace independent canonical checking.
        reported = replace(evaluation, score=Score(0, 0, 0.5)) if config.relay_enabled else evaluation
        return SolverResult(
            routes=routes, evaluation=reported,
            runtime_seconds=2.0 if config.relay_enabled else 8.0,
            iterations=2 if config.relay_enabled else 8,
            metadata={
                "initial_score": (0, 0.0, 1.0),
                "best_trajectory": ((0, 0.1, *score),),
                "best_update_count": 7 if config.relay_enabled else 0,
                "relay": relay_statistics(problem, routes, schedule, build_plan_index(problem, routes)),
                "profiling": {"route_full_evaluation_count": 2},
                "operator_uses": {"destroy:random": 2},
                "accepted_solutions": 1,
            },
        )

    monkeypatch.setattr(alns, "solve_alns", phase_result)
    result = alns.solve_relay_staged(
        problem, initial_routes=direct,
        config=ALNSConfig(max_iterations=10, relay_direct_warmup_fraction=0.8),
    )
    selected = direct if retained else relay
    expected = evaluate_solution(problem, selected, _force_full=True)
    assert calls == [(False, direct), (True, direct)]
    assert result.routes == selected
    assert result.evaluation == expected
    assert result.metadata["relay_retained_warmup_incumbent"] is retained
    assert result.metadata["relay_search_final_score"] == _score_tuple(evaluate_solution(problem, relay, _force_full=True).score)
    assert result.metadata["relay_warmup_score"] == (0, 0.0, 1.0)
    assert result.metadata["best_trajectory"][-1][2:] == _score_tuple(expected.score)
    assert result.metadata["relay"]["relay_task_count"] == (0 if retained else 1)
    expected_statistics = relay_statistics(
        problem, selected, GlobalRelayEvaluator(problem).evaluate(selected),
        build_plan_index(problem, selected),
    )
    assert dict(result.metadata["relay"]) == expected_statistics
    assert result.metadata["relay_phase_best_update_count"] == (0 if retained else 7)
    assert result.metadata["profiling"]["route_full_evaluation_count"] == 4
    assert result.metadata["operator_uses"]["destroy:random"] == 4
    assert result.metadata["accepted_solutions"] == 2
    assert result.runtime_seconds == 10.0
    assert result.iterations == 10
