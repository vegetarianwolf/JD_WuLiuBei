"""Pickup-Aware Cooperative HALNS (formal DIRECT / OWNERSHIP / RELAY search).

Formal architecture (this round):

    Balanced Ownership Reassignment      -- reconfigures *complete* task
                                            responsibility before the original
                                            customer pickup (DIRECT tasks only).
    Criticality-Guided Dynamic Relay     -- after a task has started executing,
                                            hands the *second-half delivery
                                            responsibility* to another drone
                                            (A: P_j -> h ; B: h -> D_j).

The search state is a complete
:class:`~uav_dispatch.cooperative_validation.CooperativeSolution`:

    routes + relays                 (execution structure, source of truth)
    -> task_services / ownership    (derived views, kept in sync)

Service modes are only ``DIRECT`` and ``RELAY``.  Swap is NOT part of the
formal path (``swap_model`` / ``swap_search`` / ``swap_validation`` remain
legacy standalone modules).

The formal objective is strictly ``(late_count, distance_km)``.  All pickup
slack / delivery-risk / pickup-advance diagnostics are search *guidance*
only and never enter the ``Score`` order.  ``total_lateness_min`` remains
diagnostic-only.

Main loop (per iteration)::

    Destroy (adaptive roulette over a config-built pool)
        ↓
    Direct / Ownership Repair (adaptive roulette)
        ↓
    Criticality-Guided Dynamic Relay (fixed relay_interval, +1 relay max)
        ↓
    Unified Cooperative Validation
        ↓
    HALNS / SA Acceptance (never increases late_count; best strictly
                           (late_count, distance_km))
"""
from __future__ import annotations

from dataclasses import dataclass, field
from random import Random
from time import perf_counter
from typing import Mapping, Sequence

from .alns import (
    ALNSConfig,
    DESTROY_OPERATORS as A2_DESTROY_OPERATORS,
    REPAIR_OPERATORS as A2_REPAIR_OPERATORS,
    _SearchDeadlineReached,
    _accept_worse,
    _ejection_swap_improve as _a2_ejection_improve,
    _roulette,
    destroy_solution as _a2_destroy_solution,
    solve_alns_core,
)
from .cooperative_model import (
    ServiceMode,
    pickup_drone_map,
    pickup_slacks as _pickup_slacks,
    rebuild_services,
    route_is_complete,
    service_mode_counts,
)
from .cooperative_validation import (
    CooperativeSolution,
    CooperativeValidation,
    validate_cooperative_solution,
)
from .dynamic_relay import (
    DynamicRelayPointPool,
    RelayCandidate,
    RelayTrigger,
    estimate_blocker_release_gain,
)
from .model import Problem, Score
from .ownership_reassignment import find_upstream_blockers
from .relay_candidates import (
    _receiver_window_quality,
    apply_relay_candidate,
    relay_candidates_for_task,
)
from .relay_model import RelayStation
from .search import RouteEvaluator, Routes, routes_score


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class CooperativeHALNSConfig:
    """Search-effort controls for the Pickup-Aware Cooperative HALNS.

    None of the ``*_limit`` fields are problem constraints — they only bound
    the computational effort of each operator / neighborhood.

    Deprecated swap / dynamic-station controls are intentionally removed:
    the formal path never runs a swap neighborhood or a pre-activated
    station shortlist.
    """

    seed: int = 2026080500
    time_limit_seconds: float | None = None
    max_iterations: int = 400
    candidate_limit: int | None = 48
    min_destroy_fraction: float = 0.04
    max_destroy_fraction: float = 0.10
    weight_update_interval: int = 40
    reaction_factor: float = 0.2
    minimum_weight: float = 0.05
    initial_temperature: float = 0.03
    minimum_temperature: float = 0.0005
    cooling_rate: float = 0.995

    # -- feature toggles --
    enable_ownership: bool = True
    #: Master toggle for the post-repair Criticality-Guided Dynamic Relay.
    enable_relay: bool = True
    #: Relay candidate guidance: task's own delivery is late / near-late.
    enable_delivery_risk_relay: bool = True
    #: Relay candidate guidance: task blocks a critical downstream pickup.
    enable_blocker_relay: bool = True

    # -- A2 base-layer operator toggles (mirror ALNSConfig) --
    #: Include ``assignment_destroy`` (excludes ``route_clear``) like A2.
    enable_assignment_destroy: bool = True
    #: Include the expensive ``hypergraph_destroy`` (A2 default: off).
    enable_hypergraph_destroy: bool = False
    #: Include ``cluster_regret`` / ``pair_regret`` repair (A2 default: off).
    enable_cluster_repair: bool = False
    enable_pair_repair: bool = False
    cluster_bundle_candidate_limit: int = 12
    cluster_pair_limit: int = 6
    pair_candidate_limit: int = 8

    # -- A2 base-layer ejection neighborhood (mirror ALNSConfig) --
    # The project's A2 baseline is ``solve_alns_core``, which runs with
    # ``enable_ejection=False``.  Keeping this False here makes the main
    # Cooperative-HALNS trajectory bit-for-bit identical to that baseline so
    # the comparison isolates the cooperative mechanisms.  Set to True only
    # for experiments that deliberately add the ejection reinforcement.
    enable_ejection: bool = False
    ejection_interval: int = 25
    ejection_trials: int = 24

    # -- initial solution (A2 warm start) --
    initial_max_iterations: int = 60
    initial_time_limit_seconds: float | None = None

    # -- operator budgets --
    critical_task_limit: int = 6
    blocker_per_task_limit: int = 3
    #: Delivery slack (deadline - t(D_i)) below which a DIRECT task is a
    #: delivery-risk candidate (negative = already late).  Search param only.
    delivery_risk_slack_limit: float = 15.0
    #: Pickup slack below which a pickup counts as critical (guidance only).
    near_critical_slack_limit: float = 10.0
    relay_receiver_limit: int = 1
    relay_point_candidate_limit: int = 3
    relay_candidate_limit: int = 1
    relay_max_evaluations: int = 3
    #: Per-route insertion candidate limit used inside repair.
    repair_candidate_limit: int | None = 24

    # -- cooperative neighborhood schedules (strict-improvement, on best) --
    #: Run the Balanced Ownership neighborhood every ``ownership_interval``
    #: iterations.  It operates on DIRECT tasks only and accepts only strict
    #: formal improvements of ``best`` (never worsens the trajectory).
    ownership_interval: int = 50
    #: Max balanced-pair exchanges attempted inside one ownership call.
    ownership_pair_trials: int = 3
    #: Run the dynamic-relay neighborhood every ``relay_interval`` iterations
    #: (fixed schedule — no fake adaptive relay weight).
    relay_interval: int = 30
    #: Max relay transfers added per neighborhood call (default 1).
    relay_additions_per_call: int = 1
    #: Optional hard cap on relay transfers per solution (None = unlimited).
    max_relays_per_solution: int | None = None

    verbose: bool = True


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class CooperativeHALNSResult:
    """Full result of the Pickup-Aware Cooperative HALNS."""

    solution: CooperativeSolution
    validation: CooperativeValidation
    runtime_seconds: float
    iterations: int
    time_to_best_seconds: float
    initial_score: Score
    best_score: Score
    # -- pickup-first diagnostics (guidance only, never objective) --
    pickup_late_count: int
    near_critical_pickup_count: int
    negative_pickup_slack_sum: float
    min_pickup_slack: float
    blocker_count: int
    blocker_release_potential: float
    # -- mechanism statistics --
    op_stats: dict = field(default_factory=dict)
    operator_weights: dict = field(default_factory=dict)
    service_distribution: dict = field(default_factory=dict)
    # -- legacy (deprecated) station fields, kept for the compat wrapper --
    station_adds: int = 0
    station_drops: int = 0
    station_replaces: int = 0
    active_station_count: int = 0
    # -- new cooperative-mechanism statistics --
    ownership_calls: int = 0
    ownership_accepted: int = 0
    relay_calls: int = 0
    delivery_risk_tasks_detected: int = 0
    blockers_detected: int = 0
    relay_candidates_before_dedup: int = 0
    relay_candidates_after_dedup: int = 0
    relay_candidates_screened_out: int = 0
    relay_exact_evaluations: int = 0
    relay_feasible: int = 0
    relay_accepted: int = 0
    delivery_risk_relays_accepted: int = 0
    blocker_relays_accepted: int = 0
    dual_trigger_relays_accepted: int = 0
    late_tasks_rescued: int = 0
    critical_pickups_advanced: int = 0
    relay_count_final: int = 0
    unique_relay_points_used: int = 0
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _empty_op_stats() -> dict:
    return {
        "calls": 0,
        "candidates": 0,
        "feasible": 0,
        "accepted": 0,
        "improving": 0,
        "best_improving": 0,
        "final_weight": 1.0,
    }


def _make_solution(
    problem: Problem,
    routes: Sequence[Sequence[int]],
    relays: Sequence = (),
    swaps: Sequence = (),
    active_stations: Sequence[RelayStation] = (),
) -> CooperativeSolution:
    """Build a CooperativeSolution, re-deriving services from raw state.

    This is the single reconciliation point: route / relay can never drift
    apart because ``task_services`` and ``ownership`` are always recomputed
    from the raw layers.
    """
    materialized = tuple(tuple(route) for route in routes)
    services = rebuild_services(materialized, relays, swaps)
    ownership = {
        task_id: state.primary_owner
        for task_id, state in services.items()
    }
    return CooperativeSolution(
        routes=materialized,
        relays=tuple(relays),
        swaps=tuple(swaps),
        active_stations=tuple(active_stations),
        ownership=ownership,
        task_services=services,
    )


def _prune_active_stations(
    solution: CooperativeSolution,
    pool: DynamicRelayPointPool,
) -> CooperativeSolution:
    """Keep only the relay points actually used by ``solution.relays``.

    ``active_stations`` is a derived view of the used relay points — it is
    never a pre-activated candidate shortlist.
    """
    used_ids = {relay.station_id for relay in solution.relays}
    by_id = {st.id: st for st in solution.active_stations}
    active: list[RelayStation] = []
    seen: set[int] = set()
    for relay in solution.relays:
        station = by_id.get(relay.station_id) or pool.station_for_id(
            relay.station_id
        )
        if station is not None and station.id not in seen:
            active.append(station)
            seen.add(station.id)
    if tuple(active) == solution.active_stations:
        return solution
    return CooperativeSolution(
        routes=solution.routes,
        relays=solution.relays,
        swaps=solution.swaps,
        active_stations=tuple(active),
        ownership=solution.ownership,
        task_services=solution.task_services,
    )


