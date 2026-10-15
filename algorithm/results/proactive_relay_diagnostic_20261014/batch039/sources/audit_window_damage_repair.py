"""Independent stdlib audit after deadline-window damage-closure repair.

The original DIRECT-to-four-task macro is proved by the frozen029 auditor.
Only the two pooled complete plans/subsequences remain pinned afterwards;
all other whole DIRECT requests may move. No search/closure code is imported.
"""
from __future__ import annotations
from math import hypot,isclose,isfinite
try:
    from .audit_deadline_window_repooling import audit_deadline_window_repooling_proposal,_bind
    from .audit_pinned_repooling import _plans
    from .audit_repooling import _identity,_owners,_direct_task,_shared_flight
    from .audit_mobile_relay import _point
    from .audit_weighted_relay_ablation import _geometry,reference_schedule
    from .audit_weighted_direct_pair import require,same
except ImportError:
    from audit_deadline_window_repooling import audit_deadline_window_repooling_proposal,_bind
    from audit_pinned_repooling import _plans
    from audit_repooling import _identity,_owners,_direct_task,_shared_flight
    from audit_mobile_relay import _point
    from audit_weighted_relay_ablation import _geometry,reference_schedule
    from audit_weighted_direct_pair import require,same

EPSILON=1e-9
SHARED_EPSILON_KM=1e-10
PIN_SCOPE='window_damage_repair'


def _physical(score):
    return dict(late_count=score['late_count'],total_lateness_min=float(score['total_lateness_min']),distance_km=float(score['distance_km']))


def _classifications(tasks,before,after,family):
    old={t for t in tasks if before[str(t)]-tasks[t][2]>EPSILON}
    new={t for t in tasks if after[str(t)]-tasks[t][2]>EPSILON}
    groups=dict(pooled={family['i'],family['j']},compensation={family['k1'],family['k2']})
    groups['native']=set(tasks)-groups['pooled']-groups['compensation']
    result={}
    for name,ids in groups.items():
        result[name+'_rescued_task_ids']=sorted((old-new)&ids)
        result[name+'_newly_late_task_ids']=sorted((new-old)&ids)
    return result


