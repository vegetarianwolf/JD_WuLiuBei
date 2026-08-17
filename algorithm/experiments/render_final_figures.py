"""Render publication figures from the fresh final experiment artefacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import matplotlib.pyplot as plt
import numpy as np

from algorithm.experiments.final_plot_style import (
    FOUR_PANEL_FIGSIZE,
    METHOD_LINE_PALETTE,
    MODEL_FILL_PALETTE,
    OPERATOR_PALETTE,
    TWO_PANEL_FIGSIZE,
    reference_plot_style,
    save_publication_figure,
)


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BENCHMARKS = ROOT / "algorithm" / "results" / "final_benchmarks"
DEFAULT_SCENARIOS = (
    ROOT / "algorithm" / "results" / "final_scenario_comparison"
)
DEFAULT_SENSITIVITY = ROOT / "algorithm" / "results" / "final_sensitivity"
DEFAULT_OUTPUT = ROOT / "writing" / "figures" / "final"
DEFAULT_ROUTE_SEED = 2026081701
FIGURE_MANIFEST_NAME = "figure_manifest.json"
FINAL_FIGURE_STEMS = (
    "fig5_1_title_example",
    "fig5_2_exact_validation",
    "fig5_3_relaxed_multiscale",
    "fig5_4_operator_effectiveness",
    "fig6_1_scenario_comparison",
    "fig6_2_route_layouts",
    "fig6_3_drone_count_sensitivity",
    "fig6_3_deadline_multiplier_sensitivity",
    "fig6_3_station_count_sensitivity",
)
_FINAL_FIGURE_SUFFIXES = (".png", ".svg")

SCENARIO_LABELS = {
    "pure_direct": "D",
    "direct_relay": "D+R",
    "direct_stations": "D+S",
    "direct_relay_stations": "D+R+S",
}
METRIC_LINE_STYLES = {
    "late_count": (METHOD_LINE_PALETTE[0], "o"),
    "total_lateness_min": (METHOD_LINE_PALETTE[1], "s"),
    "distance_km": (METHOD_LINE_PALETTE[2], "^"),
    "runtime_seconds": ("#7F7F7F", "D"),
}


@dataclass(frozen=True, slots=True)
class OperatorOutcomeSummary:
    """Equal-run outcome summary plus its underlying exposure."""

    mean_percentages: tuple[float, float, float, float]
    total_uses: float
    run_count: int


@dataclass(frozen=True, slots=True)
class RouteLayoutData:
    """Self-contained geometry extracted from one solution payload."""

    depot: tuple[float, float]
    stations: tuple[tuple[int, tuple[float, float]], ...]
    active_station_ids: frozenset[int]
    service_points: tuple[tuple[float, float], ...]
    routes: tuple[tuple[tuple[float, float], ...], ...]


def operator_outcome_percentages(
    statistics: dict[str, int | float],
) -> tuple[float, float, float, float]:
    """Return exclusive best/current/accepted/rejected percentages."""

    uses = float(statistics.get("uses", 0))
    if uses <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    accepted = float(statistics.get("accepted", 0))
    current = float(statistics.get("current_improvements", 0))
    best = float(statistics.get("best_improvements", 0))
    counts = (best, current - best, accepted - current, uses - accepted)
    if any(value < -1e-9 for value in counts):
        raise ValueError("算子统计不满足 best≤current≤accepted≤uses")
    return tuple(100.0 * max(0.0, value) / uses for value in counts)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_path(path: Path, base: Path) -> str:
    return Path(os.path.relpath(path, base)).as_posix()


def _file_audit_record(path: Path, *, relative_to: Path) -> dict[str, str]:
    resolved = path.resolve(strict=True)
    return {
        "relative_path": _relative_path(resolved, relative_to),
        "absolute_path": str(resolved),
        "sha256": _sha256(resolved),
    }


@contextmanager
def figure_manifest_on_success(
    *,
    output_dir: Path,
    input_paths: Mapping[str, Path],
    route_seed: int,
) -> Iterator[None]:
    """Write an input/output audit manifest after every final figure exists."""

    resolved_output = output_dir.resolve()
    resolved_output.mkdir(parents=True, exist_ok=True)
    manifest_path = resolved_output / FIGURE_MANIFEST_NAME
    manifest_path.unlink(missing_ok=True)
    input_records = {
        name: _file_audit_record(Path(path), relative_to=ROOT)
        for name, path in sorted(input_paths.items())
    }

    yield

    expected_outputs = tuple(
        resolved_output / f"{stem}{suffix}"
        for stem in FINAL_FIGURE_STEMS
        for suffix in _FINAL_FIGURE_SUFFIXES
    )
    missing = [path.name for path in expected_outputs if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "missing final figure outputs: " + ", ".join(missing)
        )
    output_records = [
        _file_audit_record(path, relative_to=resolved_output)
        for path in expected_outputs
    ]
    manifest = {
        "schema_version": 1,
        "repository_root": str(ROOT.resolve()),
        "route_seed": int(route_seed),
        "figure_count": len(FINAL_FIGURE_STEMS),
        "output_file_count": len(output_records),
        "inputs": input_records,
        "outputs": output_records,
    }
    temporary_path = resolved_output / f".{FIGURE_MANIFEST_NAME}.tmp"
    temporary_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(manifest_path)


def unique_rows_by_numeric_key(
    rows: Sequence[dict[str, Any]],
    key: str,
    *,
    context: str,
) -> tuple[dict[str, Any], ...]:
    """Validate a one-series protocol and return rows sorted by numeric x."""

    by_value: dict[float, dict[str, Any]] = {}
    for row in rows:
        value = float(row[key])
        if value in by_value:
            raise ValueError(f"{context}: duplicate {key}={value:g}")
        by_value[value] = row
    return tuple(by_value[value] for value in sorted(by_value))


def _save_pair(figure, output_dir: Path, stem: str) -> None:
    save_publication_figure(figure, output_dir / f"{stem}.png")
    save_publication_figure(figure, output_dir / f"{stem}.svg", close=True)


def _panel_label(axis, label: str, *, y: float = -0.21) -> None:
    axis.text(
        0.02,
        y,
        label,
        transform=axis.transAxes,
        ha="left",
        va="top",
        fontsize=8,
        fontweight="bold",
    )


def plot_title_example(output_dir: Path) -> None:
    nodes = {
        "0": (0.0, 0.9),
        "P1": (1.0, 1.7),
        "P2": (1.0, 0.1),
        "D2": (2.1, 0.25),
        "D1": (2.1, 1.55),
    }
    edges = {
        ("0", "P1"): 5,
        ("0", "P2"): 8,
        ("P1", "P2"): 1,
        ("P1", "D1"): 4,
        ("P2", "D2"): 2,
        ("D1", "D2"): 3,
        ("P1", "D2"): 5,
        ("P2", "D1"): 4,
    }
    optimal = (("0", "P1"), ("P1", "P2"), ("P2", "D2"), ("D2", "D1"))
    edge_label_offsets = {
        ("0", "P1"): (-0.04, 0.09),
        ("0", "P2"): (-0.03, -0.07),
        ("P1", "P2"): (0.08, 0.00),
        ("P1", "D1"): (0.00, 0.08),
        ("P2", "D2"): (0.00, -0.08),
        ("D1", "D2"): (0.08, 0.00),
        ("P1", "D2"): (-0.08, 0.05),
        ("P2", "D1"): (0.08, -0.05),
    }
    node_label_offsets = {
        "0": (0.00, 0.16, "center"),
        "P1": (0.00, 0.16, "center"),
        "P2": (-0.12, 0.04, "right"),
        "D2": (0.11, 0.05, "left"),
        "D1": (0.00, 0.16, "center"),
    }
    with reference_plot_style():
        figure, axis = plt.subplots(figsize=(5.2, 3.2))
        for left, right in edges:
            x = [nodes[left][0], nodes[right][0]]
            y = [nodes[left][1], nodes[right][1]]
            axis.plot(x, y, color="#C8CDD5", linewidth=0.75, zorder=1)
        for left, right in optimal:
            axis.annotate(
                "",
                xy=nodes[right],
                xytext=nodes[left],
                arrowprops={
                    "arrowstyle": "-|>",
                    "color": OPERATOR_PALETTE[0],
                    "linewidth": 1.8,
                    "shrinkA": 10,
                    "shrinkB": 10,
                },
                zorder=3,
            )
        for (left, right), distance in edges.items():
            offset_x, offset_y = edge_label_offsets[(left, right)]
            axis.text(
                (nodes[left][0] + nodes[right][0]) / 2 + offset_x,
                (nodes[left][1] + nodes[right][1]) / 2 + offset_y,
                str(distance),
                color="#555555",
                fontsize=7,
                ha="center",
                va="center",
                zorder=5,
                bbox={
                    "facecolor": "white",
                    "edgecolor": "none",
                    "alpha": 0.92,
                    "pad": 0.45,
                },
            )
        for name, (x, y) in nodes.items():
            color = "#5D77B0" if name == "0" else (
                "#F4B674" if name.startswith("P") else "#6FC285"
            )
            marker = "s" if name == "0" else ("o" if name.startswith("P") else "^")
            axis.scatter(
                x,
                y,
                s=88,
                c=color,
                marker=marker,
                edgecolors="white",
                linewidths=0.8,
                zorder=4,
            )
            offset_x, offset_y, horizontal_alignment = node_label_offsets[name]
            axis.text(
                x + offset_x,
                y + offset_y,
                name,
                ha=horizontal_alignment,
                va="center",
                fontsize=8,
                zorder=6,
                bbox={
                    "facecolor": "white",
                    "edgecolor": "none",
                    "alpha": 0.88,
                    "pad": 0.25,
                },
            )
        axis.text(
            0.5,
            0.02,
            r"Optimal sequence: 0$\rightarrow$P1$\rightarrow$P2$\rightarrow$D2"
            r"$\rightarrow$D1; 11 km",
            transform=axis.transAxes,
            ha="center",
            fontsize=8,
        )
        axis.set_xlim(-0.25, 2.35)
        axis.set_ylim(-0.2, 2.05)
        axis.set_aspect("equal")
        axis.axis("off")
        figure.tight_layout()
        _save_pair(figure, output_dir, "fig5_1_title_example")


def build_exact_validation_figure(
    rows: Sequence[dict[str, Any]],
) -> plt.Figure:
    """Build the exact-runtime and categorical oracle-match figure."""

    exact = sorted(
        (
            row
            for row in rows
            if row["method"] == "Pareto-DP" and row["task_count"] > 2
        ),
        key=lambda row: row["task_count"],
    )
    alns = {
        row["task_count"]: row
        for row in rows
        if row["method"] == "C2-Lex-ALNS"
        and row["deadline_multiplier"] == 1.0
    }
    sizes = [row["task_count"] for row in exact]
    missing = [size for size in sizes if size not in alns]
    if missing:
        raise ValueError(f"missing C2-Lex-ALNS exact comparisons for sizes {missing}")
    if any(float(row["runtime_seconds"]) <= 0 for row in exact):
        raise ValueError("exact runtime must be positive for the logarithmic axis")

    with reference_plot_style({"axes.grid": True}):
        figure, axes = plt.subplots(1, 2, figsize=TWO_PANEL_FIGSIZE)
        axes[0].plot(
            sizes,
            [row["runtime_seconds"] for row in exact],
            color=METHOD_LINE_PALETTE[0],
            marker="o",
        )
        axes[0].set_yscale("log")
        axes[0].set_xlabel("Number of tasks")
        axes[0].set_ylabel("Exact runtime (s, log scale)")
        axes[0].set_xticks(sizes)
        matches = [bool(alns[size]["matches_oracle"]) for size in sizes]
        axes[1].scatter(
            sizes,
            np.zeros(len(sizes)),
            marker="s",
            s=155,
            color=[
                OPERATOR_PALETTE[0] if matched else OPERATOR_PALETTE[4]
                for matched in matches
            ],
            edgecolor="white",
            linewidth=0.55,
        )
        for size, matched in zip(sizes, matches, strict=True):
            axes[1].text(
                size,
                0,
                "Yes" if matched else "No",
                color="white",
                ha="center",
                va="center",
                fontsize=6.8,
                fontweight="bold",
            )
        axes[1].set_ylim(-0.55, 0.55)
        axes[1].set_yticks(())
        axes[1].set_xticks(sizes)
        axes[1].set_xlabel("Number of tasks")
        axes[1].set_title("ALNS matches exact score")
        axes[1].grid(False)
        for axis, label in zip(axes, ("(a)", "(b)"), strict=True):
            _panel_label(axis, label)
        figure.subplots_adjust(bottom=0.24, wspace=0.34)
    return figure


def plot_exact_validation(rows: Sequence[dict[str, Any]], output_dir: Path) -> None:
    figure = build_exact_validation_figure(rows)
    with reference_plot_style():
        _save_pair(figure, output_dir, "fig5_2_exact_validation")


def plot_multiscale(rows: Sequence[dict[str, Any]], output_dir: Path) -> None:
    scale = unique_rows_by_numeric_key(
        tuple(
            row
            for row in rows
            if row["method"] == "C2-Lex-ALNS"
            and row["deadline_multiplier"] > 1.0
        ),
        "task_count",
        context="relaxed multiscale",
    )
    x = [row["task_count"] for row in scale]
    metrics = (
        ("late_count", "Late deliveries"),
        ("distance_km", "Total distance (km)"),
        ("runtime_seconds", "Search runtime (s)"),
    )
    with reference_plot_style({"axes.grid": True}):
        figure, axes = plt.subplots(1, 3, figsize=(7.1, 2.75))
        for axis, (field, ylabel), label in zip(
            axes,
            metrics,
            ("(a)", "(b)", "(c)"),
            strict=True,
        ):
            color, marker = METRIC_LINE_STYLES[field]
            values = [row[field] for row in scale]
            axis.plot(x, values, color=color, marker=marker)
            axis.set_xlabel("Number of tasks")
            axis.set_ylabel(ylabel)
            axis.set_xticks(x)
            if field == "late_count" and all(
                float(value) == 0.0 for value in values
            ):
                axis.set_ylim(-0.05, 0.5)
                axis.set_yticks([0])
                axis.text(
                    0.5,
                    0.72,
                    "All four scales: 0",
                    transform=axis.transAxes,
                    ha="center",
                    va="center",
                    color=color,
                )
            _panel_label(axis, label)
        figure.subplots_adjust(bottom=0.26, wspace=0.42)
        _save_pair(figure, output_dir, "fig5_3_relaxed_multiscale")


def summarize_operator_outcomes(
    rows: Sequence[dict[str, Any]],
) -> dict[str, OperatorOutcomeSummary]:
    """Average each operator's percentages with equal weight per observed run."""

    percentages_by_operator: dict[str, list[tuple[float, float, float, float]]] = {}
    uses_by_operator: dict[str, float] = {}
    for row in rows:
        for operator, statistics in row.get("operator_statistics", {}).items():
            uses = float(statistics.get("uses", 0))
            if uses <= 0:
                continue
            percentages_by_operator.setdefault(operator, []).append(
                operator_outcome_percentages(statistics)
            )
            uses_by_operator[operator] = uses_by_operator.get(operator, 0.0) + uses

    summaries: dict[str, OperatorOutcomeSummary] = {}
    for operator, percentages in percentages_by_operator.items():
        run_count = len(percentages)
        means = tuple(
            sum(run[index] for run in percentages) / run_count
            for index in range(4)
        )
        summaries[operator] = OperatorOutcomeSummary(
            mean_percentages=means,
            total_uses=uses_by_operator[operator],
            run_count=run_count,
        )
    return summaries


