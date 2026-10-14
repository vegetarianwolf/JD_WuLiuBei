"""Separated receiver pricing: synthetic finite models, never official data."""
from dataclasses import replace
from time import perf_counter

import pytest

from uav_dispatch import Point,Problem,Task,RelayStation,TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import split_feeder_catalogue_v4 as cat
from algorithm.experiments import split_feeder_catalogue_v3 as previous
from algorithm.tests.test_split_feeder_catalogue_v2 import audit_inputs

W,S=(.5,.25,.25),(9.,100.,100.)


def fixture():
    P=Point;H=P(0.,0.)
    tasks=[Task(1,H,P(1.,0.),1.),Task(2,H,P(10.,0.),10.),Task(3,P(1.,0.),P(10.,0.),10.)]
    tasks.extend(Task(t,H,H,100.) for t in range(4,10))
    registry={101:TransportLeg(101,1,'RELAY_IN',1),102:TransportLeg(102,1,'RELAY_OUT',1),
              103:TransportLeg(103,2,'RELAY_IN',1),104:TransportLeg(104,2,'RELAY_OUT',1)}
    p=Problem(tuple(tasks),drone_count=3,max_tasks_per_drone=3,capacity=2,speed_km_per_min=1.,
        drone_homes=(H,H,H),relay_stations=(RelayStation(1,0.,0.),),leg_registry=registry,
        task_relay_candidates={1:(1,),2:(1,)})
    return p,((4,-4,5,-5,6,-6),(7,-7,8,-8,9,-9),(1,-1,2,-2,3,-3))


def build(p=None,base=None,**kwargs):
    if p is None:p,base=fixture()
    args=dict(deadline=perf_counter()+4.,weights=W,scales=S,max_families=2,base_pool_limit=2)
    args.update(kwargs)
    return cat.build_catalogue(p,base,**args)


def surface(p,base,*,fill=True,stats=None):
    record=cat.costs.route_cost(p,tuple(v for v in base[2] if abs(v) not in (1,2)),2,deadline=perf_counter()+4.)
    rows=cat._receiver_rows(p,record,2,(1,2),1,perf_counter()+4.)
    original={t:v for a,r in enumerate(base) for t,v in cat.costs.route_cost(p,r,a,deadline=perf_counter()+4.)['delivery_times_min'].items()}
    stats={} if stats is None else stats
    price=cat._receiver_prices(p,record,2,(1,2),1,rows,{t:p.task(t).deadline_min for t in p.task_ids},
        original,W,S,perf_counter()+4.,stats,fill_limit=len(rows) if fill else 0)
    return record,rows,price,stats


def test_separated_native_delivery_improves_over_every_contiguous_C_layout():
    p,base=fixture();record,rows,price,_=surface(p,base)
    values=price(dict(ready=0.),dict(ready=0.))
    contiguous=[v for v in values if v['row'][0]==v['row'][1]]
    separated=[v for v in values if v['row'][0]<v['row'][1]]
    assert min(v['key'][0] for v in separated)<min(v['key'][0] for v in contiguous)
    best=min(values,key=lambda v:v['key'])
    assert best['key'][1:]==(0,0.,10.) and best['row'][:2]==(0,1)
    assert record['score'].distance_km==10.


def test_complete_new_catalogue_retains_real_separated_route_and_true_fleet_score():
    p,base=fixture();old=previous.build_catalogue(p,base,deadline=perf_counter()+4.,weights=W,scales=S,max_families=2,base_pool_limit=2)
    new=build(p,base)
    assert min(f['priority'][0] for f in new['families'])<min(f['priority'][0] for f in old['families'])
    for f in new['families']:
        assert f['early_pricing_evidence']['receiver_layout']['layout_type']=='separated'
        assert f['priority'][1:]==[0,0.,10.]
        assert f['early_pricing_evidence']['priced_seed_routes'][2]==[102,104,-102,3,-104,-3]


@pytest.mark.parametrize('speed',(1.,.9))
@pytest.mark.parametrize('availability',((0.,0.),(5.,9.)))
def test_all_g_h_orders_match_actual_native_selected_score_and_clocks(speed,availability):
    p,base=fixture();p=replace(p,speed_km_per_min=speed)
    record,rows,price,_=surface(p,base)
    values=price(dict(ready=availability[0]),dict(ready=availability[1]))
    assert len(values)==len(rows)
    for value in values:
        route=cat._receiver_route(record['route'],(102,104),(1,2),value['row'])
        actual=cat.costs.route_cost(p,route,2,availability=dict(zip((1,2),availability)),selected_pair=(102,104),deadline=perf_counter()+4.)
        score=actual['score']
        assert value['key'][1:]==pytest.approx((score.late_count,score.total_lateness_min,score.distance_km),abs=1e-10)
        assert value['T']==actual['event_times_min'][value['row'][0]+1]
        assert actual['shared_loaded_distance_km']>1e-10


