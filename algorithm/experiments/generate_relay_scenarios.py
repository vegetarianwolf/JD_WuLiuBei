"""Generate deterministic synthetic campus instances for relay experiments.

Coordinates are expressed directly in kilometres.  The solver therefore uses
the Euclidean coordinate metric without independently clipping individual
arcs, which preserves symmetry and the triangle inequality.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from uav_dispatch import Point, Problem, Task
from uav_dispatch.relay import Hub


SCENARIO_CODES = ("U", "G", "MZ", "MIX")
_SCENARIO_SALTS = {"U": 101, "G": 211, "MZ": 307, "MIX": 401}


@dataclass(frozen=True, slots=True)
class SyntheticScenario:
    """A reproducible task set plus pre-declared candidate relay hubs."""

    code: str
    seed: int
    tasks: tuple[Task, ...]
    candidate_hubs: tuple[Hub, ...]
    depot: Point = Point(0.0, 0.0)

    def to_problem(
        self,
        *,
        drone_count: int | None = None,
        max_tasks_per_drone: int = 25,
    ) -> Problem:
        if drone_count is None:
            drone_count = max(2, math.ceil(len(self.tasks) / max_tasks_per_drone))
        return Problem(
            self.tasks,
            drone_count=drone_count,
            max_tasks_per_drone=max_tasks_per_drone,
            capacity=2,
            speed_km_per_min=0.9,
            depot=self.depot,
        )


def _jitter(
    rng: random.Random,
    centre: tuple[float, float],
    radius: tuple[float, float] = (1.1, 1.1),
) -> Point:
    return Point(
        centre[0] + rng.uniform(-radius[0], radius[0]),
        centre[1] + rng.uniform(-radius[1], radius[1]),
    )


def _candidate_hubs(code: str) -> tuple[Hub, ...]:
    if code == "U":
        points = (
            (-6.0, -4.0),
            (-6.0, 4.0),
            (-2.0, 0.0),
            (2.0, 0.0),
            (6.0, -4.0),
            (6.0, 4.0),
            (0.0, -5.5),
            (0.0, 5.5),
        )
    elif code == "G":
        points = tuple((0.0, y) for y in (-7.0, -5.0, -3.0, -1.0, 1.0, 3.0, 5.0, 7.0))
    else:
        points = (
            (-6.0, 0.0),
            (-2.0, 0.0),
            (2.0, 0.0),
            (6.0, 0.0),
            (0.0, -6.0),
            (0.0, -2.0),
            (0.0, 2.0),
            (0.0, 6.0),
        )
    return tuple(
        Hub(f"{code.lower()}-hub-{index}", Point(*point))
        for index, point in enumerate(points, 1)
    )


def _points_for_task(
    code: str, rng: random.Random, index: int
) -> tuple[Point, Point]:
    if code == "U":
        return (
            Point(rng.uniform(-12.0, 12.0), rng.uniform(-9.0, 9.0)),
            Point(rng.uniform(-12.0, 12.0), rng.uniform(-9.0, 9.0)),
        )
    if code == "G":
        return (
            Point(rng.uniform(-12.0, -7.0), rng.uniform(-8.0, 8.0)),
            Point(rng.uniform(5.0, 12.0), rng.uniform(-7.0, 7.0)),
        )

    clusters = (
        (-10.0, -6.0),
        (-10.0, 6.0),
        (-3.0, -7.0),
        (3.0, 7.0),
        (10.0, -6.0),
        (10.0, 6.0),
    )
    pickup_index = (index + rng.randrange(len(clusters))) % len(clusters)
    if code == "MZ":
        delivery_index = (pickup_index + 3 + rng.randrange(2)) % len(clusters)
    elif rng.random() < 0.55:
        delivery_index = pickup_index
    else:
        delivery_index = (pickup_index + 3 + rng.randrange(2)) % len(clusters)
    return _jitter(rng, clusters[pickup_index]), _jitter(rng, clusters[delivery_index])


def generate_scenario(code: str, *, task_count: int, seed: int) -> SyntheticScenario:
    """Return one of U/G/MZ/MIX using only the supplied code, size and seed."""

    normalized = code.upper()
    if normalized not in SCENARIO_CODES:
        raise ValueError(f"未知 synthetic 场景: {code!r}")
    if task_count <= 0:
        raise ValueError("任务数必须为正整数")
    rng = random.Random((int(seed) << 12) ^ _SCENARIO_SALTS[normalized])
    depot = Point(0.0, 0.0)
    tasks: list[Task] = []
    for index in range(task_count):
        pickup, delivery = _points_for_task(normalized, rng, index)
        direct_time = (
            depot.distance_to(pickup) + pickup.distance_to(delivery)
        ) / 0.9
        slack = {
            "U": rng.uniform(8.0, 22.0),
            "G": rng.uniform(6.0, 18.0),
            "MZ": rng.uniform(7.0, 20.0),
            "MIX": rng.uniform(8.0, 22.0),
        }[normalized]
        tasks.append(
            Task(
                index + 1,
                pickup,
                delivery,
                deadline_min=direct_time + slack + (index % 5) * 1.5,
            )
        )
    return SyntheticScenario(
        code=normalized,
        seed=int(seed),
        tasks=tuple(tasks),
        candidate_hubs=_candidate_hubs(normalized),
        depot=depot,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=SCENARIO_CODES,
        default=list(SCENARIO_CODES),
    )
    parser.add_argument("--tasks", nargs="+", type=int, default=[25, 50, 100, 200])
    parser.add_argument(
        "--seeds",
        nargs="+",
        type=int,
        default=list(range(2026081000, 2026081010)),
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if any(count <= 0 for count in args.tasks):
        raise ValueError("任务数必须为正整数")
    for code in args.scenarios:
        for task_count in args.tasks:
            for seed in args.seeds:
                scenario = generate_scenario(code, task_count=task_count, seed=seed)
                directory = args.output_dir / code / f"n{task_count}" / f"seed_{seed}"
                directory.mkdir(parents=True, exist_ok=True)
                with (directory / "tasks.csv").open(
                    "w", encoding="utf-8", newline=""
                ) as handle:
                    writer = csv.writer(handle)
                    writer.writerow(
                        (
                            "task_id",
                            "pickup_x",
                            "pickup_y",
                            "delivery_x",
                            "delivery_y",
                            "deadline_min",
                        )
                    )
                    writer.writerows(
                        (
                            task.id,
                            task.pickup.x,
                            task.pickup.y,
                            task.delivery.x,
                            task.delivery.y,
                            task.deadline_min,
                        )
                        for task in scenario.tasks
                    )
                metadata = {
                    "scenario": code,
                    "task_count": task_count,
                    "seed": seed,
                    "distance_metric": "euclidean_coordinate_km_unclipped",
                    "depot": [scenario.depot.x, scenario.depot.y],
                    "candidate_hubs": [
                        {"id": hub.id, "x": hub.point.x, "y": hub.point.y}
                        for hub in scenario.candidate_hubs
                    ],
                }
                (directory / "scenario.json").write_text(
                    json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
