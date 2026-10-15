"""Synthetic joint-source proxy/cache and real complete-structure checks.

No official routes, optimizer runs, or budget probes are used here.
"""
from dataclasses import replace
from itertools import permutations
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import joint_source_relay_structure_v1 as m
from algorithm.tests.test_multi_relay_structure_v1 import fixture as split_fixture
from algorithm.tests.test_relay_repooling_exchange import topology

W, S = (.5, .25, .25), (6., 100., 100.)


def fixture():
    P = Point
    tasks = (Task(1, P(-2., 1.), P(6., 2.), 100.),
        Task(2, P(2., 0.), P(7., 2.), 100.),
        Task(3, P(0., 2.), P(2., 2.), 100.),
        Task(4, P(1., 2.), P(3., 2.), 100.),
        Task(5, P(3., 1.), P(4., 1.), 100.),
        Task(6, P(-3., -1.), P(-2., -1.), 100.))
    legs = {101+2*(t.id-1)+k: TransportLeg(101+2*(t.id-1)+k, t.id,
        'RELAY_IN' if k == 0 else 'RELAY_OUT', 1) for t in tasks for k in (0, 1)}
    p = Problem(tasks, drone_count=3, max_tasks_per_drone=3, capacity=2,
        speed_km_per_min=.9, drone_homes=(P(-1., 0.), P(-3., -1.), P(5., 1.)),
        relay_stations=(RelayStation(1, 5., 1.),), leg_registry=legs,
        task_relay_candidates={t.id: (1,) for t in tasks})
    base = ((3, 4, -3, -4, 5, -5), (6, -6), (1, 2, -1, -2))
    assert evaluate_solution(p, base, _force_full=True).valid
    return p, base


def surface(p, routes, a=0, groups=()):
    schedule = GlobalRelayEvaluator(p).evaluate(routes)
    assert schedule.feasible
    return m._surface(p, routes[a], a, groups, routes, schedule, perf_counter()+2.)


def walk(p, route, aircraft):
    previous, held, distance, times = p.home_points()[aircraft], set(), 0., []
    for visit in route:
        point = p.point_for_visit(visit)
        distance += previous.distance_to(point)
        times.append(distance/p.speed_km_per_min)
        if visit > 0:
            assert visit not in held
            held.add(visit)
            assert len(held) <= 2
        else:
            assert -visit in held
            held.remove(-visit)
        previous = point
    assert not held
    return distance, times


def brute(p, source, *, banned=()):
    """Independent all-gap construction and flight walk, not row formulas."""
    route, records = source['route'], []
    initial_distance, _ = walk(p, route, 0)
    for order in permutations((1, 2)):
        leg = {1: 101, 2: 103}
        for g in range(len(route)+1):
            for h in range(g, len(route)+1):
                if g in banned or h in banned:
                    continue
                # Empty second pickup, at most one original parcel throughout.
                if source['loads'][h] or any(n > 1 for n in source['loads'][g:h+1]):
                    continue
                shared = p.task(order[1]).pickup.distance_to(p.relay_stations[0].point)
                if shared <= 1e-10:
                    continue
                child = (route[:g]+(leg[order[0]],)+route[g:h]
                    +(leg[order[1]], -101, -103)+route[h:])
                distance, clocks = walk(p, child, 0)
                records.append(dict(order=order, g=g, h=h, route=child,
                    delta=distance-initial_distance, drop=clocks[h+2]))
    return records


def test_cached_profile_exact_geo_and_drop_heads_match_independent_full_brute():
    p, base = fixture(); sf = surface(p, base); deadline = perf_counter()+2.
    cache = {}; stats = dict(source_first_pickup_tables_built=0,
        source_first_pickup_table_hits=0, source_profile_positions=0, source_profile_summary_rows=0)
    fast = m._joint_rows(p, sf, (1, 2), 0, 1, [101, 103], deadline,
        cache, profile=True, statistics=stats)
    reference = brute(p, sf)
    assert {tuple(r['pickup_order_task_ids']) for r in fast} == {(1, 2), (2, 1)}
    assert any(r['g'] == r['h'] for r in reference)
    assert any(r['g'] < r['h'] for r in reference)
    assert min(r['delta_distance_km'] for r in fast) == pytest.approx(min(r['delta'] for r in reference))
    assert min(r['estimated_drop_min'] for r in fast) == pytest.approx(min(r['drop'] for r in reference))
    assert stats['source_profile_positions'] == len(reference)
    assert stats['source_profile_summary_rows'] == len(fast) < len(reference)
    assert stats['source_first_pickup_tables_built'] == 2
    repeated = m._joint_rows(p, sf, (1, 2), 0, 1, [101, 103], deadline,
        cache, profile=True, statistics=stats)
    assert repeated == fast and stats['source_first_pickup_tables_built'] == 2
    assert stats['source_first_pickup_table_hits'] == 2
    # Every summary row also represents its exact constructed distance/clock.
    for row in fast:
        distance, clocks = walk(p, m._joint_route(base[0], row), 0)
        assert row['delta_distance_km'] == pytest.approx(distance-walk(p, base[0], 0)[0])
        assert row['estimated_drop_min'] == pytest.approx(clocks[row['drop_positions'][0]])


