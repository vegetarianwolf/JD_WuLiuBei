"""Narrow protocol, independent-audit, and synthetic lazy-repair checks.

No test optimizes the official instance. Historical routes are read only as
arithmetic fixtures in temporary files; synthetic searches use twelve tasks.
"""
from __future__ import annotations

import copy
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import MappingProxyType
from zipfile import ZipFile

import pytest

from algorithm.experiments import audit_weighted_method_pair as audit
from algorithm.experiments import weighted_direct_objective as weighted


ROOT = Path(__file__).resolve().parents[2]
PARENT = ROOT / "algorithm/results/weighted_direct_single_seed_20261014"
SOURCE = ROOT / "algorithm/src/uav_dispatch"


@pytest.fixture
def contract():
    parent = audit.read(PARENT / "manifest.json")
    base = {**parent["config"], "seed": audit.SEED}
    manifest = {k: copy.deepcopy(parent[k]) for k in (
        "formula", "weights", "scales", "input_path", "input_sha256", "problem_path", "problem_sha256",
        "initial_score", "initial_routes", "initial_routes_sha256")}
    manifest.update(schema_version=2, seed=audit.SEED, order=audit.ORDER.copy(),
                    baseline_variant=audit.ORDER[0], budget_seconds=240, finalization_reserve_seconds=1,
                    variants={audit.ORDER[0]: {"objective": "weighted", "config": base},
                              audit.ORDER[1]: {"objective": "weighted", "config": {**base, **audit.OVERRIDES}}})
    return manifest, parent


def test_current_frozen_contract_accepts_only_two_new_overrides(contract):
    manifest, parent = contract
    audit.audit_protocol(manifest, parent)
    assert manifest["scales"] == [200, 4464.183540734935, 775.7731679286865]
    assert manifest["seed"] != parent["seed"]


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(schema_version=1),
    lambda m: m.update(seed=2026101451),
    lambda m: m.update(seed=float(audit.SEED)),
    lambda m: m.update(order=list(reversed(audit.ORDER))),
    lambda m: m.update(budget_seconds=241),
    lambda m: m.update(finalization_reserve_seconds=0),
    lambda m: m.update(weights=[.6, .2, .2]),
    lambda m: m.update(scales=[200, 4000, 775.7731679286865]),
    lambda m: m["variants"][audit.ORDER[0]].update(objective="lexicographic"),
    lambda m: m["variants"][audit.ORDER[0]]["config"].update(enable_lazy_repair=True),
    lambda m: m["variants"][audit.ORDER[1]]["config"].update(candidate_limit=24),
    lambda m: m["variants"][audit.ORDER[1]]["config"].update(enable_terminal_task_exchange=False),
    lambda m: m["initial_routes"][0].reverse(),
])
def test_protocol_rejects_changed_scope_weights_seed_budget_or_configuration(contract, mutate):
    manifest, parent = contract
    mutate(manifest)
    with pytest.raises(ValueError):
        audit.audit_protocol(manifest, parent)


def test_reverse_baseline_weighted_source_matches_parent_production_ast():
    parent = audit.read(PARENT / "manifest.json")
    weighted_zip = PARENT / parent["variants"]["current_weighted"]["archive"]
    lex_zip = PARENT / parent["variants"]["current_lex"]["archive"]
    with ZipFile(lex_zip) as original:
        for name, content in audit.archive_modules(weighted_zip).items():
            tree, counts = audit.restored_ast(content, name)
            assert audit.ast.dump(tree) == audit.ast.dump(audit.ast.parse(original.read("uav_dispatch/" + name)))
            if name in weighted.EXPECTED_COUNTS:
                assert counts == weighted.EXPECTED_COUNTS[name]


def test_adapter_reversal_rejects_unapproved_weighted_key_arguments():
    text = "def f(option):\n    return (*_weighted_adapter.key(option.delta.late_count, 0., option.delta.distance_km),)\n"
    with pytest.raises(ValueError, match="unapproved key"):
        audit.restored_ast(text, "search.py")


