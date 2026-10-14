"""Independent two-pin semantics, actual owner telemetry and full timelines."""
from copy import deepcopy
from math import sqrt

import pytest

from algorithm.experiments.audit_pair_pinned_repooling import audit_pair_pinned_repooling_proposal
from algorithm.experiments.audit_pinned_repooling import audit_pinned_repooling_proposal
from algorithm.tests.test_audit_repooling import fixture
from algorithm.tests.test_audit_pinned_repooling import extended_fixture


def audit(tasks, graph, base, macro, post, move, relay=True, **kwargs):
    return audit_pair_pinned_repooling_proposal(tasks, graph, base, macro, post, move,
                                              relay_enabled=relay, **kwargs)


def compensation_swap(macro):
    return (tuple(4 if v == 3 else -4 if v == -3 else v for v in macro[0]),
            tuple(3 if v == 4 else -3 if v == -4 else v for v in macro[1]), macro[2])


@pytest.mark.parametrize("relay", [False, True])
def test_released_compensation_really_changes_owner_with_exact_two_pins(relay):
    tasks, graph, base, macro, direct, move = fixture()
    if not relay:
        macro = direct
        move.update(mode="DIRECT", station_id=None, shared_double_loaded_km=0.)
    post = compensation_swap(macro)
    result = audit(tasks, graph, base, macro, post, move, relay)
    assert result["passed"] and result["pinned_task_ids"] == [1, 2]
    assert result["released_compensation_task_ids"] == [3, 4]
    assert result["compensation_owner_changed_task_ids"] == [3, 4]
    assert result["actual_final_owners"] == result["final_owners"]
    assert result["compensation_actual_final_owners"] == {"3": 1, "4": 0}
    assert result["original_macro_final_aircraft"] == [2, 2, 0, 1]
    assert result["final_tasks_per_aircraft"] == [2, 2, 2]
    assert result["actual_cross_uav_count"] == (2 if relay else 0)
    with pytest.raises(ValueError, match="selected mode"):
        audit_pinned_repooling_proposal(tasks, graph, base, macro, post, move, relay_enabled=relay)


def test_other_unpinned_tasks_and_compensation_order_can_change():
    tasks, graph, base, macro, _, move = extended_fixture()
    post = compensation_swap(macro)
    post = ((6,-6,4,-4,101,-101,7,-7),
            (5,-5,3,-3,103,-103,8,-8),post[2])
    result = audit(tasks, graph, base, macro, post, move)
    assert result["unpinned_owner_changed_task_ids"] == [3, 4, 5, 6]
    assert result["compensation_owner_changed_count"] == 2
    assert result["pinned_event_subsequences"] == [[101,-101],[103,-103],[102,104,-102,-104]]


@pytest.mark.parametrize("kind", ["source", "receiver", "leg", "order"])
def test_two_pinned_transport_identities_and_signed_order_cannot_change(kind):
    tasks, graph, base, macro, _, move = fixture()
    if kind == "source":
        post = ((5,-5,103,-103,3,-3),(6,-6,101,-101,4,-4),macro[2])
    elif kind == "receiver":
        post = ((5,-5,101,-101,3,-3,102,-102),macro[1],(104,-104))
        graph["max_tasks_per_drone"] = 3
    elif kind == "leg":
        graph["legs"] += [dict(id=111,task_id=1,kind="RELAY_IN",relay_id=10),
                          dict(id=112,task_id=1,kind="RELAY_OUT",relay_id=10)]
        post = (tuple(111 if v==101 else -111 if v==-101 else v for v in macro[0]),macro[1],
                tuple(112 if v==102 else -112 if v==-102 else v for v in macro[2]))
    else:
        post = (macro[0],macro[1],(104,102,-102,-104))
    with pytest.raises(ValueError, match="pinned mode|signed-event"):
        audit(tasks, graph, base, macro, post, move)


def test_pin_cannot_be_cancelled_and_compensation_cannot_gain_relay_plan():
    tasks, graph, base, macro, _, move = fixture()
    cancelled = ((5,-5,3,-3),macro[1],(1,104,-1,-104))
    with pytest.raises(ValueError, match="pinned mode"):
        audit(tasks, graph, base, macro, cancelled, move)
    extra = ((5,-5,101,-101,106,-106),(6,-6,103,-103,105,-105,4,-4),macro[2])
    with pytest.raises(ValueError, match="introduced or removed"):
        audit(tasks, graph, base, macro, extra, move)


def test_out_block_cannot_be_split_by_an_unpinned_task():
    tasks, graph, base, macro, _, move = extended_fixture()
    post = (macro[0],macro[1],(102,9,-9,104,-102,-104))
    with pytest.raises(ValueError, match="contiguous"):
        audit(tasks, graph, base, macro, post, move)


def test_capacity_and_exact_quota_remain_checked_for_all_unpinned_tasks():
    tasks, graph, base, macro, _, move = extended_fixture()
    loaded = (macro[0],macro[1],(9,102,104,-9,-102,-104))
    with pytest.raises(ValueError, match="capacity"):
        audit(tasks, graph, base, macro, loaded, move)
    graph["max_tasks_per_drone"] = 4
    unbalanced = (macro[0]+(6,-6),tuple(v for v in macro[1] if abs(v)!=6),macro[2])
    with pytest.raises(ValueError, match="final delivery quotas"):
        audit(tasks, graph, base, macro, unbalanced, move)


