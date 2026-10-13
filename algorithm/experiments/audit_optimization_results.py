"""Independently verify the bounded October optimization experiment.

Unlike the paper audit, this audit binds each run to its archived solver.
It does not relax or change the original paper's source fingerprint checks.
Usage: python -m algorithm.experiments.audit_optimization_results --output-dir PATH
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments.run_scenario_comparison import (
    DEFAULT_INPUT,
    ROOT,
    SCENARIOS,
    build_scenario_problem,
)

DEFAULT_OUTPUT = ROOT / "algorithm/results/optimization_20261013"
SCORE_FIELDS = ("late_count", "total_lateness_min", "distance_km")
STAGES = {"screen", "medium", "confirm", "extensions"}
SEEDS = {
    "screen": {2026101301, 2026101302},
    "medium": {2026101311, 2026101312, 2026101313},
    "confirm": {2026101321, 2026101322, 2026101323},
    "extensions": {2026101301, 2026101302},
    "equivalence": {2026101301, 2026101302, 2026101303},
}
TOLERANCE = 1e-8
VARIANT_CONFIG = {
    "baseline": {},
    "fast": {"enable_lazy_repair": True, "enable_route_distinct_regret": False},
    "allocation": {"enable_lazy_repair": True, "enable_route_distinct_regret": True},
}
# A separate, prespecified protocol: do not make the original paper/first-batch
# rules depend on editable manifest values.
FOLLOWUP_BASELINE_COMMIT = "6afe5f4aee12b8d8fddbcf92c45f306a31749e6f"
FOLLOWUP_VARIANT_CONFIG = {
    "baseline": {},
    "old_allocation": {"enable_lazy_repair": True, "enable_route_distinct_regret": True},
    "fallback": {"enable_lazy_repair": True, "enable_route_distinct_regret": True,
                 "enable_regret_position_fallback": True},
    "portfolio": {"enable_lazy_repair": True, "enable_route_distinct_regret": False,
                  "enable_regret_position_fallback": True, "enable_regret_portfolio": True},
}
FOLLOWUP_SEEDS = {
    "screen": [2026101341, 2026101342],
    "medium": [2026101351, 2026101352, 2026101353],
    # The first seed reproduces a known regression; only the last three are new.
    "confirm": [2026101321, 2026101361, 2026101362, 2026101363],
    "extensions": [2026101341, 2026101342],
}
FOLLOWUP_STAGE_CASES = {
    "screen": ["single25", "fleet200"],
    "medium": ["single25", "fleet200"],
    "confirm": ["fleet200"],
    "extensions": ["fleet200__direct_relay", "fleet200__direct_stations",
                   "fleet200__direct_relay_stations"],
}
FOLLOWUP_STAGE_BUDGETS = {
    "screen": {"single25": 5, "fleet200": 10},
    "medium": {"single25": 5, "fleet200": 30},
    "confirm": {"single25": 120, "fleet200": 240},
    "extensions": {"fleet200": 20},
}
FOLLOWUP_STAGE_CAPS = {"screen": 180, "medium": 240, "confirm": 1920, "extensions": 240}
FOLLOWUP_TOTAL_SECONDS = 3600
FOLLOWUP_RESERVED_SECONDS = 60


def _manifest_schema(manifest: dict[str, Any]) -> int:
    schema = manifest.get("schema_version", 1)
    if type(schema) is not int or schema not in (1, 2):
        raise ValueError("Unsupported experiment schema_version")
    if schema == 2 and manifest.get("kind") != "regret_followup":
        raise ValueError("Schema 2 requires kind=regret_followup")
    return schema


def _variant_config(manifest: dict[str, Any]) -> dict[str, dict[str, bool]]:
    return FOLLOWUP_VARIANT_CONFIG if _manifest_schema(manifest) == 2 else VARIANT_CONFIG


def _audit_manifest_protocol(manifest: dict[str, Any]) -> None:
    if _manifest_schema(manifest) == 1:
        return
    if manifest.get("baseline_commit") != FOLLOWUP_BASELINE_COMMIT:
        raise ValueError("Followup baseline_commit violates the prespecified protocol")
    seeds = manifest.get("seeds")
    if not isinstance(seeds, dict) or seeds != FOLLOWUP_SEEDS or any(
        not isinstance(values, list) or any(type(seed) is not int for seed in values)
        for values in seeds.values()
    ):
        raise ValueError("Followup manifest seeds violate the prespecified protocol")
    protocol = manifest.get("protocol")
    if not isinstance(protocol, dict):
        raise ValueError("Missing followup protocol")
    if protocol.get("stage_cases") != FOLLOWUP_STAGE_CASES:
        raise ValueError("Followup stage_cases violate the prespecified protocol")
    budgets = protocol.get("stage_budgets_seconds")
    if not isinstance(budgets, dict) or budgets != FOLLOWUP_STAGE_BUDGETS or any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for cases in budgets.values() for value in cases.values()
    ):
        raise ValueError("Followup stage_budgets_seconds violate the prespecified protocol")
    caps = protocol.get("stage_caps_seconds")
    if (not isinstance(caps, dict) or caps != FOLLOWUP_STAGE_CAPS
            or any(type(value) is not int for value in caps.values())
            or type(protocol.get("total_experiment_seconds")) is not int
            or protocol["total_experiment_seconds"] != FOLLOWUP_TOTAL_SECONDS):
        raise ValueError("Followup cumulative budgets violate the prespecified protocol")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fingerprint(value: Any, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"Invalid SHA256: {label}")
    return value


def _within(directory: Path, name: Any, label: str) -> Path:
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError(f"Expected relative path: {label}")
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError(f"Missing or escaping file: {label}")
    return path


def _number(value: Any, label: str, *, integer: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid numeric field: {label}")
    if not math.isfinite(value) or value < 0 or (integer and int(value) != value):
        raise ValueError(f"Nonfinite, negative or nonintegral field: {label}")
    return float(value)


def _same(actual: float, saved: Any, label: str) -> None:
    number = _number(saved, label)
    if not math.isclose(actual, number, rel_tol=0, abs_tol=TOLERANCE):
        raise ValueError(f"Recomputed {label} disagrees with saved value")


def _git(*args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, check=False, timeout=15
    )
    if result.returncode:
        raise ValueError("Baseline commit cannot be verified against the repository")
    return result.stdout


def _audit_sources(directory: Path, manifest: dict[str, Any]) -> dict[str, str]:
    schema = _manifest_schema(manifest)
    configurations = _variant_config(manifest)
    commit = manifest.get("baseline_commit")
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("Invalid baseline_commit")
    _git("cat-file", "-e", f"{commit}^{{commit}}")
    snapshots = manifest.get("source_snapshots")
    if not isinstance(snapshots, dict) or "baseline" not in snapshots:
        raise ValueError("Manifest must contain baseline source_snapshots")
    baseline_files = {
        name.removeprefix("algorithm/src/"): _git("show", f"{commit}:{name}")
        for name in _git(
            "ls-tree", "-r", "--name-only", commit, "--", "algorithm/src/uav_dispatch"
        ).decode("utf-8").splitlines()
        if name.endswith(".py")
    }
    if not baseline_files:
        raise ValueError("Baseline commit contains no solver sources")
    hashes: dict[str, str] = {}
    for variant, snapshot in snapshots.items():
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("config"), dict):
            raise ValueError(f"Invalid source snapshot configuration: {variant}")
        if variant not in configurations or snapshot["config"] != configurations[variant] or (
            schema == 2 and any(type(value) is not bool for value in snapshot["config"].values())
        ):
            raise ValueError(f"Source snapshot configuration violates the experiment protocol: {variant}")
        if schema == 2:
            expected_ref = commit if variant in {"baseline", "old_allocation"} else "working_tree"
            if snapshot.get("source_ref") != expected_ref:
                raise ValueError(f"Source snapshot source_ref violates the followup protocol: {variant}")
        path = _within(directory, snapshot.get("path"), f"snapshot {variant}")
        expected = _fingerprint(snapshot.get("sha256"), f"snapshot {variant}")
        if _sha256(path) != expected:
            raise ValueError(f"Source snapshot fingerprint mismatch: {variant}")
        try:
            with zipfile.ZipFile(path) as archive:
                names = archive.namelist()
                if len(names) != len(set(names)) or any(
                    not name.startswith("uav_dispatch/")
                    or not name.endswith(".py")
                    or ".." in Path(name).parts
                    for name in names
                ):
                    raise ValueError(f"Unexpected source archive members: {variant}")
                files = {name: archive.read(name) for name in names}
        except (OSError, zipfile.BadZipFile) as exc:
            raise ValueError(f"Invalid source archive: {variant}") from exc
        if set(files) != set(baseline_files):
            raise ValueError(f"Source archive module set differs from baseline: {variant}")
        if variant == "baseline" and files != baseline_files:
            raise ValueError("Baseline source archive disagrees with baseline_commit")
        if schema == 2 and variant == "old_allocation" and files != baseline_files:
            raise ValueError("Old allocation source archive disagrees with baseline_commit")
        hashes[variant] = expected
    return hashes


def _audit_execution_sources(directory: Path, manifest: dict[str, Any]) -> dict[str, str]:
    sources = manifest.get("execution_sources")
    expected = {"run_optimization_experiments", "run_scenario_comparison"}
    if _manifest_schema(manifest) == 2:
        expected.add("run_regret_followup")
    if "post_hoc_exploration" in manifest:
        _audit_post_hoc_exploration(directory, manifest)
        expected.add("run_regret_portfolio_validation")
    if not isinstance(sources, dict) or set(sources) != expected:
        raise ValueError("Missing frozen experiment runner or scenario builder")
    result = {}
    for name, source in sources.items():
        if not isinstance(source, dict):
            raise ValueError(f"Invalid frozen execution source: {name}")
        path = _within(directory, source.get("path"), f"execution source {name}")
        fingerprint = _fingerprint(source.get("sha256"), f"execution source {name}")
        if _sha256(path) != fingerprint:
            raise ValueError(f"Execution source fingerprint mismatch: {name}")
        result[name] = fingerprint
    if manifest.get("runner_sha256") != result["run_optimization_experiments"]:
        raise ValueError("runner_sha256 disagrees with frozen execution source")
    return result


def _audit_post_hoc_exploration(directory: Path, manifest: dict[str, Any]) -> None:
    """Bind the disclosed supplemental trial to an unchanged, qualified parent."""

    marker = manifest.get("post_hoc_exploration")
    fields = {"reason", "parent_directory", "parent_manifest_sha256", "parent_runs_sha256",
              "parent_decisions_sha256", "candidate"}
    if _manifest_schema(manifest) != 2 or not isinstance(marker, dict) or set(marker) != fields:
        raise ValueError("Invalid post_hoc_exploration marker or schema")
    if (not isinstance(marker["reason"], str) or not marker["reason"].strip()
            or marker["parent_directory"] != ".." or marker["candidate"] != "portfolio"):
        raise ValueError("Invalid post_hoc_exploration reason, parent path or candidate")
    parent = directory.resolve().parent
    documents = {}
    for name in ("manifest", "runs", "decisions"):
        path = _within(parent, f"{name}.json", f"post-hoc parent {name}")
        fingerprint = _fingerprint(marker[f"parent_{name}_sha256"], f"parent_{name}_sha256")
        if _sha256(path) != fingerprint:
            raise ValueError(f"Post-hoc parent {name} fingerprint mismatch")
        documents[name] = json.loads(path.read_text(encoding="utf-8"))
    parent_manifest = documents["manifest"]
    if not isinstance(parent_manifest, dict) or _manifest_schema(parent_manifest) != 2 or "post_hoc_exploration" in parent_manifest:
        raise ValueError("Post-hoc parent must be the original schema-2 followup")
    _audit_manifest_protocol(parent_manifest)
    for key in ("baseline_commit", "input_path", "input_sha256"):
        if parent_manifest.get(key) != manifest.get(key):
            raise ValueError(f"Post-hoc trial changed parent {key}")
    parent_sources = parent_manifest.get("source_snapshots")
    child_sources = manifest.get("source_snapshots")
    if not isinstance(parent_sources, dict) or not isinstance(child_sources, dict) or set(parent_sources) != set(FOLLOWUP_VARIANT_CONFIG) or set(child_sources) != set(FOLLOWUP_VARIANT_CONFIG):
        raise ValueError("Post-hoc trial requires the same four parent variants")
    for variant, config in FOLLOWUP_VARIANT_CONFIG.items():
        before, after = parent_sources[variant], child_sources[variant]
        if not isinstance(before, dict) or not isinstance(after, dict) or any(
            before.get(key) != after.get(key) for key in ("sha256", "config", "source_ref")
        ) or before.get("config") != config or any(type(value) is not bool for value in before["config"].values()):
            raise ValueError(f"Post-hoc trial changed parent variant {variant}")
        parent_snapshot = _within(parent, before.get("path"), f"parent variant {variant}")
        if _sha256(parent_snapshot) != _fingerprint(before.get("sha256"), f"parent variant {variant}"):
            raise ValueError(f"Post-hoc parent variant fingerprint mismatch: {variant}")
    original_execution = {"run_optimization_experiments", "run_scenario_comparison", "run_regret_followup"}
    execution = parent_manifest.get("execution_sources")
    if not isinstance(execution, dict) or set(execution) != original_execution:
        raise ValueError("Post-hoc parent has an unexpected execution protocol")
    for name in original_execution:
        before = execution[name]
        after = manifest.get("execution_sources", {}).get(name)
        if not isinstance(before, dict) or not isinstance(after, dict) or before.get("sha256") != after.get("sha256"):
            raise ValueError(f"Post-hoc trial changed parent execution source {name}")
        path = _within(parent, before.get("path"), f"parent execution source {name}")
        if _sha256(path) != _fingerprint(before.get("sha256"), f"parent execution source {name}"):
            raise ValueError(f"Post-hoc parent execution source fingerprint mismatch: {name}")
    rows = documents["runs"]
    decisions = documents["decisions"]
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows) or not isinstance(decisions, list) or not all(isinstance(item, dict) for item in decisions):
        raise ValueError("Invalid post-hoc parent runs or decisions")

    def pairs(stage: str, variant: str, case: str) -> list[tuple[dict, dict]]:
        selected = [row for row in rows if row.get("stage") == stage and row.get("case_id") == case
                    and row.get("variant") in {"baseline", variant}]
        by_cell = {(row.get("variant"), row.get("seed")): row for row in selected}
        expected_cells = {(name, seed) for name in ("baseline", variant) for seed in FOLLOWUP_SEEDS[stage]}
        if len(selected) != len(by_cell) or set(by_cell) != expected_cells:
            raise ValueError(f"Post-hoc parent {stage}/{variant}/{case} pairs are incomplete or duplicated")
        result = []
        for seed in FOLLOWUP_SEEDS[stage]:
            b, c = by_cell[("baseline", seed)], by_cell[(variant, seed)]
            for row in (b, c):
                _audit_protocol(row, equivalence=False, manifest=parent_manifest)
                if row.get("valid") is not True or row.get("budget_compliant") is not True:
                    raise ValueError("Post-hoc parent qualification requires valid, within-budget paired runs")
                if row.get("source_sha256") != parent_sources[row["variant"]]["sha256"]:
                    raise ValueError("Post-hoc parent qualification source differs from its snapshot")
                _score([row.get(field) for field in SCORE_FIELDS], "parent qualification score")
                _fingerprint(row.get("initial_routes_sha256"), "parent initial routes")
            for key in ("initial_routes_sha256", "budget_seconds", "scenario", "task_count", "drone_count"):
                if key not in b or key not in c or b[key] != c[key]:
                    raise ValueError(f"Post-hoc parent pairing conditions disagree: {key}")
            task_count, drone_count = (25, 1) if case == "single25" else (200, 8)
            if b["scenario"] != "pure_direct" or (b["task_count"], b["drone_count"]) != (task_count, drone_count):
                raise ValueError("Post-hoc parent qualification uses the wrong original case")
            result.append((b, c))
        return result

    def objective(row: dict) -> tuple:
        return tuple(row[field] for field in SCORE_FIELDS)

    fleet_screen = pairs("screen", "portfolio", "fleet200")
    single_screen = pairs("screen", "portfolio", "single25")
    if not any(objective(c) < objective(b) for b, c in fleet_screen) or not all(
        objective(c) <= objective(b) for b, c in fleet_screen + single_screen
    ):
        raise ValueError("Post-hoc parent portfolio did not pass the original screening gate")
    fleet_medium = pairs("medium", "fallback", "fleet200")
    single_medium = pairs("medium", "fallback", "single25")
    fallback_passed = sum(objective(c) < objective(b) for b, c in fleet_medium) >= 2 and all(
        c["late_count"] <= b["late_count"] for b, c in fleet_medium + single_medium
    )
    medium_decisions = [item for item in decisions if item.get("stage") == "medium"]
    if (fallback_passed or len(medium_decisions) != 1 or medium_decisions[0].get("selected_variant") != "fallback"
            or medium_decisions[0].get("passed") is not False):
        raise ValueError("Post-hoc parent fallback medium failure is not supported by its runs and decision")


def _audit_followup_budget(directory: Path, manifest: dict[str, Any],
                           rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Account for every recorded attempt, including failed processes and the probe.

    Per-stage caps belong to a cohort; the disclosed supplemental cohort shares
    its parent's one-hour total. An honest overrun remains auditable evidence,
    but it cannot receive a successful-experiment status.
    """
    if _manifest_schema(manifest) != 2:
        return None
    supplemental = "post_hoc_exploration" in manifest
    cohorts = [("current", directory, manifest, rows)]
    if supplemental:
        parent = directory.parent
        cohorts.insert(0, ("parent", parent,
                           json.loads((parent / "manifest.json").read_text(encoding="utf-8")),
                           json.loads((parent / "runs.json").read_text(encoding="utf-8"))))
    summaries, violations = [], []
    for label, root, cohort_manifest, cohort_rows in cohorts:
        _audit_manifest_protocol(cohort_manifest)
        stages = {stage: {"completed_runs": 0, "completed_seconds": 0.0,
                          "aborted_runs": 0, "aborted_seconds": 0.0,
                          "cap_seconds": cap}
                  for stage, cap in FOLLOWUP_STAGE_CAPS.items()}
        cells = set()
        for row in cohort_rows:
            _audit_protocol(row, equivalence=False, manifest=cohort_manifest)
            cell = (row["stage"], row.get("case_id"), row.get("variant"), row["seed"])
            if cell in cells:
                raise ValueError("Duplicate completed cell in cumulative budget evidence")
            cells.add(cell)
            bucket = stages[row["stage"]]
            bucket["completed_runs"] += 1
            bucket["completed_seconds"] += _number(row.get("end_to_end_seconds"), "cumulative completed seconds")
        notes_path = root / "execution_notes.json"
        aborted = []
        if notes_path.is_file():
            notes = json.loads(notes_path.read_text(encoding="utf-8"))
            if not isinstance(notes, dict) or not isinstance(notes.get("aborted_runs", []), list):
                raise ValueError("Invalid cumulative budget execution_notes")
            aborted = notes.get("aborted_runs", [])
        for attempt in aborted:
            if not isinstance(attempt, dict):
                raise ValueError("Invalid aborted attempt in cumulative budget evidence")
            stage, case = attempt.get("stage"), attempt.get("case_id")
            if stage not in FOLLOWUP_STAGE_CASES or case not in FOLLOWUP_STAGE_CASES[stage]:
                raise ValueError("Aborted attempt is outside the cumulative budget protocol")
            budget_case = "fleet200" if stage == "extensions" else case
            _audit_protocol({**attempt, "budget_seconds": FOLLOWUP_STAGE_BUDGETS[stage][budget_case]},
                            equivalence=False, manifest=cohort_manifest)
            if attempt.get("variant") not in FOLLOWUP_VARIANT_CONFIG:
                raise ValueError("Invalid aborted variant in cumulative budget evidence")
            cell = (stage, case, attempt["variant"], attempt["seed"])
            if cell in cells:
                raise ValueError("An aborted cell was duplicated or rerun in cumulative budget evidence")
            cells.add(cell)
            stages[stage]["aborted_runs"] += 1
            stages[stage]["aborted_seconds"] += _number(
                attempt.get("process_elapsed_seconds"), "aborted process_elapsed_seconds")
        for stage, bucket in stages.items():
            bucket["total_seconds"] = bucket["completed_seconds"] + bucket["aborted_seconds"]
            bucket["budget_compliant"] = bucket["total_seconds"] <= bucket["cap_seconds"] + 1e-9
            if not bucket["budget_compliant"]:
                violations.append(f"{label}:{stage} exceeded its stage cap")
        summaries.append({
            "cohort": label, "directory": "." if label == "current" else "..", "stages": stages,
            "completed_seconds": sum(item["completed_seconds"] for item in stages.values()),
            "aborted_seconds": sum(item["aborted_seconds"] for item in stages.values()),
            "aborted_run_count": len(aborted),
            "total_seconds": sum(item["total_seconds"] for item in stages.values()),
            "runs_sha256": _sha256(root / "runs.json"),
            "execution_notes_sha256": _sha256(notes_path) if notes_path.is_file() else None,
        })
    probe_root = directory.parent if supplemental else directory
    probe_path = probe_root / "diagnostics/local_repair_probe.json"
    probe_seconds = 0.0
    if probe_path.is_file():
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        if not isinstance(probe, dict):
            raise ValueError("Invalid local probe cumulative budget evidence")
        probe_seconds = _number(probe.get("elapsed_seconds"), "local probe elapsed_seconds")
    total = sum(cohort["total_seconds"] for cohort in summaries) + probe_seconds
    execution_limit = FOLLOWUP_TOTAL_SECONDS - FOLLOWUP_RESERVED_SECONDS
    total_compliant = total <= FOLLOWUP_TOTAL_SECONDS + 1e-9
    reserve_preserved = total <= execution_limit + 1e-9
    if not total_compliant:
        violations.append("Shared parent/supplement experiment exceeded 3600 seconds")
    if not reserve_preserved:
        violations.append("The 60-second verification reserve was not preserved")
    return {
        "scope": "parent_and_supplement_and_probe" if supplemental else "this_batch_and_probe",
        "accounting": "Completed runs use end-to-end seconds; no-result attempts use process elapsed seconds; the local repair probe is counted once. Development, report production and independent audit time are not included.",
        "stage_caps_apply_per_cohort": True, "shared_total_limit_seconds": FOLLOWUP_TOTAL_SECONDS,
        "verification_reserve_seconds": FOLLOWUP_RESERVED_SECONDS,
        "execution_limit_seconds": execution_limit, "cohorts": summaries,
        "local_probe_seconds": probe_seconds,
        "local_probe_file": ("../" if supplemental else "") + "diagnostics/local_repair_probe.json" if probe_path.is_file() else None,
        "local_probe_sha256": _sha256(probe_path) if probe_path.is_file() else None,
        "total_seconds": total, "remaining_total_seconds": FOLLOWUP_TOTAL_SECONDS - total,
        "total_budget_compliant": total_compliant, "verification_reserve_preserved": reserve_preserved,
        "budget_compliant": not violations, "violations": violations,
        "aborted_run_count": sum(cohort["aborted_run_count"] for cohort in summaries),
    }


