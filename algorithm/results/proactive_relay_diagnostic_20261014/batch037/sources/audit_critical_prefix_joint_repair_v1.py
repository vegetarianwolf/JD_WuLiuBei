"""Independent physical proofs for causal-prefix/dynamic-quota repair.

Only frozen, standard-library auditors are imported.  A task-subset schedule
is explicitly a preview, never a complete objective or an admissible bound.
The genuine split-feeder seed and the later unrestricted-native repair have
different provenance contracts; no synthetic DIRECT source is constructed.
"""
from __future__ import annotations

from math import hypot, isfinite
from hashlib import sha256
import json

try:
    from . import audit_split_feeder_v4 as split
    from .audit_split_feeder import _physical, _weighted, _classes
    from .audit_deadline_window_repooling import _bind
    from .audit_pinned_repooling import _plans
    from .audit_repooling import _identity, _owners, _direct_task, _shared_flight
    from .audit_mobile_relay import _point
    from .audit_weighted_relay_ablation import _geometry, reference_schedule
    from .audit_weighted_direct_pair import require
    from .audit_window_damage_repair import _current_handoff
except ImportError:
    import audit_split_feeder_v4 as split
    from audit_split_feeder import _physical, _weighted, _classes
    from audit_deadline_window_repooling import _bind
    from audit_pinned_repooling import _plans
    from audit_repooling import _identity, _owners, _direct_task, _shared_flight
    from audit_mobile_relay import _point
    from audit_weighted_relay_ablation import _geometry, reference_schedule
    from audit_weighted_direct_pair import require
    from audit_window_damage_repair import _current_handoff

PIN_SCOPE = 'critical_prefix_joint_repair'
PARTIAL_SCOPE = 'partial_same_coverage_only'
EPSILON = 1e-9


def _ids(values, tasks, label):
    require(isinstance(values, (list, tuple)) and len(values) == len(set(values))
            and all(type(t) is int and t in tasks for t in values), label + ' must be distinct known task IDs')
    return set(values)


def _subset(tasks, topology, routes, present):
    """Filter the actual immutable registry, retaining physical homes/C/K."""
    _geometry(tasks, topology)
    if not present:
        require(len(routes) == topology['drone_count'] and all(not r for r in routes),
                'An empty task subset must have empty routes')
        records = [dict(task_count=0, distance_km=0., completion_time_min=0.,
                        arrival_times_min=[], load_after_event=[]) for _ in routes]
        return dict(score=dict(late_count=0, total_lateness_min=0., distance_km=0.),
                    route_records=records, event_times_min=[[] for _ in routes], delivery_times_min={},
                    handoffs=[], relay_statistics=dict(cross_uav_handoff_count=0, relay_task_count=0))
    selected = {t: tasks[t] for t in sorted(present)}
    frozen = {**topology, 'legs': [dict(leg) for leg in topology['legs'] if leg['task_id'] in present]}
    for field in ('task_relay_candidates', 'candidate_stations_by_task'):
        if isinstance(topology.get(field), dict):
            frozen[field] = {t: v for t, v in topology[field].items() if int(t) in present}
    return reference_schedule(selected, routes, frozen)


def _counts(reference):
    return [r['task_count'] for r in reference['route_records']]


def _pin_shape(tasks, topology, raw_routes, routes, reference, pinned, relay_enabled, seed_move):
    homes, stations, registry, count, capacity, maximum, speed = _geometry(tasks, topology)
    require(capacity == 2, 'Joint repair requires the actual capacity two')
    if relay_enabled:
        require(isinstance(seed_move, dict) and isinstance(seed_move.get('family'), dict), 'RELAY needs its real seed descriptor')
        family = seed_move['family']; expected = {family['i'], family['j']}
        require(pinned == expected, 'Only the two genuine relay whole plans are pinned')
        require(raw_routes is not None, 'RELAY pins need the actual raw route source')
        require(_plans(raw_routes, registry, pinned) == _plans(routes, registry, pinned),
                'A pinned mode/leg/H/source/receiver plan changed')
        subseq = lambda rs: [[v for v in r if _identity(v, registry) in pinned] for r in rs]
        require(subseq(raw_routes) == subseq(routes), 'Pinned signed-event subsequences changed')
        availability, shared, handoff, prefixes, segments, receiver = _current_handoff(
            tasks, topology, routes, reference, family)
    else:
        require(not pinned, 'DIRECT repair must have zero pins and all tasks free')
        require(not reference['handoffs'], 'DIRECT repair cannot create any relay')
        availability, shared, handoff, prefixes, segments, receiver = {}, 0., None, [], [], None
    ordinary = set(reference['delivery_times_min']) - {str(t) for t in pinned}
    require(all(_direct_task(routes, registry, int(t)) for t in ordinary), 'Every unpinned task must remain whole DIRECT')
    return availability, float(shared), handoff, prefixes, segments, receiver


