"""Frozen, sequential follow-up of the short/long route-regret reversal.

Run prepare once, then run. This bounded protocol does not launch the paper
matrix or overwrite the earlier optimization batch.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import platform
import time

from algorithm.experiments import run_optimization_experiments as base


ROOT = base.ROOT
OUTPUT = ROOT / "algorithm/results/regret_followup_20261013"
BASELINE_COMMIT = "6afe5f4aee12b8d8fddbcf92c45f306a31749e6f"
VARIANTS = {
    "baseline": {},
    "old_allocation": {"enable_lazy_repair": True, "enable_route_distinct_regret": True},
    "fallback": {"enable_lazy_repair": True, "enable_route_distinct_regret": True,
                 "enable_regret_position_fallback": True},
    "portfolio": {"enable_lazy_repair": True, "enable_route_distinct_regret": False,
                  "enable_regret_position_fallback": True, "enable_regret_portfolio": True},
}
LABELS = {"baseline": "默认基线", "old_allocation": "旧跨机 regret",
          "fallback": "位置回退候选", "portfolio": "自适应组合候选"}
SEEDS = {"screen": [2026101341, 2026101342],
         "medium": [2026101351, 2026101352, 2026101353],
         "confirm": [2026101321, 2026101361, 2026101362, 2026101363],
         "extensions": [2026101341, 2026101342]}
CASES = {"screen": ["single25", "fleet200"], "medium": ["single25", "fleet200"],
         "confirm": ["fleet200"],
         "extensions": ["fleet200__direct_relay", "fleet200__direct_stations", "fleet200__direct_relay_stations"]}
BUDGETS = {"screen": {"single25": 5, "fleet200": 10},
           "medium": {"single25": 5, "fleet200": 30},
           "confirm": {"single25": 120, "fleet200": 240},
           "extensions": {"fleet200": 20}}
CAPS = {"screen": 180, "medium": 240, "confirm": 1920, "extensions": 240}


def prepare(output: Path):
    if output.exists() and any(p.name != "diagnostics" for p in output.iterdir()):
        raise ValueError("新批次目录只能包含只读诊断；不能覆盖已有实验")
    output.mkdir(parents=True, exist_ok=True)
    baseline_hash = base._snapshot(output / "sources/baseline.zip", BASELINE_COMMIT)
    candidate_hash = base._snapshot(output / "sources/candidate.zip")
    snapshots = {}
    for name, config in VARIANTS.items():
        historical = name in {"baseline", "old_allocation"}
        snapshots[name] = {
            "path": "sources/baseline.zip" if historical else "sources/candidate.zip",
            "sha256": baseline_hash if historical else candidate_hash,
            "source_ref": BASELINE_COMMIT if historical else "working_tree",
            "config": config, "label": LABELS[name],
            "role": "candidate" if not historical else "baseline" if name == "baseline" else "control",
        }
    execution = {}
    for name, source in (("run_optimization_experiments", Path(base.__file__)),
                         ("run_scenario_comparison", ROOT / "algorithm/experiments/run_scenario_comparison.py"),
                         ("run_regret_followup", Path(__file__))):
        target = output / "sources" / (name + ".py")
        target.write_bytes(source.read_bytes())
        execution[name] = {"path": str(target.relative_to(output)), "sha256": base.sha256(target)}
    manifest = {
        "schema_version": 2, "kind": "regret_followup", "baseline_variant": "baseline",
        "baseline_commit": BASELINE_COMMIT, "input_path": base.INPUT,
        "input_sha256": base.sha256(ROOT / base.INPUT), "source_snapshots": snapshots,
        "runner_sha256": base.sha256(Path(base.__file__)), "execution_sources": execution,
        "seeds": SEEDS, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "protocol": {
            "total_experiment_seconds": 3600, "stage_caps_seconds": CAPS,
            "stage_cases": CASES, "stage_budgets_seconds": BUDGETS,
            "single": {"tasks": 25, "drones": 1}, "fleet": {"tasks": 200, "drones": 8},
            "max_tasks_per_drone": 25, "capacity": 2, "speed_km_per_min": .9,
            "deadline_multiplier": 1., "relay_count": 4, "relay_location_seed": 42,
            "relay_direct_warmup_fraction": .8, "safety_margin_seconds": 1.,
            "time_boundary": "CSV读取前至完整核验和解JSON写出后；不含进程启动和模块导入",
            "comparison": "逐种子三级词典序；总逾期分钟是项目引入的次级目标",
            "quality_gate": "初筛集群2配对不劣且至少1胜、单机无退化；中测集群至少2胜且两实例均无逾期数增加",
            "selection": "最多两项新候选；通过初筛后按相对旧跨机版胜/负次数、相对基线胜次数选择，完全并列优先fallback；不按均值三元组判胜",
            "promotion": "先复测已知失败seed1321，再验证3个新种子；全部4个集群长测配对词典序不退化且新种子至少2胜才考虑默认启用",
            "confirm_seed_roles": {"2026101321": "已知反例复测，不是独立留出样本",
                                   "2026101361": "新种子", "2026101362": "新种子", "2026101363": "新种子"},
            "single_drone": "新机制不作用于单机；以固定迭代完整决策等价和5秒配对验证，不重复120秒长测",
            "early_stop": "任一确认配对词典序退化则停止剩余长测；所有结果保留且不换种子",
            "frozen_manuscript_results": "保留上一批及正式论文数据；新证据单独存放",
        },
    }
    base.write_json(output / "manifest.json", manifest)
    base.write_json(output / "runs.json", [])
    base.write_json(output / "decisions.json", [])
    return manifest


def verify(output, manifest):
    base.verify_execution_files(manifest)
    for name, source in manifest["execution_sources"].items():
        if base.sha256(output / source["path"]) != source["sha256"]:
            raise ValueError("归档执行源码改变")
    if base.sha256(Path(__file__)) != manifest["execution_sources"]["run_regret_followup"]["sha256"]:
        raise ValueError("后续实验运行器改变；禁止混用协议续跑")
    if (manifest["seeds"] != SEEDS or manifest["protocol"]["stage_cases"] != CASES
            or manifest["protocol"]["stage_budgets_seconds"] != BUDGETS):
        raise ValueError("固定种子、场景或预算改变")
    if ({name: item["config"] for name,item in manifest["source_snapshots"].items()} != VARIANTS
            or manifest["protocol"]["stage_caps_seconds"] != CAPS
            or manifest["protocol"]["total_experiment_seconds"] != 3600):
        raise ValueError("冻结版本配置或累计预算改变")


def pairs(rows, stage, variant, case="fleet200"):
    return base.paired(rows, stage, variant, case)


def complete(rows, stage, variant):
    return all({b["seed"] for b, _ in pairs(rows, stage, variant, case)} == set(SEEDS[stage])
               for case in CASES[stage])


def wins(pairset):
    return sum(base.score(c) < base.score(b) for b, c in pairset)


def screen_pass(rows, variant):
    fleet = pairs(rows, "screen", variant)
    return (complete(rows, "screen", variant) and wins(fleet) >= 1
            and all(base.score(c) <= base.score(b) for b, c in fleet + pairs(rows, "screen", variant, "single25")))


def choose_candidate(rows):
    eligible = [v for v in ("fallback", "portfolio") if screen_pass(rows, v)]
    def rank(variant):
        old = {r["seed"]: r for r in rows if r["stage"] == "screen" and r["case_id"] == "fleet200" and r["variant"] == "old_allocation" and r["valid"] and r["budget_compliant"]}
        candidate = [c for _, c in pairs(rows, "screen", variant)]
        comparisons = [(old[c["seed"]], c) for c in candidate if c["seed"] in old]
        return (wins(comparisons), -sum(base.score(c) > base.score(b) for b,c in comparisons),
                wins(pairs(rows, "screen", variant)), variant == "fallback")
    return max(eligible, key=rank) if eligible else None


def medium_pass(rows, variant):
    return (complete(rows, "medium", variant) and wins(pairs(rows, "medium", variant)) >= 2
            and all(c["late_count"] <= b["late_count"] for case in CASES["medium"] for b,c in pairs(rows, "medium", variant, case)))


def confirm_pass(rows, variant):
    matched = pairs(rows, "confirm", variant)
    return (complete(rows, "confirm", variant) and all(base.score(c) <= base.score(b) for b,c in matched)
            and wins([(b,c) for b,c in matched if b["seed"] != 2026101321]) >= 2)


def stage(output, manifest, name, variants):
    verify(output, manifest)
    rows = base.read_json(output / "runs.json")
    for index, seed in enumerate(SEEDS[name]):
        order = list(variants) if index % 2 == 0 else list(reversed(variants))
        for case in CASES[name]:
            for variant in order:
                if name == "confirm" and any(base.confirmation_regressed(rows, v) for v in variants if v != "baseline"):
                    base.write_json(output / "execution_notes.json", {
                        "early_stop": True, "policy": "确认出现配对退化，门槛已无法满足；停止剩余长测，不重跑或换种子。",
                        "aborted_runs": [], "stopped_before": {"case_id": case, "variant": variant, "seed": seed}})
                    return rows
                cell = (name, case, variant, seed)
                if any((r["stage"],r["case_id"],r["variant"],r["seed"]) == cell for r in rows):
                    continue
                notes = base.read_json(output / "execution_notes.json") if (output / "execution_notes.json").exists() else {}
                aborted = notes.get("aborted_runs", [])
                if any((r.get("stage"),r.get("case_id"),r.get("variant"),r.get("seed")) == cell for r in aborted):
                    raise RuntimeError("禁止重跑已中止单元")
                budget = BUDGETS[name][case.split("__")[0]]
                spent = sum(r["end_to_end_seconds"] for r in rows if r["stage"] == name)
                failed_seconds = sum(r.get("process_elapsed_seconds", 0) for r in aborted)
                total = sum(r["end_to_end_seconds"] for r in rows) + failed_seconds
                if spent + budget > CAPS[name] or total + budget > 3540:
                    raise RuntimeError("预算不足；预留60秒用于小诊断与完整复算")
                row = base.invoke(output, manifest, variant, case, seed, name, budget)
                row["order"] = order.index(variant)
                rows.append(row)
                base.save_rows(output, rows)
    return rows


def run(output):
    manifest = base.read_json(output / "manifest.json")
    rows = stage(output, manifest, "screen", list(VARIANTS))
    candidate = choose_candidate(rows)
    base.decision(output, "screen", selected_variant=candidate,
                  candidates={v: screen_pass(rows,v) for v in ("fallback","portfolio")},
                  reason="预定门槛筛选唯一候选；旧跨机版仅作为机制对照")
    if candidate:
        rows = stage(output, manifest, "medium", ["baseline", candidate])
        passed = medium_pass(rows,candidate)
        base.decision(output, "medium", selected_variant=candidate, passed=passed,
                      reason="集群至少2胜、两个实例均不增加逾期任务数")
        if passed:
            rows = stage(output, manifest, "confirm", ["baseline",candidate])
            base.decision(output, "confirm", selected_variant=candidate, passed=confirm_pass(rows,candidate),
                          early_stopped=base.confirmation_regressed(rows,candidate),
                          completed_pairs=len(pairs(rows,"confirm",candidate)), planned_pairs=4,
                          reason="已知反例加3个新种子；任何词典序退化即终止默认晋级")
        else:
            base.decision(output,"confirm",passed=False,skipped=True,reason="中测未晋级，不启动长测")
    else:
        base.decision(output,"medium",passed=False,skipped=True,reason="初筛无候选达到门槛")
        base.decision(output,"confirm",passed=False,skipped=True,reason="初筛无候选，不扩大测试")
    # Compatibility only for the selected implementation; unit tests cover
    # both switches if neither candidate advances.
    if candidate:
        rows = stage(output,manifest,"extensions",["baseline",candidate])
        base.decision(output,"extensions",selected_variant=candidate,
                      reason="小预算兼容核验，不能替代原题确认")
    else:
        base.decision(output,"extensions",skipped=True,reason="初筛未晋级，以针对性功能测试验证兼容")
    base.summarize(output)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare","run"))
    parser.add_argument("--output-dir",type=Path,default=OUTPUT)
    args=parser.parse_args(argv)
    output=args.output_dir.resolve()
    if args.action=="prepare": prepare(output)
    else: run(output)


if __name__ == "__main__":
    main()
