"""Offline diagnostic and follow-up report; reads evidence, never runs a solver."""

from __future__ import annotations

import argparse
import hashlib
from html import escape
import json
import math
from os.path import relpath
from pathlib import Path
from typing import Any, Sequence

from algorithm.experiments import render_optimization_report as base


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "algorithm/results/regret_followup_20261013"
PREVIOUS_OUTPUT = ROOT / "algorithm/results/optimization_20261013"
KNOWN_SEED = 2026101321
NEW_SEEDS = (2026101361, 2026101362, 2026101363)
SHORT_LABELS = {"baseline": "基线", "old_allocation": "旧跨机", "fallback": "位置回退", "portfolio": "自适应组合"}


def _json_bytes(path: Path) -> tuple[dict[str, Any], str]:
    content = path.read_bytes()
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError(f"诊断文档必须为对象：{path.name}")
    return value, hashlib.sha256(content).hexdigest()


def _check_source(root: Path, name: Any, expected: Any) -> Path:
    if not isinstance(name, str) or Path(name).is_absolute():
        raise ValueError("诊断来源必须为相对路径")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError(f"诊断来源缺失或越界：{name}")
    if not isinstance(expected, str) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise ValueError(f"诊断来源指纹已改变：{name}")
    return path


def _diagnostics(directory: Path) -> dict[str, Any]:
    result: dict[str, Any] = {"history": None, "probe": None, "followup_observations": None,
                              "lazy_equivalence": None, "files": {}}
    path = directory / "diagnostics/diagnostics.json"
    if path.is_file():
        history, fingerprint = _json_bytes(path)
        provenance = history.get("provenance", {})
        source_dir = provenance.get("input_directory")
        fingerprints = provenance.get("source_files_sha256")
        if not isinstance(source_dir, str) or not isinstance(fingerprints, dict) or not fingerprints:
            raise ValueError("历史诊断缺少原始证据来源指纹")
        source_root = (ROOT / source_dir).resolve()
        if Path(source_dir).is_absolute() or not source_root.is_relative_to(ROOT.resolve()):
            raise ValueError("历史诊断来源目录越界")
        for name, expected in fingerprints.items():
            _check_source(source_root, name, expected)
        if "equivalence.json" in fingerprints:
            eq, _ = _json_bytes(source_root / "equivalence.json")
            result["lazy_equivalence"] = base._equivalence_summary(eq)
        result["history"] = history
        result["files"]["diagnostics/diagnostics.json"] = fingerprint
    path = directory / "diagnostics/local_repair_probe.json"
    if path.is_file():
        probe, fingerprint = _json_bytes(path)
        provenance = probe.get("provenance", {})
        _check_source(ROOT, provenance.get("source_solution_file"), provenance.get("source_solution_sha256"))
        _check_source(path.parent, provenance.get("solver_snapshot_file"), provenance.get("solver_snapshot_sha256"))
        for repair, item in probe.get("aggregates_by_repair", {}).items():
            counts = [item.get(k) for k in ("rounds", "infinite_regret_rounds", "infinite_with_multiple_tasks")]
            if any(type(n) is not int or n < 0 for n in counts) or not counts[0] or not counts[2] <= counts[1] <= counts[0]:
                raise ValueError(f"局部修复频率分母或计数无效：{repair}")
        result["probe"] = probe
        result["files"]["diagnostics/local_repair_probe.json"] = fingerprint
    path = directory / "diagnostics/followup_observations.json"
    if path.is_file():
        observations, fingerprint = _json_bytes(path)
        evidence = observations.get("provenance", {}).get("evidence")
        if not isinstance(evidence, dict) or set(evidence) != {"baseline", "portfolio"}:
            raise ValueError("B 机制核验缺少固定长测配对的证据")
        child = directory / "supplemental_trial"
        current = json.loads((child / "runs.json").read_bytes())
        for variant, item in evidence.items():
            saved = item.get("run_row")
            if not isinstance(saved, dict):
                raise ValueError("B 机制核验缺少原始运行行")
            matching = [row for row in current if (row.get("stage"), row.get("case_id"),
                        row.get("variant"), row.get("seed")) == ("confirm", "fleet200", variant, KNOWN_SEED)]
            if len(matching) != 1 or matching[0] != saved:
                raise ValueError("B 机制核验固定长测行与当前原始记录不一致")
            row_hash = hashlib.sha256(json.dumps(saved, sort_keys=True, separators=(",", ":"),
                                                  ensure_ascii=True, allow_nan=False).encode()).hexdigest()
            if item.get("run_row_sha256") != row_hash:
                raise ValueError("B 机制核验原始运行行指纹不一致")
            expected = item.get("solution_sha256")
            if saved.get("solution_sha256") != expected:
                raise ValueError("B 机制核验完整解指纹与运行行不一致")
            _check_source(child, saved.get("solution_file"), expected)
            _check_source(ROOT, item.get("solution_file"), expected)
        result["followup_observations"] = observations
        result["files"]["diagnostics/followup_observations.json"] = fingerprint
    return result


def _verify_budget_audit(directory: Path, report: dict[str, Any]) -> dict[str, Any] | None:
    validation = report.get("validation") or {}
    cumulative = validation.get("cumulative_budget")
    if cumulative is None:
        return None
    if not isinstance(cumulative, dict) or not isinstance(cumulative.get("cohorts"), list):
        raise ValueError("共享预算审计格式无效")
    for cohort in cumulative["cohorts"]:
        relative = cohort.get("directory")
        if relative not in {".", ".."} or (relative == ".." and directory.name != "supplemental_trial"):
            raise ValueError("共享预算审计来源目录越界")
        cohort_dir = directory / relative
        for filename, field in (("runs.json", "runs_sha256"), ("execution_notes.json", "execution_notes_sha256")):
            path = cohort_dir / filename
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            if field not in cohort or cohort[field] != actual:
                raise ValueError(f"共享预算审计陈旧：{relative}/{filename} 指纹不一致")
    probe_file, probe_hash = cumulative.get("local_probe_file"), cumulative.get("local_probe_sha256")
    allowed = {"diagnostics/local_repair_probe.json"}
    if directory.name == "supplemental_trial":
        allowed.add("../diagnostics/local_repair_probe.json")
    if probe_file is not None:
        if probe_file not in allowed:
            raise ValueError("共享预算审计 probe 来源越界")
        path = directory / probe_file
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != probe_hash:
            raise ValueError("共享预算审计陈旧：局部 probe 指纹不一致")
    elif probe_hash is not None:
        raise ValueError("共享预算审计 probe 缺少来源路径")
    return cumulative


