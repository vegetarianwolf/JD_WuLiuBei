"""Small paired relay experiments against the main at the start of this task."""
from __future__ import annotations

import argparse
from pathlib import Path
import platform
import time
from zipfile import ZipFile

from algorithm.experiments import run_optimization_experiments as base

ROOT = base.ROOT
OUTPUT = ROOT / "algorithm/results/relay_trials_20261013/batch1"
BASELINE_COMMIT = "bca2fbfd6f9aca9818005213cf2a03a696ada0f0"
VARIANTS = {
    "baseline": {},
    "checkpoint": {"enable_relay_seed_checkpoint": True},
    "diversity": {"enable_relay_route_diversity": True},
    "risk": {"enable_relay_risk_seed": True},
}
LABELS = {"baseline": "本轮开始时 main", "checkpoint": "保留已完成播种改善",
          "diversity": "接力候选跨机多样性", "risk": "全局风险优先播种"}
SEEDS = {"compatibility": [2026101801], "screen": [2026101811, 2026101812],
         "medium": [2026101821, 2026101822, 2026101823],
         "reference": [2026101821, 2026101822, 2026101823]}
PRIMARY = "fleet200__direct_relay_stations"
SECONDARY = "fleet200__direct_relay"
CASES = {"compatibility": ["fleet200"], "screen": [PRIMARY, SECONDARY],
         "medium": [PRIMARY, SECONDARY], "reference": ["fleet200"]}
BUDGETS = {s: {c: (30 if s in {"medium", "reference"} else 10) for c in cases}
           for s, cases in CASES.items()}
CAPS = {"compatibility": 40, "screen": 160, "medium": 360, "reference": 90}
TOTAL_SECONDS, RESERVED_SECONDS = 720, 30
EXECUTION_NAMES = ("run_optimization_experiments", "run_scenario_comparison", "run_relay_trials")
PROTOCOL = {
    "stage_cases": CASES, "stage_budgets_seconds": BUDGETS,
    "stage_caps_seconds": CAPS, "total_experiment_seconds": TOTAL_SECONDS,
    "reserved_seconds": RESERVED_SECONDS, "compatibility_iterations": 100,
    "tasks": 200, "drones": 8, "max_tasks_per_drone": 25, "capacity": 2,
    "speed_km_per_min": .9, "deadline_multiplier": 1., "relay_count": 4,
    "relay_location_seed": 42, "relay_direct_warmup_fraction": .8,
    "safety_margin_seconds": 1., "primary_case": PRIMARY,
    "K_interpretation": "沿用论文：仅最终配送责任占每机K额度；前半程转运不占，属于原题中继扩展",
    "comparison": "同源码版本边界、同场景、同种子、同初解和同总预算，三级词典序逐种子判胜",
    "screen_gate": "任一有效配对获胜即可扩大；退化完整保留；DIRECT固定100轮兼容必须等价",
    "selection": "仅验证一个候选：依次按主场景胜数、两场景总胜数、主场景逾期数减少总量、主场景次级胜数；完全并列按checkpoint/diversity/risk",
    "default_gate": "主场景三个新种子至少两胜即可默认，披露次场景退化与长预算未知；用户授权探索门槛",
    "continuation": "完成预定三个新种子，不换种子、不重跑择优；未晋级不进行大规模或长预算实验",
    "time_boundary": "CSV读取前至完整核验和解JSON写出后；不含Python进程启动与模块导入",
    "direct_reference": "只作同总预算策略参照；预部署场景起点不同，不能把差值全归因于中继",
    "historical": "单独显示冻结旧四策略，保留其模块预算，不与本批混算改善率",
    "user_choices": ["轻量验证，3种子少量场景", "主要可视化，与先前结果对比",
                     "任一配对获胜即可扩大，保留退化", "三个种子多数胜即可默认并披露局限",
                     "沿用论文最终配送责任K语义"],
}


def _cell(row):
    return tuple(row[k] for k in ("stage", "case_id", "variant", "seed"))