@pytest.fixture
def frozen_source_fixture(contract, tmp_path, monkeypatch):
    manifest, parent = contract
    root = tmp_path / "root"
    output = root / "batch"
    production = root / "algorithm/src/uav_dispatch"
    experiments = root / "algorithm/experiments"
    for path in (output, production, experiments, root / "parent"):
        path.mkdir(parents=True)
    baseline_zip = PARENT / parent["variants"]["current_weighted"]["archive"]
    parent_zip = root / "parent" / parent["variants"]["current_weighted"]["archive"]
    parent_zip.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(baseline_zip, parent_zip)
    (root / "parent/manifest.json").write_text(json.dumps(parent))
    shutil.copyfile(baseline_zip, output / "baseline.zip")
    for path in SOURCE.glob("*.py"):
        shutil.copyfile(path, production / path.name)
    manifest.update(parent_manifest_path="parent/manifest.json", parent_weighted_source_sha256=audit.sha(parent_zip),
                    production_files_sha256={p.name: audit.sha(p) for p in production.glob("*.py")},
                    adapter_changes=copy.deepcopy(weighted.EXPECTED_COUNTS), execution_sources={})
    candidate_path = output / "candidate.zip"
    with ZipFile(candidate_path, "w") as package:
        for path in sorted(production.glob("*.py")):
            text = path.read_text()
            if path.name in weighted.EXPECTED_COUNTS:
                text, _ = weighted.transform(text)
            package.writestr("uav_dispatch/" + path.name, text)
    manifest["variants"][audit.ORDER[0]].update(archive="baseline.zip", sha256=audit.sha(output / "baseline.zip"))
    manifest["variants"][audit.ORDER[1]].update(archive="candidate.zip", sha256=audit.sha(candidate_path))
    for name in ("run_weighted_method_pair.py", "run_weighted_direct_pair.py", "weighted_direct_objective.py"):
        shutil.copyfile(ROOT / "algorithm/experiments" / name, experiments / name)
        shutil.copyfile(experiments / name, output / name)
        manifest["execution_sources"][name] = dict(path=name, sha256=audit.sha(output / name))
    original = audit.archive_modules(PARENT / parent["variants"]["current_lex"]["archive"])
    def fake_git_show(argv, **kwargs):
        assert argv[:2] == ["git", "show"]
        return subprocess.CompletedProcess(argv, 0, stdout=original[Path(argv[2]).name])
    monkeypatch.setattr(audit, "ROOT", root)
    monkeypatch.setattr(audit.subprocess, "run", fake_git_show)
    return output, manifest, parent, production


def test_source_audit_binds_exact_parent_zip_current_production_and_disabled_defaults(frozen_source_fixture):
    output, manifest, parent, _ = frozen_source_fixture
    result = audit.source_audit(output, manifest, parent)
    assert result["exact_parent_weighted_zip"] and result["research_defaults_disabled"]
    assert set(result["changed_modules"]) == audit.CHANGED_MODULES


@pytest.mark.parametrize("mutation", ["baseline_hash", "undeclared_module", "changed_adapter", "public_default"])
def test_source_audit_rejects_fingerprint_or_unapproved_code_changes(frozen_source_fixture, mutation):
    output, manifest, parent, production = frozen_source_fixture
    if mutation == "baseline_hash":
        manifest["parent_weighted_source_sha256"] = "0" * 64
    elif mutation == "changed_adapter":
        with (output / "weighted_direct_objective.py").open("a") as handle:
            handle.write("\n# changed adapter\n")
        manifest["execution_sources"]["weighted_direct_objective.py"]["sha256"] = audit.sha(output / "weighted_direct_objective.py")
    else:
        changed_name = "validation.py" if mutation == "undeclared_module" else "alns.py"
        path = production / changed_name
        text = path.read_text()
        if mutation == "undeclared_module":
            text += "\nUNDECLARED_EXPERIMENT = True\n"
        else:
            text = text.replace("enable_terminal_task_exchange: bool = False", "enable_terminal_task_exchange: bool = True")
        path.write_text(text)
        manifest["production_files_sha256"][changed_name] = audit.sha(path)
        archive = output / "candidate.zip"
        members = audit.archive_modules(archive)
        if changed_name in weighted.EXPECTED_COUNTS:
            text, _ = weighted.transform(text)
        members[changed_name] = text.encode()
        with ZipFile(archive, "w") as package:
            for name, content in members.items():
                package.writestr("uav_dispatch/" + name, content)
        manifest["variants"][audit.ORDER[1]]["sha256"] = audit.sha(archive)
    with pytest.raises(ValueError):
        audit.source_audit(output, manifest, parent)


