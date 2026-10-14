"""Terminal refinement changes only the incumbent after the ALNS main loop."""
from dataclasses import asdict, replace
from random import Random

import pytest

import uav_dispatch.alns as alns
import uav_dispatch.incumbent_polish as polishing
from uav_dispatch import ALNSConfig, Point, Problem, RelayStation, SolverResult, Task, TransportLeg, evaluate_solution


def _problem(relay=False):
    problem = Problem(tuple(Task(i, Point(x, 0), Point(x, 0), 15.)
                            for i, x in enumerate((10., -11., -10., 11.), 1)),
                      drone_count=2, max_tasks_per_drone=2, speed_km_per_min=1.)
    if relay:
        problem = replace(problem, relay_stations=(RelayStation(1, 0, 0),),
                          leg_registry={5: TransportLeg(5, 1, "RELAY_IN", 1),
                                        6: TransportLeg(6, 1, "RELAY_OUT", 1)},
                          task_relay_candidates={1: (1,)})
    return problem, ((1, -1, 2, -2), (3, -3, 4, -4))


def _main_statistics(result):
    return tuple(result.metadata[key] for key in (
        "operator_statistics", "operator_weights", "operator_uses", "accepted_solutions", "final_temperature"))


def _trajectory(result):
    return [(p[0], *p[2:]) for p in result.metadata["best_trajectory"]]


def _score_tuple(score):
    return score.late_count, score.total_lateness_min, score.distance_km


def test_explicitly_disabled_features_preserve_main_search_and_never_call_polish(monkeypatch):
    problem, initial = _problem()
    monkeypatch.setattr(polishing, "polish_direct_incumbent", lambda *a, **k: pytest.fail("disabled"))
    cfg = ALNSConfig(max_iterations=65, seed=31, relay_enabled=False,
                     enable_terminal_polish=False, enable_relay_seed_checkpoint=False)
    first = alns.solve_alns(problem, config=cfg, initial_routes=initial)
    second = alns.solve_alns(problem, config=replace(cfg, enable_terminal_polish=False,
                             enable_relay_budget_rebalance=False), initial_routes=initial)
    assert first.routes == second.routes and first.evaluation == second.evaluation
    assert _main_statistics(first) == _main_statistics(second)
    assert _trajectory(first) == _trajectory(second)
    assert first.metadata["terminal_polish"]["active"] is False


def test_single_drone_feature_is_inactive_and_decisions_match(monkeypatch):
    problem, initial = _problem()
    problem = replace(problem, drone_count=1, max_tasks_per_drone=4)
    initial = (initial[0] + initial[1],)
    cfg = ALNSConfig(max_iterations=35, seed=32, relay_enabled=False)
    baseline = alns.solve_alns(problem, config=cfg, initial_routes=initial)
    monkeypatch.setattr(polishing, "polish_direct_incumbent", lambda *a, **k: pytest.fail("single UAV"))
    result = alns.solve_alns(problem, config=replace(cfg, enable_terminal_polish=True), initial_routes=initial)
    assert result.routes == baseline.routes and _main_statistics(result) == _main_statistics(baseline)
    assert _trajectory(result) == _trajectory(baseline)
    assert result.metadata["terminal_polish"]["active"] is False
    assert result.metadata["terminal_polish"]["reserved_seconds"] == 0.


