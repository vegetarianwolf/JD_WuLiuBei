"""Relay candidate generation with cheap bounds and full DAG verification.

A relay candidate for task ``i`` is::

    (task_id, station_id, first_drone, second_drone,
     drop_gap, pick_gap, delivery_gap)

``first_drone`` keeps the original pickup (``+i`` stays on its route); the
original ``-i`` is removed from its route and inserted into ``second_drone``'s
route at ``delivery_gap``; the station drop/pick gaps position the two
asynchronous relay events.  Every candidate is validated in full by
:func:`~uav_dispatch.relay_validation.evaluate_relay_solution`; cheap bounds
only prune obviously hopeless candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Sequence

from .model import Problem, Score
from .relay_model import (
    RELAY_ENDPOINT_EPS,
    RelaySolution,
    RelayStation,
    RelayTransfer,
)
from .relay_validation import RelaySolutionEvaluation, evaluate_relay_solution
from .search import Route, Routes


@dataclass(frozen=True, slots=True)
class RelayCandidate:
    """One concrete relay move, fully evaluated against the current solution."""

    task_id: int
    station_id: int
    first_drone: int
    second_drone: int
    drop_gap: int
    pick_gap: int
    delivery_gap: int
    delta_score: Score
    relay_count: int
    evaluation: RelaySolutionEvaluation

    @property
    def signature(self) -> tuple[int, ...]:
        return (
            self.task_id,
            self.station_id,
            self.first_drone,
            self.second_drone,
            self.drop_gap,
            self.pick_gap,
            self.delivery_gap,
        )


def _insert_visit(route: Route, gap: int, visit: int) -> Route:
    return route[:gap] + (visit,) + route[gap:]


def _remove_visit(route: Route, visit: int) -> Route | None:
    """Remove the first occurrence of ``visit``; None if absent."""

    for index, value in enumerate(route):
        if value == visit:
            return route[:index] + route[index + 1 :]
    return None


@dataclass(frozen=True, slots=True)
class FreshRelayOption:
    """A relay insertion for a task that is not yet present in any route.

    The first drone picks the task up at ``pickup_gap`` and drops it at the
    station at ``drop_gap``; the second drone picks it up at ``pick_gap`` and
    delivers it at ``delivery_gap``.
    """

    task_id: int
    station_id: int
    first_drone: int
    second_drone: int
    pickup_gap: int
    drop_gap: int
    pick_gap: int
    delivery_gap: int
    delta_score: Score
    evaluation: RelaySolutionEvaluation
    solution: RelaySolution

    @property
    def signature(self) -> tuple[int, ...]:
        return (
            self.task_id,
            self.station_id,
            self.first_drone,
            self.second_drone,
            self.pickup_gap,
            self.drop_gap,
            self.pick_gap,
            self.delivery_gap,
        )


def apply_fresh_relay(
    problem: Problem,
    solution: RelaySolution,
    option: FreshRelayOption,
) -> RelaySolution:
    """Return the RelaySolution that results from applying a fresh relay."""

    routes = [list(route) for route in solution.routes]
    first_route = routes[option.first_drone]
    routes[option.first_drone] = (
        first_route[: option.pickup_gap]
        + [option.task_id]
        + first_route[option.pickup_gap :]
    )
    second_route = routes[option.second_drone]
    routes[option.second_drone] = (
        second_route[: option.delivery_gap]
        + [-option.task_id]
        + second_route[option.delivery_gap :]
    )
    new_relays = list(solution.relays)
    new_relays.append(
        RelayTransfer(
            id=max((r.id for r in solution.relays), default=-1) + 1,
            task_id=option.task_id,
            station_id=option.station_id,
            first_drone=option.first_drone,
            second_drone=option.second_drone,
            drop_position=option.drop_gap,
            pick_position=option.pick_gap,
        )
    )
    return RelaySolution(
        tuple(tuple(route) for route in routes),
        tuple(new_relays),
        solution.stations,
    )


def relay_insertion_options(
    problem: Problem,
    solution: RelaySolution,
    task_id: int,
    *,
    stations: Sequence[RelayStation],
    handling_time_min: float = 0.0,
    preferred_station_id: int | None = None,
    first_drone_limit: int = 2,
    second_drone_limit: int = 2,
    pickup_gap_limit: int = 2,
    drop_gap_limit: int = 2,
    pick_gap_limit: int = 2,
    delivery_gap_limit: int = 2,
    max_candidates: int | None = None,
    max_evaluations: int = 60,
    deadline: float | None = None,
    require_all_tasks: bool = True,
) -> tuple[FreshRelayOption, ...]:
    """Generate fully-evaluated relay insertion options for an unplaced task.

    The task is not expected to be in any route yet (multi-mode construction
    and Stage-II repair).  Capacity windows on the receiver and the first
    drone's K slot are respected; every option is DAG-validated.  The search
    stops after ``max_evaluations`` DAG evaluations to bound the cost of
    mostly-infeasible placements.

    Pass ``require_all_tasks=False`` when the solution is partial (mid-repair)
    so the completeness check does not invalidate every candidate.
    """

    from time import perf_counter

    # The fresh-insertion options must carry the station set we are exploring
    # (which may be a subset/filtered view, e.g. ``_fresh_stations``).  If the
    # incoming solution carries a different station tuple, rebuild it so the
    # generated ``RelaySolution``s reference the explored stations.
    if tuple(solution.stations) != tuple(stations):
        solution = RelaySolution(
            solution.routes, solution.relays, tuple(stations)
        )

    current = evaluate_relay_solution(
        problem,
        solution,
        handling_time_min=handling_time_min,
        require_all_tasks=require_all_tasks,
    )
    current_score = current.score
    capacity = problem.capacity
    task = problem.task(task_id)
    evaluations = 0

    ordered_stations = sorted(
        stations,
        key=lambda station: (
            0 if station.id == preferred_station_id else 1,
            station.id,
        ),
    )

    def task_count(drone: int) -> int:
        return sum(visit > 0 for visit in solution.routes[drone])

    first_candidates = sorted(
        range(len(solution.routes)),
        key=lambda drone: (task_count(drone), drone),
    )[:first_drone_limit]

    options: list[FreshRelayOption] = []
    seen: set[tuple[int, ...]] = set()
    for station in ordered_stations:
        if deadline is not None and perf_counter() >= deadline:
            break
        if station.point.distance_to(task.pickup) <= RELAY_ENDPOINT_EPS:
            continue
        if station.point.distance_to(task.delivery) <= RELAY_ENDPOINT_EPS:
            continue
        for first in first_candidates:
            if task_count(first) + 1 > problem.max_tasks_per_drone:
                continue
            base_first = solution.routes[first]
            first_loads = _loads_before(base_first)
            pickup_gaps = sorted(
                range(len(base_first) + 1),
                key=lambda gap: _insertion_detour_km(problem, base_first, gap, task_id),
            )[:pickup_gap_limit]
            for pickup_gap in pickup_gaps:
                if first_loads[pickup_gap] > capacity - 1:
                    continue
                with_pickup = _insert_visit(base_first, pickup_gap, task_id)
                drop_gaps = sorted(
                    range(pickup_gap + 1, len(with_pickup) + 1),
                    key=lambda gap: _insertion_detour_km(
                        problem, with_pickup, gap, station.source_visit
                    ),
                )[:drop_gap_limit]

                ranked_seconds = sorted(
                    (
                        second
                        for second in range(len(solution.routes))
                        if second != first
                    ),
                    key=lambda second: (
                        -_receiver_window_quality(
                            solution.routes[second], capacity
                        ),
                        best_station_insert_detour_km(
                            problem,
                            solution.routes[second],
                            station.source_visit,
                        ),
                    ),
                )[:second_drone_limit]
                for second in ranked_seconds:
                    base_second = solution.routes[second]
                    loads = _loads_before(base_second)
                    for start, end in _low_load_intervals(loads, capacity):
                        pick_pool = range(start, end)
                        pick_gaps = sorted(
                            pick_pool,
                            key=lambda gap: _insertion_detour_km(
                                problem, base_second, gap, station.source_visit
                            ),
                        )[:pick_gap_limit]
                        for pick_gap in pick_gaps:
                            delivery_pool = [
                                gap
                                for gap in range(pick_gap + 1, end)
                                if loads[gap] <= capacity - 1
                            ]
                            delivery_gaps = sorted(
                                delivery_pool,
                                key=lambda gap: _insertion_detour_km(
                                    problem, base_second, gap, -task_id
                                ),
                            )[:delivery_gap_limit]
                            for drop_gap in drop_gaps:
                                for delivery_gap in delivery_gaps:
                                    signature = (
                                        task_id,
                                        station.id,
                                        first,
                                        second,
                                        pickup_gap,
                                        drop_gap,
                                        pick_gap,
                                        delivery_gap,
                                    )
                                    if signature in seen:
                                        continue
                                    seen.add(signature)
                                    option = FreshRelayOption(
                                        task_id=task_id,
                                        station_id=station.id,
                                        first_drone=first,
                                        second_drone=second,
                                        pickup_gap=pickup_gap,
                                        drop_gap=drop_gap,
                                        pick_gap=pick_gap,
                                        delivery_gap=delivery_gap,
                                        delta_score=Score(0, 0.0),
                                        evaluation=current,
                                        solution=solution,
                                    )
                                    try:
                                        modified = apply_fresh_relay(
                                            problem, solution, option
                                        )
                                    except ValueError:
                                        continue
                                    evaluation = evaluate_relay_solution(
                                        problem,
                                        modified,
                                        handling_time_min=handling_time_min,
                                        require_all_tasks=require_all_tasks,
                                    )
                                    evaluations += 1
                                    if evaluations >= max_evaluations:
                                        options.sort(
                                            key=lambda o: o.delta_score
                                        )
                                        return tuple(options)
                                    if not evaluation.valid:
                                        continue
                                    delta = evaluation.score - current_score
                                    options.append(
                                        FreshRelayOption(
                                            task_id=task_id,
                                            station_id=station.id,
                                            first_drone=first,
                                            second_drone=second,
                                            pickup_gap=pickup_gap,
                                            drop_gap=drop_gap,
                                            pick_gap=pick_gap,
                                            delivery_gap=delivery_gap,
                                            delta_score=delta,
                                            evaluation=evaluation,
                                            solution=modified,
                                        )
                                    )
                                    if (
                                        max_candidates is not None
                                        and len(options) >= max_candidates
                                    ):
                                        options.sort(
                                            key=lambda o: o.delta_score
                                        )
                                        return tuple(options)

    options.sort(key=lambda option: (option.delta_score, option.delivery_gap))
    return tuple(options)


def apply_relay_candidate(
    problem: Problem,
    solution: RelaySolution,
    candidate: RelayCandidate,
) -> RelaySolution:
    """Return the RelaySolution that results from applying ``candidate``."""

    routes = [list(route) for route in solution.routes]
    first = candidate.first_drone
    second = candidate.second_drone
    task_id = candidate.task_id

    # The pickup (+task) must already be on the first drone's route.
    if task_id not in (abs(v) for v in routes[first]):
        raise ValueError(f"任务 {task_id} 不在 first 无人机路线上")

    # Remove the original delivery from whichever route holds it.
    removed = False
    for drone in range(len(routes)):
        if -task_id in routes[drone]:
            routes[drone] = list(_remove_visit(tuple(routes[drone]), -task_id))
            removed = True
    if not removed:
        raise ValueError(f"任务 {task_id} 缺少送达事件")

    # Insert the delivery into the second drone's route.
    second_route = tuple(routes[second])
    routes[second] = list(
        _insert_visit(second_route, candidate.delivery_gap, -task_id)
    )

    new_relays = list(solution.relays)
    new_relays.append(
        RelayTransfer(
            id=max((r.id for r in solution.relays), default=-1) + 1,
            task_id=task_id,
            station_id=candidate.station_id,
            first_drone=first,
            second_drone=second,
            drop_position=candidate.drop_gap,
            pick_position=candidate.pick_gap,
        )
    )
    return RelaySolution(
        tuple(tuple(route) for route in routes),
        relays=tuple(new_relays),
        stations=solution.stations,
    )


def _insertion_detour_km(
    problem: Problem, route: Route, gap: int, visit: int
) -> float:
    """Distance delta of inserting ``visit`` at ``gap`` in ``route``."""

    prev = None if gap == 0 else route[gap - 1]
    nxt = None if gap == len(route) else route[gap]
    if prev is None and nxt is None:
        return problem.distance(None, visit)
    if prev is None:
        return problem.distance(None, visit) + problem.distance(
            visit, nxt
        ) - problem.distance(None, nxt)
    if nxt is None:
        return problem.distance(prev, visit)
    return (
        problem.distance(prev, visit)
        + problem.distance(visit, nxt)
        - problem.distance(prev, nxt)
    )


def _loads_before(route: Route) -> tuple[int, ...]:
    """Load in front of each gap ``0..len(route)``."""

    loads = [0]
    load = 0
    for visit in route:
        load += 1 if visit > 0 else -1
        loads.append(load)
    return tuple(loads)


def _low_load_intervals(
    loads: tuple[int, ...], capacity: int
) -> tuple[tuple[int, int], ...]:
    """Maximal gap intervals where ``loads[gap] <= capacity - 1``.

    A relay package occupies one slot, so the receiver can only carry it while
    its own load is at most ``capacity - 1``.
    """

    intervals: list[tuple[int, int]] = []
    start: int | None = None
    for gap, load in enumerate(loads):
        if load <= capacity - 1:
            if start is None:
                start = gap
        else:
            if start is not None:
                intervals.append((start, gap))
                start = None
    if start is not None:
        intervals.append((start, len(loads)))
    return tuple(intervals)


def _receiver_window_quality(route: Route, capacity: int) -> int:
    """Longest low-load interval length on a route (receiver availability)."""

    loads = _loads_before(route)
    intervals = _low_load_intervals(loads, capacity)
    return max((end - start for start, end in intervals), default=0)


def best_station_insert_detour_km(
    problem: Problem, route: Sequence[int], station_source_visit: int
) -> float:
    """Cheapest distance delta of inserting a station visit into a route."""

    best = float("inf")
    for gap in range(len(route) + 1):
        prev = None if gap == 0 else route[gap - 1]
        nxt = None if gap == len(route) else route[gap]
        if prev is None and nxt is None:
            delta = problem.distance(None, station_source_visit)
        elif prev is None:
            delta = (
                problem.distance(None, station_source_visit)
                + problem.distance(station_source_visit, nxt)
                - problem.distance(None, nxt)
            )
        elif nxt is None:
            delta = problem.distance(prev, station_source_visit)
        else:
            delta = (
                problem.distance(prev, station_source_visit)
                + problem.distance(station_source_visit, nxt)
                - problem.distance(prev, nxt)
            )
        best = min(best, delta)
    return best


def _time_feasible(
    problem: Problem,
    route_first: Route,
    pickup_index: int,
    route_second: Route,
    station_visit: int,
    task_id: int,
    handling_time_min: float,
) -> bool:
    """Time feasibility check: can the relay possibly meet the deadline?

    ``False`` means no gap combination can make the task on time — skip
    all expensive DAG evaluations for this (station, second) pair.
    """
    speed = problem.speed_km_per_min
    task = problem.task(task_id)

    # Earliest possible drop: right after pickup at gap = pickup_index + 1.
    earliest_drop = 0.0
    prev: int | None = None
    for idx, visit in enumerate(route_first):
        earliest_drop += problem.distance(prev, visit) / speed
        prev = visit
        if idx == pickup_index:
            break
    earliest_drop += problem.distance(route_first[pickup_index], station_visit) / speed

    ready = earliest_drop + handling_time_min

    # Latest allowable pickup: second drone must pick up and fly to delivery
    # before deadline.
    leg_to_delivery = problem.distance(station_visit, -task_id) / speed
    latest_pickup = task.deadline_min - leg_to_delivery

    if ready >= latest_pickup - 1e-9:
        return False

    # Also check: can the second drone even *reach* the station before latest_pickup?
    # Conservative: second drone flies directly from its start to station.
    best_second_arrival = (
        problem.distance(None, route_second[0]) if route_second else 0.0
    )
    for idx in range(len(route_second)):
        best_second_arrival += problem.distance(
            route_second[idx - 1] if idx > 0 else None,
            route_second[idx],
        ) / speed
        # As soon as we can reach the station in theory
    # This is too complex. Simpler: first arrival at station is via first possible gap.
    # Use a rough lower bound: second drone starts at time 0 from depot.
    min_to_station = problem.distance(None, station_visit) / speed
    best_pickup = max(ready, min_to_station)

    return best_pickup <= latest_pickup - 1e-9


def relay_candidates_for_task(
    problem: Problem,
    solution: RelaySolution,
    task_id: int,
    *,
    stations: Sequence[RelayStation],
    handling_time_min: float = 0.0,
    second_drone_limit: int = 3,
    drop_gap_limit: int = 5,
    pick_gap_limit: int = 5,
    delivery_gap_limit: int = 5,
    max_candidates: int | None = None,
    max_evaluations: int = 60,
    stop_at_improvement: bool = False,
    deadline: float | None = None,
    require_all_tasks: bool = True,
    fixed_second: int | None = None,
    fixed_station_id: int | None = None,
    eval_counter: list | None = None,
) -> tuple[RelayCandidate, ...]:
    """Generate and fully evaluate shortlisted relay candidates for one task.

    Drop gaps are restricted to lie at or before the original delivery (early
    release; keeps first-drone capacity legal).  Receiver pick/delivery gaps
    are restricted to low-load intervals where the relay package fits within
    capacity.  Every surviving candidate is validated by the full DAG
    evaluator.  Candidates that cannot rescue the task itself are still
    returned: they may act as upstream blockers that accelerate downstream
    pickups.  The search stops after ``max_evaluations`` DAG evaluations.

    ``fixed_second`` restricts the receiver to one drone; ``fixed_station_id``
    restricts the relay point to one catalog station (both used by the
    criticality-guided relay to evaluate a concrete shortlisted candidate
    exactly).  Pass ``require_all_tasks=False`` for partial solutions
    (mid-repair).
    """

    from time import perf_counter

    # Same station-consistency fix as ``relay_insertion_options``: the
    # generated candidates must reference the explored station set.
    if tuple(solution.stations) != tuple(stations):
        solution = RelaySolution(
            solution.routes, solution.relays, tuple(stations)
        )

    if fixed_station_id is not None:
        stations = tuple(st for st in stations if st.id == fixed_station_id)
        if not stations:
            return ()

    current = evaluate_relay_solution(
        problem,
        solution,
        handling_time_min=handling_time_min,
        require_all_tasks=require_all_tasks,
    )
    current_score = current.score
    capacity = problem.capacity
    evaluations = 0

    pickup_drone: int | None = None
    pickup_index: int = -1
    delivery_drone: int | None = None
    delivery_index: int = -1
    for drone, route in enumerate(solution.routes):
        for index, visit in enumerate(route):
            if visit == task_id:
                pickup_drone = drone
                pickup_index = index
            elif visit == -task_id:
                delivery_drone = drone
                delivery_index = index
    if pickup_drone is None:
        raise ValueError(f"任务 {task_id} 缺少取件事件")
    if delivery_drone is None:
        raise ValueError(f"任务 {task_id} 缺少送达事件")

    task = problem.task(task_id)
    first = pickup_drone
    first_route = solution.routes[first]

    candidates: list[RelayCandidate] = []
    seen_signatures: set[tuple[int, ...]] = set()

    for station in stations:
        if deadline is not None and perf_counter() >= deadline:
            break
        if station.point.distance_to(task.pickup) <= RELAY_ENDPOINT_EPS:
            continue
        if station.point.distance_to(task.delivery) <= RELAY_ENDPOINT_EPS:
            continue
        station_visit = station.source_visit

        # Drop gaps: strictly after the pickup, at or before the original
        # delivery (early release keeps first-drone capacity legal).  The
        # relay's value comes from releasing the first drone as early as
        # possible (rescuing downstream pickups), so gaps are explored in
        # *earliest-first* order rather than by local detour.
        drop_upper = min(len(first_route), delivery_index + 1)
        drop_gaps = list(
            range(pickup_index + 1, drop_upper + 1)
        )[:drop_gap_limit]

        if fixed_second is not None:
            ranked_seconds = (
                [fixed_second]
                if fixed_second != first
                and 0 <= fixed_second < len(solution.routes)
                else []
            )
        else:
            ranked_seconds = sorted(
                (
                    second
                    for second in range(len(solution.routes))
                    if second != first
                ),
                key=lambda second: best_station_insert_detour_km(
                    problem, solution.routes[second], station_visit
                ),
            )[:second_drone_limit]

        for second in ranked_seconds:
            if deadline is not None and perf_counter() >= deadline:
                break
            second_route = solution.routes[second]

            # ---- time feasibility lower bound ----
            # Skip when require_all_tasks=False: blocker-relay goal is
            # capacity release, not on-time delivery of the blocker itself.
            if require_all_tasks and not _time_feasible(
                problem, first_route, pickup_index,
                second_route, station_visit, task_id, handling_time_min,
            ):
                continue

            loads = _loads_before(second_route)

            # The second drone's delivery of -task must sit at or before its
            # current delivery index (structural ordering).
            second_delivery_upper = (
                delivery_index if second == delivery_drone else len(second_route)
            )

            intervals = _low_load_intervals(loads, capacity)
            # Process windows earliest-first: the rescue value of a relay
            # comes from delivering the task as early as possible, so the
            # receiver picks the parcel at the earliest legal window.
            for start, end in intervals:
                pick_pool = range(start, min(end, second_delivery_upper + 1))
                pick_gaps = sorted(
                    pick_pool,
                    key=lambda gap: gap,  # earliest window first
                )[:pick_gap_limit]
                for pick_gap in pick_gaps:
                    delivery_pool = [
                        gap
                        for gap in range(
                            pick_gap + 1, min(end, second_delivery_upper + 1)
                        )
                        if loads[gap] <= capacity - 1
                    ]
                    delivery_gaps = sorted(
                        delivery_pool,
                        key=lambda gap: _insertion_detour_km(
                            problem, second_route, gap, -task_id
                        ),
                    )[:delivery_gap_limit]
                    for drop_gap in drop_gaps:
                        for delivery_gap in delivery_gaps:
                            signature = (
                                task_id,
                                station.id,
                                first,
                                second,
                                drop_gap,
                                pick_gap,
                                delivery_gap,
                            )
                            if signature in seen_signatures:
                                continue
                            seen_signatures.add(signature)
                            candidate_spec = RelayCandidate(
                                task_id=task_id,
                                station_id=station.id,
                                first_drone=first,
                                second_drone=second,
                                drop_gap=drop_gap,
                                pick_gap=pick_gap,
                                delivery_gap=delivery_gap,
                                delta_score=Score(0, 0.0),
                                relay_count=0,
                                evaluation=current,
                            )
                            try:
                                modified = apply_relay_candidate(
                                    problem, solution, candidate_spec
                                )
                            except ValueError:
                                continue
                            evaluation = evaluate_relay_solution(
                                problem,
                                modified,
                                handling_time_min=handling_time_min,
                                require_all_tasks=require_all_tasks,
                            )
                            evaluations += 1
                            if eval_counter is not None:
                                eval_counter.append(True)
                            if evaluations >= max_evaluations:
                                candidates.sort(
                                    key=lambda item: (
                                        item.delta_score,
                                        item.delivery_gap,
                                    )
                                )
                                return tuple(candidates)
                            if not evaluation.valid:
                                continue
                            delta = evaluation.score - current_score
                            candidates.append(
                                RelayCandidate(
                                    task_id=task_id,
                                    station_id=station.id,
                                    first_drone=first,
                                    second_drone=second,
                                    drop_gap=drop_gap,
                                    pick_gap=pick_gap,
                                    delivery_gap=delivery_gap,
                                    delta_score=delta,
                                    relay_count=len(modified.relays),
                                    evaluation=evaluation,
                                )
                            )
                            if stop_at_improvement and delta < Score(0, 0.0):
                                return (candidates[-1],)
                            if (
                                max_candidates is not None
                                and len(candidates) >= max_candidates
                            ):
                                candidates.sort(
                                    key=lambda item: (
                                        item.delta_score,
                                        item.delivery_gap,
                                    )
                                )
                                return tuple(candidates)

    candidates.sort(key=lambda item: (item.delta_score, item.delivery_gap))
    return tuple(candidates)
