"""Predeclared objective-trial gates and resume behavior; never run a solver."""

from copy import deepcopy

import pytest

from algorithm.experiments import run_objective_trials as trial


def _row(stage, case, variant, seed, score=(70, 2200.0, 640.0)):
    tasks, drones = (25, 1) if case == "single25" else (200, 8)
    return {
        "stage": stage, "case_id": case, "variant": variant, "seed": seed,
        "valid": True, "budget_compliant": True, "scenario": "pure_direct",
        "task_count": tasks, "drone_count": drones,
        "budget_seconds": trial.BUDGETS[stage][case],
        "end_to_end_seconds": trial.BUDGETS[stage][case] - 1,
        "initial_routes_sha256": "same-initial",
        "late_count": score[0], "total_lateness_min": score[1],
        "distance_km": score[2],
    }


def _stage_rows(stage, variant="boundary", fleet_scores=None, single_scores=None):
    rows = []
    for index, seed in enumerate(trial.SEEDS[stage]):
        for case in trial.CASES[stage]:
            scores = single_scores if case == "single25" else fleet_scores
            rows.append(_row(stage, case, "baseline", seed))
            rows.append(_row(stage, case, variant, seed,
                             scores[index] if scores else (69, 2200, 640)))
    return rows


def test_screen_allows_one_loss_when_primary_total_is_not_worse():
    rows = _stage_rows("screen", fleet_scores=[(68, 2400, 700), (72, 2000, 600)])
    assert trial.screen_pass(rows, "boundary")
    assert not trial.screen_pass(rows, "boundary", equivalent=False)
    for row in rows:
        if row["case_id"] == "fleet200" and row["variant"] == "boundary" and row["seed"] == trial.SEEDS["screen"][1]:
            row["late_count"] = 73
    assert not trial.screen_pass(rows, "boundary")


def test_screen_requires_a_win_and_single_drone_non_regression():
    rows = _stage_rows("screen", fleet_scores=[(70, 2200, 640)] * 2)
    assert not trial.screen_pass(rows, "boundary")
    rows = _stage_rows("screen", single_scores=[(70, 2201, 600), (70, 2200, 640)])
    assert not trial.screen_pass(rows, "boundary")


@pytest.mark.parametrize("invalidity", ["missing", "invalid", "over_budget"])
def test_incomplete_or_invalid_pairs_never_pass(invalidity):
    rows = _stage_rows("screen")
    if invalidity == "missing":
        rows.pop()
    elif invalidity == "invalid":
        rows[-1]["valid"] = False
    else:
        rows[-1]["budget_compliant"] = False
    assert not trial.complete(rows, "screen", "boundary")
    assert not trial.screen_pass(rows, "boundary")


def test_medium_requires_two_wins_and_no_pair_with_more_late_tasks():
    rows = _stage_rows("medium", fleet_scores=[(69, 2500, 700), (70, 2199, 680), (70, 2400, 690)])
    assert trial.medium_pass(rows, "boundary")
    rows[-1]["late_count"] = 71
    assert not trial.medium_pass(rows, "boundary")
    rows = _stage_rows("medium", fleet_scores=[(69, 2500, 700), (70, 2200, 640), (70, 2200, 640)])
    assert not trial.medium_pass(rows, "boundary")


def test_selection_prefers_more_wins_then_primary_gain_then_boundary_tie():
    boundary = _stage_rows("medium", "boundary", fleet_scores=[(69, 2200, 640)] * 3)
    objective = [r for r in _stage_rows("medium", "objective", fleet_scores=[(68, 2200, 640)] * 3)
                 if r["variant"] != "baseline"]
    rows = boundary + objective
    assert trial.choose_candidate(rows, ["boundary", "objective"]) == "objective"
    for row in objective:
        if row["case_id"] == "fleet200":
            row["late_count"] = 69
    assert trial.choose_candidate(rows, ["objective", "boundary"]) == "boundary"
    objective[-1]["late_count"] = 70
    objective[-1]["total_lateness_min"] = 2200
    objective[-1]["distance_km"] = 640
    objective[1]["late_count"] = 65
    assert trial.choose_candidate(rows, ["boundary", "objective"]) == "boundary"


def test_confirmation_requires_medium_four_pairs_and_two_new_seed_wins():
    medium = _stage_rows("medium")
    confirm = _stage_rows("confirm", fleet_scores=[(69, 2200, 640), (69, 2200, 640), (70, 2200, 640), (70, 2200, 640)])
    assert not trial.confirm_pass(medium + confirm, "boundary")  # historical win is not new.
    confirm[-1]["late_count"] = 69
    assert trial.confirm_pass(medium + confirm, "boundary")
    assert not trial.confirm_pass(confirm, "boundary")
    assert not trial.confirm_pass((medium + confirm)[:-2], "boundary")
    confirm[1]["late_count"] = 71
    assert not trial.confirm_pass(medium + confirm, "boundary")