def _case_problem(row: dict[str, Any], tasks: tuple) -> Problem:
    case_id = row.get("case_id")
    scenario_id = row.get("scenario")
    if case_id in ("single25", "fleet200"):
        expected_tasks, expected_drones = (25, 1) if case_id == "single25" else (200, 8)
        if scenario_id != "pure_direct":
            raise ValueError("Original cases must use pure_direct")
    elif isinstance(case_id, str) and case_id.startswith("fleet200__"):
        expected_tasks, expected_drones = 200, 8
        if case_id != f"fleet200__{scenario_id}" or scenario_id not in {
            "direct_relay", "direct_stations", "direct_relay_stations"
        }:
            raise ValueError("Unknown extension case/scenario")
    else:
        raise ValueError("Unknown original or extension case")
    if row.get("task_count") != expected_tasks or row.get("drone_count") != expected_drones:
        raise ValueError("Case task_count/drone_count violates the fixed problem definition")
    selected = tasks[:expected_tasks]
    if len(selected) != expected_tasks:
        raise ValueError("Input does not contain all declared tasks")
    if scenario_id == "pure_direct":
        return Problem(selected, drone_count=expected_drones, max_tasks_per_drone=25)
    scenario = next(item for item in SCENARIOS if item["id"] == scenario_id)
    return build_scenario_problem(
        selected, {**scenario, "warmup": 0.8}, drones=expected_drones,
        max_tasks=25, relay_count=4, relay_location_seed=42,
    )[0]


