"""Independent six-task saturated fixed-station repooling fixtures."""
from copy import deepcopy
from math import sqrt

import pytest

from algorithm.experiments.audit_repooling import audit_repooling_proposal


def fixture():
    tasks={1:((-10.,0.),(0.,1.),12.),2:((10.,0.),(0.,2.),12.),
           3:((0.,0.),(-10.,0.),100.),4:((0.,0.),(10.,0.),100.),
           5:((-10.,0.),(-10.,0.),100.),6:((10.,0.),(10.,0.),100.)}
    topology=dict(stations=[[9,0.,0.],[10,0.,0.]],
        legs=[dict(id=identifier,task_id=task,kind=kind,relay_id=9)
              for identifier,task,kind in ((101,1,"RELAY_IN"),(102,1,"RELAY_OUT"),
                  (103,2,"RELAY_IN"),(104,2,"RELAY_OUT"),
                  (105,3,"RELAY_IN"),(106,3,"RELAY_OUT"))],
        home_points=[[-10.,0.],[10.,0.],[0.,0.]],drone_count=3,capacity=2,
        max_tasks_per_drone=2,speed_km_per_min=1.)
    base=((5,-5,1,-1),(6,-6,2,-2),(3,4,-3,-4))
    relay=((5,-5,101,-101,3,-3),(6,-6,103,-103,4,-4),(102,104,-102,-104))
    direct=((5,-5,3,-3),(6,-6,4,-4),(1,-1,2,-2))
    move=dict(i=1,j=2,k1=3,k2=4,a=0,b=1,c=2,mode="RELAY",station_id=9,
        final_aircraft=[2,2,0,1],source_aircraft=[0,1],receiver_aircraft=2,
        compensation_mode="DIRECT",shared_double_loaded_km=1.)
    return tasks,topology,base,relay,direct,move


def audit(tasks,topology,base,proposal,move,relay=True):
    return audit_repooling_proposal(tasks,topology,base,proposal,move,relay_enabled=relay)


def test_repooling_toy_saturates_every_final_quota_and_measures_real_shared_flight():
    tasks,topology,base,relay,_,move=fixture()
    result=audit(tasks,topology,base,relay,move)
    assert result["passed"] and result["final_tasks_per_aircraft"]==[2,2,2]
    assert result["actual_cross_uav_count"]==2
    assert result["score"]==dict(late_count=0,total_lateness_min=0.,distance_km=42.)
    assert result["delivery_times_min"]["1"]==11. and result["delivery_times_min"]["2"]==12.
    assert result["shared_loaded_distance_km"]==1.
    assert result["receiver_load_before_pickups"]==[0,1]
    assert result["event_times_min"][2]==[10.,10.,11.,12.]
    assert sum(h["aircraft_waiting_min"] for h in result["handoffs"])==10.
    assert sum(h["package_cache_min"] for h in result["handoffs"])==0.
    assert result["source_owners"]=={"1":0,"2":1,"3":2,"4":2,"5":0,"6":1}
    assert result["final_owners"]=={"1":2,"2":2,"3":0,"4":1,"5":0,"6":1}


def test_matched_direct_need_not_pool_but_its_true_geometry_is_measured():
    tasks,topology,base,_,direct,move=fixture()
    move.update(mode="DIRECT",station_id=None,shared_double_loaded_km=0.)
    result=audit(tasks,topology,base,direct,move,False)
    assert result["passed"] and result["actual_cross_uav_count"]==0
    assert result["score"]["late_count"]==2
    assert result["score"]["distance_km"]==pytest.approx(50.+2*sqrt(101.)+sqrt(104.))
    assert result["shared_loaded_distance_km"]==0.
    direct=(direct[0],direct[1],(1,2,-1,-2))
    move["shared_double_loaded_km"]=sqrt(101.)
    result=audit(tasks,topology,base,direct,move,False)
    assert result["shared_loaded_distance_km"]==pytest.approx(sqrt(101.))
    assert result["passed"]  # DIRECT has no positive-distance pooling requirement.


def test_different_final_mapping_cannot_count_as_repooling():
    tasks,topology,base,relay,_,move=fixture()
    wrong=((5,-5,101,-101,4,-4),(6,-6,103,-103,3,-3),relay[2])
    with pytest.raises(ValueError,match="required final map"):
        audit(tasks,topology,base,wrong,move)


def test_valid_full_plan_cannot_move_an_unrelated_native_task():
    tasks,topology,base,relay,_,move=fixture()
    wrong=((6,-6)+relay[0][2:],(5,-5)+relay[1][2:],relay[2])
    with pytest.raises(ValueError,match="unrelated event"):
        audit(tasks,topology,base,wrong,move)


def test_wrong_inbound_source_is_rejected_despite_correct_final_owners():
    tasks,topology,base,relay,_,move=fixture()
    wrong=((5,-5,103,-103,3,-3),(6,-6,101,-101,4,-4),relay[2])
    with pytest.raises(ValueError,match="wrong original source"):
        audit(tasks,topology,base,wrong,move)