def _confirmation(report: dict[str, Any], selected: str | None) -> dict[str, Any]:
    pairs = {
        p["seed"]: p for group in report["groups"]
        if group["stage"] == "confirm" and group["case_id"] == "fleet200" and group["budget_seconds"] == 240
        for p in group["pairs"] if p["variant"] == selected
    }
    rows = [{**pairs.get(seed, {"seed": seed, "status": "missing", "reason": "尚无完整配对"}),
             "role": "known" if seed == KNOWN_SEED else "new"}
            for seed in (KNOWN_SEED, *NEW_SEEDS)]
    counts = {
        role: {status: sum(r["role"] == role and r["status"] == status for r in rows)
               for status in ("win", "tie", "loss", "missing", "invalid")}
        for role in ("known", "new")
    }
    complete = sum(r["status"] in {"win", "tie", "loss"} for r in rows)
    passed = complete == 4 and all(r["status"] in {"win", "tie"} for r in rows) and counts["new"]["win"] >= 2
    return {"variant": selected, "rows": rows, "counts": counts, "complete_pairs": complete,
            "planned_pairs": 4, "passed_from_raw_pairs": passed}


def _stage_evidence(report: dict[str, Any], stage: str, variant: str | None) -> dict[str, Any]:
    """Count only this batch's exact case/budget/seed pairs; never borrow a baseline."""
    manifest = report["manifest"]
    protocol = manifest.get("protocol", {})
    seeds = manifest.get("seeds", {}).get(stage, [])
    groups = [g for g in report["groups"] if g["stage"] == stage]
    cases = protocol.get("stage_cases", {}).get(stage) or list(dict.fromkeys(g["case_id"] for g in groups))
    case_summaries, all_pairs = [], []
    for case in cases:
        matching = [g for g in groups if g["case_id"] == case]
        expected_budget = protocol.get("stage_budgets_seconds", {}).get(stage, {}).get(case.split("__")[0])
        if expected_budget is not None:
            matching = [g for g in matching if g["budget_seconds"] == expected_budget]
        if len(matching) > 1:
            raise ValueError("追加批次同阶段存在不同预算，不能合并门槛结果")
        group = matching[0] if matching else None
        budget = expected_budget if expected_budget is not None else group["budget_seconds"] if group else None
        existing = {p["seed"]: p for p in group["pairs"] if p["variant"] == variant} if group else {}
        planned = seeds or list(existing)
        pairs = [{**existing.get(seed, {"seed": seed, "status": "missing"}), "case_id": case,
                  "budget_seconds": budget}
                 for seed in planned] if planned else []
        all_pairs.extend(pairs)
        counts = {status: sum(p["status"] == status for p in pairs)
                  for status in ("win", "tie", "loss", "missing", "invalid")}
        valid = [p for p in pairs if p["status"] in {"win", "tie", "loss"}]
        primary_regressions = sum(p["candidate_score"][0] > p["baseline_score"][0] for p in valid)
        case_summaries.append({"case_id": case, "budget_seconds": budget,
                               "counts": counts, "complete_pairs": len(valid), "planned_pairs": len(planned),
                               "primary_regressions": primary_regressions})
    complete = bool(case_summaries) and all(c["complete_pairs"] == c["planned_pairs"] > 0 for c in case_summaries)
    fleet = next((c for c in case_summaries if c["case_id"] == "fleet200"), None)
    gate = None
    if stage == "medium" and complete:
        gate = bool(fleet and fleet["counts"]["win"] >= 2 and not any(c["primary_regressions"] for c in case_summaries))
    return {"stage": stage, "cases": case_summaries, "pairs": all_pairs, "complete": complete,
            "gate_passed_from_raw_pairs": gate}


def _elapsed_records(report: dict[str, Any]) -> dict[str, Any]:
    # Failed and over-budget runs still consumed the user's experiment budget.
    rows = [row for group in report["groups"] for row in group["rows"]]
    notes = report.get("execution_notes") or {}
    aborted = notes.get("aborted_runs", []) if isinstance(notes, dict) else []
    completed_values = [row.get("end_to_end_seconds") for row in rows]
    aborted_values = [row.get("process_elapsed_seconds") for row in aborted]
    valid_time = lambda value: base._number(value) and value >= 0
    completed = math.fsum(value for value in completed_values if valid_time(value))
    stopped = math.fsum(value for value in aborted_values if valid_time(value))
    missing = sum(not valid_time(value) for value in completed_values + aborted_values)
    return {"completed_seconds": completed, "aborted_seconds": stopped,
            "known_seconds": math.fsum((completed, stopped)), "missing_time_records": missing,
            "completed_run_count": len(rows), "aborted_run_count": len(aborted)}


def _shared_budget(report: dict[str, Any]) -> dict[str, Any]:
    followup = report["followup"]
    main = _elapsed_records(report)
    supplemental = followup.get("supplemental", {}).get("elapsed", {})
    probe = followup["diagnostics"].get("probe")
    probe_seconds = probe.get("elapsed_seconds") if probe is not None else 0.0
    probe_has_time = base._number(probe_seconds) and probe_seconds >= 0
    components = [
        {"label": "主批已完成运行", "seconds": main["completed_seconds"]},
        {"label": "主批已中止运行", "seconds": main["aborted_seconds"]},
        {"label": "追加 B 已完成运行", "seconds": supplemental.get("completed_seconds", 0.0)},
        {"label": "追加 B 已中止运行", "seconds": supplemental.get("aborted_seconds", 0.0)},
        {"label": "局部修复诊断", "seconds": probe_seconds if probe_has_time else 0.0},
    ]
    elapsed = math.fsum(item["seconds"] for item in components)
    missing = main["missing_time_records"] + supplemental.get("missing_time_records", 0) + int(not probe_has_time)
    return {"limit_seconds": 3600, "known_elapsed_seconds": elapsed,
            "remaining_upper_bound_seconds": max(0.0, 3600 - elapsed),
            "missing_time_records": missing, "components": components,
            "within_budget": False if elapsed > 3600 else None if missing else True}


