"""Post-hoc, budget-shared trial of a smaller DIRECT destroy neighborhood.

Both arms execute the parent's frozen default solver. Only the compact arm's
destroy fractions differ. Completed parent evidence is immutable; baselines
are rerun in this separate cohort and are never selected across cohorts.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import platform
import time

from algorithm.experiments import run_optimization_experiments as base

ROOT = base.ROOT
OUTPUT = ROOT / "algorithm/results/objective_trials_20261013/compact_trial"
BASELINE_COMMIT = "32c2d6055d89e831f0e8b783d1cac5c2c750fb1c"
VARIANTS = {"baseline": {}, "compact": {"min_destroy_fraction": .02, "max_destroy_fraction": .05}}
LABELS = {"baseline": "当前默认基线", "compact": "缩小常规邻域"}
SEEDS = {
    "equivalence": [2026101401, 2026101402, 2026101403],
    "screen": [2026101401, 2026101402],
    "medium": [2026101411, 2026101412, 2026101413],
    "confirm": [2026101321, 2026101421, 2026101422, 2026101423],
}
CASES = {"equivalence": ["single25"], "screen": ["single25", "fleet200"],
         "medium": ["single25", "fleet200"], "confirm": ["fleet200"]}
BUDGETS = {"equivalence": {"single25": 10}, "screen": {"single25": 5, "fleet200": 10},
           "medium": {"single25": 5, "fleet200": 30}, "confirm": {"fleet200": 240}}
CAPS = {"equivalence": 60, "screen": 60, "medium": 210, "confirm": 1920}
TOTAL_SECONDS = 2700
RESERVED_SECONDS = 60


def _aborted(output):
    path = output / "execution_notes.json"
    return base.read_json(path).get("aborted_runs", []) if path.exists() else []


def experiment_seconds(output):
    values = [r["end_to_end_seconds"] for r in base.read_json(output / "runs.json")]
    values += [r.get("process_elapsed_seconds", 0.) for r in _aborted(output)]
    if any(not math.isfinite(v) or v < 0 for v in values):
        raise ValueError("实验耗时必须有限且非负")
    return sum(values)


def _safe_file(output, relative):
    path = (output / relative).resolve()
    if not path.is_relative_to(output.resolve()):
        raise ValueError("归档路径越出实验目录")
    return path


def parent_evidence(output):
    """Require both original candidates' completed, failed medium stages."""
    parent = output.parent
    manifest = base.read_json(parent / "manifest.json")
    rows = base.read_json(parent / "runs.json")
    decisions = base.read_json(parent / "decisions.json")
    if manifest.get("kind") != "objective_trials" or manifest.get("baseline_commit") != BASELINE_COMMIT:
        raise ValueError("追加批次必须来自指定的原题候选父批次")
    if len({d["stage"] for d in decisions}) != len(decisions):
        raise ValueError("父批次阶段决策重复")
    medium = next((d for d in decisions if d.get("stage") == "medium"), {})
    confirm = next((d for d in decisions if d.get("stage") == "confirm"), {})
    if (medium.get("passed") is not False or medium.get("selected_variant") is not None
            or medium.get("candidates") != {"boundary": False, "objective": False}
            or confirm.get("skipped") is not True or confirm.get("passed") is not False
            or any(r["stage"] == "confirm" for r in rows)):
        raise ValueError("父批次须已完成两候选中测失败并跳过确认")
    for variant in ("boundary", "objective"):
        matched = {}
        for case in ("single25", "fleet200"):
            matched[case] = base.paired(rows, "medium", variant, case)
            if {b["seed"] for b, _ in matched[case]} != set(manifest["seeds"]["medium"]):
                raise ValueError("父批次中测配对尚未完整结束")
        fleet = matched["fleet200"]
        passed = (sum(base.score(c) < base.score(b) for b, c in fleet) >= 2
                  and all(c["late_count"] <= b["late_count"] for b, c in fleet)
                  and all(base.score(c) <= base.score(b) for b, c in matched["single25"]))
        if passed:
            raise ValueError("父批次实际中测通过，与失败决定矛盾")
    if base.sha256(ROOT / manifest["input_path"]) != manifest["input_sha256"]:
        raise ValueError("父批次输入已改变")
    for source in manifest["execution_sources"].values():
        if base.sha256(_safe_file(parent, source["path"])) != source["sha256"]:
            raise ValueError("父批次归档执行源码改变")
    for source in manifest["source_snapshots"].values():
        if base.sha256(_safe_file(parent, source["path"])) != source["sha256"]:
            raise ValueError("父批次求解器快照改变")
    for row in rows:
        if base.sha256(_safe_file(parent, row["solution_file"])) != row["solution_sha256"]:
            raise ValueError("父批次已完成解指纹改变")
    return manifest, experiment_seconds(parent)