def test_positive_joint_flight_is_checked_again_after_native_reinsertion():
    tasks, graph, base, macro, _, move = extended_fixture()
    post = (macro[0],macro[1],(102,104,-102,9,-9,-104))
    result = audit(tasks, graph, base, macro, post, move)
    # The original move's shared distance is raw history, not the post value.
    assert result["shared_loaded_distance_km"] == 1.
    tasks[9] = ((0.,3.),(0.,3.),100.)
    post = (macro[0],macro[1],(102,104,9,-9,-102,-104))
    with pytest.raises(ValueError, match="capacity"):
        audit(tasks, graph, base, macro, post, move)
    # A zero-length first delivery is already illegal in raw provenance.
    tasks[1] = ((-10.,0.),(0.,0.),12.)
    move.pop("shared_double_loaded_km")
    with pytest.raises(ValueError, match="positive-distance"):
        audit(tasks, graph, base, macro, macro, move)


def test_wait_offsets_and_downstream_deadlines_are_independently_propagated():
    tasks, graph, base, macro, _, move = fixture()
    post = ((6,-6)+macro[0][2:],(5,-5)+macro[1][2:],macro[2])
    result = audit(tasks, graph, base, macro, post, move)
    assert result["event_times_min"][2] == [50.,50.,51.,52.]
    assert result["delivery_times_min"]["1"] == 51.
    assert result["score"]["late_count"] == 2
    assert result["score"]["total_lateness_min"] == 79.


def test_fake_base_raw_owner_or_incomplete_macro_cannot_be_replaced_by_valid_post():
    tasks, graph, base, macro, direct, move = fixture()
    with pytest.raises(ValueError, match="Base task ownership"):
        audit(tasks, graph, direct, macro, macro, move)
    bad_move = deepcopy(move)
    bad_move.update(a=1,b=0,source_aircraft=[1,0],final_aircraft=[2,2,1,0])
    with pytest.raises(ValueError, match="Base task ownership"):
        audit(tasks, graph, base, macro, macro, bad_move)
    with pytest.raises(ValueError):
        audit(tasks, graph, base, (macro[0],macro[1],(102,104,-102)), macro, move)


def test_new_global_cycle_is_rejected_before_any_post_score_is_claimed():
    tasks, graph, base, macro, _, move = fixture()
    graph["legs"] += [dict(id=109,task_id=5,kind="RELAY_IN",relay_id=9),
                      dict(id=110,task_id=5,kind="RELAY_OUT",relay_id=9)]
    post = ((110,-110,101,-101,3,-3),macro[1],macro[2]+(109,-109))
    with pytest.raises(ValueError, match="dependency cycle"):
        audit(tasks, graph, base, macro, post, move)


def saved_post(move, result):
    post_move = deepcopy(move)
    post_move.pop("final_aircraft")
    post_move.update(semantic="post_pair_polish",shared_double_loaded_km=result["shared_loaded_distance_km"])
    return dict(actual_final_owners=deepcopy(result["actual_final_owners"]),pinned_task_ids=[1,2],
                released_compensation_task_ids=[3,4],original_macro_move=deepcopy(move),
                pin_scope="relay_pair_only",score=deepcopy(result["score"]),move=post_move)


def test_post_telemetry_binds_actual_compensation_owners_without_reusing_historical_map():
    tasks, graph, base, macro, _, move = fixture()
    post = compensation_swap(macro)
    result = audit(tasks, graph, base, macro, post, move)
    saved = saved_post(move,result)
    assert audit(tasks, graph, base, macro, post, move, telemetry=saved)["passed"]
    # An explicit current map is allowed only when it equals actual owners.
    saved["move"]["final_aircraft"] = [2,2,1,0]
    assert audit(tasks, graph, base, macro, post, move, telemetry=saved)["passed"]
    saved["move"]["final_aircraft"] = [2,2,0,1]
    with pytest.raises(ValueError, match="historical and actual"):
        audit(tasks, graph, base, macro, post, move, telemetry=saved)


@pytest.mark.parametrize("field", ["actual_final_owners", "original_macro_move", "shared", "scope", "pins", "score"])
def test_forged_post_telemetry_is_rejected(field):
    tasks, graph, base, macro, _, move = fixture()
    post = compensation_swap(macro)
    result = audit(tasks, graph, base, macro, post, move)
    saved = saved_post(move,result)
    if field == "actual_final_owners": saved[field]["3"] = 0
    elif field == "original_macro_move": saved[field]["a"] = 1
    elif field == "shared": saved["move"]["shared_double_loaded_km"] = 99.
    elif field == "scope": saved["pin_scope"] = "four_tasks"
    elif field == "pins": saved["pinned_task_ids"] = [1,2,3,4]
    else: saved["score"]["distance_km"] += 1.
    with pytest.raises(ValueError):
        audit(tasks, graph, base, macro, post, move, telemetry=saved)