def test_terminal_changes_best_only_after_identical_fixed_iteration_main_loop(monkeypatch):
    problem, initial = _problem()
    improved = ((1, -1, 4, -4), (3, -3, 2, -2))
    generators, events = [], []
    def random_factory(seed):
        generator = Random(seed)
        generators.append(generator)
        return generator
    monkeypatch.setattr(alns, "Random", random_factory)
    monkeypatch.setattr(alns, "_repair", lambda *a, **k: initial)
    cfg = ALNSConfig(max_iterations=17, seed=33, relay_enabled=False,
                     enable_adjacent_polish=False, enable_terminal_polish=False)
    baseline = alns.solve_alns(problem, config=cfg, initial_routes=initial)
    def terminal(p, evaluator, routes, **kwargs):
        assert routes == baseline.routes
        assert generators[-1].getstate() == generators[0].getstate()
        assert kwargs["deadline"] is not None
        kwargs["statistics"]["fixture_pass"] = 1
        events.append("terminal")
        return improved
    monkeypatch.setattr(polishing, "polish_direct_incumbent", terminal)
    result = alns.solve_alns(problem, config=replace(cfg, enable_terminal_polish=True), initial_routes=initial)
    assert events == ["terminal"] and result.iterations == baseline.iterations == 17
    assert _main_statistics(result) == _main_statistics(baseline)
    assert generators[-1].getstate() == generators[0].getstate()
    assert result.routes == improved and result.evaluation.score < baseline.evaluation.score
    assert _trajectory(result)[:-1] == _trajectory(baseline)
    assert result.metadata["best_trajectory"][-1][0] == 17
    assert result.metadata["best_trajectory"][-1][2:] == _score_tuple(result.evaluation.score)
    metadata = result.metadata["terminal_polish"]
    assert metadata["accepted"] and metadata["before_score"] == _score_tuple(baseline.evaluation.score)
    assert metadata["after_score"] == _score_tuple(result.evaluation.score)
    assert metadata["statistics"] == {"fixture_pass": 1}


@pytest.mark.parametrize("budget,expected", [(100., .1), (2., .02), (.1, .001), (None, .1)])
def test_early_iteration_stop_cannot_spend_beyond_small_terminal_allowance(monkeypatch, budget, expected):
    problem, initial = _problem()
    now, calls = [10.], []
    monkeypatch.setattr(alns, "perf_counter", lambda: now[0])
    def terminal(p, evaluator, routes, **kwargs):
        calls.append(kwargs["deadline"])
        assert kwargs["deadline"] - now[0] == pytest.approx(expected)
        return routes
    monkeypatch.setattr(polishing, "polish_direct_incumbent", terminal)
    result = alns.solve_alns(problem, initial_routes=initial, config=ALNSConfig(
        max_iterations=0, time_limit_seconds=budget, enable_terminal_polish=True, relay_enabled=False))
    assert len(calls) == 1
    reserve = result.metadata["terminal_polish"]["reserved_seconds"]
    assert reserve == pytest.approx(expected) and reserve <= .1
    if budget is not None:
        assert reserve <= budget * .01 + 1e-12


def test_reserve_uses_post_construction_remaining_time_and_shortens_main_deadline(monkeypatch):
    problem, initial = _problem()
    now, deadlines = [0.], []
    original_validate = alns.evaluate_solution
    def validate(*args, **kwargs):
        result = original_validate(*args, **kwargs)
        now[0] = max(now[0], .5)
        return result
    monkeypatch.setattr(alns, "perf_counter", lambda: now[0])
    monkeypatch.setattr(alns, "evaluate_solution", validate)
    def repair(*args, **kwargs):
        deadlines.append(kwargs["deadline"])
        return initial
    monkeypatch.setattr(alns, "_repair", repair)
    monkeypatch.setattr(polishing, "polish_direct_incumbent", lambda p, e, r, **k: r)
    result = alns.solve_alns(problem, initial_routes=initial,
        config=ALNSConfig(max_iterations=1, time_limit_seconds=2., enable_terminal_polish=True))
    assert result.metadata["terminal_polish"]["reserved_seconds"] == pytest.approx(.015)
    assert deadlines == [pytest.approx(1.985)]


