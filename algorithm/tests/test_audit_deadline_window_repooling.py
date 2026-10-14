"""Independent actual-prefix/window/native-loss and twelve-map tiny cases."""
from copy import deepcopy
from itertools import permutations
import pytest
from algorithm.experiments.audit_deadline_window_repooling import audit_deadline_window_repooling_proposal
from algorithm.experiments.audit_repooling import _owners
from algorithm.experiments.audit_weighted_relay_ablation import _geometry,reference_schedule

W=(.5,.25,.25);S=(6.,100.,100.)


def fixture(*,native_loss=False,suffix=False):
    tasks={1:((-10.,0.),(0.,1.),17.),2:((14.,0.),(0.,2.),16.),
        3:((0.,0.),(-10.,0.),100.),4:((0.,0.),(14.,0.),100.),
        5:((-10.,0.),(-20.,0.),15. if native_loss else 100.),6:((14.,0.),(14.,0.),100.)}
    topology=dict(stations=[[9,0.,0.],[10,1.,0.]],
        legs=[dict(id=101+2*(t-1)+o,task_id=t,kind=k,relay_id=9)
            for t in (1,2,3) for o,k in ((0,'RELAY_IN'),(1,'RELAY_OUT'))],
        home_points=[[-10.,0.],[14.,0.],[0.,0.]],drone_count=3,capacity=2,
        max_tasks_per_drone=3 if suffix else 2,speed_km_per_min=1.)
    base=((5,-5,1,-1),(6,-6,2,-2),(3,4,-3,-4))
    relay=((5,101,-101,-5,3,-3),(6,-6,103,-103,4,-4),(102,104,-102,-104))
    if suffix:
        tasks[7]=((0.,2.),(0.,4.),100.)
        base=(*base[:2],base[2]+(7,-7));relay=(*relay[:2],relay[2]+(7,-7))
    family=dict(i=1,j=2,k1=3,k2=4,a=0,b=1,c=2,station_id=9,task_ids=[1,2,3,4],priority=[0.])
    offsets={'1':1.,'2':2.,'3':14.,'4':38.}
    if suffix:offsets['7']=40.+(200.**.5)
    previewwindow=dict(station_id=9,task_ids=[1,2],take_order_task_ids=[1,2],source_availability_min={'1':10.,'2':14.},
        receiver_arrival_min=0.,second_take_min=14.,selected_delivery_offsets_min={'1':1.,'2':2.},
        suffix_delivery_offsets_min=offsets,official_lateness_epsilon_min=1e-9,sigma_min=0.,
        required_on_time_task_ids=[1,2],latest_second_take_min_by_task={'1':16.+1e-9,'2':14.+1e-9},
        joint_latest_second_take_min=14.+1e-9,safe_window_satisfied=True,
        official_rescued_task_ids=[1],official_new_late_task_ids=[],safe_rescued_task_ids=[1],selected_safe_rescued_task_ids=[1],
        selected_rescued_task_ids=[1],selected_new_late_task_ids=[],native_rescued_task_ids=[],native_new_late_task_ids=[],
        classification_scope='all_deliveries_on_this_receiver_route',
        failure_scope='declared_safety_channel_only_not_global_J_or_ALNS_bound')
    source={'1':[5,101,-101,-5],'2':[6,-6,103,-103]}
    family['selector_window']=dict(source_preview_routes=source,receiver_gap_index=0,
        receiver_preview_route=[102,104,-102,-104,*base[2]],delivery_order_task_ids=[1,2],
        optimistic_missing_comp=True,receiver_quota_overflow=2,preview_complete_solution=False,window=previewwindow)
    move=dict(family=family,mode='RELAY',final_task_groups=[[3],[4],[1,2]],
        final_aircraft_by_task={'1':2,'2':2,'3':0,'4':1},
        actual_final_owners={'1':2,'2':2,'3':0,'4':1,'5':0,'6':1}|({'7':2} if suffix else {}))
    data=tasks,topology,base,relay,move
    preview_rank_fields(data)
    return data



