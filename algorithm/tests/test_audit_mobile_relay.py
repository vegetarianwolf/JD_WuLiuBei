"""Small independent mobile-meeting audit fixtures, without any optimizer."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from algorithm.experiments.audit_mobile_relay import audit_mobile_payload, audit_mobile_routes


def fixture():
    tasks = {1: ((-4., 0.), (4., 0.), 100.), 2: ((4., 0.), (-4., 0.), 100.)}
    base = dict(stations=[], legs=[], task_relay_candidates={"1": [], "2": []},
        home_points=[[-8., 0.], [4., 0.]], drone_count=2, capacity=2,
        max_tasks_per_drone=1, speed_km_per_min=1., relay_allowed=True)
    current = deepcopy(base)
    current["stations"] = [[1, 0., 0.]]
    current["legs"] = [dict(id=identifier, task_id=task, kind=kind, relay_id=1)
        for identifier, task, kind in ((101, 1, "RELAY_IN"), (102, 1, "RELAY_OUT"),
                                       (103, 2, "RELAY_IN"), (104, 2, "RELAY_OUT"))]
    current["task_relay_candidates"] = {"1": [1], "2": [1]}
    return tasks, base, current, ((101, -101, 104, -104), (103, -103, 102, -102))


def canonical_payload(tasks, topology, routes, metadata):
    # Production serialization is used only to prepare a tiny saved artifact;
    # the auditor itself imports none of these production modules.
    from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg, evaluate_solution
    from uav_dispatch.cli import result_payload
    p = Problem(tuple(Task(t, Point(*v[0]), Point(*v[1]), v[2]) for t, v in tasks.items()),
        drone_count=topology["drone_count"], capacity=topology["capacity"],
        max_tasks_per_drone=topology["max_tasks_per_drone"], speed_km_per_min=topology["speed_km_per_min"],
        drone_homes=tuple(Point(*x) for x in topology["home_points"]),
        relay_stations=tuple(RelayStation(*x) for x in topology["stations"]),
        leg_registry={x["id"]: TransportLeg(**x) for x in topology["legs"]},
        task_relay_candidates={int(k): tuple(v) for k, v in topology["task_relay_candidates"].items()})
    checked = evaluate_solution(p, routes, _force_full=True)
    result = SimpleNamespace(routes=routes, evaluation=checked, runtime_seconds=0., iterations=0, metadata=metadata)
    return result_payload(p, result, source="tiny-mobile-audit")


def test_physical_sync_holds_cargo_until_late_aircraft_arrives_and_preserves_score():
    tasks, base, current, routes = fixture()
    result = audit_mobile_routes(tasks, base, current, routes)
    assert result["passed"] and result["new_mobile_site_count"] == 1
    meeting = result["meetings"][0]
    assert meeting["arrival_times_min"] == [8., 4.]
    assert meeting["synchronization_min"] == 8.
    assert meeting["aircraft_waiting_min"] == [0., 4.]
    assert meeting["waiting_loads"] == [1, 1]
    assert result["total_mobile_aircraft_waiting_min"] == 4.
    assert result["mobile_ground_cache_min"] == 0.
    assert result["physical_event_times"] == [[4., 8., 8., 12.], [0., 8., 8., 12.]]
    assert result["core_reference"]["event_times_min"][1][1] == 4.
    assert result["core_reference"]["handoffs"][1]["package_cache_min"] == 4.
    assert result["score"] == dict(late_count=0, total_lateness_min=0., distance_km=20.)
    assert result["delivery_times_min"] == {"1": 12., "2": 12.}
    assert all(e["physical_time_min"] == 8. for e in meeting["drop_events"]+meeting["pickup_events"])


def test_complete_canonical_payload_and_physical_metadata_are_both_bound():
    tasks, base, current, routes = fixture()
    physical = audit_mobile_routes(tasks, base, current, routes)
    payload = canonical_payload(tasks, current, routes, {"mobile_meetings": physical["meetings"]})
    assert audit_mobile_payload(tasks, base, current, payload)["passed"]
    payload["metadata"]["mobile_meetings"][0]["drop_events"][1]["physical_time_min"] = 4.
    with pytest.raises(ValueError, match="physical handoff"):
        audit_mobile_payload(tasks, base, current, payload)


def test_missing_mobile_physics_or_forged_core_score_are_rejected():
    tasks, base, current, routes = fixture()
    payload = canonical_payload(tasks, current, routes, {})
    with pytest.raises(ValueError, match="meeting count"):
        audit_mobile_payload(tasks, base, current, payload)
    payload["metadata"]["mobile_meetings"] = audit_mobile_routes(tasks, base, current, routes)["meetings"]
    payload["score"]["distance_km"] -= 1.
    with pytest.raises(ValueError, match="distance_km"):
        audit_mobile_payload(tasks, base, current, payload)


@pytest.mark.parametrize("field,value", [
    ("capacity", 3), ("max_tasks_per_drone", 2), ("speed_km_per_min", 2.),
    ("home_points", [[-4., 0.], [4., 0.]]),
])
def test_no_free_resources_or_initial_reposition(field, value):
    tasks, base, current, routes = fixture()
    current[field] = value
    with pytest.raises(ValueError, match="base resource"):
        audit_mobile_routes(tasks, base, current, routes)


def test_original_fixed_station_cannot_be_moved():
    tasks, base, current, routes = fixture()
    base["stations"] = [[9, 100., 100.]]
    current["stations"].append([9, 99., 100.])
    with pytest.raises(ValueError, match="original station"):
        audit_mobile_routes(tasks, base, current, routes)


def test_original_transport_leg_cannot_be_removed_and_station_eligibility_is_preserved():
    tasks, base, current, routes = fixture()
    original = [dict(id=201, task_id=1, kind="RELAY_IN", relay_id=9),
                dict(id=202, task_id=1, kind="RELAY_OUT", relay_id=9)]
    for topology in (base, current):
        topology["stations"].append([9, 100., 100.])
        topology["legs"] += deepcopy(original)
        topology["task_relay_candidates"]["1"].append(9)
    assert audit_mobile_routes(tasks, base, current, routes)["passed"]
    changed = deepcopy(current)
    changed["legs"] = [leg for leg in changed["legs"] if leg["relay_id"] != 9]
    changed["task_relay_candidates"]["1"].remove(9)
    with pytest.raises(ValueError, match="original transport leg"):
        audit_mobile_routes(tasks, base, changed, routes)


def test_mobile_metadata_cannot_hide_boolean_ids_or_forged_wait():
    tasks, base, current, routes = fixture()
    physical = audit_mobile_routes(tasks, base, current, routes)
    payload = canonical_payload(tasks, current, routes, {"mobile_meetings": physical["meetings"]})
    changed = deepcopy(payload)
    changed["metadata"]["mobile_meetings"][0]["aircraft"] = [False, True]
    with pytest.raises(ValueError, match="Invalid saved mobile aircraft"):
        audit_mobile_payload(tasks, base, current, changed)
    changed = deepcopy(payload)
    changed["metadata"]["mobile_meetings"][0]["aircraft_waiting_min"] = [0., 0.]
    with pytest.raises(ValueError, match="aircraft_waiting_min"):
        audit_mobile_payload(tasks, base, current, changed)


def test_appended_unused_mobile_site_is_rejected():
    tasks, base, current, routes = fixture()
    current["stations"].append([2, 10., 10.])
    with pytest.raises(ValueError, match="exactly two UAVs"):
        audit_mobile_routes(tasks, base, current, routes)


def test_mobile_ground_cache_with_intervening_job_cannot_pass_as_synchronous():
    tasks, base, current, routes = fixture()
    tasks[3] = ((0., 0.), (0., 0.), 100.)
    for topology in (base, current):
        topology["max_tasks_per_drone"] = 2
        topology["task_relay_candidates"]["3"] = []
    routes = ((101, -101, 3, -3, 104, -104), routes[1])
    with pytest.raises(ValueError, match="not contiguous"):
        audit_mobile_routes(tasks, base, current, routes)


def test_two_each_group_keeps_load_two_while_waiting_without_ground_storage():
    tasks, base, current, _ = fixture()
    tasks[3] = ((-4., 0.), (4., 0.), 100.)
    tasks[4] = ((4., 0.), (-4., 0.), 100.)
    for topology in (base, current):
        topology["max_tasks_per_drone"] = 2
        topology["task_relay_candidates"].update({"3": [], "4": []})
    current["task_relay_candidates"].update({"3": [1], "4": [1]})
    current["legs"] += [dict(id=i, task_id=t, kind=k, relay_id=1)
        for i,t,k in ((105,3,"RELAY_IN"),(106,3,"RELAY_OUT"),(107,4,"RELAY_IN"),(108,4,"RELAY_OUT"))]
    routes = ((101,105,-101,-105,104,108,-104,-108),
              (103,107,-103,-107,102,106,-102,-106))
    result = audit_mobile_routes(tasks, base, current, routes)
    meeting = result["meetings"][0]
    assert meeting["group_size"] == 2 and meeting["waiting_loads"] == [2, 2]
    assert meeting["aircraft_waiting_min"] == [0., 4.]
    assert all(e["physical_time_min"] == 8. for e in meeting["drop_events"]+meeting["pickup_events"])
    assert result["score"]["distance_km"] == 20.


def test_ordinary_direct_initial_needs_no_new_site_and_uses_same_resources():
    tasks, base, _, _ = fixture()
    routes = ((1,-1),(2,-2))
    result = audit_mobile_routes(tasks, base, base, routes)
    assert result["passed"] and result["new_mobile_site_count"] == 0
    assert result["meetings"] == []
    payload = canonical_payload(tasks, base, routes, {})
    assert audit_mobile_payload(tasks, base, base, payload)["passed"]
