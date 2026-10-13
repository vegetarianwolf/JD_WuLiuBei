"""Small paired trials against current main; never reuse historical baselines.

Freeze this protocol and both solver sources before running. Each later stage
reserves all remaining cells, and every planned validation seed is completed.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import platform
import time

from algorithm.experiments import run_optimization_experiments as base

ROOT = base.ROOT
OUTPUT = ROOT / "algorithm/results/method_trials_20261013"
BASELINE_COMMIT = "2451101d12d59a4e6b63b6164d1784bef3b4a681"
VARIANTS = {"baseline": {}, "memo": {"enable_direct_option_cache": True},
            "cooling": {"enable_budget_cooling": True}}
LABELS = {"baseline": "当前 main 基线", "memo": "DIRECT 插入选项缓存", "cooling": "按时间预算降温"}
SEEDS = {"equivalence": [2026101501, 2026101502],
         "screen": [2026101511, 2026101512],
         "medium": [2026101521, 2026101522, 2026101523]}
CASES = {stage: ["single25", "fleet200"] for stage in SEEDS}
BUDGETS = {"equivalence": {"single25": 10, "fleet200": 10},
           "screen": {"single25": 10, "fleet200": 10},
           "medium": {"single25": 10, "fleet200": 30}}
CAPS = {"equivalence": 80, "screen": 120, "medium": 360}
TOTAL_SECONDS, RESERVED_SECONDS, EQUIVALENCE_ITERATIONS = 900, 30, 100
EXECUTION_NAMES = ("run_optimization_experiments", "run_scenario_comparison", "run_method_trials")
PROTOCOL = {
    "total_experiment_seconds": TOTAL_SECONDS, "reserved_seconds": RESERVED_SECONDS,
    "stage_caps_seconds": CAPS, "stage_cases": CASES, "stage_budgets_seconds": BUDGETS,
    "equivalence_iterations": EQUIVALENCE_ITERATIONS,
    "equivalence_variants": ["baseline", "memo"],
    "single": {"tasks": 25, "drones": 1}, "fleet": {"tasks": 200, "drones": 8},
    "max_tasks_per_drone": 25, "capacity": 2, "speed_km_per_min": .9,
    "deadline_multiplier": 1., "relay_count": 0, "safety_margin_seconds": 1.,
    "time_boundary": "CSV读取前至完整核验和解JSON写出后；不含进程启动与模块导入",
    "comparison": "只比较本批与当前main：同实例、同种子、同初解、同预算的三级词典序配对",
    "equivalence_gate": "memo须在两实例两种子各完成100轮并保持原搜索决策完全相同；不设速度阈值",
    "screen_gate": "两实例所有配对均无词典序退化；集群至少一胜；memo另须固定迭代等价",
    "medium_gate": "全部预定配对有效且按预算结束，三个30秒集群种子至少两胜；允许出现退化",
    "selection": "过关者按集群胜数、逾期任务减少总量、同逾期数次级胜数排序；完全并列选memo",
    "continuation_policy": "验证出现退化仍跑完三个预定新种子；不换种子，不重跑择优",
    "default_policy": "探索模式：选定候选通过30秒三种子多数胜即可设默认，报告必须披露退化和分钟级未知风险",
    "single_compatibility": "验证同时以10秒、三个新种子展示单机兼容性；不是分钟级证据",
    "historical_results": "不覆盖、不合并历史批次；无历史比较面板",
    "budget_accounting": "完整运行计端到端秒数；中止计进程秒数；阶段开始前预留全部缺失单元预算",
    "user_choices": ["筛选每组10秒", "扩大每组最多30秒", "精简指标仪表盘", "多数胜可设默认并披露风险",
                     "仅比较本轮与当前main", "退化也完成预定种子"],
}


def _safe(output, relative):
    path = (output / relative).resolve()
    if not path.is_relative_to(output.resolve()):
        raise ValueError("归档路径越出实验目录")
    return path


def _aborted(output):
    path = output / "execution_notes.json"
    return base.read_json(path).get("aborted_runs", []) if path.exists() else []


def _cell(row):
    return tuple(row.get(key) for key in ("stage", "case_id", "variant", "seed"))


def experiment_seconds(output):
    values = [r["end_to_end_seconds"] for r in base.read_json(output / "runs.json")]
    values += [r.get("process_elapsed_seconds", 0.) for r in _aborted(output)]
    if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("实验耗时必须有限且非负")
    return sum(values)


def prepare(output: Path):
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("新实验目录必须为空，禁止覆盖历史批次")
    output.mkdir(parents=True, exist_ok=True)
    snapshots = {}
    for name, commit in (("baseline", BASELINE_COMMIT), ("candidate", None)):
        base._snapshot(output / f"sources/{name}.zip", commit)
    for variant, config in VARIANTS.items():
        source = "baseline" if variant == "baseline" else "candidate"
        path = f"sources/{source}.zip"
        snapshots[variant] = {"path": path, "sha256": base.sha256(output / path), "config": config,
                              "source_ref": BASELINE_COMMIT if source == "baseline" else "working_tree",
                              "label": LABELS[variant], "role": "baseline" if source == "baseline" else "candidate"}
    execution = {}
    for name, source in zip(EXECUTION_NAMES, (Path(base.__file__),
            ROOT / "algorithm/experiments/run_scenario_comparison.py", Path(__file__))):
        target = output / "sources" / (name + ".py")
        target.write_bytes(source.read_bytes())
        execution[name] = {"path": str(target.relative_to(output)), "sha256": base.sha256(target)}
    manifest = {"schema_version": 5, "kind": "method_trials", "baseline_variant": "baseline",
                "baseline_commit": BASELINE_COMMIT, "input_path": base.INPUT,
                "input_sha256": base.sha256(ROOT / base.INPUT), "source_snapshots": snapshots,
                "runner_sha256": execution[EXECUTION_NAMES[0]]["sha256"], "execution_sources": execution,
                "seeds": SEEDS, "protocol": PROTOCOL,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "environment": {"python": platform.python_version(), "platform": platform.platform()}}
    base.write_json(output / "manifest.json", manifest)
    base.write_json(output / "runs.json", [])
    base.write_json(output / "decisions.json", [])
    return manifest


def verify(output, manifest):
    base.verify_execution_files(manifest)
    if (manifest.get("schema_version") != 5 or manifest.get("kind") != "method_trials"
            or manifest.get("baseline_commit") != BASELINE_COMMIT or manifest.get("seeds") != SEEDS
            or manifest.get("protocol") != PROTOCOL
            or set(manifest.get("source_snapshots", {})) != set(VARIANTS)
            or {v: s.get("config") for v, s in manifest["source_snapshots"].items()} != VARIANTS):
        raise ValueError("固定实验协议改变")
    if manifest.get("input_path") != base.INPUT or base.sha256(ROOT / base.INPUT) != manifest["input_sha256"]:
        raise ValueError("官方输入指纹改变")
    if set(manifest.get("execution_sources", {})) != set(EXECUTION_NAMES):
        raise ValueError("缺少冻结执行源码")
    for source in [*manifest["source_snapshots"].values(), *manifest["execution_sources"].values()]:
        if base.sha256(_safe(output, source["path"])) != source["sha256"]:
            raise ValueError("归档源码指纹改变")
    if base.sha256(Path(__file__)) != manifest["execution_sources"]["run_method_trials"]["sha256"]:
        raise ValueError("运行协议源码改变，禁止混用版本续跑")
    cells, referenced = set(), set()
    for row in base.read_json(output / "runs.json"):
        stage, case, variant, seed = cell = _cell(row)
        if (cell in cells or stage not in SEEDS or case not in CASES[stage] or variant not in VARIANTS
                or seed not in SEEDS[stage] or (stage == "equivalence" and variant == "cooling")):
            raise ValueError("已有实验单元重复或不属于预定协议")
        cells.add(cell)
        run_id = f"{stage}__{case}__{variant}__{seed}"
        if (row.get("run_id") != run_id or row.get("solution_file") != f"run_solutions/{run_id}.json"
                or row.get("source_sha256") != manifest["source_snapshots"][variant]["sha256"]
                or row.get("budget_seconds") != BUDGETS[stage][case]):
            raise ValueError("已有运行身份、来源或预算改变")
        solution = _safe(output, row["solution_file"])
        referenced.add(solution)
        if base.sha256(solution) != row["solution_sha256"]:
            raise ValueError("已有解指纹改变")
    if any(path.resolve() not in referenced for path in (output / "run_solutions").glob("*.json")):
        raise RuntimeError("发现未登记的孤立解，禁止重跑覆盖")
    aborted_cells = [_cell(row) for row in _aborted(output)]
    if cells.intersection(aborted_cells) or len(set(aborted_cells)) != len(aborted_cells):
        raise ValueError("中止单元重复或与完整单元重合")
    experiment_seconds(output)


def pairs(rows, stage, variant, case="fleet200"):
    return base.paired(rows, stage, variant, case)


def complete(rows, stage, variant):
    return all({b["seed"] for b, _ in pairs(rows, stage, variant, case)} == set(SEEDS[stage])
               for case in CASES[stage])


def _stable_search(output, row):
    payload = base.read_json(_safe(output, row["solution_file"]))
    metadata = payload["metadata"]
    fields = ("uses", "accepted", "current_improvements", "best_improvements", "total_reward",
              "current_late_count_improvements", "best_late_count_improvements",
              "equal_score_accepts", "unchanged_route_accepts")
    return {"routes": [r["encoded_visits"] for r in payload["routes"]], "score": payload["score"],
            "iterations": row["iterations"], "trajectory": [[p[0], *p[2:]] for p in row["best_trajectory"]],
            "statistics": {name: {k: stats[k] for k in fields} for name, stats in metadata["operator_statistics"].items()},
            "weights": metadata["operator_weights"], "accepted": metadata["accepted_solutions"]}


def equivalence_pass(output, rows):
    return complete(rows, "equivalence", "memo") and all(
        b["iterations"] == c["iterations"] == EQUIVALENCE_ITERATIONS
        and _stable_search(output, b) == _stable_search(output, c)
        for case in CASES["equivalence"] for b, c in pairs(rows, "equivalence", "memo", case))


def wins(matched):
    return sum(base.score(c) < base.score(b) for b, c in matched)


def screen_pass(rows, variant, equivalent=True):
    return (equivalent and complete(rows, "screen", variant) and wins(pairs(rows, "screen", variant)) >= 1
            and all(base.score(c) <= base.score(b) for case in CASES["screen"]
                    for b, c in pairs(rows, "screen", variant, case)))


def medium_pass(rows, variant):
    return complete(rows, "medium", variant) and wins(pairs(rows, "medium", variant)) >= 2


def choose_candidate(rows, eligible):
    candidates = [v for v in eligible if medium_pass(rows, v)]
    def rank(variant):
        matched = pairs(rows, "medium", variant)
        return (wins(matched), sum(b["late_count"] - c["late_count"] for b, c in matched),
                sum(b["late_count"] == c["late_count"] and base.score(c) < base.score(b) for b, c in matched),
                variant == "memo")
    return max(candidates, key=rank) if candidates else None


def stage(output, manifest, name, variants):
    verify(output, manifest)
    if (not variants or variants[0] != "baseline" or len(set(variants)) != len(variants)
            or any(v not in VARIANTS for v in variants) or (name == "equivalence" and variants != ["baseline", "memo"])):
        raise ValueError("阶段候选清单不符合协议")
    rows = base.read_json(output / "runs.json")
    cells = {_cell(r) for r in rows}
    aborted = _aborted(output)
    spent = sum(r["end_to_end_seconds"] for r in rows if r["stage"] == name)
    spent += sum(r.get("process_elapsed_seconds", 0.) for r in aborted if r.get("stage") == name)
    missing = sum(BUDGETS[name][case] for seed in SEEDS[name] for case in CASES[name]
                  for variant in variants if (name, case, variant, seed) not in cells)
    if spent + missing > CAPS[name] or experiment_seconds(output) + missing > TOTAL_SECONDS - RESERVED_SECONDS:
        raise RuntimeError("预算不足以预留全部剩余阶段单元及审计时间")
    for index, seed in enumerate(SEEDS[name]):
        order = variants if index % 2 == 0 else list(reversed(variants))
        for case in CASES[name]:
            for variant in order:
                cell = (name, case, variant, seed)
                if cell in cells:
                    continue
                if cell in {_cell(r) for r in aborted}:
                    raise RuntimeError("禁止重跑已有中止单元")
                if (output / "run_solutions" / f"{name}__{case}__{variant}__{seed}.json").exists():
                    raise RuntimeError("孤立解已存在，禁止覆盖")
                budget = BUDGETS[name][case]
                if spent + budget > CAPS[name] or experiment_seconds(output) + budget > TOTAL_SECONDS - RESERVED_SECONDS:
                    raise RuntimeError("累计实验预算不足，保留全部证据")
                row = base.invoke(output, manifest, variant, case, seed, name, budget,
                                  iterations=EQUIVALENCE_ITERATIONS if name == "equivalence" else None)
                row["order"] = order.index(variant)
                rows.append(row)
                cells.add(cell)
                spent += row["end_to_end_seconds"]
                base.save_rows(output, rows)
    return rows


def _decision(output, name, **details):
    path = output / "decisions.json"
    saved = base.read_json(path)
    item = {"stage": name, **details}
    matches = [d for d in saved if d.get("stage") == name]
    if matches:
        if matches != [item]:
            raise ValueError("已保存阶段决定与重新计算不一致，禁止覆盖")
    else:
        base.write_json(path, [*saved, item])


def run(output):
    manifest = base.read_json(output / "manifest.json")
    rows = stage(output, manifest, "equivalence", ["baseline", "memo"])
    equivalent = equivalence_pass(output, rows)
    _decision(output, "equivalence", passed=equivalent, candidate="memo", planned_pairs=4,
              reason=PROTOCOL["equivalence_gate"])
    rows = stage(output, manifest, "screen", list(VARIANTS))
    screening = {v: screen_pass(rows, v, equivalent if v == "memo" else True) for v in VARIANTS if v != "baseline"}
    eligible = [v for v in screening if screening[v]]
    _decision(output, "screen", candidates=screening, eligible_variants=eligible, reason=PROTOCOL["screen_gate"])
    if eligible:
        rows = stage(output, manifest, "medium", ["baseline", *eligible])
    selected = choose_candidate(rows, eligible)
    _decision(output, "medium", selected_variant=selected, passed=selected is not None, skipped=not bool(eligible),
              candidates={v: medium_pass(rows, v) for v in eligible}, continuation_after_regression=True,
              reason=PROTOCOL["medium_gate"])
    summary = base.summarize(output)
    summary.update(experiment_seconds=experiment_seconds(output), selected_variant=selected,
                   default_eligible=selected is not None, exploratory_promotion=True,
                   note="仅本批当前main配对；3种子30秒多数胜允许退化，不能证明分钟级收益")
    base.write_json(output / "summary.json", summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    prepare(output) if args.command == "prepare" else run(output)


if __name__ == "__main__":
    main()
