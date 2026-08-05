import argparse
import hashlib
import json

import pytest
from uav_dispatch import Point, Problem, Task

from algorithm.experiments.run_alns_core_comparison import (
    _checkpoint_signature,
    _load_checkpoint,
    paired_lexicographic_comparison,
    run,
)


def test_paired_comparison_uses_the_strict_score_tuple():
    rows = [
        {
            "seed": 1,
            "method": "core",
            "late_count": 5,
            "total_lateness_min": 10.0,
            "distance_km": 10.0,
        },
        {
            "seed": 1,
            "method": "hybrid",
            "late_count": 4,
            "total_lateness_min": 999.0,
            "distance_km": 999.0,
        },
        {
            "seed": 2,
            "method": "core",
            "late_count": 4,
            "total_lateness_min": 10.0,
            "distance_km": 20.0,
        },
        {
            "seed": 2,
            "method": "hybrid",
            "late_count": 4,
            "total_lateness_min": 10.0,
            "distance_km": 19.0,
        },
        {
            "seed": 3,
            "method": "core",
            "late_count": 3,
            "total_lateness_min": 100.0,
            "distance_km": 100.0,
        },
        {
            "seed": 3,
            "method": "hybrid",
            "late_count": 4,
            "total_lateness_min": 1.0,
            "distance_km": 1.0,
        },
    ]

    comparison = paired_lexicographic_comparison(
        rows, left_method="core", right_method="hybrid"
    )

    assert comparison["left_win"] == 1
    assert comparison["tie"] == 0
    assert comparison["right_win"] == 2
    assert comparison["paired_count"] == 3
    assert comparison["pairs"][0]["winner"] == "hybrid"
    assert comparison["pairs"][0]["late_count_delta_right_minus_left"] == -1


def test_resume_rejects_failed_or_mismatched_checkpoint_rows(tmp_path):
    problem = Problem(
        (Task(1, Point(1, 0), Point(2, 0), deadline_min=10),)
    )
    checkpoint = tmp_path / "comparison_runs.partial.json"
    signature = {"schema_version": 1, "candidate_limit": 48}
    checkpoint.write_text(
        json.dumps(
            {
                "signature": signature,
                "rows": [
                    {
                        "scenario": "equal_iterations_1",
                        "method": "C2-Lex-ALNS-Core",
                        "seed": 1,
                        "valid": True,
                        "compliant": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="合法性或时限"):
        _load_checkpoint(
            checkpoint,
            signature=signature,
            output_dir=tmp_path,
            problem=problem,
            equal_scenario="equal_iterations_1",
            wall_scenario="equal_wall_clock_1s",
            equal_seeds={1},
            wall_seeds=set(),
        )
    with pytest.raises(RuntimeError, match="配置"):
        _load_checkpoint(
            checkpoint,
            signature={"schema_version": 1, "candidate_limit": 47},
            output_dir=tmp_path,
            problem=problem,
            equal_scenario="equal_iterations_1",
            wall_scenario="equal_wall_clock_1s",
            equal_seeds={1},
            wall_seeds=set(),
        )


def test_resume_skips_a_completed_equal_iteration_solver(
    tmp_path, monkeypatch
):
    source = tmp_path / "tasks.csv"
    source.write_text(
        "task_id,pickup_x,pickup_y,delivery_x,delivery_y,deadline_min\n"
        "1,1,0,2,0,10\n",
        encoding="utf-8",
    )
    previous = tmp_path / "previous.json"
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    previous.write_text(
        json.dumps(
            {
                "manifest": {
                    "assumptions": {
                        "depot_km": [0.0, 0.0],
                        "speed_km_per_min": 0.9,
                        "capacity": 2,
                        "open_routes": True,
                        "service_time_min": 0.0,
                        "deadlines_are_soft": True,
                    },
                    "settings": {"candidate_limit": 48},
                    "data_audit": {"source_sha256": source_hash},
                },
                "runs": [
                    {
                        "scenario": "fleet_n200",
                        "method": "C2-Lex-ALNS",
                        "seed": 1,
                        "iterations": 0,
                        "task_count": 1,
                        "drone_count": 8,
                        "max_tasks_per_drone": 25,
                        "late_count": 0,
                        "total_lateness_min": 0.0,
                        "distance_km": 2.0,
                    },
                    {
                        "scenario": "fleet_n200",
                        "method": "C2-Lex-HALNS",
                        "seed": 1,
                        "iterations": 0,
                        "task_count": 1,
                        "drone_count": 8,
                        "max_tasks_per_drone": 25,
                        "late_count": 0,
                        "total_lateness_min": 0.0,
                        "distance_km": 2.0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "output"
    output.mkdir()
    args = argparse.Namespace(
        input=source,
        previous_results=previous,
        output_dir=output,
        seed_base=1,
        equal_seed_count=1,
        equal_iterations=0,
        wall_seed_count=0,
        wall_time_limit=240.0,
        wall_safety_margin=2.0,
        wall_max_iterations=10_000,
        candidate_limit=48,
        resume=True,
    )
    solution_dir = output / "run_solutions"
    solution_dir.mkdir()
    core_solution = solution_dir / "core.json"
    core_solution.write_text(
        json.dumps(
            {
                "valid": True,
                "score": {
                    "late_count": 0,
                    "total_lateness_min": 0.0,
                    "distance_km": 2.0,
                },
                "routes": [{"encoded_visits": [1, -1]}],
            }
        ),
        encoding="utf-8",
    )
    completed_rows = [
        {
            "scenario": "equal_iterations_0",
            "method": method,
            "seed": 1,
            "valid": True,
            "compliant": True,
            "late_count": 0,
            "on_time_rate": 1.0,
            "total_lateness_min": 0.0,
            "distance_km": 2.0,
            "runtime_seconds": 0.1,
            "iterations": 0,
            "time_to_best_seconds": 0.1,
            "source": (
                "new_core_run"
                if method == "C2-Lex-ALNS-Core"
                else "previous_results:test"
            ),
            "solution_file": (
                "run_solutions/core.json"
                if method == "C2-Lex-ALNS-Core"
                else ""
            ),
            "solution_sha256": (
                hashlib.sha256(core_solution.read_bytes()).hexdigest()
                if method == "C2-Lex-ALNS-Core"
                else ""
            ),
            "matches_previous_core_score": (
                True if method == "C2-Lex-ALNS-Core" else ""
            ),
        }
        for method in ("C2-Lex-ALNS-Core", "C2-Lex-HALNS")
    ]
    (output / "comparison_runs.partial.json").write_text(
        json.dumps(
            {
                "signature": _checkpoint_signature(
                    args,
                    input_sha256=source_hash,
                    previous_results_sha256=hashlib.sha256(
                        previous.read_bytes()
                    ).hexdigest(),
                ),
                "rows": completed_rows,
            }
        ),
        encoding="utf-8",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("已完成的 Core 求解不应被重新调用")

    monkeypatch.setattr(
        "algorithm.experiments.run_alns_core_comparison.solve_alns_core",
        fail_if_called,
    )
    run(args)

    payload = json.loads((output / "comparison_results.json").read_text())
    assert len(payload["runs"]) == 2
    assert (output / "best_alns_core_equal_iterations_0.json").exists()