def audit_partial_state(tasks, topology, routes, present_task_ids, *, target_final_counts,
                        raw_routes=None, pinned_task_ids=(), relay_enabled=False, seed_move=None,
                        telemetry=None, weights=None, scales=None, remaining_task_ids=None,
                        parent_routes=None, parent_present_task_ids=None, inserted_task_order=None,
                        owner_choices=None, insertions=None):
    """Prove a same-coverage DAG and optional actual joint insertion edge."""
    present = _ids(present_task_ids, tasks, 'Partial coverage')
    pinned = _ids(pinned_task_ids, tasks, 'Partial pins')
    require(pinned <= present, 'A partial state dropped a pinned plan')
    reference = _subset(tasks, topology, routes, present)
    _pin_shape(tasks, topology, raw_routes, routes, reference, pinned, relay_enabled, seed_move)
    counts = _counts(reference)
    require(isinstance(target_final_counts, (list, tuple)) and len(target_final_counts) == len(routes)
            and all(type(n) is int and 0 <= n <= topology['max_tasks_per_drone'] for n in target_final_counts)
            and sum(target_final_counts) == len(tasks), 'The complete target quota vector is invalid')
    if raw_routes is not None:
        _bind(target_final_counts, _counts(reference_schedule(tasks, raw_routes, topology)), 'genuine raw full quota vector')
    q = [target - actual for target, actual in zip(target_final_counts, counts)]
    require(min(q, default=0) >= 0 and sum(q) == len(tasks) - len(present), 'Partial quota remainder is not actual missing responsibility')
    missing = sorted(set(tasks) - present)
    if remaining_task_ids is not None:
        require(_ids(remaining_task_ids, tasks, 'Partial remaining tasks') == set(missing), 'Partial remaining task set differs')
    owners = _owners(routes, _geometry(tasks, topology)[2])
    transition = None
    if parent_routes is not None:
        require(parent_present_task_ids is not None, 'Parent transition requires actual parent coverage')
        before = _ids(parent_present_task_ids, tasks, 'Parent coverage')
        order = list(inserted_task_order or [])
        require(len(order) in (1, 2) and len(order) == len(set(order)) and set(order) == present - before
                and before <= present and not (set(order) & pinned), 'A beam edge must jointly restore its declared one/two whole DIRECT tasks')
        registry = _geometry(tasks, topology)[2]
        reduced = [[v for v in r if _identity(v, registry) not in set(order)] for r in routes]
        _bind(reduced, parent_routes, 'joint child keeps every parent signed event in place')
        parent = audit_partial_state(tasks, topology, parent_routes, sorted(before), target_final_counts=target_final_counts,
            raw_routes=raw_routes, pinned_task_ids=sorted(pinned), relay_enabled=relay_enabled, seed_move=seed_move)
        actual_choices = [owners[t] for t in order]
        if owner_choices is not None: _bind(owner_choices, actual_choices, 'dynamic actual owner choices')
        consumed = [actual_choices.count(a) for a in range(len(routes))]
        require(all(n <= slot for n, slot in zip(consumed, parent['remaining_quota_slots'])), 'Dynamic branch spends an unavailable owner slot')
        _bind(q, [slot - n for slot, n in zip(parent['remaining_quota_slots'], consumed)], 'dynamic exact quota consumption')
        # Removing later jobs reconstructs each intermediate physical state.  No
        # independently added cost matrix can stand in for either DAG replay.
        intermediate = []
        for k in range(1, len(order) + 1):
            later = set(order[k:]); rs = [[v for v in r if _identity(v, registry) not in later] for r in routes]
            ref = _subset(tasks, topology, rs, before | set(order[:k]))
            intermediate.append(_physical(ref['score']))
        if insertions is not None:
            require(isinstance(insertions, list) and len(insertions) == len(order), 'Missing actual insertion records')
            for k, (task, record) in enumerate(zip(order, insertions)):
                require(isinstance(record, dict) and {'task_id', 'owner', 'pickup_gap', 'delivery_gap'} <= set(record),
                        'Insertion record must identify the actual whole task, owner and both current-route gaps')
                later = set(order[k + 1:]); step = [[v for v in r if _identity(v, registry) not in later] for r in routes]
                a = owners[task]; p, d = step[a].index(task), step[a].index(-task)
                for name, expected in (('task_id', task), ('aircraft', a), ('owner', a), ('pickup_position', p), ('delivery_position', d)):
                    if name in record: _bind(record[name], expected, 'actual insertion ' + name)
                for name, expected in (('g', p), ('h', d - 1), ('pickup_gap', p), ('delivery_gap', d - 1)):
                    if name in record: _bind(record[name], expected, 'actual original-route insertion ' + name)
        transition = dict(inserted_task_order=order, owner_choices=actual_choices, consumed_quota_slots=consumed,
                          intermediate_scores=intermediate, complete_jobs_before_beam_pruning=len(order))
    elif inserted_task_order or owner_choices or insertions:
        raise ValueError('A claimed dynamic insertion transition needs its actual parent route and coverage')
    result = dict(passed=True, complete=False, complete_solution=False, scoring_scope=PARTIAL_SCOPE,
        routes=[list(r) for r in routes], present_task_ids=sorted(present), remaining_task_ids=missing,
        score=_physical(reference['score']), event_times_min=reference['event_times_min'],
        delivery_times_min=reference['delivery_times_min'], actual_final_owners={str(t): owners[t] for t in sorted(present)},
        actual_partial_final_counts=counts, remaining_quota_slots=q, pinned_task_ids=sorted(pinned),
        transition=transition, core_reference=reference)
    if telemetry is not None:
        require(isinstance(telemetry, dict) and telemetry.get('complete') is False
                and telemetry.get('scoring_scope') == PARTIAL_SCOPE, 'A partial state cannot claim a complete objective')
        for field in ('routes', 'present_task_ids', 'remaining_quota_slots', 'score', 'event_times_min', 'delivery_times_min'):
            require(field in telemetry, 'Missing partial evidence: ' + field); _bind(telemetry[field], result[field], 'partial ' + field)
        require(_ids(telemetry.get('remaining_task_ids'), tasks, 'Saved remaining tasks') == set(missing), 'Saved missing task set differs')
        if 'actual_final_owners' in telemetry: _bind(telemetry['actual_final_owners'], result['actual_final_owners'], 'partial actual owners')
        if transition is not None:
            for field in ('inserted_task_order', 'owner_choices'):
                if field in telemetry: _bind(telemetry[field], transition[field], 'partial actual ' + field)
            if insertions is not None and 'insertions' in telemetry: _bind(telemetry['insertions'], insertions, 'partial actual insertion records')
        _weighted(telemetry.get('key'), result['score'], weights, scales, 'actual same-coverage weighted key')
    return result


