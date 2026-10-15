"""Independent typed extension for two parcels carried by one relay feeder.

The frozen raw, native-polish and paid-wrapper proofs retain their original
coverage, quota, complete-DAG, deadline and adoption checks. This scoped
extension proves the additional joint source geometry and transport identity;
it neither imports the search producer nor creates another batch ledger.
"""
from __future__ import annotations

from contextlib import contextmanager
from math import hypot, isfinite

try:
    from . import audit_multi_relay_accumulation_v1 as generic
    from . import audit_multi_relay_native_batch_v1 as paid
    from .audit_weighted_direct_pair import require
    from .audit_deadline_window_repooling import _bind
except ImportError:
    import audit_multi_relay_accumulation_v1 as generic
    import audit_multi_relay_native_batch_v1 as paid
    from audit_weighted_direct_pair import require
    from audit_deadline_window_repooling import _bind

PATCH_NAMES = ('_group_descriptor', '_structure', '_audit_state', '_group_metrics',
               '_audit_catalogue', '_candidate_layout')
SOURCE_TYPE = 'joint_pair'
SOURCE_POLICY = 'joint_pair_only_new_groups_mixed_parent_groups_supported'
_active = False


def _joint(group):
    return group.get('source_type') == SOURCE_TYPE


def _descriptor(tasks, topology, group, expected_id, geometry, original):
    require(isinstance(group, dict), 'A typed group needs its real descriptor')
    if not _joint(group):
        require('source_type' not in group, 'Unknown joint source type cannot use the old split proof')
        return original(tasks, topology, group, expected_id, geometry)
    _bind(group.get('group_id'), expected_id, 'typed group sequential identity')
    count = geometry[3]
    for name in ('i', 'j'):
        require(type(group.get(name)) is int and group[name] in tasks, 'Unknown joint group task')
    require(group['i'] < group['j'], 'Joint source requires two ordered distinct original tasks')
    for name in ('a', 'b', 'c'):
        require(type(group.get(name)) is int and 0 <= group[name] < count, 'Unknown joint source/receiver aircraft')
    require(group['a'] == group['b'] != group['c'], 'Joint source must be one real feeder distinct from its receiver')
    require(geometry[4] == 2 and type(group.get('station_id')) is int and group['station_id'] in geometry[1],
            'Joint source keeps capacity two and an original registered station')
    registry = geometry[2]
    for task in (group['i'], group['j']):
        require({leg['kind'] for leg in registry.values() if leg['task_id'] == task and leg['relay_id'] == group['station_id']}
                == {'RELAY_IN', 'RELAY_OUT'}, 'Joint source task has no original paired IN/OUT legs')


def _joint_source(tasks, topology, routes, group, geometry, reference=None):
    """Prove actual two-IN co-load and adjacent same-station drops, no cache score."""
    homes, stations, registry, count, capacity, maximum, speed = geometry
    pair = (group['i'], group['j']);aircraft = group['a'];route = routes[aircraft]
    incoming = [next(ident for ident, leg in registry.items() if leg['task_id'] == t
        and leg['relay_id'] == group['station_id'] and leg['kind'] == 'RELAY_IN') for t in pair]
    pickups = [route.index(v) for v in incoming];drops = [route.index(-v) for v in incoming]
    ordered_drops = sorted(drops)
    require(ordered_drops[1] == ordered_drops[0] + 1, 'Joint source must drop its parcels adjacently at the same original H')
    require(max(pickups) < min(drops), 'Both joint parcels must actually be picked before either drop')
    clocks = reference['event_times_min'][aircraft] if reference is not None else [0.] * len(route)
    shared, segments, loads = generic._shared_flight(tasks, homes, stations, registry, route, aircraft, incoming, clocks)
    require(shared > generic.SHARED_EPSILON_KM, 'Joint source must fly positive distance actually holding both IN parcels')
    require([loads[k] for k in ordered_drops] == [2, 1], 'Joint source double-drop must release its two actually held parcels')
    result = dict(source_type=SOURCE_TYPE, group_id=group['group_id'], aircraft=aircraft,
        task_ids=list(pair), station_id=group['station_id'], in_leg_ids=incoming,
        pickup_positions=pickups, drop_positions=drops, shared_in_loaded_distance_km=float(shared))
    if reference is not None:
        times = [clocks[k] for k in drops]
        _bind(times[0], times[1], 'Joint source same-H adjacent drops have identical actual clock')
        handoffs = {row['task_id']: row for row in reference['handoffs']}
        for task, clock in zip(pair, times):
            row = handoffs[task]
            require(row['inbound_drone_id'] == aircraft + 1 and row['outbound_drone_id'] == group['c'] + 1
                    and row['cross_uav'] and row['relay_id'] == group['station_id'], 'Joint IN/OUT role or actual cross identity changed')
            _bind(row['drop_time_min'], clock, 'Joint source actual global handoff drop clock')
        result.update(pickup_times_min=[clocks[k] for k in pickups], drop_times_min=times)
    return result


