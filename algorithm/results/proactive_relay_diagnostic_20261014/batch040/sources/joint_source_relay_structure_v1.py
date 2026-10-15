"""Typed same-source two-IN groups on a real complete multi-relay parent.

Legacy split groups retain their old descriptors and structural evidence.
Only new joint-pair groups are proposed here.  Geometry and parent clocks
rank finite proposals; no partial/local price is a complete score or bound.
"""
from __future__ import annotations

from itertools import combinations
from math import isfinite
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import multi_relay_structure_v1 as old
except ImportError:
    from . import multi_relay_structure_v1 as old

Deadline = old.Deadline
PROXY_SCOPE = old.PROXY_SCOPE
_digest, _task, _layout = old._digest, old._task, old._layout
_plans_signature, _loads, _distance = old._plans_signature, old._loads, old._distance
_pool, _family_key, _retain_positions, _out_route = old._pool, old._family_key, old._retain_positions, old._out_route
_leg = old._leg


def _check(deadline, stats=None):
    if deadline is not None:
        if type(deadline) is bool or not isfinite(deadline):
            raise ValueError('finite absolute deadline required')
        if perf_counter() >= deadline:
            if stats is not None:
                stats['deadline_reached'] = True
            raise Deadline


def _group(p, group, number):
    if not isinstance(group, dict):
        raise ValueError('integer typed group descriptor required')
    if 'source_type' not in group:
        return old._group(p, group, number)
    names = ('group_id', 'i', 'j', 'a', 'b', 'c', 'station_id')
    if group['source_type'] != 'joint_pair' or any(type(group.get(k)) is not int for k in names):
        raise ValueError('registered joint_pair source type and integer fields required')
    g = {k: group[k] for k in names}
    if (g['group_id'] != number or not g['i'] < g['j'] or any(g[k] not in p._task_by_id for k in ('i', 'j'))
        or g['a'] != g['b'] or g['a'] == g['c']
        or any(not 0 <= g[k] < p.drone_count for k in ('a', 'b', 'c'))
        or g['station_id'] not in {s.id for s in p.relay_stations}):
        raise ValueError('joint pair needs two tasks and one source distinct from its receiver')
    return dict(**g, source_type='joint_pair')


def _shared(p, route, aircraft, identifiers, deadline):
    held, previous, distance = set(), p.home_points()[aircraft], 0.
    for visit in route:
        _check(deadline)
        point = p.point_for_visit(visit)
        if identifiers <= held:
            distance += previous.distance_to(point)
        if visit > 0:
            held.add(visit)
        else:
            held.remove(-visit)
        previous = point
    return float(distance)


def _shape(p, routes, index, counts, groups, expected_signature, require_complete, deadline):
    normalized = [_group(p, g, k+1) for k, g in enumerate(groups)]
    selected = [g[k] for g in normalized for k in ('i', 'j')]
    if len(set(selected)) != len(selected) or set(index.relay_task_ids) != set(selected):
        raise ValueError('actual relay plans must exactly match disjoint declared groups')
    if require_complete and set(index.task_plans) != set(p.task_ids):
        raise ValueError('complete actual task coverage required')
    if set(index.direct_task_ids) != set(index.task_plans)-set(selected):
        raise ValueError('every ordinary task must remain whole DIRECT')
    metrics = []
    for g in normalized:
        _check(deadline)
        i, j, a, b, c, H = (g[k] for k in ('i', 'j', 'a', 'b', 'c', 'station_id'))
        for task, source in ((i, a), (j, b)):
            plan = index.task_plans[task]
            if (plan.mode != 'RELAY' or plan.relay_id != H or index.inbound_route_by_task[task] != source
                or index.outbound_route_by_task[task] != c or index.delivery_route_by_task[task] != c):
                raise ValueError('actual typed group plans/roles/site differ')
        incoming = [next(x for x in index.task_plans[t].leg_ids if p.leg_registry[x].kind == 'RELAY_IN') for t in (i, j)]
        outgoing = [next(x for x in index.task_plans[t].leg_ids if p.leg_registry[x].kind == 'RELAY_OUT') for t in (i, j)]
        takes = sorted(routes[c].index(x) for x in outgoing)
        if takes[1] != takes[0]+1:
            raise ValueError('each group needs adjacent double OUT starts')
        load = 0
        for k, visit in enumerate(routes[c]):
            if k == takes[0] and load:
                raise ValueError('receiver must enter each group doubletake empty')
            load += 1 if visit > 0 else -1
        shared = _shared(p, routes[c], c, set(outgoing), deadline)
        if shared <= 1e-10:
            raise ValueError('receiver needs positive actual shared loaded flight')
        drops = [routes[source].index(-leg) for source, leg in zip((a, b), incoming)]
        if g.get('source_type') == 'joint_pair':
            pickups = [routes[a].index(x) for x in incoming]
            if max(pickups) >= min(drops) or drops[1] != drops[0]+1:
                raise ValueError('joint source needs two pickups before canonical i/j adjacent double drops')
            if _shared(p, routes[a], a, set(incoming), deadline) <= 1e-10:
                raise ValueError('joint source needs positive actual shared IN flight')
        # Keep legacy shape evidence keys exactly; joint current source proof
        # is derived from its typed descriptor and real routes, not a fake S/B.
        metrics.append(dict(group=g, take_positions=takes, shared_loaded_distance_km=shared,
            in_leg_ids=incoming, out_leg_ids=outgoing, source_drop_positions=drops))
    if expected_signature is not None:
        previous = expected_signature.get('groups') if isinstance(expected_signature, dict) else None
        if not isinstance(previous, list) or normalized[:len(previous)] != previous:
            raise ValueError('old typed group descriptors changed')
        if _plans_signature(p, routes, index, counts, previous) != expected_signature:
            raise ValueError('old plans/roles/site/subsequences or final counts changed')
    return dict(groups=metrics, group_count=len(groups), relay_task_ids=sorted(selected), cross_uav_count=len(selected),
        final_counts=counts, ordinary_task_ids=sorted(index.direct_task_ids), complete_structure=bool(require_complete),
        complete_solution=False, global_DAG_checked=False)


