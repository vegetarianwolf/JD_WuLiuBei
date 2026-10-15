"""Necessary arm-metadata proof for paid multi-group native refinement.

The existing research ledger owns manifest/receipt/phase/CF checks and writes
one batch receipt. This module adds no competing artifact or whole-batch loop.
"""
from __future__ import annotations
from math import isfinite
try:
    from . import audit_multi_relay_batch_v1 as frozen
    from . import audit_multi_relay_accumulation_v1 as raw_audit
    from . import audit_multi_relay_native_polish_v1 as polish_audit
    from . import audit_multi_relay_direct_control_v1 as direct_audit
except ImportError:
    import audit_multi_relay_batch_v1 as frozen
    import audit_multi_relay_accumulation_v1 as raw_audit
    import audit_multi_relay_native_polish_v1 as polish_audit
    import audit_multi_relay_direct_control_v1 as direct_audit

_bind, _key, _compact = frozen._bind, frozen._key, frozen._compact
_wall_evidence = frozen._wall_evidence
reference_schedule = frozen.reference_schedule
_digest = raw_audit._digest
ARMS = ('multi_native_direct', 'multi_native_relay')
SCOPE = 'paid_multi_relay_native_refinement'


def _expected_seeds(raw, maximum):
    pool = {}
    def add(snapshot, path):
        if snapshot is None or not snapshot['groups']: return
        routes = tuple(map(tuple, snapshot['routes']))
        pool.setdefault(routes, dict(raw_snapshot=snapshot, source_path=path,
            raw_snapshot_sha256=_digest(snapshot), routes_sha256=snapshot['routes_sha256'],
            group_count=len(snapshot['groups']), groups=snapshot['groups']))
    for name in ('first_complete_candidate', 'best_constructed_candidate', 'post_repair_snapshot'):
        add(raw.get(name), [name])
    for k, snapshot in enumerate(raw['full_candidate_snapshots']): add(snapshot, ['full_candidate_snapshots', k])
    for number, step in enumerate(raw['steps']):
        for name in ('complete_checkpoints', 'retained_states'):
            for k, snapshot in enumerate(step[name]): add(snapshot, ['steps', number, name, k])
    selected = []
    for count in range(1, maximum + 1):
        candidates = [(routes, record) for routes, record in pool.items() if record['group_count'] == count]
        if candidates:
            _, record = min(candidates, key=lambda item: (tuple(item[1]['raw_snapshot']['key']), item[0]))
            selected.append(dict(channel='minimum_J_saved_complete_groups_' + str(count), **record))
    return selected, len(pool)


def _reference_summary(reference, routes):
    return dict(**_compact(reference), routes_sha256=_digest(routes))


