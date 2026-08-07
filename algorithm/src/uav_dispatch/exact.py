"""Small-instance Pareto-label dynamic programming oracle."""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from .model import Problem, Score
from .search import SolverResult
from .validation import evaluate_solution


@dataclass(frozen=True, slots=True)
class _Label:
    score: Score
    route: tuple[int, ...]


def _dominates(left: Score, right: Score) -> bool:
    # Distance is also the state clock.  Allowing an epsilon-longer label to
    # dominate can cross a future deadline boundary, so dominance must be
    # conservative and use the exact stored values.
    no_worse = (
        left.late_count <= right.late_count
        and left.total_lateness_min <= right.total_lateness_min
        and left.distance_km <= right.distance_km
    )
    strictly_better = (
        left.late_count < right.late_count
        or left.total_lateness_min < right.total_lateness_min
        or left.distance_km < right.distance_km
    )
    return no_worse and strictly_better


def _add_label(frontier: list[_Label], candidate: _Label) -> None:
    for existing in frontier:
        if _dominates(existing.score, candidate.score):
            return
        if existing.score == candidate.score and existing.route <= candidate.route:
            return
    frontier[:] = [
        existing
        for existing in frontier
        if not _dominates(candidate.score, existing.score)
        and not (candidate.score == existing.score and candidate.route < existing.route)
    ]
    frontier.append(candidate)


def solve_exact(problem: Problem, max_tasks: int = 10) -> SolverResult:
    """Solve a small single-drone instance exactly with non-dominated labels."""

    if problem.drone_count != 1:
        raise ValueError("精确动态规划当前只支持单架无人机")
    task_count = len(problem.tasks)
    if task_count > max_tasks:
        raise ValueError(f"精确求解最多支持 {max_tasks} 个任务")
    if task_count > problem.max_tasks_per_drone:
        raise ValueError("单机任务上限不足")

    started = perf_counter()
    task_ids = tuple(task.id for task in problem.tasks)
    full_mask = (1 << task_count) - 1
    # A state is (picked_mask, delivered_mask, last_visit).  Keeping a Pareto
    # frontier is essential: the lexicographically best prefix alone can be
    # slower and consequently make more future deliveries late.
    layer: dict[tuple[int, int, int | None], list[_Label]] = {
        (0, 0, None): [_Label(Score(0, 0.0, 0.0), ())]
    }

    for _ in range(2 * task_count):
        next_layer: dict[tuple[int, int, int | None], list[_Label]] = {}
        for (picked_mask, delivered_mask, last_visit), labels in layer.items():
            onboard = (picked_mask & ~delivered_mask).bit_count()
            actions: list[tuple[int, int, int]] = []
            if onboard < problem.capacity:
                for index, task_id in enumerate(task_ids):
                    bit = 1 << index
                    if not picked_mask & bit:
                        actions.append((task_id, picked_mask | bit, delivered_mask))
            for index, task_id in enumerate(task_ids):
                bit = 1 << index
                if picked_mask & bit and not delivered_mask & bit:
                    actions.append((-task_id, picked_mask, delivered_mask | bit))

            for visit, new_picked, new_delivered in actions:
                state = (new_picked, new_delivered, visit)
                frontier = next_layer.setdefault(state, [])
                for label in labels:
                    leg = problem.distance(last_visit, visit)
                    new_distance = label.score.distance_km + leg
                    late_count = label.score.late_count
                    total_lateness = label.score.total_lateness_min
                    if visit < 0:
                        arrival = new_distance / problem.speed_km_per_min
                        lateness = max(
                            0.0, arrival - problem.task(-visit).deadline_min
                        )
                        if lateness > 1e-9:
                            late_count += 1
                            total_lateness += lateness
                    _add_label(
                        frontier,
                        _Label(
                            Score(late_count, total_lateness, new_distance),
                            label.route + (visit,),
                        ),
                    )
        layer = next_layer

    finals = [
        label
        for (picked_mask, delivered_mask, _), labels in layer.items()
        if picked_mask == full_mask and delivered_mask == full_mask
        for label in labels
    ]
    if not finals:
        raise RuntimeError("精确求解器未找到完整路线")
    best = min(finals, key=lambda label: (label.score, label.route))
    routes = (best.route,)
    evaluation = evaluate_solution(problem, routes)
    if not evaluation.valid:
        raise RuntimeError(f"精确求解结果非法: {evaluation.violations}")
    return SolverResult(
        routes=routes,
        evaluation=evaluation,
        runtime_seconds=perf_counter() - started,
        iterations=2 * task_count,
    )
