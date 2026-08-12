"""Three-layer A2 benchmark — 3 seeds × 240 s wall-clock.

Saves solutions under ``algorithm/results/three_layer_a2/``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from time import perf_counter

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT / "src"))

from uav_dispatch.alns import ALNSConfig, solve_alns_core
from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem

DATA_CSV = _PROJECT.parent / "data" / "raw" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
OUT_DIR = _PROJECT / "results" / "three_layer_a2" / "run_solutions"
SEEDS = (2026080500, 2026080501, 2026080502)
TIME_LIMIT = 238.0  # ~240 s wall-clock allowing ~2 s for construction


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Three-layer A2 Benchmark — 3 seeds × 240 s")
    print("=" * 60)

    tasks = load_tasks_csv(DATA_CSV)
    problem = Problem(tasks=tasks, drone_count=8, max_tasks_per_drone=25, capacity=2)
    print(f"\n{len(problem.tasks)} tasks, {problem.drone_count} drones")

    results: list[dict] = []

    for seed in SEEDS:
        print(f"\n--- seed {seed} ---")
        started = perf_counter()
        result = solve_alns_core(
            problem,
            config=ALNSConfig(
                max_iterations=100_000,
                time_limit_seconds=TIME_LIMIT,
                seed=seed,
            ),
        )
        elapsed = perf_counter() - started
        s = result.evaluation.score

        # Save minimal JSON
        payload = {
            "seed": seed,
            "valid": result.evaluation.valid,
            "score": {
                "late_count": s.late_count,
                "total_lateness_min": s.total_lateness_min,
                "distance_km": s.distance_km,
            },
            "on_time_rate": result.evaluation.on_time_rate,
            "runtime_seconds": round(elapsed, 3),
            "iterations": result.iterations,
            "method": "C2-Lex-ALNS-Core-ThreeLayer",
            "routes": [list(r) for r in result.routes],
        }

        fname = f"equal_wall_clock_240s__a2_three_layer__seed_{seed}.json"
        out_path = OUT_DIR / fname
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                            encoding="utf-8")

        s = result.evaluation.score
        print(f"  late_count={s.late_count}, "
              f"total_lateness={s.total_lateness_min:.1f}, "
              f"distance={s.distance_km:.2f}, "
              f"iters={result.iterations}, "
              f"time={elapsed:.1f}s")
        results.append({
            "seed": seed,
            "late_count": s.late_count,
            "total_lateness_min": s.total_lateness_min,
            "distance_km": s.distance_km,
            "iterations": result.iterations,
            "runtime_s": round(elapsed, 3),
        })

    # Summary
    print("\n" + "=" * 60)
    print("Summary")
    print("=" * 60)
    lates = [r["late_count"] for r in results]
    lats = [r["total_lateness_min"] for r in results]
    dists = [r["distance_km"] for r in results]
    iters = [r["iterations"] for r in results]
    print(f"  late_count:        {sum(lates)/3:.1f} avg  ({min(lates)}–{max(lates)})")
    print(f"  total_lateness_min: {sum(lats)/3:.1f} avg")
    print(f"  distance_km:        {sum(dists)/3:.2f} avg")
    print(f"  iterations:         {sum(iters)/3:.0f} avg")
    print(f"\n  → {OUT_DIR}")

    # Save summary
    with (OUT_DIR.parent / "summary.json").open("w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)

    print("Done.")


if __name__ == "__main__":
    main()
