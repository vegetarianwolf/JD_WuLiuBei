#!/usr/bin/env python3
"""Build the final manuscript from the audited source DOCX and formal outputs.

This program is deliberately strict: it accepts only the fresh, formal
``final_benchmarks``, ``final_scenario_comparison`` and ``final_sensitivity``
artefacts, verifies their experiment protocols and linked solution files,
then replaces the first Chapter 4 through the paragraph immediately before
"参考文献".  Chapters 1--3, references, sections, margins, headers, footers and
page-number fields are left in the source package.

The source file is never edited in place.  The output is written atomically
and an existing output requires ``--force``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from docx import Document
from docx.document import Document as DocumentObject
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Pt, RGBColor
from docx.table import Table
from docx.text.paragraph import Paragraph


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path(
    "/Users/vegetarianwolf/Library/Containers/com.tencent.xinWeChat/Data/"
    "Documents/xwechat_files/wxid_jfuculsqs3n822_7f24/msg/file/2026-08/"
    "低空经济场景下的物流无人机调度算法_策略与数学模型对齐修订版-1.docx"
)
DEFAULT_OUTPUT = (
    ROOT
    / "writing"
    / "manuscript"
    / "低空经济场景下的物流无人机调度算法_实验与模型对齐终稿.docx"
)
DEFAULT_RESULTS = ROOT / "algorithm" / "results"
DEFAULT_FIGURES = ROOT / "writing" / "figures" / "final"

SCENARIO_IDS = (
    "pure_direct",
    "direct_relay",
    "direct_stations",
    "direct_relay_stations",
)
EXPECTED_SCENARIO_SEEDS = (2026081701, 2026081702, 2026081703)
SCENARIO_LABELS = {
    "pure_direct": "Direct（原点起点）",
    "direct_relay": "Direct + Relay（原点起点）",
    "direct_stations": "Direct + Station（需求驱动预部署）",
    "direct_relay_stations": "Direct + Relay + Station",
}
EXPECTED_SCENARIO_SPECS = {
    "pure_direct": {"relay": False, "warmup": 0.0, "drone_homes": "origin"},
    "direct_relay": {"relay": True, "warmup": 0.8, "drone_homes": "origin"},
    "direct_stations": {"relay": False, "warmup": 0.0, "drone_homes": "stations"},
    "direct_relay_stations": {"relay": True, "warmup": 0.8, "drone_homes": "stations"},
}
EXPECTED_BUDGETS = {
    "pure_direct": 240.0,
    "direct_relay": 300.0,
    "direct_stations": 300.0,
    "direct_relay_stations": 300.0,
}
DESTROY_OPERATORS = (
    "random",
    "worst_distance",
    "worst_lex",
    "spatial_related",
    "deadline_related",
    "late_critical",
    "route_segment",
    "capacity_conflict",
    "assignment_destroy",
)
REPAIR_OPERATORS = ("greedy", "regret2", "regret3", "deadline", "slack")
OPERATOR_KEYS = tuple(f"destroy:{name}" for name in DESTROY_OPERATORS) + tuple(
    f"repair:{name}" for name in REPAIR_OPERATORS
)
SENSITIVITY_PARAMETERS = (
    "drone_count",
    "deadline_multiplier",
    "station_count",
)
EXPECTED_SENSITIVITY_LEVELS = {
    "drone_count": (8.0, 9.0, 10.0, 12.0, 16.0),
    "deadline_multiplier": (0.8, 0.9, 1.0, 1.1, 1.2),
    "station_count": (2.0, 3.0, 4.0, 5.0, 6.0),
}
FIGURE_FILES = (
    "fig5_1_title_example.png",
    "fig5_2_exact_validation.png",
    "fig5_3_relaxed_multiscale.png",
    "fig5_4_operator_effectiveness.png",
    "fig6_1_scenario_comparison.png",
    "fig6_2_route_layouts.png",
    "fig6_3_drone_count_sensitivity.png",
    "fig6_3_deadline_multiplier_sensitivity.png",
    "fig6_3_station_count_sensitivity.png",
)

OPERATOR_DESCRIPTIONS: dict[str, tuple[str, str, str]] = {
    "random": ("随机抽取完整任务", "任务归属与访问位置", "统一可行性评价"),
    "worst_distance": ("优先移除边际航程最大的任务", "长距离局部结构", "容量、配对与DAG"),
    "worst_lex": ("按三层目标边际损失排序", "服务与里程冲突任务", "严格字典序重评"),
    "spatial_related": ("移除空间邻近的相关任务", "局部空间簇", "完整任务成对移除"),
    "deadline_related": ("移除截止期相近的任务", "时效相关任务簇", "最终送达时刻重算"),
    "late_critical": ("优先移除逾期或高风险任务", "关键服务任务", "无逾期时安全回退"),
    "route_segment": ("破坏连续路线片段", "局部访问片段", "任务取送成对清理"),
    "capacity_conflict": ("聚焦高载荷冲突附近任务", "容量紧张片段", "容量轨迹逐点检查"),
    "assignment_destroy": ("跨无人机解除任务分配", "任务—无人机归属", "单机任务上限与全局校验"),
    "greedy": ("选择最佳可行插入", "取送成对插入位置", "枚举后精确评价"),
    "regret2": ("按最佳与次佳插入差排序", "最难延后任务", "候选不足时确定性回退"),
    "regret3": ("按前三个插入机会的遗憾值排序", "插入选择脆弱任务", "候选可行性逐一检查"),
    "deadline": ("按截止期紧迫度优先插入", "紧急任务位置", "准时性以最终送达判定"),
    "slack": ("按剩余时限与松弛度优先插入", "低松弛任务位置", "时间与容量联合检查"),
}


class ArtefactError(RuntimeError):
    """Raised before document mutation when a formal artefact is incomplete."""


@dataclass(frozen=True)
class Bundle:
    benchmark_dir: Path
    scenario_dir: Path
    sensitivity_dir: Path
    figure_dir: Path
    benchmark_manifest: dict[str, Any]
    benchmark_rows: tuple[dict[str, Any], ...]
    benchmark_solutions: dict[str, dict[str, Any]]
    scenario_manifest: dict[str, Any]
    scenario_rows: tuple[dict[str, Any], ...]
    scenario_summary_rows: tuple[dict[str, Any], ...]
    sensitivity_manifest: dict[str, Any]
    sensitivity_rows: tuple[dict[str, Any], ...]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-docx", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-docx", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--benchmarks-dir", type=Path)
    parser.add_argument("--scenarios-dir", type=Path)
    parser.add_argument("--sensitivity-dir", type=Path)
    parser.add_argument("--figures-dir", type=Path, default=DEFAULT_FIGURES)
    parser.add_argument(
        "--force",
        action="store_true",
        help="replace an existing output DOCX (the source is still protected)",
    )
    return parser


def _load_json(path: Path, label: str) -> Any:
    if not path.is_file():
        raise ArtefactError(f"缺少{label}：{path}")
    if path.stat().st_size == 0:
        raise ArtefactError(f"{label}为空文件：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtefactError(f"无法读取{label} {path}：{exc}") from exc


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ArtefactError(f"{label}必须是JSON对象")
    return value


def _rows(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ArtefactError(f"{label}必须是非空JSON数组")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ArtefactError(f"{label}[{index}]必须是JSON对象")
        result.append(dict(item))
    return result


def _require_keys(row: Mapping[str, Any], keys: Iterable[str], label: str) -> None:
    missing = [key for key in keys if key not in row]
    if missing:
        raise ArtefactError(f"{label}缺少字段：{', '.join(missing)}")


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ArtefactError(f"{label}必须是数值，实际为{value!r}")
    converted = float(value)
    if not math.isfinite(converted):
        raise ArtefactError(f"{label}必须是有限数值，实际为{value!r}")
    return converted


def _close(left: Any, right: float, label: str, tolerance: float = 1e-8) -> None:
    value = _number(left, label)
    if not math.isclose(value, right, rel_tol=0.0, abs_tol=tolerance):
        raise ArtefactError(f"{label}应为{right:g}，实际为{value:g}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _solver_source_sha256() -> str:
    digest = hashlib.sha256()
    source_root = ROOT / "algorithm" / "src" / "uav_dispatch"
    source_files = sorted(source_root.glob("*.py"))
    if not source_files:
        raise ArtefactError(f"找不到求解器源码：{source_root}")
    for path in source_files:
        relative = path.relative_to(ROOT).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _verify_manifest_input(manifest: Mapping[str, Any], label: str) -> None:
    _require_keys(manifest, ("input", "input_sha256"), label)
    path = Path(str(manifest["input"]))
    if not path.is_file():
        raise ArtefactError(f"{label}记录的输入文件不存在：{path}")
    if _sha256(path) != str(manifest["input_sha256"]):
        raise ArtefactError(f"{label}记录的输入SHA-256与文件不一致：{path}")


def _solution(path: Path, expected_sha256: str | None, label: str) -> dict[str, Any]:
    payload = _mapping(_load_json(path, label), label)
    if expected_sha256 is not None and _sha256(path) != expected_sha256:
        raise ArtefactError(f"{label}哈希与运行清单不一致：{path}")
    _require_keys(
        payload,
        ("valid", "violations", "score", "problem", "metadata", "routes", "relay"),
        label,
    )
    if payload["valid"] is not True or payload["violations"]:
        raise ArtefactError(f"{label}未通过可行性校验：{payload['violations']!r}")
    score = _mapping(payload["score"], f"{label}.score")
    _require_keys(score, ("late_count", "total_lateness_min", "distance_km"), f"{label}.score")
    problem = _mapping(payload["problem"], f"{label}.problem")
    _require_keys(
        problem,
        (
            "task_count",
            "drone_count",
            "max_tasks_per_drone",
            "capacity",
            "speed_km_per_min",
            "depot_km",
            "drone_homes",
            "open_routes",
            "objective_order",
        ),
        f"{label}.problem",
    )
    expected_objective_order = [
        "late_count",
        "total_lateness_min",
        "distance_km",
    ]
    if problem["open_routes"] is not True:
        raise ArtefactError(f"{label}.problem.open_routes必须为true")
    if problem["objective_order"] != expected_objective_order:
        raise ArtefactError(
            f"{label}.problem.objective_order不是正式三层字典序"
        )
    routes = payload["routes"]
    homes = problem["drone_homes"]
    if not isinstance(routes, list) or not isinstance(homes, list):
        raise ArtefactError(f"{label}的routes与problem.drone_homes必须是JSON数组")
    drone_count = int(problem["drone_count"])
    if len(routes) != drone_count or len(homes) != drone_count:
        raise ArtefactError(f"{label}的路线数、home数与drone_count不一致")
    for route_index, route in enumerate(routes):
        route_payload = _mapping(route, f"{label}.routes[{route_index}]")
        _require_keys(
            route_payload,
            ("drone_id", "encoded_visits", "visits"),
            f"{label}.routes[{route_index}]",
        )
    metadata = _mapping(payload["metadata"], f"{label}.metadata")
    _require_keys(
        metadata,
        ("iterations_per_second", "operator_statistics", "operator_weights"),
        f"{label}.metadata",
    )
    weights = _mapping(metadata["operator_weights"], f"{label}.metadata.operator_weights")
    if set(weights) != set(OPERATOR_KEYS):
        raise ArtefactError(f"{label}解文件的最终权重不是14个注册算子")
    relay = _mapping(payload["relay"], f"{label}.relay")
    _require_keys(
        relay,
        (
            "relay_count",
            "relay_task_count",
            "relay_share",
            "cross_uav_handoff_count",
            "same_uav_relay_count",
            "total_relay_waiting_time",
        ),
        f"{label}.relay",
    )
    return payload


def _validate_operator_statistics(row: Mapping[str, Any], label: str) -> None:
    statistics_payload = row.get("operator_statistics")
    if not isinstance(statistics_payload, dict):
        raise ArtefactError(f"{label}.operator_statistics必须是JSON对象")
    if set(statistics_payload) != set(OPERATOR_KEYS):
        missing = [name for name in OPERATOR_KEYS if name not in statistics_payload]
        extra = [name for name in statistics_payload if name not in OPERATOR_KEYS]
        raise ArtefactError(
            f"{label}算子集合不是最终14个注册算子；缺少={missing}，多出={extra}"
        )
    for operator, values in statistics_payload.items():
        values = _mapping(values, f"{label}.{operator}")
        _require_keys(
            values,
            ("uses", "accepted", "current_improvements", "best_improvements", "total_reward"),
            f"{label}.{operator}",
        )
        uses = _number(values["uses"], f"{label}.{operator}.uses")
        accepted = _number(values["accepted"], f"{label}.{operator}.accepted")
        current = _number(values["current_improvements"], f"{label}.{operator}.current_improvements")
        best = _number(values["best_improvements"], f"{label}.{operator}.best_improvements")
        if uses <= 0:
            raise ArtefactError(f"{label}.{operator}在正式运行中没有实际被调用")
        if not 0 <= best <= current <= accepted <= uses:
            raise ArtefactError(
                f"{label}.{operator}不满足best≤current≤accepted≤uses"
            )


def _validate_benchmarks(directory: Path) -> tuple[
    dict[str, Any], list[dict[str, Any]], dict[str, dict[str, Any]]
]:
    payload = _mapping(_load_json(directory / "results.json", "第5章正式结果"), "第5章正式结果")
    _require_keys(payload, ("manifest", "runs"), "第5章正式结果")
    manifest = _mapping(payload["manifest"], "第5章manifest")
    rows = _rows(payload["runs"], "第5章runs")
    _require_keys(
        manifest,
        (
            "protocol",
            "formal",
            "created_at_utc",
            "input_sha256",
            "exact_sizes",
            "multiscale_task_counts",
            "relaxed_deadline_multiplier",
            "scale_seconds",
            "seed",
            "operator_count",
            "solver_source_sha256",
        ),
        "第5章manifest",
    )
    if manifest["protocol"] != "section5_exact_and_relaxed_multiscale" or manifest["formal"] is not True:
        raise ArtefactError("第5章结果不是正式 section5_exact_and_relaxed_multiscale 协议")
    if manifest["operator_count"] != 14:
        raise ArtefactError("第5章manifest的operator_count必须为14")
    if manifest["solver_source_sha256"] != _solver_source_sha256():
        raise ArtefactError("第5章manifest记录的求解器源码SHA-256与当前源码不一致")
    expected_exact = [int(value) for value in manifest["exact_sizes"]]
    expected_scale = [int(value) for value in manifest["multiscale_task_counts"]]
    if expected_exact != [5, 8, 10, 12]:
        raise ArtefactError("第5章正式精确规模必须为5、8、10、12")
    if expected_scale != [50, 100, 150, 200]:
        raise ArtefactError("第5章正式多机规模必须为50、100、150、200")
    _close(manifest["relaxed_deadline_multiplier"], 2.5, "第5章宽松截止期倍率")
    _close(manifest["scale_seconds"], 30.0, "第5章多机单次预算")
    if int(manifest["seed"]) != EXPECTED_SCENARIO_SEEDS[0]:
        raise ArtefactError("第5章正式种子必须为2026081701")
    if len(rows) != 1 + 2 * len(expected_exact) + len(expected_scale):
        raise ArtefactError("第5章正式runs数量与1+2×4+4协议不一致")
    common_fields = (
        "experiment",
        "method",
        "task_count",
        "drone_count",
        "deadline_multiplier",
        "seed",
        "late_count",
        "total_lateness_min",
        "distance_km",
        "runtime_seconds",
        "iterations",
        "valid",
        "operator_statistics",
    )
    for index, row in enumerate(rows):
        label = f"第5章runs[{index}]"
        _require_keys(row, common_fields, label)
        if row["valid"] is not True:
            raise ArtefactError(f"{label}不是有效解")
        for key in ("late_count", "total_lateness_min", "distance_km", "runtime_seconds", "iterations"):
            _number(row[key], f"{label}.{key}")
        if row["method"] == "C2-Lex-ALNS":
            _validate_operator_statistics(row, label)
    title = [row for row in rows if row["experiment"] == "title_example_n2"]
    if len(title) != 1 or title[0]["method"] != "Pareto-DP":
        raise ArtefactError("第5章结果必须且只能含一个题设两任务Pareto-DP运行")
    if title[0]["late_count"] != 0 or not math.isclose(float(title[0]["distance_km"]), 11.0, abs_tol=1e-9):
        raise ArtefactError("题设两任务正式结果没有复核出0逾期、11 km")
    for size in expected_exact:
        experiment = f"official_prefix_n{size}"
        selected = [row for row in rows if row["experiment"] == experiment]
        if len(selected) != 2 or {row["method"] for row in selected} != {
            "Pareto-DP",
            "C2-Lex-ALNS",
        }:
            raise ArtefactError(f"精确规模n={size}必须恰含一条DP和一条ALNS")
        exact = next(row for row in selected if row["method"] == "Pareto-DP")
        alns = next(row for row in selected if row["method"] == "C2-Lex-ALNS")
        if int(exact["task_count"]) != size or int(alns["task_count"]) != size:
            raise ArtefactError(f"精确规模{experiment}的task_count不一致")
        if int(exact["drone_count"]) != 1 or int(alns["drone_count"]) != 1:
            raise ArtefactError(f"精确规模n={size}必须为单机")
        _close(exact["deadline_multiplier"], 1.0, f"精确规模n={size} DP截止期倍率")
        _close(alns["deadline_multiplier"], 1.0, f"精确规模n={size} ALNS截止期倍率")
        if exact["seed"] is not None or int(alns["seed"]) != EXPECTED_SCENARIO_SEEDS[0]:
            raise ArtefactError(f"精确规模n={size}的种子字段不符合协议")
        scores_match = (
            int(exact["late_count"]) == int(alns["late_count"])
            and math.isclose(float(exact["total_lateness_min"]), float(alns["total_lateness_min"]), rel_tol=0.0, abs_tol=1e-8)
            and math.isclose(float(exact["distance_km"]), float(alns["distance_km"]), rel_tol=0.0, abs_tol=1e-8)
        )
        if bool(alns.get("matches_oracle")) is not scores_match:
            raise ArtefactError(f"精确规模n={size}的matches_oracle与三层得分不一致")
    solutions: dict[str, dict[str, Any]] = {}
    for count in expected_scale:
        experiment = f"relaxed_multiuav_n{count}"
        matching = [row for row in rows if row["experiment"] == experiment]
        if len(matching) != 1:
            raise ArtefactError(f"多机扩展规模{count}必须恰有一条正式结果")
        row = matching[0]
        if row["method"] != "C2-Lex-ALNS" or int(row["task_count"]) != count:
            raise ArtefactError(f"多机扩展{experiment}的方法或任务数不一致")
        if int(row["drone_count"]) != math.ceil(count / 25):
            raise ArtefactError(f"多机扩展{experiment}的无人机数不满足ceil(N/25)")
        _close(row["deadline_multiplier"], 2.5, f"多机扩展{experiment}截止期倍率")
        if int(row["seed"]) != EXPECTED_SCENARIO_SEEDS[0]:
            raise ArtefactError(f"多机扩展{experiment}种子不一致")
        if float(row["runtime_seconds"]) > 30.1:
            raise ArtefactError(f"多机扩展{experiment}搜索时间超过名义30 s预算")
        path = directory / "solutions" / f"{experiment}.json"
        solution = _solution(path, None, f"多机扩展解{experiment}")
        solution_problem = _mapping(solution["problem"], f"多机扩展解{experiment}.problem")
        if int(solution_problem["task_count"]) != count or int(solution_problem["drone_count"]) != math.ceil(count / 25):
            raise ArtefactError(f"多机扩展解{experiment}的规模与runs不一致")
        for key in ("late_count", "total_lateness_min", "distance_km"):
            if not math.isclose(
                float(row[key]),
                float(solution["score"][key]),
                rel_tol=0.0,
                abs_tol=1e-8,
            ):
                raise ArtefactError(f"多机扩展{experiment}.{key}与解文件不一致")
        solutions[experiment] = solution
    return manifest, rows, solutions


def _validate_scenarios(directory: Path) -> tuple[
    dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]
]:
    manifest = _mapping(_load_json(directory / "manifest.json", "四场景manifest"), "四场景manifest")
    rows = _rows(_load_json(directory / "runs.json", "四场景逐运行结果"), "四场景runs")
    summary_rows = _rows(_load_json(directory / "summary.json", "四场景汇总结果"), "四场景summary")
    _require_keys(
        manifest,
        (
            "protocol",
            "formal",
            "created_at_utc",
            "python",
            "platform",
            "input_sha256",
            "solver_source_sha256",
            "tasks",
            "drones",
            "max_tasks_per_drone",
            "seeds",
            "base_seconds",
            "bonus_seconds",
            "scenario_budgets_seconds",
            "scenarios",
            "operator_count",
        ),
        "四场景manifest",
    )
    if manifest["protocol"] != "final_four_scenarios_three_paired_seeds" or manifest["formal"] is not True:
        raise ArtefactError("四场景结果不是正式 final_four_scenarios_three_paired_seeds 协议")
    if manifest["tasks"] != 200 or manifest["drones"] != 8 or manifest["max_tasks_per_drone"] != 25:
        raise ArtefactError("四场景正式规模必须为200任务、8架无人机、单机上限25项")
    if manifest["operator_count"] != 14:
        raise ArtefactError("四场景manifest的operator_count必须为14")
    _close(manifest["base_seconds"], 240.0, "四场景基础预算")
    _close(manifest["bonus_seconds"], 60.0, "四场景Relay/Station加时")
    if str(manifest["solver_source_sha256"]) != _solver_source_sha256():
        raise ArtefactError("四场景manifest的求解器源码SHA-256与当前源码不一致")
    seeds = [int(seed) for seed in manifest["seeds"]]
    if tuple(seeds) != EXPECTED_SCENARIO_SEEDS:
        raise ArtefactError(
            "四场景正式配对种子必须依次为2026081701、2026081702、2026081703"
        )
    scenario_specs = manifest["scenarios"]
    if not isinstance(scenario_specs, list) or len(scenario_specs) != len(SCENARIO_IDS):
        raise ArtefactError("四场景manifest.scenarios必须恰含4个场景对象")
    specs_by_id: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(scenario_specs):
        spec = _mapping(item, f"四场景manifest.scenarios[{index}]")
        _require_keys(spec, ("id", "label", "relay", "warmup", "drone_homes"), f"四场景manifest.scenarios[{index}]")
        scenario_id = str(spec["id"])
        if scenario_id in specs_by_id:
            raise ArtefactError(f"四场景manifest.scenarios重复id：{scenario_id}")
        specs_by_id[scenario_id] = spec
    if set(specs_by_id) != set(SCENARIO_IDS):
        raise ArtefactError("四场景manifest.scenarios与最终四场景不一致")
    for scenario_id, expected_spec in EXPECTED_SCENARIO_SPECS.items():
        actual_spec = specs_by_id[scenario_id]
        for field, expected in expected_spec.items():
            actual = actual_spec[field]
            if isinstance(expected, float):
                if not math.isclose(float(actual), expected, rel_tol=0.0, abs_tol=1e-12):
                    raise ArtefactError(f"{scenario_id}.{field}与最终协议不一致")
            elif actual != expected:
                raise ArtefactError(f"{scenario_id}.{field}与最终协议不一致")
    budgets = _mapping(manifest["scenario_budgets_seconds"], "四场景预算")
    if set(budgets) != set(SCENARIO_IDS):
        raise ArtefactError("四场景预算键与最终四场景不一致")
    for scenario, expected in EXPECTED_BUDGETS.items():
        _close(budgets[scenario], expected, f"{scenario}名义预算")
    if len(rows) != 12:
        raise ArtefactError(f"四场景正式结果应为4×3=12条，实际为{len(rows)}条")
    cells = {(row.get("scenario"), row.get("seed")) for row in rows}
    expected_cells = {(scenario, seed) for scenario in SCENARIO_IDS for seed in seeds}
    if cells != expected_cells:
        raise ArtefactError("四场景逐运行单元格不完整或重复")
    required = (
        "scenario",
        "scenario_label",
        "seed",
        "relay",
        "station_predeployment",
        "effective_time_limit_seconds",
        "construction_seconds",
        "solver_runtime_seconds",
        "wall_seconds",
        "iterations",
        "late_count",
        "total_lateness_min",
        "distance_km",
        "relay_task_count",
        "cross_uav_handoff_count",
        "total_relay_waiting_min",
        "station_count",
        "deployment_distribution",
        "deadhead_km",
        "home_assignment_rate",
        "operator_statistics",
        "solution_file",
        "solution_sha256",
    )
    enriched: list[dict[str, Any]] = []
    for index, source_row in enumerate(rows):
        row = dict(source_row)
        label = f"四场景runs[{index}]"
        _require_keys(row, required, label)
        scenario = str(row["scenario"])
        if str(row["scenario_label"]) != SCENARIO_LABELS[scenario]:
            raise ArtefactError(f"{label}.scenario_label与最终场景标签不一致")
        _close(row["effective_time_limit_seconds"], EXPECTED_BUDGETS[scenario], f"{label}.effective_time_limit_seconds")
        expected_spec = EXPECTED_SCENARIO_SPECS[scenario]
        if bool(row["relay"]) is not expected_spec["relay"]:
            raise ArtefactError(f"{label}.relay与场景定义不一致")
        expected_station = expected_spec["drone_homes"] == "stations"
        if bool(row["station_predeployment"]) is not expected_station:
            raise ArtefactError(f"{label}.station_predeployment与场景定义不一致")
        _validate_operator_statistics(row, label)
        solution_path = directory / str(row["solution_file"])
        solution = _solution(solution_path, str(row["solution_sha256"]), f"{label}解文件")
        score = _mapping(solution["score"], f"{label}解文件.score")
        problem = _mapping(solution["problem"], f"{label}解文件.problem")
        relay_payload = _mapping(solution["relay"], f"{label}解文件.relay")
        if (
            int(problem["task_count"]) != 200
            or int(problem["drone_count"]) != 8
            or int(problem["max_tasks_per_drone"]) != 25
        ):
            raise ArtefactError(f"{label}解文件规模与四场景manifest不一致")
        homes = [str(value) for value in problem["drone_homes"]]
        if expected_station:
            if all(home == "origin" for home in homes):
                raise ArtefactError(f"{label}Station场景没有任何异地home")
        elif any(home != "origin" for home in homes):
            raise ArtefactError(f"{label}原点场景出现异地home")
        if int(relay_payload["relay_count"]) != int(row["station_count"]):
            raise ArtefactError(f"{label}.station_count与解文件不一致")
        if int(relay_payload["relay_task_count"]) != int(row["relay_task_count"]):
            raise ArtefactError(f"{label}.relay_task_count与解文件不一致")
        if int(relay_payload["cross_uav_handoff_count"]) != int(row["cross_uav_handoff_count"]):
            raise ArtefactError(f"{label}.cross_uav_handoff_count与解文件不一致")
        if not math.isclose(
            float(relay_payload["total_relay_waiting_time"]),
            float(row["total_relay_waiting_min"]),
            rel_tol=0.0,
            abs_tol=1e-8,
        ):
            raise ArtefactError(f"{label}.total_relay_waiting_min与解文件不一致")
        metadata_statistics = _mapping(
            solution["metadata"]["operator_statistics"],
            f"{label}解文件.metadata.operator_statistics",
        )
        if metadata_statistics != row["operator_statistics"]:
            raise ArtefactError(f"{label}.operator_statistics与解文件metadata不一致")
        for key in ("late_count", "total_lateness_min", "distance_km"):
            if not math.isclose(float(row[key]), float(score[key]), rel_tol=0.0, abs_tol=1e-8):
                raise ArtefactError(f"{label}.{key}与解文件不一致")
        row["_solution"] = solution
        enriched.append(row)
    if {row.get("scenario") for row in summary_rows} != set(SCENARIO_IDS):
        raise ArtefactError("四场景summary没有覆盖最终四场景")
    for index, summary in enumerate(summary_rows):
        label = f"四场景summary[{index}]"
        _require_keys(
            summary,
            (
                "scenario",
                "runs",
                "mean_late_count",
                "std_late_count",
                "mean_total_lateness_min",
                "std_total_lateness_min",
                "mean_distance_km",
                "std_distance_km",
                "mean_relay_task_count",
                "mean_wall_seconds",
            ),
            label,
        )
        selected = [row for row in enriched if row["scenario"] == summary["scenario"]]
        if summary["runs"] != len(selected):
            raise ArtefactError(f"{label}.runs与逐运行结果不一致")
        expected_values = {
            "mean_late_count": statistics.fmean(float(row["late_count"]) for row in selected),
            "std_late_count": statistics.stdev(float(row["late_count"]) for row in selected),
            "mean_total_lateness_min": statistics.fmean(float(row["total_lateness_min"]) for row in selected),
            "std_total_lateness_min": statistics.stdev(float(row["total_lateness_min"]) for row in selected),
            "mean_distance_km": statistics.fmean(float(row["distance_km"]) for row in selected),
            "std_distance_km": statistics.stdev(float(row["distance_km"]) for row in selected),
            "mean_relay_task_count": statistics.fmean(float(row["relay_task_count"]) for row in selected),
            "mean_wall_seconds": statistics.fmean(float(row["wall_seconds"]) for row in selected),
        }
        for field, expected in expected_values.items():
            if not math.isclose(float(summary[field]), expected, rel_tol=0.0, abs_tol=1e-8):
                raise ArtefactError(f"{label}.{field}与逐运行结果不一致")
    return manifest, enriched, summary_rows


def _validate_sensitivity(directory: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not directory.is_dir():
        raise ArtefactError(
            f"缺少正式敏感性目录：{directory}。请先完整运行run_sensitivity_analysis.py，"
            "不得用历史结果或占位值生成论文。"
        )
    manifest = _mapping(_load_json(directory / "manifest.json", "敏感性manifest"), "敏感性manifest")
    rows = _rows(_load_json(directory / "runs.json", "敏感性逐水平结果"), "敏感性runs")
    _require_keys(
        manifest,
        (
            "protocol",
            "formal",
            "created_at_utc",
            "input_sha256",
            "solver_source_sha256",
            "tasks",
            "seed",
            "base_seconds",
            "bonus_seconds",
            "effective_seconds_per_run",
            "levels",
            "scenario",
        ),
        "敏感性manifest",
    )
    if manifest["protocol"] != "combined_mode_one_factor_sensitivity" or manifest["formal"] is not True:
        raise ArtefactError("敏感性结果不是正式 combined_mode_one_factor_sensitivity 协议")
    if manifest["tasks"] != 200:
        raise ArtefactError("正式敏感性分析必须使用200项任务")
    _close(manifest["base_seconds"], 240.0, "敏感性基础预算")
    _close(manifest["bonus_seconds"], 60.0, "敏感性Relay/Station加时")
    if str(manifest["solver_source_sha256"]) != _solver_source_sha256():
        raise ArtefactError("敏感性manifest的求解器源码SHA-256与当前源码不一致")
    if int(manifest["seed"]) != EXPECTED_SCENARIO_SEEDS[0]:
        raise ArtefactError("正式敏感性分析的固定种子必须为2026081701")
    scenario = _mapping(manifest["scenario"], "敏感性场景")
    if scenario.get("id") != "direct_relay_stations":
        raise ArtefactError("敏感性分析只能运行direct_relay_stations组合场景")
    _close(manifest["effective_seconds_per_run"], 300.0, "敏感性每次名义预算")
    levels = _mapping(manifest["levels"], "敏感性水平")
    if set(levels) != set(SENSITIVITY_PARAMETERS):
        raise ArtefactError("敏感性参数必须且只能是无人机数、截止期倍率和中继站数")
    for parameter in SENSITIVITY_PARAMETERS:
        parameter_levels = levels[parameter]
        if not isinstance(parameter_levels, list):
            raise ArtefactError(f"敏感性参数{parameter}的水平必须是JSON数组")
        actual_levels = tuple(float(value) for value in parameter_levels)
        if actual_levels != EXPECTED_SENSITIVITY_LEVELS[parameter]:
            raise ArtefactError(f"敏感性参数{parameter}的五档水平与最终协议不一致")
    expected_count = sum(len(levels[name]) for name in SENSITIVITY_PARAMETERS)
    if len(rows) != expected_count:
        raise ArtefactError(f"敏感性结果应为{expected_count}条，实际为{len(rows)}条")
    required = (
        "scenario",
        "seed",
        "effective_time_limit_seconds",
        "late_count",
        "total_lateness_min",
        "distance_km",
        "relay_task_count",
        "cross_uav_handoff_count",
        "total_relay_waiting_min",
        "station_count",
        "deadhead_km",
        "home_assignment_rate",
        "operator_statistics",
        "solution_file",
        "solution_sha256",
        "wall_seconds",
        "parameter",
        "level",
        "drone_count",
        "deadline_multiplier",
        "requested_station_count",
        "cell_output_dir",
    )
    enriched: list[dict[str, Any]] = []
    seen: set[tuple[str, float]] = set()
    for index, source_row in enumerate(rows):
        row = dict(source_row)
        label = f"敏感性runs[{index}]"
        _require_keys(row, required, label)
        if row["scenario"] != "direct_relay_stations" or int(row["seed"]) != int(manifest["seed"]):
            raise ArtefactError(f"{label}不是组合场景固定种子运行")
        parameter = str(row["parameter"])
        if parameter not in SENSITIVITY_PARAMETERS:
            raise ArtefactError(f"{label}.parameter未知：{parameter}")
        cell = (parameter, float(row["level"]))
        if cell in seen:
            raise ArtefactError(f"敏感性单元格重复：{cell}")
        seen.add(cell)
        _close(row["effective_time_limit_seconds"], 300.0, f"{label}.effective_time_limit_seconds")
        expected_design = {
            "drone_count": (int(float(row["level"])), 1.0, 4),
            "deadline_multiplier": (8, float(row["level"]), 4),
            "station_count": (8, 1.0, int(float(row["level"]))),
        }[parameter]
        if int(row["drone_count"]) != expected_design[0]:
            raise ArtefactError(f"{label}.drone_count没有遵循单因素基准")
        if not math.isclose(float(row["deadline_multiplier"]), expected_design[1], rel_tol=0.0, abs_tol=1e-12):
            raise ArtefactError(f"{label}.deadline_multiplier没有遵循单因素基准")
        if int(row["requested_station_count"]) != expected_design[2]:
            raise ArtefactError(f"{label}.requested_station_count没有遵循单因素基准")
        _validate_operator_statistics(row, label)
        solution_path = directory / str(row["cell_output_dir"]) / str(row["solution_file"])
        solution = _solution(solution_path, str(row["solution_sha256"]), f"{label}解文件")
        score = _mapping(solution["score"], f"{label}解文件.score")
        problem = _mapping(solution["problem"], f"{label}解文件.problem")
        relay_payload = _mapping(solution["relay"], f"{label}解文件.relay")
        if (
            int(problem["task_count"]) != 200
            or int(problem["drone_count"]) != int(row["drone_count"])
            or int(problem["max_tasks_per_drone"]) != 25
        ):
            raise ArtefactError(f"{label}解文件规模与敏感性行不一致")
        if all(str(home) == "origin" for home in problem["drone_homes"]):
            raise ArtefactError(f"{label}组合场景没有任何异地home")
        if int(relay_payload["relay_count"]) != int(row["requested_station_count"]):
            raise ArtefactError(f"{label}请求站点数与解文件实际站点数不一致")
        if int(row["station_count"]) != int(row["requested_station_count"]):
            raise ArtefactError(f"{label}.station_count与requested_station_count不一致")
        if int(relay_payload["relay_task_count"]) != int(row["relay_task_count"]):
            raise ArtefactError(f"{label}.relay_task_count与解文件不一致")
        if int(relay_payload["cross_uav_handoff_count"]) != int(row["cross_uav_handoff_count"]):
            raise ArtefactError(f"{label}.cross_uav_handoff_count与解文件不一致")
        if not math.isclose(
            float(relay_payload["total_relay_waiting_time"]),
            float(row["total_relay_waiting_min"]),
            rel_tol=0.0,
            abs_tol=1e-8,
        ):
            raise ArtefactError(f"{label}.total_relay_waiting_min与解文件不一致")
        metadata_statistics = _mapping(
            solution["metadata"]["operator_statistics"],
            f"{label}解文件.metadata.operator_statistics",
        )
        if metadata_statistics != row["operator_statistics"]:
            raise ArtefactError(f"{label}.operator_statistics与解文件metadata不一致")
        for key in ("late_count", "total_lateness_min", "distance_km"):
            if not math.isclose(float(row[key]), float(score[key]), rel_tol=0.0, abs_tol=1e-8):
                raise ArtefactError(f"{label}.{key}与解文件不一致")
        row["_solution"] = solution
        enriched.append(row)
    expected_cells = {
        (parameter, level)
        for parameter, parameter_levels in EXPECTED_SENSITIVITY_LEVELS.items()
        for level in parameter_levels
    }
    if seen != expected_cells:
        raise ArtefactError("敏感性runs没有完整覆盖最终三参数各五档")
    return manifest, enriched


def _validate_figure_manifest(
    figure_dir: Path,
    *,
    benchmark_dir: Path,
    scenario_dir: Path,
    sensitivity_dir: Path,
) -> None:
    manifest = _mapping(
        _load_json(figure_dir / "figure_manifest.json", "最终图像manifest"),
        "最终图像manifest",
    )
    _require_keys(
        manifest,
        (
            "schema_version",
            "route_seed",
            "figure_count",
            "output_file_count",
            "inputs",
            "outputs",
        ),
        "最终图像manifest",
    )
    if int(manifest["schema_version"]) != 1:
        raise ArtefactError("最终图像manifest.schema_version必须为1")
    if int(manifest["route_seed"]) != EXPECTED_SCENARIO_SEEDS[0]:
        raise ArtefactError("最终路线图必须固定使用seed 2026081701")
    if int(manifest["figure_count"]) != len(FIGURE_FILES) or int(
        manifest["output_file_count"]
    ) != 2 * len(FIGURE_FILES):
        raise ArtefactError("最终图像manifest的图组/文件数量不正确")
    expected_inputs = {
        "benchmark_results": benchmark_dir / "results.json",
        "scenario_runs": scenario_dir / "runs.json",
        "sensitivity_runs": sensitivity_dir / "runs.json",
    }
    input_records = _mapping(manifest["inputs"], "最终图像manifest.inputs")
    if set(input_records) != set(expected_inputs):
        raise ArtefactError("最终图像manifest.inputs与三组正式JSON不一致")
    for name, expected_path in expected_inputs.items():
        record = _mapping(input_records[name], f"最终图像manifest.inputs.{name}")
        _require_keys(record, ("absolute_path", "sha256"), f"最终图像manifest.inputs.{name}")
        if Path(str(record["absolute_path"])).resolve() != expected_path.resolve():
            raise ArtefactError(f"最终图像输入{name}的路径与正式结果目录不一致")
        if str(record["sha256"]) != _sha256(expected_path):
            raise ArtefactError(f"最终图像输入{name}的SHA-256与正式JSON不一致")
    expected_outputs = {
        filename: figure_dir / filename for filename in FIGURE_FILES
    }
    expected_outputs.update(
        {
            f"{Path(filename).stem}.svg": figure_dir / f"{Path(filename).stem}.svg"
            for filename in FIGURE_FILES
        }
    )
    output_records = manifest["outputs"]
    if not isinstance(output_records, list):
        raise ArtefactError("最终图像manifest.outputs必须为JSON数组")
    by_name: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(output_records):
        record = _mapping(item, f"最终图像manifest.outputs[{index}]")
        _require_keys(record, ("relative_path", "sha256"), f"最终图像manifest.outputs[{index}]")
        name = Path(str(record["relative_path"])).name
        if name in by_name:
            raise ArtefactError(f"最终图像manifest.outputs重复文件：{name}")
        by_name[name] = record
    if set(by_name) != set(expected_outputs):
        raise ArtefactError("最终图像manifest.outputs没有覆盖9组PNG/SVG")
    for name, path in expected_outputs.items():
        if not path.is_file() or path.stat().st_size == 0:
            raise ArtefactError(f"最终图像文件缺失或为空：{path}")
        if str(by_name[name]["sha256"]) != _sha256(path):
            raise ArtefactError(f"最终图像文件SHA-256不匹配：{path}")


def load_bundle(
    benchmark_dir: Path,
    scenario_dir: Path,
    sensitivity_dir: Path,
    figure_dir: Path,
) -> Bundle:
    for directory, label in (
        (benchmark_dir, "final_benchmarks"),
        (scenario_dir, "final_scenario_comparison"),
    ):
        if not directory.is_dir():
            raise ArtefactError(f"缺少正式结果目录{label}：{directory}")
    benchmark_manifest, benchmark_rows, benchmark_solutions = _validate_benchmarks(benchmark_dir)
    scenario_manifest, scenario_rows, scenario_summary_rows = _validate_scenarios(scenario_dir)
    sensitivity_manifest, sensitivity_rows = _validate_sensitivity(sensitivity_dir)
    for manifest, label in (
        (benchmark_manifest, "第5章manifest"),
        (scenario_manifest, "四场景manifest"),
        (sensitivity_manifest, "敏感性manifest"),
    ):
        _verify_manifest_input(manifest, label)
    input_hashes = {
        str(benchmark_manifest["input_sha256"]),
        str(scenario_manifest["input_sha256"]),
        str(sensitivity_manifest["input_sha256"]),
    }
    if len(input_hashes) != 1:
        raise ArtefactError("第5章、四场景和敏感性实验的输入SHA-256不一致")
    _validate_figure_manifest(
        figure_dir,
        benchmark_dir=benchmark_dir,
        scenario_dir=scenario_dir,
        sensitivity_dir=sensitivity_dir,
    )
    return Bundle(
        benchmark_dir=benchmark_dir,
        scenario_dir=scenario_dir,
        sensitivity_dir=sensitivity_dir,
        figure_dir=figure_dir,
        benchmark_manifest=benchmark_manifest,
        benchmark_rows=tuple(benchmark_rows),
        benchmark_solutions=benchmark_solutions,
        scenario_manifest=scenario_manifest,
        scenario_rows=tuple(scenario_rows),
        scenario_summary_rows=tuple(scenario_summary_rows),
        sensitivity_manifest=sensitivity_manifest,
        sensitivity_rows=tuple(sensitivity_rows),
    )


def _normalise_text(text: str) -> str:
    return " ".join(text.replace("\u3000", " ").split())


def _is_heading_one(paragraph: Paragraph) -> bool:
    name = paragraph.style.name.lower() if paragraph.style is not None else ""
    return name in {"heading 1", "标题 1", "标题1"} or name.endswith("heading 1")


def replaceable_range(document: DocumentObject) -> tuple[Paragraph, Paragraph]:
    chapter_four: Paragraph | None = None
    references: Paragraph | None = None
    for paragraph in document.paragraphs:
        if not _is_heading_one(paragraph):
            continue
        text = _normalise_text(paragraph.text)
        if chapter_four is None and (text.startswith("4 求解方法") or text.startswith("4. 求解方法")):
            chapter_four = paragraph
        if text == "参考文献" or text.startswith("参考文献 "):
            references = paragraph
            break
    if chapter_four is None:
        raise ArtefactError("源DOCX中没有找到第一个一级标题“4 求解方法”")
    if references is None:
        raise ArtefactError("源DOCX中没有找到一级标题“参考文献”")
    body = document._element.body
    children = list(body.iterchildren())
    if children.index(chapter_four._p) >= children.index(references._p):
        raise ArtefactError("源DOCX的第4章没有位于参考文献之前")
    return chapter_four, references


def delete_old_chapters(document: DocumentObject, start: Paragraph, references: Paragraph) -> None:
    body = document._element.body
    children = list(body.iterchildren())
    start_index = children.index(start._p)
    end_index = children.index(references._p)
    for child in children[start_index:end_index]:
        body.remove(child)


def align_preserved_front_matter(document: DocumentObject) -> None:
    """Resolve cross-chapter claims that survive the Chapter 4 replacement."""

    replacements = {
        "结合题设小规模示例、随机实例与现实数据开展数值实验": (
            "结合题设两任务示例、正式数据前缀实例与给定200任务数据开展数值实验"
        ),
        "第 5 节报告数值实验结果；第 6 节总结全文并提出展望。": (
            "第 5 节报告算法有效性实验；第 6 节给出正式应用案例与敏感性分析；"
            "第 7 节总结全文并提出展望。"
        ),
        "对应内容分别见第3—5节。": "对应内容分别见第3—6节。",
        "有限的汇合接力": "经固定中继站的异步接力",
        "嵌入汇合接力这一可选协同策略": "嵌入固定中继站异步接力这一可选协同策略",
        "考虑可选汇合接力": "考虑可选的固定中继站异步接力",
        "与本文的汇合接力策略相关": "与本文的固定中继站异步接力策略相关",
        "中继站可短时缓存包裹": "中继站可缓存包裹",
        "汇合接力策略的扩展数学刻画": "固定中继站异步接力策略的扩展数学刻画",
        "启用汇合接力的任务对": "启用固定中继站接力的任务对",
        "在统一可行性判定框架下": "在统一约束口径和分层评价实现下",
        "从共同起点出发": "从调度中心或预部署起点出发",
        "中继站仅承担短时缓存与交接，不设置库存容量和长期库存决策": (
            "中继站承担缓存与交接；当前模型不设置最大缓存时长、库存容量、"
            "服务容量与长期库存决策"
        ),
        "若包裹先到，则在站内暂存至末程取货": "若包裹先到，则在站内缓存至末程取货",
        "包裹先到时则短时暂存": "包裹先到时则在站内缓存",
        "接力任务则按照3.3.7的局部可行性规则校验。": (
            "接力任务不使用上述直接服务不等式剪枝，其交接时序按照3.3.7的"
            "全局DAG规则校验。"
        ),
        "任务不相容关系可在求解前预计算，用于强化基础模型并辅助任务插入与可行性判定。": (
            "该关系仅用于公共原点Direct模型强化；当前ALNS不调用，"
            "异地home需重定义。"
        ),
    }
    remaining = set(replacements)
    for paragraph in document.paragraphs:
        # Replace only the text-bearing run that contains the phrase.  Assigning
        # to paragraph.text would rebuild the whole paragraph and discard any
        # sibling Office Math nodes (for example the inline \ell in Section 3).
        for run in paragraph.runs:
            for source, target in replacements.items():
                if source not in run.text:
                    continue
                run.text = run.text.replace(source, target)
                remaining.discard(source)
    if remaining:
        raise ArtefactError(
            "源DOCX保留章节中缺少预期的对齐文本：" + "；".join(sorted(remaining))
        )


def normalise_preserved_math_and_symbol_table(document: DocumentObject) -> None:
    """Make preserved Office Math portable and keep Table 3-1 formulas visible.

    LibreOffice imports the four literal ``\\{`` Office Math tokens in the
    source as error markers. A semantic set-minus glyph avoids that renderer-
    specific ambiguity without changing the formulas. The first column of
    Table 3-1 also needs enough width for its longest symbol definitions.
    """

    replacements = 0
    for node in document.element.iter(qn("m:t")):
        if node.text != r"\{":
            continue
        node.text = "∖{"
        replacements += 1
    if replacements != 4:
        raise ArtefactError(
            "源DOCX中预期有4个集合差公式标记，实际为"
            f"{replacements}个；拒绝静默改写数学内容"
        )

    symbol_tables = [
        table
        for table in document.tables
        if len(table.columns) == 2
        and table.rows
        and tuple(cell.text.strip() for cell in table.rows[0].cells)
        == ("符号", "含义")
    ]
    if len(symbol_tables) != 1:
        raise ArtefactError(
            "源DOCX中应恰有1个表3-1符号说明表，实际为"
            f"{len(symbol_tables)}个"
        )
    section = document.sections[-1]
    available_width = int(section.page_width - section.left_margin - section.right_margin)
    _set_table_width(symbol_tables[0], available_width, (3.5, 5.835))
    for row in symbol_tables[0].rows:
        _set_cant_split(row)
    _set_repeat_table_header(symbol_tables[0].rows[0])


def _set_rfonts(element: Any, east_asia: str, latin: str = "Times New Roman") -> None:
    r_pr = element.get_or_add_rPr()
    r_fonts = r_pr.rFonts
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.insert(0, r_fonts)
    for attribute in ("ascii", "hAnsi", "cs"):
        r_fonts.set(qn(f"w:{attribute}"), latin)
    r_fonts.set(qn("w:eastAsia"), east_asia)


def configure_styles(document: DocumentObject) -> None:
    for style_name in ("Normal", "Body Text", "Body Text 2", "First Paragraph", "List Paragraph"):
        if style_name not in document.styles:
            continue
        style = document.styles[style_name]
        style.font.name = "Times New Roman"
        _set_rfonts(style.element, "Songti SC")
    for style_name in ("Heading 1", "Heading 2", "Heading 3", "Heading 4"):
        if style_name not in document.styles:
            continue
        style = document.styles[style_name]
        style.font.name = "Times New Roman"
        _set_rfonts(style.element, "Heiti SC")
    if "Caption" in document.styles:
        caption = document.styles["Caption"]
        caption.font.name = "Times New Roman"
        _set_rfonts(caption.element, "Songti SC")
        caption.font.size = Pt(9)
        caption.font.bold = False
        caption.font.italic = False
        caption.font.color.rgb = RGBColor(0, 0, 0)


def _format_run(run: Any, *, heading: bool = False, size: float | None = None, bold: bool | None = None) -> None:
    run.font.name = "Times New Roman"
    if bold is not None:
        run.bold = bold
    if size is not None:
        run.font.size = Pt(size)
    r_pr = run._element.get_or_add_rPr()
    r_fonts = r_pr.rFonts
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.insert(0, r_fonts)
    for attribute in ("ascii", "hAnsi", "cs"):
        r_fonts.set(qn(f"w:{attribute}"), "Times New Roman")
    r_fonts.set(qn("w:eastAsia"), "Heiti SC" if heading else "Songti SC")


def _safe_style(document: DocumentObject, preferred: str, fallback: str = "Normal") -> str:
    return preferred if preferred in document.styles else fallback


def _set_repeat_table_header(row: Any) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    tr_pr.append(tbl_header)


def _set_cant_split(row: Any) -> None:
    tr_pr = row._tr.get_or_add_trPr()
    if tr_pr.find(qn("w:cantSplit")) is None:
        tr_pr.append(OxmlElement("w:cantSplit"))


def _compact_cell_paragraph(
    paragraph: Paragraph,
    *,
    alignment: WD_ALIGN_PARAGRAPH,
    keep_with_next: bool = False,
) -> None:
    paragraph.alignment = alignment
    paragraph.paragraph_format.first_line_indent = Emu(0)
    paragraph.paragraph_format.left_indent = Emu(0)
    paragraph.paragraph_format.right_indent = Emu(0)
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.0
    paragraph.paragraph_format.keep_with_next = keep_with_next
    paragraph.paragraph_format.widow_control = True


def _shade_cell(cell: Any, fill: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shading = tc_pr.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        tc_pr.append(shading)
    shading.set(qn("w:fill"), fill)


def _set_cell_margins(cell: Any, top: int = 70, start: int = 90, bottom: int = 70, end: int = 90) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    tc_mar = tc_pr.first_child_found_in("w:tcMar")
    if tc_mar is None:
        tc_mar = OxmlElement("w:tcMar")
        tc_pr.append(tc_mar)
    for margin_name, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = tc_mar.find(qn(f"w:{margin_name}"))
        if node is None:
            node = OxmlElement(f"w:{margin_name}")
            tc_mar.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def _set_table_width(table: Table, total_width: int, ratios: Sequence[float]) -> None:
    if len(ratios) != len(table.columns) or not all(value > 0 for value in ratios):
        raise ValueError("表格列宽比例无效")
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl_pr = table._tbl.tblPr
    tbl_w = tbl_pr.find(qn("w:tblW"))
    if tbl_w is None:
        tbl_w = OxmlElement("w:tblW")
        tbl_pr.append(tbl_w)
    tbl_w.set(qn("w:w"), str(round(total_width / 635)))
    tbl_w.set(qn("w:type"), "dxa")
    denominator = sum(ratios)
    widths = [int(total_width * value / denominator) for value in ratios]
    for column, width in zip(table.columns, widths, strict=True):
        column.width = Emu(width)
    for row in table.rows:
        for cell, width in zip(row.cells, widths, strict=True):
            cell.width = Emu(width)


def _score(row: Mapping[str, Any]) -> tuple[int, float, float]:
    return (
        int(row["late_count"]),
        float(row["total_lateness_min"]),
        float(row["distance_km"]),
    )


def _fmt(value: Any, digits: int = 2) -> str:
    number = _number(value, "待排版数值")
    if math.isclose(number, round(number), abs_tol=10 ** (-(digits + 1))):
        return str(int(round(number)))
    return f"{number:.{digits}f}"


def _fmt_score(row: Mapping[str, Any], digits: int = 2) -> str:
    late, lateness, distance = _score(row)
    return f"({late}, {lateness:.{digits}f}, {distance:.{digits}f})"


def _mean_std(values: Sequence[float], digits: int = 2) -> str:
    mean = statistics.fmean(values)
    deviation = statistics.stdev(values) if len(values) > 1 else 0.0
    return f"{mean:.{digits}f}±{deviation:.{digits}f}"


def _soft_wrap_hash(value: Any, group_size: int = 8) -> str:
    """Expose harmless line-break opportunities without changing hash content."""

    text = str(value)
    return "\u200b".join(
        text[index : index + group_size]
        for index in range(0, len(text), group_size)
    )


class ManuscriptWriter:
    def __init__(self, document: DocumentObject, anchor: Paragraph, figure_dir: Path) -> None:
        self.document = document
        self.anchor = anchor
        self.figure_dir = figure_dir
        section = document.sections[-1]
        self.available_width = int(section.page_width - section.left_margin - section.right_margin)

    def paragraph(
        self,
        text: str,
        *,
        style: str = "Normal",
        align: WD_ALIGN_PARAGRAPH | None = None,
        bold: bool = False,
        keep_with_next: bool = False,
        first_line_indent: bool = True,
    ) -> Paragraph:
        paragraph = self.anchor.insert_paragraph_before(style=_safe_style(self.document, style))
        run = paragraph.add_run(text)
        _format_run(run, bold=bold)
        if align is not None:
            paragraph.alignment = align
        paragraph.paragraph_format.keep_with_next = keep_with_next
        paragraph.paragraph_format.widow_control = True
        if not first_line_indent:
            paragraph.paragraph_format.first_line_indent = Emu(0)
        return paragraph

    def rich_paragraph(
        self,
        parts: Sequence[tuple[str, bool]],
        *,
        style: str = "Normal",
        align: WD_ALIGN_PARAGRAPH | None = None,
    ) -> Paragraph:
        paragraph = self.anchor.insert_paragraph_before(style=_safe_style(self.document, style))
        for text, bold in parts:
            run = paragraph.add_run(text)
            _format_run(run, bold=bold)
        if align is not None:
            paragraph.alignment = align
        paragraph.paragraph_format.widow_control = True
        return paragraph

    def heading(self, text: str, level: int, *, page_break: bool = False) -> Paragraph:
        paragraph = self.anchor.insert_paragraph_before(style=f"Heading {level}")
        run = paragraph.add_run(text)
        _format_run(run, heading=True, bold=True)
        paragraph.paragraph_format.keep_with_next = True
        paragraph.paragraph_format.widow_control = True
        if page_break:
            paragraph.paragraph_format.page_break_before = True
        return paragraph

    def bullet(self, text: str) -> Paragraph:
        style = _safe_style(self.document, "List Bullet")
        if style == "List Bullet":
            return self.paragraph(text, style=style)
        paragraph = self.paragraph("• " + text, style=style, first_line_indent=False)
        paragraph.paragraph_format.left_indent = Pt(21)
        paragraph.paragraph_format.first_line_indent = Pt(-10.5)
        return paragraph

    def caption(self, text: str, *, keep_with_next: bool = False) -> Paragraph:
        paragraph = self.paragraph(
            text,
            style=_safe_style(self.document, "Caption"),
            align=WD_ALIGN_PARAGRAPH.CENTER,
            keep_with_next=keep_with_next,
            first_line_indent=False,
        )
        for run in paragraph.runs:
            _format_run(run, size=9, bold=False)
            run.font.italic = False
            run.font.color.rgb = RGBColor(0, 0, 0)
        return paragraph

    def table(
        self,
        caption: str,
        headers: Sequence[str],
        rows: Sequence[Sequence[Any]],
        *,
        ratios: Sequence[float] | None = None,
        font_size: float = 8.5,
    ) -> Table:
        if not headers or any(len(row) != len(headers) for row in rows):
            raise ValueError(f"表格{caption}的列数不一致")
        self.caption(caption, keep_with_next=True)
        table = self.document.add_table(rows=1, cols=len(headers))
        table.style = _safe_style(self.document, "Table", "Table Grid")
        self.anchor._p.addprevious(table._tbl)
        ratios = ratios or [1.0] * len(headers)
        _set_table_width(table, self.available_width, ratios)
        header = table.rows[0]
        _set_cant_split(header)
        _set_repeat_table_header(header)
        for cell, text in zip(header.cells, headers, strict=True):
            cell.text = str(text)
            _shade_cell(cell, "D9E2F3")
            _set_cell_margins(cell)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for paragraph in cell.paragraphs:
                _compact_cell_paragraph(
                    paragraph,
                    alignment=WD_ALIGN_PARAGRAPH.CENTER,
                    keep_with_next=True,
                )
                for run in paragraph.runs:
                    _format_run(run, size=font_size, bold=True)
        for source_row in rows:
            row = table.add_row()
            _set_cant_split(row)
            for cell, value in zip(row.cells, source_row, strict=True):
                cell.text = "—" if value is None else str(value)
                _set_cell_margins(cell)
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                rendered = "—" if value is None else str(value)
                alignment = (
                    WD_ALIGN_PARAGRAPH.CENTER
                    if isinstance(value, (int, float, bool)) or len(rendered) <= 14
                    else WD_ALIGN_PARAGRAPH.LEFT
                )
                for paragraph in cell.paragraphs:
                    _compact_cell_paragraph(paragraph, alignment=alignment)
                    for run in paragraph.runs:
                        _format_run(run, size=font_size)
        return table

    def figure(self, filename: str, caption: str, *, width_fraction: float = 0.94) -> None:
        path = self.figure_dir / filename
        paragraph = self.anchor.insert_paragraph_before()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.first_line_indent = Emu(0)
        paragraph.paragraph_format.left_indent = Emu(0)
        paragraph.paragraph_format.right_indent = Emu(0)
        paragraph.paragraph_format.space_before = Pt(0)
        paragraph.paragraph_format.space_after = Pt(0)
        paragraph.paragraph_format.keep_with_next = True
        run = paragraph.add_run()
        shape = run.add_picture(
            str(path),
            width=Emu(int(self.available_width * width_fraction)),
        )
        shape._inline.docPr.set("descr", caption)
        shape._inline.docPr.set("title", filename)
        self.caption(caption)

    def flow_figure(self, caption: str, steps: Sequence[str]) -> None:
        table = self.document.add_table(rows=0, cols=1)
        table.style = _safe_style(self.document, "Table", "Table Grid")
        self.anchor._p.addprevious(table._tbl)
        _set_table_width(table, int(self.available_width * 0.86), (1.0,))
        for index, step in enumerate(steps):
            cell = table.add_row().cells[0]
            _set_cant_split(table.rows[-1])
            cell.text = step
            _shade_cell(cell, "EEF3F8" if index % 2 == 0 else "FFFFFF")
            _set_cell_margins(cell, top=95, bottom=95)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            for paragraph in cell.paragraphs:
                _compact_cell_paragraph(
                    paragraph,
                    alignment=WD_ALIGN_PARAGRAPH.CENTER,
                )
                for run in paragraph.runs:
                    _format_run(run, size=9, bold=index in {0, len(steps) - 1})
            if index < len(steps) - 1:
                arrow = table.add_row().cells[0]
                _set_cant_split(table.rows[-1])
                arrow.text = "↓"
                for paragraph in arrow.paragraphs:
                    _compact_cell_paragraph(
                        paragraph,
                        alignment=WD_ALIGN_PARAGRAPH.CENTER,
                    )
                    for run in paragraph.runs:
                        _format_run(run, size=9)
        for row in table.rows:
            for paragraph in row.cells[-1].paragraphs[-1:]:
                paragraph.paragraph_format.keep_with_next = True
        _set_repeat_table_header(table.rows[0])
        self.caption(caption)

    def three_panel_figure(self, caption: str, panels: Sequence[tuple[str, str]]) -> None:
        if len(panels) != 3:
            raise ValueError("三联示意图必须有三个面板")
        table = self.document.add_table(rows=2, cols=3)
        table.style = _safe_style(self.document, "Table", "Table Grid")
        self.anchor._p.addprevious(table._tbl)
        _set_table_width(table, self.available_width, (1.0, 1.0, 1.0))
        for index, (title, text) in enumerate(panels):
            title_cell = table.rows[0].cells[index]
            title_cell.text = title
            _shade_cell(title_cell, "D9E2F3")
            body_cell = table.rows[1].cells[index]
            body_cell.text = text
            for cell in (title_cell, body_cell):
                _set_cell_margins(cell, top=110, bottom=110)
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                for paragraph in cell.paragraphs:
                    _compact_cell_paragraph(
                        paragraph,
                        alignment=WD_ALIGN_PARAGRAPH.CENTER,
                    )
                    for run in paragraph.runs:
                        _format_run(run, size=8.5, bold=cell is title_cell)
        for row in table.rows:
            _set_cant_split(row)
        _set_repeat_table_header(table.rows[0])
        for row in table.rows:
            for cell in row.cells:
                for paragraph in cell.paragraphs[-1:]:
                    paragraph.paragraph_format.keep_with_next = True
        self.caption(caption)


def _scenario_groups(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        scenario: sorted(
            (row for row in rows if row["scenario"] == scenario),
            key=lambda row: int(row["seed"]),
        )
        for scenario in SCENARIO_IDS
    }


def _scenario_summary(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, float]]:
    groups = _scenario_groups(rows)
    fields = (
        "late_count",
        "total_lateness_min",
        "distance_km",
        "wall_seconds",
        "relay_task_count",
        "cross_uav_handoff_count",
        "deadhead_km",
        "home_assignment_rate",
    )
    result: dict[str, dict[str, float]] = {}
    for scenario, selected in groups.items():
        values: dict[str, float] = {}
        for field in fields:
            series = [float(row[field]) for row in selected]
            values[f"mean_{field}"] = statistics.fmean(series)
            values[f"std_{field}"] = statistics.stdev(series) if len(series) > 1 else 0.0
        ips = [
            float(_mapping(row["_solution"]["metadata"], "solution.metadata")["iterations_per_second"])
            for row in selected
        ]
        values["mean_iterations_per_second"] = statistics.fmean(ips)
        result[scenario] = values
    return result


def _paired_comparisons(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    groups = _scenario_groups(rows)
    baseline = {int(row["seed"]): row for row in groups["pure_direct"]}
    comparisons: list[dict[str, Any]] = []
    for scenario in SCENARIO_IDS[1:]:
        for row in groups[scenario]:
            base = baseline[int(row["seed"])]
            outcome = "胜" if _score(row) < _score(base) else ("负" if _score(row) > _score(base) else "平")
            comparisons.append(
                {
                    "scenario": scenario,
                    "seed": int(row["seed"]),
                    "baseline_score": _fmt_score(base),
                    "scenario_score": _fmt_score(row),
                    "late_delta": int(row["late_count"]) - int(base["late_count"]),
                    "lateness_delta": float(row["total_lateness_min"]) - float(base["total_lateness_min"]),
                    "distance_delta": float(row["distance_km"]) - float(base["distance_km"]),
                    "outcome": outcome,
                }
            )
    return comparisons


def _operator_aggregate(bundle: Bundle) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    weights: dict[str, list[float]] = defaultdict(list)
    statistic_rows = [
        row for row in (*bundle.benchmark_rows, *bundle.scenario_rows)
        if row.get("operator_statistics")
    ]
    for row in statistic_rows:
        for operator, values in row["operator_statistics"].items():
            for field in ("uses", "accepted", "current_improvements", "best_improvements", "total_reward"):
                totals[operator][field] += float(values[field])
    solutions = [*bundle.benchmark_solutions.values()] + [row["_solution"] for row in bundle.scenario_rows]
    for solution in solutions:
        metadata = _mapping(solution["metadata"], "solution.metadata")
        operator_weights = _mapping(metadata.get("operator_weights"), "solution.metadata.operator_weights")
        for operator in OPERATOR_KEYS:
            weights[operator].append(_number(operator_weights[operator], f"operator_weights.{operator}"))
    result: list[dict[str, Any]] = []
    for operator in OPERATOR_KEYS:
        values = totals[operator]
        uses = values["uses"]
        if uses <= 0:
            raise ArtefactError(f"正式统计中算子{operator}没有任何调用记录")
        result.append(
            {
                "type": "破坏" if operator.startswith("destroy:") else "修复",
                "name": operator.split(":", 1)[1],
                "uses": int(uses),
                "accepted": int(values["accepted"]),
                "acceptance_rate": 100.0 * values["accepted"] / uses if uses else 0.0,
                "current": int(values["current_improvements"]),
                "best": int(values["best_improvements"]),
                "reward": values["total_reward"],
                "mean_reward": values["total_reward"] / uses if uses else 0.0,
                "mean_weight": statistics.fmean(weights[operator]),
            }
        )
    return result


def _sensitivity_groups(rows: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        parameter: sorted(
            (row for row in rows if row["parameter"] == parameter),
            key=lambda row: float(row["level"]),
        )
        for parameter in SENSITIVITY_PARAMETERS
    }


def _sensitivity_finding(parameter: str, rows: Sequence[dict[str, Any]]) -> str:
    best = min(rows, key=_score)
    first, last = rows[0], rows[-1]
    label = {
        "drone_count": "无人机数量",
        "deadline_multiplier": "截止期倍率",
        "station_count": "中继站数量",
    }[parameter]
    return (
        f"在本次单种子、单因素取值范围内，{label}的字典序最优观测点为"
        f"{_fmt(best['level'])}，得分为{_fmt_score(best)}。从最低水平到最高水平，"
        f"逾期任务数由{int(first['late_count'])}变为{int(last['late_count'])}，"
        f"总逾期由{float(first['total_lateness_min']):.2f} min变为"
        f"{float(last['total_lateness_min']):.2f} min，航程由"
        f"{float(first['distance_km']):.2f} km变为{float(last['distance_km']):.2f} km。"
        "该比较只描述本固定种子下的方向，不作显著性推断。"
    )


def write_chapter_four(writer: ManuscriptWriter, bundle: Bundle) -> None:
    writer.heading("4 求解方法", 1, page_break=True)
    writer.paragraph(
        "本文采用regret-2构造完整可行初始解，以单一自适应大邻域搜索"
        "（adaptive large neighborhood search, ALNS）优化任务分配和访问顺序，"
        "并在多机场景中加入固定中继站接力与需求驱动预部署。候选解从构造、"
        "搜索到最终校验始终按第3章定义的（逾期任务数、总逾期时长、总航程）"
        "严格字典序比较，不以加权和改变服务优先级。"
    )

    writer.heading("4.1 初始解构造与分层可行性控制", 2)
    writer.paragraph(
        "初始解既要在较短时间内覆盖全部任务，也要避免过早固化任务归属。"
        "构造开始时，所有任务均以直送计划进入候选集合；原点场景中各架无人机"
        "从调度中心出发，Station场景则先固定每条路线对应的实际起点home。"
    )
    writer.paragraph(
        "原点场景直接对尚未插入的原始任务执行regret-2；Station场景先为每架"
        "无人机播种一个取件点最接近其home的任务，再对剩余任务执行regret-2。"
        "regret-2枚举满足先取后送的成对插入位置，分别取得最佳与次佳可行插入"
        "结果，并优先处理失去当前最佳位置后代价上升最大的任务。插入增量按"
        "三层得分比较，同时维护容量、单机任务上限和取送配对；异地起点场景"
        "的首段距离由实际home到首个访问节点计算。构造结束后，独立完整评价器"
        "再次验证任务唯一服务、容量、先取后送与全局接力时序。"
    )
    writer.paragraph(
        "搜索中的可行性控制分层实现：修复算子只从局部可行的成对插入中选择，"
        "从而维护路线容量、配对和任务上限；启用Relay时，全局评价器把路线续接边"
        "与中继“卸货→取货”交接边合并为有向图，用于检测循环等待、按拓扑序传播"
        "服务时刻并计入站内等待。初始解与最终最优解另由独立完整评价器重新检查"
        "任务唯一服务和全部约束。逾期只在任务最终送达节点完成时判定。"
    )

    writer.heading("4.2 自适应大邻域搜索主算法", 2)
    writer.paragraph(
        "ALNS的一次迭代依次完成算子选择、完整任务移除、完整方案修复、精确评价、"
        "接受判定与权重更新。破坏过程从不只删除取件或送达的一半；修复过程也"
        "必须把取送节点作为同一任务恢复。候选由局部可行插入结构维护；Relay"
        "候选再经DAG时序评价后进入接受准则和最优解比较。搜索结束时，best解必须"
        "通过独立完整评价器，非法时直接报错且不输出结果。"
    )
    operator_table_rows: list[list[str]] = []
    for category, names in (("破坏", DESTROY_OPERATORS), ("修复", REPAIR_OPERATORS)):
        for name in names:
            selection, changed, guard = OPERATOR_DESCRIPTIONS[name]
            operator_table_rows.append([category, name, selection, changed, guard, "是"])
    writer.table(
        "表4-1 ALNS核心算子与作用",
        ("类型", "注册键", "选择依据", "改变内容", "可行性保护", "最终启用"),
        operator_table_rows,
        ratios=(0.55, 1.05, 1.75, 1.45, 1.5, 0.65),
        font_size=7.4,
    )
    writer.paragraph(
        "最终注册表共含14个核心算子，其中9个破坏算子负责打开不同类型的搜索"
        "邻域，5个修复算子负责恢复完整可行方案。轮盘赌按当前权重选择一对算子；"
        "系统根据候选是否被接受、是否改善当前解以及是否刷新全局最优分配分层"
        "奖励，并在固定区段内平滑更新权重。模拟退火式接受准则允许有限的劣解"
        "跳转，墙钟时间是主要终止条件，最大迭代数仅作为安全上界。"
    )
    writer.paragraph(
        "每次正式运行保存uses、accepted、current_improvements、"
        "best_improvements、total_reward及最终权重，供第5.4节复核。最终主算法"
        "不调用VND、Ejection Swap、Route Pool或Cluster Regret，也不将"
        "home_displaced与route_clear计入注册算子或实验结论。"
    )

    writer.heading("4.3 固定中继站接力与跨机交接", 2)
    writer.paragraph(
        "每项任务在DIRECT与RELAY(h)两类服务计划中唯一选择一种。DIRECT由同一架"
        "无人机连续完成取件与送达；RELAY(h)通过候选固定中继站h拆成入站"
        "RELAY_IN与出站RELAY_OUT两条运输腿，两腿可以由同一架或不同无人机执行。"
    )
    writer.paragraph(
        "交接采用异步先卸后取语义：入站无人机完成卸货后，包裹可在站内缓存；"
        "出站无人机提前到站时等待卸货完成。因此只要求中继取货时刻不早于卸货"
        "时刻，并不要求两架无人机同时到站。两腿由不同无人机完成时计一次"
        "cross_uav_handoff；由同一无人机完成时计为same_uav_relay，但仍保留"
        "经站绕行和完整时序。"
    )
    writer.paragraph(
        "Relay搜索以强DIRECT incumbent为起点，通过候选站筛选、中继播种和周期性"
        "跨机精化扩展服务计划。任何接力候选均须通过全局DAG无环检查和时间传播，"
        "然后再参与三层目标比较。正式Relay协议把可用搜索时间约按80%/20%分为"
        "DIRECT预热和Relay扩展；名义300 s时约对应240 s与60 s，实际分段还会扣除"
        "构造时间和安全余量。"
    )

    writer.heading("4.4 需求驱动的无人机初始预部署", 2)
    writer.paragraph(
        "预部署候选起点由调度中心和固定中继站组成。算法综合取件点距离的平滑"
        "衰减、截止期紧迫度与服务价值计算各候选点的需求得分，再把连续需求份额"
        "转换为无人机整数配额。Hamilton最大余数法保证配额总数严格等于机队规模；"
        "同一站点可以部署0架、1架或多架无人机，并以软性单点上限避免运力过度集中。"
    )
    writer.paragraph(
        "随后通过轻量局部调整优化由需求加权首段距离与超额负载不均衡惩罚组成的"
        "替代目标，因此不保证每一步都单独缩短首段距离。预部署结果在一次求解"
        "开始前固定，初始构造、插入增量、路线评价和最终校验都按路线索引读取"
        "实际home。某站点作为无人机起点与其是否实际承担Relay交接是两个独立决定，"
        "不能把“部署在站点”解释为“必然经该站交接”。"
    )
    writer.flow_figure(
        "图4-1 总体求解流程",
        (
            "任务数据、调度中心与候选固定中继网络",
            "按场景固定无人机home：原点或需求驱动Station预部署",
            "Station场景home-first播种；对其余任务regret-2构造完整DIRECT初始解",
            "Relay关闭：单阶段ALNS；Relay开启：约80% DIRECT预热 + 20% Relay搜索",
            "局部路线校验 + 全局DAG无环与时刻传播",
            "按（逾期数，总逾期，航程）严格字典序保留最优解",
        ),
    )
    writer.three_panel_figure(
        "图4-2 固定中继交接与异地起点示意",
        (
            ("DIRECT", "home_k → P_i → D_i\n同一架无人机完成"),
            ("同机经站", "home_k → P_i → h卸货 → h取货 → D_i\n仍计经站等待与绕行"),
            ("跨机接力", "UAV k：home_k → P_i → h卸货\nUAV l：home_l → h取货 → D_i\n取货时刻 ≥ 卸货时刻"),
        ),
    )
    writer.paragraph(
        "图4-2同时强调两个解耦关系：中继站可以承担交接而不部署无人机，部署在"
        "某站的无人机也不必在该站发生交接。算法细节、边界处理和伪代码见附录A。"
    )


def write_chapter_five(writer: ManuscriptWriter, bundle: Bundle) -> None:
    manifest = bundle.benchmark_manifest
    rows = list(bundle.benchmark_rows)
    writer.heading("5 数值实验与算法有效性", 1, page_break=True)
    writer.paragraph(
        "本章以正式final_benchmarks结果验证模型、精确求解器、ALNS主算法和14个"
        "核心算子，不把200任务业务策略差异提前解释为机制结论。实验包括单机小规模"
        "精确对照与截止期统一放宽的多机规模扩展两组；除图5-1为题设路线示意外，"
        "其余结果表图均直接读取正式JSON。"
    )

    writer.heading("5.1 实验设计、实例与评价指标", 2)
    exact_sizes = [int(value) for value in manifest["exact_sizes"]]
    scale_sizes = [int(value) for value in manifest["multiscale_task_counts"]]
    title_row = next(row for row in rows if row["experiment"] == "title_example_n2")
    settings_rows: list[list[Any]] = [
        ["题设距离矩阵", 2, 1, "原始截止期", "—", "Pareto-DP", "是"],
        [
            "正式数据前缀",
            ", ".join(map(str, exact_sizes)),
            1,
            "原始截止期",
            manifest["seed"],
            "Pareto-DP / C2-Lex-ALNS",
            "是",
        ],
        [
            "宽松多机扩展",
            ", ".join(map(str, scale_sizes)),
            "ceil(N/25)",
            f"原始截止期×{_fmt(manifest['relaxed_deadline_multiplier'])}",
            manifest["seed"],
            f"C2-Lex-ALNS，{_fmt(manifest['scale_seconds'])} s",
            "否",
        ],
    ]
    writer.table(
        "表5-1 实例与实验设置",
        ("实例组", "任务数", "无人机数", "截止期规则", "种子", "方法/预算", "精确解"),
        settings_rows,
        ratios=(1.25, 1.05, 0.8, 1.5, 1.0, 1.75, 0.7),
        font_size=8.0,
    )
    writer.paragraph(
        "评价按三层目标依次报告late_count、total_lateness_min和distance_km，并"
        "同时保留valid、runtime_seconds、iterations和算子统计。若前一层不同，"
        "后一层指标不能用来逆转比较。多机扩展仅有一个固定种子，因而不绘制"
        "误差线，也不把单次运行解释为稳定性证明。"
    )

    writer.heading("5.2 单机小算例与精确解对照", 2)
    writer.paragraph(
        f"题设两任务距离矩阵由Pareto标签动态规划求得路线"
        f"0→P1→P2→D2→D1，正式结果为{_fmt_score(title_row)}，其中总里程"
        f"{float(title_row['distance_km']):.0f} km。该结果说明即使每个任务内部"
        "必须先取后送，不同任务之间仍可交叉安排访问顺序以缩短航程。"
    )
    writer.figure("fig5_1_title_example.png", "图5-1 题设两任务最优路线")
    exact_rows: list[list[Any]] = []
    match_count = 0
    for size in exact_sizes:
        exact = next(
            row for row in rows
            if row["task_count"] == size and row["method"] == "Pareto-DP"
        )
        heuristic = next(
            row for row in rows
            if row["task_count"] == size and row["method"] == "C2-Lex-ALNS"
            and row["deadline_multiplier"] == 1.0
        )
        matched = bool(heuristic.get("matches_oracle"))
        match_count += int(matched)
        exact_rows.append(
            [
                size,
                _fmt_score(exact, 3),
                f"{float(exact['runtime_seconds']):.4f}",
                _fmt_score(heuristic, 3),
                f"{float(heuristic['runtime_seconds']):.4f}",
                "相同" if matched else "不同",
                "是" if heuristic["valid"] else "否",
            ]
        )
    writer.table(
        "表5-2 单机精确解与ALNS对照",
        ("任务数", "精确三层得分", "精确时间/s", "ALNS三层得分", "ALNS时间/s", "与精确解", "有效"),
        exact_rows,
        ratios=(0.65, 1.65, 0.85, 1.65, 0.85, 0.85, 0.55),
        font_size=7.7,
    )
    writer.paragraph(
        f"在{len(exact_sizes)}个正式小规模前缀实例中，ALNS有{match_count}个达到"
        "精确三层得分。Pareto动态规划在同一状态下保留非支配前缀，因为更短的"
        "累计距离同时意味着更早到达；只保留单一局部标签可能丢失未来更优路径。"
        "该对照验证的是已覆盖小规模上的结果一致性，不把这一结论外推为大规模全局最优。"
    )
    writer.figure("fig5_2_exact_validation.png", "图5-2 小规模精确求解时间与ALNS一致性")

    writer.heading("5.3 较宽松多机实例的规模扩展", 2)
    writer.paragraph(
        f"规模扩展把截止期统一乘以{_fmt(manifest['relaxed_deadline_multiplier'])}，"
        "所有无人机从原点出发并关闭Relay，以隔离ALNS主算法本身。各规模的无人机"
        "数量按ceil(N/25)设置，构造与搜索合计名义预算为"
        f"{_fmt(manifest['scale_seconds'])} s；此实验只验证在较宽松条件下的"
        "任务分配、路线构造与可行性。"
    )
    scale_rows: list[list[Any]] = []
    for count in scale_sizes:
        row = next(item for item in rows if item["experiment"] == f"relaxed_multiuav_n{count}")
        runtime = float(row["runtime_seconds"])
        iterations = int(row["iterations"])
        scale_rows.append(
            [
                count,
                row["drone_count"],
                row["seed"],
                row["late_count"],
                f"{float(row['total_lateness_min']):.3f}",
                f"{float(row['distance_km']):.3f}",
                f"{runtime:.2f}",
                iterations,
                f"{iterations / runtime:.1f}" if runtime > 0 else "—",
                "是" if row["valid"] else "否",
            ]
        )
    writer.table(
        "表5-3 较宽松多机实例结果",
        ("任务", "无人机", "种子", "逾期数", "总逾期/min", "航程/km", "时间/s", "迭代", "迭代/s", "有效"),
        scale_rows,
        ratios=(0.6, 0.65, 1.05, 0.65, 0.85, 0.9, 0.75, 0.8, 0.75, 0.55),
        font_size=7.2,
    )
    writer.paragraph(
        "表5-3的每一行均记录为通过完整约束校验。表中的runtime_seconds仅为"
        "solve_alns搜索阶段时间；构造时间虽从30 s名义预算中扣除，但当前第5章"
        "JSON未单列construction_seconds或总wall_seconds，因而不能据此复核完整"
        "墙钟。迭代速度按实际迭代数除以搜索阶段时间计算。由于每个规模只有一次"
        "运行，图5-3仅连接"
        "观测点，用于呈现规模变化，不提供方差或显著性。"
    )
    writer.figure("fig5_3_relaxed_multiscale.png", "图5-3 宽松截止期多机规模扩展结果")

    writer.heading("5.4 核心算子有效性", 2)
    operator_rows = _operator_aggregate(bundle)
    writer.paragraph(
        "算子有效性使用正式小规模ALNS、多机扩展和四场景运行中保存的统计量汇总。"
        "表5-4把调用、接受、改进和奖励先跨运行累加，因此迭代更多的运行权重更高；"
        "图5-4则先在每次运行内把排他性结果转为百分比，再对运行等权平均。最终权重"
        "仅取有完整solution文件的4个多机扩展与12个四场景运行做算术平均；4个"
        "小规模ALNS行没有序列化末权重，因而不进入该列。两种口径和样本范围回答"
        "的问题不同，不应逐格等同。"
    )
    writer.table(
        "表5-4 核心算子运行统计",
        ("类型", "算子", "调用", "接受", "接受率/%", "当前改进", "全局改进", "累计奖励", "次均奖励", "平均末权重"),
        [
            [
                row["type"], row["name"], row["uses"], row["accepted"],
                f"{row['acceptance_rate']:.2f}", row["current"], row["best"],
                f"{row['reward']:.1f}", f"{row['mean_reward']:.3f}", f"{row['mean_weight']:.3f}",
            ]
            for row in operator_rows
        ],
        ratios=(0.55, 1.2, 0.65, 0.65, 0.8, 0.8, 0.8, 0.85, 0.8, 0.85),
        font_size=6.9,
    )
    most_best = max(operator_rows, key=lambda row: (row["best"], row["current"], row["uses"]))
    writer.paragraph(
        f"14个注册算子在正式统计中均有记录。按累计全局最优改进次数，最高的观测算子"
        f"为{most_best['type']}算子{most_best['name']}（{most_best['best']}次）；"
        "破坏与修复算子成对使用，同一次奖励和改进标记会同时记到二者名下，因此"
        "该次数表示与全局改进共同出现的关联记录，不是单个算子的因果贡献。低频"
        "或低改进算子仍可能通过"
        "打开不同邻域帮助搜索跳出局部结构。"
    )
    writer.figure(
        "fig5_4_operator_effectiveness.png",
        "图5-4 14个核心算子的调用结果构成（先按运行归一化，再等权汇总）",
    )

    writer.heading("5.5 本章小结", 2)
    writer.paragraph(
        f"正式结果给出三类证据：题设两任务例由精确算法复核为11 km；"
        f"{len(exact_sizes)}个小规模实例中有{match_count}个ALNS运行达到精确三层"
        "得分；较宽松多机规模实验均产生通过校验的完整解。算子统计进一步确认"
        "最终14个注册算子实际进入搜索。正式200任务的四场景策略差异留待第6章分析。"
    )


def write_chapter_six(writer: ManuscriptWriter, bundle: Bundle) -> None:
    manifest = bundle.scenario_manifest
    rows = list(bundle.scenario_rows)
    groups = _scenario_groups(rows)
    summaries = _scenario_summary(rows)
    paired = _paired_comparisons(rows)
    sensitivity_groups = _sensitivity_groups(bundle.sensitivity_rows)

    writer.heading("6 正式应用案例与策略分析", 1, page_break=True)
    writer.paragraph(
        "本章使用给定200任务、8架无人机的正式数据，按照最终差异化计算预算协议"
        "比较四种协同场景。纯Direct使用240 s，启用Relay或Station的场景均增加"
        "一次60 s，组合场景不会累计为360 s。因此，本章结果是按最终预算协议的"
        "配对比较，质量差异同时包含机制与额外计算时间的共同影响，不能解释为"
        "严格同墙钟下的纯机制因果效应。"
    )

    writer.heading("6.1 200任务四场景正式案例", 2)
    writer.heading("6.1.1 数据、网络与运行协议", 3)
    first_solution = rows[0]["_solution"]
    problem = _mapping(first_solution["problem"], "四场景解.problem")
    relay_counts = sorted({int(row["station_count"]) for row in rows})
    writer.paragraph(
        f"正式实例含{manifest['tasks']}项一对一取送任务和{manifest['drones']}架无人机，"
        f"每机最多负责{manifest['max_tasks_per_drone']}项原始任务。解文件记录的容量"
        f"为{problem['capacity']}件、速度为{_fmt(problem['speed_km_per_min'])} km/min，"
        f"调度中心坐标为{tuple(problem['depot_km'])}。正式运行使用"
        f"{len(manifest['seeds'])}个配对种子：{', '.join(map(str, manifest['seeds']))}。"
    )
    writer.paragraph(
        f"为保持几何口径一致，四场景均构造{', '.join(map(str, relay_counts))}个固定"
        "候选站；是否用于Relay交接或Station预部署由各场景开关决定。"
        "每个种子都覆盖相同四场景，执行顺序按种子轮换，以降低系统负载顺序造成的"
        "偏差。输入文件和求解器源码均以SHA-256记录，完整清单见附录B。"
    )

    writer.heading("6.1.2 四场景定义与预算协议", 3)
    scenario_specs = {item["id"]: item for item in manifest["scenarios"]}
    protocol_rows: list[list[Any]] = []
    for scenario in SCENARIO_IDS:
        spec = scenario_specs[scenario]
        uses_extension = bool(spec["relay"]) or spec["drone_homes"] == "stations"
        protocol_rows.append(
            [
                scenario,
                "开" if spec["relay"] else "关",
                "需求驱动Station" if spec["drone_homes"] == "stations" else "原点",
                f"{float(spec['warmup']) * 100:.0f}% DIRECT + {100 - float(spec['warmup']) * 100:.0f}% Relay"
                if spec["relay"] else "全程DIRECT",
                _fmt(manifest["base_seconds"]),
                _fmt(manifest["bonus_seconds"]) if uses_extension else "0",
                _fmt(manifest["scenario_budgets_seconds"][scenario]),
                len(manifest["seeds"]),
            ]
        )
    writer.table(
        "表6-1 四场景定义与预算协议",
        ("场景", "Relay", "无人机起点", "分阶段搜索", "基础/s", "加时/s", "名义预算/s", "种子数"),
        protocol_rows,
        ratios=(1.6, 0.55, 1.25, 1.45, 0.65, 0.6, 0.85, 0.6),
        font_size=7.1,
    )
    writer.paragraph(
        "表6-1中的加时按“是否使用Relay或Station”判断，只增加一次。构造时间计入"
        "名义预算，搜索时间还扣除安全余量；Relay场景在剩余预算内执行80% DIRECT"
        "预热与20% Relay搜索。"
    )

    writer.heading("6.1.3 解质量与稳定性", 3)
    summary_table_rows: list[list[Any]] = []
    for scenario in SCENARIO_IDS:
        selected = groups[scenario]
        summary = summaries[scenario]
        summary_table_rows.append(
            [
                SCENARIO_LABELS[scenario],
                _mean_std([float(row["late_count"]) for row in selected]),
                _mean_std([float(row["total_lateness_min"]) for row in selected]),
                _mean_std([float(row["distance_km"]) for row in selected]),
                f"{summary['mean_wall_seconds']:.1f}",
                f"{summary['mean_iterations_per_second']:.1f}",
                f"{summary['mean_relay_task_count']:.2f}",
                f"{summary['mean_cross_uav_handoff_count']:.2f}",
            ]
        )
    writer.table(
        "表6-2 四场景三种子汇总结果",
        ("场景", "逾期数", "总逾期/min", "航程/km", "墙钟/s", "迭代/s", "Relay任务", "跨机交接"),
        summary_table_rows,
        ratios=(1.45, 0.9, 1.05, 1.05, 0.75, 0.75, 0.75, 0.75),
        font_size=7.2,
    )
    best_mean_scenario = min(
        SCENARIO_IDS,
        key=lambda scenario: (
            summaries[scenario]["mean_late_count"],
            summaries[scenario]["mean_total_lateness_min"],
            summaries[scenario]["mean_distance_km"],
        ),
    )
    best_summary = summaries[best_mean_scenario]
    writer.paragraph(
        f"按三种子均值的三层顺序，观测到的最优场景为"
        f"{SCENARIO_LABELS[best_mean_scenario]}，其均值三层得分为"
        f"({best_summary['mean_late_count']:.2f}, "
        f"{best_summary['mean_total_lateness_min']:.2f}, "
        f"{best_summary['mean_distance_km']:.2f})。均值只用于描述总体水平，"
        "逐种子配对胜负仍按每一对三层得分单独判定。"
    )
    writer.figure("fig6_1_scenario_comparison.png", "图6-1 四场景三层目标比较（均值、标准差与逐次观测）")

    writer.heading("6.1.4 相对纯Direct的逐种子配对结果", 3)
    writer.table(
        "表6-3 相对纯Direct的逐种子配对比较",
        ("比较场景", "种子", "纯Direct得分", "场景得分", "Δ逾期数", "Δ总逾期/min", "Δ航程/km", "字典序结果"),
        [
            [
                SCENARIO_LABELS[item["scenario"]],
                item["seed"],
                item["baseline_score"],
                item["scenario_score"],
                item["late_delta"],
                f"{item['lateness_delta']:.2f}",
                f"{item['distance_delta']:.2f}",
                item["outcome"],
            ]
            for item in paired
        ],
        ratios=(1.45, 0.8, 1.4, 1.4, 0.65, 0.85, 0.85, 0.75),
        font_size=6.9,
    )
    outcome_sentences: list[str] = []
    for scenario in SCENARIO_IDS[1:]:
        selected = [item for item in paired if item["scenario"] == scenario]
        counts = {outcome: sum(item["outcome"] == outcome for item in selected) for outcome in ("胜", "平", "负")}
        outcome_sentences.append(
            f"{SCENARIO_LABELS[scenario]}为{counts['胜']}胜{counts['平']}平{counts['负']}负"
        )
    writer.paragraph(
        "相对纯Direct的三组配对结果分别为：" + "；".join(outcome_sentences) + "。"
        "差值定义为扩展场景减去纯Direct，负值表示对应单项数值下降；最终胜负仍"
        "优先由逾期数决定，其次才是总逾期和航程。"
    )

    writer.heading("6.1.5 Relay、预部署与计算效率解释", 3)
    mechanism_rows: list[list[Any]] = []
    for scenario in SCENARIO_IDS:
        selected = groups[scenario]
        relay_details = [
            _mapping(row["_solution"]["relay"], "solution.relay") for row in selected
        ]
        same_uav = statistics.fmean(float(item["same_uav_relay_count"]) for item in relay_details)
        relay_share = statistics.fmean(float(item["relay_share"]) for item in relay_details)
        waiting = statistics.fmean(float(item["total_relay_waiting_time"]) for item in relay_details)
        mechanism_rows.append(
            [
                SCENARIO_LABELS[scenario],
                f"{summaries[scenario]['mean_relay_task_count']:.2f}",
                f"{relay_share:.3f}",
                f"{summaries[scenario]['mean_cross_uav_handoff_count']:.2f}",
                f"{same_uav:.2f}",
                f"{waiting:.2f}",
                f"{summaries[scenario]['mean_deadhead_km']:.2f}",
                f"{summaries[scenario]['mean_home_assignment_rate']:.3f}",
            ]
        )
    writer.table(
        "表6-4 四场景机制使用与起点指标",
        ("场景", "Relay任务", "Relay占比", "跨机交接", "同机经站", "等待/min", "首段空飞/km", "最近home分配率"),
        mechanism_rows,
        ratios=(1.45, 0.75, 0.75, 0.75, 0.75, 0.75, 0.8, 0.85),
        font_size=7.2,
    )
    relay_total = sum(float(row["relay_task_count"]) for row in rows if row["relay"])
    cross_total = sum(float(row["cross_uav_handoff_count"]) for row in rows if row["relay"])
    if relay_total > 0:
        relay_statement = (
            f"两个Relay场景的六次运行合计使用{int(relay_total)}个Relay任务，"
            f"其中记录{int(cross_total)}次跨机交接；因此Relay机制在正式解中确实被调用。"
        )
    else:
        relay_statement = (
            "两个Relay场景的正式解均未使用Relay任务，因而不能把质量差异归因于接力机制。"
        )
    writer.paragraph(
        relay_statement
        + " deadhead_km仅统计每架无人机从home到首个访问节点的首段调位里程；"
        "home_assignment_rate表示可识别的DIRECT取件任务中，其所在路线home为"
        "全部已部署home中最近者的比例，Relay腿不进入该分母，且原点场景因home"
        "相同而天然为1。三个扩展场景获得额外60 s，故机制指标与实测墙钟"
        "必须结合表6-1、表6-2共同解读。"
    )
    writer.figure(
        "fig6_2_route_layouts.png",
        "图6-2 四场景固定种子2026081701的路线布局",
    )

    writer.heading("6.2 组合场景敏感性分析", 2)
    sens_manifest = bundle.sensitivity_manifest
    writer.paragraph(
        f"敏感性分析固定使用direct_relay_stations和种子{sens_manifest['seed']}，"
        f"每次名义预算{_fmt(sens_manifest['effective_seconds_per_run'])} s。"
        "每次只改变一个参数，其余参数回到组合场景基准值；三个参数各有5个"
        "水平。由于每个水平只有一次运行，本节只讨论观察趋势，不提供误差线或"
        "统计显著性。"
    )
    design_rows: list[list[Any]] = []
    for row in sorted(
        bundle.sensitivity_rows,
        key=lambda item: (SENSITIVITY_PARAMETERS.index(item["parameter"]), float(item["level"])),
    ):
        design_rows.append(
            [
                row["parameter"],
                _fmt(row["level"]),
                row["drone_count"],
                _fmt(row["deadline_multiplier"]),
                row["requested_station_count"],
                row["seed"],
                _fmt(row["effective_time_limit_seconds"]),
                "其余参数取基准值",
            ]
        )
    writer.table(
        "表6-5 敏感性实验设计",
        ("参数", "水平", "无人机", "截止期倍率", "中继站", "种子", "预算/s", "控制"),
        design_rows,
        ratios=(1.25, 0.75, 0.7, 0.95, 0.7, 1.05, 0.75, 1.35),
        font_size=7.0,
    )
    writer.paragraph(
        "三个单因素轴都独立包含基准组合（8架无人机、截止期倍率1.0、4个站点），"
        "不跨轴复用已求得的基准解。固定seed用于控制随机流，但墙钟终止会使实际"
        "迭代数受运行速度影响，因此三个基准点不作为独立重复合并，也不要求逐位"
        "相同；每条曲线只使用本参数轴内部的五次运行。"
    )

    sensitivity_sections = (
        ("drone_count", "6.2.1 无人机数量", "无人机数", "fig6_3_drone_count_sensitivity.png", "图6-3 无人机数量敏感性"),
        ("deadline_multiplier", "6.2.2 最晚送达时间紧度", "截止期倍率", "fig6_3_deadline_multiplier_sensitivity.png", "图6-4 截止期紧度敏感性"),
        ("station_count", "6.2.3 中继站数量", "中继站数", "fig6_3_station_count_sensitivity.png", "图6-5 中继站数量敏感性"),
    )
    for table_index, (parameter, heading, level_label, filename, caption) in enumerate(sensitivity_sections, start=6):
        selected = sensitivity_groups[parameter]
        writer.heading(heading, 3)
        writer.table(
            f"表6-{table_index} {level_label}单因素结果",
            (level_label, "逾期数", "总逾期/min", "航程/km", "Relay任务", "跨机交接", "等待/min", "首段空飞/km", "有效"),
            [
                [
                    _fmt(row["level"]),
                    row["late_count"],
                    f"{float(row['total_lateness_min']):.2f}",
                    f"{float(row['distance_km']):.2f}",
                    row["relay_task_count"],
                    row["cross_uav_handoff_count"],
                    f"{float(row['total_relay_waiting_min']):.2f}",
                    f"{float(row['deadhead_km']):.2f}",
                    "是" if row["_solution"]["valid"] else "否",
                ]
                for row in selected
            ],
            ratios=(0.85, 0.65, 0.9, 0.9, 0.75, 0.75, 0.75, 0.8, 0.55),
            font_size=7.1,
        )
        writer.paragraph(_sensitivity_finding(parameter, selected))
        writer.figure(filename, caption)

    writer.heading("6.3 管理启示", 2)
    best_drone = min(sensitivity_groups["drone_count"], key=_score)
    best_station = min(sensitivity_groups["station_count"], key=_score)
    deadline_low = sensitivity_groups["deadline_multiplier"][0]
    deadline_high = sensitivity_groups["deadline_multiplier"][-1]
    writer.paragraph(
        f"第一，本固定种子下，无人机数量为{_fmt(best_drone['level'])}时取得最低"
        f"观测三层得分{_fmt_score(best_drone)}。该结果仅提示应先检查增加运力后"
        "逾期数是否仍下降，再比较里程，并不构成最优机队规模的统计估计。"
    )
    writer.paragraph(
        f"第二，本次站数轴以{_fmt(best_station['level'])}站取得最低观测得分"
        f"{_fmt_score(best_station)}。站数同时改变接力候选网与预部署起点，故需"
        "结合Relay任务、跨机交接、等待和首段调位，不能单归因于接力覆盖。"
    )
    writer.paragraph(
        f"第三，截止期倍率由{_fmt(deadline_low['level'])}提高到"
        f"{_fmt(deadline_high['level'])}时，逾期数由{deadline_low['late_count']}"
        f"降至{deadline_high['late_count']}。解释Relay作用还须确认Relay任务和"
        "跨机交接均非零，不能只由总得分反推机制使用。"
    )


def write_chapter_seven(writer: ManuscriptWriter, bundle: Bundle) -> None:
    rows = list(bundle.scenario_rows)
    summaries = _scenario_summary(rows)
    paired = _paired_comparisons(rows)
    sensitivity_groups = _sensitivity_groups(bundle.sensitivity_rows)
    best_scenario = min(
        SCENARIO_IDS,
        key=lambda scenario: (
            summaries[scenario]["mean_late_count"],
            summaries[scenario]["mean_total_lateness_min"],
            summaries[scenario]["mean_distance_km"],
        ),
    )
    best_summary = summaries[best_scenario]
    writer.heading("7 结论与展望", 1, page_break=True)

    writer.heading("7.1 主要结论", 2)
    writer.paragraph(
        "在模型层面，本文统一描述开放式一对一取送、容量与单机任务上限、最终"
        "送达截止期、直送/固定中继接力以及异地起点，并以逾期任务数、总逾期"
        "时长和总航程构成严格三层目标。该口径避免用较短航程掩盖更差的服务水平。"
    )
    writer.paragraph(
        "在算法层面，regret-2与14算子ALNS共同优化任务归属和访问顺序，全局DAG"
        "评价统一处理异步中继交接，需求驱动预部署为每条路线固定实际起点。"
        "题设两任务例由精确动态规划复核为11 km，小规模对照和多机扩展的正式"
        "记录支持实现一致性、解可行性与可运行性。"
    )
    outcome_summary: list[str] = []
    for scenario in SCENARIO_IDS[1:]:
        selected = [item for item in paired if item["scenario"] == scenario]
        wins = sum(item["outcome"] == "胜" for item in selected)
        ties = sum(item["outcome"] == "平" for item in selected)
        losses = sum(item["outcome"] == "负" for item in selected)
        outcome_summary.append(f"{SCENARIO_LABELS[scenario]} {wins}胜{ties}平{losses}负")
    writer.paragraph(
        f"在实验层面，按三种子均值三层顺序，{SCENARIO_LABELS[best_scenario]}的"
        f"观测得分最低，均值为({best_summary['mean_late_count']:.2f}, "
        f"{best_summary['mean_total_lateness_min']:.2f}, "
        f"{best_summary['mean_distance_km']:.2f})；相对纯Direct的配对结果为"
        + "；".join(outcome_summary)
        + "。敏感性分析只支持单种子下的方向性解释，完整水平和结果见表6-6至表6-8。"
    )

    writer.heading("7.2 理论与实践贡献", 2)
    writer.paragraph(
        "本文的理论贡献是把最终送达服务优先级写成可直接比较的严格三层目标，"
        "使逾期任务数量不被里程或时间权重抵消。方法贡献是以统一服务计划层和"
        "DAG时序评价连接DIRECT、RELAY与跨机交接，并以统一约束口径和分层可行性"
        "控制贯穿构造与搜索。实践贡献是以需求驱动预部署代替固定一站一机，再"
        "通过正式数据"
        "同时检查运力、站点、时效压力、首段调位和真实交接使用情况。"
    )

    writer.heading("7.3 研究局限", 2)
    limitations = (
        "距离与飞行时间为确定性输入，未纳入天气、风场和随机服务时间；",
        "尚未建模电池衰减、充换电、空域冲突与飞行高度；",
        "固定中继站允许等待与缓存，但未设置最大缓存时长、库存容量、服务容量或并发队列；",
        "第6.1节采用差异化预算，机制收益与额外计算时间不能完全分离；",
        "第6.1节只有一个200任务实例和3个种子，不能代表更广泛需求分布；",
        "第6.2节每个参数水平只有一个种子，单因素设计不能识别参数交互；",
        "首段空飞与最近home分配率只是局部代理指标，不能替代完整运营成本；",
        "精确动态规划仅支持单机、DIRECT、公共原点的小规模验证，不能证明大规模或扩展场景全局最优。",
    )
    for item in limitations:
        writer.bullet(item)

    writer.heading("7.4 后续研究", 2)
    future = (
        "补充相同总墙钟下的纯机制比较，并与当前最终预算协议并列报告；",
        "为敏感性实验增加多种子、置信区间和预先规定的统计比较；",
        "引入随机飞行时间、电池、充换电和空域冲突约束；",
        "联合优化中继站选址与无人机预部署，而非在搜索前固定；",
        "为大规模实例构造更强下界或数学规划基准。",
    )
    for item in future:
        writer.bullet(item)


def write_appendix_a(writer: ManuscriptWriter, bundle: Bundle) -> None:
    writer.heading("附录A 算法细节与伪代码", 1, page_break=True)

    writer.heading("A.1 数据结构、事件编码与记号映射", 2)
    writer.paragraph(
        "程序以原始任务为破坏和修复的最小单位。DIRECT计划包含同一架无人机上的"
        "取件事件P_i和最终送达事件D_i；RELAY(h)计划把任务拆成入站运输腿与出站"
        "运输腿，并在固定站点h增加卸货与取货事件。路线索引与无人机home一一绑定，"
        "路线首段始终从该home计算。"
    )
    writer.paragraph(
        "Score保存(late_count, total_lateness_min, distance_km)，比较运算严格按元组"
        "字典序执行。浮点指标只在数值容差范围内判断相等；任何缺失任务、重复服务、"
        "容量越界、单机任务数越界、取送倒置或交接DAG有环都会使valid为False。"
    )

    writer.heading("A.2 单机精确Pareto标签动态规划", 2)
    writer.paragraph(
        "精确算法仅用于单机、DIRECT且公共原点的开放式路线。其状态由"
        "(picked_mask, delivered_mask, last_visit)构成，标签同时记录"
        "到达状态的服务结果、累计距离与访问序列。转移只能取尚未取件且容量允许的"
        "任务，或送达已经取件但尚未送达的任务。对于同一状态，若一条标签在所有"
        "相关维度均不劣且至少一维严格更优，则支配另一条标签；只有非支配前缀"
        "进入下一步。全部任务送达后，按三层得分选择严格最优标签。"
    )
    writer.table(
        "算法A-1 单机Pareto标签动态规划",
        ("步骤", "操作"),
        (
            (1, "建立空picked_mask、空delivered_mask和调度中心起始标签"),
            (2, "枚举容量允许的取件动作与已取未送任务的送达动作"),
            (3, "更新到达时刻、逾期指标、累计距离与访问序列"),
            (4, "按同状态Pareto支配规则删除被支配标签"),
            (5, "重复直至全部任务送达，返回最小三层得分及完整路线"),
        ),
        ratios=(0.65, 5.35),
        font_size=8.4,
    )

    writer.heading("A.3 regret-2初始解", 2)
    writer.paragraph(
        "原点场景从空路线直接对全部待服务任务生成取送成对插入候选；Station"
        "场景先为每架无人机插入一个取件点最接近其home的相邻取送任务，再把其余"
        "任务交给regret-2。候选按三层增量排序，regret-2取最佳与次佳候选之间的"
        "损失，优先插入遗憾值最大的任务。若候选不足两个，则使用确定性边界值；"
        "home-aware场景在三层得分相同的情况下偏向降低起点首段空飞。"
    )
    writer.table(
        "算法A-2 regret-2初始解",
        ("步骤", "操作"),
        (
            (1, "读取任务、无人机home、容量、单机任务上限和候选剪枝设置"),
            (2, "若启用home_seed，每架无人机先获得取件点最接近其home的一个任务"),
            (3, "为每个剩余任务枚举可行取件/送达位置，计算最佳与次佳三层增量"),
            (4, "选择遗憾值最大的任务，执行其最佳可行插入并更新剩余集合"),
            (5, "任务集合为空后，调用独立完整评价器验证初始方案并返回构造时间"),
        ),
        ratios=(0.65, 5.35),
        font_size=8.4,
    )

    writer.heading("A.4 ALNS主循环", 2)
    writer.paragraph(
        "ALNS接收完整初始解、14个注册算子、初始权重、温度参数、随机种子、"
        "墙钟截止时间和最大迭代上界。修复过程通过局部可行插入维护结构，Relay"
        "候选通过全局DAG评价获得时序得分；刷新最优解时保存时间戳和三层得分"
        "轨迹，搜索结束后再对best执行独立完整校验。"
    )
    writer.table(
        "算法A-3 ALNS主循环",
        ("步骤", "操作"),
        (
            (1, "初始化current=best=regret-2解、算子权重、温度和统计计数"),
            (2, "按权重轮盘赌选择一个destroy和一个repair算子"),
            (3, "移除完整任务并修复为覆盖全部任务的候选方案"),
            (4, "以局部可行插入维护容量/配对/任务上限；Relay候选执行DAG时序评价"),
            (5, "按三层目标比较；结合模拟退火准则接受或拒绝候选"),
            (6, "记录接受、当前改进、全局改进和奖励，区段末平滑更新权重"),
            (7, "达到墙钟截止或迭代上界后完整校验best；合法时返回统计和轨迹"),
        ),
        ratios=(0.65, 5.35),
        font_size=8.2,
    )

    writer.heading("A.5 14个核心算子的完整定义", 2)
    operator_rows: list[list[Any]] = []
    for category, names in (("destroy", DESTROY_OPERATORS), ("repair", REPAIR_OPERATORS)):
        for name in names:
            selection, changed, guard = OPERATOR_DESCRIPTIONS[name]
            operator_rows.append(
                [
                    name,
                    category,
                    selection,
                    changed,
                    guard,
                    "候选评估与可行插入枚举" if category == "repair" else "任务排序与移除规模",
                ]
            )
    writer.table(
        "表A-1 最终14个注册算子定义",
        ("name", "category", "selection_rule", "state_changed", "feasibility_checks", "complexity_driver"),
        operator_rows,
        ratios=(1.15, 0.75, 1.75, 1.4, 1.55, 1.35),
        font_size=7.1,
    )
    writer.paragraph(
        "边界处理遵循显式规则：任务集合为空时不执行无意义破坏；无逾期任务时"
        "late_critical回退到可定义的完整任务选择；候选不足时使用确定性排序"
        "边界。若某个待修复任务不存在精确可行插入，或初始/最终完整评价失败，"
        "程序抛出异常并中止该次运行，不把不完整方案写成有效结果。"
    )

    writer.heading("A.6 Relay候选生成与全局DAG评价", 2)
    writer.paragraph(
        "Relay网络先为每项任务筛选有限的固定站候选，再把被选任务拆成入站和"
        "出站运输腿。路线内部访问顺序产生路线边，中继卸货到取货产生交接边；"
        "二者组成的DAG用于检测循环等待并传播最早服务时刻。出站无人机早到时"
        "等待，不要求与入站无人机同步到达。中继等待、同机经站与跨机交接均"
        "写入解文件，供第6章机制解释。"
    )
    writer.table(
        "算法A-4 Relay全局评价与预部署",
        ("阶段", "输入", "关键处理", "输出"),
        (
            ("候选网络", "任务、站点", "筛选绕行可接受的站点与运输腿", "任务—站点候选"),
            ("服务计划", "DIRECT incumbent", "中继播种与周期性跨机精化", "DIRECT/RELAY计划"),
            ("DAG评价", "路线边、交接边", "环检测、拓扑时刻传播和等待计算", "valid与最终送达时刻"),
            ("预部署", "候选home、需求得分", "Hamilton配额与局部调整", "路线索引对应home"),
        ),
        ratios=(0.85, 1.25, 2.35, 1.55),
        font_size=7.8,
    )

    writer.heading("A.7 正式协议参数", 2)
    scenario_manifest = bundle.scenario_manifest
    sensitivity_manifest = bundle.sensitivity_manifest
    station_counts = sorted({int(row["station_count"]) for row in bundle.scenario_rows})
    parameter_rows = [
        ("目标层数", "operator_count之外的Score协议", 3, "层", "全实验", "解文件objective_order"),
        ("最终算子数", "operator_count", scenario_manifest["operator_count"], "个", "ALNS", "四场景manifest"),
        ("正式任务数", "tasks", scenario_manifest["tasks"], "项", "第6章", "四场景manifest"),
        ("正式无人机数", "drones", scenario_manifest["drones"], "架", "第6.1节", "四场景manifest"),
        ("单机任务上限", "max_tasks_per_drone", scenario_manifest["max_tasks_per_drone"], "项", "第6.1节", "四场景manifest"),
        ("基础预算", "base_seconds", _fmt(scenario_manifest["base_seconds"]), "s", "四场景", "四场景manifest"),
        ("Relay/Station加时", "bonus_seconds", _fmt(scenario_manifest["bonus_seconds"]), "s", "扩展场景，只加一次", "四场景manifest"),
        ("Relay预热比例", "scenario.warmup", "0.80", "比例", "Relay场景", "四场景manifest"),
        ("正式站点数", "station_count", ", ".join(map(str, station_counts)), "站", "第6.1节", "四场景runs"),
        ("敏感性单次预算", "effective_seconds_per_run", _fmt(sensitivity_manifest["effective_seconds_per_run"]), "s", "第6.2节", "敏感性manifest"),
        ("多机宽松倍率", "relaxed_deadline_multiplier", _fmt(bundle.benchmark_manifest["relaxed_deadline_multiplier"]), "倍", "第5.3节", "第5章manifest"),
        ("多机规模预算", "scale_seconds", _fmt(bundle.benchmark_manifest["scale_seconds"]), "s", "第5.3节", "第5章manifest"),
    ]
    writer.table(
        "表A-2 正式结果文件可复核的算法与实验参数",
        ("参数", "键", "值", "单位", "范围", "来源"),
        parameter_rows,
        ratios=(1.35, 1.45, 0.9, 0.65, 1.35, 1.25),
        font_size=7.5,
    )


def write_appendix_b(writer: ManuscriptWriter, bundle: Bundle) -> None:
    writer.heading("附录B 完整实验结果与复现材料", 1, page_break=True)

    writer.heading("B.1 数据字段、预处理与输入一致性", 2)
    scenario_manifest = bundle.scenario_manifest
    sensitivity_manifest = bundle.sensitivity_manifest
    benchmark_manifest = bundle.benchmark_manifest
    first_solution = bundle.scenario_rows[0]["_solution"]
    problem = _mapping(first_solution["problem"], "solution.problem")
    writer.paragraph(
        "正式CSV的输入一致性由SHA-256校验。坐标按公里解释，飞行时间由欧氏距离"
        "除以速度获得；截止期敏感性只对所有任务的最晚送达时刻乘统一倍率，不改变"
        "坐标、任务配对或速度。任务编号和完整服务状态随各解文件保存。"
    )
    writer.table(
        "表B-1 正式输入与模型基础字段",
        ("字段", "值", "来源"),
        (
            (
                "输入SHA-256",
                _soft_wrap_hash(scenario_manifest["input_sha256"]),
                "三组正式manifest一致",
            ),
            ("任务数", problem["task_count"], "四场景解.problem"),
            ("无人机数", problem["drone_count"], "四场景解.problem"),
            ("容量", problem["capacity"], "四场景解.problem"),
            ("单机任务上限", problem["max_tasks_per_drone"], "四场景解.problem"),
            ("速度/(km/min)", _fmt(problem["speed_km_per_min"]), "四场景解.problem"),
            ("调度中心/km", tuple(problem["depot_km"]), "四场景解.problem"),
            ("开放式路线", problem["open_routes"], "四场景解.problem"),
            ("目标顺序", " → ".join(problem["objective_order"]), "四场景解.problem"),
        ),
        ratios=(1.35, 3.5, 1.45),
        font_size=7.5,
    )

    writer.heading("B.2 运行环境与可复现清单", 2)
    reproducibility_rows: list[tuple[Any, Any]] = [
        ("四场景Python", scenario_manifest["python"]),
        ("四场景平台", scenario_manifest["platform"]),
        ("四场景/敏感性求解器源码SHA-256", scenario_manifest["solver_source_sha256"]),
        ("四场景生成时间UTC", scenario_manifest["created_at_utc"]),
        ("四场景种子", ", ".join(map(str, scenario_manifest["seeds"]))),
        ("第5章生成时间UTC", benchmark_manifest["created_at_utc"]),
        ("第5章种子", benchmark_manifest["seed"]),
        ("敏感性生成时间UTC", sensitivity_manifest["created_at_utc"]),
        ("敏感性固定种子", sensitivity_manifest["seed"]),
        ("预算协议", "pure_direct=240 s；其余三场景=300 s"),
    ]
    reproducibility_rows.append(
        ("第5章求解器源码SHA-256", benchmark_manifest["solver_source_sha256"])
    )
    for label, key in (
        ("敏感性Git提交", "git_commit"),
        ("敏感性Git工作区是否有改动", "git_dirty"),
        ("敏感性实验脚本SHA-256", "experiment_script_sha256"),
        ("敏感性场景脚本SHA-256", "scenario_script_sha256"),
        ("敏感性依赖清单SHA-256", "requirements_dev_sha256"),
    ):
        if key in sensitivity_manifest:
            reproducibility_rows.append((label, sensitivity_manifest[key]))
    sensitivity_solver_config = sensitivity_manifest.get("solver_config")
    if isinstance(sensitivity_solver_config, dict):
        reproducibility_rows.extend(
            (
                ("敏感性max_iterations", sensitivity_solver_config.get("max_iterations")),
                ("敏感性candidate_limit", sensitivity_solver_config.get("candidate_limit")),
                ("敏感性安全余量/s", sensitivity_solver_config.get("safety_margin_seconds")),
                ("敏感性选站方法", sensitivity_solver_config.get("relay_location_method")),
                ("敏感性选站种子", sensitivity_solver_config.get("relay_location_seed")),
            )
        )
    writer.table(
        "表B-2 正式运行清单",
        ("项目", "记录"),
        reproducibility_rows,
        ratios=(1.6, 4.4),
        font_size=7.8,
    )

    writer.heading("B.3 第5章逐运行完整结果", 2)
    benchmark_rows = sorted(
        bundle.benchmark_rows,
        key=lambda row: (int(row["task_count"]), str(row["method"])),
    )
    writer.table(
        "表B-3a 第5章正式逐运行身份与解质量",
        ("实例", "方法", "任务", "无人机", "截止倍率", "种子", "逾期", "总逾期/min", "航程/km"),
        [
            [
                row["experiment"], row["method"], row["task_count"], row["drone_count"],
                _fmt(row["deadline_multiplier"]), row["seed"] if row["seed"] is not None else "—",
                row["late_count"], f"{float(row['total_lateness_min']):.3f}",
                f"{float(row['distance_km']):.3f}",
            ]
            for row in benchmark_rows
        ],
        ratios=(1.55, 1.05, 0.55, 0.65, 0.75, 0.9, 0.55, 0.85, 0.85),
        font_size=7.3,
    )
    writer.table(
        "表B-3b 第5章正式逐运行效率与审计字段",
        ("实例", "方法", "任务", "时间/s", "迭代", "有效", "达精确解"),
        [
            [
                row["experiment"], row["method"], row["task_count"],
                f"{float(row['runtime_seconds']):.4f}", row["iterations"],
                "是" if row["valid"] else "否",
                "是" if row.get("matches_oracle") is True else (
                    "否" if row.get("matches_oracle") is False else "—"
                ),
            ]
            for row in benchmark_rows
        ],
        ratios=(1.8, 1.2, 0.65, 0.9, 0.8, 0.65, 0.85),
        font_size=7.5,
    )
    writer.paragraph(
        "题设两任务的最优路线与11 km结果见图5-1。其余精确对照的正式JSON保存"
        "三层得分、运行时间和一致性字段；未在结果文件中序列化的路线不以人工"
        "推测补入表B-3a或表B-3b。"
    )

    writer.heading("B.4 四场景逐种子完整结果", 2)
    scenario_rows = sorted(
        bundle.scenario_rows,
        key=lambda row: (int(row["seed"]), SCENARIO_IDS.index(row["scenario"])),
    )
    writer.table(
        "表B-4a 四场景逐运行身份与解质量",
        ("场景", "种子", "预算/s", "逾期", "总逾期/min", "航程/km", "有效"),
        [
            [
                row["scenario"], row["seed"], _fmt(row["effective_time_limit_seconds"]),
                row["late_count"], f"{float(row['total_lateness_min']):.3f}",
                f"{float(row['distance_km']):.3f}",
                "是" if row["_solution"]["valid"] else "否",
            ]
            for row in scenario_rows
        ],
        ratios=(1.55, 0.95, 0.75, 0.65, 1.0, 1.0, 0.6),
        font_size=7.5,
    )
    writer.table(
        "表B-4b 四场景逐运行计算效率",
        ("场景", "种子", "构造/s", "搜索/s", "墙钟/s", "迭代", "迭代/s"),
        [
            [
                row["scenario"], row["seed"], f"{float(row['construction_seconds']):.3f}",
                f"{float(row['solver_runtime_seconds']):.3f}", f"{float(row['wall_seconds']):.3f}",
                row["iterations"], f"{float(row['_solution']['metadata']['iterations_per_second']):.2f}",
            ]
            for row in scenario_rows
        ],
        ratios=(1.55, 0.95, 0.85, 0.85, 0.85, 0.9, 0.9),
        font_size=7.5,
    )
    writer.table(
        "表B-4c 四场景逐运行Relay机制指标",
        ("场景", "种子", "Relay任务", "Relay占比", "跨机", "同机", "等待/min"),
        [
            [
                row["scenario"], row["seed"], row["relay_task_count"],
                f"{float(row['_solution']['relay']['relay_share']):.4f}",
                row["cross_uav_handoff_count"], row["_solution"]["relay"]["same_uav_relay_count"],
                f"{float(row['total_relay_waiting_min']):.3f}",
            ]
            for row in scenario_rows
        ],
        ratios=(1.5, 0.9, 0.8, 0.85, 0.65, 0.65, 0.9),
        font_size=7.5,
    )
    writer.table(
        "表B-4d 四场景逐运行Station部署指标",
        ("场景", "种子", "候选站点", "首段空飞/km", "最近home分配率"),
        [
            [
                row["scenario"], row["seed"], row["station_count"],
                f"{float(row['deadhead_km']):.3f}",
                f"{float(row['home_assignment_rate']):.4f}",
            ]
            for row in scenario_rows
        ],
        ratios=(1.5, 0.9, 0.9, 1.1, 1.2),
        font_size=7.5,
    )
    writer.table(
        "表B-4e 四场景逐运行解文件审计",
        ("场景", "种子", "解文件", "SHA-256"),
        [
            [
                row["scenario"], row["seed"], row["solution_file"],
                _soft_wrap_hash(row["solution_sha256"]),
            ]
            for row in scenario_rows
        ],
        ratios=(1.35, 0.75, 1.8, 3.4),
        font_size=7.5,
    )

    writer.heading("B.5 核心算子分场景汇总", 2)
    writer.paragraph(
        "表B-5a至表B-5d分别将同一场景的3个配对种子统计量相加，末权重取"
        "3次运行均值；逐运行原始值保存在表B-4e列出的解文件中并由SHA-256"
        "绑定。当前改进与全局改进是破坏—修复算子对共同出现的记录，不作单个"
        "算子的因果贡献解释。"
    )
    for suffix, scenario in zip("abcd", SCENARIO_IDS, strict=True):
        selected = [row for row in scenario_rows if row["scenario"] == scenario]
        aggregate_rows: list[list[Any]] = []
        for operator in OPERATOR_KEYS:
            stats_rows = [row["operator_statistics"][operator] for row in selected]
            uses = sum(int(stats["uses"]) for stats in stats_rows)
            accepted = sum(int(stats["accepted"]) for stats in stats_rows)
            current = sum(int(stats["current_improvements"]) for stats in stats_rows)
            best = sum(int(stats["best_improvements"]) for stats in stats_rows)
            reward = sum(float(stats["total_reward"]) for stats in stats_rows)
            mean_weight = statistics.fmean(
                float(row["_solution"]["metadata"]["operator_weights"][operator])
                for row in selected
            )
            operator_type, operator_name = operator.split(":", 1)
            aggregate_rows.append(
                [
                    "破坏" if operator_type == "destroy" else "修复",
                    operator_name,
                    uses,
                    accepted,
                    f"{100.0 * accepted / uses:.2f}",
                    current,
                    best,
                    f"{reward:.2f}",
                    f"{mean_weight:.4f}",
                ]
            )
        writer.table(
            f"表B-5{suffix} {scenario}三种子算子汇总",
            (
                "类型",
                "算子",
                "调用",
                "接受",
                "接受率/%",
                "当前改进",
                "全局改进",
                "奖励",
                "平均末权重",
            ),
            aggregate_rows,
            ratios=(0.65, 1.35, 0.65, 0.65, 0.8, 0.8, 0.8, 0.75, 0.9),
            font_size=7.3,
        )

    writer.heading("B.6 敏感性逐水平完整结果", 2)
    sensitivity_rows = sorted(
        bundle.sensitivity_rows,
        key=lambda row: (SENSITIVITY_PARAMETERS.index(row["parameter"]), float(row["level"])),
    )
    writer.table(
        "表B-6a 敏感性逐水平实验设计",
        ("参数", "水平", "种子", "无人机", "截止倍率", "请求站点"),
        [
            [
                row["parameter"], _fmt(row["level"]), row["seed"], row["drone_count"],
                _fmt(row["deadline_multiplier"]), row["requested_station_count"],
            ]
            for row in sensitivity_rows
        ],
        ratios=(1.3, 0.7, 1.0, 0.75, 0.9, 0.85),
        font_size=7.5,
    )
    writer.table(
        "表B-6b 敏感性逐水平解质量",
        ("参数", "水平", "逾期", "总逾期/min", "航程/km", "有效"),
        [
            [
                row["parameter"], _fmt(row["level"]), row["late_count"],
                f"{float(row['total_lateness_min']):.3f}",
                f"{float(row['distance_km']):.3f}",
                "是" if row["_solution"]["valid"] else "否",
            ]
            for row in sensitivity_rows
        ],
        ratios=(1.3, 0.7, 0.7, 1.0, 1.0, 0.65),
        font_size=7.5,
    )
    writer.table(
        "表B-6c 敏感性逐水平机制与效率",
        ("参数", "水平", "实际站点", "Relay任务", "跨机", "等待/min", "首段空飞/km", "墙钟/s"),
        [
            [
                row["parameter"], _fmt(row["level"]), row["station_count"],
                row["relay_task_count"], row["cross_uav_handoff_count"],
                f"{float(row['total_relay_waiting_min']):.3f}",
                f"{float(row['deadhead_km']):.3f}", f"{float(row['wall_seconds']):.3f}",
            ]
            for row in sensitivity_rows
        ],
        ratios=(1.25, 0.65, 0.8, 0.8, 0.65, 0.85, 0.95, 0.8),
        font_size=7.5,
    )
    writer.table(
        "表B-6d 敏感性逐水平解文件审计",
        ("参数", "水平", "解文件SHA-256"),
        [
            [
                row["parameter"], _fmt(row["level"]),
                _soft_wrap_hash(row["solution_sha256"]),
            ]
            for row in sensitivity_rows
        ],
        ratios=(1.2, 0.7, 4.1),
        font_size=7.5,
    )
    writer.paragraph(
        "表B-6a至表B-6d中每个水平对应一个正式解文件，均记录运行时的valid与"
        "violations审计结果。"
        "三层得分、机制计数、墙钟时间和solution_sha256均保留在原始JSON中；"
        "图6-3至图6-5只从这些正式行生成。"
    )

    writer.heading("B.7 可行性与结果审计", 2)
    total_solutions = (
        len(bundle.benchmark_solutions)
        + len(bundle.scenario_rows)
        + len(bundle.sensitivity_rows)
    )
    writer.paragraph(
        f"终稿生成前共读取并审计{total_solutions}个已序列化正式解文件。"
        "每个文件必须记录valid=true、violations为空，且三层得分与汇总行一致；"
        "四场景和敏感性解还必须通过清单中的SHA-256校验。任何缺失、非法、哈希"
        "不一致或协议字段错误都会在DOCX写入前触发明确异常。"
    )
    writer.table(
        "表B-7 终稿生成器的来源与一致性审计门",
        ("审计项", "通过条件"),
        (
            ("运行时完整评价", "读取正式求解器保存的valid=true与空violations记录"),
            ("路线与起点结构", "problem.drone_homes、drone_count及路线数量一致"),
            ("任务上限与Relay时序", "依赖正式解中已保存的完整评价结果，不在生成器内重算"),
            ("三层得分", "runs行与solution.score逐字段一致"),
            ("输出来源", "manifest formal=true，协议名、场景、固定种子、水平和预算匹配"),
            ("图像来源", "figure_manifest绑定三组正式JSON与9组PNG/SVG的SHA-256"),
            ("题设示意", "图5-1复现题设11 km路线；其余8组图读取正式结果JSON"),
        ),
        ratios=(1.55, 4.45),
        font_size=8.0,
    )


def build_manuscript(source: Path, output: Path, bundle: Bundle, *, force: bool) -> None:
    if source.suffix.lower() != ".docx" or not source.is_file():
        raise ArtefactError(f"源DOCX不存在或扩展名错误：{source}")
    if output.suffix.lower() != ".docx":
        raise ArtefactError(f"输出路径必须以.docx结尾：{output}")
    source_resolved = source.resolve()
    output_resolved = output.resolve(strict=False)
    if source_resolved == output_resolved:
        raise ArtefactError("拒绝原地覆盖用户提供的源DOCX；请指定不同的输出路径")
    if output.exists() and not force:
        raise ArtefactError(f"输出已存在：{output}；如需替换请显式传入--force")

    document = Document(str(source))
    align_preserved_front_matter(document)
    start, references = replaceable_range(document)
    delete_old_chapters(document, start, references)
    normalise_preserved_math_and_symbol_table(document)
    configure_styles(document)
    writer = ManuscriptWriter(document, references, bundle.figure_dir)
    write_chapter_four(writer, bundle)
    write_chapter_five(writer, bundle)
    write_chapter_six(writer, bundle)
    write_chapter_seven(writer, bundle)
    write_appendix_a(writer, bundle)
    write_appendix_b(writer, bundle)
    references.paragraph_format.page_break_before = True

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.stem}.{os.getpid()}.tmp.docx")
    try:
        document.save(str(temporary))
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    results_root = args.results_root.expanduser()
    benchmark_dir = (args.benchmarks_dir or results_root / "final_benchmarks").expanduser()
    scenario_dir = (args.scenarios_dir or results_root / "final_scenario_comparison").expanduser()
    sensitivity_dir = (args.sensitivity_dir or results_root / "final_sensitivity").expanduser()
    bundle = load_bundle(
        benchmark_dir.resolve(),
        scenario_dir.resolve(),
        sensitivity_dir.resolve(),
        args.figures_dir.expanduser().resolve(),
    )
    build_manuscript(
        args.source_docx.expanduser(),
        args.output_docx.expanduser(),
        bundle,
        force=args.force,
    )
    print(f"终稿DOCX已写入：{args.output_docx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
