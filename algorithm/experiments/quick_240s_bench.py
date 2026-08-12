"""LEGACY / NOT USED FOR CURRENT PAPER — uses the old cooperative wrapper
(solve_dynamic_cooperative) with swap enabled.

Quick 240s benchmark for B1-Dynamic-Coop-HALNS."""
import sys, json
from pathlib import Path
from time import perf_counter

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT / "src"))

from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem
from uav_dispatch.cooperative_search import CooperativeConfig, solve_dynamic_cooperative
from uav_dispatch.dynamic_relay import DynamicRelayConfig

DATA = _PROJECT.parent / "data" / "raw" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
OUT = _PROJECT / "results" / "dynamic_cooperative" / "quick_bench.json"
OUT.parent.mkdir(parents=True, exist_ok=True)

tasks = load_tasks_csv(DATA)
problem = Problem(tasks=tasks, drone_count=8, max_tasks_per_drone=25, capacity=2)
T = 238.0
SEEDS = (2026080500, 2026080501, 2026080502)
results = []

for seed in SEEDS:
    print(f"\nSeed {seed}...", flush=True)
    t0 = perf_counter()
    cfg = CooperativeConfig(
        seed=seed, time_limit_seconds=T, candidate_limit=48,
        enable_swap=True, enable_batch_relay=True, enable_ownership_exchange=True,
        dynamic_relay=DynamicRelayConfig(
            min_active_stations=2, max_active_stations=8, station_refresh_interval=20,
        ),
        verbose=False,
    )
    r = solve_dynamic_cooperative(problem, config=cfg)
    t = perf_counter() - t0
    entry = {
        "seed": seed,
        "late_count": r.validation.late_count,
        "distance_km": r.validation.distance_km,
        "total_lateness_min": r.validation.total_lateness_min,
        "runtime_s": round(t, 1),
        "iterations": r.iterations,
        "pair_exchanges": r.pair_exchanges,
        "ownership_changes": r.ownership_changes,
        "ownership_accepted": r.ownership_accepted,
        "ownership_attempts": r.ownership_attempts,
        "active_stations": r.active_station_count,
        "station_adds": r.station_adds,
        "station_drops": r.station_drops,
        "station_trajectory_len": len(r.station_trajectory),
        "relay_attempts": r.relay_attempts,
        "relay_accepted": r.relay_accepted,
        "relay_count": r.relay_count,
        "swap_candidates": r.swap_candidates,
        "swap_accepted": r.swap_accepted,
        "swap_count": r.final_swap_count,
        "service_distribution": r.service_distribution,
        "op_stats": r.op_stats,
    }
    results.append(entry)
    print(f"  late={r.validation.late_count} dist={r.validation.distance_km:.1f} "
          f"time={t:.1f}s iters={r.iterations} own_chg={r.ownership_changes} "
          f"relay_acc={r.relay_accepted} relay_n={r.relay_count} "
          f"st_traj={len(r.station_trajectory)} swap={r.final_swap_count}", flush=True)

OUT.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
print(f"\nSaved to {OUT}")
