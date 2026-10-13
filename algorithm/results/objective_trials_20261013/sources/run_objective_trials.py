"""Bounded trials of deadline-boundary removal and objective-aware rewards.

Immutable solver snapshots, new paired baselines, and sequential wall-clock
budgets. No manuscript matrix is run and no historical result is overwritten.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import platform
import time

from algorithm.experiments import run_optimization_experiments as base

ROOT = base.ROOT
OUTPUT = ROOT / "algorithm/results/objective_trials_20261013"
BASELINE_COMMIT = "32c2d6055d89e831f0e8b783d1cac5c2c750fb1c"
VARIANTS = {
    "baseline": {},
    "boundary": {"enable_boundary_destroy": True},
    "objective": {"enable_objective_rewards": True},
}
LABELS = {"baseline": "当前默认基线", "boundary": "截止期边界邻域", "objective": "第一目标奖励"}
SEEDS = {
    "equivalence": [2026101371, 2026101372, 2026101373],
    "screen": [2026101371, 2026101372],
    "medium": [2026101381, 2026101382, 2026101383],
    "confirm": [2026101321, 2026101391, 2026101392, 2026101393],
}
CASES = {"equivalence": ["single25"], "screen": ["single25", "fleet200"],
         "medium": ["single25", "fleet200"], "confirm": ["fleet200"]}
BUDGETS = {"equivalence": {"single25": 10}, "screen": {"single25": 5, "fleet200": 10},
           "medium": {"single25": 5, "fleet200": 30}, "confirm": {"fleet200": 240}}
CAPS = {"equivalence": 90, "screen": 90, "medium": 315, "confirm": 1920}
TOTAL_SECONDS = 2700
RESERVED_SECONDS = 60


def prepare(output: Path):
    if output.exists() and any(output.iterdir()):
        raise ValueError("新实验目录必须为空，不覆盖或续写历史批次")
    output.mkdir(parents=True, exist_ok=True)
    hashes = {"baseline": base._snapshot(output / "sources/baseline.zip", BASELINE_COMMIT),
              "candidate": base._snapshot(output / "sources/candidate.zip")}
    snapshots = {}
    for name, config in VARIANTS.items():
        source = "baseline" if name == "baseline" else "candidate"
        snapshots[name] = {"path": f"sources/{source}.zip", "sha256": hashes[source],
                           "source_ref": BASELINE_COMMIT if name == "baseline" else "working_tree",
                           "config": config, "label": LABELS[name],
                           "role": "baseline" if name == "baseline" else "candidate"}
    execution = {}
    for name, source in (("run_optimization_experiments", Path(base.__file__)),
                         ("run_scenario_comparison", ROOT / "algorithm/experiments/run_scenario_comparison.py"),
                         ("run_objective_trials", Path(__file__))):
        target = output / "sources" / (name + ".py")
        target.write_bytes(source.read_bytes())
        execution[name] = {"path": str(target.relative_to(output)), "sha256": base.sha256(target)}
    manifest = {
        "schema_version": 3, "kind": "objective_trials", "baseline_variant": "baseline",
        "baseline_commit": BASELINE_COMMIT, "input_path": base.INPUT,
        "input_sha256": base.sha256(ROOT / base.INPUT), "source_snapshots": snapshots,
        "runner_sha256": base.sha256(Path(base.__file__)), "execution_sources": execution,
        "seeds": SEEDS, "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "protocol": {
            "total_experiment_seconds": TOTAL_SECONDS, "reserved_seconds": RESERVED_SECONDS,
            "stage_caps_seconds": CAPS, "stage_cases": CASES, "stage_budgets_seconds": BUDGETS,
            "single": {"tasks": 25, "drones": 1}, "fleet": {"tasks": 200, "drones": 8},
            "max_tasks_per_drone": 25, "capacity": 2, "speed_km_per_min": .9,
            "deadline_multiplier": 1., "relay_count": 0, "safety_margin_seconds": 1.,
            "time_boundary": "CSV读取前至完整核验和解JSON写出后，不含进程启动与模块导入",
            "comparison": "同批同实例同预算同种子同初解逐对比较三级词典序；总逾期分钟是项目次级目标",
            "screen_gate": "两个集群配对至少一胜，逾期数差合计不大于0；单机无词典序退化；固定200轮单机等价",
            "medium_gate": "三个集群配对至少两胜，每对逾期数不增加，单机无词典序退化",
            "selection": "所有初筛过关候选参加中测；合格者按集群胜数、逾期数减少总量、次级改善胜数排序，完全并列选boundary",
            "confirmation_gate": "四个集群配对全部词典序不退化，三个新种子至少两胜；中测亦须通过",
            "confirmation_policy": "用户选择：即使一次退化，也完成已定四种子确认；不换种子、不重跑择优",
            "confirm_seed_roles": {"2026101321": "历史反例压力复测，不是独立留出",
                                   "2026101391": "新种子", "2026101392": "新种子", "2026101393": "新种子"},
            "single_drone": "两候选仅多机DIRECT生效；单机固定200轮等价及5秒配对，不重复无作用的单机长测",
            "default_policy": "中长测均改善且无明显退化才替换默认，未达门槛保持两个实验开关False",
            "frozen_manuscript_results": "保留原始题目、官方数据、论文和既有冻结批次",
            "user_choices": ["45分钟累计求解预算", "原题单机和8机集群", "报告先考核指标后证据", "适度探索短测门槛", "历史反例加3新种子", "完整跑完确认种子"],
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
    if base.sha256(Path(__file__)) != manifest["execution_sources"]["run_objective_trials"]["sha256"]:
        raise ValueError("运行协议源码改变，禁止混用版本续跑")
    protocol = manifest["protocol"]
    if (manifest["seeds"] != SEEDS or protocol["stage_cases"] != CASES
            or protocol["stage_budgets_seconds"] != BUDGETS or protocol["stage_caps_seconds"] != CAPS
            or protocol["total_experiment_seconds"] != TOTAL_SECONDS
            or protocol["reserved_seconds"] != RESERVED_SECONDS
            or {v: s["config"] for v, s in manifest["source_snapshots"].items()} != VARIANTS):
        raise ValueError("固定实验协议改变")


def pairs(rows, stage, variant, case="fleet200"):
    return base.paired(rows, stage, variant, case)


def complete(rows, stage, variant):
    return all({b["seed"] for b, _ in pairs(rows, stage, variant, case)} == set(SEEDS[stage])
               for case in CASES[stage])


def wins(pairset):
    return sum(base.score(c) < base.score(b) for b, c in pairset)


def screen_pass(rows, variant, equivalent=True):
    fleet = pairs(rows, "screen", variant)
    return (equivalent and complete(rows, "screen", variant) and wins(fleet) >= 1
            and sum(c["late_count"] - b["late_count"] for b, c in fleet) <= 0
            and all(base.score(c) <= base.score(b) for b, c in pairs(rows, "screen", variant, "single25")))


def medium_pass(rows, variant):
    fleet = pairs(rows, "medium", variant)
    return (complete(rows, "medium", variant) and wins(fleet) >= 2
            and all(c["late_count"] <= b["late_count"] for b, c in fleet)
            and all(base.score(c) <= base.score(b) for b, c in pairs(rows, "medium", variant, "single25")))


def choose_candidate(rows, eligible):
    candidates = [v for v in eligible if medium_pass(rows, v)]
    def rank(v):
        matched = pairs(rows, "medium", v)
        return (wins(matched), sum(b["late_count"]-c["late_count"] for b,c in matched),
                sum(c["late_count"] == b["late_count"] and base.score(c) < base.score(b) for b,c in matched),
                v == "boundary")
    return max(candidates, key=rank) if candidates else None


def confirm_pass(rows, variant):
    matched = pairs(rows, "confirm", variant)
    return (medium_pass(rows, variant) and complete(rows, "confirm", variant)
            and all(base.score(c) <= base.score(b) for b, c in matched)
            and wins([(b,c) for b,c in matched if b["seed"] != 2026101321]) >= 2)


def stage(output, manifest, name, variants):
    verify(output, manifest)
    rows = base.read_json(output / "runs.json")
    for index, seed in enumerate(SEEDS[name]):
        # Deterministic AB/BA; three-way pilot also rotates first position.
        order = list(variants) if index % 2 == 0 else list(reversed(variants))
        if len(variants) == 3 and index == 2:
            order = list(variants[1:]) + list(variants[:1])
        for case in CASES[name]:
            for variant in order:
                cell = (name, case, variant, seed)
                if any((r["stage"],r["case_id"],r["variant"],r["seed"]) == cell for r in rows):
                    continue
                notes_path = output / "execution_notes.json"
                aborted = base.read_json(notes_path).get("aborted_runs", []) if notes_path.exists() else []
                if any((r.get("stage"),r.get("case_id"),r.get("variant"),r.get("seed")) == cell for r in aborted):
                    raise RuntimeError("不允许重跑已有中止单元")
                budget = BUDGETS[name][case]
                total = sum(r["end_to_end_seconds"] for r in rows) + sum(r.get("process_elapsed_seconds",0) for r in aborted)
                spent = sum(r["end_to_end_seconds"] for r in rows if r["stage"] == name) + sum(r.get("process_elapsed_seconds",0) for r in aborted if r.get("stage") == name)
                if spent + budget > CAPS[name] or total + budget > TOTAL_SECONDS - RESERVED_SECONDS:
                    raise RuntimeError("累计求解预算不足，保留已有全部结果")
                row = base.invoke(output, manifest, variant, case, seed, name, budget,
                                  iterations=200 if name == "equivalence" else None)
                row["order"] = order.index(variant)
                rows.append(row)
                base.save_rows(output, rows)
    return rows


def _stable_decisions(output, row):
    payload = base.read_json(output / row["solution_file"])
    metadata = payload["metadata"]
    # Newly added diagnostic counters are not search decisions. Compare all
    # original counters and weights, visits, full score, and best iterations.
    original = ("uses", "accepted", "current_improvements", "best_improvements", "total_reward")
    return {"routes": [r["encoded_visits"] for r in payload["routes"]],
            "score": base.score(row), "iterations": row["iterations"],
            "trajectory": [[p[0], *p[2:]] for p in row["best_trajectory"]],
            "operator_statistics": {k: {f: stats[f] for f in original} for k, stats in metadata["operator_statistics"].items()},
            "operator_weights": metadata["operator_weights"], "accepted_solutions": metadata["accepted_solutions"]}


def equivalence_status(output, rows):
    status = {}
    for variant in VARIANTS.keys() - {"baseline"}:
        matched = pairs(rows, "equivalence", variant, "single25")
        status[variant] = (complete(rows, "equivalence", variant)
                           and all(b["iterations"] == c["iterations"] == 200
                                   and _stable_decisions(output,b) == _stable_decisions(output,c) for b,c in matched))
    return status


def run(output):
    manifest = base.read_json(output / "manifest.json")
    rows = stage(output, manifest, "equivalence", list(VARIANTS))
    eq = equivalence_status(output, rows)
    base.decision(output, "equivalence", candidates=eq, passed=all(eq.values()),
                  reason="单机固定200轮，比较完整路线、得分、原算子决策、权重和不含时钟的最优轨迹")
    rows = stage(output, manifest, "screen", list(VARIANTS))
    eligible = [v for v in VARIANTS if v != "baseline" and screen_pass(rows,v,eq[v])]
    base.decision(output, "screen", eligible_variants=eligible,
                  candidates={v: screen_pass(rows,v,eq[v]) for v in VARIANTS if v != "baseline"},
                  reason="用户选择适度探索：集群至少一胜且逾期数差合计不增加，单机不退化且固定迭代等价")
    candidate = None
    if eligible:
        rows = stage(output, manifest, "medium", ["baseline", *eligible])
        candidate = choose_candidate(rows, eligible)
        base.decision(output, "medium", selected_variant=candidate,
                      candidates={v: medium_pass(rows,v) for v in eligible}, passed=candidate is not None,
                      reason="集群至少2/3胜且每对逾期数不增加，单机不退化；按冻结规则选唯一长测候选")
    else:
        base.decision(output,"medium",passed=False,skipped=True,selected_variant=None,reason="初筛无候选通过，不扩大测试")
    if candidate:
        rows = stage(output, manifest, "confirm", ["baseline", candidate])
        base.decision(output, "confirm", selected_variant=candidate, passed=confirm_pass(rows,candidate),
                      completed_pairs=len(pairs(rows,"confirm",candidate)), planned_pairs=4,
                      reason="按用户选择跑完历史反例和三个新种子；全部不退化且新种子至少2胜才通过")
    else:
        base.decision(output,"confirm",passed=False,skipped=True,selected_variant=None,reason="中测无候选通过，不启动长测")
    # Every actual run (including fixed iterations) appears once in runs.json.
    return base.summarize(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    prepare(output) if args.command == "prepare" else run(output)


if __name__ == "__main__":
    main()
