"""Capacity-2 lexicographic hybrid adaptive large-neighbourhood search."""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import exp, isfinite
from random import Random
from time import perf_counter
from types import MappingProxyType
from typing import Iterable, Sequence

from .model import Problem, Score
from .search import (
    Route,
    RouteEvaluator,
    RouteInsertionOption,
    Routes,
    SolverResult,
    route_insertion_options,
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
    "route_clear",
)
REPAIR_OPERATORS = ("greedy", "regret2", "regret3", "deadline", "slack")


@dataclass(frozen=True, slots=True)
class ALNSConfig:
    max_iterations: int = 400
    time_limit_seconds: float | None = None
    seed: int = 20260805
    candidate_limit: int | None = 48
    min_destroy_fraction: float = 0.04
    max_destroy_fraction: float = 0.10
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
    enable_route_pool: bool = True
    enable_ejection: bool = True

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


def _task_ids_in_routes(routes: Sequence[Sequence[int]]) -> tuple[int, ...]:
    return tuple(visit for route in routes for visit in route if visit > 0)


def _remove_tasks(routes: Sequence[Sequence[int]], task_ids: Iterable[int]) -> Routes:
    removed = set(task_ids)
    return tuple(
        tuple(visit for visit in route if abs(visit) not in removed)
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


def destroy_solution(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    operator: str,
    count: int,
    rng: Random,
) -> tuple[Routes, tuple[int, ...]]:
    """Remove complete task pairs using one named destroy operator."""

    task_ids = list(_task_ids_in_routes(routes))
    count = min(max(1, count), len(task_ids))
    route_by_task = _task_route_index(routes)

    if operator == "random":
        removed = rng.sample(task_ids, count)
    elif operator in {"worst_distance", "worst_lex"}:
        ranked: list[tuple[Score | float, int]] = []
        for task_id in task_ids:
            route_index = route_by_task[task_id]
            route = routes[route_index]
            reduced = tuple(visit for visit in route if abs(visit) != task_id)
            old_score = evaluator.evaluate(route).score
            new_score = evaluator.evaluate(reduced).score
            gain = old_score - new_score
            key: Score | float = gain if operator == "worst_lex" else gain.distance_km
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
        for route in routes:
            metrics = evaluator.evaluate(route)
            for task_id, delivered_at in metrics.delivery_times_min.items():
                urgency.append(
                    (problem.task(task_id).deadline_min - delivered_at, task_id)
                )
        urgency.sort()
        removed = _choose_ranked([item[1] for item in urgency], count, rng, 3.0)
    elif operator == "route_segment":
        non_empty = [route for route in routes if route]
        route = rng.choice(non_empty)
        start = rng.randrange(len(route))
        ordered = []
        for offset in range(len(route)):
            task_id = abs(route[(start + offset) % len(route)])
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
            for task_id, interval in positions.items():
                overlap = sum(interval[0] <= pos < interval[-1] for pos in full_positions)
                conflicts.append((overlap, task_id))
        conflicts.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        removed = _choose_ranked([item[1] for item in conflicts], count, rng)
    elif operator == "route_clear":
        candidates = [route for route in routes if route]
        route = rng.choice(candidates)
        local = list(dict.fromkeys(abs(visit) for visit in route))
        rng.shuffle(local)
        remaining = [task for task in task_ids if task not in local]
        rng.shuffle(remaining)
        removed = (local + remaining)[:count]
    else:
        raise ValueError(f"未知破坏算子 {operator}")

    unique_removed = tuple(dict.fromkeys(removed))
    return _remove_tasks(routes, unique_removed), unique_removed


def _all_options_for_task(
    problem: Problem,
    evaluator: RouteEvaluator,
    routes: Routes,
    task_id: int,
    candidate_limit: int | None,
    option_count: int,
    cache: dict[tuple[int, int, Route, int | None, int], tuple[RouteInsertionOption, ...]],
) -> tuple[RouteInsertionOption, ...]:
    options: list[RouteInsertionOption] = []
    for route_index, route in enumerate(routes):
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
            )
            cache[key] = route_options
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
    return tuple(options[:option_count])


def _repair(
    problem: Problem,
    evaluator: RouteEvaluator,
    partial_routes: Routes,
    removed_task_ids: Iterable[int],
    strategy: str,
    candidate_limit: int | None,
) -> Routes:
    routes = tuple(tuple(route) for route in partial_routes)
    remaining = set(removed_task_ids)
    cache: dict[
        tuple[int, int, Route, int | None, int], tuple[RouteInsertionOption, ...]
    ] = {}
    regret_k = 3 if strategy == "regret3" else 2

    while remaining:
        option_count = regret_k if strategy.startswith("regret") else 1
        per_task: dict[int, tuple[RouteInsertionOption, ...]] = {}
        for task_id in sorted(remaining):
            options = _all_options_for_task(
                problem,
                evaluator,
                routes,
                task_id,
                candidate_limit,
                option_count,
                cache,
            )
            if not options:
                raise RuntimeError(f"修复阶段无法重新插入任务 {task_id}")
            per_task[task_id] = options

        if strategy == "greedy":
            chosen_task = min(
                remaining,
                key=lambda task_id: (
                    per_task[task_id][0].delta,
                    problem.task(task_id).deadline_min,
                    task_id,
                ),
            )
        elif strategy in {"deadline", "slack"}:
            if strategy == "deadline":
                chosen_task = min(
                    remaining,
                    key=lambda task_id: (
                        problem.task(task_id).deadline_min,
                        task_id,
                    ),
                )
            else:
                chosen_task = min(
                    remaining,
                    key=lambda task_id: (
                        problem.task(task_id).deadline_min
                        - problem.direct_completion_min(task_id),
                        task_id,
                    ),
                )
        elif strategy in {"regret2", "regret3"}:

            def regret_key(task_id: int) -> tuple[float, float, float, float, int]:
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

        choice = per_task[chosen_task][0]
        mutable = list(routes)
        mutable[choice.route_index] = choice.route
        routes = tuple(mutable)
        remaining.remove(chosen_task)
    return routes


