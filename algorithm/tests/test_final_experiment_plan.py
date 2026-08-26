import json
from types import SimpleNamespace

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


def test_final_scenario_plan_accumulates_one_240_second_module_per_extension():
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
            relay_or_station_bonus_seconds=240.0,
        )
        for scenario in SCENARIOS
    }
    assert budgets == {
        "pure_direct": 240.0,
        "direct_relay": 480.0,
        "direct_stations": 480.0,
        "direct_relay_stations": 720.0,
    }
    assert {
        scenario["id"]: scenario["warmup"] for scenario in SCENARIOS
    } == {
        "pure_direct": 0.0,
        "direct_relay": 0.5,
        "direct_stations": 0.0,
        "direct_relay_stations": pytest.approx(2 / 3),
    }


def test_sensitivity_can_override_the_combined_budget_to_fixed_300_seconds():
    combined = next(
        scenario
        for scenario in SCENARIOS
        if scenario["id"] == "direct_relay_stations"
    )

    assert scenario_time_limit_seconds(
        combined,
        base_seconds=240.0,
        relay_or_station_bonus_seconds=240.0,
        effective_seconds_override=300.0,
    ) == 300.0


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
        ("--bonus-seconds", "60"),
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
    assert manifest["base_seconds"] == 240.0
    assert manifest["bonus_seconds"] == 240.0
    assert manifest["scenario_budgets_seconds"] == {
        "pure_direct": 240.0,
        "direct_relay": 480.0,
        "direct_stations": 480.0,
        "direct_relay_stations": 720.0,
    }
    assert manifest["scenario_stage_targets_seconds"] == {
        "pure_direct": {"direct_search": 240.0, "relay_search": 0.0},
        "direct_relay": {"direct_search": 240.0, "relay_search": 240.0},
        "direct_stations": {"direct_search": 480.0, "relay_search": 0.0},
        "direct_relay_stations": {
            "direct_search": 480.0,
            "relay_search": pytest.approx(240.0),
        },
    }
    assert manifest["solver_config"]["alns_by_scenario"]["direct_relay"][
        "relay_direct_warmup_fraction"
    ] == 0.5
    assert manifest["solver_config"]["alns_by_scenario"][
        "direct_relay_stations"
    ]["relay_direct_warmup_fraction"] == pytest.approx(2 / 3)
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
    assert manifest["effective_seconds_per_run"] == 300.0
    assert manifest["solver_config"]["time_limit_policy"].startswith(
        "fixed 300 s"
    )
    assert manifest["solver_config"]["alns_combined"][
        "relay_direct_warmup_fraction"
    ] == 0.8
    assert manifest["scenario"]["warmup"] == 0.8
    assert manifest["solver_config"]["baseline"] == {
        "drone_count": 8,
        "deadline_multiplier": 1.0,
        "station_count": 4,
    }
    assert manifest["levels"] == SENSITIVITY_LEVELS


def test_sensitivity_main_passes_300_seconds_as_an_explicit_override(
    monkeypatch,
    tmp_path,
):
    tasks = tuple(
        Task(
            task_id,
            Point(float(task_id), 0.0),
            Point(float(task_id), 1.0),
            60.0,
        )
        for task_id in range(1, 201)
    )
    overrides = []

    def fake_run_scenario(*_args, **kwargs):
        overrides.append(kwargs["effective_seconds_override"])
        return {
            "scenario": "direct_relay_stations",
            "late_count": 0,
            "total_lateness_min": 0.0,
            "distance_km": 0.0,
            "relay_task_count": 0,
        }

    monkeypatch.setattr(
        sensitivity_experiment,
        "load_tasks_csv",
        lambda _path: tasks,
    )
    monkeypatch.setattr(sensitivity_experiment, "run_scenario", fake_run_scenario)

    assert sensitivity_experiment.main(
        ["--output-dir", str(tmp_path)]
    ) == 0
    assert len(overrides) == 15
    assert set(overrides) == {300.0}


