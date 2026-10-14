"""Independent full-route audit of fixed-H deadline-window repooling.

Frozen standard-library references only: no submitted search/cost code runs.
A complete window is rebuilt from actual source prefixes and receiver suffix;
optimistic family previews cannot certify full task coverage or global cost.
Design deadline_rescue_window_design_v1.md SHA256:
249123bd351676ff3d670ce40d57b992dbb8af6caddc651d2b75f103df974e6e.
"""
from __future__ import annotations
from math import hypot,isclose,isfinite
try:
    from .audit_repooling import _identity,_owners,_direct_task,_shared_flight
    from .audit_mobile_relay import _point
    from .audit_weighted_relay_ablation import _geometry,reference_schedule
    from .audit_weighted_direct_pair import require,same
except ImportError:
    from audit_repooling import _identity,_owners,_direct_task,_shared_flight
    from audit_mobile_relay import _point
    from audit_weighted_relay_ablation import _geometry,reference_schedule
    from audit_weighted_direct_pair import require,same

EPSILON=1e-9
SHARED_EPSILON_KM=1e-10


def _bind(actual,expected,label):
    if isinstance(expected,dict):
        require(isinstance(actual,dict) and set(actual)==set(expected),label+' fields differ')
        for k,v in expected.items():_bind(actual[k],v,label+'.'+str(k))
    elif isinstance(expected,(list,tuple)):
        require(isinstance(actual,(list,tuple)) and len(actual)==len(expected),label+' shape differs')
        for i,v in enumerate(expected):_bind(actual[i],v,label+'['+str(i)+']')
    elif expected is None or isinstance(expected,(bool,str)):
        require(type(actual) is type(expected) and actual==expected,label+' differs')
    elif type(expected) is int:
        require(type(actual) is int and actual==expected,label+' integer differs')
    else:same(actual,expected,label)


def _late(tasks,clocks,t):return clocks[str(t)]-tasks[t][2]>EPSILON


def _changes(tasks,before,after,selected):
    rescued=[t for t in sorted(tasks) if _late(tasks,before,t) and not _late(tasks,after,t)]
    lost=[t for t in sorted(tasks) if not _late(tasks,before,t) and _late(tasks,after,t)]
    return dict(selected_rescued_task_ids=[t for t in rescued if t in selected],
        native_rescued_task_ids=[t for t in rescued if t not in selected],
        selected_newly_late_task_ids=[t for t in lost if t in selected],
        native_newly_late_task_ids=[t for t in lost if t not in selected])


