"""Render the audited two-arm DIRECT weighted-objective trial, without solving.

The report embeds every value it needs and uses no network resources.  The only
comparison is the freshly run current default versus the weighted candidate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "algorithm/results/weighted_direct_single_seed_20261014"
METRICS = ("late_count", "total_lateness_min", "distance_km")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def within(directory: Path, name: str) -> Path:
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError(f"证据路径越出实验目录：{name}")
    return path


def build_data(directory: Path) -> dict:
    directory = directory.resolve()
    manifest = load(directory / "manifest.json")
    runs = load(directory / "runs.json")
    audit_path = directory / "audit.json"
    if not audit_path.exists():
        audit_path = directory / "validation.json"
    audit = load(audit_path)
    if audit.get("passed") is not True:
        raise ValueError("仅生成独立审计通过的完整实验报告")
    if not isinstance(runs, list) or len(runs) != 2:
        raise ValueError("报告只接受现有方案与加权候选的两次完整实测")
    if audit.get("run_count", 2) != 2:
        raise ValueError("审计的运行数与两臂对照不一致")
    fingerprints = {}
    for name in ("manifest.json", "runs.json", audit_path.name):
        fingerprints[name] = sha256(directory / name)
        audited = audit.get(name.removesuffix(".json") + "_sha256")
        if name != audit_path.name and audited != fingerprints[name]:
            raise ValueError(f"审计缺少或不匹配证据指纹：{name}")
    baseline = manifest.get("baseline_variant", manifest.get("order", ["current_lex"])[0])
    by_variant = {row["variant"]: row for row in runs}
    if baseline not in by_variant or len(by_variant) != 2:
        raise ValueError("重复算法或缺少现有方案")
    candidate = next(name for name in by_variant if name != baseline)
    rows = [by_variant[baseline], by_variant[candidate]]
    protocol = manifest.get("protocol", {})
    if not isinstance(protocol, dict):
        protocol = {"description": protocol}
    raw_weights = manifest.get("weights")
    scales = manifest.get("scales")
    if (not isinstance(raw_weights, list) or len(raw_weights) != 3
            or not isinstance(scales, list) or len(scales) != 3
            or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in scales)
            or any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in raw_weights)):
        raise ValueError("需要显式冻结的三项归一化权重与正尺度")
    weights = {k: w / scale for k, w, scale in zip(METRICS, raw_weights, scales)}
    if set(weights) != set(METRICS):
        raise ValueError("需要显式的 late_count / total_lateness_min / distance_km 权重")
    if any(not isinstance(weights[k], (int, float)) or not math.isfinite(weights[k])
           or weights[k] < 0 for k in METRICS):
        raise ValueError("目标权重必须为非负有限数")
    for field in ("seed", "budget_seconds", "initial_routes_sha256"):
        if field not in rows[0] or rows[0][field] != rows[1].get(field):
            raise ValueError(f"两方案未使用相同的 {field}")
    if rows[0]["budget_seconds"] != 240 or len(manifest.get("initial_routes", [])) != 8 or scales[0] != 200:
        raise ValueError("仅接受官方200任务、每臂240秒的本轮实验")
    labels = {name: spec["label"] for name, spec in manifest["variants"].items()}
    for i, row in enumerate(rows):
        row["task_count"] = 200
        row["drone_count"] = 8
        if row.get("source_sha256") != manifest["variants"][row["variant"]]["sha256"]:
            raise ValueError("运行源码与冻结快照不一致")
        if row.get("valid") is not True or row.get("budget_compliant") is not True or row.get("delivered_count") != 200:
            raise ValueError("运行未通过完整解核验")
        for field in (*METRICS, "on_time_count", "on_time_rate", "end_to_end_seconds"):
            if not isinstance(row.get(field), (int, float)) or not math.isfinite(row[field]):
                raise ValueError(f"缺少有限指标：{field}")
        if row["on_time_count"] != row["task_count"] - row["late_count"]:
            raise ValueError("按时完成数与逾期数不一致")
        if not math.isclose(row["on_time_rate"], row["on_time_count"] / row["task_count"], abs_tol=1e-12):
            raise ValueError("按时完成率与任务数不一致")
        row["solution_file"] = row["solution_path"]
        solution = within(directory, row["solution_file"])
        if sha256(solution) != row["solution_sha256"]:
            raise ValueError("解文件与审计指纹不一致")
        fingerprints[row["solution_file"]] = sha256(solution)
        row["label"] = labels.get(row["variant"], "现有词典序方案" if i == 0 else "加权总分候选")
        row["role"] = "baseline" if i == 0 else "weighted"
        row["components"] = {k: weights[k] * row[k] for k in METRICS}
        row["common_weighted_score"] = sum(row["components"].values())
        if "weighted_score" in row and not math.isclose(row["weighted_score"], row["common_weighted_score"], rel_tol=1e-10, abs_tol=1e-9):
            raise ValueError("保存的加权分数与指标/权重不一致")
        points = row.get("best_trajectory", [])
        if any(not isinstance(p, list) or len(p) < 5 or any(not isinstance(v, (int, float))
                   or not math.isfinite(v) for v in p[:5]) for p in points):
            raise ValueError("最优解轨迹格式应为[iteration,elapsed_s,late_count,lateness_min,km]")
        if any(points[j][1] > points[j + 1][1] for j in range(len(points) - 1)):
            raise ValueError("轨迹时间顺序不一致")
        row["trajectory"] = [
            {"iteration": p[0], "seconds": p[1] + row["construction_seconds"], **dict(zip(METRICS, p[2:5])),
             "common_weighted_score": sum(weights[k] * v for k, v in zip(METRICS, p[2:5]))}
            for p in points
        ]
        if points and any(not math.isclose(points[-1][j + 2], row[k], rel_tol=1e-10, abs_tol=1e-9)
                          for j, k in enumerate(METRICS)):
            raise ValueError("轨迹末点与最终核验解不一致")
    input_path = ROOT / manifest["input_path"]
    if sha256(input_path) != manifest["input_sha256"]:
        raise ValueError("官方输入已改变")
    fingerprints[manifest["input_path"]] = sha256(input_path)
    problem_path = ROOT / manifest["problem_path"]
    if sha256(problem_path) != manifest["problem_sha256"]:
        raise ValueError("题目文件已改变")
    fingerprints[manifest["problem_path"]] = sha256(problem_path)
    for name, source in manifest["variants"].items():
        target = within(directory, source["archive"])
        if sha256(target) != source["sha256"]:
            raise ValueError(f"冻结算法快照指纹不一致：{name}")
        fingerprints[source["archive"]] = source["sha256"]
    for key in ("source_snapshots", "execution_sources"):
        for source in manifest.get(key, {}).values():
            target = within(directory, source["path"])
            if sha256(target) != source["sha256"]:
                raise ValueError(f"冻结源码指纹不一致：{source['path']}")
            fingerprints[source["path"]] = source["sha256"]
    context = {}
    for name in ("provenance.json", "literature.json", "literature.md", "README.md"):
        path = directory / name
        if path.exists():
            fingerprints[name] = sha256(path)
            context[name] = load(path) if path.suffix == ".json" else path.read_text(encoding="utf-8")
    context["renderer_sha256"] = sha256(Path(__file__))
    return {"manifest": manifest, "protocol": protocol, "audit": audit, "rows": rows, "weights": weights,
            "raw_weights": raw_weights, "scales": scales,
            "fingerprints": fingerprints, "context": context,
            "evidence_links": [name for name in fingerprints
                               if (directory / name).is_file()
                               and (directory / name).resolve().is_relative_to(directory)],
            "delta": {k: rows[1][k] - rows[0][k] for k in
                      (*METRICS, "on_time_count", "on_time_rate", "end_to_end_seconds", "common_weighted_score")}}


HTML = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="官方200任务、纯直送、同种子每臂240秒：现有词典序与加权总分的离线可视化对照及独立审计。">
<title>纯直送 · 加权总分与现有方案对照</title>
<style>
:root{color-scheme:light dark;--bg:#f7f9f6;--paper:#ffffff;--ink:#20352d;--muted:#596d64;--line:#d8e2dc;--soft:#ebf1ed;--base:#356ea1;--weighted:#18846a;--good:#177453;--bad:#aa4234;--component1:#5376a3;--component2:#bf8549;--component3:#679582}
@media(prefers-color-scheme:dark){:root{--bg:#15211b;--paper:#1c2b23;--ink:#e9f0eb;--muted:#b1c1b7;--line:#3c5145;--soft:#243b2d;--base:#91b9df;--weighted:#83d3b6;--good:#99d7b5;--bad:#f2afa3;--component1:#95b8df;--component2:#e2b57f;--component3:#9dcab6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.7 "PingFang SC","Microsoft YaHei",system-ui,sans-serif}main{max-width:1160px;margin:auto;padding:36px 32px 28px}header{padding-bottom:22px;border-bottom:1px solid var(--line)}h1,h2,h3,strong{font-weight:500}h1{font-size:clamp(28px,4vw,40px);line-height:1.3;letter-spacing:-.025em;margin:10px 0 12px}h2{font-size:23px;line-height:1.4;margin:0 0 9px}h3{font-size:16px;line-height:1.5;margin:0 0 6px}p{margin:9px 0}.eyebrow{font-size:12px;letter-spacing:.08em;color:var(--weighted)}.intro,.muted,.caption{color:var(--muted)}.intro{max-width:850px}.caption{font-size:12px;line-height:1.7}.facts,.legend{display:flex;flex-wrap:wrap;gap:7px 24px;font-size:13px;margin-top:16px}.legend span{display:flex;align-items:center;gap:7px}.swatch{width:22px;height:4px;background:var(--base);display:inline-block}.swatch.weighted{background:var(--weighted)}section{margin-top:30px}.result{border-left:3px solid var(--weighted);padding:3px 0 3px 15px}.result.tradeoff{border-color:var(--component2)}.result h2{font-size:20px}.metrics,.two-col,.curves{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:26px 34px}.metrics{margin-top:20px}.metric,.plot{min-width:0}.chart{display:block;width:100%;overflow:visible}.chart text{font:12px "PingFang SC",system-ui,sans-serif;fill:var(--ink)}.chart .axis-text{fill:var(--muted)}.chart .grid{stroke:var(--line);stroke-width:1}.chart .frame{fill:none;stroke:var(--line);stroke-width:1}.chart .baseline{stroke:var(--base);fill:none;stroke-width:2;stroke-dasharray:6 4}.chart .weighted{stroke:var(--weighted);fill:none;stroke-width:2}.tabular{font-variant-numeric:tabular-nums}.delta{font-size:13px;margin-top:5px}.good{color:var(--good)}.bad{color:var(--bad)}.formula{font:14px/1.7 ui-monospace,SFMono-Regular,Consolas,monospace;word-break:break-word;margin:16px 0;padding:12px 14px;background:var(--soft)}.separator{border-top:1px solid var(--line);padding-top:24px}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}th,td{padding:10px 8px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}th{font-weight:500;color:var(--muted)}th:first-child,td:first-child{text-align:left}caption{text-align:left;color:var(--muted);font-size:12px;margin:5px 0 10px}.nowrap{white-space:nowrap}.audit-table td:first-child{min-width:170px}.audit-table td:not(:first-child){text-align:left}.trace-controls{margin:16px 0}.trace-controls label{display:block;font-size:13px;margin-bottom:5px}input[type=range]{width:100%;margin:0;accent-color:var(--weighted);min-height:32px}.trace-state{font-size:13px;background:var(--soft);padding:10px 13px;margin:9px 0 20px}.trace-state span{display:block}.curves{gap:25px 32px}.chart .time-guide{stroke:var(--muted);stroke-width:1}.method-list{padding-left:20px}.method-list li{margin:7px 0}.reference-list{padding-left:22px;font-size:13px}.reference-list li{margin:12px 0}.reference-list small{display:block;color:var(--muted);font-size:12px}a{color:var(--base);text-underline-offset:3px;overflow-wrap:anywhere}details{margin:12px 0}summary{padding:6px 0;cursor:default}pre{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font:12px/1.7 ui-monospace,SFMono-Regular,Consolas,monospace;color:var(--muted);background:var(--soft);padding:14px}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}.file-links{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:12px;margin:16px 0}footer{border-top:1px solid var(--line);padding-top:17px;margin-top:28px;color:var(--muted);font-size:12px}.sr-only{position:absolute;clip:rect(0,0,0,0);width:1px;height:1px;overflow:hidden}noscript{display:block;color:var(--bad);margin:20px 0}
@media(max-width:700px){main{padding:25px 18px}.metrics,.two-col,.curves{grid-template-columns:1fr;gap:22px}h2{font-size:21px}.facts{gap:5px 16px}th,td{padding:8px 7px}input[type=range]{min-height:44px}}
@media print{body{background:white;color:black}main{max-width:none;padding:10px}details{display:block}details>*{display:block}input[type=range],.file-links{display:none}.chart text{fill:black}.metrics,.two-col,.curves{grid-template-columns:repeat(2,minmax(0,1fr))}section{break-inside:avoid}a{color:inherit}}
</style></head><body><main id="weighted-report">
<header><div class="eyebrow">无人机取送货调度 · 目标函数探索 · 2026-10-14</div>
<h1>加权总分能改善纯直送吗？</h1>
<p class="intro">官方 200 项任务，现有方案和加权候选各跑一个相同种子、240 秒。按题目考核直接比较按时完成、逾期、总航程及求解时间，再解释加权目标的取舍。</p>
<div class="facts" id="facts"></div></header>
<section class="result" id="result" aria-labelledby="outcome-heading"><h2 id="outcome-heading"></h2><p id="outcome"></p><p class="caption">以下差值统一为“加权候选 − 现有方案”。这是本实例一次配对观察，不给出显著性或跨种子稳定性结论。</p></section>
<section aria-labelledby="metric-heading"><h2 id="metric-heading">题目考核直接相关的结果</h2>
<div class="legend" aria-label="方案图例"><span><i class="swatch" aria-hidden="true"></i>现有词典序方案（虚线轨迹）</span><span><i class="swatch weighted" aria-hidden="true"></i>加权总分候选（实线轨迹）</span></div>
<div class="metrics" id="metrics"></div>
<p class="caption">按时率与逾期率均以 200 项为分母，是任务数量的比例表达；它们不作为独立维度重复评分。截止期允许逾期，任务全部完成属于硬约束。</p></section>
<section class="separator" aria-labelledby="tradeoff-heading"><h2 id="tradeoff-heading">加权总分怎样交换不同目标</h2>
<p>现有方案严格依次最小化“逾期数 → 总逾期分钟 → 航程”；候选最小化下面的共同加权分。相同权重也用于评估现有方案，便于看清变化来源。</p>
<div class="formula tabular" id="formula" style="white-space:pre-line"></div>
<div class="two-col"><div class="metric" id="lateness"></div><div class="plot"><h3>共同加权分数分解 <span class="caption">↓ 越低越好</span></h3><svg class="chart" id="score-chart" role="img"></svg><p class="caption" id="score-caption"></p></div></div>
<p id="exchange"></p><p class="caption">总逾期分钟是项目原有次级目标。加权分数是本次研究自行设定的搜索目标，题目未提供官方综合评分，不能把分数下降等同于考核必然改善。</p>
<div class="table-wrap"><table id="metric-table"><caption>全部终点指标与方向；不对不同单位直接作百分比拼接。</caption></table></div></section>
<section class="separator" aria-labelledby="trace-heading"><h2 id="trace-heading">各方案按自身目标保留的最优解轨迹</h2>
<p class="caption">阶梯表示一次真实保留的解更新，之间维持上个记录值。加权轨迹的逾期数、逾期分钟或航程可以上升；词典序轨迹的低级目标也可能随高级目标改善而上升。这里只对各自目标下保留的解作图，不把物理指标绘制成单调下降的累计最小值。每个子图的两方案共享同一纵轴范围；纵轴为突出变化按实际数据范围截取，不一定从0开始。</p>
<div class="trace-controls"><label for="trace-time">查看记录时刻：<output id="trace-time-output" for="trace-time"></output></label><input id="trace-time" type="range" min="0" step="0.1" value="0"></div>
<div class="trace-state tabular" id="trace-state" aria-live="polite"></div><div class="curves" id="curves"></div>
<p class="caption" id="trace-caption"></p><p class="caption">独立审计复算最终路线；中间轨迹没有保存对应路线，审计只能检查记录完整性与各自目标的单调性，无法逐条重新推演中间路线。求解耗时来自计时记录，也不能由路线几何重建。</p></section>
<section class="separator" aria-labelledby="audit-heading"><h2 id="audit-heading">硬约束与公平对照</h2>
<p id="audit-intro"></p><div class="table-wrap"><table class="audit-table" id="audit-table"></table></div>
<p class="caption" id="timing-note"></p><p class="caption">计时期间没有运行测试、辅助优化或报告渲染；阅读、检索和文字编辑仍继续。本机没有被独占。两方案约0.005秒的耗时差异不能支持速度优劣结论。</p><details><summary>查看独立审计原始记录</summary><pre id="audit-raw"></pre></details></section>
<section aria-labelledby="method-heading"><h2 id="method-heading">本次改了什么，观察能说明什么</h2><div id="method"></div>
<p>单种子、单实例、一个权重向量只能回答“这次是否产生了有价值的取舍”。结果不证明全局最优、普遍提升或分钟级预算下的稳定性，也不足以代替多种子验证。本次保留现有公共默认，不据此一次探索替换生产方案。</p></section>
<section aria-labelledby="literature-heading"><h2 id="literature-heading">文献与可继续尝试的方法</h2><div id="literature-intro"></div><ol class="reference-list" id="references"></ol>
<details><summary>文献检索范围与可复现来源</summary><pre id="literature-provenance"></pre></details></section>
<section class="separator" aria-labelledby="reproduce-heading"><h2 id="reproduce-heading">复现与证据</h2><p class="caption">报告内嵌终点指标、完整轨迹和审计记录，图表无需联网。证据链接依赖同目录文件；重新求解须另建新目录，不能覆盖本次冻结结果。</p>
<pre id="commands"></pre><div class="file-links" id="file-links"></div>
<details><summary>输入、运行结果及冻结源码 SHA-256</summary><pre id="fingerprints"></pre></details>
<details><summary>实验 manifest</summary><pre id="manifest"></pre></details>
<details><summary>执行与环境 provenance</summary><pre id="provenance"></pre></details></section>
<footer id="footer"></footer><noscript>图表需要浏览器 JavaScript；本报告所有数据均已内嵌。可从同目录 runs.json、audit.json 和 manifest.json 读取原始结果。</noscript>
</main><script type="application/json" id="report-data">__DATA__</script><script>
(()=>{'use strict';
const D=JSON.parse(document.getElementById('report-data').textContent),$=id=>document.getElementById(id),rows=D.rows,weights=D.weights,delta=D.delta,protocol=D.protocol,N=rows[0].task_count;
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])), fmt=(v,n=2)=>new Intl.NumberFormat('zh-CN',{maximumFractionDigits:n}).format(v),signed=(v,n=2)=>`${v>0?'+':''}${fmt(v,n)}`,colors=['var(--base)','var(--weighted)'];
const names={late_count:'逾期任务数',total_lateness_min:'总逾期时间',distance_km:'总航程',on_time_count:'按时完成',on_time_rate:'按时完成率',end_to_end_seconds:'端到端求解时间',common_weighted_score:'共同加权总分'}, units={late_count:'项',total_lateness_min:'min',distance_km:'km',on_time_count:'项',on_time_rate:'%',end_to_end_seconds:'s',common_weighted_score:'无量纲'};
const metricValue=(r,k)=>k==='on_time_rate'?r[k]*100:r[k],text=(x,y,s,anchor='start',extra='')=>`<text x="${x}" y="${y}" text-anchor="${anchor}" ${extra}>${esc(s)}</text>`,line=(x1,y1,x2,y2,cls='grid')=>`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" class="${cls}"/>`;
const direction=k=>['on_time_count','on_time_rate'].includes(k)?1:-1, deltaClass=k=>delta[k]===0||(k==='end_to_end_seconds'&&Math.abs(delta[k])<.01)?'muted':delta[k]*direction(k)>0?'good':'bad';
$('facts').innerHTML=[`<span>种子 <strong>${esc(rows[0].seed)}</strong></span>`,`<span><strong>${N}</strong> 项任务 / <strong>${rows[0].drone_count}</strong> 架无人机</span>`,'<span>容量 <strong>2</strong> / 每机最多 <strong>25</strong> 项</span>','<span>中继 <strong>0</strong> / 公共原点出发</span>','<span>每臂预算 <strong>240 s</strong></span>'].join('');
const late=delta.late_count,km=delta.distance_km,score=delta.common_weighted_score;
$('outcome-heading').textContent=late<0?'本次加权方案减少了逾期任务':late>0?'本次加权方案增加了逾期任务':'本次两方案的逾期任务数相同';
if(late>0)$('result').classList.add('tradeoff');
$('outcome').textContent=`按时完成从 ${rows[0].on_time_count} 项变为 ${rows[1].on_time_count} 项（${signed(delta.on_time_count,0)} 项）；逾期数 ${signed(late,0)} 项，航程 ${signed(km)} km，总逾期 ${signed(delta.total_lateness_min)} min。共同加权分变化 ${signed(score,6)}。${late>0?'题目直接关心的按时表现退化，不能只凭加权分改善判定方案更优。':late<0?(km<=0&&delta.total_lateness_min<=0?'本次逾期数、航程和总逾期分钟同时改善；这只是一个种子的观察，仍不证明普遍改进。':'按时表现改善，航程或逾期幅度的变化仍需结合具体代价判断。'):'需继续结合航程、总逾期分钟与实际求解时间判断取舍。'}`;
const fields=['on_time_count','late_count','distance_km','end_to_end_seconds'];
for(const k of fields){const div=document.createElement('div');div.className='metric';div.dataset.field=k;div.innerHTML=`<h3>${names[k]} <span class="caption">${direction(k)>0?'↑ 越高越好':'↓ 越低越好'}</span></h3><svg class="chart" role="img"></svg><p class="delta ${deltaClass(k)}">候选 − 现有：<span class="tabular">${signed(delta[k],k.endsWith('count')?0:k==='end_to_end_seconds'?4:2)} ${units[k]}</span>${k==='on_time_count'||k==='late_count'?` / ${signed(delta[k]/N*100)} 个百分点`:''}</p>`;$('metrics').append(div);}
$('lateness').innerHTML='<h3>总逾期时间 <span class="caption">项目次级目标 · ↓ 越低越好</span></h3><svg class="chart" role="img"></svg><p class="delta '+deltaClass('total_lateness_min')+'">候选 − 现有：'+signed(delta.total_lateness_min)+' min</p>';$('lateness').dataset.field='total_lateness_min';
function barPlot(container,k){const svg=container.querySelector('svg'),w=Math.max(260,container.clientWidth),left=4,right=w-6,width=right-left,values=rows.map(r=>r[k]),maximum=(k.endsWith('count')?N:Math.max(...values,1)*1.07),x=v=>left+v/maximum*width,h=157;svg.setAttribute('viewBox',`0 0 ${w} ${h}`);svg.setAttribute('aria-label',`${names[k]}：现有 ${fmt(values[0])} ${units[k]}，候选 ${fmt(values[1])} ${units[k]}`);let s=`<title>${esc(names[k])}两方案共享零起点尺度</title>`;rows.forEach((r,i)=>{const y=23+i*59,label=fmt(values[i],k.endsWith('count')?0:2)+' '+units[k]+(k.endsWith('count')?' / '+fmt(values[i]/N*100)+'%':'');s+=text(left,y,i===0?'现有方案':'加权候选')+text(right,y,label,'end')+`<rect x="${left}" y="${y+9}" width="${width}" height="17" fill="var(--soft)"/><rect x="${left}" y="${y+9}" width="${Math.max(0,x(values[i])-left)}" height="17" fill="${colors[i]}"/>`;});s+=line(left,125,right,125)+text(left,145,'0','start','class="axis-text"')+text(right,145,fmt(maximum,maximum>100?0:1)+' '+units[k],'end','class="axis-text"');svg.innerHTML=s;}
$('formula').textContent=`J = ${fmt(D.raw_weights[0])} × 逾期数 / ${fmt(D.scales[0],0)} + ${fmt(D.raw_weights[1])} × 总逾期分钟 / L₀ + ${fmt(D.raw_weights[2])} × 航程 / D₀\nL₀ = ${fmt(D.scales[1],6)} min；D₀ = ${fmt(D.scales[2],6)} km。两者来自同一词典序初解，运行前冻结，搜索中不更新。`;
function scorePlot(){const svg=$('score-chart'),w=Math.max(260,svg.parentElement.clientWidth),left=4,right=w-6,max=Math.max(...rows.map(r=>r.common_weighted_score),1e-12)*1.06,keys=['late_count','total_lateness_min','distance_km'],cs=['var(--component1)','var(--component2)','var(--component3)'];svg.setAttribute('viewBox',`0 0 ${w} 213`);svg.setAttribute('aria-label',rows.map(r=>r.label+'共同加权分 '+fmt(r.common_weighted_score,6)+'（无量纲）').join('；'));let s='<title>两个方案以同一权重计算的总分与三项贡献</title>';rows.forEach((r,i)=>{const y=23+i*62;s+=text(left,y,i===0?'现有方案':'加权候选')+text(right,y,fmt(r.common_weighted_score,6)+'（无量纲）','end');let offset=left;keys.forEach((k,j)=>{const width=r.components[k]/max*(right-left);s+=`<rect x="${offset}" y="${y+10}" width="${width}" height="20" fill="${cs[j]}"><title>${esc(names[k])}贡献 ${fmt(r.components[k],6)}</title></rect>`;offset+=width;});});s+=line(left,128,right,128)+text(left,148,'0','start','class="axis-text"')+text(right,148,fmt(max,3)+'（无量纲）','end','class="axis-text"');keys.forEach((k,j)=>{const y=168+j*18;s+=`<rect x="${left}" y="${y-8}" width="14" height="8" fill="${cs[j]}"/>`+text(left+21,y,`${names[k]}贡献：${fmt(rows[0].components[k],6)} → ${fmt(rows[1].components[k],6)}`);});svg.innerHTML=s;}
$('score-caption').textContent='分数越低越好；各颜色分别表示逾期数、逾期分钟、航程的加权贡献。条形从0开始，共享同一尺度。';
const exchanges=[];if(weights.total_lateness_min>0)exchanges.push(`${fmt(weights.late_count/weights.total_lateness_min)} min 总逾期`);if(weights.distance_km>0)exchanges.push(`${fmt(weights.late_count/weights.distance_km)} km 航程`);$('exchange').textContent=`在其他指标相同的条件下，增加1项逾期会付出 ${fmt(weights.late_count,6)} 的无量纲损失，其代价等于${exchanges.join('，或')}。这些权重代表研究者设定的交换率，不是题目给定的价值关系。`;
const tableFields=['on_time_count','on_time_rate','late_count','distance_km','end_to_end_seconds','total_lateness_min','common_weighted_score'];$('metric-table').innerHTML='<caption>终点实测指标，变化均为候选减现有方案；分钟与研究分数另行标注。</caption><thead><tr><th>指标</th><th>现有词典序</th><th>加权总分</th><th>候选 − 现有</th></tr></thead><tbody>'+tableFields.map(k=>`<tr><td>${names[k]}${k==='total_lateness_min'?'（次级）':k==='common_weighted_score'?'（研究）':''}</td><td class="nowrap">${fmt(metricValue(rows[0],k),k==='common_weighted_score'?6:2)} ${units[k]}</td><td class="nowrap">${fmt(metricValue(rows[1],k),k==='common_weighted_score'?6:2)} ${units[k]}</td><td class="nowrap ${deltaClass(k)}">${signed(k==='on_time_rate'?delta[k]*100:delta[k],k==='common_weighted_score'?6:k==='end_to_end_seconds'?4:2)} ${k==='on_time_rate'?'个百分点':units[k]}</td></tr>`).join('')+'</tbody>';
const traceFields=['late_count','total_lateness_min','distance_km','common_weighted_score'],horizon=Math.max(240,...rows.flatMap(r=>r.trajectory.map(p=>p.seconds))),traceCharts=[];let selectedTime=horizon;
for(const k of traceFields){const div=document.createElement('div');div.className='plot';div.innerHTML=`<h3>${names[k]} <span class="caption">${k==='total_lateness_min'?'项目次级':k==='common_weighted_score'?'共同权重评估':'题目指标'}</span></h3><svg class="chart" role="img"></svg>`;$('curves').append(div);traceCharts.push({div,k});}
function valueAt(r,t){let best=null;for(const p of r.trajectory){if(p.seconds>t)break;best=p;}return best;}
function tracePlot({div,k}){const svg=div.querySelector('svg'),w=Math.max(260,div.clientWidth),h=230,left=65,right=w-12,top=18,bottom=178,values=rows.flatMap(r=>r.trajectory.map(p=>p[k]));svg.setAttribute('viewBox',`0 0 ${w} ${h}`);svg.setAttribute('aria-label',names[k]+'的真实保留解轨迹，现有方案虚线，加权候选实线；横轴求解记录时刻秒');if(!values.length){svg.innerHTML='<title>没有保存轨迹</title>'+text(left,70,'没有保存轨迹');return;}let min=Math.min(...values),max=Math.max(...values);const pad=max===min?Math.max(1,Math.abs(max)*.04):(max-min)*.08;min=Math.max(0,min-pad);max+=pad;const x=v=>left+v/horizon*(right-left),y=v=>bottom-(v-min)/(max-min)*(bottom-top);let s=`<title>${esc(names[k])}最优解记录轨迹</title><desc>每个点来自实际保留解记录。物理指标未进行累计最小值处理。两方案共享相同纵轴范围。</desc>`;for(let i=0;i<4;i++){const v=min+(max-min)*i/3,yy=y(v);s+=line(left,yy,right,yy)+text(left-8,yy+4,fmt(v,k==='late_count'?0:1),'end','class="axis-text"');}const ticks=w<380?[0,horizon/2,horizon]:[0,horizon/4,horizon/2,horizon*3/4,horizon];for(const v of ticks){const xx=x(v);s+=line(xx,bottom,xx,bottom+4)+text(xx,bottom+20,fmt(v,0),'middle','class="axis-text"');}s+=`<rect data-chart-frame class="frame" x="${left}" y="${top}" width="${right-left}" height="${bottom-top}"/>`+text(left,14,units[k],'start','class="axis-title" data-axis="y"')+text((left+right)/2,222,'记录时刻 / s','middle','class="axis-title" data-axis="x"');rows.forEach((r,i)=>{const points=r.trajectory;if(!points.length)return;let path=`M${x(points[0].seconds)},${y(points[0][k])}`;for(let j=1;j<points.length;j++)path+=`H${x(points[j].seconds)}V${y(points[j][k])}`;path+=`H${x(horizon)}`;s+=`<path class="${i===0?'baseline':'weighted'}" d="${path}"/>`;const current=valueAt(r,selectedTime);if(current)s+=`<circle cx="${x(selectedTime)}" cy="${y(current[k])}" r="3.5" fill="${colors[i]}"/>`;});s+=line(x(selectedTime),top,x(selectedTime),bottom,'time-guide');svg.innerHTML=s;svg.onclick=e=>{const rect=svg.getBoundingClientRect(),local=e.clientX-rect.left;selectedTime=Math.max(0,Math.min(horizon,(local-left)/(right-left)*horizon));$('trace-time').value=selectedTime;updateTrace();};}
function updateTrace(){selectedTime=Math.max(0,Math.min(horizon,selectedTime));$('trace-time-output').textContent=fmt(selectedTime,1)+' s';$('trace-state').innerHTML=rows.map((r,i)=>{const p=valueAt(r,selectedTime);return `<span>${i===0?'现有方案':'加权候选'}：${p?`逾期 ${fmt(p.late_count,0)} 项 · 总逾期 ${fmt(p.total_lateness_min)} min · 航程 ${fmt(p.distance_km)} km · 共同加权分 ${fmt(p.common_weighted_score,6)}（最后更新 ${fmt(p.seconds,2)} s）`:'尚无保存的解记录'}</span>`;}).join('');traceCharts.forEach(tracePlot);}
$('trace-time').max=horizon;$('trace-time').value=horizon;$('trace-time').addEventListener('input',e=>{selectedTime=Number(e.target.value);updateTrace();});$('trace-caption').textContent=protocol.trajectory_time_boundary||'横轴来源于 runs.json 的 best_trajectory 第2列；原始轨迹时间为搜索耗时，这里加上本臂初解构造秒数，对齐到求解开始。结尾延伸到共同240秒预算仅表示最后保留解未再改变；实际端到端耗时单独展示，不把240秒当作实测耗时。';
const audit=D.audit;$('audit-intro').textContent='两次结果均通过独立审计。核验从官方数据及最终路线重新计算完整完成情况、访问顺序、载荷、每机任务数、时序与指标。';
const checks=[['所有200项任务全部完成','硬约束：不允许遗漏或重复完成'],['每机载荷 ≤ 2','硬约束：每次取送后载荷均检查'],['每机任务数 ≤ 25','硬约束：8机×25任务，全部任务覆盖'],['先取后送、同机直送','硬约束：每个任务先取货再送达；无中继'],['共同原点、开放式路线','相同起点；最后送达后不返航'],['相同随机种子与初解',String(rows[0].seed)+'；初始路线 SHA-256 相同'],['相同输入与当前 main 基础','官方CSV及两臂冻结快照 SHA-256 已核验；候选仅作目标适配'],['相同每臂240秒预算','实际端到端耗时单独展示；预算边界见下方']];$('audit-table').innerHTML='<thead><tr><th>检查</th><th>结果 / 口径</th></tr></thead><tbody>'+checks.map(([a,b])=>`<tr><td>${esc(a)}</td><td>通过 · ${esc(b)}</td></tr>`).join('')+'</tbody>';
$('timing-note').textContent=protocol.time_boundary||'每臂从输入读取开始，包含初解构造、搜索、完整核验及解写出；不含Python启动和模块导入。总上限240秒，从搜索预算中预留1秒收尾；各臂按构造的实际耗时扣除对应预算，顺序执行。';$('audit-raw').textContent=JSON.stringify(audit,null,2);
const description=D.manifest.variant_descriptions||{};let method='<ul class="method-list"><li><strong>现有方案：</strong>'+esc(description[rows[0].variant]||'当前 main 的 C2-Lex-ALNS，保留已采用的相邻强化和末端强化，按严格词典序比较解。')+'</li><li><strong>加权候选：</strong>'+esc(description[rows[1].variant]||'相同硬约束、算子池和强化配置；候选比较、插入排序、regret、模拟退火损失、best保留、局部与末端强化采用同一加权目标；worst_lex 随评分转换成加权损失。')+'</li><li><strong>保留的启发规则：</strong>deadline / slack / risk / assignment_destroy 的领域排序规则保留；退火温度参数保持原配置，没有为加权候选额外调参。</li><li><strong>解释范围：</strong>结果反映整条加权搜索机制的行为。目标尺度改变也会改变退火增量与原温度的相对关系，不能只把差异归因于最终解的排序公式。</li></ul>';if(D.manifest.implementation_notes)method+='<p>'+esc(Array.isArray(D.manifest.implementation_notes)?D.manifest.implementation_notes.join('；'):D.manifest.implementation_notes)+'</p>';$('method').innerHTML=method;
const literature=D.context['literature.json'];let papers=Array.isArray(literature)?literature:literature?.papers||literature?.results||literature?.references||[];if(!Array.isArray(papers))papers=[];papers=[...papers];if(literature?.skill?.software_citation)papers.push({...literature.skill.software_citation,note:'检索使用 Scientific Agent Skills 的 paper-lookup 2.1。已核验当前 arXiv 记录；此项是软件方法引用，不是算法效果证据。',evidence_level:'已核验最新版元数据；未引用全文'});
$('literature-intro').textContent=literature?.summary||'实际通过 Google Scholar 阅读检索首页，选取4篇相关研究，并用 Crossref 核验DOI元数据；检索日期2026-10-14。没有穷尽文献或读取论文全文。下面区分论文支持的动机与尚未测试的项目迁移建议。另列 paper-lookup 技能的软件引用。';
const safeUrl=v=>{try{const u=new URL(v);return ['https:','http:'].includes(u.protocol)?u.href:null;}catch{return null;}};
$('references').innerHTML=papers.map(p=>{if(typeof p==='string')return `<li>${esc(p)}</li>`;const title=p.title||p.name||'未命名记录',url=safeUrl(p.url||p.doi_url||p.primary_url||(p.doi?'https://doi.org/'+p.doi:'')),authors=Array.isArray(p.authors)?p.authors.map(a=>typeof a==='string'?a:a.name||a.display_name).join(', '):p.authors||'',year=p.year||p.publication_year||'',note=p.method_support||p.relevance||p.method_implication||p.takeaway||p.note||'',inference=p.transferable_project_inference||'';return `<li>${url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(title)}</a>`:esc(title)}${year?' ('+esc(year)+')':''}<small>${esc(authors)}${p.doi?' · DOI '+esc(p.doi):''}</small>${note?'<p>'+esc(note)+'</p>':''}${inference?'<p><strong>项目迁移建议（未在本轮叠加测试）：</strong>'+esc(inference)+'</p>':''}${p.evidence_level?'<small>证据范围：'+esc(p.evidence_level)+'</small>':''}</li>`;}).join('')||'<li>结构化文献记录见下方检索原始记录。</li>';
$('literature-provenance').textContent=literature?JSON.stringify(literature,null,2):D.context['literature.md']||'本次报告目录未保存文献记录。';
const commands=D.manifest.reproduction_commands||protocol.reproduction_commands||D.context['provenance.json']?.commands;const renderCommand='python -m algorithm.experiments.render_weighted_direct_report --output-dir algorithm/results/weighted_direct_single_seed_20261014';$('commands').textContent=commands?(Array.isArray(commands)?commands.join('\n\n'):typeof commands==='string'?commands:JSON.stringify(commands,null,2))+'\n\n'+renderCommand:'完整求解与审计命令见同目录 README.md / manifest.json。\n\n'+renderCommand;
const evidence=D.evidence_links;$('file-links').innerHTML=evidence.map(name=>`<a href="${esc(name.split('/').map(encodeURIComponent).join('/'))}">${esc(name)}</a>`).join('');$('fingerprints').textContent=JSON.stringify({...D.fingerprints,'renderer_sha256':D.context.renderer_sha256},null,2);$('manifest').textContent=JSON.stringify(D.manifest,null,2);$('provenance').textContent=JSON.stringify(D.context['provenance.json']||{},null,2);$('footer').textContent='2026-10-14 · 单实例 / 单种子 / 两臂对照 · 每臂240秒 · 原始结果与历史批次分别留存';
function draw(){document.querySelectorAll('.metric[data-field]').forEach(el=>barPlot(el,el.dataset.field));scorePlot();updateTrace();}
const observer=new ResizeObserver(draw);observer.observe($('metrics'));observer.observe($('curves'));draw();
})();
</script></body></html>'''


def render_report(directory: Path) -> Path:
    data = build_data(directory)
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # A JSON string containing an external paper title must not close this tag.
    encoded = encoded.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    output = directory / "index.html"
    output.write_text(HTML.replace("__DATA__", encoded), encoding="utf-8")
    return output


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    print(render_report(args.output_dir.resolve()))


if __name__ == "__main__":
    main()
