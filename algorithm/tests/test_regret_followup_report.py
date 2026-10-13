"""Small source/decision regressions for the follow-up report; no search runs."""

import hashlib
from html.parser import HTMLParser
import json

import pytest

from algorithm.experiments import render_regret_followup_report as report


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _row(variant="baseline", seed=2026101321, late=63, stage="confirm", **changes):
    budget = {"confirm": 240, "medium": 30, "extensions": 20}.get(stage, 10)
    row = {
        "variant": variant, "seed": seed, "stage": stage, "case_id": "fleet200", "scenario": "pure_direct",
        "task_count": 200, "drone_count": 8, "budget_seconds": budget,
        "end_to_end_seconds": budget - 1, "construction_seconds": .5, "search_seconds": budget - 1.6,
        "validation_seconds": .01, "serialization_seconds": .01, "valid": True, "budget_compliant": True,
        "late_count": late, "total_lateness_min": 2000, "distance_km": 600,
        "on_time_count": 200 - late, "on_time_rate": (200 - late) / 200, "iterations": 100,
        "best_trajectory": [[0, .5, 100, 3000, 700], [100, budget - 2, late, 2000, 600]],
        "initial_routes_sha256": "a" * 64, "source_sha256": "b" * 64,
    }
    row.update(changes)
    return row


def _batch(directory, rows=(), decisions=()):
    _write(directory / "manifest.json", {
        "schema_version": 2, "kind": "regret_followup", "baseline_variant": "baseline",
        "seeds": {"confirm": [report.KNOWN_SEED, *report.NEW_SEEDS], "medium": [2026101351, 2026101352, 2026101353]},
        "source_snapshots": {}, "protocol": {
            "stage_cases": {"medium": ["single25", "fleet200"], "confirm": ["fleet200"]},
            "stage_budgets_seconds": {"medium": {"single25": 5, "fleet200": 30}, "confirm": {"fleet200": 240}},
        },
    })
    _write(directory / "runs.json", list(rows))
    _write(directory / "decisions.json", list(decisions))


def test_incomplete_batch_says_running_and_does_not_invent_scores(tmp_path):
    _batch(tmp_path, [_row(stage="screen", seed=2026101341)])
    followup = report.build_followup_data(tmp_path)["followup"]
    assert followup["status"] == "running" and "进行中" in followup["message"]
    assert followup["confirmation"]["complete_pairs"] == 0
    assert all(r["status"] == "missing" for r in followup["confirmation"]["rows"])
    assert followup["diagnostics"]["history"] is None


def test_failed_middle_stage_preserves_no_promotion_decision(tmp_path):
    _batch(tmp_path, decisions=[
        {"stage": "screen", "selected_variant": "fallback"},
        {"stage": "medium", "selected_variant": "fallback", "passed": False},
        {"stage": "confirm", "skipped": True, "passed": False},
    ])
    f = report.build_followup_data(tmp_path)["followup"]
    assert f["status"] == "not_promoted" and "未晋级" in f["message"]
    assert f["selected_variant"] == "fallback"


def test_confirmation_separates_known_regression_from_three_new_seeds(tmp_path):
    rows = []
    for index, seed in enumerate([report.KNOWN_SEED, *report.NEW_SEEDS]):
        rows.extend([_row(seed=seed), _row("portfolio", seed, late=63 if index == 3 else 62)])
    _batch(tmp_path, rows, [{"stage": "confirm", "selected_variant": "portfolio", "passed": True},
                            {"stage": "extensions", "passed": True}])
    f = report.build_followup_data(tmp_path)["followup"]
    assert f["status"] == "passed" and f["confirmation"]["passed_from_raw_pairs"]
    assert f["confirmation"]["counts"]["known"]["win"] == 1
    assert f["confirmation"]["counts"]["new"]["win"] == 2
    assert f["confirmation"]["counts"]["new"]["tie"] == 1