def _window(tasks,homes,stations,registry,speed,routes,ref,base,*,i,j,c,station,availability,sigma,required):
    require(type(sigma) in (int,float) and isfinite(sigma) and sigma>=0,'Invalid window safety sigma')
    require(isinstance(required,(tuple,list)) and all(type(t) is int for t in required)
        and len(required)==len(set(required)),'Invalid window required task IDs')
    hs={h['task_id']:h for h in ref['handoffs']};route=routes[c]
    takes=sorted(route.index(hs[t]['out_leg']) for t in (i,j));first,second=takes
    previous=_point(tasks,stations,registry,route[first-1]) if first else homes[c]
    arrival=(ref['event_times_min'][c][first-1] if first else 0.)+hypot(
        stations[station][0]-previous[0],stations[station][1]-previous[1])/speed
    T=max(arrival,*availability.values());order=[registry[route[p]]['task_id'] for p in takes]
    same(ref['event_times_min'][c][first],max(arrival,availability[str(order[0])]),'first actual OUT take')
    same(ref['event_times_min'][c][second],T,'actual max second OUT take')
    point=stations[station];elapsed=0.;offsets={}
    for p in range(second+1,len(route)):
        v=route[p];target=_point(tasks,stations,registry,v)
        elapsed+=hypot(target[0]-point[0],target[1]-point[1])/speed;point=target
        same(ref['event_times_min'][c][p],T+elapsed,'actual receiver suffix clock')
        if v<0 and (abs(v) not in registry or registry[abs(v)]['kind']=='RELAY_OUT'):
            t=_identity(v,registry);offsets[str(t)]=elapsed
            same(ref['delivery_times_min'][str(t)],T+elapsed,'actual suffix final delivery')
    require({i,j}<={int(t) for t in offsets},'Window lost selected suffix deliveries')
    require(set(required)<={int(t) for t in offsets},'Window required task outside actual suffix')
    bounds={str(t):tasks[t][2]+EPSILON-sigma-offsets[str(t)] for t in required}
    latest=min(bounds.values()) if bounds else None
    c_tasks=[t for t,a in _owners(routes,registry).items() if a==c]
    before,after=base['delivery_times_min'],ref['delivery_times_min']
    rescued=sorted(t for t in c_tasks if _late(tasks,before,t) and not _late(tasks,after,t))
    lost=sorted(t for t in c_tasks if not _late(tasks,before,t) and _late(tasks,after,t))
    safe=sorted(t for t in c_tasks if _late(tasks,before,t) and after[str(t)]<=tasks[t][2]+EPSILON-sigma)
    return dict(station_id=station,task_ids=sorted((i,j)),take_order_task_ids=order,source_availability_min=availability,
        receiver_arrival_min=arrival,second_take_min=T,
        selected_delivery_offsets_min={str(t):offsets[str(t)] for t in sorted((i,j))},
        suffix_delivery_offsets_min=offsets,official_lateness_epsilon_min=EPSILON,sigma_min=sigma,
        required_on_time_task_ids=list(required),latest_second_take_min_by_task=bounds,
        joint_latest_second_take_min=latest,safe_window_satisfied=(T<=latest if latest is not None else None),
        official_rescued_task_ids=rescued,official_new_late_task_ids=lost,
        safe_rescued_task_ids=safe,selected_safe_rescued_task_ids=[t for t in safe if t in (i,j)],
        selected_rescued_task_ids=[t for t in rescued if t in (i,j)],
        selected_new_late_task_ids=[t for t in lost if t in (i,j)],
        native_rescued_task_ids=[t for t in rescued if t not in (i,j)],native_new_late_task_ids=[t for t in lost if t not in (i,j)],
        classification_scope='all_deliveries_on_this_receiver_route',
        failure_scope='declared_safety_channel_only_not_global_J_or_ALNS_bound')



def _preview_scan(tasks,stations,registry,route,home,capacity,speed,availability):
    held=set();seen=set();point=home;clock=0.;times=[];deliveries={};loads=[]
    for v in route:
        require(type(v) is int and v!=0 and v not in seen,'Invalid/duplicate preview event')
        seen.add(v);identifier=abs(v);leg=registry.get(identifier)
        require(identifier in tasks or leg is not None,'Unknown preview transport event')
        target=_point(tasks,stations,registry,v);clock+=hypot(target[0]-point[0],target[1]-point[1])/speed
        if v>0:
            require(identifier not in held and -identifier not in seen,'Preview pickup follows delivery')
            if leg and leg['kind']=='RELAY_OUT':
                require(leg['task_id'] in availability,'Preview OUT has no real source availability')
                clock=max(clock,availability[leg['task_id']])
            held.add(identifier);require(len(held)<=capacity,'Preview capacity violation')
        else:
            require(identifier in held,'Preview delivery without held cargo');held.remove(identifier)
            if not leg or leg['kind']=='RELAY_OUT':deliveries[str(_identity(v,registry))]=clock
        times.append(clock);loads.append(len(held));point=target
    require(not held and all(-v in seen for v in seen),'Incomplete preview transport leg')
    return times,deliveries,loads