def _choose_ranked(
    ranked: Sequence[int], count: int, rng: Random, exponent: float = 3.0
) -> list[int]:
    """Rank-based randomised selection weighted toward the head of the list."""
    available = list(ranked)
    chosen: list[int] = []
    while available and len(chosen) < count:
        index = int((rng.random() ** exponent) * len(available))
        chosen.append(available.pop(index))
    return chosen


def _pickup_risk_rank(
    slacks: Mapping[int, float], tasks: Sequence[int]
) -> list[int]:
    """Rank tasks by pickup risk with *prevention* priority.

    Borderline tasks (slack just below / around zero) rank first — they are
    the ones a re-route can still save.  Deeply-negative-slack tasks rank
    last: they are no longer directly rescuable and should instead be handled
    by blocker-release logic.  This is the opposite of "most negative first".
    """

    def key(task_id: int) -> tuple[int, float, int]:
        slack = slacks.get(task_id, 0.0)
        if -5.0 <= slack < 15.0:        # near-critical / about to be late
            priority = 0
        elif -20.0 <= slack < -5.0:     # already a bit late
            priority = 1
        elif 15.0 <= slack < 40.0:      # loose but not trivial
            priority = 2
        else:                           # deeply late or very loose
            priority = 3
        return (priority, abs(slack), -task_id)

    return sorted(tasks, key=key)


def _pickup_diagnostics(
    problem: Problem, solution: CooperativeSolution
) -> dict:
    """Pickup-first diagnostics — guidance only, never the Score."""
    slacks = _pickup_slacks(problem, solution.routes)
    pickup_late = sum(1 for slack in slacks.values() if slack < 0)
    near_critical = sum(
        1
        for slack in slacks.values()
        if -5.0 <= slack < 15.0
    )
    negative_sum = sum(slack for slack in slacks.values() if slack < 0)
    min_slack = min(slacks.values(), default=0.0)

    blocker_count = 0
    blocker_potential = 0.0
    evaluator = RouteEvaluator(problem)
    critical = [
        task_id
        for task_id, slack in slacks.items()
        if slack < 15.0
    ]
    for task_id in critical[:8]:
        drone = pickup_drone_map(solution.routes).get(task_id)
        if drone is None:
            continue
        try:
            blockers = find_upstream_blockers(
                problem, evaluator, solution.routes[drone], task_id
            )
        except (ValueError, RuntimeError):
            continue
        blocker_count += len(blockers)
        blocker_potential += sum(holding for _, holding in blockers)

    return {
        "pickup_late_count": pickup_late,
        "near_critical_pickup_count": near_critical,
        "negative_pickup_slack_sum": negative_sum,
        "min_pickup_slack": min_slack,
        "blocker_count": blocker_count,
        "blocker_release_potential": blocker_potential,
    }


# ---------------------------------------------------------------------------
# Initial solution
# ---------------------------------------------------------------------------


def _build_initial_solution(
    problem: Problem, cfg: CooperativeHALNSConfig
) -> CooperativeSolution:
    """A2 warm-start wrapped as an all-DIRECT CooperativeSolution.

    The seed is built with the fast greedy constructor first (on large
    instances the regret-2 constructor alone costs ~13s and would consume the
    entire warm-start budget), then the remaining warm budget is handed to
    ``solve_alns_core`` to polish it.
    """
    from .alns import construct_greedy_initial

    seed = construct_greedy_initial(
        problem, candidate_limit=cfg.candidate_limit
    )
    if cfg.initial_time_limit_seconds is not None:
        a2 = solve_alns_core(
            problem,
            config=ALNSConfig(
                max_iterations=100_000,
                time_limit_seconds=cfg.initial_time_limit_seconds,
                seed=cfg.seed,
                candidate_limit=cfg.candidate_limit,
            ),
            initial_routes=seed.routes,
        )
    else:
        a2 = solve_alns_core(
            problem,
            config=ALNSConfig(
                max_iterations=cfg.initial_max_iterations,
                seed=cfg.seed,
                candidate_limit=cfg.candidate_limit,
            ),
            initial_routes=seed.routes,
        )
    return _make_solution(problem, a2.routes)


# ---------------------------------------------------------------------------
# Service-unit removal
# ---------------------------------------------------------------------------


def _remove_task_services(
    problem: Problem,
    solution: CooperativeSolution,
    task_ids: Sequence[int],
) -> tuple[CooperativeSolution, set[int]]:
    """Remove complete TaskServiceState units (DIRECT / RELAY).

    Returns ``(partial_solution, actually_removed_ids)``.  Removing a RELAY
    task removes its pickup (on ``relay.first_drone``), its delivery (on
    ``relay.second_drone``) and the relay transfer itself.  Removing a
    DIRECT task removes both visits from its owner route.
    """
    routes = [list(route) for route in solution.routes]
    relays = list(solution.relays)
    services = dict(solution.task_services)

    to_remove = set(task_ids)

    for task_id in to_remove:
        state = services.get(task_id)
        if state is None:
            # Fall back to raw removal from any route.
            for drone in range(len(routes)):
                routes[drone] = [
                    visit for visit in routes[drone] if abs(visit) != task_id
                ]
            continue
        if (
            state.mode == ServiceMode.RELAY
            and state.relay_event is not None
        ):
            event = state.relay_event
            if event.first_drone < len(routes):
                routes[event.first_drone] = [
                    visit for visit in routes[event.first_drone] if visit != task_id
                ]
            if event.second_drone < len(routes):
                routes[event.second_drone] = [
                    visit
                    for visit in routes[event.second_drone]
                    if visit != -task_id
                ]
            relays = [relay for relay in relays if relay.id != event.id]
        else:
            # DIRECT: remove both visits from the owner route.
            drone = state.primary_owner
            if drone is None or drone < 0 or drone >= len(routes):
                drone = pickup_drone_map(solution.routes).get(task_id)
            if drone is not None and 0 <= drone < len(routes):
                routes[drone] = [
                    visit for visit in routes[drone] if abs(visit) != task_id
                ]
            else:
                for drone_index in range(len(routes)):
                    routes[drone_index] = [
                        visit
                        for visit in routes[drone_index]
                        if abs(visit) != task_id
                    ]
        services.pop(task_id, None)

    # active_stations is a derived view of the relay points actually used.
    used_station_ids = {relay.station_id for relay in relays}
    active_stations = tuple(
        st for st in solution.active_stations if st.id in used_station_ids
    )
    return (
        CooperativeSolution(
            routes=tuple(tuple(route) for route in routes),
            relays=tuple(relays),
            swaps=solution.swaps,
            active_stations=active_stations,
            ownership={
                task_id: state.primary_owner
                for task_id, state in services.items()
            },
            task_services=services,
        ),
        to_remove,
    )


# ---------------------------------------------------------------------------
# Destroy operators
# ---------------------------------------------------------------------------


def _apply_destroy(
    problem: Problem,
    evaluator: RouteEvaluator,
    solution: CooperativeSolution,
    operator: str,
    count: int,
    rng: Random,
    cfg: CooperativeHALNSConfig,
) -> tuple[CooperativeSolution, set[int], dict]:
    """Run one cooperative destroy operator.

    Returns ``(partial_solution, removed_ids, context)`` where ``context``
    may carry ``ownership_moves`` (task -> target drone) for ownership
    destroys, or ``critical_tasks`` for blocker destroys.
    """
    task_ids = [
        task_id
        for task_id in problem.task_ids
        if task_id in solution.task_services
    ]
    count = min(max(1, count), len(task_ids))
    context: dict = {}

    if not task_ids:
        return solution, set(), context

    # Pickup slacks are only needed by the cooperative destroy operators
    # (pickup_risk / blocker / ownership).  The A2 base-layer operators must
    # stay on the exact hot path A2 uses, so we compute slacks lazily.
    slacks: dict | None = None

    def _slacks() -> dict:
        nonlocal slacks
        if slacks is None:
            slacks = _pickup_slacks(problem, solution.routes)
        return slacks

    removed: list[int] = []

    if operator == "random_service":
        removed = rng.sample(task_ids, count)

    elif operator in A2_DESTROY_OPERATORS:
        # Full A2 base-layer destroy palette.  A2's operators select which
        # tasks to remove; the cooperative ``_remove_task_services`` then
        # removes the complete TaskServiceState units (including relay
        # partners).  If the current solution holds relay-split routes that
        # A2's per-route evaluator cannot score, fall back to a safe random
        # removal rather than crashing.
        try:
            _, removed_ids = _a2_destroy_solution(
                problem,
                evaluator,
                solution.routes,
                operator,
                count,
                rng,
                use_deadline_risk=True,
            )
        except (ValueError, RuntimeError, _SearchDeadlineReached):
            removed = rng.sample(task_ids, count)
        else:
            removed = list(removed_ids)
        context["destroy_reason"] = operator

    elif operator == "pickup_risk":
        ranked = _pickup_risk_rank(_slacks(), task_ids)
        removed = _choose_ranked(ranked, count, rng, exponent=2.5)
        context["destroy_reason"] = "pickup_risk"

    elif operator == "blocker":
        # Critical pickup + its upstream blockers.
        sl = _slacks()
        ranked = _pickup_risk_rank(sl, task_ids)
        critical = [
            task_id for task_id in ranked if sl.get(task_id, 0.0) < 15.0
        ]
        if not critical:
            removed = _choose_ranked(ranked, count, rng, exponent=2.5)
        else:
            seed_task = critical[0]
            drone = pickup_drone_map(solution.routes).get(seed_task)
            blockers: list[int] = []
            if drone is not None:
                try:
                    blockers = [
                        blocker
                        for blocker, _ in find_upstream_blockers(
                            problem, evaluator, solution.routes[drone], seed_task
                        )[: cfg.blocker_per_task_limit]
                    ]
                except (ValueError, RuntimeError):
                    blockers = []
            removed = [seed_task] + blockers
            remaining = [
                task_id
                for task_id in ranked
                if task_id not in removed
            ]
            removed += _choose_ranked(remaining, count - len(removed), rng)
            context["critical_tasks"] = [seed_task]
        context["destroy_reason"] = "blocker"

    elif operator == "ownership":
        # Remove balanced DIRECT pairs from two different drones so repair
        # can re-assign them to *other* owners (works in the tight 8x25
        # setting).  Ownership only operates on DIRECT tasks — RELAY tasks
        # are left to relay_structure destroy / dynamic relay.
        direct_tasks = [
            task_id
            for task_id in task_ids
            if solution.task_services[task_id].mode == ServiceMode.DIRECT
        ]
        if not direct_tasks:
            return solution, set(), context
        pickup_map = pickup_drone_map(solution.routes)
        tasks_by_drone: dict[int, list[int]] = {}
        for task_id in direct_tasks:
            tasks_by_drone.setdefault(pickup_map.get(task_id, -1), []).append(
                task_id
            )
        drones = [d for d in tasks_by_drone if d >= 0 and tasks_by_drone[d]]
        old_owners = {
            task_id: pickup_map.get(task_id, -1) for task_id in direct_tasks
        }
        if len(drones) < 2:
            removed = _choose_ranked(
                _pickup_risk_rank(_slacks(), direct_tasks), count, rng
            )
        else:
            a, b = rng.sample(drones, 2)
            ranked_a = _pickup_risk_rank(_slacks(), tasks_by_drone[a])
            ranked_b = _pickup_risk_rank(_slacks(), tasks_by_drone[b])
            pairs = 0
            while len(removed) < count - 1 and pairs < count // 2:
                if not ranked_a or not ranked_b:
                    break
                i = ranked_a.pop(0)
                j = ranked_b.pop(0)
                removed.append(i)
                removed.append(j)
                context.setdefault("ownership_moves", {})[i] = b
                context.setdefault("ownership_moves", {})[j] = a
                pairs += 1
            if len(removed) < count:
                leftover = [
                    task_id
                    for task_id in direct_tasks
                    if task_id not in removed
                ]
                extra = _choose_ranked(
                    _pickup_risk_rank(_slacks(), leftover),
                    count - len(removed),
                    rng,
                )
                for task_id in extra:
                    removed.append(task_id)
        # Every removed task must carry an explicit target drone different
        # from its old owner, so an accepted ownership move always changes
        # the owner (or the repair is honestly rejected).
        for task_id in removed:
            if task_id in context.get("ownership_moves", {}):
                continue
            old = old_owners.get(task_id, -1)
            targets = [
                d for d in drones if d != old and d >= 0
            ]
            if targets:
                context.setdefault("ownership_moves", {})[task_id] = rng.choice(
                    targets
                )
        context["old_owners"] = old_owners
        context["destroy_reason"] = "ownership"

    elif operator == "relay_structure":
        relay_tasks = [
            task_id
            for task_id, state in solution.task_services.items()
            if state.mode == ServiceMode.RELAY
        ]
        if relay_tasks:
            removed = rng.sample(relay_tasks, min(count, len(relay_tasks)))
        else:
            removed = rng.sample(task_ids, count)
        context["destroy_reason"] = "relay_structure"

    else:
        raise ValueError(f"未知 Cooperative destroy 算子 {operator}")

    removed = list(dict.fromkeys(removed))
    partial, removed_set = _remove_task_services(problem, solution, removed)
    return partial, removed_set, context


