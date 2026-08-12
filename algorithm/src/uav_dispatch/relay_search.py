"""Relay-aware two-stage search: Stage I (initial) + Stage II (ALNS).

This module *extends* the existing ALNS machinery in ``alns.py`` (roulette
selection, adaptive segment weights, two-layer SA acceptance) with a relay
mode, rather than duplicating a parallel solver:

- Stage I builds a relay-aware initial solution: Regret-2 foundation, a short
  direct ALNS pass, a pickup-critical balanced re-assignment, then a greedy
  initial relay allocation over the frozen station layout.
- Stage II runs a relay-aware ALNS whose destroy removes complete tasks
  (direct or relay) and whose repair re-inserts each task as either a Direct
  or a Relay move over the frozen stations, so the receiver's route is
  re-optimised together with the relay instead of suffering a blind insertion.

All score decisions use the strict two-layer ``(late_count, distance_km)``
order; total lateness is a diagnostic only.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field, replace
from math import exp
from random import Random
from time import perf_counter
from types import MappingProxyType
from typing import Mapping, Sequence

from .alns import (
    ALNSConfig,
    DESTROY_OPERATORS as ALNS_DESTROY_OPERATORS,
    REPAIR_OPERATORS as ALNS_REPAIR_OPERATORS,
    _SearchDeadlineReached,
    _accept_worse,
    _ejection_swap_improve,
    _repair as alns_repair,
    _roulette,
    construct_regret_initial,
    destroy_solution as alns_destroy,
    solve_alns_core,
)
from .location_allocation import (
    LOCATION_ALLOCATION_TIME_LIMIT,
    LocationAllocationResult,
    solve_location_allocation,
)
from .model import Problem, Score
from .relay_candidates import (
    FreshRelayOption,
    RelayCandidate,
    apply_relay_candidate,
    relay_candidates_for_task,
    relay_insertion_options,
)
from .relay_location import (
    build_route_context,
    candidate_sites,
    design_relay_stations,
    greedy_station_selection,
    precompute_potential_matrix,
)
from .relay_model import (
    NUM_RELAY_STATIONS,
    RelaySolution,
    RelayStation,
    RelayTransfer,
    relay_solution_from_routes,
)
from .relay_validation import (
    RelaySolutionEvaluation,
    evaluate_relay_solution,
)
from .search import RouteEvaluator, Routes
from .validation import evaluate_solution

#: Max pickup lateness (min) for a task to be considered relay-promising.
PICKUP_RELAY_SLACK_LIMIT = 15.0


@dataclass(frozen=True, slots=True)
class RelaySearchConfig:
    """Computation controls for the relay-aware two-stage search."""

    seed: int = 2026080500
    num_stations: int = NUM_RELAY_STATIONS
    handling_time_min: float = 0.0
    candidate_limit: int = 48
    time_limit_seconds: float | None = None
    module0_share: float = 0.10
    stage1_share: float = 0.15
    module0_deadline: float | None = None
    stage1_deadline: float | None = None
    foundation: str = "regret2"  # "regret2" (quality) or "greedy" (speed)
    min_destroy_fraction: float = 0.04
    max_destroy_fraction: float = 0.10
    weight_update_interval: int = 40
    reaction_factor: float = 0.2
    minimum_weight: float = 0.05
    initial_temperature: float = 0.03
    minimum_temperature: float = 0.0005
    cooling_rate: float = 0.995
    recall_threshold: int = 800
    reheat_factor: float = 2.0
    candidate_limits: Mapping[str, int] = field(
        default_factory=lambda: {
            "second_drone_limit": 1,
            "drop_gap_limit": 2,
            "pick_gap_limit": 2,
            "delivery_gap_limit": 2,
            "pickup_gap_limit": 2,
            "first_drone_limit": 1,
            "max_candidates": 4,
            "max_evaluations": 12,
        }
    )
    # Fraction of relay-eligible tasks that get full relay option generation
    # in each repair pass.  Relay option generation is the dominant repair
    # cost, and on capacity-tight instances most relay candidates are never
    # improving, so a stochastic budget keeps the iteration rate close to the
    # plain ALNS while still probing the relay mechanism everywhere it might
    # help.  Set to 1.0 to always generate relay options (slower, exhaustive).
    relay_option_probability: float = 0.3
    # Adaptive Stage-I: when the direct solution shows almost no relay-
    # rescuable tasks (pickup on time but delivery late, or upstream slack),
    # the expensive 0-1 Location-Allocation MILP is skipped and the fast
    # greedy layout is used, handing the saved budget to Stage II.
    adaptive_stage1: bool = True
    rescuable_threshold: int = 3
    # Wall-clock cap for the 0-1 Location-Allocation MILP (seconds).
    lap_time_limit_seconds: float = 6.0
    # Advanced operator toggles, matching the plain-ALNS defaults so the
    # relay-aware search is compared on equal footing with the A2 baseline.
    enable_hypergraph_destroy: bool = False
    enable_cluster_repair: bool = False
    enable_pair_repair: bool = False
    # Ejection-swap reinforcement (same defaults as the A2 baseline).  Applied
    # on relay-free candidate solutions only (its per-route scorer cannot see
    # cross-drone relay legs).
    enable_ejection: bool = True
    ejection_interval: int = 25
    ejection_trials: int = 24
    # How many most-deadline-urgent removed tasks get a Direct -> Relay
    # conversion probe in each per-iteration route adjustment.  A larger
    # value explores the relay mechanism more aggressively but costs wall
    # clock per iteration; 1-2 keeps the iteration rate close to the plain
    # ALNS baseline on instances where relay conversion never helps.
    relay_adjust_probe_limit: int = 1
    # Relay-rescue probes: each iteration, the most pick-late tasks already
    # placed in the solution are inspected and the upstream blockers of the
    # ``relay_rescue_probe_limit`` most rescuable ones are added to the
    # Direct -> Relay conversion probe set.  This is what lets relay
    # transfers actually enter solutions on capacity-tight instances where
    # relay conversions on freshly-removed tasks never improve (their value
    # is rescuing downstream pickups, not fixing the removed tasks).
    relay_rescue_probe_limit: int = 2


@dataclass(frozen=True, slots=True)
class RelaySolverResult:
    """Full result of the relay-aware two-stage solver."""

    routes: Routes
    relays: tuple[RelayTransfer, ...]
    stations: tuple[RelayStation, ...]
    evaluation: RelaySolutionEvaluation
    runtime_seconds: float = 0.0
    iterations: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict, compare=False)


def _pickup_slack_for_task(
    problem: Problem, solution: RelaySolution, task_id: int
) -> float:
    """Return canonical pickup slack (latest_pickup - actual_pickup) for *task_id*.

    Negative → pickup is already too late for on-time delivery.
    Zero  → exactly on the boundary.
    Positive → slack remaining.
    """
    from .physical_lower_bound import pickup_slack as _ps

    pickup_time = float("inf")
    for route in solution.routes:
        elapsed = 0.0
        previous: int | None = None
        for visit in route:
            elapsed += problem.distance(previous, visit) / problem.speed_km_per_min
            previous = visit
            if visit == task_id:
                pickup_time = elapsed
                break
        if pickup_time != float("inf"):
            break
    if pickup_time == float("inf"):
        return float("inf")  # task not in any route
    return _ps(problem, task_id, pickup_time)


# ---------------------------------------------------------------------------
# Relay-aware repair (Stage I construction – kept for initial solution).
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _InsertionChoice:
    score: Score
    kind: str
    candidate: RelayCandidate | None
    fresh: object | None
    route_index: int
    route: tuple[int, ...]
    task_id: int


def _direct_options_for_task(
    problem: Problem,
    evaluator: RouteEvaluator,
    solution: RelaySolution,
    task_id: int,
    option_count: int,
) -> tuple[_InsertionChoice, ...]:
    """Best direct (same-drone) insertions for a task across all routes.

    Routes that contain single-ended relay visits (a pickup without its
    delivery on the same route) cannot be scored by the per-route
    ``RouteEvaluator``; those routes are skipped for direct insertion (the
    relay structure is preserved and the task can still be inserted elsewhere
    or via relay options).
    """

    from collections import Counter

    from .search import route_insertion_options

    choices: list[_InsertionChoice] = []
    for route_index, route in enumerate(solution.routes):
        counts = Counter(abs(visit) for visit in route)
        if any(count == 1 for count in counts.values()):
            continue
        options = route_insertion_options(
            problem,
            evaluator,
            route,
            route_index,
            task_id,
            candidate_limit=48,
            option_count=option_count,
        )
        for option in options:
            choices.append(
                _InsertionChoice(
                    score=option.delta,
                    kind="direct",
                    candidate=None,
                    fresh=None,
                    route_index=route_index,
                    route=option.route,
                    task_id=task_id,
                )
            )
    choices.sort(key=lambda choice: (choice.score, choice.route_index))
    return tuple(choices[:option_count])


def _task_is_placed(solution: RelaySolution, task_id: int) -> bool:
    return any(abs(visit) == task_id for route in solution.routes for visit in route)


def _fresh_stations(
    stations: Sequence[RelayStation], preferred_station_id: int | None
) -> tuple[RelayStation, ...]:
    """Preferred station first, plus at most one other, for fresh relays."""

    if preferred_station_id is None:
        return tuple(stations[:2])
    preferred = [s for s in stations if s.id == preferred_station_id]
    others = [s for s in stations if s.id != preferred_station_id]
    return tuple(preferred + others[:1])


def _relay_options_for_task(
    problem: Problem,
    solution: RelaySolution,
    task_id: int,
    *,
    stations: Sequence[RelayStation],
    limits: Mapping[str, int],
    preferred_station_id: int | None = None,
    deadline: float | None,
) -> tuple[_InsertionChoice, ...]:
    """Best relay insertions for a task over the frozen station layout.

    Uses conversion options when the task is already placed (direct -> relay)
    and fresh-insertion options when it is not yet in any route.
    """

    from .relay_candidates import relay_insertion_options

    if _task_is_placed(solution, task_id):
        candidates = relay_candidates_for_task(
            problem,
            solution,
            task_id,
            stations=stations,
            second_drone_limit=int(limits.get("second_drone_limit", 2)),
            drop_gap_limit=int(limits.get("drop_gap_limit", 3)),
            pick_gap_limit=int(limits.get("pick_gap_limit", 3)),
            delivery_gap_limit=int(limits.get("delivery_gap_limit", 3)),
            max_candidates=int(limits.get("max_candidates", 6)),
            max_evaluations=int(limits.get("max_evaluations", 20)),
            deadline=deadline,
            require_all_tasks=False,
        )
        return tuple(
            _InsertionChoice(
                score=candidate.delta_score,
                kind="relay_convert",
                candidate=candidate,
                fresh=None,
                route_index=-1,
                route=(),
                task_id=task_id,
            )
            for candidate in candidates
        )
    options = relay_insertion_options(
        problem,
        solution,
        task_id,
        stations=_fresh_stations(stations, preferred_station_id),
        preferred_station_id=preferred_station_id,
        first_drone_limit=int(limits.get("first_drone_limit", 1)),
        second_drone_limit=int(limits.get("second_drone_limit", 1)),
        pickup_gap_limit=int(limits.get("pickup_gap_limit", 2)),
        drop_gap_limit=int(limits.get("drop_gap_limit", 2)),
        pick_gap_limit=int(limits.get("pick_gap_limit", 2)),
        delivery_gap_limit=int(limits.get("delivery_gap_limit", 2)),
        max_candidates=int(limits.get("max_candidates", 4)),
        max_evaluations=int(limits.get("max_evaluations", 20)),
        deadline=deadline,
        require_all_tasks=False,
    )
    return tuple(
        _InsertionChoice(
            score=option.delta_score,
            kind="relay_fresh",
            candidate=None,
            fresh=option,
            route_index=-1,
            route=(),
            task_id=task_id,
        )
        for option in options
    )


def relay_repair(
    problem: Problem,
    evaluator: RouteEvaluator,
    solution: RelaySolution,
    removed_task_ids: Sequence[int],
    *,
    stations: Sequence[RelayStation],
    strategy: str,
    limits: Mapping[str, int],
    rng: Random,
    guidance: Mapping[int, int] | None = None,
    deadline: float | None,
    relay_option_probability: float = 1.0,
) -> RelaySolution:
    """Re-insert removed tasks with Direct or Relay alternatives.

    Single-pass multi-mode insertion: tasks are processed in priority order
    and each one is offered Direct insertions and (for relay-eligible tasks)
    fresh or converted Relay insertions, chosen by the strict two-layer
    ``(late_count, distance_km)`` order.  ``relay_regret2`` orders the tasks
    by regret over their two best options; ``relay_priority`` orders by
    deadline pressure.  Relay-eligible tasks are those with a Location-
    Allocation guidance, a late pickup, or a late delivery.
    """

    current = solution
    remaining = list(removed_task_ids)

    # Relay can only plausibly help tasks whose pickup is not catastrophically
    # late: a relay cannot reverse time, and the instance's deep-late tasks
    # (40-55 min) are unrecoverable by minute-level relay savings.  Restricting
    # relay option generation to this small set keeps the repair fast AND
    # focuses the search where the mechanism can actually matter.
    relay_promising: set[int] = set()
    for task_id in remaining:
        if guidance is not None and task_id in guidance:
            pickup_s = _pickup_slack_for_task(problem, current, task_id)
            # pickup_slack >= -PICKUP_RELAY_SLACK_LIMIT: not hopelessly late
            if pickup_s >= -PICKUP_RELAY_SLACK_LIMIT:
                relay_promising.add(task_id)
        elif _pickup_slack_for_task(problem, current, task_id) >= 0.0:
            relay_promising.add(task_id)

    if strategy == "relay_priority":
        remaining.sort(
            key=lambda task_id: (
                -problem.task(task_id).deadline_min,
                task_id,
            )
        )
    elif strategy == "relay_greedy":
        rng.shuffle(remaining)
    else:  # relay_regret2: handled dynamically below (per-round regret).
        remaining.sort(key=lambda task_id: task_id)

    # True multi-mode regret-2: each round we score every remaining task over
    # its Direct + (sampled) Relay options, compute the regret (cost gap
    # between the two best options) and insert the task with the largest
    # regret.  Relay option generation is expensive, so at most
    # ``_RELAY_PROBES_PER_ROUND`` relay-eligible tasks are probed per round,
    # picked by pickup slack (most rescue-able first); every other task is
    # scored over its Direct options only.
    _RELAY_PROBES_PER_ROUND = 3

    def _score_task(
        task_id: int, allow_relay_probe: bool
    ) -> tuple[list[_InsertionChoice], _InsertionChoice | None]:
        preferred = guidance.get(task_id) if guidance is not None else None
        direct = list(
            _direct_options_for_task(problem, evaluator, current, task_id, 3)
        )
        merged = sorted(direct, key=lambda choice: choice.score)[:3]
        probe_relay = (
            allow_relay_probe
            and task_id in relay_promising
            and (
                relay_option_probability >= 1.0
                or rng.random() < relay_option_probability
            )
        )
        if probe_relay:
            relay = _relay_options_for_task(
                problem,
                current,
                task_id,
                stations=stations,
                limits=limits,
                preferred_station_id=preferred,
                deadline=deadline,
            )
            merged = sorted(
                direct + list(relay), key=lambda choice: choice.score
            )[:3]
        if not merged:
            # No Direct slot at all: force a relay attempt for this task.
            relay = _relay_options_for_task(
                problem,
                current,
                task_id,
                stations=stations,
                limits=limits,
                preferred_station_id=preferred,
                deadline=deadline,
            )
            if relay:
                merged = sorted(relay, key=lambda choice: choice.score)[:1]
        return merged, merged[0] if merged else None

    while remaining:
        if deadline is not None and perf_counter() >= deadline:
            break
        if strategy == "relay_regret2" and len(remaining) > 1:
            # Rank the most rescue-able relay-promising tasks for relay probes.
            ordered = sorted(
                (t for t in remaining if t in relay_promising),
                key=lambda t: _pickup_slack_for_task(problem, current, t),
            )[: _RELAY_PROBES_PER_ROUND]
            probe_set = set(ordered)
            best_task: int | None = None
            best_choice: _InsertionChoice | None = None
            best_regret: float | None = None
            for task_id in remaining:
                merged, choice = _score_task(
                    task_id, task_id in probe_set
                )
                if choice is None:
                    continue
                regret = (
                    (merged[1].score - merged[0].score).late_count
                    if len(merged) >= 2
                    else 0
                ) + (
                    (merged[1].score - merged[0].score).distance_km
                    if len(merged) >= 2
                    else 0.0
                ) * 1e-6
                if best_task is None or regret > best_regret:
                    best_task = task_id
                    best_choice = choice
                    best_regret = regret
            if best_task is None:
                # Everything unplaceable; drop the first task defensively.
                remaining = remaining[1:]
                continue
            task_id = best_task
            choice = best_choice
            remaining.remove(task_id)
        else:
            task_id = remaining[0]
            _, choice = _score_task(task_id, True)
            if choice is None:
                remaining = remaining[1:]
                continue
            remaining = remaining[1:]

        if choice.kind == "direct":
            mutable = list(current.routes)
            mutable[choice.route_index] = choice.route
            current = RelaySolution(
                tuple(tuple(r) for r in mutable),
                current.relays,
                current.stations,
            )
        elif choice.kind == "relay_convert":
            current = apply_relay_candidate(problem, current, choice.candidate)
        else:  # relay_fresh
            current = choice.fresh.solution

    return current


# ---------------------------------------------------------------------------
# Relay route adjustment (bounded, affected-neighbourhood moves).
# ---------------------------------------------------------------------------

def relay_route_adjustment(
    problem: Problem,
    solution: RelaySolution,
    affected_tasks: Sequence[int],
    *,
    stations: Sequence[RelayStation],
    limits: Mapping[str, int],
    deadline: float | None,
) -> RelaySolution:
    """Apply bounded improving relay moves on the affected neighbourhood.

    MOVE A: direct -> relay, MOVE B: relay -> direct, MOVE C: relay station
    change, MOVE D: change second drone.  Only strict Score improvements are
    accepted and the solution is re-validated by the full DAG evaluator.
    """

    current = solution
    current_evaluation = evaluate_relay_solution(problem, current)
    for task_id in affected_tasks:
        if deadline is not None and perf_counter() >= deadline:
            break
        relay = next(
            (relay for relay in current.relays if relay.task_id == task_id), None
        )
        if relay is None:
            if not _task_is_placed(current, task_id):
                continue
            # MOVE A: try to turn this direct task into a relay.
            candidates = relay_candidates_for_task(
                problem,
                current,
                task_id,
                stations=stations,
                second_drone_limit=int(limits.get("second_drone_limit", 2)),
                drop_gap_limit=int(limits.get("drop_gap_limit", 3)),
                pick_gap_limit=int(limits.get("pick_gap_limit", 3)),
                delivery_gap_limit=int(limits.get("delivery_gap_limit", 3)),
                max_candidates=int(limits.get("max_candidates", 6)),
                max_evaluations=int(limits.get("max_evaluations", 20)),
                deadline=deadline,
            )
            # Accept relay conversions that do not worsen the primary
            # objective (late_count).  A relay may increase distance
            # slightly while rescuing a downstream late pickup; once
            # the relay is in the solution, later iterations can
            # refine the receiver's route.  Tie-breaking on delivery_gap
            # prefers the least-disruptive receiver insertion.
            improving = [c for c in candidates if c.delta_score.late_count <= 0]
            if improving:
                chosen = min(
                    improving, key=lambda c: (c.delta_score.late_count, c.delivery_gap)
                )
                current = apply_relay_candidate(problem, current, chosen)
        else:
            # MOVE B: try to revert the relay to a direct same-drone task.
            # MOVE C/D: try other stations / second drones.
            current = _relay_reassignment(
                problem, current, task_id, relay, stations, limits, deadline
            )
    return current


def _relay_rescue_targets(
    problem: Problem,
    solution: RelaySolution,
    limit: int,
    *,
    deadline: float | None = None,
) -> list[int]:
    """Upstream blockers of the most pick-late (most rescuable) tasks.

    Relay transfers rescue tasks whose *pickup* runs late: an early drop of
    an upstream holding task lets the first drone reach the target pickup
    earlier.  We score every placed task by how close its pickup runs to its
    deadline (smallest absolute gap = easiest to rescue, either already late
    by a fraction of a minute or about to be), take the ``limit`` most
    rescuable tasks, and return their direct (non-relay) upstream blockers:
    tasks picked up and delivered strictly before the target pickup whose
    holding time is large - exactly the tasks whose early release pays off.
    """

    from time import perf_counter

    evaluation = evaluate_relay_solution(problem, solution)
    if not evaluation.valid:
        return []
    ptimes = evaluation.pickup_times_min
    relay_task_ids = {r.task_id for r in solution.relays}

    scored: list[tuple[float, int]] = []
    for tid in problem.task_ids:
        pickup = ptimes.get(tid)
        if pickup is None:
            continue
        dl = problem.task(tid).deadline_min
        # Already-late-by-a-lot pickups cannot be saved by a relay either;
        # focus on the borderline ones (both sides of the deadline).
        if pickup - dl > 6.0:
            continue
        scored.append((abs(pickup - dl), tid))
    scored.sort()

    blockers: list[int] = []
    seen: set[int] = set()
    for _gap, tid in scored[:limit]:
        if deadline is not None and perf_counter() >= deadline:
            break
        for route in solution.routes:
            if tid not in route:
                continue
            pidx = route.index(tid)
            route_blockers: list[tuple[float, int]] = []
            for pos in range(pidx):
                visit = route[pos]
                if visit <= 0:
                    continue
                b = visit
                if b in seen or b in relay_task_ids:
                    continue
                bpidx = route.index(b)
                bdidx = route.index(-b)
                if bdidx >= pidx:
                    continue
                holding = 0.0
                elapsed = 0.0
                prev: int | None = None
                for v in route[bpidx:bdidx + 1]:
                    elapsed += problem.distance(prev, v) / problem.speed_km_per_min
                    prev = v
                holding = elapsed
                route_blockers.append((holding, b))
            # Largest holding first: biggest upstream release.
            for _holding, b in sorted(route_blockers, reverse=True):
                seen.add(b)
                blockers.append(b)
            break
    return blockers


def _relay_reassignment(
    problem: Problem,
    solution: RelaySolution,
    task_id: int,
    relay: RelayTransfer,
    stations: Sequence[RelayStation],
    limits: Mapping[str, int],
    deadline: float | None,
) -> RelaySolution:
    """Try switching a relay's station (MOVE C) or second drone (MOVE D)."""

    from .relay_candidates import _insert_visit, _remove_visit

    base = evaluate_relay_solution(problem, solution)
    best = solution
    best_score = base.score
    first_route = solution.routes[relay.first_drone]
    second_route = solution.routes[relay.second_drone]
    task = problem.task(task_id)

    candidates: list[RelaySolution] = []
    for station in stations:
        if station.point.distance_to(task.pickup) <= 1e-9:
            continue
        if station.point.distance_to(task.delivery) <= 1e-9:
            continue
        for second in range(len(solution.routes)):
            if second == relay.first_drone:
                continue
            if second == relay.second_drone and station.id == relay.station_id:
                continue
            if deadline is not None and perf_counter() >= deadline:
                break
            for drop_gap in (relay.drop_position,):
                for pick_gap in (relay.pick_position,):
                    new_relay = RelayTransfer(
                        id=relay.id,
                        task_id=task_id,
                        station_id=station.id,
                        first_drone=relay.first_drone,
                        second_drone=second,
                        drop_position=drop_gap,
                        pick_position=pick_gap,
                    )
                    new_relays = [
                        r if r.id != relay.id else new_relay for r in solution.relays
                    ]
                    candidate = RelaySolution(
                        solution.routes, tuple(new_relays), solution.stations
                    )
                    candidates.append(candidate)
    for candidate in candidates:
        evaluation = evaluate_relay_solution(problem, candidate)
        if evaluation.valid and evaluation.score < best_score:
            best = candidate
            best_score = evaluation.score
    return best


