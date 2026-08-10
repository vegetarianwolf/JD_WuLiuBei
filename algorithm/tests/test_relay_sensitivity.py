import csv
import json

import pytest

from algorithm.experiments.generate_relay_scenarios import generate_scenario
from algorithm.experiments.run_relay_sensitivity import (
    _parse_args,
    _select_hubs,
    run,
)


def test_hub_count_scan_uses_nested_candidate_sets() -> None:
    scenario = generate_scenario("G", task_count=10, seed=19)
    selected = {
        count: {hub.id for hub in _select_hubs(scenario, count)}
        for count in (1, 2, 4, 8)
    }

    assert selected[1] < selected[2] < selected[4] < selected[8]


def test_quick_defaults_leave_an_explicit_relay_search_share() -> None:
    args = _parse_args(["--quick"])

    assert args.tasks == [8]
    assert args.seeds == [2026081000]
    assert args.hub_counts == [1, 2]
    assert args.handoff_times == [0.0, 0.5]
    assert args.candidate_limit == 4
    assert args.task_sample_size == 8
    assert args.base_search_fraction == 0.65


def test_runner_accepts_an_auditable_iteration_gate_and_rerun_reason() -> None:
    args = _parse_args(
        [
            "--quick",
            "--require-relay-iterations",
            "--rerun-reason",
            "replace zero-iteration pilot",
        ]
    )

    assert args.require_relay_iterations is True
    assert args.rerun_reason == "replace zero-iteration pilot"


def test_runner_resumes_matching_atomic_checkpoints_then_cleans_them(
    tmp_path,
) -> None:
    output = tmp_path / "resumable"
    common = [
        "--quick",
        "--scenarios",
        "U",
        "--tasks",
        "4",
        "--seeds",
        "7",
        "--hub-counts",
        "1",
        "--handoff-times",
        "0",
        "--max-tasks-per-drone",
        "2",
        "--time-limit",
        "0.04",
        "--wall-safety-margin",
        "0.004",
        "--output-dir",
        str(output),
    ]

    with pytest.raises(RuntimeError, match="checkpoint stop requested"):
        run(_parse_args([*common, "--stop-after-runs", "1"]))

    checkpoint = output / ".sensitivity_checkpoint"
    assert (checkpoint / "manifest.json").is_file()
    assert len(tuple((checkpoint / "runs").glob("run_*.json"))) == 1
    assert not tuple(checkpoint.rglob("*.tmp"))
    assert not (output / "sensitivity_results.json").exists()

    payload = run(_parse_args([*common, "--resume"]))

    assert payload["manifest"]["checkpoint"]["resumed_run_count"] == 1
    assert payload["manifest"]["checkpoint"]["new_run_count"] == 1
    assert payload["manifest"]["checkpoint"]["cleanup"] == (
        "deleted_after_successful_consolidation"
    )
    assert not checkpoint.exists()


def test_iteration_gate_names_failed_run_and_retains_partial_checkpoint(
    tmp_path,
) -> None:
    output = tmp_path / "failed-gate"
    args = _parse_args(
        [
            "--quick",
            "--scenarios",
            "U",
            "--tasks",
            "2",
            "--seeds",
            "7",
            "--hub-counts",
            "1",
            "--handoff-times",
            "0",
            "--max-tasks-per-drone",
            "1",
            "--max-iterations",
            "0",
            "--require-relay-iterations",
            "--output-dir",
            str(output),
        ]
    )

    with pytest.raises(
        RuntimeError,
        match=r"keys=U\|N=2\|seed=7\|H=1\|tau=0",
    ):
        run(args)

    checkpoint = output / ".sensitivity_checkpoint"
    assert len(tuple((checkpoint / "runs").glob("run_*.json"))) == 2
    assert not (output / "sensitivity_results.json").exists()


def test_quick_sensitivity_runner_persists_paired_matrix_and_heatmap(tmp_path) -> None:
    output = tmp_path / "sensitivity"
    args = _parse_args(
        [
            "--quick",
            "--scenarios",
            "U",
            "G",
            "--tasks",
            "4",
            "--seeds",
            "7",
            "8",
            "--hub-counts",
            "1",
            "2",
            "--handoff-times",
            "0",
            "1",
            "--max-tasks-per-drone",
            "2",
            "--time-limit",
            "0.04",
            "--wall-safety-margin",
            "0.004",
            "--output-dir",
            str(output),
        ]
    )

    payload = run(args)

    # Per scenario/seed: one paired baseline plus 2 hubs x 2 times.
    assert len(payload["runs"]) == 2 * 2 * (1 + 2 * 2)
    assert all(row["valid"] for row in payload["runs"])
    assert payload["manifest"]["objective_order"] == [
        "on_time_count",
        "distance_km",
    ]
    assert payload["manifest"]["task_count_semantics"] == "primary-owner"
    assert payload["manifest"]["experiment_stage"] == "development_sensitivity"
    assert payload["manifest"]["official_240s_comparable"] is False

    for filename in (
        "sensitivity_runs.csv",
        "sensitivity_summary.csv",
        "sensitivity_heatmap.csv",
        "sensitivity_results.json",
        "handoff_time_x_hub_count.svg",
    ):
        assert (output / filename).is_file()

    with (output / "sensitivity_heatmap.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        heatmap = list(csv.DictReader(handle))
    assert len(heatmap) == 2 * 1 * 2 * 2
    assert {
        "scenario",
        "task_count",
        "hub_count",
        "handoff_service_min",
        "delta_on_time_count_mean",
        "delta_distance_km_mean",
        "lexicographic_wins",
        "lexicographic_ties",
        "lexicographic_losses",
    } <= set(heatmap[0])
    assert "late_count" not in heatmap[0]
    assert "total_lateness_min" not in heatmap[0]

    svg = (output / "handoff_time_x_hub_count.svg").read_text(
        encoding="utf-8"
    )
    assert svg.startswith("<svg")
    assert "hub count" in svg
    assert "handoff time (min)" in svg

    persisted = json.loads(
        (output / "sensitivity_results.json").read_text(encoding="utf-8")
    )
    assert persisted["manifest"]["distance_metric"] == (
        "euclidean_coordinate_km_unclipped"
    )
    assert len(tuple((output / "scenarios").glob("*/*/*/tasks.csv"))) == 4
