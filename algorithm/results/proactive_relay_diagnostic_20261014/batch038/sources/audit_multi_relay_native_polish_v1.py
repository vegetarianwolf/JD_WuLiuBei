"""Independent multi-group pin proof with ordinary DIRECT owner/order freedom.

This proves a genuine complete macro and its native continuation. A future
batch wrapper must separately bind the paid construction's provenance. No
search runtime, producer, evaluator or local pricing primitive supplies an
expected score; every saved entity uses the standard-library handoff DAG.
"""
from __future__ import annotations

from math import isfinite

try:
    from . import audit_multi_relay_accumulation_v1 as generic
    from .audit_weighted_relay_ablation import _geometry, reference_schedule
    from .audit_deadline_window_repooling import _bind
    from .audit_weighted_direct_pair import require
    from .audit_split_feeder import _physical, _weighted
    from .audit_repooling import _owners, _identity, _direct_task
    from .audit_pinned_repooling import _plans
except ImportError:
    import audit_multi_relay_accumulation_v1 as generic
    from audit_weighted_relay_ablation import _geometry, reference_schedule
    from audit_deadline_window_repooling import _bind
    from audit_weighted_direct_pair import require
    from audit_split_feeder import _physical, _weighted
    from audit_repooling import _owners, _identity, _direct_task
    from audit_pinned_repooling import _plans

SCOPE = 'multi_relay_native_polish'
PATCH_NAMES = ('destroy_solution', '_all_plans_for_task', '_apply_task_exact', '_repair', '_accept_worse')
_digest = generic._digest


def _context(tasks, topology, macro_routes, groups):
    geometry = _geometry(tasks, topology);reference = reference_schedule(tasks, macro_routes, topology)
    registry = geometry[2]
    local = dict(geometry=geometry, base=reference, base_owners=_owners(macro_routes, registry))
    signature, shape = generic._structure(tasks, topology, macro_routes, groups, local)
    selected = set(signature['protected_task_ids'])
    require(selected and {h['task_id'] for h in reference['handoffs']} == selected,
            'Native multi-relay polish needs its genuine complete nonempty group macro')
    return dict(geometry=geometry, macro_reference=reference, groups=[dict(g) for g in groups],
        signature=signature, selected=selected, counts=signature['final_counts'],
        reference_cache={(tuple(sorted(tasks)), tuple(map(tuple, macro_routes))): reference})


def _reference(tasks, topology, routes, present, context):
    key = tuple(sorted(present)), tuple(map(tuple, routes))
    if key not in context['reference_cache']:
        if set(present) == set(tasks): graph = topology
        else:
            graph = dict(topology)
            graph['legs'] = [leg for leg in topology.get('legs', []) if leg['task_id'] in present]
            if 'task_relay_candidates' in graph:
                graph['task_relay_candidates'] = {str(t): sites for t, sites in topology['task_relay_candidates'].items() if int(t) in present}
        context['reference_cache'][key] = reference_schedule({t: tasks[t] for t in present}, routes, graph)
    return context['reference_cache'][key]


def _prove(tasks, topology, macro_routes, routes, present, context):
    selected = context['selected'];registry = context['geometry'][2]
    require(selected <= set(present) <= set(tasks), 'A partial may remove only unpinned ordinary whole tasks')
    require({ _identity(v, registry) for r in routes for v in r } == set(present), 'Actual partial presence differs from declared tasks')
    reference = _reference(tasks, topology, routes, present, context)
    require({h['task_id'] for h in reference['handoffs']} == selected,
            'Native polish introduced or removed an actual relay plan')
    require(all(_direct_task(routes, registry, t) for t in set(present) - selected), 'All unpinned tasks must remain whole DIRECT')
    plans = _plans(routes, registry, selected);old_plans = _plans(macro_routes, registry, selected)
    require(plans == old_plans, 'Native polish changed selected mode, legs, H or source/final roles')
    events = [[v for v in r if _identity(v, registry) in selected] for r in routes]
    _bind(events, context['signature']['selected_events'], 'all groups selected signed-event subsequences')
    counts = [r['task_count'] for r in reference['route_records']]
    slots = [target - actual for target, actual in zip(context['counts'], counts)]
    require(all(q >= 0 for q in slots) and sum(slots) == len(tasks) - len(present), 'Actual final responsibility slots differ from the missing ordinary tasks')
    if set(present) == set(tasks): require(counts == context['counts'], 'Complete native polish changed per-aircraft final quotas')
    metrics = [generic._group_metrics(tasks, topology, routes, reference, g, context['geometry']) for g in context['groups']]
    owners = _owners(routes, registry)
    return dict(reference=reference, final_counts=counts, slots=slots, metrics=metrics, owners=owners)


