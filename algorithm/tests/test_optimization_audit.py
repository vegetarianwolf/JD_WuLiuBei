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


@pytest.fixture
def followup_experiment(experiment):
    directory, manifest, row, payload = experiment
    commit = audit.FOLLOWUP_BASELINE_COMMIT
    files = {
        name.removeprefix("algorithm/src/"): audit._git("show", f"{commit}:{name}")
        for name in audit._git("ls-tree", "-r", "--name-only", commit, "--", "algorithm/src/uav_dispatch").decode().splitlines()
        if name.endswith(".py")
    }
    manifest.update(schema_version=2, kind="regret_followup", baseline_commit=commit,
                    seeds=copy.deepcopy(audit.FOLLOWUP_SEEDS), source_snapshots={},
                    protocol={"stage_cases": copy.deepcopy(audit.FOLLOWUP_STAGE_CASES),
                              "stage_budgets_seconds": copy.deepcopy(audit.FOLLOWUP_STAGE_BUDGETS),
                              "stage_caps_seconds": copy.deepcopy(audit.FOLLOWUP_STAGE_CAPS),
                              "total_experiment_seconds": audit.FOLLOWUP_TOTAL_SECONDS})
    for variant, config in audit.FOLLOWUP_VARIANT_CONFIG.items():
        path = directory / f"{variant}.zip"
        frozen = variant in {"baseline", "old_allocation"}
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in files.items():
                if not frozen and name.endswith("alns.py"):
                    content += b"\n# A different candidate source is expected.\n"
                archive.writestr(name, content)
        manifest["source_snapshots"][variant] = {
            "path": path.name, "sha256": audit._sha256(path), "config": copy.deepcopy(config),
            "source_ref": commit if frozen else "working_tree", "label": variant,
        }
    runner_path = directory / "run_regret_followup.py"
    runner_path.write_bytes(Path(audit.__file__).with_name(runner_path.name).read_bytes())
    manifest["execution_sources"]["run_regret_followup"] = {
        "path": runner_path.name, "sha256": audit._sha256(runner_path),
    }
    row["seed"] = row["config"]["seed"] = payload["metadata"]["seed"] = 2026101341
    row["source_sha256"] = manifest["source_snapshots"]["baseline"]["sha256"]
    _write(directory / "solution.json", payload)
    row["solution_sha256"] = audit._sha256(directory / "solution.json")
    _write(directory / "manifest.json", manifest)
    _write(directory / "runs.json", [row])
    return directory, manifest, row, payload


@pytest.mark.parametrize("variant", audit.FOLLOWUP_VARIANT_CONFIG)
def test_followup_accepts_whitelisted_variants_and_preserves_source_identity(followup_experiment, variant):
    directory, manifest, row, payload = followup_experiment
    row["variant"] = variant
    row["config"].update(audit.FOLLOWUP_VARIANT_CONFIG[variant])
    payload["metadata"].update(audit.FOLLOWUP_VARIANT_CONFIG[variant])
    _write(directory / "solution.json", payload)
    row["solution_sha256"] = audit._sha256(directory / "solution.json")
    row["source_sha256"] = manifest["source_snapshots"][variant]["sha256"]
    _write(directory / "runs.json", [row])
    report = audit.audit_results(directory)
    assert report["schema_version"] == 2
    assert report["evidence_verified"] and report["validated_rows"] == 1
    assert report["validated_equivalence_rows"] == 0 and report["equivalence_sha256"] is None
    assert set(report["execution_sources_sha256"]) == {
        "run_regret_followup", "run_optimization_experiments", "run_scenario_comparison",
    }


def test_followup_medium_single_preserves_five_second_budget(followup_experiment):
    directory, _, row, payload = followup_experiment
    row["stage"] = "medium"
    row["seed"] = row["config"]["seed"] = payload["metadata"]["seed"] = 2026101351
    _write(directory / "solution.json", payload)
    row["solution_sha256"] = audit._sha256(directory / "solution.json")
    _write(directory / "runs.json", [row])
    assert audit.audit_results(directory)["all_runs_successful"]


