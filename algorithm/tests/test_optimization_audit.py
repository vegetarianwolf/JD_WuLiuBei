"""Tampering and failed-run coverage for the independent optimization audit."""

import copy
import json
import zipfile
from pathlib import Path

import pytest

import algorithm.experiments.audit_optimization_results as audit
from uav_dispatch.alns import construct_edd_adjacent
from uav_dispatch.cli import result_payload
from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem
from uav_dispatch.search import SolverResult


def _write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def sources():
    commit = audit._git("rev-parse", "HEAD").decode().strip()
    return commit, {
        name.removeprefix("algorithm/src/"): audit._git("show", f"{commit}:{name}")
        for name in audit._git("ls-tree", "-r", "--name-only", commit, "--", "algorithm/src/uav_dispatch").decode().splitlines()
        if name.endswith(".py")
    }


@pytest.fixture
def experiment(tmp_path, sources):
    commit, files = sources
    archive_path = tmp_path / "baseline.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    fingerprint = audit._sha256(archive_path)
    execution_sources = {}
    for name in ("run_optimization_experiments", "run_scenario_comparison"):
        path = tmp_path / f"{name}.py"
        path.write_bytes(Path(audit.__file__).with_name(f"{name}.py").read_bytes())
        execution_sources[name] = {"path": path.name, "sha256": audit._sha256(path)}
    manifest = {
        "baseline_commit": commit,
        "input_path": audit.DEFAULT_INPUT.relative_to(audit.ROOT).as_posix(),
        "input_sha256": audit._sha256(audit.DEFAULT_INPUT),
        "source_snapshots": {
            variant: {"path": "baseline.zip", "sha256": fingerprint, "config": audit.VARIANT_CONFIG[variant]}
            for variant in ("baseline", "fast")
        },
        "execution_sources": execution_sources,
        "runner_sha256": execution_sources["run_optimization_experiments"]["sha256"],
    }
    problem = Problem(tuple(load_tasks_csv(audit.DEFAULT_INPUT)[:25]))
    initial = construct_edd_adjacent(problem)
    score = initial.evaluation.score
    trajectory = [[0, 0.0, score.late_count, score.total_lateness_min, score.distance_km]]
    result = SolverResult(initial.routes, initial.evaluation, 0.05, 0, {
        "seed": 2026101301, "best_trajectory": trajectory, "time_limit_seconds": 3.9,
        "relay_enabled": False, "enable_home_seed": False,
    })
    payload = result_payload(problem, result, source=manifest["input_path"])
    _write(tmp_path / "solution.json", payload)
    row = {
        "stage": "screen", "variant": "baseline", "case_id": "single25", "scenario": "pure_direct",
        "seed": 2026101301, "task_count": 25, "drone_count": 1, "budget_seconds": 5,
        "end_to_end_seconds": 0.3, "construction_seconds": 0.1, "search_seconds": 0.1,
        "validation_seconds": 0.01, "serialization_seconds": 0.01, "budget_compliant": True,
        "valid": True, **payload["score"], "on_time_count": 25 - score.late_count,
        "on_time_rate": payload["on_time_rate"], "iterations": 0,
        "best_trajectory": [[0, 0.1, *trajectory[0][2:]]], "solution_file": "solution.json",
        "solution_sha256": audit._sha256(tmp_path / "solution.json"),
        "initial_routes_sha256": audit._value_hash(initial.routes), "source_sha256": fingerprint,
        "config": {"max_iterations": 10_000_000, "time_limit_seconds": 3.9,
                   "seed": 2026101301, "candidate_limit": 48, "relay_enabled": False,
                   "relay_direct_warmup_fraction": 0.8, "enable_home_seed": False,
                   "enable_home_bias": False},
    }
    _write(tmp_path / "manifest.json", manifest)
    _write(tmp_path / "runs.json", [row])
    return tmp_path, manifest, row, payload


def test_audit_recomputes_full_solution_and_writes_bound_validation(experiment, monkeypatch):
    directory, _, _, _ = experiment
    calls = []
    original = audit.evaluate_solution

    def checked(*args, **kwargs):
        calls.append(kwargs.get("_force_full"))
        return original(*args, **kwargs)

    monkeypatch.setattr(audit, "evaluate_solution", checked)
    report = audit.audit_results(directory)
    assert calls == [True]
    assert report["validated_rows"] == report["validated_unique_solutions"] == 1
    assert report["has_valid_results"] and report["all_runs_successful"]
    assert report["runs_sha256"] == audit._sha256(directory / "runs.json")
    assert json.loads((directory / "validation.json").read_text()) == report


