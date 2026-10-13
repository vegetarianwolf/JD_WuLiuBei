"""Fresh follow-up, parent immutability and shared-cap tests without ALNS."""
from copy import deepcopy

import pytest

from algorithm.experiments import audit_method_iteration2 as audit
from algorithm.experiments import run_method_iteration2 as trial


def _row(stage, case, variant, seed, late=70):
    order = list(trial.VARIANTS)
    if trial.SEEDS[stage].index(seed) % 2:
        order.reverse()
    tasks, drones = (25, 1) if case == "single25" else (200, 8)
    return {"stage": stage, "case_id": case, "variant": variant, "seed": seed,
            "valid": True, "budget_compliant": True, "scenario": "pure_direct", "task_count": tasks,
            "drone_count": drones, "budget_seconds": trial.BUDGETS[stage][case],
            "end_to_end_seconds": trial.BUDGETS[stage][case] - 1., "initial_routes_sha256": "same",
            "late_count": late, "total_lateness_min": 2200., "distance_km": 640., "iterations": 100,
            "order": order.index(variant)}


def _rows(stage):
    return [_row(stage, case, v, seed) for seed in trial.SEEDS[stage] for case in trial.CASES[stage] for v in trial.VARIANTS]


def _mock(monkeypatch, tmp_path, parent_seconds=115.405):
    output = tmp_path / "iteration2"
    output.mkdir()
    trial.base.write_json(tmp_path / "runs.json", [{"end_to_end_seconds": parent_seconds}])
    trial.base.write_json(output / "runs.json", [])
    monkeypatch.setattr(trial, "verify", lambda *_: None)
    calls = []
    def invoke(output, manifest, variant, case, seed, stage, budget):
        calls.append((stage, case, variant, seed))
        return _row(stage, case, variant, seed, late=90 if variant == "strings" else 70)
    monkeypatch.setattr(trial.base, "invoke", invoke)
    return output, calls


def test_followup_has_fresh_seeds_and_only_320_second_planned_work():
    assert not set(trial.SEEDS).intersection({"equivalence", "confirm"})
    assert not {s for seeds in trial.SEEDS.values() for s in seeds}.intersection({s for seeds in trial.first.SEEDS.values() for s in seeds})
    planned = sum(len(trial.SEEDS[s]) * len(trial.VARIANTS) * sum(trial.BUDGETS[s].values()) for s in trial.SEEDS)
    assert planned == sum(trial.CAPS.values()) == 320
    assert planned + 115.405 < trial.TOTAL_SECONDS - trial.RESERVED_SECONDS


def test_screen_requires_a_win_and_no_single_or_fleet_regression():
    rows = _rows("screen")
    assert not trial.screen_pass(rows)
    next(r for r in rows if r["variant"] == "strings" and r["case_id"] == "fleet200")["late_count"] = 69
    assert trial.screen_pass(rows)
    next(r for r in rows if r["variant"] == "strings" and r["case_id"] == "single25")["distance_km"] += 1
    assert not trial.screen_pass(rows)


def test_majority_allows_loss_but_requires_three_complete_paired_seeds():
    rows = _rows("medium")
    fleet = [r for r in rows if r["variant"] == "strings" and r["case_id"] == "fleet200"]
    for row, late in zip(fleet, (69, 69, 95)):
        row["late_count"] = late
    assert trial.medium_pass(rows)
    rows.remove(fleet[-1])
    assert not trial.medium_pass(rows)


def test_all_validation_seeds_run_despite_immediate_regression(monkeypatch, tmp_path):
    output, calls = _mock(monkeypatch, tmp_path)
    rows = trial.stage(output, {}, "medium")
    assert len(rows) == len(calls) == 12
    assert {r["seed"] for r in rows} == set(trial.SEEDS["medium"])
    assert [c[2] for c in calls] == ["baseline", "strings", "baseline", "strings", "strings", "baseline", "strings", "baseline", "baseline", "strings", "baseline", "strings"]
    assert trial.consumed(output) == 115.405 + 228


def test_parent_time_and_abort_time_are_reserved_before_starting_stage(monkeypatch, tmp_path):
    output, calls = _mock(monkeypatch, tmp_path, parent_seconds=620)
    trial.base.write_json(tmp_path / "execution_notes.json", {"aborted_runs": [{"process_elapsed_seconds": 11.}]})
    with pytest.raises(RuntimeError, match="全部剩余"):
        trial.stage(output, {}, "medium")
    assert calls == []


def test_followup_never_retries_aborted_cell_or_orphan_solution(monkeypatch, tmp_path):
    output, calls = _mock(monkeypatch, tmp_path)
    trial.base.write_json(output / "execution_notes.json", {"aborted_runs": [{"stage": "screen", "case_id": "single25",
        "variant": "baseline", "seed": trial.SEEDS["screen"][0], "process_elapsed_seconds": 0.}]})
    with pytest.raises(RuntimeError, match="中止"):
        trial.stage(output, {}, "screen")
    (output / "execution_notes.json").unlink()
    orphan = output / f"run_solutions/screen__single25__baseline__{trial.SEEDS['screen'][0]}.json"
    trial.base.write_json(orphan, {"preserve": True})
    with pytest.raises(RuntimeError, match="孤立"):
        trial.stage(output, {}, "screen")
    assert calls == []
    assert trial.base.read_json(orphan) == {"preserve": True}


