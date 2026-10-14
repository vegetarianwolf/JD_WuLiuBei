"""Multiple real groups, changing old waits, cycles and immutable guard."""
from copy import deepcopy
from time import perf_counter

import pytest
from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import multi_relay_accumulation_v1 as method
from algorithm.tests.test_relay_repooling_exchange import topology

W, S = (.5, .25, .25), (6., 100., 100.)


def fixture():
    a, b, c = Point(-4., 0.), Point(4., 0.), Point(0., 0.)
    tasks = (Task(1, a, Point(0., 1.), 100.), Task(2, b, Point(0., 2.), 100.),
        Task(3, b, Point(-4., 1.), 100.), Task(4, c, Point(-4., 2.), 100.),
        Task(5, b, b, 100.), Task(6, b, b, 100.))
    registry = {}
    for task in tasks:
        incoming = 7+2*(task.id-1)
        registry[incoming] = TransportLeg(incoming, task.id, 'RELAY_IN', 1)
        registry[incoming+1] = TransportLeg(incoming+1, task.id, 'RELAY_OUT', 1)
    problem = Problem(tasks, drone_count=3, capacity=2, max_tasks_per_drone=2,
        speed_km_per_min=1., drone_homes=(a, b, c),
        relay_stations=(RelayStation(1, 0., 10.),), leg_registry=registry,
        task_relay_candidates={task.id: (1,) for task in tasks})
    base = ((3, 4, -3, -4), (5, -5, 6, -6), (1, 2, -1, -2))
    g1 = dict(group_id=1, i=1, j=2, a=0, b=1, c=2, station_id=1)
    g2 = dict(group_id=2, i=3, j=4, a=1, b=2, c=0, station_id=1)
    incoming = lambda t: 7+2*(t-1)
    outgoing = lambda t: incoming(t)+1
    first = ((3, 4, -3, -4, incoming(1), -incoming(1)),
        (5, -5, 6, -6, incoming(2), -incoming(2)),
        (outgoing(1), outgoing(2), -outgoing(1), -outgoing(2)))
    second = ((incoming(1), -incoming(1), outgoing(3), outgoing(4), -outgoing(3), -outgoing(4)),
        (5, -5, 6, -6, incoming(2), -incoming(2), incoming(3), -incoming(3)),
        (outgoing(1), outgoing(2), -outgoing(1), -outgoing(2), incoming(4), -incoming(4)))
    return problem, base, first, second, g1, g2


def controlled_catalogue(first, second, g1, g2, *, invalid_second=()):
    def propose(problem, routes, groups, **kwargs):
        # Only the proposal catalogue is controlled; every full score,
        # dependency, previous-group signature and physical audit is real.
        group = g1 if not groups else g2
        route_candidates = [first] if not groups else [*invalid_second, second]
        candidates = [dict(routes=[list(r) for r in rs], new_group=group,
            family_index=0, candidate_index=k) for k, rs in enumerate(route_candidates)]
        kwargs['statistics'].update(completed=True, candidate_count=len(candidates))
        return dict(families=[group], complete_candidates=candidates, completed=True,
            scoring_scope='controlled_identity_proposals_no_score_or_feasibility_claim')
    return propose


def run(problem, base, **kwargs):
    return method.refine(problem, base, deadline=perf_counter()+3., weights=W, scales=S,
        max_groups=2, beam_width=2, max_families=2, source_option_limit=1,
        receiver_option_limit=1, position_limit=3, **kwargs)


def audit(problem, base, result, *, validate_metadata=False):
    from algorithm.experiments.audit_multi_relay_accumulation_v1 import audit_step, audit_metadata
    tasks = {task.id: ((task.pickup.x, task.pickup.y), (task.delivery.x, task.delivery.y),
        task.deadline_min) for task in problem.tasks}
    graph = topology(problem)
    graph['task_relay_candidates'] = {str(t): list(problem.task_relay_candidates.get(t, ())) for t in problem.task_ids}
    meta = result['metadata']
    assert meta['started_at_perf_counter'] <= meta['input_snapshot']['full_verified_at_perf_counter'] < meta['deadline_perf_counter']
    assert meta['runtime_seconds'] >= meta['input_snapshot']['full_verified_at_perf_counter']-meta['started_at_perf_counter']
    states = {0: meta['input_snapshot']}
    for step in meta['steps']:
        for snapshot in [*step['complete_checkpoints'], *step['retained_states']]:
            assert meta['started_at_perf_counter'] <= snapshot['full_verified_at_perf_counter'] < meta['deadline_perf_counter']
            assert meta['runtime_seconds'] >= snapshot['full_verified_at_perf_counter']-meta['started_at_perf_counter']
            parent = states[snapshot['parent_state_id']]
            assert audit_step(tasks, graph, base, parent['routes'], snapshot['routes'],
                parent['groups'], snapshot['group_added'], telemetry=snapshot,
                weights=W, scales=S, deadline=meta['deadline_perf_counter'])['passed']
        states.update({snapshot['state_id']: snapshot for snapshot in step['retained_states']})
    if validate_metadata:
        assert audit_metadata(tasks, graph, base, meta, weights=W, scales=S,
            deadline=meta['deadline_perf_counter'])['passed']


