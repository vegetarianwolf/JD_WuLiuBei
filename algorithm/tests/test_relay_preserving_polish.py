"""Tiny pin/repair tests. No official optimizer or official tasks are run."""
from dataclasses import replace
import importlib
import importlib.util
import json
from pathlib import Path
from random import Random
import sys
from time import perf_counter
from types import SimpleNamespace

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch import alns
from uav_dispatch.counters import SearchCounters
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import relay_preserving_polish as polish
from algorithm.experiments import weighted_direct_objective as weighted


WEIGHTS, SCALES = (.5, .25, .25), (9., 100., 100.)
PINS, CROSS = (1, 2, 3, 4), (1, 2)


def fixture(*, direct=False, task9_deadline=10.):
    tasks = (Task(1, Point(0., 0.), Point(10., 0.), 100.),
             Task(2, Point(0., 0.), Point(11., 0.), 100.),
             Task(3, Point(0., 0.), Point(0., 0.), 100.),
             Task(4, Point(0., 0.), Point(0., 0.), 100.),
             Task(5, Point(2., 0.), Point(3., 0.), 100.),
             Task(6, Point(3., 0.), Point(4., 0.), 100.),
             Task(7, Point(4., 0.), Point(3., 0.), 100.),
             Task(8, Point(3., 0.), Point(2., 0.), 100.),
             Task(9, Point(11., 0.), Point(12., 0.), task9_deadline))
    legs = {101 + 2*(t.id-1) + k: TransportLeg(101 + 2*(t.id-1) + k, t.id,
        "RELAY_IN" if k == 0 else "RELAY_OUT", 1) for t in tasks for k in (0, 1)}
    problem = Problem(tasks, drone_count=3, max_tasks_per_drone=3, capacity=2,
        speed_km_per_min=1., drone_homes=(Point(0., 0.), Point(0., 0.), Point(5., 0.)),
        relay_stations=(RelayStation(1, 5., 0.),), leg_registry=legs,
        task_relay_candidates={t.id: (1,) for t in tasks})
    if direct:
        routes = ((3, -3, 5, -5, 6, -6), (4, -4, 7, -7, 8, -8), (1, 2, -1, -2, 9, -9))
    else:
        routes = ((3, -3, 101, -101, 5, -5, 6, -6),
                  (4, -4, 103, -103, 7, -7, 8, -8),
                  (102, 104, -102, -104, 9, -9))
    assert evaluate_solution(problem, routes, _force_full=True).valid
    return problem, routes


def context(module, problem, routes, *, deadline=None, cross=CROSS, stats=None):
    return polish.pinned_search(module, problem, routes, deadline=perf_counter() + 2. if deadline is None else deadline,
        weights=WEIGHTS, scales=SCALES, pinned_task_ids=PINS, required_cross_task_ids=cross, statistics=stats)


def relay_config():
    return alns._RelaySearchConfig(True, 1, 2, None, 2, None, False, counters=SearchCounters())


def fake_module(*, destroy=None, repair=None, solve=None):
    functions = {name: getattr(alns, name) for name in polish.PATCH_NAMES}
    if destroy is not None:
        functions["destroy_solution"] = destroy
    if repair is not None:
        functions["_repair"] = repair
    return SimpleNamespace(**functions, _remove_tasks=alns._remove_tasks,
        _SearchDeadlineReached=alns._SearchDeadlineReached, ALNSConfig=alns.ALNSConfig,
        _weighted_adapter=weighted, solve_alns=solve, Score=polish.Score)


def native_repair(module, problem, partial, removed, deadline):
    return module._repair(problem, RouteEvaluator(problem, counters=SearchCounters()), partial,
        removed, "greedy", 20, deadline=deadline, relay_cfg=relay_config(),
        global_evaluator=GlobalRelayEvaluator(problem), repair_rank_candidate_limit=20,
        repair_exact_candidate_limit=20, direct_exact_top_k=4)


