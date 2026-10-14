"""Two-gap source price correctness on synthetic 6/8/9-task models only."""
from dataclasses import replace
from time import perf_counter

import pytest

from uav_dispatch import Point,Problem,Task,RelayStation,TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import split_feeder_catalogue_v3 as cat
from algorithm.experiments import split_feeder_catalogue_v2 as previous
from algorithm.tests.test_split_feeder_catalogue import fixture as basic_fixture
from algorithm.tests.test_split_feeder_catalogue_v2 import audit_inputs

W,S=(.5,.25,.25),(8.,100.,100.)


def carried_fixture(alternate=False):
    P=Point
    tasks=[Task(1,P(0.,0.),P(11.,0.),33.),Task(2,P(20.,0.),P(12.,0.),33.),
        Task(3,P(0.,0.),P(0.,10.),10.),Task(4,P(10.,10.),P(10.,0.),30.),
        Task(5,P(20.,0.),P(20.,10.),10.),Task(6,P(10.,10.),P(10.,0.),30.)]
    homes=[P(0.,0.),P(20.,0.)];routes=[(3,-3,4,-4),(5,-5,6,-6)]
    if alternate:
        tasks.extend(Task(t,P(5.,0.),P(5.,0.),100.) for t in (7,8))
        homes.append(P(5.,0.));routes.append((7,-7,8,-8))
    homes.append(P(10.,0.));routes.append((1,-1,2,-2))
    registry={101:TransportLeg(101,1,'RELAY_IN',1),102:TransportLeg(102,1,'RELAY_OUT',1),
              103:TransportLeg(103,2,'RELAY_IN',1),104:TransportLeg(104,2,'RELAY_OUT',1)}
    p=Problem(tuple(tasks),drone_count=len(homes),max_tasks_per_drone=2,capacity=2,speed_km_per_min=1.,
        drone_homes=tuple(homes),relay_stations=(RelayStation(1,10.,0.),),leg_registry=registry,
        task_relay_candidates={1:(1,),2:(1,)})
    return p,tuple(routes)


def build(p=None,base=None,**kwargs):
    if p is None:p,base=carried_fixture()
    options=dict(deadline=perf_counter()+4.,weights=W,scales=S,max_families=2,base_pool_limit=2)
    options.update(kwargs)
    return cat.build_catalogue(p,base,**options)


def source_records(p,base,*,a=0,t=1,H=1,stats=None):
    record=cat.costs.route_cost(p,base[a],a,deadline=perf_counter()+4.)
    due={t:p.task(t).deadline_min for t in p.task_ids}
    incoming=cat.costs.frozen._leg(p,t,H,'RELAY_IN')
    values=cat._source_positions(p,record,a,t,H,incoming,due,perf_counter()+4.,W,S,{}, {},{} if stats is None else stats)
    return values,record,incoming


def test_opportunistic_carry_changes_feeder_before_limit_one():
    p,base=carried_fixture(True)
    params=dict(deadline=perf_counter()+4.,weights=W,scales=S,max_families=1,base_pool_limit=1,feeder_limit=1)
    old=previous.build_catalogue(p,base,**params)['families'][0]
    new=build(p,base,max_families=1,base_pool_limit=1,feeder_limit=1)['families'][0]
    assert old['a']==2 and new['a']==0 and new['b']==1
    assert new['early_priority'][0]<old['early_priority'][0]
    assert new['early_priority'][1:]==[0,0.,62.]
    assert all(s['gap']<s['drop_gap'] for s in new['early_pricing_evidence']['source_layouts'])


def test_contiguous_only_price_cannot_see_zero_detour_native_preservation():
    p,base=carried_fixture();values,native,_=source_records(p,base)
    contiguous=[r for r in values if r['gap']==r['drop_gap']]
    carry=[r for r in values if r['gap']<r['drop_gap']]
    assert min(r['deltaD'] for r in contiguous)==pytest.approx(10.*2.**.5)
    assert any(r['deltaD']==0 and r['N']==0 and r['ready']==30. for r in carry)
    assert min((r['delta_key'] for r in carry))<min(r['delta_key'] for r in contiguous)
    assert native['score'].distance_km==30.


@pytest.mark.parametrize('speed',(1.,.9))
def test_every_two_gap_price_ready_and_D_match_real_complete_route(speed):
    p,base=carried_fixture();p=replace(p,speed_km_per_min=speed)
    values,native,incoming=source_records(p,base)
    assert len(values)==15  # five contiguous + all ten g<h capacity-legal pairs
    for value in values:
        g,h=value['gap'],value['drop_gap']
        route=base[0][:g]+(incoming,)+base[0][g:h]+(-incoming,)+base[0][h:]
        actual=cat.costs.route_cost(p,route,0,deadline=perf_counter()+4.)
        score=actual['score']
        assert value['ready']==actual['availability'][1]
        assert (value['N'],value['L'],value['D'])==pytest.approx((score.late_count,score.total_lateness_min,score.distance_km),abs=1e-10)
        assert actual['final_delivery_count']==native['final_delivery_count']==2


