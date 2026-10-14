"""Separated receiver deliveries: actual waiting and native bands, no search."""
from copy import deepcopy
from math import hypot

import pytest

from algorithm.experiments import audit_split_feeder_v4 as audit
from algorithm.tests import test_audit_split_feeder_v3 as source
from algorithm.tests.test_audit_split_feeder import move,W,S,snapshot
from algorithm.experiments.audit_weighted_relay_ablation import reference_schedule,_geometry


def fixture(*,g=0,h=2,source_h=2):
    data=source.fixture(g=1,h=source_h)
    data[0][2]=(data[0][2][0],(0.,5.),36.)
    data[0][7]=(*data[0][7][:2],38.)
    native=[v for v in data[2][2] if abs(v) not in (1,2)]
    routes=list(data[3]);routes[2]=tuple(native[:g]+[102,104,-102]+native[g:h]+[-104]+native[h:])
    data[3]=tuple(routes)
    return data


def evidence(data,*,g=0,h=2,source_h=2):
    saved=source.evidence(data,g=1,h=source_h)
    saved['pricing_scope']=audit.PRICING_SCOPE
    proof=audit.frozen.audit_split_feeder_proposal(*data[:3],data[3],move(data,data[3]),relay_enabled=True)
    tasks,topology,base,routes,_=data;homes,stations,registry,_,capacity,_,V=_geometry(tasks,topology)
    skeleton=[v for v in base[2] if abs(v) not in (1,2)]
    before_clock,_,_=audit._preview_scan(tasks,stations,registry,skeleton,homes[2],capacity,V,{})
    previous=homes[2];D=0.
    for v in skeleton:
        point=tasks[abs(v)][0 if v>0 else 1];D+=hypot(point[0]-previous[0],point[1]-previous[1]);previous=point
    now=proof['core_reference']['route_records'][2];delta=now['distance_km']-D;W_=proof['handoff']['receiver_waiting_min']
    # Derive the saved middle value from an independent actual event clock.
    # Hand-derived assertions below distinguish actual geometry and waiting.
    head=(proof['event_times_min'][2][g+3]-before_clock[g]-W_)*V if g<h else delta
    saved['receiver_layout'].update(gap=g,delivery_gap=h,original_pickup_gap=g,original_delivery_gap=h,
        layout_type='contiguous' if g==h else 'separated',take_positions=[g,g+1],delivery_positions=[g+2,h+3],
        head_detour_distance_km=head,tail_detour_distance_km=delta-head,
        middle_native_shift_min=head/V+W_,tail_native_shift_min=delta/V+W_,native_shift_min=delta/V+W_,
        take_times_min=proof['handoff']['take_times_min'],first_take_min=proof['handoff']['take_times_min'][0],
        selected_delivery_offsets_min={str(t):proof['delivery_times_min'][str(t)]-proof['handoff']['second_take_min'] for t in (1,2)})
    return saved


def family(data,*,g=0,h=2,source_h=2):
    f=source.family(data,g=1,h=source_h)
    f['early_pricing_evidence']=evidence(data,g=g,h=h,source_h=source_h)
    f['early_rescue_pricing_evidence']=deepcopy(f['early_pricing_evidence'])
    return f


def price(data,*,g=0,h=2,source_h=2,saved=None):
    return audit.audit_split_feeder_early_price(*data[:3],data[4],
        evidence(data,g=g,h=h,source_h=source_h) if saved is None else saved,weights=W,scales=S)


def test_separated_native_delivery_improves_real_full_price_without_omitting_second_D():
    data=fixture();p=price(data);receiver=p['receiver_layout_reference']
    assert receiver['head_detour_distance_km']==0. and receiver['tail_detour_distance_km']==1.
    assert receiver['middle_native_shift_min']==30. and receiver['tail_native_shift_min']==31.
    assert receiver['native_delivery_zones']==dict(prefix=[],middle=[7],suffix=[])
    assert p['event_times_min'][2]==[30.,30.,31.,32.,34.,35.]
    assert p['selected_delivery_times_min']=={'1':31.,'2':35.}
    assert receiver['native_score']==dict(late_count=0,total_lateness_min=0.,distance_km=5.)
    assert p['full_score']==dict(late_count=0,total_lateness_min=0.,distance_km=89.)
    old=price(fixture(g=0,h=0),g=0,h=0)
    assert old['receiver_layout_reference']['native_score']==dict(late_count=1,total_lateness_min=2.,distance_km=10.)
    assert old['full_score']==dict(late_count=1,total_lateness_min=2.,distance_km=94.)


@pytest.mark.parametrize('g,h',[(0,0),(0,1),(0,2),(2,2)])
def test_contiguous_and_open_tail_and_native_held_at_second_delivery(g,h):
    data=fixture(g=g,h=h);p=price(data,g=g,h=h);r=p['receiver_layout_reference']
    assert r['take_positions']==[g,g+1] and r['delivery_positions']==[g+2,h+3]
    if g==h:assert r['tail_detour_distance_km']==0. and r['native_delivery_zones']['middle']==[]
    if h==2:assert r['native_delivery_zones']['suffix']==[]
    if (g,h)==(0,1):
        assert r['actual_loads_through_carry_interval']==[1,2]
        assert r['native_delivery_zones']==dict(prefix=[],middle=[],suffix=[7])
        assert p['event_times_min'][2]==[30.,30.,31.,32.,35.,36.]


