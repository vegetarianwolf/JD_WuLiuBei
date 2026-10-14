"""Independent standard-library structure and DAG audit for relay ablation.

This module deliberately never imports the production data loader, model,
validator, relay scheduler or weighted adapter.  Route clocks are rebuilt from
CSV coordinates and the batch's frozen transport-leg registry.  Intermediate
score-only trajectory entries are telemetry, not independently scored routes.
"""
from __future__ import annotations

import argparse
import ast
from collections import deque
from datetime import datetime
from itertools import combinations
import json
import math
from pathlib import Path
import subprocess
from zipfile import ZipFile
from zoneinfo import ZoneInfo

try:
    from .audit_weighted_direct_pair import (
        ROOT, FIELDS, components, digest, finite, load_official_csv, objective_key,
        read, require, same, sha, within,
    )
except ImportError:
    from audit_weighted_direct_pair import (
        ROOT, FIELDS, components, digest, finite, load_official_csv, objective_key,
        read, require, same, sha, within,
    )


DEFAULT_OUTPUT = ROOT / "algorithm/results/weighted_relay_ablation_20261014"
EPSILON = 1e-9
SEEDS = [2026101471, 2026101472, 2026101473]
ORDER = ["pure_direct", "direct_relay", "direct_stations", "direct_relay_stations"]
NETWORK = dict(relay_count=4, relay_candidates_per_task=2, relay_detour_ratio=2.,
               relay_location_seed=42, relay_location_method="weighted_kmedoids")
PERIODIC = dict(interval=120, max_candidates=16, anchors_per_route=4, pair_budget_seconds=.008)
EXECUTION = {"run_weighted_relay_ablation.py", "run_weighted_direct_pair.py", "weighted_direct_objective.py",
             "weighted_relay_objective.py", "weighted_periodic_exchange.py", "run_scenario_comparison.py"}
ADAPTER_COUNTS = {
    "alns.py": dict(attribute_keys=6, regret_keys=2, regret_output_keys=2, regret_missing_keys=2,
                    raw_insertion_keys=0, acceptance=1, staged_trajectory_keys=1),
    "search.py": dict(attribute_keys=2, regret_keys=0, regret_output_keys=0, regret_missing_keys=0,
                      raw_insertion_keys=2, acceptance=0, staged_trajectory_keys=0),
}


def _integer(value, label, *, minimum=1):
    require(type(value) is int and value >= minimum, label + " must be an integer")
    return value


def _point(value, label):
    require(isinstance(value, (list, tuple)) and len(value) == 2, label + " must contain x/y")
    return tuple(finite(x, label) for x in value)


def _geometry(tasks, topology):
    """Validate the frozen registry independently of the production model."""
    require(isinstance(tasks, dict) and tasks, "Tasks must be a nonempty dictionary")
    for task_id, task in tasks.items():
        _integer(task_id, "task id")
        require(isinstance(task, (list, tuple)) and len(task) == 3, "Invalid task geometry")
        _point(task[0], "pickup")
        _point(task[1], "delivery")
        require(finite(task[2], "deadline") >= 0, "Negative deadline")
    require(isinstance(topology, dict), "Missing frozen topology")
    drone_count = _integer(topology.get("drone_count"), "drone count")
    capacity = _integer(topology.get("capacity"), "capacity")
    maximum = _integer(topology.get("max_tasks_per_drone"), "K")
    speed = finite(topology.get("speed_km_per_min"), "speed")
    require(speed > 0, "Nonpositive speed")
    homes = topology.get("home_points")
    require(isinstance(homes, list) and len(homes) == drone_count, "Home count differs from fleet")
    homes = [_point(value, "home") for value in homes]
    station_rows = topology.get("stations")
    require(isinstance(station_rows, list), "Missing frozen station coordinates")
    stations = {}
    for row in station_rows:
        require(isinstance(row, (list, tuple)) and len(row) == 3, "Invalid station row")
        station_id = _integer(row[0], "station id")
        require(station_id not in stations, "Duplicate station id")
        stations[station_id] = _point(row[1:], "station")
    registry_rows = topology.get("legs")
    require(isinstance(registry_rows, list), "Missing frozen transport-leg registry")
    legs, pairs = {}, {}
    for row in registry_rows:
        require(isinstance(row, dict) and set(row) == {"id", "task_id", "kind", "relay_id"},
                "Invalid frozen transport-leg fields")
        identifier = _integer(row["id"], "leg id")
        task_id = _integer(row["task_id"], "leg task")
        station_id = _integer(row["relay_id"], "leg station")
        kind = row["kind"]
        require(identifier not in tasks and identifier not in legs, "Duplicate or colliding transport-leg id")
        require(task_id in tasks and station_id in stations, "Unknown task or station in registry")
        require(kind in ("RELAY_IN", "RELAY_OUT"), "Unknown transport-leg kind")
        pair = pairs.setdefault((task_id, station_id), {})
        require(kind not in pair, "Duplicate transport-leg kind for task/station")
        pair[kind] = identifier
        legs[identifier] = row
    require(all(set(pair) == {"RELAY_IN", "RELAY_OUT"} for pair in pairs.values()),
            "Registry contains an unpaired transport leg")
    candidates = topology.get("task_relay_candidates")
    if candidates is not None:
        require(isinstance(candidates, dict), "Invalid task relay candidates")
        require({int(task) for task in candidates} == set(tasks), "Candidate task coverage differs")
        require(all(str(int(task)) == str(task) for task in candidates), "Noncanonical candidate task id")
        for task, selected in candidates.items():
            require(isinstance(selected, list) and len(selected) == len(set(selected)), "Duplicate candidate station")
            for station_id in selected:
                _integer(station_id, "candidate station")
                require((int(task), station_id) in pairs, "Candidate station lacks paired transport legs")
        require(set(pairs) == {(int(task), station) for task, values in candidates.items() for station in values},
                "Registry and candidate stations disagree")
    return homes, stations, legs, drone_count, capacity, maximum, speed


