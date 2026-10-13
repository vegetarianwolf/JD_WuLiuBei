"""Render an offline, source-backed Chinese report for staged optimization runs.

Usage: python -m algorithm.experiments.render_optimization_report --output-dir DIR
The report reads existing artefacts only; it never runs a solver or updates them.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import statistics
from typing import Any, Sequence


SCORE_FIELDS = ("late_count", "total_lateness_min", "distance_km")
METRIC_FIELDS = (*SCORE_FIELDS, "on_time_rate", "end_to_end_seconds", "on_time_count")
GROUP_FIELDS = (
    "stage", "case_id", "scenario", "task_count", "drone_count", "budget_seconds",
)
DEFAULT_ORIGINAL_SCENARIOS = {"pure_direct", "direct", "original", "origin", "original_direct"}


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _normalise_run(raw: dict[str, Any]) -> dict[str, Any]:
    row = dict(raw)
    missing = [key for key in (*GROUP_FIELDS, "variant", "seed") if key not in row]
    if missing:
        raise ValueError(f"运行记录缺少分组字段：{', '.join(missing)}")
    if not _number(row["budget_seconds"]) or row["budget_seconds"] <= 0:
        raise ValueError("budget_seconds 必须为有限正数")
    for field in ("task_count", "drone_count"):
        if not isinstance(row[field], int) or isinstance(row[field], bool) or row[field] < 1:
            raise ValueError(f"{field} 必须为正整数")
    issues: list[str] = []
    if row.get("valid") is not True:
        issues.append("解未通过可行性校验")
    if row.get("budget_compliant") is not True:
        issues.append("预算未达标或未核验")
    if not _number(row.get("end_to_end_seconds")) or row.get("end_to_end_seconds", -1) < 0:
        issues.append("缺少有效端到端计时")
    elif row["end_to_end_seconds"] > row["budget_seconds"]:
        issues.append("实际端到端耗时超过预算")
    if any(not _number(row.get(key)) or row.get(key, -1) < 0 for key in SCORE_FIELDS):
        issues.append("缺少有效目标得分")
    elif int(row["late_count"]) != row["late_count"] or row["late_count"] > row["task_count"]:
        issues.append("逾期任务数超出有效范围")
    expected_on_time = (
        row["task_count"] - row["late_count"]
        if _number(row.get("late_count")) else None
    )
    if expected_on_time is not None:
        if row.get("on_time_count") != expected_on_time:
            issues.append("准时任务数与逾期任务数不一致")
        rate = row.get("on_time_rate")
        if not _number(rate) or not math.isclose(rate, expected_on_time / row["task_count"], abs_tol=1e-10):
            issues.append("准时率与任务数不一致")
    row["report_issues"] = issues
    row["report_usable"] = not issues
    return row


def _stats(values: Sequence[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "mean": statistics.mean(values) if values else None,
        "sd": statistics.stdev(values) if len(values) > 1 else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
    }


def _pair_rows(rows: Sequence[dict[str, Any]], baseline: str) -> list[dict[str, Any]]:
    by_variant: dict[str, dict[Any, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        by_variant[row["variant"]][row["seed"]] = row
    base = by_variant.get(baseline, {})
    pairs: list[dict[str, Any]] = []
    for variant in sorted(set(by_variant) - {baseline}):
        current = by_variant[variant]
        for seed in sorted(set(base) | set(current), key=str):
            left, right = base.get(seed), current.get(seed)
            pair: dict[str, Any] = {"variant": variant, "seed": seed}
            if left is None or right is None:
                pair.update(status="missing", reason="缺少同种子基线或候选记录")
            elif not left["report_usable"] or not right["report_usable"]:
                pair.update(status="invalid", reason="基线或候选未通过可行性／预算核验")
            elif not left.get("initial_routes_sha256") or not right.get("initial_routes_sha256"):
                pair.update(status="invalid", reason="初始路线指纹缺失，无法核验配对公平性")
            elif left["initial_routes_sha256"] != right["initial_routes_sha256"]:
                pair.update(status="invalid", reason="初始路线指纹不同，不能纳入公平配对")
            else:
                baseline_score = tuple(left[field] for field in SCORE_FIELDS)
                candidate_score = tuple(right[field] for field in SCORE_FIELDS)
                status = "win" if candidate_score < baseline_score else "loss" if candidate_score > baseline_score else "tie"
                decisive = next((field for field in SCORE_FIELDS if left[field] != right[field]), None)
                pair.update(
                    status=status,
                    decisive_metric=decisive,
                    baseline_score=list(baseline_score),
                    candidate_score=list(candidate_score),
                    deltas={field: right[field] - left[field] for field in SCORE_FIELDS},
                    same_initial_routes=(left.get("initial_routes_sha256") == right.get("initial_routes_sha256"))
                    if left.get("initial_routes_sha256") and right.get("initial_routes_sha256") else None,
                )
            pairs.append(pair)
    return pairs


def _equivalence_summary(raw: Any) -> dict[str, Any] | None:
    """Keep fixed-iteration evidence separate from wall-clock objective runs."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("equivalence 必须为对象")
    observations = []
    for record in raw.get("observations", []):
        baseline_seconds = record.get("baseline_seconds")
        fast_seconds = record.get("fast_seconds")
        reduction = (
            1 - fast_seconds / baseline_seconds
            if _number(baseline_seconds) and baseline_seconds > 0 and _number(fast_seconds)
            else None
        )
        observations.append({
            **{key: record.get(key) for key in ("case_id", "seed", "equivalent", "baseline_seconds", "fast_seconds")},
            "runtime_reduction": reduction,
            "baseline_iterations": record.get("baseline", {}).get("iterations"),
            "fast_iterations": record.get("fast", {}).get("iterations"),
        })
    return {
        **{key: raw.get(key) for key in ("all_equivalent", "passed", "elapsed_seconds", "median_runtime_reduction")},
        "observations": observations,
    }