def test_two_individually_valid_handoffs_must_use_one_same_station():
    tasks,topology,base,relay,_,move=fixture()
    for leg in topology["legs"]:
        if leg["task_id"]==2:
            leg["relay_id"]=10
    with pytest.raises(ValueError,match="wrong original source, final receiver or station"):
        audit(tasks,topology,base,relay,move)


def test_receiver_cannot_hide_a_loaded_parcel_before_its_take_block():
    tasks,topology,base,relay,_,move=fixture()
    # A complete compensating parcel on C would cause load 3 at the second
    # OUT pickup. The independent schedule rejects that capacity violation.
    wrong=(relay[0][:-2],relay[1],(3,102,104,-3,-102,-104))
    with pytest.raises(ValueError,match="capacity"):
        audit(tasks,topology,base,wrong,move)


def test_receiver_must_take_pair_together_before_delivering_either():
    tasks,topology,base,relay,_,move=fixture()
    wrong=(relay[0],relay[1],(102,-102,104,-104))
    with pytest.raises(ValueError,match="contiguous block"):
        audit(tasks,topology,base,wrong,move)


def test_same_point_first_delivery_cannot_fake_a_joint_loaded_flight():
    tasks,topology,base,relay,_,move=fixture()
    tasks[1]=((-10.,0.),(0.,0.),12.)
    move.pop("shared_double_loaded_km")
    with pytest.raises(ValueError,match="positive-distance"):
        audit(tasks,topology,base,relay,move)


def test_compensation_relay_cannot_replace_a_whole_direct_request():
    tasks,topology,base,relay,_,move=fixture()
    wrong=((5,-5,101,-101,106,-106),
           (6,-6,103,-103,105,-105,4,-4),relay[2])
    with pytest.raises(ValueError,match="compensation tasks must stay whole DIRECT"):
        audit(tasks,topology,base,wrong,move)


def test_same_aircraft_relay_cannot_be_counted_as_true_cross():
    tasks,topology,base,relay,_,move=fixture()
    wrong=((5,-5,3,-3),relay[1],(101,-101,102,104,-102,-104))
    with pytest.raises(ValueError,match="wrong original source"):
        audit(tasks,topology,base,wrong,move)


def test_forged_shared_distance_or_descriptive_fields_are_bound():
    tasks,topology,base,relay,_,move=fixture()
    for field,value in (("shared_double_loaded_km",2.),("final_aircraft",[2,2,1,0]),
                        ("source_aircraft",[1,0]),("receiver_aircraft",1),
                        ("compensation_mode","RELAY")):
        changed=deepcopy(move); changed[field]=value
        with pytest.raises(ValueError):
            audit(tasks,topology,base,relay,changed)


@pytest.mark.parametrize("field,value",[("a",False),("c",3),("k2",3),("mode","DIRECT"),("station_id",None)])
def test_identity_and_arm_fields_cannot_be_forged(field,value):
    tasks,topology,base,relay,_,move=fixture()
    move[field]=value
    with pytest.raises(ValueError):
        audit(tasks,topology,base,relay,move)


def test_half_relay_or_wrong_original_source_assignment_is_rejected():
    tasks,topology,base,relay,_,move=fixture()
    wrong=(relay[0],relay[1],(102,104,-102))
    with pytest.raises(ValueError):
        audit(tasks,topology,base,wrong,move)
    swapped_base=(base[1],base[0],base[2])
    with pytest.raises(ValueError,match="Base task ownership"):
        audit(tasks,topology,swapped_base,relay,move)


def test_initial_relay_or_relay_in_direct_control_cannot_be_hidden():
    tasks,topology,base,relay,_,move=fixture()
    with pytest.raises(ValueError,match="base must contain only DIRECT"):
        audit(tasks,topology,relay,relay,move)
    move.update(mode="DIRECT",station_id=None)
    with pytest.raises(ValueError,match="DIRECT repooling control cannot contain relay"):
        audit(tasks,topology,base,relay,move,False)


def test_global_dependency_cycle_is_rejected_before_any_partial_plan_is_adopted():
    tasks,topology,base,_,_,move=fixture()
    wrong=((5,-5,106,-106,101,-101),
           (6,-6,103,-103,4,-4),(102,104,-102,-104,105,-105))
    with pytest.raises(ValueError,match="dependency cycle"):
        audit(tasks,topology,base,wrong,move)


def test_final_delivery_K_is_independent_of_extra_inbound_transport_legs():
    tasks,topology,base,relay,_,move=fixture()
    wrong=((5,-5,6,-6,101,-101,3,-3),(103,-103,4,-4),relay[2])
    with pytest.raises(ValueError,match="responsibility exceeds K"):
        audit(tasks,topology,base,wrong,move)
