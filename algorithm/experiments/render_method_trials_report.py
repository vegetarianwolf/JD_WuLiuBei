"""Offline paired dashboard for the new bounded method trials (no solving)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "algorithm/results/method_trials_20261013"


def build_data(directory: Path) -> dict:
    read = lambda name: json.loads((directory / name).read_text())
    manifest, runs, decisions, audit = (read(name) for name in
                                       ("manifest.json", "runs.json", "decisions.json", "validation.json"))
    if not audit.get("passed"):
        raise ValueError("报告仅接受独立审计通过的结果")
    for name in ("manifest", "runs", "decisions"):
        actual = hashlib.sha256((directory / f"{name}.json").read_bytes()).hexdigest()
        if audit.get(f"{name}_sha256") != actual:
            raise ValueError(f"审计过期：{name}.json")
    for row in runs:
        path = (directory / row["solution_file"]).resolve()
        if not path.is_relative_to(directory.resolve()) or hashlib.sha256(path.read_bytes()).hexdigest() != row["solution_sha256"]:
            raise ValueError("解文件指纹不匹配")
    literature_path = directory / "literature/literature.json"
    literature = json.loads(literature_path.read_text()) if literature_path.exists() else {}
    adopted = directory / "production.json"
    production = json.loads(adopted.read_text()) if adopted.exists() else None
    if production and production.get("validation_sha256") != hashlib.sha256((directory / "validation.json").read_bytes()).hexdigest():
        raise ValueError("采用记录与本批审计不一致")
    mechanism = []
    for row in runs:
        if row["variant"] == "memo":
            metadata = read(row["solution_file"])["metadata"]
            mechanism.append({key: row[key] for key in ("stage", "case_id", "seed")} | {
                "hits": metadata["direct_option_cache_hits"],
                "misses": metadata["direct_option_cache_misses"],
                "size": metadata["direct_option_cache_size"],
                "evictions": metadata["direct_option_cache_evictions"],
            })
    return dict(manifest=manifest, runs=runs, decisions=decisions, audit=audit,
                literature=literature, mechanism=mechanism, production=production,
                parent_link="../index.html" if manifest.get("kind") != "method_trials" else None,
                followup_link=("iteration3/index.html" if (directory / "iteration3/manifest.json").exists()
                               else "iteration2/index.html" if (directory / "iteration2/manifest.json").exists() else None))


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>无人机调度 · 小预算优化对比</title>
<style>
:root{color-scheme:light dark;--background:light-dark(#fafbfc,#16191f);--foreground:light-dark(#202832,#e6eaf1);--muted-foreground:light-dark(#556270,#b4bfcc);--border:light-dark(#dde3e9,#3a414c);--card:light-dark(#fff,#212630);--viz-series-1:light-dark(#177b91,#6fcad8);--viz-series-2:light-dark(#7755b0,#b49ee0);--viz-series-3:light-dark(#a8531d,#edb582);--font-size-base:15px}
*{box-sizing:border-box}body{margin:0;background:var(--background);color:var(--foreground);font:400 var(--font-size-base)/1.6 system-ui,-apple-system,"PingFang SC",sans-serif}main{max-width:1200px;margin:auto;padding:32px 24px}h1,h2,h3{font-weight:500;line-height:1.35}h1{font-size:28px;margin:8px 0}h2{font-size:19px;margin:24px 0 12px}h3{font-size:16px;margin:0 0 8px}p{margin:8px 0}a{color:var(--viz-series-1)}.muted{color:var(--muted-foreground)}.eyebrow{font-size:13px;letter-spacing:.08em}.top{display:flex;justify-content:space-between;gap:20px;flex-wrap:wrap}.status{max-width:420px}.stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px;margin:24px 0}.stat{padding:12px 16px;background:var(--card);border-radius:8px}.number{font-size:25px;font-variant-numeric:tabular-nums}.controls{display:flex;flex-wrap:wrap;gap:16px;margin:16px 0}.controls label{display:grid;gap:4px;font-size:13px}select{font:inherit;color:inherit;background:var(--card);border:1px solid var(--border);border-radius:5px;padding:7px 12px;min-height:38px}.legend{display:flex;gap:18px;flex-wrap:wrap}.swatch{display:inline-block;width:16px;height:8px;margin-right:6px}.plots{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:24px}.plot{min-width:0}svg{width:100%;display:block;overflow:visible}svg text{fill:var(--foreground);font:12px system-ui,sans-serif}.plot figcaption{font-size:13px;color:var(--muted-foreground)}figure{margin:0}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--border);white-space:nowrap}th{font-weight:500}td.num{text-align:right}.pair-summary{padding:12px 0;font-variant-numeric:tabular-nums}.foot{margin-top:32px;border-top:1px solid var(--border);padding-top:16px;font-size:13px}.foot details{margin:12px 0}.foot summary{cursor:pointer}ul{padding-left:22px}.callout{border-left:3px solid var(--viz-series-1);padding-left:12px}.source{word-break:break-all}button{font:inherit} @media(max-width:700px){main{padding:22px 16px}h1{font-size:23px}.plots{grid-template-columns:1fr}.stats{gap:8px}.stat{padding:10px}.number{font-size:21px}}@media(max-width:400px){.stats{grid-template-columns:1fr}.controls label{width:100%}select{width:100%;font-size:16px}}
</style></head><body><main id="method-dashboard">
<div class="top"><header><div class="eyebrow muted">2026-10-13 · 原题官方数据 · 新批次配对实验</div><h1>无人机调度：小预算优化对比</h1><p class="muted">相同数据、初解、种子和时限；与本轮开始时的 main 比较。</p></header><div class="status callout"><strong id="outcome"></strong><p id="risk" class="muted"></p></div></div>
<div class="stats"><div class="stat"><div class="muted">完整运行 / 独立合法核验</div><div class="number" id="valid-count"></div></div><div class="stat"><div class="muted">累计实际求解</div><div class="number" id="spent"></div></div><div class="stat"><div class="muted">本轮最长单次预算</div><div class="number" id="max-budget"></div></div></div>
<div class="controls"><label>实验阶段<select id="stage"></select></label><label>实例<select id="case"></select></label><label>候选方法<select id="variant"></select></label></div>
<div class="legend" id="legend"></div><p id="pair-summary" class="pair-summary" aria-live="polite"></p>
<div class="plots"><figure class="plot"><h3>按时完成率 ↑</h3><svg id="ontime" role="img"></svg><figcaption>第一目标：尽量保障更多任务按时完成。</figcaption></figure><figure class="plot"><h3>总航程 ↓</h3><svg id="distance" role="img"></svg><figcaption>不计末单返航；不能以减少里程抵消逾期任务增加。</figcaption></figure><figure class="plot"><h3>总逾期时间 ↓</h3><svg id="lateness" role="img"></svg><figcaption>项目采用的次级目标；原题没有直接指定这项优先级。</figcaption></figure><figure class="plot"><h3>端到端求解时间 ↓</h3><svg id="runtime" role="img"></svg><figcaption>含数据读取、构造、搜索、完整核验和解写出。</figcaption></figure></div>
<h2>每个种子的完整对比</h2><div class="table-wrap"><table><thead><tr><th>种子</th><th>版本</th><th>逾期任务</th><th>按时率</th><th>逾期 min</th><th>航程 km</th><th>耗时 s</th><th>迭代</th><th>配对判定</th><th>完整解</th></tr></thead><tbody id="rows"></tbody></table></div>
<h2>第一目标的搜索轨迹</h2><div class="legend" id="trajectory-legend"></div><figure><svg id="trajectory" role="img"></svg><figcaption class="muted">每种子同色实线为基线、虚线为候选；只连该次完整核验的 incumbent。横轴包括初解构造时间。</figcaption></figure>
<div class="foot"><p id="protocol"></p><details><summary>方法、门槛与版本边界</summary><div id="methods"></div><p>筛选与验证种子分别预先固定。用户选择探索门槛：30 秒集群三种子多数获胜可晋级，即使有退化也完成全部种子。未晋级候选不扩大测试；不补造未运行成绩，不重跑择优。固定迭代阶段用于核对缓存是否保持搜索决策，不用于质量判胜。</p><p>25 任务单机、200 任务 / 8 机；载荷 2、每机最多 25 任务、速度 0.9 km/min、原点起飞、全部任务完成、末单不返航。词典序：逾期数 → 总逾期分钟 → 总航程。</p><p>原题两任务示例、模拟数据和分钟级规模实验已有独立记录，本轮没有重跑；本轮不能证明最优性、分钟级效果或跨实例稳定性。单机官方前 25 任务用于兼容检查；墙钟成绩受机器负载影响。</p><p class="source" id="fingerprint"></p><p><a href="manifest.json">实验协议</a> · <a href="runs.json">逐次原始记录</a> · <a href="decisions.json">阶段决策</a> · <a href="validation.json">独立复算</a> · <a href="README.md">复现说明</a></p></details><details><summary>文献与可复查检索</summary><div id="literature"></div><p><a href="literature/literature.json">检索与来源记录</a> · <a href="literature/reading_notes.md">方法阅读笔记</a></p></details></div>
</main><script id="report-data" type="application/json">__DATA__</script>
<script>
(() => {
const root=document.getElementById('method-dashboard'),D=JSON.parse(document.getElementById('report-data').textContent),$=id=>root.querySelector('#'+id);
const labels={baseline:'当前 main 基线',memo:'插入候选复用',cooling:'预算进度降温',strings:'跨路线相邻串移除',polish:'相邻访问微调'},stages={equivalence:'固定 100 轮等价检查',screen:'10 秒小范围筛选',medium:'30 秒扩大验证'},cases={single25:'单机 · 25 任务',fleet200:'集群 · 200 任务 / 8 机'};
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),fmt=(n,d=2)=>Number(n).toFixed(d),score=r=>[r.late_count,r.total_lateness_min,r.distance_km],cmp=(a,b)=>{const x=score(a),y=score(b);for(let i=0;i<3;i++){if(x[i]!==y[i])return x[i]<y[i]?-1:1}return 0};
const chosen=D.decisions.slice().reverse().find(x=>x.selected_variant)?.selected_variant;
$('outcome').textContent=chosen?`${labels[chosen]}通过本轮探索门槛`:'本轮未达到默认替换门槛';
$('risk').textContent=chosen?'仅代表本实例与短预算观测；完整退化记录见下方。默认纳入情况见实验说明。':'保留已有默认配置，候选作为研究开关。短测结论不外推到分钟级。';
if(D.production?.default_enabled)$('risk').textContent='已设为默认；保留初筛退化。仅支持本实例短预算观测，分钟级与跨实例表现未知。';
$('valid-count').textContent=`${D.runs.length} / ${D.runs.filter(r=>r.valid&&r.budget_compliant).length}`;
$('spent').textContent=`${fmt(D.runs.reduce((s,r)=>s+r.end_to_end_seconds,0)/60)} 分钟`;
$('max-budget').textContent=`${Math.max(...D.runs.map(r=>r.budget_seconds))} 秒`;
$('protocol').textContent='逐种子按完整三级得分判胜，图表不把平均值当作最优；所有结果只与本轮同预算 main 比较。';
if(D.parent_link)$('protocol').innerHTML+=' <a href="'+esc(D.parent_link)+'">查看首批缓存与降温实验</a>。两批成绩分别展示。';
if(D.followup_link)$('protocol').innerHTML+=' <a href="'+esc(D.followup_link)+'">查看继续迭代的新方法</a>。两批成绩分别展示。';
$('fingerprint').textContent=`基线提交 ${D.manifest.baseline_commit}；输入 SHA-256 ${D.manifest.input_sha256}。求解器与执行脚本归档见 sources/。`;
$('methods').innerHTML='<p><b>插入候选复用</b>：在一次搜索内为相同任务、路线和完整插入参数复用计算结果；有界缓存减少修复重复工作，固定轮数须保持全部搜索决策。</p><p><b>预算进度降温</b>：温度端点保持 0.03 和 0.0005，按剩余搜索时间几何冷却。只作用于多机 DIRECT，墙钟改法为本项目推演；完整 best-so-far 评分和判优保持。</p>';
const memoFleet=D.mechanism.filter(r=>r.stage==='screen'&&r.case_id==='fleet200'),hits=memoFleet.reduce((s,r)=>s+r.hits,0),misses=memoFleet.reduce((s,r)=>s+r.misses,0);
$('methods').innerHTML+=`<p>10 秒集群筛选缓存命中率：${hits+misses?fmt(100*hits/(hits+misses),2)+'%':'暂无'}（仅对求解级缓存查询计数，修复内部缓存命中不进入分母）；最多 20,000 条。缓存开销是否值得由同预算成绩决定。</p>`;
if(D.runs.some(r=>r.variant==='strings'))$('methods').innerHTML='<p><b>跨路线相邻串移除</b>：在两条空间邻近的路线中均衡移除连续任务串，每个任务同时移除取送两个访问点，打开满载集群的跨机任务槽。保留原有破坏算子与修复、接受、完整历史最优评价。新算子独立适应权重，仅作用于多机 DIRECT。</p><p>这是对 SISRs 相邻串移除思想的项目适配，没有实现完整 SISRs 或随机跳过插入，不移植原论文性能结论。</p>';
if(D.runs.some(r=>r.variant==='polish'))$('methods').innerHTML='<p><b>相邻访问微调</b>：每 40 轮只检查一条 DIRECT 路线、最多 49 对相邻访问点，不额外消耗随机抽样。不同任务的两点交换后维持先取后送与容量约束，第一项严格路线改善即停止；最终历史最优仍须完整评价。</p><p>仅作用于多机 DIRECT。这是本项目设计的有界小邻域，不是完整 VND，也不是 Curtois 等原文的跨路线完整任务对交换。</p>';
const papers=Array.isArray(D.literature)?D.literature:(D.literature.sources||D.literature.papers||D.literature.results||[]);
$('literature').innerHTML=papers.map(p=>`<p><a href="${esc(p.url||p.doi_url||(p.doi?'https://doi.org/'+p.doi:'#'))}">${esc(p.title||p.name||'文献')}</a> (${esc(p.year||'')})<br><span class="muted">${esc((p.authors||[]).join(', '))} · ${esc(p.evidence_level||p.relevance||p.note||'')}</span></p>`).join('')||'<p>详细引用与检索状态见文献记录。</p>';
function options(el,values,names){const prev=el.value;el.innerHTML=values.map(v=>`<option value="${esc(v)}">${esc(names[v]||v)}</option>`).join('');if(values.includes(prev))el.value=prev}
options($('stage'),[...new Set(D.runs.map(r=>r.stage))],stages);$('stage').value=D.runs.some(r=>r.stage==='medium')?'medium':'screen';
let matched=[];
function makeSVG(id,w,h,content,title){const el=$(id);el.setAttribute('viewBox',`0 0 ${w} ${h}`);el.innerHTML=`<title>${esc(title)}</title>${content}`}
function bars(id,key,unit,percent=false){const el=$(id),w=Math.max(280,el.parentElement.clientWidth),left=90,right=65,top=24,rowH=72,h=top+rowH*matched.length+38,inner=w-left-right;
 const vals=matched.flatMap(p=>[p[0][key],p[1][key]]),budget=key==='end_to_end_seconds'?Math.max(...matched.map(p=>p[0].budget_seconds)):0,max=percent?1:Math.max(1,...vals,budget)*1.04,x=n=>left+n/max*inner;
 let out='';for(let i=0;i<4;i++){const v=max*i/3,xx=x(v);out+=`<line x1="${xx}" x2="${xx}" y1="${top-5}" y2="${h-35}" stroke="var(--border)"/><text x="${xx}" y="${h-17}" text-anchor="middle">${percent?fmt(v*100,0):fmt(v,0)}</text>`}
 matched.forEach((p,i)=>{out+=`<text x="${left-8}" y="${top+i*rowH+28}" text-anchor="end">${p[0].seed}</text>`;p.forEach((r,j)=>{const y=top+i*rowH+j*25,val=percent?fmt(r[key]*100,1)+'%':fmt(r[key]);out+=`<rect x="${left}" y="${y}" width="${Math.max(0,x(r[key])-left)}" height="17" fill="var(--viz-series-${j+1})"><title>${esc(labels[r.variant])}: ${val} ${esc(unit)}</title></rect><text x="${budget?Math.max(x(r[key])+5,x(budget)+6):x(r[key])+5}" y="${y+13}">${val}</text>`})});
 if(budget)out+=`<line x1="${x(budget)}" x2="${x(budget)}" y1="${top-5}" y2="${h-35}" stroke="var(--foreground)" stroke-dasharray="3 3"/><text x="${x(budget)}" y="${top-10}" text-anchor="end">预算 ${budget}s</text>`;
 out+=`<text class="axis-title" data-axis="x" x="${w-8}" y="${h-2}" text-anchor="end">${esc(unit)}</text>`;makeSVG(id,w,h,out,key+' 同种子基线与候选对比');}
function trajectory(){const el=$('trajectory'),w=Math.max(280,el.parentElement.clientWidth),h=270,l=65,r=24,t=22,b=45,points=matched.flatMap(p=>p.flatMap(q=>q.best_trajectory||[]));if(!points.length){makeSVG('trajectory',w,80,'<text x="10" y="30">没有完整轨迹</text>','无轨迹');return}
 const xmax=Math.max(1,...matched.flatMap(p=>p.map(q=>q.end_to_end_seconds))),ymin=Math.min(...points.map(p=>p[2]))-1,ymax=Math.max(...points.map(p=>p[2]))+1,x=v=>l+v/xmax*(w-l-r),y=v=>h-b-(v-ymin)/(ymax-ymin)*(h-b-t);
 let out='';for(let i=0;i<4;i++){let yy=y(ymin+(ymax-ymin)*i/3);out+=`<line x1="${l}" x2="${w-r}" y1="${yy}" y2="${yy}" stroke="var(--border)"/><text x="${l-8}" y="${yy+4}" text-anchor="end">${fmt(ymin+(ymax-ymin)*i/3,0)}</text>`;const xx=x(xmax*i/3);out+=`<text x="${xx}" y="${h-b+22}" text-anchor="middle">${fmt(xmax*i/3,0)}</text>`}
 matched.forEach((pair,i)=>pair.forEach((row,j)=>{const pts=row.best_trajectory;let path=`M ${x(pts[0][1])} ${y(pts[0][2])}`;pts.slice(1).forEach(p=>{path+=` H ${x(p[1])} V ${y(p[2])}`});path+=` H ${x(row.end_to_end_seconds)}`;out+=`<path d="${path}" fill="none" stroke="var(--viz-series-${i%3+1})" stroke-width="1.8" ${j?'stroke-dasharray="5 3"':''}><title>${row.seed} ${esc(labels[row.variant])}</title></path>`}));
 out+=`<text class="axis-title" data-axis="x" x="${w-r}" y="${h-4}" text-anchor="end">端到端秒</text><text class="axis-title" data-axis="y" x="8" y="14">逾期任务数</text>`;makeSVG('trajectory',w,h,out,'各随机种子的逾期任务数收敛轨迹');}
function draw(){const stage=$('stage').value,caseId=$('case').value,v=$('variant').value,rows=D.runs.filter(r=>r.stage===stage&&r.case_id===caseId);matched=[];for(const seed of [...new Set(rows.map(r=>r.seed))].sort()){const b=rows.find(r=>r.seed===seed&&r.variant==='baseline'),c=rows.find(r=>r.seed===seed&&r.variant===v);if(b&&c&&b.valid&&c.valid&&b.budget_compliant&&c.budget_compliant)matched.push([b,c])}
 const counts=[0,0,0];matched.forEach(([b,c])=>counts[cmp(c,b)+1]++);$('legend').innerHTML=`<span><i class="swatch" style="background:var(--viz-series-1)"></i>${labels.baseline}</span><span><i class="swatch" style="background:var(--viz-series-2)"></i>${labels[v]}</span>`;
 $('pair-summary').textContent=stage==='equivalence'?`${matched.length} 个固定轮数配对 · 只判搜索决策等价，不判墙钟质量改善`:`候选 ${counts[0]} 胜 / ${counts[1]} 平 / ${counts[2]} 负 · ${matched.length} 个完整配对`;
 $('trajectory-legend').innerHTML=matched.map((p,i)=>`<span><i class="swatch" style="background:var(--viz-series-${i%3+1})"></i>种子 ${p[0].seed}</span>`).join('');
 bars('ontime','on_time_rate','%',true);bars('distance','distance_km','km');bars('lateness','total_lateness_min','min');bars('runtime','end_to_end_seconds','s');trajectory();
 $('rows').innerHTML=matched.map(([b,c])=>[b,c].map((row,j)=>`<tr><td>${row.seed}</td><td>${esc(labels[row.variant])}</td><td class="num">${row.late_count}</td><td class="num">${fmt(row.on_time_rate*100,1)}%</td><td class="num">${fmt(row.total_lateness_min)}</td><td class="num">${fmt(row.distance_km)}</td><td class="num">${fmt(row.end_to_end_seconds)}</td><td class="num">${row.iterations}</td><td>${j?(stage==='equivalence'?'见等价审计':['胜','平','负'][cmp(c,b)+1]):'基线'}</td><td><a href="${esc(row.solution_file)}">JSON</a></td></tr>`).join('')).join('');}
function choose(){const stage=$('stage').value,first=!$('case').value;options($('case'),[...new Set(D.runs.filter(r=>r.stage===stage).map(r=>r.case_id))],cases);if(first&&[...$('case').options].some(o=>o.value==='fleet200'))$('case').value='fleet200';variants()}
function variants(){options($('variant'),[...new Set(D.runs.filter(r=>r.stage===$('stage').value&&r.case_id===$('case').value&&r.variant!=='baseline').map(r=>r.variant))],labels);draw()}
$('stage').addEventListener('change',choose);$('case').addEventListener('change',variants);$('variant').addEventListener('change',draw);choose();let timer;new ResizeObserver(()=>{clearTimeout(timer);timer=setTimeout(draw,50)}).observe(root);
})();
</script></body></html>'''


def render(directory: Path) -> Path:
    data = build_data(directory.resolve())
    # Escape HTML script terminators while keeping the original JSON value.
    serialized = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("<", "\\u003c")
    target = directory / "index.html"
    target.write_text(TEMPLATE.replace("__DATA__", serialized), encoding="utf-8")
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(render(args.output_dir.resolve()))


if __name__ == "__main__":
    main()
