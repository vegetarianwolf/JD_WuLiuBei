"""Independent saved-evidence audit for paired window/damage research batches.

This orchestration imports only standard-library physical auditors. Search,
closure selection and repair code are never executed to validate their output.
"""
from __future__ import annotations

from pathlib import Path

try:
    from . import run_proactive_relay_research as ledger
    from .audit_deadline_window_repooling import (
        audit_deadline_window_family, audit_deadline_window_repooling_proposal, _bind)
    from .audit_weighted_direct_pair import load_official_csv
    from .audit_weighted_relay_ablation import audit_payload, direct_counterfactual, reference_schedule, _geometry
    from .audit_repooling import _owners, _identity, _direct_task
    from .audit_window_damage_repair import audit_window_damage_repair_proposal, audit_window_damage_closure, audit_window_damage_partial
except ImportError:
    import run_proactive_relay_research as ledger
    from audit_deadline_window_repooling import (
        audit_deadline_window_family, audit_deadline_window_repooling_proposal, _bind)
    from audit_weighted_direct_pair import load_official_csv
    from audit_weighted_relay_ablation import audit_payload, direct_counterfactual, reference_schedule, _geometry
    from audit_repooling import _owners, _identity, _direct_task
    from audit_window_damage_repair import audit_window_damage_repair_proposal, audit_window_damage_closure, audit_window_damage_partial


def _physical(score):
    return dict(late_count=score['late_count'],total_lateness_min=float(score['total_lateness_min']),distance_km=float(score['distance_km']))


def _key(score, manifest):
    score = _physical(score)
    values = [score[name] for name in ledger.FIELDS]
    return [sum(w*v/s for w,v,s in zip(manifest['weights'], values, manifest['scales'])), *values]


def _compact(proof):
    return dict(passed=True, score=proof['score'], full_replay_output_sha256=ledger.digest(proof))


def _raw(tasks, topology, base, stage, relay, manifest):
    """Recheck all real input-to-raw witnesses, even unused family previews."""
    assert ledger.digest(stage['selected_families']) == stage['selector_sha256']
    assert stage['families_examined'] == len(stage['family_records'])
    assert [r['family'] for r in stage['family_records']] == stage['selected_families'][:stage['families_examined']]
    cfg = stage['configuration']
    assert cfg['receiver_all_empty_gaps_scored'] and cfg['direct_pair_all_empty_gaps_scored']
    assert cfg['direct_final_map_count'] == 12 and cfg['polish_enabled'] is False
    family_refs = []
    for family in stage['selected_families']:
        proof = audit_deadline_window_family(tasks, topology, base, family)
        assert proof['passed'] and proof['complete_solution'] is False
        family_refs.append(dict(family_sha256=ledger.digest(family), full_replay_output_sha256=ledger.digest(proof)))
    if not relay:
        for record in stage['family_records']:
            ids = record['family']['task_ids']
            maps = [[[t], [q], [k for k in ids if k not in (t,q)]] for t in ids for q in ids if t != q]
            considered, completed = record['direct_maps_considered'], record['direct_maps_completed']
            assert considered == maps[:len(considered)] and len(considered) <= 12
            assert completed == considered[:len(completed)]
            if record['direct_map_enumeration_completed']:
                assert completed == maps and record['complete_table_enumeration_completed']
    snapshots = [(name, stage.get(name)) for name in ('input_snapshot', 'first_complete_candidate', 'best_constructed_candidate')]
    snapshots += [(f'full_candidate_{i}', s) for i,s in enumerate(stage['full_candidate_snapshots'])]
    references, cache = [], {}
    for name, snapshot in snapshots:
        if snapshot is None:
            continue
        assert snapshot['valid'] is True and snapshot['complete'] is True
        digest = ledger.digest(snapshot)
        if digest not in cache:
            if snapshot.get('move') is None:
                assert snapshot['routes'] == [list(r) for r in base]
                proof = reference_schedule(tasks, snapshot['routes'], topology)
                _bind(snapshot['score'], _physical(proof['score']), 'raw input score')
                _bind(snapshot['event_times_min'], proof['event_times_min'], 'raw input events')
                _bind(snapshot['delivery_times_min'], proof['delivery_times_min'], 'raw input deliveries')
                assert snapshot['cross_uav_count'] == snapshot['relay_task_count'] == 0
            else:
                assert snapshot['move']['family'] in stage['selected_families']
                proof = audit_deadline_window_repooling_proposal(tasks, topology, base, snapshot['routes'], snapshot['move'],
                    relay_enabled=relay, telemetry=snapshot, weights=manifest['weights'], scales=manifest['scales'])
            _bind(snapshot['key'], _key(proof['score'], manifest), 'raw weighted key')
            cache[digest] = _compact(proof)
        references.append(dict(proposal=name, snapshot_sha256=digest, **cache[digest]))
    for accepted in stage['accepted_moves']:
        assert accepted['full_verified_at_perf_counter'] <= accepted['accepted_at_perf_counter'] < stage['deadline_perf_counter']
        assert accepted['full_snapshot'] in stage['full_candidate_snapshots']
    assert stage['accepted_improvements'] == len(stage['accepted_moves'])
    return dict(raw_references=references, family_references=family_refs, unique_raw_snapshots=len(cache))


