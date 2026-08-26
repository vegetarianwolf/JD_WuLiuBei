import hashlib
import json
from pathlib import Path

import matplotlib.pyplot as plt
import pytest

import algorithm.experiments.render_final_figures as final_figures
from algorithm.experiments.render_final_figures import (
    FINAL_FIGURE_STEMS,
    build_exact_validation_figure,
    build_route_layouts_figure,
    build_scenario_comparison_figure,
    build_sensitivity_figure,
    figure_manifest_on_success,
    operator_outcome_percentages,
    paired_scenario_rows,
    route_layout_from_solution,
    select_route_rows,
    summarize_operator_outcomes,
    unique_rows_by_numeric_key,
)


FORMAL_SCENARIO_SEEDS = (2026081701, 2026081702, 2026081703)
FORMAL_SCENARIOS = (
    "pure_direct",
    "direct_relay",
    "direct_stations",
    "direct_relay_stations",
)
FORMAL_SCENARIO_BUDGETS = {
    "pure_direct": 240.0,
    "direct_relay": 480.0,
    "direct_stations": 480.0,
    "direct_relay_stations": 720.0,
}


def _formal_scenario_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for seed_index, seed in enumerate(FORMAL_SCENARIO_SEEDS):
        for scenario_index, scenario in enumerate(FORMAL_SCENARIOS):
            rows.append(
                {
                    "seed": seed,
                    "scenario": scenario,
                    "effective_time_limit_seconds": FORMAL_SCENARIO_BUDGETS[
                        scenario
                    ],
                    "wall_seconds": FORMAL_SCENARIO_BUDGETS[scenario]
                    - 0.05
                    + 0.001 * seed_index,
                    "late_count": 60 + seed_index - scenario_index,
                    "total_lateness_min": (
                        2000.0 + 10.0 * seed_index - scenario_index
                    ),
                    "distance_km": (
                        620.0 + 2.0 * seed_index - scenario_index
                    ),
                    "relay_task_count": (
                        scenario_index
                        if scenario in {"direct_relay", "direct_relay_stations"}
                        else 0
                    ),
                    "solution_file": f"run_solutions/{scenario}__seed_{seed}.json",
                }
            )
    return rows


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
        {
            "seed": FORMAL_SCENARIO_SEEDS[0],
            "scenario": scenario,
            "effective_time_limit_seconds": FORMAL_SCENARIO_BUDGETS[scenario],
        }
        for scenario in FORMAL_SCENARIOS[:-1]
    ]

    with pytest.raises(
        ValueError,
        match=f"seed={FORMAL_SCENARIO_SEEDS[0]}.*direct_relay_stations",
    ):
        paired_scenario_rows(rows)


def test_scenario_comparison_requires_the_three_predeclared_paired_seeds():
    rows = _formal_scenario_rows()
    rows = [row for row in rows if row["seed"] != FORMAL_SCENARIO_SEEDS[-1]]

    with pytest.raises(ValueError, match="paired seeds.*2026081701.*2026081703"):
        paired_scenario_rows(rows)


def test_scenario_comparison_rejects_a_nonformal_budget():
    rows = _formal_scenario_rows()
    rows[1]["effective_time_limit_seconds"] = 300.0

    with pytest.raises(ValueError, match="direct_relay.*480.*300"):
        paired_scenario_rows(rows)


def test_route_panels_use_one_predefined_seed_for_every_scenario():
    rows = _formal_scenario_rows()

    selected = select_route_rows(rows, route_seed=FORMAL_SCENARIO_SEEDS[0])

    assert set(selected) == set(FORMAL_SCENARIOS)
    assert {row["seed"] for row in selected.values()} == {
        FORMAL_SCENARIO_SEEDS[0]
    }


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


