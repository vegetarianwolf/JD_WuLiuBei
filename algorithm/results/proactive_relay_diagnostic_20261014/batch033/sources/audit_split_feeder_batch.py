"""Saved-batch independent audit for quota-neutral split-feeder experiments."""
from __future__ import annotations

from pathlib import Path

try:
    from . import run_proactive_relay_research as ledger
    from .audit_deadline_window_repooling import _bind
    from .audit_split_feeder import audit_split_feeder_family, audit_split_feeder_proposal
    from .audit_weighted_direct_pair import load_official_csv
    from .audit_weighted_relay_ablation import audit_payload, direct_counterfactual, reference_schedule, _geometry
    from .audit_repooling import _owners
except ImportError:
    import run_proactive_relay_research as ledger
    from audit_deadline_window_repooling import _bind
    from audit_split_feeder import audit_split_feeder_family, audit_split_feeder_proposal
    from audit_weighted_direct_pair import load_official_csv
    from audit_weighted_relay_ablation import audit_payload, direct_counterfactual, reference_schedule, _geometry
    from audit_repooling import _owners


def _physical(score):
    return dict(late_count=score['late_count'],total_lateness_min=float(score['total_lateness_min']),distance_km=float(score['distance_km']))


def _key(score,manifest):
    values=[_physical(score)[name] for name in ledger.FIELDS]
    return [sum(w*v/s for w,v,s in zip(manifest['weights'],values,manifest['scales'])),*values]


def _compact(reference):
    return dict(score=_physical(reference['score']),full_replay_output_sha256=ledger.digest(reference))


def _audit_metadata(tasks,topology,base_routes,metadata,*,relay,manifest):
    config=metadata['configuration']
    assert config['relay_original_pair_owner_preserved'] and not config['relay_requires_compensation']
    assert config['direct_same_owner_all_empty_gaps_six_orders'] and config['direct_independent_four_task_final_maps']==12
    assert config['source_tables_are_complete_native'] and config['all_flight_and_wait_counted']
    assert config['raw_only'] and not config['polish_enabled'] and config['resources_and_homes_unchanged']
    aliases=dict(sigma='sigma_min',receiver_gap_limit='requested_receiver_gap_limit_legacy_hint',
        direct_gap_limit='requested_direct_gap_limit_legacy_hint')
    arm='split_feeder_relay' if relay else 'split_feeder_direct'
    for name,value in manifest.get('method_parameters',{}).get(arm,{}).items():
        _bind(config[aliases.get(name,name)],value,'split frozen parameter '+name)
    assert ledger.digest(metadata['selected_families']) == metadata['selector_sha256']
    assert metadata['families_examined'] == len(metadata['family_records'])
    assert [r['family'] for r in metadata['family_records']] == metadata['selected_families'][:metadata['families_examined']]
    family_references=[]
    for family in metadata['selected_families']:
        proof=audit_split_feeder_family(tasks,topology,base_routes,family,weights=manifest['weights'],scales=manifest['scales'])
        assert proof['passed']
        family_references.append(dict(family_sha256=ledger.digest(family),full_replay_output_sha256=ledger.digest(proof)))
    if not relay:
        for record in metadata['family_records']:
            family=record['family'];ids=family['task_ids']
            maps=[[[t],[q],[r for r in ids if r not in (t,q)]] for t in ids for q in ids if t!=q]
            considered,completed=record['direct_maps_considered'],record['direct_maps_completed']
            assert considered==maps[:len(considered)] and len(considered)<=12
            assert completed==considered[:len(completed)]
            if record['direct_map_enumeration_completed']:
                assert completed==maps and record['complete_table_enumeration_completed']
                assert record['direct_same_owner_pair_completed']
    snapshots=[(name,metadata.get(name)) for name in ('input_snapshot','first_complete_candidate','best_constructed_candidate')]
    snapshots += [(f'full_candidate_{i}',s) for i,s in enumerate(metadata['full_candidate_snapshots'])]
    references=[];cache={}
    for name,snapshot in snapshots:
        if snapshot is None:continue
        assert snapshot['valid'] is True and snapshot['complete'] is True
        digest=ledger.digest(snapshot)
        if digest not in cache:
            if snapshot.get('move') is None:
                assert snapshot['routes']==[list(r) for r in base_routes]
                proof=reference_schedule(tasks,base_routes,topology)
                _bind(snapshot['score'],_physical(proof['score']),'split input score')
                _bind(snapshot['table_score'],_physical(proof['score']),'split input table score')
                _bind(snapshot['event_times_min'],proof['event_times_min'],'split input events')
                _bind(snapshot['delivery_times_min'],proof['delivery_times_min'],'split input deliveries')
                owners=_owners(base_routes,_geometry(tasks,topology)[2])
                _bind(snapshot['actual_final_owners'],{str(t):a for t,a in sorted(owners.items())},'split input owners')
                _bind(snapshot['source_drop_availability_min'],{},'split input no source availability')
                _bind(snapshot['handoff'],None,'split input no handoff')
                _bind(snapshot['shared_loaded_distance_km'],0.,'split input no selected pair shared metric')
                assert snapshot['cross_uav_count']==snapshot['relay_task_count']==0
            else:
                assert snapshot['move']['family'] in metadata['selected_families']
                assert snapshot['deadline_perf_counter']==metadata['deadline_perf_counter']
                assert snapshot['full_verified_at_perf_counter']<metadata['deadline_perf_counter']
                proof=audit_split_feeder_proposal(tasks,topology,base_routes,snapshot['routes'],snapshot['move'],
                    relay_enabled=relay,telemetry=snapshot,weights=manifest['weights'],scales=manifest['scales'])
            _bind(snapshot['key'],_key(proof['score'],manifest),'split complete weighted key')
            cache[digest]=_compact(proof)
        references.append(dict(proposal=name,snapshot_sha256=digest,**cache[digest]))
    guard=_key(reference_schedule(tasks,base_routes,topology)['score'],manifest)
    for accepted in metadata['accepted_moves']:
        snapshot=accepted['full_snapshot']
        assert snapshot in metadata['full_candidate_snapshots']
        assert accepted['full_verified_at_perf_counter'] <= accepted['accepted_at_perf_counter'] < metadata['deadline_perf_counter']
        assert tuple(snapshot['key'])<tuple(guard)
        guard=snapshot['key']
    assert metadata['accepted_improvements']==len(metadata['accepted_moves'])
    _bind(metadata['initial_key'],_key(reference_schedule(tasks,base_routes,topology)['score'],manifest),'split method initial guard')
    _bind(metadata['final_key'],guard,'split final guarded key')
    return dict(family_references=family_references,proposal_references=references,unique_snapshots=len(cache))


