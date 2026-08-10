"""Buffered static relay search built beside the legacy signed-route ALNS."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import exp, isfinite
from random import Random
from time import perf_counter
from types import MappingProxyType
from typing import Iterable, Mapping, Sequence

from .alns import ALNSConfig, construct_regret_initial, solve_alns
from .relay import RelayPlan, RelayProblem, TaskCountSemantics
from .relay_search import (
    change_hub_candidates,
    change_receiver_candidates,
    direct_task_ids,
    merge_task_candidates,
    relay_task_ids,
    split_task_candidates,
)
from .relay_validation import RelayEvaluation, evaluate_relay_plan


@dataclass(frozen=True, slots=True)
class RelayALNSConfig:
    """Search limits for the relay neighbourhood layer."""

    max_iterations: int = 400
    time_limit_seconds: float | None = None
    seed: int = 20260805
    candidate_limit: int = 12
    task_sample_size: int = 24
    base_search_fraction: float = 0.80
    weight_update_interval: int = 20
    reaction_factor: float = 0.20
    minimum_weight: float = 0.10
    enable_non_improving_acceptance: bool = True
    initial_temperature: float = 0.01
    cooling_rate: float = 0.995
    minimum_temperature: float = 0.0001

    def __post_init__(self) -> None:
        if self.max_iterations < 0:
            raise ValueError("接力搜索迭代次数不能为负")
        if self.time_limit_seconds is not None and (
            not isfinite(self.time_limit_seconds) or self.time_limit_seconds <= 0
        ):
            raise ValueError("接力搜索时间上限必须为正数")
        if self.candidate_limit <= 0:
            raise ValueError("接力候选上限必须为正整数")
        if self.task_sample_size <= 0:
            raise ValueError("每轮接力任务样本数必须为正整数")
        if not isfinite(self.base_search_fraction) or not (
            0 < self.base_search_fraction < 1
        ):
            raise ValueError("基础搜索时间占比必须位于 (0, 1)")
        if self.weight_update_interval <= 0:
            raise ValueError("接力算子权重更新间隔必须为正整数")
        if not isfinite(self.reaction_factor) or not (
            0 < self.reaction_factor <= 1
        ):
            raise ValueError("接力算子反应系数必须位于 (0, 1]")
        if not isfinite(self.minimum_weight) or self.minimum_weight <= 0:
            raise ValueError("接力算子最小权重必须为有限正数")
        if not isfinite(self.initial_temperature) or self.initial_temperature < 0:
            raise ValueError("接力搜索初始温度必须为有限非负数")
        if not isfinite(self.cooling_rate) or not (0 < self.cooling_rate <= 1):
            raise ValueError("接力搜索降温率必须位于 (0, 1]")
        if (
            not isfinite(self.minimum_temperature)
            or self.minimum_temperature < 0
            or self.minimum_temperature > self.initial_temperature
        ):
            raise ValueError("接力搜索最低温度必须位于 [0, 初始温度]")


@dataclass(frozen=True, slots=True)
class RelaySolverResult:
    """A complete relay plan and its independent global evaluation."""

    plan: RelayPlan
    evaluation: RelayEvaluation
    runtime_seconds: float = 0.0
    iterations: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict, compare=False)


def competition_key(evaluation: RelayEvaluation) -> tuple[int, float]:
    """Return the two official competition objectives in lexicographic order."""

    return (
        evaluation.score.late_count,
        evaluation.score.distance_km,
    )


_RELAY_OPERATORS = (
    "split",
    "merge",
    "change_receiver",
    "change_hub",
)


def _stable_search_key(evaluation: RelayEvaluation) -> tuple[int, float, float]:
    """Use total lateness only to make equal official scores deterministic."""

    return (*competition_key(evaluation), evaluation.score.total_lateness_min)


def _roulette(
    weights: Mapping[str, float], names: Sequence[str], rng: Random
) -> str:
    total = sum(weights[name] for name in names)
    threshold = rng.random() * total
    cumulative = 0.0
    for name in names:
        cumulative += weights[name]
        if threshold <= cumulative:
            return name
    return names[-1]


def _non_improving_gap(
    candidate: RelayEvaluation,
    current: RelayEvaluation,
    task_count: int,
) -> float:
    """Map a lexicographically worse score to a scale-free SA energy gap."""

    candidate_key = competition_key(candidate)
    current_key = competition_key(current)
    if candidate_key < current_key:
        return 0.0
    if candidate_key[0] > current_key[0]:
        late_gap = (candidate_key[0] - current_key[0]) / max(1, task_count)
        distance_gap = max(0.0, candidate_key[1] - current_key[1]) / max(
            1.0, current_key[1]
        )
        return late_gap + distance_gap
    if candidate_key[1] > current_key[1]:
        return (candidate_key[1] - current_key[1]) / max(
            1.0, current_key[1]
        )
    return max(
        0.0,
        candidate.score.total_lateness_min
        - current.score.total_lateness_min,
    ) / max(1.0, current.score.total_lateness_min)


def _accept_non_improving(
    candidate: RelayEvaluation,
    current: RelayEvaluation,
    *,
    task_count: int,
    temperature: float,
    rng: Random,
) -> bool:
    if temperature <= 0:
        return False
    gap = _non_improving_gap(candidate, current, task_count)
    if gap <= 0:
        return True
    return rng.random() < exp(-gap / temperature)


def _sample_relay_tasks(
    task_ids: Sequence[int], sample_size: int, rng: Random
) -> tuple[int, ...]:
    ordered = tuple(sorted(task_ids))
    if len(ordered) <= sample_size:
        return ordered
    return tuple(sorted(rng.sample(ordered, k=sample_size)))


def _operator_candidates(
    problem: RelayProblem,
    plan: RelayPlan,
    evaluation: RelayEvaluation,
    operator: str,
    cfg: RelayALNSConfig,
    rng: Random,
) -> Iterable[RelayPlan]:
    if operator == "split":
        ranked_tasks = sorted(
            direct_task_ids(plan),
            key=lambda task_id: (
                -max(
                    0.0,
                    evaluation.delivery_times_min.get(task_id, 0.0)
                    - problem.task(task_id).deadline_min,
                ),
                problem.task(task_id).deadline_min,
                task_id,
            ),
        )
        if len(ranked_tasks) > cfg.task_sample_size:
            urgent_count = max(1, cfg.task_sample_size // 2)
            urgent = ranked_tasks[:urgent_count]
            remainder = ranked_tasks[urgent_count:]
            ranked_tasks = urgent + rng.sample(
                remainder,
                k=min(cfg.task_sample_size - len(urgent), len(remainder)),
            )
        for task_id in ranked_tasks:
            yield from split_task_candidates(
                problem,
                plan,
                task_id,
                candidate_limit=cfg.candidate_limit,
            )
        return

    task_ids = _sample_relay_tasks(
        relay_task_ids(plan), cfg.task_sample_size, rng
    )
    for task_id in task_ids:
        if operator == "merge":
            yield from merge_task_candidates(
                problem,
                plan,
                task_id,
                candidate_limit=cfg.candidate_limit,
            )
        elif operator == "change_receiver":
            yield from change_receiver_candidates(
                problem,
                plan,
                task_id,
                candidate_limit=cfg.candidate_limit,
            )
        elif operator == "change_hub":
            yield from change_hub_candidates(problem, plan, task_id)
        else:
            raise ValueError(f"未知接力算子 {operator}")


def solve_relay_alns(
    problem: RelayProblem,
    *,
    config: RelayALNSConfig | None = None,
    initial_routes: Sequence[Sequence[int]] | None = None,
    initial_plan: RelayPlan | None = None,
) -> RelaySolverResult:
    """Start the relay layer from a complete legacy solution."""

    cfg = config or RelayALNSConfig()
    started = perf_counter()
    rng = Random(cfg.seed)
    base = problem.base_problem
    relay_search_disabled_reason: str | None = None
    if (
        problem.task_count_semantics is TaskCountSemantics.STRICT_TOUCH
        and len(base.tasks) == base.drone_count * base.max_tasks_per_drone
    ):
        relay_search_disabled_reason = "strict_touch_saturated"
    elif not problem.hubs:
        relay_search_disabled_reason = "no_hubs"
    elif problem.max_handoffs_per_task == 0:
        relay_search_disabled_reason = "handoffs_disabled"

    base_search_used = False
    base_search_runtime_seconds = 0.0
    base_search_iterations = 0
    if initial_routes is not None and initial_plan is not None:
        raise ValueError("initial_routes 与 initial_plan 不能同时指定")
    if initial_routes is None and initial_plan is None:
        if cfg.max_iterations == 0:
            initial = construct_regret_initial(base)
        else:
            base_time_limit = cfg.time_limit_seconds
            if (
                base_time_limit is not None
                and relay_search_disabled_reason is None
            ):
                base_time_limit *= cfg.base_search_fraction
            initial = solve_alns(
                base,
                config=ALNSConfig(
                    max_iterations=cfg.max_iterations,
                    time_limit_seconds=base_time_limit,
                    seed=cfg.seed,
                    candidate_limit=48,
                    enable_assignment_destroy=True,
                    enable_deadline_risk=True,
                    enable_late_risk_destroy=True,
                    enable_on_time_distance_objective=True,
                    enable_rejection_pool=False,
                    enable_soft_deadline=False,
                    enable_vnd=False,
                    enable_cluster_repair=False,
                    enable_ejection=False,
                    enable_route_pool=False,
                ),
            )
            base_search_used = True
        initial_routes = initial.routes
        base_search_runtime_seconds = initial.runtime_seconds
        base_search_iterations = initial.iterations
    plan = (
        initial_plan
        if initial_plan is not None
        else RelayPlan.from_signed_routes(initial_routes or ())
    )
    evaluation = evaluate_relay_plan(problem, plan)
    if not evaluation.valid:
        raise ValueError(f"传入的接力初始解非法: {evaluation.violations}")
    deadline = (
        None
        if cfg.time_limit_seconds is None
        else started + cfg.time_limit_seconds
    )
    current_plan = plan
    current_evaluation = evaluation
    best_plan = plan
    best_evaluation = evaluation
    initial_search_key = _stable_search_key(evaluation)
    time_to_best_seconds = perf_counter() - started
    operator_weights = {name: 1.0 for name in _RELAY_OPERATORS}
    operator_uses = {name: 0 for name in _RELAY_OPERATORS}
    operator_accepts = {name: 0 for name in _RELAY_OPERATORS}
    operator_improvements = {name: 0 for name in _RELAY_OPERATORS}
    operator_no_candidates = {name: 0 for name in _RELAY_OPERATORS}
    operator_rewards = {name: 0.0 for name in _RELAY_OPERATORS}
    segment_uses = {name: 0 for name in _RELAY_OPERATORS}
    segment_rewards = {name: 0.0 for name in _RELAY_OPERATORS}
    evaluated_candidates = 0
    accepted_non_improving = 0
    accepted_equal = 0
    temperature = cfg.initial_temperature
    completed_iterations = 0

    iteration_range = (
        ()
        if relay_search_disabled_reason is not None
        else range(1, cfg.max_iterations + 1)
    )
    for iteration in iteration_range:
        if deadline is not None and perf_counter() >= deadline:
            break
        direct_tasks = direct_task_ids(current_plan)
        relay_tasks = relay_task_ids(current_plan)
        available_operators: list[str] = []
        if direct_tasks:
            available_operators.append("split")
        if relay_tasks:
            available_operators.extend(("merge", "change_receiver"))
            if len(problem.hubs) > 1:
                available_operators.append("change_hub")
        if not available_operators:
            break

        operator = _roulette(operator_weights, available_operators, rng)
        operator_uses[operator] += 1
        segment_uses[operator] += 1
        candidate_plan: RelayPlan | None = None
        candidate_evaluation: RelayEvaluation | None = None
        timed_out = False
        for candidate in _operator_candidates(
            problem,
            current_plan,
            current_evaluation,
            operator,
            cfg,
            rng,
        ):
            if deadline is not None and perf_counter() >= deadline:
                timed_out = True
                break
            evaluated = evaluate_relay_plan(problem, candidate)
            evaluated_candidates += 1
            if not evaluated.valid:
                continue
            if (
                candidate_evaluation is None
                or _stable_search_key(evaluated)
                < _stable_search_key(candidate_evaluation)
            ):
                candidate_plan = candidate
                candidate_evaluation = evaluated

        reward = 0.0
        if candidate_plan is None or candidate_evaluation is None:
            operator_no_candidates[operator] += 1
        else:
            candidate_key = _stable_search_key(candidate_evaluation)
            current_key = _stable_search_key(current_evaluation)
            improved_current = candidate_key < current_key
            equal_current = candidate_key == current_key
            accepted = (improved_current or equal_current) and not timed_out
            if (
                not accepted
                and cfg.enable_non_improving_acceptance
                and not timed_out
            ):
                accepted = _accept_non_improving(
                    candidate_evaluation,
                    current_evaluation,
                    task_count=len(base.tasks),
                    temperature=temperature,
                    rng=rng,
                )
                if accepted:
                    accepted_non_improving += 1
            if equal_current and accepted:
                accepted_equal += 1
            if accepted and not timed_out:
                current_plan = candidate_plan
                current_evaluation = candidate_evaluation
                operator_accepts[operator] += 1
                reward = 4.0 if improved_current else (2.0 if equal_current else 1.0)
            if accepted and candidate_key < _stable_search_key(best_evaluation):
                best_plan = candidate_plan
                best_evaluation = candidate_evaluation
                operator_improvements[operator] += 1
                reward = 8.0
                time_to_best_seconds = perf_counter() - started
        operator_rewards[operator] += reward
        segment_rewards[operator] += reward

        if iteration % cfg.weight_update_interval == 0:
            for name in _RELAY_OPERATORS:
                if segment_uses[name]:
                    observed = segment_rewards[name] / segment_uses[name]
                    operator_weights[name] = max(
                        cfg.minimum_weight,
                        (1 - cfg.reaction_factor) * operator_weights[name]
                        + cfg.reaction_factor * observed,
                    )
            segment_uses = {name: 0 for name in _RELAY_OPERATORS}
            segment_rewards = {name: 0.0 for name in _RELAY_OPERATORS}

        temperature = max(
            cfg.minimum_temperature, temperature * cfg.cooling_rate
        )

        completed_iterations = iteration
        if timed_out:
            break
    plan = best_plan
    evaluation = best_evaluation
    metadata = MappingProxyType(
        {
            "method": "Buffered-Static-Relay-ALNS",
            "task_count_semantics": problem.task_count_semantics.value,
            "semantics_extension": problem.semantics_extension,
            "competition_objective_order": ("late_count", "distance_km"),
            "search_score_order": (
                "late_count",
                "distance_km",
                "total_lateness_min_tie_break_only",
            ),
            "returned_solution": "best_so_far",
            "operator_selection": "adaptive_weighted_roulette",
            "acceptance_rule": (
                "simulated_annealing_normalized_lex_gap"
            ),
            "reward_scores": MappingProxyType(
                {
                    "new_best": 8.0,
                    "improved_current": 4.0,
                    "equal_current": 2.0,
                    "accepted_non_improving": 1.0,
                    "rejected": 0.0,
                }
            ),
            "relay_search_disabled_reason": relay_search_disabled_reason,
            "seed": cfg.seed,
            "candidate_limit": cfg.candidate_limit,
            "task_sample_size": cfg.task_sample_size,
            "search_tie_break": "total_lateness_min",
            "initial_competition_key": initial_search_key[:2],
            "best_competition_key": competition_key(best_evaluation),
            "current_competition_key": competition_key(current_evaluation),
            "best_stable_search_key": _stable_search_key(best_evaluation),
            "current_stable_search_key": _stable_search_key(current_evaluation),
            "current_is_best": (
                _stable_search_key(current_evaluation)
                == _stable_search_key(best_evaluation)
            ),
            "time_to_best_seconds": time_to_best_seconds,
            "accepted_splits": operator_improvements["split"],
            "accepted_merges": operator_improvements["merge"],
            "accepted_hub_changes": operator_improvements["change_hub"],
            "accepted_receiver_changes": operator_improvements[
                "change_receiver"
            ],
            "accepted_solutions": sum(operator_accepts.values()),
            "accepted_non_improving": accepted_non_improving,
            "accepted_equal": accepted_equal,
            "evaluated_candidates": evaluated_candidates,
            "operator_uses": MappingProxyType(dict(operator_uses)),
            "operator_accepts": MappingProxyType(dict(operator_accepts)),
            "operator_improvements": MappingProxyType(
                dict(operator_improvements)
            ),
            "operator_no_candidates": MappingProxyType(
                dict(operator_no_candidates)
            ),
            "operator_rewards": MappingProxyType(dict(operator_rewards)),
            "operator_weights": MappingProxyType(dict(operator_weights)),
            "weight_update_interval": cfg.weight_update_interval,
            "reaction_factor": cfg.reaction_factor,
            "minimum_weight": cfg.minimum_weight,
            "enable_non_improving_acceptance": (
                cfg.enable_non_improving_acceptance
            ),
            "initial_temperature": cfg.initial_temperature,
            "final_temperature": temperature,
            "cooling_rate": cfg.cooling_rate,
            "minimum_temperature": cfg.minimum_temperature,
            "base_search_used": base_search_used,
            "base_search_runtime_seconds": base_search_runtime_seconds,
            "base_search_iterations": base_search_iterations,
            "base_search_objective_order": ("late_count", "distance_km"),
            "base_search_fraction": cfg.base_search_fraction,
        }
    )
    return RelaySolverResult(
        plan=plan,
        evaluation=evaluation,
        runtime_seconds=perf_counter() - started,
        iterations=completed_iterations,
        metadata=metadata,
    )
