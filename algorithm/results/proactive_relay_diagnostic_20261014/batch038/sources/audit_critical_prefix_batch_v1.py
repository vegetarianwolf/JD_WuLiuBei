"""Independent saved-batch proof for paid raw seeds and dynamic quota repair."""
from __future__ import annotations
from pathlib import Path
try:
    from . import run_proactive_relay_research as ledger
    from . import audit_split_feeder_batch_v4 as raw_audit
    from . import audit_critical_prefix_joint_repair_v1 as physical
    from .audit_deadline_window_repooling import _bind
    from .audit_weighted_direct_pair import load_official_csv
    from .audit_weighted_relay_ablation import audit_payload, direct_counterfactual, reference_schedule, _geometry
    from .audit_repooling import _identity
except ImportError:
    import run_proactive_relay_research as ledger
    import audit_split_feeder_batch_v4 as raw_audit
    import audit_critical_prefix_joint_repair_v1 as physical
    from audit_deadline_window_repooling import _bind
    from audit_weighted_direct_pair import load_official_csv
    from audit_weighted_relay_ablation import audit_payload, direct_counterfactual, reference_schedule, _geometry
    from audit_repooling import _identity

_physical=raw_audit._physical
_key=raw_audit._key
_compact=raw_audit._compact


def _separated(snapshot,registry):
    move=snapshot.get('move')
    if not move or move['mode']!='RELAY':return False
    f=move['family'];pair={f['i'],f['j']};route=snapshot['routes'][f['c']]
    ends=[k for k,v in enumerate(route) if v<0 and _identity(v,registry) in pair]
    return len(ends)==2 and any(_identity(v,registry) not in pair for v in route[ends[0]+1:ends[1]])


def _expected_seeds(tasks,topology,base,raw,relay,limit):
    registry=_geometry(tasks,topology)[2];snapshots=raw['full_candidate_snapshots']
    if relay:
        candidates=[s for s in snapshots if s.get('move',{}).get('mode')=='RELAY']
        groups=[('minimum_J_RELAY',candidates),('minimum_J_genuine_separated_RELAY',
            [s for s in candidates if _separated(s,registry)])]
    else:
        unique={}
        for s in [raw['input_snapshot'],*snapshots]:unique.setdefault(tuple(map(tuple,s['routes'])),s)
        ordered=sorted(unique.values(),key=lambda s:(tuple(s['key']),tuple(map(tuple,s['routes']))))
        groups=[('independent_DIRECT_rank_'+str(n+1),[s]) for n,s in enumerate(ordered[:limit])]
    original=reference_schedule(tasks,base,topology)['delivery_times_min']
    fallback=sorted(tasks,key=lambda t:(-(original[str(t)]-tasks[t][2]),t))[:2]
    records=[];seen=set()
    for name,candidates in groups:
        if not candidates:continue
        s=min(candidates,key=lambda s:(tuple(s['key']),tuple(map(tuple,s['routes']))))
        route=tuple(map(tuple,s['routes']))
        if route in seen:continue
        seen.add(route);move=s.get('move');f=move['family'] if move else None
        focus=sorted((f['i'],f['j'])) if f else sorted(fallback)
        records.append(dict(channel=name,raw_snapshot=s,routes_sha256=ledger.digest(s['routes']),
            seed_move=move,focus_task_ids=focus,pinned_task_ids=focus if relay else [],
            genuinely_separated_receiver=_separated(s,registry)))
        if len(records)>=limit:break
    return records


