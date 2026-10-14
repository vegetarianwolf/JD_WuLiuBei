"""Independent two-gap early prices, including the entire carry interval."""
from copy import deepcopy
from math import hypot

import pytest

from algorithm.experiments import audit_split_feeder_v3 as audit
from algorithm.experiments.audit_deadline_window_repooling import _window,_preview_scan
from algorithm.experiments.audit_weighted_relay_ablation import _geometry,reference_schedule
from algorithm.tests.test_audit_split_feeder import fixture as original_fixture,move,family_with_witness,W,S


def fixture(*,g=1,h=2,suffix=True):
    data=list(original_fixture(suffix=suffix))
    data[0][1]=(*data[0][1][:2],31.)
    data[0][2]=(*data[0][2][:2],32.)
    base=data[2];routes=[list(r) for r in data[3]]
    routes[0]=list(base[0][:g])+[101]+list(base[0][g:h])+[-101]+list(base[0][h:])
    data[3]=tuple(tuple(r) for r in routes)
    return data


def physical(record):
    return {k:record[k] for k in ('late_count','total_lateness_min','distance_km')}


def key(score):
    values=[score[k] for k in ('late_count','total_lateness_min','distance_km')]
    return [sum(w*v/s for w,v,s in zip(W,values,S)),*values]


def evidence(data,*,g=1,h=2):
    """Format actual independent replay; the tests assert hand-derived cases."""
    tasks,topology,base,routes,family=data
    before=reference_schedule(tasks,base,topology)
    proof=audit.frozen.audit_split_feeder_proposal(*data[:3],routes,move(data,routes),relay_enabled=True)
    ref=proof['core_reference'];sources=[]
    homes,stations,registry,_,capacity,_,speed=_geometry(tasks,topology)
    for task,owner,left,right in ((1,0,g,h),(2,1,2,2)):
        old=before['route_records'][owner];new=ref['route_records'][owner]
        delta=new['distance_km']-old['distance_km']
        # Use actual independent old/new event clocks for the middle delay,
        # rather than importing or reproducing the pricing primitive.
        first=(ref['event_times_min'][owner][left+1]-before['event_times_min'][owner][left])*speed if left<right else delta
        sources.append(dict(task_id=task,aircraft=owner,gap=left,drop_gap=right,
            original_pickup_gap=left,original_drop_gap=right,
            layout_type='contiguous' if left==right else 'noncontiguous',
            in_drop_time_min=proof['source_drop_availability_min'][str(task)],
            delta_distance_km=delta,native_shift_min=delta/speed,
            pickup_detour_distance_km=first,drop_detour_distance_km=delta-first,
            middle_native_shift_min=first/speed,tail_native_shift_min=delta/speed,
            native_score=physical(new),float_route_replay_used=False))
    skeleton=[v for v in base[2] if abs(v) not in (1,2)]
    prev=homes[2];D=0.
    for v in skeleton:
        point=tasks[abs(v)][0 if v>0 else 1];D+=hypot(point[0]-prev[0],point[1]-prev[1]);prev=point
    native=[t for t in (7,) if t in tasks];clock=ref['delivery_times_min']
    late=[clock[str(t)]-tasks[t][2] for t in native if clock[str(t)]-tasks[t][2]>1e-9]
    receiver_score=dict(late_count=len(late),total_lateness_min=float(sum(late)),distance_km=ref['route_records'][2]['distance_km'])
    hand=proof['handoff'];delta=receiver_score['distance_km']-D
    return dict(pricing_scope=audit.PRICING_SCOPE,source_layouts=sources,
        receiver_layout=dict(gap=0,delivery_order_task_ids=[1,2],receiver_arrival_min=hand['receiver_arrival_min'],
            second_take_min=hand['second_take_min'],waiting_min=hand['receiver_waiting_min'],
            delta_distance_km=delta,native_shift_min=delta/speed+hand['receiver_waiting_min'],
            native_score=receiver_score,float_route_replay_used=False),
        full_score=proof['score'],key=key(proof['score']),selected_delivery_times_min={str(t):clock[str(t)] for t in (1,2)},
        pooled_rescued_task_ids=proof['pooled_rescued_task_ids'],pooled_newly_late_task_ids=proof['pooled_newly_late_task_ids'],
        priced_seed_routes=[list(r) for r in routes],event_times_min=proof['event_times_min'],
        delivery_times_min=clock,actual_final_owners=proof['actual_final_owners'],
        source_drop_availability_min=proof['source_drop_availability_min'],handoff=hand,
        complete_RELAY_model=True,canonical_full_checked=False,DIRECT_candidate_certificate=False,
        official_lateness_epsilon_min=1e-9)


