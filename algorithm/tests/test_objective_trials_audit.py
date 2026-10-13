"""Independent rescoring and malicious/accidental evidence mutation checks."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from algorithm.experiments import audit_objective_trials as audit
from uav_dispatch.alns import construct_edd_adjacent
from uav_dispatch.cli import result_payload
from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem
from uav_dispatch.search import SolverResult


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def baseline_sources():
    commit = "32c2d6055d89e831f0e8b783d1cac5c2c750fb1c"
    return commit, {
        name.removeprefix("algorithm/src/"): audit.checks._git("show", f"{commit}:{name}")
        for name in audit.checks._git("ls-tree", "-r", "--name-only", commit, "--", "algorithm/src/uav_dispatch").decode().splitlines()
        if name.endswith(".py")
    }


@pytest.fixture
def experiment(tmp_path, monkeypatch, baseline_sources):
    commit, files = baseline_sources
    protocol = SimpleNamespace(
        BASELINE_COMMIT=commit,
        VARIANTS={"baseline": {}, "boundary": {"enable_boundary_destroy": True},
                  "objective": {"enable_objective_rewards": True}},
        SEEDS={"equivalence": [2026101371, 2026101372, 2026101373],
               "screen": [2026101371, 2026101372], "medium": [2026101381, 2026101382, 2026101383],
               "confirm": [2026101321, 2026101391, 2026101392, 2026101393]},
        CASES={"equivalence": ["single25"], "screen": ["single25", "fleet200"],
               "medium": ["single25", "fleet200"], "confirm": ["fleet200"]},
        BUDGETS={"equivalence": {"single25": 10}, "screen": {"single25": 5, "fleet200": 10},
                 "medium": {"single25": 5, "fleet200": 30}, "confirm": {"fleet200": 240}},
        CAPS={"equivalence": 90, "screen": 90, "medium": 315, "confirm": 1920},
        TOTAL_SECONDS=2700, RESERVED_SECONDS=60,
    )
    monkeypatch.setattr(audit, "_protocol", lambda: protocol)
    archive = tmp_path / "baseline.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for name, content in files.items():
            package.writestr(name, content)
    fingerprint = audit.checks._sha256(archive)
    execution = {}
    for name in ("run_optimization_experiments", "run_scenario_comparison", "run_objective_trials"):
        path = tmp_path / f"{name}.py"
        path.write_text(f"# frozen {name}\n")
        execution[name] = {"path": path.name, "sha256": audit.checks._sha256(path)}
    manifest = {
        "schema_version": 3, "kind": "objective_trials", "baseline_commit": commit,
        "baseline_variant": "baseline", "input_path": str(audit.checks.DEFAULT_INPUT.relative_to(audit.checks.ROOT)),
        "input_sha256": audit.checks._sha256(audit.checks.DEFAULT_INPUT),
        "seeds": protocol.SEEDS,
        "source_snapshots": {name: {"path": archive.name, "sha256": fingerprint,
                                    "source_ref": commit if name == "baseline" else "working_tree", "config": flags}
                             for name, flags in protocol.VARIANTS.items()},
        "execution_sources": execution,
        "runner_sha256": execution["run_optimization_experiments"]["sha256"],
        "protocol": {"stage_cases": protocol.CASES, "stage_budgets_seconds": protocol.BUDGETS,
                     "stage_caps_seconds": protocol.CAPS, "total_experiment_seconds": 2700, "reserved_seconds": 60},
    }
    problem = Problem(tuple(load_tasks_csv(audit.checks.DEFAULT_INPUT)[:25]))
    initial = construct_edd_adjacent(problem)
    score = initial.evaluation.score
    trajectory = [[0, 0., score.late_count, score.total_lateness_min, score.distance_km]]
    result = SolverResult(initial.routes, initial.evaluation, .05, 0,
                          {"seed": 2026101371, "best_trajectory": trajectory, "time_limit_seconds": 3.9,
                           "relay_enabled": False, "enable_home_seed": False})
    payload = result_payload(problem, result, source=manifest["input_path"])
    write(tmp_path / "solution.json", payload)
    row = {
        "stage": "screen", "variant": "baseline", "case_id": "single25", "scenario": "pure_direct",
        "seed": 2026101371, "task_count": 25, "drone_count": 1, "budget_seconds": 5,
        "end_to_end_seconds": .3, "construction_seconds": .1, "search_seconds": .1,
        "validation_seconds": .01, "serialization_seconds": .01, "budget_compliant": True,
        "valid": True, "violations": [], **payload["score"], "on_time_count": 25 - score.late_count,
        "on_time_rate": payload["on_time_rate"], "iterations": 0,
        "best_trajectory": [[0, .1, *trajectory[0][2:]]], "solution_file": "solution.json",
        "solution_sha256": audit.checks._sha256(tmp_path / "solution.json"),
        "initial_routes_sha256": audit.checks._value_hash(initial.routes), "source_sha256": fingerprint,
        "config": {"max_iterations": 10_000_000, "time_limit_seconds": 3.9, "seed": 2026101371,
                   "candidate_limit": 48, "relay_enabled": False, "relay_direct_warmup_fraction": .8,
                   "enable_home_seed": False, "enable_home_bias": False},
    }
    bind_decisions(row, payload)
    write(tmp_path / "manifest.json", manifest)
    write(tmp_path / "runs.json", [row])
    return tmp_path, manifest, row, payload, protocol


def bind_decisions(row, payload):
    metadata = payload["metadata"]
    row["decision_fingerprint"] = audit.checks._value_hash({
        "routes": [route["encoded_visits"] for route in payload["routes"]], "score": payload["score"],
        "iterations": payload["iterations"], "trajectory": [[point[0], *point[2:]] for point in row["best_trajectory"]],
        "operator_statistics": metadata.get("operator_statistics", {}),
        "operator_weights": metadata.get("operator_weights", {}),
        "accepted_solutions": metadata.get("accepted_solutions"),
    })


def test_full_evaluator_and_bound_validation(experiment, monkeypatch):
    directory, *_ = experiment
    original = audit.evaluate_solution
    forced = []
    def evaluate(*args, **kwargs):
        forced.append(kwargs.get("_force_full"))
        return original(*args, **kwargs)
    monkeypatch.setattr(audit, "evaluate_solution", evaluate)
    report = audit.audit_results(directory)
    assert forced == [True]
    assert report["validated_rows"] == report["valid_on_budget_runs"] == 1
    assert report["evidence_verified"] and report["all_runs_successful"]
    assert report["manifest_sha256"] == audit.checks._sha256(directory / "manifest.json")
    assert report["runs_sha256"] == audit.checks._sha256(directory / "runs.json")


@pytest.mark.parametrize(("field", "value", "message"), [
    ("late_count", 0, "row late_count"), ("on_time_rate", 1., "on_time_rate"),
    ("source_sha256", "0" * 64, "source_sha256"), ("solution_sha256", "0" * 64, "solution fingerprint"),
    ("seed", 2026101373, "frozen objective"), ("budget_seconds", 50, "Run budget"),
    ("scenario", "direct_relay", "pure_direct"), ("task_count", 24, "task_count"),
    ("budget_compliant", False, "budget_compliant"), ("search_seconds", 2., "phase sum"),
    ("end_to_end_seconds", float("nan"), "Nonfinite"), ("solution_file", "../escape.json", "escaping"),
    ("violations", ["fabricated"], "violations disagree"),
    ("decision_fingerprint", "0" * 64, "Decision fingerprint"),
])
def test_rejects_tampered_run_and_removes_stale_success(experiment, field, value, message):
    directory, _, row, *_ = experiment
    audit.audit_results(directory)
    row[field] = value
    write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match=message):
        audit.audit_results(directory)
    assert not (directory / "validation.json").exists()


def test_rejects_route_corruption_even_with_new_solution_hash(experiment):
    directory, _, row, payload, _ = experiment
    payload["routes"][0]["encoded_visits"].pop()
    write(directory / "solution.json", payload)
    row["solution_sha256"] = audit.checks._sha256(directory / "solution.json")
    write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match="valid flag"):
        audit.audit_results(directory)


def test_rejects_new_feature_claim_missing_from_solver_metadata(experiment):
    directory, _, row, _, protocol = experiment
    row["variant"] = "boundary"
    row["config"].update(protocol.VARIANTS["boundary"])
    write(directory / "runs.json", [row])
    with pytest.raises(ValueError, match="metadata.*enable_boundary_destroy"):
        audit.audit_results(directory)


def test_rejects_changed_search_protocol(experiment):
    directory, manifest, *_ = experiment
    manifest["protocol"]["total_experiment_seconds"] = 3600
    write(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="Budgets or cases"):
        audit.audit_results(directory)


def test_pairs_cannot_silently_change_initial_routes(experiment):
    directory, _, row, *_ = experiment
    candidate = deepcopy(row)
    candidate.update(variant="boundary", initial_routes_sha256="0" * 64)
    write(directory / "runs.json", [row, candidate])
    with pytest.raises(ValueError, match="same initial routes"):
        audit.audit_results(directory)


def test_duplicate_cell_is_never_cherry_picked(experiment):
    directory, _, row, *_ = experiment
    write(directory / "runs.json", [row, row])
    with pytest.raises(ValueError, match="Duplicate experiment cell"):
        audit.audit_results(directory)


def test_honest_timeout_is_auditable_but_not_successful(experiment):
    directory, _, row, *_ = experiment
    row.update(end_to_end_seconds=5.1, budget_compliant=False)
    write(directory / "runs.json", [row])
    report = audit.audit_results(directory)
    assert report["evidence_verified"] and not report["all_runs_successful"]
    assert not report["has_valid_results"] and report["over_budget_rows"] == [0]


def test_aborted_work_is_charged_and_not_overwritten(experiment):
    directory, _, row, *_ = experiment
    attempt = {"stage": "screen", "case_id": "single25", "variant": "boundary", "seed": row["seed"],
               "process_elapsed_seconds": 91., "status": "timeout_no_result"}
    write(directory / "execution_notes.json", {"aborted_runs": [attempt]})
    report = audit.audit_results(directory)
    assert report["cumulative_budget"]["total_seconds"] == pytest.approx(91.3)
    assert not report["cumulative_budget"]["budget_compliant"]
    assert not report["all_runs_successful"]
    attempt["variant"] = "baseline"
    write(directory / "execution_notes.json", {"aborted_runs": [attempt]})
    with pytest.raises(ValueError, match="Duplicate completed or aborted"):
        audit.audit_results(directory)


def test_baseline_source_cannot_be_replaced_and_rehashed(experiment):
    directory, manifest, *_ = experiment
    path = directory / "baseline.zip"
    with zipfile.ZipFile(path) as archive:
        content = {name: archive.read(name) for name in archive.namelist()}
    name = next(iter(content))
    content[name] += b"\n# changed\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in content.items():
            archive.writestr(name, data)
    for source in manifest["source_snapshots"].values():
        source["sha256"] = audit.checks._sha256(path)
    write(directory / "manifest.json", manifest)
    with pytest.raises(ValueError, match="Baseline archive differs"):
        audit.audit_results(directory)


def test_empty_run_list_never_claims_success(experiment):
    directory, *_ = experiment
    write(directory / "runs.json", [])
    report = audit.audit_results(directory)
    assert not report["all_runs_successful"] and not report["has_valid_results"]


def gate_rows(stage, variants=("baseline", "boundary", "objective")):
    protocol = audit._protocol()
    return [{"stage": stage, "case_id": case, "variant": variant, "seed": seed,
             "valid": True, "budget_compliant": True, "late_count": 10 if variant == "baseline" else 9,
             "total_lateness_min": 20., "distance_km": 50., "iterations": 200}
            for case in protocol.CASES[stage] for seed in protocol.SEEDS[stage] for variant in variants]


def test_screen_allows_balanced_exploration_but_not_net_late_increase():
    rows = gate_rows("screen")
    candidate = [row for row in rows if row["variant"] == "boundary" and row["case_id"] == "fleet200"]
    candidate[0]["late_count"], candidate[1]["late_count"] = 9, 11
    equivalent = {"boundary": True, "objective": True}
    assert audit._quality_gates(rows, equivalent)["screen"]["boundary"]
    candidate[1]["late_count"] = 12
    assert not audit._quality_gates(rows, equivalent)["screen"]["boundary"]


def test_medium_does_not_trade_one_first_objective_regression_for_other_wins():
    rows = gate_rows("medium")
    candidate = next(row for row in rows if row["variant"] == "boundary" and row["case_id"] == "fleet200")
    equivalent = {"boundary": True, "objective": True}
    assert audit._quality_gates(rows, equivalent)["medium"]["boundary"]
    candidate["late_count"] = 11
    assert not audit._quality_gates(rows, equivalent)["medium"]["boundary"]


def test_confirmation_does_not_count_known_seed_as_independent_win():
    rows = gate_rows("screen") + gate_rows("medium") + gate_rows("confirm", ("baseline", "boundary"))
    equivalent = {"boundary": True, "objective": True}
    assert audit._quality_gates(rows, equivalent)["confirm"]["boundary"]
    for row in rows:
        if row["stage"] == "confirm" and row["variant"] == "boundary" and row["seed"] in (2026101392, 2026101393):
            row["late_count"] = 10
    assert not audit._quality_gates(rows, equivalent)["confirm"]["boundary"]


def test_selection_recomputes_tiebreak_and_eligibility():
    equivalent = {"boundary": True, "objective": True}
    rows = gate_rows("screen") + gate_rows("medium")
    assert audit._quality_gates(rows, equivalent)["selected_variant"] == "boundary"
    for row in rows:
        if row["stage"] == "medium" and row["variant"] == "objective" and row["case_id"] == "fleet200":
            row["late_count"] = 8
    assert audit._quality_gates(rows, equivalent)["selected_variant"] == "objective"
    equivalent["objective"] = False
    assert audit._quality_gates(rows, equivalent)["selected_variant"] == "boundary"


def test_confirmation_failure_still_requires_every_scheduled_seed(tmp_path, monkeypatch):
    rows = (gate_rows("equivalence") + gate_rows("screen") + gate_rows("medium")
            + gate_rows("confirm", ("baseline", "boundary")))
    first = next(row for row in rows if row["stage"] == "confirm" and row["variant"] == "boundary")
    first["late_count"] = 11
    equivalent = {"boundary": True, "objective": True}
    monkeypatch.setattr(audit, "_equivalence", lambda *_: equivalent)
    decisions = [
        {"stage": "equivalence", "candidates": equivalent, "passed": True},
        {"stage": "screen", "candidates": equivalent, "eligible_variants": ["boundary", "objective"]},
        {"stage": "medium", "candidates": equivalent, "selected_variant": "boundary", "passed": True},
        {"stage": "confirm", "selected_variant": "boundary", "passed": False, "completed_pairs": 4, "planned_pairs": 4},
    ]
    write(tmp_path / "decisions.json", decisions)
    report = audit._decision_audit(tmp_path, rows)
    assert report["protocol_complete"] and not report["default_eligible"]
    decisions[-1]["passed"] = True
    write(tmp_path / "decisions.json", decisions)
    with pytest.raises(ValueError, match="independently recomputed gate"):
        audit._decision_audit(tmp_path, rows)
    decisions[-1].update(passed=False, completed_pairs=1)
    write(tmp_path / "decisions.json", decisions)
    partial = [row for row in rows if row["stage"] != "confirm" or row["seed"] == 2026101321]
    with pytest.raises(ValueError, match="missing or unexpected experiment cells"):
        audit._decision_audit(tmp_path, partial)


def test_equivalence_ignores_diagnostic_counters_but_keeps_original_rewards(tmp_path):
    statistics = {"repair:greedy": {"uses": 4, "accepted": 2, "current_improvements": 1,
                                    "best_improvements": 1, "total_reward": 12., "primary_improvements": 1}}
    payload = {"routes": [{"encoded_visits": [1, -1]}], "metadata": {
        "operator_statistics": statistics, "operator_weights": {"repair:greedy": 1.}, "accepted_solutions": 2}}
    row = {"solution_file": "solution.json", "late_count": 1, "total_lateness_min": 2., "distance_km": 3.,
           "iterations": 200, "best_trajectory": [[0, .1, 1, 2., 3.]]}
    write(tmp_path / "solution.json", payload)
    original = audit._stable_search(tmp_path, row)
    statistics["repair:greedy"]["primary_improvements"] = 9
    write(tmp_path / "solution.json", payload)
    assert audit._stable_search(tmp_path, row) == original
    statistics["repair:greedy"]["total_reward"] = 13.
    write(tmp_path / "solution.json", payload)
    assert audit._stable_search(tmp_path, row) != original
