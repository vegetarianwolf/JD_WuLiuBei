import copy
import csv
import hashlib
import json
import math
import shutil
import subprocess

import pytest

from algorithm.experiments import sensitivity_statistics as stats


def _rows(*, seeds=stats.FORMAL_SEEDS, levels=None):
    levels = levels or {"drone_count": (8, 9)}
    rows = []
    for scenario_index, scenario in enumerate(stats.SCENARIOS):
        for parameter, values in levels.items():
            if scenario not in stats.SCENARIOS_BY_PARAMETER[parameter]:
                continue
            baseline = stats.BASELINE_LEVELS[parameter]
            for level in values:
                # Each parameter's baseline has exactly the same outcomes for
                # the same strategy and seed, as a reused solve should.
                offset = round((float(level) - baseline) * (10 if parameter == "deadline_multiplier" else 1))
                for seed_index, seed in enumerate(seeds):
                    rows.append({
                        "scenario": scenario, "parameter": parameter, "level": level,
                        "seed": seed, "effective_time_limit_seconds": 300.0,
                        "late_count": 20 + scenario_index * 5 + seed_index + offset,
                        "total_lateness_min": 100.0 + scenario_index * 10 + seed_index * 2 + offset,
                        "distance_km": 600.0 + scenario_index * 10 + seed_index * 3 + offset,
                        "relay_task_count": seed_index if scenario in ("direct_relay", "direct_relay_stations") else 0,
                        "cross_uav_handoff_count": 0,
                        "total_relay_waiting_min": 0.0,
                        "deadhead_km": 20.0 + seed_index,
                        "home_assignment_rate": 0.25 + seed_index * 0.05,
                    })
    return rows


SMALL_LEVELS = {"drone_count": (8, 9)}


def test_summary_uses_unrounded_means_sample_sd_and_observed_ranges():
    groups = stats.aggregate_sensitivity(list(reversed(_rows())), levels=SMALL_LEVELS)
    assert len(groups) == 8
    first = groups[0]
    assert (first["scenario"], first["parameter"], first["level"]) == ("pure_direct", "drone_count", 8)
    assert first["n"] == 5
    assert first["seeds"] == list(stats.FORMAL_SEEDS)
    # The five counts are exactly 20, 21, 22, 23, 24: variance is 10 / 4.
    assert first["mean_late_count"] == 22.0
    assert first["sd_late_count"] == pytest.approx(math.sqrt(2.5))
    assert first["min_late_count"] == 20
    assert first["max_late_count"] == 24
    assert first["lower_sd_late_count"] == pytest.approx(22 - math.sqrt(2.5))
    assert first["upper_sd_late_count"] == pytest.approx(22 + math.sqrt(2.5))
    assert first["mean_home_assignment_rate"] == pytest.approx(0.35)
    assert first["sd_home_assignment_rate"] == pytest.approx(0.05 * math.sqrt(2.5))
    assert first["sd_relay_task_count"] == 0


def test_full_matrix_has_55_groups_with_five_seeds_not_275_independent_estimates():
    result = stats.summarize_sensitivity(_rows(levels=stats.SENSITIVITY_LEVELS))
    summary, stability = result["summary"], result["stability"]
    assert summary["raw_row_count"] == 275
    assert summary["group_count"] == 55
    assert all(group["n"] == 5 for group in summary["groups"])
    assert len(stability["level_comparisons"]) == 15
    assert len(stability["axis_stability"]) == 11
    station_comparisons = [row for row in stability["level_comparisons"] if row["parameter"] == "station_count"]
    assert all(len(row["strategy_counts"]) == 3 for row in station_comparisons)
    assert "ddof=1" in summary["dispersion"]
    assert "not a confidence interval" in summary["dispersion"]


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "seed", "level", "scenario", "budget"])
def test_aggregation_rejects_incomplete_unpaired_or_mixed_protocol_rows(mutation):
    rows = _rows()
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    elif mutation == "seed":
        rows[0]["seed"] = 123
    elif mutation == "level":
        rows[0]["level"] = 10
    elif mutation == "scenario":
        rows[0]["scenario"] = "unknown"
    else:
        rows[0]["effective_time_limit_seconds"] = 720
    with pytest.raises(ValueError):
        stats.aggregate_sensitivity(rows, levels=SMALL_LEVELS)


@pytest.mark.parametrize("metric", stats.METRICS)
def test_all_objective_and_mechanism_metrics_must_be_finite(metric):
    rows = _rows()
    rows[0][metric] = float("nan")
    with pytest.raises(ValueError, match=metric):
        stats.aggregate_sensitivity(rows, levels=SMALL_LEVELS)


