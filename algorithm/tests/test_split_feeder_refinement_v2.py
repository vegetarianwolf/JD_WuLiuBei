"""Native-priced directory wiring, real complete plans and unchanged guards."""
from copy import deepcopy
from time import perf_counter

import pytest
from uav_dispatch.relay import build_plan_index
from algorithm.experiments import split_feeder_refinement_v2 as method
from algorithm.tests.test_split_feeder_refinement import fixture, W, S
from algorithm.tests.test_relay_repooling_exchange import topology


def run(p, base, **kwargs):
    return method.refine(p, base, deadline=perf_counter()+4., weights=W, scales=S,
        max_families=2, base_pool_limit=8, **kwargs)


def audit(p, base, result, relay_enabled):
    from algorithm.experiments.audit_split_feeder_v2 import audit_split_feeder_family, audit_split_feeder_proposal
    tasks = {t.id: ((t.pickup.x, t.pickup.y), (t.delivery.x, t.delivery.y), t.deadline_min) for t in p.tasks}
    graph = topology(p)
    graph['task_relay_candidates'] = {str(t): list(p.task_relay_candidates.get(t, ())) for t in p.task_ids}
    for family in result['metadata']['selected_families']:
        assert audit_split_feeder_family(tasks, graph, base, family, weights=W, scales=S)['passed']
    for snapshot in result['metadata']['full_candidate_snapshots']:
        assert audit_split_feeder_proposal(tasks, graph, base, snapshot['routes'], snapshot['move'],
            relay_enabled=relay_enabled, telemetry=snapshot, weights=W, scales=S)['passed']


def test_v2_real_both_arms_share_native_priced_catalogue_and_independent_strong_control():
    p, base = fixture(); before = dict(p.leg_registry)
    direct, relay = run(p, base, relay_enabled=False), run(p, base, relay_enabled=True)
    dm, rm = direct['metadata'], relay['metadata']
    assert method.catalogue.__name__.endswith('split_feeder_catalogue_v2')
    assert dm['seed'] == rm['seed'] == 2026101517
    assert dm['configuration'] == rm['configuration'] and rm['configuration']['catalogue_version'] == 2
    assert not rm['configuration']['early_relay_price_is_DIRECT_candidate']
    assert dm['selector_completed'] and rm['selector_completed']
    assert dm['selected_families'] == rm['selected_families'] and dm['selector_sha256'] == rm['selector_sha256']
    assert all('early_pricing_evidence' in f for f in rm['selected_families'])
    for record in dm['family_records']:
        assert record['direct_same_owner_pair_completed'] and record['direct_map_enumeration_completed']
        assert len(record['direct_maps_completed']) == 12
        assert record['direct_same_owner_pair_layouts_evaluated'] == 6*record['direct_same_owner_pair_empty_gaps_evaluated']
    assert direct['evaluation'].valid and relay['evaluation'].valid
    assert direct['evaluation'].score.late_count == relay['evaluation'].score.late_count == 0
    assert direct['evaluation'].score.distance_km == pytest.approx(22.65685424949238)
    assert relay['evaluation'].score.distance_km == 12.
    assert rm['final_cross_uav_count'] == 2 and not build_plan_index(p, direct['routes']).relay_task_ids
    assert build_plan_index(p, relay['routes']).delivery_route_by_task == build_plan_index(p, base).delivery_route_by_task
    assert dict(p.leg_registry) == before and 'problem' not in relay and 'problem' not in direct
    audit(p, base, direct, False); audit(p, base, relay, True)


def test_complete_seed_layout_is_replayed_instead_of_regenerated_from_a_weaker_source_shortlist(monkeypatch):
    p, base = fixture()
    selection = method.catalogue.build_catalogue(p, base, deadline=perf_counter()+3., weights=W, scales=S,
        seed=2026101517, max_families=1, base_pool_limit=8)
    expected = {tuple(tuple(r) for r in w['routes']) for w in selection['families'][0]['relay_witnesses']}
    monkeypatch.setattr(method.catalogue, 'build_catalogue', lambda *a, **k: deepcopy(selection))
    def forbidden(*args, **kwargs):
        raise AssertionError('A saved complete witness must not be replaced by source re-enumeration')
    monkeypatch.setattr(method.costs, 'source_options', forbidden)
    result = run(p, base)
    saved = {tuple(tuple(r) for r in s['routes']) for s in result['metadata']['full_candidate_snapshots']}
    assert expected == saved and saved
    for snapshot in result['metadata']['full_candidate_snapshots']:
        assert snapshot['full_verified_at_perf_counter'] < snapshot['deadline_perf_counter']
    audit(p, base, result, True)


def test_each_early_complete_seed_has_real_source_slots_and_nonworse_full_channel_representatives():
    p, base = fixture(); result = run(p, base)
    for family in result['metadata']['selected_families']:
        witnesses = family['relay_witnesses']
        assert 1 <= len(witnesses) <= 3
        for field in ('early_pricing_evidence', 'early_rescue_pricing_evidence'):
            early = family.get(field)
            if early is None:
                continue
            routes = early['priced_seed_routes']
            for task, aircraft in ((family['i'], family['a']), (family['j'], family['b'])):
                records = family['source_tables'][f"{task}:{aircraft}:{family['station_id']}"]
                assert 1 <= len(records) <= 3
                assert any(r['route'] == routes[aircraft] for r in records)
            # Exact early layouts enter the complete pool; a different final
            # representative is allowed when its real channel is no worse.
            assert min(w['key'][0] for w in witnesses) <= early['key'][0]+1e-12
            assert min(w['key'][1] for w in witnesses) <= early['full_score']['late_count']
            assert max(len(w['pooled_rescued_task_ids']) for w in witnesses) >= len(early['pooled_rescued_task_ids'])


def test_pricing_witness_cannot_replace_complete_global_score_or_clocks(monkeypatch):
    p, base = fixture()
    selection = method.catalogue.build_catalogue(p, base, deadline=perf_counter()+3., weights=W, scales=S,
        seed=2026101517, max_families=1, base_pool_limit=8)
    selection['families'][0]['relay_witnesses'][0]['table_score']['distance_km'] -= 1.
    selection['selector_sha256'] = method._digest(selection['families'])
    monkeypatch.setattr(method.catalogue, 'build_catalogue', lambda *a, **k: deepcopy(selection))
    with pytest.raises(ValueError, match='witness full score differs'):
        run(p, base)


def test_v2_complete_snapshot_deadline_and_expired_input_keep_original_guard(monkeypatch):
    p, base = fixture(); clock = [perf_counter()]; deadline = clock[0]+4.; original = method._snapshot
    monkeypatch.setattr(method, 'perf_counter', lambda: clock[0])
    def crossing(*args, **kwargs):
        value = original(*args, **kwargs)
        if kwargs['phase'] == 'full_proposal':
            clock[0] = deadline+.1
        return value
    monkeypatch.setattr(method, '_snapshot', crossing)
    result = method.refine(p, base, deadline=deadline, weights=W, scales=S, max_families=1, base_pool_limit=8)
    assert result['routes'] == base and result['evaluation'].valid and result['metadata']['deadline_reached']
    assert not result['metadata']['accepted_moves'] and not result['metadata']['full_candidate_snapshots']
    expired = method.refine(p, base, deadline=clock[0]-1., weights=W, scales=S)
    assert expired['routes'] == base and not expired['metadata']['selector_completed']
