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
class Score:
    """Strict two-layer lexicographic objective used by every solver.

    Formal order: ``(late_count, distance_km)``.
    :attr:`total_lateness_min` is a **diagnostic only** — it is never
    consulted by ``__lt__``, ``__le__``, ``__gt__``, ``__ge__``, or
    any acceptance / regret / ranking decision.
    """

    late_count: int
    total_lateness_min: float = 0.0
    distance_km: float = 0.0

    # -- comparison (strict two-layer lex) ---------------------------------
    def _cmp(self) -> tuple[int, float]:
        return (self.late_count, self.distance_km)

    def __lt__(self, other: "Score") -> bool:
        if not isinstance(other, Score):
            return NotImplemented
        return self._cmp() < other._cmp()

    def __le__(self, other: "Score") -> bool:
        if not isinstance(other, Score):
            return NotImplemented
        return self._cmp() <= other._cmp()

    def __gt__(self, other: "Score") -> bool:
        if not isinstance(other, Score):
            return NotImplemented
        return self._cmp() > other._cmp()

    def __ge__(self, other: "Score") -> bool:
        if not isinstance(other, Score):
            return NotImplemented
        return self._cmp() >= other._cmp()

    # -- arithmetic (preserves all fields for diagnostics) -----------------
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
    _task_by_id: Mapping[int, Task] = field(init=False, repr=False, compare=False)
    _visit_to_node: Mapping[int, int] = field(init=False, repr=False, compare=False)
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

    def task(self, task_id: int) -> Task:
        return self._task_by_id[task_id]

    def point_for_visit(self, visit: int) -> Point:
        task = self.task(abs(visit))
        return task.pickup if visit > 0 else task.delivery

    def distance(self, from_visit: int | None, to_visit: int) -> float:
        from_node = 0 if from_visit is None else self._visit_to_node[from_visit]
        return self._distances[from_node][self._visit_to_node[to_visit]]

    def direct_completion_min(self, task_id: int) -> float:
        return (
            self.distance(None, task_id) + self.distance(task_id, -task_id)
        ) / self.speed_km_per_min


@dataclass(frozen=True, slots=True)
class SolutionEvaluation:
    """Metrics recomputed from complete routes by the independent validator.

    ``total_lateness_min`` and ``max_lateness_min`` are descriptive
    diagnostics: they never participate in the lexicographic ``Score`` order.
    """

    valid: bool
    score: Score
    delivery_times_min: Mapping[int, float]
    total_lateness_min: float
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