def test_capacity_checks_whole_interval_and_includes_drop_gap():
    p,base=carried_fixture();base=((3,4,-3,-4),*base[1:])
    values,_,_=source_records(p,base);pairs={(v['gap'],v['drop_gap']) for v in values}
    assert (0,4) not in pairs  # endpoints empty, native gap2 carries two in the middle
    assert (0,2) not in pairs  # drop occurs before gap2's original event, but arrives loaded
    assert (1,1) in pairs and (3,4) in pairs
    loads=[0,1,2,1,0]
    expected={(g,h) for g in range(5) for h in range(g,5) if max(loads[g:h+1])<=1}
    assert pairs==expected


def test_native_held_before_pickup_and_tail_gap_are_in_correct_delay_bands():
    p,base=carried_fixture();values,_,incoming=source_records(p,base)
    value=next(v for v in values if (v['gap'],v['drop_gap'])==(1,4))
    assert value['drop_gap']==len(base[0])
    actual=cat.costs.route_cost(p,(3,incoming,-3,4,-4,-incoming),0,deadline=perf_counter()+4.)
    assert actual['load_before'][1]==1
    assert value['ready']==actual['availability'][1]
    assert actual['delivery_times_min'][3]==pytest.approx(10.+value['middle_shift'])
    assert actual['delivery_times_min'][4]==pytest.approx(30.+value['middle_shift'])
    assert value['N']==actual['score'].late_count


def test_contiguous_branch_uses_single_joint_edge_not_two_detours():
    p,base=carried_fixture();values,_,_=source_records(p,base)
    value=next(v for v in values if (v['gap'],v['drop_gap'])==(1,1))
    assert value['deltaD']==pytest.approx(10.*2.**.5)
    assert value['deltaP']==value['deltaD'] and value['deltaH']==0.


def test_stable_middle_tail_price_with_no_middle_delivery_and_prefix_late():
    p,base=carried_fixture()
    tasks=tuple(replace(t,deadline_min=.5) if t.id==3 else t for t in p.tasks)
    p=replace(p,tasks=tasks);values,_,_=source_records(p,base)
    for target in ((0,1),(2,3),(2,4)):
        value=next(v for v in values if (v['gap'],v['drop_gap'])==target)
        g,h=target;actual=cat.costs.route_cost(p,base[0][:g]+(101,)+base[0][g:h]+(-101,)+base[0][h:],0,deadline=perf_counter()+4.)
        assert (value['N'],value['L'])==pytest.approx((actual['score'].late_count,actual['score'].total_lateness_min),abs=1e-10)


def test_native_EPS_and_large_sum_force_replay_before_cuts():
    p,base=carried_fixture();stats={}
    p=replace(p,tasks=tuple(replace(t,deadline_min=10.-1e-9) if t.id==3 else t for t in p.tasks))
    values,_,_=source_records(p,base,stats=stats)
    assert stats['source_native_float_route_replays']>0 and any(v['replay'] for v in values)
    for v in values:
        g,h=v['gap'],v['drop_gap']
        actual=cat.costs.route_cost(p,base[0][:g]+(101,)+base[0][g:h]+(-101,)+base[0][h:],0,deadline=perf_counter()+4.)
        assert v['N']==actual['score'].late_count
    # Deliberately insufficient summed-L error allowance forces the same full
    # route path instead of subtracting large F_g/F_h lateness totals.
    index=cat._index(cat.costs.route_cost(p,base[0],0,deadline=perf_counter()+4.),
        {t:p.task(t).deadline_min for t in p.task_ids},0,None)
    assert cat._two_zone_nl(index,replace(index,prefix_late_count=0,prefix_lateness_min=0.),1.,2.,1.)[0] is None


def test_real_sequential_ready_works_beyond_static_four_event_example():
    p,base=carried_fixture();P=Point
    tasks=tuple(p.tasks)+(Task(7,P(10.,0.),P(10.,1.),100.),Task(8,P(10.,1.),P(10.,2.),100.))
    p=replace(p,tasks=tasks,max_tasks_per_drone=4)
    base=(base[0]+(7,-7,8,-8),*base[1:]);values,_,_=source_records(p,base)
    long=next(v for v in values if (v['gap'],v['drop_gap'])==(0,8))
    actual=cat.costs.route_cost(p,(101,)+base[0]+(-101,),0,deadline=perf_counter()+4.)
    assert long['ready']==actual['availability'][1]
    assert long['N']==actual['score'].late_count


