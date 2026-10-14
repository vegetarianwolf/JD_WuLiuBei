"""Post-run independent audit revision for protocol scale representation.

The frozen v1 completed-audit receipt was never produced for batch037: it
rejected protocol 200.0 versus manifest 200 despite exact numeric equality.
Only that field's representation comparison changes. Solver, protocol,
manifest, saved routes and all other frozen checks remain untouched.
This new source was created after optimizer execution, not pre-frozen.
"""
from __future__ import annotations
from math import isfinite
from pathlib import Path
try:
    from . import audit_multi_relay_batch_v1 as frozen
except ImportError:
    import audit_multi_relay_batch_v1 as frozen

ledger = frozen.ledger
ARMS = frozen.ARMS
FIXED_CATALOGUE = frozen.FIXED_CATALOGUE
PERIODIC = frozen.PERIODIC
AUDIT_SOURCES = frozen.AUDIT_SOURCES
_bind = frozen._bind
_physical = frozen._physical
_key = frozen._key
_near = frozen._near
_compact = frozen._compact
_audit_metadata = frozen._audit_metadata
load_official_csv = frozen.load_official_csv
audit_payload = frozen.audit_payload
direct_counterfactual = frozen.direct_counterfactual
reference_schedule = frozen.reference_schedule
ORIGINAL_FROZEN_AUDIT_SHA256 = '1f008546eb020e1233e1773c3156f6da4614c0e69593025ef1fb1c4be523f74f'


def _numeric_scales(actual, expected):
    """Permit only a JSON int/float representation difference, never tolerance."""
    assert type(actual) is list and type(expected) is list and len(actual) == len(expected), 'Protocol scales length/type differs'
    for left, right in zip(actual, expected):
        assert type(left) in (int, float) and type(right) in (int, float), 'Protocol scales must be numeric and not bool'
        assert isfinite(left) and isfinite(right), 'Protocol scales must be finite'
        assert left == right, 'Protocol scales numeric value differs'


def _original_failure(output, manifest, protocol):
    """Bind an existing failed-attempt receipt without inventing an old pass."""
    path = output / 'audit_attempt_001.json'
    if not path.exists():
        return dict(status='not_run_or_no_failed_attempt_receipt', original_frozen_audit_passed=None)
    receipt = ledger.read(path)
    _bind(receipt['status'], 'terminal_failed_before_method_replays', 'original failed audit status')
    _bind(receipt['returncode'], 1, 'original failed audit returncode')
    _bind(receipt['error_type'], 'ValueError', 'original failed audit error type')
    _bind(receipt['error_message'], 'predeclared accumulation protocol scales[0] integer differs', 'original failed audit error')
    _bind(receipt['original_batch_auditor_sha256'], ORIGINAL_FROZEN_AUDIT_SHA256, 'original failed frozen audit source')
    _bind(receipt['manifest_sha256'], ledger.sha(output / 'manifest.json'), 'original failed audit immutable manifest')
    _bind(receipt['runs_sha256'], ledger.sha(output / 'runs.json'), 'original failed audit immutable runs')
    _bind(receipt['protocol_sha256'], manifest['multi_relay_protocol']['sha256'], 'original failed audit immutable protocol')
    _numeric_scales(receipt['manifest_scales'], manifest['scales'])
    _numeric_scales(receipt['protocol_scales'], protocol['scales'])
    for field, expected in (('all_scale_values_exactly_equal', True), ('source_and_protocol_hashes_unchanged', True),
        ('saved_solution_fields_changed', False), ('optimizer_rerun', False), ('no_completed_audit_json_written', True)):
        _bind(receipt[field], expected, 'original failed audit fact ' + field)
    return dict(status=receipt['status'], original_frozen_audit_passed=False,
        receipt_path=str(path), receipt_sha256=ledger.sha(path), error_message=receipt['error_message'])


def audit(output):
    """One saved-only replay; never replace a prior completed audit receipt."""
    output = Path(output).resolve()
    assert not (output / 'audit.json').exists(), 'Never overwrite an existing completed audit'
    assert ledger.sha(Path(frozen.__file__).resolve()) == ORIGINAL_FROZEN_AUDIT_SHA256, 'Original frozen audit source changed'
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
        if field == 'scales': _numeric_scales(protocol[field], value)
        else: _bind(protocol[field], value, 'predeclared accumulation protocol ' + field)
    assert manifest['schedule'] == [dict(arm=arm, seed=protocol['seed'], run_id=arm + '__seed_' + str(protocol['seed'])) for arm in ARMS]
    original_failure = _original_failure(output, manifest, protocol)
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
    result['audit_revision'] = dict(
        kind='post_run_independent_audit_revision',
        original_frozen_audit_source_sha256=ORIGINAL_FROZEN_AUDIT_SHA256,
        revised_audit_source_sha256=ledger.sha(Path(__file__).resolve()),
        revised_audit_source_path=str(Path(__file__).resolve()),
        revised_audit_was_frozen_before_optimizer_execution=False,
        original_frozen_audit_passed=original_failure['original_frozen_audit_passed'],
        original_failed_attempt_evidence=original_failure,
        change_scope='protocol.scales numeric representation only; exact finite non-bool numeric equality',
        optimizer_protocol_manifest_results_changed=False,
        other_frozen_metadata_physics_adoption_checks_reused_unchanged=True,
        protocol_scales=protocol['scales'], manifest_scales=manifest['scales'])
    ledger.write(output / 'audit.json', result)
    print(dict(passed=True, runs=len(rows), complete_phase_references=sum(len(r['phase_references']) for r in results)))
    return result