def _audit_runtime(row: dict[str, Any], payload: dict[str, Any]) -> bool:
    total = _number(row.get("end_to_end_seconds"), "end_to_end_seconds")
    phases = [
        _number(row.get(field), field) for field in (
            "construction_seconds", "search_seconds", "validation_seconds", "serialization_seconds"
        )
    ]
    if sum(phases) > total + TOLERANCE:
        raise ValueError("Runtime phase sum exceeds end_to_end_seconds")
    budget = row.get("budget_seconds")
    compliant = budget is None or total <= _number(budget, "budget_seconds") + 1e-9
    if budget is not None and budget <= 0:
        raise ValueError("budget_seconds must be positive")
    if row.get("budget_compliant") is not compliant:
        raise ValueError("budget_compliant disagrees with measured end-to-end time")
    if _number(payload.get("runtime_seconds"), "solution runtime_seconds") > phases[1] + TOLERANCE:
        raise ValueError("Solution runtime exceeds outer search_seconds")
    if payload.get("iterations") != row.get("iterations"):
        raise ValueError("Saved iterations disagree with row")
    _number(row.get("iterations"), "iterations", integer=True)
    metadata = payload.get("metadata", {})
    if "seed" in metadata and metadata["seed"] != row["seed"]:
        raise ValueError("Saved seed disagrees with row")
    return compliant


