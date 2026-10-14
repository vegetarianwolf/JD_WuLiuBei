"""Synthetic clocks and truncation counterexamples; no competition data."""
from dataclasses import replace
import json
from math import nextafter
from time import perf_counter

import pytest

from uav_dispatch import Point,Problem,Task,RelayStation,TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import split_feeder_catalogue_v2 as cat
from algorithm.tests.test_split_feeder_catalogue import fixture,W,S


def build(p=None,routes=None,**kwargs):
    if p is None:p,routes=fixture()
    params=dict(deadline=perf_counter()+4.,weights=W,scales=S,max_families=4,base_pool_limit=4)
    params.update(kwargs)
    return cat.build_catalogue(p,routes,**params)


def damage_fixture():
    """The geometrically earliest feeder hurts an urgent native delivery."""
    P=Point
    tasks=[Task(1,P(0.,0.),P(-1.,3.),8.),Task(2,P(4.,0.),P(-1.,4.),10.),
           Task(3,P(0.,0.),P(2.,0.),2.)]
    tasks.extend(Task(t,P(2.,0.),P(2.,0.),100.) for t in (4,5))
    tasks.extend(Task(t,P(4.,0.),P(4.,0.),100.) for t in (6,7,8))
    tasks.extend(Task(t,P(-1.,0.),P(-1.,0.),100.) for t in (9,10,11))
    registry={101:TransportLeg(101,1,'RELAY_IN',9),102:TransportLeg(102,1,'RELAY_OUT',9),
              103:TransportLeg(103,2,'RELAY_IN',9),104:TransportLeg(104,2,'RELAY_OUT',9)}
    p=Problem(tuple(tasks),drone_count=4,max_tasks_per_drone=3,capacity=2,speed_km_per_min=1.,
        drone_homes=(P(0.,0.),P(4.,0.),P(-1.,0.),P(-1.,0.)),relay_stations=(RelayStation(9,-1.,0.),),
        leg_registry=registry,task_relay_candidates={1:(9,),2:(9,)})
    routes=((3,-3,4,-4,5,-5),(6,-6,7,-7,8,-8),(9,-9,10,-10),(1,-1,2,-2,11,-11))
    return p,routes


def audit_inputs(p):
    from algorithm.tests.test_relay_repooling_exchange import topology
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in p.tasks}
    graph=topology(p)
    graph['task_relay_candidates']={str(t):list(p.task_relay_candidates.get(t,())) for t in p.task_ids}
    return tasks,graph


def test_true_native_delta_changes_feeder_before_limit_one():
    p,base=damage_fixture();scales=(11.,100.,100.)
    params=dict(deadline=perf_counter()+4.,weights=W,scales=scales,max_families=1,base_pool_limit=1,feeder_limit=1)
    previous=cat.old.build_catalogue(p,base,**params)
    current=cat.build_catalogue(p,base,**{**params,'deadline':perf_counter()+4.})
    assert previous['families'][0]['a']==0
    family=current['families'][0]
    assert family['a']==2 and family['b']==1
    assert current['statistics']['source_profiles_priced_before_feeder_limit']==6
    assert current['statistics']['feeder_profiles_retained']==2
    assert family['priority'][0]<previous['families'][0]['priority'][0]
    assert family['early_pricing_evidence']['source_layouts'][0]['native_score']['late_count']==0


