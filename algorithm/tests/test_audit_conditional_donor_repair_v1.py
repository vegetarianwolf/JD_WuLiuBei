"""Complete conditional swaps: independent DAG, quota and provenance proofs."""
from copy import deepcopy

import pytest

from algorithm.experiments import audit_conditional_donor_repair_v1 as audit
from algorithm.tests.test_audit_critical_prefix_joint_repair_v1 import fixture, W, S
from algorithm.tests.test_audit_critical_prefix_joint_repair_v1 import problem
from time import perf_counter


def example(relay=True):
    data = fixture();parent = data[3] if relay else data[2]
    # Whole jobs3/6 exchange responsibility; every other signed event is kept.
    removed = [tuple(v for v in r if abs(v) not in (3, 6)) for r in parent]
    child = (removed[0] + (6, -6), removed[1] + (3, -3), removed[2])
    record = dict(t=3, d=6, a=0, b=1, partial_routes=list(map(list, removed)), quota_slots_by_aircraft=[1, 1, 0],
        inserted_task_order=[3, 6], owner_choices=[1, 0], insertions=[
            dict(task_id=3, owner=1, pickup_gap=len(removed[1]), delivery_gap=len(removed[1])),
            dict(task_id=6, owner=0, pickup_gap=len(removed[0]), delivery_gap=len(removed[0]))])
    return data, parent, child, record


def prove(data, parent, child, record, relay=True, telemetry=None):
    return audit.audit_step(*data[:3], data[3] if relay else data[2], parent, child, data[4] if relay else None,
        relay_enabled=relay, step_record=record, telemetry=telemetry, focus_task_ids=(1, 2), weights=W, scales=S)


def snapshot(data, parent, child, record, relay=True):
    p = prove(data, parent, child, record, relay)
    fields = ('pinned_task_ids', 'focus_task_ids', 'actual_final_owners', 'event_times_min', 'delivery_times_min',
        'post_global_score', 'current_source_drop_availability_min', 'current_shared_loaded_distance_km',
        'current_handoff', 'current_classifications', 'changed_owner_task_ids', 'changed_event_sequence_aircraft')
    saved = {f: deepcopy(p[f]) for f in fields}
    saved.update(routes=list(map(list, child)), routes_sha256=audit._digest(child), parent_routes_sha256=audit._digest(parent),
        valid=True, complete=True, pin_scope=audit.PIN_SCOPE, original_seed_move=data[4] if relay else None,
        score=p['score'], key=p['key'], cross_uav_count=p['actual_cross_uav_count'], relay_task_count=len(p['handoffs']),
        full_verified_at_perf_counter=1., deadline_perf_counter=2., inserted_task_order=record['inserted_task_order'],
        owner_choices=record['owner_choices'], insertions=record['insertions'], present_task_ids=sorted(data[0]),
        remaining_quota_slots=[0] * len(child), partial_quota_slots=record['quota_slots_by_aircraft'])
    return saved


@pytest.mark.parametrize('relay', [False, True])
def test_real_complete_conditional_swap_restores_exact_slots_and_all_current_clocks(relay):
    data, parent, child, record = example(relay)
    saved = snapshot(data, parent, child, record, relay)
    p = prove(data, parent, child, record, relay, saved)
    assert p['joint_full_coverage'] and p['quota_slots_by_aircraft'] == [1, 1, 0]
    assert p['actual_final_owners']['3'] == 1 and p['actual_final_owners']['6'] == 0
    assert p['transition_proof']['remaining_quota_slots'] == [0, 0, 0]
    assert p['pinned_task_ids'] == ([1, 2] if relay else [])
    assert p['actual_cross_uav_count'] == (2 if relay else 0)


def test_both_insertion_orders_reconstruct_real_intermediate_routes_without_a_delta_sum():
    data, parent, child, record = example()
    first = prove(data, parent, child, record)
    record['inserted_task_order'].reverse();record['owner_choices'].reverse();record['insertions'].reverse()
    second = prove(data, parent, child, record)
    assert first['score'] == second['score']
    assert first['transition_proof']['transition']['intermediate_scores'][0] != second['transition_proof']['transition']['intermediate_scores'][0]


@pytest.mark.parametrize('field,value', [('a', 1), ('b', 2), ('d', 3), ('quota_slots_by_aircraft', [0, 2, 0]),
    ('owner_choices', [0, 1]), ('inserted_task_order', [3])])
def test_false_current_owner_donor_slot_or_unpaid_second_job_is_rejected(field, value):
    data, parent, child, record = example();record[field] = value
    with pytest.raises(ValueError): prove(data, parent, child, record)


def test_unselected_native_moves_even_with_balanced_quotas_are_rejected():
    data, parent, child, record = example()
    # A separate native5/8 exchange keeps quotas but is outside this two-job step.
    child = tuple(tuple(8 if v == 5 else -8 if v == -5 else 5 if v == 8 else -5 if v == -8 else v for v in r) for r in child)
    with pytest.raises(ValueError, match='unselected'): prove(data, parent, child, record)


@pytest.mark.parametrize('field', ['event_times_min', 'current_handoff', 'routes_sha256', 'parent_routes_sha256',
                                  'full_verified_at_perf_counter', 'table_score'])
