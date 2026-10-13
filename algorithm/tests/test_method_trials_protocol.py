"""Budget, no-selection-bias and independent-gate tests; never run ALNS."""
from copy import deepcopy
from zipfile import ZipFile

import pytest

from algorithm.experiments import audit_method_trials as audit
from algorithm.experiments import run_method_trials as trial


def _row(stage, case, variant, seed, *, late=70, valid=True, compliant=True):
    tasks, drones = (25, 1) if case == "single25" else (200, 8)
    names = ["baseline", "memo"] if stage == "equivalence" else list(trial.VARIANTS)
    index = trial.SEEDS[stage].index(seed)
    order = names if index % 2 == 0 else list(reversed(names))
    return {"stage": stage, "case_id": case, "variant": variant, "seed": seed,
            "valid": valid, "budget_compliant": compliant, "scenario": "pure_direct",
            "task_count": tasks, "drone_count": drones, "budget_seconds": trial.BUDGETS[stage][case],
            "end_to_end_seconds": trial.BUDGETS[stage][case] - 1., "initial_routes_sha256": "same",
            "late_count": late, "total_lateness_min": 2200., "distance_km": 640.,
            "iterations": trial.EQUIVALENCE_ITERATIONS, "order": order.index(variant)}


def _rows(stage, variants=None):
    variants = variants or (["baseline", "memo"] if stage == "equivalence" else list(trial.VARIANTS))
    return [_row(stage, case, variant, seed) for seed in trial.SEEDS[stage]
            for case in trial.CASES[stage] for variant in variants]


def _mock_stage(monkeypatch, tmp_path):
    trial.base.write_json(tmp_path / "runs.json", [])
    monkeypatch.setattr(trial, "verify", lambda *_: None)
    calls = []
    def invoke(output, manifest, variant, case, seed, stage, budget, *, iterations=None):
        calls.append((stage, case, variant, seed, iterations))
        return _row(stage, case, variant, seed, late=80 if variant != "baseline" else 70)
    monkeypatch.setattr(trial.base, "invoke", invoke)
    return calls


def test_full_prespecified_design_fits_900_second_cap_with_audit_reserve():
    planned = sum(len(trial.SEEDS[s]) * sum(trial.BUDGETS[s].values()) * n
                  for s, n in (("equivalence", 2), ("screen", 3), ("medium", 3)))
    assert planned == 560
    assert planned <= trial.TOTAL_SECONDS - trial.RESERVED_SECONDS
    assert sum(trial.CAPS.values()) == planned
    assert len({seed for seeds in trial.SEEDS.values() for seed in seeds}) == 7


def test_screen_requires_fleet_improvement_and_no_regression_in_either_case():
    rows = _rows("screen")
    assert not trial.screen_pass(rows, "memo")
    next(r for r in rows if r["case_id"] == "fleet200" and r["variant"] == "memo")["late_count"] -= 1
    assert trial.screen_pass(rows, "memo")
    assert not trial.screen_pass(rows, "memo", equivalent=False)
    next(r for r in rows if r["case_id"] == "single25" and r["variant"] == "memo")["distance_km"] += 1.
    assert not trial.screen_pass(rows, "memo")


def test_exploratory_majority_accepts_two_wins_and_one_large_loss():
    rows = _rows("medium")
    fleet = [r for r in rows if r["case_id"] == "fleet200" and r["variant"] == "memo"]
    for row, late in zip(fleet, (69, 69, 90)):
        row["late_count"] = late
    assert trial.medium_pass(rows, "memo")
    # Compatibility is displayed, but the user chose a fleet-majority rule.
    next(r for r in rows if r["case_id"] == "single25" and r["variant"] == "memo")["late_count"] = 90
    assert trial.medium_pass(rows, "memo")
    assert trial.choose_candidate(rows, ["memo"]) == "memo"
    rows.remove(fleet[-1])
    assert not trial.medium_pass(rows, "memo")


@pytest.mark.parametrize("field", ["valid", "budget_compliant"])
def test_majority_requires_complete_valid_on_budget_evidence(field):
    rows = _rows("medium")
    for row in rows:
        if row["variant"] == "memo" and row["case_id"] == "fleet200":
            row["late_count"] -= 1
    assert trial.medium_pass(rows, "memo")
    next(r for r in rows if r["variant"] == "memo")[field] = False
    assert not trial.medium_pass(rows, "memo")