def _repair_evidence(tasks,topology,base,seed,entry,manifest,parameters,relay):
    meta=entry['metadata'];raw=seed['raw_snapshot']['routes'];move=seed['seed_move'];focus=seed['focus_task_ids']
    pins=focus if relay else [];deadline=entry['deadline_perf_counter'];w,s=manifest['weights'],manifest['scales']
    assert entry['returned'] and meta['deadline_perf_counter']==deadline
    assert meta['relay_enabled'] is relay and meta['original_seed_move']==move
    assert meta['pinned_task_ids']==pins and meta['focus_task_ids']==focus
    for name,value in parameters.items():_bind(meta['configuration'][name],value,'frozen repair parameter '+name)
    config=meta['configuration']
    assert config['owner_policy']=='state_local_positive_remaining_quota_all_owners'
    assert config['task_layer_size']==2 and config['first_partial_J_pruning'] is False
    assert not config['position_scores_are_bounds'] and not config['all_PD_positions_globally_scored']
    assert config['ordinary_DIRECT_owner_and_order_free'] and not config['new_relay_generation']
    counts=[r['task_count'] for r in reference_schedule(tasks,raw,topology)['route_records']]
    proofs=[];cache={}
    def checkpoint(snapshot,label):
        if snapshot is None:return
        digest=ledger.digest(snapshot)
        if digest not in cache:
            proof=physical.audit_repair(tasks,topology,base,raw,snapshot['routes'],move,relay_enabled=relay,
                telemetry=snapshot,weights=w,scales=s,deadline=deadline,focus_task_ids=focus)
            cache[digest]=_compact(proof['core_reference'])
        proofs.append(dict(checkpoint=label,snapshot_sha256=digest,**cache[digest]))
    for name in ('input_snapshot','post_repair_snapshot','first_complete_candidate','best_constructed_candidate'):
        checkpoint(meta.get(name),name)
    assert len(meta['full_candidate_snapshots'])<=parameters['snapshot_limit']
    for n,snapshot in enumerate(meta['full_candidate_snapshots']):checkpoint(snapshot,'saved_'+str(n))
    closure_refs=[];partial_count=0;complete_count=0;complete_leaves=[]
    for record in meta['closure_records']:
        before=record['before_routes']
        physical.audit_repair(tasks,topology,base,raw,before,move,relay_enabled=relay,focus_task_ids=focus)
        cp=physical.audit_closure(tasks,topology,base,before,record['closure'],pinned_task_ids=pins,
            relay_enabled=relay,seed_move=move,weights=w,scales=s)
        assert record['closure']['critical_native_limit']==2
        states={record['initial_state']['state_id']:record['initial_state']}
        common=dict(target_final_counts=counts,raw_routes=raw,pinned_task_ids=pins,relay_enabled=relay,
            seed_move=move,weights=w,scales=s)
        initial=record['initial_state'];assert initial['routes']==record['closure']['partial_routes']
        physical.audit_partial_state(tasks,topology,initial['routes'],initial['present_task_ids'],telemetry=initial,**common)
        partial_count+=1;removed=record['closure']['removed_task_ids']
        for number,layer in enumerate(record['layers']):
            assert layer['layer_index']==number and layer['task_ids']==removed[2*number:2*number+2]
            expected=sorted(set(tasks)-set(removed[2*number+len(layer['task_ids']):]))
            assert layer['present_task_ids']==expected
            assert len(layer['retained_states'])<=parameters['beam_width']
            assert not layer['retained_states'] or layer['completed']
            keys=[]
            for state in layer['retained_states']:
                assert state['present_task_ids']==expected and set(state['inserted_task_order'])==set(layer['task_ids'])
                parent=states[state['parent_state_id']]
                physical.audit_partial_state(tasks,topology,state['routes'],state['present_task_ids'],telemetry=state,
                    parent_routes=parent['routes'],parent_present_task_ids=parent['present_task_ids'],
                    inserted_task_order=state['inserted_task_order'],owner_choices=state['owner_choices'],insertions=state['insertions'],**common)
                keys.append(tuple(state['key']));states[state['state_id']]=state;partial_count+=1
            assert keys==sorted(keys)
        for leaf in record['complete_checkpoints']:
            parent=states[leaf['parent_state_id']]
            proof=physical.audit_partial_state(tasks,topology,leaf['routes'],sorted(tasks),
                parent_routes=parent['routes'],parent_present_task_ids=parent['present_task_ids'],
                inserted_task_order=leaf['inserted_task_order'],owner_choices=leaf['owner_choices'],insertions=leaf['insertions'],**common)
            full=physical.audit_repair(tasks,topology,base,raw,leaf['routes'],move,relay_enabled=relay,focus_task_ids=focus)
            _bind(leaf['key'],_key(full['score'],manifest),'independent full checkpoint key')
            assert leaf['full_verified_at_perf_counter']<deadline
            _bind(leaf['changed_owner_task_ids'],full['changed_owner_task_ids'],'full checkpoint owners')
            assert not any(proof['remaining_quota_slots']);complete_count+=1
            complete_leaves.append(leaf)
        closure_refs.append(dict(round_index=record['round_index'],proof_sha256=ledger.digest(cp),
            removed_task_ids=cp['removed_task_ids'],quota_slots_by_aircraft=cp['quota_slots_by_aircraft'],
            zero_delivery_damage_causal_task_ids=cp['zero_delivery_damage_causal_task_ids']))
    assert meta['complete_leafs_full_verified']==complete_count
    assert meta['completed_rounds']==sum(r['completed'] for r in meta['closure_records'])
    initial=meta['input_snapshot'];best_leaf=None;improvements=0
    if initial is not None:
        best_key=initial['key']
        for leaf in complete_leaves:
            if tuple(leaf['key'])<tuple(best_key):
                best_key=leaf['key'];best_leaf=leaf;improvements+=1
        assert meta['accepted_improvements']==improvements
        expected=best_leaf or initial
        assert meta['post_repair_snapshot']['routes']==expected['routes']
        _bind(meta['post_repair_snapshot']['key'],expected['key'],'actual best saved helper checkpoint')
        first=meta['first_complete_candidate']
        assert (first is None)==(not complete_leaves)
        if first is not None:
            assert first['state_id']==complete_leaves[0]['state_id'] and first['routes']==complete_leaves[0]['routes']
        constructed=meta['best_constructed_candidate']
        assert (constructed is None)==(best_leaf is None)
        if constructed is not None:
            assert constructed['state_id']==best_leaf['state_id'] and constructed['routes']==best_leaf['routes']
    checked=meta['returned_checkpoint_checked_within_deadline']
    if checked:
        post=meta['post_repair_snapshot'];assert post is not None and post['routes']==entry['returned_routes']
        assert meta['final_routes_sha256']==ledger.digest(post['routes'])
        _bind(meta['best_key'],post['key'],'helper returned current best key')
    else:
        assert meta['post_repair_snapshot'] is None and not meta['full_candidate_snapshots']
        assert entry['returned_routes']==raw and not meta['accepted_improvements']
        assert entry.get('post_consideration_skipped')=='no_helper_predeadline_complete_checkpoint'
    return dict(checkpoint_references=proofs,closure_references=closure_refs,
        partial_states_replayed=partial_count,complete_leaf_checkpoints_replayed=complete_count,
        all_probed_or_omitted_positions_replayed=False)