# ---------------------------------------------------------------------------
# Insertion helpers
# ---------------------------------------------------------------------------


def _direct_insertion_options(
    problem: Problem,
    evaluator: RouteEvaluator,
    solution: CooperativeSolution,
    task_id: int,
    allowed_drones: Sequence[int],
    candidate_limit: int | None,
    option_count: int = 1,
    profile_cache: dict | None = None,
) -> list[tuple[Score, int, Routes]]:
    """Best DIRECT insertion options for *task_id* on complete routes.

    Returns ``[(delta, drone, new_routes), ...]`` sorted by ``(late, dist)``.
    Split (relay/swap) routes are skipped — they cannot be scored by the
    per-route evaluator.
    """
    from .search import route_insertion_options

    options: list[tuple[Score, int, Routes]] = []
    for drone in allowed_drones:
        if drone < 0 or drone >= len(solution.routes):
            continue
        route = solution.routes[drone]
        if not route_is_complete(solution.routes, drone):
            continue
        if sum(visit > 0 for visit in route) >= problem.max_tasks_per_drone:
            continue
        if task_id in route or -task_id in route:
            continue
        try:
            local = route_insertion_options(
                problem,
                evaluator,
                route,
                drone,
                task_id,
                candidate_limit=candidate_limit,
                option_count=option_count,
                profile_cache=profile_cache,
            )
        except (ValueError, RuntimeError):
            continue
        for opt in local:
            new_routes = list(solution.routes)
            new_routes[drone] = opt.route
            options.append((opt.delta, drone, tuple(new_routes)))
    options.sort(key=lambda item: (item[0], item[1], item[2]))
    return options


def _insert_direct(
    problem: Problem,
    evaluator: RouteEvaluator,
    solution: CooperativeSolution,
    task_id: int,
    drone: int,
    cfg: CooperativeHALNSConfig,
    profile_cache: dict | None = None,
) -> CooperativeSolution | None:
    """Insert *task_id* as DIRECT into exactly *drone*'s route."""
    options = _direct_insertion_options(
        problem,
        evaluator,
        solution,
        task_id,
        [drone],
        cfg.repair_candidate_limit,
        option_count=1,
        profile_cache=profile_cache,
    )
    if not options:
        return None
    _, _, new_routes = options[0]
    return _make_solution(
        problem,
        new_routes,
        solution.relays,
        solution.swaps,
        solution.active_stations,
    )


# ---------------------------------------------------------------------------
# Repair operators
# ---------------------------------------------------------------------------


def _repair_direct(
    problem: Problem,
    evaluator: RouteEvaluator,
    partial: CooperativeSolution,
    removed_ids: set[int],
    cfg: CooperativeHALNSConfig,
    rng: Random,
    deadline: float | None,
    moves: Mapping[int, int] | None = None,
    strategy: str = "regret2",
) -> CooperativeSolution | None:
    """DIRECT repair using one of the full A2 repair strategies.

    ``strategy`` is any A2 repair operator (greedy / regret2 / regret3 /
    deadline / slack / cluster_regret / pair_regret / pickup_urgency).

    When ``moves`` is provided (ownership moves), each listed task may only
    be inserted into its fixed target drone — so ownership genuinely changes
    and cannot slide back to the original owner.
    """
    solution = partial
    remaining = set(removed_ids)
    profile_cache: dict = {}
    # When there is no fixed-target constraint and every route is complete
    # (the common case), delegate to the proven A2 repair with the chosen
    # strategy so the Cooperative-HALNS is never weaker than the baseline
    # per iteration.
    if moves is None or not moves:
        if all(
            route_is_complete(solution.routes, d)
            for d in range(len(solution.routes))
        ):
            from .alns import _repair as _alns_repair

            original_route_by_task = {
                visit: drone
                for drone, route in enumerate(solution.routes)
                for visit in route
                if visit > 0
            }
            try:
                repaired_routes = _alns_repair(
                    problem,
                    evaluator,
                    solution.routes,
                    remaining,
                    strategy,
                    cfg.candidate_limit,
                    use_deadline_risk=True,
                    original_route_by_task=original_route_by_task,
                    cluster_bundle_candidate_limit=cfg.cluster_bundle_candidate_limit,
                    cluster_pair_limit=cfg.cluster_pair_limit,
                    pair_candidate_limit=cfg.pair_candidate_limit,
                    deadline=deadline,
                )
            except (ValueError, RuntimeError, _SearchDeadlineReached):
                return None
            return _make_solution(
                problem,
                repaired_routes,
                solution.relays,
                solution.swaps,
                solution.active_stations,
            )

    while remaining:
        if deadline is not None and perf_counter() >= deadline:
            return None
        best_task: int | None = None
        best_option: tuple[Score, int, Routes] | None = None
        best_regret: tuple[float, float] | None = None
        best_urgency = 0.0

        for task_id in sorted(remaining):
            target = moves.get(task_id) if moves else None
            if target is not None:
                allowed = [target]
            else:
                allowed = list(range(problem.drone_count))
            options = _direct_insertion_options(
                problem,
                evaluator,
                solution,
                task_id,
                allowed,
                cfg.repair_candidate_limit,
                option_count=2,
                profile_cache=profile_cache,
            )
            if not options:
                continue
            if len(options) >= 2:
                gap = options[1][0] - options[0][0]
                regret: tuple[float, float] = (
                    float(gap.late_count),
                    gap.distance_km,
                )
            else:
                regret = (float("inf"), 0.0)
            # Urgency: pickup slack estimate (unplaced -> direct completion).
            task = problem.task(task_id)
            slack_est = (
                task.deadline_min
                - problem.direct_completion_min(task_id)
            )
            urgency = max(0.0, -slack_est)
            key = (regret, urgency, -task_id)
            if best_task is None or key > (
                best_regret or (float("-inf"), 0.0),
                best_urgency,
                -best_task,
            ):
                best_task = task_id
                best_option = options[0]
                best_regret = regret
                best_urgency = urgency

        if best_task is None or best_option is None:
            return None  # cannot place every removed task
        _, _, new_routes = best_option
        solution = _make_solution(
            problem,
            new_routes,
            solution.relays,
            solution.swaps,
            solution.active_stations,
        )
        remaining.remove(best_task)
    return solution


def _select_highest_regret_task(
    choices: Sequence[tuple[int, tuple[float, float], Score]],
) -> int:
    """Regret-2 selection: pick the task with the **maximum** regret.

    ``choices`` is ``[(task_id, regret, best_delta), ...]`` where
    ``regret = second_best - best`` (as ``(late_count, distance_km)``).

    Standard Regret-2 places the task whose second-best option is much worse
    than its best *first* (largest regret), with tie-breaks: min best delta,
    then min task id (determinism).
    """
    best_task: int | None = None
    best_key: tuple | None = None
    for task_id, regret, delta in choices:
        key = (
            -regret[0],
            -regret[1],
            delta.late_count,
            delta.distance_km,
            task_id,
        )
        if best_key is None or key < best_key:
            best_key = key
            best_task = task_id
    return best_task  # type: ignore[return-value]


