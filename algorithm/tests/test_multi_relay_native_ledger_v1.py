"""Saved-only native-pair ledger boundaries, using the existing 025 payload.

No solver, worker, runtime transformation, construction or ALNS is called.
Only the metadata callback is isolated: its real raw/post proof has its own
synthetic integration suite. Canonical routes, receipts and CF checks are real.
"""
from copy import deepcopy
import sys
from types import ModuleType

import pytest

from algorithm.experiments import run_proactive_relay_research as ledger
from algorithm.experiments import audit_multi_relay_native_batch_v1 as callback
from algorithm.experiments.audit_multi_relay_batch_v1 import FIXED_CATALOGUE, PERIODIC

CHECKPOINT = ledger.ROOT / 'algorithm/results/proactive_relay_diagnostic_20261014/batch025/phase_solutions/pair_pin_direct__seed_2026101511__final.json'
GRAPH_SOURCE = ledger.ROOT / 'algorithm/results/proactive_relay_diagnostic_20261014/batch037/manifest.json'


def make_saved_pair(path, monkeypatch):
    original = ledger.read(CHECKPOINT)
    historical = ledger.read(GRAPH_SOURCE)
    base = [r['encoded_visits'] for r in original['routes']]
    topology = historical['topology']
    schedule = [dict(arm=arm, seed=23, run_id=arm+'__seed_23') for arm in ledger.MULTI_NATIVE_PAIR]
    manifest = dict(schedule=schedule, topology=topology, input_path=historical['input_path'],
        input_sha256=historical['input_sha256'], homes='stations', artifact_json_indent=0,
        weights=historical['weights'], scales=historical['scales'],
        purpose='checkpoint_mechanism_diagnostic', budget_seconds=120., finalization_reserve_seconds=1.,
        checkpoint=dict(path=str(CHECKPOINT), sha256=ledger.sha(CHECKPOINT)),
        initial_routes=base, initial_routes_sha256=ledger.digest(base), initial_score=original['score'],
        source_archive=dict(sha256=historical['source_archive']['sha256']),
        execution_sources={name: dict(sha256=ledger.sha(ledger.EXPERIMENTS/name)) for name in
            ('audit_weighted_relay_ablation.py', 'audit_weighted_direct_pair.py')},
        method_parameters={'multi_native_direct': dict(alns_fraction=.65),
            'multi_native_relay': dict(raw_fraction=.4)},
        accumulation_fixed_catalogue_parameters=FIXED_CATALOGUE, periodic_parameters=PERIODIC)
    write = lambda file, value: ledger.write(file, value, indent=0)
    write(path/'manifest.json', manifest)
    rows = []
    for cell in schedule:
        relay = cell['arm'].endswith('_relay')
        payload = deepcopy(original)
        # No transport changes: the R arm's fixed registered-candidate coverage
        # differs from the plain output, but all 200 plans remain DIRECT.
        payload['relay']['relay_candidate_coverage'] = 1. if relay else 0.
        phases = []
        for name in ('initial', cell['arm'], 'final'):
            filename = 'phase_solutions/'+cell['arm']+'__'+name+'.json'
            write(path/filename, payload)
            values = [payload['score'][field] for field in ledger.FIELDS]
            key = sum(w*v/s for w, v, s in zip(manifest['weights'], values, manifest['scales']))
            phase = dict(phase=name, solution_path=filename, solution_sha256=ledger.sha(path/filename),
                routes_sha256=ledger.digest(base), input_routes_sha256=ledger.digest(base),
                score=payload['score'], weighted_score=key,
                metadata={'seed': cell['seed']} if name == cell['arm'] else {})
            if name == cell['arm']:
                phase.update(started_at_perf_counter=1., ended_at_perf_counter=2.,
                    start_offset_seconds=.1, solve_wall_seconds=1.)
            phases.append(phase)
        cf_path = 'counterfactuals/'+cell['arm']+'.json';write(path/cf_path, original)
        row = dict(**cell, phases=phases, valid=True, budget_compliant=True,
            purpose=manifest['purpose'], budget_seconds=manifest['budget_seconds'],
            end_to_end_seconds=1.2, construction_seconds=.1,
            initial_routes_sha256=ledger.digest(base), source_sha256=manifest['source_archive']['sha256'],
            solution_path=phases[-1]['solution_path'], solution_sha256=phases[-1]['solution_sha256'],
            counterfactual_path=cf_path, counterfactual_sha256=ledger.sha(path/cf_path),
            weighted_score=phases[-1]['weighted_score'], on_time_count=141, **original['score'])
        rows.append(row)
    write(path/'runs.json', rows)
    refresh(path, rows)
    # The separately verified parent/archive/prepare chain is outside this
    # saved-only ledger unit test. No metadata or physical audit is bypassed
    # except the explicitly isolated arm callback below.
    monkeypatch.setattr(ledger, 'verify', lambda *args, **kwargs: None)
    calls = []
    def public(tasks, graph, actual_base, metadata, *, relay, manifest, phase):
        calls.append(dict(tasks=tasks, topology=graph, base=actual_base, metadata=metadata,
            relay=relay, phase=phase))
        return dict(passed=True, final_routes_sha256=ledger.digest(actual_base),
            final_reference=dict(score=deepcopy(original['score'])), isolated_callback_only=True)
    monkeypatch.setattr(callback, '_audit_metadata', public)
    return rows, manifest, calls


