"""Core data model for the open pickup-and-delivery problem."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import hypot, isfinite
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Point:
    """A point in the task coordinate system, measured in kilometres."""

    x: float
    y: float

    def __post_init__(self) -> None:
        if not isfinite(self.x) or not isfinite(self.y):
            raise ValueError("坐标必须是有限数值")

    def distance_to(self, other: "Point") -> float:
        return hypot(self.x - other.x, self.y - other.y)


@dataclass(frozen=True, slots=True)
class Task:
    """One indivisible pickup-and-delivery request."""

    id: int
    pickup: Point
    delivery: Point
    deadline_min: float

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("任务编号必须为正整数")
        if not isfinite(self.deadline_min) or self.deadline_min < 0:
            raise ValueError("最晚送达时间不能为负")


@dataclass(frozen=True, slots=True)
class RelayStation:
    """A fixed relay station generated before the ALNS search starts."""

    id: int
    x: float
    y: float

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("中继站编号必须为正整数")
        if not isfinite(self.x) or not isfinite(self.y):
            raise ValueError("中继站坐标必须是有限数值")

    @property
    def point(self) -> Point:
        return Point(self.x, self.y)


@dataclass(frozen=True, slots=True)
class TransportLeg:
    """One immutable transport operation from the fixed leg registry.

    A DIRECT task has no registry entry: its visits stay ``+task_id``
    (pickup) and ``-task_id`` (delivery).  A RELAY task owns exactly two
    legs: ``RELAY_IN`` (pickup -> relay) and ``RELAY_OUT`` (relay ->
    delivery).  ``+leg_id`` is the leg start event and ``-leg_id`` is the
    leg end event, so the signed-route load semantics (+1 / -1) is kept.
    """

    id: int
    task_id: int
    kind: str  # "RELAY_IN" | "RELAY_OUT"
    relay_id: int

    def __post_init__(self) -> None:
        if self.id <= 0:
            raise ValueError("中转腿编号必须为正整数")
        if self.kind not in ("RELAY_IN", "RELAY_OUT"):
            raise ValueError("中转腿类型必须是 RELAY_IN 或 RELAY_OUT")
        if self.relay_id <= 0:
            raise ValueError("中转腿必须引用有效中继站")


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """The single current service plan of one original task.

    Exactly one of DIRECT (no legs) or RELAY(r) with one inbound and one
    outbound leg.  Multiple relays or both modes at once are forbidden.
    """

    task_id: int
    mode: str  # "DIRECT" | "RELAY"
    relay_id: int | None
    leg_ids: tuple[int, ...]

    def __post_init__(self) -> None:
        if self.mode not in ("DIRECT", "RELAY"):
            raise ValueError("服务方案必须是 DIRECT 或 RELAY")
        if self.mode == "DIRECT":
            if self.relay_id is not None or self.leg_ids:
                raise ValueError("DIRECT 方案不能携带中转信息")
        elif self.relay_id is None or len(self.leg_ids) != 2:
            raise ValueError("RELAY 方案必须恰好包含一条入库腿和一条出库腿")


@dataclass(frozen=True, order=True, slots=True)
class Score:
    """The strict lexicographic objective used by every solver."""

    late_count: int
    total_lateness_min: float
    distance_km: float

    def __add__(self, other: "Score") -> "Score":
        return Score(
            self.late_count + other.late_count,
            self.total_lateness_min + other.total_lateness_min,
            self.distance_km + other.distance_km,
        )

    def __sub__(self, other: "Score") -> "Score":
        return Score(
            self.late_count - other.late_count,
            self.total_lateness_min - other.total_lateness_min,
            self.distance_km - other.distance_km,
        )


@dataclass(frozen=True, slots=True)
class Problem:
    """A single- or multi-drone open PDPTW instance.

    Visit identifiers are intentionally compact: ``task_id`` is a pickup and
    ``-task_id`` is the matching delivery.  The supplied speed of 15 m/s is
    represented as 0.9 km/min so that it matches the spreadsheet deadlines.
    """

    tasks: tuple[Task, ...]
    drone_count: int = 1
    max_tasks_per_drone: int = 25
    capacity: int = 2
    speed_km_per_min: float = 0.9
    depot: Point = Point(0.0, 0.0)
    distance_matrix_km: tuple[tuple[float, ...], ...] | None = field(
        default=None, repr=False, compare=False
    )
    relay_stations: tuple[RelayStation, ...] = ()
    leg_registry: Mapping[int, TransportLeg] | None = field(
        default=None, repr=False, compare=False
    )
    task_relay_candidates: Mapping[int, tuple[int, ...]] | None = field(
        default=None, repr=False, compare=False
    )
    max_relay_hops: int = 1
    _task_by_id: Mapping[int, Task] = field(init=False, repr=False, compare=False)
    _visit_to_node: Mapping[int, int] = field(init=False, repr=False, compare=False)
    _leg_nodes: Mapping[int, tuple[int, int]] = field(
        init=False, repr=False, compare=False
    )
    _legs_by_task: Mapping[int, tuple[TransportLeg, ...]] = field(
        init=False, repr=False, compare=False
    )
    _visit_node_map: Mapping[int, int] = field(
        init=False, repr=False, compare=False
    )
    _points: tuple[Point, ...] = field(init=False, repr=False, compare=False)
    _distances: tuple[tuple[float, ...], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        tasks = tuple(sorted(self.tasks, key=lambda task: task.id))
        object.__setattr__(self, "tasks", tasks)
        if not tasks:
            raise ValueError("任务池不能为空")
        if self.drone_count <= 0:
            raise ValueError("无人机数量必须为正整数")
        if self.max_tasks_per_drone <= 0:
            raise ValueError("单机任务上限必须为正整数")
        if self.capacity <= 0:
            raise ValueError("载荷上限必须为正整数")
        if not isfinite(self.speed_km_per_min) or self.speed_km_per_min <= 0:
            raise ValueError("飞行速度必须为正数")
        if len(tasks) > self.drone_count * self.max_tasks_per_drone:
            raise ValueError("无人机总任务槽位不足以覆盖全部任务")

        task_by_id = {task.id: task for task in tasks}
        if len(task_by_id) != len(tasks):
            raise ValueError("任务编号必须唯一")
        object.__setattr__(self, "_task_by_id", MappingProxyType(task_by_id))

        points = [self.depot]
        visit_to_node: dict[int, int] = {}
        for task in tasks:
            visit_to_node[task.id] = len(points)
            points.append(task.pickup)
            visit_to_node[-task.id] = len(points)
            points.append(task.delivery)
        object.__setattr__(self, "_visit_to_node", MappingProxyType(visit_to_node))

        if self.max_relay_hops != 1:
            raise ValueError("第一版仅支持单次中转（MAX_RELAY_HOPS = 1）")
        stations = tuple(sorted(self.relay_stations, key=lambda item: item.id))
        relay_to_node: dict[int, int] = {}
        for index, station in enumerate(stations):
            if station.id in relay_to_node:
                raise ValueError("中继站编号必须唯一")
            relay_to_node[station.id] = len(points)
            points.append(station.point)
        object.__setattr__(self, "relay_stations", stations)

        legs = dict(self.leg_registry or {})
        object.__setattr__(self, "leg_registry", MappingProxyType(legs))
        in_legs_by_task: dict[int, dict[int, TransportLeg]] = {}
        out_legs_by_task: dict[int, dict[int, TransportLeg]] = {}
        for leg_id, leg in legs.items():
            if leg.id != leg_id:
                raise ValueError("中转腿注册表的键必须等于腿编号")
            if leg_id in task_by_id:
                raise ValueError("中转腿编号不能与任务编号冲突")
            if leg.task_id not in task_by_id:
                raise ValueError("中转腿引用未知任务")
            if leg.relay_id not in relay_to_node:
                raise ValueError("中转腿引用未知中继站")
            bucket = (
                in_legs_by_task.setdefault(leg.task_id, {})
                if leg.kind == "RELAY_IN"
                else out_legs_by_task.setdefault(leg.task_id, {})
            )
            if leg.relay_id in bucket:
                raise ValueError(
                    f"任务 {leg.task_id} 对中继站 {leg.relay_id} "
                    f"存在重复的 {leg.kind} 腿"
                )
            bucket[leg.relay_id] = leg
        for task_id, in_legs in in_legs_by_task.items():
            out_legs = out_legs_by_task.get(task_id, {})
            if set(in_legs) != set(out_legs):
                raise ValueError(
                    f"任务 {task_id} 的入库腿与出库腿必须按中继站成对出现"
                )
        for task_id, out_legs in out_legs_by_task.items():
            if task_id not in in_legs_by_task or not out_legs:
                raise ValueError(f"任务 {task_id} 的出库腿缺少入库腿")
        legs_by_task: dict[int, tuple[TransportLeg, ...]] = {}
        for task_id in task_by_id:
            legs_by_task[task_id] = tuple(
                leg
                for leg in legs.values()
                if leg.task_id == task_id
            )
        object.__setattr__(self, "_legs_by_task", legs_by_task)

        leg_nodes: dict[int, tuple[int, int]] = {}
        for leg_id, leg in legs.items():
            if leg.kind == "RELAY_IN":
                start_node = visit_to_node[leg.task_id]
                end_node = relay_to_node[leg.relay_id]
            else:
                start_node = relay_to_node[leg.relay_id]
                end_node = visit_to_node[-leg.task_id]
            pair = (start_node, end_node)
            leg_nodes[leg_id] = pair
            leg_nodes[-leg_id] = pair
        object.__setattr__(
            self, "_leg_nodes", MappingProxyType(leg_nodes)
        )
        visit_node_map = dict(visit_to_node)
        for leg_id, (start_node, end_node) in leg_nodes.items():
            if leg_id > 0:
                visit_node_map[leg_id] = start_node
                visit_node_map[-leg_id] = end_node
        object.__setattr__(self, "_visit_node_map", visit_node_map)

        candidates = dict(self.task_relay_candidates or {})
        for task_id, relay_ids in candidates.items():
            if task_id not in task_by_id:
                raise ValueError("中继候选引用未知任务")
            for relay_id in relay_ids:
                if relay_id not in relay_to_node:
                    raise ValueError("中继候选引用未知中继站")
        object.__setattr__(
            self,
            "task_relay_candidates",
            MappingProxyType(
                {key: tuple(value) for key, value in candidates.items()}
            ),
        )
        object.__setattr__(self, "_points", tuple(points))
        if self.distance_matrix_km is None:
            distances = tuple(
                tuple(point.distance_to(other) for other in points) for point in points
            )
        else:
            distances = tuple(
                tuple(float(value) for value in row)
                for row in self.distance_matrix_km
            )
            expected_size = len(points)
            if len(distances) != expected_size or any(
                len(row) != expected_size for row in distances
            ):
                raise ValueError(
                    f"距离矩阵必须为 {expected_size}x{expected_size}"
                )
            if any(value < 0 for row in distances for value in row):
                raise ValueError("距离矩阵不能包含负数")
            if any(not isfinite(value) for row in distances for value in row):
                raise ValueError("距离矩阵必须只包含有限数值")
            if any(abs(distances[index][index]) > 1e-12 for index in range(expected_size)):
                raise ValueError("距离矩阵对角线必须为 0")
        object.__setattr__(self, "_distances", distances)

    @property
    def task_ids(self) -> tuple[int, ...]:
        return tuple(task.id for task in self.tasks)

    @property
    def relay_ids(self) -> tuple[int, ...]:
        return tuple(station.id for station in self.relay_stations)

    @property
    def has_relays(self) -> bool:
        return bool(self.leg_registry)

    def task(self, task_id: int) -> Task:
        return self._task_by_id[task_id]

    def leg(self, leg_id: int) -> TransportLeg:
        return self.leg_registry[leg_id]

    def _node_for_visit(self, visit: int) -> int:
        return self._visit_node_map[visit]

    def point_for_visit(self, visit: int) -> Point:
        return self._points[self._node_for_visit(visit)]

    def distance(self, from_visit: int | None, to_visit: int) -> float:
        node_map = self._visit_node_map
        from_node = 0 if from_visit is None else node_map[from_visit]
        return self._distances[from_node][node_map[to_visit]]

    def direct_completion_min(self, task_id: int) -> float:
        return (
            self.distance(None, task_id) + self.distance(task_id, -task_id)
        ) / self.speed_km_per_min


@dataclass(frozen=True, slots=True)
class SolutionEvaluation:
    """Metrics recomputed from complete routes by the independent validator."""

    valid: bool
    score: Score
    delivery_times_min: Mapping[int, float]
    max_lateness_min: float
    violations: tuple[str, ...]
    route_distances_km: tuple[float, ...]
    route_completion_times_min: tuple[float, ...]

    @property
    def on_time_rate(self) -> float:
        delivered = len(self.delivery_times_min)
        if delivered == 0:
            return 0.0
        return (delivered - self.score.late_count) / delivered

    @property
    def max_route_distance_km(self) -> float:
        return max(self.route_distances_km, default=0.0)

    @property
    def max_route_completion_min(self) -> float:
        return max(self.route_completion_times_min, default=0.0)
