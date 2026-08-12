"""Module 0: relay station location design.

Candidate sites are the unique pickup/delivery service nodes (``P ∪ D``); no
artificial coordinates are generated.  Each task-station pair gets a
normalised surrogate score ``B_ir`` that combines static geometry with
route-context terms (urgency, holding pressure, capacity pressure, upstream
release potential and receiver accessibility).  ``B_ir`` is a *location
surrogate only*: it never enters the formal Score.

Layouts are produced by greedy marginal gain followed by 1-for-1 best-
improvement swaps, diversified across greedy/restart trajectories, and finally
ranked by a real operational probe that tries direct rescues and upstream
blocker relays.  The winning layout is frozen as ``FINAL_RELAY_STATIONS``;
later stages may only change task->station assignment, never the stations.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import exp, isfinite
from random import Random
from time import perf_counter
from typing import Iterable, Mapping, Sequence

from .best_known import PickupTimingRecord, pickup_timing_diagnosis
from .model import Problem, Score
from .relay_candidates import (
    RelayCandidate,
    best_station_insert_detour_km,
    relay_candidates_for_task,
)
from .relay_model import (
    NUM_RELAY_STATIONS,
    RelaySolution,
    RelayStation,
    relay_solution_from_routes,
)
from .relay_validation import evaluate_relay_solution

#: Maximum number of diversified layouts kept for the operational probe.
TOP_LAYOUTS = 8

#: Iteration cap for the 1-for-1 station local search.
LOCATION_LOCAL_SEARCH_LIMIT = 200

#: B_ir surrogate coefficients (location design only, never the Score).
COEF_URGENCY = 1.0
COEF_GEOMETRY = 1.0
COEF_DETOUR = 1.0
COEF_RECEIVER = 1.0
COEF_CAPACITY_RELEASE = 0.5
COEF_UPSTREAM_RELEASE = 1.0


@dataclass(frozen=True, slots=True)
class StationLayout:
    """One candidate station set with its surrogate and probe evidence."""

    stations: tuple[RelayStation, ...]
    static_score: float
    probe_late_count: int
    probe_distance_km: float
    feasible_relay_count: int
    direct_rescue_count: int
    downstream_rescue_count: int


@dataclass(frozen=True, slots=True)
class StationDesignResult:
    """Result of Module 0 for one direct seed solution."""

    final_stations: tuple[RelayStation, ...]
    candidate_count: int
    layouts: tuple[StationLayout, ...]
    probe_solution: RelaySolution
    probe_evaluation: object
    runtime_seconds: float


def candidate_sites(problem: Problem) -> tuple[RelayStation, ...]:
    """Unique physical service nodes ``P ∪ D`` as station candidates.

    Nodes are de-duplicated by coordinates; the first task's node in sorted
    task order becomes the ``source_visit``.
    """

    sites: dict[tuple[float, float], RelayStation] = {}
    for task in sorted(problem.tasks, key=lambda item: item.id):
        if (task.pickup.x, task.pickup.y) not in sites:
            sites[(task.pickup.x, task.pickup.y)] = RelayStation(
                len(sites), task.pickup, task.id
            )
        if (task.delivery.x, task.delivery.y) not in sites:
            sites[(task.delivery.x, task.delivery.y)] = RelayStation(
                len(sites), task.delivery, -task.id
            )
    return tuple(sorted(sites.values(), key=lambda station: station.id))


# ---------------------------------------------------------------------------
# Route-context metrics for the direct seed solution.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RouteContext:
    """Per-task route-context facts needed for B_ir and blocker analysis."""

    pickup_time_min: float
    delivery_time_min: float
    deadline_min: float
    drone: int
    pickup_position: int
    pickup_late: bool
    delivery_late: bool


def build_route_context(
    problem: Problem, routes: Sequence[Sequence[int]]
) -> dict[int, RouteContext]:
    """Compute per-task timing facts from a complete direct solution."""

    records, _ = pickup_timing_diagnosis(problem, routes)
    return {
        record.task_id: RouteContext(
            pickup_time_min=record.pickup_time_min,
            delivery_time_min=record.delivery_time_min,
            deadline_min=record.deadline_min,
            drone=record.drone - 1,
            pickup_position=record.pickup_position,
            pickup_late=record.pickup_late,
            delivery_late=record.delivery_late,
        )
        for record in records
    }


def _geometry(problem: Problem, task_id: int, station: RelayStation) -> dict[str, float]:
    task = problem.task(task_id)
    p = task.pickup
    d = task.delivery
    l1 = p.distance_to(station.point)
    l2 = station.point.distance_to(d)
    direct = p.distance_to(d)
    detour = max(0.0, l1 + l2 - direct)
    balance = (
        min(l1, l2) / max(l1, l2) if max(l1, l2) > 1e-12 else 1.0
    )
    return {
        "l1": l1,
        "l2": l2,
        "direct": direct,
        "detour": detour,
        "balance": balance,
    }


def _urgency_factor(context: RouteContext) -> float:
    if context.delivery_late:
        return 1.0
    slack = context.deadline_min - context.delivery_time_min
    return 1.0 / (1.0 + max(0.0, slack))


def _holding_pressure(context: RouteContext, capacity: int) -> float:
    holding = context.delivery_time_min - context.pickup_time_min
    return holding / max(1.0, float(capacity))


def receiver_accessibility(
    problem: Problem,
    routes: Sequence[Sequence[int]],
    task_id: int,
    station: RelayStation,
) -> float:
    """Normalised estimate of how cheaply some other UAV can serve the relay.

    ``1 / (1 + min_k[ best station detour + dist(R, D_i) ])``.
    """

    task = problem.task(task_id)
    best_cost = float("inf")
    for route in routes:
        detour = best_station_insert_detour_km(
            problem, route, station.source_visit
        )
        cost = detour + station.point.distance_to(task.delivery)
        best_cost = min(best_cost, cost)
    if not isfinite(best_cost):
        return 0.0
    return 1.0 / (1.0 + best_cost)


def upstream_release_potential(
    problem: Problem,
    routes: Sequence[Sequence[int]],
    context: Mapping[int, RouteContext],
    task_id: int,
    station: RelayStation,
) -> float:
    """Aggregated blocker-release surrogate for dropping task ``i`` at ``r``.

    For every downstream critical task ``j`` (same route, pickup after ``i``),
    the released time is ``max(0, holding_i - dist(P_i, R)/speed)``.  The
    potential scales with both the released time and the number of
    beneficiaries.
    """

    ctx_i = context[task_id]
    holding = ctx_i.delivery_time_min - ctx_i.pickup_time_min
    release_time = max(
        0.0, holding - problem.task(task_id).pickup.distance_to(station.point)
        / problem.speed_km_per_min
    )
    if release_time <= 0.0:
        return 0.0
    downstream_critical = 0
    for other_id, ctx_j in context.items():
        if ctx_j.drone != ctx_i.drone:
            continue
        if ctx_j.pickup_time_min <= ctx_i.pickup_time_min:
            continue
        if ctx_j.pickup_late or ctx_j.delivery_late:
            downstream_critical += 1
    return release_time * (1.0 + downstream_critical)


def task_station_potential(
    problem: Problem,
    routes: Sequence[Sequence[int]],
    context: Mapping[int, RouteContext],
    task_id: int,
    station: RelayStation,
) -> float:
    """The normalised location surrogate ``B_ir`` (never the Score)."""

    ctx = context[task_id]
    geometry = _geometry(problem, task_id, station)
    detour = geometry["detour"]
    direct = geometry["direct"]

    urgency = _urgency_factor(ctx)
    geometry_factor = exp(-detour / max(1e-9, direct)) * geometry["balance"]
    detour_penalty = 1.0 / (1.0 + detour)
    receiver = receiver_accessibility(problem, routes, task_id, station)
    capacity_release = min(
        1.0, _holding_pressure(ctx, problem.capacity)
    )
    upstream = upstream_release_potential(
        problem, routes, context, task_id, station
    )
    upstream_bonus = min(1.0, upstream / max(1.0, direct))

    return (
        (COEF_URGENCY * urgency)
        * (COEF_GEOMETRY * geometry_factor)
        * (COEF_DETOUR * detour_penalty)
        * (COEF_RECEIVER * receiver)
        * (
            1.0
            + COEF_CAPACITY_RELEASE * capacity_release
            + COEF_UPSTREAM_RELEASE * upstream_bonus
        )
    )


# ---------------------------------------------------------------------------
# Layout selection (uses a precomputed B_ir matrix for speed).
# ---------------------------------------------------------------------------

def precompute_potential_matrix(
    problem: Problem,
    routes: Sequence[Sequence[int]],
    context: Mapping[int, RouteContext],
    stations: Sequence[RelayStation],
) -> dict[tuple[int, int], float]:
    """Precompute the normalised surrogate ``B_ir`` for every pair.

    Task-only terms (urgency, holding pressure, downstream-critical count) and
    station-only terms (cheapest station-insert detour per drone) are computed
    once so the full matrix is O(tasks x stations) cheap operations.
    """

    speed = problem.speed_km_per_min
    capacity = problem.capacity

    task_terms: dict[int, tuple[float, float, int]] = {}
    for task_id, ctx in context.items():
        urgency = _urgency_factor(ctx)
        capacity_release = min(1.0, _holding_pressure(ctx, capacity))
        holding = ctx.delivery_time_min - ctx.pickup_time_min
        downstream_critical = 0
        for other_id, other in context.items():
            if other.drone != ctx.drone:
                continue
            if other.pickup_time_min <= ctx.pickup_time_min:
                continue
            if other.pickup_late or other.delivery_late:
                downstream_critical += 1
        task_terms[task_id] = (urgency, capacity_release, downstream_critical)

    station_detour: dict[int, list[float]] = {}
    for station in stations:
        station_detour[station.id] = [
            best_station_insert_detour_km(problem, route, station.source_visit)
            for route in routes
        ]

    matrix: dict[tuple[int, int], float] = {}
    for task_id, (urgency, capacity_release, downstream_critical) in task_terms.items():
        task = problem.task(task_id)
        p = task.pickup
        d = task.delivery
        direct = p.distance_to(d)
        holding = context[task_id].delivery_time_min - context[task_id].pickup_time_min
        for station in stations:
            geometry = _geometry(problem, task_id, station)
            detour = geometry["detour"]
            geometry_factor = (
                exp(-detour / max(1e-9, direct)) * geometry["balance"]
            )
            detour_penalty = 1.0 / (1.0 + detour)

            best_cost = float("inf")
            for detour_km in station_detour[station.id]:
                cost = detour_km + station.point.distance_to(d)
                best_cost = min(best_cost, cost)
            receiver = (
                1.0 / (1.0 + best_cost) if isfinite(best_cost) else 0.0
            )

            release_time = max(
                0.0, holding - p.distance_to(station.point) / speed
            )
            upstream_bonus = (
                min(1.0, release_time * (1.0 + downstream_critical) / max(1.0, direct))
                if release_time > 0.0
                else 0.0
            )

            matrix[(task_id, station.id)] = (
                (COEF_URGENCY * urgency)
                * (COEF_GEOMETRY * geometry_factor)
                * (COEF_DETOUR * detour_penalty)
                * (COEF_RECEIVER * receiver)
                * (
                    1.0
                    + COEF_CAPACITY_RELEASE * capacity_release
                    + COEF_UPSTREAM_RELEASE * upstream_bonus
                )
            )
    return matrix


def _layout_static_score(
    matrix: Mapping[tuple[int, int], float],
    task_ids: Sequence[int],
    stations: Sequence[RelayStation],
) -> float:
    """Sum of the best per-task surrogate over the selected stations."""

    station_ids = tuple(station.id for station in stations)
    total = 0.0
    for task_id in task_ids:
        best = 0.0
        for station_id in station_ids:
            value = matrix.get((task_id, station_id), 0.0)
            if value > best:
                best = value
        total += best
    return total


def _layout_key(stations: Sequence[RelayStation]) -> tuple[int, ...]:
    return tuple(sorted(station.id for station in stations))


def greedy_station_selection(
    matrix: Mapping[tuple[int, int], float],
    task_ids: Sequence[int],
    candidates: Sequence[RelayStation],
    num_stations: int,
    *,
    rng: Random,
) -> tuple[RelayStation, ...]:
    """Greedy marginal-gain facility selection with random tie-breaking."""

    selected: list[RelayStation] = []
    remaining = list(candidates)
    for _ in range(num_stations):
        best_gain = -1.0
        best_stations: list[RelayStation] = []
        for station in remaining:
            trial = selected + [station]
            gain = _layout_static_score(matrix, task_ids, trial)
            if gain > best_gain + 1e-12:
                best_gain = gain
                best_stations = [station]
            elif abs(gain - best_gain) <= 1e-12:
                best_stations.append(station)
        if not best_stations:
            break
        chosen = rng.choice(best_stations)
        selected.append(chosen)
        remaining = [station for station in remaining if station.id != chosen.id]
    return tuple(selected)


def swap_improve_layout(
    matrix: Mapping[tuple[int, int], float],
    task_ids: Sequence[int],
    layout: Sequence[RelayStation],
    candidates: Sequence[RelayStation],
    *,
    limit: int = 30,
    in_shortlist: int = 60,
) -> tuple[RelayStation, ...]:
    """1-for-1 best-improvement station swap until convergence.

    Uses per-task best/second-best surrogates so each swap trial is O(tasks),
    and only considers a shortlist of promising incoming stations.
    """

    candidate_by_id = {station.id: station for station in candidates}
    selected_ids = {station.id for station in layout}

    # Per-task best and second-best over the current selection.
    def selection_stats(sel: frozenset[int]) -> tuple[dict[int, float], dict[int, float], dict[int, int]]:
        best: dict[int, float] = {}
        second: dict[int, float] = {}
        argmax: dict[int, int] = {}
        for task_id in task_ids:
            top1 = 0.0
            top2 = 0.0
            top_id = -1
            for station_id in sel:
                value = matrix.get((task_id, station_id), 0.0)
                if value > top1:
                    top2 = top1
                    top1 = value
                    top_id = station_id
                elif value > top2:
                    top2 = value
            best[task_id] = top1
            second[task_id] = top2
            argmax[task_id] = top_id
        return best, second, argmax

    # Rank incoming stations by total surrogate over all tasks.
    station_totals = [
        (sum(matrix.get((task_id, station.id), 0.0) for task_id in task_ids), station)
        for station in candidates
        if station.id not in selected_ids
    ]
    station_totals.sort(reverse=True)
    incoming_shortlist = [
        station for _, station in station_totals[:in_shortlist]
    ]

    best_value, second_value, argmax_station = selection_stats(
        frozenset(selected_ids)
    )
    total_score = sum(best_value[task_id] for task_id in task_ids)

    for _ in range(limit):
        best_delta = 0.0
        best_pair: tuple[int, int] | None = None
        for out_id in selected_ids:
            for station in incoming_shortlist:
                in_id = station.id
                delta = 0.0
                for task_id in task_ids:
                    if argmax_station[task_id] == out_id:
                        new_best = max(
                            second_value[task_id],
                            matrix.get((task_id, in_id), 0.0),
                        )
                    else:
                        new_best = max(
                            best_value[task_id],
                            matrix.get((task_id, in_id), 0.0),
                        )
                    delta += new_best - best_value[task_id]
                if delta > best_delta + 1e-12:
                    best_delta = delta
                    best_pair = (out_id, in_id)
        if best_pair is None or best_delta <= 1e-12:
            break
        out_id, in_id = best_pair
        selected_ids = (selected_ids - {out_id}) | {in_id}
        total_score += best_delta
        best_value, second_value, argmax_station = selection_stats(
            frozenset(selected_ids)
        )
    return tuple(
        sorted(
            (candidate_by_id[sid] for sid in selected_ids),
            key=lambda station: station.id,
        )
    )


def diversified_layouts(
    matrix: Mapping[tuple[int, int], float],
    task_ids: Sequence[int],
    candidates: Sequence[RelayStation],
    num_stations: int,
    *,
    top: int = TOP_LAYOUTS,
    restarts: int = 6,
    seed: int = 2026080500,
) -> tuple[tuple[RelayStation, ...], ...]:
    """Greedy + swap trajectory + seeded randomized-greedy restarts."""

    layouts: dict[tuple[int, ...], tuple[RelayStation, ...]] = {}
    rng = Random(seed)

    base_greedy = greedy_station_selection(
        matrix, task_ids, candidates, num_stations, rng=rng
    )
    improved = swap_improve_layout(matrix, task_ids, base_greedy, candidates)
    layouts[_layout_key(improved)] = improved

    for _ in range(restarts):
        restarted = greedy_station_selection(
            matrix, task_ids, candidates, num_stations, rng=rng
        )
        improved = swap_improve_layout(matrix, task_ids, restarted, candidates)
        layouts[_layout_key(improved)] = improved

    ranked = sorted(
        layouts.values(),
        key=lambda layout: -_layout_static_score(matrix, task_ids, layout),
    )
    return tuple(ranked[:top])


# ---------------------------------------------------------------------------
# Operational probe.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ProbeStats:
    feasible_relay_count: int
    direct_rescue_count: int
    downstream_pickup_acceleration_count: int
    downstream_rescue_count: int


def _priority_tasks(
    problem: Problem,
    routes: Sequence[Sequence[int]],
    context: Mapping[int, RouteContext],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Priority A (direct-rescue) and Priority B (upstream blocker) tasks."""

    priority_a: list[int] = []
    for task_id, ctx in context.items():
        if (not ctx.pickup_late) and ctx.delivery_late:
            priority_a.append(task_id)
    priority_b: list[int] = []
    for task_id, ctx in context.items():
        if not (ctx.pickup_late or ctx.delivery_late):
            continue
        # A blocker: holding interval or delivery detour sits before a late
        # pickup on the same route.
        for other_id, other in context.items():
            if other.drone != ctx.drone:
                continue
            if other.pickup_time_min <= ctx.pickup_time_min:
                continue
            if other.pickup_late and ctx.delivery_time_min < other.pickup_time_min:
                priority_b.append(task_id)
                break
    return (
        tuple(sorted(set(priority_a))),
        tuple(sorted(set(priority_b))),
    )