def _repair_ownership_targeted(
    problem: Problem,
    evaluator: RouteEvaluator,
    partial: CooperativeSolution,
    removed_ids: set[int],
    cfg: CooperativeHALNSConfig,
    rng: Random,
    deadline: float | None,
    moves: Mapping[int, int],
    old_owners: Mapping[int, int] | None = None,
) -> tuple[CooperativeSolution | None, dict]:
    """Ownership-targeted repair: every task is forced onto a *different*
    drone than its old owner (balanced pair exchange via destroy + repair).

    Regret-2 ordering: tasks are placed in **descending** regret order
    (``regret = second_best - best``), i.e. the task that suffers most if not
    placed now goes first.

    Returns ``(candidate_or_None, stats)``.
    """
    stats = _empty_op_stats()
    solution = partial
    remaining = set(removed_ids)
    old_owners = old_owners or _old_owner_map(partial, removed_ids)
    profile_cache: dict = {}

    while remaining:
        if deadline is not None and perf_counter() >= deadline:
            return None, stats
        choices: list[tuple[int, tuple[float, float], Score]] = []
        best_delta_by_task: dict[int, tuple[int, Routes]] = {}

        for task_id in sorted(remaining):
            target = moves.get(task_id)
            if target is None:
                # any drone different from the old owner
                old = old_owners.get(task_id, -1)
                candidates = [
                    d
                    for d in range(problem.drone_count)
                    if d != old
                ]
            else:
                candidates = [target]
            options = _direct_insertion_options(
                problem,
                evaluator,
                solution,
                task_id,
                candidates,
                cfg.repair_candidate_limit,
                option_count=2,
                profile_cache=profile_cache,
            )
            stats["candidates"] += len(options)
            if not options:
                continue
            stats["feasible"] += 1
            if len(options) >= 2:
                gap = options[1][0] - options[0][0]
                regret = (float(gap.late_count), gap.distance_km)
            else:
                regret = (float("inf"), 0.0)
            choices.append((task_id, regret, options[0][0]))
            best_delta_by_task[task_id] = (options[0][1], options[0][2])

        if not choices:
            return None, stats
        # MAX regret first (standard Regret-2 direction).
        best_task = _select_highest_regret_task(choices)
        best_target, new_routes = best_delta_by_task[best_task]
        solution = _make_solution(
            problem,
            new_routes,
            solution.relays,
            solution.swaps,
            solution.active_stations,
        )
        remaining.remove(best_task)
    return solution, stats


def _old_owner_map(
    partial: CooperativeSolution, removed_ids: set[int]
) -> dict[int, int]:
    """Recover old owners from the *pre-removal* state is not possible after
    removal, so we derive from the removed services recorded before removal.

    This helper is intentionally simple: callers pass the removed ids and we
    return a map from the partial solution's route pickups (the remaining
    tasks).  For truly removed tasks the caller is expected to supply moves.
    """
    owners: dict[int, int] = {}
    for task_id in removed_ids:
        # The removed task is no longer in routes; old owner is unknown here.
        # It is only meaningful for tasks that still appear (should not
        # happen), so leave unset — the caller provides ``moves`` instead.
        owners[task_id] = -1
    return owners


def _task_is_placed(solution: CooperativeSolution, task_id: int) -> bool:
    return any(
        abs(visit) == task_id
        for route in solution.routes
        for visit in route
    )


def _relay_options_for_task(
    problem: Problem,
    solution: CooperativeSolution,
    task_id: int,
    stations: Sequence[RelayStation],
    cfg: CooperativeHALNSConfig,
    deadline: float | None,
) -> list[tuple[Score, CooperativeSolution]]:
    """Relay (conversion or fresh) insertion options for *task_id*.

    .. note::
        Legacy helper (used by tests).  The formal Cooperative-HALNS uses
        ``_criticality_guided_dynamic_relay_once`` instead.

    - If the task is already placed: DIRECT -> RELAY *conversion* options.
    - Otherwise: fresh multi-drone relay insertion.

    Returns ``[(delta_score, resulting_solution), ...]``.  Every returned
    option is DAG-validated by the relay machinery (``require_all_tasks`` is
    False so mid-repair partial solutions are allowed).  ``active_stations``
    of the returned solutions is pruned to the points actually used.
    """
    from .relay_candidates import relay_insertion_options

    relay_sol = solution.to_relay_solution()
    options: list[tuple[Score, CooperativeSolution]] = []

    def _build(cand_sol, deltas: Score) -> CooperativeSolution:
        station_by_id = {st.id: st for st in stations}
        used = []
        seen: set[int] = set()
        for relay in cand_sol.relays:
            st = station_by_id.get(relay.station_id)
            if st is not None and st.id not in seen:
                used.append(st)
                seen.add(st.id)
        return _make_solution(
            problem, cand_sol.routes, cand_sol.relays, (), tuple(used)
        )

    if _task_is_placed(solution, task_id):
        try:
            candidates = relay_candidates_for_task(
                problem,
                relay_sol,
                task_id,
                stations=stations,
                second_drone_limit=cfg.relay_receiver_limit,
                drop_gap_limit=3,
                pick_gap_limit=3,
                delivery_gap_limit=3,
                max_candidates=cfg.relay_candidate_limit,
                max_evaluations=cfg.relay_max_evaluations,
                deadline=deadline,
                require_all_tasks=False,
            )
        except (ValueError, RuntimeError, _SearchDeadlineReached):
            candidates = ()
        for candidate in candidates:
            try:
                cand_sol = apply_relay_candidate(problem, relay_sol, candidate)
            except (ValueError, RuntimeError):
                continue
            options.append((candidate.delta_score, _build(cand_sol, candidate.delta_score)))
    else:
        try:
            fresh = relay_insertion_options(
                problem,
                relay_sol,
                task_id,
                stations=stations,
                first_drone_limit=cfg.relay_receiver_limit,
                second_drone_limit=cfg.relay_receiver_limit,
                pickup_gap_limit=2,
                drop_gap_limit=2,
                pick_gap_limit=2,
                delivery_gap_limit=2,
                max_candidates=cfg.relay_candidate_limit,
                max_evaluations=cfg.relay_max_evaluations,
                deadline=deadline,
                require_all_tasks=False,
            )
        except (ValueError, RuntimeError, _SearchDeadlineReached):
            fresh = ()
        for option in fresh:
            options.append((option.delta_score, _build(option.solution, option.delta_score)))
    options.sort(key=lambda item: (item[0], item[1].routes))
    return options


def _repair_mixed(
    problem: Problem,
    evaluator: RouteEvaluator,
    partial: CooperativeSolution,
    removed_ids: set[int],
    stations: Sequence[RelayStation],
    cfg: CooperativeHALNSConfig,
    rng: Random,
    deadline: float | None,
) -> tuple[CooperativeSolution | None, dict]:
    """Multi-mode (DIRECT + RELAY + OWNERSHIP) regret repair.

    .. note::
        Legacy helper — NOT part of the formal repair pool.  The formal
        Cooperative-HALNS repairs with the full A2 repair palette
        (greedy / regret2 / regret3 / deadline / slack / pickup_urgency …)
        plus ``ownership_targeted``, and applies relay only through the
        post-repair Criticality-Guided Dynamic Relay neighborhood.

    One repair can simultaneously produce task A -> RELAY, task B -> DIRECT
    and task C -> a different owner — this is the "batch cooperative repair".
    Returns ``(candidate_or_None, stats)``.
    """
    stats = _empty_op_stats()
    solution = partial
    remaining = set(removed_ids)
    profile_cache: dict = {}
    probes_used = 0

    while remaining:
        if deadline is not None and perf_counter() >= deadline:
            return None, stats
        best_task: int | None = None
        best_choice: tuple[Score, CooperativeSolution] | None = None
        best_regret = (float("inf"), 0.0)

        # Rank remaining tasks for relay probing (most rescue-able first).
        # The total number of relay probes per repair call is bounded by
        # ``relay_candidate_limit`` so expensive DAG probing never dominates.
        ranked = _pickup_risk_rank(
            _pickup_slacks(problem, solution.routes),
            sorted(remaining),
        )
        allow_relay = probes_used < cfg.relay_candidate_limit
        probe_set = set(ranked[: cfg.critical_task_limit] if allow_relay else ())

        for task_id in sorted(remaining):
            direct = _direct_insertion_options(
                problem,
                evaluator,
                solution,
                task_id,
                list(range(problem.drone_count)),
                cfg.repair_candidate_limit,
                option_count=2,
                profile_cache=profile_cache,
            )
            merged: list[tuple[Score, CooperativeSolution]] = [
                (delta, _make_solution(problem, routes, solution.relays,
                                       solution.swaps,
                                       solution.active_stations))
                for delta, _, routes in direct
            ]
            if task_id in probe_set and stations:
                relay_opts = _relay_options_for_task(
                    problem,
                    solution,
                    task_id,
                    stations,
                    cfg,
                    deadline,
                )
                probes_used += 1
                stats["candidates"] += len(relay_opts)
                stats["feasible"] += len(relay_opts)
                merged.extend(relay_opts)
            merged.sort(key=lambda item: (item[0], item[1].routes))
            if not merged:
                continue
            if len(merged) >= 2:
                gap = merged[1][0] - merged[0][0]
                regret = (float(gap.late_count), gap.distance_km)
            else:
                regret = (float("inf"), 0.0)
            if best_task is None or (regret, -task_id) > (
                best_regret,
                -best_task,
            ):
                best_task = task_id
                best_choice = merged[0]
                best_regret = regret

        if best_task is None or best_choice is None:
            return None, stats
        solution = best_choice[1]
        remaining.remove(best_task)
    return solution, stats


# ---------------------------------------------------------------------------
# Criticality-Guided Dynamic Relay (post-repair neighborhood)
# ---------------------------------------------------------------------------
# One unified relay neighborhood with two candidate *guidance* sources
# (DELIVERY_RISK and BLOCKER_RELEASE).  Both produce the same physical relay
# (donor: P_j -> h ; receiver: h -> D_j), are merged + deduplicated on
# (task_id, donor, receiver, relay_point_id), cheaply screened, then only the
# top candidates are exactly DAG-evaluated.  At most ``relay_additions_per_call``
# relay transfers are added per call (default 1).