# ---------------------------------------------------------------------------
# Stage I: Location-Allocation + Initial Mixed Solution Construction.
# ---------------------------------------------------------------------------

def multi_mode_regret2_constructor(
    problem: Problem,
    *,
    stations: Sequence[RelayStation],
    guidance: Mapping[int, int] | None = None,
    limits: Mapping[str, int],
    rng: Random,
    deadline: float | None = None,
) -> RelaySolution:
    """Stage-I mixed Direct/Relay construction from empty routes.

    Each uninserted task is offered Direct insertions and fresh Relay
    insertions (preferred station first when guided); the two-layer regret-2
    rule picks the task/alternative with the highest regret.
    """

    evaluator = RouteEvaluator(problem)
    solution = RelaySolution(
        tuple(() for _ in range(problem.drone_count)),
        relays=(),
        stations=tuple(stations),
    )
    return relay_repair(
        problem,
        evaluator,
        solution,
        list(problem.task_ids),
        stations=stations,
        strategy="relay_regret2",
        limits=limits,
        rng=rng,
        guidance=guidance,
        deadline=deadline,
    )


def _stage1_location_allocation(
    problem: Problem,
    cfg: "RelaySearchConfig",
    deadline: float | None,
    rng: Random,
    direct: "SolverResult | None" = None,
) -> tuple[
    tuple[RelayStation, ...],
    LocationAllocationResult | None,
    dict[int, int],
]:
    """Quick direct diagnosis -> B_ir -> 0-1 Location-Allocation.

    The greedy layout is kept as the MILP fallback; when budget allows, a
    short operational probe ranks the MILP layout against the greedy layout
    under the formal ``(late_count, distance_km)`` order.
    """

    from .search import SolverResult

    if direct is None:
        direct = construct_regret_initial(problem, candidate_limit=48)
    routes = direct.routes
    context = build_route_context(problem, routes)
    candidates = candidate_sites(problem)
    matrix = precompute_potential_matrix(problem, routes, context, candidates)
    greedy = greedy_station_selection(
        matrix, problem.task_ids, candidates, cfg.num_stations, rng=rng
    )

    # Adaptive Stage-I: if the direct solution shows almost no relay-
    # rescuable task (pickup on time but delivery late), the expensive MILP
    # and the operational probe would burn budget for zero benefit.  Skip
    # them and keep the fast greedy layout + empty guidance.
    rescuable = _count_rescuable_tasks(problem, routes)
    skip_lap = cfg.adaptive_stage1 and rescuable < cfg.rescuable_threshold
    if skip_lap:
        lap = None
        milp_layout = greedy
    else:
        lap = solve_location_allocation(
            problem,
            candidates,
            matrix,
            cfg.num_stations,
            fallback_layout=greedy,
            time_limit=min(
                cfg.lap_time_limit_seconds,
                max(0.1, (deadline - perf_counter()) if deadline else 12.0),
            ),
            seed=cfg.seed,
        )
        candidate_by_id = {station.id: station for station in candidates}
        milp_layout = tuple(
            candidate_by_id[sid] for sid in lap.selected_station_ids
        )

    # Optional short operational probe: MILP layout vs greedy layout.
    selected = milp_layout
    guidance = {}
    if not skip_lap:
        guidance = dict(lap.task_station_guidance)
        if deadline is not None and perf_counter() < deadline:
            from .relay_location import operational_probe

            base_solution = relay_solution_from_routes(routes)
            best_pair: tuple[tuple[int, int, float], object] | None = None
            for layout in (milp_layout, greedy):
                _, evaluation, stats = operational_probe(
                    problem,
                    base_solution,
                    layout,
                    candidate_limits={
                        "max_candidates": 4,
                        "second_drone_limit": 2,
                        "drop_gap_limit": 2,
                        "pick_gap_limit": 2,
                        "delivery_gap_limit": 2,
                    },
                    priority_b_limit=12,
                    deadline=deadline,
                    seed=cfg.seed,
                )
                key = (
                    evaluation.score.late_count,
                    evaluation.score.distance_km,
                    -stats.feasible_relay_count,
                )
                if best_pair is None or key < best_pair[0]:
                    best_pair = (key, layout)
            if best_pair is not None:
                selected = best_pair[1]

    # Recompute guidance for the finally selected layout: each task's argmax
    # station among the selected set (positive benefit only).
    selected_ids = {station.id for station in selected}
    guidance = {}
    for task_id in problem.task_ids:
        best_station: int | None = None
        best_value = 0.0
        for station in selected:
            value = matrix.get((task_id, station.id), 0.0)
            if value > best_value:
                best_value = value
                best_station = station.id
        if best_station is not None and best_value > 0.0:
            guidance[task_id] = best_station
    return selected, lap, guidance


