"""Tiny pinned continuation audits; no solver or synthetic base is used."""
from copy import deepcopy
from math import sqrt

import pytest

from algorithm.experiments.audit_pinned_repooling import audit_pinned_repooling_proposal
from algorithm.tests.test_audit_repooling import fixture


def audit(tasks,topology,base,macro,polished,move,relay=True):
    return audit_pinned_repooling_proposal(tasks,topology,base,macro,polished,move,relay_enabled=relay)


def extended_fixture():
    tasks,topology,base,macro,direct,move=fixture()
    tasks.update({7:((-10.,0.),(-10.,0.),100.),8:((10.,0.),(10.,0.),100.),
                  9:((0.,0.),(0.,0.),100.)})
    topology["max_tasks_per_drone"]=3
    base=((7,-7)+base[0],(8,-8)+base[1],(9,-9)+base[2])
    macro=((7,-7)+macro[0],(8,-8)+macro[1],(9,-9)+macro[2])
    direct=((7,-7)+direct[0],(8,-8)+direct[1],(9,-9)+direct[2])
    return tasks,topology,base,macro,direct,move


def test_legitimate_native_whole_exchange_is_allowed_with_pin_identity_and_quota():
    tasks,topology,base,macro,_,move=fixture()
    polished=((6,-6)+macro[0][2:],(5,-5)+macro[1][2:],macro[2])
    result=audit(tasks,topology,base,macro,polished,move)
    assert result["passed"] and result["actual_cross_uav_count"]==2
    assert result["native_owner_changed_count"]==2
    assert result["native_owner_changed_task_ids"]==[5,6]
    assert result["native_event_sequence_changed_aircraft"]==[0,1]
    assert result["pinned_event_subsequences"]==[[101,-101,3,-3],[103,-103,4,-4],[102,104,-102,-104]]
    assert result["final_tasks_per_aircraft"]==[2,2,2]
    assert result["shared_loaded_distance_km"]==1.
    assert result["macro_score"]["distance_km"]==42.
    assert result["event_times_min"][2]==[50.,50.,51.,52.]
    assert sum(h["aircraft_waiting_min"] for h in result["handoffs"])==50.


def test_native_reordering_is_allowed_without_claiming_final_owner_change():
    tasks,topology,base,macro,_,move=extended_fixture()
    polished=((5,-5,7,-7)+macro[0][4:],macro[1],macro[2])
    result=audit(tasks,topology,base,macro,polished,move)
    assert result["passed"] and result["native_owner_changed_count"]==0
    assert result["native_event_sequence_changed_aircraft"]==[0]


def test_native_interleaving_can_change_flights_without_changing_native_owner_or_order():
    tasks,topology,base,macro,_,move=fixture()
    polished=((101,-101,5,-5,3,-3),macro[1],macro[2])
    result=audit(tasks,topology,base,macro,polished,move)
    assert result["passed"] and result["native_owner_changed_count"]==0
    assert result["native_event_sequence_changed_aircraft"]==[]
    assert result["full_event_sequence_changed_aircraft"]==[0]


def test_direct_control_can_reassign_native_tasks_and_keeps_actual_shared_geometry():
    tasks,topology,base,_,direct,move=fixture()
    move.update(mode="DIRECT",station_id=None,shared_double_loaded_km=0.)
    polished=((6,-6)+direct[0][2:],(5,-5)+direct[1][2:],direct[2])
    result=audit(tasks,topology,base,direct,polished,move,False)
    assert result["passed"] and result["actual_cross_uav_count"]==0
    assert result["native_owner_changed_count"]==2
    assert result["shared_loaded_distance_km"]==0.


def test_pin_owner_and_pin_signed_order_changes_are_rejected():
    tasks,topology,base,macro,_,move=fixture()
    wrong_owner=((5,-5,101,-101,4,-4),(6,-6,103,-103,3,-3),macro[2])
    with pytest.raises(ValueError,match="selected mode, transport leg, station, source or final owner"):
        audit(tasks,topology,base,macro,wrong_owner,move)
    wrong_order=((5,-5,3,-3,101,-101),macro[1],macro[2])
    with pytest.raises(ValueError,match="signed-event subsequence"):
        audit(tasks,topology,base,macro,wrong_order,move)