def test_old_contiguous_proxy_omits_real_native_loss_new_price_does_not():
    p,base=damage_fixture();scales=(11.,100.,100.)
    old=cat.old.build_catalogue(p,base,deadline=perf_counter()+4.,weights=W,scales=scales,
        max_families=1,base_pool_limit=1,feeder_limit=1)['families'][0]
    routes=list(base)
    for t,a,value in zip((old['i'],old['j']),(old['a'],old['b']),old['proxy']['source_proxy_records']):
        incoming=cat.costs.frozen._leg(p,t,old['station_id'],'RELAY_IN');g=value['gap']
        routes[a]=base[a][:g]+(incoming,-incoming)+base[a][g:]
    c=old['c'];skeleton=tuple(v for v in base[c] if abs(v) not in (1,2));g=old['proxy']['receiver_gap']
    out=tuple(cat.costs.frozen._leg(p,t,9,'RELAY_OUT') for t in (1,2))
    drops=tuple(-cat.costs.frozen._leg(p,t,9,'RELAY_OUT') for t in old['proxy']['delivery_order_task_ids'])
    routes[c]=skeleton[:g]+out+drops+skeleton[g:]
    actual=evaluate_solution(p,routes,_force_full=True)
    assert actual.valid and actual.score.late_count>0  # proxy assumes unchanged native N=0
    assert actual.score.total_lateness_min>=2.
    current=build(p,base,weights=W,scales=scales,max_families=1,base_pool_limit=1,feeder_limit=1)
    witness=current['families'][0]['early_pricing_evidence']
    checked=evaluate_solution(p,witness['priced_seed_routes'],_force_full=True)
    assert witness['full_score']['late_count']==checked.score.late_count==0
    assert witness['full_score']['distance_km']==pytest.approx(checked.score.distance_km)


def test_every_source_gap_prices_held_native_and_tail_suffix(monkeypatch):
    p,base=damage_fixture();captured=[];original=cat._source_heads
    def spy(records,limit,bound):
        captured.append(list(records));return original(records,limit,bound)
    monkeypatch.setattr(cat,'_source_heads',spy)
    build(p,base,scales=(11.,100.,100.),max_families=1,base_pool_limit=1,feeder_limit=1)
    first={r['gap']:r for r in captured[0]}
    assert set(first)==set(range(len(base[0])+1))
    for gap,value in first.items():
        route=base[0][:gap]+(101,-101)+base[0][gap:]
        actual=cat.costs.route_cost(p,route,0,deadline=perf_counter()+4.)
        assert value['ready']==actual['availability'][1]
        score=actual['score']
        assert (value['N'],value['L'],value['D'])==pytest.approx((score.late_count,score.total_lateness_min,score.distance_km))
    assert first[1]['N']==1 and first[1]['L']==pytest.approx(2.)  # original task3 already held
    assert first[len(base[0])]['N']==0  # empty suffix still incurs positive flight D/drop time
    assert first[len(base[0])]['D']>cat.costs.route_cost(p,base[0],0,deadline=perf_counter()+4.)['score'].distance_km


def test_all_complete_prices_clocks_and_original_quota_match_global():
    p,base=fixture();result=build(p,base);original=build_plan_index(p,base)
    for family in result['families']:
        for evidence in (family['early_pricing_evidence'],family['early_rescue_pricing_evidence']):
            routes=evidence['priced_seed_routes'];check=evaluate_solution(p,routes,_force_full=True)
            assert check.valid and check.score.late_count==evidence['full_score']['late_count']
            assert check.score.total_lateness_min==pytest.approx(evidence['full_score']['total_lateness_min'])
            assert check.score.distance_km==pytest.approx(evidence['full_score']['distance_km'])
            assert build_plan_index(p,routes).delivery_route_by_task==original.delivery_route_by_task
        for witness in family['relay_witnesses']:
            schedule=GlobalRelayEvaluator(p).evaluate(witness['routes'])
            assert schedule.feasible and schedule.score.late_count==witness['table_score']['late_count']
            for a,route in enumerate(witness['routes']):
                assert witness['event_times_min'][a]==pytest.approx([schedule.event_times_min[a,k] for k in range(len(route))])
            assert witness['delivery_times_min']==pytest.approx({str(t):v for t,v in schedule.delivery_times_min.items()})


def test_C_price_uses_retimed_removed_skeleton_all_gaps_and_actual_wait():
    p,base=fixture();result=build(p,base)
    for family in result['families']:
        evidence=family['early_pricing_evidence'];r=evidence['receiver_layout']
        skeleton=tuple(v for v in base[family['c']] if abs(v) not in (family['i'],family['j']))
        native=cat.costs.route_cost(p,skeleton,family['c'],deadline=perf_counter()+4.)
        assert native['delivery_times_min'][9]==0.  # original C delivers9 much later
        assert cat.costs.route_cost(p,base[2],2,deadline=perf_counter()+4.)['delivery_times_min'][9]>0.
        available=[x['in_drop_time_min'] for x in evidence['source_layouts']]
        assert r['second_take_min']==max(r['receiver_arrival_min'],*available)
        assert r['waiting_min']==r['second_take_min']-r['receiver_arrival_min']
        assert r['native_shift_min']==pytest.approx(r['delta_distance_km']/p.speed_km_per_min+r['waiting_min'])
    assert result['statistics']['receiver_all_gap_order_conditional_prices']>0
    assert result['statistics']['receiver_ready_cache_hits']>0


