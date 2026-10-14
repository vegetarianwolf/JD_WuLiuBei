"""Handwritten mobile two-source contacts; no optimizer is run."""
from copy import deepcopy

import pytest

from algorithm.experiments.audit_mobile_repooling import (
    audit_mobile_repooling_payload,audit_mobile_repooling_routes,
)
from algorithm.tests.test_audit_mobile_relay import canonical_payload
from algorithm.tests.test_audit_repooling import fixture as quota_fixture


def fixture():
    tasks,_,base_routes,routes,direct,move=quota_fixture()
    base=dict(stations=[[9,25.,25.]],
        legs=[dict(id=201,task_id=3,kind="RELAY_IN",relay_id=9),
              dict(id=202,task_id=3,kind="RELAY_OUT",relay_id=9)],
        task_relay_candidates={str(task):([9] if task==3 else []) for task in tasks},
        home_points=[[-10.,0.],[12.,0.],[0.,0.]],drone_count=3,capacity=2,
        max_tasks_per_drone=2,speed_km_per_min=1.)
    current=deepcopy(base)
    current["stations"].append([10,0.,0.])
    current["legs"] += [dict(id=identifier,task_id=task,kind=kind,relay_id=10)
        for identifier,task,kind in ((101,1,"RELAY_IN"),(102,1,"RELAY_OUT"),
                                    (103,2,"RELAY_IN"),(104,2,"RELAY_OUT"))]
    current["task_relay_candidates"].update({"1":[10],"2":[10]})
    move["station_id"]=10
    return tasks,base,current,base_routes,routes,direct,move


def routes_audit(tasks,base,current,routes,**kwargs):
    return audit_mobile_repooling_routes(tasks,base,current,routes,**kwargs)


def payload_fixture():
    tasks,base,current,original,routes,_,move=fixture()
    independent=routes_audit(tasks,base,current,routes,base_routes=original,move=move)
    payload=canonical_payload(tasks,current,routes,{"mobile_meetings":independent["meetings"]})
    return tasks,base,current,original,routes,move,payload


def test_three_aircraft_saturated_contact_has_empty_wait_then_one_parcel_wait_and_no_cache():
    tasks,base,current,original,routes,_,move=fixture()
    result=routes_audit(tasks,base,current,routes,base_routes=original,move=move)
    meeting=result["meetings"][0]
    assert result["passed"] and result["continuous_handoff_extension"]
    assert result["model"]=="two_source_receiver_first_repooling"
    assert result["final_tasks_per_aircraft"]==[2,2,2]
    assert result["source_responsibility_proven"]
    assert meeting["arrival_times_min"]==[10.,12.,0.]
    assert meeting["source_drop_times_min"]==[10.,12.]
    assert meeting["receiver_take_times_min"]==[10.,12.]
    assert meeting["source_arrival_order"]==[1,2]
    assert meeting["receiver_empty_wait_min"]==10. and meeting["receiver_loaded_wait_min"]==2.
    assert meeting["receiver_load_before_takes"]==[0,1]
    assert meeting["receiver_load_after_takes"]==[1,2]
    assert meeting["source_load_before_drops"]==[1,1]
    assert meeting["shared_double_loaded_km"]==1.
    assert result["total_mobile_aircraft_waiting_min"]==12.
    assert result["mobile_ground_cache_min"]==0.
    assert result["physical_event_times"][2]==[10.,12.,13.,14.]
    assert result["delivery_times_min"]["3"]==20.  # Early sender leaves at t10.
    assert result["score"]==dict(late_count=2,total_lateness_min=3.,distance_km=44.)


def test_earlier_source_is_decided_by_actual_arrival_instead_of_task_number():
    tasks,base,current,_,routes,_,_=fixture()
    for topology in (base,current):
        topology["home_points"][0]=[-14.,0.]
    routes=(routes[0],routes[1],(104,102,-102,-104))
    meeting=routes_audit(tasks,base,current,routes)["meetings"][0]
    assert meeting["arrival_times_min"]==[14.,12.,0.]
    assert meeting["source_arrival_order"]==[2,1]
    assert meeting["receiver_take_times_min"]==[12.,14.]