def _audit_protocol(row: dict[str, Any], *, equivalence: bool,
                    manifest: dict[str, Any] | None = None) -> None:
    if manifest is not None and _manifest_schema(manifest) == 2:
        stage = row.get("stage")
        if equivalence or stage not in STAGES:
            raise ValueError("Invalid followup experiment row or stage")
        if type(row.get("seed")) is not int or row["seed"] not in FOLLOWUP_SEEDS[stage]:
            raise ValueError("Seed violates the prespecified followup protocol")
        case_id = row.get("case_id")
        if case_id not in FOLLOWUP_STAGE_CASES[stage]:
            raise ValueError("Case does not belong to the declared followup experiment stage")
        budget_case = "fleet200" if stage == "extensions" else case_id
        expected = FOLLOWUP_STAGE_BUDGETS[stage][budget_case]
        if row.get("budget_seconds") != expected:
            raise ValueError("Budget violates the prespecified followup protocol")
        return
    stage = row.get("stage")
    if stage not in ({"equivalence"} if equivalence else STAGES):
        raise ValueError("Invalid experiment row or stage")
    if row.get("seed") not in SEEDS[stage]:
        raise ValueError("Seed violates the prespecified experiment protocol")
    extension_case = str(row.get("case_id", "")).startswith("fleet200__")
    if extension_case != (stage == "extensions"):
        raise ValueError("Case does not belong to the declared experiment stage")
    if equivalence:
        if row.get("budget_seconds") is not None or row.get("iterations") != 200:
            raise ValueError("Fixed-iteration equivalence must run 200 iterations without a time cap")
        return
    expected = {"screen": 5 if row.get("case_id") == "single25" else 10,
                "medium": 30, "confirm": 120 if row.get("case_id") == "single25" else 240,
                "extensions": 20}[stage]
    if row.get("budget_seconds") != expected:
        raise ValueError("Budget violates the prespecified experiment protocol")