def test_claimed_pass_requires_all_raw_pairs_not_mean_tuple(tmp_path):
    rows = [_row(), _row("fallback", late=65, total_lateness_min=1, distance_km=1),
            _row("fallback", seed=report.NEW_SEEDS[0], late=1)]
    _batch(tmp_path, rows, [{"stage": "confirm", "selected_variant": "fallback", "passed": True}])
    f = report.build_followup_data(tmp_path)["followup"]
    assert f["status"] == "inconsistent"
    assert f["confirmation"]["complete_pairs"] == 1
    assert f["confirmation"]["counts"]["known"]["loss"] == 1
    assert f["confirmation"]["counts"]["new"]["win"] == 0


def test_confirmation_requires_same_initial_routes(tmp_path):
    _batch(tmp_path, [_row(), _row("fallback", late=60, initial_routes_sha256="different")],
           [{"stage": "screen", "selected_variant": "fallback"}])
    f = report.build_followup_data(tmp_path)["followup"]
    assert f["confirmation"]["counts"]["known"]["invalid"] == 1
    assert f["confirmation"]["complete_pairs"] == 0


def test_supplemental_batch_never_merges_baselines_or_main_decision(tmp_path):
    _batch(tmp_path, [_row(stage="screen", seed=2026101341)],
           [{"stage": "confirm", "skipped": True, "passed": False}])
    _batch(tmp_path / "supplemental_trial", [_row(), _row("portfolio", late=60)])
    data = report.build_followup_data(tmp_path)
    assert data["run_count"] == 1 and len(data["groups"]) == 1
    assert data["followup"]["status"] == "not_promoted"
    assert data["followup"]["supplemental"]["run_count"] == 2


def _medium_rows(candidate_late_counts=(62, 62, 63)):
    rows = []
    for seed, late in zip((2026101351, 2026101352, 2026101353), candidate_late_counts, strict=True):
        for variant, score in (("baseline", 63), ("portfolio", late)):
            rows.append(_row(variant, seed, score, "medium"))
            rows.append(_row(variant, seed, 13, "medium", case_id="single25", task_count=25,
                             drone_count=1, budget_seconds=5, end_to_end_seconds=4,
                             search_seconds=3.4, on_time_count=12, on_time_rate=12 / 25))
    return rows


def test_supplemental_middle_gate_recomputed_from_its_fresh_pairs(tmp_path):
    _batch(tmp_path, decisions=[{"stage": "confirm", "skipped": True, "passed": False}])
    # Parent's much better baseline must not enter the independent trial.
    _write(tmp_path / "runs.json", [_row(seed=2026101351, late=1, stage="medium")])
    _batch(tmp_path / "supplemental_trial", _medium_rows(), [
        {"stage": "screen", "selected_variant": "portfolio", "inherited_qualification": True},
    ])
    f = report.build_followup_data(tmp_path)["followup"]
    s = f["supplemental"]
    assert f["status"] == "not_promoted"
    assert s["medium"]["gate_passed_from_raw_pairs"] is True
    assert s["medium"]["cases"][1]["counts"]["win"] == 2
    assert s["status"] == "running" and "中测通过" in s["message"]
    assert s["medium"]["pairs"][3]["baseline_score"][0] == 63


def test_supplemental_passed_decision_does_not_hide_raw_primary_regression(tmp_path):
    _batch(tmp_path)
    _batch(tmp_path / "supplemental_trial", _medium_rows((62, 62, 65)), [
        {"stage": "medium", "selected_variant": "portfolio", "passed": True},
    ])
    s = report.build_followup_data(tmp_path)["followup"]["supplemental"]
    assert s["status"] == "not_promoted"
    assert s["medium"]["gate_passed_from_raw_pairs"] is False
    assert s["medium"]["cases"][1]["primary_regressions"] == 1


