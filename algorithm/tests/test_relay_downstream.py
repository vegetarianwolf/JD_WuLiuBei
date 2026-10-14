"""A punctual handoff can still be the cause of another task's lateness."""

from dataclasses import replace

import pytest

import uav_dispatch.alns as alns
from uav_dispatch import (
    ALNSConfig, GlobalRelayEvaluator, Point, Problem, RelayStation, Task,
    TransportLeg, build_plan_index, evaluate_solution,
)
from uav_dispatch.search import RouteEvaluator


def _full_fleet_with_punctual_blocking_relay():
    problem = Problem(
        (
            Task(1, Point(0, 0), Point(6, 0), 100),
            Task(2, Point(6, 0), Point(7, 0), 10),
            Task(3, Point(10, 0), Point(11, 0), 100),
            Task(4, Point(11, 0), Point(12, 0), 100),
            Task(5, Point(0, 0), Point(1, 0), 100),
            Task(6, Point(1, 0), Point(2, 0), 100),
        ),
        drone_count=3, max_tasks_per_drone=2, speed_km_per_min=1,
        drone_homes=(Point(10, 0), Point(5, 0), Point(0, 0)),
        relay_stations=(RelayStation(1, 5, 0),),
        leg_registry={
            7: TransportLeg(7, 1, "RELAY_IN", 1),
            8: TransportLeg(8, 1, "RELAY_OUT", 1),
        }, task_relay_candidates={1: (1,)},
    )
    routes = ((7, -7, 3, -3, 4, -4), (8, -8, 2, -2), (5, -5, 6, -6))
    return problem, routes


def _config(enabled=True):
    return alns._RelaySearchConfig(
        enabled=True, candidates_per_task=2, plan_beam=4,
        leg_candidate_limit=4, leg_beam=5, event_cap=60, debug=False,
        downstream_refine=enabled,
    )


def test_punctual_relay_inbound_move_rescues_late_receiver_suffix_with_full_k():
    problem, routes = _full_fleet_with_punctual_blocking_relay()
    before = evaluate_solution(problem, routes, _force_full=True)
    initial_schedule = GlobalRelayEvaluator(problem).evaluate(routes)
    cfg = _config()
    refined = alns._relay_refine_pass(
        problem, RouteEvaluator(problem), GlobalRelayEvaluator(problem), routes,
        relay_cfg=cfg, task_limit=1, deadline=None,
    )
    after = evaluate_solution(problem, refined, _force_full=True)
    final_schedule = GlobalRelayEvaluator(problem).evaluate(refined)
    initial_index, final_index = build_plan_index(problem, routes), build_plan_index(problem, refined)

    assert before.valid and after.valid
    assert before.delivery_times_min[1] == 16 < problem.task(1).deadline_min
    assert after.delivery_times_min[1] == 6 < problem.task(1).deadline_min
    assert before.delivery_times_min[2] == 17 > problem.task(2).deadline_min
    assert after.delivery_times_min[2] == 7 <= problem.task(2).deadline_min
    assert before.score.late_count == 1 and after.score.late_count == 0
    assert after.score < before.score
    assert final_schedule.total_waiting_min < initial_schedule.total_waiting_min
    assert initial_index.delivery_route_by_task == final_index.delivery_route_by_task
    assert initial_index.inbound_route_by_task[1] == 0
    assert final_index.inbound_route_by_task[1] == 2
    assert cfg.refine_candidates_evaluated > 0


def test_default_off_retains_historical_late_only_behavior(monkeypatch):
    problem, routes = _full_fleet_with_punctual_blocking_relay()
    cfg = _config(False)
    monkeypatch.setattr(alns, "route_leg_insertion_options", lambda *a, **k: pytest.fail("on-time task probed with flag off"))
    refined = alns._relay_refine_pass(
        problem, RouteEvaluator(problem), GlobalRelayEvaluator(problem), routes,
        relay_cfg=cfg, task_limit=8, deadline=None,
    )
    assert refined == routes
    assert cfg.refine_candidates_evaluated == 0
    assert ALNSConfig().enable_relay_downstream_refine is False


def test_zero_task_budget_does_not_probe_downstream_move():
    problem, routes = _full_fleet_with_punctual_blocking_relay()
    cfg = _config()
    refined = alns._relay_refine_pass(
        problem, RouteEvaluator(problem), GlobalRelayEvaluator(problem), routes,
        relay_cfg=cfg, task_limit=0, deadline=None,
    )
    assert refined == routes
    assert cfg.refine_candidates_evaluated == 0


def test_solver_flag_reaches_startup_refine_and_reports_checked_gain():
    problem, routes = _full_fleet_with_punctual_blocking_relay()
    cfg = ALNSConfig(max_iterations=0, enable_relay_seed=False,
                     enable_relay_refine=True, enable_relay_downstream_refine=False)
    baseline = alns.solve_alns(problem, initial_routes=routes, config=cfg)
    result = alns.solve_alns(problem, initial_routes=routes,
                             config=replace(cfg, enable_relay_downstream_refine=True))
    assert baseline.routes == routes
    assert result.evaluation.valid
    assert result.evaluation.score.late_count == 0
    assert result.metadata["enable_relay_downstream_refine"] is True
    assert result.metadata["initial_score"] == (1, 7.0, 26.0)
    assert result.metadata["best_update_count"] == 1
    assert result.metadata["best_trajectory"][-1][2] == 0


def test_candidate_finishing_global_schedule_after_deadline_is_not_returned(monkeypatch):
    problem, routes = _full_fleet_with_punctual_blocking_relay()
    clock = [0.0]
    global_evaluator = GlobalRelayEvaluator(problem)
    original_delta = global_evaluator.evaluate_delta
    def slow_delta(*args, **kwargs):
        result = original_delta(*args, **kwargs)
        clock[0] = 2.0
        return result
    monkeypatch.setattr(alns, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(global_evaluator, "evaluate_delta", slow_delta)
    with pytest.raises(alns._SearchDeadlineReached):
        alns._relay_refine_pass(
            problem, RouteEvaluator(problem), global_evaluator, routes,
            relay_cfg=_config(), task_limit=1, deadline=1.0,
        )


@pytest.mark.parametrize("expires_in", ["insertion", "global_schedule"])
def test_startup_refine_timeout_retains_incumbent_and_trajectory(monkeypatch, expires_in):
    problem, routes = _full_fleet_with_punctual_blocking_relay()
    clock = [0.0]
    original_delta = GlobalRelayEvaluator.evaluate_delta
    original_insertion = alns.route_leg_insertion_options
    def timed_delta(self, *args, **kwargs):
        if expires_in == "insertion":
            pytest.fail("expired insertion must stop before global candidate evaluation")
        result = original_delta(self, *args, **kwargs)
        clock[0] = 2.0
        return result
    def timed_insertion(*args, **kwargs):
        result = original_insertion(*args, **kwargs)
        if expires_in == "insertion":
            clock[0] = 2.0
        return result
    monkeypatch.setattr(alns, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(GlobalRelayEvaluator, "evaluate_delta", timed_delta)
    monkeypatch.setattr(alns, "route_leg_insertion_options", timed_insertion)
    result = alns.solve_alns(
        problem, initial_routes=routes,
        config=ALNSConfig(max_iterations=0, time_limit_seconds=1,
                          enable_relay_seed=False, enable_relay_downstream_refine=True),
    )
    assert result.routes == routes
    assert result.evaluation == evaluate_solution(problem, routes, _force_full=True)
    assert result.metadata["best_update_count"] == 0
    assert result.metadata["best_trajectory"] == ((0, 0.0, 1, 7.0, 26.0),)
