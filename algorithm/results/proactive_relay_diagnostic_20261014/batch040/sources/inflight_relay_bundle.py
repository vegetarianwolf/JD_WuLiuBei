"""Research-only reciprocal swap of two packages actually carried per UAV.

The two groups come from real capacity-two DIRECT cargo frontiers. Original
pickup points remain on their original UAV; the two delivery slots receive
the other group's packages in either order. Both UAVs unload their two items
before taking the other's two at one registered fixed station. Every other
job keeps its UAV, encoded events and relative order. No whole tail is moved.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, permutations, product
from math import isfinite
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.validation import evaluate_solution


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
class _Group:
    owner: int
    tasks: tuple
    cuts: tuple
    pickup_positions: tuple
    slot_tasks: tuple
    delivery_positions: tuple


@dataclass(frozen=True)
class _Mapping:
    received_a: tuple
    received_b: tuple
    replacements_a: tuple
    replacements_b: tuple
    distance_delta: float
    priority: tuple


@dataclass(frozen=True)
class _Pair:
    left: _Group
    right: _Group
    stations: tuple
    mappings: tuple
    priority: tuple


@dataclass(frozen=True)
class _Cut:
    position: int
    route: tuple
    score: Score
    detour: float
    arrival: float


def _groups(problem, routes, index, legs):
    """Only groups witnessed simultaneously aboard are eligible, not arbitrary pairs."""
    direct = set(index.direct_task_ids)
    result = []
    for owner, route in enumerate(routes):
        active, frontiers = [], {}
        for position in range(len(route) + 1):
            if len(active) == 2 and all(t in direct and legs.get(t) for t in active):
                frontiers.setdefault(tuple(active), []).append(position)
            if position == len(route):
                break
            visit = route[position]
            if visit > 0:
                active.append(visit)
            else:
                active.remove(-visit)
        for tasks, cuts in sorted(frontiers.items()):
            slots = tuple(sorted(tasks, key=lambda t: route.index(-t)))
            result.append(_Group(owner, tasks, tuple(cuts), tuple(route.index(t) for t in tasks),
                                 slots, tuple(route.index(-t) for t in slots)))
    return tuple(result)


def _distance(problem, route, owner):
    previous, total = problem.home_node(owner), 0.
    for visit in route:
        node = problem._node_for_visit(visit)
        total += problem._distances[previous][node]
        previous = node
    return total


def _mapping(problem, routes, schedule, left, right, received_a, received_b, coefficients):
    replacements_a = tuple((-task, -received) for task, received in zip(left.slot_tasks, received_a))
    replacements_b = tuple((-task, -received) for task, received in zip(right.slot_tasks, received_b))
    replace_a, replace_b = dict(replacements_a), dict(replacements_b)
    route_a = tuple(replace_a.get(v, v) for v in routes[left.owner])
    route_b = tuple(replace_b.get(v, v) for v in routes[right.owner])
    delta = (_distance(problem, route_a, left.owner) + _distance(problem, route_b, right.owner)
             - _distance(problem, routes[left.owner], left.owner)
             - _distance(problem, routes[right.owner], right.owner))
    estimated = coefficients[2] * delta
    for group, received in ((left, received_a), (right, received_b)):
        for own_slot_task, received_task in zip(group.slot_tasks, received):
            old_time, new_time = schedule.delivery_times_min[received_task], schedule.delivery_times_min[own_slot_task]
            deadline = problem.task(received_task).deadline_min
            estimated += coefficients[0] * (int(new_time > deadline + 1e-9) - int(old_time > deadline + 1e-9))
            estimated += coefficients[1] * (max(0., new_time - deadline) - max(0., old_time - deadline))
    return _Mapping(received_a, received_b, replacements_a, replacements_b, delta,
                    (estimated, delta, received_a, received_b))


def _pairs(problem, routes, schedule, groups, legs, coefficients, seed, deadline):
    result = []
    for left, right in combinations(groups, 2):
        if _expired(deadline):
            break
        if left.owner == right.owner:
            continue
        common = set(legs[left.tasks[0]])
        for t in left.tasks + right.tasks:
            common.intersection_update(legs[t])
        if not common:
            continue
        mappings = []
        for received_a, received_b in product(permutations(right.tasks), permutations(left.tasks)):
            mappings.append(_mapping(problem, routes, schedule, left, right,
                                     received_a, received_b, coefficients))
        mappings.sort(key=lambda m: m.priority)
        tie = (sum(left.tasks) * 31 + sum(right.tasks) * 17 + seed) % 104729
        result.append(_Pair(left, right, tuple(sorted(common)), tuple(mappings),
                            (mappings[0].priority[0], mappings[0].distance_delta,
                             tie, left.owner, right.owner, left.tasks, right.tasks)))
    result.sort(key=lambda p: p.priority)
    return result


def _changed_route(problem, route, group, received, cut, station, legs):
    replacements = {task: legs[task][station][0] for task in group.tasks}
    replacements.update({-own: -legs[other][station][1]
                         for own, other in zip(group.slot_tasks, received)})
    substituted = tuple(replacements.get(v, v) for v in route)
    drop = tuple(-legs[t][station][0] for t in group.tasks)
    pickup = tuple(legs[t][station][1] for t in received)
    return substituted[:cut] + drop + pickup + substituted[cut:]


def _cut_options(problem, evaluator, routes, schedule, group, received, station,
                 legs, coefficients, deadline, stats):
    route = routes[group.owner]
    replacements = {-own: -legs[other][station][1] for own, other in zip(group.slot_tasks, received)}
    replacements.update({t: legs[t][station][0] for t in group.tasks})
    substituted = tuple(replacements.get(v, v) for v in route)
    station_node = problem._node_for_visit(-legs[group.tasks[0]][station][0])
    result = []
    for cut in group.cuts:
        if _expired(deadline):
            break
        previous, following = problem._node_for_visit(substituted[cut - 1]), problem._node_for_visit(substituted[cut])
        detour = (problem._distances[previous][station_node] + problem._distances[station_node][following]
                  - problem._distances[previous][following])
        arrival = schedule.event_times_min[(group.owner, cut - 1)] + (
            problem._distances[previous][station_node] / problem.speed_km_per_min)
        candidate = _changed_route(problem, route, group, received, cut, station, legs)
        metrics = evaluator.evaluate(candidate, start_node=problem.home_node(group.owner))
        stats["local_evaluations"] += 1
        if _expired(deadline):
            break
        result.append(_Cut(cut, candidate, metrics.score, detour, arrival))
        stats["cut_options_generated"] += 1
    result.sort(key=lambda c: (_key(c.score, coefficients), c.detour, c.arrival, c.position))
    return tuple(result)  # Capacity 2 gives one actual frontier per cargo group.


def _cross_count(index):
    return sum(index.inbound_route_by_task[t] != index.outbound_route_by_task[t]
               for t in index.relay_task_ids)


def refine(problem, routes, *, deadline, weights, scales, seed=2026101481, max_pairs=256):
    """Only true 2<->2 bundle transfers, preserving all unselected event sequences.

    All actual group frontiers, both delivery-slot permutations on each side
    (four joint mappings), and every commonly registered fixed station are
    covered until the absolute deadline. ``max_pairs`` is a processing batch
    size. Each pass freezes its proposal base, retains timely fully checked
    weighted best updates, then rebuilds groups from best-so-far. Absolute
    route-local objective sums lower-bound the complete global waiting score.
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
    initial_key, initial_cross = _key(checked.score, coefficients), _cross_count(build_plan_index(problem, current))
    stats = dict(method="In-Flight-Reciprocal-2x2-Bundle", seed=seed, weights=list(weights), scales=list(scales),
        bundle_size=2, max_pairs_per_batch=max_pairs, passes=0, batches=0, groups_generated=0,
        pairs_generated=0, pair_probes=0, mappings_probed=0, stations_probed=0,
        cut_options_generated=0, combinations_shortlisted=0, local_evaluations=0,
        lower_bound_pruned=0, global_evaluations=0, full_evaluations=1, rejected_cycles=0,
        rejected_invalid=0, duplicate_candidates_skipped=0, accepted_improvements=0,
        accepted_transfer_moves=0, accepted_control_only_moves=0,
        packages_transferred_in_accepted_moves=0, accepted_moves=[], deadline_reached=False,
        neighborhood_exhausted=False, initial_cross_uav_count=initial_cross)
    evaluator, global_evaluator = RouteEvaluator(problem), GlobalRelayEvaluator(problem)
    legs, seen, iterations = _leg_pairs(problem), {current}, 0
    while not _expired(deadline) and problem.has_relays and problem.capacity >= 2:
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
        groups = _groups(problem, base, index, legs)
        stats["groups_generated"] += len(groups)
        pairs = _pairs(problem, base, schedule, groups, legs, coefficients, seed, deadline)
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
                for mapping in pair.mappings:
                    if _expired(deadline):
                        break
                    stats["mappings_probed"] += 1
                    for station in pair.stations:
                        if _expired(deadline):
                            break
                        stats["stations_probed"] += 1
                        left = _cut_options(problem, evaluator, base, schedule, pair.left, mapping.received_a,
                            station, legs, coefficients, deadline, stats)
                        right = _cut_options(problem, evaluator, base, schedule, pair.right, mapping.received_b,
                            station, legs, coefficients, deadline, stats)
                        options = sorted(((a, b) for a in left for b in right), key=lambda c: (
                            _key(c[0].score + c[1].score, coefficients), abs(c[0].arrival - c[1].arrival),
                            c[0].detour + c[1].detour, c[0].position, c[1].position))
                        for a, b in options:
                            if _expired(deadline):
                                break
                            stats["combinations_shortlisted"] += 1
                            candidate = list(base)
                            candidate[pair.left.owner], candidate[pair.right.owner] = a.route, b.route
                            candidate = tuple(candidate)
                            if candidate in seen:
                                stats["duplicate_candidates_skipped"] += 1
                                continue
                            seen.add(candidate)
                            iterations += 1
                            lower = (base_sum - local_base[pair.left.owner] - local_base[pair.right.owner]
                                     + a.score + b.score)
                            if _key(lower, coefficients)[0] > _key(checked.score, coefficients)[0] + 1e-12:
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
                            confirmed, expected = build_plan_index(problem, candidate), dict(index.delivery_route_by_task)
                            for task in pair.left.tasks:
                                expected[task] = pair.right.owner
                            for task in pair.right.tasks:
                                expected[task] = pair.left.owner
                            if dict(confirmed.delivery_route_by_task) != expected:
                                raise RuntimeError("Bundle move changed an unrelated final responsibility")
                            accepted_at = perf_counter()
                            if accepted_at >= deadline:
                                break
                            stats["accepted_moves"].append(dict(tasks_a=list(pair.left.tasks), tasks_b=list(pair.right.tasks),
                                owner_a=pair.left.owner, owner_b=pair.right.owner, cut_a=a.position, cut_b=b.position,
                                station=station, original_slot_tasks_a=list(pair.left.slot_tasks),
                                original_slot_tasks_b=list(pair.right.slot_tasks), received_a=list(mapping.received_a),
                                received_b=list(mapping.received_b), pass_number=stats["passes"], proposal_base_key=list(base_key),
                                before_key=list(_key(checked.score, coefficients)), after_key=list(_key(confirmation.score, coefficients)),
                                delivery_replacement_delta_km=mapping.distance_delta, station_detour_km=a.detour + b.detour,
                                predicted_arrival_a_min=a.arrival, predicted_arrival_b_min=b.arrival,
                                elapsed_seconds=accepted_at - started, accepted_at_perf_counter=accepted_at,
                                preserved_all_other_job_routes_and_event_order=True))
                            current, checked, adopted = candidate, confirmation, True
                            stats["accepted_improvements"] += 1
                            stats["accepted_transfer_moves"] += 1
                            stats["packages_transferred_in_accepted_moves"] += 4
        if not adopted:
            stats["neighborhood_exhausted"] = not _expired(deadline)
            break
    if not checked.valid or _key(checked.score, coefficients) > initial_key:
        raise RuntimeError("Bundle refinement lost the checked weighted incumbent")
    final_index = build_plan_index(problem, current)
    stats["deadline_reached"] = _expired(deadline)
    stats["final_cross_uav_count"] = _cross_count(final_index)
    stats["new_cross_uav_count"] = stats["final_cross_uav_count"] - initial_cross
    stats["initial_key"], stats["final_key"] = list(initial_key), list(_key(checked.score, coefficients))
    stats["runtime_seconds"] = perf_counter() - started
    return dict(routes=current, evaluation=checked, iterations=iterations, metadata=stats)
