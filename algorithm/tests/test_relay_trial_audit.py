"""Independent relay timing and immutable exploratory-evidence checks."""
from copy import deepcopy

import pytest

from uav_dispatch import GlobalRelayEvaluator, Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.cli import result_payload
from uav_dispatch.search import SolverResult
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import audit_relay_trials as audit


def relay_problem():
    return Problem(
        (Task(1, Point(9, 0), Point(1, 0), 18.),
         Task(2, Point(0, 0), Point(1, 0), 20.),
         Task(3, Point(1, 0), Point(2, 0), 21.)),
        drone_count=2, max_tasks_per_drone=3, speed_km_per_min=1.,
        relay_stations=(RelayStation(1, 0, 0),),
        leg_registry={4: TransportLeg(4, 1, "RELAY_IN", 1), 5: TransportLeg(5, 1, "RELAY_OUT", 1),
                      6: TransportLeg(6, 2, "RELAY_IN", 1), 7: TransportLeg(7, 2, "RELAY_OUT", 1)},
        task_relay_candidates={1: (1,), 2: (1,)},
    )


def test_reference_scheduler_propagates_wait_without_recharging_it():
    problem = relay_problem()
    routes = ((4, -4, 6, -6), (5, -5, 7, -7, 3, -3))
    reference = audit.reference_schedule(problem, routes)
    # The first pickup waits 18 min; after that wait, the second pickup arrives
    # at min 20 and the second package has already been stored since min 18.
    assert reference["delivery_times_min"] == {1: 19., 2: 21., 3: 22.}
    assert reference["waiting_by_task"] == {1: 18.}
    assert reference["total_waiting_min"] == 18.
    assert reference["score"].late_count == 3
    assert reference["score"].total_lateness_min == 3.
    assert reference["score"].distance_km == 22.
    assert reference["cross_uav_handoff_count"] == 2
    production = GlobalRelayEvaluator(problem).evaluate(routes)
    assert reference["score"] == production.score
    assert reference["event_times_min"] == dict(production.event_times_min)
    assert reference["waiting_by_task"] == dict(production.waiting_by_task)


def test_reference_scheduler_rejects_cross_route_cycle():
    with pytest.raises(ValueError, match="dependency cycle"):
        audit.reference_schedule(relay_problem(), ((7, -7, 4, -4), (5, -5, 6, -6, 3, -3)))


def test_reference_scheduler_merges_same_route_adjacent_handoff_edges():
    result = audit.reference_schedule(relay_problem(), ((4, -4, 5, -5, 6, -6, 7, -7, 3, -3), ()))
    assert result["same_uav_relay_count"] == 2
    assert result["cross_uav_handoff_count"] == 0
    assert result["total_waiting_min"] == 0.
    assert set(result["delivery_times_min"]) == {1, 2, 3}


def test_reference_scheduler_counts_home_to_first_flight_and_no_return():
    problem = Problem((Task(1, Point(2, 0), Point(5, 0), 2.),), drone_count=1,
                      drone_homes=(Point(1, 0),), max_tasks_per_drone=1, speed_km_per_min=1.)
    result = audit.reference_schedule(problem, ((1, -1),))
    assert result["delivery_times_min"] == {1: 4.}
    assert result["score"].distance_km == 4.
    assert result["score"].total_lateness_min == 2.


@pytest.mark.parametrize("corruption", ["waiting", "row_statistics", "arrival"])
def test_reference_audit_rejects_corrupted_saved_timing_and_report_fields(tmp_path, monkeypatch, corruption):
    protocol = audit._protocol()
    problem = relay_problem()
    routes = ((4, -4, 6, -6), (5, -5, 7, -7, 3, -3))
    checked = evaluate_solution(problem, routes, _force_full=True)
    payload = result_payload(problem, SolverResult(routes=routes, evaluation=checked, runtime_seconds=0., iterations=0, metadata={}), source="fixture")
    score = [getattr(checked.score, field) for field in audit.checks.SCORE_FIELDS]
    payload["metadata"] = {"relay": deepcopy(payload["relay"]), "relay_warmup_score": score,
                           "relay_search_final_score": score, "relay_retained_warmup_incumbent": False,
                           "relay_warmup_iterations": 0, "relay_search_iterations": 0}
    row = dict(case_id=protocol.PRIMARY, scenario="direct_relay_stations", solution_file="solution.json", solution_sha256="a" * 64,
               relay_statistics=deepcopy(payload["relay"]), relay_warmup_score=score, relay_search_final_score=score, iterations=0,
               **{field: getattr(checked.score, field) for field in audit.checks.SCORE_FIELDS})
    if corruption == "waiting":
        payload["relay"]["total_relay_waiting_time"] = 36.
        payload["metadata"]["relay"] = deepcopy(payload["relay"])
        row["relay_statistics"] = deepcopy(payload["relay"])
    elif corruption == "row_statistics":
        row["relay_statistics"]["cross_uav_handoff_count"] = 0
    else:
        payload["routes"][1]["visits"][0]["arrival_time_min"] = 0.
    protocol.base.write_json(tmp_path / "solution.json", payload)
    monkeypatch.setattr(audit.checks, "_case_problem", lambda *_: problem)
    with pytest.raises(ValueError, match="reference total wait|Row relay statistics|reference event time"):
        audit._reference_audit(tmp_path, {"input_path": protocol.base.INPUT}, [row])


