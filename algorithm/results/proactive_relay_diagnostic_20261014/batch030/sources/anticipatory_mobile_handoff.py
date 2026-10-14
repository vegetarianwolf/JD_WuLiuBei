"""Research-only dispersed pickups and an empty receiver's mobile handoff.

The original final owner deletes one/two DIRECT pickups and flies empty to a
continuous meeting coordinate. A different UAV collects those parcels along
its own route. The receiver must arrive first: it waits holding no cargo, and
all sender drops/receiver pickups occur at the sender's arrival. Early drop and
ground storage are forbidden. This extension keeps every delivery slot and
every unrelated visit on its original aircraft in its original order.

Input is deliberately limited to pure-DIRECT Euclidean incumbents. One call
scans that frozen incumbent and adds at most one checked meeting. No unused
trial coordinates or trial transport legs are added to the returned problem.
"""
from __future__ import annotations

from dataclasses import dataclass
from heapq import heappush, heapreplace
from itertools import permutations
from math import isclose, isfinite
from time import perf_counter

from uav_dispatch.model import Point, Score, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution

try:  # Frozen experiments are copied beside one another by diagnostic runners.
    import mobile_relay_refinement as mobile
except ImportError:
    from algorithm.experiments import mobile_relay_refinement as mobile


class _Deadline(Exception):
    pass


def _check(deadline):
    if perf_counter() >= deadline:
        raise _Deadline


def _loads(route):
    load, result = 0, [0]
    for visit in route:
        load += 1 if visit > 0 else -1
        result.append(load)
    return tuple(result)


def _capacity_feasible(route, capacity):
    loads = _loads(route)
    return all(0 <= load <= capacity for load in loads) and loads[-1] == 0


def _route_score(problem, route, owner, new_legs, point, synchronization=None):
    """All actual flights; optionally the one new receiver waiting constraint."""
    previous, clock = problem.home_points()[owner], 0.
    distance, count, lateness = 0., 0, 0.
    for visit in route:
        coordinate, final_task = mobile._event(problem, visit, new_legs, point)
        km = previous.distance_to(coordinate)
        distance += km
        clock += km / problem.speed_km_per_min
        leg = new_legs.get(abs(visit))
        if synchronization is not None and visit > 0 and leg is not None and leg.kind == "RELAY_OUT":
            clock = max(clock, synchronization)
        if final_task is not None:
            late = max(0., clock - problem.task(final_task).deadline_min)
            if late > 1e-9:
                count += 1
                lateness += late
        previous = coordinate
    return Score(count, lateness, distance)


def _prefix(problem, route, owner, position, new_legs):
    """Coordinate/time just before the new meeting's first visit."""
    previous, clock = problem.home_points()[owner], 0.
    for visit in route[:position]:
        coordinate, _ = mobile._event(problem, visit, new_legs, previous)
        clock += previous.distance_to(coordinate) / problem.speed_km_per_min
        previous = coordinate
    return previous, clock


@dataclass(frozen=True, slots=True)
class _Group:
    owner: int
    tasks: tuple
    original_cuts: tuple
    priority: tuple


@dataclass(frozen=True, slots=True)
class _Option:
    route: tuple
    score: Score
    first_position: int
    handoff_position: int
    meeting_position: int
    pickup_order: tuple
    incoming: Point
    outgoing: Point | None
    prefix_time: float


@dataclass(frozen=True, slots=True)
class _Candidate:
    routes: tuple
    legs: dict
    descriptor: dict
    key: tuple
    score: Score
    arrivals: tuple
    priority: tuple


@dataclass(frozen=True, slots=True)
class _Worst:
    candidate: _Candidate

    def __lt__(self, other):
        return self.candidate.priority > other.candidate.priority


