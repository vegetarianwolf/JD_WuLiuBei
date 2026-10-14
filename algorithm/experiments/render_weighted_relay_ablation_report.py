"""Offline, source-bound 2x2 relay report; never starts an optimizer.

The performance comparison uses the 12 new equal-budget observations only.
Warmup increments and contraction of a final relay plan are mechanism evidence,
not independent DIRECT samples or a substitute for the paired control arms.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "algorithm/results/weighted_relay_ablation_20261014"
ORDER = ["pure_direct", "direct_relay", "direct_stations", "direct_relay_stations"]
SEEDS = [2026101471, 2026101472, 2026101473]
FIELDS = ("late_count", "total_lateness_min", "distance_km")
MEASURES = ("on_time_count", "on_time_rate", "distance_km", "total_lateness_min", "weighted_score", "end_to_end_seconds")
LABELS = {"pure_direct": "原点直送", "direct_relay": "原点直送＋中继",
          "direct_stations": "预部署直送", "direct_relay_stations": "预部署直送＋中继"}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def within(directory: Path, relative: str) -> Path:
    target = (directory / relative).resolve()
    if not target.is_relative_to(directory.resolve()):
        raise ValueError("证据路径越出实验目录：" + relative)
    return target


def require(value, label):
    if not value:
        raise ValueError(label)


def same(actual, expected, label):
    require(isinstance(actual, (int, float)) and not isinstance(actual, bool)
            and math.isfinite(actual) and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-8), label)


def build_data(directory: Path) -> dict:
    """Read a complete, independently audited batch and verify every binding."""
    directory = directory.resolve()
    manifest, runs, audit = (load(directory / name) for name in ("manifest.json", "runs.json", "audit.json"))
    require(manifest.get("kind") == "weighted_relay_ablation" and manifest.get("schema_version") == 1,
            "需要本轮中继2×2消融协议")
    require(manifest.get("seeds") == SEEDS and manifest.get("order") == ORDER
            and manifest.get("budget_seconds") == 240, "种子、四方案或240秒预算不一致")
    require(audit.get("passed") is True and audit.get("run_count") == 12, "需要全部12次运行的独立审计")
    require(len(runs) == 12 and len(audit.get("results", [])) == 12, "不完整的12格实验不能生成正式报告")
    fingerprints = {}
    for name in ("manifest.json", "runs.json", "audit.json"):
        fingerprints[name] = sha256(directory / name)
        if name != "audit.json":
            require(audit.get(name.removesuffix(".json") + "_sha256") == fingerprints[name], "审计已过期：" + name)
    expected = {(seed, scenario) for seed in SEEDS for scenario in ORDER}
    cells = {(row.get("seed"), row.get("scenario")) for row in runs}
    require(cells == expected, "存在重复、缺失或协议外种子/方案")
    require([row["run_id"] for row in runs] == [cell["run_id"] for cell in manifest["schedule"]], "实测顺序与冻结顺序不一致")
    results = {record["run_id"]: record for record in audit["results"]}
    require(len(results) == 12 and set(results) == {row["run_id"] for row in runs}, "审计结果与原始运行不一致")
    weights, scales = manifest["weights"], manifest["scales"]
    require(weights == [.5, .25, .25] and len(scales) == 3
            and all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in scales), "缺少冻结的共同J权重和尺度")
    source = manifest["source_archive"]
    require(sha256(within(directory, source["path"])) == source["sha256"], "求解器快照改变")
    fingerprints[source["path"]] = source["sha256"]
    for record in manifest["execution_sources"].values():
        require(sha256(within(directory, record["path"])) == record["sha256"], "冻结执行器改变")
        fingerprints[record["path"]] = record["sha256"]
    for path_key, hash_key in (("input_path", "input_sha256"), ("problem_path", "problem_sha256"),
                               ("parent_manifest_path", "parent_manifest_sha256")):
        path = within(ROOT, manifest[path_key])
        require(sha256(path) == manifest[hash_key], "题目、输入或父批指纹改变：" + path_key)
        fingerprints[manifest[path_key]] = manifest[hash_key]
    parent_audit = within(ROOT, manifest["parent_manifest_path"]).parent / "audit.json"
    require(sha256(parent_audit) == manifest["parent_audit_sha256"], "父批审计指纹改变")
    # This loader is standard-library only and is the new audit's independent
    # CSV parser, not the production solver's data loader.
    try:
        from .audit_weighted_direct_pair import load_official_csv
    except ImportError:
        from audit_weighted_direct_pair import load_official_csv
    tasks = load_official_csv(within(ROOT, manifest["input_path"]))
    require(len(tasks) == 200, "必须使用完整官方200任务")
    topologies = {}
    for name in ORDER:
        case = manifest["scenarios"][name]
        path = within(directory, case["topology_path"])
        require(sha256(path) == case["topology_sha256"], "拓扑改变：" + name)
        topologies[name] = load(path)
        fingerprints[case["topology_path"]] = case["topology_sha256"]
    compact = []
    mechanisms = []
    for row in runs:
        require(row.get("valid") is True and row.get("budget_compliant") is True
                and row.get("delivered_count") == 200 and row.get("objective") == "weighted", "无效、不完整或超预算运行")
        require(row["source_sha256"] == source["sha256"], "运行源码不同")
        case = manifest["scenarios"][row["scenario"]]
        require(row["topology_sha256"] == case["topology_sha256"]
                and row["initial_routes_sha256"] == case["initial_routes_sha256"], "运行拓扑或初解不同")
        for field in (*FIELDS, *MEASURES, "construction_seconds", "search_seconds"):
            require(isinstance(row[field], (int, float)) and not isinstance(row[field], bool)
                    and math.isfinite(row[field]), "非有限指标：" + field)
        same(row["on_time_count"], 200 - row["late_count"], "按时数与逾期数不一致")
        same(row["on_time_rate"], row["on_time_count"] / 200, "按时率不一致")
        same(row["weighted_score"], sum(w * row[k] / s for k, w, s in zip(FIELDS, weights, scales)), "保存的J不一致")
        require(row["end_to_end_seconds"] <= 240, "端到端超过240秒")
        reference = results[row["run_id"]]
        require(reference.get("passed") is True and reference.get("scenario") == row["scenario"]
                and reference.get("seed") == row["seed"], "独立审计单元不一致")
        for field in FIELDS:
            same(reference["reference"]["score"][field], row[field], "审计指标不一致：" + field)
        for path_key, hash_key in (("solution_path", "solution_sha256"), ("counterfactual_path", "counterfactual_sha256")):
            require(sha256(within(directory, row[path_key])) == row[hash_key], "解或反事实改变：" + row[path_key])
            fingerprints[row[path_key]] = row[hash_key]
        saved = load(within(directory, row["solution_path"]))
        counter_saved = load(within(directory, row["counterfactual_path"]))
        for kind, payload in (("reference", saved), ("counterfactual", counter_saved)):
            independently_scored = reference[kind]
            require(set(payload["delivery_times_min"]) == set(independently_scored["delivery_times_min"]),
                    "独立事件回放的任务覆盖改变")
            for task_id, at in payload["delivery_times_min"].items():
                same(independently_scored["delivery_times_min"][task_id], at, "独立交付时刻与完整解不同")
            for field in FIELDS:
                same(independently_scored["score"][field], payload["score"][field], "独立反事实或最终评分不同")
        phase_artifacts = row.get("phase_artifacts", [])
        require(phase_artifacts and phase_artifacts[0]["phase"] == "initial", "缺少完整构造初解")
        require(len(reference["phases"]) == len(phase_artifacts), "完整阶段独立结果数量不同")
        for record, phase_reference in zip(phase_artifacts, reference["phases"], strict=True):
            require(sha256(within(directory, record["solution_path"])) == record["solution_sha256"], "阶段完整解改变")
            require(phase_reference.get("passed") is True
                    and all(phase_reference.get(field) == record[field] for field in ("phase", "solution_path", "solution_sha256")),
                    "阶段独立核验与原始完整解不一致")
            for field in FIELDS:
                same(phase_reference["reference"]["score"][field], record["score"][field], "阶段独立评分不一致")
            fingerprints[record["solution_path"]] = record["solution_sha256"]
        phases = row["phases"]
        phase_wall = sum(p["solve_wall_seconds"] for p in phases)
        require(phase_wall + row["construction_seconds"] <= row["end_to_end_seconds"] + 1e-7, "阶段计时重叠或不一致")
        compact.append({**{k: row[k] for k in ("run_id", "scenario", "seed", "label", "home_group", "relay_enabled",
            "budget_seconds", "end_to_end_seconds", "construction_seconds", "search_seconds", "iterations",
            "initial_routes_sha256", "solution_path", "solution_sha256", "counterfactual_path", "counterfactual_sha256",
            "weighted_score", "on_time_count", "on_time_rate", *FIELDS)},
            "phases": phases, "relay_statistics": reference["reference"]["relay_statistics"],
            "periodic_counts": {k: v for k, v in row["periodic_task_exchange"].items() if k != "events"},
            "other_seconds": max(0., row["end_to_end_seconds"] - row["construction_seconds"] - phase_wall),
            "warmup_incremental": reference.get("warmup_incremental"),
            "counterfactual_delta": reference.get("counterfactual_delta")})
        if row["relay_enabled"]:
            events = [[{k: event[k] for k in ("task_id", "action", "location_km", "arrival_time_min")}
                       for event in route["visits"]] for route in saved["routes"]]
            direct_name = "direct_stations" if row["home_group"] == "stations" else "pure_direct"
            paired_direct = next(r for r in runs if r["scenario"] == direct_name and r["seed"] == row["seed"])
            paired_saved = load(within(directory, paired_direct["solution_path"]))
            matching_routes = [r["encoded_visits"] for r in counter_saved["routes"]] == [
                r["encoded_visits"] for r in paired_saved["routes"]]
            if matching_routes:
                for field in FIELDS:
                    same(reference["counterfactual"]["score"][field], paired_direct[field],
                         "相同直接化路线与正式DIRECT指标不同")
            mechanisms.append(dict(run_id=row["run_id"], scenario=row["scenario"], seed=row["seed"],
                reference=reference["reference"], counterfactual=reference["counterfactual"], events=events,
                counterfactual_delta=reference.get("counterfactual_delta"),
                paired_direct_run_id=paired_direct["run_id"], paired_direct_path=paired_direct["solution_path"],
                counterfactual_routes_match_paired_direct=matching_routes))
    pairs = []
    for group, names in (("origin", ORDER[:2]), ("stations", ORDER[2:])):
        for seed in SEEDS:
            base, relay = [next(r for r in compact if r["scenario"] == name and r["seed"] == seed) for name in names]
            require(base["initial_routes_sha256"] == relay["initial_routes_sha256"], "相同起点内初解不同")
            require(topologies[names[0]]["home_points"] == topologies[names[1]]["home_points"], "相同起点内起点不同")
            pairs.append(dict(home_group=group, seed=seed, direct=base, relay=relay,
                              delta={k: relay[k] - base[k] for k in MEASURES},
                              verdict="win" if relay["weighted_score"] < base["weighted_score"] else
                                      "loss" if relay["weighted_score"] > base["weighted_score"] else "tie"))
    summary = []
    for name in ORDER:
        group = [row for row in compact if row["scenario"] == name]
        summary.append(dict(scenario=name, label=LABELS[name], runs=3,
            means={k: mean(r[k] for r in group) for k in MEASURES},
            ranges={k: [min(r[k] for r in group), max(r[k] for r in group)] for k in MEASURES}))
    figures = {}
    figure_record_path = directory / "figures/validation.json"
    if figure_record_path.exists():
        figures = load(figure_record_path)
        require(figures.get("passed") is True and figures.get("unique_pairs") == 6,
                "论文图必须绑定完整六个配对")
        for key, source_name in (("source_manifest_sha256", "manifest.json"),
                                 ("source_runs_sha256", "runs.json"), ("source_audit_sha256", "audit.json")):
            require(figures.get(key) == fingerprints[source_name], "论文图来源已过期：" + source_name)
        require(figures.get("exporter_sha256") == sha256(ROOT / "algorithm/experiments/export_weighted_relay_figures.py"),
                "论文图导出脚本改变")
        require(set(figures.get("outputs", {})) == {"relay_paired_effects.svg", "relay_paired_effects.png", "paired_effects.csv"},
                "论文图输出清单不同")
        for name, fingerprint in figures["outputs"].items():
            path = within(directory / "figures", name)
            require(sha256(path) == fingerprint, "论文图或数据指纹改变：" + name)
            fingerprints["figures/" + name] = fingerprint
        fingerprints["figures/validation.json"] = sha256(figure_record_path)
    context = {}
    for name in ("README.md", "provenance.json", "execution_notes.json", "summary.json"):
        path = directory / name
        if path.exists():
            context[name] = load(path) if path.suffix == ".json" else path.read_text(encoding="utf-8")
            fingerprints[name] = sha256(path)
    for name in ("runs.csv", "paired_summary.csv"):
        path = directory / name
        if path.exists():
            fingerprints[name] = sha256(path)
    return dict(manifest=manifest, rows=compact, audit_summary=audit.get("summary", {}), pairs=pairs,
                summary=summary, mechanisms=mechanisms, topologies=topologies,
                tasks=[dict(id=i, pickup=t[0], delivery=t[1], deadline_min=t[2]) for i, t in sorted(tasks.items())],
                fingerprints=fingerprints, context=context, figures=figures, renderer_sha256=sha256(Path(__file__)))


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="同一加权目标，三固定种子、四方案、每次240秒；独立审计的中继性能对照与真实跨机交接机制。">
<title>中继派送 · 同预算2×2消融实验</title>
<style>
:root{color-scheme:light dark;--bg:light-dark(#fbfcfc,#171d21);--fg:light-dark(#26323a,#edf1f3);--muted:light-dark(#566775,#bbc7ce);--line:light-dark(#d4dde2,#41505a);--soft:light-dark(#eaf0f2,#253039);--direct:light-dark(#326c99,#95c1df);--relay:light-dark(#187b64,#91d7bb);--good:light-dark(#176a48,#94d6b4);--bad:light-dark(#a13738,#efa7a7);--warm:light-dark(#7994aa,#819db1);--other:light-dark(#b0bbc2,#6b7781);--cache:light-dark(#8f7035,#d7bc86);--wait:light-dark(#a14d41,#e8aa9f)}
*{box-sizing:border-box}body{margin:0;color:var(--fg);background:var(--bg);font:400 15px/1.65 system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif}main{max-width:1220px;margin:auto;padding:34px 30px 45px}header{padding-bottom:18px;border-bottom:1px solid var(--line)}h1,h2,h3,strong{font-weight:500}h1{font-size:clamp(28px,4vw,38px);line-height:1.3;margin:8px 0 14px}h2{font-size:22px;line-height:1.4;margin:0 0 10px}h3{font-size:16px;line-height:1.5;margin:0 0 7px}p{margin:8px 0}.eyebrow,.caption,.muted{color:var(--muted)}.eyebrow{font-size:13px}.caption{font-size:13px}.intro{max-width:960px}.section{padding-top:24px;margin-top:28px;border-top:1px solid var(--line)}.outcome{padding:18px 0 2px}.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:26px 34px;margin:20px 0}.grid>figure{min-width:0}figure{margin:0}.chart{display:block;width:100%;overflow:hidden}.chart text{fill:var(--fg);font:12px system-ui,-apple-system,"PingFang SC",sans-serif}.chart .axis{fill:var(--muted)}.chart .frame{fill:none;stroke:var(--line);stroke-width:1}.chart .gridline{stroke:var(--line);stroke-width:1}.legend{display:flex;flex-wrap:wrap;gap:10px 24px;margin:12px 0;font-size:13px}.legend span{display:flex;align-items:center;gap:7px}.symbol{display:inline-block;width:10px;height:10px;background:var(--direct)}.symbol.relay{background:var(--relay);border-radius:50%}.controls{display:flex;flex-wrap:wrap;gap:14px 22px;margin:15px 0}.controls label{display:grid;gap:4px;max-width:100%}select{font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:4px;padding:8px 10px;min-height:42px;max-width:100%}.table-wrap{max-width:100%;overflow-x:auto;margin:12px 0}table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}th,td{text-align:right;padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top;white-space:nowrap}th{font-weight:500;color:var(--muted)}th:first-child,td:first-child{text-align:left}caption{text-align:left;font-size:13px;color:var(--muted);margin-bottom:8px}.textcell{white-space:normal;text-align:left;max-width:520px}.good{color:var(--good)}.bad{color:var(--bad)}.number{font-variant-numeric:tabular-nums}.links{display:flex;flex-wrap:wrap;gap:7px 20px;font-size:13px}a{color:var(--direct);overflow-wrap:anywhere;text-underline-offset:3px}details{margin:14px 0}summary{font-weight:500;min-height:34px;padding:5px 0}pre{font:12px/1.65 ui-monospace,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--soft);padding:14px;color:var(--muted)}.formula{font:14px/1.7 ui-monospace,Consolas,monospace;padding:13px;background:var(--soft);overflow-wrap:anywhere;white-space:pre-line}.status{padding:8px 0}.mechanism-note{min-height:50px}.source{overflow-wrap:anywhere}.no-events{padding:25px 0}.hide{display:none}footer{font-size:13px;color:var(--muted);border-top:1px solid var(--line);margin-top:30px;padding-top:16px}@media(max-width:760px){main{padding:25px 16px 35px}.grid{grid-template-columns:1fr;gap:24px}h2{font-size:20px}.controls label{width:100%}th,td{padding:8px 7px}}@media print{.controls{display:none}main{max-width:none}.grid{grid-template-columns:1fr 1fr}section{break-inside:avoid}details>*{display:block}}
</style></head><body><main id="relay-ablation-report">
<header><div class="eyebrow">京东物流杯 · 中继派送研究 · 2026-10-14</div><h1>中继是否带来同预算的实际收益？</h1>
<p class="intro">使用当前加权算法，在原点和固定预部署两种起点设置内，分别比较只直送与允许中继。三枚种子在运行前固定，四方案每次均有240秒端到端上限，全部12次结果原样保留。</p><p class="caption" id="audit-state"></p></header>
<section class="outcome" aria-labelledby="conclusion-heading"><h2 id="conclusion-heading">本批结果支持到什么程度</h2><p id="conclusion"></p><p class="caption">胜平负按同一未舍入研究J比较。按时完成、航程和求解耗时是题目直接关注指标；总逾期分钟为项目次级指标，J不是官方评分。三个种子用于检查本实例的变化，不作显著性或跨实例普适性结论。</p></section>
<section class="section" aria-labelledby="factor-heading"><h2 id="factor-heading">2×2对照：中继与起点设置分别看</h2><div class="table-wrap"><table id="factor-table"></table></div><p class="caption">表中是三个种子的均值，全部原始值在后文展开。中继效果只在相同固定起点内比较。跨起点比较同时含预部署和既有起点构造/偏置设置，不把预部署收益归给中继。</p></section>
<section class="section" aria-labelledby="paired-heading"><h2 id="paired-heading">逐种子配对，保留每次取舍</h2><div class="legend"><span><i class="symbol" aria-hidden="true"></i>只直送 D</span><span><i class="symbol relay" aria-hidden="true"></i>允许中继 R</span></div><div class="grid" id="paired-plots"></div><div class="table-wrap"><table id="paired-table"></table></div><p class="caption">每组横线连接同起点、同种子的两个真实终点。坐标轴根据完整观测范围设置，并非所有面板从零开始；直接标签给出D→R原值。差值统一为R−D，均值不能抵消个别种子的退化。</p><div class="links" id="figure-links"></div></section>
<section class="section" aria-labelledby="budget-heading"><h2 id="budget-heading">同总预算下，时间实际花在哪里</h2><div id="budget-plots" class="grid"></div><div class="table-wrap"><table id="budget-table"></table></div><p class="caption">Relay沿用当前95% DIRECT预热，再从同一总预算内分配约12秒给中继。完整阶段解均保存并独立核验。预热运行在带有Relay计划索引的Problem上，有额外检查成本，因此不能假定其搜索前缀与纯DIRECT相同。周期请求交换仅在DIRECT阶段生效；真正Relay阶段跳过它。图中的“其他”包含阶段核验、阶段写出、初解写出、终局检查及反事实写出，不能与阶段求解重复相加。</p></section>
<section class="section" aria-labelledby="relay-heading"><h2 id="relay-heading">是否真的发生跨机派送</h2><div class="table-wrap"><table id="handoff-summary"></table></div><p class="caption">跨机由同一任务入站和出站两段是否属于不同无人机判断。同机途经站点单独计数，不当作协同接力。接收机早到产生飞机等待；包裹先到产生站点缓存，两者不能混算。</p><h3>一个可核对的跨机改进案例</h3><p id="concrete-case"></p>
<div class="controls"><label>观察真实终局<select id="mechanism-run"></select></label><label>交接任务<select id="handoff-task"></select></label></div><p class="mechanism-note number" id="handoff-detail" aria-live="polite"></p><div id="handoff-empty" class="no-events hide"></div><div class="grid" id="handoff-figures"><figure><h3>任务空间路径 · 原始公里坐标</h3><svg id="task-map" class="chart" role="img"></svg><p class="caption">只画所选任务的取件→中继站→交付路径示意，以及两架机起点；不把直线当作无人机整条实际访问路线，不是地理底图。</p></figure><figure><h3>异步交接时刻</h3><svg id="handoff-timeline" class="chart" role="img"></svg><p class="caption">包裹可先卸后取，无需两机同时在场。交接就绪时间为入站和接收机到站两者的较大值；当前扩展假定零处理时间、无限站点缓存。</p></figure></div></section>
<section class="section" aria-labelledby="mechanism-heading"><h2 id="mechanism-heading">中继的机制作用：同一终局结构的直接化反事实</h2><p>从所选Relay终局删除入站事件，在原出站事件位置改为完整DIRECT取送，保留最终责任分机和其他访问顺序，再完整回放200任务。这里没有重新优化，也没有新增算法样本；它解释这套既定计划中的中继作用，主性能结论仍由上面的240秒独立直送臂决定。反事实同时移除跨机接力和同机站点缓存，不能作为只移除跨机交接的专项消融。</p><p id="counterfactual-summary" class="number" aria-live="polite"></p><div class="grid"><figure><h3>每项任务交付时刻的变化</h3><svg id="task-effects" class="chart" role="img"></svg><p class="caption">纵轴为Relay−结构直接化交付时刻，负值更早。圆点为终局DIRECT任务，菱形为终局Relay任务；即使一个任务仍是DIRECT，也可能因同机后续访问和跨机依赖受影响。显示全200项，未择优挑任务。</p></figure><figure><h3>完整200项回放的指标取舍</h3><svg id="counterfactual-metrics" class="chart" role="img"></svg><p class="caption">不同单位分别列原值与差值，不把分钟、公里和项数放在同一个长度尺度上。反事实全部通过容量、K、取送先后和完整交付核验。</p></figure></div><details><summary>查看全部200任务时刻与逾期变化</summary><div class="table-wrap"><table id="task-effects-table"></table></div></details><details><summary>每个Relay运行自己的DIRECT预热→最终解</summary><div class="table-wrap"><table id="increment-table"></table></div><p class="caption">这是较短预热与该运行后续阶段的增量，阶段完整路线已独立复算；它不能替代使用完整240秒的配对DIRECT臂，也不作为额外独立种子。</p></details></section>
<section class="section" aria-labelledby="protocol-heading"><h2 id="protocol-heading">论文表述与可重放证据</h2><p id="paper-conclusion"></p><div class="formula" id="formula"></div><p>本批使用官方完整200任务、8架无人机、载荷2、速度0.9 km/min、开放路线。原题DIRECT要求同机先取后送。本论文中继扩展的K按最终交付责任计数，前半程转运不占K；原点中继仍属于扩展模型。预部署起点及四个站点固定生成，同起点内两臂起点和DIRECT初解完全相同。</p><p>所有新臂均使用冻结的同一加权求解器源码，种子2026101471—2026101473按预定顺序轮换方案。没有结果门槛、换种子、择优重跑或混入历史词典序、不同预算结果。生产默认不因本批观察而改动。主机并非独占环境，实测墙钟受环境影响。</p><div class="links" id="evidence-links"></div><details><summary>完整实验协议与来源指纹</summary><pre id="protocol"></pre><pre id="fingerprints"></pre></details><details><summary>12次完整原始成绩</summary><div class="table-wrap"><table id="raw-table"></table></div></details><noscript>启用JavaScript才能查看内嵌图表。全部原始记录见同目录runs.json、完整解与audit.json。</noscript></section>
<footer>离线报告；不请求任何网络资源。独立审计只使用标准库和官方CSV几何/冻结注册表回放，不调用生产求解器。最优分数轨迹为遥测，保存的完整初解、阶段解、终局与结构直接化解才具有逐事件独立核验证据。</footer>
</main><script type="application/json" id="report-data">__DATA__</script><script>
(()=>{'use strict';
const D=JSON.parse(document.getElementById('report-data').textContent),$=id=>document.getElementById(id),N=200;
const svgNS='http://www.w3.org/2000/svg',names={on_time_count:'按时完成',on_time_rate:'按时率',distance_km:'总航程',total_lateness_min:'总逾期 · 项目次级',weighted_score:'加权研究J',end_to_end_seconds:'端到端实测'},units={on_time_count:'项',on_time_rate:'%',distance_km:'km',total_lateness_min:'min',weighted_score:'J',end_to_end_seconds:'s'},digits={on_time_count:0,on_time_rate:2,distance_km:3,total_lateness_min:3,weighted_score:6,end_to_end_seconds:3};
const short={pure_direct:'原点D',direct_relay:'原点R',direct_stations:'预部署D',direct_relay_stations:'预部署R'},homes={origin:'原点',stations:'预部署'},taskById=new Map(D.tasks.map(t=>[t.id,t]));
const esc=v=>String(v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(v,n=2)=>Number(v).toLocaleString('zh-CN',{minimumFractionDigits:n,maximumFractionDigits:n}),signed=(v,n=2)=>(v>0?'+':'')+fmt(v,n),metric=(r,k)=>k==='on_time_rate'?r[k]*100:r[k],deltaMetric=(d,k)=>k==='on_time_rate'?d[k]*100:d[k],direction=k=>k==='on_time_count'||k==='on_time_rate'?1:-1;
const t=(x,y,label,anchor='start',klass='')=>`<text x="${x}" y="${y}" text-anchor="${anchor}"${klass?` class="${klass}"`:''}>${esc(label)}</text>`;
const line=(x1,y1,x2,y2,style='')=>`<line x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" ${/\bstroke=/.test(style)?'':'stroke="var(--line)"'} ${/\bstroke-width=/.test(style)?'':'stroke-width="1"'} ${style}/>`;
const dot=(x,y,relay,r=4)=>relay?`<circle cx="${x}" cy="${y}" r="${r}" fill="var(--relay)"/>`:`<rect x="${x-r}" y="${y-r}" width="${r*2}" height="${r*2}" fill="var(--direct)"/>`;
function extent(values,minimumPad=1){let lo=Math.min(...values),hi=Math.max(...values),pad=Math.max((hi-lo)*.13,minimumPad);return [lo-pad,hi+pad];}
function ticks(lo,hi,n=4){return Array.from({length:n},(_,i)=>lo+(hi-lo)*i/(n-1));}
function frame(svg,title,width,height){svg.setAttribute('viewBox',`0 0 ${width} ${height}`);svg.setAttribute('aria-label',title);return `<title>${esc(title)}</title>`;}
function figure(parent,id,title,note=''){const f=document.createElement('figure');f.innerHTML=`<h3>${esc(title)}</h3><svg id="${id}" class="chart" role="img"></svg>${note?`<p class="caption">${esc(note)}</p>`:''}`;$(parent).append(f);return f;}
function table(el,caption,heads,rows){$(el).innerHTML=`<caption>${esc(caption)}</caption><thead><tr>${heads.map(h=>'<th>'+esc(h)+'</th>').join('')}</tr></thead><tbody>${rows.map(r=>'<tr>'+r.map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')}</tbody>`;}
function score(s){return D.manifest.weights.reduce((a,w,i)=>a+w*s[['late_count','total_lateness_min','distance_km'][i]]/D.manifest.scales[i],0);}
function counts(ps){return ['win','tie','loss'].map(v=>ps.filter(p=>p.verdict===v).length);}
function classDelta(value,key){return value===0||key==='end_to_end_seconds'?'':value*direction(key)>0?'good':'bad';}
const conclusions=['origin','stations'].map(h=>{const ps=D.pairs.filter(p=>p.home_group===h),[w,e,l]=counts(ps),at=ps.reduce((a,p)=>a+p.delta.on_time_count,0)/3,j=ps.reduce((a,p)=>a+p.delta.weighted_score,0)/3,km=ps.reduce((a,p)=>a+p.delta.distance_km,0)/3;return `${homes[h]}固定起点：J为${w}胜/${e}平/${l}负，平均按时${signed(at)}项，航程${signed(km,3)} km，J ${signed(j,6)}。`;});
const crosses=D.mechanisms.reduce((a,m)=>a+m.reference.relay_statistics.cross_uav_handoff_count,0);
const stationPairs=D.pairs.filter(p=>p.home_group==='stations'),originPairs=D.pairs.filter(p=>p.home_group==='origin'),stationDistanceConsistent=stationPairs.every(p=>p.delta.distance_km<0),stationOnTimeUnchanged=stationPairs.every(p=>p.delta.on_time_count===0),stationMeanDistance=stationPairs.reduce((a,p)=>a+p.delta.distance_km,0)/3,stationBaseDistance=stationPairs.reduce((a,p)=>a+p.direct.distance_km,0)/3;
const limitedBenefit=stationDistanceConsistent&&stationOnTimeUnchanged?`本实例的预部署组显示限定的里程收益：三个种子航程均减少，平均减少${fmt(-stationMeanDistance,3)} km（${fmt(-stationMeanDistance/stationBaseDistance*100,3)}%），按时完成数均持平；加权J并非逐种子都改善。 `:'';
$('conclusion').textContent=limitedBenefit+conclusions.join(' ') + (crosses?` 六个Relay终局合计出现${crosses}个跨机交接任务记录；这些是运行内机制记录，不是新增样本。`:' 六个Relay终局都未保留跨机交接，当前终局证据尚不能支持跨机协同这一机制。');
$('audit-state').textContent='12/12次完整实测与全部阶段、结构直接化解通过独立核验；每次≤240 s，全部200项完成。三枚固定种子 × 四方案；总上限48分钟。';
const tableFields=['on_time_count','distance_km','weighted_score','total_lateness_min','end_to_end_seconds'];
const meanDigits=k=>k==='on_time_count'?2:digits[k];
table('factor-table','同一研究J；每个格子n=3，仅描述本实例观测',['起点设置','中继关闭 · 三种子均值','中继允许 · 三种子均值','R−D · 三种子均值'],['origin','stations'].map((h,i)=>{const b=D.summary[i*2],r=D.summary[i*2+1],ps=D.pairs.filter(p=>p.home_group===h),[w,e,l]=counts(ps);return [homes[h],tableFields.map(k=>`${names[k]} ${fmt(b.means[k],meanDigits(k))} ${units[k]}`).join('<br>'),tableFields.map(k=>`${names[k]} ${fmt(r.means[k],meanDigits(k))} ${units[k]}`).join('<br>'),tableFields.map(k=>`<span class="${classDelta(r.means[k]-b.means[k],k)}">${names[k]} ${signed(r.means[k]-b.means[k],meanDigits(k))} ${units[k]}</span>`).join('<br>')+`<br>J ${w}胜/${e}平/${l}负`];}));
const pairKeys=['on_time_count','distance_km','weighted_score','total_lateness_min','end_to_end_seconds'];
pairKeys.forEach((k,i)=>figure('paired-plots','paired-'+k,k==='end_to_end_seconds'?'实际用时 · 相同240秒上限':names[k]+' · '+(direction(k)>0?'↑ 越高越好':'↓ 越低越好'),i===4?'构造、所有搜索阶段、完整核验和全部解写出均计入；没有给中继额外总预算。这些运行均消耗近完整预算，微小墙钟差异不支持求解速度优劣。':''));
function drawPairs(){pairKeys.forEach(k=>{
 const svg=$('paired-'+k),w=svg.parentElement.clientWidth,left=68,right=w-13,h=455,bottom=412,vs=D.pairs.flatMap(p=>[metric(p.direct,k),metric(p.relay,k)]),minPad=k==='weighted_score'?.001:k==='end_to_end_seconds'?.05:1,[lo,hi]=extent(vs,minPad),x=v=>left+(v-lo)/(hi-lo)*(right-left),nt=w<400?3:4;
 let s=frame(svg,names[k]+'，六个真实配对，每个起点三种子',w,h);
 s+=`<rect data-chart-frame x="${left}" y="24" width="${right-left}" height="${bottom-24}" class="frame"/>`;
 ticks(lo,hi,nt).forEach((v,i)=>{s+=line(x(v),24,x(v),bottom,'class="gridline"')+t(x(v),bottom+20,fmt(v,k==='weighted_score'?3:k==='on_time_count'?0:1),i===0?'start':i===nt-1?'end':'middle','axis');});
 D.pairs.forEach((p,i)=>{const y=48+i*59,a=metric(p.direct,k),b=metric(p.relay,k);s+=t(0,y,homes[p.home_group])+t(0,y+17,String(p.seed).slice(-4),'start','axis')+t(left+6,y,`D ${fmt(a,digits[k])} → R ${fmt(b,digits[k])}`)+line(x(a),y+22,x(b),y+22,'stroke="var(--muted)"')+dot(x(a),y+22,false)+dot(x(b),y+22,true);});
 s+=t((left+right)/2,h-3,names[k]+' / '+units[k],'middle');svg.innerHTML=s;
});}
table('paired-table','每对为允许中继减去直送；按原始J判断胜平负',['起点','种子','J胜平负','按时 Δ项','航程 Δkm','总逾期 Δmin','J Δ','端到端 Δs'],D.pairs.map(p=>[homes[p.home_group],p.seed,({win:'胜',tie:'平',loss:'负'})[p.verdict],...['on_time_count','distance_km','total_lateness_min','weighted_score','end_to_end_seconds'].map(k=>`<span class="${classDelta(p.delta[k],k)}">${signed(p.delta[k],digits[k])}</span>`)]));
['origin','stations'].forEach(h=>figure('budget-plots','budget-'+h,homes[h]+' · 真实阶段时长'));
function drawBudget(){['origin','stations'].forEach(h=>{const svg=$('budget-'+h),w=svg.parentElement.clientWidth,left=73,right=w-15,plotW=right-left,rs=D.rows.filter(r=>r.home_group===h),height=rs.length*50+65;let s=frame(svg,'实际阶段时间，秒；'+homes[h],w,height);const colors=['var(--other)','var(--direct)','var(--relay)','var(--other)'];rs.forEach((r,i)=>{const y=23+i*50;let seq=[{v:r.construction_seconds,c:colors[0]},{v:r.phases.find(p=>p.phase==='direct_search'||p.phase==='direct_warmup')?.solve_wall_seconds||0,c:colors[1]},{v:r.phases.find(p=>p.phase==='relay_search')?.solve_wall_seconds||0,c:colors[2]},{v:r.other_seconds,c:colors[3]}],at=left;s+=t(0,y+9,short[r.scenario])+t(0,y+26,String(r.seed).slice(-4),'start','axis')+t(right,y-1,fmt(r.end_to_end_seconds,3)+' s','end');seq.forEach(a=>{const size=a.v/240*plotW;s+=`<rect x="${at}" y="${y+9}" width="${size}" height="15" fill="${a.c}"/>`;at+=size;});});const b=height-29;s+=line(left,b,right,b);[0,120,240].forEach(v=>s+=t(left+v/240*plotW,b+19,v+' s',v===0?'start':v===240?'end':'middle','axis'));svg.innerHTML=s;});}
table('budget-table','阶段均为实际测量，配置目标与实测分开；其他时间不重复包含求解',['方案','种子','构造 s','DIRECT求解 s','Relay求解 s','其他 s','总时长 s'],D.rows.map(r=>[short[r.scenario],r.seed,fmt(r.construction_seconds,3),fmt(r.phases.find(p=>p.phase==='direct_search'||p.phase==='direct_warmup')?.solve_wall_seconds||0,3),fmt(r.phases.find(p=>p.phase==='relay_search')?.solve_wall_seconds||0,3),fmt(r.other_seconds,3),fmt(r.end_to_end_seconds,3)]));
const budgetLegend=document.createElement('p');budgetLegend.className='caption';budgetLegend.innerHTML='<span style="color:var(--direct)">■ DIRECT</span>　<span style="color:var(--relay)">■ Relay</span>　<span style="color:var(--muted)">■ 构造/其他核验与写出</span>';$('budget-plots').after(budgetLegend);
table('handoff-summary','六个Relay终局；飞机等待与站点缓存均为全局DAG独立回放值',['方案','种子','Relay任务','跨机','同机','飞机等待 min','包裹缓存 min'],D.mechanisms.map(m=>{const r=m.reference.relay_statistics;return [short[m.scenario],m.seed,r.relay_task_count,r.cross_uav_handoff_count,r.same_uav_relay_count,fmt(r.total_relay_waiting_time,3),fmt(r.total_package_cache_time,3)];}));
$('mechanism-run').innerHTML=D.mechanisms.map(m=>`<option value="${esc(m.run_id)}">${esc(short[m.scenario])} · ${m.seed}</option>`).join('');
const concreteCase=D.mechanisms.find(m=>m.counterfactual_routes_match_paired_direct&&m.reference.relay_statistics.cross_uav_handoff_count>0&&m.reference.relay_statistics.same_uav_relay_count===0&&score(m.reference.score)<score(m.counterfactual.score));
if(concreteCase){const m=concreteCase,a=m.reference.score,b=m.counterfactual.score,hs=m.reference.handoffs;$('mechanism-run').value=m.run_id;$('concrete-case').textContent=`${short[m.scenario]} · ${m.seed}：完整终局仅有${hs.length}个Relay任务（${hs.map(h=>`任务${h.task_id}，机${h.inbound_drone_id}→机${h.outbound_drone_id}，站${h.relay_id}`).join('；')}），没有同机缓存计划。把该终局直接化后的全部路线，逐个访问事件与同种子完整240秒DIRECT对照完全相同。相对这一真实直送对照，按时${N-b.late_count}→${N-a.late_count}项，航程${signed(a.distance_km-b.distance_km,6)} km，总逾期${signed(a.total_lateness_min-b.total_lateness_min,6)} min，J ${signed(score(a)-score(b),9)}。这是本批可行跨机改善案例；它不抵消其他配对的负例，也不证明普遍更优。`;}else{$('concrete-case').textContent='本批未出现结构直接化路线与正式DIRECT对照完全相同且J改善的纯跨机案例；其他机制观察仍需与完整预算配对结果分开解释。';}
function selected(){return D.mechanisms.find(m=>m.run_id===$('mechanism-run').value)||D.mechanisms[0];}
function chosenHandoff(m){return m.reference.handoffs.find(h=>String(h.task_id)===$('handoff-task').value)||m.reference.handoffs[0];}
function updateHandoffOptions(){const m=selected(),hs=[...m.reference.handoffs].sort((a,b)=>Number(b.cross_uav)-Number(a.cross_uav)||a.task_id-b.task_id);$('handoff-task').innerHTML=hs.map(h=>`<option value="${h.task_id}">任务${h.task_id} · ${h.cross_uav?'跨机':'同机'} ${h.inbound_drone_id}→${h.outbound_drone_id}</option>`).join('');$('handoff-task').disabled=m.reference.handoffs.length===0;updateMechanism();}
function taskEvents(m,h){return m.events.flatMap((events,i)=>events.filter(e=>e.task_id===h.task_id).map(e=>({...e,drone_id:i+1})));}
function drawMap(m,h){
 const svg=$('task-map'),w=svg.parentElement.clientWidth,height=320,left=51,right=w-18,top=22,bottom=height-45,task=taskById.get(h.task_id),topo=D.topologies[m.scenario],station=topo.stations.find(s=>s[0]===h.relay_id).slice(1),donor=topo.home_points[h.inbound_drone_id-1],receiver=topo.home_points[h.outbound_drone_id-1],points=[task.pickup,task.delivery,station,donor,receiver];
 let [xmin,xmax]=extent(points.map(p=>p[0]),1),[ymin,ymax]=extent(points.map(p=>p[1]),1);
 const cx=(xmin+xmax)/2,cy=(ymin+ymax)/2,perPixel=Math.max((xmax-xmin)/(right-left),(ymax-ymin)/(bottom-top));
 xmin=cx-perPixel*(right-left)/2;xmax=cx+perPixel*(right-left)/2;ymin=cy-perPixel*(bottom-top)/2;ymax=cy+perPixel*(bottom-top)/2;
 const x=v=>left+(v-xmin)/(xmax-xmin)*(right-left),y=v=>bottom-(v-ymin)/(ymax-ymin)*(bottom-top);
 let s=frame(svg,`任务${h.task_id}从取件点经站${h.relay_id}到交付点，入段无人机${h.inbound_drone_id}，出段无人机${h.outbound_drone_id}`,w,height);
 s+=`<rect data-chart-frame x="${left}" y="${top}" width="${right-left}" height="${bottom-top}" class="frame"/>`;
 const nt=w<400?3:4;ticks(xmin,xmax,nt).forEach(v=>s+=line(x(v),top,x(v),bottom,'class="gridline"')+t(x(v),bottom+18,fmt(v,1),'middle','axis'));
 ticks(ymin,ymax,4).forEach(v=>s+=line(left,y(v),right,y(v),'class="gridline"')+t(left-8,y(v)+4,fmt(v,1),'end','axis'));
 s+=`<path d="M${x(task.pickup[0])},${y(task.pickup[1])} L${x(station[0])},${y(station[1])}" fill="none" stroke="var(--direct)" stroke-width="2"/><path d="M${x(station[0])},${y(station[1])} L${x(task.delivery[0])},${y(task.delivery[1])}" fill="none" stroke="var(--relay)" stroke-width="2"/>`;
 const raw=[{p:task.pickup,label:'取件',color:'var(--direct)',home:false},{p:station,label:'站'+h.relay_id,color:'var(--cache)',home:false},{p:task.delivery,label:'交付',color:'var(--relay)',home:false},{p:donor,label:'机'+h.inbound_drone_id+'起点',color:'var(--direct)',home:true},{p:receiver,label:'机'+h.outbound_drone_id+'起点',color:'var(--relay)',home:true}],groups=new Map();
 raw.forEach(a=>{const key=a.p.join(',');if(groups.has(key)){const g=groups.get(key);if(!g.labels.includes(a.label))g.labels.push(a.label);g.home=g.home&&a.home;}else groups.set(key,{...a,labels:[a.label]});});
 const nodes=[...groups.values()].map(a=>({...a,xx:x(a.p[0]),yy:y(a.p[1])})),occupied=nodes.map(a=>({x:a.xx-7,y:a.yy-7,w:14,h:14}));
 function overlap(a,b){return a.x<b.x+b.w+4&&a.x+a.w+4>b.x&&a.y<b.y+b.h+4&&a.y+a.h+4>b.y;}
 nodes.forEach(a=>{const {xx,yy}=a,label=a.labels.join(' / '),size=[...label].reduce((v,c)=>v+(/[^\x00-\x7F]/.test(c)?12:7),0);s+=a.home?`<rect x="${xx-4}" y="${yy-4}" width="8" height="8" fill="${a.color}"/>`:`<circle cx="${xx}" cy="${yy}" r="5" fill="${a.color}"/>`;
  const candidates=[[xx+9,yy-9],[xx-size-9,yy-9],[xx+9,yy+25],[xx-size-9,yy+25],[xx-size/2,yy-25],[xx-size/2,yy+41]].map(([lx,ly])=>({x:Math.min(right-size-4,Math.max(left+4,lx)),y:Math.min(bottom-5,Math.max(top+15,ly))-12,w:size,h:15})),box=candidates.find(b=>!occupied.some(c=>overlap(b,c)))||candidates[0];occupied.push(box);s+=t(box.x,box.y+12,label);
 });
 s+=t((left+right)/2,height-4,'x / km','middle')+t(5,17,'y / km');svg.innerHTML=s;
}
function drawTimeline(m,h){
 const svg=$('handoff-timeline'),w=svg.parentElement.clientWidth,height=320,left=85,right=w-18,es=taskEvents(m,h),pickup=es.find(e=>e.action==='pickup'),delivery=es.find(e=>e.action==='delivery'),times=[pickup.arrival_time_min,h.drop_time_min,h.aircraft_arrival_min,h.pickup_time_min,delivery.arrival_time_min],[lo,hi]=extent(times,.8),x=v=>left+(v-lo)/(hi-lo)*(right-left);
 let s=frame(svg,`任务${h.task_id}交接：入站${fmt(h.drop_time_min,3)}分钟，接收机到站${fmt(h.aircraft_arrival_min,3)}分钟，飞机等待${fmt(h.aircraft_waiting_min,3)}分钟，缓存${fmt(h.package_cache_min,3)}分钟`,w,height);
 s+=`<rect data-chart-frame x="${left}" y="22" width="${right-left}" height="247" class="frame"/>`;
 ticks(lo,hi,w<400?3:4).forEach(v=>s+=line(x(v),22,x(v),269,'class="gridline"')+t(x(v),289,fmt(v,1),'middle','axis'));
 const y1=81,y2=171,y3=236;
 s+=t(0,y1,'入段机 '+h.inbound_drone_id)+t(0,y2,'出段机 '+h.outbound_drone_id)+t(0,y3,'包裹在站');
 s+=line(x(pickup.arrival_time_min),y1,x(h.drop_time_min),y1,'stroke="var(--direct)" stroke-width="3"')+dot(x(pickup.arrival_time_min),y1,false)+dot(x(h.drop_time_min),y1,false);
 s+=t(left+7,y1-39,'取件 '+fmt(pickup.arrival_time_min,3)+' min')+t(left+7,y1-22,'入站 '+fmt(h.drop_time_min,3)+' min');
 s+=line(x(h.aircraft_arrival_min),y2,x(delivery.arrival_time_min),y2,'stroke="var(--relay)" stroke-width="3"')+dot(x(h.aircraft_arrival_min),y2,true)+dot(x(h.pickup_time_min),y2,true)+dot(x(delivery.arrival_time_min),y2,true);
 s+=t(left+7,y2-56,'到站 '+fmt(h.aircraft_arrival_min,3)+' min')+t(left+7,y2-39,'取件 '+fmt(h.pickup_time_min,3)+' min')+t(left+7,y2-22,'交付 '+fmt(delivery.arrival_time_min,3)+' min');
 if(h.aircraft_waiting_min>0)s+=`<rect x="${x(h.aircraft_arrival_min)}" y="${y2-6}" width="${x(h.pickup_time_min)-x(h.aircraft_arrival_min)}" height="12" fill="var(--wait)"/>`;
 if(h.package_cache_min>0)s+=`<rect x="${x(h.drop_time_min)}" y="${y3-6}" width="${x(h.pickup_time_min)-x(h.drop_time_min)}" height="12" fill="var(--cache)"/>`;
 s+=t(left+7,y3-18,'缓存 '+fmt(h.package_cache_min,3)+' min')+t((left+right)/2,height-3,'任务时刻 / min','middle');svg.innerHTML=s;
}
function effects(m){const relayed=new Set(m.reference.handoffs.map(h=>h.task_id));return D.tasks.map(t=>{const a=m.reference.delivery_times_min[String(t.id)],b=m.counterfactual.delivery_times_min[String(t.id)];return {id:t.id,a,b,delta:a-b,relay:relayed.has(t.id),deadline:t.deadline_min,lateA:Math.max(0,a-t.deadline_min),lateB:Math.max(0,b-t.deadline_min)};});}
function drawEffects(m){const data=effects(m),svg=$('task-effects'),w=svg.parentElement.clientWidth,height=330,left=64,right=w-17,top=24,bottom=height-48,[lo,hi]=extent([...data.map(e=>e.delta),0],.3),x=v=>left+(v-1)/199*(right-left),y=v=>bottom-(v-lo)/(hi-lo)*(bottom-top);let s=frame(svg,'全部200任务，Relay减去固定结构DIRECT交付时刻；负值更早',w,height);s+=`<rect data-chart-frame x="${left}" y="${top}" width="${right-left}" height="${bottom-top}" class="frame"/>`;ticks(lo,hi,4).forEach(v=>s+=line(left,y(v),right,y(v),'class="gridline"')+t(left-8,y(v)+4,fmt(v,1),'end','axis'));[1,Math.round(200/2),200].forEach(v=>s+=t(x(v),bottom+19,v,'middle','axis'));s+=line(left,y(0),right,y(0),'stroke="var(--muted)"');data.forEach(e=>{const xx=x(e.id),yy=y(e.delta),col=e.delta<0?'var(--good)':e.delta>0?'var(--bad)':'var(--muted)';s+=e.relay?`<path d="M${xx},${yy-4} L${xx+4},${yy} L${xx},${yy+4} L${xx-4},${yy}Z" fill="${col}"/>`:`<circle cx="${xx}" cy="${yy}" r="2.2" fill="${col}" opacity=".65"/>`;});s+=t((left+right)/2,height-5,'任务编号 / 项','middle')+t(4,16,'Δ交付 / min');svg.innerHTML=s;}
function drawCounterMetrics(m){const svg=$('counterfactual-metrics'),w=svg.parentElement.clientWidth,height=330,left=4,right=w-4,a=m.reference.score,b=m.counterfactual.score,rows=[['按时完成',N-b.late_count,N-a.late_count,'项',0],['总航程',b.distance_km,a.distance_km,'km',3],['总逾期',b.total_lateness_min,a.total_lateness_min,'min',3],['研究J',score(b),score(a),'J',6]];let s=frame(svg,'固定结构直接化CF和原Relay终局的完整指标；各单位独立',w,height);rows.forEach(([label,cf,r,u,n],i)=>{const y=28+i*73;s+=t(left,y,label)+t(right,y,'Δ '+signed(r-cf,n)+' '+u,'end')+t(left,y+25,'CF '+fmt(cf,n)+' '+u)+t(left,y+45,'Relay '+fmt(r,n)+' '+u)+line(left,y+57,right,y+57);});svg.innerHTML=s;}
function updateMechanism(){
 const m=selected(),h=chosenHandoff(m),has=!!h;
 $('handoff-figures').classList.toggle('hide',!has);$('handoff-empty').classList.toggle('hide',has);
 if(has){
  $('handoff-detail').textContent=`任务${h.task_id}：无人机${h.inbound_drone_id}取件并送至站${h.relay_id}，无人机${h.outbound_drone_id}接走交付（${h.cross_uav?'真实跨机':'同机站点计划'}）。飞机等待${fmt(h.aircraft_waiting_min,3)} min，包裹缓存${fmt(h.package_cache_min,3)} min；截止期${fmt(taskById.get(h.task_id).deadline_min,3)} min。`;
  drawMap(m,h);drawTimeline(m,h);
 }else{
  $('handoff-detail').textContent='该终局没有Relay任务。';$('handoff-empty').textContent='不补造交接路径或交接时刻；这个终局不提供跨机机制证据。';
 }
 const es=effects(m),early=es.filter(e=>e.delta< -1e-9).length,late=es.filter(e=>e.delta>1e-9).length,sameCount=N-early-late,secondary=es.filter(e=>!e.relay&&Math.abs(e.delta)>1e-9).length;
 const onlySameUav=m.reference.relay_statistics.relay_task_count>0&&m.reference.relay_statistics.cross_uav_handoff_count===0,onTimeGain=m.counterfactual.score.late_count-m.reference.score.late_count;
 const sameUavBoundary=onlySameUav?`本运行只包含${m.reference.handoffs.map(h=>`任务${h.task_id}、机${h.inbound_drone_id}→机${h.outbound_drone_id}`).join('；')}的同机站点缓存。相对结构直接化按时${signed(onTimeGain,0)}项，这是同机缓存机制，不能证明跨机创新。 `:'';
 $('counterfactual-summary').textContent=sameUavBoundary+`${short[m.scenario]} · ${m.seed}：相对同一终局结构直接化，${early}项更早、${late}项更晚、${sameCount}项在10⁻⁹ min显示容差内相同；其中${secondary}个仍为DIRECT的任务发生时刻联动变化。显示容差只用于计数说明，不改原始评分。`;
 drawEffects(m);drawCounterMetrics(m);
 table('task-effects-table','完整200项，原编号顺序；Δ统一为Relay−结构直接化',['任务','终局方式','截止 min','CF交付 min','Relay交付 min','交付 Δmin','CF逾期 min','Relay逾期 min'],es.map(e=>[e.id,e.relay?'Relay':'DIRECT',fmt(e.deadline,3),fmt(e.b,3),fmt(e.a,3),signed(e.delta,3),fmt(e.lateB,3),fmt(e.lateA,3)]));
}
const relayRows=D.rows.filter(r=>r.relay_enabled);
table('increment-table','该运行自己的完整DIRECT预热→最终终局；不是公平240秒DIRECT对照',['方案','种子','预热按时','终局按时','预热航程 km','终局航程 km','预热 J','终局 J'],relayRows.map(r=>{const p=r.phases.find(p=>p.phase==='direct_warmup'),s=p.score;return [short[r.scenario],r.seed,N-s.late_count,r.on_time_count,fmt(s.distance_km,3),fmt(r.distance_km,3),fmt(score(s),6),fmt(r.weighted_score,6)];}));
const allWin=D.pairs.every(p=>p.verdict==='win'),someWin=D.pairs.some(p=>p.verdict==='win'),allOnTime=D.pairs.every(p=>p.delta.on_time_count>=0),allDistance=D.pairs.every(p=>p.delta.distance_km<=0);$('paper-conclusion').textContent=allWin?`可写为“在本官方实例的三枚固定种子与两种固定起点设置中，允许中继均改善共同加权目标”。${allOnTime&&allDistance?'逐对按时数不减且航程不增。':'按时、航程和逾期幅度的具体取舍仍须逐对披露。'} 不写普适最优、统计显著或所有指标全面占优。`:someWin?'可写为“在同预算控制下，中继在部分配对中显示收益，并伴随按时、距离或逾期的取舍；收益依赖起点和随机种子”。本批全部退化必须与正例共同报告，不能写全面优于直送。':'本批同预算终点未显示中继的加权优势，应将结论写为机制已实现及其局限，不能用历史不同预算正例或较短预热增量代替性能证明。';
if(!crosses)$('paper-conclusion').textContent+=' 本批最终解没有真实跨机交接，尚不足以验证跨机接力创新。';
if(stationDistanceConsistent&&stationOnTimeUnchanged&&originPairs.every(p=>p.verdict==='loss')){
 const [w,e,l]=counts(stationPairs);
 $('paper-conclusion').textContent=`可写为“在这一官方实例的三个固定种子中，需求驱动预部署条件下允许中继均缩短航程，平均减少${fmt(-stationMeanDistance,3)} km（${fmt(-stationMeanDistance/stationBaseDistance*100,3)}%），按时完成数保持；共同加权目标为${w}胜/${e}平/${l}负”。原点起点的三个配对均退化。因此证据支持预部署条件下的里程收益和实际交接机制，尚不支持普遍优于直送、提升按时率、统计显著或跨实例稳定优势。`;
}
$('formula').textContent=`J = 0.50 × 逾期数 / ${D.manifest.scales[0]}\n  + 0.25 × 总逾期分钟 / ${D.manifest.scales[1]}\n  + 0.25 × 总航程公里 / ${D.manifest.scales[2]}`;
const figureLabels={'relay_paired_effects.svg':'论文配对效应图 · SVG','relay_paired_effects.png':'论文配对效应图 · PNG','paired_effects.csv':'论文图的六对原始差值 · CSV'};
$('figure-links').innerHTML=Object.keys(D.figures.outputs||{}).map(p=>`<a href="figures/${esc(p)}">${esc(figureLabels[p]||p)}</a>`).join('');
const evidence=['manifest.json','runs.json','audit.json',...Object.keys(D.context),...['runs.csv','paired_summary.csv','figures/validation.json'].filter(p=>p in D.fingerprints)];$('evidence-links').innerHTML=evidence.map(p=>`<a href="${esc(p)}">${esc(p)}</a>`).join('');$('protocol').textContent=JSON.stringify({protocol:D.manifest.protocol,seeds:D.manifest.seeds,schedule:D.manifest.schedule,network:D.manifest.network_parameters,periodic:D.manifest.periodic_parameters,source_commit:D.manifest.source_commit},null,2);$('fingerprints').textContent=JSON.stringify({...D.fingerprints,'renderer_sha256':D.renderer_sha256},null,2);
table('raw-table','全部12次新实测；均值与范围不代替原值',['方案','种子','按时 项','按时率 %','逾期 项','总逾期 min','航程 km','J','端到端 s','完整解'],D.rows.map(r=>[short[r.scenario],r.seed,r.on_time_count,fmt(r.on_time_rate*100,2),r.late_count,fmt(r.total_lateness_min,6),fmt(r.distance_km,6),fmt(r.weighted_score,9),fmt(r.end_to_end_seconds,6),`<a href="${esc(r.solution_path)}">完整终局</a>`]));
$('mechanism-run').addEventListener('change',updateHandoffOptions);$('handoff-task').addEventListener('change',updateMechanism);
let pending=false;function draw(){drawPairs();drawBudget();updateMechanism();}updateHandoffOptions();draw();new ResizeObserver(()=>{if(pending)return;pending=true;requestAnimationFrame(()=>{pending=false;draw();});}).observe($('relay-ablation-report'));
})();
</script></body></html>'''


def render_report(directory: Path) -> Path:
    directory = directory.resolve()
    data = build_data(directory)
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    encoded = encoded.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    target = directory / "index.html"
    target.write_text(TEMPLATE.replace("__DATA__", encoded), encoding="utf-8")
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    print(render_report(args.output_dir))


if __name__ == "__main__":
    main()