def test_full_joint_domain_allows_native_load_one_but_never_crosses_load_two():
    p, base = fixture(); sf = surface(p, base)
    rows = m._joint_rows(p, sf, (1, 2), 0, 1, [101, 103], perf_counter()+2., {})
    reference = brute(p, sf)
    actual = {(tuple(r['pickup_order_task_ids']), r['gap'], r['end_gap']) for r in rows}
    assert actual == {(r['order'], r['g'], r['h']) for r in reference}
    assert ((1, 2), 3, 4) in actual  # Pick up while native task4 is aboard, deliver it, then second IN.
    assert ((1, 2), 1, 4) not in actual  # Original two-native segment cannot carry another IN.
    assert all(max(m._loads(m._joint_route(base[0], row))) == 2 for row in rows)


def test_second_pickup_at_H_has_no_positive_joint_IN_flight():
    p, base = fixture(); H = p.relay_stations[0].point
    p = replace(p, tasks=tuple(replace(t, pickup=H) if t.id == 2 else t for t in p.tasks))
    sf = surface(p, base)
    rows = m._joint_rows(p, sf, (1, 2), 0, 1, [101, 103], perf_counter()+2., {})
    assert rows and {tuple(r['pickup_order_task_ids']) for r in rows} == {(2, 1)}
    p = replace(p, tasks=tuple(replace(t, pickup=H) if t.id == 1 else t for t in p.tasks))
    assert m._joint_rows(p, surface(p, base), (1, 2), 0, 1, [101, 103], perf_counter()+2., {}) == []


def joint_parent():
    p, base = fixture()
    routes = ((3, 4, -3, 101, -4, 103, -101, -103, 5, -5), base[1], (102, 104, -102, -104))
    group = dict(group_id=1, i=1, j=2, a=0, b=0, c=2, station_id=1, source_type='joint_pair')
    assert evaluate_solution(p, routes, _force_full=True).valid
    return p, base, routes, group


def test_canonical_joint_drops_old_selected_subsequence_and_banned_gaps():
    p, _, parent, group = joint_parent(); sg = m.signature(p, parent, [group])
    assert m.shape(p, parent, [group], expected_signature=sg)['final_counts'] == [3, 1, 2]
    reversed_drop = (parent[0][:6]+(-103, -101)+parent[0][8:], *parent[1:])
    assert evaluate_solution(p, reversed_drop, _force_full=True).valid
    with pytest.raises(ValueError, match='canonical'):
        m.shape(p, reversed_drop, [group])
    pickup_reorder = (parent[0][:3]+(103, -4, 101, -101, -103)+parent[0][8:], *parent[1:])
    with pytest.raises(ValueError, match='old plans'):
        m.shape(p, pickup_reorder, [group], expected_signature=sg)
    assert m._forbidden(p, parent[0], [group], 0) == {7}
    assert m._forbidden(p, parent[2], [group], 2) == {1}
    source = surface(p, parent, groups=[group]);source['banned'] |= {0, 4}
    first = m._first_table(p, source, 6, perf_counter()+2.)
    assert all(first['best_before'][h] is None or first['best_before'][h][1] not in source['banned']
        for h in range(len(parent[0])+1))
    # The new receiver domain cannot cut an old IN double drop or double OUT take.
    for a in (0, 2):
        sf = surface(p, parent, a, [group]);out = m._out_rows(p, sf, a, (3, 4), 1,
            [106, 108], (1., 1.), perf_counter()+2.)
        assert out and all(r['gap'] not in sf['banned'] and r['end_gap'] not in sf['banned'] for r in out)