def test_native_direct_repair_keeps_four_macro_plans_full_quota_and_true_cross():
    problem, routes = fixture()
    stats = {}
    deadline = perf_counter() + 2.
    before = {name: getattr(alns, name) for name in polish.PATCH_NAMES}
    with context(alns, problem, routes, deadline=deadline, stats=stats):
        partial, removed = alns.destroy_solution(problem, RouteEvaluator(problem), routes,
            "random", 4, Random(19))
        assert not set(removed) & set(PINS)
        assert len({build_plan_index(problem, routes).delivery_route_by_task[t] for t in removed}) >= 2
        repaired = native_repair(alns, problem, partial, removed, deadline)
        checked = evaluate_solution(problem, repaired, _force_full=True)
        assert checked.valid
        assert polish._signature(problem, repaired, set(PINS)) == polish._signature(problem, routes, set(PINS))
        shape = polish._shape(problem, repaired, set(CROSS))
        assert shape["shared_double_loaded_km"] > 0.
        assert stats["direct_exact_global_evaluations"] > 0
    assert all(getattr(alns, name) is function for name, function in before.items())
    assert stats["patches_restored"]


def test_waiting_offset_exact_global_score_differs_from_old_local_shortcut():
    problem, routes = fixture()
    partial = (routes[0], routes[1], routes[2][:-2])
    evaluator, global_evaluator = RouteEvaluator(problem), GlobalRelayEvaluator(problem)
    delta = evaluator.evaluate(routes[2], start_node=problem.home_node(2)).score - evaluator.evaluate(
        partial[2], start_node=problem.home_node(2)).score
    option = alns.TaskInsertionOption(9, "DIRECT", None, delta, direct_route_index=2,
        direct_route=routes[2], direct_pickup_position=4, direct_delivery_position=5)
    cfg = relay_config()
    _, old_score, _ = alns._apply_task_exact(problem, evaluator, global_evaluator, partial,
        (option,), relay_cfg=cfg, deadline=perf_counter() + 2.)
    assert old_score.late_count == 0 and old_score.total_lateness_min == 0.
    real = GlobalRelayEvaluator(problem).evaluate(routes).score
    assert real.late_count == 1 and real.total_lateness_min == 2.
    with context(alns, problem, routes):
        repaired, exact, mode = alns._apply_task_exact(problem, evaluator, global_evaluator, partial,
            (option,), relay_cfg=cfg, deadline=perf_counter() + 2.)
        assert mode == "DIRECT" and repaired == routes and exact == real


def test_pins_selected_by_original_destroy_are_filtered_and_balanced_topups_are_seeded():
    problem, routes = fixture()
    def selector(p, evaluator, current, operator, count, rng, **kwargs):
        return (), (1, 2, 3, 4)
    module = fake_module(destroy=selector)
    copies = []
    for _ in range(2):
        stats = {}
        with context(module, problem, routes, stats=stats):
            partial, removed = module.destroy_solution(problem, RouteEvaluator(problem), routes,
                "random", 4, Random(5))
            copies.append((partial, removed))
            assert stats["selected_pins_filtered"] == 4 and stats["destroy_topups"] == 4
            assert set(removed) <= {5, 6, 7, 8, 9}
            owners = build_plan_index(problem, routes).delivery_route_by_task
            assert len({owners[t] for t in removed}) >= 2
    assert copies[0] == copies[1]


def test_destroy_one_owner_is_rebalanced_or_skipped_without_reward():
    problem, routes = fixture()
    module = fake_module(destroy=lambda *a, **k: ((), (5, 6)))
    stats = {}
    with context(module, problem, routes, stats=stats):
        partial, removed = module.destroy_solution(problem, RouteEvaluator(problem), routes,
            "random", 2, Random(2))
        assert len({build_plan_index(problem, routes).delivery_route_by_task[t] for t in removed}) == 2
        assert stats["balanced_slot_replacements"] == 1
        current, removed = module.destroy_solution(problem, RouteEvaluator(problem), routes,
            "random", 1, Random(2))
        assert current == routes and removed == ()
        candidate = module._repair(problem, RouteEvaluator(problem), current, removed,
            "greedy", 20, deadline=perf_counter() + 2.)
        assert candidate == routes
        checked = evaluate_solution(problem, routes, _force_full=True)
        assert not module._accept_worse(checked.score, checked.score, problem, 1., Random(3))
        assert stats["rejected_acceptances"] == 1


