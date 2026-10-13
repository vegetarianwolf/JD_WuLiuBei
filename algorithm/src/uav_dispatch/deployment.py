"""Task-demand-driven dynamic UAV home deployment.

Replaces the old fixed "one drone per relay station plus one at the origin"
pre-deployment rule.  Given the task pool, the relay stations, the fleet
size and the depot, this module decides how many UAVs (0, 1, 2, ...) to park
at every candidate starting point -- the origin plus every existing relay
station -- so that the pre-deployment is driven by the actual task geometry
and time-window pressure instead of a hard-coded distribution.

Candidate homes are the origin (``None``) plus every relay station id; the
returned home list always has length exactly ``drone_count``.  The module is
pure standard library, fully deterministic (no RNG), and the deployment
logic is decoupled from the relay handoff semantics: a station may hold 0
UAVs and still be a perfectly usable relay point.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor, isfinite
from statistics import median
from typing import Sequence

from .model import Point, RelayStation, Task


@dataclass(frozen=True, slots=True)
class DemandConfig:
    """Tunables for the demand score and the integer allocation.

    ``spatial_scale=None`` makes the spatial decay scale adaptive: the
    median pickup-to-candidate distance (floored at 1 km), so demand falls
    off smoothly instead of a hard radius.  ``urgency`` is
    ``1 + urgency_alpha / max(slack, urgency_eps_min)`` clipped to
    ``urgency_cap`` (the cap stops extreme tasks from draining every UAV
    onto one home).  ``cap_fraction`` sets the soft per-home maximum share
    (``ceil(cap_fraction * drone_count)``); ``cap_enabled=False`` removes
    the cap.  ``local_search_rounds`` bounds the cheap deployment local
    search (0 disables it).
    """

    spatial_scale: float | None = None
    urgency_alpha: float = 10.0
    urgency_eps_min: float = 1.0
    urgency_cap: float = 3.0
    value_gamma: float = 1.0
    cap_enabled: bool = True
    cap_fraction: float = 0.5
    local_search_rounds: int = 3
    imbalance_lambda: float = 0.25

    def __post_init__(self) -> None:
        if self.spatial_scale is not None and (
            not isfinite(self.spatial_scale) or self.spatial_scale <= 0
        ):
            raise ValueError("spatial_scale 必须为正数或 None")
        if self.urgency_alpha <= 0:
            raise ValueError("urgency_alpha 必须为正数")
        if self.urgency_eps_min <= 0:
            raise ValueError("urgency_eps_min 必须为正数")
        if self.urgency_cap < 1.0:
            raise ValueError("urgency_cap 不能小于 1")
        if self.value_gamma < 0:
            raise ValueError("value_gamma 不能为负")
        if not 0 < self.cap_fraction <= 1:
            raise ValueError("cap_fraction 必须在 (0, 1] 内")
        if self.local_search_rounds < 0:
            raise ValueError("local_search_rounds 不能为负")
        if self.imbalance_lambda < 0:
            raise ValueError("imbalance_lambda 不能为负")


@dataclass(frozen=True, slots=True)
class DeploymentNode:
    """One candidate home with its demand score and final UAV count."""

    key: str  # "depot" | "relay_<station id>"
    home: int | None  # None = depot/origin, int = relay station id
    demand_score: float
    allocated_uavs: int


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    """Result of :func:`compute_dynamic_uav_homes`."""

    homes: tuple[int | None, ...]
    nodes: tuple[DeploymentNode, ...]
    config: DemandConfig
    surrogate_score: float


def deployment_distribution(
    homes: Sequence[int | None | Point],
) -> dict[str, int]:
    """Map a home list to ``{"depot": n, "relay_<id>": n, ...}`` counts."""

    distribution: dict[str, int] = {}
    for index, home in enumerate(homes):
        if home is None:
            key = "depot"
        elif isinstance(home, Point):
            key = f"point_{index}"
        else:
            key = f"relay_{home}"
        distribution[key] = distribution.get(key, 0) + 1
    return distribution


def _clip(value: float, low: float, high: float) -> float:
    return low if value < low else (high if value > high else value)


def _candidate_points(
    stations: Sequence[RelayStation], depot: Point
) -> tuple[tuple[int | None, Point], ...]:
    """Ordered candidate homes: depot first, then stations by id."""

    return ((None, depot),) + tuple(
        (station.id, station.point)
        for station in sorted(stations, key=lambda item: item.id)
    )


def _demand_scores(
    tasks: Sequence[Task],
    candidates: Sequence[tuple[int | None, Point]],
    depot: Point,
    speed_km_per_min: float,
    config: DemandConfig,
) -> tuple[list[float], list[float]]:
    """Per-candidate demand scores plus per-task surrogate urgency weights.

    ``task_weight(i, r) = spatial_i(r) * urgency_i(r) * value_i`` where

    * ``spatial_i(r) = 1 / (1 + d(pickup_i, p_r) / scale)`` with a smooth
      adaptive decay scale (never a hard radius);
    * ``urgency_i(r) = clip(1 + alpha / max(slack_i(r), eps), 1, cap)`` with
      ``slack_i(r) = deadline_i - (d(p_r, pickup_i) + d(pickup_i,
      delivery_i)) / speed`` (urgency rises continuously as slack shrinks);
    * ``value_i = 1 + gamma * clip((travel_from_depot_i - deadline_i) /
      max(deadline_i, eps), 0, 1)`` -- tasks that cannot be served on time
      from the depot are worth deploying close to.

    The second list holds the surrogate weight ``u_i = urgency_i * value_i``
    (urgency taken at the task's best candidate) used by the deployment
    local search.
    """

    speed = speed_km_per_min
    pickups = tuple(task.pickup for task in tasks)
    deliveries = tuple(task.delivery for task in tasks)
    deadlines = tuple(task.deadline_min for task in tasks)
    pair_distances = tuple(
        pickup.distance_to(delivery)
        for pickup, delivery in zip(pickups, deliveries)
    )

    if config.spatial_scale is not None:
        scale = config.spatial_scale
    else:
        all_distances = [
            pickup.distance_to(point)
            for pickup in pickups
            for _, point in candidates
        ]
        scale = max(1.0, median(all_distances)) if all_distances else 1.0

    # Value proxy: how much fast service matters from the depot's view.
    values: list[float] = []
    for index, pickup in enumerate(pickups):
        travel_from_depot = (
            depot.distance_to(pickup) + pair_distances[index]
        ) / speed
        denominator = max(deadlines[index], 1e-9)
        values.append(
            1.0
            + config.value_gamma
            * _clip(
                (travel_from_depot - deadlines[index]) / denominator,
                0.0,
                1.0,
            )
        )

    scores = [0.0] * len(candidates)
    urgencies = [1.0] * len(tasks)
    eps = config.urgency_eps_min
    for index, (pickup, pair_distance, deadline) in enumerate(
        zip(pickups, pair_distances, deadlines)
    ):
        best_urgency = 1.0
        for candidate_index, (_, point) in enumerate(candidates):
            pickup_distance = pickup.distance_to(point)
            slack = deadline - (pickup_distance + pair_distance) / speed
            urgency = _clip(
                1.0 + config.urgency_alpha / max(slack, eps),
                1.0,
                config.urgency_cap,
            )
            if urgency > best_urgency:
                best_urgency = urgency
            spatial = 1.0 / (1.0 + pickup_distance / scale)
            scores[candidate_index] += spatial * urgency * values[index]
        urgencies[index] = best_urgency
    surrogate_weights = [
        urgency * value
        for urgency, value in zip(urgencies, values)
    ]
    return scores, surrogate_weights


def _allocate_largest_remainder(
    scores: Sequence[float],
    drone_count: int,
    cap: int,
) -> list[int]:
    """Hamilton / largest-remainder allocation with a soft per-home cap.

    Guarantees ``sum(allocation) == drone_count`` whenever
    ``cap * len(scores) >= drone_count`` (the caller enforces this).  The
    caller pre-checks the all-zero fallback.
    """

    node_count = len(scores)
    allocation = [0] * node_count
    remaining = drone_count
    active = list(range(node_count))
    while remaining > 0 and active:
        total = sum(scores[node] for node in active)
        if total <= 1e-12:
            break
        raw = {node: scores[node] / total * remaining for node in active}
        # Floor shares first, clamped to the cap.
        for node in active:
            base = int(floor(raw[node]))
            grant = min(base, cap - allocation[node])
            allocation[node] += grant
            remaining -= grant
        # Largest fractional remainders get the leftover UAVs, one each.
        order = sorted(
            active,
            key=lambda node: (-(raw[node] - floor(raw[node])), node),
        )
        for node in order:
            if remaining <= 0:
                break
            if allocation[node] >= cap:
                continue
            allocation[node] += 1
            remaining -= 1
        active = [node for node in active if allocation[node] < cap]
    if remaining > 0:
        # Round-robin safety net (feasible because cap >= ceil(K / m)).
        for node in range(node_count):
            if remaining <= 0:
                break
            if allocation[node] < cap:
                allocation[node] += 1
                remaining -= 1
    if remaining != 0:
        raise RuntimeError("动态部署分配失败：容量上限内无法放下全部无人机")
    return allocation


def _surrogate_score(
    tasks: Sequence[Task],
    candidates: Sequence[tuple[int | None, Point]],
    weights: Sequence[float],
    allocation: Sequence[int],
    drone_count: int,
    config: DemandConfig,
) -> float:
    """Cheap deployment surrogate for ranking candidate deployments.

    ``weighted_first_leg`` = sum of task urgency weights times the distance
    from each task pickup to its nearest *active* (>=1 UAV) home, plus a
    load-imbalance penalty: nodes whose assigned demand exceeds their
    per-UAV fair share.  Analysis/construction-only, never the objective.
    """

    active = [index for index, count in enumerate(allocation) if count > 0]
    first_leg = 0.0
    load = [0.0] * len(candidates)
    for index, task in enumerate(tasks):
        if not active:
            break
        pickup = task.pickup
        nearest = min(
            active,
            key=lambda candidate: pickup.distance_to(
                candidates[candidate][1]
            ),
        )
        distance = pickup.distance_to(candidates[nearest][1])
        first_leg += weights[index] * distance
        load[nearest] += weights[index]
    total_weight = sum(weights)
    penalty = 0.0
    if total_weight > 0 and drone_count > 0:
        per_uav = total_weight / drone_count
        for index in range(len(candidates)):
            ideal = allocation[index] * per_uav
            if load[index] > ideal:
                penalty += config.imbalance_lambda * (load[index] - ideal)
    return first_leg + penalty


def _deployment_local_search(
    tasks: Sequence[Task],
    candidates: Sequence[tuple[int | None, Point]],
    weights: Sequence[float],
    allocation: list[int],
    drone_count: int,
    config: DemandConfig,
) -> tuple[list[int], float]:
    """Greedy single-UAV moves (home A -> home B) on the surrogate score.

    Bounded to ``config.local_search_rounds`` full scans, so the
    initialisation stays cheap (8 UAVs x 8 candidates needs no
    metaheuristic).  Deterministic tie-breaking by candidate order.
    """

    if config.local_search_rounds <= 0 or drone_count <= 1:
        current = _surrogate_score(
            tasks, candidates, weights, allocation, drone_count, config
        )
        return list(allocation), current
    allocation = list(allocation)
    current = _surrogate_score(
        tasks, candidates, weights, allocation, drone_count, config
    )
    node_count = len(candidates)
    if config.cap_enabled:
        cap = max(
            ceil(config.cap_fraction * drone_count),
            ceil(drone_count / max(1, node_count)),
        )
    else:
        cap = drone_count
    for _ in range(config.local_search_rounds):
        improved = False
        for source in range(node_count):
            if allocation[source] <= 0:
                continue
            for target in range(node_count):
                if source == target or allocation[source] <= 0:
                    continue
                if allocation[target] >= cap:
                    continue
                trial = list(allocation)
                trial[source] -= 1
                trial[target] += 1
                trial_score = _surrogate_score(
                    tasks, candidates, weights, trial, drone_count, config
                )
                if trial_score < current - 1e-12:
                    allocation = trial
                    current = trial_score
                    improved = True
        if not improved:
            break
    return allocation, current


def _homes_from_allocation(
    allocation: Sequence[int],
    candidates: Sequence[tuple[int | None, Point]],
) -> tuple[int | None, ...]:
    """Expand per-candidate UAV counts into a per-drone home list."""

    homes: list[int | None] = []
    for count, (home, _) in zip(allocation, candidates):
        homes.extend([home] * count)
    return tuple(homes)


def _validate_fixed_allocation(
    fixed: Sequence[int],
    node_count: int,
    drone_count: int,
) -> list[int]:
    allocation = [int(value) for value in fixed]
    if len(allocation) != node_count:
        raise ValueError("fixed_allocation 长度必须等于候选起点数")
    if any(value < 0 for value in allocation):
        raise ValueError("fixed_allocation 不能包含负数")
    if sum(allocation) != drone_count:
        raise ValueError("fixed_allocation 之和必须等于无人机总数")
    return allocation


def compute_dynamic_uav_homes(
    tasks: Sequence[Task],
    stations: Sequence[RelayStation],
    drone_count: int,
    depot: Point,
    *,
    speed_km_per_min: float = 0.9,
    config: DemandConfig | None = None,
    fixed_allocation: Sequence[int] | None = None,
) -> DeploymentPlan:
    """Decide how many UAVs park at every candidate home, demand-driven.

    Candidate homes are the depot plus every relay station; each may hold 0,
    1, 2, ... UAVs (never a fixed one-per-station rule).  The returned
    ``homes`` tuple always has length exactly ``drone_count`` (``None`` =
    depot, ``int`` = relay station id).  ``fixed_allocation`` overrides the
    Hamilton allocation with an explicit per-candidate count (used only by
    A/B baselines); it must sum to ``drone_count``.
    """

    cfg = config if config is not None else DemandConfig()
    if drone_count <= 0:
        raise ValueError("无人机数量必须为正整数")
    if not isfinite(speed_km_per_min) or speed_km_per_min <= 0:
        raise ValueError("飞行速度必须为正数")
    candidates = _candidate_points(stations, depot)

    if not tasks:
        if fixed_allocation is not None:
            allocation = _validate_fixed_allocation(
                fixed_allocation, len(candidates), drone_count
            )
            homes = _homes_from_allocation(allocation, candidates)
        else:
            allocation = [0] * len(candidates)
            allocation[0] = drone_count
            homes = (None,) * drone_count
        nodes = tuple(
            DeploymentNode(
                "depot" if home is None else f"relay_{home}",
                home,
                0.0,
                allocation[index],
            )
            for index, (home, _) in enumerate(candidates)
        )
        return DeploymentPlan(homes, nodes, cfg, 0.0)

    scores, surrogate_weights = _demand_scores(
        tasks, candidates, depot, speed_km_per_min, cfg
    )

    if fixed_allocation is not None:
        allocation = _validate_fixed_allocation(
            fixed_allocation, len(candidates), drone_count
        )
        final_allocation = list(allocation)
        surrogate = _surrogate_score(
            tasks,
            candidates,
            surrogate_weights,
            final_allocation,
            drone_count,
            cfg,
        )
    else:
        total_demand = sum(scores)
        if total_demand <= 1e-12:
            # Safe fallback: no usable demand signal, park everything at the
            # depot (never forced to spread UAVs across empty stations).
            final_allocation = [0] * len(candidates)
            final_allocation[0] = drone_count
            surrogate = _surrogate_score(
                tasks,
                candidates,
                surrogate_weights,
                final_allocation,
                drone_count,
                cfg,
            )
        else:
            if cfg.cap_enabled:
                cap = max(
                    ceil(cfg.cap_fraction * drone_count),
                    ceil(drone_count / max(1, len(candidates))),
                )
            else:
                cap = drone_count
            final_allocation = _allocate_largest_remainder(
                scores, drone_count, cap
            )
            final_allocation, surrogate = _deployment_local_search(
                tasks,
                candidates,
                surrogate_weights,
                final_allocation,
                drone_count,
                cfg,
            )

    homes = _homes_from_allocation(final_allocation, candidates)
    nodes = tuple(
        DeploymentNode(
            "depot" if home is None else f"relay_{home}",
            home,
            scores[index],
            final_allocation[index],
        )
        for index, (home, _) in enumerate(candidates)
    )
    return DeploymentPlan(homes, nodes, cfg, surrogate)