def _forbidden(route, aircraft, groups, registry):
    result = generic._forbidden(route, aircraft, groups, registry)
    for group in groups:
        if not _joint(group) or group['a'] != aircraft: continue
        legs = [ident for ident, leg in registry.items() if leg['task_id'] in (group['i'], group['j'])
            and leg['relay_id'] == group['station_id'] and leg['kind'] == 'RELAY_IN']
        drops = sorted(route.index(-v) for v in legs)
        require(drops[1] == drops[0] + 1, 'Parent joint source lost adjacent drops')
        result.add(drops[0] + 1)
    return result


def _norm(x, y):
    return hypot(x[0] - y[0], x[1] - y[1])


def _source_layout(tasks, topology, parent_routes, parent_groups, row, group, context):
    """Rebuild the exact source row; parent clocks are estimates, never a bound."""
    homes, stations, registry, count, capacity, maximum, speed = context['geometry']
    i, j, a, H = (group[k] for k in ('i', 'j', 'a', 'station_id'))
    route = tuple(parent_routes[a]);g, h = row.get('gap'), row.get('end_gap')
    require(type(g) is int and type(h) is int and 0 <= g <= h <= len(route), 'Invalid joint source original gap interval')
    loads = generic._loads(route);banned = _forbidden(route, a, parent_groups, registry)
    require(max(loads[g:h + 1], default=0) <= 1 and loads[h] == 0 and g not in banned and h not in banned,
            'Joint source carry interval/second pickup must respect capacity and old source/take blocks')
    order = row.get('pickup_order_task_ids')
    require(isinstance(order, list) and len(order) == 2 and all(type(t) is int for t in order)
            and set(order) == {i, j}, 'Joint source lost actual pickup order')
    incoming = [next(ident for ident, leg in registry.items() if leg['task_id'] == t
        and leg['relay_id'] == H and leg['kind'] == 'RELAY_IN') for t in (i, j)]
    by_task = dict(zip((i, j), incoming));x, y = (by_task[t] for t in order)
    for field, value in (('task_ids', [i, j]), ('aircraft', a), ('station_id', H), ('in_leg_ids', incoming),
        ('first_task_id', order[0]), ('second_task_id', order[1]), ('first_in_leg_id', x), ('second_in_leg_id', y),
        ('pickup_position', g), ('second_pickup_position', h + 1), ('drop_positions', [h + 2, h + 3]),
        ('scoring_scope', generic.PROXY_SCOPE)):
        _bind(row.get(field), value, 'actual joint source layout ' + field)
    result = route[:g] + (x,) + route[g:h] + (y, -incoming[0], -incoming[1]) + route[h:]
    reference = context['reference_cache'][tuple(map(tuple, parent_routes))]
    points = [generic._point(tasks, stations, registry, v) for v in route]
    previous = homes[a] if g == 0 else points[g - 1];P, Q = (tasks[t][0] for t in order);R = stations[H]
    first = (0. if g == 0 else reference['event_times_min'][a][g - 1]) + _norm(previous, P) / speed
    if g == h: second = first + _norm(P, Q) / speed
    else:
        pickup_detour = _norm(previous, P) + _norm(P, points[g]) - _norm(previous, points[g])
        second = reference['event_times_min'][a][h - 1] + pickup_detour / speed + _norm(points[h - 1], Q) / speed
    drop = second + _norm(Q, R) / speed
    delta = generic._distance(result, homes[a], tasks, stations, registry) - generic._distance(route, homes[a], tasks, stations, registry)
    shared = _norm(Q, R)
    require(shared > generic.SHARED_EPSILON_KM, 'Joint source layout lacks positive double-loaded flight')
    for field, value in (('estimated_pickup_min', first), ('estimated_second_pickup_min', second),
        ('estimated_drop_min', drop), ('delta_distance_km', delta), ('shared_in_loaded_distance_km', shared)):
        _bind(row.get(field), value, 'joint source current-parent geometry/clock proxy ' + field)
    return result