def shape(problem, routes, groups, *, expected_signature=None, deadline=None, require_complete=True):
    routes, index, counts = _layout(problem, routes, deadline)
    result = _shape(problem, routes, index, counts, list(groups), expected_signature, require_complete, deadline)
    _check(deadline)
    return result


def signature(problem, routes, groups, *, deadline=None):
    routes, index, counts = _layout(problem, routes, deadline)
    groups = [_group(problem, g, k+1) for k, g in enumerate(groups)]
    _shape(problem, routes, index, counts, groups, None, True, deadline)
    result = _plans_signature(problem, routes, index, counts, groups)
    _check(deadline)
    return result


def _forbidden(p, route, groups, a):
    forbidden = old._forbidden(p, route, groups, a)
    for group in groups:
        if group.get('source_type') != 'joint_pair' or group['a'] != a:
            continue
        incoming = [_leg(p, t, group['station_id'], 'RELAY_IN') for t in (group['i'], group['j'])]
        positions = sorted(route.index(-x) for x in incoming)
        if positions[1] != positions[0]+1:
            raise ValueError('old joint drop adjacency broken')
        forbidden.add(positions[0]+1)
    return forbidden


def _surface(p, route, a, groups, parent, schedule, deadline):
    _check(deadline)
    route = tuple(route)
    positions = {v: k for k, v in enumerate(parent[a])}
    return dict(route=route, points=[p.point_for_visit(v) for v in route], loads=_loads(route),
        banned=_forbidden(p, route, groups, a), home=p.home_points()[a],
        prefix=[0.]+[schedule.event_times_min[a, positions[v]] for v in route])


def _first_table(p, surface, task, deadline):
    """Exact first-pickup detours and their best valid g<h for each h.

    For a fixed h, the second-pickup/H geometry is independent of g.  Thus
    retaining the smallest first detour is exact for min geometry/min drop
    proxy heads only.  It is not an N/L/J dominance rule.
    """
    points, loads, banned = surface['points'], surface['loads'], surface['banned']
    P, V, n = p.task(task).pickup, p.speed_km_per_min, len(points)
    detours, starts, best_before, counts_before = [], [], [], []
    best, count = None, 0
    for g in range(n+1):
        _check(deadline)
        if loads[g] > 1:
            best, count = None, 0
        best_before.append(best)
        counts_before.append(count)
        u = surface['home'] if not g else points[g-1]
        delta = u.distance_to(P)
        if g < n:
            delta += P.distance_to(points[g])-u.distance_to(points[g])
        detours.append(float(delta))
        starts.append(float(surface['prefix'][g]+u.distance_to(P)/V))
        if loads[g] <= 1 and g not in banned:
            row = (float(delta), g)
            if best is None or row < best:
                best = row
            count += 1
    return dict(detours=detours, starts=starts, best_before=best_before, counts_before=counts_before)