def test_later_first_take_is_rejected_because_the_early_parcel_would_be_cached():
    tasks,base,current,_,routes,_,_=fixture()
    routes=(routes[0],routes[1],(104,102,-102,-104))
    with pytest.raises(ValueError,match="without ground caching"):
        routes_audit(tasks,base,current,routes)


def test_receiver_arriving_after_a_source_is_not_hidden_as_a_ground_station():
    tasks,base,current,_,routes,_,_=fixture()
    for topology in (base,current):
        topology["home_points"][2]=[0.,20.]
    with pytest.raises(ValueError,match="without ground caching|before both sources"):
        routes_audit(tasks,base,current,routes)


def test_two_sources_must_be_distinct_and_the_receiver_is_a_third_aircraft():
    tasks,base,current,_,routes,_,_=fixture()
    same_source=((5,-5,101,103,-101,-103,3,-3),(6,-6,4,-4),routes[2])
    with pytest.raises(ValueError,match="two distinct sources"):
        routes_audit(tasks,base,current,same_source)


def test_sources_can_hold_native_cargo_but_must_keep_the_incoming_parcel_until_contact():
    tasks,base,current,original,routes,_,move=fixture()
    routes=((5,101,-101,-5,3,-3),routes[1],routes[2])
    result=routes_audit(tasks,base,current,routes,base_routes=original,move=move)
    assert result["meetings"][0]["source_load_before_drops"]==[2,1]
    assert result["physical_event_times"][0][2]==10.
    assert result["physical_event_times"][0][3]==20.


def test_receiver_cannot_insert_a_native_job_inside_its_take_block():
    tasks,base,current,_,routes,_,_=fixture()
    tasks[7]=((0.,0.),(0.,0.),100.)
    for topology in (base,current):
        topology["max_tasks_per_drone"]=3
        topology["task_relay_candidates"]["7"]=[]
    routes=(routes[0],routes[1],(102,7,-7,104,-102,-104))
    with pytest.raises(ValueError,match="contiguous OUT block"):
        routes_audit(tasks,base,current,routes)


def test_receiver_cannot_hold_a_third_parcel_at_joint_pickup():
    tasks,base,current,_,routes,_,_=fixture()
    tasks[7]=((0.,0.),(0.,0.),100.)
    for topology in (base,current):
        topology["max_tasks_per_drone"]=3
        topology["task_relay_candidates"]["7"]=[]
    routes=(routes[0],routes[1],(7,102,104,-7,-102,-104))
    with pytest.raises(ValueError,match="capacity"):
        routes_audit(tasks,base,current,routes)


def test_coincident_first_delivery_cannot_fake_positive_joint_flight():
    tasks,base,current,_,routes,_,_=fixture()
    tasks[1]=((-10.,0.),(0.,0.),12.)
    with pytest.raises(ValueError,match="positive-distance"):
        routes_audit(tasks,base,current,routes)


@pytest.mark.parametrize("field,value",[("home_points",[[-10.,0.],[10.,0.],[0.,0.]]),
    ("capacity",3),("max_tasks_per_drone",3),("speed_km_per_min",2.)])
def test_original_homes_and_resources_cannot_be_changed(field,value):
    tasks,base,current,_,routes,_,_=fixture()
    current[field]=value
    with pytest.raises(ValueError,match="base resource"):
        routes_audit(tasks,base,current,routes)