def test_g_h_seed_survives_source_cap_and_full_candidate_pool_then_standard_channels():
    p,base=carried_fixture();result=build(p,base)
    for f in result['families']:
        assert result['configuration']['full_retention_channels']==['weighted_key','minimum_late_count','pooled_rescue']
        for saved,evidence in zip(f['seed_retention'],(f['early_pricing_evidence'],f['early_rescue_pricing_evidence'])):
            assert saved['source_seed_routes_retained'] and saved['early_seed_entered_candidate_pool'] and saved['early_seed_full_scored']
            assert f['priority'][0]<=evidence['key'][0]+1e-10
            for key,a in zip(f['source_table_keys'],(f['a'],f['b'])):
                assert len(f['source_tables'][key])<=3
                assert evidence['priced_seed_routes'][a] in [r['route'] for r in f['source_tables'][key]]
    assert result['statistics']['source_noncontiguous_positions_priced']==40


def test_whole_fleet_complete_current_clocks_and_original_final_counts():
    p,base=carried_fixture();result=build(p,base);owners=build_plan_index(p,base).delivery_route_by_task
    best=min((w for f in result['families'] for w in f['relay_witnesses']),key=lambda w:w['key'])
    assert best['table_score']=={'late_count':0,'total_lateness_min':0.,'distance_km':62.}
    for f in result['families']:
        for w in f['relay_witnesses']:
            checked=evaluate_solution(p,w['routes'],_force_full=True);global_=GlobalRelayEvaluator(p).evaluate(w['routes'])
            assert checked.valid and global_.feasible
            assert build_plan_index(p,w['routes']).delivery_route_by_task==owners
            for a,route in enumerate(w['routes']):
                assert w['event_times_min'][a]==pytest.approx([global_.event_times_min[a,k] for k in range(len(route))])


def test_v3_independent_source_g_h_and_complete_family_audit():
    from algorithm.experiments.audit_split_feeder_v3 import audit_split_feeder_family
    p,base=carried_fixture();tasks,graph=audit_inputs(p)
    for f in build(p,base)['families']:
        assert audit_split_feeder_family(tasks,graph,base,f,weights=W,scales=S)['passed']


def test_all_early_evidence_fields_alias_real_original_and_inserted_positions():
    p,base=carried_fixture();result=build(p,base)
    for f in result['families']:
        for evidence in (f['early_pricing_evidence'],f['early_rescue_pricing_evidence']):
            assert evidence['pricing_scope']==cat.EARLY_SCOPE
            for record in evidence['source_layouts']:
                g,h=record['gap'],record['drop_gap']
                assert record['original_pickup_gap']==g and record['original_drop_gap']==h
                assert record['delta_distance_km']==record['pickup_detour_distance_km']+record['drop_detour_distance_km']
                assert record['tail_native_shift_min']==record['native_shift_min']
                assert record['middle_native_shift_min']==record['pickup_detour_distance_km']/p.speed_km_per_min
                assert record['layout_type']==('contiguous' if g==h else 'noncontiguous')


def test_internal_middle_index_deadline_is_common_exception_without_partial_catalogue(monkeypatch):
    def stop(*args,**kwargs):raise cat.suffix.Deadline
    middle=cat._middle_index;build_index=cat.suffix.build_lateness_index;reached=[]
    def expire_in_middle(*args,**kwargs):
        reached.append(True);monkeypatch.setattr(cat.suffix,'build_lateness_index',stop)
        try:return middle(*args,**kwargs)
        finally:monkeypatch.setattr(cat.suffix,'build_lateness_index',build_index)
    monkeypatch.setattr(cat,'_middle_index',expire_in_middle)
    stats={}
    with pytest.raises(cat.Deadline):build(statistics=stats)
    assert reached and not stats['completed'] and stats['deadline_reached']


def test_selected_only_EPS_after_exact_carried_ready_uses_C_physical_fallback():
    p,base=carried_fixture()
    p=replace(p,tasks=tuple(replace(t,deadline_min=31.-1e-9) if t.id==1 else t for t in p.tasks))
    result=build(p,base)
    assert result['statistics']['receiver_selected_boundary_route_replays']>0
    from algorithm.experiments.audit_split_feeder_v3 import audit_split_feeder_family
    tasks,graph=audit_inputs(p)
    for f in result['families']:
        assert audit_split_feeder_family(tasks,graph,base,f,weights=W,scales=S)['passed']


def test_two_channel_actual_source_retention_and_hash_are_arm_independent():
    p,base=basic_fixture();one=build(p,base,max_families=4,base_pool_limit=4)
    two=build(p,base,max_families=4,base_pool_limit=4)
    assert one['families']==two['families'] and one['selector_sha256']==two['selector_sha256']
    assert one['catalogue_sha256']==two['catalogue_sha256']
    assert {f['station_id'] for f in one['families']}=={9,10}
    assert one['configuration']['source_general_position_limit_excludes_early_two_gap_seeds']
