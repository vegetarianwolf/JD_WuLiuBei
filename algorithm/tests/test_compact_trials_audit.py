"""Compact trial evidence checks, including the shared parent time allowance."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from algorithm.experiments import audit_compact_trials as audit
from algorithm.tests.test_objective_trials_audit import (
    baseline_sources, experiment as objective_experiment, write, bind_decisions,
)


@pytest.fixture(autouse=True)
def compact_protocol(monkeypatch):
    protocol = SimpleNamespace(
        BASELINE_COMMIT="32c2d6055d89e831f0e8b783d1cac5c2c750fb1c",
        VARIANTS={"baseline": {}, "compact": {"min_destroy_fraction": .02, "max_destroy_fraction": .05}},
        SEEDS={"equivalence": [2026101401, 2026101402, 2026101403], "screen": [2026101401, 2026101402],
               "medium": [2026101411, 2026101412, 2026101413],
               "confirm": [2026101321, 2026101421, 2026101422, 2026101423]},
        CASES={"equivalence": ["single25"], "screen": ["single25", "fleet200"],
               "medium": ["single25", "fleet200"], "confirm": ["fleet200"]},
        BUDGETS={"equivalence": {"single25": 10}, "screen": {"single25": 5, "fleet200": 10},
                 "medium": {"single25": 5, "fleet200": 30}, "confirm": {"fleet200": 240}},
        CAPS={"equivalence": 60, "screen": 60, "medium": 210, "confirm": 1920},
        TOTAL_SECONDS=2700, RESERVED_SECONDS=60,
    )
    monkeypatch.setattr(audit, "_protocol", lambda: protocol)
    return protocol


@pytest.fixture
def compact_experiment(objective_experiment, compact_protocol, monkeypatch):
    directory, manifest, row, payload, _ = objective_experiment
    manifest.update(schema_version=4, kind="compact_trials", seeds=compact_protocol.SEEDS)
    source = deepcopy(manifest["source_snapshots"]["baseline"])
    manifest["source_snapshots"] = {variant: {**source, "config": flags}
                                    for variant, flags in compact_protocol.VARIANTS.items()}
    execution = manifest["execution_sources"]
    execution.pop("run_objective_trials")
    script = directory / "run_compact_trials.py"
    script.write_text("# frozen compact runner\n")
    execution["run_compact_trials"] = {"path": script.name, "sha256": audit.checks._sha256(script)}
    manifest["protocol"] = {"stage_cases": compact_protocol.CASES, "stage_budgets_seconds": compact_protocol.BUDGETS,
                            "stage_caps_seconds": compact_protocol.CAPS, "total_experiment_seconds": 2700, "reserved_seconds": 60}
    row.update(variant="compact", seed=2026101401)
    row["config"].update(seed=2026101401, **compact_protocol.VARIANTS["compact"])
    payload["metadata"]["seed"] = 2026101401
    write(directory / "solution.json", payload)
    row["solution_sha256"] = audit.checks._sha256(directory / "solution.json")
    bind_decisions(row, payload)
    write(directory / "manifest.json", manifest)
    write(directory / "runs.json", [row])
    parent = {"manifest": deepcopy(manifest), "budget": {"total_seconds": 380., "aborted_run_count": 0},
              "verification": {"validated_rows": 39}}
    monkeypatch.setattr(audit, "_parent", lambda *_: parent)
    return directory, manifest, row, payload, parent


def test_numeric_config_is_audited_without_inventing_historical_metadata(compact_experiment, monkeypatch):
    directory, _, row, payload, _ = compact_experiment
    assert "min_destroy_fraction" not in payload["metadata"]
    actual, calls = audit.evaluate_solution, []
    def checked(*args, **kwargs):
        calls.append(kwargs.get("_force_full"))
        return actual(*args, **kwargs)
    monkeypatch.setattr(audit, "evaluate_solution", checked)
    report = audit.audit_results(directory)
    assert calls == [True]
    assert report["valid_on_budget_runs"] == 1
    assert report["cumulative_budget"]["total_seconds"] == pytest.approx(380.3)
    assert report["equivalence_sha256"] is None


@pytest.mark.parametrize(("field", "value", "message"), [
    ("seed", 2026101371, "frozen experiment cell"), ("source_sha256", "0" * 64, "source_sha256"),
    ("late_count", 0, "row late_count"), ("solution_sha256", "0" * 64, "solution fingerprint"),
    ("budget_seconds", 50, "frozen budget"), ("decision_fingerprint", "0" * 64, "Decision fingerprint"),
])
def test_child_evidence_tampering_removes_stale_validation(compact_experiment, field, value, message):
    directory, _, row, *_ = compact_experiment
    audit.audit_results(directory)
    row[field] = value
    write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match=message):
        audit.audit_results(directory)
    assert not (directory / "validation.json").exists()


def test_fraction_cannot_change_after_freezing(compact_experiment):
    directory, _, row, *_ = compact_experiment
    row["config"]["min_destroy_fraction"] = .01
    write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match="frozen numeric"):
        audit.audit_results(directory)


def test_candidate_cannot_use_working_tree_source(compact_experiment):
    directory, manifest, *_ = compact_experiment
    manifest["source_snapshots"]["compact"]["source_ref"] = "working_tree"
    write(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="source reference"):
        audit.audit_results(directory)


def test_single_equivalence_uses_full_decision_fingerprint():
    rows = gate_rows("equivalence")
    assert audit._gates(rows)["equivalence"]
    next(row for row in rows if row["variant"] == "compact")["decision_fingerprint"] = "b" * 64
    assert not audit._gates(rows)["equivalence"]


def gate_rows(stage):
    protocol = audit._protocol()
    return [{"stage": stage, "case_id": case, "variant": variant, "seed": seed,
             "valid": True, "budget_compliant": True, "late_count": 10 if variant == "baseline" else 9,
             "total_lateness_min": 20., "distance_km": 50., "iterations": 200, "decision_fingerprint": "a" * 64}
            for case in protocol.CASES[stage] for seed in protocol.SEEDS[stage] for variant in protocol.VARIANTS]


def test_confirmation_keeps_regressions_and_requires_four_pairs(tmp_path):
    rows = [row for stage in ("equivalence", "screen", "medium", "confirm") for row in gate_rows(stage)]
    first = next(row for row in rows if row["stage"] == "confirm" and row["variant"] == "compact")
    first["late_count"] = 11
    decisions = [
        {"stage": "equivalence", "selected_variant": "compact", "passed": True, "completed_pairs": 3, "planned_pairs": 3},
        {"stage": "screen", "selected_variant": "compact", "passed": True},
        {"stage": "medium", "selected_variant": "compact", "passed": True},
        {"stage": "confirm", "selected_variant": "compact", "passed": False, "completed_pairs": 4,
         "planned_pairs": 4, "continuation_after_regression": True},
    ]
    write(tmp_path / "decisions.json", decisions)
    assert audit._decisions(tmp_path, rows)["protocol_complete"]
    assert not audit._decisions(tmp_path, rows)["default_eligible"]
    partial = [row for row in rows if row["stage"] != "confirm" or row["seed"] == 2026101321]
    decisions[-1]["completed_pairs"] = 1
    write(tmp_path / "decisions.json", decisions)
    with pytest.raises(ValueError, match="missing or unexpected cells"):
        audit._decisions(tmp_path, partial)


def test_known_counterexample_is_not_counted_as_new_seed_win():
    rows = [row for stage in ("equivalence", "screen", "medium", "confirm") for row in gate_rows(stage)]
    for row in rows:
        if row["stage"] == "confirm" and row["variant"] == "compact" and row["seed"] in (2026101422, 2026101423):
            row["late_count"] = 10
    assert not audit._gates(rows)["confirm"]


def test_parent_time_and_aborted_child_attempts_share_the_original_cap(tmp_path):
    parent = {"budget": {"total_seconds": 2600., "aborted_run_count": 1}}
    write(tmp_path / "execution_notes.json", {"aborted_runs": [
        {"stage": "screen", "case_id": "fleet200", "variant": "compact", "seed": 2026101401,
         "process_elapsed_seconds": 50.}]})
    budget = audit._budget(tmp_path, [], parent)
    assert budget["total_seconds"] == 2650.
    assert budget["aborted_run_count"] == 2
    assert not budget["budget_compliant"]


def parent_binding(tmp_path):
    child = tmp_path / "compact_trial"
    child.mkdir()
    marker = {"parent_directory": "..", "parent_execution_notes_sha256": None, "parent_experiment_seconds": 380.}
    for name, value in (("manifest", {}), ("runs", []), ("decisions", [])):
        write(tmp_path / f"{name}.json", value)
        marker[f"parent_{name}_sha256"] = audit.checks._sha256(tmp_path / f"{name}.json")
    return child, {"post_hoc_exploration": marker}


@pytest.mark.parametrize("name", ["manifest", "runs", "decisions"])
def test_parent_binding_detects_changed_parent_documents(tmp_path, name):
    child, manifest = parent_binding(tmp_path)
    (tmp_path / f"{name}.json").write_text("{}\n ")
    with pytest.raises(ValueError, match=f"Parent {name} fingerprint"):
        audit._parent(child, manifest)


def test_new_parent_abort_cannot_be_omitted_from_shared_budget(tmp_path):
    child, manifest = parent_binding(tmp_path)
    write(tmp_path / "execution_notes.json", {"aborted_runs": [{"process_elapsed_seconds": 100}]})
    with pytest.raises(ValueError, match="execution notes fingerprint"):
        audit._parent(child, manifest)


def test_parent_must_have_failed_medium_and_skipped_confirmation(tmp_path, monkeypatch):
    child, manifest = parent_binding(tmp_path)
    monkeypatch.setattr(audit.parent_audit, "_manifest", lambda *_: None)
    monkeypatch.setattr(audit.parent_audit, "_sources", lambda *_: ({}, {}))
    monkeypatch.setattr(audit, "_audit_rows", lambda *_: {"invalid_rows": [], "over_budget_rows": []})
    monkeypatch.setattr(audit.parent_audit, "_decision_audit", lambda *_: {
        "protocol_complete": True, "selected_variant": "boundary", "medium": {"boundary": True}})
    with pytest.raises(ValueError, match="parent medium failure"):
        audit._parent(child, manifest)


def test_parent_runtime_marker_is_recomputed_not_trusted(tmp_path, monkeypatch):
    child, manifest = parent_binding(tmp_path)
    decisions = [{"stage": "medium", "passed": False, "candidates": {"boundary": False, "objective": False}},
                 {"stage": "confirm", "skipped": True}]
    write(tmp_path / "decisions.json", decisions)
    manifest["post_hoc_exploration"]["parent_decisions_sha256"] = audit.checks._sha256(tmp_path / "decisions.json")
    monkeypatch.setattr(audit.parent_audit, "_manifest", lambda *_: None)
    monkeypatch.setattr(audit.parent_audit, "_sources", lambda *_: ({}, {}))
    monkeypatch.setattr(audit, "_audit_rows", lambda *_: {"invalid_rows": [], "over_budget_rows": []})
    monkeypatch.setattr(audit.parent_audit, "_decision_audit", lambda *_: {
        "protocol_complete": True, "selected_variant": None, "medium": {"boundary": False}})
    monkeypatch.setattr(audit.parent_audit, "_complete", lambda *_: True)
    monkeypatch.setattr(audit.parent_audit, "_budget", lambda *_: {"total_seconds": 381., "budget_compliant": True})
    with pytest.raises(ValueError, match="parent experiment seconds"):
        audit._parent(child, manifest)
