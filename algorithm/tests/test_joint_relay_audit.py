"""Whole-solver evidence checks; these tests do not run either optimizer."""
from copy import deepcopy
import zipfile

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.cli import result_payload
from uav_dispatch.search import SolverResult
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import audit_joint_relay_trials as audit


def payload_and_row():
    problem = Problem((Task(1, Point(9, 0), Point(1, 0), 18.),
                       Task(2, Point(0, 0), Point(1, 0), 20.)),
                      drone_count=2, max_tasks_per_drone=2, speed_km_per_min=1.,
                      relay_stations=(RelayStation(1, 0, 0),),
                      leg_registry={3: TransportLeg(3, 1, "RELAY_IN", 1), 4: TransportLeg(4, 1, "RELAY_OUT", 1),
                                    5: TransportLeg(5, 2, "RELAY_IN", 1), 6: TransportLeg(6, 2, "RELAY_OUT", 1)},
                      task_relay_candidates={1: (1,), 2: (1,)})
    routes = ((3, -3, 5, -5), (4, -4, 6, -6))
    checked = evaluate_solution(problem, routes, _force_full=True)
    payload = result_payload(problem, SolverResult(routes=routes, evaluation=checked,
                            runtime_seconds=0., iterations=0, metadata={}), source="fixture")
    row = {field: getattr(checked.score, field) for field in audit.checks.SCORE_FIELDS}
    return problem, payload, row


def test_joint_reference_audit_checks_waiting_cascade_without_alns_metadata():
    problem, payload, row = payload_and_row()
    checked = audit._reference_audit(problem, payload, row)
    assert checked["waiting_by_task"] == {1: 18.}
    assert checked["delivery_times_min"] == {1: 19., 2: 21.}
    assert "staged_relay" not in payload["metadata"]


@pytest.mark.parametrize("corruption", ["wait", "arrival", "plans", "station"])
def test_joint_reference_audit_rejects_corrupted_timing_and_service_plans(corruption):
    problem, payload, row = payload_and_row()
    if corruption == "wait":
        payload["relay"]["total_relay_waiting_time"] *= 2
    elif corruption == "arrival":
        payload["routes"][1]["visits"][0]["arrival_time_min"] = 0.
    elif corruption == "plans":
        payload["task_plans"]["1"]["mode"] = "DIRECT"
    else:
        payload["relay"]["relay_coordinates"][0][1] = 2.
    with pytest.raises(ValueError, match="joint reference|task plan|station coordinates"):
        audit._reference_audit(problem, payload, row)


def test_joint_reference_still_rejects_handoff_dependency_cycles():
    problem, _, _ = payload_and_row()
    with pytest.raises(ValueError, match="dependency cycle"):
        audit.reference_schedule(problem, ((6, -6, 3, -3), (4, -4, 5, -5)))


@pytest.mark.parametrize("member", ["../escape.py", "uav_dispatch/nested/module.py", "uav_dispatch/secret.txt"])
def test_archive_members_are_confined_to_one_python_package_level(tmp_path, member):
    path = tmp_path / "source.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(member, "pass\n")
    with pytest.raises(ValueError, match="Unsafe"):
        audit._archive_files(path)


def test_archive_rejects_invalid_zip_and_duplicate_members(tmp_path):
    path = tmp_path / "source.zip"
    path.write_bytes(b"not a zip")
    with pytest.raises(ValueError, match="Invalid"):
        audit._archive_files(path)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("uav_dispatch/model.py", "pass\n")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("uav_dispatch/model.py", "pass\n")
    with pytest.raises(ValueError, match="duplicate"):
        audit._archive_files(path)


def stage_rows(stage):
    protocol = audit._protocol()
    rows = []
    for index, seed in enumerate(protocol.SEEDS[stage]):
        order = list(protocol.VARIANTS) if index % 2 == 0 else list(reversed(protocol.VARIANTS))
        for case in protocol.CASES[stage]:
            for variant in protocol.VARIANTS:
                rows.append(dict(stage=stage, case_id=case, variant=variant, seed=seed, valid=True,
                                 budget_compliant=True, late_count=10 if variant == "baseline" else 9,
                                 total_lateness_min=20., distance_km=50., order=order.index(variant),
                                 iterations=80 if variant == "baseline" else 41))
    return rows


def test_smoke_checks_feasibility_without_requiring_solver_equivalence():
    rows = stage_rows("smoke") + stage_rows("screen")
    assert audit._gates(rows)["smoke"]
    assert audit._gates(rows)["screen"]
    next(row for row in rows if row["stage"] == "smoke" and row["variant"] == "joint")["valid"] = False
    assert not audit._gates(rows)["screen"]


def test_majority_default_gate_retains_primary_and_secondary_regressions():
    protocol = audit._protocol()
    rows = stage_rows("smoke") + stage_rows("screen") + stage_rows("medium")
    first = next(row for row in rows if row["stage"] == "medium" and row["case_id"] == protocol.PRIMARY and row["variant"] == "joint")
    first["late_count"] = 11
    for row in rows:
        if row["stage"] == "medium" and row["case_id"] == protocol.SECONDARY and row["variant"] == "joint":
            row["late_count"] = 20
    assert audit._gates(rows)["default_eligible"]
    rows.remove(first)
    assert not audit._gates(rows)["default_eligible"]