def preview_rank_fields(data):
    tasks,topology,base,_,move=data;clocks={1:15.,2:16.,3:28.,4:52.,5:30.,6:0.}
    if 7 in tasks:clocks[7]=54.+200.**.5
    physical=reference_schedule(tasks,base,topology)['score']
    values=[clocks[t]-tasks[t][2] for t in tasks if clocks[t]-tasks[t][2]>1e-9]
    score=dict(late_count=len(values),total_lateness_min=float(sum(values)),distance_km=82.+(200.**.5+2. if 7 in tasks else 0.))
    old=reference_schedule(tasks,base,topology)['delivery_times_min'];selected={1,2}
    rescued={t for t in tasks if old[str(t)]-tasks[t][2]>1e-9 and clocks[t]-tasks[t][2]<=1e-9}
    lost={t for t in tasks if old[str(t)]-tasks[t][2]<=1e-9 and clocks[t]-tasks[t][2]>1e-9}
    move['family']['selector_window'].update(preview_route_table_score=score,
        preview_ranking_delta={f:score[f]-physical[f] for f in score},
        preview_classifications=dict(selected_rescued_task_ids=sorted(rescued&selected),native_rescued_task_ids=sorted(rescued-selected),
            selected_newly_late_task_ids=sorted(lost&selected),native_newly_late_task_ids=sorted(lost-selected)),
        initial_index_proxy_omits_receiver_native_delay=0.)


def audit(data,*,routes=None,move=None,relay=True,telemetry=None,**kwargs):
    return audit_deadline_window_repooling_proposal(*data[:3],data[3] if routes is None else routes,
        data[4] if move is None else move,relay_enabled=relay,telemetry=telemetry,**kwargs)


def descriptor(data,routes,*,relay=False,groups=None):
    move=deepcopy(data[4]);move['mode']='RELAY' if relay else 'DIRECT'
    registry=_geometry(data[0],data[1])[2];owners=_owners(routes,registry)
    groups=groups if groups is not None else [[t for t in range(1,5) if owners[t]==a] for a in range(3)]
    move.update(final_task_groups=groups,final_aircraft_by_task={str(t):owners[t] for t in range(1,5)},
        actual_final_owners={str(t):owner for t,owner in owners.items()})
    return move


def snapshot(data,*,routes=None,move=None,relay=True):
    routes=data[3] if routes is None else routes;move=deepcopy(data[4] if move is None else move)
    result=audit(data,routes=routes,move=move,relay=relay)
    for name in ('window','shared_loaded_distance_km','source_drop_availability_min'):move[name]=deepcopy(result[name])
    result=audit(data,routes=routes,move=move,relay=relay)
    score=deepcopy(result['score']);physical=[score[f] for f in ('late_count','total_lateness_min','distance_km')]
    saved=dict(routes=[list(r) for r in routes],move=move,score=score,table_score=deepcopy(score),valid=True,complete=True,
        cross_uav_count=result['actual_cross_uav_count'],relay_task_count=len(result['handoffs']),
        key=[sum(w*x/s for w,x,s in zip(W,physical,S)),*physical])
    for n in ('event_times_min','delivery_times_min','source_drop_availability_min','actual_final_owners',
        'shared_loaded_distance_km','window','selected_rescued_task_ids','native_rescued_task_ids',
        'selected_newly_late_task_ids','native_newly_late_task_ids','pooled_rescued_task_ids','compensation_rescued_task_ids'):saved[n]=deepcopy(result[n])
    return saved


def test_actual_source_prefix_loaded_one_wait_cache_and_selected_rescue():
    data=fixture();r=audit(data)
    assert r['final_tasks_per_aircraft']==[2,2,2] and r['actual_cross_uav_count']==2
    assert r['source_drop_availability_min']=={'1':10.,'2':14.}
    assert r['window']['receiver_arrival_min']==0. and r['window']['second_take_min']==14.
    assert r['delivery_times_min']['1']==15. and r['delivery_times_min']['2']==16.
    assert r['outbound_record']['aircraft_waiting_min']==14.
    assert r['shared_loaded_distance_km']==1. and r['selected_rescued_task_ids']==[1]
    assert r['window']['safe_window_satisfied'] is True
    saved=snapshot(data);audit(data,move=saved['move'],telemetry=saved,weights=W,scales=S)


