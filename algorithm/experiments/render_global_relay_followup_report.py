"""Render audited terminal-polish/relay-budget trials as an offline HTML report."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "algorithm/results/global_relay_followup_20261014"
HISTORICAL = ROOT / "algorithm/results/final_scenario_comparison"
LITERATURE = ROOT / "papers/notes/global_relay_followup_20261014"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def within(directory: Path, relative: str) -> Path:
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError(f"Artifact escapes batch directory: {relative}")
    return path


def build_data(directory: Path, historical: Path) -> dict:
    manifest, runs, decisions, audit = (
        load(directory / name) for name in
        ("manifest.json", "runs.json", "decisions.json", "validation.json")
    )
    if manifest.get("kind") != "global_relay_followup":
        raise ValueError("Wrong experiment kind")
    if audit.get("passed") is not True or audit.get("protocol_complete") is not True:
        raise ValueError("Only complete, independently audited batches may be rendered")
    if audit.get("run_count") != len(runs):
        raise ValueError("Audit run count does not match")
    fingerprints = {}
    for name in ("manifest", "runs", "decisions"):
        fingerprints[name] = sha256(directory / f"{name}.json")
        if audit.get(f"{name}_sha256") != fingerprints[name]:
            raise ValueError(f"Stale audit: {name}.json")
    fingerprints["validation"] = sha256(directory / "validation.json")
    context = {}
    for name in ("environment_notes", "final_source_delta", "execution_notes"):
        path = directory / f"{name}.json"
        if path.exists():
            fingerprints[name] = sha256(path)
            audited_hash = audit.get(f"{name}_sha256")
            if audited_hash is not None and audited_hash != fingerprints[name]:
                raise ValueError(f"Stale audit: {name}.json")
            context[name] = load(path)
    for source in [*manifest["source_snapshots"].values(), *manifest["execution_sources"].values()]:
        if sha256(within(directory, source["path"])) != source["sha256"]:
            raise ValueError("Frozen source fingerprint mismatch")
    input_path = ROOT / manifest["input_path"]
    if sha256(input_path) != manifest["input_sha256"]:
        raise ValueError("Input data changed since audited experiment")
    required_metrics = ("late_count", "total_lateness_min", "distance_km", "end_to_end_seconds", "on_time_rate")
    seen = set()
    for row in runs:
        key = tuple(row[k] for k in ("stage", "case_id", "variant", "seed"))
        if key in seen:
            raise ValueError("Duplicate experiment cell")
        seen.add(key)
        if sha256(within(directory, row["solution_file"])) != row["solution_sha256"]:
            raise ValueError("Solution fingerprint differs from audited run")
        if any(not math.isfinite(float(row[m])) for m in required_metrics):
            raise ValueError("Nonfinite result metric")
        if row.get("valid") is not True or row.get("budget_compliant") is not True:
            raise ValueError("Batch contains invalid or over-budget result")
        if row["source_sha256"] != manifest["source_snapshots"][row["variant"]]["sha256"]:
            raise ValueError("Run source differs from frozen manifest")
        metadata = load(within(directory, row["solution_file"]))["metadata"]
        for field in ("terminal_polish", "relay_statistics"):
            source_field = "relay" if field == "relay_statistics" else field
            if row.get(field, {}) != metadata.get(source_field, {}):
                raise ValueError(f"Run metadata mismatch: {field}")
        for field, value in row.get("phase_statistics", {}).items():
            if value != metadata.get(field):
                raise ValueError(f"Phase metadata mismatch: {field}")
    history_manifest = load(historical / "manifest.json")
    history_summary = load(historical / "summary.json")
    history_runs = load(historical / "runs.json")
    history = []
    for summary in history_summary:
        group = [row for row in history_runs if row["scenario"] == summary["scenario"]]
        if not group or len(group) != summary["runs"]:
            raise ValueError("Historical summary count mismatch")
        for field, raw in (("mean_late_count", "late_count"),
                           ("mean_total_lateness_min", "total_lateness_min"),
                           ("mean_distance_km", "distance_km"),
                           ("mean_wall_seconds", "wall_seconds")):
            if not math.isclose(summary[field], mean(r[raw] for r in group), rel_tol=1e-10, abs_tol=1e-7):
                raise ValueError(f"Historical summary mismatch: {field}")
        history.append(summary | {"budget_seconds": history_manifest["scenario_budgets_seconds"][summary["scenario"]],
                                  "on_time_rate": 1-summary["mean_late_count"]/history_manifest["tasks"]})
    history_fingerprints = {n: sha256(historical / f"{n}.json") for n in ("manifest", "runs", "summary")}
    production = load(directory / "production.json")
    if production.get("validation_sha256") != fingerprints["validation"]:
        raise ValueError("Production decision is not bound to the current audit")
    final_source_hash = production.get("final_source_delta_sha256")
    if (final_source_hash is not None or "final_source_delta" in fingerprints) and (
            final_source_hash != fingerprints.get("final_source_delta")):
        raise ValueError("Production decision is not bound to the final source delta")
    if not isinstance(production.get("default_enabled"), bool):
        raise ValueError("Production decision must state the default setting")
    if production["default_enabled"] and not any(
            d.get("stage") == "validate" and d.get("passed") is True for d in decisions):
        raise ValueError("Cannot report adoption without passed validation")
    fingerprints["production"] = sha256(directory / "production.json")
    literature = load(LITERATURE / "provenance.json")
    literature_fingerprints = {n: sha256(LITERATURE / n) for n in ("reading_notes.md", "provenance.json")}
    diagnosis = {}
    diagnosis_fingerprints = {}
    for name in ("previous_batch_diagnosis", "relay_phase_diagnosis", "saved_incumbent_probe"):
        path = directory / "diagnosis" / f"{name}.json"
        diagnosis[name] = load(path)
        diagnosis_fingerprints[name] = sha256(path)
        record = diagnosis[name]
        bindings = record.get("input_bindings", record.get("source_bindings", record.get("source_sha256", {})))
        # Saved diagnosis is historical; source code may evolve after the probe.
        # Bind its historical inputs and saved solutions, not today's solver files.
        for source, fingerprint in bindings.items():
            if source.startswith("algorithm/results/") or source == manifest["input_path"]:
                path = within(ROOT, source)
                if sha256(path) != fingerprint:
                    raise ValueError(f"Stale diagnostic input: {source}")
    prior = diagnosis["previous_batch_diagnosis"]
    relay = diagnosis["relay_phase_diagnosis"]
    probe = diagnosis["saved_incumbent_probe"]
    previous_runs = load(ROOT / prior["source_directory"] / "runs.json")
    screen_relay = [r for r in previous_runs if r["stage"] == "screen" and r.get("relay_warmup_score")]
    if len(screen_relay) != relay["relay_screen_run_count"]:
        raise ValueError("Prior relay diagnosis count mismatch")
    mismatch = 0
    for candidate in [r for r in screen_relay if r["variant"] == "downstream"]:
        reference = next(r for r in screen_relay if r["case_id"] == candidate["case_id"]
                         and r["seed"] == candidate["seed"] and r["variant"] == "global")
        mismatch += candidate["relay_warmup_score"] != reference["relay_warmup_score"]
    diagnosis = dict(
        global_summary=prior["global_summary"], downstream_summary=prior["downstream_summary"],
        global_pairs=prior["global_vs_baseline"], warmup_mismatch=mismatch,
        relay_count=len(screen_relay), relay_primary_gains=sum(r["late_count"] < r["relay_warmup_score"][0]
                                                             for r in screen_relay),
        relay_facts=relay["aggregate_facts"], probe_summary=probe["summary"],
        probe_results=[{k: v for k, v in r.items() if k != "routes"} for r in probe["results"]],
        probe_limitations=probe["limitations"],
    )
    analysis_path = directory / "diagnosis/followup_analysis.json"
    analysis = load(analysis_path)
    if (analysis.get("kind") != "read_only_followup_common_iteration_diagnosis"
            or analysis.get("batch_protocol_complete") is not True
            or analysis.get("no_new_searches") is not True
            or analysis.get("validated_run_count") != len(runs)):
        raise ValueError("Followup mechanism analysis is incomplete or mismatched")
    for relative, fingerprint in analysis["input_bindings"].items():
        if sha256(within(ROOT, relative)) != fingerprint:
            raise ValueError(f"Stale followup analysis input: {relative}")
    if sha256(directory / "diagnosis/analyze_followup.py") != analysis["script_sha256"]:
        raise ValueError("Followup analysis script differs from recorded source")
    diagnosis_fingerprints["followup_analysis"] = sha256(analysis_path)
    analysis_fields = ("method", "terminal_summary", "terminal_by_stage", "terminal_vs_baseline",
                       "evidence_summary", "limitations")
    mechanism = {k: analysis[k] for k in analysis_fields}
    fields = ("stage", "variant", "case_id", "scenario", "seed", "task_count", "drone_count", "budget_seconds",
              "end_to_end_seconds", "late_count", "total_lateness_min", "distance_km", "on_time_count", "on_time_rate",
              "iterations", "relay_statistics", "terminal_polish", "phase_statistics", "relay_warmup_score", "solution_file",
              "solution_sha256", "initial_routes_sha256", "decision_fingerprint", "valid", "budget_compliant")
    compact_runs = [{k: r[k] for k in fields if k in r} for r in runs]
    return dict(manifest=manifest, runs=compact_runs, decisions=decisions, audit=audit,
                fingerprints=fingerprints, history=history, history_manifest=history_manifest,
                history_fingerprints=history_fingerprints, production=production,
                literature=literature, literature_fingerprints=literature_fingerprints,
                context=context, diagnosis=diagnosis, diagnosis_fingerprints=diagnosis_fingerprints,
                mechanism=mechanism)


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>末端优化与中继预算调整 · 配对实验</title>
<style>
:root{color-scheme:light dark;--bg:light-dark(#fbfbfc,#16191d);--fg:light-dark(#202830,#eef1f5);--muted:light-dark(#52616e,#bac4cf);--border:light-dark(#d5dce3,#414b55);--surface:light-dark(#eef2f5,#222c35);--base:light-dark(#58738a,#98b7d1);--candidate:light-dark(#b84232,#f39b83);--phase-other:light-dark(#a4aab1,#6a7784);--good:light-dark(#156743,#82d2ac);--bad:light-dark(#a13131,#efa6a6)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:400 15px/1.65 system-ui,-apple-system,"PingFang SC",sans-serif}main{max-width:1230px;margin:auto;padding:32px 28px 55px}h1,h2,h3{font-weight:500;line-height:1.4}h1{font-size:29px;margin:7px 0 14px}h2{font-size:22px;margin:30px 0 10px}h3{font-size:16px;margin:0 0 7px}p{margin:8px 0}a{color:var(--good)}.eyebrow,.muted,figcaption{color:var(--muted)}.eyebrow{font-size:13px;letter-spacing:.06em}.lead{max-width:930px}.outcome{padding:15px 0;border-bottom:1px solid var(--border)}.controls{display:flex;flex-wrap:wrap;gap:16px;padding:17px 0}.controls label{display:grid;gap:3px;max-width:100%}select{font:inherit;color:var(--fg);background:var(--bg);padding:7px 10px;border:1px solid var(--border);border-radius:4px;min-height:42px;max-width:100%}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:26px 32px;margin:20px 0}.grid>figure{min-width:0}figure{margin:0}figcaption{font-size:13px;margin:5px 0}.chart{display:block;width:100%;overflow:hidden}.chart text{font:12px system-ui,-apple-system,"PingFang SC",sans-serif;fill:var(--fg)}.legend{display:flex;flex-wrap:wrap;gap:18px;font-size:13px}.swatch{display:inline-block;width:12px;height:12px;margin-right:6px;vertical-align:middle}.section{border-top:1px solid var(--border);padding-top:8px;margin-top:24px}.table-wrap{overflow-x:auto;margin:14px 0}table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}th,td{padding:8px;border-bottom:1px solid var(--border);text-align:left;white-space:nowrap}th{font-weight:500}.num{text-align:right}.good{color:var(--good)}.bad{color:var(--bad)}.source{overflow-wrap:anywhere;font-size:12px}.note{font-size:13px}.measure{font-variant-numeric:tabular-nums}details{margin:16px 0}summary{cursor:pointer;font-weight:500;min-height:32px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--surface);padding:14px;font-size:12px}noscript{display:block;padding:20px}.footer{padding-top:20px;margin-top:30px;border-top:1px solid var(--border)}@media(max-width:720px){main{padding:22px 16px 35px}.grid{grid-template-columns:1fr;gap:23px}h1{font-size:25px}h2{font-size:20px}.controls{gap:12px}.controls label{width:100%}}@media print{details{display:block}.controls{display:none}main{max-width:none}.grid{grid-template-columns:1fr 1fr}}
</style></head><body><main id="followup-report">
<div class="eyebrow">京东物流杯 · 2026-10-14 · 完整实例配对实验</div>
<h1 id="headline">末端优化与中继预算调整</h1>
<p class="lead" id="conclusion"></p>
<div class="outcome"><p id="audit-state" class="measure"></p><p class="note muted">官方 200 项任务 / 8 架无人机 / 每机最多 25 项 / 载荷 2 / 0.9 km·min⁻¹。四策略使用相同阶段总预算。按时完成、航程和求解时长是题目直接关注指标；总逾期分钟是项目沿用的次级目标。</p></div>
<div class="controls"><label>实验阶段<select id="stage"></select></label><label>对比版本<select id="comparison"></select></label></div>
<p id="comparison-note" class="note"></p>
<div class="legend"><span><i class="swatch" style="background:var(--base)"></i><span id="base-label"></span></span><span><i class="swatch" style="background:var(--candidate)"></i><span id="candidate-label"></span></span></div>
<section><h2 id="overview-title">四策略 · 相同预算</h2><p id="overview-note" class="note muted"></p><div id="overview" class="grid"></div></section>
<section class="section"><h2>逐种子变化，保留每次退化</h2><div class="controls"><label>详细场景<select id="case"></select></label></div><p id="pair-outcome" class="measure"></p><div id="paired" class="grid"></div><h3>本阶段全部配对成绩</h3><p class="note muted">每格为前者 → 后者（后者减前者）。显示值经过四舍五入，胜平负按原始未舍入的逾期任务数、总逾期分钟、航程严格词典序判定；均值无法抵消某个种子的退化。“仅浮点尾差”只作展示标记：逾期数相同、总逾期差绝对值 ≤ 10⁻⁹ min、航程差绝对值 ≤ 10⁻⁹ km，且严格得分不相等；不改胜平负或冻结门槛。</p><div id="paired-table" class="table-wrap"></div></section>
<section class="section"><h2>中继使用与实际跨机交接</h2><p class="note muted">中继数仅解释机制，不作为优劣指标。站点允许先卸后取；K 按最终交付责任计数，前半程不占 K，属于本项目中继扩展。真实跨机由同一任务两段是否使用不同无人机判断。</p><div id="relay-plots" class="grid"></div><div id="relay-table" class="table-wrap"></div></section>
<section class="section"><h2>预算如何分配，末端实际做了什么</h2><p class="note">中继场景的末端优化发生在 DIRECT 预热结束时，后续仍会执行中继。图中的局部前后分数只说明该调用是否改进自己的输入；它不能代替同预算 main 配对收益。记录为空时显示“—”，不补零。95% 版本还启用截止前已核验播种解 checkpoint，是组合策略。</p><p id="phase-aggregate" class="note measure"></p><div id="phase-plots" class="grid"></div><div id="phase-table" class="table-wrap"></div><div id="polish-plots" class="grid"></div><div id="polish-table" class="table-wrap"></div></section>
<section class="section"><h2>末端为何仍会输：事后轨迹诊断</h2><p id="mechanism-summary"></p><p class="note">只比较实际存在的 terminal / main 配对，不把 rebalanced 混入。比较对象是共同保守迭代范围内记录的最好得分前缀，忽略时间戳并剔除已识别的末端改善；不是所有接受状态、路线或随机选择。相同前缀与不同终点符合墙钟截断位置不同的解释，但不能分别归因为机器负载、预留时间或实现速度。</p><div id="mechanism-plots" class="grid"></div><details><summary>逐对诊断与方法边界</summary><div id="mechanism-table" class="table-wrap"></div><p class="note">阶段前后差值不是可相加的因果贡献；中继之后的终局还受改变后的 DIRECT 起点影响。跨阶段的 18 对汇总仅用于机制诊断，性能胜率仍分阶段展示。<a href="diagnosis/followup_analysis.json">完整只读诊断及来源指纹</a></p></details></section>
<section class="section"><h2>晋级、验证与默认采用</h2><div id="decisions"></div><p id="production-details" class="note"></p><p class="note muted">30 秒初筛跑完整 200 项任务、四个场景、三个配对种子；通过冻结门槛才跑 60 秒和三个新种子。选中方案在无中继场景复用 terminal，不产生额外样本；三种对比视角也共享同一批运行。验证只运行所选方案，不补造未测版本。</p><details><summary>冻结协议与单机兼容</summary><div id="compatibility"></div><pre id="protocol"></pre></details></section>
<section class="section"><h2>为什么调整：上一轮诊断与额外机制探针</h2><p id="diagnosis-note"></p><div id="diagnosis-plots" class="grid"></div><p class="note">旧批结果来自 20 秒预算，不能与本轮 30 / 60 秒直接算提升。旧批首个种子曾与一次测试短暂重叠，吞吐只是描述性观察，不作纯速度结论。机制探针在历史已保存的终局上另加最多 0.5 秒；不是新端到端配对实验。6 个带中继终局没有完整 DIRECT 预热路线，按协议跳过。图中探针收益不能记入旧预算或本轮收益；时间或距离约 10⁻¹³ 量级的浮点差别虽参与严格判定，不解释为实质收益。</p><details><summary>诊断与探针原始记录</summary><p><a href="diagnosis/previous_batch_diagnosis.json">旧批诊断</a> · <a href="diagnosis/relay_phase_diagnosis.json">中继阶段诊断</a> · <a href="diagnosis/saved_incumbent_probe.json">末端机制探针</a> · <a href="../global_relay_trials_20261014/index.html">旧批完整报告</a></p><div id="probe-table" class="table-wrap"></div></details></section>
<section class="section" id="context-section" hidden><h2>实验环境与最终源码边界</h2><div id="environment-disclosure"></div><div id="source-disclosure"></div></section>
<section class="section"><h2>历史四策略 · 单独展示</h2><p class="note">此处来自仓库 <code>final_scenario_comparison</code> 的 3 个历史种子，预算为 240 / 480 / 480 / 720 秒，与本轮预算不同，不参与本轮胜率或改善率计算。仓库当前汇总与提供截图的部分数字略有差异，以下按可追溯结果文件展示，未照抄截图数值。</p><div id="history" class="grid"></div><div id="history-table" class="table-wrap"></div></section>
<section class="section"><h2>文献如何转为本轮尝试</h2>
<p>本轮把已有跨路线改进移到已核验最好 DIRECT 解的末端，并独立比较“95% DIRECT 预热 + 播种 checkpoint”组合。主循环随机调用与奖励规则未改，不代表墙钟截断后的终局或预热分数相同。末端保留时间、95% 比例都是项目假设，不能用论文成绩替代本题实测。</p>
<ul><li><a href="https://doi.org/10.1287/trsc.2021.1059">Accorsi &amp; Vigo（2021），FILO</a>：查读原文 §2.2.1–2.2.4 与 §2.3 开头；便宜邻域达到局部最优后才用昂贵层，近期改变区域优先检查。本轮借鉴预算分层，没有完整复现 FILO。</li><li><a href="https://doi.org/10.2139/ssrn.273397">Lourenço 等（2001），Iterated Local Search</a>：原始摘要；局部搜索、扰动和接受规则共同影响最终效果。本轮一次末端调用是后处理，不冒称完整 ILS。</li><li><a href="https://doi.org/10.1287/trsc.2019.0914">Christiaens &amp; Vanden Berghe（2020），SISR</a>：原始摘要；有边界的相邻请求移除/重插作为后续方向，本轮未实现。</li></ul>
<p class="note">paper-lookup 的 7 次有界 HTTP 请求、失败与阅读范围见 <a href="../../../papers/notes/global_relay_followup_20261014/reading_notes.md">阅读笔记</a> · <a href="../../../papers/notes/global_relay_followup_20261014/provenance.json">检索来源</a>。软件方法引用：Kassis, T., Agarwal, V., He, Y., Patel, D., &amp; Brueckner, A. M.（2026），<a href="https://arxiv.org/abs/2609.00065">Scientific Agent Skills</a>；沿用同日已核验记录，不是物流性能证据。</p></section>
<footer class="footer"><h3>复核与复现</h3><p><a href="manifest.json">冻结协议</a> · <a href="runs.json">全部运行</a> · <a href="decisions.json">决策记录</a> · <a href="validation.json">独立审计</a> · <a href="README.md">复现说明</a></p><p class="note muted">固定墙钟预算下，机器负载会改变搜索迭代进度；三个种子仅描述当前实例的有限观测，不是普遍优势或显著性检验。图中所有数据直接嵌入，断网仍可切换和查看；外部引用仅在点击时访问。</p><details><summary>来源指纹</summary><p id="fingerprints" class="source"></p></details></footer>
<noscript>请启用 JavaScript 查看本地嵌入的交互图表；全部原始成绩也可从 runs.json 与 runs.csv 读取。</noscript>
</main><script>
const DATA=__DATA__;
(()=>{'use strict';
const D=DATA,root=document.getElementById('followup-report'),$=id=>document.getElementById(id);
const names={baseline:'main',terminal:'末端',rebalanced:'95%组合'},cases=D.manifest.protocol.cases,caseNames={fleet200:'直送 · 原点',fleet200__direct_relay:'直送 + 中继',fleet200__direct_stations:'直送 + 预部署',fleet200__direct_relay_stations:'直送 + 预部署 + 中继'},histNames={pure_direct:'直送 · 原点',direct_relay:'直送 + 中继',direct_stations:'直送 + 预部署',direct_relay_stations:'直送 + 预部署 + 中继'},stageNames={screen:'30 秒 · 全规模初筛',validate:'60 秒 · 新种子验证'};
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),fmt=(v,d=2)=>Number(v).toFixed(d),signed=(v,d=2)=>(v>1e-9?'+':'')+fmt(Math.abs(v)<1e-9?0:v,d),avg=a=>a.reduce((s,x)=>s+x,0)/a.length;
const score=r=>[r.late_count,r.total_lateness_min,r.distance_km],cmp=(a,b)=>{let x=score(a),y=score(b);for(let i=0;i<3;i++)if(x[i]!==y[i])return x[i]<y[i]?-1:1;return 0;};
// Display-only diagnostic: the strict cmp above remains the gate and verdict rule.
const floatOnly=(b,c)=>cmp(c,b)!==0&&b.late_count===c.late_count&&Math.abs(c.total_lateness_min-b.total_lateness_min)<=1e-9&&Math.abs(c.distance_km-b.distance_km)<=1e-9;
function floatSummary(ps){const tiny=ps.filter(([b,c])=>floatOnly(b,c)),wins=tiny.filter(([b,c])=>cmp(c,b)<0).length;return `其中 ${wins} 胜 / ${tiny.length-wins} 负仅浮点尾差（非实质收益）；展示标记不改变严格门槛。`;}
const metrics=[['late_count','逾期任务数 ↓','项',2,1],['on_time_rate','按时完成率 ↑','%',2,100],['distance_km','总航程 ↓','km',2,1],['end_to_end_seconds','实际求解时间','s',3,1],['total_lateness_min','总逾期分钟 ↓ · 项目次级','min',2,1]];
let plots=[];
function compareSpec(){const v=$('comparison').value;return v==='budget'?{view:v,a:'terminal',b:'rebalanced',cases:D.manifest.protocol.relay_cases}: {view:v,a:'baseline',b:v==='terminal'?'terminal':'selected',cases};}
function pairs(stage,caseId,spec=compareSpec()){const rs=D.runs.filter(r=>r.stage===stage&&r.case_id===caseId),candidate=spec.b==='selected'?(D.manifest.protocol.relay_cases.includes(caseId)?D.decisions.find(d=>d.stage==='screen').selected_relay_variant:'terminal'):spec.b;return rs.filter(r=>r.variant===spec.a).sort((a,b)=>a.seed-b.seed).map(b=>[b,rs.find(c=>c.seed===b.seed&&c.variant===candidate)]).filter(p=>p[1]);}
function tally(ps){const n=[0,0,0];ps.forEach(([b,c])=>n[cmp(c,b)+1]++);return n;}
function wtl(ps){const n=tally(ps);return `${n[0]} 胜 / ${n[1]} 平 / ${n[2]} 负`;}
const initial=D.decisions.find(d=>d.stage==='screen'),validation=D.decisions.find(d=>d.stage==='validate'),enabled=D.production?.default_enabled===true;
$('headline').textContent=enabled?'末端与预算调整：已达到默认采用门槛':'末端与预算调整：完整配对结果';
const screenPairs=cases.flatMap(c=>pairs('screen',c,{a:'baseline',b:'selected'})),validationPairs=cases.flatMap(c=>pairs('validate',c,{a:'baseline',b:'selected'}));
const lateDelta=ps=>ps.reduce((sum,[b,c])=>sum+c.late_count-b.late_count,0);
$('conclusion').textContent=(validationPairs.length?`60 秒新种子验证：选中方案对 main ${wtl(validationPairs)}，${validationPairs.length} 组配对逾期任务总差 ${signed(lateDelta(validationPairs),0)} 项，${validation?.passed?'通过':'未通过'}采用门槛。30 秒初筛为 ${wtl(screenPairs)}。`:`30 秒完整规模初筛：选中方案对 main ${wtl(screenPairs)}，${screenPairs.length} 组配对逾期任务总差 ${signed(lateDelta(screenPairs),0)} 项；未通过初筛门槛，按协议停止扩大。`)+floatSummary(validationPairs.length?validationPairs:screenPairs)+(enabled?'已纳入默认配置。':'新开关保持关闭，继续使用现有 main 默认算法。');
$('audit-state').textContent=`${D.runs.length} 次运行独立审计通过；累计实测 ${fmt(D.runs.reduce((s,r)=>s+r.end_to_end_seconds,0)/60)} 分钟；同一输入、配对初解、种子和阶段总预算。`;
const stages=['screen','validate'].filter(s=>D.runs.some(r=>r.stage===s));$('stage').innerHTML=stages.map(s=>`<option value="${s}">${stageNames[s]}</option>`).join('');$('stage').value=stages.includes('validate')?'validate':'screen';
function setComparison(){const old=$('comparison').value,screen=$('stage').value==='screen';$('comparison').innerHTML='<option value="selected">main → 按协议选中方案</option>'+(screen?'<option value="terminal">main → 末端强化（四场景）</option><option value="budget">末端 → 95%组合（中继场景）</option>':'');if(screen&&['selected','terminal','budget'].includes(old))$('comparison').value=old;setCases();}
function setCases(){const old=$('case').value,cs=compareSpec().cases;$('case').innerHTML=cs.map(c=>`<option value="${c}">${caseNames[c]}</option>`).join('');$('case').value=cs.includes(old)?old:cs[cs.length-1];}
function fig(id,title,caption=''){return `<figure><h3>${esc(title)}</h3><svg class="chart" id="${id}" role="img"></svg><figcaption>${esc(caption)}</figcaption></figure>`;}
function svg(id,w,h,body,title){const e=$(id);e.setAttribute('viewBox',`0 0 ${w} ${h+8}`);e.setAttribute('aria-label',title);e.innerHTML=`<title>${esc(title)}</title><desc>准确值和配对判断见相邻数据表。</desc>${body}`;}
function groupBars(id,groups,title,unit,dec){const e=$(id),w=Math.max(250,e.parentElement.clientWidth),l=64,r=68,t=24,step=91,h=t+groups.length*step+36,max=Math.max(unit==='%'?100:1,...groups.flatMap(g=>g.values.map(v=>v.value)))*(unit==='%'?1:1.02),x=v=>l+v/max*(w-l-r);let body='';for(let i=0;i<4;i++){const v=max*i/3;body+=`<line x1="${x(v)}" x2="${x(v)}" y1="${t}" y2="${h-36}" stroke="var(--border)"/><text x="${x(v)}" y="${h-17}" text-anchor="middle">${fmt(v,0)}</text>`;}
 groups.forEach((g,j)=>{const y=t+j*step;body+=`<text x="0" y="${y+4}">${esc(g.label)}</text><text x="0" y="${y+20}" style="fill:var(--muted)">${esc(g.sub||'')}</text>`;g.values.forEach((v,i)=>{const yy=y+30+i*23;body+=`<text x="${l-7}" y="${yy+12}" text-anchor="end">${esc(v.label)}</text><rect x="${l}" y="${yy}" width="${Math.max(0,x(v.value)-l)}" height="14" fill="var(${v.series==='candidate'?'--candidate':'--base'})"><title>${esc(g.label+' '+v.label)} ${fmt(v.value,dec)} ${esc(unit)}</title></rect><text x="${x(v.value)+5}" y="${yy+12}">${fmt(v.value,dec)}</text>`;});});body+=`<text class="axis-title" data-axis="x" x="${w-r}" y="${h-1}" text-anchor="end">${esc(unit)}</text><text class="axis-title" data-axis="y" x="0" y="14">策略 / 版本</text>`;svg(id,w,h,body,title);}
function deltaPlot(id,rows,title,unit,dec){const e=$(id),w=Math.max(250,e.parentElement.clientWidth),l=83,r=55,t=25,step=69,h=t+rows.length*step+37,extent=Math.max(unit==='项'?1:.01,...rows.map(v=>Math.abs(v.delta)))*1.23,x=v=>l+(v+extent)/(2*extent)*(w-l-r);let body='';for(const v of [-extent,0,extent])body+=`<line x1="${x(v)}" x2="${x(v)}" y1="${t-4}" y2="${h-36}" stroke="var(${v===0?'--fg':'--border'})"/><text x="${x(v)}" y="${h-18}" text-anchor="middle">${signed(v,unit==='项'?1:2)}</text>`;rows.forEach((v,i)=>{const y=t+i*step+15,xx=x(v.delta);body+=`<text x="0" y="${y+4}">${esc(v.label)}</text><line x1="${x(0)}" x2="${xx}" y1="${y}" y2="${y}" stroke="var(--base)"/><circle cx="${x(0)}" cy="${y}" r="3" fill="var(--base)"/><circle cx="${xx}" cy="${y}" r="5" fill="var(--candidate)"><title>${esc(v.raw)}，差值 ${signed(v.delta,dec)} ${esc(unit)}</title></circle><text x="${xx}" y="${y-11}" text-anchor="middle">${signed(v.delta,dec)}</text><text x="0" y="${y+27}" style="fill:var(--muted)">${esc(v.raw)}</text>`;});body+=`<text class="axis-title" data-axis="x" x="${w-r}" y="${h-1}" text-anchor="end">差值 ${esc(unit)}</text><text class="axis-title" data-axis="y" x="0" y="13">种子</text>`;svg(id,w,h,body,title);}
function add(id,type,rows,title,unit,dec){plots.push(()=>type==='bars'?groupBars(id,rows,title,unit,dec):deltaPlot(id,rows,title,unit,dec));}
function arrow(b,c,m){const x=b[m[0]]*m[4],y=c[m[0]]*m[4],dec=m[0]==='late_count'?0:m[3];return `${fmt(x,dec)} → ${fmt(y,dec)} (${signed(y-x,dec)})`;}
function stats(value){return value&&Object.keys(value).length?esc(JSON.stringify(value)):'—';}
function drawOverview(stage,spec){const groups=spec.cases.map(c=>({c,ps:pairs(stage,c,spec)})).filter(g=>g.ps.length);$('overview-title').textContent=spec.view==='budget'?'中继预算组合 · 相同总预算':'四策略 · 相同总预算';$('overview-note').textContent=spec.view==='budget'?'比较末端版与增加 95% 预热、播种 checkpoint 的组合；不能将差异仅归因于预热比例。':'每个条形是当前阶段三个种子的均值；四场景逐一对比。选中方案复用已测 terminal / rebalanced，不增加独立样本。';$('overview-note').textContent+=' '+floatSummary(groups.flatMap(g=>g.ps));$('overview').innerHTML=metrics.map((m,i)=>fig(`overview-${i}`,m[1],m[0]==='total_lateness_min'?'项目次级目标，不是题目单列优先级。':'从 0 起画；准确数值直接标注。')).join('');metrics.forEach((m,i)=>add(`overview-${i}`,'bars',groups.map(g=>({label:caseNames[g.c],sub:`n=${g.ps.length} 对 · 两版各 ${g.ps[0][0].budget_seconds}s 总预算`,values:[{label:names[g.ps[0][0].variant],value:avg(g.ps.map(p=>p[0][m[0]]))*m[4],series:'baseline'},{label:names[g.ps[0][1].variant],value:avg(g.ps.map(p=>p[1][m[0]]))*m[4],series:'candidate'}]})),m[1],m[2],m[3]));}
function drawPairs(stage,spec){const selected=$('case').value,ps=pairs(stage,selected,spec),late=[0,0,0];ps.forEach(([b,c])=>late[Math.sign(c.late_count-b.late_count)+1]++);$('pair-outcome').textContent=`${caseNames[selected]}：项目三级得分 ${wtl(ps)}；逾期任务单项 ${late[0]} 优 / ${late[1]} 平 / ${late[2]} 差。${floatSummary(ps)}`;$('paired').innerHTML=metrics.map((m,i)=>fig(`pair-${i}`,m[1],m[0]==='on_time_rate'?'正值表示按时率提高；差值单位为百分点。':'每行对应一个种子，标明两版原始值；负值表示后者数值更低。')).join('');metrics.forEach((m,i)=>add(`pair-${i}`,'delta',ps.map(([b,c])=>({label:String(b.seed),delta:(c[m[0]]-b[m[0]])*m[4],raw:`${fmt(b[m[0]]*m[4],m[0]==='late_count'?0:m[3])} → ${fmt(c[m[0]]*m[4],m[0]==='late_count'?0:m[3])} ${m[2]}`})),m[1],m[0]==='on_time_rate'?'百分点':m[2],m[0]==='late_count'?0:m[3]));const all=spec.cases.flatMap(c=>pairs(stage,c,spec));$('paired-table').innerHTML=`<table><thead><tr><th>场景 / 种子</th><th>前者 → 后者</th><th>逾期任务</th><th>按时率 %</th><th>航程 km</th><th>耗时 s</th><th>逾期 min · 次级</th><th>完整三级判断</th><th>完整解</th></tr></thead><tbody>${all.map(([b,c])=>{const verdict=cmp(c,b),tiny=floatOnly(b,c);return `<tr><td>${esc(caseNames[b.case_id])}<br>${b.seed}</td><td>${names[b.variant]} → ${names[c.variant]}</td>${metrics.map(m=>`<td class="num">${arrow(b,c,m)}</td>`).join('')}<td class="${tiny?'':verdict<0?'good':verdict>0?'bad':''}">${verdict<0?'后者胜':verdict>0?'后者退化':'相同'}${tiny?`<br>仅浮点尾差（非实质收益）<br><span class="note">Δ ${(c.total_lateness_min-b.total_lateness_min).toExponential(3)} min / ${(c.distance_km-b.distance_km).toExponential(3)} km</span>`:''}</td><td><a href="${esc(b.solution_file)}">前者</a> / <a href="${esc(c.solution_file)}">后者</a></td></tr>`;}).join('')}</tbody></table>`;}
function drawRelay(stage,spec){const cs=spec.cases.filter(c=>D.manifest.protocol.relay_cases.includes(c)),ps=cs.flatMap(c=>pairs(stage,c,spec)),ms=[['relay_task_count','中继任务数','项',0],['cross_uav_handoff_count','真实跨机交接数','项',0],['total_relay_waiting_time','中继等待总量','min',3]],has=(r,k)=>Number.isFinite(r.relay_statistics?.[k]);$('relay-plots').innerHTML=ms.map((m,i)=>fig(`relay-${i}`,m[1],'后者减前者；候选均由完整全局时序评价，不以数量越多越好。')).join('');ms.forEach((m,i)=>add(`relay-${i}`,'delta',ps.filter(p=>p.every(r=>has(r,m[0]))).map(([b,c])=>({label:String(b.seed).slice(-4)+(b.case_id.endsWith('stations')?' 预部署':' 原点'),delta:c.relay_statistics[m[0]]-b.relay_statistics[m[0]],raw:`${fmt(b.relay_statistics[m[0]],m[3])} → ${fmt(c.relay_statistics[m[0]],m[3])} ${m[2]}`})),m[1],m[2],m[3]));const number=(r,k,d=0)=>has(r,k)?fmt(r.relay_statistics[k],d):'—';$('relay-table').innerHTML=`<table><thead><tr><th>场景 / 种子</th><th>版本</th><th>中继任务</th><th>跨机</th><th>同机</th><th>总等待 min</th><th>平均等待 min</th><th>DIRECT预热 → 最终</th></tr></thead><tbody>${ps.flat().map(r=>`<tr><td>${caseNames[r.case_id]}<br>${r.seed}</td><td>${names[r.variant]}</td><td>${number(r,'relay_task_count')}</td><td>${number(r,'cross_uav_handoff_count')}</td><td>${number(r,'same_uav_relay_count')}</td><td>${number(r,'total_relay_waiting_time',3)}</td><td>${number(r,'average_relay_waiting_time',3)}</td><td>${r.relay_warmup_score?`逾期 ${r.relay_warmup_score[0]} → ${r.late_count}；min ${fmt(r.relay_warmup_score[1])} → ${fmt(r.total_lateness_min)}；km ${fmt(r.relay_warmup_score[2])} → ${fmt(r.distance_km)}`:'—'}</td></tr>`).join('')}</tbody></table>`;}
function budgetStack(id,groups){const e=$(id),w=Math.max(250,e.parentElement.clientWidth),l=64,r=30,t=28,step=w<400?110:90,h=t+groups.length*step+37,max=Math.max(1,...groups.map(g=>g.total))*1.03,x=v=>l+v/max*(w-l-r);let body='';for(let i=0;i<4;i++){const v=max*i/3;body+=`<line x1="${x(v)}" x2="${x(v)}" y1="${t}" y2="${h-36}" stroke="var(--border)"/><text x="${x(v)}" y="${h-17}" text-anchor="middle">${fmt(v,1)}</text>`;}groups.forEach((g,i)=>{const y=t+i*step;body+=`<text x="0" y="${y}">${esc(g.label)}</text><text x="0" y="${y+18}">${esc(g.name)} · ${g.n} 次均值</text>`;let start=0;g.parts.forEach((part,j)=>{body+=`<rect x="${x(start)}" y="${y+28}" width="${x(start+part)-x(start)}" height="18" fill="var(${['--base','--candidate','--phase-other'][j]})"><title>${['DIRECT预热','中继阶段','其他端到端开销'][j]} ${fmt(part,3)} s</title></rect>`;start+=part;});body+=w<400?`<text x="0" y="${y+64}">预热 ${fmt(g.parts[0],2)} · 中继 ${fmt(g.parts[1],2)} s</text><text x="0" y="${y+81}">其他 ${fmt(g.parts[2],2)} s</text>`:`<text x="0" y="${y+64}">预热 ${fmt(g.parts[0],2)} · 中继 ${fmt(g.parts[1],2)} · 其他 ${fmt(g.parts[2],2)} s</text>`;});body+=`<text class="axis-title" data-axis="x" x="${w-r}" y="${h-1}" text-anchor="end">实际秒数 s</text><text class="axis-title" data-axis="y" x="0" y="13">场景 / 版本</text>`;svg(id,w,h,body,'实际阶段时间分配');}
function drawPhases(stage,spec){const stageRuns=D.runs.filter(r=>r.stage===stage),phaseLines=[];for(const v of ['terminal','rebalanced']){const ps=stageRuns.filter(r=>r.variant===v&&r.terminal_polish?.active).map(r=>r.terminal_polish);if(ps.length)phaseLines.push(`${names[v]}：${ps.length} 条启用记录，${ps.filter(p=>p.accepted).length} 次接受，${ps.filter(p=>p.after_score[0]<p.before_score[0]).length} 次减少逾期，记录耗时 ${fmt(Math.min(...ps.map(p=>p.elapsed_seconds))*1000,3)}–${fmt(Math.max(...ps.map(p=>p.elapsed_seconds))*1000,3)} ms`);}const stageRelay=stageRuns.filter(r=>Array.isArray(r.relay_warmup_score));const relayLines=[...new Set(stageRelay.map(r=>r.variant))].map(v=>{const rs=stageRelay.filter(r=>r.variant===v);return `${names[v]} ${rs.filter(r=>r.late_count<r.relay_warmup_score[0]).length}/${rs.length}`;});$('phase-aggregate').textContent=`本阶段全部独立运行统计，不随对比视角重复计数。${phaseLines.join('；')}。中继阶段减少逾期的运行数：${relayLines.join('；')}。这些是阶段诊断，不能单独作因果归因。`;const all=spec.cases.flatMap(c=>pairs(stage,c,spec)).flat(),relay=all.filter(r=>Number.isFinite(r.phase_statistics?.relay_warmup_runtime_seconds)&&Number.isFinite(r.phase_statistics?.relay_search_runtime_seconds)),groups=[];for(const c of D.manifest.protocol.relay_cases){for(const v of [...new Set(relay.filter(r=>r.case_id===c).map(r=>r.variant))]){const rs=relay.filter(r=>r.case_id===c&&r.variant===v),total=avg(rs.map(r=>r.end_to_end_seconds)),warm=avg(rs.map(r=>r.phase_statistics.relay_warmup_runtime_seconds)),search=avg(rs.map(r=>r.phase_statistics.relay_search_runtime_seconds));groups.push({label:caseNames[c],name:names[v],n:rs.length,total,parts:[warm,search,Math.max(0,total-warm-search)]});}}
$('phase-plots').innerHTML=fig('phase-budget','实际阶段时间分配','从左到右为 DIRECT、中继、其他，合计端到端实测均值。其他为扣除两个记录阶段的余量；末端耗时已含 DIRECT，不再相加。')+fig('phase-gain','中继前后逾期任务变化','每行是该运行最终值减 DIRECT 预热值，属于阶段诊断，不是同预算版本净差。');plots.push(()=>budgetStack('phase-budget',groups));add('phase-gain','delta',relay.map(r=>({label:String(r.seed).slice(-4)+' '+names[r.variant],delta:r.late_count-r.relay_warmup_score[0],raw:`${r.case_id.endsWith('stations')?'预部署':'原点'}：${r.relay_warmup_score[0]} → ${r.late_count} 项`})), '中继阶段逾期任务变化','项',0);
const n=(x,d=3)=>Number.isFinite(x)?fmt(x,d):'—';$('phase-table').innerHTML=`<table><thead><tr><th>场景 / 种子 / 版本</th><th>预热目标比例</th><th>DIRECT s</th><th>中继 s</th><th>预热 → 最终逾期</th><th>播种扫描 / 改善</th><th>截止 / checkpoint</th></tr></thead><tbody>${relay.map(r=>{const p=r.phase_statistics;return `<tr><td>${caseNames[r.case_id]} / ${r.seed} / ${names[r.variant]}</td><td>${n(p.relay_direct_warmup_fraction,2)}</td><td>${n(p.relay_warmup_runtime_seconds)}</td><td>${n(p.relay_search_runtime_seconds)}</td><td>${r.relay_warmup_score[0]} → ${r.late_count}</td><td>${n(p.relay_seed_tasks_considered,0)} / ${n(p.relay_seed_improvements,0)}</td><td>${p.relay_seed_deadline_reached??'—'} / ${p.relay_seed_checkpoint_retained??'—'}</td></tr>`;}).join('')}</tbody></table>`;
const rs=pairs(stage,$('case').value,spec).flat().filter(r=>r.terminal_polish?.active),scoreText=a=>Array.isArray(a)?`${a[0]} / ${fmt(a[1],3)} / ${fmt(a[2],3)}`:'—';$('polish-plots').innerHTML=fig('polish-time','末端实际耗时 · 当前详细场景','候选真实调用记录，毫秒；基线没有该调用。预留量上限和工作计数见下表。')+fig('polish-gain','末端前后逾期任务变化 · 当前详细场景','严格接受局部改进不保证最终比独立 main 更好；接力场景此处仅为 DIRECT 预热末端。');add('polish-time','bars',rs.map(r=>({label:`${r.seed} · ${names[r.variant]}`,sub:caseNames[r.case_id],values:[{label:'实际',value:r.terminal_polish.elapsed_seconds*1000,series:'candidate'}]})),'末端实际耗时','ms',3);add('polish-gain','delta',rs.map(r=>({label:String(r.seed).slice(-4)+' '+names[r.variant],delta:r.terminal_polish.after_score[0]-r.terminal_polish.before_score[0],raw:`${r.terminal_polish.before_score[0]} → ${r.terminal_polish.after_score[0]} 项；${r.terminal_polish.accepted?'已接受':'未接受'}`})),'末端逾期任务变化','项',0);
$('polish-table').innerHTML=`<table><thead><tr><th>场景 / 种子 / 版本</th><th>阶段</th><th>预留 / 实际 ms</th><th>接受</th><th>前得分（项 / min / km）</th><th>后得分（项 / min / km）</th><th>检查工作量</th></tr></thead><tbody>${all.filter(r=>Object.keys(r.terminal_polish||{}).length).map(r=>{const p=r.terminal_polish;return `<tr><td>${caseNames[r.case_id]} / ${r.seed} / ${names[r.variant]}</td><td>${esc(p.phase)}${p.active?'':'（未启用）'}</td><td>${n(p.reserved_seconds*1000)} / ${n(p.elapsed_seconds*1000)}</td><td>${p.accepted?'是':'否'}</td><td>${scoreText(p.before_score)}</td><td>${scoreText(p.after_score)}</td><td>${stats(p.statistics)}</td></tr>`;}).join('')}</tbody></table>`;
}
function drawMechanism(stage){const m=D.mechanism,total=m.terminal_summary,stageSummary=m.terminal_by_stage[stage],rows=m.terminal_vs_baseline.filter(r=>r.stage===stage),short={fleet200:'原点',fleet200__direct_relay:'中继',fleet200__direct_stations:'预部',fleet200__direct_relay_stations:'预中'};$('mechanism-summary').textContent=`全部实际 terminal 配对 ${total.common_prefix_equal_pairs}/${total.pairs} 在共同保守迭代范围内拥有相同的记录的当前最好解得分前缀。${total.losses} 次终局失利中，${total.losses_with_fewer_main_iterations} 次主循环迭代更少，${total.losses_despite_terminal_improvement} 次虽获得自身末端改善仍输。当前 ${stageNames[stage]} 只显示 ${stageSummary.pairs} 对真实 terminal 对照；验证的中继场景仅运行 rebalanced，不推断缺失的 terminal 结果。`;
const charts=[['mechanism-iterations','DIRECT 主循环迭代差','轮',0,r=>r.main_search_iteration_delta,r=>`${r.reference_main_search_iterations} → ${r.candidate_main_search_iterations} 轮`],['mechanism-local','自身末端总逾期变化 · 次级','min',3,r=>r.candidate_terminal_delta[1],r=>`末端差 ${signed(r.candidate_terminal_delta[1],3)} min；${r.candidate_terminal_accepted?'接受':'未接受'}`],['mechanism-net','终局对 main 总逾期变化 · 次级','min',3,r=>r.net_pair_delta[1],r=>`终局差 ${signed(r.net_pair_delta[1],3)} min；严格${r.net_pair_outcome==='win'?'胜':r.net_pair_outcome==='loss'?'负':'平'}`]];
$('mechanism-plots').innerHTML=charts.map(c=>fig(c[0],c[1],'仅 terminal / main 的已测配对；后者减前者。局部变化和终局净差分别展示，不构成因果分解。')).join('');charts.forEach(c=>add(c[0],'delta',rows.map(r=>({label:String(r.seed).slice(-4)+' '+short[r.case_id],delta:c[4](r),raw:c[5](r)})),c[1],c[2],c[3]));const tinyNet=r=>r.net_pair_outcome!=='tie'&&r.net_pair_delta[0]===0&&Math.abs(r.net_pair_delta[1])<=1e-9&&Math.abs(r.net_pair_delta[2])<=1e-9;const deltaText=a=>`${signed(a[0],0)} 项 / ${signed(a[1],6)} min / ${signed(a[2],6)} km`;$('mechanism-table').innerHTML=`<table><thead><tr><th>场景 / 种子</th><th>共同前缀</th><th>保守迭代上限</th><th>主循环轮数 main → 末端</th><th>自身末端差</th><th>最终对 main 差</th><th>严格结果</th></tr></thead><tbody>${rows.map(r=>`<tr><td>${caseNames[r.case_id]} / ${r.seed}</td><td>${r.common_incumbent_prefix_equal?'一致':'不同'}</td><td>${r.common_completed_iteration_limit}</td><td>${r.reference_main_search_iterations} → ${r.candidate_main_search_iterations}</td><td>${deltaText(r.candidate_terminal_delta)}</td><td>${deltaText(r.net_pair_delta)}</td><td>${r.net_pair_outcome==='win'?'胜':r.net_pair_outcome==='loss'?'负':'平'}${tinyNet(r)?'<br>仅浮点尾差（非实质收益）':''}</td></tr>`).join('')}</tbody></table>`;
}

function drawDiagnosis(){const d=D.diagnosis,g=d.global_summary,b=d.downstream_summary,p=d.probe_summary;$('diagnosis-note').textContent=`旧 global 为 ${g.wins} 胜 / ${g.ties} 平 / ${g.losses} 负；DIRECT 吞吐比中位数 ${fmt(g.direct_throughput_ratio_median,4)}，不支持一概变慢的解释。${d.relay_count} 次中继阶段中，${d.relay_primary_gains} 次减少逾期任务；${d.warmup_mismatch}/6 组 downstream/global 的 DIRECT 预热得分已不同。downstream 严格得分实际为 ${b.wins} 胜 / ${b.ties} 平 / ${b.losses} 负，旧说明漏计完全相等的一对，原测量未改。额外末端探针在 ${p.completed} 个可用 DIRECT 终局中 ${p.improved} 个改善三级得分，但 ${p.late_count_improved} 个减少逾期任务；局部改善主要落在次级目标。`;
const completed=d.probe_results.filter(r=>r.status==='complete'),ms=[['late_count','探针逾期任务变化','项',0],['total_lateness_min','探针总逾期变化 · 项目次级','min',2],['distance_km','探针航程变化','km',3]];$('diagnosis-plots').innerHTML=ms.map((m,i)=>fig(`probe-${i}`,m[1],'额外诊断，不是同预算性能成绩；六个无法重建 DIRECT 前态的中继终局已跳过。')).join('');ms.forEach((m,i)=>add(`probe-${i}`,'delta',completed.map(r=>({label:String(r.seed).slice(-4)+(r.case_id.endsWith('stations')?' 预部署':' 原点'),delta:r.after[m[0]]-r.before[m[0]],raw:`${fmt(r.before[m[0]],m[3])} → ${fmt(r.after[m[0]],m[3])} ${m[2]}`})),m[1],m[2],m[3]));$('probe-table').innerHTML=`<table><thead><tr><th>历史场景 / 种子</th><th>状态</th><th>实际额外时间 s</th><th>得分改善</th><th>逾期数改善</th><th>限制</th></tr></thead><tbody>${d.probe_results.map(r=>`<tr><td>${caseNames[r.case_id]} / ${r.seed}</td><td>${r.status==='complete'?'完成':'跳过'}</td><td>${r.status==='complete'?fmt(r.elapsed_seconds,6):'—'}</td><td>${r.status==='complete'?(r.improved?'是':'否'):'—'}</td><td>${r.status==='complete'?(r.late_count_improved?'是':'否'):'—'}</td><td>${r.status==='skipped'?'未保存完整 DIRECT 预热路线':'追加诊断预算，不计入旧批测量'}</td></tr>`).join('')}</tbody></table>`;
}

function drawHistory(){const ms=[['mean_late_count','历史逾期任务均值','项',2],['mean_distance_km','历史航程均值','km',2],['mean_total_lateness_min','历史总逾期均值 · 项目次级','min',2],['budget_seconds','历史单次总预算（不相等）','s',0]];$('history').innerHTML=ms.map((m,i)=>fig(`hist-${i}`,m[1],'历史独立证据，不参与本轮配对结论。')).join('');ms.forEach((m,i)=>add(`hist-${i}`,'bars',D.history.map(r=>({label:histNames[r.scenario],sub:`n=${r.runs} · ${r.budget_seconds}s 总预算`,values:[{label:'历史',value:r[m[0]],series:'baseline'}]})),m[1],m[2],m[3]));$('history-table').innerHTML=`<table><thead><tr><th>历史策略</th><th>预算 s</th><th>逾期项</th><th>按时率</th><th>航程 km</th><th>逾期 min</th><th>实际 wall s</th><th>中继均值</th></tr></thead><tbody>${D.history.map(r=>`<tr><td>${histNames[r.scenario]}</td><td>${r.budget_seconds}</td><td>${fmt(r.mean_late_count)}</td><td>${fmt(r.on_time_rate*100)}%</td><td>${fmt(r.mean_distance_km)}</td><td>${fmt(r.mean_total_lateness_min)}</td><td>${fmt(r.mean_wall_seconds)}</td><td>${fmt(r.mean_relay_task_count)}</td></tr>`).join('')}</tbody></table>`;}
function render(){plots=[];const stage=$('stage').value,spec=compareSpec();$('comparison-note').textContent=spec.view==='budget'?'仅两个中继场景，6 个配对复用 terminal 运行；95% 组合包含预热比例与播种 checkpoint 两项变化，是整体运行比较。':spec.view==='terminal'?'四场景单独评价末端强化的同预算净收益；独立运行的墙钟截断可能改变终局，局部前后改善不代表净收益。':'按冻结门槛选择中继方案，无中继场景固定复用 terminal；这是既有运行的组合视角，不增加样本。初筛与新种子验证分开。';$('base-label').textContent=names[spec.a];$('candidate-label').textContent=spec.b==='selected'?'按协议选中方案':names[spec.b];drawOverview(stage,spec);drawPairs(stage,spec);drawRelay(stage,spec);drawPhases(stage,spec);drawMechanism(stage);drawDiagnosis();drawHistory();plots.forEach(f=>f());}
const formatOutcome=o=>o?`${o.wins} 胜 / ${o.ties} 平 / ${o.losses} 负，逾期总差 ${signed(o.late_delta,0)} 项`:'—';
$('decisions').innerHTML=D.decisions.map(d=>`<p><strong>${stageNames[d.stage]||esc(d.stage)}</strong>：${d.skipped?'未执行':d.passed?'通过':'未通过'}。${d.stage==='screen'?`末端对 main：${formatOutcome(d.terminal_vs_baseline)}；95%组合对末端：${formatOutcome(d.rebalanced_vs_terminal)}；选中方案对 main：${formatOutcome(d.selected_vs_baseline)}。中继选择 ${names[d.selected_relay_variant]}。`:(d.skipped?esc(d.reason||'未执行'): `选中方案对 main ${wtl(validationPairs)}，逾期任务总差 ${signed(lateDelta(validationPairs),0)} 项；按冻结的逐场景胜率和每对逾期任务数门槛判定。`)}</p>`).join('');
$('production-details').textContent=enabled?'默认采用方式：启用 DIRECT 末端优化和已核验播种 checkpoint，公共 DIRECT 预热比例改为 0.95；rebalance 强制覆盖开关保持关闭，CLI 显式比例仍受尊重。成绩来自测量时冻结快照；最终源码配置记录以 production.json 为准。':'未启用新默认配置；完整方案与审计采用记录见 production.json。';
const compat=D.runs.filter(r=>r.stage==='compatibility');$('compatibility').textContent=`25任务单机、固定100轮：${compat.length} 次运行；决策指纹${new Set(compat.map(r=>r.decision_fingerprint)).size===1?'一致':'不一致'}。这项兼容检查不计为200任务性能配对。`;$('protocol').textContent=JSON.stringify(D.manifest.protocol,null,2);
$('fingerprints').textContent=`基线 ${D.manifest.baseline_commit}；输入 ${D.manifest.input_sha256}；manifest ${D.fingerprints.manifest}；runs ${D.fingerprints.runs}；decisions ${D.fingerprints.decisions}；validation ${D.fingerprints.validation}；历史 summary ${D.history_fingerprints.summary}；历史 manifest ${D.history_fingerprints.manifest}；production ${D.fingerprints.production}；文献 notes ${D.literature_fingerprints['reading_notes.md']}；文献 provenance ${D.literature_fingerprints['provenance.json']}。`;
for(const [key,value] of Object.entries(D.context||{})){ $('context-section').hidden=false;const node=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent=key==='environment_notes'?'实验环境记录':key==='final_source_delta'?'测量快照与最终源码差异':'执行记录';pre.textContent=JSON.stringify(value,null,2);node.append(summary,pre);$('environment-disclosure').append(node);$('fingerprints').textContent+=` ${key} ${D.fingerprints[key]}。`;}
$('fingerprints').textContent+=` 诊断文件 ${JSON.stringify(D.diagnosis_fingerprints)}。`;
$('stage').addEventListener('change',()=>{setComparison();render();});$('comparison').addEventListener('change',()=>{setCases();render();});$('case').addEventListener('change',render);setComparison();render();let pending=false;new ResizeObserver(()=>{if(pending)return;pending=true;requestAnimationFrame(()=>{pending=false;plots.forEach(f=>f());});}).observe(root);
})();
</script></body></html>'''


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--historical-dir", type=Path, default=HISTORICAL)
    args = parser.parse_args(argv)
    directory = args.output_dir.resolve()
    data = build_data(directory, args.historical_dir.resolve())
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    target = directory / "index.html"
    target.write_text(TEMPLATE.replace("__DATA__", encoded), encoding="utf-8")
    print(f"Rendered {target} ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