def test_supplemental_long_pass_is_visible_separately_from_main_failure(tmp_path):
    _batch(tmp_path, decisions=[{"stage": "confirm", "skipped": True, "passed": False}])
    rows = _medium_rows()
    for seed in (report.KNOWN_SEED, *report.NEW_SEEDS):
        rows.extend([_row(seed=seed), _row("portfolio", seed, late=62)])
    _batch(tmp_path / "supplemental_trial", rows)
    f = report.build_followup_data(tmp_path)["followup"]
    assert f["status"] == "not_promoted"
    assert f["supplemental"]["status"] == "passed"
    assert f["supplemental"]["confirmation"]["counts"]["known"]["win"] == 1
    assert f["supplemental"]["confirmation"]["counts"]["new"]["win"] == 3


def test_supplemental_confirmation_requires_exact_child_initial_routes(tmp_path):
    _batch(tmp_path)
    _batch(tmp_path / "supplemental_trial", [_row(), _row("portfolio", late=60, initial_routes_sha256="different")])
    s = report.build_followup_data(tmp_path)["followup"]["supplemental"]
    assert s["confirmation"]["complete_pairs"] == 0
    assert s["confirmation"]["counts"]["known"]["invalid"] == 1
    assert s["status"] == "running"


def test_historical_diagnostic_checks_its_frozen_source_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "ROOT", tmp_path)
    _batch(tmp_path / "new")
    _write(tmp_path / "old/runs.json", [])
    fingerprint = hashlib.sha256((tmp_path / "old/runs.json").read_bytes()).hexdigest()
    _write(tmp_path / "new/diagnostics/diagnostics.json", {
        "provenance": {"input_directory": "old", "source_files_sha256": {"runs.json": fingerprint}},
    })
    data = report.build_followup_data(tmp_path / "new")
    assert data["followup"]["diagnostics"]["files"]["diagnostics/diagnostics.json"]
    _write(tmp_path / "old/runs.json", [{"changed": True}])
    with pytest.raises(ValueError, match="来源指纹已改变"):
        report.render_report(tmp_path / "new")
    assert not (tmp_path / "new/index.html").exists()


def test_local_probe_rejects_invalid_denominator(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "ROOT", tmp_path)
    _batch(tmp_path)
    _write(tmp_path / "solution.json", {})
    _write(tmp_path / "diagnostics/probe.zip", {})
    _write(tmp_path / "diagnostics/local_repair_probe.json", {
        "provenance": {
            "source_solution_file": "solution.json",
            "source_solution_sha256": hashlib.sha256((tmp_path / "solution.json").read_bytes()).hexdigest(),
            "solver_snapshot_file": "probe.zip",
            "solver_snapshot_sha256": hashlib.sha256((tmp_path / "diagnostics/probe.zip").read_bytes()).hexdigest(),
        }, "aggregates_by_repair": {"regret2": {"rounds": 10, "infinite_regret_rounds": 11,
                                                   "infinite_with_multiple_tasks": 1}},
    })
    with pytest.raises(ValueError, match="频率分母或计数无效"):
        report.build_followup_data(tmp_path)


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "script":
            assert "src" not in attrs
            self.current = [attrs, ""]
            self.scripts.append(self.current)

    def handle_endtag(self, tag):
        if tag == "script":
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current[1] += data


def test_html_reuses_original_charts_and_safely_embeds_same_source_data(tmp_path):
    _batch(tmp_path, [_row(), _row("fallback", late=62)],
           [{"stage": "screen", "selected_variant": "fallback", "reason": "</script><script>bad()</script>"}])
    path = report.render_report(tmp_path)
    html = path.read_text()
    parsed = _HTML()
    parsed.feed(html)
    assert len(parsed.ids) == len(set(parsed.ids))
    assert {"original-results", "extension-results", "alignment-chart", "probe-chart", "confirmation-table"} <= set(parsed.ids)
    assert len(parsed.scripts) == 3
    embedded = json.loads(next(text for attrs, text in parsed.scripts if attrs.get("type") == "application/json"))
    assert embedded["run_count"] == 2
    assert embedded["groups"][0]["rows"][1]["late_count"] == 62
    assert "\"reason\":\"</script>" not in html
    assert "跨无人机 regret · 退化诊断与优化复测" in html
    assert "不合并" in html and "均值三元组不参与晋级" in html


