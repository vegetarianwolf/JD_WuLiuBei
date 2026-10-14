"""Real same-original-owner split feeders and independent twelve-map DIRECT."""
from copy import deepcopy
from itertools import permutations
from math import hypot

import pytest

from algorithm.experiments.audit_split_feeder import (
    audit_split_feeder_proposal,audit_split_feeder_family,_classes_names)
from algorithm.experiments.audit_deadline_window_repooling import _window
from algorithm.experiments.audit_weighted_relay_ablation import _geometry,reference_schedule
from algorithm.experiments.audit_repooling import _owners
from algorithm.tests.test_audit_deadline_window_repooling import fixture as old_geometry

W=(.5,.25,.25);S=(9.,100.,100.)


def fixture(*,suffix=True):
    tasks,topology,_,_,_=deepcopy(old_geometry(native_loss=True,suffix=suffix))
    a=(5,-5,3,-3);b=(6,-6,4,-4);c=(1,2,-1,-2)+((7,-7) if suffix else ())
    base=(a,b,c)
    relay=((5,101,-101,-5,3,-3),(6,-6,103,-103,4,-4),
           (102,104,-102,-104)+((7,-7) if suffix else ()))
    if suffix:
        topology['drone_count']=4;topology['home_points'].append([-10.,0.])
        tasks[8]=((-10.,0.),(-18.,0.),100.);tasks[9]=((-10.,0.),(-10.,0.),100.)
        base=(*base,(8,-8,9,-9));relay=(*relay,(8,-8,9,-9))
    family=dict(i=1,j=2,k1=3,k2=4,a=0,b=1,c=2,station_id=9,
        task_ids=[1,2,3,4],pooled_task_ids=[1,2],priority=[0.])
    return tasks,topology,base,relay,family


def move(data,routes,mode='RELAY'):
    tasks,topology,base,_,family=data
    owners=_owners(routes,_geometry(tasks,topology)[2]);selected=[1,2,3,4] if mode=='DIRECT_DONOR_12MAP' else [1,2]
    return dict(family=deepcopy(family),mode=mode,selected_task_ids=selected,
        final_aircraft_by_task={str(t):owners[t] for t in selected},
        actual_final_owners={str(t):owners[t] for t in sorted(tasks)})


def audit(data,routes=None,mode='RELAY',telemetry=None,**kwargs):
    routes=data[3] if routes is None else routes
    return audit_split_feeder_proposal(*data[:3],routes,move(data,routes,mode),
        relay_enabled=mode=='RELAY',telemetry=telemetry,**kwargs)


def snapshot(data,routes=None,mode='RELAY'):
    routes=data[3] if routes is None else routes;proof=audit(data,routes,mode)
    score=proof['score'];values=[score[f] for f in ('late_count','total_lateness_min','distance_km')]
    saved=dict(routes=[list(r) for r in routes],move=move(data,routes,mode),valid=True,complete=True,
        score=deepcopy(score),table_score=deepcopy(score),key=[sum(w*v/s for w,v,s in zip(W,values,S)),*values],
        cross_uav_count=proof['actual_cross_uav_count'],relay_task_count=len(proof['handoffs']))
    for name in ('event_times_min','delivery_times_min','actual_final_owners','source_drop_availability_min',
        'shared_loaded_distance_km','handoff',*_classes_names()):saved[name]=deepcopy(proof[name])
    return saved