def reference_schedule(tasks, routes, topology):
    """Check complete service and rebuild asynchronous earliest event clocks.

    ``tasks`` has the independent CSV loader's format
    ``{id: (pickup_xy, delivery_xy, deadline)}``.  Frozen topology fields are
    ``stations``, ``legs``, ``home_points``, ``drone_count``, ``capacity``,
    ``max_tasks_per_drone`` and ``speed_km_per_min``.  All output is JSON-native.
    K counts final deliveries; inbound transfer legs never add a K slot.
    """
    homes, stations, registry, count, capacity, maximum, speed = _geometry(tasks, topology)
    require(isinstance(routes, (list, tuple)) and len(routes) == count, "Route count differs from fleet")
    successors, indegree, earliest, travel = {}, {}, {}, {}
    task_events, signed_events, route_distances, route_loads = {}, set(), [], []
    final_by_route = [set() for _ in routes]
    route_leg_counts = []
    for route_index, route in enumerate(routes):
        require(isinstance(route, (list, tuple)), "Encoded route is not a sequence")
        point, distance, onboard, loads, positive_count = homes[route_index], 0.0, set(), [], 0
        for position, visit in enumerate(route):
            require(type(visit) is int and visit != 0, "Unknown or noninteger visit")
            require(visit not in signed_events, "Duplicate service event")
            signed_events.add(visit)
            identifier = abs(visit)
            leg = registry.get(identifier)
            require(leg is not None or identifier in tasks, "Unknown task or transport leg")
            if leg is None:
                task_id, mode, station_id = identifier, "DIRECT", None
                role = "pickup" if visit > 0 else "delivery"
                target = tasks[task_id][0 if visit > 0 else 1]
            else:
                task_id, mode, station_id = leg["task_id"], "RELAY", leg["relay_id"]
                if leg["kind"] == "RELAY_IN":
                    role = "pickup" if visit > 0 else "drop"
                    target = tasks[task_id][0] if visit > 0 else stations[station_id]
                else:
                    role = "relay_pickup" if visit > 0 else "delivery"
                    target = stations[station_id] if visit > 0 else tasks[task_id][1]
            record = task_events.setdefault(task_id, {"mode": mode, "station": station_id, "events": {}})
            require(record["mode"] == mode, "Task mixes DIRECT and RELAY service")
            require(record["station"] == station_id, "Relay legs use different stations")
            require(role not in record["events"], "Task has duplicate service role")
            node = (route_index, position)
            record["events"][role] = (node, identifier)
            if visit > 0:
                require(identifier not in onboard, "Duplicate pickup")
                onboard.add(identifier)
                positive_count += 1
                require(len(onboard) <= capacity, "Route exceeds package capacity")
            else:
                require(identifier in onboard, "Delivery or drop precedes pickup or crosses drones")
                onboard.remove(identifier)
                if role == "delivery":
                    final_by_route[route_index].add(task_id)
            loads.append(len(onboard))
            length = math.hypot(target[0] - point[0], target[1] - point[1])
            distance += length
            point = target
            successors[node], indegree[node] = {}, 0
            travel[node] = length / speed
            earliest[node] = travel[node] if position == 0 else 0.0
        require(not onboard, "Route ends with undelivered load")
        require(len(final_by_route[route_index]) <= maximum, "Final-delivery responsibility exceeds K")
        route_distances.append(distance)
        route_loads.append(loads)
        route_leg_counts.append(positive_count)
    require(set(task_events) == set(tasks), "Every original task must be served exactly once")

    def edge(source, target, weight):
        if target not in successors[source]:
            successors[source][target] = weight
            indegree[target] += 1
        else:
            successors[source][target] = max(successors[source][target], weight)

    for route_index, route in enumerate(routes):
        for position in range(1, len(route)):
            edge((route_index, position - 1), (route_index, position), travel[route_index, position])
    for record in task_events.values():
        expected = {"pickup", "delivery"} if record["mode"] == "DIRECT" else {
            "pickup", "drop", "relay_pickup", "delivery"}
        require(set(record["events"]) == expected, "Incomplete DIRECT or RELAY task plan")
        if record["mode"] == "RELAY":
            edge(record["events"]["drop"][0], record["events"]["relay_pickup"][0], 0.0)
    pending = deque(node for node, degree in indegree.items() if degree == 0)
    processed = 0
    while pending:
        node = pending.popleft()
        processed += 1
        for target, weight in successors[node].items():
            earliest[target] = max(earliest[target], earliest[node] + weight)
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    require(processed == len(indegree), "Relay handoff dependency cycle")
    clocks = {task: earliest[record["events"]["delivery"][0]] for task, record in task_events.items()}
    lateness = {task: max(0.0, clocks[task] - tasks[task][2]) for task in sorted(tasks)}
    positive_lateness = [value for value in lateness.values() if value > EPSILON]
    route_records = []
    for index, route in enumerate(routes):
        values = [lateness[task] for task in sorted(final_by_route[index])]
        late = sum(value > EPSILON for value in values)
        completed = len(final_by_route[index])
        route_records.append(dict(drone_id=index + 1, distance_km=route_distances[index],
            completion_time_min=earliest[index, len(route) - 1] if route else 0.0,
            task_count=completed, transport_leg_count=route_leg_counts[index], late_count=late,
            total_lateness_min=sum(values), on_time_task_count=completed - late,
            on_time_rate=(completed - late) / completed if completed else 1.0,
            arrival_times_min=[earliest[index, position] for position in range(len(route))],
            load_after_event=route_loads[index]))
    handoffs = []
    for task_id, record in sorted(task_events.items()):
        if record["mode"] == "DIRECT":
            continue
        drop, in_leg = record["events"]["drop"]
        pickup, out_leg = record["events"]["relay_pickup"]
        arrival = travel[pickup] + (earliest[pickup[0], pickup[1] - 1] if pickup[1] else 0.0)
        handoffs.append(dict(task_id=task_id, relay_id=record["station"],
            in_leg=in_leg, out_leg=out_leg, inbound_drone_id=drop[0] + 1,
            outbound_drone_id=pickup[0] + 1, cross_uav=drop[0] != pickup[0],
            drop_time_min=earliest[drop], aircraft_arrival_min=arrival,
            pickup_time_min=earliest[pickup], aircraft_waiting_min=max(0.0, earliest[pickup] - arrival),
            package_cache_min=max(0.0, earliest[pickup] - earliest[drop])))
    waits = [handoff["aircraft_waiting_min"] for handoff in handoffs]
    relay_count = len(handoffs)
    cross = sum(handoff["cross_uav"] for handoff in handoffs)
    station_usage = [{"relay_id": station_id,
        "task_count": sum(handoff["relay_id"] == station_id for handoff in handoffs),
        "cross_uav_handoff_count": sum(handoff["relay_id"] == station_id and handoff["cross_uav"] for handoff in handoffs),
        "aircraft_waiting_min": sum(handoff["aircraft_waiting_min"] for handoff in handoffs if handoff["relay_id"] == station_id),
        "package_cache_min": sum(handoff["package_cache_min"] for handoff in handoffs if handoff["relay_id"] == station_id)}
        for station_id in sorted(stations)]
    return dict(score=dict(late_count=len(positive_lateness), total_lateness_min=sum(positive_lateness),
                          distance_km=sum(route_distances)),
        delivery_times_min={str(task): clocks[task] for task in sorted(tasks)},
        event_times_min=[record["arrival_times_min"] for record in route_records],
        route_records=route_records, handoffs=handoffs, station_usage=station_usage,
        relay_statistics=dict(direct_task_count=len(tasks) - relay_count, relay_task_count=relay_count,
            cross_uav_handoff_count=cross, same_uav_relay_count=relay_count - cross,
            total_relay_waiting_time=sum(waits), max_relay_waiting_time=max(waits, default=0.0),
            average_relay_waiting_time=sum(waits) / relay_count if relay_count else 0.0,
            total_package_cache_time=sum(handoff["package_cache_min"] for handoff in handoffs)))


