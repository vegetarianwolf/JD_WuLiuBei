"""Independent three-UAV, receiver-first mobile repooling physics.

One appended coordinate is an airborne rendezvous, retaining the original
homes and fixed network. Two distinct sources each transfer one parcel to an
empty third UAV already at that point. The receiver takes parcels in actual
source-arrival order, holds the first while waiting for the second, and then
flies carrying both. Each parcel has zero ground-cache time. Generic plans
with no appended site retain the old fixed-network semantics.

Optional original DIRECT routes plus a four-task move prove original/final
responsibility and native-event preservation. Without them, this module
proves physical service and actual transport ownership only; it invents no
original owner from topology or saved descriptions. No solver is imported.
"""
from __future__ import annotations

from math import hypot

try:
    from .audit_mobile_relay import _point, _preserved
    from .audit_repooling import _identity, _owners, _direct_task, _shared_flight, audit_repooling_proposal
    from .audit_weighted_relay_ablation import _geometry, audit_payload, reference_schedule
    from .audit_weighted_direct_pair import require, same
except ImportError:
    from audit_mobile_relay import _point, _preserved
    from audit_repooling import _identity, _owners, _direct_task, _shared_flight, audit_repooling_proposal
    from audit_weighted_relay_ablation import _geometry, audit_payload, reference_schedule
    from audit_weighted_direct_pair import require, same


MEETING_TYPE = "two_source_receiver_first_repooling"


def _source_proof(tasks,base_topology,current_topology,base_routes,routes,move,reference,new_sites,meeting):
    require((base_routes is None) == (move is None), "Original routes and quota move must be supplied together")
    if base_routes is None:
        return None
    require(isinstance(move,dict), "Missing source-bound four-task move")
    if not new_sites:
        return audit_repooling_proposal(tasks,current_topology,base_routes,routes,move,
                                        relay_enabled=move.get("mode") == "RELAY")
    for name in ("i","j","k1","k2"):
        require(type(move.get(name)) is int and move[name] in tasks, "Invalid mobile quota task: "+name)
    count=current_topology["drone_count"]
    for name in ("a","b","c"):
        require(type(move.get(name)) is int and 0 <= move[name] < count, "Invalid mobile quota aircraft: "+name)
    i,j,k1,k2=(move[name] for name in ("i","j","k1","k2"))
    a,b,c=(move[name] for name in ("a","b","c"))
    selected={i,j,k1,k2}
    require(len(selected)==4 and len({a,b,c})==3, "Mobile quota move needs four tasks and three aircraft")
    require(move.get("mode")=="RELAY" and type(move.get("station_id")) is int
            and move["station_id"]==meeting["station_id"],
            "Source-bound move differs from the mobile extension")
    require({i,j}==set(meeting["task_ids"]), "Mobile quota move transfers a different task pair")
    old_registry=_geometry(tasks,base_topology)[2]
    current_registry=_geometry(tasks,current_topology)[2]
    original=reference_schedule(tasks,base_routes,base_topology)
    require(not original["handoffs"], "Mobile repooling requires a true original DIRECT input")
    original_owners,final_owners=_owners(base_routes,old_registry),_owners(routes,current_registry)
    require([original_owners[t] for t in (i,j,k1,k2)]==[a,b,c,c], "Original mobile quota sources differ")
    require([final_owners[t] for t in (i,j,k1,k2)]==[c,c,a,b], "Mobile quota final map differs")
    actual={h["task_id"]:h for h in reference["handoffs"]}
    require(actual[i]["inbound_drone_id"]==a+1 and actual[j]["inbound_drone_id"]==b+1,
            "Mobile quota cargo uses the wrong original sender")
    require([r["task_count"] for r in original["route_records"]]==[r["task_count"] for r in reference["route_records"]],
            "Mobile quota move changed final responsibility counts")
    old_native=tuple(tuple(v for v in route if _identity(v,old_registry) not in selected) for route in base_routes)
    new_native=tuple(tuple(v for v in route if _identity(v,current_registry) not in selected) for route in routes)
    require(old_native==new_native, "Mobile quota move changed an unrelated event owner or relative order")
    require(all(_direct_task(routes,current_registry,t) for t in (k1,k2)), "Mobile compensation must be whole DIRECT tasks")
    for field,expected in (("final_aircraft",[c,c,a,b]),("source_aircraft",[a,b])):
        if field in move:
            require(type(move[field]) is list and all(type(value) is int for value in move[field])
                    and move[field]==expected, "Source-bound mobile descriptor differs: "+field)
    if "receiver_aircraft" in move:
        require(type(move["receiver_aircraft"]) is int and move["receiver_aircraft"]==c,
                "Source-bound mobile receiver differs")
    if "compensation_mode" in move:
        require(move["compensation_mode"]=="DIRECT", "Source-bound mobile compensation mode differs")
    if "shared_double_loaded_km" in move:
        same(move["shared_double_loaded_km"],meeting["shared_double_loaded_km"],"source-bound mobile shared distance")
    return dict(passed=True,original_score=original["score"],
        original_final_owners={str(t):owner for t,owner in sorted(original_owners.items())},
        selected_final_map={str(t):final_owners[t] for t in (i,j,k1,k2)},
        original_final_counts_preserved=True,native_event_owners_and_relative_order_preserved=True)


