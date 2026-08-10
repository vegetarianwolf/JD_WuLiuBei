from __future__ import annotations

import csv
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "experiments"
    / "build_relay_comparison.py"
)
SPEC = importlib.util.spec_from_file_location("build_relay_comparison", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


HISTORY_COLUMNS = (
    "experiment_stage",
    "method",
    "label",
    "task_count",
    "run_count",
    "valid_run_count",
    "on_time_count_mean",
    "on_time_count_min",
    "on_time_count_max",
    "on_time_rate_mean",
    "on_time_rate_min",
    "on_time_rate_max",
    "distance_km_mean",
    "distance_km_min",
    "distance_km_max",
)

RELAY_COLUMNS = (
    "method",
    "semantics",
    "task_count",
    "run_count",
    "valid_run_count",
    "on_time_count_mean",
    "on_time_count_min",
    "on_time_count_max",
    "on_time_rate_mean",
    "on_time_rate_min",
    "on_time_rate_max",
    "distance_km_mean",
    "distance_km_min",
    "distance_km_max",
    "runtime_seconds_mean",
    "budget_seconds",
    "relay_count_mean",
)


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, object]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _history_row(method: str, *, stage: str, task_count: int) -> dict[str, object]:
    on_time_count = min(130, task_count)
    on_time_rate = on_time_count / task_count
    return {
        "experiment_stage": stage,
        "method": method,
        "label": method,
        "task_count": task_count,
        "run_count": 3,
        "valid_run_count": 3,
        "on_time_count_mean": on_time_count,
        "on_time_count_min": on_time_count,
        "on_time_count_max": on_time_count,
        "on_time_rate_mean": on_time_rate,
        "on_time_rate_min": on_time_rate,
        "on_time_rate_max": on_time_rate,
        "distance_km_mean": 640,
        "distance_km_min": 635,
        "distance_km_max": 645,
    }


def _relay_row(method: str, *, semantics: str) -> dict[str, object]:
    return {
        "method": method,
        "semantics": semantics,
        "task_count": 200,
        "run_count": 3,
        "valid_run_count": 3,
        "on_time_count_mean": 132,
        "on_time_count_min": 131,
        "on_time_count_max": 133,
        "on_time_rate_mean": 0.66,
        "on_time_rate_min": 0.655,
        "on_time_rate_max": 0.665,
        "distance_km_mean": 650,
        "distance_km_min": 645,
        "distance_km_max": 655,
        "runtime_seconds_mean": 238,
        "budget_seconds": 240,
        "relay_count_mean": 4,
    }


def test_build_filters_history_to_formal_200_task_results(tmp_path: Path) -> None:
    history = tmp_path / "history.csv"
    relay = tmp_path / "relay.csv"
    _write_csv(
        history,
        HISTORY_COLUMNS,
        [
            _history_row("included", stage="formal_240s", task_count=200),
            _history_row("wrong-stage", stage="fixed_iterations_400", task_count=200),
            _history_row("wrong-size", stage="formal_240s", task_count=25),
        ],
    )
    _write_csv(relay, RELAY_COLUMNS, [])

    outputs = MODULE.build_relay_comparison(history, relay, tmp_path / "output")

    with outputs.strict_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["method"] for row in rows] == ["included"]


def test_relay_semantics_are_split_with_shared_no_relay_reference(
    tmp_path: Path,
) -> None:
    history = tmp_path / "history.csv"
    relay = tmp_path / "relay.csv"
    _write_csv(
        history,
        HISTORY_COLUMNS,
        [_history_row("history", stage="formal_240s", task_count=200)],
    )
    _write_csv(
        relay,
        RELAY_COLUMNS,
        [
            _relay_row("relay-strict", semantics="strict-touch"),
            _relay_row("relay-extension", semantics="primary-owner"),
        ],
    )

    outputs = MODULE.build_relay_comparison(history, relay, tmp_path / "output")

    with outputs.strict_csv.open(newline="", encoding="utf-8") as handle:
        strict_rows = list(csv.DictReader(handle))
    with outputs.primary_owner_csv.open(newline="", encoding="utf-8") as handle:
        extension_rows = list(csv.DictReader(handle))
    assert {(row["method"], row["semantics"]) for row in strict_rows} == {
        ("history", "no-relay"),
        ("relay-strict", "strict-touch"),
    }
    assert {(row["method"], row["semantics"]) for row in extension_rows} == {
        ("history", "no-relay"),
        ("relay-extension", "primary-owner"),
    }


