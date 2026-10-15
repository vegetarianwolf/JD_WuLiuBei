#!/usr/bin/env python3
"""Build the offline candidate gallery from the checked historical plot data.

Usage: python3 writing/figures/section62_ppt_20261015/build_gallery.py
No dependencies or network access are required. Only index.html is written.
"""

from __future__ import annotations

import html
import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent
THEMES = {
    "drone_count": {
        "label": "无人机数量",
        "axis_label": "无人机数量（架）",
        "baseline": 8,
        "fixed": "固定截止时间倍率 1.0、候选站点数 4。",
        "note": "本次观测中，8–16 架对应的逾期任务数为 60、46、33、13、0。单次求解结果不代表重复实验均值或最优性保证。",
    },
    "deadline_multiplier": {
        "label": "截止时间倍率",
        "axis_label": "截止时间倍率（倍）",
        "baseline": 1.0,
        "fixed": "固定无人机数 8、候选站点数 4。",
        "note": "本次观测中，截止时间倍率 0.8–1.2 对应的逾期任务数为 81、71、60、49、39。连线仅帮助比较观测点，不表示拟合或额外实验。",
    },
    "station_count": {
        "label": "候选站点配置",
        "axis_label": "候选站点数（个）及对应选址",
        "baseline": 4,
        "fixed": "固定无人机数 8、截止时间倍率 1.0；各站点数量分别重新选址。",
        "note": "2、3、4、5、6 个站点各自采用加权 k-medoids 选址，配置并非逐点扩展的嵌套集合。结果同时反映站点数量与选址变化，不能仅归因于数量；逾期任务数为 59、61、60、61、58，未呈单调变化。",
    },
}
VARIANTS = {
    "a_facets": {
        "label": "A · 三指标分面",
        "short": "三指标分面",
        "advice": "适合论文汇报与答辩：并列查看逾期任务数、总逾期时间、总飞行距离，保留三项指标各自的单位与完整水平。",
    },
    "b_service": {
        "label": "B · 逾期任务数聚焦",
        "short": "逾期任务数聚焦",
        "advice": "适合主线汇报：放大展示五个水平的逾期任务数，纵轴从零开始。讨论时间与距离时，配合 A 版或下方数据表。",
    },
    "c_matrix": {
        "label": "C · 基准变化矩阵",
        "short": "基准变化矩阵",
        "advice": "适合大屏概览：同屏比较三项指标的绝对值与相对本轴基准的变化。正负号只表示增减；各参数轴的基准分别求解。",
    },
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def level_label(theme: str, level: float) -> str:
    return f"{level:.1f}" if theme == "deadline_multiplier" else f"{level:.0f}"


def validate(data: dict) -> None:
    expected = {
        "drone_count": [8, 9, 10, 12, 16],
        "deadline_multiplier": [0.8, 0.9, 1.0, 1.1, 1.2],
        "station_count": [2, 3, 4, 5, 6],
    }
    records = data["records"]
    if len(records) != 15:
        raise ValueError("Expected exactly 15 historical observations")
    for theme, levels in expected.items():
        rows = sorted((r for r in records if r["parameter"] == theme), key=lambda r: r["level"])
        if [r["level"] for r in rows] != levels:
            raise ValueError(f"Unexpected levels for {theme}")
        for row in rows:
            for metric in ("late_count", "total_lateness_min", "distance_km"):
                if not math.isfinite(row[metric]) or row[metric] < 0:
                    raise ValueError(f"Invalid {metric} in {theme}")
            if row["late_count"] != int(row["late_count"]):
                raise ValueError("A task count must be an integer")


def table_rows(data: dict, theme: str) -> str:
    rows = []
    for row in sorted((r for r in data["records"] if r["parameter"] == theme), key=lambda r: r["level"]):
        baseline = row["level"] == THEMES[theme]["baseline"]
        label = level_label(theme, row["level"]) + ("（基准）" if baseline else "")
        rows.append(
            f'<tr><th scope="row">{esc(label)}</th>'
            f'<td>{row["late_count"]:.0f}</td>'
            f'<td>{row["total_lateness_min"]:,.2f}</td>'
            f'<td>{row["distance_km"]:,.2f}</td></tr>'
        )
    return "\n".join(rows)


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="description" content="当前稿 6.2 敏感性分析的九张汇报图候选，含历史观测数据与使用说明。">
  <title>6.2 敏感性分析 · PPT 图候选</title>
  <style>
    :root { color-scheme: light dark; --page: #ffffff; --ink: #192431; --muted: #52606d; --rule: #d7dee5; --selected: #253647; --on-selected: #ffffff; --soft: #f1f4f6; --link: #075c99; }
    @media (prefers-color-scheme: dark) { :root { --page: #141b22; --ink: #edf1f5; --muted: #bac5d0; --rule: #455360; --selected: #dde6ee; --on-selected: #192431; --soft: #26313c; --link: #a4d4f5; } }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--page); color: var(--ink); font: 400 16px/1.65 -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif; }
    main { width: min(1200px, 100%); margin: 0 auto; padding: 32px 28px 48px; }
    h1, h2, h3, legend, th, strong { font-weight: 500; }
    h1 { margin: 0 0 8px; font-size: clamp(25px, 3.2vw, 34px); line-height: 1.3; }
    h2 { margin: 0; font-size: 23px; line-height: 1.4; }
    h3 { margin: 0 0 8px; font-size: 18px; }
    p { margin: 10px 0; }
    a { color: var(--link); text-underline-offset: .18em; overflow-wrap: anywhere; }
    button, select { font: inherit; max-width: 100%; }
    select { display: block; width: 100%; min-height: 44px; padding: 7px 10px; color: var(--ink); background: var(--page); border: 1px solid var(--rule); border-radius: 4px; }
    button { color: var(--ink); }
    .muted { color: var(--muted); }
    .intro { margin-bottom: 26px; }
    .selection { display: grid; grid-template-columns: minmax(210px, 320px) minmax(0, 1fr); gap: 20px 36px; align-items: end; margin-bottom: 22px; }
    .field-label { display: block; margin-bottom: 5px; }
    .context p { margin: 0; }
    fieldset { padding: 0; margin: 0; border: 0; min-width: 0; }
    legend { margin-bottom: 10px; padding: 0; }
    .variants { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }
    .variant { display: block; width: 100%; padding: 0; overflow: hidden; text-align: left; border: 1px solid var(--rule); border-radius: 4px; background: var(--page); }
    .variant img { display: block; width: 100%; height: auto; aspect-ratio: 16 / 9; object-fit: contain; }
    .variant span { display: block; padding: 10px 12px; min-height: 44px; }
    .variant[aria-pressed="true"] span { background: var(--selected); color: var(--on-selected); }
    .variant[aria-pressed="true"] { border-color: var(--selected); }
    .selected-mark { font-style: normal; }
    .preview { margin: 30px 0 26px; }
    .preview-header { display: flex; flex-wrap: wrap; gap: 16px; justify-content: space-between; align-items: center; margin-bottom: 12px; }
    .downloads { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; }
    .downloads a { display: inline-flex; min-height: 44px; align-items: center; }
    .main-image { display: block; width: 100%; height: auto; aspect-ratio: 16 / 9; object-fit: contain; }
    figcaption { margin-top: 10px; }
    .caption-title { display: block; }
    .section { border-top: 1px solid var(--rule); padding-top: 22px; margin-top: 26px; }
    .table-scroll { width: 100%; overflow-x: auto; }
    table { border-collapse: collapse; width: 100%; min-width: 555px; font-variant-numeric: tabular-nums; }
    th, td { padding: 10px 12px; border-bottom: 1px solid var(--rule); text-align: right; white-space: nowrap; }
    th:first-child { text-align: left; }
    thead th { white-space: normal; vertical-align: bottom; }
    .boundaries { padding-left: 22px; }
    .boundaries li { margin: 7px 0; }
    .source-links { display: flex; flex-wrap: wrap; gap: 10px 24px; }
    .source-links a { display: inline-flex; align-items: center; min-height: 44px; }
    .source-path { overflow-wrap: anywhere; font-family: ui-monospace, monospace; }
    [hidden] { display: none !important; }
    @media (max-width: 700px) { main { padding: 22px 16px 34px; } .selection { grid-template-columns: 1fr; gap: 12px; } .variants { grid-template-columns: 1fr; gap: 12px; } .variant { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); align-items: stretch; } .variant span { display: flex; flex-direction: column; justify-content: center; padding: 10px; } .variant img { align-self: center; } .preview-header { display: block; } .downloads { margin-top: 8px; } h2 { font-size: 21px; } }
  </style>
</head>
<body>
<main id="section62-gallery">
  <header class="intro">
    <h1>6.2 敏感性分析 · PPT 图候选</h1>
    <p class="muted">3 个主题 × 3 种呈现，共 9 张 16:9 图。选择缩略图，查看完整候选与对应观测数据。</p>
  </header>

  <section aria-label="候选选择">
    <div class="selection">
      <label for="theme-select"><span class="field-label">敏感性主题</span><select id="theme-select">__OPTIONS__</select></label>
      <div class="context"><p id="fixed-context">__FIXED__</p><p id="baseline-context" class="muted">本轴基准：8 架；三个参数轴的基准分别求解。</p></div>
    </div>
    <fieldset>
      <legend>候选版本 · 点击缩略图切换</legend>
      <div class="variants">__CANDIDATES__</div>
    </fieldset>
  </section>

  <figure class="preview" aria-labelledby="preview-heading">
    <div class="preview-header">
      <h2 id="preview-heading">无人机数量 · 三指标分面</h2>
      <nav class="downloads" aria-label="所选图下载">
        <a id="download-png" href="png/drone_count_a_facets.png" download>下载高清 PNG</a>
        <a id="download-svg" href="svg/drone_count_a_facets.svg" download>下载 SVG</a>
        <a id="open-full" href="svg/drone_count_a_facets.svg" target="_blank" rel="noopener">原尺寸查看</a>
      </nav>
    </div>
    <img class="main-image" id="main-image" src="svg/drone_count_a_facets.svg" width="1600" height="900" alt="无人机数量敏感性：三指标分面。完整观测值列于下方数据表。" aria-describedby="version-advice observation-note">
    <p id="image-error" role="status" hidden>图像未能加载。请保留下载目录结构，并确认 svg 与 png 子目录和本页放在一起。</p>
    <figcaption><strong class="caption-title">选用建议</strong><span id="version-advice">__ADVICE__</span></figcaption>
  </figure>
  <p id="selection-status" class="muted" aria-live="polite" aria-atomic="true">当前候选：无人机数量 · A · 三指标分面</p>

  <section class="section" aria-labelledby="data-heading">
    <h2 id="data-heading">本主题观测数据</h2>
    <p id="observation-note">__OBSERVATION__</p>
    <div class="table-scroll">
      <table aria-describedby="table-note">
        <caption class="muted" id="table-caption">无人机数量：五个参数水平</caption>
        <thead><tr><th scope="col" id="parameter-heading">无人机数量（架）</th><th scope="col">逾期任务数<br>（项）</th><th scope="col">总逾期时间<br>（分钟）</th><th scope="col">总飞行距离<br>（km）</th></tr></thead>
        <tbody id="data-rows">__ROWS__</tbody>
      </table>
    </div>
    <p class="muted" id="table-note">时间与距离在表中显示两位小数，计算使用原始精度；窄屏可横向滚动数据表。图中相对变化 =（观测值 − 本轴基准值）/ 本轴基准值 × 100%。</p>
  </section>

  <section class="section" aria-labelledby="source-heading">
    <h2 id="source-heading">数据范围与来源</h2>
    <ul class="boundaries">
      <li>当前稿 6.2 所对应的历史记录；使用“__SCENARIO__”策略，__TASKS__ 项任务，随机种子 __SEED__，每次求解名义时限 __SECONDS__ 秒。</li>
      <li>__STATISTIC__。不添加误差条，不据此宣称统计显著性或参数变化的普遍因果效果。</li>
      <li>__BASELINE_NOTE__ 因此三个主题中相同名义配置的数值可能略有差异，不能混用基准。</li>
      <li>候选站点主题同时改变站点数量和相应选址，五档配置不是嵌套集合。</li>
      <li>不使用 6.1.2 策略比较的示意数值，也不混入新一轮 6.2 实验。论文正文与历史实验文件保持原样。</li>
      <li>原稿 6.2 后两处“图10”引用应分别对应图11、图12；此处仅说明，未改动原稿。</li>
    </ul>
    <p class="muted">固定来源提交：<span class="source-path">__COMMIT__</span><br>历史汇总路径：<span class="source-path">__CSV__</span></p>
    <nav class="source-links" aria-label="来源文件">
      <a href="data/plot_data.json" download>下载绘图数据 JSON</a>
      <a href="data/historical_runs.csv" download>下载历史汇总 CSV</a>
      <a href="__SOURCE_URL__" target="_blank" rel="noopener">查看固定提交中的来源</a>
    </nav>
  </section>
  <noscript><p>当前显示无人机数量的 A 版与完整数据。启用浏览器脚本可在全部九个候选之间切换；所有图片也可直接从 svg 和 png 子目录打开。</p></noscript>
</main>
<script id="gallery-data" type="application/json">__DATA__</script>
<script>
(() => {
  'use strict';
  const root = document.getElementById('section62-gallery');
  const source = JSON.parse(document.getElementById('gallery-data').textContent);
  const themes = source.themes;
  const variants = source.variants;
  const rows = source.data.records;
  const select = root.querySelector('#theme-select');
  const mainImage = root.querySelector('#main-image');
  const imageError = root.querySelector('#image-error');
  let selectedVariant = 'a_facets';
  const formatNumber = (value, digits = 2) => Number(value).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const formatLevel = (key, value) => key === 'deadline_multiplier' ? Number(value).toFixed(1) : String(Number(value));
  const imagePath = (key, variant, ext) => `${ext}/${key}_${variant}.${ext}`;
  const setText = (id, value) => { root.querySelector(`#${id}`).textContent = value; };

  function updateGallery() {
    const key = select.value;
    const theme = themes[key];
    const variant = variants[selectedVariant];
    const units = key === 'drone_count' ? ' 架' : key === 'station_count' ? ' 个站点及对应选址' : ' 倍';
    setText('fixed-context', theme.fixed);
    setText('baseline-context', `本轴基准：${formatLevel(key, theme.baseline)}${units}；三个参数轴的基准分别求解。`);
    setText('preview-heading', `${theme.label} · ${variant.short}`);
    setText('version-advice', variant.advice);
    setText('observation-note', theme.note);
    setText('parameter-heading', theme.axis_label);
    setText('table-caption', `${theme.label}：五个参数水平`);
    setText('selection-status', `当前候选：${theme.label} · ${variant.label}`);
    for (const button of root.querySelectorAll('button[data-variant]')) {
      const code = button.dataset.variant;
      const selected = code === selectedVariant;
      button.setAttribute('aria-pressed', String(selected));
      button.setAttribute('aria-label', `${themes[key].label}，${variants[code].label}${selected ? '，已选' : ''}`);
      button.querySelector('img').src = imagePath(key, code, 'svg');
      button.querySelector('.selected-mark').textContent = selected ? '（已选）' : '';
    }
    imageError.hidden = true;
    mainImage.src = imagePath(key, selectedVariant, 'svg');
    mainImage.alt = `${theme.label}敏感性：${variant.short}。完整观测值列于下方数据表。`;
    for (const ext of ['png', 'svg']) {
      root.querySelector(`#download-${ext}`).href = imagePath(key, selectedVariant, ext);
    }
    root.querySelector('#open-full').href = imagePath(key, selectedVariant, 'svg');
    const tbody = root.querySelector('#data-rows');
    tbody.replaceChildren();
    const chosenRows = rows.filter(row => row.parameter === key).sort((a, b) => a.level - b.level);
    for (const row of chosenRows) {
      const tr = document.createElement('tr');
      const label = document.createElement('th');
      label.scope = 'row';
      label.textContent = formatLevel(key, row.level) + (row.level === theme.baseline ? '（基准）' : '');
      tr.append(label);
      for (const metric of ['late_count', 'total_lateness_min', 'distance_km']) {
        const td = document.createElement('td');
        td.textContent = formatNumber(row[metric], metric === 'late_count' ? 0 : 2);
        tr.append(td);
      }
      tbody.append(tr);
    }
  }
  mainImage.addEventListener('error', () => { imageError.hidden = false; });
  mainImage.addEventListener('load', () => { imageError.hidden = true; });
  if (mainImage.complete && mainImage.naturalWidth === 0) imageError.hidden = false;
  select.addEventListener('change', updateGallery);
  for (const button of root.querySelectorAll('button[data-variant]')) {
    button.addEventListener('click', () => { selectedVariant = button.dataset.variant; updateGallery(); });
  }
})();
</script>
</body>
</html>
'''


def main() -> None:
    data = json.loads((HERE / "data" / "plot_data.json").read_text(encoding="utf-8"))
    validate(data)
    options = "".join(f'<option value="{key}">{esc(theme["label"])}</option>' for key, theme in THEMES.items())
    candidates = []
    for code, variant in VARIANTS.items():
        selected = code == "a_facets"
        candidates.append(
            f'<button class="variant" type="button" data-variant="{code}" aria-pressed="{str(selected).lower()}" '
            f'aria-label="无人机数量，{esc(variant["label"])}{"，已选" if selected else ""}">'
            f'<img src="svg/drone_count_{code}.svg" width="1600" height="900" alt="">'
            f'<span>{esc(variant["label"])}<em class="selected-mark">{"（已选）" if selected else ""}</em></span></button>'
        )
    payload = json.dumps({"data": data, "themes": THEMES, "variants": VARIANTS}, ensure_ascii=False, separators=(",", ":"))
    # Avoid ending the inert JSON script if a future source contains markup.
    payload = payload.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    tokens = {
        "OPTIONS": options,
        "CANDIDATES": "\n".join(candidates),
        "FIXED": esc(THEMES["drone_count"]["fixed"]),
        "OBSERVATION": esc(THEMES["drone_count"]["note"]),
        "ADVICE": esc(VARIANTS["a_facets"]["advice"]),
        "ROWS": table_rows(data, "drone_count"),
        "SCENARIO": esc(data["scenario"]),
        "TASKS": esc(data["tasks"]),
        "SEED": esc(data["seed"]),
        "SECONDS": esc(data["nominal_seconds"]),
        "STATISTIC": esc(data["statistic"]),
        "BASELINE_NOTE": esc(data["baseline_note"]),
        "COMMIT": esc(data["source_commit"]),
        "CSV": esc(data["source_csv"]),
        "SOURCE_URL": esc(f'https://github.com/vegetarianwolf/JD_WuLiuBei/blob/{data["source_commit"]}/{data["source_csv"]}'),
        "DATA": payload,
    }
    output = TEMPLATE
    for key, value in tokens.items():
        output = output.replace(f"__{key}__", value)
    (HERE / "index.html").write_text(output, encoding="utf-8")
    print(f"Wrote {HERE / 'index.html'} ({len(output.encode('utf-8')):,} bytes; 3 themes × 3 variants)")


if __name__ == "__main__":
    main()