def test_wait_is_added_once_to_middle_and_tail_not_twice():
    p,base=fixture();record,_,price,_=surface(p,base)
    value=next(v for v in price(dict(ready=5.),dict(ready=9.)) if v['row'][:2]==(0,1) and v['row'][3]==1)
    assert value['wait']==9. and value['middle_shift']==value['shift']==9.
    assert value['key'][1:]==pytest.approx((3,27.,10.))
    assert value['T']==9.


def test_capacity_checks_empty_start_and_entire_carry_interval_including_h():
    p,base=fixture();P=Point
    p=replace(p,tasks=(*p.tasks,Task(10,P(2.,0.),P(3.,0.),100.)),max_tasks_per_drone=4)
    record=cat.costs.route_cost(p,(3,10,-3,-10),2,deadline=perf_counter()+4.)
    rows=cat._receiver_rows(p,record,2,(1,2),1,perf_counter()+4.)
    identities={(r[0],r[1]) for r in rows}
    assert (0,1) in identities and (0,2) not in identities and (0,4) not in identities
    assert all(r[0] in (0,4) for r in rows)  # gap1 carries native1, so cannot double take


def test_contiguous_joint_detour_and_separated_open_tail_use_distinct_formulas():
    p,base=fixture();record,rows,_,_=surface(p,base)
    continuous=next(r for r in rows if r[:2]==(0,0) and r[3]==1)
    assert continuous[7]==18. and continuous[8]==0.
    separated=next(r for r in rows if r[:2]==(0,2) and r[3]==1)
    assert separated[7]==separated[8]==0.
    assert cat._receiver_route(record['route'],(102,104),(1,2),separated)==(102,104,-102,3,-3,-104)


def test_first_negative_precedes_native_pickup_and_all_actual_positions_are_bound():
    p,base=fixture();result=build(p,base)
    for f in result['families']:
        for evidence in (f['early_pricing_evidence'],f['early_rescue_pricing_evidence']):
            r=evidence['receiver_layout'];g,h=r['gap'],r['delivery_gap']
            assert (r['original_pickup_gap'],r['original_delivery_gap'])==(g,h)
            assert r['take_positions']==[g,g+1] and r['delivery_positions']==[g+2,h+3]
            route=evidence['priced_seed_routes'][2]
            assert route[g:g+2]==[102,104] and route[g+2] in (-102,-104)
            assert r['delta_distance_km']==r['head_detour_distance_km']+r['tail_detour_distance_km']


def test_zero_first_shared_flight_is_not_a_relay_pair_witness():
    p,base=fixture();p=replace(p,tasks=tuple(replace(t,delivery=Point(0.,0.)) if t.id==1 else t for t in p.tasks))
    _,rows,_,_=surface(p,base)
    assert rows and all(r[3]==2 for r in rows)


