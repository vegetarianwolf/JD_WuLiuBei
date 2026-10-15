"""V4 separated-delivery native-priced catalogue: split feeders retain original final ownership.

The shared catalogue ranks complete relay layouts for both arms. DIRECT
independently rebuilds the original pair and twelve four-task quota maps.
Every retained proposal is checked against the actual unchanged input and
the full global schedule before a strict weighted improvement is adopted.
"""
from __future__ import annotations

from hashlib import sha256
from itertools import combinations, product
import json
from math import isclose, isfinite
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import split_feeder_catalogue_v4 as catalogue
    import deadline_window_costs as costs
    import direct_flexible_pair_v1 as flexible
except ImportError:
    from . import split_feeder_catalogue_v4 as catalogue
    from . import deadline_window_costs as costs
    from . import direct_flexible_pair_v1 as flexible


Deadline = costs.Deadline
weighted_key = costs.weighted_key
EPS = 1e-9


def _check(deadline, stats):
    if perf_counter() >= deadline:
        stats['deadline_reached'] = True
        raise Deadline


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _physical(score):
    return dict(late_count=score.late_count, total_lateness_min=float(score.total_lateness_min),
        distance_km=float(score.distance_km))


def _task(p, visit):
    leg = p.leg_registry.get(abs(visit))
    return leg.task_id if leg else abs(visit)


def _remove(route, tasks):
    return tuple(v for v in route if abs(v) not in tasks)


def _classes(p, original, current, family):
    late_before = {t for t, clock in original.items() if clock-p.task(t).deadline_min > EPS}
    late_now = {t for t, clock in current.items() if clock-p.task(t).deadline_min > EPS}
    pooled, donors = {family['i'], family['j']}, {family['k1'], family['k2']}
    result = {}
    for name, tasks in (('pooled', pooled), ('donor', donors), ('native', set(p.task_ids)-pooled-donors)):
        result[name+'_rescued_task_ids'] = sorted((late_before-late_now) & tasks)
        result[name+'_newly_late_task_ids'] = sorted((late_now-late_before) & tasks)
    return result


def _shared(p, routes, pair, owners):
    """DIRECT uses cargo task IDs; relay uses its registered OUT leg IDs."""
    if owners[pair[0]] != owners[pair[1]]:
        return 0.
    aircraft = owners[pair[0]]
    identifiers = set()
    for task in pair:
        starts = [v for v in routes[aircraft] if v > 0 and _task(p, v) == task]
        if len(starts) != 1:
            raise ValueError('one actual final transport per pooled task required')
        identifiers.add(starts[0])
    held, previous, distance = set(), p.home_points()[aircraft], 0.
    for visit in routes[aircraft]:
        point = p.point_for_visit(visit)
        if identifiers <= held:
            distance += previous.distance_to(point)
        if visit > 0:
            held.add(visit)
        else:
            held.remove(-visit)
        previous = point
    return float(distance)


def _handoff(p, routes, schedule, family, index, shared):
    pair = (family['i'], family['j']); c = family['c']
    available = {}
    outgoing = {}
    for task in pair:
        plan = index.task_plans[task]
        incoming = next(v for v in plan.leg_ids if p.leg_registry[v].kind == 'RELAY_IN')
        out = next(v for v in plan.leg_ids if p.leg_registry[v].kind == 'RELAY_OUT')
        a = index.inbound_route_by_task[task]
        available[task] = schedule.event_times_min[(a, routes[a].index(-incoming))]
        outgoing[out] = task
    positions = sorted(routes[c].index(v) for v in outgoing)
    first = positions[0]
    station = next(s.point for s in p.relay_stations if s.id == family['station_id'])
    previous = p.home_points()[c] if first == 0 else p.point_for_visit(routes[c][first-1])
    arrival = (0. if first == 0 else schedule.event_times_min[(c, first-1)]) + previous.distance_to(station)/p.speed_km_per_min
    order = [outgoing[routes[c][position]] for position in positions]
    times = [schedule.event_times_min[(c, position)] for position in positions]
    second = times[-1]
    offsets = {str(_task(p, visit)): schedule.delivery_times_min[_task(p, visit)]-second
        for position, visit in enumerate(routes[c]) if visit < 0 and position > positions[-1]
        and (abs(visit) not in p.leg_registry or p.leg_registry[abs(visit)].kind == 'RELAY_OUT')}
    return dict(station_id=family['station_id'], task_ids=list(pair), source_aircraft=[family['a'], family['b']],
        receiver_aircraft=c, source_availability_min={str(t): available[t] for t in pair},
        receiver_arrival_min=arrival, take_order_task_ids=order, take_times_min=times,
        second_take_min=second, receiver_waiting_min=second-arrival,
        ground_cache_by_task_min={str(t): times[order.index(t)]-available[t] for t in pair},
        suffix_delivery_offsets_min=offsets, shared_loaded_distance_km=shared)


