import copy
from pathlib import Path

import pytest

import algorithm.experiments.audit_sensitivity_results as audit
from algorithm.experiments.audit_sensitivity_results import (
    audit_formal_runtime,
    audit_mechanism_statistics,
    audit_saved_solution,
    validate_source_fingerprints,
)
from algorithm.experiments.run_sensitivity_analysis import SENSITIVITY_SCENARIOS
from uav_dispatch import construct_regret_initial
from uav_dispatch.cli import result_payload
from uav_dispatch.model import Point, Problem, Task


def _mechanism_example():
    problem, payload, row = _saved_example()
    row.update({
        "deadhead_km": 1.0, "home_assignment_rate": 1.0,
        "relay_task_count": 0, "cross_uav_handoff_count": 0,
        "total_relay_waiting_min": 0.0,
    })
    return problem, payload, row


def test_mechanism_audit_recomputes_selected_routes():
    audit_mechanism_statistics(*_mechanism_example())


@pytest.mark.parametrize("field", [
    "deadhead_km", "home_assignment_rate", "relay_task_count",
    "cross_uav_handoff_count", "total_relay_waiting_min",
])
def test_mechanism_audit_rejects_stale_row_statistics(field):
    problem, payload, row = _mechanism_example()
    row[field] += 1
    with pytest.raises(ValueError, match=f"{field} disagrees with row"):
        audit_mechanism_statistics(problem, payload, row)


def test_mechanism_audit_rejects_inconsistent_per_drone_diagnostics():
    problem, payload, row = _mechanism_example()
    payload["relay"]["per_drone"][0]["distance_km"] += 1
    with pytest.raises(ValueError, match="per-drone distance"):
        audit_mechanism_statistics(problem, payload, row)


@pytest.mark.parametrize("retained", [False, True])
def test_staged_selection_audit_accepts_strict_minimum(retained):
    problem, payload, row = _mechanism_example()
    chosen = [payload["score"][key] for key in (
        "late_count", "total_lateness_min", "distance_km"
    )]
    worse = [chosen[0] + 1, *chosen[1:]]
    payload["metadata"].update({
        "staged_relay": True,
        "relay_warmup_score": chosen if retained else worse,
        "relay_search_final_score": worse if retained else chosen,
        "relay_retained_warmup_incumbent": retained,
        "relay_phase_best_update_count": 0,
    })
    audit_mechanism_statistics(problem, payload, row)
    payload["metadata"]["relay_retained_warmup_incumbent"] = not retained
    with pytest.raises(ValueError, match="incumbent selection"):
        audit_mechanism_statistics(problem, payload, row)


def test_staged_selection_audit_retains_relay_on_an_exact_tie():
    problem, payload, row = _mechanism_example()
    chosen = [payload["score"][key] for key in (
        "late_count", "total_lateness_min", "distance_km"
    )]
    payload["metadata"].update({
        "staged_relay": True, "relay_warmup_score": chosen,
        "relay_search_final_score": chosen,
        "relay_retained_warmup_incumbent": False,
    })
    audit_mechanism_statistics(problem, payload, row)


def test_mechanism_audit_recomputes_cross_drone_waiting_and_metadata():
    from uav_dispatch import (
        RelayStation, TransportLeg, evaluate_solution, GlobalRelayEvaluator,
        build_plan_index, relay_statistics,
    )
    from uav_dispatch.search import SolverResult

    problem = Problem(
        (Task(1, Point(10, 0), Point(2, 0), 100),),
        drone_count=2, max_tasks_per_drone=1,
        relay_stations=(RelayStation(1, 1, 0),),
        leg_registry={
            2: TransportLeg(2, 1, "RELAY_IN", 1),
            3: TransportLeg(3, 1, "RELAY_OUT", 1),
        },
        task_relay_candidates={1: (1,)},
    )
    routes = ((2, -2), (3, -3))
    result = SolverResult(routes, evaluate_solution(problem, routes), 0, 0, {
        "relay_enabled": True,
        "relay": relay_statistics(
            problem, routes, GlobalRelayEvaluator(problem).evaluate(routes),
            build_plan_index(problem, routes),
        ),
    })
    payload = result_payload(problem, result, source="test")
    row = {"deadhead_km": 11, "home_assignment_rate": 0,
           "relay_task_count": 1, "cross_uav_handoff_count": 1,
           "total_relay_waiting_min": 18 / problem.speed_km_per_min}
    audit_mechanism_statistics(problem, payload, row)
    payload["metadata"]["relay"]["total_relay_waiting_time"] = 0
    with pytest.raises(ValueError, match="waiting_min disagrees with metadata"):
        audit_mechanism_statistics(problem, payload, row)