def direct_counterfactual(routes, topology):
    """Contract each relay plan onto its unchanged final-responsibility route.

    Remove both inbound events and replace each outbound signed leg by the
    original task's signed DIRECT event.  This preserves outbound event order,
    final responsibilities and its sign sequence, while removing inbound load.
    It is a fixed-structure counterfactual, never a reoptimized DIRECT solution.
    Call ``reference_schedule`` afterwards to independently establish legality.
    """
    require(isinstance(topology, dict) and isinstance(topology.get("legs"), list), "Missing frozen registry")
    registry = {}
    for leg in topology["legs"]:
        require(isinstance(leg, dict) and set(leg) == {"id", "task_id", "kind", "relay_id"}, "Invalid transport-leg fields")
        identifier = _integer(leg["id"], "leg id")
        _integer(leg["task_id"], "leg task")
        require(identifier not in registry and leg["kind"] in ("RELAY_IN", "RELAY_OUT"), "Invalid registry")
        registry[identifier] = leg
    require(isinstance(routes, (list, tuple)), "Encoded routes are not a sequence")
    result = []
    for route in routes:
        require(isinstance(route, (list, tuple)), "Encoded route is not a sequence")
        direct = []
        for visit in route:
            require(type(visit) is int and visit != 0, "Noninteger service event")
            leg = registry.get(abs(visit))
            if leg is None:
                direct.append(visit)
            elif leg["kind"] == "RELAY_OUT":
                direct.append(leg["task_id"] if visit > 0 else -leg["task_id"])
        result.append(direct)
    return result


def audit_payload(tasks, topology, payload):
    """Bind a saved canonical solution to every independently rebuilt event."""
    require(isinstance(payload, dict) and payload.get("valid") is True
            and payload.get("violations") == [], "Saved solution is not valid")
    problem = payload.get("problem")
    require(isinstance(problem, dict), "Missing saved problem")
    for field, expected in (("task_count", len(tasks)), ("drone_count", topology["drone_count"]),
        ("max_tasks_per_drone", topology["max_tasks_per_drone"]), ("capacity", topology["capacity"])):
        require(type(problem.get(field)) is int and problem[field] == expected, "Saved problem differs: " + field)
    same(problem.get("speed_km_per_min"), topology["speed_km_per_min"], "saved speed")
    require(problem.get("open_routes") is True, "Final return must not be counted")
    same(problem.get("service_time_min"), 0.0, "zero service time")
    require(problem.get("depot_km") == [0.0, 0.0], "Saved depot differs from original origin")
    if "drone_homes" in topology:
        expected_homes = ["origin" if home is None else f"station_{home}" for home in topology["drone_homes"]]
        require(problem.get("drone_homes") == expected_homes, "Saved per-drone home labels differ")
    saved_routes = payload.get("routes")
    require(isinstance(saved_routes, list), "Missing saved routes")
    routes = [record.get("encoded_visits") for record in saved_routes if isinstance(record, dict)]
    require(len(routes) == len(saved_routes), "Invalid saved route")
    reference = reference_schedule(tasks, routes, topology)
    require(isinstance(payload.get("score"), dict) and set(payload["score"]) == set(FIELDS), "Saved score fields differ")
    for field in FIELDS:
        same(payload["score"][field], reference["score"][field], "independent " + field)
    require(type(payload["score"]["late_count"]) is int, "Late count is not an integer")
    clocks = payload.get("delivery_times_min")
    require(isinstance(clocks, dict) and set(clocks) == set(reference["delivery_times_min"]), "Saved delivery coverage differs")
    for task_id, expected in reference["delivery_times_min"].items():
        same(clocks[task_id], expected, "independent task delivery")
    _, stations, registry, *_ = _geometry(tasks, topology)
    for route_index, (saved, independent) in enumerate(zip(saved_routes, reference["route_records"], strict=True)):
        for field in ("drone_id", "task_count", "transport_leg_count", "late_count", "on_time_task_count"):
            require(type(saved.get(field)) is int and saved[field] == independent[field], "Saved route counter differs: " + field)
        for field in ("distance_km", "completion_time_min", "total_lateness_min", "on_time_rate"):
            same(saved.get(field), independent[field], "independent route " + field)
        visits = saved.get("visits")
        require(isinstance(visits, list) and len(visits) == len(routes[route_index]), "Saved event count differs")
        relay_drop_count = relay_pickup_count = 0
        for position, (visit, saved_event) in enumerate(zip(routes[route_index], visits, strict=True)):
            require(isinstance(saved_event, dict), "Invalid saved event")
            identifier = abs(visit)
            leg = registry.get(identifier)
            if leg is None:
                task_id, leg_id, relay_id = identifier, None, None
                action = "pickup" if visit > 0 else "delivery"
                target = tasks[identifier][0 if visit > 0 else 1]
            else:
                task_id, leg_id, relay_id = leg["task_id"], identifier, leg["relay_id"]
                if leg["kind"] == "RELAY_IN":
                    action = "pickup" if visit > 0 else "relay_drop"
                    target = tasks[task_id][0] if visit > 0 else stations[relay_id]
                    relay_drop_count += int(visit < 0)
                else:
                    action = "relay_pickup" if visit > 0 else "delivery"
                    target = stations[relay_id] if visit > 0 else tasks[task_id][1]
                    relay_pickup_count += int(visit > 0)
            require(saved_event.get("task_id") == task_id and type(saved_event["task_id"]) is int
                    and saved_event.get("leg_id") == leg_id and saved_event.get("relay_id") == relay_id
                    and saved_event.get("action") == action and saved_event.get("kind") == action.upper(),
                    "Saved event semantics differ from frozen registry")
            require((leg_id is None or type(saved_event["leg_id"]) is int)
                    and (relay_id is None or type(saved_event["relay_id"]) is int), "Saved leg/station id type differs")
            coordinates = _point(saved_event.get("location_km"), "saved event coordinates")
            for actual, expected in zip(coordinates, target, strict=True):
                same(actual, expected, "independent event coordinate")
            same(saved_event.get("arrival_time_min"), reference["event_times_min"][route_index][position], "independent event time")
        require(saved.get("relay_drop_count") == relay_drop_count
                and saved.get("relay_pickup_count") == relay_pickup_count, "Saved route handoff counts differ")
    relay = payload.get("relay")
    require(isinstance(relay, dict), "Missing saved relay statistics")
    for field in ("direct_task_count", "relay_task_count", "cross_uav_handoff_count", "same_uav_relay_count"):
        require(type(relay.get(field)) is int and relay[field] == reference["relay_statistics"][field],
                "Saved relay count differs: " + field)
    for field in ("total_relay_waiting_time", "max_relay_waiting_time", "average_relay_waiting_time"):
        same(relay.get(field), reference["relay_statistics"][field], "independent " + field)
    require(relay.get("relay_count") == len(stations)
            and relay.get("relay_coordinates") == topology["stations"], "Saved fixed stations differ")
    same(relay.get("relay_share"), len(reference["handoffs"]) / len(tasks), "independent relay share")
    if "relay_allowed" in topology:
        candidates = topology.get("task_relay_candidates", {}) if topology["relay_allowed"] else {}
        coverage = sum(bool(values) for values in candidates.values()) / len(candidates) if candidates else 0.0
        same(relay.get("relay_candidate_coverage"), coverage, "frozen candidate coverage")
    station_usage = relay.get("relay_usage_per_station")
    require(isinstance(station_usage, list) and len(station_usage) == len(stations), "Saved station usage count differs")
    for saved, independent in zip(station_usage, reference["station_usage"], strict=True):
        station_id = independent["relay_id"]
        require(saved.get("station_id") == station_id and all(saved.get(field) == independent["task_count"]
                for field in ("drop_count", "pickup_count", "task_count")), "Saved station task usage differs")
        same(saved.get("x"), stations[station_id][0], "station usage x")
        same(saved.get("y"), stations[station_id][1], "station usage y")
        expected_average = independent["aircraft_waiting_min"] / independent["task_count"] if independent["task_count"] else 0.0
        same(saved.get("average_wait"), expected_average, "independent station average wait")
    per_drone = relay.get("per_drone")
    require(isinstance(per_drone, list) and len(per_drone) == len(saved_routes), "Saved per-drone relay usage differs")
    for saved, canonical in zip(per_drone, saved_routes, strict=True):
        require(isinstance(saved, dict), "Invalid per-drone relay usage")
        for field, route_field in (("drone_id", "drone_id"), ("original_task_count", "task_count"),
                                  ("transport_leg_count", "transport_leg_count"), ("relay_drop_count", "relay_drop_count"),
                                  ("relay_pickup_count", "relay_pickup_count")):
            require(type(saved.get(field)) is int and saved[field] == canonical[route_field], "Per-drone relay usage differs: " + field)
        same(saved.get("distance_km"), canonical["distance_km"], "independent per-drone relay distance")
    plans = payload.get("task_plans")
    require(isinstance(plans, dict) and set(plans) == {str(task) for task in tasks}, "Saved task plan coverage differs")
    handoffs = {handoff["task_id"]: handoff for handoff in reference["handoffs"]}
    for task_id in tasks:
        handoff = handoffs.get(task_id)
        expected = (dict(mode="DIRECT", relay_id=None, leg_ids=[]) if handoff is None else
                    dict(mode="RELAY", relay_id=handoff["relay_id"], leg_ids=[handoff["in_leg"], handoff["out_leg"]]))
        require(plans[str(task_id)] == expected, "Saved task plan differs from actual signed visits")
    same(payload.get("on_time_rate"), (len(tasks) - reference["score"]["late_count"]) / len(tasks), "independent on-time rate")
    maximum_lateness = max(max(0.0, at - tasks[int(task)][2]) for task, at in reference["delivery_times_min"].items())
    same(payload.get("max_lateness_min"), maximum_lateness, "independent maximum lateness")
    same(payload.get("max_route_distance_km"), max(record["distance_km"] for record in reference["route_records"]), "independent maximum route distance")
    same(payload.get("max_route_completion_min"), max(record["completion_time_min"] for record in reference["route_records"]), "independent maximum completion time")
    return reference


