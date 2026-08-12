"""LEGACY / NOT USED FOR CURRENT PAPER — depends on the old cooperative
wrapper (solve_dynamic_cooperative), swap operator and dynamic-station
semantics that are no longer part of the formal Cooperative-HALNS path.

Use ``run_cooperative_halns_benchmark.py`` / ``run_common_start_benchmark.py``
for the current paper.

Phase K: Real benchmark gate — 3 seeds x 60s A2 vs Dynamic-Coop comparison.

Verifies that all cooperative mechanisms are actually called during
production solving on the 200-task instance.

Acceptance criteria (MANDATORY):
  - ownership_calls > 0      (proves reassignment fires inside the HALNS)
  - relay_calls > 0          (proves relay mechanism called)
  - blocker_relay_calls > 0  (proves blocker-release relay called)
  - swap_calls > 0           (proves swap operator called)
  - dynamic_station_calls > 0 (proves station management fires)
  - swap_candidates > 0 / relay_candidates > 0 (candidates really generated)

Acceptance criteria (recorded, NOT mandatory):
  - relay_accepted, swap_accepted, late_count improvement

Only (late_count, distance_km) improvements are kept.
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
    solve_dynamic_cooperative,
)
from uav_dispatch.dynamic_relay import DynamicRelayConfig
from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem

DATA_CSV = _PROJECT.parent / "data" / "raw" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
OUT_DIR = _PROJECT / "results" / "phase1_verify"
SEEDS = (2026080500, 2026080501, 2026080502)
TIME_LIMIT = 60.0


def run_a2_baseline(problem: Problem, seed: int) -> dict:
    started = perf_counter()
    result = solve_alns_core(
        problem,
        config=ALNSConfig(
            max_iterations=100_000,
            time_limit_seconds=TIME_LIMIT - 1.0,
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
        "runtime_s": round(elapsed, 1),
        "iterations": result.iterations,
        "a2_pickup_late": s.late_count,  # same as late_count for A2
    }


def run_dynamic_coop(problem: Problem, seed: int) -> dict:
    config = CooperativeConfig(
        seed=seed,
        time_limit_seconds=TIME_LIMIT,
        candidate_limit=48,
        a2_warm_start_share=0.20,
        coop_construction_share=0.30,
        coop_alns_share=0.50,
        cooperative_iterations=30,
        station_refresh_every=10,
        enable_swap=True,
        enable_batch_relay=True,
        enable_ownership_exchange=True,
        enable_relay=True,
        dynamic_relay=DynamicRelayConfig(
            min_active_stations=2,
            max_active_stations=8,
            station_refresh_interval=20,
            station_candidate_pool=20,
            station_elite_keep=2,
            station_drop_patience=80,
            marginal_activation_threshold=0.05,
            station_rotation_interval=5,
        ),
        verbose=True,
    )
    result = solve_dynamic_cooperative(problem, config=config)
    return {
        "seed": seed,
        "solver": "Dynamic-Coop",
        "late_count": result.validation.late_count,
        "distance_km": result.validation.distance_km,
        "runtime_s": round(result.runtime_seconds, 1),
        "iterations": result.iterations,
        # -- warm-start --
        "a2_pickup_late": result.stage1_late_count,
        "stage1_pickup_late_initial": result.stage1_pickup_late_initial,
        "stage1_pickup_late_final": result.stage1_pickup_late_final,
        # -- ownership --
        "ownership_changes": result.ownership_changes,
        "pair_exchanges": result.pair_exchanges,
        "cycle_exchanges": result.cycle_exchanges,
        # -- relay --
        "relay_attempts": result.relay_attempts,
        "relay_accepted": result.relay_accepted,
        "relay_count": result.relay_count,
        # -- station --
        "active_stations": result.active_station_count,
        "station_adds": result.station_adds,
        "station_drops": result.station_drops,
        "station_trajectory_len": len(result.station_trajectory),
        # -- swap --
        "swap_candidates": result.swap_candidates,
        "swap_accepted": result.swap_accepted,
        "final_swap_count": result.final_swap_count,
        # -- service --
        "service_distribution": result.service_distribution,
        # -- per-mechanism operator statistics (calls / candidates / ...) --
        "op_stats": result.op_stats,
        # -- validity --
        "valid": result.validation.valid,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 80)
    print("Phase K — Real Benchmark Gate (3 seeds x 60s)")
    print("=" * 80)

    tasks = load_tasks_csv(DATA_CSV)
    problem = Problem(
        tasks=tasks, drone_count=8, max_tasks_per_drone=25, capacity=2
    )
    print(f"\n{len(problem.tasks)} tasks, {problem.drone_count} drones, "
          f"capacity={problem.capacity}, max_tasks={problem.max_tasks_per_drone}")

    a2_results: list[dict] = []
    coop_results: list[dict] = []

    for seed in SEEDS:
        print(f"\n{'='*80}")
        print(f"Seed {seed}")
        print(f"{'='*80}")

        # A2 baseline
        print("\n[A2 Baseline]")
        a2 = run_a2_baseline(problem, seed)
        a2_results.append(a2)
        print(f"  late={a2['late_count']}, dist={a2['distance_km']:.1f}, "
              f"time={a2['runtime_s']:.1f}s")

        # Dynamic-Coop
        print(f"\n[Dynamic-Coop]")
        coop = run_dynamic_coop(problem, seed)
        coop_results.append(coop)

    # ---- Acceptance Gate ----
    print(f"\n{'='*80}")
    print("ACCEPTANCE GATE")
    print(f"{'='*80}")

    all_pass = True

    for i, (seed, coop) in enumerate(zip(SEEDS, coop_results)):
        print(f"\n--- Seed {seed} ---")
        op_stats = coop.get("op_stats", {})
        attempts = op_stats.get("attempts", {})
        candidates = op_stats.get("candidates", {})
        accepted = op_stats.get("accepted", {})
        checks = [
            ("ownership_calls > 0", attempts.get("ownership", 0) > 0, True),
            ("relay_calls > 0", attempts.get("relay", 0) > 0, True),
            ("blocker_relay_calls > 0", attempts.get("blocker_relay", 0) > 0, True),
            ("swap_calls > 0", attempts.get("swap", 0) > 0, True),
            ("dynamic_station_calls > 0", attempts.get("dynamic_station", 0) > 0, True),
            ("swap_candidates > 0", candidates.get("swap", 0) > 0, True),
            ("relay_candidates > 0", candidates.get("relay", 0) > 0, True),
            ("valid == True", coop["valid"], True),
        ]
        for name, passed, mandatory in checks:
            status = "PASS" if passed else ("FAIL*" if mandatory else "INFO")
            if mandatory and not passed:
                all_pass = False
            print(f"  [{status}] {name}: {coop.get(name.split()[0], '?')}")

    # ---- Comparison Table ----
    print(f"\n{'='*80}")
    print("COMPARISON TABLE")
    print(f"{'='*80}")
    header = (f"{'Seed':<12} {'Solver':<16} {'late':>6} {'dist':>8} "
              f"{'own_chg':>8} {'rel_att':>8} {'rel_acc':>8} {'rel_n':>6} "
              f"{'st_add':>7} {'st_drop':>8} {'st_traj':>8} {'sw_cand':>8} {'svc_dist':>20}")
    print(header)
    print("-" * len(header))

    for a2, coop in zip(a2_results, coop_results):
        delta = coop["late_count"] - a2["late_count"]
        svc = coop["service_distribution"]
        svc_str = f"D{svc.get('DIRECT',0)}/R{svc.get('RELAY',0)}/S{svc.get('SWAP',0)}"
        print(f"{a2['seed']:<12} {'A2-Core':<16} {a2['late_count']:>6} {a2['distance_km']:>8.1f} "
              f"{'-':>8} {'-':>8} {'-':>8} {'-':>6} {'-':>7} {'-':>8} {'-':>8} {'-':>8} {'-':>20}")
        print(f"{'':<12} {'Dynamic-Coop':<16} {coop['late_count']:>6} {coop['distance_km']:>8.1f} "
              f"{coop['ownership_changes']:>8} {coop['relay_attempts']:>8} "
              f"{coop['relay_accepted']:>8} {coop['relay_count']:>6} "
              f"{coop['station_adds']:>7} {coop['station_drops']:>8} "
              f"{coop['station_trajectory_len']:>8} {coop['swap_candidates']:>8} {svc_str:>20}")
        print(f"{'':<12} {'':<16} {'Δ='+str(delta):>6}")

    # ---- Save results ----
    out_path = OUT_DIR / "phase1_verify_results.json"
    out_path.write_text(json.dumps({
        "a2": a2_results,
        "dynamic_coop": coop_results,
        "gate_passed": all_pass,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nResults saved to {out_path}")

    if all_pass:
        print("\n>>> ACCEPTANCE GATE PASSED — all mandatory criteria met. <<<")
    else:
        print("\n>>> ACCEPTANCE GATE FAILED — fix the FAIL* items above. <<<")

    return 1 if not all_pass else 0


if __name__ == "__main__":
    raise SystemExit(main())
