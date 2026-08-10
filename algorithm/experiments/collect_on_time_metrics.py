#!/usr/bin/env python3
"""Build a deduplicated, direct-metric-only cross-branch experiment ledger.

The problem statement directly prioritizes the number of on-time deliveries and,
subject to that, total distance.  This collector deliberately excludes runtime
and every lateness-magnitude field from its generated comparison datasets.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
import hashlib
import json
import math
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


DIRECT_COLUMNS = (
    "run_id",
    "source_branch",
    "snapshot_commit",
    "source_file_sha256",
    "experiment_family",
    "experiment_stage",
    "scenario",
    "method",
    "variant",
    "label",
    "seed",
    "task_count",
    "on_time_count",
    "on_time_rate",
    "distance_km",
    "valid",
    "compliant",
    "is_new_experiment",
    "experiment_role",
    "source_file",
)


@dataclass(frozen=True)
class Source:
    source_id: str
    branch: str
    root: Path
    relative_path: str
    family: str
    stage: str | Callable[[dict[str, str]], str]
    is_new: bool = False
    default_task_count: int | None = None
    include: Callable[[dict[str, str]], bool] = lambda _row: True
    note: str = ""
    expected_raw_rows: int | None = None
    expected_included_rows: int | None = None


def _bool_text(
    value: str | bool | None, *, default: bool | None = True
) -> str:
    if value in (None, ""):
        if default is None:
            raise ValueError("Required boolean field is missing")
        return str(default)
    if isinstance(value, bool):
        return str(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes"}:
        return "True"
    if normalized in {"0", "false", "no"}:
        return "False"
    raise ValueError(f"Invalid boolean value: {value!r}")


def _branch_commit(root: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short=12", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _historical_benchmark_stage(row: dict[str, str]) -> str:
    scenario = row["scenario"]
    method = row["method"]
    if scenario.startswith("exact_n"):
        return "small_exact"
    if scenario == "single_n25":
        return "single_drone_n25"
    if method.endswith("-extended"):
        return "over_budget_extended"
    if row.get("seed", "") == "":
        return "constructive_n200"
    return "fixed_iterations_400"


def _task_count(row: dict[str, str], source: Source) -> int:
    if row.get("task_count"):
        return _parse_integer(row["task_count"], "task_count")
    if source.default_task_count is not None:
        return source.default_task_count
    scenario = row.get("scenario", "")
    if "n" in scenario and scenario.rsplit("n", 1)[-1].isdigit():
        return int(scenario.rsplit("n", 1)[-1])
    raise ValueError(f"Cannot infer task count for {source.source_id}: {row}")


def _parse_finite_float(value: object, field: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite, got {value!r}")
    return number


def _parse_integer(value: object, field: str) -> int:
    number = _parse_finite_float(value, field)
    if not number.is_integer():
        raise ValueError(f"{field} must be an integer, got {value!r}")
    return int(number)


def _normalize(
    row: dict[str, str],
    source: Source,
    commit: str,
    source_file_sha256: str,
) -> dict[str, object]:
    tasks = _task_count(row, source)
    late = _parse_integer(row["late_count"], "late_count")
    on_time = tasks - late
    rate = on_time / tasks
    distance = _parse_finite_float(row["distance_km"], "distance_km")
    method = row.get("method") or row.get("variant") or "unknown"
    variant = row.get("plan_code") or row.get("variant") or row.get("method") or "unknown"
    label = (
        row.get("label")
        or row.get("variant_label")
        or row.get("method")
        or row.get("variant_name")
        or row.get("variant")
        or "unknown"
    )
    if row.get("plan_code"):
        label = f"{label} [{row['plan_code']}]"
    elif row.get("algorithm"):
        label = f"{row['algorithm']} [{method}]"
    stage = source.stage(row) if callable(source.stage) else source.stage
    seed = row.get("seed", "") or "deterministic"
    if row.get("on_time_count") not in (None, ""):
        reported_on_time = _parse_integer(row["on_time_count"], "on_time_count")
        if reported_on_time != on_time:
            raise ValueError(
                f"on_time_count mismatch in {source.source_id}: "
                f"reported={reported_on_time}, recomputed={on_time}"
            )
    identity = "|".join(
        [
            source.branch,
            source.family,
            stage,
            row.get("scenario", ""),
            method,
            variant,
            seed,
            str(tasks),
        ]
    )
    run_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    if row.get("on_time_rate") not in (None, ""):
        reported_rate = _parse_finite_float(row["on_time_rate"], "on_time_rate")
        if abs(reported_rate - rate) > 1e-9:
            raise ValueError(
                f"on_time_rate mismatch in {source.source_id}: "
                f"reported={reported_rate}, recomputed={rate}"
            )
    if not source.is_new:
        experiment_role = "existing"
    elif source.source_id == "uav_new":
        actual_treatment = _bool_text(row.get("treatment"), default=None) == "True"
        expected_treatment = method.endswith("_treatment")
        if method not in {"halns_control", "halns_treatment"}:
            raise ValueError(f"uav_new contains unexpected method: {method}")
        if actual_treatment != expected_treatment:
            raise ValueError(
                f"uav_new treatment flag contradicts method {method}: "
                f"{row.get('treatment')!r}"
            )
        experiment_role = "treatment" if actual_treatment else "control"
    else:
        experiment_role = "treatment"
    formal = stage in {"formal_240s", "new_formal_240s"}
    return {
        "run_id": run_id,
        "source_branch": source.branch,
        "snapshot_commit": commit,
        "source_file_sha256": source_file_sha256,
        "experiment_family": source.family,
        "experiment_stage": stage,
        "scenario": row.get("scenario", ""),
        "method": method,
        "variant": variant,
        "label": label,
        "seed": seed,
        "task_count": tasks,
        "on_time_count": on_time,
        "on_time_rate": rate,
        "distance_km": distance,
        "valid": _bool_text(row.get("valid"), default=None if formal else True),
        "compliant": _bool_text(
            row.get("compliant"), default=None if formal else True
        ),
        "is_new_experiment": str(source.is_new),
        "experiment_role": experiment_role,
        "source_file": source.relative_path,
    }


def _mean(values: Iterable[float]) -> float:
    return statistics.fmean(values)


def _summarize(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    keys = (
        "source_branch",
        "snapshot_commit",
        "experiment_family",
        "experiment_stage",
        "scenario",
        "method",
        "variant",
        "label",
        "task_count",
        "is_new_experiment",
        "experiment_role",
    )
    groups: dict[tuple[object, ...], list[dict[str, object]]] = {}
    for row in rows:
        groups.setdefault(tuple(row[key] for key in keys), []).append(row)
    summaries: list[dict[str, object]] = []
    for key, members in groups.items():
        item = dict(zip(keys, key, strict=True))
        item.update(
            {
                "run_count": len(members),
                "valid_run_count": sum(row["valid"] == "True" for row in members),
                "on_time_count_mean": _mean(float(row["on_time_count"]) for row in members),
                "on_time_count_min": min(int(row["on_time_count"]) for row in members),
                "on_time_count_max": max(int(row["on_time_count"]) for row in members),
                "on_time_rate_mean": _mean(float(row["on_time_rate"]) for row in members),
                "on_time_rate_min": min(float(row["on_time_rate"]) for row in members),
                "on_time_rate_max": max(float(row["on_time_rate"]) for row in members),
                "distance_km_mean": _mean(float(row["distance_km"]) for row in members),
                "distance_km_min": min(float(row["distance_km"]) for row in members),
                "distance_km_max": max(float(row["distance_km"]) for row in members),
            }
        )
        summaries.append(item)
    return sorted(
        summaries,
        key=lambda row: (
            str(row["experiment_stage"]),
            int(row["task_count"]),
            -float(row["on_time_rate_mean"]),
            float(row["distance_km_mean"]),
            str(row["label"]),
        ),
    )


def _select(
    rows: list[dict[str, object]],
    *,
    family: str,
    method: str | None = None,
    variant: str | None = None,
    stage: str | None = None,
) -> list[dict[str, object]]:
    selected = [row for row in rows if row["experiment_family"] == family]
    if method is not None:
        selected = [row for row in selected if row["method"] == method]
    if variant is not None:
        selected = [row for row in selected if row["variant"] == variant]
    if stage is not None:
        selected = [row for row in selected if row["experiment_stage"] == stage]
    return selected


def _comparison(
    branch: str,
    control: list[dict[str, object]],
    treatment: list[dict[str, object]],
) -> dict[str, object]:
    if len(control) != 3 or len(treatment) != 3:
        raise ValueError(
            f"{branch}: expected exactly three control and three treatment rows, "
            f"got {len(control)} and {len(treatment)}"
        )
    control_seeds = [str(row["seed"]) for row in control]
    treatment_seeds = [str(row["seed"]) for row in treatment]
    if len(set(control_seeds)) != len(control_seeds):
        raise ValueError(f"{branch}: duplicate control seed: {control_seeds}")
    if len(set(treatment_seeds)) != len(treatment_seeds):
        raise ValueError(f"{branch}: duplicate treatment seed: {treatment_seeds}")
    control_by_seed = {str(row["seed"]): row for row in control}
    treatment_by_seed = {str(row["seed"]): row for row in treatment}
    if control_by_seed.keys() != treatment_by_seed.keys():
        raise ValueError(
            f"{branch}: control/treatment seed sets differ: "
            f"{sorted(control_by_seed)} vs {sorted(treatment_by_seed)}"
        )
    seeds = sorted(control_by_seed)
    expected_seeds = {"2026080500", "2026080501", "2026080502"}
    if set(seeds) != expected_seeds:
        raise ValueError(f"{branch}: unexpected paired seeds: {seeds}")
    task_counts = {int(row["task_count"]) for row in control + treatment}
    scenarios = {str(row["scenario"]) for row in control + treatment}
    if task_counts != {200}:
        raise ValueError(f"{branch}: paired task counts must all be 200, got {task_counts}")
    if scenarios != {"equal_wall_clock_240s"}:
        raise ValueError(
            f"{branch}: paired scenario must be equal_wall_clock_240s, "
            f"got {sorted(scenarios)}"
        )
    old_rate = _mean(float(control_by_seed[seed]["on_time_rate"]) for seed in seeds)
    new_rate = _mean(float(treatment_by_seed[seed]["on_time_rate"]) for seed in seeds)
    old_on_time = _mean(float(control_by_seed[seed]["on_time_count"]) for seed in seeds)
    new_on_time = _mean(float(treatment_by_seed[seed]["on_time_count"]) for seed in seeds)
    old_distance = _mean(float(control_by_seed[seed]["distance_km"]) for seed in seeds)
    new_distance = _mean(float(treatment_by_seed[seed]["distance_km"]) for seed in seeds)
    return {
        "source_branch": branch,
        "control_label": control[0]["label"],
        "treatment_label": treatment[0]["label"],
        "paired_seed_count": len(seeds),
        "control_on_time_count_mean": old_on_time,
        "treatment_on_time_count_mean": new_on_time,
        "on_time_count_delta": new_on_time - old_on_time,
        "control_on_time_rate_mean": old_rate,
        "treatment_on_time_rate_mean": new_rate,
        "on_time_rate_delta_pp": (new_rate - old_rate) * 100,
        "control_distance_km_mean": old_distance,
        "treatment_distance_km_mean": new_distance,
        "distance_km_delta": new_distance - old_distance,
    }


def _write_csv(path: Path, rows: list[dict[str, object]], columns: Iterable[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(columns or (rows[0].keys() if rows else []))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


BRANCH_LABELS = {
    "codex/uav-alns-dispatch": "uav",
    "codex/alns-core-comparison": "core",
    "codex/stronger-uav-neighborhoods": "stronger",
    "feature/capacity-aware-halns": "capacity",
    "feature/adaptive-deadline-rejection-pool": "adaptive",
}

STAGE_LABELS = {
    "fixed_iterations_400": "固定 400 轮（200 单）",
    "constructive_n200": "构造法（200 单）",
    "over_budget_extended": "超 240 秒扩展实验（200 单，不纳入正式排名）",
    "small_exact": "小规模 exact 对照",
    "single_drone_n25": "单机 25 单",
    "development_smoke_5_iterations": "开发 smoke：5 轮",
    "development_smoke_10s": "开发 smoke：10 秒",
}


def _markdown_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def _format_count(value: object) -> str:
    number = float(value)
    return f"{number:.0f}" if number.is_integer() else f"{number:.3f}"


def _summary_markdown(rows: list[dict[str, object]]) -> str:
    ordered = sorted(
        rows,
        key=lambda row: (
            int(row["task_count"]),
            -float(row["on_time_count_mean"]),
            float(row["distance_km_mean"]),
            BRANCH_LABELS.get(str(row["source_branch"]), str(row["source_branch"])),
            str(row["label"]),
        ),
    )
    body: list[list[str]] = []
    for row in ordered:
        body.append(
            [
                BRANCH_LABELS.get(str(row["source_branch"]), str(row["source_branch"])),
                str(row["label"]),
                str(row["task_count"]),
                str(row["run_count"]),
                (
                    f"{_format_count(row['on_time_count_mean'])} "
                    f"[{row['on_time_count_min']}, {row['on_time_count_max']}]"
                ),
                (
                    f"{100 * float(row['on_time_rate_mean']):.3f}% "
                    f"[{100 * float(row['on_time_rate_min']):.1f}%, "
                    f"{100 * float(row['on_time_rate_max']):.1f}%]"
                ),
                (
                    f"{float(row['distance_km_mean']):.3f} "
                    f"[{float(row['distance_km_min']):.3f}, "
                    f"{float(row['distance_km_max']):.3f}]"
                ),
                {
                    "existing": "现有",
                    "control": "本轮对照",
                    "treatment": "新 treatment",
                }[str(row["experiment_role"])],
            ]
        )
    return _markdown_table(
        [
            "分支",
            "方法/变体",
            "任务数",
            "运行数",
            "按时数均值 [范围]",
            "准时率均值 [范围]",
            "总里程 km 均值 [范围]",
            "批次",
        ],
        body,
    )


def _comparison_outcome(row: dict[str, object]) -> str:
    on_time_delta = float(row["on_time_count_delta"])
    distance_delta = float(row["distance_km_delta"])
    if on_time_delta > 1e-12:
        return "第一目标改善"
    if abs(on_time_delta) <= 1e-12 and distance_delta < -1e-12:
        return "第一目标持平，第二目标改善"
    if abs(on_time_delta) <= 1e-12 and abs(distance_delta) <= 1e-12:
        return "持平"
    return "未改善"


def _write_markdown_report(
    path: Path,
    all_rows: list[dict[str, object]],
    summaries: list[dict[str, object]],
    comparisons: list[dict[str, object]],
    validation: dict[str, object],
) -> None:
    existing_formal = [
        row for row in summaries if row["experiment_stage"] == "formal_240s"
    ]
    new_formal = [
        row for row in summaries if row["experiment_stage"] == "new_formal_240s"
    ]
    new_treatments = [
        row for row in new_formal if row["experiment_role"] == "treatment"
    ]
    best_existing = min(
        existing_formal,
        key=lambda row: (
            -float(row["on_time_count_mean"]),
            float(row["distance_km_mean"]),
        ),
    )
    best_new = min(
        new_treatments,
        key=lambda row: (
            -float(row["on_time_count_mean"]),
            float(row["distance_km_mean"]),
        ),
    )
    best_formal_run = min(
        (
            row
            for row in all_rows
            if row["experiment_stage"] in {"formal_240s", "new_formal_240s"}
            and row["experiment_role"] != "control"
        ),
        key=lambda row: (-int(row["on_time_count"]), float(row["distance_km"])),
    )
    primary_improvements = sum(
        float(row["on_time_count_delta"]) > 1e-12 for row in comparisons
    )

    comparison_rows: list[list[str]] = []
    for row in comparisons:
        comparison_rows.append(
            [
                BRANCH_LABELS[str(row["source_branch"])],
                (
                    f"{_format_count(row['control_on_time_count_mean'])} / "
                    f"{100 * float(row['control_on_time_rate_mean']):.3f}% / "
                    f"{float(row['control_distance_km_mean']):.3f}"
                ),
                (
                    f"{_format_count(row['treatment_on_time_count_mean'])} / "
                    f"{100 * float(row['treatment_on_time_rate_mean']):.3f}% / "
                    f"{float(row['treatment_distance_km_mean']):.3f}"
                ),
                _format_count(row["on_time_count_delta"]),
                f"{float(row['on_time_rate_delta_pp']):+.3f}",
                f"{float(row['distance_km_delta']):+.3f}",
                _comparison_outcome(row),
            ]
        )

    formal_table = _summary_markdown(existing_formal + new_formal)
    other_sections: list[str] = []
    for stage, label in STAGE_LABELS.items():
        stage_rows = [row for row in summaries if row["experiment_stage"] == stage]
        if stage_rows:
            other_sections.append(f"### {label}\n\n{_summary_markdown(stage_rows)}")

    branch_design = _markdown_table(
        ["分支", "正式 treatment", "对照"],
        [
            ["uav", "HALNS + 延后池 + 两层直接目标", "本轮同步 HALNS control"],
            ["core", "ALNS-Core + 延后池 + 两层直接目标", "已有同 seed/240s Core"],
            ["stronger", "A7：A2 risk + 延后池 + 两层直接目标", "已有 A2"],
            ["capacity", "Capacity-aware HALNS + 延后池 + 两层直接目标", "已有 Capacity-aware"],
            ["adaptive", "Experiment 5：late-risk + 延后池 + 两层直接目标", "已有 Experiment 1"],
        ],
    )

    comparison_table = _markdown_table(
        [
            "分支",
            "对照：按时数 / 准时率 / km",
            "新：按时数 / 准时率 / km",
            "Δ按时数",
            "Δ准时率百分点",
            "Δ里程 km",
            "按题目顺序",
        ],
        comparison_rows,
    )

    report = f"""# 非 main 分支准时率优先与“牺牲订单”实验汇总

