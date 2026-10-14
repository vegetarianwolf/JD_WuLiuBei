"""Standalone joint solver correctness, transfer semantics and budget guards."""

from dataclasses import replace
from time import perf_counter

import pytest

import uav_dispatch.joint_relay as joint
from uav_dispatch import (
    GlobalRelayEvaluator, Point, Problem, RelayStation, Task, TransportLeg,
    build_plan_index, evaluate_solution,
)
from uav_dispatch.search import RouteEvaluator


def _full_fleet():
    problem = Problem(
        (
            Task(1, Point(-10, 0), Point(-9, 0), 100),
            Task(2, Point(-9, 1), Point(10, 1), 100),
            Task(3, Point(10, 2), Point(9, 2), 100),
            Task(4, Point(9, 3), Point(-10, 3), 100),
        ), drone_count=2, max_tasks_per_drone=2,
        relay_stations=(RelayStation(1, 0, 1.5),),
        leg_registry={
            5: TransportLeg(5, 2, "RELAY_IN", 1),
            6: TransportLeg(6, 2, "RELAY_OUT", 1),
            7: TransportLeg(7, 4, "RELAY_IN", 1),
            8: TransportLeg(8, 4, "RELAY_OUT", 1),
        }, task_relay_candidates={2: (1,), 4: (1,)},
    )
    return problem, ((1, -1, 2, -2), (3, -3, 4, -4))


def _context(problem, routes, config=None, deadline=None):
    initial = evaluate_solution(problem, routes, _force_full=True)
    assert initial.valid
    started = perf_counter()
    return joint._Context(
        problem, config or joint.JointRelayConfig(candidate_limit=None),
        RouteEvaluator(problem), GlobalRelayEvaluator(problem), started,
        deadline, routes, initial,
        [(0, 0., initial.score.late_count, initial.score.total_lateness_min,
          initial.score.distance_km)],
    )


def _wait_problem():
    problem = Problem(
        (
            Task(1, Point(10, 0), Point(12, 0), 15),
            Task(2, Point(6, 0), Point(7, 0), 20),
        ), drone_count=2, max_tasks_per_drone=2,
        relay_stations=(RelayStation(1, 5, 0),),
        leg_registry={
            3: TransportLeg(3, 1, "RELAY_IN", 1),
            4: TransportLeg(4, 1, "RELAY_OUT", 1),
        }, task_relay_candidates={1: (1,)},
    )
    return problem, ((3, -3), (4, -4, 2, -2))


def test_joint_two_task_replacement_opens_full_k_slots_and_changes_responsibility():
    problem, routes = _full_fleet()
    original = build_plan_index(problem, routes)
    context = _context(problem, routes)
    candidate = joint._joint_neighbor(context, routes, (2, 4), original)
    assert candidate is not None
    evaluation = evaluate_solution(problem, candidate.routes, _force_full=True)
    assert evaluation.valid and evaluation.score < evaluate_solution(problem, routes, _force_full=True).score
    # Both aircraft started with exactly K=2 final-delivery duties. Joint
    # replacement exchanges them and creates a genuine cross-UAV handoff.
    final = build_plan_index(problem, candidate.routes)
    assert final.delivery_route_by_task[2] == original.delivery_route_by_task[4]
    assert final.delivery_route_by_task[4] == original.delivery_route_by_task[2]
    assert final.relay_task_ids
    assert any(final.inbound_route_by_task[t] != final.outbound_route_by_task[t]
               for t in final.relay_task_ids)
    assert [sum(route_index == i for route_index in final.delivery_route_by_task.values())
            for i in range(2)] == [2, 2]


def test_remove_original_relay_task_deletes_all_its_legs_from_every_aircraft():
    problem, routes = _wait_problem()
    reduced = joint._remove_tasks(problem, routes, (1,))
    assert reduced == ((), (2, -2))
    assert set(build_plan_index(problem, reduced).task_plans) == {2}
    context = _context(problem, routes)
    candidate = joint._single_neighbor(context, routes, 1, build_plan_index(problem, routes))
    assert candidate is not None
    checked = evaluate_solution(problem, candidate.routes, _force_full=True)
    assert checked.valid
    assert set(checked.delivery_times_min) == {1, 2}


def test_existing_waits_are_scored_globally_and_zero_iterations_preserve_incumbent():
    problem, routes = _wait_problem()
    expected = evaluate_solution(problem, routes, _force_full=True)
    result = joint.solve_joint_relay(
        problem, config=joint.JointRelayConfig(max_iterations=0), initial_routes=routes,
    )
    assert result.routes == routes and result.evaluation == expected
    assert result.metadata["relay"]["total_relay_waiting_time"] > 0
    assert result.metadata["relay"]["cross_uav_handoff_count"] == 1
    assert result.metadata["best_update_count"] == 0
    assert result.metadata["direct_warmup"] is False