def _mock_stage(monkeypatch, tmp_path, rows=None):
    trial.base.write_json(tmp_path / "runs.json", rows or [])
    monkeypatch.setattr(trial, "verify", lambda *_: None)
    calls = []

    def invoke(output, manifest, variant, case, seed, stage, budget, *, iterations=None):
        calls.append((stage, case, variant, seed, iterations))
        # Every candidate deliberately loses: confirmation must still finish.
        return _row(stage, case, variant, seed,
                    (71, 2300, 650) if variant != "baseline" else (70, 2200, 640))

    monkeypatch.setattr(trial.base, "invoke", invoke)
    return calls


def test_confirmation_runs_all_four_seeds_despite_first_and_every_regression(monkeypatch, tmp_path):
    calls = _mock_stage(monkeypatch, tmp_path)
    rows = trial.stage(tmp_path, {}, "confirm", ["baseline", "boundary"])
    assert len(calls) == len(rows) == 8
    assert {r["seed"] for r in rows} == set(trial.SEEDS["confirm"])
    assert len(trial.pairs(rows, "confirm", "boundary")) == 4
    assert [call[2] for call in calls] == [
        "baseline", "boundary", "boundary", "baseline",
        "baseline", "boundary", "boundary", "baseline",
    ]
    assert not (tmp_path / "execution_notes.json").exists()


def test_resume_skips_completed_cells_without_changing_saved_rows(monkeypatch, tmp_path):
    saved = [_row("confirm", "fleet200", "baseline", trial.SEEDS["confirm"][0])]
    original = deepcopy(saved[0])
    calls = _mock_stage(monkeypatch, tmp_path, saved)
    rows = trial.stage(tmp_path, {}, "confirm", ["baseline", "boundary"])
    assert len(calls) == 7
    assert rows[0] == original
    calls.clear()
    assert trial.stage(tmp_path, {}, "confirm", ["baseline", "boundary"]) == rows
    assert not calls


def test_resume_never_reruns_an_aborted_cell(monkeypatch, tmp_path):
    calls = _mock_stage(monkeypatch, tmp_path)
    trial.base.write_json(tmp_path / "execution_notes.json", {"aborted_runs": [{
        "stage": "confirm", "case_id": "fleet200", "variant": "baseline",
        "seed": trial.SEEDS["confirm"][0], "process_elapsed_seconds": 8,
    }]})
    with pytest.raises(RuntimeError, match="中止"):
        trial.stage(tmp_path, {}, "confirm", ["baseline", "boundary"])
    assert calls == []


def test_resume_preserves_orphan_solution_without_rerunning_cell(monkeypatch, tmp_path):
    calls = _mock_stage(monkeypatch, tmp_path)
    orphan = tmp_path / "run_solutions" / f"confirm__fleet200__baseline__{trial.SEEDS['confirm'][0]}.json"
    trial.base.write_json(orphan, {"unfinished_registration": "preserve-evidence"})
    original = orphan.read_bytes()
    with pytest.raises(RuntimeError, match="孤立解"):
        trial.stage(tmp_path, {}, "confirm", ["baseline", "boundary"])
    assert calls == []
    assert orphan.read_bytes() == original


def test_aborted_process_time_counts_against_stage_cap(monkeypatch, tmp_path):
    calls = _mock_stage(monkeypatch, tmp_path)
    trial.base.write_json(tmp_path / "execution_notes.json", {"aborted_runs": [{
        "stage": "confirm", "case_id": "fleet200", "variant": "objective",
        "seed": 999, "process_elapsed_seconds": trial.CAPS["confirm"] - 239,
    }]})
    with pytest.raises(RuntimeError, match="预算"):
        trial.stage(tmp_path, {}, "confirm", ["baseline", "boundary"])
    assert calls == []


def test_reserved_total_budget_is_enforced_before_starting_another_run(monkeypatch, tmp_path):
    prior = _row("screen", "fleet200", "baseline", trial.SEEDS["screen"][0])
    prior["end_to_end_seconds"] = trial.TOTAL_SECONDS - trial.RESERVED_SECONDS - 239
    calls = _mock_stage(monkeypatch, tmp_path, [prior])
    with pytest.raises(RuntimeError, match="预算"):
        trial.stage(tmp_path, {}, "confirm", ["baseline", "boundary"])
    assert calls == []


def test_equivalence_stage_requests_exactly_200_iterations_and_rotates_three_versions(monkeypatch, tmp_path):
    calls = _mock_stage(monkeypatch, tmp_path)
    trial.stage(tmp_path, {}, "equivalence", list(trial.VARIANTS))
    assert len(calls) == 9
    assert all(call[-1] == 200 for call in calls)
    assert [call[2] for call in calls] == [
        "baseline", "boundary", "objective",
        "objective", "boundary", "baseline",
        "boundary", "objective", "baseline",
    ]


def test_pairing_rejects_changed_initial_solution_or_duplicate_cell():
    rows = _stage_rows("screen")
    rows[1]["initial_routes_sha256"] = "different"
    with pytest.raises(ValueError, match="配对条件"):
        trial.pairs(rows, "screen", "boundary", "single25")
    rows[1]["initial_routes_sha256"] = "same-initial"
    rows.append(deepcopy(rows[1]))
    with pytest.raises(ValueError, match="重复"):
        trial.pairs(rows, "screen", "boundary", "single25")