def _current_handoff(tasks,topology,routes,reference,family):
    homes,stations,registry,count,capacity,maximum,speed=_geometry(tasks,topology)
    i,j,a,b,c,H=(family[n] for n in ('i','j','a','b','c','station_id'))
    hs={h['task_id']:h for h in reference['handoffs']}
    require(set(hs)=={i,j} and len(reference['handoffs'])==2,'Post must retain exactly two pooled relay plans')
    availability={};prefixes=[]
    for task,owner in ((i,a),(j,b)):
        h=hs[task]
        require(h['cross_uav'] and h['inbound_drone_id']==owner+1 and h['outbound_drone_id']==c+1
            and h['relay_id']==H,'Current pinned handoff source/receiver/station differs')
        route=routes[owner];drop=route.index(-h['in_leg']);point=homes[owner];clock=0.
        for position,v in enumerate(route[:drop+1]):
            require(registry.get(abs(v),{}).get('kind')!='RELAY_OUT','Source current prefix contains an OUT dependency')
            target=_point(tasks,stations,registry,v);clock+=hypot(target[0]-point[0],target[1]-point[1])/speed;point=target
            same(clock,reference['event_times_min'][owner][position],'current full source prefix clock')
        same(clock,h['drop_time_min'],'current actual source availability')
        availability[str(task)]=clock
        prefixes.append(dict(aircraft=owner,task_id=task,pickup_position=route.index(h['in_leg']),drop_position=drop,drop_min=clock))
    identifiers=[hs[t]['out_leg'] for t in (i,j)];route=routes[c]
    take_positions=sorted(route.index(v) for v in identifiers)
    require(take_positions[1]==take_positions[0]+1,'Current receiver OUT takes must be adjacent')
    shared,segments,loads=_shared_flight(tasks,homes,stations,registry,route,c,identifiers,reference['event_times_min'][c])
    require([loads[p] for p in take_positions]==[0,1],'Current receiver OUT takes must start empty with load frontiers0/1')
    require(shared>SHARED_EPSILON_KM,'Current pooled parcels need positive joint loaded distance')
    first,second=take_positions;previous=_point(tasks,stations,registry,route[first-1]) if first else homes[c]
    arrival=(reference['event_times_min'][c][first-1] if first else 0.)+hypot(
        stations[H][0]-previous[0],stations[H][1]-previous[1])/speed
    order=[registry[route[p]]['task_id'] for p in take_positions]
    t1=max(arrival,availability[str(order[0])]);T=max(arrival,*availability.values())
    times=[reference['event_times_min'][c][p] for p in take_positions]
    _bind(times,[t1,T],'current actual OUT take times')
    point=stations[H];elapsed=0.;offsets={}
    for p in range(second+1,len(route)):
        v=route[p];target=_point(tasks,stations,registry,v);elapsed+=hypot(target[0]-point[0],target[1]-point[1])/speed;point=target
        same(reference['event_times_min'][c][p],T+elapsed,'current actual receiver suffix clock')
        if v<0 and (abs(v) not in registry or registry[abs(v)]['kind']=='RELAY_OUT'):
            task=_identity(v,registry);offsets[str(task)]=elapsed
            same(reference['delivery_times_min'][str(task)],T+elapsed,'current actual suffix delivery')
    require({i,j}<={int(t) for t in offsets},'Current suffix lost a pinned pooled delivery')
    caches={str(t):max(0.,hs[t]['pickup_time_min']-availability[str(t)]) for t in sorted((i,j))}
    current=dict(station_id=H,task_ids=sorted((i,j)),source_aircraft=[{i:a,j:b}[t] for t in sorted((i,j))],receiver_aircraft=c,
        source_availability_min=availability,receiver_arrival_min=arrival,take_order_task_ids=order,take_times_min=times,
        second_take_min=T,receiver_waiting_min=T-arrival,ground_cache_by_task_min=caches,
        suffix_delivery_offsets_min=offsets,shared_loaded_distance_km=float(shared))
    return availability,float(shared),current,prefixes,segments,dict(aircraft=c,take_positions=take_positions,
        load_before_takes=[loads[p] for p in take_positions])


def _telemetry(saved,routes,move,result,tasks,topology,weights,scales):
    require(isinstance(saved,dict) and saved.get('complete') is True and saved.get('valid') is True,'Post snapshot must be complete/valid')
    require(saved.get('routes')==[list(r) for r in routes] or saved.get('routes')==tuple(tuple(r) for r in routes),'Current post routes differ')
    require(saved.get('original_macro_move')==move,'Original deadline-window macro provenance differs')
    require(saved.get('pin_scope')==PIN_SCOPE,'Damage repair pin scope differs')
    for name in ('pinned_task_ids','released_compensation_task_ids','actual_final_owners','event_times_min','delivery_times_min',
        'post_global_score','current_source_drop_availability_min','current_shared_loaded_distance_km','current_handoff','current_classifications'):
        require(name in saved,'Missing current post field: '+name);_bind(saved[name],result[name],'damage post '+name)
    for old in ('table_score','window','source_drop_availability_min','shared_loaded_distance_km'):
        require(old not in saved,'Raw table/window/physics cannot certify current damage repair: '+old)
    if 'score' in saved:_bind(saved['score'],result['score'],'damage post full score')
    if saved.get('move') is not None:
        require(saved['move']=={'semantic':'post_window_damage_repair'},'Post move must contain only current damage-repair semantics')
    for name,value in (('cross_uav_count',result['actual_cross_uav_count']),('relay_task_count',len(result['handoffs']))):
        require(type(saved.get(name)) is int and saved[name]==value,'Current post '+name+' differs')
    if 'topology' in saved:require(_geometry(tasks,saved['topology'])==_geometry(tasks,topology),'Current post topology/resources differ')
    if weights is not None or scales is not None:
        require(isinstance(weights,(list,tuple)) and isinstance(scales,(list,tuple)) and len(weights)==len(scales)==3
            and all(type(v) in (int,float) and isfinite(v) and v>0 for v in (*weights,*scales)) and isclose(sum(weights),1.),'Invalid damage-repair weighted objective')
        values=[result['score'][f] for f in ('late_count','total_lateness_min','distance_km')]
        _bind(saved.get('key'),[sum(w*v/s for w,v,s in zip(weights,values,scales)),*values],'damage post weighted key')