def test_exact_early_seed_is_in_family_surface_and_complete_pool_without_displacing_N_channel():
    p,base=fixture();result=build(p,base)
    assert result['configuration']['full_retention_channels']==['weighted_key','minimum_late_count','pooled_rescue']
    for family in result['families']:
        for record,evidence in zip(family['seed_retention'],(family['early_pricing_evidence'],family['early_rescue_pricing_evidence'])):
            assert record['early_price_model_replayed']
            assert record['source_seed_routes_retained'] and record['early_seed_full_scored'] and record['early_seed_entered_candidate_pool']
            for key,owner in zip(family['source_table_keys'],(family['a'],family['b'])):
                assert 1<=len(family['source_tables'][key])<=3
                assert evidence['priced_seed_routes'][owner] in [r['route'] for r in family['source_tables'][key]]
            assert family['priority'][0]<=evidence['key'][0]+1e-10
            assert record['witness_seed_route_retained']==any(w['routes']==evidence['priced_seed_routes'] for w in family['relay_witnesses'])


def test_cap_one_reports_actual_candidate_opportunity_not_merely_price_replay():
    result=build(source_option_limit=1)
    for family in result['families']:
        for saved,evidence in zip(family['seed_retention'],(family['early_pricing_evidence'],family['early_rescue_pricing_evidence'])):
            kept=all(evidence['priced_seed_routes'][owner] in [r['route'] for r in family['source_tables'][key]]
                     for key,owner in zip(family['source_table_keys'],(family['a'],family['b'])))
            assert saved['early_price_model_replayed'] and saved['early_seed_full_scored']==kept
            assert saved['early_seed_entered_candidate_pool']==kept
            if not kept:assert saved['retained_or_omitted_by_better_complete_channels']=='source_cap_below_two_early_layouts'


def test_source_family_authority_and_inspection_union_are_honest_and_JSON():
    result=build()
    assert result['source_tables_scope']=='inspection_union_not_a_single_shared_cap_surface'
    for family in result['families']:
        assert family['source_surface_scope']=='family_specific_cap_including_required_early_sources'
        for key,records in family['source_tables'].items():
            assert len(records)<=3
            for record in records:assert record in result['source_tables'][key]
    assert json.loads(json.dumps(result,allow_nan=False))['families']==result['families']


def test_public_independent_price_and_complete_family_auditor():
    from algorithm.experiments.audit_split_feeder_v2 import audit_split_feeder_family
    p,base=fixture();tasks,graph=audit_inputs(p)
    for family in build(p,base)['families']:
        reference=audit_split_feeder_family(tasks,graph,base,family,weights=W,scales=S)
        assert reference['passed'] and reference['audit_version']=='split_feeder_early_native_price_v2'


def test_selected_only_EPS_boundary_replays_C_before_front_pool_ranking():
    p,base=fixture(False)
    tasks=tuple(replace(t,deadline_min=8.-1e-9) if t.id==1 else t for t in p.tasks)
    p=replace(p,tasks=tasks);result=build(p,base,max_families=1,base_pool_limit=1)
    stats=result['statistics']
    assert stats['receiver_selected_boundary_route_replays']>0
    assert stats.get('receiver_native_float_route_replays',0)==0  # native deadlines are far away
    assert stats['receiver_boundary_pair_cache_entries']>0
    tasks,graph=audit_inputs(p)
    from algorithm.experiments.audit_split_feeder_v2 import audit_split_feeder_family
    assert audit_split_feeder_family(tasks,graph,base,result['families'][0],weights=W,scales=S)['passed']


