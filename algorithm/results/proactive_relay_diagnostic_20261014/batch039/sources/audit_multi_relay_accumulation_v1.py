"""Independent stdlib proof for complete multi-group relay accumulation.

All clocks are reconstructed on the entire handoff DAG. No two-pin helper,
pure-DIRECT suffix price, producer or production evaluator is used to compute
expected values. Stored entities and finite search coverage have distinct
proof scopes.
"""
from __future__ import annotations

from hashlib import sha256
import json
from math import hypot, isfinite

try:
    from .audit_weighted_relay_ablation import _geometry, reference_schedule
    from .audit_weighted_direct_pair import require
    from .audit_deadline_window_repooling import _bind
    from .audit_split_feeder import _physical, _weighted
    from .audit_repooling import _identity, _owners, _direct_task, _shared_flight
    from .audit_mobile_relay import _point
    from .audit_pinned_repooling import _plans
except ImportError:
    from audit_weighted_relay_ablation import _geometry, reference_schedule
    from audit_weighted_direct_pair import require
    from audit_deadline_window_repooling import _bind
    from audit_split_feeder import _physical, _weighted
    from audit_repooling import _identity, _owners, _direct_task, _shared_flight
    from audit_mobile_relay import _point
    from audit_pinned_repooling import _plans

SCOPE = 'multi_relay_accumulation'
EPSILON = 1e-9
SHARED_EPSILON_KM = 1e-10


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _group_descriptor(tasks, topology, group, expected_id, geometry):
    require(isinstance(group, dict), 'A relay group needs its actual static descriptor')
    _bind(group.get('group_id'), expected_id, 'relay group sequential identity')
    count = topology['drone_count']
    for field in ('i', 'j'):
        require(type(group.get(field)) is int and group[field] in tasks, 'Unknown relay-group task ' + field)
    require(group['i'] < group['j'], 'A relay group must contain two ordered distinct tasks')
    for field in ('a', 'b', 'c'):
        require(type(group.get(field)) is int and 0 <= group[field] < count, 'Unknown actual relay-group aircraft ' + field)
    require(len({group['a'], group['b'], group['c']}) == 3, 'A relay group needs two different feeders and a third receiver')
    homes, stations, registry, count, capacity, maximum, speed = geometry
    require(capacity == 2 and type(group.get('station_id')) is int and group['station_id'] in stations,
            'Multi-relay groups retain actual capacity two and an original fixed station')
    for task in (group['i'], group['j']):
        require({leg['kind'] for leg in registry.values() if leg['task_id'] == task and leg['relay_id'] == group['station_id']}
                == {'RELAY_IN', 'RELAY_OUT'}, 'A group may use only originally registered paired transport legs')


def _group_metrics(tasks, topology, routes, reference, group, geometry):
    homes, stations, registry, count, capacity, maximum, speed = geometry
    pair = (group['i'], group['j']);a, b, c = (group[n] for n in ('a', 'b', 'c'));H = group['station_id']
    handoffs = {h['task_id']: h for h in reference['handoffs']};availability = {};identifiers = []
    for task, source in zip(pair, (a, b)):
        require(task in handoffs, 'A declared relay group lost its actual transfer plan')
        h = handoffs[task]
        require(h['cross_uav'] and h['inbound_drone_id'] == source + 1 and h['outbound_drone_id'] == c + 1
                and h['relay_id'] == H, 'A group changed its source/receiver/site or lost true cross-aircraft transfer')
        availability[str(task)] = h['drop_time_min'];identifiers.append(h['out_leg'])
    route = routes[c];positions = sorted(route.index(v) for v in identifiers)
    require(positions[1] == positions[0] + 1, 'Every group must retain adjacent two OUT pickups')
    shared, segments, loads = _shared_flight(tasks, homes, stations, registry, route, c, identifiers, reference['event_times_min'][c])
    require([loads[p] for p in positions] == [0, 1], 'Every group receiver must enter its two pickups physically empty')
    require(shared > SHARED_EPSILON_KM, 'Every group needs positive actual double-loaded flight distance')
    first, second = positions;previous = homes[c] if not first else _point(tasks, stations, registry, route[first - 1])
    arrival = (0. if not first else reference['event_times_min'][c][first - 1]) + hypot(
        stations[H][0] - previous[0], stations[H][1] - previous[1]) / speed
    order = [registry[route[p]]['task_id'] for p in positions];times = [reference['event_times_min'][c][p] for p in positions]
    # Other groups can affect the prefix and all suffix clocks. Only the
    # adjacent same-station pair itself has no intervening flight/service.
    _bind(times, [max(arrival, availability[str(order[0])]), max(arrival, *availability.values())], 'actual cross-group take clocks')
    offsets = {str(_identity(v, registry)): reference['delivery_times_min'][str(_identity(v, registry))] - times[-1]
        for k, v in enumerate(route) if k > second and v < 0 and (abs(v) not in registry or registry[abs(v)]['kind'] == 'RELAY_OUT')}
    return dict(group_id=group['group_id'], station_id=H, task_ids=list(pair), source_aircraft=[a, b], receiver_aircraft=c,
        source_availability_min=availability, receiver_arrival_min=arrival, take_order_task_ids=order, take_times_min=times,
        second_take_min=times[-1], receiver_waiting_min=times[-1] - arrival,
        ground_cache_by_task_min={str(t): times[order.index(t)] - availability[str(t)] for t in pair},
        suffix_delivery_offsets_min=offsets, shared_loaded_distance_km=float(shared))


def _classifications(tasks, baseline, reference, selected):
    old = {t for t in tasks if baseline['delivery_times_min'][str(t)] - tasks[t][2] > EPSILON}
    new = {t for t in tasks if reference['delivery_times_min'][str(t)] - tasks[t][2] > EPSILON}
    return {name + '_' + suffix + '_task_ids': sorted(ids & changes)
        for name, ids in (('selected', selected), ('native', set(tasks) - selected))
        for suffix, changes in (('rescued', old - new), ('newly_late', new - old))}


