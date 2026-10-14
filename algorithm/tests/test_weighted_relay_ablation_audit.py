"""Synthetic independent relay audits; never run the official optimizer."""
from __future__ import annotations

import copy

import pytest

from algorithm.experiments import audit_weighted_relay_ablation as audit


def topology(drones=2, maximum=25, capacity=2, *, stations=None, legs=None, homes=None):
    return dict(drone_count=drones, max_tasks_per_drone=maximum, capacity=capacity,
                speed_km_per_min=1.0, home_points=homes or [[0.0, 0.0] for _ in range(drones)],
                stations=stations or [], legs=legs or [])


def relay_topology(*, homes=None, maximum=25):
    return topology(maximum=maximum, stations=[[1, 2.0, 0.0]], homes=homes,
        legs=[dict(id=11, task_id=1, kind="RELAY_IN", relay_id=1),
              dict(id=12, task_id=1, kind="RELAY_OUT", relay_id=1)])


def test_cross_drone_wait_is_counted_once_and_package_storage_is_separate():
    tasks = {1: ((3.0, 0.0), (4.0, 0.0), 4.0)}
    result = audit.reference_schedule(tasks, [[11, -11], [12, -12]], relay_topology())
    assert result["score"] == dict(late_count=1, total_lateness_min=2.0, distance_km=8.0)
    assert result["event_times_min"] == [[3.0, 4.0], [4.0, 6.0]]
    handoff = result["handoffs"][0]
    assert handoff["aircraft_arrival_min"] == 2.0
    assert handoff["aircraft_waiting_min"] == 2.0
    assert handoff["package_cache_min"] == 0.0
    assert result["relay_statistics"]["cross_uav_handoff_count"] == 1
    assert result["route_records"][0]["task_count"] == 0
    assert result["route_records"][1]["task_count"] == 1


def test_package_cache_never_becomes_aircraft_wait():
    tasks = {1: ((1.0, 0.0), (4.0, 0.0), 20.0)}
    geo = relay_topology(homes=[[0.0, 0.0], [10.0, 0.0]])
    result = audit.reference_schedule(tasks, [[11, -11], [12, -12]], geo)
    handoff = result["handoffs"][0]
    assert handoff["drop_time_min"] == 2.0
    assert handoff["pickup_time_min"] == 8.0
    assert handoff["aircraft_waiting_min"] == 0.0
    assert handoff["package_cache_min"] == 6.0
    assert result["relay_statistics"]["total_relay_waiting_time"] == 0.0


def test_same_drone_adjacent_drop_pickup_merges_parallel_edges():
    tasks = {1: ((1.0, 0.0), (4.0, 0.0), 20.0)}
    result = audit.reference_schedule(tasks, [[11, -11, 12, -12], []], relay_topology())
    assert result["event_times_min"] == [[1.0, 2.0, 2.0, 4.0], []]
    assert result["relay_statistics"]["same_uav_relay_count"] == 1
    assert result["relay_statistics"]["cross_uav_handoff_count"] == 0
    assert result["score"]["distance_km"] == 4.0


def test_earlier_wait_is_not_recounted_at_a_later_handoff():
    tasks = {1: ((3.0, 0.0), (2.0, 0.0), 20.0), 2: ((1.0, 0.0), (3.0, 0.0), 20.0)}
    legs = [dict(id=10 + 2 * task + offset, task_id=task,
                 kind="RELAY_IN" if offset == 0 else "RELAY_OUT", relay_id=1)
            for task in tasks for offset in (0, 1)]
    geo = topology(stations=[[1, 2.0, 0.0]], legs=legs)
    result = audit.reference_schedule(tasks, [[12, -12, 14, -14], [13, -13, 15, -15]], geo)
    assert result["handoffs"][0]["aircraft_waiting_min"] == 2.0
    assert result["handoffs"][1]["aircraft_waiting_min"] == 2.0
    assert result["relay_statistics"]["total_relay_waiting_time"] == 4.0


def test_cross_leg_pickup_and_delivery_on_different_drones_is_rejected():
    with pytest.raises(ValueError, match="crosses drones|undelivered load"):
        audit.reference_schedule({1: ((1.0, 0.0), (4.0, 0.0), 20.0)},
                                 [[11], [-11, 12, -12]], relay_topology())


