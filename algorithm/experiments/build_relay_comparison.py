#!/usr/bin/env python3
"""Combine historical and relay summaries without changing scoring semantics."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Sequence


FORMAL_STAGES = frozenset({"formal_240s", "new_formal_240s"})
RELAY_SEMANTICS = frozenset({"strict-touch", "primary-owner"})
HISTORY_REQUIRED_COLUMNS = frozenset(
    {
        "experiment_stage",
        "method",
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
    }
)
RELAY_REQUIRED_COLUMNS = frozenset(
    {
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
    }
)
OUTPUT_COLUMNS = (
    "source",
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
ALL_OUTPUT_COLUMNS = (
    "source",
    "source_branch",
    "experiment_family",
    "experiment_stage",
    "scenario",
    "task_count",
    "stop_condition",
    "comparison_group",
    "rank_within_group",
    "method",
    "variant",
    "semantics",
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
)

_STOP_CONDITIONS = {
    "constructive_n200": "constructive-only",
    "development_smoke_10s": "wall-clock 10 s",
    "development_smoke_5_iterations": "fixed 5 iterations",
    "fixed_iterations_400": "fixed 400 iterations",
    "formal_240s": "wall-clock 240 s",
    "new_formal_240s": "wall-clock 240 s",
    "over_budget_extended": "extended run (>240 s; non-formal)",
    "single_drone_n25": "method-specific single-drone run",
    "small_exact": "method-specific exact control",
    "relay_formal_240s": "wall-clock 240 s",
}


@dataclass(frozen=True, slots=True)
class ComparisonOutputs:
    strict_csv: Path
    primary_owner_csv: Path
    strict_markdown: Path
    primary_owner_markdown: Path
    all_experiments_csv: Path
    all_experiments_markdown: Path


def _read_csv(
    path: Path, required_columns: frozenset[str]
) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        present = set(reader.fieldnames or ())
        missing = sorted(required_columns - present)
        if missing:
            raise ValueError(
                f"{path} is missing required columns: {', '.join(missing)}"
            )
        return list(reader)


def _history_rows(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in _read_csv(path, HISTORY_REQUIRED_COLUMNS):
        if row.get("experiment_stage") not in FORMAL_STAGES:
            continue
        if int(row["task_count"]) != 200:
            continue
        _validate_run_counts(row, path)
        _validate_on_time_metrics(row, path)
        _validate_distance_metrics(row, path)
        rows.append(
            {
                "source": "history",
                "method": row.get("label") or row["method"],
                "semantics": "no-relay",
                "task_count": row["task_count"],
                "run_count": row["run_count"],
                "valid_run_count": row["valid_run_count"],
                "on_time_count_mean": row["on_time_count_mean"],
                "on_time_count_min": row["on_time_count_min"],
                "on_time_count_max": row["on_time_count_max"],
                "on_time_rate_mean": row["on_time_rate_mean"],
                "on_time_rate_min": row["on_time_rate_min"],
                "on_time_rate_max": row["on_time_rate_max"],
                "distance_km_mean": row["distance_km_mean"],
                "distance_km_min": row["distance_km_min"],
                "distance_km_max": row["distance_km_max"],
                "runtime_seconds_mean": "",
                "budget_seconds": 240.0,
                "relay_count_mean": 0.0,
            }
        )
    return rows


def _history_budget_seconds(experiment_stage: str) -> float | str:
    if experiment_stage in FORMAL_STAGES:
        return 240.0
    if experiment_stage == "development_smoke_10s":
        return 10.0
    return ""


def _stop_condition(experiment_stage: str, scenario: str) -> str:
    """Return an auditable grouping label without inventing unavailable runtime."""

    return _STOP_CONDITIONS.get(
        experiment_stage,
        scenario.strip() or "not recorded in historical summary",
    )


def _all_history_rows(path: Path) -> list[dict[str, object]]:
    """Preserve every configuration in the historical summary."""

    rows: list[dict[str, object]] = []
    for row in _read_csv(path, HISTORY_REQUIRED_COLUMNS):
        _validate_any_run_counts(row, path)
        _validate_on_time_metrics(row, path)
        _validate_distance_metrics(row, path)
        experiment_stage = row["experiment_stage"].strip()
        scenario = row.get("scenario", "").strip()
        rows.append(
            {
                "source": "history",
                "source_branch": row.get("source_branch", ""),
                "experiment_family": row.get("experiment_family", ""),
                "experiment_stage": experiment_stage,
                "scenario": scenario,
                "task_count": row["task_count"],
                "stop_condition": _stop_condition(experiment_stage, scenario),
                "method": row.get("label") or row["method"],
                "variant": row.get("variant", ""),
                "semantics": "no-relay",
                "run_count": row["run_count"],
                "valid_run_count": row["valid_run_count"],
                "on_time_count_mean": row["on_time_count_mean"],
                "on_time_count_min": row["on_time_count_min"],
                "on_time_count_max": row["on_time_count_max"],
                "on_time_rate_mean": row["on_time_rate_mean"],
                "on_time_rate_min": row["on_time_rate_min"],
                "on_time_rate_max": row["on_time_rate_max"],
                "distance_km_mean": row["distance_km_mean"],
                "distance_km_min": row["distance_km_min"],
                "distance_km_max": row["distance_km_max"],
                # Runtime was deliberately omitted from the consolidated
                # historical summary; keep the evidence gap explicit.
                "runtime_seconds_mean": "",
                "budget_seconds": _history_budget_seconds(experiment_stage),
            }
        )
    return rows


def _relay_rows(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in _read_csv(path, RELAY_REQUIRED_COLUMNS):
        if int(row["task_count"]) != 200:
            continue
        budget_seconds = float(row["budget_seconds"])
        if not math.isfinite(budget_seconds):
            raise ValueError(f"{path}: budget_seconds must be finite")
        if not math.isclose(budget_seconds, 240.0, abs_tol=1e-9):
            continue
        _validate_run_counts(row, path)
        _validate_on_time_metrics(row, path)
        _validate_distance_metrics(row, path)
        semantics = row["semantics"].strip()
        if semantics not in RELAY_SEMANTICS:
            raise ValueError(f"Unknown relay semantics: {semantics!r}")
        rows.append(
            {
                "source": "relay",
                "method": row["method"],
                "semantics": semantics,
                "task_count": row["task_count"],
                "run_count": row["run_count"],
                "valid_run_count": row["valid_run_count"],
                "on_time_count_mean": row["on_time_count_mean"],
                "on_time_count_min": row["on_time_count_min"],
                "on_time_count_max": row["on_time_count_max"],
                "on_time_rate_mean": row["on_time_rate_mean"],
                "on_time_rate_min": row["on_time_rate_min"],
                "on_time_rate_max": row["on_time_rate_max"],
                "distance_km_mean": row["distance_km_mean"],
                "distance_km_min": row["distance_km_min"],
                "distance_km_max": row["distance_km_max"],
                "runtime_seconds_mean": row["runtime_seconds_mean"],
                "budget_seconds": row["budget_seconds"],
                "relay_count_mean": row["relay_count_mean"],
            }
        )
    return rows


def _all_primary_owner_relay_rows(
    relay_rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Convert formal primary-owner relay rows to the all-experiment schema."""

    rows: list[dict[str, object]] = []
    for row in relay_rows:
        if row["semantics"] != "primary-owner":
            continue
        rows.append(
            {
                "source": "relay",
                "source_branch": "current relay branch",
                "experiment_family": "relay_handoff",
                "experiment_stage": "relay_formal_240s",
                "scenario": "equal_wall_clock_240s",
                "task_count": row["task_count"],
                "stop_condition": _STOP_CONDITIONS["relay_formal_240s"],
                "method": row["method"],
                "variant": row["method"],
                "semantics": row["semantics"],
                "run_count": row["run_count"],
                "valid_run_count": row["valid_run_count"],
                "on_time_count_mean": row["on_time_count_mean"],
                "on_time_count_min": row["on_time_count_min"],
                "on_time_count_max": row["on_time_count_max"],
                "on_time_rate_mean": row["on_time_rate_mean"],
                "on_time_rate_min": row["on_time_rate_min"],
                "on_time_rate_max": row["on_time_rate_max"],
                "distance_km_mean": row["distance_km_mean"],
                "distance_km_min": row["distance_km_min"],
                "distance_km_max": row["distance_km_max"],
                "runtime_seconds_mean": row["runtime_seconds_mean"],
                "budget_seconds": row["budget_seconds"],
            }
        )
    return rows


