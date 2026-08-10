import csv
import hashlib
import json
from dataclasses import asdict

import pytest

import algorithm.experiments.run_adaptive_deadline_rejection as experiment_runner
from algorithm.experiments.run_adaptive_deadline_rejection import (
    _parse_args,
    paired_comparisons,
    build_variants,
    config_for_variant,
    run,
)


def _write_tiny_tasks(path) -> None:
    path.write_text(
        "task_id,pickup_x,pickup_y,delivery_x,delivery_y,deadline_min\n"
        "1,1,0,2,0,4\n"
        "2,0,1,0,2,5\n",
        encoding="utf-8",
    )


def _tiny_run_argv(source, output) -> list[str]:
    return [
        "--input",
        str(source),
        "--output-dir",
        str(output),
        "--methods",
        "baseline",
        "experiment1",
        "--equal-seed-count",
        "1",
        "--equal-iterations",
        "1",
        "--wall-seed-count",
        "0",
    ]


def test_phase2_variants_are_explicit_a2_configs_differing_only_in_late_risk() -> None:
    args = _parse_args([])
    baseline, experiment1, _, _, _, _ = build_variants()

    baseline_config = config_for_variant(
        args,
        baseline,
        seed=7,
        max_iterations=5,
        time_limit_seconds=None,
    )
    experiment_config = config_for_variant(
        args,
        experiment1,
        seed=7,
        max_iterations=5,
        time_limit_seconds=None,
    )
    baseline_fields = asdict(baseline_config)
    experiment_fields = asdict(experiment_config)
    changed = {
        name
        for name in baseline_fields
        if baseline_fields[name] != experiment_fields[name]
    }

    assert [variant.name for variant in (baseline, experiment1)] == [
        "baseline",
        "experiment1",
    ]
    assert changed == {"enable_late_risk_destroy"}
    assert baseline_config.enable_assignment_destroy is True
    assert baseline_config.enable_deadline_risk is True
    assert baseline_config.enable_vnd is False
    assert baseline_config.enable_cluster_repair is False
    assert baseline_config.enable_ejection is False
    assert baseline_config.enable_route_pool is False


def test_experiment2_differs_from_a2_only_by_the_rejection_pool_flag() -> None:
    args = _parse_args([])
    baseline, _, experiment2, _, _, _ = build_variants()
    baseline_fields = asdict(
        config_for_variant(
            args,
            baseline,
            seed=7,
            max_iterations=5,
            time_limit_seconds=None,
        )
    )
    experiment_fields = asdict(
        config_for_variant(
            args,
            experiment2,
            seed=7,
            max_iterations=5,
            time_limit_seconds=None,
        )
    )

    assert experiment2.name == "experiment2"
    assert {
        name
        for name in baseline_fields
        if baseline_fields[name] != experiment_fields[name]
    } == {"enable_rejection_pool"}


def test_experiment3_enables_the_soft_deadline_search_package() -> None:
    args = _parse_args(["--soft-deadline-beta", "0.30"])
    baseline, _, _, experiment3, _, _ = build_variants()
    baseline_fields = asdict(
        config_for_variant(
            args,
            baseline,
            seed=7,
            max_iterations=5,
            time_limit_seconds=None,
        )
    )
    experiment_fields = asdict(
        config_for_variant(
            args,
            experiment3,
            seed=7,
            max_iterations=5,
            time_limit_seconds=None,
        )
    )

    assert experiment3.name == "experiment3"
    assert {
        name
        for name in baseline_fields
        if baseline_fields[name] != experiment_fields[name]
    } == {"enable_soft_deadline"}
    assert experiment_fields["soft_deadline_beta"] == 0.30


def test_experiment4_combines_all_three_adaptive_features() -> None:
    args = _parse_args([])
    baseline, _, _, _, experiment4, _ = build_variants()
    baseline_config = config_for_variant(
        args,
        baseline,
        seed=7,
        max_iterations=5,
        time_limit_seconds=None,
    )
    combined = config_for_variant(
        args,
        experiment4,
        seed=7,
        max_iterations=5,
        time_limit_seconds=None,
    )
    changed = {
        name
        for name, value in asdict(baseline_config).items()
        if value != asdict(combined)[name]
    }

    assert experiment4.name == "experiment4"
    assert changed == {
        "enable_late_risk_destroy",
        "enable_rejection_pool",
        "enable_soft_deadline",
    }


def test_experiment5_defers_the_rejection_pool_after_late_risk_destroy() -> None:
    args = _parse_args([])
    baseline, _, _, _, _, experiment5 = build_variants()
    baseline_config = config_for_variant(
        args,
        baseline,
        seed=7,
        max_iterations=5,
        time_limit_seconds=None,
    )
    treatment = config_for_variant(
        args,
        experiment5,
        seed=7,
        max_iterations=5,
        time_limit_seconds=None,
    )
    changed = {
        name
        for name, value in asdict(baseline_config).items()
        if value != asdict(treatment)[name]
    }

    assert experiment5.name == "experiment5"
    assert changed == {
        "enable_late_risk_destroy",
        "enable_rejection_pool",
        "defer_rejected_tasks",
        "enable_on_time_distance_objective",
    }