def _complete(tasks, topology, macro_routes, routes, context, *, telemetry=None, weights=None, scales=None, deadline=None):
    proof = _prove(tasks, topology, macro_routes, routes, set(tasks), context)
    ref = proof['reference'];selected = context['selected'];registry = context['geometry'][2]
    old_owners = _owners(macro_routes, registry)
    native = set(tasks) - selected
    native_events = lambda rs: [[v for v in r if _identity(v, registry) in native] for r in rs]
    before, after = native_events(macro_routes), native_events(routes)
    result = dict(passed=True, complete_solution=True, pin_scope=SCOPE, groups=context['groups'],
        pinned_task_ids=sorted(selected), pin_signature=context['signature'], final_tasks_per_aircraft=proof['final_counts'],
        group_metrics=proof['metrics'], score=_physical(ref['score']), post_global_score=_physical(ref['score']),
        event_times_min=ref['event_times_min'], delivery_times_min=ref['delivery_times_min'],
        actual_final_owners={str(t): proof['owners'][t] for t in sorted(tasks)},
        current_source_drop_availability_min={str(h['task_id']): h['drop_time_min'] for h in ref['handoffs']},
        current_shared_double_loaded_km_by_group={str(g['group_id']): g['shared_loaded_distance_km'] for g in proof['metrics']},
        selected_native_classifications=generic._classifications(tasks, context['macro_reference'], ref, selected),
        native_owner_changed_task_ids=sorted(t for t in native if proof['owners'][t] != old_owners[t]),
        native_event_sequence_changed_aircraft=[a for a in range(len(routes)) if before[a] != after[a]],
        changed_owner_task_ids=sorted(t for t in native if proof['owners'][t] != old_owners[t]),
        changed_event_sequence_aircraft=[a for a in range(len(routes)) if tuple(macro_routes[a]) != tuple(routes[a])],
        structure_signature=context['signature'], final_counts=proof['final_counts'], group_count=len(context['groups']),
        input_routes_sha256=_digest(macro_routes),
        relay_task_count=len(selected), cross_uav_count=ref['relay_statistics']['cross_uav_handoff_count'],
        routes_sha256=_digest(routes), core_reference=ref)
    if telemetry is not None:
        require(telemetry.get('valid') is True and telemetry.get('complete') is True, 'A saved native checkpoint must be a complete physical entity')
        _bind(telemetry.get('routes'), routes, 'actual complete native checkpoint routes')
        for field in ('pin_scope', 'groups', 'pinned_task_ids', 'structure_signature', 'final_counts', 'group_count',
            'input_routes_sha256', 'group_metrics', 'score', 'post_global_score',
            'event_times_min', 'delivery_times_min', 'actual_final_owners', 'current_source_drop_availability_min',
            'current_shared_double_loaded_km_by_group', 'selected_native_classifications', 'changed_owner_task_ids',
            'changed_event_sequence_aircraft', 'relay_task_count', 'cross_uav_count', 'routes_sha256'):
            _bind(telemetry.get(field), result[field], 'current native multi-group ' + field)
        _weighted(telemetry.get('key'), result['score'], weights, scales, 'actual complete native-polish objective')
        verified, declared = telemetry.get('full_verified_at_perf_counter'), telemetry.get('deadline_perf_counter')
        require(type(verified) in (int, float) and isfinite(verified) and type(declared) in (int, float) and isfinite(declared)
                and verified < declared, 'Native-polish snapshot was not fully verified before its deadline')
        if deadline is not None: _bind(declared, deadline, 'actual native-polish deadline')
        if 'selected_as_best_at_perf_counter' in telemetry:
            at = telemetry['selected_as_best_at_perf_counter']
            require(type(at) in (int, float) and isfinite(at) and verified <= at < declared,
                    'Native-polish best selection missed its full checked deadline')
        for field in ('table_score', 'window', 'independent_delta_sum'):
            require(field not in telemetry, 'A local price cannot certify a complete multi-group continuation')
    return result


def audit_complete_state(tasks, topology, macro_routes, groups, routes, *, telemetry=None, weights=None, scales=None, deadline=None):
    return _complete(tasks, topology, macro_routes, routes, _context(tasks, topology, macro_routes, groups),
                     telemetry=telemetry, weights=weights, scales=scales, deadline=deadline)


