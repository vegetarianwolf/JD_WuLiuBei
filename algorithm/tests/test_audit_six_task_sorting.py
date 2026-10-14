"""Independent triangle sorting, asynchronous native costs and forgery tests."""
from copy import deepcopy
from math import sqrt

import pytest

from algorithm.experiments.audit_six_task_sorting import audit_six_task_sorting_proposal
from algorithm.experiments.audit_weighted_relay_ablation import reference_schedule


def fixture(*,native=False,slow=False):
    vertices=((0.,0.),(10.,0.),(5.,5.*sqrt(3.)));h=(5.,5.*sqrt(3.)/3.)
    source_groups=((1,2),(3,4),(5,6));destinations=(1,2,0,2,0,1)
    tasks={t:(vertices[(t-1)//2],vertices[destinations[t-1]],100. if native else 13.) for t in range(1,7)}
    topology=dict(stations=[[9,*h],[10,*h]],
        legs=[dict(id=100+2*(t-1)+offset,task_id=t,kind=kind,relay_id=9)
              for t in range(1,7) for offset,kind in ((1,"RELAY_IN"),(2,"RELAY_OUT"))],
        home_points=[list(p) for p in vertices],drone_count=3,capacity=2,
        max_tasks_per_drone=3 if native else 2,speed_km_per_min=1.)
    if slow:topology["home_points"][2][1]+=10.
    base=tuple(tuple([*g,*(-t for t in g)]) for g in source_groups)
    final_groups=((3,5),(1,6),(2,4))
    def inleg(t):return 100+2*(t-1)+1
    def outleg(t):return inleg(t)+1
    relay=tuple(tuple([*(inleg(t) for t in own),*(-inleg(t) for t in own),
                        *(outleg(t) for t in received),*(-outleg(t) for t in received)])
                for own,received in zip(source_groups,final_groups))
    if native:
        tasks[7]=(vertices[2],vertices[2],25.)
        base=(base[0]+(7,-7),*base[1:]);relay=(relay[0]+(7,-7),*relay[1:])
    family=dict(station_id=9,aircraft=[0,1,2],source_task_groups=[list(g) for g in source_groups],
                task_ids=list(range(1,7)),priority=[0.])
    move=dict(family=family,mode="RELAY",station_id=9,aircraft=[0,1,2],
        source_task_groups=[list(g) for g in source_groups],final_task_groups=[list(g) for g in final_groups],
        final_aircraft_by_task={str(t):a for a,g in enumerate(final_groups) for t in g})
    return tasks,topology,base,relay,move


def audit(data,*,routes=None,move=None,relay=True,telemetry=None,**kwargs):
    tasks,topology,base,proposal,descriptor=data
    return audit_six_task_sorting_proposal(tasks,topology,base,proposal if routes is None else routes,
        descriptor if move is None else move,relay_enabled=relay,telemetry=telemetry,**kwargs)


def snapshot(data,*,relay=True,routes=None,move=None):
    proposal=data[3] if routes is None else routes;descriptor=data[4] if move is None else move
    proof=audit(data,routes=proposal,move=descriptor,relay=relay)
    score=proof["score"];physical=[score[k] for k in ("late_count","total_lateness_min","distance_km")]
    return dict(routes=[list(r) for r in proposal],score=deepcopy(score),table_score=deepcopy(score),
        move=deepcopy(descriptor),valid=True,complete=True,cross_uav_count=6 if relay else 0,
        relay_task_count=6 if relay else 0,event_times_min=deepcopy(proof["event_times_min"]),
        delivery_times_min=deepcopy(proof["delivery_times_min"]),
        source_drop_availability_min=deepcopy(proof["source_drop_availability_min"]),
        key=[sum(w*x/s for w,x,s in zip((.5,.25,.25),physical,(6.,100.,100.))),*physical])


def direct_move(data,groups=None):
    descriptor=deepcopy(data[4]);groups=descriptor["source_task_groups"] if groups is None else groups
    descriptor.update(mode="DIRECT",station_id=None,final_task_groups=groups,
        final_aircraft_by_task={str(t):a for a,g in enumerate(groups) for t in g})
    return descriptor


def test_triangle_has_six_real_crosses_exact_flight_and_strict_gain_mechanism():
    data=fixture();result=audit(data);r=10./sqrt(3.)
    assert result["passed"] and result["final_tasks_per_aircraft"]==[2,2,2]
    assert result["actual_cross_uav_count"]==6
    assert result["score"]["late_count"]==0
    assert result["score"]["distance_km"]==pytest.approx(6*r)
    assert result["base_score"]["distance_km"]==pytest.approx(60.)
    assert result["base_score"]["late_count"]==3
    assert all(value==pytest.approx(r) for value in result["source_drop_availability_min"].values())
    assert all(h["aircraft_waiting_min"]==pytest.approx(0.) and h["package_cache_min"]==pytest.approx(0.)
               for h in result["handoffs"])
    assert all(distance==pytest.approx(r) for distance in result["shared_double_loaded_km_by_aircraft"].values())


def test_direct_accepts_original_source_pairs_instead_of_forcing_eight_cross_maps():
    data=fixture();move=direct_move(data);result=audit(data,routes=data[2],move=move,relay=False)
    assert result["passed"] and result["actual_cross_uav_count"]==0
    assert result["score"]["distance_km"]==pytest.approx(60.)
    assert result["source_drop_availability_min"]=={}
    saved=snapshot(data,routes=data[2],move=move,relay=False)
    audit(data,routes=data[2],move=move,relay=False,telemetry=saved,weights=(.5,.25,.25),scales=(6.,100.,100.))


def test_all_six_legal_direct_PD_orders_allowed_in_complete_blocks():
    data=fixture();move=direct_move(data)
    orders=((1,-1,2,-2),(1,2,-1,-2),(1,2,-2,-1),(2,-2,1,-1),(2,1,-1,-2),(2,1,-2,-1))
    for order in orders:
        result=audit(data,routes=(order,*data[2][1:]),move=move,relay=False)
        assert result["passed"]


def test_cyclic_responsibility_mapping_is_acyclic_when_all_IN_precedes_OUT():
    result=audit(fixture(native=True,slow=True))
    assert result["passed"] and len(result["inbound_records"])==len(result["outbound_records"])==3
    for incoming,outgoing in zip(result["inbound_records"],result["outbound_records"]):
        assert max(incoming["drop_positions"])<min(outgoing["take_positions"])
    # C arrives ten minutes later. A takes B's parcel, holds one and waits for C.
    block=result["outbound_records"][0]
    assert block["take_times_min"][1]-block["take_times_min"][0]==pytest.approx(10.)
    assert block["aircraft_waiting_min"]==pytest.approx(10.)
    assert result["source_drop_availability_min"]["5"]==pytest.approx(10.+10./sqrt(3.))


def test_native_suffix_lateness_and_all_actual_waits_are_in_full_table_score():
    data=fixture(native=True,slow=True);result=audit(data);row=result["native_delivery_clock_changes"][0]
    assert row["task_id"]==7 and row["base_delivery_min"]==pytest.approx(20.)
    assert row["delivery_min"]==pytest.approx(20.+20./sqrt(3.))
    assert result["score"]["late_count"]==1 and row["lateness_min"]==pytest.approx(20./sqrt(3.)-5.)
    saved=snapshot(data);saved["table_score"]["late_count"]=0
    with pytest.raises(ValueError,match="table score"):audit(data,telemetry=saved)


def test_native_between_drop_and_take_keeps_availability_but_counts_return_to_H():
    data=fixture(native=True);route=data[3][0]
    proposal=(route[:4]+(7,-7)+route[4:8],*data[3][1:])
    result=audit(data,routes=proposal);r=10./sqrt(3.)
    assert result["source_drop_availability_min"]["1"]==pytest.approx(r)
    assert result["outbound_records"][0]["arrival_min"]==pytest.approx(3*r)
    assert result["outbound_records"][0]["aircraft_waiting_min"]==pytest.approx(0.)
    assert result["score"]["distance_km"]==pytest.approx(8*r)
    assert result["delivery_times_min"]["7"]==pytest.approx(2*r)


def test_even_legal_below_K_native_owner_drift_cannot_change_original_final_counts():
    data=fixture(native=True);proposal=(data[3][0][:-2],data[3][1]+(7,-7),data[3][2])
    physical=reference_schedule(data[0],proposal,data[1])
    assert [r["task_count"] for r in physical["route_records"]]==[2,3,2]
    with pytest.raises(ValueError,match="final quotas"):audit(data,routes=proposal)


def test_acyclic_but_OUT_before_own_IN_is_rejected_as_nonseparable():
    data=fixture();route=data[3][0];proposal=(route[4:]+route[:4],*data[3][1:])
    # A's OUT depends only on B/C independent IN prefixes. The DAG itself is
    # legal, but A's later IN availability now contains another-source wait.
    assert reference_schedule(data[0],proposal,data[1])["score"]
    with pytest.raises(ValueError,match="own IN drop"):audit(data,routes=proposal)


def test_actual_handoff_dependency_cycle_is_rejected_globally():
    data=fixture();proposal=tuple(r[4:]+r[:4] for r in data[3])
    with pytest.raises(ValueError,match="dependency cycle"):audit(data,routes=proposal)


def test_both_received_parcels_from_one_source_is_not_cross_source_sorting():
    data=fixture();groups=[[3,4],[5,6],[1,2]];move=deepcopy(data[4]);move.update(
        final_task_groups=groups,final_aircraft_by_task={str(t):a for a,g in enumerate(groups) for t in g})
    proposal=[]
    for a,g in enumerate(groups):
        out=[100+2*(t-1)+2 for t in g];proposal.append(data[3][a][:4]+tuple(out)+tuple(-v for v in out))
    with pytest.raises(ValueError,match="distinct other sources"):audit(data,routes=tuple(proposal),move=move)


def test_same_aircraft_relay_cannot_count_as_true_cross():
    data=fixture();groups=[[1,3],[2,5],[4,6]];move=deepcopy(data[4]);move.update(
        final_task_groups=groups,final_aircraft_by_task={str(t):a for a,g in enumerate(groups) for t in g})
    proposal=[]
    for a,g in enumerate(groups):
        out=[100+2*(t-1)+2 for t in g];proposal.append(data[3][a][:4]+tuple(out)+tuple(-v for v in out))
    with pytest.raises(ValueError,match="true cross"):audit(data,routes=tuple(proposal),move=move)


def test_single_relay_uses_wrong_station_despite_complete_legal_plan():
    data=fixture()
    for row in data[1]["legs"]:
        if row["task_id"]==1:row["relay_id"]=10
    with pytest.raises(ValueError,match="same fixed H"):audit(data)


def test_zero_joint_loaded_distance_cannot_count_as_batch_delivery():
    data=fixture();task=data[0][3];data[0][3]=(task[0],tuple(data[1]["stations"][0][1:]),task[2])
    with pytest.raises(ValueError,match="positive distance"):audit(data)


def test_unrelated_native_must_stay_on_same_owner_and_in_same_order():
    data=fixture(native=True);tasks,top,base,proposal,move=data
    # A/B each gets one native, permitting a quota-neutral native swap that
    # is physically valid but outside this six-task macro's source proof.
    tasks[8]=tasks[7];base=(base[0],base[1]+(8,-8),base[2]);proposal=(proposal[0],proposal[1]+(8,-8),proposal[2])
    wrong=(tuple(8 if v==7 else -8 if v==-7 else v for v in proposal[0]),
           tuple(7 if v==8 else -7 if v==-8 else v for v in proposal[1]),proposal[2])
    with pytest.raises(ValueError,match="unrelated native"):audit((tasks,top,base,proposal,move),routes=wrong)
    top["max_tasks_per_drone"]=4;base=(base[0]+(8,-8),base[1][:-2],base[2]);proposal=(proposal[0]+(8,-8),proposal[1][:-2],proposal[2])
    wrong=(proposal[0][:-4]+(8,-8,7,-7),*proposal[1:])
    with pytest.raises(ValueError,match="unrelated native"):audit((tasks,top,base,proposal,move),routes=wrong)


@pytest.mark.parametrize("field",["score","table_score","event_times_min","delivery_times_min","source_drop_availability_min","key","cross_uav_count"])
def test_complete_snapshot_numbers_are_source_bound(field):
    data=fixture();saved=snapshot(data)
    if field in ("score","table_score"):saved[field]["distance_km"]+=1.
    elif field=="event_times_min":saved[field][0][2]+=1.
    elif field in ("delivery_times_min","source_drop_availability_min"):saved[field]["1"]+=1.
    elif field=="key":saved[field][0]+=.1
    else:saved[field]=0
    with pytest.raises(ValueError):audit(data,telemetry=saved,weights=(.5,.25,.25),scales=(6.,100.,100.))


def test_missing_full_telemetry_or_forged_move_availability_rejected():
    data=fixture();saved=snapshot(data);saved.pop("table_score")
    with pytest.raises(ValueError,match="table score"):audit(data,telemetry=saved)
    move=deepcopy(data[4]);move["source_drop_availability_min"]={str(t):0. for t in range(1,7)}
    with pytest.raises(ValueError,match="drop availability"):audit(data,move=move)
    move=deepcopy(data[4]);move["shared_double_loaded_km_by_aircraft"]={str(a):0. for a in range(3)}
    with pytest.raises(ValueError,match="shared distances"):audit(data,move=move)


@pytest.mark.parametrize("kind",["source","final","half","capacity","family","initial_relay"])
def test_full_coverage_original_sources_capacity_and_descriptors_cannot_change(kind):
    data=fixture();tasks,top,base,proposal,move=data;move=deepcopy(move)
    if kind=="source":base=(base[1],base[0],base[2])
    elif kind=="final":move["final_aircraft_by_task"]["1"]=0
    elif kind=="half":proposal=(proposal[0][:-1],*proposal[1:])
    elif kind=="capacity":proposal=((101,103,106,-101,-103,110,-106,-110),*proposal[1:])
    elif kind=="family":move["family"]["aircraft"]=[False,1,2]
    else:base=proposal
    with pytest.raises(ValueError):audit((tasks,top,base,proposal,move))
