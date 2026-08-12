"""Balanced pickup ownership reassignment operators.

In the 200 = 8 × 25 setting every drone must eventually perform exactly
25 pickups.  Single-task relocation ("UAV A → UAV B") is therefore
almost always infeasible unless it is paired with a compensating move.

This module provides *balanced* exchange primitives that preserve the
per-drone pickup count invariant while allowing the solver to change
which drone picks up which task.

All operators are **pure functions** that return new ``Routes`` tuples;
they never mutate inputs.
"""

from __future__ import annotations

from collections import Counter
from typing import Sequence

from .model import Problem
from .physical_lower_bound import pickup_risk, pickup_slack
from .search import Route, RouteEvaluator, Routes


# ---------------------------------------------------------------------------
# Structural helpers
# ---------------------------------------------------------------------------


def compute_pickup_owner(routes: Routes) -> dict[int, int]:
    """Map each task → drone index that performs its pickup."""
    owner: dict[int, int] = {}
    for drone, route in enumerate(routes):
        for visit in route:
            if visit > 0:
                owner[visit] = drone
    return owner


def compute_pickup_profile(routes: Routes) -> dict[int, int]:
    """Return per-drone pickup counts."""
    return {drone: sum(1 for v in route if v > 0) for drone, route in enumerate(routes)}


def validate_pickup_counts(
    routes: Routes, max_tasks_per_drone: int
) -> bool:
    """Check that every drone respects the task-count upper bound."""
    profile = compute_pickup_profile(routes)
    return all(count <= max_tasks_per_drone for count in profile.values())


def _task_on_drone(routes: Routes, drone: int, task_id: int) -> bool:
    """True if *task_id* pickup is on *drone*."""
    return task_id in (v for v in routes[drone] if v > 0)


def _remove_task_from_route(route: Route, task_id: int) -> Route:
    """Return *route* with both pickup and delivery of *task_id* removed."""
    return tuple(v for v in route if abs(v) != task_id)


def _pickup_position(route: Route, task_id: int) -> int:
    """Index of pickup visit *task_id* in *route*, or -1."""
    for i, v in enumerate(route):
        if v == task_id:
            return i
    return -1


def _delivery_position(route: Route, task_id: int) -> int:
    """Index of delivery visit *-task_id* in *route*, or -1."""
    for i, v in enumerate(route):
        if v == -task_id:
            return i
    return -1


# ---------------------------------------------------------------------------
# Pickup urgency metrics
# ---------------------------------------------------------------------------


def pickup_risk_score(
    problem: Problem,
    task_id: int,
    pickup_time: float,
) -> float:
    """Convenience wrapper around :func:`~physical_lower_bound.pickup_risk`."""
    return pickup_risk(problem, task_id, pickup_time)


def compute_pickup_risks(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
) -> dict[int, float]:
    """Compute pickup-risk for every task given current *routes*."""
    risks: dict[int, float] = {}
    for drone, route in enumerate(routes):
        metrics = evaluator.evaluate(route)
        for task_id in (v for v in route if v > 0):
            pt = metrics.pickup_times_min.get(task_id, float("inf"))
            risks[task_id] = pickup_risk(problem, task_id, pt)
    return risks


# ---------------------------------------------------------------------------
# Balanced exchange primitives
# ---------------------------------------------------------------------------


def balanced_pair_exchange(
    routes: Routes,
    drone_a: int,
    drone_b: int,
    task_i: int,
    task_j: int,
) -> Routes | None:
    """1 ↔ 1 ownership exchange.

    - *task_i* moves from *drone_a* → *drone_b*
    - *task_j* moves from *drone_b* → *drone_a*

    After the exchange each drone has the same pickup count as before.
    Returns ``None`` if preconditions are violated.
    """
    if drone_a == drone_b:
        return None
    if task_i == task_j:
        return None
    if not _task_on_drone(routes, drone_a, task_i):
        return None
    if not _task_on_drone(routes, drone_b, task_j):
        return None

    mutable = [list(r) for r in routes]

    # Remove task_i from drone_a, task_j from drone_b
    mutable[drone_a] = list(_remove_task_from_route(routes[drone_a], task_i))
    mutable[drone_b] = list(_remove_task_from_route(routes[drone_b], task_j))

    # Insert task_j's pickup+delivery into drone_a, task_i's into drone_b.
    # We simply append both to the end of the route — the caller is expected
    # to run the ALNS repair on the swapped routes afterwards.
    mutable[drone_a].append(task_j)
    mutable[drone_a].append(-task_j)
    mutable[drone_b].append(task_i)
    mutable[drone_b].append(-task_i)

    return tuple(tuple(r) for r in mutable)


