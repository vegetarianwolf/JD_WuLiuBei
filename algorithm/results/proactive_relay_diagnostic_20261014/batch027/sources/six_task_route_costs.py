"""Finite, physically timed single-route candidates for six-task sorting.

An IN-only route is a transport prefix, not a complete original-task solution.
No search, global evaluator or complete-solution validator is imported here.
The constructor must combine all legs, then independently check the full DAG.
"""
from __future__ import annotations

from itertools import permutations
from math import isclose, isfinite
from time import perf_counter

from uav_dispatch.model import Score


LATENESS_EPSILON_MIN = 1e-9
SHARED_DISTANCE_EPSILON_KM = 1e-10


class Deadline(Exception):
    pass


_Deadline = Deadline


def _check(deadline):
    if not isfinite(deadline):
        raise ValueError("finite absolute deadline required")
    if perf_counter() >= deadline:
        raise Deadline


def _count(statistics, name, value=1):
    if statistics is not None:
        statistics[name] = statistics.get(name, 0) + value


def weighted_key(score, weights, scales):
    weights, scales = tuple(weights), tuple(scales)
    if (len(weights) != 3 or len(scales) != 3 or not isclose(sum(weights), 1.)
            or any(not isfinite(x) or x <= 0 for x in (*weights, *scales))):
        raise ValueError("three positive finite weights/scales and normalized weights required")
    values = (score.late_count, score.total_lateness_min, score.distance_km)
    return (sum(w * x / s for w, x, s in zip(weights, values, scales)), *values)


def _structure(problem, route, *, deadline=None):
    registry = problem.leg_registry or {}
    task_ids = problem._task_by_id
    route = tuple(route)
    held, seen, before, after, task_legs = set(), set(), [], [], {}
    for visit in route:
        if deadline is not None:
            _check(deadline)
        if type(visit) is not int or visit == 0:
            raise ValueError("nonzero integer transport visits required")
        identifier = abs(visit)
        leg = registry.get(identifier)
        if leg is None and identifier not in task_ids:
            raise ValueError("unknown transport visit")
        if leg is not None and leg.kind not in ("RELAY_IN", "RELAY_OUT"):
            raise ValueError("unknown transport leg kind")
        task = leg.task_id if leg is not None else identifier
        if task not in task_ids:
            raise ValueError("transport leg references an unknown task")
        if task in task_legs and task_legs[task] != identifier:
            raise ValueError("one route mixes multiple transport plans for one task")
        task_legs[task] = identifier
        before.append(len(held))
        if visit > 0:
            if identifier in seen:
                raise ValueError("duplicate transport pickup")
            seen.add(identifier)
            held.add(identifier)
            if len(held) > problem.capacity:
                raise ValueError("route capacity exceeded")
        else:
            if identifier not in held:
                raise ValueError("transport drop precedes pickup or repeats")
            held.remove(identifier)
        after.append(len(held))
    if held:
        raise ValueError("route ends with undelivered transport cargo")
    return route, tuple(before), tuple(after)


def zero_load_gaps(problem, route, *, start=0, deadline=None):
    """Insertion positions of a closed, locally valid transport route."""
    route, before, after = _structure(problem, route, deadline=deadline)
    if type(start) is not int or not 0 <= start <= len(route):
        raise ValueError("valid zero-load gap start required")
    return tuple(p for p in range(start, len(route) + 1)
                 if (before[p] if p < len(route) else 0) == 0)


