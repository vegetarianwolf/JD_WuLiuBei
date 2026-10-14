"""Experimental two-package, true cross-UAV relay refinement.

This module never changes production search or model defaults. A move removes
two complete original-task plans with the same final-delivery UAV and inserts
an inbound capacity-two block on another UAV and an outbound block on the
original UAV. All empty travel, waiting and affected deliveries are scored on
the complete handoff DAG. Local clocks and geometry only rank shortlists.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import isfinite
from random import Random
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.validation import evaluate_solution


POSITION_LIMIT = 4
COMBINATION_LIMIT = 8


def _coefficients(weights, scales):
    if len(weights) != 3 or len(scales) != 3 or any(
        not isfinite(value) or value <= 0 for value in (*weights, *scales)
    ):
        raise ValueError("Three finite positive objective weights/scales are required")
    return tuple(weight / scale for weight, scale in zip(weights, scales))


def _key(score, coefficients):
    physical = (score.late_count, score.total_lateness_min, score.distance_km)
    return (sum(c * value for c, value in zip(coefficients, physical)), *physical)


def _expired(deadline):
    return perf_counter() >= deadline


def _zero_gaps(route):
    """Positions before which no physical transport leg is on the UAV."""
    load = 0
    result = [0]
    for position, visit in enumerate(route, 1):
        load += 1 if visit > 0 else -1
        if load == 0:
            result.append(position)
    return tuple(result)


def _remove_tasks(problem, routes, task_ids):
    removed = set(task_ids)
    return tuple(tuple(visit for visit in route if (
        problem.leg_registry[abs(visit)].task_id
        if abs(visit) in problem.leg_registry else abs(visit)
    ) not in removed) for route in routes)


def _block_distance(problem, route, route_index, position, block):
    previous = route[position - 1] if position else None
    distance = 0.
    for visit in block:
        distance += problem.distance(previous, visit, from_node=(
            problem.home_node(route_index) if previous is None else None
        ))
        previous = visit
    block_to_last = distance
    if position < len(route):
        distance += problem.distance(previous, route[position])
        distance -= problem.distance(route[position - 1] if position else None,
                                     route[position], from_node=(
            problem.home_node(route_index) if position == 0 else None
        ))
    return distance, block_to_last


@dataclass(frozen=True)
class _Gap:
    route: tuple
    position: int
    score: Score
    distance_delta: float
    station_time: float
    new_deliveries: tuple


def _block_options(problem, evaluator, routes, route_index, block, schedule,
                   coefficients, deadline, stats, *, inbound):
    """Keep bounded zero-load gaps by local score, including actual prefix time."""
    route = routes[route_index]
    ranked = []
    for position in _zero_gaps(route):
        if _expired(deadline):
            break
        changed = route[:position] + block + route[position:]
        delta, block_distance = _block_distance(problem, route, route_index, position, block)
        metrics = evaluator.evaluate(changed, start_node=problem.home_node(route_index))
        if _expired(deadline):
            break
        prefix_time = schedule.event_times_min[(route_index, position - 1)] if position else 0.
        station_time = (prefix_time + block_distance / problem.speed_km_per_min if inbound
                        else prefix_time + problem.distance(
                            route[position - 1] if position else None, block[0],
                            from_node=problem.home_node(route_index) if position == 0 else None,
                        ) / problem.speed_km_per_min)
        new_deliveries = tuple((problem.leg_registry[abs(v)].task_id,
                                metrics.delivery_times_min[problem.leg_registry[abs(v)].task_id])
                               for v in block if v < 0 and
                               problem.leg_registry[abs(v)].kind == "RELAY_OUT")
        option = _Gap(changed, position, metrics.score, delta, station_time, new_deliveries)
        stats["block_options_generated"] += 1
        ranked.append(option)
    ranked.sort(key=lambda o: (_key(o.score, coefficients), o.distance_delta,
                               o.station_time, o.position))
    return tuple(ranked[:POSITION_LIMIT])


def _combo_key(problem, left, right, coefficients):
    """Approximate clocks rank only; waiting can propagate beyond these two tasks."""
    wait = max(0., left.station_time - right.station_time)
    score = left.score + right.score
    extra_late, extra_lateness = 0, 0.
    for task_id, delivered in right.new_deliveries:
        old = max(0., delivered - problem.task(task_id).deadline_min)
        new = max(0., delivered + wait - problem.task(task_id).deadline_min)
        extra_late += int(new > 1e-9) - int(old > 1e-9)
        extra_lateness += new - old
    estimated = score + Score(extra_late, extra_lateness, 0.)
    return (_key(estimated, coefficients), wait, left.distance_delta + right.distance_delta,
            left.position, right.position)


def _leg_pairs(problem):
    result = {}
    for task_id, legs in problem._legs_by_task.items():
        allowed = set(problem.task_relay_candidates.get(task_id, ()))
        stations = {}
        for leg in legs:
            if leg.relay_id in allowed:
                stations.setdefault(leg.relay_id, {})[leg.kind] = leg.id
        result[task_id] = {station: (parts["RELAY_IN"], parts["RELAY_OUT"])
                           for station, parts in stations.items()
                           if set(parts) == {"RELAY_IN", "RELAY_OUT"}}
    return result


def _select_pairs(problem, routes, index, schedule, legs, coefficients, rng, limit):
    """Task-pair choice is deterministic for the seed, not outcome-based resampling."""
    ranked = []
    for delivery_route in range(len(routes)):
        tasks = sorted(task_id for task_id, i in index.delivery_route_by_task.items()
                       if i == delivery_route and legs.get(task_id))
        for first, second in combinations(tasks, 2):
            common = tuple(sorted(set(legs[first]) & set(legs[second])))
            if not common:
                continue
            a, b = problem.task(first), problem.task(second)
            risk = sum(coefficients[0] * int(schedule.delivery_times_min[t] >
                            problem.task(t).deadline_min + 1e-9)
                       + coefficients[1] * max(0., schedule.delivery_times_min[t]
                            - problem.task(t).deadline_min) for t in (first, second))
            compactness = a.pickup.distance_to(b.pickup) + a.delivery.distance_to(b.delivery)
            ranked.append(((-risk, compactness, rng.random(), first, second),
                           delivery_route, first, second, common))
    ranked.sort(key=lambda row: row[0])
    return tuple(row[1:] for row in ranked[:limit]), len(ranked)


def _cross_count(index):
    return sum(index.inbound_route_by_task[t] != index.outbound_route_by_task[t]
               for t in index.relay_task_ids)


def refine(problem, routes, *, deadline, weights, scales, seed=2026101481, max_pairs=32):
    """Return the fully checked weighted incumbent; accept only before deadline.

    ``max_pairs`` caps pair probes per incumbent, not a claim of exhaustive
    neighborhood search. Two pickup orders, two delivery orders, all available
    common fixed stations and every other UAV are considered. At most four
    positions per block and eight combinations per order enter global scoring.
    Local shortlist scores never reject a globally improving candidate.
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
    initial_key = _key(checked.score, coefficients)
    initial_index = build_plan_index(problem, current)
    initial_cross = _cross_count(initial_index)
    original_responsibility = dict(initial_index.delivery_route_by_task)
    stats = dict(method="Capacity2-Batch-Cross-Relay", seed=seed, max_pairs=max_pairs,
                 weights=list(weights), scales=list(scales),
                 position_limit=POSITION_LIMIT, combination_limit=COMBINATION_LIMIT,
                 passes=0, pairs_available=0, pair_probes=0, block_options_generated=0,
                 combinations_shortlisted=0, global_evaluations=0, full_evaluations=1,
                 accepted_improvements=0, rejected_cycles=0, rejected_invalid=0,
                 duplicate_candidates_skipped=0, deadline_reached=False,
                 initial_cross_uav_count=initial_cross, accepted_moves=[])
    if problem.capacity < 2 or not problem.has_relays or _expired(deadline):
        stats["deadline_reached"] = _expired(deadline)
        stats["final_cross_uav_count"] = initial_cross
        stats["new_cross_uav_count"] = 0
        stats["final_responsibility_preserved"] = True
        stats["initial_key"], stats["final_key"] = list(initial_key), list(initial_key)
        stats["runtime_seconds"] = perf_counter() - started
        return dict(routes=current, evaluation=checked, iterations=0, metadata=stats)
    evaluator, global_evaluator = RouteEvaluator(problem), GlobalRelayEvaluator(problem)
    legs, rng, seen = _leg_pairs(problem), Random(seed), {current}
    iterations = 0
    while not _expired(deadline):
        stats["passes"] += 1
        index = build_plan_index(problem, current)
        schedule = global_evaluator.evaluate(current)
        pairs, available = _select_pairs(problem, current, index, schedule, legs,
                                        coefficients, rng, max_pairs)
        stats["pairs_available"] = available
        adopted = False
        for delivery_route, first, second, stations in pairs:
            if _expired(deadline):
                break
            stats["pair_probes"] += 1
            reduced = _remove_tasks(problem, current, (first, second))
            reduced_schedule = global_evaluator.evaluate(reduced)
            if _expired(deadline):
                break
            for station in stations:
                if adopted or _expired(deadline):
                    break
                i1, o1 = legs[first][station]
                i2, o2 = legs[second][station]
                for inbound_route in range(len(current)):
                    if adopted or _expired(deadline):
                        break
                    if inbound_route == delivery_route:
                        continue
                    for left_order in ((i1, i2), (i2, i1)):
                        if adopted or _expired(deadline):
                            break
                        left_block = (*left_order, -left_order[0], -left_order[1])
                        left_options = _block_options(problem, evaluator, reduced, inbound_route,
                            left_block, reduced_schedule, coefficients, deadline, stats, inbound=True)
                        for right_order in ((o1, o2), (o2, o1)):
                            if adopted or _expired(deadline):
                                break
                            right_block = (*right_order, -right_order[0], -right_order[1])
                            right_options = _block_options(problem, evaluator, reduced, delivery_route,
                                right_block, reduced_schedule, coefficients, deadline, stats, inbound=False)
                            combos = sorted(((left, right) for left in left_options
                                             for right in right_options),
                                key=lambda options: _combo_key(problem, *options, coefficients))
                            for left, right in combos[:COMBINATION_LIMIT]:
                                if _expired(deadline):
                                    break
                                stats["combinations_shortlisted"] += 1
                                candidate = list(reduced)
                                candidate[inbound_route], candidate[delivery_route] = left.route, right.route
                                candidate = tuple(candidate)
                                if candidate in seen:
                                    stats["duplicate_candidates_skipped"] += 1
                                    continue
                                seen.add(candidate)
                                iterations += 1
                                candidate_schedule = global_evaluator.evaluate(candidate)
                                stats["global_evaluations"] += 1
                                if _expired(deadline):
                                    break
                                if not candidate_schedule.feasible:
                                    stats["rejected_cycles"] += 1
                                    continue
                                if _key(candidate_schedule.score, coefficients) >= _key(checked.score, coefficients):
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
                                confirmed_index = build_plan_index(problem, candidate)
                                if dict(confirmed_index.delivery_route_by_task) != original_responsibility:
                                    raise RuntimeError("Batch move changed final-delivery responsibility")
                                if _expired(deadline):
                                    break
                                accepted_at = perf_counter()
                                if accepted_at >= deadline:
                                    break
                                stats["accepted_moves"].append(dict(tasks=[first, second],
                                    inbound_route=inbound_route, delivery_route=delivery_route, station=station,
                                    before_key=list(_key(checked.score, coefficients)),
                                    after_key=list(_key(confirmation.score, coefficients)),
                                    elapsed_seconds=accepted_at - started,
                                    accepted_at_perf_counter=accepted_at))
                                current, checked, adopted = candidate, confirmation, True
                                stats["accepted_improvements"] += 1
                                break
            if adopted:
                break
        if not adopted:
            break
    if not checked.valid or _key(checked.score, coefficients) > initial_key:
        raise RuntimeError("Batch refinement lost the checked weighted incumbent")
    final_index = build_plan_index(problem, current)
    stats["deadline_reached"] = _expired(deadline)
    stats["final_cross_uav_count"] = _cross_count(final_index)
    stats["new_cross_uav_count"] = stats["final_cross_uav_count"] - initial_cross
    stats["final_responsibility_preserved"] = (
        dict(final_index.delivery_route_by_task) == original_responsibility)
    stats["initial_key"], stats["final_key"] = list(initial_key), list(_key(checked.score, coefficients))
    stats["runtime_seconds"] = perf_counter() - started
    return dict(routes=current, evaluation=checked, iterations=iterations, metadata=stats)
