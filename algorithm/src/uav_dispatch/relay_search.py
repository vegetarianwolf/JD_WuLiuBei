"""Candidate generation and neighbourhood helpers for relay search."""

from __future__ import annotations

from collections import Counter
from math import isfinite
from typing import Callable, Iterable, TypeVar

from .model import Point, Problem
from .relay import Event, EventType, Hub, RelayPlan, RelayProblem


def generate_flow_hubs(problem: Problem, count: int) -> tuple[Hub, ...]:
    """Build deterministic static hubs from pickup-delivery corridor midpoints.

    The returned points are fixed before Relay-ALNS starts; choosing among them
    is a discrete routing decision rather than a continuous rendezvous problem.
    """

    if count <= 0:
        raise ValueError("交接点数量必须为正整数")
    midpoints = tuple(
        Point(
            (task.pickup.x + task.delivery.x) / 2,
            (task.pickup.y + task.delivery.y) / 2,
        )
        for task in problem.tasks
    )
    target = min(count, len(midpoints))
    centres = [midpoints[0]]
    while len(centres) < target:
        centres.append(
            max(
                midpoints,
                key=lambda point: (
                    min(point.distance_to(centre) for centre in centres),
                    -point.x,
                    -point.y,
                ),
            )
        )

    for _ in range(32):
        groups: list[list[Point]] = [[] for _ in centres]
        for point in midpoints:
            index = min(
                range(len(centres)),
                key=lambda candidate: (
                    point.distance_to(centres[candidate]),
                    candidate,
                ),
            )
            groups[index].append(point)
        updated = [
            (
                Point(
                    sum(point.x for point in group) / len(group),
                    sum(point.y for point in group) / len(group),
                )
                if group
                else centres[index]
            )
            for index, group in enumerate(groups)
        ]
        if all(
            old.distance_to(new) <= 1e-12
            for old, new in zip(centres, updated, strict=True)
        ):
            centres = updated
            break
        centres = updated

    if any(not isfinite(value) for point in centres for value in (point.x, point.y)):
        raise RuntimeError("交接点生成得到非有限坐标")
    ordered = sorted(centres, key=lambda point: (point.x, point.y))
    return tuple(
        Hub(f"flow-{index}", point)
        for index, point in enumerate(ordered, start=1)
    )


def direct_task_ids(plan: RelayPlan) -> tuple[int, ...]:
    """Return tasks represented only by one pickup and one delivery event."""

    events: dict[int, Counter[EventType]] = {}
    for route in plan.routes:
        for event in route:
            events.setdefault(event.task_id, Counter())[event.event_type] += 1
    return tuple(
        sorted(
            task_id
            for task_id, counts in events.items()
            if counts
            == Counter({EventType.PICKUP: 1, EventType.DELIVERY: 1})
        )
    )


def relay_task_ids(plan: RelayPlan) -> tuple[int, ...]:
    """Return tasks that contain exactly one complete relay event chain."""

    events: dict[int, Counter[EventType]] = {}
    for route in plan.routes:
        for event in route:
            events.setdefault(event.task_id, Counter())[event.event_type] += 1
    expected = Counter({event_type: 1 for event_type in EventType})
    return tuple(
        sorted(task_id for task_id, counts in events.items() if counts == expected)
    )


def _capacity_valid(route: Iterable[Event], capacity: int) -> bool:
    load = 0
    for event in route:
        if event.event_type in {EventType.PICKUP, EventType.HANDOFF_PICK}:
            load += 1
        else:
            load -= 1
        if load < 0 or load > capacity:
            return False
    return load == 0


def _route_distance(problem: RelayProblem, route: Iterable[Event]) -> float:
    previous: Event | None = None
    distance = 0.0
    for event in route:
        distance += problem.distance(previous, event)
        previous = event
    return distance


_Option = TypeVar("_Option")


