"""Integrate the audited five-seed sensitivity study into the author's latest DOCX.

Use the bundled document Python runtime. The supplied manuscript controls styles;
only section 6.2 and directly related claims are edited. A source copy is required
so repeated builds do not accumulate tables. Render and refresh TOC page caches
after building; the renderer's PDF is QA material, not the deliverable.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
import tempfile

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt
from docx.text.paragraph import Paragraph
from repair_latest_equations import fix_equation_28_context, fix_equation_30

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'algorithm/results/final_sensitivity'
FIGURES = ROOT / 'writing/figures/final'
TITLE = '低空物流网络中多无人机一对一取送的协同调度.docx'
IDS = ('pure_direct', 'direct_relay', 'direct_stations', 'direct_relay_stations')
LABELS = dict(zip(IDS, ('D', 'D+R', 'D+P', 'D+P+R')))
METRICS = ('late_count', 'total_lateness_min', 'distance_km')
EXPECTED = {
    'summary.json': 'a130a6fe0ed7afa4da53a33bf1beaf208b615dacb38c712137c04180f3dda7dc',
    'stability.json': 'e5fc267b39c54fe89dcf5d12f1a2bb5119da964943de0f1398208da61c9f2038',
    'runs.json': '121ab61cd6df0190eef1f35bf41bb0adfec1b3502303f9811f099159dc0c4cc4',
}


def read_data():
    for filename, expected in EXPECTED.items():
        assert hashlib.sha256((DATA / filename).read_bytes()).hexdigest() == expected, filename
    summary = json.loads((DATA / 'summary.json').read_text())
    stability = json.loads((DATA / 'stability.json').read_text())
    raw = json.loads((DATA / 'runs.json').read_text())
    rows = raw if isinstance(raw, list) else raw['runs']
    assert len(rows) == 275 and len({r['run_id'] for r in rows}) == 240
    assert len(summary['groups']) == 55 and summary['seed_count'] == 5
    for group in summary['groups']:
        matches = [r for r in rows if all(r[k] == group[k] for k in ('scenario', 'parameter', 'level'))]
        assert len(matches) == 5 and len({r['seed'] for r in matches}) == 5
        for metric in METRICS:
            values = [r[metric] for r in matches]
            assert abs(statistics.mean(values) - group['mean_' + metric]) < 1e-8
            assert abs(statistics.stdev(values) - group['sd_' + metric]) < 1e-8
    return summary, stability


def replace_text(p, text):
    """Preserve paragraph properties/bookmarks and the original run typography."""
    rpr = next((deepcopy(r._r.rPr) for r in p.runs if r._r.rPr is not None), None)
    for node in list(p._p):
        if node.tag not in {qn('w:pPr'), qn('w:bookmarkStart'), qn('w:bookmarkEnd')}:
            p._p.remove(node)
    r = p.add_run(text)
    if rpr is not None:
        r._r.insert(0, rpr)


def set_font(run, size=10.5, bold=False):
    run.font.name = 'Times New Roman'
    run.font.size = Pt(size)
    run.font.bold = bold
    run._r.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), '宋体')


def save_document(doc, output):
    """Replace atomically, including the read-only file supplied by the author."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix='.docx', delete=False) as f:
        temporary = Path(f.name)
    try:
        doc.save(temporary)
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def refresh_toc_cache(output, rendered_pdf):
    """Update existing PAGEREF caches without round-tripping equations through LO.

    Every title must match one rendered body heading exactly. Render once more
    after this operation and confirm that pagination is unchanged.
    """
    from pypdf import PdfReader
    norm = lambda s: re.sub(r'\s+', '', s)
    pages = [[norm(line) for line in page.extract_text().splitlines()]
             for page in PdfReader(rendered_pdf).pages]
    doc = Document(output)
    updated = {}
    for p in doc.paragraphs:
        if not p.style.name.startswith('toc '):
            continue
        label = p.text.split('\t')[0]
        hits = [i + 1 for i, lines in enumerate(pages) if norm(label) in lines]
        assert len(hits) == 1, (label, hits)
        texts = p._p.xpath('.//w:t')
        assert texts[-1].text.isdigit(), label
        assert any('PAGEREF' in x.text for x in p._p.xpath('.//w:instrText')), label
        texts[-1].text = str(hits[0])
        updated[label] = hits[0]
    assert len(updated) == 42
    save_document(doc, output)
    print(json.dumps({'toc_pages': updated}, ensure_ascii=False))


