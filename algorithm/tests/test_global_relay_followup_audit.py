"""Frozen global experiment evidence, complete pairing and tampering checks."""
from copy import deepcopy
from pathlib import Path
import shutil
import zipfile

import pytest

from algorithm.experiments import audit_global_relay_followup as audit
from uav_dispatch.alns import construct_edd_adjacent
from uav_dispatch.cli import result_payload
from uav_dispatch.io import load_tasks_csv
from uav_dispatch.search import SolverResult


def _stage_rows(stage, *, selected="terminal", improved=True):
    rows = []
    planned = audit._planned(stage, selected)
    for cell in planned:
        _, case, variant, seed = cell
        group = [c for c in planned if c[1] == case and c[3] == seed]
        rows.append(dict(zip(("stage", "case_id", "variant", "seed"), cell),
                         valid=True, budget_compliant=True, late_count=10 if variant == "baseline" or not improved else 9,
                         total_lateness_min=20., distance_km=50., iterations=100,
                         initial_routes_sha256="b" * 64, budget_seconds=audit._protocol().BUDGETS[stage],
                         scenario=case.split("__", 1)[1] if "__" in case else "pure_direct",
                         task_count=25 if case == "single25" else 200,
                         drone_count=1 if case == "single25" else 8,
                         decision_fingerprint="a" * 64, order=group.index(cell)))
    return rows


def _decisions(directory, rows):
    gates = audit._gates(rows)
    screen = gates["screen"]
    validate = {"stage": "validate", "passed": gates["default_eligible"],
                "selected_relay_variant": screen["selected_relay_variant"]} if screen["passed"] else {
                    "stage": "validate", "passed": False, "skipped": True,
                    "reason": "四场景完整规模初筛未过门槛，不启动长测"}
    audit._protocol().base.write_json(directory / "decisions.json", [{"stage": "screen", **screen}, validate])