def audit_window_damage_repair_proposal(tasks,topology,base_direct_routes,macro_routes,post_routes,original_macro_move,*,
                                       relay_enabled,telemetry=None,weights=None,scales=None):
    """Prove real029 raw provenance, then current pin2/free-DIRECT physics."""
    raw=audit_deadline_window_repooling_proposal(tasks,topology,base_direct_routes,macro_routes,original_macro_move,relay_enabled=relay_enabled)
    homes,stations,registry,count,capacity,maximum,speed=_geometry(tasks,topology)
    current=reference_schedule(tasks,post_routes,topology);family=original_macro_move['family']
    i,j,k1,k2=(family[n] for n in ('i','j','k1','k2'));pinned={i,j};ordinary=set(tasks)-pinned
    require(_plans(macro_routes,registry,pinned)==_plans(post_routes,registry,pinned),'Post changed a pinned whole mode/leg/H/source/final-owner plan')
    subsequences=lambda rs:tuple(tuple(v for v in route if _identity(v,registry) in pinned) for route in rs)
    require(subsequences(macro_routes)==subsequences(post_routes),'Post changed a selected signed-event subsequence')
    counts=[r['task_count'] for r in current['route_records']]
    require(counts==raw['final_tasks_per_aircraft'],'Post changed exact aircraft final quotas')
    require({h['task_id'] for h in current['handoffs']}=={h['task_id'] for h in raw['handoffs']},'Post added or removed relay plans')
    require(all(_direct_task(post_routes,registry,t) for t in ordinary),'Every unpinned request including compensation must remain whole DIRECT')
    availability={};shared=0.;handoff=None;prefixes=[];segments=[];receiver=None
    if relay_enabled:
        availability,shared,handoff,prefixes,segments,receiver=_current_handoff(tasks,topology,post_routes,current,family)
    else:
        require(not current['handoffs'],'DIRECT damage repair cannot create relay plans')
        owners=_owners(post_routes,registry)
        if owners[i]==owners[j]:
            owner=owners[i];shared,segments,_=_shared_flight(tasks,homes,stations,registry,post_routes[owner],owner,(i,j),current['event_times_min'][owner]);shared=float(shared)
    rawowners,owners=_owners(macro_routes,registry),_owners(post_routes,registry)
    changed=sorted(t for t in ordinary if rawowners[t]!=owners[t]);compchanged=sorted(t for t in (k1,k2) if rawowners[t]!=owners[t])
    seq=lambda rs:tuple(tuple(v for v in route if _identity(v,registry) in ordinary) for route in rs)
    oldseq,newseq=seq(macro_routes),seq(post_routes)
    base=reference_schedule(tasks,base_direct_routes,topology)
    score=_physical(current['score'])
    result=dict(passed=True,pin_scope=PIN_SCOPE,mode=original_macro_move['mode'],macro_audit=raw,macro_score=raw['score'],score=score,
        post_global_score=score,pinned_task_ids=sorted(pinned),released_compensation_task_ids=sorted((k1,k2)),
        final_tasks_per_aircraft=counts,actual_final_owners={str(t):a for t,a in sorted(owners.items())},
        pinned_event_subsequences=[list(r) for r in subsequences(post_routes)],pinned_final_owners={str(t):owners[t] for t in sorted(pinned)},
        compensation_actual_final_owners={str(t):owners[t] for t in sorted((k1,k2))},compensation_owner_changed_task_ids=compchanged,
        unpinned_owner_changed_task_ids=changed,unpinned_owner_changed_count=len(changed),
        unpinned_event_sequence_changed_aircraft=[a for a in range(count) if oldseq[a]!=newseq[a]],
        current_source_drop_availability_min=availability,current_shared_loaded_distance_km=shared,current_handoff=handoff,
        current_source_prefix_records=prefixes,current_outbound_record=receiver,current_shared_loaded_segments=segments,
        current_classifications=_classifications(tasks,base['delivery_times_min'],current['delivery_times_min'],family),
        actual_cross_uav_count=current['relay_statistics']['cross_uav_handoff_count'],delivery_times_min=current['delivery_times_min'],
        event_times_min=current['event_times_min'],handoffs=current['handoffs'],core_reference=current)
    if telemetry is not None:_telemetry(telemetry,post_routes,original_macro_move,result,tasks,topology,weights,scales)
    return result