def test_two_genuine_groups_continue_from_worse_first_state_and_recompute_old_wait(monkeypatch):
    problem, base, first, second, g1, g2 = fixture()
    runtime_catalogues = []
    propose = controlled_catalogue(first, second, g1, g2)
    def retain_runtime(*args, **kwargs):
        catalogue = propose(*args, **kwargs)
        runtime_catalogues.append(catalogue)
        return catalogue
    monkeypatch.setattr(method.structure, 'propose_catalogue', retain_runtime)
    result = run(problem, base)
    meta = result['metadata']
    assert meta['completed_depths'] == 2 and meta['max_group_count_retained'] == 2
    one = meta['steps'][0]['retained_states'][0]
    two = meta['steps'][1]['retained_states'][0]
    assert one['key'] > meta['guard_archive']['key']
    assert one['groups'] == [g1] and two['groups'] == [g1, g2]
    assert two['relay_task_count'] == two['cross_uav_count'] == 4
    assert one['group_metrics'][0]['receiver_waiting_min'] != two['group_metrics'][0]['receiver_waiting_min']
    assert one['group_metrics'][0]['source_availability_min'] != two['group_metrics'][0]['source_availability_min']
    assert all(value > 0. for value in two['current_shared_double_loaded_km_by_group'].values())
    assert result['routes'] == base and meta['returned_input_guard']
    assert meta['guard_archive'] == meta['input_snapshot']
    assert meta['valid_complete_candidates'] == meta['full_validator_calls'] == 2
    assert all('routes' in candidate for catalogue in runtime_catalogues for candidate in catalogue['complete_candidates'])
    assert all('routes' not in candidate for step in meta['steps'] for parent in step['parent_records']
        for candidate in parent['catalogue']['complete_candidates'])
    audit(problem, base, result)


def test_cross_group_cycle_and_old_double_take_damage_are_rejected(monkeypatch):
    problem, base, first, second, g1, g2 = fixture()
    cyclic = (second[0][2:]+second[0][:2], second[1], second[2])
    out1, out2, in4 = 8, 10, 13
    broken = (second[0], second[1], (out1, in4, -in4, out2, -out1, -out2))
    assert not evaluate_solution(problem, cyclic, _force_full=True).valid
    assert evaluate_solution(problem, broken, _force_full=True).valid  # Ordinary DAG alone permits this; group shape does not.
    monkeypatch.setattr(method.structure, 'propose_catalogue',
        controlled_catalogue(first, second, g1, g2, invalid_second=(cyclic, broken)))
    result = run(problem, base)
    meta = result['metadata']
    assert meta['rejected_full_or_global'] >= 1 and meta['rejected_structure'] >= 1
    assert meta['max_group_count_retained'] == 2 and result['evaluation'].valid
    audit(problem, base, result)


def test_task_reuse_or_changed_native_sequence_cannot_add_a_group(monkeypatch):
    problem, base, first, second, g1, g2 = fixture()
    propose = controlled_catalogue(first, second, g1, g2)
    def reused(problem, routes, groups, **kwargs):
        result = propose(problem, routes, groups, **kwargs)
        if groups:
            result['complete_candidates'][0]['new_group'] = {**g2, 'i': 1, 'j': 2}
        return result
    monkeypatch.setattr(method.structure, 'propose_catalogue', reused)
    result = run(problem, base)
    assert result['metadata']['rejected_parent_transition'] > 0
    assert result['metadata']['max_group_count_generated'] == 1
    audit(problem, base, result)