def test_handoff_cycle_is_rejected_even_when_each_leg_is_locally_complete():
    tasks = {1: ((0.0, 0.0), (1.0, 0.0), 20.0), 2: ((0.0, 0.0), (1.0, 0.0), 20.0)}
    legs = [dict(id=10 + 2 * task + offset, task_id=task,
                 kind="RELAY_IN" if offset == 0 else "RELAY_OUT", relay_id=1)
            for task in tasks for offset in (0, 1)]
    geo = topology(stations=[[1, 0.0, 0.0]], legs=legs)
    # Task 1's outbound precedes task 2's inbound on drone 1; converse on 2.
    with pytest.raises(ValueError, match="dependency cycle"):
        audit.reference_schedule(tasks, [[13, -13, 14, -14], [15, -15, 12, -12]], geo)


def test_inbound_leg_does_not_consume_final_responsibility_slot():
    tasks = {1: ((1.0, 0.0), (4.0, 0.0), 20.0), 2: ((1.0, 0.0), (2.0, 0.0), 20.0)}
    result = audit.reference_schedule(tasks, [[11, -11, 2, -2], [12, -12]], relay_topology(maximum=1))
    assert [record["task_count"] for record in result["route_records"]] == [1, 1]
    assert [record["transport_leg_count"] for record in result["route_records"]] == [2, 1]


def test_k25_rejects_twenty_six_final_deliveries():
    tasks = {task: ((0.0, 0.0), (1.0, 0.0), 20.0) for task in range(1, 27)}
    with pytest.raises(ValueError, match="exceeds K"):
        audit.reference_schedule(tasks, [[visit for task in tasks for visit in (task, -task)], []], topology())


@pytest.mark.parametrize("routes, message", [
    ([[1, -1], []], "Every original task"),
    ([[1, -1, 1, -1], [2, -2]], "Duplicate service"),
    ([[-1, 1], [2, -2]], "precedes pickup"),
    ([[1, -1], [2]], "undelivered load"),
    ([[True, -1], [2, -2]], "noninteger"),
    ([[0], [2, -2]], "noninteger"),
    ([[999], [2, -2]], "Unknown task"),
])
def test_completeness_unique_events_and_pickup_order_are_independent(routes, message):
    tasks = {1: ((0.0, 0.0), (1.0, 0.0), 20.0), 2: ((0.0, 0.0), (1.0, 0.0), 20.0)}
    with pytest.raises(ValueError, match=message):
        audit.reference_schedule(tasks, routes, topology())


def test_all_two_hundred_tasks_k25_and_origin_to_first_leg_open_route():
    tasks = {task: ((2.0, 0.0), (3.0, 0.0), 1000.0) for task in range(1, 201)}
    routes = [[visit for task in range(1 + 25 * drone, 26 + 25 * drone) for visit in (task, -task)]
              for drone in range(8)]
    result = audit.reference_schedule(tasks, routes, topology(drones=8))
    assert len(result["delivery_times_min"]) == 200
    assert [record["task_count"] for record in result["route_records"]] == [25] * 8
    assert result["score"]["distance_km"] == 8 * 51.0
    assert result["route_records"][0]["completion_time_min"] == 51.0
    assert result["relay_statistics"]["relay_task_count"] == 0


def test_capacity_checks_load_at_each_event_not_only_endpoints():
    tasks = {task: ((0.0, 0.0), (1.0, 0.0), 20.0) for task in range(1, 4)}
    with pytest.raises(ValueError, match="package capacity"):
        audit.reference_schedule(tasks, [[1, 2, 3, -1, -2, -3], []], topology())