def plot_operator_effectiveness(
    benchmark_rows: Sequence[dict[str, Any]],
    scenario_rows: Sequence[dict[str, Any]],
    output_dir: Path,
) -> None:
    summaries = summarize_operator_outcomes(
        tuple(benchmark_rows) + tuple(scenario_rows)
    )
    groups = (
        (
            "Destroy operators",
            sorted(name for name in summaries if name.startswith("destroy:")),
        ),
        (
            "Repair operators",
            sorted(name for name in summaries if name.startswith("repair:")),
        ),
    )
    segment_labels = (
        "New global best",
        "Current-only improvement",
        "Accepted, no improvement",
        "Rejected",
    )
    colors = (
        OPERATOR_PALETTE[0],
        OPERATOR_PALETTE[1],
        OPERATOR_PALETTE[2],
        OPERATOR_PALETTE[4],
    )
    with reference_plot_style():
        figure, axes = plt.subplots(1, 2, figsize=(7.1, 4.2))
        for axis, (title, names), panel in zip(
            axes,
            groups,
            ("(a)", "(b)"),
            strict=True,
        ):
            labels = [name.split(":", 1)[1] for name in names]
            y = np.arange(len(names))
            left = np.zeros(len(names))
            for segment_index, (segment_label, color) in enumerate(
                zip(segment_labels, colors, strict=True)
            ):
                values = [
                    summaries[name].mean_percentages[segment_index]
                    for name in names
                ]
                axis.barh(
                    y,
                    values,
                    left=left,
                    height=0.72,
                    color=color,
                    label=segment_label,
                )
                left += np.asarray(values)
            for position, name in zip(y, names, strict=True):
                summary = summaries[name]
                axis.text(
                    99.2,
                    position,
                    f"n={summary.run_count}; uses={summary.total_uses:g}",
                    ha="right",
                    va="center",
                    fontsize=6,
                    bbox={
                        "facecolor": "white",
                        "edgecolor": "none",
                        "alpha": 0.76,
                        "pad": 0.5,
                    },
                )
            axis.set_yticks(y, labels)
            axis.invert_yaxis()
            axis.set_xlim(0, 100)
            axis.set_xlabel("Outcome among uses (%)")
            axis.set_title(title)
            _panel_label(axis, panel)
        handles, labels = axes[0].get_legend_handles_labels()
        figure.legend(
            handles,
            labels,
            loc="upper center",
            ncol=2,
            bbox_to_anchor=(0.5, 1.01),
        )
        figure.subplots_adjust(
            top=0.82,
            bottom=0.16,
            left=0.17,
            right=0.98,
            wspace=0.45,
        )
        _save_pair(figure, output_dir, "fig5_4_operator_effectiveness")


