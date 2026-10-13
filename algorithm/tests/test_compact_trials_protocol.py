"""Compact-trial scheduling and evidence protection without executing a solver."""
from pathlib import Path

import pytest

from algorithm.experiments import run_compact_trials as trial


def _row(stage, case, variant, seed, *, late=70):
    tasks, drones = (25, 1) if case == "single25" else (200, 8)
    return {
        "stage": stage, "case_id": case, "variant": variant, "seed": seed,
        "valid": True, "budget_compliant": True, "scenario": "pure_direct",
        "task_count": tasks, "drone_count": drones,
        "budget_seconds": trial.BUDGETS[stage][case],
        "end_to_end_seconds": trial.BUDGETS[stage][case] - 1,
        "initial_routes_sha256": "same-initial", "late_count": late,
        "total_lateness_min": 2200., "distance_km": 640.,
    }


def _mock_stage(monkeypatch, tmp_path, *, parent_seconds=376.):
    output = tmp_path / "compact_trial"
    output.mkdir()
    trial.base.write_json(tmp_path / "runs.json", [{"end_to_end_seconds": parent_seconds}])
    trial.base.write_json(output / "runs.json", [])
    monkeypatch.setattr(trial, "verify", lambda *_: None)
    calls = []

    def invoke(output, manifest, variant, case, seed, stage, budget, *, iterations=None):
        calls.append((stage, case, variant, seed, iterations))
        return _row(stage, case, variant, seed, late=71 if variant == "compact" else 70)

    monkeypatch.setattr(trial.base, "invoke", invoke)
    return output, calls


def test_confirmation_runs_all_four_pairs_despite_all_candidate_losses(monkeypatch, tmp_path):
    output, calls = _mock_stage(monkeypatch, tmp_path)
    rows = trial.stage(output, {}, "confirm")
    assert len(rows) == len(calls) == 8
    assert len(trial.pairs(rows, "confirm")) == 4
    assert [c[2] for c in calls] == ["baseline", "compact", "compact", "baseline",
                                   "baseline", "compact", "compact", "baseline"]
    assert {c[3] for c in calls} == set(trial.SEEDS["confirm"])
    assert trial.consumed(output) == 376 + 8 * 239


def test_whole_stage_is_reserved_from_parent_shared_budget(monkeypatch, tmp_path):
    output, calls = _mock_stage(monkeypatch, tmp_path, parent_seconds=721.)
    with pytest.raises(RuntimeError, match="全部剩余阶段"):
        trial.stage(output, {}, "confirm")
    assert calls == []


def test_parent_abort_time_is_included_in_shared_budget(monkeypatch, tmp_path):
    output, calls = _mock_stage(monkeypatch, tmp_path, parent_seconds=700.)
    trial.base.write_json(tmp_path / "execution_notes.json", {
        "aborted_runs": [{"process_elapsed_seconds": 21.}],
    })
    with pytest.raises(RuntimeError, match="共享45分钟"):
        trial.stage(output, {}, "confirm")
    assert calls == []


def test_completed_resume_reserves_only_missing_cells(monkeypatch, tmp_path):
    output, calls = _mock_stage(monkeypatch, tmp_path, parent_seconds=500.)
    saved = [_row("confirm", "fleet200", "baseline", trial.SEEDS["confirm"][0])]
    trial.base.write_json(output / "runs.json", saved)
    rows = trial.stage(output, {}, "confirm")
    assert len(calls) == 7
    assert rows[0] == saved[0]
    calls.clear()
    assert trial.stage(output, {}, "confirm") == rows
    assert calls == []


def test_stage_refuses_to_overwrite_orphan_solution(monkeypatch, tmp_path):
    output, calls = _mock_stage(monkeypatch, tmp_path)
    orphan = output / "run_solutions" / f"screen__single25__baseline__{trial.SEEDS['screen'][0]}.json"
    trial.base.write_json(orphan, {"evidence": "keep"})
    before = orphan.read_bytes()
    with pytest.raises(RuntimeError, match="孤立解"):
        trial.stage(output, {}, "screen")
    assert calls == []
    assert orphan.read_bytes() == before


def test_aborted_cell_is_never_retried(monkeypatch, tmp_path):
    output, calls = _mock_stage(monkeypatch, tmp_path)
    trial.base.write_json(output / "execution_notes.json", {"aborted_runs": [{
        "stage": "screen", "case_id": "single25", "variant": "baseline",
        "seed": trial.SEEDS["screen"][0], "process_elapsed_seconds": 0,
    }]})
    with pytest.raises(RuntimeError, match="中止"):
        trial.stage(output, {}, "screen")
    assert calls == []