def _family_key(groups):
    return sorted([[g[k] for k in ('i', 'j', 'a', 'b', 'c', 'station_id')] for g in groups])


def _structure(tasks, topology, routes, groups, context):
    """Whole-plan/cargo proof only; an inter-route DAG may still have a cycle."""
    geometry = context['geometry'];homes, stations, registry, count, capacity, maximum, speed = geometry
    require(len(routes) == count, 'Structural proposal changed actual fleet size')
    grouped = {};metrics = []
    for number, g in enumerate(groups, 1):
        _group_descriptor(tasks, topology, g, number, geometry)
        for t in (g['i'], g['j']): require(t not in grouped, 'Groups reuse a task');grouped[t] = g
    events = {t: [] for t in tasks};signed = set();final = [0] * count;known = set(tasks) | set(registry)
    for a, route in enumerate(routes):
        held = set()
        for k, v in enumerate(route):
            require(type(v) is int and v and abs(v) in known and v not in signed,
                    'Structural proposal has an unknown or duplicate service event')
            signed.add(v);t = _identity(v, registry);events[t].append((a, k, v))
            if v > 0: held.add(v)
            else:
                require(-v in held, 'Structural proposal delivers a cargo it does not hold');held.remove(-v)
                if abs(v) not in registry or registry[abs(v)]['kind'] == 'RELAY_OUT': final[a] += 1
            require(len(held) <= capacity, 'Structural proposal exceeds actual cargo capacity')
        require(not held, 'Structural proposal leaves undelivered cargo')
    for t, task_events in events.items():
        if t not in grouped:
            require(len(task_events) == 2 and {v for _, _, v in task_events} == {t, -t}
                    and len({a for a, _, _ in task_events}) == 1, 'Ordinary task is not a complete whole DIRECT plan')
        else:
            g = grouped[t];source = g['a'] if t == g['i'] else g['b'];c = g['c'];H = g['station_id']
            legs = {leg['kind']: ident for ident, leg in registry.items() if leg['task_id'] == t and leg['relay_id'] == H}
            x, y = legs['RELAY_IN'], legs['RELAY_OUT']
            require(len(task_events) == 4 and {(a, v) for a, _, v in task_events} == {(source, x), (source, -x), (c, y), (c, -y)},
                    'Structural group has a missing, duplicate or wrong-role transport leg')
    require(final == [r['task_count'] for r in context['base']['route_records']], 'Structural proposal changed actual final quotas')
    for g in groups:
        pair = (g['i'], g['j']);c = g['c'];incoming = [];outgoing = []
        for t in pair:
            legs = {leg['kind']: ident for ident, leg in registry.items() if leg['task_id'] == t and leg['relay_id'] == g['station_id']}
            incoming.append(legs['RELAY_IN']);outgoing.append(legs['RELAY_OUT'])
        takes = sorted(routes[c].index(v) for v in outgoing)
        require(takes[1] == takes[0] + 1, 'Structural group has split doubletake')
        shared, _, loads = _shared_flight(tasks, homes, stations, registry, routes[c], c, outgoing, [0.] * len(routes[c]))
        require([loads[k] for k in takes] == [0, 1] and shared > SHARED_EPSILON_KM, 'Structural group lost empty doubletake or positive actual shared flight')
        metrics.append(dict(group=dict(g), take_positions=takes, shared_loaded_distance_km=float(shared), in_leg_ids=incoming,
            out_leg_ids=outgoing, source_drop_positions=[routes[g['a']].index(-incoming[0]), routes[g['b']].index(-incoming[1])]))
    plans = _plans(routes, registry, set(grouped));owners = _owners(routes, registry)
    require(owners == context['base_owners'], 'Structural proposal changed an original final owner')
    signature = dict(groups=[dict(g) for g in groups], protected_task_ids=sorted(grouped),
        plans=[[t, plans[t]['mode'], plans[t]['station_id'], list(plans[t]['leg_ids']), owners[t], plans[t]['roles']['pickup'][0], plans[t]['roles']['take'][0]] for t in sorted(grouped)],
        selected_events=[[v for v in route if _identity(v, registry) in grouped] for route in routes], final_counts=final)
    shape = dict(groups=metrics, group_count=len(groups), relay_task_ids=sorted(grouped), cross_uav_count=len(grouped),
        final_counts=final, ordinary_task_ids=sorted(set(tasks) - set(grouped)), complete_structure=True, complete_solution=False, global_DAG_checked=False)
    return signature, shape


def _context(tasks, topology, base_routes):
    geometry = _geometry(tasks, topology)
    registry = geometry[2]
    base = reference_schedule(tasks, base_routes, topology)
    require(not base['handoffs'] and all(_direct_task(base_routes, registry, t) for t in tasks),
            'Multi-group accumulation requires its genuine complete pure-DIRECT base')
    return dict(geometry=geometry, base=base, base_owners=_owners(base_routes, registry),
                reference_cache={tuple(map(tuple, base_routes)): base})


