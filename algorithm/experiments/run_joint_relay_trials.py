"""Whole-solver joint-relay comparison, frozen before small paired trials."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

from algorithm.experiments import run_optimization_experiments as base

ROOT = base.ROOT
OUTPUT = ROOT / "algorithm/results/joint_relay_trials_20261014/batch1"
BASELINE_COMMIT = "ac625e5a38ec2b92f239df2d5ca1017cbeb3ad9b"
VARIANTS = {"baseline": {"solver": "alns"}, "joint": {"solver": "joint"}}
LABELS = {"baseline": "当前 main 分阶段 ALNS", "joint": "全程联合接力 VNS"}
PRIMARY = "fleet200__direct_relay_stations"
SECONDARY = "fleet200__direct_relay"
SMALL = "small8__direct_relay"
SEEDS = {"smoke": [2026101901, 2026101902], "screen": [2026101911, 2026101912],
         "medium": [2026101921, 2026101922, 2026101923]}
CASES = {"smoke": [SMALL], "screen": [PRIMARY, SECONDARY], "medium": [PRIMARY, SECONDARY]}
BUDGETS = {s: {c: {"smoke": 5, "screen": 10, "medium": 30}[s] for c in cases}
           for s, cases in CASES.items()}
CAPS = {"smoke": 20, "screen": 80, "medium": 360}
TOTAL_SECONDS, RESERVED_SECONDS = 540, 30
EXECUTION_NAMES = ("run_joint_relay_trials", "run_scenario_comparison", "run_optimization_experiments")
PROTOCOL = {
    "stage_cases": CASES, "stage_budgets_seconds": BUDGETS, "stage_caps_seconds": CAPS,
    "total_experiment_seconds": TOTAL_SECONDS, "reserved_seconds": RESERVED_SECONDS,
    "smoke_iterations": 80, "primary_case": PRIMARY,
    "fleet": {"tasks": 200, "drones": 8, "K": 25, "stations": 4},
    "small": {"tasks": 8, "drones": 2, "K": 4, "stations": 2},
    "capacity": 2, "speed_km_per_min": .9, "relay_location_seed": 42,
    "safety_margin_seconds": 1., "objective": "逾期任务数→总逾期分钟→航程，严格三级词典序",
    "K_interpretation": "延续论文扩展：只统计最终交付责任，前半程转运不占K；存储型异步交接",
    "baseline_solver": "solve_relay_staged, DIRECT预热80%，当前main默认开关",
    "candidate_solver": "独立solve_joint_relay，全程联合搜索，无ALNS搜索和DIRECT预热阶段",
    "pairing": "同官方实例/起点/站点/种子/初解/端到端总预算，仅求解流程不同",
    "screen_gate": "全部预定初筛配对合法且预算内，任一配对获胜即可进入验证",
    "default_gate": "主场景三个新种子至少两胜可设默认，全部验证配对需完成并披露退化",
    "continuation": "三个新种子预先固定，即使退化也完成；没有初筛胜利即停止扩大",
    "time_boundary": "CSV读取前至完整校验与完整解JSON写出后，不含进程启动/模块导入",
    "history": "前一批中继微调及正式四策略独立展示，不混合跨批改善率",
    "user_choices": ["更换整套求解器，联合优化直送、中继、分机和顺序", "沿用三级目标以隔离算法变化",
                     "延续10秒初筛/30秒三种子轻量验证", "任一配对胜即可扩大", "多数胜才默认"],
}


def case_spec(case):
    if case not in {PRIMARY, SECONDARY, SMALL}:
        raise ValueError("未知固定实验场景")
    return {**PROTOCOL["small" if case == SMALL else "fleet"],
            "scenario": case.split("__", 1)[1]}


def requested_config(variant, stage, seed, remaining, scenario):
    result = dict(max_iterations=80 if stage == "smoke" else 10_000_000,
                  seed=seed, time_limit_seconds=remaining, candidate_limit=48 if variant == "baseline" else 12)
    if variant == "baseline":
        result.update(relay_enabled=True, relay_direct_warmup_fraction=.8,
                      enable_home_seed=scenario == "direct_relay_stations",
                      enable_home_bias=scenario == "direct_relay_stations")
    elif variant != "joint":
        raise ValueError("未知求解器")
    return result


def _snapshot(path, commit=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    names = (subprocess.check_output(["git", "ls-tree", "-r", "--name-only", commit, "--",
                                      "algorithm/src/uav_dispatch"], cwd=ROOT).decode().splitlines()
             if commit else [p.relative_to(ROOT).as_posix() for p in (ROOT / "algorithm/src/uav_dispatch").glob("*.py")])
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name in sorted(n for n in names if n.endswith(".py")):
            data = subprocess.check_output(["git", "show", f"{commit}:{name}"], cwd=ROOT) if commit else (ROOT / name).read_bytes()
            info = ZipInfo("uav_dispatch/" + Path(name).name, (2026, 10, 14, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, data)


def prepare(output):
    if output.exists() and any(output.iterdir()):
        raise ValueError("新批次目录必须为空")
    output.mkdir(parents=True, exist_ok=True)
    snapshots = {}
    for variant, commit in (("baseline", BASELINE_COMMIT), ("joint", None)):
        path = f"sources/{variant}.zip"
        _snapshot(output / path, commit)
        snapshots[variant] = dict(path=path, sha256=base.sha256(output / path), config=VARIANTS[variant],
                                 source_ref=commit or "working_tree", label=LABELS[variant])
    execution = {}
    for name in EXECUTION_NAMES:
        path = f"sources/{name}.py"
        (output / path).write_bytes((ROOT / f"algorithm/experiments/{name}.py").read_bytes())
        execution[name] = dict(path=path, sha256=base.sha256(output / path))
    manifest = dict(kind="joint_relay_trials", schema_version=1, baseline_variant="baseline",
                    baseline_commit=BASELINE_COMMIT, input_path=base.INPUT,
                    input_sha256=base.sha256(ROOT / base.INPUT), source_snapshots=snapshots,
                    execution_sources=execution, seeds=SEEDS, protocol=PROTOCOL,
                    created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    environment=dict(python=platform.python_version(), platform=platform.platform()))
    for name, value in (("manifest", manifest), ("runs", []), ("decisions", [])):
        base.write_json(output / f"{name}.json", value)
    return manifest


def _cell(row):
    return tuple(row[k] for k in ("stage", "case_id", "variant", "seed"))


def verify(output, manifest):
    if (manifest.get("kind") != "joint_relay_trials" or manifest.get("schema_version") != 1
            or manifest.get("protocol") != PROTOCOL or manifest.get("seeds") != SEEDS
            or manifest.get("baseline_commit") != BASELINE_COMMIT
            or {v: s["config"] for v, s in manifest["source_snapshots"].items()} != VARIANTS):
        raise ValueError("冻结协议改变")
    if base.sha256(ROOT / base.INPUT) != manifest["input_sha256"]:
        raise ValueError("官方输入改变")
    for source in [*manifest["source_snapshots"].values(), *manifest["execution_sources"].values()]:
        path = (output / source["path"]).resolve()
        if not path.is_relative_to(output.resolve()) or base.sha256(path) != source["sha256"]:
            raise ValueError("归档源码改变或路径非法")
    for name in EXECUTION_NAMES:
        if base.sha256(ROOT / f"algorithm/experiments/{name}.py") != manifest["execution_sources"][name]["sha256"]:
            raise ValueError("执行源码改变，禁止混合续跑")
    rows = base.read_json(output / "runs.json")
    if len({_cell(r) for r in rows}) != len(rows):
        raise ValueError("重复实验单元")
    referenced = set()
    for row in rows:
        stage, case, variant, seed = _cell(row)
        if (stage not in SEEDS or case not in CASES[stage] or variant not in VARIANTS
                or seed not in SEEDS[stage] or row["budget_seconds"] != BUDGETS[stage][case]
                or row["source_sha256"] != manifest["source_snapshots"][variant]["sha256"]):
            raise ValueError("已有记录不属于协议")
        path = (output / row["solution_file"]).resolve()
        if not path.is_relative_to(output.resolve()) or base.sha256(path) != row["solution_sha256"]:
            raise ValueError("已有解改变")
        referenced.add(path)
    if any(p.resolve() not in referenced for p in (output / "run_solutions").glob("*.json")):
        raise ValueError("存在未登记完整解，禁止覆盖")


def worker(path):
    from uav_dispatch.alns import ALNSConfig, construct_regret_initial, solve_relay_staged
    from uav_dispatch.cli import result_payload, _jsonable
    from uav_dispatch.io import load_tasks_csv
    from uav_dispatch.validation import evaluate_solution
    from algorithm.experiments.run_scenario_comparison import build_scenario_problem
    request = base.read_json(path)
    spec = case_spec(request["case_id"])
    started = time.perf_counter()
    tasks = load_tasks_csv(ROOT / base.INPUT)[:spec["tasks"]]
    homes = spec["scenario"] == "direct_relay_stations"
    problem, _ = build_scenario_problem(tasks, dict(id=spec["scenario"], relay=True,
             drone_homes="stations" if homes else "origin", warmup=.8), drones=spec["drones"],
             max_tasks=spec["K"], relay_count=spec["stations"], relay_location_seed=42)
    initial = construct_regret_initial(problem, candidate_limit=48, home_seed=homes, home_bias=homes)
    construction = time.perf_counter() - started
    remaining = max(1e-6, request["budget_seconds"] - construction - 1.)
    args = requested_config(request["variant"], request["stage"], request["seed"], remaining, spec["scenario"])
    if request["variant"] == "joint":
        from uav_dispatch.joint_relay import JointRelayConfig, solve_joint_relay
        config, solver = JointRelayConfig(**args), solve_joint_relay
    else:
        config, solver = ALNSConfig(**args), solve_relay_staged
    search_started = time.perf_counter()
    result = solver(problem, config=config, initial_routes=initial.routes)
    search_seconds = time.perf_counter() - search_started
    validation_started = time.perf_counter()
    checked = evaluate_solution(problem, result.routes, _force_full=True)
    if not checked.valid or checked.score != result.evaluation.score:
        raise ValueError("完整核验失败或得分不一致")
    validation_seconds = time.perf_counter() - validation_started
    serialize_started = time.perf_counter()
    payload = result_payload(problem, replace(result, evaluation=checked), source=base.INPUT)
    base.write_json(request["solution_path"], payload)
    serialization_seconds = time.perf_counter() - serialize_started
    elapsed = time.perf_counter() - started
    trajectory = [[p[0], p[1] + construction, *p[2:]] for p in result.metadata["best_trajectory"]]
    row = {k: request[k] for k in ("stage", "case_id", "variant", "seed", "source_sha256", "budget_seconds")}
    row.update(scenario=spec["scenario"], task_count=spec["tasks"], drone_count=spec["drones"],
               max_tasks_per_drone=spec["K"], relay_count=spec["stations"],
               end_to_end_seconds=elapsed, construction_seconds=construction, search_seconds=search_seconds,
               validation_seconds=validation_seconds, serialization_seconds=serialization_seconds,
               valid=checked.valid, violations=list(checked.violations), budget_compliant=elapsed <= request["budget_seconds"],
               late_count=checked.score.late_count, total_lateness_min=checked.score.total_lateness_min,
               distance_km=checked.score.distance_km, on_time_count=spec["tasks"]-checked.score.late_count,
               on_time_rate=(spec["tasks"]-checked.score.late_count)/spec["tasks"], iterations=result.iterations,
               initial_routes_sha256=base.value_hash(initial.routes), initial_score=_jsonable(initial.evaluation.score),
               best_trajectory=trajectory, config=_jsonable(asdict(config)), requested_config=args,
               solver_method=result.metadata.get("method"), relay_statistics=payload["relay"],
               neighborhood_statistics=_jsonable(result.metadata.get("neighborhood_statistics", result.metadata.get("joint_search", {}))),
               profiling=_jsonable(result.metadata.get("profiling", {})),
               solution_sha256=base.sha256(request["solution_path"]))
    base.write_json(request["response_path"], row)


def invoke(output, manifest, variant, case, seed, stage):
    source = manifest["source_snapshots"][variant]
    run_id = f"{stage}__{case}__{variant}__{seed}"
    relative = f"run_solutions/{run_id}.json"
    if (output / relative).exists():
        raise ValueError("禁止覆盖已有完整解")
    with tempfile.TemporaryDirectory(prefix="joint-relay-") as temp:
        temp = Path(temp)
        with ZipFile(output / source["path"]) as archive:
            if any(len(Path(n).parts) != 2 or not n.startswith("uav_dispatch/") or not n.endswith(".py") for n in archive.namelist()):
                raise ValueError("非法源码归档")
            archive.extractall(temp / "src")
        request = dict(stage=stage, case_id=case, variant=variant, seed=seed, source_sha256=source["sha256"],
                       budget_seconds=BUDGETS[stage][case], solution_path=str(output / relative),
                       response_path=str(temp / "response.json"))
        base.write_json(temp / "request.json", request)
        env = {**os.environ, "PYTHONPATH": os.pathsep.join((str(temp / "src"), str(ROOT))), "PYTHONDONTWRITEBYTECODE": "1"}
        clock = time.perf_counter()
        try:
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", str(temp / "request.json")],
                           cwd=ROOT, env=env, check=True, timeout=BUDGETS[stage][case]+30)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            notes = output / "execution_notes.json"
            data = base.read_json(notes) if notes.exists() else {}
            data.setdefault("aborted_runs", []).append(dict(stage=stage,case_id=case,variant=variant,seed=seed,
                process_elapsed_seconds=time.perf_counter()-clock,reason=str(error)))
            base.write_json(notes, data)
            raise
        row = base.read_json(temp / "response.json")
    row.update(run_id=run_id, solution_file=relative)
    print(f"{run_id}: {base.score(row)} {row['end_to_end_seconds']:.2f}s valid={row['valid']} budget={row['budget_compliant']}",flush=True)
    return row


def pairs(rows, stage, case=PRIMARY):
    return base.paired(rows, stage, "joint", case)


def complete(rows, stage):
    return all(len(pairs(rows,stage,c))==len(SEEDS[stage]) for c in CASES[stage])


def screen_pass(rows):
    return complete(rows,"smoke") and complete(rows,"screen") and any(base.score(c)<base.score(b)
        for case in CASES["screen"] for b,c in pairs(rows,"screen",case))


def default_pass(rows):
    return complete(rows,"medium") and sum(base.score(c)<base.score(b) for b,c in pairs(rows,"medium"))>=2


def spent(output, rows):
    notes=output/"execution_notes.json"
    aborted=base.read_json(notes).get("aborted_runs",[]) if notes.exists() else []
    return sum(r["end_to_end_seconds"] for r in rows)+sum(r["process_elapsed_seconds"] for r in aborted)


def stage(output,manifest,name):
    verify(output,manifest)
    rows=base.read_json(output/"runs.json")
    cells={_cell(r) for r in rows}
    missing=sum(BUDGETS[name][c] for seed in SEEDS[name] for c in CASES[name] for v in VARIANTS if (name,c,v,seed) not in cells)
    if sum(r["end_to_end_seconds"] for r in rows if r["stage"]==name)+missing>CAPS[name] or spent(output,rows)+missing>TOTAL_SECONDS-RESERVED_SECONDS:
        raise ValueError("预算不足以预留整个阶段")
    notes=output/"execution_notes.json"
    aborted=base.read_json(notes).get("aborted_runs",[]) if notes.exists() else []
    for i,seed in enumerate(SEEDS[name]):
        order=list(VARIANTS) if i%2==0 else list(reversed(VARIANTS))
        for case in CASES[name]:
            for variant in order:
                cell=(name,case,variant,seed)
                if cell in cells:
                    continue
                if any(_cell(r)==cell for r in aborted):
                    raise ValueError("禁止重跑中止单元")
                if spent(output,rows)+BUDGETS[name][case]>TOTAL_SECONDS-RESERVED_SECONDS:
                    raise ValueError("累计预算不足")
                row=invoke(output,manifest,variant,case,seed,name)
                row["order"]=order.index(variant)
                rows.append(row)
                base.save_rows(output,rows)
                cells.add(cell)
    return rows


def run(output):
    manifest=base.read_json(output/"manifest.json")
    stage(output,manifest,"smoke")
    rows=stage(output,manifest,"screen")
    passed=screen_pass(rows)
    base.decision(output,"screen",selected_variant="joint" if passed else None,passed=passed,reason=PROTOCOL["screen_gate"])
    if passed:
        rows=stage(output,manifest,"medium")
        base.decision(output,"medium",selected_variant="joint",passed=default_pass(rows),reason=PROTOCOL["default_gate"])
    else:
        base.decision(output,"medium",selected_variant=None,passed=False,skipped=True,reason="没有初筛胜利，停止扩大")
    base.summarize(output)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",nargs="?",choices=("prepare","run"))
    parser.add_argument("--output-dir",type=Path,default=OUTPUT)
    parser.add_argument("--worker",type=Path)
    args=parser.parse_args(argv)
    if args.worker:
        worker(args.worker)
    elif args.command=="prepare":
        prepare(args.output_dir.resolve())
    elif args.command=="run":
        run(args.output_dir.resolve())
    else:
        parser.error("需要prepare或run")


if __name__=="__main__":
    main()
