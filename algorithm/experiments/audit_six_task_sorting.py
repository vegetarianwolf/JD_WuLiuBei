"""Independent standard-library audit of a fixed-station six-task sort.

No search or cost-table implementation is imported. Whole base/proposal DAGs
are reconstructed, including all native clocks, flights, waiting and cache.
The IN-before-OUT structure is checked separately: an acyclic complete route
alone does not justify using R-independent inbound availability cost tables.
"""
from __future__ import annotations

from math import hypot,isclose,isfinite

try:
    from .audit_repooling import _identity,_owners,_shared_flight
    from .audit_mobile_relay import _point
    from .audit_weighted_relay_ablation import _geometry,reference_schedule
    from .audit_weighted_direct_pair import require,same
except ImportError:
    from audit_repooling import _identity,_owners,_shared_flight
    from audit_mobile_relay import _point
    from audit_weighted_relay_ablation import _geometry,reference_schedule
    from audit_weighted_direct_pair import require,same


def _integers(values,length,label):
    require(isinstance(values,(list,tuple)) and len(values)==length
            and all(type(v) is int for v in values),"Invalid six-task "+label)
    return tuple(values)


def _numbers(actual,expected,label):
    if isinstance(expected,dict):
        require(isinstance(actual,dict) and set(actual)==set(expected),label+" fields differ")
        for key,value in expected.items():_numbers(actual[key],value,label+"."+str(key))
    elif isinstance(expected,(tuple,list)):
        require(isinstance(actual,(tuple,list)) and len(actual)==len(expected),label+" shape differs")
        for i,value in enumerate(expected):_numbers(actual[i],value,label+"["+str(i)+"]")
    else:
        same(actual,expected,label)


def _bind_snapshot(saved,routes,move,result,weights,scales):
    require(isinstance(saved,dict),"Missing six-task full snapshot")
    require(saved.get("valid") is True and saved.get("complete") is True,"Six-task snapshot is not complete/valid")
    require(saved.get("routes")==[list(r) for r in routes] or saved.get("routes")==tuple(tuple(r) for r in routes),
            "Six-task snapshot routes differ")
    require(saved.get("move")==move,"Six-task snapshot move differs")
    require(isinstance(saved.get("score"),dict) and type(saved["score"].get("late_count")) is int,
            "Invalid six-task snapshot score")
    _numbers(saved["score"],result["score"],"six-task snapshot score")
    for name in ("event_times_min","delivery_times_min","source_drop_availability_min"):
        require(name in saved,"Missing six-task snapshot "+name)
        _numbers(saved[name],result[name],"six-task snapshot "+name)
    for name,expected in (("cross_uav_count",result["actual_cross_uav_count"]),
                          ("relay_task_count",len(result["handoffs"]))):
        require(type(saved.get(name)) is int and saved[name]==expected,"Six-task snapshot "+name+" differs")
    require(isinstance(saved.get("table_score"),dict) and type(saved["table_score"].get("late_count")) is int,
            "Missing/invalid six-task full table score")
    _numbers(saved["table_score"],result["score"],"six-task full table score")
    if weights is not None or scales is not None:
        require(isinstance(weights,(list,tuple)) and isinstance(scales,(list,tuple))
                and len(weights)==len(scales)==3 and all(type(x) in (int,float) and isfinite(x) and x>0
                for x in (*weights,*scales)) and isclose(sum(weights),1.),"Invalid six-task weighted coefficients")
        physical=[result["score"][k] for k in ("late_count","total_lateness_min","distance_km")]
        expected=[sum(w*x/s for w,x,s in zip(weights,physical,scales)),*physical]
        _numbers(saved.get("key"),expected,"six-task weighted key")