@pytest.mark.parametrize(("field", "value", "message"), [
    ("late_count", 0, "row late_count"),
    ("on_time_count", 25, "on_time_count"),
    ("on_time_rate", 1.0, "on_time_rate"),
    ("source_sha256", "0" * 64, "source_sha256"),
    ("solution_sha256", "0" * 64, "solution fingerprint"),
    ("task_count", 24, "task_count"),
    ("scenario", "direct_relay", "pure_direct"),
    ("budget_compliant", False, "budget_compliant"),
    ("budget_seconds", 50, "prespecified"),
    ("seed", 2026101303, "prespecified"),
    ("valid", False, "valid flag"),
    ("search_seconds", 2, "phase sum"),
    ("end_to_end_seconds", float("nan"), "Nonfinite"),
    ("solution_file", "../outside.json", "Missing or escaping"),
])
def test_audit_rejects_tampered_rows(experiment, field, value, message):
    directory, _, row, _ = experiment
    audit.audit_results(directory)
    row[field] = value
    _write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match=message):
        audit.audit_results(directory)
    assert not (directory / "validation.json").exists()


def test_audit_accepts_honestly_recorded_timeout_without_claiming_success(experiment):
    directory, _, row, _ = experiment
    row.update(end_to_end_seconds=5.1, budget_compliant=False)
    _write(directory / "runs.json", [row])
    report = audit.audit_results(directory)
    assert report["evidence_verified"]
    assert not report["all_runs_successful"] and not report["has_valid_results"]
    assert report["over_budget_rows"] == [0]


def test_empty_results_do_not_claim_experiment_success(experiment):
    directory, _, _, _ = experiment
    _write(directory / "runs.json", [])
    report = audit.audit_results(directory)
    assert report["validated_rows"] == report["valid_on_budget_runs"] == 0
    assert not report["has_valid_results"] and not report["all_runs_successful"]


def test_same_saved_solution_is_rescored_once_but_every_row_checked(experiment, monkeypatch):
    directory, _, row, _ = experiment
    second = {**copy.deepcopy(row), "variant": "fast"}
    second["config"].update(audit.VARIANT_CONFIG["fast"])
    _write(directory / "runs.json", [row, second])
    original = audit.evaluate_solution
    calls = []

    def checked(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(audit, "evaluate_solution", checked)
    assert audit.audit_results(directory)["validated_rows"] == 2
    assert len(calls) == 1
    second["on_time_rate"] = 1
    _write(directory / "runs.json", [row, second])
    with pytest.raises(ValueError, match="on_time_rate"):
        audit.audit_results(directory)


def test_paired_variants_must_use_identical_initial_routes(experiment):
    directory, _, row, _ = experiment
    second = {**row, "variant": "fast", "initial_routes_sha256": "0" * 64}
    _write(directory / "runs.json", [row, second])
    with pytest.raises(ValueError, match="same initial routes"):
        audit.audit_results(directory)


def test_audit_rejects_duplicate_run_cells(experiment):
    directory, _, row, _ = experiment
    _write(directory / "runs.json", [row, row])
    with pytest.raises(ValueError, match="Duplicate experiment cell"):
        audit.audit_results(directory)


def test_input_manifest_hash_cannot_be_changed(experiment):
    directory, manifest, _, _ = experiment
    manifest["input_sha256"] = "0" * 64
    _write(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="input fingerprint"):
        audit.audit_results(directory)


def test_changed_archive_rejected_even_if_manifest_hash_is_updated(experiment, sources):
    directory, manifest, row, _ = experiment
    _, files = sources
    files = dict(files)
    files["uav_dispatch/alns.py"] += b"\n# changed baseline\n"
    with zipfile.ZipFile(directory / "baseline.zip", "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    fingerprint = audit._sha256(directory / "baseline.zip")
    for snapshot in manifest["source_snapshots"].values():
        snapshot["sha256"] = fingerprint
    row["source_sha256"] = fingerprint
    _write(directory / "manifest.json", manifest)
    _write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match="baseline_commit"):
        audit.audit_results(directory)


def test_frozen_runner_must_match_manifest_fingerprint(experiment):
    directory, _, _, _ = experiment
    (directory / "run_optimization_experiments.py").write_text("# modified runner\n")
    with pytest.raises(ValueError, match="Execution source fingerprint"):
        audit.audit_results(directory)


def test_variant_cannot_hide_changed_feature_flags(experiment):
    directory, manifest, _, _ = experiment
    manifest["source_snapshots"]["fast"]["config"] = {"enable_lazy_repair": False}
    _write(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="configuration violates"):
        audit.audit_results(directory)


@pytest.mark.parametrize("field", ["candidate_limit", "time_limit_seconds"])
def test_actual_search_configuration_is_not_only_a_label(experiment, field):
    directory, _, row, _ = experiment
    row["config"][field] = 1
    _write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match="configuration"):
        audit.audit_results(directory)


