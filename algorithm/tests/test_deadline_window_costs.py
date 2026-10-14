"""Small exact local clocks/window witnesses; never load official tasks."""
from dataclasses import replace
from itertools import permutations
from time import perf_counter

import pytest

from uav_dispatch import Point,Problem,Task,RelayStation,TransportLeg
from algorithm.experiments import deadline_window_costs as costs
from algorithm.experiments import six_task_route_costs as frozen

W,S=(.5,.25,.25),(6.,100.,100.)


def fixture():
    tasks=(Task(1,Point(0.,0.),Point(5.,0.),24.),
           Task(2,Point(4.,0.),Point(6.,0.),25.),
           Task(3,Point(1.,0.),Point(1.,0.),1.5),
           Task(4,Point(8.,0.),Point(8.,0.),27.),
           Task(5,Point(-10.,0.),Point(-10.,0.),10.),
           Task(6,Point(10.,0.),Point(10.,0.),100.))
    legs={101:TransportLeg(101,1,'RELAY_IN',9),102:TransportLeg(102,1,'RELAY_OUT',9),
          103:TransportLeg(103,2,'RELAY_IN',9),104:TransportLeg(104,2,'RELAY_OUT',9)}
    return Problem(tasks,drone_count=3,max_tasks_per_drone=2,capacity=2,speed_km_per_min=1.,
        drone_homes=(Point(0.,0.),Point(4.,0.),Point(2.,0.)),
        relay_stations=(RelayStation(9,2.,0.),),leg_registry=legs,task_relay_candidates={1:(9,),2:(9,)})


def args():return dict(deadline=perf_counter()+2.,weights=W,scales=S)


def assert_oracle(p,record,available=None,selected_pair=()):
    oracle=frozen.route_cost(p,record['route'],record['aircraft'],availability=available,
        selected_pair=selected_pair,deadline=perf_counter()+1.)
    for key in ('score','event_times','delivery_times','availability','waiting_min','load_before','load_after','shared_loaded_distance_km'):
        assert record[key]==oracle[key]
    assert record['complete_solution'] is False


def test_source_single_IN_can_start_loaded_and_interleave_both_native_events():
    p=fixture();records=costs.source_options(p,(3,-3),0,1,9,option_limit=4096,position_limit=4096,**args())
    assert records
    loaded=next(r for r in records if r['route']==(3,101,-3,-101))
    assert loaded['load_before'][loaded['pickup_position']]==1
    assert loaded['in_drop_time_min']==4. and loaded['availability']=={1:4.}
    assert set(loaded['delivery_times'])=={3} and loaded['optimistic_missing_comp']
    for r in records:assert_oracle(p,r)


def test_compensation_is_reinserted_before_actual_source_clock_and_changes_rescue():
    p=fixture()
    preview=costs.source_options(p,(),0,1,9,option_limit=2,**args())[0]
    full=costs.source_options(p,(),0,1,9,compensation_task_id=5,option_limit=2,position_limit=4096,**args())
    assert preview['in_drop_time_min']==2. and preview['optimistic_missing_comp']
    assert not full[0]['optimistic_missing_comp'] and full[0]['native_complete']
    assert full[0]['in_drop_time_min']==22. and full[0]['delivery_times'][5]==10.
    assert full[1]['in_drop_time_min']==2. and full[1]['score'].late_count==1
    original={1:30.,2:30.}
    early=costs.receiver_options(p,(),2,(1,2),9,{1:preview['in_drop_time_min'],2:2.},
        original_delivery_times=original,option_limit=3,**args())
    late=costs.receiver_options(p,(),2,(1,2),9,{1:full[0]['in_drop_time_min'],2:2.},
        original_delivery_times=original,option_limit=3,**args())
    assert set(early[0]['selected_rescued_task_ids'])=={1,2}
    assert not any(r['selected_rescued_task_ids'] for r in late)
    for r in full:assert_oracle(p,r)