def test_plan_generator_probes_only_direct_and_refuses_pinned_tasks():
    problem, routes = fixture()
    probes = []
    def generator(*args, **kwargs):
        probes.append(kwargs["probe_relay"])
        return ()
    module = fake_module()
    module._all_plans_for_task = generator
    with context(module, problem, routes):
        assert module._all_plans_for_task(problem, None, routes, 5, probe_relay=True, deadline=None) == ()
        assert probes == [False]
        with pytest.raises(ValueError, match="pinned task"):
            module._all_plans_for_task(problem, None, routes, 1, deadline=None)


@pytest.mark.parametrize("kind", ["direct_conversion", "pin_owner", "pin_order", "noncontiguous_takes", "quota", "capacity"])
def test_complete_invalid_or_identity_losing_repair_rolls_back_and_cannot_earn_credit(kind):
    problem, routes = fixture()
    candidate = list(routes)
    if kind == "direct_conversion":
        candidate[0] = tuple(v for v in candidate[0] if abs(v) != 101)
        candidate[2] = tuple(1 if v == 102 else -1 if v == -102 else v for v in candidate[2])
    elif kind == "pin_owner":
        candidate[0] = tuple(v for v in candidate[0] if abs(v) != 3)
        candidate[1] = (3, -3) + candidate[1]
    elif kind == "pin_order":
        candidate[0] = (101, -101, 3, -3) + candidate[0][4:]
    elif kind == "noncontiguous_takes":
        candidate[2] = (102, 9, -9, 104, -102, -104)
    elif kind == "quota":
        candidate[0] = tuple(v for v in candidate[0] if abs(v) != 5)
        candidate[1] = candidate[1] + (5, -5)
    elif kind == "capacity":
        candidate[2] = (102, 104, 9, -102, -104, -9)
    candidate = tuple(candidate)
    module = fake_module(destroy=lambda *a, **k: ((), (5, 7)), repair=lambda *a, **k: candidate)
    stats = {}
    with context(module, problem, routes, stats=stats):
        partial, removed = module.destroy_solution(problem, RouteEvaluator(problem), routes,
            "random", 2, Random(3))
        result = module._repair(problem, None, partial, removed, "greedy", 20, deadline=None)
        assert result == routes
        score = evaluate_solution(problem, routes, _force_full=True).score
        rng = Random(4)
        before_rng = rng.getstate()
        assert not module._accept_worse(score, score, problem, 1., rng)
        assert rng.getstate() == before_rng
        assert stats["rollback_candidates"] == stats["rejected_acceptances"] == 1


def test_positive_shared_distance_is_required_and_zero_distance_is_rejected():
    problem, routes = fixture()
    shape = polish._shape(problem, routes, set(CROSS))
    assert shape["shared_double_loaded_km"] == 5.
    tasks = tuple(replace(t, delivery=Point(5., 0.)) if t.id in CROSS else t for t in problem.tasks)
    zero = replace(problem, tasks=tasks)
    assert evaluate_solution(zero, routes, _force_full=True).valid
    with pytest.raises(ValueError, match="positive distance"):
        with context(alns, zero, routes):
            pass


def test_complete_global_cycle_rolls_back_to_finite_checked_current():
    problem, routes = fixture()
    # New task5 IN on C after receiving OUT1; task5 OUT on A before dropping
    # IN1. This complete capacity-safe plan has C->A->C precedence deadlock.
    candidate = ((3, -3, 110, -110, 101, -101, 6, -6), routes[1],
                 (102, 104, -102, -104, 109, -109, 9, -9))
    assert not GlobalRelayEvaluator(problem).evaluate(candidate).feasible
    module = fake_module(destroy=lambda *a, **k: ((), (5, 7)), repair=lambda *a, **k: candidate)
    with context(module, problem, routes) as state:
        partial, removed = module.destroy_solution(problem, None, routes, "random", 2, Random(3))
        rolled = module._repair(problem, None, partial, removed, "greedy", 20)
        assert rolled == routes and state["best_checked"].valid
        score = evaluate_solution(problem, rolled, _force_full=True).score
        assert score.distance_km < float("inf")
        assert not module._accept_worse(score, score, problem, 1., Random(2))