def test_election_prioritizes_wins_then_tardy_reduction_then_fixed_memo_tie():
    rows = _rows("medium")
    for row in rows:
        if row["variant"] != "baseline" and row["case_id"] == "fleet200":
            row["late_count"] -= 1
    assert trial.choose_candidate(rows, ["cooling", "memo"]) == "memo"
    next(r for r in rows if r["variant"] == "cooling" and r["case_id"] == "fleet200")["late_count"] -= 1
    assert trial.choose_candidate(rows, ["memo", "cooling"]) == "cooling"


def test_validation_completes_all_seeds_after_regression(monkeypatch, tmp_path):
    calls = _mock_stage(monkeypatch, tmp_path)
    rows = trial.stage(tmp_path, {}, "medium", list(trial.VARIANTS))
    assert len(rows) == len(calls) == 18
    assert all(len(trial.pairs(rows, "medium", v, case)) == 3 for v in ("memo", "cooling") for case in trial.CASES["medium"])
    assert [c[2] for c in calls[:9]] == ["baseline", "memo", "cooling", "baseline", "memo", "cooling", "cooling", "memo", "baseline"]
    assert not trial.medium_pass(rows, "memo")


def test_stage_reserves_every_missing_cell_before_any_execution(monkeypatch, tmp_path):
    calls = _mock_stage(monkeypatch, tmp_path)
    monkeypatch.setattr(trial, "experiment_seconds", lambda _: trial.TOTAL_SECONDS - trial.RESERVED_SECONDS - 359)
    with pytest.raises(RuntimeError, match="全部剩余"):
        trial.stage(tmp_path, {}, "medium", list(trial.VARIANTS))
    assert calls == []


def test_completed_resume_does_not_rerun_or_require_complete_stage_budget(monkeypatch, tmp_path):
    calls = _mock_stage(monkeypatch, tmp_path)
    rows = _rows("medium")
    trial.base.write_json(tmp_path / "runs.json", rows)
    assert trial.stage(tmp_path, {}, "medium", list(trial.VARIANTS)) == rows
    assert calls == []


def test_aborted_cell_and_orphan_solution_are_never_retried(monkeypatch, tmp_path):
    calls = _mock_stage(monkeypatch, tmp_path)
    cell = {"stage": "screen", "case_id": "single25", "variant": "baseline", "seed": trial.SEEDS["screen"][0],
            "process_elapsed_seconds": 0.}
    trial.base.write_json(tmp_path / "execution_notes.json", {"aborted_runs": [cell]})
    with pytest.raises(RuntimeError, match="中止"):
        trial.stage(tmp_path, {}, "screen", list(trial.VARIANTS))
    (tmp_path / "execution_notes.json").unlink()
    orphan = tmp_path / f"run_solutions/screen__single25__baseline__{trial.SEEDS['screen'][0]}.json"
    trial.base.write_json(orphan, {"preserve": "evidence"})
    before = orphan.read_bytes()
    with pytest.raises(RuntimeError, match="孤立"):
        trial.stage(tmp_path, {}, "screen", list(trial.VARIANTS))
    assert orphan.read_bytes() == before
    assert calls == []


def test_memo_equivalence_covers_both_cases_and_requires_all_fixed_iterations(monkeypatch, tmp_path):
    rows = _rows("equivalence")
    monkeypatch.setattr(trial, "_stable_search", lambda *_: {"same": True})
    assert trial.equivalence_pass(tmp_path, rows)
    rows[-1]["iterations"] -= 1
    assert not trial.equivalence_pass(tmp_path, rows)
    rows[-1]["iterations"] += 1
    monkeypatch.setattr(trial, "_stable_search", lambda _, r: {"same": r["variant"] == "baseline"})
    assert not trial.equivalence_pass(tmp_path, rows)