def refresh(path, rows):
    ledger.write(path/'runs.json', rows, indent=0)
    for row in rows:
        response = path/'receipts'/(row['run_id']+'__response.json')
        ledger.write(response, row, indent=0)
        ledger.write(path/'receipts'/(row['run_id']+'.json'), dict(status='completed',
            manifest_sha256=ledger.sha(path/'manifest.json'), response_sha256=ledger.sha(response)), indent=0)


def test_saved_025_routes_bind_new_callback_phases_cf_and_no_audit_overwrite(tmp_path, monkeypatch):
    rows, manifest, calls = make_saved_pair(tmp_path, monkeypatch)
    ledger.audit(tmp_path)
    proof = ledger.read(tmp_path/'audit.json')
    assert proof['passed'] and len(proof['results']) == len(calls) == 2
    assert [c['relay'] for c in calls] == [False, True]
    assert all(len(c['tasks']) == 200 and c['base'] == manifest['initial_routes'] for c in calls)
    assert all(c['phase']['phase'] == row['arm'] for c, row in zip(calls, rows))
    assert all(c['topology']['relay_allowed'] is c['relay'] for c in calls)
    assert all(r['method_evidence']['isolated_callback_only'] for r in proof['results'])
    assert proof['independent_native_pair']['direct_all_tasks_free']
    assert not proof['independent_native_pair']['strongest_fresh_comparison']
    with pytest.raises(AssertionError, match='overwrite'):
        ledger.audit(tmp_path)


@pytest.mark.parametrize('fault', ['response_drift', 'receipt_hash', 'extra_phase', 'callback_hash', 'final_artifact', 'CF'])
def test_saved_ledger_cannot_hide_receipt_phase_route_or_cf_drift(tmp_path, monkeypatch, fault):
    rows, manifest, calls = make_saved_pair(tmp_path, monkeypatch)
    row = rows[-1]
    if fault == 'response_drift':
        row['end_to_end_seconds'] += .01
        ledger.write(tmp_path/'runs.json', rows, indent=0)
    elif fault == 'receipt_hash':
        file = tmp_path/'receipts'/(row['run_id']+'.json');receipt = ledger.read(file)
        receipt['response_sha256'] = '0'*64;ledger.write(file, receipt, indent=0)
    elif fault == 'extra_phase':
        row['phases'].insert(2, deepcopy(row['phases'][1]));refresh(tmp_path, rows)
    elif fault == 'callback_hash':
        old = callback._audit_metadata
        def different(*args, **kwargs):
            proof = old(*args, **kwargs);proof['final_routes_sha256'] = '0'*64;return proof
        monkeypatch.setattr(callback, '_audit_metadata', different)
    elif fault == 'final_artifact':
        row['solution_path'] = row['phases'][0]['solution_path']
        row['solution_sha256'] = row['phases'][0]['solution_sha256'];refresh(tmp_path, rows)
    else:
        file = tmp_path/row['counterfactual_path'];payload = ledger.read(file)
        payload['score']['distance_km'] += 1.
        ledger.write(file, payload);row['counterfactual_sha256'] = ledger.sha(file);refresh(tmp_path, rows)
    with pytest.raises((AssertionError, ValueError)):
        ledger.audit(tmp_path)
    assert not (tmp_path/'audit.json').exists()