def paired_scenario_rows(
    rows: Sequence[dict[str, Any]],
) -> dict[int, dict[str, dict[str, Any]]]:
    """Return complete four-scenario cells keyed by their paired seed."""

    paired: dict[int, dict[str, dict[str, Any]]] = {}
    expected = set(SCENARIO_LABELS)
    for row in rows:
        scenario = str(row["scenario"])
        if scenario not in expected:
            continue
        seed = int(row["seed"])
        cells = paired.setdefault(seed, {})
        if scenario in cells:
            raise ValueError(f"duplicate scenario cell: seed={seed}, {scenario}")
        cells[scenario] = row

    for seed, cells in paired.items():
        missing = sorted(expected - set(cells))
        if missing:
            raise ValueError(
                f"incomplete paired scenarios for seed={seed}: "
                f"missing {', '.join(missing)}"
            )
    if not paired:
        raise ValueError("no paired scenario rows available")
    return {seed: paired[seed] for seed in sorted(paired)}


def _scenario_budget_note(
    paired: dict[int, dict[str, dict[str, Any]]],
) -> str:
    budgets: dict[str, set[float]] = {scenario: set() for scenario in SCENARIO_LABELS}
    for cells in paired.values():
        for scenario, row in cells.items():
            budgets[scenario].add(float(row["effective_time_limit_seconds"]))
    if any(len(values) != 1 for values in budgets.values()):
        raise ValueError("scenario budgets must be constant across paired seeds")
    resolved = {scenario: next(iter(values)) for scenario, values in budgets.items()}
    assisted = (
        "direct_relay",
        "direct_stations",
        "direct_relay_stations",
    )
    assisted_budgets = {resolved[scenario] for scenario in assisted}
    if len(assisted_budgets) == 1:
        return (
            f"Budgets: D = {resolved['pure_direct']:g} s; "
            "D+R/D+S/D+R+S = "
            f"{next(iter(assisted_budgets)):g} s"
        )
    return "Budgets: " + "; ".join(
        f"{SCENARIO_LABELS[scenario]} = {resolved[scenario]:g} s"
        for scenario in SCENARIO_LABELS
    )


