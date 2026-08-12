"""Run task-level physical-feasibility diagnostics (Phase 0).

Produces ``algorithm/results/physical_diagnosis/``:

- ``task_feasibility.csv`` — per-task lower-bound classification
- ``capacity_saturation.json`` — per-drone capacity utilisation
- ``crossover_saving_distribution.csv`` — S_ij histogram for all task pairs
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from statistics import fmean, median

_PROJECT = Path(__file__).resolve().parents[1]  # algorithm/
sys.path.insert(0, str(_PROJECT / "src"))

from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem
from uav_dispatch.physical_lower_bound import (
    TaskFeasibility,
    capacity_profile,
    crossover_saving_distribution,
    feasibility_report,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_CSV = _PROJECT.parent / "data" / "raw" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
OUT_DIR = _PROJECT / "results" / "physical_diagnosis"

# Solution files to inspect (pick one best A2 per variant).
SOLUTION_FILES: tuple[tuple[str, Path], ...] = (
    (
        "best_alns_400iter",
        _PROJECT
        / "results"
        / "alns_core_comparison_backup_2layer"
        / "best_alns_core_equal_iterations_400.json",
    ),
    (
        "best_a2_240s_seed0",
        _PROJECT
        / "results"
        / "capacity_halns"
        / "run_solutions"
        / "equal_wall_clock_240s__a2__seed_2026080500.json",
    ),
    (
        "best_a2_240s_seed1",
        _PROJECT
        / "results"
        / "capacity_halns"
        / "run_solutions"
        / "equal_wall_clock_240s__a2__seed_2026080501.json",
    ),
    (
        "best_a2_240s_seed2",
        _PROJECT
        / "results"
        / "capacity_halns"
        / "run_solutions"
        / "equal_wall_clock_240s__a2__seed_2026080502.json",
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_solution(path: Path) -> tuple[tuple[tuple[int, ...], ...], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    routes_raw = data["routes"]
    # routes can be list-of-lists (legacy) or list-of-dicts with "encoded_visits"
    routes: list[tuple[int, ...]] = []
    for item in routes_raw:
        if isinstance(item, list):
            routes.append(tuple(int(v) for v in item))
        elif isinstance(item, dict):
            routes.append(tuple(int(v) for v in item["encoded_visits"]))
        else:
            raise TypeError(f"Unexpected route type: {type(item)}")
    return tuple(routes), data


def _write_csv(path: Path, headers: tuple[str, ...], rows: list[tuple]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("Phase 0 — Physical Feasibility Diagnostics")
    print("=" * 60)

    # ---- Load problem ----
    print(f"\nLoading data: {DATA_CSV.name}")
    tasks = load_tasks_csv(DATA_CSV)
    problem = Problem(tasks=tasks, drone_count=8, max_tasks_per_drone=25, capacity=2)
    print(f"  {len(problem.tasks)} tasks, {problem.drone_count} drones, "
          f"K={problem.max_tasks_per_drone}, capacity={problem.capacity}")

    # ---- Step 0.1: Task feasibility ----
    print("\n" + "-" * 40)
    print("Step 0.1 — Task-level physical feasibility")

    all_feasibility_rows: list[tuple] = []
    summary_rows: list[tuple] = []

    for label, sol_path in SOLUTION_FILES:
        if not sol_path.exists():
            print(f"  SKIP {label}: file not found ({sol_path})")
            continue

        routes, data = _load_solution(sol_path)
        records = feasibility_report(problem, routes)
        score = data["score"]

        # Aggregate
        impossible = [r for r in records if r.category == "physically_impossible"]
        sched_fail = [r for r in records if r.category == "scheduling_failure"]
        on_time = [r for r in records if r.category == "on_time"]
        late_ids = sorted(
            r.task_id for r in records
            if not r.solution_on_time
        )

        print(f"\n  [{label}]")
        print(f"    late_count         = {score['late_count']}")
        print(f"    physically impossible = {len(impossible)}")
        print(f"    scheduling failures   = {len(sched_fail)}")
        print(f"    on time               = {len(on_time)}")
        print(f"    P(both on-time & on-schedule) = "
              f"{len(on_time)}/{len(records)}")

        if impossible:
            worst = max(impossible, key=lambda r: r.deadline_min - r.direct_completion_min)
            print(f"    worst impossible gap = {worst.deadline_min - worst.direct_completion_min:.1f} min "
                  f"(task {worst.task_id}, deadline={worst.deadline_min:.1f}, "
                  f"min={worst.direct_completion_min:.1f})")

        if sched_fail:
            # Pickup-slack analysis
            pickup_slacks = [
                r.latest_pickup_min - r.solution_pickup_min
                for r in sched_fail
                if r.solution_pickup_min == r.solution_pickup_min  # not NaN
            ]
            if pickup_slacks:
                print(f"    mean pickup overshoot  = {fmean(abs(s) for s in pickup_slacks if s < 0):.1f} min")
                print(f"    median pickup overshoot= {median(abs(s) for s in pickup_slacks if s < 0):.1f} min")

        # Accumulate for master CSV
        for r in records:
            all_feasibility_rows.append((
                label, r.task_id, r.deadline_min, r.direct_completion_min,
                r.latest_pickup_min, r.solution_pickup_min,
                r.solution_delivery_min, r.category,
            ))
        summary_rows.append((
            label, score["late_count"], score["total_lateness_min"],
            score["distance_km"], len(impossible), len(sched_fail),
            len(on_time), " / ".join(map(str, late_ids[:10])),
        ))

    # Write Step 0.1 output
    _write_csv(
        OUT_DIR / "task_feasibility.csv",
        ("solution_label", "task_id", "deadline_min", "direct_completion_min",
         "latest_pickup_min", "solution_pickup_min", "solution_delivery_min",
         "category"),
        all_feasibility_rows,
    )
    _write_csv(
        OUT_DIR / "feasibility_summary.csv",
        ("solution_label", "late_count", "total_lateness_min", "distance_km",
         "physically_impossible", "scheduling_failures", "on_time",
         "late_task_ids (first 10)"),
        summary_rows,
    )
    print(f"\n  → {OUT_DIR / 'task_feasibility.csv'}")
    print(f"  → {OUT_DIR / 'feasibility_summary.csv'}")

    # ---- Step 0.2a: Capacity saturation ----
    print("\n" + "-" * 40)
    print("Step 0.2a — Capacity saturation per drone")

    # Use best A2 (seed0) for this analysis
    cap_sol = SOLUTION_FILES[0]
    if cap_sol[1].exists():
        routes, _ = _load_solution(cap_sol[1])
        profiles = capacity_profile(problem, routes)

        cap_rows: list[dict] = []
        for p in profiles:
            print(f"  drone {p.drone}: total={p.total_time_min:.1f} min, "
                  f"loaded={p.loaded_fraction:.1%}, full={p.full_fraction:.1%}")
            cap_rows.append({
                "drone": p.drone,
                "total_time_min": round(p.total_time_min, 4),
                "loaded_fraction": round(p.loaded_fraction, 6),
                "full_fraction": round(p.full_fraction, 6),
                "intervals": [
                    [round(s, 4), round(e, 4), l]
                    for s, e, l in p.intervals
                ],
            })
        with (OUT_DIR / "capacity_saturation.json").open("w", encoding="utf-8") as fh:
            json.dump(cap_rows, fh, indent=2, ensure_ascii=False)
        print(f"  → {OUT_DIR / 'capacity_saturation.json'}")

    # ---- Step 0.2b: Crossover saving ----
    print("\n" + "-" * 40)
    print("Step 0.2b — Crossover-saving distribution S_ij")
    print("  computing all-pairs (may take 10-30 s for 200 tasks) ...")

    savings = crossover_saving_distribution(problem)
    values = [s for _, _, s in savings]
    pos_count = sum(1 for v in values if v > 0)
    neg_count = sum(1 for v in values if v < 0)
    print(f"  pairs = {len(values)}")
    print(f"  S_ij > 0 (beneficial): {pos_count} ({pos_count/len(values):.1%})")
    print(f"  S_ij < 0 (costly):     {neg_count} ({neg_count/len(values):.1%})")
    print(f"  max(S_ij) = {max(values):.2f} km")
    print(f"  min(S_ij) = {min(values):.2f} km")
    print(f"  mean(S_ij)= {fmean(values):.2f} km")

    _write_csv(
        OUT_DIR / "crossover_saving_distribution.csv",
        ("task_i", "task_j", "S_ij_km"),
        [(i, j, round(s, 6)) for i, j, s in savings],
    )
    print(f"  → {OUT_DIR / 'crossover_saving_distribution.csv'}")

    print("\n" + "=" * 60)
    print("Diagnostics complete.")
    print(f"Output: {OUT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
