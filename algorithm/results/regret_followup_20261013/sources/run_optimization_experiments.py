"""Bounded, sequential A/B experiments using immutable solver snapshots.

This protocol is separate from the frozen manuscript experiments.  Run
``python -m algorithm.experiments.run_optimization_experiments --help``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED


ROOT = Path(__file__).resolve().parents[2]
INPUT = "algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv"
DEFAULT_OUTPUT = ROOT / "algorithm/results/optimization_20261013"
VARIANTS = {
    "baseline": {},
    "fast": {"enable_lazy_repair": True, "enable_route_distinct_regret": False},
    "allocation": {"enable_lazy_repair": True, "enable_route_distinct_regret": True},
}
SEEDS = {"screen": [2026101301, 2026101302],
         "medium": [2026101311, 2026101312, 2026101313],
         "confirm": [2026101321, 2026101322, 2026101323],
         "extensions": [2026101301, 2026101302]}
STAGE_CAPS = {"screen": 480, "medium": 360, "confirm": 2160, "extensions": 240}


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    temp.replace(path)


def value_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT).decode().strip()


def _snapshot(path, commit=None):
    """Use reproducible zip timestamps and names, excluding caches and secrets."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for source in sorted((ROOT / "algorithm/src/uav_dispatch").glob("*.py")):
            data = (subprocess.check_output(["git", "show", f"{commit}:{source.relative_to(ROOT)}"], cwd=ROOT)
                    if commit else source.read_bytes())
            info = ZipInfo("uav_dispatch/" + source.name, (2026, 10, 13, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, data)
    return sha256(path)


def prepare(output, baseline_commit):
    if output.exists() and any(output.iterdir()):
        raise ValueError("实验输出目录必须为空；已有批次用 run 继续，不覆盖源快照")
    output.mkdir(parents=True, exist_ok=True)
    commit = _git("rev-parse", baseline_commit)
    baseline_hash = _snapshot(output / "sources/baseline.zip", commit)
    candidate_hash = _snapshot(output / "sources/candidate.zip")
    execution_sources = {}
    for name, source in (("run_optimization_experiments", Path(__file__)),
                         ("run_scenario_comparison", ROOT / "algorithm/experiments/run_scenario_comparison.py")):
        target = output / "sources" / (name + ".py")
        target.write_bytes(source.read_bytes())
        execution_sources[name] = {"path": str(target.relative_to(output)), "sha256": sha256(target)}
    snapshots = {name: {"path": f"sources/{'baseline' if name == 'baseline' else 'candidate'}.zip",
                        "sha256": baseline_hash if name == "baseline" else candidate_hash,
                        "config": flags} for name, flags in VARIANTS.items()}
    manifest = {
        "schema_version": 1, "kind": "exploratory_optimization", "baseline_commit": commit,
        "baseline_variant": "baseline", "input_path": INPUT,
        "input_sha256": sha256(ROOT / INPUT), "source_snapshots": snapshots, "seeds": SEEDS,
        "runner_sha256": sha256(Path(__file__)),
        "execution_sources": execution_sources,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {"python": platform.python_version(), "platform": platform.platform()},
        "protocol": {
            "total_experiment_seconds": 3600, "stage_caps_seconds": STAGE_CAPS,
            "single": {"tasks": 25, "drones": 1}, "fleet": {"tasks": 200, "drones": 8},
            "max_tasks_per_drone": 25, "capacity": 2, "speed_km_per_min": 0.9,
            "deadline_multiplier": 1.0, "relay_count": 4, "relay_location_seed": 42,
            "relay_direct_warmup_fraction": 0.8, "safety_margin_seconds": 1.0,
            "time_boundary": "CSV读取前至完整核验和完整解JSON写出后；不含Python进程启动和模块导入",
            "comparison": "逐种子三级词典序，完整初解相同；总逾期分钟是项目次级目标",
            "quality_gate": "初筛集群2组均不劣且至少1胜；中测集群至少2胜，两个实例均无逾期数增加",
            "performance_gate": "固定迭代routes/score/搜索决策完全一致；重复计时中位数至少降低10%",
            "promotion": "质量版本确认至少2/3集群获胜且所有原题配对无词典序退化；等价版本所有确认配对无词典序退化",
            "frozen_manuscript_results": "不修改、不重跑、不与本批次混合",
        },
    }
    write_json(output / "manifest.json", manifest)
    write_json(output / "runs.json", [])
    write_json(output / "decisions.json", [])
    return manifest


def verify_execution_files(manifest):
    """A resume must not silently mix different experimental protocols."""
    if sha256(Path(__file__)) != manifest["runner_sha256"]:
        raise ValueError("运行器源码已改变；请恢复归档版本或 prepare 新批次")
    helper = manifest.get("execution_sources", {}).get("run_scenario_comparison", {})
    if sha256(ROOT / "algorithm/experiments/run_scenario_comparison.py") != helper.get("sha256"):
        raise ValueError("场景构造源码已改变；请恢复归档版本或 prepare 新批次")


def worker(request_path):
    # The parent places an extracted, verified snapshot first on PYTHONPATH.
    from dataclasses import fields, replace
    from uav_dispatch.alns import ALNSConfig, construct_regret_initial, solve_alns, solve_relay_staged
    from uav_dispatch.cli import result_payload, _jsonable
    from uav_dispatch.io import load_tasks_csv
    from uav_dispatch.model import Problem
    from uav_dispatch.validation import evaluate_solution
    from algorithm.experiments.run_scenario_comparison import build_scenario_problem

    request = read_json(request_path)
    n, drones = (25, 1) if request["case_id"] == "single25" else (200, 8)
    scenario_id = request.get("scenario", "pure_direct")
    relay = scenario_id in {"direct_relay", "direct_relay_stations"}
    homes = scenario_id in {"direct_stations", "direct_relay_stations"}
    scenario = {"id": scenario_id, "relay": relay, "drone_homes": "stations" if homes else "origin", "warmup": .8 if relay else 0.0}
    started = time.perf_counter()
    tasks = load_tasks_csv(ROOT / INPUT)[:n]
    if scenario_id == "pure_direct":
        problem = Problem(tasks, drone_count=drones, max_tasks_per_drone=25)
    else:
        problem, _ = build_scenario_problem(tasks, scenario, drones=drones, max_tasks=25,
                                             relay_count=4, relay_location_seed=42)
    initial = construct_regret_initial(problem, candidate_limit=48, home_seed=homes, home_bias=homes)
    construction = time.perf_counter() - started
    budget = request.get("budget_seconds")
    remaining = None if budget is None else max(1e-6, budget - construction - 1.0)
    config_args = dict(max_iterations=request.get("iterations", 10_000_000), time_limit_seconds=remaining,
                       seed=request["seed"], candidate_limit=48, relay_enabled=relay,
                       relay_direct_warmup_fraction=.8, enable_home_seed=homes, enable_home_bias=homes,
                       **request["config"])
    known = {field.name for field in fields(ALNSConfig)}
    unknown = set(config_args) - known
    if unknown:
        raise ValueError(f"快照不支持配置: {sorted(unknown)}")
    solver = solve_relay_staged if relay else solve_alns
    search_start = time.perf_counter()
    result = solver(problem, config=ALNSConfig(**config_args), initial_routes=initial.routes)
    search = time.perf_counter() - search_start
    validation_start = time.perf_counter()
    checked = evaluate_solution(problem, result.routes, _force_full=True)
    if checked.score != result.evaluation.score:
        raise ValueError("独立评价与求解器得分不一致")
    validation = time.perf_counter() - validation_start
    serialize_start = time.perf_counter()
    payload = result_payload(problem, replace(result, evaluation=checked), source=INPUT)
    write_json(Path(request["solution_path"]), payload)
    serialization = time.perf_counter() - serialize_start
    elapsed = time.perf_counter() - started
    trajectory = [[point[0], point[1] + construction, *point[2:]]
                  for point in result.metadata["best_trajectory"]]
    row = {key: request[key] for key in ("stage", "variant", "case_id", "seed", "source_sha256")}
    row.update(scenario=scenario_id, task_count=n, drone_count=drones,
               budget_seconds=budget, end_to_end_seconds=elapsed, construction_seconds=construction,
               search_seconds=search, validation_seconds=validation, serialization_seconds=serialization,
               budget_compliant=budget is None or elapsed <= budget, valid=checked.valid,
               violations=list(checked.violations), late_count=checked.score.late_count,
               total_lateness_min=checked.score.total_lateness_min, distance_km=checked.score.distance_km,
               on_time_count=len(checked.delivery_times_min) - checked.score.late_count,
               on_time_rate=(len(checked.delivery_times_min)-checked.score.late_count)/n,
               iterations=result.iterations, best_trajectory=trajectory,
               initial_routes_sha256=value_hash(initial.routes), solution_sha256=sha256(request["solution_path"]),
               config=config_args, profiling=_jsonable(result.metadata.get("profiling", {})))
    row["decision_fingerprint"] = value_hash({
        "routes": result.routes, "score": _jsonable(checked.score), "iterations": result.iterations,
        "trajectory": [[p[0], *p[2:]] for p in trajectory],
        "operator_statistics": _jsonable(result.metadata.get("operator_statistics", {})),
        "operator_weights": _jsonable(result.metadata.get("operator_weights", {})),
        "accepted_solutions": result.metadata.get("accepted_solutions"),
    })
    write_json(request["response_path"], row)


def invoke(output, manifest, variant, case, seed, stage, budget, *, iterations=None):
    verify_execution_files(manifest)
    source = manifest["source_snapshots"][variant]
    archive = output / source["path"]
    if sha256(archive) != source["sha256"] or sha256(ROOT / manifest["input_path"]) != manifest["input_sha256"]:
        raise ValueError("输入或源码快照指纹变化")
    run_id = f"{stage}__{case}__{variant}__{seed}"
    scenario = case.split("__", 1)[1] if "__" in case else "pure_direct"
    solution_file = f"run_solutions/{run_id}.json"
    with tempfile.TemporaryDirectory(prefix="uav-optimization-") as temp:
        temp = Path(temp)
        with ZipFile(archive) as package:
            for entry in package.namelist():
                if not entry.startswith("uav_dispatch/") or ".." in Path(entry).parts:
                    raise ValueError("非法快照路径")
            package.extractall(temp / "src")
        request = dict(stage=stage, variant=variant, case_id=case, scenario=scenario, seed=seed,
                       source_sha256=source["sha256"], config=source["config"], budget_seconds=budget,
                       solution_path=str(output / solution_file), response_path=str(temp / "response.json"))
        if iterations is not None:
            request["iterations"] = iterations
        write_json(temp / "request.json", request)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(temp / "src"), str(ROOT))),
               "PYTHONDONTWRITEBYTECODE": "1"}
        process_start = time.perf_counter()
        try:
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", str(temp / "request.json")],
                           env=env, cwd=ROOT, check=True, timeout=(budget or 60) + 60)
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError) as error:
            notes_path = output / "execution_notes.json"
            notes = read_json(notes_path) if notes_path.exists() else {}
            notes.setdefault("aborted_runs", []).append(dict(
                stage=stage, case_id=case, variant=variant, seed=seed,
                status="timeout_no_result" if isinstance(error, subprocess.TimeoutExpired) else "error_no_result",
                process_elapsed_seconds=time.perf_counter() - process_start,
                reason=str(error), source_sha256=source["sha256"]))
            write_json(notes_path, notes)
            raise
        row = read_json(temp / "response.json")
    row.update(run_id=run_id, solution_file=solution_file)
    print(f"{run_id}: ({row['late_count']}, {row['total_lateness_min']:.3f}, {row['distance_km']:.3f}) "
          f"{row['end_to_end_seconds']:.2f}s valid={row['valid']} budget={row['budget_compliant']}", flush=True)
    return row


