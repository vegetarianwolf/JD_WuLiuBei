"""0-1 Relay Station Location–Allocation (LAP) model — Stage I.

Variables
---------
``y_r`` = 1 if candidate relay site ``r`` is built.
``x_ir`` = 1 if task ``i`` is *guided* to station ``r`` in the Stage-I initial
allocation.  This is guidance, not a hard decision: the routing heuristic may
still insert the task directly, and Stage II may change the station or revert
the relay.

Constraints
-----------
- ``sum_r y_r = P`` (fixed number of stations, an infrastructure budget).
- ``sum_r x_ir <= 1`` for every task (at most one preferred station).
- ``x_ir <= y_r`` for every pair (only built stations can be allocated).
- ``x_ir = 0`` when station ``r`` is task ``i``'s own pickup or delivery node.

Objective (Stage-I surrogate ONLY, never the final scheduling objective)
-----------------------------------------------------------------------
``maximize sum_i sum_r B_ir * x_ir``  where ``B_ir`` is the normalised
task-station benefit from the location helper (geometry, urgency, holding /
capacity release, upstream blocker release and receiver accessibility).

The final scheduling objective remains the strict two-layer
``(late_count, distance_km)``; ``B_ir`` never enters it.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from types import MappingProxyType
from typing import Mapping, Sequence

from .model import Problem
from .relay_model import RELAY_ENDPOINT_EPS, RelayStation

#: Number of candidate stations kept per task before building the model.
LOCATION_TASK_SITE_SHORTLIST = 10

#: Strict time limit (seconds) for the MILP solve.
LOCATION_ALLOCATION_TIME_LIMIT = 12.0


@dataclass(frozen=True, slots=True)
class LocationAllocationResult:
    """Output of the Stage-I location–allocation model."""

    selected_station_ids: tuple[int, ...]
    task_station_guidance: Mapping[int, int]
    model_status: str
    model_runtime: float
    objective_value: float
    reduced_candidate_count: int
    solver: str


def _endpoint_blocked(
    problem: Problem, task_id: int, station: RelayStation
) -> bool:
    task = problem.task(task_id)
    return (
        station.point.distance_to(task.pickup) <= RELAY_ENDPOINT_EPS
        or station.point.distance_to(task.delivery) <= RELAY_ENDPOINT_EPS
    )


def _reduce_pairs(
    problem: Problem,
    candidates: Sequence[RelayStation],
    b_ir: Mapping[tuple[int, int], float],
    *,
    task_site_shortlist: int,
) -> tuple[list[tuple[int, int, float]], list[int], list[RelayStation]]:
    """Keep the top-L station pairs per task and drop useless stations.

    Returns ``(pairs, active_tasks, active_stations)`` where each pair is
    ``(task_id, station_id, coefficient)``.
    """

    candidate_by_id = {station.id: station for station in candidates}
    pairs: list[tuple[int, int, float]] = []
    used_station_ids: set[int] = set()
    active_tasks: list[int] = []
    for task_id in problem.task_ids:
        ranked = []
        for station in candidates:
            if _endpoint_blocked(problem, task_id, station):
                continue
            coefficient = b_ir.get((task_id, station.id), 0.0)
            if coefficient <= 0.0:
                continue
            ranked.append((coefficient, station.id))
        ranked.sort(reverse=True)
        ranked = ranked[:task_site_shortlist]
        for coefficient, station_id in ranked:
            pairs.append((task_id, station_id, coefficient))
            used_station_ids.add(station_id)
        if ranked:
            active_tasks.append(task_id)
    active_stations = [
        candidate_by_id[sid]
        for sid in sorted(used_station_ids)
        if sid in candidate_by_id
    ]
    return pairs, active_tasks, active_stations


def solve_location_allocation(
    problem: Problem,
    candidates: Sequence[RelayStation],
    b_ir: Mapping[tuple[int, int], float],
    num_stations: int,
    *,
    task_site_shortlist: int = LOCATION_TASK_SITE_SHORTLIST,
    time_limit: float = LOCATION_ALLOCATION_TIME_LIMIT,
    fallback_layout: Sequence[RelayStation] | None = None,
    seed: int = 2026080500,
) -> LocationAllocationResult:
    """Solve the 0-1 location–allocation model (scipy MILP with fallback).

    Solver policy: use ``scipy.optimize.milp`` when importable; on timeout
    keep the best incumbent; if no feasible incumbent exists, fall back to the
    provided greedy layout (or an empty guidance).
    """

    started = perf_counter()
    pairs, active_tasks, active_stations = _reduce_pairs(
        problem, candidates, b_ir, task_site_shortlist=task_site_shortlist
    )
    if len(active_stations) < num_stations:
        # Too few useful candidates: pad with the next best unused sites.
        used = {station.id for station in active_stations}
        padding = [
            station
            for station in candidates
            if station.id not in used
        ][: num_stations - len(active_stations)]
        active_stations = active_stations + tuple(padding)

    selected_ids: tuple[int, ...] = ()
    guidance: dict[int, int] = {}
    status = "unknown"
    objective = 0.0
    solver = "scipy.milp"

    try:
        from scipy.optimize import Bounds, LinearConstraint, milp
        import numpy as np

        station_ids = [station.id for station in active_stations]
        station_index = {sid: idx for idx, sid in enumerate(station_ids)}
        pair_index = {
            (task_id, station_id): idx
            for idx, (task_id, station_id, _) in enumerate(pairs)
        }

        n_y = len(active_stations)
        n_x = len(pairs)
        n_vars = n_y + n_x

        # Objective: maximize sum B_ir x_ir  ->  minimize -sum.
        c = np.zeros(n_vars)
        for (task_id, station_id, coefficient) in pairs:
            c[n_y + pair_index[(task_id, station_id)]] = -coefficient

        integrality = np.ones(n_vars)
        bounds = Bounds(np.zeros(n_vars), np.ones(n_vars))

        constraints: list[LinearConstraint] = []

        # sum_r y_r = P
        row_y = np.zeros(n_vars)
        row_y[:n_y] = 1.0
        constraints.append(LinearConstraint(row_y, num_stations, num_stations))

        # sum_r x_ir <= 1 per task
        task_pairs: dict[int, list[tuple[int, int]]] = {}
        for (task_id, station_id, _) in pairs:
            task_pairs.setdefault(task_id, []).append((task_id, station_id))
        for task_id in active_tasks:
            row = np.zeros(n_vars)
            for pair in task_pairs[task_id]:
                row[n_y + pair_index[pair]] = 1.0
            constraints.append(LinearConstraint(row, 0, 1))

        # x_ir - y_r <= 0 per pair
        for (task_id, station_id, _) in pairs:
            row = np.zeros(n_vars)
            row[n_y + pair_index[(task_id, station_id)]] = 1.0
            row[station_index[station_id]] = -1.0
            constraints.append(LinearConstraint(row, -np.inf, 0))

        result = milp(
            c=c,
            constraints=constraints,
            integrality=integrality,
            bounds=bounds,
            options={"time_limit": max(0.1, time_limit)},
        )

        if result.x is not None and result.fun is not None:
            selected_ids = tuple(
                station_ids[idx]
                for idx in range(n_y)
                if result.x[idx] > 0.5
            )
            for (task_id, station_id, _) in pairs:
                if result.x[n_y + pair_index[(task_id, station_id)]] > 0.5:
                    guidance[task_id] = station_id
            objective = -float(result.fun)
            if result.status == 0:
                status = "optimal"
            elif result.status == 1:
                status = "time_limit_incumbent"
            elif result.status == 2:
                status = "infeasible"
            else:
                status = f"status_{result.status}"
            solver = "scipy.milp"
        else:
            status = f"no_incumbent_status_{result.status}"
    except (ImportError, Exception) as error:  # pragma: no cover - env dependent
        status = f"milp_unavailable:{type(error).__name__}"

    if not selected_ids or len(selected_ids) != num_stations:
        # Fallback: greedy layout with argmax guidance.
        if fallback_layout:
            selected_ids = tuple(station.id for station in fallback_layout)
            status = "fallback_greedy"
            solver = "greedy_fallback"
        else:
            selected_ids = tuple(station.id for station in active_stations[:num_stations])
            status = "fallback_topk"
            solver = "topk_fallback"
        if not guidance:
            fallback_ids = set(selected_ids)
            for task_id in problem.task_ids:
                best = None
                best_value = 0.0
                for station in candidates:
                    if station.id not in fallback_ids:
                        continue
                    value = b_ir.get((task_id, station.id), 0.0)
                    if value > best_value:
                        best_value = value
                        best = station.id
                if best is not None:
                    guidance[task_id] = best
        objective = sum(
            b_ir.get((task_id, sid), 0.0)
            for task_id, sid in guidance.items()
        )

    return LocationAllocationResult(
        selected_station_ids=selected_ids,
        task_station_guidance=MappingProxyType(guidance),
        model_status=status,
        model_runtime=perf_counter() - started,
        objective_value=objective,
        reduced_candidate_count=len(active_stations),
        solver=solver,
    )