def _audit_configuration(row: dict[str, Any], payload: dict[str, Any],
                         manifest: dict[str, Any] | None = None) -> None:
    relay = row["scenario"] in {"direct_relay", "direct_relay_stations"}
    homes = row["scenario"] in {"direct_stations", "direct_relay_stations"}
    budget = row["budget_seconds"]
    expected = {
        "max_iterations": 200 if row["stage"] == "equivalence" else 10_000_000,
        "time_limit_seconds": None if budget is None else max(1e-6, budget - row["construction_seconds"] - 1.0),
        "seed": row["seed"], "candidate_limit": 48, "relay_enabled": relay,
        "relay_direct_warmup_fraction": 0.8, "enable_home_seed": homes,
        "enable_home_bias": homes, **_variant_config(manifest or {})[row["variant"]],
    }
    config = row.get("config")
    if not isinstance(config, dict) or set(config) != set(expected) or any(
        config[field] != value for field, value in expected.items() if field != "time_limit_seconds"
    ) or (
        manifest is not None and _manifest_schema(manifest) == 2 and any(
            type(config[field]) is not type(value)
            for field, value in expected.items() if field != "time_limit_seconds"
        )
    ):
        raise ValueError("Run configuration disagrees with variant, budget or fixed search protocol")
    if budget is None:
        if config["time_limit_seconds"] is not None:
            raise ValueError("Fixed-iteration configuration must have no time cap")
    else:
        _same(expected["time_limit_seconds"], config["time_limit_seconds"], "configuration time limit")
    metadata = payload.get("metadata", {})
    for field in ("seed", "time_limit_seconds", "relay_enabled", "enable_home_seed"):
        if field == "time_limit_seconds" and budget is not None:
            _same(expected[field], metadata.get(field), "metadata time limit")
            continue
        if metadata.get(field) != expected[field]:
            raise ValueError(f"Saved solution metadata disagrees with run configuration: {field}")
    if manifest is not None and _manifest_schema(manifest) == 2:
        for field, value in _variant_config(manifest)[row["variant"]].items():
            if type(metadata.get(field)) is not bool or metadata[field] is not value:
                raise ValueError(f"Saved solution feature metadata disagrees with run configuration: {field}")
    if relay and (metadata.get("staged_relay") is not True or metadata.get("relay_direct_warmup_fraction") != 0.8):
        raise ValueError("Relay execution must preserve the 80% direct warmup")