def _count_rescuable_tasks(problem: Problem, routes: Sequence[Sequence[int]]) -> int:
    """Count tasks that a relay could plausibly rescue in the direct solution.

    A task is counted when its pickup happens on time but its delivery is
    late (the direct-rescue case: the first drone can hand it off so a
    receiver delivers it).  Tasks whose pickup is already late cannot be
    rescued by relay (relay cannot turn back time), and in the tight 8x25
    instance almost no task falls into the first class.
    """
    speed = problem.speed_km_per_min
    delivery_times: dict[int, float] = {}
    pickup_times: dict[int, float] = {}
    for route in routes:
        elapsed = 0.0
        previous: int | None = None
        for visit in route:
            elapsed += problem.distance(previous, visit) / speed
            previous = visit
            tid = abs(visit)
            if visit > 0:
                pickup_times[tid] = elapsed
            else:
                delivery_times[tid] = elapsed
    count = 0
    for tid in problem.task_ids:
        pickup = pickup_times.get(tid, float("inf"))
        delivery = delivery_times.get(tid, float("inf"))
        deadline = problem.task(tid).deadline_min
        if pickup <= deadline + 1e-9 and delivery > deadline + 1e-9:
            count += 1
    return count


def _initial_relay_allocation(
    problem: Problem,
    solution: RelaySolution,
    guided_tasks: Sequence[int],
    *,
    stations: Sequence[RelayStation],
    limits: Mapping[str, int],
    deadline: float | None,
) -> RelaySolution:
    """Realise the LAP task guidance: convert guided tasks to relays when a
    strict ``(late_count, distance_km)`` improvement exists."""

    current = solution
    for task_id in guided_tasks:
        if deadline is not None and perf_counter() >= deadline:
            break
        candidates = relay_candidates_for_task(
            problem,
            current,
            task_id,
            stations=stations,
            second_drone_limit=int(limits.get("second_drone_limit", 2)),
            drop_gap_limit=int(limits.get("drop_gap_limit", 3)),
            pick_gap_limit=int(limits.get("pick_gap_limit", 3)),
            delivery_gap_limit=int(limits.get("delivery_gap_limit", 3)),
            max_candidates=int(limits.get("max_candidates", 6)),
            deadline=deadline,
        )
        improving = [c for c in candidates if c.delta_score < Score(0, 0.0)]
        if improving:
            chosen = min(improving, key=lambda c: c.delta_score)
            current = apply_relay_candidate(problem, current, chosen)
    return current