def build_scenario_comparison_figure(
    rows: Sequence[dict[str, Any]],
) -> plt.Figure:
    """Build the paired-seed scenario comparison with descriptive spread."""

    paired = paired_scenario_rows(rows)
    seeds = tuple(paired)
    ids = list(SCENARIO_LABELS)
    fields = (
        ("late_count", "Late deliveries"),
        ("total_lateness_min", "Total lateness (min)"),
        ("distance_km", "Total distance (km)"),
    )
    with reference_plot_style():
        figure, axes = plt.subplots(1, 3, figsize=(7.1, 3.25))
        for axis, (field, ylabel), panel in zip(
            axes,
            fields,
            ("(a)", "(b)", "(c)"),
            strict=True,
        ):
            values = [
                [float(paired[seed][scenario][field]) for seed in seeds]
                for scenario in ids
            ]
            means = [float(np.mean(group)) for group in values]
            errors = [
                float(np.std(group, ddof=1)) if len(group) > 1 else 0.0
                for group in values
            ]
            x = np.arange(len(ids))
            axis.bar(
                x,
                means,
                yerr=errors,
                capsize=2,
                color=MODEL_FILL_PALETTE,
                width=0.72,
                zorder=1,
            )
            for seed in seeds:
                paired_values = [
                    float(paired[seed][scenario][field])
                    for scenario in ids
                ]
                axis.plot(
                    x,
                    paired_values,
                    color="#555555",
                    linewidth=0.55,
                    alpha=0.55,
                    zorder=2,
                )
                axis.scatter(
                    x,
                    paired_values,
                    s=11,
                    facecolor="white",
                    edgecolor="#3F3F3F",
                    linewidth=0.45,
                    zorder=3,
                )
            axis.set_xticks(x, [SCENARIO_LABELS[sid] for sid in ids])
            axis.set_ylabel(ylabel)
            _panel_label(axis, panel)
        figure.suptitle(
            f"Paired seeds (n={len(seeds)}); bars = mean ± SD\n"
            f"{_scenario_budget_note(paired)}; objectives read (a)→(b)→(c) "
            "lexicographically",
            fontsize=8.2,
            y=0.995,
        )
        figure.subplots_adjust(top=0.75, bottom=0.23, wspace=0.42)
    return figure