def _validate_on_time_metrics(row: dict[str, str], path: Path) -> None:
    task_count = int(row["task_count"])
    if task_count <= 0:
        raise ValueError(f"{path}: task_count must be positive")
    for suffix in ("mean", "min", "max"):
        count_field = f"on_time_count_{suffix}"
        rate_field = f"on_time_rate_{suffix}"
        count = float(row[count_field])
        rate = float(row[rate_field])
        if not math.isfinite(count) or not math.isfinite(rate):
            raise ValueError(f"{path}: {count_field} and {rate_field} must be finite")
        if abs(rate - count / task_count) > 1e-9:
            raise ValueError(
                f"{path}: {rate_field} does not match {count_field}/task_count"
            )


def _validate_run_counts(row: dict[str, str], path: Path) -> None:
    _validate_any_run_counts(row, path)
    run_count = int(row["run_count"])
    valid_run_count = int(row["valid_run_count"])
    if valid_run_count != run_count:
        raise ValueError(f"{path}: all formal runs must be valid")


def _validate_any_run_counts(row: dict[str, str], path: Path) -> None:
    run_count = int(row["run_count"])
    valid_run_count = int(row["valid_run_count"])
    if run_count <= 0:
        raise ValueError(f"{path}: run_count must be positive")
    if valid_run_count < 0 or valid_run_count > run_count:
        raise ValueError(
            f"{path}: valid_run_count must be between zero and run_count"
        )


