"""Cooperative mechanism ablation — common-start fair comparison.

Every variant starts from the *same* A2 warm solution (same seed), deep-copied,
and refines for the same wall-clock budget.

Variants (formal objective everywhere: ``(late_count, distance_km)``):
    A2         : Ownership OFF, Relay OFF            (pure A2 refine)
    A2+BO      : Ownership ON,  Relay OFF            (Balanced Ownership only)
    A2+DR      : Ownership OFF, Relay ON (DR+Blocker guidance)
    Full       : Ownership ON,  Relay ON (DR+Blocker)
    DR-only    : Relay ON, Delivery-Risk ON,  Blocker OFF   (guidance ablation)
    Blocker-only: Relay ON, Delivery-Risk OFF, Blocker ON   (guidance ablation)

Usage:
    python experiments/run_ablation.py --refine 30 --seeds 500,501,502
    python experiments/run_ablation.py --refine 60 --seeds 500..509
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from time import perf_counter

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT / "src"))

from uav_dispatch.alns import ALNSConfig, solve_alns_core
from uav_dispatch.cooperative_alns import (
    CooperativeHALNSConfig,
    _make_solution,
    solve_cooperative_halns,
)
from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem

DATA_CSV = _PROJECT.parent / "data" / "raw" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
OUT_DIR = _PROJECT / "results" / "ablation"


def make_problem() -> Problem:
    tasks = load_tasks_csv(DATA_CSV)
    return Problem(
        tasks=tasks, drone_count=8, max_tasks_per_drone=25, capacity=2
    )


def variant_config(
    seed: int, refine: float, *, ownership: bool, relay: bool,
    delivery_risk: bool, blocker: bool,
) -> CooperativeHALNSConfig:
    return CooperativeHALNSConfig(
        seed=seed,
        time_limit_seconds=refine,
        candidate_limit=48,
        min_destroy_fraction=0.04,
        max_destroy_fraction=0.10,
        enable_ownership=ownership,
        enable_relay=relay,
        enable_delivery_risk_relay=delivery_risk,
        enable_blocker_relay=blocker,
        critical_task_limit=6,
        blocker_per_task_limit=3,
        delivery_risk_slack_limit=15.0,
        near_critical_slack_limit=10.0,
        relay_receiver_limit=1,
        relay_point_candidate_limit=3,
        relay_candidate_limit=1,
        relay_max_evaluations=3,
        ownership_interval=50,
        relay_interval=30,
        repair_candidate_limit=24,
        verbose=False,
    )


VARIANTS = {
    "A2": dict(ownership=False, relay=False, delivery_risk=False, blocker=False),
    "A2+BO": dict(ownership=True, relay=False, delivery_risk=False, blocker=False),
    "A2+DR": dict(ownership=False, relay=True, delivery_risk=True, blocker=True),
    "Full": dict(ownership=True, relay=True, delivery_risk=True, blocker=True),
    "DR-only": dict(ownership=False, relay=True, delivery_risk=True, blocker=False),
    "Blocker-only": dict(ownership=False, relay=True, delivery_risk=False, blocker=True),
}


def run_variant(
    problem: Problem,
    warm_routes,
    seed: int,
    refine: float,
    flags: dict,
) -> dict:
    initial = _make_solution(problem, warm_routes)
    cfg = variant_config(seed, refine, **flags)
    t0 = perf_counter()
    result = solve_cooperative_halns(
        problem, config=cfg, initial_solution=initial
    )
    runtime = perf_counter() - t0
    return {
        "late_count": result.validation.late_count,
        "distance_km": result.validation.distance_km,
        "runtime_s": round(runtime, 2),
        "iterations": result.iterations,
        "iterations_per_sec": round(result.iterations / max(runtime, 1e-9), 2),
        "time_to_best_s": round(result.time_to_best_seconds, 2),
        "ownership_accepted": result.ownership_accepted,
        "relay_accepted": result.relay_accepted,
        "delivery_risk_relays_accepted": result.delivery_risk_relays_accepted,
        "blocker_relays_accepted": result.blocker_relays_accepted,
        "dual_trigger_relays_accepted": result.dual_trigger_relays_accepted,
        "late_tasks_rescued": result.late_tasks_rescued,
        "critical_pickups_advanced": result.critical_pickups_advanced,
        "relay_exact_evaluations": result.relay_exact_evaluations,
        "relay_count_final": result.relay_count_final,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Cooperative ablation")
    parser.add_argument("--warm", type=float, default=30.0)
    parser.add_argument("--refine", type=float, default=30.0)
    parser.add_argument(
        "--seeds", type=str, default="500,501,502",
        help="comma-separated seeds (also accepts 500..509)",
    )
    parser.add_argument(
        "--variants", type=str, default="",
        help="comma-separated subset of " + ",".join(VARIANTS),
    )
    args = parser.parse_args()

    if ".." in args.seeds:
        lo, hi = (int(x) for x in args.seeds.split(".."))
        seeds = list(range(lo, hi + 1))
    else:
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]

    variant_names = (
        list(VARIANTS)
        if not args.variants
        else [v.strip() for v in args.variants.split(",") if v.strip()]
    )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    problem = make_problem()
    print(
        f"Ablation  |  warm={args.warm:.0f}s  refine={args.refine:.0f}s  "
        f"seeds={seeds}  variants={variant_names}",
        flush=True,
    )

    all_rows: dict[str, list[dict]] = {v: [] for v in variant_names}
    for seed in seeds:
        print(f"\n--- seed {seed} ---", flush=True)
        warm = solve_alns_core(
            problem,
            config=ALNSConfig(
                max_iterations=100_000,
                time_limit_seconds=args.warm,
                seed=seed,
            ),
        )
        warm_score = warm.evaluation.score
        print(
            f"  warm late={warm_score.late_count} "
            f"dist={warm_score.distance_km:.1f}",
            flush=True,
        )
        for name in variant_names:
            flags = VARIANTS[name]
            row = run_variant(
                problem, warm.routes, seed, args.refine, flags
            )
            row["seed"] = seed
            row["warm_late_count"] = warm_score.late_count
            row["variant"] = name
            all_rows[name].append(row)
            print(
                f"  {name:<12} late={row['late_count']} "
                f"dist={row['distance_km']:.1f} "
                f"({row['runtime_s']:.1f}s, {row['iterations_per_sec']} it/s) "
                f"BO={row['ownership_accepted']} relay={row['relay_accepted']}",
                flush=True,
            )

    summary = {}
    for name, rows in all_rows.items():
        late = [r["late_count"] for r in rows]
        summary[name] = {
            "late_counts": late,
            "median_late": sorted(late)[len(late) // 2],
            "mean_late": round(sum(late) / len(late), 2) if late else None,
            "mean_relay_accepted": (
                round(sum(r["relay_accepted"] for r in rows) / len(rows), 2)
                if rows else None
            ),
            "mean_ownership_accepted": (
                round(sum(r["ownership_accepted"] for r in rows) / len(rows), 2)
                if rows else None
            ),
        }
    print(f"\n=== Summary ({len(seeds)} seeds) ===", flush=True)
    for name, s in summary.items():
        print(
            f"  {name:<12} late={s['late_counts']} "
            f"median={s['median_late']} mean={s['mean_late']}",
            flush=True,
        )

    payload = {
        "warm_seconds": args.warm,
        "refine_seconds": args.refine,
        "seeds": seeds,
        "variants": variant_names,
        "summary": summary,
        "rows": all_rows,
    }
    out_name = f"ablation_w{int(args.warm)}_r{int(args.refine)}.json"
    (OUT_DIR / out_name).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Saved -> {OUT_DIR / out_name}")


if __name__ == "__main__":
    main()
