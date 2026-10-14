"""Saved-batch audit for separated-delivery priced split-feeder experiments."""
from __future__ import annotations

from itertools import combinations
from pathlib import Path

try:
    from . import run_proactive_relay_research as ledger
    from .audit_deadline_window_repooling import _bind
    from .audit_split_feeder_v4 import audit_split_feeder_family, audit_split_feeder_proposal
    from .audit_weighted_direct_pair import load_official_csv
    from .audit_weighted_relay_ablation import audit_payload, direct_counterfactual, reference_schedule, _geometry
    from .audit_repooling import _owners
except ImportError:
    import run_proactive_relay_research as ledger
    from audit_deadline_window_repooling import _bind
    from audit_split_feeder_v4 import audit_split_feeder_family, audit_split_feeder_proposal
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


def _direct_slots(route):
    """Count legal whole-P/D positions from physical native loads only."""
    loads=[0];held=set()
    for visit in route:
        if visit>0:
            assert visit not in held
            held.add(visit)
        else:
            assert -visit in held
            held.remove(-visit)
        assert len(held)<=2
        loads.append(len(held))
    assert not held
    return sum(all(load<=1 for load in loads[g:h+1])
        for g in range(len(loads)) for h in range(g,len(loads)))


def _audit_flexible_pair(coverage,native,pair,limit):
    """Check the saved finite beam's actual coverage, never global optimality."""
    if not coverage:return
    orders=[list(pair),list(reversed(pair))]
    assert coverage['flexible_insertion_orders']==orders
    completed=coverage['flexible_insertion_orders_completed']
    assert completed==orders[:len(completed)] and len(completed)<=2
    assert coverage['first_partial_is_complete_candidate'] is False
    assert coverage['first_partial_coverage_scope']=='native_plus_first_task_only'
    assert coverage['all_legal_PD_positions_scored_before_layer_cap'] is True
    assert coverage['per_layer_option_limit']==limit
    pipelines=coverage['pipeline_records']
    assert len(completed)<=len(pipelines)<=min(2,len(completed)+1)
    total=0;initial_slots=_direct_slots(native);native_ids={abs(v) for v in native}
    for number,stage in enumerate(pipelines):
        first,second=orders[number]
        assert stage['insertion_order']==orders[number]
        assert stage['first_present_task_ids']==sorted(native_ids|{first})
        assert stage['second_present_task_ids']==sorted(native_ids|{first,second})
        first_count=stage['first_legal_PD_positions_full_scored']
        assert type(first_count) is int and 0<=first_count<=initial_slots
        partial=stage.get('first_retained_routes',[])
        assert len(partial)==stage['first_records_retained']<=limit
        assert len({tuple(r) for r in partial})==len(partial)
        for route in partial:
            assert route.count(first)==route.count(-first)==1
            assert tuple(v for v in route if abs(v)!=first)==tuple(native)
            assert all(abs(v) in native_ids|{first} for v in route)
            _direct_slots(route)
        if partial:
            assert first_count==initial_slots
            assert len(partial)==min(limit,initial_slots)
        done=stage['second_parent_records_completed']
        assert type(done) is int and 0<=done<=len(partial)
        counts=[_direct_slots(r) for r in partial]
        second_count=stage['second_legal_PD_positions_full_scored']
        assert type(second_count) is int
        lower=sum(counts[:done]);upper=lower+(counts[done] if done<len(counts) else 0)
        assert lower<=second_count<=upper,'DIRECT flexible real second-layer position coverage'
        assert stage['completed'] is (number<len(completed))
        if stage['completed']:
            assert first_count==initial_slots and done==len(partial)>0
            assert second_count==sum(counts)
            assert 1<=stage['complete_records_retained']<=limit
        total+=first_count+second_count
    assert coverage['legal_PD_positions_full_scored']==total
    if coverage['six_block_enumeration_completed']:
        loads=[0]
        for v in native:loads.append(loads[-1]+(1 if v>0 else -1))
        gaps=sum(v==0 for v in loads)
        assert coverage['six_block_empty_gaps_evaluated']==gaps
        assert coverage['six_block_layouts_evaluated']==6*gaps
    else:
        assert coverage['six_block_empty_gaps_evaluated']==coverage['six_block_layouts_evaluated']==0
    if coverage['flexible_pair_completed']:
        assert completed==orders and coverage['six_block_enumeration_completed']
        assert 1<=coverage['final_records_retained']<=limit


