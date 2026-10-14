"""Global DIRECT operators stay bounded, budgeted, and visible across stages."""

from dataclasses import replace

import pytest

import uav_dispatch.alns as alns
import uav_dispatch.home_assignment as home_assignment
import uav_dispatch.route_exchange as route_exchange
from uav_dispatch import (
    ALNSConfig, Point, Problem, RelayStation, Task, TransportLeg, evaluate_solution,
)


def _problem(relay=False):
    problem = Problem(
        tuple(Task(i, Point(x, 0), Point(x + 1, 0), 4 + i)
              for i, x in ((1, 10), (2, 11), (3, 0), (4, 1), (5, 5), (6, 6))),
        drone_count=3, max_tasks_per_drone=2, speed_km_per_min=1,
        drone_homes=(Point(0, 0), Point(10, 0), Point(5, 0)),
    )
    if relay:
        problem = replace(problem, relay_stations=(RelayStation(1, 5, 0),),
                          leg_registry={7: TransportLeg(7, 1, "RELAY_IN", 1),
                                        8: TransportLeg(8, 1, "RELAY_OUT", 1)},
                          task_relay_candidates={1: (1,)})
    return problem, ((1, -1, 2, -2), (3, -3, 4, -4), (5, -5, 6, -6))


def _decisions(result):
    return (result.routes, result.evaluation, result.iterations,
            result.metadata["operator_statistics"], result.metadata["operator_weights"],
            result.metadata["operator_uses"], result.metadata["accepted_solutions"],
            [(point[0], *point[2:]) for point in result.metadata["best_trajectory"]])


def test_global_moves_observe_fixed_intervals_and_preserve_common_initial_score(monkeypatch):
    problem, initial = _problem()
    seen_tails, seen_homes = [], []
    actual_tail, actual_home = route_exchange.bounded_tail_exchange, home_assignment.reassign_route_homes
    def tail(*args, **kwargs):
        seen_tails.append((kwargs["pair_offset"], kwargs["max_pairs"], kwargs["deadline"]))
        return actual_tail(*args, **kwargs)
    def home(*args, **kwargs):
        seen_homes.append(kwargs["deadline"])
        return actual_home(*args, **kwargs)
    monkeypatch.setattr(route_exchange, "bounded_tail_exchange", tail)
    monkeypatch.setattr(home_assignment, "reassign_route_homes", home)
    cfg = ALNSConfig(max_iterations=80, seed=9, relay_enabled=False,
                     enable_tail_exchange=True, enable_home_reassignment=True)
    result = alns.solve_alns(problem, config=cfg, initial_routes=initial)
    before = evaluate_solution(problem, initial, _force_full=True).score
    assert seen_tails == [(0, 4, None), (4, 4, None), (8, 4, None), (12, 4, None)]
    assert seen_homes == [None, None]
    assert result.metadata["tail_exchange_active"] is True
    assert result.metadata["home_reassignment_active"] is True
    assert result.metadata["tail_exchange_statistics"]["pair_checks"] == 12
    assert result.metadata["home_reassignment_statistics"]["passes"] == 2
    assert result.metadata["initial_score"] == (before.late_count, before.total_lateness_min, before.distance_km)
    assert result.evaluation.valid and result.evaluation.score <= before


def test_single_drone_disables_both_moves_without_changing_search_decisions(monkeypatch):
    problem, initial = _problem()
    problem = replace(problem, drone_count=1, max_tasks_per_drone=6, drone_homes=(Point(0, 0),))
    initial = (tuple(visit for route in initial for visit in route),)
    cfg = ALNSConfig(max_iterations=45, seed=4, relay_enabled=False)
    baseline = alns.solve_alns(problem, config=cfg, initial_routes=initial)
    monkeypatch.setattr(route_exchange, "bounded_tail_exchange", lambda *a, **k: pytest.fail("single-UAV tail move"))
    monkeypatch.setattr(home_assignment, "reassign_route_homes", lambda *a, **k: pytest.fail("single-UAV home move"))
    result = alns.solve_alns(problem, initial_routes=initial,
                             config=replace(cfg, enable_tail_exchange=True, enable_home_reassignment=True))
    assert _decisions(result) == _decisions(baseline)
    assert result.metadata["tail_exchange_active"] is False
    assert result.metadata["home_reassignment_active"] is False
    assert dict(result.metadata["tail_exchange_statistics"]) == {}
    assert dict(result.metadata["home_reassignment_statistics"]) == {}


