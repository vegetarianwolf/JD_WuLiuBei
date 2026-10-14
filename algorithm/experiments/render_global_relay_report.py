"""Render independently audited global/relay comparisons as an offline HTML report."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "algorithm/results/global_relay_trials_20261014"
HISTORICAL = ROOT / "algorithm/results/final_scenario_comparison"
LITERATURE = ROOT / "papers/notes/global_relay_20261014"


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
    if manifest.get("kind") != "global_relay_trials":
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
    for name in ("environment_notes", "final_source_delta"):
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
    production = load(directory / "production.json") if (directory / "production.json").exists() else None
    if production and production.get("validation_sha256") != fingerprints["validation"]:
        raise ValueError("Production decision is not bound to the current audit")
    literature = load(LITERATURE / "provenance.json")
    literature_fingerprints = {n: sha256(LITERATURE / n) for n in ("reading_notes.md", "provenance.json")}
    fields = ("stage", "variant", "case_id", "scenario", "seed", "task_count", "drone_count", "budget_seconds",
              "end_to_end_seconds", "late_count", "total_lateness_min", "distance_km", "on_time_count", "on_time_rate",
              "iterations", "relay_statistics", "global_move_statistics", "relay_warmup_score", "solution_file",
              "solution_sha256", "initial_routes_sha256", "decision_fingerprint", "valid", "budget_compliant")
    compact_runs = [{k: r[k] for k in fields if k in r} for r in runs]
    return dict(manifest=manifest, runs=compact_runs, decisions=decisions, audit=audit,
                fingerprints=fingerprints, history=history, history_manifest=history_manifest,
                history_fingerprints=history_fingerprints, production=production,
                literature=literature, literature_fingerprints=literature_fingerprints,
                context=context)


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>全局路线调整与选择性中继 · 配对实验</title>
<style>
:root{color-scheme:light dark;--bg:light-dark(#fbfbfc,#16191d);--fg:light-dark(#202830,#eef1f5);--muted:light-dark(#52616e,#bac4cf);--border:light-dark(#d5dce3,#414b55);--surface:light-dark(#eef2f5,#222c35);--base:light-dark(#58738a,#98b7d1);--candidate:light-dark(#b84232,#f39b83);--good:light-dark(#156743,#82d2ac);--bad:light-dark(#a13131,#efa6a6)}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:400 15px/1.65 system-ui,-apple-system,"PingFang SC",sans-serif}main{max-width:1230px;margin:auto;padding:32px 28px 55px}h1,h2,h3{font-weight:500;line-height:1.4}h1{font-size:29px;margin:7px 0 14px}h2{font-size:22px;margin:30px 0 10px}h3{font-size:16px;margin:0 0 7px}p{margin:8px 0}a{color:var(--good)}.eyebrow,.muted,figcaption{color:var(--muted)}.eyebrow{font-size:13px;letter-spacing:.06em}.lead{max-width:930px}.outcome{padding:15px 0;border-bottom:1px solid var(--border)}.controls{display:flex;flex-wrap:wrap;gap:16px;padding:17px 0}.controls label{display:grid;gap:3px;max-width:100%}select{font:inherit;color:var(--fg);background:var(--bg);padding:7px 10px;border:1px solid var(--border);border-radius:4px;min-height:42px;max-width:100%}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:26px 32px;margin:20px 0}.grid>figure{min-width:0}figure{margin:0}figcaption{font-size:13px;margin:5px 0}.chart{display:block;width:100%;overflow:hidden}.chart text{font:12px system-ui,-apple-system,"PingFang SC",sans-serif;fill:var(--fg)}.legend{display:flex;flex-wrap:wrap;gap:18px;font-size:13px}.swatch{display:inline-block;width:12px;height:12px;margin-right:6px;vertical-align:middle}.section{border-top:1px solid var(--border);padding-top:8px;margin-top:24px}.table-wrap{overflow-x:auto;margin:14px 0}table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}th,td{padding:8px;border-bottom:1px solid var(--border);text-align:left;white-space:nowrap}th{font-weight:500}.num{text-align:right}.good{color:var(--good)}.bad{color:var(--bad)}.source{overflow-wrap:anywhere;font-size:12px}.note{font-size:13px}.measure{font-variant-numeric:tabular-nums}details{margin:16px 0}summary{cursor:pointer;font-weight:500;min-height:32px}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--surface);padding:14px;font-size:12px}noscript{display:block;padding:20px}.footer{padding-top:20px;margin-top:30px;border-top:1px solid var(--border)}@media(max-width:720px){main{padding:22px 16px 35px}.grid{grid-template-columns:1fr;gap:23px}h1{font-size:25px}h2{font-size:20px}.controls{gap:12px}.controls label{width:100%}}@media print{details{display:block}.controls{display:none}main{max-width:none}.grid{grid-template-columns:1fr 1fr}}
</style></head><body><main id="global-report">
<div class="eyebrow">京东物流杯 · 2026-10-14 · 完整实例配对实验</div>
<h1 id="headline">全局路线调整与选择性中继</h1>
<p class="lead" id="conclusion"></p>
<div class="outcome"><p id="audit-state" class="measure"></p><p class="note muted">官方 200 项任务 / 8 架无人机 / 每机最多 25 项 / 载荷 2 / 0.9 km·min⁻¹。四策略使用相同阶段总预算。按时完成、航程和求解时长是题目直接关注指标；总逾期分钟是项目沿用的次级目标。</p></div>
<div class="controls"><label>实验阶段<select id="stage"></select></label><label>对比版本<select id="comparison"></select></label></div>
<p id="comparison-note" class="note"></p>
<div class="legend"><span><i class="swatch" style="background:var(--base)"></i><span id="base-label"></span></span><span><i class="swatch" style="background:var(--candidate)"></i><span id="candidate-label"></span></span></div>
<section><h2 id="overview-title">四策略 · 相同预算</h2><p id="overview-note" class="note muted"></p><div id="overview" class="grid"></div></section>
<section class="section"><h2>逐种子变化，保留每次退化</h2><div class="controls"><label>详细场景<select id="case"></select></label></div><p id="pair-outcome" class="measure"></p><div id="paired" class="grid"></div><h3>本阶段全部配对成绩</h3><p class="note muted">每格为前者 → 后者（后者减前者）。判胜严格依次比较逾期任务数、总逾期分钟、航程；均值无法抵消某个种子的退化。</p><div id="paired-table" class="table-wrap"></div></section>
<section class="section"><h2>中继使用与实际跨机交接</h2><p class="note muted">中继数仅解释机制，不作为优劣指标。站点允许先卸后取；K 按最终交付责任计数，前半程不占 K，属于本项目中继扩展。真实跨机由同一任务两段是否使用不同无人机判断。</p><div id="relay-plots" class="grid"></div><div id="relay-table" class="table-wrap"></div><details><summary>全局邻域的实际工作量</summary><div id="moves-table" class="table-wrap"></div><p class="note muted">只展示运行记录实际给出的计数；没有记录的项目显示“—”，不推断为零。工作量与运行耗时一起解释，不能单独证明收益。</p></details></section>
<section class="section"><h2>晋级、验证与默认采用</h2><div id="decisions"></div><p class="note muted">初筛采用完整规模和四个主要场景，三个配对种子；通过冻结门槛才增加 60 秒三个新种子。不同阶段不混算。downstream 的 6 组消融与主比较共享 global 运行，不是新增的 6 次独立 global 样本。</p><details><summary>冻结协议与单机兼容</summary><div id="compatibility"></div><pre id="protocol"></pre></details></section>
<section class="section" id="context-section" hidden><h2>实验环境与最终源码边界</h2><div id="environment-disclosure"></div><div id="source-disclosure"></div></section>
<section class="section"><h2>历史四策略 · 单独展示</h2><p class="note">此处来自仓库 <code>final_scenario_comparison</code> 的 3 个历史种子，预算为 240 / 480 / 480 / 720 秒，与本轮预算不同，不参与本轮胜率或改善率计算。仓库当前汇总与提供截图的部分数字略有差异，以下按可追溯结果文件展示，未照抄截图数值。</p><div id="history" class="grid"></div><div id="history-table" class="table-wrap"></div></section>
<section class="section"><h2>文献如何转为本轮尝试</h2>
<p>两项全局调整是跨路线空载切点尾段交换、固定 DIRECT 路线与预部署起点的全局指派；接力候选尝试把后续订单风险计入候选排序，最终仍完整校验。它们是本题约束下的工程适配，不是对文献算法的完整复现，也不以外部论文收益预测本题成绩。</p>
<ul><li><a href="https://doi.org/10.1287/trsc.1050.0135">Ropke &amp; Pisinger（2006），取送时间窗 ALNS</a>：本轮取得摘要，支持竞争破坏/修复框架。</li><li><a href="https://doi.org/10.1016/j.cor.2013.01.013">Subramanian 等（2013），路线池与集合划分</a>：出版社摘要；PDF 403，未读全文。仅作全局重组方向参考，未宣称本轮实现路线池。</li><li><a href="https://arxiv.org/html/2012.10384">Vidal（2022），SWAP* 原文</a>：已读第 3–5 节，支持跨路线重插与重复评价控制；单节点前三位置定理不适用于本题成对取送和中继耦合。</li><li><a href="https://doi.org/10.1287/trsc.1120.0432">Masson 等（2013），带中转的 ALNS</a>：本轮摘要；<a href="https://doi.org/10.1609/aaai.v28i1.9034">Coltin &amp; Veloso（2014）</a>复用前批全文记录，支持拆分与交接时序传播，原文同步交接与本站点暂存假设不同。</li></ul>
<p class="note">方法检索使用 paper-lookup，14 次有界 HTTP 尝试及所有失败记录可复核；<a href="../../../papers/notes/global_relay_20261014/reading_notes.md">完整阅读笔记</a> · <a href="../../../papers/notes/global_relay_20261014/provenance.json">检索来源</a>。软件方法引用：Kassis, T., Agarwal, V., He, Y., Patel, D., &amp; Brueckner, A. M.（2026），<a href="https://arxiv.org/abs/2609.00065">Scientific Agent Skills</a>，本轮已核验官方作者和最新版本；这不是物流性能证据。</p>
</section>
<footer class="footer"><h3>复核与复现</h3><p><a href="manifest.json">冻结协议</a> · <a href="runs.json">全部运行</a> · <a href="decisions.json">决策记录</a> · <a href="validation.json">独立审计</a> · <a href="README.md">复现说明</a></p><p class="note muted">固定墙钟预算下，机器负载会改变搜索迭代进度；三个种子仅描述当前实例的有限观测，不是普遍优势或显著性检验。图中所有数据直接嵌入，断网仍可切换和查看；外部引用仅在点击时访问。</p><details><summary>来源指纹</summary><p id="fingerprints" class="source"></p></details></footer>
<noscript>请启用 JavaScript 查看本地嵌入的交互图表；全部原始成绩也可从 runs.json 与 runs.csv 读取。</noscript>
</main><script>
const DATA=__DATA__;
(()=>{'use strict';
const D=DATA,root=document.getElementById('global-report'),$=id=>document.getElementById(id);
const names={baseline:'main',global:'全局调整',downstream:'全局 + 下游接力'},cases=D.manifest.protocol.cases,caseNames={fleet200:'直送 · 原点',fleet200__direct_relay:'直送 + 中继',fleet200__direct_stations:'直送 + 预部署',fleet200__direct_relay_stations:'直送 + 预部署 + 中继'},histNames={pure_direct:'直送 · 原点',direct_relay:'直送 + 中继',direct_stations:'直送 + 预部署',direct_relay_stations:'直送 + 预部署 + 中继'},stageNames={screen:'20 秒 · 全规模初筛',validate:'60 秒 · 新种子验证'};
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),fmt=(v,d=2)=>Number(v).toFixed(d),signed=(v,d=2)=>(v>1e-9?'+':'')+fmt(Math.abs(v)<1e-9?0:v,d),avg=a=>a.reduce((s,x)=>s+x,0)/a.length;
const score=r=>[r.late_count,r.total_lateness_min,r.distance_km],cmp=(a,b)=>{let x=score(a),y=score(b);for(let i=0;i<3;i++)if(x[i]!==y[i])return x[i]<y[i]?-1:1;return 0;};
const metrics=[['late_count','逾期任务数 ↓','项',2,1],['on_time_rate','按时完成率 ↑','%',2,100],['distance_km','总航程 ↓','km',2,1],['end_to_end_seconds','实际求解时间','s',3,1],['total_lateness_min','总逾期分钟 ↓ · 项目次级','min',2,1]];
let plots=[];
function compareSpec(){return $('comparison').value==='ablation'?{a:'global',b:'downstream',cases:D.manifest.protocol.relay_cases}: {a:'baseline',b:'global',cases};}
function pairs(stage,caseId,spec=compareSpec()){const rs=D.runs.filter(r=>r.stage===stage&&r.case_id===caseId),candidate=(stage==='validate'&&spec.b==='global'&&D.manifest.protocol.relay_cases.includes(caseId))?(D.decisions.find(d=>d.stage==='screen')?.selected_relay_variant||'global'):spec.b;return rs.filter(r=>r.variant===spec.a).sort((a,b)=>a.seed-b.seed).map(b=>[b,rs.find(c=>c.seed===b.seed&&c.variant===candidate)]).filter(p=>p[1]);}
function tally(ps){const n=[0,0,0];ps.forEach(([b,c])=>n[cmp(c,b)+1]++);return n;}
function wtl(ps){const n=tally(ps);return `${n[0]} 胜 / ${n[1]} 平 / ${n[2]} 负`;}
const initial=D.decisions.find(d=>d.stage==='screen'),validation=D.decisions.find(d=>d.stage==='validate'),enabled=D.production?.default_enabled===true;
$('headline').textContent=enabled?'全局调整已达到采用门槛':validation?.passed?'全局调整通过验证门槛':'全局调整：保留完整实验结果';
const screenPairs=cases.flatMap(c=>pairs('screen',c,{a:'baseline',b:'global'}));
$('conclusion').textContent=`20 秒完整规模初筛：全局调整对 main ${wtl(screenPairs)}，共 ${screenPairs.length} 组配对。${initial?.passed?(validation?.skipped?'后续验证未执行。':`60 秒验证${validation?.passed?'通过':'未通过'}预设采用门槛。`):'未通过初筛门槛，按协议停止扩大。'}${enabled?'已纳入默认配置。':validation?.passed?'达到实验门槛；默认采用状态以本批记录为准。':'新开关默认关闭，继续使用现有 main 算法。'}`;
$('audit-state').textContent=`${D.runs.length} 次运行独立审计通过；累计实际求解 ${fmt(D.runs.reduce((s,r)=>s+r.end_to_end_seconds,0)/60)} 分钟；同一输入、配对初解、种子和阶段总预算。`;
const stages=['screen','validate'].filter(s=>D.runs.some(r=>r.stage===s));$('stage').innerHTML=stages.map(s=>`<option value="${s}">${stageNames[s]}</option>`).join('');$('stage').value=stages.includes('validate')?'validate':'screen';
function setComparison(){const old=$('comparison').value,ab=$('stage').value==='screen';$('comparison').innerHTML='<option value="main">main → 全局候选</option>'+(ab?'<option value="ablation">消融：global → downstream</option>':'');if(ab&&old==='ablation')$('comparison').value='ablation';setCases();}
function setCases(){const old=$('case').value,cs=compareSpec().cases;$('case').innerHTML=cs.map(c=>`<option value="${c}">${caseNames[c]}</option>`).join('');$('case').value=cs.includes(old)?old:cs[cs.length-1];}
function fig(id,title,caption=''){return `<figure><h3>${esc(title)}</h3><svg class="chart" id="${id}" role="img"></svg><figcaption>${esc(caption)}</figcaption></figure>`;}
function svg(id,w,h,body,title){const e=$(id);e.setAttribute('viewBox',`0 0 ${w} ${h}`);e.setAttribute('aria-label',title);e.innerHTML=`<title>${esc(title)}</title><desc>准确值和配对判断见相邻数据表。</desc>${body}`;}
function groupBars(id,groups,title,unit,dec){const e=$(id),w=Math.max(250,e.parentElement.clientWidth),l=64,r=68,t=24,step=91,h=t+groups.length*step+36,max=Math.max(unit==='%'?100:1,...groups.flatMap(g=>g.values.map(v=>v.value)))*(unit==='%'?1:1.02),x=v=>l+v/max*(w-l-r);let body='';for(let i=0;i<4;i++){const v=max*i/3;body+=`<line x1="${x(v)}" x2="${x(v)}" y1="${t}" y2="${h-36}" stroke="var(--border)"/><text x="${x(v)}" y="${h-17}" text-anchor="middle">${fmt(v,0)}</text>`;}
 groups.forEach((g,j)=>{const y=t+j*step;body+=`<text x="0" y="${y+4}">${esc(g.label)}</text><text x="0" y="${y+20}" style="fill:var(--muted)">${esc(g.sub||'')}</text>`;g.values.forEach((v,i)=>{const yy=y+30+i*23;body+=`<text x="${l-7}" y="${yy+12}" text-anchor="end">${esc(v.label)}</text><rect x="${l}" y="${yy}" width="${Math.max(0,x(v.value)-l)}" height="14" fill="var(${v.series==='candidate'?'--candidate':'--base'})"><title>${esc(g.label+' '+v.label)} ${fmt(v.value,dec)} ${esc(unit)}</title></rect><text x="${x(v.value)+5}" y="${yy+12}">${fmt(v.value,dec)}</text>`;});});body+=`<text class="axis-title" data-axis="x" x="${w-r}" y="${h-1}" text-anchor="end">${esc(unit)}</text><text class="axis-title" data-axis="y" x="0" y="14">策略 / 版本</text>`;svg(id,w,h,body,title);}
function deltaPlot(id,rows,title,unit,dec){const e=$(id),w=Math.max(250,e.parentElement.clientWidth),l=83,r=55,t=25,step=69,h=t+rows.length*step+37,extent=Math.max(unit==='项'?1:.01,...rows.map(v=>Math.abs(v.delta)))*1.23,x=v=>l+(v+extent)/(2*extent)*(w-l-r);let body='';for(const v of [-extent,0,extent])body+=`<line x1="${x(v)}" x2="${x(v)}" y1="${t-4}" y2="${h-36}" stroke="var(${v===0?'--fg':'--border'})"/><text x="${x(v)}" y="${h-18}" text-anchor="middle">${signed(v,unit==='项'?1:2)}</text>`;rows.forEach((v,i)=>{const y=t+i*step+15,xx=x(v.delta);body+=`<text x="0" y="${y+4}">${esc(v.label)}</text><line x1="${x(0)}" x2="${xx}" y1="${y}" y2="${y}" stroke="var(--base)"/><circle cx="${x(0)}" cy="${y}" r="3" fill="var(--base)"/><circle cx="${xx}" cy="${y}" r="5" fill="var(--candidate)"><title>${esc(v.raw)}，差值 ${signed(v.delta,dec)} ${esc(unit)}</title></circle><text x="${xx}" y="${y-11}" text-anchor="middle">${signed(v.delta,dec)}</text><text x="0" y="${y+27}" style="fill:var(--muted)">${esc(v.raw)}</text>`;});body+=`<text class="axis-title" data-axis="x" x="${w-r}" y="${h-1}" text-anchor="end">差值 ${esc(unit)}</text><text class="axis-title" data-axis="y" x="0" y="13">种子</text>`;svg(id,w,h,body,title);}
function add(id,type,rows,title,unit,dec){plots.push(()=>type==='bars'?groupBars(id,rows,title,unit,dec):deltaPlot(id,rows,title,unit,dec));}
function arrow(b,c,m){const x=b[m[0]]*m[4],y=c[m[0]]*m[4],dec=m[0]==='late_count'?0:m[3];return `${fmt(x,dec)} → ${fmt(y,dec)} (${signed(y-x,dec)})`;}
function stats(value){return value&&Object.keys(value).length?esc(JSON.stringify(value)):'—';}
function drawOverview(stage,spec){const groups=spec.cases.map(c=>({c,ps:pairs(stage,c,spec)})).filter(g=>g.ps.length);$('overview-title').textContent=spec.a==='baseline'?'四策略 · 相同预算配对':'中继消融 · downstream 对 global';$('overview-note').textContent=spec.a==='baseline'?'每个条形为当前阶段三个种子的均值；仅在同一场景内评价版本变化，完整逐种子结果在下方。':'仅两个中继场景，复用同批 global 作为对照；这 6 组配对不增加 global 独立样本数。';$('overview').innerHTML=metrics.map((m,i)=>fig(`overview-${i}`,m[1],m[0]==='total_lateness_min'?'项目次级目标，不是题目单列优先级。':'从 0 起画；准确数值直接标注。')).join('');metrics.forEach((m,i)=>add(`overview-${i}`,'bars',groups.map(g=>({label:caseNames[g.c],sub:`n=${g.ps.length} 对 · 两版各 ${g.ps[0][0].budget_seconds}s 总预算`,values:[{label:names[g.ps[0][0].variant],value:avg(g.ps.map(p=>p[0][m[0]]))*m[4],series:'baseline'},{label:g.ps[0][1].variant==='downstream'?'全局+下游':'全局',value:avg(g.ps.map(p=>p[1][m[0]]))*m[4],series:'candidate'}]})),m[1],m[2],m[3]));}
function drawPairs(stage,spec){const selected=$('case').value,ps=pairs(stage,selected,spec),late=[0,0,0];ps.forEach(([b,c])=>late[Math.sign(c.late_count-b.late_count)+1]++);$('pair-outcome').textContent=`${caseNames[selected]}：项目三级得分 ${wtl(ps)}；逾期任务单项 ${late[0]} 优 / ${late[1]} 平 / ${late[2]} 差。`;$('paired').innerHTML=metrics.map((m,i)=>fig(`pair-${i}`,m[1],m[0]==='on_time_rate'?'正值表示按时率提高；差值单位为百分点。':'每行对应一个种子，标明两版原始值；负值表示后者数值更低。')).join('');metrics.forEach((m,i)=>add(`pair-${i}`,'delta',ps.map(([b,c])=>({label:String(b.seed),delta:(c[m[0]]-b[m[0]])*m[4],raw:`${fmt(b[m[0]]*m[4],m[0]==='late_count'?0:m[3])} → ${fmt(c[m[0]]*m[4],m[0]==='late_count'?0:m[3])} ${m[2]}`})),m[1],m[0]==='on_time_rate'?'百分点':m[2],m[0]==='late_count'?0:m[3]));const all=spec.cases.flatMap(c=>pairs(stage,c,spec));$('paired-table').innerHTML=`<table><thead><tr><th>场景 / 种子</th><th>前者 → 后者</th><th>逾期任务</th><th>按时率 %</th><th>航程 km</th><th>耗时 s</th><th>逾期 min · 次级</th><th>完整三级判断</th><th>完整解</th></tr></thead><tbody>${all.map(([b,c])=>{const verdict=cmp(c,b);return `<tr><td>${esc(caseNames[b.case_id])}<br>${b.seed}</td><td>${names[b.variant]} → ${names[c.variant]}</td>${metrics.map(m=>`<td class="num">${arrow(b,c,m)}</td>`).join('')}<td class="${verdict<0?'good':verdict>0?'bad':''}">${verdict<0?'后者胜':verdict>0?'后者退化':'相同'}</td><td><a href="${esc(b.solution_file)}">前者</a> / <a href="${esc(c.solution_file)}">后者</a></td></tr>`;}).join('')}</tbody></table>`;}
function drawRelay(stage,spec){const cs=spec.cases.filter(c=>D.manifest.protocol.relay_cases.includes(c)),ps=cs.flatMap(c=>pairs(stage,c,spec)),ms=[['relay_task_count','中继任务数','项',0],['cross_uav_handoff_count','真实跨机交接数','项',0],['total_relay_waiting_time','中继等待总量','min',3]],has=(r,k)=>Number.isFinite(r.relay_statistics?.[k]);$('relay-plots').innerHTML=ms.map((m,i)=>fig(`relay-${i}`,m[1],'后者减前者；候选均由完整全局时序评价，不以数量越多越好。')).join('');ms.forEach((m,i)=>add(`relay-${i}`,'delta',ps.filter(p=>p.every(r=>has(r,m[0]))).map(([b,c])=>({label:String(b.seed).slice(-4)+(b.case_id.endsWith('stations')?' 预部署':' 原点'),delta:c.relay_statistics[m[0]]-b.relay_statistics[m[0]],raw:`${fmt(b.relay_statistics[m[0]],m[3])} → ${fmt(c.relay_statistics[m[0]],m[3])} ${m[2]}`})),m[1],m[2],m[3]));const number=(r,k,d=0)=>has(r,k)?fmt(r.relay_statistics[k],d):'—';$('relay-table').innerHTML=`<table><thead><tr><th>场景 / 种子</th><th>版本</th><th>中继任务</th><th>跨机</th><th>同机</th><th>总等待 min</th><th>平均等待 min</th><th>DIRECT预热 → 最终</th></tr></thead><tbody>${ps.flat().map(r=>`<tr><td>${caseNames[r.case_id]}<br>${r.seed}</td><td>${names[r.variant]}</td><td>${number(r,'relay_task_count')}</td><td>${number(r,'cross_uav_handoff_count')}</td><td>${number(r,'same_uav_relay_count')}</td><td>${number(r,'total_relay_waiting_time',3)}</td><td>${number(r,'average_relay_waiting_time',3)}</td><td>${r.relay_warmup_score?`逾期 ${r.relay_warmup_score[0]} → ${r.late_count}；min ${fmt(r.relay_warmup_score[1])} → ${fmt(r.total_lateness_min)}；km ${fmt(r.relay_warmup_score[2])} → ${fmt(r.distance_km)}`:'—'}</td></tr>`).join('')}</tbody></table>`;const rs=D.runs.filter(r=>r.stage===stage&&r.variant!=='baseline');$('moves-table').innerHTML=`<table><thead><tr><th>场景 / 种子 / 版本</th><th>尾段交换记录</th><th>起点重配记录</th><th>迭代 / 秒</th></tr></thead><tbody>${rs.map(r=>`<tr><td>${caseNames[r.case_id]} / ${r.seed} / ${names[r.variant]}</td><td>${stats(r.global_move_statistics?.tail_exchange_statistics)}</td><td>${stats(r.global_move_statistics?.home_reassignment_statistics)}</td><td>${fmt(r.iterations/r.end_to_end_seconds)}</td></tr>`).join('')}</tbody></table>`;}
function drawHistory(){const ms=[['mean_late_count','历史逾期任务均值','项',2],['mean_distance_km','历史航程均值','km',2],['mean_total_lateness_min','历史总逾期均值 · 项目次级','min',2],['budget_seconds','历史单次总预算（不相等）','s',0]];$('history').innerHTML=ms.map((m,i)=>fig(`hist-${i}`,m[1],'历史独立证据，不参与本轮配对结论。')).join('');ms.forEach((m,i)=>add(`hist-${i}`,'bars',D.history.map(r=>({label:histNames[r.scenario],sub:`n=${r.runs} · ${r.budget_seconds}s 总预算`,values:[{label:'历史',value:r[m[0]],series:'baseline'}]})),m[1],m[2],m[3]));$('history-table').innerHTML=`<table><thead><tr><th>历史策略</th><th>预算 s</th><th>逾期项</th><th>按时率</th><th>航程 km</th><th>逾期 min</th><th>实际 wall s</th><th>中继均值</th></tr></thead><tbody>${D.history.map(r=>`<tr><td>${histNames[r.scenario]}</td><td>${r.budget_seconds}</td><td>${fmt(r.mean_late_count)}</td><td>${fmt(r.on_time_rate*100)}%</td><td>${fmt(r.mean_distance_km)}</td><td>${fmt(r.mean_total_lateness_min)}</td><td>${fmt(r.mean_wall_seconds)}</td><td>${fmt(r.mean_relay_task_count)}</td></tr>`).join('')}</tbody></table>`;}
function render(){plots=[];const stage=$('stage').value,spec=compareSpec();$('comparison-note').textContent=spec.a==='global'?'消融比较只识别在全局候选上追加下游接力排序的效果。main 原始对照请切回主比较。':'主比较只使用每个种子一次 main 与一次候选；初筛和验证使用不同种子，分别报告。验证若选中 downstream，中继场景显示实际入围版本。';$('base-label').textContent=names[spec.a];$('candidate-label').textContent=spec.a==='global'?names.downstream:'全局候选（验证时按实际入围版本）';drawOverview(stage,spec);drawPairs(stage,spec);drawRelay(stage,spec);drawHistory();plots.forEach(f=>f());}
$('decisions').innerHTML=D.decisions.map(d=>`<p><strong>${stageNames[d.stage]||esc(d.stage)}</strong>：${d.skipped?'未执行':d.passed?'通过':'未通过'}。${d.stage==='screen'?`global ${d.wins}/${d.pairs} 胜，逾期任务总差 ${signed(d.late_delta,0)}；downstream 对 global ${d.downstream_vs_global_wins}/${d.downstream_vs_global_pairs} 胜，逾期总差 ${signed(d.downstream_vs_global_late_delta,0)}。中继验证选择 ${names[d.selected_relay_variant]||d.selected_relay_variant}。`:esc(d.reason||'按冻结的逐场景胜率及每对逾期数门槛判定。')}</p>`).join('');
const compat=D.runs.filter(r=>r.stage==='compatibility');$('compatibility').textContent=`25任务单机、固定100轮：${compat.length} 次运行；决策指纹${new Set(compat.map(r=>r.decision_fingerprint)).size===1?'一致':'不一致'}。这项兼容检查不计为200任务性能配对。`;$('protocol').textContent=JSON.stringify(D.manifest.protocol,null,2);
$('fingerprints').textContent=`基线 ${D.manifest.baseline_commit}；输入 ${D.manifest.input_sha256}；manifest ${D.fingerprints.manifest}；runs ${D.fingerprints.runs}；decisions ${D.fingerprints.decisions}；validation ${D.fingerprints.validation}；历史 summary ${D.history_fingerprints.summary}；历史 manifest ${D.history_fingerprints.manifest}；文献 provenance ${D.literature_fingerprints['provenance.json']}。`;
if(D.context?.environment_notes){$('context-section').hidden=false;$('environment-disclosure').innerHTML='<p>首个初筛种子 2026102411 的早期运行与一次全套 pytest 短暂重叠，测试套件因 macOS 图形后端退出；测量没有重跑择优，后续未并行运行全套测试。本批不据此作纯速度加速结论。</p><details><summary>环境记录</summary><pre>'+esc(JSON.stringify(D.context.environment_notes,null,2))+'</pre><a href="environment_notes.json">完整环境记录</a></details>';$('fingerprints').textContent+=` 环境记录 ${D.fingerprints.environment_notes}。`;}
if(D.context?.final_source_delta){$('context-section').hidden=false;$('source-disclosure').innerHTML='<p>本报告成绩来自测量前冻结的源码快照。测量结束后最终源码在中继腿插入枚举完成、全局增量评价完成两处追加了截止时间复查防护；该防护尚未重新测量，不能宣称其带来性能改善。复现本报告须使用归档快照，而非直接用最终源码覆盖结果。</p><details><summary>最终源码差异记录</summary><pre>'+esc(JSON.stringify(D.context.final_source_delta,null,2))+'</pre><a href="final_source_delta.json">完整差异记录</a></details>';$('fingerprints').textContent+=` 最终源码差异 ${D.fingerprints.final_source_delta}。`;}
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
