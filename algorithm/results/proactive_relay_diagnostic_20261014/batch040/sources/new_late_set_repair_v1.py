"""Complete transactions for all newly late ordinary tasks, with real donors.

Call inside the existing typed structure context. Local geometry/current-clock
frontiers only limit proposals; every partial uses its actual present-set DAG.
No score comparison discards a branch before its whole declared U is restored.
"""
from __future__ import annotations

from itertools import combinations, permutations
from math import isfinite
from time import perf_counter

from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
try:
    import multi_relay_native_polish_v1 as native
except ImportError:
    from . import multi_relay_native_polish_v1 as native

Deadline = native.Deadline
SCOPE = 'new_late_set_completion_repair'
EPS = 1e-9


def _check(deadline, stats):
    if perf_counter() >= deadline:
        stats['deadline_reached'] = True
        raise Deadline


def _late(p, schedule):
    return {t for t, clock in schedule.delivery_times_min.items()
        if clock-p.task(t).deadline_min > EPS}


def _orders(damage, donors):
    jobs = tuple(sorted((*damage, *donors)))
    if len(jobs) <= 3:
        return list(permutations(jobs))
    result = []
    for order in (jobs, tuple(reversed(jobs)), tuple(sorted(damage))+tuple(sorted(donors)),
                  tuple(sorted(donors))+tuple(sorted(damage))):
        if order not in result:
            result.append(order)
    return result


def _directory(p, index, damage, pins, donor_limit, max_donors, deadline, stats):
    owners = index.delivery_route_by_task
    rows = []
    for d in sorted(set(index.direct_task_ids)-set(damage)-pins):
        _check(deadline, stats)
        if not any(owners[d] != owners[t] for t in damage):
            continue
        geometric, nearest = min((p.task(d).pickup.distance_to(p.task(t).pickup)
            + p.task(d).delivery.distance_to(p.task(t).delivery), t) for t in damage)
        rows.append(dict(task_id=d, current_owner=owners[d], geometric_score=float(geometric),
            closest_damage_task_id=nearest))
    rows.sort(key=lambda row: (row['geometric_score'], row['current_owner'], row['task_id']))
    for rank, row in enumerate(rows):
        row['rank'] = rank
    retained = [r['task_id'] for r in rows[:donor_limit]]
    variants, seen = [], set()
    def add(donors, assignment, kind, exchanges):
        _check(deadline, stats)
        U = sorted(set(damage)|set(donors))
        identity = (tuple(U), tuple((t, assignment[t]) for t in U))
        if identity in seen:
            return
        seen.add(identity)
        quota = [sum(owners[t] == a for t in U) for a in range(p.drone_count)]
        if quota != [sum(assignment[t] == a for t in U) for a in range(p.drone_count)]:
            raise ValueError('declared whole-task assignments do not fill actual released quotas')
        variants.append(dict(variant_index=len(variants), kind=kind, donor_task_ids=sorted(donors),
            removed_task_ids=U, assignment_by_task={str(t): assignment[t] for t in U},
            quota_slots_by_aircraft=quota, exchanges=exchanges,
            insertion_orders=[list(order) for order in _orders(damage, donors)]))
    initial = {t: owners[t] for t in damage}
    add((), initial, 'same_owner_control', [])
    if max_donors >= 1:
        for t in damage:
            for d in retained:
                if owners[t] == owners[d]:
                    continue
                assignment = {**initial, t: owners[d], d: owners[t]}
                add((d,), assignment, 'single_donor_exchange', [dict(task_id=t, donor_task_id=d)])
    if max_donors >= 2:
        for pair in combinations(damage, 2):
            for donors in combinations(retained, 2):
                for order in permutations(donors):
                    if any(owners[t] == owners[d] for t, d in zip(pair, order)):
                        continue
                    assignment = dict(initial)
                    for t, d in zip(pair, order):
                        assignment[t], assignment[d] = owners[d], owners[t]
                    add(donors, assignment, 'double_donor_exchange',
                        [dict(task_id=t, donor_task_id=d) for t, d in zip(pair, order)])
    _check(deadline, stats)
    return rows, retained, variants