def score(row):
    return tuple(row[key] for key in ("late_count", "total_lateness_min", "distance_km"))


def paired(rows, stage, variant, case):
    selected = [r for r in rows if r["stage"] == stage and r["case_id"] == case
                and r["variant"] in {"baseline", variant}]
    cells = [(r["variant"], r["seed"]) for r in selected]
    if len(cells) != len(set(cells)):
        raise ValueError("重复实验单元格，不能覆盖或择优配对")
    selected = [r for r in selected if r["valid"] and r["budget_compliant"]]
    baseline = {r["seed"]: r for r in selected if r["variant"] == "baseline"}
    candidate = {r["seed"]: r for r in selected if r["variant"] == variant}
    pairs = [(baseline[s], candidate[s]) for s in sorted(baseline.keys() & candidate.keys())]
    for b, c in pairs:
        for key in ("initial_routes_sha256", "budget_seconds", "scenario", "task_count", "drone_count"):
            if key not in b or key not in c or b[key] != c[key]:
                raise ValueError(f"配对条件不一致: {key}")
    return pairs


def confirmation_regressed(rows, variant):
    return any(score(c) > score(b) for case in ("single25", "fleet200")
               for b, c in paired(rows, "confirm", variant, case))


def quality_gate(rows, stage, variant):
    fleet = paired(rows, stage, variant, "fleet200")
    single = paired(rows, stage, variant, "single25")
    if not complete_pairset(rows, stage, variant):
        return False
    wins = sum(score(c) < score(b) for b, c in fleet)
    if stage == "screen":
        return wins >= 1 and all(score(c) <= score(b) for b, c in fleet)
    return wins >= 2 and all(c["late_count"] <= b["late_count"] for b, c in fleet + single)


