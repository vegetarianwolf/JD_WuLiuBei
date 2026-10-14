"""Read-only protocol/audit fixtures; no official-instance optimization."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil

import pytest

from algorithm.experiments import audit_weighted_periodic_pair as audit


ROOT = Path(__file__).resolve().parents[2]
FIRST = ROOT / 'algorithm/results/weighted_method_improvement_20261014'
SECOND = ROOT / 'algorithm/results/weighted_periodic_improvement_20261014'


@pytest.fixture
def contract():
    return audit.read(SECOND / 'manifest.json'), audit.read(FIRST / 'manifest.json')


def test_schema3_projection_preserves_original_rows_and_manifest(contract):
    manifest, parent = contract
    original = copy.deepcopy(manifest)
    audit.audit_contract(manifest, parent)
    projected = audit.project_manifest(manifest)
    assert manifest == original and projected['schema_version'] == 2
    assert projected['order'] == audit.first.ORDER
    assert set(projected['execution_sources']) == audit.OLD_SCRIPTS
    assert projected['variants'][audit.first.ORDER[1]]['sha256'] == manifest['variants'][audit.ORDER[1]]['sha256']
    row = {'variant': audit.ORDER[1], 'periodic_task_exchange': {'passes': 1}}
    snapshot = copy.deepcopy(row)
    assert audit.project_row(row)['variant'] == audit.first.ORDER[1]
    assert row == snapshot


@pytest.mark.parametrize('mutation', [
    lambda m: m.update(schema_version=2),
    lambda m: m.update(order=list(reversed(audit.ORDER))),
    lambda m: m.update(seed=2026101462),
    lambda m: m.update(weights=[.6, .2, .2]),
    lambda m: m.update(budget_seconds=248),
    lambda m: m['periodic_parameters'].update(pair_budget_seconds=.009),
    lambda m: m['periodic_parameters'].update(interval=60),
    lambda m: m['periodic_parameters'].update(max_candidates=17),
    lambda m: m['periodic_parameters'].update(anchors_per_route=5),
    lambda m: m['periodic_parameters'].update(max_candidates=16.),
    lambda m: m['variants'][audit.ORDER[1]]['config'].update(enable_lazy_repair=False),
    lambda m: m['variants'][audit.ORDER[1]].update(sha256='0' * 64),
    lambda m: m['variants'][audit.ORDER[0]].update(periodic_exchange=True),
    lambda m: m['execution_sources'].pop('weighted_periodic_exchange.py'),
    lambda m: m['shared_baseline'].update(reused=1),
])
def test_periodic_contract_rejects_changed_scope_sources_or_bounds(contract, mutation):
    manifest, parent = contract
    mutation(manifest)
    with pytest.raises(ValueError):
        audit.audit_contract(manifest, parent)


@pytest.fixture
def shared_fixture(contract, tmp_path):
    manifest, _ = contract
    parent_rows = audit.read(FIRST / 'runs.json')
    rows = [copy.deepcopy(parent_rows[0]), {'variant': audit.ORDER[1]}]
    for name in audit.ORDER:
        spec = manifest['variants'][name]
        destination = tmp_path / spec['archive']
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SECOND / spec['archive'], destination)
    destination = tmp_path / rows[0]['solution_path']
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(FIRST / rows[0]['solution_path'], destination)
    for name in audit.NEW_SCRIPTS:
        spec = manifest['execution_sources'][name]
        target = tmp_path / spec['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SECOND / spec['path'], target)
    (tmp_path / 'runs.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n')
    return tmp_path, manifest, rows


def test_shared_baseline_binds_parent_hashes_original_row_and_solution_bytes(shared_fixture):
    output, manifest, rows = shared_fixture
    parent, checked = audit.audit_parent_and_shared(output, manifest, rows)
    assert parent['schema_version'] == 2
    assert checked['shared_baseline_reused'] and checked['baseline_row_and_solution_exact']
    assert checked['own_baseline'] is False and checked['core_zips_exact_first_stage']
    assert audit.audit_periodic_sources(output, manifest)['periodic_execution_sources_frozen']


@pytest.mark.parametrize('mutation', ['parent_manifest', 'parent_audit', 'parent_runs', 'row_label',
                                    'row_numeric_type', 'solution_bytes', 'core_bytes', 'shared_path'])
def test_shared_baseline_rejects_modified_provenance_or_claimed_exact_copy(shared_fixture, mutation):
    output, manifest, rows = shared_fixture
    if mutation in ('parent_manifest', 'parent_audit', 'parent_runs'):
        manifest['parent_method_' + mutation.removeprefix('parent_') + '_sha256'] = '0' * 64
    elif mutation == 'row_label':
        rows[0]['label'] += ' changed'
    elif mutation == 'row_numeric_type':
        rows[0]['seed'] = float(rows[0]['seed'])
    elif mutation == 'solution_bytes':
        with (output / rows[0]['solution_path']).open('a') as handle:
            handle.write(' ')
    elif mutation == 'core_bytes':
        with (output / manifest['variants'][audit.ORDER[1]]['archive']).open('ab') as handle:
            handle.write(b'changed')
    else:
        manifest['shared_baseline']['source_runs_path'] = 'algorithm/results/nonexistent/runs.json'
    with pytest.raises((ValueError, OSError)):
        audit.audit_parent_and_shared(output, manifest, rows)


@pytest.mark.parametrize('update_declared_hash', [False, True])
def test_additional_execution_source_is_bound_to_frozen_and_current_bytes(shared_fixture, update_declared_hash):
    output, manifest, _ = shared_fixture
    spec = manifest['execution_sources']['weighted_periodic_exchange.py']
    path = output / spec['path']
    path.write_text(path.read_text() + '\n# unapproved wrapper change\n')
    if update_declared_hash:
        spec['sha256'] = audit.sha(path)
    with pytest.raises(ValueError, match='execution source'):
        audit.audit_periodic_sources(output, manifest)


def test_fresh_replay_has_its_own_baseline_and_never_claims_an_exact_shared_sample(shared_fixture):
    output, manifest, rows = shared_fixture
    manifest['shared_baseline']['reused'] = False
    rows[0]['label'] = 'Fresh own baseline arithmetic fixture'
    rows[0]['periodic_task_exchange'] = {}
    _, checked = audit.audit_parent_and_shared(output, manifest, rows)
    assert checked['own_baseline'] is True and checked['shared_baseline_reused'] is False
    assert checked['baseline_row_and_solution_exact'] is False


def test_shared_baseline_rejects_raw_literal_rewriting_with_identical_parsed_values(shared_fixture):
    output, manifest, rows = shared_fixture
    path = output / 'runs.json'
    text = path.read_text()
    original = str(rows[0]['end_to_end_seconds'])
    path.write_text(text.replace(original, original + 'e0', 1))
    assert audit.read(path)[0]['end_to_end_seconds'] == rows[0]['end_to_end_seconds']
    with pytest.raises(ValueError, match='raw JSON'):
        audit.audit_parent_and_shared(output, manifest, rows)


@pytest.fixture
def periodic_fixture(contract):
    manifest, _ = contract
    event = dict(repair_call=120, route_pair=[0, 1], ranking_probes=40, shortlisted_candidates=16,
                 probes=2, canonical_checks=2, improvements=1, same_route_skips=0,
                 deadline_reached=False, before_score=dict(late_count=67, total_lateness_min=2099., distance_km=605.),
                 after_score=dict(late_count=67, total_lateness_min=2095., distance_km=604.),
                 precheck_seconds=.0002, ranking_seconds=.0005, probe_seconds=.0004,
                 canonical_seconds=.0002, budget_seconds=.008, runtime_seconds=.002)
    stats = dict(repair_calls=120, passes=1, probes=2, canonical_checks=2, improvements=1,
                 runtime_seconds=.002, events=[event])
    row = dict(iterations=120, search_seconds=1., periodic_task_exchange=stats)
    payload = dict(metadata=dict(periodic_task_exchange=copy.deepcopy(stats)))
    return manifest, row, payload


def test_periodic_statistics_are_json_native_and_bound_to_caller_own_before_after(periodic_fixture):
    manifest, row, payload = periodic_fixture
    checked = audit.audit_periodic_statistics(row, payload, manifest)
    assert checked['passed'] and checked['canonical_weighted_checkpoints']
    assert checked['improvements'] == 1 and checked['runtime_seconds'] == .002
    json.dumps(checked, allow_nan=False)


@pytest.mark.parametrize('mutation', ['row_payload', 'repair_count', 'passes', 'ranking_cap', 'precision_cap',
                                    'improvement_count', 'aggregate', 'rotation', 'interval', 'budget',
                                    'finite_runtime', 'stage_sum', 'negative_stage', 'unreported_timeout',
                                    'score_worsens', 'missing_canonical', 'missing_score', 'bool_counter'])
def test_periodic_audit_rejects_bad_bounds_scores_timings_or_counter_aggregation(periodic_fixture, mutation):
    manifest, row, payload = periodic_fixture
    stats = row['periodic_task_exchange']
    event = stats['events'][0]
    if mutation == 'row_payload':
        stats['probes'] += 1
    elif mutation == 'repair_count':
        stats['repair_calls'] += 1
    elif mutation == 'passes':
        stats['passes'] = 2
    elif mutation == 'ranking_cap':
        event['ranking_probes'] = 201
    elif mutation == 'precision_cap':
        event['probes'] = 17
    elif mutation == 'improvement_count':
        event['improvements'] = 3
    elif mutation == 'aggregate':
        stats['canonical_checks'] += 1
    elif mutation == 'rotation':
        event['route_pair'] = [0, 2]
    elif mutation == 'interval':
        event['repair_call'] = 119
    elif mutation == 'budget':
        event['budget_seconds'] = .009
    elif mutation == 'finite_runtime':
        event['runtime_seconds'] = float('nan')
    elif mutation == 'stage_sum':
        event['ranking_seconds'] = 1.
    elif mutation == 'negative_stage':
        event['canonical_seconds'] = -.01
    elif mutation == 'unreported_timeout':
        event['runtime_seconds'] = .02
    elif mutation == 'score_worsens':
        event['after_score']['total_lateness_min'] = 2200.
        event['after_score']['distance_km'] = 620.
    elif mutation == 'missing_canonical':
        event['canonical_checks'] = 1
    elif mutation == 'missing_score':
        event['before_score'] = None
    else:
        event['probes'] = True
    if mutation != 'row_payload':
        payload['metadata']['periodic_task_exchange'] = copy.deepcopy(stats)
    with pytest.raises(ValueError):
        audit.audit_periodic_statistics(row, payload, manifest)


def test_input_timeout_requires_no_probes_or_adopted_checkpoint(periodic_fixture):
    manifest, row, payload = periodic_fixture
    stats = row['periodic_task_exchange']
    event = stats['events'][0]
    event.update(ranking_probes=0, shortlisted_candidates=0, probes=0, canonical_checks=1,
                 improvements=0, before_score=None, after_score=None, deadline_reached=True,
                 precheck_seconds=.009, ranking_seconds=0., probe_seconds=0., canonical_seconds=0.,
                 runtime_seconds=.009)
    stats.update(probes=0, canonical_checks=1, improvements=0, runtime_seconds=.009)
    payload['metadata']['periodic_task_exchange'] = copy.deepcopy(stats)
    assert audit.audit_periodic_statistics(row, payload, manifest)['deadline_stops'] == 1


def test_audit_cli_writes_a_visible_failure_for_incomplete_results(tmp_path):
    (tmp_path / 'manifest.json').write_text('{}')
    (tmp_path / 'runs.json').write_text('[]')
    assert audit.main(['--output-dir', str(tmp_path)]) == 1
    result = audit.read(tmp_path / 'audit.json')
    assert result['passed'] is False and result['schema_version'] == 3 and result['error']
