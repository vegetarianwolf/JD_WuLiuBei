"""Staged weighted trajectory regression; no official-instance solving."""
from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from algorithm.experiments import weighted_direct_objective as weighted
from algorithm.experiments import weighted_relay_objective as relay_weighted
from uav_dispatch.alns import ALNSConfig
from uav_dispatch.model import Score
from uav_dispatch.search import SolverResult


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "algorithm/src/uav_dispatch"


@pytest.mark.parametrize("module_name", ["alns.py", "search.py"])
def test_current_source_round_trip_and_exact_counts(module_name):
    original = (SOURCE / module_name).read_text()
    adapted, counts = relay_weighted.transform(original, module_name)
    assert counts == relay_weighted.EXPECTED_COUNTS[module_name]
    compile(adapted, module_name, "exec")
    restored, reversed_counts = relay_weighted.restore(adapted, module_name)
    assert reversed_counts == counts
    assert ast.dump(ast.parse(restored)) == ast.dump(ast.parse(original))
    # The additional AST delta is exactly one comparison, not a solver edit.
    direct_adapted, _ = weighted.transform(original)
    inverse = relay_weighted._StagedTrajectory(reverse=True)
    stripped = inverse.visit(ast.parse(adapted))
    assert inverse.count == int(module_name == "alns.py")
    assert ast.dump(stripped) == ast.dump(ast.parse(direct_adapted))


def _staged_fixture():
    adapted, _ = relay_weighted.transform((SOURCE / "alns.py").read_text(), "alns.py")
    function = next(node for node in ast.parse(adapted).body
                    if isinstance(node, ast.FunctionDef) and node.name == "solve_relay_staged")
    tree = ast.Module(body=[ast.parse("from __future__ import annotations").body[0], function],
                      type_ignores=[])
    warm_routes, relay_routes = ((1, -1), ()), ((2, -2), (3, -3))
    warm_score, relay_score = Score(0, 100., 100.), Score(1, 0., 0.)
    evaluations = {warm_routes: SimpleNamespace(valid=True, score=warm_score),
                   relay_routes: SimpleNamespace(valid=True, score=relay_score)}
    calls = []

    def phase(problem, *, config, initial_routes):
        calls.append((config.relay_enabled, initial_routes))
        routes = relay_routes if config.relay_enabled else warm_routes
        physical = lambda score: (score.late_count, score.total_lateness_min, score.distance_km)
        trajectory = ((0, .1, *physical(warm_score)),)
        if config.relay_enabled:
            trajectory += ((1, .2, *physical(relay_score)),)
        return SolverResult(routes=routes, evaluation=evaluations[routes],
                            runtime_seconds=2. if config.relay_enabled else 8.,
                            iterations=2 if config.relay_enabled else 8,
                            metadata={"initial_score": physical(warm_score),
                                      "best_trajectory": trajectory,
                                      "best_update_count": int(config.relay_enabled)})

    namespace = dict(ALNSConfig=ALNSConfig, replace=replace, solve_alns=phase,
                     evaluate_solution=lambda problem, routes, **kwargs: evaluations[routes],
                     SolverResult=SolverResult, MappingProxyType=MappingProxyType,
                     _weighted_adapter=weighted,
                     GlobalRelayEvaluator=lambda problem: SimpleNamespace(evaluate=lambda routes: None),
                     build_plan_index=lambda problem, routes: None,
                     relay_statistics=lambda *args: {})
    exec(compile(ast.fix_missing_locations(tree), "<staged-weighted-fixture>", "exec"), namespace)
    return namespace["solve_relay_staged"], warm_routes, relay_routes, warm_score, relay_score, calls


def test_weighted_improvement_that_is_lexically_worse_reaches_final_checkpoint():
    staged, warm, relay, warm_score, relay_score, calls = _staged_fixture()
    assert warm_score < relay_score
    methods = {name: getattr(Score, name) for name in ("__lt__", "__le__", "__gt__", "__ge__")}
    with weighted.objective(Score, (200, 100, 1000)):
        assert relay_score < warm_score
        result = staged(SimpleNamespace(has_relays=True), initial_routes=warm,
                        config=ALNSConfig(max_iterations=10, relay_direct_warmup_fraction=.8))
        assert result.routes == relay
        assert result.evaluation.score == relay_score
        assert result.metadata["best_trajectory"] == ((0, .1, 0, 100., 100.),
                                                     (9, 8.2, 1, 0., 0.))
        assert result.metadata["best_update_count"] == 1
        assert result.metadata["time_to_best_seconds"] == 8.2
        assert result.metadata["relay_retained_warmup_incumbent"] is False
    assert calls == [(False, warm), (True, warm)]
    assert all(getattr(Score, name) is method for name, method in methods.items())
    assert not weighted.active and weighted.coefficients is None
    assert warm_score < relay_score


def test_inactive_adapter_retains_ordinary_staged_order():
    staged, warm, _, warm_score, _, _ = _staged_fixture()
    result = staged(SimpleNamespace(has_relays=True), initial_routes=warm,
                    config=ALNSConfig(max_iterations=10, relay_direct_warmup_fraction=.8))
    assert result.routes == warm
    assert result.evaluation.score == warm_score
    assert result.metadata["best_trajectory"] == ((0, .1, 0, 100., 100.),)
    assert result.metadata["relay_retained_warmup_incumbent"] is True


def test_staged_failure_restores_score_order():
    staged, warm, _, _, _, _ = _staged_fixture()
    method = Score.__lt__
    staged.__globals__["evaluate_solution"] = lambda *args, **kwargs: SimpleNamespace(valid=False)
    with pytest.raises(RuntimeError, match="非法路线"):
        with weighted.objective(Score, (200, 100, 1000)):
            staged(SimpleNamespace(has_relays=True), initial_routes=warm,
                   config=ALNSConfig(max_iterations=10, relay_direct_warmup_fraction=.8))
    assert Score.__lt__ is method
    assert not weighted.active and weighted.coefficients is None


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_operator", "wrong_function"])
def test_transform_rejects_unexpected_staged_count_or_pattern(mutation):
    source = (SOURCE / "alns.py").read_text()
    old = relay_weighted.ORIGINAL_COMPARISON
    replacements = {
        "missing": "False",
        "duplicate": f"({old}) or ({old})",
        "wrong_operator": old.replace(" < ", " <= "),
    }
    if mutation == "wrong_function":
        source = source.replace("def solve_relay_staged(", "def other_staged(")
    else:
        source = source.replace(old, replacements[mutation])
    with pytest.raises(ValueError, match="counts"):
        relay_weighted.transform(source, "alns.py")


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "wrong_operator", "wrong_argument"])
def test_reverse_rejects_unexpected_staged_edit(mutation):
    adapted, _ = relay_weighted.transform((SOURCE / "alns.py").read_text(), "alns.py")
    old = relay_weighted.ADAPTED_COMPARISON
    replacements = {"missing": relay_weighted.ORIGINAL_COMPARISON,
                    "duplicate": f"({old}) or ({old})",
                    "wrong_operator": old.replace(" < ", " <= "),
                    "wrong_argument": old.replace("*adjusted[2:]", "*adjusted[1:]")}
    changed = adapted.replace(old, replacements[mutation])
    assert changed != adapted
    with pytest.raises(ValueError, match="counts"):
        relay_weighted.restore(changed, "alns.py")


@pytest.mark.parametrize("operation", [relay_weighted.transform, relay_weighted.restore])
def test_unsupported_modules_are_rejected(operation):
    with pytest.raises(ValueError, match="Unsupported"):
        operation("", "relay.py")
