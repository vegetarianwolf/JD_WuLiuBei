"""Auxiliary exported route distances use the same start homes as validation."""

import pytest

from uav_dispatch import (
    GlobalRelayEvaluator, Point, Problem, RelayStation, Task, TransportLeg,
    build_plan_index, evaluate_solution, relay_statistics,
)
from uav_dispatch.cli import result_payload
from uav_dispatch.search import SolverResult


@pytest.mark.parametrize("relay", [False, True])
@pytest.mark.parametrize("homes, expected_second", [
    (None, (4.0, 8.0)),
    ((None, 1, 1), (3.0, 7.0)),
    ((None, Point(1.0, 0.0), Point(1.0, 0.0)), (3.0, 7.0)),
])
def test_relay_statistics_and_payload_distances_match_home_aware_full_evaluation(relay, homes, expected_second):
    problem = Problem(
        (
            Task(1, Point(1, 0), Point(2, 0), 100),
            Task(2, Point(3, 0), Point(4, 0), 100),
        ),
        drone_count=3, max_tasks_per_drone=1,
        relay_stations=(RelayStation(1, 1, 0),),
        drone_homes=homes,
        leg_registry={
            3: TransportLeg(3, 2, "RELAY_IN", 1),
            4: TransportLeg(4, 2, "RELAY_OUT", 1),
        } if relay else {},
        task_relay_candidates={2: (1,)} if relay else {},
    )
    routes = ((1, -1), (3, -3, 4, -4) if relay else (2, -2), ())
    evaluation = evaluate_solution(problem, routes, _force_full=True)
    assert evaluation.valid
    # Origin drone, station/point/origin drone, and an idle drone respectively.
    expected = (2.0, expected_second[int(relay)], 0.0)
    assert evaluation.route_distances_km == expected
    stats = relay_statistics(
        problem, routes, GlobalRelayEvaluator(problem).evaluate(routes),
        build_plan_index(problem, routes),
    )
    assert tuple(row["distance_km"] for row in stats["per_drone"]) == expected
    payload = result_payload(
        problem, SolverResult(routes=routes, evaluation=evaluation), source="test",
    )
    assert [row["distance_km"] for row in payload["relay"]["per_drone"]] == [
        row["distance_km"] for row in payload["routes"]
    ]
    assert sum(row["distance_km"] for row in payload["relay"]["per_drone"]) == payload["score"]["distance_km"]
