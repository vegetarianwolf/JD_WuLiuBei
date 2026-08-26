import hashlib
import json
import math

import pytest

import uav_dispatch.alns as alns_module
from uav_dispatch import Point, Problem, Task, evaluate_solution

from algorithm.experiments.run_virtual_ablation import (
    _operator_registry,
    generate_virtual_tasks,
    main,
)


def test_virtual_generator_is_deterministic_and_model_valid():
    first = generate_virtual_tasks(30, 2026082601)
    second = generate_virtual_tasks(30, 2026082601)

    assert first == second
    assert len(first) == 30
    assert all(0.0 <= task.pickup.x <= 20.0 for task in first)
    assert all(0.0 <= task.pickup.y <= 20.0 for task in first)
    assert all(0.0 <= task.delivery.x <= 20.0 for task in first)
    assert all(0.0 <= task.delivery.y <= 20.0 for task in first)
    problem = Problem(
        first,
        drone_count=2,
        max_tasks_per_drone=15,
        depot=Point(10.0, 10.0),
    )
    assert len(problem.tasks) == 30


def test_operator_registry_is_scoped_and_restored():
    original_destroy = alns_module.DESTROY_OPERATORS
    original_repair = alns_module.REPAIR_OPERATORS

    with _operator_registry(("random",), ("greedy",)):
        assert alns_module.DESTROY_OPERATORS == ("random",)
        assert alns_module.REPAIR_OPERATORS == ("greedy",)

    assert alns_module.DESTROY_OPERATORS == original_destroy
    assert alns_module.REPAIR_OPERATORS == original_repair

    with pytest.raises(RuntimeError, match="probe"):
        with _operator_registry(("random",), ("greedy",)):
            raise RuntimeError("probe")

    assert alns_module.DESTROY_OPERATORS == original_destroy
    assert alns_module.REPAIR_OPERATORS == original_repair


def test_quick_virtual_experiment_writes_auditable_bundle(tmp_path):
    output = tmp_path / "virtual"
    assert (
        main(
            [
                "--quick",
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )

    payload = json.loads((output / "results.json").read_text(encoding="utf-8"))
    assert payload["manifest"]["data_kind"] == "synthetic_virtual"
    assert payload["manifest"]["formal"] is False
    assert payload["manifest"]["budget_mode"] == "fixed_iterations"
    assert payload["manifest"]["search_budget_seconds"] is None
    assert payload["manifest"]["max_iterations"] == 12
    assert payload["manifest"]["instance_count"] == 2
    assert payload["manifest"]["run_count"] == 16
    assert len(payload["method_summary"]) == 4
    assert len(payload["ablation_summary"]) == 5
    assert all(row["valid"] for row in payload["runs"])
    variants = set(payload["manifest"]["variants"])
    assert all(
        row["iterations"] == payload["manifest"]["max_iterations"]
        for row in payload["runs"]
        if row["method_id"] in variants
    )
    assert all(
        row["iterations"] == 0
        for row in payload["runs"]
        if row["method_id"] not in variants
    )

    checksums = json.loads((output / "checksums.json").read_text(encoding="utf-8"))
    assert set(checksums) == {
        "results.json",
        "manifest.json",
        "runs.csv",
        "method_summary.csv",
        "ablation_summary.csv",
        "REPORT.md",
    }
    for filename, expected in checksums.items():
        assert hashlib.sha256((output / filename).read_bytes()).hexdigest() == expected

    instances = {row["instance_id"]: row for row in payload["instances"]}
    initial_hashes: dict[str, set[str]] = {}
    for row in payload["runs"]:
        instance = instances[row["instance_id"]]
        tasks = tuple(
            Task(
                task["id"],
                Point(*task["pickup"]),
                Point(*task["delivery"]),
                task["deadline_min"],
            )
            for task in instance["tasks"]
        )
        problem = Problem(
            tasks,
            drone_count=instance["drone_count"],
            max_tasks_per_drone=instance["max_tasks_per_drone"],
            capacity=instance["capacity"],
            speed_km_per_min=instance["speed_km_per_min"],
            depot=Point(*instance["depot_km"]),
        )
        evaluation = evaluate_solution(problem, row["routes"])
        assert evaluation.valid
        assert evaluation.score.late_count == row["late_count"]
        assert math.isclose(
            evaluation.score.total_lateness_min,
            row["total_lateness_min"],
            abs_tol=1e-9,
        )
        assert math.isclose(
            evaluation.score.distance_km,
            row["distance_km"],
            abs_tol=1e-9,
        )
        if row["method_id"] in payload["manifest"]["variants"]:
            initial_hashes.setdefault(row["instance_id"], set()).add(
                row["initial_routes_sha256"]
            )
            expected_operator_keys = {
                *(f"destroy:{name}" for name in row["active_destroy_operators"]),
                *(f"repair:{name}" for name in row["active_repair_operators"]),
            }
            assert set(row["operator_statistics"]) == expected_operator_keys
        if row["method_id"] == "uniform_operator_weights":
            assert set(row["operator_weights"].values()) == {1.0}
    assert all(len(values) == 1 for values in initial_hashes.values())
