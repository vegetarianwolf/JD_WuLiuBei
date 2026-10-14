"""Independent fixed-network three-aircraft, four-task repooling audit.

Only immutable standard-library auditors are imported. The submitted search
module is neither imported nor executed. Every proposed plan is first rebuilt
from the same frozen topology, including all native flights and relay waits.
The selected parcels' shared flight is then measured from actual onboard cargo
and Euclidean event coordinates, rather than trusted from saved diagnostics.
"""
from __future__ import annotations

from math import hypot

try:
    from .audit_mobile_relay import _point
    from .audit_weighted_relay_ablation import _geometry, reference_schedule
    from .audit_weighted_direct_pair import require, same
except ImportError:
    from audit_mobile_relay import _point
    from audit_weighted_relay_ablation import _geometry, reference_schedule
    from audit_weighted_direct_pair import require, same


def _identity(visit, registry):
    leg = registry.get(abs(visit))
    return leg["task_id"] if leg else abs(visit)


def _owners(routes, registry):
    return {_identity(visit, registry): aircraft for aircraft, route in enumerate(routes)
            for visit in route if visit < 0
            and (abs(visit) not in registry or registry[-visit]["kind"] == "RELAY_OUT")}


def _direct_task(routes, registry, task):
    return all(abs(visit) not in registry for route in routes for visit in route
               if _identity(visit, registry) == task)


def _shared_flight(tasks, homes, stations, registry, route, aircraft, identifiers, clocks):
    """Actual cargo membership, every flight and the simultaneous take frontier."""
    held, previous, segments, load_before = set(), homes[aircraft], [], {}
    for position, visit in enumerate(route):
        point = _point(tasks, stations, registry, visit)
        length = hypot(point[0]-previous[0], point[1]-previous[1])
        load_before[position] = len(held)
        if set(identifiers) <= held and length > 0.:
            segments.append(dict(from_position=position-1, to_position=position,
                from_point=list(previous), to_point=list(point), distance_km=length,
                departure_min=clocks[position-1] if position else 0., arrival_min=clocks[position]))
        if visit > 0:
            held.add(visit)
        else:
            held.remove(-visit)
        previous = point
    return sum(segment["distance_km"] for segment in segments), segments, load_before