def prepare(output):
    if output.exists() and any(output.iterdir()):
        raise ValueError("新批次目录须为空，禁止覆盖历史记录")
    output.mkdir(parents=True, exist_ok=True)
    for name, commit in (("baseline", BASELINE_COMMIT), ("candidate", None)):
        base._snapshot(output / f"sources/{name}.zip", commit)
    snapshots = {}
    for variant, flags in VARIANTS.items():
        name = "baseline" if variant == "baseline" else "candidate"
        path = f"sources/{name}.zip"
        snapshots[variant] = dict(path=path, sha256=base.sha256(output / path), config=flags,
                                 source_ref=BASELINE_COMMIT if name == "baseline" else "working_tree",
                                 label=LABELS[variant])
    execution = {}
    for name in EXECUTION_NAMES:
        source = ROOT / f"algorithm/experiments/{name}.py"
        path = f"sources/{name}.py"
        (output / path).write_bytes(source.read_bytes())
        execution[name] = dict(path=path, sha256=base.sha256(output / path))
    manifest = dict(kind="relay_trials", schema_version=1, baseline_variant="baseline",
                    baseline_commit=BASELINE_COMMIT, input_path=base.INPUT,
                    input_sha256=base.sha256(ROOT / base.INPUT), source_snapshots=snapshots,
                    execution_sources=execution, runner_sha256=execution[EXECUTION_NAMES[0]]["sha256"],
                    seeds=SEEDS, protocol=PROTOCOL, environment=dict(python=platform.python_version(),
                    platform=platform.platform()), created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    base.write_json(output / "manifest.json", manifest)
    base.write_json(output / "runs.json", [])
    base.write_json(output / "decisions.json", [])
    return manifest


def verify(output, manifest):
    if (manifest.get("protocol") != PROTOCOL or manifest.get("seeds") != SEEDS
            or manifest.get("baseline_commit") != BASELINE_COMMIT
            or manifest.get("kind") != "relay_trials" or manifest.get("schema_version") != 1
            or {v: s["config"] for v, s in manifest["source_snapshots"].items()} != VARIANTS):
        raise ValueError("冻结实验协议改变")
    base.verify_execution_files(manifest)
    for source in [*manifest["source_snapshots"].values(), *manifest["execution_sources"].values()]:
        path = (output / source["path"]).resolve()
        if not path.is_relative_to(output.resolve()) or base.sha256(path) != source["sha256"]:
            raise ValueError("冻结源码指纹改变或路径非法")
    for name in EXECUTION_NAMES:
        if base.sha256(ROOT / f"algorithm/experiments/{name}.py") != manifest["execution_sources"][name]["sha256"]:
            raise ValueError("执行源码改变，禁止混合续跑")
    if base.sha256(ROOT / base.INPUT) != manifest["input_sha256"]:
        raise ValueError("官方输入改变")
    rows = base.read_json(output / "runs.json")
    cells = [_cell(row) for row in rows]
    if len(set(cells)) != len(cells):
        raise ValueError("重复单元")
    registered = set()
    for row in rows:
        stage, case, variant, seed = _cell(row)
        if (stage not in SEEDS or case not in CASES[stage] or variant not in VARIANTS
                or seed not in SEEDS[stage] or row["budget_seconds"] != BUDGETS[stage][case]
                or row["source_sha256"] != manifest["source_snapshots"][variant]["sha256"]):
            raise ValueError("已有记录不属于冻结协议")
        path = (output / row["solution_file"]).resolve()
        if not path.is_relative_to(output.resolve()) or base.sha256(path) != row["solution_sha256"]:
            raise ValueError("已有解指纹改变")
        registered.add(path)
    if any(p.resolve() not in registered for p in (output / "run_solutions").glob("*.json")):
        raise ValueError("存在未登记孤立解，不允许覆盖")


def pairs(rows, stage, variant, case=PRIMARY):
    return base.paired(rows, stage, variant, case)


def wins(matched):
    return sum(base.score(c) < base.score(b) for b, c in matched)


def compatibility_pass(rows, variant):
    matched = pairs(rows, "compatibility", variant, "fleet200")
    return len(matched) == 1 and all(b["iterations"] == c["iterations"] == 100
        and b["decision_fingerprint"] == c["decision_fingerprint"] for b, c in matched)


def screen_pass(rows, variant):
    return compatibility_pass(rows, variant) and all(
        len(pairs(rows, "screen", variant, case)) == 2 for case in CASES["screen"]
    ) and any(wins(pairs(rows, "screen", variant, case)) for case in CASES["screen"])


def choose_candidate(rows):
    eligible = [v for v in VARIANTS if v != "baseline" and screen_pass(rows, v)]
    def rank(v):
        primary = pairs(rows, "screen", v)
        return (wins(primary), sum(wins(pairs(rows, "screen", v, c)) for c in CASES["screen"]),
                sum(b["late_count"] - c["late_count"] for b, c in primary),
                sum(b["late_count"] == c["late_count"] and base.score(c) < base.score(b) for b, c in primary),
                -list(VARIANTS).index(v))
    return max(eligible, key=rank) if eligible else None


def default_pass(rows, variant):
    return all(len(pairs(rows, "medium", variant, c)) == 3 for c in CASES["medium"]) and wins(pairs(rows, "medium", variant)) >= 2


def spent(output, rows):
    notes = output / "execution_notes.json"
    aborted = base.read_json(notes).get("aborted_runs", []) if notes.exists() else []
    return sum(r["end_to_end_seconds"] for r in rows) + sum(r["process_elapsed_seconds"] for r in aborted)


def stage(output, manifest, name, variants):
    verify(output, manifest)
    rows = base.read_json(output / "runs.json")
    cells = {_cell(r) for r in rows}
    missing = sum(BUDGETS[name][case] for seed in SEEDS[name] for case in CASES[name]
                  for v in variants if (name, case, v, seed) not in cells)
    if (sum(r["end_to_end_seconds"] for r in rows if r["stage"] == name) + missing > CAPS[name]
            or spent(output, rows) + missing > TOTAL_SECONDS - RESERVED_SECONDS):
        raise ValueError("预算不足以预留整个阶段")
    aborted_path = output / "execution_notes.json"
    aborted = base.read_json(aborted_path).get("aborted_runs", []) if aborted_path.exists() else []
    for i, seed in enumerate(SEEDS[name]):
        order = variants if i % 2 == 0 else list(reversed(variants))
        for case in CASES[name]:
            for variant in order:
                cell = (name, case, variant, seed)
                if cell in cells:
                    continue
                if any(_cell(r) == cell for r in aborted):
                    raise ValueError("已中止单元不允许重跑择优")
                row = base.invoke(output, manifest, variant, case, seed, name, BUDGETS[name][case],
                                  iterations=100 if name == "compatibility" else None)
                payload = base.read_json(output / row["solution_file"])
                row["relay_statistics"] = payload["metadata"].get("relay", {})
                row["relay_warmup_score"] = payload["metadata"].get("relay_warmup_score")
                row["relay_search_final_score"] = payload["metadata"].get("relay_search_final_score")
                row["order"] = order.index(variant)
                rows.append(row)
                base.save_rows(output, rows)
                cells.add(cell)
    return rows


def run(output):
    manifest = base.read_json(output / "manifest.json")
    stage(output, manifest, "compatibility", list(VARIANTS))
    rows = stage(output, manifest, "screen", list(VARIANTS))
    selected = choose_candidate(rows)
    base.decision(output, "screen", selected_variant=selected,
                  passed={v: screen_pass(rows, v) for v in VARIANTS if v != "baseline"},
                  reason=PROTOCOL["screen_gate"])
    if selected:
        rows = stage(output, manifest, "medium", ["baseline", selected])
        base.decision(output, "medium", selected_variant=selected, passed=default_pass(rows, selected),
                      reason=PROTOCOL["default_gate"])
        rows = stage(output, manifest, "reference", ["baseline"])
    else:
        base.decision(output, "medium", selected_variant=None, passed=False, skipped=True,
                      reason="初筛未入围，不扩大验证")
    base.summarize(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run"))
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    prepare(args.output_dir.resolve()) if args.command == "prepare" else run(args.output_dir.resolve())


if __name__ == "__main__":
    main()