def test_honest_incomplete_solution_remains_a_failed_result(experiment):
    directory, manifest, row, payload = experiment
    problem = Problem(tuple(load_tasks_csv(audit.DEFAULT_INPUT)[:25]))
    routes = (tuple(payload["routes"][0]["encoded_visits"][2:]),)
    evaluated = audit.evaluate_solution(problem, routes, _force_full=True)
    score = evaluated.score
    assert not evaluated.valid
    trajectory = [[0, 0.0, score.late_count, score.total_lateness_min, score.distance_km]]
    metadata = {**payload["metadata"], "best_trajectory": trajectory}
    result = SolverResult(routes, evaluated, 0.05, 0, metadata)
    payload = result_payload(problem, result, source=manifest["input_path"])
    _write(directory / "solution.json", payload)
    on_time = len(evaluated.delivery_times_min) - score.late_count
    row.update(valid=False, **payload["score"], on_time_count=on_time, on_time_rate=on_time / 25,
               best_trajectory=[[0, 0.1, *trajectory[0][2:]]],
               solution_sha256=audit._sha256(directory / "solution.json"))
    _write(directory / "runs.json", [row])
    report = audit.audit_results(directory)
    assert report["invalid_rows"] == [0]
    assert report["evidence_verified"] and not report["has_valid_results"]


def test_route_tampering_rejected_after_updating_solution_hash(experiment):
    directory, _, row, payload = experiment
    payload["routes"][0]["encoded_visits"] = payload["routes"][0]["encoded_visits"][2:]
    _write(directory / "solution.json", payload)
    row["solution_sha256"] = audit._sha256(directory / "solution.json")
    _write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match="valid flag"):
        audit.audit_results(directory)


@pytest.mark.parametrize("mode", ["regression", "last_score", "time", "metadata"])
def test_trajectory_rejects_regression_and_unbound_history(experiment, mode):
    directory, _, row, _ = experiment
    if mode == "regression":
        first = copy.deepcopy(row["best_trajectory"][0])
        first[2] -= 1
        row["best_trajectory"].insert(0, first)
    elif mode == "last_score":
        row["best_trajectory"][0][2] += 1
    elif mode == "time":
        row["best_trajectory"][0][1] = 9
    else:
        row["best_trajectory"][0][1] += 0.01
    _write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match="[Tt]rajectory"):
        audit.audit_results(directory)


def test_decision_fingerprint_recomputed_from_saved_solution(experiment):
    directory, _, row, _ = experiment
    row["decision_fingerprint"] = "0" * 64
    _write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match="Decision fingerprint"):
        audit.audit_results(directory)


def test_equivalence_summary_rejects_fabricated_gain():
    observations = []
    for case in ("single25", "fleet200"):
        for seed in audit.SEEDS["equivalence"]:
            observations.append({
                "case_id": case, "seed": seed, "equivalent": True,
                "baseline_seconds": 1, "fast_seconds": 0.8,
                **{variant: {"case_id": case, "seed": seed, "variant": variant,
                             "end_to_end_seconds": seconds, "decision_fingerprint": "same"}
                   for variant, seconds in (("baseline", 1), ("fast", 0.8))},
            })
    report = {"observations": observations, "all_equivalent": True,
              "median_runtime_reduction": {"single25": 0.2, "fleet200": 0.2},
              "elapsed_seconds": 10.8, "passed": True}
    assert len(audit._equivalence_rows(report)) == 12
    report["median_runtime_reduction"]["fleet200"] = 0.5
    with pytest.raises(ValueError, match="runtime reduction"):
        audit._equivalence_rows(report)
