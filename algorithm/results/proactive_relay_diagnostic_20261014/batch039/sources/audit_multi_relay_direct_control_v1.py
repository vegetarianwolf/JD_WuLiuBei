"""Independent saved-entity audit of the paid, relay-free DIRECT control.

The search runtime is never imported.  Frozen standard-library references
reconstruct physical clocks; the separate conditional-donor auditor proves
its stored complete entities and catalogue, not discarded probe telemetry.
"""
from __future__ import annotations

from hashlib import sha256
import json
from math import isfinite

try:
    from . import audit_critical_prefix_joint_repair_v1 as physical
    from . import audit_conditional_donor_metadata_v2 as conditional
    from .audit_deadline_window_repooling import _bind
    from .audit_split_feeder import _weighted
    from .audit_weighted_direct_pair import require
except ImportError:
    import audit_critical_prefix_joint_repair_v1 as physical
    import audit_conditional_donor_metadata_v2 as conditional
    from audit_deadline_window_repooling import _bind
    from audit_split_feeder import _weighted
    from audit_weighted_direct_pair import require


METHOD = 'Independent-Weighted-DIRECT-ALNS-And-Complete-Exchange-V1'
ALNS_PHASE = 'independent_DIRECT_ALNS'
REPAIR_PHASE = 'independent_DIRECT_complete_exchange'
PERIODIC = dict(interval=120, max_candidates=16, anchors_per_route=4, pair_budget_seconds=.008)
REPAIR_BOUNDS = dict(critical_limit=6, donor_limit=3, position_limit=256,
                     option_count=3, beam_width=16, max_steps=8, snapshot_limit=64)


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _number(value, label):
    require(type(value) in (int, float) and isfinite(value), label + ' must be a finite real timestamp/value')
    return value


def _key(score, weights, scales):
    values = [score[k] for k in ('late_count', 'total_lateness_min', 'distance_km')]
    result = [sum(w * v / s for w, v, s in zip(weights, values, scales)), *values]
    _weighted(result, score, weights, scales, 'independent DIRECT actual objective')
    return result


def _reference_summary(proof, routes):
    if proof is None: return None
    return dict(routes_sha256=_digest(routes), score=proof['score'],
        full_replay_output_sha256=_digest(proof),
        full_clock_sha256=_digest(dict(event_times_min=proof['event_times_min'], delivery_times_min=proof['delivery_times_min'])),
        final_tasks_per_aircraft=proof['final_tasks_per_aircraft'])


