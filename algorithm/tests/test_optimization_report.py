"""Data and offline-output regressions for the optimization report."""

from copy import deepcopy
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path

import pytest

from algorithm.experiments.render_optimization_report import build_report_data, main, render_report


def _row(variant="baseline", seed=1, *, stage="screen", budget=10.0, **changes):
    row = {
        "stage": stage, "variant": variant, "case_id": "official_n25_d1",
        "scenario": "pure_direct", "seed": seed, "task_count": 25,
        "drone_count": 1, "budget_seconds": budget,
        "end_to_end_seconds": budget - .1, "construction_seconds": .1,
        "search_seconds": budget - .3, "validation_seconds": .05,
        "serialization_seconds": .05, "budget_compliant": True, "valid": True,
        "late_count": 4, "total_lateness_min": 31.5, "distance_km": 82.3,
        "on_time_count": 21, "on_time_rate": 21 / 25,
        "iterations": 200, "best_trajectory": [[0, .1, 5, 40, 80], [200, 8, 4, 31.5, 82.3]],
        "solution_file": f"run_solutions/{variant}-{seed}.json",
        "solution_sha256": "a" * 64, "initial_routes_sha256": "b" * 64,
        "source_sha256": "c" * 64,
    }
    row.update(changes)
    if "late_count" in changes:
        row["on_time_count"] = row["task_count"] - row["late_count"]
        row["on_time_rate"] = row["on_time_count"] / row["task_count"]
    return row