def _physical(value):
    require(isinstance(value, dict) and set(value) == set(FIELDS), "Physical score fields differ")
    _integer(value["late_count"], "late count", minimum=0)
    require(value["late_count"] <= 200, "More late tasks than the official instance")
    require(all(finite(value[field], field) >= 0 for field in FIELDS), "Negative physical score")
    return tuple(value[field] for field in FIELDS)


def _weighted(value, manifest):
    return sum(components(_physical(value), manifest))


def _delta(before, after, manifest):
    return dict(weighted_delta=_weighted(after, manifest) - _weighted(before, manifest),
                physical_delta={field: after[field] - before[field] for field in FIELDS},
                on_time_delta=before["late_count"] - after["late_count"])


def audit_contract(manifest, parent):
    require(isinstance(manifest, dict) and manifest.get("schema_version") == 1
            and type(manifest["schema_version"]) is int and manifest.get("kind") == "weighted_relay_ablation",
            "Unsupported relay ablation schema")
    require(manifest.get("seeds") == SEEDS and all(type(seed) is int for seed in manifest["seeds"])
            and manifest.get("order") == ORDER, "Prespecified seeds or scenario order changed")
    require(type(manifest.get("budget_seconds")) is int and manifest["budget_seconds"] == 240
            and type(manifest.get("finalization_reserve_seconds")) in (int, float)
            and manifest["finalization_reserve_seconds"] == 1.0, "Equal 240-second budget changed")
    require(manifest.get("periodic_parameters") == PERIODIC and manifest.get("network_parameters") == NETWORK,
            "Fixed periodic or network parameters changed")
    for key in ("weights", "scales", "formula", "input_path", "input_sha256", "problem_path", "problem_sha256"):
        require(manifest.get(key) == parent.get(key), "Current weighted parent contract changed: " + key)
    require(manifest["weights"] == [.5, .25, .25]
            and manifest["scales"] == [200, 4464.183540734935, 775.7731679286865], "Frozen J weights or scales changed")
    expected_schedule = [dict(run_id=f"{name}__seed_{seed}", scenario=name, seed=seed)
                         for index, seed in enumerate(SEEDS) for name in ORDER[index:] + ORDER[:index]]
    require(manifest.get("schedule") == expected_schedule, "Prespecified rotated 12-cell schedule changed")
    require(set(manifest.get("scenarios", {})) == set(ORDER), "Scenario inventory changed")
    parent_config = parent["variants"]["weighted_periodic"]["config"]
    for name in ORDER:
        case = manifest["scenarios"][name]
        relay, homes = "relay" in name, "stations" in name
        require(case.get("relay") is relay and case.get("homes") == ("stations" if homes else "origin"),
                "Case mechanism or home group changed")
        expected = {**parent_config, "seed": SEEDS[0], "relay_enabled": relay,
                    "enable_home_seed": homes, "enable_home_bias": homes}
        require(case.get("config") == expected and all(type(case["config"][key]) is type(value)
                for key, value in expected.items()), "Case differs from current weighted periodic configuration")
        require(digest(case.get("initial_routes")) == case.get("initial_routes_sha256"), "Declared initial routes changed")
    for direct, relay in ((ORDER[0], ORDER[1]), (ORDER[2], ORDER[3])):
        require(manifest["scenarios"][direct]["initial_routes"] == manifest["scenarios"][relay]["initial_routes"],
                "Paired cases must share identical DIRECT initial routes")
    protocol = manifest.get("protocol", {})
    require(protocol.get("planned_unique_runs") == 12 and protocol.get("no_result_based_stopping") is True,
            "No result-based stopping contract changed")