@pytest.fixture
def parent_binding(monkeypatch, tmp_path):
    output = tmp_path / "iteration2"
    output.mkdir()
    source = tmp_path / "run_solutions/first.json"
    trial.base.write_json(source, {"preserve": True})
    parent = {"schema_version": 5, "kind": "method_trials", "baseline_variant": "baseline",
              "baseline_commit": trial.BASELINE_COMMIT, "input_path": trial.base.INPUT,
              "input_sha256": "same-input", "seeds": trial.first.SEEDS, "protocol": trial.first.PROTOCOL,
              "source_snapshots": {}, "execution_sources": {}}
    rows = [{"end_to_end_seconds": 115.405, "solution_file": "run_solutions/first.json", "solution_sha256": trial.base.sha256(source)}]
    for name, data in (("manifest", parent), ("runs", rows), ("decisions", [])):
        trial.base.write_json(tmp_path / f"{name}.json", data)
    validation = {"passed": True, "protocol_complete": True, "decision_audit": {"selected_variant": None},
                  "cumulative_budget": {"total_seconds": 115.405},
                  **{f"{name}_sha256": trial.base.sha256(tmp_path / f"{name}.json") for name in ("manifest", "runs", "decisions")}}
    trial.base.write_json(tmp_path / "validation.json", validation)
    marker = trial.parent_evidence(output)
    monkeypatch.setattr(audit.original, "_sources", lambda *_: ({}, {}))
    manifest = {"parent_binding": marker, "input_sha256": "same-input"}
    assert audit._parent(output, manifest)["parent_seconds"] == 115.405
    return output, manifest


@pytest.mark.parametrize("name", ["manifest", "runs", "decisions", "validation"])
def test_independent_audit_rejects_changed_parent_documents(parent_binding, name):
    output, manifest = parent_binding
    path = output.parent / f"{name}.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="Parent frozen"):
        audit._parent(output, manifest)


def test_independent_audit_rejects_changed_parent_routes_or_abort_notes(parent_binding):
    output, manifest = parent_binding
    path = output.parent / "run_solutions/first.json"
    before = path.read_bytes()
    path.write_bytes(before + b"\n")
    with pytest.raises(ValueError, match="Parent saved solution"):
        audit._parent(output, manifest)
    path.write_bytes(before)
    trial.base.write_json(output.parent / "execution_notes.json", {"aborted_runs": []})
    with pytest.raises(ValueError, match="Parent aborted"):
        audit._parent(output, manifest)


def test_independent_gate_accepts_majority_and_rejects_forged_promotion(monkeypatch, tmp_path):
    rows = [*_rows("screen"), *_rows("medium")]
    for row in rows:
        if row["variant"] == "strings" and row["case_id"] == "fleet200":
            row["late_count"] = 95 if row["stage"] == "medium" and row["seed"] == trial.SEEDS["medium"][-1] else 69
    decisions = [{"stage": "screen", "candidates": {"strings": True}, "eligible_variants": ["strings"]},
                 {"stage": "medium", "selected_variant": "strings", "passed": True, "skipped": False,
                  "candidates": {"strings": True}, "continuation_after_regression": True}]
    trial.base.write_json(tmp_path / "decisions.json", decisions)
    monkeypatch.setattr(trial, "medium_pass", lambda *_: (_ for _ in ()).throw(AssertionError("runner gate invoked")))
    assert audit._decision_audit(tmp_path, rows)["default_eligible"]
    decisions[-1]["selected_variant"] = None
    trial.base.write_json(tmp_path / "decisions.json", decisions)
    with pytest.raises(ValueError, match="independent gate"):
        audit._decision_audit(tmp_path, rows)


def test_independent_budget_includes_parent_and_preserves_audit_reserve(tmp_path):
    rows = [_row("screen", "single25", "baseline", trial.SEEDS["screen"][0])]
    trial.base.write_json(tmp_path / "execution_notes.json", {"aborted_runs": [{"stage": "screen", "case_id": "single25",
        "variant": "strings", "seed": trial.SEEDS["screen"][0], "process_elapsed_seconds": 7.}]})
    budget = audit._budget(tmp_path, rows, 115.405)
    assert budget["batch_seconds"] == 16
    assert budget["total_seconds"] == 131.405
    assert budget["budget_compliant"]
    assert not audit._budget(tmp_path, rows, 860)["reserve_preserved"]


def test_candidate_configuration_requires_previous_features_disabled():
    row = _row("screen", "fleet200", "strings", trial.SEEDS["screen"][0])
    row["construction_seconds"] = 1.
    row["config"] = {"max_iterations": 10_000_000, "time_limit_seconds": 8., "seed": row["seed"], "candidate_limit": 48,
                     "relay_enabled": False, "relay_direct_warmup_fraction": .8, "enable_home_seed": False,
                     "enable_home_bias": False, "enable_multiroute_string_destroy": True}
    payload = {"metadata": {"seed": row["seed"], "time_limit_seconds": 8., "relay_enabled": False, "enable_home_seed": False,
                            "enable_multiroute_string_destroy": True, "enable_direct_option_cache": False, "enable_budget_cooling": False}}
    audit._configuration(row, payload)
    payload["metadata"]["enable_budget_cooling"] = True
    with pytest.raises(ValueError, match="enable_budget_cooling"):
        audit._configuration(row, payload)


def test_frozen_child_protocol_rejects_parent_seeds():
    manifest = {"schema_version": 6, "kind": "method_trials_iteration2", "baseline_variant": "baseline",
                "baseline_commit": trial.BASELINE_COMMIT, "input_path": trial.base.INPUT,
                "seeds": deepcopy(trial.SEEDS), "protocol": deepcopy(trial.PROTOCOL)}
    audit._manifest(manifest)
    manifest["seeds"]["screen"] = trial.first.SEEDS["screen"]
    with pytest.raises(ValueError, match="frozen protocol"):
        audit._manifest(manifest)