@pytest.mark.parametrize(("stage", "case_id", "seed", "budget", "message"), [
    ("screen", "single25", 2026101301, 5, "Seed"),
    ("screen", "single25", 2026101341.0, 5, "Seed"),
    ("medium", "single25", 2026101351, 30, "Budget"),
    ("confirm", "single25", 2026101321, 120, "Case"),
    ("confirm", "fleet200", 2026101364, 240, "Seed"),
    ("extensions", "fleet200", 2026101341, 20, "Case"),
    ("screen", "fleet200__direct_relay", 2026101341, 10, "Case"),
    ("extra", "fleet200", 2026101341, 10, "stage"),
])
def test_followup_rejects_rows_outside_fixed_stage_protocol(
    followup_experiment, stage, case_id, seed, budget, message,
):
    directory, _, row, _ = followup_experiment
    row.update(stage=stage, case_id=case_id, seed=seed, budget_seconds=budget)
    _write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match=message):
        audit.audit_results(directory)


@pytest.mark.parametrize("change", ["unknown_schema", "bool_schema", "kind", "baseline", "seed",
                                  "seed_order", "missing_known_regression", "new_stage", "budget", "cases",
                                  "stage_cap", "total_cap"])
def test_followup_manifest_cannot_redefine_whitelist(followup_experiment, change):
    directory, manifest, _, _ = followup_experiment
    if change == "unknown_schema":
        manifest["schema_version"] = 3
    elif change == "bool_schema":
        manifest["schema_version"] = True
    elif change == "kind":
        manifest["kind"] = "exploratory_optimization"
    elif change == "baseline":
        manifest["baseline_commit"] = "0" * 40
    elif change == "seed":
        manifest["seeds"]["screen"][0] = 2026101301
    elif change == "seed_order":
        manifest["seeds"]["confirm"].reverse()
    elif change == "missing_known_regression":
        manifest["seeds"]["confirm"].pop(0)
    elif change == "new_stage":
        manifest["seeds"]["extra"] = [2026101341]
    elif change == "budget":
        manifest["protocol"]["stage_budgets_seconds"]["medium"]["single25"] = 30
    elif change == "stage_cap":
        manifest["protocol"]["stage_caps_seconds"]["medium"] = 300
    elif change == "total_cap":
        manifest["protocol"]["total_experiment_seconds"] = 7200
    else:
        manifest["protocol"]["stage_cases"]["confirm"].append("single25")
    _write(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="schema_version|kind|prespecified"):
        audit.audit_results(directory)


@pytest.mark.parametrize("change", ["unknown_variant", "flags", "numeric_flag", "old_ref", "candidate_ref"])
def test_followup_source_labels_cannot_redefine_algorithm(followup_experiment, change):
    directory, manifest, _, _ = followup_experiment
    snapshots = manifest["source_snapshots"]
    if change == "unknown_variant":
        snapshots["new_unreviewed"] = copy.deepcopy(snapshots["fallback"])
    elif change == "flags":
        snapshots["portfolio"]["config"]["enable_route_distinct_regret"] = True
    elif change == "numeric_flag":
        snapshots["fallback"]["config"]["enable_lazy_repair"] = 1
    elif change == "old_ref":
        snapshots["old_allocation"]["source_ref"] = "working_tree"
    else:
        snapshots["fallback"]["source_ref"] = audit.FOLLOWUP_BASELINE_COMMIT
    _write(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="configuration violates|source_ref"):
        audit.audit_results(directory)


def test_followup_old_allocation_must_match_git_even_after_rehash(followup_experiment):
    directory, manifest, _, _ = followup_experiment
    manifest["source_snapshots"]["old_allocation"].update(
        path=manifest["source_snapshots"]["fallback"]["path"],
        sha256=manifest["source_snapshots"]["fallback"]["sha256"],
    )
    _write(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="Old allocation.*baseline_commit"):
        audit.audit_results(directory)