def prepare(output: Path):
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("独立追加目录必须为空，不能覆盖历史批次")
    parent_manifest, parent_seconds = parent_evidence(output)
    if parent_seconds + CAPS["equivalence"] > TOTAL_SECONDS - RESERVED_SECONDS:
        raise ValueError("共享45分钟预算不足以启动追加批次")
    output.mkdir(parents=True, exist_ok=True)
    sources = output / "sources"
    sources.mkdir()
    parent_source = parent_manifest["source_snapshots"]["baseline"]
    archive = sources / "baseline.zip"
    archive.write_bytes(_safe_file(output.parent, parent_source["path"]).read_bytes())
    source_hash = base.sha256(archive)
    if source_hash != parent_source["sha256"]:
        raise ValueError("继承父批次默认源码失败")
    snapshots = {name: {"path": "sources/baseline.zip", "sha256": source_hash,
                       "source_ref": BASELINE_COMMIT, "config": config,
                       "label": LABELS[name], "role": "baseline" if name == "baseline" else "candidate"}
                 for name, config in VARIANTS.items()}
    execution = {}
    for name, source in (("run_optimization_experiments", Path(base.__file__)),
                         ("run_scenario_comparison", ROOT / "algorithm/experiments/run_scenario_comparison.py"),
                         ("run_compact_trials", Path(__file__))):
        target = sources / (name + ".py")
        target.write_bytes(source.read_bytes())
        execution[name] = {"path": str(target.relative_to(output)), "sha256": base.sha256(target)}
    notes = output.parent / "execution_notes.json"
    marker = {
        "reason": "父批次两候选中测失败后，用户选择追加缩小常规邻域；独立事后探索，不复用或挑选父批次基线。",
        "parent_directory": "..", "parent_experiment_seconds": parent_seconds,
        "parent_execution_notes_sha256": base.sha256(notes) if notes.exists() else None,
        **{f"parent_{name}_sha256": base.sha256(output.parent / f"{name}.json")
           for name in ("manifest", "runs", "decisions")},
    }
    manifest = {
        "schema_version": 4, "kind": "compact_trials", "baseline_variant": "baseline",
        "baseline_commit": BASELINE_COMMIT, "input_path": parent_manifest["input_path"],
        "input_sha256": parent_manifest["input_sha256"], "source_snapshots": snapshots,
        "runner_sha256": base.sha256(Path(base.__file__)), "execution_sources": execution,
        "seeds": SEEDS, "post_hoc_exploration": marker,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "protocol": {
            "total_experiment_seconds": TOTAL_SECONDS, "reserved_seconds": RESERVED_SECONDS,
            "shared_budget": "父批次全部完整运行、中止耗时与子批次共享45分钟；阶段开始前预留本阶段全部未运行单元预算",
            "stage_caps_seconds": CAPS, "stage_cases": CASES, "stage_budgets_seconds": BUDGETS,
            "single": {"tasks": 25, "drones": 1}, "fleet": {"tasks": 200, "drones": 8},
            "max_tasks_per_drone": 25, "capacity": 2, "speed_km_per_min": .9,
            "deadline_multiplier": 1., "relay_count": 0, "safety_margin_seconds": 1.,
            "time_boundary": "CSV读取前至完整核验和解JSON写出后，不含进程启动与模块导入",
            "comparison": "子批同实例同预算同种子同初解逐对比较三级词典序；不与父批复用基线或合并均值",
            "screen_gate": "两个集群配对至少一胜，逾期数差合计不大于0；单机无词典序退化；固定200轮单机等价",
            "medium_gate": "三个集群配对至少两胜，每对逾期数不增加，单机无词典序退化",
            "confirmation_gate": "四个集群配对全部词典序不退化，三个新种子至少两胜；中测亦须通过",
            "confirmation_policy": "即使一次退化，也完成已定四种子确认；不换种子、不重跑择优",
            "confirm_seed_roles": {"2026101321": "历史反例压力复测，不是独立留出",
                                   "2026101421": "新种子", "2026101422": "新种子", "2026101423": "新种子"},
            "single_drone": "n=25时基线3%—8%与候选2%—5%经round及最少移除2任务后均为每轮2任务；3种子固定200轮验证完整搜索等价",
            "default_policy": "全部门槛通过才考虑默认采用；失败保留默认3%—8%",
            "frozen_manuscript_results": "保留原始题目、官方数据、论文和父批次全部产物",
        },
    }
    base.write_json(output / "manifest.json", manifest)
    base.write_json(output / "runs.json", [])
    base.write_json(output / "decisions.json", [])
    return manifest


