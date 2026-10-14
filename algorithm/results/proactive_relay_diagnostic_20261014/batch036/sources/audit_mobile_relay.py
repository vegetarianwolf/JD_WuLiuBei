"""Independent geometry/DAG audit of synchronous mobile cargo exchange.

Only standard-library arithmetic and the old independent fixed-site auditor
are used. Existing ground stations retain their old semantics; every appended
site must be one two-UAV reciprocal meeting, with no ground package storage.
The canonical core payload records are checked first. Physical meeting drops
are then rebuilt with extra prefix-arrival edges, not trusted from telemetry.
"""
from __future__ import annotations

from collections import deque
from math import hypot

try:
    from .audit_weighted_relay_ablation import _geometry, audit_payload, reference_schedule
    from .audit_weighted_direct_pair import require, same
except ImportError:
    from audit_weighted_relay_ablation import _geometry, audit_payload, reference_schedule
    from audit_weighted_direct_pair import require, same


def _preserved(tasks, base, current):
    _geometry(tasks, base)
    homes, stations, registry, count, capacity, maximum, speed = _geometry(tasks, current)
    for field in ("drone_count", "capacity", "max_tasks_per_drone", "speed_km_per_min", "home_points"):
        require(current[field] == base[field], "Mobile extension changed base resource: " + field)
    for field in ("drone_homes", "deployment"):
        if field in base:
            require(current.get(field) == base[field], "Mobile extension changed " + field)
    old_sites = {row[0]: tuple(row[1:]) for row in base["stations"]}
    require(all(stations.get(site) == point for site, point in old_sites.items()),
            "Mobile extension changed or removed an original station")
    old_legs = {row["id"]: row for row in base["legs"]}
    require(all(registry.get(identifier) == leg for identifier, leg in old_legs.items()),
            "Mobile extension changed or removed an original transport leg")
    new_sites = set(stations) - set(old_sites)
    require(all(leg["relay_id"] in new_sites for identifier, leg in registry.items()
                if identifier not in old_legs), "Appended transport leg is not at a new mobile site")
    old_candidates = base.get("task_relay_candidates", {})
    current_candidates = current.get("task_relay_candidates", {})
    require(all(set(values) == set(current_candidates.get(str(task), current_candidates.get(task, ()))) & set(old_sites)
                for task, values in old_candidates.items()), "Original station eligibility changed")
    return homes, stations, registry, count, capacity, maximum, speed, new_sites, old_legs


def _point(tasks, stations, registry, visit):
    leg = registry.get(abs(visit))
    if leg is None:
        return tasks[abs(visit)][0 if visit > 0 else 1]
    if leg["kind"] == "RELAY_IN":
        return tasks[leg["task_id"]][0] if visit > 0 else stations[leg["relay_id"]]
    return stations[leg["relay_id"]] if visit > 0 else tasks[leg["task_id"]][1]