def balanced_two_by_two_exchange(
    routes: Routes,
    drone_a: int,
    drone_b: int,
    tasks_a: tuple[int, int],
    tasks_b: tuple[int, int],
) -> Routes | None:
    """2 ↔ 2 ownership exchange.

    Two tasks move A→B and two move B→A, preserving counts on both drones.
    """
    if drone_a == drone_b:
        return None
    for t in tasks_a:
        if not _task_on_drone(routes, drone_a, t):
            return None
    for t in tasks_b:
        if not _task_on_drone(routes, drone_b, t):
            return None

    mutable = [list(r) for r in routes]
    for t in tasks_a:
        mutable[drone_a] = list(_remove_task_from_route(tuple(mutable[drone_a]), t))
    for t in tasks_b:
        mutable[drone_b] = list(_remove_task_from_route(tuple(mutable[drone_b]), t))

    for t in tasks_b:
        mutable[drone_a].append(t)
        mutable[drone_a].append(-t)
    for t in tasks_a:
        mutable[drone_b].append(t)
        mutable[drone_b].append(-t)

    return tuple(tuple(r) for r in mutable)


def balanced_three_cycle(
    routes: Routes,
    drone_a: int,
    drone_b: int,
    drone_c: int,
    task_a: int,   # moves A → B
    task_b: int,   # moves B → C
    task_c: int,   # moves C → A
) -> Routes | None:
    """3-UAV cyclic ownership exchange.

    Each drone gives one task and receives one, preserving counts.
    """
    drones = {drone_a, drone_b, drone_c}
    if len(drones) != 3:
        return None
    if not _task_on_drone(routes, drone_a, task_a):
        return None
    if not _task_on_drone(routes, drone_b, task_b):
        return None
    if not _task_on_drone(routes, drone_c, task_c):
        return None

    mutable = [list(r) for r in routes]
    mutable[drone_a] = list(_remove_task_from_route(routes[drone_a], task_a))
    mutable[drone_b] = list(_remove_task_from_route(routes[drone_b], task_b))
    mutable[drone_c] = list(_remove_task_from_route(routes[drone_c], task_c))

    # task_c → drone_a, task_a → drone_b, task_b → drone_c
    mutable[drone_a].append(task_c)
    mutable[drone_a].append(-task_c)
    mutable[drone_b].append(task_a)
    mutable[drone_b].append(-task_a)
    mutable[drone_c].append(task_b)
    mutable[drone_c].append(-task_b)

    return tuple(tuple(r) for r in mutable)


# ---------------------------------------------------------------------------
# Balanced destroy-repair
# ---------------------------------------------------------------------------


def balanced_destroy_repair(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    removed_tasks: Sequence[int],
    *,
    max_tasks_per_drone: int | None = None,
) -> Routes | None:
    """Remove *removed_tasks* then re-insert them with a balanced assignment.

    The re-insertion prefers drones below their task-count target (n / M).
    If *max_tasks_per_drone* is not given, it is taken from *problem*.

    Returns a new ``Routes`` with all tasks inserted, or ``None`` if a
    feasible balanced insertion cannot be found.
    """
    if max_tasks_per_drone is None:
        max_tasks_per_drone = problem.max_tasks_per_drone

    target = len(problem.tasks) // problem.drone_count

    # Phase 1: remove tasks — first cache pickup slack BEFORE destroy
    slack_cache: dict[int, float] = {}
    mutable = [list(r) for r in routes]
    for tid in removed_tasks:
        owner = compute_pickup_owner(tuple(tuple(r) for r in mutable))
        if tid not in owner:
            return None
        drone = owner[tid]
        # Compute actual pickup time BEFORE removing the task
        try:
            metrics = evaluator.evaluate(tuple(mutable[drone]))
            pt = metrics.pickup_times_min.get(tid, float("inf"))
            slack_cache[tid] = pickup_slack(problem, tid, pt)
        except (ValueError, RuntimeError):
            slack_cache[tid] = float("inf")
        mutable[drone] = list(
            _remove_task_from_route(tuple(mutable[drone]), tid)
        )

    # Phase 2: re-insert with balanced assignment, sorted by urgency
    partial = tuple(tuple(r) for r in mutable)
    remaining = list(removed_tasks)
    # Most urgent first (most negative pickup_slack)
    remaining.sort(key=lambda tid: slack_cache.get(tid, float("inf")))

    for tid in remaining:
        # Pick drones below their target count first
        profile = compute_pickup_profile(partial)
        candidates = sorted(
            [d for d in range(problem.drone_count) if profile.get(d, 0) < max_tasks_per_drone],
            key=lambda d: profile.get(d, 0),  # fewest tasks first
        )
        if not candidates:
            return None  # all drones full

        best: tuple[int, Route, float] | None = None
        for drone in candidates:
            # Try simple append: pickup then delivery at end
            candidate_route = tuple(partial[drone]) + (tid, -tid)
            try:
                metrics = evaluator.evaluate(candidate_route)
                # Use (late_count, distance) as insertion cost
                cost = float(metrics.score.late_count) * 1e6 + metrics.score.distance_km
            except ValueError:
                continue
            if best is None or cost < best[2]:
                best = (drone, candidate_route, cost)

        if best is None:
            return None

        mutable2 = list(partial)
        mutable2[best[0]] = best[1]
        partial = tuple(tuple(r) for r in mutable2)

    return partial