def _validate_audit_fingerprints(validation: Any, source_hashes: dict[str, str]) -> None:
    """Reject stale audit evidence instead of presenting it as current."""
    if validation is None:
        return
    if not isinstance(validation, dict):
        raise ValueError("validation 必须为对象")
    for field, filename in (
        ("manifest_sha256", "manifest.json"),
        ("runs_sha256", "runs.json"),
        ("equivalence_sha256", "equivalence.json"),
    ):
        if field not in validation or validation[field] != source_hashes.get(filename):
            raise ValueError(f"审计记录陈旧或不完整：{field} 与 {filename} 不一致；请重新运行独立审计")


def build_report_data(output_dir: Path) -> dict[str, Any]:
    """Build independently grouped, paired summaries without mixing budgets."""
    output_dir = Path(output_dir)
    # Parse and hash the same bytes, so changing files cannot pair a new hash
    # with an older in-memory payload while a batch is still running.
    source_bytes = {
        name: (output_dir / name).read_bytes()
        for name in ("manifest.json", "runs.json", "decisions.json", "validation.json", "equivalence.json", "execution_notes.json")
        if name in {"manifest.json", "runs.json"} or (output_dir / name).is_file()
    }
    source_hashes = {name: hashlib.sha256(content).hexdigest() for name, content in source_bytes.items()}
    artefacts = {name: json.loads(content) for name, content in source_bytes.items()}
    validation = artefacts.get("validation.json")
    if "validation.json" in artefacts and not isinstance(validation, dict):
        raise ValueError("validation 必须为对象；现有审计文件不能作为未审计预览忽略")
    _validate_audit_fingerprints(validation, source_hashes)
    manifest = artefacts["manifest.json"]
    raw_rows = artefacts["runs.json"]
    if not isinstance(manifest, dict) or not isinstance(raw_rows, list):
        raise ValueError("manifest 必须为对象，runs 必须为数组")
    rows = [_normalise_run(row) for row in raw_rows]
    baseline = str(manifest.get("baseline_variant", "baseline"))
    original_scenarios = set(manifest.get("original_scenarios", DEFAULT_ORIGINAL_SCENARIOS))
    stage_order = list(dict.fromkeys(row["stage"] for row in rows))
    requested_order = manifest.get("stage_order")
    if isinstance(requested_order, list):
        stage_order = list(dict.fromkeys([*requested_order, *stage_order]))
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        key = tuple(row[field] for field in GROUP_FIELDS)
        unique = (*key, row["variant"], row["seed"])
        if unique in seen:
            raise ValueError(f"重复运行记录，无法公平配对：{unique}")
        seen.add(unique)
        grouped[key].append(row)
    groups = []
    families: dict[tuple[Any, ...], dict[str, Any]] = {}
    for index, (key, members) in enumerate(grouped.items()):
        group = dict(zip(GROUP_FIELDS, key))
        group["id"] = f"group-{index}"
        group["rows"] = members
        variants = sorted({row["variant"] for row in members}, key=lambda value: (value != baseline, value))
        group["variants"] = variants
        by_variant = {
            variant: {row["seed"]: row for row in members if row["variant"] == variant}
            for variant in variants
        }
        common_seeds: set[Any] = set()
        if baseline in variants and len(variants) > 1:
            common_seeds = set.intersection(*(
                {seed for seed, row in by_variant[variant].items()
                 if row["report_usable"] and row.get("initial_routes_sha256")}
                for variant in variants
            ))
            common_seeds = {
                seed for seed in common_seeds
                if len({by_variant[variant][seed]["initial_routes_sha256"] for variant in variants}) == 1
            }
        group["comparison_seeds"] = sorted(common_seeds, key=str)
        planned_seeds = manifest.get("seeds", [])
        if isinstance(planned_seeds, dict):
            planned_seeds = planned_seeds.get(group["stage"], [])
        group["planned_seed_count"] = len(set(planned_seeds)) if isinstance(planned_seeds, list) else None
        for row in members:
            row["report_in_comparison"] = row["seed"] in common_seeds
        group["unpaired_valid_count"] = sum(
            row["report_usable"] and not row["report_in_comparison"] for row in members
        )
        group["summary"] = {}
        for variant in variants:
            usable = [row for row in members if row["variant"] == variant and row["report_in_comparison"]]
            group["summary"][variant] = {
                field: _stats([row[field] for row in usable]) for field in METRIC_FIELDS
            }
        group["pairs"] = _pair_rows(members, baseline)
        group["pair_counts"] = {
            variant: {
                status: sum(pair["variant"] == variant and pair["status"] == status for pair in group["pairs"])
                for status in ("win", "tie", "loss", "missing", "invalid")
            }
            for variant in variants if variant != baseline
        }
        groups.append(group)
        family_key = tuple(group[field] for field in ("case_id", "scenario", "task_count", "drone_count"))
        if family_key not in families:
            families[family_key] = {
                "id": f"family-{len(families)}",
                **{field: group[field] for field in ("case_id", "scenario", "task_count", "drone_count")},
                "original": group["scenario"] in original_scenarios,
                "group_ids": [],
            }
        families[family_key]["group_ids"].append(group["id"])
    by_id = {group["id"]: group for group in groups}
    for family in families.values():
        family["default_group_id"] = max(
            family["group_ids"],
            key=lambda group_id: (stage_order.index(by_id[group_id]["stage"]), groups.index(by_id[group_id])),
        )
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "manifest": manifest,
        "baseline_variant": baseline,
        "stages": stage_order,
        "groups": groups,
        "families": list(families.values()),
        "decisions": artefacts.get("decisions.json"),
        "validation": validation,
        "equivalence": _equivalence_summary(artefacts.get("equivalence.json")),
        "execution_notes": artefacts.get("execution_notes.json"),
        "source_file_sha256": source_hashes,
        "run_count": len(rows),
        "usable_run_count": sum(row["report_usable"] for row in rows),
        "excluded_run_count": sum(not row["report_usable"] for row in rows),
    }


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>物流无人机调度 · 优化实验报告</title>
<meta name="description" content="官方实例、同预算配对实验与三级词典序目标的离线可视化报告。">
<style>
:root{color-scheme:light dark;--bg:#f5f5f0;--paper:#fff;--ink:#202927;--muted:#586461;--line:#dce2dc;--wash:#eef2ed;--accent:#1c5f4e;--good:#24664d;--bad:#a8463b;--series-0:#52759f;--series-1:#258168;--series-2:#b17b37;--series-3:#8b67a6;--series-4:#9b5961;--series-5:#4b838f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;line-height:1.65;font-size:15px}main{max-width:1232px;padding:48px 40px 36px;margin:auto}header{padding-bottom:28px;border-bottom:1px solid var(--line)}.eyebrow{color:var(--accent);letter-spacing:.14em;font-size:12px;font-weight:600}h1{font-size:clamp(28px,4vw,44px);letter-spacing:-.035em;line-height:1.3;margin:12px 0 14px;font-weight:600}h2{font-size:24px;letter-spacing:-.025em;font-weight:600;margin:0}h3{font-size:18px;font-weight:600;margin:0}h4{font-size:14px;margin:0 0 10px;font-weight:500}.intro{max-width:780px;margin:0;color:var(--muted)}.protocol{display:flex;flex-wrap:wrap;gap:8px 22px;margin-top:20px;color:var(--muted);font-size:13px}.protocol strong{color:var(--ink);font-weight:500}.section-head{display:flex;align-items:baseline;flex-wrap:wrap;gap:10px 20px;margin:34px 0 16px}.section-head p{margin:0;color:var(--muted);font-size:13px}.family{background:var(--paper);padding:24px 26px;margin-bottom:20px;border-radius:14px}.family-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}.subtle{font-size:12px;color:var(--muted)}.family-head p{margin:3px 0 0}.select-label{font-size:12px;color:var(--muted);display:flex;flex-direction:column;gap:4px}select{background:var(--paper);color:var(--ink);border:1px solid var(--line);border-radius:6px;min-height:38px;padding:6px 10px;font:inherit;font-size:13px;max-width:100%}.legend{display:flex;flex-wrap:wrap;gap:8px 20px;margin:16px 0 6px;font-size:12px}.legend span{display:inline-flex;align-items:center;gap:7px;overflow-wrap:anywhere}.swatch{width:13px;height:3px;display:inline-block;flex:none}.notice{padding:10px 13px;background:var(--wash);font-size:12px;color:var(--muted);border-radius:5px;margin:12px 0}.notice.warning{color:var(--bad)}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px 28px;margin-top:22px}.chart{min-width:0}.chart svg{display:block;overflow:visible;width:100%;color:var(--ink)}.chart svg text{fill:var(--muted);font:12px Inter,"PingFang SC",system-ui,sans-serif}.chart svg .value{fill:var(--ink)}.chart .empty{font-size:13px;color:var(--muted);padding:20px 0}.section-rule{border-top:1px solid var(--line);margin:24px 0 18px}.comparison{display:flex;flex-wrap:wrap;gap:10px 24px;margin:12px 0}.comparison .score{font-size:20px;font-variant-numeric:tabular-nums;letter-spacing:.02em}.comparison .label{font-size:12px;color:var(--muted)}.comparison .name{font-size:13px;margin-bottom:3px}.good{color:var(--good)}.bad{color:var(--bad)}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}th,td{padding:10px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}th{font-weight:500;color:var(--muted)}th:first-child,td:first-child{text-align:left}td.status{text-align:left;white-space:normal;min-width:115px}.empty-state{padding:18px 0;color:var(--muted);font-size:13px}.curve-head{display:flex;gap:14px;justify-content:space-between;align-items:center;flex-wrap:wrap}.curves{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px 28px;margin-top:16px}details{margin-top:17px}summary{cursor:pointer;font-size:13px;color:var(--muted);padding:8px 0}details pre{font-size:12px;line-height:1.6;background:var(--wash);padding:14px;border-radius:5px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:440px;overflow:auto}a{color:var(--accent);text-underline-offset:3px}.evidence{padding:24px 0}.evidence-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px}.evidence h3{font-size:16px}.evidence p{font-size:13px;color:var(--muted)}footer{border-top:1px solid var(--line);padding-top:18px;color:var(--muted);font-size:12px}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}.print-button{font:inherit;font-size:12px;background:none;color:var(--ink);border:1px solid var(--line);border-radius:5px;padding:7px 12px;cursor:pointer}.footer-row{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}.inline-code{font-family:ui-monospace,monospace;overflow-wrap:anywhere}.audit-links{display:flex;gap:16px;flex-wrap:wrap;font-size:13px}
@media(prefers-color-scheme:dark){:root{--bg:#151c19;--paper:#1d2622;--ink:#e8ede8;--muted:#afbeb4;--line:#37483e;--wash:#28372e;--accent:#99cbb3;--good:#94cbb1;--bad:#f2a292;--series-0:#96b8e2;--series-1:#7ccab0;--series-2:#dfb47a;--series-3:#c6a4e2;--series-4:#df98a2;--series-5:#93c4d0}}
@media(max-width:900px){main{padding:32px 24px}.metrics,.curves{grid-template-columns:repeat(2,minmax(0,1fr))}.family{padding:22px}.curves .chart:last-child{grid-column:1/-1}}
@media(max-width:580px){main{padding:24px 14px}.family{padding:19px 16px;border-radius:10px}.metrics,.curves,.evidence-grid{grid-template-columns:1fr}.curves .chart:last-child{grid-column:auto}.family-head{display:block}.family-head .select-label{margin-top:14px}.family-head select{width:100%}.protocol{gap:6px 16px}.metrics{gap:18px}.section-head{margin-top:28px}h2{font-size:22px}}
@media print{:root{color-scheme:light;--bg:#fff;--paper:#fff;--ink:#111;--muted:#444;--line:#ccc;--wash:#f3f3f3;--accent:#184b3e;--good:#24664d;--bad:#a8463b;--series-0:#52759f;--series-1:#258168;--series-2:#b17b37;--series-3:#8b67a6;--series-4:#9b5961;--series-5:#4b838f}main{max-width:none;padding:0}header{padding:0 0 16px}h1{font-size:28px}.family{padding:16px 0;border-radius:0}.metrics,.curves{grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}.family-head select,.curve-head select,.print-button{display:none}.chart{break-inside:avoid}.curves .chart:last-child{grid-column:auto}.table-wrap{overflow:visible}details pre{max-height:none;overflow:visible}a{color:inherit}footer{margin-top:20px}.notice{break-inside:avoid}}
</style>
</head>
<body><main>
<header><div class="eyebrow">LOGISTICS / PAIRED OPTIMIZATION</div><h1>物流无人机调度<br>优化实验报告</h1>
<p class="intro">围绕题目考核展示按时完成与总航程。项目采用“逾期任务数 → 总逾期分钟 → 总航程”词典序，其中总逾期分钟是项目引入的次级目标。同一实例、预算和随机种子内配对，逐阶段检查优化是否值得扩大测试。</p>
<div id="run-overview" class="protocol" aria-live="polite"></div><div id="outcome-note" class="notice"></div></header>
<noscript><p>请启用 JavaScript 查看交互图表。原始结果保存在同目录 runs.json，审计信息保存在 validation.json。</p></noscript>
<section aria-labelledby="equivalence-title"><div class="section-head"><h2 id="equivalence-title">固定迭代核验</h2><p>结果等价与整次运行提速分别判断。</p></div><div id="equivalence-results"></div></section>
<section aria-labelledby="original-title"><div class="section-head"><h2 id="original-title">原题考核</h2><p>公共原点 · 容量 2 · 先取后送 · 最终送达后不返航</p></div><div id="original-results"></div></section>
<section aria-labelledby="extension-title"><div class="section-head"><h2 id="extension-title">扩展兼容性</h2><p>中继与预部署单独展示，不替代原题成绩。</p></div><div id="extension-results"></div></section>
<section class="evidence" aria-labelledby="evidence-title"><h2 id="evidence-title">实验依据</h2><div id="execution-summary"></div><details><summary>提前停止与中止运行的原始记录</summary><pre id="execution-notes"></pre></details><div class="evidence-grid"><div><h3>晋级与停止记录</h3><p id="decision-note"></p><div id="decision-summary"></div><details><summary>展开决策原始记录</summary><pre id="decisions"></pre></details></div><div><h3>审计与可复现性</h3><p id="audit-note"></p><details><summary>展开独立校验记录</summary><pre id="validation"></pre></details></div></div><details><summary>实验协议、配置与源码指纹</summary><pre id="manifest"></pre></details><details><summary>本页数据文件 SHA-256</summary><pre id="hashes"></pre></details><div class="audit-links"><a href="runs.json">逐次运行 JSON</a><a href="manifest.json">实验协议 JSON</a></div></section>
<footer><div class="footer-row"><span id="generated-at"></span><button class="print-button" type="button" id="print-report">打印 / 保存 PDF</button></div><p>均值与样本标准差仅描述本次运行；误差线为 ±1 个样本 SD，n=1 时不绘制，不是置信区间。探索结果不等同于全局最优证明，也不替换既有论文冻结结果。</p></footer>
</main><script id="optimization-data" type="application/json">__REPORT_DATA__</script>
<script>
"use strict";
(() => {
const data=JSON.parse(document.getElementById("optimization-data").textContent);
const NS="http://www.w3.org/2000/svg";
const E=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=(v,d=2)=>typeof v==="number"&&Number.isFinite(v)?v.toLocaleString("zh-CN",{maximumFractionDigits:d,minimumFractionDigits:d}):"—";
const byId=new Map(data.groups.map(g=>[g.id,g]));
const allVariants=[...new Set(data.groups.flatMap(g=>g.variants))].sort((a,b)=>a===data.baseline_variant?-1:b===data.baseline_variant?1:a.localeCompare(b));
const color=v=>`var(--series-${Math.max(0,allVariants.indexOf(v))%6})`;
const variantLabel=v=>data.manifest.source_snapshots?.[v]?.label??(v===data.baseline_variant?"基线":({fast:"等价提速候选",allocation:"分配优化候选"}[v]??v));
const stageNames={screen:"初筛",screening:"初筛",pilot:"小范围初筛",medium:"入围验证",confirm:"原题长测",confirmation:"入围验证",validation:"入围验证",final:"原题长测",long:"原题长测",long_run:"原题长测",full:"原题长测",compatibility:"扩展兼容",extensions:"扩展兼容",extension:"扩展兼容",smoke:"冒烟校验"};
const stageLabel=s=>stageNames[s]??s;
const scenarios={pure_direct:"原题 · 直接取送",direct:"原题 · 直接取送",direct_relay:"Direct + Relay",direct_stations:"Direct + Station",direct_relay_stations:"Direct + Relay + Station"};
const metricNames={late_count:"逾期任务数",total_lateness_min:"总逾期时间",distance_km:"总航程"};
const metrics=[{key:"late_count",title:"逾期任务数",unit:"项 · 越少越好",digits:2},{key:"on_time_rate",title:"按时完成率",unit:"% · 越高越好",factor:100,digits:2},{key:"total_lateness_min",title:"总逾期时间",unit:"min · 越少越好",digits:2},{key:"distance_km",title:"总航程",unit:"km · 越少越好",digits:2},{key:"end_to_end_seconds",title:"端到端运行时间",unit:"s · 不超过本组预算",digits:3},{key:"on_time_count",title:"按时完成数",unit:"项 · 越多越好",digits:2}];
const eligible=r=>r.report_usable;
const svgEl=(tag,attrs={},text)=>{const e=document.createElementNS(NS,tag);Object.entries(attrs).forEach(([k,v])=>e.setAttribute(k,v));if(text!==undefined)e.textContent=text;return e};
const add=(svg,tag,attrs,text)=>{const node=svgEl(tag,attrs,text);svg.appendChild(node);return node};
const safeLink=value=>{const s=String(value??"");return s&&!s.startsWith("/")&&!s.includes("\\")&&!/^[a-z][a-z0-9+.-]*:/i.test(s)&&!s.split("/").includes("..")?s.split("/").map(encodeURIComponent).join("/"):null};
function barChart(container,group,metric){
container.replaceChildren();
const series=group.variants.map(v=>({variant:v,...group.summary[v][metric.key]})).filter(s=>s.n>0);
if(!series.length){container.innerHTML='<p class="empty">暂无各版本共有的有效、同初解配对样本。</p>';return}
const w=Math.max(220,container.getBoundingClientRect().width),h=series.length*55+36,left=Math.min(100,w*.3),right=66,plot=w-left-right,factor=metric.factor??1;
const max=Math.max(...series.map(s=>(s.mean+(s.sd??0))*factor),metric.key==="end_to_end_seconds"?group.budget_seconds:0,1e-9)*1.08;
const x=v=>left+Math.max(0,v)/max*plot;
const svg=svgEl("svg",{viewBox:`0 0 ${w} ${h}`,width:w,height:h,role:"img","aria-label":`${metric.title}，各版本均值与样本标准差`});
add(svg,"title",{},`${metric.title}：同一配置内有效运行的均值，误差线为样本标准差`);
for(let i=0;i<3;i++){const value=max*i/2;add(svg,"line",{x1:x(value),x2:x(value),y1:3,y2:h-24,stroke:"var(--line)","stroke-width":1});add(svg,"text",{x:x(value),y:h-5,"text-anchor":i===2?"end":i===0?"start":"middle"},fmt(value,value>=10?0:1))}
series.forEach((s,i)=>{const y=13+i*55,label=variantLabel(s.variant),mean=s.mean*factor,sd=(s.sd??0)*factor;add(svg,"text",{x:0,y:y+10},label.length>12?label.slice(0,11)+"…":label);add(svg,"text",{x:0,y:y+27},`n=${s.n}`);const rect=add(svg,"rect",{x:left,y:y+1,width:Math.max(0,x(mean)-left),height:13,fill:color(s.variant),rx:2});add(rect,"title",{},`${label}：${fmt(mean,metric.digits)}${s.sd===null?"；n=1，SD不可估计":` ± ${fmt(sd,metric.digits)}`}，n=${s.n}`);if(s.sd!==null&&sd>0){const lo=x(Math.max(0,mean-sd)),hi=x(mean+sd);add(svg,"line",{x1:lo,x2:hi,y1:y+7.5,y2:y+7.5,stroke:color(s.variant),"stroke-width":1.4});[lo,hi].forEach(t=>add(svg,"line",{x1:t,x2:t,y1:y+2,y2:y+13,stroke:color(s.variant),"stroke-width":1.4}))}add(svg,"text",{x:w,y:y+11,"text-anchor":"end",class:"value"},fmt(mean,metric.digits));add(svg,"text",{x:w,y:y+28,"text-anchor":"end"},s.sd===null?"SD —":`± ${fmt(sd,metric.digits)}`)});
container.appendChild(svg);
}
function curveChart(container,group,seed,metric,index){
container.replaceChildren();const rows=group.rows.filter(r=>String(r.seed)===String(seed)&&eligible(r)&&r.report_in_comparison);
const series=rows.map(r=>({variant:r.variant,points:(r.best_trajectory??[]).filter(p=>Array.isArray(p)&&p.length>=5&&[p[1],p[index]].every(v=>typeof v==="number"&&Number.isFinite(v))&&p[1]>=0).map(p=>({t:p[1],v:p[index]})).sort((a,b)=>a.t-b.t)})).filter(s=>s.points.length);
if(!series.length){container.innerHTML='<p class="empty">该种子暂无有效收敛轨迹。</p>';return}
const w=Math.max(220,container.getBoundingClientRect().width),h=224,left=59,right=15,top=22,bottom=39,pw=w-left-right,ph=h-top-bottom;
const points=series.flatMap(s=>s.points),observedMax=Math.max(...points.map(p=>p.v)),observedMin=Math.min(...points.map(p=>p.v)),padding=Math.max((observedMax-observedMin)*.12,Math.abs(observedMax)*.03,.01),min=Math.max(0,observedMin-padding),max=observedMax+padding,maxT=Math.max(...points.map(p=>p.t),...rows.map(r=>r.search_seconds??0),.001);
const x=t=>left+t/maxT*pw,y=v=>top+(max-v)/(max-min)*ph;
const svg=svgEl("svg",{viewBox:`0 0 ${w} ${h}`,width:w,height:h,role:"img","aria-label":`${metric.title}收敛轨迹，种子${seed}`});add(svg,"title",{},`${metric.title}，种子${seed}，每次完整三级得分改进的记录`);
for(let i=0;i<3;i++){const value=min+(max-min)*i/2;add(svg,"line",{x1:left,x2:w-right,y1:y(value),y2:y(value),stroke:"var(--line)","stroke-width":1});add(svg,"text",{x:left-8,y:y(value)+4,"text-anchor":"end"},fmt(value,index===2?0:1));const t=maxT*i/2;add(svg,"text",{x:x(t),y:h-22,"text-anchor":i===0?"start":i===2?"end":"middle"},fmt(t,maxT<10?1:0))}
add(svg,"text",{x:left,y:12},metric.unit.split(" · ")[0]);add(svg,"text",{x:w-right,y:h-3,"text-anchor":"end"},"轨迹计时 / s");
series.forEach(s=>{let d=`M${x(s.points[0].t)},${y(s.points[0].v)}`;s.points.slice(1).forEach(p=>d+=`H${x(p.t)}V${y(p.v)}`);const final=s.points[s.points.length-1];d+=`H${x(maxT)}`;const path=add(svg,"path",{d,fill:"none",stroke:color(s.variant),"stroke-width":2,"stroke-dasharray":s.variant===data.baseline_variant?"5 3":"none"});add(path,"title",{},`${variantLabel(s.variant)}：${s.points.length}个轨迹点，末次值${fmt(final.v)}`);add(svg,"circle",{cx:x(final.t),cy:y(final.v),r:3,fill:color(s.variant)})});
container.appendChild(svg);
}
function pairTable(group){const names={win:"胜",tie:"平",loss:"负",invalid:"核验未通过",missing:"配对缺失"};return `<div class="table-wrap"><table><caption class="sr-only">同种子配对结果，差值为候选减基线</caption><thead><tr><th>候选 / seed</th><th>判定</th><th>决胜目标</th><th>Δ 逾期数</th><th>Δ 逾期 min</th><th>Δ 航程 km</th></tr></thead><tbody>${group.pairs.map(p=>`<tr><td>${E(variantLabel(p.variant))}<br><span class="subtle">${E(p.seed)}</span></td><td class="${p.status==="win"?"good":p.status==="loss"?"bad":""}">${names[p.status]}</td><td>${E(metricNames[p.decisive_metric]??(p.status==="tie"?"三项目标相同":p.reason??"—"))}</td>${["late_count","total_lateness_min","distance_km"].map(k=>`<td>${p.deltas?fmt(p.deltas[k],k==="late_count"?0:3):"—"}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`}
function runTable(group){return `<div class="table-wrap"><table><caption class="sr-only">本组每次运行原始数据与状态</caption><thead><tr><th>版本 / seed</th><th>状态</th><th>逾期数</th><th>总逾期 min</th><th>航程 km</th><th>按时率</th><th>端到端 s</th><th>构造 s</th><th>搜索 s</th><th>核验 s</th><th>序列化 s</th><th>迭代</th><th>完整解</th></tr></thead><tbody>${group.rows.map(r=>{const href=safeLink(r.solution_file);return `<tr><td>${E(variantLabel(r.variant))}<br><span class="subtle">${E(r.seed)}</span></td><td class="status ${eligible(r)?(r.report_in_comparison?"good":""):"bad"}">${eligible(r)?(r.report_in_comparison?"可行 · 预算达标 · 已配对":"未配对 · 可行 · 预算达标"):E(r.report_issues.join("；"))}</td><td>${fmt(r.late_count,0)}</td><td>${fmt(r.total_lateness_min,3)}</td><td>${fmt(r.distance_km,3)}</td><td>${typeof r.on_time_rate==="number"?fmt(r.on_time_rate*100)+"%":"—"}</td>${["end_to_end_seconds","construction_seconds","search_seconds","validation_seconds","serialization_seconds"].map(k=>`<td>${fmt(r[k],3)}</td>`).join("")}<td>${fmt(r.iterations,0)}</td><td>${href?`<a href="${E(href)}">JSON</a>`:"—"}</td></tr>`}).join("")}</tbody></table></div>`}
const redraws=[];
function renderFamily(family){
const outer=document.createElement("article");outer.className="family";outer.id=family.id;
const options=family.group_ids.map(id=>byId.get(id)).sort((a,b)=>data.stages.indexOf(a.stage)-data.stages.indexOf(b.stage));
outer.innerHTML=`<div class="family-head"><div><h3>${family.task_count} 项任务 / ${family.drone_count} 架无人机</h3><p class="subtle">${E(scenarios[family.scenario]??family.scenario)} · ${E(family.case_id)}</p></div><label class="select-label">查看阶段与预算<select aria-label="${E(family.case_id)}实验阶段">${options.map(g=>`<option value="${g.id}" ${g.id===family.default_group_id?"selected":""}>${E(stageLabel(g.stage))} · ${fmt(g.budget_seconds,0)} 秒</option>`).join("")}</select></label></div><div class="family-content"></div>`;
const stageSelect=outer.querySelector("select"),content=outer.querySelector(".family-content");let draw=()=>{};
function renderGroup(){const group=byId.get(stageSelect.value),bad=group.rows.filter(r=>!eligible(r)),seeds=group.comparison_seeds;
content.innerHTML=`<div class="legend">${group.variants.map(v=>`<span><i class="swatch" style="background:${color(v)}"></i>${E(variantLabel(v))}${v===data.baseline_variant?"（虚线）":""}</span>`).join("")}</div><div class="notice ${bad.length?"warning":""}">${group.rows.length} 条运行记录，${group.rows.length-bad.length} 条通过可行性与预算核验${bad.length?`，${bad.length} 条排除出汇总与胜负比较`:""}。本视图只汇总 ${E(stageLabel(group.stage))}、${fmt(group.budget_seconds,0)} 秒预算的数据；各版本均值与柱图共用 ${group.comparison_seeds.length}${group.planned_seed_count?` / 计划 ${group.planned_seed_count}`:""} 个有效、初解一致的配对种子${group.unpaired_valid_count?`。另有 ${group.unpaired_valid_count} 条完整有效记录未配对，保留在原始表中、不参与均值比较`:""}。配对表按每个候选与基线分别核验。</div><div class="metrics">${metrics.map(m=>`<section class="chart"><h4>${m.title} <span class="subtle">${m.unit}</span></h4><div data-metric="${m.key}"></div></section>`).join("")}</div><div class="section-rule"></div><h4>同种子配对 · 严格三级词典序</h4><div class="comparison">${Object.entries(group.pair_counts).map(([v,c])=>`<div><div class="name">${E(variantLabel(v))} 相对基线</div><div class="score"><span class="good">${c.win}</span> / ${c.tie} / <span class="bad">${c.loss}</span></div><div class="label">胜 / 平 / 负${c.missing+c.invalid?` · 未纳入 ${c.missing+c.invalid} 对`:""}</div></div>`).join("")||'<p class="empty-state">尚无候选版本配对记录。</p>'}</div>${group.pairs.length?pairTable(group):""}<div class="section-rule"></div><div class="curve-head"><h4>最优解随搜索推进的变化</h4><label class="select-label">随机种子<select class="seed-select" aria-label="收敛轨迹随机种子">${seeds.map(s=>`<option>${E(s)}</option>`).join("")}</select></label></div><p class="subtle">仅展示共同配对种子的完整三级得分改进轨迹；高优先级目标改善时，较低优先级目标可能上升。轨迹沿用求解器计时，运行限额按端到端计时核验。</p><div class="curves">${[metrics[0],metrics[2],metrics[3]].map(m=>`<section class="chart"><h4>${m.title}</h4><div data-curve="${m.key}"></div></section>`).join("")}</div><details><summary>逐次运行、计时分项与完整解</summary>${runTable(group)}</details>`;
const seedSelect=content.querySelector(".seed-select");draw=()=>{metrics.forEach(m=>barChart(content.querySelector(`[data-metric="${m.key}"]`),group,m));[metrics[0],metrics[2],metrics[3]].forEach((m,i)=>curveChart(content.querySelector(`[data-curve="${m.key}"]`),group,seedSelect.value,m,i+2))};seedSelect.addEventListener("change",draw);requestAnimationFrame(draw)}
stageSelect.addEventListener("change",renderGroup);outer.refresh=renderGroup;redraws.push(()=>draw());return outer;
}
for(const [original,id] of [[true,"original-results"],[false,"extension-results"]]){const target=document.getElementById(id),families=data.families.filter(f=>f.original===original);if(!families.length){target.innerHTML=`<p class="empty-state">${original?"尚无原题实验记录。":"尚未运行扩展兼容实验；没有结果可用于比较。"}</p>`;continue}families.forEach(f=>{const node=renderFamily(f);target.appendChild(node);node.refresh()})}
document.getElementById("run-overview").innerHTML=`<span>记录 <strong>${data.run_count}</strong> 次</span><span>有效 <strong>${data.usable_run_count}</strong> 次</span><span>排除 <strong>${data.excluded_run_count}</strong> 次</span><span>阶段 <strong>${data.stages.length}</strong> 个</span><span>同目录数据 · 离线可读</span>`;
const decisionList=Array.isArray(data.decisions)?data.decisions:[];
const confirmation=decisionList.find(d=>d.stage==="confirm"||d.stage==="final"),middle=decisionList.find(d=>d.stage==="medium"),screen=decisionList.find(d=>d.stage==="screen");
let outcome="探索记录已载入；尚无最终确认结论。";
if(confirmation?.skipped)outcome=`未进行原题长测：${confirmation.reason??"前一阶段未晋级"}。`;
else if(confirmation?.passed===true)outcome="唯一入围候选通过原题长测的预定门槛；结论范围见本次配对记录。";
else if(confirmation?.passed===false)outcome="入围候选未通过原题长测的预定门槛，不能声称已获得稳定改进。";
else if(middle?.passed===false)outcome="入围候选未通过中测；尚无原题长测结论。";
else if(screen&&screen.selected_variant===null)outcome="初筛没有候选晋级；尚无原题长测结论。";
else if(screen?.selected_variant)outcome=`初筛入围：${variantLabel(screen.selected_variant)}。尚无原题长测最终结论。`;
if(data.execution_notes?.early_stop)outcome=`确认长测已提前停止，未完成运行没有成绩；当前记录不构成全计划种子验证。${confirmation?.reason??data.execution_notes.policy??""}`;
document.getElementById("outcome-note").textContent=outcome;
const eq=data.equivalence,eqTarget=document.getElementById("equivalence-results");
if(!eq)eqTarget.innerHTML='<p class="empty-state">尚无固定迭代等价核验记录。</p>';
else{const reductions=Object.entries(eq.median_runtime_reduction??{});eqTarget.innerHTML=`<article class="family"><div class="comparison"><div><div class="name">路线、得分与搜索决策</div><div class="score ${eq.all_equivalent===true?"good":"bad"}">${eq.all_equivalent===true?"全部等价":eq.all_equivalent===false?"存在差异":"未判定"}</div><div class="label">${eq.observations.length} 组固定迭代配对</div></div>${reductions.map(([name,value])=>`<div><div class="name">${E({single25:"25 任务 / 1 架",fleet200:"200 任务 / 8 架"}[name]??name)}</div><div class="score">${fmt(value*100)}%</div><div class="label">中位耗时的相对降低</div></div>`).join("")}</div><div class="notice ${eq.passed===false?"warning":""}">${eq.passed===true?"等价提速候选通过预定性能门槛。":eq.passed===false?"等价提速候选未通过预定性能门槛；结果等价不代表提速充分。":"尚无性能门槛判定。"} ${E(data.manifest.protocol?.performance_gate??"性能门槛见实验协议。")} 相对降低 = 1 − 候选耗时中位数 / 基线耗时中位数。正值表示变快，负值表示变慢；此处不作为同墙钟目标改善证据。</div><details><summary>逐组耗时与等价核验</summary><div class="table-wrap"><table><thead><tr><th>实例 / seed</th><th>等价</th><th>固定迭代</th><th>基线 s</th><th>候选 s</th><th>耗时降低</th></tr></thead><tbody>${eq.observations.map(o=>`<tr><td>${E(o.case_id)}<br><span class="subtle">${E(o.seed)}</span></td><td>${o.equivalent===true?"是":o.equivalent===false?"否":"未记录"}</td><td>${fmt(o.baseline_iterations,0)} / ${fmt(o.fast_iterations,0)}</td><td>${fmt(o.baseline_seconds,4)}</td><td>${fmt(o.fast_seconds,4)}</td><td>${o.runtime_reduction===null?"—":fmt(o.runtime_reduction*100)+"%"}</td></tr>`).join("")}</tbody></table></div><p class="subtle">两列迭代数依次为基线 / 候选。<a href="equivalence.json">查看完整核验记录</a></p></details></article>`}
const notes=data.execution_notes;
if(notes?.early_stop){const aborted=Array.isArray(notes.aborted_runs)?notes.aborted_runs:[];document.getElementById("execution-summary").innerHTML=`<div class="notice warning">本次提前停止确认长测。已完成且有完整解的记录全部保留，未配对结果单独标记；中止或未启动的运行没有得分，不计入均值、胜负或已完成样本数。</div><p>${E(notes.policy??"")}</p>${aborted.length?`<div class="table-wrap"><table><thead><tr><th>中止实例 / seed</th><th>版本</th><th>状态</th><th>中止时进程已运行</th></tr></thead><tbody>${aborted.map(r=>`<tr><td>${E(r.case_id)}<br>${E(r.seed)}</td><td>${E(variantLabel(r.variant))}</td><td>已中止，无成绩</td><td>${E(r.process_elapsed_at_stop??"未记录")}</td></tr>`).join("")}</tbody></table></div>`:""}`}
document.getElementById("decision-summary").innerHTML=decisionList.map(d=>`<p><strong>${E(stageLabel(d.stage))}</strong> · ${d.skipped?"跳过，未运行":d.passed===true?"通过门槛":d.passed===false?"未通过门槛":d.selected_variant?"选定 "+E(variantLabel(d.selected_variant)):"未选定候选"}<br><span class="subtle">${E(d.reason??"")}</span></p>`).join("");
document.getElementById("decision-note").textContent=data.decisions===null?"尚无阶段决策记录；未运行的阶段不能视为通过。":"保存筛选、晋级、停止或跳过的原始决定；未执行阶段不填补成绩。";
document.getElementById("audit-note").textContent=data.validation===null?"尚无独立审计记录。图表中的有效状态仅依据运行记录及本页一致性检查。":"审计记录与当前协议、运行和等价核验文件的指纹一致；具体通过、失败和覆盖范围以展开后的原始记录为准。";
for(const [id,value] of [["decisions",data.decisions],["validation",data.validation],["manifest",data.manifest],["hashes",data.source_file_sha256],["execution-notes",data.execution_notes]])document.getElementById(id).textContent=value===null?"未生成":JSON.stringify(value,null,2);
document.getElementById("generated-at").textContent=`报告生成：${new Date(data.generated_at_utc).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour12:false})}（北京时间）`;
document.getElementById("print-report").addEventListener("click",()=>window.print());
let scheduled=false;const redraw=()=>{if(scheduled)return;scheduled=true;requestAnimationFrame(()=>{scheduled=false;redraws.forEach(f=>f())})};
if(typeof ResizeObserver!=="undefined"){const observer=new ResizeObserver(redraw);document.querySelectorAll(".family").forEach(el=>observer.observe(el))}else window.addEventListener("resize",redraw);
window.addEventListener("beforeprint",()=>redraws.forEach(f=>f()));
})();
</script></body></html>'''


def render_report(output_dir: Path) -> Path:
    """Write index.html alongside source JSON, embedding all data for offline use."""
    output_dir = Path(output_dir)
    data = build_report_data(output_dir)
    # JSON is not HTML-safe by itself: close-script sequences must stay data.
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    encoded = encoded.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    encoded = encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    target = output_dir / "index.html"
    target.write_text(HTML_TEMPLATE.replace("__REPORT_DATA__", encoded), encoding="utf-8")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    print(render_report(args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