def test_physical_boundary_fallback_is_cached_only_by_actual_availability_pair(monkeypatch):
    monkeypatch.setattr(cat,'_native_nl',lambda index,shift:(None,'forced_boundary'))
    p,base=fixture();result=build(p,base);stats=result['statistics']
    assert stats['receiver_boundary_pair_cache_entries']>0
    assert stats['receiver_actual_pair_cache_hits']>0
    assert stats.get('receiver_ready_cache_hits',0)==0
    assert stats.get('receiver_native_shift_cache_hits',0)==0
    for family in result['families']:
        saved=family['early_pricing_evidence'];actual=evaluate_solution(p,saved['priced_seed_routes'],_force_full=True)
        assert saved['full_score']['late_count']==actual.score.late_count
        assert saved['full_score']['total_lateness_min']==pytest.approx(actual.score.total_lateness_min)


def test_compact_query_matches_primitive_or_demands_physical_replay():
    p,base=damage_fixture();record=cat.costs.route_cost(p,base[0],0,deadline=perf_counter()+4.);due={t:p.task(t).deadline_min for t in p.task_ids}
    for gap in range(len(base[0])+1):
        index=cat._index(record,due,gap,None)
        for shift in (0.,.5,1.,3.,11.):
            value,reason=cat._native_nl(index,shift)
            reference=cat.suffix.shift_direct_suffix(index,shift)
            if reason is None:
                assert value==pytest.approx((reference['late_count'],reference['total_lateness_min']))
            else:assert value is None
    index=cat._index(record,due,0,None)
    assert cat._native_nl(index,-1.)[0] is None
    assert cat._native_nl(index,float('inf'))[0] is None


def test_tiny_negative_geometry_is_explicit_but_material_negative_is_rejected():
    assert cat._nonnegative_detour(nextafter(0.,-1.),1.)==(0.,True)
    assert cat._nonnegative_detour(0.,1.)==(0.,False)
    with pytest.raises(ValueError,match='exceeds roundoff'):cat._nonnegative_detour(-.01,1.)
    with pytest.raises(ValueError):cat._nonnegative_detour(float('nan'),1.)


def test_primitive_internal_deadline_is_translated_and_never_publishes_prefix(monkeypatch):
    def expired(*args,**kwargs):raise cat.suffix.Deadline
    monkeypatch.setattr(cat.suffix,'index_direct_suffix',expired)
    stats={}
    with pytest.raises(cat.Deadline):build(statistics=stats)
    assert stats['deadline_reached'] and not stats['completed']


def test_post_digest_deadline_and_expired_entry_do_not_return_partial(monkeypatch):
    stats={}
    with pytest.raises(cat.Deadline):build(deadline=perf_counter()-1.,statistics=stats)
    assert stats['deadline_reached'] and not stats['completed']
    digest=cat._digest;calls=[]
    def late_digest(value):
        calls.append(True)
        if len(calls)==2:raise cat.Deadline
        return digest(value)
    monkeypatch.setattr(cat,'_digest',late_digest)
    with pytest.raises(cat.Deadline):build(statistics=stats)
    assert len(calls)==2 and stats['deadline_reached'] and not stats['completed']


def test_common_catalogue_determinism_and_four_station_set_retention():
    p,base=fixture();stations=(*p.relay_stations,RelayStation(11,1.,0.),RelayStation(12,-1.,0.))
    legs={};identifier=101
    for h in stations:
        for t in (1,2):
            for kind in ('RELAY_IN','RELAY_OUT'):
                legs[identifier]=TransportLeg(identifier,t,kind,h.id);identifier+=1
    p=replace(p,relay_stations=stations,leg_registry=legs,task_relay_candidates={1:(9,10,11,12),2:(9,10,11,12)})
    one=build(p,base);two=build(p,base)
    assert {f['station_id'] for f in one['families']}=={9,10,11,12}
    assert one['families']==two['families'] and one['selector_sha256']==two['selector_sha256']
    assert one['catalogue_sha256']==two['catalogue_sha256']
    assert not one['configuration']['old_ready_distance_Pareto_used']


def test_complete_empty_network_is_not_an_unfinished_catalogue():
    p,base=fixture();p=replace(p,leg_registry={},task_relay_candidates={})
    result=build(p,base)
    assert result['completed'] and result['families']==[] and result['source_tables']=={}
