"""Independent saved-batch proof for multi-group relay and paid DIRECT control.

The two arms share a historical complete DIRECT checkpoint, physical resources,
weights and total wall ceiling. They do not share a candidate directory or raw
relay stage. This is a checkpoint mechanism comparison, not fresh performance.
"""
from __future__ import annotations

from math import isfinite
from pathlib import Path

try:
    from . import run_proactive_relay_research as ledger
    from . import audit_multi_relay_accumulation_v1 as relay_audit
    from .audit_deadline_window_repooling import _bind
    from .audit_weighted_direct_pair import load_official_csv
    from .audit_weighted_relay_ablation import audit_payload, direct_counterfactual, reference_schedule
except ImportError:
    import run_proactive_relay_research as ledger
    import audit_multi_relay_accumulation_v1 as relay_audit
    from audit_deadline_window_repooling import _bind
    from audit_weighted_direct_pair import load_official_csv
    from audit_weighted_relay_ablation import audit_payload, direct_counterfactual, reference_schedule

ARMS = ('accumulation_direct', 'accumulation_relay')
RELAY_PARAMETERS = {'max_groups', 'beam_width', 'max_families', 'source_option_limit',
                    'receiver_option_limit', 'position_limit', 'snapshot_limit'}
DIRECT_PARAMETERS = {'alns_fraction', 'critical_limit', 'donor_limit', 'position_limit',
                     'option_count', 'beam_width', 'max_steps', 'snapshot_limit'}
FIXED_CATALOGUE = dict(feeder_limit=3, base_pool_limit=64)
PERIODIC = dict(interval=120, max_candidates=16, anchors_per_route=4, pair_budget_seconds=.008)
AUDIT_SOURCES = (
    'run_proactive_relay_research.py',
    'audit_multi_relay_batch_v1.py', 'audit_multi_relay_accumulation_v1.py',
    'audit_multi_relay_direct_control_v1.py', 'audit_conditional_donor_metadata_v2.py',
    'audit_conditional_donor_repair_v1.py', 'audit_critical_prefix_joint_repair_v1.py',
    'audit_split_feeder_v4.py', 'audit_split_feeder.py', 'audit_deadline_window_repooling.py',
    'audit_window_damage_repair.py', 'audit_repooling.py', 'audit_pinned_repooling.py',
    'audit_mobile_relay.py', 'audit_weighted_relay_ablation.py', 'audit_weighted_direct_pair.py',
)


def _physical(score):
    return {name: score[name] if name == 'late_count' else float(score[name]) for name in ledger.FIELDS}


def _key(score, manifest):
    values = [_physical(score)[name] for name in ledger.FIELDS]
    return [sum(w * v / s for w, v, s in zip(manifest['weights'], values, manifest['scales'])), *values]


def _compact(reference):
    return dict(score=_physical(reference['score']), full_replay_output_sha256=ledger.digest(reference))


def _finite(value):
    return type(value) in (int, float) and isfinite(value)


def _near(actual, expected, message):
    assert _finite(actual) and _finite(expected) and abs(actual - expected) <= 1e-8, message


def _wall_evidence(metadata, phase, manifest):
    """Bind absolute helper clocks to the worker's measured one-method phase."""
    begin, end = phase['started_at_perf_counter'], phase['ended_at_perf_counter']
    offset, wall = phase['start_offset_seconds'], phase['solve_wall_seconds']
    assert all(_finite(v) for v in (begin, end, offset, wall)) and end >= begin and offset >= 0 and wall >= 0
    _near(end - begin, wall, 'Method wall interval differs from the worker phase')
    started, deadline, runtime = (metadata[k] for k in ('started_at_perf_counter', 'deadline_perf_counter', 'runtime_seconds'))
    assert all(_finite(v) for v in (started, deadline, runtime)) and begin <= started <= end and runtime >= 0
    assert runtime <= wall + 1e-8, 'Helper runtime exceeds its paid phase'
    _near(deadline, begin - offset + manifest['budget_seconds'] - manifest['finalization_reserve_seconds'],
          'Helper absolute deadline differs from the worker budget')
    assert started <= deadline or metadata.get('deadline_reached') is True
    latest = begin; timestamps = 0
    def visit(value):
        nonlocal latest, timestamps
        if isinstance(value, list):
            for item in value: visit(item)
        elif isinstance(value, dict):
            for field, item in value.items():
                if field in ('full_verified_at_perf_counter', 'accepted_at_perf_counter',
                             'selected_as_best_at_perf_counter', 'completed_at_perf_counter'):
                    assert _finite(item) and begin <= item <= end, 'Published verification clock is outside its actual paid phase'
                    assert item < deadline, 'Published verification/adoption missed the actual helper deadline'
                    latest = max(latest, item);timestamps += 1
                elif field == 'deadline_perf_counter':
                    assert _finite(item) and item <= deadline, 'Nested search was given time beyond the arm deadline'
                visit(item)
    visit(metadata)
    return dict(phase_start_offset_seconds=offset, phase_wall_seconds=wall,
        helper_started_at_perf_counter=started, helper_deadline_perf_counter=deadline,
        published_timestamp_references=timestamps, latest_published_timestamp_perf_counter=latest)


