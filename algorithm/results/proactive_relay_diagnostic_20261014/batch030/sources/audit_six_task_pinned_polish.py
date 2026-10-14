"""Independent full replay after six-plan-preserving ordinary task repair.

The original DIRECT-to-raw macro is audited first with its frozen auditor.
The current solution may move/reorder every unpinned DIRECT task and weave
native service between selected IN events or OUT deliveries. Raw finite-table
scores and source availability never certify the post-polish solution.
"""
from __future__ import annotations

from math import hypot,isclose,isfinite

try:
    from .audit_six_task_sorting import audit_six_task_sorting_proposal,_numbers
    from .audit_pinned_repooling import _plans
    from .audit_repooling import _identity,_owners,_direct_task,_shared_flight
    from .audit_mobile_relay import _point
    from .audit_weighted_relay_ablation import _geometry,reference_schedule
    from .audit_weighted_direct_pair import require,same
except ImportError:
    from audit_six_task_sorting import audit_six_task_sorting_proposal,_numbers
    from audit_pinned_repooling import _plans
    from audit_repooling import _identity,_owners,_direct_task,_shared_flight
    from audit_mobile_relay import _point
    from audit_weighted_relay_ablation import _geometry,reference_schedule
    from audit_weighted_direct_pair import require,same


def _bind(tasks,topology,saved,routes,move,result,weights,scales):
    require(isinstance(saved,dict) and saved.get("valid") is True and saved.get("complete") is True,
            "Six-task post snapshot is not complete/valid")
    require(saved.get("routes")==[list(r) for r in routes]
            or saved.get("routes")==tuple(tuple(r) for r in routes),"Six-task post routes differ")
    require(saved.get("original_macro_move")==move,"Six-task original macro provenance differs")
    require(saved.get("pin_scope")=="six_task_sorting","Six-task post pin scope differs")
    pins=saved.get("pinned_task_ids")
    require(isinstance(pins,list) and all(type(t) is int for t in pins) and pins==result["pinned_task_ids"],
            "Six-task post pinned IDs differ")
    owners=saved.get("actual_final_owners")
    require(isinstance(owners,dict) and all(type(a) is int for a in owners.values())
            and owners==result["actual_final_owners"],"Six-task post actual owners differ")
    require("table_score" not in saved or saved["table_score"] is None or saved["table_score"]=="not_applicable",
            "Raw table score cannot certify a post-polish solution")
    require("source_drop_availability_min" not in saved and "shared_double_loaded_km_by_aircraft" not in saved,
            "Post snapshot must separate current physics from raw fields")
    for name in ("post_global_score","event_times_min","delivery_times_min",
                 "current_source_drop_availability_min","current_shared_double_loaded_km_by_aircraft"):
        require(name in saved,"Missing six-task post current field: "+name)
        _numbers(saved[name],result[name],"six-task post "+name)
    require(type(saved["post_global_score"].get("late_count")) is int,"Six-task post N must be integer")
    if "score" in saved:
        require(isinstance(saved["score"],dict) and type(saved["score"].get("late_count")) is int,
                "Invalid six-task post score")
        _numbers(saved["score"],result["score"],"six-task post score")
    for name,expected in (("cross_uav_count",result["actual_cross_uav_count"]),
                          ("relay_task_count",len(result["handoffs"]))):
        if name in saved:require(type(saved[name]) is int and saved[name]==expected,"Six-task post "+name+" differs")
    if "topology" in saved:
        require(_geometry(tasks,saved["topology"])==_geometry(tasks,topology),"Six-task post topology/resources differ")
    descriptor=saved.get("move")
    if descriptor is not None:
        require(isinstance(descriptor,dict) and descriptor.get("semantic")=="post_six_task_pinned_polish",
                "Six-task post move must declare current post semantics")
        for name in ("family","mode","station_id","aircraft","source_task_groups","final_task_groups","final_aircraft_by_task"):
            if name in descriptor:require(name in move and descriptor[name]==move[name],"Six-task post selected identity differs: "+name)
        require(not {"source_drop_availability_min","shared_double_loaded_km_by_aircraft","table_score"}&set(descriptor),
                "Six-task post move contains ambiguous raw physical values")
    if weights is not None or scales is not None:
        require(isinstance(weights,(list,tuple)) and isinstance(scales,(list,tuple)) and len(weights)==len(scales)==3
                and all(type(x) in (int,float) and isfinite(x) and x>0 for x in (*weights,*scales))
                and isclose(sum(weights),1.),"Invalid six-task post weighted objective")
        physical=[result["score"][k] for k in ("late_count","total_lateness_min","distance_km")]
        _numbers(saved.get("key"),[sum(w*x/s for w,x,s in zip(weights,physical,scales)),*physical],"six-task post weighted key")