def test_family_head_diversity_preserves_different_real_group_over_cheaper_duplicate_layout(monkeypatch):
    problem, base, first, _, g1, g2 = fixture()
    same_family_other_layout = ((7, -7, 3, 4, -3, -4), first[1], first[2])
    other_group = {**g2, 'group_id': 1}
    other_family_layout = ((12, 14, -12, -14), (5, -5, 6, -6, 11, -11),
        (1, 2, -1, -2, 13, -13))
    assert evaluate_solution(problem, same_family_other_layout).score.distance_km < evaluate_solution(problem, other_family_layout).score.distance_km
    def propose(*args, **kwargs):
        records = [(first, g1), (same_family_other_layout, g1), (other_family_layout, other_group)]
        return dict(families=[g1, other_group], complete_candidates=[dict(routes=routes,
            new_group=group, family_index=int(group == other_group), candidate_index=k)
            for k, (routes, group) in enumerate(records)], completed=True)
    monkeypatch.setattr(method.structure, 'propose_catalogue', propose)
    result = method.refine(problem, base, deadline=perf_counter()+3., weights=W, scales=S,
        max_groups=1, beam_width=2)
    retained = result['metadata']['steps'][0]['retained_states']
    assert {tuple(tuple(row) for row in s['group_family_key']) for s in retained} == {method._family_key([g1]), method._family_key([other_group])}
    assert result['metadata']['steps'][0]['retained_family_count'] == 2
    audit(problem, base, result)


def test_deadline_after_first_complete_group_keeps_only_predeadline_checkpoint(monkeypatch):
    problem, base, first, second, g1, g2 = fixture()
    clock = [perf_counter()]
    deadline = clock[0]+3.
    propose = controlled_catalogue(first, second, g1, g2)
    def expire_on_second(problem, routes, groups, **kwargs):
        if groups:
            clock[0] = deadline+1.
        return propose(problem, routes, groups, **kwargs)
    monkeypatch.setattr(method, 'perf_counter', lambda: clock[0])
    monkeypatch.setattr(method.structure, 'propose_catalogue', expire_on_second)
    result = method.refine(problem, base, deadline=deadline, weights=W, scales=S,
        max_groups=2, beam_width=1)
    meta = result['metadata']
    assert meta['deadline_reached'] and meta['max_group_count_retained'] == 1
    assert meta['best_constructed_candidate']['full_verified_at_perf_counter'] < deadline
    assert result['routes'] == base and meta['post_repair_snapshot']['group_count'] == 0
    assert meta['returned_checkpoint_checked_within_deadline']
    audit(problem, base, result)


@pytest.mark.parametrize('expire_after_validation', [False, True])
def test_expired_initialization_returns_no_new_checked_plan(monkeypatch, expire_after_validation):
    problem, base, *_ = fixture()
    clock = [perf_counter()]
    deadline = clock[0]+2.
    calls = []
    actual = method.evaluate_solution
    if not expire_after_validation:
        clock[0] = deadline+1.
    monkeypatch.setattr(method, 'perf_counter', lambda: clock[0])
    def expire(*args, **kwargs):
        calls.append(1)
        checked = actual(*args, **kwargs)
        clock[0] = deadline+1.
        return checked
    monkeypatch.setattr(method, 'evaluate_solution', expire)
    result = method.refine(problem, base, deadline=deadline, weights=W, scales=S)
    assert result['evaluation'] is None and len(calls) == int(expire_after_validation)
    assert not result['metadata']['returned_checkpoint_checked_within_deadline']
    assert result['metadata']['input_snapshot'] is None


def test_input_relay_seed_or_false_direct_control_is_not_silently_accepted():
    problem, base, first, *_ = fixture()
    with pytest.raises(ValueError, match='R-only'):
        method.refine(problem, base, deadline=perf_counter()+2., weights=W, scales=S, relay_enabled=False)
    with pytest.raises(ValueError, match='pure-DIRECT'):
        method.refine(problem, first, deadline=perf_counter()+2., weights=W, scales=S)


def test_current_catalogue_and_complete_driver_integrate_without_controlled_scores():
    problem, base, *_ = fixture()
    result = run(problem, base)
    assert result['evaluation'].valid
    assert result['metadata']['full_validator_calls'] > 0
    for step in result['metadata']['steps']:
        for parent in step['parent_records']:
            catalogue = parent['catalogue']
            assert catalogue['candidate_representation'] == 'parent_plus_layouts_and_routes_sha256'
            assert catalogue['complete_candidates_contain_routes'] is False
            assert all('routes' not in candidate and {'new_group', 'routes_sha256', 'source_layouts',
                'receiver_layout', 'candidate_index', 'family_index'} <= candidate.keys()
                for candidate in catalogue['complete_candidates'])
    audit(problem, base, result, validate_metadata=True)
