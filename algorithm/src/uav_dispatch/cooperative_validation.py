"""Unified cooperative validator (formal DIRECT / RELAY path).

Validates a complete :class:`CooperativeSolution` — covering routes, relay
transfers, the relay points actually used, ownership assignments, and all
cross-cutting constraints (pickup / delivery completeness, precedence,
``Q`` capacity, ``K`` customer-pick semantics, deadline, ``drop -> pick``
async relay ordering, and TaskServiceState consistency).

Swap is not part of the formal path: ``solution.swaps`` is a deprecated
legacy field that must stay empty.  No candidate may enter the incumbent
without passing this validator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Sequence

from .model import Problem, Score
from .relay_model import RelaySolution, RelayStation, RelayTransfer
from .search import RouteEvaluator, Routes


# ---------------------------------------------------------------------------
# Unified solution container
# ---------------------------------------------------------------------------


@dataclass
class CooperativeSolution:
    """A complete cooperative solution with routes, relays and relay points.

    This is the **single source of truth** for Stage-2 search state.

    ``routes + relays`` carry the actual execution structure; ``ownership``
    and ``task_services`` are derived views that must agree with the routes
    (enforced by :func:`validate_cooperative_solution`).

    ``active_stations`` only lists the relay points that are *actually used*
    by the current :attr:`relays` — it is not a pre-activated candidate
    shortlist (candidate points live in a search-level pool, not in the
    solution state).

    ``swaps`` is a deprecated legacy field, kept only so old standalone
    code paths do not crash; the formal solver never populates it.
    """

    routes: Routes
    relays: tuple[RelayTransfer, ...] = ()
    swaps: tuple[object, ...] = ()
    #: Relay points actually used by :attr:`relays` (not a candidate list).
    active_stations: tuple[RelayStation, ...] = ()
    #: Which drone picks up each task (must equal the route CUSTOMER_PICK owner).
    ownership: Mapping[int, int] = field(default_factory=dict)
    #: Per-task service state (DIRECT / RELAY) with primary_owner, etc.
    task_services: Mapping[int, object] = field(default_factory=dict)

    @property
    def relay_count(self) -> int:
        return len(self.relays)

    @property
    def swap_count(self) -> int:
        return len(self.swaps)

    @property
    def active_station_count(self) -> int:
        return len(self.active_stations)

    def to_relay_solution(self) -> RelaySolution:
        """Convert to a RelaySolution (drops swaps)."""
        return RelaySolution(
            routes=self.routes,
            relays=self.relays,
            stations=self.active_stations,
        )


# ---------------------------------------------------------------------------
# Validation result
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CooperativeValidation:
    """Full validation verdict for a :class:`CooperativeSolution`."""

    valid: bool
    violations: tuple[str, ...]
    score: Score
    late_count: int
    total_lateness_min: float
    distance_km: float
    pickup_count_per_drone: Mapping[int, int]
    delivery_count_per_drone: Mapping[int, int]
    relay_count: int
    swap_count: int
    active_station_count: int
    pickup_times_min: Mapping[int, float]
    delivery_times_min: Mapping[int, float]
    ownership: Mapping[int, int]


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def validate_cooperative_solution(
    problem: Problem,
    solution: CooperativeSolution,
) -> CooperativeValidation:
    """Validate a cooperative solution using the relay DAG evaluator when
    relays exist, and the per-route evaluator for pure-DIRECT solutions.

    Routes with relays are validated via the relay DAG evaluator
    (``relay_validation``), never via per-route ``RouteEvaluator`` which
    would incorrectly reject split pickup/delivery across drones.
    """
    violations: list[str] = []
    routes = solution.routes
    relays = solution.relays
    swaps = solution.swaps
    stations = solution.active_stations
    ownership = dict(solution.ownership)
    expected = set(problem.task_ids)

    # -- 1. Pickup coverage --
    all_pickups: dict[int, int] = {}
    for drone, route in enumerate(routes):
        for visit in route:
            if visit > 0:
                all_pickups[visit] = all_pickups.get(visit, 0) + 1
    for tid in expected:
        count = all_pickups.get(tid, 0)
        if count == 0:
            violations.append(f"任务 {tid} 缺少取件")
        elif count > 1:
            violations.append(f"任务 {tid} 重复取件 ({count} 次)")

    # -- 2. Delivery coverage --
    all_deliveries: dict[int, int] = {}
    for drone, route in enumerate(routes):
        for visit in route:
            if visit < 0:
                all_deliveries[abs(visit)] = all_deliveries.get(abs(visit), 0) + 1
    for tid in expected:
        count = all_deliveries.get(tid, 0)
        if count == 0:
            violations.append(f"任务 {tid} 缺少送达")
        elif count > 1:
            violations.append(f"任务 {tid} 重复送达 ({count} 次)")

    # -- 3. K-constraint (pickup counts per drone) --
    pickup_counts: dict[int, int] = {
        d: sum(1 for v in route if v > 0) for d, route in enumerate(routes)
    }
    delivery_counts: dict[int, int] = {
        d: sum(1 for v in route if v < 0) for d, route in enumerate(routes)
    }
    for drone in range(problem.drone_count):
        count = pickup_counts.get(drone, 0)
        if count > problem.max_tasks_per_drone:
            violations.append(
                f"无人机 {drone} 取件 {count} 超过上限 {problem.max_tasks_per_drone}"
            )

    # -- 4. Swap must not be present in the formal path --
    if swaps:
        violations.append(
            f"正式 Cooperative 路径不允许包含 Swap 事件 ({len(swaps)} 个)"
        )

    # -- 5. Relay structural checks --
    relay_task_ids: set[int] = set()
    station_ids = {st.id for st in stations}
    for relay in relays:
        tid = relay.task_id
        if tid in relay_task_ids:
            violations.append(f"任务 {tid} 有多个中继")
        relay_task_ids.add(tid)
        if relay.station_id not in station_ids:
            violations.append(
                f"中继 {relay.id} 站点 {relay.station_id} 不在活跃集合中"
            )
        if relay.first_drone == relay.second_drone:
            violations.append(f"中继 {relay.id} 两架无人机相同")
        if tid not in (v for v in routes[relay.first_drone] if v > 0):
            violations.append(
                f"中继任务 {tid}: 取件不在 first_drone {relay.first_drone}"
            )
        if -tid not in routes[relay.second_drone]:
            violations.append(
                f"中继任务 {tid}: 送达不在 second_drone {relay.second_drone}"
            )

    # -- 6. Ownership consistency (routes are the truth) --
    for tid in expected:
        owner_drone = ownership.get(tid)
        if owner_drone is not None:
            if tid not in (v for v in routes[owner_drone] if v > 0):
                violations.append(
                    f"所有权不一致: 任务 {tid} 声称属于 {owner_drone} 但不在路线上"
                )

    # -- 7. Active station validity: only used relay points may be listed --
    used_station_ids = {relay.station_id for relay in relays}
    for st in stations:
        if st.id not in used_station_ids:
            violations.append(
                f"站点 {st.id} 未被任何 RelayTransfer 使用，不应出现在 active_stations"
            )
        if abs(st.source_visit) not in expected:
            violations.append(
                f"站点 {st.id} source_visit {st.source_visit} 无效"
            )
    for sid in used_station_ids:
        if sid not in station_ids:
            violations.append(f"中继引用的站点 {sid} 不在 active_stations 中")

    # -- 7b. TaskServiceState consistency with routes / relays --
    # The unified cooperative state must never drift: the per-task service
    # record has to agree with where the pickup/delivery actually live and
    # with the relay event list.  (Only checked when services exist.)
    if solution.task_services:
        from .cooperative_model import (
            ServiceMode as _ServiceMode,
            rebuild_services as _rebuild_services,
        )

        rebuilt = _rebuild_services(routes, relays, ())
        for tid in sorted(expected):
            state = solution.task_services.get(tid)
            ref = rebuilt.get(tid)
            if state is None:
                violations.append(f"任务 {tid}: 缺少 TaskServiceState")
                continue
            if ref is None:
                continue
            if state.mode != ref.mode:
                violations.append(
                    f"任务 {tid}: 服务模式 {state.mode.name} "
                    f"与路线推导 {ref.mode.name} 不一致"
                )
            if ref.primary_owner >= 0 and state.primary_owner != ref.primary_owner:
                violations.append(
                    f"任务 {tid}: primary_owner {state.primary_owner} "
                    f"与路线推导 {ref.primary_owner} 不一致"
                )
            if ref.delivery_owner >= 0 and state.delivery_owner != ref.delivery_owner:
                violations.append(
                    f"任务 {tid}: delivery_owner {state.delivery_owner} "
                    f"与路线推导 {ref.delivery_owner} 不一致"
                )
            if state.mode == _ServiceMode.DIRECT:
                if state.primary_owner != state.delivery_owner:
                    violations.append(
                        f"DIRECT 任务 {tid}: primary_owner != delivery_owner"
                    )
                if state.relay_event is not None:
                    violations.append(f"DIRECT 任务 {tid}: 不应携带 relay 事件")
            elif state.mode == _ServiceMode.RELAY:
                if state.relay_event is None:
                    violations.append(f"RELAY 任务 {tid}: 缺少 relay_event")
                if state.relay_event is not None:
                    if state.relay_event.task_id != tid:
                        violations.append(
                            f"RELAY 任务 {tid}: relay_event 任务编号不匹配"
                        )
                    # pickup_owner must equal relay.first_drone, delivery_owner
                    # must equal relay.second_drone.
                    if state.primary_owner != state.relay_event.first_drone:
                        violations.append(
                            f"RELAY 任务 {tid}: pickup_owner "
                            f"{state.primary_owner} != relay.first_drone "
                            f"{state.relay_event.first_drone}"
                        )
                    if state.delivery_owner != state.relay_event.second_drone:
                        violations.append(
                            f"RELAY 任务 {tid}: delivery_owner "
                            f"{state.delivery_owner} != relay.second_drone "
                            f"{state.relay_event.second_drone}"
                        )
            else:
                violations.append(
                    f"任务 {tid}: 未知服务模式 {state.mode.name}"
                )

    # -- 8. Timing / distance: relay DAG for relay solutions, per-route for DIRECT --
    pickup_times: dict[int, float] = {}
    delivery_times: dict[int, float] = {}
    total_distance = 0.0
    total_lateness = 0.0
    late_count = 0
    has_relays = len(relays) > 0

    if has_relays:
        from .relay_validation import evaluate_relay_solution as _eval_relay
        rev = _eval_relay(problem, solution.to_relay_solution())
        if not rev.valid:
            violations.extend(rev.violations)
        late_count = rev.score.late_count
        total_distance = rev.score.distance_km
        total_lateness = rev.total_lateness_min
        pickup_times = dict(rev.pickup_times_min)
        delivery_times = dict(rev.delivery_times_min)
    else:
        # Pure DIRECT: per-route RouteEvaluator (fast, correct)
        ev = RouteEvaluator(problem)
        for d, r in enumerate(routes):
            try:
                m = ev.evaluate(r)
            except ValueError as e:
                violations.append(f"无人机 {d} 路线非法: {e}")
                continue
            total_distance += m.score.distance_km
            total_lateness += m.total_lateness_min
            late_count += m.score.late_count
            for tid, t in m.pickup_times_min.items():
                pickup_times[tid] = t
            for tid, t in m.delivery_times_min.items():
                delivery_times[tid] = t

    # -- Compose result --
    score = Score(late_count, total_lateness, total_distance)
    return CooperativeValidation(
        valid=not violations,
        violations=tuple(violations),
        score=score,
        late_count=late_count,
        total_lateness_min=total_lateness,
        distance_km=total_distance,
        pickup_count_per_drone=MappingProxyType(pickup_counts),
        delivery_count_per_drone=MappingProxyType(delivery_counts),
        relay_count=len(relays),
        swap_count=len(swaps),
        active_station_count=len(stations),
        pickup_times_min=MappingProxyType(pickup_times),
        delivery_times_min=MappingProxyType(delivery_times),
        ownership=MappingProxyType(ownership),
    )