def audit_sources(output, manifest):
    try:
        from .weighted_relay_objective import restore
    except ImportError:
        from weighted_relay_objective import restore
    require(manifest.get("adapter_changes") == ADAPTER_COUNTS, "Counted weighted source transformations changed")
    execution = manifest.get("execution_sources")
    require(isinstance(execution, dict) and set(execution) == EXECUTION, "Execution source inventory changed")
    for name, artifact in execution.items():
        require(sha(within(output, artifact["path"])) == artifact["sha256"]
                == sha(ROOT / "algorithm/experiments" / name), "Frozen execution source changed: " + name)
    source = manifest.get("source_archive")
    require(isinstance(source, dict) and sha(within(output, source["path"])) == source["sha256"], "Solver archive changed")
    with ZipFile(within(output, source["path"])) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)) and all(name.startswith("uav_dispatch/") and name.endswith(".py")
                and len(Path(name).parts) == 2 and ".." not in Path(name).parts for name in names), "Unsafe solver archive")
        files = {Path(name).name: archive.read(name) for name in names}
    production = manifest.get("production_files_sha256")
    require(isinstance(production, dict) and set(production) == set(files), "Production source inventory differs")
    commit = manifest.get("source_commit")
    require(isinstance(commit, str) and len(commit) == 40 and all(ch in "0123456789abcdef" for ch in commit), "Invalid source commit")
    counts = {}
    for name, frozen in files.items():
        path = ROOT / "algorithm/src/uav_dispatch" / name
        require(sha(path) == production[name], "Current production source differs: " + name)
        result = subprocess.run(["git", "show", f"{commit}:algorithm/src/uav_dispatch/{name}"],
                                cwd=ROOT, check=True, capture_output=True, timeout=15)
        require(result.stdout == path.read_bytes(), "Production source differs from declared commit: " + name)
        if name in ADAPTER_COUNTS:
            restored, transformation_counts = restore(frozen.decode(), name)
            require(transformation_counts == ADAPTER_COUNTS[name]
                    and ast.dump(ast.parse(restored)) == ast.dump(ast.parse(path.read_text())),
                    "Weighted solver contains an unapproved source change: " + name)
            counts[name] = transformation_counts
        else:
            require(frozen == path.read_bytes(), "Solver archive contains unrelated changes: " + name)
    return dict(passed=True, all_arms_identical_solver_source=True, exact_counted_weighted_edits=counts,
                source_sha256=source["sha256"], execution_source_count=len(execution))


def _topologies(output, manifest, tasks):
    topologies, initials = {}, {}
    for name, case in manifest["scenarios"].items():
        path = within(output, case["topology_path"])
        topology = read(path)
        require(sha(path) == case["topology_sha256"] and digest(topology) == case["topology_digest"], "Frozen topology changed")
        require(topology.get("drone_count") == 8 and topology.get("max_tasks_per_drone") == 25
                and topology.get("capacity") == 2 and topology.get("speed_km_per_min") == .9,
                "Official fleet constraints changed")
        require(topology.get("relay_allowed") is case["relay"] and topology.get("network_parameters") == NETWORK
                and len(topology.get("stations", [])) == 4, "Frozen network scenario changed")
        _geometry(tasks, topology)
        homes = topology.get("drone_homes")
        require(isinstance(homes, list) and len(homes) == 8, "Frozen home identity count differs")
        stations = {row[0]: row[1:] for row in topology["stations"]}
        for home, point in zip(homes, topology["home_points"], strict=True):
            require(home is None or (type(home) is int and home in stations), "Unknown frozen home station")
            require(point == ([0.0, 0.0] if home is None else stations[home]), "Frozen home points differ from station identities")
        if case["homes"] == "origin":
            require(homes == [None] * 8 and topology.get("deployment") is None, "Origin case contains predeployment")
        else:
            require(isinstance(topology.get("deployment"), dict), "Station case lacks frozen deployment")
        require(all(abs(visit) in tasks for route in case["initial_routes"] for visit in route), "Common construction must be DIRECT")
        initial = reference_schedule(tasks, case["initial_routes"], topology)
        for field in FIELDS:
            same(case["initial_score"][field], initial["score"][field], "independent initial " + field)
        topologies[name], initials[name] = topology, initial
    for direct, relay in ((ORDER[0], ORDER[1]), (ORDER[2], ORDER[3])):
        a, b = topologies[direct], topologies[relay]
        require({key: value for key, value in a.items() if key != "relay_allowed"}
                == {key: value for key, value in b.items() if key != "relay_allowed"},
                "Paired DIRECT/RELAY geometries or deployment differ")
    require(all(topologies[name][key] == topologies[ORDER[0]][key] for name in ORDER
                for key in ("stations", "legs", "task_relay_candidates")), "Network differs across the 2x2 arms")
    return topologies, initials


def _saved_payload(output, artifact, tasks, topology, manifest):
    path = within(output, artifact["solution_path"])
    require(sha(path) == artifact["solution_sha256"], "Saved solution artifact fingerprint changed")
    payload = read(path)
    require(payload.get("source") == manifest["input_path"], "Saved solution input source differs")
    problem = payload.get("problem", {})
    require(problem.get("objective_order") == ["weighted_total_score", *FIELDS]
            and problem.get("experiment_objective") == dict(kind="weighted", formula=manifest["formula"],
                weights=manifest["weights"], scales=manifest["scales"]), "Saved objective normalization differs")
    reference = audit_payload(tasks, topology, payload)
    if "routes_sha256" in artifact:
        require(digest([record["encoded_visits"] for record in payload["routes"]]) == artifact["routes_sha256"],
                "Phase route digest differs")
    if "score" in artifact:
        require(set(artifact["score"]) == set(FIELDS), "Phase physical score fields differ")
        for field in FIELDS:
            same(artifact["score"][field], reference["score"][field], "independent phase " + field)
        same(artifact["weighted_score"], _weighted(reference["score"], manifest), "independent phase J")
    return payload, reference