def audit(output):
    output=Path(output).resolve()
    assert not (output/'audit.json').exists(),'Never overwrite an existing completed audit'
    manifest=ledger.read(output/'manifest.json')
    ledger.verify(output,manifest,current_execution=False)
    for name in ('audit_split_feeder.py','audit_split_feeder_batch.py','audit_weighted_relay_ablation.py','audit_weighted_direct_pair.py'):
        assert ledger.sha(ledger.EXPERIMENTS/name)==manifest['execution_sources'][name]['sha256'],name
    tasks=load_official_csv(ledger.ROOT/manifest['input_path']);rows=ledger.read(output/'runs.json')
    assert len(rows)==len(manifest['schedule'])>0
    results=[];paired={}
    for row,cell in zip(rows,manifest['schedule']):
        assert all(row[name]==cell[name] for name in ('run_id','arm','seed'))
        assert row['arm'] in ledger.SPLIT_FEEDER_PAIR and row['valid'] and row['budget_compliant']
        assert 0<=row['end_to_end_seconds']<=manifest['budget_seconds']
        assert row['source_sha256']==manifest['source_archive']['sha256']
        assert row['initial_routes_sha256']==manifest['initial_routes_sha256']
        response=output/'receipts'/(row['run_id']+'__response.json')
        receipt=ledger.read(output/'receipts'/(row['run_id']+'.json'))
        assert receipt['status']=='completed' and receipt['manifest_sha256']==ledger.sha(output/'manifest.json')
        assert receipt['response_sha256']==ledger.sha(response) and ledger.read(response)==row
        relay=row['arm'].endswith('_relay');topology={**manifest['topology'],'relay_allowed':relay}
        before=manifest['initial_routes'];previous_hash=manifest['initial_routes_sha256'];previous_key=None
        method_metadata=None;method_evidence=None;phase_references=[]
        for phase in row['phases']:
            assert ledger.sha(output/phase['solution_path'])==phase['solution_sha256']
            payload=ledger.read(output/phase['solution_path'])
            assert {**phase['topology'],'relay_allowed':relay}==topology
            phase_reference=audit_payload(tasks,topology,payload)
            routes=[r['encoded_visits'] for r in payload['routes']]
            assert ledger.digest(routes)==phase['routes_sha256'] and phase['input_routes_sha256']==previous_hash
            _bind(phase['score'],_physical(phase_reference['score']),'split phase score')
            current_key=_key(phase_reference['score'],manifest)
            assert abs(phase['weighted_score']-current_key[0])<1e-10
            if previous_key is not None:assert tuple(current_key)<=tuple(previous_key)
            if phase['phase']=='initial':
                assert phase['routes_sha256']==manifest['initial_routes_sha256']
                _bind(phase['score'],_physical(manifest['initial_score']),'split original score')
            if phase['phase']==row['arm']:
                assert method_metadata is None
                method_metadata=phase['metadata'];assert method_metadata['seed']==row['seed']
                method_evidence=_audit_metadata(tasks,topology,before,method_metadata,relay=relay,manifest=manifest)
                _bind(method_metadata['final_key'],current_key,'split phase final guard')
                if routes!=before:assert method_metadata['accepted_moves'][-1]['full_snapshot']['routes']==routes
                paired[(row['seed'],row['arm'])]=method_metadata
            phase_references.append(dict(phase=phase['phase'],routes_sha256=phase['routes_sha256'],**_compact(phase_reference)))
            previous_key,previous_hash,before=current_key,phase['routes_sha256'],routes
        assert method_metadata is not None and method_evidence is not None
        assert row['solution_path']==row['phases'][-1]['solution_path'] and row['solution_sha256']==row['phases'][-1]['solution_sha256']
        _bind({k:row[k] for k in ledger.FIELDS},{k:previous_key[n+1] for n,k in enumerate(ledger.FIELDS)},'split final row')
        assert abs(row['weighted_score']-previous_key[0])<1e-10 and row['on_time_count']==len(tasks)-row['late_count']
        path=output/row['counterfactual_path'];assert ledger.sha(path)==row['counterfactual_sha256']
        cf=ledger.read(path)
        assert [r['encoded_visits'] for r in cf['routes']]==[list(r) for r in direct_counterfactual(before,topology)]
        cf_reference=audit_payload(tasks,{**topology,'relay_allowed':False},cf)
        results.append(dict(run_id=row['run_id'],passed=True,phase_references=phase_references,
            method_evidence=method_evidence,fixed_structure_direct=_compact(cf_reference)))
    comparisons=[]
    for seed in sorted({row['seed'] for row in rows}):
        if all((seed,arm) in paired for arm in ledger.SPLIT_FEEDER_PAIR):
            direct,relay=(paired[(seed,arm)] for arm in ledger.SPLIT_FEEDER_PAIR)
            assert direct['selector_completed'] and relay['selector_completed']
            assert direct['selected_families']==relay['selected_families'] and direct['selector_sha256']==relay['selector_sha256']
            for metadata in (direct,relay):
                assert metadata['selector_completed_at_perf_counter']<metadata['deadline_perf_counter']
            assert direct['configuration']==relay['configuration']
            comparisons.append(dict(seed=seed,arms=list(ledger.SPLIT_FEEDER_PAIR),selectors_completed=True,
                same_selected_families=True,selector_sha256=direct['selector_sha256'],direct_balanced_assignment_space=12,
                direct_same_owner_pair_scoring_completed_by_family=[r['direct_same_owner_pair_completed'] for r in direct['family_records']],
                direct_maps_completed_by_family=[len(r['direct_maps_completed']) for r in direct['family_records']],
                same_total_budget_not_same_candidate_count=True,
                relay_witnesses_not_DIRECT_admissibility=True))
    result=dict(passed=True,manifest_sha256=ledger.sha(output/'manifest.json'),runs_sha256=ledger.sha(output/'runs.json'),
        results=results,selector_comparisons=comparisons)
    ledger.write(output/'audit.json',result)
    print(dict(passed=True,runs=len(rows),proposal_references=sum(len(r['method_evidence']['proposal_references']) for r in results)))
    return result
