"""Nine-task source witnesses; no solver/helper implementation is imported."""
from copy import deepcopy

import pytest

from algorithm.experiments.audit_six_task_pinned_polish import audit_six_task_pinned_polish_proposal
from algorithm.experiments.audit_six_task_sorting import audit_six_task_sorting_proposal
from algorithm.experiments.audit_weighted_relay_ablation import reference_schedule
from algorithm.tests.test_audit_six_task_sorting import fixture as triangle,direct_move


def fixture(*,direct=False):
    tasks,topology,base,raw,move=triangle()
    topology["max_tasks_per_drone"]=3
    for task,point in ((7,(1.,0.)),(8,(9.,0.)),(9,(5.,7.))):
        tasks[task]=(point,point,1.5 if task==7 else 100.)
        topology["legs"] += [dict(id=100+2*(task-1)+offset,task_id=task,kind=kind,relay_id=9)
            for offset,kind in ((1,"RELAY_IN"),(2,"RELAY_OUT"))]
    base=tuple(route+(7+a,-(7+a)) for a,route in enumerate(base))
    raw=tuple(route+(7+a,-(7+a)) for a,route in enumerate(raw))
    if direct:
        move=direct_move((tasks,topology,base,raw,move));raw=base
    return tasks,topology,base,raw,move


def audit(data,post=None,*,telemetry=None,**kwargs):
    tasks,topology,base,raw,move=data
    return audit_six_task_pinned_polish_proposal(tasks,topology,base,raw,raw if post is None else post,
        move,relay_enabled=move["mode"]=="RELAY",telemetry=telemetry,**kwargs)


def snapshot(data,post):
    proof=audit(data,post);physical=[proof["score"][k] for k in ("late_count","total_lateness_min","distance_km")]
    return dict(routes=[list(r) for r in post],score=deepcopy(proof["score"]),valid=True,complete=True,
        original_macro_move=deepcopy(data[4]),actual_final_owners=proof["actual_final_owners"],
        pinned_task_ids=list(range(1,7)),pin_scope="six_task_sorting",post_global_score=deepcopy(proof["score"]),
        event_times_min=deepcopy(proof["event_times_min"]),delivery_times_min=deepcopy(proof["delivery_times_min"]),
        current_source_drop_availability_min=deepcopy(proof["current_source_drop_availability_min"]),
        current_shared_double_loaded_km_by_aircraft=deepcopy(proof["current_shared_double_loaded_km_by_aircraft"]),
        cross_uav_count=6 if data[4]["mode"]=="RELAY" else 0,
        relay_task_count=6 if data[4]["mode"]=="RELAY" else 0,
        key=[sum(w*x/s for w,x,s in zip((.5,.25,.25),physical,(9.,100.,100.))),*physical])


@pytest.mark.parametrize("direct",[False,True])
def test_three_free_native_tasks_really_change_owner_with_same_complete_six_plans_and_quotas(direct):
    data=fixture(direct=direct)
    remap={7:8,8:9,9:7}
    post=tuple(tuple((1 if v>0 else -1)*remap[abs(v)] if abs(v) in remap else v for v in route) for route in data[3])
    proof=audit(data,post)
    assert proof["passed"] and proof["unpinned_owner_changed_task_ids"]==[7,8,9]
    assert proof["final_tasks_per_aircraft"]==[3,3,3]
    assert proof["actual_cross_uav_count"]==(0 if direct else 6)
    saved=snapshot(data,post)
    audit(data,post,telemetry=saved,weights=(.5,.25,.25),scales=(9.,100.,100.))
    with pytest.raises(ValueError,match="unrelated native"):
        audit_six_task_sorting_proposal(data[0],data[1],data[2],post,data[4],relay_enabled=not direct)


@pytest.mark.parametrize("where",["between_IN_pickups","before_first_IN_with_load","between_IN_drops","between_OUT_deliveries"])
def test_legal_native_interleaving_is_post_valid_but_not_a_new_raw_macro(where):
    data=fixture();route=data[3][0];core=route[:-2]
    if where=="between_IN_pickups":changed=core[:1]+(7,-7)+core[1:]
    elif where=="before_first_IN_with_load":changed=(7,core[0],-7)+core[1:]
    elif where=="between_IN_drops":changed=core[:3]+(7,-7)+core[3:]
    else:changed=core[:7]+(7,-7)+core[7:]
    post=(changed,*data[3][1:]);proof=audit(data,post)
    assert proof["passed"] and proof["final_tasks_per_aircraft"]==[3,3,3]
    assert proof["unpinned_owner_changed_count"]==0
    assert proof["current_shared_double_loaded_km_by_aircraft"]["0"]>0.
    with pytest.raises(ValueError):
        audit_six_task_sorting_proposal(data[0],data[1],data[2],post,data[4],relay_enabled=True)
    if where=="between_IN_pickups":
        assert proof["delivery_times_min"]["7"]==pytest.approx(1.)
        raw=audit(data)
        assert proof["current_source_drop_availability_min"]["1"]==pytest.approx(raw["current_source_drop_availability_min"]["1"]+2.)
        assert sum(h["aircraft_waiting_min"] for h in proof["handoffs"])>0.
    if where=="between_IN_drops":
        assert proof["current_source_drop_availability_min"]["2"]>proof["current_source_drop_availability_min"]["1"]+1.
    saved=snapshot(data,post)
    audit(data,post,telemetry=saved,weights=(.5,.25,.25),scales=(9.,100.,100.))


