"""Independent audit of one-way, receiver-first mobile cargo handoff.

This deliberately audits only the conservative branch where the receiver is
already at the meeting point when the sender arrives. The sender retains all
new cargo until that instant; the receiver takes it immediately, so no package
is left on the ground and no unmodelled sender wait is permitted. The branch
does not cover general one-way meetings where the sender must wait for a later
receiver. Original fixed-site auditors and reciprocal auditors stay unchanged.
"""
from __future__ import annotations

from math import hypot

try:
    from .audit_mobile_relay import _bind_meetings, _point, _preserved
    from .audit_weighted_relay_ablation import audit_payload, reference_schedule
    from .audit_weighted_direct_pair import require, same
except ImportError:
    from audit_mobile_relay import _bind_meetings, _point, _preserved
    from audit_weighted_relay_ablation import audit_payload, reference_schedule
    from audit_weighted_direct_pair import require, same


def audit_anticipatory_routes(tasks, base_topology, current_topology, routes):
    """Rebuild true travel, cargo state, receiver-first timing and final scores."""
    (homes, stations, registry, count, capacity, maximum, speed,
     new_sites, old_legs) = _preserved(tasks, base_topology, current_topology)
    require(len(new_sites) <= 1, "Conservative anticipatory branch supports a single new meeting site")
    reference = reference_schedule(tasks, routes, current_topology)
    event_times = reference["event_times_min"]
    station_events = {site: {"drop": [], "take": []} for site in new_sites}
    loads, onboards, arrivals = {}, {}, {}
    used_new_legs, used_old_legs = set(), set()
    for aircraft, route in enumerate(routes):
        point, cargo = homes[aircraft], set()
        for position, visit in enumerate(route):
            node = (aircraft, position)
            target = _point(tasks, stations, registry, visit)
            arrivals[node] = (event_times[aircraft][position-1] if position else 0.) + hypot(
                target[0]-point[0], target[1]-point[1])/speed
            point = target
            loads[node], onboards[node] = len(cargo), tuple(sorted(cargo))
            identifier, leg = abs(visit), registry.get(abs(visit))
            if leg is not None:
                if identifier in old_legs:
                    used_old_legs.add(identifier)
                elif leg["relay_id"] in new_sites:
                    used_new_legs.add(identifier)
                    role = ("drop" if visit < 0 and leg["kind"] == "RELAY_IN"
                            else "take" if visit > 0 and leg["kind"] == "RELAY_OUT" else None)
                    if role:
                        station_events[leg["relay_id"]][role].append(dict(
                            route_index=aircraft, position=position, visit=visit,
                            task_id=leg["task_id"]))
            if visit > 0:
                cargo.add(identifier)
            else:
                cargo.remove(identifier)
    expected_new_legs = {identifier for identifier, leg in registry.items() if leg["relay_id"] in new_sites}
    require(used_new_legs == expected_new_legs, "Anticipatory site contains unused registered transport legs")
    if new_sites:
        require(not used_old_legs, "Anticipatory branch must start from DIRECT cargo without pre-existing relay plans")
    meetings = []
    final_owner = {}
    for aircraft, route in enumerate(routes):
        for visit in route:
            if visit < 0:
                leg = registry.get(-visit)
                if leg is None or leg["kind"] == "RELAY_OUT":
                    final_owner[str(-visit if leg is None else leg["task_id"])] = aircraft
    for site in sorted(new_sites):
        drops, takes = station_events[site]["drop"], station_events[site]["take"]
        require(len(drops) in (1, 2) and len(drops) == len(takes),
                "Anticipatory handoff must transfer exactly one or two complete parcels")
        senders, receivers = {e["route_index"] for e in drops}, {e["route_index"] for e in takes}
        require(len(senders) == len(receivers) == 1 and senders != receivers,
                "Anticipatory handoff must use one sender and one distinct receiver")
        sender, receiver = next(iter(senders)), next(iter(receivers))
        require(set(e["task_id"] for e in drops) == set(e["task_id"] for e in takes),
                "Anticipatory receiver takes a different parcel group")
        for block, label in ((drops, "sender drop"), (takes, "receiver take")):
            block.sort(key=lambda e:e["position"])
            positions = [e["position"] for e in block]
            require(positions == list(range(positions[0], positions[0]+len(block))),
                    "Anticipatory "+label+" block is not contiguous")
        first_drop, first_take = (sender, drops[0]["position"]), (receiver, takes[0]["position"])
        arrival_sender, arrival_receiver = arrivals[first_drop], arrivals[first_take]
        # Equality is admitted only within the existing independent auditor's
        # clock tolerance; a positive material receiver delay is rejected.
        if arrival_receiver > arrival_sender:
            same(arrival_receiver, arrival_sender, "Receiver must arrive no later than sender")
        synchronization = event_times[sender][first_drop[1]]
        same(synchronization, arrival_sender, "Sender cannot wait or unload early in receiver-first branch")
        require(loads[first_take]+len(takes) <= capacity,
                "Receiver cannot take the complete anticipatory parcel group within capacity")
        require(loads[first_take] == 0, "Anticipatory receiver must arrive empty for the complete pickup block")
        require(all(abs(e["visit"]) in onboards[first_drop] for e in drops),
                "Sender did not keep every transferred parcel onboard until meeting")
        physical_drops, physical_takes = [], []
        for block, output in ((drops, physical_drops), (takes, physical_takes)):
            for e in block:
                same(event_times[e["route_index"]][e["position"]], synchronization,
                     "Anticipatory drop and take must happen at the same instant")
                output.append(dict(route_index=e["route_index"], position=e["position"], visit=e["visit"],
                                   physical_time_min=event_times[e["route_index"]][e["position"]]))
        handoffs = [h for h in reference["handoffs"] if h["relay_id"] == site]
        require(len(handoffs) == len(drops) and all(h["cross_uav"] for h in handoffs),
                "Anticipatory parcels are not all true cross-UAV transfers")
        for handoff in handoffs:
            same(handoff["package_cache_min"], 0., "Anticipatory parcel cannot be cached on ground")
        waiting = max(0., synchronization-arrival_receiver)
        same(sum(h["aircraft_waiting_min"] for h in handoffs), waiting,
             "Receiver wait must be charged once for the whole pickup block")
        meetings.append(dict(station_id=site, point=list(stations[site]), aircraft=[sender,receiver],
            own_task_groups=[[e["task_id"] for e in drops],[]], group_size=len(drops),
            cut_positions=[first_drop[1],first_take[1]], arrival_times_min=[arrival_sender,arrival_receiver],
            synchronization_min=synchronization, aircraft_waiting_min=[0.,waiting],
            waiting_loads=[loads[first_drop],loads[first_take]],
            drop_events=physical_drops, pickup_events=physical_takes,
            meeting_type="receiver_arrives_first", ground_cache_min=0.))
    waits = [wait for meeting in meetings for wait in meeting["aircraft_waiting_min"]]
    return dict(passed=True, score=reference["score"], delivery_times_min=reference["delivery_times_min"],
        physical_event_times=event_times, meetings=meetings,
        total_mobile_aircraft_waiting_min=sum(waits), maximum_mobile_aircraft_waiting_min=max(waits,default=0.),
        mobile_ground_cache_min=0., new_mobile_site_count=len(new_sites), final_delivery_owner=final_owner,
        branch="receiver_arrives_first_only", core_reference=reference)


def audit_anticipatory_payload(tasks, base_topology, current_topology, payload):
    """Bind complete saved core events and receiver-first physical metadata."""
    audit_payload(tasks, current_topology, payload)
    routes = [r["encoded_visits"] for r in payload["routes"]]
    result = audit_anticipatory_routes(tasks, base_topology, current_topology, routes)
    saved = payload.get("mobile_meetings", payload.get("metadata", {}).get("mobile_meetings"))
    if result["new_mobile_site_count"] or saved is not None:
        _bind_meetings(saved, result["meetings"])
        require(all(item.get("meeting_type") == "receiver_arrives_first" for item in saved),
                "Saved anticipatory meeting branch differs")
        for item, meeting in zip(saved, result["meetings"], strict=True):
            # These descriptive fields are optional in legacy-shaped metadata,
            # but any saved values must agree with independent cargo/cache state.
            if "waiting_loads" in item:
                require(item["waiting_loads"] == meeting["waiting_loads"],
                        "Saved anticipatory waiting cargo loads differ")
            if "ground_cache_min" in item:
                same(item["ground_cache_min"], 0., "Saved anticipatory ground cache differs")
    return result