def test_equivalence_rebuild_includes_all_nine_current_main_decision_counters(tmp_path):
    fields = ("uses", "accepted", "current_improvements", "best_improvements", "total_reward",
              "current_late_count_improvements", "best_late_count_improvements",
              "equal_score_accepts", "unchanged_route_accepts")
    stats = dict.fromkeys(fields, 0)
    row = {"solution_file": "run_solutions/equivalence.json", "iterations": 100,
           "best_trajectory": [[0, 1., 70, 2200., 640.]]}
    payload = {"routes": [{"encoded_visits": [1, -1]}], "score": {"late_count": 70}, "iterations": 100,
               "metadata": {"operator_statistics": {"random": stats}, "operator_weights": {"random": 1.},
                            "accepted_solutions": 0, "best_trajectory": [[0, 0., 70, 2200., 640.]]}}
    path = tmp_path / row["solution_file"]
    trial.base.write_json(path, payload)
    original = trial._stable_search(tmp_path, row)
    assert original == audit._stable_search(tmp_path, row)
    for field in fields:
        stats[field] = 1
        trial.base.write_json(path, payload)
        assert trial._stable_search(tmp_path, row) != original
        assert audit._stable_search(tmp_path, row) != original
        stats[field] = 0
    stats["option_cache_hits"] = 1
    trial.base.write_json(path, payload)
    assert trial._stable_search(tmp_path, row) == audit._stable_search(tmp_path, row) == original


def test_saved_stage_decisions_cannot_be_overwritten(tmp_path):
    trial.base.write_json(tmp_path / "decisions.json", [])
    trial._decision(tmp_path, "screen", passed=False)
    trial._decision(tmp_path, "screen", passed=False)
    with pytest.raises(ValueError, match="禁止覆盖"):
        trial._decision(tmp_path, "screen", passed=True)
    assert trial.base.read_json(tmp_path / "decisions.json") == [{"stage": "screen", "passed": False}]


def test_independent_audit_gate_matches_exploratory_policy_without_runner_calls(monkeypatch, tmp_path):
    rows = [*_rows("equivalence"), *_rows("screen"), *_rows("medium")]
    for row in rows:
        if row["case_id"] == "fleet200" and row["variant"] in {"memo", "cooling"} and row["stage"] != "equivalence":
            row["late_count"] = 90 if row["stage"] == "medium" and row["seed"] == trial.SEEDS["medium"][-1] else 69
    monkeypatch.setattr(audit, "_stable_search", lambda *_: {"same": True})
    monkeypatch.setattr(trial, "medium_pass", lambda *_: (_ for _ in ()).throw(AssertionError("runner gate called")))
    gates = audit._gates(tmp_path, rows)
    assert gates["screen"] == gates["medium"] == {"memo": True, "cooling": True}
    assert gates["selected_variant"] == "memo"
    assert gates["default_eligible"]
    decisions = [
        {"stage": "equivalence", "passed": True, "candidate": "memo", "planned_pairs": 4},
        {"stage": "screen", "candidates": gates["screen"], "eligible_variants": ["memo", "cooling"]},
        {"stage": "medium", "selected_variant": "memo", "passed": True, "skipped": False,
         "candidates": gates["medium"], "continuation_after_regression": True},
    ]
    trial.base.write_json(tmp_path / "decisions.json", decisions)
    assert audit._decision_audit(tmp_path, rows)["protocol_complete"]
    decisions[-1]["selected_variant"] = "cooling"
    trial.base.write_json(tmp_path / "decisions.json", decisions)
    with pytest.raises(ValueError, match="independent gate"):
        audit._decision_audit(tmp_path, rows)


def test_independent_audit_rejects_invalid_row_seed_budget_or_feature_metadata():
    row = _row("screen", "fleet200", "memo", trial.SEEDS["screen"][0])
    audit._row_protocol(row)
    changed = deepcopy(row)
    changed["seed"] = 2026101301
    with pytest.raises(ValueError, match="cell"):
        audit._row_protocol(changed)
    changed = deepcopy(row)
    changed["budget_seconds"] = 30
    with pytest.raises(ValueError, match="budget"):
        audit._row_protocol(changed)
    row["construction_seconds"] = 1.
    row["config"] = {"max_iterations": 10_000_000, "time_limit_seconds": 8., "seed": row["seed"],
                     "candidate_limit": 48, "relay_enabled": False, "relay_direct_warmup_fraction": .8,
                     "enable_home_seed": False, "enable_home_bias": False, "enable_direct_option_cache": True}
    payload = {"metadata": {"seed": row["seed"], "time_limit_seconds": 8., "relay_enabled": False,
                            "enable_home_seed": False, "enable_direct_option_cache": True}}
    audit._configuration(row, payload)
    payload["metadata"]["enable_direct_option_cache"] = False
    with pytest.raises(ValueError, match="metadata"):
        audit._configuration(row, payload)


