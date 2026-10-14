"""Conditional donors, complete joint costs, pin physics and deadline evidence."""
from time import perf_counter

import pytest
from uav_dispatch import Point, Problem, Task
from uav_dispatch.relay import build_plan_index
from algorithm.experiments import conditional_donor_repair_v1 as method
from algorithm.tests.test_relay_repooling_exchange import topology
from algorithm.tests.test_split_feeder_refinement_v4 import separated_fixture

W, S = (.5, .25, .25), (4., 100., 100.)


def fixture():
    home_a, home_b = Point(0., 0.), Point(10., 0.)
    problem = Problem((Task(1, home_b, home_b, 100.), Task(2, home_b, home_b, 100.),
        Task(3, home_a, home_a, 100.)), drone_count=2, capacity=2,
        max_tasks_per_drone=2, speed_km_per_min=1., drone_homes=(home_a, home_b))
    return problem, ((1, -1), (2, -2, 3, -3))


def run(problem, base, **kwargs):
    return method.refine(problem, base, base, deadline=perf_counter()+3., weights=W, scales=S,
        relay_enabled=False, focus_task_ids=(1,), critical_limit=3, donor_limit=3,
        position_limit=4, option_count=2, beam_width=2, max_steps=1, **kwargs)


def audit(problem, base, raw, result, move=None, relay=False):
    from algorithm.experiments.audit_conditional_donor_repair_v1 import audit_checkpoint, audit_step, audit_catalogue, audit_metadata
    tasks = {task.id: ((task.pickup.x, task.pickup.y), (task.delivery.x, task.delivery.y),
        task.deadline_min) for task in problem.tasks}
    graph = topology(problem)
    graph['task_relay_candidates'] = {str(t): list(problem.task_relay_candidates.get(t, ())) for t in problem.task_ids}
    meta = result['metadata']
    initial = meta['input_snapshot']
    assert initial is not None
    states = {initial['state_id']: initial}
    snapshots = [initial, meta['post_repair_snapshot'], *meta['full_candidate_snapshots'],
        meta['first_complete_candidate'], meta['best_constructed_candidate']]
    for step in meta['steps']:
        for record in step['parent_records']:
            if record['catalogue'] is not None:
                parent = states[record['parent_state_id']]
                assert audit_catalogue(tasks, graph, base, parent['routes'], record['catalogue'],
                    pinned_task_ids=meta['pinned_task_ids'], focus_task_ids=meta['focus_task_ids'],
                    seed_move=move, relay_enabled=relay, original_raw_routes=raw,
                    weights=W, scales=S)['passed']
        snapshots.extend(step['complete_checkpoints'])
        snapshots.extend(step['retained_states'])
        for snapshot in [*step['complete_checkpoints'], *step['retained_states']]:
            parent = states[snapshot['parent_state_id']]
            descriptor = {**snapshot['exchange'], **{field: snapshot[field] for field in
                ('inserted_task_order', 'owner_choices', 'insertions', 'parent_routes_sha256')}}
            assert audit_step(tasks, graph, base, raw, parent['routes'], snapshot['routes'], move,
                relay_enabled=relay, step_record=descriptor, telemetry=snapshot,
                focus_task_ids=meta['focus_task_ids'], weights=W, scales=S,
                deadline=meta['deadline_perf_counter'])['passed']
            assert snapshot['partial_quota_slots'] == [int(a in (descriptor['a'], descriptor['b']))
                for a in range(problem.drone_count)]
            assert not any(snapshot['remaining_quota_slots'])
        states.update({snapshot['state_id']: snapshot for snapshot in step['retained_states']})
    for snapshot in snapshots:
        if snapshot is not None:
            assert audit_checkpoint(tasks, graph, base, raw, snapshot['routes'], move,
                relay_enabled=relay, telemetry=snapshot, focus_task_ids=meta['focus_task_ids'],
                weights=W, scales=S, deadline=meta['deadline_perf_counter'])['passed']
    assert audit_metadata(tasks, graph, base, raw, meta, seed_move=move,
        focus_task_ids=meta['focus_task_ids'], relay_enabled=relay,
        weights=W, scales=S, deadline=meta['deadline_perf_counter'])['passed']


def test_actual_conditional_donor_can_choose_nonlowest_id_and_close_real_quota():
    problem, base = fixture()
    result = run(problem, base)
    meta = result['metadata']
    assert result['evaluation'].valid and result['evaluation'].score.distance_km == 0.
    snapshot = meta['post_repair_snapshot']
    assert {snapshot['exchange']['t'], snapshot['exchange']['d']} == {1, 3}
    assert build_plan_index(problem, result['routes']).delivery_route_by_task == {1: 1, 2: 1, 3: 0}
    assert meta['complete_leafs_full_verified'] == meta['complete_proposals_generated'] > 0
    assert not meta['configuration']['first_partial_J_pruning']
    assert any(pair['insertion_orders_completed'] == [[pair['t'], pair['d']], [pair['d'], pair['t']]]
        for parent in meta['steps'][0]['parent_records'] for pair in parent['pairs'])
    audit(problem, base, base, result)


