"""Actual recording path on a tiny fixture; never runs the official optimizer."""
import importlib
from pathlib import Path
from types import MappingProxyType


def test_periodic_worker_enters_after_construction_and_records_native_stats(tmp_path, monkeypatch):
    from uav_dispatch import alns, io
    from uav_dispatch.model import Point, Problem, Task
    from uav_dispatch.search import SolverResult
    from uav_dispatch.validation import evaluate_solution

    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "algorithm/experiments"))
    runner = importlib.import_module("run_weighted_periodic_pair")
    helper = importlib.import_module("weighted_periodic_exchange")
    tasks = tuple(Task(i, Point(i, 0), Point(i + .2, .1), 10) for i in range(1, 5))
    routes = tuple((i, -i) for i in range(1, 5)) + ((),) * 4
    problem = Problem(tasks, drone_count=8, max_tasks_per_drone=25)
    evaluation = evaluate_solution(problem, routes, _force_full=True)
    result = SolverResult(routes, evaluation, iterations=1, metadata=MappingProxyType({
        "best_trajectory": (), "terminal_polish": MappingProxyType({"active": True}),
        "operator_statistics": MappingProxyType({}), "profiling": MappingProxyType({}),
    }))
    monkeypatch.setattr(io, "load_tasks_csv", lambda path: tasks)
    original_repair = alns._repair
    seen = []

    def fake_construct(problem, **kwargs):
        assert alns._repair is original_repair and not helper._active
        seen.append("construction")
        return result

    def fake_solve(problem, *, config, initial_routes):
        assert alns._repair is not original_repair and helper._active
        assert tuple(initial_routes) == routes
        seen.append("solve")
        return result

    monkeypatch.setattr(alns, "construct_regret_initial", fake_construct)
    monkeypatch.setattr(alns, "solve_alns", fake_solve)
    manifest = runner.read(runner.FIRST / "manifest.json")
    manifest["initial_routes_sha256"] = runner.first.digest(routes)
    manifest["periodic_parameters"] = dict(interval=120, max_candidates=16, anchors_per_route=4,
                                            pair_budget_seconds=.008)
    manifest["variants"] = {"weighted_periodic": {
        **manifest["variants"]["weighted_exchange_lazy"], "label": "Tiny recording fixture",
    }}
    output = tmp_path / "output"
    runner.write(output / "manifest.json", manifest)
    request, response = tmp_path / "request.json", tmp_path / "response.json"
    runner.write(request, dict(output=str(output), variant="weighted_periodic", response=str(response)))
    runner.worker(request)
    row = runner.read(response)
    payload = runner.read(output / row["solution_path"])
    assert seen == ["construction", "solve"]
    assert alns.solve_alns is fake_solve and alns._repair is original_repair and not helper._active
    assert row["periodic_task_exchange"] == payload["metadata"]["periodic_task_exchange"]
    assert row["periodic_task_exchange"] == dict(repair_calls=0, passes=0, probes=0,
        canonical_checks=0, improvements=0, runtime_seconds=0., events=[])
    assert payload["problem"]["task_count"] == 4
