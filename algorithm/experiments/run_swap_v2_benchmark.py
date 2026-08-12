"""LEGACY / NOT USED FOR CURRENT PAPER — benchmarks the legacy standalone
swap solver (swap_search).  Swap is not part of the formal Cooperative-HALNS
path; this file is kept only for the historical swap-v2 comparison.

Updated Swap benchmark with B.3 improvements (3 seeds × 240 s).

Compares against the already-run three-layer A2+PU baseline.
"""

from __future__ import annotations

import json, sys, time
from pathlib import Path

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT / "src"))

from uav_dispatch.alns import ALNSConfig
from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem
from uav_dispatch.swap_search import SwapConfig, solve_swap

DATA_CSV = _PROJECT.parent / "data" / "raw" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
OUT_DIR = _PROJECT / "results" / "swap_v2" / "run_solutions"
SEEDS = (2026080500, 2026080501, 2026080502)
TIME_LIMIT = 240.0

# Existing A2+PU baseline (from ablation)
A2_RESULTS = {
    2026080500: {"late": 74, "lateness": 2506.0, "dist": 666.90},
    2026080501: {"late": 75, "lateness": 2557.7, "dist": 657.16},
    2026080502: {"late": 74, "lateness": 2635.5, "dist": 676.02},
}


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 60)
    print("Swap v2 (B.3) Benchmark — 3 seeds × 240 s")
    print("=" * 60)

    tasks = load_tasks_csv(DATA_CSV)
    problem = Problem(tasks=tasks, drone_count=8, max_tasks_per_drone=25, capacity=2)
    print(f"\n{len(problem.tasks)} tasks, {problem.drone_count} drones")

    swap_results = []
    for seed in SEEDS:
        print(f"\n--- Swap seed {seed} ---")
        t0 = time.perf_counter()
        result = solve_swap(
            problem,
            alns_config=ALNSConfig(max_iterations=100_000, seed=seed),
            swap_config=SwapConfig(max_iterations=500),
            time_limit_seconds=TIME_LIMIT,
            swap_time_share=58.0,
            safety_margin_seconds=2.0,
        )
        elapsed = time.perf_counter() - t0
        s = result.evaluation.score

        print(f"  late={s.late_count}, lateness={s.total_lateness_min:.1f}, "
              f"dist={s.distance_km:.2f}, swaps={len(result.swaps)}, "
              f"iters={result.iterations}, time={elapsed:.1f}s")

        swap_results.append({
            "seed": seed,
            "late": s.late_count,
            "lateness": s.total_lateness_min,
            "dist": s.distance_km,
            "swap_count": len(result.swaps),
            "iterations": result.iterations,
            "runtime": round(elapsed, 1),
        })

        # Save
        payload = {
            "method": "C2-Lex-SWAP-v2",
            "seed": seed,
            "score": {
                "late_count": s.late_count,
                "total_lateness_min": s.total_lateness_min,
                "distance_km": s.distance_km,
            },
            "swap_count": len(result.swaps),
            "iterations": result.iterations,
            "runtime_s": round(elapsed, 1),
            "routes": [list(r) for r in result.routes],
            "swaps": [
                {
                    "id": e.id, "drone_a": e.drone_a, "drone_b": e.drone_b,
                    "task_a_to_b": e.task_a_to_b, "task_b_to_a": e.task_b_to_a,
                    "meeting_node": e.meeting_node,
                    "position_a": e.position_a, "position_b": e.position_b,
                }
                for e in result.swaps
            ],
        }
        fname = f"swap_v2__seed_{seed}.json"
        (OUT_DIR / fname).write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    # ---- Comparison ----
    print("\n" + "=" * 60)
    print("Swap v2 vs A2+PU (baseline)")
    print("=" * 60)
    print(f"{'Metric':<25s} {'A2+PU':>10s} {'Swap v2':>10s} {'Delta':>10s}")
    print("-" * 55)

    avg_a2_late = sum(r["late"] for r in A2_RESULTS.values()) / 3
    avg_swap_late = sum(r["late"] for r in swap_results) / 3
    for name, a2_key, swap_key in [
        ("late_count", "late", "late"),
        ("total_lateness_min", "lateness", "lateness"),
        ("distance_km", "dist", "dist"),
    ]:
        a2_avg = sum(A2_RESULTS[s][a2_key] for s in SEEDS) / 3
        sw_avg = sum(r[swap_key] for r in swap_results) / 3
        d = sw_avg - a2_avg
        sign = "+" if d > 0 else ""
        print(f"  {name:<23s} {a2_avg:>10.1f} {sw_avg:>10.1f} {sign}{d:>9.1f}")

    print(f"\n  {'swap_count':<23s} {'0':>10} {sum(r['swap_count'] for r in swap_results)/3:>10.1f}")

    # Save summary
    summary = {
        "a2_baseline": {str(s): A2_RESULTS[s] for s in SEEDS},
        "swap_v2": {str(r["seed"]): r for r in swap_results},
    }
    (OUT_DIR.parent / "swap_vs_a2_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n→ {OUT_DIR.parent}")
    print("Done.")


if __name__ == "__main__":
    main()