def _receiver_shortlist(
    problem: Problem,
    solution: CooperativeSolution,
    donor: int,
    limit: int,
) -> list[int]:
    """Rank receivers by free capacity-window quality (most free first)."""
    receivers = [d for d in range(problem.drone_count) if d != donor]

    def key(d: int) -> tuple:
        route = solution.routes[d]
        window = _receiver_window_quality(route, problem.capacity)
        return (-window, len(route), d)

    receivers.sort(key=key)
    return receivers[:limit]


def _estimate_delivery_gain(
    problem: Problem,
    pickup_times: Mapping[int, float],
    task_id: int,
    station: RelayStation,
    current_delivery_time: float,
) -> float:
    """Cheap lower-bound of how many minutes earlier the delivery can be if
    ``task_id`` is relayed through *station*.

    ``new_delivery_lb = max(earliest_drop, receiver_depot_arrival) + h -> D_i``
    is an optimistic bound: if even that is not earlier than the current
    delivery time, the candidate cannot rescue the delivery (screen out).
    """
    speed = problem.speed_km_per_min
    task = problem.task(task_id)
    h = station.point
    t_pickup = pickup_times.get(task_id)
    if t_pickup is None:
        return 0.0
    earliest_drop = t_pickup + task.pickup.distance_to(h) / speed
    receiver_depot_arrival = problem.distance(None, station.source_visit) / speed
    earliest_pick = max(earliest_drop, receiver_depot_arrival)
    new_delivery_lb = earliest_pick + h.distance_to(task.delivery) / speed
    return max(0.0, current_delivery_time - new_delivery_lb)


def _delivery_risk_candidates(
    problem: Problem,
    solution: CooperativeSolution,
    cfg: CooperativeHALNSConfig,
    relay_pool: DynamicRelayPointPool,
    delivery_times: Mapping[int, float],
    pickup_times: Mapping[int, float],
) -> tuple[list[RelayCandidate], int]:
    """Delivery-risk relay candidates for DIRECT tasks whose own delivery is
    late / near-late (``delivery_slack < delivery_risk_slack_limit``).

    Returns ``(candidates, detected_count)``.
    """
    candidates: list[RelayCandidate] = []
    already_relayed = {relay.task_id for relay in solution.relays}
    risk: list[tuple[int, float]] = []
    for tid, state in solution.task_services.items():
        if state.mode != ServiceMode.DIRECT:
            continue
        if tid in already_relayed:
            continue
        t_delivery = delivery_times.get(tid)
        if t_delivery is None:
            continue
        slack = problem.task(tid).deadline_min - t_delivery
        if slack < cfg.delivery_risk_slack_limit:
            risk.append((tid, slack))
    # Already-late first (prevention of new lateness second).
    risk.sort(key=lambda item: (0 if item[1] < 0 else 1, item[1], item[0]))
    detected = len(risk)

    for tid, _slack in risk[: cfg.critical_task_limit]:
        state = solution.task_services[tid]
        donor = state.primary_owner
        if donor < 0:
            continue
        current_delivery = delivery_times.get(tid, 0.0)
        task = problem.task(tid)
        speed = problem.speed_km_per_min
        t_pickup = pickup_times.get(tid, 0.0)
        # Geometric prefilter: a delivery-risk rescue needs h with
        # (P_i -> h + h -> D_i)/v < t(D_i) - t(P_i); prune everything else
        # before the expensive contextual scoring.
        leg_bound = speed * max(0.0, current_delivery - t_pickup) + 1e-6
        for receiver in _receiver_shortlist(
            problem, solution, donor, cfg.relay_receiver_limit
        ):
            for ctx_score, station in relay_pool.shortlist_for_scored(
                solution.routes, tid, donor, receiver,
                cfg.relay_point_candidate_limit,
                prefilter=lambda st, t=task, lb=leg_bound: (
                    t.pickup.distance_to(st.point)
                    + st.point.distance_to(t.delivery)
                ) <= lb,
            ):
                gain = _estimate_delivery_gain(
                    problem, pickup_times, tid, station, current_delivery
                )
                if gain <= 1e-9:
                    continue
                candidates.append(
                    RelayCandidate(
                        task_id=tid,
                        donor=donor,
                        receiver=receiver,
                        relay_point_id=station.id,
                        triggers=frozenset({RelayTrigger.DELIVERY_RISK}),
                        estimated_delivery_gain_min=gain,
                        estimated_distance_delta=ctx_score,
                    )
                )
    return candidates, detected


def _blocker_release_candidates(
    problem: Problem,
    solution: CooperativeSolution,
    cfg: CooperativeHALNSConfig,
    evaluator: RouteEvaluator,
    relay_pool: DynamicRelayPointPool,
    pickup_slacks_map: Mapping[int, float],
) -> tuple[list[RelayCandidate], int]:
    """Blocker-release relay candidates.

    For a critical pickup ``i`` (pickup slack below ``near_critical_slack_limit``)
    whose donor is held up by blocker ``j`` (``P_j ... D_j ... P_i``), relaying
    ``j`` to another drone lets the donor reach ``P_i`` earlier.  The estimated
    pickup advance is cheap guidance only.

    Returns ``(candidates, blockers_detected)``.
    """
    candidates: list[RelayCandidate] = []
    already_relayed = {relay.task_id for relay in solution.relays}
    critical = sorted(
        (
            (task_id, slack)
            for task_id, slack in pickup_slacks_map.items()
            if slack < cfg.near_critical_slack_limit
        ),
        key=lambda item: (abs(item[1]), item[0]),
    )[: cfg.critical_task_limit]
    blockers_detected = 0

    for crit_tid, _slack in critical:
        donor = pickup_drone_map(solution.routes).get(crit_tid)
        if donor is None:
            continue
        try:
            blockers = find_upstream_blockers(
                problem, evaluator, solution.routes[donor], crit_tid
            )
        except (ValueError, RuntimeError):
            continue
        blockers_detected += len(blockers)
        for blocker_tid, _holding in blockers[: cfg.blocker_per_task_limit]:
            if blocker_tid in already_relayed:
                continue
            state = solution.task_services.get(blocker_tid)
            if state is None or state.mode != ServiceMode.DIRECT:
                continue
            if state.primary_owner != donor:
                continue
            b_task = problem.task(blocker_tid)
            crit_task = problem.task(crit_tid)
            # Geometric prefilter: a blocker-release shortcut needs
            # (P_j -> h + h -> P_i)/v < (P_j -> D_j + D_j -> P_i)/v.
            path_bound = (
                b_task.pickup.distance_to(b_task.delivery)
                + b_task.delivery.distance_to(crit_task.pickup)
            ) + 1e-6
            for receiver in _receiver_shortlist(
                problem, solution, donor, cfg.relay_receiver_limit
            ):
                for ctx_score, station in relay_pool.shortlist_for_scored(
                    solution.routes, blocker_tid, donor, receiver,
                    cfg.relay_point_candidate_limit,
                    prefilter=lambda st, b=b_task, c=crit_task, pb=path_bound: (
                        b.pickup.distance_to(st.point)
                        + st.point.distance_to(c.pickup)
                    ) <= pb,
                ):
                    gain = estimate_blocker_release_gain(
                        problem, solution.routes, donor,
                        blocker_tid, crit_tid, station,
                    )
                    if gain <= 1e-9:
                        continue
                    candidates.append(
                        RelayCandidate(
                            task_id=blocker_tid,
                            donor=donor,
                            receiver=receiver,
                            relay_point_id=station.id,
                            triggers=frozenset({RelayTrigger.BLOCKER_RELEASE}),
                            critical_task_id=crit_tid,
                            estimated_pickup_gain_min=gain,
                            estimated_distance_delta=ctx_score,
                        )
                    )
    return candidates, blockers_detected


def _merge_relay_candidates(
    candidates: Sequence[RelayCandidate],
) -> list[RelayCandidate]:
    """Deduplicate by ``(task_id, donor, receiver, relay_point_id)``, merging
    triggers so a candidate found by both guidances is DAG-evaluated once."""
    merged: dict[tuple[int, int, int, int], RelayCandidate] = {}
    for cand in candidates:
        key = cand.dedup_key
        if key in merged:
            merged[key] = merged[key].merged_with(cand)
        else:
            merged[key] = cand
    return list(merged.values())


