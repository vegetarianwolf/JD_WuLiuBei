"""Frozen weighted baseline versus bounded task exchange and lazy repair.

This batch preserves the earlier weights/scales and runs exactly one fresh
shared seed, pure DIRECT, with a 240-second end-to-end limit per arm.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

import run_weighted_direct_pair as common
import weighted_direct_objective as weighted

ROOT = common.ROOT
PARENT = ROOT / "algorithm/results/weighted_direct_single_seed_20261014"
OUTPUT = ROOT / "algorithm/results/weighted_method_improvement_20261014"
ORDER = ["weighted_baseline", "weighted_exchange_lazy"]
SEED = 2026101461
read, write, sha, digest = common.read, common.write, common.sha, common.digest


def prepare(output):
    if (output / "manifest.json").exists():
        raise ValueError("Frozen experiment exists; select a new output directory")
    parent = read(PARENT / "manifest.json")
    parent_source = PARENT / parent["variants"]["current_weighted"]["archive"]
    assert sha(parent_source) == parent["variants"]["current_weighted"]["sha256"]
    assert sha(weighted.__file__) == parent["adapter_sha256"]
    assert sha(ROOT / parent["input_path"]) == parent["input_sha256"]
    sys.path.insert(0, str(ROOT / "algorithm/src"))
    from uav_dispatch.alns import ALNSConfig, construct_regret_initial
    from uav_dispatch.io import load_tasks_csv
    from uav_dispatch.model import Problem
    problem = Problem(load_tasks_csv(ROOT / parent["input_path"]), drone_count=8, max_tasks_per_drone=25)
    assert len(problem.tasks) == 200
    initial = construct_regret_initial(problem, candidate_limit=48, home_seed=False, home_bias=False)
    assert digest(initial.routes) == parent["initial_routes_sha256"]
    assert dataclasses.asdict(initial.evaluation.score) == parent["initial_score"]
    base_config = {**parent["config"], "seed": SEED}
    candidate_config = {**base_config, "enable_lazy_repair": True, "enable_terminal_task_exchange": True}
    ALNSConfig(**candidate_config)
    sources = output / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    base_path = sources / "weighted_baseline.zip"
    shutil.copyfile(parent_source, base_path)
    candidate_path = sources / "weighted_exchange_lazy.zip"
    transformations = {}
    production_files = {}
    with ZipFile(candidate_path, "w") as package:
        for path in sorted((ROOT / "algorithm/src/uav_dispatch").glob("*.py")):
            production_files[path.name] = sha(path)
            text = path.read_text()
            if path.name in weighted.EXPECTED_COUNTS:
                text, counts = weighted.transform(text)
                assert counts == weighted.EXPECTED_COUNTS[path.name], (path.name, counts)
                transformations[path.name] = counts
            package.writestr("uav_dispatch/" + path.name, text)
    variants = {
        ORDER[0]: dict(label="当前加权基线", objective="weighted", archive=str(base_path.relative_to(output)),
                       sha256=sha(base_path), config=base_config),
        ORDER[1]: dict(label="整任务互换＋惰性修复", objective="weighted", archive=str(candidate_path.relative_to(output)),
                       sha256=sha(candidate_path), config=candidate_config),
    }
    execution_sources = {}
    for path in (Path(__file__), Path(weighted.__file__), Path(common.__file__)):
        frozen = sources / path.name
        shutil.copyfile(path, frozen)
        execution_sources[path.name] = dict(path=str(frozen.relative_to(output)), sha256=sha(frozen))
    manifest = dict(schema_version=2, seed=SEED, order=ORDER, baseline_variant=ORDER[0], variants=variants,
                    budget_seconds=240, finalization_reserve_seconds=1, weights=parent["weights"], scales=parent["scales"],
                    formula=parent["formula"], initial_score=parent["initial_score"],
                    initial_routes=[list(route) for route in initial.routes], initial_routes_sha256=digest(initial.routes),
                    input_path=parent["input_path"], input_sha256=parent["input_sha256"],
                    problem_path=parent["problem_path"], problem_sha256=parent["problem_sha256"],
                    source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
                    parent_manifest_path=str((PARENT / "manifest.json").relative_to(ROOT)), parent_manifest_sha256=sha(PARENT / "manifest.json"),
                    parent_weighted_source_sha256=sha(parent_source), adapter_changes=transformations,
                    production_files_sha256=production_files, execution_sources=execution_sources,
                    environment=dict(python=sys.version, platform=platform.platform()),
                    protocol=dict(description="完整200任务/8机/K25/容量2/原点/无中继无预部署。共用前批固定加权J、相同初解和新种子2026101461；两个版本各一次240秒，顺序执行。",
                                  time_boundary="读取输入、构造初解、搜索、终端强化、完整核验及解写出均计时；不含Python启动/导入。每臂240秒上限，扣实测构造时间与1秒收尾。候选整任务交换共享既有≤0.1秒终端预算，不加预算。",
                                  trajectory_time_boundary="搜索轨迹加各自构造秒数，对齐到端到端计时；每臂按自己的固定加权J保留真实最优解。"),
                    variant_descriptions={
                        ORDER[0]: "上一批实际运行的加权源码冻结快照；同一0.5/0.25/0.25权重、同一固定尺度、原ALNS及既有末端强化。重新运行本批新种子，不复用旧成绩。",
                        ORDER[1]: "新增原位置整任务取送标签互换，在已有末端预算内有界搜索，逐候选精确评两路线、改善后完整规范核验；同时启用决策等价的deadline/slack惰性修复，只评价实际可被选中的静态短名单。",
                    },
                    implementation_notes=["两方案的加权目标、初解、算子池、退火温度、候选限制、相邻强化及原末端步骤保持。",
                                          "候选包含邻域与修复提速两个组成，正式对比评价整个组合；不会把组合成绩单独归因于某一步。",
                                          "交换整任务保持每条路线符号序列，因此保持载荷曲线、取送先后和任务数；代理排名只选候选，不代替完整时序评分。",
                                          "新研究开关公共默认关闭；不以一个种子推断统计稳定性。"],
                    reproduction_commands=[".venv/bin/python algorithm/experiments/run_weighted_method_pair.py prepare --output-dir algorithm/results/weighted_method_replay_NEW",
                                           ".venv/bin/python algorithm/experiments/run_weighted_method_pair.py run --output-dir algorithm/results/weighted_method_replay_NEW",
                                           ".venv/bin/python algorithm/experiments/audit_weighted_method_pair.py",
                                           ".venv/bin/python algorithm/experiments/render_weighted_method_report.py"])
    write(output / "manifest.json", manifest)
    write(output / "runs.json", [])
    print(json.dumps({"prepared": str(output), "seed": SEED, "order": ORDER, "weights": manifest["weights"],
                      "scales": manifest["scales"]}, ensure_ascii=False))


def worker(request_path):
    request = read(request_path)
    output = Path(request["output"])
    manifest = read(output / "manifest.json")
    from uav_dispatch.alns import ALNSConfig, construct_regret_initial, solve_alns
    from uav_dispatch.cli import result_payload
    from uav_dispatch.io import load_tasks_csv
    from uav_dispatch.model import Problem, Score
    from uav_dispatch.validation import evaluate_solution
    spec = manifest["variants"][request["variant"]]
    started = time.perf_counter()
    problem = Problem(load_tasks_csv(ROOT / manifest["input_path"]), drone_count=8, max_tasks_per_drone=25)
    initial = construct_regret_initial(problem, candidate_limit=48, home_seed=False, home_bias=False)
    assert digest(initial.routes) == manifest["initial_routes_sha256"]
    construction = time.perf_counter() - started
    config = ALNSConfig(**{**spec["config"], "time_limit_seconds": max(.001, 240 - construction - 1)})
    with weighted.objective(Score, manifest["scales"], manifest["weights"]):
        search_started = time.perf_counter()
        result = solve_alns(problem, config=config, initial_routes=initial.routes)
        search_seconds = time.perf_counter() - search_started
        check = evaluate_solution(problem, result.routes, _force_full=True)
        assert check.valid and check.score == result.evaluation.score
        payload = result_payload(problem, dataclasses.replace(result, evaluation=check), source=manifest["input_path"])
        payload["problem"]["objective_order"] = ["weighted_total_score", "late_count", "total_lateness_min", "distance_km"]
        payload["problem"]["experiment_objective"] = dict(kind="weighted", formula=manifest["formula"], weights=manifest["weights"], scales=manifest["scales"])
        solution = output / "run_solutions" / (request["variant"] + ".json")
        write(solution, payload)
    elapsed = time.perf_counter() - started
    score = dataclasses.asdict(check.score)
    contributions = [w * score[k] / scale for w, k, scale in zip(manifest["weights"],
                                                               ("late_count", "total_lateness_min", "distance_km"), manifest["scales"])]
    row = dict(variant=request["variant"], label=spec["label"], objective="weighted", seed=manifest["seed"],
               budget_seconds=240, end_to_end_seconds=elapsed, construction_seconds=construction, search_seconds=search_seconds,
               valid=check.valid, budget_compliant=elapsed <= 240, iterations=result.iterations, **score,
               weighted_score=sum(contributions), weighted_components=contributions, on_time_count=200 - score["late_count"],
               on_time_rate=1 - score["late_count"] / 200, delivered_count=len(check.delivery_times_min),
               initial_routes_sha256=digest(initial.routes), source_sha256=spec["sha256"], config=dataclasses.asdict(config),
               solution_path=str(solution.relative_to(output)), solution_sha256=sha(solution),
               best_trajectory=result.metadata.get("best_trajectory", []),
               terminal_polish=result.metadata.get("terminal_polish", {}),
               operator_statistics=result.metadata.get("operator_statistics", {}),
               performance_counters=result.metadata.get("profiling", {}))
    write(request["response"], row)


def run(output):
    manifest = read(output / "manifest.json")
    for name, spec in manifest["execution_sources"].items():
        assert sha(output / spec["path"]) == spec["sha256"] == sha(ROOT / "algorithm/experiments" / name)
    assert sha(ROOT / manifest["input_path"]) == manifest["input_sha256"]
    rows = read(output / "runs.json")
    assert [r["variant"] for r in rows] == ORDER[:len(rows)]
    for name in ORDER[len(rows):]:
        spec = manifest["variants"][name]
        archive = output / spec["archive"]
        assert sha(archive) == spec["sha256"]
        assert not (output / "run_solutions" / (name + ".json")).exists(), "Never overwrite completed results"
        with tempfile.TemporaryDirectory(prefix="weighted-method-pair-") as temporary:
            temporary = Path(temporary)
            with ZipFile(archive) as package:
                assert all(n.startswith("uav_dispatch/") and n.endswith(".py") and len(Path(n).parts) == 2 for n in package.namelist())
                package.extractall(temporary)
            request = temporary / "request.json"
            write(request, dict(output=str(output), variant=name, response=str(temporary / "response.json")))
            print("START " + name, flush=True)
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker", "--request", str(request)], cwd=ROOT,
                           env={**os.environ, "PYTHONPATH": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}, check=True, timeout=270)
            row = read(temporary / "response.json")
        rows.append(row)
        write(output / "runs.json", rows)
        print(json.dumps({k: row[k] for k in ("variant", "late_count", "total_lateness_min", "distance_km", "weighted_score",
                                              "end_to_end_seconds", "iterations")}, ensure_ascii=False), flush=True)
        if not row["valid"] or not row["budget_compliant"]:
            raise RuntimeError("Invalid or over-budget run retained for diagnosis")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "worker"))
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--request", type=Path)
    args = parser.parse_args()
    if args.command == "worker":
        worker(args.request)
    elif args.command == "prepare":
        prepare(args.output_dir.resolve())
    else:
        run(args.output_dir.resolve())
