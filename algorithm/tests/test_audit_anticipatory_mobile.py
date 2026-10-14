"""Tiny independent receiver-first handoff audits; no optimizer is run."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from algorithm.experiments.audit_anticipatory_mobile import (
    audit_anticipatory_payload, audit_anticipatory_routes,
)


def fixture(group_size=1):
    tasks = {1: ((10., 0.), (0., 1.), 10.), 2: ((10., 0.), (10., 0.), 100.)}
    if group_size == 2:
        tasks[3] = ((10., 0.), (0., 2.), 100.)
    base = dict(stations=[], legs=[], task_relay_candidates={str(t): [] for t in tasks},
        home_points=[[10., 0.], [5., 0.]], drone_count=2, capacity=2,
        max_tasks_per_drone=group_size, speed_km_per_min=1., relay_allowed=True)
    current = deepcopy(base)
    current["stations"] = [[1, 5., 0.]]
    selected = [1] if group_size == 1 else [1, 3]
    for index, task in enumerate(selected):
        current["legs"] += [dict(id=101+2*index, task_id=task, kind="RELAY_IN", relay_id=1),
                            dict(id=102+2*index, task_id=task, kind="RELAY_OUT", relay_id=1)]
        current["task_relay_candidates"][str(task)] = [1]
    pickups = tuple(101+2*i for i in range(group_size))
    takes = tuple(102+2*i for i in range(group_size))
    routes = ((2, -2)+pickups+tuple(-x for x in pickups), takes+tuple(-x for x in takes))
    return tasks, base, current, routes


def canonical_payload(tasks, topology, routes, metadata):
    # Only this fixture uses production serialization. The auditor imports no
    # solver/evaluator and independently rebuilds all flights and cargo clocks.
    from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg, evaluate_solution
    from uav_dispatch.cli import result_payload
    problem = Problem(tuple(Task(t, Point(*v[0]), Point(*v[1]), v[2]) for t, v in tasks.items()),
        drone_count=topology["drone_count"], capacity=topology["capacity"],
        max_tasks_per_drone=topology["max_tasks_per_drone"], speed_km_per_min=topology["speed_km_per_min"],
        drone_homes=tuple(Point(*x) for x in topology["home_points"]),
        relay_stations=tuple(RelayStation(*x) for x in topology["stations"]),
        leg_registry={x["id"]: TransportLeg(**x) for x in topology["legs"]},
        task_relay_candidates={int(k): tuple(v) for k, v in topology["task_relay_candidates"].items()})
    checked = evaluate_solution(problem, routes, _force_full=True)
    result = SimpleNamespace(routes=routes, evaluation=checked, runtime_seconds=0., iterations=0, metadata=metadata)
    return result_payload(problem, result, source="tiny-anticipatory-audit")


@pytest.mark.parametrize("size", [1, 2])
def test_receiver_waits_empty_sender_keeps_all_cargo_and_no_cache(size):
    tasks, base, current, routes = fixture(size)
    result = audit_anticipatory_routes(tasks, base, current, routes)
    meeting = result["meetings"][0]
    assert result["passed"] and result["branch"] == "receiver_arrives_first_only"
    assert meeting["aircraft"] == [0, 1]
    assert meeting["own_task_groups"] == [[1] if size == 1 else [1, 3], []]
    assert meeting["arrival_times_min"] == [5., 0.]
    assert meeting["synchronization_min"] == 5.
    assert meeting["aircraft_waiting_min"] == [0., 5.]
    assert meeting["waiting_loads"] == [size, 0]
    assert result["total_mobile_aircraft_waiting_min"] == 5.
    assert result["mobile_ground_cache_min"] == 0.
    assert result["physical_event_times"] == result["core_reference"]["event_times_min"]
    assert all(e["physical_time_min"] == 5. for e in meeting["drop_events"]+meeting["pickup_events"])
    assert result["delivery_times_min"]["1"] == pytest.approx(5.+26.**.5)
    assert result["score"]["late_count"] == 1
    assert result["score"]["total_lateness_min"] == pytest.approx(26.**.5-5.)
    assert result["final_delivery_owner"]["1"] == 1 and result["final_delivery_owner"]["2"] == 0


def test_exact_equal_arrivals_are_valid():
    tasks, base, current, routes = fixture()
    for topology in (base, current):
        topology["home_points"][1] = [0., 0.]
    meeting = audit_anticipatory_routes(tasks, base, current, routes)["meetings"][0]
    assert meeting["arrival_times_min"] == [5., 5.]
    assert meeting["aircraft_waiting_min"] == [0., 0.]


def test_receiver_wait_propagates_to_every_downstream_direct_delivery():
    tasks, base, current, routes = fixture()
    tasks[3] = ((0., 1.), (0., 2.), 100.)
    for topology in (base, current):
        topology["max_tasks_per_drone"] = 2
        topology["task_relay_candidates"]["3"] = []
    routes = (routes[0], routes[1]+(3, -3))
    result = audit_anticipatory_routes(tasks, base, current, routes)
    assert result["physical_event_times"][1] == pytest.approx([5., 5.+26.**.5, 5.+26.**.5, 6.+26.**.5])
    assert result["delivery_times_min"]["3"] == pytest.approx(6.+26.**.5)


def test_sender_first_ground_cache_is_rejected_instead_of_inventing_sender_wait():
    tasks, base, current, routes = fixture()
    for topology in (base, current):
        topology["home_points"][1] = [-10., 0.]
    with pytest.raises(ValueError, match="Receiver must arrive no later"):
        audit_anticipatory_routes(tasks, base, current, routes)


def test_single_package_receiver_with_existing_cargo_is_outside_empty_branch():
    tasks, base, current, routes = fixture()
    tasks[3] = ((5., 0.), (5., 0.), 100.)
    for topology in (base, current):
        topology["max_tasks_per_drone"] = 2
        topology["task_relay_candidates"]["3"] = []
    routes = (routes[0], (3, 102, -102, -3))
    with pytest.raises(ValueError, match="receiver must arrive empty"):
        audit_anticipatory_routes(tasks, base, current, routes)


def test_two_package_receiver_cannot_exceed_capacity():
    tasks, base, current, routes = fixture(2)
    tasks[4] = ((5., 0.), (5., 0.), 100.)
    for topology in (base, current):
        topology["max_tasks_per_drone"] = 3
        topology["task_relay_candidates"]["4"] = []
    routes = (routes[0], (4,)+routes[1]+(-4,))
    with pytest.raises(ValueError, match="capacity"):
        audit_anticipatory_routes(tasks, base, current, routes)


def test_sender_two_drop_block_cannot_hide_an_intervening_direct_job():
    tasks, base, current, routes = fixture(2)
    tasks[4] = ((5., 0.), (5., 0.), 100.)
    for topology in (base, current):
        topology["task_relay_candidates"]["4"] = []
    routes = ((2, -2, 101, 103, -101, 4, -4, -103), routes[1])
    with pytest.raises(ValueError, match="sender drop block is not contiguous"):
        audit_anticipatory_routes(tasks, base, current, routes)


def test_receiver_two_take_block_cannot_hide_an_intervening_direct_job():
    tasks, base, current, routes = fixture(2)
    tasks[4] = ((5., 0.), (5., 0.), 100.)
    for topology in (base, current):
        topology["max_tasks_per_drone"] = 3
        topology["task_relay_candidates"]["4"] = []
    routes = (routes[0], (102, 4, -4, 104, -102, -104))
    with pytest.raises(ValueError, match="receiver take block is not contiguous"):
        audit_anticipatory_routes(tasks, base, current, routes)


def test_same_aircraft_transfer_is_not_a_cross_aircraft_handoff():
    tasks, base, current, routes = fixture()
    for topology in (base, current):
        topology["max_tasks_per_drone"] = 2
    routes = (routes[0]+routes[1], ())
    with pytest.raises(ValueError, match="one distinct receiver"):
        audit_anticipatory_routes(tasks, base, current, routes)


@pytest.mark.parametrize("field,value", [
    ("capacity", 3), ("drone_count", 3), ("max_tasks_per_drone", 2),
    ("speed_km_per_min", 2.), ("home_points", [[10., 0.], [0., 0.]]),
])
def test_original_fleet_capacity_K_speed_and_homes_are_immutable(field, value):
    tasks, base, current, routes = fixture()
    current[field] = value
    with pytest.raises(ValueError):
        audit_anticipatory_routes(tasks, base, current, routes)


def test_original_fixed_network_preserved_but_cannot_be_used_in_new_branch():
    tasks, base, current, routes = fixture()
    original = [dict(id=201, task_id=2, kind="RELAY_IN", relay_id=9),
                dict(id=202, task_id=2, kind="RELAY_OUT", relay_id=9)]
    for topology in (base, current):
        topology["stations"].append([9, 10., 0.])
        topology["legs"] += deepcopy(original)
        topology["task_relay_candidates"]["2"].append(9)
    assert audit_anticipatory_routes(tasks, base, current, routes)["passed"]
    changed = deepcopy(current)
    changed["stations"][-1][1] = 9.
    with pytest.raises(ValueError, match="original station"):
        audit_anticipatory_routes(tasks, base, changed, routes)
    changed = deepcopy(current)
    changed["legs"] = [x for x in changed["legs"] if x["relay_id"] != 9]
    changed["task_relay_candidates"]["2"].remove(9)
    with pytest.raises(ValueError, match="original transport leg"):
        audit_anticipatory_routes(tasks, base, changed, routes)
    routes = ((201, -201, 202, -202, 101, -101), routes[1])
    with pytest.raises(ValueError, match="pre-existing relay plans"):
        audit_anticipatory_routes(tasks, base, current, routes)


def test_unused_registered_mobile_leg_or_second_mobile_site_cannot_pass():
    tasks, base, current, routes = fixture()
    changed = deepcopy(current)
    changed["legs"] += [dict(id=103, task_id=2, kind="RELAY_IN", relay_id=1),
                        dict(id=104, task_id=2, kind="RELAY_OUT", relay_id=1)]
    changed["task_relay_candidates"]["2"] = [1]
    with pytest.raises(ValueError, match="unused registered transport legs"):
        audit_anticipatory_routes(tasks, base, changed, routes)
    current["stations"].append([2, 5., 1.])
    with pytest.raises(ValueError, match="single new meeting site"):
        audit_anticipatory_routes(tasks, base, current, routes)


def test_complete_payload_binds_physical_wait_type_and_all_event_identities():
    tasks, base, current, routes = fixture(2)
    physical = audit_anticipatory_routes(tasks, base, current, routes)
    payload = canonical_payload(tasks, current, routes, {"mobile_meetings": physical["meetings"]})
    assert audit_anticipatory_payload(tasks, base, current, payload)["passed"]
    for field, value in (("meeting_type", "sender_arrives_first"),
                         ("aircraft_waiting_min", [0., 0.]),
                         ("arrival_times_min", [5., 5.]),
                         ("waiting_loads", [0, 0]), ("ground_cache_min", 5.)):
        changed = deepcopy(payload)
        changed["metadata"]["mobile_meetings"][0][field] = value
        with pytest.raises(ValueError):
            audit_anticipatory_payload(tasks, base, current, changed)
    changed = deepcopy(payload)
    changed["metadata"]["mobile_meetings"][0]["pickup_events"][1]["physical_time_min"] = 0.
    with pytest.raises(ValueError, match="physical handoff"):
        audit_anticipatory_payload(tasks, base, current, changed)
    changed = deepcopy(payload)
    changed["score"]["distance_km"] -= 1.
    with pytest.raises(ValueError, match="distance_km"):
        audit_anticipatory_payload(tasks, base, current, changed)


def test_missing_metadata_is_rejected_and_plain_direct_initial_is_valid():
    tasks, base, current, routes = fixture()
    payload = canonical_payload(tasks, current, routes, {})
    with pytest.raises(ValueError, match="meeting count"):
        audit_anticipatory_payload(tasks, base, current, payload)
    direct = ((2, -2), (1, -1))
    audited = audit_anticipatory_routes(tasks, base, base, direct)
    assert audited["passed"] and not audited["meetings"]
    assert audited["new_mobile_site_count"] == 0
    payload = canonical_payload(tasks, base, direct, {})
    assert audit_anticipatory_payload(tasks, base, base, payload)["passed"]


def test_final_responsibility_K_cannot_be_hidden_by_relay_in_not_counting():
    tasks, base, current, routes = fixture(2)
    for topology in (base, current):
        topology["max_tasks_per_drone"] = 1
    with pytest.raises(ValueError, match="responsibility exceeds K"):
        audit_anticipatory_routes(tasks, base, current, routes)