def _pack(p, base_records, family, replacements, mode):
    records = list(base_records)
    for aircraft, record in replacements.items():
        records[aircraft] = record
    routes = tuple(r['route'] for r in records)
    owners = {t: a for a, record in enumerate(records) for t in record['delivery_times']}
    selected = tuple(family['task_ids']) if mode == 'DIRECT_DONOR_12MAP' else (family['i'], family['j'])
    move = dict(family=family, mode=mode, selected_task_ids=sorted(selected),
        final_aircraft_by_task={str(t): owners[t] for t in sorted(selected)},
        actual_final_owners={str(t): a for t, a in sorted(owners.items())})
    return dict(routes=routes, records=records, move=move,
        score=sum((r['score'] for r in records), Score(0, 0., 0.)))


def _retain(candidates, limit, p, original, family, w, s):
    unique = {}
    for candidate in candidates:
        unique.setdefault(candidate['routes'], candidate)
    ordered = sorted(unique.values(), key=lambda x: (weighted_key(x['score'], w, s), x['routes']))
    if not ordered:
        return ()
    def rescues(candidate):
        current = {t: clock for r in candidate['records'] for t, clock in r['delivery_times'].items()}
        return len(_classes(p, original, current, family)['pooled_rescued_task_ids'])
    representatives = [ordered[0],
        min(ordered, key=lambda x: (x['score'].late_count, x['score'].total_lateness_min,
            x['score'].distance_km, x['routes'])),
        min(ordered, key=lambda x: (-rescues(x), weighted_key(x['score'], w, s), x['routes']))]
    result = []
    for candidate in representatives+ordered:
        if len(result) >= limit:
            break
        if all(candidate['routes'] != old['routes'] for old in result):
            result.append(candidate)
    return tuple(result)


def _relay(p, base_records, family, *, deadline, stats):
    candidates = []
    for witness in family['relay_witnesses']:
        _check(deadline, stats)
        available = {int(t): value for t, value in witness['source_drop_availability_min'].items()}
        replacements = {}
        for a in (family['a'], family['b'], family['c']):
            replacements[a] = costs.route_cost(p, tuple(witness['routes'][a]), a, availability=available,
                deadline=deadline, statistics=stats['route_cost_statistics'])
        candidate = _pack(p, base_records, family, replacements, 'RELAY')
        candidate['catalogue_witness'] = witness
        candidates.append(candidate)
        stats['complete_table_candidates'] += 1
    return candidates