def _groups(problem, routes, base_scores, weights, scales, seed, max_size, deadline, stats):
    result = []
    for owner, route in enumerate(routes):
        active, frontiers = [], {}
        for position in range(len(route) + 1):
            _check(deadline)
            for size in range(1, max_size + 1):
                if len(active) == size:
                    frontiers.setdefault(tuple(active), []).append(position)
                elif size == 1:
                    for task in active:
                        frontiers.setdefault((task,), []).append(position)
            if position == len(route):
                break
            if route[position] > 0:
                active.append(route[position])
            else:
                active.remove(-route[position])
        for tasks, cuts in sorted(frontiers.items()):
            # Teleport pickup deletion is a local optimistic ranking, never an
            # accepted solution. Delivery responsibility/slots remain unchanged.
            deleted = tuple(v for v in route if v not in tasks)
            lower = _route_score(problem, deleted, owner, {}, Point(0., 0.))
            delta = mobile.weighted_key(lower, weights, scales)[0] - mobile.weighted_key(base_scores[owner], weights, scales)[0]
            span = max((problem.task(t).pickup.distance_to(problem.task(u).pickup)
                        for t in tasks for u in tasks), default=0.)
            tie = (owner * 31 + sum(tasks) * 17 + seed) % 104729
            result.append(_Group(owner, tasks, tuple(cuts), (delta, -len(tasks), span, tie, owner, tasks)))
    stats["groups_generated"] = len(result)
    return tuple(sorted(result, key=lambda g: g.priority))


def _sender_positions(route, size):
    """Capacity-two placements, including original load-one piggybacking.

    For two extra parcels the second pickup immediately precedes the joint
    drop, at an original empty gap. For one extra parcel, every intervening
    original frontier must have load at most one.
    """
    loads = _loads(route)
    for handoff, load in enumerate(loads):
        if load > (0 if size == 2 else 1):
            continue
        for first in range(handoff, -1, -1):
            if loads[first] > 1:
                break
            yield first, handoff


def _sender_route(route, incoming, first, handoff):
    if len(incoming) == 1:
        return route[:first] + incoming + route[first:handoff] + (-incoming[0],) + route[handoff:]
    return (route[:first] + incoming[:1] + route[first:handoff] + incoming[1:]
            + tuple(-v for v in incoming) + route[handoff:])


def _sender_options(problem, routes, owner, group, by_task, new_legs, weights, scales,
                    limit, deadline, stats):
    original, result = routes[owner], []
    for order in permutations(group.tasks):
        incoming = tuple(by_task[t][0] for t in order)
        for first, handoff in _sender_positions(original, len(order)):
            _check(deadline)
            candidate = _sender_route(original, incoming, first, handoff)
            if not _capacity_feasible(candidate, problem.capacity):
                stats["capacity_pruned"] += 1
                continue
            meeting_position = handoff + len(order)
            ua, ta = _prefix(problem, candidate, owner, meeting_position, new_legs)
            next_position = meeting_position + len(order)
            va = (problem.point_for_visit(candidate[next_position]) if next_position < len(candidate) else None)
            lower = _route_score(problem, candidate, owner, new_legs, ua)
            stats["sender_options_generated"] += 1
            result.append(_Option(candidate, lower, first, handoff, meeting_position, order, ua, va, ta))
    result.sort(key=lambda o: (mobile.weighted_key(o.score, weights, scales), o.prefix_time,
                               o.first_position, o.handoff_position, o.pickup_order))
    return tuple(result[:limit])


def _receiver_options(problem, routes, group, by_task, new_legs, weights, scales,
                      limit, deadline, stats):
    original, tasks = routes[group.owner], group.tasks
    reduced = tuple(-by_task[-v][1] if v < 0 and -v in tasks else v for v in original if v not in tasks)
    first_delivery = min(reduced.index(-by_task[t][1]) for t in tasks)
    order = tuple(sorted(tasks, key=lambda t: original.index(-t)))
    starts = tuple(by_task[t][1] for t in order)
    loads, result = _loads(reduced), []
    for position in range(first_delivery + 1):
        _check(deadline)
        if loads[position] != 0:
            stats["receiver_nonempty_cuts_pruned"] += 1
            continue
        candidate = reduced[:position] + starts + reduced[position:]
        if not _capacity_feasible(candidate, problem.capacity):
            stats["capacity_pruned"] += 1
            continue
        ub, tb = _prefix(problem, candidate, group.owner, position, new_legs)
        vb, _ = mobile._event(problem, candidate[position + len(tasks)], new_legs, ub)
        lower = _route_score(problem, candidate, group.owner, new_legs, ub)
        stats["receiver_options_generated"] += 1
        result.append(_Option(candidate, lower, position, position, position, order, ub, vb, tb))
    result.sort(key=lambda o: (mobile.weighted_key(o.score, weights, scales), o.prefix_time, o.first_position))
    return tuple(result[:limit])


