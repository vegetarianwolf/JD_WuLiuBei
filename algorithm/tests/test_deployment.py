"""Tests for deployment planning, Point-based drone homes, first-flight
diagnostics, and the home-aware ALNS switches.
"""

from __future__ import annotations

import math

import pytest

from uav_dispatch import (
    ALNSConfig,
    Point,
    Problem,
    Task,
    construct_regret_initial,
    solve_alns_core,
)
from uav_dispatch.cli import build_drone_homes
from uav_dispatch.deploy import k_means_homes, k_means_points
from uav_dispatch.diagnostics import (
    deadhead_km,
    deadhead_vs_origin_km,
    first_flight_km,
    home_mismatch,
    min_deadhead_lb,
    origin_min_deadhead_lb,
)
from uav_dispatch.relay import build_relay_network


def _tiny_tasks() -> tuple[Task, ...]:
    return (
        Task(1, Point(1, 1), Point(2, 2), 10),
        Task(2, Point(5, 5), Point(6, 6), 20),
        Task(3, Point(1, 5), Point(2, 6), 30),
        Task(4, Point(5, 1), Point(6, 2), 40),
        Task(5, Point(3, 3), Point(4, 4), 50),
    )


def _tiny_problem(*, homes: tuple[Point, ...]) -> Problem:
    return Problem(
        _tiny_tasks(),
        drone_count=len(homes),
        max_tasks_per_drone=5,
        drone_homes=homes,
    )


# ---------------------------------------------------------------- k-means

def test_k_means_homes_is_deterministic_and_counts():
    tasks = _tiny_tasks()
    first = k_means_homes(tasks, 2, seed=42)
    second = k_means_homes(tasks, 2, seed=42)

    assert first == second
    assert len(first) == 2
    assert all(isinstance(point, Point) for point in first)


def test_k_means_homes_different_seed_gives_same_shape():
    tasks = _tiny_tasks()

    assert len(k_means_homes(tasks, 3, seed=1)) == 3
    assert len(k_means_homes(tasks, 3, seed=99)) == 3


def test_k_means_homes_rejects_invalid_k():
    tasks = _tiny_tasks()

    with pytest.raises(ValueError):
        k_means_homes(tasks, 0)
    with pytest.raises(ValueError):
        k_means_homes(tasks, 100)


# ------------------------------------------------------- Point-based homes

def test_problem_accepts_point_homes_and_registers_nodes():
    tasks = _tiny_tasks()
    homes = (Point(0, 0), Point(5, 5))
    problem = Problem(
        tasks, drone_count=2, max_tasks_per_drone=5, drone_homes=homes
    )

    assert problem.drone_home_nodes[0] != problem.drone_home_nodes[1]
    assert problem.home_points() == homes
    # home (5,5) -> pickup of task 2 (5,5) is a zero-length first leg.
    assert problem.distance(
        None, 2, from_node=problem.home_node(1)
    ) == pytest.approx(0.0)
    # Mixed homes keep the legacy semantics (None=origin, int=station).
    problem2 = Problem(
        tasks,
        drone_count=2,
        max_tasks_per_drone=5,
        drone_homes=(None, Point(0, 0)),
    )
    assert problem2.drone_home_nodes[0] == 0
    assert problem2.drone_home_nodes[1] != 0


def test_point_homes_share_node_for_identical_coordinates():
    tasks = _tiny_tasks()
    problem = Problem(
        tasks,
        drone_count=2,
        max_tasks_per_drone=5,
        drone_homes=(Point(1.5, 2.5), Point(1.5, 2.5)),
    )

    assert problem.drone_home_nodes[0] == problem.drone_home_nodes[1]


# ---------------------------------------------------- build_drone_homes

def test_build_drone_homes_modes():
    tasks = _tiny_tasks()
    network = build_relay_network(tasks, relay_count=2, location_seed=1)
    depot = Point(0.0, 0.0)

    assert build_drone_homes(2, network, "origin") is None
    homes = build_drone_homes(
        3, network, "stations", tasks=tasks, depot=depot
    )
    # Dynamic deployment: length always equals drone_count and every entry
    # is either the depot or an existing station (never a fixed one-per-
    # station rule, so no exact tuple is asserted).
    assert len(homes) == 3
    assert set(homes) <= {None, 1, 2}


