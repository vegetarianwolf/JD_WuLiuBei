"""Experiment-only periodic whole-task exchanges after DIRECT repair.

Patch only the supplied ALNS module's repair function, after construction and
with explicit initial_routes supplied to solve_alns. Improved repair candidates
continue through ordinary ALNS acceptance and operator rewards unchanged.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
from itertools import combinations
from math import isfinite
from time import perf_counter

from uav_dispatch.direct_task_exchange import _positions, _replacement_distance_delta
from uav_dispatch.model import Score
from uav_dispatch.validation import evaluate_solution


_active = False
INTERVAL = 120
PAIR_BUDGET_SECONDS = .008
MAX_CANDIDATES = 16
ANCHORS_PER_ROUTE = 4


def _score(score):
    return dict(late_count=score.late_count, total_lateness_min=score.total_lateness_min,
                distance_km=score.distance_km)


def _pair_exchange(problem, evaluator, routes, pair, deadline, event,
                   max_candidates=MAX_CANDIDATES, anchors_per_route=ANCHORS_PER_ROUTE):
    """Improve one fixed route pair; return only pre-deadline checkpoints."""
    def expired():
        if perf_counter() >= deadline:
            event['deadline_reached'] = True
            return True
        return False

    if expired():
        return routes
    if evaluator.problem is not problem:
        raise ValueError('周期交换评分器与问题必须一致')
    if len(routes) != problem.drone_count:
        raise ValueError('周期交换要求每架无人机恰好一条路线')
    if any(abs(visit) in problem.leg_registry for route in routes for visit in route):
        event['skip_reason'] = 'relay_events'
        return routes
    stage = perf_counter()
    baseline = evaluate_solution(problem, routes, _force_full=True)
    event['precheck_seconds'] += perf_counter() - stage
    event['canonical_checks'] += 1
    if expired():
        return routes
    if not baseline.valid:
        raise ValueError(f'周期交换的输入解非法: {baseline.violations}')
    current, current_score = routes, baseline.score
    event['before_score'] = event['after_score'] = _score(current_score)
    left, right = pair
    if not current[left] or not current[right]:
        event['skip_reason'] = 'empty_route'
        return current

    stage = perf_counter()
    metrics, locations = {}, {}
    for route_index in pair:
        if expired():
            event['ranking_seconds'] += perf_counter() - stage
            return current
        metrics[route_index] = evaluator.evaluate(current[route_index], start_node=problem.home_node(route_index))
        locations.update({task: (route_index, positions)
                          for task, positions in _positions(current[route_index]).items()})

    def anchor_priority(task):
        route_index, positions = locations[task]
        route, home = current[route_index], problem.home_node(route_index)
        lateness = max(0.0, metrics[route_index].delivery_times_min[task] - problem.task(task).deadline_min)
        incident = {edge for position in positions for edge in (position, position + 1) if edge < len(route)}
        isolation = sum(problem.distance(route[edge - 1] if edge else None, route[edge],
                                         from_node=home if edge == 0 else None) for edge in incident)
        return -lateness, -isolation, task

    tasks = {route_index: sorted((task for task in locations if locations[task][0] == route_index),
                                key=anchor_priority) for route_index in pair}
    proxies = {}
    for route_index, other_index in (pair, tuple(reversed(pair))):
        for task in tasks[route_index][:anchors_per_route]:
            for other in tasks[other_index]:
                if expired():
                    event['ranking_seconds'] += perf_counter() - stage
                    return current
                key = min(task, other), max(task, other)
                if key in proxies:
                    continue
                event['ranking_probes'] += 1
                a_clock, b_clock = (metrics[route_index].delivery_times_min[task],
                                    metrics[other_index].delivery_times_min[other])
                a_deadline, b_deadline = problem.task(task).deadline_min, problem.task(other).deadline_min
                old_late = (max(0.0, a_clock - a_deadline), max(0.0, b_clock - b_deadline))
                new_late = (max(0.0, b_clock - a_deadline), max(0.0, a_clock - b_deadline))
                distance_delta = (
                    _replacement_distance_delta(problem, current[route_index], problem.home_node(route_index),
                                                locations[task][1], task, other)
                    + _replacement_distance_delta(problem, current[other_index], problem.home_node(other_index),
                                                  locations[other][1], other, task)
                )
                proxies[key] = Score(sum(value > 1e-9 for value in new_late) - sum(value > 1e-9 for value in old_late),
                                     sum(new_late) - sum(old_late), distance_delta)
    candidates = sorted(proxies, key=lambda key: (proxies[key], key))[:max_candidates]
    event['shortlisted_candidates'] = len(candidates)
    event['ranking_seconds'] += perf_counter() - stage
    for task, other in candidates:
        if expired():
            return current
        a, _ = locations[task]
        b, _ = locations[other]
        if a == b:
            event['same_route_skips'] += 1
            continue
        new_a = tuple((other if visit > 0 else -other) if abs(visit) == task else visit
                      for visit in current[a])
        new_b = tuple((task if visit > 0 else -task) if abs(visit) == other else visit
                      for visit in current[b])
        stage = perf_counter()
        event['probes'] += 1
        a_metrics = evaluator.evaluate(new_a, start_node=problem.home_node(a))
        if expired():
            event['probe_seconds'] += perf_counter() - stage
            return current
        b_metrics = evaluator.evaluate(new_b, start_node=problem.home_node(b))
        event['probe_seconds'] += perf_counter() - stage
        if expired():
            return current
        if a_metrics.score + b_metrics.score >= metrics[a].score + metrics[b].score:
            continue
        changed = list(current)
        changed[a], changed[b] = new_a, new_b
        candidate = tuple(changed)
        stage = perf_counter()
        checked = evaluate_solution(problem, candidate, _force_full=True)
        event['canonical_seconds'] += perf_counter() - stage
        event['canonical_checks'] += 1
        if expired():
            return current
        if checked.valid and checked.score < current_score:
            current, current_score = candidate, checked.score
            metrics[a], metrics[b] = a_metrics, b_metrics
            for route_index in pair:
                locations.update({tid: (route_index, positions)
                                  for tid, positions in _positions(current[route_index]).items()})
            event['improvements'] += 1
            event['after_score'] = _score(current_score)
    return current


@contextmanager
def periodic_exchange(alns_module, *, interval=INTERVAL, pair_budget_seconds=PAIR_BUDGET_SECONDS,
                      max_candidates=MAX_CANDIDATES, anchors_per_route=ANCHORS_PER_ROUTE,
                      statistics=None):
    """Temporarily patch repair; the caller must construct its initial routes first.

    Every 120 successful repairs rotates one lexicographically ordered route
    pair. A pass uses at most 8 ms, limited further by the original repair's
    main-search deadline. All counters and event records are JSON-native.
    Custom limits are available for synthetic tests but cannot exceed the
    production experimental bounds. No random generator is accessed.
    """
    global _active
    if _active:
        raise RuntimeError('Nested periodic exchange contexts are unsupported')
    if type(interval) is not int or interval < 1:
        raise ValueError('交换间隔必须为正整数')
    if not isfinite(pair_budget_seconds) or not 0 < pair_budget_seconds <= PAIR_BUDGET_SECONDS:
        raise ValueError('周期交换预算必须在 (0, 0.008] 秒内')
    if type(max_candidates) is not int or not 0 <= max_candidates <= MAX_CANDIDATES:
        raise ValueError('精评候选上限必须在 0–16 内')
    if type(anchors_per_route) is not int or not 1 <= anchors_per_route <= ANCHORS_PER_ROUTE:
        raise ValueError('每路 anchor 上限必须在 1–4 内')
    stats = statistics if statistics is not None else {}
    stats.update(repair_calls=0, passes=0, probes=0, canonical_checks=0, improvements=0,
                 runtime_seconds=0.0, events=[])
    original = alns_module._repair
    pass_index = 0

    @wraps(original)
    def repair(*args, **kwargs):
        nonlocal pass_index
        result = original(*args, **kwargs)
        stats['repair_calls'] += 1
        if stats['repair_calls'] % interval or max_candidates == 0:
            return result
        problem = args[0] if args else kwargs['problem']
        evaluator = args[1] if len(args) > 1 else kwargs['evaluator']
        relay_cfg = kwargs.get('relay_cfg')
        if len(result) < 2 or (relay_cfg is not None and relay_cfg.enabled and problem.has_relays):
            return result
        pairs = tuple(combinations(range(len(result)), 2))
        pair = pairs[pass_index % len(pairs)]
        pass_index += 1
        stats['passes'] += 1
        started = perf_counter()
        main_deadline = kwargs.get('deadline', args[7] if len(args) > 7 else None)
        deadline = started + pair_budget_seconds
        if main_deadline is not None:
            deadline = min(deadline, main_deadline)
        event = dict(repair_call=stats['repair_calls'], route_pair=list(pair),
                     ranking_probes=0, shortlisted_candidates=0, probes=0, canonical_checks=0,
                     improvements=0, same_route_skips=0, deadline_reached=False,
                     before_score=None, after_score=None, precheck_seconds=0.0,
                     ranking_seconds=0.0, probe_seconds=0.0, canonical_seconds=0.0,
                     budget_seconds=max(0.0, deadline - started))
        stats['events'].append(event)
        try:
            return _pair_exchange(problem, evaluator, result, pair, deadline, event,
                                  max_candidates=max_candidates, anchors_per_route=anchors_per_route)
        finally:
            event['runtime_seconds'] = perf_counter() - started
            stats['runtime_seconds'] += event['runtime_seconds']
            for key in ('probes', 'canonical_checks', 'improvements'):
                stats[key] += event[key]

    _active = True
    alns_module._repair = repair
    try:
        yield stats
    finally:
        alns_module._repair = original
        _active = False
