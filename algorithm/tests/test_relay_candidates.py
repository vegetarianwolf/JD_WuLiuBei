"""Bounded relay experiments: UAV diversity, global risk, and checkpoints."""

from dataclasses import replace

import pytest

import uav_dispatch.alns as alns
from uav_dispatch import (
    ALNSConfig, GlobalRelayEvaluator, Point, Problem, RelayStation, Task,
    TransportLeg, evaluate_solution, solve_alns,
)
from uav_dispatch.search import RouteEvaluator


def _config(**changes):
    return alns._RelaySearchConfig(
        enabled=True, candidates_per_task=2, plan_beam=4,
        leg_candidate_limit=4, leg_beam=5, event_cap=60, debug=False,
        **changes,
    )


def _bridge():
    return Problem(
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


def _options(problem, routes, diversity, *, inbound=True, width=2, deadline=None):
    leg = 4 if inbound else 5
    return alns._best_leg_options(
        problem, RouteEvaluator(problem), routes, leg, -leg,
        leg_candidate_limit=None, counts_toward_k=not inbound, event_cap=60,
        top_k=width, deadline=deadline, route_diversity=diversity,
    )


def test_route_diversity_retains_a_second_uav_and_a_faster_handoff_position():
    problem = _bridge()
    reduced = ((1, -1), (3, -3))
    baseline = _options(problem, reduced, False)
    diverse = _options(problem, reduced, True)
    assert len(baseline) == len(diverse) == 2
    assert {option.route_index for option in baseline} == {0}
    assert {option.route_index for option in diverse} == {0, 1}
    assert diverse[0] == baseline[0]
    assert diverse[1].end_time_min < baseline[1].end_time_min
    assert diverse[1].delta.distance_km > baseline[1].delta.distance_km


def test_route_diversity_refills_positions_when_only_one_uav_has_a_k_slot():
    problem = _bridge()
    problem = replace(
        problem,
        tasks=problem.tasks + (Task(6, Point(10, 2), Point(11, 2), 100),),
    )
    reduced = ((1, -1, 3, -3), (6, -6))
    baseline = _options(problem, reduced, False, inbound=False, width=3)
    diverse = _options(problem, reduced, True, inbound=False, width=3)
    assert baseline == diverse
    assert len(diverse) == 3
    assert {option.route_index for option in diverse} == {1}
    # Inbound transport still can use a UAV whose delivery responsibility
    # is full; diversity must not count inbound legs as original deliveries.
    inbound = _options(problem, reduced, True, width=2)
    assert {option.route_index for option in inbound} == {0, 1}


@pytest.mark.parametrize("diversity", [False, True])
def test_diverse_leg_beam_honors_the_deadline_before_generating_options(monkeypatch, diversity):
    monkeypatch.setattr(alns, "perf_counter", lambda: 2.0)
    with pytest.raises(alns._SearchDeadlineReached):
        _options(_bridge(), ((1, -1), (3, -3)), diversity, deadline=1.0)


def test_risk_seed_uses_global_waits_and_keeps_task_limit(monkeypatch):
    problem = Problem(
        (
            Task(1, Point(0, 0), Point(1, 0), 0.5),
            Task(2, Point(0, 0), Point(1, 0), 1),
            Task(3, Point(10, 0), Point(0, 0), 100),
        ), drone_count=2, max_tasks_per_drone=3,
        relay_stations=(RelayStation(1, 0, 0),),
        leg_registry={
            4: TransportLeg(4, 1, "RELAY_IN", 1),
            5: TransportLeg(5, 1, "RELAY_OUT", 1),
            6: TransportLeg(6, 2, "RELAY_IN", 1),
            7: TransportLeg(7, 2, "RELAY_OUT", 1),
            8: TransportLeg(8, 3, "RELAY_IN", 1),
            9: TransportLeg(9, 3, "RELAY_OUT", 1),
        }, task_relay_candidates={1: (1,), 2: (1,), 3: (1,)},
    )
    routes = ((1, -1, 8, -8), (9, -9, 2, -2))
    assert evaluate_solution(problem, routes, _force_full=True).valid
    local = RouteEvaluator(problem)
    global_eval = GlobalRelayEvaluator(problem)
    local_risk = {
        task_id: local.evaluate(routes[route_index]).delivery_times_min[task_id]
        / problem.task(task_id).deadline_min
        for task_id, route_index in ((1, 0), (2, 1))
    }
    global_times = global_eval.evaluate(routes).delivery_times_min
    assert local_risk[1] > local_risk[2]
    assert global_times[2] / problem.task(2).deadline_min > local_risk[1]
    seen = []

    def no_plans(problem, evaluator, reduced, task_id, **kwargs):
        seen.append(task_id)
        return ()

    monkeypatch.setattr(alns, "_all_plans_for_task", no_plans)
    for risk, expected in ((False, 1), (True, 2)):
        seen.clear()
        config = _config(risk_seed=risk)
        result = alns._relay_seed_pass(
            problem, local, global_eval, routes, relay_cfg=config,
            task_limit=1, deadline=None,
        )
        assert seen == [expected]
        assert result == routes
        assert config.seed_tasks_considered == 1


def test_checkpoint_keeps_only_a_previously_confirmed_improvement(monkeypatch):
    problem = _bridge()
    # Add another eligible DIRECT task to force a later seed iteration.
    problem = replace(problem, task_relay_candidates={2: (1,), 3: (1,)})
    direct = ((1, -1), (2, -2, 3, -3))
    improved = ((1, -1, 4, -4), (5, -5, 3, -3))
    initial_score = evaluate_solution(problem, direct, _force_full=True).score
    improved_score = evaluate_solution(problem, improved, _force_full=True).score
    assert improved_score < initial_score
    monkeypatch.setattr(alns, "_all_plans_for_task", lambda *args, **kwargs: (object(),))
    monkeypatch.setattr(
        alns, "_apply_task_exact",
        lambda *args, **kwargs: (improved, improved_score, "RELAY"),
    )
    # First move is completely confirmed within budget. The next loop's
    # check expires before another candidate is built.
    clock = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(alns, "perf_counter", lambda: next(clock))
    config = _config(seed_checkpoint=True)
    retained = alns._relay_seed_pass(
        problem, RouteEvaluator(problem), GlobalRelayEvaluator(problem), direct,
        relay_cfg=config, task_limit=None, deadline=1.0,
    )
    assert retained == improved
    assert config.seed_improvements == 1
    assert config.seed_deadline_reached and config.seed_checkpoint_retained
    clock = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(alns, "perf_counter", lambda: next(clock))
    with pytest.raises(alns._SearchDeadlineReached):
        alns._relay_seed_pass(
            problem, RouteEvaluator(problem), GlobalRelayEvaluator(problem), direct,
            relay_cfg=_config(), task_limit=None, deadline=1.0,
        )


def test_checkpoint_rejects_candidate_whose_full_confirmation_finishes_late(monkeypatch):
    problem = _bridge()
    direct = ((1, -1), (2, -2, 3, -3))
    improved = ((1, -1, 4, -4), (5, -5, 3, -3))
    score = evaluate_solution(problem, improved, _force_full=True).score
    monkeypatch.setattr(alns, "_all_plans_for_task", lambda *args, **kwargs: (object(),))
    monkeypatch.setattr(alns, "_apply_task_exact", lambda *args, **kwargs: (improved, score, "RELAY"))
    clock = iter((0.0, 2.0))
    monkeypatch.setattr(alns, "perf_counter", lambda: next(clock))
    config = _config(seed_checkpoint=True)
    assert alns._relay_seed_pass(
        problem, RouteEvaluator(problem), GlobalRelayEvaluator(problem), direct,
        relay_cfg=config, task_limit=None, deadline=1.0,
    ) == direct
    assert config.seed_improvements == 0
    assert config.seed_deadline_reached and not config.seed_checkpoint_retained


def test_relay_experiments_and_promoted_checkpoint_are_inactive_in_direct_search():
    defaults = ALNSConfig()
    assert not defaults.enable_relay_route_diversity
    assert not defaults.enable_relay_risk_seed
    assert defaults.enable_relay_seed_checkpoint
    result = solve_alns(
        _bridge(), initial_routes=((1, -1), (2, -2, 3, -3)),
        config=ALNSConfig(
            max_iterations=0, relay_enabled=False,
            enable_relay_route_diversity=True, enable_relay_risk_seed=True,
            enable_relay_seed_checkpoint=True,
        ),
    )
    assert not result.metadata["relay_route_diversity_active"]
    assert not result.metadata["relay_risk_seed_active"]
    assert not result.metadata["relay_seed_checkpoint_active"]
