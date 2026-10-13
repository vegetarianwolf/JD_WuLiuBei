"""Independent arithmetic examples and tamper checks for the statistics audit."""

from copy import deepcopy
import csv
import math

import pytest

from algorithm.experiments.audit_sensitivity_statistics import (
    _moments,
    audit_aggregates,
    audit_summary_csv,
)
from algorithm.experiments.sensitivity_statistics import summarize_sensitivity


SEEDS = (11, 22)
LEVELS = {"drone_count": (8, 10, 12)}


def _small_rows():
    # The direct strategy improves lexicographically for seed 11 despite a
    # longer route, and worsens for seed 22 despite a shorter route. Its seed
    # 11 optimum is shared by two levels; seed 22 has an interior optimum.
    scores = {
        "pure_direct": {
            11: ((1, 4, 10), (0, 7, 20), (0, 7, 20)),
            22: ((1, 6, 14), (0, 1, 8), (2, 1, 8)),
        },
        "direct_relay": {
            11: ((0, 9, 12), (0, 8, 20), (0, 7, 20)),
            22: ((1, 6, 14), (1, 3, 10), (1, 0, 9)),
        },
        "direct_stations": {
            11: ((9, 9, 90),) * 3,
            22: ((9, 11, 110),) * 3,
        },
        "direct_relay_stations": {
            11: ((10, 10, 100),) * 3,
            22: ((10, 12, 120),) * 3,
        },
    }
    rows = []
    for scenario, by_seed in scores.items():
        for seed, sequence in by_seed.items():
            for index, (level, score) in enumerate(zip(LEVELS["drone_count"], sequence)):
                relays = (index + 1 if seed == 11 else 3 - index) if scenario == "direct_relay" else 0
                rows.append({
                    "scenario": scenario, "parameter": "drone_count", "level": level,
                    "seed": seed, "effective_time_limit_seconds": 300.0,
                    "late_count": score[0], "total_lateness_min": score[1],
                    "distance_km": score[2], "relay_task_count": relays,
                    "cross_uav_handoff_count": relays,
                    "total_relay_waiting_min": relays / 2,
                    "deadhead_km": score[2] / 10,
                    "home_assignment_rate": 0.5 if "stations" in scenario else 0.0,
                })
    return rows


@pytest.fixture
def evidence():
    rows = _small_rows()
    reports = summarize_sensitivity(rows, seeds=SEEDS, levels=LEVELS)
    return rows, reports["summary"], reports["stability"]


def _axis(stability, scenario="pure_direct"):
    return next(item for item in stability["axis_stability"] if item["scenario"] == scenario)


def _group(summary, scenario="pure_direct", level=8):
    return next(item for item in summary["groups"] if item["scenario"] == scenario and item["level"] == level)


def test_independent_sample_moments_match_hand_calculation():
    # [1, 3, 5] has sample variance 4, rather than population variance 8/3.
    assert _moments([1, 3, 5]) == {
        "mean": 3.0, "sd": 2.0, "min": 1.0, "max": 5.0,
        "lower_sd": 1.0, "upper_sd": 5.0,
    }
    assert _moments([2, 4])["sd"] == pytest.approx(math.sqrt(2))


@pytest.mark.parametrize("values", [[], [1], [1, math.nan], [1, math.inf]])
def test_independent_moments_reject_inadequate_or_nonfinite_samples(values):
    with pytest.raises(ValueError):
        _moments(values)