def test_new_native_relay_cannot_be_added_even_with_same_owner_and_pin_sequence():
    tasks,topology,base,macro,_,move=fixture()
    topology["legs"] += [dict(id=109,task_id=5,kind="RELAY_IN",relay_id=9),
                         dict(id=110,task_id=5,kind="RELAY_OUT",relay_id=9)]
    polished=((110,-110,101,-101,3,-3),macro[1],(109,-109)+macro[2])
    with pytest.raises(ValueError,match="introduced or removed a relay plan"):
        audit(tasks,topology,base,macro,polished,move)


def test_selected_relay_cannot_be_cancelled_or_changed_to_direct():
    tasks,topology,base,macro,_,move=fixture()
    polished=((5,-5,3,-3),macro[1],(1,104,-1,-104))
    with pytest.raises(ValueError,match="selected mode, transport leg"):
        audit(tasks,topology,base,macro,polished,move)


def test_pin_subsequence_alone_does_not_allow_native_job_inside_receiver_take_block():
    tasks,topology,base,macro,_,move=extended_fixture()
    polished=(macro[0],macro[1],(102,9,-9,104,-102,-104))
    with pytest.raises(ValueError,match="OUT pickups must remain contiguous"):
        audit(tasks,topology,base,macro,polished,move)


def test_receiver_capacity_still_checked_for_native_insertions():
    tasks,topology,base,macro,_,move=extended_fixture()
    polished=(macro[0],macro[1],(9,102,104,-9,-102,-104))
    with pytest.raises(ValueError,match="capacity"):
        audit(tasks,topology,base,macro,polished,move)


def test_final_quotas_preserved_even_when_changed_counts_are_below_K():
    tasks,topology,base,macro,_,move=extended_fixture()
    topology["max_tasks_per_drone"]=4
    polished=(macro[0]+(6,-6),tuple(v for v in macro[1] if abs(v)!=6),macro[2])
    with pytest.raises(ValueError,match="final delivery quotas"):
        audit(tasks,topology,base,macro,polished,move)


def test_global_wait_offset_reaches_all_downstream_native_delivery_clocks():
    tasks,topology,base,macro,_,move=extended_fixture()
    tasks[9]=((11.,0.),(12.,0.),10.)
    base=(base[0],base[1],base[2][2:]+(9,-9))
    macro=(macro[0],macro[1],macro[2][2:]+(9,-9))
    result=audit(tasks,topology,base,macro,macro,move)
    assert result["delivery_times_min"]["9"]==pytest.approx(13.+sqrt(125.))
    assert result["score"]["late_count"]==1
    assert result["score"]["total_lateness_min"]==pytest.approx(3.+sqrt(125.))
    assert result["native_owner_changed_count"]==0


def test_new_cycle_is_rejected_before_any_finite_candidate_score_is_claimed():
    tasks,topology,base,macro,_,move=fixture()
    topology["legs"] += [dict(id=109,task_id=5,kind="RELAY_IN",relay_id=9),
                         dict(id=110,task_id=5,kind="RELAY_OUT",relay_id=9)]
    polished=((110,-110,101,-101,3,-3),macro[1],macro[2]+(109,-109))
    with pytest.raises(ValueError,match="dependency cycle"):
        audit(tasks,topology,base,macro,polished,move)


def test_fake_original_source_or_synthetic_final_mapping_base_is_rejected():
    tasks,topology,base,macro,direct,move=fixture()
    wrong=deepcopy(move); wrong.update(a=1,b=0,source_aircraft=[1,0],final_aircraft=[2,2,1,0])
    with pytest.raises(ValueError,match="Base task ownership"):
        audit(tasks,topology,base,macro,macro,wrong)
    with pytest.raises(ValueError,match="Base task ownership"):
        audit(tasks,topology,direct,macro,macro,move)


def test_original_macro_must_be_valid_before_pin_continuation_is_considered():
    tasks,topology,base,macro,_,move=fixture()
    invalid=(macro[0],macro[1],(102,104,-102))
    with pytest.raises(ValueError):
        audit(tasks,topology,base,invalid,macro,move)
    tasks[1]=((-10.,0.),(0.,0.),12.)
    move.pop("shared_double_loaded_km")
    with pytest.raises(ValueError,match="positive-distance"):
        audit(tasks,topology,base,macro,macro,move)


def test_saved_shared_geometry_remains_bound_after_pin_polish():
    tasks,topology,base,macro,_,move=fixture()
    move["shared_double_loaded_km"]=2.
    with pytest.raises(ValueError,match="shared loaded distance"):
        audit(tasks,topology,base,macro,macro,move)