def test_direct_control_has_same_four_pins_and_relay_timing_mode_but_no_cross():
    problem, routes = fixture(direct=True)
    stats = {}
    with context(alns, problem, routes, cross=(), stats=stats):
        partial, removed = alns.destroy_solution(problem, RouteEvaluator(problem), routes,
            "random", 2, Random(15))
        candidate = native_repair(alns, problem, partial, removed, perf_counter() + 2.)
        assert evaluate_solution(problem, candidate, _force_full=True).valid
        assert build_plan_index(problem, candidate).relay_task_ids == ()
        assert polish._signature(problem, candidate, set(PINS)) == polish._signature(problem, routes, set(PINS))
    assert stats["required_cross_task_ids"] == []


def test_exception_nested_and_no_change_restore_every_patch():
    problem, routes = fixture()
    module = fake_module()
    originals = {name: getattr(module, name) for name in polish.PATCH_NAMES}
    stats = {}
    with pytest.raises(RuntimeError, match="deliberate"):
        with context(module, problem, routes, stats=stats):
            with pytest.raises(RuntimeError, match="nested"):
                with context(module, problem, routes):
                    pass
            raise RuntimeError("deliberate")
    assert stats["patches_restored"] and not polish._active
    assert all(getattr(module, name) is fn for name, fn in originals.items())
    with context(module, problem, routes):
        pass
    assert all(getattr(module, name) is fn for name, fn in originals.items())


def test_expired_and_overrun_repair_are_never_saved(monkeypatch):
    problem, routes = fixture()
    module = fake_module()
    with context(module, problem, routes, deadline=perf_counter() - 1.):
        with pytest.raises(alns._SearchDeadlineReached):
            module.destroy_solution(problem, None, routes, "random", 2, Random(1))
    clock = [0.]
    monkeypatch.setattr(polish, "perf_counter", lambda: clock[0])
    def slow(*args, **kwargs):
        clock[0] = 2.
        return routes
    module = fake_module(destroy=lambda *a, **k: ((), (5, 7)), repair=slow)
    stats = {}
    with context(module, problem, routes, deadline=1., stats=stats) as state:
        partial, removed = module.destroy_solution(problem, None, routes, "random", 2, Random(3))
        with pytest.raises(alns._SearchDeadlineReached):
            module._repair(problem, None, partial, removed, "greedy", 20)
        assert state["best_routes"] == routes and state["best_checked_at_perf_counter"] is None


def test_complete_full_confirmation_finishing_after_deadline_is_not_a_checkpoint(monkeypatch):
    problem, routes = fixture()
    clock, evaluations = [0.], [0]
    original = polish.evaluate_solution
    monkeypatch.setattr(polish, "perf_counter", lambda: clock[0])
    def slow_full(*args, **kwargs):
        result = original(*args, **kwargs)
        evaluations[0] += 1
        if evaluations[0] > 1:
            clock[0] = 2.
        return result
    monkeypatch.setattr(polish, "evaluate_solution", slow_full)
    module = fake_module(destroy=lambda *a, **k: ((), (5, 7)), repair=lambda *a, **k: routes)
    with context(module, problem, routes, deadline=1.) as state:
        partial, removed = module.destroy_solution(problem, None, routes, "random", 2, Random(3))
        with pytest.raises(alns._SearchDeadlineReached):
            module._repair(problem, None, partial, removed, "greedy", 20)
        assert state["best_routes"] == routes and state["best_checked_at_perf_counter"] is None


def test_direct_exact_global_evaluation_overrun_is_not_returned(monkeypatch):
    problem, routes = fixture()
    clock = [0.]
    monkeypatch.setattr(polish, "perf_counter", lambda: clock[0])
    global_evaluator = GlobalRelayEvaluator(problem)
    original = global_evaluator.evaluate_delta
    def delayed(*args, **kwargs):
        result = original(*args, **kwargs)
        clock[0] = 2.
        return result
    monkeypatch.setattr(global_evaluator, "evaluate_delta", delayed)
    partial = (routes[0], routes[1], routes[2][:-2])
    option = alns.TaskInsertionOption(9, "DIRECT", None, polish.Score(0, 0., 1.),
        direct_route_index=2, direct_route=routes[2], direct_pickup_position=4, direct_delivery_position=5)
    with context(alns, problem, routes, deadline=1.):
        with pytest.raises(alns._SearchDeadlineReached):
            alns._apply_task_exact(problem, RouteEvaluator(problem), global_evaluator, partial,
                (option,), relay_cfg=relay_config(), deadline=1.)