def verify(output, manifest):
    base.verify_execution_files(manifest)
    if manifest.get("schema_version") != 4 or manifest.get("kind") != "compact_trials":
        raise ValueError("不是固定compact追加协议")
    for name, source in manifest["execution_sources"].items():
        if base.sha256(_safe_file(output, source["path"])) != source["sha256"]:
            raise ValueError("归档执行源码改变")
    if base.sha256(Path(__file__)) != manifest["execution_sources"]["run_compact_trials"]["sha256"]:
        raise ValueError("追加运行器已修改，禁止混用版本续跑")
    if base.sha256(ROOT / manifest["input_path"]) != manifest["input_sha256"]:
        raise ValueError("官方输入指纹改变")
    protocol = manifest["protocol"]
    if (manifest["seeds"] != SEEDS or protocol["stage_cases"] != CASES
            or protocol["stage_budgets_seconds"] != BUDGETS or protocol["stage_caps_seconds"] != CAPS
            or protocol["total_experiment_seconds"] != TOTAL_SECONDS
            or protocol["reserved_seconds"] != RESERVED_SECONDS
            or {v: s["config"] for v, s in manifest["source_snapshots"].items()} != VARIANTS):
        raise ValueError("固定追加实验协议改变")
    marker = manifest["post_hoc_exploration"]
    if marker["parent_directory"] != "..":
        raise ValueError("父批次路径改变")
    for name in ("manifest", "runs", "decisions"):
        if base.sha256(output.parent / f"{name}.json") != marker[f"parent_{name}_sha256"]:
            raise ValueError("父批次证据改变，禁止续跑")
    notes = output.parent / "execution_notes.json"
    if (base.sha256(notes) if notes.exists() else None) != marker["parent_execution_notes_sha256"]:
        raise ValueError("父批次中止记录改变")
    parent_manifest, parent_seconds = parent_evidence(output)
    if parent_seconds != marker["parent_experiment_seconds"]:
        raise ValueError("绑定的父批次累计耗时不一致")
    expected_source = parent_manifest["source_snapshots"]["baseline"]["sha256"]
    for source in manifest["source_snapshots"].values():
        if (source["source_ref"] != BASELINE_COMMIT or source["sha256"] != expected_source
                or base.sha256(_safe_file(output, source["path"])) != source["sha256"]):
            raise ValueError("子批次只能使用父默认求解器快照")
    rows = base.read_json(output / "runs.json")
    cells = set()
    referenced = set()
    for row in rows:
        cell = (row["stage"], row["case_id"], row["variant"], row["seed"])
        stage_name, case, variant, seed = cell
        if (cell in cells or stage_name not in SEEDS or case not in CASES[stage_name]
                or seed not in SEEDS[stage_name] or variant not in VARIANTS):
            raise ValueError("已有实验单元重复或不属于预定协议")
        cells.add(cell)
        run_id = f"{stage_name}__{case}__{variant}__{seed}"
        if row["run_id"] != run_id or row["solution_file"] != f"run_solutions/{run_id}.json":
            raise ValueError("已有解路径与实验单元不一致")
        solution = _safe_file(output, row["solution_file"])
        referenced.add(solution)
        if base.sha256(solution) != row["solution_sha256"] or row["source_sha256"] != expected_source:
            raise ValueError("已有解或来源指纹改变，禁止续跑")
        if row["budget_seconds"] != BUDGETS[stage_name][case]:
            raise ValueError("已有运行预算不属于固定协议")
        expected_config = VARIANTS[variant]
        for key in ("min_destroy_fraction", "max_destroy_fraction"):
            if row["config"].get(key) != expected_config.get(key):
                raise ValueError("已有运行使用了不同邻域参数")
        payload = base.read_json(solution)
        if base.score(payload["score"]) != base.score(row) or payload["iterations"] != row["iterations"]:
            raise ValueError("已有行指标与完整解不一致")
        fingerprint = base.value_hash({
            "routes": [r["encoded_visits"] for r in payload["routes"]],
            "score": payload["score"], "iterations": payload["iterations"],
            "trajectory": [[p[0], *p[2:]] for p in payload["metadata"]["best_trajectory"]],
            "operator_statistics": payload["metadata"]["operator_statistics"],
            "operator_weights": payload["metadata"]["operator_weights"],
            "accepted_solutions": payload["metadata"]["accepted_solutions"],
        })
        if fingerprint != row["decision_fingerprint"]:
            raise ValueError("已有完整解的搜索决策指纹不一致")
    if any(path.resolve() not in referenced for path in (output / "run_solutions").glob("*.json")):
        raise RuntimeError("发现未登记的孤立解文件，禁止重跑覆盖；保留文件供审计")
    if cells & {(r.get("stage"), r.get("case_id"), r.get("variant"), r.get("seed")) for r in _aborted(output)}:
        raise ValueError("同一单元同时有完整和中止记录")
    experiment_seconds(output)