def complete_pairset(rows, stage, variant):
    return all({b["seed"] for b, _ in paired(rows, stage, variant, case)} == set(SEEDS[stage])
               for case in ("single25", "fleet200"))


def save_rows(output, rows):
    write_json(output / "runs.json", rows)
    fields = ["run_id", "stage", "variant", "case_id", "scenario", "seed", "task_count", "drone_count",
              "budget_seconds", "end_to_end_seconds", "construction_seconds", "search_seconds", "validation_seconds",
              "serialization_seconds", "valid", "budget_compliant", "late_count", "on_time_count", "on_time_rate",
              "total_lateness_min", "distance_km", "iterations", "initial_routes_sha256", "source_sha256",
              "solution_file", "solution_sha256"]
    with (output / "runs.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def stage(output, manifest, name, variants):
    verify_execution_files(manifest)
    rows = read_json(output / "runs.json")
    cases = (["fleet200__" + item for item in ("direct_relay", "direct_stations", "direct_relay_stations")]
             if name == "extensions" else ["single25", "fleet200"])
    for seed_index, seed in enumerate(SEEDS[name]):
        order = list(variants) if seed_index % 2 == 0 else list(reversed(variants))
        for case in cases:
            for variant in order:
                if name == "confirm" and any(confirmation_regressed(rows, v) for v in variants if v != "baseline"):
                    notes_path = output / "execution_notes.json"
                    notes = read_json(notes_path) if notes_path.exists() else {}
                    notes.update(early_stop=True, reason="已有确认配对词典序退化，所有配对不退化门槛已无法满足",
                                 policy="停止剩余长测；保留全部已完成记录，不换种子、不重跑择优")
                    write_json(notes_path, notes)
                    return rows
                notes_path = output / "execution_notes.json"
                aborted = read_json(notes_path).get("aborted_runs", []) if notes_path.exists() else []
                if any(r.get("stage") == name and r.get("case_id") == case
                       and r.get("variant") == variant and r.get("seed") == seed for r in aborted):
                    raise RuntimeError("该实验已中止并保留记录，禁止原单元格重跑择优")
                if any(r["stage"] == name and r["case_id"] == case and r["variant"] == variant and r["seed"] == seed for r in rows):
                    continue
                budget = {"screen": 5 if case == "single25" else 10, "medium": 30,
                          "confirm": 120 if case == "single25" else 240, "extensions": 20}[name]
                if sum(r["end_to_end_seconds"] for r in rows if r["stage"] == name) + budget > STAGE_CAPS[name]:
                    raise RuntimeError(f"{name} 累计实验预算不足，停止启动新运行")
                equivalence_seconds = (read_json(output / "equivalence.json").get("elapsed_seconds", 0)
                                       if (output / "equivalence.json").exists() else 0)
                total = sum(r["end_to_end_seconds"] for r in rows) + equivalence_seconds
                if total + budget > 3600:
                    raise RuntimeError("累计实验预算不足，停止启动新运行")
                row = invoke(output, manifest, variant, case, seed, name, budget)
                row["order"] = order.index(variant)
                rows.append(row)
                save_rows(output, rows)
    return rows


def equivalence(output, manifest):
    verify_execution_files(manifest)
    path = output / "equivalence.json"
    if path.exists():
        return read_json(path)
    notes_path = output / "execution_notes.json"
    aborted = read_json(notes_path).get("aborted_runs", []) if notes_path.exists() else []
    if (any(r.get("stage") == "equivalence" for r in aborted)
            or any((output / "run_solutions").glob("equivalence__*.json"))):
        raise RuntimeError("等价测试批次未完整结束；保留已完成和中止证据，禁止重跑覆盖")
    observations = []
    for case in ("single25", "fleet200"):
        for repeat, seed in enumerate((2026101301, 2026101302, 2026101303)):
            pair = {}
            for variant in (("baseline", "fast") if repeat % 2 == 0 else ("fast", "baseline")):
                pair[variant] = invoke(output, manifest, variant, case, seed, "equivalence", None, iterations=200)
            observations.append({"case_id": case, "seed": seed,
                "equivalent": pair["baseline"]["decision_fingerprint"] == pair["fast"]["decision_fingerprint"],
                "baseline_seconds": pair["baseline"]["end_to_end_seconds"],
                "fast_seconds": pair["fast"]["end_to_end_seconds"],
                "baseline": pair["baseline"], "fast": pair["fast"]})
            if not observations[-1]["equivalent"]:
                break
    improvement = {case: 1 - statistics.median(r["fast_seconds"] for r in observations if r["case_id"] == case)
                         / statistics.median(r["baseline_seconds"] for r in observations if r["case_id"] == case)
                   for case in ("single25", "fleet200")}
    result = {"observations": observations, "median_runtime_reduction": improvement,
              "all_equivalent": len(observations) == 6 and all(r["equivalent"] for r in observations),
              "elapsed_seconds": sum(r["baseline_seconds"] + r["fast_seconds"] for r in observations)}
    result["passed"] = result["all_equivalent"] and improvement["fleet200"] >= .1
    write_json(path, result)
    return result


def decision(output, stage_name, **details):
    path = output / "decisions.json"
    decisions = read_json(path)
    item = {"stage": stage_name, **details}
    decisions = [d for d in decisions if d["stage"] != stage_name] + [item]
    write_json(path, decisions)
    print(json.dumps(item, ensure_ascii=False), flush=True)


def summarize(output):
    rows = read_json(output / "runs.json")
    groups = {}
    for row in rows:
        key = (row["stage"], row["case_id"], row["variant"], row["budget_seconds"])
        groups.setdefault(key, []).append(row)
    summary = []
    for (stage_name, case, variant, budget), group in groups.items():
        valid = [r for r in group if r["valid"] and r["budget_compliant"]]
        variants = {r["variant"] for r in rows if r["stage"] == stage_name and r["case_id"] == case}
        paired_seeds = None
        for other in variants - {"baseline"}:
            seeds = {b["seed"] for b, _ in paired(rows, stage_name, other, case)}
            paired_seeds = seeds if paired_seeds is None else paired_seeds & seeds
        comparison = [r for r in valid if r["seed"] in (paired_seeds or set())]
        metrics = {}
        for metric in ("late_count", "on_time_count", "on_time_rate", "total_lateness_min", "distance_km", "end_to_end_seconds"):
            values = [r[metric] for r in comparison]
            metrics[metric] = {"mean": statistics.mean(values) if values else None,
                               "sample_sd": statistics.stdev(values) if len(values) > 1 else None,
                               "min": min(values) if values else None, "max": max(values) if values else None}
        pairs = paired(rows, stage_name, variant, case) if variant != "baseline" else []
        summary.append(dict(stage=stage_name, case_id=case, variant=variant, budget_seconds=budget,
                            n=len(comparison), raw_n=len(group), unpaired=len(valid)-len(comparison),
                            failed=len(group)-len(valid), metrics=metrics,
                            paired_wins=sum(score(c) < score(b) for b,c in pairs),
                            paired_ties=sum(score(c) == score(b) for b,c in pairs),
                            paired_losses=sum(score(c) > score(b) for b,c in pairs)))
    eq = read_json(output / "equivalence.json") if (output / "equivalence.json").exists() else {}
    result = {"groups": summary, "experiment_seconds": sum(r["end_to_end_seconds"] for r in rows) + eq.get("elapsed_seconds", 0),
              "note": "样本SD为ddof=1，非置信区间；逐种子判胜，不按均值三元组排序"}
    write_json(output / "summary.json", result)
    return result


def run(output):
    manifest = read_json(output / "manifest.json")
    eq = equivalence(output, manifest)
    rows = stage(output, manifest, "screen", list(VARIANTS))
    allocation_pass = eq["all_equivalent"] and quality_gate(rows, "screen", "allocation")
    candidate = "allocation" if allocation_pass else "fast" if eq["passed"] else None
    decision(output, "screen", selected_variant=candidate, equivalent_speed_passed=eq["passed"],
             allocation_passed=allocation_pass,
             reason="按预定门槛选择唯一候选；不因未通过而更换种子")
    if candidate:
        rows = stage(output, manifest, "medium", ["baseline", candidate])
        medium_pass = quality_gate(rows, "medium", candidate)
        if candidate == "fast":
            pairs = paired(rows, "medium", candidate, "fleet200") + paired(rows, "medium", candidate, "single25")
            medium_pass = eq["passed"] and complete_pairset(rows, "medium", candidate) and all(c["late_count"] <= b["late_count"] for b,c in pairs)
        decision(output, "medium", selected_variant=candidate, passed=medium_pass,
                 reason="质量版需集群至少2胜且两个实例无逾期数增加；等价提速版需无逾期数增加")
        if medium_pass:
            rows = stage(output, manifest, "confirm", ["baseline", candidate])
            pairs = paired(rows, "confirm", candidate, "fleet200") + paired(rows, "confirm", candidate, "single25")
            no_regression = complete_pairset(rows, "confirm", candidate) and all(score(c) <= score(b) for b,c in pairs)
            promote = no_regression and (candidate == "fast" or quality_gate(rows, "confirm", candidate))
            decision(output, "confirm", selected_variant=candidate, passed=promote,
                     early_stopped=confirmation_regressed(rows, candidate),
                     completed_pairs=len(pairs), planned_pairs=6,
                     reason="默认晋级要求所有原题配对均无词典序退化；质量版还需集群至少2胜")
        else:
            decision(output, "confirm", passed=False, skipped=True, reason="中测未晋级，停止长测")
    else:
        decision(output, "medium", passed=False, skipped=True, reason="初筛无入围者")
        decision(output, "confirm", passed=False, skipped=True, reason="初筛无入围者，停止长测")
    # Correctness compatibility has a separate, bounded budget even when quality fails.
    extension_variant = candidate or "fast"
    stage(output, manifest, "extensions", ["baseline", extension_variant])
    decision(output, "extensions", selected_variant=extension_variant,
             reason="仅作扩展兼容对照，不替代原题晋级证据")
    summarize(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", choices=("prepare", "run", "equivalence", "screen", "medium", "confirm", "extensions"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--baseline-commit", default="88f232ca66e2ed74ebeb90c4141654413c0714e3")
    parser.add_argument("--variants", nargs="+", choices=tuple(VARIANTS), default=list(VARIANTS))
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        worker(args.worker)
        return
    output = args.output_dir.resolve()
    if args.action == "prepare":
        prepare(output, args.baseline_commit)
    elif args.action == "run":
        run(output)
    elif args.action == "equivalence":
        equivalence(output, read_json(output / "manifest.json"))
    elif args.action:
        stage(output, read_json(output / "manifest.json"), args.action, args.variants)
    else:
        parser.error("指定 prepare 或 run")


if __name__ == "__main__":
    main()
