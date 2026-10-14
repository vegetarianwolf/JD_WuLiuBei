"""Saved batch orchestration must bind own-arm seeds and complete clocks."""
from copy import deepcopy
from time import perf_counter

import pytest

from algorithm.experiments import audit_window_damage_batch as audit
from algorithm.experiments import window_damage_repair as method
from algorithm.tests.test_deadline_window_repooling import fixture, W, S
from algorithm.tests.test_relay_repooling_exchange import topology
from algorithm.tests.test_audit_mobile_relay import canonical_payload


@pytest.fixture(scope='module')
def tiny_results():
    problem, base = fixture()
    tasks = {t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    graph = topology(problem)
    graph['task_relay_candidates'] = {str(t):list(problem.task_relay_candidates.get(t,())) for t in problem.task_ids}
    results = {}
    for arm in audit.ledger.DAMAGE_PAIR:
        results[arm] = method.refine(problem,base,deadline=perf_counter()+2.,weights=W,scales=S,
            relay_enabled=arm.endswith('_relay'),closure_limit=6,beam_width=2,position_limit=8,seed=17)
    return tasks,graph,[list(r) for r in base],results


def make_batch(path,tiny,monkeypatch):
    tasks,graph,base,results = tiny
    ledger = audit.ledger
    write = ledger.write
    schedule = [dict(arm=arm,seed=17,run_id=arm+'__seed_17') for arm in ledger.DAMAGE_PAIR]
    manifest = dict(schedule=schedule,topology=graph,input_path='tiny-only.csv',weights=list(W),scales=list(S),
        budget_seconds=5.,source_archive=dict(sha256='tiny-archive-not-executed'),initial_routes=base,
        initial_routes_sha256=ledger.digest(base),initial_score=audit.reference_schedule(tasks,base,graph)['score'])
    manifest['execution_sources'] = {name:dict(sha256=ledger.sha(ledger.EXPERIMENTS/name)) for name in
        ('audit_window_damage_batch.py','audit_window_damage_repair.py','audit_deadline_window_repooling.py',
         'audit_weighted_relay_ablation.py','audit_weighted_direct_pair.py')}
    write(path/'manifest.json',manifest)
    rows = []
    for cell in schedule:
        arm = cell['arm'];out = {**results[arm],'metadata':deepcopy(results[arm]['metadata'])};relay = arm.endswith('_relay')
        actual = {**graph,'relay_allowed':relay}
        serialize = actual if relay else {**actual,'task_relay_candidates':{}}
        phases=[];previous=ledger.digest(base)
        for phase,routes,metadata in [('initial',base,{}),(arm,out['routes'],out['metadata']),('final',out['routes'],{})]:
            name='phase_solutions/'+arm+'__'+phase+'.json'
            payload=canonical_payload(tasks,serialize,routes,{})
            write(path/name,payload)
            phases.append(dict(phase=phase,solution_path=name,solution_sha256=ledger.sha(path/name),
                topology=graph,score=payload['score'],weighted_score=audit._key(payload['score'],manifest)[0],
                routes_sha256=ledger.digest(routes),input_routes_sha256=previous,metadata=metadata))
            previous=ledger.digest(routes)
        cf_routes=audit.direct_counterfactual(out['routes'],actual)
        cf_path='counterfactuals/'+arm+'.json'
        write(path/cf_path,canonical_payload(tasks,{**graph,'task_relay_candidates':{}},cf_routes,{}))
        score=phases[-1]['score']
        row=dict(**cell,phases=phases,valid=True,budget_compliant=True,end_to_end_seconds=out['metadata']['runtime_seconds'],
            source_sha256=manifest['source_archive']['sha256'],initial_routes_sha256=manifest['initial_routes_sha256'],
            solution_path=phases[-1]['solution_path'],solution_sha256=phases[-1]['solution_sha256'],
            counterfactual_path=cf_path,counterfactual_sha256=ledger.sha(path/cf_path),**score,
            weighted_score=audit._key(score,manifest)[0],on_time_count=len(tasks)-score['late_count'])
        response=path/'receipts'/(cell['run_id']+'__response.json');write(response,row)
        write(path/'receipts'/(cell['run_id']+'.json'),dict(status='completed',manifest_sha256=ledger.sha(path/'manifest.json'),
            response_sha256=ledger.sha(response)))
        rows.append(row)
    write(path/'runs.json',rows)
    monkeypatch.setattr(ledger,'verify',lambda *args,**kwargs:None)
    monkeypatch.setattr(audit,'load_official_csv',lambda _:tasks)
    return rows


def test_real_tiny_paired_results_replay_all_raw_and_post_evidence(tmp_path,tiny_results,monkeypatch):
    rows=make_batch(tmp_path,tiny_results,monkeypatch)
    result=audit.audit(tmp_path)
    assert result['passed'] and len(result['results'])==2
    assert result['selector_comparisons'][0]['same_selected_families']
    assert all(r['raw_stage']['unique_raw_snapshots']>0 for r in result['results'])
    for row,reference in zip(rows,result['results']):
        assert [p['score'] for p in row['phases']]==[p['reference']['score'] for p in reference['phase_references']]
    relay=result['results'][1]
    assert any(r['proposal']=='damage_closure' for r in relay['post_references'])
    assert any(r['proposal']=='joint_beam_partial' for r in relay['post_references'])
    assert any(r['proposal']=='complete_joint_checkpoint' for r in relay['post_references'])


@pytest.mark.parametrize('tamper',['omitted_direct_map','helper_budget'])
def test_saved_search_claims_cannot_silently_omit_maps_or_extend_helper_budget(tmp_path,tiny_results,monkeypatch,tamper):
    rows=make_batch(tmp_path,tiny_results,monkeypatch)
    row=rows[0 if tamper=='omitted_direct_map' else 1]
    metadata=next(p['metadata'] for p in row['phases'] if p['phase']==row['arm'])
    if tamper=='omitted_direct_map':
        record=metadata['raw_stage']['family_records'][0]
        record['direct_maps_completed']=record['direct_maps_completed'][:-1]
    else:
        record=metadata['polish_records'][0]
        record['helper_metadata']['deadline_perf_counter']=record['leaf_deadline_perf_counter']+1.
    audit.ledger.write(tmp_path/'runs.json',rows)
    response=tmp_path/'receipts'/(row['run_id']+'__response.json');audit.ledger.write(response,row)
    receipt=tmp_path/'receipts'/(row['run_id']+'.json');value=audit.ledger.read(receipt)
    value['response_sha256']=audit.ledger.sha(response);audit.ledger.write(receipt,value)
    with pytest.raises(AssertionError):audit.audit(tmp_path)
