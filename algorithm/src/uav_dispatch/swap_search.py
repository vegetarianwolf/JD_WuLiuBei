"""Stage-2 swap search for the Swap-only cooperative model.

Stage 2 fixes the pickup ownership produced by Stage 1 (A2): every drone still
picks up its original 25 tasks.  The search then introduces, removes and
relocates bilateral one-for-one swaps to improve the strict two-layer
objective ``(late_count, distance_km)``.

Candidate generation is deliberately pruned (late-task shortlist, partner
shortlist, meeting-point shortlist by detour lower bound, bounded gap and
delivery positions) so the search never enumerates the full
``tasks x tasks x nodes x positions`` space.  Every candidate is nevertheless
validated in full by :func:`~uav_dispatch.swap_validation.evaluate_swap_solution`;
pruning only affects speed, never the feasibility verdict.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from math import exp, isfinite
from random import Random
from time import perf_counter
from types import MappingProxyType
from typing import Mapping, Sequence

from .alns import ALNSConfig, solve_alns_core
from .model import Problem
from .search import Routes
from .swap_model import SwapEvent, SwapSolution, swap_solution_from_routes
from .swap_validation import (
    SwapSolutionEvaluation,
    evaluate_swap_solution,
    gap_completion_times,
)


@dataclass(frozen=True, slots=True)
class SwapSolverResult:
    """Full result of the two-stage Swap-only solver."""

    routes: Routes
    swaps: tuple[SwapEvent, ...]
    evaluation: SwapSolutionEvaluation
    runtime_seconds: float = 0.0
    iterations: int = 0
    metadata: Mapping[str, object] = field(default_factory=dict, compare=False)


@dataclass(frozen=True, slots=True)
class SwapConfig:
    """Computation-control parameters for the stage-2 swap search.

    None of these are problem constraints; they only bound the search effort.
    """

    seed: int = 2026080501
    time_limit_seconds: float | None = None
    max_iterations: int = 200
    swap_point_candidate_limit: int = 12
    swap_partner_limit: int = 12
    max_swaps: int = 20
    max_swaps_per_drone: int = 5
    swap_service_time_min: float = 0.0
    late_candidate_tasks: int = 20
    delivery_position_limit: int = 12
    random_fraction: float = 0.3
    full_eval_per_pair: int = 5
    initial_temperature: float = 0.03
    minimum_temperature: float = 0.0005
    cooling_rate: float = 0.995
    enable_distance_swap: bool = True
    enable_swap_relocate: bool = True
    enable_swap_remove: bool = True

    def __post_init__(self) -> None:
        if self.max_iterations < 0:
            raise ValueError("迭代次数不能为负")
        if self.time_limit_seconds is not None and (
            not isfinite(self.time_limit_seconds) or self.time_limit_seconds <= 0
        ):
            raise ValueError("时间上限必须为正数")
        if self.swap_point_candidate_limit < 1:
            raise ValueError("汇合点候选数至少为 1")
        if self.swap_partner_limit < 1:
            raise ValueError("伙伴候选数至少为 1")
        if self.max_swaps < 0 or self.max_swaps_per_drone < 0:
            raise ValueError("交换数量上限不能为负")
        if self.late_candidate_tasks < 1 or self.delivery_position_limit < 1:
            raise ValueError("候选任务数与送达位置数至少为 1")
        if not 0 <= self.random_fraction <= 1:
            raise ValueError("随机选择比例必须在 [0, 1] 内")
        if self.full_eval_per_pair < 1:
            raise ValueError("每对任务全量评价数至少为 1")
        if not isfinite(self.swap_service_time_min) or self.swap_service_time_min < 0:
            raise ValueError("交换作业时间必须为非负有限值")


# ---------------------------------------------------------------------------
# Structural helpers.
# ---------------------------------------------------------------------------

def pickup_owner(routes: Routes) -> dict[int, int]:
    """Map each task to the drone that performs its pickup (its owner)."""

    return {
        visit: drone
        for drone, route in enumerate(routes)
        for visit in route
        if visit > 0
    }


def all_meeting_nodes(problem: Problem) -> tuple[int, ...]:
    """Every pickup and delivery node is a legal rendezvous candidate."""

    return tuple(
        visit for task_id in problem.task_ids for visit in (task_id, -task_id)
    )


def _meeting_point_shortlist(
    problem: Problem,
    task_i: int,
    task_j: int,
    limit: int,
) -> tuple[int, ...]:
    """Top ``limit`` rendezvous candidates by a proxy detour lower bound.

    The proxy uses the pickup points of the two tasks as stand-ins for the
    drones' positions, so the shortlist is computed once per (i, j) pair.
    """

    a_prev = task_i
    b_prev = task_j
    a_next = -task_j
    b_next = -task_i
    base_a = problem.distance(a_prev, a_next)
    base_b = problem.distance(b_prev, b_next)
    ranked: list[tuple[float, int]] = []
    for meeting in all_meeting_nodes(problem):
        detour = (
            problem.distance(a_prev, meeting)
            + problem.distance(meeting, a_next)
            - base_a
            + problem.distance(b_prev, meeting)
            + problem.distance(meeting, b_next)
            - base_b
        )
        ranked.append((detour, meeting))
    ranked.sort()
    return tuple(meeting for _, meeting in ranked[:limit])


def _detour_lb(
    problem: Problem,
    routes: Routes,
    drone_a: int,
    drone_b: int,
    gap_a: int,
    gap_b: int,
    meeting: int,
    task_i: int,
    task_j: int,
) -> float:
    """Pure-distance detour lower bound for one candidate swap geometry."""

    route_a = routes[drone_a]
    route_b = routes[drone_b]
    a_prev = None if gap_a == 0 else route_a[gap_a - 1]
    b_prev = None if gap_b == 0 else route_b[gap_b - 1]
    a_next = -task_j
    b_next = -task_i
    base_a = problem.distance(a_prev, a_next)
    base_b = problem.distance(b_prev, b_next)
    detour_a = (
        problem.distance(a_prev, meeting)
        + problem.distance(meeting, a_next)
        - base_a
    )
    detour_b = (
        problem.distance(b_prev, meeting)
        + problem.distance(meeting, b_next)
        - base_b
    )
    return detour_a + detour_b


def _gap_options(
    routes: Routes,
    drone: int,
    task_id: int,
    limit: int,
) -> tuple[int, ...]:
    """Legal swap gaps for a drone: strictly after its pickup of ``task_id``.

    The delivery of ``task_id`` will be removed before the swap is inserted, so
    the route shrinks by one visit and the maximum gap is ``len(route) - 1``.
    Returns the earliest ``limit`` gaps (deliver the received parcel soon).
    """

    route = routes[drone]
    pickup_index = route.index(task_id)
    upper = len(route) - 1
    return tuple(
        range(pickup_index + 1, min(pickup_index + 1 + limit, upper + 1))
    )


def build_swap_candidate(
    solution: SwapSolution,
    task_i: int,
    task_j: int,
    drone_a: int,
    drone_b: int,
    gap_a: int,
    gap_b: int,
    meeting: int,
    new_swap_id: int,
) -> SwapSolution | None:
    """Construct the full Swap-only solution for one candidate swap.

    A gives ``task_i`` to B and receives ``task_j``.  The previous deliveries
    ``-task_i`` / ``-task_j`` are removed and the received parcels are inserted
    as the first visit right after each swap gap.  Returns ``None`` when the
    geometry is structurally illegal.
    """

    routes = solution.routes
    if drone_a >= len(routes) or drone_b >= len(routes):
        return None
    route_a = routes[drone_a]
    route_b = routes[drone_b]
    if -task_i not in route_a or -task_j not in route_b:
        return None
    route_a = tuple(visit for visit in route_a if visit != -task_i)
    route_b = tuple(visit for visit in route_b if visit != -task_j)
    if task_i not in route_a or task_j not in route_b:
        return None
    pickup_a = route_a.index(task_i)
    pickup_b = route_b.index(task_j)
    if not (pickup_a < gap_a <= len(route_a)):
        return None
    if not (pickup_b < gap_b <= len(route_b)):
        return None
    new_route_a = route_a[:gap_a] + (-task_j,) + route_a[gap_a:]
    new_route_b = route_b[:gap_b] + (-task_i,) + route_b[gap_b:]
    new_routes = list(routes)
    new_routes[drone_a] = new_route_a
    new_routes[drone_b] = new_route_b
    event = SwapEvent(
        id=new_swap_id,
        drone_a=drone_a,
        drone_b=drone_b,
        task_a_to_b=task_i,
        task_b_to_a=task_j,
        meeting_node=meeting,
        position_a=gap_a,
        position_b=gap_b,
    )
    return SwapSolution(tuple(new_routes), solution.swaps + (event,))


def _crossover_saving(
    problem: Problem,
    task_i: int,
    task_j: int,
) -> float:
    """Rough destination-crossover saving S_ij for the partner shortlist."""

    task_a = problem.task(task_i)
    task_b = problem.task(task_j)
    return (
        task_a.pickup.distance_to(task_a.delivery)
        + task_b.pickup.distance_to(task_b.delivery)
        - task_a.pickup.distance_to(task_b.delivery)
        - task_b.pickup.distance_to(task_a.delivery)
    )


def _partner_shortlist(
    problem: Problem,
    config: SwapConfig,
    evaluation: SwapSolutionEvaluation,
    owner: dict[int, int],
    task_i: int,
    drone_a: int,
) -> tuple[int, ...]:
    """Rank partner tasks for the late task ``task_i``.

    Combines destination crossover saving, deadline slack for on-time partners
    and a mutual-rescue bonus when both tasks are already late.
    """

    late_ids = {
        task_id
        for task_id in evaluation.delivery_times_min
        if evaluation.delivery_times_min[task_id]
        > problem.task(task_id).deadline_min + 1e-9
    }
    scores: dict[int, float] = {}
    for task_j in problem.task_ids:
        if task_j == task_i:
            continue
        if owner.get(task_j) == drone_a:
            continue
        if task_j not in evaluation.delivery_times_min:
            continue
        task = problem.task(task_j)
        saving = _crossover_saving(problem, task_i, task_j)
        scale = max(
            1.0,
            problem.direct_completion_min(task_i)
            + problem.direct_completion_min(task_j),
        )
        score = saving / scale
        if task_j in late_ids:
            score += 2.0
        else:
            slack = task.deadline_min - evaluation.delivery_times_min[task_j]
            score += min(1.0, max(0.0, slack / max(1.0, task.deadline_min)))
        scores[task_j] = score
    ranked = sorted(scores, key=lambda task_id: scores[task_id], reverse=True)
    return tuple(ranked[: config.swap_partner_limit])


def _choose_ranked(ranked: Sequence[int], count: int, rng: Random) -> list[int]:
    """Rank-based randomised selection weighted toward the head of the list."""

    available = list(ranked)
    chosen: list[int] = []
    while available and len(chosen) < count:
        index = int((rng.random() ** 4.0) * len(available))
        chosen.append(available.pop(index))
    return chosen


# ---------------------------------------------------------------------------
# Shared candidate enumeration with cheap rescue screening.
# ---------------------------------------------------------------------------

def _evaluate_swap_combos(
    problem: Problem,
    config: SwapConfig,
    solution: SwapSolution,
    gap_times: tuple[tuple[float, ...], ...],
    task_i: int,
    task_j: int,
    drone_a: int,
    drone_b: int,
    next_swap_id: int,
    *,
    rescue_task_id: int | None = None,
    deadline: float | None = None,
) -> list[tuple[SwapSolution, SwapSolutionEvaluation]]:
    """Enumerate pruned swap geometries and full-evaluate the best ones.

    The rescue screening uses the current-solution gap ready times to skip
    geometries that cannot possibly bring ``rescue_task_id`` on time.  The
    final feasibility verdict is always the full synchronised evaluator.
    """

    speed = problem.speed_km_per_min
    routes = solution.routes
    route_a = routes[drone_a]
    route_b = routes[drone_b]
    try:
        delivery_a_index = route_a.index(-task_i)
        delivery_b_index = route_b.index(-task_j)
    except ValueError:
        return []  # a parcel is already handed to another drone
    gaps_a = _gap_options(
        routes, drone_a, task_i, config.delivery_position_limit
    )
    gaps_b = _gap_options(
        routes, drone_b, task_j, config.delivery_position_limit
    )
    # The swap must sit before the original delivery so the rescue screening
    # using current-solution prefix times stays exact for this pair.
    gaps_a = tuple(gap for gap in gaps_a if gap <= delivery_a_index)
    gaps_b = tuple(gap for gap in gaps_b if gap <= delivery_b_index)
    if not gaps_a or not gaps_b:
        return []
    meetings = _meeting_point_shortlist(
        problem, task_i, task_j, config.swap_point_candidate_limit
    )

    def rescue_possible(gap_a: int, gap_b: int, meeting: int) -> bool:
        if rescue_task_id is None:
            return True
        a_prev = None if gap_a == 0 else route_a[gap_a - 1]
        b_prev = None if gap_b == 0 else route_b[gap_b - 1]
        arrival_a = gap_times[drone_a][gap_a] + problem.distance(
            a_prev, meeting
        ) / speed
        arrival_b = gap_times[drone_b][gap_b] + problem.distance(
            b_prev, meeting
        ) / speed
        delivery_est = max(arrival_a, arrival_b) + problem.distance(
            meeting, -rescue_task_id
        ) / speed
        return (
            delivery_est <= problem.task(rescue_task_id).deadline_min + 1e-9
        )

    combos: list[tuple[float, int, int, int]] = []
    for gap_a in gaps_a:
        for gap_b in gaps_b:
            for meeting in meetings:
                if not rescue_possible(gap_a, gap_b, meeting):
                    continue
                lb = _detour_lb(
                    problem,
                    routes,
                    drone_a,
                    drone_b,
                    gap_a,
                    gap_b,
                    meeting,
                    task_i,
                    task_j,
                )
                combos.append((lb, gap_a, gap_b, meeting))
    combos.sort(key=lambda item: item[0])

    candidates: list[tuple[SwapSolution, SwapSolutionEvaluation]] = []
    for _, gap_a, gap_b, meeting in combos[: config.full_eval_per_pair]:
        if deadline is not None and perf_counter() >= deadline:
            break
        candidate = build_swap_candidate(
            solution,
            task_i,
            task_j,
            drone_a,
            drone_b,
            gap_a,
            gap_b,
            meeting,
            next_swap_id,
        )
        if candidate is None:
            continue
        candidate_evaluation = evaluate_swap_solution(
            problem,
            candidate,
            swap_service_time_min=config.swap_service_time_min,
        )
        if candidate_evaluation.valid:
            candidates.append((candidate, candidate_evaluation))
    candidates.sort(key=lambda item: item[1].score)
    return candidates


# ---------------------------------------------------------------------------
# Operator S1: late rescue swap.
# ---------------------------------------------------------------------------

def late_rescue_swap(
    problem: Problem,
    config: SwapConfig,
    solution: SwapSolution,
    evaluation: SwapSolutionEvaluation,
    rng: Random,
    *,
    deadline: float | None = None,
) -> tuple[SwapSolution, SwapSolutionEvaluation] | None:
    """Try single one-for-one swaps that rescue currently-late tasks.

    Returns the best strictly-improving candidate (or ``None``).  Selection of
    which late tasks to attack is rank-based randomised (``random_fraction``
    of the time a fully random late task is tried).
    """

    owner = pickup_owner(solution.routes)
    swaps_by_task = {
        task_id: event
        for event in solution.swaps
        for task_id in (event.task_a_to_b, event.task_b_to_a)
    }
    swaps_per_drone: dict[int, int] = {}
    for event in solution.swaps:
        swaps_per_drone[event.drone_a] = swaps_per_drone.get(event.drone_a, 0) + 1
        swaps_per_drone[event.drone_b] = swaps_per_drone.get(event.drone_b, 0) + 1

    late_tasks = [
        task_id
        for task_id in evaluation.delivery_times_min
        if evaluation.delivery_times_min[task_id]
        > problem.task(task_id).deadline_min + 1e-9
    ]
    late_tasks = [t for t in late_tasks if t not in swaps_by_task]
    if not late_tasks:
        return None

    ranked_late = sorted(
        late_tasks,
        key=lambda task_id: -(
            evaluation.delivery_times_min[task_id]
            / max(1e-9, problem.task(task_id).deadline_min)
        ),
    )
    attack_count = min(config.late_candidate_tasks, len(ranked_late))
    attack = _choose_ranked(ranked_late, attack_count, rng)
    if rng.random() < config.random_fraction and late_tasks:
        attack.append(rng.choice(late_tasks))

    current_score = evaluation.score
    best: tuple[SwapSolution, SwapSolutionEvaluation] | None = None
    next_swap_id = max((event.id for event in solution.swaps), default=-1) + 1
    gap_times = gap_completion_times(
        problem,
        solution,
        swap_service_time_min=config.swap_service_time_min,
    )

    for task_i in attack:
        if deadline is not None and perf_counter() >= deadline:
            break
        drone_a = owner[task_i]
        if swaps_per_drone.get(drone_a, 0) >= config.max_swaps_per_drone:
            continue
        if len(solution.swaps) >= config.max_swaps:
            break
        partners = _partner_shortlist(
            problem, config, evaluation, owner, task_i, drone_a
        )
        for task_j in partners:
            drone_b = owner[task_j]
            if drone_b == drone_a or task_j in swaps_by_task:
                continue
            if swaps_per_drone.get(drone_b, 0) >= config.max_swaps_per_drone:
                continue
            candidates = _evaluate_swap_combos(
                problem,
                config,
                solution,
                gap_times,
                task_i,
                task_j,
                drone_a,
                drone_b,
                next_swap_id,
                rescue_task_id=task_i,
                deadline=deadline,
            )
            if not candidates:
                continue
            candidate, candidate_evaluation = candidates[0]
            if candidate_evaluation.score < current_score:
                if (
                    best is None
                    or candidate_evaluation.score < best[1].score
                ):
                    best = (candidate, candidate_evaluation)
    return best


def _rescue_candidate_for_task(
    problem: Problem,
    config: SwapConfig,
    solution: SwapSolution,
    evaluation: SwapSolutionEvaluation,
    task_i: int,
    *,
    partner_limit: int | None = None,
    deadline: float | None = None,
) -> tuple[SwapSolution, SwapSolutionEvaluation] | None:
    """Best single swap that rescues exactly one given late task.

    This is the oracle primitive used by the Swap Opportunity experiment: for a
    fixed baseline, every bounded (partner, meeting, gap) geometry is tried and
    the best candidate that makes ``task_i`` on time is returned.
    """

    owner = pickup_owner(solution.routes)
    swaps_by_task = {
        task_id: event
        for event in solution.swaps
        for task_id in (event.task_a_to_b, event.task_b_to_a)
    }
    drone_a = owner[task_i]
    partners = _partner_shortlist(
        problem,
        config,
        evaluation,
        owner,
        task_i,
        drone_a,
    )
    if partner_limit is not None:
        partners = partners[:partner_limit]
    best: tuple[SwapSolution, SwapSolutionEvaluation] | None = None
    next_swap_id = max((event.id for event in solution.swaps), default=-1) + 1
    gap_times = gap_completion_times(
        problem,
        solution,
        swap_service_time_min=config.swap_service_time_min,
    )
    for task_j in partners:
        if deadline is not None and perf_counter() >= deadline:
            break
        drone_b = owner[task_j]
        if drone_b == drone_a or task_j in swaps_by_task:
            continue
        candidates = _evaluate_swap_combos(
            problem,
            config,
            solution,
            gap_times,
            task_i,
            task_j,
            drone_a,
            drone_b,
            next_swap_id,
            rescue_task_id=task_i,
            deadline=deadline,
        )
        if not candidates:
            continue
        candidate, candidate_evaluation = candidates[0]
        if (
            candidate_evaluation.delivery_times_min.get(
                task_i, float("inf")
            )
            <= problem.task(task_i).deadline_min + 1e-9
        ):
            if best is None or candidate_evaluation.score < best[1].score:
                best = (candidate, candidate_evaluation)
    return best


def solve_swap_stage2(
    problem: Problem,
    baseline_routes: Sequence[Sequence[int]],
    config: SwapConfig | None = None,
) -> SwapSolverResult:
    """Run the stage-2 swap search over a fixed stage-1 baseline.

    The incumbent starts at the zero-swap baseline, so the returned solution is
    never worse than the input baseline under
    ``(late_count, total_lateness_min, distance_km)``.

    No longer terminates early when ``late_rescue_swap`` finds no single-step
    rescue — the search continues until the time budget expires, allowing the
    SA chain to explore the solution space.
    """

    cfg = config or SwapConfig()
    started = perf_counter()
    deadline = (
        None
        if cfg.time_limit_seconds is None
        else started + cfg.time_limit_seconds
    )
    rng = Random(cfg.seed)
    solution = swap_solution_from_routes(tuple(tuple(r) for r in baseline_routes))
    evaluation = evaluate_swap_solution(
        problem, solution, swap_service_time_min=cfg.swap_service_time_min
    )
    if not evaluation.valid:
        raise ValueError(f"Stage-2 基线解非法: {evaluation.violations}")
    best = solution
    best_evaluation = evaluation
    current = solution
    current_evaluation = evaluation
    temperature = cfg.initial_temperature
    completed_iterations = 0
    improved_count = 0

    for iteration in range(1, cfg.max_iterations + 1):
        if deadline is not None and perf_counter() >= deadline:
            break
        improved = late_rescue_swap(
            problem, cfg, current, current_evaluation, rng, deadline=deadline
        )
        if improved is None:
            # No single-step rescue available — continue exploring within
            # the time budget instead of giving up early.  The SA chain may
            # still find beneficial moves in later iterations.
            temperature = max(cfg.minimum_temperature, temperature * cfg.cooling_rate)
            completed_iterations = iteration
            continue
        candidate, candidate_evaluation = improved
        candidate_score = candidate_evaluation.score
        current_score = current_evaluation.score
        if candidate_score < current_score:
            current = candidate
            current_evaluation = candidate_evaluation
            improved_count += 1
            if candidate_score < best_evaluation.score:
                best = candidate
                best_evaluation = candidate_evaluation
        else:
            # Strict two-layer SA: (late_count, distance_km).
            # late_count increase → never accept.
            if candidate_score.late_count > current_score.late_count:
                pass  # reject
            elif candidate_score.late_count < current_score.late_count:
                current = candidate
                current_evaluation = candidate_evaluation
            else:
                # late_count equal: SA on distance
                from .alns import _accept_worse
                if _accept_worse(candidate_score, current_score, problem,
                                 temperature, rng):
                    current = candidate
                    current_evaluation = candidate_evaluation
        temperature = max(cfg.minimum_temperature, temperature * cfg.cooling_rate)
        completed_iterations = iteration

    metadata = MappingProxyType(
        {
            "method": "C2-Lex-SWAP-Stage2",
            "seed": cfg.seed,
            "max_iterations": cfg.max_iterations,
            "completed_iterations": completed_iterations,
            "improved_moves": improved_count,
            "swap_count": len(best.swaps),
            "swap_service_time_min": cfg.swap_service_time_min,
            "time_limit_seconds": cfg.time_limit_seconds,
            "initial_score": (
                evaluation.score.late_count,
                evaluation.score.distance_km,
            ),
        }
    )
    return SwapSolverResult(
        routes=best.routes,
        swaps=best.swaps,
        evaluation=best_evaluation,
        runtime_seconds=perf_counter() - started,
        iterations=completed_iterations,
        metadata=metadata,
    )


def solve_swap(
    problem: Problem,
    *,
    alns_config: ALNSConfig | None = None,
    swap_config: SwapConfig | None = None,
    time_limit_seconds: float | None = 240.0,
    swap_time_share: float = 58.0,
    safety_margin_seconds: float = 2.0,
) -> SwapSolverResult:
    """Two-stage C2-Lex-SWAP solver within one wall-clock budget.

    Stage 1 runs the stable same-carrier A2 (``solve_alns_core``) for
    ``time_limit_seconds - swap_time_share - safety_margin_seconds``.  Stage 2
    then optimises swaps for ``swap_time_share`` seconds while keeping the
    Stage-1 pickup ownership.  The returned solution is never worse than the
    Stage-1 baseline.
    """

    if time_limit_seconds is None:
        raise ValueError("Swap 求解需要总时间预算")
    if swap_time_share <= 0 or safety_margin_seconds < 0:
        raise ValueError("Swap 时间份额与安全余量设置非法")
    stage1_limit = time_limit_seconds - swap_time_share - safety_margin_seconds
    if stage1_limit <= 0:
        raise ValueError("Stage-1 A2 时间预算不足")

    stage1_config = replace(
        alns_config or ALNSConfig(),
        time_limit_seconds=stage1_limit,
    )
    stage1 = solve_alns_core(problem, config=stage1_config)

    stage2_config = replace(
        swap_config or SwapConfig(),
        time_limit_seconds=swap_time_share,
    )
    stage2 = solve_swap_stage2(problem, stage1.routes, stage2_config)

    total_runtime = stage1.runtime_seconds + stage2.runtime_seconds
    metadata = MappingProxyType(
        {
            "method": "C2-Lex-SWAP",
            "time_limit_seconds": time_limit_seconds,
            "swap_time_share_seconds": swap_time_share,
            "safety_margin_seconds": safety_margin_seconds,
            "swap_service_time_min": stage2_config.swap_service_time_min,
            "max_swaps": stage2_config.max_swaps,
            "stage1_runtime_seconds": stage1.runtime_seconds,
            "stage2_runtime_seconds": stage2.runtime_seconds,
            "swap_count": len(stage2.swaps),
            "stage1_metadata": MappingProxyType(dict(stage1.metadata)),
            "stage2_metadata": MappingProxyType(dict(stage2.metadata)),
        }
    )
    return SwapSolverResult(
        routes=stage2.routes,
        swaps=stage2.swaps,
        evaluation=stage2.evaluation,
        runtime_seconds=total_runtime,
        iterations=stage1.iterations + stage2.iterations,
        metadata=metadata,
    )
