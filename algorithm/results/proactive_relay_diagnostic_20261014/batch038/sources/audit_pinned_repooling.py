"""Independent repooling audit after ordinary native tasks are reoptimized.

The genuine original DIRECT layout and complete macro are audited first. No
synthetic base is constructed. The later plan may reassign/reorder native
tasks, but must retain all four selected transport plans, aircraft and signed
event subsequences, with the original final quotas and no extra relay plans.
"""
from __future__ import annotations

try:
    from .audit_repooling import audit_repooling_proposal, _identity, _owners, _shared_flight
    from .audit_weighted_relay_ablation import _geometry, reference_schedule
    from .audit_weighted_direct_pair import require, same
except ImportError:
    from audit_repooling import audit_repooling_proposal, _identity, _owners, _shared_flight
    from audit_weighted_relay_ablation import _geometry, reference_schedule
    from audit_weighted_direct_pair import require, same


def _plans(routes, registry, selected):
    """Transport identity and role aircraft; native insertions may shift positions."""
    result = {}
    for aircraft, route in enumerate(routes):
        for visit in route:
            task = _identity(visit, registry)
            if task not in selected:
                continue
            leg = registry.get(abs(visit))
            mode, station = ("RELAY", leg["relay_id"]) if leg else ("DIRECT", None)
            if leg is None:
                role = "pickup" if visit > 0 else "delivery"
            elif leg["kind"] == "RELAY_IN":
                role = "pickup" if visit > 0 else "drop"
            else:
                role = "take" if visit > 0 else "delivery"
            record = result.setdefault(task, dict(mode=mode, station_id=station, leg_ids=set(), roles={}))
            if leg:
                record["leg_ids"].add(leg["id"])
            record["roles"][role] = (aircraft, visit)
    return {task:dict(**{key:value for key,value in record.items() if key != "leg_ids"},
                      leg_ids=tuple(sorted(record["leg_ids"]))) for task,record in result.items()}


def audit_pinned_repooling_proposal(tasks, topology, base_direct_routes, macro_routes,
                                  polished_routes, move, *, relay_enabled):
    """Audit a true macro then its pinned continuation, allowing native changes."""
    macro = audit_repooling_proposal(tasks,topology,base_direct_routes,macro_routes,move,
                                    relay_enabled=relay_enabled)
    homes, stations, registry, count, capacity, maximum, speed = _geometry(tasks,topology)
    polished = reference_schedule(tasks,polished_routes,topology)
    i,j,k1,k2 = (move[name] for name in ("i","j","k1","k2"))
    selected = {i,j,k1,k2}
    expected_plans = _plans(macro_routes,registry,selected)
    actual_plans = _plans(polished_routes,registry,selected)
    require(actual_plans == expected_plans,
            "Pinned repooling changed a selected mode, transport leg, station, source or final owner")
    subsequences = lambda routes: tuple(tuple(v for v in route if _identity(v,registry) in selected) for route in routes)
    require(subsequences(macro_routes) == subsequences(polished_routes),
            "Pinned repooling changed a selected signed-event subsequence")
    final_counts = [record["task_count"] for record in polished["route_records"]]
    require(final_counts == macro["final_tasks_per_aircraft"], "Pinned repooling changed final delivery quotas")
    original_relay_tasks = {handoff["task_id"] for handoff in macro["handoffs"]}
    final_relay_tasks = {handoff["task_id"] for handoff in polished["handoffs"]}
    require(original_relay_tasks == final_relay_tasks, "Pinned repooling introduced or removed a relay plan")
    require(all(actual_plans[t]["mode"] == "DIRECT" for t in (k1,k2)),
            "Pinned compensation tasks must remain whole DIRECT plans")
    receiver = move["c"]
    if relay_enabled:
        by_task = {handoff["task_id"]:handoff for handoff in polished["handoffs"]}
        require(len(by_task) == 2 and set(by_task) == {i,j}, "Pinned relay macro must retain exactly two cross transfers")
        for task, source in ((i,move["a"]),(j,move["b"])):
            handoff = by_task[task]
            require(handoff["cross_uav"] and handoff["inbound_drone_id"] == source+1
                    and handoff["outbound_drone_id"] == receiver+1
                    and handoff["relay_id"] == move["station_id"], "Pinned handoff aircraft or station changed")
        identifiers = (by_task[i]["out_leg"],by_task[j]["out_leg"])
    else:
        require(not polished["handoffs"], "Pinned DIRECT control cannot introduce a relay")
        identifiers = (i,j)
    route = polished_routes[receiver]
    distance, segments, loads = _shared_flight(tasks,homes,stations,registry,route,receiver,identifiers,
                                              polished["event_times_min"][receiver])
    positions = sorted(route.index(identifier) for identifier in identifiers)
    if relay_enabled:
        require(positions[1] == positions[0]+1, "Pinned OUT pickups must remain contiguous")
        require(loads[positions[0]] == 0, "Pinned receiver must enter its two-parcel pickup block empty")
        require(distance > 1e-12, "Pinned parcels lost positive-distance joint loaded flight")
    for field in ("shared_double_loaded_km","shared_loaded_distance_km","shared_loaded_distance"):
        if field in move:
            same(move[field],distance,"saved pinned repooling shared loaded distance")
    macro_owners, polished_owners = _owners(macro_routes,registry),_owners(polished_routes,registry)
    native = set(tasks)-selected
    changed_owners = sorted(task for task in native if macro_owners[task] != polished_owners[task])
    native_sequences = lambda routes: tuple(tuple(v for v in route if _identity(v,registry) in native) for route in routes)
    old_native,new_native = native_sequences(macro_routes),native_sequences(polished_routes)
    changed_routes = [aircraft for aircraft in range(count) if old_native[aircraft] != new_native[aircraft]]
    return dict(passed=True,mode=move["mode"],macro_audit=macro,macro_score=macro["score"],score=polished["score"],
        final_tasks_per_aircraft=final_counts,pinned_task_ids=sorted(selected),
        pinned_event_subsequences=[list(sequence) for sequence in subsequences(polished_routes)],
        final_owners={str(task):owner for task,owner in sorted(polished_owners.items())},
        native_owner_changed_count=len(changed_owners),native_owner_changed_task_ids=changed_owners,
        native_event_sequence_changed_aircraft=changed_routes,
        full_event_sequence_changed_aircraft=[aircraft for aircraft in range(count)
            if tuple(macro_routes[aircraft]) != tuple(polished_routes[aircraft])],
        shared_loaded_distance_km=distance,shared_loaded_segments=segments,
        receiver_load_before_pickups=[loads[position] for position in positions],
        actual_cross_uav_count=polished["relay_statistics"]["cross_uav_handoff_count"],
        delivery_times_min=polished["delivery_times_min"],event_times_min=polished["event_times_min"],
        handoffs=polished["handoffs"],core_reference=polished)
