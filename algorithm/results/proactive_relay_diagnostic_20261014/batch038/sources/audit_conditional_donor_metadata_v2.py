"""Same conditional proof, with one request-local registry for visit identity.

Only repeated immutable geometry parsing inside the pair-removal comprehension
is removed. All physical schedules and catalogue expected values use the
frozen independent v1 auditors. This is audit efficiency, not solver gain.
"""
from math import isfinite
try:
    from .audit_conditional_donor_repair_v1 import (
        PIN_SCOPE,_bind,_full,_digest,_geometry,_identity,require,
        audit_catalogue,audit_step,audit_checkpoint)
except ImportError:
    from audit_conditional_donor_repair_v1 import (
        PIN_SCOPE,_bind,_full,_digest,_geometry,_identity,require,
        audit_catalogue,audit_step,audit_checkpoint)

def audit_metadata(tasks, topology, base_routes, original_raw_routes, metadata, *, seed_move=None,
                    focus_task_ids=(), relay_enabled, weights, scales, deadline):
    """Replay saved complete states/edges and honest unfinished-step evidence.

    Every declared donor proxy is rebuilt.  Only stored complete search states
    have independent entity replays; counters for discarded full probes remain
    production telemetry, with accounting constraints rather than invented
    route proofs. The returned evidence contains hashes/counts, not route copies.
    """
    require(isinstance(metadata, dict), 'Missing actual conditional helper metadata')
    _bind(metadata.get('pin_scope'), PIN_SCOPE, 'conditional helper scope')
    _bind(metadata.get('relay_enabled'), relay_enabled, 'conditional helper arm')
    _bind(metadata.get('original_seed_move'), seed_move, 'conditional helper raw provenance')
    _bind(metadata.get('focus_task_ids'), list(focus_task_ids), 'conditional helper actual focus')
    _bind(metadata.get('deadline_perf_counter'), deadline, 'conditional helper actual deadline')
    raw_proof = _full(tasks, topology, base_routes, original_raw_routes, original_raw_routes, seed_move, relay_enabled, focus_task_ids)
    pins = raw_proof['pinned_task_ids'];_bind(metadata.get('pinned_task_ids'), pins, 'conditional actual relay identities')
    config = metadata.get('configuration');require(isinstance(config, dict), 'Missing declared conditional repair bounds')
    for field, maximum in (('critical_limit', 6), ('donor_limit', 3), ('position_limit', 256), ('option_count', 3),
                           ('beam_width', 16), ('max_steps', 8), ('snapshot_limit', 64)):
        require(type(config.get(field)) is int and 1 <= config[field] <= maximum, 'Invalid conditional bound ' + field)
    require(config['option_count'] <= config['position_limit'], 'Conditional position frontier exceeds its true surface cap')
    for field, expected in (('task_layer_size', 2), ('whole_task_exchange', 't:a_to_b,donor:b_to_a'),
        ('first_partial_J_pruning', False), ('state_comparison', 'complete_same_all_task_coverage'),
        ('position_scores_are_bounds', False), ('all_PD_positions_globally_scored', False),
        ('each_complete_proposal_standard_full_checked', True), ('ordinary_DIRECT_owner_and_order_free', True), ('new_relay_generation', False)):
        _bind(config.get(field), expected, 'conditional declared policy ' + field)
    if not metadata.get('initialization_completed'):
        require(metadata.get('initialization_completed') is False and metadata.get('deadline_reached') is True,
                'An unpublished initialization requires its honest deadline failure')
        require(all(metadata.get(field) is None for field in ('input_snapshot', 'post_repair_snapshot', 'first_complete_candidate',
                'best_constructed_candidate', 'best_key', 'final_routes_sha256')), 'Expired initialization fabricated a complete checkpoint')
        require(metadata.get('steps') == [] and metadata.get('full_candidate_snapshots') == []
                and metadata.get('accepted_improvements') == 0 and metadata.get('returned_input_guard') is True
                and metadata.get('returned_checkpoint_checked_within_deadline') is False,
                'Expired initialization cannot adopt or return a newly checked state')
        return dict(passed=True, initialization_completed=False, predeadline_checkpoint_verified=False,
            saved_complete_state_references=[], catalogue_references=[], saved_complete_states_replayed=0,
            complete_transition_edges_replayed=0, all_complete_probes_entity_replayed=False)
    initial = metadata.get('input_snapshot');require(isinstance(initial, dict), 'Completed initialization lost its complete input checkpoint')
    _bind(initial.get('routes'), original_raw_routes, 'genuine original raw helper checkpoint')
    require(initial.get('state_id') == 0 and initial.get('parent_state_id') is None and initial.get('exchange') is None
            and initial.get('inserted_task_order') == [] and initial.get('owner_choices') == [] and initial.get('insertions') == []
            and initial.get('partial_quota_slots') is None and initial.get('parent_routes_sha256') is None,
            'The input checkpoint cannot claim an invented conditional parent edge')
    input_proof = audit_checkpoint(tasks, topology, base_routes, original_raw_routes, initial['routes'], seed_move,
        relay_enabled=relay_enabled, telemetry=initial, focus_task_ids=focus_task_ids, weights=weights, scales=scales, deadline=deadline)
    registry = _geometry(tasks, topology)[2]
    states = {0: initial};state_refs = {};edge_refs = {};catalogue_refs = [];all_checkpoints = []
    def reference(snapshot, parent=None, directory=None, parent_record=None):
        require(isinstance(snapshot, dict) and type(snapshot.get('state_id')) is int and snapshot['state_id'] >= 0,
                'A saved complete beam state needs its actual integer state ID')
        sid = snapshot['state_id'];digest = _digest(snapshot)
        if sid in states: _bind(snapshot, states[sid], 'one actual complete state ID cannot identify different entities')
        if digest in state_refs: return state_refs[digest]
        if parent is None:
            require(sid == 0, 'A new complete state lost its actual complete parent')
            proof = input_proof
        else:
            require(snapshot.get('parent_state_id') == parent['state_id'] and sid > parent['state_id'], 'Conditional child parent ID is not its actual earlier state')
            exchange = snapshot.get('exchange');require(isinstance(exchange, dict), 'A noninput complete state lost its actual conditional pair')
            record_index = exchange.get('catalogue_record_index')
            require(type(record_index) is int and 0 <= record_index < len(directory['records']), 'Complete state references an undeclared conditioned donor')
            chosen = directory['records'][record_index]
            for field, expected in (('t', chosen['t']), ('d', chosen['donor_task_id']), ('a', chosen['a']), ('b', chosen['b'])):
                _bind(exchange.get(field), expected, 'complete state actual catalogue pair ' + field)
            matching = [p for p in parent_record['pairs'] if p['catalogue_record_index'] == record_index]
            require(len(matching) == 1, 'A saved complete state did not actually start its declared pair probe')
            step_record = {**exchange, **{field: snapshot[field] for field in ('inserted_task_order', 'owner_choices', 'insertions', 'partial_quota_slots', 'parent_routes_sha256')}}
            proof = audit_step(tasks, topology, base_routes, original_raw_routes, parent['routes'], snapshot['routes'], seed_move,
                relay_enabled=relay_enabled, step_record=step_record, telemetry=snapshot, focus_task_ids=focus_task_ids,
                weights=weights, scales=scales, deadline=deadline)
            edge_refs[sid] = dict(state_id=sid, parent_state_id=parent['state_id'], t=proof['t'], d=proof['d'],
                from_aircraft=proof['a'], conditioned_recipient=proof['b'], proof_sha256=_digest(proof),
                routes_sha256=proof['routes_sha256'], actual_freed_quota_slots=proof['quota_slots_by_aircraft'])
        value = dict(state_id=sid, snapshot_sha256=digest, routes_sha256=_digest(snapshot['routes']), score=proof['score'],
            full_clock_sha256=_digest(dict(event_times_min=proof['event_times_min'], delivery_times_min=proof['delivery_times_min'])),
            full_verified_at_perf_counter=snapshot['full_verified_at_perf_counter'])
        state_refs[digest] = value;states[sid] = snapshot
        return value
    reference(initial)
    previous = [initial];steps = metadata.get('steps');require(isinstance(steps, list) and len(steps) <= config['max_steps'], 'Conditional step count exceeds its actual cap')
    generated = verified_saved = completed_steps = 0
    for number, step in enumerate(steps):
        require(isinstance(step, dict) and step.get('step_index') == number, 'Conditional complete-step index differs')
        _bind(step.get('parent_state_ids'), [s['state_id'] for s in previous], 'actual previous complete beam parents')
        parents = step.get('parent_records');retained = step.get('retained_states');checkpoints = step.get('complete_checkpoints')
        require(isinstance(parents, list) and isinstance(retained, list) and isinstance(checkpoints, list), 'Missing actual step evidence collections')
        require(len(parents) <= len(previous) and len(retained) <= config['beam_width'], 'Actual parent/beam coverage exceeds its cap')
        contexts = {};step_generated = 0
        for parent_index, record in enumerate(parents):
            parent = previous[parent_index]
            _bind(record.get('parent_state_id'), parent['state_id'], 'conditional actual parent prefix')
            directory = record.get('catalogue')
            if directory is None:
                require(metadata.get('deadline_reached') is True and record.get('completed') is False
                        and not record.get('pairs') and parent_index == len(parents) - 1 and number == len(steps) - 1,
                        'An unfinished unpublished catalogue must be the honest terminal deadline prefix')
                continue
            cp = audit_catalogue(tasks, topology, base_routes, parent['routes'], directory, pinned_task_ids=pins,
                seed_move=seed_move, relay_enabled=relay_enabled, focus_task_ids=focus_task_ids,
                weights=weights, scales=scales, original_raw_routes=original_raw_routes)
            _bind(record.get('catalogue_statistics'), directory['statistics'], 'actual completed catalogue telemetry alias')
            _bind(directory['configuration']['critical_limit'], config['critical_limit'], 'actual critical catalogue cap')
            _bind(directory['configuration']['donor_limit'], config['donor_limit'], 'actual conditional donor cap')
            catalogue_refs.append(dict(step_index=number, parent_state_id=parent['state_id'], **cp))
            contexts[parent['state_id']] = (parent, directory, record)
            pairs = record.get('pairs');require(isinstance(pairs, list) and len(pairs) <= len(directory['records']), 'Probe prefix exceeds its actual donor catalogue')
            for pair_index, pair in enumerate(pairs):
                proposal = directory['records'][pair_index]
                for field, expected in (('catalogue_record_index', pair_index), ('t', proposal['t']), ('d', proposal['donor_task_id']), ('a', proposal['a']), ('b', proposal['b']),
                                        ('partial_quota_slots', proposal['removal_quota_slots_by_aircraft'])):
                    _bind(pair.get(field), expected, 'actual conditioned pair-probe prefix ' + field)
                removed = [[v for v in r if _identity(v, registry) not in {pair['t'], pair['d']}] for r in parent['routes']]
                _bind(pair.get('partial_routes_sha256'), _digest(removed), 'actual pair removal hash')
                orders = [[pair['t'], pair['d']], [pair['d'], pair['t']]]
                finished_orders = pair.get('insertion_orders_completed')
                require(isinstance(finished_orders, list) and finished_orders == orders[:len(finished_orders)] and len(finished_orders) <= 2,
                        'Pair insertion-order completion is not the actual two-order prefix')
                if pair.get('completed') is True: require(finished_orders == orders, 'Completed pair did not finish both whole-job insertion orders')
                else: require(metadata.get('deadline_reached') is True and pair_index == len(pairs) - 1 and parent_index == len(parents) - 1
                              and number == len(steps) - 1, 'An incomplete pair must be the terminal deadline prefix')
                count = pair.get('candidate_count')
                require(type(count) is int and 0 <= count <= 2 * config['option_count'] ** 2, 'Impossible complete two-task proposal count')
                step_generated += count
            if record.get('completed') is True:
                require(len(pairs) == len(directory['records']) and all(p['completed'] for p in pairs), 'Completed parent omitted conditioned pairs')
            else:
                require(metadata.get('deadline_reached') is True and parent_index == len(parents) - 1 and number == len(steps) - 1,
                        'An unfinished parent must be an honest terminal deadline prefix')
        _bind(step.get('candidate_count'), step_generated, 'actual full-coverage proposal accounting');generated += step_generated
        for snapshot in checkpoints:
            require(snapshot.get('parent_state_id') in contexts, 'Complete checkpoint lost its actual catalogue parent')
            parent, directory, record = contexts[snapshot['parent_state_id']]
            reference(snapshot, parent, directory, record);all_checkpoints.append(snapshot)
        for snapshot in retained:
            require(snapshot.get('parent_state_id') in contexts, 'Retained complete beam lost its actual catalogue parent')
            parent, directory, record = contexts[snapshot['parent_state_id']]
            reference(snapshot, parent, directory, record)
        require(len({_digest(s['routes']) for s in retained}) == len(retained), 'A complete beam duplicated route states')
        require([(tuple(s['key']), tuple(map(tuple, s['routes']))) for s in retained]
                == sorted((tuple(s['key']), tuple(map(tuple, s['routes']))) for s in retained), 'The retained complete beam is not sorted by its actual full key')
        if step.get('completed') is True:
            require(len(parents) == len(previous) and all(p['completed'] for p in parents), 'Completed step lost actual parent/candidate coverage')
            completed_steps += 1
        else:
            require(metadata.get('deadline_reached') is True and number == len(steps) - 1, 'An unfinished complete beam must be a terminal deadline prefix')
        previous = retained
    _bind(metadata.get('complete_proposals_generated'), generated, 'actual generated full-coverage proposal count')
    _bind(metadata.get('completed_steps'), completed_steps, 'actual completed search-step count')
    full_verified = metadata.get('complete_leafs_full_verified');rejected = metadata.get('full_validator_rejections')
    require(type(full_verified) is int and type(rejected) is int and 0 <= full_verified <= generated and 0 <= rejected <= generated - full_verified,
            'Full-validator telemetry exceeds actual complete proposal coverage')
    require(len(edge_refs) <= full_verified, 'More stored complete entities than actual full verifications')
    if 'complete_full_validator_calls' in metadata:
        calls = metadata['complete_full_validator_calls']
        require(type(calls) is int and full_verified + rejected <= calls <= generated, 'Actual full-call accounting is inconsistent')
    if not metadata.get('deadline_reached'):
        require(full_verified + rejected == generated, 'Finished complete probes lack standard full-check accounting')
    expected_saved = all_checkpoints[:config['snapshot_limit']]
    _bind(metadata.get('full_candidate_snapshots'), expected_saved, 'actual bounded chronological checkpoint snapshots')
    _bind(metadata.get('first_complete_candidate'), all_checkpoints[0] if all_checkpoints else None, 'actual first complete candidate')
    best = initial;improvements = 0;last_best = None
    for snapshot in all_checkpoints:
        if tuple(snapshot['key']) < tuple(best['key']):
            require('selected_as_best_at_perf_counter' in snapshot, 'A selected internal best lost its deadline evidence')
            best = snapshot;last_best = snapshot;improvements += 1
        else:
            require('selected_as_best_at_perf_counter' not in snapshot, 'A nonimproving complete checkpoint falsely claims best selection')
    _bind(metadata.get('accepted_improvements'), improvements, 'actual internal full best-chain count')
    _bind(metadata.get('best_constructed_candidate'), last_best, 'actual best constructed candidate source')
    _bind(metadata.get('post_repair_snapshot'), best, 'actual returned predeadline complete best')
    _bind(metadata.get('best_key'), best['key'], 'actual returned full best key')
    _bind(metadata.get('final_routes_sha256'), _digest(best['routes']), 'actual returned full route hash')
    _bind(metadata.get('returned_input_guard'), improvements == 0, 'actual input-guard return flag')
    require(metadata.get('returned_checkpoint_checked_within_deadline') is True, 'A published initialized return must identify its checked complete checkpoint')
    return dict(passed=True, initialization_completed=True, predeadline_checkpoint_verified=True,
        saved_complete_state_references=list(state_refs.values()), complete_transition_edges=list(edge_refs.values()),
        catalogue_references=catalogue_refs, saved_complete_states_replayed=len(state_refs),
        complete_transition_edges_replayed=len(edge_refs), stored_checkpoints_replayed=len(all_checkpoints),
        generated_complete_proposals=generated, production_reported_full_verified=full_verified,
        production_reported_full_rejections=rejected, actual_internal_best_improvements=improvements,
        returned_score=best['score'], returned_routes_sha256=_digest(best['routes']),
        all_declared_donor_proxies_replayed=True, all_complete_probes_entity_replayed=False)
