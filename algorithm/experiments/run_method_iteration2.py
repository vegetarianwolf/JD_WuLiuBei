"""Independent string-destroy follow-up sharing the first batch's time cap.

The first batch stays immutable. This quality candidate gets fresh paired
baselines and two stages, with no search-equivalence claim.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import platform
import time

from algorithm.experiments import run_method_trials as first

base, ROOT = first.base, first.ROOT
OUTPUT = first.OUTPUT / "iteration2"
BASELINE_COMMIT = first.BASELINE_COMMIT
VARIANTS = {"baseline": {}, "strings": {"enable_multiroute_string_destroy": True}}
LABELS = {"baseline": "当前 main 基线", "strings": "跨路线相邻串移除"}
SEEDS = {"screen": [2026101601, 2026101602], "medium": [2026101611, 2026101612, 2026101613]}
CASES = {stage: ["single25", "fleet200"] for stage in SEEDS}
BUDGETS = {"screen": {"single25": 10, "fleet200": 10}, "medium": {"single25": 10, "fleet200": 30}}
CAPS = {"screen": 80, "medium": 240}
TOTAL_SECONDS, RESERVED_SECONDS = first.TOTAL_SECONDS, first.RESERVED_SECONDS
EXECUTION_NAMES = (*first.EXECUTION_NAMES, "run_method_iteration2")
PROTOCOL = {**{k: v for k, v in first.PROTOCOL.items() if not k.startswith("equivalence")},
            "stage_caps_seconds": CAPS, "stage_cases": CASES, "stage_budgets_seconds": BUDGETS,
            "screen_gate": "两实例所有配对均无词典序退化；集群至少一胜",
            "selection": "唯一候选strings仅在初筛通过且三种子验证多数胜时采用",
            "budget_accounting": "父批全部完整和中止耗时与本批共享900秒；阶段开始前预留全部缺失单元与30秒审计",
            "followup": "用户选择继续搜索文献并迭代新方法；新质量方向不声明固定搜索等价"}
_safe, _cell, _aborted = first._safe, first._cell, first._aborted
experiment_seconds, pairs, _decision = first.experiment_seconds, first.pairs, first._decision


def parent_evidence(output):
    parent = output.parent
    documents = {name: base.read_json(parent / f"{name}.json") for name in ("manifest", "runs", "decisions", "validation")}
    manifest, validation = documents["manifest"], documents["validation"]
    if (manifest.get("kind") != "method_trials" or manifest.get("baseline_commit") != BASELINE_COMMIT
            or validation.get("passed") is not True or validation.get("protocol_complete") is not True
            or validation.get("decision_audit", {}).get("selected_variant") is not None):
        raise ValueError("追加批次须绑定已完成且未晋级的首批独立审计")
    for name in ("manifest", "runs", "decisions"):
        if base.sha256(parent / f"{name}.json") != validation.get(f"{name}_sha256"):
            raise ValueError("父批次审计绑定指纹不一致")
    for source in [*manifest["source_snapshots"].values(), *manifest["execution_sources"].values()]:
        if base.sha256(_safe(parent, source["path"])) != source["sha256"]:
            raise ValueError("父批次源码证据改变")
    for row in documents["runs"]:
        if base.sha256(_safe(parent, row["solution_file"])) != row["solution_sha256"]:
            raise ValueError("父批次完整解证据改变")
    seconds = experiment_seconds(parent)
    if not math.isclose(seconds, validation["cumulative_budget"]["total_seconds"], rel_tol=0, abs_tol=1e-9):
        raise ValueError("父批次累计耗时与独立审计不一致")
    notes = parent / "execution_notes.json"
    return {"parent_directory": "..", "parent_experiment_seconds": seconds,
            **{f"parent_{name}_sha256": base.sha256(parent / f"{name}.json") for name in documents},
            "parent_execution_notes_sha256": base.sha256(notes) if notes.exists() else None}


def prepare(output):
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("新追加目录必须为空，禁止覆盖历史批次")
    binding = parent_evidence(output)
    if binding["parent_experiment_seconds"] + CAPS["screen"] > TOTAL_SECONDS - RESERVED_SECONDS:
        raise RuntimeError("共享预算不足以启动追加初筛")
    output.mkdir(parents=True, exist_ok=True)
    snapshots, execution = {}, {}
    for variant, config in VARIANTS.items():
        path = f"sources/{variant}.zip"
        fingerprint = base._snapshot(output / path, BASELINE_COMMIT if variant == "baseline" else None)
        snapshots[variant] = {"path": path, "sha256": fingerprint, "config": config, "label": LABELS[variant],
                              "source_ref": BASELINE_COMMIT if variant == "baseline" else "working_tree",
                              "role": "baseline" if variant == "baseline" else "candidate"}
    for name in EXECUTION_NAMES:
        source = ROOT / f"algorithm/experiments/{name}.py"
        target = output / "sources" / source.name
        target.write_bytes(source.read_bytes())
        execution[name] = {"path": str(target.relative_to(output)), "sha256": base.sha256(target)}
    manifest = {"schema_version": 6, "kind": "method_trials_iteration2", "baseline_variant": "baseline",
                "baseline_commit": BASELINE_COMMIT, "input_path": base.INPUT, "input_sha256": base.sha256(ROOT / base.INPUT),
                "source_snapshots": snapshots, "execution_sources": execution,
                "runner_sha256": execution["run_optimization_experiments"]["sha256"],
                "seeds": SEEDS, "protocol": PROTOCOL, "parent_binding": binding,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "environment": {"python": platform.python_version(), "platform": platform.platform()}}
    for name, data in (("manifest", manifest), ("runs", []), ("decisions", [])):
        base.write_json(output / f"{name}.json", data)
    return manifest


def verify(output, manifest):
    base.verify_execution_files(manifest)
    if (manifest.get("schema_version") != 6 or manifest.get("kind") != "method_trials_iteration2"
            or manifest.get("baseline_commit") != BASELINE_COMMIT or manifest.get("seeds") != SEEDS
            or manifest.get("protocol") != PROTOCOL or set(manifest.get("source_snapshots", {})) != set(VARIANTS)
            or {v: s.get("config") for v, s in manifest["source_snapshots"].items()} != VARIANTS):
        raise ValueError("固定追加协议改变")
    if manifest.get("parent_binding") != parent_evidence(output):
        raise ValueError("父批次冻结证据改变")
    if manifest.get("input_path") != base.INPUT or base.sha256(ROOT / base.INPUT) != manifest["input_sha256"]:
        raise ValueError("官方输入指纹改变")
    if set(manifest.get("execution_sources", {})) != set(EXECUTION_NAMES):
        raise ValueError("冻结执行源码不完整")
    for source in [*manifest["source_snapshots"].values(), *manifest["execution_sources"].values()]:
        if base.sha256(_safe(output, source["path"])) != source["sha256"]:
            raise ValueError("归档源码指纹改变")
    for name in EXECUTION_NAMES:
        if base.sha256(ROOT / f"algorithm/experiments/{name}.py") != manifest["execution_sources"][name]["sha256"]:
            raise ValueError("执行协议源码改变，禁止混用续跑")
    cells, paths = set(), set()
    for row in base.read_json(output / "runs.json"):
        stage, case, variant, seed = cell = _cell(row)
        if cell in cells or stage not in SEEDS or case not in CASES[stage] or variant not in VARIANTS or seed not in SEEDS[stage]:
            raise ValueError("重复或未预定实验单元")
        cells.add(cell)
        run_id = f"{stage}__{case}__{variant}__{seed}"
        if (row.get("run_id") != run_id or row.get("solution_file") != f"run_solutions/{run_id}.json"
                or row.get("source_sha256") != manifest["source_snapshots"][variant]["sha256"]
                or row.get("budget_seconds") != BUDGETS[stage][case]):
            raise ValueError("已有运行身份或预算改变")
        path = _safe(output, row["solution_file"])
        paths.add(path)
        if base.sha256(path) != row["solution_sha256"]:
            raise ValueError("已有解指纹改变")
    if any(p.resolve() not in paths for p in (output / "run_solutions").glob("*.json")):
        raise RuntimeError("未登记孤立解，禁止覆盖")
    aborted = [_cell(r) for r in _aborted(output)]
    if cells.intersection(aborted) or len(set(aborted)) != len(aborted):
        raise ValueError("中止单元重复或与完整单元重合")
    experiment_seconds(output)


def consumed(output):
    return experiment_seconds(output.parent) + experiment_seconds(output)


def complete(rows, stage):
    return all({b["seed"] for b, _ in pairs(rows, stage, "strings", case)} == set(SEEDS[stage]) for case in CASES[stage])


def screen_pass(rows):
    return (complete(rows, "screen") and first.wins(pairs(rows, "screen", "strings")) >= 1
            and all(base.score(c) <= base.score(b) for case in CASES["screen"] for b, c in pairs(rows, "screen", "strings", case)))


def medium_pass(rows):
    return complete(rows, "medium") and first.wins(pairs(rows, "medium", "strings")) >= 2


def stage(output, manifest, name):
    verify(output, manifest)
    rows, aborted = base.read_json(output / "runs.json"), _aborted(output)
    cells = {_cell(r) for r in rows}
    spent = sum(r["end_to_end_seconds"] for r in rows if r["stage"] == name)
    spent += sum(r.get("process_elapsed_seconds", 0.) for r in aborted if r.get("stage") == name)
    missing = sum(BUDGETS[name][case] for seed in SEEDS[name] for case in CASES[name] for v in VARIANTS if (name, case, v, seed) not in cells)
    if spent + missing > CAPS[name] or consumed(output) + missing > TOTAL_SECONDS - RESERVED_SECONDS:
        raise RuntimeError("共享预算不足以预留全部剩余阶段及审计时间")
    for index, seed in enumerate(SEEDS[name]):
        order = list(VARIANTS) if index % 2 == 0 else list(reversed(VARIANTS))
        for case in CASES[name]:
            for variant in order:
                cell = (name, case, variant, seed)
                if cell in cells:
                    continue
                if cell in {_cell(r) for r in aborted}:
                    raise RuntimeError("禁止重跑中止单元")
                if (output / f"run_solutions/{name}__{case}__{variant}__{seed}.json").exists():
                    raise RuntimeError("孤立解已存在，禁止覆盖")
                budget = BUDGETS[name][case]
                if spent + budget > CAPS[name] or consumed(output) + budget > TOTAL_SECONDS - RESERVED_SECONDS:
                    raise RuntimeError("累计共享预算不足，保留所有证据")
                row = base.invoke(output, manifest, variant, case, seed, name, budget)
                row["order"] = order.index(variant)
                rows.append(row)
                cells.add(cell)
                spent += row["end_to_end_seconds"]
                base.save_rows(output, rows)
    return rows


def run(output):
    manifest = base.read_json(output / "manifest.json")
    rows = stage(output, manifest, "screen")
    screened = screen_pass(rows)
    _decision(output, "screen", candidates={"strings": screened}, eligible_variants=["strings"] if screened else [],
              reason=PROTOCOL["screen_gate"])
    if screened:
        rows = stage(output, manifest, "medium")
    selected = "strings" if screened and medium_pass(rows) else None
    _decision(output, "medium", selected_variant=selected, passed=selected is not None, skipped=not screened,
              candidates={"strings": medium_pass(rows)} if screened else {}, continuation_after_regression=True,
              reason=PROTOCOL["medium_gate"])
    summary = base.summarize(output)
    summary.update(experiment_seconds=experiment_seconds(output), shared_experiment_seconds=consumed(output),
                   selected_variant=selected, default_eligible=selected is not None, exploratory_promotion=True,
                   note="独立追加批次只比较自己的当前main基线；3种子30秒多数胜允许退化，分钟级收益未知")
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