def stage1_initial_solution(
    problem: Problem,
    *,
    cfg: "RelaySearchConfig",
    deadline: float | None,
    rng: Random,
    frozen_stations: Sequence[RelayStation] | None = None,
    lap_result: LocationAllocationResult | None = None,
) -> tuple[RelaySolution, dict[str, object]]:
    """Build the Stage-I mixed Direct/Relay initial solution ``S0``.

    Pipeline: quick direct diagnosis -> B_ir -> 0-1 Location-Allocation
    (stations + task guidance) -> direct foundation + initial relay
    allocation -> relay route adjustment.
    """

    started = perf_counter()
    if frozen_stations is not None:
        stations = tuple(frozen_stations)
        guidance: dict[int, int] = {}
        lap = lap_result
    else:
        stations, lap, guidance = _stage1_location_allocation(
            problem, cfg, deadline, rng
        )

    # Direct foundation (fast regret-2; a short ALNS pass would need budget).
    direct = construct_regret_initial(problem, candidate_limit=48)
    solution = relay_solution_from_routes(direct.routes, stations=stations)
    foundation_evaluation = evaluate_relay_solution(problem, solution)

    # Initial task-to-station allocation (relay conversion of guided tasks).
    guided_tasks = sorted(
        guidance,
        key=lambda task_id: -problem.task(task_id).deadline_min,
    )
    solution = _initial_relay_allocation(
        problem,
        solution,
        guided_tasks,
        stations=stations,
        limits=cfg.candidate_limits,
        deadline=deadline,
    )
    allocation_evaluation = evaluate_relay_solution(problem, solution)

    # Relay route adjustment (bounded) over the affected neighbourhood.
    solution = relay_route_adjustment(
        problem,
        solution,
        guided_tasks,
        stations=stations,
        limits=cfg.candidate_limits,
        deadline=deadline,
    )
    s0_evaluation = evaluate_relay_solution(problem, solution)
    info = {
        "stage1_runtime_seconds": perf_counter() - started,
        "lap_status": lap.model_status if lap else None,
        "lap_runtime_seconds": lap.model_runtime if lap else None,
        "lap_objective": lap.objective_value if lap else None,
        "guided_relay_tasks": len(guidance),
        "foundation_score": (
            foundation_evaluation.score.late_count,
            foundation_evaluation.score.distance_km,
        ),
        "allocation_score": (
            allocation_evaluation.score.late_count,
            allocation_evaluation.score.distance_km,
        ),
        "s0_score": (
            s0_evaluation.score.late_count,
            s0_evaluation.score.distance_km,
        ),
    }
    return solution, info


