"""A real tiny archived-runtime pair, canonical receipts and compact proofs."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest

from algorithm.experiments import audit_multi_relay_batch_v1 as audit
from algorithm.tests.test_audit_mobile_relay import canonical_payload

W, S = (.5, .25, .25), (9., 100., 100.)
DIRECT = dict(alns_fraction=.4, critical_limit=2, donor_limit=2, position_limit=4,
              option_count=2, beam_width=2, max_steps=1, snapshot_limit=2)
RELAY = dict(max_groups=2, beam_width=2, max_families=2, source_option_limit=1,
             receiver_option_limit=1, position_limit=3, snapshot_limit=2)
_ARCHIVED_PAIR_CACHE = None


@pytest.fixture(scope='module')
def archived_pair(tmp_path_factory):
    """Only nine jobs; no official input optimizer or timing probe is run."""
    global _ARCHIVED_PAIR_CACHE
    if _ARCHIVED_PAIR_CACHE is not None: return _ARCHIVED_PAIR_CACHE
    folder = tmp_path_factory.mktemp('actual-independent-multi-group-pair')
    ledger = audit.ledger;parent = ledger.read(ledger.PARENT / 'manifest.json')
    archive = ledger.PARENT / parent['source_archive']['path']
    assert ledger.sha(archive) == parent['source_archive']['sha256']
    with ZipFile(archive) as package: package.extractall(folder / 'bundle')
    script = folder / 'tiny.py'
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
w,s=(.5,.25,.25),(9.,100.,100.)
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
    answers[arm]=dict(routes=outcome['routes'],metadata=outcome['metadata'],
        begin=begin,end=end,wall=end-begin)
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
    result = json.loads(result.stdout)
    result['tasks'] = {int(t): value for t, value in result['tasks'].items()}
    result['archive_sha256'] = ledger.sha(archive)
    _ARCHIVED_PAIR_CACHE = result
    return result


def make_batch(path, data, monkeypatch):
    ledger = audit.ledger;tasks, graph, base = data['tasks'], data['graph'], data['base']
    write = lambda p, value: ledger.write(p, value, indent=0)
    checkpoint = path / 'historical_checkpoint.json'
    write(checkpoint, canonical_payload(tasks, {**graph, 'task_relay_candidates': {}}, base, {}))
    schedule = [dict(arm=arm, seed=17, run_id=arm + '__seed_17') for arm in audit.ARMS]
    manifest = dict(schedule=schedule, topology=graph, input_path='tiny-not-official.csv', input_sha256='tiny-data',
        weights=list(W), scales=list(S), budget_seconds=.552, finalization_reserve_seconds=.1,
        purpose='checkpoint_mechanism_diagnostic', homes='stations', artifact_json_indent=0,
        source_archive=dict(sha256=data['archive_sha256']), initial_routes=base,
        initial_routes_sha256=ledger.digest(base), initial_score=audit.reference_schedule(tasks, base, graph)['score'],
        checkpoint=dict(path=str(checkpoint), sha256=ledger.sha(checkpoint)),
        accumulation_fixed_catalogue_parameters=audit.FIXED_CATALOGUE,
        accumulation_direct_periodic_parameters=audit.PERIODIC,
        method_parameters=dict(zip(audit.ARMS, (DIRECT, RELAY))))
    protocol = dict(kind='predeclared_independent_arm_historical_checkpoint_mechanism_diagnostic',
        arms=list(audit.ARMS), seed=17, budget_seconds_per_arm=manifest['budget_seconds'], homes='stations',
        checkpoint_path=str(checkpoint), checkpoint_sha256=ledger.sha(checkpoint), input_sha256='tiny-data',
        weights=list(W), scales=list(S), method_parameters=manifest['method_parameters'],
        fixed_catalogue_parameters=audit.FIXED_CATALOGUE, direct_periodic_parameters=audit.PERIODIC)
    protocol_path = path / 'protocol.json';write(protocol_path, protocol)
    manifest['multi_relay_protocol'] = dict(path=str(protocol_path), sha256=ledger.sha(protocol_path))
    manifest['execution_sources'] = {name: dict(sha256=ledger.sha(ledger.EXPERIMENTS / name)) for name in audit.AUDIT_SOURCES}
    write(path / 'manifest.json', manifest);rows = []
    for cell in schedule:
        arm = cell['arm'];outcome = deepcopy(data['answers'][arm]);relay = arm.endswith('_relay')
        actual = {**graph, 'relay_allowed': relay}
        serialize = actual if relay else {**actual, 'task_relay_candidates': {}}
        phases = [];previous = ledger.digest(base)
        for name, routes, metadata in [('initial', base, {}), (arm, outcome['routes'], outcome['metadata']), ('final', outcome['routes'], {})]:
            filename = 'phase_solutions/' + arm + '__' + name + '.json'
            payload = canonical_payload(tasks, serialize, routes, {});write(path / filename, payload)
            phase = dict(phase=name, solution_path=filename, solution_sha256=ledger.sha(path / filename),
                topology=graph, routes_sha256=ledger.digest(routes), input_routes_sha256=previous,
                score=payload['score'], weighted_score=audit._key(payload['score'], manifest)[0], metadata=metadata)
            if name == arm:
                phase.update(started_at_perf_counter=outcome['begin'], ended_at_perf_counter=outcome['end'],
                    solve_wall_seconds=outcome['wall'], start_offset_seconds=.002)
            phases.append(phase);previous = ledger.digest(routes)
        cf = audit.direct_counterfactual(outcome['routes'], actual);cf_path = 'counterfactuals/' + arm + '.json'
        write(path / cf_path, canonical_payload(tasks, {**graph, 'task_relay_candidates': {}}, cf, {}))
        score = phases[-1]['score']
        row = dict(**cell, phases=phases, valid=True, budget_compliant=True,
            end_to_end_seconds=.002 + outcome['wall'] + .01, construction_seconds=.001,
            budget_seconds=manifest['budget_seconds'], purpose=manifest['purpose'],
            source_sha256=manifest['source_archive']['sha256'], initial_routes_sha256=manifest['initial_routes_sha256'],
            solution_path=phases[-1]['solution_path'], solution_sha256=phases[-1]['solution_sha256'], **score,
            weighted_score=audit._key(score, manifest)[0], on_time_count=len(tasks) - score['late_count'],
            counterfactual_path=cf_path, counterfactual_sha256=ledger.sha(path / cf_path))
        response = path / 'receipts' / (cell['run_id'] + '__response.json');write(response, row)
        write(path / 'receipts' / (cell['run_id'] + '.json'), dict(status='completed',
            manifest_sha256=ledger.sha(path / 'manifest.json'), response_sha256=ledger.sha(response)))
        rows.append(row)
    write(path / 'runs.json', rows)
    # Parent archive/input receipts are covered by the immutable ledger. The
    # small test supplies its own canonical physical input and real workers.
    monkeypatch.setattr(ledger, 'verify', lambda *args, **kwargs: None)
    monkeypatch.setattr(audit, 'load_official_csv', lambda _: tasks)
    return rows, manifest


def test_real_independent_pair_full_phases_compact_catalogue_and_CF(tmp_path, archived_pair, monkeypatch):
    rows, manifest = make_batch(tmp_path, archived_pair, monkeypatch)
    result = audit.audit(tmp_path)
    assert result['passed'] and len(result['results']) == 2
    comparison = result['paired_checkpoint_comparisons'][0]
    assert comparison['DIRECT_has_independent_paid_native_search'] and not comparison['shared_raw_directory']
    assert comparison['historical_checkpoint_diagnostic_only'] and not comparison['strongest_fresh_baseline']
    direct, relay = result['results']
    assert direct['method_evidence']['independent_method_evidence']['actual_ALNS_reference'] is not None
    assert relay['method_evidence']['independent_method_evidence']['catalogue_references']
    assert all(len(r['phase_references']) == 3 for r in result['results'])
    with pytest.raises(AssertionError, match='Never overwrite'): audit.audit(tmp_path)


@pytest.mark.parametrize('fault', ['config', 'catalogue_fixed', 'deadline', 'phase_end', 'old_verification', 'invented_guard'])
def test_paid_method_identity_clock_and_original_guard_cannot_be_forged(archived_pair, fault):
    arm = 'accumulation_relay';out = deepcopy(archived_pair['answers'][arm]);meta = out['metadata']
    manifest = dict(weights=list(W), scales=list(S), budget_seconds=.552, finalization_reserve_seconds=.1,
        accumulation_fixed_catalogue_parameters=deepcopy(audit.FIXED_CATALOGUE),
        method_parameters={arm: RELAY})
    phase = dict(started_at_perf_counter=out['begin'], ended_at_perf_counter=out['end'],
        start_offset_seconds=.002, solve_wall_seconds=out['wall'])
    if fault == 'config': meta['configuration']['max_groups'] += 1
    elif fault == 'catalogue_fixed': meta['steps'][0]['parent_records'][0]['catalogue']['metadata']['configuration']['base_pool_limit'] += 1
    elif fault == 'deadline': manifest['budget_seconds'] += 1.
    elif fault == 'phase_end': phase['ended_at_perf_counter'] += 1.
    elif fault == 'old_verification': meta['input_snapshot']['full_verified_at_perf_counter'] = out['begin'] - 1.
    else: meta['guard_archive']['routes'][0] = []
    with pytest.raises((AssertionError, ValueError, KeyError)):
        audit._audit_metadata(archived_pair['tasks'], archived_pair['graph'], archived_pair['base'], meta,
            relay=True, manifest=manifest, phase=phase)


def test_response_receipt_cannot_hide_modified_saved_runs(tmp_path, archived_pair, monkeypatch):
    rows, _ = make_batch(tmp_path, archived_pair, monkeypatch)
    rows[0]['end_to_end_seconds'] += .001
    audit.ledger.write(tmp_path / 'runs.json', rows)
    with pytest.raises(AssertionError): audit.audit(tmp_path)


def test_counterfactual_must_be_exact_route_transform(tmp_path, archived_pair, monkeypatch):
    rows, _ = make_batch(tmp_path, archived_pair, monkeypatch)
    row = rows[-1];path = tmp_path / row['counterfactual_path'];payload = audit.ledger.read(path)
    payload['score']['distance_km'] += 1.
    audit.ledger.write(path, payload);row['counterfactual_sha256'] = audit.ledger.sha(path)
    audit.ledger.write(tmp_path / 'runs.json', rows)
    response = tmp_path / 'receipts' / (row['run_id'] + '__response.json');audit.ledger.write(response, row)
    receipt_path = tmp_path / 'receipts' / (row['run_id'] + '.json');receipt = audit.ledger.read(receipt_path)
    receipt['response_sha256'] = audit.ledger.sha(response);audit.ledger.write(receipt_path, receipt)
    with pytest.raises(ValueError): audit.audit(tmp_path)
