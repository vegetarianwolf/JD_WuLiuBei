"""Research-only reciprocal handoff of two in-flight DIRECT packages.

Two original pickup events stay at their physical locations. At a fixed
station each UAV drops its package and takes the other's; the two existing
delivery positions become the other package's delivery. All other jobs keep
their UAV and event order, including any other transport leg already aboard.
This preserves load and per-UAV final responsibility counts without moving
whole tails or claiming a DIRECT improvement as a relay improvement.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import isfinite
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.validation import evaluate_solution


CUT_LIMIT = 4
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


@dataclass(frozen=True)
class _Pair:
    first: int
    second: int
    a: int
    b: int
    pickup_a: int
    delivery_a: int
    pickup_b: int
    delivery_b: int
    stations: tuple
    replacement_delta: float
    priority: tuple


@dataclass(frozen=True)
class _Cut:
    position: int
    route: tuple
    score: Score
    detour: float
    arrival: float


def _replacement_delta(problem, route, route_index, position, new_delivery):
    previous_node = problem._node_for_visit(route[position - 1]) if position else problem.home_node(route_index)
    old_node, new_node = problem._node_for_visit(route[position]), problem._node_for_visit(new_delivery)
    delta = problem._distances[previous_node][new_node] - problem._distances[previous_node][old_node]
    if position + 1 < len(route):
        following = problem._node_for_visit(route[position + 1])
        delta += problem._distances[new_node][following] - problem._distances[old_node][following]
    return delta


def _pairs(problem, routes, index, schedule, legs, coefficients, seed, deadline):
    positions = {}
    for t in index.direct_task_ids:
        if legs.get(t):
            owner = index.delivery_route_by_task[t]
            positions[t] = (owner, routes[owner].index(t), routes[owner].index(-t))
    result = []
    for first, second in combinations(sorted(positions), 2):
        if _expired(deadline):
            break
        a, pa, da = positions[first]
        b, pb, db = positions[second]
        if a == b:
            continue
        stations = tuple(sorted(set(legs[first]) & set(legs[second])))
        if not stations:
            continue
        delta = (_replacement_delta(problem, routes[a], a, da, -second)
                 + _replacement_delta(problem, routes[b], b, db, -first))
        estimated = coefficients[2] * delta
        for task_id, new_time in ((first, schedule.delivery_times_min[second]),
                                  (second, schedule.delivery_times_min[first])):
            old_time, deadline_task = schedule.delivery_times_min[task_id], problem.task(task_id).deadline_min
            estimated += coefficients[0] * (int(new_time > deadline_task + 1e-9)
                                             - int(old_time > deadline_task + 1e-9))
            estimated += coefficients[1] * (max(0., new_time - deadline_task)
                                             - max(0., old_time - deadline_task))
        tie = (first * 31 + second * 17 + seed) % 104729
        result.append(_Pair(first, second, a, b, pa, da, pb, db, stations, delta,
                            (estimated, delta, tie, first, second)))
    result.sort(key=lambda p: p.priority)
    return result


def _changed_route(route, task_id, inbound, received_outbound, cut):
    """Keep every unrelated encoded event in its original relative order."""
    substituted = tuple(inbound if v == task_id else -received_outbound if v == -task_id
                        else v for v in route)
    return substituted[:cut] + (-inbound, received_outbound) + substituted[cut:]


def _cut_options(problem, evaluator, routes, schedule, route_index, task_id,
                 inbound, received_outbound, pickup, delivery, coefficients,
                 deadline, stats):
    original = routes[route_index]
    replaced = tuple(inbound if v == task_id else -received_outbound if v == -task_id
                     else v for v in original)
    station_node = problem._node_for_visit(-inbound)
    options = []
    # Own original package is on board at every such cut. The drop and take
    # change load by -1,+1, so another in-flight DIRECT or old relay leg may
    # safely stay aboard without being moved or relayed a second time.
    for cut in range(pickup + 1, delivery + 1):
        if _expired(deadline):
            break
        previous = problem._node_for_visit(replaced[cut - 1])
        following = problem._node_for_visit(replaced[cut])
        detour = (problem._distances[previous][station_node]
                  + problem._distances[station_node][following]
                  - problem._distances[previous][following])
        arrival = schedule.event_times_min[(route_index, cut - 1)] + (
            problem._distances[previous][station_node] / problem.speed_km_per_min)
        candidate = replaced[:cut] + (-inbound, received_outbound) + replaced[cut:]
        metrics = evaluator.evaluate(candidate, start_node=problem.home_node(route_index))
        stats["local_evaluations"] += 1
        if _expired(deadline):
            break
        options.append(_Cut(cut, candidate, metrics.score, detour, arrival))
        stats["cut_options_generated"] += 1
    options.sort(key=lambda c: (_key(c.score, coefficients), c.detour, c.arrival, c.position))
    return tuple(options[:CUT_LIMIT])


def _cross_count(index):
    return sum(index.inbound_route_by_task[t] != index.outbound_route_by_task[t]
               for t in index.relay_task_ids)


def refine(problem, routes, *, deadline, weights, scales, seed=2026101481, max_pairs=256):
    """Weighted checked-best search, with no direct-only candidate family.

    Each pass freezes its proposal base. Pairs stream in batches of
    ``max_pairs`` until exhausted or the deadline; at most four cuts per side
    and eight combinations per common fixed station enter exact screening.
    Route-local scores give an absolute positive-weight lower bound ignoring
    handoff waiting; every potentially improving full plan receives global
    DAG evaluation and independent full constraint/metric validation.
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
    initial_cross = _cross_count(build_plan_index(problem, current))
    stats = dict(method="In-Flight-Reciprocal-Relay-Swap", seed=seed, weights=list(weights), scales=list(scales),
        max_pairs_per_batch=max_pairs, cut_limit=CUT_LIMIT, combination_limit=COMBINATION_LIMIT,
        passes=0, batches=0, pairs_generated=0, pair_probes=0, cut_options_generated=0,
        combinations_shortlisted=0, local_evaluations=0, lower_bound_pruned=0, global_evaluations=0,
        full_evaluations=1, rejected_cycles=0, rejected_invalid=0, duplicate_candidates_skipped=0,
        accepted_improvements=0, accepted_transfer_moves=0, accepted_control_only_moves=0,
        packages_transferred_in_accepted_moves=0, accepted_moves=[], deadline_reached=False,
        neighborhood_exhausted=False, initial_cross_uav_count=initial_cross)
    evaluator, global_evaluator = RouteEvaluator(problem), GlobalRelayEvaluator(problem)
    legs, seen, iterations = _leg_pairs(problem), {current}, 0
    while not _expired(deadline) and problem.has_relays:
        stats["passes"] += 1
        base, base_key = current, _key(checked.score, coefficients)
        index = build_plan_index(problem, base)
        schedule = global_evaluator.evaluate(base)
        if _expired(deadline):
            break
        local_base = tuple(evaluator.evaluate(r, start_node=problem.home_node(i)).score
                           for i, r in enumerate(base))
        base_sum = Score(sum(s.late_count for s in local_base),
                         sum(s.total_lateness_min for s in local_base), sum(s.distance_km for s in local_base))
        pairs = _pairs(problem, base, index, schedule, legs, coefficients, seed, deadline)
        stats["pairs_generated"] += len(pairs)
        adopted = False
        for batch_start in range(0, len(pairs), max_pairs):
            if _expired(deadline):
                break
            stats["batches"] += 1
            for pair in pairs[batch_start:batch_start + max_pairs]:
                if _expired(deadline):
                    break
                stats["pair_probes"] += 1
                for station in pair.stations:
                    if _expired(deadline):
                        break
                    in_i, out_i = legs[pair.first][station]
                    in_j, out_j = legs[pair.second][station]
                    left = _cut_options(problem, evaluator, base, schedule, pair.a, pair.first,
                        in_i, out_j, pair.pickup_a, pair.delivery_a, coefficients, deadline, stats)
                    right = _cut_options(problem, evaluator, base, schedule, pair.b, pair.second,
                        in_j, out_i, pair.pickup_b, pair.delivery_b, coefficients, deadline, stats)
                    options = sorted(((a, b) for a in left for b in right), key=lambda c: (
                        _key(c[0].score + c[1].score, coefficients),
                        abs(c[0].arrival - c[1].arrival), c[0].detour + c[1].detour,
                        max(c[0].arrival, c[1].arrival), c[0].position, c[1].position))
                    for a, b in options[:COMBINATION_LIMIT]:
                        if _expired(deadline):
                            break
                        stats["combinations_shortlisted"] += 1
                        candidate = list(base)
                        candidate[pair.a], candidate[pair.b] = a.route, b.route
                        candidate = tuple(candidate)
                        if candidate in seen:
                            stats["duplicate_candidates_skipped"] += 1
                            continue
                        seen.add(candidate)
                        iterations += 1
                        lower_bound = base_sum - local_base[pair.a] - local_base[pair.b] + a.score + b.score
                        if _key(lower_bound, coefficients)[0] > _key(checked.score, coefficients)[0] + 1e-12:
                            stats["lower_bound_pruned"] += 1
                            continue
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
                        confirmed = build_plan_index(problem, candidate)
                        expected = dict(index.delivery_route_by_task)
                        expected[pair.first], expected[pair.second] = pair.b, pair.a
                        if dict(confirmed.delivery_route_by_task) != expected:
                            raise RuntimeError("In-flight move changed an unrelated final responsibility")
                        accepted_at = perf_counter()
                        if accepted_at >= deadline:
                            break
                        stats["accepted_moves"].append(dict(tasks=[pair.first, pair.second],
                            owner_a=pair.a, owner_b=pair.b, cut_a=a.position, cut_b=b.position, station=station,
                            pickup_positions=[pair.pickup_a, pair.pickup_b],
                            delivery_positions=[pair.delivery_a, pair.delivery_b], pass_number=stats["passes"],
                            proposal_base_key=list(base_key), before_key=list(_key(checked.score, coefficients)),
                            after_key=list(_key(confirmation.score, coefficients)),
                            delivery_replacement_delta_km=pair.replacement_delta,
                            station_detour_km=a.detour + b.detour,
                            predicted_arrival_a_min=a.arrival, predicted_arrival_b_min=b.arrival,
                            elapsed_seconds=accepted_at - started, accepted_at_perf_counter=accepted_at,
                            preserved_all_other_job_routes_and_event_order=True))
                        current, checked, adopted = candidate, confirmation, True
                        stats["accepted_improvements"] += 1
                        stats["accepted_transfer_moves"] += 1
                        stats["packages_transferred_in_accepted_moves"] += 2
        if not adopted:
            stats["neighborhood_exhausted"] = not _expired(deadline)
            break
    if not checked.valid or _key(checked.score, coefficients) > initial_key:
        raise RuntimeError("In-flight refinement lost the checked weighted incumbent")
    final_index = build_plan_index(problem, current)
    stats["deadline_reached"] = _expired(deadline)
    stats["final_cross_uav_count"] = _cross_count(final_index)
    stats["new_cross_uav_count"] = stats["final_cross_uav_count"] - initial_cross
    stats["initial_key"], stats["final_key"] = list(initial_key), list(_key(checked.score, coefficients))
    stats["runtime_seconds"] = perf_counter() - started
    return dict(routes=current, evaluation=checked, iterations=iterations, metadata=stats)
