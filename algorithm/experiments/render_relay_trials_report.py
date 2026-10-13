"""Render an audited, offline relay-trial report without invoking the solver."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "algorithm/results/relay_trials_20261013/batch1"
HISTORICAL = ROOT / "algorithm/results/final_scenario_comparison"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def build_data(directory: Path, historical: Path) -> dict:
    manifest, runs, decisions, audit = (
        load(directory / name) for name in
        ("manifest.json", "runs.json", "decisions.json", "validation.json")
    )
    if not audit.get("passed") or not audit.get("protocol_complete"):
        raise ValueError("Only complete, independently audited batches may be rendered")
    fingerprints = {}
    for name in ("manifest", "runs", "decisions"):
        actual = sha256(directory / f"{name}.json")
        if audit.get(f"{name}_sha256") != actual:
            raise ValueError(f"Stale audit: {name}.json")
        fingerprints[name] = actual
    fingerprints["validation"] = sha256(directory / "validation.json")
    if audit.get("run_count", len(runs)) != len(runs):
        raise ValueError("Audit run count does not match")
    for row in runs:
        solution = (directory / row["solution_file"]).resolve()
        if not solution.is_relative_to(directory.resolve()) or sha256(solution) != row["solution_sha256"]:
            raise ValueError("Solution path or fingerprint does not match the audited run")
        for metric in ("late_count", "total_lateness_min", "distance_km", "end_to_end_seconds", "on_time_rate"):
            if not math.isfinite(float(row[metric])):
                raise ValueError(f"Nonfinite metric: {metric}")

    history_manifest = load(historical / "manifest.json")
    history_summary = load(historical / "summary.json")
    history_runs = load(historical / "runs.json")
    # The summary is the actual historical source, not the pasted slide numbers.
    # Reconcile its count and means to the frozen raw runs before embedding it.
    history = []
    for summary in history_summary:
        group = [row for row in history_runs if row["scenario"] == summary["scenario"]]
        if len(group) != summary["runs"]:
            raise ValueError("Historical summary count disagrees with raw runs")
        for field, raw in (("mean_late_count", "late_count"),
                           ("mean_total_lateness_min", "total_lateness_min"),
                           ("mean_distance_km", "distance_km"),
                           ("mean_wall_seconds", "wall_seconds")):
            if not math.isclose(summary[field], mean(row[raw] for row in group), abs_tol=1e-7):
                raise ValueError(f"Historical summary disagreement: {field}")
        budget = history_manifest["scenario_budgets_seconds"][summary["scenario"]]
        history.append(summary | {"budget_seconds": budget,
                                  "on_time_rate": 1 - summary["mean_late_count"] / history_manifest["tasks"]})
    history_fingerprints = {name: sha256(historical / f"{name}.json")
                            for name in ("manifest", "runs", "summary")}
    production_path = directory / "production.json"
    production = load(production_path) if production_path.exists() else None
    if production and production.get("validation_sha256") not in (None, fingerprints["validation"]):
        raise ValueError("Production record refers to a different audit")
    notes = ROOT / "papers/notes/relay_20261013/provenance.json"
    literature = load(notes) if notes.exists() else {}
    return dict(manifest=manifest, runs=runs, decisions=decisions, audit=audit,
                fingerprints=fingerprints, history=history,
                history_manifest=history_manifest, history_fingerprints=history_fingerprints,
                production=production, literature=literature)


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>中继接力 · 小预算配对实验</title>
<style>
:root{color-scheme:light dark;--bg:light-dark(#fbfbfc,#16191d);--fg:light-dark(#202830,#eef1f5);--muted:light-dark(#55626e,#b7c1cc);--border:light-dark(#d9dfe5,#3f4852);--surface:light-dark(#f0f3f6,#242c35);--base:light-dark(#596d80,#99b2c8);--candidate:light-dark(#bc3c33,#f3998e);--good:light-dark(#18744f,#81cfaa);--bad:light-dark(#a13530,#efaaa2)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:400 15px/1.65 system-ui,-apple-system,"PingFang SC",sans-serif}main{max-width:1240px;margin:auto;padding:32px 26px 50px}h1,h2,h3{font-weight:500;line-height:1.4}h1{font-size:29px;margin:6px 0 12px}h2{font-size:22px;margin:30px 0 8px}h3{font-size:16px;margin:0 0 8px}p{margin:8px 0}a{color:var(--good)}.muted,figcaption{color:var(--muted)}.eyebrow{font-size:13px;letter-spacing:.08em}.lead{max-width:850px}.outcome{padding:14px 0;border-bottom:1px solid var(--border)}.controls{display:flex;flex-wrap:wrap;gap:18px;padding:18px 0}.controls label{display:grid;gap:4px}select{font:inherit;color:var(--fg);background:var(--bg);padding:7px 10px;border:1px solid var(--border);border-radius:4px;min-height:40px}.legend{display:flex;flex-wrap:wrap;gap:20px;font-size:13px}.swatch{display:inline-block;width:12px;height:12px;border-radius:50%;vertical-align:middle;margin-right:6px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px 30px;margin:18px 0}.grid>figure{min-width:0}figure{margin:0}figcaption{font-size:13px;margin-top:4px}svg{width:100%;display:block;overflow:hidden}svg text{font:12px system-ui,-apple-system,"PingFang SC",sans-serif;fill:var(--fg)}.case{padding-top:8px;border-top:1px solid var(--border);margin-top:20px}.pair-title{display:flex;gap:18px;align-items:baseline;flex-wrap:wrap}.pair-title h2{margin:12px 0 3px}.pair-outcome{font-variant-numeric:tabular-nums;font-weight:500}.table-wrap{overflow-x:auto;margin:15px 0}table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}th,td{padding:8px;border-bottom:1px solid var(--border);text-align:left;white-space:nowrap}th{font-weight:500}.num{text-align:right}.detail{font-size:13px}.good{color:var(--good)}.bad{color:var(--bad)}.source{overflow-wrap:anywhere}details{margin:16px 0}summary{font-weight:500;cursor:pointer;min-height:32px}.foot{margin-top:30px;padding-top:16px;border-top:1px solid var(--border)}.note{border-left:3px solid var(--base);padding:0 12px;margin:14px 0}.stage-review{margin-bottom:14px}.horizontal-list{display:flex;gap:20px;flex-wrap:wrap}.horizontal-list span{white-space:nowrap}.badge{font-size:13px;color:var(--muted)}.empty{color:var(--muted);padding:18px 0}@media(max-width:780px){main{padding:24px 18px}.grid{grid-template-columns:1fr;gap:22px}h1{font-size:25px}}@media(max-width:420px){main{padding:18px 12px}.controls label{width:100%}select{width:100%;font-size:16px}.pair-title{gap:4px}}@media print{body{background:#fff;color:#202830}.controls{display:none}details{display:block}.grid{grid-template-columns:1fr 1fr}.case,figure{break-inside:avoid}a{color:inherit}}
</style></head><body><main id="relay-report">
<header><div class="eyebrow muted">2026-10-13 · 200 任务 / 8 架无人机 · 中继接力</div><h1>接力候选是否带来更好的调度？</h1><p class="lead">以本轮开始时的 main 为基线，先用 10 秒筛选，再对入围方法用 30 秒与 3 个新种子验证。按时订单、总航程与计算时长分别展示；所有退化保留。</p></header>
<div class="outcome"><h3 id="headline"></h3><p id="conclusion"></p><p class="muted detail" id="audit-state"></p></div>
<div class="controls"><label>独立实验阶段<select id="stage"></select></label><label>候选方法<select id="variant"></select></label></div>
<div class="legend"><span><i class="swatch" style="background:var(--base)"></i>main 基线 / 零差值</span><span><i class="swatch" style="background:var(--candidate)"></i>候选方法</span><span>点图：候选 − main；点旁列出原始值</span></div>
<p id="stage-note" class="detail muted" aria-live="polite"></p>
<div id="paired-sections"></div>
<section><h2>30 秒策略参照：直送与中继</h2><p class="muted">从有效且预算合规的原始运行读取，包括只有 baseline 的直送参考。这里展示各策略均值；起点不同，差异不能全归因于中继。它不参与候选晋级判定。</p><div id="reference-note" class="detail"></div><div id="reference-plots" class="grid"></div><div id="reference-table" class="table-wrap"></div></section>
<section class="case"><h2>过去的四策略结果</h2><p class="note">单独保留历史实验的 240 / 480 / 480 / 720 秒预算。它们来自仓库冻结记录，数值与附图略有不同；本轮没有重跑历史。不同源码、种子、预热比例和总预算，不计算跨批改善率或胜率。</p><div id="history-plots" class="grid"></div><div id="history-table" class="table-wrap"></div><p class="detail"><a href="../../final_scenario_comparison/summary.json">历史摘要原始来源</a> · <a href="../../final_scenario_comparison/manifest.json">历史协议与预算</a> · <a href="../../final_scenario_comparison/REPORT.md">历史报告</a></p></section>
<section class="foot"><h2>如何解释这次结果</h2><p>原题直接考核按时任务数、完成全部任务的航程和求解时长。当前工程判优顺序为逾期任务数 → 总逾期分钟 → 航程；总逾期分钟是项目次级约定，因此“配对获胜”可能只改善该约定，而航程更长。</p><p>本轮研究扩展采用存储型接力：先在站点卸货，再由接手机取货，<code>drop_time ≤ pickup_time</code>；沿用论文的最终交付责任计入每机 K=25，前半程转运不占 K。AAAI 2014 文献的即时交接要求双方同时在场，与当前存储模型不同。</p><p>短预算、小样本只提供本实例的探索证据。三个新种子多数获胜是用户选择的采用门槛；没有显著性或最优性证明，也未验证分钟级与其他实例效果。求解时间从 CSV 读取前至完整核验和解写出后，不含 Python 启动和模块导入。</p>
<details><summary>本轮三个独立候选与阶段决策</summary><p><b>保留播种改善</b>：播种耗尽阶段预算时，返回此前已完成且严格改善的 checkpoint。<b>跨机候选多样性</b>：在腿候选 beam 中为不同无人机保留最佳选项，减少同一架机的多个位置挤占全部候选。<b>全局风险优先播种</b>：依据完整全局时序，对原本按任务 ID 扫描的播种顺序进行风险排序。</p><p>三者分别测试，没有组合。主场景为需求驱动预部署 + 中继，次场景为原点起飞 + 中继；筛选允许任一有效配对获胜后扩大，只验证一个入围候选。DIRECT 固定 100 轮兼容检查只验证决策等价，不作为墙钟质量证据。</p><div id="decisions"></div><div id="compatibility"></div></details>
<details><summary>文献证据与检索范围</summary><p>[全文] Coltin &amp; Veloso (2014), <a href="https://doi.org/10.1609/aaai.v28i1.9034">Scheduling for Transfers in Pickup and Delivery Problems with Very Large Neighborhood Search</a>，官方 PDF 页 4–5支持先直送后拆分、交接关联时序传播和死锁检测；具体 beam 多样性、风险优先与 checkpoint 是项目适配。</p><p>[摘要] Masson, Lehuédé &amp; Péton (2013), <a href="https://doi.org/10.1287/trsc.1120.0432">An Adaptive Large Neighborhood Search for the Pickup and Delivery Problem with Transfers</a>，摘要支持固定中转点插入启发式与 ALNS 的组合。本轮未读取其全文。</p><p>仓库 Wen 等论文中的 Transfer 是跨实例知识迁移，不能作为物理中继的直接依据。arXiv 方法检索两次返回 429，没有文献记录；3 次 Crossref 首屏检索为有界非穷尽检索。</p><p>软件方法引用：Kassis, Agarwal, He, Patel &amp; Brueckner (2026), <a href="https://arxiv.org/abs/2609.00065">Scientific Agent Skills</a>。最新元数据已在线核实，引用无版本 URL。</p><p><a href="../../../../papers/notes/relay_20261013/reading_notes.md">方法阅读与本题适配</a> · <a href="../../../../papers/notes/relay_20261013/provenance.json">可复现检索及失败记录</a></p></details>
<details><summary>完整数据、核验与源码指纹</summary><p class="source" id="fingerprints"></p><p><a href="manifest.json">冻结协议</a> · <a href="runs.json">全部运行</a> · <a href="decisions.json">决策</a> · <a href="validation.json">独立复算</a> · <a href="README.md">复现说明</a></p><p>图形、数值和来源元数据嵌入本报告，离线即可查看；完整解与源码归档保留为相邻仓库文件。</p></details></section>
</main><script type="application/json" id="report-data">__DATA__</script><script>
(() => {
'use strict';
const D=JSON.parse(document.getElementById('report-data').textContent),root=document.getElementById('relay-report'),$=id=>root.querySelector('#'+id);
const names={baseline:'main 基线',checkpoint:'保留播种改善',diversity:'跨机候选多样性',risk:'全局风险优先播种'},caseNames={fleet200__direct_relay_stations:'主场景 · 预部署 + 中继',fleet200__direct_relay:'次场景 · 原点起飞 + 中继'},stageNames={screen:'10 秒筛选 · 2 个种子',medium:'30 秒验证 · 3 个新种子'};
const primary=D.manifest.protocol.primary_case,secondary='fleet200__direct_relay',valid=r=>r.valid&&r.budget_compliant,esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(v,d=2)=>Number(v).toFixed(d),sign=(v,d=2)=>(v>1e-9?'+':'')+fmt(Math.abs(v)<1e-9?0:v,d),mean=rs=>rs.reduce((s,x)=>s+x,0)/rs.length;
const score=r=>[r.late_count,r.total_lateness_min,r.distance_km],cmp=(a,b)=>{const x=score(a),y=score(b);for(let i=0;i<3;i++){if(x[i]!==y[i])return x[i]<y[i]?-1:1}return 0};
const metrics=[['late_count','逾期任务数 ↓','项',0,1],['on_time_rate','按时完成率 ↑','百分点',1,100],['distance_km','总航程 ↓','km',2,1],['end_to_end_seconds','端到端求解时间 ↓','s',3,1],['total_lateness_min','总逾期时间 ↓ · 项目次级','min',2,1]];
const last=D.decisions.find(x=>x.stage==='medium'),selected=D.decisions.find(x=>x.stage==='screen')?.selected_variant;
function pairs(stage,caseId,v){const rs=D.runs.filter(r=>r.stage===stage&&r.case_id===caseId&&valid(r));return rs.filter(r=>r.variant==='baseline').sort((a,b)=>a.seed-b.seed).map(b=>[b,rs.find(c=>c.seed===b.seed&&c.variant===v)]).filter(p=>p[1]);}
function wtl(ps){const n=[0,0,0];ps.forEach(([b,c])=>n[cmp(c,b)+1]++);return `${n[0]} 胜 / ${n[1]} 平 / ${n[2]} 负`;}
const promoted=last&&last.passed===true;
$('headline').textContent=selected?`${names[selected]}：${promoted?'达到短预算采用门槛':'未达到默认替换门槛'}`:'本轮没有候选进入扩大验证';
if(selected&&last){const ps=pairs('medium',primary,selected),ss=pairs('medium',secondary,selected),delta=mean(ps.map(([b,c])=>c.late_count-b.late_count)),rateDelta=mean(ps.map(([b,c])=>100*(c.on_time_rate-b.on_time_rate))),first=[0,0,0];ps.forEach(([b,c])=>first[Math.sign(c.late_count-b.late_count)+1]++);$('conclusion').textContent=`30 秒主场景按项目三级目标 ${wtl(ps)}；官方逾期任务单项 ${first[0]} 优 / ${first[1]} 平 / ${first[2]} 差，均值变化 ${sign(delta)} 项、按时率 ${sign(rateDelta,3)} 个百分点。原点次场景按项目目标 ${wtl(ss)}。${D.production?.default_enabled?'已设为默认。':promoted?'默认纳入情况见本批复现说明。':'保留现有默认。'}${delta>0?'总逾期或航程的均值改善不能抵消更多任务逾期。':'项目判胜可能仅改善总逾期，官方指标未必同步改善。'}`;}
else $('conclusion').textContent='筛选记录完整保留，未扩大为长预算测试。';
$('audit-state').textContent=`${D.runs.length} 次完整运行；独立审计通过 ${D.runs.filter(valid).length} 次。累计实际求解 ${fmt(D.runs.reduce((s,r)=>s+r.end_to_end_seconds,0)/60)} 分钟；最长单次预算 ${Math.max(...D.runs.map(r=>r.budget_seconds))} 秒。`;
const stages=['screen','medium'].filter(s=>D.runs.some(r=>r.stage===s));$('stage').innerHTML=stages.map(s=>`<option value="${s}">${stageNames[s]}</option>`).join('');$('stage').value=stages.includes('medium')?'medium':'screen';
function setVariants(){const stage=$('stage').value,vs=[...new Set(D.runs.filter(r=>r.stage===stage&&r.variant!=='baseline').map(r=>r.variant))],old=$('variant').value;$('variant').innerHTML=vs.map(v=>`<option value="${v}">${names[v]}</option>`).join('');$('variant').value=vs.includes(old)?old:vs.includes(selected)?selected:vs[0];}
let plots=[];
function svgNode(id,w,h,body,title){const e=$(id);e.setAttribute('viewBox',`0 0 ${w} ${h}`);e.innerHTML=`<title>${esc(title)}</title><desc>${esc(title)}，准确值同时提供在邻近数据表。</desc>${body}`;}
function figure(id,title,caption=''){return `<figure><h3>${esc(title)}</h3><svg id="${id}" role="img" aria-label="${esc(title)}"></svg><figcaption>${esc(caption)}</figcaption></figure>`;}
function deltaPlot(id,rs,title,unit,decimals){const e=$(id),w=Math.max(280,e.parentElement.clientWidth),l=68,r=62,t=25,rh=64,h=t+rh*rs.length+44,vals=rs.map(x=>x.delta),abs=Math.max(...vals.map(Math.abs),unit==='项'?1:unit==='百分点'?.5:.05)*1.25,x=n=>l+(n+abs)/(2*abs)*(w-l-r);let out='';
 for(const v of [-abs,0,abs]){out+=`<line x1="${x(v)}" x2="${x(v)}" y1="${t-3}" y2="${h-42}" stroke="var(${v===0?'--fg':'--border'})" stroke-width="${v===0?1.1:.7}"/><text x="${x(v)}" y="${h-22}" text-anchor="middle">${sign(v,unit==='项'?1:2)}</text>`;}
 rs.forEach((v,i)=>{const y=t+i*rh+13,xx=x(v.delta);out+=`<text x="${l-8}" y="${y+4}" text-anchor="end">${esc(v.label)}</text><line x1="${x(0)}" x2="${xx}" y1="${y}" y2="${y}" stroke="var(--base)"/><circle cx="${x(0)}" cy="${y}" r="3" fill="var(--base)"/><circle cx="${xx}" cy="${y}" r="5" fill="var(${v.series==='baseline'?'--base':'--candidate'})"><title>${esc(v.label)}：${esc(v.raw)}；变化 ${sign(v.delta,decimals)} ${esc(unit)}</title></circle><text x="${xx}" y="${y-10}" text-anchor="middle">${sign(v.delta,decimals)}</text><text x="${l}" y="${y+26}" fill="var(--muted)">${esc(v.raw)}</text>`;});
 out+=`<text class="axis-title" data-axis="x" x="${w-r}" y="${h-4}" text-anchor="end">${esc(unit)}</text><text class="axis-title" data-axis="y" x="4" y="14">种子</text>`;svgNode(id,w,h,out,title);}
function bars(id,rs,title,unit,decimals){const e=$(id),w=Math.max(280,e.parentElement.clientWidth),l=w<420?105:150,r=73,t=24,rh=53,h=t+rh*rs.length+40,max=(unit==='%'?100:Math.max(1,...rs.map(r=>r.value))*1.06),x=v=>l+v/max*(w-l-r);let out='';
 for(let i=0;i<4;i++){const v=max*i/3;out+=`<line x1="${x(v)}" x2="${x(v)}" y1="${t-3}" y2="${h-39}" stroke="var(--border)"/><text x="${x(v)}" y="${h-20}" text-anchor="middle">${fmt(v,0)}</text>`;}
 rs.forEach((v,i)=>{const y=t+i*rh,lines=[''];let size=0;for(const ch of v.label){const z=ch.charCodeAt(0)>255?12:6.5;if(size+z>l-17){lines.push('');size=0;}lines[lines.length-1]+=ch;size+=z;}out+=`<text x="${l-8}" y="${y+13}" text-anchor="end">${lines.map((s,j)=>`<tspan x="${l-8}" dy="${j?14:0}">${esc(s)}</tspan>`).join('')}</text>${v.sub?`<text x="${l-8}" y="${y+(lines.length>1?42:32)}" text-anchor="end">${esc(v.sub)}</text>`:''}<rect x="${l}" y="${y+1}" width="${Math.max(0,x(v.value)-l)}" height="17" fill="var(${v.series==='candidate'?'--candidate':'--base'})"><title>${esc(v.label)} ${fmt(v.value,decimals)} ${esc(unit)}</title></rect><text x="${x(v.value)+5}" y="${y+15}">${fmt(v.value,decimals)}${unit==='%'?'%':''}</text>`;});
 out+=`<text class="axis-title" data-axis="x" x="${w-r}" y="${h-3}" text-anchor="end">${esc(unit)}</text><text class="axis-title" data-axis="y" x="4" y="14">策略</text>`;svgNode(id,w,h,out,title);}
function graph(id,type,rs,title,unit,d){plots.push(()=>type==='delta'?deltaPlot(id,rs,title,unit,d):bars(id,rs,title,unit,d));}
function raw(v,key,d){return key==='on_time_rate'?fmt(v*100,1)+'%':fmt(v,d);}
function table(rs,includePair=false){return `<table><thead><tr><th>种子</th><th>版本 / 策略</th><th>逾期项</th><th>按时率</th><th>航程 km</th><th>耗时 s</th><th>逾期 min</th>${includePair?'<th>配对判断</th>':''}<th>完整解</th></tr></thead><tbody>${rs.map(r=>`<tr><td>${r.seed}</td><td>${esc(r.display||names[r.variant])}</td><td class="num">${r.late_count}</td><td class="num">${fmt(r.on_time_rate*100,1)}%</td><td class="num">${fmt(r.distance_km)}</td><td class="num">${fmt(r.end_to_end_seconds,3)}</td><td class="num">${fmt(r.total_lateness_min)}</td>${includePair?`<td>${esc(r.verdict||'—')}</td>`:''}<td><a href="${esc(r.solution_file)}">查看</a></td></tr>`).join('')}</tbody></table>`;}
function drawPaired(){const stage=$('stage').value,v=$('variant').value;plots=[];$('stage-note').textContent=stage==='screen'?'每个候选分别与基线配对，2 个筛选种子；不与30秒验证种子平均。':'仅展示一个入围候选的3个新种子，主场景与原点次场景分别判定；不隐藏退化。';
 $('paired-sections').innerHTML=[primary,secondary].map((c,k)=>{const ps=pairs(stage,c,v);if(!ps.length)return `<section class="case"><h2>${caseNames[c]}</h2><p class="empty">该阶段没有有效完整配对。</p></section>`;return `<section class="case"><div class="pair-title"><h2>${caseNames[c]}</h2><span class="pair-outcome">${wtl(ps)}</span><span class="badge">相同 ${ps[0][0].budget_seconds} 秒总预算</span></div><div class="grid" id="metrics-${k}">${metrics.map((m,i)=>figure(`case-${k}-metric-${i}`,m[1],i===1?'正值表示按时率更高，其余质量/耗时指标负值更低。':i===4?'项目次级目标，原题没有单列这项优先级。':'逐种子原始值：main → 候选。')).join('')}</div><details><summary>逐种子完整成绩与取舍</summary><div class="table-wrap">${table(ps.flatMap(([b,c])=>[b,{...c,verdict:cmp(c,b)<0?'候选胜':cmp(c,b)>0?'候选负':'完全平'}]),true)}</div><p class="detail">判胜按完整三级得分，不以航程或均值抵消新增逾期。所有种子与完整解保留。</p></details><details><summary>接力阶段与真实交接诊断</summary><h3>接力阶段：DIRECT 预热 → 最终完整解</h3><p class="detail muted">下列变化属于每次运行自己的中继阶段；不是两算法间的提升，也不单独计为候选获胜。</p><div class="grid">${[['late_count','逾期任务变化','项'],['total_lateness_min','总逾期变化 · 项目次级','min'],['distance_km','航程变化','km']].map((m,i)=>figure(`warm-${k}-${i}`,m[1],'每种子依次列 main、候选；预热值 → 最终值。')).join('')}</div><h3>真实接力与等待</h3><div class="grid">${figure(`relay-${k}-0`,'跨机接力数变化','由 RELAY_IN 与 RELAY_OUT 是否属于不同 UAV 判定。')}${figure(`relay-${k}-1`,'中继总等待变化','在全局时序下计算，等待会继续影响后续订单。')}</div><div class="table-wrap"><table><thead><tr><th>种子</th><th>版本</th><th>中继任务</th><th>跨机</th><th>同机</th><th>等待 min</th><th>平均等待 min</th><th>迭代 / s</th></tr></thead><tbody>${ps.flatMap(p=>p).map(r=>{const s=r.relay_statistics||{};return `<tr><td>${r.seed}</td><td>${names[r.variant]}</td><td class="num">${s.relay_task_count??'—'}</td><td class="num">${s.cross_uav_handoff_count??'—'}</td><td class="num">${s.same_uav_relay_count??'—'}</td><td class="num">${fmt(s.total_relay_waiting_time||0,3)}</td><td class="num">${fmt(s.average_relay_waiting_time||0,3)}</td><td class="num">${fmt(r.iterations/r.end_to_end_seconds)}</td></tr>`;}).join('')}</tbody></table></div></details></section>`;}).join('');
 [primary,secondary].forEach((caseId,k)=>{const ps=pairs(stage,caseId,v);if(!ps.length)return;metrics.forEach((m,i)=>graph(`case-${k}-metric-${i}`,'delta',ps.map(([b,c])=>({label:String(b.seed).slice(-4),delta:(c[m[0]]-b[m[0]])*m[4],raw:`${b.seed} · ${raw(b[m[0]],m[0],m[3])} → ${raw(c[m[0]],m[0],m[3])}`})),m[1],m[2],m[3]));
 [['late_count','项',0,0],['total_lateness_min','min',2,1],['distance_km','km',2,2]].forEach((m,i)=>{const rs=ps.flatMap(p=>p).filter(r=>Array.isArray(r.relay_warmup_score)).map(r=>({label:String(r.seed).slice(-4),series:r.variant,delta:r[m[0]]-r.relay_warmup_score[m[3]],raw:`${r.variant==='baseline'?'main':'候选'} · ${fmt(r.relay_warmup_score[m[3]],m[2])} → ${fmt(r[m[0]],m[2])}`}));graph(`warm-${k}-${i}`,'delta',rs,'预热到最终 '+m[0],m[1],m[2]);});
 [['cross_uav_handoff_count','项',0],['total_relay_waiting_time','min',3]].forEach((m,i)=>graph(`relay-${k}-${i}`,'delta',ps.map(([b,c])=>({label:String(b.seed).slice(-4),delta:(c.relay_statistics?.[m[0]]||0)-(b.relay_statistics?.[m[0]]||0),raw:`${b.seed} · ${fmt(b.relay_statistics?.[m[0]]||0,m[2])} → ${fmt(c.relay_statistics?.[m[0]]||0,m[2])}`})),m[0],m[1],m[2]));});
 drawReferences(v);drawHistory();plots.forEach(f=>f());}
function drawReferences(v){const refs=D.runs.filter(r=>r.stage==='reference'&&r.variant==='baseline'&&valid(r)),rows=refs.map(r=>({...r,display:'直送 · 原点'}));
 for(const [c,label] of [[secondary,'原点中继'],[primary,'预部署中继']])for(const version of ['baseline',v])rows.push(...D.runs.filter(r=>r.stage==='medium'&&r.case_id===c&&r.variant===version&&valid(r)).map(r=>({...r,display:`${label} · ${version==='baseline'?'main':'候选'}`})));
 if(!refs.length){$('reference-note').textContent='未运行或未取得有效直送参考，不补造数据。';$('reference-plots').innerHTML='';$('reference-table').innerHTML='';return;}
 const groups=[...new Set(rows.map(r=>r.display))].map(label=>({label,rows:rows.filter(r=>r.display===label)}));$('reference-note').textContent=`直送参考 ${refs.length} 次，${refs[0].budget_seconds} 秒；每个策略单独取均值，图中标注样本数与预算。`;
 $('reference-plots').innerHTML=metrics.map((m,i)=>figure(`ref-${i}`,m[1],'各组有效运行均值；条形从 0 开始。')).join('');metrics.forEach((m,i)=>graph(`ref-${i}`,'bars',groups.map(g=>({label:g.label.replace('中继 · ',' '),sub:`n=${g.rows.length} · ${g.rows[0].budget_seconds}s`,value:mean(g.rows.map(r=>r[m[0]]))*(m[0]==='on_time_rate'?100:1),series:g.rows[0].variant==='baseline'?'baseline':'candidate'})),m[1],m[0]==='on_time_rate'?'%':m[2],m[3]));$('reference-table').innerHTML=table(rows);}
function drawHistory(){const labels={pure_direct:'直送 · 原点',direct_relay:'直送 + 中继',direct_stations:'直送 + 预部署',direct_relay_stations:'直送 + 预部署 + 中继'},ms=[['mean_late_count','历史逾期任务均值','项',2],['on_time_rate','历史按时率均值','%',1],['mean_distance_km','历史航程均值','km',2],['mean_wall_seconds','历史 wall_seconds 均值','s',2],['mean_total_lateness_min','历史总逾期均值 · 项目次级','min',2],['budget_seconds','历史总预算（不相等）','s',0]];
 $('history-plots').innerHTML=ms.map((m,i)=>figure(`hist-${i}`,m[1],'各策略 3 个历史种子均值；独立来源，不参与本批配对。')).join('');ms.forEach((m,i)=>graph(`hist-${i}`,'bars',D.history.map(r=>({label:labels[r.scenario],sub:`${r.budget_seconds}s 总预算`,value:r[m[0]]*(m[0]==='on_time_rate'?100:1),series:'baseline'})),m[1],m[2],m[3]));$('history-table').innerHTML=`<table><thead><tr><th>历史策略</th><th>总预算 s</th><th>逾期均值</th><th>按时率</th><th>航程 km</th><th>逾期 min</th><th>实际 wall s</th><th>中继任务均值</th></tr></thead><tbody>${D.history.map(r=>`<tr><td>${labels[r.scenario]}</td><td class="num">${r.budget_seconds}</td><td class="num">${fmt(r.mean_late_count)}</td><td class="num">${fmt(r.on_time_rate*100,1)}%</td><td class="num">${fmt(r.mean_distance_km)}</td><td class="num">${fmt(r.mean_total_lateness_min)}</td><td class="num">${fmt(r.mean_wall_seconds)}</td><td class="num">${fmt(r.mean_relay_task_count)}</td></tr>`).join('')}</tbody></table>`;}
$('decisions').innerHTML='<ul>'+D.decisions.map(x=>`<li>${stageNames[x.stage]||x.stage}：${esc(x.selected_variant?names[x.selected_variant]:'未入围')}；${esc(x.reason||'')}；结果 ${esc(typeof x.passed==='object'?JSON.stringify(x.passed):String(x.passed))}</li>`).join('')+'</ul>';
const compat=D.runs.filter(r=>r.stage==='compatibility');$('compatibility').innerHTML=`<p>DIRECT 固定 100 轮：${compat.length} 次运行，${new Set(compat.map(r=>r.decision_fingerprint)).size===1?'四个版本的搜索决策指纹一致':'决策指纹不一致，见审计记录'}。</p>`;
$('fingerprints').textContent=`基线 ${D.manifest.baseline_commit}；输入 SHA-256 ${D.manifest.input_sha256}。本批 runs SHA-256 ${D.fingerprints.runs}；审计 SHA-256 ${D.fingerprints.validation}。历史 summary SHA-256 ${D.history_fingerprints.summary}，历史源码 ${D.history_manifest.solver_source_sha256}。`;
root.addEventListener('toggle',()=>requestAnimationFrame(()=>plots.forEach(f=>f())),true);$('stage').addEventListener('change',()=>{setVariants();drawPaired();});$('variant').addEventListener('change',drawPaired);setVariants();drawPaired();
let pending=false;new ResizeObserver(()=>{if(pending)return;pending=true;requestAnimationFrame(()=>{pending=false;plots.forEach(f=>f());});}).observe(root);
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