def test_native_ontime_loss_can_cancel_selected_rescue_and_full_table_includes_it():
    data=fixture(native_loss=True);r=audit(data)
    assert r['selected_rescued_task_ids']==[1] and r['native_newly_late_task_ids']==[5]
    assert r['base_score']['late_count']==r['score']['late_count']==1
    assert r['delivery_times_min']['5']==30.
    saved=snapshot(data);saved['table_score']['late_count']=0
    with pytest.raises(ValueError,match='table_score'):audit(data,move=saved['move'],telemetry=saved)


def test_every_receiver_suffix_native_delivery_uses_true_T_plus_ell():
    data=fixture(suffix=True);r=audit(data)
    assert r['window']['suffix_delivery_offsets_min']=={'1':1.,'2':2.,'7':4.}
    assert r['delivery_times_min']['7']==18.
    move=deepcopy(data[4]);move['window']=deepcopy(r['window']);move['window']['required_on_time_task_ids']=[1,7]
    # Rebuild declared parameters; the old bound is deliberately forged.
    with pytest.raises(ValueError,match='fields differ'):audit(data,move=move)


def test_direct_accepts_all_twelve_assignments_without_station_or_relay_role_map():
    data=fixture();maps=set()
    for a,b in permutations(range(1,5),2):
        pair=[t for t in range(1,5) if t not in (a,b)]
        routes=((5,-5,a,-a),(6,-6,b,-b),tuple(v for t in pair for v in (t,-t)))
        move=descriptor(data,routes);r=audit(data,routes=routes,move=move,relay=False)
        assert r['passed'] and not r['handoffs'] and r['window'] is None
        assert r['final_tasks_per_aircraft']==[2,2,2];maps.add(tuple(r['actual_final_owners'][str(t)] for t in range(1,5)))
    assert len(maps)==12
    saved=snapshot(data,routes=data[2],move=descriptor(data,data[2]),relay=False)
    audit(data,routes=data[2],move=saved['move'],relay=False,telemetry=saved,weights=W,scales=S)


def test_sigma_is_safety_only_official_ontime_does_not_change():
    data=fixture();saved=snapshot(data);window=saved['move']['window']
    assert window['safe_window_satisfied'] is True
    window['sigma_min']=1e-8
    window['latest_second_take_min_by_task']={str(t):data[0][t][2]+1e-9-1e-8-window['suffix_delivery_offsets_min'][str(t)] for t in (1,2)}
    window['joint_latest_second_take_min']=min(window['latest_second_take_min_by_task'].values())
    window['safe_window_satisfied']=False;window['safe_rescued_task_ids']=[1]
    saved['window']=deepcopy(window)
    result=audit(data,move=saved['move'],telemetry=saved)
    assert result['score']['late_count']==0 and result['selected_rescued_task_ids']==[1]
    assert result['window']['safe_window_satisfied'] is False


@pytest.mark.parametrize('excess,late',[(0.,False),(.5e-9,False),(2e-9,True)])
def test_official_EPS_boundary_is_independent_of_safety_channel(excess,late):
    data=fixture();data[0][2]=(data[0][2][0],data[0][2][1],16.-excess)
    w=data[4]['family']['selector_window']['window']
    w['latest_second_take_min_by_task']['2']=16.-excess+1e-9-2.
    w['joint_latest_second_take_min']=min(w['latest_second_take_min_by_task'].values())
    w['safe_window_satisfied']=14.<=w['joint_latest_second_take_min']
    w['official_new_late_task_ids']=w['selected_new_late_task_ids']=[2] if late else []
    preview_rank_fields(data)
    r=audit(data);assert (r['score']['late_count']>0)==late