@pytest.fixture(scope="module")
def frozen_fixture(tmp_path_factory):
    """Synthetic audit evidence, built by scoring routes, never by search."""
    directory = tmp_path_factory.mktemp("global-audit") / "complete"
    p = audit._protocol()
    archived = p.ROOT / "algorithm/results/global_relay_followup_20261014"
    original = p.base.read_json(archived / "manifest.json")
    assert original["baseline_commit"] == p.BASELINE_COMMIT

    def historical_snapshot(path, commit=None):
        assert commit in (None, p.BASELINE_COMMIT)
        source = original["source_snapshots"]["baseline" if commit else "terminal"]
        frozen = archived / source["path"]
        assert p.base.sha256(frozen) == source["sha256"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(frozen.read_bytes())

    # Promoted public defaults must not change this historical source generation.
    # Keep all source-difference assertions strict by using its SHA-bound archives.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(p, "snapshot", historical_snapshot)
        p.prepare(directory)
    manifest = audit._read(directory / "manifest.json")
    tasks = tuple(load_tasks_csv(audit.checks.DEFAULT_INPUT))
    rows, cache = [], {}
    for cell in audit._planned("compatibility") + audit._planned("screen"):
        stage, case, variant, seed = cell
        scenario = case.split("__", 1)[1] if "__" in case else "pure_direct"
        row = dict(zip(("stage", "case_id", "variant", "seed"), cell), scenario=scenario,
                   task_count=25 if case == "single25" else 200, drone_count=1 if case == "single25" else 8)
        if case not in cache:
            problem = audit.checks._case_problem(row, tasks)
            initial = construct_edd_adjacent(problem)
            cache[case] = problem, initial
        problem, initial = cache[case]
        score = initial.evaluation.score
        score_tuple = [getattr(score, key) for key in audit.checks.SCORE_FIELDS]
        relay = case in p.RELAY_CASES
        homes = "stations" in case
        configuration = {"max_iterations": 100 if stage == "compatibility" else 10_000_000,
                         "time_limit_seconds": p.BUDGETS[stage] - 1.1, "seed": seed,
                         "candidate_limit": 48, "relay_enabled": relay, "relay_direct_warmup_fraction": .8,
                         "enable_home_seed": homes, "enable_home_bias": homes, **p.VARIANTS[variant]}
        metadata = {"seed": seed, "relay_enabled": relay, "enable_home_seed": homes,
                    "time_limit_seconds": configuration["time_limit_seconds"],
                    "best_trajectory": [[0, 0., *score_tuple]], "initial_score": score_tuple,
                    **p.VARIANTS[variant]}
        if relay:
            metadata.update(staged_relay=True, relay_direct_warmup_fraction=.95 if variant == "rebalanced" else .8,
                            relay_warmup_runtime_seconds=.016, relay_search_runtime_seconds=.004,
                            relay_warmup_score=score_tuple, relay_search_final_score=score_tuple,
                            relay_retained_warmup_incumbent=False, relay_warmup_iterations=80,
                            relay_search_iterations=20)
        if variant != "baseline":
            active = problem.drone_count > 1
            metadata["terminal_polish"] = {
                "active": active,
                "phase": "direct_warmup" if relay else "direct" if active else "inactive",
                "reserved_seconds": .1 if active else 0., "elapsed_seconds": .001 if active else 0.,
                "accepted": False, "before_score": score_tuple, "after_score": score_tuple, "statistics": {},
            }
        metadata["relay_seed_checkpoint_active"] = relay and variant == "rebalanced"
        result = SolverResult(initial.routes, initial.evaluation, runtime_seconds=.02, iterations=100, metadata=metadata)
        payload = result_payload(problem, result, source=p.base.INPUT)
        if relay:
            payload["metadata"]["relay"] = deepcopy(payload["relay"])
        run_id = "__".join(map(str, cell))
        row.update(run_id=run_id, solution_file=f"run_solutions/{run_id}.json",
                   source_sha256=manifest["source_snapshots"][variant]["sha256"],
                   initial_routes_sha256=p.base.value_hash(initial.routes), budget_seconds=p.BUDGETS[stage],
                   end_to_end_seconds=.3, construction_seconds=.1, search_seconds=.1,
                   validation_seconds=.01, serialization_seconds=.01, budget_compliant=True,
                   valid=True, violations=[], **payload["score"], on_time_count=len(problem.tasks) - score.late_count,
                   on_time_rate=payload["on_time_rate"], iterations=100,
                   best_trajectory=[[0, .1, *score_tuple]], config=configuration, profiling={},
                   relay_statistics=deepcopy(payload["metadata"].get("relay", {})),
                   relay_warmup_score=metadata.get("relay_warmup_score"),
                   terminal_polish=deepcopy(payload["metadata"].get("terminal_polish", {})),
                   phase_statistics={key: metadata.get(key) for key in p.PHASE_FIELDS})
        planned = audit._planned(stage)
        row["order"] = [c for c in planned if c[1] == case and c[3] == seed].index(cell)
        row["decision_fingerprint"] = p.base.value_hash({
            "routes": initial.routes, "score": payload["score"], "iterations": 100,
            "trajectory": [[0, *score_tuple]], "operator_statistics": {}, "operator_weights": {},
            "accepted_solutions": None})
        p.base.write_json(directory / row["solution_file"], payload)
        row["solution_sha256"] = p.base.sha256(directory / row["solution_file"])
        rows.append(row)
    p.base.write_json(directory / "runs.json", rows)
    _decisions(directory, rows)
    return directory


@pytest.fixture
def evidence(tmp_path, frozen_fixture):
    target = tmp_path / "evidence"
    shutil.copytree(frozen_fixture, target)
    return target


def test_full_screen_failure_is_complete_valid_evidence_without_validation_runs(evidence):
    report = audit.audit_results(evidence)
    assert report["passed"] and report["protocol_complete"]
    assert report["run_count"] == report["independent_dag_audit"]["validated_rows"] == 33
    assert report["decision_audit"]["screen"]["passed"] is False
    assert report["decision_audit"]["default_eligible"] is False
    assert report["independent_dag_audit"]["relay_enabled_rows"] == 18
    assert audit._read(evidence / "validation.json") == report


@pytest.mark.parametrize("corruption", ["solution", "input", "orphan", "duplicate", "initial", "configuration", "metadata", "trajectory", "source"])
def test_audit_rejects_corruption_in_complete_evidence(evidence, corruption):
    p = audit._protocol()
    manifest, rows = audit._read(evidence / "manifest.json"), audit._read(evidence / "runs.json")
    row = rows[4]
    payload = audit._read(evidence / row["solution_file"])
    if corruption == "solution":
        payload["routes"][0]["encoded_visits"] = []
    elif corruption == "input":
        manifest["input_sha256"] = "0" * 64
    elif corruption == "orphan":
        p.base.write_json(evidence / "run_solutions/orphan.json", payload)
    elif corruption == "duplicate":
        rows.append(deepcopy(row))
    elif corruption == "initial":
        row["initial_routes_sha256"] = "b" * 64
    elif corruption == "configuration":
        row["config"]["enable_terminal_polish"] = False
    elif corruption == "metadata":
        payload["metadata"]["enable_terminal_polish"] = False
    elif corruption == "trajectory":
        row["best_trajectory"][-1][2] += 1
    elif corruption == "source":
        row["source_sha256"] = "0" * 64
    p.base.write_json(evidence / row["solution_file"], payload)
    row["solution_sha256"] = p.base.sha256(evidence / row["solution_file"])
    p.base.write_json(evidence / "manifest.json", manifest)
    p.base.write_json(evidence / "runs.json", rows)
    with pytest.raises(ValueError):
        audit.audit_results(evidence)
    assert not (evidence / "validation.json").exists()


@pytest.mark.parametrize("field", ["total_relay_waiting_time", "cross_uav_handoff_count", "relay_usage_per_station"])
def test_reference_rejects_relay_statistics_even_when_all_copies_match(evidence, field):
    p = audit._protocol()
    rows = audit._read(evidence / "runs.json")
    row = next(r for r in rows if r["case_id"] in p.RELAY_CASES)
    payload = audit._read(evidence / row["solution_file"])
    if field == "relay_usage_per_station":
        payload["relay"][field][0]["task_count"] += 1
    else:
        payload["relay"][field] += 1
    payload["metadata"]["relay"] = deepcopy(payload["relay"])
    row["relay_statistics"] = deepcopy(payload["relay"])
    p.base.write_json(evidence / row["solution_file"], payload)
    with pytest.raises(ValueError, match="reference"):
        audit._reference_audit(evidence, audit._read(evidence / "manifest.json"), [row])


@pytest.mark.parametrize("variant", ["baseline", "terminal"])
def test_git_source_contents_and_complete_candidate_module_set_are_checked(evidence, variant):
    manifest = audit._read(evidence / "manifest.json")
    source = manifest["source_snapshots"][variant]
    path = evidence / source["path"]
    with zipfile.ZipFile(path) as archive:
        files = {n: archive.read(n) for n in archive.namelist()}
    if variant == "baseline":
        files["uav_dispatch/alns.py"] += b"\n# modified\n"
    else:
        files.pop("uav_dispatch/incumbent_polish.py")
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    for arm in (["baseline"] if variant == "baseline" else ["terminal", "rebalanced"]):
        manifest["source_snapshots"][arm]["sha256"] = audit.checks._sha256(path)
    with pytest.raises(ValueError, match="module set or baseline Git"):
        audit._sources(evidence, manifest)


def test_validation_completeness_and_single_regression_block_promotion(tmp_path):
    rows = _stage_rows("compatibility") + _stage_rows("screen") + _stage_rows("validate")
    _decisions(tmp_path, rows)
    report = audit._decision_audit(tmp_path, rows)
    assert report["protocol_complete"] and report["default_eligible"]
    with pytest.raises(ValueError, match="all prespecified"):
        audit._decision_audit(tmp_path, rows[:-1])
    candidate = next(r for r in rows if r["stage"] == "validate" and r["variant"] == "terminal")
    candidate["late_count"] = 11
    assert audit._gates(rows)["default_eligible"] is False
    with pytest.raises(ValueError, match="independent gate"):
        audit._decision_audit(tmp_path, rows)


def test_downstream_selection_and_selected_validation_cells_are_independent(tmp_path):
    rows = _stage_rows("compatibility") + _stage_rows("screen")
    for row in rows:
        if row["stage"] == "screen" and row["variant"] == "rebalanced":
            row["late_count"] = 8
    assert audit._gates(rows)["screen"]["selected_relay_variant"] == "rebalanced"
    rows += _stage_rows("validate", selected="rebalanced")
    _decisions(tmp_path, rows)
    assert audit._decision_audit(tmp_path, rows)["protocol_complete"]
    next(r for r in rows if r["stage"] == "validate" and r["variant"] == "rebalanced")["variant"] = "terminal"
    with pytest.raises(ValueError, match="experiment cells"):
        audit._decision_audit(tmp_path, rows)


def test_complete_primary_gate_does_not_allow_missing_or_invalid_seeds(tmp_path):
    rows = _stage_rows("compatibility") + _stage_rows("screen", improved=False)
    primary = audit._protocol().CASES[-1]
    for row in rows:
        if row["stage"] == "screen" and row["case_id"] == primary and row["variant"] == "terminal":
            row["late_count"] = 9
    assert audit._gates(rows)["screen"]["passed"]
    _decisions(tmp_path, rows)
    with pytest.raises(ValueError, match="all prespecified"):
        audit._decision_audit(tmp_path, rows)
    # No validation rows yet: a validation decision cannot already exist.
    assert not audit._gates(rows[:-2])["screen"]["passed"]


def test_alternation_order_and_fixed_iteration_compatibility_are_checked(tmp_path):
    rows = _stage_rows("compatibility") + _stage_rows("screen", improved=False)
    _decisions(tmp_path, rows)
    rows[0]["iterations"] = 99
    with pytest.raises(ValueError, match="fixed-iteration"):
        audit._decision_audit(tmp_path, rows)
    rows[0]["iterations"] = 100
    rows[5]["order"] += 1
    with pytest.raises(ValueError, match="AB/BA"):
        audit._decision_audit(tmp_path, rows)


def test_aborts_count_toward_cap_and_cannot_be_rerun(tmp_path):
    p = audit._protocol()
    stage, case, variant, seed = audit._planned("screen")[0]
    attempt = dict(stage=stage, case_id=case, variant=variant, seed=seed, source_sha256="a" * 64,
                   status="timeout_no_result", process_elapsed_seconds=2501.)
    p.base.write_json(tmp_path / "execution_notes.json", {"aborted_runs": [attempt]})
    result = audit._budget(tmp_path, [], {"baseline": "a" * 64})
    assert result["aborted_run_count"] == 1 and not result["budget_compliant"]
    with pytest.raises(ValueError, match="duplicated"):
        audit._budget(tmp_path, [{**attempt, "end_to_end_seconds": 1.}], {"baseline": "a" * 64})


def test_frozen_manifest_rejects_changed_protocol(evidence):
    manifest = audit._read(evidence / "manifest.json")
    manifest["protocol"]["max_tasks_per_drone"] = 26
    with pytest.raises(ValueError, match="frozen protocol"):
        audit._manifest(manifest)


@pytest.mark.parametrize("field", ["active", "phase", "accepted", "before_score", "after_score",
                                   "reserved_seconds", "elapsed_seconds", "statistics"])
def test_terminal_phase_metadata_is_checked_even_when_row_and_payload_agree(evidence, field):
    rows = audit._read(evidence / "runs.json")
    row = next(r for r in rows if r["stage"] == "screen" and r["variant"] == "terminal"
               and r["case_id"] == "fleet200")
    payload = audit._read(evidence / row["solution_file"])
    terminal = payload["metadata"]["terminal_polish"]
    if field == "active":
        terminal[field] = False
    elif field == "phase":
        terminal[field] = "direct_warmup"
    elif field == "accepted":
        terminal[field] = True
    elif field in {"before_score", "after_score"}:
        terminal[field][0] += 1
    elif field == "reserved_seconds":
        terminal[field] = .1001
    elif field == "elapsed_seconds":
        terminal[field] = 2.
    else:
        terminal[field] = {"probes": -1}
    row["terminal_polish"] = deepcopy(terminal)
    with pytest.raises(ValueError):
        audit._configuration(row, payload)


def test_rebalanced_effective_fraction_is_95_percent_while_worker_config_stays_80(evidence):
    p = audit._protocol()
    rows = audit._read(evidence / "runs.json")
    row = next(r for r in rows if r["variant"] == "rebalanced" and r["case_id"] in p.RELAY_CASES)
    payload = audit._read(evidence / row["solution_file"])
    assert row["config"]["relay_direct_warmup_fraction"] == .8
    assert payload["metadata"]["relay_direct_warmup_fraction"] == .95
    audit._configuration(row, payload)
    payload["metadata"]["relay_direct_warmup_fraction"] = .8
    row["phase_statistics"]["relay_direct_warmup_fraction"] = .8
    with pytest.raises(ValueError, match="warmup fraction"):
        audit._configuration(row, payload)


def test_phase_statistics_cannot_diverge_from_solution(evidence):
    rows = audit._read(evidence / "runs.json")
    row = next(r for r in rows if r["scenario"] == "direct_relay")
    payload = audit._read(evidence / row["solution_file"])
    row["phase_statistics"]["relay_search_iterations"] += 1
    with pytest.raises(ValueError, match="Phase statistics"):
        audit._configuration(row, payload)


def test_selected_combination_not_terminal_alone_controls_screen_gate():
    p = audit._protocol()
    rows = _stage_rows("compatibility") + _stage_rows("screen", improved=False)
    for row in rows:
        if row["stage"] != "screen":
            continue
        if row["case_id"] in p.RELAY_CASES:
            row["late_count"] = {"baseline": 10, "terminal": 11, "rebalanced": 9}[row["variant"]]
        elif row["variant"] == "terminal" and row["seed"] == p.SEEDS["screen"][0]:
            row["late_count"] = 9
    screen = audit._gates(rows)["screen"]
    assert screen["selected_relay_variant"] == "rebalanced"
    assert screen["terminal_vs_baseline"]["wins"] == 2
    assert screen["terminal_vs_baseline"]["late_delta"] > 0
    assert screen["selected_vs_baseline"]["wins"] == 8
    assert screen["passed"] is True
    assert screen == p.screen_decision(rows)


def test_three_arm_order_rotates_instead_of_reversing():
    p = audit._protocol()
    planned = audit._planned("screen")
    orders = [[cell[2] for cell in planned if cell[1] == p.RELAY_CASES[0] and cell[3] == seed]
              for seed in p.SEEDS["screen"]]
    assert orders == [["baseline", "terminal", "rebalanced"],
                      ["terminal", "rebalanced", "baseline"],
                      ["rebalanced", "baseline", "terminal"]]


def test_source_configuration_does_not_treat_integer_one_as_boolean(evidence):
    manifest = audit._read(evidence / "manifest.json")
    manifest["source_snapshots"]["terminal"]["config"]["enable_terminal_polish"] = 1
    with pytest.raises(ValueError, match="configuration"):
        audit._sources(evidence, manifest)