def test_official_ranking_uses_on_time_then_distance_and_excludes_lateness(
    tmp_path: Path,
) -> None:
    history = tmp_path / "history.csv"
    relay = tmp_path / "relay.csv"
    historical = _history_row("history", stage="formal_240s", task_count=200)
    historical.update(
        on_time_count_mean=132,
        on_time_rate_mean=0.66,
        distance_km_mean=620,
    )
    more_on_time = _relay_row("more-on-time", semantics="strict-touch")
    more_on_time.update(
        on_time_count_mean=133,
        on_time_rate_mean=0.665,
        distance_km_mean=900,
        runtime_seconds_mean=999,
        total_lateness_min=999999,
    )
    shorter_distance = _relay_row("shorter-distance", semantics="strict-touch")
    shorter_distance.update(
        on_time_count_mean=132,
        on_time_rate_mean=0.66,
        distance_km_mean=610,
        runtime_seconds_mean=1,
        total_lateness_min=0,
    )
    _write_csv(history, HISTORY_COLUMNS, [historical])
    _write_csv(
        relay,
        RELAY_COLUMNS + ("total_lateness_min",),
        [more_on_time, shorter_distance],
    )

    outputs = MODULE.build_relay_comparison(history, relay, tmp_path / "output")

    with outputs.strict_csv.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        assert "total_lateness_min" not in (reader.fieldnames or ())
    assert [row["method"] for row in rows] == [
        "more-on-time",
        "shorter-distance",
        "history",
    ]


def test_unknown_relay_semantics_is_rejected(tmp_path: Path) -> None:
    history = tmp_path / "history.csv"
    relay = tmp_path / "relay.csv"
    _write_csv(history, HISTORY_COLUMNS, [])
    _write_csv(
        relay,
        RELAY_COLUMNS,
        [_relay_row("ambiguous", semantics="sometimes-strict")],
    )

    with pytest.raises(ValueError, match="Unknown relay semantics"):
        MODULE.build_relay_comparison(history, relay, tmp_path / "output")


def test_missing_required_relay_column_is_rejected(tmp_path: Path) -> None:
    history = tmp_path / "history.csv"
    relay = tmp_path / "relay.csv"
    _write_csv(history, HISTORY_COLUMNS, [])
    columns = tuple(
        column for column in RELAY_COLUMNS if column != "runtime_seconds_mean"
    )
    row = _relay_row("relay", semantics="strict-touch")
    row.pop("runtime_seconds_mean")
    _write_csv(relay, columns, [row])

    with pytest.raises(ValueError, match="runtime_seconds_mean"):
        MODULE.build_relay_comparison(history, relay, tmp_path / "output")


def test_on_time_rate_is_cross_checked_against_count(tmp_path: Path) -> None:
    history = tmp_path / "history.csv"
    relay = tmp_path / "relay.csv"
    _write_csv(history, HISTORY_COLUMNS, [])
    row = _relay_row("relay", semantics="strict-touch")
    row["on_time_rate_mean"] = 0.5
    _write_csv(relay, RELAY_COLUMNS, [row])

    with pytest.raises(ValueError, match="on_time_rate_mean"):
        MODULE.build_relay_comparison(history, relay, tmp_path / "output")


def test_relay_comparison_excludes_non_240_second_runs(tmp_path: Path) -> None:
    history = tmp_path / "history.csv"
    relay = tmp_path / "relay.csv"
    _write_csv(history, HISTORY_COLUMNS, [])
    formal = _relay_row("formal", semantics="strict-touch")
    smoke = _relay_row("smoke", semantics="strict-touch")
    smoke["budget_seconds"] = 10
    _write_csv(relay, RELAY_COLUMNS, [formal, smoke])

    outputs = MODULE.build_relay_comparison(history, relay, tmp_path / "output")

    with outputs.strict_csv.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["method"] for row in rows] == ["formal"]


def test_formal_comparison_rejects_partially_invalid_configurations(
    tmp_path: Path,
) -> None:
    history = tmp_path / "history.csv"
    relay = tmp_path / "relay.csv"
    _write_csv(history, HISTORY_COLUMNS, [])
    row = _relay_row("invalid", semantics="strict-touch")
    row["valid_run_count"] = 2
    _write_csv(relay, RELAY_COLUMNS, [row])

    with pytest.raises(ValueError, match="all formal runs must be valid"):
        MODULE.build_relay_comparison(history, relay, tmp_path / "output")


def test_non_finite_distance_is_rejected(tmp_path: Path) -> None:
    history = tmp_path / "history.csv"
    relay = tmp_path / "relay.csv"
    _write_csv(history, HISTORY_COLUMNS, [])
    row = _relay_row("relay", semantics="strict-touch")
    row["distance_km_mean"] = "NaN"
    _write_csv(relay, RELAY_COLUMNS, [row])

    with pytest.raises(ValueError, match="distance_km_mean must be finite"):
        MODULE.build_relay_comparison(history, relay, tmp_path / "output")


def test_markdown_outputs_keep_official_and_extension_results_separate(
    tmp_path: Path,
) -> None:
    history = tmp_path / "history.csv"
    relay = tmp_path / "relay.csv"
    _write_csv(
        history,
        HISTORY_COLUMNS,
        [_history_row("history", stage="formal_240s", task_count=200)],
    )
    _write_csv(
        relay,
        RELAY_COLUMNS,
        [
            _relay_row("relay-strict", semantics="strict-touch"),
            _relay_row("relay-extension", semantics="primary-owner"),
        ],
    )

    outputs = MODULE.build_relay_comparison(history, relay, tmp_path / "output")

    strict = outputs.strict_markdown.read_text(encoding="utf-8")
    extension = outputs.primary_owner_markdown.read_text(encoding="utf-8")
    assert "官方 strict-touch / no-relay" in strict
    assert "relay-strict" in strict
    assert "relay-extension" not in strict
    assert "primary-owner 题意扩展" in extension
    assert "relay-extension" in extension
    assert "relay-strict" not in extension
    markdown = (strict + extension).lower()
    for non_scoring_metric in (
        "runtime",
        "budget",
        "relay_count",
        "total_lateness",
        "运行时间",
        "预算 / s",
        "接力数均值",
    ):
        assert non_scoring_metric not in markdown


