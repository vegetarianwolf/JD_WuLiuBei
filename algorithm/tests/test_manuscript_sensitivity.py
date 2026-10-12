"""Regression checks for accepting only complete, consistently reused evidence."""

from copy import deepcopy
import json

import pytest

from algorithm.experiments import run_sensitivity_analysis as experiment
from writing import build_final_manuscript as manuscript


@pytest.fixture
def formal_evidence(monkeypatch, tmp_path):
    args = experiment._parser().parse_args([])
    manifest = json.loads(json.dumps(experiment.build_manifest(args, task_count=200)))
    rows = []
    solutions = {}
    specs = {spec["id"]: spec for spec in manifest["scenarios"]}
    for cell in experiment.build_cells(args):
        spec = specs[cell["scenario"]]
        row = {
            **cell,
            "seed": args.seed,
            "relay": spec["relay"],
            "station_predeployment": spec["drone_homes"] == "stations",
            "effective_time_limit_seconds": 300.0,
            "late_count": 10,
            "total_lateness_min": 50.0,
            "distance_km": 100.0,
            "relay_task_count": int(spec["relay"]),
            "cross_uav_handoff_count": int(spec["relay"]),
            "total_relay_waiting_min": float(spec["relay"]),
            "station_count": cell["requested_station_count"],
            "deadhead_km": 20.0,
            "home_assignment_rate": 0.5,
            "operator_statistics": {},
            "solution_file": "solution.json",
            "solution_sha256": "a" * 64,
            "wall_seconds": 300.0,
        }
        rows.append(row)
        solutions[str(tmp_path / row["cell_output_dir"] / row["solution_file"])] = {
            "score": {key: row[key] for key in ("late_count", "total_lateness_min", "distance_km")},
            "problem": {
                "task_count": 200,
                "drone_count": row["drone_count"],
                "max_tasks_per_drone": 25,
                "drone_homes": ["station_1" if row["station_predeployment"] else "origin"] * row["drone_count"],
            },
            "relay": {
                "relay_count": row["station_count"],
                "relay_task_count": row["relay_task_count"],
                "cross_uav_handoff_count": row["cross_uav_handoff_count"],
                "total_relay_waiting_time": row["total_relay_waiting_min"],
            },
            "metadata": {"operator_statistics": {}},
        }
    monkeypatch.setattr(manuscript, "_load_json", lambda path, _label: deepcopy(manifest if path.name == "manifest.json" else rows))
    monkeypatch.setattr(manuscript, "_solution", lambda path, *_args: deepcopy(solutions[str(path)]))
    monkeypatch.setattr(manuscript, "_validate_operator_statistics", lambda *_args: None)
    return tmp_path, manifest, rows, solutions


def test_manuscript_accepts_all_strategies_with_shared_baselines(formal_evidence):
    directory, _, _, _ = formal_evidence
    _, rows = manuscript._validate_sensitivity(directory)
    assert len(rows) == 55
    assert len({row["run_id"] for row in rows}) == 48
    assert {row["scenario"] for row in rows} == set(manuscript.SCENARIO_IDS)


def test_manuscript_rejects_missing_strategy_even_if_other_rows_are_valid(formal_evidence):
    directory, _, rows, _ = formal_evidence
    rows[:] = [row for row in rows if row["scenario"] != "direct_relay"]
    with pytest.raises(manuscript.ArtefactError, match="55条"):
        manuscript._validate_sensitivity(directory)


def test_manuscript_rejects_cross_strategy_baseline_reuse(formal_evidence):
    directory, _, rows, _ = formal_evidence
    reused = next(row for row in rows if row["scenario"] == "direct_relay" and row["reused_from"])
    reused["reused_from"] = rows[0]["cell_id"]
    with pytest.raises(manuscript.ArtefactError, match="同一次实际求解"):
        manuscript._validate_sensitivity(directory)


def test_manuscript_rejects_reused_baseline_with_different_runtime(formal_evidence):
    directory, _, rows, _ = formal_evidence
    next(row for row in rows if row["reused_from"])["wall_seconds"] += 1
    with pytest.raises(manuscript.ArtefactError, match="复用的解文件或结果不一致"):
        manuscript._validate_sensitivity(directory)


def test_manuscript_rejects_origin_strategy_with_predeployed_home(formal_evidence):
    directory, _, rows, solutions = formal_evidence
    row = next(row for row in rows if row["scenario"] == "direct_relay")
    solutions[str(directory / row["cell_output_dir"] / row["solution_file"])]["problem"]["drone_homes"][0] = "station_1"
    with pytest.raises(manuscript.ArtefactError, match="原点策略出现异地home"):
        manuscript._validate_sensitivity(directory)


def test_manuscript_rejects_station_axis_declared_for_pure_direct(formal_evidence):
    directory, manifest, _, _ = formal_evidence
    manifest["parameter_applicability"] = deepcopy(manifest["parameter_applicability"])
    manifest["parameter_applicability"]["pure_direct"].append("station_count")
    with pytest.raises(manuscript.ArtefactError, match="参数适用性"):
        manuscript._validate_sensitivity(directory)


def test_manuscript_rejects_mixed_strategy_trend_conclusion():
    rows = [{"scenario": "pure_direct"}, {"scenario": "direct_relay"}]
    with pytest.raises(ValueError, match="不得混合策略"):
        manuscript._sensitivity_finding("drone_count", rows)


def test_manuscript_describes_nonmonotonic_stations_and_unused_relay():
    rows = [
        {"scenario": "direct_relay", "level": level, "late_count": late,
         "total_lateness_min": 10.0, "distance_km": 50.0,
         "relay_task_count": relays, "cross_uav_handoff_count": relays}
        for level, late, relays in [(2, 3, 0), (3, 5, 1), (4, 2, 1)]
    ]
    text = manuscript._sensitivity_finding("station_count", rows)
    assert "并非单调改善" in text
    assert "2档未采用接力" in text
    assert "最优观测点为4" in text