def _saved_example():
    problem = Problem(
        (Task(1, Point(1, 0), Point(2, 0), 1.0),),
        drone_count=1, max_tasks_per_drone=25,
    )
    result = construct_regret_initial(problem)
    payload = result_payload(problem, result, source="test")
    return problem, payload, copy.deepcopy(payload["score"])


def test_saved_routes_are_independently_rescored():
    audit_saved_solution(*_saved_example())


@pytest.mark.parametrize("target", ["row", "solution"])
def test_audit_rejects_a_stale_objective_even_when_valid_flag_is_true(target):
    problem, payload, row = _saved_example()
    (row if target == "row" else payload["score"])["late_count"] = 0
    with pytest.raises(ValueError, match=f"late_count disagrees with {target}"):
        audit_saved_solution(problem, payload, row)


def test_audit_rejects_infeasible_serialized_routes_even_when_valid_flag_is_true():
    problem, payload, row = _saved_example()
    payload["routes"][0]["encoded_visits"] = [-1, 1]
    with pytest.raises(ValueError, match="infeasible"):
        audit_saved_solution(problem, payload, row)


@pytest.mark.parametrize("mutation", ["missing", "extra", "changed", "nonfinite"])
def test_audit_rejects_inconsistent_saved_delivery_times(mutation):
    problem, payload, row = _saved_example()
    if mutation == "missing":
        payload["delivery_times_min"].pop("1")
    elif mutation == "extra":
        payload["delivery_times_min"]["2"] = 1.0
    else:
        payload["delivery_times_min"]["1"] = (
            float("nan") if mutation == "nonfinite" else 0.0
        )
    with pytest.raises(ValueError, match="delivery times disagree"):
        audit_saved_solution(problem, payload, row)


def _source_fingerprints():
    directory = Path(audit.__file__).parent
    return {
        "experiment_script_sha256": audit._sha256(directory / "run_sensitivity_analysis.py"),
        "scenario_script_sha256": audit._sha256(directory / "run_scenario_comparison.py"),
        "solver_source_sha256": audit._solver_source_sha256(),
    }


def test_audit_accepts_matching_source_fingerprints():
    validate_source_fingerprints(_source_fingerprints())


@pytest.mark.parametrize(
    "field", ["experiment_script_sha256", "scenario_script_sha256", "solver_source_sha256"]
)
def test_audit_rejects_changed_experiment_scenario_and_solver_sources(field):
    manifest = _source_fingerprints()
    manifest[field] = "modified"
    with pytest.raises(ValueError, match=field):
        validate_source_fingerprints(manifest)


def _formal_runtime_example(scenario_id="direct_relay"):
    scenario = next(s for s in SENSITIVITY_SCENARIOS if s["id"] == scenario_id)
    construction = 0.5
    solver_runtime = 300.0 - construction - 0.05
    row = {
        "seed": 2026081701,
        "scenario": scenario_id,
        "construction_seconds": construction,
        "solver_runtime_seconds": solver_runtime,
        "wall_seconds": construction + solver_runtime,
        "relay_warmup_runtime_seconds": solver_runtime * 0.8 if scenario["relay"] else 0.0,
        "relay_search_runtime_seconds": solver_runtime * 0.2 if scenario["relay"] else 0.0,
    }
    payload = {
        "runtime_seconds": solver_runtime,
        "metadata": {
            "seed": row["seed"],
            "time_limit_seconds": solver_runtime,
            "relay_enabled": scenario["relay"],
            "enable_home_seed": scenario["drone_homes"] == "stations",
        },
    }
    if scenario["relay"]:
        payload["metadata"].update({
            "staged_relay": True,
            "relay_direct_warmup_fraction": 0.8,
            "relay_warmup_runtime_seconds": row["relay_warmup_runtime_seconds"],
            "relay_search_runtime_seconds": row["relay_search_runtime_seconds"],
        })
    return row, payload, scenario