def _criticality_guided_dynamic_relay_once(
    problem: Problem,
    solution: CooperativeSolution,
    cfg: CooperativeHALNSConfig,
    rng: Random,
    deadline: float | None,
    relay_pool: DynamicRelayPointPool,
) -> tuple[CooperativeSolution, dict]:
    """One Criticality-Guided Dynamic Relay neighborhood call.

    Pipeline: delivery-risk + blocker-release candidates -> merge + dedup ->
    cheap screening -> exact DAG evaluation of the top candidates only.
    At most ``cfg.relay_additions_per_call`` (default 1) RelayTransfer is
    added.  The exact decision ranks strictly by ``(late_count, distance_km)``;
    estimated delivery / pickup gains are used only as tie-break when the
    formal scores are equal.

    Returns ``(new_solution, stats)``.
    """
    stats = {
        "delivery_risk_tasks_detected": 0,
        "blockers_detected": 0,
        "candidates_before_dedup": 0,
        "candidates_after_dedup": 0,
        "screened_out": 0,
        "exact_evaluations": 0,
        "feasible": 0,
        "applied": False,
        "applied_triggers": frozenset(),
        "applied_pickup_advance": 0.0,
        "applied_delivery_gain": 0.0,
    }
    if solution.swaps:
        return solution, stats
    if not (cfg.enable_delivery_risk_relay or cfg.enable_blocker_relay):
        return solution, stats
    if cfg.max_relays_per_solution is not None and (
        solution.relay_count >= cfg.max_relays_per_solution
    ):
        return solution, stats

    validation = validate_cooperative_solution(problem, solution)
    delivery_times = dict(validation.delivery_times_min)
    pickup_times = dict(validation.pickup_times_min)
    evaluator = RouteEvaluator(problem)

    candidates: list[RelayCandidate] = []
    if cfg.enable_delivery_risk_relay:
        dr, detected = _delivery_risk_candidates(
            problem, solution, cfg, relay_pool, delivery_times, pickup_times
        )
        candidates.extend(dr)
        stats["delivery_risk_tasks_detected"] += detected
    if cfg.enable_blocker_relay:
        br, blockers = _blocker_release_candidates(
            problem, solution, cfg, evaluator, relay_pool,
            _pickup_slacks(problem, solution.routes),
        )
        candidates.extend(br)
        stats["blockers_detected"] += blockers
    stats["candidates_before_dedup"] = len(candidates)
    if not candidates:
        return solution, stats

    merged = _merge_relay_candidates(candidates)
    stats["candidates_after_dedup"] = len(merged)

    screened: list[RelayCandidate] = []
    for cand in merged:
        if (
            cand.estimated_delivery_gain_min > 1e-9
            or cand.estimated_pickup_gain_min > 1e-9
        ):
            screened.append(cand)
        else:
            stats["screened_out"] += 1
    if not screened:
        return solution, stats

    # Order candidates for exact evaluation: contextual relay-point score
    # first (it accounts for the receiver route), estimated rescue gain second.
    screened.sort(
        key=lambda c: (
            c.estimated_distance_delta,
            -(
                c.estimated_delivery_gain_min
                + c.estimated_pickup_gain_min
            ),
        )
    )
    top = screened[: cfg.relay_candidate_limit]

    # DAG station set: points already used + the candidate point.
    used_station_ids = {relay.station_id for relay in solution.relays}
    base_stations = [
        relay_pool.station_for_id(sid) for sid in sorted(used_station_ids)
    ]
    base_stations = [st for st in base_stations if st is not None]

    best_solution = solution
    best_score = validation.score
    best_advance = 0.0
    best_delivery_gain = 0.0
    best_triggers: frozenset[RelayTrigger] = frozenset()
    exact_evaluations = 0
    feasible = 0
    eval_counter: list = []

    for cand in top:
        if deadline is not None and perf_counter() >= deadline:
            break
        if exact_evaluations >= cfg.relay_max_evaluations:
            break
        station = relay_pool.station_for_id(cand.relay_point_id)
        if station is None:
            continue
        eval_stations = list(base_stations)
        if station.id not in used_station_ids:
            eval_stations.append(station)
        relay_sol = solution.to_relay_solution()
        eval_counter.clear()
        remaining_budget = max(1, cfg.relay_max_evaluations - exact_evaluations)
        try:
            dag_candidates = relay_candidates_for_task(
                problem,
                relay_sol,
                cand.task_id,
                stations=eval_stations,
                second_drone_limit=1,
                drop_gap_limit=3,
                pick_gap_limit=3,
                delivery_gap_limit=3,
                max_candidates=4,
                max_evaluations=min(6, remaining_budget),
                deadline=deadline,
                require_all_tasks=True,
                fixed_second=cand.receiver,
                fixed_station_id=station.id,
                eval_counter=eval_counter,
            )
        except (ValueError, RuntimeError, _SearchDeadlineReached):
            continue
        exact_evaluations += len(eval_counter) or 1
        if not dag_candidates:
            continue
        # relay_candidates_for_task returns candidates sorted by delta_score
        # (late_count, distance_km); take the best.
        dag_cand = dag_candidates[0]
        feasible += 1
        try:
            applied = apply_relay_candidate(problem, relay_sol, dag_cand)
        except (ValueError, RuntimeError):
            continue
        new_active = list(base_stations)
        if station.id not in used_station_ids:
            new_active.append(station)
        new_sol = _make_solution(
            problem, applied.routes, applied.relays, (), new_active
        )
        new_val = validate_cooperative_solution(problem, new_sol)
        if not new_val.valid:
            continue

        advance = 0.0
        if cand.critical_task_id is not None:
            old_pt = pickup_times.get(cand.critical_task_id)
            new_pt = new_val.pickup_times_min.get(cand.critical_task_id)
            if old_pt is not None and new_pt is not None:
                advance = max(0.0, old_pt - new_pt)
        delivery_gain = 0.0
        old_dt = delivery_times.get(cand.task_id)
        new_dt = new_val.delivery_times_min.get(cand.task_id)
        if old_dt is not None and new_dt is not None:
            delivery_gain = max(0.0, old_dt - new_dt)

        # STRICT formal ranking (late_count, distance_km).  Guidance gains
        # are only a tie-break when the formal score is exactly equal — never
        # a licence to accept a longer distance for the same late_count.
        formal = (new_val.score.late_count, new_val.score.distance_km)
        best_formal = (best_score.late_count, best_score.distance_km)
        if formal < best_formal or (
            abs(formal[0] - best_formal[0]) < 1e-9
            and abs(formal[1] - best_formal[1]) < 1e-9
            and (delivery_gain + advance)
            > (best_delivery_gain + best_advance) + 1e-9
        ):
            best_solution = new_sol
            best_score = new_val.score
            best_advance = advance
            best_delivery_gain = delivery_gain
            best_triggers = cand.triggers

    stats["exact_evaluations"] = exact_evaluations
    stats["feasible"] = feasible
    if best_solution is not solution:
        stats["applied"] = True
        stats["applied_triggers"] = best_triggers
        stats["applied_pickup_advance"] = best_advance
        stats["applied_delivery_gain"] = best_delivery_gain
    return best_solution, stats


# ---------------------------------------------------------------------------
# Dynamic operator pools
# ---------------------------------------------------------------------------


def build_destroy_pool(cfg: CooperativeHALNSConfig) -> tuple[str, ...]:
    """Destroy operators actually available under ``cfg``.

    The full A2 base-layer destroy palette is guaranteed (mirroring the
    ``ALNSConfig`` flags), plus the cooperative destroy operators:

    - ``pickup_risk`` / ``blocker`` — pickup-first cooperative destroys;
    - ``relay_structure`` — tears existing RELAYs down so the base layer can
      re-decide DIRECT / ownership / relay;
    - ``ownership`` — only in the pool when ``enable_ownership=True``.

    ``ownership`` is only in the pool when ``enable_ownership=True``, so an
    ownership destroy can never be drawn by the roulette otherwise (this is
    what makes the ``enable_ownership`` ablation honest).
    """
    operators = [
        name
        for name in A2_DESTROY_OPERATORS
        if (
            name != "route_clear"
            if cfg.enable_assignment_destroy
            else name != "assignment_destroy"
        )
        and (cfg.enable_hypergraph_destroy or name != "hypergraph_destroy")
    ]
    operators += ["pickup_risk", "blocker", "relay_structure"]
    if cfg.enable_ownership:
        operators.append("ownership")
    return tuple(operators)


def build_repair_pool(cfg: CooperativeHALNSConfig) -> tuple[str, ...]:
    """Repair operators actually available under ``cfg``.

    The full A2 base-layer repair palette is guaranteed (mirroring the
    ``ALNSConfig`` flags), plus ``ownership_targeted`` when ownership is
    enabled.  Relay never enters the repair roulette — it runs only as the
    post-repair Criticality-Guided Dynamic Relay neighborhood.
    """
    operators = [
        name
        for name in A2_REPAIR_OPERATORS
        if (cfg.enable_cluster_repair or name != "cluster_regret")
        and (cfg.enable_pair_repair or name != "pair_regret")
    ]
    if cfg.enable_ownership:
        operators.append("ownership_targeted")
    return tuple(operators)


def _a2_destroy_pool(cfg: CooperativeHALNSConfig) -> tuple[str, ...]:
    """The *exact* A2 base-layer destroy pool (same filter as ALNSConfig).

    This is the pool used by the main Cooperative-HALNS roulette so the
    search trajectory is bit-for-bit identical to ``solve_alns_core``; the
    cooperative mechanisms run as scheduled neighborhoods instead of
    roulette operators.
    """
    return tuple(
        name
        for name in A2_DESTROY_OPERATORS
        if (
            name != "route_clear"
            if cfg.enable_assignment_destroy
            else name != "assignment_destroy"
        )
        and (cfg.enable_hypergraph_destroy or name != "hypergraph_destroy")
    )


def _a2_repair_pool(cfg: CooperativeHALNSConfig) -> tuple[str, ...]:
    """The *exact* A2 base-layer repair pool (same filter as ALNSConfig)."""
    return tuple(
        name
        for name in A2_REPAIR_OPERATORS
        if (cfg.enable_cluster_repair or name != "cluster_regret")
        and (cfg.enable_pair_repair or name != "pair_regret")
    )


def _score_of_solution(
    problem: Problem,
    evaluator: RouteEvaluator,
    solution: CooperativeSolution,
) -> Score:
    """Cheap score for a solution: full validation iff it carries relays."""
    if solution.relays:
        val = validate_cooperative_solution(problem, solution)
        return val.score
    return routes_score(evaluator, solution.routes)


def _ownership_neighborhood_once(
    problem: Problem,
    evaluator: RouteEvaluator,
    solution: CooperativeSolution,
    cfg: CooperativeHALNSConfig,
    rng: Random,
    deadline: float | None,
) -> tuple[CooperativeSolution, dict]:
    """Balanced Ownership neighborhood on DIRECT tasks (strict-only).

    Chooses the drone pairs with the largest pickup-load imbalance, removes
    one balanced DIRECT pair from each, and re-inserts each task onto the
    *other* drone (cross-ownership, max-regret-first).  Only a strict formal
    improvement of the input is returned — the returned solution is never
    worse than ``solution`` under ``(late_count, distance_km)``.
    """
    stats = _empty_op_stats()
    direct_tasks = [
        task_id
        for task_id, state in solution.task_services.items()
        if state.mode == ServiceMode.DIRECT
    ]
    if len(direct_tasks) < 2:
        return solution, stats
    pickup_map = pickup_drone_map(solution.routes)
    tasks_by_drone: dict[int, list[int]] = {}
    for task_id in direct_tasks:
        drone = pickup_map.get(task_id, -1)
        if drone >= 0:
            tasks_by_drone.setdefault(drone, []).append(task_id)
    drones = sorted(tasks_by_drone)
    if len(drones) < 2:
        return solution, stats

    # Pair the drones by pickup-load imbalance, most imbalanced first.
    pairs: list[tuple[int, int]] = []
    for i in range(len(drones)):
        for j in range(i + 1, len(drones)):
            pairs.append((drones[i], drones[j]))
    pairs.sort(
        key=lambda p: -abs(
            len(tasks_by_drone[p[0]]) - len(tasks_by_drone[p[1]])
        )
    )
    trials = max(1, cfg.ownership_pair_trials)
    slacks = _pickup_slacks(problem, solution.routes)

    best_solution = solution
    best_score = _score_of_solution(problem, evaluator, solution)
    for pair in pairs[:trials]:
        if deadline is not None and perf_counter() >= deadline:
            break
        a, b = pair
        ranked_a = _pickup_risk_rank(slacks, tasks_by_drone[a])
        ranked_b = _pickup_risk_rank(slacks, tasks_by_drone[b])
        # Balanced cross-exchange: one task from each drone, swapped owners.
        if not ranked_a or not ranked_b:
            continue
        # Prefer exchanging the most imbalanced pickup profiles, but also
        # allow a second candidate from the heavier drone.
        if len(tasks_by_drone[a]) > len(tasks_by_drone[b]) and len(ranked_a) > 1:
            ta = ranked_a.pop(0)
            tb = ranked_b[0]
        elif len(tasks_by_drone[b]) > len(tasks_by_drone[a]) and len(ranked_b) > 1:
            tb = ranked_b.pop(0)
            ta = ranked_a[0]
        else:
            ta = ranked_a[0]
            tb = ranked_b[0]
        moves = {ta: b, tb: a}
        old_owners = {ta: a, tb: b}
        partial, removed = _remove_task_services(
            problem, best_solution, [ta, tb]
        )
        repaired, rstats = _repair_ownership_targeted(
            problem, evaluator, partial, removed, cfg, rng, deadline,
            moves, old_owners=old_owners,
        )
        stats["candidates"] += rstats["candidates"]
        stats["feasible"] += rstats["feasible"]
        if repaired is None:
            continue
        val = validate_cooperative_solution(problem, repaired)
        if not val.valid:
            continue
        if val.score < best_score:
            best_solution = repaired
            best_score = val.score

    if best_solution is not solution:
        stats["accepted"] = 1
        stats["improving"] = 1
        stats["best_improving"] = 1
    return best_solution, stats


