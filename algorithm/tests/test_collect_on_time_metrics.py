from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "experiments" / "collect_on_time_metrics.py"
SPEC = importlib.util.spec_from_file_location("collect_on_time_metrics", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _run(family: str, seed: str, on_time: int, distance: float, label: str):
    return {
        "experiment_family": family,
        "scenario": "equal_wall_clock_240s",
        "seed": seed,
        "task_count": 200,
        "on_time_count": on_time,
        "on_time_rate": on_time / 200,
        "distance_km": distance,
        "label": label,
    }


def test_comparison_uses_on_time_count_before_distance() -> None:
    control = [
        _run("control", "2026080500", 130, 640.0, "control"),
        _run("control", "2026080501", 130, 640.0, "control"),
        _run("control", "2026080502", 130, 640.0, "control"),
    ]
    treatment = [
        _run("treatment", "2026080500", 131, 900.0, "treatment"),
        _run("treatment", "2026080501", 130, 639.0, "treatment"),
        _run("treatment", "2026080502", 129, 500.0, "treatment"),
    ]

    result = MODULE._comparison("branch", control, treatment)

    assert "treatment_wins" not in result
    assert "ties" not in result
    assert "treatment_losses" not in result
    assert result["on_time_count_delta"] == 0
    assert result["distance_km_delta"] == pytest.approx(39.6666666667)


def test_comparison_rejects_duplicate_seed_instead_of_overwriting() -> None:
    control = [
        _run("control", "2026080500", 130, 640.0, "control"),
        _run("control", "2026080500", 131, 641.0, "control"),
        _run("control", "2026080502", 130, 640.0, "control"),
    ]
    treatment = [
        _run("treatment", "2026080500", 131, 639.0, "treatment"),
        _run("treatment", "2026080501", 131, 639.0, "treatment"),
        _run("treatment", "2026080502", 131, 639.0, "treatment"),
    ]

    with pytest.raises(ValueError, match="duplicate control seed"):
        MODULE._comparison("branch", control, treatment)


def test_comparison_rejects_non_240_second_scenario() -> None:
    seeds = ("2026080500", "2026080501", "2026080502")
    control = [_run("control", seed, 130, 640.0, "control") for seed in seeds]
    treatment = [
        _run("treatment", seed, 131, 639.0, "treatment") for seed in seeds
    ]
    for row in control + treatment:
        row["scenario"] = "equal_wall_clock_120s"

    with pytest.raises(ValueError, match="must be equal_wall_clock_240s"):
        MODULE._comparison("branch", control, treatment)


def test_normalized_run_id_is_stable_and_on_time_count_is_cross_checked(tmp_path: Path) -> None:
    source = MODULE.Source(
        "fixture",
        "branch",
        tmp_path,
        "runs.csv",
        "family",
        "formal_240s",
        default_task_count=200,
    )
    row = {
        "scenario": "equal_wall_clock_240s",
        "method": "method",
        "seed": "2026080500",
        "late_count": "70",
        "on_time_count": "130",
        "on_time_rate": "0.65",
        "distance_km": "640.0",
        "valid": "True",
        "compliant": "True",
    }

    first = MODULE._normalize(row, source, "abc123", "filehash")
    second = MODULE._normalize(dict(reversed(tuple(row.items()))), source, "abc123", "filehash")

    assert first["run_id"] == second["run_id"]
    bad = dict(row, on_time_count="129")
    with pytest.raises(ValueError, match="on_time_count mismatch"):
        MODULE._normalize(bad, source, "abc123", "filehash")

    missing_gate = dict(row)
    missing_gate.pop("valid")
    with pytest.raises(ValueError, match="Required boolean field is missing"):
        MODULE._normalize(missing_gate, source, "abc123", "filehash")

    with pytest.raises(ValueError, match="late_count must be an integer"):
        MODULE._normalize(dict(row, late_count="70.5"), source, "abc123", "filehash")
    with pytest.raises(ValueError, match="distance_km must be finite"):
        MODULE._normalize(dict(row, distance_km="NaN"), source, "abc123", "filehash")


def test_uav_control_cannot_be_silently_labeled_as_treatment(tmp_path: Path) -> None:
    source = MODULE.Source(
        "uav_new",
        "codex/uav-alns-dispatch",
        tmp_path,
        "runs.csv",
        "uav_on_time_treatment",
        "new_formal_240s",
        is_new=True,
        default_task_count=200,
    )
    row = {
        "scenario": "equal_wall_clock_240s",
        "method": "halns_control",
        "treatment": "True",
        "seed": "2026080500",
        "late_count": "70",
        "on_time_rate": "0.65",
        "distance_km": "640.0",
        "valid": "True",
        "compliant": "True",
    }

    with pytest.raises(ValueError, match="contradicts method"):
        MODULE._normalize(row, source, "abc123", "filehash")


def test_direct_output_schema_excludes_diagnostic_metrics() -> None:
    assert "total_lateness_min" not in MODULE.DIRECT_COLUMNS
    assert "runtime_seconds" not in MODULE.DIRECT_COLUMNS
    assert "late_count" not in MODULE.DIRECT_COLUMNS
    assert {"on_time_count", "on_time_rate", "distance_km"} <= set(MODULE.DIRECT_COLUMNS)