def _value_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _audit_decisions(row: dict[str, Any], payload: dict[str, Any], *, required: bool) -> None:
    if not required and "decision_fingerprint" not in row:
        return
    metadata = payload.get("metadata", {})
    expected = _value_hash({
        "routes": [route["encoded_visits"] for route in payload["routes"]],
        "score": payload["score"], "iterations": payload["iterations"],
        "trajectory": [[p[0], *p[2:]] for p in row["best_trajectory"]],
        "operator_statistics": metadata.get("operator_statistics", {}),
        "operator_weights": metadata.get("operator_weights", {}),
        "accepted_solutions": metadata.get("accepted_solutions"),
    })
    if row.get("decision_fingerprint") != expected:
        raise ValueError("Decision fingerprint disagrees with saved search decisions")


def _equivalence_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    observations = report.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("Missing equivalence observations")
    cells = set()
    rows = []
    for observation in observations:
        case, seed = observation.get("case_id"), observation.get("seed")
        if case not in {"single25", "fleet200"} or seed not in SEEDS["equivalence"] or (case, seed) in cells:
            raise ValueError("Invalid or duplicate equivalence observation")
        cells.add((case, seed))
        for variant in ("baseline", "fast"):
            row = observation.get(variant)
            if not isinstance(row, dict) or (row.get("case_id"), row.get("seed"), row.get("variant")) != (case, seed, variant):
                raise ValueError("Equivalence observation disagrees with nested run")
            _same(row["end_to_end_seconds"], observation.get(f"{variant}_seconds"), "equivalence runtime")
            rows.append(row)
        equal = observation["baseline"].get("decision_fingerprint") == observation["fast"].get("decision_fingerprint")
        if observation.get("equivalent") is not equal:
            raise ValueError("Equivalence flag disagrees with search decisions")
    all_equal = len(observations) == 6 and all(item["equivalent"] for item in observations)
    if report.get("all_equivalent") is not all_equal:
        raise ValueError("Equivalence summary disagrees with observations")
    improvements = {}
    for case in ("single25", "fleet200"):
        matching = [item for item in observations if item["case_id"] == case]
        if not matching:
            raise ValueError("Equivalence report must cover both original cases")
        baseline = statistics.median(item["baseline_seconds"] for item in matching)
        if baseline <= 0:
            raise ValueError("Equivalence runtime must be positive")
        improvement = 1 - statistics.median(item["fast_seconds"] for item in matching) / baseline
        saved = report.get("median_runtime_reduction", {}).get(case)
        if not isinstance(saved, (int, float)) or not math.isfinite(saved) or not math.isclose(improvement, saved, rel_tol=0, abs_tol=TOLERANCE):
            raise ValueError("Equivalence runtime reduction disagrees with observations")
        improvements[case] = improvement
    _same(sum(item["baseline_seconds"] + item["fast_seconds"] for item in observations), report.get("elapsed_seconds"), "equivalence elapsed_seconds")
    if report.get("passed") is not (all_equal and improvements["fleet200"] >= 0.1):
        raise ValueError("Equivalence pass flag disagrees with prespecified 10% speed gate")
    return rows