def _banned(p, route, owner, groups):
    """Original gaps inside a protected adjacent take/drop pair."""
    banned = set()
    for group in groups:
        if group['c'] == owner:
            ids = [next(v for v, leg in p.leg_registry.items() if leg.task_id == t
                and leg.relay_id == group['station_id'] and leg.kind == 'RELAY_OUT')
                for t in (group['i'], group['j'])]
            positions = sorted(route.index(v) for v in ids)
            if positions[1] != positions[0]+1:
                raise ValueError('protected double take was already split')
            banned.add(positions[1])
        if group.get('source_type') == 'joint_pair' and group['a'] == owner:
            ids = [next(v for v, leg in p.leg_registry.items() if leg.task_id == t
                and leg.relay_id == group['station_id'] and leg.kind == 'RELAY_IN')
                for t in (group['i'], group['j'])]
            positions = sorted(route.index(-v) for v in ids)
            if positions[1] != positions[0]+1:
                raise ValueError('protected joint double drop was already split')
            banned.add(positions[1])
    return banned


def _insert(route, task, g, h):
    return route[:g]+(task,)+route[g:h]+(-task,)+route[h:]


def _original_option(route, seed_route, task):
    """Anchor original P/D to surviving seed events, preserving current order."""
    positions = {v: k for k, v in enumerate(seed_route)}
    if task not in positions or -task not in positions:
        return None
    g = next((k for k, v in enumerate(route) if v in positions and positions[v] > positions[task]), len(route))
    with_pickup = route[:g]+(task,)+route[g:]
    negative = next((k for k, v in enumerate(with_pickup)
        if v in positions and positions[v] > positions[-task]), len(with_pickup))
    negative = max(g+1, negative)
    return g, negative-1


def _frontier(p, state, task, owner, groups, seed_routes, original_owner, limit, deadline, stats):
    route = state['routes'][owner];n = len(route);loads = [0]
    for v in route:
        loads.append(loads[-1]+(1 if v > 0 else -1))
    banned = _banned(p, route, owner, groups)
    points = [p.point_for_visit(v) for v in route]
    P, D, V = p.task(task).pickup, p.task(task).delivery, p.speed_km_per_min
    items = []
    for g in range(n+1):
        _check(deadline, stats)
        if loads[g] >= 2:
            continue
        u = p.home_points()[owner] if not g else points[g-1]
        prefix = 0. if not g else state['schedule'].event_times_min[owner, g-1]
        pickup = prefix+u.distance_to(P)/V
        for h in range(g, n+1):
            _check(deadline, stats)
            if loads[h] >= 2:
                break
            if g in banned or h in banned:
                continue
            if h == g:
                delta = u.distance_to(P)+P.distance_to(D)
                if h < n:
                    delta += D.distance_to(points[h])-u.distance_to(points[h])
                end = pickup+P.distance_to(D)/V
            else:
                first = u.distance_to(P)+P.distance_to(points[g])-u.distance_to(points[g])
                end = state['schedule'].event_times_min[owner, h-1]+first/V+points[h-1].distance_to(D)/V
                delta = first+points[h-1].distance_to(D)
                if h < n:
                    delta += D.distance_to(points[h])-points[h-1].distance_to(points[h])
            items.append((float(delta), float(pickup), float(end), g, h))
    streams = (sorted(items), sorted(items, key=lambda r:(r[1],r[0],r[2],r[3:])),
        sorted(items, key=lambda r:(r[2],r[0],r[1],r[3:])))
    kept, seen = [], set()
    for row in (*[s[0] for s in streams if s], *streams[0]):
        _check(deadline, stats)
        if row[3:] not in seen and len(kept) < limit:
            seen.add(row[3:]);kept.append((row, 'finite_geometry_early_pickup_early_delivery'))
    stats['legal_PD_positions'] += len(items)
    stats['primary_frontier_positions_retained'] += len(kept)
    stats['primary_frontier_positions_omitted'] += len(items)-len(kept)
    stats['frontier_positions_retained'] += len(kept)
    stats['frontier_positions_omitted'] += len(items)-len(kept)
    if owner == original_owner:
        original = _original_option(route, seed_routes[owner], task)
        if original is not None and original not in seen:
            g, h = original
            if g not in banned and h not in banned and max(loads[g:h+1]) < 2:
                kept.append(((None,None,None,g,h), 'seed_anchor_reconstruction'))
                stats['extra_original_position_branches'] += 1
                stats['frontier_positions_retained'] += 1
                stats['frontier_positions_omitted'] -= 1
    return [(_insert(route, task, row[3], row[4]), dict(task_id=task, owner=owner,
        pickup_gap=row[3], delivery_gap=row[4], origin=origin)) for row, origin in kept]