def _direct(p, base_records, family, w, s, original, *, deadline, source_position_limit,
        table_option_limit, direct_gap_limit, stats, coverage):
    i, j, a, b, c = (family[k] for k in ('i', 'j', 'a', 'b', 'c'))
    pair_native = _remove(base_records[c]['route'], {i, j})
    complete = []
    same_coverage = coverage['direct_same_owner_flexible_coverage']
    try:
        same = flexible.pair_options(p, pair_native, c, (i, j), deadline=deadline, weights=w, scales=s,
            option_limit=table_option_limit, requested_block_gap_limit_legacy_hint=direct_gap_limit,
            original_delivery_times=original, rescue_task_ids=(i, j), coverage=same_coverage,
            statistics=stats['route_cost_statistics'])
    finally:
        coverage['direct_same_owner_flexible_insertion_orders'] = same_coverage.get('flexible_insertion_orders_completed', [])
        coverage['direct_same_owner_flexible_pair_completed'] = same_coverage.get('flexible_pair_completed', False)
    for receiver in same:
        _check(deadline, stats)
        complete.append(_pack(p, base_records, family, {c: receiver}, 'DIRECT_SAME_OWNER'))
        stats['complete_table_candidates'] += 1
    coverage['direct_same_owner_pair_completed'] = True
    coverage['direct_same_owner_pair_options_retained'] = len(same)
    coverage['direct_same_owner_pair_empty_gaps_evaluated'] = same_coverage['six_block_empty_gaps_evaluated']
    coverage['direct_same_owner_pair_layouts_evaluated'] = same_coverage['six_block_layouts_evaluated']
    selected = tuple(family['task_ids']); aircraft = (a, b, c)
    native = {owner: _remove(base_records[owner]['route'], set(selected)) for owner in aircraft}
    singles, pairs = {}, {}
    for owner in aircraft[:2]:
        for task in selected:
            _check(deadline, stats)
            singles[(owner, task)] = costs.direct_single_options(p, native[owner], owner, task,
                deadline=deadline, weights=w, scales=s, option_limit=table_option_limit,
                position_limit=source_position_limit, original_delivery_times=original, rescue_task_ids=(i, j),
                statistics=stats['route_cost_statistics'])
    for pair in combinations(selected, 2):
        _check(deadline, stats)
        pair_progress = dict(task_pair=list(pair), coverage={})
        coverage['direct_donor_pair_flexible_records'].append(pair_progress)
        pairs[pair] = flexible.pair_options(p, native[c], c, pair, deadline=deadline, weights=w, scales=s,
            option_limit=table_option_limit, requested_block_gap_limit_legacy_hint=direct_gap_limit,
            original_delivery_times=original, rescue_task_ids=(i, j), coverage=pair_progress['coverage'],
            statistics=stats['route_cost_statistics'])
        coverage['direct_donor_pair_flexible_completed'].append(list(pair))
    for first in selected:
        for second in selected:
            if first == second:
                continue
            _check(deadline, stats)
            pair = tuple(t for t in selected if t not in (first, second))
            mapping = [[first], [second], list(pair)]
            coverage['direct_maps_considered'].append(mapping)
            stats['direct_final_maps_examined'] += 1
            for records in product(singles[(a, first)], singles[(b, second)], pairs[pair]):
                _check(deadline, stats)
                complete.append(_pack(p, base_records, family, dict(zip(aircraft, records)), 'DIRECT_DONOR_12MAP'))
                stats['complete_table_candidates'] += 1
            coverage['direct_maps_completed'].append(mapping)
    coverage['direct_map_enumeration_completed'] = True
    return complete


def _verify(p, base, routes, move, relay_enabled):
    before, current = build_plan_index(p, base), build_plan_index(p, routes)
    f, mode = move['family'], move['mode']; pair = (f['i'], f['j'])
    if (not f['i'] < f['j'] or len({f['a'], f['b'], f['c']}) != 3
        or any(before.delivery_route_by_task[t] != f['c'] for t in pair)
        or before.delivery_route_by_task[f['k1']] != f['a'] or before.delivery_route_by_task[f['k2']] != f['b']
        or f['task_ids'] != sorted((*pair, f['k1'], f['k2']))):
        raise ValueError('family does not describe the actual original owners')
    if mode not in ('RELAY', 'DIRECT_SAME_OWNER', 'DIRECT_DONOR_12MAP') or (mode == 'RELAY') != relay_enabled:
        raise ValueError('candidate mode does not match arm')
    selected = set(f['task_ids']) if mode == 'DIRECT_DONOR_12MAP' else set(pair)
    if move['selected_task_ids'] != sorted(selected):
        raise ValueError('selected reconstruction set differs')
    for a in range(p.drone_count):
        if sum(x == a for x in before.delivery_route_by_task.values()) != sum(x == a for x in current.delivery_route_by_task.values()):
            raise ValueError('final quota changed')
        if tuple(v for v in base[a] if abs(v) not in selected) != tuple(v for v in routes[a] if _task(p, v) not in selected):
            raise ValueError('unselected owner or relative event order changed')
    actual = {str(t): a for t, a in sorted(current.delivery_route_by_task.items())}
    if actual != move['actual_final_owners'] or move['final_aircraft_by_task'] != {str(t): actual[str(t)] for t in sorted(selected)}:
        raise ValueError('saved final map differs from actual route')
    if set(current.relay_task_ids) != (set(pair) if relay_enabled else set()):
        raise ValueError('required two relay identities changed')
    if mode == 'DIRECT_DONOR_12MAP':
        final = [sum(current.delivery_route_by_task[t] == owner for t in selected) for owner in (f['a'], f['b'], f['c'])]
        if final != [1, 1, 2]:
            raise ValueError('four-task independent DIRECT quota map differs')
    elif any(current.delivery_route_by_task[t] != f['c'] for t in pair):
        raise ValueError('original pair final owner changed')
    if relay_enabled:
        for task, source in zip(pair, (f['a'], f['b'])):
            if (current.inbound_route_by_task[task] != source or current.outbound_route_by_task[task] != f['c']
                or current.task_plans[task].relay_id != f['station_id']):
                raise ValueError('split feeder role or fixed station changed')
        identifiers = {next(v for v in current.task_plans[t].leg_ids if p.leg_registry[v].kind == 'RELAY_OUT') for t in pair}
        route = routes[f['c']]; starts = sorted(route.index(v) for v in identifiers)
        if starts[1] != starts[0]+1 or sum(1 if v > 0 else -1 for v in route[:starts[0]]) != 0:
            raise ValueError('receiver must enter its consecutive takes empty')
        if _shared(p, routes, pair, current.delivery_route_by_task) <= 1e-10:
            raise ValueError('no positive actual double-loaded flight')
    return current