def test_protocol_values_and_schedule_bind_before_prepare(tmp_path):
    historical = ledger.read(GRAPH_SOURCE)
    params = dict(multi_native_direct=dict(alns_fraction=.65), multi_native_relay=dict(raw_fraction=.4))
    manifest = dict(purpose='checkpoint_mechanism_diagnostic', checkpoint=dict(path=str(CHECKPOINT), sha256=ledger.sha(CHECKPOINT)),
        input_sha256=historical['input_sha256'], weights=historical['weights'], scales=historical['scales'],
        homes='stations', budget_seconds=120., finalization_reserve_seconds=1., method_parameters=params,
        topology=historical['topology'], artifact_json_indent=0,
        accumulation_fixed_catalogue_parameters=FIXED_CATALOGUE, periodic_parameters=PERIODIC,
        schedule=[dict(arm=a, seed=23, run_id=a+'__seed_23') for a in ledger.MULTI_NATIVE_PAIR])
    manifest['execution_sources'] = {name+'.py': {} for name in
        ('multi_relay_direct_control_v1', 'multi_relay_native_refinement_v1', 'audit_multi_relay_native_batch_v1')}
    protocol = dict(kind='predeclared_independent_arm_multi_group_native_historical_checkpoint_diagnostic',
        batch=tmp_path.name, arms=list(ledger.MULTI_NATIVE_PAIR), seed=23, budget_seconds_per_arm=120.,
        homes='stations', checkpoint_path=str(CHECKPOINT), checkpoint_sha256=ledger.sha(CHECKPOINT),
        input_sha256=manifest['input_sha256'], weights=manifest['weights'], scales=manifest['scales'],
        method_parameters=params, fixed_catalogue_parameters=FIXED_CATALOGUE,
        direct_periodic_parameters=PERIODIC, artifact_json_indent=0)
    file = tmp_path/'protocol.json';ledger.write(file, protocol)
    manifest['multi_relay_native_protocol'] = dict(path=str(file), sha256=ledger.sha(file))
    ledger._verify_multi_native_protocol(tmp_path, manifest)
    changed = deepcopy(manifest);changed['execution_sources'].pop('audit_multi_relay_native_batch_v1.py')
    with pytest.raises(AssertionError): ledger._verify_multi_native_protocol(tmp_path, changed)
    changed = deepcopy(manifest);changed['method_parameters']['multi_native_relay']['raw_fraction'] = .5
    with pytest.raises(ValueError): ledger._verify_multi_native_protocol(tmp_path, changed)
    changed = deepcopy(manifest);changed['schedule'][0]['seed'] += 1
    with pytest.raises(AssertionError): ledger._verify_multi_native_protocol(tmp_path, changed)
    protocol['seed'] += 1;ledger.write(file, protocol)
    with pytest.raises(AssertionError): ledger._verify_multi_native_protocol(tmp_path, manifest)


def test_native_protocol_cannot_pair_transport_with_another_auditor_or_missing_source():
    baseline, auditor = ledger._native_implementation({})
    assert baseline == {arm: ledger.METHODS[arm] for arm in ledger.MULTI_NATIVE_PAIR}
    assert auditor == 'audit_multi_relay_native_batch_v1'
    joint = dict(method_modules=dict(multi_native_direct='multi_relay_direct_control_v1',
        multi_native_relay='joint_source_native_refinement_v1'), metadata_auditor='audit_joint_source_native_v1')
    assert ledger._native_implementation(joint)[1] == joint['metadata_auditor']
    for changed in (dict(joint, metadata_auditor=auditor),
                    dict(joint, method_modules={**joint['method_modules'], 'multi_native_direct': 'joint_source_native_refinement_v1'}),
                    dict(joint, method_modules={**joint['method_modules'], 'multi_native_relay': 'unregistered_module'})):
        with pytest.raises(AssertionError): ledger._native_implementation(changed)


def test_same_saved_ledger_selects_declared_typed_auditor(tmp_path, monkeypatch):
    rows, manifest, calls = make_saved_pair(tmp_path, monkeypatch)
    manifest.update(method_modules=dict(multi_native_direct='multi_relay_direct_control_v1',
        multi_native_relay='joint_source_native_refinement_v1'), metadata_auditor='audit_joint_source_native_v1')
    ledger.write(tmp_path/'manifest.json', manifest, indent=0);refresh(tmp_path, rows)
    selected = []
    module = ModuleType('algorithm.experiments.audit_joint_source_native_v1')
    old = callback._audit_metadata
    def typed(*args, **kwargs):
        selected.append(kwargs['relay']);return old(*args, **kwargs)
    module._audit_metadata = typed
    monkeypatch.setitem(sys.modules, module.__name__, module)
    ledger.audit(tmp_path)
    assert selected == [False, True] and len(calls) == 2
