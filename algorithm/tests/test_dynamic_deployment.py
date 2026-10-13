"""Tests for the demand-driven dynamic UAV home deployment.

Covers the required guarantees: no fixed one-per-station rule, balanced
allocation under uniform demand, zero-UAV stations allowed, the integer
allocation always summing to the drone count, the empty-demand fallback to
the depot, the homes-list length invariant, and that a station with 0 UAVs
still works as a relay handoff point.
"""

from __future__ import annotations

from random import Random

import pytest

from uav_dispatch import Problem, Task, evaluate_solution
from uav_dispatch.deployment import (
    DemandConfig,
    DeploymentPlan,
    compute_dynamic_uav_homes,
    deployment_distribution,
)
from uav_dispatch.model import Point, RelayStation
from uav_dispatch.relay import (
    build_plan_index,
    build_relay_network,
    compute_dynamic_station_count,
)


def _node_allocation(
    plan: DeploymentPlan,
) -> dict[int | None, int]:
    return {node.home: node.allocated_uavs for node in plan.nodes}


def _clustered_tasks() -> tuple[Task, ...]:
    """Six tasks tightly clustered near (10,10) plus two near the depot."""
    return (
        Task(1, Point(10, 10), Point(11, 11), 30),
        Task(2, Point(10.5, 10.5), Point(11.5, 11.5), 35),
        Task(3, Point(9.5, 10.2), Point(10.5, 11.2), 40),
        Task(4, Point(10.2, 9.8), Point(11.2, 10.8), 32),
        Task(5, Point(9.8, 10.8), Point(10.8, 11.8), 38),
        Task(6, Point(10.7, 10.3), Point(11.7, 11.3), 42),
        Task(7, Point(1, 1), Point(2, 2), 60),
        Task(8, Point(1.5, 1.5), Point(2.5, 2.5), 65),
    )


# ------------------------------------------------- Test 1: concentrated demand

def test_concentrated_demand_stacks_uavs_not_one_per_station():
    tasks = _clustered_tasks()
    stations = (RelayStation(1, 10, 10), RelayStation(2, 20, 20))
    plan = compute_dynamic_uav_homes(tasks, stations, 8, Point(0, 0))

    allocation = _node_allocation(plan)
    assert sum(allocation.values()) == 8
    # The hot station must hold more than one UAV...
    assert allocation[1] >= 2
    # ...and the deployment is visibly NOT one-per-station.
    assert max(allocation.values()) >= 2
    assert allocation[2] != allocation[1] or allocation[None] >= 2


# --------------------------------------------------- Test 2: uniform demand

def test_uniform_demand_is_balanced():
    tasks = tuple(
        Task(index, Point(x, y), Point(x + 1, y + 1), 100)
        for index, (x, y) in enumerate(
            [
                (0, 0),
                (10, 0),
                (0, 10),
                (10, 10),
                (20, 0),
                (0, 20),
                (20, 20),
                (10, 10),
                (5, 5),
                (15, 15),
            ],
            start=1,
        )
    )
    stations = (
        RelayStation(1, 10, 0),
        RelayStation(2, 0, 10),
        RelayStation(3, 10, 10),
    )
    plan = compute_dynamic_uav_homes(tasks, stations, 8, Point(0, 0))

    counts = [node.allocated_uavs for node in plan.nodes]
    assert sum(counts) == 8
    assert max(counts) - min(counts) <= 2


# ------------------------------------------------- Test 3: empty station -> 0

def test_far_station_may_get_zero_uavs():
    tasks = (
        Task(1, Point(1, 1), Point(2, 2), 500),
        Task(2, Point(2, 2), Point(3, 3), 500),
        Task(3, Point(1, 3), Point(2, 4), 500),
        Task(4, Point(3, 1), Point(4, 2), 500),
    )
    stations = (
        RelayStation(1, 100, 100),  # far from every pickup
        RelayStation(2, 5, 5),
        RelayStation(3, -5, 5),
    )
    plan = compute_dynamic_uav_homes(tasks, stations, 8, Point(0, 0))

    allocation = _node_allocation(plan)
    assert allocation[1] == 0  # allowed: a station may hold zero UAVs
    assert sum(allocation.values()) == 8


# --------------------------------------------------- Test 4: sum == drone_count

def test_allocation_always_sums_to_drone_count():
    rng = Random(7)
    for _ in range(15):
        tasks = tuple(
            Task(
                index,
                Point(rng.uniform(0, 50), rng.uniform(0, 50)),
                Point(rng.uniform(0, 50), rng.uniform(0, 50)),
                rng.uniform(20, 200),
            )
            for index in range(1, 21)
        )
        stations = tuple(
            RelayStation(
                station_index + 1,
                rng.uniform(0, 50),
                rng.uniform(0, 50),
            )
            for station_index in range(4)
        )
        for drone_count in (3, 8, 12):
            plan = compute_dynamic_uav_homes(
                tasks, stations, drone_count, Point(0, 0)
            )
            assert len(plan.homes) == drone_count
            assert sum(
                node.allocated_uavs for node in plan.nodes
            ) == drone_count


# ---------------------------------------------- Test 5: fallback to the depot

def test_fallback_to_depot_when_no_demand():
    plan = compute_dynamic_uav_homes((), (), 5, Point(0, 0))
    assert plan.homes == (None,) * 5

    stations = (RelayStation(1, 5, 5),)
    plan2 = compute_dynamic_uav_homes((), stations, 5, Point(0, 0))
    assert plan2.homes == (None,) * 5


# ---------------------------------------------- Test 6: homes length invariant

