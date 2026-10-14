"""Research-only route-tail crossover with physical cross-UAV cargo exchange.

Both cuts have completed the same number of final deliveries. Future complete
transport legs swap with the tails; packages carried at the cuts are converted
from DIRECT to one-hop relay through a common fixed station. Every station
approach, wait and subsequent delivery is part of the complete handoff DAG.
Disabling transfers restricts the same search to empty/empty tail exchanges.
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


def _coefficients(weights, scales):
    if len(weights) != 3 or len(scales) != 3 or any(
        not isfinite(value) or value <= 0 for value in (*weights, *scales)
    ):
        raise ValueError("Three finite positive objective weights/scales are required")
    return tuple(w / s for w, s in zip(weights, scales))


def _key(score, coefficients):
    physical = (score.late_count, score.total_lateness_min, score.distance_km)
    return (sum(c * value for c, value in zip(coefficients, physical)), *physical)


def _expired(deadline):
    return perf_counter() >= deadline


@dataclass(frozen=True)
class _Cut:
    position: int
    final_done: int
    active: tuple
    previous_node: int
    time: float


@dataclass(frozen=True)
class _Spec:
    a: int
    b: int
    left: _Cut
    right: _Cut
    station: int | None
    distance_delta: float
    ready: float
    priority: tuple


def _cuts(problem, route, route_index, schedule, *, enable_transfers):
    """An active old relay leg cannot be split into a second transfer."""
    active, final_done, result, rejected = [], 0, [], 0
    for position in range(len(route) + 1):
        old_relay_active = any(visit in problem.leg_registry for visit in active)
        if old_relay_active:
            rejected += 1
        elif not active or enable_transfers:
            result.append(_Cut(position, final_done, tuple(active),
                               problem._node_for_visit(route[position - 1]) if position
                               else problem.home_node(route_index),
                               schedule.event_times_min[(route_index, position - 1)]
                               if position else 0.))
        if position == len(route):
            break
        visit = route[position]
        if visit > 0:
            active.append(visit)
        else:
            active.remove(-visit)
            leg = problem.leg_registry.get(-visit)
            if leg is None or leg.kind == "RELAY_OUT":
                final_done += 1
    return tuple(result), rejected


def _leg_pairs(problem):
    result = {}
    for task_id, legs in problem._legs_by_task.items():
        allowed, stations = set(problem.task_relay_candidates.get(task_id, ())), {}
        for leg in legs:
            if leg.relay_id in allowed:
                stations.setdefault(leg.relay_id, {})[leg.kind] = leg.id
        result[task_id] = {station: (parts["RELAY_IN"], parts["RELAY_OUT"])
                           for station, parts in stations.items()
                           if set(parts) == {"RELAY_IN", "RELAY_OUT"}}
    return result


def _edge(problem, source_node, tail):
    return problem._distances[source_node][problem._node_for_visit(tail[0])] if tail else 0.


def _specifications(problem, routes, schedule, legs, coefficients, deadline,
                    enable_transfers, seed, stats):
    grouped = []
    for i, route in enumerate(routes):
        cuts, rejected = _cuts(problem, route, i, schedule, enable_transfers=enable_transfers)
        stats["active_old_relay_cuts_rejected"] += rejected
        by_done = {}
        for cut in cuts:
            by_done.setdefault(cut.final_done, []).append(cut)
        grouped.append(by_done)
    stations_by_id = {s.id: s for s in problem.relay_stations}
    result = []
    for a, b in combinations(range(len(routes)), 2):
        for done in sorted(set(grouped[a]) & set(grouped[b])):
            for left in grouped[a][done]:
                for right in grouped[b][done]:
                    if _expired(deadline):
                        return result
                    stats["cut_pairs_generated"] += 1
                    if left.active and right.active:
                        # Both old prefix->tail precedence edges remain via
                        # the respective drop->opposite pickup. Triangle
                        # inequality only lengthens those edges; all old
                        # dependencies remain. No positive-weight metric can
                        # improve, so keep the constructor for tests but do
                        # not spend search time on this dominated macro.
                        stats["both_loaded_pairs_pruned"] += 1
                        continue
                    active = left.active + right.active
                    if active:
                        common = set(stations_by_id)
                        for task_id in active:
                            common.intersection_update(legs.get(task_id, {}))
                        station_ids = sorted(common)
                    else:
                        station_ids = [None]
                    tail_a, tail_b = routes[a][left.position:], routes[b][right.position:]
                    old_edges = _edge(problem, left.previous_node, tail_a) + _edge(problem, right.previous_node, tail_b)
                    for station_id in station_ids:
                        if station_id is None:
                            if (left.position == right.position == 0 and
                                    problem._points[left.previous_node] == problem._points[right.previous_node]):
                                # Identical-home whole-route swaps only rename
                                # UAVs; summation tail noise is not a move.
                                stats["symmetric_home_swaps_skipped"] += 1
                                continue
                            delta = (_edge(problem, left.previous_node, tail_b)
                                     + _edge(problem, right.previous_node, tail_a) - old_edges)
                            ready = max(left.time, right.time)
                            approximate = coefficients[2] * delta
                        else:
                            station_node = problem._node_for_visit(legs[active[0]][station_id][1])
                            to_a = problem._distances[left.previous_node][station_node]
                            to_b = problem._distances[right.previous_node][station_node]
                            delta = (to_a + to_b + _edge(problem, station_node, tail_b)
                                     + _edge(problem, station_node, tail_a) - old_edges)
                            ready = max(left.time + to_a / problem.speed_km_per_min,
                                        right.time + to_b / problem.speed_km_per_min)
                            approximate = coefficients[2] * delta
                            station = stations_by_id[station_id]
                            for task_id in active:
                                task = problem.task(task_id)
                                earliest = ready + station.point.distance_to(task.delivery) / problem.speed_km_per_min
                                previous = schedule.delivery_times_min[task_id]
                                approximate += coefficients[0] * (
                                    int(earliest > task.deadline_min + 1e-9)
                                    - int(previous > task.deadline_min + 1e-9))
                                approximate += coefficients[1] * (
                                    max(0., earliest - task.deadline_min)
                                    - max(0., previous - task.deadline_min))
                        # A stable seed-dependent tie break only; no random
                        # reranking after measuring an outcome.
                        tie = ((a * 31 + b * 17 + left.position * 13 + right.position * 7
                                + (station_id or 0) * 5 + seed) % 104729)
                        priority = (approximate, delta, ready, tie, a, b,
                                    left.position, right.position, station_id or 0)
                        result.append(_Spec(a, b, left, right, station_id, delta, ready, priority))
    result.sort(key=lambda spec: spec.priority)
    return result


def _candidate(problem, routes, spec, legs):
    left, right = spec.left, spec.right
    prefix_a, tail_a = routes[spec.a][:left.position], routes[spec.a][left.position:]
    prefix_b, tail_b = routes[spec.b][:right.position], routes[spec.b][right.position:]
    if spec.station is None:
        changed_a, changed_b = prefix_a + tail_b, prefix_b + tail_a
    else:
        replacements = {}
        for task_id in left.active + right.active:
            inbound, outbound = legs[task_id][spec.station]
            replacements[task_id], replacements[-task_id] = inbound, -outbound
        convert = lambda route: tuple(replacements.get(visit, visit) for visit in route)
        drop_a = tuple(-legs[t][spec.station][0] for t in left.active)
        drop_b = tuple(-legs[t][spec.station][0] for t in right.active)
        pickup_a = tuple(legs[t][spec.station][1] for t in right.active)
        pickup_b = tuple(legs[t][spec.station][1] for t in left.active)
        changed_a = convert(prefix_a) + drop_a + pickup_a + convert(tail_b)
        changed_b = convert(prefix_b) + drop_b + pickup_b + convert(tail_a)
    result = list(routes)
    result[spec.a], result[spec.b] = changed_a, changed_b
    return tuple(result)


def _cross_count(index):
    return sum(index.inbound_route_by_task[t] != index.outbound_route_by_task[t]
               for t in index.relay_task_ids)


def refine(problem, routes, *, deadline, weights, scales, seed=2026101481,
           max_pairs=256, enable_transfers=True):
    """Checked weighted descent over equal-delivery-prefix cut pairs.

    ``max_pairs`` is the number of cut/station specifications per processing
    batch. Without improvement, subsequent batches are scanned until the
    current list is exhausted or the absolute deadline expires. On improvement
    the next pass regenerates cuts and actual clocks from the best checkpoint.
    Each pass freezes its proposal base while timely checked candidates update
    best-so-far, so an early empty-tail improvement cannot hide a better cargo
    transfer from the same base. Memoized complete candidates are
    never reevaluated. Local scores ignore handoff waiting and only prune when
    their positive-weight scalar lower bound exceeds incumbent J + 1e-12.
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
    index = build_plan_index(problem, current)
    initial_cross, initial_key = _cross_count(index), _key(checked.score, coefficients)
    stats = dict(method="Relay-Tail-Crossover", seed=seed, weights=list(weights), scales=list(scales),
                 enable_transfers=bool(enable_transfers), max_pairs_per_batch=max_pairs,
                 passes=0, batches=0, cut_pairs_generated=0, specifications_generated=0,
                 specifications_considered=0, active_old_relay_cuts_rejected=0,
                 symmetric_home_swaps_skipped=0,
                 both_loaded_pairs_pruned=0,
                 local_evaluations=0, lower_bound_pruned=0, global_evaluations=0,
                 full_evaluations=1, rejected_cycles=0, rejected_invalid=0,
                 duplicate_candidates_skipped=0, accepted_improvements=0,
                 accepted_transfer_moves=0, accepted_control_only_moves=0,
                 packages_transferred_in_accepted_moves=0, accepted_moves=[],
                 deadline_reached=False, neighborhood_exhausted=False,
                 initial_cross_uav_count=initial_cross)
    evaluator, global_evaluator = RouteEvaluator(problem), GlobalRelayEvaluator(problem)
    legs, seen, iterations = _leg_pairs(problem), {current}, 0
    while not _expired(deadline):
        stats["passes"] += 1
        proposal_base = current
        proposal_base_key = _key(checked.score, coefficients)
        schedule = global_evaluator.evaluate(proposal_base)
        if _expired(deadline):
            break
        local_base = tuple(evaluator.evaluate(route, start_node=problem.home_node(i)).score
                           for i, route in enumerate(proposal_base))
        base_sum = Score(sum(s.late_count for s in local_base),
                         sum(s.total_lateness_min for s in local_base),
                         sum(s.distance_km for s in local_base))
        specs = _specifications(problem, proposal_base, schedule, legs, coefficients, deadline,
                                enable_transfers, seed, stats)
        stats["specifications_generated"] += len(specs)
        adopted = False
        for batch_start in range(0, len(specs), max_pairs):
            if _expired(deadline):
                break
            stats["batches"] += 1
            for spec in specs[batch_start:batch_start + max_pairs]:
                if _expired(deadline):
                    break
                stats["specifications_considered"] += 1
                candidate = _candidate(problem, proposal_base, spec, legs)
                if candidate in seen:
                    stats["duplicate_candidates_skipped"] += 1
                    continue
                seen.add(candidate)
                iterations += 1
                try:
                    score_a = evaluator.evaluate(candidate[spec.a], start_node=problem.home_node(spec.a)).score
                    score_b = evaluator.evaluate(candidate[spec.b], start_node=problem.home_node(spec.b)).score
                except ValueError:
                    stats["rejected_invalid"] += 1
                    continue
                stats["local_evaluations"] += 2
                if _expired(deadline):
                    break
                lower_bound = base_sum - local_base[spec.a] - local_base[spec.b] + score_a + score_b
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
                accepted_at = perf_counter()
                if accepted_at >= deadline:
                    break
                transferred = len(spec.left.active) + len(spec.right.active)
                stats["accepted_moves"].append(dict(route_a=spec.a, route_b=spec.b,
                    cut_a=spec.left.position, cut_b=spec.right.position,
                    final_done=spec.left.final_done, station=spec.station,
                    cross_tasks=list(spec.left.active + spec.right.active),
                    cargo_a=list(spec.left.active), cargo_b=list(spec.right.active),
                    pass_number=stats["passes"], proposal_base_key=list(proposal_base_key),
                    geometric_delta_km=spec.distance_delta, predicted_station_ready_min=spec.ready,
                    before_key=list(_key(checked.score, coefficients)),
                    after_key=list(_key(confirmation.score, coefficients)),
                    elapsed_seconds=accepted_at - started, accepted_at_perf_counter=accepted_at))
                stats["accepted_improvements"] += 1
                stats["accepted_transfer_moves" if transferred else "accepted_control_only_moves"] += 1
                stats["packages_transferred_in_accepted_moves"] += transferred
                current, checked, adopted = candidate, confirmation, True
        if not adopted:
            stats["neighborhood_exhausted"] = not _expired(deadline)
            break
    if not checked.valid or _key(checked.score, coefficients) > initial_key:
        raise RuntimeError("Tail refinement lost the checked weighted incumbent")
    final_index = build_plan_index(problem, current)
    stats["deadline_reached"] = _expired(deadline)
    stats["final_cross_uav_count"] = _cross_count(final_index)
    stats["new_cross_uav_count"] = stats["final_cross_uav_count"] - initial_cross
    stats["initial_key"], stats["final_key"] = list(initial_key), list(_key(checked.score, coefficients))
    stats["runtime_seconds"] = perf_counter() - started
    return dict(routes=current, evaluation=checked, iterations=iterations, metadata=stats)