def _audit_metadata(tasks, topology, base_routes, metadata, *, relay, manifest, phase=None):
    arm = ARMS[int(relay)]
    parameters = manifest['method_parameters'][arm]
    assert set(parameters) == (RELAY_PARAMETERS if relay else DIRECT_PARAMETERS), 'Incomplete or unexpected frozen method bounds'
    deadline = metadata['deadline_perf_counter']
    if relay:
        for name, value in parameters.items():
            _bind(metadata['configuration'][name], value, 'frozen relay accumulation bound ' + name)
        assert metadata['method'] == 'Complete-State-Multi-Relay-Accumulation-V1'
        proof = relay_audit.audit_metadata(tasks, topology, base_routes, metadata,
            weights=manifest['weights'], scales=manifest['scales'], deadline=deadline)
        _bind(manifest['accumulation_fixed_catalogue_parameters'], FIXED_CATALOGUE, 'fixed current-state catalogue bounds')
        for step in metadata['steps']:
            for parent in step['parent_records']:
                catalogue = parent['catalogue']
                if catalogue is not None:
                    for name, value in FIXED_CATALOGUE.items():
                        _bind(catalogue['metadata']['configuration'][name], value, 'actual fixed catalogue bound ' + name)
        if metadata['initialization_completed']:
            final = metadata['post_repair_snapshot'];routes = final['routes']
            _bind(metadata['best_key'], _key(final['score'], manifest), 'actual strict accumulation return key')
        else:
            routes = base_routes
            assert metadata['post_repair_snapshot'] is None and metadata['returned_input_guard']
            assert not metadata['accepted_moves']
    else:
        try:
            from . import audit_multi_relay_direct_control_v1 as direct_audit
        except ImportError:
            import audit_multi_relay_direct_control_v1 as direct_audit
        proof = direct_audit.audit_metadata(tasks, topology, base_routes, metadata,
            weights=manifest['weights'], scales=manifest['scales'], deadline=deadline, parameters=parameters)
        _bind(manifest['accumulation_direct_periodic_parameters'], PERIODIC, 'fixed independent DIRECT periodic policy')
        assert metadata['relay_enabled'] is False
        routes = metadata['final_snapshot']['routes']
    assert proof['passed'] is True
    original = reference_schedule(tasks, base_routes, topology)
    reference = reference_schedule(tasks, routes, topology)
    assert tuple(_key(reference['score'], manifest)) <= tuple(_key(original['score'], manifest)), 'Method returned a worse original guard'
    if not relay: assert not reference['handoffs'], 'Independent DIRECT output contains a relay'
    if phase is not None:
        wall = _wall_evidence(metadata, phase, manifest)
    else: wall = None
    return dict(passed=True, arm=arm, independent_method_evidence=proof, paid_wall_evidence=wall,
        final_reference=_compact(reference), final_routes_sha256=ledger.digest(routes),
        returned_original_guard=ledger.digest(routes) == ledger.digest(base_routes),
        no_shared_raw_directory=True, checkpoint_mechanism_diagnostic_only=True,
        strongest_fresh_comparison=False)


