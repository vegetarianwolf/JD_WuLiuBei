from dataclasses import replace
from math import sqrt

import pytest

from uav_dispatch import alns
from uav_dispatch.alns import ALNSConfig, construct_regret_initial, solve_alns
from uav_dispatch.model import Point, Problem, Task


def problem(drones=2):
    return Problem(tuple(Task(i, Point(i, 0), Point(i + 1, 1), 6 + i)
                         for i in range(1, 7)),
                   drone_count=drones, max_tasks_per_drone=6)


def decisions(result):
    return (result.routes, result.evaluation.score, result.iterations,
            result.metadata["operator_statistics"], result.metadata["operator_weights"],
            result.metadata["accepted_solutions"],
            [(p[0], *p[2:]) for p in result.metadata["best_trajectory"]])


def test_geometric_schedule_endpoints_and_clamping():
    cfg = ALNSConfig(initial_temperature=.04, minimum_temperature=.001)
    assert alns._budget_temperature(cfg, -.2) == .04
    assert alns._budget_temperature(cfg, 0) == .04
    assert alns._budget_temperature(cfg, .5) == pytest.approx(sqrt(.04 * .001))
    assert alns._budget_temperature(cfg, 1) == pytest.approx(.001)
    assert alns._budget_temperature(cfg, 3) == pytest.approx(.001)


def test_iteration_budget_schedule_is_used_at_acceptance(monkeypatch):
    p = problem()
    initial = construct_regret_initial(p)
    temperatures = []
    original = alns._accept_worse

    def capture(candidate, current, problem, temperature, rng):
        temperatures.append(temperature)
        return original(candidate, current, problem, temperature, rng)

    monkeypatch.setattr(alns, "_accept_worse", capture)
    cfg = ALNSConfig(max_iterations=5, enable_budget_cooling=True,
                     relay_enabled=False)
    result = solve_alns(p, config=cfg, initial_routes=initial.routes)
    assert temperatures == pytest.approx([
        alns._budget_temperature(cfg, i / 4) for i in range(5)])
    assert result.evaluation.valid
    assert result.evaluation.score <= initial.evaluation.score
    assert result.metadata["budget_cooling_active"]


def test_wall_budget_uses_search_time_after_startup(monkeypatch):
    p = problem()
    initial = construct_regret_initial(p)
    # Set the search clock only after startup. A repair consumes one second;
    # other operations have no fake elapsed time. The fourth repair reaches
    # the deadline and must not accept a late result.
    clock = [0.0]
    real_repair = alns._repair
    real_evaluate = alns.evaluate_solution
    startup_calls = [0]

    def startup_delay(*args, **kwargs):
        result = real_evaluate(*args, **kwargs)
        if startup_calls[0] == 0:
            clock[0] += 2.
        startup_calls[0] += 1
        return result

    def delayed_repair(*args, **kwargs):
        result = real_repair(*args, **kwargs)
        clock[0] += 1.
        return result

    monkeypatch.setattr(alns, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(alns, "evaluate_solution", startup_delay)
    monkeypatch.setattr(alns, "_repair", delayed_repair)
    temperatures = []
    original = alns._accept_worse
    monkeypatch.setattr(alns, "_accept_worse", lambda c, s, p, t, rng:
                        (temperatures.append(t), original(c, s, p, t, rng))[1])
    cfg = ALNSConfig(max_iterations=100, time_limit_seconds=6.,
                     enable_budget_cooling=True, relay_enabled=False)
    result = solve_alns(p, config=cfg, initial_routes=initial.routes)
    assert temperatures == pytest.approx([
        alns._budget_temperature(cfg, i / 4) for i in (1, 2, 3)])
    assert result.runtime_seconds == 6
    assert result.evaluation.valid
    assert result.evaluation.score <= initial.evaluation.score


def test_single_drone_does_not_change_search_decisions():
    p = problem(drones=1)
    initial = construct_regret_initial(p)
    cfg = ALNSConfig(max_iterations=30, seed=2026101401, relay_enabled=False)
    baseline = solve_alns(p, config=cfg, initial_routes=initial.routes)
    candidate = solve_alns(p, config=replace(cfg, enable_budget_cooling=True),
                           initial_routes=initial.routes)
    assert decisions(candidate) == decisions(baseline)
    assert not candidate.metadata["budget_cooling_active"]


def test_zero_iteration_budget_preserves_construction():
    p = problem()
    baseline = solve_alns(p, config=ALNSConfig(max_iterations=0))
    candidate = solve_alns(p, config=ALNSConfig(max_iterations=0,
                                              enable_budget_cooling=True))
    assert candidate.routes == baseline.routes
    assert candidate.evaluation == baseline.evaluation
