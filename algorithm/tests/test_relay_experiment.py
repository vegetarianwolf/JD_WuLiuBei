import csv
import hashlib
import json
from pathlib import Path

import pytest

from algorithm.experiments.run_relay_handoff import _parse_args, run
from algorithm.experiments.migrate_relay_handoff_artifacts import migrate


SUMMARY_FIELDS = {
    "method",
    "semantics",
    "task_count",
    "run_count",
    "valid_run_count",
    "on_time_count_mean",
    "on_time_count_min",
    "on_time_count_max",
    "on_time_rate_mean",
    "on_time_rate_min",
    "on_time_rate_max",
    "distance_km_mean",
    "distance_km_min",
    "distance_km_max",
    "runtime_seconds_mean",
    "budget_seconds",
    "relay_count_mean",
}


def test_quick_runner_persists_all_four_methods_and_summary_schema(tmp_path) -> None:
    output = tmp_path / "relay-results"
    args = _parse_args(
        [
            "--quick",
            "--output-dir",
            str(output),
            "--methods",
            "baseline",
            "relay-disabled",
            "static-1hub",
            "static-multihub",
            "--seeds",
            "7",
            "--tasks",
            "4",
            "--drones",
            "2",
            "--max-tasks",
            "2",
            "--time-limit",
            "0.15",
            "--wall-safety-margin",
            "0.01",
            "--task-count-semantics",
            "primary-owner",
            "--hub-count",
            "2",
        ]
    )

    payload = run(args)

    assert {row["method"] for row in payload["runs"]} == {
        "baseline",
        "relay-disabled",
        "static-1hub",
        "static-multihub",
    }
    assert all(row["valid"] and row["runtime_compliant"] for row in payload["runs"])
    assert all(
        row["compliant"] is row["runtime_compliant"]
        for row in payload["runs"]
    )
    assert all(
        row["official_semantics_compliant"] is False
        for row in payload["runs"]
    )
    assert all(row["official_plan_compliant"] is True for row in payload["runs"])
    assert all(row["semantics_extension"] for row in payload["runs"])
    assert all(len(row["input_sha256"]) == 64 for row in payload["runs"])
    disabled = next(
        row for row in payload["runs"] if row["method"] == "relay-disabled"
    )
    assert disabled["relay_count"] == 0
    assert disabled["direct_event_equivalent"] is True

    assert (output / "relay_runs.csv").is_file()
    assert (output / "relay_summary.csv").is_file()
    assert (output / "relay_results.json").is_file()
    assert len(tuple((output / "run_solutions").glob("*.json"))) == 4

    with (output / "relay_summary.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        summaries = list(csv.DictReader(handle))
    assert len(summaries) == 4
    assert SUMMARY_FIELDS <= set(summaries[0])

    persisted = json.loads(
        (output / "relay_results.json").read_text(encoding="utf-8")
    )
    assert persisted["manifest"]["objective_order"] == [
        "on_time_count",
        "distance_km",
    ]
    assert persisted["manifest"]["compliance_fields"] == {
        "compliant": "backward-compatible alias of runtime_compliant",
        "runtime_compliant": "runtime_seconds <= budget_seconds",
        "official_semantics_compliant": (
            "task_count_semantics == strict-touch and semantics_extension == false"
        ),
        "official_plan_compliant": (
            "the persisted plan is valid when independently re-evaluated "
            "under strict-touch semantics"
        ),
    }
    assert persisted["manifest"]["summary_metric_filter"] == (
        "valid == true and runtime_compliant == true"
    )
    expected_source_files = {
        "algorithm/src/uav_dispatch/relay.py",
        "algorithm/src/uav_dispatch/relay_validation.py",
        "algorithm/src/uav_dispatch/relay_search.py",
        "algorithm/src/uav_dispatch/relay_alns.py",
        "algorithm/experiments/run_relay_handoff.py",
    }
    source_hashes = persisted["manifest"]["source_code_sha256"]
    assert set(source_hashes) == expected_source_files
    repository_root = Path(__file__).resolve().parents[2]
    assert source_hashes == {
        relative_path: hashlib.sha256(
            (repository_root / relative_path).read_bytes()
        ).hexdigest()
        for relative_path in sorted(expected_source_files)
    }
    expected_snapshot_hash = hashlib.sha256(
        json.dumps(
            source_hashes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert persisted["manifest"]["source_snapshot_sha256"] == (
        expected_snapshot_hash
    )
    assert all(
        row["source_snapshot_sha256"] == expected_snapshot_hash
        for row in payload["runs"]
    )
    with (output / "relay_runs.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        persisted_runs = list(csv.DictReader(handle))
    assert all(
        row["source_snapshot_sha256"] == expected_snapshot_hash
        for row in persisted_runs
    )
    assert {
        "relay_py_sha256",
        "relay_validation_py_sha256",
        "relay_search_py_sha256",
        "relay_alns_py_sha256",
        "runner_py_sha256",
    } <= set(persisted_runs[0])
    assert all(summary["valid_run_count"] == 1 for summary in payload["summaries"])
    assert all(
        summary["evaluation_valid_run_count"] == 1
        for summary in payload["summaries"]
    )
    assert all(
        summary["runtime_compliant_run_count"] == 1
        for summary in payload["summaries"]
    )
    assert all(
        summary["official_semantics_compliant_run_count"] == 0
        for summary in payload["summaries"]
    )
    assert all(
        summary["official_plan_compliant_run_count"] == 1
        for summary in payload["summaries"]
    )
    assert all(
        summary["summary_eligible_run_count"] == 1
        for summary in payload["summaries"]
    )
    for row in payload["runs"]:
        solution = json.loads(
            (output / row["solution_file"]).read_text(encoding="utf-8")
        )
        assert len(solution["event_routes"]) == 2
        assert sum(len(route) for route in solution["event_routes"]) >= 8
        assert all(
            {"task_id", "event_type", "event_time_min"} <= set(event)
            for route in solution["event_routes"]
            for event in route
        )
        assert len(solution["problem"]["tasks"]) == 4
        assert solution["experiment"]["runtime_compliant"] is True
        assert solution["experiment"]["compliant"] is True
        assert solution["experiment"]["official_semantics_compliant"] is False
        assert solution["experiment"]["official_plan_compliant"] is True
        assert solution["experiment"]["source_code_sha256"] == source_hashes
        assert solution["experiment"]["source_snapshot_sha256"] == (
            expected_snapshot_hash
        )
        assert solution["official_plan_audit"] == {
            "task_count_semantics": "strict-touch",
            "valid": True,
            "violations": [],
            "physical_touch_counts": solution["diagnostics"][
                "physical_touch_counts"
            ],
        }

    disabled_solution = json.loads(
        (output / disabled["solution_file"]).read_text(encoding="utf-8")
    )
    equivalence = disabled_solution["direct_equivalence_audit"]
    assert equivalence["checked"] is True
    assert equivalence["equivalent"] is True
    assert equivalence["late_count_delta"] == 0
    assert equivalence["total_lateness_abs_delta_min"] <= 1e-9
    assert equivalence["distance_abs_delta_km"] <= 1e-9
    assert equivalence["max_delivery_time_abs_delta_min"] <= 1e-9


def _write_official_style_tasks(path) -> None:
    path.write_text(
        "task_id,pickup_x,pickup_y,delivery_x,delivery_y,deadline_min\n"
        "1,1,0,2,0,4\n"
        "2,0,1,0,2,5\n"
        "3,2,1,3,2,6\n",
        encoding="utf-8",
    )


def test_quick_flag_materializes_safe_smoke_defaults_without_overriding_explicit_values() -> None:
    quick = _parse_args(["--quick"])
    explicit = _parse_args(
        [
            "--quick",
            "--seeds",
            "1",
            "2",
            "--tasks",
            "6",
            "--drones",
            "3",
            "--max-tasks",
            "2",
            "--time-limit",
            "0.4",
            "--wall-safety-margin",
            "0.03",
        ]
    )

    assert quick.seeds == [2026080500]
    assert (quick.tasks, quick.drones, quick.max_tasks) == (8, 4, 2)
    assert (quick.time_limit, quick.wall_safety_margin) == (0.25, 0.02)
    assert explicit.seeds == [1, 2]
    assert (explicit.tasks, explicit.drones, explicit.max_tasks) == (6, 3, 2)
    assert (explicit.time_limit, explicit.wall_safety_margin) == (0.4, 0.03)


def test_official_input_and_static_hubs_are_fully_auditable(tmp_path) -> None:
    source = tmp_path / "official.csv"
    _write_official_style_tasks(source)
    output = tmp_path / "formal"
    args = _parse_args(
        [
            "--dataset",
            "official",
            "--input",
            str(source),
            "--output-dir",
            str(output),
            "--methods",
            "baseline",
            "static-1hub",
            "static-multihub",
            "--seeds",
            "11",
            "--tasks",
            "2",
            "--drones",
            "2",
            "--max-tasks",
            "1",
            "--time-limit",
            "0.08",
            "--wall-safety-margin",
            "0.01",
            "--task-count-semantics",
            "strict-touch",
            "--hub-count",
            "2",
            "--handoff-service-min",
            "0.75",
        ]
    )

    payload = run(args)

    manifest = payload["manifest"]
    assert manifest["dataset"] == "official"
    assert manifest["input_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert len(manifest["problem_sha256"]) == 64
    assert manifest["input_sha256"] != manifest["problem_sha256"]
    assert manifest["budget_includes_direct_base_search"] is True
    assert manifest["baseline_definition"] == {
        "family": "A2",
        "enable_late_risk_destroy": True,
        "objective_order": ["on_time_count", "distance_km"],
    }
    assert all(row["task_count"] == 2 for row in payload["runs"])
    assert all(row["semantics"] == "strict-touch" for row in payload["runs"])
    assert all(row["semantics_extension"] is False for row in payload["runs"])
    assert all(
        row["official_semantics_compliant"] is True
        for row in payload["runs"]
    )
    assert all(row["base_search_used"] is True for row in payload["runs"])
    assert all(
        row["base_search_runtime_seconds"] <= row["runtime_seconds"]
        for row in payload["runs"]
    )

    by_method = {row["method"]: row for row in payload["runs"]}
    assert by_method["baseline"]["hub_count"] == 0
    assert by_method["static-1hub"]["hub_count"] == 1
    assert by_method["static-multihub"]["hub_count"] == 2
    multi = json.loads(
        (output / by_method["static-multihub"]["solution_file"]).read_text(
            encoding="utf-8"
        )
    )
    assert len(multi["problem"]["hubs"]) == 2
    assert multi["problem"]["handoff_service_min"] == 0.75


def test_metadata_migration_preserves_solver_outputs_and_closes_hash_chain(
    tmp_path,
) -> None:
    output = tmp_path / "legacy-like"
    payload = run(
        _parse_args(
            [
                "--quick",
                "--output-dir",
                str(output),
                "--methods",
                "baseline",
                "static-1hub",
                "--seeds",
                "17",
                "--task-count-semantics",
                "primary-owner",
            ]
        )
    )
    score_signature = [
        (
            row["method"],
            row["on_time_count"],
            row["distance_km"],
            row["runtime_seconds"],
            row["relay_count"],
        )
        for row in payload["runs"]
    ]
    event_routes = {
        row["solution_file"]: json.loads(
            (output / row["solution_file"]).read_text(encoding="utf-8")
        )["event_routes"]
        for row in payload["runs"]
    }

    first = migrate(output)
    first_solution_hashes = {
        row["solution_file"]: row["solution_sha256"]
        for row in first["runs"]
    }
    second = migrate(output)

    assert first["manifest"]["artifact_generation_mode"] == (
        "metadata_only_migration"
    )
    assert first["manifest"]["solver_outputs_reused_without_rerun"] is True
    assert [
        (
            row["method"],
            row["on_time_count"],
            row["distance_km"],
            row["runtime_seconds"],
            row["relay_count"],
        )
        for row in first["runs"]
    ] == score_signature
    assert first_solution_hashes == {
        row["solution_file"]: row["solution_sha256"]
        for row in second["runs"]
    }
    for row in second["runs"]:
        solution_path = output / row["solution_file"]
        assert row["solution_sha256"] == hashlib.sha256(
            solution_path.read_bytes()
        ).hexdigest()
        solution = json.loads(solution_path.read_text(encoding="utf-8"))
        assert solution["event_routes"] == event_routes[row["solution_file"]]
        assert solution["experiment"]["source_snapshot_role"] == (
            "reproduction_source_after_metadata_migration"
        )


@pytest.mark.parametrize(
    ("extra_args", "message"),
    [
        (["--time-limit", "1", "--wall-safety-margin", "1"], "安全余量"),
        (["--tasks", "3", "--drones", "1", "--max-tasks", "2"], "任务槽位"),
        (["--methods", "baseline", "baseline"], "不能重复"),
        (["--methods", "static-multihub", "--hub-count", "1"], "至少为 2"),
    ],
)
def test_invalid_experiment_design_is_rejected(extra_args, message) -> None:
    args = _parse_args(["--quick", *extra_args])

    with pytest.raises(ValueError, match=message):
        run(args)
