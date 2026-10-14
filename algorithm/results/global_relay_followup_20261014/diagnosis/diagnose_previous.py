"""Read-only diagnosis of the completed preceding batch; never imports a solver.

Run from any working directory with Python 3.11+. Old records are hash-checked
and read, and only this new diagnosis directory receives an output file.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import statistics
from zipfile import ZipFile


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
SOURCE = ROOT / "algorithm/results/global_relay_trials_20261014"
SCORE_FIELDS = ("late_count", "total_lateness_min", "distance_km")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def score(row):
    return tuple(row[field] for field in SCORE_FIELDS)


def at(points, value, axis):
    choices = [point for point in points if point[axis] <= value]
    return choices[-1][2:] if choices else None


def summarize_run(row, payload):
    metadata = payload["metadata"]
    points = row["best_trajectory"]
    late_changes = [point for index, point in enumerate(points) if index == 0 or point[2] < points[index - 1][2]]
    direct_iterations = metadata.get("relay_warmup_iterations", row["iterations"])
    direct_seconds = metadata.get("relay_warmup_runtime_seconds", payload["runtime_seconds"])
    tail = metadata.get("tail_exchange_statistics", {})
    home = metadata.get("home_reassignment_statistics", {})
    warm = metadata.get("relay_warmup_score")
    relay_score = metadata.get("relay_search_final_score")
    return {
        "run_id": row["run_id"], "case_id": row["case_id"], "variant": row["variant"], "seed": row["seed"],
        "solution_file": row["solution_file"], "solution_sha256": row["solution_sha256"],
        "score": list(score(row)), "initial_score": points[0][2:], "iterations": row["iterations"],
        "search_seconds": row["search_seconds"], "iterations_per_search_second": row["iterations"] / row["search_seconds"],
        "direct_iterations": direct_iterations, "direct_seconds": direct_seconds,
        "direct_iterations_per_second": direct_iterations / direct_seconds,
        "relay_iterations": metadata.get("relay_search_iterations", 0),
        "relay_seconds": metadata.get("relay_search_runtime_seconds", 0.),
        "tail_exchange": tail, "home_reassignment": home,
        "tail_improvements_per_100_probes": 100 * tail.get("improvements", 0) / tail["probes"] if tail.get("probes") else None,
        "home_improvements_per_100_passes": 100 * home.get("improvements", 0) / home["passes"] if home.get("passes") else None,
        "home_canonical_rejections": home.get("canonical_rejections", 0),
        "best_update_count": len(points) - 1, "last_best_at_seconds": points[-1][1],
        "last_late_count_improvement_at_seconds": late_changes[-1][1],
        "seconds_after_last_late_count_improvement": row["end_to_end_seconds"] - late_changes[-1][1],
        "time_checkpoints": {str(t): at(points, t, 1) for t in (2, 5, 10, 15, 18)},
        "iteration_checkpoints": {str(i): at(points, i, 0) for i in (20, 40, 100, 400, 800, 1000)},
        "relay_warmup_score": warm, "relay_final_score": relay_score,
        "relay_phase_score_delta": [b - a for a, b in zip(warm, relay_score)] if warm is not None else None,
        "relay_task_count": payload["relay"]["relay_task_count"],
        "relay_phase_best_update_count": metadata.get("relay_phase_best_update_count", 0),
        "relay_seed_improvements": metadata.get("relay_seed_improvements", 0),
        "refine_candidates_evaluated": metadata.get("refine_candidates_evaluated", 0),
        "route_full_evaluations": metadata.get("profiling", {}).get("route_full_evaluation_count"),
        "first_seed_environment_overlap_possible": row["seed"] == 2026102411,
    }


def compare(left, right, left_raw, right_raw):
    common_iter = min(left["direct_iterations"], right["direct_iterations"])
    left_at = at(left_raw["best_trajectory"], common_iter, 0)
    right_at = at(right_raw["best_trajectory"], common_iter, 0)
    outcome = "win" if tuple(right["score"]) < tuple(left["score"]) else "loss" if tuple(right["score"]) > tuple(left["score"]) else "tie"
    # These are incumbent trajectory observations, not an identical search path.
    prefix = 0
    for a, b in zip(left_raw["best_trajectory"], right_raw["best_trajectory"]):
        if [a[0], *a[2:]] != [b[0], *b[2:]]:
            break
        prefix += 1
    return {
        "case_id": left["case_id"], "seed": left["seed"], "reference": left["variant"], "candidate": right["variant"],
        "outcome": outcome, "reference_score": left["score"], "candidate_score": right["score"],
        "score_delta": [b - a for a, b in zip(left["score"], right["score"])],
        "total_iteration_ratio": right["iterations"] / left["iterations"],
        "direct_iteration_ratio": right["direct_iterations"] / left["direct_iterations"],
        "direct_throughput_ratio": right["direct_iterations_per_second"] / left["direct_iterations_per_second"],
        "common_direct_iteration": common_iter, "reference_incumbent_at_common_iteration": left_at,
        "candidate_incumbent_at_common_iteration": right_at,
        "common_iteration_outcome": "win" if tuple(right_at) < tuple(left_at) else "loss" if tuple(right_at) > tuple(left_at) else "tie",
        "shared_best_trajectory_prefix_entries": prefix,
        "last_shared_best_iteration": left_raw["best_trajectory"][prefix - 1][0] if prefix else None,
        "candidate_tail_improvements": right["tail_exchange"].get("improvements", 0),
        "candidate_tail_probes": right["tail_exchange"].get("probes", 0),
        "candidate_home_passes": right["home_reassignment"].get("passes", 0),
        "candidate_home_improvements": right["home_reassignment"].get("improvements", 0),
        "candidate_home_canonical_rejections": right["home_reassignment"].get("canonical_rejections", 0),
        "first_seed_environment_overlap_possible": left["first_seed_environment_overlap_possible"],
    }


def aggregate_pairs(pairs):
    return {"pairs": len(pairs), "wins": sum(p["outcome"] == "win" for p in pairs),
            "ties": sum(p["outcome"] == "tie" for p in pairs), "losses": sum(p["outcome"] == "loss" for p in pairs),
            "late_count_delta_sum": sum(p["score_delta"][0] for p in pairs),
            "direct_throughput_ratio_median": statistics.median(p["direct_throughput_ratio"] for p in pairs),
            "direct_throughput_ratio_range": [min(p["direct_throughput_ratio"] for p in pairs), max(p["direct_throughput_ratio"] for p in pairs)],
            "candidate_tail_improvements": sum(p["candidate_tail_improvements"] for p in pairs),
            "candidate_tail_probes": sum(p["candidate_tail_probes"] for p in pairs),
            "candidate_home_passes": sum(p["candidate_home_passes"] for p in pairs),
            "candidate_home_improvements": sum(p["candidate_home_improvements"] for p in pairs),
            "candidate_home_canonical_rejections": sum(p["candidate_home_canonical_rejections"] for p in pairs)}


def main():
    manifest, validation = read(SOURCE / "manifest.json"), read(SOURCE / "validation.json")
    bindings = {}
    for name in ("manifest", "runs", "decisions"):
        path = SOURCE / f"{name}.json"
        bindings[path.relative_to(ROOT).as_posix()] = sha(path)
        if sha(path) != validation[f"{name}_sha256"]:
            raise ValueError(f"Old {name} evidence changed")
    for name in ("validation.json", "environment_notes.json", "final_source_delta.json"):
        bindings[(SOURCE / name).relative_to(ROOT).as_posix()] = sha(SOURCE / name)
    if not validation["passed"] or not validation["protocol_complete"]:
        raise ValueError("Diagnosis requires the complete validated historical batch")
    source_facts = {}
    for variant, spec in manifest["source_snapshots"].items():
        path = SOURCE / spec["path"]
        if sha(path) != spec["sha256"]:
            raise ValueError("Solver source changed")
        bindings[path.relative_to(ROOT).as_posix()] = sha(path)
        with ZipFile(path) as archive:
            text = archive.read("uav_dispatch/alns.py").decode()
        source_facts[variant] = {"sha256": sha(path), "config": spec["config"],
            "periodic_tail_on_repaired_candidate": "if tail_exchange_active and iteration % 20 == 0:" in text,
            "periodic_home_on_repaired_candidate": "if home_reassignment_active and iteration % 40 == 0:" in text,
            "canonical_best_check_after_moves": "candidate_evaluation = evaluate_solution(" in text,
            "operator_rewards_shared_with_destroy_repair": 'segment_rewards[key] += reward' in text}
    runs, raw = [], {}
    for row in read(SOURCE / "runs.json"):
        path = SOURCE / row["solution_file"]
        if sha(path) != row["solution_sha256"]:
            raise ValueError("Raw solution changed")
        bindings[path.relative_to(ROOT).as_posix()] = sha(path)
        if row["stage"] != "screen":
            continue
        runs.append(summarize_run(row, read(path)))
        raw[row["run_id"]] = row
    index = {(r["case_id"], r["variant"], r["seed"]): r for r in runs}
    pairs, relay_pairs = [], []
    for candidate in runs:
        if candidate["variant"] == "baseline":
            continue
        reference_variant = "baseline" if candidate["variant"] == "global" else "global"
        reference = index[candidate["case_id"], reference_variant, candidate["seed"]]
        pair = compare(reference, candidate, raw[reference["run_id"]], raw[candidate["run_id"]])
        (pairs if candidate["variant"] == "global" else relay_pairs).append(pair)
    cases = manifest["protocol"]["cases"]
    relay_runs = [r for r in runs if r["relay_warmup_score"] is not None]
    report = {
        "kind": "read_only_previous_global_relay_diagnosis", "schema_version": 1,
        "source_directory": SOURCE.relative_to(ROOT).as_posix(), "script_sha256": sha(Path(__file__)),
        "input_bindings": bindings, "no_new_searches": True,
        "source_facts": source_facts, "runs": runs, "global_vs_baseline": pairs, "downstream_vs_global": relay_pairs,
        "global_summary": aggregate_pairs(pairs),
        "global_by_case": {c: aggregate_pairs([p for p in pairs if p["case_id"] == c]) for c in cases},
        "global_excluding_first_seed": aggregate_pairs([p for p in pairs if p["seed"] != 2026102411]),
        "global_by_outcome": {o: aggregate_pairs([p for p in pairs if p["outcome"] == o]) for o in ("win", "loss")},
        "downstream_summary": aggregate_pairs(relay_pairs),
        "relay_phase": {
            "run_count": len(relay_runs),
            "runs_improving_late_count_after_direct_warmup": sum(r["relay_phase_score_delta"][0] < 0 for r in relay_runs),
            "runs_improving_secondary_score_after_direct_warmup": sum(tuple(r["relay_final_score"]) < tuple(r["relay_warmup_score"]) for r in relay_runs),
            "refine_probes_by_variant": {v: sum(r["refine_candidates_evaluated"] for r in relay_runs if r["variant"] == v) for v in ("baseline", "global", "downstream")},
            "late_count_changes": [r["relay_phase_score_delta"][0] for r in relay_runs],
        },
        "evidence": [
            "The frozen source polishes repaired candidates every 20/40 iterations before acceptance; operator rewards then credit the selected destroy/repair pair.",
            "Improvement counters compare each polishing move to its immediate candidate, not to the incumbent and not to the final returned solution.",
            "There is no recorded timing per global operator or timestamp linking each local improvement to later acceptance; causal overhead and marginal payoff cannot be recovered.",
            "All 18 Relay-stage runs retained the DIRECT warmup late-task count; Relay added secondary-objective gains only in this short budget.",
            "The first seed has documented possible CPU contention. Throughput is descriptive observation, never a pure implementation-speed estimate.",
        ],
        "hypotheses": [
            "Repeated improvement of repaired, sometimes rejected candidates may spend effort on solutions that never become incumbent.",
            "Periodic composite moves change the current trajectory and destroy/repair reward attribution; a local improvement does not guarantee a better final basin.",
            "The home-assignment DP is often run after useful home assignments have already been recovered, so sparse or incumbent-only triggering may avoid low-yield passes.",
        ],
        "recommended_single_followup": {
            "name": "Deferred incumbent-only composite polishing",
            "design": "Keep the original ALNS loop and its operator adaptation unchanged; reserve a small bounded final slice in each DIRECT search phase, apply one home assignment and one complete 28-pair equal-tail pass to the canonical incumbent, then stop unless the predeclared cap permits another strictly improving pass. Relay uses the refined DIRECT warmup incumbent before its unchanged relay phase.",
            "reason": "This reuses already-validated cheap feasible moves across all four strategies while removing repeated candidate polishing and its reward/trajectory coupling.",
            "controls": "Old periodic flags and downstream experiment off; same input, initial solution, seeds and total wall-clock budget; include reserve and validation in budget; strict full-score acceptance; complete all four paired scenarios before interpretation.",
            "limitation": "A reserved slice removes time from baseline search, so even monotone polishing cannot guarantee a same-budget win. No outcome has been measured for this proposal.",
        },
    }
    target = HERE / "previous_batch_diagnosis.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"global": report["global_summary"], "excluding_first_seed": report["global_excluding_first_seed"],
                      "outcomes": report["global_by_outcome"], "relay_phase": report["relay_phase"],
                      "output": str(target)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