@pytest.mark.parametrize("change", ["missing", "modified", "wrong_worker"])
def test_followup_requires_bound_orchestrator_and_original_worker(followup_experiment, change):
    directory, manifest, _, _ = followup_experiment
    if change == "missing":
        manifest["execution_sources"].pop("run_regret_followup")
    elif change == "modified":
        (directory / "run_regret_followup.py").write_text("# Changed after the run.\n")
    else:
        manifest["runner_sha256"] = manifest["execution_sources"]["run_regret_followup"]["sha256"]
    _write(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="frozen experiment runner|Execution source fingerprint|runner_sha256"):
        audit.audit_results(directory)


def test_followup_rejects_previous_batch_equivalence_file(followup_experiment):
    directory, _, _, _ = followup_experiment
    _write(directory / "equivalence.json", {})
    with pytest.raises(ValueError, match="equivalence.json"):
        audit.audit_results(directory)


@pytest.mark.parametrize("field", ["enable_lazy_repair", "enable_regret_portfolio", "candidate_limit"])
def test_followup_run_configuration_must_match_exact_feature_flags(followup_experiment, field):
    directory, manifest, row, _ = followup_experiment
    row["variant"] = "portfolio"
    row["source_sha256"] = manifest["source_snapshots"]["portfolio"]["sha256"]
    row["config"].update(audit.FOLLOWUP_VARIANT_CONFIG["portfolio"])
    row["config"][field] = 1
    _write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match="configuration"):
        audit.audit_results(directory)


def test_schema_one_cannot_use_followup_protocol_or_variants(experiment):
    directory, manifest, row, _ = experiment
    row["seed"] = 2026101341
    _write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match="Seed.*prespecified"):
        audit.audit_results(directory)
    manifest["source_snapshots"]["fallback"] = copy.deepcopy(manifest["source_snapshots"]["fast"])
    manifest["source_snapshots"]["fallback"]["config"] = audit.FOLLOWUP_VARIANT_CONFIG["fallback"]
    _write(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="configuration violates"):
        audit.audit_results(directory)


@pytest.mark.parametrize("value", [False, 1, None])
def test_followup_rejects_inconsistent_or_non_boolean_recorded_feature_flags(followup_experiment, value):
    directory, manifest, row, payload = followup_experiment
    row["variant"] = "portfolio"
    row["config"].update(audit.FOLLOWUP_VARIANT_CONFIG["portfolio"])
    row["source_sha256"] = manifest["source_snapshots"]["portfolio"]["sha256"]
    payload["metadata"].update(audit.FOLLOWUP_VARIANT_CONFIG["portfolio"])
    payload["metadata"]["enable_regret_portfolio"] = value
    _write(directory / "solution.json", payload)
    row["solution_sha256"] = audit._sha256(directory / "solution.json")
    _write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match="feature metadata.*enable_regret_portfolio"):
        audit.audit_results(directory)