def _write_complete_scenario_shards(output_dir):
    args = scenario_experiment._parser().parse_args(
        ["--output-dir", str(output_dir)]
    )
    shard_dir = output_dir / "shards"
    shard_dir.mkdir(parents=True)
    solution_dir = output_dir / "run_solutions"
    solution_dir.mkdir(parents=True)
    expected_budgets = {
        "pure_direct": 240.0,
        "direct_relay": 480.0,
        "direct_stations": 480.0,
        "direct_relay_stations": 720.0,
    }
    for seed in scenario_experiment.DEFAULT_SEEDS:
        rows = []
        for index, scenario in enumerate(SCENARIOS):
            budget = expected_budgets[scenario["id"]]
            construction_seconds = 0.5
            solver_runtime_seconds = budget - construction_seconds - 0.05
            warmup_fraction = float(scenario["warmup"])
            solution_file = (
                f"run_solutions/{scenario['id']}__seed_{seed}.json"
            )
            solution_path = output_dir / solution_file
            solution_path.write_text(
                json.dumps({"scenario": scenario["id"], "seed": seed})
                + "\n",
                encoding="utf-8",
            )
            rows.append(
                {
                    "scenario": scenario["id"],
                    "scenario_label": scenario["label"],
                    "seed": seed,
                    "relay": bool(scenario["relay"]),
                    "station_predeployment": (
                        scenario["drone_homes"] == "stations"
                    ),
                    "effective_time_limit_seconds": expected_budgets[
                        scenario["id"]
                    ],
                    "construction_seconds": construction_seconds,
                    "solver_runtime_seconds": solver_runtime_seconds,
                    "relay_warmup_runtime_seconds": (
                        solver_runtime_seconds * warmup_fraction
                        if scenario["relay"]
                        else 0.0
                    ),
                    "relay_search_runtime_seconds": (
                        solver_runtime_seconds * (1.0 - warmup_fraction)
                        if scenario["relay"]
                        else 0.0
                    ),
                    "late_count": index,
                    "total_lateness_min": float(index),
                    "distance_km": 100.0 + index,
                    "relay_task_count": int(bool(scenario["relay"])),
                    "wall_seconds": expected_budgets[scenario["id"]] - 0.05,
                    "solution_file": solution_file,
                    "solution_sha256": scenario_experiment._sha256(
                        solution_path
                    ),
                }
            )
        payload = scenario_experiment._build_seed_shard_payload(
            args,
            seed=seed,
            rows=rows,
        )
        (shard_dir / f"seed_{seed}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def test_scenario_worker_seed_is_restricted_to_the_three_formal_seeds():
    for seed in scenario_experiment.DEFAULT_SEEDS:
        args = scenario_experiment._parser().parse_args(
            ["--worker-seed", str(seed)]
        )
        assert args.worker_seed == seed

    with pytest.raises(SystemExit):
        scenario_experiment._parser().parse_args(["--worker-seed", "7"])
    with pytest.raises(SystemExit):
        scenario_experiment._parser().parse_args(
            [
                "--worker-seed",
                str(scenario_experiment.DEFAULT_SEEDS[0]),
                "--finalize-shards",
            ]
        )


def test_scenario_worker_writes_only_its_seed_shard(monkeypatch, tmp_path):
    tasks = tuple(
        Task(
            task_id,
            Point(float(task_id), 0.0),
            Point(float(task_id), 1.0),
            60.0,
        )
        for task_id in range(1, 201)
    )
    selected_seed = scenario_experiment.DEFAULT_SEEDS[1]
    calls = []

    def fake_run_scenario(_tasks, scenario, **kwargs):
        calls.append((scenario["id"], kwargs["seed"]))
        return {
            "scenario": scenario["id"],
            "seed": kwargs["seed"],
            "effective_time_limit_seconds": scenario_time_limit_seconds(
                scenario,
                base_seconds=kwargs["base_seconds"],
                relay_or_station_bonus_seconds=kwargs["bonus_seconds"],
            ),
            "solution_file": (
                f"run_solutions/{scenario['id']}__seed_{kwargs['seed']}.json"
            ),
        }

    monkeypatch.setattr(
        scenario_experiment,
        "load_tasks_csv",
        lambda _path: tasks,
    )
    monkeypatch.setattr(scenario_experiment, "run_scenario", fake_run_scenario)

    assert scenario_experiment.main(
        [
            "--output-dir",
            str(tmp_path),
            "--worker-seed",
            str(selected_seed),
        ]
    ) == 0
    assert {scenario for scenario, _seed in calls} == {
        scenario["id"] for scenario in SCENARIOS
    }
    assert {seed for _scenario, seed in calls} == {selected_seed}
    shard = json.loads(
        (tmp_path / "shards" / f"seed_{selected_seed}.json").read_text(
            encoding="utf-8"
        )
    )
    assert shard["seed"] == selected_seed
    assert len(shard["rows"]) == 4
    for name in (
        "runs.json",
        "summary.json",
        "manifest.json",
        "runs.csv",
        "summary.csv",
        "REPORT.md",
    ):
        assert not (tmp_path / name).exists()


def test_finalize_shards_does_not_solve_and_reuses_final_output_logic(
    monkeypatch,
    tmp_path,
):
    _write_complete_scenario_shards(tmp_path)
    monkeypatch.setattr(
        scenario_experiment,
        "load_tasks_csv",
        lambda _path: pytest.fail("finalize must not load task data"),
    )
    monkeypatch.setattr(
        scenario_experiment,
        "run_scenario",
        lambda *_args, **_kwargs: pytest.fail("finalize must not solve"),
    )

    assert scenario_experiment.main(
        ["--output-dir", str(tmp_path), "--finalize-shards"]
    ) == 0
    rows = json.loads((tmp_path / "runs.json").read_text(encoding="utf-8"))
    assert len(rows) == 12
    assert {
        (row["seed"], row["scenario"]) for row in rows
    } == {
        (seed, scenario["id"])
        for seed in scenario_experiment.DEFAULT_SEEDS
        for scenario in SCENARIOS
    }
    manifest = json.loads(
        (tmp_path / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["scenario_budgets_seconds"] == {
        "pure_direct": 240.0,
        "direct_relay": 480.0,
        "direct_stations": 480.0,
        "direct_relay_stations": 720.0,
    }
    for name in ("summary.json", "runs.csv", "summary.csv", "REPORT.md"):
        assert (tmp_path / name).is_file()
    report = (tmp_path / "REPORT.md").read_text(encoding="utf-8")
    assert "Direct阶段/s" in report
    assert "Relay阶段/s" in report


def test_finalize_shards_rejects_a_wrong_budget(tmp_path):
    _write_complete_scenario_shards(tmp_path)
    shard_path = (
        tmp_path
        / "shards"
        / f"seed_{scenario_experiment.DEFAULT_SEEDS[0]}.json"
    )
    shard = json.loads(shard_path.read_text(encoding="utf-8"))
    shard["rows"][0]["effective_time_limit_seconds"] = 300.0
    shard_path.write_text(json.dumps(shard), encoding="utf-8")

    with pytest.raises(ValueError, match="预算"):
        scenario_experiment.main(
            ["--output-dir", str(tmp_path), "--finalize-shards"]
        )


def test_finalize_shards_rejects_a_missing_solution_file(tmp_path):
    _write_complete_scenario_shards(tmp_path)
    missing = (
        tmp_path
        / "run_solutions"
        / (
            "pure_direct__seed_"
            f"{scenario_experiment.DEFAULT_SEEDS[0]}.json"
        )
    )
    missing.unlink()

    with pytest.raises(ValueError, match="解文件"):
        scenario_experiment.main(
            ["--output-dir", str(tmp_path), "--finalize-shards"]
        )


def test_finalize_shards_rejects_an_incorrect_relay_phase_split(tmp_path):
    _write_complete_scenario_shards(tmp_path)
    shard_path = (
        tmp_path
        / "shards"
        / f"seed_{scenario_experiment.DEFAULT_SEEDS[0]}.json"
    )
    shard = json.loads(shard_path.read_text(encoding="utf-8"))
    relay_row = next(
        row for row in shard["rows"] if row["scenario"] == "direct_relay"
    )
    relay_row["relay_warmup_runtime_seconds"] = 300.0
    relay_row["relay_search_runtime_seconds"] = (
        relay_row["solver_runtime_seconds"] - 300.0
    )
    shard_path.write_text(json.dumps(shard), encoding="utf-8")

    with pytest.raises(ValueError, match="阶段时长"):
        scenario_experiment.main(
            ["--output-dir", str(tmp_path), "--finalize-shards"]
        )


def test_run_scenario_exposes_relay_phase_runtimes(monkeypatch, tmp_path):
    scenario = next(
        item for item in SCENARIOS if item["id"] == "direct_relay"
    )
    problem = SimpleNamespace(
        drone_homes=None,
        drone_count=8,
        relay_stations=(),
    )
    evaluation = SimpleNamespace(
        valid=True,
        violations=(),
        score=SimpleNamespace(
            late_count=0,
            total_lateness_min=0.0,
            distance_km=1.0,
        ),
    )
    result = SimpleNamespace(
        routes=((),) * 8,
        evaluation=evaluation,
        runtime_seconds=479.0,
        iterations=10,
        metadata={
            "method": "C2-Lex-ALNS-Staged",
            "relay": {},
            "operator_statistics": {},
            "relay_warmup_runtime_seconds": 239.5,
            "relay_search_runtime_seconds": 239.5,
        },
    )
    monkeypatch.setattr(
        scenario_experiment,
        "build_scenario_problem",
        lambda *_args, **_kwargs: (problem, None),
    )
    monkeypatch.setattr(
        scenario_experiment,
        "construct_regret_initial",
        lambda *_args, **_kwargs: SimpleNamespace(routes=((),) * 8),
    )
    monkeypatch.setattr(
        scenario_experiment,
        "solve_relay_staged",
        lambda *_args, **_kwargs: result,
    )
    monkeypatch.setattr(
        scenario_experiment,
        "result_payload",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        scenario_experiment,
        "deployment_distribution",
        lambda _homes: {},
    )
    monkeypatch.setattr(
        scenario_experiment,
        "deadhead_km",
        lambda *_args, **_kwargs: 0.0,
    )
    monkeypatch.setattr(
        scenario_experiment,
        "home_assignment_rate",
        lambda *_args, **_kwargs: 0.0,
    )

    row = scenario_experiment.run_scenario(
        (),
        scenario,
        seed=scenario_experiment.DEFAULT_SEEDS[0],
        output_dir=tmp_path,
        input_path=scenario_experiment.DEFAULT_INPUT,
        drones=8,
        max_tasks=25,
        base_seconds=240.0,
        bonus_seconds=240.0,
        safety_margin_seconds=0.05,
        max_iterations=10,
        candidate_limit=48,
        relay_count="auto",
        relay_location_seed=42,
    )

    assert row["relay_warmup_runtime_seconds"] == 239.5
    assert row["relay_search_runtime_seconds"] == 239.5


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