def _audit_state(tasks, topology, base_routes, routes, groups, context, *, telemetry=None, weights=None, scales=None, deadline=None):
    require(isinstance(groups, (list, tuple)), 'Missing complete relay-group set')
    homes, stations, registry, count, capacity, maximum, speed = context['geometry']
    base = context['base']
    encoded = tuple(map(tuple, routes))
    if encoded not in context['reference_cache']:
        context['reference_cache'][encoded] = reference_schedule(tasks, routes, topology)
    reference = context['reference_cache'][encoded];owners = _owners(routes, registry);base_owners = context['base_owners']
    require(owners == base_owners, 'Quota-neutral group accumulation must preserve every original final owner')
    counts = [r['task_count'] for r in reference['route_records']]
    require(counts == [r['task_count'] for r in base['route_records']], 'A complete group state changed actual per-aircraft final quotas')
    selected = set();metrics = []
    for number, group in enumerate(groups, 1):
        _group_descriptor(tasks, topology, group, number, context['geometry'])
        pair = {group['i'], group['j']}
        require(not selected & pair, 'Different groups cannot reuse an original task')
        require(all(owners[t] == group['c'] for t in pair), 'A relay group lost its unchanged final receiver responsibility')
        selected.update(pair);metrics.append(_group_metrics(tasks, topology, routes, reference, group, context['geometry']))
    require({h['task_id'] for h in reference['handoffs']} == selected and len(reference['handoffs']) == len(selected),
            'The actual full relay set differs from the complete disjoint group directory')
    require(all(_direct_task(routes, registry, t) for t in set(tasks) - selected), 'Every nongroup task must remain a whole DIRECT plan')
    result = dict(passed=True, complete_solution=True, pin_scope=SCOPE, groups=[dict(g) for g in groups], group_metrics=metrics,
        score=_physical(reference['score']), post_global_score=_physical(reference['score']), core_reference=reference,
        final_tasks_per_aircraft=counts, actual_final_owners={str(t): owners[t] for t in sorted(tasks)},
        event_times_min=reference['event_times_min'], delivery_times_min=reference['delivery_times_min'],
        current_source_drop_availability_min={str(t): h['drop_time_min'] for h in reference['handoffs'] for t in (h['task_id'],)},
        current_shared_double_loaded_km_by_group={str(m['group_id']): m['shared_loaded_distance_km'] for m in metrics},
        selected_native_classifications=_classifications(tasks, base, reference, selected),
        cross_uav_count=reference['relay_statistics']['cross_uav_handoff_count'], relay_task_count=len(reference['handoffs']),
        group_count=len(groups), routes_sha256=_digest(routes))
    if telemetry is not None:
        require(isinstance(telemetry, dict) and telemetry.get('valid') is True and telemetry.get('complete') is True,
                'A multi-group state must have a real complete validity certificate')
        _bind(telemetry.get('pin_scope'), SCOPE, 'current multi-group scope');_bind(telemetry.get('routes'), routes, 'actual complete group routes')
        for field in ('groups', 'group_metrics', 'score', 'post_global_score', 'actual_final_owners', 'event_times_min', 'delivery_times_min',
                      'current_source_drop_availability_min', 'current_shared_double_loaded_km_by_group', 'selected_native_classifications',
                      'cross_uav_count', 'relay_task_count', 'routes_sha256'):
            _bind(telemetry.get(field), result[field], 'actual multi-group ' + field)
        if 'group_count' in telemetry: _bind(telemetry['group_count'], len(groups), 'actual complete group count')
        if 'group_family_key' in telemetry: _bind(telemetry['group_family_key'], _family_key(groups), 'actual canonical group family')
        if 'structure_signature' in telemetry:
            signature, _ = _structure(tasks, topology, routes, groups, context)
            _bind(telemetry['structure_signature'], signature, 'actual complete group identity signature')
        _weighted(telemetry.get('key'), result['score'], weights, scales, 'complete multi-group weighted key')
        for old in ('table_score', 'window', 'independent_delta_sum'):
            require(old not in telemetry, 'A local/pure-DIRECT price cannot certify a multi-group full state')
        verified = telemetry.get('full_verified_at_perf_counter');declared = telemetry.get('deadline_perf_counter', deadline)
        require(type(verified) in (int, float) and isfinite(verified) and type(declared) in (int, float) and isfinite(declared)
                and verified < declared, 'Multi-group checkpoint was not fully verified before its absolute deadline')
        if deadline is not None: _bind(declared, deadline, 'actual multi-group deadline')
        for field in ('accepted_at_perf_counter', 'selected_as_best_at_perf_counter'):
            if field in telemetry:
                t = telemetry[field];require(type(t) in (int, float) and isfinite(t) and verified <= t < declared,
                                            'Multi-group best selection missed its full verified deadline')
    return result


def audit_state(tasks, topology, base_routes, routes, groups, *, telemetry=None, weights=None, scales=None, deadline=None):
    """Prove every group using one immutable geometry/base context per call."""
    return _audit_state(tasks, topology, base_routes, routes, groups, _context(tasks, topology, base_routes),
                        telemetry=telemetry, weights=weights, scales=scales, deadline=deadline)


def _audit_step(tasks, topology, base_routes, parent_routes, child_routes, parent_groups, new_group, context, *,
                telemetry=None, weights=None, scales=None, deadline=None):
    parent = _audit_state(tasks, topology, base_routes, parent_routes, parent_groups, context)
    _group_descriptor(tasks, topology, new_group, len(parent_groups) + 1, context['geometry'])
    registry = context['geometry'][2];pair = {new_group['i'], new_group['j']}
    already = {t for g in parent_groups for t in (g['i'], g['j'])}
    require(not pair & already and all(_direct_task(parent_routes, registry, t) for t in pair), 'New group tasks must be current ungrouped whole DIRECT requests')
    owners = _owners(parent_routes, registry)
    require(all(owners[t] == new_group['c'] for t in pair), 'Both newly selected tasks must really belong to current receiver C')
    child = _audit_state(tasks, topology, base_routes, child_routes, [*parent_groups, new_group], context,
                        telemetry=telemetry, weights=weights, scales=scales, deadline=deadline)
    strip = lambda rs: [[v for v in r if _identity(v, registry) not in pair] for r in rs]
    _bind(strip(child_routes), strip(parent_routes), 'Adding one group must preserve every other signed event and owner')
    require(_plans(parent_routes, registry, already) == _plans(child_routes, registry, already), 'An old group changed transport identity/role/site')
    for old in parent_groups:
        selected = {old['i'], old['j']};seq = lambda rs: [[v for v in r if _identity(v, registry) in selected] for r in rs]
        _bind(seq(child_routes), seq(parent_routes), 'Old-group per-aircraft selected signed subsequences')
    result = {**child, 'parent_score': parent['score'], 'parent_core_reference': parent['core_reference'],
        'group_added': dict(new_group), 'parent_routes_sha256': _digest(parent_routes),
        'old_group_clock_changes': [dict(group_id=m['group_id'], before=m, after=child['group_metrics'][k])
                                    for k, m in enumerate(parent['group_metrics'])]}
    if telemetry is not None:
        _bind(telemetry.get('group_added'), new_group, 'actual one group added')
        _bind(telemetry.get('parent_routes_sha256'), _digest(parent_routes), 'actual complete group parent hash')
    return result


