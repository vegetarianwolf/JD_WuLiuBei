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
        if variant not in VARIANT_CONFIG or snapshot["config"] != VARIANT_CONFIG[variant]:
            raise ValueError(f"Source snapshot configuration violates the experiment protocol: {variant}")
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
        hashes[variant] = expected
    return hashes


def _audit_execution_sources(directory: Path, manifest: dict[str, Any]) -> dict[str, str]:
    sources = manifest.get("execution_sources")
    expected = {"run_optimization_experiments", "run_scenario_comparison"}
    if not isinstance(sources, dict) or set(sources) != expected:
        raise ValueError("Missing frozen experiment runner or scenario builder")
    result = {}
    for name, source in sources.items():
        path = _within(directory, source.get("path"), f"execution source {name}")
        fingerprint = _fingerprint(source.get("sha256"), f"execution source {name}")
        if _sha256(path) != fingerprint:
            raise ValueError(f"Execution source fingerprint mismatch: {name}")
        result[name] = fingerprint
    if manifest.get("runner_sha256") != result["run_optimization_experiments"]:
        raise ValueError("runner_sha256 disagrees with frozen execution source")
    return result


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


def _audit_protocol(row: dict[str, Any], *, equivalence: bool) -> None:
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


def _audit_configuration(row: dict[str, Any], payload: dict[str, Any]) -> None:
    relay = row["scenario"] in {"direct_relay", "direct_relay_stations"}
    homes = row["scenario"] in {"direct_stations", "direct_relay_stations"}
    budget = row["budget_seconds"]
    expected = {
        "max_iterations": 200 if row["stage"] == "equivalence" else 10_000_000,
        "time_limit_seconds": None if budget is None else max(1e-6, budget - row["construction_seconds"] - 1.0),
        "seed": row["seed"], "candidate_limit": 48, "relay_enabled": relay,
        "relay_direct_warmup_fraction": 0.8, "enable_home_seed": homes,
        "enable_home_bias": homes, **VARIANT_CONFIG[row["variant"]],
    }
    config = row.get("config")
    if not isinstance(config, dict) or set(config) != set(expected) or any(
        config[field] != value for field, value in expected.items() if field != "time_limit_seconds"
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
    equivalence_rows = _equivalence_rows(json.loads(equivalence_path.read_text(encoding="utf-8"))) if equivalence_path.is_file() else []
    for index, row in enumerate(rows + equivalence_rows):
        if not isinstance(row, dict):
            raise ValueError("Invalid experiment row")
        is_equivalence = index >= len(rows)
        _audit_protocol(row, equivalence=is_equivalence)
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
        _audit_configuration(row, payload)
        _audit_trajectory(row, payload)
        _audit_decisions(row, payload, required=is_equivalence)
        if is_equivalence:
            continue
        if not evaluation.valid:
            invalid_rows.append(index)
        if not compliant:
            over_budget_rows.append(index)
        valid_on_budget += int(evaluation.valid and compliant)
    report = {
        "schema_version": 1,
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "method": "frozen source/input hashes, original problem reconstruction, full independent route evaluation, paired initial routes, trajectories and end-to-end budget flags",
        "evidence_verified": True,
        "has_valid_results": valid_on_budget > 0,
        "all_runs_successful": bool(rows) and valid_on_budget == len(rows),
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
    }
    validation_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    report = audit_results(parser.parse_args().output_dir)
    print(f"Verified {report['validated_rows']} rows; {report['valid_on_budget_runs']} valid and within budget")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
