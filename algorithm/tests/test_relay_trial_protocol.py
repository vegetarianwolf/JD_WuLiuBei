"""User-selected permissive gates still require valid, comparable full pairs."""
import pytest

from algorithm.experiments import run_relay_trials as trial


def _rows(stage):
    rows = []
    for seed in trial.SEEDS[stage]:
        for case in trial.CASES[stage]:
            for variant in trial.VARIANTS:
                rows.append(dict(stage=stage, case_id=case, variant=variant, seed=seed,
                    budget_seconds=trial.BUDGETS[stage][case], scenario=case.split("__")[-1],
                    task_count=200, drone_count=8, valid=True, budget_compliant=True,
                    initial_routes_sha256="initial", iterations=100,
                    decision_fingerprint="unchanged", late_count=80,
                    total_lateness_min=3000., distance_km=700.))
    return rows


def test_one_screen_win_expands_despite_another_loss():
    rows = _rows("compatibility") + _rows("screen")
    changed = [r for r in rows if r["stage"] == "screen" and r["variant"] == "diversity"
               and r["case_id"] == trial.PRIMARY]
    changed[0]["late_count"], changed[1]["late_count"] = 79, 99
    assert trial.screen_pass(rows, "diversity")
    assert trial.choose_candidate(rows) == "diversity"


@pytest.mark.parametrize("field", ["valid", "budget_compliant"])
def test_incomplete_or_invalid_prespecified_pairs_do_not_qualify(field):
    rows = _rows("compatibility") + _rows("screen")
    next(r for r in rows if r["stage"] == "screen" and r["variant"] == "risk")["late_count"] = 79
    assert trial.screen_pass(rows, "risk")
    target = next(r for r in rows if r["stage"] == "screen" and r["variant"] == "risk"
                  and r["case_id"] == trial.SECONDARY)
    target[field] = False
    assert not trial.screen_pass(rows, "risk")
    target[field] = True
    rows.remove(target)
    assert not trial.screen_pass(rows, "risk")


def test_mismatched_initial_solution_is_not_a_valid_pair():
    rows = _rows("screen")
    next(r for r in rows if r["variant"] == "checkpoint")["initial_routes_sha256"] = "changed"
    with pytest.raises(ValueError, match="initial_routes"):
        trial.pairs(rows, "screen", "checkpoint")


def test_default_gate_uses_primary_majority_and_keeps_secondary_visible():
    rows = _rows("medium")
    changed = [r for r in rows if r["variant"] == "diversity" and r["case_id"] == trial.PRIMARY]
    for i, row in enumerate(changed):
        row["late_count"] = 79 if i < 2 else 81
    assert trial.default_pass(rows, "diversity")
    rows.remove(next(r for r in rows if r["variant"] == "diversity" and r["case_id"] == trial.SECONDARY))
    assert not trial.default_pass(rows, "diversity")


def test_direct_compatibility_gate_requires_decision_equivalence():
    rows = _rows("compatibility")
    assert trial.compatibility_pass(rows, "checkpoint")
    next(r for r in rows if r["variant"] == "checkpoint")["decision_fingerprint"] = "changed"
    assert not trial.compatibility_pass(rows, "checkpoint")
