"""Whole saved-batch replay of a real quota-full split-feeder tiny witness."""
from copy import deepcopy
from time import perf_counter

import pytest

from uav_dispatch import Point,Problem,RelayStation,Task,TransportLeg
from algorithm.experiments import audit_split_feeder_batch_v4 as audit
from algorithm.experiments import split_feeder_refinement_v4 as method
from algorithm.tests.test_relay_repooling_exchange import topology
from algorithm.tests.test_audit_mobile_relay import canonical_payload

W,S=(.5,.25,.25),(9.,100.,100.)


@pytest.fixture(scope='module')
def tiny_results():
    left,right,hub=Point(-4.,0.),Point(4.,0.),Point(0.,0.)
    tasks=(Task(1,left,Point(0.,3.),9.),Task(2,right,Point(0.,4.),9.),
        *(Task(t,left,left,100.) for t in (3,4,5)),
        *(Task(t,right,right,100.) for t in (6,7,8)),Task(9,hub,hub,100.))
    registry={identifier:TransportLeg(identifier,t.id,kind,1) for t in tasks
        for identifier,kind in ((100+2*t.id,'RELAY_IN'),(101+2*t.id,'RELAY_OUT'))}
    p=Problem(tasks,drone_count=3,capacity=2,max_tasks_per_drone=3,speed_km_per_min=.9,
        drone_homes=(left,right,hub),relay_stations=(RelayStation(1,0.,0.),),leg_registry=registry,
        task_relay_candidates={t.id:(1,) for t in tasks})
    base=((3,-3,4,-4,5,-5),(6,-6,7,-7,8,-8),(9,-9,1,-1,2,-2))
    task_rows={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in tasks}
    graph=topology(p);graph['task_relay_candidates']={str(t.id):[1] for t in tasks}
    results={}
    for arm in audit.ledger.SEPARATED_DELIVERY_PAIR:
        results[arm]=method.refine(p,base,deadline=perf_counter()+3.,weights=W,scales=S,
            seed=19,relay_enabled=arm.endswith('_relay'),max_families=8,base_pool_limit=24)
    expired={arm:method.refine(p,base,deadline=perf_counter()-1.,weights=W,scales=S,
        seed=19,relay_enabled=arm.endswith('_relay'),max_families=8,base_pool_limit=24)
        for arm in audit.ledger.SEPARATED_DELIVERY_PAIR}
    return task_rows,graph,[list(r) for r in base],results,expired


def make_batch(path,tiny,monkeypatch):
    tasks,graph,base,results=tiny[:4];ledger=audit.ledger
    write=lambda path,value:ledger.write(path,value,indent=0)
    schedule=[dict(arm=arm,seed=19,run_id=arm+'__seed_19') for arm in ledger.SEPARATED_DELIVERY_PAIR]
    manifest=dict(schedule=schedule,topology=graph,input_path='tiny-only.csv',weights=list(W),scales=list(S),
        budget_seconds=5.,artifact_json_indent=0,source_archive=dict(sha256='tiny-archive-not-executed'),initial_routes=base,
        initial_routes_sha256=ledger.digest(base),initial_score=audit.reference_schedule(tasks,base,graph)['score'])
    manifest['execution_sources']={name:dict(sha256=ledger.sha(ledger.EXPERIMENTS/name)) for name in
        ('audit_split_feeder.py','audit_split_feeder_v2.py','audit_split_feeder_v3.py','audit_split_feeder_v4.py','audit_split_feeder_batch_v4.py','audit_deadline_window_repooling.py','audit_repooling.py','audit_window_damage_repair.py','audit_weighted_relay_ablation.py','audit_weighted_direct_pair.py')}
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


def test_real_paired_quota_full_assist_and_all_saved_clocks_are_replayed(tmp_path,tiny_results,monkeypatch):
    rows=make_batch(tmp_path,tiny_results,monkeypatch);result=audit.audit(tmp_path)
    assert result['passed'] and len(result['results'])==2 and result['selector_comparisons'][0]['same_selected_families']
    for row,reference in zip(rows,result['results']):
        assert [p['score'] for p in row['phases']]==[p['score'] for p in reference['phase_references']]
        assert reference['method_evidence']['unique_snapshots']>1
    assert rows[1]['late_count']==0
    assert rows[1]['weighted_score']<rows[0]['weighted_score']


def test_expired_catalogue_preserves_input_without_claiming_completed_paired_coverage(
        tmp_path,tiny_results,monkeypatch):
    expired=(*tiny_results[:3],tiny_results[4])
    rows=make_batch(tmp_path,expired,monkeypatch);result=audit.audit(tmp_path)
    comparison=result['selector_comparisons'][0]
    assert result['passed'] and comparison['selectors_completed'] is False
    assert comparison['selector_completed_by_arm']==[False,False]
    assert comparison['same_selected_families'] is None and comparison['selector_sha256'] is None
    for row in rows:
        metadata=next(p['metadata'] for p in row['phases'] if p['phase']==row['arm'])
        assert metadata['deadline_reached'] and not metadata['accepted_moves']
        assert all(p['routes_sha256']==row['initial_routes_sha256'] for p in row['phases'])


