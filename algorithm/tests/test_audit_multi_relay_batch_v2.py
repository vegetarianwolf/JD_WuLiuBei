"""Post-run scale representation fix; all frozen physical checks stay strict."""
import json
import os
import subprocess
import sys
from zipfile import ZipFile

import pytest

from algorithm.experiments import audit_multi_relay_batch_v2 as audit
from algorithm.tests.test_audit_multi_relay_batch_v1 import make_batch


@pytest.fixture(scope='module')
def archived_pair(tmp_path_factory):
    """The actual tiny runtime receives integer scales, as official037 did."""
    folder = tmp_path_factory.mktemp('actual-integer-scale-independent-pair')
    ledger = audit.ledger;parent = ledger.read(ledger.PARENT / 'manifest.json')
    archive = ledger.PARENT / parent['source_archive']['path']
    assert ledger.sha(archive) == parent['source_archive']['sha256']
    with ZipFile(archive) as package: package.extractall(folder / 'bundle')
    script = folder / 'tiny_integer_scales.py'
    script.write_text('''
import json
from time import perf_counter
from uav_dispatch import Point,Problem,RelayStation,Task,TransportLeg
import weighted_direct_objective as weighted
from uav_dispatch.model import Score
import multi_relay_direct_control_v1 as direct
import multi_relay_accumulation_v1 as relay
left,right,hub=Point(-4.,0.),Point(4.,0.),Point(0.,0.)
tasks=(Task(1,left,Point(0.,3.),9.),Task(2,right,Point(0.,4.),9.),
    *(Task(t,left,left,100.) for t in (3,4,5)),*(Task(t,right,right,100.) for t in (6,7,8)),Task(9,hub,hub,100.))
registry={}
for t in tasks:
    x=10+2*(t.id-1);registry[x]=TransportLeg(x,t.id,'RELAY_IN',1);registry[x+1]=TransportLeg(x+1,t.id,'RELAY_OUT',1)
p=Problem(tasks,drone_count=3,capacity=2,max_tasks_per_drone=3,speed_km_per_min=.9,
    drone_homes=(left,right,hub),relay_stations=(RelayStation(1,0.,0.),),leg_registry=registry,
    task_relay_candidates={t.id:(1,) for t in tasks})
base=((3,-3,4,-4,5,-5),(6,-6,7,-7,8,-8),(9,-9,1,-1,2,-2))
w,s=(.5,.25,.25),(9,100.,100.)
parameters={
 'accumulation_direct':dict(alns_fraction=.4,critical_limit=2,donor_limit=2,position_limit=4,option_count=2,beam_width=2,max_steps=1,snapshot_limit=2),
 'accumulation_relay':dict(max_groups=2,beam_width=2,max_families=2,source_option_limit=1,receiver_option_limit=1,position_limit=3,snapshot_limit=2)}
answers={}
for arm,module in (('accumulation_direct',direct),('accumulation_relay',relay)):
    begin=perf_counter();deadline=begin+.45
    with weighted.objective(Score,s,w):
        outcome=module.refine(p,base,deadline=deadline,weights=w,scales=s,seed=17,
            relay_enabled=arm.endswith('_relay'),**parameters[arm])
    end=perf_counter()
    answers[arm]=dict(routes=outcome['routes'],metadata=outcome['metadata'],begin=begin,end=end,wall=end-begin)
assert not weighted.active and weighted.coefficients is None
graph=dict(home_points=[[-4.,0.],[4.,0.],[0.,0.]],stations=[[1,0.,0.]],
    legs=[dict(id=x,task_id=l.task_id,kind=l.kind,relay_id=l.relay_id) for x,l in registry.items()],
    drone_count=3,max_tasks_per_drone=3,capacity=2,speed_km_per_min=.9,
    task_relay_candidates={str(t.id):[1] for t in tasks})
print(json.dumps(dict(answers=answers,base=base,graph=graph,
    tasks={str(t.id):[[t.pickup.x,t.pickup.y],[t.delivery.x,t.delivery.y],t.deadline_min] for t in tasks}),allow_nan=False))
''')
    env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': os.pathsep.join(
        (str(folder / 'bundle'), str(ledger.EXPERIMENTS), str(ledger.ROOT)))}
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                            env=env, cwd=ledger.ROOT, timeout=10)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout);data['tasks'] = {int(t): value for t, value in data['tasks'].items()}
    data['archive_sha256'] = ledger.sha(archive)
    return data


def prepare(path, data, monkeypatch, *, mixed=False):
    rows, manifest = make_batch(path, data, monkeypatch)
    monkeypatch.setattr(audit, 'load_official_csv', lambda _: data['tasks'])
    if mixed:
        manifest['scales'][0] = int(manifest['scales'][0])
        audit.ledger.write(path / 'manifest.json', manifest, indent=0)
        for row in rows:
            receipt_path = path / 'receipts' / (row['run_id'] + '.json')
            receipt = audit.ledger.read(receipt_path)
            receipt['manifest_sha256'] = audit.ledger.sha(path / 'manifest.json')
            audit.ledger.write(receipt_path, receipt, indent=0)
    return rows, manifest