def test_polish_margin_gate_skips_solver_and_never_means_accepting_worse_macro():
    problem, routes = fixture()
    calls = []
    module = fake_module(solve=lambda *a, **k: calls.append(k))
    key = polish.mobile.weighted_key(evaluate_solution(problem, routes, _force_full=True).score, WEIGHTS, SCALES)
    result = polish.polish_pinned_macro(problem, routes, deadline=perf_counter() + 2., weights=WEIGHTS,
        scales=SCALES, seed=71, pinned_task_ids=PINS, required_cross_task_ids=CROSS,
        comparison_key=(key[0]-.01, *key[1:]), proposal_margin=.001, alns_module=module)
    assert not calls and not result["metadata"]["adoptable"]
    assert result["metadata"]["skipped_reason"] == "predeclared_weighted_proposal_margin"
    assert result["routes"] == routes and result["evaluation"].valid
    assert result["metadata"]["patches_restored"] and not weighted.active
    json.dumps(result["metadata"], allow_nan=False)


def test_helper_uses_predeadline_checked_improvement_not_unchecked_solver_result():
    problem, routes = fixture(task9_deadline=100.)
    problem = replace(problem, tasks=tuple(
        replace(t, pickup=Point(10., 0.), delivery=Point(11., 0.)) if t.id == 5 else
        replace(t, pickup=Point(2., 0.), delivery=Point(3., 0.)) if t.id == 8 else t
        for t in problem.tasks))
    candidate = list(routes)
    # Swap two entire unpinned DIRECT labels across A/B; signs/capacity/quotas
    # and all four pinned plans remain identical.
    candidate[0] = tuple(8 if v == 5 else -8 if v == -5 else v for v in candidate[0])
    candidate[1] = tuple(5 if v == 8 else -5 if v == -8 else v for v in candidate[1])
    candidate = tuple(candidate)
    module = None
    def run_tiny(p, config, initial_routes):
        assert config.relay_enabled and not config.enable_relay_seed and not config.enable_relay_refine
        partial, removed = module.destroy_solution(p, RouteEvaluator(p), initial_routes, "random", 2, Random(config.seed))
        module._repair(p, None, partial, removed, "greedy", 20)
        return SimpleNamespace(iterations=1, routes=((999,),))  # Deliberately untrusted reporting result.
    module = fake_module(destroy=lambda *a, **k: ((), (5, 8)), repair=lambda *a, **k: candidate, solve=run_tiny)
    initial = evaluate_solution(problem, routes, _force_full=True)
    improved = evaluate_solution(problem, candidate, _force_full=True)
    assert improved.valid and improved.score.distance_km < initial.score.distance_km
    result = polish.polish_pinned_macro(problem, routes, deadline=perf_counter() + 2., weights=WEIGHTS,
        scales=SCALES, seed=13, pinned_task_ids=PINS, required_cross_task_ids=CROSS,
        comparison_key=polish.mobile.weighted_key(initial.score, WEIGHTS, SCALES), alns_module=module)
    assert result["routes"] == candidate and result["evaluation"].score == improved.score
    assert result["metadata"]["adoptable"] and result["metadata"]["accepted_improvements"] == 1
    assert result["metadata"]["post_pin_snapshot"]["cross_uav_count"] == 2
    assert result["metadata"]["patches_restored"]


def test_helper_timeout_and_solver_exceptions_restore_weighted_objective_and_patches(monkeypatch):
    problem, routes = fixture()
    key = polish.mobile.weighted_key(evaluate_solution(problem, routes, _force_full=True).score, WEIGHTS, SCALES)
    def timeout(*args, **kwargs):
        raise alns._SearchDeadlineReached
    module = fake_module(solve=timeout)
    originals = {name: getattr(module, name) for name in polish.PATCH_NAMES}
    result = polish.polish_pinned_macro(problem, routes, deadline=perf_counter() + 2., weights=WEIGHTS,
        scales=SCALES, seed=1, pinned_task_ids=PINS, required_cross_task_ids=CROSS,
        comparison_key=key, alns_module=module)
    assert result["routes"] == routes and result["metadata"]["skipped_reason"] == "search_reached_absolute_deadline"
    assert not weighted.active and all(getattr(module, n) is f for n, f in originals.items())
    module.solve_alns = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("solver error"))
    with pytest.raises(RuntimeError, match="solver error"):
        polish.polish_pinned_macro(problem, routes, deadline=perf_counter() + 2., weights=WEIGHTS,
            scales=SCALES, seed=1, pinned_task_ids=PINS, required_cross_task_ids=CROSS,
            comparison_key=key, alns_module=module)
    assert not weighted.active and not polish._active
    assert all(getattr(module, n) is f for n, f in originals.items())


