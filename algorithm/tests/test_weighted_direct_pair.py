"""Narrow objective-adapter checks; no official-instance optimization runs."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

from algorithm.experiments import weighted_direct_objective as weighted
from uav_dispatch.model import Score


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "algorithm/src/uav_dispatch"


def test_order_restores_after_success_and_exception():
    methods = {name: getattr(Score, name) for name in ("__lt__", "__le__", "__gt__", "__ge__")}
    ordinary_better, weighted_better = Score(0, 100, 100), Score(1, 0, 0)
    assert ordinary_better < weighted_better
    with weighted.objective(Score, (200, 100, 1000)):
        assert weighted_better < ordinary_better
        assert weighted_better <= ordinary_better
        assert ordinary_better > weighted_better
        assert ordinary_better >= weighted_better
        with pytest.raises(RuntimeError, match="Nested"):
            with weighted.objective(Score, (200, 100, 1000)):
                pass
    assert ordinary_better < weighted_better
    with pytest.raises(RuntimeError, match="deliberate"):
        with weighted.objective(Score, (200, 100, 1000)):
            raise RuntimeError("deliberate")
    assert not weighted.active and weighted.coefficients is None
    assert all(getattr(Score, name) is method for name, method in methods.items())


@pytest.mark.parametrize("scales,weights", [
    ((0, 1, 1), (.5, .25, .25)), ((-1, 1, 1), (.5, .25, .25)),
    ((math.inf, 1, 1), (.5, .25, .25)), ((math.nan, 1, 1), (.5, .25, .25)),
    ((1, 1, 1), (0, .5, .5)), ((1, 1, 1), (-.5, .75, .75)),
    ((1, 1, 1), (math.inf, .25, .25)), ((1, 1, 1), (math.nan, .25, .25)),
    ((1, 1), (.5, .25, .25)), ((1, 1, 1), (.5, .25)),
    ((1, 1, 1), (.5, .25, .30)),
])
def test_invalid_normalization_does_not_mutate_order(scales, weights):
    method = Score.__lt__
    with pytest.raises(ValueError):
        with weighted.objective(Score, scales, weights):
            pass
    assert Score.__lt__ is method
    assert not weighted.active and weighted.coefficients is None


def test_scalar_annealing_probability_boundary():
    class FixedRandom:
        def __init__(self, value):
            self.value, self.calls = value, 0

        def random(self):
            self.calls += 1
            return self.value

    with weighted.objective(Score, (200, 100, 1000)):
        current, candidate = Score(0, 0, 0), Score(1, 0, 0)
        probability = math.exp(-.0025 / .01)
        below, equal = FixedRandom(probability - 1e-10), FixedRandom(probability)
        assert weighted.accept(candidate, current, .01, below)
        assert not weighted.accept(candidate, current, .01, equal)
        improvement = FixedRandom(.999)
        # A lexicographically worse solution can be a scalar improvement.
        assert weighted.accept(Score(1, 0, 0), Score(0, 100, 100), .01, improvement)
        assert improvement.calls == 0
        assert weighted.accept(current, current, .01, improvement)
        assert improvement.calls == 0


def test_raw_insertion_key_changes_ranking_without_rewriting_records(monkeypatch):
    monkeypatch.setitem(sys.modules, "weighted_direct_objective", weighted)
    source = """def rank(ranked):
    ranked.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
    return ranked
"""
    transformed, counts = weighted.transform(source)
    assert counts["raw_insertion_keys"] == 1
    namespace = {}
    exec(compile(transformed, "<minimal-insertion-ranking>", "exec"), namespace)
    records = [(0, 100., 100., 1, 3, 9., 10.), (1, 0., 0., 0, 1, 2., 3.)]
    assert namespace["rank"](records.copy()) == records
    with weighted.objective(Score, (200, 100, 1000)):
        assert namespace["rank"](records.copy()) == list(reversed(records))
    assert records[0][5:] == (9., 10.)


def test_current_source_transform_counts_and_regret_tie_break(monkeypatch):
    for name, expected in weighted.EXPECTED_COUNTS.items():
        transformed, actual = weighted.transform((SOURCE / name).read_text())
        assert actual == expected
        compile(transformed, name, "exec")
    monkeypatch.setitem(sys.modules, "weighted_direct_objective", weighted)
    source = """def regret_key(best, alternative):
    regret = (float(alternative.late_count - best.late_count),
              alternative.total_lateness_min - best.total_lateness_min,
              alternative.distance_km - best.distance_km)
    return (regret[0], regret[1], regret[2], -10., -2)
"""
    transformed, counts = weighted.transform(source)
    assert counts["regret_keys"] == counts["regret_output_keys"] == 1
    namespace = {}
    exec(compile(transformed, "<regret-ranking>", "exec"), namespace)
    best, alternative = Score(0, 0, 0), Score(1, 2, 3)
    assert namespace["regret_key"](best, alternative) == (1., 2, 3, -10., -2)
    with weighted.objective(Score, (200, 100, 1000)):
        assert namespace["regret_key"](best, alternative) == (.00825, 1., 2, 3, -10., -2)


def test_inactive_transform_matches_original_few_iteration_search(tmp_path):
    script = """
import json
from uav_dispatch.model import Point, Problem, Task
from uav_dispatch.alns import ALNSConfig, construct_regret_initial, solve_alns
p = Problem(tuple(Task(i, Point(i, i % 2), Point(i + .3, .2), 2 + i)
                  for i in range(1, 7)), drone_count=2, max_tasks_per_drone=3)
initial = construct_regret_initial(p, candidate_limit=12)
result = solve_alns(p, config=ALNSConfig(max_iterations=6, time_limit_seconds=None,
                   seed=81, relay_enabled=False, candidate_limit=12,
                   enable_terminal_polish=False), initial_routes=initial.routes)
print(json.dumps(dict(routes=result.routes, iterations=result.iterations,
                     trajectory=[(x[0], *x[2:]) for x in result.metadata['best_trajectory']],
                     operators={k: dict(v) for k, v in result.metadata['operator_statistics'].items()})))
"""
    results = []
    for name in ("ordinary", "adapted_inactive"):
        package = tmp_path / name / "uav_dispatch"
        package.mkdir(parents=True)
        for path in SOURCE.glob("*.py"):
            text = path.read_text()
            if name == "adapted_inactive" and path.name in weighted.EXPECTED_COUNTS:
                text, counts = weighted.transform(text)
                assert counts == weighted.EXPECTED_COUNTS[path.name]
            (package / path.name).write_text(text)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(package.parent), str(ROOT / "algorithm/experiments"))),
               "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env,
                                check=True, capture_output=True, text=True, timeout=15)
        results.append(json.loads(result.stdout))
    assert results[0] == results[1]