def save_real_failure(path, manifest, error):
    protocol = audit.ledger.read(audit.ledger.ROOT / manifest['multi_relay_protocol']['path'])
    audit.ledger.write(path / 'audit_attempt_001.json', dict(status='terminal_failed_before_method_replays',
        returncode=1, error_type=type(error).__name__, error_message=str(error),
        original_batch_auditor_sha256=audit.ORIGINAL_FROZEN_AUDIT_SHA256,
        manifest_sha256=audit.ledger.sha(path / 'manifest.json'), runs_sha256=audit.ledger.sha(path / 'runs.json'),
        protocol_sha256=manifest['multi_relay_protocol']['sha256'], manifest_scales=manifest['scales'],
        protocol_scales=protocol['scales'], all_scale_values_exactly_equal=True,
        source_and_protocol_hashes_unchanged=True, saved_solution_fields_changed=False,
        optimizer_rerun=False, no_completed_audit_json_written=True))


def refresh_run(path, rows, row):
    audit.ledger.write(path / 'runs.json', rows)
    response = path / 'receipts' / (row['run_id'] + '__response.json');audit.ledger.write(response, row)
    receipt_path = path / 'receipts' / (row['run_id'] + '.json');receipt = audit.ledger.read(receipt_path)
    receipt['response_sha256'] = audit.ledger.sha(response);audit.ledger.write(receipt_path, receipt)


def test_actual_pair_original_failure_then_numeric_revision_and_provenance(tmp_path, archived_pair, monkeypatch):
    rows, manifest = prepare(tmp_path, archived_pair, monkeypatch, mixed=True)
    with pytest.raises(ValueError, match=r'protocol scales\[0\] integer differs') as failure:
        audit.frozen.audit(tmp_path)
    assert not (tmp_path / 'audit.json').exists()
    save_real_failure(tmp_path, manifest, failure.value)
    original_manifest = audit.ledger.sha(tmp_path / 'manifest.json');original_runs = audit.ledger.sha(tmp_path / 'runs.json')
    result = audit.audit(tmp_path)
    assert result['passed'] and len(result['results']) == 2
    revision = result['audit_revision']
    assert revision['kind'] == 'post_run_independent_audit_revision'
    assert revision['original_frozen_audit_passed'] is False
    assert revision['revised_audit_was_frozen_before_optimizer_execution'] is False
    assert revision['optimizer_protocol_manifest_results_changed'] is False
    assert revision['original_frozen_audit_source_sha256'] == audit.ORIGINAL_FROZEN_AUDIT_SHA256
    assert revision['revised_audit_source_sha256'] == audit.ledger.sha(audit.__file__)
    assert revision['original_failed_attempt_evidence']['receipt_sha256'] == audit.ledger.sha(tmp_path / 'audit_attempt_001.json')
    assert audit.ledger.sha(tmp_path / 'manifest.json') == original_manifest
    assert audit.ledger.sha(tmp_path / 'runs.json') == original_runs
    assert audit._audit_metadata is audit.frozen._audit_metadata
    with pytest.raises(AssertionError, match='Never overwrite'): audit.audit(tmp_path)


def test_same_representation_retains_identical_frozen_complete_proofs(tmp_path, archived_pair, monkeypatch):
    import shutil
    old, new = tmp_path / 'original', tmp_path / 'revised'
    prepare(old, archived_pair, monkeypatch)
    shutil.copytree(old, new)
    original = audit.frozen.audit(old)
    revised = audit.audit(new)
    revision = revised.pop('audit_revision')
    assert revision['original_frozen_audit_passed'] is None  # No failure receipt is invented.
    assert revised == original


@pytest.mark.parametrize('actual,expected', [([9.], [9]), ([9], [9.])])
def test_exact_int_float_numeric_equality_is_allowed(actual, expected):
    audit._numeric_scales(actual, expected)


@pytest.mark.parametrize('actual,expected', [([9.000000000000002], [9]), ([True], [1.]),
    ([1.], [True]), ([float('nan')], [9.]), ([float('inf')], [9.]), ([], [9.]), ([9.], [9., 100.])])
def test_different_values_bool_nonfinite_and_length_stay_rejected(actual, expected):
    with pytest.raises(AssertionError): audit._numeric_scales(actual, expected)


@pytest.mark.parametrize('fault', ['configuration', 'verification_clock', 'counterfactual', 'response_receipt'])
def test_numeric_revision_preserves_other_frozen_strict_rejections(tmp_path, archived_pair, monkeypatch, fault):
    rows, manifest = prepare(tmp_path, archived_pair, monkeypatch, mixed=True)
    row = rows[-1];phase = row['phases'][1]
    if fault == 'configuration': phase['metadata']['configuration']['beam_width'] += 1
    elif fault == 'verification_clock':
        phase['metadata']['input_snapshot']['full_verified_at_perf_counter'] = phase['started_at_perf_counter'] - 1.
    elif fault == 'counterfactual':
        path = tmp_path / row['counterfactual_path'];payload = audit.ledger.read(path)
        payload['score']['distance_km'] += 1.
        audit.ledger.write(path, payload);row['counterfactual_sha256'] = audit.ledger.sha(path)
    else:
        row['end_to_end_seconds'] += .001
        audit.ledger.write(tmp_path / 'runs.json', rows)
    if fault != 'response_receipt': refresh_run(tmp_path, rows, row)
    with pytest.raises((AssertionError, ValueError)): audit.audit(tmp_path)