def _preview(tasks,topology,base_routes,base,family,homes,stations,registry,capacity,maximum,speed):
    """Witness only: original-C quota may overflow, without relaxing full audit."""
    saved=family.get('selector_window');require(isinstance(saved,dict),'Missing true-base selector-window witness')
    require(saved.get('optimistic_missing_comp') is True and saved.get('preview_complete_solution') is False,
        'Selector preview must explicitly be incomplete/missing compensation')
    i,j,a,b,c,H=(family[n] for n in ('i','j','a','b','c','station_id'))
    pairs={t:{leg['kind']:identifier for identifier,leg in registry.items() if leg['task_id']==t and leg['relay_id']==H} for t in (i,j)}
    require(all(set(pair)=={'RELAY_IN','RELAY_OUT'} for pair in pairs.values()),'Preview legs missing at original H')
    source=saved.get('source_preview_routes');require(isinstance(source,dict) and set(source)=={str(i),str(j)},'Preview needs two real source routes')
    routes=[tuple(r) for r in base_routes];event_times=[list(r) for r in base['event_times_min']]
    deliveries=dict(base['delivery_times_min']);availability={};prefix_records=[]
    for t,owner in ((i,a),(j,b)):
        route=source[str(t)];require(isinstance(route,(list,tuple)),'Invalid source preview route')
        identifier=pairs[t]['RELAY_IN']
        require(sum(v==identifier for v in route)==sum(v==-identifier for v in route)==1,'Preview must contain unique same-H IN pair')
        require(tuple(v for v in route if abs(v)!=identifier)==tuple(v for v in base_routes[owner] if abs(v)!=t),
            'Source preview must only replace its original DIRECT task with IN')
        times,finals,_=_preview_scan(tasks,stations,registry,route,homes[owner],capacity,speed,{})
        availability[str(t)]=times[route.index(-identifier)];routes[owner]=tuple(route);event_times[owner]=times;deliveries.update(finals)
        prefix_records.append(dict(aircraft=owner,task_id=t,drop_time_min=availability[str(t)]))
    gap=saved.get('receiver_gap_index');original=base_routes[c]
    require(type(gap) is int and 0<=gap<=len(original),'Preview receiver gap index invalid')
    require((base['route_records'][c]['load_after_event'][gap-1] if gap else 0)==0,'Preview receiver gap is not empty in original route')
    route=saved.get('receiver_preview_route');require(isinstance(route,(list,tuple)),'Missing actual receiver preview route')
    ids={pairs[t]['RELAY_OUT'] for t in (i,j)};block=route[gap:gap+4]
    require(len(block)==4 and set(block[:2])==ids and set(-v for v in block[2:])==ids,
        'Preview receiver block must take two then deliver two')
    order=[registry[-v]['task_id'] for v in block[2:]]
    _bind(saved.get('delivery_order_task_ids'),order,'preview delivery order')
    require(tuple(route[:gap])==tuple(original[:gap]) and tuple(route[gap+4:])==tuple(original[gap:]),
        'Receiver preview must only insert OUT block into actual original C gap')
    times,finals,_=_preview_scan(tasks,stations,registry,route,homes[c],capacity,speed,{int(t):v for t,v in availability.items()})
    routes[c]=tuple(route);event_times[c]=times;deliveries.update(finals)
    shared,_,_=_shared_flight(tasks,homes,stations,registry,route,c,ids,times)
    require(shared>SHARED_EPSILON_KM,'Preview receiver needs positive joint loaded flight')
    overflow=max(0,base['route_records'][c]['task_count']+2-maximum)
    require(type(saved.get('receiver_quota_overflow')) is int and saved['receiver_quota_overflow']==overflow,
        'Preview receiver quota overflow must be explicit and actual')
    declared=saved.get('window');require(isinstance(declared,dict),'Missing selector preview window arithmetic')
    require(declared.get('official_lateness_epsilon_min')==EPSILON,'Preview official lateness epsilon changed')
    reference=dict(event_times_min=event_times,delivery_times_min=deliveries,
        handoffs=[dict(task_id=t,out_leg=pairs[t]['RELAY_OUT']) for t in (i,j)])
    require(declared.get('required_on_time_task_ids') and any(t in (i,j) and _late(tasks,base['delivery_times_min'],t)
        for t in declared['required_on_time_task_ids']),'Preview window needs an originally late pooled target')
    window=_window(tasks,homes,stations,registry,speed,routes,reference,base,i=i,j=j,c=c,station=H,
        availability=availability,sigma=declared.get('sigma_min'),required=declared.get('required_on_time_task_ids'))
    _bind(declared,window,'real selector preview window')
    require(set(deliveries)=={str(t) for t in tasks},'Preview delivery coverage differs from original task set')
    losses=[deliveries[str(t)]-tasks[t][2] for t in tasks if _late(tasks,deliveries,t)]
    distance=0.
    for owner,route in enumerate(routes):
        point=homes[owner]
        for v in route:
            target=_point(tasks,stations,registry,v);distance+=hypot(target[0]-point[0],target[1]-point[1]);point=target
    preview_score=dict(late_count=len(losses),total_lateness_min=float(sum(losses)),distance_km=distance)
    delta={f:preview_score[f]-base['score'][f] for f in preview_score}
    classifications=_changes(tasks,base['delivery_times_min'],deliveries,{i,j})
    for name,expected in (('preview_route_table_score',preview_score),('preview_ranking_delta',delta),('preview_classifications',classifications)):
        require(name in saved,'Missing true preview ranking field: '+name);_bind(saved[name],expected,'real selector '+name)
    if 'initial_index_proxy_omits_receiver_native_delay' in saved:
        value=saved['initial_index_proxy_omits_receiver_native_delay']
        require(type(value) in (int,float) and isfinite(value),'Initial index proxy must be finite; it is not a physical bound')
    return dict(preview_route_table_score=preview_score,preview_ranking_delta=delta,preview_classifications=classifications,
        preview_complete_solution=False,optimistic_missing_comp=True,receiver_quota_overflow=overflow,
        source_prefix_records=prefix_records,receiver_gap_index=gap,window=window,
        scope='this original-route directory witness only; not a bound after compensation or ordinary repair')