def source_record(data,task,owner):
    routes=data[3];route=routes[owner]
    if task==1:
        times=[0.,0.,10.,30.,50.,60.];loads=[1,2,1,0,1,0];deliveries={'5':30.,'3':60.};score=dict(late_count=1,total_lateness_min=15.,distance_km=60.)
    else:
        times=[0.,0.,0.,14.,14.,28.];loads=[1,0,1,0,1,0];deliveries={'6':0.,'4':28.};score=dict(late_count=0,total_lateness_min=0.,distance_km=28.)
    pickup,drop=(1,2) if task==1 else (2,3);values=[score[f] for f in ('late_count','total_lateness_min','distance_km')]
    return dict(task_id=task,aircraft=owner,station_id=9,route=list(route),score=score,
        key=[sum(w*v/s for w,v,s in zip(W,values,S)),*values],event_times_min=times,delivery_times_min=deliveries,
        source_drop_availability_min={str(task):times[drop]},in_drop_time_min=times[drop],in_drop_position=drop,pickup_position=pickup,
        load_before=[0,*loads[:-1]],load_after=loads,final_delivery_count=2,native_complete=True,
        optimistic_missing_comp=False,compensation_task_id=None,latest_drop_bounds=[14.],
        source_bound_position_coverage=[dict(bound_min=14.,legal_positions_before_bound=1,best_geometry_retained=True,
            latest_safe_retained=True,omitted_due_position_limit=False)],
        coverage_scope='finite_position_retention_only_not_global_feasibility_or_J_bound')


def family_with_witness(data):
    tasks,topology,base,routes,family=data;family=deepcopy(family)
    family['source_table_keys']=['1:0:9','2:1:9']
    family['source_tables']={key:[source_record(data,t,a)] for key,t,a in zip(family['source_table_keys'],(1,2),(0,1))}
    proof=audit(data);score=proof['score'];values=[score[f] for f in ('late_count','total_lateness_min','distance_km')]
    homes,stations,registry,_,_,_,speed=_geometry(tasks,topology)
    window=_window(tasks,homes,stations,registry,speed,routes,proof['core_reference'],reference_schedule(tasks,base,topology),
        i=1,j=2,c=2,station=9,availability=proof['source_drop_availability_min'],sigma=0.,required=[1,2])
    family['relay_witnesses']=[dict(routes=[list(r) for r in routes],table_score=score,
        key=[sum(w*v/s for w,v,s in zip(W,values,S)),*values],event_times_min=proof['event_times_min'],
        delivery_times_min=proof['delivery_times_min'],source_drop_availability_min=proof['source_drop_availability_min'],
        shared_loaded_distance_km=proof['shared_loaded_distance_km'],actual_final_owners=proof['actual_final_owners'],
        window=window,pooled_rescued_task_ids=proof['pooled_rescued_task_ids'],
        pooled_newly_late_task_ids=proof['pooled_newly_late_task_ids'],complete_RELAY_model=True,canonical_full_checked=False,
        scoring_scope='complete_RELAY_model_common_ranking_not_DIRECT_candidate')]
    family['priority']=family['relay_witnesses'][0]['key']
    family['donor_evidence']=[]
    for task,owner,beforeD,afterD,deleted in ((3,0,40.,10.,[5,-5]),(4,1,28.,0.,[6,-6])):
        deltaD=afterD-beforeD
        family['donor_evidence'].append(dict(task_id=task,aircraft=owner,candidate_count=2,
            baseline_score=dict(late_count=0,total_lateness_min=0.,distance_km=beforeD),deleted_route=deleted,
            deletion_score=dict(late_count=0,total_lateness_min=0.,distance_km=afterD),
            deletion_delta_key=[W[2]*deltaD/S[2],0,0.,deltaD],
            policy='best_exact_single_whole_DIRECT_deletion_weighted_credit; DIRECT-only donor; unchanged in RELAY'))
    return family


def test_true_same_owner_two_feeders_keep_all_final_owners_and_account_native_delay():
    data=fixture();proof=audit(data)
    assert proof['original_final_owners']==proof['actual_final_owners']
    assert proof['actual_final_owners']['1']==proof['actual_final_owners']['2']==2
    assert proof['actual_cross_uav_count']==2 and proof['unselected_task_count']==7
    assert proof['final_tasks_per_aircraft']==[2,2,3,2]
    assert proof['source_drop_availability_min']=={'1':10.,'2':14.}
    assert proof['handoff']['receiver_arrival_min']==0. and proof['handoff']['take_times_min']==[10.,14.]
    assert proof['handoff']['receiver_waiting_min']==14.
    assert proof['handoff']['ground_cache_by_task_min']=={'1':0.,'2':0.}
    assert proof['delivery_times_min']['1']==15. and proof['delivery_times_min']['2']==16.
    assert proof['pooled_rescued_task_ids']==[1,2] and proof['native_newly_late_task_ids']==[5]
    assert proof['shared_loaded_distance_km']==1. and proof['score']['late_count']==1
    audit(data,telemetry=snapshot(data),weights=W,scales=S)