def _supplemental_summary(report: dict[str, Any]) -> dict[str, Any]:
    medium = _stage_evidence(report, "medium", "portfolio")
    confirm = _confirmation(report, "portfolio")
    extensions = _stage_evidence(report, "extensions", "portfolio")
    decisions = report.get("decisions") if isinstance(report.get("decisions"), list) else []
    latest = {d.get("stage"): d for d in decisions if isinstance(d, dict)}
    confirm_decision = latest.get("confirm", {})
    has_confirm = any(g["stage"] == "confirm" for g in report["groups"])
    if confirm["passed_from_raw_pairs"]:
        status, message = "passed", "B 的原始长测配对达到门槛：反例复测无退化，三个新种子至少两胜且均无退化。"
    elif any(c["loss"] for c in confirm["counts"].values()):
        status, message = "not_promoted", "B 的长测出现词典序退化，未达到默认启用门槛。"
    elif confirm["complete_pairs"] == 4:
        status, message = "not_promoted", "B 的四组长测已完成，但新种子获胜数不足，未达到默认启用门槛。"
    elif confirm_decision.get("passed") is True:
        status, message = "inconsistent", "B 的决策记录与完整原始配对不一致，不能确认通过。"
    elif has_confirm:
        status, message = "running", f"B 的长测进行中，已完成 {confirm['complete_pairs']}/4 组有效配对；尚无最终结论。"
    elif medium["gate_passed_from_raw_pairs"] is False:
        status, message = "not_promoted", "B 的独立中测未达到集群至少两胜且两实例均无逾期数增加的门槛。"
    elif confirm_decision.get("skipped") or confirm_decision.get("passed") is False:
        status, message = "not_promoted", "B 未完成原题长测验证，现有证据不足以默认启用。"
    elif medium["gate_passed_from_raw_pairs"] is True:
        status, message = "running", "B 的独立中测通过门槛，长测尚待完成；不能将中测通过视为最终晋级。"
    else:
        status, message = "running", "B 的独立中测进行中，尚无完整门槛结论。"
    return {
        "href": "supplemental_trial/index.html", "run_count": report["run_count"],
        "decisions": report["decisions"], "source_file_sha256": report["source_file_sha256"],
        "status": status, "message": message, "audited": report.get("validation") is not None,
        "medium": medium, "confirmation": confirm, "extensions": extensions,
        "elapsed": _elapsed_records(report),
    }


def build_followup_data(output_dir: Path) -> dict[str, Any]:
    report = base.build_report_data(output_dir)
    budget_audit = _verify_budget_audit(Path(output_dir), report)
    if report["manifest"].get("schema_version") != 2 or report["manifest"].get("kind") != "regret_followup":
        raise ValueError("本报告仅接受 schema_version=2 的 regret_followup 批次")
    decisions = report.get("decisions")
    decisions = decisions if isinstance(decisions, list) else []
    latest = {d.get("stage"): d for d in decisions if isinstance(d, dict)}
    selected = next((latest[s].get("selected_variant") for s in ("confirm", "medium", "screen")
                     if latest.get(s, {}).get("selected_variant")), None)
    confirm = _confirmation(report, selected)
    decision = latest.get("confirm", {})
    notes = report.get("execution_notes") or {}
    label = SHORT_LABELS.get(selected, selected or "候选")
    if decision.get("passed") is True:
        if confirm["passed_from_raw_pairs"]:
            status, message = "passed", f"{label}通过预定长测门槛；已知反例复测与三个新种子均无词典序退化。"
        else:
            status, message = "inconsistent", "决策记录与完整配对不一致：不能确认晋级，请核对原始证据。"
    elif decision.get("skipped"):
        status, message = "not_promoted", "本轮未晋级原题长测；没有为未运行阶段填补成绩。"
    elif decision.get("passed") is False:
        status, message = "not_promoted", f"{label}未通过长测门槛；本轮证据不支持默认启用。"
    elif isinstance(notes, dict) and notes.get("early_stop"):
        status, message = "not_promoted", "长测已提前停止；已完成结果保留，未完成运行没有成绩。"
    elif latest.get("medium", {}).get("passed") is False or (
        "screen" in latest and latest["screen"].get("selected_variant") is None
    ):
        status, message = "not_promoted", "本轮候选未达到扩大测试门槛；保留当前默认算法。"
    else:
        stage = next((s for s in ("confirm", "medium", "screen")
                      if any(g["stage"] == s for g in report["groups"])), "screen")
        stage_label = {"screen": "小范围初筛", "medium": "入围中测", "confirm": "原题长测"}[stage]
        status, message = "running", f"{stage_label}进行中：当前为已完成记录快照，尚无最终晋级结论。"
    if status in {"passed", "not_promoted"} and selected and "extensions" not in latest:
        message += " 扩展兼容性尚未结束。"
    report["followup"] = {
        "status": status, "message": message, "selected_variant": selected,
        "confirmation": confirm, "diagnostics": _diagnostics(Path(output_dir)),
        "medium": _stage_evidence(report, "medium", selected),
        "extensions": _stage_evidence(report, "extensions", selected),
        "budget_audit": budget_audit,
    }
    supplemental_dir = Path(output_dir) / "supplemental_trial"
    if (supplemental_dir / "manifest.json").is_file() and (supplemental_dir / "runs.json").is_file():
        supplemental = base.build_report_data(supplemental_dir)
        child_budget = _verify_budget_audit(supplemental_dir, supplemental)
        child = _supplemental_summary(supplemental)
        child["budget_audit"] = child_budget
        if child["status"] == "passed" and child_budget and child_budget.get("budget_compliant") is not True:
            child.update(status="not_promoted", message="B 的分数配对达到长测门槛，但累计或阶段预算协议未通过；不能确认晋级。")
        report["followup"]["supplemental"] = child
    report["followup"]["shared_budget"] = _shared_budget(report)
    if report["followup"]["status"] == "passed" and budget_audit and budget_audit.get("budget_compliant") is not True:
        report["followup"].update(status="not_promoted", message="主批分数配对达到长测门槛，但预算协议未通过；不能确认晋级。")
    if report["followup"]["shared_budget"]["within_budget"] is not True:
        for result in (report["followup"], report["followup"].get("supplemental", {})):
            if result.get("status") == "passed":
                result.update(status="not_promoted", message="分数配对达到长测门槛，但共享预算已超额或计时不完整；不能确认晋级。")
    return report