def _periodic(stats, count):
    require(isinstance(stats, dict), 'Missing actual periodic exchange telemetry')
    for name in ('repair_calls', 'passes', 'probes', 'canonical_checks', 'improvements'):
        require(type(stats.get(name)) is int and stats[name] >= 0, 'Invalid periodic counter ' + name)
    events = stats.get('events')
    require(isinstance(events, list) and len(events) == stats['passes'], 'Periodic pass count differs from its saved events')
    require(stats['passes'] <= stats['repair_calls'] // PERIODIC['interval'], 'Periodic passes exceed actual repair cadence')
    for name in ('probes', 'canonical_checks', 'improvements'):
        require(all(type(e.get(name)) is int and e[name] >= 0 for e in events), 'Invalid periodic event counter ' + name)
        _bind(stats[name], sum(e[name] for e in events), 'Periodic event total ' + name)
    runtime = _number(stats.get('runtime_seconds'), 'Periodic runtime')
    require(runtime >= 0, 'Negative periodic runtime')
    for event in events:
        pair = event.get('route_pair')
        require(isinstance(pair, list) and len(pair) == 2 and all(type(a) is int and 0 <= a < count for a in pair)
                and pair[0] < pair[1], 'Periodic pair must use two actual aircraft')
        call = event.get('repair_call')
        require(type(call) is int and call > 0 and call % PERIODIC['interval'] == 0 and call <= stats['repair_calls'],
                'Periodic event missed its actual repair cadence')
        budget = _number(event.get('budget_seconds'), 'Periodic event wall budget')
        require(0 <= budget <= PERIODIC['pair_budget_seconds'] + 1e-8, 'Periodic event exceeds its independent wall limit')
    return dict(saved_events=len(events), internal_full_route_entities_replayed=False)


def audit_metadata(tasks, topology, base_routes, metadata, *, weights, scales, deadline, parameters=None):
    """Prove original guard → actual weighted ALNS → complete DIRECT exchange.

    Input guard may be checked after entry expiry. Every newly considered or
    adopted state must instead carry a matching pre-deadline full checkpoint.
    All tasks are free DIRECT tasks; no relay catalogue, pins or fake H target
    is accepted. The returned references contain digests, never duplicate fleets.
    """
    require(isinstance(metadata, dict), 'Missing independent DIRECT metadata')
    require(isinstance(parameters, dict) or parameters is None, 'DIRECT parameters must be a mapping')
    _bind(metadata.get('method'), METHOD, 'independent DIRECT method')
    _bind(metadata.get('relay_enabled'), False, 'independent DIRECT arm')
    _bind(metadata.get('deadline_perf_counter'), deadline, 'independent DIRECT wrapper deadline')
    deadline = _number(deadline, 'Wrapper deadline')
    started = _number(metadata.get('started_at_perf_counter'), 'Wrapper start')
    runtime = _number(metadata.get('runtime_seconds'), 'Wrapper runtime')
    require(runtime >= 0 and type(metadata.get('seed')) is int, 'Invalid actual DIRECT runtime/seed')
    require(type(metadata.get('deadline_reached')) is bool, 'DIRECT deadline state must be explicit')
    config = metadata.get('configuration')
    require(isinstance(config, dict), 'Missing actual independent DIRECT configuration')
    fraction = _number(config.get('alns_fraction'), 'Actual ALNS budget fraction')
    require(0 < fraction < 1, 'Invalid independent ALNS budget split')
    for field, expected in (('no_relay_catalogue', True), ('pins_empty', True), ('all_tasks_native_free', True),
        ('final_guard', 'original_complete_input_strict_weighted'), ('strongest_fresh_end_to_end_comparison', False),
        ('wrapper_verification_reserve_seconds', .15)):
        _bind(config.get(field), expected, 'Actual independent DIRECT policy ' + field)
    repair_config = config.get('repair_parameters')
    require(isinstance(repair_config, dict) and set(repair_config) == set(REPAIR_BOUNDS), 'Missing actual conditional repair bounds')
    for name, maximum in REPAIR_BOUNDS.items():
        require(type(repair_config[name]) is int and 1 <= repair_config[name] <= maximum, 'Invalid DIRECT repair bound ' + name)
    require(repair_config['option_count'] <= repair_config['position_limit'], 'Repair frontier exceeds its position cap')
    for name, value in (parameters or {}).items():
        if name in repair_config: _bind(repair_config[name], value, 'Protocol DIRECT repair parameter ' + name)
        elif name == 'alns_fraction': _bind(fraction, value, 'Protocol actual ALNS fraction')
        elif name == 'seed': _bind(metadata['seed'], value, 'Protocol DIRECT seed')
        elif name == 'relay_enabled': _bind(False, value, 'Protocol relay-free DIRECT')
        else: raise ValueError('Unknown independent DIRECT protocol parameter ' + str(name))

    def reference(routes, saved=None, *, focus=(), seed_routes=None, predeadline=False):
        return physical.audit_complete_state(tasks, topology, routes, telemetry=saved, weights=weights, scales=scales,
            relay_enabled=False, raw_routes=seed_routes, focus_task_ids=focus, deadline=deadline if predeadline else None,
            require_predeadline=predeadline)

    initial = metadata.get('input_snapshot')
    require(isinstance(initial, dict), 'DIRECT control lost its genuine original guard')
    _bind(initial.get('phase'), 'original_guard', 'DIRECT input guard phase')
    _bind(initial.get('routes'), base_routes, 'Original strong DIRECT input routes')
    _bind(initial.get('focus_task_ids'), [], 'Input guard has no relay/delivery pins')
    _bind(initial.get('seed_routes_sha256'), None, 'Input guard cannot claim a fabricated raw seed')
    base_proof = reference(base_routes, initial)
    _bind(metadata.get('initial_key'), _key(base_proof['score'], weights, scales), 'Original DIRECT objective')
    alns = metadata.get('alns_record');repair = metadata.get('repair_record')
    alns_proof = periodic_proof = repair_proof = None
    if alns is not None:
        require(isinstance(alns, dict), 'Invalid saved actual ALNS stage')
        sub_deadline = started + (deadline - started) * fraction
        _bind(alns.get('deadline_perf_counter'), sub_deadline, 'Actual ALNS sub-deadline')
        configured = _number(alns.get('configured_at_perf_counter'), 'Actual ALNS configuration time')
        begin = _number(alns.get('started_at_perf_counter'), 'Actual ALNS stage start')
        returned = _number(alns.get('returned_at_perf_counter'), 'Actual ALNS return')
        require(started <= configured <= begin <= returned, 'ALNS stage times are not ordered actual events')
        if configured >= deadline:
            require(metadata['deadline_reached'] is True, 'Late ALNS configuration must report wrapper expiry')
        cfg = alns.get('config')
        require(isinstance(cfg, dict), 'Actual frozen ALNS configuration was not saved')
        for field, expected in (('max_iterations', 10_000_000), ('seed', metadata['seed']), ('relay_enabled', False),
                ('enable_home_seed', True), ('enable_home_bias', True), ('enable_terminal_task_exchange', True), ('enable_lazy_repair', True)):
            _bind(cfg.get(field), expected, 'Actual frozen DIRECT ALNS config ' + field)
        _bind(cfg.get('time_limit_seconds'), max(.001, sub_deadline - configured), 'Actual ALNS configured wall allowance')
        _bind(alns.get('periodic_configuration'), PERIODIC, 'Actual periodic whole-DIRECT exchange policy')
        _bind(alns.get('objective_mode'), 'frozen_weighted_ALNS', 'Actual ALNS scalar objective')
        _bind(alns.get('effective_coefficients'), [w / s for w, s in zip(weights, scales)], 'Actual ALNS weighted coefficients')
        require(type(alns.get('iterations')) is int and 0 <= alns['iterations'] <= cfg['max_iterations'], 'Invalid actual ALNS iteration count')
        alns_proof = reference(alns.get('routes'))
        periodic_proof = _periodic(alns.get('periodic_task_exchange'), len(base_routes))
    else:
        require(metadata['deadline_reached'] is True and repair is None, 'Only expiry can omit the actual independent ALNS stage')

    snapshots = metadata.get('full_candidate_snapshots');accepted = metadata.get('accepted_moves')
    require(isinstance(snapshots, list) and isinstance(accepted, list) and len(snapshots) <= 2,
            'Invalid complete wrapper consideration/adoption collections')
    phases = [s.get('phase') if isinstance(s, dict) else None for s in snapshots]
    require(phases == [ALNS_PHASE, REPAIR_PHASE][:len(phases)], 'DIRECT wrapper consideration must follow its actual two stages')
    if snapshots: require(alns is not None, 'Wrapper considered an ALNS result that was never returned')
    if alns is None: require(not snapshots, 'Expired pre-ALNS input cannot fabricate a new full consideration')
    if repair is not None:
        require(isinstance(repair, dict) and len(snapshots) >= 1, 'Repair must start from the actually considered ALNS result')
        _bind(repair.get('base_routes'), alns['routes'], 'Repair must start from actual ALNS routes, not the strong guard or a relay seed')
        _bind(repair.get('base_routes_sha256'), _digest(alns['routes']), 'Actual ALNS repair-base hash')
        focus = sorted(sorted(tasks, key=lambda t: (-(alns_proof['delivery_times_min'][str(t)] - tasks[t][2]), t))[:2])
        _bind(repair.get('focus_task_ids'), focus, 'Actual ALNS lateness-derived DIRECT delivery targets')
        repair_deadline = deadline - .15
        _bind(repair.get('deadline_perf_counter'), repair_deadline, 'Actual complete-exchange deadline with wrapper reserve')
        require(type(repair.get('returned')) is bool, 'Repair return state must be explicit')
        if repair['returned']:
            _number(repair.get('returned_at_perf_counter'), 'Actual repair return')
            require(repair['returned_at_perf_counter'] >= alns['returned_at_perf_counter'], 'Repair returned before its actual ALNS base existed')
            helper = repair.get('metadata')
            repair_proof = conditional.audit_metadata(tasks, topology, alns['routes'], alns['routes'], helper,
                seed_move=None, focus_task_ids=focus, relay_enabled=False, weights=weights, scales=scales, deadline=repair_deadline)
            _bind(helper.get('seed'), metadata['seed'], 'Actual conditional repair seed')
            for name, value in repair_config.items(): _bind(helper['configuration'].get(name), value, 'Actual conditional helper bound ' + name)
            checkpoint = helper.get('post_repair_snapshot')
            if helper.get('returned_checkpoint_checked_within_deadline') is True:
                require(isinstance(checkpoint, dict), 'A checked DIRECT helper return must contain its actual full checkpoint')
                _bind(repair.get('returned_routes'), checkpoint['routes'], 'Actual complete helper returned routes')
                require('post_consideration_skipped' not in repair, 'Checked helper checkpoint cannot be labeled unpublishable')
            else:
                require(checkpoint is None and len(snapshots) == 1, 'Unchecked/expired helper cannot introduce a new considered plan')
                _bind(repair.get('returned_routes'), alns['routes'], 'Expired helper retains its actual input only')
                if 'post_consideration_skipped' in repair:
                    _bind(repair['post_consideration_skipped'], 'no_helper_predeadline_complete_checkpoint', 'Honest expired-helper skip')
                else: require(metadata['deadline_reached'] is True, 'Unchecked helper return was neither skipped nor stopped by wrapper expiry')
        else:
            require(metadata['deadline_reached'] is True and repair.get('metadata') is None and repair.get('returned_routes') is None
                    and len(snapshots) == 1, 'Unfinished helper fabricated a return/checkpoint')
    elif len(snapshots) == 2:
        raise ValueError('A complete-exchange consideration has no actual helper source')

    if alns is not None and (not snapshots or repair is None):
        require(metadata['deadline_reached'] is True, 'A finished stage cannot silently omit its ordinary next stage')
    if repair is not None and repair.get('returned') is True and repair['metadata'].get('returned_checkpoint_checked_within_deadline') is True:
        if len(snapshots) != 2: require(metadata['deadline_reached'] is True, 'A checked helper return was silently dropped before consideration')

    best = initial;best_proof = base_proof;candidate_refs = [];expected_accepted = []
    for number, saved in enumerate(snapshots):
        phase = phases[number]
        actual_routes = alns['routes'] if number == 0 else repair['returned_routes']
        focus = [] if number == 0 else repair['focus_task_ids']
        seed_routes = None if number == 0 else alns['routes']
        _bind(saved.get('routes'), actual_routes, 'Wrapper consideration actual stage return')
        _bind(saved.get('focus_task_ids'), focus, 'Actual wrapper focus scope')
        _bind(saved.get('seed_routes_sha256'), None if number == 0 else _digest(seed_routes), 'Actual wrapper source provenance')
        proof = reference(actual_routes, saved, focus=focus, seed_routes=seed_routes, predeadline=True)
        verified = _number(saved.get('full_verified_at_perf_counter'), 'Wrapper full verification')
        returned = alns['returned_at_perf_counter'] if number == 0 else repair['returned_at_perf_counter']
        require(returned <= verified, 'Wrapper full verification predates its actual stage return')
        candidate_refs.append(dict(phase=phase, snapshot_sha256=_digest(saved), routes_sha256=_digest(actual_routes),
            score=proof['score'], full_clock_sha256=_digest(dict(event_times_min=proof['event_times_min'], delivery_times_min=proof['delivery_times_min'])),
            full_verified_at_perf_counter=verified))
        if tuple(_key(proof['score'], weights, scales)) < tuple(_key(best_proof['score'], weights, scales)):
            expected_accepted.append((saved, proof));best, best_proof = saved, proof
    require(len(accepted) == len(expected_accepted), 'Wrapper adoption chain omitted or invented a strict full improvement')
    for move, (saved, proof) in zip(accepted, expected_accepted):
        _bind(move.get('source'), saved['phase'], 'Actual strict adoption stage')
        _bind(move.get('full_snapshot'), saved, 'Adoption must bind its exact complete consideration')
        _bind(move.get('full_verified_at_perf_counter'), saved['full_verified_at_perf_counter'], 'Actual adoption verification time')
        at = _number(move.get('accepted_at_perf_counter'), 'Actual strict adoption time')
        require(saved['full_verified_at_perf_counter'] <= at < deadline, 'DIRECT adoption missed its checked absolute deadline')
    _bind(metadata.get('accepted_improvements'), len(expected_accepted), 'Actual strict original-guard improvement count')
    _bind(metadata.get('final_snapshot'), best, 'Actual strict best returned DIRECT entity')
    _bind(metadata.get('final_key'), _key(best_proof['score'], weights, scales), 'Actual complete final DIRECT key')
    return dict(passed=True, metadata_sha256=_digest(metadata), input_reference=_reference_summary(base_proof, base_routes),
        actual_ALNS_reference=_reference_summary(alns_proof, alns['routes']) if alns is not None else None,
        periodic_reference=periodic_proof, repair_metadata_reference=repair_proof,
        wrapper_candidate_references=candidate_refs, wrapper_candidates_replayed=len(candidate_refs),
        final_reference=_reference_summary(best_proof, best['routes']), final_routes_sha256=_digest(best['routes']), final_score=best_proof['score'],
        accepted_improvements=len(expected_accepted), all_saved_helper_complete_entities_replayed=True,
        all_discarded_ALNS_or_exchange_entities_replayed=False)
