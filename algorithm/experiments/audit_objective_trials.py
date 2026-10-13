"""Independent evidence audit for the frozen objective-focused trials.

This is a separate protocol, not a relaxation of the October regret audits.
The audit never runs a solver; it reconstructs every saved route from the
official CSV and checks it with the complete evaluator.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any
import zipfile

from uav_dispatch.io import load_tasks_csv
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import audit_optimization_results as checks


def _protocol():
    from algorithm.experiments import run_objective_trials
    return run_objective_trials


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _manifest(manifest: dict) -> None:
    protocol = _protocol()
    if manifest.get("schema_version") != 3 or manifest.get("kind") != "objective_trials":
        raise ValueError("Expected schema 3 objective_trials manifest")
    if manifest.get("baseline_commit") != protocol.BASELINE_COMMIT:
        raise ValueError("Baseline commit violates the frozen objective protocol")
    if manifest.get("baseline_variant") != "baseline":
        raise ValueError("Unexpected baseline variant")
    if manifest.get("seeds") != protocol.SEEDS:
        raise ValueError("Seeds violate the frozen objective protocol")
    saved = manifest.get("protocol")
    expected = {
        "stage_cases": protocol.CASES,
        "stage_budgets_seconds": protocol.BUDGETS,
        "stage_caps_seconds": protocol.CAPS,
        "total_experiment_seconds": protocol.TOTAL_SECONDS,
        "reserved_seconds": protocol.RESERVED_SECONDS,
    }
    if not isinstance(saved, dict) or any(saved.get(key) != value for key, value in expected.items()):
        raise ValueError("Budgets or cases violate the frozen objective protocol")
    for seeds in manifest["seeds"].values():
        if not isinstance(seeds, list) or any(type(seed) is not int for seed in seeds):
            raise ValueError("Seeds must be integer lists")


def _sources(directory: Path, manifest: dict) -> tuple[dict, dict]:
    protocol = _protocol()
    commit = manifest["baseline_commit"]
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("Invalid baseline commit")
    checks._git("cat-file", "-e", f"{commit}^{{commit}}")
    baseline_files = {
        name.removeprefix("algorithm/src/"): checks._git("show", f"{commit}:{name}")
        for name in checks._git("ls-tree", "-r", "--name-only", commit, "--", "algorithm/src/uav_dispatch").decode().splitlines()
        if name.endswith(".py")
    }
    snapshots = manifest.get("source_snapshots")
    if not baseline_files or not isinstance(snapshots, dict) or set(snapshots) != set(protocol.VARIANTS):
        raise ValueError("Incomplete source snapshot variants")
    hashes = {}
    for variant, flags in protocol.VARIANTS.items():
        source = snapshots[variant]
        if not isinstance(source, dict) or source.get("config") != flags or any(
            type(source["config"][key]) is not type(value) for key, value in flags.items()
        ):
            raise ValueError("Source configuration violates the frozen variant")
        expected_ref = commit if variant == "baseline" else "working_tree"
        if source.get("source_ref") != expected_ref:
            raise ValueError("Source reference disagrees with variant")
        path = checks._within(directory, source.get("path"), f"source {variant}")
        fingerprint = checks._fingerprint(source.get("sha256"), f"source {variant}")
        if checks._sha256(path) != fingerprint:
            raise ValueError("Source snapshot fingerprint mismatch")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or any(
                not name.startswith("uav_dispatch/") or not name.endswith(".py")
                or len(Path(name).parts) != 2 or ".." in Path(name).parts for name in names
            ):
                raise ValueError("Unsafe or duplicate source archive members")
            files = {name: archive.read(name) for name in names}
        if set(files) != set(baseline_files):
            raise ValueError("Solver module set differs from baseline")
        if variant == "baseline" and files != baseline_files:
            raise ValueError("Baseline archive differs from Git objects")
        hashes[variant] = fingerprint
    required = {"run_optimization_experiments", "run_scenario_comparison", "run_objective_trials"}
    execution = manifest.get("execution_sources")
    if not isinstance(execution, dict) or set(execution) != required:
        raise ValueError("Incomplete frozen execution sources")
    execution_hashes = {}
    for name, source in execution.items():
        path = checks._within(directory, source.get("path"), name)
        fingerprint = checks._fingerprint(source.get("sha256"), name)
        if checks._sha256(path) != fingerprint:
            raise ValueError("Execution source fingerprint mismatch")
        execution_hashes[name] = fingerprint
    if manifest.get("runner_sha256") != execution_hashes["run_optimization_experiments"]:
        raise ValueError("Worker runner fingerprint mismatch")
    return hashes, execution_hashes


def _cell(row: dict) -> tuple:
    return tuple(row.get(key) for key in ("stage", "case_id", "variant", "seed"))


def _row_protocol(row: dict) -> None:
    protocol = _protocol()
    stage, case, variant, seed = _cell(row)
    if (stage not in protocol.SEEDS or case not in protocol.CASES[stage]
            or variant not in protocol.VARIANTS or type(seed) is not int
            or seed not in protocol.SEEDS[stage]):
        raise ValueError("Run cell violates the frozen objective protocol")
    if row.get("budget_seconds") != protocol.BUDGETS[stage][case]:
        raise ValueError("Run budget violates the frozen objective protocol")
    if row.get("scenario") != "pure_direct":
        raise ValueError("Objective trials must use the original pure_direct case")


def _configuration(row: dict, payload: dict) -> None:
    protocol = _protocol()
    expected = {
        "max_iterations": 200 if row["stage"] == "equivalence" else 10_000_000,
        "time_limit_seconds": max(1e-6, row["budget_seconds"] - row["construction_seconds"] - 1.0),
        "seed": row["seed"], "candidate_limit": 48, "relay_enabled": False,
        "relay_direct_warmup_fraction": .8, "enable_home_seed": False,
        "enable_home_bias": False, **protocol.VARIANTS[row["variant"]],
    }
    config = row.get("config")
    if not isinstance(config, dict) or set(config) != set(expected) or any(
        config[key] != value or type(config[key]) is not type(value)
        for key, value in expected.items() if key != "time_limit_seconds"
    ):
        raise ValueError("Run configuration disagrees with frozen variant")
    checks._same(expected["time_limit_seconds"], config["time_limit_seconds"], "configuration time limit")
    metadata = payload.get("metadata", {})
    for key in ("seed", "relay_enabled", "enable_home_seed", *protocol.VARIANTS[row["variant"]]):
        if metadata.get(key) != expected[key] or type(metadata.get(key)) is not type(expected[key]):
            raise ValueError(f"Solution metadata disagrees with configuration: {key}")
    checks._same(expected["time_limit_seconds"], metadata.get("time_limit_seconds"), "metadata time limit")
    if row["iterations"] > expected["max_iterations"]:
        raise ValueError("Iteration count exceeds configuration")


def _budget(directory: Path, rows: list[dict]) -> dict:
    protocol = _protocol()
    notes_path = directory / "execution_notes.json"
    notes = _read(notes_path) if notes_path.exists() else {}
    attempts = notes.get("aborted_runs", [])
    if not isinstance(attempts, list):
        raise ValueError("Invalid aborted attempt list")
    cells = {_cell(row) for row in rows}
    stages = {stage: {"completed_seconds": 0., "aborted_seconds": 0., "cap_seconds": cap}
              for stage, cap in protocol.CAPS.items()}
    for row in rows:
        stages[row["stage"]]["completed_seconds"] += row["end_to_end_seconds"]
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError("Invalid aborted attempt")
        stage, case, _, _ = _cell(attempt)
        if stage not in protocol.SEEDS or case not in protocol.CASES[stage]:
            raise ValueError("Aborted cell violates the frozen protocol")
        _row_protocol({**attempt, "budget_seconds": protocol.BUDGETS[stage][case], "scenario": "pure_direct"})
        if _cell(attempt) in cells:
            raise ValueError("Duplicate completed or aborted experiment cell")
        cells.add(_cell(attempt))
        stages[stage]["aborted_seconds"] += checks._number(attempt.get("process_elapsed_seconds"), "aborted elapsed seconds")
    for bucket in stages.values():
        bucket["total_seconds"] = bucket["completed_seconds"] + bucket["aborted_seconds"]
    total = sum(bucket["total_seconds"] for bucket in stages.values())
    violations = [f"Stage cap exceeded: {stage}" for stage, bucket in stages.items()
                  if bucket["total_seconds"] > bucket["cap_seconds"]]
    if total > protocol.TOTAL_SECONDS:
        violations.append("Total experiment budget exceeded")
    if total > protocol.TOTAL_SECONDS - protocol.RESERVED_SECONDS:
        violations.append("Verification reserve was consumed")
    return {"stages": stages, "total_seconds": total, "aborted_run_count": len(attempts),
            "budget_compliant": not violations, "violations": violations,
            "total_budget_seconds": protocol.TOTAL_SECONDS, "reserved_seconds": protocol.RESERVED_SECONDS,
            "execution_notes_sha256": checks._sha256(notes_path) if notes_path.exists() else None}


def _score(row: dict) -> tuple:
    return tuple(row[field] for field in checks.SCORE_FIELDS)


def _pairs(rows: list[dict], stage: str, variant: str, case: str = "fleet200") -> list[tuple[dict, dict]]:
    selected = [row for row in rows if row["stage"] == stage and row["case_id"] == case
                and row["variant"] in {"baseline", variant}
                and row["valid"] and row["budget_compliant"]]
    index = {(row["variant"], row["seed"]): row for row in selected}
    return [(index["baseline", seed], index[variant, seed]) for seed in _protocol().SEEDS[stage]
            if ("baseline", seed) in index and (variant, seed) in index]


def _complete(rows: list[dict], stage: str, variant: str) -> bool:
    protocol = _protocol()
    return all({baseline["seed"] for baseline, _ in _pairs(rows, stage, variant, case)} == set(protocol.SEEDS[stage])
               for case in protocol.CASES[stage])


def _stable_search(directory: Path, row: dict) -> dict:
    """Independently rebuild the original search-decision record, excluding clocks."""
    payload = _read(checks._within(directory, row["solution_file"], "equivalence solution"))
    metadata = payload["metadata"]
    original = ("uses", "accepted", "current_improvements", "best_improvements", "total_reward")
    return {
        "routes": [route["encoded_visits"] for route in payload["routes"]], "score": _score(row),
        "iterations": row["iterations"], "trajectory": [[point[0], *point[2:]] for point in row["best_trajectory"]],
        "statistics": {name: {key: stats[key] for key in original}
                       for name, stats in metadata["operator_statistics"].items()},
        "weights": metadata["operator_weights"], "accepted": metadata["accepted_solutions"],
    }


def _equivalence(directory: Path, rows: list[dict]) -> dict[str, bool]:
    return {variant: _complete(rows, "equivalence", variant) and all(
        baseline["iterations"] == candidate["iterations"] == 200
        and _stable_search(directory, baseline) == _stable_search(directory, candidate)
        for baseline, candidate in _pairs(rows, "equivalence", variant, "single25")
    ) for variant in _protocol().VARIANTS if variant != "baseline"}


def _quality_gates(rows: list[dict], equivalent: dict[str, bool]) -> dict:
    """Recompute prespecified gates without invoking runner gate functions."""
    candidates = [variant for variant in _protocol().VARIANTS if variant != "baseline"]
    screen, medium, confirm, ranks = {}, {}, {}, {}
    for variant in candidates:
        screen_pairs = _pairs(rows, "screen", variant)
        screen[variant] = bool(equivalent[variant] and _complete(rows, "screen", variant)
            and sum(_score(candidate) < _score(baseline) for baseline, candidate in screen_pairs) >= 1
            and sum(candidate["late_count"] - baseline["late_count"] for baseline, candidate in screen_pairs) <= 0
            and all(_score(candidate) <= _score(baseline) for baseline, candidate in _pairs(rows, "screen", variant, "single25")))
        medium_pairs = _pairs(rows, "medium", variant)
        win_count = sum(_score(candidate) < _score(baseline) for baseline, candidate in medium_pairs)
        medium[variant] = bool(_complete(rows, "medium", variant) and win_count >= 2
            and all(candidate["late_count"] <= baseline["late_count"] for baseline, candidate in medium_pairs)
            and all(_score(candidate) <= _score(baseline) for baseline, candidate in _pairs(rows, "medium", variant, "single25")))
        ranks[variant] = (win_count,
            sum(baseline["late_count"] - candidate["late_count"] for baseline, candidate in medium_pairs),
            sum(candidate["late_count"] == baseline["late_count"] and _score(candidate) < _score(baseline)
                for baseline, candidate in medium_pairs), variant == "boundary")
        confirm_pairs = _pairs(rows, "confirm", variant)
        confirm[variant] = bool(medium[variant] and _complete(rows, "confirm", variant)
            and all(_score(candidate) <= _score(baseline) for baseline, candidate in confirm_pairs)
            and sum(_score(candidate) < _score(baseline) for baseline, candidate in confirm_pairs
                    if baseline["seed"] != 2026101321) >= 2)
    eligible = [variant for variant in candidates if screen[variant]]
    qualified = [variant for variant in eligible if medium[variant]]
    selected = max(qualified, key=ranks.__getitem__) if qualified else None
    return {"equivalence": equivalent, "screen": screen, "eligible_variants": eligible,
            "medium": medium, "selected_variant": selected, "confirm": confirm,
            "default_eligible": bool(selected and confirm[selected])}


def _decision_audit(directory: Path, rows: list[dict]) -> dict:
    protocol = _protocol()
    path = directory / "decisions.json"
    decisions = _read(path) if path.exists() else []
    if not isinstance(decisions, list) or any(not isinstance(item, dict) for item in decisions):
        raise ValueError("Invalid stage decisions")
    by_stage = {item.get("stage"): item for item in decisions}
    if len(by_stage) != len(decisions) or not set(by_stage).issubset(protocol.SEEDS):
        raise ValueError("Duplicate or unknown stage decision")
    gates = _quality_gates(rows, _equivalence(directory, rows))
    eligible, selected = gates["eligible_variants"], gates["selected_variant"]

    def check(stage: str, expected: dict, variants: list[str]) -> None:
        if stage not in by_stage:
            return
        decision = by_stage[stage]
        if any(decision.get(key) != value or type(decision.get(key)) is not type(value)
               for key, value in expected.items()):
            raise ValueError(f"Stage decision disagrees with independently recomputed gate: {stage}")
        expected_cells = {(stage, case, variant, seed) for case in protocol.CASES[stage]
                          for seed in protocol.SEEDS[stage] for variant in variants}
        actual_cells = {_cell(row) for row in rows if row["stage"] == stage}
        if actual_cells != expected_cells:
            raise ValueError(f"Completed stage has missing or unexpected experiment cells: {stage}")

    check("equivalence", {"candidates": gates["equivalence"], "passed": all(gates["equivalence"].values())}, list(protocol.VARIANTS))
    check("screen", {"candidates": gates["screen"], "eligible_variants": eligible}, list(protocol.VARIANTS))
    if eligible:
        check("medium", {"selected_variant": selected, "candidates": {variant: gates["medium"][variant] for variant in eligible},
                         "passed": selected is not None}, ["baseline", *eligible])
    else:
        check("medium", {"selected_variant": None, "passed": False, "skipped": True}, [])
    if selected:
        check("confirm", {"selected_variant": selected, "passed": gates["confirm"][selected],
                          "completed_pairs": len(_pairs(rows, "confirm", selected)), "planned_pairs": 4}, ["baseline", selected])
    else:
        check("confirm", {"selected_variant": None, "passed": False, "skipped": True}, [])
    for stage, permitted in (("medium", {"baseline", *eligible} if eligible else set()),
                             ("confirm", {"baseline", selected} if selected else set())):
        if any(row["variant"] not in permitted for row in rows if row["stage"] == stage):
            raise ValueError(f"Unqualified candidate was tested in stage: {stage}")
    # A later stage cannot certify completion while an earlier stage has no decision.
    order = ("equivalence", "screen", "medium", "confirm")
    for index, stage in enumerate(order):
        if stage in by_stage and any(prior not in by_stage for prior in order[:index]):
            raise ValueError("Stage decision is missing a prerequisite")
    return {**gates, "protocol_complete": set(by_stage) == set(order),
            "decisions_sha256": checks._sha256(path) if path.exists() else None}


def audit_results(output_dir: Path) -> dict:
    directory = Path(output_dir).resolve()
    target = directory / "validation.json"
    target.unlink(missing_ok=True)
    manifest_path, rows_path = directory / "manifest.json", directory / "runs.json"
    manifest, rows = _read(manifest_path), _read(rows_path)
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ValueError("Expected manifest object and runs list")
    _manifest(manifest)
    source_hashes, execution_hashes = _sources(directory, manifest)
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
        _row_protocol(row)
        if _cell(row) in cells:
            raise ValueError("Duplicate experiment cell")
        cells.add(_cell(row))
        if row.get("source_sha256") != source_hashes[row["variant"]]:
            raise ValueError("Run source_sha256 disagrees with snapshot")
        pair = (row["stage"], row["case_id"], row["seed"])
        fingerprint = checks._fingerprint(row.get("initial_routes_sha256"), "initial routes")
        if initial_hashes.setdefault(pair, fingerprint) != fingerprint:
            raise ValueError("Paired variants must share the same initial routes")
        problem = checks._case_problem(row, tasks)
        solution = checks._within(directory, row.get("solution_file"), "solution")
        solution_hash = checks._fingerprint(row.get("solution_sha256"), "solution")
        if checks._sha256(solution) != solution_hash:
            raise ValueError("Saved solution fingerprint mismatch")
        payload = _read(solution)
        routes = payload.get("routes", [])
        if [route.get("drone_id") for route in routes] != list(range(1, problem.drone_count + 1)):
            raise ValueError("Missing, duplicate or reordered drone IDs")
        cache_key = (row["case_id"], solution_hash)
        if cache_key not in evaluations:
            evaluations[cache_key] = evaluate_solution(problem, [route["encoded_visits"] for route in routes], _force_full=True)
        evaluation = evaluations[cache_key]
        checks._audit_solution(problem, payload, row, evaluation)
        if row.get("violations") != list(evaluation.violations):
            raise ValueError("Run violations disagree with independent evaluator")
        compliant = checks._audit_runtime(row, payload)
        _configuration(row, payload)
        checks._audit_trajectory(row, payload)
        checks._audit_decisions(row, payload, required=True)
        if not evaluation.valid:
            invalid.append(index)
        if not compliant:
            over_budget.append(index)
    budget = _budget(directory, rows)
    decisions = _decision_audit(directory, rows)
    successful = bool(rows) and not invalid and not over_budget and budget["budget_compliant"] and not budget["aborted_run_count"]
    report = {
        "schema_version": 3, "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "Frozen protocol/source/input checks and independent complete route rescoring; no solver run",
        "evidence_verified": True, "has_valid_results": any(index not in invalid and index not in over_budget for index in range(len(rows))),
        "all_runs_successful": successful, "validated_rows": len(rows),
        "validated_unique_solutions": len(evaluations), "validated_equivalence_rows": sum(row["stage"] == "equivalence" for row in rows),
        "valid_on_budget_runs": len(rows) - len(set(invalid + over_budget)),
        "invalid_rows": invalid, "over_budget_rows": over_budget,
        "manifest_sha256": checks._sha256(manifest_path), "runs_sha256": checks._sha256(rows_path),
        "equivalence_sha256": None,
        "input_sha256": input_hash, "source_snapshots_sha256": source_hashes,
        "execution_sources_sha256": execution_hashes,
        "audit_script_sha256": checks._sha256(Path(__file__)), "cumulative_budget": budget,
        "decision_audit": decisions, "protocol_complete": decisions["protocol_complete"],
        "decisions_sha256": decisions["decisions_sha256"],
    }
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    report = audit_results(args.output_dir)
    print(f"Audited {report['validated_rows']} rows; valid and within budget: {report['valid_on_budget_runs']}")
    return 0 if report["all_runs_successful"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