def _snapshot(p, routes, full, schedule, w, s, *, phase, move=None, table_score=None, original=None):
    index = build_plan_index(p, routes)
    family = move['family'] if move else None
    shared = _shared(p, routes, (family['i'], family['j']), index.delivery_route_by_task) if family else 0.
    handoff = _handoff(p, routes, schedule, family, index, shared) if family and index.relay_task_ids else None
    result = dict(routes=[list(r) for r in routes], valid=True, complete=True, phase=phase, move=move,
        score=_physical(full.score), key=list(weighted_key(full.score, w, s)),
        cross_uav_count=sum(index.inbound_route_by_task[t] != index.outbound_route_by_task[t] for t in index.relay_task_ids),
        relay_task_count=len(index.relay_task_ids), table_score=_physical(full.score if table_score is None else table_score),
        event_times_min=[[schedule.event_times_min[(a, k)] for k in range(len(r))] for a, r in enumerate(routes)],
        delivery_times_min={str(t): v for t, v in sorted(schedule.delivery_times_min.items())},
        actual_final_owners={str(t): a for t, a in sorted(index.delivery_route_by_task.items())},
        source_drop_availability_min=handoff['source_availability_min'] if handoff else {},
        shared_loaded_distance_km=shared, handoff=handoff)
    if family:
        result.update(_classes(p, original, schedule.delivery_times_min, family))
    return result


def _table_matches(p, candidate, full, canonical):
    score = candidate['score']
    if (score.late_count != full.score.late_count or abs(score.total_lateness_min-full.score.total_lateness_min) > 1e-8
        or abs(score.distance_km-full.score.distance_km) > 1e-8):
        raise ValueError('complete route table/global score mismatch')
    for a, record in enumerate(candidate['records']):
        if len(record['event_times']) != len(candidate['routes'][a]) or any(
                abs(value-canonical.event_times_min[(a, k)]) > 1e-8 for k, value in enumerate(record['event_times'])):
            raise ValueError('complete route table/global clock mismatch')
    witness = candidate.get('catalogue_witness')
    if witness is not None:
        if tuple(tuple(r) for r in witness['routes']) != candidate['routes']:
            raise ValueError('catalogue witness routes differ from complete reconstruction')
        expected = witness['table_score']
        if expected['late_count'] != full.score.late_count or any(abs(expected[key]-_physical(full.score)[key]) > 1e-8
                for key in ('total_lateness_min', 'distance_km')):
            raise ValueError('catalogue witness full score differs')
        if witness['event_times_min'] != [[r['event_times'][k] for k in range(len(r['route']))] for r in candidate['records']]:
            raise ValueError('catalogue witness clock differs')


