"""Independent schema-3 periodic-exchange audit; standard library only.

Immutable schema-2 projections reuse the earlier independent CSV/geometric
walk and source-adapter checks. Original rows and manifests are never edited.
Additional checks bind the shared baseline, wrapper source and periodic work.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
from itertools import combinations
import json
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

try:
    from . import audit_weighted_method_pair as first
except ImportError:
    import audit_weighted_method_pair as first


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / 'algorithm/results/weighted_periodic_improvement_20261014'
ORDER = ['weighted_baseline', 'weighted_periodic']
PARAMETERS = dict(interval=120, max_candidates=16, anchors_per_route=4, pair_budget_seconds=.008)
OLD_SCRIPTS = {'run_weighted_method_pair.py', 'run_weighted_direct_pair.py', 'weighted_direct_objective.py'}
NEW_SCRIPTS = {'run_weighted_periodic_pair.py', 'weighted_periodic_exchange.py'}
FIELDS = first.FIELDS
read, sha, digest = first.read, first.sha, first.digest
require, same, finite, within = first.require, first.same, first.finite, first.within
score, walk, objective_key = first.score, first.walk, first.objective_key


def first_row_json(path):
    """Extract the first raw array object, preserving its JSON literals."""
    raw = Path(path).read_text()
    position = len(raw) - len(raw.lstrip())
    require(raw[position:position + 1] == '[', 'Run records must be a JSON array')
    position += 1
    while position < len(raw) and raw[position].isspace():
        position += 1
    value, end = json.JSONDecoder().raw_decode(raw, position)
    require(isinstance(value, dict), 'First run record must be an object')
    return raw[position:end]


def project_manifest(manifest):
    """Return a separate schema-2 view, retaining frozen archive paths."""
    projected = copy.deepcopy(manifest)
    projected['schema_version'] = 2
    projected['order'] = first.ORDER.copy()
    projected['variants'] = {old: copy.deepcopy(manifest['variants'][new])
                             for old, new in zip(first.ORDER, ORDER, strict=True)}
    projected['execution_sources'] = {name: copy.deepcopy(manifest['execution_sources'][name])
                                      for name in OLD_SCRIPTS}
    return projected


def project_row(row):
    projected = copy.deepcopy(row)
    projected['variant'] = first.ORDER[ORDER.index(row['variant'])]
    return projected


def audit_contract(manifest, parent_method):
    require(manifest['schema_version'] == 3 and manifest['order'] == ORDER
            and set(manifest['variants']) == set(ORDER)
            and manifest['baseline_variant'] == ORDER[0], 'Expected exactly schema-3 periodic pair')
    require(manifest['periodic_parameters'] == PARAMETERS
            and all(type(manifest['periodic_parameters'][key]) is type(value)
                    for key, value in PARAMETERS.items()), 'Frozen periodic parameters changed')
    require(parent_method['schema_version'] == 2 and parent_method['order'] == first.ORDER,
            'Periodic batch requires the completed first method pair')
    for key in ('seed', 'weights', 'scales', 'formula', 'budget_seconds', 'finalization_reserve_seconds',
                'input_path', 'input_sha256', 'problem_path', 'problem_sha256', 'initial_score',
                'initial_routes', 'initial_routes_sha256', 'parent_manifest_path', 'parent_manifest_sha256',
                'parent_weighted_source_sha256', 'production_files_sha256', 'adapter_changes'):
        require(manifest[key] == parent_method[key], 'First-stage frozen contract changed: ' + key)
    for new, old in zip(ORDER, first.ORDER, strict=True):
        spec, parent_spec = manifest['variants'][new], parent_method['variants'][old]
        for key in ('sha256', 'objective', 'config'):
            require(spec[key] == parent_spec[key], 'Frozen core changed: ' + new + '/' + key)
        require(spec['periodic_exchange'] is (new == ORDER[1]), 'Incorrect periodic arm flag')
    require(set(manifest['execution_sources']) == OLD_SCRIPTS | NEW_SCRIPTS,
            'Frozen periodic execution-source inventory changed')
    for name in OLD_SCRIPTS:
        require(manifest['execution_sources'][name]['sha256'] == parent_method['execution_sources'][name]['sha256'],
                'Earlier execution source changed: ' + name)
    shared = manifest['shared_baseline']
    require(type(shared['reused']) is bool, 'Shared baseline flag must be explicit boolean')


def audit_parent_and_shared(output, manifest, rows):
    parent_path = within(ROOT, manifest['parent_method_manifest_path'])
    parent_directory = parent_path.parent
    require(sha(parent_path) == manifest['parent_method_manifest_sha256'], 'Parent method manifest changed')
    require(sha(parent_directory / 'audit.json') == manifest['parent_method_audit_sha256'], 'Parent method audit changed')
    require(sha(parent_directory / 'runs.json') == manifest['parent_method_runs_sha256'], 'Parent method runs changed')
    parent_method, parent_audit, parent_rows = (read(parent_path), read(parent_directory / 'audit.json'),
                                               read(parent_directory / 'runs.json'))
    require(parent_audit['passed'] is True and parent_audit['run_count'] == 2
            and parent_audit['manifest_sha256'] == sha(parent_path)
            and parent_audit['runs_sha256'] == sha(parent_directory / 'runs.json'),
            'Parent method audit does not bind its completed records')
    require([row['variant'] for row in parent_rows] == first.ORDER, 'Parent method run inventory changed')
    audit_contract(manifest, parent_method)
    shared = manifest['shared_baseline']
    require(within(ROOT, shared['source_runs_path']) == parent_directory / 'runs.json'
            and shared['source_runs_sha256'] == manifest['parent_method_runs_sha256'],
            'Declared shared baseline run source changed')
    source_solution = within(parent_directory, parent_rows[0]['solution_path'])
    require(within(ROOT, shared['source_solution_path']) == source_solution
            and shared['source_solution_sha256'] == parent_rows[0]['solution_sha256'] == sha(source_solution),
            'Declared baseline solution source changed')
    for new, old in zip(ORDER, first.ORDER, strict=True):
        archive = within(output, manifest['variants'][new]['archive'])
        parent_archive = within(parent_directory, parent_method['variants'][old]['archive'])
        require(sha(archive) == manifest['variants'][new]['sha256'] == sha(parent_archive)
                and archive.read_bytes() == parent_archive.read_bytes(), 'Core ZIP is not exact first-stage source')
    if shared['reused']:
        # Preserve key order and numeric representation, not merely Python's
        # bool/int/float equality. Outer array indentation is immaterial.
        encoded = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        require(encoded(rows[0]) == encoded(parent_rows[0]), 'Shared baseline row is not copied exactly')
        require(first_row_json(output / 'runs.json') == first_row_json(parent_directory / 'runs.json'),
                'Shared baseline raw JSON record changed')
        solution = within(output, rows[0]['solution_path'])
        require(solution.read_bytes() == source_solution.read_bytes()
                and sha(solution) == shared['source_solution_sha256'], 'Shared baseline solution bytes changed')
    else:
        require(rows[0].get('periodic_task_exchange', {}) == {}, 'Fresh own baseline must not use periodic exchange')
    return parent_method, dict(shared_baseline_reused=shared['reused'], own_baseline=not shared['reused'],
                              baseline_row_and_solution_exact=shared['reused'],
                              parent_method_artifacts_bound=True, core_zips_exact_first_stage=True)


def audit_periodic_statistics(row, payload, manifest):
    stats = row['periodic_task_exchange']
    require(stats == payload['metadata'].get('periodic_task_exchange'),
            'Periodic row telemetry differs from the saved solution')
    required = {'repair_calls', 'passes', 'probes', 'canonical_checks', 'improvements', 'runtime_seconds', 'events'}
    require(isinstance(stats, dict) and set(stats) == required, 'Periodic statistics fields changed')
    for key in required - {'runtime_seconds', 'events'}:
        require(type(stats[key]) is int and stats[key] >= 0, 'Invalid periodic counter: ' + key)
    require(stats['repair_calls'] == row['iterations'], 'Periodic repair count disagrees with completed iterations')
    require(stats['passes'] == stats['repair_calls'] // PARAMETERS['interval'], 'Periodic interval pass count changed')
    events = stats['events']
    require(isinstance(events, list) and len(events) == stats['passes'], 'Periodic event inventory changed')
    pairs = list(combinations(range(8), 2))
    totals = dict(probes=0, canonical_checks=0, improvements=0)
    total_runtime = 0.0
    timed_out = 0
    for index, event in enumerate(events):
        require(type(event['repair_call']) is int and event['repair_call'] == (index + 1) * PARAMETERS['interval'],
                'Periodic event did not occur at its declared repair interval')
        require(event['route_pair'] == list(pairs[index % len(pairs)])
                and all(type(value) is int for value in event['route_pair']), 'Periodic route-pair rotation changed')
        for key, upper in (('ranking_probes', 2 * 4 * 25), ('shortlisted_candidates', 16), ('probes', 16),
                           ('canonical_checks', 17), ('improvements', 16), ('same_route_skips', 16)):
            require(type(event[key]) is int and 0 <= event[key] <= upper, 'Periodic work bound exceeded: ' + key)
        require(event['shortlisted_candidates'] <= event['ranking_probes']
                and event['probes'] + event['same_route_skips'] <= event['shortlisted_candidates']
                and event['improvements'] <= event['probes']
                and event['canonical_checks'] <= event['probes'] + 1,
                'Periodic work counters are internally inconsistent')
        require(type(event['deadline_reached']) is bool and 'skip_reason' not in event,
                'Official complete DIRECT routes cannot skip a periodic pair')
        budget = finite(event['budget_seconds'], 'periodic call budget')
        runtime = finite(event['runtime_seconds'], 'periodic call runtime')
        require(0 <= budget <= PARAMETERS['pair_budget_seconds'] + 1e-10 and runtime >= 0,
                'Periodic call budget or runtime is invalid')
        stage_seconds = [finite(event[key], 'periodic stage ' + key)
                         for key in ('precheck_seconds', 'ranking_seconds', 'probe_seconds', 'canonical_seconds')]
        require(all(value >= 0 for value in stage_seconds) and sum(stage_seconds) <= runtime + 1e-8,
                'Periodic stage timing exceeds its own measured runtime')
        # A full atomic evaluation can finish after its deadline; source checks
        # prove its result is then vetoed. Small bookkeeping overhead remains.
        require(runtime <= budget + .002 or event['deadline_reached'], 'Unreported periodic deadline overrun')
        before, after = event['before_score'], event['after_score']
        if before is None or after is None:
            require(before is after is None and event['deadline_reached']
                    and event['canonical_checks'] <= 1 and event['probes'] == event['improvements'] == 0
                    and event['ranking_probes'] == event['shortlisted_candidates'] == 0,
                    'Missing periodic checkpoint scores without an input-check timeout')
        else:
            before_key = objective_key(score(before), manifest, 'weighted')
            after_key = objective_key(score(after), manifest, 'weighted')
            require(after_key <= before_key and (after_key < before_key) is (event['improvements'] > 0),
                    'Periodic checkpoint did not strictly improve the fixed weighted objective')
            require(event['canonical_checks'] >= 1 + event['improvements'],
                    'Periodic accepted checkpoints lack canonical checks')
        for key in totals:
            totals[key] += event[key]
        total_runtime += runtime
        timed_out += event['deadline_reached']
    require(all(stats[key] == value for key, value in totals.items()), 'Periodic aggregate counters disagree with events')
    same(stats['runtime_seconds'], total_runtime, 'periodic aggregate runtime')
    require(0 <= finite(stats['runtime_seconds'], 'periodic total runtime') <= row['search_seconds'],
            'Periodic work did not fit inside the recorded search time')
    return dict(passed=True, repair_calls=stats['repair_calls'], passes=stats['passes'], probes=stats['probes'],
                improvements=stats['improvements'], canonical_checks=stats['canonical_checks'],
                runtime_seconds=stats['runtime_seconds'], deadline_stops=timed_out,
                complete_records_match_saved_solution=True, fixed_interval_and_rotation=True,
                bounded_shortlist_and_exact_probes=True, canonical_weighted_checkpoints=True,
                final_ALNS_acceptance_independent_of_periodic_refinement=True,
                intermediate_checkpoint_routes_available=False)


def audit_periodic_sources(output, manifest):
    for name in NEW_SCRIPTS:
        spec = manifest['execution_sources'][name]
        require(sha(within(output, spec['path'])) == spec['sha256'] == sha(ROOT / 'algorithm/experiments' / name),
                'Frozen periodic execution source changed: ' + name)
    return dict(periodic_execution_sources_frozen=True, periodic_parameters_frozen=True)


def audit_results(output):
    output = output.resolve()
    manifest, rows = read(output / 'manifest.json'), read(output / 'runs.json')
    require(isinstance(rows, list) and [row['variant'] for row in rows] == ORDER,
            'Exactly one baseline and one completed periodic candidate are required')
    parent_method, shared_checks = audit_parent_and_shared(output, manifest, rows)
    projected = project_manifest(manifest)
    parent_path = within(ROOT, manifest['parent_manifest_path'])
    require(sha(parent_path) == manifest['parent_manifest_sha256'], 'Original weighted parent manifest changed')
    parent = read(parent_path)
    first.audit_protocol(projected, parent)
    sources = first.source_audit(output, projected, parent)
    sources.update(shared_checks, **audit_periodic_sources(output, manifest))
    input_path = within(ROOT, manifest['input_path'])
    require(sha(input_path) == manifest['input_sha256'], 'Official input fingerprint changed')
    require(sha(within(ROOT, manifest['problem_path'])) == manifest['problem_sha256'], 'Problem fingerprint changed')
    tasks = first.common.load_official_csv(input_path)
    require(digest(manifest['initial_routes']) == manifest['initial_routes_sha256'], 'Initial routes fingerprint changed')
    initial_physical, _, _ = walk(tasks, manifest['initial_routes'])
    for actual, expected in zip(score(manifest['initial_score']), initial_physical, strict=True):
        same(actual, expected, 'independent initial score')
    runs = []
    for row in rows:
        checked = first.audit_run(output, project_row(row), projected, tasks, initial_physical)
        checked['variant'] = row['variant']
        runs.append(checked)
    payload = read(within(output, rows[1]['solution_path']))
    periodic_checks = audit_periodic_statistics(rows[1], payload, manifest)
    baseline, candidate = rows
    comparison = dict(candidate_minus_baseline={field: candidate[field] - baseline[field]
                      for field in (*FIELDS, 'weighted_score', 'on_time_rate', 'end_to_end_seconds')},
                      weighted_objective_improves=objective_key(tuple(candidate[field] for field in FIELDS), manifest, 'weighted')
                      < objective_key(tuple(baseline[field] for field in FIELDS), manifest, 'weighted'),
                      original_lex_objective_improves=tuple(candidate[field] for field in FIELDS)
                      < tuple(baseline[field] for field in FIELDS))
    report = output / 'index.html'
    return dict(schema_version=3, passed=True, run_count=2, manifest_sha256=sha(output / 'manifest.json'),
                runs_sha256=sha(output / 'runs.json'), audited_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                evaluator='Independent CSV + math.hypot DIRECT walk; no production imports',
                source_checks=sources, input_sha256=manifest['input_sha256'],
                initial_routes_sha256=manifest['initial_routes_sha256'],
                independent_initial_score=dict(zip(FIELDS, initial_physical, strict=True)), runs=runs,
                periodic_task_exchange=periodic_checks, comparison=comparison,
                shared_baseline=shared_checks, new_run_count=1 if shared_checks['shared_baseline_reused'] else 2,
                report_sha256=sha(report) if report.exists() else None,
                limitations=['One seed on one complete official instance; no statistical or cross-instance claim.',
                             'The baseline is an explicitly shared earlier run when reused; it is not a new sample.',
                             'Intermediate ALNS and periodic checkpoint routes are unavailable; only complete initial/final routes are recomputed.',
                             'Periodic checkpoint counts describe repair refinement; ordinary ALNS acceptance can reject a refined candidate.',
                             'Elapsed stage times are checked against stored telemetry, not reconstructed from route geometry.',
                             'The candidate combines periodic exchange, terminal exchange and lazy repair; gains do not identify individual causal contributions.'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=DEFAULT_OUTPUT)
    output = parser.parse_args(argv).output_dir.resolve()
    try:
        result = audit_results(output)
    except (ValueError, KeyError, TypeError, OSError, StopIteration, subprocess.CalledProcessError) as error:
        result = dict(schema_version=3, passed=False, error=f'{type(error).__name__}: {error}')
    (output / 'audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps({key: result[key] for key in ('passed', 'run_count', 'error') if key in result}, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
