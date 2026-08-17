import hashlib
import json
from pathlib import Path

import matplotlib.pyplot as plt
import pytest

import algorithm.experiments.render_final_figures as final_figures
from algorithm.experiments.render_final_figures import (
    FINAL_FIGURE_STEMS,
    build_exact_validation_figure,
    build_scenario_comparison_figure,
    figure_manifest_on_success,
    operator_outcome_percentages,
    paired_scenario_rows,
    route_layout_from_solution,
    select_route_rows,
    summarize_operator_outcomes,
    unique_rows_by_numeric_key,
)


def test_operator_outcome_percentages_are_exclusive_and_sum_to_100():
    percentages = operator_outcome_percentages(
        {
            "uses": 20,
            "accepted": 12,
            "current_improvements": 7,
            "best_improvements": 2,
        }
    )

    assert percentages == pytest.approx((10.0, 25.0, 25.0, 40.0))
    assert sum(percentages) == pytest.approx(100.0)


def test_operator_outcomes_are_equal_weighted_by_run_and_report_exposure():
    rows = [
        {
            "operator_statistics": {
                "destroy:random": {
                    "uses": 100,
                    "accepted": 100,
                    "current_improvements": 100,
                    "best_improvements": 100,
                }
            }
        },
        {
            "operator_statistics": {
                "destroy:random": {
                    "uses": 10,
                    "accepted": 0,
                    "current_improvements": 0,
                    "best_improvements": 0,
                }
            }
        },
    ]

    summary = summarize_operator_outcomes(rows)["destroy:random"]

    assert summary.mean_percentages == pytest.approx((50.0, 0.0, 0.0, 50.0))
    assert summary.total_uses == pytest.approx(110.0)
    assert summary.run_count == 2


def test_numeric_series_rejects_duplicate_x_values_before_plotting():
    rows = [
        {"level": 1.0, "distance_km": 10.0},
        {"level": 1.0, "distance_km": 11.0},
    ]

    with pytest.raises(ValueError, match="duplicate level"):
        unique_rows_by_numeric_key(rows, "level", context="sensitivity")


def test_scenario_comparison_rejects_an_incomplete_paired_seed():
    rows = [
        {"seed": 7, "scenario": "pure_direct"},
        {"seed": 7, "scenario": "direct_relay"},
        {"seed": 7, "scenario": "direct_stations"},
    ]

    with pytest.raises(ValueError, match="seed=7.*direct_relay_stations"):
        paired_scenario_rows(rows)


def test_route_panels_use_one_predefined_seed_for_every_scenario():
    scenarios = (
        "pure_direct",
        "direct_relay",
        "direct_stations",
        "direct_relay_stations",
    )
    rows = [
        {
            "seed": seed,
            "scenario": scenario,
            "distance_km": 100.0 if seed == 11 else 1.0,
            "solution_file": f"{scenario}-{seed}.json",
        }
        for seed in (11, 12)
        for scenario in scenarios
    ]

    selected = select_route_rows(rows, route_seed=11)

    assert set(selected) == set(scenarios)
    assert {row["seed"] for row in selected.values()} == {11}


def test_route_layout_uses_payload_depot_homes_stations_and_service_points():
    solution = {
        "problem": {
            "depot_km": [5.0, 6.0],
            "drone_homes": ["origin", "station_3"],
            "open_routes": True,
        },
        "relay": {
            "relay_coordinates": [[3, 10.0, 11.0], [4, 20.0, 21.0]],
            "relay_usage_per_station": [
                {"station_id": 3, "task_count": 0},
                {"station_id": 4, "task_count": 2},
            ],
        },
        "routes": [
            {
                "visits": [
                    {"kind": "PICKUP", "location_km": [6.0, 7.0]},
                    {"kind": "DELIVERY", "location_km": [7.0, 8.0]},
                ]
            },
            {
                "visits": [
                    {"kind": "RELAY_PICKUP", "location_km": [20.0, 21.0]},
                    {"kind": "DELIVERY", "location_km": [22.0, 23.0]},
                ]
            },
        ],
    }

    layout = route_layout_from_solution(solution)

    assert layout.depot == (5.0, 6.0)
    assert layout.routes[0][0] == layout.depot
    assert layout.routes[1][0] == (10.0, 11.0)
    assert layout.active_station_ids == frozenset({3, 4})
    assert (20.0, 21.0) not in layout.service_points
    assert (22.0, 23.0) in layout.service_points


