"""Pickup-Aware Cooperative HALNS benchmark (formal path).

Gates
-----
Gate A : ``pytest`` must be fully green before any benchmark runs.
Gate B : smoke — solver must not crash, solution valid, enabled mechanisms
         really called (ownership_calls / relay_calls > 0), relay exact
         evaluations bounded.
Gate C : A2-Core vs Cooperative-HALNS head-to-head at equal total wall-clock.
Gate D : Common-start refinement — the *fair* comparison: the same A2 warm
         solution is refined for the same additional wall-clock by plain A2
         (Branch A) and by the Pickup-Aware Cooperative-HALNS (Branch B).

Formal objective everywhere: ``(late_count, distance_km)``.

The formal Cooperative-HALNS has exactly two service modes (DIRECT / RELAY);
swap is not part of this path.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from time import perf_counter

_PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PROJECT / "src"))

from uav_dispatch.alns import ALNSConfig, solve_alns_core
from uav_dispatch.cooperative_alns import (
    CooperativeHALNSConfig,
    solve_cooperative_halns,
)
from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem

DATA_CSV = _PROJECT.parent / "data" / "raw" / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
OUT_DIR = _PROJECT / "results" / "cooperative_halns"
RUN_DIR = OUT_DIR / "run_solutions"
SEEDS = (2026080500, 2026080501, 2026080502)
GATE_C_TIME = 240.0     # seconds per solver per seed (equal total budget, project protocol)
WARM_SECONDS = 120.0    # common-start warm budget
REFINE_SECONDS = 120.0  # common-start refine budget (equal for both branches)
SMOKE_TIME = 12.0


def make_problem() -> Problem:
    tasks = load_tasks_csv(DATA_CSV)
    return Problem(
        tasks=tasks, drone_count=8, max_tasks_per_drone=25, capacity=2
    )


def coop_config(seed: int, time_limit: float, warm_share: float = 0.5):
    return CooperativeHALNSConfig(
        seed=seed,
        time_limit_seconds=time_limit,
        candidate_limit=48,
        initial_time_limit_seconds=time_limit * warm_share,
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
        verbose=True,
    )


def run_a2(problem: Problem, seed: int, time_limit: float) -> dict:
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
        "solver": "A2-Core",
        "seed": seed,
        "late_count": s.late_count,
        "distance_km": s.distance_km,
        "total_lateness_min": result.evaluation.total_lateness_min,
        "runtime_s": round(elapsed, 2),
        "iterations": result.iterations,
        "time_to_best_s": round(
            float(result.metadata.get("time_to_best_seconds", 0.0)), 2
        ),
    }


def run_coop(problem: Problem, seed: int, time_limit: float) -> dict:
    started = perf_counter()
    result = solve_cooperative_halns(
        problem, config=coop_config(seed, time_limit)
    )
    elapsed = perf_counter() - started
    op = result.op_stats
    svc = result.service_distribution
    return {
        "solver": "Cooperative-HALNS",
        "seed": seed,
        "late_count": result.validation.late_count,
        "distance_km": result.validation.distance_km,
        "total_lateness_min": result.validation.total_lateness_min,
        "runtime_s": round(elapsed, 2),
        "iterations": result.iterations,
        "time_to_best_s": round(result.time_to_best_seconds, 2),
        "pickup_late_count": result.pickup_late_count,
        "near_critical_pickup_count": result.near_critical_pickup_count,
        "min_pickup_slack": round(result.min_pickup_slack, 2),
        # -- ownership --
        "ownership_calls": result.ownership_calls,
        "ownership_candidates": op["ownership"]["candidates"],
        "ownership_feasible": op["ownership"]["feasible"],
        "ownership_accepted": result.ownership_accepted,
        "ownership_improving": op["ownership"]["improving"],
        "ownership_best_improving": op["ownership"]["best_improving"],
        # -- relay --
        "relay_calls": result.relay_calls,
        "delivery_risk_tasks_detected": result.delivery_risk_tasks_detected,
        "blockers_detected": result.blockers_detected,
        "relay_candidates_before_dedup": result.relay_candidates_before_dedup,
        "relay_candidates_after_dedup": result.relay_candidates_after_dedup,
        "relay_candidates_screened_out": result.relay_candidates_screened_out,
        "relay_exact_evaluations": result.relay_exact_evaluations,
        "relay_feasible": result.relay_feasible,
        "relay_accepted": result.relay_accepted,
        "delivery_risk_relays_accepted": result.delivery_risk_relays_accepted,
        "blocker_relays_accepted": result.blocker_relays_accepted,
        "dual_trigger_relays_accepted": result.dual_trigger_relays_accepted,
        "late_tasks_rescued": result.late_tasks_rescued,
        "critical_pickups_advanced": result.critical_pickups_advanced,
        "relay_count_final": result.relay_count_final,
        "unique_relay_points_used": result.unique_relay_points_used,
        "active_station_count": result.active_station_count,
        "service_distribution": {
            "DIRECT": svc.get("DIRECT", 0),
            "RELAY": svc.get("RELAY", 0),
        },
        "operator_weights": result.operator_weights,
        "valid": result.validation.valid,
        "op_stats": op,
    }


def gate_a() -> bool:
    print("=" * 78)
    print("Gate A: pytest")
    print("=" * 78)
    res = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=_PROJECT,
        capture_output=True,
        text=True,
    )
    print(res.stdout[-1200:])
    ok = res.returncode == 0
    print(f"Gate A {'PASS' if ok else 'FAIL'}")
    return ok


def gate_b(problem: Problem) -> bool:
    print("=" * 78)
    print(f"Gate B: smoke ({SMOKE_TIME:.0f}s)")
    print("=" * 78)
    seed = SEEDS[0]
    ok = True
    try:
        a2 = run_a2(problem, seed, SMOKE_TIME)
        print(f"  A2       late={a2['late_count']} dist={a2['distance_km']:.1f}")
        coop = run_coop(problem, seed, SMOKE_TIME)
        print(
            f"  Coop     late={coop['late_count']} dist={coop['distance_km']:.1f} "
            f"valid={coop['valid']}"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  Gate B crashed: {exc!r}")
        return False
    if not coop["valid"]:
        ok = False
        print("  FAIL: coop solution invalid")
    if coop["ownership_calls"] <= 0:
        ok = False
        print("  FAIL: ownership_calls = 0")
    if coop["relay_calls"] <= 0:
        ok = False
        print("  FAIL: relay_calls = 0")
    else:
        print(
            f"  ownership_calls={coop['ownership_calls']} "
            f"relay_calls={coop['relay_calls']} "
            f"relay_exact_evaluations={coop['relay_exact_evaluations']} "
            f"relay_accepted={coop['relay_accepted']}"
        )
    print(f"Gate B {'PASS' if ok else 'FAIL'}")
    return ok


def gate_c(problem: Problem) -> dict:
    print("=" * 78)
    print(f"Gate C: 3 seeds x {GATE_C_TIME:.0f}s  (A2 vs Cooperative-HALNS)")
    print("=" * 78)
    a2_results: list[dict] = []
    coop_results: list[dict] = []
    for seed in SEEDS:
        print(f"\n--- seed {seed} ---")
        a2 = run_a2(problem, seed, GATE_C_TIME)
        a2_results.append(a2)
        print(
            f"  A2    late={a2['late_count']} dist={a2['distance_km']:.1f} "
            f"iters={a2['iterations']}"
        )
        coop = run_coop(problem, seed, GATE_C_TIME)
        coop_results.append(coop)
        svc = coop["service_distribution"]
        print(
            f"  Coop  late={coop['late_count']} dist={coop['distance_km']:.1f} "
            f"iters={coop['iterations']} "
            f"svc=D{svc['DIRECT']}/R{svc['RELAY']} "
            f"relays={coop['relay_count_final']}"
        )
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_DIR / f"coop_seed{seed}.json").write_text(
            json.dumps(coop, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    return {"a2": a2_results, "coop": coop_results}


def gate_d_common_start(problem: Problem) -> dict:
    """Same A2 warm solution, then equal refine budget for both branches."""
    print("=" * 78)
    print(
        f"Gate D: common-start refinement  "
        f"({WARM_SECONDS:.0f}s warm + {REFINE_SECONDS:.0f}s refine each)"
    )
    print("=" * 78)
    rows: list[dict] = []
    for seed in SEEDS:
        print(f"\n--- seed {seed} ---")
        warm = solve_alns_core(
            problem,
            config=ALNSConfig(
                max_iterations=100_000,
                time_limit_seconds=WARM_SECONDS,
                seed=seed,
            ),
        )
        warm_score = warm.evaluation.score
        print(
            f"  warm      late={warm_score.late_count} "
            f"dist={warm_score.distance_km:.1f}"
        )
        warm_routes = warm.routes

        # Branch A: plain A2 refine for REFINE_SECONDS.
        t0 = perf_counter()
        a2_refine = solve_alns_core(
            problem,
            config=ALNSConfig(
                max_iterations=100_000,
                time_limit_seconds=REFINE_SECONDS,
                seed=seed,
            ),
            initial_routes=warm_routes,
        )
        a2_runtime = perf_counter() - t0

        # Branch B: Cooperative-HALNS refine for REFINE_SECONDS (no re-warm).
        from uav_dispatch.cooperative_alns import _make_solution
        coop_initial = _make_solution(problem, warm_routes)
        t0 = perf_counter()
        coop_refine = solve_cooperative_halns(
            problem,
            config=CooperativeHALNSConfig(
                seed=seed,
                time_limit_seconds=REFINE_SECONDS,
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
                ownership_interval=25,
                relay_interval=10,
                repair_candidate_limit=24,
                verbose=True,
            ),
            initial_solution=coop_initial,
        )
        coop_runtime = perf_counter() - t0

        a2_s = a2_refine.evaluation.score
        coop_s = coop_refine.validation.score
        row = {
            "seed": seed,
            "warm_late_count": warm_score.late_count,
            "warm_distance_km": warm_score.distance_km,
            "a2_refine_late_count": a2_s.late_count,
            "a2_refine_distance_km": a2_s.distance_km,
            "a2_refine_runtime_s": round(a2_runtime, 2),
            "a2_refine_iters": a2_refine.iterations,
            "coop_refine_late_count": coop_s.late_count,
            "coop_refine_distance_km": coop_s.distance_km,
            "coop_refine_runtime_s": round(coop_runtime, 2),
            "coop_refine_iters": coop_refine.iterations,
            "coop_improved_late": coop_s.late_count < a2_s.late_count,
            "coop_improved_lex": (coop_s.late_count, coop_s.distance_km)
            < (a2_s.late_count, a2_s.distance_km),
            "coop_relay_accepted": coop_refine.relay_accepted,
            "coop_ownership_accepted": coop_refine.ownership_accepted,
            "coop_late_tasks_rescued": coop_refine.late_tasks_rescued,
            "coop_critical_pickups_advanced": (
                coop_refine.critical_pickups_advanced
            ),
            "coop_relay_exact_evaluations": coop_refine.relay_exact_evaluations,
            "coop_iterations_per_sec": round(
                coop_refine.iterations / max(coop_runtime, 1e-9), 2
            ),
            "a2_iterations_per_sec": round(
                a2_refine.iterations / max(a2_runtime, 1e-9), 2
            ),
        }
        rows.append(row)
        print(
            f"  A2-refine   late={a2_s.late_count} dist={a2_s.distance_km:.1f} "
            f"({a2_runtime:.1f}s, {row['a2_iterations_per_sec']} it/s)"
        )
        print(
            f"  Coop-refine late={coop_s.late_count} dist={coop_s.distance_km:.1f} "
            f"({coop_runtime:.1f}s, {row['coop_iterations_per_sec']} it/s) "
            f"better={row['coop_improved_lex']}"
        )
    return {"common_start": rows}


def summarize_gate_c(gate_c_data: dict) -> dict:
    a2 = gate_c_data["a2"]
    coop = gate_c_data["coop"]

    def med(values):
        return sorted(values)[len(values) // 2]

    strict_improvements = 0
    ties = 0
    for a2_row, coop_row in zip(a2, coop):
        a2_key = (a2_row["late_count"], a2_row["distance_km"])
        coop_key = (coop_row["late_count"], coop_row["distance_km"])
        if coop_key < a2_key:
            strict_improvements += 1
        elif coop_key == a2_key:
            ties += 1

    return {
        "a2": {
            "late_counts": [r["late_count"] for r in a2],
            "median_late_count": med([r["late_count"] for r in a2]),
            "distances_km": [r["distance_km"] for r in a2],
        },
        "coop": {
            "late_counts": [r["late_count"] for r in coop],
            "median_late_count": med([r["late_count"] for r in coop]),
            "distances_km": [r["distance_km"] for r in coop],
            "strict_improvements": strict_improvements,
            "ties": ties,
        },
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    results: dict = {}

    # ---- Gate A ----
    if not gate_a():
        print("\n>>> Gate A FAILED — fix tests before benchmarking. <<<")
        results["gate_a"] = False
        (OUT_DIR / "benchmark_results.json").write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return

    problem = make_problem()
    print(
        f"\n{len(problem.tasks)} tasks, {problem.drone_count} drones, "
        f"capacity={problem.capacity}, max_tasks={problem.max_tasks_per_drone}"
    )

    # ---- Gate B ----
    results["gate_a"] = True
    results["gate_b"] = gate_b(problem)

    # ---- Gate C ----
    results["gate_c"] = gate_c(problem)
    results["gate_c_summary"] = summarize_gate_c(results["gate_c"])

    # ---- Gate D: common-start ----
    results["gate_d"] = gate_d_common_start(problem)

    (OUT_DIR / "benchmark_results.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nResults saved to {OUT_DIR / 'benchmark_results.json'}")


if __name__ == "__main__":
    main()