@pytest.mark.parametrize("mutate,message", [
    (lambda t: t["legs"].pop(), "unpaired"),
    (lambda t: t["legs"][0].update(id=1), "colliding"),
    (lambda t: t["legs"][0].update(kind="OTHER"), "kind"),
    (lambda t: t["legs"][0].update(relay_id=2), "Unknown task or station"),
    (lambda t: t["stations"].append(t["stations"][0]), "Duplicate station"),
    (lambda t: t.update(home_points=[[0.0, 0.0]]), "Home count"),
    (lambda t: t.update(speed_km_per_min=0.0), "Nonpositive speed"),
])
def test_frozen_registry_and_geometry_must_be_valid(mutate, message):
    geo = relay_topology()
    mutate(geo)
    with pytest.raises(ValueError, match=message):
        audit.reference_schedule({1: ((1.0, 0.0), (4.0, 0.0), 20.0)}, [[11, -11], [12, -12]], geo)


def test_task_cannot_mix_direct_and_relay_or_choose_multiple_stations():
    geo = relay_topology()
    with pytest.raises(ValueError, match="mixes DIRECT"):
        audit.reference_schedule({1: ((1.0, 0.0), (4.0, 0.0), 20.0)},
                                 [[11, -11], [1, -1, 12, -12]], geo)
    geo["stations"].append([2, 3.0, 0.0])
    geo["legs"].extend([dict(id=13, task_id=1, kind="RELAY_IN", relay_id=2),
                         dict(id=14, task_id=1, kind="RELAY_OUT", relay_id=2)])
    with pytest.raises(ValueError, match="different stations"):
        audit.reference_schedule({1: ((1.0, 0.0), (4.0, 0.0), 20.0)}, [[11, -11], [14, -14]], geo)


def test_contracting_relay_preserves_final_responsibility_and_removes_inbound_load():
    tasks = {1: ((1.0, 0.0), (4.0, 0.0), 20.0), 2: ((1.0, 0.0), (3.0, 0.0), 20.0)}
    geo = relay_topology(maximum=1)
    routes = [[11, 2, -11, -2], [12, -12]]
    relay = audit.reference_schedule(tasks, routes, geo)
    contracted_routes = audit.direct_counterfactual(routes, geo)
    assert contracted_routes == [[2, -2], [1, -1]]
    contracted = audit.reference_schedule(tasks, contracted_routes, geo)
    assert [r["task_count"] for r in contracted["route_records"]] == [1, 1]
    assert contracted["relay_statistics"]["relay_task_count"] == 0
    assert contracted["route_records"][0]["load_after_event"] == [1, 0]
    assert relay["route_records"][0]["load_after_event"] == [1, 2, 1, 0]
    assert routes == [[11, 2, -11, -2], [12, -12]]


def test_direct_without_any_station_or_registry_is_supported():
    tasks = {1: ((1.0, 0.0), (2.0, 0.0), 20.0)}
    result = audit.reference_schedule(tasks, [[1, -1]], topology(drones=1))
    assert result["score"]["distance_km"] == 2.0
    assert audit.direct_counterfactual([[1, -1]], topology(drones=1)) == [[1, -1]]
    assert result["station_usage"] == []


def test_reference_output_is_json_native_and_input_is_not_mutated():
    tasks = {1: ((1.0, 0.0), (4.0, 0.0), 20.0)}
    geo = relay_topology()
    original = copy.deepcopy(geo)
    result = audit.reference_schedule(tasks, [[11, -11], [12, -12]], geo)
    assert audit.json.loads(audit.json.dumps(result, allow_nan=False)) == result
    assert geo == original


