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
            "seed": cell["seed"],
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
    assert len(rows) == 275
    assert len({row["run_id"] for row in rows}) == 240
    assert {row["scenario"] for row in rows} == set(manuscript.SCENARIO_IDS)


def test_manuscript_rejects_missing_strategy_even_if_other_rows_are_valid(formal_evidence):
    directory, _, rows, _ = formal_evidence
    rows[:] = [row for row in rows if row["scenario"] != "direct_relay"]
    with pytest.raises(manuscript.ArtefactError, match="275条"):
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
        manuscript._sensitivity_finding("drone_count", rows, {})


def test_manuscript_rejects_missing_paired_seed(formal_evidence):
    directory, manifest, _, _ = formal_evidence
    manifest["seeds"] = manifest["seeds"][:-1]
    with pytest.raises(manuscript.ArtefactError, match="五个配对种子"):
        manuscript._validate_sensitivity(directory)


def test_manuscript_rejects_cross_seed_baseline_reuse(formal_evidence):
    directory, _, rows, _ = formal_evidence
    reused = next(row for row in rows if row["reused_from"])
    wrong = next(row for row in rows if row["scenario"] == reused["scenario"]
                 and row["seed"] != reused["seed"] and row["parameter"] == "drone_count" and row["level"] == 8)
    reused["reused_from"] = wrong["cell_id"]
    with pytest.raises(manuscript.ArtefactError, match="同一次实际求解"):
        manuscript._validate_sensitivity(directory)


def test_manuscript_uses_seedwise_frequencies_not_best_mean(formal_evidence):
    from algorithm.experiments.sensitivity_statistics import summarize_sensitivity
    _, _, rows, _ = formal_evidence
    reports = summarize_sensitivity(rows)
    selected = [row for row in reports["summary"]["groups"]
                if row["scenario"] == "direct_relay" and row["parameter"] == "station_count"]
    axis = next(item for item in reports["stability"]["axis_stability"]
                if item["scenario"] == "direct_relay" and item["parameter"] == "station_count")
    text = manuscript._sensitivity_finding("station_count", selected, axis)
    assert "全轴相邻档位均未变差的种子有5/5个" in text
    assert "2档5/5、3档5/5、4档5/5、5档5/5、6档5/5" in text
    assert "最优观测点" not in text
    assert "改善/持平/变差为0/5/0" in text
    comparison = manuscript._sensitivity_comparison("station_count", reports["stability"])
    assert "D+R 0/5/0" in comparison
    assert "唯一最优/并列最优/未达最优" in comparison


@pytest.fixture
def aggregate_evidence(formal_evidence, monkeypatch):
    from algorithm.experiments.sensitivity_statistics import summarize_sensitivity
    directory, manifest, rows, _ = formal_evidence
    reports = summarize_sensitivity(rows)
    sources = {key: "a" * 64 for key in ("manifest_sha256", "runs_sha256", "statistics_script_sha256")}
    for report in reports.values():
        report["sources"] = sources
    receipt = {
        "schema_version": 2, "formal": True, "formal_runtime_checked": True,
        "statistics_checked": True, "mechanism_statistics_checked": True, "retained_warmup_runs": [],
        "validated_rows": 275, "validated_unique_solutions": 240,
        "validated_seeds": list(manuscript.EXPECTED_SENSITIVITY_SEEDS),
        "input_sha256": manifest["input_sha256"], "solver_source_sha256": manifest["solver_source_sha256"],
        "solution_sha256_by_run": {row["run_id"]: row["solution_sha256"] for row in rows},
        **sources,
        **{key: "a" * 64 for key in ("summary_sha256", "summary_csv_sha256", "stability_sha256",
                                    "audit_script_sha256", "statistics_audit_script_sha256")},
    }
    documents = {"summary.json": reports["summary"], "stability.json": reports["stability"], "validation.json": receipt}
    monkeypatch.setattr(manuscript, "_load_json", lambda path, _label: deepcopy(documents[path.name]))
    monkeypatch.setattr(manuscript, "_sha256", lambda path: "a" * 64)
    return directory, manifest, rows, reports, receipt


def test_manuscript_accepts_recomputed_aggregates_and_current_audit(aggregate_evidence):
    directory, manifest, rows, _, _ = aggregate_evidence
    groups, stability = manuscript._validate_sensitivity_statistics(directory, rows, manifest)
    assert len(groups) == 55
    assert all(row["n"] == 5 for row in groups)
    assert stability["seed_count"] == 5