def _trajectory(payload, iterations, seconds, initial_score, final_score, manifest):
    metadata = payload.get("metadata")
    require(isinstance(metadata, dict), "Missing search metadata")
    points = metadata.get("best_trajectory")
    require(isinstance(points, list) and points, "Missing initial-to-final trajectory telemetry")
    previous_key, previous_iteration, previous_seconds = None, -1, -1.0
    for point in points:
        require(isinstance(point, list) and len(point) == 5, "Malformed best-trajectory point")
        iteration, clock, *physical = point
        require(type(iteration) is int and previous_iteration <= iteration <= iterations, "Trajectory iteration order differs")
        require(previous_seconds <= finite(clock, "trajectory clock") <= seconds + 1e-7, "Trajectory exceeds search timing")
        value = dict(zip(FIELDS, physical, strict=True))
        _physical(value)
        key = objective_key(physical, manifest, "weighted")
        require(previous_key is None or key < previous_key, "Trajectory is not strictly improving in fixed J")
        previous_key, previous_iteration, previous_seconds = key, iteration, clock
    require(points[0][0] == 0 and metadata.get("best_update_count") == len(points) - 1,
            "Trajectory initial/count metadata differs")
    for position, expected in ((0, initial_score), (-1, final_score)):
        for field, value in zip(FIELDS, points[position][2:], strict=True):
            same(value, expected[field], "trajectory boundary " + field)
    initial_metadata = metadata.get("initial_score")
    require(isinstance(initial_metadata, list) and len(initial_metadata) == 3, "Missing metadata initial score")
    for field, value in zip(FIELDS, initial_metadata, strict=True):
        same(value, initial_score[field], "metadata initial " + field)
    same(metadata.get("time_to_best_seconds"), points[-1][1], "metadata time to best")
    return dict(checkpoint_count=len(points), strict_weighted_monotonicity=True,
                complete_boundary_routes_independently_scored=True, intermediate_checkpoint_routes_available=False)


def _phase_configuration(row, phase_name):
    config = dict(row["config"])
    if phase_name == "direct_search":
        return config
    warm_iterations = min(config["max_iterations"] - 1,
                          max(1, round(config["max_iterations"] * config["relay_direct_warmup_fraction"])))
    warm_seconds = config["time_limit_seconds"] * config["relay_direct_warmup_fraction"]
    if phase_name == "direct_warmup":
        return {**config, "relay_enabled": False, "max_iterations": warm_iterations, "time_limit_seconds": warm_seconds}
    require(phase_name == "relay_search", "Unknown solver phase")
    return {**config, "relay_enabled": True, "max_iterations": config["max_iterations"] - warm_iterations,
            "time_limit_seconds": config["time_limit_seconds"] - warm_seconds}


def _periodic_statistics(row, payload, direct_iterations, manifest):
    stats = row.get("periodic_task_exchange")
    require(stats == payload["metadata"].get("periodic_task_exchange"), "Periodic row and final metadata differ")
    fields = {"repair_calls", "passes", "probes", "canonical_checks", "improvements", "runtime_seconds", "events"}
    require(isinstance(stats, dict) and set(stats) == fields, "Periodic statistics fields differ")
    for field in fields - {"runtime_seconds", "events"}:
        _integer(stats[field], "periodic " + field, minimum=0)
    require(stats["repair_calls"] == row["iterations"] and stats["passes"] == direct_iterations // PERIODIC["interval"],
            "Periodic work must occur only at completed DIRECT-phase repair intervals")
    events = stats["events"]
    require(isinstance(events, list) and len(events) == stats["passes"], "Periodic event inventory differs")
    pairs = list(combinations(range(8), 2))
    totals, runtime_total = dict(probes=0, canonical_checks=0, improvements=0), 0.0
    for index, event in enumerate(events):
        require(isinstance(event, dict) and event.get("repair_call") == (index + 1) * PERIODIC["interval"]
                and type(event["repair_call"]) is int and event.get("route_pair") == list(pairs[index % len(pairs)]),
                "Periodic interval or route pair rotation differs")
        for field, upper in (("ranking_probes", 200), ("shortlisted_candidates", 16), ("probes", 16),
                             ("canonical_checks", 17), ("improvements", 16), ("same_route_skips", 16)):
            require(type(event.get(field)) is int and 0 <= event[field] <= upper, "Periodic bound exceeded: " + field)
        require(event["shortlisted_candidates"] <= event["ranking_probes"]
                and event["probes"] + event["same_route_skips"] <= event["shortlisted_candidates"]
                and event["improvements"] <= event["probes"]
                and event["canonical_checks"] <= event["probes"] + 1, "Inconsistent periodic work counters")
        require(type(event.get("deadline_reached")) is bool and "skip_reason" not in event, "Unexpected periodic skip")
        budget = finite(event.get("budget_seconds"), "periodic local budget")
        runtime = finite(event.get("runtime_seconds"), "periodic runtime")
        require(0 <= budget <= .008 + 1e-10 and runtime >= 0
                and (runtime <= budget + .002 or event["deadline_reached"]), "Unreported periodic timing overrun")
        stage_seconds = [finite(event.get(field), "periodic stage timing") for field in
                         ("precheck_seconds", "ranking_seconds", "probe_seconds", "canonical_seconds")]
        require(all(value >= 0 for value in stage_seconds) and sum(stage_seconds) <= runtime + 1e-8,
                "Periodic stage timings exceed local runtime")
        before, after = event.get("before_score"), event.get("after_score")
        if before is None or after is None:
            require(before is after is None and event["deadline_reached"]
                    and event["probes"] == event["improvements"] == event["ranking_probes"] == event["shortlisted_candidates"] == 0,
                    "Periodic checkpoint missing without an input-check timeout")
        else:
            before_key = objective_key(_physical(before), manifest, "weighted")
            after_key = objective_key(_physical(after), manifest, "weighted")
            require(after_key <= before_key and (after_key < before_key) is (event["improvements"] > 0)
                    and event["canonical_checks"] >= 1 + event["improvements"], "Periodic checkpoint lacks canonical J improvement")
        for field in totals:
            totals[field] += event[field]
        runtime_total += runtime
    require(all(stats[field] == value for field, value in totals.items()), "Periodic totals differ from recorded passes")
    same(stats["runtime_seconds"], runtime_total, "periodic runtime total")
    require(0 <= runtime_total <= row["search_seconds"], "Periodic work exceeds total search time")
    return dict(passed=True, repair_calls=stats["repair_calls"], passes=stats["passes"],
                improvements=stats["improvements"], runtime_seconds=runtime_total,
                active_relay_phase_skips_direct_periodic_exchange=True)