def test_relay_phase_disables_direct_global_moves_without_changing_decisions(monkeypatch):
    problem, initial = _problem(relay=True)
    cfg = ALNSConfig(max_iterations=40, seed=7, enable_relay_seed=False, enable_relay_refine=False)
    baseline = alns.solve_alns(problem, config=cfg, initial_routes=initial)
    monkeypatch.setattr(route_exchange, "bounded_tail_exchange", lambda *a, **k: pytest.fail("relay phase tail move"))
    monkeypatch.setattr(home_assignment, "reassign_route_homes", lambda *a, **k: pytest.fail("relay phase home move"))
    result = alns.solve_alns(problem, initial_routes=initial,
                             config=replace(cfg, enable_tail_exchange=True, enable_home_reassignment=True))
    assert _decisions(result) == _decisions(baseline)
    assert result.metadata["tail_exchange_active"] is False
    assert result.metadata["home_reassignment_active"] is False
    assert dict(result.metadata["tail_exchange_statistics"]) == {}
    assert dict(result.metadata["home_reassignment_statistics"]) == {}


def test_same_home_fleet_disables_home_reassignment(monkeypatch):
    problem, initial = _problem()
    problem = replace(problem, drone_homes=None)
    monkeypatch.setattr(home_assignment, "reassign_route_homes", lambda *a, **k: pytest.fail("same-home permutation"))
    result = alns.solve_alns(problem, initial_routes=initial,
                             config=ALNSConfig(max_iterations=40, enable_home_reassignment=True))
    assert result.metadata["home_reassignment_active"] is False
    assert dict(result.metadata["home_reassignment_statistics"]) == {}


@pytest.mark.parametrize("kind,iterations", [("tail", 20), ("home", 40)])
def test_operator_result_completed_after_budget_is_not_an_incumbent(monkeypatch, kind, iterations):
    problem = Problem(
        (Task(1, Point(10, 0), Point(11, 0), 2), Task(2, Point(0, 0), Point(1, 0), 2)),
        drone_count=2, max_tasks_per_drone=1, speed_km_per_min=1,
        drone_homes=(Point(0, 0), Point(10, 0)),
    )
    initial = ((1, -1), (2, -2))
    improved = tuple(reversed(initial))
    clock = [0.0]
    monkeypatch.setattr(alns, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(alns, "_repair", lambda *a, **k: initial)
    def overdue_result(*args, **kwargs):
        assert kwargs["deadline"] == 1.0
        clock[0] = 2.0
        return improved
    if kind == "tail":
        monkeypatch.setattr(route_exchange, "bounded_tail_exchange", overdue_result)
    else:
        monkeypatch.setattr(home_assignment, "reassign_route_homes", overdue_result)
    result = alns.solve_alns(
        problem, initial_routes=initial,
        config=ALNSConfig(max_iterations=iterations, time_limit_seconds=1,
                          enable_adjacent_polish=False,
                          enable_tail_exchange=kind == "tail",
                          enable_home_reassignment=kind == "home"),
    )
    assert result.routes == initial
    assert result.evaluation.score.late_count == 2
    assert result.metadata["best_update_count"] == 0
    assert result.metadata["best_trajectory"] == ((0, 0.0, 2, 18.0, 22.0),)


def test_staged_search_combines_complete_work_counters_and_preserves_phase_boundary(monkeypatch):
    problem, initial = _problem(relay=True)
    actual_solve = alns.solve_alns
    phases = []
    def capture(p, *, config, initial_routes):
        result = actual_solve(p, config=config, initial_routes=initial_routes)
        phases.append((config, tuple(initial_routes), result))
        return result
    monkeypatch.setattr(alns, "solve_alns", capture)
    result = alns.solve_relay_staged(
        problem, initial_routes=initial,
        config=ALNSConfig(max_iterations=100, seed=4, relay_direct_warmup_fraction=0.8,
                          enable_tail_exchange=True, enable_home_reassignment=True,
                          enable_relay_seed=False, enable_relay_refine=False),
    )
    assert len(phases) == 2
    assert [phase[0].relay_enabled for phase in phases] == [False, True]
    assert phases[1][1] == phases[0][2].routes
    for name in ("tail_exchange_statistics", "home_reassignment_statistics"):
        warm, relay = (phase[2].metadata[name] for phase in phases)
        assert warm and not relay
        assert dict(result.metadata[name]) == {
            key: warm.get(key, 0) + relay.get(key, 0) for key in warm.keys() | relay.keys()
        }
    assert result.metadata["home_reassignment_statistics"]["passes"] == 2
    assert result.metadata["tail_exchange_statistics"]["pair_checks"] == 12
    # Final-phase activation is false even though combined work includes DIRECT.
    assert result.metadata["tail_exchange_active"] is False
    assert result.metadata["home_reassignment_active"] is False
    assert result.iterations == sum(phase[2].iterations for phase in phases)
    assert result.runtime_seconds == sum(phase[2].runtime_seconds for phase in phases)
    assert result.evaluation.score <= phases[0][2].evaluation.score