def test_full_audit_matches_hand_calculated_pairs_optima_and_directions(evidence):
    rows, summary, stability = evidence
    result = audit_aggregates(rows, summary, stability, seeds=SEEDS)
    assert result == {"validated_groups": 12, "validated_level_comparisons": 3, "validated_axes": 4}

    baseline = _group(summary)
    assert baseline["mean_total_lateness_min"] == 5.0
    assert baseline["sd_total_lateness_min"] == pytest.approx(math.sqrt(2))
    assert baseline["min_total_lateness_min"] == 4.0
    assert baseline["max_total_lateness_min"] == 6.0

    direct = _axis(stability)
    assert direct["endpoint_counts"] == {"improved": 1, "tied": 0, "worsened": 1}
    assert direct["monotonic_counts"] == {"nonincreasing": 1, "nondecreasing": 0, "constant": 0}
    assert direct["endpoint_summary"]["late_count"]["mean"] == 0.0
    assert direct["endpoint_summary"]["late_count"]["sd"] == pytest.approx(math.sqrt(2))
    assert direct["endpoint_summary"]["distance_km"]["mean"] == 2.0
    assert direct["endpoint_summary"]["distance_km"]["sd"] == pytest.approx(8 * math.sqrt(2))
    assert direct["optimal_level_counts"] == [
        {"level": 8.0, "unique_best": 0, "tied_best": 0, "including_ties": 0},
        {"level": 10.0, "unique_best": 1, "tied_best": 1, "including_ties": 2},
        {"level": 12.0, "unique_best": 0, "tied_best": 1, "including_ties": 1},
    ]
    constant = _axis(stability, "direct_stations")
    assert constant["endpoint_counts"] == {"improved": 0, "tied": 2, "worsened": 0}
    assert constant["monotonic_counts"] == {"nonincreasing": 2, "nondecreasing": 2, "constant": 2}
    assert all(item["tied_best"] == 2 and item["unique_best"] == 0 for item in constant["optimal_level_counts"])

    relay = _axis(stability, "direct_relay")
    assert relay["endpoint_summary"]["relay_task_count"]["mean"] == 0.0
    assert relay["endpoint_summary"]["relay_task_count"]["sd"] == pytest.approx(2 * math.sqrt(2))
    assert relay["metric_monotonic_counts"]["relay_task_count"] == {
        "nonincreasing": 1, "nondecreasing": 1, "constant": 0,
    }
    first = stability["level_comparisons"][0]
    winners = {item["scenario"]: item for item in first["strategy_counts"]}
    assert winners["pure_direct"] == {"scenario": "pure_direct", "wins": 0, "ties": 1, "losses": 1}
    assert winners["direct_relay"] == {"scenario": "direct_relay", "wins": 1, "ties": 1, "losses": 0}


@pytest.mark.parametrize("mutation", ["missing_pair", "duplicate_pair", "wrong_seed"])
def test_statistics_audit_rejects_missing_or_duplicate_raw_pairs(evidence, mutation):
    rows, summary, stability = evidence
    if mutation == "missing_pair":
        rows.pop()
    elif mutation == "duplicate_pair":
        rows.append(deepcopy(rows[0]))
    else:
        rows[0]["seed"] = 999
    with pytest.raises(ValueError, match="seed"):
        audit_aggregates(rows, summary, stability, seeds=SEEDS)


@pytest.mark.parametrize("seeds", [(), (11,), (11, 11)])
def test_statistics_audit_requires_distinct_repeated_seeds(evidence, seeds):
    with pytest.raises(ValueError, match="distinct seeds"):
        audit_aggregates(*evidence, seeds=seeds)


@pytest.mark.parametrize("mutation", [
    "population_sd", "sd_interval", "summary_n", "summary_missing", "summary_duplicate",
    "winner", "paired_count", "duplicate_pairwise", "paired_score", "endpoint",
    "optimal_level", "optimal_frequency", "metric_direction", "lexicographic_direction",
    "mechanism_direction", "mechanism_delta", "mechanism_sd", "endpoint_population_sd",
    "axis_missing", "comparison_duplicate",
])
def test_statistics_audit_rejects_tampered_descriptions_and_stability(evidence, mutation):
    rows, summary, stability = evidence
    direct = _axis(stability)
    relay = _axis(stability, "direct_relay")
    first = stability["level_comparisons"][0]
    if mutation == "population_sd":
        _group(summary)["sd_total_lateness_min"] = 1.0
    elif mutation == "sd_interval":
        _group(summary)["lower_sd_distance_km"] += 1
    elif mutation == "summary_n":
        _group(summary)["n"] = 3
    elif mutation == "summary_missing":
        summary["groups"].pop()
    elif mutation == "summary_duplicate":
        summary["groups"].append(deepcopy(summary["groups"][0]))
    elif mutation == "winner":
        first["by_seed"][0]["winners"] = ["pure_direct"]
    elif mutation == "paired_count":
        first["pairwise"][0]["wins"] += 1
    elif mutation == "duplicate_pairwise":
        first["pairwise"].append(deepcopy(first["pairwise"][0]))
    elif mutation == "paired_score":
        first["by_seed"][0]["scores"]["pure_direct"][0] = 0
    elif mutation == "endpoint":
        direct["endpoint_counts"]["improved"] = 2
    elif mutation == "optimal_level":
        direct["by_seed"][0]["optimal_levels"] = [8]
    elif mutation == "optimal_frequency":
        direct["optimal_level_counts"][1]["unique_best"] = 2
    elif mutation == "metric_direction":
        direct["metric_monotonic_counts"]["distance_km"]["nonincreasing"] = 2
    elif mutation == "lexicographic_direction":
        direct["by_seed"][0]["nonincreasing"] = False
    elif mutation == "mechanism_direction":
        relay["metric_monotonic_counts"]["relay_task_count"]["nonincreasing"] = 2
    elif mutation == "mechanism_delta":
        relay["by_seed"][0]["endpoint_deltas"]["relay_task_count"] = 0
    elif mutation == "mechanism_sd":
        relay["endpoint_summary"]["relay_task_count"]["sd"] = 2.0
    elif mutation == "endpoint_population_sd":
        direct["endpoint_summary"]["late_count"]["sd"] = 1.0
    elif mutation == "axis_missing":
        stability["axis_stability"].pop()
    else:
        stability["level_comparisons"].append(deepcopy(first))
    with pytest.raises(ValueError):
        audit_aggregates(rows, summary, stability, seeds=SEEDS)