def test_unweighted_module_or_incompatible_pins_are_rejected():
    problem, routes = fixture()
    key = polish.mobile.weighted_key(evaluate_solution(problem, routes, _force_full=True).score, WEIGHTS, SCALES)
    with pytest.raises(ValueError, match="weighted-transformed"):
        polish.polish_pinned_macro(problem, routes, deadline=perf_counter() + 2., weights=WEIGHTS,
            scales=SCALES, seed=1, pinned_task_ids=PINS, required_cross_task_ids=CROSS,
            comparison_key=key, alns_module=alns)
    with pytest.raises(ValueError, match="four"):
        with polish.pinned_search(alns, problem, routes, deadline=perf_counter() + 2.,
            weights=WEIGHTS, scales=SCALES, pinned_task_ids=(1, 2), required_cross_task_ids=CROSS):
            pass


@pytest.fixture
def real_weighted_runtime(tmp_path, monkeypatch):
    """Freeze/transform both hot modules into an isolated temporary package."""
    source = Path(alns.__file__).parent
    package_name = "tiny_pinned_weighted_runtime"
    package = tmp_path / package_name
    package.mkdir()
    for path in source.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name in weighted.EXPECTED_COUNTS:
            text, counts = weighted.transform(text)
            assert counts == weighted.EXPECTED_COUNTS[path.name]
        (package / path.name).write_text(text, encoding="utf-8")
    monkeypatch.setitem(sys.modules, "weighted_direct_objective", weighted)
    spec = importlib.util.spec_from_file_location(package_name, package / "__init__.py",
        submodule_search_locations=[str(package)])
    runtime = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, package_name, runtime)
    spec.loader.exec_module(runtime)
    module = importlib.import_module(package_name + ".alns")
    assert module._weighted_adapter is weighted and module.Score is not polish.Score
    try:
        yield module
    finally:
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(package_name + "."):
                sys.modules.pop(name, None)


@pytest.mark.parametrize("direct", [False, True])
def test_real_transformed_alns_loop_cooperates_with_all_five_patches(real_weighted_runtime, direct):
    module = real_weighted_runtime
    problem, routes = fixture(direct=direct)
    original = {name: getattr(module, name) for name in polish.PATCH_NAMES}
    checked = evaluate_solution(problem, routes, _force_full=True)
    config = module.ALNSConfig(max_iterations=8, relay_min_destroy_fraction=.23,
        relay_max_destroy_fraction=.23, repair_rank_candidate_limit=12,
        repair_exact_candidate_limit=12, direct_exact_top_k=4)
    result = polish.polish_pinned_macro(problem, routes, deadline=perf_counter() + .08,
        weights=WEIGHTS, scales=SCALES, seed=19, pinned_task_ids=PINS,
        required_cross_task_ids=() if direct else CROSS,
        comparison_key=polish.mobile.weighted_key(checked.score, WEIGHTS, SCALES),
        config=config, alns_module=module)
    stats = result["metadata"]
    assert result["evaluation"].valid and result["iterations"] <= 8
    assert stats["destroy_calls"] > 0 and stats["repair_calls"] > 0
    assert stats["direct_plan_calls"] > 0 and stats["direct_exact_global_evaluations"] > 0
    assert stats["complete_valid_candidates"] > 0
    assert polish._signature(problem, result["routes"], set(PINS)) == polish._signature(problem, routes, set(PINS))
    scheduled = GlobalRelayEvaluator(problem).evaluate(result["routes"])
    assert scheduled.feasible and scheduled.score == result["evaluation"].score
    if not direct:
        assert scheduled.total_waiting_min > 0.
        assert stats["post_pin_snapshot"]["cross_uav_count"] == 2
        assert stats["post_pin_snapshot"]["pinned_shape"]["shared_double_loaded_km"] > 0.
    else:
        assert stats["post_pin_snapshot"]["cross_uav_count"] == 0
    assert stats["configuration"]["time_limit_seconds"] <= .08
    assert stats["real_loop_globals_preflight_checked"]
    assert stats["patches_restored"] and not weighted.active and not polish._active
    assert all(getattr(module, name) is function for name, function in original.items())
    json.dumps(stats, allow_nan=False)