def pairs(rows, stage, variant="compact", case="fleet200"):
    return base.paired(rows, stage, variant, case)


def complete(rows, stage, variant="compact"):
    return all({b["seed"] for b, _ in pairs(rows, stage, variant, case)} == set(SEEDS[stage])
               for case in CASES[stage])


def wins(matched):
    return sum(base.score(c) < base.score(b) for b, c in matched)


def equivalence_pass(rows):
    return (complete(rows, "equivalence") and all(
        b["iterations"] == c["iterations"] == 200 and b["decision_fingerprint"] == c["decision_fingerprint"]
        for b, c in pairs(rows, "equivalence", case="single25")))


def screen_pass(rows, equivalent=True):
    fleet = pairs(rows, "screen")
    return (equivalent and complete(rows, "screen") and wins(fleet) >= 1
            and sum(c["late_count"] - b["late_count"] for b, c in fleet) <= 0
            and all(base.score(c) <= base.score(b) for b, c in pairs(rows, "screen", case="single25")))


def medium_pass(rows):
    fleet = pairs(rows, "medium")
    return (complete(rows, "medium") and wins(fleet) >= 2
            and all(c["late_count"] <= b["late_count"] for b, c in fleet)
            and all(base.score(c) <= base.score(b) for b, c in pairs(rows, "medium", case="single25")))


def confirm_pass(rows):
    matched = pairs(rows, "confirm")
    return (medium_pass(rows) and complete(rows, "confirm")
            and all(base.score(c) <= base.score(b) for b, c in matched)
            and wins([(b, c) for b, c in matched if b["seed"] != 2026101321]) >= 2)