def plot_scenario_comparison(rows: Sequence[dict[str, Any]], output_dir: Path) -> None:
    figure = build_scenario_comparison_figure(rows)
    with reference_plot_style():
        _save_pair(figure, output_dir, "fig6_1_scenario_comparison")


def select_route_rows(
    rows: Sequence[dict[str, Any]],
    *,
    route_seed: int = DEFAULT_ROUTE_SEED,
) -> dict[str, dict[str, Any]]:
    """Select all route panels from one predeclared paired seed."""

    paired = paired_scenario_rows(rows)
    if route_seed not in paired:
        available = ", ".join(str(seed) for seed in paired)
        raise ValueError(
            f"route seed {route_seed} is unavailable; available seeds: {available}"
        )
    return paired[route_seed]


def _point(value: Sequence[int | float]) -> tuple[float, float]:
    if len(value) != 2:
        raise ValueError(f"expected a two-dimensional point, got {value!r}")
    return (float(value[0]), float(value[1]))


def _route_home_point(
    home: str,
    *,
    depot: tuple[float, float],
    stations: dict[int, tuple[float, float]],
) -> tuple[tuple[float, float], int | None]:
    if home == "origin":
        return depot, None
    if home.startswith("station_"):
        station_id = int(home.split("_", 1)[1])
        if station_id not in stations:
            raise ValueError(f"route home references unknown station {station_id}")
        return stations[station_id], station_id
    if home.startswith("point_") and home.endswith(")") and "(" in home:
        coordinates = home.rsplit("(", 1)[1][:-1].split(",")
        return _point(tuple(float(value) for value in coordinates)), None
    raise ValueError(f"unsupported route home {home!r}")