@pytest.fixture
def saved_relay_payload():
    """Hand-written known schedule; not generated by the implementation under test."""
    tasks = {1: ((3.0, 0.0), (4.0, 0.0), 4.0)}
    geo = relay_topology()
    geo.update(drone_homes=[None, None], relay_allowed=True, task_relay_candidates={"1": [1]})
    def event(leg_id, action, point, clock):
        return dict(task_id=1, leg_id=leg_id, relay_id=1, action=action, kind=action.upper(),
                    location_km=point, arrival_time_min=clock)
    payload = dict(valid=True, violations=[],
        problem=dict(task_count=1, drone_count=2, max_tasks_per_drone=25, capacity=2,
            speed_km_per_min=1.0, depot_km=[0.0, 0.0], drone_homes=["origin", "origin"],
            open_routes=True, service_time_min=0.0),
        score=dict(late_count=1, total_lateness_min=2.0, distance_km=8.0),
        delivery_times_min={"1": 6.0}, on_time_rate=0.0, max_lateness_min=2.0,
        max_route_distance_km=4.0, max_route_completion_min=6.0,
        routes=[dict(drone_id=1, task_count=0, transport_leg_count=1, relay_drop_count=1,
            relay_pickup_count=0, distance_km=4.0, completion_time_min=4.0,
            late_count=0, total_lateness_min=0.0, on_time_task_count=0, on_time_rate=1.0,
            encoded_visits=[11, -11], visits=[event(11, "pickup", [3.0, 0.0], 3.0),
                                            event(11, "relay_drop", [2.0, 0.0], 4.0)]),
            dict(drone_id=2, task_count=1, transport_leg_count=1, relay_drop_count=0,
            relay_pickup_count=1, distance_km=4.0, completion_time_min=6.0,
            late_count=1, total_lateness_min=2.0, on_time_task_count=0, on_time_rate=0.0,
            encoded_visits=[12, -12], visits=[event(12, "relay_pickup", [2.0, 0.0], 4.0),
                                            event(12, "delivery", [4.0, 0.0], 6.0)])],
        relay=dict(relay_count=1, relay_candidate_coverage=1.0, relay_coordinates=[[1, 2.0, 0.0]],
            direct_task_count=0, relay_task_count=1, relay_share=1.0, cross_uav_handoff_count=1,
            same_uav_relay_count=0, total_relay_waiting_time=2.0, average_relay_waiting_time=2.0,
            max_relay_waiting_time=2.0,
            relay_usage_per_station=[dict(station_id=1, x=2.0, y=0.0, drop_count=1,
                pickup_count=1, task_count=1, average_wait=2.0)],
            per_drone=[dict(drone_id=1, original_task_count=0, transport_leg_count=1,
                           relay_drop_count=1, relay_pickup_count=0, distance_km=4.0),
                       dict(drone_id=2, original_task_count=1, transport_leg_count=1,
                           relay_drop_count=0, relay_pickup_count=1, distance_km=4.0)]),
        task_plans={"1": dict(mode="RELAY", relay_id=1, leg_ids=[11, 12])})
    return tasks, geo, payload


def test_saved_payload_every_event_and_counter_is_bound_to_independent_geometry(saved_relay_payload):
    tasks, geo, payload = saved_relay_payload
    reference = audit.audit_payload(tasks, geo, payload)
    assert reference["handoffs"][0]["aircraft_waiting_min"] == 2.0
    assert reference["score"] == payload["score"]


@pytest.mark.parametrize("mutate", [
    lambda p: p["routes"][1]["visits"][0].update(arrival_time_min=2.0),
    lambda p: p["routes"][0]["visits"][1].update(location_km=[3.0, 0.0]),
    lambda p: p["routes"][1]["visits"][0].update(action="pickup", kind="PICKUP"),
    lambda p: p["routes"][1]["visits"][1].update(leg_id=11),
    lambda p: p["routes"][0].update(task_count=1),
    lambda p: p["routes"][1].update(completion_time_min=4.0),
    lambda p: p["routes"][1].update(total_lateness_min=0.0),
    lambda p: p["delivery_times_min"].update({"1": 4.0}),
    lambda p: p["score"].update(distance_km=7.0),
    lambda p: p["score"].update(late_count=True),
    lambda p: p["problem"].update(open_routes=False),
    lambda p: p["problem"].update(capacity=3),
    lambda p: p["problem"].update(drone_homes=["origin", "station_1"]),
    lambda p: p["relay"].update(total_relay_waiting_time=0.0),
    lambda p: p["relay"].update(cross_uav_handoff_count=0),
    lambda p: p["relay"]["relay_usage_per_station"][0].update(average_wait=4.0),
    lambda p: p["relay"]["per_drone"][0].update(original_task_count=1),
    lambda p: p["task_plans"]["1"].update(leg_ids=[12, 11]),
    lambda p: p.update(max_route_completion_min=4.0),
    lambda p: p.update(max_lateness_min=0.0),
])
def test_saved_payload_rejects_clock_coordinate_structure_and_metadata_corruption(saved_relay_payload, mutate):
    tasks, geo, payload = saved_relay_payload
    mutate(payload)
    with pytest.raises(ValueError):
        audit.audit_payload(tasks, geo, payload)


