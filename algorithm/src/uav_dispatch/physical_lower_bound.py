"""Task-level physical feasibility diagnostics.

For each task, compute two lower bounds:

- ``min_completion_i``: shortest possible completion time if one UAV serves
  only this task (depot → pickup → delivery, no other work).
- ``latest_pickup_i``: latest pickup time that still allows on-time delivery
  = deadline_i - d(P_i, D_i) / speed.

A task is *physically impossible* if ``min_completion_i > deadline_i``
or ``latest_pickup_i <= 0`` — no algorithm can ever deliver it on time.

All other late tasks are *scheduling failures*: the task could theoretically
be on time, but the current solution's pickup timing is too late.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .model import Problem, Task


@dataclass(frozen=True, slots=True)
class TaskFeasibility:
    """Physical-feasibility verdict for a single task."""

    task_id: int
    deadline_min: float
    direct_completion_min: float
    latest_pickup_min: float
    physically_possible: bool
    # --- from a concrete solution (may be NaN when missing) ---
    solution_pickup_min: float = float("nan")
    solution_delivery_min: float = float("nan")
    solution_on_time: bool = False
    scheduling_failure: bool = False

    @property
    def category(self) -> str:
        if not self.physically_possible:
            return "physically_impossible"
        if not self.solution_on_time:
            return "scheduling_failure"
        return "on_time"


def compute_min_completion(problem: Problem, task_id: int) -> float:
    """Shortest single-task completion time: depot → P_i → D_i, no load, no wait."""
    return problem.direct_completion_min(task_id)


def compute_latest_pickup(problem: Problem, task_id: int) -> float:
    """Latest pickup moment that still allows on-time delivery.

    ``deadline_i - d(P_i, D_i) / speed`` — if the UAV leaves the pickup point
    later than this, it cannot reach D_i before the deadline even if it flies
    directly.
    """
    task = problem.task(task_id)
    leg_min = problem.distance(task_id, -task_id) / problem.speed_km_per_min
    return task.deadline_min - leg_min


# -- Unified public API (preferred entry points) ---------------------------


def latest_pickup_time(problem: Problem, task_id: int) -> float:
    """Canonical latest pickup time for *task_id*."""
    return compute_latest_pickup(problem, task_id)


def pickup_slack(
    problem: Problem, task_id: int, pickup_time: float
) -> float:
    """Pickup slack: ``latest_pickup_time - pickup_time``.

    Negative → pickup is already too late for on-time delivery even with a
    direct flight.
    Zero   → exactly on the boundary.
    """
    return latest_pickup_time(problem, task_id) - pickup_time


def pickup_risk(
    problem: Problem, task_id: int, pickup_time: float
) -> float:
    """Pickup urgency score (non-negative; higher = more critical).

    When slack ≥ 0: ``1.0 / (1.0 + slack)`` — decays gracefully.
    When slack < 0: ``1.0 + abs(slack)`` — already late, high priority.
    """
    slack = pickup_slack(problem, task_id, pickup_time)
    if slack >= 0:
        return 1.0 / (1.0 + slack)
    return 1.0 + abs(slack)


# -- Route-based pickup-time helpers --------------------------------------


def compute_pickup_times(
    problem: Problem, routes: Sequence[Sequence[int]]
) -> dict[int, float]:
    """Compute the pickup time for every task given signed routes.

    Mimics the simple single-route propagation that ``RouteEvaluator`` uses:
    elapsed time accumulates over the route and is recorded at the ``+task_id``
    visit.
    """
    pickups: dict[int, float] = {}
    for route in routes:
        elapsed = 0.0
        prev: int | None = None
        for visit in route:
            elapsed += problem.distance(prev, visit) / problem.speed_km_per_min
            prev = visit
            if visit > 0:
                pickups[visit] = elapsed
    return pickups


def feasibility_report(
    problem: Problem,
    routes: Sequence[Sequence[int]],
) -> tuple[TaskFeasibility, ...]:
    """Produce one :class:`TaskFeasibility` record for every task."""
    pickups = compute_pickup_times(problem, routes)
    delivery_times = _compute_delivery_times(problem, routes)

    records: list[TaskFeasibility] = []
    for task_id in problem.task_ids:
        deadline = problem.task(task_id).deadline_min
        direct_comp = compute_min_completion(problem, task_id)
        latest_pickup = compute_latest_pickup(problem, task_id)
        physically_possible = direct_comp <= deadline and latest_pickup > 0

        pickup_t = pickups.get(task_id, float("nan"))
        delivery_t = delivery_times.get(task_id, float("nan"))
        on_time = delivery_t <= deadline + 1e-9 if delivery_t == delivery_t else False
        scheduling_failure = physically_possible and not on_time

        records.append(
            TaskFeasibility(
                task_id=task_id,
                deadline_min=deadline,
                direct_completion_min=direct_comp,
                latest_pickup_min=latest_pickup,
                physically_possible=physically_possible,
                solution_pickup_min=pickup_t,
                solution_delivery_min=delivery_t,
                solution_on_time=on_time,
                scheduling_failure=scheduling_failure,
            )
        )
    return tuple(records)


def _compute_delivery_times(
    problem: Problem, routes: Sequence[Sequence[int]]
) -> dict[int, float]:
    """Compute delivery time for every task from signed routes."""
    deliveries: dict[int, float] = {}
    for route in routes:
        elapsed = 0.0
        prev: int | None = None
        for visit in route:
            elapsed += problem.distance(prev, visit) / problem.speed_km_per_min
            prev = visit
            if visit < 0:
                deliveries[abs(visit)] = elapsed
    return deliveries


# ---------------------------------------------------------------------------
# Capacity-saturation diagnostics (Step 0.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CapacityProfile:
    """Capacity time-series for one drone."""

    drone: int
    intervals: tuple[tuple[float, float, int], ...]
    """(start_min, end_min, load) — contiguous constant-load windows."""

    @property
    def total_time_min(self) -> float:
        if not self.intervals:
            return 0.0
        return self.intervals[-1][1]

    @property
    def loaded_fraction(self) -> float:
        """Proportion of time the drone carries ≥ 1 parcel."""
        total = self.total_time_min
        if total <= 0:
            return 0.0
        loaded = sum(
            end - start for start, end, load in self.intervals if load >= 1
        )
        return loaded / total

    @property
    def full_fraction(self) -> float:
        """Proportion of time the drone is at capacity (load == 2)."""
        total = self.total_time_min
        if total <= 0:
            return 0.0
        full = sum(
            end - start for start, end, load in self.intervals if load >= 2
        )
        return full / total


def capacity_profile(
    problem: Problem, routes: Sequence[Sequence[int]]
) -> tuple[CapacityProfile, ...]:
    """Build per-drone capacity profiles from signed routes."""
    profiles: list[CapacityProfile] = []
    for drone, route in enumerate(routes):
        intervals: list[tuple[float, float, int]] = []
        elapsed = 0.0
        load = 0
        prev: int | None = None
        for visit in route:
            leg_min = problem.distance(prev, visit) / problem.speed_km_per_min
            start = elapsed
            elapsed += leg_min
            if intervals:
                intervals[-1] = (intervals[-1][0], start, intervals[-1][2])
            intervals.append((start, elapsed, load))
            load += 1 if visit > 0 else -1
            prev = visit
        profiles.append(
            CapacityProfile(drone=drone, intervals=tuple(intervals))
        )
    return tuple(profiles)


# ---------------------------------------------------------------------------
# Crossover-saving distribution (Step 0.2)
# ---------------------------------------------------------------------------


def crossover_saving(
    problem: Problem, task_i: int, task_j: int
) -> float:
    """Destination-crossover distance saving S_ij for a potential swap.

    Positive = swapping deliveries saves distance; negative = swapping costs
    more than direct delivery.
    """
    task_a = problem.task(task_i)
    task_b = problem.task(task_j)
    return (
        task_a.pickup.distance_to(task_a.delivery)
        + task_b.pickup.distance_to(task_b.delivery)
        - task_a.pickup.distance_to(task_b.delivery)
        - task_b.pickup.distance_to(task_a.delivery)
    )


def crossover_saving_distribution(
    problem: Problem,
) -> list[tuple[int, int, float]]:
    """Compute S_ij for all unordered task pairs (i < j)."""
    results: list[tuple[int, int, float]] = []
    ids = problem.task_ids
    for idx_i, i in enumerate(ids):
        for j in ids[idx_i + 1:]:
            saving = crossover_saving(problem, i, j)
            results.append((i, j, saving))
    return results