def audit_window_damage_closure(tasks,topology,macro_routes,closure_record,*,pinned_task_ids):
    """Verify actual whole-DIRECT removals/slots; no partial is a full solution."""
    require(isinstance(closure_record,dict),'Missing actual damage closure record')
    homes,stations,registry,count,capacity,maximum,speed=_geometry(tasks,topology)
    raw=reference_schedule(tasks,macro_routes,topology);owners=_owners(macro_routes,registry)
    require(isinstance(pinned_task_ids,(list,tuple)) and len(pinned_task_ids)==2
        and all(type(t) is int and t in tasks for t in pinned_task_ids) and len(set(pinned_task_ids))==2,'Two distinct actual pinned tasks required')
    pinned=set(pinned_task_ids);eligible={t for t in tasks if t not in pinned and _direct_task(macro_routes,registry,t)}
    removed=closure_record.get('removed_task_ids')
    require(isinstance(removed,list) and all(type(t) is int and t in eligible for t in removed)
        and len(removed)==len(set(removed)),'Closure must remove distinct whole DIRECT nonpin tasks')
    _bind(closure_record.get('removed_task_ids_sorted'),sorted(removed),'closure sorted removals')
    _bind(closure_record.get('pinned_task_ids'),sorted(pinned),'closure actual pins')
    selected=set(removed);partial=tuple(tuple(v for v in route if _identity(v,registry) not in selected) for route in macro_routes)
    _bind(closure_record.get('partial_routes'),partial,'closure actual whole-plan partial routes')
    q=[sum(owners[t]==a for t in removed) for a in range(count)]
    pin_counts=[sum(owners[t]==a for t in pinned) for a in range(count)]
    remaining=[sum(owners[t]==a for t in eligible-selected) for a in range(count)]
    rawcounts=[r['task_count'] for r in raw['route_records']]
    require([p+n+s for p,n,s in zip(pin_counts,remaining,q)]==rawcounts,'Actual closure quota identity fails')
    for name,expected in (('raw_final_counts',rawcounts),('pin_final_counts',pin_counts),('remaining_native_final_counts',remaining),
        ('quota_slots_by_aircraft',q),('eligible_native_count',len(eligible))):_bind(closure_record.get(name),expected,'closure '+name)
    if 'quota_slots' in closure_record:_bind(closure_record['quota_slots'],q,'closure quota alias')
    opened=[a for a,v in enumerate(q) if v>0];unopened=[a for a in range(count) if a not in opened]
    _bind(closure_record.get('actual_opened_aircraft'),opened,'closure actually opened aircraft')
    _bind(closure_record.get('all_aircraft_opened'),not unopened,'closure all-aircraft claim')
    reasons=closure_record.get('unopened_aircraft_reasons')
    require(isinstance(reasons,dict) and set(reasons)=={str(a) for a in unopened}
        and all(isinstance(v,str) and v for v in reasons.values()),'Closure unopened-aircraft reasons do not match actual slots')
    limit=closure_record.get('closure_limit')
    require(type(limit) is int and limit>=len(removed),'Closure exceeds its actual declared cap')
    omitted=sorted(eligible-selected);_bind(closure_record.get('omitted_task_ids'),omitted,'closure omitted eligible tasks')
    truncation=closure_record.get('truncation');require(isinstance(truncation,dict),'Missing closure truncation')
    _bind(truncation.get('eligible_omitted_count'),len(omitted),'closure eligible omitted count')
    effective=min(limit,len(eligible))
    eligible_owners={owners[t] for t in eligible}
    require(len(opened)==min(effective,len(eligible_owners)),'Donor-first closure did not open the actual number of available aircraft slots before expansion')
    if 'effective_closure_limit' in closure_record:_bind(closure_record['effective_closure_limit'],effective,'closure effective cap')
    require(type(truncation.get('limit_reached')) is bool and truncation['limit_reached']==(len(removed)>=effective),
        'Closure cap-reached claim differs')
    require(type(truncation.get('expansion_truncated')) is bool,'Closure expansion truncation must be explicit')
    if 'eligible_not_removed_task_ids' in closure_record:_bind(closure_record['eligible_not_removed_task_ids'],omitted,'closure eligible-not-removed alias')
    evidence=closure_record.get('evidence',{})
    require(isinstance(evidence,dict),'Invalid closure evidence')
    donors=evidence.get('donor_reservations',[]);require(isinstance(donors,list),'Invalid donor reservations')
    donor_aircraft=set()
    for donor in donors:
        require(isinstance(donor,dict),'Invalid donor record')
        a,t=donor.get('aircraft'),donor.get('task_id')
        require(type(a) is int and 0<=a<count and type(t) is int and t in selected and owners[t]==a,
            'Donor must actually release an unpinned whole DIRECT task from its declared aircraft')
        require(a not in donor_aircraft,'Duplicate aircraft donor reservation');donor_aircraft.add(a)
        ranking=donor.get('ranked_task_ids');index=donor.get('rank_index');head=donor.get('rotation_head_size')
        require(isinstance(ranking,list) and len(ranking)==len(set(ranking)) and all(type(k) is int and k in eligible and owners[k]==a for k in ranking),
            'Donor ranking contains another owner or a pinned/non-DIRECT task')
        require(type(index) is int and 0<=index<len(ranking) and ranking[index]==t,'Donor rank index does not select the real removed donor')
        require(type(head) is int and 1<=head<=len(ranking) and index<head,'Donor rotation head does not contain its selected task')
    if 'category_rankings' in evidence:
        categories=evidence['category_rankings'];require(isinstance(categories,dict),'Invalid closure category rankings')
        require(all(isinstance(v,list) and all(type(t) is int and t in eligible for t in v) for v in categories.values()),'Closure category contains a noneligible task')
        pool=set().union(*(set(v) for v in categories.values()),{d['task_id'] for d in donors})
        expansion=sorted(pool-selected)
        _bind(closure_record.get('expansion_omitted_task_ids'),expansion,'closure omitted expansion candidates')
        _bind(truncation.get('expansion_omitted_count'),len(expansion),'closure expansion omitted count')
        _bind(truncation['expansion_truncated'],bool(expansion) and len(removed)>=effective,'closure actual expansion truncation')
    if 'removed_owner_by_task' in closure_record:
        _bind(closure_record['removed_owner_by_task'],{str(t):owners[t] for t in sorted(removed)},'closure real removed owners')
    return dict(passed=True,complete_solution=False,pinned_task_ids=sorted(pinned),removed_task_ids=removed,
        quota_slots_by_aircraft=q,actual_opened_aircraft=opened,all_aircraft_opened=not unopened,
        raw_final_counts=rawcounts,pin_final_counts=pin_counts,remaining_native_final_counts=remaining,
        partial_routes=[list(r) for r in partial],eligible_native_count=len(eligible),omitted_task_ids=omitted,
        bound_scope='actual removals and quota slots only; no selection-optimality or partial full-score claim')


