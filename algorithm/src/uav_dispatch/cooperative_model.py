"""Unified Cooperative search-state model (Pickup-Aware Cooperative HALNS).

This module defines the single search-state vocabulary used by the formal
Cooperative HALNS: :class:`ServiceMode`, :class:`TaskServiceState`, and the
helpers that derive / reconcile per-task service records against the raw
``routes + relays`` representation.

The formal Cooperative-HALNS has exactly two service modes:

- ``DIRECT`` — ``A: P_i -> D_i`` (same drone picks up and delivers).
- ``RELAY``  — ``A: P_i -> h`` then ``B: h -> D_i`` (two *different* drones,
  an asynchronous hand-over at relay point ``h``).

Swap is no longer part of the formal cooperative path (``swap_model`` /
``swap_search`` / ``swap_validation`` remain as legacy standalone modules).

``routes + relays`` is the single source of truth for the execution
structure; :func:`rebuild_services` recovers the per-task
:class:`TaskServiceState` (pickup owner / delivery owner / mode).  The
``ownership`` map on :class:`CooperativeSolution` must always agree with the
actual ``CUSTOMER_PICK`` owner read off the routes — it is never a second
independent truth.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum, auto
from typing import Mapping, Sequence

from .model import Problem
from .physical_lower_bound import pickup_slack as _canonical_pickup_slack
from .relay_model import RelayTransfer
from .search import Routes


class ServiceMode(Enum):
    """How one task's pickup-delivery is fulfilled inside a cooperative state.

    Formal Cooperative-HALNS only: ``DIRECT`` (same drone) or ``RELAY``
    (donor picks up + drops at a relay point, receiver delivers).
    """

    DIRECT = auto()  # A: P_i -> D_i  (pickup_owner == delivery_owner)
    RELAY = auto()   # A: P_i -> h ; B: h -> D_i  (pickup_owner != delivery_owner)


@dataclass
class TaskServiceState:
    """Complete service record for one task.

    Every destroy / repair / ownership / relay move operates on this unit —
    never on raw split routes.

    ``primary_owner`` is the original customer pickup owner (the ``K``
    constraint applies to it); :attr:`pickup_owner` is an alias for the new
    formal vocabulary.  ``delivery_owner`` is the drone that performs the
    final delivery (same as ``primary_owner`` for DIRECT, ``relay.second_drone``
    for RELAY).
    """

    task_id: int
    mode: ServiceMode = ServiceMode.DIRECT
    #: UAV that performs the original customer pickup (K-constraint applies).
    primary_owner: int = -1
    #: UAV that performs the final delivery.
    delivery_owner: int = -1
    #: Set iff ``mode == RELAY``.
    relay_event: RelayTransfer | None = None

    @property
    def pickup_owner(self) -> int:
        """Alias for the formal ``pickup_owner`` vocabulary (== primary_owner)."""
        return self.primary_owner

    @property
    def is_direct(self) -> bool:
        return (
            self.mode == ServiceMode.DIRECT
            and self.primary_owner == self.delivery_owner
            and self.relay_event is None
        )

    @property
    def is_relay(self) -> bool:
        return self.mode == ServiceMode.RELAY and self.relay_event is not None

    @property
    def pickup_drone(self) -> int:
        return self.primary_owner

    @property
    def delivery_drone(self) -> int:
        return self.delivery_owner


# ---------------------------------------------------------------------------
# Reconciliation helpers (single source of truth = routes + relays)
# ---------------------------------------------------------------------------


def relay_by_task(relays: Sequence[RelayTransfer]) -> dict[int, RelayTransfer]:
    """Index relay transfers by task id (at most one per task)."""
    return {relay.task_id: relay for relay in relays}


def pickup_drone_map(routes: Routes) -> dict[int, int]:
    """Map every task -> drone that performs its pickup."""
    return {
        visit: drone
        for drone, route in enumerate(routes)
        for visit in route
        if visit > 0
    }


def delivery_drone_map(routes: Routes) -> dict[int, int]:
    """Map every task -> drone that performs its delivery."""
    return {
        abs(visit): drone
        for drone, route in enumerate(routes)
        for visit in route
        if visit < 0
    }


def rebuild_services(
    routes: Routes,
    relays: Sequence[RelayTransfer] = (),
    swaps: Sequence[object] = (),
) -> dict[int, TaskServiceState]:
    """Rebuild the authoritative per-task service records from raw state.

    Given only ``routes + relays`` it reconstructs a consistent
    ``{task_id: TaskServiceState}``:

    - DIRECT: ``+i`` and ``-i`` on the same drone, no ``RelayTransfer``.
    - RELAY:  ``+i`` on ``relay.first_drone``, ``-i`` on
      ``relay.second_drone``, one ``RelayTransfer``.

    ``swaps`` is kept as a deprecated positional slot (always empty in the
    formal path) so legacy callers do not break.
    """
    pickups = pickup_drone_map(routes)
    deliveries = delivery_drone_map(routes)
    relays_by = relay_by_task(relays)

    services: dict[int, TaskServiceState] = {}
    for task_id in pickups:
        transfer = relays_by.get(task_id)
        if transfer is not None:
            services[task_id] = TaskServiceState(
                task_id=task_id,
                mode=ServiceMode.RELAY,
                primary_owner=transfer.first_drone,
                delivery_owner=transfer.second_drone,
                relay_event=transfer,
            )
            continue
        services[task_id] = TaskServiceState(
            task_id=task_id,
            mode=ServiceMode.DIRECT,
            primary_owner=pickups.get(task_id, -1),
            delivery_owner=deliveries.get(task_id, -1),
        )
    return services


def service_mode_counts(
    services: Mapping[int, TaskServiceState],
) -> dict[str, int]:
    """DIRECT / RELAY counts for diagnostics (never objective)."""
    counts = {"DIRECT": 0, "RELAY": 0}
    for state in services.values():
        counts[state.mode.name] = counts.get(state.mode.name, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Route completeness helpers (needed by repair)
# ---------------------------------------------------------------------------


def route_is_complete(routes: Routes, drone: int) -> bool:
    """True if every visit on *drone* forms a complete same-drone pair.

    A route that contains a relay or swap split visit (pickup without its
    delivery, or a received delivery without its pickup) is *not* complete
    and cannot be scored by the per-route ``RouteEvaluator``.
    """
    counts = Counter(abs(visit) for visit in routes[drone])
    return all(count == 2 for count in counts.values())


def complete_route_indices(routes: Routes) -> list[int]:
    """Indices of routes whose every task is a complete same-drone pair."""
    return [d for d in range(len(routes)) if route_is_complete(routes, d)]


def compute_pickup_times(
    problem: Problem, routes: Routes
) -> dict[int, float]:
    """Pickup times via route traversal that tolerates relay/swap splits.

    Used only for pickup-slack *guidance* and diagnostics — never for the
    formal ``Score`` (relay/swap timing is decided by the DAG evaluators).
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


def pickup_slacks(
    problem: Problem, routes: Routes
) -> dict[int, float]:
    """Canonical pickup slack per task (negative = pickup already too late)."""
    times = compute_pickup_times(problem, routes)
    return {
        task_id: _canonical_pickup_slack(problem, task_id, pickup_t)
        for task_id, pickup_t in times.items()
    }