def _score(values: Any, label: str) -> tuple[float, float, float]:
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise ValueError(f"Invalid score tuple: {label}")
    return tuple(_number(v, label, integer=(i == 0)) for i, v in enumerate(values))


def _audit_trajectory(row: dict[str, Any], payload: dict[str, Any]) -> None:
    trajectory = row.get("best_trajectory")
    if not isinstance(trajectory, list) or not trajectory:
        raise ValueError("Missing best_trajectory")
    previous_score = None
    previous_iteration = previous_time = -1.0
    for point in trajectory:
        if not isinstance(point, (list, tuple)) or len(point) != 5:
            raise ValueError("Invalid best_trajectory point")
        iteration = _number(point[0], "trajectory iteration", integer=True)
        elapsed = _number(point[1], "trajectory time")
        score = _score(point[2:], "trajectory score")
        if iteration < previous_iteration or elapsed < previous_time:
            raise ValueError("Trajectory iteration/time must be nondecreasing")
        if iteration > row["iterations"] or elapsed > row["end_to_end_seconds"] + TOLERANCE:
            raise ValueError("Trajectory exceeds final iteration/runtime")
        if previous_score is not None and score > previous_score:
            raise ValueError("Trajectory lexicographic best score deteriorates")
        previous_score, previous_iteration, previous_time = score, iteration, elapsed
    for actual, field in zip(trajectory[-1][2:], SCORE_FIELDS, strict=True):
        _same(actual, row[field], f"trajectory final {field}")
    saved = payload.get("metadata", {}).get("best_trajectory")
    if saved is not None:
        if len(saved) != len(trajectory):
            raise ValueError("Trajectory length disagrees with saved solution")
        for point, source in zip(trajectory, saved, strict=True):
            expected = [source[0], source[1] + row["construction_seconds"], *source[2:]]
            for actual, recorded in zip(point, expected, strict=True):
                _same(actual, recorded, "trajectory/saved solution")


def _audit_solution(problem: Problem, payload: dict[str, Any], row: dict[str, Any], evaluation: Any) -> None:
    if payload.get("valid") is not evaluation.valid or row.get("valid") is not evaluation.valid:
        raise ValueError("Saved valid flag disagrees with independent evaluation")
    if payload.get("violations") != list(evaluation.violations):
        raise ValueError("Saved violations disagree with independent evaluation")
    expected_problem = {
        "task_count": len(problem.tasks), "drone_count": problem.drone_count,
        "max_tasks_per_drone": 25, "capacity": 2, "speed_km_per_min": 0.9,
        "depot_km": [0.0, 0.0], "open_routes": True, "service_time_min": 0.0,
        "objective_order": list(SCORE_FIELDS),
        "drone_homes": ["origin" if home is None else f"station_{home}" for home in (
            problem.drone_homes if problem.drone_homes is not None else (None,) * problem.drone_count
        )],
    }
    for field, value in expected_problem.items():
        if payload.get("problem", {}).get(field) != value:
            raise ValueError(f"Saved problem configuration mismatch: {field}")
    for field in SCORE_FIELDS:
        actual = getattr(evaluation.score, field)
        _same(actual, row.get(field), f"row {field}")
        _same(actual, payload.get("score", {}).get(field), f"solution {field}")
    delivered = len(evaluation.delivery_times_min)
    on_time = delivered - evaluation.score.late_count
    _same(on_time, row.get("on_time_count"), "on_time_count")
    _same(on_time / len(problem.tasks), row.get("on_time_rate"), "on_time_rate")
    _same(evaluation.on_time_rate, payload.get("on_time_rate"), "solution on_time_rate")
    if evaluation.valid and set(evaluation.delivery_times_min) != {task.id for task in problem.tasks}:
        raise ValueError("Valid solution does not complete every original task")
    expected_deliveries = {str(k): v for k, v in evaluation.delivery_times_min.items()}
    saved_deliveries = payload.get("delivery_times_min", {})
    if set(saved_deliveries) != set(expected_deliveries):
        raise ValueError("Saved delivery task set disagrees with complete evaluation")
    for task_id, delivered_at in expected_deliveries.items():
        _same(delivered_at, saved_deliveries[task_id], f"delivery time {task_id}")
    for index, route in enumerate(payload["routes"]):
        _same(evaluation.route_distances_km[index], route.get("distance_km"), "per-drone distance")
        _same(evaluation.route_completion_times_min[index], route.get("completion_time_min"), "per-drone completion time")


