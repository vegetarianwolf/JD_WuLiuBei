"""Independent full-coverage conditional-donor exchange proofs.

Search, proposal evaluators and pricing primitives are not imported.  The
frozen035 auditor proves genuine raw provenance/current physical schedules;
this module independently proves each complete two-whole-task transition.
"""
from __future__ import annotations

from hashlib import sha256
import json
from math import hypot, isfinite

try:
    from . import audit_critical_prefix_joint_repair_v1 as physical
    from .audit_deadline_window_repooling import _bind
    from .audit_split_feeder import _weighted
    from .audit_repooling import _identity, _owners, _direct_task
    from .audit_weighted_relay_ablation import _geometry, reference_schedule
    from .audit_weighted_direct_pair import require
except ImportError:
    import audit_critical_prefix_joint_repair_v1 as physical
    from audit_deadline_window_repooling import _bind
    from audit_split_feeder import _weighted
    from audit_repooling import _identity, _owners, _direct_task
    from audit_weighted_relay_ablation import _geometry, reference_schedule
    from audit_weighted_direct_pair import require

PIN_SCOPE = 'conditional_donor_completion_repair'
PROXY_SCOPE = 'contiguous_PD_geometry_and_no_wait_path_proposal_only_not_joint_score_or_bound'


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _full(tasks, topology, base_routes, original_raw_routes, routes, seed_move, relay_enabled, focus):
    return physical.audit_repair(tasks, topology, base_routes, original_raw_routes, routes, seed_move,
        relay_enabled=relay_enabled, focus_task_ids=focus)


def _key(score, weights, scales):
    values = [score[f] for f in ('late_count', 'total_lateness_min', 'distance_km')]
    result = [sum(w * value / scale for w, value, scale in zip(weights, values, scales)), *values]
    _weighted(result, score, weights, scales, 'independent conditional-donor objective')
    return result


def _clock_and_deadline(saved, deadline):
    verified = saved.get('full_verified_at_perf_counter')
    declared = saved.get('deadline_perf_counter', deadline)
    require(type(verified) in (int, float) and isfinite(verified) and type(declared) in (int, float)
            and isfinite(declared) and verified < declared, 'Conditional full checkpoint missed its absolute verification deadline')
    if deadline is not None: _bind(declared, deadline, 'actual conditional repair deadline')
    for field in ('selected_as_best_at_perf_counter', 'accepted_at_perf_counter'):
        if field in saved:
            value = saved[field]
            require(type(value) in (int, float) and isfinite(value) and verified <= value < declared,
                    'Conditional checkpoint selection missed its verified deadline')


def _telemetry(saved, tasks, topology, routes, original_raw_routes, seed_move, proof,
               focus, weights, scales, deadline, parent_routes=None):
    require(isinstance(saved, dict) and saved.get('valid') is True and saved.get('complete') is True,
            'Conditional repair checkpoint must be a complete valid physical plan')
    _bind(saved.get('routes'), routes, 'conditional checkpoint actual routes')
    _bind(saved.get('pin_scope'), PIN_SCOPE, 'conditional actual pin scope')
    _bind(saved.get('original_seed_move'), seed_move, 'conditional genuine raw move')
    for field in ('pinned_task_ids', 'focus_task_ids', 'actual_final_owners', 'event_times_min', 'delivery_times_min',
                  'post_global_score', 'current_source_drop_availability_min', 'current_shared_loaded_distance_km',
                  'current_handoff', 'current_classifications', 'changed_owner_task_ids', 'changed_event_sequence_aircraft'):
        require(field in saved, 'Missing current complete conditional evidence: ' + field)
        _bind(saved[field], proof[field], 'conditional current ' + field)
    _bind(saved.get('score'), proof['score'], 'conditional full actual score')
    _weighted(saved.get('key'), proof['score'], weights, scales, 'conditional full weighted key')
    for field, expected in (('cross_uav_count', proof['actual_cross_uav_count']), ('relay_task_count', len(proof['handoffs']))):
        _bind(saved.get(field), expected, 'conditional current ' + field)
    _bind(saved.get('routes_sha256'), _digest(routes), 'conditional actual route hash')
    _bind(saved.get('present_task_ids'), sorted(tasks), 'conditional complete coverage')
    _bind(saved.get('remaining_quota_slots'), [0] * len(routes), 'conditional complete paid quotas')
    if 'raw_routes_sha256' in saved: _bind(saved['raw_routes_sha256'], _digest(original_raw_routes), 'conditional true raw hash')
    if parent_routes is not None and 'parent_routes_sha256' in saved:
        _bind(saved['parent_routes_sha256'], _digest(parent_routes), 'conditional actual complete parent hash')
    if 'pinned_shape' in saved:
        handoff = proof['current_handoff'];takes = proof['current_outbound_record']
        shape = dict(required_cross_task_ids=proof['pinned_task_ids'], receiver=handoff['receiver_aircraft'] if handoff else None,
            source_aircraft=sorted(handoff['source_aircraft']) if handoff else [], station_id=handoff['station_id'] if handoff else None,
            take_positions=takes['take_positions'] if takes else [], shared_loaded_distance_km=proof['current_shared_loaded_distance_km'] if handoff else 0.)
        _bind(saved['pinned_shape'], shape, 'conditional actual pin shape')
    for field in ('table_score', 'window', 'source_drop_availability_min', 'shared_loaded_distance_km'):
        require(field not in saved, 'Old raw prices/clocks cannot certify a current conditional step: ' + field)
    _clock_and_deadline(saved, deadline)