def _audit_metadata(tasks,topology,base_routes,metadata,*,relay,manifest):
    config=metadata['configuration']
    assert config['catalogue_version']==4
    assert config['early_catalogue_native_pricing']=='restricted_two_gap_IN_and_separated_OUT_delivery'
    assert config['early_relay_price_is_DIRECT_candidate'] is False
    assert config['relay_original_pair_owner_preserved'] and not config['relay_requires_compensation']
    assert config['direct_same_owner_all_empty_gaps_six_orders'] and config['direct_independent_four_task_final_maps']==12
    assert config['direct_pair_flexible_native_interleaving'] is True
    assert config['direct_pair_flexible_whole_insertion_orders']==2
    assert config['source_tables_are_complete_native'] and config['all_flight_and_wait_counted']
    assert config['raw_only'] and not config['polish_enabled'] and config['resources_and_homes_unchanged']
    aliases=dict(sigma='sigma_min',receiver_gap_limit='requested_receiver_gap_limit_legacy_hint',
        direct_gap_limit='requested_direct_gap_limit_legacy_hint')
    arm='separated_delivery_relay' if relay else 'separated_delivery_direct'
    for name,value in manifest.get('method_parameters',{}).get(arm,{}).items():
        _bind(config[aliases.get(name,name)],value,'split frozen parameter '+name)
    assert ledger.digest(metadata['selected_families']) == metadata['selector_sha256']
    if metadata['selector_completed']:
        catalogue=metadata['catalogue']
        assert catalogue['completed'] and catalogue['families']==metadata['selected_families']
        assert catalogue['selector_sha256']==metadata['selector_sha256']
        assert catalogue['catalogue_sha256']==ledger.digest(dict(families=catalogue['families'],
            base_routes=base_routes,weights=manifest['weights'],scales=manifest['scales'],
            configuration=catalogue['configuration']))
        assert catalogue['configuration']['old_ready_distance_Pareto_used'] is False
        assert catalogue['configuration']['uniform_price_is_global_bound'] is False
        assert catalogue['source_tables_scope']=='inspection_union_not_a_single_shared_cap_surface'
        observed={}
        for family in catalogue['families']:
            for key,records in family['source_tables'].items():
                assert 0<len(records)<=config['source_option_limit']
                for record in records:
                    observed.setdefault(key,{}).setdefault(tuple(record['route']),set()).add(ledger.digest(record))
        assert set(catalogue['source_tables'])==set(observed),'catalogue inspection union keys'
        for key,records in catalogue['source_tables'].items():
            assert len({tuple(r['route']) for r in records})==len(records),'catalogue inspection union duplicates'
            assert {tuple(r['route']) for r in records}==set(observed[key]),'catalogue inspection union routes'
            assert all(ledger.digest(r) in observed[key][tuple(r['route'])] for r in records),'catalogue inspection union records'
    else:
        assert metadata['catalogue'] is None and not metadata['selected_families']
        assert not metadata['family_records'] and not metadata['full_candidate_snapshots']
        assert not metadata['accepted_moves'] and metadata['accepted_improvements']==0
    assert metadata['families_examined'] == len(metadata['family_records'])
    assert [r['family'] for r in metadata['family_records']] == metadata['selected_families'][:metadata['families_examined']]
    family_references=[]
    for family in metadata['selected_families']:
        if config['source_option_limit']==3:
            retention=family['seed_retention']
            assert len(retention)==2 and [r['channel'] for r in retention]==['weighted','pooled']
            assert all(r['source_seed_routes_retained'] and r['early_seed_full_scored'] for r in retention)
            assert all(r['early_seed_entered_candidate_pool'] and r['receiver_seed_layout_full_scored'] for r in retention)
        proof=audit_split_feeder_family(tasks,topology,base_routes,family,weights=manifest['weights'],scales=manifest['scales'])
        assert proof['passed']
        if config['source_option_limit']==config['table_option_limit']==3:
            witnesses=family['relay_witnesses']
            for early in (family['early_pricing_evidence'],family['early_rescue_pricing_evidence']):
                assert min(w['table_score']['late_count'] for w in witnesses)<=early['full_score']['late_count']
                assert max(len(w['pooled_rescued_task_ids']) for w in witnesses)>=len(early['pooled_rescued_task_ids'])
        family_references.append(dict(family_sha256=ledger.digest(family),full_replay_output_sha256=ledger.digest(proof)))
    if not relay:
        for record in metadata['family_records']:
            family=record['family'];ids=family['task_ids']
            pair=[family['i'],family['j']]
            native=tuple(v for v in base_routes[family['c']] if abs(v) not in ids)
            same=record['direct_same_owner_flexible_coverage']
            _audit_flexible_pair(same,native,pair,config['table_option_limit'])
            assert record['direct_same_owner_flexible_pair_completed']==same.get('flexible_pair_completed',False)
            assert record['direct_same_owner_flexible_insertion_orders']==same.get('flexible_insertion_orders_completed',[])
            pairs=[list(p) for p in combinations(ids,2)]
            donor_records=record['direct_donor_pair_flexible_records']
            assert [r['task_pair'] for r in donor_records]==pairs[:len(donor_records)]
            for saved in donor_records:
                _audit_flexible_pair(saved['coverage'],native,saved['task_pair'],config['table_option_limit'])
            pair_done=record['direct_donor_pair_flexible_completed']
            assert pair_done==pairs[:len(pair_done)]
            assert len(pair_done)<=len(donor_records)<=min(6,len(pair_done)+1)
            assert all(r['coverage']['flexible_pair_completed'] for r in donor_records[:len(pair_done)])
            maps=[[[t],[q],[r for r in ids if r not in (t,q)]] for t in ids for q in ids if t!=q]
            considered,completed=record['direct_maps_considered'],record['direct_maps_completed']
            assert considered==maps[:len(considered)] and len(considered)<=12
            assert completed==considered[:len(completed)]
            if record['direct_map_enumeration_completed']:
                assert completed==maps and record['complete_table_enumeration_completed']
                assert record['direct_same_owner_pair_completed']
                assert record['direct_same_owner_flexible_pair_completed'] and pair_done==pairs
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
    for name in ('audit_split_feeder.py','audit_split_feeder_v2.py','audit_split_feeder_v3.py','audit_split_feeder_v4.py','audit_split_feeder_batch_v4.py','audit_deadline_window_repooling.py','audit_repooling.py','audit_window_damage_repair.py','audit_weighted_relay_ablation.py','audit_weighted_direct_pair.py'):
        assert ledger.sha(ledger.EXPERIMENTS/name)==manifest['execution_sources'][name]['sha256'],name
    tasks=load_official_csv(ledger.ROOT/manifest['input_path']);rows=ledger.read(output/'runs.json')
    assert len(rows)==len(manifest['schedule'])>0
    results=[];paired={}
    for row,cell in zip(rows,manifest['schedule']):
        assert all(row[name]==cell[name] for name in ('run_id','arm','seed'))
        assert row['arm'] in ledger.SEPARATED_DELIVERY_PAIR and row['valid'] and row['budget_compliant']
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
        if all((seed,arm) in paired for arm in ledger.SEPARATED_DELIVERY_PAIR):
            direct,relay=(paired[(seed,arm)] for arm in ledger.SEPARATED_DELIVERY_PAIR)
            assert direct['configuration']==relay['configuration']
            completed=direct['selector_completed'] and relay['selector_completed']
            for metadata in (direct,relay):
                if metadata['selector_completed']:
                    assert metadata['selector_completed_at_perf_counter']<metadata['deadline_perf_counter']
            if completed:
                assert direct['selected_families']==relay['selected_families'] and direct['selector_sha256']==relay['selector_sha256']
            comparisons.append(dict(seed=seed,arms=list(ledger.SEPARATED_DELIVERY_PAIR),selectors_completed=completed,
                both_selectors_completed=completed,
                selector_completed_by_arm=[direct['selector_completed'],relay['selector_completed']],
                same_selected_families=True if completed else None,
                selector_sha256=direct['selector_sha256'] if completed else None,direct_balanced_assignment_space=12,
                direct_same_owner_pair_scoring_completed_by_family=[r['direct_same_owner_pair_completed'] for r in direct['family_records']],
                direct_maps_completed_by_family=[len(r['direct_maps_completed']) for r in direct['family_records']],
                same_total_budget_not_same_candidate_count=True,
                relay_witnesses_not_DIRECT_admissibility=True))
    result=dict(passed=True,manifest_sha256=ledger.sha(output/'manifest.json'),runs_sha256=ledger.sha(output/'runs.json'),
        results=results,selector_comparisons=comparisons)
    ledger.write(output/'audit.json',result)
    print(dict(passed=True,runs=len(rows),proposal_references=sum(len(r['method_evidence']['proposal_references']) for r in results)))
    return result