def _receiver_layout(tasks, topology, parent_routes, parent_groups, row, group, context, ready):
    homes, stations, registry, count, capacity, maximum, speed = context['geometry']
    i, j, c, H = (group[k] for k in ('i', 'j', 'c', 'station_id'))
    native = tuple(v for v in parent_routes[c] if abs(v) not in (i, j))
    g, h = row.get('gap'), row.get('end_gap');loads = generic._loads(native)
    require(type(g) is int and type(h) is int and 0 <= g <= h <= len(native), 'Invalid receiver original two-gap interval')
    banned = _forbidden(native, c, parent_groups, registry)
    require(loads[g] == 0 and max(loads[g:h + 1], default=0) <= 1 and g not in banned and h not in banned,
            'Receiver interval exceeds capacity or splits an existing source/drop block')
    first, last = row.get('first_task_id'), row.get('last_task_id')
    require(type(first) is int and type(last) is int and {first, last} == {i, j}, 'Receiver layout lost selected delivery order')
    outgoing = [next(ident for ident, leg in registry.items() if leg['task_id'] == t
        and leg['relay_id'] == H and leg['kind'] == 'RELAY_OUT') for t in (i, j)]
    _bind(row.get('out_leg_ids'), outgoing, 'actual typed receiver original OUT legs')
    _bind(row.get('scoring_scope'), generic.PROXY_SCOPE, 'typed receiver proxy scope')
    by_task = dict(zip((i, j), outgoing))
    rebuilt = native[:g] + tuple(outgoing) + (-by_task[first],) + native[g:h] + (-by_task[last],) + native[h:]
    points = [generic._point(tasks, stations, registry, v) for v in native]
    previous = homes[c] if g == 0 else points[g - 1]
    old_positions = {v: k for k, v in enumerate(parent_routes[c])}
    parent = context['reference_cache'][tuple(map(tuple, parent_routes))]
    prefix = 0. if g == 0 else parent['event_times_min'][c][old_positions[native[g - 1]]]
    R = stations[H];arrival = prefix + _norm(previous, R) / speed;T = max(arrival, *ready)
    firsttime = T + _norm(R, tasks[first][1]) / speed;end = firsttime;previous = tasks[first][1]
    for v in native[g:h]:
        point = generic._point(tasks, stations, registry, v);end += _norm(previous, point) / speed;previous = point
    end += _norm(previous, tasks[last][1]) / speed
    for field, value in (('estimated_receiver_arrival_min', arrival), ('estimated_second_take_min', T),
        ('estimated_first_delivery_min', firsttime), ('estimated_second_delivery_min', end)):
        _bind(row.get(field), value, 'current-parent typed receiver estimate only ' + field)
    delta = generic._distance(rebuilt, homes[c], tasks, stations, registry) - generic._distance(native, homes[c], tasks, stations, registry)
    _bind(row.get('delta_distance_km'), delta, 'actual receiver added geometric length')
    return rebuilt