@pytest.mark.parametrize('tamper',['omitted_direct_map','late_adoption'])
def test_incomplete_map_and_after_deadline_adoption_claims_fail(tmp_path,tiny_results,monkeypatch,tamper):
    rows=make_batch(tmp_path,tiny_results,monkeypatch);row=rows[0 if tamper=='omitted_direct_map' else 1]
    metadata=next(p['metadata'] for p in row['phases'] if p['phase']==row['arm'])
    if tamper=='omitted_direct_map':
        record=metadata['family_records'][0];record['direct_maps_completed']=record['direct_maps_completed'][:-1]
    else:
        assert metadata['accepted_moves']
        metadata['accepted_moves'][0]['accepted_at_perf_counter']=metadata['deadline_perf_counter']+1.
    audit.ledger.write(tmp_path/'runs.json',rows)
    response=tmp_path/'receipts'/(row['run_id']+'__response.json');audit.ledger.write(response,row)
    receipt=tmp_path/'receipts'/(row['run_id']+'.json');saved=audit.ledger.read(receipt)
    saved['response_sha256']=audit.ledger.sha(response);audit.ledger.write(receipt,saved)
    with pytest.raises(AssertionError):audit.audit(tmp_path)


@pytest.mark.parametrize('tamper',['native_price','actual_wait','nonexistent_source_gap',
    'nonexistent_receiver_delivery_gap','receiver_middle_shift','receiver_second_delivery_clock'])
def test_early_prices_fail_physical_replay_even_after_catalogue_hashes_are_rebound(
        tmp_path,tiny_results,monkeypatch,tamper):
    rows=make_batch(tmp_path,tiny_results,monkeypatch);row=rows[0]
    metadata=next(p['metadata'] for p in row['phases'] if p['phase']==row['arm'])
    family=metadata['selected_families'][0];early=family['early_pricing_evidence']
    if tamper=='native_price':
        early['source_layouts'][0]['native_score']['late_count']+=1
    elif tamper=='actual_wait':
        early['receiver_layout']['waiting_min']+=1.
    elif tamper=='nonexistent_source_gap':
        early['source_layouts'][0]['gap']=10000
    elif tamper=='nonexistent_receiver_delivery_gap':
        early['receiver_layout']['delivery_gap']=10000
    elif tamper=='receiver_middle_shift':
        early['receiver_layout']['middle_native_shift_min']+=1.
    else:
        second=early['receiver_layout']['delivery_order_task_ids'][-1]
        early['selected_delivery_times_min'][str(second)]+=1.
    catalogue=metadata['catalogue'];catalogue['families']=metadata['selected_families']
    metadata['selector_sha256']=catalogue['selector_sha256']=audit.ledger.digest(catalogue['families'])
    manifest=audit.ledger.read(tmp_path/'manifest.json')
    catalogue['catalogue_sha256']=audit.ledger.digest(dict(families=catalogue['families'],
        base_routes=manifest['initial_routes'],weights=manifest['weights'],scales=manifest['scales'],
        configuration=catalogue['configuration']))
    audit.ledger.write(tmp_path/'runs.json',rows)
    response=tmp_path/'receipts'/(row['run_id']+'__response.json');audit.ledger.write(response,row)
    receipt=tmp_path/'receipts'/(row['run_id']+'.json');saved=audit.ledger.read(receipt)
    saved['response_sha256']=audit.ledger.sha(response);audit.ledger.write(receipt,saved)
    with pytest.raises(ValueError,match='early'):
        audit.audit(tmp_path)


def test_inspection_union_cannot_hide_a_family_specific_source_route(tmp_path,tiny_results,monkeypatch):
    rows=make_batch(tmp_path,tiny_results,monkeypatch);row=rows[0]
    metadata=next(p['metadata'] for p in row['phases'] if p['phase']==row['arm'])
    union=metadata['catalogue']['source_tables'];key=next(iter(union));union[key]=union[key][1:]
    audit.ledger.write(tmp_path/'runs.json',rows)
    response=tmp_path/'receipts'/(row['run_id']+'__response.json');audit.ledger.write(response,row)
    receipt=tmp_path/'receipts'/(row['run_id']+'.json');saved=audit.ledger.read(receipt)
    saved['response_sha256']=audit.ledger.sha(response);audit.ledger.write(receipt,saved)
    with pytest.raises(AssertionError,match='inspection union'):
        audit.audit(tmp_path)


@pytest.mark.parametrize('tamper',['missing_order','partial_is_complete','missing_scored_slot'])
def test_direct_flexible_coverage_cannot_invent_complete_search(tmp_path,tiny_results,monkeypatch,tamper):
    rows=make_batch(tmp_path,tiny_results,monkeypatch);row=rows[0]
    metadata=next(p['metadata'] for p in row['phases'] if p['phase']==row['arm'])
    progress=metadata['family_records'][0]['direct_same_owner_flexible_coverage']
    if tamper=='missing_order':
        progress['flexible_insertion_orders_completed'].pop()
    elif tamper=='partial_is_complete':
        progress['first_partial_is_complete_candidate']=True
    else:
        progress['pipeline_records'][0]['first_legal_PD_positions_full_scored']-=1
    audit.ledger.write(tmp_path/'runs.json',rows)
    response=tmp_path/'receipts'/(row['run_id']+'__response.json');audit.ledger.write(response,row)
    receipt=tmp_path/'receipts'/(row['run_id']+'.json');saved=audit.ledger.read(receipt)
    saved['response_sha256']=audit.ledger.sha(response);audit.ledger.write(receipt,saved)
    with pytest.raises(AssertionError):audit.audit(tmp_path)
