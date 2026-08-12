"""LEGACY / NOT USED FOR CURRENT PAPER — validates the legacy standalone
relay/swap solvers (B.2 time-feasibility + B.3 swap).  Swap is not part of
the formal Cooperative-HALNS path.

Quick validation: B.2 (time feasibility) + B.3 (swap) on relay/swap solvers.

Runs 60 s per variant to check that the new code doesn't regress and
measure the effect of the time-feasibility lower bound.
"""

from __future__ import annotations

import json, sys, time
from pathlib import Path

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT / "src"))

from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem
from uav_dispatch.alns import ALNSConfig, solve_alns_core
from uav_dispatch.swap_search import SwapConfig, solve_swap

DATA_CSV = _PROJECT.parent / "data" / "raw" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"


def run_swap_quick(problem: Problem, label: str, time_s: float = 60.0) -> dict:
    t0 = time.perf_counter()
    result = solve_swap(
        problem,
        alns_config=ALNSConfig(max_iterations=100_000, time_limit_seconds=time_s * 0.7),
        swap_config=SwapConfig(max_iterations=500, time_limit_seconds=time_s * 0.25),
        time_limit_seconds=time_s,
        swap_time_share=time_s * 0.25,
        safety_margin_seconds=2.0,
    )
    elapsed = time.perf_counter() - t0
    s = result.evaluation.score
    return {
        "label": label,
        "late": s.late_count,
        "lateness": s.total_lateness_min,
        "dist": s.distance_km,
        "swap_count": len(result.swaps),
        "iterations": result.iterations,
        "runtime": round(elapsed, 1),
    }


def main():
    print("=" * 50)
    print("B.2/B.3 Quick Validation (60 s each)")
    print("=" * 50)

    tasks = load_tasks_csv(DATA_CSV)
    problem = Problem(tasks=tasks, drone_count=8, max_tasks_per_drone=25, capacity=2)
    print(f"{len(problem.tasks)} tasks, {problem.drone_count} drones\n")

    results = []

    # --- Swap: new three-layer + no-greedy-break ---
    print("--- Swap (B.3: no greedy break, 3-layer SA) ---")
    r = run_swap_quick(problem, "swap_v2", 60.0)
    results.append(r)
    print(f"  late={r['late']}, lateness={r['lateness']:.1f}, dist={r['dist']:.1f}, "
          f"swaps={r['swap_count']}, iters={r['iterations']}, time={r['runtime']}s")

    # --- A2 baseline for comparison ---
    print("\n--- A2 baseline (same budget) ---")
    t0 = time.perf_counter()
    a2 = solve_alns_core(problem, config=ALNSConfig(
        max_iterations=100_000, time_limit_seconds=58.0, seed=2026080500,
    ))
    elapsed = time.perf_counter() - t0
    s = a2.evaluation.score
    r_a2 = {"label": "a2_baseline", "late": s.late_count, "lateness": s.total_lateness_min,
            "dist": s.distance_km, "swap_count": 0, "iterations": a2.iterations,
            "runtime": round(elapsed, 1)}
    results.append(r_a2)
    print(f"  late={r_a2['late']}, lateness={r_a2['lateness']:.1f}, dist={r_a2['dist']:.1f}, "
          f"iters={r_a2['iterations']}, time={r_a2['runtime']}s")

    # Summary
    print("\n" + "-" * 40)
    for r in results:
        print(f"  {r['label']:<20s} late={r['late']:>3}  lateness={r['lateness']:>8.1f}  "
              f"dist={r['dist']:>7.1f}  swaps={r['swap_count']:>2}  iters={r['iterations']:>4}")

    # Save
    out_dir = _PROJECT / "results" / "b23_validation"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n→ {out_dir / 'results.json'}")
    print("Done.")


if __name__ == "__main__":
    main()