def test_exact_match_panel_shows_both_states_without_bar_grid():
    rows = [
        {
            "method": "Pareto-DP",
            "task_count": 3,
            "runtime_seconds": 0.1,
            "deadline_multiplier": 1.0,
        },
        {
            "method": "Pareto-DP",
            "task_count": 4,
            "runtime_seconds": 1.0,
            "deadline_multiplier": 1.0,
        },
        {
            "method": "C2-Lex-ALNS",
            "task_count": 3,
            "deadline_multiplier": 1.0,
            "matches_oracle": True,
        },
        {
            "method": "C2-Lex-ALNS",
            "task_count": 4,
            "deadline_multiplier": 1.0,
            "matches_oracle": False,
        },
    ]

    figure = build_exact_validation_figure(rows)
    try:
        match_axis = figure.axes[1]
        text_labels = {text.get_text() for text in match_axis.texts}
        assert {"Yes", "No"} <= text_labels
        assert all(
            not line.get_visible()
            for line in match_axis.get_xgridlines() + match_axis.get_ygridlines()
        )
        assert len(match_axis.collections) == 1
    finally:
        plt.close(figure)


def test_successful_figure_set_writes_auditable_manifest(tmp_path):
    inputs = {
        "benchmark_results": tmp_path / "benchmarks" / "results.json",
        "scenario_runs": tmp_path / "scenarios" / "runs.json",
        "sensitivity_runs": tmp_path / "sensitivity" / "runs.json",
    }
    for name, path in inputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"source": name}), encoding="utf-8")
    output_dir = tmp_path / "figures"
    output_dir.mkdir()

    with figure_manifest_on_success(
        output_dir=output_dir,
        input_paths=inputs,
        route_seed=2026081701,
    ):
        for stem in FINAL_FIGURE_STEMS:
            for suffix in (".png", ".svg"):
                (output_dir / f"{stem}{suffix}").write_bytes(
                    f"{stem}{suffix}".encode()
                )

    manifest = json.loads(
        (output_dir / "figure_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["route_seed"] == 2026081701
    assert manifest["figure_count"] == 9
    assert manifest["output_file_count"] == 18
    assert set(manifest["inputs"]) == set(inputs)
    for name, source_path in inputs.items():
        record = manifest["inputs"][name]
        assert record["absolute_path"] == str(source_path.resolve())
        assert not Path(record["relative_path"]).is_absolute()
        assert record["sha256"] == hashlib.sha256(
            source_path.read_bytes()
        ).hexdigest()
    assert len(manifest["outputs"]) == 18
    for record in manifest["outputs"]:
        output_path = output_dir / record["relative_path"]
        assert record["absolute_path"] == str(output_path.resolve())
        assert record["sha256"] == hashlib.sha256(
            output_path.read_bytes()
        ).hexdigest()


def test_incomplete_figure_set_leaves_no_manifest(tmp_path):
    source_path = tmp_path / "results.json"
    source_path.write_text("{}", encoding="utf-8")
    output_dir = tmp_path / "figures"

    with pytest.raises(FileNotFoundError, match="missing final figure outputs"):
        with figure_manifest_on_success(
            output_dir=output_dir,
            input_paths={"benchmark_results": source_path},
            route_seed=7,
        ):
            expected_outputs = [
                output_dir / f"{stem}{suffix}"
                for stem in FINAL_FIGURE_STEMS
                for suffix in (".png", ".svg")
            ]
            for path in expected_outputs[:-1]:
                path.write_bytes(b"partial")

    assert not (output_dir / "figure_manifest.json").exists()


def test_main_writes_manifest_after_all_renderers_complete(
    tmp_path,
    monkeypatch,
):
    benchmark_dir = tmp_path / "benchmarks"
    scenario_dir = tmp_path / "scenarios"
    sensitivity_dir = tmp_path / "sensitivity"
    benchmark_dir.mkdir()
    scenario_dir.mkdir()
    sensitivity_dir.mkdir()
    (benchmark_dir / "results.json").write_text(
        json.dumps({"runs": []}),
        encoding="utf-8",
    )
    (scenario_dir / "runs.json").write_text("[]", encoding="utf-8")
    (sensitivity_dir / "runs.json").write_text("[]", encoding="utf-8")
    output_dir = tmp_path / "figures"

    def write_pair(stem, target):
        for suffix in (".png", ".svg"):
            (target / f"{stem}{suffix}").write_bytes(stem.encode())

    monkeypatch.setattr(
        final_figures,
        "plot_title_example",
        lambda target: write_pair("fig5_1_title_example", target),
    )
    monkeypatch.setattr(
        final_figures,
        "plot_exact_validation",
        lambda rows, target: write_pair("fig5_2_exact_validation", target),
    )
    monkeypatch.setattr(
        final_figures,
        "plot_multiscale",
        lambda rows, target: write_pair("fig5_3_relaxed_multiscale", target),
    )
    monkeypatch.setattr(
        final_figures,
        "plot_operator_effectiveness",
        lambda benchmarks, scenarios, target: write_pair(
            "fig5_4_operator_effectiveness",
            target,
        ),
    )
    monkeypatch.setattr(
        final_figures,
        "plot_scenario_comparison",
        lambda rows, target: write_pair("fig6_1_scenario_comparison", target),
    )
    monkeypatch.setattr(
        final_figures,
        "plot_route_layouts",
        lambda rows, scenarios, target, route_seed: write_pair(
            "fig6_2_route_layouts",
            target,
        ),
    )

    def write_sensitivity(rows, target):
        for parameter in (
            "drone_count",
            "deadline_multiplier",
            "station_count",
        ):
            write_pair(f"fig6_3_{parameter}_sensitivity", target)

    monkeypatch.setattr(
        final_figures,
        "plot_sensitivity",
        write_sensitivity,
    )

    result = final_figures.main(
        [
            "--benchmarks",
            str(benchmark_dir),
            "--scenarios",
            str(scenario_dir),
            "--sensitivity",
            str(sensitivity_dir),
            "--output-dir",
            str(output_dir),
            "--route-seed",
            "41",
        ]
    )

    assert result == 0
    manifest = json.loads(
        (output_dir / "figure_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["route_seed"] == 41
    assert len(manifest["outputs"]) == 18


def test_scenario_figure_exposes_pairing_sample_size_spread_and_budgets():
    scenarios = (
        "pure_direct",
        "direct_relay",
        "direct_stations",
        "direct_relay_stations",
    )
    rows = []
    for seed in (1, 2, 3):
        for scenario_index, scenario in enumerate(scenarios):
            rows.append(
                {
                    "seed": seed,
                    "scenario": scenario,
                    "effective_time_limit_seconds": (
                        240.0 if scenario == "pure_direct" else 300.0
                    ),
                    "late_count": seed + scenario_index,
                    "total_lateness_min": 10.0 * seed + scenario_index,
                    "distance_km": 100.0 * seed + scenario_index,
                }
            )

    figure = build_scenario_comparison_figure(rows)
    try:
        title = figure.get_suptitle()
        assert "n=3" in title
        assert "mean ± SD" in title
        assert "D = 240 s" in title
        assert "D+R/D+S/D+R+S = 300 s" in title
        for axis in figure.axes:
            paired_lines = [
                line
                for line in axis.lines
                if tuple(line.get_xdata()) == (0, 1, 2, 3)
                and line.get_linestyle() == "-"
            ]
            assert len(paired_lines) == 3
    finally:
        plt.close(figure)
