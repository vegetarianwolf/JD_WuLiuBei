"""Real nine-task/four-UAV post witnesses, no search/helper imports."""
from copy import deepcopy
import pytest
from algorithm.experiments.audit_window_damage_repair import audit_window_damage_repair_proposal,audit_window_damage_closure
from algorithm.experiments.audit_deadline_window_repooling import audit_deadline_window_repooling_proposal
from algorithm.experiments.audit_weighted_relay_ablation import reference_schedule,_geometry
from algorithm.experiments.audit_repooling import _owners,_identity
from algorithm.tests.test_audit_deadline_window_repooling import fixture as window_fixture,descriptor

W=(.5,.25,.25);S=(9.,100.,100.)


def fixture(*,direct=False):
    tasks,topology,base,raw,move=deepcopy(window_fixture(suffix=True))
    topology['drone_count']=4;topology['home_points'].append([-10.,0.])
    tasks[8]=((-10.,0.),(-18.,0.),100.);tasks[9]=((-10.,0.),(-10.,0.),100.)
    donor=(8,-8,9,-9);base=(*base,donor);raw=(*raw,donor)
    move['actual_final_owners'].update({'8':3,'9':3})
    move['family']['selector_window']['preview_route_table_score']['distance_km']+=16.
    if direct:
        raw=base;move=descriptor((tasks,topology,base,raw,move),raw,relay=False)
    return tasks,topology,base,raw,move


def audit(data,post=None,*,telemetry=None,**kwargs):
    return audit_window_damage_repair_proposal(*data[:4],data[3] if post is None else post,data[4],
        relay_enabled=data[4]['mode']=='RELAY',telemetry=telemetry,**kwargs)


def snapshot(data,post):
    proof=audit(data,post);score=deepcopy(proof['score']);values=[score[k] for k in ('late_count','total_lateness_min','distance_km')]
    saved=dict(routes=[list(r) for r in post],complete=True,valid=True,score=score,post_global_score=deepcopy(score),
        key=[sum(w*v/s for w,v,s in zip(W,values,S)),*values],original_macro_move=deepcopy(data[4]),
        pin_scope='window_damage_repair',move={'semantic':'post_window_damage_repair'},
        cross_uav_count=proof['actual_cross_uav_count'],relay_task_count=len(proof['handoffs']))
    for n in ('pinned_task_ids','released_compensation_task_ids','actual_final_owners','event_times_min','delivery_times_min',
        'current_source_drop_availability_min','current_shared_loaded_distance_km','current_handoff','current_classifications'):
        saved[n]=deepcopy(proof[n])
    return saved


def swap_external_donor(data):
    remap={5:8,8:5}
    return tuple(tuple((1 if v>0 else -1)*remap[abs(v)] if abs(v) in remap else v for v in r) for r in data[3])


@pytest.mark.parametrize('direct',[False,True])
def test_nine_tasks_external_fourth_aircraft_donor_really_changes_owner_preserving_raw_quotas(direct):
    data=fixture(direct=direct);post=swap_external_donor(data);proof=audit(data,post)
    assert proof['actual_final_owners']['5']==3 and proof['actual_final_owners']['8']==0
    assert proof['unpinned_owner_changed_task_ids']==[5,8]
    assert proof['final_tasks_per_aircraft']==[2,2,3,2]
    assert proof['actual_cross_uav_count']==(0 if direct else 2)
    assert proof['pinned_final_owners']==({'1':0,'2':1} if direct else {'1':2,'2':2})
    saved=snapshot(data,post);audit(data,post,telemetry=saved,weights=W,scales=S)
    with pytest.raises(ValueError):
        audit_deadline_window_repooling_proposal(data[0],data[1],data[2],post,data[4],relay_enabled=not direct)


def test_after_first_OUT_delivery_native_PD_can_interleave_without_raw_fourblock():
    data=fixture();post=(*data[3][:2],(102,104,-102,7,-7,-104),data[3][3]);proof=audit(data,post)
    assert proof['current_handoff']['suffix_delivery_offsets_min']=={'1':1.,'7':4.,'2':6.}
    assert proof['delivery_times_min']['2']==20.
    assert proof['current_classifications']['pooled_newly_late_task_ids']==[2]
    assert proof['current_shared_loaded_distance_km']==1.
    saved=snapshot(data,post);audit(data,post,telemetry=saved)
    with pytest.raises(ValueError,match='four-event'):
        audit_deadline_window_repooling_proposal(data[0],data[1],data[2],post,data[4],relay_enabled=True)


