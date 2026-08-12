"""Best-known direct solution discovery and pickup-timing diagnosis.

The relay design work needs a trustworthy reference: the best *direct*
(same-drone) solution ever saved in this repository, re-evaluated from its
saved routes under the *current* two-layer objective ``(late_count,
distance_km)``.  Historical ``score``/``objective_order`` fields in old JSON
files are never trusted for ranking; every candidate route is reloaded and
re-scored with the independent validator.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .model import Point, Problem
from .search import Routes
from .validation import evaluate_solution


@dataclass(frozen=True, slots=True)
class BestKnownSolution:
    """A complete direct solution re-scored under the current objective."""

    routes: Routes
    source_path: Path
    late_count: int
    distance_km: float
    total_lateness_min: float


@dataclass(frozen=True, slots=True)
class PickupTimingRecord:
    """One task's pickup/delivery timing in a complete solution."""

    task_id: int
    pickup_time_min: float
    delivery_time_min: float
    deadline_min: float
    pickup_late: bool
    delivery_late: bool
    drone: int
    pickup_position: int


def load_routes_from_payload(payload: object) -> Routes | None:
    """Extract encoded routes from a solution JSON payload, if present."""

    if not isinstance(payload, dict):
        return None
    routes = payload.get("routes")
    if not isinstance(routes, list):
        return None
    try:
        return tuple(
            tuple(int(visit) for visit in route["encoded_visits"])
            for route in routes
            if isinstance(route, dict) and "encoded_visits" in route
        )
    except (KeyError, TypeError, ValueError):
        return None


def payload_compatible(payload: object, problem: Problem) -> bool:
    """Return True when a saved payload targets this problem's parameters.

    The formal reference instance is N=200, M=8, K=25, Q=2, speed=0.9 km/min,
    depot=(0,0), open route.  Payloads without a ``problem`` block are
    accepted optimistically (older files) and validated by re-evaluation.
    """

    if not isinstance(payload, dict):
        return False
    block = payload.get("problem")
    if not isinstance(block, dict):
        return True
    if int(block.get("task_count", problem.task_ids[-1] if problem.task_ids else 0)) != len(problem.tasks):
        return False
    if int(block.get("drone_count", problem.drone_count)) != problem.drone_count:
        return False
    if (
        int(block.get("max_tasks_per_drone", problem.max_tasks_per_drone))
        != problem.max_tasks_per_drone
    ):
        return False
    if int(block.get("capacity", problem.capacity)) != problem.capacity:
        return False
    if (
        abs(float(block.get("speed_km_per_min", problem.speed_km_per_min)) - problem.speed_km_per_min)
        > 1e-12
    ):
        return False
    depot_km = block.get("depot_km")
    if isinstance(depot_km, (list, tuple)) and len(depot_km) == 2:
        if (
            abs(float(depot_km[0]) - problem.depot.x) > 1e-12
            or abs(float(depot_km[1]) - problem.depot.y) > 1e-12
        ):
            return False
    return True


def _iter_solution_files(results_root: Path) -> Iterable[Path]:
    """Yield every JSON file that plausibly holds complete routes."""

    run_solutions = results_root / "run_solutions"
    candidates: list[Path] = []
    if run_solutions.is_dir():
        candidates.extend(sorted(run_solutions.glob("*.json")))
    # Best-known / best-* files and any other route-bearing JSON at any depth.
    candidates.extend(sorted(results_root.glob("**/*.json")))
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        yield path


def find_best_known_direct_solution(
    problem: Problem,
    *,
    results_root: Path | None = None,
) -> BestKnownSolution | None:
    """Scan saved route JSONs and return the best re-scored direct solution.

    Ranking uses only ``(late_count, distance_km)`` re-computed by the
    independent validator; ``total_lateness`` never participates.
    """

    root = results_root or (Path(__file__).resolve().parents[2] / "results")
    best: BestKnownSolution | None = None
    for path in _iter_solution_files(root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not payload_compatible(payload, problem):
            continue
        routes = load_routes_from_payload(payload)
        if routes is None or not routes:
            continue
        evaluation = evaluate_solution(problem, routes)
        if not evaluation.valid:
            continue
        candidate = BestKnownSolution(
            routes=routes,
            source_path=path,
            late_count=evaluation.score.late_count,
            distance_km=evaluation.score.distance_km,
            total_lateness_min=evaluation.total_lateness_min,
        )
        if best is None or (
            candidate.late_count,
            candidate.distance_km,
        ) < (best.late_count, best.distance_km):
            best = candidate
    return best


def pickup_timing_diagnosis(
    problem: Problem,
    routes: Sequence[Sequence[int]],
) -> tuple[tuple[PickupTimingRecord, ...], dict[str, int]]:
    """Compute per-task pickup/delivery timing for a complete solution.

    Returns ``(records, summary)`` where summary counts late deliveries,
    pickup-late tasks and pickup-on-time-but-delivery-late tasks.
    """

    by_task: dict[int, PickupTimingRecord] = {}
    for drone_index, route in enumerate(routes, start=1):
        elapsed = 0.0
        previous: int | None = None
        for position, visit in enumerate(route, start=1):
            task_id = abs(visit)
            elapsed += (
                problem.distance(previous, visit) / problem.speed_km_per_min
            )
            previous = visit
            task = problem.task(task_id)
            if visit > 0:
                by_task[task_id] = PickupTimingRecord(
                    task_id=task_id,
                    pickup_time_min=elapsed,
                    delivery_time_min=float("nan"),
                    deadline_min=task.deadline_min,
                    pickup_late=elapsed > task.deadline_min + 1e-9,
                    delivery_late=False,
                    drone=drone_index,
                    pickup_position=position - 1,
                )
            else:
                record = by_task.get(task_id)
                if record is None:
                    continue
                by_task[task_id] = PickupTimingRecord(
                    task_id=task_id,
                    pickup_time_min=record.pickup_time_min,
                    delivery_time_min=elapsed,
                    deadline_min=record.deadline_min,
                    pickup_late=record.pickup_late,
                    delivery_late=elapsed > record.deadline_min + 1e-9,
                    drone=drone_index,
                    pickup_position=record.pickup_position,
                )

    records = tuple(by_task[task_id] for task_id in sorted(by_task))
    late_deliveries = sum(record.delivery_late for record in records)
    pickup_late = sum(record.pickup_late for record in records)
    pickup_on_time_delivery_late = sum(
        (not record.pickup_late) and record.delivery_late for record in records
    )
    summary = {
        "task_count": len(records),
        "late_deliveries": late_deliveries,
        "pickup_late_count": pickup_late,
        "pickup_on_time_delivery_late_count": pickup_on_time_delivery_late,
    }
    return records, summary