def _points(sender, receiver, speed, limit):
    """Intersections, four-terminal median/foci and the arrival-equality point."""
    a, b, c, d = sender.incoming, sender.outgoing, receiver.incoming, receiver.outgoing
    terminals = (a, c, d) if b is None else (a, b, c, d)
    earliest = mobile._earliest_meeting(a, c, sender.prefix_time, receiver.prefix_time, speed)
    median = mobile._geometric_median(terminals)
    pool = [median, earliest, *terminals, mobile._mid(a, c), mobile._mid(c, d)]
    if b is not None:
        pool.extend((*mobile._intersections(a, b, c, d), mobile._mid(a, b)))
    # Approach the median from the receiver focus: this produces feasible-side
    # seeds when the unconstrained distance median would let the sender arrive
    # before the empty receiver. No feasibility is assumed without exact timing.
    for fraction in (.25, .5, .75):
        pool.append(Point(c.x + fraction * (median.x - c.x), c.y + fraction * (median.y - c.y)))
    unique = {(round(p.x, 10), round(p.y, 10)): p for p in pool}
    def priority(point):
        aa = sender.prefix_time + a.distance_to(point) / speed
        ab = receiver.prefix_time + c.distance_to(point) / speed
        distance = a.distance_to(point) + c.distance_to(point) + point.distance_to(d)
        if b is not None:
            distance += point.distance_to(b)
        return (ab > aa, distance, aa, point.x, point.y)
    ranked = sorted(unique.values(), key=priority)
    chosen = ranked[:limit]
    if earliest not in chosen and limit > 1:
        chosen[-1] = earliest
    return tuple(chosen)


def _new_legs(problem, tasks):
    station = max((s.id for s in problem.relay_stations), default=0) + 1
    next_id = max((*problem.task_ids, *(problem.leg_registry or {})), default=0) + 1
    legs, by_task = {}, {}
    for task in tasks:
        by_task[task] = (next_id, next_id + 1)
        legs[next_id] = TransportLeg(next_id, task, "RELAY_IN", station)
        legs[next_id + 1] = TransportLeg(next_id + 1, task, "RELAY_OUT", station)
        next_id += 2
    return legs, by_task, station


def _descriptor(group, sender_owner, sender, receiver, point, station):
    return dict(type="receiver_arrives_first", meeting_type="receiver_arrives_first",
        station_id=station, point=[point.x, point.y], aircraft=[sender_owner, group.owner],
        own_task_groups=[list(sender.pickup_order), []], group_size=len(group.tasks),
        original_cut_positions=[sender.handoff_position, receiver.first_position],
        sender_first_position=sender.first_position, sender_handoff_position=sender.handoff_position,
        sender_pickup_order=list(sender.pickup_order), receiver_position=receiver.first_position,
        original_receiver_cuts=list(group.original_cuts), receiver_required_empty=True)


def _physical_meetings(problem, routes, descriptor, schedule):
    """Check actual receiver-first clocks; never retime an early cache drop."""
    station, (sender, receiver) = descriptor["station_id"], descriptor["aircraft"]
    registry = problem.leg_registry or {}
    drops = [dict(route_index=sender, position=p, visit=v) for p, v in enumerate(routes[sender])
        if v < 0 and (leg := registry.get(-v)) is not None and leg.relay_id == station and leg.kind == "RELAY_IN"]
    takes = [dict(route_index=receiver, position=p, visit=v) for p, v in enumerate(routes[receiver])
        if v > 0 and (leg := registry.get(v)) is not None and leg.relay_id == station and leg.kind == "RELAY_OUT"]
    size = descriptor["group_size"]
    if len(drops) != size or len(takes) != size:
        raise ValueError("wrong directed mobile group shape")
    if any([e["position"] for e in events] != list(range(events[0]["position"], events[0]["position"] + size))
           for events in (drops, takes)):
        raise ValueError("directed mobile meeting visits are not contiguous")
    arrivals = []
    for aircraft, events in ((sender, drops), (receiver, takes)):
        cut = events[0]["position"]
        before = schedule.event_times_min[(aircraft, cut - 1)] if cut else 0.
        previous = problem.point_for_visit(routes[aircraft][cut - 1]) if cut else problem.home_points()[aircraft]
        point = Point(*descriptor["point"])
        arrivals.append(before + previous.distance_to(point) / problem.speed_km_per_min)
    if _loads(routes[receiver])[takes[0]["position"]] != 0:
        raise ValueError("receiver is not empty before the mobile meeting")
    if arrivals[1] > arrivals[0]:
        raise ValueError("receiver arrives after sender: ground caching would be required")
    sync = arrivals[0]
    for event in drops + takes:
        actual = schedule.event_times_min[(event["route_index"], event["position"])]
        if not isclose(actual, sync, rel_tol=1e-10, abs_tol=1e-8):
            raise ValueError("directed mobile drop/take is not synchronous")
        event["physical_time_min"] = sync
    return [{**descriptor, "cut_positions": [drops[0]["position"], takes[0]["position"]],
        "arrival_times_min": arrivals, "synchronization_min": sync,
        "aircraft_waiting_min": [0., sync - arrivals[1]], "receiver_waiting_load": 0,
        "drop_events": drops, "pickup_events": takes,
        "physical_model": "empty_receiver_arrives_first_and_waits_for_sender"}]