def _raw_proof(tasks, topology, base_routes, raw_routes, seed_move, relay_enabled):
    if seed_move is not None:
        return split.audit_split_feeder_proposal(tasks, topology, base_routes, raw_routes, seed_move, relay_enabled=relay_enabled)
    require(not relay_enabled and [list(r) for r in raw_routes] == [list(r) for r in base_routes],
            'Only the genuine original DIRECT checkpoint can omit its seed move')
    reference = reference_schedule(tasks, base_routes, topology)
    require(not reference['handoffs'], 'Original strong DIRECT input contains relay plans')
    return dict(passed=True, score=_physical(reference['score']), core_reference=reference,
                final_tasks_per_aircraft=_counts(reference), handoffs=[])


def _classifications(tasks, base, current, seed_move, focus):
    old = {t for t in tasks if base[str(t)] - tasks[t][2] > EPSILON}
    new = {t for t in tasks if current[str(t)] - tasks[t][2] > EPSILON}
    donor = {seed_move['family'][n] for n in ('k1', 'k2')} - set(focus) if seed_move else set()
    groups = dict(pooled=set(focus), donor=donor, native=set(tasks) - set(focus) - donor)
    return {name + '_' + suffix + '_task_ids': sorted(ids & changed)
            for name, ids in groups.items() for suffix, changed in (('rescued', old - new), ('newly_late', new - old))}


