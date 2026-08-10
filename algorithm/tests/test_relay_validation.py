import pytest

from uav_dispatch import Point, Problem, Task, evaluate_solution
from uav_dispatch.relay import Event, EventType, Hub, RelayPlan, RelayProblem
from uav_dispatch.relay_validation import evaluate_relay_plan


def test_direct_plan_evaluation_is_identical_to_the_legacy_validator() -> None:
    tasks = (
        Task(1, Point(0, 0), Point(0, 0), 8),
        Task(2, Point(0, 0), Point(0, 0), 100),
    )
    base = Problem(
        tasks,
        drone_count=2,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
        distance_matrix_km=(
            (0, 5, 9, 2, 3),
            (5, 0, 4, 6, 7),
            (9, 4, 0, 8, 9),
            (2, 6, 8, 0, 1),
            (3, 7, 9, 1, 0),
        ),
    )
    signed_routes = ((1, -1), (2, -2))
    legacy = evaluate_solution(base, signed_routes)

    relay = evaluate_relay_plan(
        RelayProblem(base), RelayPlan.from_signed_routes(signed_routes)
    )

    assert relay.valid
    assert relay.score == legacy.score
    assert dict(relay.delivery_times_min) == dict(legacy.delivery_times_min)
    assert relay.max_lateness_min == legacy.max_lateness_min
    assert relay.route_distances_km == legacy.route_distances_km
    assert relay.route_completion_times_min == legacy.route_completion_times_min
    assert relay.relay_count == 0
    assert relay.direct_task_count == 2
    assert relay.package_wait_min == 0
    assert relay.uav_wait_min == 0
    assert dict(relay.package_wait_times_min) == {}
    assert dict(relay.hub_peak_inventory) == {}
    assert relay.max_hub_inventory == 0
    assert relay.physical_touch_counts == (1, 1)
    assert relay.primary_owner_counts == (1, 1)


