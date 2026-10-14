"""Nonmonotone complete-state accumulation of disjoint split-feeder groups.

The pure-DIRECT input is an immutable adoption guard. Relay beam states may
be worse than it; only complete schedules are compared or extended. This
module supplies no DIRECT optimizer and deliberately rejects that mode.
"""
from __future__ import annotations

from math import isfinite
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import multi_relay_structure_v1 as structure
    import split_feeder_refinement_v4 as metrics
except ImportError:
    from . import multi_relay_structure_v1 as structure
    from . import split_feeder_refinement_v4 as metrics

Deadline = structure.Deadline
weighted_key = metrics.weighted_key
SCOPE = 'multi_relay_accumulation'


def _check(deadline, statistics):
    if perf_counter() >= deadline:
        statistics['deadline_reached'] = True
        raise Deadline


def _same_score(first, second):
    return (first.late_count == second.late_count
        and abs(first.total_lateness_min-second.total_lateness_min) <= 1e-8
        and abs(first.distance_km-second.distance_km) <= 1e-8)


def _family_key(groups):
    return tuple(sorted((g['i'], g['j'], g['a'], g['b'], g['c'], g['station_id']) for g in groups))


def _rank(state):
    return state['key'], state['routes']


def _strip(problem, routes, selected):
    return tuple(tuple(v for v in route if metrics._task(problem, v) not in selected) for route in routes)


def _classes(problem, original, current, selected):
    before = {t for t, clock in original.items() if clock-problem.task(t).deadline_min > 1e-9}
    after = {t for t, clock in current.items() if clock-problem.task(t).deadline_min > 1e-9}
    return {name+'_'+suffix+'_task_ids': sorted(ids & changed)
        for name, ids in (('selected', selected), ('native', set(problem.task_ids)-selected))
        for suffix, changed in (('rescued', before-after), ('newly_late', after-before))}


def _compact_catalogue(proposed, deadline, statistics):
    """Retain layouts, leg identity and route hashes, without fleet copies.

    The runtime candidate dictionaries remain intact for the actual full
    checks. An independent reader reconstructs each saved proposal from its
    complete parent and these layouts; saved states retain their full routes.
    """
    compact = {field: value for field, value in proposed.items() if field != 'complete_candidates'}
    candidates = []
    for candidate in proposed['complete_candidates']:
        _check(deadline, statistics)
        saved = {field: value for field, value in candidate.items() if field != 'routes'}
        if 'routes_sha256' not in saved:
            saved['routes_sha256'] = metrics._digest(candidate['routes'])
        candidates.append(saved)
    compact.update(complete_candidates=candidates,
        candidate_representation='parent_plus_layouts_and_routes_sha256',
        complete_candidates_contain_routes=False)
    _check(deadline, statistics)
    return compact


def _snapshot(problem, state, checked, weights, scales, *, phase, base_schedule, deadline, statistics):
    routes, schedule, groups = state['routes'], state['schedule'], state['groups']
    index = build_plan_index(problem, routes)
    handoffs, availability, shared = [], {}, {}
    for group in groups:
        _check(deadline, statistics)
        distance = metrics._shared(problem, routes, (group['i'], group['j']), index.delivery_route_by_task)
        handoff = metrics._handoff(problem, routes, schedule, group, index, distance)
        handoffs.append(dict(group_id=group['group_id'], **handoff))
        availability.update(handoff['source_availability_min'])
        shared[str(group['group_id'])] = distance
    selected = {t for group in groups for t in (group['i'], group['j'])}
    snapshot = dict(phase=phase, pin_scope=SCOPE, valid=True, complete=True,
        state_id=state['id'], parent_state_id=state.get('parent_id'),
        parent_routes_sha256=state.get('parent_routes_sha256'), group_added=state.get('group_added'),
        family_index=state.get('family_index'), candidate_index=state.get('candidate_index'),
        groups=groups, group_count=len(groups), routes=[list(route) for route in routes],
        group_family_key=[list(row) for row in _family_key(groups)],
        score=metrics._physical(checked.score), post_global_score=metrics._physical(checked.score),
        key=list(weighted_key(checked.score, weights, scales)),
        event_times_min=[[schedule.event_times_min[a, k] for k in range(len(route))]
            for a, route in enumerate(routes)],
        delivery_times_min={str(t): clock for t, clock in sorted(schedule.delivery_times_min.items())},
        actual_final_owners={str(t): owner for t, owner in sorted(index.delivery_route_by_task.items())},
        group_metrics=handoffs, current_source_drop_availability_min=availability,
        current_shared_double_loaded_km_by_group=shared,
        selected_native_classifications=_classes(problem, base_schedule.delivery_times_min,
            schedule.delivery_times_min, selected),
        relay_task_count=len(index.relay_task_ids),
        cross_uav_count=sum(index.inbound_route_by_task[t] != index.outbound_route_by_task[t]
            for t in index.relay_task_ids),
        structure_signature=structure.signature(problem, routes, groups, deadline=deadline),
        deadline_perf_counter=deadline)
    snapshot['routes_sha256'] = metrics._digest(snapshot['routes'])
    _check(deadline, statistics)
    snapshot['full_verified_at_perf_counter'] = perf_counter()
    _check(deadline, statistics)
    return snapshot