# ---------------------------------------------------------------------------
# Critical-task identification
# ---------------------------------------------------------------------------


def identify_critical_tasks(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    top_k: int = 10,
) -> list[tuple[int, float, int]]:
    """Return the Top-K most pickup-critical tasks.

    Each entry is ``(task_id, pickup_slack, drone)`` sorted by
    *ascending* slack (most negative first).

    Routes that contain relay-split visits (pickup without delivery on the
    same drone) are handled gracefully: if per-route evaluation fails, the
    function falls back to computing pickup times directly from the route
    traversal.
    """
    entries: list[tuple[int, float, int]] = []
    for drone, route in enumerate(routes):
        # Try per-route evaluator first (fast)
        try:
            metrics = evaluator.evaluate(route)
            for visit in route:
                if visit > 0:
                    pt = metrics.pickup_times_min.get(visit, float("inf"))
                    slack = pickup_slack(problem, visit, pt)
                    entries.append((visit, slack, drone))
            continue
        except (ValueError, RuntimeError):
            pass

        # Fallback: compute pickup times by traversing the route directly
        elapsed = 0.0
        prev: int | None = None
        for visit in route:
            elapsed += problem.distance(prev, visit) / problem.speed_km_per_min
            prev = visit
            if visit > 0:
                slack = pickup_slack(problem, visit, elapsed)
                entries.append((visit, slack, drone))

    entries.sort(key=lambda x: x[1])  # most negative first
    return entries[:top_k]


def find_upstream_blockers(
    problem: Problem,
    evaluator: RouteEvaluator,
    route: Route,
    critical_task_id: int,
) -> list[tuple[int, float]]:
    """Find tasks whose delivery occupies the drone before *critical_task_id*.

    Returns ``[(blocker_task_id, holding_time_min), …]`` sorted by
    descending holding time.
    """
    crit_pos = _pickup_position(route, critical_task_id)
    if crit_pos < 0:
        return []

    # Try per-route evaluator first
    try:
        metrics = evaluator.evaluate(route)
    except (ValueError, RuntimeError):
        # Fallback: compute traversal times directly
        return _blockers_by_traversal(problem, route, crit_pos)

    blockers: list[tuple[int, float]] = []
    for i, visit in enumerate(route):
        if i >= crit_pos:
            break
        if visit < 0:  # delivery of some task
            tid = abs(visit)
            pickup_t = metrics.pickup_times_min.get(tid, 0.0)
            delivery_t = metrics.delivery_times_min.get(tid, 0.0)
            holding = delivery_t - pickup_t
            if holding > 0:
                blockers.append((tid, holding))
    blockers.sort(key=lambda x: -x[1])
    return blockers


def _blockers_by_traversal(
    problem: Problem, route: Route, crit_pos: int,
) -> list[tuple[int, float]]:
    """Fallback blocker detection via direct route traversal."""
    pickup_times: dict[int, float] = {}
    delivery_times: dict[int, float] = {}
    elapsed = 0.0
    prev: int | None = None
    for i, visit in enumerate(route):
        elapsed += problem.distance(prev, visit) / problem.speed_km_per_min
        prev = visit
        if visit > 0:
            pickup_times[abs(visit)] = elapsed
        else:
            delivery_times[abs(visit)] = elapsed

    blockers: list[tuple[int, float]] = []
    for i, visit in enumerate(route):
        if i >= crit_pos:
            break
        if visit < 0:
            tid = abs(visit)
            pt = pickup_times.get(tid, 0.0)
            dt = delivery_times.get(tid, 0.0)
            holding = dt - pt
            if holding > 0:
                blockers.append((tid, holding))
    blockers.sort(key=lambda x: -x[1])
    return blockers
