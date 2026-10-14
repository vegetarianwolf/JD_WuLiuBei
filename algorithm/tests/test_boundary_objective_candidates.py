"""Independent DIRECT candidates for deadline rescue and objective rewards."""

from dataclasses import replace
from random import Random

import pytest

import uav_dispatch.alns as alns
from uav_dispatch import (
    ALNSConfig, Point, Problem, RelayStation, Score, Task, TransportLeg,
    construct_regret_initial, evaluate_solution, solve_alns,
)
from uav_dispatch.search import RouteEvaluator


class _FirstRanked(Random):
    def random(self):
        return 0.0


def _fleet(*, drones=2):
    tasks = tuple(
        Task(i, Point(i, i % 3), Point(i + 0.5, 1), 5 + i)
        for i in range(1, 9)
    )
    return Problem(tasks, drone_count=drones, max_tasks_per_drone=8 // drones)


def _decisions(result):
    return (
        result.routes, result.evaluation.score, result.iterations,
        result.metadata["initial_score"], result.metadata["accepted_solutions"],
        dict(result.metadata["operator_uses"]),
        dict(result.metadata["operator_statistics"]),
        dict(result.metadata["operator_weights"]),
        tuple((p[0], *p[2:]) for p in result.metadata["best_trajectory"]),
    )


def test_boundary_destroy_rescues_near_late_tasks_and_opens_two_route_slots():
    problem = Problem(
        (
            Task(1, Point(10, 0), Point(11, 0), 12),
            Task(2, Point(1, 0), Point(2, 0), 20),
            Task(3, Point(10, 0), Point(11, 0), 12),
            Task(4, Point(1, 0), Point(2, 0), 21),
            Task(5, Point(10, 0), Point(11, 0), 0.5),
            Task(6, Point(1, 0), Point(2, 0), 15),
        ),
        drone_count=3, max_tasks_per_drone=2, speed_km_per_min=1,
    )
    routes = ((1, -1, 2, -2), (3, -3, 4, -4), (5, -5, 6, -6))
    evaluator = RouteEvaluator(problem)
    partial, removed = alns.destroy_solution(
        problem, evaluator, routes, "deadline_boundary", 2, _FirstRanked(),
    )
    assert removed == (4, 2)  # lateness 1 and 2; task 5 is individually impossible.
    assert [sum(v < 0 for v in route) for route in partial] == [1, 1, 2]
    assert all(v not in {2, -2, 4, -4} for route in partial for v in route)
    _, deep_removed = alns.destroy_solution(
        problem, evaluator, routes, "late_critical", 1, _FirstRanked(),
    )
    assert deep_removed == (5,)
    repaired = alns._repair(problem, evaluator, partial, removed, "greedy", None)
    assert evaluate_solution(problem, repaired, _force_full=True).valid


@pytest.mark.parametrize("count", [1, 2, 4, 8, 99])
def test_boundary_destroy_removes_complete_unique_pairs(count):
    problem = _fleet()
    initial = construct_regret_initial(problem, candidate_limit=None)
    partial, removed = alns.destroy_solution(
        problem, RouteEvaluator(problem), initial.routes,
        "deadline_boundary", count, Random(4),
    )
    assert len(removed) == len(set(removed)) == min(count, 8)
    for task_id in problem.task_ids:
        flat = tuple(v for route in partial for v in route)
        assert flat.count(task_id) == flat.count(-task_id) == (task_id not in removed)


@pytest.mark.parametrize(
    "candidate,current,best,accepted,improved_best,reward",
    [
        (Score(2, 100, 50), Score(3, 10, 40), Score(3, 10, 40), True, True, 16),
        (Score(3, 100, 50), Score(4, 10, 40), Score(2, 10, 40), True, False, 8),
        (Score(2, 9, 50), Score(2, 10, 40), Score(2, 10, 40), True, True, 2),
        (Score(2, 9, 50), Score(2, 10, 40), Score(2, 8, 40), True, False, 1),
        (Score(2, 10, 39), Score(2, 10, 40), Score(2, 8, 40), True, False, 1),
        (Score(2, 10, 40), Score(2, 10, 40), Score(2, 10, 40), True, False, 0),
        (Score(3, 1, 1), Score(2, 10, 40), Score(2, 10, 40), True, False, 0),
        (Score(3, 1, 1), Score(2, 10, 40), Score(2, 10, 40), False, False, 0),
    ],
)
def test_objective_reward_separates_primary_secondary_equal_and_worse(
    candidate, current, best, accepted, improved_best, reward,
):
    assert alns._objective_reward(
        candidate, current, best, accepted=accepted, improved_best=improved_best,
    ) == reward


def test_equal_score_acceptance_is_preserved_but_does_not_earn_reward(monkeypatch):
    problem = _fleet()
    initial = construct_regret_initial(problem, candidate_limit=None)
    monkeypatch.setattr(alns, "_repair", lambda *args, **kwargs: initial.routes)
    config = ALNSConfig(max_iterations=4, weight_update_interval=1)
    baseline = solve_alns(problem, config=config, initial_routes=initial.routes)
    candidate = solve_alns(
        problem, config=replace(config, enable_objective_rewards=True),
        initial_routes=initial.routes,
    )
    assert candidate.metadata["accepted_solutions"] == 4
    assert candidate.routes == baseline.routes == initial.routes
    for result, total_reward in ((baseline, 4.0), (candidate, 0.0)):
        repairs = [v for k, v in result.metadata["operator_statistics"].items()
                   if k.startswith("repair:")]
        assert sum(v["total_reward"] for v in repairs) == total_reward
        assert sum(v["equal_score_accepts"] for v in repairs) == 4
        assert sum(v["unchanged_route_accepts"] for v in repairs) == 4
        assert sum(v["best_late_count_improvements"] for v in repairs) == 0
    assert all(w >= 0.05 for w in candidate.metadata["operator_weights"].values())


@pytest.mark.parametrize("boundary,rewards", [(True, False), (False, True), (True, True)])
def test_new_flags_never_change_initial_construction(boundary, rewards):
    problem = _fleet()
    baseline = solve_alns(problem, config=ALNSConfig(max_iterations=0, enable_terminal_polish=False))
    candidate = solve_alns(problem, config=ALNSConfig(
        max_iterations=0, enable_boundary_destroy=boundary, enable_terminal_polish=False,
        enable_objective_rewards=rewards,
    ))
    assert candidate.routes == baseline.routes
    assert candidate.evaluation == baseline.evaluation
    assert candidate.metadata["initial_score"] == baseline.metadata["initial_score"]
    assert candidate.metadata["boundary_destroy_active"] is boundary
    assert candidate.metadata["objective_rewards_active"] is rewards
    assert len(candidate.metadata["operator_uses"]) == 14 + int(boundary)
    assert "destroy:late_critical" in candidate.metadata["operator_uses"]
    assert not candidate.metadata["enable_route_distinct_regret"]
    assert not candidate.metadata["enable_regret_portfolio"]


@pytest.mark.parametrize("boundary,rewards", [(True, False), (False, True), (True, True)])
def test_single_drone_keeps_every_search_decision(boundary, rewards):
    problem = _fleet(drones=1)
    initial = construct_regret_initial(problem, candidate_limit=12)
    config = ALNSConfig(max_iterations=20, seed=4, candidate_limit=12)
    baseline = solve_alns(problem, config=config, initial_routes=initial.routes)
    candidate = solve_alns(problem, initial_routes=initial.routes, config=replace(
        config, enable_boundary_destroy=boundary, enable_objective_rewards=rewards,
    ))
    assert _decisions(candidate) == _decisions(baseline)
    assert candidate.metadata["boundary_destroy_active"] is False
    assert candidate.metadata["objective_rewards_active"] is False


def test_relay_search_keeps_every_search_decision():
    problem = Problem(
        (Task(1, Point(1, 0), Point(2, 0), 10),
         Task(2, Point(3, 0), Point(4, 0), 10)),
        drone_count=2, max_tasks_per_drone=1,
        relay_stations=(RelayStation(1, 0, 0),),
        leg_registry={3: TransportLeg(3, 1, "RELAY_IN", 1),
                      4: TransportLeg(4, 1, "RELAY_OUT", 1)},
        task_relay_candidates={1: (1,)},
    )
    initial = ((1, -1), (2, -2))
    config = ALNSConfig(max_iterations=4, enable_relay_seed=False, enable_relay_refine=False)
    baseline = solve_alns(problem, config=config, initial_routes=initial)
    candidate = solve_alns(problem, initial_routes=initial, config=replace(
        config, enable_boundary_destroy=True, enable_objective_rewards=True,
    ))
    assert _decisions(candidate) == _decisions(baseline)
    assert candidate.metadata["boundary_destroy_active"] is False
    assert candidate.metadata["objective_rewards_active"] is False


def test_boundary_operator_is_used_without_replacing_existing_operator(monkeypatch):
    problem = _fleet()
    monkeypatch.setattr(alns, "_roulette", lambda weights, rng:
                        "deadline_boundary" if "random" in weights else "greedy")
    result = solve_alns(problem, config=ALNSConfig(
        max_iterations=3, candidate_limit=12, enable_boundary_destroy=True,
    ))
    assert result.evaluation.valid
    assert result.metadata["operator_uses"]["destroy:deadline_boundary"] == 3
    assert "late_critical" in alns.DESTROY_OPERATORS
    assert "deadline_boundary" not in alns.DESTROY_OPERATORS


def test_new_experiments_are_disabled_by_default():
    config = ALNSConfig()
    assert config.enable_boundary_destroy is False
    assert config.enable_objective_rewards is False