def test_independent_budget_counts_aborted_time_and_rejects_duplicate_attempt(tmp_path):
    rows = [_row("screen", "single25", "baseline", trial.SEEDS["screen"][0])]
    abort = {"stage": "screen", "case_id": "single25", "variant": "memo", "seed": trial.SEEDS["screen"][0],
             "process_elapsed_seconds": 7.}
    trial.base.write_json(tmp_path / "execution_notes.json", {"aborted_runs": [abort]})
    assert audit._budget(tmp_path, rows)["total_seconds"] == 16.
    trial.base.write_json(tmp_path / "execution_notes.json", {"aborted_runs": [abort, abort]})
    with pytest.raises(ValueError, match="duplicated"):
        audit._budget(tmp_path, rows)


def test_frozen_manifest_rejects_budget_or_baseline_edit():
    manifest = {"schema_version": 5, "kind": "method_trials", "baseline_variant": "baseline",
                "baseline_commit": trial.BASELINE_COMMIT, "input_path": trial.base.INPUT,
                "seeds": deepcopy(trial.SEEDS), "protocol": deepcopy(trial.PROTOCOL)}
    audit._manifest(manifest)
    manifest["protocol"]["total_experiment_seconds"] += 1
    with pytest.raises(ValueError, match="frozen protocol"):
        audit._manifest(manifest)


@pytest.fixture
def synthetic_sources(monkeypatch, tmp_path):
    """Tiny hash-real fake modules; this never freezes the working solver."""
    sources = tmp_path / "sources"
    sources.mkdir()
    for name, data in (("baseline", b"baseline"), ("candidate", b"candidate")):
        with ZipFile(sources / f"{name}.zip", "w") as archive:
            archive.writestr("uav_dispatch/example.py", data)
    monkeypatch.setattr(audit.checks, "_git", lambda *args:
                        b"algorithm/src/uav_dispatch/example.py\n" if args[0] == "ls-tree" else b"baseline")
    snapshots = {}
    for variant, config in trial.VARIANTS.items():
        name = "baseline" if variant == "baseline" else "candidate"
        path = f"sources/{name}.zip"
        snapshots[variant] = {"path": path, "sha256": trial.base.sha256(tmp_path / path), "config": config,
                              "source_ref": trial.BASELINE_COMMIT if variant == "baseline" else "working_tree"}
    execution = {}
    for name in trial.EXECUTION_NAMES:
        target = sources / f"{name}.py"
        target.write_bytes((trial.ROOT / f"algorithm/experiments/{name}.py").read_bytes())
        execution[name] = {"path": str(target.relative_to(tmp_path)), "sha256": trial.base.sha256(target)}
    manifest = {"source_snapshots": snapshots, "execution_sources": execution,
                "runner_sha256": execution["run_optimization_experiments"]["sha256"]}
    assert audit._sources(tmp_path, manifest)[0]["memo"] == snapshots["memo"]["sha256"]
    return tmp_path, manifest


def test_audit_rejects_archive_tampering_and_disagrees_with_rehashed_baseline_git_bytes(synthetic_sources):
    output, manifest = synthetic_sources
    baseline = output / "sources/baseline.zip"
    with ZipFile(baseline, "w") as archive:
        archive.writestr("uav_dispatch/example.py", b"tampered baseline")
    with pytest.raises(ValueError, match="fingerprint"):
        audit._sources(output, manifest)
    manifest["source_snapshots"]["baseline"]["sha256"] = trial.base.sha256(baseline)
    with pytest.raises(ValueError, match="baseline Git bytes"):
        audit._sources(output, manifest)


def test_audit_rejects_archived_execution_helper_change(synthetic_sources):
    output, manifest = synthetic_sources
    helper = output / "sources/run_scenario_comparison.py"
    helper.write_bytes(helper.read_bytes() + b"\n# changed\n")
    with pytest.raises(ValueError, match="execution source changed"):
        audit._sources(output, manifest)
