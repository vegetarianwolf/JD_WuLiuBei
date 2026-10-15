#!/usr/bin/env python3
"""Rebuild nine presentation figures from the pinned manuscript data; no solver calls.

Run from any directory: MPLCONFIGDIR=/tmp/section62-mpl python build_figures.py
Dependencies: numpy, matplotlib, Pillow. FONT_PATH can select a Chinese font.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", "/tmp/section62-mpl")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.patches import Rectangle
from matplotlib.ticker import MaxNLocator, FuncFormatter
import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
DATA = json.loads((ROOT / "data/plot_data.json").read_text())
BLUE, ORANGE, GREEN = "#0072B2", "#D55E00", "#00836B"
INK, MUTED, GRID = "#172B3A", "#4D5E6B", "#DDE3E8"
BG, DARK, WHITE = "#FFFFFF", "#102432", "#F4F7FA"
METRICS = [
    ("late_count", "逾期任务数", "单", BLUE),
    ("total_lateness_min", "总逾期时间", "min", ORANGE),
    ("distance_km", "总航程", "km", GREEN),
]
THEMES = {
    "drone_count": {
        "name": "机队规模", "axis": "无人机数量（架）", "base": 8, "fig": 10,
        "control": "截止期倍率 1.0 · 候选站 4 个",
        "headline": "16 架时，本次观测为零逾期",
        "finding": "逾期数 60 → 0；总航程在中间水平仍有上升",
        "limit": "仅测试 8、9、10、12、16 架；零逾期的最低机队规模尚未识别。",
    },
    "deadline_multiplier": {
        "name": "服务时限", "axis": "截止期倍率（倍）", "base": 1.0, "fig": 11,
        "control": "无人机 8 架 · 候选站 4 个",
        "headline": "0.8 → 1.2 倍：逾期数从 81 降至 39",
        "finding": "端点逾期数下降 51.85%；总航程未呈单调变化",
        "limit": "倍率同时改变截止期定义；这里是所测水平的观测比较，不是因果效应。",
    },
    "station_count": {
        "name": "候选站点配置", "axis": "候选站数量（个）", "base": 4, "fig": 12,
        "control": "无人机 8 架 · 截止期倍率 1.0 · 各档重新选址",
        "headline": "逾期数在 58–61 单间波动，未随站数单调下降",
        "finding": "6 站配置的观测三级得分最低；不能推广为普遍最优站数",
        "limit": "各档站点位置也变化；本图比较数量及对应选址方案，无法单独识别数量作用。",
    },
}


def choose_font():
    custom = os.environ.get("FONT_PATH")
    candidates = [custom] if custom else []
    candidates += ["/System/Library/Fonts/STHeiti Light.ttc", "/System/Library/Fonts/STHeiti Medium.ttc"]
    for p in candidates:
        if p and Path(p).is_file():
            font_manager.fontManager.addfont(p)
            return p, font_manager.FontProperties(fname=p).get_name()
    for f in font_manager.fontManager.ttflist:
        if any(name in f.name for name in ("Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", "PingFang")):
            return f.fname, f.name
    raise RuntimeError("A Chinese font is required; set FONT_PATH to an installed TTF/TTC/OTF.")


FONT_PATH, FONT_NAME = choose_font()
plt.rcParams.update({
    "font.family": FONT_NAME, "font.size": 17, "axes.unicode_minus": False,
    "svg.fonttype": "path", "svg.hashsalt": "section62-manuscript-303e711",
    "axes.edgecolor": GRID, "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": MUTED, "ytick.color": MUTED, "axes.linewidth": 0.8,
    "savefig.facecolor": BG,
})


def records(theme):
    return sorted((r for r in DATA["records"] if r["parameter"] == theme), key=lambda r: r["level"])


def level_label(theme, x):
    return f"{x:.1f}" if theme == "deadline_multiplier" else f"{x:.0f}"


def value_label(key, value, precise=False):
    return f"{value:.0f}" if key == "late_count" else f"{value:,.{2 if precise else 1}f}"


def frame(theme, variant, title=None, dark=False):
    meta = THEMES[theme]
    fig = plt.figure(figsize=(16, 9), dpi=240, facecolor=DARK if dark else BG)
    color, secondary = (WHITE, "#C3CFD7") if dark else (INK, MUTED)
    fig.text(.055, .943, f"6.2 · 图 {meta['fig']} · {meta['name']}    /    {variant}", fontsize=15, color=secondary)
    fig.text(.055, .870, title or f"{meta['name']}对三项绩效指标的观测", fontsize=29, color=color, weight="medium")
    fig.text(.055, .817, meta["control"], fontsize=17, color=secondary)
    fig.text(.055, .108, meta["limit"], fontsize=14, color=secondary)
    fig.text(.055, .061, "直送 + 预部署 + 中继  ·  200 任务  ·  300 s/次  ·  单种子单次观测（无误差线）", fontsize=14, color=secondary)
    fig.text(.055, .027, "当前稿 6.2 对应历史数据：303e711  |  seed 2026081701  |  三轴基准分别求解  |  线段仅连接观测点", fontsize=12, color=secondary)
    return fig


def axis_style(ax, theme, rows, key, local=False):
    xs, ys = [r["level"] for r in rows], [r[key] for r in rows]
    span = max(xs) - min(xs)
    ax.set_xlim(min(xs) - span * .12, max(xs) + span * .12)
    ax.set_xticks(xs, [level_label(theme, x) for x in xs])
    ax.set_xlabel(THEMES[theme]["axis"], fontsize=16, labelpad=13)
    if local:
        pad = max((max(ys) - min(ys)) * .35, 8)
        ax.set_ylim(min(ys) - pad, max(ys) + pad)
    else:
        ax.set_ylim(0, max(ys) * 1.27 if max(ys) else 1)
    ax.yaxis.set_major_locator(MaxNLocator(nbins=4, integer=key == "late_count"))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.grid(axis="y", color=GRID, linewidth=.8)
    ax.set_axisbelow(True)
    ax.tick_params(axis="both", labelsize=14, length=0, pad=8)
    for edge in ["top", "right", "left"]:
        ax.spines[edge].set_visible(False)
    ax.axvline(THEMES[theme]["base"], color="#B6C0C8", lw=1, ls=(0, (3, 4)), zorder=1)


def label_points(ax, xs, ys, key, theme):
    # Numeric x spacing is preserved. Alternating offsets separate the 8/9/10 labels.
    for i, (x, y) in enumerate(zip(xs, ys)):
        offset = 15
        if key == "distance_km" and i % 2:
            offset = -27
        ax.annotate(value_label(key, y), (x, y), xytext=(0, offset), textcoords="offset points",
                    ha="center", va="center", fontsize=14, color=INK,
                    bbox={"facecolor": BG, "edgecolor": "none", "pad": 1.5}, zorder=6)


def facets(theme):
    rows = records(theme)
    fig = frame(theme, "A · 三指标分面")
    lefts = [.070, .387, .704]
    for i, (key, label, unit, color) in enumerate(METRICS):
        ax = fig.add_axes([lefts[i], .290, .257, .410])
        xs, ys = [r["level"] for r in rows], [r[key] for r in rows]
        local = key == "distance_km"
        axis_style(ax, theme, rows, key, local=local)
        ax.set_title(f"{label}（{unit}）", fontsize=20, loc="left", pad=25, color=INK)
        ax.plot(xs, ys, color=color, lw=2.4, marker=["o", "s", "^"][i], ms=7.5, zorder=3, clip_on=False)
        label_points(ax, xs, ys, key, theme)
        base = next(r for r in rows if r["level"] == THEMES[theme]["base"])
        ax.scatter([base["level"]], [base[key]], marker="D", s=140, facecolors="none", edgecolors=INK, linewidths=1.2, zorder=4)
        if local:
            ax.text(.0, 1.025, "局部纵轴，刻度见左侧", transform=ax.transAxes, fontsize=11, color=MUTED)
    fig.text(.055, .161, "◇ 本轴基准    ·    三项指标均为越低越好；按逾期数、总逾期、航程依次比较，未合成加权分数", fontsize=14, color=MUTED)
    return fig


def service(theme):
    rows = records(theme)
    fig = frame(theme, "B · 服务指标聚焦", THEMES[theme]["headline"])
    ax = fig.add_axes([.085, .290, .845, .425])
    axis_style(ax, theme, rows, "late_count")
    ax.set_ylabel("逾期任务数（单）", fontsize=20, labelpad=15)
    ax.tick_params(axis="both", labelsize=18)
    xs, ys = [r["level"] for r in rows], [r["late_count"] for r in rows]
    ax.vlines(xs, 0, ys, color=BLUE, linewidth=2, alpha=.24)
    ax.plot(xs, ys, color=BLUE, linewidth=3, marker="o", markersize=11, zorder=3, clip_on=False)
    base = next(r for r in rows if r["level"] == THEMES[theme]["base"])
    ax.scatter([base["level"]], [base["late_count"]], marker="D", s=260, facecolors="none", edgecolors=INK, linewidths=1.4, zorder=4)
    for r in rows:
        ax.annotate(f"{r['late_count']:.0f}", (r["level"], r["late_count"]), (0, 22), textcoords="offset points", ha="center", fontsize=27, weight="medium")
    fig.text(.055, .161, "◇ 本轴基准    ·    纵轴从 0 开始    ·    其余两项指标见 A 版，完整数值见交互总览的数据表", fontsize=14, color=MUTED)
    return fig


def matrix(theme):
    rows = records(theme)
    base = next(r for r in rows if r["level"] == THEMES[theme]["base"])
    fig = frame(theme, "C · 基准变化矩阵", THEMES[theme]["headline"], dark=True)
    fig.text(.065, .754, "测试水平", fontsize=17, color="#C3CFD7")
    centers = [.365, .605, .845]
    for center, (_, label, unit, _) in zip(centers, METRICS):
        fig.text(center, .754, f"{label}（{unit}）", ha="center", fontsize=19, color=WHITE)
    for i, r in enumerate(rows):
        y = .642 - i * .105
        level = level_label(theme, r["level"])
        suffix = " 倍" if theme == "deadline_multiplier" else (" 架" if theme == "drone_count" else " 站")
        fig.text(.065, y + .025, level + suffix, fontsize=24, color=WHITE)
        if r["level"] == THEMES[theme]["base"]:
            fig.text(.065, y - .007, "本轴基准", fontsize=13, color="#C3CFD7")
        for center, (key, _, _, _) in zip(centers, METRICS):
            delta = (r[key] / base[key] - 1) * 100
            fill = "#17445C" if delta < -1e-10 else ("#653F29" if delta > 1e-10 else "#273B49")
            fig.patches.append(Rectangle((center - .108, y - .012), .216, .086,
                                        transform=fig.transFigure, facecolor=fill, edgecolor="none"))
            fig.text(center, y + .039, value_label(key, r[key], precise=True), ha="center", fontsize=24, color=WHITE)
            desc = "基准值" if abs(delta) < 1e-10 else f"较基准 {'+' if delta > 0 else '−'}{abs(delta):.2f}%"
            fig.text(center, y + .002, desc, ha="center", fontsize=14, color="#D8E4EB")
    fig.text(.055, .161, "底色表示方向：蓝色低于基准，橙色高于基准，灰色相同；深浅不编码幅度。无跨指标加权。", fontsize=14, color="#C3CFD7")
    return fig


def inspect_text_bounds(fig, name):
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    bounds = fig.bbox
    bad = []
    for obj in fig.findobj(matplotlib.text.Text):
        if not obj.get_visible() or not obj.get_text():
            continue
        box = obj.get_window_extent(renderer)
        if box.x0 < -1 or box.y0 < -1 or box.x1 > bounds.width + 1 or box.y1 > bounds.height + 1:
            bad.append(obj.get_text())
    if bad:
        raise ValueError(f"Text outside canvas in {name}: {bad}")
    return {"file": name, "canvas_text_bounds": "passed", "pixels": [3840, 2160]}


def contact_sheet(names):
    # Review-only image. Each independent slide remains 3840 x 2160.
    canvas = Image.new("RGB", (1920, 1164), "#E9EEF2")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(FONT_PATH, 26)
    for idx, name in enumerate(names):
        x, y = (idx % 3) * 640, (idx // 3) * 388
        thumb = Image.open(ROOT / "png" / f"{name}.png").convert("RGB").resize((624, 351), Image.Resampling.LANCZOS)
        canvas.paste(thumb, (x + 8, y + 29))
        theme, variant = name.rsplit("_", 2)[0], name.rsplit("_", 2)[1]
        draw.text((x + 14, y), f"{THEMES[theme]['name']} · {variant.upper()}", fill=INK, font=font)
    canvas.save(ROOT / "candidate-overview.png")


def main():
    checks, names = [], []
    for theme in THEMES:
        for suffix, build in [("a_facets", facets), ("b_service", service), ("c_matrix", matrix)]:
            name = f"{theme}_{suffix}"
            fig = build(theme)
            checks.append(inspect_text_bounds(fig, name))
            for kind in ["png", "svg"]:
                (ROOT / kind).mkdir(exist_ok=True)
                metadata = {"Creator": "Matplotlib; pinned manuscript data 303e711", "Date": None} if kind == "svg" else {"Software": "Matplotlib; pinned manuscript data 303e711"}
                destination = ROOT / kind / f"{name}.{kind}"
                fig.savefig(destination, dpi=240, facecolor=fig.get_facecolor(), metadata=metadata)
                if kind == "svg":
                    # Preserve SVG path whitespace separators while keeping Git diffs clean.
                    destination.write_text("\n".join(line.rstrip() for line in destination.read_text().splitlines()) + "\n")
            plt.close(fig)
            names.append(name)
    contact_sheet(names)
    report = {"source_commit": DATA["source_commit"], "source_csv_sha256": hashlib.sha256((ROOT / "data/historical_runs.csv").read_bytes()).hexdigest(),
              "figures": checks, "font": FONT_NAME, "matplotlib": matplotlib.__version__,
              "notes": ["SVG文字转轮廓，避免跨平台缺字", "无求解器调用，无插值拟合，无标准差/置信区间", "画布边界自动检查仅作辅助；另需视觉检查"]}
    (ROOT / "render_checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(f"Generated {len(names)} figures in PNG and SVG, plus candidate overview.")


if __name__ == "__main__":
    main()