def refine(problem, routes, *, deadline, weights, scales, seed=2026101522, relay_enabled=True,
           max_groups=3, beam_width=4, max_families=16, source_option_limit=3,
           receiver_option_limit=3, position_limit=12, snapshot_limit=6):
    started = perf_counter()
    if relay_enabled is not True:
        raise ValueError('relay accumulation is R-only; use an independent full-budget DIRECT optimizer')
    bounds = ((max_groups, 3), (beam_width, 16), (max_families, 64),
        (source_option_limit, 3), (receiver_option_limit, 3), (position_limit, 256), (snapshot_limit, 64))
    if (type(deadline) is bool or not isfinite(deadline) or type(seed) is not int
        or any(type(value) is not int or not 1 <= value <= maximum for value, maximum in bounds)):
        raise ValueError('finite positive multi-group accumulation bounds required')
    weights, scales = tuple(weights), tuple(scales)
    weighted_key(Score(0, 0., 0.), weights, scales)
    base = tuple(tuple(route) for route in routes)
    statistics = dict(method='Complete-State-Multi-Relay-Accumulation-V1', pin_scope=SCOPE,
        seed=seed, started_at_perf_counter=started, deadline_perf_counter=deadline,
        deadline_reached=False, initialization_completed=False,
        input_snapshot=None, guard_archive=None, post_repair_snapshot=None,
        first_complete_candidate=None, best_constructed_candidate=None,
        full_candidate_snapshots=[], steps=[], accepted_improvements=0,
        accepted_moves=[],
        returned_input_guard=True, returned_checkpoint_checked_within_deadline=False,
        proposed_complete_candidates=0, full_validator_calls=0, global_evaluations=0,
        valid_complete_candidates=0, rejected_full_or_global=0, rejected_structure=0,
        rejected_parent_transition=0, completed_depths=0, max_group_count_generated=0,
        max_group_count_retained=0,
        configuration=dict(max_groups=max_groups, beam_width=beam_width, max_families=max_families,
            source_option_limit=source_option_limit, receiver_option_limit=receiver_option_limit,
            position_limit=position_limit, snapshot_limit=snapshot_limit,
            relay_only_module=True, allow_worse_complete_beam_states=True,
            original_DIRECT_guard_separate_archive=True, each_complete_candidate_standard_full_checked=True,
            beam_policy='canonical_group_roles_station_family_heads_then_full_key_fill',
            group_family_key_excludes_group_id_and_generation_order=True, native_repair=False,
            pure_DIRECT_suffix_prices_are_bounds=False,
            old_group_clock_reuse=False, resources_homes_weights_unchanged=True))
    try:
        _check(deadline, statistics)
        checked = evaluate_solution(problem, base, _force_full=True)
        _check(deadline, statistics)
        base_index = build_plan_index(problem, base)
        if not checked.valid or base_index.relay_task_ids:
            raise ValueError('a real complete pure-DIRECT guard is required; no historical relay seeds')
        evaluator = GlobalRelayEvaluator(problem)
        base_schedule = evaluator.evaluate(base)
        _check(deadline, statistics)
        structure.shape(problem, base, [], deadline=deadline, require_complete=True)
        guard = dict(id=0, routes=base, groups=[], schedule=base_schedule,
            key=weighted_key(checked.score, weights, scales), checked=checked)
        guard_snapshot = _snapshot(problem, guard, checked, weights, scales,
            phase='strong_DIRECT_guard', base_schedule=base_schedule, deadline=deadline, statistics=statistics)
        statistics.update(input_snapshot=guard_snapshot, guard_archive=guard_snapshot, initialization_completed=True)
    except Deadline:
        statistics.update(skipped_reason='initialization_reached_absolute_deadline',
            runtime_seconds=perf_counter()-started, best_key=None, final_routes_sha256=None)
        return dict(routes=base, evaluation=None, iterations=0, metadata=statistics)

    best, best_snapshot, best_relay = guard, guard_snapshot, None
    serial = 0

    def publish(state, step, *, force=False):
        nonlocal best, best_snapshot, best_relay
        if not force and best_relay is not None and state['key'] >= best_relay['key'] and state['key'] >= best['key']:
            return True
        try:
            snapshot = _snapshot(problem, state, state['checked'], weights, scales,
                phase='complete_new_relay_group', base_schedule=base_schedule,
                deadline=deadline, statistics=statistics)
        except (ValueError, KeyError, IndexError):
            statistics['rejected_structure'] += 1
            return False
        state['snapshot'] = snapshot
        step['complete_checkpoints'].append(snapshot)
        if statistics['first_complete_candidate'] is None:
            statistics['first_complete_candidate'] = snapshot
        if len(statistics['full_candidate_snapshots']) < snapshot_limit:
            statistics['full_candidate_snapshots'].append(snapshot)
        if best_relay is None or state['key'] < best_relay['key']:
            best_relay = state
            statistics['best_constructed_candidate'] = snapshot
        if state['key'] < best['key']:
            _check(deadline, statistics)
            snapshot['selected_as_best_at_perf_counter'] = perf_counter()
            _check(deadline, statistics)
            best, best_snapshot = state, snapshot
            statistics['accepted_improvements'] += 1
            statistics['returned_input_guard'] = False
            statistics['accepted_moves'].append(dict(state_id=state['id'],
                parent_state_id=state['parent_id'], group_added=state['group_added'],
                full_snapshot=snapshot,
                full_verified_at_perf_counter=snapshot['full_verified_at_perf_counter'],
                accepted_at_perf_counter=snapshot['selected_as_best_at_perf_counter']))
        return True

    states = [guard]
    try:
        for depth in range(1, max_groups+1):
            _check(deadline, statistics)
            step = dict(depth=depth, parent_state_ids=[state['id'] for state in states],
                parent_records=[], retained_states=[], complete_checkpoints=[], candidate_count=0, completed=False)
            statistics['steps'].append(step)
            global_heads, family_heads = {}, {}
            for parent in states:
                _check(deadline, statistics)
                signature = structure.signature(problem, parent['routes'], parent['groups'], deadline=deadline)
                proposed_statistics = {}
                record = dict(parent_state_id=parent['id'], parent_groups=parent['groups'],
                    parent_signature=signature, catalogue=None, catalogue_statistics=proposed_statistics,
                    candidate_count=0, completed=False)
                step['parent_records'].append(record)
                proposed = structure.propose_catalogue(problem, parent['routes'], parent['groups'],
                    deadline=deadline, weights=weights, scales=scales, max_families=max_families,
                    source_option_limit=source_option_limit, receiver_option_limit=receiver_option_limit,
                    position_limit=position_limit, statistics=proposed_statistics)
                record['catalogue'] = _compact_catalogue(proposed, deadline, statistics)
                _check(deadline, statistics)
                parent_hash = metrics._digest([list(route) for route in parent['routes']])
                parent_index = build_plan_index(problem, parent['routes'])
                for candidate in proposed['complete_candidates']:
                    _check(deadline, statistics)
                    record['candidate_count'] += 1
                    step['candidate_count'] += 1
                    statistics['proposed_complete_candidates'] += 1
                    proposal_routes = tuple(tuple(route) for route in candidate['routes'])
                    group = dict(candidate['new_group'])
                    groups = [*parent['groups'], group]
                    statistics['full_validator_calls'] += 1
                    full = evaluate_solution(problem, proposal_routes, _force_full=True)
                    _check(deadline, statistics)
                    schedule = evaluator.evaluate_delta(parent['routes'], parent['schedule'], proposal_routes)
                    statistics['global_evaluations'] += 1
                    _check(deadline, statistics)
                    if not full.valid or not schedule.feasible or not _same_score(full.score, schedule.score):
                        statistics['rejected_full_or_global'] += 1
                        continue
                    try:
                        i, j = group['i'], group['j']
                        if (group['group_id'] != depth or i >= j or {i, j}-set(parent_index.direct_task_ids)
                            or parent_index.delivery_route_by_task[i] != group['c']
                            or parent_index.delivery_route_by_task[j] != group['c']
                            or _strip(problem, parent['routes'], {i, j}) != _strip(problem, proposal_routes, {i, j})):
                            statistics['rejected_parent_transition'] += 1
                            continue
                        index = build_plan_index(problem, proposal_routes)
                        if index.delivery_route_by_task != base_index.delivery_route_by_task:
                            statistics['rejected_parent_transition'] += 1
                            continue
                        structure.shape(problem, proposal_routes, groups,
                            expected_signature=signature, deadline=deadline, require_complete=True)
                    except (ValueError, KeyError, IndexError, TypeError):
                        statistics['rejected_structure'] += 1
                        continue
                    _check(deadline, statistics)
                    serial += 1
                    state = dict(id=serial, parent_id=parent['id'], routes=proposal_routes, groups=groups,
                        group_added=group, parent_routes_sha256=parent_hash,
                        family_index=candidate['family_index'], candidate_index=candidate['candidate_index'],
                        schedule=schedule, checked=full, key=weighted_key(full.score, weights, scales))
                    statistics['valid_complete_candidates'] += 1
                    statistics['max_group_count_generated'] = max(statistics['max_group_count_generated'], len(groups))
                    if not publish(state, step, force=not step['complete_checkpoints']):
                        continue
                    global_heads.setdefault(proposal_routes, state)
                    if len(global_heads) > beam_width:
                        del global_heads[max(global_heads, key=lambda route: _rank(global_heads[route]))]
                    family = _family_key(groups)
                    if family not in family_heads or _rank(state) < _rank(family_heads[family]):
                        family_heads[family] = state
                    if len(family_heads) > beam_width:
                        del family_heads[max(family_heads, key=lambda key: (_rank(family_heads[key]), key))]
                record['completed'] = True
            _check(deadline, statistics)
            chosen = {state['routes']: state for state in family_heads.values()}
            for state in sorted(global_heads.values(), key=_rank):
                if len(chosen) < beam_width:
                    chosen.setdefault(state['routes'], state)
            states = sorted(chosen.values(), key=_rank)
            retained = []
            for state in states:
                if 'snapshot' not in state:
                    try:
                        state['snapshot'] = _snapshot(problem, state, state['checked'], weights, scales,
                            phase='retained_complete_multi_group_state', base_schedule=base_schedule,
                            deadline=deadline, statistics=statistics)
                    except (ValueError, KeyError, IndexError):
                        statistics['rejected_structure'] += 1
                        continue
                step['retained_states'].append(state['snapshot'])
                retained.append(state)
            states = retained
            _check(deadline, statistics)
            step['completed'] = True
            step['retained_family_count'] = len({_family_key(state['groups']) for state in states})
            statistics['completed_depths'] += 1
            statistics['max_group_count_retained'] = max(statistics['max_group_count_retained'], depth if states else 0)
            if not states:
                break
    except Deadline:
        statistics['deadline_reached'] = True
    statistics.update(post_repair_snapshot=best_snapshot, best_key=list(best['key']),
        returned_checkpoint_checked_within_deadline=True,
        final_routes_sha256=best_snapshot['routes_sha256'], runtime_seconds=perf_counter()-started)
    return dict(routes=best['routes'], evaluation=best['checked'],
        iterations=statistics['global_evaluations'], metadata=statistics)
