"""Render the audited periodic candidate and all three unique valid records.

The 240-second baseline is shared exactly with the first method experiment;
this batch executes one new candidate and adds no independent baseline sample.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

try:
    from . import render_weighted_direct_report as common
    from . import render_weighted_method_report as first_report
except ImportError:
    import render_weighted_direct_report as common
    import render_weighted_method_report as first_report

ROOT = common.ROOT
OUTPUT = ROOT / "algorithm/results/weighted_periodic_improvement_20261014"
FIRST = first_report.OUTPUT


def build_data(directory: Path) -> dict:
    data = common.build_data(directory)
    manifest, rows = data["manifest"], data["rows"]
    if manifest.get("schema_version") != 3:
        raise ValueError("需要 schema 3 的周期交换实验")
    if [r["variant"] for r in rows] != ["weighted_baseline", "weighted_periodic"]:
        raise ValueError("需要当前加权基线与周期互换候选")
    if any(r.get("objective") != "weighted" for r in rows):
        raise ValueError("两方案必须使用同一固定加权目标")
    parameters = manifest.get("periodic_parameters", {})
    if parameters != dict(interval=120, max_candidates=16, anchors_per_route=4, pair_budget_seconds=.008):
        raise ValueError("周期交换参数必须与冻结的 120/16/4/8ms 方案一致")
    previous = first_report.build_data(FIRST)
    for name in ("manifest", "runs", "audit"):
        field = "parent_method_" + name + "_sha256"
        if manifest.get(field) != common.sha256(FIRST / (name + ".json")):
            raise ValueError("前阶段证据指纹不一致：" + name)
    shared = manifest.get("shared_baseline", {})
    if shared.get("reused") is True:
        raw_baseline = common.load(directory / "runs.json")[0]
        if raw_baseline != common.load(FIRST / "runs.json")[0]:
            raise ValueError("共享基线必须逐字段保持原始实测记录")
        if shared.get("source_runs_sha256") != common.sha256(FIRST / "runs.json"):
            raise ValueError("共享基线原 runs 指纹不一致")
        if shared.get("source_solution_sha256") != rows[0]["solution_sha256"]:
            raise ValueError("共享基线原路线指纹不一致")
    else:
        raise ValueError("本历史报告要求共享基线；全新重放应另行标明其样本范围")
    expected_cfg = previous["manifest"]["variants"]["weighted_exchange_lazy"]["config"]
    if manifest["variants"]["weighted_periodic"]["config"] != expected_cfg:
        raise ValueError("周期候选必须继承首轮候选配置，不额外调参")
    if manifest["variants"]["weighted_periodic"]["sha256"] != previous["manifest"]["variants"]["weighted_exchange_lazy"]["sha256"]:
        raise ValueError("周期候选的核心 ZIP 必须与首轮候选相同")
    periodic = rows[1].get("periodic_task_exchange")
    if not isinstance(periodic, dict) or not isinstance(periodic.get("events"), list):
        raise ValueError("需要周期交换真实计数与逐次记录")
    if periodic.get("passes") != len(periodic["events"]):
        raise ValueError("周期调用次数与事件记录不一致")
    for event in periodic["events"]:
        if event.get("probes", 0) > 16 or not 0 <= event.get("budget_seconds", -1) <= .008000001:
            raise ValueError("单次周期动作超出冻结候选数或预留范围")
        if not math.isfinite(event.get("runtime_seconds", math.nan)):
            raise ValueError("缺少周期动作有限实测耗时")
    data["trials"] = [rows[0], previous["rows"][1], rows[1]]
    for key in ("probe", "primitive_probe"):
        if key in previous:
            data[key] = previous[key]
    data["first_stage"] = dict(delta=previous["delta"], manifest_sha256=common.sha256(FIRST / "manifest.json"),
                               runs_sha256=common.sha256(FIRST / "runs.json"), audit_sha256=common.sha256(FIRST / "audit.json"))
    data["protocol"] = {
        **data["protocol"],
        "description": "官方200任务/8机/K25/容量2/原点/纯直送；共享首阶段一次已审计的同种子240秒基线，本阶段只新增一次周期候选240秒。",
        "time_boundary": data["protocol"].get("time_boundary", "")
                         + " 基线的路线、精确原始耗时与全部记录原样复用，不再运行；周期候选的全部动作从原总预算内支出。本段按shared_baseline解释本阶段，原manifest继承的首阶段description仅作历史原始证据。",
    }
    for name in ("execution_notes.json", "provenance.json", "README.md"):
        path = directory / name
        if path.exists():
            data["context"][name] = common.load(path) if path.suffix == ".json" else path.read_text(encoding="utf-8")
            data["fingerprints"][name] = common.sha256(path)
            if name not in data["evidence_links"]:
                data["evidence_links"].append(name)
    data["context"]["renderer_sha256"] = common.sha256(Path(__file__))
    data["context"]["base_renderer_sha256"] = common.sha256(Path(common.__file__))
    data["context"]["first_renderer_sha256"] = common.sha256(Path(first_report.__file__))
    return data


def report_template() -> str:
    html = first_report.report_template()
    replacements = {
        "纯直送 · 加权算法改进配对报告": "纯直送 · 周期请求交换改进报告",
        "完整请求交换能进一步改善直送吗？": "把完整请求交换放进搜索过程",
        "官方 200 项任务，以当前加权方案为基线，新候选加入完整请求原位交换与已有惰性修复。两方案使用同一目标、相同新种子，各 240 秒；先看按时完成、航程与实际耗时，再解释方法收益。": "官方 200 项任务，沿用同一加权目标与种子 2026101461。基线完整复用上一阶段的一次 240 秒实测；本阶段新增一个 240 秒候选，每 120 次完整修复轮转一对路线，进行有界完整请求交换。先看题目指标，再查看全部三条有效记录。",
        "当前加权基线（虚线轨迹）": "共享加权基线（虚线轨迹）",
        "整任务互换＋惰性修复（实线轨迹）": "周期互换＋末端互换＋惰性修复（实线轨迹）",
        "这是本实例一次配对观察，不给出显著性或跨种子稳定性结论。": "两个候选共用同一条基线记录，不是两个独立配对样本；不作显著性或跨种子稳定性结论。",
        "结果反映整任务交换和惰性修复的组合效果。本次没有单独消融，不能把终点差异全部归因于某一步，也不能把更多迭代直接等同于更好考核成绩。": "周期互换、末端互换与惰性修复组成完整候选。每次周期动作后的解仍进入原ALNS接受与奖励；后续轨迹会自然改变，不是固定迭代决策等价的缓存替换。没有逐组件消融。",
        "共执行3次优化器：1次记录失败＋2次正式有效结果": "本次算法改进探索共执行4次优化器：1次记录失败＋3条有效结果；二阶段复用基线，不重复计数",
        "随后按相同种子与配置重新完整跑两臂。": "随后按相同种子与配置完成首轮两臂，本阶段只新增一次周期候选。",
        "首次记录故障与完整配对重跑说明": "记录故障、共享基线与实际运行次数",
        "候选仅新增整任务交换与惰性修复": "候选包含周期互换、末端互换与惰性修复；加权参数保持",
        "末端交换与修复工作量": "末端动作与修复工作量",
        "两方案共享已有末端强化预留，至多 0.1 秒；候选在剩余时间内增加完整请求原位交换。下表给出实际调用、精确候选与接受情况，不能把主循环迭代差异单独解释为交换收益。": "两方案的原末端预留均至多 0.1 秒；候选保留首轮末端互换和惰性修复。周期动作的耗时已包含在原 240 秒总预算内，并在上一节单列；不为新增动作延长预算。",
        "没有额外执行说明。": "执行与共享基线说明见本目录 provenance.json 和 manifest.json。",
        "按时表现改善，航程或逾期幅度的变化仍需结合具体代价判断。": "按时表现改善，但航程或总逾期幅度增加；这是同一加权目标下的取舍，不能声称所有指标同时改善。",
        ".venv/bin/python algorithm/experiments/render_weighted_method_report.py": ".venv/bin/python algorithm/experiments/render_weighted_periodic_report.py",
        "单实例 / 单种子 / 两臂对照": "单实例 / 单种子 / 共享基线与两个候选",
        "两次结果均通过独立审计。": "本阶段两个记录均通过独立审计，其中基线是前阶段同一次实测的完整复制。",
        "加权基线": "共享基线",
    }
    for old, new in replacements.items():
        if old not in html:
            raise ValueError("首轮报告模板变化，需检查周期报告文案：" + old)
        html = html.replace(old, new)
    html = html.replace("共享共享基线", "共享加权基线")
    html = html.replace("+'\\n\\n'+renderCommand", "+(Array.isArray(commands)&&commands.some(c=>c.includes('render_weighted_periodic_report'))?'':'\\n\\n'+renderCommand)")
    html = html.replace('<section aria-labelledby="method-heading">', STAGES_MARKUP + '<section aria-labelledby="method-heading">')
    marker = "const execution=D.context['execution_notes.json'];"
    html = html.replace(marker, STAGES_JS + "\n" + marker)
    html = html.replace("function draw(){", "function draw(){drawTrials();")
    html = html.replace("observer.observe($('curves'));", "observer.observe($('curves'));observer.observe($('trial-plots'));")
    html = html.replace("</style></head>", ".trial-plots{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px}@media(max-width:900px){.trial-plots{grid-template-columns:1fr}}\n</style></head>")
    return html


STAGES_MARKUP = '''<section class="separator" aria-labelledby="stages-heading"><h2 id="stages-heading">两阶段探索：三条有效记录，共享一次基线</h2>
<p id="stages-note"></p><div class="trial-plots" id="trial-plots"></div><div class="table-wrap"><table id="trials-table"></table></div>
<p class="caption">首轮候选只增加末端互换与惰性修复；周期候选在其上增加修复后的有界交换。本阶段没有重跑基线、换种子或把失败孤立解择优加入。全部退化与取舍均保留。</p>
<h3>周期动作：候选内的真实调用记录</h3><div class="table-wrap"><table id="periodic-table"></table></div>
<p class="caption" id="periodic-note"></p><details><summary>逐次路线对、动作前后评分与计时</summary><pre id="periodic-raw"></pre></details></section>
'''

STAGES_JS = r'''
$('outcome').textContent+=` 相对共享基线：J ${signed(delta.common_weighted_score/rows[0].common_weighted_score*100)}%，航程 ${signed(delta.distance_km/rows[0].distance_km*100)}%，总逾期分钟 ${signed(delta.total_lateness_min/rows[0].total_lateness_min*100)}%。`;
const trials=D.trials,trialLabels=['共享基线','首轮末端组合','周期组合'],trialKeys=['on_time_count','distance_km','common_weighted_score'];
const trialColors=['var(--base)','var(--component2)','var(--weighted)'];
const firstChange=D.first_stage.delta;
$('stages-note').textContent=`首轮末端组合相对共享基线：按时数 ${signed(firstChange.on_time_count,0)} 项，航程 ${signed(firstChange.distance_km)} km，J ${signed(firstChange.common_weighted_score,6)}（${signed(firstChange.common_weighted_score/trials[0].common_weighted_score*100,4)}%）。改善幅度有限，因此继续测试周期邻域。下列三条是本次探索全部有效优化记录；两次对照共用同一个基线，不能作为独立重复试验。含首次记录故障，共4次优化器调用。`;
for(const k of trialKeys){const div=document.createElement('div');div.className='plot';div.dataset.field=k;div.innerHTML=`<h3>${names[k]} <span class="caption">${direction(k)>0?'↑ 越高越好':'↓ 越低越好'}</span></h3><svg class="chart" role="img"></svg>`;$('trial-plots').append(div);}
function drawTrials(){document.querySelectorAll('#trial-plots .plot').forEach(div=>{const k=div.dataset.field,svg=div.querySelector('svg'),w=Math.max(260,div.clientWidth),left=4,right=w-6,width=right-left,values=trials.map(r=>r[k]),max=k==='on_time_count'?N:Math.max(...values,1e-12)*1.07;svg.setAttribute('viewBox',`0 0 ${w} 218`);svg.setAttribute('aria-label',names[k]+'三条独特有效记录：'+values.map((v,i)=>trialLabels[i]+' '+fmt(v,k==='common_weighted_score'?6:2)+' '+units[k]).join('；'));let marks='<title>'+esc(names[k])+'：共享基线与两个候选</title>';values.forEach((v,i)=>{const y=22+i*58;marks+=text(left,y,trialLabels[i])+text(right,y,fmt(v,k==='common_weighted_score'?6:k==='on_time_count'?0:2)+' '+units[k],'end')+`<rect x="${left}" y="${y+9}" width="${width}" height="17" fill="var(--soft)"/><rect x="${left}" y="${y+9}" width="${v/max*width}" height="17" fill="${trialColors[i]}"/>`;});marks+=line(left,185,right,185)+text(left,207,'0','start','class="axis-text"')+text(right,207,fmt(max,k==='common_weighted_score'?3:0)+' '+units[k],'end','class="axis-text"');svg.innerHTML=marks;});}
const allFields=['on_time_count','on_time_rate','late_count','distance_km','total_lateness_min','end_to_end_seconds','common_weighted_score'];
$('trials-table').innerHTML='<caption>三条有效终点；共享基线只列一次，各单位保持独立。</caption><thead><tr><th>指标</th>'+trialLabels.map(l=>'<th>'+l+'</th>').join('')+'</tr></thead><tbody>'+allFields.map(k=>'<tr><td>'+names[k]+'</td>'+trials.map(r=>'<td class="nowrap">'+fmt(metricValue(r,k),k==='common_weighted_score'?6:2)+' '+units[k]+'</td>').join('')+'</tr>').join('')+'</tbody>';
const periodic=rows[1].periodic_task_exchange,events=periodic.events||[],maxPass=events.length?Math.max(...events.map(e=>e.runtime_seconds)):0,params=D.manifest.periodic_parameters;
const periodicFields=[['触发间隔',params.interval,'次完整修复'],['每次精评上限',params.max_candidates,'对'],['每次预算参数',params.pair_budget_seconds*1000,'ms'],['完整修复调用',periodic.repair_calls,'次'],['周期动作调用',periodic.passes,'次'],['精确交换检查',periodic.probes,'对'],['动作内改善',periodic.improvements,'次'],['完整规范检查',periodic.canonical_checks,'次'],['周期动作累计实测',periodic.runtime_seconds,'s'],['单次最大实测',maxPass*1000,'ms']];
$('periodic-table').innerHTML='<caption>周期动作耗时是候选240秒总计时的一部分，不额外相加为算法总耗时。</caption><thead><tr><th>诊断</th><th>候选实测/配置</th></tr></thead><tbody>'+periodicFields.map(([a,v,u])=>'<tr><td>'+a+'</td><td>'+fmt(v,u==='s'||u==='ms'?5:0)+' '+u+'</td></tr>').join('')+'</tbody>';
$('periodic-note').textContent='轮转路线对，每次最多16个精确候选。8ms是单次动作预算参数，最大实测调用耗时另列。每个保留checkpoint必须在截止前完成整体规范核验；动作内改善计数不代表每次都通过后续ALNS接受或更新全局best。周期动作不抽取随机数，但改变修复结果会改变后续搜索轨迹。';
$('periodic-raw').textContent=JSON.stringify(periodic,null,2);
'''


def render_report(directory: Path) -> Path:
    directory = directory.resolve()
    first_report.prepare_literature(directory)
    data = build_data(directory)
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    encoded = encoded.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    output = directory / "index.html"
    output.write_text(report_template().replace("__DATA__", encoded), encoding="utf-8")
    return output


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    print(render_report(args.output_dir))


if __name__ == "__main__":
    main()