def test_paired_comparisons_use_the_official_lexicographic_order() -> None:
    rows = [
        {
            "scenario": "wall",
            "method": "baseline",
            "seed": 1,
            "late_count": 5,
            "total_lateness_min": 10.0,
            "distance_km": 100.0,
        },
        {
            "scenario": "wall",
            "method": "experiment1",
            "seed": 1,
            "late_count": 4,
            "total_lateness_min": 999.0,
            "distance_km": 999.0,
        },
        {
            "scenario": "wall",
            "method": "baseline",
            "seed": 2,
            "late_count": 5,
            "total_lateness_min": 10.0,
            "distance_km": 100.0,
        },
        {
            "scenario": "wall",
            "method": "experiment1",
            "seed": 2,
            "late_count": 5,
            "total_lateness_min": 11.0,
            "distance_km": 1.0,
        },
    ]

    summaries, pairs = paired_comparisons(rows)

    assert summaries == [
        {
            "scenario": "wall",
            "baseline": "baseline",
            "method": "experiment1",
            "pairs": 2,
            "wins": 1,
            "losses": 1,
            "ties": 0,
        }
    ]
    assert [pair["outcome"] for pair in pairs] == ["win", "loss"]


def test_phase2_runner_writes_valid_routes_csv_and_json(tmp_path) -> None:
    source = tmp_path / "tasks.csv"
    _write_tiny_tasks(source)
    output = tmp_path / "results"
    args = _parse_args(
        [
            "--input",
            str(source),
            "--output-dir",
            str(output),
            "--methods",
            "baseline",
            "experiment1",
            "experiment2",
            "experiment3",
            "--equal-seed-count",
            "1",
            "--equal-iterations",
            "1",
            "--wall-seed-count",
            "0",
        ]
    )

    payload = run(args)

    assert len(payload["runs"]) == 4
    assert all(row["valid"] for row in payload["runs"])
    rejection_row = next(
        row for row in payload["runs"] if row["method"] == "experiment2"
    )
    assert rejection_row["final_rejected_count"] == 0
    assert rejection_row["peak_rejected_count"] <= 0.10 * 2
    soft_row = next(row for row in payload["runs"] if row["method"] == "experiment3")
    assert soft_row["enable_soft_deadline"] is True
    assert soft_row["soft_deadline_beta"] == 0.20
    assert soft_row["internal_search_score_enabled"] is True
    assert (output / "adaptive_runs.csv").is_file()
    assert (output / "adaptive_summary.csv").is_file()
    assert (output / "adaptive_results.json").is_file()
    assert (output / "adaptive_runs.partial.json").is_file()
    assert (output / "adaptive_runs.partial.csv").is_file()
    assert (output / "paired_comparisons.csv").is_file()
    assert (output / "paired_comparison_pairs.csv").is_file()
    assert len(tuple((output / "run_solutions").glob("*.json"))) == 4

    persisted = json.loads(
        (output / "adaptive_results.json").read_text(encoding="utf-8")
    )
    assert len(persisted["paired_comparisons"]) == 3
    assert len(persisted["paired_comparison_pairs"]) == 3
    signature = persisted["manifest"]["checkpoint_signature"]
    assert signature["schema_version"] >= 1
    assert signature["input"]["sha256"] == hashlib.sha256(
        source.read_bytes()
    ).hexdigest()
    assert len(signature["materialized_runs"]) == 4
    assert any(
        path.endswith("run_adaptive_deadline_rejection.py")
        for path in signature["source_code_sha256"]
    )
    assert any(
        path.endswith("uav_dispatch/alns.py")
        for path in signature["source_code_sha256"]
    )

    with (output / "adaptive_runs.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        assert len(list(csv.DictReader(handle))) == 4
    for row in payload["runs"]:
        solution = output / row["solution_file"]
        assert hashlib.sha256(solution.read_bytes()).hexdigest() == row[
            "solution_sha256"
        ]
        solution_payload = json.loads(solution.read_text(encoding="utf-8"))
        assert solution_payload["score"]["late_count"] == row["late_count"]


def test_wall_scenario_name_budget_and_compliance_are_dynamic(tmp_path) -> None:
    source = tmp_path / "tasks.csv"
    _write_tiny_tasks(source)
    output = tmp_path / "wall-results"
    args = _parse_args(
        [
            "--input",
            str(source),
            "--output-dir",
            str(output),
            "--methods",
            "baseline",
            "--equal-seed-count",
            "0",
            "--wall-seed-count",
            "1",
            "--wall-time-limit",
            "2.5",
            "--wall-safety-margin",
            "0.1",
            "--wall-max-iterations",
            "1",
        ]
    )

    payload = run(args)

    row = payload["runs"][0]
    assert row["scenario"] == "equal_wall_clock_2.5s"
    assert row["budget_seconds"] == 2.5
    assert 0 < row["effective_search_time_limit_seconds"] < 2.5
    assert row["runtime_seconds"] <= row["budget_seconds"]
    assert row["compliant"] is True
    summary = payload["summaries"][0]
    assert summary["valid_rate"] == 1.0
    assert summary["compliant_rate"] == 1.0
    assert summary["max_final_rejected_count"] == 0
    assert summary["max_peak_rejected_count"] == 0


def test_resume_skips_completed_runs_and_rejects_signature_changes(
    tmp_path, monkeypatch
) -> None:
    source = tmp_path / "tasks.csv"
    _write_tiny_tasks(source)
    output = tmp_path / "resume-results"
    argv = _tiny_run_argv(source, output)
    first = run(_parse_args(argv))
    first_rows = first["runs"]

    def unexpected_solver_call(*args, **kwargs):
        raise AssertionError("resume reran an already checkpointed solve")

    monkeypatch.setattr(
        experiment_runner, "solve_alns_core", unexpected_solver_call
    )
    resumed = run(_parse_args([*argv, "--resume"]))

    assert resumed["runs"] == first_rows
    assert len({
        (row["scenario"], row["method"], row["seed"])
        for row in resumed["runs"]
    }) == len(first_rows)

    changed = [
        *argv,
        "--resume",
        "--risk-aware-lateness-lambda",
        "2.0",
    ]
    with pytest.raises(RuntimeError, match="断点.*不一致"):
        run(_parse_args(changed))
