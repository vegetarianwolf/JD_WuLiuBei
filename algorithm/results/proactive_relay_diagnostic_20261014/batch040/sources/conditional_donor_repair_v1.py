"""Target-conditioned donor exchanges, compared only after both jobs return.

The donor catalogue and position frontiers are finite proposals, not bounds.
Every search state is complete; the first inserted job has no objective gate.
"""
from __future__ import annotations

from itertools import permutations
from math import isfinite
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import critical_prefix_joint_repair_v1 as previous
    import conditional_donor_catalogue_v1 as catalogue
except ImportError:
    from . import critical_prefix_joint_repair_v1 as previous
    from . import conditional_donor_catalogue_v1 as catalogue

closure = previous.closure
raw_tools = previous.raw_tools
Deadline = closure.Deadline
weighted_key = previous.weighted_key
_frontier = previous._frontier
SCOPE = 'conditional_donor_completion_repair'


def _check(deadline, statistics):
    if perf_counter() >= deadline:
        statistics['deadline_reached'] = True
        raise Deadline


def _snapshot(p, state, checked, weights, scales, *, phase, base_schedule,
              initial_routes, seed_move, pins, focus, deadline, statistics):
    snapshot = previous._snapshot(p, state, checked, weights, scales, phase=phase,
        base_schedule=base_schedule, initial_routes=initial_routes,
        seed_move=seed_move, pins=pins, focus=focus, deadline=deadline, stats=statistics)
    snapshot.update(pin_scope=SCOPE, exchange=state.get('exchange'),
        parent_routes_sha256=state.get('parent_routes_sha256'),
        partial_quota_slots=state.get('partial_quota_slots'),
        remaining_quota_slots=list(state['q']), present_task_ids=sorted(p.task_ids))
    _check(deadline, statistics)
    snapshot['full_verified_at_perf_counter'] = perf_counter()
    _check(deadline, statistics)
    return snapshot


def _same_score(first, second):
    return (first.late_count == second.late_count
        and abs(first.total_lateness_min-second.total_lateness_min) <= 1e-8
        and abs(first.distance_km-second.distance_km) <= 1e-8)