def route_cost(problem, route, aircraft, *, availability=None, selected_pair=(), deadline,
               statistics=None):
    """True local N/L/D, including OUT waits and every native delivery.

    ``availability`` maps original task IDs to actual source IN-drop times.
    An IN end stores availability but is never counted as final delivery.
    This function does not establish global coverage, final K or DAG legality.
    """
    _check(deadline)
    _count(statistics, "route_cost_requests")
    if type(aircraft) is not int or not 0 <= aircraft < problem.drone_count:
        raise ValueError("valid aircraft index required")
    if not isfinite(problem.speed_km_per_min) or problem.speed_km_per_min <= 0:
        raise ValueError("positive finite speed required")
    if problem.distance_matrix_km is not None:
        raise ValueError("Euclidean route geometry required")
    route, load_before, load_after = _structure(problem, route, deadline=deadline)
    available = {} if availability is None else dict(availability)
    if any(type(t) is not int or t not in problem._task_by_id or type(v) is bool
           or not isfinite(v) or v < 0 for t, v in available.items()):
        raise ValueError("finite nonnegative task availability required")
    registry = problem.leg_registry or {}
    held, previous, clock, distance = set(), problem.home_points()[aircraft], 0., 0.
    clocks, deliveries, drops, waits, shared = [], {}, {}, [], 0.
    pair = frozenset(selected_pair)
    if len(pair) not in (0, 2):
        raise ValueError("shared flight requires two distinct transport identifiers")
    for visit in route:
        _check(deadline)
        point = problem.point_for_visit(visit)
        length = previous.distance_to(point)
        if not isfinite(length) or length < 0:
            raise ValueError("finite nonnegative route geometry required")
        distance += length
        clock += length / problem.speed_km_per_min
        if pair and pair <= held:
            shared += length
        leg = registry.get(abs(visit))
        task = leg.task_id if leg is not None else abs(visit)
        wait = 0.
        if visit > 0:
            if leg is not None and leg.kind == "RELAY_OUT":
                if task not in available:
                    raise ValueError("OUT pickup lacks source availability")
                wait = max(0., available[task] - clock)
                clock += wait
            held.add(visit)
        else:
            held.remove(-visit)
            if leg is not None and leg.kind == "RELAY_IN":
                drops[task] = clock
                if task in available and abs(available[task] - clock) > 1e-8:
                    raise ValueError("own IN drop differs from supplied source availability")
            else:
                deliveries[task] = clock
        clocks.append(clock)
        waits.append(wait)
        previous = point
    late = [delivered - problem.task(task).deadline_min for task, delivered in deliveries.items()]
    positive = [value for value in late if value > LATENESS_EPSILON_MIN]
    if len(deliveries) > problem.max_tasks_per_drone:
        raise ValueError("route final delivery count exceeds K")
    _check(deadline)
    _count(statistics, "route_cost_evaluations")
    return dict(route=route, score=Score(len(positive), sum(positive), distance),
        event_times=tuple(clocks), event_times_min=tuple(clocks), delivery_times=deliveries,
        delivery_times_min=deliveries, availability=drops, drop_times_min=drops,
        waiting_min=tuple(waits), total_waiting_min=sum(waits), load_before=load_before, load_after=load_after,
        shared_loaded_distance_km=shared, aircraft=aircraft, in_end_position=None,
        scoring_scope="single_aircraft_transport_route", complete_solution=False)


def _parameters(problem, pair, aircraft, gap_limit, weights, scales, deadline):
    _check(deadline)
    pair = tuple(pair)
    if (problem.capacity != 2 or problem.distance_matrix_km is not None or len(pair) != 2
            or any(type(t) is not int or t not in problem._task_by_id for t in pair) or len(set(pair)) != 2
            or type(aircraft) is not int or not 0 <= aircraft < problem.drone_count
            or type(gap_limit) is not int or not 1 <= gap_limit <= 4):
        raise ValueError("two distinct tasks, Euclidean C2, valid aircraft and gap limit 1..4 required")
    weighted_key(Score(0, 0., 0.), weights, scales)
    return pair


def _native(problem, route, *, deadline):
    route, _, _ = _structure(problem, route, deadline=deadline)
    if any(abs(v) in (problem.leg_registry or {}) for v in route):
        raise ValueError("native skeleton must contain only whole DIRECT plans")
    return route


def _leg(problem, task, station, kind):
    if type(station) is not int or station not in {s.id for s in problem.relay_stations}:
        raise ValueError("existing fixed station required")
    candidates = problem.task_relay_candidates or {}
    if candidates and station not in candidates.get(task, ()):
        raise ValueError("station is not eligible for this task")
    matches = [leg.id for leg in (problem.leg_registry or {}).values()
               if leg.task_id == task and leg.relay_id == station and leg.kind == kind]
    if len(matches) != 1:
        raise ValueError("unique existing transport leg required")
    return matches[0]


def _gap_options(problem, route, aircraft, block, *, start, limit, deadline):
    gaps = zero_load_gaps(problem, route, start=start, deadline=deadline)
    if not gaps:
        return ()
    ranked = []
    for gap in gaps:
        _check(deadline)
        left = problem.home_points()[aircraft] if gap == 0 else problem.point_for_visit(route[gap - 1])
        previous, delta = left, 0.
        for visit in block:
            point = problem.point_for_visit(visit)
            delta += previous.distance_to(point)
            previous = point
        if gap < len(route):
            right = problem.point_for_visit(route[gap])
            delta += previous.distance_to(right) - left.distance_to(right)
        ranked.append((delta, gap))
    # Preserve an early physical frontier as well as low flight insertion cost.
    return tuple(dict.fromkeys((min(gaps), *(gap for _, gap in sorted(ranked)))))[:limit]