def test_true_OUT_waits_and_all_native_suffix_deliveries_use_both_sources():
    p=replace(fixture(),max_tasks_per_drone=3)
    r=costs.route_cost(p,(102,104,-102,-104,4,-4),2,availability={1:10.,2:20.},
        selected_pair=(102,104),deadline=perf_counter()+1.)
    assert r['event_times'][:4]==(10.,20.,23.,24.)
    assert r['delivery_times'][4]==26. and r['total_waiting_min']==20.
    window=costs.make_window(p,r,(1,2),9,{1:10.,2:20.},gap=0,
        original_delivery_times={1:30.,2:30.,4:30.},sigma=0.,deadline=perf_counter()+1.)
    assert window['receiver_arrival_min']==0. and window['second_take_min']==20.
    assert window['selected_delivery_offsets_min']=={'1':3.,'2':4.}
    assert window['suffix_delivery_offsets_min']=={'1':3.,'2':4.,'4':6.}
    assert window['official_rescued_task_ids']==[1,2,4]
    assert window['selected_rescued_task_ids']==[1,2] and window['native_rescued_task_ids']==[4]
    assert_oracle(p,r,{1:10.,2:20.},(102,104))


def test_suffix_offsets_include_native_flight_between_selected_deliveries():
    p=replace(fixture(),max_tasks_per_drone=3)
    r=costs.route_cost(p,(102,104,-102,4,-4,-104),2,availability={1:10.,2:20.},
        selected_pair=(102,104),deadline=perf_counter()+1.)
    w=costs.make_window(p,r,(1,2),9,{1:10.,2:20.},sigma=0.,deadline=perf_counter()+1.)
    assert r['delivery_times'][2]==28. and w['selected_delivery_offsets_min']['2']==8.
    assert w['suffix_delivery_offsets_min']['4']==6.
    assert not w['safe_window_satisfied']
    assert_oracle(p,r,{1:10.,2:20.},(102,104))


def test_every_empty_gap_and_both_delivery_orders_are_truly_scored_before_retention():
    p=replace(fixture(),max_tasks_per_drone=4)
    stats={};native=(3,-3,4,-4)
    records=costs.receiver_options(p,native,2,(1,2),9,{1:2.,2:2.},gap_limit=1,
        option_limit=100,original_delivery_times={1:30.,2:30.,3:2.,4:30.},statistics=stats,**args())
    assert stats['receiver_candidates']==2*len(costs.zero_load_gaps(p,native))==6
    assert {r['pickup_position'] for r in records}=={0,2,4}
    assert {r['delivery_order_task_ids'] for r in records}=={(1,2),(2,1)}
    assert all(r['receiver_empty_gaps_evaluated']==3 and r['requested_gap_limit']==1 for r in records)
    for r in records:assert_oracle(p,r,{1:2.,2:2.},(102,104))


def test_K_overflow_preview_is_explicit_and_never_modifies_the_problem():
    p=fixture();native=(3,-3,4,-4)
    with pytest.raises(ValueError,match='exceeds K'):
        costs.receiver_options(p,native,2,(1,2),9,{1:2.,2:2.},**args())
    records=costs.receiver_options(p,native,2,(1,2),9,{1:2.,2:2.},allow_final_k_overflow=True,**args())
    assert p.max_tasks_per_drone==2
    assert all(r['preview_only'] and r['final_delivery_count']==4 and r['final_k_overflow_count']==2
               and r['final_k_limit']==2 and not r['complete_solution'] for r in records)
    assert all(r['score'].distance_km>0 and len(r['delivery_times'])==4 for r in records)


def test_direct_single_can_interleave_native_and_pair_supports_all_six_PD_orders():
    p=fixture();records=costs.direct_single_options(p,(3,-3),0,1,option_limit=4096,position_limit=4096,**args())
    assert any(r['route']==(3,1,-3,-1) for r in records)
    for r in records:assert_oracle(p,r)
    stats={};pairs=costs.direct_pair_options(p,(),2,(1,2),option_limit=100,gap_limit=1,statistics=stats,**args())
    assert stats['direct_pair_candidates']==6 and len(pairs)==6
    for r in pairs:assert_oracle(p,r,selected_pair=(1,2))


def test_DIRECT_pair_also_scans_every_empty_gap_even_when_gap_limit_is_one():
    p=replace(fixture(),max_tasks_per_drone=3);stats={}
    records=costs.direct_pair_options(p,(3,-3),2,(1,2),option_limit=100,gap_limit=1,statistics=stats,**args())
    assert stats['direct_pair_candidates']==12 and {r['pickup_position'] for r in records}=={0,2}
    assert all(r['empty_gaps_evaluated']==2 for r in records)


