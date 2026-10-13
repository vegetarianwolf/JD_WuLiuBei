"""First-flight and deployment-utilization diagnostics for route plans.

These read-only helpers quantify how much a solved plan exploits a
distributed UAV home deployment:

* ``deadhead_km`` / ``first_flight_km``: total and per-drone empty first leg
  from each drone's home to its first visit;
* ``deadhead_vs_origin_km``: the same routes' first legs if every drone had
  started at the origin, i.e. the deadhead the deployment saved;
* ``home_mismatch``: how many tasks are flown by a drone whose home is *not*
  the nearest deployment point to the task pickup (assignment locality);
* ``min_deadhead_lb`` / ``origin_min_deadhead_lb``: per-drone lower bounds on
  the unavoidable first-flight distance for a given deployment and for the
  all-origin baseline, so the "deployment leverage ceiling" can be reported.

Everything depends only on the public ``Problem`` / ``Point`` API plus the
solved routes, so these functions are safe to reuse from reports or tests.
"""

from __future__ import annotations

from typing import Sequence

from .model import Point, Problem


def _task_route_index(
    problem: Problem,
    routes: Sequence[Sequence[int]],
) -> dict[int, int]:
    """Map each original task id to the route index that carries it.

    Relay-leg visits share the positive-sign space with task pickups, so the
    collected visit ids are filtered against the actual task pool.
    """

    task_ids = set(problem.task_ids)
    return {
        visit: route_index
        for route_index, route in enumerate(routes)
        for visit in route
        if visit > 0 and visit in task_ids
    }


def first_flight_km(problem: Problem, routes: Sequence[Sequence[int]]) -> list[float]:
    """Per-drone empty first-leg distance (home -> first visit), 0 if idle."""

    legs: list[float] = []
    for route_index, route in enumerate(routes):
        if not route:
            legs.append(0.0)
            continue
        legs.append(
            problem.distance(
                None,
                route[0],
                from_node=problem.home_node(route_index),
            )
        )
    return legs


def deadhead_km(problem: Problem, routes: Sequence[Sequence[int]]) -> float:
    """Total empty first-leg distance across the fleet."""

    return sum(first_flight_km(problem, routes))


def deadhead_vs_origin_km(
    problem: Problem, routes: Sequence[Sequence[int]]
) -> float:
    """First-leg total the same routes would cost if all drones were at the
    origin, minus the actual first-leg total (saving from deployment)."""

    actual = 0.0
    origin = 0.0
    for route_index, route in enumerate(routes):
        if not route:
            continue
        actual += problem.distance(
            None,
            route[0],
            from_node=problem.home_node(route_index),
        )
        origin += problem.distance(None, route[0], from_node=0)
    return origin - actual


def home_mismatch(
    problem: Problem, routes: Sequence[Sequence[int]]
) -> tuple[int, int, float]:
    """Count tasks whose route's home is not the nearest deployment point to
    the pickup.

    Returns ``(matched, mismatched, total_gap_km)`` where the gap is the
    summed extra distance between the assigned home and the nearest home.
    """

    homes = problem.home_points()
    route_by_task = _task_route_index(problem, routes)
    matched = 0
    mismatched = 0
    total_gap = 0.0
    for task_id, route_index in route_by_task.items():
        pickup = problem.task(task_id).pickup
        own_home = homes[route_index]
        nearest_gap = min(
            home.distance_to(pickup) for home in homes
        )
        own_gap = own_home.distance_to(pickup)
        if own_gap <= nearest_gap + 1e-12:
            matched += 1
        else:
            mismatched += 1
            total_gap += own_gap - nearest_gap
    return matched, mismatched, total_gap


def home_assignment_rate(
    problem: Problem, routes: Sequence[Sequence[int]]
) -> float:
    """Fraction of tasks whose route's home is the nearest deployment point
    to the pickup (analysis-only; never part of the dispatch objective)."""

    matched, mismatched, _ = home_mismatch(problem, routes)
    total = matched + mismatched
    return matched / total if total else 0.0


def min_deadhead_lb(problem: Problem) -> float:
    """Lower bound on total first-flight distance for the current deployment:
    each drone flies at least to its nearest task pickup."""

    homes = problem.home_points()
    total = 0.0
    for home in homes:
        total += min(
            home.distance_to(task.pickup) for task in problem.tasks
        )
    return total


def origin_min_deadhead_lb(problem: Problem) -> float:
    """Same lower bound when every drone starts at the origin (depot)."""

    depot: Point = problem.depot
    return sum(
        min(depot.distance_to(task.pickup) for task in problem.tasks)
        for _ in range(problem.drone_count)
    )
