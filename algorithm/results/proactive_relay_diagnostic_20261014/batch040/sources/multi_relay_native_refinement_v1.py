"""Paid multi-group construction followed by group-preserving native ALNS.

The original pure-DIRECT plan is a separate strict adoption guard.  Raw
complete group states are paid for again in this call; no historical relay
seed or pure-DIRECT suffix price is used to certify a polished state.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter

from uav_dispatch import alns as default_alns
from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution

try:
    import multi_relay_accumulation_v1 as raw_method
    import multi_relay_native_polish_v1 as polish
    import multi_relay_structure_v1 as structure
except ImportError:
    from . import multi_relay_accumulation_v1 as raw_method
    from . import multi_relay_native_polish_v1 as polish
    from . import multi_relay_structure_v1 as structure

Deadline = structure.Deadline
SCOPE = 'paid_multi_relay_native_refinement'
RESERVE_SECONDS = .15


def _check(deadline, stats):
    if perf_counter() >= deadline:
        stats['deadline_reached'] = True
        raise Deadline


def _routes(snapshot):
    return tuple(tuple(r) for r in snapshot['routes'])


def _seeds(raw_metadata, *, max_groups, raw_deadline, weights, scales, deadline, statistics):
    """Select only actually saved complete raw entities, including open layers.

    One minimum full(key,routes) per actual group count, route deduplicated.
    The saved path proves provenance; an unfinished depth is not required to
    have published a retained beam if it already has a checked checkpoint.
    """
    pool = {}
    def add(snapshot, path):
        _check(deadline, statistics)
        if snapshot is None: return
        if not isinstance(snapshot, dict): raise ValueError('raw saved checkpoint must be a dictionary')
        groups = snapshot.get('groups')
        if isinstance(groups, list) and not groups: return
        if (not isinstance(groups, list) or not 1 <= len(groups) <= max_groups
            or snapshot.get('group_count') != len(groups)
            or snapshot.get('valid') is not True or snapshot.get('complete') is not True):
            raise ValueError('raw seed must be a saved complete actual-group checkpoint')
        verified = snapshot.get('full_verified_at_perf_counter')
        if (type(verified) not in (int, float) or not isfinite(verified) or verified >= raw_deadline
            or snapshot.get('deadline_perf_counter') != raw_deadline):
            raise ValueError('raw seed has no matching pre-raw-deadline complete certificate')
        routes = _routes(snapshot)
        if snapshot.get('routes_sha256') != raw_method.metrics._digest(routes):
            raise ValueError('raw complete checkpoint routes do not match its physical hash')
        score = snapshot['score']
        key = raw_method.weighted_key(Score(score['late_count'], score['total_lateness_min'], score['distance_km']), weights, scales)
        if tuple(snapshot.get('key', ())) != tuple(key):
            raise ValueError('raw complete checkpoint full weighted key differs')
        if routes not in pool:
            pool[routes] = dict(raw_snapshot=snapshot, source_path=path,
                raw_snapshot_sha256=raw_method.metrics._digest(snapshot),
                routes_sha256=snapshot['routes_sha256'], group_count=len(groups), groups=groups)
    for name in ('first_complete_candidate', 'best_constructed_candidate', 'post_repair_snapshot'):
        add(raw_metadata.get(name), [name])
    for k, snapshot in enumerate(raw_metadata.get('full_candidate_snapshots', [])):
        add(snapshot, ['full_candidate_snapshots', k])
    for number, step in enumerate(raw_metadata.get('steps', [])):
        for name in ('complete_checkpoints', 'retained_states'):
            for k, snapshot in enumerate(step.get(name, [])):
                add(snapshot, ['steps', number, name, k])
    selected = []
    for count in range(1, max_groups + 1):
        _check(deadline, statistics)
        choices = [(routes, record) for routes, record in pool.items() if record['group_count'] == count]
        if choices:
            _, record = min(choices, key=lambda item: (tuple(item[1]['raw_snapshot']['key']), item[0]))
            selected.append(dict(channel='minimum_J_saved_complete_groups_' + str(count), **record))
    statistics['saved_raw_seed_pool_size'] = len(pool)
    _check(deadline, statistics)
    return selected


def refine(problem, routes, *, deadline, weights, scales, seed, relay_enabled=True,
           raw_fraction=.4, max_groups=3, beam_width=4, max_families=16,
           source_option_limit=3, receiver_option_limit=3, position_limit=12,
           snapshot_limit=6, polish_max_iterations=1_000_000,
           polish_initial_temperature=.0025, polish_minimum_temperature=.00001,
           polish_cooling_rate=.97, alns_module=None):
    started = perf_counter();weights, scales = tuple(weights), tuple(scales)
    if (relay_enabled is not True or type(seed) is not int or type(deadline) is bool or not isfinite(deadline)
        or type(raw_fraction) is bool or not isfinite(raw_fraction) or not 0 < raw_fraction < 1):
        raise ValueError('R-only, finite absolute deadline, real seed and paid raw fraction required')
    limits = ((max_groups, 3), (beam_width, 16), (max_families, 64), (source_option_limit, 3),
        (receiver_option_limit, 3), (position_limit, 256), (snapshot_limit, 64))
    if (any(type(n) is not int or not 1 <= n <= limit for n, limit in limits)
        or max(source_option_limit, receiver_option_limit) > position_limit
        or type(polish_max_iterations) is not int or polish_max_iterations < 1
        or any(type(n) is bool or not isfinite(n) or n <= 0 for n in
            (polish_initial_temperature, polish_minimum_temperature, polish_cooling_rate))
        or polish_minimum_temperature > polish_initial_temperature or polish_cooling_rate > 1):
        raise ValueError('finite positive construction and actual ALNS profile required')
    raw_method.weighted_key(Score(0, 0., 0.), weights, scales)
    module = default_alns if alns_module is None else alns_module
    base = tuple(tuple(r) for r in routes)
    raw_parameters = dict(max_groups=max_groups, beam_width=beam_width, max_families=max_families,
        source_option_limit=source_option_limit, receiver_option_limit=receiver_option_limit,
        position_limit=position_limit, snapshot_limit=snapshot_limit)
    profile = dict(max_iterations=polish_max_iterations, initial_temperature=polish_initial_temperature,
        minimum_temperature=polish_minimum_temperature, cooling_rate=polish_cooling_rate)
    stats = dict(method='Paid-Multi-Relay-Construction-And-Native-Polish-V1', pin_scope=SCOPE, seed=seed,
        relay_enabled=True, started_at_perf_counter=started, deadline_perf_counter=deadline,
        deadline_reached=False, initialization_completed=False, input_snapshot=None, initial_key=None,
        raw_deadline_perf_counter=None, raw_stage_returned_at_perf_counter=None, raw_metadata=None,
        raw_output_routes=None, seed_records=[], seed_selection_completed=False,
        polish_records=[], saved_raw_seed_pool_size=0,
        full_candidate_snapshots=[], accepted_moves=[], accepted_improvements=0,
        final_snapshot=None, final_key=None, returned_input_guard=True, complete_checks=0, partial_global_checks=0,
        configuration=dict(raw_fraction=raw_fraction, raw_method='multi_relay_accumulation_v1', raw_parameters=raw_parameters,
            polish_alns_profile=profile, wrapper_verification_reserve_seconds=RESERVE_SECONDS,
            seed_policy='minimum_J_saved_complete_each_actual_group_count_then_route_deduplicate',
            maximum_seed_channels=max_groups, allow_complete_checkpoint_from_unfinished_raw_depth=True,
            no_free_historical_relay_seed=True, raw_native_owners_and_order_fixed=True,
            polished_native_owner_and_order_free=True, polished_selected_plans_and_subsequences_fixed=True,
            helper_return_requires_cached_predeadline_complete_checkpoint=True,
            final_guard='original_pure_DIRECT_complete_input_strict_weighted', strongest_fresh_end_to_end_comparison=False))
    best, best_checked, best_key, final = base, None, None, None
    iterations = 0
    evaluator = GlobalRelayEvaluator(problem)
    try:
        _check(deadline, stats)
        checked = evaluate_solution(problem, base, _force_full=True);_check(deadline, stats)
        base_index = build_plan_index(problem, base)
        if not checked.valid or base_index.relay_task_ids:
            raise ValueError('genuine complete pure-DIRECT strong guard required')
        base_schedule = evaluator.evaluate(base);_check(deadline, stats)
        if not base_schedule.feasible or not raw_method._same_score(checked.score, base_schedule.score):
            raise ValueError('original full score differs from actual guard schedule')
        target_counts = structure.shape(problem, base, [], deadline=deadline)['final_counts']
        guard_state = dict(id=0, routes=base, groups=[], schedule=base_schedule)
        guard = raw_method._snapshot(problem, guard_state, checked, weights, scales,
            phase='original_strong_DIRECT_guard', base_schedule=base_schedule, deadline=deadline, statistics=stats)
        _check(deadline, stats)
        best_checked, best_key, final = checked, raw_method.weighted_key(checked.score, weights, scales), guard
        stats.update(initialization_completed=True, input_snapshot=guard, initial_key=list(best_key))
    except Deadline:
        stats.update(final_snapshot=None, final_key=None, runtime_seconds=perf_counter()-started,
            returned_checkpoint_checked_within_deadline=False)
        return dict(routes=base, evaluation=None, iterations=0, metadata=stats)

    def consider(candidate, record, phase):
        nonlocal best, best_checked, best_key, final
        _check(deadline, stats)
        current = tuple(tuple(r) for r in candidate);macro = _routes(record['raw_snapshot']);groups = record['groups']
        expected = structure.signature(problem, macro, groups, deadline=deadline)
        selected = set(expected['protected_task_ids'])
        macro_index = build_plan_index(problem, macro)
        if (macro_index.delivery_route_by_task != base_index.delivery_route_by_task
            or raw_method._strip(problem, macro, selected) != raw_method._strip(problem, base, selected)):
            raise ValueError('raw macro source must preserve the paid construction owner/native-sequence contract')
        full = evaluate_solution(problem, current, _force_full=True);stats['complete_checks'] += 1
        _check(deadline, stats)
        current_schedule = evaluator.evaluate(current);_check(deadline, stats)
        if not full.valid or not current_schedule.feasible or not raw_method._same_score(full.score, current_schedule.score):
            raise ValueError('outer current complete schedule/full validation differs')
        shape = structure.shape(problem, current, groups, expected_signature=expected, deadline=deadline)
        if shape['final_counts'] != target_counts:
            raise ValueError('polish changed original final responsibility counts')
        index = build_plan_index(problem, current)
        # Classifications for native polish deliberately use this actual raw
        # macro, while the outer guard has a separate original comparison.
        macro_schedule = evaluator.evaluate(macro);_check(deadline, stats)
        if not macro_schedule.feasible:
            raise ValueError('raw macro source has no feasible current physical schedule')
        preview = dict(routes=current, index=index, schedule=current_schedule, shape=shape, final_counts=shape['final_counts'])
        snapshot = polish._snapshot(problem, preview, full, groups, expected, macro, macro_schedule,
            weights, scales, deadline, stats, phase=phase)
        snapshot['full_verified_at_perf_counter'] = perf_counter()
        _check(deadline, stats)
        stats['full_candidate_snapshots'].append(snapshot)
        candidate_key = raw_method.weighted_key(full.score, weights, scales)
        if candidate_key < best_key:
            accepted = perf_counter();_check(deadline, stats)
            best, best_checked, best_key, final = current, full, candidate_key, snapshot
            stats['accepted_moves'].append(dict(source=phase, seed_routes_sha256=record['routes_sha256'],
                full_snapshot=snapshot, full_verified_at_perf_counter=snapshot['full_verified_at_perf_counter'],
                accepted_at_perf_counter=accepted))
            stats['accepted_improvements'] += 1;stats['returned_input_guard'] = False

    try:
        _check(deadline, stats)
        raw_deadline = started + (deadline-started)*raw_fraction
        stats['raw_deadline_perf_counter'] = raw_deadline
        raw = raw_method.refine(problem, base, deadline=raw_deadline, weights=weights, scales=scales,
            seed=seed, relay_enabled=True, **raw_parameters)
        iterations += raw['iterations'];stats.update(raw_metadata=raw['metadata'],
            raw_output_routes=[list(r) for r in raw['routes']], raw_stage_returned_at_perf_counter=perf_counter())
        _check(deadline, stats)
        selected = _seeds(raw['metadata'], max_groups=max_groups, raw_deadline=raw_deadline,
            weights=weights, scales=scales, deadline=deadline, statistics=stats)
        stats['seed_records'] = [{k: v for k, v in record.items() if k != 'raw_snapshot'} for record in selected]
        stats['seed_selection_completed'] = True
        for number, record in enumerate(selected):
            _check(deadline, stats)
            consider(_routes(record['raw_snapshot']), record, 'raw_seed_' + str(number))
            allocation_started = perf_counter()
            _check(deadline, stats)
            remaining_channels = len(selected)-number
            leaf_deadline = allocation_started + max(0., deadline-RESERVE_SECONDS-allocation_started)/remaining_channels
            entry = dict(seed_index=number, channel=record['channel'], group_count=record['group_count'],
                groups=record['groups'], source_seed_routes_sha256=record['routes_sha256'],
                allocation_started_at_perf_counter=allocation_started, remaining_channels=remaining_channels,
                deadline_perf_counter=leaf_deadline, returned=False, metadata=None, returned_routes=None)
            stats['polish_records'].append(entry)
            cfg = module.ALNSConfig(**profile)
            outcome = polish.polish_groups(problem, _routes(record['raw_snapshot']), record['groups'],
                deadline=leaf_deadline, weights=weights, scales=scales, seed=seed+number,
                alns_module=module, config=cfg, snapshot_limit=snapshot_limit)
            iterations += outcome['iterations'];meta = outcome['metadata']
            entry.update(returned=True, metadata=meta, returned_routes=[list(r) for r in outcome['routes']],
                returned_at_perf_counter=perf_counter())
            _check(deadline, stats)
            checkpoint = meta.get('post_repair_snapshot')
            if meta.get('returned_checkpoint_checked_within_deadline') is not True:
                if outcome.get('evaluation') is not None or checkpoint is not None:
                    raise ValueError('unchecked helper return fabricated a complete checkpoint/evaluation')
                entry['post_consideration_skipped'] = 'no_helper_predeadline_complete_checkpoint'
                continue
            history = [meta.get('input_snapshot'), *meta.get('best_checkpoint_history', [])]
            if (outcome.get('evaluation') is None or not isinstance(checkpoint, dict)
                or not any(checkpoint is saved for saved in history if saved is not None)
                or checkpoint.get('valid') is not True or checkpoint.get('complete') is not True
                or checkpoint.get('routes') != entry['returned_routes']
                or checkpoint.get('routes_sha256') != raw_method.metrics._digest(outcome['routes'])
                or checkpoint.get('groups') != record['groups']
                or checkpoint.get('deadline_perf_counter') != leaf_deadline
                or type(checkpoint.get('full_verified_at_perf_counter')) not in (int, float)
                or checkpoint['full_verified_at_perf_counter'] >= leaf_deadline):
                raise ValueError('helper return is not its cached matching pre-leaf-deadline complete checkpoint')
            consider(outcome['routes'], record, 'polished_seed_' + str(number))
    except Deadline:
        stats['deadline_reached'] = True
    stats.update(final_snapshot=final, final_key=list(best_key), runtime_seconds=perf_counter()-started,
        returned_checkpoint_checked_within_deadline=final['full_verified_at_perf_counter'] < deadline)
    return dict(routes=best, evaluation=best_checked, iterations=iterations, metadata=stats)