def test_improved_terminal_result_returned_after_its_deadline_is_rejected(monkeypatch):
    problem, initial = _problem()
    improved = ((1, -1, 4, -4), (3, -3, 2, -2))
    now = [0.]
    monkeypatch.setattr(alns, "perf_counter", lambda: now[0])
    def terminal(*args, **kwargs):
        now[0] = kwargs["deadline"] + .001
        return improved
    monkeypatch.setattr(polishing, "polish_direct_incumbent", terminal)
    result = alns.solve_alns(problem, initial_routes=initial,
        config=ALNSConfig(max_iterations=0, time_limit_seconds=2., enable_terminal_polish=True))
    assert result.routes == initial and not result.metadata["terminal_polish"]["accepted"]
    assert len(result.metadata["best_trajectory"]) == 1


@pytest.mark.parametrize("rebalanced,expected_fraction", [(False, .8), (True, .95)])
def test_staged_budget_and_terminal_metadata_follow_actual_direct_phase(monkeypatch, rebalanced, expected_fraction):
    problem, initial = _problem(relay=True)
    real_solve, phases, terminal_calls = alns.solve_alns, [], []
    def terminal(p, evaluator, routes, **kwargs):
        terminal_calls.append(routes)
        kwargs["statistics"]["fixture_pass"] = 1
        return routes
    monkeypatch.setattr(polishing, "polish_direct_incumbent", terminal)
    def solve(p, *, config, initial_routes):
        # Capture actual per-phase budgets, then avoid clock-bound test work.
        result = real_solve(p, config=config, initial_routes=initial_routes)
        phases.append((config, tuple(initial_routes), result))
        return result
    monkeypatch.setattr(alns, "solve_alns", solve)
    cfg = ALNSConfig(max_iterations=20, time_limit_seconds=10., seed=35,
                     relay_direct_warmup_fraction=.8, enable_terminal_polish=True,
                     enable_relay_budget_rebalance=rebalanced, enable_relay_seed=False, enable_relay_refine=False)
    result = alns.solve_relay_staged(problem, config=cfg, initial_routes=initial)
    assert len(phases) == 2 and len(terminal_calls) == 1
    assert [phase[0].relay_enabled for phase in phases] == [False, True]
    assert [phase[0].time_limit_seconds for phase in phases] == pytest.approx([10 * expected_fraction, 10 * (1 - expected_fraction)])
    assert phases[0][0].max_iterations == round(20 * expected_fraction)
    assert phases[1][1] == phases[0][2].routes
    assert cfg.relay_direct_warmup_fraction == .8
    assert result.metadata["relay_direct_warmup_fraction"] == expected_fraction
    warm = dict(phases[0][2].metadata["terminal_polish"])
    assert dict(result.metadata["terminal_polish"]) == {**warm, "phase": "direct_warmup"}
    assert result.metadata["terminal_polish"]["statistics"] == {"fixture_pass": 1}
    assert phases[1][2].metadata["terminal_polish"]["active"] is False
    checked = evaluate_solution(problem, result.routes, _force_full=True)
    assert checked.valid and checked.score == result.evaluation.score
    assert result.metadata["best_trajectory"][-1][2:] == _score_tuple(checked.score)
    assert result.iterations == sum(p[2].iterations for p in phases)


def test_rebalance_does_not_change_direct_only_search(monkeypatch):
    problem, initial = _problem()
    cfg = ALNSConfig(max_iterations=40, seed=36, relay_enabled=False)
    baseline = alns.solve_relay_staged(problem, config=cfg, initial_routes=initial)
    candidate = alns.solve_relay_staged(problem, config=replace(cfg, enable_relay_budget_rebalance=True), initial_routes=initial)
    assert candidate.routes == baseline.routes and _main_statistics(candidate) == _main_statistics(baseline)
    assert _trajectory(candidate) == _trajectory(baseline)


def test_promoted_defaults_enable_validated_features_without_forcing_custom_ratios():
    cfg = ALNSConfig()
    assert cfg.enable_terminal_polish is True
    assert cfg.enable_relay_seed_checkpoint is True
    assert cfg.relay_direct_warmup_fraction == .95
    assert cfg.enable_relay_budget_rebalance is False