def stage_rows(stage, variants=None):
    protocol = audit._protocol()
    variants = list(protocol.VARIANTS) if variants is None else variants
    rows = []
    for index, seed in enumerate(protocol.SEEDS[stage]):
        order = variants if index % 2 == 0 else list(reversed(variants))
        for case in protocol.CASES[stage]:
            for variant in variants:
                rows.append({"stage": stage, "case_id": case, "variant": variant, "seed": seed,
                             "valid": True, "budget_compliant": True, "late_count": 10 if variant == "baseline" else 9,
                             "total_lateness_min": 20., "distance_km": 50., "iterations": 100,
                             "decision_fingerprint": "a" * 64, "order": order.index(variant)})
    return rows


def test_screen_selection_recomputes_predeclared_tiebreak_and_compatibility():
    rows = stage_rows("compatibility") + stage_rows("screen")
    assert audit._gates(rows)["selected_variant"] == "checkpoint"
    next(row for row in rows if row["stage"] == "compatibility" and row["variant"] == "checkpoint")["decision_fingerprint"] = "b" * 64
    assert audit._gates(rows)["selected_variant"] == "diversity"


def test_exploratory_default_gate_keeps_one_regression_and_requires_all_pairs():
    protocol = audit._protocol()
    rows = stage_rows("compatibility") + stage_rows("screen") + stage_rows("medium", ["baseline", "checkpoint"])
    regression = next(row for row in rows if row["stage"] == "medium" and row["case_id"] == protocol.PRIMARY
                      and row["variant"] == "checkpoint")
    regression["late_count"] = 11
    assert audit._gates(rows)["default_eligible"]
    rows.remove(regression)
    assert not audit._gates(rows)["default_eligible"]


def test_decision_audit_rejects_missing_reference_and_changed_selection(tmp_path):
    protocol = audit._protocol()
    rows = stage_rows("compatibility") + stage_rows("screen") + stage_rows("medium", ["baseline", "checkpoint"])
    decisions = [dict(stage="screen", selected_variant="checkpoint", passed={v: True for v in protocol.VARIANTS if v != "baseline"},
                      reason=protocol.PROTOCOL["screen_gate"]),
                 dict(stage="medium", selected_variant="checkpoint", passed=True, reason=protocol.PROTOCOL["default_gate"])]
    protocol.base.write_json(tmp_path / "decisions.json", decisions)
    assert not audit._decision_audit(tmp_path, rows)["protocol_complete"]
    rows += stage_rows("reference", ["baseline"])
    assert audit._decision_audit(tmp_path, rows)["protocol_complete"]
    decisions[0]["selected_variant"] = "risk"
    protocol.base.write_json(tmp_path / "decisions.json", decisions)
    with pytest.raises(ValueError, match="independent gate"):
        audit._decision_audit(tmp_path, rows)


@pytest.fixture
def historical_relay_snapshots(monkeypatch):
    """Keep historical audit input independent of new checkout modules."""
    protocol = audit._protocol()
    archived = protocol.ROOT / "algorithm/results/relay_trials_20261013/batch1"
    original = protocol.base.read_json(archived / "manifest.json")
    assert original["baseline_commit"] == protocol.BASELINE_COMMIT

    def snapshot(path, commit=None):
        assert commit in (None, protocol.BASELINE_COMMIT)
        source = original["source_snapshots"]["baseline" if commit else "checkpoint"]
        frozen = archived / source["path"]
        assert protocol.base.sha256(frozen) == source["sha256"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(frozen.read_bytes())

    # The old helper enumerates live files even for a historical Git ref.
    # Supply its original immutable archives instead of weakening the audit.
    monkeypatch.setattr(protocol.base, "_snapshot", snapshot)
    return protocol


def test_audit_rejects_changed_frozen_manifest_and_baseline_archive(tmp_path, historical_relay_snapshots):
    protocol = historical_relay_snapshots
    directory = tmp_path / "batch"
    manifest = protocol.prepare(directory)
    audit._manifest(manifest)
    audit._sources(directory, manifest)
    changed = deepcopy(manifest)
    changed["protocol"]["max_tasks_per_drone"] = 26
    with pytest.raises(ValueError, match="frozen protocol"):
        audit._manifest(changed)
    (directory / manifest["source_snapshots"]["baseline"]["path"]).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="snapshot fingerprint"):
        audit._sources(directory, manifest)


def test_aborted_work_counts_against_stage_budget_and_cannot_be_rerun(tmp_path):
    protocol = audit._protocol()
    attempt = dict(stage="screen", case_id=protocol.PRIMARY, variant="checkpoint", seed=protocol.SEEDS["screen"][0],
                   process_elapsed_seconds=protocol.CAPS["screen"] + 1.)
    protocol.base.write_json(tmp_path / "execution_notes.json", {"aborted_runs": [attempt]})
    budget = audit._budget(tmp_path, [])
    assert budget["aborted_run_count"] == 1
    assert not budget["budget_compliant"]
    with pytest.raises(ValueError, match="duplicated"):
        audit._budget(tmp_path, [{**attempt, "end_to_end_seconds": 1.}])