def test_counts_must_be_integers_and_rates_must_use_fractional_units():
    rows = _rows()
    rows[0]["late_count"] = 0.5
    with pytest.raises(ValueError, match="integer count"):
        stats.aggregate_sensitivity(rows, levels=SMALL_LEVELS)
    rows[0]["late_count"] = 20
    rows[0]["home_assignment_rate"] = 25
    with pytest.raises(ValueError, match="between 0 and 1"):
        stats.aggregate_sensitivity(rows, levels=SMALL_LEVELS)


@pytest.mark.parametrize("seeds", [(1,), (1, 1), (1.0, 2)])
def test_sample_sd_requires_distinct_integer_replicates(seeds):
    with pytest.raises(ValueError, match="two distinct integer paired seeds"):
        stats.aggregate_sensitivity([], seeds=seeds, levels=SMALL_LEVELS)


def test_baseline_reuse_must_match_but_is_not_counted_as_extra_samples():
    rows = _rows(levels=stats.SENSITIVITY_LEVELS)
    baseline_copy = next(row for row in rows if row["parameter"] == "deadline_multiplier" and row["level"] == 1.0)
    baseline_copy["distance_km"] += 1
    with pytest.raises(ValueError, match="Baseline reuse differs.*distance_km"):
        stats.aggregate_sensitivity(rows)


def test_negative_mathematical_sd_endpoint_is_preserved_in_summary():
    rows = _rows()
    for row in rows:
        if row["scenario"] == "pure_direct":
            row["late_count"] = int(row["seed"] == stats.FORMAL_SEEDS[-1])
    first = stats.aggregate_sensitivity(rows, levels=SMALL_LEVELS)[0]
    assert first["mean_late_count"] == 0.2
    assert first["sd_late_count"] == pytest.approx(math.sqrt(0.2))
    assert first["lower_sd_late_count"] == pytest.approx(0.2 - math.sqrt(0.2))
    assert first["lower_sd_late_count"] < 0


def _set_score(row, score):
    row.update(dict(zip(stats.OBJECTIVE_METRICS, score, strict=True)))


def test_paired_winners_respect_each_lexicographic_priority_and_shared_best_ties():
    rows = _rows()
    direct = [(1, 100, 100), (3, 0, 100), (1, 10, 100), (1, 10, 10), (2, 10, 10)]
    relay = [(2, 0, 0), (2, 1000, 0), (1, 20, 0), (1, 10, 20), (2, 10, 10)]
    for row in rows:
        i = stats.FORMAL_SEEDS.index(row["seed"])
        _set_score(row, direct[i] if row["scenario"] == "pure_direct" else relay[i] if row["scenario"] == "direct_relay" else (9, 0, 0))
    comparison = stats.paired_stability(rows, levels=SMALL_LEVELS)["level_comparisons"][0]
    counts = {item["scenario"]: item for item in comparison["strategy_counts"]}
    assert counts["pure_direct"] == {"scenario": "pure_direct", "wins": 3, "ties": 1, "losses": 1}
    assert counts["direct_relay"] == {"scenario": "direct_relay", "wins": 1, "ties": 1, "losses": 3}
    assert comparison["pairwise"][0] == {"scenario_a": "pure_direct", "scenario_b": "direct_relay", "wins": 3, "ties": 1, "losses": 1}
    # The two weaker strategies tie with each other but do not share best.
    assert comparison["pairwise"][-1]["ties"] == 5
    assert counts["direct_stations"]["ties"] == 0
    assert counts["direct_stations"]["losses"] == 5
    assert comparison["by_seed"][-1]["winners"] == ["pure_direct", "direct_relay"]
    assert all(item["wins"] + item["ties"] + item["losses"] == 5 for item in comparison["pairwise"])


def test_paired_comparison_does_not_rank_the_tuple_of_marginal_means():
    rows = _rows()
    for row in rows:
        direct_score = 100 if row["seed"] == stats.FORMAL_SEEDS[-1] else 0
        _set_score(row, (direct_score, 0, 0) if row["scenario"] == "pure_direct" else (1, 0, 0) if row["scenario"] == "direct_relay" else (200, 0, 0))
    summary = stats.aggregate_sensitivity(rows, levels=SMALL_LEVELS)
    assert summary[0]["mean_late_count"] == 20
    assert summary[2]["mean_late_count"] == 1
    paired = stats.paired_stability(rows, levels=SMALL_LEVELS)["level_comparisons"][0]
    assert paired["pairwise"][0]["wins"] == 4
    assert paired["pairwise"][0]["losses"] == 1


def test_lexicographic_ties_are_not_created_by_rounding_original_scores():
    rows = _rows()
    for row in rows:
        _set_score(row, (1, 1, 1.0 if row["scenario"] == "pure_direct" else 1.0 + 1e-10))
    first = stats.paired_stability(rows, levels=SMALL_LEVELS)["level_comparisons"][0]
    assert first["pairwise"][0]["wins"] == 5
    assert first["pairwise"][0]["ties"] == 0