FOLLOWUP_STYLE = r'''
.followup-lead{font-size:17px;line-height:1.7;color:var(--ink);margin:12px 0 0}.followup-grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}.followup-grid>div{min-width:0}.followup-grid p,.mechanism p{font-size:13px;color:var(--muted)}.evidence-tag{font-size:11px;color:var(--accent);letter-spacing:.08em}.followup-controls{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px}.followup-controls label{flex:1;min-width:130px}.diagnostic-facts{padding-left:20px;margin:12px 0;font-size:13px;color:var(--muted)}.diagnostic-facts li{padding-bottom:7px}.followup-svg svg{width:100%;display:block;overflow:hidden}.followup-svg text{font:12px Inter,"PingFang SC",system-ui,sans-serif;fill:var(--muted)}.followup-svg .value{fill:var(--ink)}.followup-table td{white-space:normal}.followup-table td:first-child{min-width:135px}.followup-status{font-size:12px;display:inline-block;padding:3px 9px;border:1px solid var(--line);border-radius:20px;margin:16px 0 0}.followup-note{margin:6px 0 16px;font-size:12px;color:var(--muted)}#confirmation-summary{margin:10px 0 15px}.mechanism{padding:20px 0}.mechanism h3{font-size:17px}.followup-hashes{overflow-wrap:anywhere}.followup-hashes code{font-size:11px}.shared-budget{padding:14px 0 4px}.budget-value{font-size:20px;font-variant-numeric:tabular-nums}.budget-track{height:6px;border-radius:3px;background:var(--line);margin:9px 0}.budget-track span{height:100%;display:block;background:var(--accent);border-radius:3px}.budget-components{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:12px;color:var(--muted)}
@media(max-width:680px){.followup-grid{grid-template-columns:1fr}.followup-lead{font-size:15px}.followup-controls{gap:10px}.followup-controls label{min-width:100%}.followup-controls select{width:100%}}
@media print{.followup-controls{display:none}.followup-grid{grid-template-columns:1fr 1fr}.followup-svg{break-inside:avoid}}
'''

FOLLOWUP_SECTIONS = r'''
<div id="equivalence-results" hidden></div>
<section aria-labelledby="diagnostic-title"><div class="section-head"><h2 id="diagnostic-title">为什么短期领先，后来被反超</h2><p>上一轮同一完整长测配对的诊断，不混用不同 seed 的短测。</p></div>
<article class="family"><div class="family-head"><div><h3>相对优势反转，最优解没有丢失</h3><p id="diagnostic-scope" class="subtle"></p></div></div>
<p class="followup-note">旧候选的 best-so-far 自身没有变坏：逾期数停在 65 后，总逾期与航程仍在改善。基线后来降到 64／63，导致相对优势反转。下图展示检查点的实际记录，不把次级目标下降当作第一目标改善。</p>
<div class="followup-controls"><label class="select-label">对齐方式<select id="diagnostic-axis"><option value="time">相同端到端时间</option><option value="iterations">相同完成迭代数</option></select></label><label class="select-label">目标指标<select id="diagnostic-metric"><option value="late_count">逾期任务数 · 越少越好</option><option value="total_lateness_min">总逾期分钟 · 越少越好</option><option value="distance_km">总航程 km · 越少越好</option></select></label></div>
<div class="legend"><span><i class="swatch" style="background:var(--series-0)"></i>基线</span><span><i class="swatch" style="background:var(--series-1)"></i>旧跨机</span></div><div id="alignment-chart" class="followup-svg"></div>
<p class="subtle">检查点读取此前最后一次完整词典序最优；虚线仅连接检查点，不表示精确中间轨迹。同迭代仅对齐轮数，不等于相同邻域工作量、温度路径或状态。</p>
<details><summary>全部检查点的三个原始指标</summary><div id="alignment-table" class="table-wrap"></div></details></article>
<div class="followup-grid"><article class="family"><span class="evidence-tag">已记录的证据</span><ul id="diagnostic-facts" class="diagnostic-facts"></ul></article><article class="family"><span class="evidence-tag">机制推断与混杂</span><p>共同迭代区间内也出现 64 对 65 的差距，因此“仅仅是吞吐变慢”不足以解释反转。早期好解后的搜索集中、自适应权重反馈与备选信息损失仍是待检验假设。</p><p id="lazy-context"></p><p>只有一个旧长测配对。相同种子在算法分歧后并不保证逐轮随机路径相同；运行顺序、机器负载、累计而非时序算子统计仍限制因果归因。</p></article></div></section>
<section aria-labelledby="mechanism-title"><div class="section-head"><h2 id="mechanism-title">regret 如何失去区分能力</h2><p>可复现的局部机制；不能直接认定为长测反转的唯一根因。</p></div>
<article class="family mechanism"><div class="followup-grid"><div><h3>位置备选与跨机备选</h3><p>传统 regret-k 比较任务的前 k 个插入位置；跨机版本要求这 k 个备选来自不同无人机。接近每机 25 任务上限时，有空任务槽的无人机可能少于 k，即使同一条路线仍有多个可行插入位置，跨机备选也不够。</p><p>旧实现把备选不足的任务都赋为 inf。当一轮多个待插任务同时为 inf，regret 无法继续区分它们的机会损失，选择更多依赖后续并列规则。inf 并不表示任务不可行。</p></div><div><h3>两个针对性改动</h3><p><strong>位置回退 A：</strong>跨机备选不足 k 个时，恢复位置 regret；若真实位置也不足，仍保留 inf。</p><p><strong>自适应组合 B：</strong>保留传统 regret2/3，另增跨机 regret2/3，由独立权重选择，并启用位置回退。新算子只在多机 DIRECT 搜索活跃，初解与单机保持原行为。</p><p>Relay 扩展先进行 DIRECT 预热，这一阶段新算子可以活跃；后续 Relay 阶段的算子池不启用组合。最终阶段 metadata 中的 false 不能用于否定预热阶段曾启用新算子。</p></div></div></article>
<article class="family"><h3>局部修复中的全 inf 轮次</h3><p id="probe-scope" class="followup-note"></p><div id="probe-chart" class="followup-svg"></div><p class="subtle">分母是局部修复的插入轮次，不是独立长测样本。深色计数为其中仍有多个待插任务的轮次；最后只剩一个任务时全 inf 并不影响任务间排序。</p><p class="notice">本诊断从一个已保存最终解做局部 destroy–repair；没有运行新的 ALNS。deadline-risk 指导关闭，与旧长测开启时不同；lazy、位置回退及组合开关均关闭，仅观察旧跨机规则。因此这些频率不能外推为长测整段发生率，也不是 A/B 干预效果。</p><details><summary>按破坏算子查看计数与分母</summary><div id="probe-table" class="table-wrap"></div></details></article></section>
<section aria-labelledby="confirmation-title"><div class="section-head"><h2 id="confirmation-title">主批长测：复现反例与新证据分开</h2><p>200 任务 / 8 架 · 每次 240 秒端到端预算</p></div><article class="family"><p id="confirmation-summary"></p><div id="confirmation-table" class="table-wrap"></div><p class="subtle">2026101321 是已知失败种子的复测，不能记作新的独立留出样本。预定门槛：四组完整配对均无词典序退化，且三个新种子至少两胜；任一退化提前停止。未完成或初解不一致不判胜，均值三元组不参与晋级。</p></article></section>
<section id="supplemental-section" hidden><div class="section-head"><h2>追加 B：独立探索批次</h2></div><article class="family"><p class="notice warning">这是观察 A 中测失败后追加的协议扩展，不属于原先预登记的唯一候选路线。重新运行该批基线与 B，保留 A 的全部失败证据；两批基线、均值与胜负不合并，也不择优选取基线。</p><p id="supplemental-summary"></p><p id="supplemental-medium-gate" class="notice"></p><div id="supplemental-stage-table" class="table-wrap"></div><div class="section-rule"></div><h3>B 的长测：已知反例 / 新种子</h3><p id="supplemental-confirmation-summary"></p><div id="supplemental-confirmation-table" class="table-wrap"></div><p class="subtle">独立追加批次的 240 秒配对。种子 1321 用于复现已知反例，不计入三个新种子的胜场。未完成或核验未通过不判胜。</p><div id="followup-mechanism" hidden><div class="section-rule"></div><h3>B 的机制核验：改动生效，仍不充分</h3><p id="followup-mechanism-note"></p><div id="followup-mechanism-table" class="table-wrap"></div><p id="followup-operators" class="subtle"></p><p class="subtle">这是已知反例的一次针对性干预。去除该 inf 机制确实发生，仍不能据此保证长期第一目标改善；累计频次只描述搜索结果相关性，缺少时序权重与被拒候选记录，不能认定自适应分配是唯一原因。最佳词典序记录自身没有退化，后期仍可能改善较低优先级目标。</p></div><details><summary>追加批次原始决策与指纹</summary><pre id="supplemental-data"></pre></details></article></section>
'''