def refine(problem, base_routes, raw_routes, *, seed_move=None, focus_task_ids=(),
           deadline, weights, scales, seed=2026101521, relay_enabled=True,
           critical_limit=6, donor_limit=3, position_limit=12, option_count=3,
           beam_width=4, max_steps=2, snapshot_limit=6):
    started = perf_counter()
    weights, scales = tuple(weights), tuple(scales)
    limits = ((critical_limit, 6), (donor_limit, 3), (position_limit, 256),
        (option_count, 3), (beam_width, 16), (max_steps, 8), (snapshot_limit, 64))
    if (type(deadline) is bool or not isfinite(deadline) or type(seed) is not int
        or type(relay_enabled) is not bool
        or any(type(value) is not int or not 1 <= value <= maximum for value, maximum in limits)
        or option_count > position_limit):
        raise ValueError('finite positive conditional-donor search bounds required')
    weighted_key(Score(0, 0., 0.), weights, scales)
    base = tuple(tuple(route) for route in base_routes)
    initial = tuple(tuple(route) for route in raw_routes)
    pins = frozenset((seed_move['family']['i'], seed_move['family']['j'])) if relay_enabled and seed_move else frozenset()
    if relay_enabled and len(pins) != 2:
        raise ValueError('two genuine split-relay pins required')
    focus = tuple(sorted(set(focus_task_ids or pins)))
    if any(type(t) is not int or t not in problem._task_by_id for t in focus):
        raise ValueError('known focus task IDs required')
    statistics = dict(method='Conditional-Donor-Complete-Two-Task-Repair-V1', seed=seed,
        relay_enabled=relay_enabled, pin_scope=SCOPE, pinned_task_ids=sorted(pins),
        focus_task_ids=list(focus), original_seed_move=seed_move,
        deadline_perf_counter=deadline, deadline_reached=False, initialization_completed=False,
        input_snapshot=None, post_repair_snapshot=None, first_complete_candidate=None,
        best_constructed_candidate=None, full_candidate_snapshots=[], steps=[],
        returned_input_guard=True, returned_checkpoint_checked_within_deadline=False,
        global_partial_evaluations=0, global_delta_evaluations=0,
        complete_proposals_generated=0, complete_full_validator_calls=0, complete_leafs_full_verified=0,
        full_validator_rejections=0, invalid_candidates=0, accepted_improvements=0,
        completed_steps=0, legal_PD_positions=0, position_shortlist_retained=0,
        position_frontier_retained=0, PD_positions_omitted=0,
        configuration=dict(critical_limit=critical_limit, donor_limit=donor_limit,
            position_limit=position_limit, option_count=option_count,
            beam_width=beam_width, max_steps=max_steps, snapshot_limit=snapshot_limit,
            task_layer_size=2, whole_task_exchange='t:a_to_b,donor:b_to_a',
            first_partial_J_pruning=False, state_comparison='complete_same_all_task_coverage',
            position_policy='all_legal_PD_geometry_early_pickup_early_delivery_shortlist_then_frontier',
            position_scores_are_bounds=False, all_PD_positions_globally_scored=False,
            each_complete_proposal_standard_full_checked=True,
            complete_beam_retention='streaming_exact_top_key_routes_after_two_jobs_complete',
            ordinary_DIRECT_owner_and_order_free=True, new_relay_generation=False))
    try:
        _check(deadline, statistics)
        base_checked = evaluate_solution(problem, base, _force_full=True)
        _check(deadline, statistics)
        initial_checked = evaluate_solution(problem, initial, _force_full=True)
        _check(deadline, statistics)
        if not base_checked.valid or not initial_checked.valid or build_plan_index(problem, base).relay_task_ids:
            raise ValueError('real pure-DIRECT base and complete raw seed required')
        if seed_move is not None:
            raw_tools._verify(problem, base, initial, seed_move, relay_enabled)
        elif initial != base:
            raise ValueError('a nonoriginal seed requires actual raw provenance')
        _check(deadline, statistics)
        evaluator = GlobalRelayEvaluator(problem)
        base_schedule = evaluator.evaluate(base)
        _check(deadline, statistics)
        schedule = evaluator.evaluate(initial)
        _check(deadline, statistics)
        expected = closure.signature(problem, initial, pins, deadline=deadline)
        closure.shape(problem, initial, pinned_task_ids=pins, deadline=deadline)
        best = dict(id=0, routes=initial, schedule=schedule,
            key=weighted_key(initial_checked.score, weights, scales),
            q=[0]*problem.drone_count, checked=initial_checked)
        snapshot_arguments = dict(base_schedule=base_schedule, initial_routes=initial,
            seed_move=seed_move, pins=pins, focus=focus, deadline=deadline, statistics=statistics)
        best_snapshot = _snapshot(problem, best, initial_checked, weights, scales,
            phase='input', **snapshot_arguments)
        statistics.update(input_snapshot=best_snapshot, initialization_completed=True)
    except Deadline:
        statistics.update(skipped_reason='initialization_reached_absolute_deadline',
            runtime_seconds=perf_counter()-started, best_key=None, final_routes_sha256=None)
        return dict(routes=initial, evaluation=None, iterations=0, metadata=statistics)

    serial = 0

    def exact_insert(parent, task, owner, route, insertion):
        nonlocal serial
        _check(deadline, statistics)
        if parent['q'][owner] <= 0:
            raise ValueError('insertion requires a real released responsibility slot')
        routes = list(parent['routes'])
        routes[owner] = route
        routes = tuple(routes)
        scored = evaluator.evaluate_delta(parent['routes'], parent['schedule'], routes)
        statistics['global_delta_evaluations'] += 1
        _check(deadline, statistics)
        if not scored.feasible:
            statistics['invalid_candidates'] += 1
            return None
        try:
            closure.shape(problem, routes, pinned_task_ids=pins, deadline=deadline)
        except (ValueError, KeyError, IndexError):
            statistics['invalid_candidates'] += 1
            return None
        if set(scored.delivery_times_min) != set(parent['schedule'].delivery_times_min)|{task}:
            raise ValueError('a whole-task insertion changed unrelated coverage')
        quota = list(parent['q'])
        quota[owner] -= 1
        serial += 1
        return dict(id=serial, routes=routes, schedule=scored,
            key=weighted_key(scored.score, weights, scales), q=quota,
            insertions=(*parent.get('insertions', ()), insertion),
            owner_choices=(*parent.get('owner_choices', ()), owner))

    def full_check(state):
        _check(deadline, statistics)
        try:
            statistics['complete_full_validator_calls'] += 1
            checked = evaluate_solution(problem, state['routes'], _force_full=True)
            _check(deadline, statistics)
            if (not checked.valid or not _same_score(checked.score, state['schedule'].score)
                or closure.signature(problem, state['routes'], pins, deadline=deadline) != expected):
                statistics['full_validator_rejections'] += 1
                return False
        except (ValueError, KeyError, IndexError):
            statistics['full_validator_rejections'] += 1
            return False
        _check(deadline, statistics)
        state['checked'] = checked
        state['key'] = weighted_key(checked.score, weights, scales)
        state['full_check_completed_at_perf_counter'] = perf_counter()
        _check(deadline, statistics)
        statistics['complete_leafs_full_verified'] += 1
        return True

    def checkpoint(state, step, *, force=False):
        nonlocal best, best_snapshot
        if not force and state['key'] >= best['key']:
            return True
        try:
            snapshot = _snapshot(problem, state, state['checked'], weights, scales,
                phase='complete_conditional_donor_exchange', **snapshot_arguments)
        except (ValueError, KeyError, IndexError):
            statistics['invalid_candidates'] += 1
            return False
        state['snapshot'] = snapshot
        step['complete_checkpoints'].append(snapshot)
        if statistics['first_complete_candidate'] is None:
            statistics['first_complete_candidate'] = snapshot
        if len(statistics['full_candidate_snapshots']) < snapshot_limit:
            statistics['full_candidate_snapshots'].append(snapshot)
        if state['key'] < best['key']:
            _check(deadline, statistics)
            snapshot['selected_as_best_at_perf_counter'] = perf_counter()
            _check(deadline, statistics)
            best, best_snapshot = state, snapshot
            statistics['accepted_improvements'] += 1
            statistics['best_constructed_candidate'] = snapshot
            statistics['returned_input_guard'] = False
        return True

    states = [best]
    try:
        for step_number in range(max_steps):
            _check(deadline, statistics)
            step = dict(step_index=step_number, parent_state_ids=[state['id'] for state in states],
                parent_records=[], candidate_count=0, retained_states=[], complete_checkpoints=[], completed=False)
            statistics['steps'].append(step)
            expanded = {}
            for parent in states:
                _check(deadline, statistics)
                catalogue_statistics = {}
                parent_record = dict(parent_state_id=parent['id'], catalogue=None,
                    catalogue_statistics=catalogue_statistics, pairs=[], completed=False)
                step['parent_records'].append(parent_record)
                proposed = catalogue.propose_catalogue(problem, base, parent['routes'],
                    pinned_task_ids=pins, focus_task_ids=focus, expected_signature=expected,
                    deadline=deadline, weights=weights, scales=scales,
                    critical_limit=critical_limit, donor_limit=donor_limit,
                    statistics=catalogue_statistics)
                parent_record['catalogue'] = proposed
                _check(deadline, statistics)
                parent_digest = raw_tools._digest([list(route) for route in parent['routes']])
                index = build_plan_index(problem, parent['routes'])
                for record_index, proposal in enumerate(proposed['records']):
                    _check(deadline, statistics)
                    t, d, a, b = (proposal['t'], proposal['donor_task_id'], proposal['a'], proposal['b'])
                    if (t == d or a == b or t in pins or d in pins
                        or {t, d}-set(index.direct_task_ids)
                        or index.delivery_route_by_task[t] != a or index.delivery_route_by_task[d] != b):
                        raise ValueError('conditional catalogue differs from current complete task owners')
                    removed = closure._whole_remove(problem, parent['routes'], {t, d})
                    quota = [int(owner in (a, b)) for owner in range(problem.drone_count)]
                    preview = closure.partial_preview(problem, removed, pinned_task_ids=pins,
                        expected_signature=expected, remaining_task_ids=(t, d),
                        remaining_quota_slots=quota, deadline=deadline, global_evaluator=evaluator)
                    statistics['global_partial_evaluations'] += 1
                    pair_record = dict(catalogue_record_index=record_index, t=t, d=d, a=a, b=b,
                        partial_quota_slots=quota, candidate_count=0, insertion_orders_completed=[],
                        partial_routes_sha256=raw_tools._digest([list(route) for route in removed]), completed=False)
                    parent_record['pairs'].append(pair_record)
                    for order in permutations((t, d)):
                        _check(deadline, statistics)
                        first, second = order
                        owner = b if first == t else a
                        target = b if second == t else a
                        reduced = dict(routes=removed, schedule=preview['schedule'], q=quota,
                            insertions=(), owner_choices=())
                        for route, insertion in _frontier(problem, reduced, first, owner,
                                position_limit, option_count, deadline, statistics, pair_record):
                            one = exact_insert(reduced, first, owner, route, insertion)
                            if one is None:
                                continue
                            for second_route, second_insertion in _frontier(problem, one, second,
                                    target, position_limit, option_count, deadline, statistics, pair_record):
                                two = exact_insert(one, second, target, second_route, second_insertion)
                                if two is None:
                                    continue
                                if any(two['q']) or set(two['schedule'].delivery_times_min) != set(problem.task_ids):
                                    raise ValueError('a two-task completion failed to restore real quotas or coverage')
                                two.update(parent_id=parent['id'], order=order,
                                    parent_routes_sha256=parent_digest, partial_quota_slots=quota,
                                    exchange=dict(t=t, d=d, a=a, b=b, catalogue_record_index=record_index))
                                statistics['complete_proposals_generated'] += 1
                                pair_record['candidate_count'] += 1
                                step['candidate_count'] += 1
                                if not full_check(two):
                                    continue
                                if not checkpoint(two, step, force=not step['complete_checkpoints']):
                                    continue
                                expanded.setdefault(two['routes'], two)
                                # All compared proposals now have identical full
                                # coverage. Streaming top-k equals the final
                                # (key,routes) sort without retaining every DAG.
                                if len(expanded) > beam_width:
                                    worst = max(expanded, key=lambda routes:
                                        (expanded[routes]['key'], routes))
                                    del expanded[worst]
                        pair_record['insertion_orders_completed'].append(list(order))
                    pair_record['completed'] = True
                parent_record['completed'] = True
            _check(deadline, statistics)
            states = sorted(expanded.values(), key=lambda state: (state['key'], state['routes']))[:beam_width]
            _check(deadline, statistics)
            retained = []
            for state in states:
                if 'snapshot' not in state:
                    try:
                        state['snapshot'] = _snapshot(problem, state, state['checked'], weights, scales,
                            phase='retained_complete_conditional_donor_exchange', **snapshot_arguments)
                    except (ValueError, KeyError, IndexError):
                        statistics['invalid_candidates'] += 1
                        continue
                step['retained_states'].append(state['snapshot'])
                retained.append(state)
            states = retained
            _check(deadline, statistics)
            step['completed'] = True
            statistics['completed_steps'] += 1
            if not states:
                break
    except Deadline:
        statistics['deadline_reached'] = True
    statistics.update(post_repair_snapshot=best_snapshot, best_key=list(best['key']),
        returned_checkpoint_checked_within_deadline=best_snapshot is not None,
        final_routes_sha256=best_snapshot['routes_sha256'], runtime_seconds=perf_counter()-started)
    return dict(routes=best['routes'], evaluation=best['checked'],
        iterations=statistics['global_delta_evaluations'], metadata=statistics)