@pytest.fixture
def arithmetic_fixture(contract, tmp_path):
    # Deliberately relabel a saved solution as a synthetic audit fixture. It
    # proves arithmetic validation, never provenance of a new solver run.
    manifest, _ = contract
    old = next(r for r in audit.read(PARENT / "runs.json") if r["variant"] == "current_weighted")
    payload = audit.read(PARENT / old["solution_path"])
    row = copy.deepcopy(old)
    row.update(variant=audit.ORDER[0], seed=audit.SEED,
               terminal_polish=copy.deepcopy(payload["metadata"]["terminal_polish"]),
               operator_statistics=copy.deepcopy(payload["metadata"]["operator_statistics"]),
               performance_counters=copy.deepcopy(payload["metadata"]["profiling"]))
    row["config"]["seed"] = audit.SEED
    payload["metadata"]["seed"] = audit.SEED
    path = tmp_path / "arithmetic_fixture.json"
    path.write_text(json.dumps(payload))
    row.update(solution_path=path.name, solution_sha256=audit.sha(path))
    manifest["variants"][audit.ORDER[0]]["sha256"] = old["source_sha256"]
    tasks = audit.common.load_official_csv(ROOT / manifest["input_path"])
    initial, _, _ = audit.walk(tasks, manifest["initial_routes"])
    return tmp_path, row, manifest, tasks, initial, path, payload


def test_independent_run_arithmetic_checks_both_weighted_labels(arithmetic_fixture):
    output, row, manifest, tasks, initial, _, _ = arithmetic_fixture
    checked = audit.audit_run(output, row, manifest, tasks, initial)
    assert checked["passed"] and checked["capacity_precedence_and_K25"]
    assert checked["independent_score"]["late_count"] == 67


@pytest.mark.parametrize("mutation", ["score", "coordinate", "clock", "duplicate", "budget", "objective", "hash", "profiling"])
def test_independent_audit_rejects_dishonest_solution_and_telemetry(arithmetic_fixture, mutation):
    output, row, manifest, tasks, initial, path, payload = arithmetic_fixture
    if mutation == "score":
        payload["score"]["late_count"] -= 1
    elif mutation == "coordinate":
        payload["routes"][0]["visits"][0]["location_km"][0] += 1
    elif mutation == "clock":
        payload["delivery_times_min"]["1"] += 1
    elif mutation == "duplicate":
        payload["routes"][0]["encoded_visits"][1] = payload["routes"][0]["encoded_visits"][0]
    elif mutation == "budget":
        row["end_to_end_seconds"] = 241
    elif mutation == "objective":
        row["objective"] = "lexicographic"
    elif mutation == "hash":
        row["source_sha256"] = "0" * 64
    elif mutation == "profiling":
        row["performance_counters"]["repair_rank_evaluation_count"] += 1
    path.write_text(json.dumps(payload))
    row["solution_sha256"] = audit.sha(path)
    with pytest.raises(ValueError):
        audit.audit_run(output, row, manifest, tasks, initial)


def test_weighted_lazy_repair_matches_fixed_iteration_search_on_twelve_tasks(tmp_path):
    package = tmp_path / "uav_dispatch"
    package.mkdir()
    for path in SOURCE.glob("*.py"):
        text = path.read_text()
        if path.name in weighted.EXPECTED_COUNTS:
            text, counts = weighted.transform(text)
            assert counts == weighted.EXPECTED_COUNTS[path.name]
        (package / path.name).write_text(text)
    script = """
import dataclasses, json
import weighted_direct_objective as weighted
from uav_dispatch.model import Point, Problem, Score, Task
from uav_dispatch.alns import ALNSConfig, construct_regret_initial, solve_alns
p=Problem(tuple(Task(i,Point((i*7)%11,(i*3)%5),Point((i*2)%9+.3,(i*5)%7+.2),4+i)
                for i in range(1,13)),drone_count=3,max_tasks_per_drone=4)
initial=construct_regret_initial(p,candidate_limit=12)
scales=(12,max(1.,initial.evaluation.score.total_lateness_min),max(1.,initial.evaluation.score.distance_km))
results=[]
with weighted.objective(Score,scales):
    for lazy in (False,True):
        r=solve_alns(p,config=ALNSConfig(max_iterations=30,time_limit_seconds=None,seed=171,
                     candidate_limit=12,repair_rank_candidate_limit=8,repair_exact_candidate_limit=12,
                     relay_enabled=False,enable_terminal_polish=False,enable_lazy_repair=lazy),
                     initial_routes=initial.routes)
        results.append(dict(routes=r.routes,score=dataclasses.asdict(r.evaluation.score),
                     iterations=r.iterations,accepted=r.metadata['accepted_solutions'],
                     trajectory=[(x[0],*x[2:]) for x in r.metadata['best_trajectory']],
                     statistics={k:dict(v) for k,v in r.metadata['operator_statistics'].items()},
                     weights=dict(r.metadata['operator_weights'])))
assert results[0]==results[1],results
assert sum(results[0]['statistics']['repair:'+k]['uses'] for k in ('deadline','slack'))>0
print(json.dumps(dict(equal=True,task_count=12,iterations=30)))
"""
    env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(tmp_path), str(ROOT / "algorithm/experiments"))),
           "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=env,
                            check=True, capture_output=True, text=True, timeout=15)
    assert json.loads(result.stdout) == dict(equal=True, task_count=12, iterations=30)