def construct_regret_initial(
    problem: Problem, *, candidate_limit: int | None = 48
) -> SolverResult:
    """Construct a complete deterministic regret-2 initial solution."""

    started = perf_counter()
    evaluator = RouteEvaluator(problem)
    routes: Routes = tuple(() for _ in range(problem.drone_count))
    routes = _repair(
        problem,
        evaluator,
        routes,
        problem.task_ids,
        "regret2",
        candidate_limit,
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
            delta = evaluator.evaluate(candidate).score - evaluator.evaluate(route).score
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
                delta = evaluator.evaluate(candidate).score - evaluator.evaluate(route).score
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
        for route in routes:
            tasks = frozenset(visit for visit in route if visit > 0)
            if not tasks:
                continue
            column = _RouteColumn(tasks, route, self.evaluator.evaluate(route).score)
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
                if (partial_score, candidate) < (best_score, best_routes):
                    best_score = partial_score
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
    for route in routes:
        metrics = evaluator.evaluate(route)
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
    evaluator = RouteEvaluator(problem)
    if initial_routes is None:
        current = construct_regret_initial(
            problem, candidate_limit=cfg.candidate_limit
        ).routes
    else:
        current = tuple(tuple(route) for route in initial_routes)
        initial_validation = evaluate_solution(problem, current)
        if not initial_validation.valid:
            raise ValueError(f"传入的初始解非法: {initial_validation.violations}")
        current = current + tuple(
            () for _ in range(problem.drone_count - len(current))
        )
    current_score = routes_score(evaluator, current)
    initial_score = current_score
    best = current
    best_score = current_score
    time_to_best = perf_counter() - started

    destroy_weights = {name: 1.0 for name in DESTROY_OPERATORS}
    repair_weights = {name: 1.0 for name in REPAIR_OPERATORS}
    total_uses = {
        **{f"destroy:{name}": 0 for name in DESTROY_OPERATORS},
        **{f"repair:{name}": 0 for name in REPAIR_OPERATORS},
    }
    segment_uses = {key: 0 for key in total_uses}
    segment_rewards = {key: 0.0 for key in total_uses}
    accepted_count = 0
    temperature = cfg.initial_temperature
    route_pool = _RoutePool(problem, evaluator) if cfg.enable_route_pool else None
    if route_pool is not None:
        route_pool.add(best)
    completed_iterations = 0

    for iteration in range(1, cfg.max_iterations + 1):
        if deadline is not None and perf_counter() >= deadline:
            break
        destroy_name = _roulette(destroy_weights, rng)
        repair_name = _roulette(repair_weights, rng)
        minimum_pairs = 1 if len(problem.tasks) == 1 else 2
        lower = max(
            minimum_pairs,
            round(len(problem.tasks) * cfg.min_destroy_fraction),
        )
        upper = max(lower, round(len(problem.tasks) * cfg.max_destroy_fraction))
        remove_count = rng.randint(lower, upper)
        partial, removed = destroy_solution(
            problem,
            evaluator,
            current,
            destroy_name,
            remove_count,
            rng,
        )
        if deadline is not None and perf_counter() >= deadline:
            break
        candidate = _repair(
            problem,
            evaluator,
            partial,
            removed,
            repair_name,
            cfg.candidate_limit,
        )
        if deadline is not None and perf_counter() >= deadline:
            break
        if (
            cfg.enable_ejection
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
        if deadline is not None and perf_counter() >= deadline:
            break
        candidate_score = routes_score(evaluator, candidate)
        if deadline is not None and perf_counter() >= deadline:
            break
        improved_current = candidate_score < current_score
        accepted = _accept_worse(
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
        if candidate_score < best_score:
            best = candidate
            best_score = candidate_score
            time_to_best = perf_counter() - started
            reward = 8.0
            if route_pool is not None:
                route_pool.add(best)

        destroy_key = f"destroy:{destroy_name}"
        repair_key = f"repair:{repair_name}"
        for key in (destroy_key, repair_key):
            total_uses[key] += 1
            segment_uses[key] += 1
            segment_rewards[key] += reward

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
                route_pool.add(best)

        if iteration % cfg.weight_update_interval == 0:
            for name in DESTROY_OPERATORS:
                key = f"destroy:{name}"
                if segment_uses[key]:
                    observed = segment_rewards[key] / segment_uses[key]
                    destroy_weights[name] = max(
                        cfg.minimum_weight,
                        (1 - cfg.reaction_factor) * destroy_weights[name]
                        + cfg.reaction_factor * observed,
                    )
            for name in REPAIR_OPERATORS:
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
            "time_limit_seconds": cfg.time_limit_seconds,
            "initial_score": (
                initial_score.late_count,
                initial_score.total_lateness_min,
                initial_score.distance_km,
            ),
            "time_to_best_seconds": time_to_best,
            "accepted_solutions": accepted_count,
            "operator_uses": MappingProxyType(dict(total_uses)),
            "operator_weights": MappingProxyType(all_weights),
            "route_pool_columns": len(route_pool.columns) if route_pool else 0,
            "route_pool_last_nodes": route_pool.last_nodes if route_pool else 0,
        }
    )
    return SolverResult(
        routes=best,
        evaluation=evaluation,
        runtime_seconds=perf_counter() - started,
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
