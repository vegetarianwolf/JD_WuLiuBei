"""Native-priced directory wiring, real complete plans and unchanged guards."""
from copy import deepcopy
from time import perf_counter

import pytest
from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator
from uav_dispatch.validation import evaluate_solution
from uav_dispatch.relay import build_plan_index
from algorithm.experiments import split_feeder_refinement_v4 as method
from algorithm.tests.test_split_feeder_refinement import fixture, W, S
from algorithm.tests.test_relay_repooling_exchange import topology


def carried_fixture():
    """Both feeders can carry the new cargo around their native square for free."""
    left, right, H = Point(0., 0.), Point(20., 0.), Point(10., 0.)
    tasks = (Task(1, left, Point(11., 0.), 33.), Task(2, right, Point(12., 0.), 33.),
        Task(3, left, Point(0., 10.), 10.), Task(4, Point(10., 10.), H, 30.),
        Task(5, right, Point(20., 10.), 10.), Task(6, Point(10., 10.), H, 30.))
    legs = {101: TransportLeg(101, 1, 'RELAY_IN', 1), 102: TransportLeg(102, 1, 'RELAY_OUT', 1),
        103: TransportLeg(103, 2, 'RELAY_IN', 1), 104: TransportLeg(104, 2, 'RELAY_OUT', 1)}
    p = Problem(tasks, drone_count=3, capacity=2, max_tasks_per_drone=2, speed_km_per_min=1.,
        drone_homes=(left, right, H), relay_stations=(RelayStation(1, H.x, H.y),),
        leg_registry=legs, task_relay_candidates={1: (1,), 2: (1,)})
    base = ((3, -3, 4, -4), (5, -5, 6, -6), (1, 2, -1, -2))
    assert evaluate_solution(p, base, _force_full=True).valid
    return p, base


def separated_fixture():
    """Only a receiver doing native work between selected deliveries reaches N0."""
    H = Point(0., 0.)
    tasks = [Task(1, H, Point(1., 0.), 1.), Task(2, H, Point(11., 0.), 11.)]
    tasks += [Task(t, H, H, 100.) for t in range(3, 9)]
    tasks += [Task(9, Point(1., 0.), Point(10., 0.), 10.)]
    legs = {101: TransportLeg(101, 1, 'RELAY_IN', 1), 102: TransportLeg(102, 1, 'RELAY_OUT', 1),
        103: TransportLeg(103, 2, 'RELAY_IN', 1), 104: TransportLeg(104, 2, 'RELAY_OUT', 1)}
    p = Problem(tuple(tasks), drone_count=3, capacity=2, max_tasks_per_drone=3, speed_km_per_min=1.,
        drone_homes=(H, H, H), relay_stations=(RelayStation(1, 0., 0.),), leg_registry=legs,
        task_relay_candidates={1: (1,), 2: (1,)})
    base = ((3, -3, 4, -4, 5, -5), (6, -6, 7, -7, 8, -8), (9, -9, 1, 2, -1, -2))
    assert evaluate_solution(p, base, _force_full=True).valid
    return p, base


def run(p, base, **kwargs):
    return method.refine(p, base, deadline=perf_counter()+4., weights=W, scales=S,
        max_families=2, base_pool_limit=8, **kwargs)


def audit(p, base, result, relay_enabled):
    from algorithm.experiments.audit_split_feeder_v4 import audit_split_feeder_family, audit_split_feeder_proposal
    tasks = {t.id: ((t.pickup.x, t.pickup.y), (t.delivery.x, t.delivery.y), t.deadline_min) for t in p.tasks}
    graph = topology(p)
    graph['task_relay_candidates'] = {str(t): list(p.task_relay_candidates.get(t, ())) for t in p.task_ids}
    for family in result['metadata']['selected_families']:
        assert audit_split_feeder_family(tasks, graph, base, family, weights=W, scales=S)['passed']
    for snapshot in result['metadata']['full_candidate_snapshots']:
        assert audit_split_feeder_proposal(tasks, graph, base, snapshot['routes'], snapshot['move'],
            relay_enabled=relay_enabled, telemetry=snapshot, weights=W, scales=S)['passed']


