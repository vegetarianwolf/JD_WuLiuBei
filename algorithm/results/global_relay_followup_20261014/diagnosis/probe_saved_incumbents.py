"""Bounded post-hoc local-neighborhood probe, not a paired solver benchmark."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter
from zipfile import ZipFile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SOURCE = ROOT / "algorithm/results/global_relay_trials_20261014"
sys.path[:0] = [str(ROOT / "algorithm/src"), str(ROOT)]

from algorithm.experiments.audit_optimization_results import _case_problem
from uav_dispatch.io import load_tasks_csv
from uav_dispatch.home_assignment import reassign_route_homes
from uav_dispatch.route_exchange import bounded_tail_exchange
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.validation import evaluate_solution


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scores(evaluation):
    return {k: getattr(evaluation.score, k) for k in ("late_count", "total_lateness_min", "distance_km")}


def main():
    manifest = read(SOURCE / "manifest.json")
    if sha(SOURCE / "runs.json") != read(SOURCE / "validation.json")["runs_sha256"]:
        raise ValueError("Historical results changed")
    input_path = ROOT / manifest["input_path"]
    if sha(input_path) != manifest["input_sha256"]:
        raise ValueError("Official input changed")
    bindings = {str(input_path.relative_to(ROOT)): sha(input_path),
                str((SOURCE / "runs.json").relative_to(ROOT)): sha(SOURCE / "runs.json")}
    archive = SOURCE / manifest["source_snapshots"]["global"]["path"]
    if sha(archive) != manifest["source_snapshots"]["global"]["sha256"]:
        raise ValueError("Historical candidate archive changed")
    with ZipFile(archive) as frozen:
        for name in ("route_exchange.py", "home_assignment.py", "io.py", "model.py", "search.py", "validation.py", "relay.py"):
            path = ROOT / "algorithm/src/uav_dispatch" / name
            if path.read_bytes() != frozen.read("uav_dispatch/" + name):
                raise ValueError(f"Probe source differs from measured module: {name}")
            bindings[str(path.relative_to(ROOT))] = sha(path)
    tasks = load_tasks_csv(input_path)
    results = []
    for row in read(SOURCE / "runs.json"):
        if row["stage"] != "screen" or row["variant"] != "baseline":
            continue
        path = SOURCE / row["solution_file"]
        if sha(path) != row["solution_sha256"]:
            raise ValueError("Historical solution changed")
        bindings[str(path.relative_to(ROOT))] = sha(path)
        payload = read(path)
        record = {"run_id": row["run_id"], "case_id": row["case_id"], "seed": row["seed"],
                  "source_solution": str(path.relative_to(ROOT)), "source_solution_sha256": sha(path)}
        if payload["relay"]["relay_task_count"]:
            record.update(status="skipped", reason="Final routes contain relay legs; complete DIRECT warmup routes were not persisted, only their score.")
            results.append(record)
            continue
        problem = _case_problem(row, tasks)
        routes = tuple(tuple(r["encoded_visits"]) for r in payload["routes"])
        if any(abs(v) in problem.leg_registry for route in routes for v in route):
            raise ValueError("Probe requires completely DIRECT routes")
        started = perf_counter()
        deadline = started + .5
        before = evaluate_solution(problem, routes, _force_full=True)
        if not before.valid or scores(before) != payload["score"]:
            raise ValueError("Historical baseline does not reproduce")
        evaluator = RouteEvaluator(problem)
        tail, home = {}, {}
        changed = bounded_tail_exchange(problem, evaluator, routes, deadline=deadline,
                                        max_pairs=28, statistics=tail)
        after_tail = evaluate_solution(problem, changed, _force_full=True)
        changed = reassign_route_homes(problem, evaluator, changed, deadline=deadline, statistics=home)
        after = evaluate_solution(problem, changed, _force_full=True)
        elapsed = perf_counter() - started
        if not after.valid or after.score > before.score:
            raise ValueError("Probe produced invalid or worse canonical score")
        record.update(status="complete", budget_seconds=.5, elapsed_seconds=elapsed,
                      within_budget=elapsed <= .5, before=scores(before), after_tail=scores(after_tail),
                      after=scores(after), improved=after.score < before.score,
                      late_count_improved=after.score.late_count < before.score.late_count,
                      statistics={"tail": tail, "home": home}, routes=[list(r) for r in changed])
        results.append(record)
    completed = [r for r in results if r["status"] == "complete"]
    report = {"kind": "saved_incumbent_mechanism_probe", "schema_version": 1,
              "method": "One existing 28-pair equal-tail pass, then one existing exact home-assignment call; 0.5-second per-snapshot cap including complete before/after scoring.",
              "limitations": ["Post-hoc mechanism probe on already optimized historical final routes; not a new end-to-end ALNS run or a same-budget paired benchmark.",
                              "Six Relay final solutions cannot substitute for their unavailable complete DIRECT warmup routes; no scores or routes are invented for skipped cases.",
                              "This probe order is tail then home; the proposed combined wrapper may use a different predeclared bounded order and requires its own tests and paired experiment.",
                              "The 0.5-second cap is extra diagnostic time, so its gains cannot be attributed to the old 20-second budgets."],
              "source_bindings": bindings, "candidate_archive_sha256": sha(archive), "script_sha256": sha(Path(__file__)),
              "results": results, "summary": {"eligible_baselines": len(results), "completed": len(completed),
                  "skipped": len(results) - len(completed), "improved": sum(r["improved"] for r in completed),
                  "late_count_improved": sum(r["late_count_improved"] for r in completed),
                  "total_probe_seconds": sum(r["elapsed_seconds"] for r in completed),
                  "all_within_budget": all(r["within_budget"] for r in completed)}}
    (HERE / "saved_incumbent_probe.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "runs": [{k: v for k, v in r.items() if k not in {"routes", "source_solution_sha256"}} for r in results]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