def _joint_candidate(tasks, topology, parent_routes, parent_groups, candidate, context):
    group = candidate['new_group'];geometry = context['geometry'];registry = geometry[2]
    generic._group_descriptor(tasks, topology, group, len(parent_groups) + 1, geometry)
    i, j, c = (group[k] for k in ('i', 'j', 'c'));owners = generic._owners(parent_routes, registry)
    require(all(owners[t] == c and generic._direct_task(parent_routes, registry, t) for t in (i, j)),
            'Joint catalogue pair must be current same-owner whole DIRECT')
    rows = candidate.get('source_layouts')
    require(isinstance(rows, list) and len(rows) == 1, 'Joint atomic proposal must save one two-task source layout')
    rebuilt = [tuple(r) for r in parent_routes]
    rebuilt[group['a']] = _source_layout(tasks, topology, parent_routes, parent_groups, rows[0], group, context)
    require(isinstance(candidate.get('receiver_layout'), dict), 'Joint proposal lost receiver layout')
    ready = rows[0]['estimated_drop_min']
    rebuilt[c] = _receiver_layout(tasks, topology, parent_routes, parent_groups, candidate['receiver_layout'], group, context, [ready, ready])
    _bind(candidate.get('routes_sha256'), generic._digest(rebuilt), 'exact parent-plus-joint-layout complete fleet hash')
    strip = lambda rs: [[v for v in route if generic._identity(v, registry) not in (i, j)] for route in rs]
    _bind(strip(rebuilt), strip(parent_routes), 'Only the selected pair changes actual transport plan')
    generic._structure(tasks, topology, rebuilt, [*parent_groups, group], context)
    return rebuilt


def _joint_family(tasks, topology, parent_routes, parent_groups, family, context):
    group = dict(group_id=len(parent_groups) + 1, **{k: family[k] for k in ('i', 'j', 'a', 'b', 'c', 'station_id', 'source_type')})
    generic._group_descriptor(tasks, topology, group, len(parent_groups) + 1, context['geometry'])
    homes, stations, registry, count, capacity, maximum, speed = context['geometry']
    i, j, a, c, H = (group[k] for k in ('i', 'j', 'a', 'c', 'station_id'))
    owners = generic._owners(parent_routes, registry)
    require(all(generic._direct_task(parent_routes, registry, t) and owners[t] == c for t in (i, j)),
            'Retained joint family has no true same-C whole DIRECT provenance')
    _bind(family.get('scoring_scope'), generic.PROXY_SCOPE, 'joint retained family finite estimate scope')
    source = family.get('source_joint_profile');require(isinstance(source, dict), 'Joint family must preserve its actual two-task source profile')
    _source_layout(tasks, topology, parent_routes, parent_groups, source, group, context)
    native = tuple(v for v in parent_routes[c] if abs(v) not in (i, j));g = family.get('receiver_profile_gap')
    first = family.get('receiver_profile_first_task_id');loads = generic._loads(native)
    require(type(g) is int and 0 <= g <= len(native) and loads[g] == 0
            and g not in _forbidden(native, c, parent_groups, registry) and first in (i, j), 'Joint family receiver profile is not an empty real gap/order')
    last = j if first == i else i;R = stations[H];P = tasks[first][1];Q = tasks[last][1]
    require(_norm(R, P) > generic.SHARED_EPSILON_KM, 'Joint family receiver lacks positive shared loaded flight')
    previous = homes[c] if g == 0 else generic._point(tasks, stations, registry, native[g - 1])
    delta = _norm(previous, R) + _norm(R, P) + _norm(P, Q)
    if g < len(native):
        nxt = generic._point(tasks, stations, registry, native[g]);delta += _norm(Q, nxt) - _norm(previous, nxt)
    removal = generic._distance(native, homes[c], tasks, stations, registry) - generic._distance(parent_routes[c], homes[c], tasks, stations, registry)
    positions = {v: k for k, v in enumerate(parent_routes[c])};reference = context['reference_cache'][tuple(map(tuple, parent_routes))]
    prefix = 0. if g == 0 else reference['event_times_min'][c][positions[native[g - 1]]]
    _bind(family.get('geometry_proxy_delta_km'), source['delta_distance_km'] + removal + delta, 'joint family true full geometric proxy')
    _bind(family.get('estimated_second_take_proxy_min'), max(source['estimated_drop_min'], prefix + _norm(previous, R) / speed), 'joint family current-parent timing proxy only')
    return group


