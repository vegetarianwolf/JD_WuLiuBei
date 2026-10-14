"""Research-only non-monotone four-task reconstruction on a strong layout.

Two complete tasks from each of two UAVs are removed. Every other event stays
on its original UAV in its original relative order. Public capacity-feasible
pair insertions rebuild DIRECT or true cross-UAV relay plans in a small beam;
the two released final-delivery slots per UAV are preserved exactly. Missing
tasks are allowed only in equally deep internal beam states. Complete plans
are fully checked, optionally polished, and guarded against the input score.

The relay mode reserves beam diversity before judging complete combined moves,
so an expensive first station visit need not improve immediately. All flights,
station waits and other-task clocks enter the complete global handoff DAG.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import isclose, isfinite
from random import Random
from time import perf_counter

from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.search import RouteEvaluator, route_insertion_options, route_leg_insertion_options
from uav_dispatch.validation import evaluate_solution


class _Deadline(Exception):
    pass


class _PolishStop(Exception):
    pass


def weighted_key(score, weights, scales):
    physical = (score.late_count, score.total_lateness_min, score.distance_km)
    return (sum(w * x / s for w, x, s in zip(weights, physical, scales)), *physical)


def _check(deadline, stats):
    if perf_counter() >= deadline:
        stats["deadline_reached"] = True
        raise _Deadline


def _task_id(problem, visit):
    leg = (problem.leg_registry or {}).get(abs(visit))
    return leg.task_id if leg else abs(visit)


def _remove(problem, routes, task_ids):
    removed = set(task_ids)
    return tuple(tuple(v for v in route if _task_id(problem, v) not in removed) for route in routes)


def _legs(problem):
    result = {}
    for task_id, legs in problem._legs_by_task.items():
        stations = {}
        for leg in legs:
            stations.setdefault(leg.relay_id, {})[leg.kind] = leg.id
        result[task_id] = {s: (parts["RELAY_IN"], parts["RELAY_OUT"])
                           for s, parts in stations.items()
                           if set(parts) == {"RELAY_IN", "RELAY_OUT"}}
    return result


def _double_loaded_adjacency(problem, routes):
    """Distance actually shared by two parcels before each service event."""
    result = []
    for route_index, route in enumerate(routes):
        active, previous, adjacency = set(), None, {}
        for visit in route:
            if len(active) == 2:
                pair = tuple(sorted({_task_id(problem, v) for v in active}))
                if len(pair) == 2:
                    adjacency[pair] = adjacency.get(pair, 0.) + problem.distance(
                        previous, visit, from_node=problem.home_node(route_index) if previous is None else None)
            if visit > 0:
                active.add(visit)
            else:
                active.remove(-visit)
            previous = visit
        result.append(adjacency)
    return tuple(result)


def _clusters(problem, routes, evaluation, seed, max_clusters, deadline, stats):
    index, shared, rng = build_plan_index(problem, routes), _double_loaded_adjacency(problem, routes), Random(seed)
    per_route = []
    for route_index in range(problem.drone_count):
        tasks = sorted(t for t, owner in index.delivery_route_by_task.items() if owner == route_index)
        ranked = []
        for pair in combinations(tasks, 2):
            _check(deadline, stats)
            a, b = map(problem.task, pair)
            geometry = a.pickup.distance_to(b.pickup) + a.delivery.distance_to(b.delivery)
            deadline_gap = abs(a.deadline_min - b.deadline_min)
            risk = sum(1. / (1. + abs(evaluation.delivery_times_min[t] - problem.task(t).deadline_min)) for t in pair)
            overlap = shared[route_index].get(pair, 0.)
            ranked.append(((geometry - overlap, deadline_gap, -risk, pair), pair, overlap))
        ranked.sort()
        per_route.append(ranked[:4])
    ranked = []
    for left, right in combinations(range(problem.drone_count), 2):
        for _, a, shared_a in per_route[left]:
            for _, b, shared_b in per_route[right]:
                _check(deadline, stats)
                ta, tb = tuple(map(problem.task, a)), tuple(map(problem.task, b))
                # Opposite flow supports collection near the other territory,
                # while paired overlap protects useful existing cargo packing.
                opposed = min(x.pickup.distance_to(y.delivery) + y.pickup.distance_to(x.delivery)
                              for x in ta for y in tb)
                compact = (ta[0].pickup.distance_to(ta[1].pickup) + ta[0].delivery.distance_to(ta[1].delivery)
                           + tb[0].pickup.distance_to(tb[1].pickup) + tb[0].delivery.distance_to(tb[1].delivery))
                gap = abs(sum(t.deadline_min for t in ta) - sum(t.deadline_min for t in tb)) / 2.
                rank = (compact + .5 * opposed - shared_a - shared_b, gap, rng.random(), left, right, a, b)
                task_ids = tuple(sorted(a + b, key=lambda t: (problem.task(t).deadline_min, t)))
                ranked.append((rank, dict(route_indices=(left, right), task_ids=task_ids,
                                         original_pairs=(a, b), shared_double_loaded_km=(shared_a, shared_b))))
    ranked.sort(key=lambda row: row[0])
    return tuple(cluster for _, cluster in ranked[:max_clusters])


def _partial(problem, routes, expected_ids, evaluator, stats):
    """Validate present complete plans; absence is permitted only internally."""
    stats["partial_evaluations"] += 1
    try:
        index = build_plan_index(problem, routes)
    except (ValueError, KeyError):
        stats["rejected_invalid_partial"] += 1
        return None
    if set(index.task_plans) != set(expected_ids):
        stats["rejected_wrong_partial_coverage"] += 1
        return None
    for route_index, route in enumerate(routes):
        load = 0
        for visit in route:
            load += 1 if visit > 0 else -1
            if not 0 <= load <= problem.capacity:
                stats["rejected_invalid_partial"] += 1
                return None
        if load or sum(owner == route_index for owner in index.delivery_route_by_task.values()) > problem.max_tasks_per_drone:
            stats["rejected_invalid_partial"] += 1
            return None
    scheduled = evaluator.evaluate(routes)
    if not scheduled.feasible:
        stats["rejected_cycles"] += 1
        return None
    if set(scheduled.delivery_times_min) != set(expected_ids):
        stats["rejected_wrong_partial_coverage"] += 1
        return None
    return scheduled, index


@dataclass(frozen=True, slots=True)
class _State:
    routes: tuple
    key: tuple
    modes: tuple  # (task_id, final_route, mode, station, inbound_route)


def _diverse(states, width, recipients):
    """Reserve relay/DIRECT and recipient-slot families at the same depth."""
    ranked = sorted(states, key=lambda s: (s.key, s.modes, s.routes))
    representatives, other, families, routes_seen = [], [], set(), set()
    for state in ranked:
        if state.routes in routes_seen:
            continue
        routes_seen.add(state.routes)
        counts = tuple(sum(entry[1] == recipient for entry in state.modes) for recipient in recipients)
        family = (any(entry[2] == "RELAY" for entry in state.modes), counts)
        if family not in families:
            families.add(family)
            representatives.append(state)
        else:
            other.append(state)
    # Under the small beam preserve at least one pure DIRECT and one genuine
    # relay before allocating the remaining recipient responsibilities.
    heads = []
    for relay in (False, True):
        match = next((s for s in representatives if any(e[2] == "RELAY" for e in s.modes) == relay), None)
        if match is not None:
            heads.append(match)
    remainder = [s for s in representatives if s not in heads] + other
    return tuple((heads + remainder)[:width])


def _extend(problem, state, task_id, recipients, legs, local, candidate_limit,
            option_count, relay_enabled, deadline, stats):
    used = {i: sum(entry[1] == i for entry in state.modes) for i in recipients}
    for receiver in recipients:
        if used[receiver] >= 2:
            continue
        _check(deadline, stats)
        options = route_insertion_options(problem, local, state.routes[receiver], receiver, task_id,
                                          candidate_limit=candidate_limit, option_count=option_count)
        _check(deadline, stats)
        for option in options:
            changed = list(state.routes)
            changed[receiver] = option.route
            yield tuple(changed), state.modes + ((task_id, receiver, "DIRECT", None, receiver),)
        if not relay_enabled:
            continue
        for station, (inbound, outbound) in sorted(legs.get(task_id, {}).items()):
            carrier = next(i for i in recipients if i != receiver)
            _check(deadline, stats)
            inbound_options = route_leg_insertion_options(problem, local, state.routes[carrier], carrier,
                inbound, -inbound, candidate_limit=candidate_limit, option_count=option_count,
                counts_toward_k=False, event_cap=None)
            _check(deadline, stats)
            outbound_options = route_leg_insertion_options(problem, local, state.routes[receiver], receiver,
                outbound, -outbound, candidate_limit=candidate_limit, option_count=option_count,
                counts_toward_k=True, event_cap=None)
            _check(deadline, stats)
            for incoming in inbound_options:
                for outgoing in outbound_options:
                    changed = list(state.routes)
                    changed[carrier], changed[receiver] = incoming.route, outgoing.route
                    yield tuple(changed), state.modes + ((task_id, receiver, "RELAY", station, carrier),)


def _rebuild(problem, base_routes, cluster, legs, local, global_evaluator, weights, scales,
             relay_enabled, beam_width, candidate_limit, option_count, deadline, stats):
    removed = set(cluster["task_ids"])
    remaining = set(problem.task_ids) - removed
    routes = _remove(problem, base_routes, removed)
    partial = _partial(problem, routes, remaining, global_evaluator, stats)
    if partial is None:
        return ()
    states = (_State(routes, weighted_key(partial[0].score, weights, scales), ()),)
    stages = []
    for task_id in cluster["task_ids"]:
        _check(deadline, stats)
        remaining.add(task_id)
        proposals = []
        for state in states:
            for routes, modes in _extend(problem, state, task_id, cluster["route_indices"], legs, local,
                candidate_limit, option_count, relay_enabled, deadline, stats):
                _check(deadline, stats)
                partial = _partial(problem, routes, remaining, global_evaluator, stats)
                _check(deadline, stats)
                if partial is not None:
                    proposals.append(_State(routes, weighted_key(partial[0].score, weights, scales), modes))
        states = _diverse(proposals, beam_width, cluster["route_indices"])
        stages.append(dict(inserted_task_ids=[t for t in cluster["task_ids"] if t in remaining],
                           valid_candidates=len(proposals), kept_states=len(states),
                           kept_relay_states=sum(any(e[2] == "RELAY" for e in s.modes) for s in states)))
        if not states:
            break
    stats["beam_stages"].append(stages)
    # Only the last depth reaches full coverage. Callers never adopt these
    # internal scheduled scores without independent complete validation.
    return states if remaining == set(problem.task_ids) else ()


def _snapshot(problem, routes, checked, weights, scales, *, phase, cluster=None):
    index = build_plan_index(problem, routes)
    return dict(routes=[list(r) for r in routes], key=list(weighted_key(checked.score, weights, scales)),
                score=dict(late_count=checked.score.late_count,
                           total_lateness_min=checked.score.total_lateness_min,
                           distance_km=checked.score.distance_km),
                complete=True, valid=True, phase=phase, cluster=cluster,
                cross_uav_count=sum(index.inbound_route_by_task[t] != index.outbound_route_by_task[t]
                                    for t in index.relay_task_ids), relay_task_count=len(index.relay_task_ids))


def _preserves_other_events(problem, base, candidate, selected):
    return _remove(problem, base, selected) == _remove(problem, candidate, selected)


def _polish(problem, routes, checked, selected, local, global_evaluator, weights, scales,
            *, deadline, polish_seconds, candidate_limit, option_count, stats):
    start, current, current_key = perf_counter(), routes, weighted_key(checked.score, weights, scales)
    end = min(deadline, start + polish_seconds)
    def check():
        _check(deadline, stats)
        if perf_counter() >= end:
            raise _PolishStop
    def candidates():
        for route_index, route in enumerate(current):
            for position in range(len(route) - 1):
                if (_task_id(problem, route[position]) not in selected
                        and _task_id(problem, route[position + 1]) not in selected):
                    continue
                check()
                changed = list(current)
                changed[route_index] = route[:position] + (route[position + 1], route[position]) + route[position + 2:]
                yield tuple(changed)
        index = build_plan_index(problem, current)
        for task_id in sorted(selected):
            plan = index.task_plans[task_id]
            leg_ids = plan.leg_ids if plan.mode == "RELAY" else (task_id,)
            for leg_id in leg_ids:
                check()
                route_index = next(i for i, r in enumerate(current) if leg_id in r)
                reduced = tuple(v for v in current[route_index] if abs(v) != leg_id)
                if plan.mode == "DIRECT":
                    options = route_insertion_options(problem, local, reduced, route_index, task_id,
                        candidate_limit=candidate_limit, option_count=option_count)
                else:
                    options = route_leg_insertion_options(problem, local, reduced, route_index, leg_id, -leg_id,
                        candidate_limit=candidate_limit, option_count=option_count,
                        counts_toward_k=problem.leg_registry[leg_id].kind == "RELAY_OUT", event_cap=None)
                check()
                for option in options:
                    changed = list(current)
                    changed[route_index] = option.route
                    yield tuple(changed)
    try:
        for _ in range(2):
            improved = False
            for candidate in candidates():
                check()
                if candidate == current:
                    continue
                stats["polish_candidates"] += 1
                partial = _partial(problem, candidate, set(problem.task_ids), global_evaluator, stats)
                check()
                if partial is None or weighted_key(partial[0].score, weights, scales) >= current_key:
                    continue
                full = evaluate_solution(problem, candidate, _force_full=True)
                stats["full_evaluations"] += 1
                check()
                if full.valid and weighted_key(full.score, weights, scales) < current_key:
                    current, checked, current_key = candidate, full, weighted_key(full.score, weights, scales)
                    stats["polish_improvements"] += 1
                    improved = True
                    break
            if not improved:
                break
    except _PolishStop:
        stats["polish_cutoffs"] += 1
    return current, checked, dict(runtime_seconds=perf_counter() - start,
                                  assigned_seconds=polish_seconds, passes_limit=2)


def refine(problem, routes, *, deadline, weights, scales, seed=2026101492,
           relay_enabled=True, max_clusters=2, beam_width=6, polish_seconds=.15,
           candidate_limit=12, option_count=2, complete_candidates=2, proposal_margin=.0075):
    """Bounded non-monotone proposals, each complete and checked before adoption.

    DIRECT control uses the identical cluster selector, task order, capacity
    insertion bounds, beam width, complete-candidate count and polish limit.
    Only the relay plan choices and associated mode diversity are disabled.
    All candidates start at the same input checkpoint; accepted results do not
    alter the next cluster's input or selector. ``deadline`` is absolute.
    """
    weights, scales = tuple(weights), tuple(scales)
    if (len(weights) != 3 or len(scales) != 3
            or any(not isfinite(x) or x <= 0 for x in (*weights, *scales))
            or not isclose(sum(weights), 1.) or not isfinite(deadline)):
        raise ValueError("finite positive normalized weights/scales and deadline required")
    if (not isinstance(relay_enabled, bool) or any(not isinstance(x, int) or isinstance(x, bool) or x < 1
            for x in (max_clusters, beam_width, candidate_limit, option_count, complete_candidates))
            or beam_width < 2 or candidate_limit < 4
            or not isfinite(polish_seconds) or polish_seconds < 0
            or not isfinite(proposal_margin) or proposal_margin < 0):
        raise ValueError("positive limits, beam>=2, candidate_limit>=4, and nonnegative polish/margin required")
    started = perf_counter()
    base = tuple(tuple(r) for r in routes)
    if len(base) > problem.drone_count:
        raise ValueError("too many routes")
    base += ((),) * (problem.drone_count - len(base))
    checked = evaluate_solution(problem, base, _force_full=True)
    if not checked.valid:
        raise ValueError(f"invalid initial solution: {checked.violations}")
    best, best_checked = base, checked
    initial_key = best_key = weighted_key(checked.score, weights, scales)
    stats = dict(method="Packing-Preserving-Cluster-Beam", seed=seed, relay_enabled=relay_enabled,
                 clusters_attempted=0, complete_proposals=0, full_evaluations=1,
                 accepted_improvements=0, partial_evaluations=0, rejected_invalid_partial=0,
                 rejected_wrong_partial_coverage=0, rejected_cycles=0, rejected_invalid_complete=0,
                 rejected_over_margin=0, polish_candidates=0, polish_improvements=0, polish_cutoffs=0,
                 deadline_reached=False, beam_stages=[], cluster_records=[],
                 configuration=dict(max_clusters=max_clusters, beam_width=beam_width,
                    polish_seconds=polish_seconds, candidate_limit=candidate_limit, option_count=option_count,
                    complete_candidates=complete_candidates, proposal_margin=proposal_margin),
                 first_complete_candidate=None, best_constructed_candidate=None,
                 input_snapshot=_snapshot(problem, base, checked, weights, scales, phase="pre"))
    local, global_evaluator, legs = RouteEvaluator(problem), GlobalRelayEvaluator(problem), _legs(problem)
    try:
        _check(deadline, stats)
        clusters = _clusters(problem, base, checked, seed, max_clusters, deadline, stats)
        stats["selected_clusters"] = list(clusters)
        for cluster_index, cluster in enumerate(clusters):
            _check(deadline, stats)
            stats["clusters_attempted"] += 1
            record = dict(cluster_index=cluster_index, cluster=cluster, complete_candidates=[])
            stats["cluster_records"].append(record)
            states = _rebuild(problem, base, cluster, legs, local, global_evaluator, weights, scales,
                relay_enabled, beam_width, candidate_limit, option_count, deadline, stats)
            # Reserve one completed DIRECT and one completed relay proposal if
            # available, rather than spending every polish slot on one mode.
            states = _diverse(states, complete_candidates, cluster["route_indices"])
            for state in states:
                _check(deadline, stats)
                full = evaluate_solution(problem, state.routes, _force_full=True)
                stats["full_evaluations"] += 1
                _check(deadline, stats)
                if not full.valid:
                    stats["rejected_invalid_complete"] += 1
                    continue
                if not _preserves_other_events(problem, base, state.routes, cluster["task_ids"]):
                    raise AssertionError("rebuild changed an unselected event's UAV or order")
                before = _snapshot(problem, state.routes, full, weights, scales,
                                   phase="full_proposal", cluster=cluster_index)
                _check(deadline, stats)
                stats["complete_proposals"] += 1
                if stats["first_complete_candidate"] is None:
                    stats["first_complete_candidate"] = before
                if (stats["best_constructed_candidate"] is None
                        or tuple(before["key"]) < tuple(stats["best_constructed_candidate"]["key"])):
                    stats["best_constructed_candidate"] = before
                candidate_record = dict(full_proposal=before, post_polish=None, adopted=False)
                record["complete_candidates"].append(candidate_record)
                if before["key"][0] > initial_key[0] + proposal_margin:
                    stats["rejected_over_margin"] += 1
                    candidate_record["polish_skipped"] = "predeclared_weighted_margin"
                    continue
                polished, final, polish_timing = _polish(problem, state.routes, full,
                    set(cluster["task_ids"]), local, global_evaluator, weights, scales,
                    deadline=deadline, polish_seconds=polish_seconds,
                    candidate_limit=candidate_limit, option_count=option_count, stats=stats)
                _check(deadline, stats)
                if not _preserves_other_events(problem, base, polished, cluster["task_ids"]):
                    raise AssertionError("polish changed an unselected event's UAV or order")
                candidate_record["post_polish"] = _snapshot(problem, polished, final, weights, scales,
                    phase="post_polish", cluster=cluster_index)
                candidate_record["polish_timing"] = polish_timing
                key = weighted_key(final.score, weights, scales)
                _check(deadline, stats)
                if key < best_key:
                    best, best_checked, best_key = polished, final, key
                    candidate_record["adopted"] = True
                    stats["accepted_improvements"] += 1
    except _Deadline:
        pass
    final_index = build_plan_index(problem, best)
    stats.update(initial_key=list(initial_key), final_key=list(best_key),
                 final_cross_uav_count=sum(final_index.inbound_route_by_task[t] != final_index.outbound_route_by_task[t]
                                           for t in final_index.relay_task_ids),
                 runtime_seconds=perf_counter() - started)
    return dict(routes=best, evaluation=best_checked, iterations=stats["complete_proposals"], metadata=stats)