def audit_repair(tasks, topology, base_routes, raw_routes, post_routes, seed_move=None, *, relay_enabled,
                 telemetry=None, weights=None, scales=None, deadline=None, comparison_routes=None, focus_task_ids=()):
    """Prove true raw provenance, then free-native/full-quota current physics."""
    raw = _raw_proof(tasks, topology, base_routes, raw_routes, seed_move, relay_enabled)
    registry = _geometry(tasks, topology)[2]
    focus = _ids(focus_task_ids, tasks, 'Repair focus')
    pinned = {seed_move['family'][n] for n in ('i', 'j')} if relay_enabled else set()
    if not focus: focus = set(pinned)
    current = reference_schedule(tasks, post_routes, topology)
    availability, shared, handoff, prefixes, segments, receiver = _pin_shape(
        tasks, topology, raw_routes, post_routes, current, pinned, relay_enabled, seed_move)
    require(_counts(current) == raw['final_tasks_per_aircraft'], 'Complete repair changed an actual aircraft final quota')
    before_owners, owners = _owners(raw_routes, registry), _owners(post_routes, registry)
    changed = sorted(t for t in tasks if before_owners[t] != owners[t])
    sequences = [a for a in range(len(post_routes)) if tuple(raw_routes[a]) != tuple(post_routes[a])]
    base = reference_schedule(tasks, base_routes, topology)
    if not relay_enabled and len(focus) == 2:
        i, j = sorted(focus)
        if owners[i] == owners[j]:
            a = owners[i]; shared, segments, _ = _shared_flight(tasks, *_geometry(tasks, topology)[:3],
                post_routes[a], a, (i, j), current['event_times_min'][a]); shared = float(shared)
    score = _physical(current['score'])
    result = dict(passed=True, complete=True, complete_solution=True, pin_scope=PIN_SCOPE,
        raw_audit=raw, score=score, post_global_score=score, pinned_task_ids=sorted(pinned), focus_task_ids=sorted(focus),
        actual_final_owners={str(t): owners[t] for t in sorted(tasks)}, final_tasks_per_aircraft=_counts(current),
        changed_owner_task_ids=changed, changed_event_sequence_aircraft=sequences,
        current_source_drop_availability_min=availability, current_shared_loaded_distance_km=float(shared),
        current_handoff=handoff, current_source_prefix_records=prefixes, current_shared_loaded_segments=segments,
        current_outbound_record=receiver, event_times_min=current['event_times_min'], delivery_times_min=current['delivery_times_min'],
        current_classifications=_classifications(tasks, base['delivery_times_min'], current['delivery_times_min'], seed_move, focus),
        actual_cross_uav_count=current['relay_statistics']['cross_uav_handoff_count'], handoffs=current['handoffs'],
        core_reference=current)
    if telemetry is not None:
        require(isinstance(telemetry, dict) and telemetry.get('valid') is True and telemetry.get('complete') is True,
                'A repair checkpoint must be completely verified')
        require(telemetry.get('pin_scope') == PIN_SCOPE, 'Repair pin scope differs')
        _bind(telemetry.get('original_seed_move'), seed_move, 'true original seed move')
        _bind(telemetry.get('routes'), post_routes, 'current complete repair routes')
        for field in ('pinned_task_ids', 'focus_task_ids', 'actual_final_owners', 'event_times_min', 'delivery_times_min',
                      'post_global_score', 'current_source_drop_availability_min', 'current_shared_loaded_distance_km',
                      'current_handoff', 'current_classifications', 'changed_owner_task_ids', 'changed_event_sequence_aircraft'):
            require(field in telemetry, 'Missing current repair field: ' + field); _bind(telemetry[field], result[field], 'repair ' + field)
        if 'pinned_shape' in telemetry:
            expected_shape = dict(required_cross_task_ids=sorted(pinned), receiver=handoff['receiver_aircraft'] if handoff else None,
                source_aircraft=sorted(handoff['source_aircraft']) if handoff else [], station_id=handoff['station_id'] if handoff else None,
                take_positions=receiver['take_positions'] if receiver else [], shared_loaded_distance_km=float(shared) if handoff else 0.)
            _bind(telemetry['pinned_shape'], expected_shape, 'actual current pin shape')
        if 'score' in telemetry: _bind(telemetry['score'], score, 'repair full score')
        if 'routes_sha256' in telemetry:
            actual_digest = sha256(json.dumps(post_routes, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            _bind(telemetry['routes_sha256'], actual_digest, 'repair actual route digest')
        for field in ('table_score', 'window', 'source_drop_availability_min', 'shared_loaded_distance_km'):
            require(field not in telemetry, 'Historical raw pricing cannot certify a repaired plan: ' + field)
        for field, expected in (('cross_uav_count', result['actual_cross_uav_count']), ('relay_task_count', len(current['handoffs']))):
            if field in telemetry: _bind(telemetry[field], expected, 'repair ' + field)
        _weighted(telemetry.get('key'), score, weights, scales, 'repair actual weighted key')
        verified = telemetry.get('full_verified_at_perf_counter'); declared = telemetry.get('deadline_perf_counter', deadline)
        require(type(verified) in (float, int) and isfinite(verified) and type(declared) in (float, int)
                and isfinite(declared) and verified < declared, 'Complete checkpoint was not verified strictly before its deadline')
        if deadline is not None: _bind(declared, deadline, 'actual repair deadline')
        if 'accepted_at_perf_counter' in telemetry:
            require(verified <= telemetry['accepted_at_perf_counter'] < declared, 'Adoption missed its verified deadline')
        if 'selected_as_best_at_perf_counter' in telemetry:
            chosen = telemetry['selected_as_best_at_perf_counter']
            require(type(chosen) in (int, float) and isfinite(chosen) and verified <= chosen < declared,
                    'Internal best was selected outside its complete verified deadline')
        if telemetry.get('accepted') is True:
            require(weights is not None and scales is not None, 'Adoption comparison needs the actual weighted objective')
            comparison = reference_schedule(tasks, comparison_routes if comparison_routes is not None else base_routes, topology)
            scalar = lambda s: sum(w * s[f] / scale for w, f, scale in zip(weights, ('late_count', 'total_lateness_min', 'distance_km'), scales))
            require(scalar(score) < scalar(comparison['score']), 'Adopted complete leaf did not strictly improve the original strong objective')
    return result


# Descriptive alias for the batch worker; both names implement the same proof.
audit_critical_prefix_joint_repair_proposal = audit_repair


def audit_complete_state(tasks, topology, routes, *, telemetry=None, weights=None, scales=None,
                         target_final_counts=None, raw_routes=None, seed_move=None, relay_enabled=None,
                         focus_task_ids=(), deadline=None, require_predeadline=False):
    """Generic current full proof for wrapper checkpoints, not raw provenance."""
    reference = reference_schedule(tasks, routes, topology)
    relay = bool(reference['handoffs']) if relay_enabled is None else relay_enabled
    pins = {seed_move['family'][n] for n in ('i', 'j')} if relay and seed_move else set()
    availability, shared, handoff, prefixes, segments, receiver = _pin_shape(
        tasks, topology, raw_routes, routes, reference, pins, relay, seed_move)
    if target_final_counts is not None: _bind(_counts(reference), target_final_counts, 'complete actual quotas')
    owners = _owners(routes, _geometry(tasks, topology)[2]);score = _physical(reference['score'])
    result = dict(passed=True, complete_solution=True, score=score, core_reference=reference,
        actual_final_owners={str(t): owners[t] for t in sorted(tasks)}, final_tasks_per_aircraft=_counts(reference),
        event_times_min=reference['event_times_min'], delivery_times_min=reference['delivery_times_min'],
        source_drop_availability_min=availability, shared_loaded_distance_km=float(shared), handoff=handoff,
        cross_uav_count=reference['relay_statistics']['cross_uav_handoff_count'], relay_task_count=len(reference['handoffs']))
    if telemetry is not None:
        require(telemetry.get('valid') is True and telemetry.get('complete') is True, 'Wrapper full checkpoint lacks complete validity')
        _bind(telemetry.get('routes'), routes, 'wrapper actual full routes')
        for field in ('score', 'event_times_min', 'delivery_times_min', 'actual_final_owners', 'cross_uav_count',
                      'relay_task_count', 'source_drop_availability_min', 'shared_loaded_distance_km', 'handoff'):
            _bind(telemetry.get(field), result[field], 'wrapper actual ' + field)
        _weighted(telemetry.get('key'), score, weights, scales, 'wrapper actual weighted key')
        if 'focus_task_ids' in telemetry: _bind(telemetry['focus_task_ids'], sorted(focus_task_ids), 'wrapper actual focus')
        if raw_routes is not None and 'seed_routes_sha256' in telemetry:
            digest = sha256(json.dumps(raw_routes, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()
            _bind(telemetry['seed_routes_sha256'], digest, 'wrapper true seed route digest')
        if require_predeadline or 'full_verified_at_perf_counter' in telemetry:
            timestamp = telemetry.get('full_verified_at_perf_counter');declared = telemetry.get('deadline_perf_counter', deadline)
            require(type(timestamp) in (int, float) and isfinite(timestamp) and type(declared) in (int, float)
                    and isfinite(declared) and timestamp < declared, 'Wrapper checkpoint missed its full-verification deadline')
            if deadline is not None: _bind(declared, deadline, 'wrapper actual deadline')
    return result


def _ancestors(routes, registry, node, eligible):
    """Strict route predecessors plus real drop-to-take DAG predecessors."""
    drop_nodes = {abs(v): (a, k) for a, r in enumerate(routes) for k, v in enumerate(r) if v < 0}
    incoming = {(leg['task_id'], leg['relay_id']): identifier for identifier, leg in registry.items() if leg['kind'] == 'RELAY_IN'}
    todo = [(node[0], node[1] - 1)] if node[1] else []
    seen = set()
    while todo:
        a, k = todo.pop()
        if (a, k) in seen: continue
        seen.add((a, k));v = routes[a][k]
        if k: todo.append((a, k - 1))
        leg = registry.get(v) if v > 0 else None
        if leg and leg['kind'] == 'RELAY_OUT': todo.append(drop_nodes[incoming[(leg['task_id'], leg['relay_id'])]])
    return sorted({_identity(routes[a][k], registry) for a, k in seen} & eligible)


def _target(tasks, topology, routes, reference, descriptor, eligible):
    """Locate a target by physical identity, never by its old shifted index."""
    homes, stations, registry, count, capacity, maximum, speed = _geometry(tasks, topology)
    kind, t = descriptor.get('type'), descriptor.get('task_id')
    if kind == 'delivery':
        require(type(t) is int and t in tasks, 'Unknown actual delivery target')
        if str(t) not in reference['delivery_times_min']: return None
        locations = [(a, k) for a, r in enumerate(routes) for k, v in enumerate(r) if v < 0 and _identity(v, registry) == t
                     and (abs(v) not in registry or registry[abs(v)]['kind'] == 'RELAY_OUT')]
        a, k = locations[0];clock = reference['delivery_times_min'][str(t)]
        expected_id = 'delivery:' + str(t)
    elif kind == 'in_drop':
        handoffs = [h for h in reference['handoffs'] if h['task_id'] == t]
        require(len(handoffs) == 1, 'IN-drop target lost its actual relay task')
        h = handoffs[0];a = h['inbound_drone_id'] - 1;k = routes[a].index(-h['in_leg']);clock = h['drop_time_min']
        expected_id = 'in_drop:' + str(t)
    elif kind == 'receiver_arrival':
        require(t is None and len(reference['handoffs']) == 2, 'Arrival target needs the two real transfers')
        receivers = {h['outbound_drone_id'] - 1 for h in reference['handoffs']}
        require(len(receivers) == 1, 'Arrival target receiver differs')
        a = next(iter(receivers));k = min(routes[a].index(h['out_leg']) for h in reference['handoffs'])
        previous = homes[a] if not k else _point(tasks, stations, registry, routes[a][k - 1])
        station = stations[reference['handoffs'][0]['relay_id']]
        clock = (0. if not k else reference['event_times_min'][a][k - 1]) + hypot(station[0] - previous[0], station[1] - previous[1]) / speed
        expected_id = 'receiver_arrival'
    else:
        raise ValueError('Target must be an actual IN drop, pre-wait receiver arrival or delivery')
    return dict(target_id=expected_id, type=kind, task_id=t, aircraft=a, position=k, clock_min=clock,
                causal_prefix_task_ids=_ancestors(routes, registry, (a, k), eligible), ranking_only=True)


def _partners(tasks, topology, routes):
    homes, stations, registry, count, capacity, maximum, speed = _geometry(tasks, topology)
    result = {t: {} for t in tasks}
    for a, route in enumerate(routes):
        held = set();previous = homes[a]
        for v in route:
            point = _point(tasks, stations, registry, v);distance = hypot(point[0] - previous[0], point[1] - previous[1])
            if len(held) == 2 and distance > 0.:
                i, j = sorted(_identity(x, registry) for x in held)
                if i == j: continue
                result[i][j] = result[i].get(j, 0.) + distance
                result[j][i] = result[j].get(i, 0.) + distance
            if v > 0: held.add(v)
            else: held.remove(-v)
            previous = point
    return result


def audit_closure(tasks, topology, base_routes, seed_routes, closure_record, *, pinned_task_ids=(),
                   relay_enabled=False, seed_move=None, weights=None, scales=None):
    """Prove each real deletion's signed causal effect and actual freed slots."""
    require(isinstance(closure_record, dict), 'Missing actual causal closure')
    registry = _geometry(tasks, topology)[2];count = topology['drone_count']
    base = reference_schedule(tasks, base_routes, topology);raw = reference_schedule(tasks, seed_routes, topology)
    require(not base['handoffs'], 'Closure requires the genuine pure-DIRECT original input')
    pinned = _ids(pinned_task_ids, tasks, 'Closure pins')
    _pin_shape(tasks, topology, seed_routes, seed_routes, raw, pinned, relay_enabled, seed_move)
    require(_counts(base) == _counts(raw), 'Closure current full counts differ from original quotas')
    eligible = set(tasks) - pinned;owners = _owners(seed_routes, registry);oldowners = _owners(base_routes, registry)
    plans = _plans(seed_routes, registry, pinned)
    expected_signature = dict(plans=[[t, plans[t]['mode'], plans[t]['station_id'], list(plans[t]['leg_ids']), owners[t],
        plans[t]['roles']['pickup'][0] if plans[t]['mode'] == 'RELAY' else None,
        plans[t]['roles']['take'][0] if plans[t]['mode'] == 'RELAY' else None] for t in sorted(pinned)],
        selected_events=[[v for v in r if _identity(v, registry) in pinned] for r in seed_routes],
        final_counts=_counts(raw), relay_task_ids=sorted(h['task_id'] for h in raw['handoffs']))
    _bind(closure_record.get('expected_signature'), expected_signature, 'closure genuine pinned plan signature')
    removed = closure_record.get('removed_task_ids');selected = _ids(removed, tasks, 'Closure removals')
    require(selected <= eligible, 'Closure removed a pinned plan')
    for field, expected in (('pinned_task_ids', sorted(pinned)), ('eligible_task_ids', sorted(eligible)),
                            ('eligible_native_count', len(eligible)), ('omitted_task_ids', sorted(eligible - selected)),
                            ('eligible_omitted_count', len(eligible - selected))):
        _bind(closure_record.get(field), expected, 'closure ' + field)
    partial = [[v for v in r if _identity(v, registry) not in selected] for r in seed_routes]
    _bind(closure_record.get('partial_routes'), partial, 'closure genuine whole-task removal')
    q = [sum(owners[t] == a for t in selected) for a in range(count)]
    pin_counts = [sum(owners[t] == a for t in pinned) for a in range(count)]
    native_counts = [sum(owners[t] == a for t in eligible - selected) for a in range(count)]
    for field, expected in (('raw_final_counts', _counts(raw)), ('pin_final_counts', pin_counts),
                            ('remaining_native_final_counts', native_counts), ('quota_slots_by_aircraft', q), ('remaining_quota_slots', q)):
        _bind(closure_record.get(field), expected, 'closure ' + field)
    require([r + n + slot for r, n, slot in zip(pin_counts, native_counts, q)] == _counts(raw), 'Actual q=K-r-n identity failed')
    opened = [a for a, slot in enumerate(q) if slot];eligible_owners = {owners[t] for t in eligible}
    for field, expected in (('actual_opened_aircraft', opened), ('all_aircraft_opened', len(opened) == count),
                            ('all_eligible_aircraft_opened', set(opened) == eligible_owners)):
        _bind(closure_record.get(field), expected, 'closure ' + field)
    reasons = closure_record.get('unopened_aircraft_reasons')
    require(isinstance(reasons, dict) and set(reasons) == {str(a) for a in range(count) if a not in opened}
            and all(isinstance(x, str) and x for x in reasons.values()), 'Unopened aircraft need truthful explicit reasons')
    cap = closure_record.get('closure_limit');require(type(cap) is int and cap >= len(selected), 'Closure exceeded its real cap')
    effective = min(cap, len(eligible));_bind(closure_record.get('effective_closure_limit'), effective, 'closure effective cap')
    require(len(opened) == min(effective, len(eligible_owners)), 'The closure did not reserve real available aircraft donor slots first')
    _bind(closure_record.get('truncated'), len(selected) < len(eligible), 'closure omission declaration')
    require(closure_record.get('complete') is False and closure_record.get('complete_solution') is False
            and closure_record.get('global_objective_bound') is False, 'Causal previews cannot claim complete feasibility or a global objective bound')
    _bind(closure_record.get('scoring_scope'), 'causal_single_whole_removal_ranking_and_real_quota_slots_only', 'closure ranking-only scope')
    target_records = closure_record.get('targets');require(isinstance(target_records, list), 'Missing actual target directory')
    targets = {};prefix = set()
    for saved in target_records:
        require(isinstance(saved, dict), 'Invalid target record')
        actual = _target(tasks, topology, seed_routes, raw, saved, eligible)
        require(actual is not None and actual['target_id'] not in targets, 'Undefined or duplicate seed target')
        if not relay_enabled: require(actual['type'] == 'delivery', 'DIRECT cannot invent handoff or station-arrival targets')
        for field, expected in actual.items(): _bind(saved.get(field), expected, 'actual causal target ' + field)
        if 'deadline_min' in saved: _bind(saved['deadline_min'], tasks[actual['task_id']][2], 'target actual deadline')
        targets[actual['target_id']] = actual;prefix.update(actual['causal_prefix_task_ids'])
    if relay_enabled:
        require({'receiver_arrival', *('in_drop:' + str(t) for t in pinned), *('delivery:' + str(t) for t in pinned)} <= set(targets),
                'The closure must retain separate source and selected-delivery target directions')
    focus = _ids(closure_record.get('focus_task_ids'), tasks, 'Closure focus')
    critical_limit = closure_record.get('critical_native_limit')
    require(type(critical_limit) is int and 0 <= critical_limit <= 8, 'Invalid declared critical delivery-target limit')
    chosen = focus | pinned
    critical = sorted(eligible - chosen, key=lambda t: (abs(raw['delivery_times_min'][str(t)] - tasks[t][2]), tasks[t][2], t))[:critical_limit]
    require({target['task_id'] for target in targets.values() if target['type'] == 'delivery'} == chosen | set(critical),
            'Actual focus/pin/critical-delivery target directory is incomplete or substituted')
    _bind(closure_record.get('prefix_candidate_task_ids'), sorted(prefix), 'actual causal-prefix candidate union')
    rows = closure_record.get('per_task_evidence');require(isinstance(rows, list), 'Missing single-whole previews')
    require(len(rows) == len(eligible) and {r.get('task_id') for r in rows} == eligible, 'Single-whole previews must cover the declared eligible task set')
    rawpartners, basepartners = _partners(tasks, topology, seed_routes), _partners(tasks, topology, base_routes)
    verified_rows = {};zero_damage_causal = []
    for row in rows:
        task = row['task_id'];rs = [[v for v in r if _identity(v, registry) != task] for r in seed_routes]
        present = set(tasks) - {task};preview = _subset(tasks, topology, rs, present)
        after = {};delta = {};undefined = []
        for name, target in targets.items():
            value = _target(tasks, topology, rs, preview, target, eligible - {task})
            after[name] = value['clock_min'] if value is not None else None
            delta[name] = target['clock_min'] - after[name] if value is not None else None
            if value is None: undefined.append(name)
        oldclock, clock = base['delivery_times_min'][str(task)], raw['delivery_times_min'][str(task)];due = tasks[task][2]
        oldlate = oldclock - due if oldclock - due > EPSILON else 0.
        late = clock - due if clock - due > EPSILON else 0.
        causal = [name for name, target in targets.items() if task in target['causal_prefix_task_ids']]
        gains = [max(0., value) for value in delta.values() if value is not None]
        expected = dict(base_aircraft=oldowners[task], raw_aircraft=owners[task], base_delivery_min=oldclock,
            raw_delivery_min=clock, base_lateness_min=oldlate, raw_lateness_min=late, delta_lateness_min=late - oldlate,
            positive_delta_lateness_min=max(0., late - oldlate), newly_late=oldlate == 0. and late > 0.,
            signed_delivery_delay_min=clock - oldclock, positive_delivery_delay_min=max(0., clock - oldclock),
            causal_target_ids=causal, target_clock_after_min=after, signed_improvement_min=delta, undefined_targets=undefined,
            maximum_positive_target_gain_min=max(gains, default=0.), total_positive_target_gain_min=sum(gains),
            preview_present_task_ids=sorted(present), preview_final_counts=_counts(preview),
            preview_remaining_quota_slots=[n - k for n, k in zip(_counts(raw), _counts(preview))], preview_complete=False,
            removed=task in selected, repair_order_index=removed.index(task) if task in selected else None)
        for field, value in expected.items(): _bind(row.get(field), value, 'causal removal ' + str(task) + ' ' + field)
        for name, partners in (('raw_co_loaded_partners', rawpartners), ('base_co_loaded_partners', basepartners)):
            if name in row: _bind(row[name], [dict(task_id=t, distance_km=d) for t, d in sorted(partners[task].items())], 'actual ' + name)
        if causal and abs(clock - oldclock) <= 1e-9 and gains and max(gains) > 1e-9: zero_damage_causal.append(task)
        verified_rows[task] = expected
    donors = closure_record.get('donor_reservations');require(isinstance(donors, list), 'Missing donor reservations')
    seen = set()
    for donor in donors:
        a, t = donor.get('aircraft'), donor.get('task_id')
        require(type(a) is int and a in opened and a not in seen and t in selected and owners[t] == a, 'Donor did not release its real aircraft quota')
        seen.add(a);ranking = donor.get('ranked_task_ids');index = donor.get('rank_index')
        require(_ids(ranking, tasks, 'Donor ranking') == {t for t in eligible if owners[t] == a}, 'Donor ranking must contain its actual eligible owner tasks')
        require(type(index) is int and 0 <= index < len(ranking) and ranking[index] == t, 'Donor rank index does not select its real removal')
    require(seen == set(opened), 'Every declared open aircraft must have its real donor reservation')
    events = closure_record.get('selection_events');require(isinstance(events, list) and len(events) == len(selected), 'Closure selection events do not cover actual removals')
    require({e.get('task_id') for e in events} == selected, 'Closure selection events differ from actual removals')
    for index, event in enumerate(events):
        require(event.get('selection_index') == index and event.get('raw_aircraft') == owners[event['task_id']]
                and isinstance(event.get('reason'), str) and event['reason'], 'Closure selection evidence has a false owner/index/reason')
        if event['reason'].startswith('causal_prefix_target:'):
            target_id = event['reason'][len('causal_prefix_target:'):]
            require(target_id in targets and event['task_id'] in targets[target_id]['causal_prefix_task_ids'],
                    'A claimed causal-prefix selection is outside its real DAG ancestors')
    require([e['task_id'] for e in events[:len(donors)]] == [d['task_id'] for d in donors],
            'Real donor reservations must precede optional closure expansion')
    directions = closure_record.get('prefix_direction_representatives')
    require(isinstance(directions, list) and len(directions) == len(targets)
            and {d.get('target_id') for d in directions} == set(targets), 'Separate causal target directions were lost')
    for direction in directions:
        name = direction['target_id'];ranking = direction.get('ranked_task_ids')
        defined_candidates = {t for t in targets[name]['causal_prefix_task_ids'] if verified_rows[t]['signed_improvement_min'][name] is not None}
        require(_ids(ranking, tasks, 'Causal direction ranking') == defined_candidates,
                'A causal direction omits or invents real prefix candidates')
        representative = ranking[0] if ranking else None
        _bind(direction.get('representative_task_id'), representative, 'actual causal direction representative')
        _bind(direction.get('representative_retained'), representative in selected, 'actual retained causal direction')
        _bind(direction.get('positive_single_removal_exists'), any(verified_rows[t]['signed_improvement_min'][name] > 0. for t in ranking),
              'actual signed causal direction opportunity')
    partial_proof = audit_partial_state(tasks, topology, partial, sorted(set(tasks) - selected), target_final_counts=_counts(raw),
        raw_routes=seed_routes, pinned_task_ids=sorted(pinned), relay_enabled=relay_enabled, seed_move=seed_move)
    saved_preview = closure_record.get('partial_preview')
    require(isinstance(saved_preview, dict) and saved_preview.get('complete') is False and saved_preview.get('complete_solution') is False,
            'Closure subset preview must explicitly remain incomplete')
    for field in ('routes', 'present_task_ids', 'remaining_quota_slots', 'score', 'event_times_min', 'delivery_times_min'):
        _bind(saved_preview.get(field), partial_proof[field], 'closure actual partial ' + field)
    for field, expected in (('final_counts', partial_proof['actual_partial_final_counts']),
                            ('final_owners', partial_proof['actual_final_owners']), ('full_task_coverage', not selected),
                            ('standard_full_validator_used', False)):
        _bind(saved_preview.get(field), expected, 'closure actual preview ' + field)
    require(_ids(saved_preview.get('remaining_task_ids'), tasks, 'Closure preview missing tasks') == selected,
            'Closure preview missing coverage differs from its actual whole removals')
    _bind(saved_preview.get('scoring_scope'), 'physical_DAG_preview_on_exact_present_task_set_only', 'closure exact-subset preview scope')
    return dict(passed=True, complete_solution=False, scoring_scope='causal_single_whole_removal_ranking_and_real_quota_slots_only',
        removed_task_ids=list(removed), quota_slots_by_aircraft=q, actual_opened_aircraft=opened,
        final_tasks_per_aircraft=_counts(raw), targets=list(targets.values()), per_task_evidence=verified_rows,
        zero_delivery_damage_causal_task_ids=sorted(zero_damage_causal), partial_proof=partial_proof,
        bound_scope='actual signed deletion effects and real slots only; not a complete score or global feasibility bound')
