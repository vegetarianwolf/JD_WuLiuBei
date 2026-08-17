import pytest

from algorithm.experiments import run_benchmarks as benchmark_experiment
from algorithm.experiments import run_scenario_comparison as scenario_experiment
from algorithm.experiments import run_sensitivity_analysis as sensitivity_experiment
from algorithm.experiments.run_scenario_comparison import (
    SCENARIOS,
    scenario_time_limit_seconds,
)
from algorithm.experiments.run_sensitivity_analysis import (
    SENSITIVITY_LEVELS,
    scale_deadlines,
)
from uav_dispatch import Point, Task


def test_final_scenario_plan_has_four_cases_and_grants_the_60_second_bonus():
    assert [scenario["id"] for scenario in SCENARIOS] == [
        "pure_direct",
        "direct_relay",
        "direct_stations",
        "direct_relay_stations",
    ]

    budgets = {
        scenario["id"]: scenario_time_limit_seconds(
            scenario,
            base_seconds=240.0,
            relay_or_station_bonus_seconds=60.0,
        )
        for scenario in SCENARIOS
    }
    assert budgets == {
        "pure_direct": 240.0,
        "direct_relay": 300.0,
        "direct_stations": 300.0,
        "direct_relay_stations": 300.0,
    }


def test_sensitivity_plan_uses_combined_mode_and_five_levels_per_parameter():
    assert SENSITIVITY_LEVELS == {
        "drone_count": (8, 9, 10, 12, 16),
        "deadline_multiplier": (0.8, 0.9, 1.0, 1.1, 1.2),
        "station_count": (2, 3, 4, 5, 6),
    }


def test_deadline_scaling_preserves_geometry_and_task_identity():
    task = Task(7, Point(1.0, 2.0), Point(3.0, 4.0), 50.0)

    scaled = scale_deadlines((task,), 0.8)

    assert scaled == (Task(7, task.pickup, task.delivery, 40.0),)


@pytest.mark.parametrize(
    "argv",
    (
        ("--seeds", "1", "2", "3"),
        ("--tasks", "24"),
        ("--base-seconds", "239"),
        ("--relay-count", "4"),
    ),
)
def test_formal_scenario_cli_rejects_non_protocol_overrides(monkeypatch, argv):
    monkeypatch.setattr(
        scenario_experiment,
        "load_tasks_csv",
        lambda _path: pytest.fail("formal validation must precede data loading"),
    )

    with pytest.raises(ValueError, match="正式四场景协议"):
        scenario_experiment.main(argv)


@pytest.mark.parametrize(
    "argv",
    (
        ("--seed", "7"),
        ("--tasks", "24"),
        ("--bonus-seconds", "59"),
        ("--candidate-limit", "24"),
    ),
)
def test_formal_sensitivity_cli_rejects_non_protocol_overrides(monkeypatch, argv):
    monkeypatch.setattr(
        sensitivity_experiment,
        "load_tasks_csv",
        lambda _path: pytest.fail("formal validation must precede data loading"),
    )

    with pytest.raises(ValueError, match="正式敏感性协议"):
        sensitivity_experiment.main(argv)


def test_formal_benchmark_cli_rejects_seed_override_before_loading_data(
    monkeypatch,
):
    monkeypatch.setattr(
        benchmark_experiment,
        "load_tasks_csv",
        lambda _path: pytest.fail("formal validation must precede data loading"),
    )

    with pytest.raises(ValueError, match="正式第5章协议"):
        benchmark_experiment.main(["--seed", "7"])


def test_formal_benchmark_cli_rejects_non_official_input_before_loading_data(
    monkeypatch,
    tmp_path,
):
    other_input = tmp_path / "other.csv"
    other_input.write_text("different input\n", encoding="utf-8")
    monkeypatch.setattr(
        benchmark_experiment,
        "load_tasks_csv",
        lambda _path: pytest.fail("formal validation must precede data loading"),
    )

    with pytest.raises(ValueError, match="input_sha256"):
        benchmark_experiment.main(["--input", str(other_input)])


@pytest.mark.parametrize(
    "argv",
    (
        ("--scale-seconds", "29"),
        ("--max-iterations", "999"),
        ("--candidate-limit", "24"),
    ),
)
def test_formal_benchmark_cli_rejects_non_protocol_alns_overrides(
    monkeypatch,
    argv,
):
    monkeypatch.setattr(
        benchmark_experiment,
        "load_tasks_csv",
        lambda _path: pytest.fail("formal validation must precede data loading"),
    )

    with pytest.raises(ValueError, match="正式第5章协议"):
        benchmark_experiment.main(argv)


def test_default_formal_configs_validate_and_quick_runs_may_override():
    scenario_experiment.validate_formal_protocol(
        scenario_experiment._parser().parse_args([])
    )
    sensitivity_experiment.validate_formal_protocol(
        sensitivity_experiment._parser().parse_args([])
    )
    scenario_experiment.validate_formal_protocol(
        scenario_experiment._parser().parse_args(
            ["--quick", "--seeds", "1", "2"]
        )
    )
    sensitivity_experiment.validate_formal_protocol(
        sensitivity_experiment._parser().parse_args(
            ["--quick", "--seed", "7"]
        )
    )
    benchmark_experiment.validate_formal_protocol(
        benchmark_experiment._parser().parse_args([])
    )
    benchmark_experiment.validate_formal_protocol(
        benchmark_experiment._parser().parse_args(
            [
                "--quick",
                "--seed",
                "7",
                "--scale-seconds",
                "0.25",
                "--max-iterations",
                "99",
                "--candidate-limit",
                "4",
            ]
        )
    )