def _audit_runtime(row, payload, scenario):
    audit_formal_runtime(
        row, payload, scenario, budget_seconds=300.0, safety_margin_seconds=0.05
    )


@pytest.mark.parametrize("scenario", [s["id"] for s in SENSITIVITY_SCENARIOS])
def test_audit_accepts_formal_runtimes_for_every_strategy(scenario):
    _audit_runtime(*_formal_runtime_example(scenario))


@pytest.mark.parametrize(
    "field", ["construction_seconds", "solver_runtime_seconds", "wall_seconds",
              "relay_warmup_runtime_seconds", "relay_search_runtime_seconds"]
)
@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
def test_audit_rejects_nonfinite_or_negative_runtime_fields(field, value):
    row, payload, scenario = _formal_runtime_example()
    row[field] = value
    with pytest.raises(ValueError, match=field):
        _audit_runtime(row, payload, scenario)


def test_audit_rejects_runtime_below_the_formal_budget_even_if_metadata_matches():
    row, payload, scenario = _formal_runtime_example("pure_direct")
    row["solver_runtime_seconds"] = payload["runtime_seconds"] = 10.0
    row["wall_seconds"] = 10.5
    with pytest.raises(ValueError, match="solver runtime does not match"):
        _audit_runtime(row, payload, scenario)


def test_audit_rejects_wall_time_above_the_formal_budget():
    row, payload, scenario = _formal_runtime_example()
    row["wall_seconds"] = 330.0
    with pytest.raises(ValueError, match="wall runtime does not match"):
        _audit_runtime(row, payload, scenario)


@pytest.mark.parametrize("field", ["runtime_seconds", "time_limit_seconds"])
def test_audit_rejects_saved_runtime_that_disagrees_with_row_or_budget(field):
    row, payload, scenario = _formal_runtime_example()
    target = payload if field == "runtime_seconds" else payload["metadata"]
    target[field] -= 10
    with pytest.raises(ValueError, match="Saved solver.*disagrees"):
        _audit_runtime(row, payload, scenario)


@pytest.mark.parametrize(
    "field, value", [("seed", 7), ("relay_enabled", False), ("enable_home_seed", True)]
)
def test_audit_rejects_incorrect_saved_solver_seed_or_mechanisms(field, value):
    row, payload, scenario = _formal_runtime_example()
    payload["metadata"][field] = value
    with pytest.raises(ValueError, match="Saved solver.*disagrees"):
        _audit_runtime(row, payload, scenario)


def test_audit_rejects_a_nonformal_declared_relay_phase_fraction():
    row, payload, scenario = _formal_runtime_example()
    payload["metadata"]["relay_direct_warmup_fraction"] = 0.5
    with pytest.raises(ValueError, match="80% / 20%"):
        _audit_runtime(row, payload, scenario)


def test_audit_rejects_wrong_observed_phase_split_even_if_recorded_fraction_is_correct():
    row, payload, scenario = _formal_runtime_example()
    for field in ("relay_warmup_runtime_seconds", "relay_search_runtime_seconds"):
        row[field] = payload["metadata"][field] = row["solver_runtime_seconds"] * 0.5
    with pytest.raises(ValueError, match="阶段时长未按正式比例分配"):
        _audit_runtime(row, payload, scenario)


def test_audit_rejects_saved_phase_time_that_disagrees_with_row():
    row, payload, scenario = _formal_runtime_example()
    payload["metadata"]["relay_warmup_runtime_seconds"] -= 1.0
    with pytest.raises(ValueError, match="Saved relay_warmup_runtime_seconds disagrees"):
        _audit_runtime(row, payload, scenario)


def test_audit_rejects_relay_phase_runtime_for_a_direct_strategy():
    row, payload, scenario = _formal_runtime_example("direct_stations")
    row["relay_warmup_runtime_seconds"] = 1.0
    with pytest.raises(ValueError, match="Non-Relay strategy"):
        _audit_runtime(row, payload, scenario)