def _seed_candidates(stage):
    """Own-arm true pooled channel, with deterministic route deduplication."""
    unique = {}
    for snapshot in stage['full_candidate_snapshots']:
        if snapshot.get('pooled_rescued_task_ids'):
            route = tuple(tuple(r) for r in snapshot['routes'])
            if route not in unique:
                unique[route] = snapshot
    ranked = sorted(unique.values(), key=lambda s:(tuple(s['key']), tuple(tuple(r) for r in s['routes'])))
    if not ranked:
        return []
    selected = [ranked[0]]
    remaining = [s for s in ranked if s['routes'] != selected[0]['routes']]
    if remaining:
        selected.append(min(remaining, key=lambda s:(-len(s['pooled_rescued_task_ids']), tuple(s['key']), tuple(tuple(r) for r in s['routes']))))
    return selected


def _damage_rows(tasks,topology,base_routes,before_routes,closure,move):
    """Bind the damage ranking inputs to two actual complete schedules."""
    base = reference_schedule(tasks,base_routes,topology)
    current = reference_schedule(tasks,before_routes,topology)
    registry = _geometry(tasks,topology)[2]
    old_owners,new_owners = _owners(base_routes,registry),_owners(before_routes,registry)
    family = move['family'];pins = {family['i'],family['j']};comp = {family['k1'],family['k2']}
    evidence = closure['evidence'];rows = evidence['per_task']
    assert len(rows) == len(tasks)-2 and {r['task_id'] for r in rows} == set(tasks)-pins
    _bind(evidence['base_score'],_physical(base['score']),'damage base score')
    _bind(evidence['raw_score'],_physical(current['score']),'damage actual current score')
    assert evidence['ranking_is_gain_or_bound'] is False and evidence['all_damage_metrics_use_current_full_DAG'] is True
    assert set(evidence['compensation_task_ids']) == comp
    for row in rows:
        task = row['task_id'];due = tasks[task][2]
        old,new = base['delivery_times_min'][str(task)],current['delivery_times_min'][str(task)]
        old_late = old-due if old-due > 1e-9 else 0.
        new_late = new-due if new-due > 1e-9 else 0.
        expected = dict(base_aircraft=old_owners[task],raw_aircraft=new_owners[task],owner_changed=old_owners[task]!=new_owners[task],
            deadline_min=due,base_delivery_min=old,raw_delivery_min=new,base_lateness_min=old_late,raw_lateness_min=new_late,
            delta_lateness_min=new_late-old_late,positive_delta_lateness_min=max(0.,new_late-old_late),
            newly_late=old_late==0. and new_late>0.,positive_delivery_delay_min=max(0.,new-old),is_supplied_compensation=task in comp)
        for name,value in expected.items():
            _bind(row[name],value,'damage task '+str(task)+' '+name)