def test_all_experiments_output_keeps_every_history_configuration_and_groups_ranks(
    tmp_path: Path,
) -> None:
    history = tmp_path / "history.csv"
    relay = tmp_path / "relay.csv"
    historical_rows: list[dict[str, object]] = []
    for index in range(58):
        task_count = 200 if index < 40 else 25
        row = _history_row(
            f"history-{index:02d}",
            stage=(
                "over_budget_extended"
                if index == 57
                else ("formal_240s" if index < 20 else "fixed_iterations_400")
            ),
            task_count=task_count,
        )
        on_time_count = min(130, task_count)
        row.update(
            scenario=(
                "equal_wall_clock_240s"
                if index < 20
                else "equal_iterations_400"
            ),
            experiment_family=f"family-{index % 4}",
            source_branch=f"branch-{index % 3}",
            variant=f"variant-{index}",
            on_time_count_mean=on_time_count,
            on_time_count_min=on_time_count,
            on_time_count_max=on_time_count,
            on_time_rate_mean=on_time_count / task_count,
            on_time_rate_min=on_time_count / task_count,
            on_time_rate_max=on_time_count / task_count,
        )
        historical_rows.append(row)
    history_columns = HISTORY_COLUMNS + (
        "scenario",
        "experiment_family",
        "source_branch",
        "variant",
    )
    _write_csv(history, history_columns, historical_rows)

    relay_rows = [
        _relay_row(f"relay-{index}", semantics="primary-owner")
        for index in range(3)
    ]
    excluded_strict = _relay_row("strict-audit", semantics="strict-touch")
    excluded_smoke = _relay_row("relay-smoke", semantics="primary-owner")
    excluded_smoke["budget_seconds"] = 10
    _write_csv(
        relay,
        RELAY_COLUMNS,
        relay_rows + [excluded_strict, excluded_smoke],
    )

    outputs = MODULE.build_relay_comparison(history, relay, tmp_path / "output")

    with outputs.all_experiments_csv.open(
        newline="", encoding="utf-8"
    ) as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        fields = set(reader.fieldnames or ())
    assert len(rows) == 61
    assert {
        row["method"] for row in rows if row["source"] == "history"
    } == {f"history-{index:02d}" for index in range(58)}
    assert {
        row["method"] for row in rows if row["source"] == "relay"
    } == {"relay-0", "relay-1", "relay-2"}
    assert {
        "experiment_stage",
        "task_count",
        "stop_condition",
        "comparison_group",
        "rank_within_group",
        "runtime_seconds_mean",
        "budget_seconds",
    } <= fields
    assert "total_lateness_min" not in fields

    group_ranks: dict[str, list[int]] = {}
    for row in rows:
        group_ranks.setdefault(row["comparison_group"], []).append(
            int(row["rank_within_group"])
        )
    assert len(group_ranks) >= 3
    assert all(ranks == list(range(1, len(ranks) + 1)) for ranks in group_ranks.values())

    markdown = outputs.all_experiments_markdown.read_text(encoding="utf-8")
    assert "全部历史实验与新正式 Relay" in markdown
    assert "每个分组独立排名" in markdown
    assert "history-57" in markdown
    assert "relay-2" in markdown
    assert "strict-audit" not in markdown
    assert "relay-smoke" not in markdown
    assert markdown.count("\n## ") == len(group_ranks)
    assert "total_lateness" not in markdown
    for non_scoring_metric in (
        "runtime",
        "budget",
        "relay_count",
        "运行时间",
        "预算 / s",
        "接力数",
    ):
        assert non_scoring_metric not in markdown.lower()


def test_cli_builds_all_six_comparison_artifacts(tmp_path: Path) -> None:
    history = tmp_path / "history.csv"
    relay = tmp_path / "relay.csv"
    output = tmp_path / "output"
    _write_csv(
        history,
        HISTORY_COLUMNS,
        [_history_row("history", stage="formal_240s", task_count=200)],
    )
    _write_csv(
        relay,
        RELAY_COLUMNS,
        [_relay_row("relay-strict", semantics="strict-touch")],
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--history-summary",
            str(history),
            "--relay-summary",
            str(relay),
            "--output-dir",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "strict_no_relay_comparison.csv" in completed.stdout
    assert {path.name for path in output.iterdir()} == {
        "strict_no_relay_comparison.csv",
        "strict_no_relay_comparison.md",
        "primary_owner_extension_comparison.csv",
        "primary_owner_extension_comparison.md",
        "all_experiments_comparison.csv",
        "all_experiments_comparison.md",
    }