@pytest.fixture
def supplemental_experiment(followup_experiment):
    parent, parent_manifest, template, _ = followup_experiment
    rows = []
    for stage, candidate in (("screen", "portfolio"), ("medium", "fallback")):
        for case, tasks, drones in (("single25", 25, 1), ("fleet200", 200, 8)):
            for seed in audit.FOLLOWUP_SEEDS[stage]:
                for variant in ("baseline", candidate):
                    row = copy.deepcopy(template)
                    late = 10
                    if variant == candidate and case == "fleet200":
                        late = 11 if stage == "medium" and seed == 2026101353 else 9
                    row.update(stage=stage, variant=variant, case_id=case, task_count=tasks,
                               drone_count=drones, seed=seed, late_count=late,
                               budget_seconds=audit.FOLLOWUP_STAGE_BUDGETS[stage][case],
                               source_sha256=parent_manifest["source_snapshots"][variant]["sha256"])
                    rows.append(row)
    decisions = [{"stage": "screen", "selected_variant": "fallback", "candidates": {"portfolio": True, "fallback": True}},
                 {"stage": "medium", "selected_variant": "fallback", "passed": False}]
    _write(parent / "runs.json", rows)
    _write(parent / "decisions.json", decisions)
    child = parent / "supplemental_trial"
    child.mkdir()
    manifest = copy.deepcopy(parent_manifest)
    for source in list(manifest["source_snapshots"].values()) + list(manifest["execution_sources"].values()):
        (child / source["path"]).write_bytes((parent / source["path"]).read_bytes())
    launcher = child / "run_regret_portfolio_validation.py"
    launcher.write_text("# Frozen supplemental orchestration fixture\n")
    manifest["execution_sources"]["run_regret_portfolio_validation"] = {"path": launcher.name, "sha256": audit._sha256(launcher)}
    manifest["post_hoc_exploration"] = {
        "reason": "Fallback failed its medium gate; portfolio already passed screening.",
        "parent_directory": "..", "candidate": "portfolio",
        **{f"parent_{name}_sha256": audit._sha256(parent / f"{name}.json") for name in ("manifest", "runs", "decisions")},
    }
    return parent, child, manifest, rows, decisions


def test_supplemental_requires_explicit_lineage_and_fourth_frozen_launcher(supplemental_experiment):
    _, child, manifest, _, _ = supplemental_experiment
    hashes = audit._audit_execution_sources(child, manifest)
    assert set(hashes) == {"run_optimization_experiments", "run_scenario_comparison", "run_regret_followup", "run_regret_portfolio_validation"}


@pytest.mark.parametrize("mutation", ["missing_marker", "null_marker", "schema_one", "missing_launcher", "launcher_changed",
                                      "path", "candidate", "empty_reason", "extra_marker_key", "parent_hash", "decisions_hash",
                                      "source_changed", "old_execution_changed"])
def test_supplemental_rejects_unbound_or_changed_protocol(supplemental_experiment, mutation):
    _, child, manifest, _, _ = supplemental_experiment
    marker = manifest["post_hoc_exploration"]
    if mutation == "missing_marker":
        manifest.pop("post_hoc_exploration")
    elif mutation == "null_marker":
        manifest["post_hoc_exploration"] = None
    elif mutation == "schema_one":
        manifest["schema_version"] = 1
    elif mutation == "missing_launcher":
        manifest["execution_sources"].pop("run_regret_portfolio_validation")
    elif mutation == "launcher_changed":
        (child / "run_regret_portfolio_validation.py").write_text("# Changed after freeze\n")
    elif mutation == "path":
        marker["parent_directory"] = "../.."
    elif mutation == "candidate":
        marker["candidate"] = "fallback"
    elif mutation == "empty_reason":
        marker["reason"] = " "
    elif mutation == "extra_marker_key":
        marker["allow_seed_replacement"] = True
    elif mutation == "parent_hash":
        marker["parent_runs_sha256"] = "0" * 64
    elif mutation == "decisions_hash":
        marker["parent_decisions_sha256"] = "0" * 64
    elif mutation == "source_changed":
        manifest["source_snapshots"]["portfolio"]["sha256"] = "0" * 64
    else:
        manifest["execution_sources"]["run_regret_followup"]["sha256"] = "0" * 64
    with pytest.raises(ValueError):
        audit._audit_execution_sources(child, manifest)


@pytest.mark.parametrize("mutation", ["screen_all_ties", "screen_loss", "single_regression", "screen_missing_seed",
                                      "screen_replaced_seed", "screen_duplicate", "screen_initial_mismatch",
                                      "medium_actually_passed", "medium_missing_seed", "medium_decision_passed",
                                      "medium_decision_wrong_candidate"])