def audit(output):
    output = Path(output)
    manifest = ledger.read(output/'manifest.json')
    ledger.verify(output, manifest, current_execution=False)
    for name in ('audit_window_damage_batch.py','audit_window_damage_repair.py','audit_deadline_window_repooling.py',
                 'audit_weighted_relay_ablation.py','audit_weighted_direct_pair.py'):
        assert ledger.sha(ledger.EXPERIMENTS/name) == manifest['execution_sources'][name]['sha256'], name
    rows = ledger.read(output/'runs.json')
    assert len(rows) == len(manifest['schedule']) > 0
    tasks = load_official_csv(ledger.ROOT/manifest['input_path'])
    results, paired, contexts = [], {}, {}
    for row, cell in zip(rows, manifest['schedule']):
        assert all(row[k] == cell[k] for k in ('run_id','arm','seed'))
        assert row['arm'] in ledger.DAMAGE_PAIR and row['valid'] and row['budget_compliant']
        assert 0 <= row['end_to_end_seconds'] <= manifest['budget_seconds']
        assert row['source_sha256'] == manifest['source_archive']['sha256']
        assert row['initial_routes_sha256'] == manifest['initial_routes_sha256']
        receipt = ledger.read(output/'receipts'/(row['run_id']+'.json'))
        response = output/'receipts'/(row['run_id']+'__response.json')
        assert receipt['status'] == 'completed' and receipt['manifest_sha256'] == ledger.sha(output/'manifest.json')
        assert receipt['response_sha256'] == ledger.sha(response) and ledger.read(response) == row
        relay = row['arm'].endswith('_relay')
        topology = {**manifest['topology'], 'relay_allowed':relay}
        before, previous_key, previous_hash = manifest['initial_routes'], None, manifest['initial_routes_sha256']
        phase_refs, raw_reference, posts, metadata, method_input = [], None, [], None, None
        for phase in row['phases']:
            assert ledger.sha(output/phase['solution_path']) == phase['solution_sha256']
            payload = ledger.read(output/phase['solution_path'])
            assert {**phase['topology'],'relay_allowed':relay} == topology
            phase_proof = audit_payload(tasks, topology, payload)
            routes = [r['encoded_visits'] for r in payload['routes']]
            assert ledger.digest(routes) == phase['routes_sha256'] and phase['input_routes_sha256'] == previous_hash
            _bind(phase['score'], _physical(phase_proof['score']), 'phase score')
            current_key = _key(phase_proof['score'], manifest)
            assert abs(current_key[0]-phase['weighted_score']) < 1e-10
            if previous_key is not None:
                assert tuple(current_key) <= tuple(previous_key)
            if phase['phase'] == 'initial':
                assert ledger.digest(routes) == manifest['initial_routes_sha256']
                _bind(phase_proof['score'], _physical(manifest['initial_score']), 'initial score')
            if phase['phase'] == row['arm']:
                assert metadata is None
                metadata, method_input = phase['metadata'], before
                raw = metadata['raw_stage']
                assert metadata['seed'] == row['seed'] and raw['seed'] == row['seed']
                assert raw['deadline_perf_counter'] == metadata['proposal_deadline_perf_counter'] < metadata['deadline_perf_counter']
                cfg = metadata['configuration']
                assert cfg['proposal_margin'] is None and cfg['genuine_pooled_channel_has_no_J_margin_gate'] is True
                assert cfg['polish_uses_ALNS'] is False and cfg['ordinary_tasks_direct_only'] is True
                assert 1 <= cfg['candidate_limit'] <= 2 and 0 < cfg['proposal_fraction'] < 1
                for name,value in manifest.get('method_parameters',{}).get(row['arm'],{}).items():
                    actual_parameter = cfg[name] if name in cfg else cfg['raw_parameters'][name]
                    _bind(actual_parameter,value,'frozen method parameter '+name)
                assert metadata['seed_selection_policy'] == 'genuine_pooled_first_positive_route_dedup_minJ_then_max_pooled_scan_remaining'
                raw_reference = _raw(tasks, topology, before, raw, relay, manifest)
                assert metadata['selected_families'] == raw['selected_families']
                assert metadata['selector_sha256'] == raw['selector_sha256']
                assert metadata['selector_completed'] == raw['selector_completed']
                expected_seeds = _seed_candidates(raw)[:metadata['configuration']['candidate_limit']]
                seed_records = metadata['seed_selection_records']
                assert len(seed_records) <= len(expected_seeds)
                if metadata['seed_selection_completed']:
                    assert len(seed_records) == len(expected_seeds)
                    assert metadata['seed_selection_completed_at_perf_counter'] < metadata['deadline_perf_counter']
                for n,(seed_record,snapshot) in enumerate(zip(seed_records,expected_seeds)):
                    assert raw['full_candidate_snapshots'][seed_record['raw_snapshot_index']] == snapshot
                    assert seed_record['full_proposal_sha256'] == ledger.digest(snapshot)
                    assert seed_record['routes_sha256'] == ledger.digest(snapshot['routes'])
                    _bind(seed_record['key'],snapshot['key'],'own-arm pooled seed key')
                    _bind(seed_record['pooled_rescued_task_ids'],snapshot['pooled_rescued_task_ids'],'own-arm genuine pooled seeds')
                    assert seed_record['selection_channel'] == ('minimum_true_J_genuine_pooled' if n == 0 else 'maximum_pooled_then_J_distinct_route')
                records = metadata['polish_records']
                assert [r['full_proposal'] for r in records] == expected_seeds[:len(records)]
                assert len(records) <= len(expected_seeds) <= 2
                assert metadata['polish_calls'] == len(records)
                cache = {}
                for index, record in enumerate(records):
                    macro, move = record['full_proposal'], record['original_macro_move']
                    assert macro in expected_seeds and move == macro['move'] and record['macro_routes'] == macro['routes']
                    assert macro['pooled_rescued_task_ids']
                    assert record['leaf_started_at_perf_counter'] < record['leaf_deadline_perf_counter'] <= metadata['deadline_perf_counter']
                    helper = record['helper_metadata']
                    family = move['family']
                    assert helper['pin_scope'] == 'window_damage_repair'
                    _bind(helper['pinned_task_ids'],sorted((family['i'],family['j'])),'helper actual pinned tasks')
                    _bind(helper['released_compensation_task_ids'],sorted((family['k1'],family['k2'])),'helper released compensation tasks')
                    assert record['leaf_started_at_perf_counter'] < record['helper_deadline_perf_counter'] <= record['leaf_deadline_perf_counter']
                    assert helper['deadline_perf_counter'] == record['helper_deadline_perf_counter']
                    assert abs(record['helper_deadline_perf_counter']-(record['leaf_deadline_perf_counter']-record['guard_reserve_seconds'])) < 1e-8
                    snapshots = [(name,helper.get(name)) for name in ('input_snapshot','first_complete_candidate','best_constructed_candidate','post_repair_snapshot')]
                    snapshots += [(f'full_candidate_{i}',s) for i,s in enumerate(helper.get('full_candidate_snapshots',[]))]
                    snapshots.append(('post_polish',record.get('post_polish')))
                    for name, snapshot in snapshots:
                        if snapshot is None:
                            continue
                        digest = ledger.digest(snapshot)
                        if digest not in cache:
                            post = audit_window_damage_repair_proposal(tasks,topology,before,macro['routes'],snapshot['routes'],move,
                                relay_enabled=relay,telemetry=snapshot,weights=manifest['weights'],scales=manifest['scales'])
                            cache[digest] = _compact(post)
                        posts.append(dict(leaf=index,proposal=name,snapshot_sha256=digest,**cache[digest]))
                    before_routes = macro['routes']
                    for closure_record in helper['closure_records']:
                        before_routes = closure_record['before_routes']
                        # A later destroy may start from an already verified repair,
                        # but it cannot silently change either pinned plan.
                        audit_window_damage_repair_proposal(tasks,topology,before,macro['routes'],before_routes,move,
                            relay_enabled=relay)
                        closure = closure_record['closure']
                        closure_proof = audit_window_damage_closure(tasks,topology,before_routes,closure,
                            pinned_task_ids=helper['pinned_task_ids'])
                        _damage_rows(tasks,topology,before,before_routes,closure,move)
                        _bind(closure_record['partial_routes'],closure_proof['partial_routes'],'joint repair removed routes')
                        _bind(closure_record['quota_slots_by_aircraft'],closure_proof['quota_slots_by_aircraft'],'joint actual quota slots')
                        removed = set(closure_proof['removed_task_ids'])
                        slots = closure_proof['quota_slots_by_aircraft']
                        registry = _geometry(tasks,topology)[2]
                        original_owners = _owners(before_routes,registry)
                        for assignment in closure_record['owner_assignments']:
                            owners = assignment['owner_assignment']
                            assert {int(t) for t in owners} == removed
                            assert all(type(a) is int and 0 <= a < len(slots) for a in owners.values())
                            assert [sum(a == owner for owner in owners.values()) for a in range(len(slots))] == slots
                            _bind(assignment['changed_owner_task_ids'],[t for t in closure_proof['removed_task_ids'] if owners[str(t)] != original_owners[t]],'assignment changed owners')
                            _bind(assignment['actual_assigned_aircraft'],sorted(set(owners.values())),'assignment actual aircraft')
                        posts.append(dict(leaf=index,proposal='damage_closure',reference=closure_proof))
                        present = set(tasks)-removed
                        partial = audit_window_damage_partial(tasks,topology,closure_record['partial_routes'],sorted(present),
                            target_final_counts=closure_proof['raw_final_counts'])
                        posts.append(dict(leaf=index,proposal='removed_partial',**_compact(partial)))
                        for layer_index,layer in enumerate(closure_record['layers']):
                            inserted = layer['inserted_task_ids']
                            assert len(inserted) <= 2 and len(set(inserted)) == len(inserted) and set(inserted) <= removed-present
                            present.update(inserted)
                            assert sorted(present) == layer['present_task_ids']
                            states = layer['beam_state_snapshots']
                            assert len(states) == layer['beam_states'] <= helper['configuration']['beam_width']
                            assert sorted({s['assignment_index'] for s in states}) == layer['assignment_families']
                            for state_index,state in enumerate(states):
                                assignment = state['assignment_index']
                                assert type(assignment) is int and 0 <= assignment < len(closure_record['owner_assignments'])
                                partial = audit_window_damage_partial(tasks,topology,state['routes'],sorted(present),telemetry=state,
                                    weights=manifest['weights'],scales=manifest['scales'],target_final_counts=closure_proof['raw_final_counts'])
                                restored = present & removed
                                filtered = [[v for v in r if _identity(v,registry) not in restored] for r in state['routes']]
                                assert filtered == closure_proof['partial_routes'], 'Partial changed retained native/pin events'
                                actual_owners = _owners(state['routes'],registry)
                                owner_map = closure_record['owner_assignments'][assignment]['owner_assignment']
                                assert all(_direct_task(state['routes'],registry,t) and actual_owners[t] == owner_map[str(t)] for t in restored)
                                posts.append(dict(leaf=index,proposal='joint_beam_partial',layer=layer_index,state=state_index,**_compact(partial)))
                        for checkpoint in closure_record['complete_checkpoints']:
                            assert checkpoint['full_verified_at_perf_counter'] < helper['deadline_perf_counter']
                            snapshot = checkpoint['full_snapshot']
                            checkpoint_proof = audit_window_damage_repair_proposal(tasks,topology,before,macro['routes'],snapshot['routes'],move,
                                relay_enabled=relay,telemetry=snapshot,weights=manifest['weights'],scales=manifest['scales'])
                            actual = _owners(snapshot['routes'],registry)
                            assignment_index = checkpoint['assignment_index']
                            assert type(assignment_index) is int and 0 <= assignment_index < len(closure_record['owner_assignments'])
                            owner_map = closure_record['owner_assignments'][assignment_index]['owner_assignment']
                            assert all(actual[t] == owner_map[str(t)] for t in removed)
                            assert [[v for v in r if _identity(v,registry) not in removed] for r in snapshot['routes']] == closure_proof['partial_routes']
                            changed = [t for t in closure_proof['removed_task_ids'] if actual[t] != original_owners[t]]
                            _bind(checkpoint['changed_owner_task_ids'],changed,'complete actual changed owners')
                            involved = {move['family'][name] for name in ('a','b','c')}
                            _bind(checkpoint['owner_change_outside_macro_aircraft_task_ids'],
                                [t for t in changed if original_owners[t] not in involved or actual[t] not in involved],'complete outside-macro owner changes')
                            posts.append(dict(leaf=index,proposal='complete_joint_checkpoint',**_compact(checkpoint_proof)))
                        assert closure_record['complete_leaf_count'] == len(closure_record['complete_checkpoints'])
                        assert closure_record['completed_assignment_families'] == [c['assignment_index'] for c in closure_record['complete_checkpoints']]
                        assert closure_record['uncompleted_assignment_families'] == sorted(set(closure_record['initial_assignment_families'])-set(closure_record['completed_assignment_families']))
                    if record['adopted']:
                        assert record['finished_within_leaf_deadline'] and helper['returned_checkpoint_checked_within_deadline']
                        assert record['full_verified_at_perf_counter'] < record['leaf_deadline_perf_counter']
                assert metadata['accepted_improvements'] == len(metadata['accepted_moves'])
                previous_adopted_key = _key(reference_schedule(tasks,before,topology)['score'],manifest)
                for accepted in metadata['accepted_moves']:
                    snapshot = accepted['full_snapshot']
                    assert accepted['full_verified_at_perf_counter'] <= accepted['accepted_at_perf_counter'] < metadata['deadline_perf_counter']
                    assert tuple(snapshot['key']) < tuple(previous_adopted_key)
                    previous_adopted_key = snapshot['key']
                    if accepted['kind'] == 'raw_macro':
                        assert snapshot in raw['full_candidate_snapshots']
                    else:
                        matching = [r for r in records if r.get('post_polish') == snapshot and r['adopted']]
                        assert matching
                if routes != before:
                    assert metadata['accepted_moves'][-1]['full_snapshot']['routes'] == routes
                for name in ('first_complete_candidate','best_constructed_candidate'):
                    snapshot = metadata.get(name)
                    if snapshot is not None:
                        assert any(r.get('post_polish') == snapshot for r in records), 'Unbound post alias'
                assert metadata['full_candidate_snapshots'] == [r['post_polish'] for r in records if r['post_polish'] is not None]
                _bind(metadata['initial_key'],_key(reference_schedule(tasks,before,topology)['score'],manifest),'method initial key')
                _bind(metadata['final_key'],current_key,'method guarded final key')
                paired[(row['seed'],row['arm'])] = raw
                contexts[(row['seed'],row['arm'])] = metadata
            phase_refs.append(dict(phase=phase['phase'],routes_sha256=phase['routes_sha256'],reference=_compact(phase_proof)))
            previous_key, previous_hash, before = current_key, phase['routes_sha256'], routes
        assert metadata is not None and raw_reference is not None and method_input is not None
        assert row['solution_path'] == row['phases'][-1]['solution_path'] and row['solution_sha256'] == row['phases'][-1]['solution_sha256']
        _bind({k:row[k] for k in ledger.FIELDS}, {k:previous_key[i+1] for i,k in enumerate(ledger.FIELDS)}, 'final row')
        assert abs(row['weighted_score']-previous_key[0]) < 1e-10 and row['on_time_count'] == len(tasks)-row['late_count']
        cf_path = output/row['counterfactual_path']
        assert ledger.sha(cf_path) == row['counterfactual_sha256']
        cf = ledger.read(cf_path)
        assert [r['encoded_visits'] for r in cf['routes']] == [list(r) for r in direct_counterfactual(before,topology)]
        cf_proof = audit_payload(tasks,{**topology,'relay_allowed':False},cf)
        results.append(dict(run_id=row['run_id'],passed=True,phase_references=phase_refs,raw_stage=raw_reference,
            post_references=posts,fixed_structure_direct=_compact(cf_proof)))
    comparisons = []
    for seed in sorted({r['seed'] for r in rows}):
        if all((seed,arm) in paired for arm in ledger.DAMAGE_PAIR):
            direct, relay = (paired[(seed,arm)] for arm in ledger.DAMAGE_PAIR)
            assert direct['selector_completed'] and relay['selector_completed']
            assert direct['selected_families'] == relay['selected_families'] and direct['selector_sha256'] == relay['selector_sha256']
            assert contexts[(seed,ledger.DAMAGE_PAIR[0])]['configuration'] == contexts[(seed,ledger.DAMAGE_PAIR[1])]['configuration']
            for stage in (direct,relay):
                assert stage['selector_completed_at_perf_counter'] < stage['deadline_perf_counter']
            comparisons.append(dict(seed=seed,arms=list(ledger.DAMAGE_PAIR),selectors_completed=True,
                same_selected_families=True,selector_sha256=direct['selector_sha256'],direct_balanced_assignment_space=12,
                own_arm_pooled_seeds=True,same_total_budget_not_same_candidate_count=True))
    result = dict(passed=True,manifest_sha256=ledger.sha(output/'manifest.json'),runs_sha256=ledger.sha(output/'runs.json'),
        results=results,selector_comparisons=comparisons)
    ledger.write(output/'audit.json',result)
    print({'passed':True,'runs':len(rows),'post_references':sum(len(r['post_references']) for r in results)})
    return result