def _capture_staged_configs(monkeypatch, problem, initial, config):
    """Inspect real staging and canonical checks without an ALNS search."""
    captured = []
    def phase(p, *, config, initial_routes):
        captured.append((config, tuple(initial_routes)))
        checked = evaluate_solution(p, initial_routes, _force_full=True)
        return SolverResult(
            routes=tuple(initial_routes), evaluation=checked, iterations=config.max_iterations,
            runtime_seconds=.002, metadata={
                "initial_score": _score_tuple(checked.score),
                "best_trajectory": ((0, .001, *_score_tuple(checked.score)),),
            },
        )
    monkeypatch.setattr(alns, "solve_alns", phase)
    result = alns.solve_relay_staged(problem, config=config, initial_routes=initial)
    return captured, result


@pytest.mark.parametrize("budget", [None, 10.])
def test_public_relay_defaults_match_measured_rebalanced_effective_phase_configs(monkeypatch, budget):
    problem, initial = _problem(relay=True)
    public = ALNSConfig(max_iterations=100, time_limit_seconds=budget, seed=37)
    measured = replace(public, relay_direct_warmup_fraction=.8, enable_relay_budget_rebalance=True)
    public_phases, public_result = _capture_staged_configs(monkeypatch, problem, initial, public)
    measured_phases, measured_result = _capture_staged_configs(monkeypatch, problem, initial, measured)
    assert len(public_phases) == len(measured_phases) == 2
    for (left, left_routes), (right, right_routes) in zip(public_phases, measured_phases, strict=True):
        # These two provenance fields differ, but solve_alns does not use
        # either to search; the real staged wrapper has resolved the budgets.
        left_fields, right_fields = asdict(left), asdict(right)
        for field in ("relay_direct_warmup_fraction", "enable_relay_budget_rebalance"):
            left_fields.pop(field)
            right_fields.pop(field)
        assert left_fields == right_fields
        assert left_routes == right_routes == initial
        assert left.enable_terminal_polish and left.enable_relay_seed_checkpoint
    assert [cfg.max_iterations for cfg, _ in public_phases] == [95, 5]
    assert [cfg.relay_enabled for cfg, _ in public_phases] == [False, True]
    assert public_result.metadata["relay_direct_warmup_fraction"] == .95
    assert public_result.routes == measured_result.routes
    assert public_result.evaluation == measured_result.evaluation


@pytest.mark.parametrize("fraction,expected_iterations", [(0., [100]), (.5, [50, 50]), (.8, [80, 20])])
def test_explicit_public_warmup_fraction_preserves_requested_search_split(monkeypatch, fraction, expected_iterations):
    problem, initial = _problem(relay=True)
    config = ALNSConfig(max_iterations=100, time_limit_seconds=10., relay_direct_warmup_fraction=fraction)
    phases, _ = _capture_staged_configs(monkeypatch, problem, initial, config)
    assert [cfg.max_iterations for cfg, _ in phases] == expected_iterations
    assert [cfg.time_limit_seconds for cfg, _ in phases] == pytest.approx(
        [10.] if fraction == 0 else [10 * fraction, 10 * (1 - fraction)])
    assert [cfg.relay_enabled for cfg, _ in phases] == ([True] if fraction == 0 else [False, True])
    assert all(not cfg.enable_relay_budget_rebalance for cfg, _ in phases)


def test_public_direct_defaults_match_measured_terminal_decisions():
    problem, initial = _problem()
    public = ALNSConfig(max_iterations=17, seed=38, relay_enabled=False)
    measured = replace(public, relay_direct_warmup_fraction=.8, enable_relay_seed_checkpoint=False)
    first = alns.solve_alns(problem, config=public, initial_routes=initial)
    second = alns.solve_alns(problem, config=measured, initial_routes=initial)
    assert first.routes == second.routes and first.evaluation == second.evaluation
    assert _main_statistics(first) == _main_statistics(second)
    assert _trajectory(first) == _trajectory(second)
    assert first.metadata["terminal_polish"]["active"] is True
    assert first.metadata["relay_seed_checkpoint_active"] is False