def test_supplemental_recomputes_parent_qualification_even_after_rehash(supplemental_experiment, mutation):
    parent, child, manifest, rows, decisions = supplemental_experiment
    screen_candidates = [row for row in rows if row["stage"] == "screen" and row["case_id"] == "fleet200" and row["variant"] == "portfolio"]
    if mutation == "screen_all_ties":
        for row in screen_candidates:
            row["late_count"] = 10
    elif mutation == "screen_loss":
        screen_candidates[0]["late_count"] = 11
    elif mutation == "single_regression":
        next(row for row in rows if row["stage"] == "screen" and row["case_id"] == "single25" and row["variant"] == "portfolio")["late_count"] = 11
    elif mutation == "screen_missing_seed":
        rows.remove(screen_candidates[0])
    elif mutation == "screen_replaced_seed":
        screen_candidates[0]["seed"] = 2026101343
    elif mutation == "screen_duplicate":
        rows.append(copy.deepcopy(screen_candidates[0]))
    elif mutation == "screen_initial_mismatch":
        screen_candidates[0]["initial_routes_sha256"] = "0" * 64
    elif mutation == "medium_actually_passed":
        for row in rows:
            if row["stage"] == "medium" and row["variant"] == "fallback" and row["case_id"] == "fleet200":
                row["late_count"] = 9
    elif mutation == "medium_missing_seed":
        rows.remove(next(row for row in rows if row["stage"] == "medium"))
    elif mutation == "medium_decision_passed":
        decisions[1]["passed"] = True
    else:
        decisions[1]["selected_variant"] = "portfolio"
    _write(parent / "runs.json", rows)
    _write(parent / "decisions.json", decisions)
    for name in ("runs", "decisions"):
        manifest["post_hoc_exploration"][f"parent_{name}_sha256"] = audit._sha256(parent / f"{name}.json")
    with pytest.raises(ValueError, match="Post-hoc parent"):
        audit._audit_execution_sources(child, manifest)


def test_followup_budget_records_stage_caps_and_reserved_margin(followup_experiment):
    directory, _, row, _ = followup_experiment
    report = audit.audit_results(directory)
    budget = report["cumulative_budget"]
    assert budget["scope"] == "this_batch_and_probe"
    assert budget["total_seconds"] == row["end_to_end_seconds"]
    assert budget["shared_total_limit_seconds"] == 3600
    assert budget["verification_reserve_seconds"] == 60
    assert budget["execution_limit_seconds"] == 3540
    assert budget["cohorts"][0]["stages"]["screen"]["completed_runs"] == 1
    assert budget["budget_compliant"] and report["all_runs_successful"]


@pytest.mark.parametrize("elapsed", [1.5, 181.0])
def test_followup_budget_preserves_no_result_failures_and_counts_stage_time(followup_experiment, elapsed):
    directory, _, row, _ = followup_experiment
    _write(directory / "execution_notes.json", {"aborted_runs": [{
        "stage": "screen", "case_id": "single25", "variant": "fallback", "seed": 2026101342,
        "status": "timeout_no_result", "process_elapsed_seconds": elapsed,
    }]})
    report = audit.audit_results(directory)
    budget = report["cumulative_budget"]
    assert report["evidence_verified"] and report["has_valid_results"]
    assert not report["all_runs_successful"]
    assert budget["total_seconds"] == row["end_to_end_seconds"] + elapsed
    assert budget["aborted_run_count"] == 1
    stage = budget["cohorts"][0]["stages"]["screen"]
    assert stage["aborted_seconds"] == elapsed and stage["aborted_runs"] == 1
    assert stage["budget_compliant"] is (elapsed < 180)
    assert budget["budget_compliant"] is (elapsed < 180)
    assert budget["cohorts"][0]["execution_notes_sha256"] == audit._sha256(directory / "execution_notes.json")