def build(source, output):
    summary, stability = read_data()
    doc = Document(source)
    equation_repairs = [fix_equation_28_context(doc), fix_equation_30(doc)]
    body = doc.element.body
    paras = doc.paragraphs
    start = next(p for p in paras if p.text == '6.2 敏感性分析' and p.style.name == 'Heading 2')
    stop = next(p for p in paras if p.text == '6.3 管理启示' and p.style.name == 'Heading 2')
    # Paragraph wrappers are regenerated by python-docx; locate the template by XML.
    first = Paragraph(start._p.getnext(), doc)
    body_template = deepcopy(first._p)
    caption_template = deepcopy(next(p._p for p in paras if p.text.startswith('表10 ')))
    heading_templates = [deepcopy(next(p._p for p in paras if p.text.startswith(f'6.2.{i} ') and p.style.name == 'Heading 3')) for i in (1, 2, 3)]
    table_template = next(t for t in doc.tables if t.cell(0, 0).text == '参数' and '测试水平' in t.cell(0, 1).text)
    table_pr = deepcopy(table_template._tbl.tblPr)
    # Keep the original heading and its TOC bookmark, replace its entire body.
    node = start._p.getnext()
    while node is not stop._p:
        nxt = node.getnext()
        body.remove(node)
        node = nxt

    def paragraph(text, template=body_template, keep=False):
        element = deepcopy(template)
        for mark in list(element.xpath('.//w:bookmarkStart | .//w:bookmarkEnd')):
            mark.getparent().remove(mark)
        stop._p.addprevious(element)
        p = Paragraph(element, doc)
        replace_text(p, text)
        p.paragraph_format.keep_with_next = keep
        p.paragraph_format.widow_control = True
        return p

    def heading(i):
        # Preserve the original bookmark IDs so existing TOC links remain valid.
        element = deepcopy(heading_templates[i - 1])
        stop._p.addprevious(element)
        return Paragraph(element, doc)

    def table(caption, headers, records, widths):
        paragraph(caption, caption_template, True)
        t = doc.add_table(rows=1, cols=len(headers))
        t._tbl.remove(t._tbl.tblPr)
        t._tbl.insert(0, deepcopy(table_pr))
        t.autofit = False
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        stop._p.addprevious(t._tbl)
        for col, width in zip(t.columns, widths):
            col.width = Cm(width)
        for idx, values in enumerate([headers, *records]):
            row = t.rows[0] if idx == 0 else t.add_row()
            pr = row._tr.get_or_add_trPr()
            pr.append(OxmlElement('w:cantSplit'))
            if idx == 0:
                pr.append(OxmlElement('w:tblHeader'))
            for cell, value, width in zip(row.cells, values, widths):
                cell.width = Cm(width)
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                p = cell.paragraphs[0]
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                pf = p.paragraph_format
                pf.first_line_indent = Cm(0)
                pf.space_before = Pt(2)
                pf.space_after = Pt(2)
                pf.line_spacing = Pt(12)
                pf.line_spacing_rule = WD_LINE_SPACING.AT_LEAST
                pf.keep_with_next = idx == 0
                pf.widow_control = True
                set_font(p.add_run(str(value)), 9.5, idx == 0)
                if idx == 0:
                    borders = OxmlElement('w:tcBorders')
                    border = OxmlElement('w:bottom')
                    for key, value in [('val', 'single'), ('sz', '6'), ('color', '000000')]:
                        border.set(qn('w:' + key), value)
                    borders.append(border)
                    cell._tc.get_or_add_tcPr().append(borders)
        # Match the author's three-line academic tables, not a new design theme.
        return t

    def figure(name, caption):
        p = paragraph('')
        pf = p.paragraph_format
        pf.first_line_indent = Cm(0)
        pf.line_spacing = 1.0
        pf.space_before = Pt(5)
        pf.space_after = Pt(2)
        pf.keep_with_next = True
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(str(FIGURES / name), width=Cm(16))
        p = paragraph(caption, caption_template)
        p.paragraph_format.space_after = Pt(7)

    def mean_sd(row, metric):
        return f"{row['mean_' + metric]:.2f}±{row['sd_' + metric]:.2f}"

    def group(parameter, level, scenario):
        return next(r for r in summary['groups'] if r['parameter'] == parameter and r['level'] == level and r['scenario'] == scenario)

    paragraph('为检验各类策略的参数响应及随机搜索稳定性，本文对直送（D）、直送+中继（D+R）、直送+预部署（D+P）和直送+预部署+中继（D+P+R）开展配对多随机种子单因素实验。每次仅改变一个参数，其余参数保持在8架无人机、截止期倍率1.00和4个候选站的基准值；D不使用候选站，其站点数量实际为0，站点敏感性不适用。表10列出测试范围。')
    table('表10 关键运营参数敏感性设置', ['参数', '测试水平', '基准值', '适用策略'], [
        ['无人机数量', '8、9、10、12、16', '8', 'D、D+R、D+P、D+P+R'],
        ['截止期倍率', '0.80、0.90、1.00、1.10、1.20', '1.00', 'D、D+R、D+P、D+P+R'],
        ['候选站数量', '2、3、4、5、6', '4', 'D+R、D+P、D+P+R'],
    ], [3.0, 5.2, 1.6, 6.2])
    paragraph('各配置均采用2026081701—2026081705这5个配对随机种子，统一使用200项正式任务、单机累计任务上限25和候选插入数48。每次求解的名义总预算为300 s，采用perf_counter单调程序时钟计时，包含初始构造；该时长并非CPU时间。两个中继策略均将80%的搜索预算用于直送预热、20%用于接力搜索，其余策略全程执行直送搜索。本节协议独立于第6.1节的策略比较，不将两节结果合并统计或直接据此归因于某一机制。')
    paragraph('实验覆盖55个“策略×参数×水平”配置，共275条种子结果。相同策略、相同种子的相同基准配置在各参数轴之间复用，因此实际完成240次唯一求解，复用的35条记录不构成额外独立样本。本批实验使用8个并发进程，并随种子轮换策略执行顺序；固定种子控制随机数流，但计时终止和机器负载仍可能影响迭代数。批处理期间的主机暂停记录及实际程序耗时保留在复现文件中。')
    paragraph('表11—13及图10—12报告均值±样本标准差（SD，n=5，分母n−1），误差线表示正负1个SD，用于描述五次搜索的离散程度，并非置信区间。各配置的最小值、最大值和逐种子结果另见随文仓库algorithm/results/final_sensitivity中的summary.csv及runs.csv。稳定性判断直接使用同一种子下未取整的“逾期任务数—总逾期时间—总航程”原始字典序得分，不对均值三元组排序；端点改善表示最大水平优于最小水平，全轴单调不劣表示每次提高水平时得分均不变差。')

    heading(1)
    base = [group('drone_count', 8, s) for s in IDS]
    baseline_text = '、'.join(mean_sd(r, 'late_count') for r in base)
    paragraph('机队规模的改善方向在四种策略中均较稳定。表11与图10显示，8架时D、D+R、D+P、D+P+R的逾期任务数分别为' + baseline_text + '，五种子观测范围分别为63—67、64—67、59—62和59—63；增加至16架后，四种策略在全部5个种子上均实现零逾期。四种策略的8→16架端点得分均为5次改善、0次持平、0次变差，且全轴单调不劣均为5/5。16架也是每个策略、每个种子在已测试水平中的唯一最优档位。')
    paragraph('这一结论表明，扩充机队在本实例中稳定释放了并行服务能力，但不意味着航程也必然单调减少。16架时四种策略的平均航程分别为' + '、'.join(mean_sd(group('drone_count', 16, s), 'distance_km') for s in IDS) + ' km。此时前两层目标均为0，可进一步比较航程；在未达到相同服务水平时，应先遵守字典序优先关系。16架仅是所测离散档位中的观测结果，不能据此断言它是实现零逾期的最小机队或经济最优规模。')

    def objective_table(axis, number, title, axis_label):
        records = []
        for s in IDS:
            selected = sorted([r for r in summary['groups'] if r['scenario'] == s and r['parameter'] == axis], key=lambda r: r['level'])
            for r in selected:
                level = f"{r['level']:.2f}" if axis == 'deadline_multiplier' else str(int(r['level']))
                records.append([LABELS[s], level, *[mean_sd(r, m) for m in METRICS]])
        t = table(f'表{number} {title}（n=5，均值±样本SD）', ['策略', axis_label, '逾期任务数', '总逾期/min', '航程/km'], records, [2.3, 2.0, 3.0, 4.4, 4.3])
        if axis == 'deadline_multiplier':
            # Avoid leaving the final data row alone on a continuation page.
            for row in t.rows:
                for cell in row.cells:
                    cell.paragraphs[0].paragraph_format.space_before = Pt(1.5)
                    cell.paragraphs[0].paragraph_format.space_after = Pt(1.5)

    figure('fig6_3_drone_count_sensitivity.png', '图10 四种策略的机队规模敏感性（n=5，均值±样本SD）')
    objective_table('drone_count', 11, '机队规模敏感性结果', '无人机数')
    heading(2)
    paragraph('放宽服务时限的改善方向同样在四种策略中保持一致。由表12与图11可见，截止期倍率由0.80提高至1.20时，各策略的端点得分均为5次改善、0次持平、0次变差，全轴单调不劣均为5/5，且1.20在每个种子上均为所测水平中的唯一最优档位。按D、D+R、D+P、D+P+R顺序，平均逾期任务数分别由' + '、'.join(mean_sd(group('deadline_multiplier', .8, s), 'late_count') for s in IDS) + '降至' + '、'.join(mean_sd(group('deadline_multiplier', 1.2, s), 'late_count') for s in IDS) + '。')
    paragraph('前两层指标的变化反映服务承诺与可用运力之间的匹配程度；航程变化应作为第三层结果分别阅读，不能用航程较短抵消逾期增加。该实验支持在本需求实例中评估更宽时限带来的准时性收益，但单因素设计尚不能识别机队规模与服务时限的交互效应，也不能直接确定不同订单的最佳服务承诺。')
    figure('fig6_3_deadline_multiplier_sensitivity.png', '图11 四种策略的截止期倍率敏感性（n=5，均值±样本SD）')
    objective_table('deadline_multiplier', 12, '截止期倍率敏感性结果', '截止期倍率')
    heading(3)
    paragraph('站点配置的响应随策略和随机种子变化，未出现一致的单调改善。D+R使用站点作为接力候选位置，D+P将其作为预部署候选起点，D+P+R同时使用两项功能，故本轴仅比较这三种策略。每个站点数量均采用加权k-medoids方法和选址种子42重新选址；站数增加会同时改变位置，并非在原站集上逐个追加，因此本实验考察的是站点配置的整体变化。')
    paragraph('由表13与图12可见，站点数由2增至6时，D+R、D+P和D+P+R的端点得分改善/持平/变差次数分别为3/0/2、2/0/3和3/0/2；三者全轴单调不劣均为0/5。逐种子最佳站点数的频次分别为：D+R在3、4、6站上各出现2、2、1次；D+P在3、4、5、6站上各出现1、1、2、1次；D+P+R在4、5、6站上各出现2、2、1次。因此，单一种子下的“6站最优”不能作为稳定结论，增加站点也不保证改善三层服务目标。')
    figure('fig6_3_station_count_sensitivity.png', '图12 三种适用策略的站点敏感性（n=5，均值±样本SD；D不适用）')
    objective_table('station_count', 13, '候选站数量敏感性结果', '候选站数')
    paragraph('表14进一步报告每个参数水平下各策略在5个配对种子中的唯一最优次数。D+P在15个展示水平中的8个水平上均为5/5唯一最优，其余7个水平的最优策略随种子变化，未出现并列最优。由于基准配置在三个参数轴中重复展示，15个水平并非15项独立试验，这些次数不能解释为总体胜出概率或统计显著性。结果支持预部署在本实例中的较稳定优势，但不足以认定组合策略始终最佳。')
    axis_names = {'drone_count': '无人机数', 'deadline_multiplier': '截止期倍率', 'station_count': '候选站数'}
    records = []
    for item in stability['level_comparisons']:
        counts = {x['scenario']: x['wins'] for x in item['strategy_counts']}
        level = f"{item['level']:.2f}" if item['parameter'] == 'deadline_multiplier' else str(int(item['level']))
        records.append([axis_names[item['parameter']], level, *[str(counts[s]) + '/5' if s in counts else '不适用' for s in IDS]])
    table('表14 各参数水平下的逐种子唯一最优策略频次', ['参数', '水平', 'D', 'D+R', 'D+P', 'D+P+R'], records, [3.4, 2.0, 2.5, 2.6, 2.6, 2.9])
    paragraph('本节结果刻画固定200任务实例和300 s预算下的随机搜索波动，不代表不同需求日或不同网络的变异。均值、SD及观测范围描述解的离散程度，配对字典序次数描述排序一致性，两者共同用于判断结论是否受单次搜索影响。完整配置、原始得分、汇总统计和配对判断分别保存在manifest.json、runs.json、summary.json与stability.json中，可由同目录的validation.json核验。')

    # Keep the author's manuscript intact outside the directly related claims.
    replacements = [
        ('四种场景采用相同的任务数据、可行性规则和三层字典序目标，差异仅来自起点配置与服务方式的开放范围。',
         '四种场景采用相同的任务数据、可行性规则和三层字典序目标，模型设置的差异来自起点配置与服务方式的开放范围。该节报告既有三种子策略实验，其计算协议与第6.2节新增的统一300 s实验不同，不据此作纯机制因果比较；逐种子的统一预算结果以第6.2节为准。'),
        ('敏感性分析进一步表明，机队规模、服务时限和站点位置共同影响系统绩效。',
         '四种策略的五随机种子敏感性分析表明，增加机队规模与放宽时限的改善方向保持一致，而站点配置的效果及最优站点数随种子变化。'),
        ('Sensitivity analysis further shows that fleet size, service deadlines, and station locations jointly shape system performance.',
         'Sensitivity analysis of all four strategies across five paired random seeds shows consistent improvements from fleet expansion and deadline relaxation, whereas station-configuration effects and the best station count vary across seeds.'),
        ('二者均显著作用于逾期任务数和总逾期时间。',
         '在本实例的五个配对种子中，四种策略沿这两个参数轴均保持三层得分单调不劣。'),
        ('这一结果意味着，低空物流网络设计本质上不是单纯的运力扩张问题，而是运力、服务承诺与空间基础设施之间的协同配置问题。',
         '五个配对种子进一步显示，四种策略沿机队规模和截止期倍率两轴均保持得分单调不劣，16架时全部达到零逾期；站点轴则没有跨种子一致的最优数量。在15个展示水平中，D+P有8个水平保持5/5唯一最优，其余7个水平的最优策略随种子变化，故不能将某一组合策略或站点数量视为普遍最优。'),
        ('首先，当前部分协同策略和敏感性实验受计算预算与随机种子数量限制，后续可在统一墙钟预算和更多重复实例下，通过置信区间和统计检验进一步识别预部署与中继接力的独立及交互效应。',
         '首先，敏感性实验已统一300 s程序计时预算并扩展至5个配对随机种子，但仍仅对应一个200任务需求实例；样本SD不等于置信区间，单因素实验也不能识别参数交互。后续应增加独立需求实例与重复次数，并采用多因素设计检验机制及参数交互效应。'),
        ('按照“逾期任务数—总逾期时长—总飞行里程”的严格字典序规则，“直送+预部署+中继”取得最低观测三层得分（59.33，1860.72 min，613.86 km）。',
         '“直送+预部署+中继”的三项观测均值为（59.33，1860.72 min，613.86 km）；这些分项均值用于描述综合表现，并非逐种子字典序排名。'),
        ('这与表8的字典序比较一致，说明当前实例中预部署承担主要的结构性改善作用，中继接力更多表现为对少量关键任务的补充性时序重组。',
         '这与表8所示分项均值的变化方向一致；在该协议下，预部署表现出更明显的服务改善，中继接力主要体现为少量关键任务的补充性时序重组。统一预算下的逐种子比较另见第6.2节。'),
    ]
    for old, new in replacements:
        matches = [p for p in doc.paragraphs if old in p.text]
        assert len(matches) == 1, old
        p = matches[0]
        replace_text(p, p.text.replace(old, new))
    management = next(p for p in doc.paragraphs if p.text.startswith('第三，机队规模与服务时限应进行联合设计'))
    replace_text(management, management.text + '上述结果分别来自两个单因素参数轴，联合配置的收益仍需后续多因素实验检验。')
    stations = next(p for p in doc.paragraphs if p.text.startswith('第四，中继基础设施规划应坚持'))
    replace_text(stations, '第四，中继基础设施规划应同时评估站点数量、布局与功能复用。五个种子中，三种站点策略均未呈现全轴单调不劣，且最优站数不一致，说明本实例不支持仅凭站点增加预期服务改善。由于站数变化伴随重新选址，现有结果尚不能单独识别位置质量的因果作用。管理上可进一步评估候选站能否兼顾高需求区域的预部署起点与跨机交接位置，并在给定建设成本、缓存容量和服务能力后，比较不同数量与布局的组合。')
    conclusion = next(p for p in doc.paragraphs if p.text.startswith('敏感性分析进一步表明，低空物流网络的服务能力'))
    replace_text(conclusion, '五随机种子敏感性分析进一步区分了稳定的变化方向与依赖搜索结果的策略选择。在本200任务实例和统一300 s预算下，四种策略沿机队规模与截止期倍率两轴均保持三层得分单调不劣，且16架时全部达到零逾期；站点轴则没有跨种子一致的最优数量。D+P在15个展示水平中的8个水平保持5/5唯一最优，其余7个水平的最优策略随种子变化，且三个基准水平复用同一组解。因此，可将机队规模和服务承诺作为后续联合规划的重点，同时评估站点数量与布局；当前单因素结果尚不能确定联合配置的最优方案，也不能将某一组合策略或站点数量视为普遍最优。')
    # Repair only layout defects observed in the supplied manuscript's rendering.
    for p in doc.paragraphs[5:14]:
        assert not p.text.strip()
        pf = p.paragraph_format
        pf.line_spacing = Pt(3)
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        pf.space_before = pf.space_after = Pt(0)
    for i, p in enumerate(doc.paragraphs[14:18]):
        pf = p.paragraph_format
        pf.line_spacing = 1.25
        pf.space_after = Pt(6)
        pf.keep_with_next = i < 3
    for p in doc.paragraphs:
        if p._p.xpath('.//m:oMath') and re.fullmatch(r'\s*\(\d+\)\s*', p.text):
            p.paragraph_format.line_spacing = Pt(18)
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.AT_LEAST
    refs = next(p for p in doc.paragraphs if p.text == '参考文献')
    node = refs._p.getprevious()
    while node is not None and node.tag == qn('w:p') and not Paragraph(node, doc).text.strip() and not node.xpath('.//w:drawing | .//m:oMath'):
        prev = node.getprevious()
        body.remove(node)
        node = prev
    for t in doc.tables:
        for row in t.rows:
            pr = row._tr.get_or_add_trPr()
            if pr.find(qn('w:cantSplit')) is None:
                pr.append(OxmlElement('w:cantSplit'))
    update = doc.settings.element.find(qn('w:updateFields'))
    if update is None:
        update = OxmlElement('w:updateFields')
        doc.settings.element.append(update)
    update.set(qn('w:val'), 'true')
    save_document(doc, output)
    print(json.dumps({'output': str(output), 'groups': 55, 'seeds': 5, 'sensitivity_tables': 5, 'sensitivity_figures': 3, 'equation_repairs': equation_repairs, 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest()}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--output', type=Path, default=ROOT / TITLE)
    parser.add_argument('--refresh-toc', type=Path, metavar='RENDERED_PDF')
    args = parser.parse_args()
    if args.refresh_toc:
        refresh_toc_cache(args.output, args.refresh_toc)
    else:
        if args.source is None or args.source.resolve() == args.output.resolve():
            parser.error('Use an unchanged source copy so the author manuscript remains recoverable.')
        build(args.source, args.output)
