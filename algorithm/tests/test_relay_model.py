from math import inf

import pytest

import uav_dispatch

from uav_dispatch import Point, Problem, Task
from uav_dispatch.relay import (
    Event,
    EventType,
    Hub,
    RelayPlan,
    RelayProblem,
    TaskCountSemantics,
)


def test_relay_public_api_is_exported_from_the_package() -> None:
    assert uav_dispatch.RelayProblem is RelayProblem
    assert uav_dispatch.RelayPlan is RelayPlan
    assert uav_dispatch.solve_relay_alns is not None


def test_signed_routes_round_trip_through_direct_relay_events() -> None:
    signed_routes = ((2, -2), (1, -1), ())

    plan = RelayPlan.from_signed_routes(signed_routes)

    assert plan.routes == (
        (
            Event(2, EventType.PICKUP),
            Event(2, EventType.DELIVERY),
        ),
        (
            Event(1, EventType.PICKUP),
            Event(1, EventType.DELIVERY),
        ),
        (),
    )
    assert plan.to_signed_routes() == signed_routes


def test_relay_problem_preserves_base_distances_and_adds_static_hubs() -> None:
    task = Task(1, Point(10, 0), Point(20, 0), 100)
    base = Problem(
        (task,),
        distance_matrix_km=((0, 7, 9), (8, 0, 4), (6, 3, 0)),
    )
    problem = RelayProblem(
        base,
        hubs=(Hub("gate", Point(3, 4)),),
        handoff_service_min=1.5,
        task_count_semantics="primary-owner",
    )

    pickup = Event(1, EventType.PICKUP)
    delivery = Event(1, EventType.DELIVERY)
    drop = Event(1, EventType.HANDOFF_DROP, "gate")

    assert problem.distance(None, pickup) == 7
    assert problem.distance(pickup, delivery) == 4
    assert problem.distance(None, drop) == 5
    assert problem.point_for_event(drop) == Point(3, 4)
    assert problem.task_count_semantics is TaskCountSemantics.PRIMARY_OWNER
    assert problem.semantics_extension is True


@pytest.mark.parametrize(
    "builder",
    [
        lambda base: Event(0, EventType.PICKUP),
        lambda base: Event(1, EventType.PICKUP, "gate"),
        lambda base: Event(1, EventType.HANDOFF_DROP),
        lambda base: Event(1, EventType.HANDOFF_DROP, 7),
        lambda base: Hub("", Point(0, 0)),
        lambda base: Hub("gate", (0, 0)),
        lambda base: RelayProblem(
            base,
            hubs=(Hub("gate", Point(0, 0)), Hub("gate", Point(1, 0))),
        ),
        lambda base: RelayProblem(base, hubs=("gate",)),
        lambda base: RelayProblem(base, handoff_service_min=inf),
        lambda base: RelayProblem(base, max_handoffs_per_task=2),
    ],
)
def test_relay_model_rejects_malformed_events_hubs_and_configuration(builder) -> None:
    base = Problem((Task(1, Point(0, 0), Point(1, 0), 10),))

    with pytest.raises(ValueError):
        builder(base)


def test_relay_plan_rejects_non_events_and_invalid_signed_visits() -> None:
    with pytest.raises(ValueError):
        RelayPlan(((1,),))
    with pytest.raises(ValueError):
        RelayPlan.from_signed_routes(((True,),))