@pytest.mark.parametrize("elapsed", [-1, float("nan"), None, True])
def test_followup_budget_rejects_missing_or_invalid_failed_process_time(followup_experiment, elapsed):
    directory, manifest, row, _ = followup_experiment
    _write(directory / "execution_notes.json", {"aborted_runs": [{
        "stage": "screen", "case_id": "single25", "variant": "fallback", "seed": 2026101342,
        "process_elapsed_seconds": elapsed,
    }]})
    with pytest.raises(ValueError, match="process_elapsed_seconds"):
        audit._audit_followup_budget(directory, manifest, [row])


def test_followup_budget_rejects_aborted_cell_rerun(followup_experiment):
    directory, manifest, row, _ = followup_experiment
    _write(directory / "execution_notes.json", {"aborted_runs": [{
        **{key: row[key] for key in ("stage", "case_id", "variant", "seed")},
        "process_elapsed_seconds": 1.0,
    }]})
    with pytest.raises(ValueError, match="duplicated or rerun"):
        audit._audit_followup_budget(directory, manifest, [row])


@pytest.mark.parametrize(("elapsed", "total_compliant"), [(3550.0, True), (3601.0, False)])
def test_cumulative_overrun_is_retained_without_claiming_success(followup_experiment, elapsed, total_compliant):
    directory, _, _, _ = followup_experiment
    (directory / "diagnostics").mkdir()
    _write(directory / "diagnostics/local_repair_probe.json", {"elapsed_seconds": elapsed})
    report = audit.audit_results(directory)
    budget = report["cumulative_budget"]
    assert report["evidence_verified"] and report["has_valid_results"]
    assert not report["all_runs_successful"] and not budget["budget_compliant"]
    assert not budget["verification_reserve_preserved"]
    assert budget["total_budget_compliant"] is total_compliant
    assert budget["violations"]


def test_supplemental_budget_shares_parent_total_and_counts_probe_once(supplemental_experiment):
    parent, child, manifest, parent_rows, _ = supplemental_experiment
    row = {**parent_rows[0], "stage": "medium", "seed": 2026101351,
           "budget_seconds": 5, "end_to_end_seconds": 4.0}
    _write(child / "runs.json", [row])
    (parent / "diagnostics").mkdir()
    _write(parent / "diagnostics/local_repair_probe.json", {"elapsed_seconds": 0.5})
    _write(parent / "execution_notes.json", {"aborted_runs": [{
        "stage": "extensions", "case_id": "fleet200__direct_relay", "variant": "fallback",
        "seed": 2026101341, "process_elapsed_seconds": 2.0,
    }]})
    budget = audit._audit_followup_budget(child, manifest, [row])
    assert budget["scope"] == "parent_and_supplement_and_probe"
    assert budget["total_seconds"] == pytest.approx(sum(r["end_to_end_seconds"] for r in parent_rows) + 4 + 2 + 0.5)
    assert budget["local_probe_seconds"] == 0.5
    assert budget["local_probe_file"] == "../diagnostics/local_repair_probe.json"
    assert budget["local_probe_sha256"] == audit._sha256(parent / "diagnostics/local_repair_probe.json")
    assert budget["aborted_run_count"] == 1
    assert [c["cohort"] for c in budget["cohorts"]] == ["parent", "current"]
    assert budget["budget_compliant"]


def test_supplemental_stage_caps_apply_per_cohort_but_total_is_shared(supplemental_experiment):
    parent, child, manifest, parent_rows, _ = supplemental_experiment
    parent_row = {**parent_rows[0], "stage": "medium", "seed": 2026101351,
                  "budget_seconds": 5, "end_to_end_seconds": 150.0}
    child_row = {**parent_row, "end_to_end_seconds": 160.0}
    _write(parent / "runs.json", [parent_row])
    _write(child / "runs.json", [child_row])
    budget = audit._audit_followup_budget(child, manifest, [child_row])
    assert budget["total_seconds"] == 310
    assert budget["stage_caps_apply_per_cohort"] and budget["budget_compliant"]
    assert all(c["stages"]["medium"]["budget_compliant"] for c in budget["cohorts"])
