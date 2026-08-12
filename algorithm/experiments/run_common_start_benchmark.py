"""Common-start fairness benchmark: A2 refine vs Pickup-Aware Cooperative-HALNS.

Fair protocol (section "Common-start 公平比较"):
    seed s
      -> run ONE A2 warm start
      -> same_initial_solution (deep-copied)
         /                            \
    A2 refine (refine_s budget)   Full Cooperative-HALNS refine (same budget)

Every method gets the *same* initial solution and the *same* wall-clock
refinement budget, so any difference is attributable to the search mechanism
itself, not to warm-start luck.

Formal objective: ``(late_count, distance_km)``.

Usage:
    python experiments/run_common_start_benchmark.py --warm 30 --refine 30 --seeds 500,501,502
    python experiments/run_common_start_benchmark.py --warm 30 --refine 60 --seeds 500..509
    python experiments/run_common_start_benchmark.py --warm 30 --refine 120 --seeds 500..509
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
OUT_DIR = _PROJECT / "results" / "common_start"


def make_problem() -> Problem:
    tasks = load_tasks_csv(DATA_CSV)
    return Problem(
        tasks=tasks, drone_count=8, max_tasks_per_drone=25, capacity=2
    )


def full_config(seed: int, refine: float) -> CooperativeHALNSConfig:
    return CooperativeHALNSConfig(
        seed=seed,
        time_limit_seconds=refine,
        candidate_limit=48,
        min_destroy_fraction=0.04,
        max_destroy_fraction=0.10,
        enable_ownership=True,
        enable_relay=True,
        enable_delivery_risk_relay=True,
        enable_blocker_relay=True,
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


def run_common_start(
    problem: Problem,
    seed: int,
    warm_s: float,
    refine_s: float,
) -> dict:
    # ---- one shared warm start ----
    warm = solve_alns_core(
        problem,
        config=ALNSConfig(
            max_iterations=100_000,
            time_limit_seconds=warm_s,
            seed=seed,
        ),
    )
    warm_score = warm.evaluation.score
    warm_routes = warm.routes

    # ---- Branch A: plain A2 refine ----
    t0 = perf_counter()
    a2 = solve_alns_core(
        problem,
        config=ALNSConfig(
            max_iterations=100_000,
            time_limit_seconds=refine_s,
            seed=seed,
        ),
        initial_routes=warm_routes,
    )
    a2_runtime = perf_counter() - t0
    a2_s = a2.evaluation.score

    # ---- Branch B: Full Cooperative-HALNS refine (same initial) ----
    coop_initial = _make_solution(problem, warm_routes)
    t0 = perf_counter()
    full = solve_cooperative_halns(
        problem,
        config=full_config(seed, refine_s),
        initial_solution=coop_initial,
    )
    full_runtime = perf_counter() - t0
    full_s = full.validation.score

    outcome = (
        "win"
        if (full_s.late_count, full_s.distance_km)
        < (a2_s.late_count, a2_s.distance_km)
        else (
            "tie"
            if (full_s.late_count, full_s.distance_km)
            == (a2_s.late_count, a2_s.distance_km)
            else "loss"
        )
    )
    row = {
        "seed": seed,
        "warm_late_count": warm_score.late_count,
        "warm_distance_km": warm_score.distance_km,
        "a2_refine_late_count": a2_s.late_count,
        "a2_refine_distance_km": a2_s.distance_km,
        "a2_runtime_s": round(a2_runtime, 2),
        "a2_iters": a2.iterations,
        "a2_iterations_per_sec": round(a2.iterations / max(a2_runtime, 1e-9), 2),
        "full_refine_late_count": full_s.late_count,
        "full_refine_distance_km": full_s.distance_km,
        "full_runtime_s": round(full_runtime, 2),
        "full_iters": full.iterations,
        "full_iterations_per_sec": round(
            full.iterations / max(full_runtime, 1e-9), 2
        ),
        "full_time_to_best_s": round(full.time_to_best_seconds, 2),
        "outcome": outcome,
        # -- mechanism counters --
        "ownership_accepted": full.ownership_accepted,
        "relay_accepted": full.relay_accepted,
        "delivery_risk_relays_accepted": full.delivery_risk_relays_accepted,
        "blocker_relays_accepted": full.blocker_relays_accepted,
        "dual_trigger_relays_accepted": full.dual_trigger_relays_accepted,
        "late_tasks_rescued": full.late_tasks_rescued,
        "critical_pickups_advanced": full.critical_pickups_advanced,
        "relay_exact_evaluations": full.relay_exact_evaluations,
        "relay_candidates_before_dedup": full.relay_candidates_before_dedup,
        "relay_count_final": full.relay_count_final,
        "unique_relay_points_used": full.unique_relay_points_used,
    }
    print(
        f"  seed {seed}: warm late={warm_score.late_count} | "
        f"A2 late={a2_s.late_count} dist={a2_s.distance_km:.1f} "
        f"({a2_runtime:.1f}s, {row['a2_iterations_per_sec']} it/s) | "
        f"Full late={full_s.late_count} dist={full_s.distance_km:.1f} "
        f"({full_runtime:.1f}s, {row['full_iterations_per_sec']} it/s) "
        f"-> {outcome}",
        flush=True,
    )
    print(
        f"       BO={row['ownership_accepted']} "
        f"relay={row['relay_accepted']} (DR={row['delivery_risk_relays_accepted']}, "
        f"B={row['blocker_relays_accepted']}, dual={row['dual_trigger_relays_accepted']}) "
        f"rescued={row['late_tasks_rescued']} advanced={row['critical_pickups_advanced']} "
        f"relay_eval={row['relay_exact_evaluations']} relays={row['relay_count_final']}",
        flush=True,
    )
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description="Common-start benchmark")
    parser.add_argument("--warm", type=float, default=30.0)
    parser.add_argument("--refine", type=float, default=30.0)
    parser.add_argument(
        "--seeds", type=str, default="500,501,502",
        help="comma-separated seeds (also accepts 500..509)",
    )
    parser.add_argument("--out", type=str, default="")
    args = parser.parse_args()

    if ".." in args.seeds:
        lo, hi = (int(x) for x in args.seeds.split(".."))
        seeds = list(range(lo, hi + 1))
    else:
        seeds = [int(s) for s in args.seeds.split(",") if s.strip()]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    problem = make_problem()
    print(
        f"Common-start  |  warm={args.warm:.0f}s  refine={args.refine:.0f}s  "
        f"seeds={seeds}",
        flush=True,
    )

    rows = [run_common_start(problem, seed, args.warm, args.refine) for seed in seeds]

    wins = sum(1 for r in rows if r["outcome"] == "win")
    ties = sum(1 for r in rows if r["outcome"] == "tie")
    losses = sum(1 for r in rows if r["outcome"] == "loss")
    print(
        f"\n=== Summary ({len(rows)} seeds) ===\n"
        f"  Full vs A2: {wins} win / {ties} tie / {losses} loss",
        flush=True,
    )

    payload = {
        "warm_seconds": args.warm,
        "refine_seconds": args.refine,
        "seeds": seeds,
        "win_tie_loss": {"win": wins, "tie": ties, "loss": losses},
        "rows": rows,
    }
    out_name = args.out or f"common_start_w{int(args.warm)}_r{int(args.refine)}.json"
    (OUT_DIR / out_name).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Saved -> {OUT_DIR / out_name}")


if __name__ == "__main__":
    main()