def operational_probe(
    problem: Problem,
    direct_solution: RelaySolution,
    layout: Sequence[RelayStation],
    *,
    candidate_limits: Mapping[str, int] | None = None,
    priority_b_limit: int = 25,
    deadline: float | None = None,
    seed: int = 2026080500,
) -> tuple[RelaySolution, object, ProbeStats]:
    """Operationally probe one station layout on the direct seed solution.

    Tries Priority A (direct rescue) tasks first, then a capped set of
    Priority B (upstream blocker) tasks.  Every candidate is fully
    DAG-validated.  Strict Score-improving relays are applied greedily;
    non-improving relays are still measured as *counterfactuals* (downstream
    pickup acceleration / downstream rescue) so the layout ranking has signal
    even when single-shot relays cannot rescue on their own.
    """

    limits = {
        "second_drone_limit": 2,
        "drop_gap_limit": 3,
        "pick_gap_limit": 3,
        "delivery_gap_limit": 3,
        "max_candidates": 8,
        **(candidate_limits or {}),
    }
    context = build_route_context(problem, direct_solution.routes)
    priority_a, priority_b = _priority_tasks(
        problem, direct_solution.routes, context
    )
    # Rank Priority B by holding length (the strongest blocker signal) and cap.
    ranked_b = sorted(
        priority_b,
        key=lambda task_id: (
            -(context[task_id].delivery_time_min - context[task_id].pickup_time_min),
            context[task_id].pickup_time_min,
            task_id,
        ),
    )[:priority_b_limit]

    solution = relay_solution_from_routes(
        direct_solution.routes, stations=layout
    )
    evaluation = evaluate_relay_solution(problem, solution)
    stats = ProbeStats(0, 0, 0, 0)
    rng = Random(seed)

    def late_set() -> set[int]:
        return {
            task_id
            for task_id, delivered_at in evaluation.delivery_times_min.items()
            if delivered_at > problem.task(task_id).deadline_min + 1e-9
        }

    baseline_pickups = dict(evaluation.pickup_times_min)
    late_before = late_set()

    def measure_and_apply(chosen: RelayCandidate, *, is_priority_a: bool) -> None:
        nonlocal solution, evaluation, stats, baseline_pickups, late_before
        after_pickups = dict(chosen.evaluation.pickup_times_min)
        late_after = {
            task_id
            for task_id, delivered_at in chosen.evaluation.delivery_times_min.items()
            if delivered_at > problem.task(task_id).deadline_min + 1e-9
        }
        rescued = late_before - late_after
        accelerated = [
            other_id
            for other_id in after_pickups
            if after_pickups[other_id]
            < baseline_pickups.get(other_id, 0.0) - 1e-9
        ]
        if is_priority_a and chosen.task_id in rescued:
            stats = replace(
                stats, direct_rescue_count=stats.direct_rescue_count + 1
            )
        stats = replace(
            stats,
            downstream_rescue_count=stats.downstream_rescue_count
            + len(rescued),
            downstream_pickup_acceleration_count=(
                stats.downstream_pickup_acceleration_count + len(accelerated)
            ),
        )
        # Apply only strict Score improvements (keeps the probe solution valid).
        if chosen.delta_score < Score(0, 0.0):
            solution = _apply(problem, solution, chosen)
            evaluation = chosen.evaluation
            baseline_pickups = after_pickups
            late_before = late_after

    for task_id in [*priority_a, *ranked_b]:
        if deadline is not None and perf_counter() >= deadline:
            break
        is_priority_a = task_id in priority_a
        candidates = relay_candidates_for_task(
            problem,
            solution,
            task_id,
            stations=layout,
            second_drone_limit=int(limits["second_drone_limit"]),
            drop_gap_limit=int(limits["drop_gap_limit"]),
            pick_gap_limit=int(limits["pick_gap_limit"]),
            delivery_gap_limit=int(limits["delivery_gap_limit"]),
            max_candidates=int(limits["max_candidates"]),
            stop_at_improvement=False,
            deadline=deadline,
        )
        if not candidates:
            continue
        stats = replace(
            stats,
            feasible_relay_count=stats.feasible_relay_count + len(candidates),
        )
        improving = [
            candidate
            for candidate in candidates
            if candidate.delta_score < Score(0, 0.0)
        ]
        if improving:
            chosen = min(
                improving,
                key=lambda candidate: candidate.delta_score,
            )
            measure_and_apply(chosen, is_priority_a=is_priority_a)
        else:
            # Counterfactual: best non-improving relay still shows whether the
            # layout could accelerate downstream pickups or rescue tasks.
            chosen = candidates[0]
            measure_and_apply(chosen, is_priority_a=is_priority_a)

    return solution, evaluation, stats