def test_feeder_full_K_can_add_IN_with_original_native_already_held():
    data=fixture(suffix=False);proof=audit(data)
    assert proof['final_tasks_per_aircraft']==[2,2,2] and data[1]['max_tasks_per_drone']==2
    source=proof['core_reference']['route_records'][0]
    assert source['transport_leg_count']==3 and source['task_count']==2
    assert source['load_after_event'][:3]==[1,2,1]


def test_native_may_follow_first_OUT_delivery_without_weakening_doubletake():
    data=fixture();routes=(*data[3][:2],(102,104,-102,7,-7,-104),data[3][3])
    proof=audit(data,routes)
    assert proof['delivery_times_min']['2']==20. and proof['shared_loaded_distance_km']==1.
    assert proof['handoff']['suffix_delivery_offsets_min']=={'1':1.,'7':4.,'2':6.}
    assert proof['pooled_rescued_task_ids']==[1]


def test_fixed_cache_can_hold_early_source_without_fictitious_sender_ack_wait():
    data=fixture();routes=(*data[3][:2],(104,102,-104,-102,7,-7),data[3][3])
    proof=audit(data,routes)
    assert proof['handoff']['take_order_task_ids']==[2,1]
    assert proof['handoff']['take_times_min']==[14.,14.]
    assert proof['handoff']['ground_cache_by_task_min']=={'1':4.,'2':0.}
    assert proof['source_drop_availability_min']=={'1':10.,'2':14.}
    assert proof['event_times_min'][0][3]==30.


def test_tail_feeder_starts_at_actual_home_prefix_end_and_actual_completion_time():
    data=fixture();routes=(data[2][0]+(101,-101),data[2][1]+(103,-103),data[3][2],data[3][3])
    proof=audit(data,routes)
    assert proof['source_drop_availability_min']=={'1':50.,'2':42.}
    assert proof['handoff']['second_take_min']==50.


def test_wrong_original_owner_is_not_made_valid_by_synthetic_repool_base():
    data=fixture();bad=(data[2][2],data[2][1],data[2][0],data[2][3])
    with pytest.raises(ValueError,match='True base ownership'):
        audit_split_feeder_proposal(data[0],data[1],bad,data[3],move(data,data[3]),relay_enabled=True)


def test_direct_all_twelve_maps_are_independent_of_relay_final_map_and_H():
    data=fixture();seen=set()
    for t,q in permutations((1,2,3,4),2):
        pair=[k for k in (1,2,3,4) if k not in (t,q)]
        routes=((5,-5,t,-t),(6,-6,q,-q),tuple(v for k in pair for v in (k,-k))+(7,-7),data[2][3])
        proof=audit(data,routes,'DIRECT_DONOR_12MAP')
        assert not proof['handoffs'] and proof['handoff'] is None and proof['source_drop_availability_min']=={}
        assert proof['final_tasks_per_aircraft']==[2,2,3,2] and proof['unselected_task_count']==5
        assert proof['shared_loaded_distance_km']==0.
        seen.add(tuple(proof['actual_final_owners'][str(k)] for k in (1,2,3,4)))
        audit(data,routes,'DIRECT_DONOR_12MAP',telemetry=snapshot(data,routes,'DIRECT_DONOR_12MAP'),weights=W,scales=S)
    assert len(seen)==12 and (0,1,2,2) in seen and (2,2,0,1) in seen