def route_layout_from_solution(solution: dict[str, Any]) -> RouteLayoutData:
    """Extract plotted geometry from the self-contained solution payload."""

    problem = solution["problem"]
    depot = _point(problem["depot_km"])
    stations = {
        int(item[0]): _point(item[1:])
        for item in solution.get("relay", {}).get("relay_coordinates", [])
    }
    active_station_ids = {
        int(item["station_id"])
        for item in solution.get("relay", {}).get(
            "relay_usage_per_station", []
        )
        if float(item.get("task_count", 0)) > 0
    }
    homes = tuple(problem["drone_homes"])
    route_payloads = tuple(solution["routes"])
    if len(homes) != len(route_payloads):
        raise ValueError("drone_homes and routes must have the same length")

    paths: list[tuple[tuple[float, float], ...]] = []
    service_points: set[tuple[float, float]] = set()
    for home, route in zip(homes, route_payloads, strict=True):
        start, home_station_id = _route_home_point(
            str(home),
            depot=depot,
            stations=stations,
        )
        if home_station_id is not None:
            active_station_ids.add(home_station_id)
        visits = tuple(_point(visit["location_km"]) for visit in route["visits"])
        for visit, point in zip(route["visits"], visits, strict=True):
            if visit.get("kind") not in {"RELAY_DROP", "RELAY_PICKUP"}:
                service_points.add(point)
        path = (start,) + visits
        if not bool(problem.get("open_routes", True)) and visits:
            path += (start,)
        paths.append(path)

    return RouteLayoutData(
        depot=depot,
        stations=tuple(sorted(stations.items())),
        active_station_ids=frozenset(active_station_ids),
        service_points=tuple(sorted(service_points)),
        routes=tuple(paths),
    )


