"""Paired-seed descriptive summaries for the four-strategy sensitivity study.

The sample SD uses ddof=1. Mean +/- one SD describes between-seed spread,
not a confidence interval. Strategy and level comparisons always use each
seed's original lexicographic score, never a tuple of marginal means.

Run after the solver and before the combined route/statistics audit. This
module does not import the experiment runner or alter its frozen fingerprints.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import itertools
import json
import math
import statistics
from pathlib import Path
from typing import Any, Mapping, Sequence


FORMAL_SEEDS = (2026081701, 2026081702, 2026081703, 2026081704, 2026081705)
SCENARIOS = ("pure_direct", "direct_relay", "direct_stations", "direct_relay_stations")
SENSITIVITY_LEVELS = {
    "drone_count": (8, 9, 10, 12, 16),
    "deadline_multiplier": (0.8, 0.9, 1.0, 1.1, 1.2),
    "station_count": (2, 3, 4, 5, 6),
}
BASELINE_LEVELS = {"drone_count": 8, "deadline_multiplier": 1.0, "station_count": 4}
SCENARIOS_BY_PARAMETER = {
    parameter: tuple(s for s in SCENARIOS if parameter != "station_count" or s != "pure_direct")
    for parameter in SENSITIVITY_LEVELS
}
OBJECTIVE_METRICS = ("late_count", "total_lateness_min", "distance_km")
MECHANISM_METRICS = (
    "relay_task_count", "cross_uav_handoff_count", "total_relay_waiting_min",
    "deadhead_km", "home_assignment_rate",
)
METRICS = OBJECTIVE_METRICS + MECHANISM_METRICS
DISPERSION_DESCRIPTION = "sample SD (ddof=1); mean +/- one SD is not a confidence interval"
_DEFAULT_DIRECTORY = Path(__file__).resolve().parents[2] / "algorithm/results/final_sensitivity"
_COUNT_METRICS = {"late_count", "relay_task_count", "cross_uav_handoff_count"}
RowKey = tuple[str, str, float, int]


def _protocol(
    seeds: Sequence[int], levels: Mapping[str, Sequence[int | float]],
) -> tuple[tuple[int, ...], dict[str, tuple[float, ...]]]:
    seed_values = tuple(seeds)
    if len(seed_values) < 2 or len(set(seed_values)) != len(seed_values) or any(
        type(seed) is not int for seed in seed_values
    ):
        raise ValueError("At least two distinct integer paired seeds are required for sample SD")
    if not levels or set(levels) - set(SENSITIVITY_LEVELS):
        raise ValueError("Unsupported sensitivity parameters")
    ordered_levels = {}
    for parameter in SENSITIVITY_LEVELS:
        if parameter not in levels:
            continue
        values = tuple(float(level) for level in levels[parameter])
        if len(values) < 2 or tuple(sorted(set(values))) != values or any(
            not math.isfinite(value) for value in values
        ):
            raise ValueError(f"{parameter}: levels must be finite, unique and increasing")
        ordered_levels[parameter] = values
    return seed_values, ordered_levels


def validate_paired_rows(
    rows: Sequence[Mapping[str, Any]], *, seeds: Sequence[int] = FORMAL_SEEDS,
    levels: Mapping[str, Sequence[int | float]] = SENSITIVITY_LEVELS,
    budget_seconds: float = 300.0,
) -> dict[RowKey, Mapping[str, Any]]:
    """Require every applicable scenario/level/seed exactly once.

    A levels mapping containing one parameter supports an individual figure.
    All eight outcome/mechanism metrics are validated, and baseline copies
    must agree wherever multiple axes occur in the supplied matrix.
    """

    seed_values, ordered_levels = _protocol(seeds, levels)
    if not math.isfinite(budget_seconds) or budget_seconds <= 0:
        raise ValueError("Sensitivity budget must be finite and positive")
    expected = {
        (scenario, parameter, level, seed)
        for parameter, values in ordered_levels.items()
        for scenario in SCENARIOS_BY_PARAMETER[parameter]
        for level in values for seed in seed_values
    }
    indexed: dict[RowKey, Mapping[str, Any]] = {}
    for row in rows:
        try:
            key = (str(row["scenario"]), str(row["parameter"]), float(row["level"]), row["seed"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("Invalid sensitivity scenario/parameter/level/seed key") from exc
        if type(key[3]) is not int or key not in expected:
            raise ValueError(f"Unexpected sensitivity scenario/level/seed: {key}")
        if key in indexed:
            raise ValueError(f"Duplicate sensitivity scenario/level/seed: {key}")
        try:
            budget = float(row["effective_time_limit_seconds"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Missing valid sensitivity budget for {key}") from exc
        if not math.isclose(budget, budget_seconds, rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"Sensitivity uses a fixed {budget_seconds:g} s total budget; got {budget:g}")
        for metric in METRICS:
            try:
                value = float(row[metric])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{key}: missing valid metric {metric}") from exc
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{key}: nonfinite or negative {metric}")
            if metric in _COUNT_METRICS and not value.is_integer():
                raise ValueError(f"{key}: {metric} must be an integer count")
            if metric == "home_assignment_rate" and value > 1:
                raise ValueError(f"{key}: home_assignment_rate must be between 0 and 1")
        indexed[key] = row
    missing = expected - indexed.keys()
    if missing:
        raise ValueError(f"Missing paired sensitivity scenario/level/seed cells: {sorted(missing)}")
    for scenario in SCENARIOS:
        for seed in seed_values:
            baseline_rows = [
                indexed[scenario, parameter, float(BASELINE_LEVELS[parameter]), seed]
                for parameter, values in ordered_levels.items()
                if scenario in SCENARIOS_BY_PARAMETER[parameter] and BASELINE_LEVELS[parameter] in values
            ]
            for row in baseline_rows[1:]:
                source = baseline_rows[0]
                for field in (*METRICS, "run_id", "solution_sha256"):
                    if field in source or field in row:
                        if row.get(field) != source.get(field):
                            raise ValueError(f"Baseline reuse differs for {scenario}, seed={seed}: {field}")
    return indexed


def _describe(values: Sequence[float]) -> dict[str, float]:
    return {
        "mean": statistics.fmean(values), "sd": statistics.stdev(values),
        "min": min(values), "max": max(values),
    }


def aggregate_sensitivity(
    rows: Sequence[Mapping[str, Any]], *, seeds: Sequence[int] = FORMAL_SEEDS,
    levels: Mapping[str, Sequence[int | float]] = SENSITIVITY_LEVELS,
    budget_seconds: float = 300.0,
) -> list[dict[str, Any]]:
    """Return one flat mean/sample-SD/range record per applicable configuration."""

    seed_values, ordered_levels = _protocol(seeds, levels)
    indexed = validate_paired_rows(rows, seeds=seed_values, levels=ordered_levels, budget_seconds=budget_seconds)
    groups = []
    for scenario in SCENARIOS:
        for parameter, values in ordered_levels.items():
            if scenario not in SCENARIOS_BY_PARAMETER[parameter]:
                continue
            for level in values:
                group = {"scenario": scenario, "parameter": parameter, "level": level, "n": len(seed_values), "seeds": list(seed_values)}
                for metric in METRICS:
                    stats = _describe([float(indexed[scenario, parameter, level, seed][metric]) for seed in seed_values])
                    group.update({f"{stat}_{metric}": value for stat, value in stats.items()})
                    # Retain the mathematical endpoints even below zero. A chart
                    # may clip its display, but must never overwrite these data.
                    group[f"lower_sd_{metric}"] = stats["mean"] - stats["sd"]
                    group[f"upper_sd_{metric}"] = stats["mean"] + stats["sd"]
                groups.append(group)
    return groups


def _score(row: Mapping[str, Any]) -> tuple[float, float, float]:
    return tuple(float(row[metric]) for metric in OBJECTIVE_METRICS)


def _monotonic(values: Sequence[Any]) -> dict[str, bool]:
    return {
        "nonincreasing": all(right <= left for left, right in zip(values, values[1:])),
        "nondecreasing": all(right >= left for left, right in zip(values, values[1:])),
        "constant": all(value == values[0] for value in values[1:]),
    }


def paired_stability(
    rows: Sequence[Mapping[str, Any]], *, seeds: Sequence[int] = FORMAL_SEEDS,
    levels: Mapping[str, Sequence[int | float]] = SENSITIVITY_LEVELS,
    budget_seconds: float = 300.0,
) -> dict[str, Any]:
    """Describe paired winners, within-seed endpoint changes and level stability.

    Lower lexicographic triples are better. Per-level wins mean unique best;
    ties mean shared best. Pairwise wins/losses are relative to scenario_a.
    Exact original numbers are compared, with no rounding or epsilon ties.
    """

    seed_values, ordered_levels = _protocol(seeds, levels)
    indexed = validate_paired_rows(rows, seeds=seed_values, levels=ordered_levels, budget_seconds=budget_seconds)
    comparisons = []
    for parameter, values in ordered_levels.items():
        scenarios = SCENARIOS_BY_PARAMETER[parameter]
        for level in values:
            by_seed = []
            counts = {scenario: {"scenario": scenario, "wins": 0, "ties": 0, "losses": 0} for scenario in scenarios}
            pairwise = [{"scenario_a": a, "scenario_b": b, "wins": 0, "ties": 0, "losses": 0} for a, b in itertools.combinations(scenarios, 2)]
            for seed in seed_values:
                scores = {scenario: _score(indexed[scenario, parameter, level, seed]) for scenario in scenarios}
                best = min(scores.values())
                winners = [scenario for scenario, score in scores.items() if score == best]
                for scenario in scenarios:
                    outcome = "losses" if scenario not in winners else "wins" if len(winners) == 1 else "ties"
                    counts[scenario][outcome] += 1
                for pair in pairwise:
                    a, b = scores[pair["scenario_a"]], scores[pair["scenario_b"]]
                    pair["wins" if a < b else "losses" if a > b else "ties"] += 1
                by_seed.append({"seed": seed, "scores": {s: list(score) for s, score in scores.items()}, "winners": winners})
            comparisons.append({"parameter": parameter, "level": level, "n": len(seed_values), "strategy_counts": list(counts.values()), "pairwise": pairwise, "by_seed": by_seed})
    axes = []
    for scenario in SCENARIOS:
        for parameter, values in ordered_levels.items():
            if scenario not in SCENARIOS_BY_PARAMETER[parameter]:
                continue
            by_seed = []
            optimal_counts = {level: {"level": level, "unique_best": 0, "tied_best": 0, "including_ties": 0} for level in values}
            for seed in seed_values:
                series = [indexed[scenario, parameter, level, seed] for level in values]
                scores = [_score(row) for row in series]
                best = min(scores)
                optimal = [level for level, score in zip(values, scores, strict=True) if score == best]
                for level in optimal:
                    optimal_counts[level]["including_ties"] += 1
                    optimal_counts[level]["unique_best" if len(optimal) == 1 else "tied_best"] += 1
                by_seed.append({
                    "seed": seed, "start_score": list(scores[0]), "end_score": list(scores[-1]),
                    "endpoint_deltas": {metric: float(series[-1][metric]) - float(series[0][metric]) for metric in METRICS},
                    "endpoint_outcome": "improved" if scores[-1] < scores[0] else "worsened" if scores[-1] > scores[0] else "tied",
                    **_monotonic(scores),
                    "metric_monotonic": {metric: _monotonic([float(row[metric]) for row in series]) for metric in METRICS},
                    "optimal_levels": optimal,
                })
            axes.append({
                "scenario": scenario, "parameter": parameter, "n": len(seed_values),
                "start_level": values[0], "end_level": values[-1],
                "endpoint_summary": {metric: _describe([row["endpoint_deltas"][metric] for row in by_seed]) for metric in METRICS},
                "endpoint_counts": {outcome: sum(row["endpoint_outcome"] == outcome for row in by_seed) for outcome in ("improved", "tied", "worsened")},
                "monotonic_counts": {kind: sum(row[kind] for row in by_seed) for kind in ("nonincreasing", "nondecreasing", "constant")},
                "metric_monotonic_counts": {metric: {kind: sum(row["metric_monotonic"][metric][kind] for row in by_seed) for kind in ("nonincreasing", "nondecreasing", "constant")} for metric in METRICS},
                "optimal_level_counts": list(optimal_counts.values()), "by_seed": by_seed,
            })
    return {
        "schema_version": 1, "protocol": "paired_seed_sensitivity_stability",
        "seeds": list(seed_values), "seed_count": len(seed_values),
        "comparison": "Exact within-seed lexicographic (late_count, total_lateness_min, distance_km); lower is better; no ranking of mean triples",
        "ties_definition": "Strategy wins = unique best, ties = shared best; pairwise wins/losses refer to scenario_a; best-level including_ties counts may exceed seed_count",
        "monotonic_definition": "Levels increase left to right; nonincreasing/nondecreasing include equality; constant seeds count in both directions; descriptive, not a significance test",
        "level_comparisons": comparisons, "axis_stability": axes,
    }


def summarize_sensitivity(
    rows: Sequence[Mapping[str, Any]], *, seeds: Sequence[int] = FORMAL_SEEDS,
    levels: Mapping[str, Sequence[int | float]] = SENSITIVITY_LEVELS,
    budget_seconds: float = 300.0,
) -> dict[str, dict[str, Any]]:
    groups = aggregate_sensitivity(rows, seeds=seeds, levels=levels, budget_seconds=budget_seconds)
    return {
        "summary": {
            "schema_version": 1, "protocol": "paired_seed_sensitivity_summary",
            "seeds": list(seeds), "seed_count": len(seeds), "group_count": len(groups),
            "raw_row_count": len(rows), "metrics": list(METRICS),
            "dispersion": DISPERSION_DESCRIPTION, "groups": groups,
        },
        "stability": paired_stability(rows, seeds=seeds, levels=levels, budget_seconds=budget_seconds),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_statistics(directory: Path) -> dict[str, dict[str, Any]]:
    manifest_path, runs_path = directory / "manifest.json", directory / "runs.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = json.loads(runs_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 3 or manifest.get("protocol") != "four_strategy_one_factor_sensitivity":
        raise ValueError("Statistics require the schema-3 paired-seed sensitivity protocol")
    seeds, levels = _protocol(manifest["seeds"], manifest["levels"])
    budget = float(manifest["effective_seconds_per_run"])
    if manifest["formal"] and (
        seeds != FORMAL_SEEDS or levels != {key: tuple(float(x) for x in value) for key, value in SENSITIVITY_LEVELS.items()}
        or budget != 300.0 or manifest["planned_rows"] != 275 or manifest["planned_unique_solves"] != 240
    ):
        raise ValueError("Formal sensitivity statistics require five paired seeds, 275 rows and a 300 s budget")
    if manifest["seed_count"] != len(seeds) or manifest["planned_rows"] != len(rows):
        raise ValueError("Manifest seed or row count disagrees with the data")
    reports = summarize_sensitivity(rows, seeds=seeds, levels=levels, budget_seconds=budget)
    sources = {"manifest_sha256": _sha256(manifest_path), "runs_sha256": _sha256(runs_path), "statistics_script_sha256": _sha256(Path(__file__))}
    for name, report in reports.items():
        report["sources"] = sources
        temporary = directory / f".{name}.json.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        temporary.replace(directory / f"{name}.json")
    groups = reports["summary"]["groups"]
    temporary_csv = directory / ".summary.csv.tmp"
    with temporary_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        # Keep byte fingerprints stable when Git normalizes text files to LF.
        writer = csv.DictWriter(handle, fieldnames=list(groups[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows({**group, "seeds": ";".join(str(seed) for seed in group["seeds"])} for group in groups)
    temporary_csv.replace(directory / "summary.csv")
    return reports


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=_DEFAULT_DIRECTORY)
    args = parser.parse_args(argv)
    reports = write_statistics(args.directory)
    summary = reports["summary"]
    print(f"Summarized {summary['raw_row_count']} rows into {summary['group_count']} configurations across {summary['seed_count']} paired seeds")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