def _audit_metadata(tasks,topology,base_routes,metadata,*,relay,manifest):
    config=metadata['configuration'];arm='critical_prefix_relay' if relay else 'critical_prefix_direct'
    params=manifest.get('method_parameters',{}).get(arm,{})
    assert config['raw_method']=='split_feeder_refinement_v4' and config['matched_shared_raw_diagnostic']
    assert not config['strongest_fresh_end_to_end_comparison'] and config['no_free_historical_relay_seed']
    assert config['final_guard']=='original_complete_input_strict_weighted'
    assert config['direct_post_pins_empty'] and config['relay_post_pins_two_actual_plans'] and config['resources_homes_weights_unchanged']
    assert metadata['relay_enabled'] is relay
    for name,value in params.items():
        if name in ('raw_fraction','repair_seed_limit'):_bind(config[name],value,'wrapper fixed '+name)
        else:_bind(config['raw_parameters'].get(name,config['repair_parameters'].get(name)),value,'wrapper fixed '+name)
    deadline=metadata['deadline_perf_counter'];w,s=manifest['weights'],manifest['scales']
    original=physical.audit_complete_state(tasks,topology,base_routes,telemetry=metadata['input_snapshot'],weights=w,scales=s,
        focus_task_ids=(),relay_enabled=False)
    _bind(metadata['initial_key'],_key(original['score'],manifest),'original guard key')
    raw=metadata['raw_metadata'];rawproof=None
    if raw is None:
        assert metadata['deadline_reached'] and not metadata['seed_records'] and not metadata['repair_records']
        assert not metadata['full_candidate_snapshots'] and not metadata['accepted_moves']
    else:
        assert raw['deadline_perf_counter']==metadata['raw_deadline_perf_counter']<deadline
        started=metadata['started_at_perf_counter']
        assert abs(metadata['raw_deadline_perf_counter']-(started+(deadline-started)*config['raw_fraction']))<1e-9
        for name,value in config['raw_parameters'].items():
            _bind(raw['configuration']['sigma_min' if name=='sigma' else name],value,'paid raw parameter '+name)
        rawproof=raw_audit._audit_metadata(tasks,topology,base_routes,raw,relay=relay,manifest=manifest)
        raw_guard=raw['accepted_moves'][-1]['full_snapshot']['routes'] if raw['accepted_moves'] else base_routes
        assert metadata['raw_output_routes']==raw_guard
        selected=_expected_seeds(tasks,topology,base_routes,raw,relay,config['repair_seed_limit'])
        _bind(metadata['seed_records'],selected,'actual complete minimum-J seed channels')
    seeds={s['routes_sha256']:s for s in metadata['seed_records']}
    repairs=[]
    assert len(metadata['repair_records'])<=len(metadata['seed_records'])
    for number,entry in enumerate(metadata['repair_records']):
        seed=metadata['seed_records'][number]
        assert entry['seed_index']==number and entry['channel']==seed['channel']
        assert entry['deadline_perf_counter']<=deadline
        if entry['returned']:
            repairs.append(_repair_evidence(tasks,topology,base_routes,seed,entry,manifest,config['repair_parameters'],relay))
        else:
            assert metadata['deadline_reached'] and entry['metadata'] is None and entry['returned_routes'] is None
    refs=[];snapshots=metadata['full_candidate_snapshots']
    for snapshot in snapshots:
        seed=seeds[snapshot['seed_routes_sha256']]
        proof=physical.audit_complete_state(tasks,topology,snapshot['routes'],telemetry=snapshot,weights=w,scales=s,
            target_final_counts=original['final_tasks_per_aircraft'],raw_routes=seed['raw_snapshot']['routes'],
            seed_move=seed['seed_move'],relay_enabled=relay,focus_task_ids=seed['focus_task_ids'],deadline=deadline,require_predeadline=True)
        phase=snapshot['phase'];number=int(phase.rsplit('_',1)[1]);assert metadata['seed_records'][number]==seed
        if phase.startswith('raw_seed_'):assert snapshot['routes']==seed['raw_snapshot']['routes']
        else:
            assert phase.startswith('repair_seed_')
            entry=metadata['repair_records'][number]
            assert entry['returned'] and entry['metadata']['returned_checkpoint_checked_within_deadline']
            assert snapshot['routes']==entry['returned_routes']
        refs.append(dict(snapshot_sha256=ledger.digest(snapshot),**_compact(proof['core_reference'])))
    guard=metadata['initial_key'];final=metadata['input_snapshot']
    for accepted in metadata['accepted_moves']:
        snapshot=accepted['full_snapshot'];assert snapshot in snapshots
        assert accepted['source']==snapshot['phase'] and accepted['seed_routes_sha256']==snapshot['seed_routes_sha256']
        assert accepted['full_verified_at_perf_counter']==snapshot['full_verified_at_perf_counter']
        assert accepted['full_verified_at_perf_counter']<=accepted['accepted_at_perf_counter']<deadline
        assert tuple(snapshot['key'])<tuple(guard);guard=snapshot['key'];final=snapshot
    assert metadata['accepted_improvements']==len(metadata['accepted_moves'])
    _bind(metadata['final_key'],guard,'strict original final guarded key')
    _bind(metadata['final_snapshot'],final,'actual final complete guard source')
    return dict(raw_method_evidence=rawproof,proposal_references=refs,unique_snapshots=len({r['snapshot_sha256'] for r in refs}),
        repair_evidence=repairs,paid_shared_raw_diagnostic_only=True,strong_fresh_comparison=False)