def audit_step(tasks, topology, base_routes, original_raw_routes, parent_routes, child_routes, seed_move=None, *,
                relay_enabled, step_record, telemetry=None, focus_task_ids=(), weights=None, scales=None, deadline=None):
    """Prove a state-conditioned complete t→b,d→a exchange and both insertions."""
    require(isinstance(step_record, dict), 'A conditional exchange needs its actual pair/transition descriptor')
    parent = _full(tasks, topology, base_routes, original_raw_routes, parent_routes, seed_move, relay_enabled, focus_task_ids)
    child = _full(tasks, topology, base_routes, original_raw_routes, child_routes, seed_move, relay_enabled, focus_task_ids)
    registry = _geometry(tasks, topology)[2];owners = _owners(parent_routes, registry);newowners = _owners(child_routes, registry)
    for field in ('t', 'd'):
        require(type(step_record.get(field)) is int and step_record[field] in tasks, 'Unknown conditional whole task: ' + field)
    t, d = step_record['t'], step_record['d'];a, b = owners[t], owners[d]
    require(t != d and a != b, 'Conditional donor and target need distinct current owners')
    _bind(step_record.get('a'), a, 'conditional target actual current owner')
    _bind(step_record.get('b'), b, 'conditional donor actual current owner/target recipient')
    require({t, d}.isdisjoint(parent['pinned_task_ids'])
            and _direct_task(parent_routes, registry, t) and _direct_task(parent_routes, registry, d),
            'Conditional exchange may remove only ordinary complete DIRECT tasks')
    require(newowners[t] == b and newowners[d] == a, 'Conditional exchange did not close the declared responsibility swap')
    selected = {t, d};strip = lambda rs: [[v for v in r if _identity(v, registry) not in selected] for r in rs]
    removed = strip(parent_routes)
    _bind(strip(child_routes), removed, 'conditional step must preserve every unselected signed event and owner')
    q = [int(owner in (a, b)) for owner in range(len(parent_routes))]
    target_counts = parent['final_tasks_per_aircraft']
    reduced = physical.audit_partial_state(tasks, topology, removed, sorted(set(tasks) - selected),
        target_final_counts=target_counts, raw_routes=original_raw_routes, pinned_task_ids=parent['pinned_task_ids'],
        relay_enabled=relay_enabled, seed_move=seed_move)
    _bind(reduced['remaining_quota_slots'], q, 'actual two released final-responsibility slots')
    for field in ('partial_routes', 'removed_routes'):
        if field in step_record: _bind(step_record[field], removed, 'conditional true ' + field)
    for field in ('quota_slots_by_aircraft', 'remaining_quota_slots', 'partial_quota_slots'):
        if field in step_record: _bind(step_record[field], q, 'conditional true freed ' + field)
    order = step_record.get('inserted_task_order')
    require(isinstance(order, (list, tuple)) and len(order) == 2 and set(order) == selected,
            'Conditional step must restore both whole jobs before candidate comparison')
    require('owner_choices' in step_record and 'insertions' in step_record, 'Missing true two-job dynamic insertion trace')
    transition = physical.audit_partial_state(tasks, topology, child_routes, sorted(tasks), target_final_counts=target_counts,
        raw_routes=original_raw_routes, pinned_task_ids=parent['pinned_task_ids'], relay_enabled=relay_enabled, seed_move=seed_move,
        parent_routes=removed, parent_present_task_ids=sorted(set(tasks) - selected), inserted_task_order=order,
        owner_choices=step_record['owner_choices'], insertions=step_record['insertions'])
    require(not any(transition['remaining_quota_slots']), 'A claimed complete conditional step still owes responsibility slots')
    for field, expected in (('parent_routes_sha256', _digest(parent_routes)), ('routes_sha256', _digest(child_routes))):
        if field in step_record: _bind(step_record[field], expected, 'conditional physical ' + field)
    result = {**child, 'pin_scope': PIN_SCOPE, 'parent_score': parent['score'], 't': t, 'd': d, 'a': a, 'b': b,
              'parent_core_reference': parent['core_reference'], 'partial_removal_proof': reduced, 'transition_proof': transition,
              'quota_slots_by_aircraft': q, 'joint_full_coverage': True, 'selected_task_ids': sorted(selected),
              'parent_routes_sha256': _digest(parent_routes), 'routes_sha256': _digest(child_routes)}
    if weights is not None or scales is not None:
        result['parent_key'] = _key(parent['score'], weights, scales)
        result['key'] = _key(child['score'], weights, scales)
    if telemetry is not None:
        _telemetry(telemetry, tasks, topology, child_routes, original_raw_routes, seed_move, result,
                   focus_task_ids, weights, scales, deadline, parent_routes)
        for field in ('inserted_task_order', 'owner_choices', 'insertions'):
            if field in telemetry: _bind(telemetry[field], step_record[field], 'conditional actual ' + field)
        _bind(telemetry.get('partial_quota_slots'), q, 'conditional paid pair slots')
    return result