def _validate_distance_metrics(row: dict[str, str], path: Path) -> None:
    for suffix in ("mean", "min", "max"):
        field = f"distance_km_{suffix}"
        distance = float(row[field])
        if not math.isfinite(distance):
            raise ValueError(f"{path}: {field} must be finite")


def _write_csv(
    path: Path,
    rows: list[dict[str, object]],
    columns: tuple[str, ...] = OUTPUT_COLUMNS,
) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=columns,
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(rows)


def _official_order(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Rank only by the two direct metrics stated in the problem."""

    return sorted(
        rows,
        key=lambda row: (
            -float(row["on_time_count_mean"]),
            float(row["distance_km_mean"]),
            str(row["method"]),
        ),
    )


def _grouped_official_order(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Rank independently within stage/task-count/stopping-condition groups."""

    grouped: dict[tuple[str, int, str], list[dict[str, object]]] = {}
    for row in rows:
        key = (
            str(row["experiment_stage"]),
            int(row["task_count"]),
            str(row["stop_condition"]),
        )
        grouped.setdefault(key, []).append(row)

    ordered: list[dict[str, object]] = []
    for experiment_stage, task_count, stop_condition in sorted(grouped):
        comparison_group = (
            f"{experiment_stage} | n={task_count} | {stop_condition}"
        )
        for rank, row in enumerate(_official_order(grouped[
            (experiment_stage, task_count, stop_condition)
        ]), start=1):
            ranked = dict(row)
            ranked["comparison_group"] = comparison_group
            ranked["rank_within_group"] = rank
            ordered.append(ranked)
    return ordered


def _markdown_text(
    title: str,
    note: str,
    rows: list[dict[str, object]],
) -> str:
    lines = [
        f"# {title}",
        "",
        note,
        "",
        "表格只展示并按题面评分指标排序：先按时订单数（等价准时率），再总里程。",
        "",
        "| 方法 | 语义 | 有效运行 | 按时数均值 [min, max] | 准时率均值 [min, max] | 总里程 km 均值 [min, max] |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        method = str(row["method"]).replace("|", "\\|").replace("\n", " ")
        semantics = str(row["semantics"]).replace("|", "\\|")
        lines.append(
            "| "
            + " | ".join(
                [
                    method,
                    semantics,
                    f"{int(row['valid_run_count'])}/{int(row['run_count'])}",
                    (
                        f"{float(row['on_time_count_mean']):.3f} "
                        f"[{float(row['on_time_count_min']):.0f}, "
                        f"{float(row['on_time_count_max']):.0f}]"
                    ),
                    (
                        f"{100 * float(row['on_time_rate_mean']):.3f}% "
                        f"[{100 * float(row['on_time_rate_min']):.3f}%, "
                        f"{100 * float(row['on_time_rate_max']):.3f}%]"
                    ),
                    (
                        f"{float(row['distance_km_mean']):.3f} "
                        f"[{float(row['distance_km_min']):.3f}, "
                        f"{float(row['distance_km_max']):.3f}]"
                    ),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _write_markdown(
    path: Path,
    *,
    title: str,
    note: str,
    rows: list[dict[str, object]],
) -> None:
    path.write_text(_markdown_text(title, note, rows), encoding="utf-8")


def _safe_markdown(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _markdown_group_label(comparison_group: object) -> str:
    """Keep CSV provenance exact while avoiding audit-only metric names in Markdown."""

    return _safe_markdown(
        str(comparison_group).replace(
            "over_budget_extended",
            "over_limit_extended",
        )
    )


def _all_experiments_markdown_text(
    rows: list[dict[str, object]],
    *,
    history_count: int,
    relay_count: int,
) -> str:
    lines = [
        "# 全部历史实验与新正式 Relay 对比",
        "",
        (
            f"本表逐条保留 {history_count} 个历史配置，并追加 {relay_count} 个 "
            "200 单、240 秒、primary-owner Relay 正式配置。"
        ),
        "",
        (
            "每个分组独立排名，名次均从 1 重新开始；不同 experiment_stage、"
            "任务规模或停止条件之间不得按名次直接比较。"
        ),
        (
            "表格只展示并按题面评分指标排序：先按时订单数"
            "（等价准时率），再总里程。"
        ),
        "",
    ]
    previous_group: str | None = None
    for row in rows:
        group = str(row["comparison_group"])
        if group != previous_group:
            if previous_group is not None:
                lines.append("")
            lines.extend(
                [
                    f"## {_markdown_group_label(group)}",
                    "",
                    (
                        "| 组内名次 | 来源/实验族 | 方法 | 语义 | 有效运行 | "
                        "按时数均值 [min, max] | 准时率均值 [min, max] | "
                        "总里程 km 均值 [min, max] |"
                    ),
                    (
                        "|---:|---|---|---|---:|---:|---:|---:|"
                    ),
                ]
            )
            previous_group = group
        source_family = _safe_markdown(
            f"{row['source']} / {row['experiment_family'] or '—'}"
        )
        lines.append(
            "| "
            + " | ".join(
                [
                    str(int(row["rank_within_group"])),
                    source_family,
                    _safe_markdown(row["method"]),
                    _safe_markdown(row["semantics"]),
                    f"{int(row['valid_run_count'])}/{int(row['run_count'])}",
                    (
                        f"{float(row['on_time_count_mean']):.3f} "
                        f"[{float(row['on_time_count_min']):.0f}, "
                        f"{float(row['on_time_count_max']):.0f}]"
                    ),
                    (
                        f"{100 * float(row['on_time_rate_mean']):.3f}% "
                        f"[{100 * float(row['on_time_rate_min']):.3f}%, "
                        f"{100 * float(row['on_time_rate_max']):.3f}%]"
                    ),
                    (
                        f"{float(row['distance_km_mean']):.3f} "
                        f"[{float(row['distance_km_min']):.3f}, "
                        f"{float(row['distance_km_max']):.3f}]"
                    ),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def build_relay_comparison(
    history_summary: Path | str,
    relay_summary: Path | str,
    output_dir: Path | str,
) -> ComparisonOutputs:
    """Build the official strict/no-relay comparison artifacts."""

    history_path = Path(history_summary)
    relay_path = Path(relay_summary)
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    historical = _history_rows(history_path)
    all_historical = _all_history_rows(history_path)
    relay = _relay_rows(relay_path)
    strict_csv = destination / "strict_no_relay_comparison.csv"
    primary_owner_csv = destination / "primary_owner_extension_comparison.csv"
    strict_rows = _official_order(
        historical + [row for row in relay if row["semantics"] == "strict-touch"]
    )
    primary_owner_rows = _official_order(
        historical + [row for row in relay if row["semantics"] == "primary-owner"]
    )
    strict_markdown = destination / "strict_no_relay_comparison.md"
    primary_owner_markdown = destination / "primary_owner_extension_comparison.md"
    all_experiments_csv = destination / "all_experiments_comparison.csv"
    all_experiments_markdown = destination / "all_experiments_comparison.md"
    _write_csv(strict_csv, strict_rows)
    _write_csv(primary_owner_csv, primary_owner_rows)
    _write_markdown(
        strict_markdown,
        title="官方 strict-touch / no-relay 对比",
        note="本表仅包含无接力历史结果与 strict-touch 接力结果。",
        rows=strict_rows,
    )
    _write_markdown(
        primary_owner_markdown,
        title="primary-owner 题意扩展对比",
        note=(
            "primary-owner 是题意扩展，不能冒充官方 strict-touch 结论；"
            "no-relay 历史结果仅作为参照。"
        ),
        rows=primary_owner_rows,
    )
    all_relay = _all_primary_owner_relay_rows(relay)
    all_rows = _grouped_official_order(all_historical + all_relay)
    _write_csv(all_experiments_csv, all_rows, ALL_OUTPUT_COLUMNS)
    all_experiments_markdown.write_text(
        _all_experiments_markdown_text(
            all_rows,
            history_count=len(all_historical),
            relay_count=len(all_relay),
        ),
        encoding="utf-8",
    )
    return ComparisonOutputs(
        strict_csv=strict_csv,
        primary_owner_csv=primary_owner_csv,
        strict_markdown=strict_markdown,
        primary_owner_markdown=primary_owner_markdown,
        all_experiments_csv=all_experiments_csv,
        all_experiments_markdown=all_experiments_markdown,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build separate strict/no-relay and primary-owner comparison tables, "
            "plus a stage-grouped all-experiment table, from historical and "
            "relay summary CSV files."
        )
    )
    parser.add_argument(
        "--history-summary",
        type=Path,
        required=True,
        help="Cross-branch historical summary.csv",
    )
    parser.add_argument(
        "--relay-summary",
        type=Path,
        required=True,
        help="Formal relay_summary.csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for the three CSV and three Markdown outputs",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    outputs = build_relay_comparison(
        args.history_summary,
        args.relay_summary,
        args.output_dir,
    )
    for path in (
        outputs.strict_csv,
        outputs.strict_markdown,
        outputs.primary_owner_csv,
        outputs.primary_owner_markdown,
        outputs.all_experiments_csv,
        outputs.all_experiments_markdown,
    ):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
