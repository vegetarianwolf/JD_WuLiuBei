"""Immutable generic plan, multi-cohort cap and user-selected permissive gate."""
from dataclasses import FrozenInstanceError, replace

import pytest

from algorithm.experiments import run_method_iteration3 as trial

shared = trial.shared


def _rows(stage):
    result = []
    for index, seed in enumerate(trial.SEEDS[stage]):
        order = list(trial.VARIANTS) if index % 2 == 0 else list(reversed(trial.VARIANTS))
        for case in trial.CASES[stage]:
            for variant in order:
                result.append({"stage": stage, "case_id": case, "variant": variant, "seed": seed,
                    "order": order.index(variant), "budget_seconds": trial.BUDGETS[stage][case], "scenario": "pure_direct",
                    "initial_routes_sha256": "same", "task_count": 25 if case == "single25" else 200,
                    "drone_count": 1 if case == "single25" else 8, "valid": True, "budget_compliant": True,
                    "end_to_end_seconds": trial.BUDGETS[stage][case] - 1., "late_count": 70,
                    "total_lateness_min": 2200., "distance_km": 640.})
    return result


def test_third_plan_is_immutable_has_new_seeds_and_two_bound_prior_cohorts():
    with pytest.raises(FrozenInstanceError):
        trial.PLAN.flag = "changed"
    assert trial.PROTOCOL["allow_screen_regressions"] is True
    assert {name for name, _, _ in trial.PLAN.priors} == {"first", "strings"}
    assert trial.SEEDS == {"screen": [2026101701, 2026101702], "medium": [2026101711, 2026101712, 2026101713]}
    assert sum(trial.CAPS.values()) == 320


def test_user_one_win_screen_allows_fleet_and_single_regressions():
    rows = _rows("screen")
    fleet = [r for r in rows if r["variant"] == "polish" and r["case_id"] == "fleet200"]
    fleet[0]["late_count"], fleet[1]["late_count"] = 69, 99
    next(r for r in rows if r["variant"] == "polish" and r["case_id"] == "single25")["late_count"] = 95
    assert trial.screen_pass(rows)
    strict = replace(trial.PLAN, allow_screen_regressions=False)
    assert not shared.screen_pass(rows, plan=strict)
    fleet[0]["late_count"] = 70
    assert not trial.screen_pass(rows)


@pytest.mark.parametrize("bad_field", ["valid", "budget_compliant"])
def test_permissive_gate_still_requires_every_prespecified_pair(bad_field):
    rows = _rows("screen")
    next(r for r in rows if r["variant"] == "polish" and r["case_id"] == "fleet200")["late_count"] = 69
    assert trial.screen_pass(rows)
    rows[-1][bad_field] = False
    assert not trial.screen_pass(rows)
    rows[-1][bad_field] = True
    rows.pop()
    assert not trial.screen_pass(rows)


def test_shared_stage_reserves_all_three_cohorts_before_execution(monkeypatch, tmp_path):
    shared.base.write_json(tmp_path / "runs.json", [])
    monkeypatch.setattr(shared, "verify", lambda *_args, **_kwargs: None)
    calls = []
    monkeypatch.setattr(shared.base, "invoke", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(RuntimeError, match="全部剩余"):
        shared.stage(tmp_path, {"prior_bindings": {"prior_experiment_seconds": 631.}}, "medium", plan=trial.PLAN)
    assert calls == []


def test_all_validation_seeds_run_after_regression(monkeypatch, tmp_path):
    shared.base.write_json(tmp_path / "runs.json", [])
    monkeypatch.setattr(shared, "verify", lambda *_args, **_kwargs: None)
    cells = {(r["stage"], r["case_id"], r["variant"], r["seed"]): r for r in _rows("medium")}
    calls = []
    def invoke(output, manifest, variant, case, seed, stage, budget):
        cell = (stage, case, variant, seed)
        calls.append(cell)
        row = cells[cell]
        row["late_count"] = 95 if variant == "polish" else 70
        return row
    monkeypatch.setattr(shared.base, "invoke", invoke)
    rows = shared.stage(tmp_path, {"prior_bindings": {"prior_experiment_seconds": 187.}}, "medium", plan=trial.PLAN)
    assert len(rows) == len(calls) == 12
    assert {r["seed"] for r in rows} == set(trial.SEEDS["medium"])
    assert not trial.medium_pass(rows)


def test_independent_audit_accepts_permissive_screen_then_majority_with_loss(monkeypatch, tmp_path):
    rows = [*_rows("screen"), *_rows("medium")]
    for row in rows:
        if row["variant"] == "polish" and row["case_id"] == "fleet200":
            row["late_count"] = 95 if row["seed"] in {trial.SEEDS["screen"][-1], trial.SEEDS["medium"][-1]} else 69
    shared.base.write_json(tmp_path / "decisions.json", [
        {"stage": "screen", "candidates": {"polish": True}, "eligible_variants": ["polish"]},
        {"stage": "medium", "selected_variant": "polish", "passed": True, "skipped": False,
         "candidates": {"polish": True}, "continuation_after_regression": True}])
    monkeypatch.setattr(shared, "screen_pass", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("runner gate called")))
    decision = shared._audit_decisions(tmp_path, rows, trial.PLAN)
    assert decision["default_eligible"] and decision["protocol_complete"]


def test_shared_budget_counts_priors_once_and_keeps_reserve(tmp_path):
    rows = _rows("screen")
    prior = {"prior_experiment_seconds": 187.}
    budget = shared._audit_budget(tmp_path, rows, prior, trial.PLAN)
    assert budget["batch_seconds"] == 72.
    assert budget["prior_seconds"] == 187.
    assert budget["total_seconds"] == 259.
    assert budget["reserve_preserved"]
    assert not shared._audit_budget(tmp_path, rows, {"prior_experiment_seconds": 800.}, trial.PLAN)["reserve_preserved"]


def test_cohort_binding_reads_each_audited_batch_not_its_shared_total(monkeypatch, tmp_path):
    output = tmp_path / "iteration3"
    output.mkdir()
    for name, relative, kind in trial.PLAN.priors:
        root = (output / relative).resolve()
        root.mkdir(exist_ok=True)
        manifest = {"kind": kind, "baseline_commit": trial.BASELINE_COMMIT, "source_snapshots": {}, "execution_sources": {}}
        elapsed = 115. if name == "first" else 72.
        for key, document in (("manifest", manifest), ("runs", [{"end_to_end_seconds": elapsed, "solution_file": "solution.json", "solution_sha256": "solutionhash"}]), ("decisions", [])):
            shared.base.write_json(root / f"{key}.json", document)
        validation = {"passed": True, "protocol_complete": True, "decision_audit": {"selected_variant": None},
                      "cumulative_budget": {"total_seconds": elapsed if name == "first" else 187., **({"batch_seconds": elapsed} if name == "strings" else {})},
                      **{f"{key}_sha256": shared.base.sha256(root / f"{key}.json") for key in ("manifest", "runs", "decisions")}}
        shared.base.write_json(root / "validation.json", validation)
    real_sha = shared.base.sha256
    monkeypatch.setattr(shared.base, "sha256", lambda path: "solutionhash" if path.name == "solution.json" else real_sha(path))
    binding = shared.cohort_evidence(output, trial.PLAN)
    assert binding["prior_experiment_seconds"] == 187.
    assert set(binding["cohorts"]) == {"first", "strings"}
