"""Upgrade relay artifacts without rerunning the optimization algorithm.

The migration preserves routes, scores, runtimes, and diagnostics.  It only
adds explicit compliance/provenance fields, independently revalidates each
persisted plan under strict-touch semantics, and closes the solution hash
chain after those metadata additions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from uav_dispatch import Point, Problem, Task
from uav_dispatch.relay import (
    Event,
    Hub,
    RelayPlan,
    RelayProblem,
    TaskCountSemantics,
)
from uav_dispatch.relay_validation import evaluate_relay_plan

from algorithm.experiments.run_relay_handoff import (
    RUN_FIELDS,
    SOURCE_CODE_CSV_FIELDS,
    SUMMARY_FIELDS,
    _compliance_manifest_fields,
    _json_sha256,
    _source_code_hashes,
    _summarize,
    _write_csv,
    _write_json,
)


SOURCE_SNAPSHOT_ROLE = "reproduction_source_after_metadata_migration"


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path} 顶层必须是 JSON object")
    return payload


def _strict_plan_audit(solution: Mapping[str, Any]) -> dict[str, Any]:
    problem_payload = solution["problem"]
    tasks = tuple(
        Task(
            int(task["id"]),
            Point(*map(float, task["pickup"])),
            Point(*map(float, task["delivery"])),
            float(task["deadline_min"]),
        )
        for task in problem_payload["tasks"]
    )
    base_problem = Problem(
        tasks,
        drone_count=int(problem_payload["drone_count"]),
        max_tasks_per_drone=int(problem_payload["max_tasks_per_drone"]),
        capacity=int(problem_payload["capacity"]),
        speed_km_per_min=float(problem_payload["speed_km_per_min"]),
    )
    hubs = tuple(
        Hub(
            str(hub["id"]),
            Point(float(hub["x"]), float(hub["y"])),
        )
        for hub in problem_payload["hubs"]
    )
    method = str(solution["experiment"]["method"])
    max_handoffs = 0 if method in {"baseline", "relay-disabled"} else 1
    relay_problem = RelayProblem(
        base_problem,
        hubs=hubs,
        handoff_service_min=float(problem_payload["handoff_service_min"]),
        task_count_semantics=TaskCountSemantics.STRICT_TOUCH,
        max_handoffs_per_task=max_handoffs,
    )
    plan = RelayPlan(
        tuple(
            tuple(
                Event(
                    int(event["task_id"]),
                    str(event["event_type"]),
                    event.get("hub_id"),
                )
                for event in route
            )
            for route in solution["event_routes"]
        )
    )
    evaluation = evaluate_relay_plan(relay_problem, plan)

    diagnostics = solution["diagnostics"]
    if evaluation.score.late_count != int(diagnostics["late_count"]):
        raise ValueError("strict-touch 重算的逾期任务数与持久化结果不一致")
    if not math.isclose(
        evaluation.score.distance_km,
        float(solution["score"]["distance_km"]),
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        raise ValueError("strict-touch 重算的总里程与持久化结果不一致")

    return {
        "task_count_semantics": TaskCountSemantics.STRICT_TOUCH.value,
        "valid": evaluation.valid,
        "violations": list(evaluation.violations),
        "physical_touch_counts": list(evaluation.physical_touch_counts),
    }


def _resolved_solution_path(output_dir: Path, relative_path: str) -> Path:
    output_root = output_dir.resolve()
    solution_path = (output_root / relative_path).resolve()
    if not solution_path.is_relative_to(output_root):
        raise ValueError(f"solution_file 越出输出目录: {relative_path}")
    return solution_path


def migrate(
    output_dir: Path,
    *,
    source_code_sha256: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Migrate one result directory without invoking ``solve_relay_alns``."""

    output_dir = Path(output_dir)
    results_path = output_dir / "relay_results.json"
    payload = _load_json(results_path)
    rows = [dict(row) for row in payload["runs"]]
    source_hashes = dict(
        _source_code_hashes()
        if source_code_sha256 is None
        else source_code_sha256
    )
    source_snapshot_sha256 = _json_sha256(source_hashes)

    for row in rows:
        solution_path = _resolved_solution_path(
            output_dir, str(row["solution_file"])
        )
        solution = _load_json(solution_path)
        experiment = solution["experiment"]
        semantics = str(row["semantics"])
        semantics_extension = semantics == TaskCountSemantics.PRIMARY_OWNER.value
        runtime_compliant = (
            float(row["runtime_seconds"]) <= float(row["budget_seconds"])
        )
        official_semantics_compliant = (
            semantics == TaskCountSemantics.STRICT_TOUCH.value
            and not semantics_extension
        )
        official_plan_audit = _strict_plan_audit(solution)
        official_plan_compliant = bool(official_plan_audit["valid"])

        experiment.update(
            {
                "semantics_extension": semantics_extension,
                "runtime_compliant": runtime_compliant,
                "official_semantics_compliant": (
                    official_semantics_compliant
                ),
                "official_plan_compliant": official_plan_compliant,
                "compliant": runtime_compliant,
                "source_code_sha256": source_hashes,
                "source_snapshot_sha256": source_snapshot_sha256,
                "source_snapshot_role": SOURCE_SNAPSHOT_ROLE,
            }
        )
        solution["official_plan_audit"] = official_plan_audit
        solution["provenance_migration"] = {
            "solver_rerun": False,
            "preserved": [
                "event_routes",
                "score",
                "runtime_seconds",
                "iterations",
                "diagnostics",
            ],
        }
        _write_json(solution_path, solution)

        row.update(
            {
                "runtime_compliant": runtime_compliant,
                "official_semantics_compliant": (
                    official_semantics_compliant
                ),
                "official_plan_compliant": official_plan_compliant,
                "compliant": runtime_compliant,
                "semantics_extension": semantics_extension,
                "source_snapshot_sha256": source_snapshot_sha256,
                "source_snapshot_role": SOURCE_SNAPSHOT_ROLE,
                **{
                    csv_field: source_hashes[relative_path]
                    for relative_path, csv_field in (
                        SOURCE_CODE_CSV_FIELDS.items()
                    )
                },
                "solution_sha256": hashlib.sha256(
                    solution_path.read_bytes()
                ).hexdigest(),
            }
        )

    summaries = _summarize(rows)
    manifest = dict(payload["manifest"])
    manifest.update(
        {
            "source_code_sha256": source_hashes,
            "source_snapshot_sha256": source_snapshot_sha256,
            "source_snapshot_role": SOURCE_SNAPSHOT_ROLE,
            "artifact_schema_version": 2,
            "artifact_generation_mode": "metadata_only_migration",
            "solver_outputs_reused_without_rerun": True,
            **_compliance_manifest_fields(),
        }
    )
    migrated = {
        "manifest": manifest,
        "runs": rows,
        "summaries": summaries,
    }
    _write_csv(output_dir / "relay_runs.csv", rows, RUN_FIELDS)
    _write_csv(output_dir / "relay_summary.csv", summaries, SUMMARY_FIELDS)
    _write_json(results_path, migrated)
    return migrated


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dirs", nargs="+", type=Path)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    for output_dir in args.output_dirs:
        migrate(output_dir)
        print(f"migrated {output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