@pytest.fixture
def synthetic_contract():
    parent_config = dict(seed=2026101461, time_limit_seconds=None, max_iterations=10_000_000,
                         relay_enabled=False, enable_home_seed=False, enable_home_bias=False)
    parent = dict(weights=[.5, .25, .25], scales=[200, 4464.183540734935, 775.7731679286865],
                  formula="fixed J", input_path="input.csv", input_sha256="1" * 64,
                  problem_path="problem.docx", problem_sha256="2" * 64,
                  variants={"weighted_periodic": {"config": parent_config}})
    cases = {}
    for name in audit.ORDER:
        relay, homes = "relay" in name, "stations" in name
        routes = [[1, -1]]
        cases[name] = dict(relay=relay, homes="stations" if homes else "origin", initial_routes=routes,
            initial_routes_sha256=audit.digest(routes),
            config={**parent_config, "seed": audit.SEEDS[0], "relay_enabled": relay,
                    "enable_home_seed": homes, "enable_home_bias": homes})
    manifest = {key: copy.deepcopy(parent[key]) for key in (
        "weights", "scales", "formula", "input_path", "input_sha256", "problem_path", "problem_sha256")}
    manifest.update(kind="weighted_relay_ablation", schema_version=1, seeds=audit.SEEDS.copy(),
        order=audit.ORDER.copy(), budget_seconds=240, finalization_reserve_seconds=1.0,
        network_parameters=copy.deepcopy(audit.NETWORK), periodic_parameters=copy.deepcopy(audit.PERIODIC),
        scenarios=cases, protocol=dict(planned_unique_runs=12, no_result_based_stopping=True),
        schedule=[dict(run_id=f"{name}__seed_{seed}", scenario=name, seed=seed)
                  for index, seed in enumerate(audit.SEEDS) for name in audit.ORDER[index:] + audit.ORDER[:index]])
    return manifest, parent


def test_prespecified_contract_has_twelve_runs_without_score_based_gates(synthetic_contract):
    manifest, parent = synthetic_contract
    audit.audit_contract(manifest, parent)
    assert len(manifest["schedule"]) == 12


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(schema_version=True),
    lambda m: m["seeds"].reverse(),
    lambda m: m["schedule"].pop(),
    lambda m: m["schedule"].reverse(),
    lambda m: m.update(budget_seconds=480),
    lambda m: m.update(finalization_reserve_seconds=0),
    lambda m: m["periodic_parameters"].update(interval=60),
    lambda m: m["network_parameters"].update(relay_count=6),
    lambda m: m["weights"].__setitem__(0, .6),
    lambda m: m["scales"].__setitem__(1, 4000),
    lambda m: m["scenarios"]["direct_relay"]["config"].update(relay_enabled=False),
    lambda m: m["scenarios"]["direct_relay"]["config"].update(candidate_limit=24),
    lambda m: m["scenarios"]["direct_relay"].update(initial_routes=[[1, -1, 2, -2]]),
    lambda m: m["protocol"].update(no_result_based_stopping=False),
])
def test_contract_rejects_budgets_weights_cherry_picking_and_confounded_controls(synthetic_contract, mutate):
    manifest, parent = synthetic_contract
    mutate(manifest)
    with pytest.raises(ValueError):
        audit.audit_contract(manifest, parent)


def test_phase_split_reproduces_existing_ninety_five_percent_policy():
    row = dict(config=dict(max_iterations=10_000_000, time_limit_seconds=238.0,
                           relay_direct_warmup_fraction=.95, relay_enabled=True, seed=42))
    warm, relay = [audit._phase_configuration(row, phase) for phase in ("direct_warmup", "relay_search")]
    assert warm["max_iterations"] == 9_500_000
    assert relay["max_iterations"] == 500_000
    assert warm["relay_enabled"] is False and relay["relay_enabled"] is True
    assert warm["time_limit_seconds"] + relay["time_limit_seconds"] == 238.0