def audit_window_damage_partial(tasks,topology,routes,present_task_ids,*,telemetry=None,weights=None,scales=None,
                                target_final_counts=None):
    """Rebuild an explicit task-subset DAG; this never certifies a full plan."""
    _geometry(tasks,topology)
    require(isinstance(present_task_ids,(list,tuple)) and bool(present_task_ids)
        and all(type(t) is int and t in tasks for t in present_task_ids)
        and len(set(present_task_ids))==len(present_task_ids),'Partial present IDs must be distinct known whole tasks')
    present=set(present_task_ids);subset={t:tasks[t] for t in sorted(present)}
    partial_topology={**topology,'legs':[dict(v) for v in topology['legs'] if v['task_id'] in present]}
    for name in ('task_relay_candidates','candidate_stations_by_task'):
        if isinstance(topology.get(name),dict):
            partial_topology[name]={t:v for t,v in topology[name].items() if int(t) in present}
    reference=reference_schedule(subset,routes,partial_topology)
    score=_physical(reference['score']);counts=[r['task_count'] for r in reference['route_records']]
    remaining=None
    if target_final_counts is not None:
        require(isinstance(target_final_counts,(list,tuple)) and len(target_final_counts)==topology['drone_count']
            and all(type(v) is int and 0<=v<=topology['max_tasks_per_drone'] for v in target_final_counts)
            and sum(target_final_counts)==len(tasks),'Partial target counts must be the genuine complete fleet quota vector')
        remaining=[target-present for target,present in zip(target_final_counts,counts)]
        require(all(v>=0 for v in remaining) and sum(remaining)==len(tasks)-len(present),'Partial exceeds actual remaining quota slots')
    result=dict(passed=True,complete_solution=False,scoring_scope='partial_same_coverage_only',
        original_task_count=len(tasks),present_task_ids=sorted(present),score=score,
        actual_partial_final_counts=counts,remaining_quota_slots=remaining,event_times_min=reference['event_times_min'],
        delivery_times_min=reference['delivery_times_min'],core_reference=reference,
        bound_scope='actual task-subset schedule only; no complete objective, assignment-sum or global-improvement bound')
    if telemetry is not None:
        require(isinstance(telemetry,dict) and telemetry.get('complete') is not True
            and telemetry.get('scoring_scope','partial_same_coverage_only')=='partial_same_coverage_only',
            'Partial beam evidence cannot claim complete-solution scoring')
        require(telemetry.get('routes')==[list(r) for r in routes] or telemetry.get('routes')==tuple(tuple(r) for r in routes),
            'Partial state routes differ')
        for name in ('score','event_times_min','delivery_times_min','present_task_ids'):
            if name in telemetry:_bind(telemetry[name],result[name],'actual partial '+name)
        if 'remaining_quota_slots' in telemetry:
            require(target_final_counts is not None,'Actual complete target counts required to prove partial slot remainder')
            _bind(telemetry['remaining_quota_slots'],remaining,'actual partial remaining slots')
        if weights is not None or scales is not None:
            require(isinstance(weights,(tuple,list)) and isinstance(scales,(tuple,list)) and len(weights)==len(scales)==3
                and all(type(v) in (int,float) and isfinite(v) and v>0 for v in (*weights,*scales)) and isclose(sum(weights),1.),
                'Invalid actual partial weighted objective')
            values=[score[f] for f in ('late_count','total_lateness_min','distance_km')]
            _bind(telemetry.get('key'),[sum(w*v/s for w,v,s in zip(weights,values,scales)),*values],'actual partial weighted key')
    return result