def audit_six_task_sorting_proposal(tasks,topology,base_routes,proposal_routes,move,*,relay_enabled,
                                  telemetry=None,weights=None,scales=None):
    """Validate real source provenance and a complete six-task physical macro.

    DIRECT may use any partition of six tasks into two per selected aircraft;
    it is not forced to one of the eight distinct-source RELAY assignments.
    Tiny tests may use fewer tasks/UAVs/K, with exactly the same constraints.
    """
    require(type(relay_enabled) is bool and isinstance(move,dict),"Invalid six-task arm/move")
    homes,stations,registry,count,capacity,maximum,speed=_geometry(tasks,topology)
    require(capacity==2,"Six-task sorting requires capacity two")
    family=move.get("family")
    require(isinstance(family,dict),"Missing six-task source family")
    aircraft=_integers(family.get("aircraft"),3,"source aircraft")
    require(tuple(sorted(set(aircraft)))==aircraft and all(0<=a<count for a in aircraft),
            "Six-task aircraft must be three sorted distinct original aircraft")
    source_groups=family.get("source_task_groups")
    require(isinstance(source_groups,(list,tuple)) and len(source_groups)==3,"Invalid six-task source groups")
    source_groups=tuple(_integers(g,2,"source task pair") for g in source_groups)
    require(all(tuple(sorted(set(g)))==g for g in source_groups),"Source task pairs must be sorted/distinct")
    selected=set(t for g in source_groups for t in g)
    require(len(selected)==6 and selected<=set(tasks),"Six-task source family must contain six distinct known tasks")
    require(_integers(family.get("task_ids"),6,"family task IDs")==tuple(sorted(selected)),"Family task coverage differs")
    family_station=family.get("station_id")
    require(type(family_station) is int and family_station in stations,"Family station must be existing/fixed")
    require(move.get("mode")==("RELAY" if relay_enabled else "DIRECT"),"Six-task saved arm differs")
    require("station_id" in move and move["station_id"]==(family_station if relay_enabled else None),
            "Six-task station differs from original family/arm")
    for name,expected in (("aircraft",list(aircraft)),("source_task_groups",[list(g) for g in source_groups])):
        if name in move:require(move[name]==expected,"Six-task duplicated source descriptor differs: "+name)
    final_groups=move.get("final_task_groups")
    require(isinstance(final_groups,(list,tuple)) and len(final_groups)==3,"Invalid six-task final groups")
    final_groups=tuple(_integers(g,2,"final task pair") for g in final_groups)
    require(len({t for g in final_groups for t in g})==6 and {t for g in final_groups for t in g}==selected,
            "Six-task final partition does not cover six tasks once")
    expected_final={str(t):a for a,g in zip(aircraft,final_groups) for t in g}
    require(isinstance(move.get("final_aircraft_by_task"),dict)
            and all(type(v) is int for v in move["final_aircraft_by_task"].values())
            and move["final_aircraft_by_task"]==expected_final,"Saved six-task final ownership differs")
    base=reference_schedule(tasks,base_routes,topology)
    require(not base["handoffs"],"Six-task source must be a pure DIRECT complete solution")
    proposal=reference_schedule(tasks,proposal_routes,topology)
    original_owners,final_owners=_owners(base_routes,registry),_owners(proposal_routes,registry)
    source_owner={t:a for a,g in zip(aircraft,source_groups) for t in g}
    require(all(original_owners[t]==source_owner[t] for t in selected),"Six-task original source ownership differs")
    require(all(final_owners[t]==expected_final[str(t)] for t in selected),"Six-task actual final assignment differs")
    counts=[r["task_count"] for r in proposal["route_records"]]
    require(counts==[r["task_count"] for r in base["route_records"]],"Six-task final quotas changed")
    without=lambda routes:tuple(tuple(v for v in route if _identity(v,registry) not in selected) for route in routes)
    require(without(base_routes)==without(proposal_routes),"Six-task sorting moved/reordered an unrelated native event")
    availability={};inbound_records=[];outbound_records=[];shared={};shared_segments={}
    if relay_enabled:
        handoffs={h["task_id"]:h for h in proposal["handoffs"]}
        require(len(handoffs)==6 and set(handoffs)==selected,"Six-task relay must contain exactly six single-hop plans")
        for task,handoff in handoffs.items():
            require(handoff["cross_uav"] and handoff["inbound_drone_id"]==source_owner[task]+1
                    and handoff["outbound_drone_id"]==expected_final[str(task)]+1
                    and handoff["relay_id"]==family_station,"Six-task handoff lost original source/true cross/same fixed H")
        for a,own,received in zip(aircraft,source_groups,final_groups):
            require(len({source_owner[t] for t in received})==2 and all(source_owner[t]!=a for t in received),
                    "Each OUT pair must come from the two distinct other sources")
            route=proposal_routes[a]
            in_ids={handoffs[t]["in_leg"] for t in own};out_ids={handoffs[t]["out_leg"] for t in received}
            in_positions=[p for p,v in enumerate(route) if abs(v) in in_ids]
            out_positions=[p for p,v in enumerate(route) if abs(v) in out_ids]
            require(len(in_positions)==4 and in_positions==list(range(in_positions[0],in_positions[0]+4)),
                    "Six-task IN batch must be a contiguous four-event block")
            require([route[p]>0 for p in in_positions]==[True,True,False,False],"Six-task IN batch must collect two then drop two")
            require(len(out_positions)==4 and out_positions==list(range(out_positions[0],out_positions[0]+4)),
                    "Six-task OUT batch must be a contiguous four-event block")
            require([route[p]>0 for p in out_positions]==[True,True,False,False],"Six-task OUT batch must take two then deliver two")
            require(in_positions[-1]<out_positions[0],"Every own IN drop must precede any own OUT take")
            before=lambda p:proposal["route_records"][a]["load_after_event"][p-1] if p else 0
            require(before(in_positions[0])==before(out_positions[0])==0,"Six-task IN/OUT blocks must start at empty gaps")
            # The checked prefix contains no OUT, hence has no handoff wait.
            point=homes[a];clock=0.;prefix_clocks={}
            for p,v in enumerate(route[:in_positions[-1]+1]):
                require(not (registry.get(abs(v),{}).get("kind")=="RELAY_OUT"),"Inbound availability prefix contains OUT dependency")
                target=_point(tasks,stations,registry,v);clock+=hypot(target[0]-point[0],target[1]-point[1])/speed
                prefix_clocks[p]=clock;point=target
                same(clock,proposal["event_times_min"][a][p],"independent IN prefix clock")
            drop_positions=[p for p in in_positions if route[p]<0]
            for task in own:
                h=handoffs[task];p=route.index(-h["in_leg"])
                same(prefix_clocks[p],h["drop_time_min"],"source-derived drop availability")
                availability[str(task)]=prefix_clocks[p]
            inbound_records.append(dict(aircraft=a,task_ids=list(own),event_positions=in_positions,
                drop_positions=drop_positions,drop_times_min=[proposal["event_times_min"][a][p] for p in drop_positions]))
            distance,segments,loads=_shared_flight(tasks,homes,stations,registry,route,a,out_ids,proposal["event_times_min"][a])
            require(distance>1e-10,"Six-task OUT pair must fly positive distance jointly loaded")
            take_positions=out_positions[:2]
            require([loads[p] for p in take_positions]==[0,1],"Six-task OUT takes must have empty/one load frontiers")
            first=take_positions[0];previous=_point(tasks,stations,registry,route[first-1]) if first else homes[a]
            arrival=(proposal["event_times_min"][a][first-1] if first else 0.)+hypot(
                stations[family_station][0]-previous[0],stations[family_station][1]-previous[1])/speed
            take_tasks=[registry[route[p]]["task_id"] for p in take_positions]
            ready=[handoffs[t]["drop_time_min"] for t in take_tasks]
            first_clock=max(arrival,ready[0]);second_clock=max(first_clock,ready[1])
            _numbers([proposal["event_times_min"][a][p] for p in take_positions],[first_clock,second_clock],"actual OUT block clocks")
            shared[str(a)]=distance;shared_segments[str(a)]=segments
            outbound_records.append(dict(aircraft=a,task_ids=take_tasks,event_positions=out_positions,
                take_positions=take_positions,arrival_min=arrival,drop_availability_min=ready,
                take_times_min=[first_clock,second_clock],block_end_min=second_clock,
                aircraft_waiting_min=second_clock-arrival,shared_double_loaded_km=distance))
    else:
        require(not proposal["handoffs"],"DIRECT six-task control cannot contain relay plans")
        for a,received in zip(aircraft,final_groups):
            route=proposal_routes[a];positions=[p for p,v in enumerate(route) if abs(v) in received]
            require(len(positions)==4 and positions==list(range(positions[0],positions[0]+4)),
                    "DIRECT six-task pair must occupy one complete four-event block")
            before=proposal["route_records"][a]["load_after_event"][positions[0]-1] if positions[0] else 0
            require(before==0,"DIRECT six-task pair block must start empty")
            distance,segments,_=_shared_flight(tasks,homes,stations,registry,route,a,received,proposal["event_times_min"][a])
            shared[str(a)]=distance;shared_segments[str(a)]=segments
    if "source_drop_availability_min" in move:_numbers(move["source_drop_availability_min"],availability,"saved source drop availability")
    if "shared_double_loaded_km_by_aircraft" in move:_numbers(move["shared_double_loaded_km_by_aircraft"],shared,"saved pair shared distances")
    native_changes=[]
    for task in sorted(set(tasks)-selected):
        old=base["delivery_times_min"][str(task)];new=proposal["delivery_times_min"][str(task)]
        if abs(old-new)>1e-9:native_changes.append(dict(task_id=task,base_delivery_min=old,delivery_min=new,
            clock_delta_min=new-old,base_lateness_min=max(0.,old-tasks[task][2]),lateness_min=max(0.,new-tasks[task][2])))
    result=dict(passed=True,mode=move["mode"],station_id=move["station_id"],family=family,
        selected_task_ids=sorted(selected),source_owners={str(t):a for t,a in sorted(source_owner.items())},
        actual_final_owners={str(t):a for t,a in sorted(final_owners.items())},final_tasks_per_aircraft=counts,
        base_score=base["score"],score=proposal["score"],source_drop_availability_min=availability,
        inbound_records=inbound_records,outbound_records=outbound_records,
        shared_double_loaded_km_by_aircraft=shared,shared_loaded_segments_by_aircraft=shared_segments,
        actual_cross_uav_count=proposal["relay_statistics"]["cross_uav_handoff_count"],
        native_delivery_clock_changes=native_changes,delivery_times_min=proposal["delivery_times_min"],
        event_times_min=proposal["event_times_min"],handoffs=proposal["handoffs"],core_reference=proposal)
    if telemetry is not None:_bind_snapshot(telemetry,proposal_routes,move,result,weights,scales)
    return result