def test_all_twelve_A1_B1_C2_responsibility_maps_have_whole_DIRECT_local_support():
    p=fixture();selected={1,2,3,5};maps=set()
    for a_task,b_task in permutations(selected,2):
        c_pair=tuple(sorted(selected-{a_task,b_task}));maps.add((a_task,b_task,c_pair))
        a=costs.direct_single_options(p,(),0,a_task,**args())[0]
        b=costs.direct_single_options(p,(),1,b_task,**args())[0]
        c=costs.direct_pair_options(p,(),2,c_pair,**args())[0]
        assert set(a['delivery_times'])=={a_task} and set(b['delivery_times'])=={b_task}
        assert set(c['delivery_times'])==set(c_pair)
        assert all(abs(v)<100 for r in (a,b,c) for v in r['route'])
        assert sum(len(r['delivery_times']) for r in (a,b,c))==4
    assert len(maps)==12


@pytest.mark.parametrize('late,expected',[(.5e-9,0),(1.e-9,0),(1.5e-9,1)])
def test_official_N_and_L_follow_subtraction_threshold(late,expected):
    p=Problem((Task(1,Point(0.,0.),Point(0.,0.),0.),),drone_count=1,capacity=2,max_tasks_per_drone=1,
              speed_km_per_min=1.,drone_homes=(Point(late,0.),))
    r=costs.route_cost(p,(1,-1),0,deadline=perf_counter()+1.)
    assert r['score'].late_count==expected and r['score'].total_lateness_min==(late if expected else 0.)


def test_official_EPS_subtraction_and_extra_safety_window_are_distinct():
    p=fixture();p=replace(p,tasks=tuple(replace(t,deadline_min=3.-.5e-9) if t.id==1 else t for t in p.tasks))
    r=costs.receiver_options(p,(),2,(1,2),9,{1:0.,2:0.},option_limit=3,
        original_delivery_times={1:30.,2:30.},sigma=1e-8,**args())[0]
    assert 1 in r['selected_rescued_task_ids'] and 1 not in r['window']['safe_rescued_task_ids']
    assert not r['window']['safe_window_satisfied']
    # IEEE addition and subtraction can disagree at official epsilon. All
    # classifications use the same subtraction as the physical score.
    clock=16.+1e-9
    assert clock<=16.+1e-9 and clock-16.>1e-9
    q=replace(p,tasks=tuple(replace(t,deadline_min=16.) if t.id==1 else t for t in p.tasks))
    recovered,damaged,_=costs._classifications(q,{1:clock},{1:15.},0.)
    assert recovered==[] and damaged==[1]


def test_actual_homes_and_zero_shared_flight_are_not_replaced_by_station_start():
    p=fixture();records=costs.source_options(p,(),1,1,9,**args())
    assert records[0]['in_drop_time_min']==6.  # home4 -> pickup0 -> H2
    q=replace(p,tasks=tuple(replace(t,delivery=Point(2.,0.)) if t.id in (1,2) else t for t in p.tasks))
    stats={};assert costs.receiver_options(q,(),2,(1,2),9,{1:0.,2:0.},statistics=stats,**args())==()
    assert stats['receiver_zero_shared_rejections']==2


def test_required_window_Q_must_be_real_suffix_and_empty_Q_is_explicit():
    p=fixture();r=costs.receiver_options(p,(),2,(1,2),9,{1:0.,2:0.},**args())[0]
    with pytest.raises(ValueError,match='suffix deliveries'):
        costs.make_window(p,r,(1,2),9,{1:0.,2:0.},required_on_time_task_ids=(3,),deadline=perf_counter()+1.)
    w=costs.make_window(p,r,(1,2),9,{1:0.,2:0.},required_on_time_task_ids=(),deadline=perf_counter()+1.)
    assert w['joint_latest_second_take_min'] is None and w['safe_window_satisfied'] is None