> 结论：新组合策略只在 {primary_improvements}/5 个分支提高了平均按时订单数，未形成跨分支稳定收益；因此所有分支均保持默认关闭，不建议直接替换现有正式算法。

## 技术摘要

题目文档没有给出独立分值公式或权重。它直接要求先尽可能增加按时送达任务，再在此前提下缩短总配送里程。因此本报告只比较按时订单数、准时率和总里程，不用逾期幅度、迭代次数或运行时间给算法排名。

现有代码其实早已把逾期订单数放在第一目标；在固定 200 单且全部完成时，这与最大化准时率完全等价。本轮真正新增的是：删除旧的逾期幅度 tie-break，在搜索中把已明显逾期的 removed 任务暂时延后修复，先保护其余订单的路线结构。最终仍回插全部任务，不允许永久漏单。

现有正式实验中表现最好的均值是 **{best_existing['label']}**（{BRANCH_LABELS[str(best_existing['source_branch'])]}）：平均按时 **{_format_count(best_existing['on_time_count_mean'])}/200**，准时率 **{100 * float(best_existing['on_time_rate_mean']):.3f}%**，总里程 **{float(best_existing['distance_km_mean']):.3f} km**。新 treatment 中最好的是 **{best_new['label']}**（{BRANCH_LABELS[str(best_new['source_branch'])]}）：平均按时 **{_format_count(best_new['on_time_count_mean'])}/200**，准时率 **{100 * float(best_new['on_time_rate_mean']):.3f}%**，总里程 **{float(best_new['distance_km_mean']):.3f} km**。