def audit_step(tasks, topology, base_routes, parent_routes, child_routes, parent_groups, new_group, *,
               telemetry=None, weights=None, scales=None, deadline=None):
    """Only current same-C whole DIRECT i/j become the next actual group."""
    return _audit_step(tasks, topology, base_routes, parent_routes, child_routes, parent_groups, new_group,
        _context(tasks, topology, base_routes), telemetry=telemetry, weights=weights, scales=scales, deadline=deadline)


PROXY_SCOPE = 'current_parent_geometry_and_clock_estimates_only_not_global_price_or_bound'


def _distance(route, home, tasks, stations, registry):
    previous = home;total = 0.
    for v in route:
        point = _point(tasks, stations, registry, v);total += hypot(point[0] - previous[0], point[1] - previous[1]);previous = point
    return total


def _loads(route):
    values = [0]
    for v in route: values.append(values[-1] + (1 if v > 0 else -1))
    return values


def _forbidden(route, aircraft, groups, registry):
    result = set()
    for group in groups:
        if group['c'] != aircraft: continue
        out = [ident for ident, leg in registry.items() if leg['task_id'] in (group['i'], group['j'])
               and leg['kind'] == 'RELAY_OUT' and leg['relay_id'] == group['station_id']]
        p = sorted(route.index(v) for v in out);require(p[1] == p[0] + 1, 'Parent old group lost adjacent takes')
        result.add(p[0] + 1)
    return result


def _candidate_layout(tasks, topology, parent_routes, parent_groups, candidate, context):
    geometry = context['geometry'];homes, stations, registry, count, capacity, maximum, speed = geometry
    group = candidate['new_group'];_group_descriptor(tasks, topology, group, len(parent_groups) + 1, geometry)
    i, j, a, b, c, H = (group[k] for k in ('i', 'j', 'a', 'b', 'c', 'station_id'))
    parent_ref = context['reference_cache'][tuple(map(tuple, parent_routes))]
    owners = _owners(parent_routes, registry)
    require(all(owners[t] == c and _direct_task(parent_routes, registry, t) for t in (i, j)), 'Catalogue pair is not current same-owner whole DIRECT')
    rebuilt = [tuple(r) for r in parent_routes];source_rows = candidate.get('source_layouts')
    require(isinstance(source_rows, list) and len(source_rows) == 2, 'Atomic proposal lost its actual two source layouts')
    distance = lambda x, y: hypot(x[0] - y[0], x[1] - y[1])
    for row, t, owner in zip(source_rows, (i, j), (a, b)):
        route = tuple(parent_routes[owner]);g, h = row.get('gap'), row.get('end_gap')
        require(type(g) is int and type(h) is int and 0 <= g <= h <= len(route), 'Invalid actual source two-gap positions')
        loads = _loads(route);banned = _forbidden(route, owner, parent_groups, registry)
        require(max(loads[g:h + 1], default=0) <= 1 and g not in banned and h not in banned, 'Actual source carry interval exceeds capacity or splits old doubletake')
        x = next(ident for ident, leg in registry.items() if leg['task_id'] == t and leg['relay_id'] == H and leg['kind'] == 'RELAY_IN')
        for field, expected in (('in_leg_id', x), ('task_id', t), ('aircraft', owner), ('station_id', H), ('scoring_scope', PROXY_SCOPE)):
            _bind(row.get(field), expected, 'actual source proposal ' + field)
        rebuilt[owner] = route[:g] + (x,) + route[g:h] + (-x,) + route[h:]
        points = [_point(tasks, stations, registry, v) for v in route];u = homes[owner] if not g else points[g - 1];P = tasks[t][0];R = stations[H]
        start = (0. if not g else parent_ref['event_times_min'][owner][g - 1]) + distance(u, P) / speed
        if g == h: end = start + distance(P, R) / speed
        else:
            pickup_detour = distance(u, P) + distance(P, points[g]) - distance(u, points[g])
            end = parent_ref['event_times_min'][owner][h - 1] + pickup_detour / speed + distance(points[h - 1], R) / speed
        _bind(row.get('estimated_pickup_min'), start, 'declared current-parent pickup estimate only')
        _bind(row.get('estimated_drop_min'), end, 'declared current-parent drop estimate only')
        delta = _distance(rebuilt[owner], homes[owner], tasks, stations, registry) - _distance(route, homes[owner], tasks, stations, registry)
        _bind(row.get('delta_distance_km'), delta, 'actual source added geometric length')
    native = tuple(v for v in parent_routes[c] if abs(v) not in (i, j));row = candidate.get('receiver_layout')
    require(isinstance(row, dict), 'Atomic proposal lost its receiver layout')
    g, h = row.get('gap'), row.get('end_gap');loads = _loads(native);banned = _forbidden(native, c, parent_groups, registry)
    require(type(g) is int and type(h) is int and 0 <= g <= h <= len(native) and loads[g] == 0
            and max(loads[g:h + 1], default=0) <= 1 and g not in banned and h not in banned,
            'Actual receiver layout must start empty and respect its whole carry interval/old groups')
    first, last = row.get('first_task_id'), row.get('last_task_id');require({first, last} == {i, j}, 'Receiver proposal lost its selected delivery order')
    out = [next(ident for ident, leg in registry.items() if leg['task_id'] == t and leg['relay_id'] == H and leg['kind'] == 'RELAY_OUT') for t in (i, j)]
    _bind(row.get('out_leg_ids'), out, 'actual receiver registered OUT legs');_bind(row.get('scoring_scope'), PROXY_SCOPE, 'receiver estimate scope')
    by_task = dict(zip((i, j), out));rebuilt[c] = native[:g] + tuple(out) + (-by_task[first],) + native[g:h] + (-by_task[last],) + native[h:]
    points = [_point(tasks, stations, registry, v) for v in native];previous = homes[c] if not g else points[g - 1]
    positions = {v: k for k, v in enumerate(parent_routes[c])}
    prefix = 0. if not g else parent_ref['event_times_min'][c][positions[native[g - 1]]]
    R = stations[H];arrival = prefix + distance(previous, R) / speed;T = max(arrival, *(r['estimated_drop_min'] for r in source_rows))
    firsttime = T + distance(R, tasks[first][1]) / speed;point = tasks[first][1];end = firsttime
    for v in native[g:h]:
        next_point = _point(tasks, stations, registry, v);end += distance(point, next_point) / speed;point = next_point
    end += distance(point, tasks[last][1]) / speed
    for field, expected in (('estimated_receiver_arrival_min', arrival), ('estimated_second_take_min', T),
                            ('estimated_first_delivery_min', firsttime), ('estimated_second_delivery_min', end)):
        _bind(row.get(field), expected, 'current-parent receiver proxy ' + field)
    delta = _distance(rebuilt[c], homes[c], tasks, stations, registry) - _distance(native, homes[c], tasks, stations, registry)
    _bind(row.get('delta_distance_km'), delta, 'actual receiver added geometric length')
    # The persisted catalogue omits full fleets. Actual gaps, original legs
    # and the parent are sufficient to reconstruct their complete entities;
    # the digest later binds a saved full checkpoint to this exact layout.
    _bind(candidate.get('routes_sha256'), _digest(rebuilt), 'actual atomic candidate route hash')
    strip = lambda rs: [[v for v in r if _identity(v, registry) not in {i, j}] for r in rs]
    _bind(strip(rebuilt), strip(parent_routes), 'Catalogue atomic proposal preserves every unselected event')
    _structure(tasks, topology, rebuilt, [*parent_groups, group], context)