def family(data,*,g=1,h=2):
    f=family_with_witness(data);tasks,topology,base,routes,_=data
    proof=audit.frozen.audit_split_feeder_proposal(*data[:3],routes,move(data,routes),relay_enabled=True)
    homes,stations,registry,_,capacity,_,speed=_geometry(tasks,topology)
    for task,owner,identifier in ((1,0,101),(2,1,103)):
        row=f['source_tables'][f'{task}:{owner}:9'][0];route=routes[owner]
        times,deliveries,loads=_preview_scan(tasks,stations,registry,route,homes[owner],capacity,speed,{})
        row.update(route=list(route),score=physical(proof['core_reference']['route_records'][owner]),
            key=key(physical(proof['core_reference']['route_records'][owner])),event_times_min=times,
            delivery_times_min=deliveries,source_drop_availability_min={str(task):times[route.index(-identifier)]},
            in_drop_time_min=times[route.index(-identifier)],in_drop_position=route.index(-identifier),
            pickup_position=route.index(identifier),load_before=[0,*loads[:-1]],load_after=loads,
            latest_drop_bounds=[],source_bound_position_coverage=[])
    witness=f['relay_witnesses'][0]
    for name in ('event_times_min','delivery_times_min','actual_final_owners','source_drop_availability_min',
                 'shared_loaded_distance_km','pooled_rescued_task_ids','pooled_newly_late_task_ids'):
        witness[name]=proof[name]
    witness.update(routes=[list(r) for r in routes],table_score=proof['score'],key=key(proof['score']),
        window=_window(tasks,homes,stations,registry,speed,routes,proof['core_reference'],reference_schedule(tasks,base,topology),
            i=1,j=2,c=2,station=9,availability=proof['source_drop_availability_min'],sigma=0.,required=[1,2]))
    f.update(priority=key(proof['score']),early_pricing_evidence=evidence(data,g=g,h=h))
    f['early_rescue_pricing_evidence']=deepcopy(f['early_pricing_evidence'])
    f['early_priority']=list(f['early_pricing_evidence']['key'])
    f['source_surface_scope']='family_specific_cap_including_required_early_sources'
    f['seed_retention']=[dict(channel=c,source_seed_routes_retained=True,early_seed_full_scored=True,
        early_price_model_replayed=True,early_seed_entered_candidate_pool=True,witness_seed_route_retained=True,
        retained_or_omitted_by_better_complete_channels='retained_exact_layout',
        early_key=list(f['priority']),best_retained_key=list(f['priority'])) for c in ('weighted','pooled')]
    return f


def price(data,*,g=1,h=2,saved=None):
    return audit.audit_split_feeder_early_price(*data[:3],data[4],evidence(data,g=g,h=h) if saved is None else saved,weights=W,scales=S)


def test_carry_native_already_held_then_drop_can_avoid_contiguous_native_damage():
    data=fixture();proof=price(data);source=proof['source_layout_references'][0]
    assert source['native_delivery_zones']==dict(prefix=[],middle=[5],suffix=[3])
    assert source['actual_loads_through_carry_interval']==[2,1]
    assert source['pickup_detour_distance_km']==source['drop_detour_distance_km']==0.
    assert source['native_score']==dict(late_count=0,total_lateness_min=0.,distance_km=40.)
    assert source['event_times_min']==[0.,0.,10.,30.,30.,40.]
    assert proof['selected_delivery_times_min']=={'1':31.,'2':32.}
    assert proof['full_score']==dict(late_count=0,total_lateness_min=0.,distance_km=88.)
    assert proof['complete_two_gap_RELAY_entity_physics_passed']
    contiguous=price(fixture(g=1,h=1),g=1,h=1)
    assert contiguous['source_layout_references'][0]['native_score']['total_lateness_min']==15.
    assert not proof['DIRECT_candidate_certificate'] and not proof['complete_solution']