def test_homes_length_equals_drone_count():
    tasks = _clustered_tasks()
    stations = (RelayStation(1, 10, 10), RelayStation(2, 20, 20))

    for drone_count in (1, 2, 8, 15):
        plan = compute_dynamic_uav_homes(
            tasks, stations, drone_count, Point(0, 0)
        )
        assert len(plan.homes) == drone_count
        assert deployment_distribution(plan.homes)  # non-empty


# --------------------------------------- Test 7: zero-UAV station relay usage

def test_zero_uav_station_remains_relay_usable():
    tasks = (
        Task(1, Point(1, 1), Point(8, 8), 120),
        Task(2, Point(2, 2), Point(9, 9), 120),
    )
    network = build_relay_network(tasks, relay_count=2, location_seed=1)
    # Deploy only at the depot and station 1: station 2 holds zero UAVs.
    problem = Problem(
        tasks,
        drone_count=2,
        max_tasks_per_drone=5,
        relay_stations=network.stations,
        leg_registry=network.leg_registry,
        task_relay_candidates=network.task_relay_candidates,
        drone_homes=(None, 1),
    )
    # Station 2 must still be a fully usable relay handoff point.
    in_leg = next(
        leg
        for leg in network.leg_registry.values()
        if leg.kind == "RELAY_IN" and leg.relay_id == 2
    )
    out_leg = next(
        leg
        for leg in network.leg_registry.values()
        if leg.kind == "RELAY_OUT"
        and leg.relay_id == 2
        and leg.task_id == in_leg.task_id
    )
    routes = (
        (in_leg.id, -in_leg.id, 2, -2),
        (out_leg.id, -out_leg.id),
    )
    evaluation = evaluate_solution(problem, routes)
    assert evaluation.valid
    plan_index = build_plan_index(problem, routes)
    task_plan = plan_index.task_plans[in_leg.task_id]
    assert task_plan.mode == "RELAY"
    assert task_plan.relay_id == 2


# ---------------------------------------------------------- misc invariants

def test_dynamic_plan_is_deterministic():
    tasks = _clustered_tasks()
    stations = (RelayStation(1, 10, 10), RelayStation(2, 20, 20))
    first = compute_dynamic_uav_homes(tasks, stations, 8, Point(0, 0))
    second = compute_dynamic_uav_homes(tasks, stations, 8, Point(0, 0))

    assert first.homes == second.homes
    assert first.nodes == second.nodes


def test_fixed_allocation_must_sum_to_drone_count():
    tasks = _clustered_tasks()
    stations = (RelayStation(1, 10, 10), RelayStation(2, 20, 20))

    with pytest.raises(ValueError):
        compute_dynamic_uav_homes(
            tasks, stations, 8, Point(0, 0), fixed_allocation=(1, 1, 1)
        )
    with pytest.raises(ValueError):
        compute_dynamic_uav_homes(
            tasks, stations, 8, Point(0, 0), fixed_allocation=(3, 3, 3)
        )


def test_cap_can_be_disabled():
    tasks = _clustered_tasks()
    stations = (RelayStation(1, 10, 10),)
    config = DemandConfig(cap_enabled=False)
    plan = compute_dynamic_uav_homes(
        tasks, stations, 8, Point(0, 0), config=config
    )
    assert sum(node.allocated_uavs for node in plan.nodes) == 8
    assert len(plan.homes) == 8


# --------------------------------------------- dynamic station count

def test_dynamic_station_count_concentrated():
    tasks = tuple(
        Task(
            index,
            Point(5 + (index % 3) * 0.2, 5 + (index // 3) * 0.2),
            Point(5.5, 5.5),
            60,
        )
        for index in range(1, 9)
    )
    # A tight cluster needs only one station.
    assert compute_dynamic_station_count(tasks, 8) == 1


def test_dynamic_station_count_spread_needs_more_stations():
    spread = tuple(
        Task(
            index,
            Point((index % 8) * 15, (index // 8) * 15),
            Point((index % 8) * 15 + 1, (index // 8) * 15 + 1),
            100,
        )
        for index in range(1, 41)
    )
    concentrated = tuple(
        Task(index, Point(5, 5), Point(5.5, 5.5), 60)
        for index in range(1, 9)
    )
    assert compute_dynamic_station_count(spread, 8) >= 4
    assert compute_dynamic_station_count(spread, 8) > compute_dynamic_station_count(
        concentrated, 8
    )


def test_dynamic_station_count_bounds_and_determinism():
    spread = tuple(
        Task(
            index,
            Point((index % 8) * 15, (index // 8) * 15),
            Point((index % 8) * 15 + 1, (index // 8) * 15 + 1),
            100,
        )
        for index in range(1, 41)
    )
    first = compute_dynamic_station_count(spread, 8)
    assert first == compute_dynamic_station_count(spread, 8)
    assert 1 <= first <= 8
    assert compute_dynamic_station_count(spread, 8, max_stations=2) <= 2
    # Empty task pool falls back to the minimum.
    assert compute_dynamic_station_count((), 8) == 1
    assert compute_dynamic_station_count((), 8, min_stations=2) == 2
    with pytest.raises(ValueError):
        compute_dynamic_station_count(spread, 8, coverage_target_km=0)


def test_dynamic_station_count_feeds_relay_network():
    tasks = _clustered_tasks()
    count = compute_dynamic_station_count(tasks, 8)
    network = build_relay_network(
        tasks, relay_count=count, location_seed=1
    )
    assert len(network.stations) == count
    assert all(
        task_id in network.task_relay_candidates
        for task_id in (task.id for task in tasks)
    )
