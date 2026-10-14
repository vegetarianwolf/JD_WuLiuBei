"""Saved early full-native prices are proved without importing price code."""
from copy import deepcopy
import pytest

from algorithm.experiments import audit_split_feeder_v2 as audit
from algorithm.tests.test_audit_split_feeder import fixture,family_with_witness,move,snapshot,W,S


def evidence(data):
    """A native-loaded source gap1, source gap2 and empty C skeleton gap0."""
    tasks,topology,base,routes,_=data;proof=audit.frozen.audit_split_feeder_proposal(
        *data[:3],routes,move(data,routes),relay_enabled=True)
    score=proof['score'];values=[score[k] for k in ('late_count','total_lateness_min','distance_km')]
    return dict(pricing_scope=audit.PRICING_SCOPE,
        source_layouts=[dict(task_id=1,aircraft=0,gap=1,in_drop_time_min=10.,delta_distance_km=20.,
            native_shift_min=20.,native_score=dict(late_count=1,total_lateness_min=15.,distance_km=60.),float_route_replay_used=False),
            dict(task_id=2,aircraft=1,gap=2,in_drop_time_min=14.,delta_distance_km=0.,
            native_shift_min=0.,native_score=dict(late_count=0,total_lateness_min=0.,distance_km=28.),float_route_replay_used=False)],
        receiver_layout=dict(gap=0,delivery_order_task_ids=[1,2],receiver_arrival_min=0.,second_take_min=14.,
            waiting_min=14.,delta_distance_km=0.,native_shift_min=14.,
            native_score=dict(late_count=0,total_lateness_min=0.,distance_km=4.),float_route_replay_used=False),
        full_score=deepcopy(score),key=[sum(w*v/s for w,v,s in zip(W,values,S)),*values],
        selected_delivery_times_min={'1':15.,'2':16.},pooled_rescued_task_ids=proof['pooled_rescued_task_ids'],
        pooled_newly_late_task_ids=proof['pooled_newly_late_task_ids'],
        complete_RELAY_model=True,canonical_full_checked=False,DIRECT_candidate_certificate=False,
        official_lateness_epsilon_min=1e-9,routes=[list(r) for r in routes],priced_seed_routes=[list(r) for r in routes],
        event_times_min=proof['event_times_min'],delivery_times_min=proof['delivery_times_min'],
        actual_final_owners=proof['actual_final_owners'],source_drop_availability_min=proof['source_drop_availability_min'],handoff=proof['handoff'])


def family(data):
    f=family_with_witness(data);f['early_pricing_evidence']=evidence(data)
    f['early_rescue_pricing_evidence']=deepcopy(f['early_pricing_evidence'])
    f['early_priority']=list(f['early_pricing_evidence']['key'])
    f['source_surface_scope']='family_specific_cap_including_required_early_sources'
    f['seed_retention']=[dict(channel=c,source_seed_routes_retained=True,early_seed_full_scored=True,
        early_price_model_replayed=True,early_seed_entered_candidate_pool=True,
        witness_seed_route_retained=True,retained_or_omitted_by_better_complete_channels='retained_exact_layout',
        early_key=list(f['early_pricing_evidence']['key']),best_retained_key=list(f['priority'])) for c in ('weighted','pooled')]
    return f


def price(data,saved=None):
    return audit.audit_split_feeder_early_price(*data[:3],data[4],evidence(data) if saved is None else saved,weights=W,scales=S)


def test_complete_price_reconstructs_actual_native_delay_wait_and_all200_equivalent_tasks():
    data=fixture();proof=price(data)
    assert proof['passed'] and proof['complete_contiguous_RELAY_entity_physics_passed']
    assert proof['full_score']==dict(late_count=1,total_lateness_min=15.,distance_km=108.)
    assert proof['source_layout_references'][0]['native_score']['late_count']==1
    assert proof['receiver_skeleton']['route']==[7,-7]
    assert proof['receiver_skeleton']['event_times_min']==[2.,4.]
    assert proof['receiver_layout_reference']['native_shift_min']==14.
    assert proof['event_times_min'][0]==[0.,0.,10.,30.,50.,60.]
    assert proof['event_times_min'][2]==[10.,14.,15.,16.,16.,18.]
    assert proof['pooled_rescued_task_ids']==[1,2]
    assert proof['all_classifications']['native_newly_late_task_ids']==[5]
    assert not proof['complete_solution'] and not proof['DIRECT_candidate_certificate']


def test_full_family_keeps_old_witness_proof_and_adds_true_early_price_for_both_arms():
    data=fixture();f=family(data)
    proof=audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)
    assert proof['witness_references'][0]['score']==proof['early_pricing_reference']['full_score']
    assert proof['weighted_keys_checked'] and proof['audit_version']=='split_feeder_early_native_price_v2'
    topology={**data[1],'relay_allowed':False}
    assert audit.audit_split_feeder_family(data[0],topology,data[2],f,weights=W,scales=S)['early_pricing_reference']['passed']
    invalid=move(data,data[3],'DIRECT_SAME_OWNER')
    with pytest.raises(ValueError):
        audit.audit_split_feeder_proposal(data[0],topology,data[2],data[3],invalid,relay_enabled=False)