def refine(problem, routes, *, deadline, weights, scales, seed=2026101497, max_pairs=256,
           sender_limit=8, receiver_limit=4, combination_limit=8, point_limit=10,
           full_candidate_limit=32, max_group_size=2, screening_fraction=.7):
    """Return one checked receiver-first handoff, or the checked DIRECT input.

    Task groups use actual DIRECT on-board frontiers. All sender placements and
    empty receiver cuts are screened to the declared local shortlists. Only the
    declared combination/coordinate shortlists are covered; no global claim of
    neighborhood exhaustion is made. Seventy percent of remaining wall budget
    is reserved for cheap coordinate screening and the rest for full checks.
    Full candidates are ordered by complete physical weighted scores, including
    the empty receiver's wait. Final acceptance uses production DAG/full checks.
    """
    weights, scales = tuple(weights), tuple(scales)
    bounds = (max_pairs, sender_limit, receiver_limit, combination_limit, point_limit, full_candidate_limit, max_group_size)
    if (len(weights) != 3 or len(scales) != 3 or any(not isfinite(v) or v <= 0 for v in (*weights, *scales))
        or not isclose(sum(weights), 1.) or not isfinite(deadline)):
        raise ValueError("finite positive normalized weights/scales and deadline required")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in bounds) or max_group_size > 2:
        raise ValueError("positive integer bounds and group size one/two required")
    if not isfinite(screening_fraction) or not 0. < screening_fraction < 1.:
        raise ValueError("screening_fraction must be strictly between zero and one")
    started = perf_counter()
    base = tuple(tuple(r) for r in routes)
    if len(base) > problem.drone_count:
        raise ValueError("too many routes")
    base += ((),) * (problem.drone_count - len(base))
    checked = evaluate_solution(problem, base, _force_full=True)
    if not checked.valid:
        raise ValueError(f"invalid initial solution: {checked.violations}")
    index = build_plan_index(problem, base)
    owners = dict(index.delivery_route_by_task)
    initial_key = best_key = mobile.weighted_key(checked.score, weights, scales)
    best, best_problem = base, problem
    stats = dict(method="Anticipatory-Mobile-Directed-Handoff", model_extension="continuous_airborne_rendezvous",
        seed=seed, weights=list(weights), scales=list(scales), fixed_station_cache=False,
        receiver_required_empty=True, receiver_arrives_first=True, groups_generated=0,
        groups_shortlisted=0, groups_examined=0, sender_probes=0, sender_options_generated=0,
        receiver_options_generated=0, receiver_nonempty_cuts_pruned=0, capacity_pruned=0,
        combinations_examined=0, points_examined=0, receiver_late_pruned=0, weighted_score_pruned=0,
        candidate_duplicates=0, candidate_pool_size=0, new_problems_built=0, global_evaluations=0,
        full_evaluations=1, rejected_cycles=0, rejected_invalid=0, rejected_physical=0,
        accepted_improvements=0, accepted_transfer_moves=0, accepted_control_only_moves=0,
        new_cross_uav_count=0, final_cross_uav_count=0, mobile_meetings=[], accepted_moves=[],
        first_complete_candidate=None, best_constructed_candidate=None, full_candidate_snapshots=[],
        screening_deadline_reached=False, deadline_reached=False, unsupported_reason=None,
        initial_key=list(initial_key), input_snapshot=mobile._snapshot(problem, base, checked, weights, scales, []),
        configuration=dict(max_pairs=max_pairs, sender_limit=sender_limit, receiver_limit=receiver_limit,
            combination_limit=combination_limit, point_limit=point_limit, full_candidate_limit=full_candidate_limit,
            max_group_size=max_group_size, max_moves=1, screening_fraction=screening_fraction))
    if problem.distance_matrix_km is not None:
        stats["unsupported_reason"] = "custom_distance_matrix"
    elif index.relay_task_ids:
        stats["unsupported_reason"] = "input_contains_active_relay_plans_requires_pure_DIRECT"
    elif problem.capacity != 2:
        stats["unsupported_reason"] = "capacity_must_be_two"
    pool, seen = [], set()
    screen_deadline = min(deadline, started + screening_fraction * max(0., deadline - started))
    base_scores = ()
    if stats["unsupported_reason"] is None:
        try:
            _check(screen_deadline)
            base_scores = tuple(_route_score(problem, r, i, {}, Point(0., 0.)) for i, r in enumerate(base))
            base_sum = sum(base_scores, Score(0, 0., 0.))
            groups = _groups(problem, base, base_scores, weights, scales, seed, max_group_size, screen_deadline, stats)
            groups = groups[:max_pairs]
            stats["groups_shortlisted"] = len(groups)
            for group in groups:
                _check(screen_deadline)
                stats["groups_examined"] += 1
                legs, by_task, station = _new_legs(problem, group.tasks)
                receiving = _receiver_options(problem, base, group, by_task, legs, weights, scales,
                    receiver_limit, screen_deadline, stats)
                for owner in range(len(base)):
                    _check(screen_deadline)
                    if owner == group.owner:
                        continue
                    stats["sender_probes"] += 1
                    sending = _sender_options(problem, base, owner, group, by_task, legs, weights, scales,
                        sender_limit, screen_deadline, stats)
                    combinations = sorted(((a, b) for a in sending for b in receiving), key=lambda pair: (
                        mobile.weighted_key(pair[0].score + pair[1].score, weights, scales),
                        max(0., pair[1].prefix_time - pair[0].prefix_time),
                        pair[0].first_position, pair[0].handoff_position, pair[1].first_position, pair[0].pickup_order))
                    for sending_option, receiving_option in combinations[:combination_limit]:
                        _check(screen_deadline)
                        stats["combinations_examined"] += 1
                        changed = list(base)
                        changed[owner], changed[group.owner] = sending_option.route, receiving_option.route
                        candidate = tuple(changed)
                        for point in _points(sending_option, receiving_option, problem.speed_km_per_min, point_limit):
                            _check(screen_deadline)
                            stats["points_examined"] += 1
                            aa = sending_option.prefix_time + sending_option.incoming.distance_to(point) / problem.speed_km_per_min
                            ab = receiving_option.prefix_time + receiving_option.incoming.distance_to(point) / problem.speed_km_per_min
                            if ab > aa:
                                stats["receiver_late_pruned"] += 1
                                continue
                            signature = (candidate, tuple(group.tasks), point.x, point.y)
                            if signature in seen:
                                stats["candidate_duplicates"] += 1
                                continue
                            seen.add(signature)
                            exact = (base_sum - base_scores[owner] - base_scores[group.owner]
                                + _route_score(problem, candidate[owner], owner, legs, point)
                                + _route_score(problem, candidate[group.owner], group.owner, legs, point, aa))
                            key = mobile.weighted_key(exact, weights, scales)
                            if key[0] > initial_key[0] + 1e-10:
                                stats["weighted_score_pruned"] += 1
                                continue
                            descriptor = _descriptor(group, owner, sending_option, receiving_option, point, station)
                            priority = (*key, -len(group.tasks), owner, group.owner, group.tasks,
                                sending_option.first_position, sending_option.handoff_position,
                                receiving_option.first_position, sending_option.pickup_order, point.x, point.y)
                            proposal = _Candidate(candidate, legs, descriptor, key, exact, (aa, ab), priority)
                            if len(pool) < full_candidate_limit:
                                heappush(pool, _Worst(proposal))
                            elif priority < pool[0].candidate.priority:
                                heapreplace(pool, _Worst(proposal))
        except _Deadline:
            stats["screening_deadline_reached"] = True
        stats["candidate_pool_size"] = len(pool)
        try:
            for item in sorted(pool, key=lambda item: item.candidate.priority):
                _check(deadline)
                candidate = item.candidate
                changed_problem = mobile._problem(problem, candidate.legs, candidate.descriptor)
                stats["new_problems_built"] += 1
                _check(deadline)
                scheduled = GlobalRelayEvaluator(changed_problem).evaluate(candidate.routes)
                stats["global_evaluations"] += 1
                _check(deadline)
                if not scheduled.feasible:
                    stats["rejected_cycles"] += 1
                    continue
                try:
                    physical = _physical_meetings(changed_problem, candidate.routes, candidate.descriptor, scheduled)
                except ValueError:
                    stats["rejected_physical"] += 1
                    continue
                confirmation = evaluate_solution(changed_problem, candidate.routes, _force_full=True)
                stats["full_evaluations"] += 1
                _check(deadline)
                if not confirmation.valid:
                    stats["rejected_invalid"] += 1
                    continue
                final_index = build_plan_index(changed_problem, candidate.routes)
                if dict(final_index.delivery_route_by_task) != owners:
                    raise RuntimeError("anticipatory handoff changed final responsibility")
                selected = set(candidate.descriptor["own_task_groups"][0])
                for aircraft, route in enumerate(candidate.routes):
                    retained = tuple(v for v in route if abs(v) not in candidate.legs)
                    expected = tuple(v for v in base[aircraft] if abs(v) not in selected)
                    if retained != expected:
                        raise RuntimeError("anticipatory handoff moved another job or event order")
                snapshot = mobile._snapshot(changed_problem, candidate.routes, confirmation, weights, scales, physical)
                _check(deadline)
                stats["full_candidate_snapshots"].append(snapshot)
                if stats["first_complete_candidate"] is None:
                    stats["first_complete_candidate"] = snapshot
                if stats["best_constructed_candidate"] is None or tuple(snapshot["key"]) < tuple(stats["best_constructed_candidate"]["key"]):
                    stats["best_constructed_candidate"] = snapshot
                key = mobile.weighted_key(confirmation.score, weights, scales)
                if key < best_key:
                    _check(deadline)
                    accepted_at = perf_counter()
                    if accepted_at >= deadline:
                        raise _Deadline
                    best_problem, best, checked, best_key = changed_problem, candidate.routes, confirmation, key
                    stats["mobile_meetings"] = physical
                    stats["accepted_improvements"] = stats["accepted_transfer_moves"] = 1
                    stats["new_cross_uav_count"] = stats["final_cross_uav_count"] = len(selected)
                    stats["accepted_moves"] = [dict(station_id=candidate.descriptor["station_id"],
                        tasks=sorted(selected), sender=candidate.descriptor["aircraft"][0], receiver=candidate.descriptor["aircraft"][1],
                        preserved_final_responsibility=True, preserved_other_job_routes_and_event_order=True,
                        accepted_at_perf_counter=accepted_at, full_snapshot=snapshot)]
                    # Exact manual ordering is only a shortlist heuristic. Full
                    # validation may use a different floating summation order;
                    # retain the checked best through all predeclared proposals.
        except _Deadline:
            stats["deadline_reached"] = True
    if not checked.valid or best_key > initial_key:
        raise RuntimeError("anticipatory handoff lost the checked weighted incumbent")
    final_index = build_plan_index(best_problem, best)
    stats.update(deadline_reached=perf_counter() >= deadline, final_key=list(best_key),
        final_cross_uav_count=sum(final_index.inbound_route_by_task[t] != final_index.outbound_route_by_task[t]
                                  for t in final_index.relay_task_ids),
        final_responsibility_preserved=dict(build_plan_index(best_problem, best).delivery_route_by_task) == owners,
        runtime_seconds=perf_counter() - started)
    return dict(problem=best_problem, routes=best, evaluation=checked,
                iterations=stats["points_examined"], metadata=stats)