FOLLOWUP_SCRIPT = r'''
<script>
"use strict";
(()=>{
const report=JSON.parse(document.getElementById("optimization-data").textContent),f=report.followup,d=f.diagnostics,h=d.history,p=d.probe;
const E=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=(v,digits=2)=>typeof v==="number"&&Number.isFinite(v)?v.toLocaleString("zh-CN",{minimumFractionDigits:digits,maximumFractionDigits:digits}):"—";
const $=id=>document.getElementById(id),NS="http://www.w3.org/2000/svg",names={baseline:"基线",allocation:"旧跨机",old_allocation:"旧跨机",fallback:"位置回退",portfolio:"自适应组合"};
const labels={late_count:"逾期任务数 / 项",total_lateness_min:"总逾期 / min",distance_km:"总航程 / km"};
const add=(parent,tag,attrs={},text)=>{const e=document.createElementNS(NS,tag);for(const[k,v]of Object.entries(attrs))e.setAttribute(k,v);if(text!==undefined)e.textContent=text;parent.append(e);return e};
const statusLabel={running:"进行中",passed:"达到长测门槛",not_promoted:"未晋级",inconsistent:"证据不一致"};
const mainFleet=f.medium.cases.find(c=>c.case_id==="fleet200"),extCounts=f.extensions.cases.reduce((a,c)=>({win:a.win+c.counts.win,tie:a.tie+c.counts.tie,loss:a.loss+c.counts.loss}),{win:0,tie:0,loss:0});
let mainMessage=f.message;
if(mainFleet?.complete_pairs)mainMessage+=` 中测集群：${mainFleet.counts.win} 胜 / ${mainFleet.counts.tie} 平 / ${mainFleet.counts.loss} 负。`;
if(f.extensions.complete)mainMessage+=` 扩展兼容：${extCounts.win} 胜 / ${extCounts.tie} 平 / ${extCounts.loss} 负，不能替代原题晋级。`;
$("outcome-note").innerHTML=f.supplemental?`<strong>主批 A（位置回退）</strong>：${E(mainMessage)}<br><strong>独立追加 B（自适应组合）</strong>：${E(f.supplemental.message)}`:E(mainMessage);
$("outcome-note").classList.toggle("warning",["not_promoted","inconsistent"].includes(f.supplemental?.status??f.status));
$("followup-status").textContent=f.supplemental?`主批 A ${statusLabel[f.status]} · B ${statusLabel[f.supplemental.status]} · ${report.validation&&f.supplemental.audited?"两批均已审计":"含未审计预览"}`:`${statusLabel[f.status]}${report.validation?" · 已审计":" · 未审计预览"}`;
const budget=f.shared_budget,known=budget.known_elapsed_seconds;
$("shared-budget").innerHTML=`<div><strong>共享实验预算</strong> · 两批合计上限 3600 秒（60 分钟）</div><div class="budget-value">${budget.missing_time_records?"≥ ":""}${fmt(known/60)} 分钟 <span class="subtle">/ 60 分钟 · ${fmt(known,3)} 秒</span></div><div class="budget-track" role="img" aria-label="已知累计耗时 ${fmt(known,3)} 秒，共享预算 3600 秒"><span style="width:${Math.min(100,known/36)}%;${budget.within_budget===false?"background:var(--bad)":""}"></span></div><div class="budget-components">${budget.components.map(x=>`<span>${E(x.label)} ${fmt(x.seconds,3)} s</span>`).join("")}</div><p class="subtle">完成运行按实际端到端耗时累计，超时与失败也计入；中止运行使用已记录的进程耗时，另加局部 probe 实测耗时。两份 manifest 共享这一小时额度，不是各有一小时。${budget.missing_time_records?`另有 ${budget.missing_time_records} 条计时缺失，此处为已知下界，不能确认预算达标。`:budget.within_budget===false?"已超过共享预算。":"当前记录在共享预算内。"}</p>`;
$("diagnostic-scope").textContent=h?`${h.scope.task_count} 任务 / ${h.scope.drone_count} 架 · seed ${h.scope.seed} · ${h.scope.budget_seconds} 秒预算 · 上一轮冻结记录`:"诊断文件尚未生成，暂无同种子历史证据。";
$("diagnostic-facts").innerHTML=h?(h.findings?.evidence??[]).map(t=>`<li>${E(t)}</li>`).join(""):"<li>等待诊断文件，未虚构历史结果。</li>";
const eq=d.lazy_equivalence;
$("lazy-context").textContent=eq?.all_equivalent===true?`旧候选同时启用 lazy 与跨机 regret。前轮固定迭代的 ${eq.observations.length} 组 lazy 等价检查中，路线、得分与搜索决策全部一致，支持该实现只改变计算方式；这不是对所有实例的证明。限时运行的吞吐变化仍可能影响完成轮数和最终得分。`:"旧候选同时启用 lazy 与跨机 regret。本页尚无可核验的固定迭代等价记录，不能把它视为仅改变 regret 的纯因果对照。";
function alignment(){
 const axis=$("diagnostic-axis").value,metric=$("diagnostic-metric").value,points=h?(axis==="time"?h.time_checkpoints:h.iteration_checkpoints):[],target=$("alignment-chart");target.replaceChildren();
 if(!points?.length){target.innerHTML='<p class="empty-state">暂无同种子对齐记录。</p>';$("alignment-table").textContent="未生成";return}
 const w=Math.max(220,target.clientWidth),height=280,left=64,right=18,top=25,bottom=43,values=points.flatMap(x=>[x.baseline.score[metric],x.allocation.score[metric]]),lo=Math.min(...values),hi=Math.max(...values),pad=Math.max((hi-lo)*.12,1),min=Math.max(0,lo-pad),max=hi+pad,xmin=points[0].checkpoint,xmax=points.at(-1).checkpoint;
 const x=t=>left+(t-xmin)/Math.max(xmax-xmin,1)*(w-left-right),y=v=>top+(max-v)/(max-min)*(height-top-bottom),svg=add(target,"svg",{viewBox:`0 0 ${w} ${height}`,height,role:"img","aria-label":`${labels[metric]}，同${axis==="time"?"时间":"迭代数"}检查点，种子${h.scope.seed}`});add(svg,"title",{},`${labels[metric]}：基线与旧跨机 regret 的实际检查点值`);
 for(let i=0;i<4;i++){const v=min+(max-min)*i/3,t=xmin+(xmax-xmin)*i/3;add(svg,"line",{x1:left,x2:w-right,y1:y(v),y2:y(v),stroke:"var(--line)"});add(svg,"text",{x:left-7,y:y(v)+4,"text-anchor":"end"},fmt(v,metric==="late_count"?0:1));add(svg,"text",{x:x(t),y:height-23,"text-anchor":i===0?"start":i===3?"end":"middle"},fmt(t,0))}
 add(svg,"text",{x:left,y:13},labels[metric]);add(svg,"text",{x:w-right,y:height-3,"text-anchor":"end"},axis==="time"?"端到端时间 / s":"完成迭代 / 轮");
 ["baseline","allocation"].forEach((variant,i)=>{const color=`var(--series-${i})`;add(svg,"polyline",{points:points.map(t=>`${x(t.checkpoint)},${y(t[variant].score[metric])}`).join(" "),fill:"none",stroke:color,"stroke-width":1.5,"stroke-dasharray":"4 4"});points.forEach(t=>{const c=add(svg,"circle",{cx:x(t.checkpoint),cy:y(t[variant].score[metric]),r:3.5,fill:color});add(c,"title",{},`${names[variant]} · ${t.checkpoint}${axis==="time"?"秒":"轮"}：${fmt(t[variant].score[metric],metric==="late_count"?0:3)}`)})});
 $("alignment-table").innerHTML=`<table><thead><tr><th>${axis==="time"?"秒":"迭代"}</th><th>基线（逾期 / 分钟 / km）</th><th>旧跨机（逾期 / 分钟 / km）</th><th>词典序胜者</th></tr></thead><tbody>${points.map(t=>`<tr><td>${fmt(t.checkpoint,0)}</td>${["baseline","allocation"].map(v=>`<td>${fmt(t[v].score.late_count,0)} / ${fmt(t[v].score.total_lateness_min)} / ${fmt(t[v].score.distance_km)}</td>`).join("")}<td>${E(names[t.lexicographic_winner]??"平")}</td></tr>`).join("")}</tbody></table>`;
}
function probe(){
 const target=$("probe-chart");target.replaceChildren();
 if(!p){$("probe-scope").textContent="局部修复诊断尚未生成。";return}
 $("probe-scope").textContent=`${p.cell_count} 个局部修复单元 · ${p.configuration.destroy_operators.length} 种破坏算子 × ${p.configuration.seeds.length} 个局部随机种子 × ${p.configuration.repair_operators.length} 种 regret · 每单元移除 ${p.configuration.removed_tasks} 任务`;
 const entries=Object.entries(p.aggregates_by_repair),w=Math.max(220,target.clientWidth),left=63,right=18,height=entries.length*88+35,x=v=>left+v*(w-left-right),svg=add(target,"svg",{viewBox:`0 0 ${w} ${height}`,height,role:"img","aria-label":"局部修复全 inf 轮次占比，分母为每种 regret 的插入轮次"});
 [0,.5,1].forEach(v=>{add(svg,"line",{x1:x(v),x2:x(v),y1:4,y2:height-24,stroke:"var(--line)"});add(svg,"text",{x:x(v),y:height-5,"text-anchor":v===1?"end":v===0?"start":"middle"},`${v*100}%`)});
 entries.forEach(([name,r],i)=>{const y=16+i*88,ratio=r.infinite_regret_rounds/r.rounds;add(svg,"text",{x:0,y:y+12},name);add(svg,"rect",{x:left,y:y+2,width:ratio*(w-left-right),height:14,fill:"var(--series-1)",opacity:.4,rx:2});add(svg,"rect",{x:left,y:y+2,width:r.infinite_with_multiple_tasks/r.rounds*(w-left-right),height:14,fill:"var(--series-1)",rx:2});add(svg,"text",{x:left,y:y+38,class:"value"},`全 inf ${r.infinite_regret_rounds}/${r.rounds}（${fmt(ratio*100,1)}%）`);add(svg,"text",{x:left,y:y+57},`多任务 ${r.infinite_with_multiple_tasks}/${r.rounds}（${fmt(r.infinite_with_multiple_tasks/r.rounds*100,1)}%）`)});
 $("probe-table").innerHTML=`<table><thead><tr><th>destroy / repair</th><th>插入轮次</th><th>全 inf</th><th>其中多待插任务</th></tr></thead><tbody>${p.aggregates_by_destroy_and_repair.map(r=>`<tr><td>${E(r.destroy)} / ${E(r.repair)}</td><td>${r.rounds}</td><td>${r.infinite_regret_rounds}</td><td>${r.infinite_with_multiple_tasks}</td></tr>`).join("")}</tbody></table>`;
}
const c=f.confirmation,statusNames={win:"胜",tie:"平",loss:"负",missing:"未完成配对",invalid:"核验未通过"};
const confirmationSummary=c=>`${c.variant?names[c.variant]:"尚无入围候选"} · 完整有效配对 ${c.complete_pairs}/4。已知反例：${c.counts.known.win} 胜 / ${c.counts.known.tie} 平 / ${c.counts.known.loss} 负；新种子：${c.counts.new.win} 胜 / ${c.counts.new.tie} 平 / ${c.counts.new.loss} 负（计划 3 组）。`;
const confirmationMarkup=c=>`<table><thead><tr><th>seed / 证据角色</th><th>判定</th><th>基线（逾期 / min / km）</th><th>候选（逾期 / min / km）</th></tr></thead><tbody>${c.rows.map(r=>`<tr><td>${r.seed}<br><span class="subtle">${r.role==="known"?"已知反例复测":"新种子"}</span></td><td class="${r.status==="loss"?"bad":r.status==="win"?"good":""}">${statusNames[r.status]}</td>${["baseline_score","candidate_score"].map(k=>`<td>${r[k]?r[k].map((v,i)=>fmt(v,i===0?0:2)).join(" / "):"—"}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
$("confirmation-summary").textContent=confirmationSummary(c);$("confirmation-table").innerHTML=confirmationMarkup(c);
if(f.supplemental&&c.complete_pairs===0){const section=document.querySelector('[aria-labelledby="confirmation-title"]'),article=section.querySelector("article"),details=document.createElement("details"),summary=document.createElement("summary");$("confirmation-title").textContent="主批 A：未启动长测";summary.textContent="A 中测未晋级，展开未运行的长测计划";details.append(summary,article);section.append(details)}
if(f.supplemental){
 const s=f.supplemental;$("supplemental-section").hidden=false;$("supplemental-summary").innerHTML=`<strong>${E(s.message)}</strong><br>已完成 ${s.run_count} 条运行记录 · ${s.audited?"已审计":"未审计预览"}。<a href="supplemental_trial/index.html">查看 B 的独立报告</a>`;
 $("supplemental-medium-gate").textContent=`独立中测门槛：${s.medium.gate_passed_from_raw_pairs===true?"通过":s.medium.gate_passed_from_raw_pairs===false?"未通过":"尚无完整结论"}。要求集群三组至少两胜，且单机、集群均无逾期任务数增加。逐原始配对重算，不使用主批基线。`;
 const cases={single25:"单机 25 任务",fleet200:"集群 200 任务",fleet200__direct_relay:"中继",fleet200__direct_stations:"预部署",fleet200__direct_relay_stations:"中继 + 预部署"};
 $("supplemental-stage-table").innerHTML=`<table><thead><tr><th>阶段 / 实例</th><th>预算 s</th><th>有效配对 / 计划</th><th>胜 / 平 / 负</th><th>逾期数退化</th><th>待配对 / 无效</th></tr></thead><tbody>${[s.medium,s.extensions].flatMap(stage=>stage.cases.map(r=>`<tr><td>${stage.stage==="medium"?"独立中测":"扩展兼容"} · ${E(cases[r.case_id]??r.case_id)}</td><td>${fmt(r.budget_seconds,0)}</td><td>${r.complete_pairs} / ${r.planned_pairs}</td><td>${r.counts.win} / ${r.counts.tie} / ${r.counts.loss}</td><td class="${r.primary_regressions?"bad":""}">${r.primary_regressions}</td><td>${r.counts.missing} / ${r.counts.invalid}</td></tr>`)).join("")}</tbody></table>`;
 $("supplemental-confirmation-summary").textContent=confirmationSummary(s.confirmation)+(s.confirmation.passed_from_raw_pairs?" 已达到长测门槛。":" 尚未达到长测门槛。");$("supplemental-confirmation-table").innerHTML=confirmationMarkup(s.confirmation);$("supplemental-data").textContent=JSON.stringify(s,null,2);
}
const observation=d.followup_observations;
if(observation&&f.supplemental){
 $("followup-mechanism").hidden=false;const rd=observation.run_diagnostics,common=observation.common_iterations,counters=observation.regret_counters,bc=counters.portfolio.values,groups=observation.operator_diagnostics.portfolio.groups;
 $("followup-mechanism-note").textContent=`该长测中，B 触发位置回退 ${fmt(bc.regret_position_fallback_count,0)} 次，全 inf 轮次 ${fmt(bc.regret_all_infinite_round_count,0)}；最终仍有 ${rd.portfolio.final_score.late_count} 项逾期，基线为 ${rd.baseline.final_score.late_count} 项。共同 ${common.iteration} 轮时，基线 ${common.baseline.score.late_count} 项、B ${common.portfolio.score.late_count} 项，吞吐差仍不足以单独解释结果。`;
 const scoreText=s=>`${fmt(s.late_count,0)} / ${fmt(s.total_lateness_min)} / ${fmt(s.distance_km)}`;
 const rows=[["最终：逾期 / min / km",scoreText(rd.baseline.final_score),scoreText(rd.portfolio.final_score)],
 ["完成迭代 / 轮",fmt(rd.baseline.iterations,0),fmt(rd.portfolio.iterations,0)],
 [`共同 ${common.iteration} 轮：逾期 / min / km`,scoreText(common.baseline.score),scoreText(common.portfolio.score)],
 ["最后一次逾期数改善 / 端到端秒",fmt(rd.baseline.last_primary_improvement.end_to_end_seconds),fmt(rd.portfolio.last_primary_improvement.end_to_end_seconds)],
 ["最后一次逾期数改善 / 迭代",fmt(rd.baseline.last_primary_improvement.iteration,0),fmt(rd.portfolio.last_primary_improvement.iteration,0)],
 ["第一目标末段停滞 / 搜索秒",fmt(rd.baseline.primary_plateau_seconds),fmt(rd.portfolio.primary_plateau_seconds)],
 ["位置回退次数",counters.baseline.available?fmt(counters.baseline.values.regret_position_fallback_count,0):"未采集",fmt(bc.regret_position_fallback_count,0)],
 ["全 inf 插入轮次",counters.baseline.available?fmt(counters.baseline.values.regret_all_infinite_round_count,0):"未采集",fmt(bc.regret_all_infinite_round_count,0)]];
 $("followup-mechanism-table").innerHTML=`<table><thead><tr><th>同一已知反例的机制记录</th><th>新基线</th><th>B</th></tr></thead><tbody>${rows.map(r=>`<tr>${r.map(v=>`<td>${E(v)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
 $("followup-operators").textContent=`B 的两种跨机 regret 合计使用 ${fmt(groups.route_regret.uses,0)} 次，占完成迭代 ${fmt(groups.route_regret.use_fraction*100,1)}%；两种传统位置 regret 合计 ${fmt(groups.position_regret.uses,0)} 次，占 ${fmt(groups.position_regret.use_fraction*100,1)}%。这是末端累计计数，不能确定哪一阶段或哪种算子导致停滞。`;
}
const provenance=$("followup-sources");provenance.innerHTML=Object.entries(d.files).map(([name,hash])=>`<p><a href="${E(name)}">${E(name)}</a><br><code>${E(hash)}</code></p>`).join("")||"诊断来源文件尚未生成。";
$("followup-provenance").textContent=JSON.stringify({history:h?.provenance??null,local_repair:p?.provenance??null,followup:observation?.provenance??null},null,2);
$("diagnostic-axis").addEventListener("change",alignment);$("diagnostic-metric").addEventListener("change",alignment);
alignment();probe();let pending=false;const redraw=()=>{if(pending)return;pending=true;requestAnimationFrame(()=>{pending=false;alignment();probe()})};
if(typeof ResizeObserver!=="undefined"){const observer=new ResizeObserver(redraw);observer.observe($("alignment-chart"));observer.observe($("probe-chart"))}else window.addEventListener("resize",redraw);
})();
</script>
'''