@pytest.mark.parametrize('tamper',['source_native','source_gap','source_shift','receiver_native','receiver_wait',
    'receiver_T','receiver_gap','omit_native_damage','source_D','full_D','selected_clock','rescued','rank_scope',
    'DIRECT_certificate','primitive_clock','epsilon','priced_routes','floatflag'])
def test_no_false_early_price_native_loss_or_window_can_hide_behind_math_flags(tamper):
    data=fixture();saved=evidence(data);f=data[4]
    if tamper=='source_native':saved['source_layouts'][0]['native_score']['late_count']=0
    elif tamper=='source_gap':saved['source_layouts'][0]['gap']=4
    elif tamper=='source_shift':saved['source_layouts'][0]['native_shift_min']=0.
    elif tamper=='receiver_native':saved['receiver_layout']['native_score']['total_lateness_min']=20.
    elif tamper=='receiver_wait':saved['receiver_layout']['waiting_min']=0.
    elif tamper=='receiver_T':saved['receiver_layout']['second_take_min']=0.
    elif tamper=='receiver_gap':saved['receiver_layout']['gap']=1
    elif tamper=='omit_native_damage':saved['full_score']['late_count']=0;saved['full_score']['total_lateness_min']=0.
    elif tamper=='source_D':saved['source_layouts'][0]['delta_distance_km']=0.
    elif tamper=='full_D':saved['full_score']['distance_km']-=20.
    elif tamper=='selected_clock':saved['selected_delivery_times_min']['1']=10.
    elif tamper=='rescued':saved['pooled_rescued_task_ids']=[]
    elif tamper=='rank_scope':saved['pricing_scope']='DIRECT_complete_candidate'
    elif tamper=='DIRECT_certificate':saved['DIRECT_candidate_certificate']=True
    elif tamper=='primitive_clock':saved['event_times_min'][0][3]=10.
    elif tamper=='priced_routes':saved['priced_seed_routes'][0]=list(data[2][0])
    elif tamper=='floatflag':saved['source_layouts'][0]['float_route_replay_used']='false'
    else:saved['official_lateness_epsilon_min']=1.
    with pytest.raises(ValueError):audit.audit_split_feeder_early_price(*data[:3],f,saved,weights=W,scales=S)


def test_C_native_price_excludes_selected_lateness_but_full_price_includes_it():
    data=fixture();data[0][1]=(data[0][1][0],data[0][1][1],14.)
    saved=evidence(data);proof=price(data,saved)
    assert proof['full_score']['late_count']==2 and proof['full_score']['total_lateness_min']==16.
    assert proof['receiver_layout_reference']['native_score']['late_count']==0
    saved['receiver_layout']['native_score']['late_count']=1;saved['receiver_layout']['native_score']['total_lateness_min']=1.
    with pytest.raises(ValueError):price(data,saved)


def test_official_EPS_is_independent_of_saved_safe_channels():
    data=fixture();data[0][1]=(data[0][1][0],data[0][1][1],15.-2e-9)
    saved=evidence(data);proof=price(data,saved)
    assert proof['full_score']['late_count']==2
    saved['full_score']['late_count']=1
    with pytest.raises(ValueError):price(data,saved)


def test_extra_old_witness_clock_still_fails_before_new_early_proof():
    data=fixture();f=family(data);f['relay_witnesses'][0]['event_times_min'][0][3]=0.
    with pytest.raises(ValueError):audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)


def test_secondary_seed_is_independently_priced_and_primary_priority_is_not_its_key():
    data=fixture();f=family(data);f['early_rescue_pricing_evidence']['source_layouts'][0]['native_score']['late_count']=0
    with pytest.raises(ValueError):audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)


def test_distinct_early_source_must_really_be_in_family_table_but_final_witness_can_omit_it():
    data=fixture();f=family(data);other=f['early_rescue_pricing_evidence']
    row=deepcopy(f['source_tables']['1:0:9'][0])
    row.update(route=[101,-101,5,-5,3,-3],pickup_position=0,in_drop_position=1,
        event_times_min=[0.,10.,20.,30.,50.,60.],load_before=[0,1,0,1,0,1],load_after=[1,0,1,0,1,0])
    other['source_layouts'][0]['gap']=0
    other['priced_seed_routes'][0]=list(row['route']);other['routes'][0]=list(row['route'])
    other['event_times_min'][0]=list(row['event_times_min'])
    # The route is not yet available to the full candidate pool: claiming
    # scored/retained opportunity must be rejected, despite its correct price.
    with pytest.raises(ValueError,match='early seed opportunity'):
        audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)
    f['source_tables']['1:0:9'].append(row)
    f['seed_retention'][1].update(witness_seed_route_retained=False,
        retained_or_omitted_by_better_complete_channels='true_weighted_minN_pooled_representatives')
    proof=audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)
    assert proof['early_rescue_pricing_reference']['actual_source_seed_routes_retained']==[True,True]
    assert proof['early_rescue_pricing_reference']['actual_witness_seed_route_retained'] is False