def test_source_already_loaded_and_native_delivery_before_INdrop_recomputes_actual_arrival():
    data=fixture();post=((5,101,-5,-101,3,-3),*data[3][1:]);proof=audit(data,post)
    assert proof['core_reference']['route_records'][0]['load_after_event'][:3]==[1,2,1]
    assert proof['current_source_drop_availability_min']['1']==30.
    assert proof['current_handoff']['second_take_min']==30.
    assert proof['delivery_times_min']['1']==31.


def test_compensation_owner_is_released_and_never_reused_from_raw_map():
    data=fixture();remap={3:8,8:3}
    post=tuple(tuple((1 if v>0 else -1)*remap[abs(v)] if abs(v) in remap else v for v in route) for route in data[3])
    proof=audit(data,post)
    assert proof['compensation_owner_changed_task_ids']==[3]
    assert proof['compensation_actual_final_owners']['3']==3
    assert data[4]['final_aircraft_by_task']['3']==0
    audit(data,post,telemetry=snapshot(data,post))


def test_direct_pin_pair_may_have_different_owners_no_forced_Cwindow_or_shared():
    data=fixture(direct=True);proof=audit(data)
    assert proof['pinned_final_owners']=={'1':0,'2':1}
    assert proof['current_handoff'] is None and proof['current_source_drop_availability_min']=={}
    assert proof['current_shared_loaded_distance_km']==0.


def test_changed_pin_modes_roles_H_or_signed_event_order_and_newrelay_rejected():
    data=fixture();post=((5,103,-103,-5,3,-3),(6,-6,101,-101,4,-4),*data[3][2:])
    with pytest.raises(ValueError,match='pinned whole'):audit(data,post)
    post=(*data[3][:2],(104,102,-102,-104,7,-7),data[3][3])
    reference_schedule(data[0],post,data[1])
    with pytest.raises(ValueError,match='signed-event'):audit(data,post)
    data=fixture(direct=True);post=fixture()[3]
    with pytest.raises(ValueError):audit(data,post)


def test_current_take_adjacency_empty_frontier_capacity_and_positive_shared_are_hard():
    data=fixture();post=(*data[3][:2],(102,7,-7,104,-102,-104),data[3][3])
    reference_schedule(data[0],post,data[1])
    with pytest.raises(ValueError,match='adjacent'):audit(data,post)
    post=(*data[3][:2],(7,102,104,-102,-104,-7),data[3][3])
    with pytest.raises(ValueError,match='capacity'):audit(data,post)
    post=(*data[3][:2],(7,102,-7,104,-102,-104),data[3][3])
    with pytest.raises(ValueError,match='adjacent'):audit(data,post)


def test_wrong_final_quota_even_below_max_and_missing_coverage_rejected():
    data=fixture();post=(data[3][0][:-2],data[3][1],data[3][2],data[3][3]+(3,-3))
    assert reference_schedule(data[0],post,data[1])['score']
    with pytest.raises(ValueError,match='exact aircraft final quotas'):audit(data,post)
    with pytest.raises(ValueError):audit(data,(*data[3][:3],data[3][3][:-1]))


@pytest.mark.parametrize('field',['actual_final_owners','post_global_score','event_times_min','delivery_times_min',
    'current_source_drop_availability_min','current_shared_loaded_distance_km','current_handoff','current_classifications'])
def test_every_current_full_physical_or_classification_field_is_bound(field):
    data=fixture();saved=snapshot(data,data[3])
    if field=='actual_final_owners':saved[field]['3']=3
    elif field=='post_global_score':saved[field]['distance_km']+=1.
    elif field=='event_times_min':saved[field][0][0]+=1.
    elif field=='delivery_times_min':saved[field]['1']+=1.
    elif field=='current_source_drop_availability_min':saved[field]['1']+=1.
    elif field=='current_shared_loaded_distance_km':saved[field]+=1.
    elif field=='current_handoff':saved[field]['receiver_waiting_min']=0.
    else:saved[field]['pooled_rescued_task_ids']=[]
    with pytest.raises(ValueError):audit(data,telemetry=saved)


@pytest.mark.parametrize('field',['table_score','window','source_drop_availability_min','shared_loaded_distance_km'])
def test_raw_score_or_window_cannot_certify_post(field):
    data=fixture();saved=snapshot(data,data[3]);saved[field]=deepcopy(data[4].get(field,{}))
    with pytest.raises(ValueError,match='Raw table/window'):audit(data,telemetry=saved)


def test_no_synthetic_base_historical_macro_or_wrong_objective_is_accepted():
    data=fixture();saved=snapshot(data,data[3]);saved['original_macro_move']['family']['k1']=9
    with pytest.raises(ValueError,match='provenance'):audit(data,telemetry=saved)
    fake=(data[2][1],data[2][0],*data[2][2:])
    with pytest.raises(ValueError):
        audit_window_damage_repair_proposal(data[0],data[1],fake,data[3],data[3],data[4],relay_enabled=True)
    saved=snapshot(data,data[3]);saved['key'][0]-=.1
    with pytest.raises(ValueError,match='weighted'):audit(data,telemetry=saved,weights=W,scales=S)


