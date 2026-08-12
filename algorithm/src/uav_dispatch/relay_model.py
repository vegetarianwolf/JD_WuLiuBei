"""Explicit data structures for the Relay-aware cooperative model.

The Relay model lets a package be handed over asynchronously at a relay
station (a smart locker / micro-hub / UAV transfer cabinet / safe logistics
transfer pad) between two *different* drones::

    UAV A:  P_i -> RELAY_DROP(i, r)   (A continues immediately)
    UAV B:  ...  -> RELAY_PICK(i, r) -> D_i

DROP and PICK are two *distinct* events.  The first drone never waits for the
second; the station can temporarily store the package in between, so the two
timelines are only coupled by the precedence edge ``DROP -> PICK``.

Route encoding
--------------
A :class:`~uav_dispatch.search.Route` is a ``tuple[int, ...]`` of visits where a
positive ``+task_id`` is the pickup and ``-task_id`` the delivery.  Relay uses
the same signed routes as the Swap model (the whole ALNS machinery depends on
it): a relay task has ``+i`` on the first drone, ``-i`` on the second drone,
and exactly one :class:`RelayTransfer`.  A direct task has ``+i`` and ``-i`` on
the same drone and no transfer.

Task-count semantics
--------------------
``task_count(drone)`` counts only *positive pickup events* on that drone, so a
relay task still counts as exactly one task on its first (pickup-owning) drone
even though the second drone performs the final delivery.  This matches the
``owned_counts`` semantics of ``swap_validation``.

Stations
--------
Stations are an explicit modelling extension.  This version interprets them as
temporary-storage lockers with unlimited capacity, no queue, no congestion, no
charging and no activation cost, and a handling time of 0.  ``source_visit``
identifies which existing service node a station is placed on: ``> 0`` is the
pickup node of that task, ``< 0`` is the delivery node.  A task may never use
its own pickup or delivery node as its relay station.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .model import Point, Problem
from .search import Route, Routes

#: Numerical guard used to reject degenerate station placements.
RELAY_ENDPOINT_EPS = 1e-9

#: Default infrastructure budget (number of built stations).  This is an
#: ``infrastructure availability`` budget, not an optimised count.
NUM_RELAY_STATIONS = 4

#: Default station handling time in minutes (the problem ignores other
#: operation time, so the default is 0).
RELAY_HANDLING_TIME_MIN = 0.0


@dataclass(frozen=True, slots=True)
class RelayStation:
    """A fixed relay facility located at one existing service node.

    ``source_visit`` is the visit integer of the node the station is placed
    on: ``> 0`` is the pickup node of that task, ``< 0`` is the delivery node.
    """

    id: int
    point: Point
    source_visit: int

    def __post_init__(self) -> None:
        if self.id < 0:
            raise ValueError("中继站编号不能为负")
        if self.source_visit == 0:
            raise ValueError("中继站必须位于某个任务服务节点")

    @property
    def label(self) -> str:
        task_id = abs(self.source_visit)
        node = "P" if self.source_visit > 0 else "D"
        return f"RS{self.id}({node}{task_id})"


@dataclass(frozen=True, slots=True)
class RelayTransfer:
    """One asynchronous hand-over of a single package at one station.

    The first drone picks up ``task_id`` and drops it at the station at the
    ``drop_position`` gap; the second drone later picks it up there at the
    ``pick_position`` gap and delivers it.  The task still counts as one task
    on the first drone only.
    """

    id: int
    task_id: int
    station_id: int
    first_drone: int
    second_drone: int
    drop_position: int
    pick_position: int

    def __post_init__(self) -> None:
        if self.id < 0:
            raise ValueError("中继编号不能为负")
        if self.task_id <= 0:
            raise ValueError("中继任务编号必须为正整数")
        if self.station_id < 0:
            raise ValueError("中继站编号不能为负")
        if self.first_drone < 0 or self.second_drone < 0:
            raise ValueError("无人机编号不能为负")
        if self.first_drone == self.second_drone:
            raise ValueError("中继必须发生在两架不同无人机之间")
        if self.drop_position < 0 or self.pick_position < 0:
            raise ValueError("中继插入位置不能为负")


@dataclass(frozen=True, slots=True)
class RelaySolution:
    """A complete Relay solution: per-drone signed routes, explicit transfers
    and the fixed station set (frozen after Module 0)."""

    routes: Routes
    relays: tuple[RelayTransfer, ...] = ()
    stations: tuple[RelayStation, ...] = ()

    def __post_init__(self) -> None:
        if not self.routes:
            raise ValueError("Relay 解必须至少包含一条路线")
        seen_ids: set[int] = set()
        for relay in self.relays:
            if relay.id in seen_ids:
                raise ValueError(f"中继编号 {relay.id} 重复")
            seen_ids.add(relay.id)
        seen_stations: set[int] = set()
        for station in self.stations:
            if station.id in seen_stations:
                raise ValueError(f"中继站编号 {station.id} 重复")
            seen_stations.add(station.id)

    @property
    def relay_count(self) -> int:
        return len(self.relays)


def relay_solution_from_routes(
    routes: Sequence[Sequence[int]],
    stations: Sequence[RelayStation] = (),
) -> RelaySolution:
    """Wrap a plain same-drone solution as a zero-relay Relay solution."""

    return RelaySolution(
        tuple(tuple(route) for route in routes),
        relays=(),
        stations=tuple(stations),
    )


def gaps_for_route(route: Sequence[int]) -> tuple[int, ...]:
    """Return every legal gap index ``0..len(route)`` for a route."""

    return tuple(range(len(route) + 1))


def station_point(problem: Problem, station: RelayStation) -> Point:
    """Resolve a station's physical point from its source visit node."""

    task = problem.task(abs(station.source_visit))
    return task.pickup if station.source_visit > 0 else task.delivery


def station_for_id(solution: RelaySolution, station_id: int) -> RelayStation:
    for station in solution.stations:
        if station.id == station_id:
            return station
    raise KeyError(station_id)


def relays_by_task(
    relays: Sequence[RelayTransfer],
) -> dict[int, RelayTransfer]:
    """Index relay transfers by their task id (each task has at most one)."""

    return {relay.task_id: relay for relay in relays}
