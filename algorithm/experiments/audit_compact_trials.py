"""Audit the disclosed compact-neighborhood continuation without changing its parent.

Both variants must use the exact archived baseline solver. Only the two numeric
destruction fractions differ. All parent and child routes are independently
rescored, and their completed and aborted work shares the original time cap.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import zipfile

from uav_dispatch.io import load_tasks_csv
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import audit_objective_trials as parent_audit

checks = parent_audit.checks
_read = parent_audit._read
_cell = parent_audit._cell
_score = parent_audit._score


def _protocol():
    from algorithm.experiments import run_compact_trials
    return run_compact_trials


def _manifest(manifest: dict) -> None:
    protocol = _protocol()
    if manifest.get("schema_version") != 4 or manifest.get("kind") != "compact_trials":
        raise ValueError("Expected schema 4 compact_trials manifest")
    if manifest.get("baseline_commit") != protocol.BASELINE_COMMIT or manifest.get("baseline_variant") != "baseline":
        raise ValueError("Compact trial changed its baseline")
    if manifest.get("seeds") != protocol.SEEDS or any(
        type(seed) is not int for seeds in manifest["seeds"].values() for seed in seeds
    ):
        raise ValueError("Compact trial changed frozen seeds")
    expected = {"stage_cases": protocol.CASES, "stage_budgets_seconds": protocol.BUDGETS,
                "stage_caps_seconds": protocol.CAPS, "total_experiment_seconds": protocol.TOTAL_SECONDS,
                "reserved_seconds": protocol.RESERVED_SECONDS}
    saved = manifest.get("protocol")
    if not isinstance(saved, dict) or any(saved.get(key) != value for key, value in expected.items()):
        raise ValueError("Compact trial changed frozen budgets or cases")


def _sources(directory: Path, manifest: dict, parent_manifest: dict) -> tuple[dict, dict]:
    protocol = _protocol()
    snapshots = manifest.get("source_snapshots")
    if not isinstance(snapshots, dict) or set(snapshots) != set(protocol.VARIANTS):
        raise ValueError("Incomplete compact solver snapshots")
    reference = parent_manifest["source_snapshots"]["baseline"]
    commit = manifest["baseline_commit"]
    baseline_files = {
        name.removeprefix("algorithm/src/"): checks._git("show", f"{commit}:{name}")
        for name in checks._git("ls-tree", "-r", "--name-only", commit, "--", "algorithm/src/uav_dispatch").decode().splitlines()
        if name.endswith(".py")
    }
    hashes = {}
    for variant, flags in protocol.VARIANTS.items():
        source = snapshots[variant]
        if (not isinstance(source, dict) or source.get("config") != flags
                or any(type(source["config"][key]) is not type(value) for key, value in flags.items())
                or source.get("source_ref") != commit):
            raise ValueError("Compact solver configuration or source reference changed")
        path = checks._within(directory, source.get("path"), f"compact source {variant}")
        fingerprint = checks._fingerprint(source.get("sha256"), f"compact source {variant}")
        if checks._sha256(path) != fingerprint or fingerprint != reference["sha256"]:
            raise ValueError("Compact solver must match the unchanged parent baseline archive")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or any(
                not name.startswith("uav_dispatch/") or not name.endswith(".py") or len(Path(name).parts) != 2
                or ".." in Path(name).parts for name in names
            ):
                raise ValueError("Unsafe compact solver archive")
            if {name: archive.read(name) for name in names} != baseline_files:
                raise ValueError("Compact solver archive disagrees with baseline Git objects")
        hashes[variant] = fingerprint
    required = {"run_optimization_experiments", "run_scenario_comparison", "run_compact_trials"}
    execution = manifest.get("execution_sources")
    if not isinstance(execution, dict) or set(execution) != required:
        raise ValueError("Incomplete compact execution sources")
    execution_hashes = {}
    for name, source in execution.items():
        path = checks._within(directory, source.get("path"), name)
        fingerprint = checks._fingerprint(source.get("sha256"), name)
        if checks._sha256(path) != fingerprint:
            raise ValueError("Compact execution source fingerprint mismatch")
        execution_hashes[name] = fingerprint
    if manifest.get("runner_sha256") != execution_hashes["run_optimization_experiments"]:
        raise ValueError("Compact worker fingerprint mismatch")
    for name in ("run_optimization_experiments", "run_scenario_comparison"):
        if execution_hashes[name] != parent_manifest["execution_sources"][name]["sha256"]:
            raise ValueError("Compact trial changed the shared parent execution helper")
    return hashes, execution_hashes


def _row_protocol(row: dict) -> None:
    protocol = _protocol()
    stage, case, variant, seed = _cell(row)
    if (stage not in protocol.SEEDS or case not in protocol.CASES[stage] or variant not in protocol.VARIANTS
            or type(seed) is not int or seed not in protocol.SEEDS[stage]):
        raise ValueError("Compact run violates a frozen experiment cell")
    if row.get("budget_seconds") != protocol.BUDGETS[stage][case] or row.get("scenario") != "pure_direct":
        raise ValueError("Compact run violates the frozen budget or original scenario")


def _configuration(row: dict, payload: dict) -> None:
    expected = {"max_iterations": 200 if row["stage"] == "equivalence" else 10_000_000,
                "time_limit_seconds": max(1e-6, row["budget_seconds"] - row["construction_seconds"] - 1.),
                "seed": row["seed"], "candidate_limit": 48, "relay_enabled": False,
                "relay_direct_warmup_fraction": .8, "enable_home_seed": False, "enable_home_bias": False,
                **_protocol().VARIANTS[row["variant"]]}
    config = row.get("config")
    if not isinstance(config, dict) or set(config) != set(expected) or any(
        config[key] != value or type(config[key]) is not type(value)
        for key, value in expected.items() if key != "time_limit_seconds"
    ):
        raise ValueError("Compact run configuration differs from the frozen numeric parameters")
    checks._same(expected["time_limit_seconds"], config["time_limit_seconds"], "compact configured time")
    metadata = payload.get("metadata", {})
    # The unchanged historical solver does not emit destruction fractions in
    # metadata. Those values are bound by the row configuration and manifest.
    for key in ("seed", "relay_enabled", "enable_home_seed"):
        if metadata.get(key) != expected[key] or type(metadata.get(key)) is not type(expected[key]):
            raise ValueError("Compact metadata disagrees with execution configuration")
    checks._same(expected["time_limit_seconds"], metadata.get("time_limit_seconds"), "compact metadata time")
    if row["iterations"] > expected["max_iterations"]:
        raise ValueError("Compact run exceeded its iteration cap")


def _audit_rows(directory: Path, manifest: dict, rows: list[dict], hashes: dict, row_check, config_check) -> dict:
    source = checks._within(checks.ROOT, manifest.get("input_path"), "official input")
    input_hash = checks._fingerprint(manifest.get("input_sha256"), "official input")
    if checks._sha256(source) != input_hash or input_hash != checks._sha256(checks.DEFAULT_INPUT):
        raise ValueError("Original input fingerprint mismatch")
    tasks = tuple(load_tasks_csv(source))
    cells, initial_hashes, evaluations = set(), {}, {}
    invalid, over_budget = [], []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("Expected run object")
        row_check(row)
        if _cell(row) in cells:
            raise ValueError("Duplicate experiment cell")
        cells.add(_cell(row))
        if row.get("source_sha256") != hashes[row["variant"]]:
            raise ValueError("Run source_sha256 disagrees with snapshot")
        pair = (row["stage"], row["case_id"], row["seed"])
        fingerprint = checks._fingerprint(row.get("initial_routes_sha256"), "initial routes")
        if initial_hashes.setdefault(pair, fingerprint) != fingerprint:
            raise ValueError("Paired variants must share the same initial routes")
        problem = checks._case_problem(row, tasks)
        path = checks._within(directory, row.get("solution_file"), "solution")
        fingerprint = checks._fingerprint(row.get("solution_sha256"), "solution")
        if checks._sha256(path) != fingerprint:
            raise ValueError("Saved solution fingerprint mismatch")
        payload = _read(path)
        routes = payload.get("routes", [])
        if [route.get("drone_id") for route in routes] != list(range(1, problem.drone_count + 1)):
            raise ValueError("Missing, duplicate or reordered drone IDs")
        key = (row["case_id"], fingerprint)
        if key not in evaluations:
            evaluations[key] = evaluate_solution(problem, [route["encoded_visits"] for route in routes], _force_full=True)
        evaluation = evaluations[key]
        checks._audit_solution(problem, payload, row, evaluation)
        if row.get("violations") != list(evaluation.violations):
            raise ValueError("Run violations disagree with complete evaluation")
        compliant = checks._audit_runtime(row, payload)
        config_check(row, payload)
        checks._audit_trajectory(row, payload)
        checks._audit_decisions(row, payload, required=True)
        if not evaluation.valid:
            invalid.append(index)
        if not compliant:
            over_budget.append(index)
    return {"input_sha256": input_hash, "validated_rows": len(rows), "validated_unique_solutions": len(evaluations),
            "invalid_rows": invalid, "over_budget_rows": over_budget,
            "valid_on_budget_runs": len(rows) - len(set(invalid + over_budget))}


def _parent(directory: Path, manifest: dict) -> dict:
    marker = manifest.get("post_hoc_exploration")
    if not isinstance(marker, dict) or marker.get("parent_directory") != "..":
        raise ValueError("Missing disclosed parent experiment binding")
    root = directory.parent
    documents = {}
    for name in ("manifest", "runs", "decisions"):
        path = checks._within(root, f"{name}.json", f"parent {name}")
        expected = checks._fingerprint(marker.get(f"parent_{name}_sha256"), f"parent {name}")
        if checks._sha256(path) != expected:
            raise ValueError(f"Parent {name} fingerprint mismatch")
        documents[name] = _read(path)
    notes = root / "execution_notes.json"
    expected_notes = marker.get("parent_execution_notes_sha256")
    if (checks._sha256(notes) if notes.exists() else None) != expected_notes:
        raise ValueError("Parent execution notes fingerprint mismatch")
    parent_manifest, parent_rows = documents["manifest"], documents["runs"]
    if not isinstance(parent_manifest, dict) or not isinstance(parent_rows, list):
        raise ValueError("Invalid parent manifest or rows")
    parent_audit._manifest(parent_manifest)
    for key in ("baseline_commit", "input_path", "input_sha256"):
        if parent_manifest.get(key) != manifest.get(key):
            raise ValueError(f"Compact experiment changed parent {key}")
    hashes, _ = parent_audit._sources(root, parent_manifest)
    verification = _audit_rows(root, parent_manifest, parent_rows, hashes,
                               parent_audit._row_protocol, parent_audit._configuration)
    decisions = parent_audit._decision_audit(root, parent_rows)
    if (not decisions["protocol_complete"] or decisions["selected_variant"] is not None
            or any(decisions["medium"].values()) or any(row["stage"] == "confirm" for row in parent_rows)):
        raise ValueError("Compact continuation requires completed parent medium failure and skipped confirmation")
    medium = next((item for item in documents["decisions"] if item.get("stage") == "medium"), {})
    confirm = next((item for item in documents["decisions"] if item.get("stage") == "confirm"), {})
    if (medium.get("passed") is not False or medium.get("skipped") is True or confirm.get("skipped") is not True
            or medium.get("candidates") != {"boundary": False, "objective": False}
            or any(not parent_audit._complete(parent_rows, "medium", variant) for variant in ("boundary", "objective"))):
        raise ValueError("Parent must actually fail medium testing before compact continuation")
    budget = parent_audit._budget(root, parent_rows)
    checks._same(budget["total_seconds"], marker.get("parent_experiment_seconds"), "parent experiment seconds")
    if verification["invalid_rows"] or verification["over_budget_rows"] or not budget["budget_compliant"]:
        raise ValueError("Parent eligibility requires valid results within the original budget")
    return {"manifest": parent_manifest, "verification": verification, "decision_audit": decisions, "budget": budget,
            "manifest_sha256": marker["parent_manifest_sha256"], "runs_sha256": marker["parent_runs_sha256"],
            "decisions_sha256": marker["parent_decisions_sha256"], "execution_notes_sha256": expected_notes}


def _pairs(rows: list[dict], stage: str, case: str = "fleet200") -> list[tuple[dict, dict]]:
    index = {(row["variant"], row["seed"]): row for row in rows
             if row["stage"] == stage and row["case_id"] == case and row["valid"] and row["budget_compliant"]}
    return [(index["baseline", seed], index["compact", seed]) for seed in _protocol().SEEDS[stage]
            if ("baseline", seed) in index and ("compact", seed) in index]


def _complete(rows: list[dict], stage: str) -> bool:
    return all({baseline["seed"] for baseline, _ in _pairs(rows, stage, case)} == set(_protocol().SEEDS[stage])
               for case in _protocol().CASES[stage])


def _gates(rows: list[dict]) -> dict:
    eq = _complete(rows, "equivalence") and all(b["iterations"] == c["iterations"] == 200
        and b["decision_fingerprint"] == c["decision_fingerprint"] for b, c in _pairs(rows, "equivalence", "single25"))
    screen_pairs, medium_pairs, confirm_pairs = (_pairs(rows, stage) for stage in ("screen", "medium", "confirm"))
    screen = (eq and _complete(rows, "screen") and sum(_score(c) < _score(b) for b, c in screen_pairs) >= 1
              and sum(c["late_count"] - b["late_count"] for b, c in screen_pairs) <= 0
              and all(_score(c) <= _score(b) for b, c in _pairs(rows, "screen", "single25")))
    medium = (_complete(rows, "medium") and sum(_score(c) < _score(b) for b, c in medium_pairs) >= 2
              and all(c["late_count"] <= b["late_count"] for b, c in medium_pairs)
              and all(_score(c) <= _score(b) for b, c in _pairs(rows, "medium", "single25")))
    confirm = (medium and _complete(rows, "confirm") and all(_score(c) <= _score(b) for b, c in confirm_pairs)
               and sum(_score(c) < _score(b) for b, c in confirm_pairs if b["seed"] != 2026101321) >= 2)
    return {"equivalence": eq, "screen": screen, "medium": medium, "confirm": confirm,
            "default_eligible": bool(screen and medium and confirm)}


def _decisions(directory: Path, rows: list[dict]) -> dict:
    protocol = _protocol()
    path = directory / "decisions.json"
    decisions = _read(path) if path.exists() else []
    if not isinstance(decisions, list) or any(not isinstance(item, dict) for item in decisions):
        raise ValueError("Invalid compact stage decisions")
    stages = {item.get("stage"): item for item in decisions}
    order = ("equivalence", "screen", "medium", "confirm")
    if len(stages) != len(decisions) or not set(stages).issubset(order):
        raise ValueError("Duplicate or unknown compact stage decision")
    gates = _gates(rows)
    for index, stage in enumerate(order):
        # Equivalence and screening always run; later stages require promotion.
        if stage in {"equivalence", "screen"}:
            prior_passed = True
        elif stage == "medium":
            prior_passed = gates["screen"]
        else:
            prior_passed = gates["screen"] and gates["medium"]
        permitted = set(protocol.VARIANTS) if prior_passed else set()
        actual = {_cell(row) for row in rows if row["stage"] == stage}
        if any(cell[2] not in permitted for cell in actual):
            raise ValueError(f"Compact stage was run without qualification: {stage}")
        if stage not in stages:
            continue
        if any(prior not in stages for prior in order[:index]):
            raise ValueError("Compact stage decision missing prerequisite")
        expected = {"selected_variant": "compact" if prior_passed else None,
                    "passed": bool(gates[stage] if prior_passed else False)}
        if not prior_passed:
            expected["skipped"] = True
        elif stage in {"equivalence", "confirm"}:
            expected.update(completed_pairs=len(_pairs(rows, stage, "single25" if stage == "equivalence" else "fleet200")),
                            planned_pairs=3 if stage == "equivalence" else 4)
            if stage == "confirm":
                expected["continuation_after_regression"] = True
        item = stages[stage]
        if any(item.get(key) != value or type(item.get(key)) is not type(value) for key, value in expected.items()):
            raise ValueError(f"Compact decision disagrees with independent gate: {stage}")
        planned = {(stage, case, variant, seed) for case in protocol.CASES[stage]
                   for variant in permitted for seed in protocol.SEEDS[stage]}
        if actual != planned:
            raise ValueError(f"Compact completed stage has missing or unexpected cells: {stage}")
    return {**gates, "protocol_complete": set(stages) == set(order),
            "decisions_sha256": checks._sha256(path) if path.exists() else None}


def _budget(directory: Path, rows: list[dict], parent: dict) -> dict:
    protocol = _protocol()
    path = directory / "execution_notes.json"
    notes = _read(path) if path.exists() else {}
    attempts = notes.get("aborted_runs", [])
    if not isinstance(attempts, list):
        raise ValueError("Invalid compact aborted attempt list")
    stages = {stage: {"completed_seconds": 0., "aborted_seconds": 0., "cap_seconds": cap}
              for stage, cap in protocol.CAPS.items()}
    cells = {_cell(row) for row in rows}
    for row in rows:
        stages[row["stage"]]["completed_seconds"] += checks._number(row["end_to_end_seconds"], "completed seconds")
    for attempt in attempts:
        stage, case, _, _ = _cell(attempt)
        if stage not in protocol.SEEDS or case not in protocol.CASES[stage]:
            raise ValueError("Compact aborted cell outside frozen protocol")
        _row_protocol({**attempt, "scenario": "pure_direct", "budget_seconds": protocol.BUDGETS[stage][case]})
        if _cell(attempt) in cells:
            raise ValueError("Duplicate compact completed or aborted cell")
        cells.add(_cell(attempt))
        stages[stage]["aborted_seconds"] += checks._number(attempt.get("process_elapsed_seconds"), "aborted seconds")
    for bucket in stages.values():
        bucket["total_seconds"] = bucket["completed_seconds"] + bucket["aborted_seconds"]
    child_total = sum(bucket["total_seconds"] for bucket in stages.values())
    parent_total = parent["budget"]["total_seconds"]
    total = parent_total + child_total
    violations = [f"Compact stage cap exceeded: {stage}" for stage, bucket in stages.items()
                  if bucket["total_seconds"] > bucket["cap_seconds"]]
    if total > protocol.TOTAL_SECONDS - protocol.RESERVED_SECONDS:
        violations.append("Parent and compact work consumed the verification reserve")
    if total > protocol.TOTAL_SECONDS:
        violations.append("Parent and compact work exceeded the shared total budget")
    return {"stages": stages, "parent_experiment_seconds": parent_total, "compact_experiment_seconds": child_total,
            "total_seconds": total, "total_budget_seconds": protocol.TOTAL_SECONDS,
            "reserved_seconds": protocol.RESERVED_SECONDS, "budget_compliant": not violations, "violations": violations,
            "aborted_run_count": len(attempts) + parent["budget"]["aborted_run_count"],
            "execution_notes_sha256": checks._sha256(path) if path.exists() else None}


def audit_results(output_dir: Path) -> dict:
    directory = Path(output_dir).resolve()
    target = directory / "validation.json"
    target.unlink(missing_ok=True)
    manifest_path, rows_path = directory / "manifest.json", directory / "runs.json"
    manifest, rows = _read(manifest_path), _read(rows_path)
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ValueError("Expected compact manifest object and run list")
    _manifest(manifest)
    parent = _parent(directory, manifest)
    hashes, execution_hashes = _sources(directory, manifest, parent["manifest"])
    result = _audit_rows(directory, manifest, rows, hashes, _row_protocol, _configuration)
    decisions = _decisions(directory, rows)
    budget = _budget(directory, rows, parent)
    report = {"schema_version": 4, "validated_at_utc": datetime.now(timezone.utc).isoformat(),
              "method": "Bound unchanged parent evidence and baseline solver; independently rescore parent/child routes and promotion gates",
              "evidence_verified": True, "has_valid_results": result["valid_on_budget_runs"] > 0,
              "all_runs_successful": bool(rows) and result["valid_on_budget_runs"] == len(rows)
                  and budget["budget_compliant"] and not budget["aborted_run_count"],
              **result, "manifest_sha256": checks._sha256(manifest_path), "runs_sha256": checks._sha256(rows_path),
              "source_snapshots_sha256": hashes, "execution_sources_sha256": execution_hashes,
              "audit_script_sha256": checks._sha256(Path(__file__)), "equivalence_sha256": None,
              "validated_equivalence_rows": sum(row["stage"] == "equivalence" for row in rows),
              "decision_audit": decisions, "protocol_complete": decisions["protocol_complete"],
              "decisions_sha256": decisions["decisions_sha256"], "cumulative_budget": budget,
              "parent_evidence": {key: value for key, value in parent.items() if key != "manifest"}}
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    report = audit_results(parser.parse_args().output_dir)
    print(f"Audited {report['validated_rows']} compact rows and {report['parent_evidence']['verification']['validated_rows']} parent rows")
    return 0 if report["all_runs_successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