def test_relay_stations_match_kmeans_points():
    tasks = _tiny_tasks()
    pickups = tuple(task.pickup for task in tasks)
    midpoints = tuple(
        Point(
            (task.pickup.x + task.delivery.x) / 2.0,
            (task.pickup.y + task.delivery.y) / 2.0,
        )
        for task in tasks
    )
    pickup_network = build_relay_network(
        tasks, relay_count=2, location_seed=1, location_method="kmeans_pickup"
    )
    midpoint_network = build_relay_network(
        tasks, relay_count=2, location_seed=1, location_method="kmeans_midpoint"
    )
    medoid_network = build_relay_network(
        tasks,
        relay_count=2,
        location_seed=1,
        location_method="weighted_kmedoids",
    )
    # k-means site methods return exactly the deployable cluster centres, so
    # the relay sites and the pre-deployed homes are the same points.
    assert tuple(s.point for s in pickup_network.stations) == k_means_points(
        pickups, 2, seed=1
    )
    assert tuple(s.point for s in midpoint_network.stations) == k_means_points(
        midpoints, 2, seed=1
    )
    # Deterministic across calls and the legacy default is unchanged.
    again = build_relay_network(
        tasks, relay_count=2, location_seed=1, location_method="kmeans_pickup"
    )
    assert again.stations == pickup_network.stations
    assert medoid_network.metadata["location_method"] == "weighted_kmedoids"
    assert pickup_network.metadata["location_method"] == "kmeans_pickup"
    with pytest.raises(ValueError):
        build_relay_network(tasks, relay_count=2, location_method="unknown")


# --------------------------------------------------------- diagnostics

def test_diagnostics_deadhead_and_utilization():
    problem = _tiny_problem(homes=(Point(0, 0), Point(5, 5)))
    routes = (
        (1, -1),
        (2, -2, 3, -3, 4, -4, 5, -5),
    )

    assert first_flight_km(problem, routes) == pytest.approx(
        [math.sqrt(2), 0.0]
    )
    assert deadhead_km(problem, routes) == pytest.approx(math.sqrt(2))
    # All-origin first legs: (0,0)->task1 (sqrt2) and (0,0)->task2 (5sqrt2).
    assert deadhead_vs_origin_km(problem, routes) == pytest.approx(
        5 * math.sqrt(2)
    )
    # Every task sits on the drone whose home is nearest its pickup.
    matched, mismatched, gap = home_mismatch(problem, routes)
    assert (matched, mismatched) == (5, 0)
    assert gap == pytest.approx(0.0)
    # LB: drone at (0,0) -> nearest pickup (1,1); drone at (5,5) -> itself.
    assert min_deadhead_lb(problem) == pytest.approx(math.sqrt(2))
    assert origin_min_deadhead_lb(problem) == pytest.approx(2 * math.sqrt(2))


def test_diagnostics_mismatch_detects_bad_assignment():
    problem = _tiny_problem(homes=(Point(0, 0), Point(5, 5)))
    # Task 1's pickup (1,1) is nearest home (0,0), but sits on drone 1.
    routes = ((), (1, -1, 2, -2, 3, -3, 4, -4, 5, -5))

    matched, mismatched, gap = home_mismatch(problem, routes)
    assert matched == 4
    assert mismatched == 1
    assert gap == pytest.approx(math.sqrt(32) - math.sqrt(2))


# --------------------------------------------------- home-aware switches

def test_home_seed_construction_is_valid_and_complete():
    problem = _tiny_problem(homes=(Point(0, 0), Point(5, 5)))
    result = construct_regret_initial(problem, candidate_limit=8, home_seed=True)

    assert result.evaluation.valid
    served = {
        visit
        for route in result.routes
        for visit in route
        if visit > 0
    }
    assert served == set(problem.task_ids)


def test_home_displaced_operator_gating():
    problem = _tiny_problem(homes=(Point(0, 0), Point(5, 5)))
    cfg_off = ALNSConfig(max_iterations=5, seed=1, candidate_limit=8)
    result_off = solve_alns_core(problem, config=cfg_off)
    assert result_off.evaluation.valid
    assert "destroy:home_displaced" not in result_off.metadata[
        "operator_weights"
    ]

    cfg_on = ALNSConfig(
        max_iterations=5,
        seed=1,
        candidate_limit=8,
        enable_home_displaced=True,
    )
    result_on = solve_alns_core(problem, config=cfg_on)
    assert result_on.evaluation.valid
    assert "destroy:home_displaced" in result_on.metadata["operator_weights"]