def refine(problem, base_routes, raw_routes, groups, *, deadline, weights, scales, seed=0,
           donor_limit=4, max_donors=2, position_limit=12, option_count=3,
           max_damage=4, snapshot_limit=6):
    started = perf_counter();weights, scales = tuple(weights), tuple(scales)
    if (type(deadline) is bool or not isfinite(deadline) or type(seed) is not int
        or any(type(v) is not int or not 0 <= v <= limit for v,limit in
            ((donor_limit,4),(max_donors,2),(snapshot_limit,64)))
        or any(type(v) is not int or not 1 <= v <= limit for v,limit in
            ((position_limit,256),(option_count,3),(max_damage,4))) or option_count > position_limit):
        raise ValueError('finite deadline and declared bounded new-late repair parameters required')
    native._key(native.Score(0,0.,0.), weights, scales)
    initial, base = tuple(map(tuple,raw_routes)), tuple(map(tuple,base_routes))
    groups = [dict(g) for g in groups]
    stats = dict(method='Complete-New-Late-Native-Set-Repair-V1', pin_scope=SCOPE, seed=seed,
        weights=list(weights), scales=list(scales), groups=groups,
        started_at_perf_counter=started, deadline_perf_counter=deadline, deadline_reached=False,
        initialization_completed=False, selection_completed=False, input_snapshot=None,
        post_repair_snapshot=None, first_complete_candidate=None, best_constructed_candidate=None,
        best_checkpoint_history=[], full_candidate_snapshots=[], variants=[], variant_directory=[],
        D_all=[], D_native=[], D_protected=[], donor_directory=[], retained_donor_task_ids=[],
        omitted_donor_task_ids=[], base_routes_sha256=native.metrics._digest(base),
        input_routes_sha256=native.metrics._digest(initial), partial_global_checks=0, complete_checks=0,
        global_delta_evaluations=0, invalid_partial_candidates=0, complete_candidates=0,
        complete_proposals_generated=0,complete_validator_rejections=0,
        completed_variants=0, started_variants=0, completed_orders=0, accepted_improvements=0,
        legal_PD_positions=0, frontier_positions_retained=0, frontier_positions_omitted=0,
        primary_frontier_positions_retained=0,primary_frontier_positions_omitted=0,
        extra_original_position_branches=0, returned_checkpoint_checked_within_deadline=False,
        configuration=dict(donor_limit=donor_limit,max_donors=max_donors,position_limit=position_limit,
            option_count=option_count,max_damage=max_damage,snapshot_limit=snapshot_limit,
            control_frontier_limit=position_limit,donor_frontier_limit=option_count,
            maximum_anchor_extra_per_frontier=1,
            frontier_omission_counters_include_explored_anchor=True,
            extra_original_owner_seed_anchor_branch=True,partial_J_pruning=False,
            complete_U_required_before_score_comparison=True,
            position_policy='geometry_early_pickup_early_delivery_heads_then_geometry_fill',
            donor_policy='min_D_pickup_plus_delivery_distance_then_current_owner_then_task',
            order_policy='all_permutations_up_to_three_else_four_deterministic_orders',
            proof_scope='saved_complete_checkpoints_and_started_variant_removal_partials',
            unrecorded_probes_independently_replayed=False))
    evaluator = GlobalRelayEvaluator(problem)
    best_routes, best_checked, best_snapshot, best_key = initial, None, None, None
    try:
        _check(deadline, stats)
        base_checked = evaluate_solution(problem, base, _force_full=True);_check(deadline, stats)
        base_index = build_plan_index(problem, base)
        if not base_checked.valid or base_index.relay_task_ids:
            raise ValueError('original strong base must be complete pure DIRECT')
        base_schedule = evaluator.evaluate(base);_check(deadline, stats)
        if not base_schedule.feasible or not native._same_score(base_checked.score,base_schedule.score):
            raise ValueError('base full validation/current DAG disagree')
        expected = native.structure.signature(problem, initial, groups, deadline=deadline)
        checked, initial_preview = native._complete(problem, initial, groups, expected,evaluator,deadline,stats)
        if expected['final_counts'] != [sum(a==v for v in base_index.delivery_route_by_task.values())
                for a in range(problem.drone_count)]:
            raise ValueError('base and current input final responsibility counts differ')
        initial_schedule = initial_preview['schedule']
        snapshot = native._snapshot(problem,initial_preview,checked,groups,expected,initial,initial_schedule,
            weights,scales,deadline,stats,phase='input_new_late_set_repair')
        _check(deadline, stats)
        best_checked,best_snapshot,best_key = checked,snapshot,native._key(checked.score,weights,scales)
        stats.update(initialization_completed=True,input_snapshot=snapshot,pin_signature=expected,
            pinned_task_ids=expected['protected_task_ids'],initial_key=list(best_key))
        pins=set(expected['protected_task_ids']);index=initial_preview['index']
        all_damage = _late(problem,initial_schedule)-_late(problem,base_schedule)
        damage = sorted(all_damage & set(index.direct_task_ids)-pins)
        stats.update(D_all=sorted(all_damage),D_native=damage,D_protected=sorted(all_damage-set(damage)))
        _check(deadline, stats)
        if not damage or len(damage)>max_damage:
            stats.update(selection_completed=True,skipped_reason='no_newly_late_native_tasks' if not damage else 'new_late_native_set_exceeds_max_damage')
        else:
            rows, donors, variants = _directory(problem,index,damage,pins,donor_limit,max_donors,deadline,stats)
            stats.update(donor_directory=rows,retained_donor_task_ids=donors,
                omitted_donor_task_ids=[r['task_id'] for r in rows[donor_limit:]],
                variant_directory=variants,selection_completed=True)
            owners=index.delivery_route_by_task
            def checkpoint(state,variant,order_record):
                nonlocal best_routes,best_checked,best_snapshot,best_key
                stats['complete_proposals_generated']+=1
                variant['complete_proposals_generated']+=1
                order_record['complete_proposals_generated']+=1
                try:
                    full, complete = native._complete(problem,state['routes'],groups,expected,evaluator,deadline,stats)
                except (ValueError,KeyError,IndexError):
                    stats['complete_validator_rejections']+=1
                    variant['complete_validator_rejections']+=1
                    order_record['complete_validator_rejections']+=1
                    return
                stats['complete_candidates'] += 1;variant['complete_candidates'] += 1
                order_record['complete_candidates'] += 1
                key=native._key(full.score,weights,scales)
                should_save=not variant['saved_complete_checkpoints'] or key<best_key
                if not should_save:
                    return
                snap=native._snapshot(problem,complete,full,groups,expected,initial,initial_schedule,
                    weights,scales,deadline,stats,phase='complete_new_late_set_transaction',parent_routes=initial)
                snap.update(variant_index=variant['variant_index'],insertion_order_index=order_record['order_index'],
                    insertion_trace=state['trace'],removed_task_ids=variant['removed_task_ids'],
                    assignment_by_task=variant['assignment_by_task'],
                    repaired_damage_task_ids=sorted(set(damage)-_late(problem,complete['schedule'])))
                _check(deadline, stats)
                improves=key<best_key
                if improves:
                    selected=perf_counter()
                    _check(deadline, stats)
                    snap['selected_as_best_at_perf_counter']=selected
                # Publish a saved complete state and its strict-best update
                # together; expiry cannot expose an unadopted better entity.
                variant['saved_complete_checkpoints'].append(snap)
                if stats['first_complete_candidate'] is None:stats['first_complete_candidate']=snap
                if len(stats['full_candidate_snapshots'])<snapshot_limit:stats['full_candidate_snapshots'].append(snap)
                if improves:
                    best_routes,best_checked,best_snapshot,best_key=state['routes'],full,snap,key
                    stats['best_checkpoint_history'].append(snap);stats['best_constructed_candidate']=snap
                    stats['accepted_improvements']+=1
            for declaration in variants:
                _check(deadline, stats)
                U=set(declaration['removed_task_ids'])
                partial=tuple(tuple(v for v in route if abs(v) not in U) for route in initial)
                preview=native._preview(problem,partial,groups,expected,evaluator,deadline=deadline,statistics=stats)
                if set(preview['remaining_task_ids'])!=U or preview['remaining_quota_slots']!=declaration['quota_slots_by_aircraft']:
                    raise ValueError('whole-set removal differs from declared real missing tasks or quotas')
                evidence=native._partial_evidence(preview,weights,scales,deadline,stats)
                variant=dict(**declaration,removal_partial=evidence,orders=[],completed=False,
                    partial_candidates=0,complete_candidates=0,complete_proposals_generated=0,
                    complete_validator_rejections=0,
                    saved_complete_checkpoints=[])
                stats['variants'].append(variant);stats['started_variants']+=1
                for number,order in enumerate(declaration['insertion_orders']):
                    _check(deadline, stats)
                    record=dict(order_index=number,task_ids=order,partial_candidates=0,
                        complete_candidates=0,complete_proposals_generated=0,completed=False)
                    record['complete_validator_rejections']=0
                    variant['orders'].append(record)
                    limit=position_limit if declaration['kind']=='same_owner_control' else option_count
                    def visit(state,offset):
                        _check(deadline, stats)
                        if offset==len(order):
                            if state['remaining_task_ids'] or any(state['remaining_quota_slots']):
                                raise ValueError('a declared transaction did not restore all tasks and quotas')
                            checkpoint(state,variant,record);return
                        t=order[offset];a=declaration['assignment_by_task'][str(t)]
                        if state['remaining_quota_slots'][a]<=0:raise ValueError('assignment has no real released owner slot')
                        for route,step in _frontier(problem,state,t,a,groups,initial,owners[t],limit,deadline,stats):
                            _check(deadline, stats)
                            candidate=list(state['routes']);candidate[a]=route;candidate=tuple(candidate)
                            scheduled=evaluator.evaluate_delta(state['routes'],state['schedule'],candidate)
                            stats['global_delta_evaluations']+=1;record['partial_candidates']+=1;variant['partial_candidates']+=1
                            _check(deadline, stats)
                            if not scheduled.feasible:
                                stats['invalid_partial_candidates']+=1;continue
                            try:
                                child=native._preview(problem,candidate,groups,expected,evaluator,
                                    deadline=deadline,statistics=stats,schedule=scheduled)
                            except (ValueError,KeyError,IndexError):
                                stats['invalid_partial_candidates']+=1;continue
                            if set(child['present_task_ids'])!=set(state['present_task_ids'])|{t}:
                                raise ValueError('whole insertion changed unrelated task coverage')
                            child['trace']=[*state['trace'],step]
                            visit(child,offset+1)
                    visit({**preview,'trace':[]},0)
                    _check(deadline, stats)
                    record['completed']=True;stats['completed_orders']+=1
                _check(deadline, stats)
                variant['completed']=True;stats['completed_variants']+=1
    except Deadline:
        stats['deadline_reached']=True
    stats.update(post_repair_snapshot=best_snapshot,final_key=list(best_key) if best_key is not None else None,
        returned_checkpoint_checked_within_deadline=best_snapshot is not None,
        returned_input_checkpoint=best_routes==initial,runtime_seconds=perf_counter()-started)
    return dict(routes=best_routes,evaluation=best_checked,iterations=stats['global_delta_evaluations'],metadata=stats)
