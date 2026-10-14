"""Research-only simultaneous reciprocal transfers on a checked incumbent.

Two tasks with different final-delivery UAVs exchange responsibility at one
existing station. Each UAV picks up its own task, drops it, takes the other
task, then delivers that task. This changes both plans together, preserves
each final-delivery K count, and always forms two genuine cross-UAV transfers.
The search contains no DIRECT exchange or construction/ALNS stage.

Gap/clock estimates are heuristic shortlisting only. Existing transfer waits
may absorb delays or propagate to other UAVs; every accepted complete plan is
therefore globally scheduled and independently fully checked before deadline.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from math import isclose, isfinite
from random import Random
from time import perf_counter

from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution


def weighted_key(score, weights, scales):
    physical = (score.late_count, score.total_lateness_min, score.distance_km)
    return (sum(w * x / s for w, x, s in zip(weights, physical, scales)), *physical)


def _check(deadline, counters):
    if perf_counter() >= deadline:
        counters["deadline_reached"] = True
        raise _Deadline


class _Deadline(Exception):
    pass


def _remove(problem, routes, task_ids):
    removed = set(task_ids)
    registry = problem.leg_registry or {}
    return tuple(tuple(v for v in route if (
        registry[abs(v)].task_id if abs(v) in registry else abs(v)
    ) not in removed) for route in routes)


def _legs(problem, task_id):
    result = {}
    for leg in problem._legs_by_task.get(task_id, ()):
        result.setdefault(leg.relay_id, {})[leg.kind] = leg.id
    return {station: (kinds["RELAY_IN"], kinds["RELAY_OUT"])
            for station, kinds in result.items()
            if set(kinds) == {"RELAY_IN", "RELAY_OUT"}}


def _delivery_task(problem, visit):
    if visit >= 0:
        return None
    leg = (problem.leg_registry or {}).get(-visit)
    return -visit if leg is None else leg.task_id if leg.kind == "RELAY_OUT" else None


@dataclass(frozen=True, slots=True)
class _Gap:
    position: int
    drop: float
    extra_km: float
    suffix: tuple[tuple[int, float], ...]
    rank: float


def _suffix_cost(problem, suffix, delay, weights, scales):
    count, lateness = 0, 0.
    for task_id, clock in suffix:
        deadline = problem.task(task_id).deadline_min
        before, after = max(0., clock - deadline), max(0., clock + delay - deadline)
        count += int(after > 1e-9) - int(before > 1e-9)
        lateness += after - before
    return weights[0] * count / scales[0] + weights[1] * lateness / scales[1]


def _gap_options(problem, routes, schedule, route_index, block, target_task,
                 weights, scales, limit, deadline, counters):
    """Zero-load gaps ranked by detour, transfer clock and suffix deadline risk."""
    route, load, choices = routes[route_index], 0, []
    speed = problem.speed_km_per_min
    internal = sum(problem.distance(a, b) for a, b in zip(block, block[1:]))
    for position in range(len(route) + 1):
        _check(deadline, counters)
        if load == 0:
            previous = route[position - 1] if position else None
            home = problem.home_node(route_index) if position == 0 else None
            prefix = schedule.event_times_min[(route_index, position - 1)] if position else 0.
            first_km = problem.distance(previous, block[0], from_node=home)
            extra = first_km + internal
            if position < len(route):
                extra += (problem.distance(block[-1], route[position])
                          - problem.distance(previous, route[position], from_node=home))
            drop = prefix + (first_km + problem.distance(block[0], block[1])) / speed
            suffix = tuple((task_id, schedule.delivery_times_min[task_id])
                           for v in route[position:]
                           if (task_id := _delivery_task(problem, v)) is not None)
            finish = drop + problem.distance(block[2], block[3]) / speed
            late = max(0., finish - problem.task(target_task).deadline_min)
            rank = (weights[2] * extra / scales[2]
                    + weights[0] * int(late > 1e-9) / scales[0]
                    + weights[1] * late / scales[1]
                    + _suffix_cost(problem, suffix, extra / speed, weights, scales))
            choices.append(_Gap(position, drop, extra, suffix, rank))
        if position < len(route):
            load += 1 if route[position] > 0 else -1
    return tuple(sorted(choices, key=lambda g: (g.rank, g.drop, g.position))[:limit])


def _pair_order(problem, routes, index, evaluation, rng):
    """Find opposite flows, with deadline-critical delivery and donor suffixes."""
    eligible = [t for t in sorted(index.task_plans) if _legs(problem, t)]
    stations = {t: set(_legs(problem, t)) & set(problem.task_relay_candidates.get(t, ()))
                for t in eligible}
    pairs = []
    for offset, first in enumerate(eligible):
        a, ti = index.delivery_route_by_task[first], problem.task(first)
        for second in eligible[offset + 1:]:
            b, tj = index.delivery_route_by_task[second], problem.task(second)
            if a == b or not stations[first] & stations[second]:
                continue
            # i's pickup near j's destination, and vice versa, favours local
            # post-handoff work instead of two aircraft crossing and returning.
            crossed = (ti.pickup.distance_to(ti.delivery) + tj.pickup.distance_to(tj.delivery)
                       - ti.pickup.distance_to(tj.delivery) - tj.pickup.distance_to(ti.delivery))
            critical = sum(1. / (1. + abs(evaluation.delivery_times_min[t] - problem.task(t).deadline_min))
                           for t in (first, second))
            pairs.append((-(crossed + 2. * critical), first, second))
    pairs.sort()
    # Deterministic seed-based exploration is separate from the main ranking.
    split = int(len(pairs) * .8)
    tail = pairs[split:]
    rng.shuffle(tail)
    return pairs[:split] + tail


def _candidates(problem, routes, index, first, second, evaluator, weights,
                scales, gap_limit, candidate_limit, deadline, counters):
    a, b = index.delivery_route_by_task[first], index.delivery_route_by_task[second]
    reduced = _remove(problem, routes, (first, second))
    _check(deadline, counters)
    base = evaluator.evaluate(reduced)
    counters["partial_evaluations"] += 1
    _check(deadline, counters)
    if not base.feasible:
        return ()
    left_legs, right_legs = _legs(problem, first), _legs(problem, second)
    common = sorted(set(left_legs) & set(right_legs)
                    & set(problem.task_relay_candidates.get(first, ()))
                    & set(problem.task_relay_candidates.get(second, ())))
    ranked = []
    for station in common:
        _check(deadline, counters)
        in_i, out_i = left_legs[station]
        in_j, out_j = right_legs[station]
        block_a, block_b = (in_i, -in_i, out_j, -out_j), (in_j, -in_j, out_i, -out_i)
        gaps_a = _gap_options(problem, reduced, base, a, block_a, second,
                              weights, scales, gap_limit, deadline, counters)
        gaps_b = _gap_options(problem, reduced, base, b, block_b, first,
                              weights, scales, gap_limit, deadline, counters)
        for ga in gaps_a:
            for gb in gaps_b:
                _check(deadline, counters)
                meet = max(ga.drop, gb.drop)
                finish_i = meet + problem.distance(out_i, -out_i) / problem.speed_km_per_min
                finish_j = meet + problem.distance(out_j, -out_j) / problem.speed_km_per_min
                late_i = max(0., finish_i - problem.task(first).deadline_min)
                late_j = max(0., finish_j - problem.task(second).deadline_min)
                rank = (weights[0] * (int(late_i > 1e-9) + int(late_j > 1e-9)) / scales[0]
                        + weights[1] * (late_i + late_j) / scales[1]
                        + weights[2] * (ga.extra_km + gb.extra_km) / scales[2]
                        + _suffix_cost(problem, ga.suffix,
                            ga.extra_km / problem.speed_km_per_min + meet - ga.drop, weights, scales)
                        + _suffix_cost(problem, gb.suffix,
                            gb.extra_km / problem.speed_km_per_min + meet - gb.drop, weights, scales))
                ranked.append((rank, station, ga.position, gb.position, block_a, block_b))
    ranked.sort()
    # Keep the best combination from each station before remaining choices.
    heads, rest, seen = [], [], set()
    for item in ranked:
        if item[1] in seen:
            rest.append(item)
        else:
            seen.add(item[1])
            heads.append(item)
    result = []
    for _, station, pa, pb, block_a, block_b in (heads + rest)[:candidate_limit]:
        changed = list(reduced)
        changed[a] = reduced[a][:pa] + block_a + reduced[a][pa:]
        changed[b] = reduced[b][:pb] + block_b + reduced[b][pb:]
        result.append((tuple(changed), station))
    return tuple(result)


def refine(problem, routes, *, deadline, weights, scales, seed=2026101481,
           max_pairs=256, gap_limit=6, candidates_per_pair=12, memo_limit=8192):
    """Return a fully checked best plan, with no accepted over-deadline work.

    ``deadline`` is an absolute ``perf_counter`` value. ``iterations`` counts
    examined original-task pairs, not global candidate evaluations. No task
    pair is required to improve as two individually applied relay conversions.
    """
    weights, scales = tuple(weights), tuple(scales)
    if (len(weights) != 3 or len(scales) != 3
            or any(not isfinite(x) or x <= 0 for x in (*weights, *scales))
            or not isclose(sum(weights), 1.) or not isfinite(deadline)):
        raise ValueError("finite positive scales/weights summing to one and finite deadline required")
    if (not isinstance(max_pairs, int) or max_pairs < 0
            or any(not isinstance(x, int) or x < 1
                   for x in (gap_limit, candidates_per_pair, memo_limit))):
        raise ValueError("nonnegative max_pairs and positive integer shortlist bounds required")
    started = perf_counter()
    best = tuple(tuple(r) for r in routes)
    if len(best) > problem.drone_count:
        raise ValueError("too many routes")
    best += ((),) * (problem.drone_count - len(best))
    checked = evaluate_solution(problem, best, _force_full=True)
    if not checked.valid:
        raise ValueError(f"invalid initial solution: {checked.violations}")
    initial_key = best_key = weighted_key(checked.score, weights, scales)
    counters = dict(pairs_examined=0, partial_evaluations=0, global_evaluations=0,
                    full_evaluations=1, accepted_improvements=0, memo_hits=0,
                    rejected_infeasible=0, rejected_invalid=0, deadline_reached=False)
    trajectory = [(0, perf_counter() - started, *best_key)]
    moves, memo, rng = [], OrderedDict(), Random(seed)
    evaluator = GlobalRelayEvaluator(problem)
    try:
        _check(deadline, counters)
        while counters["pairs_examined"] < max_pairs:
            _check(deadline, counters)
            index = build_plan_index(problem, best)
            order = _pair_order(problem, best, index, checked, rng)
            _check(deadline, counters)
            improved = False
            for _, first, second in order:
                _check(deadline, counters)
                if counters["pairs_examined"] >= max_pairs:
                    break
                counters["pairs_examined"] += 1
                candidates = _candidates(problem, best, index, first, second, evaluator,
                    weights, scales, gap_limit, candidates_per_pair, deadline, counters)
                for candidate, station in candidates:
                    _check(deadline, counters)
                    if candidate in memo:
                        counters["memo_hits"] += 1
                        memo.move_to_end(candidate)
                        continue
                    memo[candidate] = None
                    if len(memo) > memo_limit:
                        memo.popitem(last=False)
                    scheduled = evaluator.evaluate(candidate)
                    counters["global_evaluations"] += 1
                    _check(deadline, counters)
                    if not scheduled.feasible:
                        counters["rejected_infeasible"] += 1
                        continue
                    if weighted_key(scheduled.score, weights, scales) >= best_key:
                        continue
                    confirmed = evaluate_solution(problem, candidate, _force_full=True)
                    counters["full_evaluations"] += 1
                    _check(deadline, counters)
                    if not confirmed.valid:
                        counters["rejected_invalid"] += 1
                        continue
                    candidate_key = weighted_key(confirmed.score, weights, scales)
                    if candidate_key >= best_key:
                        continue
                    a, b = index.delivery_route_by_task[first], index.delivery_route_by_task[second]
                    best, checked, best_key = candidate, confirmed, candidate_key
                    counters["accepted_improvements"] += 1
                    moves.append(dict(tasks=[first, second], previous_delivery_routes=[a, b],
                                      delivery_routes=[b, a], station=station,
                                      weighted_key=list(best_key)))
                    trajectory.append((counters["pairs_examined"], perf_counter() - started, *best_key))
                    improved = True
                    break
                if improved:
                    break
            if not improved:
                break
    except _Deadline:
        pass
    final_index = build_plan_index(problem, best)
    cross = sum(final_index.inbound_route_by_task[t] != final_index.outbound_route_by_task[t]
                for t in final_index.relay_task_ids)
    metadata = dict(method="Reciprocal-Relay-Refinement", seed=seed,
        initial_key=list(initial_key), final_key=list(best_key),
        best_trajectory=trajectory, accepted_moves=moves, direct_moves=0,
        limits=dict(max_pairs=max_pairs, gap_limit=gap_limit,
                    candidates_per_pair=candidates_per_pair, memo_limit=memo_limit),
        reciprocal_search=counters,
        relay=dict(relay_task_count=len(final_index.relay_task_ids),
                   cross_uav_handoff_count=cross,
                   same_uav_relay_count=len(final_index.relay_task_ids)-cross))
    return dict(routes=best, evaluation=checked, iterations=counters["pairs_examined"], metadata=metadata)