def test_buffered_relay_propagates_cross_drone_time_and_package_wait() -> None:
    base = Problem(
        (
            Task(1, Point(1, 0), Point(10, 0), 13),
            Task(2, Point(1, 0), Point(5, 0), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        speed_km_per_min=1,
    )
    problem = RelayProblem(
        base,
        hubs=(Hub("gate", Point(3, 0)),),
        handoff_service_min=1,
        task_count_semantics="primary-owner",
    )
    plan = RelayPlan(
        (
            (
                Event(1, EventType.PICKUP),
                Event(1, EventType.HANDOFF_DROP, "gate"),
            ),
            (
                Event(2, EventType.PICKUP),
                Event(2, EventType.DELIVERY),
                Event(1, EventType.HANDOFF_PICK, "gate"),
                Event(1, EventType.DELIVERY),
            ),
        )
    )

    result = evaluate_relay_plan(problem, plan)

    assert result.valid
    assert result.event_times_min == ((1, 3), (1, 5, 7, 14))
    assert result.route_completion_times_min == (4, 14)
    assert result.route_distances_km == (3, 14)
    assert result.delivery_times_min == {1: 14, 2: 5}
    assert result.score.late_count == 1
    assert result.score.total_lateness_min == pytest.approx(1)
    assert result.score.distance_km == pytest.approx(17)
    assert result.relay_count == 1
    assert result.direct_task_count == 1
    assert result.package_wait_min == pytest.approx(3)
    assert result.uav_wait_min == 0
    assert result.max_hub_inventory == 1
    assert result.hub_peak_inventory == {"gate": 1}
    assert result.physical_touch_counts == (1, 2)
    assert result.primary_owner_counts == (1, 1)


def test_receiver_waits_when_it_reaches_the_hub_before_package_is_ready() -> None:
    base = Problem(
        (Task(1, Point(1, 0), Point(10, 0), 100),),
        drone_count=2,
        max_tasks_per_drone=1,
        speed_km_per_min=1,
    )
    problem = RelayProblem(
        base,
        hubs=(Hub("gate", Point(3, 0)),),
        handoff_service_min=2,
        task_count_semantics="primary-owner",
    )
    plan = RelayPlan(
        (
            (
                Event(1, EventType.PICKUP),
                Event(1, EventType.HANDOFF_DROP, "gate"),
            ),
            (
                Event(1, EventType.HANDOFF_PICK, "gate"),
                Event(1, EventType.DELIVERY),
            ),
        )
    )

    result = evaluate_relay_plan(problem, plan)

    assert result.valid
    assert result.event_times_min == ((1, 3), (5, 12))
    assert result.package_wait_min == 0
    assert result.uav_wait_min == pytest.approx(2)


def test_cross_drone_custody_cycle_is_rejected() -> None:
    base = Problem(
        (
            Task(1, Point(1, 0), Point(10, 0), 100),
            Task(2, Point(1, 1), Point(10, 1), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
    )
    problem = RelayProblem(
        base,
        hubs=(Hub("gate", Point(5, 0)),),
        task_count_semantics="primary-owner",
    )
    plan = RelayPlan(
        (
            (
                Event(1, EventType.PICKUP),
                Event(2, EventType.HANDOFF_PICK, "gate"),
                Event(1, EventType.HANDOFF_DROP, "gate"),
                Event(2, EventType.DELIVERY),
            ),
            (
                Event(2, EventType.PICKUP),
                Event(1, EventType.HANDOFF_PICK, "gate"),
                Event(2, EventType.HANDOFF_DROP, "gate"),
                Event(1, EventType.DELIVERY),
            ),
        )
    )

    result = evaluate_relay_plan(problem, plan)

    assert not result.valid
    assert any("交接环" in violation for violation in result.violations)


def test_saturated_strict_touch_rejects_relay_but_primary_owner_allows_it() -> None:
    base = Problem(
        (
            Task(1, Point(1, 0), Point(2, 0), 100),
            Task(2, Point(1, 1), Point(2, 1), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=1,
    )
    hub = Hub("gate", Point(1.5, 0))
    plan = RelayPlan(
        (
            (
                Event(1, EventType.PICKUP),
                Event(1, EventType.HANDOFF_DROP, "gate"),
            ),
            (
                Event(1, EventType.HANDOFF_PICK, "gate"),
                Event(1, EventType.DELIVERY),
                Event(2, EventType.PICKUP),
                Event(2, EventType.DELIVERY),
            ),
        )
    )

    strict = evaluate_relay_plan(
        RelayProblem(base, hubs=(hub,), task_count_semantics="strict-touch"),
        plan,
    )
    primary_owner = evaluate_relay_plan(
        RelayProblem(base, hubs=(hub,), task_count_semantics="primary-owner"),
        plan,
    )

    assert not strict.valid
    assert strict.semantics_extension is False
    assert strict.physical_touch_counts == (1, 2)
    assert any("触达 2 个任务" in violation for violation in strict.violations)
    assert primary_owner.valid
    assert primary_owner.semantics_extension is True
    assert primary_owner.primary_owner_counts == (1, 1)
    assert primary_owner.relay_count == 1


def test_custody_rules_reject_split_direct_service_and_mismatched_hubs() -> None:
    base = Problem(
        (Task(1, Point(1, 0), Point(2, 0), 100),),
        drone_count=2,
        max_tasks_per_drone=1,
    )
    split_direct = evaluate_relay_plan(
        RelayProblem(base),
        RelayPlan(
            (
                (Event(1, EventType.PICKUP),),
                (Event(1, EventType.DELIVERY),),
            )
        ),
    )
    mismatched_hubs = evaluate_relay_plan(
        RelayProblem(
            base,
            hubs=(Hub("west", Point(1, 0)), Hub("east", Point(2, 0))),
            task_count_semantics="primary-owner",
        ),
        RelayPlan(
            (
                (
                    Event(1, EventType.PICKUP),
                    Event(1, EventType.HANDOFF_DROP, "west"),
                ),
                (
                    Event(1, EventType.HANDOFF_PICK, "east"),
                    Event(1, EventType.DELIVERY),
                ),
            )
        ),
    )

    assert not split_direct.valid
    assert any(
        "直接取送不在同一架" in item for item in split_direct.violations
    )
    assert not mismatched_hubs.valid
    assert any("交接点不一致" in item for item in mismatched_hubs.violations)


def test_capacity_is_checked_across_pickup_and_handoff_pick_events() -> None:
    base = Problem(
        tuple(
            Task(task_id, Point(task_id, 0), Point(task_id + 4, 0), 100)
            for task_id in range(1, 4)
        ),
        drone_count=1,
        max_tasks_per_drone=3,
        capacity=2,
    )
    plan = RelayPlan.from_signed_routes(((1, 2, 3, -1, -2, -3),))

    result = evaluate_relay_plan(RelayProblem(base), plan)

    assert not result.valid
    assert any("载荷 3 超过上限 2" in item for item in result.violations)


def test_relay_disabled_and_incomplete_event_sets_are_rejected() -> None:
    base = Problem(
        (Task(1, Point(1, 0), Point(2, 0), 100),),
        drone_count=2,
        max_tasks_per_drone=1,
    )
    hub = Hub("gate", Point(1.5, 0))
    relay_plan = RelayPlan(
        (
            (
                Event(1, EventType.PICKUP),
                Event(1, EventType.HANDOFF_DROP, "gate"),
            ),
            (
                Event(1, EventType.HANDOFF_PICK, "gate"),
                Event(1, EventType.DELIVERY),
            ),
        )
    )
    disabled = evaluate_relay_plan(
        RelayProblem(
            base,
            hubs=(hub,),
            max_handoffs_per_task=0,
            task_count_semantics="primary-owner",
        ),
        relay_plan,
    )
    incomplete = evaluate_relay_plan(
        RelayProblem(base, hubs=(hub,)),
        RelayPlan(
            (
                (
                    Event(1, EventType.PICKUP),
                    Event(1, EventType.HANDOFF_DROP, "gate"),
                ),
            )
        ),
    )

    assert not disabled.valid
    assert any("不允许交接" in item for item in disabled.violations)
    assert not incomplete.valid
    assert any("事件组成非法" in item for item in incomplete.violations)