def test_v4_real_both_arms_share_native_priced_catalogue_and_independent_strong_control():
    p, base = fixture(); before = dict(p.leg_registry)
    direct, relay = run(p, base, relay_enabled=False), run(p, base, relay_enabled=True)
    dm, rm = direct['metadata'], relay['metadata']
    assert method.catalogue.__name__.endswith('split_feeder_catalogue_v4')
    assert dm['seed'] == rm['seed'] == 2026101519
    assert dm['configuration'] == rm['configuration'] and rm['configuration']['catalogue_version'] == 4
    assert not rm['configuration']['early_relay_price_is_DIRECT_candidate']
    assert dm['selector_completed'] and rm['selector_completed']
    assert dm['selected_families'] == rm['selected_families'] and dm['selector_sha256'] == rm['selector_sha256']
    assert all('early_pricing_evidence' in f for f in rm['selected_families'])
    for record in dm['family_records']:
        assert record['direct_same_owner_pair_completed'] and record['direct_map_enumeration_completed']
        assert len(record['direct_maps_completed']) == 12
        assert record['direct_same_owner_pair_layouts_evaluated'] == 6*record['direct_same_owner_pair_empty_gaps_evaluated']
        assert record['direct_same_owner_flexible_pair_completed']
        assert record['direct_same_owner_flexible_insertion_orders'] == [[record['family']['i'], record['family']['j']], [record['family']['j'], record['family']['i']]]
        assert len(record['direct_donor_pair_flexible_completed']) == 6
        assert all(r['coverage']['flexible_pair_completed'] for r in record['direct_donor_pair_flexible_records'])
    assert direct['evaluation'].valid and relay['evaluation'].valid
    assert direct['evaluation'].score.late_count == relay['evaluation'].score.late_count == 0
    assert direct['evaluation'].score.distance_km == pytest.approx(22.65685424949238)
    assert relay['evaluation'].score.distance_km == 12.
    assert rm['final_cross_uav_count'] == 2 and not build_plan_index(p, direct['routes']).relay_task_ids
    assert build_plan_index(p, relay['routes']).delivery_route_by_task == build_plan_index(p, base).delivery_route_by_task
    assert dict(p.leg_registry) == before and 'problem' not in relay and 'problem' not in direct
    audit(p, base, direct, False); audit(p, base, relay, True)


def test_actual_separated_receiver_witness_and_equally_flexible_direct_are_complete_and_audited():
    p, base = separated_fixture(); before = dict(p.leg_registry)
    direct, relay = run(p, base, relay_enabled=False), run(p, base, relay_enabled=True)
    dm, rm = direct['metadata'], relay['metadata']
    assert dm['selected_families'] == rm['selected_families'] and dm['selector_sha256'] == rm['selector_sha256']
    assert dm['configuration'] == rm['configuration']
    assert dm['configuration']['direct_pair_flexible_native_interleaving']
    assert dm['configuration']['direct_pair_flexible_whole_insertion_orders'] == 2
    assert direct['evaluation'].score.late_count == relay['evaluation'].score.late_count == 0
    assert direct['evaluation'].score.distance_km == relay['evaluation'].score.distance_km == 11.
    assert build_plan_index(p, relay['routes']).delivery_route_by_task == build_plan_index(p, base).delivery_route_by_task
    receiver = relay['routes'][2]
    assert receiver.index(-102) < receiver.index(9) < receiver.index(-9) < receiver.index(-104)
    clock = GlobalRelayEvaluator(p).evaluate(relay['routes']).delivery_times_min
    assert clock[1] == 1. and clock[9] == 10. and clock[2] == 11.
    assert any(s['original_delivery_gap'] > s['original_pickup_gap']
        for f in rm['selected_families'] for s in (f['early_pricing_evidence']['receiver_layout'],))
    assert all(r['early_seed_entered_candidate_pool'] for f in rm['selected_families'] for r in f['seed_retention'])
    assert dict(p.leg_registry) == before and 'problem' not in direct and 'problem' not in relay
    audit(p, base, relay, True); audit(p, base, direct, False)