def test_direct_same_owner_actual_cargo_distance_is_not_entire_owner_route():
    data=fixture();routes=data[2];proof=audit(data,routes,'DIRECT_SAME_OWNER')
    actual=proof['shared_loaded_distance_km'];whole=proof['core_reference']['route_records'][2]['distance_km']
    assert actual==hypot(14.,1.) and actual<whole
    saved=snapshot(data,routes,'DIRECT_SAME_OWNER');saved['shared_loaded_distance_km']=whole
    with pytest.raises(ValueError,match='shared_loaded_distance'):audit(data,routes,'DIRECT_SAME_OWNER',saved)


@pytest.mark.parametrize('field',['score','table_score','event_times_min','delivery_times_min','actual_final_owners',
    'source_drop_availability_min','shared_loaded_distance_km','handoff','key','pooled_rescued_task_ids',
    'donor_newly_late_task_ids','native_newly_late_task_ids'])
def test_every_saved_physical_clock_score_and_classification_is_bound(field):
    data=fixture();saved=snapshot(data)
    if field in ('score','table_score'):saved[field]['distance_km']+=1.
    elif field=='event_times_min':saved[field][0][0]+=1.
    elif field=='delivery_times_min':saved[field]['1']+=1.
    elif field=='actual_final_owners':saved[field]['1']=0
    elif field=='source_drop_availability_min':saved[field]['1']+=1.
    elif field=='shared_loaded_distance_km':saved[field]+=1.
    elif field=='handoff':saved[field]['receiver_waiting_min']=0.
    elif field=='key':saved[field][0]+=.1
    else:saved[field]=[999]
    with pytest.raises(ValueError):audit(data,telemetry=saved,weights=W,scales=S)


def test_native_owner_order_and_exact_quota_are_immutable_in_this_macro():
    data=fixture();routes=tuple(tuple((1 if v>0 else -1)*({5:8,8:5}.get(abs(v),abs(v)))
        if abs(v) in (5,8) else v for v in r) for r in data[3])
    with pytest.raises(ValueError,match='unrelated signed'):audit(data,routes)
    routes=((101,-101,3,-3,5,-5),*data[3][1:])
    with pytest.raises(ValueError,match='unrelated signed'):audit(data,routes)
    routes=(data[3][0][:-2],data[3][1],data[3][2],data[3][3]+(3,-3))
    with pytest.raises(ValueError,match='exact final task quotas'):audit(data,routes)


def test_capacity_take_frontier_extra_relay_and_source_identity_rejected():
    data=fixture();routes=(*data[3][:2],(7,102,104,-102,-104,-7),data[3][3])
    with pytest.raises(ValueError,match='capacity'):audit(data,routes)
    routes=(*data[3][:2],(102,7,-7,104,-102,-104),data[3][3])
    with pytest.raises(ValueError,match='adjacent'):audit(data,routes)
    routes=((5,103,-103,-5,3,-3),(6,-6,101,-101,4,-4),*data[3][2:])
    with pytest.raises(ValueError,match='source/receiver/station'):audit(data,routes)
    routes=((5,101,-101,-5,105,-105,106,-106),*data[3][1:])
    with pytest.raises(ValueError):audit(data,routes)


def test_original_eligibility_and_positive_joint_loaded_flight_are_required():
    data=fixture();data[4]['station_id']=10
    with pytest.raises(ValueError,match='eligible paired legs'):audit(data)
    data=fixture();data[0][1]=(data[0][1][0],(0.,0.),data[0][1][2])
    with pytest.raises(ValueError,match='positive joint'):audit(data)


def test_full_DAG_rejects_cycle_before_a_false_clock_can_certify_candidate():
    data=fixture();routes=((104,-104,101,-101,5,-5,3,-3),(102,-102,103,-103,6,-6,4,-4),(7,-7),data[3][3])
    with pytest.raises(ValueError,match='dependency cycle'):audit(data,routes)