def _joint_row(p, surface, pair, a, H, order, g, h, incoming, first_table, deadline):
    _check(deadline)
    points, V = surface['points'], p.speed_km_per_min
    first, second = order
    P, Q = p.task(first).pickup, p.task(second).pickup
    R = next(s.point for s in p.relay_stations if s.id == H)
    shared = Q.distance_to(R)
    if shared <= 1e-10:
        return None
    u = surface['home'] if not h else points[h-1]
    if g == h:
        delta = u.distance_to(P)+P.distance_to(Q)+shared
        if h < len(points):
            delta += R.distance_to(points[h])-u.distance_to(points[h])
        pickup = surface['prefix'][h]+u.distance_to(P)/V
        second_clock = pickup+P.distance_to(Q)/V
    else:
        first_delta = first_table['detours'][g]
        delta = first_delta+u.distance_to(Q)+shared
        if h < len(points):
            delta += R.distance_to(points[h])-u.distance_to(points[h])
        pickup = first_table['starts'][g]
        second_clock = surface['prefix'][h]+first_delta/V+u.distance_to(Q)/V
    drop = second_clock+shared/V
    mapping = dict(zip(pair, incoming))
    return dict(gap=g, end_gap=h, task_ids=list(pair), aircraft=a, station_id=H,
        first_task_id=first, second_task_id=second, pickup_order_task_ids=list(order),
        in_leg_ids=list(incoming), first_in_leg_id=mapping[first], second_in_leg_id=mapping[second],
        pickup_position=g, second_pickup_position=h+1, drop_positions=[h+2, h+3],
        delta_distance_km=float(delta), estimated_pickup_min=float(pickup),
        estimated_second_pickup_min=float(second_clock), estimated_drop_min=float(drop),
        shared_in_loaded_distance_km=float(shared), scoring_scope=PROXY_SCOPE)


def _joint_rows(p, surface, pair, a, H, incoming, deadline, first_cache, *, profile=False, statistics=None):
    """All legal two-pickup positions, or exact geometry/drop proxy heads."""
    rows, logical = [], 0
    n, loads, banned = len(surface['route']), surface['loads'], surface['banned']
    for order in (pair, tuple(reversed(pair))):
        key = (a, order[0])
        if key not in first_cache:
            first_cache[key] = _first_table(p, surface, order[0], deadline)
            if statistics is not None:
                statistics['source_first_pickup_tables_built'] += 1
        elif statistics is not None:
            statistics['source_first_pickup_table_hits'] += 1
        table = first_cache[key]
        for h in range(n+1):
            _check(deadline)
            if loads[h] or h in banned:
                continue
            if p.task(order[1]).pickup.distance_to(next(s.point for s in p.relay_stations if s.id == H)) <= 1e-10:
                continue
            if profile:
                choices = [h]
                if table['best_before'][h] is not None:
                    choices.append(table['best_before'][h][1])
                logical += 1+table['counts_before'][h]
            else:
                choices = []
                for g in range(h, -1, -1):
                    _check(deadline)
                    if loads[g] > 1:
                        break
                    if g not in banned:
                        choices.append(g)
                logical += len(choices)
            for g in choices:
                row = _joint_row(p, surface, pair, a, H, order, g, h, incoming, table, deadline)
                if row is not None:
                    rows.append(row)
    if statistics is not None:
        name = 'source_profile_positions' if profile else 'source_full_positions'
        statistics[name] += logical
        if profile:
            statistics['source_profile_summary_rows'] += len(rows)
    return rows


def _joint_route(route, row):
    g, h = row['gap'], row['end_gap']
    return (route[:g]+(row['first_in_leg_id'],)+route[g:h]
        +(row['second_in_leg_id'], *(-x for x in row['in_leg_ids']))+route[h:])