def shared_route_bounds(
    layouts: Sequence[RouteLayoutData],
    *,
    padding_fraction: float = 0.04,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return one padded x/y extent for comparable route panels."""

    points: list[tuple[float, float]] = []
    for layout in layouts:
        points.append(layout.depot)
        points.extend(point for _, point in layout.stations)
        points.extend(layout.service_points)
        points.extend(point for route in layout.routes for point in route)
    if not points:
        raise ValueError("route layouts contain no coordinates")
    xs, ys = zip(*points)
    x_span = max(xs) - min(xs)
    y_span = max(ys) - min(ys)
    x_pad = max(x_span * padding_fraction, 0.25)
    y_pad = max(y_span * padding_fraction, 0.25)
    return (
        (min(xs) - x_pad, max(xs) + x_pad),
        (min(ys) - y_pad, max(ys) + y_pad),
    )


def plot_route_layouts(
    rows: Sequence[dict[str, Any]],
    scenario_dir: Path,
    output_dir: Path,
    *,
    route_seed: int = DEFAULT_ROUTE_SEED,
) -> None:
    selected = select_route_rows(rows, route_seed=route_seed)
    layouts = {
        scenario: route_layout_from_solution(
            _load(scenario_dir / row["solution_file"])
        )
        for scenario, row in selected.items()
    }
    x_limits, y_limits = shared_route_bounds(tuple(layouts.values()))
    with reference_plot_style():
        figure, axes = plt.subplots(
            2,
            2,
            figsize=FOUR_PANEL_FIGSIZE,
            sharex=True,
            sharey=True,
        )
        for axis, sid, panel in zip(
            axes.flat,
            SCENARIO_LABELS,
            ("(a)", "(b)", "(c)", "(d)"),
            strict=True,
        ):
            layout = layouts[sid]
            for route_index, path in enumerate(layout.routes):
                if len(path) < 2:
                    continue
                x, y = zip(*path)
                axis.plot(
                    x,
                    y,
                    color=plt.cm.tab10(route_index % 10),
                    linewidth=0.85,
                    alpha=0.78,
                    label="UAV routes" if route_index == 0 else "_nolegend_",
                )
            if layout.service_points:
                cx, cy = zip(*layout.service_points)
                axis.scatter(
                    cx,
                    cy,
                    marker="o",
                    s=4,
                    color="#303030",
                    alpha=0.45,
                    linewidth=0,
                    label="Service node",
                    zorder=3,
                )
            inactive = [
                point
                for station_id, point in layout.stations
                if station_id not in layout.active_station_ids
            ]
            active = [
                point
                for station_id, point in layout.stations
                if station_id in layout.active_station_ids
            ]
            if inactive:
                sx, sy = zip(*inactive)
                axis.scatter(
                    sx,
                    sy,
                    marker="s",
                    s=18,
                    facecolor="white",
                    edgecolor="#94C3CB",
                    linewidth=0.75,
                    label="Candidate station",
                    zorder=4,
                )
            if active:
                sx, sy = zip(*active)
                axis.scatter(
                    sx,
                    sy,
                    marker="s",
                    s=20,
                    facecolor="#029EAC",
                    edgecolor="white",
                    linewidth=0.55,
                    label="Active/home station",
                    zorder=5,
                )
            axis.scatter(
                [layout.depot[0]],
                [layout.depot[1]],
                marker="*",
                s=36,
                color="#E9807B",
                edgecolor="white",
                linewidth=0.45,
                label="Depot",
                zorder=6,
            )
            axis.set_title(SCENARIO_LABELS[sid])
            axis.set_aspect("equal", adjustable="box")
            axis.set_xlim(*x_limits)
            axis.set_ylim(*y_limits)
            _panel_label(axis, panel, y=-0.10)
        legend_entries: dict[str, Any] = {}
        for axis in axes.flat:
            handles, labels = axis.get_legend_handles_labels()
            for handle, label in zip(handles, labels, strict=True):
                legend_entries.setdefault(label, handle)
        figure.legend(
            legend_entries.values(),
            legend_entries,
            loc="upper center",
            ncol=5,
            bbox_to_anchor=(0.5, 0.995),
        )
        figure.suptitle(
            f"Paired route layouts for predefined seed {route_seed}",
            fontsize=8.5,
            y=0.955,
        )
        figure.supxlabel("x (km)", y=0.02, fontsize=8.5)
        figure.supylabel("y (km)", x=0.02, fontsize=8.5)
        figure.subplots_adjust(
            top=0.88,
            bottom=0.10,
            left=0.08,
            hspace=0.40,
            wspace=0.20,
        )
        _save_pair(figure, output_dir, "fig6_2_route_layouts")


def plot_sensitivity(rows: Sequence[dict[str, Any]], output_dir: Path) -> None:
    labels = {
        "drone_count": "Number of UAVs",
        "deadline_multiplier": "Deadline multiplier",
        "station_count": "Number of stations",
    }
    metrics = (
        ("late_count", "Late deliveries"),
        ("total_lateness_min", "Total lateness (min)"),
        ("distance_km", "Total distance (km)"),
    )
    for parameter, xlabel in labels.items():
        selected = unique_rows_by_numeric_key(
            tuple(row for row in rows if row["parameter"] == parameter),
            "level",
            context=f"sensitivity {parameter}",
        )
        x = [float(row["level"]) for row in selected]
        with reference_plot_style({"axes.grid": True}):
            figure, axes = plt.subplots(1, 3, figsize=(7.1, 2.75))
            for axis, (field, ylabel), panel in zip(
                axes,
                metrics,
                ("(a)", "(b)", "(c)"),
                strict=True,
            ):
                color, marker = METRIC_LINE_STYLES[field]
                axis.plot(
                    x,
                    [row[field] for row in selected],
                    color=color,
                    marker=marker,
                )
                axis.set_xlabel(xlabel)
                axis.set_ylabel(ylabel)
                axis.set_xticks(x)
                _panel_label(axis, panel)
            figure.subplots_adjust(bottom=0.26, wspace=0.44)
            _save_pair(figure, output_dir, f"fig6_3_{parameter}_sensitivity")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmarks", type=Path, default=DEFAULT_BENCHMARKS)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--sensitivity", type=Path, default=DEFAULT_SENSITIVITY)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--route-seed", type=int, default=DEFAULT_ROUTE_SEED)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    benchmark_path = args.benchmarks / "results.json"
    scenario_path = args.scenarios / "runs.json"
    sensitivity_path = args.sensitivity / "runs.json"
    with figure_manifest_on_success(
        output_dir=args.output_dir,
        input_paths={
            "benchmark_results": benchmark_path,
            "scenario_runs": scenario_path,
            "sensitivity_runs": sensitivity_path,
        },
        route_seed=args.route_seed,
    ):
        benchmark_rows = _load(benchmark_path)["runs"]
        scenario_rows = _load(scenario_path)
        sensitivity_rows = _load(sensitivity_path)
        plot_title_example(args.output_dir)
        plot_exact_validation(benchmark_rows, args.output_dir)
        plot_multiscale(benchmark_rows, args.output_dir)
        plot_operator_effectiveness(
            benchmark_rows,
            scenario_rows,
            args.output_dir,
        )
        plot_scenario_comparison(scenario_rows, args.output_dir)
        plot_route_layouts(
            scenario_rows,
            args.scenarios,
            args.output_dir,
            route_seed=args.route_seed,
        )
        plot_sensitivity(sensitivity_rows, args.output_dir)
    print(f"图像已写入 {args.output_dir}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