def test_noncontiguous_early_seeds_survive_into_real_full_witness_with_native_clocks_and_quota():
    p, base = carried_fixture(); direct = run(p, base, relay_enabled=False); relay = run(p, base)
    dm, rm = direct['metadata'], relay['metadata']
    assert dm['selected_families'] == rm['selected_families']
    assert dm['selector_sha256'] == rm['selector_sha256']
    assert rm['configuration']['early_catalogue_native_pricing'] == 'restricted_two_gap_IN_and_separated_OUT_delivery'
    assert relay['evaluation'].valid and relay['evaluation'].score.late_count == 0
    assert relay['evaluation'].score.distance_km == pytest.approx(62.)
    idx = build_plan_index(p, relay['routes'])
    assert idx.delivery_route_by_task == build_plan_index(p, base).delivery_route_by_task
    assert set(idx.relay_task_ids) == {1, 2}
    assert {idx.inbound_route_by_task[t] for t in (1, 2)} == {0, 1}
    assert all(sum(v == a for v in idx.delivery_route_by_task.values()) == 2 for a in range(3))
    actual = GlobalRelayEvaluator(p).evaluate(relay['routes'])
    assert actual.delivery_times_min[3] == actual.delivery_times_min[5] == 10.
    assert actual.delivery_times_min[4] == actual.delivery_times_min[6] == 30.
    assert actual.delivery_times_min[1] == 31. and actual.delivery_times_min[2] == 32.
    saved_routes = {tuple(tuple(r) for r in s['routes']) for s in rm['full_candidate_snapshots']}
    family = next(f for f in rm['selected_families'] if (f['a'], f['b']) == (0, 1))
    seed = family['early_pricing_evidence']
    assert all(s['original_pickup_gap'] < s['original_drop_gap'] for s in seed['source_layouts'])
    # H equals the last native delivery, so dropping immediately before or
    # after that zero-distance native service are equally valid optima.
    assert all(s['gap'] == s['original_pickup_gap'] and s['drop_gap'] == s['original_drop_gap'] for s in seed['source_layouts'])
    assert all(s['pickup_detour_distance_km'] == s['drop_detour_distance_km'] == 0. for s in seed['source_layouts'])
    assert tuple(tuple(r) for r in relay['routes']) in saved_routes
    for task, a in ((1, 0), (2, 1)):
        incoming = 101 if task == 1 else 103
        route = relay['routes'][a]
        assert route.index(incoming)+1 < route.index(-incoming)
    assert all(r['early_seed_entered_candidate_pool'] for r in family['seed_retention'])
    assert min(w['key'][0] for w in family['relay_witnesses']) <= seed['key'][0]+1e-12
    for task, a in ((1, family['a']), (2, family['b'])):
        records = family['source_tables'][f"{task}:{a}:1"]
        assert len(records) <= 3 and any(r['route'] == seed['priced_seed_routes'][a] for r in records)
    assert all(r['direct_same_owner_pair_completed'] and len(r['direct_maps_completed']) == 12 for r in dm['family_records'])
    audit(p, base, relay, True); audit(p, base, direct, False)


def test_complete_seed_layout_is_replayed_instead_of_regenerated_from_a_weaker_source_shortlist(monkeypatch):
    p, base = fixture()
    selection = method.catalogue.build_catalogue(p, base, deadline=perf_counter()+3., weights=W, scales=S,
        seed=2026101519, max_families=1, base_pool_limit=8)
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
        seed=2026101519, max_families=1, base_pool_limit=8)
    selection['families'][0]['relay_witnesses'][0]['table_score']['distance_km'] -= 1.
    selection['selector_sha256'] = method._digest(selection['families'])
    monkeypatch.setattr(method.catalogue, 'build_catalogue', lambda *a, **k: deepcopy(selection))
    with pytest.raises(ValueError, match='witness full score differs'):
        run(p, base)


def test_v4_complete_snapshot_deadline_and_expired_input_keep_original_guard(monkeypatch):
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