def audit(output):
    """One saved-only replay; never replace a prior completed audit receipt."""
    output = Path(output).resolve()
    assert not (output / 'audit.json').exists(), 'Never overwrite an existing completed audit'
    manifest = ledger.read(output / 'manifest.json')
    ledger.verify(output, manifest, current_execution=False)
    assert tuple(ledger.ACCUMULATION_PAIR) == ARMS
    assert manifest['purpose'] == 'checkpoint_mechanism_diagnostic' and manifest['checkpoint'] is not None
    assert 0 < manifest['finalization_reserve_seconds'] < manifest['budget_seconds']
    protocol_spec = manifest['multi_relay_protocol'];protocol_path = ledger.ROOT / protocol_spec['path']
    assert ledger.sha(protocol_path) == protocol_spec['sha256'], 'Changed predeclared accumulation protocol'
    protocol = ledger.read(protocol_path)
    assert protocol['kind'] == 'predeclared_independent_arm_historical_checkpoint_mechanism_diagnostic'
    _bind(protocol['arms'], list(ARMS), 'predeclared independent pair')
    for field, value in (('budget_seconds_per_arm', manifest['budget_seconds']), ('homes', manifest['homes']),
        ('checkpoint_path', manifest['checkpoint']['path']), ('checkpoint_sha256', manifest['checkpoint']['sha256']),
        ('input_sha256', manifest['input_sha256']), ('weights', manifest['weights']), ('scales', manifest['scales']),
        ('method_parameters', manifest['method_parameters']), ('fixed_catalogue_parameters', FIXED_CATALOGUE),
        ('direct_periodic_parameters', PERIODIC)):
        _bind(protocol[field], value, 'predeclared accumulation protocol ' + field)
    assert manifest['schedule'] == [dict(arm=arm, seed=protocol['seed'], run_id=arm + '__seed_' + str(protocol['seed'])) for arm in ARMS]
    for name in AUDIT_SOURCES:
        assert ledger.sha(ledger.EXPERIMENTS / name) == manifest['execution_sources'][name]['sha256'], name
    tasks = load_official_csv(ledger.ROOT / manifest['input_path'])
    if len(tasks) == 200:
        graph = manifest['topology']
        assert (graph['drone_count'], graph['capacity'], graph['max_tasks_per_drone'], graph['speed_km_per_min']) == (8, 2, 25, .9)
        assert len(graph['stations']) == 4
    base = manifest['initial_routes']
    checkpoint = ledger.read(ledger.ROOT / manifest['checkpoint']['path'])
    checkpoint_reference = audit_payload(tasks, {**manifest['topology'], 'relay_allowed': False}, checkpoint)
    assert [r['encoded_visits'] for r in checkpoint['routes']] == base, 'Initial guard differs from its declared historical saved solution'
    _bind(manifest['initial_score'], _physical(checkpoint_reference['score']), 'historical complete original guard')
    rows = ledger.read(output / 'runs.json')
    assert len(rows) == len(manifest['schedule']) > 0
    assert len({r['run_id'] for r in rows}) == len(rows)
    results = []; paired = {}
    for row, cell in zip(rows, manifest['schedule']):
        assert all(row[name] == cell[name] for name in ('run_id', 'arm', 'seed'))
        assert row['arm'] in ARMS and row['valid'] is True and row['budget_compliant'] is True
        assert 0 <= row['construction_seconds'] <= row['end_to_end_seconds'] <= manifest['budget_seconds']
        _bind(row['budget_seconds'], manifest['budget_seconds'], 'actual common total wall ceiling')
        assert row['purpose'] == manifest['purpose']
        assert row['source_sha256'] == manifest['source_archive']['sha256']
        assert row['initial_routes_sha256'] == manifest['initial_routes_sha256']
        response = output / 'receipts' / (row['run_id'] + '__response.json')
        receipt = ledger.read(output / 'receipts' / (row['run_id'] + '.json'))
        assert receipt['status'] == 'completed' and receipt['manifest_sha256'] == ledger.sha(output / 'manifest.json')
        assert receipt['response_sha256'] == ledger.sha(response) and ledger.read(response) == row
        relay = row['arm'] == ARMS[1]
        topology = {**manifest['topology'], 'relay_allowed': relay}
        assert [p['phase'] for p in row['phases']] == ['initial', row['arm'], 'final'], 'The paid checkpoint arm has an unexpected/free extra search phase'
        before = base; previous_hash = manifest['initial_routes_sha256']; previous_key = None
        method_metadata = method_evidence = None; phase_references = []
        for phase in row['phases']:
            assert ledger.sha(output / phase['solution_path']) == phase['solution_sha256']
            payload = ledger.read(output / phase['solution_path'])
            assert {**phase['topology'], 'relay_allowed': relay} == topology
            phase_reference = audit_payload(tasks, topology, payload)
            routes = [r['encoded_visits'] for r in payload['routes']]
            assert ledger.digest(routes) == phase['routes_sha256'] and phase['input_routes_sha256'] == previous_hash
            _bind(phase['score'], _physical(phase_reference['score']), 'actual complete accumulation phase score')
            current_key = _key(phase_reference['score'], manifest)
            _near(phase['weighted_score'], current_key[0], 'Actual weighted phase scalar differs')
            if previous_key is not None: assert tuple(current_key) <= tuple(previous_key)
            if phase['phase'] == 'initial':
                assert routes == base
                _bind(phase['score'], _physical(manifest['initial_score']), 'actual initial checkpoint score')
            elif phase['phase'] == row['arm']:
                assert before == base, 'A historical relay seed or unrecorded preliminary optimization was inserted'
                method_metadata = phase['metadata'];assert method_metadata['seed'] == row['seed']
                method_evidence = _audit_metadata(tasks, topology, base, method_metadata, relay=relay, manifest=manifest, phase=phase)
                assert method_evidence['final_routes_sha256'] == ledger.digest(routes), 'Worker phase differs from the audited method return'
                _bind(method_evidence['final_reference']['score'], _physical(phase_reference['score']), 'actual method return phase')
                assert phase['start_offset_seconds'] >= row['construction_seconds']
                assert phase['start_offset_seconds'] + phase['solve_wall_seconds'] <= row['end_to_end_seconds'] + 1e-8
            else: assert routes == before, 'Final serialization changed the audited complete method output'
            phase_references.append(dict(phase=phase['phase'], routes_sha256=phase['routes_sha256'], **_compact(phase_reference)))
            previous_key, previous_hash, before = current_key, phase['routes_sha256'], routes
        assert method_evidence is not None and method_metadata is not None
        assert row['solution_path'] == row['phases'][-1]['solution_path'] and row['solution_sha256'] == row['phases'][-1]['solution_sha256']
        _bind({k: row[k] for k in ledger.FIELDS}, {k: previous_key[n + 1] for n, k in enumerate(ledger.FIELDS)}, 'actual final accumulation row')
        _near(row['weighted_score'], previous_key[0], 'Final row weighted scalar differs')
        assert row['on_time_count'] == len(tasks) - row['late_count']
        path = output / row['counterfactual_path'];assert ledger.sha(path) == row['counterfactual_sha256']
        counterfactual = ledger.read(path)
        assert [r['encoded_visits'] for r in counterfactual['routes']] == [list(r) for r in direct_counterfactual(before, topology)]
        cf_reference = audit_payload(tasks, {**topology, 'relay_allowed': False}, counterfactual)
        results.append(dict(run_id=row['run_id'], passed=True, phase_references=phase_references,
            method_evidence=method_evidence, fixed_structure_direct=_compact(cf_reference)))
        assert (row['seed'], row['arm']) not in paired
        paired[row['seed'], row['arm']] = row
    comparisons = []
    for seed in sorted({r['seed'] for r in rows}):
        assert all((seed, arm) in paired for arm in ARMS), 'The declared diagnostic pair is incomplete'
        comparisons.append(dict(seed=seed, arms=list(ARMS), same_original_complete_DIRECT_checkpoint=True,
            same_weights_homes_resources_and_total_wall_ceiling=True,
            DIRECT_has_independent_paid_native_search=True, RELAY_pays_current_state_catalogues=True,
            shared_raw_directory=False, equal_candidate_counts_required=False,
            historical_checkpoint_diagnostic_only=True, strongest_fresh_baseline=False))
    result = dict(passed=True, manifest_sha256=ledger.sha(output / 'manifest.json'), runs_sha256=ledger.sha(output / 'runs.json'),
        results=results, paired_checkpoint_comparisons=comparisons,
        original_guard_reference=_compact(checkpoint_reference), strict_original_telemetry_passed=True)
    assert not (output / 'audit.json').exists(), 'Never overwrite an existing completed audit'
    ledger.write(output / 'audit.json', result)
    print(dict(passed=True, runs=len(rows), complete_phase_references=sum(len(r['phase_references']) for r in results)))
    return result