def test_new_receiver_domain_is_used_in_general_full_tables_without_old_rebuild(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('old four-block receiver rebuild must not run')
    monkeypatch.setattr(cat.costs,'receiver_options',forbidden)
    p,base=fixture();result=build(p,base)
    assert result['configuration']['general_receiver_uses_same_separated_domain']
    for f in result['families']:
        for retention,evidence in zip(f['seed_retention'],(f['early_pricing_evidence'],f['early_rescue_pricing_evidence'])):
            assert retention['receiver_seed_layout_full_scored'] and retention['early_seed_entered_candidate_pool']
            assert f['priority'][0]<=evidence['key'][0]+1e-10
        assert len(f['relay_witnesses'])<=3


def test_general_J_N_pooled_channels_and_complete_native_owners_remain_real():
    p,base=fixture();result=build(p,base);owners=build_plan_index(p,base).delivery_route_by_task
    assert result['configuration']['full_retention_channels']==['weighted_key','minimum_late_count','pooled_rescue']
    for f in result['families']:
        for w in f['relay_witnesses']:
            checked=evaluate_solution(p,w['routes'],_force_full=True);global_=GlobalRelayEvaluator(p).evaluate(w['routes'])
            assert checked.valid and global_.feasible and len(global_.delivery_times_min)==9
            assert build_plan_index(p,w['routes']).delivery_route_by_task==owners
            assert w['table_score']['late_count']==global_.score.late_count
            for a,route in enumerate(w['routes']):
                assert w['event_times_min'][a]==pytest.approx([global_.event_times_min[a,k] for k in range(len(route))])


def test_source_033_positions_prices_and_representatives_are_unchanged():
    from algorithm.tests.test_split_feeder_catalogue_v3 import carried_fixture
    p,base=carried_fixture();record=cat.costs.route_cost(p,base[0],0,deadline=perf_counter()+4.)
    due={t:p.task(t).deadline_min for t in p.task_ids};args=(p,record,0,1,1,101,due,perf_counter()+4.,W,S)
    new=cat._source_positions(*args,{}, {},{})
    old=previous._source_positions(*args,{}, {},{})
    assert new==old


def test_selected_only_EPS_replays_before_ranking_and_pair_cache_does_not_alias(monkeypatch):
    p,base=fixture();p=replace(p,tasks=tuple(replace(t,deadline_min=100.) if t.id in (2,3) else t for t in p.tasks))
    _,_,price,stats=surface(p,base,fill=False)
    price(dict(ready=1e-9),dict(ready=0.))
    assert stats['receiver_selected_boundary_route_replays']>0
    assert stats.get('receiver_native_float_route_replays',0)==0
    assert stats['receiver_boundary_pair_cache_entries']==1
    price(dict(ready=0.),dict(ready=1e-9))
    assert stats['receiver_boundary_pair_cache_entries']==2 and stats.get('receiver_ready_cache_hits',0)==0
    price(dict(ready=1e-9),dict(ready=0.))
    assert stats['receiver_actual_pair_cache_hits']==1


def test_receiver_native_boundary_and_combined_error_require_full_replay(monkeypatch):
    p,base=fixture();p=replace(p,tasks=tuple(replace(t,deadline_min=10.-1e-9) if t.id==3 else t for t in p.tasks))
    _,_,price,stats=surface(p,base)
    price(dict(ready=0.),dict(ready=0.))
    assert stats['receiver_native_float_route_replays']>0
    assert stats['receiver_boundary_pair_cache_entries']>0


def test_large_clock_combined_two_band_error_uses_actual_route_even_far_from_EPS():
    p,base=fixture()
    p=replace(p,tasks=tuple(replace(t,delivery=Point(1e12,0.),deadline_min=2e12) if t.id==3
        else replace(t,deadline_min=2e12) if t.id in (1,2) else t for t in p.tasks))
    record,_,query,stats=surface(p,base)
    values=query(dict(ready=0.),dict(ready=0.))
    assert stats['float_replay_two_zone_sum_error']>0
    for value in values:
        actual=cat.costs.route_cost(p,cat._receiver_route(record['route'],(102,104),(1,2),value['row']),2,
            availability={1:0.,2:0.},selected_pair=(102,104),deadline=perf_counter()+4.)
        assert value['key'][1]==actual['score'].late_count
        assert value['key'][2]==pytest.approx(actual['score'].total_lateness_min)


def test_internal_receiver_middle_index_expiry_translates_primitive_deadline(monkeypatch):
    p,base=fixture();_,_,query,_=surface(p,base)
    middle=cat._middle_index;build_index=cat.suffix.build_lateness_index;reached=[]
    def stop(*args,**kwargs):raise cat.suffix.Deadline
    def expire_middle(*args,**kwargs):
        reached.append(True);monkeypatch.setattr(cat.suffix,'build_lateness_index',stop)
        try:return middle(*args,**kwargs)
        finally:monkeypatch.setattr(cat.suffix,'build_lateness_index',build_index)
    monkeypatch.setattr(cat,'_middle_index',expire_middle)
    with pytest.raises(cat.Deadline):query(dict(ready=0.),dict(ready=0.))
    assert reached


def test_receiver_query_deadline_and_complete_catalogue_deadline_have_no_partial_adoption():
    p,base=fixture();record=cat.costs.route_cost(p,(3,-3),2,deadline=perf_counter()+4.)
    rows=cat._receiver_rows(p,record,2,(1,2),1,perf_counter()+4.)
    query=cat._receiver_prices(p,record,2,(1,2),1,rows,{t:p.task(t).deadline_min for t in p.task_ids},
        {t:0. for t in p.task_ids},W,S,perf_counter()-1.,{})
    with pytest.raises(cat.Deadline):query(dict(ready=0.),dict(ready=0.))
    stats={}
    with pytest.raises(cat.Deadline):build(p,base,deadline=perf_counter()-1.,statistics=stats)
    assert not stats['completed'] and stats['deadline_reached']


def test_current_source_receiver_and_complete_family_independent_v4_audit():
    from algorithm.experiments.audit_split_feeder_v4 import audit_split_feeder_family
    p,base=fixture();tasks,graph=audit_inputs(p)
    for f in build(p,base)['families']:
        assert audit_split_feeder_family(tasks,graph,base,f,weights=W,scales=S)['passed']


def test_hash_and_two_early_layouts_are_shared_without_arm_inputs():
    p,base=fixture();one=build(p,base);two=build(p,base)
    assert one['families']==two['families'] and one['selector_sha256']==two['selector_sha256']
    assert one['catalogue_sha256']==two['catalogue_sha256']
    assert one['configuration']['source_capacity_scope']==previous.build_catalogue(
        p,base,deadline=perf_counter()+4.,weights=W,scales=S,max_families=2,base_pool_limit=2)['configuration']['source_capacity_scope']