def test_scenario_manifest_captures_the_reproducibility_contract():
    args = scenario_experiment._parser().parse_args([])

    manifest = scenario_experiment.build_manifest(args, task_count=200)

    assert manifest["schema_version"] == 1
    assert manifest["formal"] is True
    assert manifest["input_sha256"]
    assert manifest["solver_source_sha256"]
    assert manifest["experiment_script_sha256"]
    assert manifest["git_commit"] is None or len(manifest["git_commit"]) == 40
    assert isinstance(manifest["git_dirty"], (bool, type(None)))
    assert manifest["environment"]["python_version"]
    assert manifest["environment"]["platform"]
    assert "UAV_DISPATCH_LEGACY_CANDIDATE_EVAL" in manifest["environment"]
    assert manifest["solver_config"]["max_iterations"] == 10_000_000
    assert manifest["solver_config"]["candidate_limit"] == 48
    assert manifest["solver_config"]["relay_location_seed"] == 42
    assert manifest["solver_config"]["relay_location_method"] == "weighted_kmedoids"
    assert manifest["solver_config"]["relay_detour_ratio"] == 2.0
    assert set(manifest["solver_config"]["alns_by_scenario"]) == {
        scenario["id"] for scenario in SCENARIOS
    }


def test_sensitivity_manifest_captures_the_reproducibility_contract():
    args = sensitivity_experiment._parser().parse_args([])

    manifest = sensitivity_experiment.build_manifest(args, task_count=200)

    assert manifest["schema_version"] == 1
    assert manifest["formal"] is True
    assert manifest["input_sha256"]
    assert manifest["solver_source_sha256"]
    assert manifest["experiment_script_sha256"]
    assert manifest["scenario_script_sha256"]
    assert manifest["git_commit"] is None or len(manifest["git_commit"]) == 40
    assert isinstance(manifest["git_dirty"], (bool, type(None)))
    assert manifest["environment"]["python_version"]
    assert manifest["environment"]["platform"]
    assert manifest["solver_config"]["max_iterations"] == 10_000_000
    assert manifest["solver_config"]["candidate_limit"] == 48
    assert manifest["solver_config"]["relay_location_seed"] == 42
    assert manifest["solver_config"]["relay_location_method"] == "weighted_kmedoids"
    assert manifest["solver_config"]["baseline"] == {
        "drone_count": 8,
        "deadline_multiplier": 1.0,
        "station_count": 4,
    }
    assert manifest["levels"] == SENSITIVITY_LEVELS


def test_benchmark_manifest_captures_the_reproducibility_contract():
    args = benchmark_experiment._parser().parse_args([])

    manifest = benchmark_experiment.build_manifest(
        args,
        exact_sizes=benchmark_experiment.EXACT_SIZES,
        scale_counts=benchmark_experiment.MULTISCALE_TASK_COUNTS,
        input_task_count=200,
    )

    assert manifest["schema_version"] == 1
    assert manifest["formal"] is True
    assert manifest["exact_sizes"] == [5, 8, 10, 12]
    assert manifest["multiscale_task_counts"] == [50, 100, 150, 200]
    assert manifest["relaxed_deadline_multiplier"] == 2.5
    assert manifest["scale_seconds"] == 30.0
    assert manifest["seed"] == 2026081701
    assert manifest["input_task_count"] == 200
    assert manifest["input_sha256"]
    assert manifest["solver_source_sha256"]
    assert manifest["experiment_script_sha256"]
    assert manifest["git_commit"] is None or len(manifest["git_commit"]) == 40
    assert isinstance(manifest["git_dirty"], (bool, type(None)))
    assert manifest["environment"]["python_version"]
    assert manifest["environment"]["platform"]
    assert manifest["solver_config"]["exact_alns"]["max_iterations"] == 1_000
    assert manifest["solver_config"]["exact_alns"]["candidate_limit"] is None
    assert manifest["solver_config"]["multiscale_alns"]["max_iterations"] == 10_000_000
    assert manifest["solver_config"]["multiscale_alns"]["candidate_limit"] == 48
    assert manifest["solver_config"]["max_tasks_per_drone"] == 25


def test_quick_benchmark_manifest_is_explicitly_nonformal():
    args = benchmark_experiment._parser().parse_args(
        [
            "--quick",
            "--seed",
            "7",
            "--scale-seconds",
            "0.25",
            "--max-iterations",
            "99",
            "--candidate-limit",
            "4",
        ]
    )

    manifest = benchmark_experiment.build_manifest(
        args,
        exact_sizes=benchmark_experiment.EXACT_SIZES[:2],
        scale_counts=benchmark_experiment.MULTISCALE_TASK_COUNTS[:2],
        input_task_count=200,
    )

    assert manifest["formal"] is False
    assert manifest["seed"] == 7
    assert manifest["scale_seconds"] == 0.25
    assert manifest["exact_sizes"] == [5, 8]
    assert manifest["multiscale_task_counts"] == [50, 100]
    assert manifest["solver_config"]["exact_alns"]["max_iterations"] == 100
    assert manifest["solver_config"]["multiscale_alns"]["max_iterations"] == 99
    assert manifest["solver_config"]["multiscale_alns"]["candidate_limit"] == 4