def closure(data,removed=(5,8),limit=2):
    registry=_geometry(data[0],data[1])[2];owners=_owners(data[3],registry);pins={1,2};eligible=set(data[0])-pins
    q=[sum(owners[t]==a for t in removed) for a in range(4)];pin=[sum(owners[t]==a for t in pins) for a in range(4)]
    remaining=[sum(owners[t]==a for t in eligible-set(removed)) for a in range(4)]
    raw=[sum(owners[t]==a for t in data[0]) for a in range(4)];opened=[a for a,v in enumerate(q) if v]
    return dict(removed_task_ids=list(removed),removed_task_ids_sorted=sorted(removed),pinned_task_ids=[1,2],
        partial_routes=[[v for v in r if _identity(v,registry) not in removed] for r in data[3]],
        quota_slots_by_aircraft=q,quota_slots=list(q),actual_opened_aircraft=opened,
        all_aircraft_opened=len(opened)==4,unopened_aircraft_reasons={str(a):'declared_bounded_cap' for a in range(4) if a not in opened},
        raw_final_counts=raw,pin_final_counts=pin,remaining_native_final_counts=remaining,eligible_native_count=len(eligible),
        closure_limit=limit,effective_closure_limit=min(limit,len(eligible)),omitted_task_ids=sorted(eligible-set(removed)),
        truncation=dict(limit_reached=len(removed)>=min(limit,len(eligible)),eligible_omitted_count=len(eligible-set(removed)),expansion_truncated=bool(eligible-set(removed))))


def test_actual_removed_wholeplans_prove_external_donor_slots_and_truthful_partial_scope():
    data=fixture();record=closure(data);r=audit_window_damage_closure(data[0],data[1],data[3],record,pinned_task_ids=[1,2])
    assert r['quota_slots_by_aircraft']==[1,0,0,1] and r['actual_opened_aircraft']==[0,3]
    assert not r['all_aircraft_opened'] and not r['complete_solution']
    assert r['raw_final_counts']==[2,2,3,2] and sum(r['quota_slots_by_aircraft'])==2


@pytest.mark.parametrize('field',['quota_slots_by_aircraft','partial_routes','actual_opened_aircraft','all_aircraft_opened',
    'pin_final_counts','remaining_native_final_counts','omitted_task_ids'])
def test_false_donor_or_quota_and_all_aircraft_claim_is_rejected(field):
    data=fixture();record=closure(data)
    if field=='quota_slots_by_aircraft':record[field]=[1,1,0,0]
    elif field=='partial_routes':record[field][0]+=[5,-5]
    elif field=='actual_opened_aircraft':record[field]=[0,1,2,3]
    elif field=='all_aircraft_opened':record[field]=True
    elif field in ('pin_final_counts','remaining_native_final_counts'):record[field][0]+=1
    else:record[field]=[]
    with pytest.raises(ValueError):audit_window_damage_closure(data[0],data[1],data[3],record,pinned_task_ids=[1,2])


def test_effective_cap_larger_than_eligible_is_not_a_false_overflow():
    data=fixture();record=closure(data,removed=tuple(range(3,10)),limit=16)
    r=audit_window_damage_closure(data[0],data[1],data[3],record,pinned_task_ids=[1,2])
    assert r['all_aircraft_opened'] and r['eligible_native_count']==7


def test_donor_reservation_must_really_remove_declared_owner_whole_task():
    data=fixture();record=closure(data)
    record['evidence']=dict(donor_reservations=[dict(aircraft=3,task_id=8,rank_index=0,ranked_task_ids=[8,9],rotation_head_size=2)])
    audit_window_damage_closure(data[0],data[1],data[3],record,pinned_task_ids=[1,2])
    record['evidence']['donor_reservations'][0]['aircraft']=1
    with pytest.raises(ValueError,match='Donor'):audit_window_damage_closure(data[0],data[1],data[3],record,pinned_task_ids=[1,2])


def partial_snapshot(data):
    from algorithm.experiments.audit_window_damage_repair import audit_window_damage_partial
    rec=closure(data);present=sorted(set(data[0])-set(rec['removed_task_ids']));routes=rec['partial_routes']
    proof=audit_window_damage_partial(data[0],data[1],routes,present,target_final_counts=rec['raw_final_counts'])
    score=proof['score'];values=[score[f] for f in ('late_count','total_lateness_min','distance_km')]
    saved=dict(routes=routes,score=deepcopy(score),key=[sum(w*v/s for w,v,s in zip(W,values,S)),*values],
        event_times_min=deepcopy(proof['event_times_min']),delivery_times_min=deepcopy(proof['delivery_times_min']),
        remaining_quota_slots=rec['quota_slots_by_aircraft'],present_task_ids=present,assignment_index=0,
        scoring_scope='partial_same_coverage_only')
    return rec,present,routes,saved


