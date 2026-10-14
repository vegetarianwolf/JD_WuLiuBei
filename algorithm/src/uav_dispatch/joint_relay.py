"""Standalone joint DIRECT/transfer/assignment/order variable-neighborhood search.

This solver has no ALNS stage or DIRECT warmup. Its principal neighborhood
jointly replaces two original-task plans, including both task orders and
cross-aircraft relay legs. Delivery responsibility slots are freed together;
inbound transport does not consume a final-delivery K slot. A deterministic
neighborhood cycle alternates joint replacements, single-plan conversion or
merge, and visit-order changes. Occasional feasible perturbations escape a
local optimum, while independently checked best-so-far is never discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from random import Random
from time import perf_counter
from types import MappingProxyType
from typing import Sequence

from .model import Problem, Score
from .relay import GlobalRelayEvaluator, build_plan_index, relay_statistics
from .search import (
    RouteEvaluator, Routes, SolverResult, route_insertion_options,
    route_leg_insertion_options,
)
from .validation import evaluate_solution


@dataclass(frozen=True, slots=True)
class JointRelayConfig:
    max_iterations: int = 400
    time_limit_seconds: float | None = None
    seed: int = 20260805
    candidate_limit: int | None = 12
    position_options: int = 2
    station_limit: int = 2
    route_pool_size: int = 2
    plan_limit: int = 6
    pair_beam: int = 4
    perturb_after: int = 12
    perturb_max_extra_late: int = 1
    adjacent_probe_limit: int = 12

    def __post_init__(self):
        if self.max_iterations < 0:
            raise ValueError("迭代次数不能为负")
        if self.time_limit_seconds is not None and (
            not isfinite(self.time_limit_seconds) or self.time_limit_seconds <= 0
        ):
            raise ValueError("时间上限必须是有限正数")
        if self.candidate_limit is not None and self.candidate_limit < 2:
            raise ValueError("候选位置上限至少为2")
        for name in (
            "position_options", "station_limit", "route_pool_size", "plan_limit",
            "pair_beam", "perturb_after", "adjacent_probe_limit",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name}必须为正整数")
        if self.perturb_max_extra_late < 0:
            raise ValueError("扰动额外逾期上限不能为负")


class _DeadlineReached(RuntimeError):
    pass


def _check(deadline):
    if deadline is not None and perf_counter() >= deadline:
        raise _DeadlineReached


@dataclass(frozen=True, slots=True)
class _Plan:
    routes: Routes
    score: Score
    mode: str
    delivery_route: int


@dataclass(slots=True)
class _Context:
    problem: Problem
    config: JointRelayConfig
    evaluator: RouteEvaluator
    global_evaluator: GlobalRelayEvaluator
    started: float
    deadline: float | None
    best: Routes
    best_evaluation: object
    trajectory: list
    iteration: int = 0
    work: dict = field(default_factory=lambda: {
        "plan_generations": 0, "generated_plans": 0, "global_evaluations": 0,
        "full_evaluations": 0, "joint_pair_probes": 0,
        "single_plan_probes": 0, "adjacent_probes": 0,
        "rejected_cycles": 0, "rejected_invalid": 0,
        "accepted_improvements": 0, "accepted_perturbations": 0,
        "deadline_reached": False,
    })

    def schedule(self, routes):
        _check(self.deadline)
        result = self.global_evaluator.evaluate(routes)
        self.work["global_evaluations"] += 1
        _check(self.deadline)
        if not result.feasible:
            self.work["rejected_cycles"] += 1
        return result

    def confirm(self, plan):
        """Only complete, independently checked, in-budget plans enter state."""
        _check(self.deadline)
        checked = evaluate_solution(self.problem, plan.routes, _force_full=True)
        self.work["full_evaluations"] += 1
        _check(self.deadline)
        if not checked.valid:
            self.work["rejected_invalid"] += 1
            return None
        canonical = _Plan(plan.routes, checked.score, plan.mode, plan.delivery_route)
        if checked.score < self.best_evaluation.score:
            self.best, self.best_evaluation = plan.routes, checked
            self.trajectory.append((
                self.iteration, perf_counter() - self.started,
                checked.score.late_count, checked.score.total_lateness_min,
                checked.score.distance_km,
            ))
        return canonical


def _remove_tasks(problem, routes, task_ids):
    """Remove every direct or relay visit of each ORIGINAL task everywhere."""
    removed = set(task_ids)
    registry = problem.leg_registry
    return tuple(tuple(
        visit for visit in route
        if (registry[abs(visit)].task_id if abs(visit) in registry else abs(visit))
        not in removed
    ) for route in routes)


def _with_route(routes, index, route):
    changed = list(routes)
    changed[index] = route
    return tuple(changed)


def _diverse(plans, limit):
    """Keep mode/responsibility alternatives before filling position choices."""
    ordered = sorted(plans, key=lambda p: (p.score, p.mode, p.delivery_route, p.routes))
    heads, rest, seen = [], [], set()
    for plan in ordered:
        key = plan.mode, plan.delivery_route
        (rest if key in seen else heads).append(plan)
        seen.add(key)
    return tuple(sorted((heads + rest)[:limit], key=lambda p: (
        p.score, p.mode, p.delivery_route, p.routes,
    )))


def _task_plans(context, routes, task_id, route_pool):
    """Bounded complete plans for one removed task on a partial solution.

    Cross-UAV combinations are composed on different routes, so each leg's
    local capacity-feasible insertion stays valid. Local deltas never prune
    a global score: existing waits can absorb delays. Every composed plan
    is scheduled on the complete precedence DAG, including unchanged UAVs.
    """
    p, cfg, evaluator = context.problem, context.config, context.evaluator
    _check(context.deadline)
    context.work["plan_generations"] += 1
    candidates = []
    for route_index in route_pool:
        _check(context.deadline)
        options = route_insertion_options(
            p, evaluator, routes[route_index], route_index, task_id,
            candidate_limit=cfg.candidate_limit, option_count=cfg.position_options,
        )
        _check(context.deadline)
        for option in options:
            candidates.append((_with_route(routes, route_index, option.route),
                               "DIRECT", route_index))
    by_station = {}
    for leg in p._legs_by_task.get(task_id, ()):
        by_station.setdefault(leg.relay_id, {})[leg.kind] = leg.id
    for station in p.task_relay_candidates.get(task_id, ())[:cfg.station_limit]:
        _check(context.deadline)
        pair = by_station.get(station, {})
        if set(pair) != {"RELAY_IN", "RELAY_OUT"}:
            continue
        inbound, outbound = [], []
        for route_index in route_pool:
            for kind, target in (("RELAY_IN", inbound), ("RELAY_OUT", outbound)):
                _check(context.deadline)
                leg_id = pair[kind]
                target.extend(route_leg_insertion_options(
                    p, evaluator, routes[route_index], route_index, leg_id, -leg_id,
                    candidate_limit=cfg.candidate_limit,
                    option_count=cfg.position_options,
                    counts_toward_k=kind == "RELAY_OUT", event_cap=None,
                ))
                _check(context.deadline)
        for left in inbound:
            for right in outbound:
                _check(context.deadline)
                if left.route_index == right.route_index:
                    continue
                changed = list(routes)
                changed[left.route_index], changed[right.route_index] = left.route, right.route
                candidates.append((tuple(changed), "RELAY", right.route_index))
    plans, seen = [], set()
    for changed, mode, route_index in candidates:
        _check(context.deadline)
        if changed in seen:
            continue
        seen.add(changed)
        schedule = context.schedule(changed)
        context.work["generated_plans"] += 1
        if schedule.feasible:
            plans.append(_Plan(changed, schedule.score, mode, route_index))
    _check(context.deadline)
    return _diverse(plans, cfg.plan_limit)


def _route_pool(context, routes, task_ids, index):
    """Include current serving/transport UAVs, then nearby bridge UAVs."""
    p, cfg = context.problem, context.config
    required = []
    for task_id in task_ids:
        route_index = index.delivery_route_by_task[task_id]
        if route_index not in required:
            required.append(route_index)
    # Existing relay sources must stay available even when the configured
    # bridge pool is smaller. This lets a plan be merged or re-routed safely.
    for task_id in task_ids:
        for route_index in sorted(index.task_routes[task_id]):
            if route_index not in required:
                required.append(route_index)
    midpoint = p.task(task_ids[0]).pickup
    remaining = sorted((i for i in range(len(routes)) if i not in required),
        key=lambda i: (p._points[p.home_node(i)].distance_to(midpoint), i))
    needed = max(len(required), min(len(routes), cfg.route_pool_size))
    return tuple(required + remaining[:max(0, needed - len(required))])


def _joint_neighbor(context, routes, pair, index):
    """Jointly enumerate DIRECT/DIRECT, DIRECT/RELAY and RELAY/RELAY plans."""
    context.work["joint_pair_probes"] += 1
    reduced = _remove_tasks(context.problem, routes, pair)
    pool = _route_pool(context, routes, pair, index)
    best = None
    seen = set()
    # Both orders are essential: early insertion can occupy the only K slot
    # or locally look worse until the other task is jointly reassigned.
    for first, second in (pair, tuple(reversed(pair))):
        first_plans = _diverse(_task_plans(context, reduced, first, pool), context.config.pair_beam)
        for partial in first_plans:
            for complete in _task_plans(context, partial.routes, second, pool):
                _check(context.deadline)
                if complete.routes == routes or complete.routes in seen:
                    continue
                seen.add(complete.routes)
                if best is not None and complete.score >= best.score:
                    continue
                checked = context.confirm(complete)
                if checked is not None and (best is None or checked.score < best.score):
                    best = checked
    return best


def _single_neighbor(context, routes, task_id, index):
    """Convert DIRECT to a true transfer, merge RELAY, or replace both legs."""
    context.work["single_plan_probes"] += 1
    reduced = _remove_tasks(context.problem, routes, (task_id,))
    pool = _route_pool(context, routes, (task_id,), index)
    for plan in _task_plans(context, reduced, task_id, pool):
        if plan.routes != routes:
            checked = context.confirm(plan)
            if checked is not None:
                return checked
    return None


def _adjacent_neighbor(context, routes, route_index):
    """Reorder visits, including transfer events, with complete validation."""
    route = routes[route_index]
    limit = min(max(0, len(route) - 1), context.config.adjacent_probe_limit)
    if not limit:
        return None
    start = (context.iteration * context.config.adjacent_probe_limit) % (len(route) - 1)
    best = None
    for offset in range(limit):
        _check(context.deadline)
        position = (start + offset) % (len(route) - 1)
        swapped = list(route)
        swapped[position], swapped[position + 1] = swapped[position + 1], swapped[position]
        changed = _with_route(routes, route_index, tuple(swapped))
        schedule = context.schedule(changed)
        context.work["adjacent_probes"] += 1
        if not schedule.feasible or (best is not None and schedule.score >= best.score):
            continue
        checked = context.confirm(_Plan(changed, schedule.score, "ORDER", route_index))
        if checked is not None and (best is None or checked.score < best.score):
            best = checked
    return best


def _select_tasks(context, routes, index, rng):
    schedule = context.schedule(routes)
    ids = tuple(sorted(index.task_plans))
    ranked = sorted(ids, key=lambda task_id: (
        -(schedule.delivery_times_min[task_id] - context.problem.task(task_id).deadline_min),
        task_id,
    ))
    first = rng.choice(ranked[:min(8, len(ranked))]) if rng.random() < 0.8 else rng.choice(ids)
    other = [task_id for task_id in ids
             if index.delivery_route_by_task[task_id] != index.delivery_route_by_task[first]]
    if not other:
        other = [task_id for task_id in ids if task_id != first]
    if not other:
        return first, None
    task = context.problem.task(first)
    other.sort(key=lambda task_id: (
        context.problem.task(task_id).pickup.distance_to(task.pickup)
        + context.problem.task(task_id).delivery.distance_to(task.delivery), task_id,
    ))
    second = rng.choice(other[:min(8, len(other))]) if rng.random() < 0.8 else rng.choice(other)
    return first, second


def _initial_routes(problem):
    """Quick feasible construction, not a DIRECT optimization stage.

    Benchmark callers normally supply the common frozen constructor's routes.
    The standalone fallback appends adjacent pairs in deadline order to the
    nearest available aircraft, without search, relay seeding or warmup.
    """
    routes = [[] for _ in range(problem.drone_count)]
    count = [0] * problem.drone_count
    for task in sorted(problem.tasks, key=lambda task: (task.deadline_min, task.id)):
        available = [i for i in range(problem.drone_count)
                     if count[i] < problem.max_tasks_per_drone]
        index = min(available, key=lambda i: (
            problem.distance(routes[i][-1] if routes[i] else None, task.id,
                from_node=problem.home_node(i) if not routes[i] else None), count[i], i,
        ))
        routes[index].extend((task.id, -task.id))
        count[index] += 1
    return tuple(tuple(route) for route in routes)


def solve_joint_relay(problem: Problem, *, config: JointRelayConfig | None = None,
                      initial_routes: Sequence[Sequence[int]] | None = None) -> SolverResult:
    """Search every plan type from iteration one and return checked best-so-far."""
    cfg = config or JointRelayConfig()
    started = perf_counter()
    deadline = None if cfg.time_limit_seconds is None else started + cfg.time_limit_seconds
    current = _initial_routes(problem) if initial_routes is None else tuple(tuple(r) for r in initial_routes)
    if len(current) > problem.drone_count:
        raise ValueError("传入路线数量超过无人机数量")
    current += ((),) * (problem.drone_count - len(current))
    initial = evaluate_solution(problem, current, _force_full=True)
    if not initial.valid:
        raise ValueError(f"传入初始解非法: {initial.violations}")
    trajectory = [(0, perf_counter() - started, initial.score.late_count,
                   initial.score.total_lateness_min, initial.score.distance_km)]
    evaluator = RouteEvaluator(problem)
    global_evaluator = GlobalRelayEvaluator(problem)
    context = _Context(problem, cfg, evaluator, global_evaluator, started, deadline,
                       current, initial, trajectory)
    rng, current_score = Random(cfg.seed), initial.score
    neighborhood, stalled, completed = 0, 0, 0
    uses = {"joint_pair": 0, "single_plan": 0, "adjacent_order": 0}
    accepted = 0
    for iteration in range(1, cfg.max_iterations + 1):
        try:
            _check(deadline)
            context.iteration = iteration
            index = build_plan_index(problem, current)
            first, second = _select_tasks(context, current, index, rng)
            if neighborhood == 0 and second is not None:
                uses["joint_pair"] += 1
                candidate = _joint_neighbor(context, current, (first, second), index)
            elif neighborhood <= 1:
                uses["single_plan"] += 1
                candidate = _single_neighbor(context, current, first, index)
            else:
                uses["adjacent_order"] += 1
                candidate = _adjacent_neighbor(context, current, (iteration // 3) % len(current))
            _check(deadline)
            completed = iteration
            if candidate is not None and candidate.score < current_score:
                current, current_score = candidate.routes, candidate.score
                accepted += 1
                context.work["accepted_improvements"] += 1
                neighborhood, stalled = 0, 0
            else:
                stalled += 1
                neighborhood = (neighborhood + 1) % 3
                if (candidate is not None and stalled >= cfg.perturb_after
                        and candidate.score.late_count <= context.best_evaluation.score.late_count + cfg.perturb_max_extra_late):
                    # This is a fully checked feasible shake, not a best update.
                    current, current_score = candidate.routes, candidate.score
                    accepted += 1
                    context.work["accepted_perturbations"] += 1
                    neighborhood, stalled = 0, 0
        except _DeadlineReached:
            context.work["deadline_reached"] = True
            # A complete best candidate confirmed during this interrupted
            # neighborhood is already retained; count the attempted iteration
            # so every recorded trajectory index remains within iterations.
            completed = max(completed, context.iteration)
            break
    final = evaluate_solution(problem, context.best, _force_full=True)
    if not final.valid or final.score > initial.score:
        raise RuntimeError("联动搜索丢失合法历史最优解")
    final_schedule = global_evaluator.evaluate(context.best)
    stats = relay_statistics(problem, context.best, final_schedule, build_plan_index(problem, context.best))
    elapsed = perf_counter() - started
    metadata = {
        "method": "Joint-Relay-VNS", "seed": cfg.seed,
        "relay_enabled": problem.has_relays, "time_limit_seconds": cfg.time_limit_seconds,
        "initial_score": (initial.score.late_count, initial.score.total_lateness_min, initial.score.distance_km),
        "best_trajectory": tuple(context.trajectory),
        "best_update_count": len(context.trajectory) - 1,
        "time_to_best_seconds": context.trajectory[-1][1],
        "accepted_solutions": accepted,
        "iterations_per_second": completed / elapsed if elapsed > 0 else 0.,
        "operator_uses": MappingProxyType(uses),
        "operator_statistics": MappingProxyType({}),
        "operator_weights": MappingProxyType({}),
        "profiling": MappingProxyType({**evaluator.counters.as_dict(), **context.work}),
        "joint_search": MappingProxyType(dict(context.work)),
        "relay": MappingProxyType(stats), "direct_warmup": False,
    }
    return SolverResult(context.best, final, elapsed, completed, MappingProxyType(metadata))
