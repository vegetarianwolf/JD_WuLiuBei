"""LEGACY / NOT USED FOR CURRENT PAPER — depends on the old cooperative
wrapper (solve_dynamic_cooperative) and old dynamic-station / swap semantics.

Use ``run_common_start_benchmark.py`` / ``run_cooperative_halns_benchmark.py``
for the current paper.

B1-Dynamic-Coop-HALNS Benchmark — 5 seeds × 240s wall-clock.

Compares against A2 baseline using the formal 200-task instance.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from time import perf_counter

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT / "src"))

from uav_dispatch.alns import ALNSConfig, solve_alns_core
from uav_dispatch.cooperative_search import (
    CooperativeConfig,
    CooperativeResult,
    solve_dynamic_cooperative,
)
from uav_dispatch.cooperative_validation import CooperativeSolution
from uav_dispatch.dynamic_relay import DynamicRelayConfig
from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem

DATA_CSV = _PROJECT.parent / "data" / "raw" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
OUT_DIR = _PROJECT / "results" / "dynamic_cooperative"
RUN_DIR = OUT_DIR / "run_solutions"
SEEDS = (2026080500, 2026080501, 2026080502, 2026080503, 2026080504)
TIME_LIMIT = 240.0  # seconds wall-clock per seed


def run_a2_baseline(problem: Problem, seed: int, time_limit: float) -> dict:
    """Run standard A2 HALNS (three-layer) for comparison."""
    started = perf_counter()
    result = solve_alns_core(
        problem,
        config=ALNSConfig(
            max_iterations=100_000,
            time_limit_seconds=time_limit - 1.0,
            seed=seed,
        ),
    )
    elapsed = perf_counter() - started
    s = result.evaluation.score
    return {
        "seed": seed,
        "solver": "A2-Core",
        "late_count": s.late_count,
        "distance_km": s.distance_km,
        "total_lateness_min": result.evaluation.total_lateness_min,
        "runtime_s": round(elapsed, 3),
        "iterations": result.iterations,
    }


def run_new_solver(problem: Problem, seed: int, time_limit: float) -> dict:
    """Run B1-Dynamic-Coop-HALNS."""
    config = CooperativeConfig(
        seed=seed,
        time_limit_seconds=time_limit,
        candidate_limit=48,
        enable_swap=True,
        enable_batch_relay=True,
        enable_ownership_exchange=True,
        dynamic_relay=DynamicRelayConfig(
            min_active_stations=2,
            max_active_stations=8,
            station_refresh_interval=20,
            station_candidate_pool=20,
            station_elite_keep=2,
            station_drop_patience=80,
        ),
        verbose=True,
    )
    result = solve_dynamic_cooperative(problem, config=config)
    return {
        "seed": seed,
        "solver": "B1-Dynamic-Coop-HALNS",
        "late_count": result.validation.late_count,
        "distance_km": result.validation.distance_km,
        "total_lateness_min": result.validation.total_lateness_min,
        "runtime_s": round(result.runtime_seconds, 3),
        "iterations": result.iterations,
        "stage1_pickup_late_initial": result.stage1_pickup_late_initial,
        "stage1_pickup_late_final": result.stage1_pickup_late_final,
        "ownership_changes": result.ownership_changes,
        "pair_exchanges": result.pair_exchanges,
        "cycle_exchanges": result.cycle_exchanges,
        "relay_count": result.relay_count,
        "active_station_count": result.active_station_count,
        "station_adds": result.station_adds,
        "station_drops": result.station_drops,
        "station_replaces": result.station_replaces,
        "batch_relay_count": result.batch_relay_count,
        "swap_candidates": result.swap_candidates,
        "swap_accepted": result.swap_accepted,
        "final_swap_count": result.final_swap_count,
    }


def summarize(name: str, results: list[dict]) -> dict:
    lates = [r["late_count"] for r in results]
    dists = [r["distance_km"] for r in results]
    runtimes = [r["runtime_s"] for r in results]
    iters = [r["iterations"] for r in results]

    sorted_lates = sorted(lates)
    n = len(sorted_lates)
    median_late = sorted_lates[n // 2] if n > 0 else 0

    return {
        "solver": name,
        "seeds": len(results),
        "best_late_count": min(lates),
        "mean_late_count": sum(lates) / n,
        "median_late_count": median_late,
        "worst_late_count": max(lates),
        "std_late_count": (
            (sum((x - sum(lates) / n) ** 2 for x in lates) / n) ** 0.5
        ),
        "best_distance_km": min(dists),
        "mean_distance_km": sum(dists) / n,
        "mean_runtime_s": sum(runtimes) / n,
        "mean_iterations": sum(iters) / n,
    }


def main() -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("B1-Dynamic-Coop-HALNS Benchmark")
    print(f"Seeds: {SEEDS}  |  Time limit: {TIME_LIMIT}s per seed")
    print("=" * 70)

    tasks = load_tasks_csv(DATA_CSV)
    problem = Problem(
        tasks=tasks, drone_count=8, max_tasks_per_drone=25, capacity=2
    )
    print(f"\n{len(problem.tasks)} tasks, {problem.drone_count} drones, "
          f"capacity={problem.capacity}, max_tasks={problem.max_tasks_per_drone}")

    a2_results: list[dict] = []
    new_results: list[dict] = []

    for seed in SEEDS:
        print(f"\n{'─'*70}")
        print(f"Seed {seed}")
        print(f"{'─'*70}")

        # A2 baseline
        print("\n[A2 Baseline]")
        a2 = run_a2_baseline(problem, seed, TIME_LIMIT)
        a2_results.append(a2)
        print(f"  late={a2['late_count']}, dist={a2['distance_km']:.1f}, "
              f"time={a2['runtime_s']:.1f}s, iters={a2['iterations']}")

        # New solver
        print(f"\n[B1-Dynamic-Coop-HALNS]")
        new = run_new_solver(problem, seed, TIME_LIMIT)
        new_results.append(new)

        # Save per-seed
        seed_path = RUN_DIR / f"dynamic_coop_seed{seed}.json"
        seed_path.write_text(json.dumps(new, indent=2, ensure_ascii=False),
                             encoding="utf-8")

    # Summaries
    a2_summary = summarize("A2-Core", a2_results)
    new_summary = summarize("B1-Dynamic-Coop-HALNS", new_results)

    print(f"\n{'='*70}")
    print("RESULTS SUMMARY")
    print(f"{'='*70}")

    for summary in [a2_summary, new_summary]:
        print(f"\n--- {summary['solver']} ---")
        print(f"  late_count: best={summary['best_late_count']}, "
              f"mean={summary['mean_late_count']:.1f}, "
              f"median={summary['median_late_count']}, "
              f"worst={summary['worst_late_count']}, "
              f"std={summary['std_late_count']:.1f}")
        print(f"  distance_km: best={summary['best_distance_km']:.1f}, "
              f"mean={summary['mean_distance_km']:.1f}")
        print(f"  runtime: {summary['mean_runtime_s']:.1f}s avg, "
              f"iters: {summary['mean_iterations']:.0f} avg")

    # Per-seed comparison
    print(f"\n{'─'*70}")
    print("Per-Seed Comparison")
    print(f"{'─'*70}")
    print(f"{'Seed':<12} {'A2 late':>8} {'A2 dist':>8} {'New late':>8} "
          f"{'New dist':>8} {'Δ late':>8} {'Swap':>6}")
    for a2, new in zip(a2_results, new_results):
        delta = new["late_count"] - a2["late_count"]
        sw = new.get("best_swap_count", 0)
        print(f"{a2['seed']:<12} {a2['late_count']:>8} {a2['distance_km']:>8.1f} "
              f"{new['late_count']:>8} {new['distance_km']:>8.1f} "
              f"{delta:>+8} {sw:>6}")

    # Mechanism diagnosis
    print(f"\n{'─'*70}")
    print("Mechanism Diagnosis (New Solver)")
    print(f"{'─'*70}")
    for r in new_results:
        print(f"\nSeed {r['seed']}:")
        print(f"  S1 pickup-late: {r.get('stage1_pickup_late_initial','?')} "
              f"→ {r.get('stage1_pickup_late_final','?')}")
        print(f"  ownership changes: {r.get('ownership_reassignments',0)}")
        print(f"  pair exchanges: {r.get('pair_exchanges',0)}")
        print(f"  cycle exchanges: {r.get('cycle_exchanges',0)}")
        print(f"  relay count: {r.get('relay_count',0)}")
        print(f"  active stations: {r.get('active_station_count',0)}")
        print(f"  station adds/drops/replaces: "
              f"{r.get('station_adds',0)}/{r.get('station_drops',0)}/"
              f"{r.get('station_replaces',0)}")
        print(f"  batch relays: {r.get('batch_relay_count',0)}")
        print(f"  swap: accepted={r.get('accepted_swap_moves',0)}, "
              f"improving={r.get('improving_swap_moves',0)}, "
              f"best={r.get('best_swap_count',0)}")

    # Save summary
    summary_path = OUT_DIR / "benchmark_summary.json"
    summary_path.write_text(
        json.dumps(
            {"a2": a2_summary, "new": new_summary, "per_seed": new_results},
            indent=2, ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\nSummary saved to {summary_path}")


if __name__ == "__main__":
    main()