def test_samecoverage_partial_global_clock_includes_true_pin_wait_and_actual_quota_remainder():
    from algorithm.experiments.audit_window_damage_repair import audit_window_damage_partial
    data=fixture();rec,present,routes,saved=partial_snapshot(data)
    proof=audit_window_damage_partial(data[0],data[1],routes,present,telemetry=saved,weights=W,scales=S,
        target_final_counts=rec['raw_final_counts'])
    assert proof['complete_solution'] is False and proof['remaining_quota_slots']==[1,0,0,1]
    assert proof['delivery_times_min']['1']==15. and proof['delivery_times_min']['2']==16.
    assert proof['core_reference']['relay_statistics']['total_relay_waiting_time']==14.
    assert set(proof['delivery_times_min'])=={str(t) for t in present}


@pytest.mark.parametrize('changed',['present','slots','clock','score','complete','key'])
def test_partial_coverage_slots_score_and_wait_cannot_be_forged(changed):
    from algorithm.experiments.audit_window_damage_repair import audit_window_damage_partial
    data=fixture();rec,present,routes,saved=partial_snapshot(data)
    if changed=='present':present=sorted(present+[5])
    elif changed=='slots':saved['remaining_quota_slots']=[0,1,0,1]
    elif changed=='clock':saved['delivery_times_min']['1']=1.
    elif changed=='score':saved['score']['distance_km']+=1.
    elif changed=='complete':saved['complete']=True
    else:saved['key'][0]-=.01
    with pytest.raises(ValueError):audit_window_damage_partial(data[0],data[1],routes,present,telemetry=saved,
        weights=W,scales=S,target_final_counts=rec['raw_final_counts'])


def test_partial_remaining_slots_require_actual_complete_vector_and_do_not_invent_full_K():
    from algorithm.experiments.audit_window_damage_repair import audit_window_damage_partial
    data=fixture();rec,present,routes,saved=partial_snapshot(data)
    with pytest.raises(ValueError,match='target counts'):audit_window_damage_partial(data[0],data[1],routes,present,telemetry=saved)
    with pytest.raises(ValueError):audit_window_damage_partial(data[0],data[1],routes,present,target_final_counts=[3,3,3,3])
    # The raw tiny vector is2/2/3/2, not every aircraft's maximum3.
    assert rec['raw_final_counts']==[2,2,3,2]


def test_real_global_dependency_cycle_is_rejected_even_before_pinned_role_guard():
    data=fixture();post=((5,-5,3,-3),data[3][1],(102,104,-102,-104,101,-101,7,-7),data[3][3])
    with pytest.raises(ValueError,match='dependency cycle'):audit(data,post)


def test_additional_nonpin_relay_and_changed_pin_direct_mode_cannot_hide_in_full_post():
    data=fixture();post=((5,101,-101,-5,105,-105,106,-106),*data[3][1:])
    reference_schedule(data[0],post,data[1])
    with pytest.raises(ValueError,match='added or removed relay'):audit(data,post)
    post=((5,-5,3,-3),data[3][1],(1,104,-1,-104,7,-7),data[3][3])
    reference_schedule(data[0],post,data[1])
    with pytest.raises(ValueError,match='pinned whole'):audit(data,post)


def test_donor_category_omissions_match_actual_declared_pool_and_limit():
    data=fixture();record=closure(data);record['eligible_not_removed_task_ids']=list(record['omitted_task_ids'])
    record['evidence']=dict(donor_reservations=[dict(aircraft=3,task_id=8,rank_index=0,ranked_task_ids=[8,9],rotation_head_size=2)],
        category_rankings={'damage':[5,6],'compensation':[3,4]})
    record['expansion_omitted_task_ids']=[3,4,6];record['truncation']['expansion_omitted_count']=3
    audit_window_damage_closure(data[0],data[1],data[3],record,pinned_task_ids=[1,2])
    record['expansion_omitted_task_ids']=[3,4]
    with pytest.raises(ValueError,match='omitted expansion'):audit_window_damage_closure(data[0],data[1],data[3],record,pinned_task_ids=[1,2])


def test_donor_reservation_cannot_spend_cap_on_one_owner_while_claiming_bounded_unopened_others():
    data=fixture();record=closure(data,removed=(3,5),limit=2)
    with pytest.raises(ValueError,match='Donor-first'):audit_window_damage_closure(data[0],data[1],data[3],record,pinned_task_ids=[1,2])
