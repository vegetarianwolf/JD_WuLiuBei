"""Independently rescore saved sensitivity routes from the original requests.

Usage: python -m algorithm.experiments.audit_sensitivity_results [result-dir]
Writes validation.json only after every cell and unique solution passes.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from uav_dispatch import load_tasks_csv
from uav_dispatch.model import Problem
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments.run_scenario_comparison import (
    DEFAULT_INPUT,
    _sha256,
    _solver_source_sha256,
    _validate_relay_phase_runtime,
    build_scenario_problem,
)
from algorithm.experiments.run_sensitivity_analysis import (
    DEFAULT_OUTPUT,
    FORMAL_PROTOCOL_CONFIG,
    SENSITIVITY_SCENARIOS,
    _parser,
    scale_deadlines,
    validate_formal_protocol,
    validate_rows,
)


def audit_saved_solution(
    problem: Problem, payload: dict[str, Any], row: dict[str, Any]
) -> None:
    """Recompute constraints and all objective terms without search caches."""

    routes = payload["routes"]
    if [route["drone_id"] for route in routes] != list(range(1, problem.drone_count + 1)):
        raise ValueError("Saved routes have missing or reordered drone IDs")
    evaluation = evaluate_solution(
        problem, [route["encoded_visits"] for route in routes], _force_full=True
    )
    if not evaluation.valid:
        raise ValueError(f"Saved routes are infeasible: {evaluation.violations}")
    if payload["valid"] is not True or payload["violations"]:
        raise ValueError("Saved validity disagrees with independent validation")
    for key in ("late_count", "total_lateness_min", "distance_km"):
        actual = getattr(evaluation.score, key)
        for source_name, source in (("solution", payload["score"]), ("row", row)):
            if not math.isclose(actual, float(source[key]), rel_tol=0, abs_tol=1e-8):
                raise ValueError(f"Recomputed {key} disagrees with {source_name}")
    for index, route in enumerate(routes):
        if not math.isclose(
            evaluation.route_distances_km[index], route["distance_km"], rel_tol=0, abs_tol=1e-8
        ):
            raise ValueError("Recomputed per-drone distance disagrees with saved route")
    expected_deliveries = {
        str(task_id): delivered_at
        for task_id, delivered_at in evaluation.delivery_times_min.items()
    }
    saved_deliveries = payload["delivery_times_min"]
    if set(saved_deliveries) != set(expected_deliveries) or any(
        not math.isclose(expected, float(saved_deliveries[task_id]), rel_tol=0, abs_tol=1e-8)
        for task_id, expected in expected_deliveries.items()
    ):
        raise ValueError("Recomputed delivery times disagree with saved solution")


def validate_source_fingerprints(manifest: dict[str, Any]) -> None:
    """Bind the audit to the experiment and scenario builders that ran."""

    expected = {
        "experiment_script_sha256": _sha256(Path(__file__).with_name("run_sensitivity_analysis.py")),
        "scenario_script_sha256": _sha256(Path(__file__).with_name("run_scenario_comparison.py")),
        "solver_source_sha256": _solver_source_sha256(),
    }
    for field, fingerprint in expected.items():
        if manifest.get(field) != fingerprint:
            raise ValueError(f"Source fingerprint mismatch: {field}")


def _runtime_number(source: dict[str, Any], field: str) -> float:
    try:
        value = float(source[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Missing or invalid runtime field: {field}") from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"Nonfinite or negative runtime field: {field}")
    return value


def audit_formal_runtime(
    row: dict[str, Any], payload: dict[str, Any], scenario: dict[str, Any],
    *, budget_seconds: float, safety_margin_seconds: float,
) -> None:
    """Check observed full-budget runs and match their saved solver metadata.

    Tolerances follow the existing formal scenario audit: timer checks permit
    at most 1% of the nominal budget (and a 2 s minimum for phase boundaries).
    This is not applied to quick runs, which may stop at their iteration cap.
    """

    construction = _runtime_number(row, "construction_seconds")
    solver_runtime = _runtime_number(row, "solver_runtime_seconds")
    wall_runtime = _runtime_number(row, "wall_seconds")
    warmup_runtime = _runtime_number(row, "relay_warmup_runtime_seconds")
    relay_runtime = _runtime_number(row, "relay_search_runtime_seconds")
    recorded_runtime = _runtime_number(payload, "runtime_seconds")
    metadata = payload["metadata"]
    recorded_limit = _runtime_number(metadata, "time_limit_seconds")
    expected_solver = max(1e-6, budget_seconds - construction - safety_margin_seconds)
    if not math.isclose(recorded_runtime, solver_runtime, rel_tol=0, abs_tol=1e-8):
        raise ValueError("Saved solver runtime disagrees with row")
    if not math.isclose(recorded_limit, expected_solver, rel_tol=0, abs_tol=1e-8):
        raise ValueError("Saved solver time limit disagrees with fixed-budget construction allowance")
    tolerance = max(2.0, 0.01 * budget_seconds)
    if not math.isclose(solver_runtime, expected_solver, rel_tol=0, abs_tol=tolerance):
        raise ValueError("Observed solver runtime does not match the formal budget")
    if not math.isclose(wall_runtime, budget_seconds, rel_tol=0, abs_tol=tolerance):
        raise ValueError("Observed wall runtime does not match the formal budget")
    if not math.isclose(wall_runtime, construction + solver_runtime, rel_tol=0, abs_tol=tolerance):
        raise ValueError("Observed wall runtime disagrees with construction plus solver time")
    if metadata.get("seed") != row["seed"]:
        raise ValueError("Saved solver seed disagrees with row")
    if metadata.get("relay_enabled") is not scenario["relay"]:
        raise ValueError("Saved solver Relay mechanism disagrees with scenario")
    if metadata.get("enable_home_seed") is not (scenario["drone_homes"] == "stations"):
        raise ValueError("Saved solver predeployment mechanism disagrees with scenario")
    if scenario["relay"]:
        if metadata.get("staged_relay") is not True or metadata.get("relay_direct_warmup_fraction") != 0.8:
            raise ValueError("Saved Relay phase allocation must use the formal 80% / 20% split")
        for field in ("relay_warmup_runtime_seconds", "relay_search_runtime_seconds"):
            if not math.isclose(_runtime_number(metadata, field), row[field], rel_tol=0, abs_tol=1e-8):
                raise ValueError(f"Saved {field} disagrees with row")
        _validate_relay_phase_runtime(
            row, scenario, budget=budget_seconds,
            safety_margin_seconds=safety_margin_seconds,
            cell=(row["seed"], row["scenario"]),
        )
    elif warmup_runtime != 0 or relay_runtime != 0 or metadata.get("staged_relay", False):
        raise ValueError("Non-Relay strategy contains recorded Relay phase runtime")


def audit_directory(directory: Path) -> dict[str, Any]:
    manifest_path = directory / "manifest.json"
    runs_path = directory / "runs.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = json.loads(runs_path.read_text(encoding="utf-8"))
    if manifest["schema_version"] != 2 or manifest["protocol"] != "four_strategy_one_factor_sensitivity":
        raise ValueError("Expected the four-strategy sensitivity protocol")
    validate_source_fingerprints(manifest)
    input_path = Path(manifest["input"])
    if not input_path.is_file():
        input_path = DEFAULT_INPUT
    if _sha256(input_path) != manifest["input_sha256"]:
        raise ValueError("Original input fingerprint mismatch")
    args = _parser().parse_args([])
    for key in FORMAL_PROTOCOL_CONFIG:
        setattr(args, key, manifest.get(key, manifest["solver_config"].get(key)))
    args.input = input_path
    args.output_dir = directory
    args.quick = not manifest["formal"]
    validate_formal_protocol(args)
    validate_rows(rows, args)
    source_tasks = load_tasks_csv(input_path)[:manifest["tasks"]]
    if len(source_tasks) != manifest["tasks"]:
        raise ValueError("Input does not contain the declared task count")
    scenarios = {scenario["id"]: scenario for scenario in SENSITIVITY_SCENARIOS}
    audited: dict[str, str] = {}
    for row in rows:
        path = directory / row["cell_output_dir"] / row["solution_file"]
        if row["run_id"] in audited:
            if audited[row["run_id"]] != row["solution_sha256"]:
                raise ValueError("Reused baseline refers to a different solution")
            continue
        tasks = scale_deadlines(source_tasks, row["deadline_multiplier"])
        problem, _ = build_scenario_problem(
            tasks, scenarios[row["scenario"]], drones=row["drone_count"], max_tasks=25,
            relay_count=row["requested_station_count"], relay_location_seed=42,
        )
        payload = json.loads(path.read_text(encoding="utf-8"))
        audit_saved_solution(problem, payload, row)
        if manifest["formal"]:
            audit_formal_runtime(
                row, payload, scenarios[row["scenario"]],
                budget_seconds=args.base_seconds + args.bonus_seconds,
                safety_margin_seconds=args.safety_margin_seconds,
            )
        audited[row["run_id"]] = row["solution_sha256"]
    if len(audited) != manifest["planned_unique_solves"]:
        raise ValueError("Independent solve count disagrees with manifest")
    return {
        "schema_version": 1,
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "formal": manifest["formal"],
        "method": "verify source fingerprints; rebuild problems from original CSV; full route feasibility, objective and delivery-time recomputation",
        "formal_runtime_checked": bool(manifest["formal"]),
        "validated_rows": len(rows),
        "validated_unique_solutions": len(audited),
        "manifest_sha256": _sha256(manifest_path),
        "runs_sha256": _sha256(runs_path),
        "input_sha256": manifest["input_sha256"],
        "solver_source_sha256": _solver_source_sha256(),
        "audit_script_sha256": _sha256(Path(__file__)),
        "solution_sha256_by_run": audited,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report_path = args.directory / "validation.json"
    report_path.unlink(missing_ok=True)
    report = audit_directory(args.directory)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Verified {report['validated_rows']} cells / {report['validated_unique_solutions']} unique solutions")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
