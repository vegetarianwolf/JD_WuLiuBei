"""Whole saved tiny pair with independently checked conditional exchanges."""
from copy import deepcopy
import pytest
from algorithm.experiments import audit_conditional_donor_batch_v1 as audit
from algorithm.tests.test_conditional_donor_refinement_v1 import tiny,W,S
from algorithm.tests.test_audit_mobile_relay import canonical_payload

def make_batch(path,tiny,monkeypatch):
    p,base,graph,tasks,outcomes=tiny;ledger=audit.ledger
    results={arm:outcomes[arm.endswith("_relay")] for arm in ledger.CONDITIONAL_DONOR_PAIR}
    write=lambda path,value:ledger.write(path,value,indent=0)
    schedule=[dict(arm=arm,seed=17,run_id=arm+'__seed_17') for arm in ledger.CONDITIONAL_DONOR_PAIR]
    manifest=dict(schedule=schedule,topology=graph,input_path='tiny-only.csv',weights=list(W),scales=list(S),
        budget_seconds=5.,artifact_json_indent=0,source_archive=dict(sha256='tiny-archive-not-executed'),initial_routes=base,
        initial_routes_sha256=ledger.digest(base),initial_score=audit.reference_schedule(tasks,base,graph)['score'])
    manifest['execution_sources']={name:dict(sha256=ledger.sha(ledger.EXPERIMENTS/name)) for name in
        ('audit_split_feeder.py','audit_split_feeder_v2.py','audit_split_feeder_v3.py','audit_split_feeder_v4.py','audit_split_feeder_batch_v4.py','audit_deadline_window_repooling.py','audit_repooling.py','audit_window_damage_repair.py','audit_weighted_relay_ablation.py','audit_weighted_direct_pair.py','audit_critical_prefix_joint_repair_v1.py','audit_critical_prefix_batch_v1.py','audit_conditional_donor_repair_v1.py','audit_conditional_donor_batch_v1.py')}
    write(path/'manifest.json',manifest);rows=[]
    for cell in schedule:
        arm=cell['arm'];out={**results[arm],'metadata':deepcopy(results[arm]['metadata'])};relay=arm.endswith('_relay')
        actual={**graph,'relay_allowed':relay};serialize=actual if relay else {**actual,'task_relay_candidates':{}}
        phases=[];previous=ledger.digest(base)
        for phase,routes,metadata in [('initial',base,{}),(arm,out['routes'],out['metadata']),('final',out['routes'],{})]:
            name='phase_solutions/'+arm+'__'+phase+'.json';payload=canonical_payload(tasks,serialize,routes,{})
            write(path/name,payload)
            phases.append(dict(phase=phase,solution_path=name,solution_sha256=ledger.sha(path/name),topology=graph,
                score=payload['score'],weighted_score=audit._key(payload['score'],manifest)[0],routes_sha256=ledger.digest(routes),
                input_routes_sha256=previous,metadata=metadata));previous=ledger.digest(routes)
        cf_routes=audit.direct_counterfactual(out['routes'],actual);cf_path='counterfactuals/'+arm+'.json'
        write(path/cf_path,canonical_payload(tasks,{**graph,'task_relay_candidates':{}},cf_routes,{}))
        score=phases[-1]['score']
        row=dict(**cell,phases=phases,valid=True,budget_compliant=True,end_to_end_seconds=out['metadata']['runtime_seconds'],
            source_sha256=manifest['source_archive']['sha256'],initial_routes_sha256=manifest['initial_routes_sha256'],
            solution_path=phases[-1]['solution_path'],solution_sha256=phases[-1]['solution_sha256'],**score,
            counterfactual_path=cf_path,counterfactual_sha256=ledger.sha(path/cf_path),
            weighted_score=audit._key(score,manifest)[0],on_time_count=len(tasks)-score['late_count'])
        response=path/'receipts'/(cell['run_id']+'__response.json');write(response,row)
        write(path/'receipts'/(cell['run_id']+'.json'),dict(status='completed',manifest_sha256=ledger.sha(path/'manifest.json'),response_sha256=ledger.sha(response)))
        rows.append(row)
    write(path/'runs.json',rows)
    monkeypatch.setattr(ledger,'verify',lambda *a,**k:None)
    monkeypatch.setattr(audit,'load_official_csv',lambda _:tasks)
    return rows


def test_real_saved_pair_replays_complete_exchange_and_strong_original_guard(tmp_path,tiny,monkeypatch):
    rows=make_batch(tmp_path,tiny,monkeypatch);result=audit.audit(tmp_path)
    assert result['passed'] and len(result['results'])==2
    assert result['selector_comparisons'][0]['same_selected_families']
    assert not result['selector_comparisons'][0]['strongest_fresh_baseline']
    for r in result['results']:
        assert r['method_evidence']['raw_method_evidence']['proposal_references']
        assert r['method_evidence']['repair_evidence']
    with pytest.raises(AssertionError,match='Never overwrite'):audit.audit(tmp_path)


@pytest.mark.parametrize('tamper',['seed_digest','invented_separation','raw_configuration','late_adoption','original_clock'])
def test_wrapper_provenance_or_predeadline_corruption_cannot_pass(tiny,tamper):
    p,base,graph,tasks,results=tiny;relay=True;meta=deepcopy(results[True]['metadata'])
    if tamper=='seed_digest':meta['seed_records'][0]['routes_sha256']='0'*64
    elif tamper=='invented_separation':meta['seed_records'][0]['genuinely_separated_receiver']=not meta['seed_records'][0]['genuinely_separated_receiver']
    elif tamper=='raw_configuration':meta['configuration']['raw_parameters']['sigma']+=1
    elif tamper=='late_adoption':
        assert meta['accepted_moves'];meta['accepted_moves'][0]['accepted_at_perf_counter']=meta['deadline_perf_counter']+1
    else:meta['input_snapshot']['delivery_times_min']['1']+=1
    with pytest.raises((AssertionError,ValueError,KeyError)):
        audit._audit_metadata(tasks,graph,[list(r) for r in base],meta,relay=relay,
            manifest=dict(weights=list(W),scales=list(S)))