def test_false_full_clock_cache_hash_old_price_or_overdue_checkpoint_fails(field):
    data, parent, child, record = example();saved = snapshot(data, parent, child, record)
    if field == 'event_times_min': saved[field][1][0] += 1.
    elif field == 'current_handoff': saved[field]['second_take_min'] += 1.
    elif field in ('routes_sha256', 'parent_routes_sha256'): saved[field] = '0' * 64
    elif field == 'full_verified_at_perf_counter': saved[field] = 2.
    else: saved[field] = saved['score']
    with pytest.raises(ValueError): prove(data, parent, child, record, telemetry=saved)


def test_no_fake_raw_source_or_changed_pinned_transfer_roles():
    data, parent, child, record = example()
    wrong = deepcopy(data[4]);wrong['family']['a'], wrong['family']['b'] = wrong['family']['b'], wrong['family']['a']
    with pytest.raises(ValueError):
        audit.audit_step(*data[:3], data[3], parent, child, wrong, relay_enabled=True, step_record=record)
    bad = list(child);bad[0], bad[1] = bad[1], bad[0]
    with pytest.raises(ValueError, match='pinned'): prove(data, parent, tuple(bad), record)


def test_DIRECT_has_no_implicit_focus_pin_and_can_move_original_pooled_whole_task():
    data = fixture();parent = data[2]
    removed = [tuple(v for v in r if abs(v) not in (1, 3)) for r in parent]
    child = (removed[0] + (1, -1), removed[1], removed[2] + (3, -3))
    record = dict(t=1, d=3, a=2, b=0, inserted_task_order=[1, 3], owner_choices=[0, 2], insertions=[
        dict(task_id=1, owner=0, pickup_gap=4, delivery_gap=4), dict(task_id=3, owner=2, pickup_gap=4, delivery_gap=4)])
    p = prove(data, parent, child, record, False)
    assert p['pinned_task_ids'] == [] and p['actual_final_owners']['1'] == 0


def test_missing_initial_checkpoint_cannot_be_fabricated_by_postdeadline_telemetry():
    data, parent, child, record = example();saved = snapshot(data, parent, child, record)
    saved.pop('full_verified_at_perf_counter')
    with pytest.raises(ValueError, match='deadline'):
        audit.audit_checkpoint(*data[:3], data[3], child, data[4], relay_enabled=True,
            telemetry=saved, focus_task_ids=(1, 2), weights=W, scales=S)


def actual_metadata(relay=True):
    from algorithm.experiments.conditional_donor_repair_v1 import refine
    data = fixture();deadline = perf_counter() + 3.
    result = refine(problem(data), data[2], data[3] if relay else data[2], seed_move=data[4] if relay else None,
        focus_task_ids=(1, 2), relay_enabled=relay, weights=W, scales=S, deadline=deadline,
        critical_limit=2, donor_limit=2, position_limit=2, option_count=1, beam_width=2, max_steps=1, snapshot_limit=1)
    return data, result['metadata']


def metadata_proof(data, saved, relay=True):
    return audit.audit_metadata(*data[:3], data[3] if relay else data[2], saved,
        seed_move=data[4] if relay else None, focus_task_ids=(1, 2), relay_enabled=relay,
        weights=W, scales=S, deadline=saved['deadline_perf_counter'])


@pytest.mark.parametrize('relay', [False, True])
def test_real_small_helper_catalogues_complete_beam_checkpoints_and_best_chain_cross_audit(relay):
    data, saved = actual_metadata(relay)
    p = metadata_proof(data, saved, relay)
    assert p['predeadline_checkpoint_verified'] and p['saved_complete_states_replayed'] >= 2
    assert p['complete_transition_edges_replayed'] >= 1 and p['catalogue_references']
    assert not p['all_complete_probes_entity_replayed']
    assert saved['complete_leafs_full_verified'] >= p['complete_transition_edges_replayed']


@pytest.mark.parametrize('fault', ['proxy', 'direction', 'coverage', 'best_chain'])
def test_forged_conditional_surface_direction_coverage_or_returned_best_is_rejected(fault):
    data, saved = actual_metadata()
    if fault == 'proxy':
        saved['steps'][0]['parent_records'][0]['catalogue']['donor_directories'][0]['per_donor_evidence'][0]['geometric_net_distance_proxy_km'] += 1.
    elif fault == 'direction':
        saved['steps'][0]['parent_records'][0]['catalogue']['critical_direction_representatives'][0]['signed_gain_min'] += 1.
    elif fault == 'coverage':
        saved['steps'][0]['parent_records'][0]['catalogue']['donor_directories'].pop()
    else:
        saved['best_key'][0] -= 1.
    with pytest.raises(ValueError): metadata_proof(data, saved)


def test_expired_initialization_no_complete_proof_or_adoption_is_reported():
    from algorithm.experiments.conditional_donor_repair_v1 import refine
    data = fixture();deadline = perf_counter() - 1.
    result = refine(problem(data), data[2], data[2], relay_enabled=False, deadline=deadline, weights=W, scales=S, focus_task_ids=(1, 2))
    assert result['evaluation'] is None
    p = metadata_proof(data, result['metadata'], False)
    assert not p['predeadline_checkpoint_verified'] and not p['initialization_completed']
    assert p['saved_complete_states_replayed'] == 0
    result['metadata']['returned_checkpoint_checked_within_deadline'] = True
    with pytest.raises(ValueError): metadata_proof(data, result['metadata'], False)