全部正式单次运行中，直接目标最好的一个是 **{best_formal_run['label']}**（{BRANCH_LABELS[str(best_formal_run['source_branch'])]}，seed `{best_formal_run['seed']}`）：按时 **{best_formal_run['on_time_count']}/200**，准时率 **{100 * float(best_formal_run['on_time_rate']):.1f}%**，总里程 **{float(best_formal_run['distance_km']):.3f} km**。

## 指标口径

- 第一目标：最大化按时订单数；准时率 = 按时订单数 / 任务数。
- 第二目标：仅在第一目标相同的前提下，最小化全部无人机总配送里程。
- 正式比较顺序为 `(-按时订单数, 总里程)`。
- 可行性是门槛，不是分数：每单必须取货一次、送达一次、先取后送，并满足载重、单机订单数和机队规模约束。
- 题目中的“牺牲”解释为不再优先挽救少数订单的准时性，绝不解释为不配送；所有新正式解最终延后池均为空。

## 改动设计与分支覆盖

统一 treatment 的延后池最多容纳 `floor(0.10 × N)` 个任务，只接纳“单独删除后会减少当前逾期订单数”的任务；同等收益按节省里程排序。普通 removed 任务先用各分支原生 repair 回插，延后任务最后用 Regret-2 回插。部分解从不参与接受或 best 比较，修复超时则退回上一完整 best。

{branch_design}

## 五个分支的新实验对照