def audit_partial_state(tasks, topology, macro_routes, groups, routes, present_task_ids, *, telemetry=None, weights=None, scales=None, deadline=None):
    context = _context(tasks, topology, macro_routes, groups)
    present = list(present_task_ids)
    require(present == sorted(set(present)) and all(type(t) is int for t in present), 'Partial presence must be sorted actual task identities')
    proof = _prove(tasks, topology, macro_routes, routes, present, context);ref = proof['reference']
    result = dict(passed=True, complete_solution=False, scoring_scope='partial_same_coverage_only', present_task_ids=present,
        remaining_task_ids=sorted(set(tasks) - set(present)), remaining_quota_slots=proof['slots'], final_counts=proof['final_counts'],
        actual_final_owners={str(t): proof['owners'][t] for t in present},
        score=_physical(ref['score']), event_times_min=ref['event_times_min'], delivery_times_min=ref['delivery_times_min'],
        routes_sha256=_digest(routes))
    if telemetry is not None:
        require(telemetry.get('complete', False) is False, 'A partial route set cannot claim a full solution')
        _bind(telemetry.get('routes'), routes, 'actual native partial routes')
        _bind(telemetry.get('scoring_scope'), result['scoring_scope'], 'partial comparison scope')
        for field in ('present_task_ids', 'remaining_task_ids', 'remaining_quota_slots', 'final_counts', 'actual_final_owners',
                      'score', 'event_times_min', 'delivery_times_min', 'routes_sha256'):
            _bind(telemetry.get(field), result[field], 'actual same-coverage native partial ' + field)
        _weighted(telemetry.get('key'), result['score'], weights, scales, 'same-coverage partial key only')
        if deadline is not None:
            at = telemetry.get('checked_at_perf_counter')
            require(type(at) in (int, float) and isfinite(at) and at < deadline, 'Saved partial was checked after its deadline')
    return result


def _summary(proof):
    return dict(score=proof['score'], routes_sha256=proof['routes_sha256'],
        full_replay_output_sha256=_digest(proof),
        full_clock_sha256=_digest(dict(event=proof['event_times_min'], delivery=proof['delivery_times_min'])))


