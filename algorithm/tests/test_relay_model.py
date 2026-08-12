"""Tests for the Relay explicit data structures."""

from __future__ import annotations

import pytest

from uav_dispatch.model import Point, Problem, Task
from uav_dispatch.relay_model import (
    RelaySolution,
    RelayStation,
    RelayTransfer,
    gaps_for_route,
    relay_solution_from_routes,
)


def _station(relay_id: int = 0, source_visit: int = -2) -> RelayStation:
    return RelayStation(relay_id, Point(0, 2), source_visit)


def test_relay_station_rejects_negative_id_and_depot_source():
    with pytest.raises(ValueError, match="不能为负"):
        RelayStation(-1, Point(0, 0), 1)
    with pytest.raises(ValueError, match="任务服务节点"):
        RelayStation(0, Point(0, 0), 0)


def test_relay_transfer_requires_two_distinct_drones():
    with pytest.raises(ValueError, match="不同无人机"):
        RelayTransfer(0, 1, 0, 0, 0, 1, 2)


def test_relay_transfer_rejects_invalid_positions_and_task():
    with pytest.raises(ValueError, match="正整数"):
        RelayTransfer(0, -1, 0, 0, 1, 1, 2)
    with pytest.raises(ValueError, match="不能为负"):
        RelayTransfer(0, 1, 0, 0, 1, -1, 2)


def test_relay_solution_from_routes_has_no_relays():
    routes = ((1, -1), (2, -2))
    solution = relay_solution_from_routes(routes, stations=(_station(),))

    assert solution.relay_count == 0
    assert solution.routes == routes
    assert len(solution.stations) == 1


def test_relay_solution_rejects_duplicate_relay_and_station_ids():
    routes = ((1,), (2, -2, -1))
    first = RelayTransfer(0, 1, 0, 0, 1, 1, 2)
    duplicate = RelayTransfer(0, 2, 0, 0, 1, 1, 2)
    with pytest.raises(ValueError, match="重复"):
        RelaySolution(routes, (first, duplicate), (_station(),))

    with pytest.raises(ValueError, match="重复"):
        RelaySolution(routes, (), (_station(0), _station(0, 3)))


def test_relay_solution_requires_at_least_one_route():
    with pytest.raises(ValueError, match="至少包含一条路线"):
        RelaySolution(())


def test_gaps_for_route():
    assert gaps_for_route((1, 2, -2, -1)) == (0, 1, 2, 3, 4)
    assert gaps_for_route(()) == (0,)


def test_relay_station_label():
    assert _station(3, 7).label == "RS3(P7)"
    assert _station(3, -7).label == "RS3(D7)"