def _apply(
    problem: Problem,
    solution: RelaySolution,
    candidate: RelayCandidate,
) -> RelaySolution:
    from .relay_candidates import apply_relay_candidate

    return apply_relay_candidate(problem, solution, candidate)


def design_relay_stations(
    problem: Problem,
    direct_routes: Sequence[Sequence[int]],
    *,
    num_stations: int = NUM_RELAY_STATIONS,
    top: int = TOP_LAYOUTS,
    probe_candidate_limits: Mapping[str, int] | None = None,
    deadline: float | None = None,
    seed: int = 2026080500,
) -> StationDesignResult:
    """Run Module 0 end-to-end and freeze ``FINAL_RELAY_STATIONS``."""

    started = perf_counter()
    candidates = candidate_sites(problem)
    context = build_route_context(problem, direct_routes)
    matrix = precompute_potential_matrix(
        problem, direct_routes, context, candidates
    )
    layouts = diversified_layouts(
        matrix,
        problem.task_ids,
        candidates,
        num_stations,
        top=top,
        restarts=6,
        seed=seed,
    )

    probed: list[StationLayout] = []
    best_result: tuple[RelaySolution, object, ProbeStats] | None = None
    best_key: tuple[int, int, float] | None = None
    for layout in layouts:
        direct_solution = relay_solution_from_routes(direct_routes)
        solution, evaluation, stats = operational_probe(
            problem,
            direct_solution,
            layout,
            candidate_limits=probe_candidate_limits,
            deadline=deadline,
            seed=seed,
        )
        static_score = _layout_static_score(
            matrix, problem.task_ids, layout
        )
        probed.append(
            StationLayout(
                stations=layout,
                static_score=static_score,
                probe_late_count=evaluation.score.late_count,
                probe_distance_km=evaluation.score.distance_km,
                feasible_relay_count=stats.feasible_relay_count,
                direct_rescue_count=stats.direct_rescue_count,
                downstream_rescue_count=stats.downstream_rescue_count,
            )
        )
        # Formal ranking: (late_count, distance); tie-break by more downstream
        # rescue, direct rescue, feasible relays, then static score.
        key = (
            evaluation.score.late_count,
            evaluation.score.distance_km,
            -stats.downstream_rescue_count,
            -stats.direct_rescue_count,
            -stats.feasible_relay_count,
            -static_score,
        )
        if best_key is None or key < best_key:
            best_key = key
            best_result = (solution, evaluation, stats)

    final_solution, final_evaluation, _ = best_result
    final_layout = final_solution.stations
    return StationDesignResult(
        final_stations=final_layout,
        candidate_count=len(candidates),
        layouts=tuple(probed),
        probe_solution=final_solution,
        probe_evaluation=final_evaluation,
        runtime_seconds=perf_counter() - started,
    )
