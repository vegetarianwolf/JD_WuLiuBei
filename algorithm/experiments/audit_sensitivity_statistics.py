"""Independent arithmetic and paired-comparison audit for sensitivity summaries.

This deliberately does not import the statistics producer: every displayed
summary and stability count is recomputed directly from the validated raw rows.
"""
from __future__ import annotations

import csv
import itertools
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

OBJECTIVES = ("late_count", "total_lateness_min", "distance_km")
METRICS = OBJECTIVES + (
    "relay_task_count", "cross_uav_handoff_count", "total_relay_waiting_min",
    "deadhead_km", "home_assignment_rate",
)


def _moments(values):
    numbers = [float(v) for v in values]
    if len(numbers) < 2 or not all(math.isfinite(v) for v in numbers):
        raise ValueError("Statistics audit needs at least two finite paired observations")
    mean = math.fsum(numbers) / len(numbers)
    sd = math.sqrt(math.fsum((x - mean) ** 2 for x in numbers) / (len(numbers) - 1))
    return {"mean": mean, "sd": sd, "min": min(numbers), "max": max(numbers),
            "lower_sd": mean - sd, "upper_sd": mean + sd}


def _number(actual, expected, label):
    try:
        matches = math.isfinite(float(actual)) and math.isclose(
            float(actual), float(expected), rel_tol=1e-10, abs_tol=1e-9)
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise ValueError(f"Statistics audit mismatch: {label}")


def _indexed(items, key, expected_keys, label):
    index = {key(item): item for item in items}
    if len(index) != len(items) or set(index) != set(expected_keys):
        raise ValueError(f"Statistics audit coverage mismatch: {label}")
    return index


def _score(row):
    return tuple(float(row[metric]) for metric in OBJECTIVES)


def _directions(values):
    return {
        "nonincreasing": all(b <= a for a, b in zip(values, values[1:])),
        "nondecreasing": all(b >= a for a, b in zip(values, values[1:])),
        "constant": all(v == values[0] for v in values),
    }