def consumed(output):
    return experiment_seconds(output.parent) + experiment_seconds(output)


def stage(output, manifest, name):
    verify(output, manifest)
    rows = base.read_json(output / "runs.json")
    cells = {(r["stage"], r["case_id"], r["variant"], r["seed"]) for r in rows}
    missing = sum(BUDGETS[name][case] for seed in SEEDS[name] for case in CASES[name]
                  for variant in VARIANTS if (name, case, variant, seed) not in cells)
    aborted = _aborted(output)
    spent = sum(r["end_to_end_seconds"] for r in rows if r["stage"] == name)
    spent += sum(r.get("process_elapsed_seconds", 0.) for r in aborted if r.get("stage") == name)
    if spent + missing > CAPS[name] or consumed(output) + missing > TOTAL_SECONDS - RESERVED_SECONDS:
        raise RuntimeError("共享45分钟预算不足，不能预留全部剩余阶段单元；保留已有结果")
    for index, seed in enumerate(SEEDS[name]):
        order = list(VARIANTS) if index % 2 == 0 else list(reversed(VARIANTS))
        for case in CASES[name]:
            for variant in order:
                cell = (name, case, variant, seed)
                if cell in cells:
                    continue
                if any((r.get("stage"), r.get("case_id"), r.get("variant"), r.get("seed")) == cell for r in aborted):
                    raise RuntimeError("禁止重跑已有中止单元")
                solution = output / "run_solutions" / f"{name}__{case}__{variant}__{seed}.json"
                if solution.exists():
                    raise RuntimeError("孤立解文件已存在，禁止覆盖或重跑择优")
                budget = BUDGETS[name][case]
                if spent + budget > CAPS[name] or consumed(output) + budget > TOTAL_SECONDS - RESERVED_SECONDS:
                    raise RuntimeError("累计求解预算不足，保留已有全部结果")
                row = base.invoke(output, manifest, variant, case, seed, name, budget,
                                  iterations=200 if name == "equivalence" else None)
                row["order"] = order.index(variant)
                rows.append(row)
                cells.add(cell)
                spent += row["end_to_end_seconds"]
                base.save_rows(output, rows)
    return rows


def run(output):
    manifest = base.read_json(output / "manifest.json")
    rows = stage(output, manifest, "equivalence")
    equivalent = equivalence_pass(rows)
    base.decision(output, "equivalence", selected_variant="compact", passed=equivalent,
                  completed_pairs=len(pairs(rows, "equivalence", case="single25")), planned_pairs=3,
                  reason="单机均每轮移除2任务；3种子固定200轮，完整决策指纹必须相同")
    rows = stage(output, manifest, "screen")
    screened = screen_pass(rows, equivalent)
    base.decision(output, "screen", selected_variant="compact", passed=screened,
                  reason="子批独立新基线；集群至少一胜且逾期数差合计不增加，单机不退化且固定迭代等价")
    if screened:
        rows = stage(output, manifest, "medium")
        passed = medium_pass(rows)
        base.decision(output, "medium", selected_variant="compact", passed=passed,
                      reason="至少两个集群配对获胜，每对逾期数不增加，单机不退化")
    else:
        passed = False
        base.decision(output, "medium", selected_variant=None, passed=False, skipped=True,
                      reason="初筛未过，不扩大求解")
    if passed:
        rows = stage(output, manifest, "confirm")
        base.decision(output, "confirm", selected_variant="compact", passed=confirm_pass(rows),
                      completed_pairs=len(pairs(rows, "confirm")), planned_pairs=4,
                      continuation_after_regression=True,
                      reason="完整跑完历史反例加三个新种子；全部不退化且新种子至少两胜才通过")
    else:
        base.decision(output, "confirm", selected_variant=None, passed=False, skipped=True,
                      reason="中测未过，不启动长测")
    summary = base.summarize(output)
    summary["parent_experiment_seconds"] = experiment_seconds(output.parent)
    summary["parent_and_supplement_experiment_seconds"] = consumed(output)
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
