"""Independent proof of mandatory new-late-set repair before native ALNS.

This is a metadata callback extension, not a batch ledger. Expected damage
sets, donor geometry, actual missing quota slots, whole-task transitions and
current clocks come from immutable standard-library auditors, never from the
producer or its insertion evaluators.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from itertools import combinations, permutations
from math import hypot, isfinite

try:
    from . import audit_joint_source_native_v1 as joint
    from . import audit_multi_relay_native_polish_v1 as native
    from . import audit_multi_relay_native_batch_v1 as paid
    from .audit_deadline_window_repooling import _bind
    from .audit_weighted_direct_pair import require
except ImportError:
    import audit_joint_source_native_v1 as joint
    import audit_multi_relay_native_polish_v1 as native
    import audit_multi_relay_native_batch_v1 as paid
    from audit_deadline_window_repooling import _bind
    from audit_weighted_direct_pair import require

SCOPE = 'new_late_set_completion_repair'
EPSILON = 1e-9
FOCUS_PARAMETERS = {
    'focus_fraction': 'fraction', 'focus_max_seconds': 'max_seconds',
    'focus_donor_limit': 'donor_limit', 'focus_max_donors': 'max_donors',
    'focus_position_limit': 'position_limit', 'focus_option_count': 'option_count',
    'focus_max_damage': 'max_damage',
}
_digest = joint.generic._digest
_active = False


def _finite(value):
    return type(value) in (int, float) and isfinite(value)


def _parameters(parameters):
    require(isinstance(parameters, dict) and set(parameters) == set(FOCUS_PARAMETERS), 'Incomplete or unexpected focused-stage parameters')
    require(_finite(parameters['focus_fraction']) and 0 < parameters['focus_fraction'] < 1
            and _finite(parameters['focus_max_seconds']) and parameters['focus_max_seconds'] > 0,
            'Focused stage needs a strict wall fraction and positive finite maximum wall')
    for name, maximum in (('focus_donor_limit', 4), ('focus_max_donors', 2),
        ('focus_position_limit', 256), ('focus_option_count', 3), ('focus_max_damage', 4)):
        require(type(parameters[name]) is int and 0 <= parameters[name] <= maximum, 'Invalid actual focused bound ' + name)
    require(parameters['focus_position_limit'] >= 1 and parameters['focus_option_count'] >= 1
            and parameters['focus_max_damage'] >= 1
            and parameters['focus_option_count'] <= parameters['focus_position_limit'], 'Focused insertion limits are inconsistent')
    return {field: parameters[name] for name, field in FOCUS_PARAMETERS.items()}


def _damage(tasks, base_reference, raw_reference, raw_owners, selected):
    rows = []
    for task in sorted(set(tasks) - set(selected)):
        base_clock = base_reference['delivery_times_min'][str(task)]
        raw_clock = raw_reference['delivery_times_min'][str(task)]
        if base_clock - tasks[task][2] <= EPSILON and raw_clock - tasks[task][2] > EPSILON:
            rows.append(dict(task_id=task, deadline_min=tasks[task][2], base_delivery_min=base_clock,
                raw_delivery_min=raw_clock, current_owner=raw_owners[task]))
    return rows


def _donor_directory(tasks, damage, owners, selected, donor_limit):
    rows = [];D = set(damage)
    for task in sorted(set(tasks) - set(selected) - D):
        if not any(owners[task] != owners[t] for t in D): continue
        def distance(t):
            P, Q = tasks[task][:2];U, V = tasks[t][:2]
            return hypot(P[0]-U[0], P[1]-U[1]) + hypot(Q[0]-V[0], Q[1]-V[1])
        closest = min(D, key=lambda t: (distance(t), t))
        rows.append(dict(task_id=task, current_owner=owners[task], geometric_score=distance(closest), closest_damage_task_id=closest))
    rows.sort(key=lambda r: (r['geometric_score'], r['current_owner'], r['task_id']))
    for rank, row in enumerate(rows): row['rank'] = rank
    kept = [r['task_id'] for r in rows[:donor_limit]]
    return rows, kept, [r['task_id'] for r in rows[donor_limit:]]


def _orders(removed, damage, donors):
    U = sorted(removed)
    if len(U) <= 3: return [list(order) for order in permutations(U)]
    streams = (U, list(reversed(U)), [*sorted(damage), *sorted(donors)], [*sorted(donors), *sorted(damage)])
    result = []
    for order in streams:
        if order not in result: result.append(order)
    return result


def _variants(damage, retained_donors, owners, max_donors, aircraft_count=None):
    """The declared finite complete assignments, including the mandatory control."""
    D = sorted(damage);result = [];seen = set()
    count = max(owners.values(), default=-1) + 1 if aircraft_count is None else aircraft_count
    def add(donors, assignment, kind, exchanges):
        U = sorted(set(D) | set(donors));identity = (tuple(U), tuple(assignment[t] for t in U))
        if identity in seen: return
        seen.add(identity)
        q = [sum(owners[t] == a for t in U) for a in range(count)]
        require(q == [sum(assignment[t] == a for t in U) for a in range(count)], 'A donor declaration does not close real released responsibilities')
        result.append(dict(variant_index=len(result), kind=kind, donor_task_ids=sorted(donors), removed_task_ids=U,
            assignment_by_task={str(t): assignment[t] for t in U}, quota_slots_by_aircraft=q,
            exchanges=exchanges, insertion_orders=_orders(U, D, donors)))
    add([], {t: owners[t] for t in D}, 'same_owner_control', [])
    if max_donors >= 1:
        for t in D:
            for donor in retained_donors:
                if owners[t] == owners[donor]: continue
                assignment = {u: owners[u] for u in D};assignment[t] = owners[donor];assignment[donor] = owners[t]
                add([donor], assignment, 'single_donor_exchange', [dict(task_id=t, donor_task_id=donor)])
    if max_donors >= 2:
        for selected in combinations(D, 2):
            for pair in combinations(retained_donors, 2):
                for donors in (pair, tuple(reversed(pair))):
                    if any(owners[t] == owners[d] for t, d in zip(selected, donors)): continue
                    assignment = {u: owners[u] for u in D}
                    for t, donor in zip(selected, donors): assignment[t] = owners[donor];assignment[donor] = owners[t]
                    add(pair, assignment, 'double_donor_exchange', [dict(task_id=t, donor_task_id=d) for t, d in zip(selected, donors)])
    return result


def _transition(tasks, topology, raw_routes, groups, removed, child_routes, assignment, order, insertions, context):
    """Every task is restored physically; different missing sets are never added."""
    registry = context['geometry'][2];U = set(removed)
    stripped = [[v for v in route if native._identity(v, registry) not in U] for route in raw_routes]
    actual_stripped = [[v for v in route if native._identity(v, registry) not in U] for route in child_routes]
    _bind(actual_stripped, stripped, 'Focused complete candidate preserves every unselected signed event')
    require(isinstance(order, list) and len(order) == len(U) and all(type(t) is int for t in order)
            and set(order) == U and len(set(order)) == len(order), 'Focused restore trace must contain its whole mandatory set and donors exactly once')
    require(isinstance(insertions, list) and len(insertions) == len(order), 'Focused restore trace lost actual whole-task insertions')
    present = set(tasks) - U;current = [list(r) for r in stripped]
    proof = native._prove(tasks, topology, raw_routes, current, present, context)
    q = proof['slots'];start_q = list(q);prefixes = []
    for task, record in zip(order, insertions):
        require(isinstance(record, dict), 'A whole-task insertion needs actual owner and gaps')
        owner = assignment[str(task)]
        require(type(owner) is int and 0 <= owner < len(current) and q[owner] > 0, 'Focused restore spends an unavailable real responsibility slot')
        _bind(record.get('task_id'), task, 'focused actual restored task')
        _bind(record.get('owner'), owner, 'focused declared quota-respecting owner')
        g, h = record.get('pickup_gap'), record.get('delivery_gap');route = current[owner]
        require(type(g) is int and type(h) is int and 0 <= g <= h <= len(route), 'Focused whole-task PD gaps are not current original-route positions')
        require(record.get('origin') in ('finite_geometry_early_pickup_early_delivery', 'seed_anchor_reconstruction'),
                'Focused insertion lost its declared finite frontier/anchor origin')
        if record['origin'] == 'seed_anchor_reconstruction':
            raw_owners = native._owners(raw_routes, registry)
            require(owner == raw_owners[task], 'A seed anchor cannot invent an original destination owner')
            positions = {v: k for k, v in enumerate(raw_routes[owner])}
            anchor_g = next((k for k, v in enumerate(route) if v in positions and positions[v] > positions[task]), len(route))
            with_pickup = [*route[:anchor_g], task, *route[anchor_g:]]
            negative = next((k for k, v in enumerate(with_pickup) if v in positions and positions[v] > positions[-task]), len(with_pickup))
            _bind([g, h], [anchor_g, max(anchor_g + 1, negative) - 1], 'Actual original-owner surviving-event anchor')
        current[owner] = [*route[:g], task, *route[g:h], -task, *route[h:]]
        present.add(task);proof = native._prove(tasks, topology, raw_routes, current, present, context)
        q = proof['slots'];prefixes.append(dict(present_task_ids=sorted(present), remaining_quota_slots=list(q),
            routes_sha256=_digest(current), score=proof['reference']['score']))
    _bind(current, child_routes, 'Focused complete route is exactly its declared joint restore trace')
    require(not any(q) and present == set(tasks), 'Focused complete candidate has unfinished quota or task coverage')
    return dict(removed_routes=stripped, quota_slots_by_aircraft=start_q, prefixes=prefixes,
        returned_routes_sha256=_digest(current), all_removed_tasks_restored=True)


def _numeric_objective(actual, expected, name):
    require(isinstance(actual, (list, tuple)) and len(actual) == len(expected) == 3
        and all(_finite(v) and v > 0 for v in (*actual, *expected))
        and all(a == b for a, b in zip(actual, expected)), 'Focused actual objective differs in ' + name)


def _summary(proof):
    return dict(score=proof['score'], routes_sha256=proof['routes_sha256'],
        full_clock_sha256=_digest(dict(event=proof['event_times_min'], delivery=proof['delivery_times_min'])))


def _audit_focus(tasks, topology, base_routes, raw_routes, groups, metadata, *, weights, scales, deadline, parameters=None):
    require(isinstance(metadata, dict), 'Missing actual complete new-late-set stage metadata')
    _bind(metadata.get('method'), 'Complete-New-Late-Native-Set-Repair-V1', 'actual focused repair implementation')
    _bind(metadata.get('pin_scope'), SCOPE, 'actual focused stage scope')
    _bind(metadata.get('groups'), groups, 'focused actual typed relay groups')
    _bind(metadata.get('deadline_perf_counter'), deadline, 'focused actual absolute deadline')
    _bind(metadata.get('base_routes_sha256'), _digest(base_routes), 'focused genuine original DIRECT reference')
    _bind(metadata.get('input_routes_sha256'), _digest(raw_routes), 'focused actual paid raw macro')
    require(type(metadata.get('seed')) is int, 'Focused stage needs an actual integer seed')
    _numeric_objective(metadata.get('weights'), weights, 'weights');_numeric_objective(metadata.get('scales'), scales, 'scales')
    started, runtime = metadata.get('started_at_perf_counter'), metadata.get('runtime_seconds')
    require(_finite(started) and _finite(runtime) and runtime >= 0, 'Focused actual wall clocks must be finite')
    cfg = metadata.get('configuration');require(isinstance(cfg, dict), 'Missing actual focused finite limits')
    for name, lower, upper in (('donor_limit', 0, 4), ('max_donors', 0, 2), ('position_limit', 1, 256),
        ('option_count', 1, 3), ('max_damage', 1, 4), ('snapshot_limit', 0, 64)):
        require(type(cfg.get(name)) is int and lower <= cfg[name] <= upper, 'Invalid actual focused finite bound ' + name)
    require(cfg['option_count'] <= cfg['position_limit'], 'Focused option frontier exceeds actual position surface')
    if parameters is not None:
        actual_parameters = _parameters(parameters)
        for field in ('donor_limit', 'max_donors', 'position_limit', 'option_count', 'max_damage'):
            _bind(cfg[field], actual_parameters[field], 'Focused actual protocol bound ' + field)
    for field, expected in (('control_frontier_limit', cfg['position_limit']), ('donor_frontier_limit', cfg['option_count']),
        ('maximum_anchor_extra_per_frontier', 1), ('frontier_omission_counters_include_explored_anchor', True),
        ('extra_original_owner_seed_anchor_branch', True), ('partial_J_pruning', False),
        ('complete_U_required_before_score_comparison', True),
        ('position_policy', 'geometry_early_pickup_early_delivery_heads_then_geometry_fill'),
        ('donor_policy', 'min_D_pickup_plus_delivery_distance_then_current_owner_then_task'),
        ('order_policy', 'all_permutations_up_to_three_else_four_deterministic_orders'),
        ('proof_scope', 'saved_complete_checkpoints_and_started_variant_removal_partials'),
        ('unrecorded_probes_independently_replayed', False)):
        _bind(cfg.get(field), expected, 'focused actual finite policy ' + field)
    context = native._context(tasks, topology, raw_routes, groups)
    original = native.reference_schedule(tasks, base_routes, topology)
    require(not original['handoffs'], 'Focused original comparison must really be complete pure DIRECT')
    require(context['counts'] == [r['task_count'] for r in original['route_records']], 'Focused raw and original reference differ in per-aircraft final quotas')
    counters = ('partial_global_checks', 'complete_checks', 'global_delta_evaluations', 'invalid_partial_candidates',
        'complete_candidates', 'complete_proposals_generated', 'complete_validator_rejections',
        'completed_variants', 'started_variants', 'completed_orders', 'accepted_improvements',
        'legal_PD_positions', 'frontier_positions_retained', 'frontier_positions_omitted',
        'primary_frontier_positions_retained', 'primary_frontier_positions_omitted', 'extra_original_position_branches')
    for field in counters:
        require(type(metadata.get(field)) is int and metadata[field] >= 0, 'Focused actual counter must be nonnegative: ' + field)
    if metadata.get('initialization_completed') is not True:
        require(metadata.get('initialization_completed') is False and metadata.get('deadline_reached') is True
            and metadata.get('selection_completed') is False, 'Unpublished focused initialization needs honest expiry')
        require(all(metadata.get(k) is None for k in ('input_snapshot', 'post_repair_snapshot', 'first_complete_candidate', 'best_constructed_candidate', 'final_key'))
            and all(metadata.get(k) == [] for k in ('best_checkpoint_history', 'full_candidate_snapshots', 'variants', 'variant_directory',
                'D_all', 'D_native', 'D_protected', 'donor_directory', 'retained_donor_task_ids', 'omitted_donor_task_ids'))
            and metadata.get('returned_checkpoint_checked_within_deadline') is False
            and metadata['accepted_improvements'] == 0, 'Expired focus fabricated selection, a complete cache or adoption')
        return dict(passed=True, initialization_completed=False, predeadline_checkpoint_verified=False,
            returned_routes_sha256=_digest(raw_routes), saved_complete_entity_references=[], all_unrecorded_probes_entity_replayed=False)
    raw_owners = native._owners(raw_routes, context['geometry'][2]);pins = context['selected']
    _bind(metadata.get('pinned_task_ids'), sorted(pins), 'Focused true union of all typed group tasks')
    _bind(metadata.get('pin_signature'), context['signature'], 'Focused all-group plan/subsequence signature')
    rows_all = _damage(tasks, original, context['macro_reference'], raw_owners, set())
    D_all = [r['task_id'] for r in rows_all];D = [t for t in D_all if t not in pins]
    for field, expected in (('D_all', D_all), ('D_native', D), ('D_protected', sorted(set(D_all) & pins))):
        _bind(metadata.get(field), expected, 'Focused exact original-relative complete damage set ' + field)
    refs = {};by_snapshot = {};partials = [];ordered = [];transitions = []
    complete_counts = generated_counts = rejected_counts = partial_counts = completed_orders = completed_variants = 0
    def complete(snapshot):
        require(isinstance(snapshot, dict), 'A focused complete cache must be its actual saved entity')
        token = _digest(snapshot)
        if token not in refs:
            p = native.audit_complete_state(tasks, topology, raw_routes, groups, snapshot['routes'], telemetry=snapshot,
                weights=weights, scales=scales, deadline=deadline)
            require(started <= snapshot['full_verified_at_perf_counter'] <= started + runtime,
                'Focused complete cache was not checked in its actual run')
            refs[token] = dict(snapshot_sha256=token, **_summary(p));by_snapshot[token] = p
        return by_snapshot[token]
    initial = metadata['input_snapshot'];complete(initial)
    _bind(initial.get('phase'), 'input_new_late_set_repair', 'Focused actual complete input phase')
    _bind(initial['routes'], raw_routes, 'Focused complete input is actual raw macro')
    _bind(initial.get('parent_routes_sha256'), None, 'Focused input cannot invent a restore parent')
    _bind(metadata.get('initial_key'), initial['key'], 'Focused input complete objective')
    declarations = metadata.get('variant_directory');variants = metadata.get('variants')
    require(isinstance(declarations, list) and isinstance(variants, list), 'Missing focused declaration/started-variant prefix')
    if metadata.get('selection_completed') is not True:
        require(metadata.get('selection_completed') is False and metadata.get('deadline_reached') is True,
                'An unfinished focused catalogue must be an honest expiry')
        require(declarations == variants == [] and metadata['donor_directory'] == metadata['retained_donor_task_ids'] == metadata['omitted_donor_task_ids'] == [],
                'Unfinished focused selection published an alleged complete directory')
    elif not D or len(D) > cfg['max_damage']:
        reason = 'no_newly_late_native_tasks' if not D else 'new_late_native_set_exceeds_max_damage'
        _bind(metadata.get('skipped_reason'), reason, 'Focused whole-set skip, without damage truncation')
        require(declarations == variants == [] and metadata['donor_directory'] == metadata['retained_donor_task_ids'] == metadata['omitted_donor_task_ids'] == [],
                'A skipped focused set cannot claim donor/restore coverage')
    else:
        directory, donors, omitted = _donor_directory(tasks, D, raw_owners, pins, cfg['donor_limit'])
        _bind(metadata.get('donor_directory'), directory, 'Focused complete real donor geometry directory')
        _bind(metadata.get('retained_donor_task_ids'), donors, 'Focused actual finite donor pool')
        _bind(metadata.get('omitted_donor_task_ids'), omitted, 'Focused declared donor omissions in geometry order')
        expected = _variants(D, donors, raw_owners, cfg['max_donors'], len(raw_routes))
        _bind(declarations, expected, 'Focused mandatory-control and all declared conditional assignments/orders')
        require(len(variants) <= len(declarations), 'Focused started variants exceed actual declared finite pool')
    for index, variant in enumerate(variants):
        declaration = declarations[index]
        _bind({k: variant.get(k) for k in declaration}, declaration, 'Focused actual declared variant prefix')
        U = set(declaration['removed_task_ids'])
        require(set(D) <= U and not (U & pins) and U == set(D) | set(declaration['donor_task_ids']), 'Every focused variant must remove all mandatory damage and only real ordinary donors')
        removal = [[v for v in route if native._identity(v, context['geometry'][2]) not in U] for route in raw_routes]
        saved = variant.get('removal_partial');require(isinstance(saved, dict), 'Focused started variant lost actual removal partial entity')
        p = native.audit_partial_state(tasks, topology, raw_routes, groups, removal, sorted(set(tasks)-U),
            telemetry=saved, weights=weights, scales=scales, deadline=deadline)
        require(started <= saved['checked_at_perf_counter'] <= started + runtime, 'Focused removal partial was not checked within actual stage')
        _bind(p['remaining_quota_slots'], declaration['quota_slots_by_aircraft'], 'Focused real slots released by whole U deletion')
        partials.append(dict(variant_index=index, snapshot_sha256=_digest(saved), **_summary(p)))
        orders = variant.get('orders');require(isinstance(orders, list) and len(orders) <= len(declaration['insertion_orders']), 'Focused order probe prefix exceeds declared orders')
        order_totals = complete_total = generated_total = rejected_total = 0
        for number, record in enumerate(orders):
            _bind(record.get('order_index'), number, 'Focused actual restore-order index')
            _bind(record.get('task_ids'), declaration['insertion_orders'][number], 'Focused actual whole U restore-order prefix')
            require(all(type(record.get(k)) is int and record[k] >= 0 for k in ('partial_candidates', 'complete_candidates',
                'complete_proposals_generated', 'complete_validator_rejections')), 'Focused order counters are invalid')
            require(record['complete_candidates'] + record['complete_validator_rejections'] <= record['complete_proposals_generated'] <= record['partial_candidates'],
                'Focused complete/check/reject counts exceed actual whole insertion probes')
            if record.get('completed') is True:
                require(record['complete_candidates'] + record['complete_validator_rejections'] == record['complete_proposals_generated'],
                    'Completed focused order lost full-validator accounting')
            order_totals += record['partial_candidates'];complete_total += record['complete_candidates']
            generated_total += record['complete_proposals_generated'];rejected_total += record['complete_validator_rejections']
            if record.get('completed') is True: completed_orders += 1
            else:
                require(record.get('completed') is False and metadata['deadline_reached'] and index == len(variants)-1
                    and number == len(orders)-1, 'Only the terminal deadline order may be unfinished')
        _bind(variant.get('partial_candidates'), order_totals, 'Focused actual variant partial-probe sum')
        _bind(variant.get('complete_candidates'), complete_total, 'Focused actual variant complete-probe sum')
        _bind(variant.get('complete_proposals_generated'), generated_total, 'Focused actual variant full-proposal sum')
        _bind(variant.get('complete_validator_rejections'), rejected_total, 'Focused actual variant full-reject sum')
        if variant.get('completed') is True:
            require(len(orders) == len(declaration['insertion_orders']) and all(o['completed'] for o in orders), 'A completed focused variant omitted declared restore orders')
            completed_variants += 1
        else:
            require(variant.get('completed') is False and metadata['deadline_reached'] and index == len(variants)-1, 'Only the last focused variant may stop at deadline')
        saved_candidates = variant.get('saved_complete_checkpoints');require(isinstance(saved_candidates, list), 'Focused complete checkpoint collection must be explicit')
        require(len(saved_candidates) <= complete_total, 'More saved focused complete candidates than actual full checks')
        if complete_total and not saved_candidates: require(metadata['deadline_reached'] and index == len(variants)-1, 'The first completed candidate of each finished variant must be saved')
        for snapshot in saved_candidates:
            proof = complete(snapshot);_bind(snapshot.get('phase'), 'complete_new_late_set_transaction', 'Focused complete restore phase')
            _bind(snapshot.get('parent_routes_sha256'), _digest(raw_routes), 'Focused complete restore genuine raw parent')
            _bind(snapshot.get('variant_index'), index, 'Focused complete checkpoint true variant')
            oi = snapshot.get('insertion_order_index');require(type(oi) is int and 0 <= oi < len(orders), 'Focused saved candidate was not reached by its order prefix')
            require(orders[oi]['complete_candidates'] > 0, 'Focused saved candidate has no full probe in the declared order')
            _bind(snapshot.get('removed_task_ids'), declaration['removed_task_ids'], 'Focused complete whole removed U')
            _bind(snapshot.get('assignment_by_task'), declaration['assignment_by_task'], 'Focused complete quota-closing assignment')
            restored = _transition(tasks, topology, raw_routes, groups, declaration['removed_task_ids'], snapshot['routes'],
                declaration['assignment_by_task'], orders[oi]['task_ids'], snapshot.get('insertion_trace'), context)
            _bind(snapshot.get('repaired_damage_task_ids'), [t for t in D if proof['delivery_times_min'][str(t)]-tasks[t][2] <= EPSILON], 'Focused true original-damage rescue status')
            ordered.append(snapshot);transitions.append(dict(snapshot_sha256=_digest(snapshot), variant_index=index,
                insertion_order_index=oi, all_removed_tasks_restored=restored['all_removed_tasks_restored'],
                real_removal_quota_slots=restored['quota_slots_by_aircraft'], restored_prefix_references=restored['prefixes']))
        complete_counts += complete_total;partial_counts += order_totals
        generated_counts += generated_total;rejected_counts += rejected_total
    if not metadata.get('deadline_reached'):
        require(len(variants) == len(declarations) and all(v['completed'] for v in variants), 'Finished focused stage omitted a declared complete variant')
    for field, value in (('started_variants', len(variants)), ('completed_variants', completed_variants),
        ('completed_orders', completed_orders), ('complete_candidates', complete_counts), ('global_delta_evaluations', partial_counts)):
        _bind(metadata[field], value, 'Focused actual finite probe accounting ' + field)
    _bind(metadata['complete_proposals_generated'], generated_counts, 'Focused actual generated whole full-coverage probes')
    _bind(metadata['complete_validator_rejections'], rejected_counts, 'Focused actual rejected complete full-validator probes')
    require(metadata['invalid_partial_candidates'] <= partial_counts and metadata['complete_checks'] >= complete_counts + 1,
            'Focused physical check counters omit actual complete checkpoints')
    require(metadata['frontier_positions_retained'] == metadata['primary_frontier_positions_retained'] + metadata['extra_original_position_branches']
        and metadata['frontier_positions_omitted'] == metadata['primary_frontier_positions_omitted'] - metadata['extra_original_position_branches']
        and metadata['legal_PD_positions'] == metadata['frontier_positions_retained'] + metadata['frontier_positions_omitted'],
        'Focused frontier accounting cannot hide extra explored original anchors')
    _bind(metadata.get('full_candidate_snapshots'), ordered[:cfg['snapshot_limit']], 'Focused actual bounded chronological stored checkpoints')
    _bind(metadata.get('first_complete_candidate'), ordered[0] if ordered else None, 'Focused actual first published complete restore')
    best = initial;history = [];last_at = started
    for snapshot in ordered:
        if tuple(snapshot['key']) < tuple(best['key']):
            at = snapshot.get('selected_as_best_at_perf_counter')
            require(_finite(at) and snapshot['full_verified_at_perf_counter'] <= at < deadline
                and last_at <= at <= started + runtime, 'Focused strict-best adoption missed its actual verified paid deadline')
            history.append(snapshot);best = snapshot;last_at = at
        else:
            require('selected_as_best_at_perf_counter' not in snapshot, 'A nonimproving focused candidate falsely claims strict best adoption')
    _bind(metadata.get('best_checkpoint_history'), history, 'Focused strict complete internal best history')
    _bind(metadata.get('best_constructed_candidate'), history[-1] if history else None, 'Focused actual best newly constructed full state')
    _bind(metadata['accepted_improvements'], len(history), 'Focused strict complete internal improvement count')
    _bind(metadata.get('post_repair_snapshot'), best, 'Focused actual returned earlier complete cache')
    _bind(metadata.get('final_key'), best['key'], 'Focused actual guarded complete return key')
    _bind(metadata.get('returned_input_checkpoint'), best['routes'] == initial['routes'], 'Focused actual input-cache return flag')
    _bind(metadata.get('returned_checkpoint_checked_within_deadline'), True, 'Focused returned cache has real predeadline full verification')
    return dict(passed=True, initialization_completed=True, predeadline_checkpoint_verified=True,
        exact_damage_task_ids=D, all_newly_late_task_ids=D_all, protected_newly_late_task_ids=sorted(set(D_all)&pins),
        exact_damage_rows=[row for row in rows_all if row['task_id'] in D], donor_directory_sha256=_digest(metadata['donor_directory']),
        declared_variants=len(declarations), started_variants=len(variants), completed_variants=completed_variants,
        saved_complete_entity_references=list(refs.values()), removal_partial_references=partials,
        complete_transition_references=transitions, returned_routes_sha256=_digest(best['routes']), returned_score=best['score'],
        all_unrecorded_probes_entity_replayed=False, omitted_position_frontiers_claimed_bounded=False)


def audit_focused_stage(tasks, topology, base_routes, raw_routes, groups, metadata, *, weights, scales, deadline, parameters=None):
    """One actual stage; old raw provenance is bound by the outer paid callback."""
    with (nullcontext() if joint._active else joint.joint_source_context()):
        return _audit_focus(tasks, topology, base_routes, raw_routes, groups, metadata,
            weights=weights, scales=scales, deadline=deadline, parameters=parameters)


@contextmanager
def _focused_audit_context(base_routes, parameters, statistics):
    global _active
    if _active: raise RuntimeError('Nested focused-native independent audit contexts are unsupported')
    original = paid.polish_audit.audit_metadata;references = []
    def proof(tasks, topology, raw_routes, groups, metadata, *, weights, scales, deadline):
        stage = metadata.get('focused_stage');require(isinstance(stage, dict), 'Focused native leaf lost its real preceding stage')
        _bind(stage.get('base_routes_sha256'), _digest(base_routes), 'Focused native true common original comparison')
        _bind(stage.get('original_raw_routes_sha256'), _digest(raw_routes), 'Focused native true paid raw macro')
        start, subdeadline = stage.get('focus_started_at_perf_counter'), stage.get('focus_deadline_perf_counter')
        require(_finite(start) and _finite(subdeadline), 'Focused allocation clocks must be finite')
        expected = min(deadline, start + min(parameters['focus_max_seconds'], max(0., deadline-start) * parameters['focus_fraction']))
        _bind(subdeadline, expected, 'Focused actual fraction/capped wall allocation')
        require(subdeadline <= deadline, 'Focused stage received time past its actual native leaf')
        focus = stage.get('metadata');require(isinstance(focus, dict), 'Missing actual focused stage metadata')
        require(start <= focus['started_at_perf_counter'] <= metadata['started_at_perf_counter'], 'Focused stage/native initialization order is false')
        require(focus['started_at_perf_counter'] + focus['runtime_seconds'] <= metadata['started_at_perf_counter'] + 1e-8,
                'Native stage began before focused repair returned')
        _bind(focus.get('seed'), metadata['seed'], 'Focused actual leaf seed')
        _bind(focus['configuration']['snapshot_limit'], metadata['snapshot_limit'], 'Focused actual evidence cap')
        fp = audit_focused_stage(tasks, topology, base_routes, raw_routes, groups, focus,
            weights=weights, scales=scales, deadline=subdeadline, parameters=parameters)
        used = fp['predeadline_checkpoint_verified']
        _bind(stage.get('focus_used'), used, 'Focused actual cached-return use flag')
        focused = focus['post_repair_snapshot']['routes'] if used else raw_routes
        _bind(stage.get('focused_return_routes_sha256'), _digest(focused), 'Focused actual cached return route identity')
        _bind(stage.get('native_input_routes_sha256'), _digest(focused), 'Actual native initialization input after focus')
        np = original(tasks, topology, focused, groups, metadata, weights=weights, scales=scales, deadline=deadline)
        _bind(stage.get('native_initialization_failed'), not np['predeadline_checkpoint_verified'], 'Focused/native honest unchecked initialization fallback')
        references.append(dict(focused_stage_evidence=fp, native_evidence=np, focused_stage_record_sha256=_digest(stage)))
        return references[-1]
    _active = True
    try:
        paid.polish_audit.audit_metadata = proof
        statistics['independent_metadata_hook_installed'] = paid.polish_audit.audit_metadata is proof
        yield references
    finally:
        paid.polish_audit.audit_metadata = original
        _active = False
        statistics['independent_metadata_hook_restored'] = paid.polish_audit.audit_metadata is original


def _audit_metadata(tasks, topology, base_routes, metadata, *, relay, manifest, phase=None):
    """Validate only the added stage, reusing the existing paid/typed callback."""
    require(type(relay) is bool, 'Focused independent arm must be a real boolean')
    if not relay: return joint._audit_metadata(tasks, topology, base_routes, metadata, relay=False, manifest=manifest, phase=phase)
    actual = manifest['method_parameters']['multi_native_relay'];focus = {name: actual[name] for name in FOCUS_PARAMETERS}
    _parameters(focus)
    core_params = {name: value for name, value in actual.items() if name not in FOCUS_PARAMETERS}
    core_manifest = {**manifest, 'method_parameters': {**manifest['method_parameters'], 'multi_native_relay': core_params}}
    extension = metadata.get('damage_extension');require(isinstance(extension, dict), 'Focused arm needs its actual one-call context receipt')
    for field, expected in (('name', 'new_late_set_before_native_context_v1'), ('parameters', focus),
        ('base_routes_sha256', _digest(base_routes)), ('alias_name', 'wrapper.polish.polish_groups'),
        ('hook_identity', dict(installed=True, restored=True)), ('restored', True)):
        _bind(extension.get(field), expected, 'Actual focused callable extension ' + field)
    entered, left, elapsed = (extension.get(k) for k in ('entered_at_perf_counter', 'left_at_perf_counter', 'elapsed_seconds'))
    require(all(_finite(v) for v in (entered, left, elapsed)) and entered <= metadata['started_at_perf_counter'] <= left,
            'Focused context clocks do not contain the actual paid core')
    _bind(elapsed, left-entered, 'Focused actual context paid wall')
    require(metadata['started_at_perf_counter'] + metadata['runtime_seconds'] <= left + 1e-8, 'Focused context left before core returned')
    if phase is not None: require(phase['started_at_perf_counter'] <= entered <= left <= phase['ended_at_perf_counter'], 'Focused setup/teardown lies outside actual worker paid phase')
    for entry in metadata.get('polish_records', []):
        if not entry.get('returned'): continue
        helper = entry.get('metadata');require(isinstance(helper, dict), 'A returned focused leaf lost its actual native metadata')
        stage = helper.get('focused_stage');require(isinstance(stage, dict), 'A returned focused leaf lost its preceding stage')
        focused = stage.get('metadata');require(isinstance(focused, dict), 'A returned focused leaf lost its repair metadata')
        clocks = (entry.get('allocation_started_at_perf_counter'), stage.get('focus_started_at_perf_counter'),
            focused.get('started_at_perf_counter'), helper.get('started_at_perf_counter'), entry.get('returned_at_perf_counter'))
        require(all(_finite(v) for v in clocks) and list(clocks) == sorted(clocks),
                'Focused repair/native clocks precede the actual paid leaf allocation or return')
        require(metadata['started_at_perf_counter'] <= clocks[0] <= clocks[-1] <= metadata['started_at_perf_counter'] + metadata['runtime_seconds'] + 1e-8,
                'Focused repair/native leaf lies outside its actual core execution')
    stats = {}
    with _focused_audit_context(base_routes, focus, stats):
        result = joint._audit_metadata(tasks, topology, base_routes, metadata, relay=True, manifest=core_manifest, phase=phase)
    return dict(**result, focused_native_extension_evidence=dict(extension_sha256=_digest(extension),
        validated_focus_parameters=focus, original_manifest_not_modified=True, **stats))