def test_compensation_before_drop_really_delays_full_availability():
    data=fixture();routes=((5,3,-3,101,-101,-5),*data[3][1:])
    r=audit(data,routes=routes);assert r['source_drop_availability_min']['1']>10.
    assert r['window']['second_take_min']>14. and not r['window']['safe_window_satisfied']


def test_reverse_OUT_take_order_preserves_T_and_creates_real_cache():
    data=fixture();route=(104,102,-102,-104);r=audit(data,routes=(*data[3][:2],route))
    assert r['window']['second_take_min']==14.
    assert next(h for h in r['handoffs'] if h['task_id']==1)['package_cache_min']==4.


@pytest.mark.parametrize('field', ['score','table_score','delivery_times_min','source_drop_availability_min',
    'actual_final_owners','event_times_min','selected_rescued_task_ids','native_newly_late_task_ids'])
def test_complete_snapshot_physical_or_rescue_forgery_rejected(field):
    data=fixture();saved=snapshot(data)
    if field in ('score','table_score'):saved[field]['distance_km']+=1.
    elif field in ('delivery_times_min','source_drop_availability_min'):saved[field]['1']+=1.
    elif field=='actual_final_owners':saved[field]['1']=0
    elif field=='event_times_min':saved[field][0][0]+=1.
    else:saved[field]=[6]
    with pytest.raises(ValueError):audit(data,move=saved['move'],telemetry=saved)


@pytest.mark.parametrize('field,value',[('second_take_min',1.),('receiver_arrival_min',1.),
    ('safe_window_satisfied',False),('official_lateness_epsilon_min',1e-5),('sigma_min',-1.),
    ('required_on_time_task_ids',[5]),('suffix_delivery_offsets_min',{'1':0.,'2':1.})])
def test_window_certificate_cannot_replace_actual_join_or_suffix(field,value):
    data=fixture();saved=snapshot(data);saved['move']['window'][field]=value;saved['window']=deepcopy(saved['move']['window'])
    with pytest.raises(ValueError):audit(data,move=saved['move'],telemetry=saved)


def test_partial_optimistic_clock_and_wrong_source_base_cannot_be_complete():
    data=fixture();saved=snapshot(data);saved['move']['window']['optimistic_missing_comp']=True
    with pytest.raises(ValueError,match='partial'):audit(data,move=saved['move'])
    swapped=(data[2][1],data[2][0],data[2][2])
    with pytest.raises(ValueError,match='source ownership'):
        audit_deadline_window_repooling_proposal(data[0],data[1],swapped,data[3],data[4],relay_enabled=True)
    with pytest.raises(ValueError):audit(data,routes=(data[3][0],data[3][1],data[3][2][:-1]))


def test_native_owner_or_sequence_and_roles_are_fixed_even_if_full_DAG_valid():
    data=fixture();routes=((6,101,-101,-6,3,-3),(5,-5,103,-103,4,-4),data[3][2])
    reference_schedule(data[0],routes,data[1])
    with pytest.raises(ValueError,match='unrelated native'):audit(data,routes=routes)
    routes=((5,103,-103,-5,3,-3),(6,-6,101,-101,4,-4),data[3][2])
    with pytest.raises(ValueError,match='original source'):audit(data,routes=routes)


def test_capacity_quota_nonconsecutive_receiving_and_zero_shared_are_rejected():
    data=fixture();bad=(data[3][0][:-2],data[3][1],(3,102,104,-102,-104,-3))
    with pytest.raises(ValueError,match='capacity'):audit(data,routes=bad)
    bad=(data[3][0][:-2],data[3][1]+(3,-3),data[3][2])
    with pytest.raises(ValueError,match='quota|exceeds K'):audit(data,routes=bad)
    bad=(*data[3][:2],(102,-102,104,-104))
    with pytest.raises(ValueError,match='consecutive OUT'):audit(data,routes=bad)
    data[0][1]=(data[0][1][0],(0.,0.),17.)
    with pytest.raises(ValueError,match='positive joint'):audit(data)


