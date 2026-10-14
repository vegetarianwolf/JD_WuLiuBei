"""Actual ablation worker recording checks with four synthetic tasks.

All optimization and construction functions are mocked. Full route validation,
payload normalization, phase capture, staged configuration, counterfactual
conversion and JSON file writes execute the real production/runner paths.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import importlib
from pathlib import Path
import sys
from types import MappingProxyType

import pytest

from algorithm.experiments import weighted_direct_objective as weighted
from uav_dispatch import alns, io
from uav_dispatch.model import Point, Problem, RelayStation, Score, Task, TransportLeg
from uav_dispatch.search import SolverResult
from uav_dispatch.validation import evaluate_solution


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def recording(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "algorithm/experiments"))
    monkeypatch.setitem(sys.modules, "weighted_direct_objective", weighted)
    runner = importlib.import_module("run_weighted_relay_ablation")
    tasks = (
        Task(1, Point(-10, 0), Point(-9, 0), 100),
        Task(2, Point(-9, 1), Point(10, 1), 100),
        Task(3, Point(10, 2), Point(9, 2), 100),
        Task(4, Point(-8, 1), Point(-8, 2), 100),
    )
    full = Problem(tasks, drone_count=8, max_tasks_per_drone=25,
                   relay_stations=(RelayStation(1, .5, 1),),
                   leg_registry={5: TransportLeg(5, 2, "RELAY_IN", 1),
                                 6: TransportLeg(6, 2, "RELAY_OUT", 1)},
                   task_relay_candidates={2: (1,)})
    plain = replace(full, leg_registry={}, task_relay_candidates={})
    direct_routes = ((1, -1), (2, -2, 3, -3), (4, -4)) + ((),) * 5
    relay_routes = ((1, -1, 5, -5), (6, -6, 3, -3), (4, -4)) + ((),) * 5
    direct_evaluation = evaluate_solution(plain, direct_routes, _force_full=True)
    relay_evaluation = evaluate_solution(full, relay_routes, _force_full=True)
    assert direct_evaluation.valid and relay_evaluation.valid
    assert relay_evaluation.score < direct_evaluation.score
    initial = SolverResult(direct_routes, direct_evaluation)
    topology = {"stations": [[1, .5, 1]],
                "legs": [asdict(x) for x in full.leg_registry.values()],
                "home_points": [[p.x, p.y] for p in full.home_points()]}
    constructed, solved = [], []

    def build_case(values, name):
        assert values == tasks
        return (full if runner.SCENARIOS[name]["relay"] else plain), plain, topology

    def construct(problem, **kwargs):
        assert problem is plain
        constructed.append(kwargs)
        return initial

    def solve(problem, *, config, initial_routes):
        assert tuple(tuple(r) for r in initial_routes) == direct_routes
        assert weighted.active
        solved.append(config)
        routes = relay_routes if config.relay_enabled else direct_routes
        evaluation = evaluate_solution(problem, routes, _force_full=True)
        physical = tuple(asdict(evaluation.score).values())
        metadata = MappingProxyType({
            "initial_score": tuple(asdict(direct_evaluation.score).values()),
            "best_trajectory": ((0, 0., *physical),),
            "best_update_count": 0,
            "terminal_polish": MappingProxyType({
                "active": not config.relay_enabled,
                "statistics": MappingProxyType({"nested": (1, 2)}),
            }),
            "operator_statistics": MappingProxyType({
                "repair:deadline": MappingProxyType({"uses": 2, "accepted": 1}),
            }),
            "profiling": MappingProxyType({"repair_rank_evaluation_count": 3}),
        })
        return SolverResult(routes, evaluation, runtime_seconds=.00001, iterations=7,
                            metadata=metadata)

    monkeypatch.setattr(runner, "build_case", build_case)
    monkeypatch.setattr(io, "load_tasks_csv", lambda path: tasks)
    monkeypatch.setattr(alns, "construct_regret_initial", construct)
    monkeypatch.setattr(alns, "solve_alns", solve)
    output = tmp_path / "worker_recording"
    cases = {}
    for name, spec in runner.SCENARIOS.items():
        config = alns.ALNSConfig(max_iterations=10_000_000, seed=runner.SEEDS[0],
                                 relay_enabled=spec["relay"],
                                 enable_home_seed=spec["homes"] == "stations",
                                 enable_home_bias=spec["homes"] == "stations",
                                 enable_lazy_repair=True, enable_terminal_task_exchange=True)
        cases[name] = {**spec, "topology_digest": runner.digest(topology),
                       "topology_sha256": "0" * 64,
                       "initial_routes_sha256": runner.digest(direct_routes),
                       "config": asdict(config)}
    manifest = dict(input_path="synthetic_fixture.csv", scales=[200, 100, 1000],
                    weights=[.5, .25, .25], formula="Synthetic fixed normalization",
                    finalization_reserve_seconds=1., periodic_parameters=runner.PERIODIC,
                    source_archive={"sha256": "1" * 64}, scenarios=cases)
    runner.write(output / "manifest.json", manifest)
    return dict(runner=runner, output=output, manifest=manifest, constructed=constructed,
                solved=solved, direct_routes=direct_routes, relay_routes=relay_routes,
                fake_solve=solve)


@pytest.mark.parametrize("name", ["pure_direct", "direct_relay", "direct_stations", "direct_relay_stations"])
def test_actual_worker_writes_every_phase_final_and_counterfactual(recording, tmp_path, name):
    fixture, runner = recording, recording["runner"]
    run_id = f"{name}__seed_{runner.SEEDS[1]}"
    request, response = tmp_path / "request.json", tmp_path / "response.json"
    runner.write(request, dict(output=str(fixture["output"]), scenario=name,
                               seed=runner.SEEDS[1], run_id=run_id, response=str(response)))
    original_repair, original_score_order = alns._repair, Score.__lt__
    runner.worker(request)
    assert alns.solve_alns is fixture["fake_solve"]
    assert alns._repair is original_repair and Score.__lt__ is original_score_order
    assert not weighted.active and weighted.coefficients is None
    row = runner.read(response)
    final = runner.read(fixture["output"] / row["solution_path"])
    counterfactual = runner.read(fixture["output"] / row["counterfactual_path"])
    relay_allowed = runner.SCENARIOS[name]["relay"]
    expected_phases = (["direct_warmup", "relay_search"] if relay_allowed else ["direct_search"])
    assert [p["phase"] for p in row["phases"]] == expected_phases
    assert [p["phase"] for p in row["phase_artifacts"]] == ["initial", *expected_phases]
    assert len(fixture["solved"]) == len(expected_phases)
    assert row["valid"] and row["budget_compliant"]
    assert row["seed"] == runner.SEEDS[1]
    assert final["problem"]["task_count"] == counterfactual["problem"]["task_count"] == 4
    assert final["problem"]["objective_order"][0] == "weighted_total_score"
    assert row["best_trajectory"] == final["metadata"]["best_trajectory"]
    assert row["periodic_task_exchange"] == final["metadata"]["periodic_task_exchange"]
    assert final["metadata"]["terminal_polish"]["statistics"]["nested"] == [1, 2]
    assert final["metadata"]["operator_statistics"]["repair:deadline"]["uses"] == (4 if relay_allowed else 2)
    assert final["metadata"]["profiling"]["repair_rank_evaluation_count"] == (6 if relay_allowed else 3)
    assert row["solution_sha256"] == runner.sha(fixture["output"] / row["solution_path"])
    assert row["counterfactual_sha256"] == runner.sha(fixture["output"] / row["counterfactual_path"])
    for record in row["phase_artifacts"]:
        path = fixture["output"] / record["solution_path"]
        value = runner.read(path)
        assert record["solution_sha256"] == runner.sha(path)
        assert value["valid"] and value["violations"] == []
        assert value["metadata"].get("terminal_polish", {}).get("statistics", {}).get("nested") in (None, [1, 2])
    for config in fixture["solved"]:
        assert config.seed == runner.SEEDS[1] and config.relay_direct_warmup_fraction == .95
        assert config.enable_lazy_repair and config.enable_terminal_task_exchange
    assert fixture["constructed"] == [dict(candidate_limit=48,
        home_seed=runner.SCENARIOS[name]["homes"] == "stations",
        home_bias=runner.SCENARIOS[name]["homes"] == "stations")]
    if relay_allowed:
        warm, relay = fixture["solved"]
        assert not warm.relay_enabled and relay.relay_enabled
        assert warm.time_limit_seconds == pytest.approx(row["config"]["time_limit_seconds"] * .95)
        assert relay.time_limit_seconds == pytest.approx(row["config"]["time_limit_seconds"] * .05)
        assert final["relay"]["cross_uav_handoff_count"] == 1
        assert counterfactual["relay"]["relay_task_count"] == 0
        assert counterfactual["task_plans"]["2"]["mode"] == "DIRECT"
        assert counterfactual["routes"][0]["encoded_visits"] == [1, -1]
        assert counterfactual["routes"][1]["encoded_visits"] == [2, -2, 3, -3]
    else:
        assert not fixture["solved"][0].relay_enabled
        assert final["relay"]["relay_task_count"] == 0
        assert counterfactual["score"] == final["score"]


def test_worker_exception_restores_capture_periodic_and_weighted_context(recording, tmp_path, monkeypatch):
    fixture, runner = recording, recording["runner"]
    def fail(*args, **kwargs):
        raise RuntimeError("synthetic solver failure")
    monkeypatch.setattr(alns, "solve_alns", fail)
    original_repair, original_score_order = alns._repair, Score.__lt__
    request, response = tmp_path / "request.json", tmp_path / "response.json"
    runner.write(request, dict(output=str(fixture["output"]), scenario="direct_relay",
                               seed=runner.SEEDS[0], run_id="exception_fixture", response=str(response)))
    with pytest.raises(RuntimeError, match="synthetic solver failure"):
        runner.worker(request)
    assert alns.solve_alns is fail and alns._repair is original_repair
    assert Score.__lt__ is original_score_order and not weighted.active
    assert weighted.coefficients is None and not response.exists()
    assert not (fixture["output"] / "run_solutions/exception_fixture.json").exists()
    assert (fixture["output"] / "phase_solutions/exception_fixture__initial.json").exists()


@pytest.mark.parametrize("field", ["topology_digest", "initial_routes_sha256"])
def test_worker_rejects_wrong_frozen_construction_before_solver(recording, tmp_path, field):
    runner, output = recording["runner"], recording["output"]
    manifest = recording["manifest"]
    manifest["scenarios"]["pure_direct"][field] = "wrong_frozen_digest"
    runner.write(output / "manifest.json", manifest)
    request, response = tmp_path / "request.json", tmp_path / "response.json"
    runner.write(request, dict(output=str(output), scenario="pure_direct", seed=runner.SEEDS[0],
                               run_id="corrupt_fixture", response=str(response)))
    with pytest.raises(AssertionError):
        runner.worker(request)
    assert recording["solved"] == [] and not response.exists()


@pytest.mark.parametrize("name", ["pure_direct", "direct_relay", "direct_stations", "direct_relay_stations"])
def test_actual_build_case_passes_network_config_and_serializes_topology(tmp_path, monkeypatch, name):
    """Keep build_case real, but replace station/deployment construction."""
    monkeypatch.syspath_prepend(str(ROOT / "algorithm/experiments"))
    monkeypatch.setitem(sys.modules, "weighted_direct_objective", weighted)
    runner = importlib.import_module("run_weighted_relay_ablation")
    from algorithm.experiments import run_scenario_comparison as scenarios
    from uav_dispatch.deployment import DemandConfig, DeploymentNode, DeploymentPlan
    tasks = (Task(1, Point(0, 0), Point(2, 0), 10),)
    station = RelayStation(1, 1, 0)
    homes = (1, None, None, None, None, None, None, None) if "stations" in name else None
    full = Problem(tasks, drone_count=8, max_tasks_per_drone=25,
                   relay_stations=(station,), drone_homes=homes,
                   leg_registry={2: TransportLeg(2, 1, "RELAY_IN", 1),
                                 3: TransportLeg(3, 1, "RELAY_OUT", 1)},
                   task_relay_candidates={1: (1,)})
    plan = (DeploymentPlan(homes=homes, nodes=(DeploymentNode("relay_1", 1, 2., 1),),
                           config=DemandConfig(), surrogate_score=1.) if homes else None)
    def build(values, scenario, **kwargs):
        assert values == tasks and scenario == dict(relay=True, drone_homes=runner.SCENARIOS[name]["homes"])
        assert kwargs == dict(drones=8, max_tasks=25, **runner.NETWORK)
        return full, plan
    monkeypatch.setattr(scenarios, "build_scenario_problem", build)
    actual, plain, topology = runner.build_case(tasks, name)
    assert actual.has_relays == runner.SCENARIOS[name]["relay"]
    assert not plain.has_relays and actual.home_points() == plain.home_points()
    path = tmp_path / "topology.json"
    runner.write(path, topology)
    restored = runner.read(path)
    assert runner.digest(topology) == runner.digest(restored)
    assert restored["legs"] == [asdict(full.leg_registry[i]) for i in (2, 3)]
    assert restored["task_relay_candidates"] == {"1": [1]}
    assert restored["home_points"] == [[p.x, p.y] for p in full.home_points()]
    assert (restored["deployment"] is not None) == (homes is not None)