def test_actual_worker_serializes_nested_mappingproxy_telemetry(contract, tmp_path, monkeypatch):
    """Run the real worker and JSON writer with a mocked four-task solver.

    Before the recording fix, its final response write raised TypeError even
    though result_payload had already normalized the same nested metadata.
    """
    from uav_dispatch import alns, io
    from uav_dispatch.model import Point, Problem, Task
    from uav_dispatch.search import SolverResult
    from uav_dispatch.validation import evaluate_solution

    monkeypatch.syspath_prepend(str(ROOT / "algorithm/experiments"))
    monkeypatch.setitem(sys.modules, "weighted_direct_objective", weighted)
    runner = importlib.import_module("run_weighted_method_pair")
    tasks = tuple(Task(i, Point(i, 0), Point(i + .2, .1), 10) for i in range(1, 5))
    routes = tuple((i, -i) for i in range(1, 5)) + ((),) * 4
    problem = Problem(tasks, drone_count=8, max_tasks_per_drone=25)
    evaluation = evaluate_solution(problem, routes, _force_full=True)
    initial = SolverResult(routes, evaluation)
    physical = (evaluation.score.late_count, evaluation.score.total_lateness_min, evaluation.score.distance_km)
    telemetry = MappingProxyType({
        "best_trajectory": ((0, 0., *physical),),
        "terminal_polish": MappingProxyType({"active": True, "statistics": MappingProxyType({"nested": (1, 2)})}),
        "operator_statistics": MappingProxyType({"repair:deadline": MappingProxyType({"uses": 2})}),
        "profiling": MappingProxyType({"repair_rank_evaluation_count": 3}),
    })
    result = SolverResult(routes, evaluation, runtime_seconds=.001, iterations=3, metadata=telemetry)
    monkeypatch.setattr(io, "load_tasks_csv", lambda path: tasks)
    monkeypatch.setattr(alns, "construct_regret_initial", lambda problem, **kwargs: initial)
    calls = []
    def fake_solve(problem, *, config, initial_routes):
        calls.append(config)
        assert tuple(tuple(r) for r in initial_routes) == routes
        return result
    monkeypatch.setattr(alns, "solve_alns", fake_solve)
    manifest, _ = contract
    manifest["initial_routes_sha256"] = runner.digest(routes)
    manifest["variants"][audit.ORDER[0]].update(label="Serialization fixture", sha256="0" * 64)
    output = tmp_path / "recording_fixture"
    runner.write(output / "manifest.json", manifest)
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"
    runner.write(request, dict(output=str(output), variant=audit.ORDER[0], response=str(response)))
    runner.worker(request)
    row = runner.read(response)
    payload = runner.read(output / row["solution_path"])
    assert len(calls) == 1 and payload["problem"]["task_count"] == 4
    assert row["terminal_polish"]["statistics"]["nested"] == [1, 2]
    assert row["operator_statistics"] == {"repair:deadline": {"uses": 2}}
    assert row["performance_counters"] == {"repair_rank_evaluation_count": 3}
    for row_key, payload_key in (("best_trajectory", "best_trajectory"), ("terminal_polish", "terminal_polish"),
                                 ("operator_statistics", "operator_statistics"), ("performance_counters", "profiling")):
        assert row[row_key] == payload["metadata"][payload_key]