def audit_six_task_pinned_polish_proposal(tasks,topology,base_direct_routes,macro_routes,
                                        polished_routes,original_macro_move,*,relay_enabled,
                                        telemetry=None,weights=None,scales=None):
    """Audit real raw provenance, then current six-plan/full-resource physics."""
    raw=audit_six_task_sorting_proposal(tasks,topology,base_direct_routes,macro_routes,
        original_macro_move,relay_enabled=relay_enabled)
    homes,stations,registry,count,capacity,maximum,speed=_geometry(tasks,topology)
    post=reference_schedule(tasks,polished_routes,topology)
    family=original_macro_move["family"];selected=set(family["task_ids"]);aircraft=family["aircraft"]
    require(_plans(macro_routes,registry,selected)==_plans(polished_routes,registry,selected),
            "Six-task pinned modes/legs/stations/role aircraft changed")
    subsequences=lambda routes:tuple(tuple(v for v in route if _identity(v,registry) in selected) for route in routes)
    require(subsequences(macro_routes)==subsequences(polished_routes),"Six-task selected signed subsequence changed")
    counts=[r["task_count"] for r in post["route_records"]]
    require(counts==raw["final_tasks_per_aircraft"],"Six-task post final quotas changed")
    require({h["task_id"] for h in post["handoffs"]}=={h["task_id"] for h in raw["handoffs"]},
            "Six-task post introduced/removed a relay plan")
    require(all(_direct_task(polished_routes,registry,t) for t in set(tasks)-selected),
            "Unpinned six-task post requests must remain whole DIRECT")
    availability={};shared={};segments={};prefixes=[];receivers=[]
    if relay_enabled:
        by_task={h["task_id"]:h for h in post["handoffs"]}
        require(len(by_task)==6 and set(by_task)==selected and all(h["cross_uav"] for h in by_task.values()),
                "Six-task post must retain six genuine single-hop crosses")
        source={t:a for a,g in zip(aircraft,family["source_task_groups"]) for t in g}
        for a,own,received in zip(aircraft,family["source_task_groups"],original_macro_move["final_task_groups"]):
            route=polished_routes[a]
            require(len({source[t] for t in received})==2 and a not in {source[t] for t in received},
                    "Post OUT pair must retain two other sources")
            drop_positions=[route.index(-by_task[t]["in_leg"]) for t in own]
            out_ids=[by_task[t]["out_leg"] for t in received]
            take_positions=sorted(route.index(identifier) for identifier in out_ids)
            require(max(drop_positions)<min(take_positions),"Every current own IN drop must precede own OUT")
            require(take_positions[1]==take_positions[0]+1,"Current OUT takes must remain consecutive")
            distance,flight,before=_shared_flight(tasks,homes,stations,registry,route,a,out_ids,post["event_times_min"][a])
            require([before[p] for p in take_positions]==[0,1],"Current OUT takes must enter empty then load one")
            require(distance>1e-10,"Current OUT pair lost positive joint loaded flight")
            point=homes[a];clock=0.;prefix=[]
            for p,v in enumerate(route[:max(drop_positions)+1]):
                require(registry.get(abs(v),{}).get("kind")!="RELAY_OUT","Current IN prefix contains an OUT dependency")
                target=_point(tasks,stations,registry,v);clock+=hypot(target[0]-point[0],target[1]-point[1])/speed
                same(clock,post["event_times_min"][a][p],"current independent IN prefix clock")
                prefix.append(clock);point=target
            for t,p in zip(own,drop_positions):
                same(prefix[p],by_task[t]["drop_time_min"],"current source drop availability")
                availability[str(t)]=prefix[p]
            prefixes.append(dict(aircraft=a,task_ids=list(own),drop_positions=drop_positions,
                drop_times_min=[prefix[p] for p in drop_positions]))
            shared[str(a)]=distance;segments[str(a)]=flight
            first=take_positions[0];previous=_point(tasks,stations,registry,route[first-1]) if first else homes[a]
            h=stations[original_macro_move["station_id"]]
            arrival=(post["event_times_min"][a][first-1] if first else 0.)+hypot(h[0]-previous[0],h[1]-previous[1])/speed
            take_tasks=[registry[route[p]]["task_id"] for p in take_positions];ready=[by_task[t]["drop_time_min"] for t in take_tasks]
            t1=max(arrival,ready[0]);t2=max(t1,ready[1])
            _numbers([post["event_times_min"][a][p] for p in take_positions],[t1,t2],"current OUT clocks")
            receivers.append(dict(aircraft=a,task_ids=take_tasks,take_positions=take_positions,arrival_min=arrival,
                take_times_min=[t1,t2],source_availability_min=ready,aircraft_waiting_min=t2-arrival))
    else:
        require(not post["handoffs"],"Six-task DIRECT post cannot introduce relays")
        for a,pair in zip(aircraft,original_macro_move["final_task_groups"]):
            distance,flight,_=_shared_flight(tasks,homes,stations,registry,polished_routes[a],a,pair,post["event_times_min"][a])
            shared[str(a)]=distance;segments[str(a)]=flight
    old_owners,new_owners=_owners(macro_routes,registry),_owners(polished_routes,registry)
    native=set(tasks)-selected
    changed=sorted(t for t in native if old_owners[t]!=new_owners[t])
    native_sequences=lambda routes:tuple(tuple(v for v in route if _identity(v,registry) in native) for route in routes)
    old_sequences,new_sequences=native_sequences(macro_routes),native_sequences(polished_routes)
    result=dict(passed=True,mode=original_macro_move["mode"],pin_scope="six_task_sorting",pinned_task_ids=sorted(selected),
        raw_audit=raw,macro_score=raw["score"],score=post["score"],post_global_score=post["score"],
        actual_final_owners={str(t):a for t,a in sorted(new_owners.items())},final_tasks_per_aircraft=counts,
        pinned_event_subsequences=[list(r) for r in subsequences(polished_routes)],
        unpinned_owner_changed_count=len(changed),unpinned_owner_changed_task_ids=changed,
        unpinned_event_sequence_changed_aircraft=[a for a in range(count) if old_sequences[a]!=new_sequences[a]],
        current_source_drop_availability_min=availability,current_shared_double_loaded_km_by_aircraft=shared,
        current_source_prefix_records=prefixes,current_outbound_records=receivers,
        current_shared_loaded_segments_by_aircraft=segments,actual_cross_uav_count=post["relay_statistics"]["cross_uav_handoff_count"],
        delivery_times_min=post["delivery_times_min"],event_times_min=post["event_times_min"],handoffs=post["handoffs"],core_reference=post)
    if telemetry is not None:_bind(tasks,topology,telemetry,polished_routes,original_macro_move,result,weights,scales)
    return result
