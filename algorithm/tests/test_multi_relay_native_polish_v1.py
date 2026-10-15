"""Synthetic multi-group weighted runtime checks; no official routes/data."""
from copy import deepcopy
from dataclasses import replace
from random import Random
from time import perf_counter
from types import SimpleNamespace

import pytest

from uav_dispatch import Point, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import multi_relay_native_polish_v1 as polish
from algorithm.tests.test_multi_relay_accumulation_v1 import fixture as groups_fixture
from algorithm.tests.test_relay_preserving_polish import real_weighted_runtime

W, S = (.5, .25, .25), (9., 100., 100.)


def fixture():
    """Two groups share three planes, use different H, and have five free jobs."""
    p, _, _, second, g1, g2 = groups_fixture()
    h2 = RelayStation(2, -4., 10.)
    native = (Task(7, Point(-4., 10.), Point(-4., 10.), 5.),
        Task(8, Point(-4., 2.), Point(-4., 2.), 5.),
        Task(9, Point(4., 0.), Point(4., 0.), 5.))
    registry = {}
    for task in (*p.tasks, *native):
        for station in (1, 2):
            for k, kind in enumerate(('RELAY_IN', 'RELAY_OUT')):
                identifier = 100*station+1+2*(task.id-1)+k
                registry[identifier] = TransportLeg(identifier, task.id, kind, station)
    replacement = {}
    for identifier, leg in p.leg_registry.items():
        station = 2 if leg.task_id in (3, 4) else 1
        replacement[identifier] = 100*station+1+2*(leg.task_id-1)+int(leg.kind == 'RELAY_OUT')
    p = replace(p, tasks=(*p.tasks, *native), max_tasks_per_drone=3,
        relay_stations=(*p.relay_stations, h2), leg_registry=registry,
        task_relay_candidates={t.id: (1, 2) for t in (*p.tasks, *native)})
    rows = tuple(tuple((1 if v > 0 else -1)*replacement.get(abs(v), abs(v)) for v in r) for r in second)
    routes = tuple(rows[a]+(7+a, -(7+a)) for a in range(3))
    groups = [g1, {**g2, 'station_id': 2}]
    assert evaluate_solution(p, routes, _force_full=True).valid
    polish.structure.signature(p, routes, groups)
    return p, routes, groups


def config(module, iterations=2):
    return module.ALNSConfig(max_iterations=iterations, relay_min_destroy_fraction=.56,
        relay_max_destroy_fraction=.56, repair_rank_candidate_limit=24,
        repair_exact_candidate_limit=24, direct_exact_top_k=6,
        initial_temperature=.0025, minimum_temperature=.00001, cooling_rate=.97)


def run(module, p, routes, groups, **kwargs):
    return polish.polish_groups(p, routes, groups, deadline=kwargs.pop('deadline', perf_counter()+2.),
        weights=W, scales=S, seed=19, alns_module=module,
        config=kwargs.pop('config', config(module)), **kwargs)


def assert_snapshot(p, routes, groups, snapshot):
    proposed = tuple(tuple(r) for r in snapshot['routes'])
    checked = evaluate_solution(p, proposed, _force_full=True)
    schedule = GlobalRelayEvaluator(p).evaluate(proposed)
    assert checked.valid and schedule.feasible and polish._same_score(checked.score, schedule.score)
    assert snapshot['score'] == snapshot['post_global_score'] == polish.metrics._physical(schedule.score)
    assert snapshot['event_times_min'] == [[schedule.event_times_min[a, k] for k in range(len(r))]
        for a, r in enumerate(proposed)]
    assert snapshot['delivery_times_min'] == {str(t): v for t, v in sorted(schedule.delivery_times_min.items())}
    assert snapshot['structure_signature'] == polish.structure.signature(p, routes, groups)
    assert polish.structure.signature(p, proposed, groups) == snapshot['structure_signature']
    assert snapshot['pin_scope'] == polish.SCOPE and snapshot['groups'] == groups
    assert snapshot['pinned_task_ids'] == [1, 2, 3, 4]
    assert snapshot['relay_task_count'] == snapshot['cross_uav_count'] == 4
    assert set(snapshot['current_shared_double_loaded_km_by_group']) == {'1', '2'}
    assert all(d > 1e-10 for d in snapshot['current_shared_double_loaded_km_by_group'].values())
    assert snapshot['full_verified_at_perf_counter'] < snapshot['deadline_perf_counter']
    assert 'table_score' not in snapshot and 'topology' not in snapshot