def test_route_figure_titles_expose_protocol_score_and_relay_use(tmp_path):
    rows = _formal_scenario_rows()
    selected_seed = FORMAL_SCENARIO_SEEDS[0]
    solution = {
        "problem": {
            "depot_km": [0.0, 0.0],
            "drone_homes": ["origin"],
            "open_routes": True,
        },
        "relay": {
            "relay_coordinates": [[1, 1.0, 1.0]],
            "relay_usage_per_station": [],
        },
        "routes": [
            {
                "visits": [
                    {"kind": "PICKUP", "location_km": [1.0, 0.0]},
                    {"kind": "DELIVERY", "location_km": [1.0, 1.0]},
                ]
            }
        ],
    }
    for row in rows:
        if row["seed"] != selected_seed:
            continue
        solution_path = tmp_path / str(row["solution_file"])
        solution_path.parent.mkdir(parents=True, exist_ok=True)
        solution_path.write_text(json.dumps(solution), encoding="utf-8")

    figure = build_route_layouts_figure(
        rows,
        tmp_path,
        route_seed=selected_seed,
    )
    try:
        titles = [axis.get_title() for axis in figure.axes]
        assert "D · 240 s" in titles[0]
        assert "D+R · 240+240 s" in titles[1]
        assert "D+P · 240+240 s" in titles[2]
        assert "D+P+R · 240+240+240 s" in titles[3]
        assert all("Lex score = (" in title for title in titles)
        assert all("Relay tasks =" in title for title in titles)
        assert "n=1 illustrative paired seed" in figure.get_suptitle()
        assert "P = demand-driven position/predeployment" in figure.get_suptitle()
    finally:
        plt.close(figure)


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
    scenario_rows = _formal_scenario_rows()
    (scenario_dir / "runs.json").write_text(
        json.dumps(scenario_rows),
        encoding="utf-8",
    )
    (sensitivity_dir / "runs.json").write_text("[]", encoding="utf-8")
    (scenario_dir / "manifest.json").write_text(
        json.dumps({"protocol": "formal"}),
        encoding="utf-8",
    )
    (sensitivity_dir / "manifest.json").write_text(
        json.dumps({"protocol": "ofat"}),
        encoding="utf-8",
    )
    for row in scenario_rows:
        if row["seed"] != FORMAL_SCENARIO_SEEDS[0]:
            continue
        solution_path = scenario_dir / str(row["solution_file"])
        solution_path.parent.mkdir(parents=True, exist_ok=True)
        solution_path.write_text(
            json.dumps({"scenario": row["scenario"]}),
            encoding="utf-8",
        )
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
            str(FORMAL_SCENARIO_SEEDS[0]),
        ]
    )

    assert result == 0
    manifest = json.loads(
        (output_dir / "figure_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["route_seed"] == FORMAL_SCENARIO_SEEDS[0]
    assert len(manifest["outputs"]) == 18
    assert {
        "scenario_manifest",
        "sensitivity_manifest",
        "route_solution_pure_direct",
        "route_solution_direct_relay",
        "route_solution_direct_stations",
        "route_solution_direct_relay_stations",
    } <= set(manifest["inputs"])


def test_scenario_figure_exposes_pairing_sample_size_spread_and_budgets():
    rows = _formal_scenario_rows()

    figure = build_scenario_comparison_figure(rows)
    try:
        title = figure.get_suptitle()
        assert "n=3" in title
        assert "mean ± SD" in title
        assert "unequal cumulative budgets" in title.lower()
        assert len(figure.axes) == 4
        assert [axis.get_title() for axis in figure.axes] == [
            "Priority 1",
            "Priority 2 (conditional)",
            "Priority 3 (conditional)",
            "Observed wall time",
        ]
        for axis in figure.axes:
            assert [tick.get_text() for tick in axis.get_xticklabels()] == [
                "D\n240 s",
                "D+R\n240+240 s",
                "D+P\n240+240 s",
                "D+P+R\n240+240+240 s",
            ]
            assert not axis.patches
            paired_lines = [
                line
                for line in axis.lines
                if tuple(line.get_xdata()) == (0, 1, 2, 3)
                and line.get_linestyle() == "-"
                and line.get_marker() == "None"
            ]
            assert len(paired_lines) == 3
            assert any(line.get_marker() == "D" for line in axis.lines)
    finally:
        plt.close(figure)


def _sensitivity_rows(parameter: str) -> list[dict[str, object]]:
    levels = {
        "drone_count": (8, 9, 10, 12, 16),
        "deadline_multiplier": (0.8, 0.9, 1.0, 1.1, 1.2),
        "station_count": (2, 3, 4, 5, 6),
    }[parameter]
    return [
        {
            "parameter": parameter,
            "level": level,
            "seed": FORMAL_SCENARIO_SEEDS[0],
            "scenario": "direct_relay_stations",
            "effective_time_limit_seconds": 300.0,
            "late_count": index,
            "total_lateness_min": 100.0 + index,
            "distance_km": 600.0 + index,
        }
        for index, level in enumerate(levels)
    ]


@pytest.mark.parametrize(
    ("parameter", "baseline"),
    (("drone_count", 8.0), ("deadline_multiplier", 1.0), ("station_count", 4.0)),
)
def test_sensitivity_figure_is_explicitly_single_seed_exploratory(
    parameter,
    baseline,
):
    figure = build_sensitivity_figure(_sensitivity_rows(parameter), parameter)
    try:
        title = figure.get_suptitle()
        assert "OFAT exploratory" in title
        assert "n=1" in title
        assert "no error bars" in title
        assert "fixed 300 s total budget" in title
        assert [axis.get_title() for axis in figure.axes] == [
            "Priority 1",
            "Priority 2 (conditional)",
            "Priority 3 (conditional)",
        ]
        assert [
            text.get_text() for text in figure.axes[0].get_legend().get_texts()
        ] == ["Baseline level"]
        for axis in figure.axes:
            baseline_lines = [
                line
                for line in axis.lines
                if line.get_linestyle() == "--"
                and tuple(line.get_xdata()) == (baseline, baseline)
            ]
            assert len(baseline_lines) == 1
    finally:
        plt.close(figure)


def test_sensitivity_figure_rejects_a_budget_other_than_fixed_300_seconds():
    rows = _sensitivity_rows("drone_count")
    rows[0]["effective_time_limit_seconds"] = 720.0

    with pytest.raises(ValueError, match="fixed 300 s.*720"):
        build_sensitivity_figure(rows, "drone_count")
