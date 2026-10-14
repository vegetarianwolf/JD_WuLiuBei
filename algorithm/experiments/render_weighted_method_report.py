"""Render the audited weighted-baseline versus request-exchange/lazy trial.

Both arms use the same frozen weighted objective.  This renderer never solves
or probes a neighbourhood; mechanism diagnostics are displayed separately.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from . import render_weighted_direct_report as common
except ImportError:
    import render_weighted_direct_report as common

ROOT = common.ROOT
OUTPUT = ROOT / "algorithm/results/weighted_method_improvement_20261014"
NOTES = ROOT / "papers/notes/weighted_improvement_20261014"


def prepare_literature(directory: Path) -> None:
    """Map verified literature records to the report's display schema."""
    source = common.load(NOTES / "provenance.json")
    papers = []
    details = {
        "10.1016/j.trb.2003.07.001": (
            "取得原作者稿，实读 §3.2–3.3；Crossref 精确题录核验。",
            "请求 × 路线的增量插入成本矩阵与快速取送可行性检查。候选中的 deadline/slack 静态短名单是项目工程适配，不宣称该文提出了本实现。"),
        "10.1016/j.cor.2021.105643": (
            "原始 arXiv 全文 §3；Crossref 精确核验正式期刊年 2022。",
            "跨路线交换邻域提供动机。本次实现完整取送请求原位交换，保持两条载荷符号序列；它与文中允许重插位置的 SWAP* 不同，容量不变性来自本题适配证明。"),
        "10.1016/j.cor.2024.106562": (
            "精确题录、Scholar 方法片段和出版社摘要/预览；已接收稿 HTTP403，未读全文。",
            "选择性检查受改变区域的计算组织。百万规模 CVRP 性能不能外推到本题 200 单；本轮没有启用该文完整算法。"),
        "2505.02158": (
            "原始 arXiv 全文 §4.1–4.3；当前官方记录为 2025-05-04 v1。",
            "请求相关破坏、随机跳过插入和延迟接受是后续方向。本次保持原模拟退火，未叠加这些机制。"),
        "10.19942/j.issn.2096-5915.2025.06.51": (
            "实际浏览知网托管期刊官方题录和中英文摘要；未取全文。",
            "ALNS 的禁忌列表与重初始化可作为后续独立实验；无人车同步策略收益不视为本题直送收益。"),
        "10.19678/j.issn.1000-3428.00252242": (
            "期刊官方作者、2025-07-14 网络发布日期及摘要；未取全文，不补造卷页。",
            "将客户顺序与服务工具分开编码，为分别改善任务分配和路线内顺序提供背景；不能凭摘要称其已实现本次完整请求交换。"),
    }
    for record in source["selected_papers"]:
        doi = record.get("doi", record.get("DOI"))
        arxiv = record.get("arxiv_id")
        title = record.get("title", "")
        if isinstance(title, list):
            title = title[0]
        authors = record.get("authors")
        if authors is None:
            authors = [" ".join(filter(None, (a.get("given"), a.get("family"))))
                       for a in record.get("author", [])]
        year = record.get("year", record.get("published", {}).get("date-parts", [[None]])[0][0])
        if year is None and record.get("online_date"):
            year = int(record["online_date"][:4])
        evidence, support = details[doi or arxiv]
        if doi == "10.19942/j.issn.2096-5915.2025.06.51":
            url = "https://umst.cbpt.cnki.net/portal/journal/portal/client/paper/62a603302c3c0e2bb40ce2231dd9ff84"
        elif doi == "10.19678/j.issn.1000-3428.00252242":
            url = "https://www.ecice06.com/CN/10.19678/j.issn.1000-3428.00252242"
        else:
            url = "https://doi.org/" + doi if doi else "https://arxiv.org/abs/" + arxiv
        papers.append(dict(title=title, doi=doi, authors=authors, year=year,
                           url=url, evidence_level=evidence, method_support=support))
    summary = (
        "2026-10-14 实际浏览 Google Scholar 三个首屏，再以 Crossref 和原论文核验；新增4篇国际方法文献及2篇中文官方摘要。"
        "知网首页已访问并提交主题词，但结果页未产生；高级检索受浏览器限制。随后实际读到知网托管期刊的公开题录和摘要。"
        "这些访问状态分别记录，未将受阻检索说成空结果或完整知网综述。完整阅读范围与迁移边界见 literature.md。"
    )
    software = {**source["software_citation"], "evidence_level": "本轮重新核验官方当前记录，最新 v2；软件来源引用。"}
    mapped = dict(summary=summary, papers=papers,
                  skill=dict(name="paper-lookup", software_citation=software),
                  retrieval=source)
    (directory / "literature.json").write_text(json.dumps(mapped, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    notes = (NOTES / "reading_notes.md").read_text(encoding="utf-8")
    notes = notes.replace("[provenance.json](provenance.json)", "[检索记录](literature.json)")
    notes = notes.replace("../global_relay_followup_20261014/reading_notes.md",
                          "../../../papers/notes/global_relay_followup_20261014/reading_notes.md")
    (directory / "literature.md").write_text(notes, encoding="utf-8")


def build_data(directory: Path) -> dict:
    data = common.build_data(directory)
    manifest, rows = data["manifest"], data["rows"]
    if manifest.get("schema_version") != 2:
        raise ValueError("需要本轮 schema 2 的加权方法实验")
    if [r["variant"] for r in rows] != ["weighted_baseline", "weighted_exchange_lazy"]:
        raise ValueError("需要重新实跑的当前加权基线与完整请求交换候选")
    if any(r.get("objective") != "weighted" for r in rows):
        raise ValueError("本次两个方案必须使用相同加权目标")
    base_cfg, new_cfg = (manifest["variants"][r["variant"]]["config"] for r in rows)
    allowed = {"enable_lazy_repair", "enable_terminal_task_exchange"}
    differences = {key for key in base_cfg.keys() | new_cfg.keys()
                   if base_cfg.get(key, False) != new_cfg.get(key, False)}
    if differences != allowed or any(new_cfg.get(key) is not True for key in allowed):
        raise ValueError("本轮候选只允许新增整任务交换与已有惰性修复开关")
    for row in rows:
        terminal = row.get("terminal_polish", {})
        reserved = terminal.get("reserved_seconds")
        if not isinstance(reserved, (float, int)) or not 0 <= reserved <= 0.100000001:
            raise ValueError("末端强化必须共享既有至多 0.1 秒预留")
        row["role"] = "baseline" if row is rows[0] else "candidate"
    execution_path = directory / "execution_notes.json"
    if execution_path.exists():
        execution = common.load(execution_path)
        data["context"]["execution_notes.json"] = execution
        data["fingerprints"]["execution_notes.json"] = common.sha256(execution_path)
        data["evidence_links"].append("execution_notes.json")
        failure = execution.get("recording_failure", {})
        if failure.get("failed_solution_path"):
            failed_path = common.within(directory, failure["failed_solution_path"])
            if common.sha256(failed_path) != failure.get("failed_solution_sha256"):
                raise ValueError("首次记录故障的归档解与说明指纹不一致")
            name = failure["failed_solution_path"]
            data["fingerprints"][name] = failure["failed_solution_sha256"]
            data["evidence_links"].append(name)
    for key, name in (("probe", "swap_probe.json"), ("primitive_probe", "swap_primitive_probe.json")):
        probe_path = directory / "diagnostics" / name
        if probe_path.exists():
            probe = common.load(probe_path)
            if probe.get("formal_run") is not False:
                raise ValueError("保存解邻域探测只能作为非正式机制诊断")
            data[key] = {k: v for k, v in probe.items() if k != "final_routes"}
            relative = "diagnostics/" + name
            data["fingerprints"][relative] = common.sha256(probe_path)
            data["evidence_links"].append(relative)
    data["context"]["renderer_sha256"] = common.sha256(Path(__file__))
    return data


def report_template() -> str:
    html = common.HTML
    replacements = {
        "现有词典序与加权总分": "当前加权基线与整任务互换＋惰性修复",
        "纯直送 · 加权总分与现有方案对照": "纯直送 · 加权算法改进配对报告",
        "目标函数探索": "加权算法改进",
        "加权总分能改善纯直送吗？": "完整请求交换能进一步改善直送吗？",
        "官方 200 项任务，现有方案和加权候选各跑一个相同种子、240 秒。按题目考核直接比较按时完成、逾期、总航程及求解时间，再解释加权目标的取舍。": "官方 200 项任务，以当前加权方案为基线，新候选加入完整请求原位交换与已有惰性修复。两方案使用同一目标、相同新种子，各 240 秒；先看按时完成、航程与实际耗时，再解释方法收益。",
        "现有词典序方案（虚线轨迹）": "当前加权基线（虚线轨迹）",
        "加权总分候选（实线轨迹）": "整任务互换＋惰性修复（实线轨迹）",
        "加权总分怎样交换不同目标": "相同加权目标下的结果分解",
        "现有方案严格依次最小化“逾期数 → 总逾期分钟 → 航程”；候选最小化下面的共同加权分。相同权重也用于评估现有方案，便于看清变化来源。": "两方案都最小化下面同一个 J，权重与尺度沿用上一批已冻结的加权版本。本次比较改变搜索邻域与修复工作量，没有改变指标偏好；共同分数用于解释同一目标的改善。",
        "各方案按自身目标保留的最优解轨迹": "两方案按同一加权目标保留的最优解轨迹",
        "加权轨迹的逾期数、逾期分钟或航程可以上升；词典序轨迹的低级目标也可能随高级目标改善而上升。这里只对各自目标下保留的解作图": "两方案的逾期数、逾期分钟或航程均可能在加权总分下降时上升。这里只对同一固定加权目标下保留的解作图",
        "各自目标的单调性": "共同加权目标的单调性",
        "两方案约0.005秒的耗时差异不能支持速度优劣结论。": "小量计时差异不支持速度优劣结论；搜索迭代计数另列，仅作为计算工作量诊断。",
        "阅读、检索和文字编辑仍继续。": "只继续阅读与文本编辑。",
        "只能回答“这次是否产生了有价值的取舍”": "只能回答“本次方法组合是否改善了当前加权基线”",
        "候选仅作目标适配": "候选仅新增整任务交换与惰性修复",
        "相同输入与当前 main 基础": "相同输入与冻结加权基础",
        "现有词典序": "当前加权基线",
        "加权总分</th>": "改进候选</th>",
        "加权候选": "改进候选",
        "现有方案": "加权基线",
        "本次加权方案": "本次改进候选",
        "候选 − 现有": "候选 − 基线",
        "if(late>0)$('result').classList.add('tradeoff');": "if(late>0||km>0||score>0)$('result').classList.add('tradeoff');",
        "'需继续结合航程、总逾期分钟与实际求解时间判断取舍。'": "score<0?(km>0?'共同加权分下降，但按时表现持平、航程增加；这是加权偏好下的权衡，不能声称题目两个直接指标同时改善。':'共同加权分下降，按时表现持平；改善幅度仍只代表本次种子的观察。'):score>0?'共同加权分也未改善；这次候选没有超越当前加权基线。':'共同加权分相同；需结合航程与逾期幅度判断本次结果。'",
        "来自同一词典序初解": "沿用上一批同一构造初解的冻结尺度",
        "const fields=['on_time_count','late_count','distance_km','end_to_end_seconds'];": "const fields=['on_time_count','distance_km','end_to_end_seconds','late_count'];",
        "当前 main 的 C2-Lex-ALNS，保留已采用的相邻强化和末端强化，按严格词典序比较解。": "上一批实际加权源码冻结快照，本次新种子重新运行。",
        "相同硬约束、算子池和强化配置；候选比较、插入排序、regret、模拟退火损失、best保留、局部与末端强化采用同一加权目标；worst_lex 随评分转换成加权损失。": "相同加权目标、算子池和退火配置；在已有末端预算内增加完整请求原位交换，同时启用已有 deadline/slack 惰性短名单修复。",
        "结果反映整条加权搜索机制的行为。目标尺度改变也会改变退火增量与原温度的相对关系，不能只把差异归因于最终解的排序公式。": "结果反映整任务交换和惰性修复的组合效果。本次没有单独消融，不能把终点差异全部归因于某一步，也不能把更多迭代直接等同于更好考核成绩。",
        "python -m algorithm.experiments.render_weighted_direct_report --output-dir algorithm/results/weighted_direct_single_seed_20261014": ".venv/bin/python algorithm/experiments/render_weighted_method_report.py",
    }
    for old, new in replacements.items():
        if old not in html:
            raise ValueError("旧模板发生变化，需要检查替换文案：" + old)
        html = html.replace(old, new)
    html = html.replace('<section aria-labelledby="literature-heading">', TERMINAL_MARKUP + '<section aria-labelledby="literature-heading">')
    html = html.replace('<p id="audit-intro"></p>', '<p id="audit-intro"></p><p id="recording-note"></p><details><summary>首次记录故障与完整配对重跑说明</summary><pre id="execution-notes"></pre></details>')
    marker = "const literature=D.context['literature.json'];"
    html = html.replace(marker, TERMINAL_JS + "\n" + marker)
    return html


TERMINAL_MARKUP = '''<section class="separator" aria-labelledby="terminal-heading"><h2 id="terminal-heading">末端交换与修复工作量</h2>
<p>两方案共享已有末端强化预留，至多 0.1 秒；候选在剩余时间内增加完整请求原位交换。下表给出实际调用、精确候选与接受情况，不能把主循环迭代差异单独解释为交换收益。</p>
<div class="table-wrap"><table id="terminal-table"></table></div><p class="caption" id="terminal-note"></p>
<details><summary>正式运行的末端诊断与修复计数</summary><pre id="terminal-raw"></pre></details>
<h3>保存解邻域探测：机制诊断，非正式成绩</h3><p id="probe-note"></p>
<p id="primitive-note"></p><p class="caption">这两个探测都从上一批相同的已保存解出发，读取范围与截止规则不同；它们用于检查邻域缺口与已实现动作，不构成两次正式运行，也不从其速度差异推断算法总体速度。</p>
<details><summary>查看两个探测的范围与原始记录</summary><pre id="probe-raw"></pre></details></section>
'''

TERMINAL_JS = r'''
const execution=D.context['execution_notes.json'];
$('recording-note').textContent=execution?'执行透明记录：首次基线优化器已完成，但诊断元数据序列化报错，精确端到端耗时与正式响应丢失；该孤立解和当时冻结文件归档，未纳入本报告成绩。修复仅调整元数据的序列化方式，随后按相同种子与配置重新完整跑两臂。共执行3次优化器：1次记录失败＋2次正式有效结果；没有从失败解和新解中择优。':'本报告仅展示独立审计通过的两次完整配对结果。';
$('execution-notes').textContent=execution?JSON.stringify(execution,null,2):'没有额外执行说明。';
const terminalValue=(r,k)=>{const t=r.terminal_polish||{},s=t.statistics||{};return k==='iterations'?r.iterations:k==='reserved'?t.reserved_seconds:k==='elapsed'?t.elapsed_seconds:k==='active'?(t.task_exchange_active?'启用':'未启用'):k==='accepted'?(t.accepted?'保留了改善':'未保留新解'):s[k]??0;};
const terminalFields=[['末端预留','reserved','s'],['末端实测','elapsed','s'],['请求交换','active',''],['交换调用','task_exchange_calls','次'],['交换排名时间','task_exchange_ranking_seconds','s'],['短名单排名检查','task_exchange_shortlist_probes','对'],['入选短名单','task_exchange_shortlisted_candidates','对'],['精确两路线候选','task_exchange_candidates','对'],['交换完整核验','task_exchange_full_checks','次'],['动作内接受交换','task_exchange_improvements','次'],['外层最终保留','accepted',''],['主循环完成迭代','iterations','次']];
$('terminal-table').innerHTML='<caption>正式配对运行的算法诊断；共享预算内的强化计时，缺少的计数记为0。</caption><thead><tr><th>诊断项</th><th>当前加权基线</th><th>改进候选</th></tr></thead><tbody>'+terminalFields.map(([label,k,unit])=>'<tr><td>'+label+'</td>'+rows.map(r=>'<td>'+((typeof terminalValue(r,k)==='number')?fmt(terminalValue(r,k),unit==='s'?5:0):esc(terminalValue(r,k)))+(unit?' '+unit:'')+'</td>').join('')+'</tr>').join('')+'</tbody>';
$('terminal-note').textContent='末端实测包括原有步骤和新增交换；0.1 秒是共享的预留上限。动作内接受计数与外层最终保留分别列出，外层还检查完整规范评分与截止时刻。两臂独立搜索轨迹不同，候选末端前后的收益只解释自身 checkpoint，不能代替完整基线比较。';
$('terminal-raw').textContent=JSON.stringify(rows.map(r=>({variant:r.variant,terminal_polish:r.terminal_polish,performance_counters:r.performance_counters})),null,2);
const probe=D.probe;
$('probe-note').textContent=probe?`邻域缺口探测：实现前在上一批已保存的加权解上，对全部跨路线请求对排名后检查 ${fmt(probe.exact_two_route_checks,0)} 个精确交换，找到 ${fmt(probe.improving_probes,0)} 个改善候选，最佳交换航程变化 ${signed(probe.after_score.distance_km-probe.before_score.distance_km)} km、总逾期变化 ${signed(probe.after_score.total_lateness_min-probe.before_score.total_lateness_min)} min，逾期数保持 ${fmt(probe.before_score.late_count,0)} 项。它只说明邻域确有改善机会，不是本批240秒成绩，也没有复用旧分数作正式基线。`:'本目录没有保存机制探测。';
const primitive=D.primitive_probe;
$('primitive-note').textContent=primitive?`已实现有界版本探测：在 ${fmt(primitive.requested_primitive_seconds,3)} s 单独动作上限内，实际动作耗时 ${fmt(primitive.actual_primitive_seconds,6)} s，精确检查 ${fmt(primitive.statistics.candidates,0)} 个交换、接受 ${fmt(primitive.statistics.improvements,0)} 个，航程变化 ${signed(primitive.after_score.distance_km-primitive.before_score.distance_km)} km，总逾期变化 ${signed(primitive.after_score.total_lateness_min-primitive.before_score.total_lateness_min)} min。最终路线另经完整规范核验与独立重走。正式效果仍以本报告两方案、同一新种子的240秒终点为准。`:'未保存已实现动作的额外静态探测。';
$('probe-raw').textContent=JSON.stringify({neighborhood_gap:probe||null,bounded_implemented_action:primitive||null},null,2);
'''


def render_report(directory: Path) -> Path:
    directory = directory.resolve()
    prepare_literature(directory)
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