def test_renderer_rejects_old_batch_kind(tmp_path):
    _batch(tmp_path)
    _write(tmp_path / "manifest.json", {"schema_version": 1, "kind": "exploratory_optimization"})
    with pytest.raises(ValueError, match="仅接受"):
        report.render_report(tmp_path)


def test_previous_entry_archive_is_original_and_never_overwritten(tmp_path, monkeypatch):
    current = tmp_path / "regret_followup_20261013"
    previous = tmp_path / "optimization_20261013"
    monkeypatch.setattr(report, "DEFAULT_OUTPUT", current)
    monkeypatch.setattr(report, "PREVIOUS_OUTPUT", previous)
    previous.mkdir()
    original = b"<!doctype html><title>Previous frozen report</title>\n"
    (previous / "index.html").write_bytes(original)
    _batch(current)
    report.render_report(current, update_previous_entry=True)
    assert (previous / "index.initial.html").read_bytes() == original
    assert (previous / "index.html").read_text().count('<base href="../regret_followup_20261013/">') == 1
    assert "<base " not in (current / "index.html").read_text()
    assert '../optimization_20261013/index.initial.html' in (current / "index.html").read_text()
    report.render_report(current, update_previous_entry=True)
    assert (previous / "index.initial.html").read_bytes() == original
    assert (previous / "index.html").read_text().count("<base ") == 1


def test_previous_entry_update_rejects_nondefault_output_before_writing(tmp_path):
    _batch(tmp_path)
    with pytest.raises(ValueError, match="仅允许默认"):
        report.render_report(tmp_path, update_previous_entry=True)
    assert not (tmp_path / "index.html").exists()


def test_default_render_does_not_modify_any_previous_report(tmp_path, monkeypatch):
    previous = tmp_path / "old"
    previous.mkdir()
    (previous / "index.html").write_bytes(b"original")
    monkeypatch.setattr(report, "PREVIOUS_OUTPUT", previous)
    _batch(tmp_path / "new")
    report.render_report(tmp_path / "new")
    assert (previous / "index.html").read_bytes() == b"original"
    assert not (previous / "index.initial.html").exists()


def test_shared_budget_includes_two_batches_failed_runs_aborts_and_probe(tmp_path, monkeypatch):
    _batch(tmp_path, [_row(end_to_end_seconds=241, budget_compliant=False),
                      _row("fallback", valid=False, end_to_end_seconds=7)])
    _write(tmp_path / "execution_notes.json", {"aborted_runs": [{"process_elapsed_seconds": 12.5}]})
    _batch(tmp_path / "supplemental_trial", [_row(end_to_end_seconds=239),
                                               _row("portfolio", end_to_end_seconds=3, valid=False)])
    _write(tmp_path / "supplemental_trial/execution_notes.json", {
        "aborted_runs": [{"process_elapsed_seconds": 8.25}], "summary_total_seconds": 99999,
    })
    monkeypatch.setattr(report, "_diagnostics", lambda _: {"history": None, "probe": {"elapsed_seconds": .462},
                                                         "lazy_equivalence": None, "files": {}})
    data = report.build_followup_data(tmp_path)
    budget = data["followup"]["shared_budget"]
    assert budget["known_elapsed_seconds"] == pytest.approx(241 + 7 + 12.5 + 239 + 3 + 8.25 + .462)
    assert budget["limit_seconds"] == 3600 and budget["within_budget"] is True
    assert budget["missing_time_records"] == 0
    assert data["excluded_run_count"] == 2


def test_shared_budget_missing_abort_time_is_lower_bound_not_zero_certainty(tmp_path):
    _batch(tmp_path, [_row(end_to_end_seconds=239)])
    _write(tmp_path / "execution_notes.json", {"aborted_runs": [{"process_elapsed_at_stop": "01:20"}]})
    budget = report.build_followup_data(tmp_path)["followup"]["shared_budget"]
    assert budget["known_elapsed_seconds"] == 239
    assert budget["missing_time_records"] == 1
    assert budget["within_budget"] is None