def _update_previous_entry(directory: Path, generated_html: str) -> None:
    if directory.resolve() != DEFAULT_OUTPUT.resolve():
        raise ValueError("更新旧入口仅允许默认 regret_followup 结果目录")
    previous = PREVIOUS_OUTPUT / "index.html"
    archive = PREVIOUS_OUTPUT / "index.initial.html"
    if not archive.exists():
        original = previous.read_bytes()
        # An exclusive create prevents a subsequent refresh from replacing
        # the old report with the new entry, even if two refreshes race.
        try:
            with archive.open("xb") as handle:
                handle.write(original)
        except FileExistsError:
            pass
    if "<base " in generated_html.lower():
        raise ValueError("新报告不得预先包含 base，避免相对链接重复改写")
    entry = generated_html.replace("<head>", '<head>\n<base href="../regret_followup_20261013/">', 1)
    previous.write_text(entry, encoding="utf-8")


def render_report(output_dir: Path, *, update_previous_entry: bool = False) -> Path:
    """Write only this batch's index after validating all loaded evidence."""
    directory = Path(output_dir)
    if update_previous_entry and directory.resolve() != DEFAULT_OUTPUT.resolve():
        raise ValueError("更新旧入口仅允许默认 regret_followup 结果目录")
    data = build_followup_data(directory)
    template = base.HTML_TEMPLATE.replace("物流无人机调度 · 优化实验报告", "跨无人机 regret · 退化诊断与优化复测")
    template = template.replace("物流无人机调度<br>优化实验报告", "跨无人机 regret<br>退化诊断与优化复测")
    template = template.replace("轨迹沿用求解器计时，运行限额按端到端计时核验。",
                                "本批轨迹已加入初解构造偏移，横轴为端到端时间；运行限额也按端到端计时核验。")
    template = template.replace("轨迹计时 / s", "端到端时间 / s")
    template = template.replace('<h2 id="original-title">原题考核</h2>', '<h2 id="original-title">本批次 · 原题考核</h2>')
    template = template.replace('<h2 id="extension-title">扩展兼容性</h2>', '<h2 id="extension-title">本批次 · 扩展兼容性</h2>')
    template = template.replace('<div id="run-overview"', '<div id="followup-status" class="followup-status"></div><div id="run-overview"')
    template = template.replace('</header>', '<div id="shared-budget" class="shared-budget" aria-live="polite"></div></header>', 1)
    history_link = escape(relpath(PREVIOUS_OUTPUT / "index.initial.html", directory), quote=True)
    template = template.replace('</header>', f'<p class="subtle"><a href="{history_link}">上一轮原始报告（原样归档）</a> · 原有报告验收指纹对应此归档副本。</p></header>', 1)
    start = template.index('<section aria-labelledby="equivalence-title">')
    end = template.index('<section aria-labelledby="original-title">', start)
    main_confirmation = FOLLOWUP_SECTIONS.index('<section aria-labelledby="confirmation-title">')
    supplemental = FOLLOWUP_SECTIONS.index('<section id="supplemental-section"')
    sections = (FOLLOWUP_SECTIONS[:main_confirmation] + FOLLOWUP_SECTIONS[supplemental:]
                + FOLLOWUP_SECTIONS[main_confirmation:supplemental])
    template = template[:start] + sections + template[end:]
    template = template.replace("</style>", FOLLOWUP_STYLE + "</style>", 1)
    label_start = template.index("const variantLabel=v=>")
    label_end = template.index(";", label_start)
    template = template[:label_start] + "const variantLabel=v=>(" + json.dumps(SHORT_LABELS, ensure_ascii=False) + ")[v]??v" + template[label_end:]
    evidence = '<h3>诊断来源与源码指纹</h3><div id="followup-sources" class="followup-hashes"></div><details><summary>历史证据、诊断源码与局部修复归档 SHA-256</summary><pre id="followup-provenance"></pre></details>'
    template = template.replace('<details><summary>实验协议、配置与源码指纹</summary>', evidence + '<details><summary>实验协议、配置与源码指纹</summary>', 1)
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    for value, escaped in (("&", "\\u0026"), ("<", "\\u003c"), (">", "\\u003e"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        encoded = encoded.replace(value, escaped)
    template = template.replace("__REPORT_DATA__", encoded).replace("</body>", FOLLOWUP_SCRIPT + "</body>")
    target = directory / "index.html"
    target.write_text(template, encoding="utf-8")
    if update_previous_entry:
        _update_previous_entry(directory, template)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--update-previous-entry", action="store_true",
                        help="仅默认复测目录：保留旧报告副本并更新旧 HTML 入口")
    args = parser.parse_args(argv)
    print(render_report(args.output_dir, update_previous_entry=args.update_previous_entry))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
