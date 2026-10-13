"""Admission gates cannot turn failed or unpaired experiments into improvements."""
from copy import deepcopy

import pytest

from algorithm.experiments.run_optimization_experiments import quality_gate, paired, score


def rows(stage="screen", candidate="allocation"):
    seeds = [2026101301, 2026101302] if stage == "screen" else [2026101311, 2026101312, 2026101313]
    return [dict(stage=stage, case_id=case, seed=seed, variant=variant,
                 valid=True, budget_compliant=True, late_count=10 if variant == "baseline" else 9,
                 total_lateness_min=30., distance_km=80., budget_seconds=5 if case == "single25" else 10,
                 initial_routes_sha256="a" * 64, scenario="pure_direct",
                 task_count=25 if case == "single25" else 200, drone_count=1 if case == "single25" else 8)
            for case in ("single25", "fleet200") for seed in seeds for variant in ("baseline", candidate)]


def test_initial_gate_requires_complete_pairs_and_does_not_accept_distance_over_lateness():
    data = rows()
    assert quality_gate(data, "screen", "allocation")
    bad = deepcopy(data)
    row = next(r for r in bad if r["case_id"] == "fleet200" and r["variant"] == "allocation")
    row.update(late_count=11, total_lateness_min=0, distance_km=1)
    assert not quality_gate(bad, "screen", "allocation")
    row["budget_compliant"] = False
    assert not quality_gate(bad, "screen", "allocation")
    assert len(paired(bad, "screen", "allocation", "fleet200")) == 1


def test_medium_gate_rejects_single_drone_lateness_regression():
    data = rows("medium")
    assert quality_gate(data, "medium", "allocation")
    next(r for r in data if r["case_id"] == "single25" and r["variant"] == "allocation")["late_count"] = 11
    assert not quality_gate(data, "medium", "allocation")


def test_medium_gate_requires_two_fleet_wins():
    data = rows("medium")
    fleet = [r for r in data if r["case_id"] == "fleet200" and r["variant"] == "allocation"]
    for row in fleet[:2]:
        row["late_count"] = 10
    assert not quality_gate(data, "medium", "allocation")
    assert score(fleet[0]) == (10, 30., 80.)


def test_gate_rejects_replacement_seeds_even_with_a_complete_sample():
    data = rows()
    for row in data:
        row["seed"] += 100
    assert not quality_gate(data, "screen", "allocation")


def test_complete_pairset_used_by_fast_gate_requires_exact_seeds():
    from algorithm.experiments.run_optimization_experiments import complete_pairset
    data = rows("medium", "fast")
    assert complete_pairset(data, "medium", "fast")
    for row in data:
        row["seed"] += 100
    assert not complete_pairset(data, "medium", "fast")


@pytest.mark.parametrize("evidence", ["completed_solution", "aborted"])
def test_incomplete_equivalence_is_not_rerun(tmp_path, monkeypatch, evidence):
    import algorithm.experiments.run_optimization_experiments as runner
    monkeypatch.setattr(runner, "verify_execution_files", lambda manifest: None)
    def forbidden(*args, **kwargs):
        pytest.fail("incomplete equivalence must not invoke another solve")
    monkeypatch.setattr(runner, "invoke", forbidden)
    if evidence == "completed_solution":
        runner.write_json(tmp_path / "run_solutions/equivalence__single25__baseline__1.json", {})
    else:
        runner.write_json(tmp_path / "execution_notes.json", {"aborted_runs": [{"stage": "equivalence"}]})
    with pytest.raises(RuntimeError, match="禁止重跑"):
        runner.equivalence(tmp_path, {})


@pytest.mark.parametrize("field,value", [("initial_routes_sha256", "b" * 64),
                                         ("budget_seconds", 11), ("drone_count", 9)])
def test_pairing_rejects_different_initial_conditions(field, value):
    data = rows()
    next(r for r in data if r["case_id"] == "fleet200" and r["variant"] == "allocation")[field] = value
    with pytest.raises(ValueError, match="配对"):
        paired(data, "screen", "allocation", "fleet200")


def test_pairing_rejects_duplicate_run_cells():
    data = rows()
    data.append(dict(next(r for r in data if r["case_id"] == "fleet200")))
    with pytest.raises(ValueError, match="重复"):
        paired(data, "screen", "allocation", "fleet200")


def test_confirmation_stops_after_the_first_irrecoverable_regression():
    from algorithm.experiments.run_optimization_experiments import confirmation_regressed
    data = rows()
    for row in data:
        row["stage"] = "confirm"
    assert not confirmation_regressed(data, "allocation")
    next(r for r in data if r["case_id"] == "fleet200" and r["variant"] == "allocation").update(
        late_count=11, total_lateness_min=0, distance_km=1)
    assert confirmation_regressed(data, "allocation")


def test_resume_rejects_changed_runner_and_scenario_helper(tmp_path, monkeypatch):
    import algorithm.experiments.run_optimization_experiments as runner
    fake_runner = tmp_path / "runner.py"
    helper = tmp_path / "algorithm/experiments/run_scenario_comparison.py"
    helper.parent.mkdir(parents=True)
    fake_runner.write_text("runner")
    helper.write_text("helper")
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    monkeypatch.setattr(runner, "__file__", str(fake_runner))
    manifest = {"runner_sha256": runner.sha256(fake_runner),
                "execution_sources": {"run_scenario_comparison": {"sha256": runner.sha256(helper)}}}
    runner.verify_execution_files(manifest)
    fake_runner.write_text("changed")
    with pytest.raises(ValueError, match="运行器"):
        runner.verify_execution_files(manifest)
    fake_runner.write_text("runner")
    helper.write_text("changed")
    with pytest.raises(ValueError, match="场景"):
        runner.verify_execution_files(manifest)