def test_endpoint_sd_is_based_on_paired_changes_and_monotonicity_is_per_seed():
    levels = {"drone_count": (8, 9, 10)}
    rows = _rows(levels=levels)
    paths = [(4, 3, 2), (1, 2, 3), (2, 1, 2), (1, 1, 1), (2, 2, 1)]
    for row in rows:
        i = stats.FORMAL_SEEDS.index(row["seed"])
        j = levels["drone_count"].index(row["level"])
        _set_score(row, (paths[i][j], 0, 0))
    axis = stats.paired_stability(rows, levels=levels)["axis_stability"][0]
    assert axis["endpoint_counts"] == {"improved": 2, "tied": 2, "worsened": 1}
    assert axis["monotonic_counts"] == {"nonincreasing": 3, "nondecreasing": 2, "constant": 1}
    assert axis["metric_monotonic_counts"]["late_count"] == axis["monotonic_counts"]
    # Paired changes are -2,+2,0,0,-1. Their variance is 8.8 / 4.
    assert axis["endpoint_summary"]["late_count"] == pytest.approx({"mean": -0.2, "sd": math.sqrt(2.2), "min": -2, "max": 2})
    best = axis["optimal_level_counts"]
    assert best == [
        {"level": 8, "unique_best": 1, "tied_best": 1, "including_ties": 2},
        {"level": 9, "unique_best": 1, "tied_best": 1, "including_ties": 2},
        {"level": 10, "unique_best": 2, "tied_best": 1, "including_ties": 3},
    ]
    assert axis["by_seed"][3]["optimal_levels"] == [8, 9, 10]


def _write_inputs(directory, rows=None):
    rows = rows or _rows(levels=stats.SENSITIVITY_LEVELS)
    manifest = {
        "schema_version": 3, "protocol": "four_strategy_one_factor_sensitivity",
        "formal": True, "seeds": list(stats.FORMAL_SEEDS), "seed_count": 5,
        "levels": stats.SENSITIVITY_LEVELS, "effective_seconds_per_run": 300.0,
        "planned_rows": 275, "planned_unique_solves": 240,
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (directory / "runs.json").write_text(json.dumps(rows), encoding="utf-8")


def test_statistics_cli_writes_all_configs_ranges_stability_and_source_hashes(tmp_path):
    _write_inputs(tmp_path)
    assert stats.main([str(tmp_path)]) == 0
    summary = json.loads((tmp_path / "summary.json").read_text())
    stability = json.loads((tmp_path / "stability.json").read_text())
    with (tmp_path / "summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    assert len(csv_rows) == 55
    csv_bytes = (tmp_path / "summary.csv").read_bytes()
    assert b"\r\n" not in csv_bytes
    assert csv_bytes.count(b"\n") == 56  # Header plus all configuration rows.
    assert hashlib.sha256(csv_bytes).hexdigest() == hashlib.sha256(
        csv_bytes.replace(b"\r\n", b"\n")
    ).hexdigest()
    assert csv_rows[0]["seeds"] == ";".join(str(seed) for seed in stats.FORMAL_SEEDS)
    assert float(csv_rows[0]["sd_late_count"]) == summary["groups"][0]["sd_late_count"]
    for report in (summary, stability):
        for name in ("manifest", "runs"):
            assert report["sources"][f"{name}_sha256"] == hashlib.sha256((tmp_path / f"{name}.json").read_bytes()).hexdigest()
        assert report["sources"]["statistics_script_sha256"]


def test_statistics_rejects_partial_data_before_writing_outputs(tmp_path):
    _write_inputs(tmp_path, _rows(levels=stats.SENSITIVITY_LEVELS)[:-1])
    with pytest.raises(ValueError):
        stats.write_statistics(tmp_path)
    assert not (tmp_path / "summary.json").exists()
    assert not (tmp_path / "summary.csv").exists()
    assert not (tmp_path / "stability.json").exists()


def test_summary_csv_git_text_normalization_preserves_its_byte_fingerprint(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("Git is required for the text-normalization regression check")
    _write_inputs(tmp_path)
    stats.write_statistics(tmp_path)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / ".gitattributes").write_text("*.csv text eol=lf\n", encoding="utf-8")
    raw_oid = subprocess.run(
        ["git", "hash-object", "--no-filters", "summary.csv"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    normalized_oid = subprocess.run(
        ["git", "hash-object", "--path=summary.csv", "summary.csv"],
        cwd=tmp_path, check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert raw_oid == normalized_oid


def test_formal_statistics_rejects_a_three_seed_manifest(tmp_path):
    _write_inputs(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["seeds"] = manifest["seeds"][:3]
    manifest["seed_count"] = 3
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="five paired seeds"):
        stats.write_statistics(tmp_path)