def refine(problem, routes, *, deadline, weights, scales, seed=2026101519, relay_enabled=True,
        max_families=16, base_pool_limit=64, source_position_limit=8, source_option_limit=3,
        table_option_limit=3, feeder_limit=3, receiver_gap_limit=4, direct_gap_limit=4,
        full_candidates_per_family=3, sigma=0.):
    started = perf_counter(); w, s = tuple(weights), tuple(scales)
    bounds = ((max_families, 64), (base_pool_limit, 256), (source_position_limit, 512), (source_option_limit, 3),
        (table_option_limit, 3), (feeder_limit, 8), (receiver_gap_limit, 64), (direct_gap_limit, 64), (full_candidates_per_family, 3))
    if (type(deadline) is bool or not isfinite(deadline) or type(relay_enabled) is not bool or len(w) != 3 or len(s) != 3
        or any(not isfinite(v) or v <= 0 for v in (*w, *s)) or not isclose(sum(w), 1.)
        or any(type(v) is not int or not 1 <= v <= upper for v, upper in bounds) or base_pool_limit < max_families
        or type(sigma) is bool or not isfinite(sigma) or sigma < 0):
        raise ValueError('finite normalized weighted objective and bounded search required')
    base = tuple(tuple(r) for r in routes)
    if len(base) > problem.drone_count:
        raise ValueError('too many routes')
    base += ((),)*(problem.drone_count-len(base))
    checked = evaluate_solution(problem, base, _force_full=True)
    if (not checked.valid or build_plan_index(problem, base).relay_task_ids or problem.capacity != 2 or problem.drone_count < 3
        or not problem.has_relays or problem.distance_matrix_km is not None):
        raise ValueError('valid pure DIRECT Euclidean C2 fixed-network input required')
    evaluator = GlobalRelayEvaluator(problem); baseline = evaluator.evaluate(base)
    initial_key = best_key = weighted_key(checked.score, w, s); best, best_checked = base, checked
    stats = dict(method='Separated-Delivery-Native-Priced-Split-Feeder-V4-RELAY' if relay_enabled else 'Flexible-Whole-Pair-And-Twelve-Map-V4-DIRECT',
        seed=seed, relay_enabled=relay_enabled, deadline_perf_counter=deadline, deadline_reached=False,
        active_stage='initial', timeout_stage=None, selected_families=[], selector_sha256=_digest([]), selector_completed=False,
        selector_completed_at_perf_counter=None, catalogue=None, catalogue_statistics={}, route_cost_statistics={}, family_records=[],
        families_examined=0, complete_table_candidates=0, full_evaluations=0, rejected_complete_candidates=[],
        direct_final_maps_examined=0, full_candidate_snapshots=[], first_complete_candidate=None, best_constructed_candidate=None,
        accepted_moves=[], accepted_improvements=0, initial_key=list(initial_key), final_key=list(initial_key),
        input_snapshot=_snapshot(problem, base, checked, baseline, w, s, phase='input'),
        configuration=dict(catalogue_version=4, early_catalogue_native_pricing='restricted_two_gap_IN_and_separated_OUT_delivery',
            early_relay_price_is_DIRECT_candidate=False, max_families=max_families, base_pool_limit=base_pool_limit, source_position_limit=source_position_limit,
            source_option_limit=source_option_limit, table_option_limit=table_option_limit, feeder_limit=feeder_limit,
            requested_receiver_gap_limit_legacy_hint=receiver_gap_limit, requested_direct_gap_limit_legacy_hint=direct_gap_limit,
            gap_limits_ignored_for_pre_scoring_prefilter=True, full_candidates_per_family=full_candidates_per_family, sigma_min=sigma,
            relay_original_pair_owner_preserved=True, relay_requires_compensation=False, source_final_count_may_already_equal_K=True,
            direct_same_owner_all_empty_gaps_six_orders=True, direct_independent_four_task_final_maps=12,
            direct_pair_flexible_native_interleaving=True, direct_pair_flexible_whole_insertion_orders=2,
            direct_pair_flexible_position_policy='all_capacity_legal_PD_positions_full_scored_before_per_layer_cap',
            relay_receiver_candidate_layout='empty_gap_two_consecutive_takes_first_delivery_native_interval_second_delivery',
            direct_single_position_policy='finite_geometry_and_early_clock_surface_positions',
            complete_retention_channels=['weighted_key', 'full_late_count', 'pooled_i_j_rescue'],
            source_tables_are_complete_native=True, all_flight_and_wait_counted=True, catalogue_shared=True,
            raw_only=True, polish_enabled=False, resources_and_homes_unchanged=True))
    try:
        _check(deadline, stats); stats['active_stage'] = 'base_route_tables'
        records = [costs.route_cost(problem, r, a, deadline=deadline, statistics=stats['route_cost_statistics']) for a, r in enumerate(base)]
        stats['active_stage'] = 'shared_catalogue'
        selection = catalogue.build_catalogue(problem, base, deadline=deadline, weights=w, scales=s, seed=seed,
            max_families=max_families, base_pool_limit=base_pool_limit, source_position_limit=source_position_limit,
            source_option_limit=source_option_limit, table_option_limit=table_option_limit, feeder_limit=feeder_limit,
            sigma=sigma, statistics=stats['catalogue_statistics'])
        if not selection['completed']:
            raise ValueError('catalogue must not return a partial family prefix')
        families = selection['families']; digest = _digest(families); completed = perf_counter()
        if digest != selection['selector_sha256']:
            raise ValueError('catalogue family digest differs')
        if completed >= deadline:
            raise Deadline
        stats.update(catalogue=selection, selected_families=families, selector_sha256=digest,
            selector_completed=True, selector_completed_at_perf_counter=completed)
        for family in families:
            _check(deadline, stats); stats['active_stage'] = 'complete_route_tables'; stats['families_examined'] += 1
            coverage = dict(family=family, complete_candidates=0, complete_table_enumeration_completed=False,
                direct_same_owner_pair_completed=False, direct_same_owner_flexible_pair_completed=False,
                direct_same_owner_flexible_insertion_orders=[], direct_same_owner_flexible_coverage={},
                direct_donor_pair_flexible_completed=[], direct_donor_pair_flexible_records=[],
                direct_maps_considered=[], direct_maps_completed=[],
                direct_map_enumeration_completed=False)
            stats['family_records'].append(coverage)
            candidates = (_relay(problem, records, family, deadline=deadline, stats=stats) if relay_enabled else
                _direct(problem, records, family, w, s, baseline.delivery_times_min, deadline=deadline,
                    source_position_limit=source_position_limit, table_option_limit=table_option_limit,
                    direct_gap_limit=direct_gap_limit, stats=stats, coverage=coverage))
            complete = _retain(candidates, full_candidates_per_family, problem, baseline.delivery_times_min, family, w, s)
            coverage.update(complete_candidates=len(complete), complete_table_enumeration_completed=True)
            for candidate in complete:
                _check(deadline, stats); stats['active_stage'] = 'canonical_full_guard'
                full = evaluate_solution(problem, candidate['routes'], _force_full=True); stats['full_evaluations'] += 1
                canonical = evaluator.evaluate(candidate['routes'])
                if not full.valid or not canonical.feasible:
                    stats['rejected_complete_candidates'].append(dict(family=family, reason='invalid_complete_candidate'))
                    continue
                _verify(problem, base, candidate['routes'], candidate['move'], relay_enabled)
                _table_matches(problem, candidate, full, canonical)
                snapshot = _snapshot(problem, candidate['routes'], full, canonical, w, s, phase='full_proposal',
                    move=candidate['move'], table_score=candidate['score'], original=baseline.delivery_times_min)
                verified = perf_counter()
                if verified >= deadline:
                    raise Deadline
                snapshot.update(full_verified_at_perf_counter=verified, deadline_perf_counter=deadline)
                _check(deadline, stats)
                stats['full_candidate_snapshots'].append(snapshot)
                if stats['first_complete_candidate'] is None:
                    stats['first_complete_candidate'] = snapshot
                if stats['best_constructed_candidate'] is None or tuple(snapshot['key']) < tuple(stats['best_constructed_candidate']['key']):
                    stats['best_constructed_candidate'] = snapshot
                key = weighted_key(full.score, w, s)
                if key < best_key:
                    accepted = perf_counter()
                    if accepted >= deadline:
                        raise Deadline
                    best, best_checked, best_key = candidate['routes'], full, key; stats['accepted_improvements'] += 1
                    stats['accepted_moves'].append(dict(move=candidate['move'], full_snapshot=snapshot,
                        full_verified_at_perf_counter=verified, accepted_at_perf_counter=accepted))
    except Deadline:
        stats['deadline_reached'] = True; stats['timeout_stage'] = stats['active_stage']
    index = build_plan_index(problem, best)
    stats.update(final_key=list(best_key), final_cross_uav_count=sum(index.inbound_route_by_task[t] != index.outbound_route_by_task[t]
        for t in index.relay_task_ids), runtime_seconds=perf_counter()-started)
    return dict(routes=best, evaluation=best_checked, iterations=stats['complete_table_candidates'], metadata=stats)
