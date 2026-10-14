"""Offline paired report for complete-solver joint-relay replacement trials."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path

from algorithm.experiments import render_relay_trials_report as previous

ROOT = previous.ROOT
OUTPUT = ROOT / "algorithm/results/joint_relay_trials_20261014/batch1"
PRIOR = ROOT / "algorithm/results/relay_trials_20261013/batch1"


def build_data(directory: Path, prior_directory: Path) -> dict:
    manifest, runs, decisions, audit = (
        previous.load(directory / name) for name in
        ("manifest.json", "runs.json", "decisions.json", "validation.json")
    )
    if not audit.get("passed") or not audit.get("protocol_complete"):
        raise ValueError("Only complete, independently audited joint batches may be rendered")
    fingerprints = {}
    mechanism = []
    for name in ("manifest", "runs", "decisions"):
        actual = previous.sha256(directory / f"{name}.json")
        if audit.get(f"{name}_sha256") != actual:
            raise ValueError(f"Stale joint audit: {name}.json")
        fingerprints[name] = actual
    fingerprints["validation"] = previous.sha256(directory / "validation.json")
    if audit.get("run_count", len(runs)) != len(runs):
        raise ValueError("Joint audit count does not match")
    for row in runs:
        solution = (directory / row["solution_file"]).resolve()
        if not solution.is_relative_to(directory.resolve()) or previous.sha256(solution) != row["solution_sha256"]:
            raise ValueError("Joint solution fingerprint or path does not match")
        for field in ("late_count", "total_lateness_min", "distance_km", "end_to_end_seconds", "on_time_rate"):
            if not math.isfinite(float(row[field])):
                raise ValueError(f"Nonfinite metric: {field}")
        payload = previous.load(solution)
        metadata = payload.get("metadata", {})
        statistics = (row.get("neighborhood_statistics") or row.get("joint_statistics")
                      or metadata.get("joint_search") or metadata.get("joint_statistics")
                      or metadata.get("joint_search_statistics") or {})
        mechanism.append({key: row[key] for key in ("stage", "case_id", "variant", "seed")} | {
            "statistics": statistics,
            "profiling": row.get("profiling", {}),
            "metadata": {key: value for key, value in metadata.items()
                         if key.startswith("joint_") or key in ("neighborhood_statistics", "search_method", "operator_uses")},
        })
    production_file = directory / "production.json"
    production = previous.load(production_file) if production_file.exists() else None
    if production and production.get("validation_sha256") not in (None, fingerprints["validation"]):
        raise ValueError("Joint production decision refers to another audit")
    prior = previous.build_data(prior_directory, previous.HISTORICAL)
    # Reuse the verified old results only as a separately labelled historical view.
    prior_runs = [{key: row.get(key) for key in (
        "stage", "case_id", "variant", "seed", "valid", "budget_compliant",
        "budget_seconds", "late_count", "on_time_rate", "distance_km",
        "total_lateness_min", "end_to_end_seconds", "relay_statistics")}
        for row in prior["runs"] if row["stage"] == "medium"]
    note = ROOT / "papers/notes/joint_relay_20261014/provenance.json"
    return dict(manifest=manifest, runs=runs, decisions=decisions, audit=audit,
                fingerprints=fingerprints, production=production, mechanism=mechanism,
                prior_runs=prior_runs, prior_decisions=prior["decisions"],
                prior_fingerprints=prior["fingerprints"], prior_production=prior["production"],
                history=prior["history"], history_fingerprints=prior["history_fingerprints"],
                history_manifest=prior["history_manifest"],
                literature=previous.load(note) if note.exists() else {})


def build_extension(directory: Path, initial_data: dict) -> dict:
    """Keep the user-requested, post-hoc budget diagnostic in its own cohort."""
    manifest, runs, decisions, audit = (
        previous.load(directory / name) for name in
        ("manifest.json", "runs.json", "decisions.json", "validation.json")
    )
    if not audit.get("passed") or not audit.get("protocol_complete"):
        raise ValueError("Only independently audited, complete budget diagnostics may be rendered")
    if manifest.get("kind") != "joint_relay_extension" or not decisions or any(
        not decision.get("diagnostic_only") or decision.get("passed") is not False
        for decision in decisions
    ):
        raise ValueError("Extension must be explicitly diagnostic and never promote the default")
    initial_manifest = initial_data["manifest"]
    for name, fingerprint in initial_data["fingerprints"].items():
        if manifest.get("parent_bindings", {}).get(name) != fingerprint:
            raise ValueError("Budget diagnostic is not bound to this original audited batch")
    fingerprints = {}
    for name in ("manifest", "runs", "decisions"):
        actual = previous.sha256(directory / f"{name}.json")
        if audit.get(f"{name}_sha256") != actual:
            raise ValueError(f"Stale diagnostic audit: {name}.json")
        fingerprints[name] = actual
    fingerprints["validation"] = previous.sha256(directory / "validation.json")
    for key in ("input_sha256", "baseline_commit"):
        if manifest.get(key) != initial_manifest.get(key):
            raise ValueError(f"Diagnostic changes the original {key}")
    for variant, source in initial_manifest["source_snapshots"].items():
        if manifest.get("source_snapshots", {}).get(variant, {}).get("sha256") != source["sha256"]:
            raise ValueError("Budget diagnostic must use the identical frozen solver sources")
    if audit.get("run_count", len(runs)) != len(runs):
        raise ValueError("Budget diagnostic audit count mismatch")
    for row in runs:
        solution = (directory / row["solution_file"]).resolve()
        if not solution.is_relative_to(directory.resolve()) or previous.sha256(solution) != row["solution_sha256"]:
            raise ValueError("Budget diagnostic solution fingerprint mismatch")
    return dict(manifest=manifest, runs=runs, decisions=decisions, audit=audit,
                fingerprints=fingerprints, diagnostic_only=True)


STYLE = re.search(r"<style>([\s\S]*?)</style>", previous.TEMPLATE).group(1)
CHART_HELPERS = previous.TEMPLATE.split("function svgNode", 1)[1].split("function raw", 1)[0]
CHART_HELPERS = "function svgNode" + CHART_HELPERS

TEMPLATE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>联合接力求解器 · 同预算配对实验</title><style>__STYLE__</style></head><body><main id="joint-report">
<header><div class="eyebrow muted">2026-10-14 · 200 任务 / 8 架无人机 · 完整求解器替代尝试</div><h1>联合决定任务、接力与路线</h1><p class="lead">新的独立搜索同时探索任务分配、直送 / 中继、固定网络内的站点选择和访问顺序。与重新运行的旧求解器使用相同初始路线、种子和总预算；先小范围筛选，再按预定门槛扩大验证。</p></header>
<div class="outcome"><h3 id="headline"></h3><p id="conclusion"></p><p id="audit-state" class="detail muted"></p></div>
<div class="controls"><label>独立实验阶段<select id="stage"></select></label></div>
<div class="legend"><span><i class="swatch" style="background:var(--base)"></i>旧求解器 / 零差值</span><span><i class="swatch" style="background:var(--candidate)"></i>联合接力 VNS</span><span>点图：新 − 旧；每点标出旧值 → 新值</span></div>
<p id="stage-note" class="detail muted" aria-live="polite"></p><div id="paired-sections"></div><div id="extension-section"></div>
<section class="case"><h2>上次中继优化尝试</h2><p class="note">上一批仅修改旧求解器的跨机候选多样性，在两个场景均为 1 胜 2 负，保留原默认。下列为该批独立的 30 秒 / 3 种子记录，不与本批新结果混算改善率、胜率或平均值。</p><div class="grid" id="prior-plots"></div><div id="prior-table" class="table-wrap"></div><p class="detail"><a href="../../relay_trials_20261013/batch1/index.html">上次完整可视化</a> · <a href="../../relay_trials_20261013/batch1/validation.json">上次独立复算</a></p></section>
<details><summary>更早的正式四策略：240 / 480 / 480 / 720 秒</summary><p class="note">历史预算不相等，源码、种子和预热策略不同。保留它们说明既有预部署与中继结果，不把长预算成绩与本次短测直接排名。</p><div id="history-plots" class="grid"></div><div id="history-table" class="table-wrap"></div><p class="detail"><a href="../../final_scenario_comparison/summary.json">历史摘要</a> · <a href="../../final_scenario_comparison/manifest.json">历史预算与协议</a></p></details>
<section class="foot"><h2>题目指标与研究范围</h2><p>原题直接考核尽可能多订单按时、全部任务的总航程和求解时长。工程判优仍是逾期任务数 → 总逾期分钟 → 航程；总逾期是项目次级约定。项目配对获胜可能仅来自总逾期，官方指标需逐项判断。</p><p>载荷上限 2、每机 K=25、速度 0.9 km/min、所有任务完成、不计末单后返航。研究扩展沿用最终交付责任占 K、前半程转运不占的解释；站点允许存储，先卸货后取货。联合搜索选择现有固定站点，不连续优化坐标。</p><p>新求解器没有原 ALNS 的 DIRECT 预热阶段。报告用完整 incumbent 轨迹、真实交接和邻域统计观察搜索，不编造预热贡献。旧求解器仍使用自身原有流程，两者只按相同端到端总预算比较。</p><p>短预算和少量种子不构成最优性或稳定性证明。首批未进入原定扩大验证；用户追加的预算诊断单独展示，全部退化保留。计时从 CSV 读取前至完整核验与解写出后，不含 Python 启动和时钟起点前的模块导入；新分支约2毫秒导入保守计入端到端时间。</p>
<details><summary>完整求解器替代与阶段决策</summary><p>候选通过单独入口执行 transfer-aware VNS，保持独立路线状态，在任务模式、两机责任和访问次序的邻域上探索；没有调用旧 ALNS 作为预热或搜索内核。容量与 K 先剪枝，交接后的全局时序和完整成绩最终确认。该框架是项目实现，未复现某篇文献的全套算法。</p><div id="decisions"></div><div id="protocol"></div></details>
<details><summary>方法文献与复用证据</summary><p>Coltin &amp; Veloso (2014), <a href="https://doi.org/10.1609/aaai.v28i1.9034">Scheduling for Transfers in Pickup and Delivery Problems with Very Large Neighborhood Search</a>，此前已读官方全文页 2253–2254，支持包含转运的路线邻域与关联时序检查。其 VLNS-T 的随机移除 / 重插与退火并非本项目新 VNS 结构，不将本次实现写成论文复现。</p><p>Masson, Lehuédé &amp; Péton (2013), <a href="https://doi.org/10.1287/trsc.1120.0432">An Adaptive Large Neighborhood Search for the Pickup and Delivery Problem with Transfers</a>仅摘要级背景依据。AAAI 论文要求即时同步交接，当前模型允许站点存储，两者不同。</p><p>本批新增文献 API 请求为 0，复用上一批已核验记录及指纹。软件方法引用沿用已在线核实的 Kassis 等（2026）<a href="https://arxiv.org/abs/2609.00065">Scientific Agent Skills</a>，无版本 URL。</p><p><a href="../../../../papers/notes/joint_relay_20261014/reading_notes.md">方法适配笔记</a> · <a href="../../../../papers/notes/joint_relay_20261014/provenance.json">证据复用记录</a></p></details>
<details><summary>原始数据、独立复算与源码指纹</summary><p class="source" id="fingerprints"></p><p><a href="manifest.json">冻结协议</a> · <a href="runs.json">全部运行</a> · <a href="decisions.json">阶段决策</a> · <a href="validation.json">独立复算</a> · <a href="README.md">复现说明</a></p><p>绘图数据嵌入本报告，离线可查看；完整解与源码归档为相邻仓库文件。</p></details></section>
</main><script id="report-data" type="application/json">__DATA__</script><script>
(() => {'use strict';
const D=JSON.parse(document.getElementById('report-data').textContent),root=document.getElementById('joint-report'),$=id=>root.querySelector('#'+id),esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const names={baseline:'旧求解器',joint:'联合接力 VNS'},primary=D.manifest.protocol.primary_case||'fleet200__direct_relay_stations',secondary=D.manifest.protocol.secondary_case||'fleet200__direct_relay',candidate=Object.keys(D.manifest.source_snapshots).find(x=>x!=='baseline')||'joint';names[candidate]='联合接力 VNS';
const caseNames={[primary]:'主场景 · 预部署 + 中继',[secondary]:'次场景 · 原点起飞 + 中继'},stageNames={screen:'10 秒小范围筛选',medium:'30 秒扩大验证'},valid=r=>r.valid&&r.budget_compliant,fmt=(n,d=2)=>Number(n).toFixed(d),sign=(n,d=2)=>(n>1e-9?'+':'')+fmt(Math.abs(n)<1e-9?0:n,d),mean=vs=>vs.reduce((s,n)=>s+n,0)/vs.length;
const score=r=>[r.late_count,r.total_lateness_min,r.distance_km],cmp=(a,b)=>{const x=score(a),y=score(b);for(let i=0;i<3;i++)if(x[i]!==y[i])return x[i]<y[i]?-1:1;return 0;};
const metrics=[['late_count','逾期任务数 ↓','项',0,1],['on_time_rate','按时完成率 ↑','百分点',1,100],['distance_km','总航程 ↓','km',2,1],['end_to_end_seconds','端到端求解时间 ↓','s',3,1],['total_lateness_min','总逾期时间 ↓ · 项目次级','min',2,1]];
function pairs(stage,c){const rs=D.runs.filter(r=>r.stage===stage&&r.case_id===c&&valid(r));return rs.filter(r=>r.variant==='baseline').sort((a,b)=>a.seed-b.seed).map(b=>[b,rs.find(r=>r.variant===candidate&&r.seed===b.seed)]).filter(p=>p[1]);}
function wtl(ps){const n=[0,0,0];ps.forEach(([b,c])=>n[cmp(c,b)+1]++);return `${n[0]} 胜 / ${n[1]} 平 / ${n[2]} 负`;}
const stages=['screen','medium'].filter(s=>D.runs.some(r=>r.stage===s));$('stage').innerHTML=stages.map(s=>`<option value="${s}">${stageNames[s]}</option>`).join('');$('stage').value=stages.includes('medium')?'medium':'screen';
const last=D.decisions.find(x=>x.stage==='medium'),promoted=last?.passed===true;$('headline').textContent=promoted?'联合接力 VNS 达到本批采用门槛':'联合接力 VNS 未达到本批默认替换门槛';
const headlinePairs=pairs(stages.includes('medium')?'medium':'screen',primary),official=[0,0,0];headlinePairs.forEach(([b,c])=>official[Math.sign(c.late_count-b.late_count)+1]++);
if(headlinePairs.length){const dl=mean(headlinePairs.map(([b,c])=>c.late_count-b.late_count)),dr=mean(headlinePairs.map(([b,c])=>100*(c.on_time_rate-b.on_time_rate)));$('conclusion').textContent=`${stages.includes('medium')?'30 秒验证':'10 秒筛选'}主场景按项目三级目标 ${wtl(headlinePairs)}；官方逾期任务单项 ${official[0]} 优 / ${official[1]} 平 / ${official[2]} 差，平均逾期 ${sign(dl)} 项、按时率 ${sign(dr,3)} 个百分点。${D.production?.default_enabled?'已替换该入口的默认求解器。':promoted?'是否设为默认见本批采用记录。':'保留旧默认，新框架作为研究入口。'}${dl>0?'航程或总逾期均值改善不能抵消更多任务逾期。':''}`;}
$('audit-state').textContent=`原定批次 ${D.runs.length} 次完整运行，${D.runs.filter(valid).length} 次有效且预算合规；实际求解 ${fmt(D.runs.reduce((s,r)=>s+r.end_to_end_seconds,0)/60)} 分钟。${D.extension?'用户后续追加的30秒诊断独立列于下方，没有并入这些统计。':'所有正式数据已独立复算。'}`;
let plots=[];__CHART_HELPERS__
function raw(v,key,d){return key==='on_time_rate'?fmt(v*100,1)+'%':fmt(v,d);}
function table(rs){return `<table><thead><tr><th>种子</th><th>版本</th><th>逾期项</th><th>按时率</th><th>航程 km</th><th>耗时 s</th><th>逾期 min</th><th>项目配对</th><th>完整解</th></tr></thead><tbody>${rs.map(r=>`<tr><td>${r.seed}</td><td>${names[r.variant]}</td><td class="num">${r.late_count}</td><td class="num">${fmt(r.on_time_rate*100,1)}%</td><td class="num">${fmt(r.distance_km)}</td><td class="num">${fmt(r.end_to_end_seconds,3)}</td><td class="num">${fmt(r.total_lateness_min)}</td><td>${esc(r.verdict||'—')}</td><td><a href="${esc(r.solution_file)}">查看</a></td></tr>`).join('')}</tbody></table>`;}
function trajectory(id,pair){const e=$(id),w=Math.max(280,e.parentElement.clientWidth),l=55,r=24,t=25,b=42,h=260,points=pair.flatMap(row=>row.best_trajectory||[]);if(!points.length){svgNode(id,w,65,'<text x="8" y="30">未保存完整轨迹</text>','缺少轨迹');return;}const xMax=Math.max(1,...pair.map(r=>r.end_to_end_seconds)),lo=Math.max(0,Math.min(...points.map(p=>p[2]))-1),hi=Math.max(...points.map(p=>p[2]))+1,x=n=>l+n/xMax*(w-l-r),y=n=>h-b-(n-lo)/(hi-lo)*(h-t-b);let out=`<rect x="${l}" y="${t}" width="${w-l-r}" height="${h-t-b}" fill="none" stroke="var(--border)"/>`;
 for(let i=0;i<4;i++){const v=lo+(hi-lo)*i/3,xx=x(xMax*i/3);out+=`<line x1="${l}" x2="${w-r}" y1="${y(v)}" y2="${y(v)}" stroke="var(--border)" stroke-width=".7"/><text x="${l-7}" y="${y(v)+4}" text-anchor="end">${fmt(v,0)}</text><text x="${xx}" y="${h-b+20}" text-anchor="middle">${fmt(xMax*i/3,0)}</text>`;}
 pair.forEach((row,j)=>{const ps=row.best_trajectory||[];if(!ps.length)return;let path=`M ${x(ps[0][1])} ${y(ps[0][2])}`;ps.slice(1).forEach(p=>path+=` H ${x(Math.min(xMax,p[1]))} V ${y(p[2])}`);path+=` H ${x(row.end_to_end_seconds)}`;out+=`<path d="${path}" fill="none" stroke="var(${j?'--candidate':'--base'})" stroke-width="1.8" ${j?'':'stroke-dasharray="5 3"'}><title>${row.seed} ${names[row.variant]}</title></path>`;});out+=`<text class="axis-title" data-axis="x" x="${w-r}" y="${h-3}" text-anchor="end">端到端秒</text><text class="axis-title" data-axis="y" x="3" y="15">逾期项</text>`;svgNode(id,w,h,out,`种子 ${pair[0].seed} 的完整最优轨迹`);}
function statsFor(row){const record=D.mechanism.find(m=>m.stage===row.stage&&m.case_id===row.case_id&&m.variant===row.variant&&m.seed===row.seed);return record?.statistics||{};}
function neighborhoodEntries(row){const s=statsFor(row),m=D.mechanism.find(m=>m.stage===row.stage&&m.case_id===row.case_id&&m.variant===row.variant&&m.seed===row.seed)?.metadata||{},uses=m.operator_uses||{};return [['joint_pair','成对联动','joint_pair_probes'],['single_plan','单任务模式','single_plan_probes'],['adjacent_order','相邻访问','adjacent_probes']].filter(([key,,probe])=>key in uses||probe in s).map(([key,name,probe])=>({name,attempts:uses[key]??0,probes:s[probe]??0}));}
function draw(){const stage=$('stage').value;plots=[];$('stage-note').textContent=stage==='screen'?'筛选种子与扩大验证种子分别固定；当前只展示10秒配对。':'仅展示30秒的新验证种子；不与筛选样本合并。';
 $('paired-sections').innerHTML=[primary,secondary].map((c,k)=>{const ps=pairs(stage,c);if(!ps.length)return `<section class="case"><h2>${caseNames[c]}</h2><p class="empty">该阶段未运行完整有效配对，不补造成绩。</p></section>`;const ns=ps.flatMap(p=>p).filter(r=>r.variant===candidate).flatMap(row=>neighborhoodEntries(row).map(s=>({seed:row.seed,...s})));return `<section class="case"><div class="pair-title"><h2>${caseNames[c]}</h2><span class="pair-outcome">${wtl(ps)}</span><span class="badge">相同 ${ps[0][0].budget_seconds} 秒预算 / ${ps.length} 配对</span></div><div class="grid">${metrics.map((m,i)=>figure(`core-${k}-${i}`,m[1],i===1?'正值表示按时率更高；其他指标负值更低。':i===4?'项目次级目标，原题未单列其优先级。':'逐种子旧值 → 新值；所有退化保留。')).join('')}</div><details><summary>逐种子完整成绩</summary><div class="table-wrap">${table(ps.flatMap(([b,c])=>[b,{...c,verdict:cmp(c,b)<0?'新胜':cmp(c,b)>0?'新负':'完全平'}]))}</div></details><details><summary>完整搜索轨迹、真实交接与邻域诊断</summary><p class="detail muted">旧版虚线、新版实线；每个种子分别绘制。新求解器只有一个联合搜索过程，没有DIRECT预热阶段。旧ALNS迭代与新VNS邻域调用含义不同，搜索轮数不用于判定谁更快。</p><div class="grid">${ps.map((p,i)=>figure(`trajectory-${k}-${i}`,`种子 ${p[0].seed}`,'横轴为端到端秒，纵轴为已保存最优解的逾期任务。')).join('')}</div><div class="grid">${figure(`relay-${k}-0`,'跨机接力数量变化','实际由两条运输腿所属无人机核实。')}${figure(`relay-${k}-1`,'中继总等待变化','全局传播后的交接等待，不以中继数量代替质量。')}${figure(`rate-${k}`,'候选完整核验 / s','仅新求解器完整核验调用 / 端到端秒，含拒绝候选；不能与旧ALNS迭代率比较。')}</div><div class="table-wrap"><table><thead><tr><th>种子</th><th>版本</th><th>中继任务</th><th>跨机</th><th>同机</th><th>等待 min</th><th>搜索轮数</th></tr></thead><tbody>${ps.flatMap(p=>p).map(r=>{const s=r.relay_statistics||{};return `<tr><td>${r.seed}</td><td>${names[r.variant]}</td><td class="num">${s.relay_task_count??'—'}</td><td class="num">${s.cross_uav_handoff_count??'—'}</td><td class="num">${s.same_uav_relay_count??'—'}</td><td class="num">${fmt(s.total_relay_waiting_time||0,3)}</td><td class="num">${r.iterations}</td></tr>`;}).join('')}</tbody></table></div>${ns.length?`<h3>新求解器的邻域统计</h3><div class="grid">${figure(`neighborhood-${k}`,'各邻域调用次数','各邻域跨该阶段种子求和，只作搜索开销诊断。')}</div><div class="table-wrap"><table><thead><tr><th>种子</th><th>邻域</th><th>邻域调用</th><th>局部探测</th></tr></thead><tbody>${ns.map(n=>`<tr><td>${n.seed}</td><td>${esc(n.name)}</td><td class="num">${n.attempts}</td><td class="num">${n.probes}</td></tr>`).join('')}</tbody></table></div>`:'<p class="detail muted">没有可用邻域计数记录，保留空缺。</p>'}</details></section>`;}).join('');
 [primary,secondary].forEach((c,k)=>{const ps=pairs(stage,c);if(!ps.length)return;metrics.forEach((m,i)=>graph(`core-${k}-${i}`,'delta',ps.map(([b,c])=>({label:String(b.seed).slice(-4),delta:(c[m[0]]-b[m[0]])*m[4],raw:`${b.seed} · ${raw(b[m[0]],m[0],m[3])} → ${raw(c[m[0]],m[0],m[3])}`})),m[1],m[2],m[3]));ps.forEach((p,i)=>plots.push(()=>trajectory(`trajectory-${k}-${i}`,p)));
 [['cross_uav_handoff_count','项',0],['total_relay_waiting_time','min',3]].forEach((m,i)=>graph(`relay-${k}-${i}`,'delta',ps.map(([b,c])=>({label:String(b.seed).slice(-4),delta:(c.relay_statistics?.[m[0]]||0)-(b.relay_statistics?.[m[0]]||0),raw:`${b.seed} · ${fmt(b.relay_statistics?.[m[0]]||0,m[2])} → ${fmt(c.relay_statistics?.[m[0]]||0,m[2])}`})),m[0],m[1],m[2]));
 const rs=ps.map(p=>p[1]),rates=rs.map(r=>{const s=statsFor(r),entries=neighborhoodEntries(r),count=s.full_evaluations??0;return {label:String(r.seed),value:count/r.end_to_end_seconds,series:'candidate',sub:`${count} 次完整评价`};});graph(`rate-${k}`,'bars',rates,'候选完整核验速率','次 / s',2);const ns=rs.flatMap(neighborhoodEntries);if(ns.length){const nsNames=[...new Set(ns.map(n=>n.name))];graph(`neighborhood-${k}`,'bars',nsNames.map(name=>({label:name,value:ns.filter(n=>n.name===name).reduce((n,e)=>n+e.attempts,0),series:'candidate'})),'邻域调用次数','次',0);}});drawExtension();drawPrior();drawHistory();plots.forEach(f=>f());}
function drawExtension(){const E=D.extension;if(!E){$('extension-section').innerHTML='';return;}const rs=E.runs.filter(valid).filter(r=>r.case_id===primary),ps=rs.filter(r=>r.variant==='baseline').sort((a,b)=>a.seed-b.seed).map(b=>[b,rs.find(r=>r.variant===candidate&&r.seed===b.seed)]).filter(p=>p[1]);
 $('extension-section').innerHTML=`<section class="case"><h2>用户追加：30 秒预算诊断 · ${ps.length} 个新种子</h2><p class="note">在原定10秒筛选失败之后，由用户追加；仅主预部署场景，不是预先计划的3种子扩大验证。源码和初始路线保持，所有${E.runs.length}次追加运行独立审计通过。它不与原批混算均值、胜负，也不用于更改默认。</p><div class="pair-title"><span class="pair-outcome">本追加批按项目目标 ${wtl(ps)}</span><span class="badge">30秒相同总预算 / ${ps.length}配对</span></div><div class="grid">${metrics.map((m,i)=>figure(`ext-core-${i}`,m[1],'仅本追加诊断：旧值 → 新值。')).join('')}</div><details><summary>追加诊断的完整成绩与实际交接</summary><div class="table-wrap">${table(ps.flatMap(([b,c])=>[b,{...c,solution_file:E.relative_path+'/'+c.solution_file,verdict:cmp(c,b)<0?'新胜':cmp(c,b)>0?'新负':'完全平'}]).map(r=>r.variant==='baseline'?{...r,solution_file:E.relative_path+'/'+r.solution_file}:r))}</div><div class="grid">${ps.map((p,i)=>figure(`ext-trajectory-${i}`,`追加种子 ${p[0].seed}`,'虚线旧版、实线新版；完整最优轨迹。')).join('')}${figure('ext-relay-0','追加批跨机接力数变化','真实跨机运输腿，不与原批合并。')}${figure('ext-relay-1','追加批中继等待变化','全局交接时序中的总等待。')}</div></details><p class="detail">本追加批实际求解 ${fmt(E.runs.reduce((s,r)=>s+r.end_to_end_seconds,0)/60)} 分钟。<a href="${esc(E.relative_path)}/validation.json">追加独立复算</a> · <a href="${esc(E.relative_path)}/manifest.json">用户追加协议</a> · <a href="index.initial.html">原批最初报告</a></p></section>`;
 metrics.forEach((m,i)=>graph(`ext-core-${i}`,'delta',ps.map(([b,c])=>({label:String(b.seed).slice(-4),delta:(c[m[0]]-b[m[0]])*m[4],raw:`${b.seed} · ${raw(b[m[0]],m[0],m[3])} → ${raw(c[m[0]],m[0],m[3])}`})),m[1],m[2],m[3]));ps.forEach((p,i)=>plots.push(()=>trajectory(`ext-trajectory-${i}`,p)));[['cross_uav_handoff_count','项',0],['total_relay_waiting_time','min',3]].forEach((m,i)=>graph(`ext-relay-${i}`,'delta',ps.map(([b,c])=>({label:String(b.seed).slice(-4),delta:(c.relay_statistics?.[m[0]]||0)-(b.relay_statistics?.[m[0]]||0),raw:`${b.seed} · ${fmt(b.relay_statistics?.[m[0]]||0,m[2])} → ${fmt(c.relay_statistics?.[m[0]]||0,m[2])}`})),m[0],m[1],m[2]));}
function drawPrior(){const rows=D.prior_runs.filter(valid),labels={};for(const c of [primary,secondary])for(const v of ['baseline','diversity'])labels[c+'|'+v]=(c===primary?'预部署':'原点')+' · '+(v==='baseline'?'前批基线':'前批候选');const groups=Object.entries(labels).map(([key,label])=>{const [c,v]=key.split('|');return {label,rows:rows.filter(r=>r.case_id===c&&r.variant===v)};}).filter(g=>g.rows.length);
 $('prior-plots').innerHTML=metrics.map((m,i)=>figure(`prior-${i}`,`前批 ${m[1]}`,'独立30秒验证均值；条形从0开始。')).join('');metrics.forEach((m,i)=>graph(`prior-${i}`,'bars',groups.map(g=>({label:g.label,sub:`n=${g.rows.length} · 30s`,value:mean(g.rows.map(r=>r[m[0]]))*(m[0]==='on_time_rate'?100:1),series:g.rows[0].variant==='baseline'?'baseline':'candidate'})),m[1],m[0]==='on_time_rate'?'%':m[2],m[3]));$('prior-table').innerHTML=`<table><thead><tr><th>前批策略</th><th>n</th><th>逾期均值</th><th>按时率</th><th>航程 km</th><th>耗时 s</th><th>逾期 min</th></tr></thead><tbody>${groups.map(g=>`<tr><td>${esc(g.label)}</td><td class="num">${g.rows.length}</td>${['late_count','on_time_rate','distance_km','end_to_end_seconds','total_lateness_min'].map(key=>`<td class="num">${raw(mean(g.rows.map(r=>r[key])),key,2)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;}
function drawHistory(){const labels={pure_direct:'直送 · 原点',direct_relay:'直送 + 中继',direct_stations:'直送 + 预部署',direct_relay_stations:'预部署 + 中继'},ms=[['mean_late_count','历史逾期任务均值','项',2],['on_time_rate','历史按时率均值','%',1],['mean_distance_km','历史航程均值','km',2],['mean_wall_seconds','历史 wall_seconds 均值','s',2],['mean_total_lateness_min','历史总逾期均值 · 项目次级','min',2],['budget_seconds','历史总预算（不相等）','s',0]];
 $('history-plots').innerHTML=ms.map((m,i)=>figure(`hist-${i}`,m[1],'独立历史来源，每策略3个种子。')).join('');ms.forEach((m,i)=>graph(`hist-${i}`,'bars',D.history.map(r=>({label:labels[r.scenario],sub:`${r.budget_seconds}s 总预算`,value:r[m[0]]*(m[0]==='on_time_rate'?100:1),series:'baseline'})),m[1],m[2],m[3]));$('history-table').innerHTML=`<table><thead><tr><th>历史策略</th><th>预算 s</th><th>逾期均值</th><th>按时率</th><th>航程 km</th><th>逾期 min</th></tr></thead><tbody>${D.history.map(r=>`<tr><td>${labels[r.scenario]}</td><td class="num">${r.budget_seconds}</td><td class="num">${fmt(r.mean_late_count)}</td><td class="num">${fmt(r.on_time_rate*100,1)}%</td><td class="num">${fmt(r.mean_distance_km)}</td><td class="num">${fmt(r.mean_total_lateness_min)}</td></tr>`).join('')}</tbody></table>`;}
$('decisions').innerHTML='<ul>'+D.decisions.map(x=>`<li>${stageNames[x.stage]||x.stage}：${esc(x.reason||'')}；结果 ${esc(JSON.stringify(x.passed??x))}</li>`).join('')+'</ul>';
$('protocol').innerHTML=['screen_gate','default_gate','pairing','continuation'].filter(k=>D.manifest.protocol[k]).map(k=>`<p>${esc(D.manifest.protocol[k])}</p>`).join('')+`<p>小规模合法性烟测：8任务 / 2机 / K=4，每次最多80轮、5秒预算；${D.runs.filter(r=>r.stage==='smoke').length} 次完整记录。烟测不与200任务的质量成绩混算。</p>`;
$('fingerprints').textContent=`旧版冻结提交 ${D.manifest.baseline_commit}；输入 SHA-256 ${D.manifest.input_sha256}。本批 runs ${D.fingerprints.runs}；独立审计 ${D.fingerprints.validation}。前批 runs ${D.prior_fingerprints.runs}；历史 summary ${D.history_fingerprints.summary}。${D.extension?'追加 runs '+D.extension.fingerprints.runs+'；追加审计 '+D.extension.fingerprints.validation+'；原报告 '+D.initial_report_sha256+'。':''}`;
$('stage').addEventListener('change',draw);root.addEventListener('toggle',()=>requestAnimationFrame(()=>plots.forEach(f=>f())),true);draw();let pending=false;new ResizeObserver(()=>{if(pending)return;pending=true;requestAnimationFrame(()=>{pending=false;plots.forEach(f=>f());});}).observe(root);
})();</script></body></html>'''


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--prior-dir", type=Path, default=PRIOR)
    parser.add_argument("--extension-dir", type=Path)
    args = parser.parse_args(argv)
    directory = args.output_dir.resolve()
    data = build_data(directory, args.prior_dir.resolve())
    if args.extension_dir:
        data["extension"] = build_extension(args.extension_dir.resolve(), data)
        data["extension"]["relative_path"] = Path(os.path.relpath(args.extension_dir.resolve(), directory)).as_posix()
        original = directory / "index.initial.html"
        if not original.exists():
            original.write_bytes((directory / "index.html").read_bytes())
        data["initial_report_sha256"] = previous.sha256(original)
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    html = TEMPLATE.replace("__STYLE__", STYLE).replace("__CHART_HELPERS__", CHART_HELPERS).replace("__DATA__", encoded)
    target = directory / "index.html"
    target.write_text(html, encoding="utf-8")
    print(f"Rendered {target} ({target.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