def test_scalar_trajectory_accepts_more_late_tasks_when_fixed_weighted_j_improves():
    manifest = dict(weights=[.5, .25, .25], scales=[200, 4464.183540734935, 775.7731679286865])
    before = dict(late_count=1, total_lateness_min=1000.0, distance_km=100.0)
    after = dict(late_count=2, total_lateness_min=10.0, distance_km=100.0)
    points = [[0, 0.0, *before.values()], [1, .01, *after.values()]]
    payload = dict(metadata=dict(best_trajectory=points, best_update_count=1,
                                initial_score=list(before.values()), time_to_best_seconds=.01))
    result = audit._trajectory(payload, 1, .02, before, after, manifest)
    assert result["strict_weighted_monotonicity"] is True
    assert audit._delta(before, after, manifest)["weighted_delta"] < 0


@pytest.mark.parametrize("point", [
    [1, .03, 0, 10.0, 100.0],  # Outside the measured search wall.
    [2, .01, 0, 10.0, 100.0],  # Beyond completed iterations.
    [1, .01, 2, 1000.0, 100.0],  # Worsens weighted objective.
    [1, .01, True, 10.0, 100.0],  # Bool is not an integral count.
])
def test_trajectory_rejects_impossible_time_iteration_and_objective(point):
    manifest = dict(weights=[.5, .25, .25], scales=[200, 4464.183540734935, 775.7731679286865])
    before = dict(late_count=1, total_lateness_min=1000.0, distance_km=100.0)
    after = dict(zip(audit.FIELDS, point[2:], strict=True))
    payload = dict(metadata=dict(best_trajectory=[[0, 0.0, *before.values()], point], best_update_count=1,
                                initial_score=list(before.values()), time_to_best_seconds=point[1]))
    with pytest.raises(ValueError):
        audit._trajectory(payload, 1, .02, before, after, manifest)


@pytest.mark.parametrize("completed", [0, 1, 11])
def test_partial_run_inventory_cannot_claim_passed_proof(synthetic_contract, tmp_path, monkeypatch, completed):
    manifest, parent = synthetic_contract
    root, output = tmp_path / "repo", tmp_path / "batch"
    root.mkdir()
    output.mkdir()
    parent_directory = root / "parent"
    parent_directory.mkdir()
    def write(path, value):
        path.write_text(audit.json.dumps(value))
    write(parent_directory / "manifest.json", parent)
    write(parent_directory / "runs.json", [])
    write(parent_directory / "audit.json", dict(passed=True,
        manifest_sha256=audit.sha(parent_directory / "manifest.json"),
        runs_sha256=audit.sha(parent_directory / "runs.json")))
    (root / "input.csv").write_text("synthetic")
    (root / "problem.docx").write_text("synthetic problem")
    parent["input_sha256"] = manifest["input_sha256"] = audit.sha(root / "input.csv")
    parent["problem_sha256"] = manifest["problem_sha256"] = audit.sha(root / "problem.docx")
    write(parent_directory / "manifest.json", parent)
    write(parent_directory / "audit.json", dict(passed=True,
        manifest_sha256=audit.sha(parent_directory / "manifest.json"),
        runs_sha256=audit.sha(parent_directory / "runs.json")))
    manifest.update(parent_manifest_path="parent/manifest.json",
                    parent_manifest_sha256=audit.sha(parent_directory / "manifest.json"),
                    parent_audit_sha256=audit.sha(parent_directory / "audit.json"))
    write(output / "manifest.json", manifest)
    write(output / "runs.json", manifest["schedule"][:completed])
    monkeypatch.setattr(audit, "ROOT", root)
    monkeypatch.setattr(audit, "load_official_csv", lambda path: {1: ((0.0, 0.0), (1.0, 0.0), 20.0)})
    def must_not_run(*args):
        raise AssertionError("Incomplete batch must fail before expensive source/route audit")
    monkeypatch.setattr(audit, "audit_sources", must_not_run)
    with pytest.raises(ValueError, match="All 12 prespecified runs must be complete"):
        audit.audit_results(output)
