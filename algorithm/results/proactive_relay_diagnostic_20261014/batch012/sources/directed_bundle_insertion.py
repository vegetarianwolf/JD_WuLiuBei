"""Research-only directed two-package transfer with dispersed sender pickups.

Final delivery responsibility never changes. The receiving UAV's two original
DIRECT pickups are replaced by one fixed-station two-package pickup, while its
delivery slots and every other job keep their order. A different UAV can pick
the first package while carrying one original item, deliver that original item,
then pick the second package and jointly drop both at the fixed station.
Standard unlimited station caching is used; this is not a mobile rendezvous.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.validation import evaluate_solution


SENDER_LIMIT = 8
RECEIVER_LIMIT = 4
COMBINATION_LIMIT = 8


def _coefficients(weights, scales):
    if len(weights) != 3 or len(scales) != 3 or any(
        not isfinite(v) or v <= 0 for v in (*weights, *scales)
    ):
        raise ValueError("Three finite positive objective weights/scales are required")
    return tuple(w / s for w, s in zip(weights, scales))


def _key(score, coefficients):
    physical = (score.late_count, score.total_lateness_min, score.distance_km)
    return (sum(c * v for c, v in zip(coefficients, physical)), *physical)


def _expired(deadline):
    return perf_counter() >= deadline


def _leg_pairs(problem):
    result = {}
    for task_id, legs in problem._legs_by_task.items():
        allowed, stations = set(problem.task_relay_candidates.get(task_id, ())), {}
        for leg in legs:
            if leg.relay_id in allowed:
                stations.setdefault(leg.relay_id, {})[leg.kind] = leg.id
        result[task_id] = {s: (p["RELAY_IN"], p["RELAY_OUT"])
                           for s, p in stations.items() if set(p) == {"RELAY_IN", "RELAY_OUT"}}
    return result


def _loads(route):
    result, load = [0], 0
    for visit in route:
        load += 1 if visit > 0 else -1
        result.append(load)
    return tuple(result)


def _capacity_feasible(route, capacity):
    loads = _loads(route)
    return all(0 <= load <= capacity for load in loads) and loads[-1] == 0


def _distance(problem, route, owner):
    previous, total = problem.home_node(owner), 0.
    for visit in route:
        node = problem._node_for_visit(visit)
        total += problem._distances[previous][node]
        previous = node
    return total


def _arrival_estimate(problem, original, changed_prefix, owner, schedule, anchor_position=None):
    """Retain baseline waiting as a ranking estimate, not a feasibility bound."""
    local = _distance(problem, changed_prefix, owner) / problem.speed_km_per_min
    if not changed_prefix:
        return local
    previous = changed_prefix[-1]
    if anchor_position is not None or previous in original:
        position = anchor_position if anchor_position is not None else original.index(previous)
        baseline_local = _distance(problem, original[:position + 1], owner) / problem.speed_km_per_min
        local += max(0., schedule.event_times_min[(owner, position)] - baseline_local)
    return local


@dataclass(frozen=True)
class _Pair:
    owner: int
    tasks: tuple
    stations: tuple
    original_cuts: tuple
    pickup_removal_delta: float
    priority: tuple


@dataclass(frozen=True)
class _Option:
    route: tuple
    score: Score
    first_position: int
    handoff_position: int
    pickup_order: tuple
    arrival: float


def _pairs(problem, routes, index, schedule, legs, coefficients, seed, deadline):
    direct, result = set(index.direct_task_ids), []
    for owner, route in enumerate(routes):
        active, frontiers = [], {}
        for position in range(len(route) + 1):
            if _expired(deadline):
                return sorted(result, key=lambda p: p.priority)
            if len(active) == 2 and all(t in direct and legs.get(t) for t in active):
                frontiers.setdefault(tuple(active), []).append(position)
            if position == len(route):
                break
            visit = route[position]
            if visit > 0:
                active.append(visit)
            else:
                active.remove(-visit)
        old_distance = _distance(problem, route, owner)
        for tasks, cuts in sorted(frontiers.items()):
            stations = tuple(sorted(set(legs[tasks[0]]) & set(legs[tasks[1]])))
            if not stations:
                continue
            deleted = tuple(v for v in route if v not in tasks)
            delta = _distance(problem, deleted, owner) - old_distance
            risk = sum(coefficients[0] * int(schedule.delivery_times_min[t] > problem.task(t).deadline_min + 1e-9)
                       + coefficients[1] * max(0., schedule.delivery_times_min[t] - problem.task(t).deadline_min)
                       for t in tasks)
            span = problem.task(tasks[0]).pickup.distance_to(problem.task(tasks[1]).pickup)
            tie = (tasks[0] * 31 + tasks[1] * 17 + seed) % 104729
            result.append(_Pair(owner, tasks, stations, tuple(cuts), delta,
                                (coefficients[2] * delta - risk, span, tie, owner, tasks)))
    result.sort(key=lambda p: p.priority)
    return result


def _sender_positions(route):
    """Exact capacity-two geometry of dispersed pickups and a joint drop.

    While holding both extra packages, original load must be zero. Therefore
    the second new pickup and joint drop have no original event between them;
    the first pickup may precede original load-one events but no load-two state.
    """
    loads = _loads(route)
    result = []
    for handoff, load in enumerate(loads):
        if load != 0:
            continue
        for first in range(handoff, -1, -1):
            if loads[first] > 1:
                break
            result.append((first, handoff))
    return tuple(result)


def _sender_route(route, first_in, second_in, first, handoff):
    return (route[:first] + (first_in,) + route[first:handoff]
            + (second_in, -first_in, -second_in) + route[handoff:])


def _sender_options(problem, evaluator, routes, schedule, owner, tasks, station,
                    legs, coefficients, deadline, stats):
    original, result = routes[owner], []
    for order in (tasks, tuple(reversed(tasks))):
        first_in, second_in = (legs[t][station][0] for t in order)
        for first, handoff in _sender_positions(original):
            if _expired(deadline):
                break
            candidate = _sender_route(original, first_in, second_in, first, handoff)
            if not _capacity_feasible(candidate, problem.capacity):
                stats["capacity_pruned"] += 1
                continue
            metrics = evaluator.evaluate(candidate, start_node=problem.home_node(owner))
            stats["local_evaluations"] += 1
            if _expired(deadline):
                break
            # Arrival at the first station drop; both drops are co-located.
            arrival_prefix = candidate[:handoff + 3]
            arrival = _arrival_estimate(problem, original, arrival_prefix, owner, schedule,
                                        handoff - 1 if handoff else None)
            result.append(_Option(candidate, metrics.score, first, handoff, order, arrival))
            stats["sender_options_generated"] += 1
        if _expired(deadline):
            break
    result.sort(key=lambda o: (_key(o.score, coefficients), o.arrival, o.first_position,
                               o.handoff_position, o.pickup_order))
    return tuple(result[:SENDER_LIMIT])


def _receiver_options(problem, evaluator, routes, schedule, pair, station, legs,
                      coefficients, deadline, stats):
    original, tasks = routes[pair.owner], pair.tasks
    outbound = {t: legs[t][station][1] for t in tasks}
    reduced = tuple(-outbound[-v] if -v in outbound else v for v in original if v not in tasks)
    first_delivery = min(reduced.index(-outbound[t]) for t in tasks)
    # Co-located starts have identical physical times; use original final-slot
    # order as the deterministic pickup order rather than duplicating routes.
    order = tuple(sorted(tasks, key=lambda t: original.index(-t)))
    starts = tuple(outbound[t] for t in order)
    result = []
    for position in range(first_delivery + 1):
        if _expired(deadline):
            break
        candidate = reduced[:position] + starts + reduced[position:]
        if not _capacity_feasible(candidate, problem.capacity):
            stats["capacity_pruned"] += 1
            continue
        metrics = evaluator.evaluate(candidate, start_node=problem.home_node(pair.owner))
        stats["local_evaluations"] += 1
        if _expired(deadline):
            break
        anchor = original.index(reduced[position - 1]) if position else None
        arrival = _arrival_estimate(problem, original, candidate[:position + 1], pair.owner, schedule, anchor)
        result.append(_Option(candidate, metrics.score, position, position, order, arrival))
        stats["receiver_options_generated"] += 1
    result.sort(key=lambda o: (_key(o.score, coefficients), o.arrival, o.first_position))
    return tuple(result[:RECEIVER_LIMIT])


def _cross_count(index):
    return sum(index.inbound_route_by_task[t] != index.outbound_route_by_task[t]
               for t in index.relay_task_ids)


def refine(problem, routes, *, deadline, weights, scales, seed=2026101481, max_pairs=256):
    """Checked weighted directed insertion, retaining all original final owners.

    Every receiver's actual shared DIRECT frontier pair, every other sender,
    both sender pickup orders and every common registered fixed station are
    covered until deadline. Sender/receiver shortlists keep 8/4 candidates;
    eight combinations per sender/receiver/station enter exact global scoring.
    Local absolute objective sums only prune above incumbent J + 1e-12.
    """
    if not isfinite(deadline) or not isinstance(max_pairs, int) or max_pairs < 1:
        raise ValueError("A finite absolute deadline and positive max_pairs are required")
    weights, scales = tuple(weights), tuple(scales)
    coefficients = _coefficients(weights, scales)
    started = perf_counter()
    current = tuple(tuple(route) for route in routes)
    if len(current) > problem.drone_count:
        raise ValueError("Too many UAV routes")
    current += ((),) * (problem.drone_count - len(current))
    checked = evaluate_solution(problem, current, _force_full=True)
    if not checked.valid:
        raise ValueError(f"Invalid input incumbent: {checked.violations}")
    initial_index = build_plan_index(problem, current)
    final_responsibility = dict(initial_index.delivery_route_by_task)
    initial_key, initial_cross = _key(checked.score, coefficients), _cross_count(initial_index)
    stats = dict(method="Directed-Dispersed-Pickup-2x-Relay", seed=seed, weights=list(weights), scales=list(scales),
        max_pairs_per_batch=max_pairs, sender_limit=SENDER_LIMIT, receiver_limit=RECEIVER_LIMIT,
        combination_limit=COMBINATION_LIMIT, fixed_station_cache=True, mobile_rendezvous=False,
        passes=0, batches=0, pairs_generated=0, pair_probes=0, stations_probed=0, sender_probes=0,
        sender_options_generated=0, receiver_options_generated=0, combinations_shortlisted=0,
        capacity_pruned=0, local_evaluations=0, lower_bound_pruned=0, global_evaluations=0,
        full_evaluations=1, rejected_cycles=0, rejected_invalid=0, duplicate_candidates_skipped=0,
        accepted_improvements=0, accepted_transfer_moves=0, accepted_control_only_moves=0,
        packages_transferred_in_accepted_moves=0, accepted_moves=[], deadline_reached=False,
        neighborhood_exhausted=False, initial_cross_uav_count=initial_cross)
    evaluator, global_evaluator = RouteEvaluator(problem), GlobalRelayEvaluator(problem)
    legs, seen, iterations = _leg_pairs(problem), {current}, 0
    while not _expired(deadline) and problem.has_relays and problem.capacity == 2:
        stats["passes"] += 1
        base, base_key = current, _key(checked.score, coefficients)
        index, schedule = build_plan_index(problem, base), global_evaluator.evaluate(base)
        if _expired(deadline):
            break
        local_base = tuple(evaluator.evaluate(r, start_node=problem.home_node(i)).score for i, r in enumerate(base))
        base_sum = Score(sum(s.late_count for s in local_base),
                         sum(s.total_lateness_min for s in local_base), sum(s.distance_km for s in local_base))
        pairs = _pairs(problem, base, index, schedule, legs, coefficients, seed, deadline)
        stats["pairs_generated"] += len(pairs)
        adopted = False
        for start in range(0, len(pairs), max_pairs):
            if _expired(deadline):
                break
            stats["batches"] += 1
            for pair in pairs[start:start + max_pairs]:
                if _expired(deadline):
                    break
                stats["pair_probes"] += 1
                for station in pair.stations:
                    if _expired(deadline):
                        break
                    stats["stations_probed"] += 1
                    receiving = _receiver_options(problem, evaluator, base, schedule, pair, station,
                        legs, coefficients, deadline, stats)
                    for owner in range(len(base)):
                        if _expired(deadline):
                            break
                        if owner == pair.owner:
                            continue
                        stats["sender_probes"] += 1
                        sending = _sender_options(problem, evaluator, base, schedule, owner, pair.tasks,
                            station, legs, coefficients, deadline, stats)
                        options = sorted(((a, b) for a in sending for b in receiving), key=lambda c: (
                            _key(c[0].score + c[1].score, coefficients),
                            max(0., c[0].arrival - c[1].arrival), c[0].first_position,
                            c[0].handoff_position, c[1].first_position, c[0].pickup_order))
                        for a, b in options[:COMBINATION_LIMIT]:
                            if _expired(deadline):
                                break
                            stats["combinations_shortlisted"] += 1
                            candidate = list(base)
                            candidate[owner], candidate[pair.owner] = a.route, b.route
                            candidate = tuple(candidate)
                            if candidate in seen:
                                stats["duplicate_candidates_skipped"] += 1
                                continue
                            seen.add(candidate)
                            iterations += 1
                            lower = base_sum - local_base[owner] - local_base[pair.owner] + a.score + b.score
                            if _key(lower, coefficients)[0] > _key(checked.score, coefficients)[0] + 1e-12:
                                stats["lower_bound_pruned"] += 1
                                continue
                            proposed = global_evaluator.evaluate(candidate)
                            stats["global_evaluations"] += 1
                            if _expired(deadline):
                                break
                            if not proposed.feasible:
                                stats["rejected_cycles"] += 1
                                continue
                            if _key(proposed.score, coefficients) >= _key(checked.score, coefficients):
                                continue
                            confirmation = evaluate_solution(problem, candidate, _force_full=True)
                            stats["full_evaluations"] += 1
                            if _expired(deadline):
                                break
                            if not confirmation.valid:
                                stats["rejected_invalid"] += 1
                                continue
                            if _key(confirmation.score, coefficients) >= _key(checked.score, coefficients):
                                continue
                            confirmed = build_plan_index(problem, candidate)
                            if dict(confirmed.delivery_route_by_task) != final_responsibility:
                                raise RuntimeError("Directed transfer changed final responsibility")
                            accepted_at = perf_counter()
                            if accepted_at >= deadline:
                                break
                            stats["accepted_moves"].append(dict(tasks=list(pair.tasks), sender=owner,
                                receiver=pair.owner, station=station, pickup_order=list(a.pickup_order),
                                sender_first_position=a.first_position, sender_handoff_position=a.handoff_position,
                                receiver_position=b.first_position, original_receiver_cuts=list(pair.original_cuts),
                                pass_number=stats["passes"], proposal_base_key=list(base_key),
                                before_key=list(_key(checked.score, coefficients)), after_key=list(_key(confirmation.score, coefficients)),
                                predicted_drop_min=a.arrival, predicted_pickup_min=b.arrival,
                                elapsed_seconds=accepted_at - started, accepted_at_perf_counter=accepted_at,
                                preserved_final_responsibility=True, preserved_other_job_routes_and_event_order=True))
                            current, checked, adopted = candidate, confirmation, True
                            stats["accepted_improvements"] += 1
                            stats["accepted_transfer_moves"] += 1
                            stats["packages_transferred_in_accepted_moves"] += 2
        if not adopted:
            stats["neighborhood_exhausted"] = not _expired(deadline)
            break
    if not checked.valid or _key(checked.score, coefficients) > initial_key:
        raise RuntimeError("Directed insertion lost the checked weighted incumbent")
    final_index = build_plan_index(problem, current)
    stats["deadline_reached"] = _expired(deadline)
    stats["final_cross_uav_count"] = _cross_count(final_index)
    stats["new_cross_uav_count"] = stats["final_cross_uav_count"] - initial_cross
    stats["final_responsibility_preserved"] = dict(final_index.delivery_route_by_task) == final_responsibility
    stats["initial_key"], stats["final_key"] = list(initial_key), list(_key(checked.score, coefficients))
    stats["runtime_seconds"] = perf_counter() - started
    return dict(routes=current, evaluation=checked, iterations=iterations, metadata=stats)