def test_single25_destroy_counts_are_mathematically_identical():
    for fractions in ((.03, .08), (.02, .05)):
        lower = max(2, round(25 * fractions[0]))
        upper = max(lower, round(25 * fractions[1]))
        assert (lower, upper) == (2, 2)
    assert tuple(max(2, round(200 * x)) for x in (.03, .08)) == (6, 16)
    assert tuple(max(2, round(200 * x)) for x in (.02, .05)) == (4, 10)


def test_equivalence_requires_all_200_iteration_fingerprints_to_match():
    rows = []
    for seed in trial.SEEDS["equivalence"]:
        for variant in trial.VARIANTS:
            row = _row("equivalence", "single25", variant, seed)
            row.update(iterations=200, decision_fingerprint="same")
            rows.append(row)
    assert trial.equivalence_pass(rows)
    rows[-1]["decision_fingerprint"] = "different"
    assert not trial.equivalence_pass(rows)
    rows[-1]["decision_fingerprint"] = "same"
    rows[-1]["iterations"] = 199
    assert not trial.equivalence_pass(rows)


@pytest.fixture
def prepared(monkeypatch, tmp_path):
    # Hash-real but solver-free archives: this fixture never extracts or runs.
    parent_source = tmp_path / "sources" / "baseline.zip"
    parent_source.parent.mkdir()
    parent_source.write_bytes(b"frozen-source")
    parent_manifest = {
        "input_path": trial.base.INPUT, "input_sha256": trial.base.sha256(trial.ROOT / trial.base.INPUT),
        "source_snapshots": {"baseline": {
            "path": "sources/baseline.zip", "sha256": trial.base.sha256(parent_source),
        }},
    }
    trial.base.write_json(tmp_path / "manifest.json", parent_manifest)
    trial.base.write_json(tmp_path / "runs.json", [])
    trial.base.write_json(tmp_path / "decisions.json", [])
    monkeypatch.setattr(trial, "parent_evidence", lambda _: (parent_manifest, 0.))
    output = tmp_path / "compact_trial"
    manifest = trial.prepare(output)
    trial.verify(output, manifest)
    return output, manifest


def test_prepare_uses_one_identical_parent_baseline_snapshot(prepared):
    output, manifest = prepared
    baseline, compact = (manifest["source_snapshots"][v] for v in trial.VARIANTS)
    assert baseline["path"] == compact["path"] == "sources/baseline.zip"
    assert baseline["sha256"] == compact["sha256"]
    assert baseline["source_ref"] == compact["source_ref"] == trial.BASELINE_COMMIT
    assert manifest["schema_version"] == 4
    assert manifest["kind"] == "compact_trials"
    assert manifest["post_hoc_exploration"]["parent_directory"] == ".."
    assert set(manifest["execution_sources"]) == {
        "run_optimization_experiments", "run_scenario_comparison", "run_compact_trials",
    }
    assert trial.base.read_json(output / "runs.json") == []


@pytest.mark.parametrize("parent_file", ["manifest", "runs", "decisions"])
def test_resume_rejects_changed_parent_evidence(prepared, parent_file):
    output, manifest = prepared
    path = output.parent / f"{parent_file}.json"
    path.write_bytes(path.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="父批次证据改变"):
        trial.verify(output, manifest)


def test_resume_rejects_new_parent_abort_notes(prepared):
    output, manifest = prepared
    trial.base.write_json(output.parent / "execution_notes.json", {"aborted_runs": []})
    with pytest.raises(ValueError, match="中止记录"):
        trial.verify(output, manifest)


def test_resume_rejects_solver_archive_change(prepared):
    output, manifest = prepared
    (output / "sources/baseline.zip").write_bytes(b"changed")
    with pytest.raises(ValueError, match="默认求解器快照"):
        trial.verify(output, manifest)


def test_resume_rejects_any_unregistered_solution(prepared):
    output, manifest = prepared
    trial.base.write_json(output / "run_solutions/unknown.json", {"preserve": True})
    with pytest.raises(RuntimeError, match="孤立解"):
        trial.verify(output, manifest)


def test_resume_rejects_changed_experimental_flags(prepared):
    output, manifest = prepared
    manifest["source_snapshots"]["compact"]["config"] = {"min_destroy_fraction": .01, "max_destroy_fraction": .05}
    with pytest.raises(ValueError, match="固定追加实验协议"):
        trial.verify(output, manifest)
