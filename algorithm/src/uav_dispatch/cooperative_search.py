"""Legacy compatibility layer for the Cooperative solver.

The actual Pickup-First Cooperative HALNS now lives in
:mod:`uav_dispatch.cooperative_alns` (unified DIRECT / OWNERSHIP / RELAY /
BLOCKER-RELAY / SWAP / DYNAMIC-STATION search inside one ALNS loop).

This module keeps the historical public surface working:
- re-exports :class:`ServiceMode` / :class:`TaskServiceState` (canonical
  definitions in :mod:`uav_dispatch.cooperative_model`);
- keeps the old helper functions used by legacy tests;
- ``solve_dynamic_cooperative`` is now a thin compatibility wrapper that
  builds a :class:`~uav_dispatch.cooperative_alns.CooperativeHALNSConfig`
  from the legacy :class:`CooperativeConfig` and delegates to
  :func:`~uav_dispatch.cooperative_alns.solve_cooperative_halns`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from random import Random
from time import perf_counter
from typing import TYPE_CHECKING

from .alns import (
    _SearchDeadlineReached,
)
from .cooperative_alns import (
    CooperativeHALNSConfig,
    solve_cooperative_halns,
)
from .cooperative_model import (
    ServiceMode,
    TaskServiceState,
)
from .cooperative_validation import (
    CooperativeSolution,
    CooperativeValidation,
    validate_cooperative_solution,
)
from .dynamic_relay import (
    DynamicRelayConfig,
    DynamicRelayManager,
)
from .model import Problem
from .ownership_reassignment import (
    find_upstream_blockers,
)
from .physical_lower_bound import pickup_slack
from .relay_candidates import (
    relay_candidates_for_task,
    apply_relay_candidate,
)
from .relay_model import RelayStation, RelayTransfer
from .relay_validation import evaluate_relay_solution
from .search import RouteEvaluator, Routes

if TYPE_CHECKING:
    from .swap_model import SwapEvent
    from .swap_search import SwapConfig


@dataclass
class CooperativeConfig:
    """Configuration for the Dynamic Cooperative solver.

    Time budget is split into three configurable phases:
    - A2 warm-start (default 20%)
    - Cooperative construction (default 30%)
    - Persistent cooperative ALNS (default 50%)
    """

    seed: int = 2026080500
    time_limit_seconds: float = 240.0
    candidate_limit: int = 48

    # -- time-budget shares (must sum to <= 1.0) --
    a2_warm_start_share: float = 0.20
    coop_construction_share: float = 0.30
    coop_alns_share: float = 0.50

    # -- feature toggles --
    enable_relay: bool = True
    enable_ownership_exchange: bool = True
    enable_swap: bool = True
    enable_batch_relay: bool = True

    # -- cooperative loop control --
    cooperative_iterations: int = 50
    station_refresh_every: int = 10
    marginal_activation_threshold: float = 0.05

    # -- sub-component configs --
    dynamic_relay: DynamicRelayConfig = field(default_factory=DynamicRelayConfig)
    swap_config: "SwapConfig | None" = None  # auto-construct default if None

    # -- refinement phase --
    exchange_rounds: int = 8
    relay_rounds: int = 3

    # -- deprecated --
    a2_share: float = 0.90  # deprecated; use a2_warm_start_share

    verbose: bool = True


@dataclass
class CooperativeResult:
    """Full result of the Dynamic Cooperative solver."""

    solution: CooperativeSolution
    validation: CooperativeValidation
    runtime_seconds: float
    iterations: int

    # -- warm-start stats --
    stage1_late_count: int = 0
    stage1_pickup_late_initial: int = 0
    stage1_pickup_late_final: int = 0

    # -- ownership exchange stats --
    ownership_changes: int = 0
    pair_exchanges: int = 0
    cycle_exchanges: int = 0
    ownership_attempts: int = 0
    ownership_feasible: int = 0
    ownership_accepted: int = 0
    ownership_improving: int = 0
    # per-operator: attempts/feasible/accepted
    op_stats: dict = field(default_factory=dict)

    # -- relay stats --
    relay_count: int = 0
    relay_attempts: int = 0
    relay_accepted: int = 0
    relay_successes: int = 0
    blocker_relay_count: int = 0
    downstream_rescued: int = 0
    batch_relay_count: int = 0

    # -- station stats --
    active_station_count: int = 0
    station_adds: int = 0
    station_drops: int = 0
    station_replaces: int = 0
    station_trajectory: list = field(default_factory=list)

    # -- swap stats --
    swap_candidates: int = 0
    swap_accepted: int = 0
    swap_improving: int = 0
    final_swap_count: int = 0

    # -- service distribution --
    service_distribution: dict = field(default_factory=dict)

    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Task service abstraction
# ---------------------------------------------------------------------------
# Canonical definitions live in cooperative_model.py (the unified
# Cooperative-HALNS state model).  Re-exported here for backward
# compatibility with the legacy API.
#
#   ServiceMode      = cooperative_model.ServiceMode
#   TaskServiceState = cooperative_model.TaskServiceState


def _check_service_invariants(
    solution: CooperativeSolution,
    max_tasks_per_drone: int,
    task_id_set: set[int],
) -> list[str]:
    """Verify every task has exactly one consistent service record."""
    violations: list[str] = []
    svc = solution.task_services

    for tid in sorted(task_id_set):
        if tid not in svc:
            violations.append(f"Task {tid}: missing TaskServiceState")
            continue
        st = svc[tid]
        if st.mode == ServiceMode.DIRECT:
            if st.primary_owner != st.delivery_owner:
                violations.append(
                    f"DIRECT task {tid}: primary_owner != delivery_owner"
                )
            if st.relay_event is not None:
                violations.append(f"DIRECT task {tid}: has relay event")
        elif st.mode == ServiceMode.RELAY:
            if st.relay_event is None:
                violations.append(f"RELAY task {tid}: missing relay_event")
            if st.relay_event is not None:
                if st.primary_owner != st.relay_event.first_drone:
                    violations.append(
                        f"RELAY task {tid}: pickup_owner != relay.first_drone"
                    )
                if st.delivery_owner != st.relay_event.second_drone:
                    violations.append(
                        f"RELAY task {tid}: delivery_owner != relay.second_drone"
                    )
        else:
            violations.append(f"Task {tid}: unknown service mode {st.mode.name}")

    # K-constraint: primary_owner pickup counts
    primary_counts: dict[int, int] = {}
    for tid, st in svc.items():
        primary_counts[st.primary_owner] = primary_counts.get(st.primary_owner, 0) + 1
    for drone, count in primary_counts.items():
        if count > max_tasks_per_drone:
            violations.append(
                f"Drone {drone}: primary_owner pickups {count} > {max_tasks_per_drone}"
            )

    return violations


# ---------------------------------------------------------------------------
# Cooperative construction helpers
# ---------------------------------------------------------------------------


def _build_initial_task_services(
    problem: Problem, evaluator: RouteEvaluator, routes: Routes,
) -> dict[int, TaskServiceState]:
    """Build all-DIRECT TaskServiceState from initial A2 routes."""
    services: dict[int, TaskServiceState] = {}
    for drone, route in enumerate(routes):
        for visit in route:
            if visit > 0:
                tid = visit
                services[tid] = TaskServiceState(
                    task_id=tid,
                    mode=ServiceMode.DIRECT,
                    primary_owner=drone,
                    delivery_owner=drone,
                )
    return services


def _build_coop_solution(
    routes: Routes,
    task_services: dict[int, TaskServiceState],
    relays: tuple[RelayTransfer, ...] = (),
    swaps: tuple[object, ...] = (),
    active_stations: tuple[RelayStation, ...] = (),
) -> CooperativeSolution:
    """Build CooperativeSolution with all fields populated."""
    ownership = {
        tid: st.primary_owner for tid, st in task_services.items()
    }
    return CooperativeSolution(
        routes=routes,
        relays=relays,
        swaps=swaps,
        active_stations=active_stations,
        ownership=ownership,
        task_services=task_services,
    )


def _targeted_ownership_move(
    problem: Problem,
    evaluator: RouteEvaluator,
    solution: CooperativeSolution,
    task_i: int,
    drone_a: int,
    task_j: int,
    drone_b: int,
    cfg: CooperativeConfig,
) -> CooperativeSolution | None:
    """Exchange task_i (A -> B) and task_j (B -> A) with *fixed-target*
    insertion.

    This is a true ownership reassignment: after removing both tasks, task_i
    may only be re-inserted on drone B and task_j only on drone A.  A move
    that silently slides ownership back to the original owner is impossible
    by construction (the old code re-ran a global ALNS repair, which could
    send tasks home — that pseudo-exchange is removed here).
    """
    from .cooperative_alns import (
        _insert_direct as _insert_direct_op,
        _remove_task_services as _remove_svc,
    )

    halns_cfg = CooperativeHALNSConfig(candidate_limit=cfg.candidate_limit)
    partial, _removed = _remove_svc(problem, solution, [task_i, task_j])
    after_i = _insert_direct_op(
        problem, evaluator, partial, task_i, drone_b, halns_cfg
    )
    if after_i is None:
        return None
    after_both = _insert_direct_op(
        problem, evaluator, after_i, task_j, drone_a, halns_cfg
    )
    if after_both is None:
        return None
    services = after_both.task_services
    # Guarantee: accepted ownership move must actually change the owner.
    if (
        task_i not in services
        or task_j not in services
        or services[task_i].primary_owner != drone_b
        or services[task_j].primary_owner != drone_a
    ):
        return None
    return after_both


def _cooperative_ownership_exchange(
    problem: Problem,
    evaluator: RouteEvaluator,
    solution: CooperativeSolution,
    relay_mgr: "DynamicRelayManager | None",
    rng: Random,
    cfg: CooperativeConfig,
) -> tuple[CooperativeSolution, int, int]:
    """One round of balanced ownership exchange.

    Selects candidate tasks by pickup slack and upstream blockers
    (NOT random), tries a balanced 1-for-1 exchange, then re-inserts each
    task with *fixed-target* repair so the owner genuinely changes.

    Returns (new_solution, pair_exchanges, cycle_exchanges).
    """
    routes = solution.routes
    svc = dict(solution.task_services)

    # Compute current pickup slacks
    task_slacks: dict[int, float] = {}
    for drone, route in enumerate(routes):
        try:
            metrics = evaluator.evaluate(route)
        except (ValueError, RuntimeError):
            continue
        for visit in route:
            if visit > 0:
                pt = metrics.pickup_times_min.get(visit, float("inf"))
                task_slacks[visit] = pickup_slack(problem, visit, pt)

    # Identify exchange candidates: prioritize borderline slack tasks
    # (where ownership change CAN help), not hopelessly late tasks.
    # Sort: rescue_class first (borderline > deeply late), then abs(slack).
    def _rescue_priority(tid_slack: tuple[int, float]) -> tuple[int, float]:
        _, sl = tid_slack
        if -10 <= sl <= 10:       # borderline — highest priority
            return (0, abs(sl))
        elif -30 <= sl < -10:     # moderately late
            return (1, abs(sl))
        elif 10 < sl <= 30:       # loose but near
            return (2, abs(sl))
        else:                      # deeply late or very loose
            return (3, abs(sl))

    exchange_candidates = sorted(
        [(tid, s) for tid, s in task_slacks.items() if s < 10.0],
        key=_rescue_priority,
    )[:15]
    if not exchange_candidates:
        return solution, 0, 0

    best_solution = solution
    current_val = validate_cooperative_solution(problem, solution)
    best_score = current_val.score
    pair_count = 0
    cycle_count = 0
    tried_pairs: set[tuple[int, int]] = set()

    def _drone_of(task_id: int) -> int:
        state = svc.get(task_id)
        if state is not None:
            return state.primary_owner
        return -1

    for crit_tid, _crit_slack in exchange_candidates:
        crit_drone = _drone_of(crit_tid)
        if crit_drone < 0:
            continue

        # Strategy A: exchange the critical task itself with a loose task on
        # another drone (fixed-target insertion).
        best_partner_drone = -1
        best_partner_tid = -1
        best_partner_slack = float("-inf")
        for d in range(problem.drone_count):
            if d == crit_drone:
                continue
            for tid, sl in task_slacks.items():
                if _drone_of(tid) != d:
                    continue
                if sl > best_partner_slack:
                    best_partner_slack = sl
                    best_partner_drone = d
                    best_partner_tid = tid

        if (
            best_partner_drone >= 0
            and (crit_tid, best_partner_tid) not in tried_pairs
        ):
            tried_pairs.add((crit_tid, best_partner_tid))
            try:
                new_sol = _targeted_ownership_move(
                    problem, evaluator, solution,
                    crit_tid, crit_drone,
                    best_partner_tid, best_partner_drone,
                    cfg,
                )
                if new_sol is not None:
                    val = validate_cooperative_solution(problem, new_sol)
                    if val.valid and val.score < best_score:
                        best_solution = new_sol
                        best_score = val.score
                        pair_count += 1
            except (_SearchDeadlineReached, RuntimeError, ValueError):
                pass

        # Strategy B: exchange an upstream blocker of the critical task with
        # a loose task on another drone.
        try:
            blockers = find_upstream_blockers(
                problem, evaluator, routes[crit_drone], crit_tid
            )
        except (ValueError, RuntimeError):
            blockers = []

        for blocker_tid, _ in blockers[:3]:
            if (blocker_tid, crit_tid) in tried_pairs:
                continue
            tried_pairs.add((blocker_tid, crit_tid))

            best_d2, best_t2, best_s2 = -1, -1, float("-inf")
            for d in range(problem.drone_count):
                if d == crit_drone:
                    continue
                for tid, sl in task_slacks.items():
                    if _drone_of(tid) != d:
                        continue
                    if sl > best_s2:
                        best_s2, best_d2, best_t2 = sl, d, tid

            if best_d2 < 0:
                continue
            try:
                new_sol2 = _targeted_ownership_move(
                    problem, evaluator, solution,
                    blocker_tid, crit_drone,
                    best_t2, best_d2,
                    cfg,
                )
                if new_sol2 is not None:
                    val2 = validate_cooperative_solution(problem, new_sol2)
                    if val2.valid and val2.score < best_score:
                        best_solution = new_sol2
                        best_score = val2.score
                        pair_count += 1
            except (_SearchDeadlineReached, RuntimeError, ValueError):
                pass

    return best_solution, pair_count, cycle_count


def _cooperative_relay_probe(
    problem: Problem,
    solution: CooperativeSolution,
    stations: tuple[RelayStation, ...],
    deadline: float | None,
) -> tuple[CooperativeSolution, int, int]:
    """Try Fresh Relay for tasks with negative or near-zero pickup slack.

    Targets: (a) pickup_slack < 0 (already late), (b) 0 <= slack < 5
    (prevention), (c) upstream blockers of (a)/(b).

    Returns (new_solution, relay_attempts, relay_accepted).
    """
    routes = solution.routes
    svc = dict(solution.task_services)
    evaluator = RouteEvaluator(problem)

    # Compute pickup slacks
    task_slacks: dict[int, float] = {}
    task_drones: dict[int, int] = {}
    for drone, route in enumerate(routes):
        try:
            metrics = evaluator.evaluate(route)
        except (ValueError, RuntimeError):
            continue
        for visit in route:
            if visit > 0:
                pt = metrics.pickup_times_min.get(visit, float("inf"))
                task_slacks[visit] = pickup_slack(problem, visit, pt)
                task_drones[visit] = drone

    # Prevention targets: slack < 0 (critical) + 0 <= slack < 5 (near-critical)
    prevention = sorted(
        [(tid, s) for tid, s in task_slacks.items() if s < 5.0],
        key=lambda x: x[1],
    )[:8]

    if not prevention or not stations:
        return solution, 0, 0

    # Also add upstream blockers of prevention targets
    target_tasks: set[int] = {tid for tid, _ in prevention}
    for tid, _ in prevention[:3]:
        drone = task_drones.get(tid, -1)
        if drone < 0:
            continue
        try:
            blockers = find_upstream_blockers(problem, evaluator, routes[drone], tid)
            for blocker_tid, _ in blockers[:2]:
                if blocker_tid not in target_tasks:
                    target_tasks.add(blocker_tid)
        except (ValueError, RuntimeError):
            pass

    base_rs = solution.to_relay_solution()
    current_val = validate_cooperative_solution(problem, solution)
    best_solution = solution
    best_score = current_val.score
    relay_attempts = 0
    relay_accepted = 0

    for tid in target_tasks:
        if deadline and perf_counter() >= deadline:
            break
        # Only try relay for tasks that are currently DIRECT
        st = svc.get(tid)
        if st is None or st.mode != ServiceMode.DIRECT:
            continue
        try:
            candidates = relay_candidates_for_task(
                problem, base_rs, tid, stations=stations,
                handling_time_min=0.0, max_evaluations=8,
                stop_at_improvement=False, require_all_tasks=False,
                deadline=deadline)
            relay_attempts += len(candidates)
            if candidates:
                best_c = candidates[0]
                ns = apply_relay_candidate(problem, base_rs, best_c)
                nv = evaluate_relay_solution(problem, ns)
                if nv.valid and nv.score < best_score:
                    # Build new CooperativeSolution with relay preserved
                    new_svc = dict(svc)
                    # Find the RelayTransfer that was created
                    new_relay = None
                    for r in ns.relays:
                        if r.task_id == tid:
                            new_relay = r
                            break
                    if new_relay is None:
                        continue
                    new_svc[tid] = TaskServiceState(
                        task_id=tid, mode=ServiceMode.RELAY,
                        primary_owner=new_relay.first_drone,
                        delivery_owner=new_relay.second_drone,
                        relay_event=new_relay)
                    new_stations = tuple(ns.stations)
                    new_sol = _build_coop_solution(
                        ns.routes, new_svc,
                        relays=ns.relays,
                        swaps=solution.swaps,
                        active_stations=new_stations)
                    val = validate_cooperative_solution(problem, new_sol)
                    if val.valid:
                        best_solution = new_sol
                        best_score = nv.score
                        relay_accepted += 1
        except (RuntimeError, ValueError, _SearchDeadlineReached):
            pass

    return best_solution, relay_attempts, relay_accepted


def _cooperative_swap_probe(
    problem: Problem,
    solution: CooperativeSolution,
    rng: Random,
    cfg: CooperativeConfig,
    deadline: float | None,
) -> tuple[CooperativeSolution, int, int]:
    """LEGACY: swap is no longer part of the formal Cooperative path.

    Returns the input solution unchanged (``swap_candidates = 0``,
    ``swap_accepted = 0``).  Kept only so the legacy compat surface does not
    crash; the formal Cooperative-HALNS never produces a swap.
    """
    return solution, 0, 0


# ---------------------------------------------------------------------------
# Main entry point — 20/30/50 budget architecture
# ---------------------------------------------------------------------------


def solve_dynamic_cooperative(
    problem: Problem, *, config: CooperativeConfig | None = None,
) -> CooperativeResult:
    """Compatibility wrapper over :func:`solve_cooperative_halns`.

    The old three-phase "A2 + post-processing" pipeline (ownership exchange /
    relay probe / swap probe as Stage-2 patches) has been replaced by the
    unified Pickup-First Cooperative HALNS in :mod:`cooperative_alns`, where
    DIRECT / OWNERSHIP / RELAY / BLOCKER-RELAY / SWAP / DYNAMIC-STATION are
    all operators inside one ALNS loop.

    This wrapper maps the legacy :class:`CooperativeConfig` onto a
    :class:`~uav_dispatch.cooperative_alns.CooperativeHALNSConfig` and maps
    the result back to the legacy :class:`CooperativeResult` surface so old
    callers and experiments keep working.
    """
    cfg = config or CooperativeConfig()
    wall_start = perf_counter()
    total = cfg.time_limit_seconds

    if cfg.verbose:
        print(
            f"Coop-HALNS (compat wrapper)  |  seed={cfg.seed}  |  "
            f"{total:.0f}s  (unified HALNS)",
            flush=True,
        )

    halns_cfg = CooperativeHALNSConfig(
        seed=cfg.seed,
        time_limit_seconds=total,
        candidate_limit=cfg.candidate_limit,
        initial_time_limit_seconds=total * cfg.a2_warm_start_share,
        enable_ownership=cfg.enable_ownership_exchange,
        enable_relay=cfg.enable_relay,
        enable_delivery_risk_relay=cfg.enable_relay,
        enable_blocker_relay=cfg.enable_relay,
        verbose=cfg.verbose,
    )
    result = solve_cooperative_halns(problem, config=halns_cfg)
    final_sol = result.solution
    final_val = result.validation
    op_stats = result.op_stats

    svc_dist = {"DIRECT": 0, "RELAY": 0, "SWAP": 0}
    for state in final_sol.task_services.values():
        svc_dist[state.mode.name] = svc_dist.get(state.mode.name, 0) + 1

    return CooperativeResult(
        solution=final_sol,
        validation=final_val,
        runtime_seconds=result.runtime_seconds,
        iterations=result.iterations,
        stage1_late_count=result.initial_score.late_count,
        stage1_pickup_late_initial=result.pickup_late_count,
        stage1_pickup_late_final=result.pickup_late_count,
        ownership_changes=op_stats["ownership"]["accepted"],
        pair_exchanges=op_stats["ownership"]["accepted"],
        cycle_exchanges=0,
        ownership_attempts=op_stats["ownership"]["calls"],
        ownership_feasible=op_stats["ownership"]["feasible"],
        ownership_accepted=op_stats["ownership"]["accepted"],
        ownership_improving=op_stats["ownership"]["improving"],
        op_stats={
            "attempts": {name: s["calls"] for name, s in op_stats.items()},
            "accepted": {
                name: s["accepted"] for name, s in op_stats.items()
            },
            "candidates": {
                name: s["candidates"] for name, s in op_stats.items()
            },
            "feasible": {name: s["feasible"] for name, s in op_stats.items()},
            "improving": {
                name: s["improving"] for name, s in op_stats.items()
            },
            "best_improving": {
                name: s["best_improving"] for name, s in op_stats.items()
            },
        },
        relay_count=final_sol.relay_count,
        relay_attempts=op_stats["relay"]["calls"],
        relay_accepted=op_stats["relay"]["accepted"],
        relay_successes=op_stats["relay"]["accepted"],
        blocker_relay_count=op_stats["blocker_relay"]["accepted"],
        downstream_rescued=op_stats["blocker_relay"]["improving"],
        batch_relay_count=final_sol.relay_count,
        active_station_count=result.active_station_count,
        station_adds=result.station_adds,
        station_drops=result.station_drops,
        station_replaces=result.station_replaces,
        station_trajectory=[],
        swap_candidates=op_stats["swap"]["candidates"],
        swap_accepted=op_stats["swap"]["accepted"],
        swap_improving=op_stats["swap"]["improving"],
        final_swap_count=final_sol.swap_count,
        service_distribution=svc_dist,
        metadata=dict(result.metadata),
    )