def test_statistics_audit_accepts_reversed_pair_orientation_when_counts_reverse(evidence):
    rows, summary, stability = evidence
    pair = stability["level_comparisons"][0]["pairwise"][0]
    pair["scenario_a"], pair["scenario_b"] = pair["scenario_b"], pair["scenario_a"]
    pair["wins"], pair["losses"] = pair["losses"], pair["wins"]
    audit_aggregates(rows, summary, stability, seeds=SEEDS)


def test_paired_endpoint_sd_uses_correlated_differences_not_marginal_spreads():
    seeds = (1, 2, 3, 4, 5)
    levels = {"drone_count": (8, 12)}
    rows = []
    for scenario in ("pure_direct", "direct_relay", "direct_stations", "direct_relay_stations"):
        for seed in seeds:
            for level in levels["drone_count"]:
                # Endpoints are perfectly positively correlated across seeds:
                # [100,200,300,400,500] -> [110,210,310,410,510].
                rows.append({
                    "scenario": scenario, "parameter": "drone_count", "level": level,
                    "seed": seed, "effective_time_limit_seconds": 300.0,
                    "late_count": 1, "total_lateness_min": 10.0,
                    "distance_km": 100.0 * seed + (10.0 if level == 12 else 0.0),
                    "relay_task_count": 0, "cross_uav_handoff_count": 0,
                    "total_relay_waiting_min": 0.0, "deadhead_km": 0.0,
                    "home_assignment_rate": 0.0,
                })
    reports = summarize_sensitivity(rows, seeds=seeds, levels=levels)
    summary, stability = reports["summary"], reports["stability"]
    assert _group(summary, level=8)["sd_distance_km"] == pytest.approx(math.sqrt(25_000))
    assert _group(summary, level=12)["sd_distance_km"] == pytest.approx(math.sqrt(25_000))
    endpoint = _axis(stability)["endpoint_summary"]["distance_km"]
    assert endpoint == {"mean": 10.0, "sd": 0.0, "min": 10.0, "max": 10.0}
    audit_aggregates(rows, summary, stability, seeds=seeds)

    # Treating the paired endpoints as independent gives sqrt(2)*marginal SD.
    endpoint["sd"] = math.sqrt(50_000)
    with pytest.raises(ValueError, match="paired endpoint summary"):
        audit_aggregates(rows, summary, stability, seeds=seeds)


def _write_csv(path, groups):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(groups[0]))
        writer.writeheader()
        writer.writerows({**group, "seeds": ";".join(map(str, group["seeds"]))} for group in groups)


def test_statistics_audit_accepts_summary_csv(evidence, tmp_path):
    _, summary, _ = evidence
    path = tmp_path / "summary.csv"
    _write_csv(path, summary["groups"])
    audit_summary_csv(path, summary)


@pytest.mark.parametrize("mutation", ["sd", "missing", "duplicate", "seed"])
def test_statistics_audit_rejects_csv_tampering(evidence, tmp_path, mutation):
    _, summary, _ = evidence
    groups = deepcopy(summary["groups"])
    if mutation == "sd":
        groups[0]["sd_total_lateness_min"] = 1.0
    elif mutation == "missing":
        groups.pop()
    elif mutation == "duplicate":
        groups.append(deepcopy(groups[0]))
    else:
        groups[0]["seeds"] = [11, 999]
    path = tmp_path / "summary.csv"
    _write_csv(path, groups)
    with pytest.raises(ValueError):
        audit_summary_csv(path, summary)
