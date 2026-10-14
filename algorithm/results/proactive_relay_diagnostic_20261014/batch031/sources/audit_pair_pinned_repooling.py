"""Independent fixed-network audit with only the two relay tasks pinned.

The original DIRECT layout must first generate a genuine four-task macro.
Its compensation tasks are historical provenance, not post-polish owner
constraints. This module imports immutable standard-library auditors only;
it never imports the search implementation or constructs a synthetic base.
"""
from __future__ import annotations

try:
    from .audit_repooling import audit_repooling_proposal, _identity, _owners, _direct_task, _shared_flight
    from .audit_pinned_repooling import _plans
    from .audit_weighted_relay_ablation import _geometry, reference_schedule
    from .audit_weighted_direct_pair import require, same
except ImportError:
    from audit_repooling import audit_repooling_proposal, _identity, _owners, _direct_task, _shared_flight
    from audit_pinned_repooling import _plans
    from audit_weighted_relay_ablation import _geometry, reference_schedule
    from audit_weighted_direct_pair import require, same


def _bind_telemetry(saved, result, original_move):
    """Optional source-bound post snapshot, distinct from the historical move."""
    require(isinstance(saved, dict), "Missing pair-pinned post telemetry")
    expected = result["actual_final_owners"]
    actual = saved.get("actual_final_owners")
    require(isinstance(actual, dict) and set(actual) == set(expected)
            and all(type(value) is int for value in actual.values()) and actual == expected,
            "Saved pair-pinned actual final owners differ")
    for field in ("pinned_task_ids", "released_compensation_task_ids"):
        actual = saved.get(field)
        require(isinstance(actual, list) and all(type(value) is int for value in actual)
                and actual == result[field], "Saved pair-pinned descriptor differs: " + field)
    require(saved.get("original_macro_move") == original_move,
            "Saved pair-pinned original macro provenance differs")
    require(saved.get("pin_scope") == "relay_pair_only", "Saved pair-pinned scope differs")
    if "score" in saved:
        require(isinstance(saved["score"], dict) and set(saved["score"]) == set(result["score"]),
                "Saved pair-pinned score fields differ")
        for field, value in result["score"].items():
            same(saved["score"][field], value, "saved pair-pinned score " + field)
    for field in ("compensation_owner_changed_task_ids", "compensation_actual_final_owners"):
        if field in saved:
            require(saved[field] == result[field], "Saved pair-pinned compensation telemetry differs: " + field)
    move = saved.get("move")
    if move is not None:
        require(isinstance(move, dict) and move.get("semantic") in
                ("post_pair_polish", "original_macro_before_pair_polish"),
                "Post-polish move must declare its pair-polish semantics")
        historical = move["semantic"] == "original_macro_before_pair_polish"
        for field in ("i", "j", "k1", "k2", "a", "b", "c", "mode", "station_id",
                      "source_aircraft", "receiver_aircraft", "compensation_mode"):
            if field in original_move:
                require(move.get(field) == original_move[field], "Historical macro descriptor changed: " + field)
        if "final_aircraft" in move:
            expected = (original_move["final_aircraft"] if historical else
                        [result["actual_final_owners"][str(original_move[field])]
                         for field in ("i", "j", "k1", "k2")])
            require(isinstance(move["final_aircraft"], list)
                    and all(type(value) is int for value in move["final_aircraft"])
                    and move["final_aircraft"] == expected,
                    "Saved post final-aircraft mapping confuses historical and actual owners")
        for field in ("shared_double_loaded_km", "shared_loaded_distance_km", "shared_loaded_distance"):
            if field in move:
                expected = original_move.get(field) if historical else result["shared_loaded_distance_km"]
                require(expected is not None, "Historical shared distance lacks original macro provenance")
                same(move[field], expected, "saved post pair shared loaded distance")


