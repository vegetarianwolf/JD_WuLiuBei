"""Exactly two sequential, independently timed DIRECT runs of current main."""
from __future__ import annotations

import argparse
from contextlib import nullcontext
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile

import weighted_direct_objective as weighted

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "algorithm/results/weighted_direct_single_seed_20261014"
INPUT = ROOT / "algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv"
SEED = 2026101451
ORDER = ["current_lex", "current_weighted"]


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def prepare(output):
    if (output / "manifest.json").exists():
        raise ValueError("Frozen batch already exists; use a new output directory")
    sys.path.insert(0, str(ROOT / "algorithm/src"))
    from uav_dispatch.io import load_tasks_csv
    from uav_dispatch.model import Problem
    from uav_dispatch.alns import ALNSConfig, construct_regret_initial
    tasks = load_tasks_csv(INPUT)
    assert len(tasks) == 200
    problem = Problem(tasks, drone_count=8, max_tasks_per_drone=25)
    config = ALNSConfig(max_iterations=10_000_000, seed=SEED, relay_enabled=False,
                        enable_home_seed=False, enable_home_bias=False)
    initial = construct_regret_initial(problem, candidate_limit=config.candidate_limit,
                                      home_seed=False, home_bias=False)
    initial_score = dataclasses.asdict(initial.evaluation.score)
    scales = [200, initial_score["total_lateness_min"], initial_score["distance_km"]]
    sources = output / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    variants = {}
    adaptations = {}
    for name in ORDER:
        archive = sources / (name + ".zip")
        with ZipFile(archive, "w") as package:
            for path in sorted((ROOT / "algorithm/src/uav_dispatch").glob("*.py")):
                source = path.read_text()
                if name == "current_weighted" and path.name in weighted.EXPECTED_COUNTS:
                    source, counts = weighted.transform(source)
                    assert counts == weighted.EXPECTED_COUNTS[path.name], (path.name, counts)
                    adaptations[path.name] = counts
                package.writestr("uav_dispatch/" + path.name, source)
        variants[name] = dict(label="当前 main · " + ("词典序" if name == "current_lex" else "加权总分"),
                              objective="lexicographic" if name == "current_lex" else "weighted",
                              archive=str(archive.relative_to(output)), sha256=sha(archive))
    manifest = dict(schema_version=1, access_date="2026-10-14", seed=SEED, order=ORDER,
                    budget_seconds=240, finalization_reserve_seconds=1, weights=[0.5, 0.25, 0.25], scales=scales,
                    formula="J = 0.5*late_count/200 + 0.25*total_lateness_min/L0 + 0.25*distance_km/D0",
                    initial_score=initial_score, initial_routes_sha256=digest(initial.routes),
                    initial_routes=[list(route) for route in initial.routes], variants=variants,
                    adapter_changes=adaptations, input_path=str(INPUT.relative_to(ROOT)), input_sha256=sha(INPUT),
                    problem_path="命题1-低空经济场景下的物流无人机调度算法.docx",
                    problem_sha256=sha(ROOT / "命题1-低空经济场景下的物流无人机调度算法.docx"),
                    source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
                    runner_sha256=sha(__file__), adapter_sha256=sha(weighted.__file__),
                    config=dataclasses.asdict(config),
                    environment=dict(python=sys.version, platform=platform.platform()),
                    protocol="官方完整200任务；8机；容量2；每机K25；0.9km/min；全原点；无中继、无预部署；开放路线。仅当前main两方案各一次，顺序执行，同种子同初解。每次240秒端到端上限，包含输入、初解构造、搜索、完整核验、解写出；不含Python导入，预留1秒收尾。不重跑择优。",
                    interpretation="按时数/率、航程和求解耗时为原题直接考核指标；总逾期分钟为项目次级目标。权重为探索性工程设定，非官方评分。更换搜索目标及其排序/退火增量，worst_lex随评分变为加权损失；保留算子池和deadline/slack/risk/assignment_destroy规则、温度参数与默认末端优化；单种子不据此更换公共默认。")
    write(output / "manifest.json", manifest)
    write(output / "runs.json", [])
    print(json.dumps({"prepared": str(output), "seed": SEED, "initial_score": initial_score,
                      "scales": scales, "adapter_changes": adaptations}, ensure_ascii=False))