def test_cycle_in_a_proposed_cross_handoff_is_rejected():
    problem, initial = _full_fleet()
    context = _context(problem, initial)
    # Each UAV waits for a package whose inbound drop follows its own
    # outbound pickup: the two handoffs form a precedence cycle.
    cycle = ((1, -1, 8, -8, 5, -5), (3, -3, 6, -6, 7, -7))
    assert not context.schedule(cycle).feasible
    assert context.confirm(joint._Plan(cycle, context.best_evaluation.score, "RELAY", 0)) is None
    assert context.best == initial
    with pytest.raises(ValueError, match="初始解非法"):
        joint.solve_joint_relay(problem, initial_routes=cycle)


def test_fixed_iteration_search_is_deterministic_and_keeps_strict_best_history():
    problem, initial = _full_fleet()
    config = joint.JointRelayConfig(max_iterations=25, candidate_limit=None, seed=7, perturb_after=2)
    first = joint.solve_joint_relay(problem, config=config, initial_routes=initial)
    second = joint.solve_joint_relay(problem, config=config, initial_routes=initial)
    assert first.iterations == second.iterations == 25
    assert first.routes == second.routes
    assert first.evaluation == second.evaluation
    assert first.metadata["operator_uses"] == second.metadata["operator_uses"]
    assert first.metadata["joint_search"] == second.metadata["joint_search"]
    trajectory = first.metadata["best_trajectory"]
    assert [p[0] for p in trajectory] == [p[0] for p in second.metadata["best_trajectory"]]
    assert [p[2:] for p in trajectory] == [p[2:] for p in second.metadata["best_trajectory"]]
    assert all(later[2:] < earlier[2:] for earlier, later in zip(trajectory, trajectory[1:]))
    assert tuple(first.metadata["initial_score"]) == trajectory[0][2:]
    assert first.evaluation.score <= evaluate_solution(problem, initial, _force_full=True).score
    assert first.metadata["relay"]["cross_uav_handoff_count"] > 0


def test_deadline_is_checked_before_generating_task_candidates(monkeypatch):
    problem, routes = _full_fleet()
    context = _context(problem, routes, deadline=1.)
    monkeypatch.setattr(joint, "perf_counter", lambda: 2.)
    with pytest.raises(joint._DeadlineReached):
        joint._task_plans(context, joint._remove_tasks(problem, routes, (2,)), 2, (0, 1))
    assert context.work["plan_generations"] == 0
    assert context.work["generated_plans"] == 0


def test_candidate_confirmed_after_deadline_cannot_replace_best(monkeypatch):
    problem, routes = _full_fleet()
    context = _context(problem, routes)
    improved = joint._joint_neighbor(context, routes, (2, 4), build_plan_index(problem, routes))
    context = _context(problem, routes, deadline=1.)
    values = iter((0., 2.))
    monkeypatch.setattr(joint, "perf_counter", lambda: next(values))
    with pytest.raises(joint._DeadlineReached):
        context.confirm(improved)
    assert context.best == routes
    assert len(context.trajectory) == 1


def test_completed_improvement_survives_later_deadline_inside_neighborhood(monkeypatch):
    problem, initial = _full_fleet()
    probe = _context(problem, initial)
    candidate = joint._joint_neighbor(probe, initial, (2, 4), build_plan_index(problem, initial))

    def interrupted(context, routes, pair, index):
        assert context.confirm(candidate) is not None
        raise joint._DeadlineReached

    monkeypatch.setattr(joint, "_joint_neighbor", interrupted)
    result = joint.solve_joint_relay(
        problem, config=joint.JointRelayConfig(max_iterations=5), initial_routes=initial,
    )
    assert result.routes == candidate.routes
    assert result.evaluation.score < evaluate_solution(problem, initial).score
    assert result.iterations == 1
    assert result.metadata["joint_search"]["deadline_reached"] is True
    assert result.metadata["best_trajectory"][-1][0] <= result.iterations


def test_fallback_construction_is_complete_and_works_without_relay():
    problem, _ = _full_fleet()
    problem = replace(problem, relay_stations=(), leg_registry={}, task_relay_candidates={})
    result = joint.solve_joint_relay(problem, config=joint.JointRelayConfig(max_iterations=3))
    assert result.evaluation.valid
    assert set(result.evaluation.delivery_times_min) == set(problem.task_ids)
    assert result.metadata["method"] == "Joint-Relay-VNS"
    assert result.metadata["relay"]["relay_task_count"] == 0


@pytest.mark.parametrize("kwargs", [
    {"max_iterations": -1}, {"time_limit_seconds": 0}, {"time_limit_seconds": float("nan")},
    {"pair_beam": 0}, {"route_pool_size": 0}, {"candidate_limit": 1},
    {"perturb_max_extra_late": -1},
])
def test_joint_config_rejects_invalid_bounds(kwargs):
    with pytest.raises(ValueError):
        joint.JointRelayConfig(**kwargs)