def audit_checkpoint(tasks, topology, base_routes, original_raw_routes, routes, seed_move=None, *, relay_enabled,
                      telemetry=None, focus_task_ids=(), weights=None, scales=None, deadline=None):
    """Current complete checkpoint; its parent edge is proved separately."""
    result = _full(tasks, topology, base_routes, original_raw_routes, routes, seed_move, relay_enabled, focus_task_ids)
    result = {**result, 'pin_scope': PIN_SCOPE}
    if telemetry is not None:
        _telemetry(telemetry, tasks, topology, routes, original_raw_routes, seed_move, result,
                   focus_task_ids, weights, scales, deadline)
    return result


audit_conditional_donor_repair_proposal = audit_checkpoint


def _surface(tasks, topology, route, aircraft, task, pins):
    """Independently enumerate only the declared geometric proposal surface."""
    homes, stations, registry, count, capacity, maximum, speed = _geometry(tasks, topology)
    point = lambda v: physical._point(tasks, stations, registry, v)
    P, D = tasks[task][:2]
    distance = lambda x, y: hypot(x[0] - y[0], x[1] - y[1])
    positions = [k for k, v in enumerate(route) if v > 0 and abs(v) in registry
                 and registry[v]['kind'] == 'RELAY_OUT' and registry[v]['task_id'] in pins]
    forbidden = {positions[0] + 1} if len(positions) == 2 and positions[1] == positions[0] + 1 else set()
    held = set();previous = homes[aircraft];prefix_distance = 0.;rows = []
    for gap in range(len(route) + 1):
        if len(held) < capacity and gap not in forbidden:
            detour = distance(previous, P) + distance(P, D)
            if gap < len(route): detour += distance(D, point(route[gap])) - distance(previous, point(route[gap]))
            rows.append(dict(gap=gap, distance_increment_km=float(detour),
                no_wait_delivery_proxy_min=(prefix_distance / speed + distance(previous, P) / speed) + distance(P, D) / speed))
        if gap < len(route):
            v = route[gap];next_point = point(v);prefix_distance += distance(previous, next_point);previous = next_point
            if v > 0: held.add(v)
            else:
                require(-v in held, 'Proxy skeleton must preserve actual pickup-before-delivery cargo')
                held.remove(-v)
            require(len(held) <= capacity, 'Proxy skeleton exceeds actual cargo capacity')
    require(not held and rows, 'A proxy skeleton must close cargo and retain an actual legal contiguous gap')
    geometry = min(rows, key=lambda r: (r['distance_increment_km'], r['no_wait_delivery_proxy_min'], r['gap']))
    early = min(rows, key=lambda r: (r['no_wait_delivery_proxy_min'], r['distance_increment_km'], r['gap']))
    return dict(aircraft=aircraft, inserted_task_id=task, route_after_removal_sha256=_digest(route),
        legal_contiguous_gaps=len(rows), positions_retained=len({geometry['gap'], early['gap']}),
        skeleton_distance_km=float(prefix_distance), geometric=geometry, earliest_delivery=early,
        forbidden_pin_take_internal_gaps=sorted(forbidden), scoring_scope=PROXY_SCOPE)


def _critical_rank(row):
    return (-row['maximum_positive_target_gain_min'], -row['total_positive_target_gain_min'],
            -int(row['newly_late']), -row['positive_delta_lateness_min'], -row['positive_delivery_delay_min'], row['task_id'])