def audit_results(output_dir: Path) -> dict[str, Any]:
    """Verify all evidence, then write validation.json; honest failed runs remain failed."""

    directory = Path(output_dir).resolve()
    validation_path = directory / "validation.json"
    validation_path.unlink(missing_ok=True)
    manifest_path, runs_path = directory / "manifest.json", directory / "runs.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = json.loads(runs_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ValueError("Expected manifest object and runs array")
    schema = _manifest_schema(manifest)
    _audit_manifest_protocol(manifest)
    source_hashes = _audit_sources(directory, manifest)
    execution_hashes = _audit_execution_sources(directory, manifest)
    input_path = _within(ROOT, manifest.get("input_path"), "input_path")
    input_hash = _fingerprint(manifest.get("input_sha256"), "input_sha256")
    if _sha256(input_path) != input_hash or input_hash != _sha256(DEFAULT_INPUT):
        raise ValueError("Original input fingerprint mismatch")
    tasks = tuple(load_tasks_csv(input_path))
    cells: set[tuple] = set()
    initial_hashes: dict[tuple, str] = {}
    evaluations: dict[tuple, Any] = {}
    problems: dict[str, Problem] = {}
    invalid_rows: list[int] = []
    over_budget_rows: list[int] = []
    valid_on_budget = 0
    equivalence_path = directory / "equivalence.json"
    if schema == 2 and equivalence_path.exists():
        raise ValueError("Followup must not import an equivalence.json from the first batch")
    equivalence_rows = _equivalence_rows(json.loads(equivalence_path.read_text(encoding="utf-8"))) if equivalence_path.is_file() else []
    for index, row in enumerate(rows + equivalence_rows):
        if not isinstance(row, dict):
            raise ValueError("Invalid experiment row")
        is_equivalence = index >= len(rows)
        _audit_protocol(row, equivalence=is_equivalence, manifest=manifest)
        variant = row.get("variant")
        if variant not in source_hashes or row.get("source_sha256") != source_hashes[variant]:
            raise ValueError("Run source_sha256 disagrees with archived variant")
        _number(row.get("seed"), "seed", integer=True)
        cell = (row["stage"], variant, row.get("case_id"), row["seed"])
        if cell in cells:
            raise ValueError("Duplicate experiment cell")
        cells.add(cell)
        pair = (row["stage"], row.get("case_id"), row["seed"])
        initial_hash = _fingerprint(row.get("initial_routes_sha256"), "initial_routes_sha256")
        if initial_hashes.setdefault(pair, initial_hash) != initial_hash:
            raise ValueError("Paired variants do not share the same initial routes")
        # Recheck the row's fixed case definition even if its problem was cached.
        problem = _case_problem(row, tasks) if row.get("case_id") not in problems else problems[row["case_id"]]
        if row.get("task_count") != len(problem.tasks) or row.get("drone_count") != problem.drone_count:
            raise ValueError("Case task_count/drone_count violates the fixed problem definition")
        expected_scenario = "pure_direct" if row["case_id"] in {"single25", "fleet200"} else row["case_id"].split("__", 1)[1]
        if row.get("scenario") != expected_scenario:
            raise ValueError("Case scenario mismatch")
        problems[row["case_id"]] = problem
        solution_path = _within(directory, row.get("solution_file"), "solution_file")
        solution_hash = _fingerprint(row.get("solution_sha256"), "solution_sha256")
        if _sha256(solution_path) != solution_hash:
            raise ValueError("Saved solution fingerprint mismatch")
        payload = json.loads(solution_path.read_text(encoding="utf-8"))
        routes = payload.get("routes", [])
        if [route.get("drone_id") for route in routes] != list(range(1, problem.drone_count + 1)):
            raise ValueError("Saved routes have missing, duplicate or reordered drone IDs")
        key = (row["case_id"], solution_hash)
        if key not in evaluations:
            evaluations[key] = evaluate_solution(
                problem, [route["encoded_visits"] for route in routes], _force_full=True
            )
        evaluation = evaluations[key]
        _audit_solution(problem, payload, row, evaluation)
        compliant = _audit_runtime(row, payload)
        _audit_configuration(row, payload, manifest)
        _audit_trajectory(row, payload)
        _audit_decisions(row, payload, required=is_equivalence)
        if is_equivalence:
            continue
        if not evaluation.valid:
            invalid_rows.append(index)
        if not compliant:
            over_budget_rows.append(index)
        valid_on_budget += int(evaluation.valid and compliant)
    cumulative_budget = _audit_followup_budget(directory, manifest, rows)
    cumulative_success = cumulative_budget is None or (
        cumulative_budget["budget_compliant"] and not cumulative_budget["aborted_run_count"]
    )
    report = {
        "schema_version": schema,
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "frozen source/input hashes, original problem reconstruction, full independent route evaluation, paired initial routes, trajectories and end-to-end budget flags",
        "evidence_verified": True,
        "has_valid_results": valid_on_budget > 0,
        "all_runs_successful": bool(rows) and valid_on_budget == len(rows) and cumulative_success,
        "validated_rows": len(rows),
        "validated_unique_solutions": len(evaluations),
        "validated_equivalence_rows": len(equivalence_rows),
        "valid_on_budget_runs": valid_on_budget,
        "invalid_rows": invalid_rows,
        "over_budget_rows": over_budget_rows,
        "manifest_sha256": _sha256(manifest_path),
        "runs_sha256": _sha256(runs_path),
        "input_sha256": input_hash,
        "source_snapshots_sha256": source_hashes,
        "execution_sources_sha256": execution_hashes,
        "audit_script_sha256": _sha256(Path(__file__)),
        "equivalence_sha256": _sha256(equivalence_path) if equivalence_rows else None,
        "post_hoc_exploration": manifest.get("post_hoc_exploration"),
        "cumulative_budget": cumulative_budget,
    }
    validation_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    report = audit_results(parser.parse_args().output_dir)
    print(f"Verified {report['validated_rows']} rows; {report['valid_on_budget_runs']} valid and within budget")
    budget = report["cumulative_budget"]
    if budget is not None:
        status = "within cumulative limits" if budget["budget_compliant"] else "CUMULATIVE BUDGET FAILED"
        print(f"{budget['scope']}: {budget['total_seconds']:.3f}/3600 seconds; "
              f"60-second verification reserve; {status}; {budget['aborted_run_count']} aborted attempts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
