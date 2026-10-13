"""Render this trial's objective-first, offline report without running a solver.

Usage::

    python -m algorithm.experiments.render_objective_trials_report --output-dir DIR

Only exact stage/case/scenario/size/budget/seed pairs enter comparisons.  Every
failed, missing and regressed pair remains visible.  Aggregate metric values are
descriptive; the report never uses an averaged score tuple to declare a winner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from algorithm.experiments import render_optimization_report as base


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "algorithm/results/objective_trials_20261013"


def build_report_data(directory: Path) -> dict[str, Any]:
    """Use the established independent evidence reader, then add pair details."""
    report = base.build_report_data(Path(directory))
    manifest = report["manifest"]
    audit = report.get("validation")
    if manifest.get("kind") == "compact_trials":
        marker = manifest.get("post_hoc_exploration", {})
        if marker.get("parent_directory") != "..":
            raise ValueError("追加实验缺少父批绑定")
        for name in ("manifest", "runs", "decisions", "execution_notes"):
            path = Path(directory).parent / f"{name}.json"
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            if actual != marker.get(f"parent_{name}_sha256"):
                raise ValueError(f"追加实验父批证据已改变：{name}.json")
            if audit is not None and audit.get("parent_evidence", {}).get(f"{name}_sha256") != actual:
                raise ValueError(f"追加实验父批审计已陈旧：{name}.json")
    if manifest.get("kind") in {"objective_trials", "compact_trials"} and audit is not None:
        expected_decisions = report["source_file_sha256"].get("decisions.json")
        gate_audit = audit.get("decision_audit")
        if (not isinstance(gate_audit, dict)
                or audit.get("decisions_sha256") != expected_decisions
                or gate_audit.get("decisions_sha256") != expected_decisions):
            raise ValueError("阶段决策审计陈旧或缺失：decisions.json 指纹不一致")
        cumulative = audit.get("cumulative_budget")
        if (not isinstance(cumulative, dict)
                or cumulative.get("execution_notes_sha256")
                != report["source_file_sha256"].get("execution_notes.json")):
            raise ValueError("预算审计陈旧或缺失：execution_notes.json 指纹不一致")
    protocol = manifest.get("protocol", {})
    if not isinstance(protocol, dict):
        protocol = {"description": protocol}
    report["protocol"] = protocol
    labels = manifest.get("variant_labels", {})
    report["variant_labels"] = labels if isinstance(labels, dict) else {}
    for variant, snapshot in manifest.get("source_snapshots", {}).items():
        if isinstance(snapshot, dict) and snapshot.get("label"):
            report["variant_labels"].setdefault(variant, snapshot["label"])
    report["variant_labels"].setdefault(report["baseline_variant"], "现有默认算法")
    descriptions = manifest.get("variant_descriptions", {})
    report["variant_descriptions"] = descriptions if isinstance(descriptions, dict) else {}
    report["variant_descriptions"].setdefault("boundary", "优先移除刚逾期且单任务直飞下界仍可能按时完成的任务，并在另一架无人机上释放名额，尝试跨机重排。")
    report["variant_descriptions"].setdefault("objective", "调整自适应算子的奖励，使减少逾期任务数的改进获得更高权重。")
    report["variant_descriptions"].setdefault("compact", "使用与现有基线相同的求解器代码，把集群每次移除任务比例从 3%–8% 缩至 2%–5%（200 任务时约 4–10 项），以提高单位时间内的搜索轮数。")
    decisions = report.get("decisions") or []
    selected = next((d.get("selected_variant") for d in reversed(decisions)
                     if isinstance(d, dict) and d.get("selected_variant")), None)
    report["selected_variant"] = selected
    # Expected missing pairs are shown even if neither run has finished yet.
    for group in report["groups"]:
        planned = manifest.get("seeds", [])
        if isinstance(planned, dict):
            planned = planned.get(group["stage"], [])
        planned = planned if isinstance(planned, list) else []
        expected_variants = []
        if manifest.get("kind") in {"objective_trials", "compact_trials"}:
            if group["stage"] in {"screen", "equivalence"}:
                expected_variants = list(manifest.get("source_snapshots", {}))
            elif manifest.get("kind") == "compact_trials":
                expected_variants = list(manifest.get("source_snapshots", {}))
            elif group["stage"] == "medium":
                expected_variants = next((d.get("eligible_variants", []) for d in reversed(decisions)
                                          if d.get("stage") == "screen"), [])
            elif group["stage"] == "confirm" and selected:
                expected_variants = [selected]
        group["variants"] = list(dict.fromkeys([*group["variants"], *expected_variants]))
        for variant in group["variants"]:
            if variant == report["baseline_variant"]:
                continue
            present = {p["seed"] for p in group["pairs"] if p["variant"] == variant}
            for seed in planned:
                if seed not in present:
                    group["pairs"].append({"variant": variant, "seed": seed, "status": "missing",
                                           "reason": "计划种子尚无本阶段完整记录"})
        group["pairs"].sort(key=lambda p: (p["variant"], str(p["seed"])))
        for pair in group["pairs"]:
            by_variant = {row["variant"]: index for index, row in enumerate(group["rows"])
                          if row["seed"] == pair["seed"]}
            pair["baseline_row_index"] = by_variant.get(report["baseline_variant"])
            pair["candidate_row_index"] = by_variant.get(pair["variant"])
            if pair["status"] in {"win", "tie", "loss"}:
                left = group["rows"][pair["baseline_row_index"]]
                right = group["rows"][pair["candidate_row_index"]]
                pair["metric_deltas"] = {
                    field: right[field] - left[field]
                    for field in base.METRIC_FIELDS
                }
    # The report can be regenerated from the embedded inputs and this exact code.
    report["renderer_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report["report_kind"] = "objective_trials"
    links = list(report["source_file_sha256"])
    if (Path(directory) / "README.md").is_file():
        links.insert(0, "README.md")
    for sources_key in ("source_snapshots", "execution_sources"):
        for source in manifest.get(sources_key, {}).values():
            name = source.get("path") if isinstance(source, dict) else None
            if isinstance(name, str) and not Path(name).is_absolute():
                path = (Path(directory) / name).resolve()
                if path.is_relative_to(Path(directory).resolve()) and path.is_file():
                    links.append(name)
    report["evidence_links"] = list(dict.fromkeys(links))
    child_directory = Path(directory) / "compact_trial"
    if (child_directory / "manifest.json").is_file() and (child_directory / "runs.json").is_file():
        child = build_report_data(child_directory)
        report["related_trial"] = {
            "label": "独立追加：小移除邻域", "report_path": "compact_trial/index.html",
            "directory": "compact_trial", "manifest": child["manifest"],
            "validation": child["validation"], "decisions": child["decisions"],
            "variant_labels": child["variant_labels"], "selected_variant": child["selected_variant"],
            "source_file_sha256": child["source_file_sha256"],
            "run_count": child["run_count"],
            "known_elapsed_seconds": sum(row.get("end_to_end_seconds", 0)
                                         for group in child["groups"] for row in group["rows"]
                                         if base._number(row.get("end_to_end_seconds")))
            + sum(item["process_elapsed_seconds"]
                  for item in (child.get("execution_notes") or {}).get("aborted_runs", [])
                  if base._number(item.get("process_elapsed_seconds"))),
            "groups": [{**{key: group[key] for key in base.GROUP_FIELDS},
                        "pairs": [{key: pair[key] for key in ("seed", "variant", "status")}
                                  for pair in group["pairs"]]}
                       for group in child["groups"] if group["stage"] != "equivalence"],
        }
    return report


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>原题指标 · 本轮算法优化对比</title>
<meta name="description" content="离线呈现无人机调度算法的原题指标、同预算逐种子对比、收敛轨迹及独立审计。">
<style>
:root{color-scheme:light dark;--bg:#f7f8f5;--paper:#fff;--ink:#202e29;--muted:#5d6b63;--line:#dce3dc;--soft:#eef2eb;--base:#537ca0;--candidate:#16846b;--good:#177153;--bad:#a54038;--accent:#265c45;--warn:#946318}
@media(prefers-color-scheme:dark){:root{--bg:#151c18;--paper:#1b2720;--ink:#e6eee8;--muted:#adbeb2;--line:#3b4b40;--soft:#24372b;--base:#a0bbdb;--candidate:#80ceb8;--good:#8ad1ac;--bad:#f2a59e;--accent:#a7d9bc;--warn:#e7c180}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 "PingFang SC","Microsoft YaHei",system-ui,sans-serif}main{max-width:1200px;margin:auto;padding:40px 36px 32px}header{padding:0 0 26px;border-bottom:1px solid var(--line)}.eyebrow{font-size:12px;letter-spacing:.12em;color:var(--accent)}h1{font-size:clamp(28px,4vw,42px);line-height:1.25;letter-spacing:-.03em;font-weight:500;margin:12px 0}h2{font-size:23px;line-height:1.4;font-weight:500;margin:0 0 8px}h3{font-size:16px;font-weight:500;margin:0 0 4px}p{margin:8px 0}.muted,.intro,.caption{color:var(--muted)}.intro{max-width:880px}.caption{font-size:12px;line-height:1.6}.facts{display:flex;flex-wrap:wrap;gap:5px 24px;font-size:13px;margin:18px 0 0}.facts b{font-weight:500;color:var(--ink)}section{margin-top:30px}.outcome{border-left:3px solid var(--accent);padding:6px 0 6px 16px;margin-top:24px}.outcome.warning{border-color:var(--bad)}.outcome h2{font-size:20px}.controls{display:flex;flex-wrap:wrap;gap:14px 18px;align-items:end;margin:22px 0 10px}label{display:grid;gap:4px;min-width:130px;max-width:100%;font-size:12px;color:var(--muted)}select,button{font:inherit;color:var(--ink);background:var(--paper);border:1px solid var(--line);border-radius:5px;min-height:40px;padding:7px 11px}select{font-size:14px;width:100%;max-width:100%}button{font-size:12px}.scope{font-size:13px;margin:14px 0 8px}.legend{display:flex;flex-wrap:wrap;gap:8px 23px;font-size:13px;margin:12px 0}.legend span{display:flex;align-items:center;gap:8px}.swatch{display:inline-block;width:18px;height:4px;background:var(--base)}.swatch.candidate{background:var(--candidate)}.metrics{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:22px 32px;margin:22px 0}.metric,.curve{min-width:0}.metric svg,.curve svg{width:100%;display:block;overflow:visible}.metric svg text,.curve svg text{font:12px "PingFang SC",system-ui,sans-serif;fill:var(--ink)}.metric-value{font-variant-numeric:tabular-nums;font-size:24px;letter-spacing:-.02em;line-height:1.45;margin:9px 0 0}.metric-value span{font-size:15px;color:var(--muted)}.delta{font-size:13px}.metric .caption{min-height:20px}.good{color:var(--good)}.bad{color:var(--bad)}.warning-text{color:var(--warn)}.paired{border-top:1px solid var(--line);padding-top:20px}.counts{display:flex;flex-wrap:wrap;gap:8px 20px;font-size:13px;margin:12px 0}.counts strong{font-size:21px;font-weight:500;font-variant-numeric:tabular-nums;margin-right:4px}.countbar{display:flex;height:8px;max-width:480px;gap:2px;margin:8px 0 12px}.countbar span{min-width:2px;background:var(--line)}.table-wrap{overflow-x:auto;margin-top:14px}table{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}td,th{padding:10px 9px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap;vertical-align:top}td:first-child,th:first-child{text-align:left}th{font-weight:500;color:var(--muted)}td.status{text-align:left;white-space:normal;min-width:140px}td small{display:block;font-size:11px;color:var(--muted)}.curves{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px}.curve-controls{display:flex;align-items:end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:15px 0}.detail{background:var(--soft);padding:12px 14px;margin:14px 0;font-size:13px;overflow-wrap:anywhere}.pipeline{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:22px;margin:18px 0}.stage{border-top:3px solid var(--line);padding-top:12px}.stage p{font-size:13px;color:var(--muted)}.stage .stage-count{font-size:20px;font-variant-numeric:tabular-nums}.stage h3{font-size:15px}.evidence-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px 34px;margin-top:18px}.evidence-grid p{font-size:13px}.evidence-grid dd{margin:0 0 12px;font-size:13px;color:var(--muted);overflow-wrap:anywhere}.evidence-grid dt{font-size:13px;margin-bottom:4px}.table-wrap caption{text-align:left;margin-bottom:10px;color:var(--muted)}details{margin-top:14px}summary{padding:8px 0;color:var(--accent);font-size:13px;cursor:pointer}pre{font:12px/1.6 ui-monospace,SFMono-Regular,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--soft);padding:14px;max-height:440px;overflow:auto}a{color:var(--accent);text-underline-offset:3px}.source-links{display:flex;gap:8px 20px;flex-wrap:wrap;font-size:13px}.empty{font-size:13px;color:var(--muted);padding:20px 0}.foot{margin-top:32px;border-top:1px solid var(--line);padding-top:18px;display:flex;justify-content:space-between;gap:18px;flex-wrap:wrap;font-size:12px;color:var(--muted)}.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)}.tooltip{font-size:12px;margin-top:6px;min-height:42px;color:var(--muted);font-variant-numeric:tabular-nums}.curve svg [data-chart-hit]{touch-action:pan-y}.chart-frame{fill:none;stroke:var(--line);stroke-width:1}.axis-label{fill:var(--muted)!important}.chip{font-size:12px;color:var(--muted);padding:3px 0}.error{color:var(--bad)}noscript{display:block;padding:16px}
@media(max-width:900px){.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.curves{grid-template-columns:1fr}.evidence-grid{grid-template-columns:1fr}.pipeline{grid-template-columns:1fr;gap:14px}}
@media(max-width:520px){main{padding:25px 16px}.controls{display:grid;grid-template-columns:1fr;gap:10px}.controls label{min-width:0}.metrics{grid-template-columns:1fr;gap:24px}.metric-value{font-size:25px}.curves{gap:20px}h2{font-size:21px}.counts{gap:5px 15px}.legend{gap:5px 16px}.outcome{padding-left:12px}.scope{overflow-wrap:anywhere}select{font-size:16px}.curve-controls label{width:100%}.foot{display:block}.foot button{margin-top:10px}}
@media print{body{background:white;color:#222}main{max-width:none;padding:14px}.controls,button{display:none}section,.metric,.curve{break-inside:avoid}.metrics{grid-template-columns:repeat(3,minmax(0,1fr))}.curves{grid-template-columns:1fr}.table-wrap{overflow:visible}details[open] pre{max-height:none}}
</style>
</head><body><main id="objective-report">
<header><div class="eyebrow">物流网络中的无人机调度 / 本轮有界优化实验</div><h1>先看原题指标，再看改进是否稳定</h1><p class="intro">相同题目数据、相同初解、相同种子与相同端到端预算，比较现有默认算法与本轮候选。先小范围筛选，有改善证据才进入中长测；所有退化、失败与未完成记录保留。</p><div class="facts" id="facts"></div></header>
<div id="outcome" class="outcome" aria-live="polite"></div>
<aside id="related-trial" class="outcome" hidden aria-label="独立追加实验"></aside>
<section aria-labelledby="metric-heading"><h2 id="metric-heading">原题考核指标的前后对比</h2><p class="caption">下方均值仅描述当前候选与基线的有效同种子配对；样本范围同步显示。是否获胜按每个种子的目标顺序判断，不能从均值拼出一个“平均赢家”。</p>
<div class="controls"><label>实验阶段<select id="stage"></select></label><label>题目场景与求解预算<select id="scenario"></select></label><label>对比候选<select id="variant"></select></label></div><div class="scope" id="scope" aria-live="polite"></div><div class="legend"><span><i class="swatch"></i><span id="baseline-label">现有默认算法</span></span><span><i class="swatch candidate"></i><span id="candidate-label">本轮候选</span></span></div><div class="metrics" id="metrics"></div></section>
<section class="paired" aria-labelledby="paired-heading"><h2 id="paired-heading">逐种子结果：改善与退化一起看</h2><div class="counts" id="pair-counts"></div><div class="countbar" id="countbar" aria-hidden="true"></div><p class="caption">单项变化统一为“候选 − 基线”。按时完成数／率越高越好；逾期数、总里程及时间越低越好。词典序依次比较逾期任务数 → 总逾期分钟（项目次级目标）→ 总里程。</p><div class="table-wrap" id="pairs"></div><details><summary>查看未纳入比较的运行与原因</summary><div id="excluded"></div></details></section>
<section aria-labelledby="curve-heading"><h2 id="curve-heading">同种子的搜索收敛</h2><div class="curve-controls"><p class="caption">每条线是当时的最优完整解；原始改进点以阶梯线连接，不插值制造改进。</p><label>种子<select id="seed"></select></label></div><div id="curve-state" class="caption"></div><div class="curves" id="curves"></div><div class="tooltip" id="curve-detail" aria-live="polite">点击或轻触曲线，查看该时刻双方的完整目标值。</div></section>
<section class="paired" aria-labelledby="method-heading"><h2 id="method-heading">筛选、确认与实施决定</h2><p class="caption">确认阶段即使出现一次退化，仍跑完预定种子评估稳定性。只有中长测均改善且无明显退化，才考虑替换默认算法。</p><div class="pipeline" id="pipeline"></div><div class="evidence-grid"><div><h3>方法与题目口径</h3><dl id="method"></dl></div><div><h3>证据与实验预算</h3><dl id="audit"></dl></div></div><details><summary>阶段决策与候选配置</summary><pre id="decisions"></pre></details><details><summary>数据、代码与独立审计指纹</summary><pre id="fingerprints"></pre></details><details><summary>完整审计记录</summary><pre id="validation"></pre></details><div class="source-links" id="sources"></div></section>
<footer class="foot"><span id="generated"></span><button type="button" id="print">打印 / 保存 PDF</button></footer>
<noscript>此报告的交互图表需要 JavaScript。原始 manifest.json、runs.json、decisions.json 与 validation.json 与报告同目录保存，可直接核查。</noscript>
</main><script type="application/json" id="report-data">__REPORT_DATA__</script>
<script>
(()=>{'use strict';
const D=JSON.parse(document.getElementById('report-data').textContent), $=id=>document.getElementById(id), root=$('objective-report');
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const finite=v=>typeof v==='number'&&Number.isFinite(v), fmt=(v,n=2)=>finite(v)?new Intl.NumberFormat('zh-CN',{maximumFractionDigits:n,minimumFractionDigits:0}).format(v):'—', signed=(v,n=2)=>finite(v)?v!==0&&Math.abs(v)<10**(-n)?`${v>0?'+':'−'}<${10**(-n)}`:`${v>0?'+':''}${fmt(v,n)}`:'—';
const stageNames={equivalence:'单机固定轮次等价',screen:'小范围初筛',medium:'中测',confirm:'长测确认',extensions:'扩展场景',final:'最终确认'}, statusNames={win:'胜',tie:'平',loss:'退化',missing:'未配对',invalid:'无效'};
const names={late_count:'逾期任务数',total_lateness_min:'总逾期分钟',distance_km:'总里程',on_time_count:'按时完成数',on_time_rate:'按时完成率',end_to_end_seconds:'端到端时间'};
const units={late_count:'项',total_lateness_min:'分钟',distance_km:'km',on_time_count:'项',on_time_rate:'%',end_to_end_seconds:'秒'};
const metrics=['on_time_count','on_time_rate','distance_km','late_count','end_to_end_seconds'];
const baseline=D.baseline_variant, label=v=>D.variant_labels[v]||v, stageLabel=v=>stageNames[v]||v;
const seedRole=s=>D.protocol.confirm_seed_roles?.[String(s)]||'';
for(const g of D.groups)for(const p of g.pairs){p.baseline=Number.isInteger(p.baseline_row_index)?g.rows[p.baseline_row_index]:null;p.candidate=Number.isInteger(p.candidate_row_index)?g.rows[p.candidate_row_index]:null;}
const scaleValue=(v,f)=>f==='on_time_rate'?100*v:v, unit=f=>units[f], value=(v,f)=>`${fmt(scaleValue(v,f),f==='late_count'||f==='on_time_count'?2:2)} ${unit(f)}`;
const valid=p=>['win','tie','loss'].includes(p.status), color=v=>v===baseline?'var(--base)':'var(--candidate)';
const stages=[...new Set([...D.stages,...D.groups.map(g=>g.stage)])], stageKey=g=>g.stage;
const keyOptions=[...new Set(D.groups.filter(g=>g.stage!=='equivalence').map(stageKey))].sort((a,b)=>stages.indexOf(a)-stages.indexOf(b));
const defaultGroup=[...D.groups].reverse().find(g=>g.case_id==='fleet200'&&g.stage==='confirm')||[...D.groups].reverse().find(g=>g.case_id==='fleet200')||D.groups[0];
let group=null,variant=null,seed=null;
function options(el,values,chosen,toText){el.innerHTML=values.map(v=>`<option value="${esc(v)}"${String(v)===String(chosen)?' selected':''}>${esc(toText(v))}</option>`).join('');el.disabled=!values.length;}
function rowDelta(a,b,f){if(!a||!b||!finite(a[f])||!finite(b[f]))return '—';const d=scaleValue(b[f]-a[f],f);return `${signed(d)}${f==='on_time_rate'?' 个百分点':' '+unit(f)}`;}
function pairsFor(){return group?group.pairs.filter(p=>p.variant===variant):[];}
function svgText(x,y,text,anchor='start',cls=''){return `<text x="${x}" y="${y}" text-anchor="${anchor}" class="${cls}">${esc(text)}</text>`;}
function drawMetric(container,field,pairs){
 const usable=pairs.filter(valid);if(!usable.length){container.innerHTML=`<h3>${names[field]}</h3><p class="empty">暂无有效同种子配对</p>`;return;}
 const left=usable.map(p=>scaleValue(p.baseline[field],field)),right=usable.map(p=>scaleValue(p.candidate[field],field));
 const mean=a=>a.reduce((s,x)=>s+x,0)/a.length, a=mean(left),b=mean(right),delta=b-a, better=(field==='on_time_count'||field==='on_time_rate')?delta>0:delta<0;
 const max=Math.max(...left,...right,1)*1.04,w=Math.max(260,container.clientWidth),x0=4,x1=w-6,x=v=>x0+v/max*(x1-x0), h=95;
 const range=(values,y,series)=>`<rect x="${x0}" y="${y}" width="${Math.max(.4,x(mean(values))-x0)}" height="17" fill="${series}"/><path d="M${x(Math.min(...values))},${y+25}H${x(Math.max(...values))}" stroke="${series}" stroke-width="2"/><circle cx="${x(Math.min(...values))}" cy="${y+25}" r="2" fill="${series}"/><circle cx="${x(Math.max(...values))}" cy="${y+25}" r="2" fill="${series}"/>`;
 const pct=field==='on_time_rate'?' 个百分点':` ${unit(field)}`;
 container.innerHTML=`<h3>${names[field]} <span class="caption">${field==='on_time_count'||field==='on_time_rate'?'↑ 越高越好':'↓ 越低越好'}</span></h3><div class="metric-value">${fmt(a)} <span>→</span> ${fmt(b)} <span>${unit(field)}</span></div><div class="delta ${delta===0?'muted':better?'good':'bad'}">${delta===0?'持平':`变化 ${signed(delta)}${pct}`}</div><svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(names[field]+'，现有算法均值 '+fmt(a)+'，候选均值 '+fmt(b)+'，单位 '+unit(field))}"><title>${esc(names[field])}，有效配对均值与范围</title>${range(left,8,'var(--base)')}${range(right,51,'var(--candidate)')}${svgText(x0,93,'0','start','axis-label')}${svgText(x1,93,fmt(max)+' '+unit(field),'end','axis-label')}</svg><p class="caption">范围：现有 ${fmt(Math.min(...left))}–${fmt(Math.max(...left))}；候选 ${fmt(Math.min(...right))}–${fmt(Math.max(...right))}</p>`;
}
function renderPairs(){
 const pairs=pairsFor(),counts=Object.fromEntries(Object.keys(statusNames).map(s=>[s,pairs.filter(p=>p.status===s).length]));
 $('pair-counts').innerHTML=Object.entries(counts).map(([s,n])=>`<span class="${s==='win'?'good':s==='loss'||s==='invalid'?'bad':'muted'}"><strong>${n}</strong>${statusNames[s]}</span>`).join('');
 $('countbar').innerHTML=Object.entries(counts).filter(([,n])=>n).map(([s,n])=>`<span style="flex:${n};background:${s==='win'?'var(--good)':s==='loss'?'var(--bad)':s==='tie'?'var(--base)':'var(--line)'}"></span>`).join('');
 if(!pairs.length){$('pairs').innerHTML='<p class="empty">本阶段尚无候选配对记录。</p>';$('excluded').innerHTML='';return;}
 $('pairs').innerHTML=`<table><caption>每个单元格显示：现有 → 候选；下行为变化。未配对／无效结果不进入均值。</caption><thead><tr><th>种子</th><th>按时完成</th><th>完成率</th><th>总里程</th><th>逾期数</th><th>端到端时间</th><th>总逾期分钟<br>项目次级目标</th><th>逐种子结论</th></tr></thead><tbody>${pairs.map(p=>`<tr><td>${esc(p.seed)}</td>${[...metrics,'total_lateness_min'].map(f=>`<td>${p.baseline?fmt(scaleValue(p.baseline[f],f)):'—'} → ${p.candidate?fmt(scaleValue(p.candidate[f],f)):'—'}<small>${rowDelta(p.baseline,p.candidate,f)}</small></td>`).join('')}<td class="status ${p.status==='win'?'good':p.status==='loss'||p.status==='invalid'?'bad':'muted'}">${statusNames[p.status]}<small>${esc(p.reason||(p.decisive_metric?`由${names[p.decisive_metric]}决定`:'三级目标相同'))}</small></td></tr>`).join('')}</tbody></table>`;
 const excluded=group.rows.filter(r=>[baseline,variant].includes(r.variant)&&(!r.report_usable||!pairs.some(p=>valid(p)&&p.seed===r.seed)));
 $('excluded').innerHTML=excluded.length?`<div class="table-wrap"><table><thead><tr><th>算法 / 种子</th><th>原因</th><th>实际时间</th></tr></thead><tbody>${excluded.map(r=>`<tr><td>${esc(label(r.variant))} / ${esc(r.seed)}</td><td class="status">${esc(r.report_issues.join('；')||'缺少有效同种子对手，不纳入比较')}</td><td>${fmt(r.end_to_end_seconds)} 秒</td></tr>`).join('')}</tbody></table></div>`:'<p class="caption">所选算法在本组内无被排除的运行。</p>';
}
function points(row,field){if(!row)return [];const ix={late_count:2,total_lateness_min:3,distance_km:4}[field];return (row.best_trajectory||[]).filter(p=>Array.isArray(p)&&finite(p[1])&&finite(p[ix])).map(p=>({x:p[1],y:p[ix]}));}
function at(values,t){let v=null;for(const p of values){if(p.x>t)break;v=p;}return v;}
function renderCurves(){
 const pair=pairsFor().find(p=>String(p.seed)===String(seed));$('curves').innerHTML='';$('curve-detail').textContent='点击或轻触曲线，查看该时刻双方的完整目标值。';
 if(!pair){$('curve-state').textContent='暂无轨迹配对。';return;}
 $('curve-state').textContent=`种子 ${pair.seed} · ${statusNames[pair.status]}${group.stage==='confirm'&&seedRole(pair.seed)?' · '+seedRole(pair.seed):''}${valid(pair)?' · 与上方同一预算、同一场景':` · ${pair.reason||'未纳入比较'}`}`;
 const rows=[pair.baseline,pair.candidate].filter(Boolean);
 for(const f of ['late_count','distance_km','total_lateness_min']){
  const div=document.createElement('div');div.className='curve';$('curves').appendChild(div);div.innerHTML=`<h3>${names[f]}${f==='total_lateness_min'?' · 项目次级目标':''}</h3>`;
  const series=rows.map(r=>({row:r,values:points(r,f)})).filter(s=>s.values.length),all=series.flatMap(s=>s.values.map(p=>p.y));
  if(!all.length){div.insertAdjacentHTML('beforeend','<p class="empty">没有保存可用的改进轨迹。</p>');continue;}
  const w=Math.max(260,div.clientWidth),h=242,l=66,r=w-12,top=18,bottom=189,maxX=group.budget_seconds;
  const low=Math.min(...all),high=Math.max(...all),pad=Math.max((high-low)*.08,f==='late_count'?1:Math.max(Math.abs(high)*.025,.1)),minY=Math.max(0,low-pad),maxY=high+pad;
  const x=t=>l+(r-l)*t/maxX,y=v=>bottom-(bottom-top)*(v-minY)/(maxY-minY),ticks=w<400?3:4;
  let html=`<svg viewBox="0 0 ${w} ${h}" role="img" aria-label="${esc(`种子 ${pair.seed} 的${names[f]}搜索轨迹`)}"><title>${esc(names[f]+'随端到端时间的变化')}</title><rect class="chart-frame" data-chart-frame x="${l}" y="${top}" width="${r-l}" height="${bottom-top}"/>`;
  for(let i=0;i<3;i++){const v=minY+(maxY-minY)*i/2,yy=y(v);html+=`<path d="M${l},${yy}H${r}" stroke="var(--line)"/>${svgText(l-8,yy+4,fmt(v,f==='late_count'?1:1),'end','axis-label')}`;}
  for(let i=0;i<ticks;i++){const t=maxX*i/(ticks-1);html+=svgText(x(t),bottom+20,fmt(t,1),i===0?'start':i===ticks-1?'end':'middle','axis-label');}
  html+=svgText(l,12,unit(f),'start','axis-label')+svgText((l+r)/2,h-12,'端到端时间（秒）','middle','axis-label');
  for(const s of series){const ps=s.values.filter(p=>p.x<=maxX);let d='';ps.forEach((p,i)=>{d+=i?`H${x(p.x)}V${y(p.y)}`:`M${x(p.x)},${y(p.y)}`;});if(ps.length){const end=Math.min(maxX,finite(s.row.end_to_end_seconds)?s.row.end_to_end_seconds:ps.at(-1).x);d+=`H${x(Math.max(end,ps.at(-1).x))}`;}html+=`<path d="${d}" fill="none" stroke="${color(s.row.variant)}" stroke-width="2"${s.row.variant===baseline?' stroke-dasharray="5 3"':''}/>`;}
  html+=`<line data-guide x1="${l}" x2="${l}" y1="${top}" y2="${bottom}" stroke="var(--muted)" visibility="hidden"/><rect data-chart-hit data-chart-hover-overlay="cross-series" x="${l}" y="${top}" width="${r-l}" height="${bottom-top}" fill="transparent"/></svg>`;div.insertAdjacentHTML('beforeend',html);
  const svg=div.querySelector('svg'),overlay=svg.querySelector('[data-chart-hit]'),guide=svg.querySelector('[data-guide]');
  function show(e){const rect=svg.getBoundingClientRect(),sx=(e.clientX-rect.left)*w/rect.width,t=Math.max(0,Math.min(maxX,(sx-l)/(r-l)*maxX));guide.setAttribute('x1',x(t));guide.setAttribute('x2',x(t));guide.setAttribute('visibility','visible');$('curve-detail').textContent=`${fmt(t)} 秒 · `+rows.map(row=>`${label(row.variant)}：`+['late_count','total_lateness_min','distance_km'].map(k=>{const v=at(points(row,k),t);return `${names[k]} ${v?fmt(v.y):'尚未产生初解'}${v?' '+unit(k):''}`;}).join(' / ')).join('；');}
  overlay.addEventListener('pointermove',show);overlay.addEventListener('click',show);overlay.addEventListener('pointerleave',()=>guide.setAttribute('visibility','hidden'));
 }
}
function renderSelection(){
 if(!group){$('scope').textContent='尚无实验结果；不会生成虚构的对比。';$('metrics').innerHTML='';$('pairs').innerHTML='';$('curves').innerHTML='';return;}
 const pairs=pairsFor(),n=pairs.filter(valid).length;$('baseline-label').textContent=label(baseline);$('candidate-label').textContent=label(variant);
 $('scope').textContent=`${group.drone_count===1?'原题单机':'原题 '+group.drone_count+' 架集群'} · ${group.task_count} 项任务 · ${stageLabel(group.stage)} · 每次 ${fmt(group.budget_seconds)} 秒预算 · ${n}/${pairs.length} 组有效同种子配对${n<2?'（样本不足以判断稳定性）':''}`;
 $('metrics').innerHTML=metrics.map(f=>`<div class="metric" data-metric="${f}"></div>`).join('');$('metrics').querySelectorAll('[data-metric]').forEach(el=>drawMetric(el,el.dataset.metric,pairs));renderPairs();
 const seeds=pairs.map(p=>p.seed);seed=seeds.some(s=>String(s)===String(seed))?seed:seeds[0];options($('seed'),seeds,seed,s=>`${s} · ${statusNames[pairs.find(p=>p.seed===s).status]}`);renderCurves();
}
function chooseGroup(){const matches=D.groups.filter(g=>stageKey(g)===$('stage').value);options($('scenario'),matches.map(g=>g.id),matches.some(g=>g.id===$('scenario').value)?$('scenario').value:(matches.find(g=>g.case_id===group?.case_id)||matches.find(g=>g.drone_count===8)||matches[0])?.id,id=>{const g=D.groups.find(x=>x.id===id);return `${g.drone_count===1?'单机':g.drone_count+' 架集群'} / ${g.task_count} 项 / ${fmt(g.budget_seconds)} 秒${g.scenario!=='pure_direct'?' / '+g.scenario:''}`;});chooseVariant();}
function chooseVariant(){group=D.groups.find(g=>g.id===$('scenario').value)||null;const variants=group?group.variants.filter(v=>v!==baseline):[];variant=variants.includes(variant)?variant:variants.includes(D.selected_variant)?D.selected_variant:variants[0];options($('variant'),variants,variant,label);renderSelection();}
function renderEvidence(){
 const m=D.manifest,p=D.protocol,decisions=Array.isArray(D.decisions)?D.decisions:[],audit=D.validation,budget=p.total_experiment_seconds||p.shared_total_limit_seconds;
 const rows=D.groups.flatMap(g=>g.rows),aborted=D.execution_notes?.aborted_runs||[],elapsed=rows.reduce((s,r)=>s+(finite(r.end_to_end_seconds)?r.end_to_end_seconds:0),0)+aborted.reduce((s,r)=>s+(finite(r.process_elapsed_seconds)?r.process_elapsed_seconds:0),0),missingTimes=rows.filter(r=>!finite(r.end_to_end_seconds)).length+aborted.filter(r=>!finite(r.process_elapsed_seconds)).length;
 const shared=audit?.cumulative_budget,sharedTotal=finite(shared?.parent_experiment_seconds)?shared.total_seconds:null;
 $('facts').innerHTML=`<span>范围 <b>原题单机 + 8 架集群</b></span><span>已记录 <b>${D.run_count} 次运行</b></span><span>本批求解 <b>${fmt(elapsed/60)} 分钟</b></span><span>共享预算 <b>${sharedTotal===null?'':fmt(sharedTotal/60)+' / '}${budget?fmt(budget/60)+' 分钟':'未记录'}</b></span><span>证据审计 <b>${audit?.evidence_verified?'已核验':audit?'未通过':'尚未审计'}</b></span>`;
 if(m.kind==='compact_trials'){root.querySelector('.eyebrow').textContent='独立追加实验 / 更小的移除邻域';root.querySelector('h1').textContent='更小的邻域，能否稳定改善原题指标';root.querySelector('.intro').textContent=(m.post_hoc_exploration?.reason||'首轮候选中测失败后，经用户选择追加小邻域实验。')+' 本批使用原45分钟预算的剩余部分，重新运行相同种子、相同初解、相同预算的基线；所有退化与失败保留。';$('facts').insertAdjacentHTML('beforeend','<span>独立批次 <b>本页不合并父批结果</b></span>');}
 const final=decisions.findLast(d=>d.stage==='confirm');
 const eligible=audit?.evidence_verified===true&&audit?.all_runs_successful===true&&audit?.decision_audit?.protocol_complete===true&&audit?.decision_audit?.default_eligible===true,confirmed=final?.passed===true;
 const losses=D.groups.filter(g=>g.stage!=='equivalence').flatMap(g=>g.pairs).filter(p=>p.status==='loss').length;
 let headline=eligible?'本轮结论：候选达到默认替换门槛':confirmed?'长测记录通过，等待独立审计确认':final?.passed===false?'本轮决定：保留现有默认算法':'先保留现有默认算法，按阶段证据评估候选';
 $('outcome').classList.toggle('warning',Boolean(final?.passed===false));$('outcome').innerHTML=`<h2>${headline}</h2><p class="caption">${esc(final?.reason||'当前尚无通过完整中长测并实施替换的记录。')}${losses?` 本批记录含 ${losses} 组退化，均可在逐种子表查看。`:''}</p>`;
 if(D.related_trial){const child=D.related_trial,ca=child.validation,complete=ca?.decision_audit?.protocol_complete===true,passed=ca?.decision_audit?.default_eligible===true&&ca?.all_runs_successful===true,counts=child.groups.map(g=>{const ps=g.pairs;return `${stageLabel(g.stage)} / ${g.drone_count===1?'单机':'8 机'} / ${fmt(g.budget_seconds)} 秒：${ps.filter(p=>p.status==='win').length} 胜、${ps.filter(p=>p.status==='tie').length} 平、${ps.filter(p=>p.status==='loss').length} 退化${ps.some(p=>!valid(p))?'，含未配对／无效记录':''}`;});const cb=ca?.cumulative_budget;$('related-trial').hidden=false;$('related-trial').innerHTML=`<h2><a href="${esc(child.report_path)}">${esc(child.label)} → 查看独立对比</a></h2><p>首轮两项候选的失败结论保留在本页。追加候选重新生成同批基线，不与首轮或历史批次拼接均值。</p><p class="caption">追加状态：${passed?'达到默认替换门槛':complete?'已完成，未达到默认替换门槛':'进行中／待审计'}；${child.run_count} 次运行，追加已知求解时间 ${fmt(child.known_elapsed_seconds/60)} 分钟。${finite(cb?.total_seconds)?` 父子累计 ${fmt(cb.total_seconds/60)} / ${fmt(cb.total_budget_seconds/60)} 分钟，${cb.budget_compliant?'共享预算核验通过':'共享预算未通过核验'}。`:''}${counts.map(c=>'<br>'+esc(c)).join('')}</p>`;}
 const knownStages=[...new Set(['screen','medium','confirm',...stages])].filter(s=>s!=='equivalence');
 $('pipeline').innerHTML=knownStages.map(s=>{
  const gs=D.groups.filter(g=>g.stage===s),ps=gs.flatMap(g=>g.pairs),v=ps.filter(valid),d=decisions.findLast(x=>x.stage===s),planned=m.seeds?.[s];
  const breakdown=[...new Set(ps.map(p=>p.variant))].map(variant=>{
   const fleet=gs.filter(g=>g.drone_count>1).flatMap(g=>g.pairs).filter(p=>p.variant===variant);
   const single=gs.filter(g=>g.drone_count===1).flatMap(g=>g.pairs).filter(p=>p.variant===variant);
   return `<p>${esc(label(variant))}<br>集群 ${fleet.filter(p=>p.status==='win').length} 胜 / ${fleet.filter(p=>p.status==='tie').length} 平 / <span class="${fleet.some(p=>p.status==='loss')?'bad':''}">${fleet.filter(p=>p.status==='loss').length} 退化</span>${single.length?`；单机 ${single.filter(p=>p.status==='win').length} 胜 / ${single.filter(p=>p.status==='tie').length} 平 / ${single.filter(p=>p.status==='loss').length} 退化`:''}</p>`;
  }).join('');
  return `<div class="stage"><h3>${stageLabel(s)}</h3><div class="stage-count">${v.length} <span class="caption">有效配对合计${Array.isArray(planned)?` / 每候选每场景计划 ${planned.length} 种子`:''}</span></div>${breakdown}<p>${ps.filter(x=>x.status==='invalid').length} 无效 · ${ps.filter(x=>x.status==='missing').length} 未配对</p><p>${esc(d?.reason||(!gs.length?'未运行／尚无记录':'已有结果，尚无阶段决定'))}</p>${d?`<span class="chip">${d.skipped?'跳过':d.passed===true?'阶段通过':d.passed===false?'阶段未通过':d.selected_variant?'选择 '+label(d.selected_variant):'已记录'}</span>`:''}</div>`;
 }).join('');
 const method=[['原题直接指标','按时完成任务数／率、逾期任务数、总飞行里程与端到端求解时间。总逾期分钟为项目引入的次级目标，独立标注。'],['配对与目标顺序',p.comparison||'同批、同阶段、同场景、同预算、同种子、相同初解；逐种子比较逾期数 → 总逾期分钟 → 总里程。'],['时间边界',p.time_boundary||'以 runs.json 的 end_to_end_seconds 字段为准。'],['初筛条件',p.quality_gate||p.screen_gate||'查看完整阶段协议。'],['中测条件',p.medium_gate||'查看完整阶段协议。'],['长测条件',p.confirmation_gate||p.promotion||'中长测均改善且无明显退化才替换。'],['默认替换条件',p.default_policy||'中长测均改善且无明显退化才替换。'],['单机验证',p.single_drone||'以实际单机配对为准。'],['候选方法',Object.entries(D.variant_descriptions).filter(([v])=>m.source_snapshots?.[v]).map(([v,s])=>label(v)+'：'+s).join('；')||'候选配置及代码快照见下方证据。']];
 $('method').innerHTML=method.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
 const eqRaw=audit?.decision_audit?.equivalence??decisions.find(d=>d.stage==='equivalence')?.candidates??(m.kind==='compact_trials'?decisions.find(d=>d.stage==='equivalence')?.passed:undefined),eq=typeof eqRaw==='boolean'?{compact:eqRaw}:eqRaw;
 const facts=[['有效与异常运行',`${D.usable_run_count}/${D.run_count} 次符合本报告有效性条件；${D.excluded_run_count} 次被排除；另有 ${aborted.length} 次中止记录。`],['已知求解耗时',`${fmt(elapsed)} 秒${budget?`，本轮共享上限 ${fmt(budget)} 秒`:''}。${sharedTotal!==null?`父批 ${fmt(shared.parent_experiment_seconds)} 秒 + 本批 ${fmt(shared.compact_experiment_seconds)} 秒 = 累计 ${fmt(sharedTotal)} 秒。`:''}${missingTimes?`${missingTimes} 条计时缺失，不能确认总预算合规。`:'已计入已完成运行和有计时的中止尝试。'} 固定轮次等价运行在 runs.json 内只计一次。独立审计与报告制作不计入求解时间。`],['单机固定轮次等价',eq?Object.entries(eq).map(([v,ok])=>`${label(v)}：${ok?'通过':'未通过'}`).join('；')+(m.kind==='compact_trials'?'。固定200轮搜索决策指纹一致性核验，完整解独立复核；不作为质量胜负。':'。完整路线、目标、搜索决策、权重与不含计时的轨迹对照；不作为质量胜负。'):'尚无固定轮次等价记录。'],['独立审计',audit?`原始证据${audit.evidence_verified?'通过':'未通过'}核验；${audit.validated_unique_solutions??'—'} 个完整解复核；预算违规 ${(audit.over_budget_rows||[]).length} 条。`:'尚无 validation.json；本页仅为未审计预览。'],['基线版本',(m.baseline_commit||'未记录')],['证据边界','当前页面不合并历史批次或不等预算。场景／阶段／候选选择同步更新指标、逐种子结果和轨迹。少量种子不代表统计显著性。']];
 $('audit').innerHTML=facts.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('');
 $('decisions').textContent=JSON.stringify({decisions:D.decisions,protocol:p,source_snapshots:m.source_snapshots,execution_notes:D.execution_notes},null,2);$('fingerprints').textContent=JSON.stringify({input_sha256:m.input_sha256,source_file_sha256:D.source_file_sha256,renderer_sha256:D.renderer_sha256,source_snapshots:m.source_snapshots},null,2);$('validation').textContent=JSON.stringify(audit,null,2);
 $('sources').innerHTML=D.evidence_links.map(name=>`<a href="${encodeURI(name)}">${esc(name)}</a>`).join('')+(m.kind==='compact_trials'?'<a href="../index.html">返回首轮候选报告</a>':'');$('generated').textContent=`生成于 ${new Date(D.generated_at_utc).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}（北京时间） · 离线自包含 · 指标由嵌入的原始记录计算`;
}
renderEvidence();options($('stage'),keyOptions,defaultGroup?stageKey(defaultGroup):null,stageLabel);group=defaultGroup;chooseGroup();
$('stage').addEventListener('change',chooseGroup);$('scenario').addEventListener('change',chooseVariant);$('variant').addEventListener('change',()=>{variant=$('variant').value;renderSelection();});$('seed').addEventListener('change',()=>{seed=$('seed').value;renderCurves();});$('print').addEventListener('click',()=>window.print());
let oldWidth=root.clientWidth,resizeTimer;new ResizeObserver(()=>{if(root.clientWidth!==oldWidth){oldWidth=root.clientWidth;clearTimeout(resizeTimer);resizeTimer=setTimeout(renderSelection,100);}}).observe(root);
})();
</script></body></html>
'''


def render_report(directory: Path, destination: Path | None = None) -> Path:
    directory = Path(directory)
    report = build_report_data(directory)
    payload = json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # The payload is data, not markup, including when a source field contains </script>.
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    target = destination or directory / "index.html"
    if report.get("related_trial"):
        render_report(directory / "compact_trial")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(HTML_TEMPLATE.replace("__REPORT_DATA__", payload), encoding="utf-8")
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT, help="已有实验 JSON 所在目录")
    parser.add_argument("--destination", type=Path, help="报告输出位置；默认实验目录下的 index.html")
    args = parser.parse_args(argv)
    print(render_report(args.output_dir, args.destination).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