def _write(directory: Path, rows, *, manifest=None, decisions=None, validation=None):
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"baseline_commit": "d" * 40, "input_sha256": "e" * 64,
               "source_snapshots": {"baseline": {"sha256": "c" * 64, "path": "source/baseline", "config": {}}},
               "seeds": [1, 2], "protocol": "paired_exploratory"}
    payload.update(manifest or {})
    (directory / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")
    (directory / "runs.json").write_text(json.dumps(rows), encoding="utf-8")
    for filename, value in (("decisions.json", decisions), ("validation.json", validation)):
        if value is not None:
            if filename == "validation.json":
                value = {
                    **value,
                    "manifest_sha256": hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest(),
                    "runs_sha256": hashlib.sha256((directory / "runs.json").read_bytes()).hexdigest(),
                    "equivalence_sha256": None,
                }
            (directory / filename).write_text(json.dumps(value), encoding="utf-8")


def test_pairing_uses_strict_lexicographic_scores_not_distance_or_averages(tmp_path):
    _write(tmp_path, [
        _row(), _row("candidate", late_count=3, total_lateness_min=100, distance_km=200),
        _row(seed=2), _row("candidate", seed=2, total_lateness_min=32, distance_km=20),
        _row(seed=3), _row("candidate", seed=3),
    ])
    group = build_report_data(tmp_path)["groups"][0]
    assert group["pair_counts"]["candidate"] == {"win": 1, "tie": 1, "loss": 1, "missing": 0, "invalid": 0}
    assert [pair["decisive_metric"] for pair in group["pairs"]] == ["late_count", "total_lateness_min", None]
    assert group["pairs"][0]["deltas"]["distance_km"] == pytest.approx(117.7)


def test_budgets_stages_and_scenarios_are_never_pooled(tmp_path):
    rows = [
        _row(), _row("candidate", late_count=3),
        _row(stage="final", budget=120), _row("candidate", stage="final", budget=120, late_count=2),
        _row(stage="final", budget=240, scenario="direct_relay"),
        _row("candidate", stage="final", budget=240, scenario="direct_relay", late_count=1),
        _row(stage="final", budget=60), _row("candidate", stage="final", budget=60, late_count=4),
    ]
    _write(tmp_path, rows)
    report = build_report_data(tmp_path)
    assert len(report["groups"]) == 4
    assert all(group["summary"]["candidate"]["late_count"]["n"] == 1 for group in report["groups"])
    assert len(report["families"]) == 2
    assert [family["original"] for family in report["families"]] == [True, False]
    by_id = {group["id"]: group for group in report["groups"]}
    latest = by_id[report["families"][0]["default_group_id"]]
    assert (latest["stage"], latest["budget_seconds"]) == ("final", 60)


def test_invalid_and_over_budget_rows_stay_visible_but_are_not_statistics(tmp_path):
    rows = [
        _row(), _row("candidate", late_count=3),
        _row(seed=2), _row("candidate", seed=2, late_count=1, end_to_end_seconds=10.01),
        _row(seed=3), _row("candidate", seed=3, valid=False),
    ]
    _write(tmp_path, rows)
    report = build_report_data(tmp_path)
    group = report["groups"][0]
    assert report["excluded_run_count"] == 2
    assert len(group["rows"]) == 6
    assert group["summary"]["candidate"]["late_count"] == {"n": 1, "mean": 3, "sd": None, "min": 3, "max": 3}
    assert group["pair_counts"]["candidate"]["invalid"] == 2
    assert "实际端到端耗时超过预算" in group["rows"][3]["report_issues"]


def test_missing_seed_is_never_paired_with_another_seed(tmp_path):
    _write(tmp_path, [_row(seed=1), _row("candidate", seed=2)])
    group = build_report_data(tmp_path)["groups"][0]
    assert [pair["status"] for pair in group["pairs"]] == ["missing", "missing"]
    assert group["pair_counts"]["candidate"]["win"] == 0


@pytest.mark.parametrize("candidate_hash", [None, "", "different"])
def test_initial_routes_must_be_present_and_identical_before_pairing(tmp_path, candidate_hash):
    _write(tmp_path, [_row(), _row("candidate", initial_routes_sha256=candidate_hash, late_count=1)])
    group = build_report_data(tmp_path)["groups"][0]
    assert group["pair_counts"]["candidate"]["invalid"] == 1
    assert group["pair_counts"]["candidate"]["win"] == 0
    assert "初始路线指纹" in group["pairs"][0]["reason"]


def test_duplicate_run_is_rejected_instead_of_selecting_the_best(tmp_path):
    _write(tmp_path, [_row(), _row(distance_km=1)])
    with pytest.raises(ValueError, match="重复运行记录"):
        build_report_data(tmp_path)


def test_means_and_sample_sd_are_computed_from_unrounded_observations(tmp_path):
    _write(tmp_path, [_row(distance_km=1.001), _row(seed=2, distance_km=1.003),
                     _row("candidate", distance_km=1.001), _row("candidate", seed=2, distance_km=1.003)])
    stats = build_report_data(tmp_path)["groups"][0]["summary"]["baseline"]["distance_km"]
    assert stats["mean"] == pytest.approx(1.002)
    assert stats["sd"] == pytest.approx(0.002 / 2 ** .5)
    assert stats["n"] == 2


def test_incomplete_confirmation_uses_only_common_seeds_without_dropping_orphan(tmp_path):
    rows = [
        _row(stage="confirm", budget=120),
        _row("candidate", stage="confirm", budget=120, late_count=3),
        _row("candidate", seed=2, stage="confirm", budget=120, late_count=1),
    ]
    _write(tmp_path, rows, manifest={"seeds": {"confirm": [1, 2, 3]}})
    report = build_report_data(tmp_path)
    group = report["groups"][0]
    assert report["run_count"] == report["usable_run_count"] == 3
    assert group["comparison_seeds"] == [1]
    assert group["planned_seed_count"] == 3
    assert group["unpaired_valid_count"] == 1
    assert group["summary"]["baseline"]["late_count"]["n"] == 1
    assert group["summary"]["candidate"]["late_count"] == {"n": 1, "mean": 3, "sd": None, "min": 3, "max": 3}
    assert group["rows"][-1]["seed"] == 2
    assert group["rows"][-1]["report_usable"] is True
    assert group["rows"][-1]["report_in_comparison"] is False
    assert group["pair_counts"]["candidate"]["missing"] == 1


@pytest.mark.parametrize("third_variant_change", [
    {"seed": 2}, {"valid": False}, {"initial_routes_sha256": "changed"},
])
def test_summary_intersection_requires_every_variant_valid_and_same_initial(tmp_path, third_variant_change):
    _write(tmp_path, [_row(), _row("candidate"), _row("third", **third_variant_change)])
    group = build_report_data(tmp_path)["groups"][0]
    assert group["comparison_seeds"] == []
    assert all(summary["late_count"]["n"] == 0 for summary in group["summary"].values())
    assert len(group["rows"]) == 3


def test_aborted_execution_notes_are_visible_but_never_counted_as_results(tmp_path):
    _write(tmp_path, [_row(), _row("candidate")])
    notes = {"early_stop": True, "aborted_runs": [{"stage": "confirm", "case_id": "single25",
             "variant": "baseline", "seed": 2, "status": "aborted_no_result", "reason": "门槛已无法恢复"}]}
    (tmp_path / "execution_notes.json").write_text(json.dumps(notes), encoding="utf-8")
    report = build_report_data(tmp_path)
    assert report["run_count"] == 2
    assert report["execution_notes"] == notes
    assert "execution_notes.json" in report["source_file_sha256"]
    parser = _PageParser()
    parser.feed(render_report(tmp_path).read_text(encoding="utf-8"))
    assert json.loads(parser.data)["execution_notes"]["early_stop"] is True


def test_inconsistent_on_time_rate_is_visible_and_excluded(tmp_path):
    _write(tmp_path, [_row(on_time_rate=.99)])
    report = build_report_data(tmp_path)
    assert report["usable_run_count"] == 0
    assert "准时率与任务数不一致" in report["groups"][0]["rows"][0]["report_issues"]


def test_empty_runs_and_missing_optional_records_are_not_fabricated(tmp_path):
    _write(tmp_path, [])
    report = build_report_data(tmp_path)
    assert report["groups"] == report["families"] == []
    assert report["decisions"] is report["validation"] is None
    assert report["usable_run_count"] == report["run_count"] == 0
    assert render_report(tmp_path).is_file()


@pytest.mark.parametrize("filename", ["runs.json", "manifest.json", "equivalence.json"])
def test_report_rejects_modified_inputs_after_independent_audit(tmp_path, filename):
    _write(tmp_path, [_row()], validation={"evidence_verified": True})
    equivalence = tmp_path / "equivalence.json"
    equivalence.write_text(json.dumps({"observations": [], "passed": False}), encoding="utf-8")
    audit_path = tmp_path / "validation.json"
    audit = json.loads(audit_path.read_text())
    audit["equivalence_sha256"] = hashlib.sha256(equivalence.read_bytes()).hexdigest()
    audit_path.write_text(json.dumps(audit), encoding="utf-8")
    report_path = render_report(tmp_path)
    previous_report = report_path.read_bytes()
    changed = tmp_path / filename
    changed.write_bytes(changed.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="审计记录陈旧或不完整"):
        render_report(tmp_path)
    assert report_path.read_bytes() == previous_report


@pytest.mark.parametrize("field", ["manifest_sha256", "runs_sha256", "equivalence_sha256"])
def test_audit_must_include_all_required_source_fingerprints(tmp_path, field):
    _write(tmp_path, [_row()], validation={"evidence_verified": True})
    path = tmp_path / "validation.json"
    audit = json.loads(path.read_text())
    del audit[field]
    path.write_text(json.dumps(audit), encoding="utf-8")
    with pytest.raises(ValueError, match=field):
        build_report_data(tmp_path)


def test_report_rejects_missing_previously_audited_equivalence_file(tmp_path):
    _write(tmp_path, [_row()], validation={"evidence_verified": True})
    path = tmp_path / "validation.json"
    audit = json.loads(path.read_text())
    audit["equivalence_sha256"] = "f" * 64
    path.write_text(json.dumps(audit), encoding="utf-8")
    with pytest.raises(ValueError, match="equivalence_sha256"):
        build_report_data(tmp_path)


def test_existing_null_audit_is_not_silently_treated_as_an_unaudited_preview(tmp_path):
    _write(tmp_path, [_row()])
    (tmp_path / "validation.json").write_text("null", encoding="utf-8")
    with pytest.raises(ValueError, match="validation 必须为对象"):
        build_report_data(tmp_path)


class _PageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
        self.scripts = []
        self.resources = []
        self.in_data = False
        self.data = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if tag == "script":
            self.scripts.append(attrs)
            self.in_data = attrs.get("id") == "optimization-data"
        if tag in {"script", "link", "img", "iframe"}:
            self.resources.extend(attrs.get(key) for key in ("src", "href") if attrs.get(key))

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_data = False

    def handle_data(self, text):
        if self.in_data:
            self.data += text


def test_html_is_offline_parseable_and_keeps_embedded_data_as_data(tmp_path):
    malicious = '</script><script src="https://example.invalid/attack.js">alert(1)</script>'
    decisions = [{"stage": "final", "status": "skipped", "reason": malicious}]
    rows = [_row(), _row("candidate", late_count=2)]
    _write(tmp_path, rows, decisions=decisions, validation={"status": "failed", "checked": 1})
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    target = render_report(tmp_path)
    parser = _PageParser()
    parser.feed(target.read_text(encoding="utf-8"))
    embedded = json.loads(parser.data)
    assert embedded["decisions"] == decisions
    assert embedded["validation"]["status"] == "failed"
    assert len(parser.scripts) == 2
    assert parser.resources == []
    assert len(parser.ids) == len(set(parser.ids))
    assert embedded["groups"][0]["rows"][1]["distance_km"] == rows[1]["distance_km"]
    assert embedded["groups"][0]["pair_counts"]["candidate"]["win"] == 1
    assert before == {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    assert set(embedded["source_file_sha256"]) == set(before)


def test_cli_custom_baseline_and_stage_order(tmp_path, capsys):
    _write(tmp_path, [
        _row("old", stage="final"), _row("new", stage="final"),
        _row("old", stage="screen"), _row("new", stage="screen"),
    ], manifest={"baseline_variant": "old", "stage_order": ["screen", "final"]})
    assert main(["--output-dir", str(tmp_path)]) == 0
    assert str(tmp_path / "index.html") in capsys.readouterr().out
    report = build_report_data(tmp_path)
    assert report["families"][0]["default_group_id"] == "group-0"
    assert report["groups"][0]["pairs"][0]["variant"] == "new"


def test_fixed_iteration_equivalence_is_separate_and_preserves_failed_gate(tmp_path):
    _write(tmp_path, [_row()], decisions=[{"stage": "confirm", "skipped": True, "passed": False, "reason": "中测未晋级"}])
    source = {
        "observations": [{"case_id": "single25", "seed": 1, "equivalent": True,
                          "baseline_seconds": 10, "fast_seconds": 9.5,
                          "baseline": {"iterations": 200, "best_trajectory": [[0, 0, 1, 1, 1]]},
                          "fast": {"iterations": 200}}],
        "median_runtime_reduction": {"single25": .05},
        "all_equivalent": True, "passed": False, "elapsed_seconds": 19.5,
    }
    (tmp_path / "equivalence.json").write_text(json.dumps(source), encoding="utf-8")
    report = build_report_data(tmp_path)
    assert report["run_count"] == 1
    assert report["equivalence"]["all_equivalent"] is True
    assert report["equivalence"]["passed"] is False
    observation = report["equivalence"]["observations"][0]
    assert observation["runtime_reduction"] == pytest.approx(.05)
    assert observation["baseline_iterations"] == observation["fast_iterations"] == 200
    assert "baseline" not in observation  # No duplicate trajectories in the page.
    assert report["decisions"][0]["skipped"] is True
    assert "equivalence.json" in report["source_file_sha256"]
    assert "固定迭代核验" in render_report(tmp_path).read_text(encoding="utf-8")


@pytest.mark.parametrize("change", [{"budget_seconds": 0}, {"budget_seconds": float("nan")}, {"task_count": 0}, {"drone_count": True}])
def test_bad_grouping_values_raise_explicit_errors(tmp_path, change):
    row = deepcopy(_row())
    row.update(change)
    _write(tmp_path, [row])
    with pytest.raises(ValueError):
        build_report_data(tmp_path)