def audit_aggregates(rows, summary, stability, *, seeds):
    """Independently verify all 55 aggregates and all seedwise comparisons."""
    seeds = list(seeds)
    if len(seeds) < 2 or len(set(seeds)) != len(seeds):
        raise ValueError("Statistics audit requires at least two distinct seeds without duplicates")
    grouped = defaultdict(list)
    levels = defaultdict(dict)
    axes = defaultdict(dict)
    for row in rows:
        key = (row["scenario"], row["parameter"], float(row["level"]))
        grouped[key].append(row)
        levels[(row["parameter"], float(row["level"]))][(row["seed"], row["scenario"])] = row
        axes[(row["scenario"], row["parameter"])][(row["seed"], float(row["level"]))] = row
    groups = _indexed(summary["groups"], lambda x: (x["scenario"], x["parameter"], float(x["level"])), grouped, "summary groups")
    for key, raw in grouped.items():
        item = groups[key]
        if len(raw) != len(seeds) or sorted(r["seed"] for r in raw) != sorted(seeds):
            raise ValueError("Statistics audit found missing/duplicate seeds")
        if item["n"] != len(seeds) or item["seeds"] != seeds:
            raise ValueError("Statistics audit sample count mismatch")
        for metric in METRICS:
            for statistic, expected in _moments(r[metric] for r in raw).items():
                _number(item[f"{statistic}_{metric}"], expected, f"{key}/{statistic}_{metric}")

    if stability["seeds"] != seeds or stability["seed_count"] != len(seeds):
        raise ValueError("Statistics audit stability seed mismatch")
    comparisons = _indexed(stability["level_comparisons"], lambda x: (x["parameter"], float(x["level"])), levels, "level comparisons")
    for key, raw in levels.items():
        item = comparisons[key]
        if item["n"] != len(seeds):
            raise ValueError("Statistics audit comparison sample count mismatch")
        scenarios = sorted({scenario for _, scenario in raw})
        by_seed = _indexed(item["by_seed"], lambda x: x["seed"], seeds, "comparison seeds")
        counts = {s: Counter(wins=0, ties=0, losses=0) for s in scenarios}
        pairs = {pair: Counter(wins=0, ties=0, losses=0) for pair in itertools.combinations(scenarios, 2)}
        for seed in seeds:
            scores = {scenario: _score(raw[(seed, scenario)]) for scenario in scenarios}
            best = min(scores.values())
            winners = {scenario for scenario, score in scores.items() if score == best}
            record = by_seed[seed]
            if set(record["scores"]) != set(scenarios) or set(record["winners"]) != winners or len(record["winners"]) != len(winners):
                raise ValueError("Statistics audit best-strategy winner mismatch")
            for scenario, score in scores.items():
                if tuple(record["scores"][scenario]) != score:
                    raise ValueError("Statistics audit raw paired score mismatch")
                counts[scenario]["wins" if scenario in winners and len(winners) == 1 else "ties" if scenario in winners else "losses"] += 1
            for a, b in pairs:
                outcome = "wins" if scores[a] < scores[b] else "ties" if scores[a] == scores[b] else "losses"
                pairs[(a, b)][outcome] += 1
        actual_counts = _indexed(item["strategy_counts"], lambda x: x["scenario"], scenarios, "strategy counts")
        for scenario in scenarios:
            for field, expected in counts[scenario].items():
                if actual_counts[scenario][field] != expected:
                    raise ValueError("Statistics audit best-strategy count mismatch")
        actual_pairs = _indexed(item["pairwise"], lambda x: tuple(sorted((x["scenario_a"], x["scenario_b"]))), pairs, "paired strategy counts")
        for pair, expected in pairs.items():
            record = actual_pairs[pair]
            reversed_pair = record["scenario_a"] != pair[0]
            for field, value in expected.items():
                actual_field = ({"wins": "losses", "losses": "wins", "ties": "ties"}[field] if reversed_pair else field)
                if record[actual_field] != value:
                    raise ValueError("Statistics audit pairwise win/tie/loss mismatch")

    actual_axes = _indexed(stability["axis_stability"], lambda x: (x["scenario"], x["parameter"]), axes, "axis stability")
    for key, raw in axes.items():
        item = actual_axes[key]
        axis_levels = sorted({level for _, level in raw})
        if item["n"] != len(seeds) or item["start_level"] != axis_levels[0] or item["end_level"] != axis_levels[-1]:
            raise ValueError("Statistics audit axis endpoints mismatch")
        by_seed = _indexed(item["by_seed"], lambda x: x["seed"], seeds, "axis seeds")
        outcomes = Counter(improved=0, tied=0, worsened=0)
        directions = Counter(nonincreasing=0, nondecreasing=0, constant=0)
        metric_directions = {metric: Counter(nonincreasing=0, nondecreasing=0, constant=0) for metric in METRICS}
        best_counts = {level: Counter(unique_best=0, tied_best=0, including_ties=0) for level in axis_levels}
        deltas = {metric: [] for metric in METRICS}
        for seed in seeds:
            ordered = [raw[(seed, level)] for level in axis_levels]
            scores = [_score(r) for r in ordered]
            record = by_seed[seed]
            outcome = "improved" if scores[-1] < scores[0] else "tied" if scores[-1] == scores[0] else "worsened"
            outcomes[outcome] += 1
            if tuple(record["start_score"]) != scores[0] or tuple(record["end_score"]) != scores[-1] or record["endpoint_outcome"] != outcome:
                raise ValueError("Statistics audit endpoint outcome mismatch")
            for field, value in _directions(scores).items():
                if record[field] is not value:
                    raise ValueError("Statistics audit lexicographic monotonicity mismatch")
                directions[field] += value
            for metric in METRICS:
                values = [r[metric] for r in ordered]
                delta = values[-1] - values[0]
                deltas[metric].append(delta)
                _number(record["endpoint_deltas"][metric], delta, "paired endpoint delta")
                for field, value in _directions(values).items():
                    if record["metric_monotonic"][metric][field] is not value:
                        raise ValueError("Statistics audit metric monotonicity mismatch")
                    metric_directions[metric][field] += value
            best_levels = {level for level, score in zip(axis_levels, scores) if score == min(scores)}
            if set(record["optimal_levels"]) != best_levels or len(record["optimal_levels"]) != len(best_levels):
                raise ValueError("Statistics audit optimal level mismatch")
            for level in best_levels:
                best_counts[level]["including_ties"] += 1
                best_counts[level]["unique_best" if len(best_levels) == 1 else "tied_best"] += 1
        if item["endpoint_counts"] != dict(outcomes) or item["monotonic_counts"] != dict(directions):
            raise ValueError("Statistics audit axis stability count mismatch")
        if item["metric_monotonic_counts"] != {m: dict(c) for m, c in metric_directions.items()}:
            raise ValueError("Statistics audit metric direction count mismatch")
        best_records = _indexed(item["optimal_level_counts"], lambda x: float(x["level"]), axis_levels, "optimal level counts")
        for level, expected in best_counts.items():
            if any(best_records[level][field] != count for field, count in expected.items()):
                raise ValueError("Statistics audit optimal level frequency mismatch")
        for metric in METRICS:
            for statistic, expected in _moments(deltas[metric]).items():
                if statistic in {"mean", "sd", "min", "max"}:
                    _number(item["endpoint_summary"][metric][statistic], expected, "paired endpoint summary")
    return {"validated_groups": len(groups), "validated_level_comparisons": len(comparisons), "validated_axes": len(actual_axes)}


def audit_summary_csv(path, summary):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        csv_rows = list(csv.DictReader(stream))
    keys = lambda row: (row["scenario"], row["parameter"], float(row["level"]))
    actual = _indexed(csv_rows, keys, [keys(r) for r in summary["groups"]], "summary CSV")
    for row in summary["groups"]:
        stored = actual[keys(row)]
        _number(stored["n"], row["n"], "CSV sample count")
        if stored.get("seeds") != ";".join(str(seed) for seed in row["seeds"]):
            raise ValueError("Statistics audit CSV paired seed list mismatch")
        for metric in METRICS:
            for statistic in ("mean", "sd", "min", "max", "lower_sd", "upper_sd"):
                field = f"{statistic}_{metric}"
                _number(stored[field], row[field], f"CSV {field}")
