"""Read-only aggregation of the preceding frozen relay experiment.

Run from any working directory. No solver is called and no old artifact is
modified. All aggregate values derive from the listed immutable inputs.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from statistics import mean


ROOT = Path(__file__).resolve().parents[4]
SOURCE = ROOT / "algorithm/results/global_relay_trials_20261014"
OUTPUT = Path(__file__).with_name("relay_phase_diagnosis.json")


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def main():
    runs_path = SOURCE / "runs.json"
    sources = {str(runs_path.relative_to(ROOT)): digest(runs_path)}
    all_rows = json.loads(runs_path.read_text())
    records = []
    direct_references = []
    for row in all_rows:
        if row["stage"] != "screen":
            continue
        solution_path = SOURCE / row["solution_file"]
        sources[str(solution_path.relative_to(ROOT))] = digest(solution_path)
        solution = json.loads(solution_path.read_text())
        metadata = solution["metadata"]
        final = [row[key] for key in ("late_count", "total_lateness_min", "distance_km")]
        assert final == [solution["score"][key] for key in ("late_count", "total_lateness_min", "distance_km")]
        if "relay" not in row["scenario"]:
            direct_references.append({
                "run_id": row["run_id"], "scenario": row["scenario"],
                "variant": row["variant"], "seed": row["seed"],
                "score": final, "construction_seconds": row["construction_seconds"],
                "search_seconds": row["search_seconds"], "iterations": row["iterations"],
            })
            continue
        warm_score = metadata["relay_warmup_score"]
        warm_runtime = metadata["relay_warmup_runtime_seconds"]
        warm_iterations = metadata["relay_warmup_iterations"]
        startup_points = [point for point in metadata["best_trajectory"]
                          if point[0] == warm_iterations and point[1] > warm_runtime]
        # A point at the warmup iteration count is the relay startup's
        # aggregate best update, before any relay ALNS iteration completes.
        # It is a recorded update time, not independent seed/refine timing.
        startup_elapsed = [point[1] - warm_runtime for point in startup_points]
        relay = solution["relay"]
        record = {
            "run_id": row["run_id"], "scenario": row["scenario"],
            "variant": row["variant"], "seed": row["seed"],
            "budget_seconds": row["budget_seconds"],
            "construction_seconds": row["construction_seconds"],
            "warmup_runtime_seconds": warm_runtime,
            "relay_runtime_seconds": metadata["relay_search_runtime_seconds"],
            "warmup_iterations": warm_iterations,
            "relay_iterations": metadata["relay_search_iterations"],
            "warmup_score": warm_score, "final_score": final,
            "score_delta_final_minus_warmup": [after - before for after, before in zip(final, warm_score)],
            "relay_phase_best_update_count": metadata["relay_phase_best_update_count"],
            "startup_best_update_elapsed_seconds": startup_elapsed,
            "startup_best_update_score": [point[2:] for point in startup_points],
            "later_relay_best_update_count": sum(point[0] > warm_iterations for point in metadata["best_trajectory"]),
            "seed_tasks_considered": metadata["relay_seed_tasks_considered"],
            "seed_improvements": metadata["relay_seed_improvements"],
            "seed_deadline_reached": metadata["relay_seed_deadline_reached"],
            "refine_candidates_evaluated": metadata["refine_candidates_evaluated"],
            "refine_candidates_pruned": metadata["refine_candidates_pruned"],
            "relay_task_count": relay["relay_task_count"],
            "cross_uav_handoff_count": relay["cross_uav_handoff_count"],
            "same_uav_relay_count": relay["same_uav_relay_count"],
            "total_relay_waiting_min": relay["total_relay_waiting_time"],
            "average_relay_waiting_min": relay["average_relay_waiting_time"],
            "max_relay_waiting_min": relay["max_relay_waiting_time"],
        }
        records.append(record)

    by_variant = {}
    for variant in sorted({record["variant"] for record in records}):
        selected = [record for record in records if record["variant"] == variant]
        starts = [value for record in selected for value in record["startup_best_update_elapsed_seconds"]]
        by_variant[variant] = {
            "run_count": len(selected),
            "mean_construction_seconds": mean(record["construction_seconds"] for record in selected),
            "total_relay_runtime_seconds": sum(record["relay_runtime_seconds"] for record in selected),
            "total_relay_iterations": sum(record["relay_iterations"] for record in selected),
            "mean_startup_best_update_elapsed_seconds": mean(starts),
            "min_startup_best_update_elapsed_seconds": min(starts),
            "max_startup_best_update_elapsed_seconds": max(starts),
            "relay_phase_primary_improved_runs": sum(record["final_score"][0] < record["warmup_score"][0] for record in selected),
            "startup_only_best_update_runs": sum(record["later_relay_best_update_count"] == 0 for record in selected),
            "total_later_relay_best_updates": sum(record["later_relay_best_update_count"] for record in selected),
            "total_refine_candidates_evaluated": sum(record["refine_candidates_evaluated"] for record in selected),
            "total_relay_tasks": sum(record["relay_task_count"] for record in selected),
            "total_cross_uav_handoffs": sum(record["cross_uav_handoff_count"] for record in selected),
            "total_same_uav_relays": sum(record["same_uav_relay_count"] for record in selected),
            "total_relay_waiting_min": sum(record["total_relay_waiting_min"] for record in selected),
            "sum_lateness_reduction_min": sum(record["warmup_score"][1] - record["final_score"][1] for record in selected),
        }
    starts = [value for record in records for value in record["startup_best_update_elapsed_seconds"]]
    result = {
        "kind": "read_only_relay_phase_diagnosis", "source_batch_run_count": len(all_rows),
        "relay_screen_run_count": len(records), "source_sha256": sources,
        "records": records, "by_variant": by_variant, "direct_references": direct_references,
        "aggregate_facts": {
            "primary_improved_runs": sum(record["final_score"][0] < record["warmup_score"][0] for record in records),
            "startup_only_best_update_runs": sum(record["later_relay_best_update_count"] == 0 for record in records),
            "all_seed_scans_completed_200": all(record["seed_tasks_considered"] == 200 and not record["seed_deadline_reached"] for record in records),
            "all_final_relay_counts_equal_seed_improvement_counts": all(record["relay_task_count"] == record["seed_improvements"] for record in records),
            "startup_best_update_elapsed_range_seconds": [min(starts), max(starts)],
            "downstream_vs_global_refine_evaluation_ratio": by_variant["downstream"]["total_refine_candidates_evaluated"] / by_variant["global"]["total_refine_candidates_evaluated"],
        },
        "limitations": [
            "No new solving or timing was performed; source phases used 20-second end-to-end budgets and 80% DIRECT warmup.",
            "Startup update time combines seeding, startup refine, and validation; separate component durations were not logged.",
            "Cumulative refine counts include startup and later iterations; no per-candidate success or wall-clock timing is available.",
            "Equal final relay counts and seed-improvement counts do not establish that every final relay is the same seeded task.",
            "Final waiting is a final-schedule measure and does not bound waiting in rejected intermediate candidates.",
            "Variants diverge in their DIRECT warmup; differences in phase gains or cumulative work are not isolated causal estimates.",
            "A shorter relay stage is a hypothesis for new paired testing; it does not establish improved scores before testing.",
        ],
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(OUTPUT), "aggregate_facts": result["aggregate_facts"], "by_variant": by_variant}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