def in_options(problem, native_route, aircraft, task_pair, station_id, *, deadline,
               option_limit=2, gap_limit=4, weights, scales, statistics=None):
    pair = _parameters(problem, task_pair, aircraft, gap_limit, weights, scales, deadline)
    if type(option_limit) is not int or not 1 <= option_limit <= 2:
        raise ValueError("IN option limit must be one or two")
    native = _native(problem, native_route, deadline=deadline)
    if any(abs(v) in pair for v in native):
        raise ValueError("selected task remains in native skeleton")
    identifiers = tuple(_leg(problem, t, station_id, "RELAY_IN") for t in pair)
    candidates = {}
    for pickups in (identifiers, tuple(reversed(identifiers))):
        block = pickups + tuple(-v for v in identifiers)
        for gap in _gap_options(problem, native, aircraft, block, start=0, limit=gap_limit, deadline=deadline):
            _check(deadline)
            route = native[:gap] + block + native[gap:]
            record = route_cost(problem, route, aircraft, selected_pair=identifiers,
                                deadline=deadline, statistics=statistics)
            record.update(record_kind="source_IN", station_id=station_id, source_task_pair=pair,
                          task_pair=pair, in_end_position=gap + 3, pickup_position=gap)
            candidates[route] = record
            _count(statistics, "in_candidates")
    ordered = sorted(candidates.values(), key=lambda r: (weighted_key(r["score"], weights, scales), r["route"]))
    if not ordered:
        return ()
    result = [ordered[0]]
    earliest = min(ordered, key=lambda r: (max(r["availability"].values()),
                    weighted_key(r["score"], weights, scales), r["route"]))
    if option_limit > 1 and earliest["route"] != result[0]["route"]:
        result.append(earliest)
    for record in ordered:
        if len(result) < option_limit and all(record["route"] != r["route"] for r in result):
            result.append(record)
    _check(deadline)
    return tuple(result)


def out_option(problem, in_record, aircraft, task_pair, station_id, availability, *, deadline,
               gap_limit=4, weights, scales, statistics=None):
    pair = _parameters(problem, task_pair, aircraft, gap_limit, weights, scales, deadline)
    route = tuple(in_record["route"])
    registry = problem.leg_registry or {}
    incoming = {abs(v): registry[abs(v)] for v in route if abs(v) in registry}
    source_pair = tuple(in_record.get("source_task_pair", ()))
    if (in_record.get("aircraft") != aircraft or in_record.get("station_id") != station_id
            or len(incoming) != 2 or any(l.kind != "RELAY_IN" or l.relay_id != station_id for l in incoming.values())
            or len(source_pair) != 2 or len(set(source_pair)) != 2
            or set(source_pair) != {l.task_id for l in incoming.values()}
            or {l.task_id for l in incoming.values()} & set(pair)):
        raise ValueError("OUT requires the same aircraft's two different-source IN prefix")
    last = max(route.index(-identifier) for identifier in incoming)
    if in_record.get("in_end_position") != last:
        raise ValueError("IN frontier differs from actual final IN drop")
    route_cost(problem, route, aircraft, availability=availability, deadline=deadline, statistics=statistics)
    if any((registry[abs(v)].task_id if abs(v) in registry else abs(v)) in pair for v in route):
        raise ValueError("OUT task already present on receiver")
    identifiers = tuple(_leg(problem, t, station_id, "RELAY_OUT") for t in pair)
    best = None
    for deliveries in (identifiers, tuple(reversed(identifiers))):
        block = identifiers + tuple(-v for v in deliveries)
        for gap in _gap_options(problem, route, aircraft, block, start=last + 1, limit=gap_limit, deadline=deadline):
            candidate = route[:gap] + block + route[gap:]
            record = route_cost(problem, candidate, aircraft, availability=availability, selected_pair=identifiers,
                                deadline=deadline, statistics=statistics)
            _count(statistics, "out_candidates")
            if record["shared_loaded_distance_km"] <= SHARED_DISTANCE_EPSILON_KM:
                _count(statistics, "out_zero_shared_rejections")
                continue
            record.update(record_kind="receiver_OUT", station_id=station_id,
                source_task_pair=source_pair, task_pair=pair,
                in_end_position=last, pickup_position=gap, outbound_take_positions=(gap, gap + 1))
            if best is None or (weighted_key(record["score"], weights, scales), record["route"]) < \
                    (weighted_key(best["score"], weights, scales), best["route"]):
                best = record
    _check(deadline)
    return best


def direct_option(problem, native_route, aircraft, task_pair, *, deadline, gap_limit=4,
                  weights, scales, statistics=None):
    pair = _parameters(problem, task_pair, aircraft, gap_limit, weights, scales, deadline)
    native = _native(problem, native_route, deadline=deadline)
    if any(abs(v) in pair for v in native):
        raise ValueError("selected DIRECT task remains in native skeleton")
    best = None
    for block in permutations((pair[0], -pair[0], pair[1], -pair[1])):
        if block.index(pair[0]) > block.index(-pair[0]) or block.index(pair[1]) > block.index(-pair[1]):
            continue
        for gap in _gap_options(problem, native, aircraft, block, start=0, limit=gap_limit, deadline=deadline):
            record = route_cost(problem, native[:gap] + block + native[gap:], aircraft,
                                selected_pair=pair, deadline=deadline, statistics=statistics)
            _count(statistics, "direct_candidates")
            record.update(record_kind="DIRECT", task_pair=pair, pickup_position=gap)
            if best is None or (weighted_key(record["score"], weights, scales), record["route"]) < \
                    (weighted_key(best["score"], weights, scales), best["route"]):
                best = record
    _check(deadline)
    return best