def _audit_catalogue(tasks, topology, parent_routes, parent_groups, catalogue, context, driver_config):
    require(isinstance(catalogue, dict) and catalogue.get('completed') is True, 'Only a completed finite atomic catalogue may be published')
    _bind(catalogue.get('candidate_representation'), 'parent_plus_layouts_and_routes_sha256', 'actual compact atomic catalogue representation')
    _bind(catalogue.get('complete_candidates_contain_routes'), False, 'persisted atomic catalogue reconstructs routes from actual layouts')
    _bind(catalogue.get('parent_routes_sha256'), _digest(parent_routes), 'actual multi-group catalogue parent')
    signature, shape = _structure(tasks, topology, parent_routes, parent_groups, context)
    _bind(catalogue.get('parent_signature'), signature, 'actual complete parent group signature')
    _bind(catalogue.get('parent_shape'), shape, 'actual parent local structure, not a global certificate')
    completed, declared = catalogue.get('completed_at_perf_counter'), catalogue.get('deadline_perf_counter')
    require(type(completed) in (int, float) and isfinite(completed) and type(declared) in (int, float) and isfinite(declared)
            and completed < declared, 'The finite catalogue was published past its absolute deadline')
    metadata = catalogue.get('metadata');require(isinstance(metadata, dict), 'Missing finite catalogue evidence scope')
    config = metadata.get('configuration');require(isinstance(config, dict), 'Missing finite geometric frontier bounds')
    for field in ('max_families', 'source_option_limit', 'receiver_option_limit', 'position_limit'):
        _bind(config.get(field), driver_config[field], 'actual finite proposal bound ' + field)
    _bind(config.get('scoring_scope'), PROXY_SCOPE, 'finite current-parent proxy scope')
    _bind(config.get('uses_pure_DIRECT_suffix_prices'), False, 'No pure-DIRECT native price is a multi-group bound')
    _bind(metadata.get('complete_candidate_scope'), 'atomic_all_tasks_present_structural_only_driver_must_full_global_validate', 'catalogue has no global feasibility certificate')
    registry = context['geometry'][2];owners = _owners(parent_routes, registry)
    ordinary = {t for t in tasks if _direct_task(parent_routes, registry, t)}
    _bind(metadata.get('ordinary_task_ids'), sorted(ordinary), 'actual current ordinary requests')
    _bind(metadata.get('current_group_count'), len(parent_groups), 'actual parent group count')
    families = catalogue.get('families');candidates = catalogue.get('complete_candidates')
    require(isinstance(families, list) and isinstance(candidates, list) and len(families) <= config['max_families'], 'Catalogue exceeds its declared finite family cap')
    require(len(candidates) <= len(families) * config['source_option_limit'] ** 2 * config['receiver_option_limit'],
            'Atomic catalogue exceeds its declared actual source/receiver frontier product')
    family_keys = set()
    for family in families:
        descriptor = dict(group_id=len(parent_groups) + 1, **{k: family[k] for k in ('i', 'j', 'a', 'b', 'c', 'station_id')})
        _group_descriptor(tasks, topology, descriptor, len(parent_groups) + 1, context['geometry'])
        require({family['i'], family['j']} <= ordinary and owners[family['i']] == owners[family['j']] == family['c'], 'Retained family does not originate in current same-C whole DIRECT tasks')
        key = tuple(family[k] for k in ('i', 'j', 'a', 'b', 'c', 'station_id'))
        require(key not in family_keys, 'The finite family directory duplicates a role/site task family');family_keys.add(key)
        _bind(family.get('scoring_scope'), PROXY_SCOPE, 'retained family finite heuristic scope')
        require(all(type(family.get(k)) in (int, float) and isfinite(family[k]) for k in ('geometry_proxy_delta_km', 'estimated_second_take_proxy_min')),
                'Retained family proxy must contain finite geometry/current-clock estimates')
        homes, stations, _, _, _, _, speed = context['geometry'];H = family['station_id'];R = stations[H]
        parent_ref = context['reference_cache'][tuple(map(tuple, parent_routes))]
        norm = lambda x, y: hypot(x[0] - y[0], x[1] - y[1])
        point = lambda v: _point(tasks, stations, registry, v)
        gaps = family.get('source_profile_gaps');require(isinstance(gaps, list) and len(gaps) == 2, 'Family lost its two actual source-profile gaps')
        deltas = [];ready = []
        for t, aircraft, gap in zip((family['i'], family['j']), (family['a'], family['b']), gaps):
            route = parent_routes[aircraft];loads = _loads(route)
            require(type(gap) is int and 0 <= gap <= len(route) and loads[gap] <= 1
                    and gap not in _forbidden(route, aircraft, parent_groups, registry), 'Family source-profile gap is outside its actual legal finite surface')
            u = homes[aircraft] if not gap else point(route[gap - 1]);P = tasks[t][0]
            delta = norm(u, P) + norm(P, R)
            if gap < len(route): delta += norm(R, point(route[gap])) - norm(u, point(route[gap]))
            deltas.append(delta)
            ready.append((0. if not gap else parent_ref['event_times_min'][aircraft][gap - 1]) + norm(u, P) / speed + norm(P, R) / speed)
        c = family['c'];native = tuple(v for v in parent_routes[c] if abs(v) not in (family['i'], family['j']))
        gap = family.get('receiver_profile_gap');first = family.get('receiver_profile_first_task_id')
        require(type(gap) is int and 0 <= gap <= len(native) and _loads(native)[gap] == 0
                and gap not in _forbidden(native, c, parent_groups, registry) and first in (family['i'], family['j']),
                'Family receiver profile is not its actual empty legal gap/order')
        last = family['j'] if first == family['i'] else family['i'];u = homes[c] if not gap else point(native[gap - 1])
        D1, D2 = tasks[first][1], tasks[last][1]
        require(norm(R, D1) > SHARED_EPSILON_KM, 'Family profile lacks positive initial double-loaded flight')
        out_delta = norm(u, R) + norm(R, D1) + norm(D1, D2)
        if gap < len(native): out_delta += norm(D2, point(native[gap])) - norm(u, point(native[gap]))
        removal_delta = _distance(native, homes[c], tasks, stations, registry) - _distance(parent_routes[c], homes[c], tasks, stations, registry)
        old_positions = {v: k for k, v in enumerate(parent_routes[c])}
        prefix = 0. if not gap else parent_ref['event_times_min'][c][old_positions[native[gap - 1]]]
        _bind(family['geometry_proxy_delta_km'], sum(deltas) + removal_delta + out_delta, 'selected family true geometric proxy')
        _bind(family['estimated_second_take_proxy_min'], max(*ready, prefix + norm(u, R) / speed), 'selected family current-parent timing estimate only')
    seen = set()
    for index, candidate in enumerate(candidates):
        require('routes' not in candidate, 'A compact atomic catalogue must not carry an unbound alternate route fleet')
        _bind(candidate.get('candidate_index'), index, 'atomic candidate actual index')
        fi = candidate.get('family_index');require(type(fi) is int and 0 <= fi < len(families), 'Atomic candidate references an undeclared family')
        f = families[fi];expected = dict(group_id=len(parent_groups) + 1, **{k: f[k] for k in ('i', 'j', 'a', 'b', 'c', 'station_id')})
        _bind(candidate.get('new_group'), expected, 'atomic candidate actual role/site group')
        require(candidate.get('complete_atomic_transport_plans') is True and candidate.get('full_global_checked') is False,
                'Atomic proposal cannot claim a driver full/global checkpoint')
        _bind(candidate.get('scoring_scope'), PROXY_SCOPE, 'atomic proposal only scope')
        _candidate_layout(tasks, topology, parent_routes, parent_groups, candidate, context)
        require(candidate['routes_sha256'] not in seen, 'Atomic catalogue duplicated a complete route proposal');seen.add(candidate['routes_sha256'])
    stats = metadata.get('statistics');require(isinstance(stats, dict) and stats.get('completed') is True, 'Missing actual completed finite-catalogue counters')
    _bind(stats.get('complete_atomic_candidates'), len(candidates), 'actual stored atomic candidate count')
    by_owner = [sum(owners[t] == a for t in ordinary) for a in range(len(parent_routes))]
    _bind(stats.get('current_same_owner_pairs'), sum(n * (n - 1) // 2 for n in by_owner), 'actual current same-owner task-pair coverage')
    for field in ('source_positions_omitted', 'receiver_positions_omitted', 'feeder_profiles_omitted', 'duplicate_candidates_omitted', 'structural_candidates_rejected'):
        require(type(stats.get(field)) is int and stats[field] >= 0, 'Finite omission/rejection count must be honest nonnegative telemetry')
    return dict(passed=True, catalogue_sha256=_digest(catalogue), families_replayed=len(families), atomic_structural_layouts_replayed=len(candidates),
        candidate_representation='parent_plus_layouts_and_routes_sha256',
        reconstructed_route_digests_bound=True,
        current_same_owner_pairs=stats['current_same_owner_pairs'], all_retained_atomic_layouts_structurally_rebuilt=True,
        all_omitted_family_prices_replayed=False, all_atomic_candidates_globally_feasible_claimed=False)


def audit_metadata(tasks, topology, base_routes, metadata, *, weights, scales, deadline):
    """Compact proof of saved full-state beam, all old groups and guard chain."""
    require(isinstance(metadata, dict) and metadata.get('pin_scope') == SCOPE, 'Missing actual multi-group helper metadata')
    _bind(metadata.get('deadline_perf_counter'), deadline, 'actual accumulation deadline')
    config = metadata.get('configuration');require(isinstance(config, dict), 'Missing frozen multi-group search bounds')
    for field, maximum in (('max_groups', 3), ('beam_width', 16), ('max_families', 64), ('source_option_limit', 3),
                           ('receiver_option_limit', 3), ('position_limit', 256), ('snapshot_limit', 64)):
        require(type(config.get(field)) is int and 1 <= config[field] <= maximum, 'Invalid finite accumulation bound ' + field)
    for field, expected in (('relay_only_module', True), ('allow_worse_complete_beam_states', True),
        ('original_DIRECT_guard_separate_archive', True), ('each_complete_candidate_standard_full_checked', True),
        ('beam_policy', 'canonical_group_roles_station_family_heads_then_full_key_fill'), ('native_repair', False),
        ('group_family_key_excludes_group_id_and_generation_order', True),
        ('pure_DIRECT_suffix_prices_are_bounds', False), ('old_group_clock_reuse', False), ('resources_homes_weights_unchanged', True)):
        _bind(config.get(field), expected, 'actual accumulation policy ' + field)
    context = _context(tasks, topology, base_routes)
    if metadata.get('initialization_completed') is not True:
        require(metadata.get('initialization_completed') is False and metadata.get('deadline_reached') is True,
                'Unpublished initialization requires an honest deadline failure')
        require(all(metadata.get(f) is None for f in ('input_snapshot', 'guard_archive', 'post_repair_snapshot', 'first_complete_candidate',
                    'best_constructed_candidate', 'best_key', 'final_routes_sha256')) and metadata.get('steps') == []
                and metadata.get('full_candidate_snapshots') == [] and metadata.get('accepted_improvements') == 0
                and metadata.get('returned_input_guard') is True and metadata.get('returned_checkpoint_checked_within_deadline') is False,
                'Expired initialization cannot publish or adopt a complete checkpoint')
        return dict(passed=True, initialization_completed=False, predeadline_checkpoint_verified=False,
                    saved_complete_states_replayed=0, complete_transition_edges_replayed=0, all_probes_entity_replayed=False)
    initial = metadata.get('input_snapshot');require(isinstance(initial, dict), 'A completed initialization lost the original DIRECT guard')
    _bind(metadata.get('guard_archive'), initial, 'separate immutable original DIRECT guard archive')
    _bind(initial.get('routes'), base_routes, 'actual original complete guard routes')
    require(initial.get('state_id') == 0 and initial.get('groups') == [] and initial.get('parent_state_id') is None
            and initial.get('parent_routes_sha256') is None and initial.get('group_added') is None,
            'An original DIRECT guard cannot claim a relay parent or group')
    initial_proof = _audit_state(tasks, topology, base_routes, base_routes, [], context,
        telemetry=initial, weights=weights, scales=scales, deadline=deadline)
    states = {0: initial};references = {};edges = {};catalogue_refs = [];all_checkpoints = []
    def prove(snapshot, parent=None, catalogue=None):
        require(isinstance(snapshot, dict) and type(snapshot.get('state_id')) is int and snapshot['state_id'] >= 0,
                'A complete accumulation state needs its actual integer ID')
        sid = snapshot['state_id'];digest = _digest(snapshot)
        if sid in states: _bind(snapshot, states[sid], 'one multi-group state ID cannot refer to different full entities')
        if digest in references: return
        if parent is None:
            require(sid == 0, 'A noninitial multi-group state lost its complete parent')
            proof = initial_proof
        else:
            require(snapshot.get('parent_state_id') == parent['state_id'] and sid > parent['state_id'], 'Multi-group parent pointer does not refer to its actual earlier complete state')
            candidate_index = snapshot.get('candidate_index');family_index = snapshot.get('family_index')
            candidates = catalogue['complete_candidates'];require(type(candidate_index) is int and 0 <= candidate_index < len(candidates), 'Saved state references an undeclared full-coverage proposal')
            candidate = candidates[candidate_index]
            _bind(candidate.get('routes_sha256'), _digest(snapshot['routes']), 'actual saved checkpoint equals reconstructed catalogue route')
            _bind(candidate.get('new_group'), snapshot['group_added'], 'actual saved catalogue group')
            _bind(candidate.get('family_index'), family_index, 'actual saved family index')
            proof = _audit_step(tasks, topology, base_routes, parent['routes'], snapshot['routes'], parent['groups'], snapshot['group_added'], context,
                telemetry=snapshot, weights=weights, scales=scales, deadline=deadline)
            edges[sid] = dict(state_id=sid, parent_state_id=parent['state_id'], group_added=snapshot['group_added'],
                proof_sha256=_digest(proof), old_group_clock_changes_sha256=_digest(proof['old_group_clock_changes']))
        references[digest] = dict(state_id=sid, snapshot_sha256=digest, routes_sha256=proof['routes_sha256'], score=proof['score'],
            group_count=proof['group_count'], full_clock_sha256=_digest(dict(event=proof['event_times_min'], delivery=proof['delivery_times_min'])),
            full_verified_at_perf_counter=snapshot['full_verified_at_perf_counter'])
        states[sid] = snapshot
    prove(initial)
    previous = [initial];steps = metadata.get('steps');require(isinstance(steps, list) and len(steps) <= config['max_groups'], 'Too many accumulation depths')
    generated = completed = max_retained = 0
    for depth, step in enumerate(steps, 1):
        _bind(step.get('depth'), depth, 'actual accumulation depth')
        _bind(step.get('parent_state_ids'), [s['state_id'] for s in previous], 'actual previous full beam parent IDs')
        parents = step.get('parent_records');retained = step.get('retained_states');checkpoints = step.get('complete_checkpoints')
        require(isinstance(parents, list) and isinstance(retained, list) and isinstance(checkpoints, list)
                and len(parents) <= len(previous) and len(retained) <= config['beam_width'], 'Actual parent/beam coverage exceeds its declared cap')
        contexts = {};step_count = 0
        for k, record in enumerate(parents):
            parent = previous[k];_bind(record.get('parent_state_id'), parent['state_id'], 'actual complete-parent prefix')
            _bind(record.get('parent_groups'), parent['groups'], 'actual existing group directory')
            catalogue = record.get('catalogue')
            if catalogue is None:
                require(metadata.get('deadline_reached') is True and record.get('completed') is False
                        and record.get('candidate_count') == 0 and k == len(parents) - 1 and depth == len(steps),
                        'An unpublished catalogue must be the honest terminal deadline prefix')
                continue
            _bind(catalogue.get('deadline_perf_counter'), deadline, 'actual complete-catalogue absolute deadline')
            cp = _audit_catalogue(tasks, topology, parent['routes'], parent['groups'], catalogue, context, config)
            _bind(record.get('parent_signature'), catalogue['parent_signature'], 'actual saved complete-parent signature')
            catalogue_refs.append(dict(depth=depth, parent_state_id=parent['state_id'], **cp))
            contexts[parent['state_id']] = (parent, catalogue)
            count = record.get('candidate_count')
            require(type(count) is int and 0 <= count <= len(catalogue['complete_candidates']), 'Actual candidate probe prefix exceeds its finite catalogue')
            if record.get('completed') is True:
                require(count == len(catalogue['complete_candidates']), 'A completed parent omitted declared complete proposals')
            else:
                require(metadata.get('deadline_reached') is True and k == len(parents) - 1 and depth == len(steps), 'An unfinished parent must be the terminal deadline prefix')
            step_count += count
        _bind(step.get('candidate_count'), step_count, 'actual full-coverage proposal accounting');generated += step_count
        for snapshot in [*checkpoints, *retained]:
            require(snapshot.get('parent_state_id') in contexts, 'A saved state lost its actual published catalogue parent')
            parent, catalogue = contexts[snapshot['parent_state_id']]
            require(snapshot['candidate_index'] < next(r['candidate_count'] for r in parents if r['parent_state_id'] == parent['state_id']),
                    'A saved state was never reached by its actual parent probe prefix')
            prove(snapshot, parent, catalogue)
            _bind(snapshot.get('group_count'), depth, 'actual complete group count at depth')
        all_checkpoints.extend(checkpoints)
        require(len({_digest(s['routes']) for s in retained}) == len(retained), 'The complete beam duplicated route entities')
        ordering = [(tuple(s['key']), tuple(map(tuple, s['routes']))) for s in retained]
        require(ordering == sorted(ordering), 'The retained complete beam is not ranked by actual full key/routes')
        _bind(step.get('retained_family_count'), len({_digest(_family_key(s['groups'])) for s in retained}), 'actual retained canonical group-family diversity')
        if step.get('completed') is True:
            require(len(parents) == len(previous) and all(r['completed'] for r in parents), 'A completed depth omitted actual parent coverage')
            completed += 1
            if retained: max_retained = depth
        else: require(metadata.get('deadline_reached') is True and depth == len(steps), 'An unfinished depth must be the terminal deadline prefix')
        previous = retained
    _bind(metadata.get('proposed_complete_candidates'), generated, 'actual proposed full-coverage count')
    _bind(metadata.get('completed_depths'), completed, 'actual completed depth count')
    _bind(metadata.get('max_group_count_retained'), max_retained, 'actual fully retained maximum group count')
    valid = metadata.get('valid_complete_candidates');full_calls = metadata.get('full_validator_calls');global_calls = metadata.get('global_evaluations')
    require(all(type(v) is int and v >= 0 for v in (valid, full_calls, global_calls)) and len(edges) <= valid <= global_calls <= full_calls <= generated,
            'Full/global/valid probe counters contradict actual saved entities')
    if not metadata.get('deadline_reached'): require(global_calls == full_calls == generated, 'Finished probes omitted actual global/full verification calls')
    _bind(metadata.get('full_candidate_snapshots'), all_checkpoints[:config['snapshot_limit']], 'actual bounded chronological checkpoint aliases')
    _bind(metadata.get('first_complete_candidate'), all_checkpoints[0] if all_checkpoints else None, 'actual first complete relay candidate')
    best = initial;best_relay = None;improvements = 0;accepted_moves = []
    for snapshot in all_checkpoints:
        if best_relay is None or tuple(snapshot['key']) < tuple(best_relay['key']): best_relay = snapshot
        if tuple(snapshot['key']) < tuple(best['key']):
            require('selected_as_best_at_perf_counter' in snapshot, 'A selected strict best lost its deadline evidence')
            best = snapshot;improvements += 1
            accepted_moves.append(dict(state_id=snapshot['state_id'], parent_state_id=snapshot['parent_state_id'],
                group_added=snapshot['group_added'], full_snapshot=snapshot,
                full_verified_at_perf_counter=snapshot['full_verified_at_perf_counter'],
                accepted_at_perf_counter=snapshot['selected_as_best_at_perf_counter']))
        else: require('selected_as_best_at_perf_counter' not in snapshot, 'A nonimproving relay state falsely claims original-guard adoption')
    _bind(metadata.get('best_constructed_candidate'), best_relay, 'actual best relay state, possibly worse than original guard')
    _bind(metadata.get('post_repair_snapshot'), best, 'actual final strict guarded complete state')
    _bind(metadata.get('best_key'), best['key'], 'actual strict guarded final key')
    _bind(metadata.get('accepted_improvements'), improvements, 'actual complete strict-guard improvements')
    _bind(metadata.get('accepted_moves'), accepted_moves, 'actual predeadline strict-guard acceptance aliases')
    _bind(metadata.get('returned_input_guard'), improvements == 0, 'actual original DIRECT guard return flag')
    _bind(metadata.get('final_routes_sha256'), _digest(best['routes']), 'actual returned complete route hash')
    require(metadata.get('returned_checkpoint_checked_within_deadline') is True, 'Initialized return lost its checked complete checkpoint')
    return dict(passed=True, initialization_completed=True, predeadline_checkpoint_verified=True,
        saved_complete_state_references=list(references.values()), complete_transition_edges=list(edges.values()), catalogue_references=catalogue_refs,
        saved_complete_states_replayed=len(references), complete_transition_edges_replayed=len(edges), stored_checkpoints_replayed=len(all_checkpoints),
        generated_complete_proposals=generated, production_reported_valid_complete_proposals=valid,
        returned_score=best['score'], returned_routes_sha256=_digest(best['routes']), max_saved_group_count=max(s['group_count'] for s in references.values()),
        all_probes_entity_replayed=False, all_catalogue_proposals_claimed_globally_feasible=False)