def _snapshot(saved,routes,move,result,weights,scales,tasks,topology):
    require(isinstance(saved,dict) and saved.get('valid') is True and saved.get('complete') is True,
        'Deadline-window snapshot must be complete/valid')
    require(saved.get('routes')==[list(r) for r in routes] or saved.get('routes')==tuple(tuple(r) for r in routes),
        'Deadline-window snapshot routes differ')
    require(saved.get('move')==move,'Deadline-window snapshot source move differs')
    require(not saved.get('optimistic_missing_comp',False),'Optimistic partial is not a complete macro')
    for name in ('score','table_score'):
        require(name in saved,'Missing full deadline-window '+name)
        _bind(saved[name],result['score'],'deadline-window full '+name)
    for name in ('event_times_min','delivery_times_min','source_drop_availability_min','actual_final_owners',
                 'shared_loaded_distance_km','window','selected_rescued_task_ids','native_rescued_task_ids',
                 'selected_newly_late_task_ids','native_newly_late_task_ids','pooled_rescued_task_ids','compensation_rescued_task_ids'):
        require(name in saved,'Missing deadline-window snapshot '+name)
        _bind(saved[name],result[name],'deadline-window snapshot '+name)
    for name,expected in (('cross_uav_count',result['actual_cross_uav_count']),('relay_task_count',len(result['handoffs']))):
        require(type(saved.get(name)) is int and saved[name]==expected,'Deadline-window '+name+' differs')
    if 'topology' in saved:require(_geometry(tasks,saved['topology'])==_geometry(tasks,topology),'Topology/resources differ')
    if weights is not None or scales is not None:
        require(isinstance(weights,(list,tuple)) and isinstance(scales,(list,tuple)) and len(weights)==len(scales)==3
            and all(type(x) in (int,float) and isfinite(x) and x>0 for x in (*weights,*scales))
            and isclose(sum(weights),1.),'Invalid deadline-window objective coefficients')
        physical=[result['score'][f] for f in ('late_count','total_lateness_min','distance_km')]
        _bind(saved.get('key'),[sum(w*x/s for w,x,s in zip(weights,physical,scales)),*physical],'deadline-window weighted key')



def _source_family(tasks,topology,base_routes,family):
    require(topology.get('distance_matrix_km') is None,'Deadline-window audit requires Euclidean geometry')
    homes,stations,registry,count,capacity,maximum,speed=_geometry(tasks,topology)
    require(capacity==2,'Deadline-window experiment requires capacity two')
    require(isinstance(family,dict),'Missing deadline-window family')
    for name in ('i','j','k1','k2'):
        require(type(family.get(name)) is int and family[name] in tasks,'Invalid family task: '+name)
    for name in ('a','b','c'):
        require(type(family.get(name)) is int and 0<=family[name]<count,'Invalid family aircraft: '+name)
    i,j,k1,k2=(family[n] for n in ('i','j','k1','k2'));a,b,c=(family[n] for n in ('a','b','c'))
    selected={i,j,k1,k2};require(len(selected)==4 and len({a,b,c})==3,'Four distinct tasks/three aircraft required')
    station=family.get('station_id');require(type(station) is int and station in stations,'Family station must be original fixed H')
    _bind(family.get('task_ids'),sorted(selected),'family task coverage')
    base=reference_schedule(tasks,base_routes,topology);require(not base['handoffs'],'Real source must be a pure DIRECT complete solution')
    require(any(_late(tasks,base['delivery_times_min'],t) for t in (i,j)),
        'Deadline-window family must target an originally late selected parcel')
    original=_owners(base_routes,registry)
    require([original[t] for t in (i,j,k1,k2)]==[a,b,c,c],'Original source ownership differs')
    preview=_preview(tasks,topology,base_routes,base,family,homes,stations,registry,capacity,maximum,speed)
    return dict(geometry=(homes,stations,registry,count,capacity,maximum,speed),task_ids=(i,j,k1,k2),aircraft=(a,b,c),
        selected=selected,station_id=station,base=base,original=original,preview=preview)