def _joint_catalogue(tasks, topology, parent_routes, parent_groups, catalogue, context, driver_config):
    require(isinstance(catalogue, dict) and catalogue.get('completed') is True, 'Only a completed finite joint catalogue may be published')
    _bind(catalogue.get('candidate_representation'), 'parent_plus_layouts_and_routes_sha256', 'actual joint compact catalogue format')
    _bind(catalogue.get('complete_candidates_contain_routes'), False, 'Joint candidate fleet is reconstructed from real parent/layout')
    _bind(catalogue.get('parent_routes_sha256'), generic._digest(parent_routes), 'joint catalogue real complete parent')
    signature, shape = generic._structure(tasks, topology, parent_routes, parent_groups, context)
    _bind(catalogue.get('parent_signature'), signature, 'joint catalogue real parent signature')
    _bind(catalogue.get('parent_shape'), shape, 'joint catalogue real parent structure only')
    completed, deadline = catalogue.get('completed_at_perf_counter'), catalogue.get('deadline_perf_counter')
    require(type(completed) in (int, float) and isfinite(completed) and type(deadline) in (int, float) and isfinite(deadline)
            and completed < deadline, 'Joint catalogue publication missed absolute deadline')
    meta = catalogue.get('metadata');require(isinstance(meta, dict), 'Joint catalogue needs finite coverage evidence')
    cfg = meta.get('configuration');require(isinstance(cfg, dict), 'Joint catalogue needs actual finite bounds')
    for field in ('max_families', 'source_option_limit', 'receiver_option_limit', 'position_limit'):
        _bind(cfg.get(field), driver_config[field], 'joint finite proposal bound ' + field)
    _bind(cfg.get('scoring_scope'), generic.PROXY_SCOPE, 'joint catalogue timing/geometry are estimates only')
    _bind(cfg.get('uses_pure_DIRECT_suffix_prices'), False, 'Joint catalogue is not a pure-DIRECT price bound')
    for field, value in (
        ('source_group_policy', 'same_source_joint_pair_only'),
        ('source_position_policy', 'two_pickup_orders_firstP_gap_g_load_at_most_one_through_empty_h_secondP_jointdrop'),
        ('source_profile_policy', 'exact_geometry_and_parent_clock_drop_heads_via_cached_first_detour_not_NLD_bound'),
        ('receiver_position_policy', 'empty_doubletake_first_delivery_native_interval_second_delivery')):
        _bind(cfg.get(field), value, 'actual joint finite source/receiver policy ' + field)
    _bind(meta.get('complete_candidate_scope'), 'atomic_all_tasks_present_structural_only_driver_must_full_global_validate', 'Joint structural catalogue is not a full global schedule certificate')
    registry = context['geometry'][2];owners = generic._owners(parent_routes, registry)
    ordinary = {t for t in tasks if generic._direct_task(parent_routes, registry, t)}
    _bind(meta.get('ordinary_task_ids'), sorted(ordinary), 'joint catalogue real current native task set')
    _bind(meta.get('current_group_count'), len(parent_groups), 'joint catalogue current parent group count')
    families, candidates = catalogue.get('families'), catalogue.get('complete_candidates')
    require(isinstance(families, list) and isinstance(candidates, list) and len(families) <= cfg['max_families'], 'Joint family pool exceeds finite cap')
    require(len(candidates) <= len(families) * cfg['source_option_limit'] * cfg['receiver_option_limit'], 'Joint frontier product exceeds actual one-source domain')
    keys = set();descriptors = []
    for family in families:
        require(_joint(family), 'New joint-source catalogue cannot silently propose old split-source families')
        descriptor = _joint_family(tasks, topology, parent_routes, parent_groups, family, context);descriptors.append(descriptor)
        key = tuple(descriptor[k] for k in ('i', 'j', 'a', 'b', 'c', 'station_id', 'source_type'))
        require(key not in keys, 'Joint family directory duplicates its task/roles/site identity');keys.add(key)
    seen = set()
    for index, candidate in enumerate(candidates):
        require('routes' not in candidate, 'Joint compact catalogue cannot hide alternate full routes')
        _bind(candidate.get('candidate_index'), index, 'actual joint candidate index')
        fi = candidate.get('family_index');require(type(fi) is int and 0 <= fi < len(families), 'Joint candidate references an undeclared family')
        _bind(candidate.get('new_group'), descriptors[fi], 'joint proposal actual group/family source type')
        require(candidate.get('complete_atomic_transport_plans') is True and candidate.get('full_global_checked') is False,
                'Joint compact proposal cannot claim complete DAG feasibility')
        _bind(candidate.get('scoring_scope'), generic.PROXY_SCOPE, 'joint atomic proposal estimate scope')
        generic._candidate_layout(tasks, topology, parent_routes, parent_groups, candidate, context)
        require(candidate['routes_sha256'] not in seen, 'Joint catalogue duplicated one complete atomic fleet');seen.add(candidate['routes_sha256'])
    stats = meta.get('statistics');require(isinstance(stats, dict) and stats.get('completed') is True, 'Joint catalogue counters need completed state')
    _bind(stats.get('complete_atomic_candidates'), len(candidates), 'joint actual complete atomic count')
    counts = [sum(owners[t] == a for t in ordinary) for a in range(len(parent_routes))]
    _bind(stats.get('current_same_owner_pairs'), sum(n * (n - 1) // 2 for n in counts), 'joint true same-owner pair surface')
    for field in ('source_positions_omitted', 'receiver_positions_omitted', 'feeder_profiles_omitted', 'duplicate_candidates_omitted', 'structural_candidates_rejected'):
        require(type(stats.get(field)) is int and stats[field] >= 0, 'Joint catalogue omission/rejection telemetry must be nonnegative')
    return dict(passed=True, catalogue_sha256=generic._digest(catalogue), families_replayed=len(families),
        atomic_structural_layouts_replayed=len(candidates), candidate_representation='parent_plus_layouts_and_routes_sha256',
        reconstructed_route_digests_bound=True, current_same_owner_pairs=stats['current_same_owner_pairs'],
        all_retained_atomic_layouts_structurally_rebuilt=True, all_omitted_family_prices_replayed=False,
        all_atomic_candidates_globally_feasible_claimed=False, source_type=SOURCE_TYPE)


@contextmanager
def joint_source_context(statistics=None):
    """Process-local independent typed proof, always restore frozen functions."""
    global _active
    if _active: raise RuntimeError('Nested independent joint-source proof contexts are unsupported')
    stats = {} if statistics is None else statistics
    require(isinstance(stats, dict), 'Joint-source proof statistics must be a dictionary')
    originals = {name: getattr(generic, name) for name in PATCH_NAMES}
    require(all(fn.__globals__ is generic.__dict__ for fn in originals.values()), 'Typed proof requires the original frozen generic auditor functions')
    unique = {}
    def descriptor(tasks, topology, group, expected_id, geometry):
        return _descriptor(tasks, topology, group, expected_id, geometry, originals['_group_descriptor'])
    def remember(tasks, topology, routes, group, geometry, reference=None):
        proof = _joint_source(tasks, topology, routes, group, geometry, reference)
        if reference is not None:
            key = generic._digest(dict(routes=routes, group=group, clocks=proof))
            unique.setdefault(key, dict(routes_sha256=generic._digest(routes), **proof))
        return proof
    def structure(tasks, topology, routes, groups, context):
        signature, shape = originals['_structure'](tasks, topology, routes, groups, context)
        for group in groups:
            if _joint(group): remember(tasks, topology, routes, group, context['geometry'])
        return signature, shape
    def metrics(tasks, topology, routes, reference, group, geometry):
        if _joint(group): remember(tasks, topology, routes, group, geometry, reference)
        return originals['_group_metrics'](tasks, topology, routes, reference, group, geometry)
    def state(*args, **kwargs):
        return originals['_audit_state'](*args, **kwargs)
    def candidate(tasks, topology, parent_routes, parent_groups, value, context):
        if _joint(value['new_group']): return _joint_candidate(tasks, topology, parent_routes, parent_groups, value, context)
        return originals['_candidate_layout'](tasks, topology, parent_routes, parent_groups, value, context)
    def catalogue(tasks, topology, parent_routes, parent_groups, value, context, config):
        # Mixed existing split/joint groups are supported, but the declared
        # new catalogue is joint-only; an old split run plus a label is not it.
        return _joint_catalogue(tasks, topology, parent_routes, parent_groups, value, context, config)
    patched = dict(_group_descriptor=descriptor, _structure=structure, _audit_state=state,
                   _group_metrics=metrics, _candidate_layout=candidate, _audit_catalogue=catalogue)
    _active = True
    try:
        for name, fn in patched.items(): setattr(generic, name, fn)
        yield stats
    finally:
        for name, fn in originals.items(): setattr(generic, name, fn)
        _active = False
        stats.update(patch_names=list(PATCH_NAMES), independent_functions_restored=all(getattr(generic, n) is f for n, f in originals.items()),
            unique_current_joint_source_proofs=list(unique.values()), source_shared_and_drop_clock_independently_checked=True,
            current_complete_DAG_proof_delegated_to_frozen_public_auditors=True,
            discarded_structural_catalogue_candidates_claimed_globally_feasible=False)


def _audit_metadata(tasks, topology, base_routes, metadata, *, relay, manifest, phase=None):
    """Existing callback interface; only the declared joint typed extension."""
    require(type(relay) is bool, 'Independent arm mode must be a real boolean')
    if not relay:
        return paid._audit_metadata(tasks, topology, base_routes, metadata, relay=False, manifest=manifest, phase=phase)
    extension = metadata.get('extension');require(isinstance(extension, dict), 'Joint-source arm must declare its minimal structure extension')
    for name, value in (('name', 'joint_source_pair_structure_context_v1'), ('source_policy', SOURCE_POLICY),
        ('alias_names', ['raw.structure', 'polish.structure', 'wrapper.structure']), ('restored', True)):
        _bind(extension.get(name), value, 'actual joint structure extension ' + name)
    expected_hooks = {name: dict(installed=True, restored=True) for name in extension['alias_names']}
    _bind(extension.get('hook_identity'), expected_hooks, 'actual three typed-structure aliases')
    entered, left, elapsed = (extension.get(name) for name in ('entered_at_perf_counter', 'left_at_perf_counter', 'elapsed_seconds'))
    require(all(type(v) in (int, float) and isfinite(v) for v in (entered, left, elapsed))
            and entered <= metadata['started_at_perf_counter'] <= left and elapsed >= 0,
            'Joint context setup/teardown clocks must cover actual paid wrapper')
    _bind(elapsed, left - entered, 'actual joint alias context paid elapsed wall')
    require(metadata['started_at_perf_counter'] + metadata['runtime_seconds'] <= left + 1e-8, 'Joint context left before actual paid wrapper return')
    if phase is not None:
        require(phase['started_at_perf_counter'] <= entered <= left <= phase['ended_at_perf_counter'], 'Joint context wall lies outside its actual worker method phase')
    proof_stats = {}
    with joint_source_context(proof_stats):
        proof = paid._audit_metadata(tasks, topology, base_routes, metadata, relay=True, manifest=manifest, phase=phase)
    return dict(**proof, joint_source_extension_evidence=dict(extension_sha256=generic._digest(extension), **proof_stats))