def _out_rows(p, surface, c, pair, H, outgoing, availability, deadline, *, contiguous_only=False):
    """Legacy separated-delivery domain with old joint-drop gaps protected."""
    points, loads, banned = surface['points'], surface['loads'], surface['banned']
    R = next(s.point for s in p.relay_stations if s.id == H)
    V, n, rows = p.speed_km_per_min, len(points), []
    for g in range(n+1):
        _check(deadline)
        if loads[g] or g in banned:
            continue
        u = surface['home'] if not g else points[g-1]
        arrival = surface['prefix'][g]+u.distance_to(R)/V
        T = max(*availability, arrival)
        for first, last in (pair, tuple(reversed(pair))):
            D1, D2 = p.task(first).delivery, p.task(last).delivery
            if R.distance_to(D1) <= 1e-10:
                continue
            first_clock = T+R.distance_to(D1)/V
            middle_clock = first_clock
            for h in range(g, g+1 if contiguous_only else n+1):
                _check(deadline)
                if loads[h] >= 2:
                    break
                if h > g:
                    middle_clock += (D1.distance_to(points[g]) if h == g+1 else points[h-2].distance_to(points[h-1]))/V
                if h in banned:
                    continue
                if h == g:
                    delta = u.distance_to(R)+R.distance_to(D1)+D1.distance_to(D2)
                    if h < n:
                        delta += D2.distance_to(points[h])-u.distance_to(points[h])
                    end = first_clock+D1.distance_to(D2)/V
                else:
                    head = u.distance_to(R)+R.distance_to(D1)+D1.distance_to(points[g])-u.distance_to(points[g])
                    tail = points[h-1].distance_to(D2)
                    if h < n:
                        tail += D2.distance_to(points[h])-points[h-1].distance_to(points[h])
                    delta = head+tail
                    end = middle_clock+points[h-1].distance_to(D2)/V
                rows.append(dict(gap=g, end_gap=h, first_task_id=first, last_task_id=last,
                    delta_distance_km=float(delta), estimated_first_delivery_min=float(first_clock),
                    estimated_second_delivery_min=float(end), estimated_receiver_arrival_min=float(arrival),
                    estimated_second_take_min=float(T), out_leg_ids=list(outgoing), scoring_scope=PROXY_SCOPE))
    return rows