def instrument_solver(monkeypatch, module, callback):
    monkeypatch.setitem(module.__dict__, '_test_callback', callback)
    # The actual module/global preflight still applies; only synthetic control
    # boundaries are varied, never scores, routes or physical validators.
    exec('def _test_solve(problem, *, config, initial_routes):\n    return _test_callback(problem, config, initial_routes)\n', module.__dict__)
    monkeypatch.setattr(module, 'solve_alns', module._test_solve)


def test_actual_weighted_two_group_loop_reassigns_native_and_restores_runtime(real_weighted_runtime):
    module = real_weighted_runtime
    p, routes, groups = fixture()
    original = {n: getattr(module, n) for n in polish.PATCH_NAMES}
    score_functions = {n: getattr(module.Score, n) for n in ('__lt__', '__le__', '__gt__', '__ge__')}
    result = run(module, p, routes, groups)
    meta = result['metadata']
    assert result['evaluation'].valid and result['iterations'] == 2
    assert meta['destroy_calls'] == meta['repair_calls'] == 2
    assert meta['complete_valid_candidates'] > 0 and meta['direct_exact_global_evaluations'] > 0
    assert any(s['changed_owner_task_ids'] for s in [*meta['full_candidate_snapshots'], meta['post_repair_snapshot']])
    assert meta['final_key'] <= meta['initial_key']
    assert any(s['group_metrics'] != meta['input_snapshot']['group_metrics'] for s in meta['full_candidate_snapshots'])
    assert meta['configuration']['initial_temperature'] == .0025
    assert meta['configuration']['minimum_temperature'] == .00001
    assert meta['configuration']['cooling_rate'] == .97
    assert not meta['configuration']['enable_budget_cooling']
    for snap in [meta['input_snapshot'], *meta['full_candidate_snapshots'], meta['post_repair_snapshot']]:
        assert_snapshot(p, routes, groups, snap)
    for snap in meta['partial_state_snapshots']:
        index = build_plan_index(p, snap['routes'])
        assert not snap['complete'] and snap['scoring_scope'] == 'partial_same_coverage_only'
        assert set(snap['present_task_ids']) == set(index.task_plans)
        assert set(snap['remaining_task_ids']).isdisjoint({1, 2, 3, 4})
        assert sum(snap['remaining_quota_slots']) == len(snap['remaining_task_ids'])
        assert snap['remaining_quota_slots'] == [3-n for n in snap['final_counts']]
    assert meta['patches_restored'] and meta['objective_state_restored'] and meta['score_comparators_restored']
    assert all(getattr(module, n) is fn for n, fn in original.items())
    assert all(getattr(module.Score, n) is fn for n, fn in score_functions.items())
    assert not module._weighted_adapter.active and module._weighted_adapter.coefficients is None
    assert not polish._active
    from algorithm.experiments.audit_multi_relay_native_polish_v1 import audit_metadata
    from algorithm.tests.test_relay_repooling_exchange import topology
    tasks = {t.id: ((t.pickup.x, t.pickup.y), (t.delivery.x, t.delivery.y), t.deadline_min) for t in p.tasks}
    graph = topology(p)
    graph['task_relay_candidates'] = {str(t): list(p.task_relay_candidates[t]) for t in p.task_ids}
    assert audit_metadata(tasks, graph, routes, groups, meta, weights=W, scales=S,
        deadline=meta['deadline_perf_counter'])['passed']