def audit_deadline_window_family(tasks,topology,base_routes,family):
    """Audit a selected directory witness even when no full macro was built."""
    context=_source_family(tasks,topology,base_routes,family)
    return dict(passed=True,complete_solution=False,family=family,selected_task_ids=sorted(context['selected']),
        base_score=context['base']['score'],source_owners={str(t):a for t,a in sorted(context['original'].items())},
        selector_preview=context['preview'])


def audit_deadline_window_repooling_proposal(tasks,topology,base_routes,proposal_routes,move,*,relay_enabled,
                                            telemetry=None,weights=None,scales=None):
    """Audit complete real provenance, twelve-map DIRECT freedom and window."""
    require(type(relay_enabled) is bool and isinstance(move,dict),'Invalid deadline-window arm/move')
    family=move.get('family');context=_source_family(tasks,topology,base_routes,family)
    homes,stations,registry,count,capacity,maximum,speed=context['geometry']
    i,j,k1,k2=context['task_ids'];a,b,c=context['aircraft'];selected=context['selected'];station=context['station_id']
    base,original,preview=context['base'],context['original'],context['preview']
    require(move.get('mode')==('RELAY' if relay_enabled else 'DIRECT'),'Saved deadline-window arm differs')
    if 'station_id' in move:require(move['station_id']==(station if relay_enabled else None),'Saved handoff station differs')
    for name in ('i','j','k1','k2','a','b','c'):
        if name in move:require(type(move[name]) is int and move[name]==family[name],'Duplicated source field differs: '+name)
    ref=reference_schedule(tasks,proposal_routes,topology)
    original,final=_owners(base_routes,registry),_owners(proposal_routes,registry)
    require([original[t] for t in (i,j,k1,k2)]==[a,b,c,c],'Original source ownership differs')
    counts=[r['task_count'] for r in ref['route_records']]
    require(counts==[r['task_count'] for r in base['route_records']],'Deadline-window final quotas changed')
    without=lambda rs:tuple(tuple(v for v in r if _identity(v,registry) not in selected) for r in rs)
    require(without(base_routes)==without(proposal_routes),'Deadline-window moved/reordered an unrelated native event')
    require(all(_direct_task(proposal_routes,registry,t) for t in set(tasks)-{i,j}),'Compensation/native tasks must stay whole DIRECT')
    groups=move.get('final_task_groups')
    require(isinstance(groups,(list,tuple)) and len(groups)==3 and all(isinstance(g,(list,tuple)) for g in groups) and [len(g) for g in groups]==[1,1,2]
        and all(type(t) is int for g in groups for t in g) and sorted(t for g in groups for t in g)==sorted(selected),
        'DIRECT control requires an A1/B1/C2 whole-task partition')
    expected={str(t):owner for owner,g in zip((a,b,c),groups) for t in g}
    require(all(final[t]==expected[str(t)] for t in selected),'Actual selected final roles differ')
    _bind(move.get('final_aircraft_by_task'),expected,'saved selected owners')
    _bind(move.get('actual_final_owners'),{str(t):owner for t,owner in sorted(final.items())},'move actual final owners')
    availability={};inbound=[];outbound=None;window=None
    if relay_enabled:
        require([final[t] for t in (i,j,k1,k2)]==[c,c,a,b],'RELAY changed its two-source compensation role map')
        hs={h['task_id']:h for h in ref['handoffs']}
        require(len(hs)==2 and set(hs)=={i,j},'Exactly two selected single-hop relay plans required')
        for task,source in ((i,a),(j,b)):
            h=hs[task];require(h['cross_uav'] and h['inbound_drone_id']==source+1
                and h['outbound_drone_id']==c+1 and h['relay_id']==station,'Wrong original source/receiver/same-H true cross')
            route=proposal_routes[source];drop=route.index(-h['in_leg']);point=homes[source];clock=0.
            for p,v in enumerate(route[:drop+1]):
                require(registry.get(abs(v),{}).get('kind')!='RELAY_OUT','Source prefix cannot depend on relay OUT')
                target=_point(tasks,stations,registry,v);clock+=hypot(target[0]-point[0],target[1]-point[1])/speed;point=target
                same(clock,ref['event_times_min'][source][p],'real independent source prefix clock')
            same(clock,h['drop_time_min'],'real IN drop availability');availability[str(task)]=clock
            inbound.append(dict(aircraft=source,task_id=task,pickup_position=route.index(h['in_leg']),drop_position=drop,drop_time_min=clock))
        ids=[hs[t]['out_leg'] for t in (i,j)];route=proposal_routes[c]
        pos=[p for p,v in enumerate(route) if abs(v) in ids]
        require(len(pos)==4 and pos==list(range(pos[0],pos[0]+4)) and [route[p]>0 for p in pos]==[True,True,False,False],
            'Receiver needs one consecutive OUT four-event ++-- block')
        distance,segments,loads=_shared_flight(tasks,homes,stations,registry,route,c,ids,ref['event_times_min'][c])
        require([loads[p] for p in pos[:2]]==[0,1],'Receiver two OUT takes must start empty')
        require(distance>SHARED_EPSILON_KM,'Receiver pair needs positive joint loaded flight')
        declared=move.get('window');require(declared is None or isinstance(declared,dict),'Invalid declared complete window')
        if declared is not None:
            require(not declared.get('optimistic_missing_comp',False),'Optimistic partial window cannot certify full macro')
            require(declared.get('official_lateness_epsilon_min')==EPSILON,'Official lateness epsilon changed')
        sigma=declared.get('sigma_min',0.) if declared is not None else 0.
        required=declared.get('required_on_time_task_ids',[i,j]) if declared is not None else [i,j]
        window=_window(tasks,homes,stations,registry,speed,proposal_routes,ref,base,i=i,j=j,c=c,
            station=station,availability=availability,sigma=sigma,required=required)
        outbound=dict(aircraft=c,event_positions=pos,take_task_ids=[registry[route[p]]['task_id'] for p in pos[:2]],
            arrival_min=window['receiver_arrival_min'],second_take_min=window['second_take_min'],
            aircraft_waiting_min=window['second_take_min']-window['receiver_arrival_min'],load_before_takes=[loads[p] for p in pos[:2]])
    else:
        require(not ref['handoffs'] and all(_direct_task(proposal_routes,registry,t) for t in selected),'DIRECT control cannot contain relay plans')
        ids=sorted(t for t in selected if final[t]==c)
        distance,segments,loads=_shared_flight(tasks,homes,stations,registry,proposal_routes[c],c,ids,ref['event_times_min'][c])
    result=dict(passed=True,mode=move['mode'],family=family,selected_task_ids=sorted(selected),
        selector_preview=preview,source_owners={str(t):owner for t,owner in sorted(original.items())},actual_final_owners={str(t):owner for t,owner in sorted(final.items())},
        final_tasks_per_aircraft=counts,base_score={**base['score'],'total_lateness_min':float(base['score']['total_lateness_min']),'distance_km':float(base['score']['distance_km'])},
        score={**ref['score'],'total_lateness_min':float(ref['score']['total_lateness_min']),'distance_km':float(ref['score']['distance_km'])},
        source_drop_availability_min=availability,shared_loaded_distance_km=float(distance),shared_loaded_segments=segments,
        inbound_records=inbound,outbound_record=outbound,window=window,actual_cross_uav_count=ref['relay_statistics']['cross_uav_handoff_count'],
        delivery_times_min=ref['delivery_times_min'],event_times_min=ref['event_times_min'],handoffs=ref['handoffs'],
        core_reference=ref,**_changes(tasks,base['delivery_times_min'],ref['delivery_times_min'],selected))
    result['pooled_rescued_task_ids']=[t for t in result['selected_rescued_task_ids'] if t in (i,j)]
    result['compensation_rescued_task_ids']=[t for t in result['selected_rescued_task_ids'] if t in (k1,k2)]
    for name in ('window','shared_loaded_distance_km','source_drop_availability_min'):
        if name in move:_bind(move[name],result[name],'move '+name)
    if telemetry is not None:_snapshot(telemetry,proposal_routes,move,result,weights,scales,tasks,topology)
    return result