def test_original_registry_and_station_must_remain_and_new_catalog_must_be_used_exactly():
    tasks,base,current,_,routes,_,_=fixture()
    changed=deepcopy(current); changed["stations"][0][1]=24.
    with pytest.raises(ValueError,match="original station"):
        routes_audit(tasks,base,changed,routes)
    changed=deepcopy(current); changed["legs"]=changed["legs"][2:]
    changed["task_relay_candidates"]["3"]=[]
    with pytest.raises(ValueError,match="original transport leg"):
        routes_audit(tasks,base,changed,routes)
    changed=deepcopy(current)
    changed["legs"] += [dict(id=105,task_id=4,kind="RELAY_IN",relay_id=10),
                        dict(id=106,task_id=4,kind="RELAY_OUT",relay_id=10)]
    changed["task_relay_candidates"]["4"]=[10]
    with pytest.raises(ValueError,match="only four new"):
        routes_audit(tasks,base,changed,routes)
    current["stations"].append([11,2.,2.])
    with pytest.raises(ValueError,match="at most one"):
        routes_audit(tasks,base,current,routes)


def test_old_active_relay_is_forbidden_in_the_new_mobile_branch():
    tasks,base,current,_,routes,_,_=fixture()
    routes=((5,-5,101,-101,202,-202),(6,-6,103,-103,201,-201,4,-4),routes[2])
    with pytest.raises(ValueError,match="active old relay"):
        routes_audit(tasks,base,current,routes)


def test_exact_four_leg_catalog_cannot_include_an_unused_converted_task():
    tasks,base,current,_,_,_,_=fixture()
    routes=((5,-5,1,-1),(6,-6,103,-103,4,-4),(104,-104,3,-3))
    with pytest.raises(ValueError,match="unused new leg"):
        routes_audit(tasks,base,current,routes)


def test_source_bound_move_checks_actual_carrier_and_final_K_not_only_metadata():
    tasks,base,current,original,routes,_,move=fixture()
    wrong_sources=((5,-5,103,-103,3,-3),(6,-6,101,-101,4,-4),(104,102,-102,-104))
    assert routes_audit(tasks,base,current,wrong_sources)["passed"]
    with pytest.raises(ValueError,match="wrong original sender"):
        routes_audit(tasks,base,current,wrong_sources,base_routes=original,move=move)
    overquota=(routes[0]+(6,-6),tuple(v for v in routes[1] if abs(v)!=6),routes[2])
    with pytest.raises(ValueError,match="responsibility exceeds K"):
        routes_audit(tasks,base,current,overquota)


def test_physical_proof_alone_does_not_guess_original_final_responsibility():
    tasks,base,current,_,routes,_,_=fixture()
    result=routes_audit(tasks,base,current,routes)
    assert result["passed"] and not result["source_responsibility_proven"]
    assert result["source_mapping_audit"] is None


def test_explicit_real_base_and_move_prove_finalmap_and_native_event_preservation():
    tasks,base,current,original,routes,direct,move=fixture()
    wrong_final=((5,-5,101,-101,4,-4),(6,-6,103,-103,3,-3),routes[2])
    with pytest.raises(ValueError,match="final map differs"):
        routes_audit(tasks,base,current,wrong_final,base_routes=original,move=move)
    changed_native=((6,-6)+routes[0][2:],(5,-5)+routes[1][2:],routes[2])
    with pytest.raises(ValueError,match="unrelated event owner"):
        routes_audit(tasks,base,current,changed_native,base_routes=original,move=move)
    with pytest.raises(ValueError,match="Original mobile quota sources"):
        routes_audit(tasks,base,current,routes,base_routes=direct,move=move)
    with pytest.raises(ValueError,match="supplied together"):
        routes_audit(tasks,base,current,routes,base_routes=original)


def test_complete_canonical_payload_binds_all_contact_clocks_and_zero_cache():
    tasks,base,current,original,_,move,payload=payload_fixture()
    result=audit_mobile_repooling_payload(tasks,base,current,payload,base_routes=original,move=move)
    assert result["passed"] and result["source_responsibility_proven"]
    for field,value in (("source_aircraft",[1,0]),("receiver_aircraft",0),
        ("source_arrival_order",[2,1]),("receiver_loaded_wait_min",0.),
        ("source_drop_times_min",[12.,12.]),("receiver_load_before_takes",[False,1]),
        ("shared_double_loaded_km",2.),("ground_cache_min",2.),("compensation_task_ids",[4,3])):
        changed=deepcopy(payload); changed["metadata"]["mobile_meetings"][0][field]=value
        with pytest.raises(ValueError):
            audit_mobile_repooling_payload(tasks,base,current,changed,base_routes=original,move=move)
    changed=deepcopy(payload)
    changed["metadata"]["mobile_meetings"][0]["pickup_events"][0]["physical_time_min"]=12.
    with pytest.raises(ValueError,match="contact time"):
        audit_mobile_repooling_payload(tasks,base,current,changed)
    changed=deepcopy(payload); changed["score"]["distance_km"]-=1.
    with pytest.raises(ValueError,match="distance_km"):
        audit_mobile_repooling_payload(tasks,base,current,changed)


