"""Bounded research on batch/reciprocal cross-UAV relay neighborhoods.

Each batch is frozen before execution; no rerun or overwriting a cell.
Diagnostic batches may start at a historical DIRECT checkpoint and therefore
are explicitly NOT equal-budget performance comparisons. Fresh screen batches
use the same construction, homes, weights and total budget in every arm.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter
from types import MappingProxyType
from zipfile import ZipFile

ROOT = Path(os.environ.get("JD_RELAY_WORKSPACE_ROOT", str(Path(__file__).resolve().parents[2]))).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PARENT = ROOT / "algorithm/results/weighted_relay_ablation_20261014"
DEFAULT = ROOT / "algorithm/results/proactive_relay_screen_20261014/batch001"
EXPERIMENTS = ROOT / "algorithm/experiments"
FIELDS = ("late_count", "total_lateness_min", "distance_km")
METHODS = {"batch": "batch_relay_refinement", "reciprocal": "reciprocal_relay_refinement",
           "tail_direct": "relay_tail_crossover", "tail_relay": "relay_tail_crossover",
           "construct_direct": "relay_first_constructor", "construct_relay": "relay_first_constructor",
           "inflight": "inflight_relay_swap", "bundle": "inflight_relay_bundle",
           "cluster_direct": "relay_cluster_beam", "cluster_relay": "relay_cluster_beam",
           "mobile": "mobile_relay_refinement", "mobile_v2": "mobile_relay_refinement_v2",
           "mobile_v3": "mobile_relay_refinement_v3", "directed": "directed_bundle_insertion",
           "delivery_direct": "mobile_delivery_reconstruction", "delivery_relay": "mobile_delivery_reconstruction",
           "anticipatory": "anticipatory_mobile_handoff",
           "quota_direct": "quota_balanced_relay_exchange", "quota_relay": "quota_balanced_relay_exchange"}
DIRECT_ARMS = ("direct", "tail_direct", "construct_direct", "cluster_direct", "delivery_direct", "quota_direct")
ANTICIPATORY_ARMS = ("anticipatory",)
MOBILE_ARMS = ("mobile", "mobile_v2", "mobile_v3", "delivery_relay", *ANTICIPATORY_ARMS)
EXECUTION = (
    "run_proactive_relay_research.py", "batch_relay_refinement.py", "reciprocal_relay_refinement.py",
    "relay_tail_crossover.py",
    "relay_first_constructor.py",
    "inflight_relay_swap.py",
    "inflight_relay_bundle.py",
    "relay_cluster_beam.py",
    "mobile_relay_refinement.py", "audit_mobile_relay.py",
    "mobile_relay_refinement_v2.py",
    "mobile_relay_refinement_v3.py", "mobile_geometry_bound.py", "mobile_deadline_geometry.py",
    "mobile_delivery_reconstruction.py",
    "anticipatory_mobile_handoff.py", "audit_anticipatory_mobile.py",
    "quota_balanced_relay_exchange.py",
    "directed_bundle_insertion.py",
    "run_weighted_relay_ablation.py", "run_weighted_direct_pair.py", "weighted_direct_objective.py",
    "weighted_relay_objective.py", "weighted_periodic_exchange.py", "run_scenario_comparison.py",
    "audit_weighted_relay_ablation.py", "audit_weighted_direct_pair.py",
)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sha(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def key(score, manifest):
    values = [getattr(score, field) for field in FIELDS]
    return (sum(w * v / s for w, v, s in zip(manifest["weights"], values, manifest["scales"])), *values)


def model_topology(problem, template):
    """Freeze candidate geometry; physical homes never follow a moved site."""
    return {**template,
            "stations": [[s.id, s.x, s.y] for s in problem.relay_stations],
            "legs": [asdict(v) for _, v in sorted((problem.leg_registry or {}).items())],
            "task_relay_candidates": {str(t): list(v) for t, v in problem.task_relay_candidates.items()},
            "home_points": [[p.x, p.y] for p in problem.home_points()]}


def unchanged_physics(before, after):
    assert before.tasks == after.tasks
    for field in ("drone_count", "drone_homes", "max_tasks_per_drone", "capacity", "speed_km_per_min", "depot", "max_relay_hops"):
        assert getattr(before, field) == getattr(after, field), "Changed physical resource: " + field
    assert before.home_points() == after.home_points(), "Rendezvous optimization changed starting positions"
    old = {s.id: (s.x, s.y) for s in before.relay_stations}
    new = {s.id: (s.x, s.y) for s in after.relay_stations}
    assert all(new.get(s) == xy for s, xy in old.items()), "Existing station or home was moved"


def build_case(tasks, homes):
    # Preserve exactly the previously frozen four sites and deployment policy.
    # Only the algorithmic station shortlist expands: all four existing sites
    # get legs for all tasks, with no artificial route-event cap.
    from run_scenario_comparison import build_scenario_problem
    from uav_dispatch.model import TransportLeg
    network = dict(relay_count=4, relay_candidates_per_task=2, relay_detour_ratio=2.,
                   relay_location_seed=42, relay_location_method="weighted_kmedoids")
    full, plan = build_scenario_problem(tasks, dict(relay=True, drone_homes=homes),
                                      drones=8, max_tasks=25, **network)
    plain = replace(full, leg_registry={}, task_relay_candidates={})
    topology = dict(stations=[[s.id, s.x, s.y] for s in full.relay_stations],
                    home_points=[[p.x, p.y] for p in full.home_points()],
                    drone_homes=list(full.drone_homes or (None,) * 8),
                    drone_count=8, max_tasks_per_drone=25, capacity=2, speed_km_per_min=.9,
                    relay_allowed=True, network_parameters=network, deployment=asdict(plan) if plan else None)
    legs, candidates, counter = {}, {}, max(t.id for t in tasks) + 1
    for task in sorted(tasks, key=lambda t: t.id):
        candidates[task.id] = tuple(s.id for s in full.relay_stations)
        for station in full.relay_stations:
            legs[counter] = TransportLeg(counter, task.id, "RELAY_IN", station.id)
            legs[counter + 1] = TransportLeg(counter + 1, task.id, "RELAY_OUT", station.id)
            counter += 2
    full = replace(full, leg_registry=legs, task_relay_candidates=candidates)
    topology = {**topology, "legs": [asdict(v) for _, v in sorted(legs.items())],
                "task_relay_candidates": {str(t): list(v) for t, v in candidates.items()},
                "network_parameters": {**topology["network_parameters"], "relay_candidates_per_task": 4,
                                       "relay_detour_ratio": None, "candidate_policy": "all existing four sites"},
                "research_station_shortlist": "all four existing fixed stations; no event cap"}
    return full, plain, topology


def prepare(args):
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a new empty batch directory; never replace an existing experiment")
    sys.path.insert(0, str(ROOT / "algorithm/src"))
    from uav_dispatch.alns import construct_regret_initial
    from uav_dispatch.io import load_tasks_csv
    from uav_dispatch.validation import evaluate_solution
    parent = read(PARENT / "manifest.json")
    audit = read(PARENT / "audit.json")
    assert audit["passed"] and audit["manifest_sha256"] == sha(PARENT / "manifest.json")
    assert audit["runs_sha256"] == sha(PARENT / "runs.json")
    tasks = load_tasks_csv(ROOT / parent["input_path"])
    full, plain, topology = build_case(tasks, args.homes)
    initial = construct_regret_initial(plain, candidate_limit=48,
                                      home_seed=args.homes == "stations", home_bias=args.homes == "stations")
    assert digest(initial.routes) == parent["scenarios"]["direct_stations" if args.homes == "stations" else "pure_direct"]["initial_routes_sha256"]
    checkpoint = None
    if args.checkpoint:
        path = args.checkpoint.resolve()
        saved = read(path)
        routes = tuple(tuple(r["encoded_visits"]) for r in saved["routes"])
        checked = evaluate_solution(plain, routes, _force_full=True)
        assert checked.valid and asdict(checked.score) == saved["score"]
        assert saved["problem"]["drone_homes"] == ["origin" if h is None else f"station_{h}" for h in topology["drone_homes"]]
        initial = replace(initial, routes=routes, evaluation=checked)
        checkpoint = dict(path=str(path.relative_to(ROOT)), sha256=sha(path),
                          boundary="Historical solved checkpoint + extra research budget; not an equal-budget DIRECT comparison")
    sources = output / "sources"
    sources.mkdir(parents=True)
    archive = sources / "weighted_solver.zip"
    parent_archive = PARENT / parent["source_archive"]["path"]
    assert sha(parent_archive) == parent["source_archive"]["sha256"]
    shutil.copyfile(parent_archive, archive)
    execution = {}
    for name in EXECUTION:
        source = EXPERIMENTS / name
        assert source.is_file(), "Candidate must be implemented before freezing: " + name
        copy = sources / name
        shutil.copyfile(source, copy)
        execution[name] = dict(path=str(copy.relative_to(output)), sha256=sha(copy))
    arms = args.arms.split(",")
    assert len(set(arms)) == len(arms) and set(arms) <= {"direct", *METHODS, "combined"}
    parameters = args.method_parameters
    assert isinstance(parameters, dict) and set(parameters) <= set(arms)
    import inspect
    reserved = {"problem", "routes", "deadline", "weights", "scales", "seed", "enable_transfers", "relay_enabled"}
    for method, values in parameters.items():
        assert method in METHODS and isinstance(values, dict) and not (set(values) & reserved)
        signature = inspect.signature(importlib.import_module(METHODS[method]).refine)
        assert set(values) <= set(signature.parameters), "Unknown frozen method parameter"
    assert args.budget_seconds > 1 and 0 < args.warmup_fraction < 1
    schedule = [dict(arm=arm, seed=args.seed, run_id=f"{arm}__seed_{args.seed}") for arm in arms]
    manifest = dict(kind="proactive_relay_research", schema_version=1,
        purpose="checkpoint_mechanism_diagnostic" if checkpoint else "fresh_equal_budget_screen",
        prepared_at=datetime.now(timezone.utc).isoformat(), parent_manifest_sha256=sha(PARENT / "manifest.json"),
        parent_audit_sha256=sha(PARENT / "audit.json"), parent_source_archive_sha256=sha(parent_archive),
        execution_worktree_base_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
        inherited_solver_source_commit=parent["source_commit"],
        source_archive=dict(path="sources/weighted_solver.zip", sha256=sha(archive)), execution_sources=execution,
        input_path=parent["input_path"], input_sha256=parent["input_sha256"],
        problem_path=parent["problem_path"], problem_sha256=parent["problem_sha256"],
        homes=args.homes, topology=topology, topology_sha256=digest(topology),
        weights=parent["weights"], scales=parent["scales"], formula=parent["formula"],
        budget_seconds=args.budget_seconds, finalization_reserve_seconds=1., warmup_fraction=args.warmup_fraction,
        checkpoint=checkpoint, initial_routes=[list(r) for r in initial.routes], initial_routes_sha256=digest(initial.routes),
        initial_score=asdict(initial.evaluation.score), schedule=schedule,
        periodic_parameters=dict(interval=120, max_candidates=16, anchors_per_route=4, pair_budget_seconds=.008),
        method_parameters=parameters,
        protocol="Official full 200 tasks, 8 UAVs, C2, final K25, equal fixed homes; frozen weighted preference. Every empty flight and global transfer wait counted. No additional orders/resources/free repositioning. Station coordinates and home allocation unchanged, all four existing sites registered. Only complete checked macro plans adopted. Sequential single-seed exploratory batch; no significance claim or seed replacement.",
        timing_boundary="CSV/network/construction, all optimizer phases, validation and saved solutions/CF included, ending after CF write. Python imports and IPC response/parent-ledger writes excluded.")
    if set(arms) & set(MOBILE_ARMS):
        manifest["model_extension"] = "Optimized synchronous mobile rendezvous, separate from the fixed-four-site cache model"
        manifest["protocol"] = (
            "Official full 200 tasks, 8 UAVs, C2, final K25, same fixed physical homes, weights, scales and deadlines. "
            "New rendezvous coordinates are selected during search and used once by one UAV pair. "
            "Reciprocal arms exchange equal-sized onboard cargo groups only when both aircraft are present; early aircraft waits holding cargo. "
            "Every flight and synchronization wait is charged; no mobile ground cache, new vehicle or free initial relocation. "
            "This changes the prior fixed-four-site relay extension and is labelled separately. "
            "Sequential source-bound exploratory batch, no significance claim or seed replacement.")
        if set(arms) & set(ANTICIPATORY_ARMS):
            manifest["protocol"] += (
                " Anticipatory arms instead transfer one or two parcels in one direction: the empty receiver "
                "must arrive before the sender and wait until contact. The sender retains cargo until contact; "
                "all receiver travel and waiting are charged. This conservative branch starts from pure DIRECT "
                "routes, preserves final owners, and allows at most one new meeting per call. "
                "It is independently audited by the separate receiver-first physical auditor.")
    if any(arm.startswith("quota_") for arm in arms):
        manifest["protocol"] += (
            " Quota arms trade two whole delivery responsibilities between two aircraft. "
            "The relay arm may carry one request through an existing station while the compensating request "
            "travels directly; the matched DIRECT arm disables only that relay option. "
            "Both use the same declared pair selector, insertion bounds and rebuilding orders.")
    write(output / "manifest.json", manifest)
    write(output / "runs.json", [])
    print(json.dumps({"prepared": str(output), "purpose": manifest["purpose"], "arms": arms,
                      "initial_score": manifest["initial_score"]}, ensure_ascii=False))


def verify(output, manifest, *, current_execution=True):
    assert sha(PARENT / "manifest.json") == manifest["parent_manifest_sha256"]
    assert sha(PARENT / "audit.json") == manifest["parent_audit_sha256"]
    assert digest(manifest["topology"]) == manifest["topology_sha256"]
    assert digest(manifest["initial_routes"]) == manifest["initial_routes_sha256"]
    assert sha(output / manifest["source_archive"]["path"]) == manifest["source_archive"]["sha256"]
    for name, spec in manifest["execution_sources"].items():
        assert sha(output / spec["path"]) == spec["sha256"], name
        if current_execution:
            assert sha(EXPERIMENTS / name) == spec["sha256"], name
    for field in ("input", "problem"):
        assert sha(ROOT / manifest[field + "_path"]) == manifest[field + "_sha256"]
    if manifest["checkpoint"]:
        assert sha(ROOT / manifest["checkpoint"]["path"]) == manifest["checkpoint"]["sha256"]


def worker(request):
    req = read(request)
    output = Path(req["output"])
    manifest = read(output / "manifest.json")
    verify(output, manifest, current_execution=False)
    for name, spec in manifest["execution_sources"].items():
        assert sha(Path(__file__).parent / name) == spec["sha256"], "Worker must run exact frozen execution files: " + name
    from uav_dispatch import alns
    from uav_dispatch.cli import result_payload
    from uav_dispatch.io import load_tasks_csv
    from uav_dispatch.model import Score
    from uav_dispatch.search import SolverResult
    from uav_dispatch.validation import evaluate_solution
    import weighted_direct_objective as weighted
    import weighted_periodic_exchange as periodic
    import run_scenario_comparison  # Load frozen scenario-helper imports before the measured interval.
    modules = {name: importlib.import_module(module) for name, module in METHODS.items()}
    started = perf_counter()
    full, plain, topology = build_case(load_tasks_csv(ROOT / manifest["input_path"]), manifest["homes"])
    assert digest(topology) == manifest["topology_sha256"]
    routes = tuple(tuple(r) for r in manifest["initial_routes"])
    if not manifest["checkpoint"]:
        initial = alns.construct_regret_initial(plain, candidate_limit=48,
                     home_seed=manifest["homes"] == "stations", home_bias=manifest["homes"] == "stations")
        assert digest(initial.routes) == manifest["initial_routes_sha256"]
        routes = initial.routes
    phases, periodic_stats, mobile_meetings = [], {}, []
    initial_check = evaluate_solution(full, routes, _force_full=True)
    assert initial_check.valid and asdict(initial_check.score) == manifest["initial_score"]
    construction = perf_counter() - started
    deadline = started + manifest["budget_seconds"] - manifest["finalization_reserve_seconds"]
    def save(current, evaluation, phase, **extras):
        problem = plain if req["arm"] in DIRECT_ARMS else full
        result = SolverResult(tuple(current), evaluation, runtime_seconds=extras.get("solve_wall_seconds", 0.),
                              iterations=extras.get("iterations", 0), metadata=MappingProxyType(extras.get("metadata", {})))
        value = result_payload(problem, result, source=manifest["input_path"])
        value["problem"]["objective_order"] = ["weighted_total_score", *FIELDS]
        value["problem"]["experiment_objective"] = dict(kind="weighted", weights=manifest["weights"], scales=manifest["scales"])
        path = f"phase_solutions/{req['run_id']}__{phase}.json"
        write(output / path, value)
        record = dict(phase=phase, solution_path=path, solution_sha256=sha(output / path),
                      routes_sha256=digest(current),
                      topology=model_topology(full, topology),
                      input_routes_sha256=phases[-1]["routes_sha256"] if phases else manifest["initial_routes_sha256"],
                      score=asdict(evaluation.score), weighted_score=key(evaluation.score, manifest)[0], **extras)
        phases.append(record)
        return value
    with weighted.objective(Score, manifest["scales"], manifest["weights"]):
        save(routes, initial_check, "initial")
        if not manifest["checkpoint"]:
            seconds = max(.001, (deadline - perf_counter()) * (1. if req["arm"] == "direct" else manifest["warmup_fraction"]))
            cfg = alns.ALNSConfig(max_iterations=10_000_000, seed=req["seed"], time_limit_seconds=seconds,
                relay_enabled=False, enable_home_seed=manifest["homes"] == "stations",
                enable_home_bias=manifest["homes"] == "stations", enable_terminal_task_exchange=True,
                enable_lazy_repair=True)
            begin = perf_counter()
            with periodic.periodic_exchange(alns, statistics=periodic_stats, **manifest["periodic_parameters"]):
                result = alns.solve_alns(plain, config=cfg, initial_routes=routes)
            checked = evaluate_solution(full, result.routes, _force_full=True)
            assert checked.valid
            routes = result.routes
            save(routes, checked, "direct_search", start_offset_seconds=begin-started,
                 solve_wall_seconds=perf_counter()-begin, config=asdict(cfg), iterations=result.iterations)
        else:
            checked = initial_check
        arm = req["arm"]
        methods = ["batch", "reciprocal", "tail_relay"] if arm == "combined" else ([arm] if arm != "direct" else [])
        for number, method in enumerate(methods):
            begin = perf_counter()
            sub_deadline = begin + (deadline - begin) / (len(methods) - number)
            controls = {"enable_transfers": method == "tail_relay"} if method.startswith("tail_") else {}
            if method.startswith("construct_"):
                controls = {"relay_enabled": method == "construct_relay"}
            if method.startswith("cluster_"):
                controls = {"relay_enabled": method == "cluster_relay"}
            if method.startswith("delivery_"):
                controls = {"relay_enabled": method == "delivery_relay"}
            if method.startswith("quota_"):
                controls = {"relay_enabled": method == "quota_relay"}
            controls = {**controls, **manifest.get("method_parameters", {}).get(method, {})}
            outcome = modules[method].refine(full, routes, deadline=sub_deadline,
                        weights=manifest["weights"], scales=manifest["scales"], seed=req["seed"], **controls)
            if "problem" in outcome:
                assert method in MOBILE_ARMS, "Only the separately declared mobile extension may add rendezvous points"
                unchanged_physics(full, outcome["problem"])
                full = outcome["problem"]
                plain = replace(full, leg_registry={}, task_relay_candidates={})
                mobile_meetings = outcome["metadata"]["mobile_meetings"]
            candidate = evaluate_solution(full, outcome["routes"], _force_full=True)
            assert candidate.valid
            # The module owns all pre-deadline adoption guards; this guard also
            # prevents a final checked phase from worsening its full incumbent.
            assert key(candidate.score, manifest) <= key(checked.score, manifest)
            routes, checked = outcome["routes"], candidate
            save(routes, checked, method, start_offset_seconds=begin-started,
                 solve_wall_seconds=perf_counter()-begin, iterations=outcome["iterations"], metadata=outcome["metadata"])
        final = save(routes, checked, "final", metadata=dict(periodic_task_exchange=periodic_stats,
                                                            mobile_meetings=mobile_meetings))
        counter_routes = tuple(tuple(v if abs(v) not in full.leg_registry else
            (full.leg_registry[abs(v)].task_id if v > 0 else -full.leg_registry[abs(v)].task_id)
            for v in route if abs(v) not in full.leg_registry or full.leg_registry[abs(v)].kind == "RELAY_OUT") for route in routes)
        counter = evaluate_solution(plain, counter_routes, _force_full=True)
        assert counter.valid
        counter_payload = result_payload(plain, SolverResult(counter_routes, counter), source=manifest["input_path"])
        counter_path = f"counterfactuals/{req['run_id']}.json"
        write(output / counter_path, counter_payload)
    elapsed = perf_counter() - started
    row = dict(**{k:req[k] for k in ("run_id", "arm", "seed")}, purpose=manifest["purpose"],
        valid=checked.valid, budget_compliant=elapsed <= manifest["budget_seconds"], budget_seconds=manifest["budget_seconds"],
        end_to_end_seconds=elapsed, construction_seconds=construction, phases=phases,
        **asdict(checked.score), weighted_score=key(checked.score, manifest)[0], on_time_count=200-checked.score.late_count,
        initial_routes_sha256=digest(manifest["initial_routes"]), source_sha256=manifest["source_archive"]["sha256"],
        solution_path=phases[-1]["solution_path"], solution_sha256=phases[-1]["solution_sha256"],
        counterfactual_path=counter_path, counterfactual_sha256=sha(output / counter_path))
    write(req["response"], row)


def run(output):
    manifest = read(output / "manifest.json")
    verify(output, manifest)
    rows = read(output / "runs.json")
    assert [r["run_id"] for r in rows] == [c["run_id"] for c in manifest["schedule"][:len(rows)]]
    for cell in manifest["schedule"][len(rows):]:
        assert not any((output / "phase_solutions").glob(cell["run_id"] + "__*.json")), "No retry of an existing cell"
        receipt = output / "receipts" / (cell["run_id"] + ".json")
        assert not receipt.exists(), "Previously started cell is never automatically retried"
        start_receipt = dict(**cell, status="started", started_at=datetime.now(timezone.utc).isoformat(),
                             manifest_sha256=sha(output / "manifest.json"))
        write(receipt, start_receipt)
        with tempfile.TemporaryDirectory(prefix="proactive-relay-") as temp:
            temp = Path(temp)
            with ZipFile(output / manifest["source_archive"]["path"]) as package:
                assert all(n.startswith("uav_dispatch/") and n.endswith(".py") and len(Path(n).parts)==2 for n in package.namelist())
                package.extractall(temp)
            request = temp / "request.json"
            write(request, dict(**cell, output=str(output), response=str(temp / "response.json")))
            print("START " + cell["run_id"], flush=True)
            try:
                subprocess.run([sys.executable, str(output / "sources/run_proactive_relay_research.py"), "worker", "--request", str(request)],
                    cwd=ROOT, env={**os.environ, "JD_RELAY_WORKSPACE_ROOT": str(ROOT),
                                  "PYTHONPATH": os.pathsep.join((str(temp),str(ROOT))), "PYTHONDONTWRITEBYTECODE":"1"},
                    check=True, timeout=manifest["budget_seconds"] + 30)
            except BaseException as error:
                write(receipt, {**start_receipt, "status": "failed_or_interrupted", "error_type": type(error).__name__})
                raise
            row = read(temp / "response.json")
            write(output / "receipts" / (cell["run_id"] + "__response.json"), row)
        rows.append(row)
        write(output / "runs.json", rows)
        write(receipt, {**start_receipt, "status": "completed", "completed_at": datetime.now(timezone.utc).isoformat(),
                        "response_sha256": sha(output / "receipts" / (cell["run_id"] + "__response.json"))})
        print(json.dumps({k:row[k] for k in ("run_id", *FIELDS, "weighted_score", "end_to_end_seconds")}), flush=True)
        assert row["valid"] and row["budget_compliant"], "Failed cell retained; no score-based retry"


def audit(output):
    from algorithm.experiments.audit_weighted_direct_pair import load_official_csv
    from algorithm.experiments.audit_weighted_relay_ablation import audit_payload, direct_counterfactual, reference_schedule
    manifest = read(output / "manifest.json")
    verify(output, manifest, current_execution=False)
    # The independent geometry/DAG auditors are immutable across research
    # iterations; newer orchestration does not change historical evidence.
    for name in ("audit_weighted_relay_ablation.py", "audit_weighted_direct_pair.py"):
        assert sha(EXPERIMENTS / name) == manifest["execution_sources"][name]["sha256"]
    mobile = any(r["arm"] in MOBILE_ARMS for r in manifest["schedule"])
    if mobile:
        from algorithm.experiments.audit_mobile_relay import audit_mobile_payload, audit_mobile_routes
        assert sha(EXPERIMENTS / "audit_mobile_relay.py") == manifest["execution_sources"]["audit_mobile_relay.py"]["sha256"]
    if any(r["arm"] in ANTICIPATORY_ARMS for r in manifest["schedule"]):
        from algorithm.experiments.audit_anticipatory_mobile import audit_anticipatory_payload, audit_anticipatory_routes
        assert sha(EXPERIMENTS / "audit_anticipatory_mobile.py") == manifest["execution_sources"]["audit_anticipatory_mobile.py"]["sha256"]
    tasks = load_official_csv(ROOT / manifest["input_path"])
    rows = read(output / "runs.json")
    assert len(rows) == len(manifest["schedule"]) > 0, "Cannot pass an empty or incomplete research batch"
    assert [r["run_id"] for r in rows] == [c["run_id"] for c in manifest["schedule"]]
    results = []
    for row, cell in zip(rows, manifest["schedule"]):
        assert all(row[k] == cell[k] for k in ("run_id", "arm", "seed"))
        assert row["valid"] and row["budget_compliant"] and 0 <= row["end_to_end_seconds"] <= manifest["budget_seconds"]
        assert row["initial_routes_sha256"] == manifest["initial_routes_sha256"]
        assert row["source_sha256"] == manifest["source_archive"]["sha256"]
        actual_topology = {**manifest["topology"], "relay_allowed": row["arm"] not in DIRECT_ARMS}
        if row["arm"] in MOBILE_ARMS:
            physical_payload_audit, physical_routes_audit = (
                (audit_anticipatory_payload, audit_anticipatory_routes)
                if row["arm"] in ANTICIPATORY_ARMS else (audit_mobile_payload, audit_mobile_routes))
        preceding_key = None
        preceding_routes_hash = manifest["initial_routes_sha256"]
        proposal_references = []
        for phase in row["phases"]:
            assert sha(output / phase["solution_path"]) == phase["solution_sha256"]
            phase_payload = read(output / phase["solution_path"])
            phase_topology = {**phase.get("topology", actual_topology), "relay_allowed": actual_topology["relay_allowed"]}
            if row["arm"] not in MOBILE_ARMS:
                assert phase_topology == actual_topology, "Fixed-site experiment changed its frozen topology"
            reference = audit_payload(tasks, phase_topology, phase_payload)
            if row["arm"] in MOBILE_ARMS:
                physical_payload_audit(tasks, manifest["topology"], phase_topology, phase_payload)
            phase_routes = [r["encoded_visits"] for r in phase_payload["routes"]]
            assert digest(phase_routes) == phase["routes_sha256"]
            assert phase["input_routes_sha256"] == preceding_routes_hash
            preceding_routes_hash = phase["routes_sha256"]
            assert all(abs(reference["score"][k]-phase["score"][k]) < 1e-8 for k in FIELDS)
            if phase["phase"] == "initial":
                assert preceding_routes_hash == manifest["initial_routes_sha256"]
                assert all(abs(reference["score"][k]-manifest["initial_score"][k]) < 1e-8 for k in FIELDS)
            values = [reference["score"][k] for k in FIELDS]
            phase_key = (sum(w * v / s for w, v, s in zip(manifest["weights"], values, manifest["scales"])), *values)
            assert abs(phase_key[0] - phase["weighted_score"]) < 1e-10
            if preceding_key is not None:
                assert phase_key <= preceding_key, "Saved complete phase worsens its verified incumbent"
            preceding_key = phase_key
            metadata = phase.get("metadata", {})
            proposals = [(name, metadata.get(name)) for name in
                         ("first_complete_candidate", "best_constructed_candidate", "input_snapshot")]
            proposals.extend((f"full_candidate_{i}", snapshot) for i, snapshot in
                             enumerate(metadata.get("full_candidate_snapshots", [])))
            for cluster_record in metadata.get("cluster_records", []):
                for number, candidate_record in enumerate(cluster_record["complete_candidates"]):
                    for stage in ("full_proposal", "post_polish"):
                        proposals.append((f"cluster_{cluster_record['cluster_index']}__candidate_{number}__{stage}",
                                          candidate_record.get(stage)))
            for group_index, group_record in enumerate(metadata.get("group_records", [])):
                for number, candidate_record in enumerate(group_record["proposals"]):
                    for stage in ("full_proposal", "post_geometry", "post_polish"):
                        proposals.append((f"group_{group_index}__candidate_{number}__{stage}",
                                          candidate_record.get(stage)))
            for proposal_name, proposal in proposals:
                if proposal is None:
                    continue
                assert proposal["complete"] and proposal["valid"]
                proposal_topology = {**phase_topology, **proposal.get("topology", {}),
                                     "relay_allowed": phase_topology["relay_allowed"]}
                proposal_reference = reference_schedule(tasks, proposal["routes"], proposal_topology)
                if row["arm"] in MOBILE_ARMS:
                    mobile_reference = physical_routes_audit(tasks, manifest["topology"], proposal_topology, proposal["routes"])
                    from algorithm.experiments.audit_mobile_relay import _bind_meetings
                    _bind_meetings(proposal["mobile_meetings"], mobile_reference["meetings"])
                    if row["arm"] in ANTICIPATORY_ARMS:
                        assert all(m.get("meeting_type") == "receiver_arrives_first" for m in proposal["mobile_meetings"])
                assert all(abs(proposal_reference["score"][k]-proposal["score"][k]) < 1e-8 for k in FIELDS)
                proposal_values = [proposal_reference["score"][k] for k in FIELDS]
                expected_key = (sum(w*v/s for w,v,s in zip(manifest["weights"],proposal_values,manifest["scales"])), *proposal_values)
                assert all(abs(a-b)<1e-8 for a,b in zip(expected_key,proposal["key"]))
                assert proposal_reference["relay_statistics"]["cross_uav_handoff_count"] == proposal["cross_uav_count"]
                assert proposal_reference["relay_statistics"]["relay_task_count"] == proposal["relay_task_count"]
                if proposal_name == "input_snapshot":
                    assert digest(proposal["routes"]) == phase["input_routes_sha256"]
                proposal_references.append(dict(phase=phase["phase"], proposal=proposal_name,
                    routes_sha256=digest(proposal["routes"]), reference=proposal_reference))
        assert sha(output / row["solution_path"]) == row["solution_sha256"]
        final_payload = read(output / row["solution_path"])
        final_topology = {**row["phases"][-1].get("topology", actual_topology), "relay_allowed": actual_topology["relay_allowed"]}
        reference = audit_payload(tasks, final_topology, final_payload)
        physical_mobile = (physical_payload_audit(tasks, manifest["topology"], final_topology, final_payload)
                           if row["arm"] in MOBILE_ARMS else None)
        assert all(abs(reference["score"][k]-row[k]) < 1e-8 for k in FIELDS)
        assert abs(preceding_key[0] - row["weighted_score"]) < 1e-10
        assert row["on_time_count"] == len(tasks)-reference["score"]["late_count"]
        cf_topology = {**final_topology, "relay_allowed": False}
        assert sha(output / row["counterfactual_path"]) == row["counterfactual_sha256"]
        cf_payload = read(output / row["counterfactual_path"])
        final_routes = [route["encoded_visits"] for route in final_payload["routes"]]
        expected_cf = direct_counterfactual(final_routes, final_topology)
        assert [list(r) for r in expected_cf] == [r["encoded_visits"] for r in cf_payload["routes"]]
        cf = audit_payload(tasks, cf_topology, cf_payload)
        if row["arm"] in DIRECT_ARMS:
            assert reference["relay_statistics"]["relay_task_count"] == 0
        results.append(dict(run_id=row["run_id"], passed=True, reference=reference, fixed_structure_direct=cf,
                            non_adopted_or_constructed_proposals=proposal_references,
                            synchronous_mobile_physical_reference=physical_mobile))
    record = dict(passed=True, purpose=manifest["purpose"], source_manifest_sha256=sha(output / "manifest.json"),
                  source_runs_sha256=sha(output / "runs.json"), results=results)
    write(output / "audit.json", record)
    print(json.dumps({"audit_passed": True, "run_count": len(results), "purpose": manifest["purpose"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "worker", "audit"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--seed", type=int, default=2026101481)
    parser.add_argument("--budget-seconds", type=float, default=30)
    parser.add_argument("--warmup-fraction", type=float, default=.6)
    parser.add_argument("--homes", choices=("stations", "origin"), default="stations")
    parser.add_argument("--arms", default="direct,batch,reciprocal")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--method-parameters", type=json.loads, default={})
    args = parser.parse_args()
    if args.command == "prepare": prepare(args)
    elif args.command == "run": run(args.output_dir.resolve())
    elif args.command == "audit": audit(args.output_dir.resolve())
    else: worker(args.request)