def _inline_relay_routes(solution: RelaySolution) -> Routes:
    """Merge relay split routes into balanced direct routes (A2-compatible).

    For each relay transfer, the delivery ``-task_id`` is moved from the
    second drone to the first drone (immediately after the pickup), creating
    a route that the per-route ``RouteEvaluator`` can score.
    """

    if not solution.relays:
        return solution.routes
    routes = [list(r) for r in solution.routes]
    for relay in solution.relays:
        tid = relay.task_id
        first = routes[relay.first_drone]
        second = routes[relay.second_drone]
        if -tid in second:
            second.remove(-tid)
        if tid in first:
            pidx = first.index(tid)
            first.insert(pidx + 1, -tid)
    return tuple(tuple(r) for r in routes)


# ---------------------------------------------------------------------------
# Stage II: relay-aware ALNS improvement.
# ---------------------------------------------------------------------------

def _stage2_improve(
    problem: Problem,
    s0: RelaySolution,
    *,
    stations: Sequence[RelayStation],
    guidance: Mapping[int, int],
    cfg: "RelaySearchConfig",
    deadline: float | None,
    rng: Random,
) -> tuple[RelaySolution, RelaySolutionEvaluation, dict[str, object]]:
    """Run the relay-aware ALNS improvement loop (Stage II)."""

    evaluator = RouteEvaluator(problem)
    # Stage II operates on relay-free routes (A2-compatible).  If Stage I
    # seeded relay transfers we inline them back to direct routes — current
    # stays clean for the A2 destroy/repair operators.
    current_routes = _inline_relay_routes(s0) if s0.relays else s0.routes
    current = RelaySolution(current_routes, (), s0.stations)
    current_evaluation = evaluate_relay_solution(problem, current)
    best = s0 if s0.relays else current
    best_evaluation = evaluate_relay_solution(problem, s0) if s0.relays else current_evaluation

    # Stage II reuses the full plain-ALNS operator set (the high-quality
    # destroy/repair arsenal that the standalone A2 baseline uses) so the
    # relay-aware search is never weaker than the baseline, and layers the
    # relay mechanism on top: relay-aware destroy (for relay tasks) and a
    # per-iteration relay route-adjustment pass that tries Direct -> Relay
    # conversions on the repaired neighbourhood.
    destroy_operators = tuple(
        name
        for name in ALNS_DESTROY_OPERATORS
        if (cfg.enable_hypergraph_destroy or name != "hypergraph_destroy")
    )
    repair_operators = tuple(
        name
        for name in ALNS_REPAIR_OPERATORS
        if (cfg.enable_cluster_repair or name != "cluster_regret")
        and (cfg.enable_pair_repair or name != "pair_regret")
    )
    destroy_weights = {op: 1.0 for op in destroy_operators}
    repair_weights = {op: 1.0 for op in repair_operators}
    destroy_uses: dict[str, int] = defaultdict(int)
    repair_uses: dict[str, int] = defaultdict(int)
    destroy_gains: dict[str, float] = defaultdict(float)
    repair_gains: dict[str, float] = defaultdict(float)
    destroy_improvements: dict[str, int] = defaultdict(int)
    repair_improvements: dict[str, int] = defaultdict(int)
    destroy_runtime: dict[str, float] = defaultdict(float)
    repair_runtime: dict[str, float] = defaultdict(float)

    temperature = cfg.initial_temperature
    no_best_improvement = 0
    completed_iterations = 0
    accepted_solutions = 0
    accepted_worse_count = 0
    evaluator_calls = 0

    def _stage2_iteration() -> None:
        """One destroy/repair iteration; ``continue`` semantics via return."""
        nonlocal temperature, no_best_improvement, current, current_evaluation
        nonlocal best, best_evaluation, evaluator_calls
        nonlocal accepted_solutions, accepted_worse_count

        remove_count = max(
            2,
            round(
                len(problem.tasks)
                * rng.uniform(cfg.min_destroy_fraction, cfg.max_destroy_fraction)
            ),
        )

        # ── A2 destroy/repair (relay-free) ──
        # current is always kept relay-free so the proven A2 operators work
        # at full speed.  Relay transfers are layered on top as a post-repair
        # Direct→Relay conversion probe; if a relay conversion improves the
        # primary objective the relayed solution is recorded as *best* but
        # *current* stays direct (so the A2 machinery keeps optimising).
        t0 = perf_counter()
        partial_routes, removed = alns_destroy(
            problem, evaluator, current.routes, destroy_op, remove_count, rng
        )
        destroy_runtime[destroy_op] += perf_counter() - t0
        if not removed:
            return

        t0 = perf_counter()
        repaired_routes = alns_repair(
            problem,
            evaluator,
            partial_routes,
            removed,
            repair_op,
            candidate_limit=cfg.candidate_limit,
            deadline=deadline,
        )
        repair_runtime[repair_op] += perf_counter() - t0

        repaired = RelaySolution(repaired_routes, (), current.stations)

        # Ejection-swap reinforcement (plain-ALNS parity, relay-free only).
        if (
            cfg.enable_ejection
            and cfg.ejection_interval > 0
            and completed_iterations % cfg.ejection_interval == 0
        ):
            ejected_routes = _ejection_swap_improve(
                problem,
                evaluator,
                repaired.routes,
                cfg.candidate_limit,
                rng,
                cfg.ejection_trials,
                deadline,
            )
            repaired = RelaySolution(ejected_routes, (), repaired.stations)

        # ── Relay probing (Direct → Relay conversion) ──
        # Two sources of probe targets:
        #   (a) urgent_removed – tasks just re-inserted, sorted by deadline
        #   (b) rescue_targets – upstream blockers of the most pick-late
        #       tasks already placed in the repaired solution
        urgent_removed = sorted(
            removed, key=lambda t: problem.task(t).deadline_min
        )[: cfg.relay_adjust_probe_limit]
        rescue_targets = _relay_rescue_targets(
            problem, repaired, cfg.relay_rescue_probe_limit, deadline=deadline
        )
        affected_tasks = list(urgent_removed) + rescue_targets
        if affected_tasks:
            light_limits = {
                **cfg.candidate_limits,
                "second_drone_limit": 2,
                "drop_gap_limit": 2,
                "pick_gap_limit": 3,
                "delivery_gap_limit": 2,
                "max_candidates": 4,
                "max_evaluations": 24,
            }
            probed = relay_route_adjustment(
                problem,
                repaired,
                affected_tasks,
                stations=stations,
                limits=light_limits,
                deadline=deadline,
            )
            # If relay probing produced a transfer, evaluate it with the
            # full DAG and consider it for *best* (not current – current
            # stays relay-free for the next A2 round).
            if probed.relays:
                probed_ev = evaluate_relay_solution(problem, probed)
                evaluator_calls += 1
                if probed_ev.valid and probed_ev.score < best_evaluation.score:
                    best = probed
                    best_evaluation = probed_ev
                    no_best_improvement = 0
            # Use the probed version for SA acceptance too (it may be
            # relay-free if no conversion was accepted).
            repaired = probed

        # ── SA acceptance ──
        candidate_evaluation = evaluate_relay_solution(problem, repaired)
        evaluator_calls += 1
        if not candidate_evaluation.valid:
            return
        candidate_score = candidate_evaluation.score
        current_score = current_evaluation.score

        improved = False
        if candidate_score < best_evaluation.score:
            # If repaired has relays, best was already updated above.
            if not repaired.relays:
                best = repaired
                best_evaluation = candidate_evaluation
            improved = True
            accepted_solutions += 1
            no_best_improvement = 0
            reward = 8.0
        elif candidate_score < current_score:
            # Accept into current only if relay-free (A2-compatible).
            if not repaired.relays:
                current = repaired
                current_evaluation = candidate_evaluation
            improved = True
            accepted_solutions += 1
            reward = 4.0
        elif _accept_worse(
            candidate_score, current_score, problem, temperature, rng
        ):
            # Accept into current only if relay-free (A2-compatible).
            if not repaired.relays:
                current = repaired
                current_evaluation = candidate_evaluation
            accepted_solutions += 1
            accepted_worse_count += 1
            reward = 1.0
        else:
            reward = 0.0

        destroy_gains[destroy_op] += reward
        repair_gains[repair_op] += reward
        if improved:
            destroy_improvements[destroy_op] += 1
            repair_improvements[repair_op] += 1

        if completed_iterations % cfg.weight_update_interval == 0:
            for weights, uses, gains in (
                (destroy_weights, destroy_uses, destroy_gains),
                (repair_weights, repair_uses, repair_gains),
            ):
                for op, weight in list(weights.items()):
                    usage = uses[op]
                    score = gains[op] / usage if usage > 0 else 1.0
                    weights[op] = max(
                        cfg.minimum_weight,
                        cfg.reaction_factor * score
                        + (1.0 - cfg.reaction_factor) * weight,
                    )
                    uses[op] = 0
                    gains[op] = 0.0

        temperature = max(cfg.minimum_temperature, temperature * cfg.cooling_rate)
        no_best_improvement += 1
        if no_best_improvement >= cfg.recall_threshold:
            # Recall to best — inline relays so current stays A2-compatible.
            current = RelaySolution(
                _inline_relay_routes(best), (), best.stations
            )
            current_evaluation = evaluate_relay_solution(problem, current)
            temperature = cfg.reheat_factor * cfg.initial_temperature
            no_best_improvement = 0

    while deadline is None or perf_counter() < deadline:
        completed_iterations += 1
        destroy_op = _roulette(destroy_weights, rng)
        repair_op = _roulette(repair_weights, rng)
        destroy_uses[destroy_op] += 1
        repair_uses[repair_op] += 1
        try:
            _stage2_iteration()
        except _SearchDeadlineReached:
            break

    metadata = {
        "method": "RSLA-2S-ALNS",
        "seed": cfg.seed,
        "iterations": completed_iterations,
        "evaluator_calls": evaluator_calls,
        "accepted_solutions": accepted_solutions,
        "accepted_worse_count": accepted_worse_count,
        "destroy_uses": MappingProxyType(dict(destroy_uses)),
        "repair_uses": MappingProxyType(dict(repair_uses)),
        "destroy_improvements": MappingProxyType(dict(destroy_improvements)),
        "repair_improvements": MappingProxyType(dict(repair_improvements)),
        "destroy_runtime_seconds": MappingProxyType(dict(destroy_runtime)),
        "repair_runtime_seconds": MappingProxyType(dict(repair_runtime)),
        "destroy_weights": MappingProxyType(dict(destroy_weights)),
        "repair_weights": MappingProxyType(dict(repair_weights)),
        "relay_count": len(best.relays),
    }
    return best, best_evaluation, metadata