_DESTROY_BASE_WEIGHTS = {
    # A2 base-layer destroys
    "random": 1.0,
    "worst_distance": 1.0,
    "worst_lex": 1.0,
    "spatial_related": 1.0,
    "deadline_related": 1.0,
    "late_critical": 1.5,
    "route_segment": 1.0,
    "capacity_conflict": 1.0,
    "assignment_destroy": 1.0,
    "route_clear": 1.0,
    "hypergraph_destroy": 0.8,
    # cooperative destroys
    "pickup_risk": 1.5,
    "blocker": 1.2,
    "relay_structure": 0.6,
    "ownership": 1.0,
}
_REPAIR_BASE_WEIGHTS = {
    # A2 base-layer repairs
    "greedy": 1.0,
    "regret2": 2.0,
    "regret3": 1.5,
    "deadline": 1.2,
    "slack": 1.2,
    "cluster_regret": 1.0,
    "pair_regret": 1.0,
    "pickup_urgency": 1.0,
    # cooperative repair
    "ownership_targeted": 1.0,
}


def _empty_relay_aggregate() -> dict:
    return {
        "relay_calls": 0,
        "delivery_risk_tasks_detected": 0,
        "blockers_detected": 0,
        "relay_candidates_before_dedup": 0,
        "relay_candidates_after_dedup": 0,
        "relay_candidates_screened_out": 0,
        "relay_exact_evaluations": 0,
        "relay_feasible": 0,
        "relay_accepted": 0,
        "delivery_risk_relays_accepted": 0,
        "blocker_relays_accepted": 0,
        "dual_trigger_relays_accepted": 0,
        "late_tasks_rescued": 0,
        "critical_pickups_advanced": 0,
    }


# ---------------------------------------------------------------------------
# Main HALNS loop
# ---------------------------------------------------------------------------