def test_real_two_stage_wait_is_charged_once_to_both_delay_bands():
    data=fixture(source_h=1);p=price(data,source_h=1);r=p['receiver_layout_reference']
    assert p['handoff']['source_availability_min']=={'1':10.,'2':14.}
    assert r['take_times_min']==[10.,14.] and r['waiting_min']==14.
    assert r['middle_native_shift_min']==14. and r['tail_native_shift_min']==15.
    assert p['event_times_min'][2]==[10.,14.,15.,16.,18.,19.]
    saved=evidence(data,source_h=1);saved['receiver_layout']['tail_native_shift_min']+=14.
    with pytest.raises(ValueError):price(data,source_h=1,saved=saved)


def test_receiver_full_carry_interval_including_h_is_checked_not_only_endpoints():
    data=fixture();saved=evidence(data)
    data[0][10]=((0.,3.),(0.,6.),100.)
    base=list(data[2]);base[2]=(1,2,-1,-2,7,10,-7,-10);data[2]=tuple(base)
    data[1]['max_tasks_per_drone']=4
    for h in (2,4):
        row=saved['receiver_layout'];row.update(delivery_gap=h,original_delivery_gap=h,delivery_positions=[2,h+3])
        with pytest.raises(ValueError,match='receiver carry interval.*load2'):price(data,saved=saved)


def test_doubletake_must_start_empty_even_if_first_D_immediately_releases_one():
    data=fixture();saved=evidence(data);saved['receiver_layout'].update(gap=1,delivery_gap=2,
        original_pickup_gap=1,take_positions=[1,2],delivery_positions=[3,5])
    with pytest.raises(ValueError,match='actually be empty'):price(data,saved=saved)


def test_actual_second_delivery_uses_native_flight_chain_not_original_direct_offset():
    data=fixture();data[0][7]=((2.,2.),(2.,4.),38.)
    p=price(data);offset=p['selected_delivery_times_min']['2']-p['handoff']['second_take_min']
    assert offset==pytest.approx(3.+2.*5.**.5)
    assert offset>5.  # The old immediate Dfirst->Dsecond path would be 5.
    saved=evidence(data);saved['receiver_layout']['selected_delivery_offsets_min']['2']=5.
    with pytest.raises(ValueError,match='actual selected delivery offsets'):price(data,saved=saved)


@pytest.mark.parametrize('field,value',[
    ('delivery_gap',-1),('original_pickup_gap',1),('original_delivery_gap',1),('layout_type','contiguous'),
    ('take_positions',[1,2]),('delivery_positions',[2,4]),('head_detour_distance_km',2.),
    ('tail_detour_distance_km',2.),('middle_native_shift_min',31.),('tail_native_shift_min',61.),
    ('native_shift_min',61.),('second_take_min',14.),('waiting_min',0.),('take_times_min',[0.,30.]),
    ('native_score',dict(late_count=1,total_lateness_min=1.,distance_km=5.)),
    ('middle_native_delivery_task_ids',[]),('float_route_replay_used','false')])
def test_forged_C_layout_zone_wait_price_or_current_clock_is_rejected(field,value):
    data=fixture();saved=evidence(data);saved['receiver_layout'][field]=value
    with pytest.raises(ValueError):price(data,saved=saved)


def test_selected_only_EPS_and_omitted_native_damage_cannot_hide_in_full_score():
    data=fixture(source_h=1);data[0][2]=(*data[0][2][:2],19.-2e-9)
    p=price(data,source_h=1);assert p['selected_delivery_times_min']['2']==19.
    assert p['full_score']['late_count']==2  # native5 plus the second selected task
    saved=evidence(data,source_h=1);saved['full_score']['late_count']=1
    with pytest.raises(ValueError):price(data,source_h=1,saved=saved)


def test_shared_family_physical_proof_supports_native_between_D_and_still_binds_both_seeds():
    data=fixture();f=family(data)
    p=audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)
    assert p['audit_version']=='split_feeder_two_gap_IN_separated_OUT_native_price_v4'
    assert p['witness_references'][0]['event_times_min'][2]==[30.,30.,31.,32.,34.,35.]
    assert p['early_pricing_reference']['actual_witness_seed_route_retained']
    direct={**data[1],'relay_allowed':False}
    assert audit.audit_split_feeder_family(data[0],direct,data[2],f,weights=W,scales=S)['passed']
    f['seed_retention'][1]['early_seed_entered_candidate_pool']=False
    with pytest.raises(ValueError):audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)


def test_separated_receiver_seed_pool_claim_binds_its_actual_source_backed_full_layout():
    data=fixture();f=family(data)
    f['seed_retention'][0]['receiver_seed_layout_full_scored']=True
    assert audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)['passed']
    f['seed_retention'][0]['receiver_seed_layout_full_scored']=False
    with pytest.raises(ValueError,match='actual receiver layout candidate opportunity'):
        audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)


def test_generic_complete_proposal_and_window_replay_do_not_lock_old_four_event_block():
    data=fixture();saved=snapshot(data,data[3]);proof=audit.audit_split_feeder_proposal(
        *data[:3],data[3],saved['move'],relay_enabled=True,telemetry=saved,weights=W,scales=S)
    assert proof['shared_loaded_distance_km']==1. and proof['delivery_times_min']['7']==34.
    saved['handoff']['suffix_delivery_offsets_min']['2']=2.
    with pytest.raises(ValueError):audit.audit_split_feeder_proposal(
        *data[:3],data[3],saved['move'],relay_enabled=True,telemetry=saved,weights=W,scales=S)