def test_two_detours_distinct_middle_and_suffix_shifts_and_true_ready():
    data=fixture();data[0][1]=((-5.,0.),data[0][1][1],31.)
    data[1]['stations'][0]=[9,1.,0.]
    proof=price(data);source=proof['source_layout_references'][0]
    assert source['pickup_detour_distance_km']==10. and source['drop_detour_distance_km']==2.
    assert source['middle_native_shift_min']==10. and source['tail_native_shift_min']==12.
    assert source['event_times_min']==[0.,5.,20.,41.,42.,52.]
    assert source['native_score']==dict(late_count=1,total_lateness_min=5.,distance_km=52.)
    assert source['in_drop_time_min']==41. and source['final_delivery_count']==2


@pytest.mark.parametrize('g,h',[(0,1),(0,4),(4,4),(1,1)])
def test_empty_middle_deliveries_open_tail_and_contiguous_edge_are_distinct(g,h):
    data=fixture(g=g,h=h);proof=price(data,g=g,h=h);src=proof['source_layout_references'][0]
    if (g,h)==(0,1):
        assert src['native_delivery_zones']['middle']==[]
        assert src['pickup_detour_distance_km']==0. and src['drop_detour_distance_km']==20.
    if h==4:
        assert src['native_delivery_zones']['suffix']==[] and src['in_drop_time_min']==50.
        assert src['native_score']['distance_km']==50. and src['native_score']['late_count']==0
    if g==h:
        assert src['native_delivery_zones']['middle']==[] and src['drop_detour_distance_km']==0.
    assert src['transport_leg_count']==src['final_delivery_count']+1


def test_full_K_and_very_late_IN_do_not_count_as_native_final_lateness():
    data=fixture(suffix=False);data[0][1]=(*data[0][1][:2],0.)
    source=price(data)['source_layout_references'][0]
    assert data[1]['max_tasks_per_drone']==2
    assert source['final_delivery_count']==2 and source['transport_leg_count']==3
    assert source['in_drop_time_min']==30. and source['native_score']['late_count']==0


def test_tail_drop_detour_adds_distance_and_drop_time_but_no_native_tail_delay():
    data=fixture(g=1,h=4);data[0][1]=((-5.,0.),data[0][1][1],31.)
    source=price(data,g=1,h=4)['source_layout_references'][0]
    assert source['pickup_detour_distance_km']==source['drop_detour_distance_km']==10.
    assert source['middle_native_shift_min']==10. and source['tail_native_shift_min']==20.
    assert source['native_delivery_zones']==dict(prefix=[],middle=[5,3],suffix=[])
    assert source['native_delivery_times_min']=={'3':50.,'5':20.}
    assert source['native_score']==dict(late_count=1,total_lateness_min=5.,distance_km=60.)
    assert source['in_drop_time_min']==60.


def test_clock_error_path_metadata_is_only_finite_declared_allowance_not_score_proof():
    data=fixture();saved=evidence(data)
    saved['source_layouts'][0]['timing_error_bound_min']=float('inf')
    with pytest.raises(ValueError,match='error allowance'):price(data,saved=saved)
    saved['source_layouts'][0]['timing_error_bound_min']=0.
    assert price(data,saved=saved)['passed']


def test_whole_interval_load_not_only_safe_endpoints_is_required():
    data=fixture();saved=evidence(data);base=list(data[2]);base[0]=(5,3,-5,-3);data[2]=tuple(base)
    saved['source_layouts'][0].update(gap=0,drop_gap=4,original_pickup_gap=0,original_drop_gap=4)
    assert [0,1,2,1,0][0]==[0,1,2,1,0][-1]==0
    with pytest.raises(ValueError,match='carry interval.*load2'):price(data,saved=saved)