下表均为 200 单、3 个相同 seed、240 秒总预算的均值；正的 Δ按时数/Δ准时率为改善，负的 Δ里程为改善。里程不能补偿第一目标下降。uav 为本轮同步 control/treatment；其余四个分支使用仓库中同 seed、同预算的既有对照，因此属于跨快照配对。

{comparison_table}

## 全部正式 200 单、240 秒实验

每行按独立实验配置汇总；方括号给出三个 seed 的直接指标范围。表内同时包含既有实验和本轮新实验。

{formal_table}

## 其他全部现有实验

不同任务规模或停止条件不得与 200 单、240 秒正式结果直接排名。以下仍全部列出，保证历史实验覆盖完整。

{chr(10).join(other_sections)}

## 数据覆盖与校验

- 去重后共 **{validation['all_run_count']}** 条独立运行、**{validation['summary_row_count']}** 条配置汇总；其中正式 240 秒运行 **{validation['formal_run_count']}** 条，本轮新增正式运行 **{validation['new_formal_run_count']}** 条（含 uav 同步 control 3 条）。
- 当前五个分支快照中的勘误 CSV 完全一致，SHA-256 为 `{validation['input_dataset_sha256']}`；历史运行是否使用该快照仍以各自已保存的结果与来源记录为准。
- 后继分支继承的相同 benchmark、Core 对比中重复的 HALNS 400 轮行、adaptive 的 partial checkpoint 以及重复 smoke 阶段均只保留一个 canonical 副本。
- 所有正式来源行都必须显式标记 valid/compliant，否则 collector 拒绝纳入；本轮新 treatment 解在提交前另做了分支级独立重算。来源 CSV 的按时数和准时率已由任务数重新计算并交叉验证。
- 稳定语义 run ID 会拒绝重复配置/seed；五组比较强制双方恰好三个相同 seed、相同场景、相同 200 单。

## 局限与稳健性

- 每个正式配置只有 3 个 seed；表中保留完整范围，但不引入题目未要求的统计量作为排名依据。
- 除 uav 外，新 treatment 与既有 control 来自不同代码快照；默认关闭路径有回归测试，但墙钟搜索仍可能受机器负载影响。
- treatment 同时改变了 tie-break 和 repair 顺序，本轮不能把效果完全归因于其中一个机制。
- 10% 是统一、保守的首轮上限，并非已调优参数；延后回插仍可能破坏先前保护的路线结构。

## 建议与下一步

1. 不把本轮 treatment 设为默认；保留开关和完整失败结果，避免回归。
2. 继续以现有 late-risk destroy 为主线：它直接扩大高风险订单附近的邻域，而不是在回插末端制造结构性代价。
3. 下一轮做 2%/5%/10% 延后比例与“仅两层目标”“仅 repair-last”“二者组合”的析因实验，仍用 3 个相同 seed、240 秒和完整订单约束。
4. 用预计最早可达时间、边际插入代价和路径冲突度定义 hopeless 概率；只有高置信不可准时且延后后能增加其他按时订单时才进入池。
5. 若最终提交需要一个单一算法，按题目顺序选择正式均值最高且全部可行的现有配置，不要用更短里程补偿更低准时率。

## 复现与明细文件