@pytest.mark.parametrize("field", ["mean_late_count", "sd_late_count", "min_distance_km"])
def test_manuscript_rejects_changed_statistic(aggregate_evidence, field):
    directory, manifest, rows, reports, _ = aggregate_evidence
    reports["summary"]["groups"][0][field] += 0.1
    with pytest.raises(manuscript.ArtefactError, match="重新计算不一致"):
        manuscript._validate_sensitivity_statistics(directory, rows, manifest)


def test_manuscript_rejects_changed_stability(aggregate_evidence):
    directory, manifest, rows, reports, _ = aggregate_evidence
    reports["stability"]["axis_stability"][0]["endpoint_counts"]["improved"] += 1
    with pytest.raises(manuscript.ArtefactError, match="重新计算不一致"):
        manuscript._validate_sensitivity_statistics(directory, rows, manifest)


@pytest.mark.parametrize("field", ["summary_csv_sha256", "statistics_audit_script_sha256", "validated_seeds", "statistics_checked", "mechanism_statistics_checked"])
def test_manuscript_rejects_stale_independent_audit(aggregate_evidence, field):
    directory, manifest, rows, _, receipt = aggregate_evidence
    receipt[field] = None
    with pytest.raises(manuscript.ArtefactError, match="独立审计凭据"):
        manuscript._validate_sensitivity_statistics(directory, rows, manifest)


def test_manuscript_rejects_wrong_retained_run_receipt(aggregate_evidence):
    directory, manifest, rows, _, receipt = aggregate_evidence
    receipt["retained_warmup_runs"] = [rows[0]["run_id"]]
    with pytest.raises(manuscript.ArtefactError, match="retained_warmup_runs"):
        manuscript._validate_sensitivity_statistics(directory, rows, manifest)


def test_retained_warmup_count_does_not_count_reused_axes():
    retained = {"scenario": "direct_relay", "seed": 2026081701, "run_id": "run1",
                "_solution": {"metadata": {"relay_retained_warmup_incumbent": True}}}
    other = {**retained, "run_id": "run2", "_solution": {"metadata": {"relay_retained_warmup_incumbent": False}}}
    assert manuscript._retained_warmup_count([retained, deepcopy(retained), other]) == 1


@pytest.fixture
def exact_prefix_rows():
    rows = []
    for size, exact, heuristic in (
        (5, (0, 0., 28.953), (0, 0., 28.953)),
        (8, (1, .642723415, 46.40393118), (1, .7735082894, 45.50065580)),
        (10, (2, 17.864, 54.646), (2, 17.864, 54.646)),
        (12, (3, 65.78430927, 62.41057961), (3, 99.44994141, 64.18490208)),
    ):
        for method, score in (("Pareto-DP", exact), ("C2-Lex-ALNS", heuristic)):
            rows.append({"task_count": size, "method": method, "deadline_multiplier": 1.,
                         "matches_oracle": score == exact, "late_count": score[0],
                         "total_lateness_min": score[1], "distance_km": score[2]})
    return rows


def test_exact_summary_discloses_both_matching_and_unmatched_sizes(exact_prefix_rows):
    text = manuscript._exact_prefix_summary(exact_prefix_rows)
    assert "4个小规模前缀实例中，ALNS有2个" in text
    assert "任务数为5、10" in text
    assert "任务数为8、12的运行未达到精确得分" in text


def test_exact_gaps_do_not_treat_shorter_distance_as_a_win(exact_prefix_rows):
    text = manuscript._exact_prefix_gaps(exact_prefix_rows)
    assert "8任务时" in text and "12任务时" in text
    assert "0.774" in text and "0.643" in text
    assert "较短航程不能抵消更大的总逾期" in text
    assert "并不保证达到精确最优" in text


def test_scenario_comparisons_use_paired_raw_scores_instead_of_mean_order():
    rows = []
    for index, seed in enumerate(manuscript.EXPECTED_SCENARIO_SEEDS):
        for scenario in manuscript.SCENARIO_IDS:
            lateness = (9., 9., 100.)[index] if scenario == "direct_relay" else 10.
            rows.append({"scenario": scenario, "seed": seed, "late_count": 1,
                         "total_lateness_min": lateness, "distance_km": 20.})
    outcomes = [row["outcome"] for row in manuscript._paired_comparisons(rows)
                if row["scenario"] == "direct_relay"]
    assert outcomes == ["胜", "胜", "负"]