def test_missing_mobile_metadata_or_wrong_contact_leg_identity_is_rejected():
    tasks,base,current,_,_,_,payload=payload_fixture()
    changed=deepcopy(payload); changed["metadata"]["mobile_meetings"]=[]
    with pytest.raises(ValueError,match="meeting count"):
        audit_mobile_repooling_payload(tasks,base,current,changed)


def test_optional_legacy_shaped_aliases_cannot_forge_aircraft_load_or_time():
    tasks,base,current,original,_,move,payload=payload_fixture()
    good=deepcopy(payload)
    record=good["metadata"]["mobile_meetings"][0]
    record.update(aircraft=[0,1,2],own_task_groups=[[1],[2],[]],cut_positions=[3,3,0],
        source_task_ids=[1,2],take_order_task_ids=[1,2],waiting_loads=[1,1,0],
        synchronization_min=12.,receiver_waiting_empty_min=10.,receiver_waiting_one_parcel_min=2.,
        handoff_times_min=[10.,12.],repooling_move=deepcopy(move))
    assert audit_mobile_repooling_payload(tasks,base,current,good,base_routes=original,move=move)["passed"]
    for field,value in (("aircraft",[False,1,2]),("waiting_loads",[0,1,0]),
                       ("source_task_ids",[2,1]),("synchronization_min",10.)):
        changed=deepcopy(good); changed["metadata"]["mobile_meetings"][0][field]=value
        with pytest.raises(ValueError):
            audit_mobile_repooling_payload(tasks,base,current,changed)
    changed=deepcopy(good)
    changed["metadata"]["mobile_meetings"][0]["repooling_move"]["a"]=1
    with pytest.raises(ValueError,match="current sources"):
        audit_mobile_repooling_payload(tasks,base,current,changed)
    changed=deepcopy(payload)
    changed["metadata"]["mobile_meetings"][0]["drop_events"][0]["visit"]=-103
    with pytest.raises(ValueError,match="contact identity"):
        audit_mobile_repooling_payload(tasks,base,current,changed)


def test_no_new_site_ordinary_direct_and_fixed_relay_are_generic_not_mobile():
    tasks,base,_,original,_,direct,move=fixture()
    result=routes_audit(tasks,base,base,original)
    assert result["passed"] and result["model"]=="generic_fixed_network"
    assert not result["continuous_handoff_extension"] and result["meetings"]==[]
    payload=canonical_payload(tasks,base,original,{})
    assert audit_mobile_repooling_payload(tasks,base,base,payload)["passed"]
    move.update(mode="DIRECT",station_id=None,shared_double_loaded_km=0.)
    assert routes_audit(tasks,base,base,direct,base_routes=original,move=move)["source_responsibility_proven"]
    fixed=deepcopy(base)
    fixed["legs"] += [dict(id=101,task_id=1,kind="RELAY_IN",relay_id=9),
                      dict(id=102,task_id=1,kind="RELAY_OUT",relay_id=9)]
    fixed["task_relay_candidates"]["1"]=[9]
    routes=((5,-5,101,-101),(6,-6,2,-2),(102,-102,3,4,-3,-4))
    for topology in (fixed,):
        topology["max_tasks_per_drone"]=3
    result=routes_audit(tasks,fixed,fixed,routes)
    assert result["passed"] and not result["continuous_handoff_extension"]
    assert result["core_reference"]["relay_statistics"]["relay_task_count"]==1
