"""Equivalence tests for GlobalRelayEvaluator.evaluate_delta.

The differential evaluator must reproduce the full evaluator bit-for-bit on
every candidate that differs from its base solution in at most two routes,
and fall back (with identical results) on larger or cyclic changes.
"""

from random import Random

import pytest

from uav_dispatch import (
    GlobalRelayEvaluator,
    Point,
    Problem,
    RelayStation,
    Task,
    TransportLeg,
    build_plan_index,
    construct_regret_initial,
)


def _relay_problem(drone_count: int = 3, task_count: int = 5) -> Problem:
    tasks = tuple(
        Task(
            i,
            Point((i * 7) % 23 - 10, (i * 3) % 11 - 5),
            Point((i * 5) % 19 - 8, (i * 7) % 13 - 6),
            60.0 + 10.0 * i,
        )
        for i in range(1, task_count + 1)
    )
    stations = (
        RelayStation(1, 0.0, 0.0),
        RelayStation(2, 5.0, 4.0),
    )
    counter = task_count + 1
    registry: dict[int, TransportLeg] = {}
    candidates: dict[int, tuple[int, ...]] = {}
    for task in tasks:
        relay_ids = []
        for station in stations:
            in_id = counter
            counter += 1
            out_id = counter
            counter += 1
            registry[in_id] = TransportLeg(
                in_id, task.id, "RELAY_IN", station.id
            )
            registry[out_id] = TransportLeg(
                out_id, task.id, "RELAY_OUT", station.id
            )
            relay_ids.append(station.id)
        candidates[task.id] = tuple(relay_ids)
    return Problem(
        tasks,
        drone_count=drone_count,
        max_tasks_per_drone=task_count,
        relay_stations=stations,
        leg_registry=registry,
        task_relay_candidates=candidates,
    )


def _leg_ids(problem: Problem, task_id: int, relay_id: int):
    legs = [
        leg
        for leg in problem.leg_registry.values()
        if leg.task_id == task_id and leg.relay_id == relay_id
    ]
    in_id = next(leg.id for leg in legs if leg.kind == "RELAY_IN")
    out_id = next(leg.id for leg in legs if leg.kind == "RELAY_OUT")
    return in_id, out_id


def _move_direct_task(routes, from_route: int, to_route: int, task_id: int):
    """Move one DIRECT task's visits between two routes (<=2 routes change)."""
    out = list(routes)
    out[from_route] = tuple(
        visit for visit in routes[from_route] if abs(visit) != task_id
    )
    out[to_route] = routes[to_route] + (
        task_id,
        -task_id,
    )
    return tuple(out)


def _assert_schedules_equal(delta, full, label: str) -> None:
    assert delta.feasible == full.feasible, f"{label}: feasible mismatch"
    assert delta.score == full.score, (
        f"{label}: score {delta.score} != {full.score}"
    )
    assert delta.delivery_times_min == full.delivery_times_min, (
        f"{label}: delivery times differ"
    )
    assert delta.total_waiting_min == pytest.approx(
        full.total_waiting_min, abs=1e-12
    ), f"{label}: waiting differs"
    assert delta.max_waiting_min == pytest.approx(
        full.max_waiting_min, abs=1e-12
    ), f"{label}: max waiting differs"
    assert dict(delta.event_times_min) == dict(full.event_times_min), (
        f"{label}: event times differ"
    )


def test_delta_equals_full_for_one_and_two_route_moves():
    problem = _relay_problem(drone_count=3, task_count=6)
    # Cross-UAV relay for task 5 (inbound on route 0, outbound on route 1)
    # plus direct tasks spread over all three routes, so moving a direct
    # task re-times routes that carry handoff edges.
    in5, out5 = _leg_ids(problem, 5, 1)
    base = (
        (1, -1, 2, -2, in5, -in5),
        (3, -3, 4, -4, out5, -out5),
        (6, -6),
    )
    evaluator = GlobalRelayEvaluator(problem)
    base_schedule = evaluator.evaluate(base)
    assert base_schedule.feasible
    plan_index = build_plan_index(problem, base)
    assert plan_index.relay_task_ids == (5,)

    direct_tasks = [1, 2, 3, 4, 6]
    rng = Random(11)
    cases = 0
    for from_route in range(problem.drone_count):
        for to_route in range(problem.drone_count):
            if from_route == to_route:
                continue
            task_id = rng.choice(direct_tasks)
            candidate = _move_direct_task(
                base, from_route, to_route, task_id
            )
            delta = evaluator.evaluate_delta(
                base, base_schedule, candidate
            )
            full = evaluator.evaluate(candidate)
            _assert_schedules_equal(
                delta, full, f"move {task_id} {from_route}->{to_route}"
            )
            cases += 1
    assert cases >= 6


def test_delta_identity_returns_base_schedule():
    problem = _relay_problem(drone_count=3, task_count=5)
    base = construct_regret_initial(problem, candidate_limit=None).routes
    evaluator = GlobalRelayEvaluator(problem)
    schedule = evaluator.evaluate(base)
    assert evaluator.evaluate_delta(base, schedule, base) is schedule


def test_delta_falls_back_when_more_than_two_routes_change():
    problem = _relay_problem(drone_count=3, task_count=5)
    base = construct_regret_initial(problem, candidate_limit=None).routes
    evaluator = GlobalRelayEvaluator(problem)
    schedule = evaluator.evaluate(base)
    # Change three routes at once (append a duplicate direct pair to each).
    candidate = tuple(
        (base[i] + (1, -1)) if i < 3 else base[i]
        for i in range(len(base))
    )
    delta = evaluator.evaluate_delta(base, schedule, candidate)
    full = evaluator.evaluate(candidate)
    assert delta.feasible == full.feasible
    assert delta.score == full.score
    assert dict(delta.event_times_min) == dict(full.event_times_min)


def test_delta_cycle_falls_back_to_full_evaluation():
    """A handoff cycle introduced by a two-route change must be detected."""
    problem = _relay_problem(drone_count=2, task_count=2)
    in_a, out_a = _leg_ids(problem, 1, 1)
    in_b, out_b = _leg_ids(problem, 2, 1)
    base = (
        (in_a, -in_a, in_b, -in_b),
        (out_a, -out_a, out_b, -out_b),
    )
    evaluator = GlobalRelayEvaluator(problem)
    schedule = evaluator.evaluate(base)
    assert schedule.feasible
    # Cross the handoffs: task A's drop and task B's pickup on route 0,
    # task B's drop and task A's pickup on route 1, forming a directed
    # cycle -in_A -> +out_A -> -in_B -> +out_B -> -in_A.
    candidate = (
        (out_b, -out_b, in_a, -in_a),
        (out_a, -out_a, in_b, -in_b),
    )
    full = evaluator.evaluate(candidate)
    assert not full.feasible
    delta = evaluator.evaluate_delta(base, schedule, candidate)
    assert delta.feasible == full.feasible
    assert delta.score == full.score
    assert dict(delta.event_times_min) == dict(full.event_times_min)