def audit_catalogue(tasks, topology, base_routes, parent_routes, catalogue, *, pinned_task_ids=(),
                     seed_move=None, relay_enabled=False, focus_task_ids=(), weights, scales,
                     original_raw_routes=None):
    """Prove actual directions and every declared finite conditioned proxy."""
    require(isinstance(catalogue, dict) and catalogue.get('completed') is True, 'Only a completed finite catalogue may be published')
    require(catalogue.get('complete') is False and catalogue.get('complete_solution') is False
            and catalogue.get('global_objective_bound') is False, 'A conditional proposal catalogue is not a completed objective or bound')
    source = catalogue.get('source_preview')
    proof = physical.audit_closure(tasks, topology, base_routes, parent_routes, source,
        pinned_task_ids=pinned_task_ids, relay_enabled=relay_enabled, seed_move=seed_move, weights=weights, scales=scales)
    require(source['closure_limit'] == 0 and source['removed_task_ids'] == [], 'Critical evidence must not secretly fix a removal-U constraint')
    _bind(catalogue.get('source_preview_scope'), 'all_eligible_actual_single_whole_deletion_effects_not_a_removal_U_constraint', 'actual source-preview scope')
    if original_raw_routes is not None:
        _full(tasks, topology, base_routes, original_raw_routes, parent_routes, seed_move, relay_enabled, focus_task_ids)
    pins = set(pinned_task_ids);rows = {t: dict(row, task_id=t) for t, row in proof['per_task_evidence'].items()}
    ranking = sorted(rows, key=lambda t: _critical_rank(rows[t]))
    config = catalogue.get('configuration');require(isinstance(config, dict), 'Missing conditional catalogue bounds')
    critical_limit, donor_limit = config.get('critical_limit'), config.get('donor_limit')
    require(type(critical_limit) is int and 0 <= critical_limit <= 6 and type(donor_limit) is int and 0 <= donor_limit <= 3,
            'Conditional catalogue bounds differ from its declared finite policy')
    _bind(config.get('weights'), list(weights), 'catalogue actual weights');_bind(config.get('scales'), list(scales), 'catalogue actual scales')
    _bind(config.get('destination_policy'), 'every_other_actual_aircraft', 'conditioned destination coverage')
    _bind(config.get('source_surface'), 'all_capacity_legal_contiguous_PD_gaps_excluding_internal_double_take_gap', 'declared proxy surface')
    _bind(config.get('surface_cache_limit'), 4096, 'declared bounded surface cache')
    for field, expected in (('partner_depth', 1), ('critical_native_limit', 2)):
        _bind(config.get(field), expected, 'declared actual critical-preview policy ' + field)
        _bind(source.get(field), expected, 'actual source critical-preview policy ' + field)
    channels_order = ['pair_geometry', 'critical_early_no_wait', 'donor_return_early_no_wait']
    _bind(config.get('donor_channels'), channels_order, 'conditional proxy channels')
    _bind(config.get('critical_policy'), 'defined_causal_target_direction_heads_then_actual_gain_damage_delay_ID_fill', 'actual causal-direction policy')
    critical = [];directions = []
    for target in source['targets']:
        name = target['target_id']
        ordered = sorted((t for t in target['causal_prefix_task_ids'] if rows[t]['signed_improvement_min'][name] is not None),
                         key=lambda t: (-rows[t]['signed_improvement_min'][name], _critical_rank(rows[t])))
        representative = ordered[0] if ordered else None
        if representative is not None and representative not in critical and len(critical) < critical_limit: critical.append(representative)
        retained = representative in critical
        directions.append(dict(target_id=name, ranked_task_ids=ordered, representative_task_id=representative,
            signed_gain_min=None if representative is None else rows[representative]['signed_improvement_min'][name],
            representative_retained=retained, omitted_reason=None if retained else ('no_defined_causal_candidate' if representative is None else 'critical_limit')))
    for t in ranking:
        if t not in critical and len(critical) < critical_limit: critical.append(t)
    _bind(catalogue.get('critical_direction_representatives'), directions, 'actual defined causal-direction heads')
    parent_hash = _digest(parent_routes)
    for field, expected in (('critical_task_ids', critical), ('critical_task_ranking', ranking),
        ('critical_omitted_task_ids', sorted(set(rows) - set(critical))), ('parent_routes_sha256', parent_hash),
        ('pinned_task_ids', sorted(pins)), ('focus_task_ids', list(focus_task_ids)),
        ('eligible_native_count', len(rows)), ('expected_signature', source['expected_signature'])):
        _bind(catalogue.get(field), expected, 'conditional actual ' + field)
    actual_rows = {r['task_id']: r for r in source['per_task_evidence']}
    _bind(catalogue.get('critical_evidence'), [actual_rows[t] for t in critical], 'conditional truthful selected deletion evidence')
    registry = _geometry(tasks, topology)[2];owners = _owners(parent_routes, registry);count = len(parent_routes)
    parent_reference = reference_schedule(tasks, parent_routes, topology)
    original_distances = [r['distance_km'] for r in parent_reference['route_records']]
    by_owner = {a: sorted(t for t in rows if owners[t] == a) for a in range(count)}
    directories = catalogue.get('donor_directories');records = catalogue.get('records')
    require(isinstance(directories, list) and isinstance(records, list), 'Missing actual conditional donor directories/records')
    require(len(directories) == len(critical) * (count - 1), 'A selected critical task lost a declared other-aircraft destination')
    expected_records = [];logical_surfaces = 0;position_scores = 0;cache = {};hits = misses = 0
    def surface(route, a, t):
        nonlocal hits, misses, position_scores, logical_surfaces
        logical_surfaces += 1;key = (a, tuple(route), t)
        if key in cache: hits += 1;return cache[key]
        misses += 1;value = _surface(tasks, topology, route, a, t, pins);position_scores += value['legal_contiguous_gaps']
        if len(cache) >= 4096: cache.pop(next(iter(cache)))
        cache[key] = value;return value
    index = 0
    for t in critical:
        a = owners[t];route_a = tuple(v for v in parent_routes[a] if abs(v) != t)
        for b in range(count):
            if b == a: continue
            saved = directories[index];require(isinstance(saved, dict), 'Invalid conditional destination directory')
            candidates = []
            for d in by_owner[b]:
                route_b = tuple(v for v in parent_routes[b] if abs(v) != d)
                returned, incoming = surface(route_a, a, d), surface(route_b, b, t)
                constant = returned['skeleton_distance_km'] + incoming['skeleton_distance_km'] - original_distances[a] - original_distances[b]
                net = constant + returned['geometric']['distance_increment_km'] + incoming['geometric']['distance_increment_km']
                candidates.append(dict(donor_task_id=d, donor_return_surface=returned, critical_destination_surface=incoming,
                    geometric_net_distance_proxy_km=float(net), critical_early_delivery_proxy_min=incoming['earliest_delivery']['no_wait_delivery_proxy_min'],
                    donor_return_early_delivery_proxy_min=returned['earliest_delivery']['no_wait_delivery_proxy_min'],
                    scoring_scope=PROXY_SCOPE, full_joint_score=None, complete_solution=False))
            lookup = {r['donor_task_id']: r for r in candidates}
            geometry = sorted(lookup, key=lambda d: (lookup[d]['geometric_net_distance_proxy_km'], lookup[d]['critical_early_delivery_proxy_min'], lookup[d]['donor_return_early_delivery_proxy_min'], d))
            early_t = sorted(lookup, key=lambda d: (lookup[d]['critical_early_delivery_proxy_min'], lookup[d]['geometric_net_distance_proxy_km'], d))
            early_d = sorted(lookup, key=lambda d: (lookup[d]['donor_return_early_delivery_proxy_min'], lookup[d]['geometric_net_distance_proxy_km'], d))
            channels = dict(zip(channels_order, (geometry, early_t, early_d)));kept = [];retained_by = {}
            for name, order in channels.items():
                if order:
                    d = order[0]
                    if d in kept: retained_by[d].append(name)
                    elif len(kept) < donor_limit: kept.append(d);retained_by[d] = [name]
            for d in geometry:
                if d not in kept and len(kept) < donor_limit: kept.append(d);retained_by[d] = ['geometry_fill']
            expected = dict(directory_index=index, t=t, a=a, b=b, parent_routes_sha256=parent_hash,
                eligible_donor_task_ids=by_owner[b], retained_donor_task_ids=kept, omitted_donor_task_ids=sorted(set(lookup) - set(kept)),
                eligible_donor_count=len(lookup), retained_donor_count=len(kept), donor_rankings=channels, per_donor_evidence=candidates,
                coverage_scope='all_current_destination_whole_DIRECT_donors_scored_by_finite_contiguous_proxy_only',
                omitted_scope='not_retained_by_declared_proxy_channels_not_proved_infeasible_or_dominated')
            _bind(saved, expected, 'independently rebuilt conditional donor directory')
            for d in kept:
                q = [int(owner in (a, b)) for owner in range(count)]
                entry = dict(record_index=len(expected_records), donor_directory_index=index, t=t, a=a, b=b, donor_task_id=d,
                    task_ids=sorted((t, d)), parent_routes_sha256=parent_hash, target_owner_by_task={str(t): b, str(d): a},
                    removal_quota_slots_by_aircraft=q, removed_task_ids=[t, d], retained_by_channels=retained_by[d],
                    critical_rank_index=ranking.index(t), critical_target_ids=actual_rows[t]['causal_target_ids'],
                    critical_target_clock_improvements_min=actual_rows[t]['signed_improvement_min'],
                    geometric_net_distance_proxy_km=lookup[d]['geometric_net_distance_proxy_km'],
                    full_joint_score=None, complete=False, complete_solution=False, global_objective_bound=False)
                entry['proposal_sha256'] = _digest({k: entry[k] for k in ('parent_routes_sha256', 't', 'a', 'b', 'donor_task_id', 'target_owner_by_task')})
                expected_records.append(entry)
            index += 1
    _bind(records, expected_records, 'truthful bounded conditional proposals')
    stats = catalogue.get('statistics');require(isinstance(stats, dict) and stats.get('completed') is True, 'Missing completed catalogue counters')
    if 'completed_at_perf_counter' in catalogue:
        completed, declared = catalogue['completed_at_perf_counter'], catalogue.get('deadline_perf_counter')
        require(type(completed) in (int, float) and isfinite(completed) and type(declared) in (int, float)
                and isfinite(declared) and completed < declared, 'Conditional catalogue was not published before its absolute deadline')
    counters = dict(proxy_positions_scored=position_scores, proxy_surface_cache_hits=hits, proxy_surface_cache_misses=misses,
        critical_retained=len(critical), destination_directories=len(directories), conditional_records=len(records),
        conditional_donors_scored=sum(len(d['eligible_donor_task_ids']) for d in directories),
        conditional_donors_omitted=sum(len(d['omitted_donor_task_ids']) for d in directories))
    for field, expected in counters.items(): _bind(stats.get(field), expected, 'actual conditional catalogue counter ' + field)
    return dict(passed=True, complete_solution=False, catalogue_sha256=_digest(catalogue), records_sha256=_digest(records),
        critical_task_ids=critical, conditional_records=len(records), destination_directories=len(directories),
        conditional_donors_scored=counters['conditional_donors_scored'], conditional_donors_omitted=counters['conditional_donors_omitted'],
        logical_surface_requests=logical_surfaces, exact_geometric_proxy_positions=position_scores,
        source_preview_proof_sha256=_digest(proof), scoring_scope=PROXY_SCOPE,
        all_declared_donor_proxies_replayed=True, all_full_exchange_positions_replayed=False)


