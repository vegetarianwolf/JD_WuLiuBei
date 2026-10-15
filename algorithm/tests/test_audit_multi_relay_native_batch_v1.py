"""Necessary callback proof; use the actual tiny paid wrapper, no new ledger."""
from copy import deepcopy
from time import perf_counter

import pytest

from algorithm.experiments import audit_multi_relay_native_batch_v1 as audit
from algorithm.tests.test_multi_relay_native_refinement_v1 import fixture, run, W, S
from algorithm.tests.test_relay_preserving_polish import real_weighted_runtime
from algorithm.tests.test_relay_repooling_exchange import topology

_PAID_CASE = None
PARAMS = dict(raw_fraction=.4, max_groups=2, beam_width=2, max_families=2, source_option_limit=1,
    receiver_option_limit=1, position_limit=3, snapshot_limit=3, polish_max_iterations=2,
    polish_initial_temperature=.0025, polish_minimum_temperature=.00001, polish_cooling_rate=.97)


@pytest.fixture
def paid_case(real_weighted_runtime):
    global _PAID_CASE
    if _PAID_CASE is None:
        p, base = fixture();result = run(real_weighted_runtime, p, base)
        tasks = {t.id: ((t.pickup.x, t.pickup.y), (t.delivery.x, t.delivery.y), t.deadline_min) for t in p.tasks}
        graph = topology(p);graph['task_relay_candidates'] = {str(t): list(p.task_relay_candidates[t]) for t in p.task_ids}
        manifest = dict(weights=list(W), scales=list(S), accumulation_fixed_catalogue_parameters=audit.frozen.FIXED_CATALOGUE,
            method_parameters={'multi_native_relay': PARAMS})
        # Keep only the JSON-shaped saved evidence; the runtime evaluation owns
        # immutable mapping proxies and is deliberately outside this proof.
        saved_result = {'routes': result['routes'], 'metadata': result['metadata']}
        _PAID_CASE = (tasks, graph, base, saved_result, manifest)
    return deepcopy(_PAID_CASE)


def prove(case):
    tasks, graph, base, result, manifest = case
    return audit._audit_metadata(tasks, graph, base, result['metadata'], relay=True, manifest=manifest)


def test_actual_paid_raw_channels_native_post_and_original_guard(paid_case):
    proof = prove(paid_case);meta = paid_case[3]['metadata']
    assert proof['passed'] and proof['raw_method_evidence']['predeadline_checkpoint_verified']
    assert len(proof['native_leaf_evidence']) == len(meta['polish_records']) == 2
    assert [s['group_count'] for s in meta['seed_records']] == [1, 2]
    assert len(proof['proposal_references']) == 4
    assert proof['final_routes_sha256'] == audit._digest(paid_case[3]['routes'])
    assert proof['checkpoint_mechanism_diagnostic_only'] and not proof['strongest_fresh_comparison']
    assert proof['no_shared_raw_directory'] and not proof['unrecorded_probes_entity_replayed']
    assert not hasattr(audit, 'audit')  # The established ledger owns the only whole-batch loop.


@pytest.mark.parametrize('fault', ['paid_raw_deadline', 'channel_source', 'missing_channel', 'raw_digest',
    'leaf_deadline', 'profile', 'late_post', 'final_guard'])
def test_paid_source_current_channels_leaf_profile_and_strict_guard_cannot_drift(paid_case, fault):
    meta = paid_case[3]['metadata']
    if fault == 'paid_raw_deadline': meta['raw_metadata']['deadline_perf_counter'] += 1.
    elif fault == 'channel_source': meta['seed_records'][0]['source_path'] = ['input_snapshot']
    elif fault == 'missing_channel': meta['seed_records'].pop()
    elif fault == 'raw_digest': meta['seed_records'][0]['raw_snapshot_sha256'] = '0' * 64
    elif fault == 'leaf_deadline': meta['polish_records'][0]['deadline_perf_counter'] += 1.
    elif fault == 'profile': meta['polish_records'][0]['metadata']['configuration']['initial_temperature'] *= 2
    elif fault == 'late_post': meta['full_candidate_snapshots'][-1]['full_verified_at_perf_counter'] = meta['deadline_perf_counter']
    else: meta['final_key'][0] -= .01
    with pytest.raises((AssertionError, ValueError)): prove(paid_case)


def test_expired_entry_returns_only_true_original_guard_without_raw_or_leaf(real_weighted_runtime):
    p, base = fixture();result = run(real_weighted_runtime, p, base, deadline=perf_counter()-1.)
    tasks = {t.id: ((t.pickup.x, t.pickup.y), (t.delivery.x, t.delivery.y), t.deadline_min) for t in p.tasks}
    manifest = dict(weights=list(W), scales=list(S), method_parameters={'multi_native_relay': PARAMS})
    proof = audit._audit_metadata(tasks, topology(p), base, result['metadata'], relay=True, manifest=manifest)
    assert proof['passed'] and not proof['initialization_completed']
    assert proof['no_new_predeadline_checkpoint'] and proof['final_routes_sha256'] == audit._digest(base)


def test_direct_callback_routes_only_own_fixed_parameters_to_frozen_public_proof(paid_case, monkeypatch):
    tasks, graph, base, _, _ = paid_case;called = []
    params = dict(alns_fraction=.65, critical_limit=6, donor_limit=3, position_limit=12,
        option_count=3, beam_width=4, max_steps=3, snapshot_limit=6)
    def public(*args, **kwargs):
        called.append((args, kwargs));return {'passed': True}
    monkeypatch.setattr(audit.direct_audit, 'audit_metadata', public)
    meta = dict(deadline_perf_counter=1., final_snapshot={'routes': base})
    manifest = dict(weights=list(W), scales=list(S), periodic_parameters=audit.frozen.PERIODIC,
        method_parameters={'multi_native_direct': params})
    result = audit._audit_metadata(tasks, graph, base, meta, relay=False, manifest=manifest)
    assert result['passed'] and len(called) == 1 and called[0][1]['parameters'] == params
    assert result['no_shared_raw_directory']