def _audit_metadata(tasks, topology, base_routes, metadata, *, relay, manifest, phase=None):
    """Paid raw -> true complete channels -> native post -> original guard."""
    assert type(relay) is bool
    arm = ARMS[int(relay)];params = manifest['method_parameters'][arm]
    weights, scales = manifest['weights'], manifest['scales'];deadline = metadata['deadline_perf_counter']
    original = reference_schedule(tasks, base_routes, topology)
    assert not original['handoffs']
    if not relay:
        assert set(params) == frozen.DIRECT_PARAMETERS
        _bind(manifest['periodic_parameters'], frozen.PERIODIC, 'independent DIRECT periodic policy')
        proof = direct_audit.audit_metadata(tasks, topology, base_routes, metadata,
            weights=weights, scales=scales, deadline=deadline, parameters=params)
        routes = metadata['final_snapshot']['routes']
        final = reference_schedule(tasks, routes, topology)
        return dict(passed=True, arm=arm, independent_method_evidence=proof,
            paid_wall_evidence=_wall_evidence(metadata, phase, manifest) if phase else None,
            final_reference=_reference_summary(final, routes), final_routes_sha256=_digest(routes),
            no_shared_raw_directory=True, checkpoint_mechanism_diagnostic_only=True,
            strongest_fresh_comparison=False)
    config = metadata['configuration']
    assert metadata['method'] == 'Paid-Multi-Relay-Construction-And-Native-Polish-V1'
    _bind(metadata['pin_scope'], SCOPE, 'paid native wrapper scope');_bind(metadata['relay_enabled'], True, 'paid native arm')
    raw_names = frozen.RELAY_PARAMETERS
    profile_names = {'polish_max_iterations': 'max_iterations', 'polish_initial_temperature': 'initial_temperature',
        'polish_minimum_temperature': 'minimum_temperature', 'polish_cooling_rate': 'cooling_rate'}
    assert set(params) == raw_names | {'raw_fraction'} | set(profile_names)
    _bind(config['raw_fraction'], params['raw_fraction'], 'paid raw wall fraction')
    for name in raw_names: _bind(config['raw_parameters'][name], params[name], 'paid raw parameter ' + name)
    for name, field in profile_names.items(): _bind(config['polish_alns_profile'][field], params[name], 'actual native ALNS profile ' + name)
    for field, expected in (('raw_method', 'multi_relay_accumulation_v1'), ('wrapper_verification_reserve_seconds', .15),
        ('seed_policy', 'minimum_J_saved_complete_each_actual_group_count_then_route_deduplicate'),
        ('maximum_seed_channels', params['max_groups']), ('allow_complete_checkpoint_from_unfinished_raw_depth', True),
        ('no_free_historical_relay_seed', True), ('raw_native_owners_and_order_fixed', True),
        ('polished_native_owner_and_order_free', True), ('polished_selected_plans_and_subsequences_fixed', True),
        ('helper_return_requires_cached_predeadline_complete_checkpoint', True),
        ('final_guard', 'original_pure_DIRECT_complete_input_strict_weighted'), ('strongest_fresh_end_to_end_comparison', False)):
        _bind(config[field], expected, 'paid native policy ' + field)
    if not metadata['initialization_completed']:
        assert metadata['deadline_reached'] and metadata['input_snapshot'] is metadata['final_snapshot'] is None
        assert metadata['raw_metadata'] is None and metadata['seed_records'] == metadata['polish_records'] == metadata['full_candidate_snapshots'] == metadata['accepted_moves'] == []
        assert not metadata['returned_checkpoint_checked_within_deadline'] and metadata['accepted_improvements'] == 0
        return dict(passed=True, arm=arm, initialization_completed=False, final_reference=_reference_summary(original, base_routes),
            final_routes_sha256=_digest(base_routes), paid_wall_evidence=_wall_evidence(metadata, phase, manifest) if phase else None,
            no_new_predeadline_checkpoint=True, strongest_fresh_comparison=False)
    initial = metadata['input_snapshot']
    raw_audit.audit_state(tasks, topology, base_routes, base_routes, [], telemetry=initial, weights=weights, scales=scales, deadline=deadline)
    _bind(metadata['initial_key'], _key(original['score'], manifest), 'original strong DIRECT key')
    raw = metadata['raw_metadata'];rawproof = None;selected = []
    if raw is not None:
        started = metadata['started_at_perf_counter'];returned = metadata['raw_stage_returned_at_perf_counter']
        raw_deadline = started + (deadline - started) * params['raw_fraction']
        _bind(metadata['raw_deadline_perf_counter'], raw_deadline, 'paid construction deadline')
        _bind(raw['deadline_perf_counter'], raw_deadline, 'actual paid raw deadline')
        assert started <= raw['started_at_perf_counter'] <= returned <= started + metadata['runtime_seconds']
        assert raw['runtime_seconds'] <= returned - raw['started_at_perf_counter'] + 1e-8
        _bind(raw['seed'], metadata['seed'], 'paid raw seed')
        for name in raw_names: _bind(raw['configuration'][name], params[name], 'actual raw construction bound ' + name)
        _bind(manifest['accumulation_fixed_catalogue_parameters'], frozen.FIXED_CATALOGUE, 'fixed actual construction catalogue')
        for step in raw['steps']:
            for parent in step['parent_records']:
                if parent['catalogue'] is not None:
                    for name, value in frozen.FIXED_CATALOGUE.items():
                        _bind(parent['catalogue']['metadata']['configuration'][name], value, 'actual fixed raw catalogue ' + name)
        rawproof = raw_audit.audit_metadata(tasks, topology, base_routes, raw, weights=weights, scales=scales, deadline=raw_deadline)
        output = raw['post_repair_snapshot']['routes'] if raw['initialization_completed'] else base_routes
        _bind(metadata['raw_output_routes'], output, 'actual paid construction return')
        selected, pool = _expected_seeds(raw, params['max_groups'])
        if metadata['seed_selection_completed']:
            _bind(metadata['seed_records'], [{k: v for k, v in s.items() if k != 'raw_snapshot'} for s in selected], 'all true saved group-count channels')
            _bind(metadata['saved_raw_seed_pool_size'], pool, 'actual complete saved raw pool')
        else:
            assert metadata['deadline_reached'] and metadata['seed_records'] == metadata['polish_records'] == metadata['full_candidate_snapshots'] == []
            selected = []
    else:
        assert metadata['deadline_reached'] and not metadata['seed_selection_completed']
        assert metadata['seed_records'] == metadata['polish_records'] == metadata['full_candidate_snapshots'] == []
    entries = metadata['polish_records'];assert len(entries) <= len(selected)
    postproofs = []
    for number, entry in enumerate(entries):
        seed = selected[number];macro = seed['raw_snapshot']['routes']
        for field, value in (('seed_index', number), ('channel', seed['channel']), ('group_count', seed['group_count']),
            ('groups', seed['groups']), ('source_seed_routes_sha256', seed['routes_sha256']), ('remaining_channels', len(selected) - number)):
            _bind(entry[field], value, 'actual native leaf source ' + field)
        allocation = entry['allocation_started_at_perf_counter']
        assert metadata['started_at_perf_counter'] <= allocation < deadline
        leaf_deadline = allocation + max(0., deadline - .15 - allocation) / (len(selected) - number)
        _bind(entry['deadline_perf_counter'], leaf_deadline, 'actual remaining-channel wall division')
        assert leaf_deadline <= deadline
        if entry['returned']:
            helper = entry['metadata']
            assert allocation <= helper['started_at_perf_counter'] <= entry['returned_at_perf_counter']
            assert entry['returned_at_perf_counter'] <= metadata['started_at_perf_counter'] + metadata['runtime_seconds']
            assert helper['runtime_seconds'] <= entry['returned_at_perf_counter'] - helper['started_at_perf_counter'] + 1e-8
            _bind(helper['seed'], metadata['seed'] + number, 'actual native leaf seed')
            _bind(helper['snapshot_limit'], params['snapshot_limit'], 'native evidence cap')
            postproofs.append(polish_audit.audit_metadata(tasks, topology, macro, seed['groups'], helper,
                weights=weights, scales=scales, deadline=leaf_deadline))
            if helper['initialization_completed']:
                for field, value in config['polish_alns_profile'].items(): _bind(helper['configuration'][field], value, 'actual paid ALNS profile ' + field)
            if helper['returned_checkpoint_checked_within_deadline']:
                _bind(entry['returned_routes'], helper['post_repair_snapshot']['routes'], 'actual native cached return')
                assert 'post_consideration_skipped' not in entry
            else:
                _bind(entry['returned_routes'], macro, 'expired leaf returns only its input routes')
                assert entry.get('post_consideration_skipped') == 'no_helper_predeadline_complete_checkpoint' or metadata['deadline_reached']
        else:
            assert metadata['deadline_reached'] and entry['metadata'] is None and entry['returned_routes'] is None
    refs = [];expected_phases = []
    for number, entry in enumerate(entries):
        expected_phases.append('raw_seed_' + str(number))
        if entry['returned'] and entry['metadata']['returned_checkpoint_checked_within_deadline']:
            expected_phases.append('polished_seed_' + str(number))
    # A deadline may stop after a raw consideration or after a checked leaf
    # returns; it cannot reorder or fabricate any complete source consideration.
    snapshots = metadata['full_candidate_snapshots'];names = [s['phase'] for s in snapshots]
    allowed = expected_phases + (['raw_seed_' + str(len(entries))] if len(entries) < len(selected) else [])
    assert names == allowed[:len(names)]
    if not metadata['deadline_reached']: assert names == expected_phases and len(entries) == len(selected)
    best = initial;accepted_expected = []
    for snapshot in snapshots:
        name = snapshot['phase'];number = int(name.rsplit('_', 1)[1]);seed = selected[number]
        macro = seed['raw_snapshot']['routes']
        proof = polish_audit.audit_complete_state(tasks, topology, macro, seed['groups'], snapshot['routes'],
            telemetry=snapshot, weights=weights, scales=scales, deadline=deadline)
        if name.startswith('raw_seed_'):
            _bind(snapshot['routes'], macro, 'actual complete raw consideration')
            assert metadata['raw_stage_returned_at_perf_counter'] <= snapshot['full_verified_at_perf_counter']
        else:
            assert name.startswith('polished_seed_') and number < len(entries)
            entry = entries[number];assert entry['returned'] and entry['metadata']['returned_checkpoint_checked_within_deadline']
            _bind(snapshot['routes'], entry['returned_routes'], 'actual complete polished consideration')
            assert entry['returned_at_perf_counter'] <= snapshot['full_verified_at_perf_counter']
        refs.append(dict(snapshot_sha256=_digest(snapshot), source_seed_routes_sha256=seed['routes_sha256'],
            **_reference_summary(proof['core_reference'], snapshot['routes'])))
        if tuple(snapshot['key']) < tuple(best['key']):
            accepted_expected.append((snapshot, seed));best = snapshot
    assert len(metadata['accepted_moves']) == len(accepted_expected)
    for move, (snapshot, seed) in zip(metadata['accepted_moves'], accepted_expected):
        _bind(move['source'], snapshot['phase'], 'actual original-guard adoption source')
        _bind(move['seed_routes_sha256'], seed['routes_sha256'], 'actual adopted complete seed provenance')
        _bind(move['full_snapshot'], snapshot, 'actual adopted complete consideration')
        _bind(move['full_verified_at_perf_counter'], snapshot['full_verified_at_perf_counter'], 'actual outer full verification')
        assert snapshot['full_verified_at_perf_counter'] <= move['accepted_at_perf_counter'] < deadline
    _bind(metadata['accepted_improvements'], len(accepted_expected), 'actual strict original-guard improvements')
    _bind(metadata['final_snapshot'], best, 'actual final original-guard cache')
    _bind(metadata['final_key'], best['key'], 'actual final original-guard objective')
    _bind(metadata['returned_input_guard'], best is initial, 'actual original DIRECT guard return flag')
    assert metadata['returned_checkpoint_checked_within_deadline'] is True
    final_reference = reference_schedule(tasks, best['routes'], topology)
    return dict(passed=True, arm=arm, initialization_completed=True, raw_method_evidence=rawproof,
        native_leaf_evidence=postproofs, proposal_references=refs,
        paid_wall_evidence=_wall_evidence(metadata, phase, manifest) if phase else None,
        final_reference=_reference_summary(final_reference, best['routes']), final_routes_sha256=_digest(best['routes']),
        no_shared_raw_directory=True, checkpoint_mechanism_diagnostic_only=True,
        strongest_fresh_comparison=False, unrecorded_probes_entity_replayed=False)
