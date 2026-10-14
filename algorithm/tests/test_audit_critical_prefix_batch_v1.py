"""Real saved tiny pair and adversarial evidence checks for dynamic repair."""
from copy import deepcopy
import pytest
from algorithm.experiments import audit_critical_prefix_batch_v1 as audit
from algorithm.tests.test_critical_prefix_refinement_v1 import tiny,W,S
from algorithm.tests.test_audit_mobile_relay import canonical_payload

def make_batch(path,tiny,monkeypatch):
    p,base,graph,tasks,outcomes=tiny;ledger=audit.ledger
    results={arm:outcomes[arm.endswith("_relay")] for arm in ledger.CRITICAL_PREFIX_PAIR}
    write=lambda path,value:ledger.write(path,value,indent=0)
    schedule=[dict(arm=arm,seed=17,run_id=arm+'__seed_17') for arm in ledger.CRITICAL_PREFIX_PAIR]
    manifest=dict(schedule=schedule,topology=graph,input_path='tiny-only.csv',weights=list(W),scales=list(S),
        budget_seconds=5.,artifact_json_indent=0,source_archive=dict(sha256='tiny-archive-not-executed'),initial_routes=base,
        initial_routes_sha256=ledger.digest(base),initial_score=audit.reference_schedule(tasks,base,graph)['score'])
    manifest['execution_sources']={name:dict(sha256=ledger.sha(ledger.EXPERIMENTS/name)) for name in
        ('audit_split_feeder.py','audit_split_feeder_v2.py','audit_split_feeder_v3.py','audit_split_feeder_v4.py','audit_split_feeder_batch_v4.py','audit_deadline_window_repooling.py','audit_repooling.py','audit_window_damage_repair.py','audit_weighted_relay_ablation.py','audit_weighted_direct_pair.py','audit_critical_prefix_joint_repair_v1.py','audit_critical_prefix_batch_v1.py')}
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


def test_saved_real_pair_replays_raw_seeds_closures_joint_edges_and_guards(tmp_path,tiny,monkeypatch):
    rows=make_batch(tmp_path,tiny,monkeypatch);result=audit.audit(tmp_path)
    assert result['passed'] and len(result['results'])==2
    assert result['selector_comparisons'][0]['same_selected_families']
    assert result['selector_comparisons'][0]['strongest_fresh_baseline'] is False
    for ref in result['results']:
        assert ref['method_evidence']['raw_method_evidence']['proposal_references']
        assert ref['method_evidence']['repair_evidence']
        assert sum(p['partial_states_replayed'] for p in ref['method_evidence']['repair_evidence'])>0
    with pytest.raises(AssertionError,match='Never overwrite'):audit.audit(tmp_path)


@pytest.mark.parametrize('tamper',['seed_digest','invented_separation','direct_pins','different_raw_config',
    'partial_quota','partial_clock','pair_owner','late_checkpoint','late_adoption','false_original_guard'])
def test_corrupted_physical_or_provenance_evidence_is_rejected(tiny,tamper):
    p,base,graph,tasks,outcomes=tiny;relay=tamper in ('invented_separation','late_adoption')
    meta=deepcopy(outcomes[relay]['metadata']);manifest=dict(weights=list(W),scales=list(S))
    if tamper=='seed_digest':meta['seed_records'][0]['routes_sha256']='0'*64
    elif tamper=='invented_separation':meta['seed_records'][0]['genuinely_separated_receiver']=not meta['seed_records'][0]['genuinely_separated_receiver']
    elif tamper=='direct_pins':meta['repair_records'][0]['metadata']['pinned_task_ids']=[1,2]
    elif tamper=='different_raw_config':meta['configuration']['raw_parameters']['sigma']+=1.
    elif tamper=='late_adoption':
        assert meta['accepted_moves'];meta['accepted_moves'][0]['accepted_at_perf_counter']=meta['deadline_perf_counter']+1.
    elif tamper=='false_original_guard':meta['input_snapshot']['delivery_times_min']['1']+=1.
    else:
        records=[r for repair in meta['repair_records'] for r in repair['metadata']['closure_records']]
        assert records;record=records[0]
        if tamper=='partial_quota':record['initial_state']['remaining_quota_slots'][0]+=1
        elif tamper=='partial_clock':record['initial_state']['event_times_min'][0][0]+=1.
        elif tamper=='pair_owner':
            states=[s for layer in record['layers'] for s in layer['retained_states']]
            assert states;states[0]['owner_choices'][0]=(states[0]['owner_choices'][0]+1)%p.drone_count
        else:
            snapshots=meta['repair_records'][0]['metadata']['full_candidate_snapshots'];assert snapshots
            snapshots[0]['full_verified_at_perf_counter']=meta['repair_records'][0]['deadline_perf_counter']+1.
    with pytest.raises((ValueError,AssertionError,KeyError)):
        audit._audit_metadata(tasks,{**graph,'relay_allowed':relay},[list(r) for r in base],meta,relay=relay,manifest=manifest)


def test_expired_raw_keeps_only_actual_original_complete_guard(tiny):
    from time import perf_counter
    from algorithm.experiments import critical_prefix_refinement_v1 as method
    p,base,graph,tasks,_=tiny
    out=method.refine(p,base,deadline=perf_counter()-1.,weights=W,scales=S,relay_enabled=True)
    proof=audit._audit_metadata(tasks,graph,[list(r) for r in base],out['metadata'],relay=True,
        manifest=dict(weights=list(W),scales=list(S)))
    assert proof['raw_method_evidence'] is None and not proof['repair_evidence']