@pytest.mark.parametrize('kind',['source','single','pair','receiver','window'])
def test_expired_deadline_and_retention_overrun_do_not_return_candidates(monkeypatch,kind):
    p=fixture();r=costs.receiver_options(p,(),2,(1,2),9,{1:0.,2:0.},**args())[0]
    def call(deadline):
        if kind=='source':return costs.source_options(p,(),0,1,9,deadline=deadline,weights=W,scales=S)
        if kind=='single':return costs.direct_single_options(p,(),0,1,deadline=deadline,weights=W,scales=S)
        if kind=='pair':return costs.direct_pair_options(p,(),2,(1,2),deadline=deadline,weights=W,scales=S)
        if kind=='receiver':return costs.receiver_options(p,(),2,(1,2),9,{1:0.,2:0.},deadline=deadline,weights=W,scales=S)
        return costs.make_window(p,r,(1,2),9,{1:0.,2:0.},deadline=deadline)
    with pytest.raises(costs.Deadline):call(perf_counter()-1.)
    if kind!='window':
        clock=[perf_counter()];deadline=clock[0]+10.;actual=costs._retained
        monkeypatch.setattr(costs,'perf_counter',lambda:clock[0])
        def overrun(*a,**k):
            value=actual(*a,**k);clock[0]=deadline+1.;return value
        monkeypatch.setattr(costs,'_retained',overrun)
        with pytest.raises(costs.Deadline):call(deadline)


def test_invalid_availability_mixed_tasks_nonempty_receiver_and_fake_old_clocks():
    p=fixture()
    for bad in ({1:-1.,2:0.},{1:float('nan'),2:0.},{1:True,2:0.},{1:0.}):
        with pytest.raises(ValueError):costs.receiver_options(p,(),2,(1,2),9,bad,**args())
    with pytest.raises(ValueError,match='selected task remains'):
        costs.source_options(p,(1,-1),0,1,9,**args())
    with pytest.raises(ValueError):costs.route_cost(p,(3,102,104,-102,-104,-3),2,
        availability={1:0.,2:0.},deadline=perf_counter()+1.)
    r=costs.receiver_options(p,(),2,(1,2),9,{1:2.,2:4.},**args())[0]
    r['event_times']=(-99.,)*4;r['score']=costs.Score(99,99.,99.)
    w=costs.make_window(p,r,(1,2),9,{1:2.,2:4.},deadline=perf_counter()+1.)
    assert w['second_take_min']==4. and w['receiver_arrival_min']==0.


def test_selected_rescue_survives_when_the_lowestN_candidate_only_protects_natives():
    p=fixture();h=Point(2.,0.)
    due={1:4.,2:100.,3:2.,4:8.}
    p=replace(p,max_tasks_per_drone=4,drone_homes=(h,h,h),tasks=tuple(
        replace(t,pickup=h,deadline_min=due[t.id]) if t.id in (1,2) else
        replace(t,deadline_min=due[t.id]) if t.id in due else t for t in p.tasks))
    available={t:costs.source_options(p,(),a,t,9,**args())[0]['in_drop_time_min']
               for a,t in enumerate((1,2))}
    assert available=={1:0.,2:0.}
    original={1:10.,2:10.,3:1.,4:8.};native=(3,-3,4,-4)
    all_options=costs.receiver_options(p,native,2,(1,2),9,available,original_delivery_times=original,
        option_limit=100,**args())
    kept=costs.receiver_options(p,native,2,(1,2),9,available,original_delivery_times=original,
        option_limit=3,**args())
    best=min(all_options,key=lambda r:costs.weighted_key(r['score'],W,S))
    assert best['score'].late_count==1 and not best['selected_rescued_task_ids']
    assert any(r['selected_rescued_task_ids']==[1] and r['score'].late_count==2 for r in kept)
    assert min(costs.weighted_key(r['score'],W,S) for r in kept)==costs.weighted_key(best['score'],W,S)
    assert min(r['score'].late_count for r in kept)==min(r['score'].late_count for r in all_options)
    assert all(r['receiver_any_selected_rescue_candidate'] and r['receiver_selected_rescue_layout_count']>0 for r in kept)
    assert all(r['latest_safe_rescue_gap_evaluated']==0 for r in kept)