def test_partial_q_uses_true_target_counts_and_rejects_half_pins_and_negative_slots():
    p, routes, groups = fixture()
    expected = polish.structure.signature(p, routes, groups)
    evaluator = GlobalRelayEvaluator(p)
    stats = {'partial_global_checks': 0}
    partial = tuple(tuple(v for v in r if abs(v) not in {7, 9}) for r in routes)
    got = polish._preview(p, partial, groups, expected, evaluator, deadline=perf_counter()+2., statistics=stats)
    assert got['remaining_quota_slots'] == [1, 0, 1]
    broken = (partial[0][1:], partial[1], partial[2])
    with pytest.raises((ValueError, KeyError)):
        polish._preview(p, broken, groups, expected, evaluator, deadline=perf_counter()+2., statistics=stats)
    # A valid K=4 route can still violate the input's actual [3,3,3] target.
    relaxed = replace(p, max_tasks_per_drone=4)
    moved = (partial[0]+(7, -7), partial[1]+(9, -9), partial[2])
    assert evaluate_solution(relaxed, moved, _force_full=True).valid
    with pytest.raises(ValueError, match='slots'):
        polish._preview(relaxed, moved, groups, expected, GlobalRelayEvaluator(relaxed),
            deadline=perf_counter()+2., statistics=stats)


@pytest.mark.parametrize('damage', ['take_gap', 'pin_order', 'new_relay', 'cycle'])
def test_invalid_complete_rolls_back_and_gets_no_acceptance_reward(real_weighted_runtime, monkeypatch, damage):
    module = real_weighted_runtime
    p, routes, groups = fixture()
    candidate = [list(r) for r in routes]
    if damage == 'take_gap':
        candidate[2].remove(9); candidate[2].remove(-9)
        candidate[2][1:1] = [9, -9]
        assert evaluate_solution(p, candidate, _force_full=True).valid
    elif damage == 'pin_order':
        candidate[2][0], candidate[2][1] = candidate[2][1], candidate[2][0]
        assert evaluate_solution(p, candidate, _force_full=True).valid
    elif damage == 'new_relay':
        # Extra relay on native7 is prohibited even if all plans remain paired.
        candidate[0] = [213 if v == 7 else -213 if v == -7 else v for v in candidate[0]]
        candidate[1] += [214, -214]
    else:
        candidate[0] = candidate[0][2:]+candidate[0][:2]
        assert not GlobalRelayEvaluator(p).evaluate(candidate).feasible
    candidate = tuple(tuple(r) for r in candidate)
    monkeypatch.setitem(module.__dict__, '_test_bad', candidate)
    exec('def _test_repair(*args, **kwargs):\n    return _test_bad\n', module.__dict__)
    monkeypatch.setattr(module, '_repair', module._test_repair)
    def callback(problem, cfg, initial):
        partial, removed = module.destroy_solution(problem, module.RouteEvaluator(problem), initial, 'random', 5, Random(1))
        repaired = module._repair(problem, None, partial, removed, 'greedy', 12, deadline=perf_counter()+1.)
        assert repaired == initial
        checked = evaluate_solution(problem, initial, _force_full=True)
        assert module._accept_worse(checked.score, checked.score, problem, 1., Random(2)) is False
        return SimpleNamespace(iterations=1, metadata={})
    instrument_solver(monkeypatch, module, callback)
    result = run(module, p, routes, groups)
    assert result['routes'] == routes
    assert result['metadata']['rollback_candidates'] == result['metadata']['rejected_acceptances'] == 1
    assert result['metadata']['complete_valid_candidates'] == 0
    assert result['metadata']['patches_restored']


def test_expired_initialization_never_publishes_or_replays_after_deadline(real_weighted_runtime, monkeypatch):
    module = real_weighted_runtime
    p, routes, groups = fixture()
    monkeypatch.setattr(polish, 'evaluate_solution', lambda *a, **k: (_ for _ in ()).throw(AssertionError('late replay')))
    result = run(module, p, routes, groups, deadline=perf_counter()-1.)
    assert result['evaluation'] is None and result['routes'] == routes
    assert result['metadata']['input_snapshot'] is result['metadata']['post_repair_snapshot'] is None
    assert not result['metadata']['returned_checkpoint_checked_within_deadline']


