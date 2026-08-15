"""Deterministic k-means clustering for deployment and relay planning.

``k_means_points`` clusters an arbitrary point set with k-means++ seeding
plus Lloyd iterations under a fixed RNG, so a given (points, k, seed) always
returns the same cluster centres.  ``k_means_homes`` is a thin wrapper that
clusters the task pickups.  Relay station placement (``build_relay_network``)
calls the same routine, so the relay sites and the pre-deployed drone homes
can be the very same points.  Pure standard library (``pyproject.toml``
declares no third-party dependencies).
"""

from __future__ import annotations

from math import isfinite
from random import Random
from typing import Iterable, Sequence

from .model import Point, Task


def _k_means_plus_plus(
    points: Sequence[Point],
    k: int,
    rng: Random,
) -> list[Point]:
    """k-means++ initial centres with D-squared weighted seeding."""

    centres: list[Point] = [points[rng.randrange(len(points))]]
    while len(centres) < k:
        squared = [
            min(
                (point.x - centre.x) ** 2 + (point.y - centre.y) ** 2
                for centre in centres
            )
            for point in points
        ]
        total = sum(squared)
        if total <= 0.0:
            # All points already coincide with a centre; pick any unseen point.
            remaining = [
                point for point in points if point not in centres
            ]
            if not remaining:
                raise ValueError("任务点过少，无法生成 k 个互异簇心")
            centres.append(remaining[rng.randrange(len(remaining))])
            continue
        target = rng.random() * total
        cumulative = 0.0
        chosen = 0
        for index, weight in enumerate(squared):
            cumulative += weight
            if cumulative >= target:
                chosen = index
                break
        centres.append(points[chosen])
    return centres


def k_means_points(
    points: Sequence[Point],
    k: int,
    *,
    seed: int = 42,
    iterations: int = 25,
) -> tuple[Point, ...]:
    """Cluster ``points`` with deterministic k-means++ and return the centres.

    The returned centres are the final cluster means after Lloyd iteration,
    so they are arbitrary coordinates.  Deterministic for a fixed ``seed``.
    Used both for UAV home deployment and for relay station placement.

    Raises ``ValueError`` when ``k`` is non-positive or exceeds the number of
    distinct points.
    """

    if k <= 0:
        raise ValueError("聚类数必须为正整数")
    if iterations < 1:
        raise ValueError("k-means 迭代次数必须为正整数")
    if not points:
        raise ValueError("点集为空，无法聚类")
    if k > len(points):
        raise ValueError(f"聚类数 {k} 不能超过点数 {len(points)}")
    rng = Random(seed)
    centres = _k_means_plus_plus(points, k, rng)
    assignments = [0] * len(points)

    def nearest_index(point: Point) -> int:
        return min(
            range(k),
            key=lambda index: (
                (point.x - centres[index].x) ** 2
                + (point.y - centres[index].y) ** 2
            ),
        )

    for _ in range(iterations):
        changed = False
        for index, point in enumerate(points):
            nearest = nearest_index(point)
            if nearest != assignments[index]:
                assignments[index] = nearest
                changed = True
        if not changed:
            break
        new_centres: list[Point | None] = [None] * k
        sums_x = [0.0] * k
        sums_y = [0.0] * k
        counts = [0] * k
        for index, point in enumerate(points):
            cluster = assignments[index]
            sums_x[cluster] += point.x
            sums_y[cluster] += point.y
            counts[cluster] += 1
        for cluster in range(k):
            if counts[cluster] > 0:
                new_centres[cluster] = Point(
                    sums_x[cluster] / counts[cluster],
                    sums_y[cluster] / counts[cluster],
                )
        # Re-seed any empty cluster from the point farthest from its centre.
        for cluster in range(k):
            if new_centres[cluster] is None:
                fallback = max(
                    points,
                    key=lambda point: (
                        (point.x - centres[cluster].x) ** 2
                        + (point.y - centres[cluster].y) ** 2
                    ),
                )
                new_centres[cluster] = fallback
        centres = [centre for centre in new_centres if centre is not None]
    homes = tuple(centres)
    for home in homes:
        if not isfinite(home.x) or not isfinite(home.y):
            raise RuntimeError("k-means 簇心产生非有限坐标")
    return homes


def k_means_homes(
    tasks: Iterable[Task],
    k: int,
    *,
    seed: int = 42,
    iterations: int = 25,
) -> tuple[Point, ...]:
    """Place ``k`` drone homes on the task pickup clusters.

    Thin wrapper over :func:`k_means_points`; relay station placement calls
    the same routine so deployment and relay share the same points.
    """

    return k_means_points(
        tuple(task.pickup for task in tasks),
        k,
        seed=seed,
        iterations=iterations,
    )