def audit_mobile_repooling_routes(tasks,base_topology,current_topology,routes,*,base_routes=None,move=None):
    """Rebuild service, receiver-first contacts and all actual flight/wait clocks."""
    homes,stations,registry,count,capacity,maximum,speed,new_sites,old_legs=_preserved(tasks,base_topology,current_topology)
    require(len(new_sites)<=1, "Mobile repooling supports at most one appended meeting point")
    reference=reference_schedule(tasks,routes,current_topology)
    final_owners=_owners(routes,registry)
    meetings=[]
    if new_sites:
        require(capacity==2, "Mobile repooling requires capacity two")
        site=next(iter(new_sites))
        new_legs={identifier:leg for identifier,leg in registry.items() if identifier not in old_legs}
        require(len(new_legs)==4 and all(leg["relay_id"]==site for leg in new_legs.values()),
                "Mobile repooling must register only four new IN/OUT legs at one point")
        task_ids=sorted({leg["task_id"] for leg in new_legs.values()})
        require(len(task_ids)==2, "Mobile repooling must transfer exactly two distinct tasks")
        require(all(sorted(leg["kind"] for leg in new_legs.values() if leg["task_id"]==task)==["RELAY_IN","RELAY_OUT"]
                    for task in task_ids), "Mobile task does not have one IN and one OUT leg")
        used={abs(v) for route in routes for v in route if abs(v) in registry}
        require(used==set(new_legs), "Mobile repooling contains an unused new leg or an active old relay plan")
        handoffs={h["task_id"]:h for h in reference["handoffs"]}
        require(set(handoffs)==set(task_ids), "Mobile repooling must use only its two new relay plans")
        sources=[handoffs[task]["inbound_drone_id"]-1 for task in task_ids]
        receivers={handoffs[task]["outbound_drone_id"]-1 for task in task_ids}
        require(len(set(sources))==2 and len(receivers)==1, "Mobile repooling needs two distinct sources and one receiver")
        receiver=next(iter(receivers))
        require(receiver not in sources and all(handoffs[t]["cross_uav"] for t in task_ids),
                "Mobile repooling needs three different aircraft and two true cross transfers")
        clocks=reference["event_times_min"]
        drop_positions=[routes[aircraft].index(-handoffs[task]["in_leg"]) for task,aircraft in zip(task_ids,sources)]
        takes=sorted((routes[receiver].index(handoffs[task]["out_leg"]),task) for task in task_ids)
        require(takes[1][0]==takes[0][0]+1, "Mobile receiver must take both parcels in one contiguous OUT block")
        arrivals,held_at_drop=[],[]
        for task,aircraft,position in zip(task_ids,sources,drop_positions):
            previous=_point(tasks,stations,registry,routes[aircraft][position-1]) if position else homes[aircraft]
            before=clocks[aircraft][position-1] if position else 0.
            arrival=before+hypot(stations[site][0]-previous[0],stations[site][1]-previous[1])/speed
            arrivals.append(arrival)
            cargo=set()
            for visit in routes[aircraft][:position]:
                if visit>0:
                    cargo.add(visit)
                else:
                    cargo.remove(-visit)
            require(handoffs[task]["in_leg"] in cargo, "Mobile source did not hold its own parcel until contact")
            held_at_drop.append(len(cargo))
            same(clocks[aircraft][position],arrival,"mobile source drop must equal its own physical arrival")
            same(handoffs[task]["pickup_time_min"],arrival,"mobile source must meet receiver without ground caching")
            same(handoffs[task]["package_cache_min"],0.,"mobile parcel ground cache must be zero")
            if position+1<len(routes[aircraft]):
                successor=_point(tasks,stations,registry,routes[aircraft][position+1])
                required=handoffs[task]["pickup_time_min"]+hypot(successor[0]-stations[site][0],successor[1]-stations[site][1])/speed
                if clocks[aircraft][position+1]<required:
                    same(clocks[aircraft][position+1],required,"mobile source departed before receiver ACK")
        first_position=takes[0][0]
        previous=_point(tasks,stations,registry,routes[receiver][first_position-1]) if first_position else homes[receiver]
        before=clocks[receiver][first_position-1] if first_position else 0.
        receiver_arrival=before+hypot(stations[site][0]-previous[0],stations[site][1]-previous[1])/speed
        if receiver_arrival>min(arrivals):
            same(receiver_arrival,min(arrivals),"mobile receiver must arrive before both sources")
        ordered_arrivals=[arrivals[task_ids.index(task)] for _,task in takes]
        if ordered_arrivals[0]>ordered_arrivals[1]:
            same(ordered_arrivals[0],ordered_arrivals[1],"mobile OUT order must follow actual source arrival")
        take_times=[clocks[receiver][position] for position,_ in takes]
        for actual,expected in zip(take_times,ordered_arrivals):
            same(actual,expected,"mobile take must occur at its source's arrival")
        identifiers=tuple(handoffs[task]["out_leg"] for task in task_ids)
        shared,segments,load_before=_shared_flight(tasks,homes,stations,registry,routes[receiver],receiver,identifiers,clocks[receiver])
        loads=[load_before[position] for position,_ in takes]
        require(loads==[0,1], "Mobile receiver load must start empty and rise from zero to one to two")
        require(shared>1e-12, "Mobile selected parcels must share a positive-distance flight")
        empty_wait=max(0.,min(arrivals)-receiver_arrival)
        loaded_wait=max(arrivals)-min(arrivals)
        same(sum(handoff["aircraft_waiting_min"] for handoff in handoffs.values()),empty_wait+loaded_wait,
             "mobile receiver empty/loaded waits must count the whole elapsed wait")
        drop_events=[dict(task_id=task,route_index=aircraft,position=position,
            visit=-handoffs[task]["in_leg"],physical_time_min=clocks[aircraft][position])
            for task,aircraft,position in zip(task_ids,sources,drop_positions)]
        pickup_events=[dict(task_id=task,route_index=receiver,position=position,
            visit=handoffs[task]["out_leg"],physical_time_min=clocks[receiver][position]) for position,task in takes]
        meetings.append(dict(meeting_type=MEETING_TYPE,kind=MEETING_TYPE,station_id=site,point=list(stations[site]),
            source_aircraft=sources,receiver_aircraft=receiver,task_ids=task_ids,
            source_drop_positions=drop_positions,receiver_take_positions=[position for position,_ in takes],
            arrival_times_min=arrivals+[receiver_arrival],source_arrival_order=[task for _,task in takes],
            source_drop_times_min=arrivals,receiver_take_times_min=take_times,
            receiver_empty_wait_min=empty_wait,receiver_loaded_wait_min=loaded_wait,
            source_load_before_drops=held_at_drop,receiver_load_before_takes=loads,
            receiver_load_after_takes=[value+1 for value in loads],
            drop_events=drop_events,pickup_events=pickup_events,ground_cache_min=0.,
            shared_double_loaded_km=shared,shared_loaded_segments=segments,
            aircraft_waiting_min=[0.,0.,empty_wait+loaded_wait]))
    proof=_source_proof(tasks,base_topology,current_topology,base_routes,routes,move,reference,new_sites,
                        meetings[0] if meetings else None)
    if proof is not None and meetings:
        compensation={move["i"]:move["k1"],move["j"]:move["k2"]}
        meetings[0]["compensation_task_ids"]=[compensation[task] for task in meetings[0]["task_ids"]]
    return dict(passed=True,model=MEETING_TYPE if new_sites else "generic_fixed_network",
        continuous_handoff_extension=bool(new_sites),new_mobile_site_count=len(new_sites),meetings=meetings,
        source_responsibility_proven=proof is not None,source_mapping_audit=proof,
        score=reference["score"],delivery_times_min=reference["delivery_times_min"],physical_event_times=reference["event_times_min"],
        final_owners={str(task):owner for task,owner in sorted(final_owners.items())},
        final_tasks_per_aircraft=[record["task_count"] for record in reference["route_records"]],
        total_mobile_aircraft_waiting_min=sum(m["receiver_empty_wait_min"]+m["receiver_loaded_wait_min"] for m in meetings),
        mobile_ground_cache_min=0.,core_reference=reference)


