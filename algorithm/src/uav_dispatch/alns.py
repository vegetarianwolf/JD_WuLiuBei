"""Capacity-2 lexicographic hybrid adaptive large-neighbourhood search."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from itertools import permutations
from math import ceil, exp, isfinite
from random import Random
from time import perf_counter
from types import MappingProxyType
from typing import Iterable, Sequence

from .model import Problem, Score
from .relay import (
    GlobalRelayEvaluator,
    build_plan_index,
    relay_statistics,
)
from .search import (
    Route,
    RouteEvaluator,
    RouteInsertionOption,
    RouteInsertionSurface,
    Routes,
    SearchCounters,
    SolverResult,
    _pair_removal_delta_exact,
    _visits_removal_delta_exact,
    insert_leg_pair,
    route_insertion_options,
    route_leg_insertion_options,
    routes_score,
)
from .validation import evaluate_solution


DESTROY_OPERATORS = (
    "random",
    "worst_distance",
    "worst_lex",
    "spatial_related",
    "deadline_related",
    "late_critical",
    "route_segment",
    "capacity_conflict",
    "assignment_destroy",
    "home_displaced",
    "route_clear",
)
REPAIR_OPERATORS = (
    "greedy",
    "regret2",
    "regret3",
    "deadline",
    "slack",
    "cluster_regret",
)


class _SearchDeadlineReached(RuntimeError):
    """Abort an incomplete destroy/repair iteration at the wall-clock limit."""


@dataclass(frozen=True, slots=True)
class ALNSConfig:
    max_iterations: int = 400
    time_limit_seconds: float | None = None
    seed: int = 20260805
    candidate_limit: int | None = 48
    # Direct repair scales superlinearly with the number of removed tasks;
    # this band keeps assignment-changing neighborhoods while avoiding the
    # former 4%-10% repair bottleneck.
    min_destroy_fraction: float = 0.03
    max_destroy_fraction: float = 0.08
    weight_update_interval: int = 40
    reaction_factor: float = 0.2
    minimum_weight: float = 0.05
    initial_temperature: float = 0.03
    minimum_temperature: float = 0.0005
    cooling_rate: float = 0.995
    route_pool_interval: int = 50
    route_pool_node_limit: int = 5_000
    ejection_interval: int = 25
    ejection_trials: int = 24
    enable_route_pool: bool = False
    enable_ejection: bool = True
    enable_assignment_destroy: bool = True
    # Home-aware switches (default off so legacy behaviour is bit-identical).
    enable_home_seed: bool = False
    enable_home_displaced: bool = False
    enable_home_bias: bool = False
    enable_deadline_risk: bool = True
    enable_vnd: bool = False
    vnd_max_moves: int = 2
    vnd_task_limit: int = 12
    vnd_swap_pair_limit: int = 24
    vnd_block_window_limit: int = 16
    enable_cluster_repair: bool = False
    cluster_bundle_candidate_limit: int = 12
    cluster_pair_limit: int = 6
    relay_enabled: bool = True
    # A relay-aware search has a much larger neighbourhood than DIRECT.
    # Spend most of a fixed budget building a strong DIRECT incumbent, then
    # let the relay phase improve (and never worsen) that incumbent.
    relay_direct_warmup_fraction: float = 0.80
    relay_candidates_per_task: int = 2
    relay_plan_beam: int = 4
    relay_leg_candidate_limit: int | None = 4
    relay_leg_beam: int = 5
    relay_event_cap: int | None = 60
    relay_sample_every: int = 3
    relay_global_limit: int = 3
    relay_debug: bool = False
    # The seed pass converts strictly-better DIRECT tasks to relay right
    # after construction; the refine pass then moves relay legs between UAVs.
    enable_relay_seed: bool = True
    relay_seed_task_limit: int | None = 200
    enable_relay_refine: bool = True
    relay_refine_interval: int = 10
    relay_refine_task_limit: int | None = 8
    # Relay destroy schedule: smaller routine removals than the direct-only
    # schedule plus a periodic large destroy to keep diversification.
    relay_min_destroy_fraction: float = 0.03
    relay_max_destroy_fraction: float = 0.06
    relay_large_destroy_interval: int = 30
    relay_large_destroy_min: float = 0.06
    relay_large_destroy_max: float = 0.10
    # Two-stage repair: cheap ranking for every remaining task, exact
    # refinement only for the chosen task.
    repair_rank_candidate_limit: int | None = 24
    repair_exact_candidate_limit: int | None = 48
    # Relay probe budget per repair round.
    relay_probe_fraction: float = 0.25
    relay_probe_min: int = 1
    relay_probe_max_tasks: int = 2
    direct_exact_top_k: int = 2

    def __post_init__(self) -> None:
        if self.max_iterations < 0:
            raise ValueError("迭代次数不能为负")
        if self.time_limit_seconds is not None and (
            not isfinite(self.time_limit_seconds) or self.time_limit_seconds <= 0
        ):
            raise ValueError("时间上限必须为正数")
        if self.candidate_limit is not None and self.candidate_limit < 4:
            raise ValueError("候选位置上限至少为 4")
        if not (
            isfinite(self.min_destroy_fraction)
            and isfinite(self.max_destroy_fraction)
            and 0 < self.min_destroy_fraction <= self.max_destroy_fraction <= 1
        ):
            raise ValueError("破坏比例必须满足 0 < min <= max <= 1")
        if self.weight_update_interval <= 0:
            raise ValueError("权重更新周期必须为正整数")
        if not isfinite(self.reaction_factor) or not 0 < self.reaction_factor <= 1:
            raise ValueError("权重反应系数必须在 (0, 1] 内")
        if not isfinite(self.cooling_rate) or not 0 < self.cooling_rate <= 1:
            raise ValueError("降温率必须在 (0, 1] 内")
        if (
            not isfinite(self.minimum_weight)
            or self.minimum_weight <= 0
            or not isfinite(self.initial_temperature)
            or not isfinite(self.minimum_temperature)
            or self.initial_temperature <= 0
            or self.minimum_temperature <= 0
            or self.minimum_temperature > self.initial_temperature
        ):
            raise ValueError("温度和最小权重必须为有限正数")
        if self.route_pool_interval < 0 or self.ejection_interval < 0:
            raise ValueError("强化周期不能为负")
        if self.route_pool_node_limit < 0 or self.ejection_trials < 0:
            raise ValueError("强化搜索预算不能为负")
        if min(
            self.vnd_max_moves,
            self.vnd_task_limit,
            self.vnd_swap_pair_limit,
            self.vnd_block_window_limit,
            self.cluster_bundle_candidate_limit,
            self.cluster_pair_limit,
        ) < 0:
            raise ValueError("VND 与聚类修复搜索预算不能为负")
        if self.relay_candidates_per_task <= 0:
            raise ValueError("每个任务的中继候选数必须为正整数")
        if (
            not isfinite(self.relay_direct_warmup_fraction)
            or not 0 <= self.relay_direct_warmup_fraction < 1
        ):
            raise ValueError("中继 DIRECT 预热比例必须位于 [0, 1) 内")
        if self.relay_plan_beam < 1:
            raise ValueError("中继计划束宽必须为正整数")
        if (
            self.relay_leg_candidate_limit is not None
            and self.relay_leg_candidate_limit < 2
        ):
            raise ValueError("单腿候选位置上限至少为 2")
        if self.relay_leg_beam < 1:
            raise ValueError("单腿束宽必须为正整数")
        if self.relay_event_cap is not None and self.relay_event_cap < 4:
            raise ValueError("路线事件数软剪枝至少为 4")
        if self.relay_sample_every <= 0:
            raise ValueError("中继候选抽样周期必须为正整数")
        if self.relay_global_limit < 1:
            raise ValueError("中继精确评估束宽必须为正整数")
        if self.relay_refine_interval <= 0:
            raise ValueError("中继精化周期必须为正整数")
        if (
            self.relay_refine_task_limit is not None
            and self.relay_refine_task_limit < 0
        ):
            raise ValueError("中继精化任务上限不能为负")
        if (
            self.relay_seed_task_limit is not None
            and self.relay_seed_task_limit < 0
        ):
            raise ValueError("中继播种任务上限不能为负")
        for name, value in (
            ("常规中继破坏比例下限", self.relay_min_destroy_fraction),
            ("常规中继破坏比例上限", self.relay_max_destroy_fraction),
            ("大破坏比例下限", self.relay_large_destroy_min),
            ("大破坏比例上限", self.relay_large_destroy_max),
        ):
            if not (isfinite(value) and 0 < value <= 1):
                raise ValueError(f"{name}必须位于 (0, 1] 内")
        if self.relay_min_destroy_fraction > self.relay_max_destroy_fraction:
            raise ValueError("常规中继破坏比例必须满足 min <= max")
        if self.relay_large_destroy_min > self.relay_large_destroy_max:
            raise ValueError("大破坏比例必须满足 min <= max")
        if self.relay_large_destroy_interval < 0:
            raise ValueError("中继大破坏周期不能为负")
        if self.repair_rank_candidate_limit is not None and (
            self.repair_rank_candidate_limit < 4
        ):
            raise ValueError("修复排名候选位置上限至少为 4")
        if self.repair_exact_candidate_limit is not None and (
            self.repair_exact_candidate_limit < 4
        ):
            raise ValueError("修复精确候选位置上限至少为 4")
        if (
            not isfinite(self.relay_probe_fraction)
            or not 0 < self.relay_probe_fraction <= 1
        ):
            raise ValueError("中继探测比例必须在 (0, 1] 内")
        if self.relay_probe_min < 0:
            raise ValueError("中继最小探测任务数不能为负")
        if self.relay_probe_max_tasks < 1:
            raise ValueError("中继最大探测任务数必须为正整数")
        if self.direct_exact_top_k < 1:
            raise ValueError("DIRECT 精确候选数必须为正整数")


@dataclass(frozen=True, slots=True)
class TaskInsertionOption:
    """One complete service plan for a destroyed original task.

    A DIRECT option carries a single route insertion; a RELAY option carries
    one inbound and one outbound leg insertion plus the bounded beam of
    promising (inbound, outbound) combinations for global evaluation.
    """

    task_id: int
    mode: str
    relay_id: int | None
    local_estimated_delta: Score
    direct_route_index: int | None = None
    direct_route: Route | None = None
    direct_pickup_position: int | None = None
    direct_delivery_position: int | None = None
    inbound_route_index: int | None = None
    inbound_route: Route | None = None
    inbound_start_position: int | None = None
    inbound_end_position: int | None = None
    outbound_route_index: int | None = None
    outbound_route: Route | None = None
    outbound_start_position: int | None = None
    outbound_end_position: int | None = None
    combos: tuple[
        tuple[RouteInsertionOption, RouteInsertionOption], ...
    ] = ()
    exact_score: Score | None = None
    waiting_min: float = 0.0
    end_time_min: float | None = None
    feasible: bool = True


class _LegOptionCache:
    """Cross-iteration bounded memo for relay leg insertion options.

    Leg options are pure functions of (route snapshot, home node, leg pair,
    limits); only one or two routes change per ALNS iteration, so route
    snapshots repeat heavily and keying the memo by the route content (not a
    per-repair version counter) makes it valid across repair passes.
    """

    __slots__ = ("data", "maxsize", "hits", "misses")

    def __init__(self, maxsize: int = 20_000) -> None:
        self.data: OrderedDict[
            tuple, tuple[RouteInsertionOption, ...]
        ] = OrderedDict()
        self.maxsize = maxsize
        self.hits = 0
        self.misses = 0

    def get(
        self, key: tuple
    ) -> tuple[RouteInsertionOption, ...] | None:
        cached = self.data.get(key)
        if cached is None:
            self.misses += 1
            return None
        self.hits += 1
        self.data.move_to_end(key)
        return cached

    def put(self, key: tuple, value: tuple[RouteInsertionOption, ...]) -> None:
        self.data[key] = value
        self.data.move_to_end(key)
        if len(self.data) > self.maxsize:
            self.data.popitem(last=False)


@dataclass(slots=True)
class _RelaySearchConfig:
    """Mutable per-solve relay search settings and evaluation counters."""

    enabled: bool
    candidates_per_task: int
    plan_beam: int
    leg_candidate_limit: int | None
    leg_beam: int
    event_cap: int | None
    debug: bool
    sample_every: int = 4
    global_limit: int = 3
    probe_fraction: float = 0.25
    probe_min: int = 1
    probe_max_tasks: int = 3
    counters: SearchCounters | None = None
    global_evaluation_count: int = 0
    candidate_evaluation_count: int = 0
    generated_relay_options: int = 0
    selected_relay_options: int = 0
    refine_candidates_evaluated: int = 0
    refine_candidates_pruned: int = 0
    leg_option_cache: _LegOptionCache | None = None


def _relay_refine_pass(
    problem: Problem,
    evaluator: RouteEvaluator,
    global_evaluator: GlobalRelayEvaluator,
    routes: Routes,
    *,
    relay_cfg: _RelaySearchConfig,
    task_limit: int | None,
    deadline: float | None,
) -> Routes:
    """Bounded first-improve pass that moves single relay legs between UAVs.

    Relay value comes from pairing an inbound leg on one drone with an
    outbound leg on a better-placed drone; single-task repair rarely
    assembles such pairs alone, so this small neighbourhood relocates one
    leg (inbound or outbound) of each relay task onto another drone and
    accepts only strict lexicographic improvements.
    """

    plan_index = build_plan_index(problem, routes)
    relay_tasks = list(plan_index.relay_task_ids)
    if not relay_tasks:
        return routes
    current = routes
    current_schedule = global_evaluator.evaluate(current)
    current_score = current_schedule.score
    # Only tasks late in the current schedule are worth refining, most-late
    # first.  Relocating a leg of an on-time task can only chase the third
    # (distance) objective, and the old id-order scan wasted most of its
    # global evaluations on tasks whose schedule was already fine.  The
    # task_limit cap still bounds the number of considered tasks.
    delivery_times = current_schedule.delivery_times_min
    late_tasks = [
        task_id
        for task_id in relay_tasks
        if delivery_times.get(task_id, float("inf"))
        > problem.task(task_id).deadline_min + 1e-9
    ]
    late_tasks.sort(
        key=lambda task_id: (
            delivery_times.get(task_id, float("inf"))
            - problem.task(task_id).deadline_min,
            task_id,
        ),
        reverse=True,
    )
    considered = 0
    for task_id in late_tasks:
        if task_limit is not None and considered >= task_limit:
            break
        considered += 1
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        plan = plan_index.task_plans[task_id]
        in_leg, out_leg = plan.leg_ids
        in_route = plan_index.inbound_route_by_task[task_id]
        out_route = plan_index.outbound_route_by_task[task_id]
        for leg_id, source_route, other_route, is_out in (
            (out_leg, out_route, in_route, True),
            (in_leg, in_route, out_route, False),
        ):
            source_route_list = tuple(
                visit
                for visit in current[source_route]
                if abs(visit) != leg_id
            )
            # Safe lower-bound pruning for the RELAY_OUT (pickup side) leg
            # only.  Removing the pickup cannot help any other route (its
            # handoff predecessor stays put), so the global delta is at
            # least source_removal_delta + target_insertion_delta (the
            # insertion side can only add handoff waiting).  When that lower
            # bound is not strictly negative the candidate cannot strictly
            # improve the lexicographic score and the expensive global
            # evaluation is skipped.  The RELAY_IN (drop side) removal
            # relaxes the handoff constraint of the pickup on another drone,
            # which these local deltas cannot bound, so it is never pruned.
            source_removal_delta: Score | None = None
            if is_out:
                source_removed_score = evaluator.evaluate(
                    source_route_list,
                    start_node=problem.home_node(source_route),
                ).score
                source_orig_score = evaluator.evaluate(
                    current[source_route],
                    start_node=problem.home_node(source_route),
                ).score
                source_removal_delta = (
                    source_removed_score - source_orig_score
                )
            for target_index, target in enumerate(current):
                if target_index == source_route:
                    continue
                if deadline is not None and perf_counter() >= deadline:
                    raise _SearchDeadlineReached
                options = route_leg_insertion_options(
                    problem,
                    evaluator,
                    target,
                    target_index,
                    leg_id,
                    -leg_id,
                    candidate_limit=4,
                    option_count=2,
                    counts_toward_k=is_out,
                    event_cap=relay_cfg.event_cap,
                )
                for option in options:
                    if target_index == other_route:
                        other_leg = in_leg if is_out else out_leg
                        combined = option.route
                        try:
                            other_pos = combined.index(other_leg)
                            other_end = combined.index(-other_leg)
                            leg_pos = combined.index(leg_id)
                            leg_end = combined.index(-leg_id)
                        except ValueError:
                            continue
                        # Same-UAV adjacency: an outbound pickup directly
                        # after the inbound drop is dominated by DIRECT.
                        if is_out and leg_pos == other_end + 1:
                            continue
                        if not is_out and other_pos == leg_end + 1:
                            continue
                        if is_out and leg_pos <= other_end:
                            continue
                        if not is_out and other_pos <= leg_end:
                            continue
                    if source_removal_delta is not None:
                        local_delta = source_removal_delta + option.delta
                        if not local_delta < Score(0, 0, 0):
                            relay_cfg.refine_candidates_pruned += 1
                            continue
                    candidate = list(current)
                    candidate[source_route] = source_route_list
                    candidate[target_index] = option.route
                    candidate_routes = tuple(candidate)
                    schedule = global_evaluator.evaluate_delta(
                        current, current_schedule, candidate_routes
                    )
                    relay_cfg.global_evaluation_count += 1
                    relay_cfg.refine_candidates_evaluated += 1
                    if schedule.feasible and schedule.score < current_score:
                        return candidate_routes
    return current


def _relay_seed_pass(
    problem: Problem,
    evaluator: RouteEvaluator,
    global_evaluator: GlobalRelayEvaluator,
    routes: Routes,
    *,
    relay_cfg: _RelaySearchConfig,
    task_limit: int | None,
    deadline: float | None,
) -> Routes:
    """Bounded pass that seeds DIRECT tasks with strictly better relay plans.

    A single relay repair round only probes a handful of tasks, so the search
    may never enter the relay region of the solution space.  This pass runs
    once after construction: for every task in id order it builds the relay
    beam and, when the best relay combination is strictly better than the
    task's current DIRECT plan, adopts it.  The solution only improves.
    """

    plan_index = build_plan_index(problem, routes)
    direct_tasks = [
        task_id
        for task_id in sorted(plan_index.direct_task_ids)
        if problem.task_relay_candidates.get(task_id)
    ]
    if not direct_tasks:
        return routes
    current = routes
    considered = 0
    for task_id in direct_tasks:
        if task_limit is not None and considered >= task_limit:
            break
        considered += 1
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        visits = plan_index.task_visits[task_id]
        # The task is currently served DIRECT, so remove its visits first;
        # re-insertion plans are built against the reduced partial solution.
        reduced = tuple(
            tuple(visit for visit in route if visit not in visits)
            for route in current
        )
        options = _all_plans_for_task(
            problem,
            evaluator,
            reduced,
            task_id,
            candidate_limit=24,
            option_count=6,
            relay_cfg=relay_cfg,
            deadline=deadline,
            probe_relay=True,
        )
        if not options:
            continue
        candidate, score, mode = _apply_task_exact(
            problem,
            evaluator,
            global_evaluator,
            reduced,
            options,
            relay_cfg=relay_cfg,
            deadline=deadline,
            direct_exact_top_k=1,
        )
        if candidate is not None and mode == "RELAY" and score is not None:
            current = candidate
            if relay_cfg.debug:
                print(f"[relay-debug] seed task={task_id} mode=RELAY")
    return current


def _task_ids_in_routes(
    problem: Problem, routes: Sequence[Sequence[int]]
) -> tuple[int, ...]:
    registry = problem.leg_registry
    seen: set[int] = set()
    ordered: list[int] = []
    for route in routes:
        for visit in route:
            if visit > 0:
                leg = registry.get(visit) if registry else None
                task_id = leg.task_id if leg is not None else visit
                if task_id not in seen:
                    seen.add(task_id)
                    ordered.append(task_id)
    return tuple(ordered)


def _visit_task_id(problem: Problem, visit: int) -> int:
    """Original task owning one route visit (DIRECT visit or transport leg)."""

    if problem.leg_registry:
        leg = problem.leg_registry.get(abs(visit))
        if leg is not None:
            return leg.task_id
    return abs(visit)


def _route_task_ids(problem: Problem, route: Sequence[int]) -> tuple[int, ...]:
    seen: set[int] = set()
    ordered: list[int] = []
    for visit in route:
        task_id = _visit_task_id(problem, visit)
        if task_id not in seen:
            seen.add(task_id)
            ordered.append(task_id)
    return tuple(ordered)


def _remove_tasks(
    routes: Sequence[Sequence[int]],
    plan_index,
    task_ids: Iterable[int],
) -> Routes:
    """Remove complete original tasks: both relay legs disappear together."""

    forbidden: set[int] = set()
    for task_id in task_ids:
        forbidden.update(plan_index.task_visits[task_id])
    return tuple(
        tuple(visit for visit in route if visit not in forbidden)
        for route in routes
    )


def _task_route_index(routes: Sequence[Sequence[int]]) -> dict[int, int]:
    return {
        visit: route_index
        for route_index, route in enumerate(routes)
        for visit in route
        if visit > 0
    }


def _choose_ranked(
    ranked: Sequence[int], count: int, rng: Random, exponent: float = 4.0
) -> list[int]:
    available = list(ranked)
    chosen: list[int] = []
    while available and len(chosen) < count:
        index = int((rng.random() ** exponent) * len(available))
        chosen.append(available.pop(index))
    return chosen


def _deadline_risk(delivered_at: float, deadline: float) -> float:
    """Return monotonic urgency without changing the lexicographic score."""

    if deadline <= 1e-12:
        return float("inf") if delivered_at > 1e-12 else 1.0
    return delivered_at / deadline


def _score_strictly_better(
    candidate: Score,
    incumbent: Score,
    *,
    tolerance: float = 1e-9,
) -> bool:
    """Compare local moves lexicographically while ignoring round-off noise."""

    if not candidate < incumbent:
        return False
    if candidate.late_count != incumbent.late_count:
        return True
    lateness_delta = (
        candidate.total_lateness_min - incumbent.total_lateness_min
    )
    if lateness_delta < -tolerance:
        return True
    if lateness_delta != 0.0:
        return False
    return candidate.distance_km < incumbent.distance_km - tolerance


class _DirectPlanIndex:
    """Minimal plan-index stand-in for DIRECT-only problems.

    ``_remove_tasks`` and ``destroy_solution`` only consume
    ``task_visits`` / ``task_routes`` / ``delivery_route_by_task``; for a
    direct task these are fully determined by the route visit signs, so the
    O(tasks x visits) general plan-index build can be skipped.
    """

    __slots__ = ("task_visits",)

    def __init__(self, task_visits: dict[int, frozenset[int]]) -> None:
        self.task_visits = task_visits


def destroy_solution(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    operator: str,
    count: int,
    rng: Random,
    *,
    use_deadline_risk: bool = True,
) -> tuple[Routes, tuple[int, ...]]:
    """Remove complete original tasks using one named destroy operator.

    A relay task is always removed as one unit: its RELAY_IN and RELAY_OUT
    legs disappear together, so no orphan legs remain.  For DIRECT-only
    problems the operator behaviour is identical to the legacy version.
    """

    task_ids = _task_ids_in_routes(problem, routes)
    count = min(max(1, count), len(task_ids))
    if not problem.has_relays:
        route_by_task = _task_route_index(routes)
        task_visits_by_task: dict[int, frozenset[int]] = {
            task_id: frozenset((task_id, -task_id)) for task_id in task_ids
        }
        plan_index = _DirectPlanIndex(task_visits_by_task)
    else:
        plan_index = build_plan_index(problem, routes)
        route_by_task = dict(plan_index.delivery_route_by_task)
        task_visits_by_task = plan_index.task_visits
        task_routes_by_task = plan_index.task_routes
    route_metrics = {
        route_index: evaluator.evaluate(
            routes[route_index],
            start_node=problem.home_node(route_index),
        )
        for route_index, route in enumerate(routes)
        if route
    }

    if not problem.has_relays:
        # Direct tasks live as one (pickup, delivery) pair on one route; the
        # removal gain is computed bit-identically to full evaluation by
        # re-walking the spliced route over cached surface data.
        speed = problem.speed_km_per_min
        distances = problem._distances
        surface_cache = evaluator.surface_cache

        def removal_gain(task_id: int) -> Score:
            route_index = route_by_task[task_id]
            route = routes[route_index]
            pickup_position = route.index(task_id)
            delivery_position = route.index(-task_id, pickup_position + 1)
            surface = surface_cache.get(
                route, start_node=problem.home_node(route_index)
            )
            late_count, lateness, distance_delta = _pair_removal_delta_exact(
                surface,
                pickup_position,
                delivery_position,
                speed=speed,
                distances=distances,
                registry=problem.leg_registry,
                route_start_node=problem.home_node(route_index),
            )
            return Score(late_count, lateness, distance_delta)
    else:
        # Relay tasks span one leg pair per route (two pairs when both legs
        # share a route); remove all of the task's visits on each route
        # incrementally from the cached surface.
        speed = problem.speed_km_per_min
        distances = problem._distances
        surface_cache = evaluator.surface_cache

        def removal_gain(task_id: int) -> Score:
            visits = task_visits_by_task[task_id]
            gain = Score(0, 0.0, 0.0)
            for route_index in task_routes_by_task[task_id]:
                route = routes[route_index]
                positions = tuple(
                    index
                    for index, visit in enumerate(route)
                    if visit in visits
                )
                surface = surface_cache.get(
                    route, start_node=problem.home_node(route_index)
                )
                late_count, lateness, distance_delta = (
                    _visits_removal_delta_exact(
                        surface,
                        positions,
                        speed=speed,
                        distances=distances,
                        registry=problem.leg_registry,
                        route_start_node=problem.home_node(route_index),
                    )
                )
                gain += Score(late_count, lateness, distance_delta)
            return gain

    if operator == "random":
        removed = rng.sample(task_ids, count)
    elif operator == "assignment_destroy":
        non_empty_indices = [
            route_index for route_index, route in enumerate(routes) if route
        ]

        def task_priority(task_id: int) -> tuple[float, ...]:
            gain = removal_gain(task_id)
            delivery_route = route_by_task[task_id]
            metrics = route_metrics[delivery_route]
            delivered_at = metrics.delivery_times_min[task_id]
            deadline = problem.task(task_id).deadline_min
            lateness = max(0.0, delivered_at - deadline)
            risk = (
                _deadline_risk(delivered_at, deadline)
                if use_deadline_risk
                else 0.0
            )
            utilization_gap = 1.0 - (
                len(metrics.delivery_times_min) / problem.max_tasks_per_drone
            )
            return (
                float(gain.late_count),
                gain.total_lateness_min,
                lateness,
                risk,
                gain.distance_km,
                utilization_gap,
                -deadline,
                -task_id,
            )

        def route_priority(route_index: int) -> tuple[float, ...]:
            metrics = route_metrics[route_index]
            max_risk = max(
                (
                    _deadline_risk(
                        delivered_at,
                        problem.task(task_id).deadline_min,
                    )
                    for task_id, delivered_at in metrics.delivery_times_min.items()
                ),
                default=0.0,
            )
            if not use_deadline_risk:
                max_risk = 0.0
            task_count = len(metrics.delivery_times_min)
            utilization_gap = 1.0 - task_count / problem.max_tasks_per_drone
            return (
                float(metrics.score.late_count),
                metrics.score.total_lateness_min,
                max_risk,
                utilization_gap,
                metrics.score.distance_km / max(1, task_count),
                -route_index,
            )

        ranked_routes = sorted(
            non_empty_indices,
            key=route_priority,
            reverse=True,
        )
        # A full fleet can change assignment only when at least two UAVs expose
        # a free task slot.  Spread the mandatory removals before filling the
        # rest from the most inefficient selected routes.
        route_count = min(
            len(ranked_routes),
            count,
            max(2, round(count ** 0.5)),
        )
        selected_routes = _choose_ranked(
            ranked_routes, route_count, rng, exponent=2.5
        )
        removed = []
        for route_index in selected_routes:
            local_ranked = sorted(
                _route_task_ids(problem, routes[route_index]),
                key=task_priority,
                reverse=True,
            )
            removed.extend(_choose_ranked(local_ranked, 1, rng, exponent=3.0))

        selected_pool = sorted(
            (
                task_id
                for route_index in selected_routes
                for task_id in _route_task_ids(problem, routes[route_index])
                if task_id not in removed
            ),
            key=task_priority,
            reverse=True,
        )
        if len(selected_pool) < count - len(removed):
            selected_pool.extend(
                sorted(
                    (
                        task_id
                        for task_id in task_ids
                        if task_id not in removed
                        and task_id not in selected_pool
                    ),
                    key=task_priority,
                    reverse=True,
                )
            )
        removed.extend(
            _choose_ranked(
                selected_pool,
                count - len(removed),
                rng,
                exponent=3.0,
            )
        )
    elif operator == "home_displaced":
        homes = problem.home_points()

        def displacement(task_id: int) -> tuple[float, int]:
            route_index = route_by_task[task_id]
            own_home = homes[route_index]
            pickup = problem.task(task_id).pickup
            nearest = min(home.distance_to(pickup) for home in homes)
            return own_home.distance_to(pickup) - nearest, -task_id

        ranked = sorted(task_ids, key=displacement, reverse=True)
        removed = _choose_ranked(ranked, count, rng, exponent=2.0)
    elif operator in {"worst_distance", "worst_lex"}:
        ranked: list[tuple[Score | float, int]] = []
        for task_id in task_ids:
            gain = removal_gain(task_id)
            key: Score | float = (
                gain if operator == "worst_lex" else gain.distance_km
            )
            ranked.append((key, task_id))
        ranked.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        removed = _choose_ranked([item[1] for item in ranked], count, rng)
    elif operator in {"spatial_related", "deadline_related"}:
        seed = rng.choice(task_ids)
        seed_task = problem.task(seed)

        def relatedness(task_id: int) -> tuple[float, int]:
            task = problem.task(task_id)
            deadline_gap = abs(task.deadline_min - seed_task.deadline_min)
            spatial_gap = task.pickup.distance_to(seed_task.pickup) + task.delivery.distance_to(
                seed_task.delivery
            )
            same_route_bonus = -2.0 if route_by_task[task_id] == route_by_task[seed] else 0.0
            if operator == "deadline_related":
                value = deadline_gap + 0.15 * spatial_gap + same_route_bonus
            else:
                value = spatial_gap + 0.15 * deadline_gap + same_route_bonus
            return value, task_id

        ranked_ids = [task_id for _, task_id in sorted(map(relatedness, task_ids))]
        removed = _choose_ranked(ranked_ids, count, rng, exponent=2.5)
    elif operator == "late_critical":
        urgency: list[tuple[float, int]] = []
        for route_index, route in enumerate(routes):
            metrics = evaluator.evaluate(
                route, start_node=problem.home_node(route_index)
            )
            for task_id, delivered_at in metrics.delivery_times_min.items():
                deadline = problem.task(task_id).deadline_min
                if use_deadline_risk:
                    urgency.append(
                        (-_deadline_risk(delivered_at, deadline), task_id)
                    )
                else:
                    urgency.append((deadline - delivered_at, task_id))
        urgency.sort()
        removed = _choose_ranked([item[1] for item in urgency], count, rng, 3.0)
    elif operator == "route_segment":
        non_empty = [route for route in routes if route]
        route = rng.choice(non_empty)
        start = rng.randrange(len(route))
        ordered = []
        for offset in range(len(route)):
            task_id = _visit_task_id(
                problem, route[(start + offset) % len(route)]
            )
            if task_id not in ordered:
                ordered.append(task_id)
            if len(ordered) == count:
                break
        remaining = [task for task in task_ids if task not in ordered]
        rng.shuffle(remaining)
        removed = (ordered + remaining)[:count]
    elif operator == "capacity_conflict":
        conflicts: list[tuple[int, int]] = []
        for route in routes:
            load = 0
            full_positions: set[int] = set()
            positions: dict[int, list[int]] = {}
            for index, visit in enumerate(route):
                load += 1 if visit > 0 else -1
                positions.setdefault(abs(visit), []).append(index)
                if load == problem.capacity:
                    full_positions.add(index)
            for visit_id, interval in positions.items():
                overlap = sum(interval[0] <= pos < interval[-1] for pos in full_positions)
                conflicts.append((overlap, _visit_task_id(problem, visit_id)))
        conflicts.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        removed = _choose_ranked([item[1] for item in conflicts], count, rng)
    elif operator == "route_clear":
        candidates = [route for route in routes if route]
        route = rng.choice(candidates)
        local = list(
            dict.fromkeys(_visit_task_id(problem, visit) for visit in route)
        )
        rng.shuffle(local)
        remaining = [task for task in task_ids if task not in local]
        rng.shuffle(remaining)
        removed = (local + remaining)[:count]
    else:
        raise ValueError(f"未知破坏算子 {operator}")

    unique_removed = tuple(dict.fromkeys(removed))
    return _remove_tasks(routes, plan_index, unique_removed), unique_removed


def _all_options_for_task(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    task_id: int,
    candidate_limit: int | None,
    option_count: int,
    cache: dict[tuple[int, int, Route, int | None, int], tuple[RouteInsertionOption, ...]],
    deadline: float | None = None,
    surfaces: dict[int, RouteInsertionSurface] | None = None,
) -> tuple[RouteInsertionOption, ...]:
    options: list[RouteInsertionOption] = []
    for route_index, route in enumerate(routes):
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        key = (task_id, route_index, route, candidate_limit, option_count)
        route_options = cache.get(key)
        if route_options is None:
            route_options = route_insertion_options(
                problem,
                evaluator,
                route,
                route_index,
                task_id,
                candidate_limit=candidate_limit,
                option_count=option_count,
                surface=surfaces.get(route_index) if surfaces else None,
            )
            cache[key] = route_options
        options.extend(route_options)
    options.sort(
        key=lambda option: (
            option.delta.late_count,
            option.delta.total_lateness_min,
            option.delta.distance_km,
            option.route_index,
            option.pickup_position,
            option.delivery_position,
            option.route,
        )
    )
    return tuple(options[:option_count])


class _RepairCache:
    """Route-versioned option cache inside one relay-aware repair pass.

    Options are pure functions of (task/leg, route snapshot), so caching by
    route version reproduces the exact same option sequences as full
    recomputation while only re-evaluating routes changed by the previous
    insertion.
    """

    __slots__ = ("versions", "direct", "leg", "combos")

    def __init__(self, route_count: int) -> None:
        self.versions = [0] * route_count
        self.direct: dict[
            tuple[int, int, int], tuple[RouteInsertionOption, ...]
        ] = {}
        self.leg: dict[
            tuple[int, int, int, int, bool],
            tuple[RouteInsertionOption, ...],
        ] = {}
        self.combos: dict[
            tuple[int, int, tuple[int, ...]],
            tuple[tuple[Score, RouteInsertionOption, RouteInsertionOption], ...],
        ] = {}

    def direct_options(
        self,
        problem: Problem,
        evaluator: RouteEvaluator,
        routes: Routes,
        task_id: int,
        route_index: int,
        candidate_limit: int | None,
        option_count: int,
        deadline: float | None,
    ) -> tuple[RouteInsertionOption, ...]:
        key = (
            task_id,
            route_index,
            self.versions[route_index],
            candidate_limit if candidate_limit is not None else -1,
        )
        cached = self.direct.get(key)
        if cached is not None:
            return cached
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        surface = evaluator.surface_cache.get(
            routes[route_index], start_node=problem.home_node(route_index)
        )
        cached = route_insertion_options(
            problem,
            evaluator,
            routes[route_index],
            route_index,
            task_id,
            candidate_limit=candidate_limit,
            option_count=option_count,
            surface=surface,
        )
        self.direct[key] = cached
        return cached

    def leg_options(
        self,
        problem: Problem,
        evaluator: RouteEvaluator,
        routes: Routes,
        start_visit: int,
        end_visit: int,
        route_index: int,
        leg_candidate_limit: int | None,
        counts_toward_k: bool,
        event_cap: int | None,
        top_k: int,
        deadline: float | None,
    ) -> tuple[RouteInsertionOption, ...]:
        key = (
            start_visit,
            route_index,
            self.versions[route_index],
            leg_candidate_limit if leg_candidate_limit is not None else -1,
            counts_toward_k,
        )
        cached = self.leg.get(key)
        if cached is not None:
            return cached
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        surface = evaluator.surface_cache.get(
            routes[route_index], start_node=problem.home_node(route_index)
        )
        cached = route_leg_insertion_options(
            problem,
            evaluator,
            routes[route_index],
            route_index,
            start_visit,
            end_visit,
            candidate_limit=leg_candidate_limit,
            option_count=min(4, top_k),
            counts_toward_k=counts_toward_k,
            event_cap=event_cap,
            surface=surface,
        )
        self.leg[key] = cached
        return cached

    def bump(self, route_index: int) -> None:
        self.versions[route_index] += 1


def _rank_task_options(
    options: Sequence[TaskInsertionOption],
) -> tuple[TaskInsertionOption, ...]:
    return tuple(
        sorted(
            options,
            key=lambda option: (
                option.local_estimated_delta.late_count,
                option.local_estimated_delta.total_lateness_min,
                option.local_estimated_delta.distance_km,
                0 if option.mode == "DIRECT" else 1,
                option.relay_id if option.relay_id is not None else 0,
                option.direct_route_index
                if option.direct_route_index is not None
                else option.inbound_route_index,
                option.direct_pickup_position
                if option.direct_pickup_position is not None
                else option.inbound_start_position,
                option.direct_delivery_position
                if option.direct_delivery_position is not None
                else option.inbound_end_position,
                option.direct_route if option.direct_route is not None else (),
            ),
        )
    )


def _compose_relay_combo(
    routes: Routes,
    in_opt: RouteInsertionOption,
    out_opt: RouteInsertionOption,
    in_start: int,
    in_end: int,
    out_start: int,
    out_end: int,
) -> Routes | None:
    """Combine one inbound and one outbound leg insertion.

    Cross-UAV handoff is always allowed.  For a same-UAV composition the
    relay pickup must follow the relay drop with at least one other event in
    between; an adjacent drop/pickup pair is dominated by DIRECT and pruned.
    """

    if in_opt.route_index != out_opt.route_index:
        candidate = list(routes)
        candidate[in_opt.route_index] = in_opt.route
        candidate[out_opt.route_index] = out_opt.route
        return tuple(candidate)
    base = routes[in_opt.route_index]
    pickup_in, delivery_in = in_opt.pickup_position, in_opt.delivery_position
    pickup_out, delivery_out = (
        out_opt.pickup_position,
        out_opt.delivery_position,
    )
    pickup_out += (pickup_out >= pickup_in) + (pickup_out >= delivery_in)
    delivery_out += (delivery_out >= pickup_in) + (delivery_out >= delivery_in)
    combined = insert_leg_pair(
        insert_leg_pair(base, in_start, in_end, pickup_in, delivery_in),
        out_start,
        out_end,
        pickup_out,
        delivery_out,
    )
    drop_index = combined.index(in_end)
    pickup_index = combined.index(out_start)
    if pickup_index <= drop_index:
        return None
    if pickup_index == drop_index + 1:
        return None
    candidate = list(routes)
    candidate[in_opt.route_index] = combined
    return tuple(candidate)


def _best_leg_options(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    start_visit: int,
    end_visit: int,
    *,
    leg_candidate_limit: int | None,
    counts_toward_k: bool,
    event_cap: int | None,
    top_k: int,
    deadline: float | None,
    repair_cache: _RepairCache | None = None,
    leg_option_cache: _LegOptionCache | None = None,
) -> tuple[RouteInsertionOption, ...]:
    options: list[RouteInsertionOption] = []
    for route_index, route in enumerate(routes):
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        if leg_option_cache is not None:
            # Persistent, route-content-keyed memo: valid across repair
            # passes because the options are pure functions of the route
            # snapshot, which only one or two routes change per iteration.
            key = (
                route,
                problem.home_node(route_index),
                start_visit,
                end_visit,
                leg_candidate_limit if leg_candidate_limit is not None else -1,
                counts_toward_k,
                event_cap if event_cap is not None else -1,
                min(4, top_k),
            )
            route_options = leg_option_cache.get(key)
            if route_options is None:
                route_options = route_leg_insertion_options(
                    problem,
                    evaluator,
                    route,
                    route_index,
                    start_visit,
                    end_visit,
                    candidate_limit=leg_candidate_limit,
                    option_count=min(4, top_k),
                    counts_toward_k=counts_toward_k,
                    event_cap=event_cap,
                )
                leg_option_cache.put(key, route_options)
        elif repair_cache is not None:
            route_options = repair_cache.leg_options(
                problem,
                evaluator,
                routes,
                start_visit,
                end_visit,
                route_index,
                leg_candidate_limit,
                counts_toward_k,
                event_cap,
                top_k,
                deadline,
            )
        else:
            route_options = route_leg_insertion_options(
                problem,
                evaluator,
                route,
                route_index,
                start_visit,
                end_visit,
                candidate_limit=leg_candidate_limit,
                option_count=min(4, top_k),
                counts_toward_k=counts_toward_k,
                event_cap=event_cap,
            )
        options.extend(route_options)
    options.sort(
        key=lambda option: (
            option.delta,
            option.route_index,
            option.pickup_position,
            option.delivery_position,
            option.route,
        )
    )
    return tuple(options[:top_k])


def _handoff_surrogate(
    problem: Problem,
    in_opt: RouteInsertionOption,
    out_opt: RouteInsertionOption,
    deadline: float,
) -> tuple[Score, float]:
    """Cheap handoff-aware score estimate for one inbound/outbound combo.

    Estimates the relay waiting as the gap between the inbound drop and the
    outbound pickup and folds its effect on the outbound delivery into the
    local score delta.  The surrogate is deliberately approximate: the
    global evaluator decides the final score of shortlisted combos.
    """

    base = in_opt.delta + out_opt.delta
    drop_time = in_opt.end_time_min
    pickup_time = out_opt.start_time_min
    if drop_time is None or pickup_time is None:
        return base, 0.0
    wait = max(0.0, drop_time - pickup_time)
    if wait <= 0.0 or out_opt.end_time_min is None:
        return base, wait
    old_late = out_opt.end_time_min - deadline
    old_term = old_late if old_late > 1e-9 else 0.0
    new_late = old_late + wait
    new_term = new_late if new_late > 1e-9 else 0.0
    if new_term > 0.0 and old_term <= 0.0:
        adjusted = base + Score(1, new_term, 0.0)
    else:
        adjusted = base + Score(0, new_term - old_term, 0.0)
    return adjusted, wait


def _build_relay_beam(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    relay_cfg: _RelaySearchConfig,
    deadline: float | None,
    repair_cache: _RepairCache | None,
    in_id: int,
    out_id: int,
) -> tuple[tuple[Score, RouteInsertionOption, RouteInsertionOption], ...]:
    """Top inbound x outbound combinations for one (task, relay) pair."""

    inbound = _best_leg_options(
        problem,
        evaluator,
        routes,
        in_id,
        -in_id,
        leg_candidate_limit=relay_cfg.leg_candidate_limit,
        counts_toward_k=False,
        event_cap=relay_cfg.event_cap,
        top_k=relay_cfg.leg_beam,
        deadline=deadline,
        repair_cache=repair_cache,
        leg_option_cache=relay_cfg.leg_option_cache,
    )
    outbound = _best_leg_options(
        problem,
        evaluator,
        routes,
        out_id,
        -out_id,
        leg_candidate_limit=relay_cfg.leg_candidate_limit,
        counts_toward_k=True,
        event_cap=relay_cfg.event_cap,
        top_k=relay_cfg.leg_beam,
        deadline=deadline,
        repair_cache=repair_cache,
        leg_option_cache=relay_cfg.leg_option_cache,
    )
    if not inbound or not outbound:
        return ()
    if relay_cfg.counters is not None:
        relay_cfg.counters.relay_beam_build_count += 1
    task_deadline = problem.task(
        problem.leg_registry[out_id].task_id
    ).deadline_min
    # Rank every inbound x outbound pair by the cheap handoff-aware
    # surrogate first, then compose only the ranked shortlist.  Composition
    # is purely a feasibility filter (same-UAV adjacency); ranking without
    # it avoids composing the whole cross product on every beam build -- the
    # hottest relay-only loop in a wall-clock run.  Selection is unchanged:
    # the first plan_beam feasible pairs in surrogate order are exactly the
    # old sort-then-truncate result.
    ranked: list[
        tuple[Score, float, RouteInsertionOption, RouteInsertionOption]
    ] = []
    for in_opt in inbound:
        for out_opt in outbound:
            score, wait = _handoff_surrogate(
                problem, in_opt, out_opt, task_deadline
            )
            ranked.append((score, wait, in_opt, out_opt))
    if not ranked:
        return ()
    ranked.sort(
        key=lambda item: (
            item[0],
            item[1],
            item[2].route_index,
            item[3].route_index,
            item[2].pickup_position,
            item[3].pickup_position,
        )
    )
    combos: list[
        tuple[Score, float, RouteInsertionOption, RouteInsertionOption]
    ] = []
    for score, wait, in_opt, out_opt in ranked:
        if (
            _compose_relay_combo(
                routes,
                in_opt,
                out_opt,
                in_id,
                -in_id,
                out_id,
                -out_id,
            )
            is None
        ):
            continue
        combos.append((score, wait, in_opt, out_opt))
        if len(combos) >= relay_cfg.plan_beam:
            break
    return tuple(
        (score, in_opt, out_opt) for score, _, in_opt, out_opt in combos
    )


def _all_plans_for_task(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    task_id: int,
    *,
    candidate_limit: int | None,
    option_count: int,
    relay_cfg: _RelaySearchConfig | None,
    deadline: float | None,
    repair_cache: _RepairCache | None = None,
    round_index: int = 0,
    probe_relay: bool = True,
) -> tuple[TaskInsertionOption, ...]:
    """Ranked surrogate-only plans for one destroyed task.

    Relay option generation is gated by ``probe_relay``: the repair loop
    decides which high-potential tasks get a relay beam each round (see
    :func:`_select_relay_probe_tasks`).  No global evaluation happens here;
    the chosen task's exact shortlist is evaluated later by
    :func:`_apply_task_exact`.
    """

    options: list[TaskInsertionOption] = []
    if relay_cfg is not None and relay_cfg.counters is not None:
        relay_cfg.counters.relay_plans_for_task_count += 1
    if repair_cache is not None:
        direct_by_route = []
        for route_index in range(len(routes)):
            direct_by_route.append(
                repair_cache.direct_options(
                    problem,
                    evaluator,
                    routes,
                    task_id,
                    route_index,
                    candidate_limit,
                    option_count,
                    deadline,
                )
            )
        direct_options = [
            option
            for route_options in direct_by_route
            for option in route_options
        ]
        direct_options.sort(
            key=lambda option: (
                option.delta.late_count,
                option.delta.total_lateness_min,
                option.delta.distance_km,
                option.route_index,
                option.pickup_position,
                option.delivery_position,
                option.route,
            )
        )
        direct_options = direct_options[:option_count]
    else:
        direct_options = _all_options_for_task(
            problem,
            evaluator,
            routes,
            task_id,
            candidate_limit,
            option_count,
            {},
            deadline,
        )
    for option in direct_options:
        options.append(
            TaskInsertionOption(
                task_id=task_id,
                mode="DIRECT",
                relay_id=None,
                local_estimated_delta=option.delta,
                direct_route_index=option.route_index,
                direct_route=option.route,
                direct_pickup_position=option.pickup_position,
                direct_delivery_position=option.delivery_position,
                end_time_min=option.end_time_min,
            )
        )
    if (
        relay_cfg is None
        or not relay_cfg.enabled
        or not problem.has_relays
        or not probe_relay
    ):
        return _rank_task_options(options)

    candidates = problem.task_relay_candidates.get(task_id, ())
    if not candidates:
        return _rank_task_options(options)
    legs_by_relay: dict[int, list[int | None]] = {}
    for leg in problem._legs_by_task.get(task_id, ()):
        pair = legs_by_relay.setdefault(leg.relay_id, [None, None])
        pair[0 if leg.kind == "RELAY_IN" else 1] = leg.id

    for relay_id in candidates[: relay_cfg.candidates_per_task]:
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        pair = legs_by_relay.get(relay_id)
        if pair is None or pair[0] is None or pair[1] is None:
            continue
        in_id, out_id = pair[0], pair[1]
        beam: tuple[
            tuple[Score, RouteInsertionOption, RouteInsertionOption], ...
        ] = ()
        if repair_cache is not None:
            combo_key = (task_id, relay_id, tuple(repair_cache.versions))
            cached_beam = repair_cache.combos.get(combo_key)
            if cached_beam is not None:
                beam = cached_beam
                if relay_cfg.counters is not None:
                    relay_cfg.counters.relay_beam_cache_hit_count += 1
        if not beam:
            beam = _build_relay_beam(
                problem,
                evaluator,
                routes,
                relay_cfg,
                deadline,
                repair_cache,
                in_id,
                out_id,
            )
            if repair_cache is not None and beam:
                repair_cache.combos[combo_key] = beam
        if not beam:
            continue
        best_delta, best_in, best_out = beam[0]
        if relay_cfg.debug:
            print(
                f"[relay-debug] task={task_id} relay={relay_id} "
                f"surrogate_delta={best_delta} "
                f"inbound_uav={best_in.route_index} "
                f"outbound_uav={best_out.route_index}"
            )
        options.append(
            TaskInsertionOption(
                task_id=task_id,
                mode="RELAY",
                relay_id=relay_id,
                local_estimated_delta=best_delta,
                inbound_route_index=best_in.route_index,
                inbound_route=best_in.route,
                inbound_start_position=best_in.pickup_position,
                inbound_end_position=best_in.delivery_position,
                outbound_route_index=best_out.route_index,
                outbound_route=best_out.route,
                outbound_start_position=best_out.pickup_position,
                outbound_end_position=best_out.delivery_position,
                end_time_min=best_out.end_time_min,
                combos=tuple(
                    (in_opt, out_opt) for _, in_opt, out_opt in beam
                ),
            )
        )
        relay_cfg.generated_relay_options += 1
    return _rank_task_options(options)


def _route_has_relay_event_at_or_after(
    problem: Problem, route: Route, from_position: int
) -> bool:
    """True when a relay visit sits at/after ``from_position``.

    A pair insertion only shifts events at or after its pickup position, so
    when no relay event is shifted the modified route cannot change any
    handoff timing and its local score delta equals the global one.
    """

    registry = problem.leg_registry
    if not registry:
        return False
    for index in range(from_position, len(route)):
        if registry.get(abs(route[index])) is not None:
            return True
    return False


def _apply_task_exact(
    problem: Problem,
    evaluator: RouteEvaluator,
    global_evaluator: GlobalRelayEvaluator,
    routes: Routes,
    options: Sequence[TaskInsertionOption],
    *,
    relay_cfg: _RelaySearchConfig,
    deadline: float | None,
    direct_exact_top_k: int = 2,
) -> tuple[Routes | None, Score | None, str | None]:
    """Exact global evaluation of the chosen task's shortlist.

    The shortlist unifies the top ``direct_exact_top_k`` DIRECT insertions
    with a station-diverse set of at most ``global_limit`` relay beam
    combinations across all candidate stations.
    A DIRECT candidate whose route carries no relay events cannot change any
    cross-UAV dependency, so its global score delta equals the exact local
    route delta and the global evaluator is skipped.
    """

    best_routes: Routes | None = None
    best_score: Score | None = None
    best_mode: str | None = None
    # The current solution is the delta base for every candidate below; one
    # cached global evaluation provides the shared base schedule.
    current_schedule = global_evaluator.evaluate(routes)
    current_score = current_schedule.score
    current_has_waiting = (
        getattr(current_schedule, "total_waiting_min", 0.0) > 1e-12
    )
    direct_added = 0
    for option in options:
        if option.mode != "DIRECT":
            continue
        if direct_added >= direct_exact_top_k:
            break
        direct_added += 1
        candidate = list(routes)
        candidate[option.direct_route_index] = option.direct_route
        candidate_routes = tuple(candidate)
        if relay_cfg.counters is not None:
            relay_cfg.counters.repair_exact_evaluation_count += 1
        local_delta = (
            evaluator.evaluate(
                option.direct_route,
                start_node=problem.home_node(option.direct_route_index),
            ).score
            - evaluator.evaluate(
                routes[option.direct_route_index],
                start_node=problem.home_node(option.direct_route_index),
            ).score
        )
        affects_handoff_timing = _route_has_relay_event_at_or_after(
            problem, option.direct_route, option.direct_pickup_position
        )
        if not affects_handoff_timing:
            # No relay event is shifted, so no handoff timing changes and
            # the local route delta is exactly the global score delta.
            candidate_score = current_score + local_delta
        else:
            # Do not prune on ``local_delta`` here.  An insertion before a
            # relay pickup can consume idle waiting, so the local delta is
            # not generally a lower bound on the global schedule delta.  It
            # is a valid bound when the current schedule has no waiting at
            # all: in that case global and route-local clocks coincide.
            if (
                best_score is not None
                and not current_has_waiting
                and current_score + local_delta >= best_score
            ):
                continue
            schedule = global_evaluator.evaluate_delta(
                routes, current_schedule, candidate_routes
            )
            relay_cfg.global_evaluation_count += 1
            if not schedule.feasible:
                continue
            candidate_score = schedule.score
        if best_score is None or candidate_score < best_score:
            best_score = candidate_score
            best_routes = candidate_routes
            best_mode = "DIRECT"
    relay_options = [option for option in options if option.mode == "RELAY"]
    relay_shortlist: list[
        tuple[
            Score,
            float,
            int,
            int,
            TaskInsertionOption,
            RouteInsertionOption,
            RouteInsertionOption,
            int,
            int,
        ]
    ] = []
    for option_index, relay_option in enumerate(relay_options):
        legs = tuple(
            leg
            for leg in problem._legs_by_task.get(relay_option.task_id, ())
            if leg.relay_id == relay_option.relay_id
        )
        in_id = next(leg.id for leg in legs if leg.kind == "RELAY_IN")
        out_id = next(leg.id for leg in legs if leg.kind == "RELAY_OUT")
        task_deadline = problem.task(relay_option.task_id).deadline_min
        for combo_index, (in_opt, out_opt) in enumerate(relay_option.combos):
            estimate, wait = _handoff_surrogate(
                problem, in_opt, out_opt, task_deadline
            )
            relay_shortlist.append(
                (
                    estimate,
                    wait,
                    option_index,
                    combo_index,
                    relay_option,
                    in_opt,
                    out_opt,
                    in_id,
                    out_id,
                )
            )

    if relay_shortlist:
        relay_shortlist.sort(
            key=lambda item: (
                item[0],
                item[1],
                item[4].relay_id,
                item[5].route_index,
                item[6].route_index,
                item[5].pickup_position,
                item[6].pickup_position,
            )
        )
        # Reserve one head candidate per station whenever the budget permits,
        # then spend the remaining slots globally by surrogate rank.  The old
        # code silently ignored every station except the first ranked one.
        heads: list[tuple] = []
        rest: list[tuple] = []
        seen_options: set[int] = set()
        for item in relay_shortlist:
            option_index = item[2]
            if option_index not in seen_options:
                seen_options.add(option_index)
                heads.append(item)
            else:
                rest.append(item)
        ordered_shortlist = heads + rest
        evaluated = 0
        for (
            _,
            _,
            _,
            _,
            relay_option,
            in_opt,
            out_opt,
            in_id,
            out_id,
        ) in ordered_shortlist:
            if evaluated >= relay_cfg.global_limit:
                break
            if deadline is not None and perf_counter() >= deadline:
                raise _SearchDeadlineReached
            candidate = _compose_relay_combo(
                routes, in_opt, out_opt, in_id, -in_id, out_id, -out_id
            )
            if candidate is None:
                continue
            if best_score is not None:
                if not current_has_waiting:
                    # With a zero-wait current schedule, the exact sum of
                    # changed route scores is a safe lower bound: adding the
                    # new handoff can only delay the global candidate.  This
                    # recovers the useful pruning without assuming that the
                    # handoff surrogate itself is a bound.
                    local_delta = Score(0, 0.0, 0.0)
                    for route_index in {
                        in_opt.route_index,
                        out_opt.route_index,
                    }:
                        local_delta += (
                            evaluator.evaluate(
                                candidate[route_index],
                                start_node=problem.home_node(route_index),
                            ).score
                            - evaluator.evaluate(
                                routes[route_index],
                                start_node=problem.home_node(route_index),
                            ).score
                        )
                    if current_score + local_delta >= best_score:
                        continue
            evaluated += 1
            schedule = global_evaluator.evaluate_delta(
                routes, current_schedule, candidate
            )
            relay_cfg.global_evaluation_count += 1
            relay_cfg.candidate_evaluation_count += 1
            if relay_cfg.counters is not None:
                relay_cfg.counters.repair_exact_evaluation_count += 1
            if not schedule.feasible:
                continue
            if best_score is None or schedule.score < best_score:
                best_score = schedule.score
                best_routes = candidate
                best_mode = "RELAY"
    if best_routes is not None:
        return best_routes, best_score, best_mode
    # Robustness fallback: first feasible plan in ranked order.
    for option in options:
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        if option.mode == "DIRECT":
            candidate = list(routes)
            candidate[option.direct_route_index] = option.direct_route
            candidate_routes = tuple(candidate)
        else:
            legs = tuple(
                leg
                for leg in problem._legs_by_task.get(option.task_id, ())
                if leg.relay_id == option.relay_id
            )
            in_id = next(leg.id for leg in legs if leg.kind == "RELAY_IN")
            out_id = next(leg.id for leg in legs if leg.kind == "RELAY_OUT")
            candidate_routes = None
            for in_opt, out_opt in option.combos:
                candidate_routes = _compose_relay_combo(
                    routes,
                    in_opt,
                    out_opt,
                    in_id,
                    -in_id,
                    out_id,
                    -out_id,
                )
                if candidate_routes is not None:
                    break
            if candidate_routes is None:
                continue
        schedule = global_evaluator.evaluate_delta(
            routes, current_schedule, candidate_routes
        )
        relay_cfg.global_evaluation_count += 1
        if schedule.feasible:
            return candidate_routes, schedule.score, option.mode
    return None, None, None


def _select_relay_probe_tasks(
    problem: Problem,
    remaining: set[int],
    per_task: Mapping[int, tuple[TaskInsertionOption, ...]],
    relay_cfg: _RelaySearchConfig,
    round_index: int,
) -> set[int]:
    """Pick the high-potential tasks that get a relay beam this round.

    The primary criterion is relay potential derived from the cheap DIRECT
    ranking: whether the direct insertion goes late, its lateness, the
    detour ratio of the best relay candidate, the spatial span and the
    deadline slack.  The old ``sample_every`` modulo rule survives only as a
    bounded fallback exploration step.
    """

    station_by_id = {
        station.id: station.point for station in problem.relay_stations
    }
    potential: list[tuple[tuple[float, ...], int]] = []
    for task_id in sorted(remaining):
        candidates = problem.task_relay_candidates.get(task_id, ())
        if not candidates:
            continue
        task = problem.task(task_id)
        options = per_task.get(task_id)
        if not options:
            continue
        best_delta = options[0].local_estimated_delta
        direct_late = 1 if best_delta.late_count > 0 else 0
        span = max(task.pickup.distance_to(task.delivery), 1e-9)
        detour = min(
            (
                task.pickup.distance_to(station_by_id[relay_id])
                + station_by_id[relay_id].distance_to(task.delivery)
            )
            / span
            for relay_id in candidates
        )
        slack = task.deadline_min - problem.direct_completion_min(task_id)
        potential.append(
            (
                (
                    float(direct_late),
                    best_delta.total_lateness_min,
                    -detour,
                    span,
                    -slack,
                ),
                task_id,
            )
        )
    potential.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    eligible = len(potential)
    if eligible == 0:
        return set()
    count = max(
        relay_cfg.probe_min,
        min(
            relay_cfg.probe_max_tasks,
            ceil(relay_cfg.probe_fraction * eligible),
        ),
    )
    count = min(count, eligible)
    selected = [task_id for _, task_id in potential[:count]]
    selected_set = set(selected)
    # Fallback modulo exploration, still bounded by probe_max_tasks.
    if len(selected) < relay_cfg.probe_max_tasks:
        for _, task_id in potential[count:]:
            if (
                (task_id + round_index) % relay_cfg.sample_every == 0
                and task_id not in selected_set
            ):
                selected.append(task_id)
                selected_set.add(task_id)
            if len(selected) >= relay_cfg.probe_max_tasks:
                break
    return selected_set


def _repair_relay(
    problem: Problem,
    evaluator: RouteEvaluator,
    global_evaluator: GlobalRelayEvaluator,
    partial_routes: Routes,
    removed_task_ids: Iterable[int],
    strategy: str,
    candidate_limit: int | None,
    use_deadline_risk: bool,
    relay_cfg: _RelaySearchConfig,
    deadline: float | None,
    repair_rank_candidate_limit: int | None = None,
    repair_exact_candidate_limit: int | None = None,
    direct_exact_top_k: int = 2,
) -> Routes:
    """Repair with relay-aware plans: DIRECT versus RELAY per task.

    Stage A ranks every remaining task with a cheap candidate limit (no
    relay beams, no materialized shortlist beyond the top options).  Stage B
    re-ranks only the chosen task with the exact candidate limit and a
    relay beam, then evaluates a bounded exact shortlist.
    """

    routes = tuple(tuple(route) for route in partial_routes)
    remaining = set(removed_task_ids)
    regret_k = 3 if strategy == "regret3" else 2
    round_index = 0
    repair_cache = _RepairCache(len(routes))
    counters = relay_cfg.counters
    rank_limit = (
        repair_rank_candidate_limit
        if candidate_limit is not None
        else candidate_limit
    )
    exact_limit = (
        repair_exact_candidate_limit
        if candidate_limit is not None
        else candidate_limit
    )

    while remaining:
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        option_count = regret_k if strategy.startswith("regret") else 1
        per_task: dict[int, tuple[TaskInsertionOption, ...]] = {}
        # Stage A: cheap DIRECT ranking for every remaining task.
        for task_id in sorted(remaining):
            options = _all_plans_for_task(
                problem,
                evaluator,
                routes,
                task_id,
                candidate_limit=rank_limit,
                option_count=max(option_count, 2),
                relay_cfg=relay_cfg,
                deadline=deadline,
                repair_cache=repair_cache,
                round_index=round_index,
                probe_relay=False,
            )
            if not options:
                raise RuntimeError(f"修复阶段无法重新插入任务 {task_id}")
            per_task[task_id] = options
        if counters is not None:
            counters.repair_rank_evaluation_count += len(remaining)
        # Relay probe: only high-potential tasks build relay beams.
        probe_tasks = _select_relay_probe_tasks(
            problem, remaining, per_task, relay_cfg, round_index
        )
        for task_id in sorted(probe_tasks):
            options = _all_plans_for_task(
                problem,
                evaluator,
                routes,
                task_id,
                candidate_limit=rank_limit,
                option_count=max(option_count, 2),
                relay_cfg=relay_cfg,
                deadline=deadline,
                repair_cache=repair_cache,
                round_index=round_index,
                probe_relay=True,
            )
            if options:
                per_task[task_id] = options

        def predicted_risk(task_id: int) -> float:
            if not use_deadline_risk:
                return 0.0
            choice = per_task[task_id][0]
            if choice.end_time_min is not None:
                delivered_at = choice.end_time_min
            elif choice.mode == "DIRECT":
                delivered_at = evaluator.evaluate(
                    choice.direct_route,
                    start_node=problem.home_node(
                        choice.direct_route_index
                    ),
                ).delivery_times_min[task_id]
            else:
                outbound_route = choice.combos[0][1].route
                delivered_at = evaluator.evaluate(
                    outbound_route,
                    start_node=problem.home_node(
                        choice.outbound_route_index
                    ),
                ).delivery_times_min[task_id]
            return _deadline_risk(
                delivered_at,
                problem.task(task_id).deadline_min,
            )

        def risk_guided_min(key):
            shortlist = sorted(remaining, key=key)[:3]
            return min(
                shortlist,
                key=lambda task_id: (-predicted_risk(task_id), key(task_id)),
            )

        if strategy == "greedy":

            def greedy_key(task_id: int):
                return (
                    per_task[task_id][0].local_estimated_delta,
                    problem.task(task_id).deadline_min,
                    task_id,
                )

            chosen_task = min(remaining, key=greedy_key)
        elif strategy in {"deadline", "slack"}:
            if strategy == "deadline":
                order_key = lambda task_id: (
                    problem.task(task_id).deadline_min,
                    task_id,
                )
            else:
                order_key = lambda task_id: (
                    problem.task(task_id).deadline_min
                    - problem.direct_completion_min(task_id),
                    task_id,
                )
            chosen_task = (
                risk_guided_min(order_key)
                if use_deadline_risk
                else min(remaining, key=order_key)
            )
        elif strategy in {"regret2", "regret3"}:

            def regret_key(
                task_id: int,
            ) -> tuple[float, float, float, float, int]:
                options = per_task[task_id]
                if len(options) < regret_k:
                    regret = (float("inf"), float("inf"), float("inf"))
                else:
                    best = options[0].local_estimated_delta
                    alternative = options[regret_k - 1].local_estimated_delta
                    regret = (
                        float(alternative.late_count - best.late_count),
                        alternative.total_lateness_min
                        - best.total_lateness_min,
                        alternative.distance_km - best.distance_km,
                    )
                return (
                    regret[0],
                    regret[1],
                    regret[2],
                    -problem.task(task_id).deadline_min,
                    -task_id,
                )

            chosen_task = max(remaining, key=regret_key)
        else:
            raise ValueError(f"未知修复算子 {strategy}")

        # Stage B: exact refinement of the chosen task only.
        chosen_options = _all_plans_for_task(
            problem,
            evaluator,
            routes,
            chosen_task,
            candidate_limit=exact_limit,
            option_count=max(option_count, 2),
            relay_cfg=relay_cfg,
            deadline=deadline,
            repair_cache=repair_cache,
            round_index=round_index,
            probe_relay=True,
        )
        if not chosen_options:
            raise RuntimeError(f"修复阶段无法重新插入任务 {chosen_task}")
        candidate_routes, _, chosen_mode = _apply_task_exact(
            problem,
            evaluator,
            global_evaluator,
            routes,
            chosen_options,
            relay_cfg=relay_cfg,
            deadline=deadline,
            direct_exact_top_k=direct_exact_top_k,
        )
        applied = candidate_routes is not None
        if applied:
            for route_index in range(len(routes)):
                if candidate_routes[route_index] != routes[route_index]:
                    repair_cache.bump(route_index)
            if chosen_mode == "RELAY":
                relay_cfg.selected_relay_options += 1
            routes = candidate_routes
            if relay_cfg.debug and chosen_mode == "RELAY":
                print(
                    f"[relay-debug] inserted task={chosen_task} mode=RELAY"
                )
        if not applied:
            raise RuntimeError(f"修复阶段无法重新插入任务 {chosen_task}")
        remaining.remove(chosen_task)
        round_index += 1
    return routes


@dataclass(frozen=True, slots=True)
class _BundleInsertionOption:
    delta: Score
    route_index: int
    route: Route
    task_ids: tuple[int, int]


def _bundle_insertion_options(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    task_ids: tuple[int, int],
    *,
    candidate_limit: int | None,
    bundle_candidate_limit: int,
    deadline: float | None,
) -> tuple[_BundleInsertionOption, ...]:
    local_limit = (
        bundle_candidate_limit
        if candidate_limit is None
        else min(candidate_limit, bundle_candidate_limit)
    )
    beam_width = max(2, min(6, bundle_candidate_limit))
    options: dict[tuple[int, Route], _BundleInsertionOption] = {}
    for route_index, route in enumerate(routes):
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        task_count = sum(visit > 0 for visit in route)
        if task_count + 2 > problem.max_tasks_per_drone:
            continue
        old_score = evaluator.evaluate(
            route, start_node=problem.home_node(route_index)
        ).score
        for first_task, second_task in (task_ids, task_ids[::-1]):
            first_options = route_insertion_options(
                problem,
                evaluator,
                route,
                route_index,
                first_task,
                candidate_limit=local_limit,
                option_count=beam_width,
            )
            for first in first_options:
                if deadline is not None and perf_counter() >= deadline:
                    raise _SearchDeadlineReached
                second_options = route_insertion_options(
                    problem,
                    evaluator,
                    first.route,
                    route_index,
                    second_task,
                    candidate_limit=local_limit,
                    option_count=beam_width,
                )
                for second in second_options:
                    option = _BundleInsertionOption(
                        evaluator.evaluate(
                            second.route,
                            start_node=problem.home_node(route_index),
                        ).score
                        - old_score,
                        route_index,
                        second.route,
                        tuple(sorted(task_ids)),
                    )
                    key = (route_index, second.route)
                    existing = options.get(key)
                    if existing is None or option.delta < existing.delta:
                        options[key] = option
    return tuple(
        sorted(
            options.values(),
            key=lambda option: (
                option.delta,
                option.route_index,
                option.route,
            ),
        )
    )


def cluster_regret_repair(
    problem: Problem,
    evaluator: RouteEvaluator,
    partial_routes: Routes,
    removed_task_ids: Iterable[int],
    *,
    candidate_limit: int | None,
    original_route_by_task: dict[int, int] | None = None,
    use_deadline_risk: bool = True,
    bundle_candidate_limit: int = 12,
    pair_limit: int = 6,
    deadline: float | None = None,
) -> Routes:
    """Repair related task pairs jointly with a bounded bundle-regret rule."""

    routes = tuple(tuple(route) for route in partial_routes)
    remaining = set(removed_task_ids)
    original_routes = original_route_by_task or {}

    def relatedness(left_task: int, right_task: int) -> tuple[float, int]:
        left = problem.task(left_task)
        right = problem.task(right_task)
        same_route_bonus = (
            -2.0
            if left_task in original_routes
            and right_task in original_routes
            and original_routes[left_task] == original_routes[right_task]
            else 0.0
        )
        value = (
            left.pickup.distance_to(right.pickup)
            + 0.5 * left.delivery.distance_to(right.delivery)
            + 0.15 * abs(left.deadline_min - right.deadline_min)
            + same_route_bonus
        )
        return value, right_task

    while len(remaining) >= 2:
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        candidate_pairs: set[tuple[int, int]] = set()
        for task_id in sorted(remaining):
            partner = min(
                (other for other in remaining if other != task_id),
                key=lambda other: relatedness(task_id, other),
            )
            candidate_pairs.add(tuple(sorted((task_id, partner))))
        ranked_pairs = sorted(
            candidate_pairs,
            key=lambda pair: (relatedness(*pair), pair),
        )[:pair_limit]

        choices: list[
            tuple[tuple[float, ...], _BundleInsertionOption]
        ] = []
        for pair in ranked_pairs:
            options = _bundle_insertion_options(
                problem,
                evaluator,
                routes,
                pair,
                candidate_limit=candidate_limit,
                bundle_candidate_limit=bundle_candidate_limit,
                deadline=deadline,
            )
            if not options:
                continue
            best = options[0]
            alternative = next(
                (
                    option
                    for option in options[1:]
                    if option.route_index != best.route_index
                ),
                None,
            )
            if alternative is None:
                regret = (float("inf"),) * 3
            else:
                alternative_delta = alternative.delta
                regret = (
                    float(alternative_delta.late_count - best.delta.late_count),
                    alternative_delta.total_lateness_min
                    - best.delta.total_lateness_min,
                    alternative_delta.distance_km - best.delta.distance_km,
                )
            metrics = evaluator.evaluate(
                best.route, start_node=problem.home_node(best.route_index)
            )
            risk = max(
                (
                    _deadline_risk(
                        metrics.delivery_times_min[task_id],
                        problem.task(task_id).deadline_min,
                    )
                    for task_id in pair
                ),
                default=0.0,
            )
            choices.append(
                (
                    (
                        regret[0],
                        risk if use_deadline_risk else 0.0,
                        regret[1],
                        regret[2],
                        -relatedness(*pair)[0],
                        -float(pair[0]),
                        -float(pair[1]),
                    ),
                    best,
                )
            )
        if not choices:
            break
        _, chosen = max(choices, key=lambda item: item[0])
        mutable = list(routes)
        mutable[chosen.route_index] = chosen.route
        routes = tuple(mutable)
        remaining.difference_update(chosen.task_ids)

    if remaining:
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        routes = _repair(
            problem,
            evaluator,
            routes,
            remaining,
            "regret2",
            candidate_limit,
            use_deadline_risk=use_deadline_risk,
            deadline=deadline,
        )
    return routes


def _repair(
    problem: Problem,
    evaluator: RouteEvaluator,
    partial_routes: Routes,
    removed_task_ids: Iterable[int],
    strategy: str,
    candidate_limit: int | None,
    use_deadline_risk: bool = False,
    original_route_by_task: dict[int, int] | None = None,
    cluster_bundle_candidate_limit: int = 12,
    cluster_pair_limit: int = 6,
    deadline: float | None = None,
    *,
    relay_cfg: _RelaySearchConfig | None = None,
    global_evaluator: GlobalRelayEvaluator | None = None,
    repair_rank_candidate_limit: int | None = None,
    repair_exact_candidate_limit: int | None = None,
    direct_exact_top_k: int = 2,
    home_bias: bool = False,
) -> Routes:
    if (
        relay_cfg is not None
        and relay_cfg.enabled
        and problem.has_relays
    ):
        relay_strategy = (
            "regret2" if strategy == "cluster_regret" else strategy
        )
        return _repair_relay(
            problem,
            evaluator,
            global_evaluator,
            partial_routes,
            removed_task_ids,
            relay_strategy,
            candidate_limit,
            use_deadline_risk,
            relay_cfg,
            deadline,
            repair_rank_candidate_limit,
            repair_exact_candidate_limit,
            direct_exact_top_k,
        )
    if strategy == "cluster_regret":
        return cluster_regret_repair(
            problem,
            evaluator,
            partial_routes,
            removed_task_ids,
            candidate_limit=candidate_limit,
            original_route_by_task=original_route_by_task,
            use_deadline_risk=use_deadline_risk,
            bundle_candidate_limit=cluster_bundle_candidate_limit,
            pair_limit=cluster_pair_limit,
            deadline=deadline,
        )
    routes = tuple(tuple(route) for route in partial_routes)
    remaining = set(removed_task_ids)
    repair_cache = _RepairCache(len(routes))
    regret_k = 3 if strategy == "regret3" else 2
    counters = evaluator.counters
    rank_limit = (
        repair_rank_candidate_limit
        if candidate_limit is not None
        else candidate_limit
    )
    exact_limit = (
        repair_exact_candidate_limit
        if candidate_limit is not None
        else candidate_limit
    )

    def ranked_options(task_id: int, candidate_limit: int | None):
        direct_by_route = [
            repair_cache.direct_options(
                problem,
                evaluator,
                routes,
                task_id,
                route_index,
                candidate_limit,
                option_count,
                deadline,
            )
            for route_index in range(len(routes))
        ]
        merged = [
            option
            for route_options in direct_by_route
            for option in route_options
        ]
        if home_bias:
            # Home-proximity tiebreak: among equally-scoring insertions,
            # prefer the route whose home is closest to the task pickup.
            # Only an initial-construction bias; never part of the objective.
            merged.sort(
                key=lambda option: (
                    option.delta.late_count,
                    option.delta.total_lateness_min,
                    option.delta.distance_km,
                    problem.distance(
                        None,
                        task_id,
                        from_node=problem.home_node(option.route_index),
                    ),
                    option.route_index,
                    option.pickup_position,
                    option.delivery_position,
                    option.route,
                )
            )
        else:
            merged.sort(
                key=lambda option: (
                    option.delta.late_count,
                    option.delta.total_lateness_min,
                    option.delta.distance_km,
                    option.route_index,
                    option.pickup_position,
                    option.delivery_position,
                    option.route,
                )
            )
        return tuple(merged[:option_count])

    while remaining:
        if deadline is not None and perf_counter() >= deadline:
            raise _SearchDeadlineReached
        option_count = regret_k if strategy.startswith("regret") else 1
        per_task: dict[int, tuple[RouteInsertionOption, ...]] = {}
        for task_id in sorted(remaining):
            options = ranked_options(task_id, rank_limit)
            if not options:
                raise RuntimeError(f"修复阶段无法重新插入任务 {task_id}")
            per_task[task_id] = options
        if counters is not None:
            counters.repair_rank_evaluation_count += len(remaining)

        def predicted_risk(task_id: int) -> float:
            if not use_deadline_risk:
                return 0.0
            choice = per_task[task_id][0]
            if choice.end_time_min is not None:
                delivered_at = choice.end_time_min
            else:
                delivered_at = evaluator.evaluate(
                    choice.route,
                    start_node=problem.home_node(choice.route_index),
                ).delivery_times_min[task_id]
            return _deadline_risk(
                delivered_at,
                problem.task(task_id).deadline_min,
            )

        def risk_guided_min(key):
            shortlist = sorted(remaining, key=key)[:3]
            return min(
                shortlist,
                key=lambda task_id: (-predicted_risk(task_id), key(task_id)),
            )

        if strategy == "greedy":
            def greedy_key(task_id: int):
                delta = per_task[task_id][0].delta
                return (
                    delta.late_count,
                    delta.total_lateness_min,
                    delta.distance_km,
                    problem.task(task_id).deadline_min,
                    task_id,
                )

            chosen_task = min(remaining, key=greedy_key)
        elif strategy in {"deadline", "slack"}:
            if strategy == "deadline":
                order_key = lambda task_id: (
                    problem.task(task_id).deadline_min,
                    task_id,
                )
            else:
                order_key = lambda task_id: (
                    problem.task(task_id).deadline_min
                    - problem.direct_completion_min(task_id),
                    task_id,
                )
            chosen_task = (
                risk_guided_min(order_key)
                if use_deadline_risk
                else min(remaining, key=order_key)
            )
        elif strategy in {"regret2", "regret3"}:

            def regret_key(
                task_id: int,
            ) -> tuple[float, float, float, float, int]:
                options = per_task[task_id]
                if len(options) < regret_k:
                    regret = (float("inf"), float("inf"), float("inf"))
                else:
                    best = options[0].delta
                    alternative = options[regret_k - 1].delta
                    regret = (
                        float(alternative.late_count - best.late_count),
                        alternative.total_lateness_min - best.total_lateness_min,
                        alternative.distance_km - best.distance_km,
                    )
                return (
                    regret[0],
                    regret[1],
                    regret[2],
                    -problem.task(task_id).deadline_min,
                    -task_id,
                )

            chosen_task = max(remaining, key=regret_key)
        else:
            raise ValueError(f"未知修复算子 {strategy}")

        # Stage B: the chosen task alone is re-ranked at exact precision.
        exact_options = ranked_options(chosen_task, exact_limit)
        if not exact_options:
            raise RuntimeError(f"修复阶段无法重新插入任务 {chosen_task}")
        if counters is not None:
            counters.repair_exact_evaluation_count += 1
        choice = exact_options[0]
        mutable = list(routes)
        mutable[choice.route_index] = choice.route
        routes = tuple(mutable)
        repair_cache.bump(choice.route_index)
        remaining.remove(chosen_task)
    return routes


def _seed_home_first_tasks(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
) -> tuple[Routes, tuple[int, ...]]:
    """Insert one adjacent pickup-delivery pair per drone, choosing the task
    whose pickup is nearest that drone's home (起点感知播种).

    Returns the seeded routes and the seeded task ids so the caller can
    exclude them from the regret-2 repair pool.
    """

    remaining = set(problem.task_ids)
    mutable = [list(route) for route in routes]
    seeded: list[int] = []
    for route_index in range(problem.drone_count):
        if not remaining:
            break
        home_node = problem.home_node(route_index)
        task_id = min(
            remaining,
            key=lambda tid: problem.distance(None, tid, from_node=home_node),
        )
        mutable[route_index] = [task_id, -task_id]
        seeded.append(task_id)
        remaining.remove(task_id)
    return tuple(tuple(route) for route in mutable), tuple(seeded)


def construct_regret_initial(
    problem: Problem,
    *,
    candidate_limit: int | None = 48,
    relay_cfg: _RelaySearchConfig | None = None,
    global_evaluator: GlobalRelayEvaluator | None = None,
    repair_rank_candidate_limit: int | None = None,
    repair_exact_candidate_limit: int | None = None,
    direct_exact_top_k: int = 2,
    home_seed: bool = False,
    home_bias: bool = False,
) -> SolverResult:
    """Construct a complete deterministic regret-2 initial solution.

    With a relay network attached, each task is inserted by comparing DIRECT
    against its relay plans, so the initial solution itself can use relays.
    With ``home_seed``, every drone first receives the task whose pickup is
    nearest its home before the regret-2 repair fills the remaining pool.
    """

    started = perf_counter()
    evaluator = RouteEvaluator(problem)
    routes: Routes = tuple(() for _ in range(problem.drone_count))
    remaining_task_ids: tuple[int, ...] = problem.task_ids
    if home_seed:
        routes, seeded_ids = _seed_home_first_tasks(problem, evaluator, routes)
        if seeded_ids:
            seeded_set = set(seeded_ids)
            remaining_task_ids = tuple(
                task_id
                for task_id in problem.task_ids
                if task_id not in seeded_set
            )
    routes = _repair(
        problem,
        evaluator,
        routes,
        remaining_task_ids,
        "regret2",
        candidate_limit,
        relay_cfg=relay_cfg,
        global_evaluator=global_evaluator,
        repair_rank_candidate_limit=repair_rank_candidate_limit,
        repair_exact_candidate_limit=repair_exact_candidate_limit,
        direct_exact_top_k=direct_exact_top_k,
        home_bias=home_bias,
    )
    evaluation = evaluate_solution(problem, routes)
    if not evaluation.valid:
        raise RuntimeError(f"初始解非法: {evaluation.violations}")
    return SolverResult(
        routes,
        evaluation,
        runtime_seconds=perf_counter() - started,
        metadata=MappingProxyType({"method": "regret2"}),
    )


def _finish_constructor(
    problem: Problem, routes: Routes, started: float, method: str
) -> SolverResult:
    evaluation = evaluate_solution(problem, routes)
    if not evaluation.valid:
        raise RuntimeError(f"{method} 初始解非法: {evaluation.violations}")
    return SolverResult(
        routes,
        evaluation,
        runtime_seconds=perf_counter() - started,
        metadata=MappingProxyType({"method": method}),
    )


def construct_edd_adjacent(problem: Problem) -> SolverResult:
    """Earliest-deadline-first baseline with adjacent pickup-delivery pairs."""

    started = perf_counter()
    evaluator = RouteEvaluator(problem)
    routes: Routes = tuple(() for _ in range(problem.drone_count))
    for task in sorted(problem.tasks, key=lambda item: (item.deadline_min, item.id)):
        choices: list[tuple[Score, int, Route]] = []
        for route_index, route in enumerate(routes):
            if sum(visit > 0 for visit in route) >= problem.max_tasks_per_drone:
                continue
            candidate = route + (task.id, -task.id)
            delta = (
                evaluator.evaluate(
                    candidate, start_node=problem.home_node(route_index)
                ).score
                - evaluator.evaluate(
                    route, start_node=problem.home_node(route_index)
                ).score
            )
            choices.append((delta, route_index, candidate))
        if not choices:
            raise RuntimeError(f"任务 {task.id} 没有可用路线槽位")
        _, route_index, candidate = min(choices)
        mutable = list(routes)
        mutable[route_index] = candidate
        routes = tuple(mutable)
    return _finish_constructor(problem, routes, started, "edd-adjacent")


def construct_nearest_adjacent(problem: Problem) -> SolverResult:
    """Distance-nearest adjacent-pair baseline."""

    started = perf_counter()
    evaluator = RouteEvaluator(problem)
    routes: Routes = tuple(() for _ in range(problem.drone_count))
    remaining = set(problem.task_ids)
    while remaining:
        choices: list[tuple[float, float, int, int, Route]] = []
        for task_id in sorted(remaining):
            for route_index, route in enumerate(routes):
                if sum(visit > 0 for visit in route) >= problem.max_tasks_per_drone:
                    continue
                candidate = route + (task_id, -task_id)
                delta = (
                    evaluator.evaluate(
                        candidate,
                        start_node=problem.home_node(route_index),
                    ).score
                    - evaluator.evaluate(
                        route, start_node=problem.home_node(route_index)
                    ).score
                )
                choices.append(
                    (
                        delta.distance_km,
                        problem.task(task_id).deadline_min,
                        task_id,
                        route_index,
                        candidate,
                    )
                )
        if not choices:
            raise RuntimeError("最近邻构造没有可用路线槽位")
        _, _, task_id, route_index, candidate = min(choices)
        mutable = list(routes)
        mutable[route_index] = candidate
        routes = tuple(mutable)
        remaining.remove(task_id)
    return _finish_constructor(problem, routes, started, "nearest-adjacent")


def construct_greedy_initial(
    problem: Problem, *, candidate_limit: int | None = 48
) -> SolverResult:
    """EDD-ordered full-position lexicographic greedy insertion baseline."""

    started = perf_counter()
    evaluator = RouteEvaluator(problem)
    routes: Routes = tuple(() for _ in range(problem.drone_count))
    cache: dict[
        tuple[int, int, Route, int | None, int], tuple[RouteInsertionOption, ...]
    ] = {}
    for task in sorted(problem.tasks, key=lambda item: (item.deadline_min, item.id)):
        choices = _all_options_for_task(
            problem,
            evaluator,
            routes,
            task.id,
            candidate_limit,
            1,
            cache,
        )
        if not choices:
            raise RuntimeError(f"任务 {task.id} 没有可用插入位置")
        choice = choices[0]
        mutable = list(routes)
        mutable[choice.route_index] = choice.route
        routes = tuple(mutable)
    return _finish_constructor(problem, routes, started, "greedy-full-position")


@dataclass(frozen=True, slots=True)
class _RouteColumn:
    tasks: frozenset[int]
    route: Route
    score: Score


class _RoutePool:
    """Restricted set-partitioning reinforcement solved by bounded DFS."""

    def __init__(self, problem: Problem, evaluator: RouteEvaluator) -> None:
        self.problem = problem
        self.evaluator = evaluator
        self.columns: dict[frozenset[int], _RouteColumn] = {}
        self.last_nodes = 0

    def add(self, routes: Routes) -> None:
        for route_index, route in enumerate(routes):
            tasks = frozenset(visit for visit in route if visit > 0)
            if not tasks:
                continue
            column = _RouteColumn(
                tasks,
                route,
                self.evaluator.evaluate(
                    route, start_node=self.problem.home_node(route_index)
                ).score,
            )
            existing = self.columns.get(tasks)
            if existing is None or (column.score, column.route) < (
                existing.score,
                existing.route,
            ):
                self.columns[tasks] = column

    def recombine(
        self,
        incumbent: Routes,
        node_limit: int,
        deadline: float | None = None,
    ) -> Routes:
        self.add(incumbent)
        if deadline is not None and perf_counter() >= deadline:
            return incumbent
        columns = tuple(
            sorted(self.columns.values(), key=lambda col: (col.score, col.route))
        )
        by_task: dict[int, list[_RouteColumn]] = {
            task_id: [] for task_id in self.problem.task_ids
        }
        for column in columns:
            for task_id in column.tasks:
                by_task[task_id].append(column)

        incumbent_score = routes_score(self.evaluator, incumbent)
        best_score = incumbent_score
        best_routes = incumbent
        nodes = 0

        def dfs(
            uncovered: frozenset[int],
            selected: tuple[_RouteColumn, ...],
            partial_score: Score,
        ) -> None:
            nonlocal best_score, best_routes, nodes
            if nodes >= node_limit or (
                deadline is not None and perf_counter() >= deadline
            ):
                return
            nodes += 1
            if not uncovered:
                ordered = tuple(
                    column.route
                    for column in sorted(
                        selected,
                        key=lambda col: (min(col.tasks), col.route),
                    )
                )
                candidate = ordered + tuple(
                    () for _ in range(self.problem.drone_count - len(ordered))
                )
                # Exact home-aware score at the leaf; ``partial_score`` is a
                # source-home bound kept only for pruning.
                exact = routes_score(self.evaluator, candidate)
                if exact < best_score:
                    best_score = exact
                    best_routes = candidate
                return
            if len(selected) >= self.problem.drone_count or partial_score >= best_score:
                return

            task_id = min(
                uncovered,
                key=lambda task: sum(
                    column.tasks <= uncovered for column in by_task[task]
                ),
            )
            compatible = [
                column
                for column in by_task[task_id]
                if column.tasks <= uncovered
            ]
            for column in compatible:
                dfs(
                    uncovered - column.tasks,
                    selected + (column,),
                    partial_score + column.score,
                )
                if nodes >= node_limit or (
                    deadline is not None and perf_counter() >= deadline
                ):
                    break

        dfs(frozenset(self.problem.task_ids), (), Score(0, 0.0, 0.0))
        self.last_nodes = nodes
        return best_routes


def _local_search_task_order(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    use_deadline_risk: bool,
) -> tuple[int, ...]:
    priorities: list[tuple[tuple[float, ...], int]] = []
    for route_index, route in enumerate(routes):
        metrics = evaluator.evaluate(
            route, start_node=problem.home_node(route_index)
        )
        for task_id, delivered_at in metrics.delivery_times_min.items():
            deadline = problem.task(task_id).deadline_min
            reduced = tuple(visit for visit in route if abs(visit) != task_id)
            distance_gain = (
                metrics.score.distance_km
                - evaluator.evaluate(
                    reduced, start_node=problem.home_node(route_index)
                ).score.distance_km
            )
            priorities.append(
                (
                    (
                        _deadline_risk(delivered_at, deadline)
                        if use_deadline_risk
                        else 0.0,
                        max(0.0, delivered_at - deadline),
                        distance_gain,
                        -deadline,
                        -task_id,
                    ),
                    task_id,
                )
            )
    priorities.sort(reverse=True)
    return tuple(task_id for _, task_id in priorities)


def inter_uav_relocate_once(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    candidate_limit: int | None,
    use_deadline_risk: bool = True,
    task_limit: int | None = 12,
    deadline: float | None = None,
) -> Routes:
    """Return the best bounded strict-improving one-pair inter-UAV relocate."""

    materialized = tuple(tuple(route) for route in routes)
    if all(
        sum(visit > 0 for visit in route) >= problem.max_tasks_per_drone
        for route in materialized
    ):
        return materialized
    current_score = routes_score(evaluator, materialized)
    best_score = current_score
    best_routes = materialized
    route_by_task = _task_route_index(materialized)
    ordered_tasks = _local_search_task_order(
        problem,
        evaluator,
        materialized,
        use_deadline_risk=use_deadline_risk,
    )
    if task_limit is not None:
        ordered_tasks = ordered_tasks[:task_limit]

    for task_id in ordered_tasks:
        if deadline is not None and perf_counter() >= deadline:
            return best_routes
        source_index = route_by_task[task_id]
        source = tuple(
            visit
            for visit in materialized[source_index]
            if abs(visit) != task_id
        )
        for target_index, target in enumerate(materialized):
            if target_index == source_index:
                continue
            if sum(visit > 0 for visit in target) >= problem.max_tasks_per_drone:
                continue
            if deadline is not None and perf_counter() >= deadline:
                return best_routes
            options = route_insertion_options(
                problem,
                evaluator,
                target,
                target_index,
                task_id,
                candidate_limit=candidate_limit,
                option_count=1,
            )
            if not options:
                continue
            candidate = list(materialized)
            candidate[source_index] = source
            candidate[target_index] = options[0].route
            candidate_routes = tuple(candidate)
            candidate_score = routes_score(evaluator, candidate_routes)
            if _score_strictly_better(candidate_score, best_score):
                best_score = candidate_score
                best_routes = candidate_routes
    return best_routes


def inter_uav_swap_once(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    candidate_limit: int | None,
    use_deadline_risk: bool = True,
    task_limit: int | None = 12,
    pair_limit: int | None = 24,
    deadline: float | None = None,
) -> Routes:
    """Return the best bounded strict-improving exchange of two UAV task pairs."""

    materialized = tuple(tuple(route) for route in routes)
    current_score = routes_score(evaluator, materialized)
    best_score = current_score
    best_routes = materialized
    route_by_task = _task_route_index(materialized)
    ordered_tasks = _local_search_task_order(
        problem,
        evaluator,
        materialized,
        use_deadline_risk=use_deadline_risk,
    )
    if task_limit is not None:
        ordered_tasks = ordered_tasks[:task_limit]

    evaluated_pairs = 0
    for left_position, left_task in enumerate(ordered_tasks):
        left_route_index = route_by_task[left_task]
        for right_task in ordered_tasks[left_position + 1 :]:
            right_route_index = route_by_task[right_task]
            if left_route_index == right_route_index:
                continue
            if pair_limit is not None and evaluated_pairs >= pair_limit:
                return best_routes
            if deadline is not None and perf_counter() >= deadline:
                return best_routes
            evaluated_pairs += 1
            left_reduced = tuple(
                visit
                for visit in materialized[left_route_index]
                if abs(visit) != left_task
            )
            right_reduced = tuple(
                visit
                for visit in materialized[right_route_index]
                if abs(visit) != right_task
            )
            right_into_left = route_insertion_options(
                problem,
                evaluator,
                left_reduced,
                left_route_index,
                right_task,
                candidate_limit=candidate_limit,
                option_count=1,
            )
            left_into_right = route_insertion_options(
                problem,
                evaluator,
                right_reduced,
                right_route_index,
                left_task,
                candidate_limit=candidate_limit,
                option_count=1,
            )
            if not right_into_left or not left_into_right:
                continue
            candidate = list(materialized)
            candidate[left_route_index] = right_into_left[0].route
            candidate[right_route_index] = left_into_right[0].route
            candidate_routes = tuple(candidate)
            candidate_score = routes_score(evaluator, candidate_routes)
            if _score_strictly_better(candidate_score, best_score):
                best_score = candidate_score
                best_routes = candidate_routes
    return best_routes


def intra_route_block_improve_once(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    use_deadline_risk: bool = True,
    window_limit: int | None = 16,
    deadline: float | None = None,
) -> Routes:
    """Reorder bounded capacity-2 PPDD blocks while preserving pair precedence."""

    materialized = tuple(tuple(route) for route in routes)
    if problem.capacity != 2:
        return materialized
    current_score = routes_score(evaluator, materialized)
    best_score = current_score
    best_routes = materialized
    ranked_windows: list[tuple[tuple[float, ...], int, int]] = []
    for route_index, route in enumerate(materialized):
        metrics = evaluator.evaluate(
            route, start_node=problem.home_node(route_index)
        )
        for start in range(max(0, len(route) - 3)):
            block = route[start : start + 4]
            if tuple(visit > 0 for visit in block) != (
                True,
                True,
                False,
                False,
            ):
                continue
            pickups = {visit for visit in block if visit > 0}
            deliveries = {abs(visit) for visit in block if visit < 0}
            if pickups != deliveries or len(pickups) != 2:
                continue
            risk = max(
                (
                    _deadline_risk(
                        metrics.delivery_times_min[task_id],
                        problem.task(task_id).deadline_min,
                    )
                    for task_id in pickups
                ),
                default=0.0,
            )
            ranked_windows.append(
                (
                    (
                        risk if use_deadline_risk else 0.0,
                        -float(route_index),
                        -float(start),
                    ),
                    route_index,
                    start,
                )
            )
    ranked_windows.sort(reverse=True)
    if window_limit is not None:
        ranked_windows = ranked_windows[:window_limit]

    for _, route_index, start in ranked_windows:
        if deadline is not None and perf_counter() >= deadline:
            return best_routes
        route = materialized[route_index]
        block = route[start : start + 4]
        for reordered in sorted(set(permutations(block))):
            if reordered == block:
                continue
            candidate_route = route[:start] + reordered + route[start + 4 :]
            try:
                evaluator.evaluate(
                    candidate_route,
                    start_node=problem.home_node(route_index),
                )
            except ValueError:
                continue
            candidate = list(materialized)
            candidate[route_index] = candidate_route
            candidate_routes = tuple(candidate)
            candidate_score = routes_score(evaluator, candidate_routes)
            if _score_strictly_better(candidate_score, best_score):
                best_score = candidate_score
                best_routes = candidate_routes
    return best_routes


def vnd_improve(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    *,
    candidate_limit: int | None,
    use_deadline_risk: bool = True,
    max_moves: int = 2,
    task_limit: int | None = 12,
    swap_pair_limit: int | None = 24,
    block_window_limit: int | None = 16,
    deadline: float | None = None,
) -> Routes:
    """Run bounded VND over relocate, swap, and capacity-2 block moves."""

    current = tuple(tuple(route) for route in routes)
    current_score = routes_score(evaluator, current)
    accepted_moves = 0
    neighborhood = 0
    while neighborhood < 3 and accepted_moves < max_moves:
        if deadline is not None and perf_counter() >= deadline:
            break
        if neighborhood == 0:
            candidate = inter_uav_relocate_once(
                problem,
                evaluator,
                current,
                candidate_limit=candidate_limit,
                use_deadline_risk=use_deadline_risk,
                task_limit=task_limit,
                deadline=deadline,
            )
        elif neighborhood == 1:
            candidate = inter_uav_swap_once(
                problem,
                evaluator,
                current,
                candidate_limit=candidate_limit,
                use_deadline_risk=use_deadline_risk,
                task_limit=task_limit,
                pair_limit=swap_pair_limit,
                deadline=deadline,
            )
        else:
            candidate = intra_route_block_improve_once(
                problem,
                evaluator,
                current,
                use_deadline_risk=use_deadline_risk,
                window_limit=block_window_limit,
                deadline=deadline,
            )
        candidate_score = routes_score(evaluator, candidate)
        if _score_strictly_better(candidate_score, current_score):
            current = candidate
            current_score = candidate_score
            accepted_moves += 1
            neighborhood = 0
        else:
            neighborhood += 1
    return current


def _ejection_swap_improve(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    candidate_limit: int | None,
    rng: Random,
    max_trials: int,
    deadline: float | None = None,
) -> Routes:
    current_score = routes_score(evaluator, routes)
    best_routes = routes
    best_score = current_score
    route_by_task = _task_route_index(routes)
    late_tasks: list[tuple[float, int]] = []
    for route_index, route in enumerate(routes):
        metrics = evaluator.evaluate(
            route, start_node=problem.home_node(route_index)
        )
        for task_id in metrics.late_task_ids:
            late_tasks.append(
                (
                    metrics.delivery_times_min[task_id]
                    - problem.task(task_id).deadline_min,
                    task_id,
                )
            )
    late_tasks.sort(reverse=True)
    trials = 0
    for _, urgent_task in late_tasks:
        if deadline is not None and perf_counter() >= deadline:
            return best_routes
        source_index = route_by_task[urgent_task]
        target_indices = [
            index for index in range(len(routes)) if index != source_index
        ]
        rng.shuffle(target_indices)
        for target_index in target_indices:
            if deadline is not None and perf_counter() >= deadline:
                return best_routes
            ejectable = [visit for visit in routes[target_index] if visit > 0]
            ejectable.sort(
                key=lambda task_id: (
                    problem.task(task_id).deadline_min,
                    task_id,
                ),
                reverse=True,
            )
            for ejected_task in ejectable[:6]:
                if deadline is not None and perf_counter() >= deadline:
                    return best_routes
                trials += 1
                source = tuple(
                    visit
                    for visit in routes[source_index]
                    if abs(visit) != urgent_task
                )
                target = tuple(
                    visit
                    for visit in routes[target_index]
                    if abs(visit) != ejected_task
                )
                urgent_options = route_insertion_options(
                    problem,
                    evaluator,
                    target,
                    target_index,
                    urgent_task,
                    candidate_limit=candidate_limit,
                    option_count=1,
                )
                ejected_options = route_insertion_options(
                    problem,
                    evaluator,
                    source,
                    source_index,
                    ejected_task,
                    candidate_limit=candidate_limit,
                    option_count=1,
                )
                if urgent_options and ejected_options:
                    candidate = list(routes)
                    candidate[target_index] = urgent_options[0].route
                    candidate[source_index] = ejected_options[0].route
                    candidate_routes = tuple(candidate)
                    score = routes_score(evaluator, candidate_routes)
                    if score < best_score:
                        best_score = score
                        best_routes = candidate_routes
                if trials >= max_trials:
                    return best_routes
    return best_routes


def _roulette(weights: dict[str, float], rng: Random) -> str:
    names = tuple(weights)
    values = [weights[name] for name in names]
    return rng.choices(names, weights=values, k=1)[0]


def _accept_worse(
    candidate: Score,
    current: Score,
    problem: Problem,
    temperature: float,
    rng: Random,
) -> bool:
    if candidate <= current:
        return True
    if candidate.late_count != current.late_count:
        delta = (candidate.late_count - current.late_count) / len(problem.tasks)
    elif abs(candidate.total_lateness_min - current.total_lateness_min) > 1e-12:
        scale = max(1.0, sum(task.deadline_min for task in problem.tasks))
        delta = (candidate.total_lateness_min - current.total_lateness_min) / scale
    else:
        delta = (candidate.distance_km - current.distance_km) / max(
            1.0, current.distance_km
        )
    if delta <= 0:
        return True
    return rng.random() < exp(-delta / max(temperature, 1e-12))


def solve_alns(
    problem: Problem,
    *,
    config: ALNSConfig | None = None,
    initial_routes: Sequence[Sequence[int]] | None = None,
) -> SolverResult:
    """Run C2-Lex-HALNS and independently validate its best-so-far result."""

    cfg = config or ALNSConfig()
    started = perf_counter()
    deadline = (
        None
        if cfg.time_limit_seconds is None
        else started + cfg.time_limit_seconds
    )
    rng = Random(cfg.seed)
    counters = SearchCounters()
    evaluator = RouteEvaluator(problem, counters=counters)
    relay_active = bool(problem.has_relays) and cfg.relay_enabled
    relay_cfg = (
        _RelaySearchConfig(
            enabled=relay_active,
            candidates_per_task=cfg.relay_candidates_per_task,
            plan_beam=cfg.relay_plan_beam,
            leg_candidate_limit=cfg.relay_leg_candidate_limit,
            leg_beam=cfg.relay_leg_beam,
            event_cap=cfg.relay_event_cap,
            debug=cfg.relay_debug,
            sample_every=cfg.relay_sample_every,
            global_limit=cfg.relay_global_limit,
            probe_fraction=cfg.relay_probe_fraction,
            probe_min=cfg.relay_probe_min,
            probe_max_tasks=cfg.relay_probe_max_tasks,
            counters=counters,
            leg_option_cache=_LegOptionCache(),
        )
        if relay_active
        else None
    )
    global_evaluator = (
        GlobalRelayEvaluator(problem, counters=counters)
        if relay_active
        else None
    )
    if initial_routes is None:
        current = construct_regret_initial(
            problem,
            candidate_limit=cfg.candidate_limit,
            relay_cfg=relay_cfg,
            global_evaluator=global_evaluator,
            repair_rank_candidate_limit=cfg.repair_rank_candidate_limit,
            repair_exact_candidate_limit=cfg.repair_exact_candidate_limit,
            direct_exact_top_k=cfg.direct_exact_top_k,
            home_seed=cfg.enable_home_seed,
            home_bias=cfg.enable_home_bias,
        ).routes
    else:
        current = tuple(tuple(route) for route in initial_routes)
        initial_validation = evaluate_solution(problem, current)
        if not initial_validation.valid:
            raise ValueError(f"传入的初始解非法: {initial_validation.violations}")
        current = current + tuple(
            () for _ in range(problem.drone_count - len(current))
        )

    def score_of(candidate_routes: Routes) -> Score:
        if global_evaluator is not None:
            return global_evaluator.evaluate(candidate_routes).score
        return routes_score(evaluator, candidate_routes)

    if relay_cfg is not None and cfg.enable_relay_seed:
        try:
            current = _relay_seed_pass(
                problem,
                evaluator,
                global_evaluator,
                current,
                relay_cfg=relay_cfg,
                task_limit=cfg.relay_seed_task_limit,
                deadline=deadline,
            )
        except _SearchDeadlineReached:
            pass
    if relay_cfg is not None and cfg.enable_relay_refine:
        try:
            current = _relay_refine_pass(
                problem,
                evaluator,
                global_evaluator,
                current,
                relay_cfg=relay_cfg,
                task_limit=cfg.relay_refine_task_limit,
                deadline=deadline,
            )
        except _SearchDeadlineReached:
            pass
    current_score = score_of(current)
    initial_score = current_score
    best = current
    best_score = current_score
    time_to_best = perf_counter() - started
    best_trajectory: list[tuple[int, float, int, float, float]] = [
        (
            0,
            time_to_best,
            best_score.late_count,
            best_score.total_lateness_min,
            best_score.distance_km,
        )
    ]

    destroy_operators = tuple(
        name
        for name in DESTROY_OPERATORS
        if (
            (
                name != "route_clear"
                if cfg.enable_assignment_destroy
                else name != "assignment_destroy"
            )
            and (cfg.enable_home_displaced or name != "home_displaced")
        )
    )
    destroy_weights = {name: 1.0 for name in destroy_operators}
    repair_operators = tuple(
        name
        for name in REPAIR_OPERATORS
        if cfg.enable_cluster_repair or name != "cluster_regret"
    )
    repair_weights = {name: 1.0 for name in repair_operators}
    total_uses = {
        **{f"destroy:{name}": 0 for name in destroy_operators},
        **{f"repair:{name}": 0 for name in repair_operators},
    }
    segment_uses = {key: 0 for key in total_uses}
    segment_rewards = {key: 0.0 for key in total_uses}
    accepted_count = 0
    vnd_calls = 0
    vnd_improved_iterations = 0
    temperature = cfg.initial_temperature
    route_pool = (
        _RoutePool(problem, evaluator)
        if cfg.enable_route_pool and relay_cfg is None
        else None
    )
    if route_pool is not None:
        route_pool.add(best)
    completed_iterations = 0

    for iteration in range(1, cfg.max_iterations + 1):
        if deadline is not None and perf_counter() >= deadline:
            break
        destroy_name = _roulette(destroy_weights, rng)
        repair_name = _roulette(repair_weights, rng)
        minimum_pairs = 1 if len(problem.tasks) == 1 else 2
        if relay_cfg is not None:
            # Relay repair is stronger than DIRECT repair, so routine
            # destroy stays smaller; a periodic large destroy keeps
            # diversification without inflating the average repair cost.
            use_large = (
                cfg.relay_large_destroy_interval > 0
                and iteration % cfg.relay_large_destroy_interval == 0
            )
            if use_large:
                lower = max(
                    minimum_pairs,
                    round(
                        len(problem.tasks) * cfg.relay_large_destroy_min
                    ),
                )
                upper = max(
                    lower,
                    round(
                        len(problem.tasks) * cfg.relay_large_destroy_max
                    ),
                )
            else:
                lower = max(
                    minimum_pairs,
                    round(
                        len(problem.tasks) * cfg.relay_min_destroy_fraction
                    ),
                )
                upper = max(
                    lower,
                    round(
                        len(problem.tasks) * cfg.relay_max_destroy_fraction
                    ),
                )
        else:
            lower = max(
                minimum_pairs,
                round(len(problem.tasks) * cfg.min_destroy_fraction),
            )
            upper = max(
                lower,
                round(len(problem.tasks) * cfg.max_destroy_fraction),
            )
        remove_count = rng.randint(lower, upper)
        if relay_cfg is not None:
            plan_index = build_plan_index(problem, current)
            original_route_by_task = dict(plan_index.delivery_route_by_task)
        else:
            original_route_by_task = _task_route_index(current)
        partial, removed = destroy_solution(
            problem,
            evaluator,
            current,
            destroy_name,
            remove_count,
            rng,
            use_deadline_risk=cfg.enable_deadline_risk,
        )
        if deadline is not None and perf_counter() >= deadline:
            break
        try:
            candidate = _repair(
                problem,
                evaluator,
                partial,
                removed,
                repair_name,
                cfg.candidate_limit,
                use_deadline_risk=cfg.enable_deadline_risk,
                original_route_by_task=original_route_by_task,
                cluster_bundle_candidate_limit=cfg.cluster_bundle_candidate_limit,
                cluster_pair_limit=cfg.cluster_pair_limit,
                deadline=deadline,
                relay_cfg=relay_cfg,
                global_evaluator=global_evaluator,
                repair_rank_candidate_limit=cfg.repair_rank_candidate_limit,
                repair_exact_candidate_limit=cfg.repair_exact_candidate_limit,
                direct_exact_top_k=cfg.direct_exact_top_k,
            )
        except _SearchDeadlineReached:
            break
        timed_out = deadline is not None and perf_counter() >= deadline
        if (
            not timed_out
            and relay_cfg is not None
            and cfg.enable_relay_refine
            and cfg.relay_refine_interval > 0
            and iteration % cfg.relay_refine_interval == 0
        ):
            try:
                candidate = _relay_refine_pass(
                    problem,
                    evaluator,
                    global_evaluator,
                    candidate,
                    relay_cfg=relay_cfg,
                    task_limit=cfg.relay_refine_task_limit,
                    deadline=deadline,
                )
            except _SearchDeadlineReached:
                break
            timed_out = deadline is not None and perf_counter() >= deadline
        if (
            not timed_out
            and relay_cfg is None
            and cfg.enable_vnd
            and cfg.vnd_max_moves > 0
        ):
            repaired_score = routes_score(evaluator, candidate)
            candidate = vnd_improve(
                problem,
                evaluator,
                candidate,
                candidate_limit=cfg.candidate_limit,
                use_deadline_risk=cfg.enable_deadline_risk,
                max_moves=cfg.vnd_max_moves,
                task_limit=cfg.vnd_task_limit,
                swap_pair_limit=cfg.vnd_swap_pair_limit,
                block_window_limit=cfg.vnd_block_window_limit,
                deadline=deadline,
            )
            vnd_calls += 1
            if routes_score(evaluator, candidate) < repaired_score:
                vnd_improved_iterations += 1
        timed_out = timed_out or (
            deadline is not None and perf_counter() >= deadline
        )
        if (
            not timed_out
            and relay_cfg is None
            and cfg.enable_ejection
            and cfg.ejection_interval > 0
            and iteration % cfg.ejection_interval == 0
        ):
            candidate = _ejection_swap_improve(
                problem,
                evaluator,
                candidate,
                cfg.candidate_limit,
                rng,
                cfg.ejection_trials,
                deadline,
            )
        timed_out = timed_out or (
            deadline is not None and perf_counter() >= deadline
        )
        candidate_score = score_of(candidate)
        timed_out = timed_out or (
            deadline is not None and perf_counter() >= deadline
        )
        improved_current = candidate_score < current_score
        accepted = False if timed_out else _accept_worse(
            candidate_score, current_score, problem, temperature, rng
        )
        reward = 0.0
        if accepted:
            current = candidate
            current_score = candidate_score
            accepted_count += 1
            reward = 4.0 if improved_current else 1.0
            if route_pool is not None:
                route_pool.add(current)
        if not timed_out and candidate_score < best_score:
            best = candidate
            best_score = candidate_score
            time_to_best = perf_counter() - started
            best_trajectory.append(
                (
                    iteration,
                    time_to_best,
                    best_score.late_count,
                    best_score.total_lateness_min,
                    best_score.distance_km,
                )
            )
            reward = 8.0
            if route_pool is not None:
                route_pool.add(best)

        destroy_key = f"destroy:{destroy_name}"
        repair_key = f"repair:{repair_name}"
        for key in (destroy_key, repair_key):
            total_uses[key] += 1
            segment_uses[key] += 1
            segment_rewards[key] += reward

        if timed_out:
            completed_iterations = iteration
            break

        if (
            route_pool is not None
            and cfg.route_pool_interval > 0
            and iteration % cfg.route_pool_interval == 0
        ):
            recombined = route_pool.recombine(
                best,
                cfg.route_pool_node_limit,
                deadline,
            )
            if deadline is not None and perf_counter() >= deadline:
                completed_iterations = iteration
                break
            recombined_score = routes_score(evaluator, recombined)
            if recombined_score < best_score:
                best = recombined
                best_score = recombined_score
                current = recombined
                current_score = recombined_score
                time_to_best = perf_counter() - started
                best_trajectory.append(
                    (
                        iteration,
                        time_to_best,
                        best_score.late_count,
                        best_score.total_lateness_min,
                        best_score.distance_km,
                    )
                )
                route_pool.add(best)

        if iteration % cfg.weight_update_interval == 0:
            for name in destroy_operators:
                key = f"destroy:{name}"
                if segment_uses[key]:
                    observed = segment_rewards[key] / segment_uses[key]
                    destroy_weights[name] = max(
                        cfg.minimum_weight,
                        (1 - cfg.reaction_factor) * destroy_weights[name]
                        + cfg.reaction_factor * observed,
                    )
            for name in repair_operators:
                key = f"repair:{name}"
                if segment_uses[key]:
                    observed = segment_rewards[key] / segment_uses[key]
                    repair_weights[name] = max(
                        cfg.minimum_weight,
                        (1 - cfg.reaction_factor) * repair_weights[name]
                        + cfg.reaction_factor * observed,
                    )
            segment_uses = {key: 0 for key in segment_uses}
            segment_rewards = {key: 0.0 for key in segment_rewards}

        temperature = max(
            cfg.minimum_temperature, temperature * cfg.cooling_rate
        )
        completed_iterations = iteration

    evaluation = evaluate_solution(problem, best)
    if not evaluation.valid:
        raise RuntimeError(f"ALNS 最终解非法: {evaluation.violations}")
    all_weights = {
        **{f"destroy:{key}": value for key, value in destroy_weights.items()},
        **{f"repair:{key}": value for key, value in repair_weights.items()},
    }
    if not all(isfinite(value) and value > 0 for value in all_weights.values()):
        raise RuntimeError("ALNS 算子权重出现非法数值")
    runtime_seconds = perf_counter() - started
    metadata = MappingProxyType(
        {
            "method": (
                "C2-Lex-ALNS-Core"
                if not cfg.enable_route_pool and not cfg.enable_ejection
                else "C2-Lex-HALNS"
            ),
            "seed": cfg.seed,
            "enable_route_pool": cfg.enable_route_pool,
            "enable_ejection": cfg.enable_ejection,
            "enable_assignment_destroy": cfg.enable_assignment_destroy,
            "enable_home_seed": cfg.enable_home_seed,
            "enable_home_displaced": cfg.enable_home_displaced,
            "enable_deadline_risk": cfg.enable_deadline_risk,
            "enable_vnd": cfg.enable_vnd,
            "enable_cluster_repair": cfg.enable_cluster_repair,
            "relay_enabled": bool(relay_active),
            "vnd_calls": vnd_calls,
            "vnd_improved_iterations": vnd_improved_iterations,
            "time_limit_seconds": cfg.time_limit_seconds,
            "initial_score": (
                initial_score.late_count,
                initial_score.total_lateness_min,
                initial_score.distance_km,
            ),
            "time_to_best_seconds": time_to_best,
            "best_trajectory": tuple(best_trajectory),
            "best_update_count": len(best_trajectory) - 1,
            "accepted_solutions": accepted_count,
            "iterations_per_second": (
                completed_iterations / runtime_seconds
                if runtime_seconds > 0
                else 0.0
            ),
            "operator_uses": MappingProxyType(dict(total_uses)),
            "operator_weights": MappingProxyType(all_weights),
            "route_pool_columns": len(route_pool.columns) if route_pool else 0,
            "route_pool_last_nodes": route_pool.last_nodes if route_pool else 0,
            "profiling": MappingProxyType(counters.as_dict()),
        }
    )
    if relay_cfg is not None:
        final_plan_index = build_plan_index(problem, best)
        final_schedule = global_evaluator.evaluate(best)
        stats = relay_statistics(
            problem, best, final_schedule, final_plan_index
        )
        metadata = MappingProxyType(
            {
                **dict(metadata),
                "relay": MappingProxyType(stats),
                "global_evaluation_count": relay_cfg.global_evaluation_count,
                "relay_candidate_evaluation_count": (
                    relay_cfg.candidate_evaluation_count
                ),
                "generated_relay_options": relay_cfg.generated_relay_options,
                "selected_relay_options": relay_cfg.selected_relay_options,
                "refine_candidates_evaluated": (
                    relay_cfg.refine_candidates_evaluated
                ),
                "refine_candidates_pruned": relay_cfg.refine_candidates_pruned,
                "relay_leg_cache_hits": (
                    relay_cfg.leg_option_cache.hits
                    if relay_cfg.leg_option_cache is not None
                    else 0
                ),
                "relay_leg_cache_misses": (
                    relay_cfg.leg_option_cache.misses
                    if relay_cfg.leg_option_cache is not None
                    else 0
                ),
            }
        )
    return SolverResult(
        routes=best,
        evaluation=evaluation,
        runtime_seconds=runtime_seconds,
        iterations=completed_iterations,
        metadata=metadata,
    )


def solve_alns_core(
    problem: Problem,
    *,
    config: ALNSConfig | None = None,
    initial_routes: Sequence[Sequence[int]] | None = None,
) -> SolverResult:
    """Run the adaptive LNS core without route-pool or ejection reinforcement."""

    core_config = replace(
        config or ALNSConfig(),
        enable_route_pool=False,
        enable_ejection=False,
    )
    return solve_alns(
        problem,
        config=core_config,
        initial_routes=initial_routes,
    )


def solve_relay_staged(
    problem: Problem,
    *,
    config: ALNSConfig | None = None,
    initial_routes: Sequence[Sequence[int]] | None = None,
    core: bool = False,
) -> SolverResult:
    """Run DIRECT warm-up followed by relay-aware search in one total budget.

    Relay plans strictly enlarge the feasible plan set, but evaluating them
    makes each ALNS iteration more expensive.  This staged solver first uses
    the fast DIRECT neighbourhood to construct a competitive incumbent and
    then starts the relay phase from that incumbent.  The second phase keeps
    best-so-far semantics, so enabling relay cannot discard the warm-up score.

    ``time_limit_seconds`` is split as wall-clock time.  Without a time limit,
    ``max_iterations`` is split instead.  Set
    ``relay_direct_warmup_fraction=0`` to reproduce a pure relay search.
    """

    cfg = config or ALNSConfig()
    solver = solve_alns_core if core else solve_alns
    fraction = cfg.relay_direct_warmup_fraction
    if not problem.has_relays or not cfg.relay_enabled or fraction <= 0:
        return solver(problem, config=cfg, initial_routes=initial_routes)

    if cfg.max_iterations < 2:
        return solver(problem, config=cfg, initial_routes=initial_routes)

    warm_iterations = min(
        cfg.max_iterations - 1,
        max(1, round(cfg.max_iterations * fraction)),
    )
    relay_iterations = cfg.max_iterations - warm_iterations
    if cfg.time_limit_seconds is None:
        warm_seconds = None
        relay_seconds = None
    else:
        warm_seconds = cfg.time_limit_seconds * fraction
        relay_seconds = cfg.time_limit_seconds - warm_seconds

    warm_config = replace(
        cfg,
        relay_enabled=False,
        max_iterations=warm_iterations,
        time_limit_seconds=warm_seconds,
    )
    warm_result = solver(
        problem,
        config=warm_config,
        initial_routes=initial_routes,
    )

    relay_config = replace(
        cfg,
        relay_enabled=True,
        max_iterations=relay_iterations,
        time_limit_seconds=relay_seconds,
    )
    relay_result = solver(
        problem,
        config=relay_config,
        initial_routes=warm_result.routes,
    )
    if relay_result.evaluation.score > warm_result.evaluation.score:
        raise RuntimeError("中继阶段违反 best-so-far：结果劣于 DIRECT 预热解")

    warm_trajectory = tuple(warm_result.metadata.get("best_trajectory", ()))
    relay_trajectory = tuple(relay_result.metadata.get("best_trajectory", ()))
    merged_trajectory = list(warm_trajectory)
    for iteration, elapsed, late_count, lateness, distance in relay_trajectory:
        adjusted = (
            warm_result.iterations + iteration,
            warm_result.runtime_seconds + elapsed,
            late_count,
            lateness,
            distance,
        )
        if not merged_trajectory or adjusted[2:] < merged_trajectory[-1][2:]:
            merged_trajectory.append(adjusted)

    warm_profile = dict(warm_result.metadata.get("profiling", {}))
    relay_profile = dict(relay_result.metadata.get("profiling", {}))
    combined_profile = {
        key: warm_profile.get(key, 0) + relay_profile.get(key, 0)
        for key in warm_profile.keys() | relay_profile.keys()
    }
    warm_uses = dict(warm_result.metadata.get("operator_uses", {}))
    relay_uses = dict(relay_result.metadata.get("operator_uses", {}))
    combined_uses = {
        key: warm_uses.get(key, 0) + relay_uses.get(key, 0)
        for key in warm_uses.keys() | relay_uses.keys()
    }
    runtime_seconds = warm_result.runtime_seconds + relay_result.runtime_seconds
    iterations = warm_result.iterations + relay_result.iterations
    metadata = {
        **dict(relay_result.metadata),
        "method": (
            "C2-Lex-Relay-Staged-Core"
            if core
            else "C2-Lex-Relay-Staged-HALNS"
        ),
        "time_limit_seconds": cfg.time_limit_seconds,
        "initial_score": warm_result.metadata.get("initial_score"),
        "time_to_best_seconds": (
            merged_trajectory[-1][1] if merged_trajectory else 0.0
        ),
        "best_trajectory": tuple(merged_trajectory),
        "best_update_count": max(0, len(merged_trajectory) - 1),
        "accepted_solutions": (
            int(warm_result.metadata.get("accepted_solutions", 0))
            + int(relay_result.metadata.get("accepted_solutions", 0))
        ),
        "iterations_per_second": (
            iterations / runtime_seconds if runtime_seconds > 0 else 0.0
        ),
        "operator_uses": MappingProxyType(combined_uses),
        "profiling": MappingProxyType(combined_profile),
        "staged_relay": True,
        "relay_direct_warmup_fraction": fraction,
        "relay_warmup_runtime_seconds": warm_result.runtime_seconds,
        "relay_search_runtime_seconds": relay_result.runtime_seconds,
        "relay_warmup_iterations": warm_result.iterations,
        "relay_search_iterations": relay_result.iterations,
        "relay_warmup_score": (
            warm_result.evaluation.score.late_count,
            warm_result.evaluation.score.total_lateness_min,
            warm_result.evaluation.score.distance_km,
        ),
        "relay_phase_best_update_count": relay_result.metadata.get(
            "best_update_count", 0
        ),
    }
    return SolverResult(
        routes=relay_result.routes,
        evaluation=relay_result.evaluation,
        runtime_seconds=runtime_seconds,
        iterations=iterations,
        metadata=MappingProxyType(metadata),
    )