@pytest.mark.parametrize('change', ['source_route','free_source_prefix','receiver_route','receiver_gap',
    'overflow','complete_flag','missing_comp_flag','source_availability','join_time','required_false_rescue'])
def test_selector_preview_is_bound_to_true_source_routes_original_gap_and_own_arithmetic(change):
    data=fixture();w=data[4]['family']['selector_window']
    if change=='source_route':w['source_preview_routes']['1']=[101,-101]
    elif change=='free_source_prefix':w['source_preview_routes']['1']=[101,-101,5,-5];w['window']['source_availability_min']['1']=0.
    elif change=='receiver_route':w['receiver_preview_route']=[102,104,-102,-104,4,3,-3,-4]
    elif change=='receiver_gap':w['receiver_gap_index']=1
    elif change=='overflow':w['receiver_quota_overflow']=0
    elif change=='complete_flag':w['preview_complete_solution']=True
    elif change=='missing_comp_flag':w['optimistic_missing_comp']=False
    elif change=='source_availability':w['window']['source_availability_min']['1']=0.
    elif change=='join_time':w['window']['second_take_min']=10.
    else:w['window']['official_rescued_task_ids']=[]
    with pytest.raises(ValueError):audit(data)


def test_selector_preview_empty_gap_uses_actual_C_home_not_free_station_start():
    data=fixture();data[1]['home_points'][2]=[20.,0.]
    with pytest.raises(ValueError,match='preview window'):audit(data)


def test_preview_can_be_good_while_real_compensation_destroys_that_window():
    data=fixture();routes=((5,3,-3,101,-101,-5),*data[3][1:]);r=audit(data,routes=routes)
    assert r['selector_preview']['window']['safe_window_satisfied'] is True
    assert r['selector_preview']['window']['second_take_min']==14.
    assert r['window']['second_take_min']==30. and r['window']['safe_window_satisfied'] is False
    assert r['selector_preview']['receiver_quota_overflow']==2
    assert r['selector_preview']['preview_complete_solution'] is False
    # Its preview fullreceiver K4 never relaxes the actual macro's K2.
    assert r['final_tasks_per_aircraft']==[2,2,2]


def test_exact_EPS_float_case_matches_reference_lateness_subtraction():
    data=fixture();due=16.-1e-9;data[0][2]=(data[0][2][0],data[0][2][1],due)
    assert (16.-due)>1e-9
    w=data[4]['family']['selector_window']['window']
    w['latest_second_take_min_by_task']['2']=due+1e-9-2.
    w['joint_latest_second_take_min']=min(w['latest_second_take_min_by_task'].values())
    w['safe_window_satisfied']=14.<=w['joint_latest_second_take_min']
    w['official_new_late_task_ids']=w['selected_new_late_task_ids']=[2]
    preview_rank_fields(data)
    r=audit(data);assert r['score']['late_count']==1 and r['selected_newly_late_task_ids']==[2]


def test_no_synthetic_base_or_resource_change_can_certify_complete_saved_candidate():
    data=fixture();saved=snapshot(data)
    fake=deepcopy(data[2]);fake=(fake[0],fake[1],(4,3,-4,-3))
    with pytest.raises(ValueError,match='original C gap'):
        audit_deadline_window_repooling_proposal(data[0],data[1],fake,data[3],saved['move'],relay_enabled=True)
    saved['topology']=deepcopy(data[1]);saved['topology']['max_tasks_per_drone']=3
    with pytest.raises(ValueError,match='resources'):audit(data,move=saved['move'],telemetry=saved)


def test_real_producer_float_zero_physics_does_not_relax_integer_task_identity():
    data=fixture();routes=(data[2][0],data[2][1],(3,-3,4,-4));move=descriptor(data,routes)
    saved=snapshot(data,routes=routes,move=move,relay=False)
    assert type(saved['shared_loaded_distance_km']) is float and saved['shared_loaded_distance_km']==0.
    assert type(saved['score']['total_lateness_min']) is float
    audit(data,routes=routes,move=saved['move'],relay=False,telemetry=saved,weights=W,scales=S)
    saved['move']['final_aircraft_by_task']['1']=0.
    with pytest.raises(ValueError,match='integer'):audit(data,routes=routes,move=saved['move'],relay=False)