def test_scenario_comparison_does_not_round_before_determining_winner():
    rows = []
    for seed in manuscript.EXPECTED_SCENARIO_SEEDS:
        for scenario in manuscript.SCENARIO_IDS:
            rows.append({"scenario": scenario, "seed": seed, "late_count": 1,
                         "total_lateness_min": 1. if scenario == "direct_relay" else 1.0000000000001,
                         "distance_km": 100. if scenario == "direct_relay" else 20.})
    outcomes = [row["outcome"] for row in manuscript._paired_comparisons(rows)
                if row["scenario"] == "direct_relay"]
    assert outcomes == ["胜"] * 3


def test_appendix_distinguishes_raw_score_order_from_validation_tolerance():
    class EndOfSection(Exception):
        pass

    class Recorder:
        def __init__(self):
            self.paragraphs = []

        def heading(self, text, *_args, **_kwargs):
            if text.startswith("A.2"):
                raise EndOfSection()

        def paragraph(self, text):
            self.paragraphs.append(text)

    recorder = Recorder()
    with pytest.raises(EndOfSection):
        manuscript.write_appendix_a(recorder, None)
    text = "".join(recorder.paragraphs)
    assert "不以epsilon将近似得分合并为平局" in text
    assert "迟到超过1e-9 min才计入逾期数与总逾期" in text
    assert "不改变已形成Score的严格排序" in text
    assert "浮点指标只在数值容差范围内判断相等" not in text


def test_exact_matching_validator_does_not_merge_one_ulp_difference(tmp_path):
    import math

    directory = manuscript.ROOT / "algorithm/results/final_benchmarks"
    payload = json.loads((directory / "results.json").read_text(encoding="utf-8"))
    # This isolated fixture tests score equality, independently of whether
    # the frozen historical benchmark was produced by the current solver.
    # Update only the temporary copy; production provenance checks stay on.
    payload["manifest"]["solver_source_sha256"] = manuscript._solver_source_sha256()
    matched = next(row for row in payload["runs"]
                   if row["method"] == "C2-Lex-ALNS" and row.get("matches_oracle") is True)
    matched["distance_km"] = math.nextafter(matched["distance_km"], math.inf)
    (tmp_path / "results.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(manuscript.ArtefactError, match="matches_oracle与三层得分不一致"):
        manuscript._validate_benchmarks(tmp_path)


def test_benchmark_validator_rejects_stale_solver_source_fingerprint(tmp_path):
    directory = manuscript.ROOT / "algorithm/results/final_benchmarks"
    payload = json.loads((directory / "results.json").read_text(encoding="utf-8"))
    current_hash = manuscript._solver_source_sha256()
    stale_hash = ("0" if current_hash[0] != "0" else "1") + current_hash[1:]
    payload["manifest"]["solver_source_sha256"] = stale_hash
    (tmp_path / "results.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(manuscript.ArtefactError, match="求解器源码SHA-256与当前源码不一致"):
        manuscript._validate_benchmarks(tmp_path)


def test_stability_conclusion_does_not_claim_improvement_from_constant_scores(formal_evidence):
    from algorithm.experiments.sensitivity_statistics import summarize_sensitivity
    _, _, rows, _ = formal_evidence
    stability = summarize_sensitivity(rows)["stability"]
    trend, ranking = manuscript._sensitivity_conclusion(stability)
    assert "0/4种策略的端点得分均5/5改善" in trend
    assert "0/4种策略的末档在各种子中均唯一最优" in trend
    assert "没有策略在任一水平内全部种子均唯一最优" in ranking
    assert "15个水平始终并列最优" in ranking
    assert "不能将这些展示水平视为独立证据合并" in ranking


def test_stability_conclusion_reports_installed_formal_evidence():
    path = manuscript.ROOT / "algorithm/results/final_sensitivity/stability.json"
    stability = json.loads(path.read_text(encoding="utf-8"))
    trend, ranking = manuscript._sensitivity_conclusion(stability)
    assert "无人机数从8到16时，4种策略的5个种子均端点改善" in trend
    assert "截止倍率从0.80到1.20时，4种策略的5个种子均端点改善" in trend
    assert "D+R为0/5，最优档位为3/4/6站" in trend
    assert "D+P为0/5，最优档位为3/4/5/6站" in trend
    assert "D+P+R为0/5，最优档位为4/5/6站" in trend
    assert "D+P在8个水平内5/5唯一最优" in ranking
    assert "另有7个水平的最优策略集合随种子变化" in ranking