@pytest.mark.parametrize('single',[False,True])
def test_DIRECT_three_channel_tables_keep_actual_weighted_and_N_and_rescue_extrema(single):
    p=replace(fixture(),max_tasks_per_drone=3)
    original={1:30.,2:30.,3:0.}
    if single:
        fn=lambda n:costs.direct_single_options(p,(3,-3),2,1,option_limit=n,
            position_limit=100,original_delivery_times=original,**args())
    else:
        fn=lambda n:costs.direct_pair_options(p,(3,-3),2,(1,2),option_limit=n,
            original_delivery_times=original,**args())
    all_options=fn(100);kept=fn(3)
    assert min(costs.weighted_key(r['score'],W,S) for r in kept)==min(costs.weighted_key(r['score'],W,S) for r in all_options)
    assert min(r['score'].late_count for r in kept)==min(r['score'].late_count for r in all_options)
    assert max(len(r['selected_rescued_task_ids']) for r in kept)==max(len(r['selected_rescued_task_ids']) for r in all_options)


def test_middle_source_window_is_kept_beyond_physical_and_earliest_drop_extremes():
    p=fixture();h=Point(0.,0.)
    tasks=tuple(replace(t,pickup=h,delivery=Point(-1.,0.),deadline_min=1.) if t.id==3 else
                replace(t,pickup=h,delivery=Point(-2.,0.),deadline_min=2.) if t.id==4 else t for t in p.tasks)
    p=replace(p,tasks=tasks,max_tasks_per_drone=4)
    native=(3,4,-3,-4,5,-5)  # Two urgent co-carried jobs, then true compensation5.
    records=costs.source_options(p,native,0,1,9,optimistic_missing_comp=False,
        option_limit=3,position_limit=8,latest_drop_bounds=(6.5,),**args())
    assert records[0]['in_drop_time_min']==22. and records[0]['score'].late_count==0
    assert records[1]['in_drop_time_min']==2. and records[1]['score'].late_count==3
    middle=next(r for r in records if 2.<r['in_drop_time_min']<=6.5)
    assert middle['in_drop_time_min']==6. and middle['score'].late_count==1
    assert middle['delivery_times'][3]==1. and middle['delivery_times'][4]==2.
    assert middle['delivery_times'][5]==18.  # Actual compensation is present, not dropped from clock.
    assert middle['source_bound_position_coverage'][0]['best_geometry_retained']
    assert all(not r['optimistic_missing_comp'] for r in records)
    for r in records:assert_oracle(p,r)


def test_negative_or_truncated_window_bounds_do_not_delete_physical_source_candidates():
    p=fixture()
    records=costs.source_options(p,(3,-3),0,1,9,position_limit=1,option_limit=3,
        latest_drop_bounds=(-1.,2.,3.,4.),**args())
    assert records and records[0]['latest_drop_bounds']==(-1.,2.,3.,4.)
    first=records[0]['source_bound_position_coverage'][0]
    assert first['legal_positions_before_bound']==0 and not first['omitted_due_position_limit']
    assert any(c['omitted_due_position_limit'] for c in records[0]['source_bound_position_coverage'][1:])
    with pytest.raises(ValueError,match='four finite'):
        costs.source_options(p,(),0,1,9,latest_drop_bounds=tuple(range(5)),**args())


def test_DIRECT_retention_mask_separates_pooled_rescue_from_compensation_rescue():
    p=fixture();original={1:30.,2:30.,3:30.,5:30.}
    records=costs.direct_pair_options(p,(),2,(3,5),option_limit=3,original_delivery_times=original,
        rescue_task_ids=(1,2),**args())
    assert any(r['selected_rescued_task_ids'] for r in records)
    assert all(r['retention_rescue_task_ids']==[1,2] and not r['retention_rescued_task_ids'] for r in records)
    # The mask changes a retention channel, never physics or true score.
    for r in records:assert_oracle(p,r,selected_pair=(3,5))


def test_bound_omission_statistics_survive_without_retaining_all_route_records():
    p=fixture();stats={}
    costs.source_options(p,(3,-3),0,1,9,position_limit=1,option_limit=3,
        latest_drop_bounds=(-1.,2.,3.,4.),optimistic_missing_comp=False,statistics=stats,**args())
    assert stats['source_latest_bound_checks']==4 and stats['source_latest_bound_position_omissions']>0
    assert len(stats['source_complete_bound_position_examples'])==1
    assert stats['source_complete_bound_position_examples'][0]['coverage'][0]['bound_min']==-1.