def audit_repooling_proposal(tasks, topology, base_routes, proposal_routes, move, *, relay_enabled):
    """Verify both arms' common final map and the relay arm's genuine new pair.

    Aircraft indices in ``move`` are zero based. The single supplied topology
    is the immutable common base/proposal geometry, so it supplies no new home,
    station or resource to either plan. Tiny fixtures may use fewer aircraft
    and a smaller final quota than the official instance.
    """
    require(type(relay_enabled) is bool, "Repooling arm must be boolean")
    require(isinstance(move, dict), "Missing repooling move")
    homes, stations, registry, count, capacity, maximum, speed = _geometry(tasks, topology)
    require(capacity == 2, "Repooling experiment requires capacity two")
    for name in ("i", "j", "k1", "k2"):
        require(type(move.get(name)) is int and move[name] in tasks, "Invalid repooling task: "+name)
    for name in ("a", "b", "c"):
        require(type(move.get(name)) is int and 0 <= move[name] < count, "Invalid repooling aircraft: "+name)
    i, j, k1, k2 = (move[name] for name in ("i", "j", "k1", "k2"))
    a, b, c = (move[name] for name in ("a", "b", "c"))
    selected = {i, j, k1, k2}
    require(len(selected) == 4 and len({a,b,c}) == 3, "Repooling requires four distinct tasks and three distinct aircraft")
    require(move.get("mode") == ("RELAY" if relay_enabled else "DIRECT"), "Saved repooling arm differs")
    require("station_id" in move, "Missing saved repooling station")
    station = move["station_id"]
    if relay_enabled:
        require(type(station) is int and station in stations, "Repooling must use an existing fixed station")
    else:
        require(station is None, "DIRECT repooling control cannot claim a handoff station")
    for name, expected in (("final_aircraft", [c,c,a,b]), ("source_aircraft", [a,b])):
        if name in move:
            require(isinstance(move[name], list) and all(type(value) is int for value in move[name])
                    and move[name] == expected, "Saved repooling descriptor differs: "+name)
    if "receiver_aircraft" in move:
        require(type(move["receiver_aircraft"]) is int and move["receiver_aircraft"] == c,
                "Saved repooling receiver differs")
    if "compensation_mode" in move:
        require(move["compensation_mode"] == "DIRECT", "Saved repooling compensation differs")

    base = reference_schedule(tasks, base_routes, topology)
    require(not base["handoffs"], "Repooling base must contain only DIRECT tasks")
    proposal = reference_schedule(tasks, proposal_routes, topology)
    original_owners, final_owners = _owners(base_routes, registry), _owners(proposal_routes, registry)
    require([original_owners[t] for t in (i,j,k1,k2)] == [a,b,c,c], "Base task ownership differs from repooling sources")
    require([final_owners[t] for t in (i,j,k1,k2)] == [c,c,a,b], "Repooling changed the required final map")
    require([r["task_count"] for r in base["route_records"]] == [r["task_count"] for r in proposal["route_records"]],
            "Repooling changed an aircraft's final task quota")
    without = lambda routes: tuple(tuple(v for v in route if _identity(v,registry) not in selected) for route in routes)
    require(without(base_routes) == without(proposal_routes), "Repooling moved or reordered an unrelated event")
    require(_direct_task(proposal_routes,registry,k1) and _direct_task(proposal_routes,registry,k2),
            "Repooling compensation tasks must stay whole DIRECT plans")

    if relay_enabled:
        handoffs = proposal["handoffs"]
        require(len(handoffs) == 2 and {h["task_id"] for h in handoffs} == {i,j},
                "Repooling must retain exactly its two selected relay plans")
        by_task = {h["task_id"]:h for h in handoffs}
        for task, source in ((i,a),(j,b)):
            handoff = by_task[task]
            require(handoff["cross_uav"] and handoff["inbound_drone_id"] == source+1
                    and handoff["outbound_drone_id"] == c+1 and handoff["relay_id"] == station,
                    "Repooling relay has the wrong original source, final receiver or station")
        identifiers = (by_task[i]["out_leg"], by_task[j]["out_leg"])
    else:
        require(not proposal["handoffs"] and all(_direct_task(proposal_routes,registry,t) for t in selected),
                "DIRECT repooling control cannot contain relay plans")
        identifiers = (i,j)
    route = proposal_routes[c]
    distance, segments, loads = _shared_flight(tasks,homes,stations,registry,route,c,identifiers,
                                             proposal["event_times_min"][c])
    take_positions = sorted(route.index(identifier) for identifier in identifiers)
    if relay_enabled:
        require(take_positions[1] == take_positions[0]+1, "Repooling OUT pickups must form one contiguous block")
        require(loads[take_positions[0]] == 0, "Repooling receiver must start its two-parcel pickup block empty")
        require(distance > 1e-12, "Repooling selected parcels must share a positive-distance loaded flight")
    for field in ("shared_double_loaded_km", "shared_loaded_distance_km", "shared_loaded_distance"):
        if field in move:
            same(move[field], distance, "saved repooling shared loaded distance")
    return dict(passed=True, mode=move["mode"], station_id=station,
        source_owners={str(t):owner for t,owner in sorted(original_owners.items())},
        final_owners={str(t):owner for t,owner in sorted(final_owners.items())},
        final_tasks_per_aircraft=[r["task_count"] for r in proposal["route_records"]],
        shared_loaded_distance_km=distance, shared_loaded_segments=segments,
        receiver_load_before_pickups=[loads[position] for position in take_positions],
        actual_cross_uav_count=proposal["relay_statistics"]["cross_uav_handoff_count"],
        base_score=base["score"], score=proposal["score"],
        delivery_times_min=proposal["delivery_times_min"], event_times_min=proposal["event_times_min"],
        handoffs=proposal["handoffs"], core_reference=proposal)