def audit_mobile_routes(tasks, base_topology, current_topology, routes):
    """Rebuild resource invariants, continuous flight and synchronous meetings."""
    (homes, stations, registry, count, capacity, maximum, speed,
     new_sites, old_legs) = _preserved(tasks, base_topology, current_topology)
    core = reference_schedule(tasks, routes, current_topology)
    successors, indegree, earliest, travel, roles, deliveries, drops, pickups = {}, {}, {}, {}, {}, {}, {}, {}
    load_before, onboard_before, site_events = {}, {}, {site: {} for site in new_sites}
    used_new_legs = set()
    for route_index, route in enumerate(routes):
        previous, onboard = homes[route_index], set()
        for position, visit in enumerate(route):
            node = (route_index, position)
            point = _point(tasks, stations, registry, visit)
            travel[node] = hypot(point[0]-previous[0], point[1]-previous[1]) / speed
            previous = point
            successors[node], indegree[node], earliest[node] = {}, 0, travel[node] if position == 0 else 0.
            load_before[node], onboard_before[node] = len(onboard), tuple(sorted(onboard))
            identifier, leg = abs(visit), registry.get(abs(visit))
            role = "pickup" if visit > 0 else "delivery"
            if leg is not None:
                if leg["kind"] == "RELAY_IN":
                    role = "pickup" if visit > 0 else "drop"
                else:
                    role = "take" if visit > 0 else "delivery"
                if leg["relay_id"] in new_sites:
                    used_new_legs.add(identifier)
                    if role in ("drop", "take"):
                        site_events[leg["relay_id"]].setdefault(route_index, []).append((position, role, leg["task_id"]))
            task = identifier if leg is None else leg["task_id"]
            roles[node] = role
            if role == "delivery":
                deliveries[task] = node
            if role == "drop":
                drops[task] = node
            if role == "take":
                pickups[task] = node
            if visit > 0:
                onboard.add(identifier)
            else:
                onboard.remove(identifier)
    expected_new_legs = {identifier for identifier, leg in registry.items() if leg["relay_id"] in new_sites}
    require(used_new_legs == expected_new_legs, "Mobile site contains unused registered transport legs")

    def edge(source, target, weight):
        if target not in successors[source]:
            successors[source][target] = weight
            indegree[target] += 1
        else:
            successors[source][target] = max(successors[source][target], weight)

    for route_index, route in enumerate(routes):
        for position in range(1, len(route)):
            edge((route_index, position-1), (route_index, position), travel[route_index, position])
    for task, drop in drops.items():
        edge(drop, pickups[task], 0.)

    meeting_specs = []
    for site in sorted(new_sites):
        aircraft = sorted(site_events[site])
        require(len(aircraft) == 2, "Each mobile site must be used by exactly two UAVs")
        blocks, groups, first = [], [], []
        for route_index in aircraft:
            events = site_events[site][route_index]
            positions = [event[0] for event in events]
            require(positions == list(range(positions[0], positions[-1]+1)),
                    "Mobile drop/take block is not contiguous")
            own = [t for _, role, t in events if role == "drop"]
            taken = [t for _, role, t in events if role == "take"]
            require(len(own) in (1, 2) and len(own) == len(taken), "Mobile group must exchange one or two parcels per UAV")
            require([role for _, role, _ in events] == ["drop"]*len(own)+["take"]*len(taken),
                    "Mobile UAV must drop all own cargo before taking the other group")
            require(positions[0] > 0, "Mobile aircraft cannot drop cargo before travelling from its pickup")
            blocks.append(events)
            groups.append(own)
            first.append((route_index, positions[0]))
            require(load_before[first[-1]] <= capacity and len(own) <= load_before[first[-1]],
                    "Waiting aircraft does not hold its own transferred cargo")
        require(len(groups[0]) == len(groups[1]), "Reciprocal mobile groups have different sizes")
        for side in (0, 1):
            other = 1-side
            require(set(t for _, role, t in blocks[side] if role == "take") == set(groups[other]),
                    "Mobile aircraft takes cargo from outside the other aircraft's group")
            require(all(drops[t][0] == aircraft[side] and pickups[t][0] == aircraft[other] for t in groups[side]),
                    "Mobile cargo does not form a true cross-UAV handoff")
            previous_node = (aircraft[side], first[side][1]-1)
            edge(previous_node, first[other], travel[first[side]])
        meeting_specs.append((site, aircraft, blocks, groups, first))

    pending = deque(node for node, degree in indegree.items() if degree == 0)
    processed = 0
    while pending:
        node = pending.popleft()
        processed += 1
        for target, weight in successors[node].items():
            earliest[target] = max(earliest[target], earliest[node]+weight)
            indegree[target] -= 1
            if not indegree[target]:
                pending.append(target)
    require(processed == len(earliest), "Synchronous mobile handoff dependency cycle")
    clocks = {str(t): earliest[node] for t, node in sorted(deliveries.items())}
    for task, clock in clocks.items():
        same(clock, core["delivery_times_min"][task], "Mobile synchronization changed final delivery clock")
    lateness = [max(0., clocks[str(task)]-tasks[task][2]) for task in sorted(tasks)]
    positive = [value for value in lateness if value > 1e-9]
    physical_score = dict(late_count=len(positive), total_lateness_min=sum(positive), distance_km=core["score"]["distance_km"])
    for field, value in physical_score.items():
        same(value, core["score"][field], "Mobile/core physical score differs: "+field)
    physical_events = [[earliest[i, position] for position in range(len(route))] for i, route in enumerate(routes)]
    meetings = []
    for site, aircraft, blocks, groups, first in meeting_specs:
        arrivals = [earliest[r, pos-1]+travel[r, pos] for r, pos in first]
        synchronization = max(arrivals)
        drop_events, pickup_events = [], []
        for side, route_index in enumerate(aircraft):
            for position, role, task in blocks[side]:
                same(earliest[route_index, position], synchronization, "Mobile event is not simultaneous")
                if role == "drop":
                    require(registry[abs(routes[route_index][position])]["id"] in onboard_before[first[side]],
                            "Mobile parcel was not held on aircraft during synchronization wait")
                record = dict(route_index=route_index, position=position, visit=routes[route_index][position],
                              physical_time_min=earliest[route_index, position])
                (drop_events if role == "drop" else pickup_events).append(record)
        meetings.append(dict(station_id=site, point=list(stations[site]), aircraft=aircraft,
            own_task_groups=groups, cut_positions=[pos for _, pos in first],
            arrival_times_min=arrivals, synchronization_min=synchronization,
            aircraft_waiting_min=[max(0., synchronization-at) for at in arrivals],
            waiting_loads=[load_before[node] for node in first], group_size=len(groups[0]),
            drop_events=drop_events, pickup_events=pickup_events, ground_cache_min=0.))
    waits = [wait for meeting in meetings for wait in meeting["aircraft_waiting_min"]]
    return dict(passed=True, score=physical_score, delivery_times_min=clocks,
                physical_event_times=physical_events, meetings=meetings,
                total_mobile_aircraft_waiting_min=sum(waits),
                maximum_mobile_aircraft_waiting_min=max(waits, default=0.),
                mobile_ground_cache_min=0., new_mobile_site_count=len(new_sites),
                core_reference=core)