def test_declared_decisions_require_complete_validation_and_abba_order(tmp_path):
    protocol = audit._protocol()
    rows = stage_rows("smoke") + stage_rows("screen") + stage_rows("medium")
    decisions = [dict(stage="screen", selected_variant="joint", passed=True, reason=protocol.PROTOCOL["screen_gate"]),
                 dict(stage="medium", selected_variant="joint", passed=True, reason=protocol.PROTOCOL["default_gate"])]
    protocol.base.write_json(tmp_path / "decisions.json", decisions)
    assert audit._decision_audit(tmp_path, rows)["protocol_complete"]
    rows[-1]["order"] = 3
    with pytest.raises(ValueError, match="AB/BA"):
        audit._decision_audit(tmp_path, rows)
    rows.pop()
    with pytest.raises(ValueError, match="missing or unexpected cells"):
        audit._decision_audit(tmp_path, rows)


def test_no_screen_win_requires_skipped_medium_and_no_expanded_rows(tmp_path):
    protocol = audit._protocol()
    rows = stage_rows("smoke") + stage_rows("screen")
    for row in rows:
        if row["stage"] == "screen" and row["variant"] == "joint":
            row["late_count"] = 11
    protocol.base.write_json(tmp_path / "decisions.json", [
        dict(stage="screen", selected_variant=None, passed=False, reason=protocol.PROTOCOL["screen_gate"]),
        dict(stage="medium", selected_variant=None, passed=False, skipped=True, reason="没有初筛胜利，停止扩大")])
    assert audit._decision_audit(tmp_path, rows)["protocol_complete"]
    with pytest.raises(ValueError, match="without screening"):
        audit._decision_audit(tmp_path, rows + stage_rows("medium"))


def test_aborted_work_is_counted_and_cells_cannot_be_retried(tmp_path):
    protocol = audit._protocol()
    attempt = dict(stage="screen", case_id=protocol.PRIMARY, variant="joint", seed=protocol.SEEDS["screen"][0],
                   process_elapsed_seconds=protocol.CAPS["screen"] + 1.)
    protocol.base.write_json(tmp_path / "execution_notes.json", {"aborted_runs": [attempt]})
    budget = audit._budget(tmp_path, [])
    assert budget["aborted_run_count"] == 1
    assert budget["maximum_planned_runs"] == 24
    assert not budget["budget_compliant"]
    with pytest.raises(ValueError, match="duplicated"):
        audit._budget(tmp_path, [{**attempt, "end_to_end_seconds": 1.}])


def test_source_snapshots_bind_baseline_and_allow_only_isolated_solver_integration(tmp_path):
    protocol = audit._protocol()
    directory = tmp_path / "batch"
    manifest = protocol.prepare(directory)
    audit._manifest(manifest)
    audit._sources(directory, manifest)
    assert audit._config_defaults(directory, manifest, "joint")["station_limit"] == 2
    changed = deepcopy(manifest)
    changed["protocol"]["small"]["K"] = 5
    with pytest.raises(ValueError, match="frozen protocol"):
        audit._manifest(changed)
    path = directory / manifest["source_snapshots"]["joint"]["path"]
    files = audit._archive_files(path)
    files["uav_dispatch/model.py"] += b"\n# unauthorized model change\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    manifest["source_snapshots"]["joint"]["sha256"] = audit.checks._sha256(path)
    with pytest.raises(ValueError, match="outside CLI/export integration"):
        audit._sources(directory, manifest)


def config_fixture(defaults):
    protocol = audit._protocol()
    row = dict(stage="smoke", case_id=protocol.SMALL, variant="joint", seed=protocol.SEEDS["smoke"][0],
               scenario="direct_relay", budget_seconds=5, construction_seconds=.2, iterations=10,
               initial_score=dict(late_count=1, total_lateness_min=2., distance_km=3.),
               late_count=1, total_lateness_min=2., distance_km=3., solver_method="Joint-Relay-VNS",
               best_trajectory=[[0, .3, 1, 2., 3.]], profiling={}, neighborhood_statistics={}, relay_statistics={})
    requested = dict(max_iterations=80, seed=row["seed"], candidate_limit=12, time_limit_seconds=3.8)
    row.update(requested_config=requested, config={**defaults, **requested})
    payload = dict(relay={}, metadata=dict(method="Joint-Relay-VNS", seed=row["seed"], relay_enabled=True,
                  time_limit_seconds=3.8, direct_warmup=False, initial_score=[1, 2., 3.],
                  best_trajectory=[[0, .1, 1, 2., 3.]], profiling={}, joint_search={}))
    return row, payload


@pytest.mark.parametrize("corruption", ["whole_solver", "default", "shared_initial", "statistics"])
def test_config_audit_binds_whole_solver_archived_defaults_and_shared_start(tmp_path, corruption):
    protocol = audit._protocol()
    directory = tmp_path / "batch"
    manifest = protocol.prepare(directory)
    defaults = audit._config_defaults(directory, manifest, "joint")
    row, payload = config_fixture(defaults)
    audit._configuration(row, payload, defaults)
    if corruption == "whole_solver":
        payload["metadata"]["direct_warmup"] = True
    elif corruption == "default":
        row["config"]["pair_beam"] += 1
    elif corruption == "shared_initial":
        payload["metadata"]["initial_score"][0] = 2
    else:
        row["neighborhood_statistics"] = {"generated_plans": 100}
    with pytest.raises(ValueError, match="must not use|frozen value|initial score|report neighborhood"):
        audit._configuration(row, payload, defaults)