def test_legacy_split_shape_signature_unchanged_and_mixed_parent_supported():
    p, _, one, two, groups = split_fixture()
    assert m.shape(p, one, groups[:1]) == m.old.shape(p, one, groups[:1])
    assert m.signature(p, one, groups[:1]) == m.old.signature(p, one, groups[:1])
    assert m.shape(p, two, groups) == m.old.shape(p, two, groups)
    # Replace group2's two sources by a true single source while preserving group1.
    mixed = (two[0], (103, -103, 105, 107, -105, -107, 5, 6, -5, -6), (102, 104, -102, -104))
    new = dict(groups[1], b=1, source_type='joint_pair')
    assert evaluate_solution(p, mixed, _force_full=True).valid
    actual = m.shape(p, mixed, [groups[0], new], expected_signature=m.signature(p, one, groups[:1]))
    assert actual['group_count'] == 2 and actual['cross_uav_count'] == 4
    with pytest.raises(ValueError):
        m.shape(p, mixed, [groups[0], {k:v for k,v in new.items() if k != 'source_type'}])


def test_actual_catalogue_has_one_joint_source_and_full_quota_without_source_overwrite():
    p, base = fixture(); stats = {}
    cat = m.propose_catalogue(p, base, [], deadline=perf_counter()+3., weights=W, scales=S,
        max_families=3, source_option_limit=3, receiver_option_limit=3,
        position_limit=12, feeder_limit=2, base_pool_limit=8, statistics=stats)
    assert cat['completed'] and stats['completed'] and cat['complete_candidates']
    assert cat['parent_signature'] == m.signature(p, base, [])
    assert cat['metadata']['configuration']['source_group_policy'] == 'same_source_joint_pair_only'
    assert stats['source_first_pickup_tables_built'] <= p.drone_count*len(p.tasks)
    assert stats['source_first_pickup_table_hits'] > 0
    assert len(cat['complete_candidates']) <= len(cat['families'])*3*3
    valid = []
    for proposal in cat['complete_candidates']:
        g = proposal['new_group'];routes = tuple(map(tuple, proposal['routes']))
        assert g['a'] == g['b'] != g['c'] and g['source_type'] == 'joint_pair'
        assert len(proposal['source_layouts']) == 1 and not proposal['full_global_checked']
        assert m._digest(routes) == proposal['routes_sha256']
        assert routes[g['a']] == m._joint_route(base[g['a']], proposal['source_layouts'][0])
        shape = m.shape(p, routes, [g], expected_signature=cat['parent_signature'])
        assert shape['final_counts'] == [3, 1, 2]
        index = build_plan_index(p, routes)
        assert {index.inbound_route_by_task[t] for t in (g['i'],g['j'])} == {g['a']}
        schedule = GlobalRelayEvaluator(p).evaluate(routes)
        if schedule.feasible:
            assert evaluate_solution(p, routes, _force_full=True).valid
            valid.append(proposal)
    assert valid
    from algorithm.experiments import audit_joint_source_native_v1 as typed
    from algorithm.experiments import audit_multi_relay_accumulation_v1 as independent
    tasks = {t.id: ((t.pickup.x, t.pickup.y), (t.delivery.x, t.delivery.y), t.deadline_min)
        for t in p.tasks}
    graph = topology(p)
    graph['task_relay_candidates'] = {str(t): list(p.task_relay_candidates[t]) for t in p.task_ids}
    with typed.joint_source_context():
        proof = independent.audit_step(tasks, graph, base, base, valid[0]['routes'],
            [], valid[0]['new_group'], weights=W, scales=S)
    assert proof['passed'] and proof['cross_uav_count'] == 2


def test_deadline_is_frozen_identity_and_expired_catalogue_never_publishes_prefix(monkeypatch):
    assert m.Deadline is m.old.Deadline
    p, base = fixture();stats = {}
    with pytest.raises(m.Deadline):
        m.propose_catalogue(p, base, [], deadline=perf_counter()-1., weights=W, scales=S, statistics=stats)
    assert stats['deadline_reached'] and not stats['completed']
    # Expiry during final parent/candidate hash must not publish a completed result.
    clock = [perf_counter()];deadline = clock[0]+2.;original = m._digest
    def expire(value):
        clock[0] = deadline+1.
        return original(value)
    monkeypatch.setattr(m, 'perf_counter', lambda: clock[0])
    monkeypatch.setattr(m, '_digest', expire)
    stats = {}
    with pytest.raises(m.Deadline):
        m.propose_catalogue(p, base, [], deadline=deadline, weights=W, scales=S,
            max_families=1, source_option_limit=1, receiver_option_limit=1,
            position_limit=1, base_pool_limit=1, statistics=stats)
    assert stats['deadline_reached'] and not stats['completed']
