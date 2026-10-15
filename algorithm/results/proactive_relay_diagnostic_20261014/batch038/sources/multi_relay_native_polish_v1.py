"""Weighted ordinary-task ALNS with all existing relay groups preserved.

The five-hook mechanism follows the earlier pair/six-pin helpers.  The new
boundary is an arbitrary heterogeneous group set and real partial quota
accounting; it does not reuse the accumulation driver's native-owner guard.
Only cached, fully checked, pre-deadline complete checkpoints are returned.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from math import isclose, isfinite
from time import perf_counter
from types import ModuleType

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution

try:
    import multi_relay_structure_v1 as structure
    import split_feeder_refinement_v4 as metrics
except ImportError:
    from . import multi_relay_structure_v1 as structure
    from . import split_feeder_refinement_v4 as metrics

SCOPE = 'multi_relay_native_polish'
PATCH_NAMES = ('destroy_solution', '_all_plans_for_task', '_apply_task_exact', '_repair', '_accept_worse')
_active = False
Deadline = structure.Deadline


def _check(deadline, statistics=None):
    if perf_counter() >= deadline:
        if statistics is not None:
            statistics['deadline_reached'] = True
        raise Deadline


def _key(score, weights, scales):
    return metrics.weighted_key(score, weights, scales)


def _same_score(first, second):
    return (first.late_count == second.late_count
        and abs(first.total_lateness_min-second.total_lateness_min) <= 1e-8
        and abs(first.distance_km-second.distance_km) <= 1e-8)


def _arguments(args, kwargs, names):
    return {name: args[k] if k < len(args) else kwargs.get(name) for k, name in enumerate(names)}


def _preview(problem, routes, groups, expected, evaluator, *, deadline, statistics,
             schedule=None):
    """Validate actual whole present plans, protected groups and real q.

    A missing ordinary task is allowed here only; this is never a full score
    certificate or a lower bound.  The optional schedule is private and comes
    directly from evaluate_delta for these exact routes.
    """
    _check(deadline, statistics)
    routes = tuple(tuple(r) for r in routes)
    shape = structure.shape(problem, routes, groups, deadline=deadline, require_complete=False)
    index = build_plan_index(problem, routes)
    actual = structure._plans_signature(problem, routes, index, shape['final_counts'], groups)
    for field in ('groups', 'protected_task_ids', 'plans', 'selected_events'):
        if actual[field] != expected[field]:
            raise ValueError('protected group plans/roles/site or selected subsequences changed')
    present = set(index.task_plans)
    missing = set(problem.task_ids)-present
    if not present <= set(problem.task_ids) or missing & set(expected['protected_task_ids']):
        raise ValueError('unknown task or missing pinned plan in partial state')
    quotas = [target-current for target, current in zip(expected['final_counts'], shape['final_counts'])]
    if any(q < 0 for q in quotas) or sum(quotas) != len(missing):
        raise ValueError('partial final responsibility slots do not match whole missing tasks')
    if schedule is None:
        schedule = evaluator.evaluate(routes)
    if not schedule.feasible or set(schedule.delivery_times_min) != present:
        raise ValueError('partial current group DAG is infeasible or has wrong task coverage')
    _check(deadline, statistics)
    statistics['partial_global_checks'] += 1
    return dict(routes=routes, index=index, shape=shape, schedule=schedule,
        present_task_ids=sorted(present), remaining_task_ids=sorted(missing),
        remaining_quota_slots=quotas, final_counts=list(shape['final_counts']))


def _partial_evidence(preview, weights, scales, deadline, statistics):
    routes, scheduled = preview['routes'], preview['schedule']
    result = dict(complete=False, scoring_scope='partial_same_coverage_only',
        routes=[list(r) for r in routes], routes_sha256=metrics._digest(routes),
        present_task_ids=preview['present_task_ids'], remaining_task_ids=preview['remaining_task_ids'],
        remaining_quota_slots=preview['remaining_quota_slots'], final_counts=preview['final_counts'],
        actual_final_owners={str(t): a for t, a in sorted(preview['index'].delivery_route_by_task.items())},
        score=metrics._physical(scheduled.score), key=list(_key(scheduled.score, weights, scales)),
        event_times_min=[[scheduled.event_times_min[a, k] for k in range(len(r))] for a, r in enumerate(routes)],
        delivery_times_min={str(t): value for t, value in sorted(scheduled.delivery_times_min.items())})
    _check(deadline, statistics)
    result['checked_at_perf_counter'] = perf_counter()
    _check(deadline, statistics)
    return result


def _complete(problem, routes, groups, expected, evaluator, deadline, statistics):
    _check(deadline, statistics)
    checked = evaluate_solution(problem, routes, _force_full=True)
    statistics['complete_checks'] += 1
    _check(deadline, statistics)
    if not checked.valid:
        raise ValueError('complete validation rejected candidate')
    preview = _preview(problem, routes, groups, expected, evaluator,
        deadline=deadline, statistics=statistics)
    if preview['remaining_task_ids'] or any(preview['remaining_quota_slots']):
        raise ValueError('complete repair still has whole missing tasks or quota slots')
    if not _same_score(checked.score, preview['schedule'].score):
        raise ValueError('canonical full score differs from actual current DAG')
    return checked, preview


def _snapshot(problem, preview, checked, groups, expected, initial, initial_schedule,
              weights, scales, deadline, statistics, *, phase, parent_routes=None):
    routes, index, schedule = preview['routes'], preview['index'], preview['schedule']
    handoffs, availability, shared = [], {}, {}
    for group in groups:
        _check(deadline, statistics)
        distance = metrics._shared(problem, routes, (group['i'], group['j']), index.delivery_route_by_task)
        handoff = metrics._handoff(problem, routes, schedule, group, index, distance)
        handoffs.append(dict(group_id=group['group_id'], **handoff))
        availability.update(handoff['source_availability_min'])
        shared[str(group['group_id'])] = distance
    original_index = build_plan_index(problem, initial)
    selected = set(expected['protected_task_ids'])
    before_late = {t for t, clock in initial_schedule.delivery_times_min.items()
        if clock-problem.task(t).deadline_min > 1e-9}
    after_late = {t for t, clock in schedule.delivery_times_min.items()
        if clock-problem.task(t).deadline_min > 1e-9}
    classes = {name+'_'+suffix+'_task_ids': sorted(ids & changed)
        for name, ids in (('selected', selected), ('native', set(problem.task_ids)-selected))
        for suffix, changed in (('rescued', before_late-after_late), ('newly_late', after_late-before_late))}
    snapshot = dict(pin_scope=SCOPE, phase=phase, valid=True, complete=True,
        groups=[dict(g) for g in groups], group_count=len(groups), pinned_task_ids=sorted(selected),
        structure_signature=expected, routes=[list(r) for r in routes],
        routes_sha256=metrics._digest(routes), input_routes_sha256=metrics._digest(initial),
        parent_routes_sha256=metrics._digest(parent_routes) if parent_routes is not None else None,
        score=metrics._physical(checked.score), post_global_score=metrics._physical(checked.score),
        key=list(_key(checked.score, weights, scales)),
        actual_final_owners={str(t): a for t, a in sorted(index.delivery_route_by_task.items())},
        event_times_min=[[schedule.event_times_min[a, k] for k in range(len(r))] for a, r in enumerate(routes)],
        delivery_times_min={str(t): value for t, value in sorted(schedule.delivery_times_min.items())},
        group_metrics=handoffs, current_source_drop_availability_min=availability,
        current_shared_double_loaded_km_by_group=shared, selected_native_classifications=classes,
        changed_owner_task_ids=sorted(t for t in problem.task_ids
            if original_index.delivery_route_by_task[t] != index.delivery_route_by_task[t]),
        changed_event_sequence_aircraft=[a for a, r in enumerate(routes) if tuple(initial[a]) != r],
        relay_task_count=len(index.relay_task_ids),
        cross_uav_count=sum(index.inbound_route_by_task[t] != index.outbound_route_by_task[t]
            for t in index.relay_task_ids), final_counts=preview['final_counts'],
        deadline_perf_counter=deadline)
    _check(deadline, statistics)
    snapshot['full_verified_at_perf_counter'] = perf_counter()
    _check(deadline, statistics)
    return snapshot


@contextmanager
def _search(alns_module, problem, initial, groups, expected, initial_checked,
            initial_preview, initial_snapshot, *, deadline, weights, scales,
            statistics, snapshot_limit):
    """Only this actual worker module receives five reversible function hooks."""
    global _active
    if _active:
        raise RuntimeError('nested multi-group native polish is unsupported')
    original = {name: getattr(alns_module, name) for name in PATCH_NAMES}
    evaluator = GlobalRelayEvaluator(problem)
    pins = set(expected['protected_task_ids'])
    state = dict(before=initial, before_checked=initial_checked, rejected=False,
        checked_by_routes={initial: (initial_checked, initial_preview, initial_snapshot)},
        best_routes=initial, best_checked=initial_checked, best_snapshot=initial_snapshot,
        best_key=_key(initial_checked.score, weights, scales), partials={})

    def check(local=None):
        end = deadline if local is None else min(deadline, local)
        if perf_counter() >= end:
            statistics['deadline_reached'] = True
            raise alns_module._SearchDeadlineReached

    def preview(routes, local=None, schedule=None):
        check(local)
        rs = tuple(tuple(r) for r in routes)
        cached = state['partials'].get(rs)
        if cached is not None:
            return cached
        try:
            value = _preview(problem, rs, groups, expected, evaluator,
                deadline=deadline if local is None else min(deadline, local),
                statistics=statistics, schedule=schedule)
        except Deadline as error:
            raise alns_module._SearchDeadlineReached from error
        if len(state['partials']) >= 64:
            state['partials'].clear()
        state['partials'][rs] = value
        return value

    def save_partial(value):
        if len(statistics['partial_state_snapshots']) < snapshot_limit:
            evidence = _partial_evidence(value, weights, scales, deadline, statistics)
            if not any(s['routes_sha256'] == evidence['routes_sha256'] for s in statistics['partial_state_snapshots']):
                statistics['partial_state_snapshots'].append(evidence)

    def reject(reason):
        state['rejected'] = True
        statistics['rollback_candidates'] += 1
        reasons = statistics['rejected_reasons']
        reasons[reason] = reasons.get(reason, 0)+1
        return state['before']

    def destroy(p, local, routes, operator, count, rng, **kwargs):
        check()
        if p is not problem:
            raise ValueError('worker Problem changed')
        current = tuple(tuple(r) for r in routes)
        state['rejected'] = False
        cached = state['checked_by_routes'].get(current)
        if cached is None:
            raise ValueError('ALNS current state has no pre-deadline complete checkpoint')
        state['before'], state['before_checked'] = current, cached[0]
        statistics['destroy_calls'] += 1
        _, selected = original['destroy_solution'](p, local, current, operator, count, rng, **kwargs)
        check()
        index = build_plan_index(problem, current)
        eligible = sorted(set(index.direct_task_ids)-pins)
        target = min(max(1, count), len(eligible))
        statistics['selected_pins_filtered'] += sum(t in pins for t in selected)
        removed = list(dict.fromkeys(t for t in selected if t in eligible))[:target]
        pool = [t for t in eligible if t not in removed]
        rng.shuffle(pool)
        if len(removed) < target:
            statistics['destroy_topups'] += target-len(removed)
            removed.extend(pool[:target-len(removed)])
        owners = index.delivery_route_by_task
        if len(removed) >= 2 and len({owners[t] for t in removed}) < 2:
            other = next((t for t in pool if t not in removed and owners[t] != owners[removed[0]]), None)
            if other is not None:
                removed[-1] = other
                statistics['balanced_slot_replacements'] += 1
        if len(removed) < 2 or len({owners[t] for t in removed}) < 2:
            statistics['skipped_no_balanced_destroy'] += 1
            state['rejected'] = True
            return current, ()
        partial = alns_module._remove_tasks(current, index, removed)
        value = preview(partial)
        if set(value['remaining_task_ids']) != set(removed):
            raise ValueError('destroy did not remove exactly its whole ordinary tasks')
        save_partial(value)
        check()
        return partial, tuple(removed)

    def plans(*args, **kwargs):
        values = _arguments(args, kwargs, ('problem', 'evaluator', 'routes', 'task_id'))
        p, local, routes, task = (values[k] for k in ('problem', 'evaluator', 'routes', 'task_id'))
        end = kwargs.get('deadline')
        check(end)
        if p is not problem or task in pins:
            raise ValueError('only this Problem and ordinary missing tasks may be repaired')
        current = preview(routes, end)
        if task not in current['remaining_task_ids']:
            raise ValueError('repair task is not a whole missing ordinary plan')
        statistics['direct_plan_calls'] += 1
        options = []
        for owner, slots in enumerate(current['remaining_quota_slots']):
            if not slots:
                continue
            check(end)
            local_options = alns_module.route_insertion_options(p, local, routes[owner], owner, task,
                candidate_limit=kwargs.get('candidate_limit'), option_count=kwargs['option_count'])
            check(end)
            statistics['owner_routes_proposed'] += 1
            for option in local_options:
                options.append(alns_module.TaskInsertionOption(task_id=task, mode='DIRECT', relay_id=None,
                    local_estimated_delta=option.delta, direct_route_index=owner, direct_route=option.route,
                    direct_pickup_position=option.pickup_position, direct_delivery_position=option.delivery_position,
                    end_time_min=option.end_time_min))
        options.sort(key=lambda option: (_key(option.local_estimated_delta, weights, scales),
            option.direct_route_index, option.direct_pickup_position, option.direct_delivery_position, option.direct_route))
        check(end)
        return tuple(options[:kwargs['option_count']])

    def exact(p, local, global_evaluator, routes, options, *, relay_cfg, deadline=None, direct_exact_top_k=2):
        check(deadline)
        if p is not problem:
            raise ValueError('exact repair Problem changed')
        current = preview(routes, deadline)
        best, best_score, seen = None, None, 0
        for option in options:
            if option.mode != 'DIRECT' or option.task_id in pins:
                raise ValueError('only ordinary whole DIRECT exact choices are allowed')
            if seen >= direct_exact_top_k:
                break
            owner = option.direct_route_index
            if not current['remaining_quota_slots'][owner] or option.task_id not in current['remaining_task_ids']:
                statistics['rejected_quota_options'] += 1
                continue
            seen += 1
            candidate = list(routes)
            candidate[owner] = option.direct_route
            candidate = tuple(tuple(r) for r in candidate)
            proposed = global_evaluator.evaluate_delta(routes, current['schedule'], candidate)
            statistics['direct_exact_global_evaluations'] += 1
            relay_cfg.global_evaluation_count += 1
            if getattr(relay_cfg, 'counters', None) is not None:
                relay_cfg.counters.repair_exact_evaluation_count += 1
            check(deadline)
            try:
                value = preview(candidate, deadline, proposed)
                if set(value['present_task_ids']) != set(current['present_task_ids']) | {option.task_id}:
                    raise ValueError('exact option changed wrong task coverage')
                if value['index'].delivery_route_by_task[option.task_id] != owner:
                    raise ValueError('exact option has wrong whole-task owner')
            except (ValueError, KeyError, IndexError):
                statistics['rejected_partial_options'] += 1
                continue
            if best_score is None or _key(proposed.score, weights, scales) < _key(best_score, weights, scales):
                best, best_score = candidate, proposed.score
        if best is not None:
            save_partial(preview(best, deadline))
        return best, best_score, 'DIRECT' if best is not None else None

    def repair(*args, **kwargs):
        values = _arguments(args, kwargs, ('problem', 'evaluator', 'partial_routes', 'removed_task_ids'))
        end = kwargs.get('deadline', args[7] if len(args) > 7 else None)
        check(end)
        if values['problem'] is not problem:
            raise ValueError('repair Problem changed')
        removed = tuple(values['removed_task_ids'])
        statistics['repair_calls'] += 1
        if state['rejected'] or not removed:
            state['rejected'] = True
            return state['before']
        try:
            partial = preview(values['partial_routes'], end)
            if set(removed) & pins or set(partial['remaining_task_ids']) != set(removed):
                return reject('destroy_coverage_or_pins')
            candidate = original['_repair'](*args, **kwargs)
            check(end)
            candidate = tuple(tuple(r) for r in candidate)
            checked, value = _complete(problem, candidate, groups, expected, evaluator, deadline, statistics)
            snapshot = _snapshot(problem, value, checked, groups, expected, initial,
                initial_preview['schedule'], weights, scales, deadline, statistics,
                phase='complete_native_repair', parent_routes=state['before'])
        except Deadline as error:
            raise alns_module._SearchDeadlineReached from error
        except alns_module._SearchDeadlineReached:
            raise
        except (ValueError, KeyError, IndexError, RuntimeError):
            check(end)
            return reject('complete_or_group_or_quota')
        check(end)
        statistics['complete_valid_candidates'] += 1
        state['checked_by_routes'][candidate] = (checked, value, snapshot)
        if len(state['checked_by_routes']) > 64:
            state['checked_by_routes'] = {candidate: (checked, value, snapshot),
                state['before']: state['checked_by_routes'][state['before']],
                state['best_routes']: state['checked_by_routes'][state['best_routes']]}
        if statistics['first_complete_candidate'] is None:
            statistics['first_complete_candidate'] = snapshot
        if len(statistics['full_candidate_snapshots']) < snapshot_limit:
            statistics['full_candidate_snapshots'].append(snapshot)
        key = _key(checked.score, weights, scales)
        if key < state['best_key']:
            check(end)
            snapshot['selected_as_best_at_perf_counter'] = perf_counter()
            check(end)
            state.update(best_routes=candidate, best_checked=checked, best_snapshot=snapshot, best_key=key)
            statistics['accepted_improvements'] += 1
            statistics['best_constructed_candidate'] = snapshot
            statistics['best_checkpoint_history'].append(snapshot)
        return candidate

    def accept(*args, **kwargs):
        if state['rejected']:
            state['rejected'] = False
            statistics['rejected_acceptances'] += 1
            return False
        if perf_counter() >= deadline:
            return False
        return original['_accept_worse'](*args, **kwargs)

    replacements = dict(destroy_solution=destroy, _all_plans_for_task=plans,
        _apply_task_exact=exact, _repair=repair, _accept_worse=accept)
    _active = True
    statistics['patches_restored'] = False
    try:
        for name, function in replacements.items():
            setattr(alns_module, name, function)
        yield state
    finally:
        for name, function in original.items():
            setattr(alns_module, name, function)
        statistics['patches_restored'] = all(getattr(alns_module, name) is function for name, function in original.items())
        state['rejected'] = False
        _active = False


def polish_groups(problem, routes, groups, *, deadline, weights, scales, seed,
                  alns_module, config=None, snapshot_limit=6):
    """Preserve every declared real group; repair only ordinary whole tasks.

    There is no proposal margin or external DIRECT adoption gate here.  The
    returned best is no worse than this complete input macro.  The future
    caller must separately guard its paid original strong DIRECT incumbent.
    """
    started = perf_counter()
    if _active:
        raise RuntimeError('nested multi-group native polish is unsupported')
    weights, scales = tuple(weights), tuple(scales)
    if (type(deadline) is bool or not isfinite(deadline) or type(seed) is not int
        or type(snapshot_limit) is not int or not 0 <= snapshot_limit <= 64
        or len(weights) != 3 or len(scales) != 3
        or any(type(v) is bool or not isfinite(v) or v <= 0 for v in (*weights, *scales))
        or not isclose(sum(weights), 1.)):
        raise ValueError('finite normalized objective, absolute deadline and bounded snapshot limit required')
    if (not isinstance(alns_module, ModuleType) or not hasattr(alns_module, '_weighted_adapter')
        or any(getattr(alns_module, name).__globals__ is not alns_module.__dict__ for name in ('solve_alns', '_repair'))):
        raise ValueError('supply the actual frozen weighted ALNS module with solve/repair globals in that same module')
    if config is not None and not isinstance(config, alns_module.ALNSConfig):
        raise ValueError('configuration must belong to the actual supplied ALNS module')
    adapter = alns_module._weighted_adapter
    coefficients = tuple(w/s for w, s in zip(weights, scales))
    if adapter.active and tuple(adapter.coefficients) != coefficients:
        raise ValueError('active weighted objective differs from requested coefficients')
    initial = tuple(tuple(r) for r in routes)
    groups = [dict(g) for g in groups]
    if not groups:
        raise ValueError('at least one complete real relay group is required')
    stats = dict(method='Multi-Relay-Preserving-Ordinary-DIRECT-Polish-V1', pin_scope=SCOPE,
        seed=seed, weights=list(weights), scales=list(scales), groups=groups,
        started_at_perf_counter=started, deadline_perf_counter=deadline, deadline_reached=False,
        initialization_completed=False, input_snapshot=None, post_repair_snapshot=None,
        first_complete_candidate=None, best_constructed_candidate=None, full_candidate_snapshots=[],
        partial_state_snapshots=[], best_checkpoint_history=[], snapshot_limit=snapshot_limit, iterations=0,
        destroy_calls=0, selected_pins_filtered=0, destroy_topups=0, balanced_slot_replacements=0,
        skipped_no_balanced_destroy=0, direct_plan_calls=0, owner_routes_proposed=0,
        partial_global_checks=0, rejected_partial_options=0, rejected_quota_options=0,
        direct_exact_global_evaluations=0, repair_calls=0, complete_checks=0,
        complete_valid_candidates=0, rollback_candidates=0, rejected_acceptances=0,
        rejected_reasons={}, accepted_improvements=0, patches_restored=True,
        objective_state_restored=True, score_comparators_restored=True,
        runtime_module_identity_checked=True, returned_checkpoint_checked_within_deadline=False,
        unrecorded_probes_independently_replayed=False, new_relay_generation=False,
        ordinary_owner_and_order_free=True, partial_scores_are_complete_bounds=False)
    try:
        _check(deadline, stats)
        expected = structure.signature(problem, initial, groups, deadline=deadline)
        _check(deadline, stats)
        evaluator = GlobalRelayEvaluator(problem)
        checked, preview = _complete(problem, initial, groups, expected, evaluator, deadline, stats)
        snapshot = _snapshot(problem, preview, checked, groups, expected, initial, preview['schedule'],
            weights, scales, deadline, stats, phase='input_complete_multigroup_macro')
        _check(deadline, stats)
    except Deadline:
        stats.update(skipped_reason='initialization_reached_absolute_deadline',
            runtime_seconds=perf_counter()-started, returned_initial_checkpoint=False,
            returned_checkpoint_kind='no_predeadline_complete_checkpoint')
        return dict(routes=initial, evaluation=None, iterations=0, metadata=stats)
    stats.update(initialization_completed=True, input_snapshot=snapshot, best_constructed_candidate=snapshot,
        initial_key=list(_key(checked.score, weights, scales)), pinned_task_ids=expected['protected_task_ids'],
        pin_signature=expected, target_final_counts=expected['final_counts'])
    cfg = config if config is not None else alns_module.ALNSConfig()
    remaining = max(1e-12, deadline-perf_counter())
    cfg = replace(cfg, seed=seed, time_limit_seconds=remaining, relay_enabled=True,
        enable_relay_seed=False, enable_relay_refine=False, enable_relay_budget_rebalance=False,
        enable_terminal_polish=False, enable_terminal_task_exchange=False, enable_tail_exchange=False,
        enable_home_reassignment=False, enable_adjacent_polish=False, enable_home_seed=False,
        enable_home_bias=False, enable_boundary_destroy=False, enable_multiroute_string_destroy=False,
        enable_objective_rewards=False, enable_budget_cooling=False, enable_regret_portfolio=False,
        relay_large_destroy_interval=0)
    stats['configuration'] = asdict(cfg)
    stats['configuration'].update(assigned_remaining_seconds=remaining,
        proposal_scope='finite_local_whole_DIRECT_positions_then_current_global_exact_top_k',
        real_partial_quota_required=True, same_coverage_partial_scores_only=True)
    before_active = adapter.active
    before_coefficients = None if adapter.coefficients is None else tuple(adapter.coefficients)
    comparators = {name: getattr(alns_module.Score, name) for name in ('__lt__', '__le__', '__gt__', '__ge__')}
    objective = nullcontext() if adapter.active else adapter.objective(alns_module.Score, scales, weights)
    best, best_checked, best_snapshot, iterations = initial, checked, snapshot, 0
    try:
        with objective:
            with _search(alns_module, problem, initial, groups, expected, checked, preview, snapshot,
                deadline=deadline, weights=weights, scales=scales, statistics=stats,
                snapshot_limit=snapshot_limit) as state:
                try:
                    _check(deadline, stats)
                    outcome = alns_module.solve_alns(problem, config=cfg, initial_routes=initial)
                    iterations = outcome.iterations
                    summary = getattr(outcome, 'metadata', {})
                    stats['alns_summary'] = dict(iterations=iterations,
                        accepted_solutions=summary.get('accepted_solutions'),
                        operator_statistics={name: dict(value) for name, value in summary.get('operator_statistics', {}).items()})
                except (Deadline, alns_module._SearchDeadlineReached):
                    stats['skipped_reason'] = 'search_reached_absolute_deadline'
                    stats['deadline_reached'] = True
                best, best_checked, best_snapshot = state['best_routes'], state['best_checked'], state['best_snapshot']
    finally:
        after_coefficients = None if adapter.coefficients is None else tuple(adapter.coefficients)
        stats['objective_state_restored'] = adapter.active == before_active and after_coefficients == before_coefficients
        stats['score_comparators_restored'] = all(getattr(alns_module.Score, name) is function for name, function in comparators.items())
    # No physics, snapshot construction, or fresh checkpoint is performed here.
    stats.update(post_repair_snapshot=best_snapshot, final_key=list(_key(best_checked.score, weights, scales)),
        iterations=iterations, returned_initial_checkpoint=best == initial,
        returned_checkpoint_kind='input_macro' if best == initial else 'new_predeadline_complete_candidate',
        returned_checkpoint_checked_within_deadline=best_snapshot['full_verified_at_perf_counter'] < deadline,
        best_checked_at_perf_counter=best_snapshot['full_verified_at_perf_counter'],
        deadline_reached=stats['deadline_reached'] or perf_counter() >= deadline,
        runtime_seconds=perf_counter()-started)
    return dict(routes=best, evaluation=best_checked, iterations=iterations, metadata=stats)