def _bind_meetings(saved, independent):
    require(isinstance(saved, list) and len(saved) == len(independent), "Saved mobile meeting count differs")
    by_site = {item.get("station_id"): item for item in saved if isinstance(item, dict)}
    require(len(by_site) == len(saved), "Saved mobile meeting sites are duplicate or invalid")
    for reference in independent:
        actual = by_site.get(reference["station_id"])
        require(actual is not None, "Missing saved mobile meeting")
        require(type(actual.get("station_id")) is int and type(actual.get("group_size")) is int,
                "Saved mobile meeting identifiers must be integers")
        point = actual.get("point")
        require(isinstance(point, list) and len(point) == 2, "Invalid saved mobile point")
        for a, b in zip(point, reference["point"], strict=True):
            same(a, b, "saved mobile point")
        for field in ("aircraft", "cut_positions"):
            values = actual.get(field)
            require(isinstance(values, list) and len(values) == 2
                    and all(type(v) is int and v >= 0 for v in values), "Invalid saved mobile "+field)
        own = actual.get("own_task_groups")
        require(isinstance(own, list) and len(own) == 2
                and all(isinstance(group, list) and all(type(t) is int and t > 0 for t in group)
                        for group in own), "Invalid saved mobile cargo groups")
        for field in ("point", "aircraft", "own_task_groups", "cut_positions", "group_size"):
            require(actual.get(field) == reference[field], "Saved mobile meeting differs: "+field)
        same(actual.get("synchronization_min"), reference["synchronization_min"], "saved mobile synchronization")
        for field in ("arrival_times_min", "aircraft_waiting_min"):
            values = actual.get(field)
            require(isinstance(values, list) and len(values) == 2, "Invalid mobile times: "+field)
            for a, b in zip(values, reference[field], strict=True):
                same(a, b, "saved mobile "+field)
        for field in ("drop_events", "pickup_events"):
            events = actual.get(field)
            require(isinstance(events, list) and len(events) == len(reference[field]), "Mobile event binding differs")
            indexed = {(e.get("route_index"), e.get("position")): e for e in events if isinstance(e, dict)}
            require(len(indexed) == len(events), "Duplicate saved mobile event")
            require(all(type(e.get("route_index")) is int and type(e.get("position")) is int
                        and type(e.get("visit")) is int for e in events), "Invalid saved mobile event identifiers")
            for event in reference[field]:
                stored = indexed.get((event["route_index"], event["position"]))
                require(stored is not None and stored.get("visit") == event["visit"], "Saved mobile event identity differs")
                same(stored.get("physical_time_min"), event["physical_time_min"], "saved physical handoff time")


def audit_mobile_payload(tasks, base_topology, current_topology, payload):
    """Bind canonical core metrics plus saved synchronous mobile telemetry."""
    audit_payload(tasks, current_topology, payload)
    routes = [route["encoded_visits"] for route in payload["routes"]]
    result = audit_mobile_routes(tasks, base_topology, current_topology, routes)
    saved = payload.get("mobile_meetings", payload.get("metadata", {}).get("mobile_meetings"))
    if result["new_mobile_site_count"]:
        _bind_meetings(saved, result["meetings"])
    elif saved is not None:
        _bind_meetings(saved, [])
    return result