@pytest.mark.parametrize('field,value',[
    ('gap',True),('drop_gap',0),('original_pickup_gap',0),('original_drop_gap',1),
    ('layout_type','contiguous'),('pickup_detour_distance_km',4.),('drop_detour_distance_km',4.),
    ('middle_native_shift_min',1.),('tail_native_shift_min',1.),('native_shift_min',1.),
    ('in_drop_time_min',10.),('delta_distance_km',20.),('middle_native_delivery_task_ids',[3]),
    ('pickup_position',0),('in_drop_position',2)])
def test_false_gap_alias_detour_zone_price_or_drop_cannot_pass(field,value):
    data=fixture();saved=evidence(data);saved['source_layouts'][0][field]=value
    with pytest.raises(ValueError):price(data,saved=saved)


@pytest.mark.parametrize('kind',['native_N','native_L','native_clock','full_D','selected_clock','C_wait','priced_routes'])
def test_native_selected_and_entire_fleet_physics_still_bound(kind):
    data=fixture();saved=evidence(data)
    if kind=='native_N':saved['source_layouts'][0]['native_score']['late_count']=1
    elif kind=='native_L':saved['source_layouts'][0]['native_score']['total_lateness_min']=1.
    elif kind=='native_clock':saved['event_times_min'][0][2]=0.
    elif kind=='full_D':saved['full_score']['distance_km']-=10.
    elif kind=='selected_clock':saved['selected_delivery_times_min']['1']=17.
    elif kind=='C_wait':saved['receiver_layout']['waiting_min']=0.
    else:saved['priced_seed_routes'][0]=list(data[2][0])
    with pytest.raises(ValueError):price(data,saved=saved)


def test_official_EPS_uses_actual_native_clock_in_middle_and_suffix():
    data=fixture();data[0][5]=(*data[0][5][:2],10.-2e-9)
    proof=price(data);assert proof['source_layout_references'][0]['native_score']['late_count']==1
    assert proof['source_layout_references'][0]['native_score']['total_lateness_min']>1e-9
    saved=evidence(data);saved['source_layouts'][0]['native_score']['late_count']=0
    with pytest.raises(ValueError):price(data,saved=saved)


def test_both_early_family_channels_and_real_source_membership_and_DIRECT_scope():
    data=fixture();f=family(data)
    result=audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)
    assert result['audit_version']=='split_feeder_two_gap_native_price_v3'
    assert result['early_pricing_reference']['actual_source_seed_routes_retained']==[True,True]
    assert all(x['early_seed_full_scored'] for x in result['seed_retention_references'])
    topology={**data[1],'relay_allowed':False}
    assert audit.audit_split_feeder_family(data[0],topology,data[2],f,weights=W,scales=S)['passed']
    with pytest.raises(ValueError):
        audit.audit_split_feeder_proposal(data[0],topology,data[2],data[3],move(data,data[3],'DIRECT_SAME_OWNER'),relay_enabled=False)
    f['early_rescue_pricing_evidence']['source_layouts'][0]['drop_gap']=1
    with pytest.raises(ValueError):audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)


def test_cap3_axes_are_available_and_seed_retention_never_relabels_absent_source():
    data=fixture();f=family(data);f['seed_retention'][1]['early_seed_entered_candidate_pool']=False
    with pytest.raises(ValueError):audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)
    f=family(data);f['source_tables']['1:0:9'][0]['route']=[5,101,-101,-5,3,-3]
    with pytest.raises(ValueError):audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)


@pytest.mark.parametrize('field,value',[('original_pickup_gap',0),('original_drop_gap',1),('source_layout_type','contiguous')])
def test_optional_source_table_gap_aliases_describe_the_actual_validated_route(field,value):
    data=fixture();f=family(data);f['source_tables']['1:0:9'][0][field]=value
    with pytest.raises(ValueError,match='source gap alias'):
        audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)