def test_real_alns_rejected_shape_has_zero_acceptance_and_zero_operator_reward(real_weighted_runtime, monkeypatch):
    module = real_weighted_runtime
    problem, routes = fixture()
    bad = (routes[0], routes[1], (102, 9, -9, 104, -102, -104))
    assert evaluate_solution(problem, bad, _force_full=True).valid
    module.__dict__["_pinned_bad_candidate"] = bad
    exec("def _pinned_bad_repair(*args, **kwargs):\n    return _pinned_bad_candidate\n", module.__dict__)
    monkeypatch.setattr(module, "_repair", module._pinned_bad_repair)
    checked = evaluate_solution(problem, routes, _force_full=True)
    result = polish.polish_pinned_macro(problem, routes, deadline=perf_counter() + .08,
        weights=WEIGHTS, scales=SCALES, seed=29, pinned_task_ids=PINS,
        required_cross_task_ids=CROSS,
        comparison_key=polish.mobile.weighted_key(checked.score, WEIGHTS, SCALES),
        config=module.ALNSConfig(max_iterations=3, relay_min_destroy_fraction=.23,
            relay_max_destroy_fraction=.23), alns_module=module)
    stats = result["metadata"]
    assert result["routes"] == routes and result["evaluation"].valid
    assert stats["rollback_candidates"] == stats["rejected_acceptances"] == 3
    assert stats["alns_summary"]["accepted_solutions"] == 0
    assert all(item["accepted"] == 0 and item["total_reward"] == 0.
               for item in stats["alns_summary"]["operator_statistics"].values())
    assert not weighted.active and stats["patches_restored"]


def test_expired_baseline_is_explicitly_an_unchanged_initial_checkpoint():
    problem, routes = fixture()
    checked = evaluate_solution(problem, routes, _force_full=True)
    module = fake_module(solve=lambda *a, **k: pytest.fail("expired solver must not run"))
    result = polish.polish_pinned_macro(problem, routes, deadline=perf_counter() - 1.,
        weights=WEIGHTS, scales=SCALES, seed=1, pinned_task_ids=PINS, required_cross_task_ids=CROSS,
        comparison_key=polish.mobile.weighted_key(checked.score, WEIGHTS, SCALES), alns_module=module)
    stats = result["metadata"]
    assert result["routes"] == routes and stats["returned_initial_checkpoint"]
    assert stats["returned_checkpoint_kind"] == "initial_macro_no_search_change"
    assert not stats["initial_validation_within_deadline"]
    assert not stats["returned_checkpoint_checked_within_deadline"]
    assert stats["best_checked_at_perf_counter"] is None and stats["patches_restored"]


def test_real_module_with_foreign_repair_globals_is_rejected_before_patching(real_weighted_runtime, monkeypatch):
    module = real_weighted_runtime
    problem, routes = fixture()
    checked = evaluate_solution(problem, routes, _force_full=True)
    monkeypatch.setattr(module, "_repair", lambda *a, **k: routes)
    functions = {name: getattr(module, name) for name in polish.PATCH_NAMES}
    with pytest.raises(ValueError, match="same ALNS module"):
        polish.polish_pinned_macro(problem, routes, deadline=perf_counter() + .08,
            weights=WEIGHTS, scales=SCALES, seed=1, pinned_task_ids=PINS, required_cross_task_ids=CROSS,
            comparison_key=polish.mobile.weighted_key(checked.score, WEIGHTS, SCALES), alns_module=module)
    assert all(getattr(module, name) is function for name, function in functions.items())
    assert not weighted.active and not polish._active