def solve_relay(
    problem: Problem,
    *,
    stations: Sequence[RelayStation] | None = None,
    config: RelaySearchConfig | None = None,
    initial_routes: Sequence[Sequence[int]] | None = None,
) -> RelaySolverResult:
    """Two-stage relay search: Stage I (Location-Allocation + construction)
    and Stage II (relay-aware ALNS improvement).

    ``stations`` freezes the layout when provided (Stage-I LAP is skipped);
    otherwise Stage I selects them within its budget share.  The total budget
    is ``config.time_limit_seconds`` with Stage I capped at
    ``(module0_share + stage1_share)`` of it.
    """

    cfg = config or RelaySearchConfig()
    started = perf_counter()
    deadline = (
        None
        if cfg.time_limit_seconds is None
        else started + cfg.time_limit_seconds
    )
    stage1_ceiling = (
        None
        if deadline is None
        else started + cfg.time_limit_seconds * (cfg.module0_share + cfg.stage1_share)
    )
    rng = Random(cfg.seed)

    # Stage I: one direct foundation (used for diagnosis, B_ir and S0).
    if initial_routes is not None:
        foundation_routes = tuple(tuple(r) for r in initial_routes)
        direct_foundation = None
    else:
        if cfg.foundation == "greedy":
            from .alns import construct_greedy_initial

            direct_foundation = construct_greedy_initial(
                problem, candidate_limit=48
            )
        else:
            direct_foundation = construct_regret_initial(
                problem, candidate_limit=48
            )
        foundation_routes = direct_foundation.routes

    lap_result: LocationAllocationResult | None = None
    if stations is not None:
        frozen = tuple(stations)
        guidance: dict[int, int] = {}
    else:
        frozen, lap_result, guidance = _stage1_location_allocation(
            problem, cfg, stage1_ceiling, rng, direct=direct_foundation
        )

    # Stage I: initial mixed solution from the foundation.
    s0 = relay_solution_from_routes(foundation_routes, stations=frozen)
    guided_tasks = sorted(
        guidance, key=lambda t: -problem.task(t).deadline_min
    )
    if guided_tasks:
        s0 = _initial_relay_allocation(
            problem,
            s0,
            guided_tasks,
            stations=frozen,
            limits=cfg.candidate_limits,
            deadline=stage1_ceiling,
        )
    s0 = relay_route_adjustment(
        problem,
        s0,
        guided_tasks,
        stations=frozen,
        limits=cfg.candidate_limits,
        deadline=stage1_ceiling,
    )
    s0_evaluation = evaluate_relay_solution(problem, s0)
    stage1_info = {
        "stage1_runtime_seconds": perf_counter() - started,
        "foundation_score": (
            (direct_foundation.evaluation.score.late_count, direct_foundation.evaluation.score.distance_km)
            if direct_foundation is not None
            else None
        ),
        "guided_relay_tasks": len(guidance),
        "s0_score": (
            s0_evaluation.score.late_count,
            s0_evaluation.score.distance_km,
        ),
    }

    # Stage II: relay-aware ALNS improvement with all remaining time.
    best, best_evaluation, stage2_metadata = _stage2_improve(
        problem,
        s0,
        stations=frozen,
        guidance=guidance,
        cfg=cfg,
        deadline=deadline,
        rng=rng,
    )

    metadata = {
        **stage2_metadata,
        "stage1": MappingProxyType(stage1_info),
        "lap": (
            MappingProxyType(
                {
                    "status": lap_result.model_status,
                    "runtime_seconds": lap_result.model_runtime,
                    "objective": lap_result.objective_value,
                    "reduced_candidates": lap_result.reduced_candidate_count,
                    "solver": lap_result.solver,
                }
            )
            if lap_result is not None
            else None
        ),
        "station_ids": tuple(station.id for station in frozen),
        "station_sources": tuple(station.source_visit for station in frozen),
        "runtime_seconds": perf_counter() - started,
    }
    return RelaySolverResult(
        routes=best.routes,
        relays=best.relays,
        stations=best.stations,
        evaluation=best_evaluation,
        runtime_seconds=perf_counter() - started,
        iterations=int(stage2_metadata["iterations"]),
        metadata=MappingProxyType(metadata),
    )
