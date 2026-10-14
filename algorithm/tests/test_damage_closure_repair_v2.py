"""DIRECT pair distance uses cargo task IDs, with frozen v1 search unchanged."""
from time import perf_counter

import pytest
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import damage_closure_repair as frozen
from algorithm.experiments import damage_closure_repair_v2 as corrected
from algorithm.experiments import window_damage_repair as wrapper
from algorithm.tests.test_damage_closure_repair import move, W, S
from algorithm.tests.test_relay_damage_closure import fixture


PINS = frozenset((1, 2))


def snapshot(module, p, base, routes):
    checked = evaluate_solution(p, routes, _force_full=True)
    assert checked.valid
    evaluator = GlobalRelayEvaluator(p)
    return module._snapshot(p, routes, checked, evaluator.evaluate(routes), W, S,
        base_schedule=evaluator.evaluate(base), move=move(), phase='regression_complete')


def native_before_and_after(*, simultaneous=True):
    p, base, raw = fixture(direct=True)
    pair = (1, 2, -1, -2) if simultaneous else (1, -1, 2, -2)
    routes = (raw[0], raw[1], (9, -9, *pair, 10, -10), *raw[3:])
    assert evaluate_solution(p, routes, _force_full=True).valid
    return p, base, routes


def test_same_owner_direct_pair_counts_only_actual_joint_segment_between_native_tasks():
    p, base, routes = native_before_and_after()
    index = build_plan_index(p, routes)
    assert all(index.task_plans[t].leg_ids == () for t in PINS)
    # Native 9 is completed before either pickup; native 10 is completed after
    # both deliveries. Only the 0 -> 10 segment carries both pinned parcels.
    assert corrected._shared(p, routes, PINS, index) == 10.
    old, new = snapshot(frozen, p, base, routes), snapshot(corrected, p, base, routes)
    assert old['current_shared_loaded_distance_km'] == 32.
    assert new['current_shared_loaded_distance_km'] == 10.
    assert new['current_handoff'] is None
    assert new['current_source_drop_availability_min'] == {}
    assert old | {'current_shared_loaded_distance_km': 10.} == new


def test_same_owner_direct_pair_without_overlap_has_zero_shared_distance():
    p, base, routes = native_before_and_after(simultaneous=False)
    index = build_plan_index(p, routes)
    assert corrected._shared(p, routes, PINS, index) == 0.
    assert frozen._shared(p, routes, PINS, index) == 52.
    assert snapshot(corrected, p, base, routes)['current_shared_loaded_distance_km'] == 0.


def test_different_owner_direct_pair_still_has_zero_shared_distance():
    p, base, _ = fixture(direct=True)
    index = build_plan_index(p, base)
    assert corrected._shared(p, base, PINS, index) == frozen._shared(p, base, PINS, index) == 0.


def test_relay_out_leg_identity_and_positive_joint_shape_are_unchanged():
    p, base, routes = fixture()
    index = build_plan_index(p, routes)
    assert corrected._shared(p, routes, PINS, index) == frozen._shared(p, routes, PINS, index) == 5.
    assert corrected._receiver_shape(p, routes, PINS) == frozen._receiver_shape(p, routes, PINS) == 2
    assert snapshot(corrected, p, base, routes) == snapshot(frozen, p, base, routes)
    split = (routes[0], routes[1], (102, 9, -9, 104, -102, -104, 10, -10), *routes[3:])
    for module in (frozen, corrected):
        with pytest.raises(ValueError, match='take pair split'):
            module._receiver_shape(p, split, PINS)


def search_trace(result):
    """Physical search evidence, excluding timestamps and auxiliary snapshots."""
    m = result['metadata']
    return dict(routes=result['routes'], score=result['evaluation'].score, iterations=result['iterations'],
        key=m['final_key'], partial_evaluations=m['global_partial_evaluations'],
        global_evaluations=m['global_incremental_evaluations'], expansions=m['joint_pair_expansions'],
        valid_candidates=m['complete_valid_candidates'], rejected=m['rejected_complete_leaves'],
        rounds=[dict(removed=r['closure']['removed_task_ids'], slots=r['quota_slots_by_aircraft'],
            assignments=r['owner_assignments'],
            layers=[dict(present=l['present_task_ids'],
                states=[{k: state[k] for k in ('routes', 'score', 'key', 'remaining_quota_slots', 'assignment_index',
                    'event_times_min', 'delivery_times_min')} for state in l['beam_state_snapshots']]) for l in r['layers']],
            complete=[dict(routes=c['full_snapshot']['routes'], score=c['full_snapshot']['score'],
                key=c['full_snapshot']['key'], assignment=c['assignment_index']) for c in r['complete_checkpoints']])
            for r in m['closure_records']])


@pytest.mark.parametrize('direct', [False, True])
def test_v1_v2_have_identical_real_joint_search_keys_routes_and_shape(direct):
    p, base, routes = fixture(direct=direct)
    results = [module.repair_damage_closure(p, base, routes,
        original_macro_move=move(), relay_enabled=not direct, deadline=perf_counter()+3.,
        weights=W, scales=S, closure_limit=8, position_limit=8, max_rounds=1)
        for module in (frozen, corrected)]
    assert search_trace(results[0]) == search_trace(results[1])
    for result in results:
        assert result['metadata']['complete_valid_candidates'] > 0
        assert result['metadata']['returned_checkpoint_checked_within_deadline']
    cross = frozenset() if direct else PINS
    for result in results:
        corrected._validate(p, result['routes'], PINS, corrected._signature(p, routes, PINS), cross)


@pytest.mark.parametrize('arm', [False, True])
def test_existing_wrapper_with_v2_alias_has_same_guard_and_independent_current_metrics(monkeypatch, arm):
    from algorithm.tests.test_window_damage_repair import run, audit
    from algorithm.tests.test_deadline_window_repooling import fixture as window_fixture
    p, base = window_fixture()
    original = run(p, base, relay_enabled=arm)
    # Runtime-only alias for this tiny integration; frozen wrapper/source files
    # are unchanged, and pytest restores the module reference afterwards.
    monkeypatch.setattr(wrapper, 'repair', corrected)
    new = run(p, base, relay_enabled=arm)
    assert original['routes'] == new['routes']
    assert original['evaluation'].score == new['evaluation'].score
    assert original['metadata']['final_key'] == new['metadata']['final_key']
    assert original['metadata']['selector_sha256'] == new['metadata']['selector_sha256']
    assert len(original['metadata']['polish_records']) == len(new['metadata']['polish_records']) > 0
    assert all(r['helper_metadata']['complete_valid_candidates'] > 0 for r in new['metadata']['polish_records'])
    audit(p, base, new, arm)