def propose_catalogue(problem, parent_routes, groups, *, deadline, weights, scales, max_families=16,
        source_option_limit=3, receiver_option_limit=3, position_limit=12, feeder_limit=3,
        base_pool_limit=64, statistics=None):
    started = perf_counter()
    stats = {} if statistics is None else statistics
    if not isinstance(stats, dict):
        raise ValueError('mutable statistics dictionary required')
    stats.update(completed=False, deadline_reached=False, source_profile_positions=0, source_profile_summary_rows=0,
        source_profiles_scored=0, feeder_profiles_omitted=0, current_same_owner_pairs=0,
        role_site_families_proxy_scored=0, source_full_positions=0, receiver_full_positions=0,
        source_positions_omitted=0, receiver_positions_omitted=0, complete_atomic_candidates=0,
        duplicate_candidates_omitted=0, structural_candidates_rejected=0,
        source_first_pickup_tables_built=0, source_first_pickup_table_hits=0)
    try:
        if deadline is None or type(deadline) is bool or not isfinite(deadline):
            raise ValueError('finite absolute deadline required')
        bounds = ((max_families, 64), (source_option_limit, 3), (receiver_option_limit, 3),
            (position_limit, 256), (feeder_limit, 7), (base_pool_limit, 512))
        if (any(type(v) is not int or not 1 <= v <= maximum for v, maximum in bounds)
            or max(source_option_limit, receiver_option_limit) > position_limit):
            raise ValueError('positive finite joint proposal bounds required')
        old.costs.weighted_key(Score(0, 0., 0.), tuple(weights), tuple(scales))
        _check(deadline, stats)
        routes, index, counts = _layout(problem, parent_routes, deadline)
        current_shape = shape(problem, routes, groups, deadline=deadline)
        old_signature = signature(problem, routes, groups, deadline=deadline)
        checked = evaluate_solution(problem, routes, _force_full=True)
        _check(deadline, stats)
        schedule = GlobalRelayEvaluator(problem).evaluate(routes)
        _check(deadline, stats)
        if not checked.valid or not schedule.feasible:
            raise ValueError('actual complete feasible parent required')
        groups = old_signature['groups']
        by_owner = {a: sorted(t for t in index.direct_task_ids if index.delivery_route_by_task[t] == a)
            for a in range(problem.drone_count)}
        stations = sorted(problem.relay_stations, key=lambda s: s.id)
        if not stations:
            raise ValueError('existing registered fixed sites required')
        station_caps = {s.id: base_pool_limit//len(stations)+int(k < base_pool_limit % len(stations))
            for k, s in enumerate(stations)}
        # Strong references and all caches are local to this one real parent.
        surfaces = [_surface(problem, route, a, groups, routes, schedule, deadline) for a, route in enumerate(routes)]
        original_distances = [_distance(problem, route, a) for a, route in enumerate(routes)]
        legs = {}
        for identifier, leg in problem.leg_registry.items():
            _check(deadline, stats)
            if problem.task_relay_candidates and leg.relay_id not in problem.task_relay_candidates.get(leg.task_id, ()):
                continue
            key = (leg.task_id, leg.relay_id, leg.kind)
            if key in legs:
                raise ValueError('unique eligible task/site transport legs required')
            legs[key] = identifier
        first_cache, source_cache, receiver_cache = {}, {}, {}
        pools = {s.id: [] for s in stations}
        for c, tasks in by_owner.items():
            for pair in combinations(tasks, 2):
                _check(deadline, stats)
                i, j = pair
                stats['current_same_owner_pairs'] += 1
                native = tuple(v for v in routes[c] if abs(v) not in pair)
                receiver = _surface(problem, native, c, groups, routes, schedule, deadline)
                removed_delta = _distance(problem, native, c)-original_distances[c]
                for station in stations:
                    _check(deadline, stats)
                    H = station.id
                    incoming = [legs.get((t, H, 'RELAY_IN')) for t in pair]
                    outgoing = [legs.get((t, H, 'RELAY_OUT')) for t in pair]
                    if any(x is None for x in (*incoming, *outgoing)):
                        continue
                    profiles = []
                    for a in range(problem.drone_count):
                        _check(deadline, stats)
                        if a == c:
                            continue
                        rows = _joint_rows(problem, surfaces[a], pair, a, H, incoming, deadline,
                            first_cache, profile=True, statistics=stats)
                        stats['source_profiles_scored'] += 1
                        if not rows:
                            continue
                        geo = min(rows, key=lambda r: (r['delta_distance_km'], r['estimated_pickup_min'],
                            r['estimated_drop_min'], r['gap'], r['end_gap'], r['first_task_id']))
                        early = min(rows, key=lambda r: (r['estimated_drop_min'], r['delta_distance_km'],
                            r['gap'], r['end_gap'], r['first_task_id']))
                        profiles.append(dict(aircraft=a, geometry=geo, earliest=early))
                    geometry = sorted(profiles, key=lambda x: (x['geometry']['delta_distance_km'], x['geometry']['estimated_drop_min'], x['aircraft']))
                    early = sorted(profiles, key=lambda x: (x['earliest']['estimated_drop_min'], x['earliest']['delta_distance_km'], x['aircraft']))
                    kept = []
                    for item in (*[stream[0] for stream in (geometry, early) if stream], *geometry):
                        if item['aircraft'] not in {x['aircraft'] for x in kept} and len(kept) < feeder_limit:
                            kept.append(item)
                    stats['feeder_profiles_omitted'] += len(profiles)-len(kept)
                    for item in kept:
                        source = item['geometry']
                        rows = _out_rows(problem, receiver, c, pair, H, outgoing,
                            (source['estimated_drop_min'],)*2, deadline, contiguous_only=True)
                        if not rows:
                            continue
                        best = min(rows, key=lambda r: (r['delta_distance_km'], r['estimated_second_take_min'], r['gap'], r['first_task_id']))
                        family = dict(i=i, j=j, a=item['aircraft'], b=item['aircraft'], c=c,
                            station_id=H, source_type='joint_pair', source_joint_profile=source,
                            geometry_proxy_delta_km=float(source['delta_distance_km']+removed_delta+best['delta_distance_km']),
                            estimated_second_take_proxy_min=best['estimated_second_take_min'],
                            receiver_profile_gap=best['gap'], receiver_profile_first_task_id=best['first_task_id'],
                            scoring_scope=PROXY_SCOPE)
                        stats['role_site_families_proxy_scored'] += 1
                        pools[H].append(family)
                        if len(pools[H]) > 2*station_caps[H]:
                            pools[H] = _pool(pools[H], station_caps[H], deadline)
        pools = {H: _pool(rows, station_caps[H], deadline) for H, rows in pools.items()}
        families = []
        for depth in range(max((len(rows) for rows in pools.values()), default=0)):
            for station in stations:
                _check(deadline, stats)
                rows = pools[station.id]
                if depth < len(rows) and len(families) < max_families:
                    families.append(rows[depth])
        candidates, seen = [], set()
        for fi, family in enumerate(families):
            _check(deadline, stats)
            i, j, a, c, H = (family[k] for k in ('i', 'j', 'a', 'c', 'station_id'))
            pair = (i, j)
            incoming = [legs[t, H, 'RELAY_IN'] for t in pair]
            outgoing = [legs[t, H, 'RELAY_OUT'] for t in pair]
            source_key = (i, j, a, H)
            if source_key not in source_cache:
                rows = _joint_rows(problem, surfaces[a], pair, a, H, incoming, deadline,
                    first_cache, statistics=stats)
                chosen = _retain_positions(rows, position_limit, source_option_limit,
                    'estimated_pickup_min', 'estimated_drop_min', deadline)
                stats['source_positions_omitted'] += len(rows)-len(chosen)
                source_cache[source_key] = chosen
            native = tuple(v for v in routes[c] if abs(v) not in pair)
            receiver = _surface(problem, native, c, groups, routes, schedule, deadline)
            for source in source_cache[source_key]:
                cache_key = (i, j, c, H, source['estimated_drop_min'])
                if cache_key not in receiver_cache:
                    rows = _out_rows(problem, receiver, c, pair, H, outgoing,
                        (source['estimated_drop_min'],)*2, deadline)
                    chosen = _retain_positions(rows, position_limit, receiver_option_limit,
                        'estimated_first_delivery_min', 'estimated_second_delivery_min', deadline)
                    stats['receiver_full_positions'] += len(rows)
                    stats['receiver_positions_omitted'] += len(rows)-len(chosen)
                    receiver_cache[cache_key] = chosen
                for out in receiver_cache[cache_key]:
                    _check(deadline, stats)
                    child = list(routes)
                    child[a] = _joint_route(routes[a], source)
                    child[c] = _out_route(native, out, pair)
                    child = tuple(child)
                    new = dict(group_id=len(groups)+1, **{k: family[k] for k in ('i', 'j', 'a', 'b', 'c', 'station_id', 'source_type')})
                    try:
                        shape(problem, child, [*groups, new], expected_signature=old_signature, deadline=deadline)
                    except (ValueError, KeyError, IndexError):
                        stats['structural_candidates_rejected'] += 1
                        continue
                    identity = _digest(child)
                    if identity in seen:
                        stats['duplicate_candidates_omitted'] += 1
                        continue
                    seen.add(identity)
                    stats['complete_atomic_candidates'] += 1
                    candidates.append(dict(routes=[list(r) for r in child], new_group=new,
                        family_index=fi, candidate_index=len(candidates), routes_sha256=identity,
                        source_layouts=[source], receiver_layout=out, scoring_scope=PROXY_SCOPE,
                        complete_atomic_transport_plans=True, full_global_checked=False))
        _check(deadline, stats)
        result = dict(families=families, complete_candidates=candidates, parent_routes_sha256=_digest(routes),
            parent_signature=old_signature, parent_shape=current_shape,
            metadata=dict(configuration=dict(max_families=max_families, source_option_limit=source_option_limit,
                receiver_option_limit=receiver_option_limit, position_limit=position_limit, feeder_limit=feeder_limit,
                base_pool_limit=base_pool_limit, station_pool_limits={str(H): n for H, n in station_caps.items()},
                family_pool_policy='bounded_geometry_and_estimated_take_heads_per_station_then_station_round_robin',
                source_group_policy='same_source_joint_pair_only',
                source_position_policy='two_pickup_orders_firstP_gap_g_load_at_most_one_through_empty_h_secondP_jointdrop',
                source_profile_policy='exact_geometry_and_parent_clock_drop_heads_via_cached_first_detour_not_NLD_bound',
                receiver_position_policy='empty_doubletake_first_delivery_native_interval_second_delivery',
                scoring_scope=PROXY_SCOPE, uses_pure_DIRECT_suffix_prices=False),
                station_pool_counts={str(H): len(rows) for H, rows in pools.items()},
                ordinary_task_ids=sorted(index.direct_task_ids), current_group_count=len(groups),
                eligible_pair_scope='all_current_same_final_owner_whole_DIRECT_pairs',
                complete_candidate_scope='atomic_all_tasks_present_structural_only_driver_must_full_global_validate'),
            completed=True)
        stats.update(completed=True, runtime_seconds=perf_counter()-started)
        result['metadata']['statistics'] = dict(stats)
        result['completed_at_perf_counter'] = perf_counter()
        result['deadline_perf_counter'] = deadline
        _check(deadline, stats)
        return result
    except Deadline:
        stats.update(completed=False, deadline_reached=True, runtime_seconds=perf_counter()-started)
        raise
