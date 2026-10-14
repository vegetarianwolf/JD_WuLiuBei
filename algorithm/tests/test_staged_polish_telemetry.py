"""Staged Relay telemetry must retain real DIRECT warm-up polish work."""

import pytest

import uav_dispatch.alns as alns
from uav_dispatch import (
    ALNSConfig, Point, Problem, RelayStation, Task, TransportLeg,
    evaluate_solution,
)


@pytest.mark.parametrize("enabled", [False, True])
def test_staged_fixed_iteration_counts_include_warmup_with_final_phase_inactive(
    monkeypatch, enabled,
):
    tasks = (
        Task(1, Point(10, 0), Point(11, 0), 30),
        Task(2, Point(1, 0), Point(2, 0), 3),
        Task(3, Point(1, 1), Point(2, 1), 100),
        Task(4, Point(3, 1), Point(4, 1), 100),
    )
    problem = Problem(
        tasks, drone_count=2, max_tasks_per_drone=2,
        relay_stations=(RelayStation(1, 5, 0),),
        leg_registry={5: TransportLeg(5, 1, "RELAY_IN", 1),
                      6: TransportLeg(6, 1, "RELAY_OUT", 1)},
        task_relay_candidates={1: (1,)},
    )
    initial = ((1, 2, -1, -2), (3, -3, 4, -4))
    # A deterministic warm repair supplies a route with a known strict
    # first adjacent improvement at iteration 40. The real solver handles
    # scoring, acceptance, incumbent protection and both staged phases.
    real_repair, real_solve = alns._repair, alns.solve_alns
    phases = []

    def controlled_repair(*args, **kwargs):
        if kwargs.get("relay_cfg") is None:
            return initial
        return real_repair(*args, **kwargs)

    def observed_solve(*args, **kwargs):
        result = real_solve(*args, **kwargs)
        phases.append(result)
        return result

    monkeypatch.setattr(alns, "_repair", controlled_repair)
    monkeypatch.setattr(alns, "solve_alns", observed_solve)
    result = alns.solve_relay_staged(
        problem,
        initial_routes=initial,
        config=ALNSConfig(
            max_iterations=100, seed=3, relay_direct_warmup_fraction=.8,
            enable_adjacent_polish=enabled,
            enable_terminal_polish=False,
            enable_relay_seed=False, enable_relay_refine=False,
        ),
    )
    warm, relay = phases
    assert (warm.iterations, relay.iterations, result.iterations) == (80, 20, 100)
    assert result.metadata["adjacent_polish_active"] is False
    assert relay.metadata["adjacent_polish_active"] is False
    assert warm.metadata["adjacent_polish_active"] is enabled
    for key in (
        "adjacent_polish_passes", "adjacent_polish_probes",
        "adjacent_polish_position_checks", "adjacent_polish_improvements",
    ):
        assert relay.metadata[key] == 0
        assert result.metadata[key] == warm.metadata[key] + relay.metadata[key]
        assert bool(result.metadata[key]) is enabled
    if enabled:
        assert result.metadata["adjacent_polish_passes"] == 2
        assert result.metadata["adjacent_polish_improvements"] == 1
    assert evaluate_solution(problem, result.routes, _force_full=True).valid
    assert result.evaluation.score <= warm.evaluation.score