def audit_row(output, row, cell, manifest, tasks, topology, initial):
    require(isinstance(row, dict) and all(row.get(key) == value and type(row[key]) is type(value)
            for key, value in cell.items()), "Run does not match its prespecified cell")
    case = manifest["scenarios"][row["scenario"]]
    require(row.get("objective") == "weighted" and row.get("relay_enabled") is case["relay"]
            and row.get("home_group") == case["homes"] and row.get("label") == case["label"], "Run arm identity differs")
    require(type(row.get("budget_seconds")) is int and row["budget_seconds"] == 240
            and row.get("valid") is True and row.get("budget_compliant") is True, "Invalid or over-budget run")
    elapsed, construction, search = [finite(row.get(field), field) for field in
                                     ("end_to_end_seconds", "construction_seconds", "search_seconds")]
    require(0 <= construction <= elapsed and 0 <= search and construction + search <= elapsed <= 240,
            "Measured end-to-end timing is inconsistent")
    expected_config = {**case["config"], "seed": row["seed"], "time_limit_seconds": max(.001, 240 - construction - 1.0)}
    require(row.get("config") == expected_config and all(type(row["config"][key]) is type(value)
            for key, value in expected_config.items()), "Run configuration differs from frozen arm")
    require(row.get("source_sha256") == manifest["source_archive"]["sha256"]
            and row.get("topology_sha256") == case["topology_sha256"]
            and row.get("initial_routes_sha256") == case["initial_routes_sha256"], "Run source/topology/initial binding differs")
    _integer(row.get("iterations"), "completed iterations", minimum=0)
    require(row["iterations"] <= expected_config["max_iterations"], "Iteration cap exceeded")
    final, reference = _saved_payload(output, row, tasks, topology, manifest)
    routes = [record["encoded_visits"] for record in final["routes"]]
    if not case["relay"]:
        require(all(abs(visit) in tasks for route in routes for visit in route), "DIRECT arm used a relay leg")
    for field in FIELDS:
        same(row.get(field), reference["score"][field], "independent final row " + field)
    require(type(row["late_count"]) is int and type(row.get("delivered_count")) is int and row["delivered_count"] == 200
            and type(row.get("on_time_count")) is int
            and row["on_time_count"] == 200 - reference["score"]["late_count"], "Run completion counts differ")
    same(row.get("on_time_rate"), (200 - reference["score"]["late_count"]) / 200, "row on-time rate")
    expected_components = components(_physical(reference["score"]), manifest)
    require(isinstance(row.get("weighted_components"), list) and len(row["weighted_components"]) == 3, "Missing weighted components")
    for actual, expected in zip(row["weighted_components"], expected_components, strict=True):
        same(actual, expected, "independent weighted component")
    same(row.get("weighted_score"), sum(expected_components), "independent final J")
    require(row.get("relay_statistics") == final["relay"] and row.get("best_trajectory") == final["metadata"].get("best_trajectory"),
            "Final row telemetry differs from solution payload")
    same(final.get("iterations"), row["iterations"], "final iteration count")
    metadata = final["metadata"]
    for field in ("seed", "relay_enabled", "enable_home_seed", "enable_lazy_repair",
                  "enable_terminal_task_exchange"):
        require(metadata.get(field) == expected_config[field] and type(metadata[field]) is type(expected_config[field]),
                "Saved execution metadata differs: " + field)
    same(metadata.get("time_limit_seconds"), expected_config["time_limit_seconds"], "final configured search time")
    trajectory_audit = _trajectory(final, row["iterations"], search, initial["score"], reference["score"], manifest)
    artifacts = row.get("phase_artifacts")
    expected_phases = ["initial", "direct_warmup", "relay_search"] if case["relay"] else ["initial", "direct_search"]
    require(isinstance(artifacts, list) and [artifact.get("phase") for artifact in artifacts] == expected_phases
            and row.get("phases") == artifacts[1:], "Saved solver phase inventory differs")
    independently_scored_phases, phase_payloads = [], []
    previous_routes_digest, previous_score, previous_end = case["initial_routes_sha256"], initial["score"], construction
    phase_iterations, phase_runtime = 0, 0.0
    for artifact in artifacts:
        saved, independent = _saved_payload(output, artifact, tasks, topology, manifest)
        phase_name = artifact["phase"]
        if phase_name == "initial":
            require(artifact.get("config") is None and artifact["routes_sha256"] == case["initial_routes_sha256"],
                    "Initial payload is not the common construction")
        else:
            phase_config = _phase_configuration(row, phase_name)
            require(artifact.get("config") == phase_config and artifact.get("input_routes_sha256") == previous_routes_digest,
                    "Phase configuration or incumbent inheritance differs")
            begin, end, wall, runtime = [finite(artifact.get(field), field) for field in
                                       ("start_offset_seconds", "end_offset_seconds", "solve_wall_seconds", "runtime_seconds")]
            require(previous_end <= begin <= end <= elapsed and 0 <= runtime <= wall + .01
                    and 0 <= wall <= end - begin + 1e-8, "Phase measured timing is inconsistent")
            _integer(artifact.get("iterations"), "phase iterations", minimum=0)
            require(artifact["iterations"] <= phase_config["max_iterations"]
                    and saved.get("iterations") == artifact["iterations"], "Phase iteration cap or payload differs")
            same(saved.get("runtime_seconds"), runtime, "phase runtime payload")
            _trajectory(saved, artifact["iterations"], wall, previous_score, independent["score"], manifest)
            for field in ("seed", "relay_enabled", "enable_home_seed", "enable_lazy_repair", "enable_terminal_task_exchange"):
                require(saved["metadata"].get(field) == phase_config[field], "Phase metadata differs: " + field)
            same(saved["metadata"].get("time_limit_seconds"), phase_config["time_limit_seconds"], "phase configured search time")
            phase_iterations += artifact["iterations"]
            phase_runtime += runtime
            previous_end = end
        independently_scored_phases.append({**artifact, "reference": independent, "passed": True})
        phase_payloads.append(saved)
        previous_routes_digest, previous_score = artifact["routes_sha256"], independent["score"]
    require(phase_iterations == row["iterations"], "Combined phase iterations differ from final")
    same(final.get("runtime_seconds"), phase_runtime, "combined solver runtime")
    if case["relay"]:
        warm = independently_scored_phases[1]
        retain_warm = (objective_key(_physical(artifacts[2]["score"]), manifest, "weighted")
                       > objective_key(_physical(artifacts[1]["score"]), manifest, "weighted"))
        selected_artifact = artifacts[1] if retain_warm else artifacts[2]
        require(digest(routes) == selected_artifact["routes_sha256"], "Final does not select the independently bound phase winner")
        require(metadata.get("staged_relay") is True and metadata.get("relay_direct_warmup_fraction") == .95
                and metadata.get("relay_retained_warmup_incumbent") is retain_warm, "Staged incumbent retention metadata differs")
        for field, expected in (("relay_warmup_iterations", artifacts[1]["iterations"]),
                                ("relay_search_iterations", artifacts[2]["iterations"])):
            require(metadata.get(field) == expected, "Staged iteration metadata differs")
        for field, index in (("relay_warmup_runtime_seconds", 1), ("relay_search_runtime_seconds", 2)):
            same(metadata.get(field), artifacts[index]["runtime_seconds"], "staged " + field)
        for field, index in (("relay_warmup_score", 1), ("relay_search_final_score", 2)):
            require(isinstance(metadata.get(field), list) and len(metadata[field]) == 3, "Missing staged physical score")
            for name, value in zip(FIELDS, metadata[field], strict=True):
                same(value, independently_scored_phases[index]["reference"]["score"][name], "staged score " + name)
        warmup_incremental = _delta(warm["reference"]["score"], reference["score"], manifest)
        direct_iterations = artifacts[1]["iterations"]
    else:
        require(digest(routes) == artifacts[-1]["routes_sha256"], "Final differs from saved DIRECT incumbent")
        require(not metadata.get("staged_relay", False), "DIRECT case unexpectedly staged")
        warmup_incremental = None
        direct_iterations = row["iterations"]
    counter_artifact = dict(solution_path=row["counterfactual_path"], solution_sha256=row["counterfactual_sha256"])
    counter_topology = {**topology, "relay_allowed": False}
    counter_payload, counter_reference = _saved_payload(output, counter_artifact, tasks, counter_topology, manifest)
    require(counter_payload.get("iterations") == 0 and type(counter_payload["iterations"]) is int
            and counter_payload.get("runtime_seconds") == 0.0 and counter_payload.get("metadata") == {},
            "Fixed-structure counterfactual unexpectedly contains optimizer work")
    expected_counter_routes = direct_counterfactual(routes, topology)
    require([record["encoded_visits"] for record in counter_payload["routes"]] == expected_counter_routes
            and all(abs(visit) in tasks for route in expected_counter_routes for visit in route), "Counterfactual is not exact no-search contraction")
    for field in FIELDS:
        same(row["counterfactual_score"][field], counter_reference["score"][field], "independent counterfactual " + field)
    periodic_audit = _periodic_statistics(row, final, direct_iterations, manifest)
    return dict(run_id=row["run_id"], scenario=row["scenario"], seed=row["seed"], passed=True,
                reference=reference, counterfactual=counter_reference, phases=independently_scored_phases,
                warmup_incremental=warmup_incremental,
                counterfactual_delta=_delta(counter_reference["score"], reference["score"], manifest),
                trajectory=trajectory_audit, periodic=periodic_audit,
                counterfactual_is_fixed_structure_not_reoptimized=True)