def audit_metadata(tasks, topology, base_routes, original_raw_routes, metadata, *, seed_move=None,
                    focus_task_ids=(), relay_enabled, weights, scales, deadline):
    """Replay saved complete states/edges and honest unfinished-step evidence.

    Every declared donor proxy is rebuilt.  Only stored complete search states
    have independent entity replays; counters for discarded full probes remain
    production telemetry, with accounting constraints rather than invented
    route proofs. The returned evidence contains hashes/counts, not route copies.
    """
    require(isinstance(metadata, dict), 'Missing actual conditional helper metadata')
    _bind(metadata.get('pin_scope'), PIN_SCOPE, 'conditional helper scope')
    _bind(metadata.get('relay_enabled'), relay_enabled, 'conditional helper arm')
    _bind(metadata.get('original_seed_move'), seed_move, 'conditional helper raw provenance')
    _bind(metadata.get('focus_task_ids'), list(focus_task_ids), 'conditional helper actual focus')
    _bind(metadata.get('deadline_perf_counter'), deadline, 'conditional helper actual deadline')
    raw_proof = _full(tasks, topology, base_routes, original_raw_routes, original_raw_routes, seed_move, relay_enabled, focus_task_ids)
    pins = raw_proof['pinned_task_ids'];_bind(metadata.get('pinned_task_ids'), pins, 'conditional actual relay identities')
    config = metadata.get('configuration');require(isinstance(config, dict), 'Missing declared conditional repair bounds')
    for field, maximum in (('critical_limit', 6), ('donor_limit', 3), ('position_limit', 256), ('option_count', 3),
                           ('beam_width', 16), ('max_steps', 8), ('snapshot_limit', 64)):
        require(type(config.get(field)) is int and 1 <= config[field] <= maximum, 'Invalid conditional bound ' + field)
    require(config['option_count'] <= config['position_limit'], 'Conditional position frontier exceeds its true surface cap')
    for field, expected in (('task_layer_size', 2), ('whole_task_exchange', 't:a_to_b,donor:b_to_a'),
        ('first_partial_J_pruning', False), ('state_comparison', 'complete_same_all_task_coverage'),
        ('position_scores_are_bounds', False), ('all_PD_positions_globally_scored', False),
        ('each_complete_proposal_standard_full_checked', True), ('ordinary_DIRECT_owner_and_order_free', True), ('new_relay_generation', False)):
        _bind(config.get(field), expected, 'conditional declared policy ' + field)
    if not metadata.get('initialization_completed'):
        require(metadata.get('initialization_completed') is False and metadata.get('deadline_reached') is True,
                'An unpublished initialization requires its honest deadline failure')
        require(all(metadata.get(field) is None for field in ('input_snapshot', 'post_repair_snapshot', 'first_complete_candidate',
                'best_constructed_candidate', 'best_key', 'final_routes_sha256')), 'Expired initialization fabricated a complete checkpoint')
        require(metadata.get('steps') == [] and metadata.get('full_candidate_snapshots') == []
                and metadata.get('accepted_improvements') == 0 and metadata.get('returned_input_guard') is True
                and metadata.get('returned_checkpoint_checked_within_deadline') is False,
                'Expired initialization cannot adopt or return a newly checked state')
        return dict(passed=True, initialization_completed=False, predeadline_checkpoint_verified=False,
            saved_complete_state_references=[], catalogue_references=[], saved_complete_states_replayed=0,
            complete_transition_edges_replayed=0, all_complete_probes_entity_replayed=False)
    initial = metadata.get('input_snapshot');require(isinstance(initial, dict), 'Completed initialization lost its complete input checkpoint')
    _bind(initial.get('routes'), original_raw_routes, 'genuine original raw helper checkpoint')
    require(initial.get('state_id') == 0 and initial.get('parent_state_id') is None and initial.get('exchange') is None
            and initial.get('inserted_task_order') == [] and initial.get('owner_choices') == [] and initial.get('insertions') == []
            and initial.get('partial_quota_slots') is None and initial.get('parent_routes_sha256') is None,
            'The input checkpoint cannot claim an invented conditional parent edge')
    input_proof = audit_checkpoint(tasks, topology, base_routes, original_raw_routes, initial['routes'], seed_move,
        relay_enabled=relay_enabled, telemetry=initial, focus_task_ids=focus_task_ids, weights=weights, scales=scales, deadline=deadline)
    states = {0: initial};state_refs = {};edge_refs = {};catalogue_refs = [];all_checkpoints = []
    def reference(snapshot, parent=None, directory=None, parent_record=None):
        require(isinstance(snapshot, dict) and type(snapshot.get('state_id')) is int and snapshot['state_id'] >= 0,
                'A saved complete beam state needs its actual integer state ID')
        sid = snapshot['state_id'];digest = _digest(snapshot)
        if sid in states: _bind(snapshot, states[sid], 'one actual complete state ID cannot identify different entities')
        if digest in state_refs: return state_refs[digest]
        if parent is None:
            require(sid == 0, 'A new complete state lost its actual complete parent')
            proof = input_proof
        else:
            require(snapshot.get('parent_state_id') == parent['state_id'] and sid > parent['state_id'], 'Conditional child parent ID is not its actual earlier state')
            exchange = snapshot.get('exchange');require(isinstance(exchange, dict), 'A noninput complete state lost its actual conditional pair')
            record_index = exchange.get('catalogue_record_index')
            require(type(record_index) is int and 0 <= record_index < len(directory['records']), 'Complete state references an undeclared conditioned donor')
            chosen = directory['records'][record_index]
            for field, expected in (('t', chosen['t']), ('d', chosen['donor_task_id']), ('a', chosen['a']), ('b', chosen['b'])):
                _bind(exchange.get(field), expected, 'complete state actual catalogue pair ' + field)
            matching = [p for p in parent_record['pairs'] if p['catalogue_record_index'] == record_index]
            require(len(matching) == 1, 'A saved complete state did not actually start its declared pair probe')
            step_record = {**exchange, **{field: snapshot[field] for field in ('inserted_task_order', 'owner_choices', 'insertions', 'partial_quota_slots', 'parent_routes_sha256')}}
            proof = audit_step(tasks, topology, base_routes, original_raw_routes, parent['routes'], snapshot['routes'], seed_move,
                relay_enabled=relay_enabled, step_record=step_record, telemetry=snapshot, focus_task_ids=focus_task_ids,
                weights=weights, scales=scales, deadline=deadline)
            edge_refs[sid] = dict(state_id=sid, parent_state_id=parent['state_id'], t=proof['t'], d=proof['d'],
                from_aircraft=proof['a'], conditioned_recipient=proof['b'], proof_sha256=_digest(proof),
                routes_sha256=proof['routes_sha256'], actual_freed_quota_slots=proof['quota_slots_by_aircraft'])
        value = dict(state_id=sid, snapshot_sha256=digest, routes_sha256=_digest(snapshot['routes']), score=proof['score'],
            full_clock_sha256=_digest(dict(event_times_min=proof['event_times_min'], delivery_times_min=proof['delivery_times_min'])),
            full_verified_at_perf_counter=snapshot['full_verified_at_perf_counter'])
        state_refs[digest] = value;states[sid] = snapshot
        return value
    reference(initial)
    previous = [initial];steps = metadata.get('steps');require(isinstance(steps, list) and len(steps) <= config['max_steps'], 'Conditional step count exceeds its actual cap')
    generated = verified_saved = completed_steps = 0
    for number, step in enumerate(steps):
        require(isinstance(step, dict) and step.get('step_index') == number, 'Conditional complete-step index differs')
        _bind(step.get('parent_state_ids'), [s['state_id'] for s in previous], 'actual previous complete beam parents')
        parents = step.get('parent_records');retained = step.get('retained_states');checkpoints = step.get('complete_checkpoints')
        require(isinstance(parents, list) and isinstance(retained, list) and isinstance(checkpoints, list), 'Missing actual step evidence collections')
        require(len(parents) <= len(previous) and len(retained) <= config['beam_width'], 'Actual parent/beam coverage exceeds its cap')
        contexts = {};step_generated = 0
        for parent_index, record in enumerate(parents):
            parent = previous[parent_index]
            _bind(record.get('parent_state_id'), parent['state_id'], 'conditional actual parent prefix')
            directory = record.get('catalogue')
            if directory is None:
                require(metadata.get('deadline_reached') is True and record.get('completed') is False
                        and not record.get('pairs') and parent_index == len(parents) - 1 and number == len(steps) - 1,
                        'An unfinished unpublished catalogue must be the honest terminal deadline prefix')
                continue
            cp = audit_catalogue(tasks, topology, base_routes, parent['routes'], directory, pinned_task_ids=pins,
                seed_move=seed_move, relay_enabled=relay_enabled, focus_task_ids=focus_task_ids,
                weights=weights, scales=scales, original_raw_routes=original_raw_routes)
            _bind(record.get('catalogue_statistics'), directory['statistics'], 'actual completed catalogue telemetry alias')
            _bind(directory['configuration']['critical_limit'], config['critical_limit'], 'actual critical catalogue cap')
            _bind(directory['configuration']['donor_limit'], config['donor_limit'], 'actual conditional donor cap')
            catalogue_refs.append(dict(step_index=number, parent_state_id=parent['state_id'], **cp))
            contexts[parent['state_id']] = (parent, directory, record)
            pairs = record.get('pairs');require(isinstance(pairs, list) and len(pairs) <= len(directory['records']), 'Probe prefix exceeds its actual donor catalogue')
            for pair_index, pair in enumerate(pairs):
                proposal = directory['records'][pair_index]
                for field, expected in (('catalogue_record_index', pair_index), ('t', proposal['t']), ('d', proposal['donor_task_id']), ('a', proposal['a']), ('b', proposal['b']),
                                        ('partial_quota_slots', proposal['removal_quota_slots_by_aircraft'])):
                    _bind(pair.get(field), expected, 'actual conditioned pair-probe prefix ' + field)
                removed = [[v for v in r if _identity(v, _geometry(tasks, topology)[2]) not in {pair['t'], pair['d']}] for r in parent['routes']]
                _bind(pair.get('partial_routes_sha256'), _digest(removed), 'actual pair removal hash')
                orders = [[pair['t'], pair['d']], [pair['d'], pair['t']]]
                finished_orders = pair.get('insertion_orders_completed')
                require(isinstance(finished_orders, list) and finished_orders == orders[:len(finished_orders)] and len(finished_orders) <= 2,
                        'Pair insertion-order completion is not the actual two-order prefix')
                if pair.get('completed') is True: require(finished_orders == orders, 'Completed pair did not finish both whole-job insertion orders')
                else: require(metadata.get('deadline_reached') is True and pair_index == len(pairs) - 1 and parent_index == len(parents) - 1
                              and number == len(steps) - 1, 'An incomplete pair must be the terminal deadline prefix')
                count = pair.get('candidate_count')
                require(type(count) is int and 0 <= count <= 2 * config['option_count'] ** 2, 'Impossible complete two-task proposal count')
                step_generated += count
            if record.get('completed') is True:
                require(len(pairs) == len(directory['records']) and all(p['completed'] for p in pairs), 'Completed parent omitted conditioned pairs')
            else:
                require(metadata.get('deadline_reached') is True and parent_index == len(parents) - 1 and number == len(steps) - 1,
                        'An unfinished parent must be an honest terminal deadline prefix')
        _bind(step.get('candidate_count'), step_generated, 'actual full-coverage proposal accounting');generated += step_generated
        for snapshot in checkpoints:
            require(snapshot.get('parent_state_id') in contexts, 'Complete checkpoint lost its actual catalogue parent')
            parent, directory, record = contexts[snapshot['parent_state_id']]
            reference(snapshot, parent, directory, record);all_checkpoints.append(snapshot)
        for snapshot in retained:
            require(snapshot.get('parent_state_id') in contexts, 'Retained complete beam lost its actual catalogue parent')
            parent, directory, record = contexts[snapshot['parent_state_id']]
            reference(snapshot, parent, directory, record)
        require(len({_digest(s['routes']) for s in retained}) == len(retained), 'A complete beam duplicated route states')
        require([(tuple(s['key']), tuple(map(tuple, s['routes']))) for s in retained]
                == sorted((tuple(s['key']), tuple(map(tuple, s['routes']))) for s in retained), 'The retained complete beam is not sorted by its actual full key')
        if step.get('completed') is True:
            require(len(parents) == len(previous) and all(p['completed'] for p in parents), 'Completed step lost actual parent/candidate coverage')
            completed_steps += 1
        else:
            require(metadata.get('deadline_reached') is True and number == len(steps) - 1, 'An unfinished complete beam must be a terminal deadline prefix')
        previous = retained
    _bind(metadata.get('complete_proposals_generated'), generated, 'actual generated full-coverage proposal count')
    _bind(metadata.get('completed_steps'), completed_steps, 'actual completed search-step count')
    full_verified = metadata.get('complete_leafs_full_verified');rejected = metadata.get('full_validator_rejections')
    require(type(full_verified) is int and type(rejected) is int and 0 <= full_verified <= generated and 0 <= rejected <= generated - full_verified,
            'Full-validator telemetry exceeds actual complete proposal coverage')
    require(len(edge_refs) <= full_verified, 'More stored complete entities than actual full verifications')
    if 'complete_full_validator_calls' in metadata:
        calls = metadata['complete_full_validator_calls']
        require(type(calls) is int and full_verified + rejected <= calls <= generated, 'Actual full-call accounting is inconsistent')
    if not metadata.get('deadline_reached'):
        require(full_verified + rejected == generated, 'Finished complete probes lack standard full-check accounting')
    expected_saved = all_checkpoints[:config['snapshot_limit']]
    _bind(metadata.get('full_candidate_snapshots'), expected_saved, 'actual bounded chronological checkpoint snapshots')
    _bind(metadata.get('first_complete_candidate'), all_checkpoints[0] if all_checkpoints else None, 'actual first complete candidate')
    best = initial;improvements = 0;last_best = None
    for snapshot in all_checkpoints:
        if tuple(snapshot['key']) < tuple(best['key']):
            require('selected_as_best_at_perf_counter' in snapshot, 'A selected internal best lost its deadline evidence')
            best = snapshot;last_best = snapshot;improvements += 1
        else:
            require('selected_as_best_at_perf_counter' not in snapshot, 'A nonimproving complete checkpoint falsely claims best selection')
    _bind(metadata.get('accepted_improvements'), improvements, 'actual internal full best-chain count')
    _bind(metadata.get('best_constructed_candidate'), last_best, 'actual best constructed candidate source')
    _bind(metadata.get('post_repair_snapshot'), best, 'actual returned predeadline complete best')
    _bind(metadata.get('best_key'), best['key'], 'actual returned full best key')
    _bind(metadata.get('final_routes_sha256'), _digest(best['routes']), 'actual returned full route hash')
    _bind(metadata.get('returned_input_guard'), improvements == 0, 'actual input-guard return flag')
    require(metadata.get('returned_checkpoint_checked_within_deadline') is True, 'A published initialized return must identify its checked complete checkpoint')
    return dict(passed=True, initialization_completed=True, predeadline_checkpoint_verified=True,
        saved_complete_state_references=list(state_refs.values()), complete_transition_edges=list(edge_refs.values()),
        catalogue_references=catalogue_refs, saved_complete_states_replayed=len(state_refs),
        complete_transition_edges_replayed=len(edge_refs), stored_checkpoints_replayed=len(all_checkpoints),
        generated_complete_proposals=generated, production_reported_full_verified=full_verified,
        production_reported_full_rejections=rejected, actual_internal_best_improvements=improvements,
        returned_score=best['score'], returned_routes_sha256=_digest(best['routes']),
        all_declared_donor_proxies_replayed=True, all_complete_probes_entity_replayed=False)