def _identity_equal(actual,expected):
    if type(expected) is int:
        return type(actual) is int and actual==expected
    return isinstance(actual,list) and len(actual)==len(expected) and all(
        _identity_equal(value,reference) for value,reference in zip(actual,expected))


def _bind(saved,actual,tasks,routes,registry):
    require(isinstance(saved,list) and len(saved)==len(actual), "Saved mobile repooling meeting count differs")
    for record,reference in zip(saved,actual):
        require(isinstance(record,dict) and record.get("meeting_type")==MEETING_TYPE, "Saved mobile repooling type differs")
        if "kind" in record:
            require(record["kind"]==MEETING_TYPE, "Saved mobile repooling kind differs")
        require(type(record.get("station_id")) is int and record["station_id"]==reference["station_id"],
                "Saved mobile repooling station differs")
        point=record.get("point")
        require(isinstance(point,list) and len(point)==2, "Invalid saved mobile repooling point")
        for actual_value,expected in zip(point,reference["point"]):
            same(actual_value,expected,"saved mobile repooling point")
        for field in ("source_aircraft","task_ids","source_drop_positions","receiver_take_positions",
                      "source_arrival_order","source_load_before_drops","receiver_load_before_takes","receiver_load_after_takes"):
            value=record.get(field)
            require(isinstance(value,list) and all(type(item) is int for item in value)
                    and value==reference[field], "Saved mobile repooling identity/load differs: "+field)
        require(type(record.get("receiver_aircraft")) is int and record["receiver_aircraft"]==reference["receiver_aircraft"],
                "Saved mobile repooling receiver differs")
        for field in ("arrival_times_min","source_drop_times_min","receiver_take_times_min"):
            value=record.get(field)
            require(isinstance(value,list) and len(value)==len(reference[field]), "Invalid saved mobile repooling times: "+field)
            for actual_value,expected in zip(value,reference[field]):
                same(actual_value,expected,"saved mobile repooling "+field)
        for field in ("receiver_empty_wait_min","receiver_loaded_wait_min","ground_cache_min","shared_double_loaded_km"):
            same(record.get(field),reference[field],"saved mobile repooling "+field)
        if "aircraft_waiting_min" in record:
            require(isinstance(record["aircraft_waiting_min"],list) and len(record["aircraft_waiting_min"])==3,
                    "Invalid saved mobile repooling aircraft waits")
            for actual_value,expected in zip(record["aircraft_waiting_min"],reference["aircraft_waiting_min"]):
                same(actual_value,expected,"saved mobile repooling aircraft wait")
        aliases=dict(source_task_ids=reference["task_ids"],
            aircraft=reference["source_aircraft"]+[reference["receiver_aircraft"]],
            own_task_groups=[[task] for task in reference["task_ids"]]+[[]],
            cut_positions=reference["source_drop_positions"]+[reference["receiver_take_positions"][0]],
            take_order_task_ids=reference["source_arrival_order"],
            waiting_loads=reference["source_load_before_drops"]+[0])
        for field,expected in aliases.items():
            if field in record:
                require(_identity_equal(record[field],expected), "Saved mobile repooling alias identity/load differs: "+field)
        for field,expected in (("synchronization_min",max(reference["source_drop_times_min"])),
            ("receiver_waiting_empty_min",reference["receiver_empty_wait_min"]),
            ("receiver_waiting_one_parcel_min",reference["receiver_loaded_wait_min"])):
            if field in record:
                same(record[field],expected,"saved mobile repooling alias "+field)
        if "handoff_times_min" in record:
            require(isinstance(record["handoff_times_min"],list) and len(record["handoff_times_min"])==2,
                    "Invalid saved mobile handoff times")
            for value,expected in zip(record["handoff_times_min"],reference["source_drop_times_min"]):
                same(value,expected,"saved mobile handoff times")
        for field in ("drop_events","pickup_events"):
            value=record.get(field)
            require(isinstance(value,list) and len(value)==2, "Missing saved mobile repooling contact events")
            for event,expected in zip(value,reference[field]):
                require(isinstance(event,dict) and all(type(event.get(key)) is int and event[key]==expected[key]
                    for key in ("task_id","route_index","position","visit")), "Saved mobile repooling contact identity differs")
                same(event.get("physical_time_min"),expected["physical_time_min"],"saved mobile repooling contact time")
        if "compensation_task_ids" in record:
            values=record["compensation_task_ids"]
            require(isinstance(values,list) and len(values)==2 and all(type(task) is int and task in tasks for task in values)
                    and len(set(values+reference["task_ids"]))==4, "Invalid saved mobile compensation IDs")
            owners=_owners(routes,registry)
            require(all(_direct_task(routes,registry,task) and owners[task]==aircraft
                for task,aircraft in zip(values,reference["source_aircraft"])), "Saved mobile compensation current final map differs")
            if "compensation_task_ids" in reference:
                require(values==reference["compensation_task_ids"], "Saved compensation differs from source-bound move")
        if "repooling_move" in record:
            descriptor=record["repooling_move"]
            require(isinstance(descriptor,dict), "Invalid saved mobile quota descriptor")
            expected=dict(i=reference["task_ids"][0],j=reference["task_ids"][1],
                a=reference["source_aircraft"][0],b=reference["source_aircraft"][1],
                c=reference["receiver_aircraft"],station_id=reference["station_id"])
            require(all(type(descriptor.get(key)) is int and descriptor[key]==value for key,value in expected.items())
                    and descriptor.get("mode")=="RELAY", "Saved mobile quota descriptor current sources differ")
            compensation=[descriptor.get("k1"),descriptor.get("k2")]
            require(all(type(task) is int and task in tasks for task in compensation)
                    and len(set(compensation+reference["task_ids"]))==4,
                    "Invalid saved mobile quota descriptor compensation")
            owners=_owners(routes,registry)
            require(all(_direct_task(routes,registry,task) and owners[task]==aircraft
                for task,aircraft in zip(compensation,reference["source_aircraft"])),
                "Saved mobile quota descriptor compensation current map differs")
            if "compensation_task_ids" in record:
                require(compensation==record["compensation_task_ids"], "Saved mobile compensation aliases differ")
            if "compensation_task_ids" in reference:
                require(compensation==reference["compensation_task_ids"], "Saved quota descriptor differs from source-bound move")
            for field,value in (("final_aircraft",[reference["receiver_aircraft"]]*2+reference["source_aircraft"]),
                                ("source_aircraft",reference["source_aircraft"]),
                                ("receiver_aircraft",reference["receiver_aircraft"])):
                if field in descriptor:
                    require(_identity_equal(descriptor[field],value), "Saved mobile quota descriptor alias differs: "+field)
            if "compensation_mode" in descriptor:
                require(descriptor["compensation_mode"]=="DIRECT", "Saved mobile quota descriptor compensation mode differs")
            if "shared_double_loaded_km" in descriptor:
                same(descriptor["shared_double_loaded_km"],reference["shared_double_loaded_km"],"saved mobile quota descriptor distance")


def audit_mobile_repooling_payload(tasks,base_topology,current_topology,payload,*,base_routes=None,move=None):
    """Bind all canonical flights/clocks plus the saved three-UAV contact data."""
    audit_payload(tasks,current_topology,payload)
    routes=[route["encoded_visits"] for route in payload["routes"]]
    result=audit_mobile_repooling_routes(tasks,base_topology,current_topology,routes,base_routes=base_routes,move=move)
    saved=payload.get("mobile_meetings",payload.get("metadata",{}).get("mobile_meetings"))
    if result["new_mobile_site_count"] or saved is not None:
        _bind(saved,result["meetings"],tasks,routes,_geometry(tasks,current_topology)[2])
    return result