def test_declared_complete_check_and_adoption_deadlines_are_strict():
    data=fixture();saved=snapshot(data)
    saved.update(full_verified_at_perf_counter=10.,accepted_at_perf_counter=11.,deadline_perf_counter=12.)
    audit(data,telemetry=saved)
    saved['accepted_at_perf_counter']=12.
    with pytest.raises(ValueError,match='adoption'):audit(data,telemetry=saved)
    saved['accepted_at_perf_counter']=11.;saved['full_verified_at_perf_counter']=12.
    with pytest.raises(ValueError,match='full check'):audit(data,telemetry=saved)


def test_catalogue_window_is_not_accepted_as_current_full_snapshot_clock():
    data=fixture();saved=snapshot(data)
    saved['window']=family_with_witness(data)['relay_witnesses'][0]['window']
    with pytest.raises(ValueError,match='preview cannot certify'):audit(data,telemetry=saved)


def test_full_complete_shared_RELAY_witness_is_ranking_only_for_DIRECT_catalogue():
    data=fixture();family=family_with_witness(data)
    proof=audit_split_feeder_family(*data[:3],family,weights=W,scales=S)
    assert proof['passed'] and not proof['complete_solution'] and not proof['DIRECT_candidate_certificate']
    assert len(proof['source_route_references'])==2 and proof['witness_references'][0]['actual_cross_uav_count']==2
    assert proof['witness_references'][0]['window']['second_take_min']==14.
    direct_topology={**data[1],'relay_allowed':False}
    assert audit_split_feeder_family(data[0],direct_topology,data[2],family,weights=W,scales=S)['passed']
    with pytest.raises(ValueError):
        audit_split_feeder_proposal(data[0],direct_topology,data[2],data[3],move(data,data[3],'DIRECT_SAME_OWNER'),relay_enabled=False)


@pytest.mark.parametrize('tamper',['source_time','source_native','source_key','full_table','window','scope','source_not_in_surface','role','donor_credit','donor_deleted_route'])
def test_catalogue_source_and_complete_witness_physics_and_scope_are_independent(tamper):
    data=fixture();family=family_with_witness(data);record=family['source_tables']['1:0:9'][0];witness=family['relay_witnesses'][0]
    if tamper=='source_time':record['in_drop_time_min']=0.
    elif tamper=='source_native':record['route']=[101,-101,3,-3,5,-5]
    elif tamper=='source_key':record['key'][0]-=.1
    elif tamper=='full_table':witness['table_score']['late_count']=0
    elif tamper=='window':witness['window']['second_take_min']=0.
    elif tamper=='scope':witness['scoring_scope']='DIRECT_candidate'
    elif tamper=='source_not_in_surface':
        # This source is legal but differs from the actual full witness route.
        r=list(data[2][0])+( [101,-101] )
        record['route']=r
    elif tamper=='donor_credit':family['donor_evidence'][0]['deletion_delta_key'][0]-=1.
    elif tamper=='donor_deleted_route':family['donor_evidence'][0]['deleted_route']=[]
    else:family['a']=2
    with pytest.raises(ValueError):audit_split_feeder_family(*data[:3],family,weights=W,scales=S)


def test_window_sigma_is_a_declared_channel_not_official_lateness_redefinition():
    data=fixture();family=family_with_witness(data);w=family['relay_witnesses'][0]['window']
    w['sigma_min']=1e-8
    for task in w['required_on_time_task_ids']:w['latest_second_take_min_by_task'][str(task)]-=1e-8
    w['joint_latest_second_take_min']-=1e-8;w['safe_window_satisfied']=False
    w['safe_rescued_task_ids']=[1];w['selected_safe_rescued_task_ids']=[1]
    proof=audit_split_feeder_family(*data[:3],family,weights=W,scales=S)
    assert proof['witness_references'][0]['window']['official_rescued_task_ids']==[1,2]
    assert proof['witness_references'][0]['window']['selected_safe_rescued_task_ids']==[1]