def test_direct_native_may_split_two_selected_deliveries_without_raw_four_block_rule():
    data=fixture(direct=True);route=data[3][0]
    post=(route[:3]+(7,-7)+route[3:4],*data[3][1:])
    proof=audit(data,post)
    assert proof["passed"] and proof["actual_cross_uav_count"]==0
    assert proof["current_source_drop_availability_min"]=={}


@pytest.mark.parametrize("kind",["drop_relay","source","order","take_gap","quota","capacity","cycle","extra_relay","missing"])
def test_identity_shape_capacity_quota_and_global_DAG_breaks_rejected(kind):
    data=fixture();tasks,topology,base,raw,move=data;post=list(raw)
    if kind=="drop_relay":
        post[0]=tuple(v for v in post[0] if abs(v)!=101)
        post[1]=tuple(1 if v==102 else -1 if v==-102 else v for v in post[1])
    elif kind=="source":
        post[0]=tuple(v for v in post[0] if abs(v)!=101);post[1]=(101,-101)+post[1]
    elif kind=="order":post[0]=post[0][:6]+(-110,-106)+post[0][8:]
    elif kind=="take_gap":post[0]=post[0][:5]+(7,-7)+post[0][5:8]
    elif kind=="quota":
        topology["max_tasks_per_drone"]=4;post[0]=post[0][:-2];post[1]=post[1]+(7,-7)
        assert reference_schedule(tasks,tuple(post),topology)
    elif kind=="capacity":post[0]=post[0][:4]+(7,)+post[0][4:8]+(-7,)
    elif kind=="cycle":post=[r[4:8]+r[:4]+r[8:] for r in post]
    elif kind=="extra_relay":post[0]=post[0][:-2]+(113,-113,114,-114)
    else:post[0]=post[0][:-1]
    with pytest.raises(ValueError):audit((tasks,topology,base,raw,move),tuple(post))


def test_changed_leg_and_fixed_station_rejected_after_valid_original_raw_source_proof():
    data=fixture();tasks,topology,base,raw,move=data
    topology["legs"] += [dict(id=501,task_id=1,kind="RELAY_IN",relay_id=10),
                         dict(id=502,task_id=1,kind="RELAY_OUT",relay_id=10)]
    post=tuple(tuple(501 if v==101 else -501 if v==-101 else 502 if v==102 else -502 if v==-102 else v for v in r) for r in raw)
    with pytest.raises(ValueError,match="modes/legs/stations"):audit(data,post)


@pytest.mark.parametrize("field",["post_global_score","event_times_min","delivery_times_min","current_source_drop_availability_min","current_shared_double_loaded_km_by_aircraft","actual_final_owners","key","table_score"])
def test_current_snapshot_not_certified_by_false_clocks_or_old_table(field):
    data=fixture();core=data[3][0][:-2];post=(core[:1]+(7,-7)+core[1:],*data[3][1:]);saved=snapshot(data,post)
    if field=="post_global_score":saved[field]["distance_km"]+=1.
    elif field=="event_times_min":saved[field][0][2]+=1.
    elif field=="delivery_times_min":saved[field]["7"]+=1.
    elif field=="current_source_drop_availability_min":saved[field]=audit(data)[field]
    elif field=="current_shared_double_loaded_km_by_aircraft":saved[field]["0"]+=1.
    elif field=="actual_final_owners":saved[field]["7"]=1
    elif field=="key":saved[field][0]+=.1
    else:saved[field]=deepcopy(saved["score"])
    with pytest.raises(ValueError):audit(data,post,telemetry=saved,weights=(.5,.25,.25),scales=(9.,100.,100.))


def test_post_move_provenance_raw_physics_cannot_be_ambiguously_reused():
    data=fixture();post=data[3];saved=snapshot(data,post)
    saved["move"]=deepcopy(data[4]);saved["move"]["semantic"]="post_six_task_pinned_polish"
    saved["move"]["source_drop_availability_min"]=audit(data)["current_source_drop_availability_min"]
    with pytest.raises(ValueError,match="raw physical"):audit(data,post,telemetry=saved)
    saved["move"]={k:v for k,v in data[4].items() if k not in ("source_drop_availability_min","shared_double_loaded_km_by_aircraft")}
    saved["move"]["semantic"]="post_six_task_pinned_polish"
    audit(data,post,telemetry=saved)
    saved["move"]={"semantic":"post_six_task_pinned_polish"};audit(data,post,telemetry=saved)
    saved["table_score"]="not_applicable";audit(data,post,telemetry=saved)
    saved["source_drop_availability_min"]=audit(data)["current_source_drop_availability_min"]
    with pytest.raises(ValueError,match="separate current"):audit(data,post,telemetry=saved)


def test_synthetic_relay_base_cannot_replace_original_direct_macro_provenance():
    data=fixture();tasks,topology,base,raw,move=data
    with pytest.raises(ValueError,match="pure DIRECT"):
        audit((tasks,topology,raw,raw,move))
