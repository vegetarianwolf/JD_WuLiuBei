"""LEGACY / NOT USED FOR CURRENT PAPER — uses the old cooperative wrapper
(solve_dynamic_cooperative) with swap enabled.

Quick A2 vs Dynamic-Coop comparison — single seed, 30s."""
import sys, json
from pathlib import Path
from time import perf_counter

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT / "src"))

from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem
from uav_dispatch.alns import ALNSConfig, solve_alns_core
from uav_dispatch.cooperative_search import CooperativeConfig, solve_dynamic_cooperative
from uav_dispatch.dynamic_relay import DynamicRelayConfig

DATA = _PROJECT.parent / "data" / "raw" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
tasks = load_tasks_csv(DATA)
p = Problem(tasks=tasks, drone_count=8, max_tasks_per_drone=25, capacity=2)
T = 30.0
SEED = 42

# ---- A2 baseline ----
print("=== A2 Baseline (30s) ===", flush=True)
t0 = perf_counter()
a2 = solve_alns_core(p, config=ALNSConfig(max_iterations=100_000, time_limit_seconds=T - 1, seed=SEED))
t_a2 = perf_counter() - t0
s = a2.evaluation.score
print(f"late={s.late_count} dist={s.distance_km:.1f} time={t_a2:.1f}s iters={a2.iterations}", flush=True)

# ---- Dynamic-Coop ----
print("\n=== Dynamic-Coop (30s) ===", flush=True)
t0 = perf_counter()
cfg = CooperativeConfig(
    seed=SEED, time_limit_seconds=T, candidate_limit=48,
    a2_warm_start_share=0.30, coop_construction_share=0.35, coop_alns_share=0.35,
    cooperative_iterations=30, station_refresh_every=5,
    enable_relay=True, enable_ownership_exchange=True, enable_swap=True,
    dynamic_relay=DynamicRelayConfig(
        min_active_stations=2, max_active_stations=8,
        station_rotation_interval=3, station_elite_keep=0,
    ),
    verbose=True,
)
r = solve_dynamic_cooperative(p, config=cfg)
t_coop = perf_counter() - t0

# ---- Comparison table ----
print()
print("=" * 70)
print("COMPARISON (seed=%d, %ds)" % (SEED, int(T)))
print("=" * 70)
rows = [
    ("late_count", str(s.late_count), str(r.validation.late_count)),
    ("distance_km", "%.1f" % s.distance_km, "%.1f" % r.validation.distance_km),
    ("runtime_s", "%.1f" % t_a2, "%.1f" % t_coop),
    ("iterations", str(a2.iterations), str(r.iterations)),
    ("", "", ""),
    ("ownership_changes", "-", str(r.ownership_changes)),
    ("ownership_attempts", "-", str(r.ownership_attempts)),
    ("ownership_accepted", "-", str(r.ownership_accepted)),
    ("relay_attempts", "-", str(r.relay_attempts)),
    ("relay_accepted", "-", str(r.relay_accepted)),
    ("relay_count", "-", str(r.relay_count)),
    ("swap_candidates", "-", str(r.swap_candidates)),
    ("station_trajectory", "-", str(len(r.station_trajectory))),
    ("station_adds", "-", str(r.station_adds)),
    ("service_dist", "-", str(r.service_distribution)),
    ("valid", "-", str(r.validation.valid)),
    ("op_stats", "-", json.dumps(r.op_stats)),
]
print("%-28s %12s %15s" % ("Metric", "A2", "Dynamic-Coop"))
print("-" * 56)
for label, a2v, coopv in rows:
    print("%-28s %12s %15s" % (label, a2v, coopv))
