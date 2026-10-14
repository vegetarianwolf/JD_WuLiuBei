"""Frozen 2x2 relay ablation: three paired seeds, all arms 240s end-to-end.

No result-based gates or retries. Current weighted periodic DIRECT behaviour
is retained; relay arms use the existing 95% staged warmup. All stages and a
no-search fixed-structure DIRECT counterfactual are saved for independent QA.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter
from types import MappingProxyType
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
from zoneinfo import ZoneInfo

import run_weighted_direct_pair as common
import weighted_direct_objective as weighted
import weighted_relay_objective as relay_adapter

ROOT, read, write, sha, digest = common.ROOT, common.read, common.write, common.sha, common.digest
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / "algorithm/results/weighted_relay_ablation_20261014"
PARENT = ROOT / "algorithm/results/weighted_periodic_improvement_20261014"
SEEDS = [2026101471, 2026101472, 2026101473]
ORDER = ["pure_direct", "direct_relay", "direct_stations", "direct_relay_stations"]
LABELS = {"pure_direct": "原点直送", "direct_relay": "原点直送＋中继",
          "direct_stations": "预部署直送", "direct_relay_stations": "预部署直送＋中继"}
SCENARIOS = {name: dict(relay="relay" in name, homes="stations" if "stations" in name else "origin",
                         label=LABELS[name]) for name in ORDER}
BUDGET = 240
NETWORK = dict(relay_count=4, relay_candidates_per_task=2, relay_detour_ratio=2., relay_location_seed=42,
               relay_location_method="weighted_kmedoids")
PERIODIC = dict(interval=120, max_candidates=16, anchors_per_route=4, pair_budget_seconds=.008)
EXECUTION = ["run_weighted_relay_ablation.py", "run_weighted_direct_pair.py", "weighted_direct_objective.py",
             "weighted_relay_objective.py", "weighted_periodic_exchange.py", "run_scenario_comparison.py"]


def build_case(tasks, name):
    from algorithm.experiments.run_scenario_comparison import build_scenario_problem
    spec = SCENARIOS[name]
    full, plan = build_scenario_problem(tasks, dict(relay=True, drone_homes=spec["homes"]),
                                         drones=8, max_tasks=25, **NETWORK)
    plain = replace(full, leg_registry={}, task_relay_candidates={})
    actual = full if spec["relay"] else plain
    topology = dict(stations=[[s.id, s.x, s.y] for s in full.relay_stations],
                    legs=[asdict(leg) for _, leg in sorted(full.leg_registry.items())],
                    task_relay_candidates={str(t): list(v) for t, v in full.task_relay_candidates.items()},
                    home_points=[[p.x, p.y] for p in full.home_points()],
                    drone_homes=list(full.drone_homes or (None,) * 8),
                    drone_count=8, max_tasks_per_drone=25, capacity=2, speed_km_per_min=.9,
                    relay_allowed=spec["relay"], network_parameters=NETWORK,
                    deployment=asdict(plan) if plan else None)
    return actual, plain, topology


def prepare(output):
    if output.exists() and any(output.iterdir()):
        raise ValueError("Select a new empty directory; never overwrite results")
    sys.path.insert(0, str(ROOT / "algorithm/src"))
    from uav_dispatch.alns import ALNSConfig, construct_regret_initial
    from uav_dispatch.io import load_tasks_csv
    parent = read(PARENT / "manifest.json")
    parent_audit = read(PARENT / "audit.json")
    assert parent_audit["passed"] and parent_audit["manifest_sha256"] == sha(PARENT / "manifest.json")
    tasks = load_tasks_csv(ROOT / parent["input_path"])
    assert len(tasks) == 200 and sha(ROOT / parent["input_path"]) == parent["input_sha256"]
    sources = output / "sources"
    sources.mkdir(parents=True)
    transformations, production = {}, {}
    archive = sources / "weighted_solver.zip"
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as package:
        for path in sorted((ROOT / "algorithm/src/uav_dispatch").glob("*.py")):
            production[path.name] = sha(path)
            text = path.read_text()
            if path.name in relay_adapter.EXPECTED_COUNTS:
                text, counts = relay_adapter.transform(text, path.name)
                assert counts == relay_adapter.EXPECTED_COUNTS[path.name]
                transformations[path.name] = counts
            info = ZipInfo("uav_dispatch/" + path.name, (2026, 10, 14, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            package.writestr(info, text)
    execution = {}
    for name in EXECUTION:
        source = ROOT / "algorithm/experiments" / name
        frozen = sources / name
        shutil.copyfile(source, frozen)
        execution[name] = dict(path=str(frozen.relative_to(output)), sha256=sha(frozen))
    cases = {}
    for name in ORDER:
        _, plain, topology = build_case(tasks, name)
        homes = SCENARIOS[name]["homes"] == "stations"
        initial = construct_regret_initial(plain, candidate_limit=48, home_seed=homes, home_bias=homes)
        topology_path = "topologies/" + name + ".json"
        write(output / topology_path, topology)
        config = asdict(ALNSConfig(max_iterations=10_000_000, seed=SEEDS[0], relay_enabled=SCENARIOS[name]["relay"],
                                  enable_home_seed=homes, enable_home_bias=homes,
                                  enable_lazy_repair=True, enable_terminal_task_exchange=True))
        cases[name] = dict(**SCENARIOS[name], topology_path=topology_path, topology_sha256=sha(output / topology_path),
                           topology_digest=digest(topology), initial_routes=[list(r) for r in initial.routes],
                           initial_routes_sha256=digest(initial.routes), initial_score=asdict(initial.evaluation.score),
                           config=config)
    for a, b in ((ORDER[0], ORDER[1]), (ORDER[2], ORDER[3])):
        assert cases[a]["initial_routes_sha256"] == cases[b]["initial_routes_sha256"]
    schedule = [dict(run_id=f"{name}__seed_{seed}", scenario=name, seed=seed)
                for index, seed in enumerate(SEEDS)
                for name in ORDER[index:] + ORDER[:index]]
    manifest = dict(kind="weighted_relay_ablation", schema_version=1, seeds=SEEDS, order=ORDER,
                    budget_seconds=BUDGET, finalization_reserve_seconds=1., schedule=schedule,
                    scenarios=cases, weights=parent["weights"], scales=parent["scales"], formula=parent["formula"],
                    periodic_parameters=PERIODIC, network_parameters=NETWORK,
                    input_path=parent["input_path"], input_sha256=parent["input_sha256"],
                    problem_path=parent["problem_path"], problem_sha256=parent["problem_sha256"],
                    source_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
                    source_archive=dict(path="sources/weighted_solver.zip", sha256=sha(archive)),
                    production_files_sha256=production, adapter_changes=transformations, execution_sources=execution,
                    parent_manifest_path=str((PARENT / "manifest.json").relative_to(ROOT)),
                    parent_manifest_sha256=sha(PARENT / "manifest.json"),
                    parent_audit_sha256=sha(PARENT / "audit.json"),
                    environment=dict(python=sys.version, platform=platform.platform()),
                    created_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                    protocol=dict(planned_unique_runs=12, no_result_based_stopping=True,
                        purpose="Equal-budget 2x2 ablation of relay within each fixed home configuration; all seeds fixed before solving.",
                        timing="Input, fixed network/deployment, common DIRECT construction, every solver phase, full validation and all solution writes included; Python startup/imports excluded.",
                        objective="Fixed current research J, not an official scalar score; paper manuscript's original lexicographic results kept separate.",
                        K_interpretation="Paper extension: final delivery responsibility only, inbound relay transport excluded; station cache unlimited and zero handling time.",
                        controls="Same source/config within each home pair except relay_enabled and immutable relay registry; identical homes and DIRECT initial routes.",
                        relay_policy="Current 0.95 DIRECT warmup on relay Problem; active relay phase skips DIRECT periodic exchange. Warmup has plan-index overhead and is not an identical search prefix to pure DIRECT.",
                        counterfactual="Delete inbound relay events and replace outbound leg events with whole DIRECT task at the same positions; no reoptimization or extra solver sample.",
                        interpretation="Relay versus its own shorter warmup is incremental evidence, not a substitute for full-budget paired DIRECT; fixed-structure counterfactual is mechanism evidence only.",
                        historical="Old unequal-budget and lexicographic records excluded from new statistics; all worsening arms retained."))
    write(output / "manifest.json", manifest)
    write(output / "runs.json", [])
    print(json.dumps(dict(prepared=str(output), runs=12, budget_seconds=240, seeds=SEEDS), ensure_ascii=False))


def payload(problem, result, manifest):
    from uav_dispatch.cli import result_payload
    value = result_payload(problem, result, source=manifest["input_path"])
    value["problem"]["objective_order"] = ["weighted_total_score", "late_count", "total_lateness_min", "distance_km"]
    value["problem"]["experiment_objective"] = dict(kind="weighted", formula=manifest["formula"],
                                                   weights=manifest["weights"], scales=manifest["scales"])
    return value


def worker(request_path):
    req = read(request_path)
    output = Path(req["output"])
    manifest = read(output / "manifest.json")
    name, seed, run_id = req["scenario"], req["seed"], req["run_id"]
    case = manifest["scenarios"][name]
    from uav_dispatch import alns
    from uav_dispatch.io import load_tasks_csv
    from uav_dispatch.model import Score
    from uav_dispatch.search import SolverResult
    from uav_dispatch.validation import evaluate_solution
    import weighted_periodic_exchange as periodic
    started = perf_counter()
    tasks = load_tasks_csv(ROOT / manifest["input_path"])
    problem, plain, topology = build_case(tasks, name)
    assert digest(topology) == case["topology_digest"]
    homes = case["homes"] == "stations"
    initial = alns.construct_regret_initial(plain, candidate_limit=48, home_seed=homes, home_bias=homes)
    assert digest(initial.routes) == case["initial_routes_sha256"]
    construction = perf_counter() - started
    config = alns.ALNSConfig(**{**case["config"], "seed": seed,
        "time_limit_seconds": max(.001, BUDGET - construction - manifest["finalization_reserve_seconds"])})
    artifacts = []
    def save(result, phase, phase_config=None, **extra):
        checked = evaluate_solution(problem, result.routes, _force_full=True)
        assert checked.valid and checked.score == result.evaluation.score
        value = payload(problem, replace(result, evaluation=checked), manifest)
        path = f"phase_solutions/{run_id}__{phase}.json"
        write(output / path, value)
        record = dict(phase=phase, solution_path=path, solution_sha256=sha(output / path),
                      routes_sha256=digest(result.routes), score=asdict(checked.score),
                      weighted_score=sum(w * getattr(checked.score, key) / s for w, key, s in zip(manifest["weights"],
                          ("late_count", "total_lateness_min", "distance_km"), manifest["scales"])),
                      config=asdict(phase_config) if phase_config else None, **extra)
        artifacts.append(record)
        return value
    phases = []
    original = alns.solve_alns
    def capture(problem_arg, *, config, initial_routes=None):
        begin = perf_counter()
        result = original(problem_arg, config=config, initial_routes=initial_routes)
        solve_wall = perf_counter() - begin
        phase = ("relay_search" if config.relay_enabled else "direct_warmup") if case["relay"] else "direct_search"
        save(result, phase, config, solve_wall_seconds=solve_wall, start_offset_seconds=begin-started,
             input_routes_sha256=digest(initial_routes), runtime_seconds=result.runtime_seconds,
             iterations=result.iterations, end_offset_seconds=perf_counter()-started)
        phases.append(artifacts[-1])
        return result
    stats = {}
    with weighted.objective(Score, manifest["scales"], manifest["weights"]):
        save(initial, "initial")
        alns.solve_alns = capture
        try:
            search_started = perf_counter()
            with periodic.periodic_exchange(alns, statistics=stats, **manifest["periodic_parameters"]):
                result = (alns.solve_relay_staged if case["relay"] else alns.solve_alns)(
                    problem, config=config, initial_routes=initial.routes)
            search_seconds = perf_counter() - search_started
        finally:
            alns.solve_alns = original
        result = replace(result, metadata=MappingProxyType({**result.metadata, "periodic_task_exchange": stats}))
        final = payload(problem, result, manifest)
        check = evaluate_solution(problem, result.routes, _force_full=True)
        assert check.valid and check.score == result.evaluation.score
        final_path = f"run_solutions/{run_id}.json"
        write(output / final_path, final)
        # Fixed structure, no optimizer: preserve final responsibility and
        # remaining event order, replace station pickups by client pickups.
        routes = []
        for route in result.routes:
            new = []
            for visit in route:
                leg = problem.leg_registry.get(abs(visit))
                if leg is None:
                    new.append(visit)
                elif leg.kind == "RELAY_OUT":
                    new.append(leg.task_id if visit > 0 else -leg.task_id)
            routes.append(tuple(new))
        direct = tuple(routes)
        direct_check = evaluate_solution(plain, direct, _force_full=True)
        assert direct_check.valid
        direct_result = SolverResult(direct, direct_check)
        direct_path = f"counterfactuals/{run_id}__fixed_structure_direct.json"
        write(output / direct_path, payload(plain, direct_result, manifest))
    elapsed = perf_counter() - started
    physical = asdict(check.score)
    contributions = [w * physical[k] / s for k, w, s in zip(
        ("late_count", "total_lateness_min", "distance_km"), manifest["weights"], manifest["scales"])]
    row = dict(run_id=run_id, scenario=name, label=case["label"], seed=seed, objective="weighted",
               relay_enabled=case["relay"], home_group=case["homes"], budget_seconds=BUDGET,
               end_to_end_seconds=elapsed, construction_seconds=construction, search_seconds=search_seconds,
               valid=check.valid, budget_compliant=elapsed <= BUDGET, config=asdict(config),
               iterations=result.iterations, **physical, weighted_score=sum(contributions),
               weighted_components=contributions, on_time_count=200-physical["late_count"],
               on_time_rate=1-physical["late_count"]/200, delivered_count=200,
               source_sha256=manifest["source_archive"]["sha256"], topology_sha256=case["topology_sha256"],
               initial_routes_sha256=digest(initial.routes), solution_path=final_path,
               solution_sha256=sha(output / final_path), phase_artifacts=artifacts, phases=phases,
               counterfactual_path=direct_path, counterfactual_sha256=sha(output / direct_path),
               counterfactual_score=asdict(direct_check.score),
               best_trajectory=final["metadata"].get("best_trajectory", []),
               relay_statistics=final["relay"], periodic_task_exchange=final["metadata"]["periodic_task_exchange"])
    write(req["response"], row)


def verify(output, manifest):
    assert manifest["seeds"] == SEEDS and manifest["order"] == ORDER and manifest["budget_seconds"] == BUDGET
    for name, spec in manifest["execution_sources"].items():
        assert sha(output / spec["path"]) == spec["sha256"] == sha(ROOT / "algorithm/experiments" / name)
    assert sha(ROOT / manifest["input_path"]) == manifest["input_sha256"]
    assert sha(output / manifest["source_archive"]["path"]) == manifest["source_archive"]["sha256"]
    for case in manifest["scenarios"].values():
        assert sha(output / case["topology_path"]) == case["topology_sha256"]


def run(output):
    manifest = read(output / "manifest.json")
    verify(output, manifest)
    rows = read(output / "runs.json")
    schedule = manifest["schedule"]
    assert [r["run_id"] for r in rows] == [c["run_id"] for c in schedule[:len(rows)]]
    for cell in schedule[len(rows):]:
        assert not (output / "run_solutions" / (cell["run_id"] + ".json")).exists(), "Never overwrite an isolated saved result"
        assert not any((output / "phase_solutions").glob(cell["run_id"] + "__*.json")), "Never overwrite an isolated phase or retry based on its score"
        assert not any((output / "counterfactuals").glob(cell["run_id"] + "__*.json")), "Never overwrite a saved counterfactual"
        with tempfile.TemporaryDirectory(prefix="weighted-relay-ablation-") as temp:
            temp = Path(temp)
            with ZipFile(output / manifest["source_archive"]["path"]) as package:
                assert all(n.startswith("uav_dispatch/") and n.endswith(".py") and len(Path(n).parts)==2 for n in package.namelist())
                package.extractall(temp)
            request = temp / "request.json"
            write(request, dict(**cell, output=str(output), response=str(temp / "response.json")))
            print("START " + cell["run_id"], flush=True)
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "worker", "--request", str(request)],
                           cwd=ROOT, env={**os.environ, "PYTHONPATH": os.pathsep.join((str(temp), str(ROOT))),
                                          "PYTHONDONTWRITEBYTECODE": "1"}, check=True, timeout=270)
            row = read(temp / "response.json")
        rows.append(row)
        write(output / "runs.json", rows)
        print(json.dumps({k:row[k] for k in ("run_id", "late_count", "total_lateness_min", "distance_km", "weighted_score", "end_to_end_seconds")}, ensure_ascii=False), flush=True)
        if not row["valid"] or not row["budget_compliant"]:
            raise RuntimeError("Invalid or over-budget result retained; do not retry or tune from its score")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "worker"))
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--request", type=Path)
    args = parser.parse_args()
    if args.command == "prepare": prepare(args.output_dir.resolve())
    elif args.command == "run": run(args.output_dir.resolve())
    else: worker(args.request)