def _diverse_best(
    options: Iterable[_Option],
    limit: int,
    *,
    distance_key: Callable[[_Option], object],
    early_key: Callable[[_Option], object],
) -> tuple[_Option, ...]:
    materialized = tuple(options)
    if len(materialized) <= limit:
        return materialized
    selected: list[_Option] = []
    seen: set[_Option] = set()
    for ordered, quota in (
        (sorted(materialized, key=distance_key), max(1, limit // 2)),
        (sorted(materialized, key=early_key), limit),
    ):
        added = 0
        for option in ordered:
            if option not in seen:
                selected.append(option)
                seen.add(option)
                added += 1
            if len(selected) >= limit or added >= quota:
                break
        if len(selected) >= limit:
            break
    return tuple(selected)


def split_task_candidates(
    problem: RelayProblem,
    plan: RelayPlan,
    task_id: int,
    *,
    candidate_limit: int | None = None,
) -> Iterable[RelayPlan]:
    """Yield capacity-feasible one-handoff placements for a direct task."""

    pickup_node: tuple[int, int] | None = None
    delivery_node: tuple[int, int] | None = None
    for route_index, route in enumerate(plan.routes):
        for event_index, event in enumerate(route):
            if event.task_id != task_id:
                continue
            if event.event_type is EventType.PICKUP:
                pickup_node = (route_index, event_index)
            elif event.event_type is EventType.DELIVERY:
                delivery_node = (route_index, event_index)
            else:
                return
    if (
        pickup_node is None
        or delivery_node is None
        or pickup_node[0] != delivery_node[0]
    ):
        return

    sender = pickup_node[0]
    sender_without_delivery = tuple(
        event
        for event in plan.routes[sender]
        if not (
            event.task_id == task_id
            and event.event_type is EventType.DELIVERY
        )
    )
    pickup_position = next(
        index
        for index, event in enumerate(sender_without_delivery)
        if event.task_id == task_id and event.event_type is EventType.PICKUP
    )
    limit = candidate_limit
    if limit is not None and limit <= 0:
        raise ValueError("接力候选上限必须为正整数")
    combined: list[tuple[float, int, int, int, RelayPlan]] = []
    for hub in problem.hubs:
        drop = Event(task_id, EventType.HANDOFF_DROP, hub.id)
        pick = Event(task_id, EventType.HANDOFF_PICK, hub.id)
        delivery = Event(task_id, EventType.DELIVERY)
        sender_options: list[tuple[float, int, tuple[Event, ...]]] = []
        for drop_position in range(pickup_position + 1, len(sender_without_delivery) + 1):
            sender_route = (
                sender_without_delivery[:drop_position]
                + (drop,)
                + sender_without_delivery[drop_position:]
            )
            if not _capacity_valid(sender_route, problem.base_problem.capacity):
                continue
            sender_options.append(
                (_route_distance(problem, sender_route), drop_position, sender_route)
            )
        if limit is not None:
            sender_options = list(
                _diverse_best(
                    sender_options,
                    limit,
                    distance_key=lambda option: (option[0], option[1]),
                    early_key=lambda option: (option[1], option[0]),
                )
            )
        for receiver, receiver_route in enumerate(plan.routes):
            if receiver == sender:
                continue
            receiver_options: list[
                tuple[float, int, int, tuple[Event, ...]]
            ] = []
            for pick_position in range(len(receiver_route) + 1):
                with_pick = (
                    receiver_route[:pick_position]
                    + (pick,)
                    + receiver_route[pick_position:]
                )
                for delivery_position in range(
                    pick_position + 1, len(receiver_route) + 2
                ):
                    new_receiver_route = (
                        with_pick[:delivery_position]
                        + (delivery,)
                        + with_pick[delivery_position:]
                    )
                    if not _capacity_valid(
                        new_receiver_route, problem.base_problem.capacity
                    ):
                        continue
                    receiver_options.append(
                        (
                            _route_distance(problem, new_receiver_route),
                            delivery_position,
                            pick_position,
                            new_receiver_route,
                        )
                    )
            if limit is not None:
                receiver_options = list(
                    _diverse_best(
                        receiver_options,
                        limit,
                        distance_key=lambda option: (
                            option[0],
                            option[1],
                            option[2],
                        ),
                        early_key=lambda option: (
                            option[1],
                            option[2],
                            option[0],
                        ),
                    )
                )
            for sender_distance, drop_position, sender_route in sender_options:
                for (
                    receiver_distance,
                    delivery_position,
                    pick_position,
                    new_receiver_route,
                ) in receiver_options:
                    routes = list(plan.routes)
                    routes[sender] = sender_route
                    routes[receiver] = new_receiver_route
                    combined.append(
                        (
                            sender_distance + receiver_distance,
                            delivery_position,
                            drop_position,
                            receiver,
                            RelayPlan(routes),
                        )
                    )
    if limit is not None:
        combined = list(
            _diverse_best(
                combined,
                limit,
                distance_key=lambda option: option[:4],
                early_key=lambda option: (
                    option[1],
                    option[2],
                    option[0],
                    option[3],
                ),
            )
        )
    for _, _, _, _, candidate in combined:
        yield candidate


def merge_task_candidates(
    problem: RelayProblem,
    plan: RelayPlan,
    task_id: int,
    *,
    candidate_limit: int | None = None,
) -> Iterable[RelayPlan]:
    """Yield direct placements that remove one complete relay chain."""

    if candidate_limit is not None and candidate_limit <= 0:
        raise ValueError("接力候选上限必须为正整数")
    sender: int | None = None
    for route_index, route in enumerate(plan.routes):
        if any(
            event.task_id == task_id and event.event_type is EventType.PICKUP
            for event in route
        ):
            sender = route_index
            break
    if sender is None or task_id not in relay_task_ids(plan):
        return
    stripped = tuple(
        tuple(event for event in route if event.task_id != task_id)
        for route in plan.routes
    )
    owner_route = stripped[sender]
    pickup = Event(task_id, EventType.PICKUP)
    delivery = Event(task_id, EventType.DELIVERY)
    options: list[tuple[float, int, int, RelayPlan]] = []
    for pickup_position in range(len(owner_route) + 1):
        with_pickup = (
            owner_route[:pickup_position]
            + (pickup,)
            + owner_route[pickup_position:]
        )
        for delivery_position in range(pickup_position + 1, len(owner_route) + 2):
            direct_route = (
                with_pickup[:delivery_position]
                + (delivery,)
                + with_pickup[delivery_position:]
            )
            if not _capacity_valid(direct_route, problem.base_problem.capacity):
                continue
            routes = list(stripped)
            routes[sender] = direct_route
            options.append(
                (
                    _route_distance(problem, direct_route),
                    delivery_position,
                    pickup_position,
                    RelayPlan(routes),
                )
            )
    if candidate_limit is not None:
        options = list(
            _diverse_best(
                options,
                candidate_limit,
                distance_key=lambda option: option[:3],
                early_key=lambda option: (option[1], option[2], option[0]),
            )
        )
    for _, _, _, candidate in options:
        yield candidate


def change_hub_candidates(
    problem: RelayProblem,
    plan: RelayPlan,
    task_id: int,
) -> Iterable[RelayPlan]:
    """Yield plans that move one relay chain to another fixed hub."""

    if task_id not in relay_task_ids(plan):
        return
    current_hubs = {
        event.hub_id
        for route in plan.routes
        for event in route
        if event.task_id == task_id
        and event.event_type in {EventType.HANDOFF_DROP, EventType.HANDOFF_PICK}
    }
    current_hub = next(iter(current_hubs)) if len(current_hubs) == 1 else None
    for hub in problem.hubs:
        if hub.id == current_hub:
            continue
        routes = tuple(
            tuple(
                (
                    Event(event.task_id, event.event_type, hub.id)
                    if event.task_id == task_id
                    and event.event_type
                    in {EventType.HANDOFF_DROP, EventType.HANDOFF_PICK}
                    else event
                )
                for event in route
            )
            for route in plan.routes
        )
        yield RelayPlan(routes)


def change_receiver_candidates(
    problem: RelayProblem,
    plan: RelayPlan,
    task_id: int,
    *,
    candidate_limit: int | None = None,
) -> Iterable[RelayPlan]:
    """Yield placements of a relay task's downstream segment on another drone."""

    if candidate_limit is not None and candidate_limit <= 0:
        raise ValueError("接力候选上限必须为正整数")
    if task_id not in relay_task_ids(plan):
        return
    sender: int | None = None
    for route_index, route in enumerate(plan.routes):
        if any(
            event.task_id == task_id and event.event_type is EventType.PICKUP
            for event in route
        ):
            sender = route_index
            break
    if sender is None:
        return
    pick_event = next(
        event
        for route in plan.routes
        for event in route
        if event.task_id == task_id
        and event.event_type is EventType.HANDOFF_PICK
    )
    delivery_event = Event(task_id, EventType.DELIVERY)
    stripped = tuple(
        tuple(
            event
            for event in route
            if not (
                event.task_id == task_id
                and event.event_type
                in {EventType.HANDOFF_PICK, EventType.DELIVERY}
            )
        )
        for route in plan.routes
    )
    options: list[tuple[float, int, int, int, RelayPlan]] = []
    for receiver, receiver_route in enumerate(stripped):
        if receiver == sender:
            continue
        for pick_position in range(len(receiver_route) + 1):
            with_pick = (
                receiver_route[:pick_position]
                + (pick_event,)
                + receiver_route[pick_position:]
            )
            for delivery_position in range(
                pick_position + 1, len(receiver_route) + 2
            ):
                route = (
                    with_pick[:delivery_position]
                    + (delivery_event,)
                    + with_pick[delivery_position:]
                )
                if not _capacity_valid(route, problem.base_problem.capacity):
                    continue
                routes = list(stripped)
                routes[receiver] = route
                options.append(
                    (
                        _route_distance(problem, route),
                        delivery_position,
                        pick_position,
                        receiver,
                        RelayPlan(routes),
                    )
                )
    if candidate_limit is not None:
        options = list(
            _diverse_best(
                options,
                candidate_limit,
                distance_key=lambda option: option[:4],
                early_key=lambda option: (
                    option[1],
                    option[2],
                    option[0],
                    option[3],
                ),
            )
        )
    for _, _, _, _, candidate in options:
        yield candidate