def test_initialization_expiry_mid_full_has_no_checkpoint(real_weighted_runtime, monkeypatch):
    module = real_weighted_runtime
    p, routes, groups = fixture()
    now = [perf_counter()]; deadline = now[0]+1.
    original = polish.evaluate_solution
    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        now[0] = deadline+1.
        return result
    monkeypatch.setattr(polish, 'perf_counter', lambda: now[0])
    monkeypatch.setattr(polish, 'evaluate_solution', slow)
    result = run(module, p, routes, groups, deadline=deadline)
    assert result['evaluation'] is None and result['metadata']['input_snapshot'] is None
    assert not result['metadata']['initialization_completed']


def test_solver_cleanup_expiry_returns_only_cached_full_checkpoint(real_weighted_runtime, monkeypatch):
    module = real_weighted_runtime
    p, routes, groups = fixture()
    now = [perf_counter()]; deadline = now[0]+2.
    original_solve = module.solve_alns
    def callback(problem, cfg, initial):
        result = original_solve(problem, config=cfg, initial_routes=initial)
        now[0] = deadline+1.
        return result
    instrument_solver(monkeypatch, module, callback)
    monkeypatch.setattr(polish, 'perf_counter', lambda: now[0])
    result = run(module, p, routes, groups, deadline=deadline)
    meta = result['metadata']
    assert result['evaluation'].valid and meta['deadline_reached']
    assert meta['post_repair_snapshot'] in [meta['input_snapshot'], *meta['full_candidate_snapshots'], *meta['best_checkpoint_history']]
    assert meta['post_repair_snapshot']['full_verified_at_perf_counter'] < deadline
    assert meta['returned_checkpoint_checked_within_deadline'] and meta['patches_restored']


def test_exception_nested_and_actual_module_objective_preflights_restore(real_weighted_runtime, monkeypatch):
    module = real_weighted_runtime
    p, routes, groups = fixture()
    old = {name: getattr(module, name) for name in polish.PATCH_NAMES}
    def callback(problem, cfg, initial):
        with pytest.raises(RuntimeError, match='nested'):
            run(module, problem, initial, groups)
        raise RuntimeError('deliberate exception')
    instrument_solver(monkeypatch, module, callback)
    old['solve_alns'] = module.solve_alns
    with pytest.raises(RuntimeError, match='deliberate'):
        run(module, p, routes, groups)
    assert not polish._active and not module._weighted_adapter.active
    assert all(getattr(module, n) is old[n] for n in polish.PATCH_NAMES)
    with pytest.raises(ValueError, match='configuration'):
        run(module, p, routes, groups, config=object())
    with module._weighted_adapter.objective(module.Score, S, (.6, .2, .2)):
        with pytest.raises(ValueError, match='differs'):
            run(module, p, routes, groups)


def test_zero_shared_or_undeclared_group_cannot_initialize(real_weighted_runtime):
    module = real_weighted_runtime
    p, routes, groups = fixture()
    with pytest.raises(ValueError):
        run(module, p, routes, groups[:1])
    bad = deepcopy(groups); bad[1]['station_id'] = 1
    with pytest.raises(ValueError):
        run(module, p, routes, bad)
    zero = replace(p, tasks=tuple(replace(t, delivery=Point(-4., 10.)) if t.id in (3, 4) else t for t in p.tasks))
    assert evaluate_solution(zero, routes, _force_full=True).valid
    with pytest.raises(ValueError, match='positive'):
        run(module, zero, routes, groups)


def test_native_can_interleave_source_leg_and_receiver_deliveries_without_releasing_pins():
    p, routes, groups = fixture()
    expected = polish.structure.signature(p, routes, groups)
    candidate = [list(r) for r in routes]
    candidate[0].remove(7); candidate[0].remove(-7)
    first_delivery = candidate[0].index(-206)
    candidate[0][first_delivery+1:first_delivery+1] = [7, -7]
    candidate[1].remove(5); candidate[1].remove(-5)
    source_pickup = candidate[1].index(205)
    candidate[1][source_pickup+1:source_pickup+1] = [5, -5]
    candidate = tuple(tuple(r) for r in candidate)
    checked, value = polish._complete(p, candidate, groups, expected, GlobalRelayEvaluator(p),
        perf_counter()+2., {'complete_checks': 0, 'partial_global_checks': 0})
    assert checked.valid and not value['remaining_task_ids']
    assert polish.structure.signature(p, candidate, groups) == expected