def test_full_source_IN_pickup_may_share_native_cargo_at_capacity_two():
    r=audit(fixture())
    assert r['core_reference']['route_records'][0]['load_after_event'][:3]==[1,2,1]


@pytest.mark.parametrize('field,value',[('pooled_rescued_task_ids',[3]),('compensation_rescued_task_ids',[1])])
def test_pooled_rescue_cannot_be_replaced_by_compensation_credit(field,value):
    data=fixture();saved=snapshot(data);saved[field]=value
    with pytest.raises(ValueError,match='rescued_task_ids'):audit(data,move=saved['move'],telemetry=saved)


def test_pooled_and_compensation_true_rescue_are_counted_separately():
    data=fixture();data[0][4]=(data[0][4][0],data[0][4][1],30.)
    preview_rank_fields(data);saved=snapshot(data);r=audit(data,move=saved['move'],telemetry=saved)
    assert r['selected_rescued_task_ids']==[1,4]
    assert r['pooled_rescued_task_ids']==[1] and r['compensation_rescued_task_ids']==[4]


@pytest.mark.parametrize('field', ['preview_route_table_score','preview_ranking_delta','preview_classifications'])
def test_actual_preview_native_penalty_fields_are_recomputed_not_trusted(field):
    data=fixture(native_loss=True);w=data[4]['family']['selector_window']
    if field=='preview_classifications':w[field]['native_newly_late_task_ids']=[]
    else:w[field]['total_lateness_min']-=10.
    with pytest.raises(ValueError,match='real selector'):audit(data)


def test_preview_full_native_cost_penalty_is_present_even_when_selected_window_succeeds():
    r=audit(fixture(native_loss=True));v=r['selector_preview']
    assert v['window']['safe_window_satisfied'] is True
    assert v['preview_route_table_score']['late_count']==1
    assert v['preview_route_table_score']['total_lateness_min']==15.
    assert v['preview_classifications']['native_newly_late_task_ids']==[5]


def test_empty_full_Q_is_undefined_window_without_a_true_safety_claim():
    data=fixture();saved=snapshot(data);w=saved['move']['window']
    w.update(required_on_time_task_ids=[],latest_second_take_min_by_task={},joint_latest_second_take_min=None,safe_window_satisfied=None)
    saved['window']=deepcopy(w);r=audit(data,move=saved['move'],telemetry=saved)
    assert r['window']['joint_latest_second_take_min'] is None and r['window']['safe_window_satisfied'] is None
    assert r['score']['late_count']==0
    data[4]['family']['selector_window']['window']['required_on_time_task_ids']=[]
    with pytest.raises(ValueError,match='originally late pooled target'):audit(data)


def test_catalog_family_without_any_complete_candidate_is_still_independently_audited():
    from algorithm.experiments.audit_deadline_window_repooling import audit_deadline_window_family
    data=fixture();proof=audit_deadline_window_family(*data[:3],data[4]['family'])
    assert proof['passed'] and proof['complete_solution'] is False
    assert proof['selector_preview']['window']['second_take_min']==14.
    assert proof['selector_preview']['receiver_quota_overflow']==2
    assert proof['selector_preview']==audit(data)['selector_preview']


def test_family_only_audit_rejects_forged_previews_without_needing_a_proposal():
    from algorithm.experiments.audit_deadline_window_repooling import audit_deadline_window_family
    data=fixture();family=deepcopy(data[4]['family'])
    family['selector_window']['window']['second_take_min']=0.
    with pytest.raises(ValueError):audit_deadline_window_family(*data[:3],family)
    family=deepcopy(data[4]['family']);family['a']=1
    with pytest.raises(ValueError):audit_deadline_window_family(*data[:3],family)
