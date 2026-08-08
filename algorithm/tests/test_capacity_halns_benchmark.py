import json

import pytest

import algorithm.experiments.run_capacity_halns_benchmark as benchmark_module
from algorithm.experiments.run_capacity_halns_benchmark import (
    _parse_args,
    build_variants,
    config_for_variant,
    paired_lexicographic_comparison,
    rotate_variants,
    run,
)


def test_capacity_halns_variants_hold_a2_constant_except_new_operators():
    baseline, capacity_halns = build_variants()

    assert baseline.code == "a2"
    assert capacity_halns.code == "capacity_halns"
    assert baseline.enable_assignment_destroy
    assert baseline.enable_deadline_risk
    assert not baseline.enable_vnd
    assert not baseline.enable_cluster_repair
    assert not baseline.enable_ejection
    assert not baseline.enable_route_pool
    assert not baseline.enable_pair_repair
    assert not baseline.enable_hypergraph_destroy
    assert capacity_halns.flags() == {
        **baseline.flags(),
        "enable_pair_repair": True,
        "enable_hypergraph_destroy": True,
    }
    assert rotate_variants((baseline, capacity_halns), 1) == (
        capacity_halns,
        baseline,
    )


def test_benchmark_builds_matched_wall_clock_configs_and_quick_plan():
    args = _parse_args(["--quick"])
    baseline, capacity_halns = build_variants()

    assert args.seed_count == 1
    assert args.time_limit == 10.0
    assert args.safety_margin == 0.5
    control = config_for_variant(
        args,
        baseline,
        seed=7,
        search_time_limit=8.5,
    )
    treatment = config_for_variant(
        args,
        capacity_halns,
        seed=7,
        search_time_limit=8.5,
    )

    assert control.seed == treatment.seed == 7
    assert control.max_iterations == treatment.max_iterations == 1_000
    assert control.time_limit_seconds == treatment.time_limit_seconds == 8.5
    assert control.candidate_limit == treatment.candidate_limit == 48
    assert control.pair_candidate_limit == treatment.pair_candidate_limit == 8
    assert control.enable_pair_repair is False
    assert control.enable_hypergraph_destroy is False
    assert treatment.enable_pair_repair is True
    assert treatment.enable_hypergraph_destroy is True


def test_benchmark_persists_complete_results_and_refuses_to_overwrite(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "tasks.csv"
    source.write_text(
        "task_id,pickup_x,pickup_y,delivery_x,delivery_y,deadline_min\n"
        "1,1,0,2,0,10\n"
        "2,-1,0,-2,0,10\n",
        encoding="utf-8",
    )
    output = tmp_path / "capacity_halns"
    args = _parse_args(
        [
            "--input",
            str(source),
            "--output-dir",
            str(output),
            "--seed-count",
            "1",
            "--time-limit",
            "0.2",
            "--safety-margin",
            "0.02",
            "--max-iterations",
            "2",
        ]
    )
    cache_clears = []
    original_clear = benchmark_module.graph_module.clear_interaction_graph_cache

    def tracked_clear():
        cache_clears.append(True)
        original_clear()

    monkeypatch.setattr(
        benchmark_module.graph_module,
        "clear_interaction_graph_cache",
        tracked_clear,
    )

    payload = run(args)

    assert len(cache_clears) == 2
    assert len(payload["runs"]) == 2
    assert payload["manifest"]["objective_order"] == [
        "late_count",
        "total_lateness_min",
        "distance_km",
    ]
    assert {row["variant"] for row in payload["runs"]} == {
        "a2",
        "capacity_halns",
    }
    assert all(row["valid"] and row["compliant"] for row in payload["runs"])
    assert (output / "capacity_halns_runs.csv").is_file()
    assert (output / "capacity_halns_summary.csv").is_file()
    assert (output / "paired_comparisons.csv").is_file()
    assert (output / "BENCHMARK_REPORT.md").is_file()
    persisted = json.loads(
        (output / "capacity_halns_results.json").read_text(encoding="utf-8")
    )
    assert persisted["paired_comparison"]["paired_count"] == 1
    assert len(list((output / "run_solutions").glob("*.json"))) == 2

    with pytest.raises(FileExistsError, match="覆盖"):
        run(args)


def test_paired_comparison_never_trades_priority_for_distance():
    rows = [
        {
            "seed": 1,
            "variant": "a2",
            "late_count": 2,
            "total_lateness_min": 0.0,
            "distance_km": 1.0,
            "runtime_seconds": 1.0,
            "iterations": 10,
        },
        {
            "seed": 1,
            "variant": "capacity_halns",
            "late_count": 1,
            "total_lateness_min": 100.0,
            "distance_km": 1_000.0,
            "runtime_seconds": 1.0,
            "iterations": 10,
        },
        {
            "seed": 2,
            "variant": "a2",
            "late_count": 1,
            "total_lateness_min": 1.0,
            "distance_km": 100.0,
            "runtime_seconds": 1.0,
            "iterations": 10,
        },
        {
            "seed": 2,
            "variant": "capacity_halns",
            "late_count": 1,
            "total_lateness_min": 2.0,
            "distance_km": 1.0,
            "runtime_seconds": 1.0,
            "iterations": 10,
        },
    ]

    comparison = paired_lexicographic_comparison(rows)

    assert comparison["capacity_halns_win"] == 1
    assert comparison["a2_win"] == 1
    assert [pair["winner"] for pair in comparison["pairs"]] == [
        "capacity_halns",
        "a2",
    ]


def test_interrupted_benchmark_resumes_from_verified_checkpoint(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "tasks.csv"
    source.write_text(
        "task_id,pickup_x,pickup_y,delivery_x,delivery_y,deadline_min\n"
        "1,1,0,2,0,10\n"
        "2,-1,0,-2,0,10\n",
        encoding="utf-8",
    )
    output = tmp_path / "capacity_halns"
    args = _parse_args(
        [
            "--input",
            str(source),
            "--output-dir",
            str(output),
            "--seed-count",
            "1",
            "--time-limit",
            "0.2",
            "--safety-margin",
            "0.02",
            "--max-iterations",
            "1",
        ]
    )
    original_solve = benchmark_module.solve_alns
    call_count = 0

    def interrupt_second_run(*positional, **keywords):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("simulated interruption")
        return original_solve(*positional, **keywords)

    monkeypatch.setattr(
        benchmark_module,
        "solve_alns",
        interrupt_second_run,
    )
    with pytest.raises(RuntimeError, match="simulated interruption"):
        run(args)
    assert (output / "capacity_halns_runs.partial.json").is_file()
    assert len(list((output / "run_solutions").glob("*.json"))) == 1

    monkeypatch.setattr(benchmark_module, "solve_alns", original_solve)
    args.resume = True
    payload = run(args)

    assert len(payload["runs"]) == 2
    assert payload["manifest"]["settings"]["resumed_from_checkpoint"] is True
    assert not (output / "capacity_halns_runs.partial.json").exists()