def audit_metadata(tasks, topology, macro_routes, groups, metadata, *, weights, scales, deadline):
    """Audit saved complete caches, partial quotas and the strict internal best.

    Hook restoration flags are bound to the frozen implementation's telemetry;
    actual function identities are also exercised by the separate runtime tiny
    tests. Unrecorded ALNS candidates/SA transitions are not entity-replayed.
    """
    require(isinstance(metadata, dict), 'Missing multi-group native-polish metadata')
    _bind(metadata.get('method'), 'Multi-Relay-Preserving-Ordinary-DIRECT-Polish-V1', 'actual native-polish method')
    _bind(metadata.get('pin_scope'), SCOPE, 'actual native-polish scope')
    _bind(metadata.get('groups'), groups, 'actual complete macro groups')
    _bind(metadata.get('deadline_perf_counter'), deadline, 'actual native-polish absolute deadline')
    require(type(metadata.get('seed')) is int, 'Native-polish seed must be an actual integer')
    for field, expected in (('weights', weights), ('scales', scales)):
        actual = metadata.get(field)
        require(isinstance(actual, list) and len(actual) == len(expected) == 3
            and all(type(v) in (int, float) and isfinite(v) and v > 0 for v in (*actual, *expected))
            and all(a == e for a, e in zip(actual, expected)), 'Actual numeric objective differs in ' + field)
    started, runtime = metadata.get('started_at_perf_counter'), metadata.get('runtime_seconds')
    require(type(started) in (int, float) and isfinite(started) and type(runtime) in (int, float) and isfinite(runtime)
            and runtime >= 0, 'Actual native-polish wall clocks must be finite')
    for field, expected in (('patches_restored', True), ('objective_state_restored', True), ('score_comparators_restored', True),
        ('runtime_module_identity_checked', True), ('new_relay_generation', False), ('ordinary_owner_and_order_free', True),
        ('unrecorded_probes_independently_replayed', False), ('partial_scores_are_complete_bounds', False)):
        _bind(metadata.get(field), expected, 'native-polish runtime evidence ' + field)
    context = _context(tasks, topology, macro_routes, groups)
    limit = metadata.get('snapshot_limit')
    require(type(limit) is int and 0 <= limit <= 64, 'Invalid saved native-polish snapshot cap')
    require(type(metadata.get('deadline_reached')) is bool, 'Actual native-polish deadline state must be explicit')
    if metadata.get('initialization_completed') is not True:
        require(metadata.get('initialization_completed') is False and metadata['deadline_reached'] is True,
                'Unpublished native initialization requires honest deadline expiry')
        require(all(metadata.get(f) is None for f in ('input_snapshot', 'post_repair_snapshot', 'first_complete_candidate', 'best_constructed_candidate'))
                and metadata.get('full_candidate_snapshots') == [] and metadata.get('partial_state_snapshots') == []
                and metadata.get('best_checkpoint_history') == [] and metadata.get('accepted_improvements') == 0
                and metadata.get('returned_checkpoint_checked_within_deadline') is False
                and metadata.get('returned_initial_checkpoint') is False
                and metadata.get('returned_checkpoint_kind') == 'no_predeadline_complete_checkpoint',
                'Expired initialization fabricated a new checked cache or adoption')
        return dict(passed=True, initialization_completed=False, predeadline_checkpoint_verified=False,
            saved_complete_entity_references=[], saved_partial_entity_references=[],
            paid_macro_construction_provenance_verified=False, all_unrecorded_probes_entity_replayed=False)
    _bind(metadata.get('pinned_task_ids'), sorted(context['selected']), 'actual group pin union')
    _bind(metadata.get('pin_signature'), context['signature'], 'actual complete group signature')
    _bind(metadata.get('target_final_counts'), context['counts'], 'actual initial per-aircraft final quotas')
    cfg = metadata.get('configuration');require(isinstance(cfg, dict), 'Missing actual weighted ALNS configuration')
    _bind(cfg.get('seed'), metadata['seed'], 'actual weighted native-polish ALNS seed')
    _bind(cfg.get('relay_enabled'), True, 'existing relay DAG timing stays enabled')
    for field in ('enable_relay_seed', 'enable_relay_refine', 'enable_relay_budget_rebalance', 'enable_terminal_polish',
        'enable_terminal_task_exchange', 'enable_tail_exchange', 'enable_home_reassignment', 'enable_adjacent_polish',
        'enable_home_seed', 'enable_home_bias', 'enable_boundary_destroy', 'enable_multiroute_string_destroy',
        'enable_objective_rewards', 'enable_budget_cooling', 'enable_regret_portfolio'):
        _bind(cfg.get(field), False, 'disabled separate native-polish stage ' + field)
    _bind(cfg.get('relay_large_destroy_interval'), 0, 'disabled new-relay large destroy')
    _bind(cfg.get('proposal_scope'), 'finite_local_whole_DIRECT_positions_then_current_global_exact_top_k', 'finite native proposal scope')
    _bind(cfg.get('real_partial_quota_required'), True, 'actual partial quota policy')
    _bind(cfg.get('same_coverage_partial_scores_only'), True, 'partial objective comparison scope')
    allowance = cfg.get('time_limit_seconds')
    require(type(allowance) in (int, float) and isfinite(allowance) and 0 < allowance <= max(1e-12, deadline - started),
            'Actual ALNS time allowance exceeds its paid remaining wall')
    _bind(cfg.get('assigned_remaining_seconds'), allowance, 'actual assigned native ALNS remaining wall')
    refs = {};partials = []
    def prove(snapshot):
        require(isinstance(snapshot, dict), 'Missing saved complete native cache')
        digest = _digest(snapshot)
        if digest not in refs:
            p = _complete(tasks, topology, macro_routes, snapshot['routes'], context, telemetry=snapshot,
                          weights=weights, scales=scales, deadline=deadline)
            require(started <= snapshot['full_verified_at_perf_counter'] <= started + runtime,
                    'Published cache was not created within the actual helper run')
            if snapshot.get('phase') == 'input_complete_multigroup_macro':
                _bind(snapshot['routes'], macro_routes, 'actual native input macro routes')
                _bind(snapshot.get('parent_routes_sha256'), None, 'initial macro has no invented parent')
            else:
                _bind(snapshot.get('phase'), 'complete_native_repair', 'actual whole native-repair checkpoint phase')
                parent = snapshot.get('parent_routes_sha256')
                require(isinstance(parent, str) and len(parent) == 64 and all(c in '0123456789abcdef' for c in parent),
                        'A native repair checkpoint lost its pre-destroy complete-current hash')
            refs[digest] = dict(snapshot_sha256=digest, phase=snapshot['phase'], **_summary(p))
        return snapshot
    initial = prove(metadata['input_snapshot'])
    _bind(initial.get('phase'), 'input_complete_multigroup_macro', 'actual initial complete checkpoint')
    _bind(metadata.get('initial_key'), initial['key'], 'actual complete macro starting objective')
    history = metadata.get('best_checkpoint_history');candidates = metadata.get('full_candidate_snapshots')
    require(isinstance(history, list) and isinstance(candidates, list) and len(candidates) <= limit,
            'Invalid saved complete candidate/history collections')
    best = initial;last_at = started
    for snapshot in history:
        prove(snapshot)
        require(tuple(snapshot['key']) < tuple(best['key']), 'Internal best history did not strictly improve its complete macro incumbent')
        at = snapshot.get('selected_as_best_at_perf_counter')
        require(type(at) in (int, float) and last_at <= at <= started + runtime and at < deadline,
                'Internal best history has a false chronological selection clock')
        best = snapshot;last_at = at
    for snapshot in candidates: prove(snapshot)
    first = metadata.get('first_complete_candidate')
    if first is not None:
        prove(first)
        if candidates: _bind(first, candidates[0], 'actual first saved native repair')
    valid = metadata.get('complete_valid_candidates')
    require(type(valid) is int and valid >= max(len(candidates), len(history), int(first is not None)), 'Valid complete count contradicts saved cache entities')
    require((first is not None) == (valid > 0) and len(candidates) == min(valid, limit), 'Saved native candidate prefix contradicts its explicit cap')
    _bind(metadata.get('accepted_improvements'), len(history), 'actual internal strict best count')
    _bind(metadata.get('best_constructed_candidate'), best, 'actual best complete cached native result')
    _bind(metadata.get('post_repair_snapshot'), best, 'returned pre-deadline cached native checkpoint')
    _bind(metadata.get('final_key'), best['key'], 'actual returned internal guarded objective')
    unchanged = best['routes'] == initial['routes']
    _bind(metadata.get('returned_initial_checkpoint'), unchanged, 'actual input cache return flag')
    _bind(metadata.get('returned_checkpoint_kind'), 'input_macro' if unchanged else 'new_predeadline_complete_candidate', 'actual returned cache kind')
    _bind(metadata.get('returned_checkpoint_checked_within_deadline'), True, 'returned cache pre-deadline verification')
    _bind(metadata.get('best_checked_at_perf_counter'), best['full_verified_at_perf_counter'], 'actual cached best verification clock')
    for snapshot in metadata.get('partial_state_snapshots', []):
        require(len(partials) < limit, 'Saved partial evidence exceeds its declared cap')
        p = audit_partial_state(tasks, topology, macro_routes, groups, snapshot['routes'], snapshot['present_task_ids'],
            telemetry=snapshot, weights=weights, scales=scales, deadline=deadline)
        require(started <= snapshot['checked_at_perf_counter'] <= started + runtime, 'Saved partial was not checked within its helper run')
        partials.append(dict(snapshot_sha256=_digest(snapshot), **_summary(p)))
    for field in ('destroy_calls', 'selected_pins_filtered', 'destroy_topups', 'balanced_slot_replacements', 'skipped_no_balanced_destroy',
        'direct_plan_calls', 'owner_routes_proposed', 'partial_global_checks', 'rejected_partial_options', 'rejected_quota_options',
        'direct_exact_global_evaluations', 'repair_calls', 'complete_checks', 'rollback_candidates', 'rejected_acceptances', 'iterations'):
        require(type(metadata.get(field)) is int and metadata[field] >= 0, 'Invalid actual native-polish counter ' + field)
    require(metadata['complete_checks'] >= valid + 1 and metadata['repair_calls'] >= valid
            and metadata['partial_global_checks'] >= len(partials), 'Actual native/full counters omit saved physical checks')
    require(metadata['rejected_acceptances'] <= metadata['rollback_candidates'] + metadata['skipped_no_balanced_destroy'],
            'Rollback rejection count exceeds invalid/no-destroy candidates')
    return dict(passed=True, initialization_completed=True, predeadline_checkpoint_verified=True,
        saved_complete_entity_references=list(refs.values()), saved_partial_entity_references=partials,
        internal_best_checkpoints_replayed=len(history), final_reference=refs[_digest(best)],
        final_routes_sha256=best['routes_sha256'], final_score=best['score'],
        reported_hook_restoration_checked=True, runtime_hook_identity_behavior_tested_separately=True,
        paid_macro_construction_provenance_verified=False, all_unrecorded_probes_entity_replayed=False)