def test_shared_budget_limit_is_global_not_one_hour_for_each_batch(tmp_path):
    _batch(tmp_path, [_row(end_to_end_seconds=2000, budget_compliant=False)])
    _batch(tmp_path / "supplemental_trial", [_row(end_to_end_seconds=1800, budget_compliant=False)])
    budget = report.build_followup_data(tmp_path)["followup"]["shared_budget"]
    assert budget["known_elapsed_seconds"] == 3800
    assert budget["within_budget"] is False
    assert budget["remaining_upper_bound_seconds"] == 0


def _validation(directory, cumulative):
    _write(directory / "validation.json", {
        "manifest_sha256": hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest(),
        "runs_sha256": hashlib.sha256((directory / "runs.json").read_bytes()).hexdigest(),
        "equivalence_sha256": None, "evidence_verified": True, "cumulative_budget": cumulative,
    })


def test_budget_protocol_failure_blocks_score_only_promotion(tmp_path):
    rows = [row for seed in (report.KNOWN_SEED, *report.NEW_SEEDS)
            for row in (_row(seed=seed), _row("portfolio", seed, late=62))]
    _batch(tmp_path, rows, [{"stage": "confirm", "selected_variant": "portfolio", "passed": True}])
    _validation(tmp_path, {"cohorts": [{"directory": ".",
        "runs_sha256": hashlib.sha256((tmp_path / "runs.json").read_bytes()).hexdigest(),
        "execution_notes_sha256": None}], "local_probe_file": None, "local_probe_sha256": None,
        "budget_compliant": False})
    f = report.build_followup_data(tmp_path)["followup"]
    assert f["confirmation"]["passed_from_raw_pairs"]
    assert f["status"] == "not_promoted" and "预算协议未通过" in f["message"]


def test_budget_audit_is_invalidated_by_changed_abort_records(tmp_path):
    _batch(tmp_path)
    _validation(tmp_path, {"cohorts": [{"directory": ".",
        "runs_sha256": hashlib.sha256((tmp_path / "runs.json").read_bytes()).hexdigest(),
        "execution_notes_sha256": None}], "local_probe_file": None, "local_probe_sha256": None,
        "budget_compliant": True})
    _write(tmp_path / "execution_notes.json", {"aborted_runs": [{"process_elapsed_seconds": 10}]})
    with pytest.raises(ValueError, match="共享预算审计陈旧"):
        report.build_followup_data(tmp_path)


def test_mechanism_evidence_binds_fixed_rows_but_allows_extension_append(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "ROOT", tmp_path)
    _batch(tmp_path)
    child = tmp_path / "supplemental_trial"
    rows = [_row(), _row("portfolio", late=65)]
    evidence = {}
    for row in rows:
        path = child / f"{row['variant']}.json"
        _write(path, {"saved_solution": row["variant"]})
        row.update(solution_file=path.name, solution_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
        evidence[row["variant"]] = {
            "solution_file": path.relative_to(tmp_path).as_posix(), "solution_sha256": row["solution_sha256"],
            "run_row": dict(row), "run_row_sha256": hashlib.sha256(json.dumps(
                row, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest(),
        }
    _batch(child, rows)
    _write(tmp_path / "diagnostics/followup_observations.json", {"provenance": {"evidence": evidence}})
    assert report.build_followup_data(tmp_path)["followup"]["diagnostics"]["followup_observations"]
    rows.append(_row(stage="extensions", seed=2026101341))
    _write(child / "runs.json", rows)
    assert report.build_followup_data(tmp_path)["followup"]["diagnostics"]["followup_observations"]
    rows[0]["iterations"] += 1
    _write(child / "runs.json", rows)
    with pytest.raises(ValueError, match="固定长测行"):
        report.build_followup_data(tmp_path)