def test_small_source_cap_omission_is_honest_but_is_not_claimed_full_pool_opportunity():
    data=fixture();f=family(data);other=f['early_rescue_pricing_evidence']
    other['source_layouts'][0]['gap']=0
    other['priced_seed_routes'][0]=[101,-101,5,-5,3,-3];other['routes'][0]=list(other['priced_seed_routes'][0])
    other['event_times_min'][0]=[0.,10.,20.,30.,50.,60.]
    f['seed_retention'][1].update(source_seed_routes_retained=False,early_seed_full_scored=False,
        witness_seed_route_retained=False,retained_or_omitted_by_better_complete_channels='source_cap_below_two_early_layouts',
        early_price_model_replayed=True,entered_candidate_pool=False,early_seed_entered_candidate_pool=False)
    result=audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)
    assert result['seed_retention_references'][1]['early_price_model_replayed']
    assert not result['seed_retention_references'][1]['entered_candidate_pool']
    f['seed_retention'][1]['early_seed_full_scored']=True
    with pytest.raises(ValueError):audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)


@pytest.mark.parametrize('tamper',['witness_bool','retained_key','source_scope','reason','pool_bool','model_bool'])
def test_retention_claims_are_bound_to_actual_family_routes_keys_and_scope(tamper):
    data=fixture();f=family(data)
    if tamper=='witness_bool':f['seed_retention'][0]['witness_seed_route_retained']=False
    elif tamper=='retained_key':f['seed_retention'][0]['best_retained_key'][0]-=1.
    elif tamper=='source_scope':f['source_surface_scope']='aggregate_union_can_ignore_family_cap'
    elif tamper=='pool_bool':f['seed_retention'][0]['early_seed_entered_candidate_pool']=False
    elif tamper=='model_bool':f['seed_retention'][0]['early_price_model_replayed']=False
    else:f['seed_retention'][0]['retained_or_omitted_by_better_complete_channels']='source_cap_below_two_early_layouts'
    with pytest.raises(ValueError):audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)


@pytest.mark.parametrize('role',['source','receiver'])
def test_roundoff_correction_is_not_claimed_without_physical_replay(role):
    data=fixture();saved=evidence(data)
    row=saved['source_layouts'][0] if role=='source' else saved['receiver_layout']
    row['geometry_roundoff_corrected']=True
    with pytest.raises(ValueError,match='physical route replay'):price(data,saved)
    row['float_route_replay_used']=True
    assert price(data,saved)['passed']


def test_tail_source_no_native_suffix_still_charges_real_completion_and_flight():
    data=fixture();saved=evidence(data)
    routes=[list(r) for r in data[3]];routes[0]=list(data[2][0])+[101,-101]
    proof=audit.frozen.audit_split_feeder_proposal(*data[:3],routes,move(data,routes),relay_enabled=True)
    saved['source_layouts'][0].update(gap=4,in_drop_time_min=50.,delta_distance_km=10.,native_shift_min=10.,
        native_score=dict(late_count=0,total_lateness_min=0.,distance_km=50.))
    saved['receiver_layout'].update(second_take_min=50.,waiting_min=50.,native_shift_min=50.)
    saved.update(priced_seed_routes=routes,routes=routes,full_score=proof['score'],
        key=[sum(w*proof['score'][k]/s for w,s,k in zip(W,S,('late_count','total_lateness_min','distance_km'))),
             *[proof['score'][k] for k in ('late_count','total_lateness_min','distance_km')]],
        selected_delivery_times_min={'1':51.,'2':52.},pooled_rescued_task_ids=[])
    for name in ('event_times_min','delivery_times_min','actual_final_owners','source_drop_availability_min','handoff'):
        saved[name]=proof[name]
    result=price(data,saved)
    assert result['full_score']==dict(late_count=2,total_lateness_min=70.,distance_km=98.)
    assert result['source_layout_references'][0]['native_score']['late_count']==0
    assert result['handoff']['second_take_min']==50.
    f=family(data);f['early_priority']=[0.,0,0.,0.]
    with pytest.raises(ValueError):audit.audit_split_feeder_family(*data[:3],f,weights=W,scales=S)


def test_full_proposal_wrapper_does_not_weaken_DIRECT_shared_or_deadline_guards():
    data=fixture();saved=snapshot(data,data[2],'DIRECT_SAME_OWNER');m=saved['move']
    expected=audit.frozen.audit_split_feeder_proposal(*data[:3],data[2],m,relay_enabled=False,telemetry=saved,weights=W,scales=S)
    assert audit.audit_split_feeder_proposal(*data[:3],data[2],m,relay_enabled=False,telemetry=saved,weights=W,scales=S)==expected
    saved.update(full_verified_at_perf_counter=10.,deadline_perf_counter=10.)
    with pytest.raises(ValueError):audit.audit_split_feeder_proposal(*data[:3],data[2],m,relay_enabled=False,telemetry=saved,weights=W,scales=S)