def solve_cooperative_halns(
    problem: Problem,
    *,
    config: CooperativeHALNSConfig | None = None,
    initial_solution: CooperativeSolution | None = None,
) -> CooperativeHALNSResult:
    """Run the Pickup-Aware Cooperative HALNS and return its best state.

    Per-iteration pipeline::

        Destroy (adaptive roulette over the *exact* A2 destroy pool)
            ↓
        Direct Repair (adaptive roulette over the *exact* A2 repair pool)
            ↓
        A2 ejection neighborhood (fixed ejection_interval)
            ↓
        Criticality-Guided Dynamic Relay (fixed relay_interval; strict-only)
            ↓
        Balanced Ownership neighborhood (fixed ownership_interval; strict-only)
            ↓
        HALNS / SA Acceptance

    The main search trajectory is bit-for-bit identical to ``solve_alns_core``
    (same operator pools, uniform weights, same rng stream) — the cooperative
    neighborhoods use a separate rng and accept only strict formal
    improvements, so the Cooperative-HALNS can never be worse than the A2
    baseline and can only add value on top.

    The returned solution is never worse than the initial solution under the
    strict ``(late_count, distance_km)`` order.  The formal path never
    produces a SWAP and never uses a pre-activated station shortlist.
    """
    cfg = config or CooperativeHALNSConfig()
    started = perf_counter()
    deadline = (
        None
        if cfg.time_limit_seconds is None
        else started + cfg.time_limit_seconds
    )
    rng = Random(cfg.seed)
    evaluator = RouteEvaluator(problem)
    relay_pool = DynamicRelayPointPool(problem)

    # ---- initial solution ----
    if initial_solution is None:
        initial_solution = _build_initial_solution(problem, cfg)
    initial_solution = _make_solution(
        problem,
        initial_solution.routes,
        initial_solution.relays,
        initial_solution.swaps,
        initial_solution.active_stations,
    )
    initial_solution = _prune_active_stations(initial_solution, relay_pool)
    initial_validation = validate_cooperative_solution(
        problem, initial_solution
    )
    if not initial_validation.valid:
        raise ValueError(
            f"Cooperative-HALNS 初始解非法: {initial_validation.violations}"
        )

    best = current = initial_solution
    best_score = current_score = initial_validation.score
    initial_score = initial_validation.score
    time_to_best = 0.0

    # ---- main trajectory is bit-for-bit A2 ----
    # The roulette draws only the *exact* A2 base-layer operators with uniform
    # weights (same as solve_alns_core).  The cooperative mechanisms
    # (Balanced Ownership + Criticality-Guided Dynamic Relay) run as
    # scheduled strict-improvement neighborhoods on ``best`` with a *separate*
    # rng, so the main search trajectory is identical to a standalone A2 run
    # and can only be improved, never degraded.
    coop_rng = Random(cfg.seed + 0x5EED)
    destroy_operators = _a2_destroy_pool(cfg)
    repair_operators = _a2_repair_pool(cfg)
    destroy_weights = {name: 1.0 for name in destroy_operators}
    repair_weights = {name: 1.0 for name in repair_operators}
    segment_uses: dict[str, int] = {}
    segment_rewards: dict[str, float] = {}
    for key in (
        *[f"destroy:{name}" for name in destroy_operators],
        *[f"repair:{name}" for name in repair_operators],
    ):
        segment_uses[key] = 0
        segment_rewards[key] = 0.0

    # mechanism statistics (swap / blocker_relay / dynamic_station keys kept
    # empty for the legacy compat wrapper).
    mechanism_stats: dict[str, dict] = {
        name: _empty_op_stats()
        for name in ("ownership", "relay", "blocker_relay", "swap",
                     "dynamic_station")
    }
    relay_agg = _empty_relay_aggregate()
    ownership_accepted = 0
    temperature = cfg.initial_temperature
    completed_iterations = 0
    accepted_solutions = 0

    def _reward(improved_best: bool, improved_current: bool,
                accepted: bool) -> float:
        if improved_best:
            return 8.0
        if improved_current:
            return 4.0
        if accepted:
            return 1.0
        return 0.0

    for iteration in range(1, cfg.max_iterations + 1):
        if deadline is not None and perf_counter() >= deadline:
            break

        destroy_name = _roulette(destroy_weights, rng)
        repair_name = _roulette(repair_weights, rng)

        lower = max(2, round(len(problem.tasks) * cfg.min_destroy_fraction))
        upper = max(lower, round(len(problem.tasks) * cfg.max_destroy_fraction))
        remove_count = rng.randint(lower, upper)

        # ---- destroy ----
        partial, removed, destroy_context = _apply_destroy(
            problem, evaluator, current, destroy_name, remove_count, rng, cfg
        )
        if not removed:
            continue

        # ---- repair: exactly one of the A2 repair strategies ----
        # ``ownership_targeted`` is no longer a roulette operator: Balanced
        # Ownership runs as a scheduled neighborhood below.
        candidate = _repair_direct(
            problem, evaluator, partial, removed, cfg, rng, deadline,
            strategy=repair_name,
        )
        if candidate is None:
            # rejected — still record operator usage with zero reward
            for key in (f"destroy:{destroy_name}", f"repair:{repair_name}"):
                segment_uses[key] += 1
            continue

        # ---- A2 base-layer ejection neighborhood (mirror solve_alns_core) ----
        # ``solve_alns_core`` runs with ``enable_ejection=False``, so the
        # Cooperative-HALNS main loop must also disable ejection to stay
        # bit-for-bit on the A2 baseline trajectory.
        if (
            cfg.enable_ejection
            and cfg.ejection_interval > 0
            and iteration % cfg.ejection_interval == 0
            and not candidate.relays
        ):
            try:
                ejected_routes = _a2_ejection_improve(
                    problem,
                    evaluator,
                    candidate.routes,
                    cfg.candidate_limit,
                    rng,
                    cfg.ejection_trials,
                    deadline,
                )
            except (ValueError, RuntimeError, _SearchDeadlineReached):
                pass
            else:
                candidate = _make_solution(
                    problem,
                    ejected_routes,
                    candidate.relays,
                    candidate.swaps,
                    candidate.active_stations,
                )

        # ---- Criticality-Guided Dynamic Relay (post-repair neighborhood) ----
        # Runs with ``coop_rng`` so the main A2 rng stream stays untouched.
        relay_applied = False
        relay_triggers: frozenset[RelayTrigger] = frozenset()
        relay_pickup_advance = 0.0
        pre_relay_late_tasks: set[int] = set()
        if cfg.enable_relay and iteration % cfg.relay_interval == 0:
            pre_val = validate_cooperative_solution(problem, candidate)
            pre_relay_late_tasks = {
                tid
                for tid, t in pre_val.delivery_times_min.items()
                if problem.task(tid).deadline_min - t < 0
            }
            candidate, relay_stats = _criticality_guided_dynamic_relay_once(
                problem, candidate, cfg, coop_rng, deadline, relay_pool
            )
            relay_agg["relay_calls"] += 1
            relay_agg["delivery_risk_tasks_detected"] += relay_stats.get(
                "delivery_risk_tasks_detected", 0
            )
            relay_agg["blockers_detected"] += relay_stats.get(
                "blockers_detected", 0
            )
            relay_agg["relay_candidates_before_dedup"] += relay_stats.get(
                "candidates_before_dedup", 0
            )
            relay_agg["relay_candidates_after_dedup"] += relay_stats.get(
                "candidates_after_dedup", 0
            )
            relay_agg["relay_candidates_screened_out"] += relay_stats.get(
                "screened_out", 0
            )
            relay_agg["relay_exact_evaluations"] += relay_stats.get(
                "exact_evaluations", 0
            )
            relay_agg["relay_feasible"] += relay_stats.get("feasible", 0)
            relay_applied = relay_stats.get("applied", False)
            relay_triggers = relay_stats.get("applied_triggers", frozenset())
            relay_pickup_advance = relay_stats.get("applied_pickup_advance", 0.0)

        # ---- Balanced Ownership neighborhood (strict-improvement) ----
        # Cross-drone DIRECT task exchange on the candidate; only a strict
        # formal improvement is kept, so the A2 trajectory can never regress.
        ownership_applied = False
        if (
            cfg.enable_ownership
            and cfg.ownership_interval > 0
            and iteration % cfg.ownership_interval == 0
        ):
            candidate, ownership_stats = _ownership_neighborhood_once(
                problem, evaluator, candidate, cfg, coop_rng, deadline
            )
            mechanism_stats["ownership"]["calls"] += 1
            mechanism_stats["ownership"]["candidates"] += ownership_stats.get(
                "candidates", 0
            )
            mechanism_stats["ownership"]["feasible"] += ownership_stats.get(
                "feasible", 0
            )
            if ownership_stats.get("accepted", 0):
                ownership_applied = True

        # ---- scoring (cheap when relay-free) ----
        # The relay / ownership neighborhoods validate their own candidates,
        # and the A2 repair returns complete routes, so the hot path only
        # needs the cheap per-route score unless relays are present.
        if candidate.relays:
            candidate_validation = validate_cooperative_solution(
                problem, candidate
            )
            if not candidate_validation.valid:
                for key in (f"destroy:{destroy_name}", f"repair:{repair_name}"):
                    segment_uses[key] += 1
                continue
            candidate_score = candidate_validation.score
        else:
            candidate_validation = None
            candidate_score = routes_score(evaluator, candidate.routes)

        # ---- acceptance ----
        improved_current = candidate_score < current_score
        improved_best = candidate_score < best_score
        accepted = _accept_worse(
            candidate_score, current_score, problem, temperature, rng
        )
        if accepted:
            current = candidate
            current_score = candidate_score
            accepted_solutions += 1
        if improved_best:
            best = candidate
            best_score = candidate_score
            time_to_best = perf_counter() - started
        reward = _reward(improved_best, improved_current, accepted)

        # ---- mechanism attribution ----
        if accepted:
            # Ownership attribution: the Balanced Ownership neighborhood
            # applied a strict formal improvement to the candidate, which
            # was then accepted into the search.
            if ownership_applied:
                mechanism_stats["ownership"]["accepted"] += 1
                ownership_accepted += 1
                if reward >= 4.0:
                    mechanism_stats["ownership"]["improving"] += 1
                if reward >= 8.0:
                    mechanism_stats["ownership"]["best_improving"] += 1
            if relay_applied:
                relay_agg["relay_accepted"] += 1
                mechanism_stats["relay"]["accepted"] += 1
                if reward >= 4.0:
                    mechanism_stats["relay"]["improving"] += 1
                if reward >= 8.0:
                    mechanism_stats["relay"]["best_improving"] += 1
                if RelayTrigger.DELIVERY_RISK in relay_triggers:
                    relay_agg["delivery_risk_relays_accepted"] += 1
                if RelayTrigger.BLOCKER_RELEASE in relay_triggers:
                    relay_agg["blocker_relays_accepted"] += 1
                if len(relay_triggers) >= 2:
                    relay_agg["dual_trigger_relays_accepted"] += 1
                # late rescue: a pre-relay late task became on-time.
                post_late = {
                    tid
                    for tid, t in candidate_validation.delivery_times_min.items()
                    if problem.task(tid).deadline_min - t < 0
                }
                rescued = len(pre_relay_late_tasks - post_late)
                relay_agg["late_tasks_rescued"] += rescued
                if relay_pickup_advance > 1e-9:
                    relay_agg["critical_pickups_advanced"] += 1

        for key in (
            f"destroy:{destroy_name}",
            f"repair:{repair_name}",
        ):
            segment_uses[key] += 1
            segment_rewards[key] += reward

        # ---- adaptive weight update (destroy / repair only) ----
        if iteration % cfg.weight_update_interval == 0:
            for name in destroy_operators:
                key = f"destroy:{name}"
                if segment_uses[key]:
                    observed = segment_rewards[key] / segment_uses[key]
                    destroy_weights[name] = max(
                        cfg.minimum_weight,
                        (1 - cfg.reaction_factor) * destroy_weights[name]
                        + cfg.reaction_factor * observed,
                    )
            for name in repair_operators:
                key = f"repair:{name}"
                if segment_uses[key]:
                    observed = segment_rewards[key] / segment_uses[key]
                    repair_weights[name] = max(
                        cfg.minimum_weight,
                        (1 - cfg.reaction_factor) * repair_weights[name]
                        + cfg.reaction_factor * observed,
                    )
            segment_uses = {key: 0 for key in segment_uses}
            segment_rewards = {key: 0.0 for key in segment_rewards}

        temperature = max(
            cfg.minimum_temperature, temperature * cfg.cooling_rate
        )
        completed_iterations = iteration

    # ---- finalise ----
    final = _prune_active_stations(best, relay_pool)
    final_validation = validate_cooperative_solution(problem, final)
    if not final_validation.valid:
        raise RuntimeError(
            f"Cooperative-HALNS 最终解非法: {final_validation.violations}"
        )
    runtime = perf_counter() - started
    diagnostics = _pickup_diagnostics(problem, final)

    for name, stats in mechanism_stats.items():
        if name == "ownership":
            stats["final_weight"] = (
                destroy_weights.get("ownership", 1.0)
                * repair_weights.get("ownership_targeted", 1.0)
            )
        elif name == "relay":
            stats["final_weight"] = destroy_weights.get("relay_structure", 1.0)
        else:
            stats["final_weight"] = 1.0

    all_weights = {
        **{f"destroy:{k}": v for k, v in destroy_weights.items()},
        **{f"repair:{k}": v for k, v in repair_weights.items()},
    }

    if cfg.verbose:
        print(
            f"Coop-HALNS  |  late={final_validation.late_count} "
            f"dist={final_validation.distance_km:.1f}  "
            f"time={runtime:.1f}s  iters={completed_iterations}  "
            f"svc={service_mode_counts(final.task_services)}",
            flush=True,
        )
        print(
            f"  pickup_late={diagnostics['pickup_late_count']} "
            f"relays={len(final.relays)} "
            f"relay_accepted={relay_agg['relay_accepted']} "
            f"relay_exact_eval={relay_agg['relay_exact_evaluations']}",
            flush=True,
        )

    relay_count_final = len(final.relays)
    unique_relay_points_used = len(
        {relay.station_id for relay in final.relays}
    )
    return CooperativeHALNSResult(
        solution=final,
        validation=final_validation,
        runtime_seconds=runtime,
        iterations=completed_iterations,
        time_to_best_seconds=time_to_best,
        initial_score=initial_score,
        best_score=final_validation.score,
        pickup_late_count=diagnostics["pickup_late_count"],
        near_critical_pickup_count=diagnostics["near_critical_pickup_count"],
        negative_pickup_slack_sum=diagnostics["negative_pickup_slack_sum"],
        min_pickup_slack=diagnostics["min_pickup_slack"],
        blocker_count=diagnostics["blocker_count"],
        blocker_release_potential=diagnostics["blocker_release_potential"],
        op_stats=mechanism_stats,
        operator_weights=all_weights,
        service_distribution=service_mode_counts(final.task_services),
        station_adds=0,
        station_drops=0,
        station_replaces=0,
        active_station_count=len(final.active_stations),
        ownership_calls=mechanism_stats["ownership"]["calls"],
        ownership_accepted=ownership_accepted,
        relay_calls=relay_agg["relay_calls"],
        delivery_risk_tasks_detected=relay_agg["delivery_risk_tasks_detected"],
        blockers_detected=relay_agg["blockers_detected"],
        relay_candidates_before_dedup=relay_agg[
            "relay_candidates_before_dedup"
        ],
        relay_candidates_after_dedup=relay_agg["relay_candidates_after_dedup"],
        relay_candidates_screened_out=relay_agg[
            "relay_candidates_screened_out"
        ],
        relay_exact_evaluations=relay_agg["relay_exact_evaluations"],
        relay_feasible=relay_agg["relay_feasible"],
        relay_accepted=relay_agg["relay_accepted"],
        delivery_risk_relays_accepted=relay_agg[
            "delivery_risk_relays_accepted"
        ],
        blocker_relays_accepted=relay_agg["blocker_relays_accepted"],
        dual_trigger_relays_accepted=relay_agg["dual_trigger_relays_accepted"],
        late_tasks_rescued=relay_agg["late_tasks_rescued"],
        critical_pickups_advanced=relay_agg["critical_pickups_advanced"],
        relay_count_final=relay_count_final,
        unique_relay_points_used=unique_relay_points_used,
        metadata={
            "method": "Pickup-Aware Cooperative-HALNS",
            "seed": cfg.seed,
            "iterations": completed_iterations,
            "accepted_solutions": accepted_solutions,
            "time_limit_seconds": cfg.time_limit_seconds,
            "initial_score": (
                initial_score.late_count,
                initial_score.distance_km,
            ),
            "enable_ownership": cfg.enable_ownership,
            "enable_relay": cfg.enable_relay,
            "enable_delivery_risk_relay": cfg.enable_delivery_risk_relay,
            "enable_blocker_relay": cfg.enable_blocker_relay,
        },
    )