def _summary(rows, manifest):
    lookup = {(row["seed"], row["scenario"]): row for row in rows}
    result = []
    for home_group, direct_name, relay_name in (("origin", ORDER[0], ORDER[1]), ("stations", ORDER[2], ORDER[3])):
        pairs = []
        for seed in SEEDS:
            direct, relay = lookup[seed, direct_name], lookup[seed, relay_name]
            before = {field: direct[field] for field in FIELDS}
            after = {field: relay[field] for field in FIELDS}
            delta = _delta(before, after, manifest)
            before_key, after_key = objective_key(_physical(before), manifest, "weighted"), objective_key(_physical(after), manifest, "weighted")
            pairs.append(dict(seed=seed, direct_run_id=direct["run_id"], relay_run_id=relay["run_id"],
                direct_score=before, relay_score=after, direct_weighted_score=_weighted(before, manifest),
                relay_weighted_score=_weighted(after, manifest), **delta,
                strict_weighted_outcome="win" if after_key < before_key else "loss" if after_key > before_key else "tie",
                physical_on_time_outcome="win" if after["late_count"] < before["late_count"] else "loss" if after["late_count"] > before["late_count"] else "tie",
                cross_uav_handoff_count=relay["relay_statistics"]["cross_uav_handoff_count"]))
        result.append(dict(home_group=home_group, pair_count=3, pairs=pairs,
            strict_weighted_wins=sum(pair["strict_weighted_outcome"] == "win" for pair in pairs),
            strict_weighted_ties=sum(pair["strict_weighted_outcome"] == "tie" for pair in pairs),
            strict_weighted_losses=sum(pair["strict_weighted_outcome"] == "loss" for pair in pairs),
            mean_weighted_delta=sum(pair["weighted_delta"] for pair in pairs) / 3,
            mean_physical_delta={field: sum(pair["physical_delta"][field] for pair in pairs) / 3 for field in FIELDS},
            mean_on_time_delta=sum(pair["on_time_delta"] for pair in pairs) / 3,
            significance_or_cross_instance_claim=False))
    return result


def audit_results(output):
    """Pass only after every prespecified run is present and independently valid."""
    output = Path(output).resolve()
    manifest, rows = read(output / "manifest.json"), read(output / "runs.json")
    parent_path = within(ROOT, manifest["parent_manifest_path"])
    require(sha(parent_path) == manifest["parent_manifest_sha256"], "Parent weighted manifest changed")
    parent_audit_path = parent_path.parent / "audit.json"
    require(sha(parent_audit_path) == manifest["parent_audit_sha256"], "Parent weighted audit changed")
    parent, parent_audit = read(parent_path), read(parent_audit_path)
    require(parent_audit.get("passed") is True and parent_audit.get("manifest_sha256") == sha(parent_path)
            and parent_audit.get("runs_sha256") == sha(parent_path.parent / "runs.json"), "Parent audit does not bind completed parent results")
    audit_contract(manifest, parent)
    require(sha(within(ROOT, manifest["input_path"])) == manifest["input_sha256"]
            and sha(within(ROOT, manifest["problem_path"])) == manifest["problem_sha256"], "Official input or problem changed")
    tasks = load_official_csv(within(ROOT, manifest["input_path"]))
    require(isinstance(rows, list) and len(rows) == 12
            and [row.get("run_id") for row in rows] == [cell["run_id"] for cell in manifest["schedule"]],
            "All 12 prespecified runs must be complete; partial progress cannot pass")
    source_audit = audit_sources(output, manifest)
    topologies, initials = _topologies(output, manifest, tasks)
    results, registered = [], set()
    for row, cell in zip(rows, manifest["schedule"], strict=True):
        name = cell["scenario"]
        results.append(audit_row(output, row, cell, manifest, tasks, topologies[name], initials[name]))
        registered.update(within(output, artifact["solution_path"]) for artifact in row["phase_artifacts"])
        registered.add(within(output, row["solution_path"]))
        registered.add(within(output, row["counterfactual_path"]))
    existing = {path.resolve() for directory in ("run_solutions", "phase_solutions", "counterfactuals")
                for path in (output / directory).glob("*.json")}
    require(existing == registered, "Unregistered or missing measured solution artifact")
    total = sum(row["end_to_end_seconds"] for row in rows)
    require(total <= 12 * 240, "Cumulative prespecified budget exceeded")
    return dict(passed=True, run_count=12, planned_unique_runs=12, completed_unique_runs=12,
                seeds=SEEDS, manifest_sha256=sha(output / "manifest.json"), runs_sha256=sha(output / "runs.json"),
                audited_at=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), total_measured_end_to_end_seconds=total,
                source=source_audit, results=results, summary=_summary(rows, manifest),
                checks=dict(all_prespecified_cells_completed=True, independent_csv_geometry=True,
                    independent_structure_and_handoff_dag=True, all_service_event_clocks_recomputed=True,
                    K_is_final_delivery_responsibility=True, paired_homes_network_and_initial_routes_identical=True,
                    all_arms_equal_240_second_budget=True, counterfactual_has_no_optimizer=True,
                    warmup_incremental_separate_from_full_budget_comparison=True,
                    no_historical_samples_mixed=True, intermediate_trajectory_routes_available=False))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    result = audit_results(args.output_dir)
    (args.output_dir / "audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({key: result[key] for key in ("passed", "run_count", "total_measured_end_to_end_seconds")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