def worker(request_path):
    request = read(request_path)
    output = Path(request["output"])
    manifest = read(output / "manifest.json")
    from uav_dispatch.alns import ALNSConfig, construct_regret_initial, solve_alns
    from uav_dispatch.cli import result_payload
    from uav_dispatch.io import load_tasks_csv
    from uav_dispatch.model import Problem, Score
    from uav_dispatch.validation import evaluate_solution
    name = request["variant"]
    spec = manifest["variants"][name]
    started = time.perf_counter()
    problem = Problem(load_tasks_csv(INPUT), drone_count=8, max_tasks_per_drone=25)
    initial = construct_regret_initial(problem, candidate_limit=manifest["config"]["candidate_limit"],
                                      home_seed=False, home_bias=False)
    assert digest(initial.routes) == manifest["initial_routes_sha256"]
    construction = time.perf_counter() - started
    values = {**manifest["config"], "time_limit_seconds": max(0.001, 240 - construction - 1)}
    config = ALNSConfig(**values)
    context = weighted.objective(Score, manifest["scales"], manifest["weights"]) if spec["objective"] == "weighted" else nullcontext()
    with context:
        search_started = time.perf_counter()
        result = solve_alns(problem, config=config, initial_routes=initial.routes)
        search_seconds = time.perf_counter() - search_started
        check = evaluate_solution(problem, result.routes, _force_full=True)
        assert check.valid and check.score == result.evaluation.score
        payload = result_payload(problem, dataclasses.replace(result, evaluation=check), source=manifest["input_path"])
        payload["problem"]["objective_order"] = (["weighted_total_score", "late_count", "total_lateness_min", "distance_km"]
                                                  if spec["objective"] == "weighted" else ["late_count", "total_lateness_min", "distance_km"])
        payload["problem"]["experiment_objective"] = dict(kind=spec["objective"], formula=manifest["formula"],
                                                           weights=manifest["weights"], scales=manifest["scales"])
        solution = output / "run_solutions" / (name + ".json")
        write(solution, payload)
    elapsed = time.perf_counter() - started
    score = dataclasses.asdict(check.score)
    components = [w * score[field] / scale for w, scale, field in zip(manifest["weights"], manifest["scales"],
                                                                   ("late_count", "total_lateness_min", "distance_km"))]
    row = dict(variant=name, label=spec["label"], objective=spec["objective"], seed=manifest["seed"],
               budget_seconds=240, end_to_end_seconds=elapsed, construction_seconds=construction, search_seconds=search_seconds,
               valid=check.valid, budget_compliant=elapsed <= 240, iterations=result.iterations,
               **score, weighted_score=sum(components), weighted_components=components,
               on_time_rate=1 - score["late_count"] / 200, on_time_count=200 - score["late_count"],
               delivered_count=len(check.delivery_times_min), initial_routes_sha256=digest(initial.routes),
               source_sha256=spec["sha256"], solution_path=str(solution.relative_to(output)), solution_sha256=sha(solution),
               config=dataclasses.asdict(config), best_trajectory=result.metadata.get("best_trajectory", []))
    write(request["response"], row)


def run(output):
    manifest = read(output / "manifest.json")
    assert sha(__file__) == manifest["runner_sha256"] and sha(weighted.__file__) == manifest["adapter_sha256"]
    assert sha(INPUT) == manifest["input_sha256"]
    rows = read(output / "runs.json")
    assert [r["variant"] for r in rows] == ORDER[:len(rows)]
    for name in ORDER[len(rows):]:
        spec = manifest["variants"][name]
        archive = output / spec["archive"]
        assert sha(archive) == spec["sha256"]
        assert not (output / "run_solutions" / (name + ".json")).exists(), "Never overwrite a completed run"
        with tempfile.TemporaryDirectory(prefix="weighted-direct-pair-") as temporary:
            temporary = Path(temporary)
            with ZipFile(archive) as package:
                assert all(n.startswith("uav_dispatch/") and ".." not in Path(n).parts for n in package.namelist())
                package.extractall(temporary)
            request = temporary / "request.json"
            write(request, dict(output=str(output), variant=name, response=str(temporary / "response.json")))
            print("START " + name, flush=True)
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker", "--request", str(request)], cwd=ROOT,
                           env={**os.environ, "PYTHONPATH": str(temporary), "PYTHONDONTWRITEBYTECODE": "1"}, check=True, timeout=270)
            row = read(temporary / "response.json")
        rows.append(row)
        write(output / "runs.json", rows)
        print(json.dumps({k: row[k] for k in ("variant", "late_count", "total_lateness_min", "distance_km",
                                              "weighted_score", "end_to_end_seconds", "iterations", "valid")}, ensure_ascii=False), flush=True)
        if not row["valid"] or not row["budget_compliant"]:
            raise RuntimeError("Invalid or over-budget result retained for diagnosis")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "worker"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--request", type=Path)
    args = parser.parse_args()
    if args.command == "worker":
        worker(args.request)
    elif args.command == "prepare":
        prepare(args.output_dir.resolve())
    else:
        run(args.output_dir.resolve())
