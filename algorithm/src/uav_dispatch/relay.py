"""Immutable event model for buffered relay handoffs.

The legacy solver deliberately keeps its compact signed-integer routes.  This
module provides a separate representation so relay events can span drones
without changing the meaning of any existing route.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import isfinite
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from .model import Point, Problem, Task


class EventType(str, Enum):
    """The four custody-changing events supported by a relay plan."""

    PICKUP = "pickup"
    HANDOFF_DROP = "handoff_drop"
    HANDOFF_PICK = "handoff_pick"
    DELIVERY = "delivery"


class TaskCountSemantics(str, Enum):
    """How a relay task consumes each drone's task quota."""

    STRICT_TOUCH = "strict-touch"
    PRIMARY_OWNER = "primary-owner"


@dataclass(frozen=True, slots=True)
class Event:
    """One task event on one drone route."""

    task_id: int
    event_type: EventType
    hub_id: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.task_id, bool)
            or not isinstance(self.task_id, int)
            or self.task_id <= 0
        ):
            raise ValueError("任务编号必须为正整数")
        event_type = EventType(self.event_type)
        object.__setattr__(self, "event_type", event_type)
        is_handoff = event_type in {
            EventType.HANDOFF_DROP,
            EventType.HANDOFF_PICK,
        }
        if is_handoff:
            if not isinstance(self.hub_id, str) or not self.hub_id.strip():
                raise ValueError("交接事件必须指定交接点")
        if not is_handoff and self.hub_id is not None:
            raise ValueError("普通取送事件不能指定交接点")


@dataclass(frozen=True, slots=True)
class Hub:
    """A fixed location where a package may be buffered between drones."""

    id: str
    point: Point

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("交接点编号不能为空")
        if not isinstance(self.point, Point):
            raise ValueError("交接点坐标必须使用 Point")


@dataclass(frozen=True, slots=True)
class RelayProblem:
    """Relay-specific constraints wrapped around an unchanged base problem."""

    base_problem: Problem
    hubs: tuple[Hub, ...] = ()
    handoff_service_min: float = 0.0
    task_count_semantics: TaskCountSemantics = TaskCountSemantics.STRICT_TOUCH
    max_handoffs_per_task: int = 1
    _hub_by_id: Mapping[str, Hub] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.base_problem, Problem):
            raise ValueError("base_problem 必须是 Problem")
        hubs = tuple(self.hubs)
        if any(not isinstance(hub, Hub) for hub in hubs):
            raise ValueError("hubs 必须只包含 Hub")
        object.__setattr__(self, "hubs", hubs)
        semantics = TaskCountSemantics(self.task_count_semantics)
        object.__setattr__(self, "task_count_semantics", semantics)
        if not isfinite(self.handoff_service_min) or self.handoff_service_min < 0:
            raise ValueError("交接操作时长必须为有限非负数")
        if self.max_handoffs_per_task not in (0, 1):
            raise ValueError("首版每个任务最多允许一次交接")
        hub_by_id = {hub.id: hub for hub in hubs}
        if len(hub_by_id) != len(hubs):
            raise ValueError("交接点编号必须唯一")
        object.__setattr__(self, "_hub_by_id", MappingProxyType(hub_by_id))

    @property
    def semantics_extension(self) -> bool:
        """Whether task counting intentionally extends the conservative wording."""

        return self.task_count_semantics is TaskCountSemantics.PRIMARY_OWNER

    @property
    def task_ids(self) -> tuple[int, ...]:
        return self.base_problem.task_ids

    def task(self, task_id: int) -> Task:
        return self.base_problem.task(task_id)

    def hub(self, hub_id: str) -> Hub:
        return self._hub_by_id[hub_id]

    def point_for_event(self, event: Event) -> Point:
        if event.event_type is EventType.PICKUP:
            return self.task(event.task_id).pickup
        if event.event_type is EventType.DELIVERY:
            return self.task(event.task_id).delivery
        if event.hub_id is None:
            raise ValueError("交接事件必须指定交接点")
        return self.hub(event.hub_id).point

    def distance(self, from_event: Event | None, to_event: Event) -> float:
        """Use the base matrix exactly unless a leg touches a relay hub."""

        direct_types = {EventType.PICKUP, EventType.DELIVERY}
        if to_event.event_type in direct_types and (
            from_event is None or from_event.event_type in direct_types
        ):
            from_visit = None if from_event is None else _signed_visit(from_event)
            return self.base_problem.distance(from_visit, _signed_visit(to_event))
        from_point = (
            self.base_problem.depot
            if from_event is None
            else self.point_for_event(from_event)
        )
        return from_point.distance_to(self.point_for_event(to_event))


def _signed_visit(event: Event) -> int:
    if event.event_type is EventType.PICKUP:
        return event.task_id
    if event.event_type is EventType.DELIVERY:
        return -event.task_id
    raise ValueError("交接事件没有 legacy signed visit 表示")


RelayRoute = tuple[Event, ...]
RelayRoutes = tuple[RelayRoute, ...]


@dataclass(frozen=True, slots=True)
class RelayPlan:
    """A fleet-wide collection of event routes."""

    routes: RelayRoutes

    def __init__(self, routes: Iterable[Sequence[Event]]) -> None:
        materialized = tuple(tuple(route) for route in routes)
        if any(
            not isinstance(event, Event)
            for route in materialized
            for event in route
        ):
            raise ValueError("接力路线必须只包含 Event")
        object.__setattr__(self, "routes", materialized)

    @classmethod
    def from_signed_routes(
        cls, routes: Iterable[Sequence[int]]
    ) -> "RelayPlan":
        """Losslessly lift legacy ``+pickup/-delivery`` routes."""

        return cls(
            tuple(
                tuple(_event_from_signed_visit(visit) for visit in route)
                for route in routes
            )
        )

    def to_signed_routes(self) -> tuple[tuple[int, ...], ...]:
        """Lower a direct-only plan back to the legacy representation."""

        routes: list[tuple[int, ...]] = []
        for route in self.routes:
            signed: list[int] = []
            for event in route:
                if event.event_type is EventType.PICKUP:
                    signed.append(event.task_id)
                elif event.event_type is EventType.DELIVERY:
                    signed.append(-event.task_id)
                else:
                    raise ValueError(
                        "包含交接事件的方案不能转换为 signed 路线"
                    )
            routes.append(tuple(signed))
        return tuple(routes)


def _event_from_signed_visit(visit: int) -> Event:
    if isinstance(visit, bool) or not isinstance(visit, int) or visit == 0:
        raise ValueError("signed visit 必须是非零整数")
    return Event(
        abs(visit),
        EventType.PICKUP if visit > 0 else EventType.DELIVERY,
    )
