"""Explicit data structures for the Swap-only cooperative model.

The Swap-only model lets two different drones exchange one parcel each at a
safe meeting point::

    UAV A:  P_i -> H -> D_j
    UAV B:  P_j -> H -> D_i

A swap is never encoded with magic visit integers inside a route; it lives in
an explicit :class:`SwapEvent` attached to a :class:`SwapSolution`.

Route encoding
--------------
A :class:`~uav_dispatch.search.Route` is a ``tuple[int, ...]`` of visits where a
positive ``+task_id`` is the pickup and ``-task_id`` the delivery.  In the
Swap-only model a route may legally contain ``+i`` without ``-i`` (drone A
hands parcel ``i`` over at the meeting point) and ``-i`` without ``+i`` (drone
B receives parcel ``i`` at the meeting point).  Globally every task must still
have exactly one pickup and one delivery.

Meeting nodes
-------------
Every pickup node and every delivery node of the problem is a legal rendezvous
candidate, i.e. ``H = P | D``.  A meeting node is identified by a visit
integer: ``+task_id`` is the pickup point of that task and ``-task_id`` its
delivery point.  No artificial coordinates are ever generated.

Gap positions
-------------
``position_a``/``position_b`` are gap indices into the drone's route.  A swap
inserted at gap ``g`` takes place after the visit at index ``g - 1`` and
before the visit at index ``g``.  For example the route ``(+1, +2, -2, -1)``
has gaps ``0, 1, 2, 3, 4``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .search import Route, Routes


@dataclass(frozen=True, slots=True)
class SwapEvent:
    """One bilateral one-for-one parcel exchange between two drones.

    ``drone_a`` holds ``task_a_to_b`` before the exchange and hands it to
    ``drone_b`` while receiving ``task_b_to_a`` in return.  Both drones must
    already carry their parcel, and the number of carried parcels on each
    drone is unchanged by the exchange.
    """

    id: int
    drone_a: int
    drone_b: int
    task_a_to_b: int
    task_b_to_a: int
    meeting_node: int
    position_a: int
    position_b: int

    def __post_init__(self) -> None:
        if self.id < 0:
            raise ValueError("交换编号不能为负")
        if self.drone_a < 0 or self.drone_b < 0:
            raise ValueError("无人机编号不能为负")
        if self.drone_a == self.drone_b:
            raise ValueError("交换必须发生在两架不同无人机之间")
        if self.task_a_to_b <= 0 or self.task_b_to_a <= 0:
            raise ValueError("交换任务编号必须为正整数")
        if self.task_a_to_b == self.task_b_to_a:
            raise ValueError("两架无人机不能交换同一个任务")
        if self.meeting_node == 0:
            raise ValueError("汇合点不能是起点")
        if self.position_a < 0 or self.position_b < 0:
            raise ValueError("交换插入位置不能为负")


@dataclass(frozen=True, slots=True)
class SwapSolution:
    """A complete Swap-only solution: per-drone routes plus explicit swaps."""

    routes: Routes
    swaps: tuple[SwapEvent, ...] = ()

    def __post_init__(self) -> None:
        if not self.routes:
            raise ValueError("Swap 解必须至少包含一条路线")
        seen_ids: set[int] = set()
        for event in self.swaps:
            if event.id in seen_ids:
                raise ValueError(f"交换编号 {event.id} 重复")
            seen_ids.add(event.id)

    @property
    def swap_count(self) -> int:
        return len(self.swaps)


def swap_solution_from_routes(routes: Routes) -> SwapSolution:
    """Wrap plain same-carrier routes as a Swap-only solution with no swaps.

    This is the canonical embedding of a baseline (A2) solution into the
    Swap-only model.  When ``swap_count == 0`` the feasible solution space of
    the Swap-only model contains every plain PDPTW solution.
    """

    return SwapSolution(routes=tuple(tuple(route) for route in routes))


def meeting_node_label(meeting_node: int) -> str:
    """Human-readable label for a meeting visit, e.g. ``P37`` or ``D143``."""

    task_id = abs(meeting_node)
    return f"P{task_id}" if meeting_node > 0 else f"D{task_id}"


def gaps_for_route(route: Route) -> tuple[int, ...]:
    """Return the valid gap indices for a route (0..len(route))."""

    return tuple(range(len(route) + 1))
