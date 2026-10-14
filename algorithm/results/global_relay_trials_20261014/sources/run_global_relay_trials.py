"""Full-instance, four-scenario paired screening of composite global moves.

Frozen before the first measured run. All solving is sequential; historical
snapshots and the existing worker preserve identical construction and timing.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import platform
import subprocess
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

from algorithm.experiments import run_optimization_experiments as base

ROOT = base.ROOT
OUTPUT = ROOT / "algorithm/results/global_relay_trials_20261014"
BASELINE_COMMIT = "39baeb4e33431c57881a70a10737de84fb1414c0"
VARIANTS = {
    "baseline": {},
    "global": {"enable_tail_exchange": True, "enable_home_reassignment": True},
    "downstream": {"enable_tail_exchange": True, "enable_home_reassignment": True,
                   "enable_relay_downstream_refine": True},
}
LABELS = {"baseline": "本轮 main 基线", "global": "全局尾段交换 + 起点重配",
          "downstream": "全局调整 + 接力后续影响"}
CASES = ["fleet200", "fleet200__direct_relay", "fleet200__direct_stations",
         "fleet200__direct_relay_stations"]
RELAY_CASES = [CASES[1], CASES[3]]
SEEDS = {"compatibility": [2026102400], "screen": [2026102411, 2026102412, 2026102413],
         "validate": [2026102421, 2026102422, 2026102423]}
BUDGETS = {"compatibility": 15, "screen": 20, "validate": 60}
EXECUTION_NAMES = ("run_optimization_experiments", "run_scenario_comparison", "run_global_relay_trials")
PROTOCOL = {
    "tasks": 200, "drones": 8, "max_tasks_per_drone": 25, "capacity": 2,
    "speed_km_per_min": .9, "relay_count": 4, "relay_location_seed": 42,
    "relay_direct_warmup_fraction": .8, "safety_margin_seconds": 1.,
    "stage_budgets_seconds": BUDGETS, "cases": CASES, "relay_cases": RELAY_CASES,
    "total_experiment_seconds": 2200, "compatibility_iterations": 100,
    "comparison": "同实例、场景、种子、构造初解、总墙钟预算；逐种子三级词典序；保留全部退化",
    "screen_gate": "global在四场景12对中至少7胜且总逾期任务数不增加；或主场景至少2/3胜且主场景总逾期任务数不增加",
    "relay_selection": "downstream对global的两个中继场景6对至少4胜且总逾期任务数不增加，才选择其作中继验证版本；否则使用global",
    "validation": "仅global通过初筛才执行60秒四场景三个新种子，完成预定配对，不换种子或重复择优",
    "promotion": "验证12对至少8胜，各场景至少2/3胜，所有配对逾期任务数不增；否则新开关默认关闭",
    "K_interpretation": "中继扩展沿用最终交付责任计入K，前半程转运不占K；原题直送每机最多25项",
    "objectives": "题目首要尽量按时且航程短；项目沿用逾期任务数/总逾期分钟/航程三级词典序",
    "timing": "CSV读取前至核验和完整解写出后，含网络构造和预部署；不含Python启动导入",
    "user_direction": "2026-10-14用户要求本次全盘自主决策、不再询问，小范围不要太小；先完整规模覆盖四主场景再按门槛扩大",
}


def cell(row):
    return tuple(row[k] for k in ("stage", "case_id", "variant", "seed"))


def snapshot(path, commit=None):
    """Baseline enumerates the Git tree, so new candidate modules are allowed."""
    if commit:
        names = subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", commit, "--", "algorithm/src/uav_dispatch"],
            cwd=ROOT, text=True).splitlines()
        files = {Path(n).name: subprocess.check_output(["git", "show", f"{commit}:{n}"], cwd=ROOT)
                 for n in names if n.endswith(".py")}
    else:
        files = {p.name: p.read_bytes() for p in (ROOT / "algorithm/src/uav_dispatch").glob("*.py")}
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            info = ZipInfo("uav_dispatch/" + name, (2026, 10, 14, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, data)


def prepare(output):
    if output.exists() and any(output.iterdir()):
        raise ValueError("新批次必须为空，不覆盖历史结果")
    snapshot(output / "sources/baseline.zip", BASELINE_COMMIT)
    snapshot(output / "sources/candidate.zip")
    execution = {}
    for name in EXECUTION_NAMES:
        relative = f"sources/{name}.py"
        (output / relative).write_bytes((ROOT / f"algorithm/experiments/{name}.py").read_bytes())
        execution[name] = {"path": relative, "sha256": base.sha256(output / relative)}
    sources = {}
    for variant, flags in VARIANTS.items():
        path = f"sources/{'baseline' if variant == 'baseline' else 'candidate'}.zip"
        sources[variant] = dict(path=path, sha256=base.sha256(output / path), config=flags,
                               source_ref=BASELINE_COMMIT if variant == "baseline" else "working_tree")
    manifest = dict(kind="global_relay_trials", schema_version=1, baseline_commit=BASELINE_COMMIT,
                    input_path=base.INPUT, input_sha256=base.sha256(ROOT / base.INPUT),
                    source_snapshots=sources, execution_sources=execution,
                    runner_sha256=execution[EXECUTION_NAMES[0]]["sha256"],
                    protocol=PROTOCOL, seeds=SEEDS, labels=LABELS,
                    environment=dict(python=platform.python_version(), platform=platform.platform()))
    base.write_json(output / "manifest.json", manifest)
    base.write_json(output / "runs.json", [])
    base.write_json(output / "decisions.json", [])


def verify(output):
    manifest = base.read_json(output / "manifest.json")
    if (manifest["protocol"] != PROTOCOL or manifest["seeds"] != SEEDS
            or manifest["baseline_commit"] != BASELINE_COMMIT
            or {v: s["config"] for v, s in manifest["source_snapshots"].items()} != VARIANTS):
        raise ValueError("冻结协议改变")
    for source in [*manifest["source_snapshots"].values(), *manifest["execution_sources"].values()]:
        if base.sha256(output / source["path"]) != source["sha256"]:
            raise ValueError("冻结源码改变")
    for name, source in manifest["execution_sources"].items():
        if base.sha256(ROOT / f"algorithm/experiments/{name}.py") != source["sha256"]:
            raise ValueError("执行源码改变，不能续跑混用")
    rows = base.read_json(output / "runs.json")
    if len({cell(r) for r in rows}) != len(rows):
        raise ValueError("重复单元")
    registered = set()
    for row in rows:
        path = output / row["solution_file"]
        if base.sha256(path) != row["solution_sha256"]:
            raise ValueError("原始解改变")
        registered.add(path.resolve())
    if any(p.resolve() not in registered for p in (output / "run_solutions").glob("*.json")):
        raise ValueError("发现未登记解，禁止覆盖重跑")
    return manifest, rows


def stage(output, name, selected="global"):
    manifest, rows = verify(output)
    completed = {cell(r) for r in rows}
    cases = ["single25"] if name == "compatibility" else CASES
    notes = output / "execution_notes.json"
    aborted = base.read_json(notes).get("aborted_runs", []) if notes.exists() else []
    for index, seed in enumerate(SEEDS[name]):
        for case in cases:
            variants = (["baseline", "global", "downstream"] if name == "compatibility" else
                        ["baseline", "global"] + (["downstream"] if name == "screen" and case in RELAY_CASES else []))
            if name == "validate":
                variants = ["baseline", selected if case in RELAY_CASES else "global"]
            if index % 2:
                variants = variants[::-1]
            for variant in variants:
                key = name, case, variant, seed
                if key in completed:
                    continue
                if any(cell(r) == key for r in aborted):
                    raise ValueError("已中止单元禁止重跑择优")
                spent = sum(r["end_to_end_seconds"] for r in rows) + sum(r["process_elapsed_seconds"] for r in aborted)
                if spent + BUDGETS[name] > PROTOCOL["total_experiment_seconds"]:
                    raise ValueError("累计预算耗尽")
                row = base.invoke(output, manifest, variant, case, seed, name, BUDGETS[name],
                                  iterations=100 if name == "compatibility" else None)
                payload = base.read_json(output / row["solution_file"])
                row["relay_statistics"] = payload["metadata"].get("relay", {})
                row["global_move_statistics"] = {k: payload["metadata"].get(k, {}) for k in
                                                ("tail_exchange_statistics", "home_reassignment_statistics")}
                row["relay_warmup_score"] = payload["metadata"].get("relay_warmup_score")
                row["order"] = variants.index(variant)
                rows.append(row)
                base.save_rows(output, rows)
                completed.add(key)
    return rows


def matched(rows, stage_name, variant, cases):
    return [pair for case in cases for pair in base.paired(rows, stage_name, variant, case)]


def wins(pairs):
    return sum(base.score(c) < base.score(b) for b, c in pairs)


def late_delta(pairs):
    return sum(c["late_count"] - b["late_count"] for b, c in pairs)


def screen_decision(rows):
    pairs = matched(rows, "screen", "global", CASES)
    primary = matched(rows, "screen", "global", [CASES[3]])
    passed = len(pairs) == 12 and ((wins(pairs) >= 7 and late_delta(pairs) <= 0)
                                  or (wins(primary) >= 2 and late_delta(primary) <= 0))
    comparisons = []
    for case in RELAY_CASES:
        for seed in SEEDS["screen"]:
            pair = [next((r for r in rows if cell(r) == ("screen", case, v, seed)), None)
                    for v in ("global", "downstream")]
            if all(pair) and all(r["valid"] and r["budget_compliant"] for r in pair):
                comparisons.append(pair)
    downstream = len(comparisons) == 6 and wins(comparisons) >= 4 and late_delta(comparisons) <= 0
    return dict(passed=passed, wins=wins(pairs), pairs=len(pairs), late_delta=late_delta(pairs),
                selected_relay_variant="downstream" if downstream else "global",
                downstream_vs_global_wins=wins(comparisons), downstream_vs_global_pairs=len(comparisons),
                downstream_vs_global_late_delta=late_delta(comparisons))


def promotion(rows, selected):
    pairs = [p for case in CASES for p in matched(rows, "validate", selected if case in RELAY_CASES else "global", [case])]
    by_case = {case: matched(rows, "validate", selected if case in RELAY_CASES else "global", [case]) for case in CASES}
    return (len(pairs) == 12 and wins(pairs) >= 8
            and all(len(ps) == 3 and wins(ps) >= 2 for ps in by_case.values())
            and all(c["late_count"] <= b["late_count"] for b, c in pairs))


def run(output):
    rows = stage(output, "compatibility")
    single = [r for r in rows if r["stage"] == "compatibility"]
    if len(single) != 3 or len({r["decision_fingerprint"] for r in single}) != 1:
        raise ValueError("单机固定轮次兼容检查失败")
    rows = stage(output, "screen")
    decision = screen_decision(rows)
    base.decision(output, "screen", **decision)
    if decision["passed"]:
        rows = stage(output, "validate", decision["selected_relay_variant"])
        base.decision(output, "validate", passed=promotion(rows, decision["selected_relay_variant"]),
                      selected_relay_variant=decision["selected_relay_variant"])
    else:
        base.decision(output, "validate", passed=False, skipped=True,
                      reason="四场景完整规模初筛未过门槛，不启动长测")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args()
    (prepare if args.command == "prepare" else run)(args.output_dir.resolve())


if __name__ == "__main__":
    main()