- [逐次运行直接指标](on_time_objective/all_runs.csv)
- [全部配置汇总](on_time_objective/summary.csv)
- [五分支新旧对照](on_time_objective/new_vs_control.csv)
- [来源与去重清单](on_time_objective/source_manifest.csv)
- [数据校验结果](on_time_objective/validation.json)
- [汇总脚本](../experiments/collect_on_time_metrics.py)
- [题目文档](../../命题1-低空经济场景下的物流无人机调度算法.docx)
"""
    path.write_text(report, encoding="utf-8")


def _write_artifact_snapshot(
    path: Path,
    summaries: list[dict[str, object]],
    comparisons: list[dict[str, object]],
) -> None:
    comparison_table: list[dict[str, object]] = []
    comparison_chart: list[dict[str, object]] = []
    for row in comparisons:
        branch = BRANCH_LABELS[str(row["source_branch"])]
        outcome = _comparison_outcome(row)
        comparison_table.append(
            {
                "branch": branch,
                "control_on_time_count": row["control_on_time_count_mean"],
                "treatment_on_time_count": row["treatment_on_time_count_mean"],
                "on_time_count_delta": row["on_time_count_delta"],
                "control_on_time_rate": row["control_on_time_rate_mean"],
                "treatment_on_time_rate": row["treatment_on_time_rate_mean"],
                "on_time_rate_delta_pp": row["on_time_rate_delta_pp"],
                "control_distance_km": row["control_distance_km_mean"],
                "treatment_distance_km": row["treatment_distance_km_mean"],
                "distance_km_delta": row["distance_km_delta"],
                "outcome": outcome,
                "paired_seed_count": row["paired_seed_count"],
            }
        )
        for series in ("对照", "新策略"):
            prefix = "control" if series == "对照" else "treatment"
            comparison_chart.append(
                {
                    "branch": branch,
                    "series": series,
                    "on_time_count_mean": row[f"{prefix}_on_time_count_mean"],
                    "on_time_rate": row[f"{prefix}_on_time_rate_mean"],
                    "distance_km_mean": row[f"{prefix}_distance_km_mean"],
                    "on_time_rate_delta_pp": row["on_time_rate_delta_pp"],
                    "distance_km_delta": row["distance_km_delta"],
                    "outcome": outcome,
                    "paired_seed_count": row["paired_seed_count"],
                }
            )

    formal_summary = [
        {
            "branch": BRANCH_LABELS[str(row["source_branch"])],
            "method": row["label"],
            "role": {
                "existing": "现有",
                "control": "本轮对照",
                "treatment": "新 treatment",
            }[str(row["experiment_role"])],
            "run_count": row["run_count"],
            "on_time_count_mean": row["on_time_count_mean"],
            "on_time_rate_mean": row["on_time_rate_mean"],
            "distance_km_mean": row["distance_km_mean"],
            "on_time_count_min": row["on_time_count_min"],
            "on_time_count_max": row["on_time_count_max"],
            "distance_km_min": row["distance_km_min"],
            "distance_km_max": row["distance_km_max"],
        }
        for row in summaries
        if row["experiment_stage"] in {"formal_240s", "new_formal_240s"}
    ]
    formal_summary.sort(
        key=lambda row: (
            -float(row["on_time_count_mean"]),
            float(row["distance_km_mean"]),
            str(row["branch"]),
            str(row["method"]),
        )
    )
    for rank, row in enumerate(formal_summary, start=1):
        row["official_rank"] = rank
    best_existing = next(row for row in formal_summary if row["role"] == "现有")
    new_treatments = [row for row in formal_summary if row["role"] == "新 treatment"]
    best_new = min(
        new_treatments,
        key=lambda row: (
            -float(row["on_time_count_mean"]),
            float(row["distance_km_mean"]),
        ),
    )
    headline = [
        {
            "best_existing_on_time_rate": best_existing["on_time_rate_mean"],
            "best_existing_distance_km": best_existing["distance_km_mean"],
            "best_new_on_time_rate": best_new["on_time_rate_mean"],
            "best_new_distance_km": best_new["distance_km_mean"],
        }
    ]
    snapshot = {
        "version": 1,
        "status": "ready",
        "datasets": {
            "headline": headline,
            "comparison_chart": comparison_chart,
            "comparison_table": comparison_table,
            "formal_summary": formal_summary,
        },
    }
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_artifact_manifest(
    path: Path,
    summaries: list[dict[str, object]],
    comparisons: list[dict[str, object]],
) -> None:
    title = "非 main 分支准时率优先与“牺牲订单”实验汇总"
    existing_formal = [
        row for row in summaries if row["experiment_stage"] == "formal_240s"
    ]
    new_treatments = [
        row
        for row in summaries
        if row["experiment_stage"] == "new_formal_240s"
        and row["experiment_role"] == "treatment"
    ]
    best_existing = min(
        existing_formal,
        key=lambda row: (
            -float(row["on_time_count_mean"]),
            float(row["distance_km_mean"]),
        ),
    )
    best_new = min(
        new_treatments,
        key=lambda row: (
            -float(row["on_time_count_mean"]),
            float(row["distance_km_mean"]),
        ),
    )
    improved = sum(
        float(row["on_time_count_delta"]) > 1e-12 for row in comparisons
    )
    manifest = {
        "version": 1,
        "surface": "report",
        "title": title,
        "description": "五个非 main 分支的准时率优先延后修复实验与全部历史实验汇总。",
        "cards": [
            {
                "id": "best_existing_rate",
                "dataset": "headline",
                "sourceId": "formal_summary",
                "description": "既有正式配置中按题目两层目标排序的最佳均值。",
                "metrics": [
                    {
                        "label": "既有正式最佳准时率",
                        "field": "best_existing_on_time_rate",
                        "format": "percent",
                    }
                ],
            },
            {
                "id": "best_existing_distance",
                "dataset": "headline",
                "sourceId": "formal_summary",
                "description": "既有正式最佳配置的第二目标。",
                "metrics": [
                    {
                        "label": "对应总里程 (km)",
                        "field": "best_existing_distance_km",
                        "format": "number",
                    }
                ],
            },
            {
                "id": "best_new_rate",
                "dataset": "headline",
                "sourceId": "formal_summary",
                "description": "本轮五个 treatment 中按题目两层目标排序的最佳均值。",
                "metrics": [
                    {
                        "label": "新 treatment 最佳准时率",
                        "field": "best_new_on_time_rate",
                        "format": "percent",
                    }
                ],
            },
            {
                "id": "best_new_distance",
                "dataset": "headline",
                "sourceId": "formal_summary",
                "description": "本轮最佳 treatment 的第二目标。",
                "metrics": [
                    {
                        "label": "对应总里程 (km)",
                        "field": "best_new_distance_km",
                        "format": "number",
                    }
                ],
            },
        ],
        "charts": [
            {
                "id": "branch_on_time_rate",
                "title": "五分支对照与新策略的平均准时率",
                "subtitle": f"新策略在 {improved}/5 个分支提高平均按时订单数。",
                "type": "bar",
                "dataset": "comparison_chart",
                "sourceId": "comparison",
                "encodings": {
                    "x": {"field": "branch", "type": "ordinal", "label": "分支"},
                    "y": {
                        "field": "on_time_rate",
                        "type": "quantitative",
                        "label": "平均准时率",
                        "format": "percent",
                    },
                    "color": {"field": "series", "type": "nominal", "label": "方案"},
                    "tooltip": [
                        {
                            "field": "on_time_count_mean",
                            "type": "quantitative",
                            "label": "平均按时数",
                            "format": "number",
                        },
                        {
                            "field": "distance_km_mean",
                            "type": "quantitative",
                            "label": "平均总里程",
                            "format": "number",
                            "unit": "km",
                        },
                        {"field": "outcome", "type": "text", "label": "题目顺序结论"},
                    ],
                },
            }
        ],
        "tables": [
            {
                "id": "branch_comparison",
                "title": "五分支新旧方案直接指标对照",
                "dataset": "comparison_table",
                "sourceId": "comparison",
                "density": "compact",
                "defaultSort": {"field": "treatment_on_time_rate", "direction": "desc"},
                "columns": [
                    {"field": "branch", "label": "分支", "type": "text"},
                    {
                        "field": "control_on_time_rate",
                        "label": "对照准时率",
                        "type": "percent",
                        "format": "percent",
                    },
                    {
                        "field": "treatment_on_time_rate",
                        "label": "新策略准时率",
                        "type": "percent",
                        "format": "percent",
                    },
                    {
                        "field": "on_time_rate_delta_pp",
                        "label": "准时率变化",
                        "type": "number",
                        "format": "number",
                        "unit": "百分点",
                    },
                    {
                        "field": "control_distance_km",
                        "label": "对照里程",
                        "type": "number",
                        "format": "number",
                        "unit": "km",
                    },
                    {
                        "field": "treatment_distance_km",
                        "label": "新策略里程",
                        "type": "number",
                        "format": "number",
                        "unit": "km",
                    },
                    {"field": "outcome", "label": "题目顺序结论", "type": "text"},
                ],
            },
            {
                "id": "formal_summary",
                "title": "全部正式 200 单、240 秒配置",
                "dataset": "formal_summary",
                "sourceId": "formal_summary",
                "density": "compact",
                "defaultSort": {"field": "official_rank", "direction": "asc"},
                "columns": [
                    {
                        "field": "official_rank",
                        "label": "两层目标排名",
                        "type": "number",
                    },
                    {"field": "branch", "label": "分支", "type": "text"},
                    {"field": "method", "label": "方法/变体", "type": "text"},
                    {"field": "role", "label": "批次", "type": "text"},
                    {
                        "field": "on_time_count_mean",
                        "label": "平均按时数",
                        "type": "number",
                        "format": "number",
                    },
                    {
                        "field": "on_time_rate_mean",
                        "label": "平均准时率",
                        "type": "percent",
                        "format": "percent",
                    },
                    {
                        "field": "distance_km_mean",
                        "label": "平均总里程",
                        "type": "number",
                        "format": "number",
                        "unit": "km",
                    },
                    {"field": "run_count", "label": "运行数", "type": "number"},
                ],
            },
        ],
        "sources": [
            {
                "id": "problem_statement",
                "label": "命题1题目文档",
                "path": "命题1-低空经济场景下的物流无人机调度算法.docx",
            },
            {
                "id": "comparison",
                "label": "五分支新旧对照",
                "path": "algorithm/reports/on_time_objective/new_vs_control.csv",
                "query": {
                    "id": "comparison_csv_snapshot",
                    "description": "读取五个分支的新旧直接指标均值与差值。",
                    "engine": "duckdb",
                    "language": "sql",
                    "sql": (
                        "SELECT * FROM read_csv_auto("
                        "'algorithm/reports/on_time_objective/new_vs_control.csv');"
                    ),
                    "tables_used": [
                        "algorithm/reports/on_time_objective/new_vs_control.csv"
                    ],
                    "filters": ["200 tasks", "3 matched seeds", "240-second protocol"],
                    "metric_definitions": [
                        "on_time_rate = on_time_count / 200",
                        "distance_km is total open-route delivery distance",
                    ],
                },
            },
            {
                "id": "formal_summary",
                "label": "全部实验配置汇总",
                "path": "algorithm/reports/on_time_objective/summary.csv",
                "query": {
                    "id": "formal_summary_csv_snapshot",
                    "description": "读取全部配置的直接指标汇总。",
                    "engine": "duckdb",
                    "language": "sql",
                    "sql": (
                        "SELECT * FROM read_csv_auto("
                        "'algorithm/reports/on_time_objective/summary.csv');"
                    ),
                    "tables_used": ["algorithm/reports/on_time_objective/summary.csv"],
                    "filters": ["canonical deduplicated experiment configurations"],
                    "metric_definitions": [
                        "on_time_count_mean is the mean delivered by deadline",
                        "on_time_rate_mean = on_time_count_mean / task_count",
                        "distance_km_mean is mean total open-route distance",
                    ],
                },
            },
            {
                "id": "all_runs",
                "label": "逐次运行直接指标",
                "path": "algorithm/reports/on_time_objective/all_runs.csv",
                "query": {
                    "id": "all_runs_csv_snapshot",
                    "description": "读取去重后的逐次运行直接指标。",
                    "engine": "duckdb",
                    "language": "sql",
                    "sql": (
                        "SELECT * FROM read_csv_auto("
                        "'algorithm/reports/on_time_objective/all_runs.csv');"
                    ),
                    "tables_used": ["algorithm/reports/on_time_objective/all_runs.csv"],
                    "filters": ["canonical final result rows only"],
                    "metric_definitions": [
                        "on_time_count = task_count - late_count in source data",
                        "on_time_rate = on_time_count / task_count",
                        "distance_km is total open-route distance",
                    ],
                },
            },
        ],
        "blocks": [
            {"id": "title", "type": "markdown", "body": f"# {title}"},
            {
                "id": "technical_summary",
                "type": "markdown",
                "body": (
                    "## 技术摘要\n\n"
                    f"新组合策略在 {improved}/5 个分支提高平均按时订单数，未形成跨分支稳定收益。"
                    "现有实现原本已经把按时订单数作为第一目标；本轮真正测试的是去掉逾期幅度 tie-break，"
                    "并把已明显逾期的 removed 任务延后回插。全部订单仍须完成。"
                ),
            },
            {
                "id": "headline_metrics",
                "type": "metric-strip",
                "cardIds": [
                    "best_existing_rate",
                    "best_existing_distance",
                    "best_new_rate",
                    "best_new_distance",
                ],
            },
            {
                "id": "key_findings",
                "type": "markdown",
                "sourceId": "formal_summary",
                "body": (
                    "## 关键发现\n\n"
                    f"既有正式最佳均值为 {best_existing['label']}：准时率 "
                    f"{100 * float(best_existing['on_time_rate_mean']):.3f}%，总里程 "
                    f"{float(best_existing['distance_km_mean']):.3f} km。"
                    f"本轮 treatment 最佳为 {best_new['label']}：准时率 "
                    f"{100 * float(best_new['on_time_rate_mean']):.3f}%，总里程 "
                    f"{float(best_new['distance_km_mean']):.3f} km。"
                    "里程仅在按时数相同后用于比较，不能补偿准时率下降。"
                ),
            },
            {"id": "comparison_chart", "type": "chart", "chartId": "branch_on_time_rate"},
            {"id": "comparison_table", "type": "table", "tableId": "branch_comparison"},
            {
                "id": "scope",
                "type": "markdown",
                "sourceId": "problem_statement",
                "body": (
                    "## 范围与指标\n\n"
                    "题目文档没有单独分值公式或权重；直接比较顺序是先最大化按时订单数，"
                    "再最小化总配送里程。准时率是固定任务数下按时订单数的等价表达。"
                    "约束合法性是纳入门槛，不作为分数。"
                ),
            },
            {
                "id": "methodology",
                "type": "markdown",
                "body": (
                    "## 方法\n\n"
                    "五个分支统一使用最多 10% 的延后池；只有单独删除后能减少当前逾期订单数的任务才可进入。"
                    "普通任务先由分支原生 repair 回插，延后任务最后由 Regret-2 回插。"
                    "正式比较采用 200 单、三个相同 seed 和 240 秒总预算。"
                ),
            },
            {"id": "formal_table_block", "type": "table", "tableId": "formal_summary"},
            {
                "id": "limitations",
                "type": "markdown",
                "body": (
                    "## 局限\n\n"
                    "每个正式配置仅三个 seed；除 uav 外，对照来自仓库中的同 seed、同预算历史批次。"
                    "本轮同时改变 tie-break 与回插顺序，不能独立归因；10% 延后比例尚未调优。"
                ),
            },
            {
                "id": "recommendations",
                "type": "markdown",
                "body": (
                    "## 建议\n\n"
                    "保持 treatment 默认关闭。下一轮优先保留已有 late-risk destroy，"
                    "并对 2%/5%/10% 延后比例以及‘仅两层目标/仅 repair-last/组合’做析因实验。"
                ),
            },
            {
                "id": "further_questions",
                "type": "markdown",
                "body": (
                    "## 后续问题\n\n"
                    "需要验证基于预计最早可达时间、边际插入代价与路径冲突度的 hopeless 概率，"
                    "能否比当前‘已经逾期’筛选更早、更准确地识别应降低准时优先级的订单。"
                ),
            },
        ],
    }
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def build_sources(args: argparse.Namespace) -> list[Source]:
    return [
        Source(
            "uav_benchmark",
            "codex/uav-alns-dispatch",
            args.uav_root,
            "algorithm/results/benchmark_runs.csv",
            "uav_benchmark",
            _historical_benchmark_stage,
            note="Canonical benchmark artifact; inherited copies on later branches are not reread.",
            expected_raw_rows=41,
            expected_included_rows=41,
        ),
        Source(
            "core_history",
            "codex/alns-core-comparison",
            args.core_root,
            "algorithm/results/alns_core_comparison/comparison_runs.csv",
            "core_comparison",
            lambda row: "formal_240s"
            if row["scenario"] == "equal_wall_clock_240s"
            else "fixed_iterations_400",
            default_task_count=200,
            include=lambda row: not (
                row["scenario"] == "equal_iterations_400"
                and row["method"] == "C2-Lex-HALNS"
            ),
            note="Five inherited HALNS fixed-iteration rows are represented by uav_benchmark and excluded here.",
            expected_raw_rows=16,
            expected_included_rows=11,
        ),
        Source(
            "stronger_history",
            "codex/stronger-uav-neighborhoods",
            args.stronger_root,
            "algorithm/results/neighborhood_ablation/ablation_runs.csv",
            "neighborhood_ablation",
            "formal_240s",
            default_task_count=200,
            expected_raw_rows=21,
            expected_included_rows=21,
        ),
        Source(
            "capacity_history",
            "feature/capacity-aware-halns",
            args.capacity_root,
            "algorithm/results/capacity_halns/capacity_halns_runs.csv",
            "capacity_comparison",
            "formal_240s",
            default_task_count=200,
            expected_raw_rows=6,
            expected_included_rows=6,
        ),
        Source(
            "adaptive_history",
            "feature/adaptive-deadline-rejection-pool",
            args.adaptive_root,
            "results/adaptive_deadline_rejection/adaptive_runs.csv",
            "adaptive_comparison",
            "formal_240s",
            default_task_count=200,
            note="Final CSV is canonical; the identical partial checkpoint is excluded.",
            expected_raw_rows=15,
            expected_included_rows=15,
        ),
        Source(
            "adaptive_smoke_iterations",
            "feature/adaptive-deadline-rejection-pool",
            args.adaptive_root,
            "results/adaptive_deadline_rejection/smoke_phase5/adaptive_runs.csv",
            "adaptive_smoke_iterations",
            "development_smoke_5_iterations",
            default_task_count=200,
            note=(
                "Canonical phase-5 file subsumes the identical phase-2/3/4 smoke rows; "
                "partial checkpoints are excluded."
            ),
            expected_raw_rows=5,
            expected_included_rows=5,
        ),
        Source(
            "adaptive_smoke_wall",
            "feature/adaptive-deadline-rejection-pool",
            args.adaptive_root,
            "results/adaptive_deadline_rejection/smoke_wall_phase5/adaptive_runs.csv",
            "adaptive_smoke_wall",
            "development_smoke_10s",
            default_task_count=200,
            note="Final CSV is canonical; the partial checkpoint is excluded.",
            expected_raw_rows=5,
            expected_included_rows=5,
        ),
        Source(
            "uav_new",
            "codex/uav-alns-dispatch",
            args.uav_root,
            "algorithm/results/on_time_deferred_comparison/on_time_runs.csv",
            "uav_on_time_treatment",
            "new_formal_240s",
            is_new=True,
            default_task_count=200,
            expected_raw_rows=6,
            expected_included_rows=6,
        ),
        Source(
            "core_new",
            "codex/alns-core-comparison",
            args.core_root,
            "algorithm/results/on_time_sacrifice_core/comparison_runs.csv",
            "core_on_time_treatment",
            "new_formal_240s",
            is_new=True,
            default_task_count=200,
            expected_raw_rows=3,
            expected_included_rows=3,
        ),
        Source(
            "stronger_new",
            "codex/stronger-uav-neighborhoods",
            args.stronger_root,
            "algorithm/results/on_time_sacrifice/ablation_runs.csv",
            "stronger_on_time_treatment",
            "new_formal_240s",
            is_new=True,
            default_task_count=200,
            expected_raw_rows=3,
            expected_included_rows=3,
        ),
        Source(
            "capacity_new",
            "feature/capacity-aware-halns",
            args.capacity_root,
            "algorithm/results/capacity_halns_punctuality_paired/capacity_halns_runs.csv",
            "capacity_on_time_treatment",
            "new_formal_240s",
            is_new=True,
            default_task_count=200,
            expected_raw_rows=3,
            expected_included_rows=3,
        ),
        Source(
            "adaptive_new",
            "feature/adaptive-deadline-rejection-pool",
            args.adaptive_root,
            "results/on_time_sacrifice/adaptive_runs.csv",
            "adaptive_on_time_treatment",
            "new_formal_240s",
            is_new=True,
            default_task_count=200,
            expected_raw_rows=3,
            expected_included_rows=3,
        ),
    ]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    repo = Path(__file__).resolve().parents[2]
    parser.add_argument("--uav-root", type=Path, default=Path("/tmp/JD_WuLiuBei-worktrees/uav"))
    parser.add_argument("--core-root", type=Path, default=Path("/tmp/JD_WuLiuBei-worktrees/core"))
    parser.add_argument(
        "--stronger-root", type=Path, default=Path("/tmp/JD_WuLiuBei-worktrees/stronger")
    )
    parser.add_argument(
        "--capacity-root", type=Path, default=Path("/tmp/JD_WuLiuBei-worktrees/capacity")
    )
    parser.add_argument("--adaptive-root", type=Path, default=repo)
    parser.add_argument(
        "--output-dir", type=Path, default=repo / "algorithm/reports/on_time_objective"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    branch_roots = {
        "codex/uav-alns-dispatch": args.uav_root,
        "codex/alns-core-comparison": args.core_root,
        "codex/stronger-uav-neighborhoods": args.stronger_root,
        "feature/capacity-aware-halns": args.capacity_root,
        "feature/adaptive-deadline-rejection-pool": args.adaptive_root,
    }
    default_runner_relative_paths = {
        "codex/uav-alns-dispatch": "algorithm/experiments/run_on_time_deferred_comparison.py",
        "codex/alns-core-comparison": "algorithm/experiments/run_alns_core_comparison.py",
        "codex/stronger-uav-neighborhoods": "algorithm/experiments/run_neighborhood_ablation.py",
        "feature/capacity-aware-halns": "algorithm/experiments/run_capacity_halns_benchmark.py",
        "feature/adaptive-deadline-rejection-pool": (
            "algorithm/experiments/run_adaptive_deadline_rejection.py"
        ),
    }
    solver_sha256_by_branch = {
        branch: _sha256(root / "algorithm/src/uav_dispatch/alns.py")
        for branch, root in branch_roots.items()
    }
    data_relative_path = Path(
        "algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv"
    )
    dataset_sha256_by_branch = {
        "codex/uav-alns-dispatch": _sha256(args.uav_root / data_relative_path),
        "codex/alns-core-comparison": _sha256(args.core_root / data_relative_path),
        "codex/stronger-uav-neighborhoods": _sha256(
            args.stronger_root / data_relative_path
        ),
        "feature/capacity-aware-halns": _sha256(
            args.capacity_root / data_relative_path
        ),
        "feature/adaptive-deadline-rejection-pool": _sha256(
            args.adaptive_root / data_relative_path
        ),
    }
    unique_dataset_hashes = set(dataset_sha256_by_branch.values())
    if len(unique_dataset_hashes) != 1:
        raise ValueError(
            f"Cross-branch input dataset hashes differ: {dataset_sha256_by_branch}"
        )
    input_dataset_sha256 = next(iter(unique_dataset_hashes))
    all_rows: list[dict[str, object]] = []
    source_manifest: list[dict[str, object]] = []
    runner_sha256_by_source: dict[str, str] = {}
    for source in build_sources(args):
        path = source.root / source.relative_path
        if not path.exists():
            raise FileNotFoundError(f"Missing source {source.source_id}: {path}")
        commit = _branch_commit(source.root)
        source_file_sha256 = _sha256(path)
        runner_relative_path = (
            "algorithm/experiments/run_benchmarks.py"
            if source.source_id == "uav_benchmark"
            else default_runner_relative_paths[source.branch]
        )
        runner_file_sha256 = _sha256(source.root / runner_relative_path)
        runner_sha256_by_source[source.source_id] = runner_file_sha256
        with path.open(newline="", encoding="utf-8") as handle:
            raw_rows = list(csv.DictReader(handle))
        included = [row for row in raw_rows if source.include(row)]
        if source.expected_raw_rows is not None and len(raw_rows) != source.expected_raw_rows:
            raise ValueError(
                f"{source.source_id}: expected {source.expected_raw_rows} raw rows, "
                f"got {len(raw_rows)}"
            )
        if (
            source.expected_included_rows is not None
            and len(included) != source.expected_included_rows
        ):
            raise ValueError(
                f"{source.source_id}: expected {source.expected_included_rows} included rows, "
                f"got {len(included)}"
            )
        for row in included:
            all_rows.append(
                _normalize(row, source, commit, source_file_sha256)
            )
        source_manifest.append(
            {
                "source_id": source.source_id,
                "source_branch": source.branch,
                "snapshot_commit": commit,
                "source_file": source.relative_path,
                "source_file_sha256": source_file_sha256,
                "snapshot_solver_file": "algorithm/src/uav_dispatch/alns.py",
                "snapshot_solver_file_sha256": solver_sha256_by_branch[source.branch],
                "snapshot_runner_file": runner_relative_path,
                "snapshot_runner_file_sha256": runner_file_sha256,
                "raw_row_count": len(raw_rows),
                "included_row_count": len(included),
                "excluded_duplicate_row_count": len(raw_rows) - len(included),
                "note": source.note,
            }
        )

    all_rows.sort(key=lambda row: (str(row["experiment_stage"]), str(row["run_id"])))
    if len({row["run_id"] for row in all_rows}) != len(all_rows):
        raise ValueError("Duplicate run_id detected")
    role_counts = Counter(str(row["experiment_role"]) for row in all_rows)
    expected_role_counts = Counter(existing=104, control=3, treatment=15)
    if role_counts != expected_role_counts:
        raise ValueError(
            f"Unexpected experiment role counts: {dict(role_counts)}; "
            f"expected {dict(expected_role_counts)}"
        )
    for row in all_rows:
        if not 0 <= int(row["on_time_count"]) <= int(row["task_count"]):
            raise ValueError(f"Invalid on-time count: {row}")
        if not 0 <= float(row["on_time_rate"]) <= 1:
            raise ValueError(f"Invalid on-time rate: {row}")
        if float(row["distance_km"]) < 0:
            raise ValueError(f"Invalid distance: {row}")
        if row["experiment_stage"] in {"formal_240s", "new_formal_240s"} and (
            row["valid"] != "True" or row["compliant"] != "True"
        ):
            raise ValueError(f"Invalid formal run: {row}")

    comparisons = [
        _comparison(
            "codex/uav-alns-dispatch",
            _select(all_rows, family="uav_on_time_treatment", variant="halns_control"),
            _select(all_rows, family="uav_on_time_treatment", variant="halns_treatment"),
        ),
        _comparison(
            "codex/alns-core-comparison",
            _select(
                all_rows,
                family="core_comparison",
                method="C2-Lex-ALNS-Core",
                stage="formal_240s",
            ),
            _select(all_rows, family="core_on_time_treatment", variant="core_treatment"),
        ),
        _comparison(
            "codex/stronger-uav-neighborhoods",
            _select(all_rows, family="neighborhood_ablation", variant="A2"),
            _select(all_rows, family="stronger_on_time_treatment", variant="A7"),
        ),
        _comparison(
            "feature/capacity-aware-halns",
            _select(all_rows, family="capacity_comparison", variant="capacity_halns"),
            _select(
                all_rows,
                family="capacity_on_time_treatment",
                variant="capacity_halns_punctuality",
            ),
        ),
        _comparison(
            "feature/adaptive-deadline-rejection-pool",
            _select(all_rows, family="adaptive_comparison", method="experiment1"),
            _select(all_rows, family="adaptive_on_time_treatment", method="experiment5"),
        ),
    ]

    summaries = _summarize(all_rows)
    formal_run_count = sum(
        row["experiment_stage"] in {"formal_240s", "new_formal_240s"}
        for row in all_rows
    )
    new_formal_run_count = sum(
        row["experiment_stage"] == "new_formal_240s" for row in all_rows
    )
    expected_counts = {
        "all runs": (len(all_rows), 122),
        "summary rows": (len(summaries), 58),
        "formal runs": (formal_run_count, 66),
        "new formal runs": (new_formal_run_count, 18),
        "sources": (len(source_manifest), 12),
        "comparisons": (len(comparisons), 5),
    }
    mismatches = {
        name: {"actual": actual, "expected": expected}
        for name, (actual, expected) in expected_counts.items()
        if actual != expected
    }
    if mismatches:
        raise ValueError(f"Cross-branch coverage mismatch: {mismatches}")
    forbidden_fields = {"late_count", "total_lateness_min", "runtime_seconds"}
    emitted_fields = set(DIRECT_COLUMNS)
    emitted_fields.update(summaries[0].keys() if summaries else ())
    emitted_fields.update(comparisons[0].keys() if comparisons else ())
    forbidden_present = sorted(forbidden_fields & emitted_fields)
    if forbidden_present:
        raise ValueError(
            "Non-reporting metrics leaked into consolidated outputs: "
            + ", ".join(forbidden_present)
        )
    _write_csv(args.output_dir / "all_runs.csv", all_rows, DIRECT_COLUMNS)
    _write_csv(args.output_dir / "summary.csv", summaries)
    _write_csv(args.output_dir / "new_vs_control.csv", comparisons)
    _write_csv(args.output_dir / "source_manifest.csv", source_manifest)
    validation = {
        "status": "passed",
        "direct_metric_order": ["maximize on_time_count", "minimize distance_km"],
        "input_dataset_sha256": input_dataset_sha256,
        "input_dataset_sha256_by_branch": dataset_sha256_by_branch,
        "snapshot_solver_sha256_by_branch": solver_sha256_by_branch,
        "snapshot_runner_sha256_by_source": runner_sha256_by_source,
        "problem_statement_sha256": _sha256(
            args.adaptive_root / "命题1-低空经济场景下的物流无人机调度算法.docx"
        ),
        "all_run_count": len(all_rows),
        "summary_row_count": len(summaries),
        "formal_run_count": formal_run_count,
        "new_formal_run_count": new_formal_run_count,
        "source_count": len(source_manifest),
        "comparison_count": len(comparisons),
        "forbidden_ranking_metrics_present": bool(forbidden_present),
    }
    (args.output_dir / "validation.json").write_text(
        json.dumps(validation, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_markdown_report(
        args.output_dir.parent / "ON_TIME_SACRIFICE_REPORT.md",
        all_rows,
        summaries,
        comparisons,
        validation,
    )
    _write_artifact_snapshot(
        args.output_dir / "artifact_snapshot.json",
        summaries,
        comparisons,
    )
    _write_artifact_manifest(
        args.output_dir / "artifact_manifest.json",
        summaries,
        comparisons,
    )
    print(json.dumps(validation, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