def audit(output):
    output=Path(output).resolve()
    assert not (output/'audit.json').exists(),'Never overwrite an existing completed audit'
    manifest=ledger.read(output/'manifest.json')
    ledger.verify(output,manifest,current_execution=False)
    for name in ('audit_split_feeder.py','audit_split_feeder_v2.py','audit_split_feeder_v3.py','audit_split_feeder_v4.py','audit_split_feeder_batch_v4.py','audit_deadline_window_repooling.py','audit_repooling.py','audit_window_damage_repair.py','audit_weighted_relay_ablation.py','audit_weighted_direct_pair.py','audit_critical_prefix_joint_repair_v1.py','audit_critical_prefix_batch_v1.py'):
        assert ledger.sha(ledger.EXPERIMENTS/name)==manifest['execution_sources'][name]['sha256'],name
    tasks=load_official_csv(ledger.ROOT/manifest['input_path']);rows=ledger.read(output/'runs.json')
    assert len(rows)==len(manifest['schedule'])>0
    results=[];paired={}
    for row,cell in zip(rows,manifest['schedule']):
        assert all(row[name]==cell[name] for name in ('run_id','arm','seed'))
        assert row['arm'] in ledger.CRITICAL_PREFIX_PAIR and row['valid'] and row['budget_compliant']
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
        if all((seed,arm) in paired for arm in ledger.CRITICAL_PREFIX_PAIR):
            direct,relay=(paired[(seed,arm)] for arm in ledger.CRITICAL_PREFIX_PAIR)
            assert direct['configuration']==relay['configuration']
            rawd,rawr=direct['raw_metadata'],relay['raw_metadata']
            completed=bool(rawd and rawr and rawd['selector_completed'] and rawr['selector_completed'])
            if completed:
                assert rawd['selected_families']==rawr['selected_families'] and rawd['selector_sha256']==rawr['selector_sha256']
            comparisons.append(dict(seed=seed,arms=list(ledger.CRITICAL_PREFIX_PAIR),both_raw_selectors_completed=completed,
                same_selected_families=True if completed else None,paid_shared_raw_only=True,strongest_fresh_baseline=False,
                selector_sha256=rawd['selector_sha256'] if completed else None,
                seed_channels_by_arm=[[s['channel'] for s in m['seed_records']] for m in (direct,relay)],
                same_total_budget_not_same_candidate_count=True))
    result=dict(passed=True,manifest_sha256=ledger.sha(output/'manifest.json'),runs_sha256=ledger.sha(output/'runs.json'),
        results=results,selector_comparisons=comparisons)
    ledger.write(output/'audit.json',result)
    print(dict(passed=True,runs=len(rows),proposal_references=sum(len(r['method_evidence']['proposal_references']) for r in results)))
    return result
