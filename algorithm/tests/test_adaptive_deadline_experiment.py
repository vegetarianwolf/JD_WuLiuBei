from dataclasses import asdict

from algorithm.experiments.run_adaptive_deadline_rejection import (
    _parse_args,
    build_variants,
    config_for_variant,
    run,
)


def test_phase2_variants_are_explicit_a2_configs_differing_only_in_late_risk() -> None:
    args = _parse_args([])
    baseline, experiment1, _ = build_variants()

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
    baseline, _, experiment2 = build_variants()
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


def test_phase2_runner_writes_valid_routes_csv_and_json(tmp_path) -> None:
    source = tmp_path / "tasks.csv"
    source.write_text(
        "task_id,pickup_x,pickup_y,delivery_x,delivery_y,deadline_min\n"
        "1,1,0,2,0,4\n"
        "2,0,1,0,2,5\n",
        encoding="utf-8",
    )
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
            "--equal-seed-count",
            "1",
            "--equal-iterations",
            "1",
            "--wall-seed-count",
            "0",
        ]
    )

    payload = run(args)

    assert len(payload["runs"]) == 3
    assert all(row["valid"] for row in payload["runs"])
    rejection_row = next(
        row for row in payload["runs"] if row["method"] == "experiment2"
    )
    assert rejection_row["final_rejected_count"] == 0
    assert rejection_row["peak_rejected_count"] <= 0.10 * 2
    assert (output / "adaptive_runs.csv").is_file()
    assert (output / "adaptive_summary.csv").is_file()
    assert (output / "adaptive_results.json").is_file()
    assert len(tuple((output / "run_solutions").glob("*.json"))) == 3