def test_complete_beam_can_continue_from_worse_than_input_without_partial_objective_gate():
    problem, base = fixture()
    result = method.refine(problem, base, base, deadline=perf_counter()+3., weights=W, scales=S,
        relay_enabled=False, focus_task_ids=(1,), critical_limit=2, donor_limit=2,
        position_limit=3, option_count=1, beam_width=2, max_steps=2)
    meta = result['metadata']
    assert meta['completed_steps'] == 2
    assert all(snapshot['complete'] for step in meta['steps'] for snapshot in step['retained_states'])
    assert len(meta['steps'][1]['parent_records']) == len(meta['steps'][0]['retained_states'])
    assert any(snapshot['key'] > meta['best_key'] for snapshot in meta['steps'][0]['retained_states'])
    assert result['evaluation'].valid and result['evaluation'].score.distance_km == 0.
    audit(problem, base, base, result)


def test_real_separated_relay_seed_keeps_roles_empty_consecutive_takes_and_native_between_deliveries():
    problem, base = separated_fixture()
    raw = method.raw_tools.refine(problem, base, deadline=perf_counter()+3., weights=W, scales=S,
        relay_enabled=True, max_families=1, base_pool_limit=4)
    proposal = next(snapshot for snapshot in raw['metadata']['full_candidate_snapshots']
        if tuple(tuple(route) for route in snapshot['routes']) == raw['routes'])
    result = method.refine(problem, base, raw['routes'], seed_move=proposal['move'],
        focus_task_ids=(1, 2), deadline=perf_counter()+3., weights=W, scales=S,
        relay_enabled=True, critical_limit=2, donor_limit=1, position_limit=3,
        option_count=1, beam_width=2, max_steps=1)
    meta = result['metadata']
    assert result['evaluation'].valid and meta['complete_leafs_full_verified'] > 0
    assert set(build_plan_index(problem, result['routes']).relay_task_ids) == {1, 2}
    assert meta['post_repair_snapshot']['current_handoff']['shared_loaded_distance_km'] > 0.
    audit(problem, base, raw['routes'], result, proposal['move'], True)


def test_deadline_after_improvement_returns_saved_full_entity_without_late_validation(monkeypatch):
    problem, base = fixture()
    clock = [perf_counter()]
    deadline = clock[0]+3.
    frontier, evaluate = method._frontier, method.evaluate_solution
    monkeypatch.setattr(method, 'perf_counter', lambda: clock[0])
    def expire_after_gain(*args, **kwargs):
        if args[-2]['accepted_improvements']:
            clock[0] = deadline+1.
        return frontier(*args, **kwargs)
    def no_late_full(*args, **kwargs):
        assert clock[0] < deadline
        return evaluate(*args, **kwargs)
    monkeypatch.setattr(method, '_frontier', expire_after_gain)
    monkeypatch.setattr(method, 'evaluate_solution', no_late_full)
    result = method.refine(problem, base, base, deadline=deadline, weights=W, scales=S,
        relay_enabled=False, focus_task_ids=(1,), critical_limit=3, donor_limit=3,
        position_limit=4, option_count=2, max_steps=2)
    meta = result['metadata']
    assert meta['deadline_reached'] and meta['accepted_improvements'] > 0
    assert meta['returned_checkpoint_checked_within_deadline']
    assert meta['post_repair_snapshot']['full_verified_at_perf_counter'] < deadline
    assert any(not step['completed'] for step in meta['steps'])
    audit(problem, base, base, result)


@pytest.mark.parametrize('expire_after_first', [False, True])
def test_expired_initialization_has_no_new_evaluation_or_checkpoint(monkeypatch, expire_after_first):
    problem, base = fixture()
    clock = [perf_counter()]
    deadline = clock[0]+2.
    calls = [0]
    evaluate = method.evaluate_solution
    if not expire_after_first:
        clock[0] = deadline+1.
    monkeypatch.setattr(method, 'perf_counter', lambda: clock[0])
    def expire(*args, **kwargs):
        calls[0] += 1
        result = evaluate(*args, **kwargs)
        clock[0] = deadline+1.
        return result
    monkeypatch.setattr(method, 'evaluate_solution', expire)
    result = method.refine(problem, base, base, deadline=deadline, weights=W, scales=S, relay_enabled=False)
    meta = result['metadata']
    assert result['evaluation'] is None and calls[0] == int(expire_after_first)
    assert meta['input_snapshot'] is None and meta['post_repair_snapshot'] is None
    assert not meta['initialization_completed'] and not meta['returned_checkpoint_checked_within_deadline']


def test_invalid_full_candidate_is_rejected_and_original_complete_guard_retained(monkeypatch):
    problem, base = fixture()
    snapshot = method._snapshot
    def reject(*args, **kwargs):
        if kwargs['phase'] != 'input':
            raise ValueError('controlled invalid snapshot')
        return snapshot(*args, **kwargs)
    monkeypatch.setattr(method, '_snapshot', reject)
    result = run(problem, base)
    assert result['routes'] == base and result['evaluation'].valid
    assert result['metadata']['invalid_candidates'] > 0
    assert not result['metadata']['accepted_improvements']
    audit(problem, base, base, result)


def test_nonoriginal_direct_seed_without_provenance_and_invalid_bounds_are_rejected():
    problem, base = fixture()
    with pytest.raises(ValueError, match='provenance'):
        method.refine(problem, base, tuple(reversed(base)), deadline=perf_counter()+2.,
            weights=W, scales=S, relay_enabled=False)
    with pytest.raises(ValueError, match='bounds'):
        method.refine(problem, base, base, deadline=perf_counter()+2., weights=W, scales=S,
            relay_enabled=False, option_count=4)