def audit_pair_pinned_repooling_proposal(tasks, topology, base_direct_routes, macro_routes,
                                       polished_routes, original_macro_move, *, relay_enabled,
                                       telemetry=None):
    """Audit true raw provenance, then two pinned plans and free DIRECT tasks.

    ``original_macro_move.final_aircraft`` and its saved shared distance bind
    only the original macro. The post result always reports actual owners and
    actual shared geometry independently. Optional ``telemetry`` binds the
    producer's post snapshot fields to those independently derived values.
    """
    move = original_macro_move
    macro = audit_repooling_proposal(tasks, topology, base_direct_routes, macro_routes, move,
                                    relay_enabled=relay_enabled)
    homes, stations, registry, count, capacity, maximum, speed = _geometry(tasks, topology)
    polished = reference_schedule(tasks, polished_routes, topology)
    i, j, k1, k2 = (move[name] for name in ("i", "j", "k1", "k2"))
    pinned = {i, j}
    expected_plans = _plans(macro_routes, registry, pinned)
    actual_plans = _plans(polished_routes, registry, pinned)
    require(actual_plans == expected_plans,
            "Pair-pinned repooling changed a pinned mode, transport leg, station, source or final owner")
    subsequences = lambda routes: tuple(tuple(v for v in route if _identity(v, registry) in pinned)
                                       for route in routes)
    require(subsequences(macro_routes) == subsequences(polished_routes),
            "Pair-pinned repooling changed a pinned signed-event subsequence")
    counts = [record["task_count"] for record in polished["route_records"]]
    require(counts == macro["final_tasks_per_aircraft"], "Pair-pinned repooling changed final delivery quotas")
    old_relay = {handoff["task_id"] for handoff in macro["handoffs"]}
    new_relay = {handoff["task_id"] for handoff in polished["handoffs"]}
    require(old_relay == new_relay, "Pair-pinned repooling introduced or removed a relay plan")
    require(all(_direct_task(polished_routes, registry, task) for task in set(tasks) - pinned),
            "Every unpinned task, including released compensation, must remain whole DIRECT")
    receiver = move["c"]
    if relay_enabled:
        by_task = {handoff["task_id"]: handoff for handoff in polished["handoffs"]}
        require(len(by_task) == 2 and set(by_task) == pinned, "Pair-pinned macro must retain exactly its two transfers")
        for task, source in ((i, move["a"]), (j, move["b"])):
            handoff = by_task[task]
            require(handoff["cross_uav"] and handoff["inbound_drone_id"] == source + 1
                    and handoff["outbound_drone_id"] == receiver + 1
                    and handoff["relay_id"] == move["station_id"], "Pair-pinned handoff roles or station changed")
        identifiers = (by_task[i]["out_leg"], by_task[j]["out_leg"])
    else:
        require(not polished["handoffs"], "Pair-pinned DIRECT control cannot introduce relay plans")
        identifiers = (i, j)
    route = polished_routes[receiver]
    distance, segments, loads = _shared_flight(tasks, homes, stations, registry, route, receiver, identifiers,
                                              polished["event_times_min"][receiver])
    positions = sorted(route.index(identifier) for identifier in identifiers)
    if relay_enabled:
        require(positions[1] == positions[0] + 1, "Pair-pinned OUT pickups must remain contiguous")
        require(loads[positions[0]] == 0, "Pair-pinned receiver must enter its pickup block empty")
        require(distance > 1e-12, "Pair-pinned parcels lost positive-distance joint loaded flight")
    before_owners, after_owners = _owners(macro_routes, registry), _owners(polished_routes, registry)
    unpinned = set(tasks) - pinned
    changed = sorted(task for task in unpinned if before_owners[task] != after_owners[task])
    compensation_changed = [task for task in (k1, k2) if before_owners[task] != after_owners[task]]
    native = lambda routes: tuple(tuple(v for v in route if _identity(v, registry) in unpinned) for route in routes)
    old_sequences, new_sequences = native(macro_routes), native(polished_routes)
    result = dict(passed=True, mode=move["mode"], pin_scope="relay_pair_only", macro_audit=macro,
        macro_score=macro["score"], score=polished["score"], pinned_task_ids=[i, j],
        released_compensation_task_ids=[k1, k2], original_macro_final_aircraft=[receiver, receiver, move["a"], move["b"]],
        final_tasks_per_aircraft=counts, actual_final_owners={str(task): owner for task, owner in sorted(after_owners.items())},
        pinned_final_owners={str(task): after_owners[task] for task in (i, j)},
        compensation_actual_final_owners={str(task): after_owners[task] for task in (k1, k2)},
        compensation_owner_changed_count=len(compensation_changed), compensation_owner_changed_task_ids=compensation_changed,
        unpinned_owner_changed_count=len(changed), unpinned_owner_changed_task_ids=changed,
        pinned_event_subsequences=[list(sequence) for sequence in subsequences(polished_routes)],
        unpinned_event_sequence_changed_aircraft=[a for a in range(count) if old_sequences[a] != new_sequences[a]],
        full_event_sequence_changed_aircraft=[a for a in range(count) if tuple(macro_routes[a]) != tuple(polished_routes[a])],
        shared_loaded_distance_km=distance, shared_loaded_segments=segments,
        receiver_load_before_pickups=[loads[position] for position in positions],
        actual_cross_uav_count=polished["relay_statistics"]["cross_uav_handoff_count"],
        delivery_times_min=polished["delivery_times_min"], event_times_min=polished["event_times_min"],
        handoffs=polished["handoffs"], core_reference=polished)
    result["final_owners"] = dict(result["actual_final_owners"])
    if telemetry is not None:
        _bind_telemetry(telemetry, result, move)
    return result
