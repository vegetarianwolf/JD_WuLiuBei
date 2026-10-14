"""Finite DIRECT whole-pair insertion with real native clocks at every layer.

The two insertion orders have different partial coverage and are ranked in
separate pipelines. Only complete two-task records join the original six
contiguous precedence-correct blocks. No H or relay availability is consulted.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter

try:
    import deadline_window_costs as costs
except ImportError:
    from . import deadline_window_costs as costs

Deadline = costs.Deadline


def _check(deadline):
    if type(deadline) is bool or not isfinite(deadline):
        raise ValueError('finite absolute deadline required')
    if perf_counter() >= deadline:
        raise Deadline


def _retain(records, limit, w, s, deadline):
    unique = {}
    for record in records:
        _check(deadline)
        unique.setdefault(record['route'], record)
    rank = lambda r: (r['_flexible_pair_key'], r['route'])
    ordered = sorted(unique.values(), key=rank)
    _check(deadline)
    if not ordered:
        return ()
    heads = (ordered[0],
        min(ordered, key=lambda r: (r['_flexible_pair_key'][1:], rank(r))),
        min(ordered, key=lambda r: (-len(r['retention_rescued_task_ids']),
            len(r['retention_new_late_task_ids']), rank(r))))
    result = []
    for record in (*heads, *ordered):
        _check(deadline)
        if len(result) >= limit:
            break
        if all(record['route'] != old['route'] for old in result):
            result.append(record)
    return tuple(result)


def _scored(p, route, aircraft, pair, w, s, original, rescue, deadline, statistics):
    record = costs.route_cost(p, route, aircraft, selected_pair=pair, deadline=deadline, statistics=statistics)
    costs._rescue_evidence(p, record, pair, original, rescue_task_ids=rescue)
    record['_flexible_pair_key'] = costs.weighted_key(record['score'], w, s)
    return record


def _insert(p, record, aircraft, task, pair, w, s, original, rescue, deadline, statistics, progress,
        stage, stage_counter):
    route = record['route']; n = len(route); loads = (*record['load_before'], 0)
    result = []
    for g in range(n+1):
        _check(deadline)
        if loads[g] >= 2:
            continue
        for h in range(g, n+1):
            _check(deadline)
            if loads[h] >= 2:
                break
            candidate = route[:g]+(task,)+route[g:h]+(-task,)+route[h:]
            actual = _scored(p, candidate, aircraft, pair, w, s, original, rescue, deadline, statistics)
            actual.update(flexible_inserted_task_id=task, flexible_pickup_original_gap=g,
                flexible_delivery_original_gap=h)
            result.append(actual)
            progress['legal_PD_positions_full_scored'] += 1
            stage[stage_counter] += 1
    _check(deadline)
    return result


def pair_options(p, native_route, aircraft, task_pair, *, deadline, weights, scales,
        option_limit=3, original_delivery_times=None, rescue_task_ids=None,
        requested_block_gap_limit_legacy_hint=4, coverage=None, statistics=None):
    """Return at most ``option_limit`` genuine complete single-aircraft records.

    Every capacity-legal P/D position is scored before each layer's cap. The
    first layer is only a ranking state missing the other selected task; it
    never enters the returned complete pool or a global solution certificate.
    """
    _check(deadline)
    w, s = tuple(weights), tuple(scales)
    pair = costs._parameters(p, aircraft, task_pair, w, s, deadline, option_limit)
    if len(pair) != 2 or option_limit > 3:
        raise ValueError('two distinct whole DIRECT tasks and channel cap at most3 required')
    native = costs._native(p, native_route, set(pair), deadline)
    initial = costs.route_cost(p, native, aircraft, deadline=deadline, statistics=statistics)
    if len(initial['delivery_times'])+2 > p.max_tasks_per_drone:
        raise ValueError('complete pair would exceed final delivery quota')
    mask = pair if rescue_task_ids is None else tuple(rescue_task_ids)
    progress = {} if coverage is None else coverage
    if not isinstance(progress, dict):
        raise ValueError('mutable coverage dictionary required')
    orders = [list(pair), list(reversed(pair))]
    progress.update(flexible_insertion_orders=orders, flexible_insertion_orders_completed=[],
        flexible_pair_completed=False, pipeline_records=[], legal_PD_positions_full_scored=0,
        first_partial_is_complete_candidate=False, first_partial_coverage_scope='native_plus_first_task_only',
        per_layer_option_limit=option_limit, all_legal_PD_positions_scored_before_layer_cap=True,
        six_block_enumeration_completed=False, six_block_empty_gaps_evaluated=0,
        six_block_layouts_evaluated=0, final_records_retained=0)
    complete = []
    for first, second in (pair, tuple(reversed(pair))):
        _check(deadline)
        stage = dict(insertion_order=[first, second], first_present_task_ids=sorted((*initial['delivery_times'], first)),
            second_present_task_ids=sorted((*initial['delivery_times'], first, second)),
            first_legal_PD_positions_full_scored=0, first_records_retained=0, first_retained_routes=[],
            second_parent_records_completed=0, second_legal_PD_positions_full_scored=0,
            complete_records_retained=0, completed=False)
        progress['pipeline_records'].append(stage)
        raw = _insert(p, initial, aircraft, first, pair, w, s, original_delivery_times, mask,
            deadline, statistics, progress, stage, 'first_legal_PD_positions_full_scored')
        partial = _retain(raw, option_limit, w, s, deadline)
        stage['first_records_retained'] = len(partial)
        stage['first_retained_routes'] = [list(record['route']) for record in partial]
        leaves = []
        for record in partial:
            _check(deadline)
            leaves.extend(_insert(p, record, aircraft, second, pair, w, s, original_delivery_times, mask,
                deadline, statistics, progress, stage, 'second_legal_PD_positions_full_scored'))
            stage['second_parent_records_completed'] += 1
        retained = _retain(leaves, option_limit, w, s, deadline)
        for record in retained:
            record.update(record_kind='DIRECT_flexible_pair', task_pair=pair,
                flexible_insertion_order=(first, second), flexible_whole_pair_complete=True)
        complete.extend(retained)
        _check(deadline)
        stage.update(complete_records_retained=len(retained), completed=True)
        progress['flexible_insertion_orders_completed'].append([first, second])
    blocks = costs.direct_pair_options(p, native, aircraft, pair, deadline=deadline, weights=w, scales=s,
        option_limit=option_limit, gap_limit=requested_block_gap_limit_legacy_hint,
        original_delivery_times=original_delivery_times, rescue_task_ids=mask, statistics=statistics)
    for record in blocks:
        record['_flexible_pair_key'] = costs.weighted_key(record['score'], w, s)
    progress.update(six_block_enumeration_completed=True,
        six_block_empty_gaps_evaluated=blocks[0]['empty_gaps_evaluated'] if blocks else 0)
    progress['six_block_layouts_evaluated'] = 6*progress['six_block_empty_gaps_evaluated']
    complete.extend(blocks)
    result = _retain(complete, option_limit, w, s, deadline)
    _check(deadline)
    progress.update(flexible_pair_completed=True, final_records_retained=len(result))
    return result
