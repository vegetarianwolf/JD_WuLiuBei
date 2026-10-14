"""Independent fixed-site split-feeder provenance and physical audit.

The two parcels really belong to the same original receiver. No synthetic
source layout, cost-table clock, search module or production model is used.
"""
from __future__ import annotations

from math import hypot,isclose,isfinite

try:
    from .audit_deadline_window_repooling import _bind,_preview_scan,_window
    from .audit_repooling import _identity,_owners,_direct_task,_shared_flight
    from .audit_weighted_relay_ablation import _geometry,reference_schedule
    from .audit_weighted_direct_pair import require
    from .audit_window_damage_repair import _current_handoff
except ImportError:
    from audit_deadline_window_repooling import _bind,_preview_scan,_window
    from audit_repooling import _identity,_owners,_direct_task,_shared_flight
    from audit_weighted_relay_ablation import _geometry,reference_schedule
    from audit_weighted_direct_pair import require
    from audit_window_damage_repair import _current_handoff

EPSILON=1e-9
MODES=('RELAY','DIRECT_SAME_OWNER','DIRECT_DONOR_12MAP')


def _physical(score):
    return dict(late_count=score['late_count'],total_lateness_min=float(score['total_lateness_min']),
                distance_km=float(score['distance_km']))


def _family_base(tasks,topology,base_routes,family):
    require(isinstance(family,dict),'Missing split-feeder family')
    homes,stations,registry,count,capacity,maximum,speed=_geometry(tasks,topology)
    require(capacity==2,'Split-feeder experiment requires actual capacity2')
    for name in ('i','j','k1','k2'):
        require(type(family.get(name)) is int and family[name] in tasks,'Invalid family task '+name)
    for name in ('a','b','c'):
        require(type(family.get(name)) is int and 0<=family[name]<count,'Invalid family aircraft '+name)
    i,j,k1,k2=(family[n] for n in ('i','j','k1','k2'))
    a,b,c=(family[n] for n in ('a','b','c'));H=family.get('station_id')
    require(i<j and len({i,j,k1,k2})==4 and len({a,b,c})==3,
            'Family needs ordered distinct pooled pair, two donors and three distinct aircraft')
    _bind(family.get('task_ids'),sorted((i,j,k1,k2)),'split-feeder family task IDs')
    if 'pooled_task_ids' in family:_bind(family['pooled_task_ids'],[i,j],'split-feeder pooled pair')
    require(type(H) is int and H in stations,'Family station must be an original fixed site')
    for task in (i,j):
        require({leg['kind'] for leg in registry.values() if leg['task_id']==task and leg['relay_id']==H}
                =={'RELAY_IN','RELAY_OUT'},'Family station lacks original eligible paired legs')
    base=reference_schedule(tasks,base_routes,topology)
    require(not base['handoffs'] and all(_direct_task(base_routes,registry,t) for t in tasks),
            'Split-feeder source must be the real pure-DIRECT complete input')
    original=_owners(base_routes,registry)
    require([original[t] for t in (i,j,k1,k2)]==[c,c,a,b],
            'True base ownership must be i/j:C and actual donors k1:A/k2:B')
    return base,original,(homes,stations,registry,count,capacity,maximum,speed)


def _classes(tasks,base,current,family):
    old={t for t in tasks if base[str(t)]-tasks[t][2]>EPSILON}
    new={t for t in tasks if current[str(t)]-tasks[t][2]>EPSILON}
    groups=dict(pooled={family['i'],family['j']},donor={family['k1'],family['k2']})
    groups['native']=set(tasks)-groups['pooled']-groups['donor']
    result={}
    for name,ids in groups.items():
        result[name+'_rescued_task_ids']=sorted((old-new)&ids)
        result[name+'_newly_late_task_ids']=sorted((new-old)&ids)
    return result


def _deadline_telemetry(saved):
    fields=('full_verified_at_perf_counter','accepted_at_perf_counter','deadline_perf_counter')
    if not any(f in saved for f in fields):return
    require(all(f in saved for f in (fields[0],fields[2])),'Incomplete declared full-check deadline evidence')
    require(all(type(saved[f]) in (int,float) and isfinite(saved[f]) for f in fields if f in saved),
            'Nonfinite declared deadline evidence')
    verified,deadline=saved[fields[0]],saved[fields[2]]
    require(verified<deadline,'Split-feeder full check was not strictly before declared deadline')
    if fields[1] in saved:
        require(verified<=saved[fields[1]]<deadline,'Split-feeder adoption was not checked before its deadline')


def _telemetry(saved,tasks,topology,routes,move,result,weights,scales):
    require(isinstance(saved,dict) and saved.get('valid') is True and saved.get('complete') is True,
            'Split-feeder snapshot must be complete and valid')
    _bind(saved.get('routes'),routes,'split-feeder full routes')
    _bind(saved.get('move'),move,'split-feeder exact move provenance')
    for name in ('window','source_preview_routes','optimistic_missing_comp'):
        require(name not in saved,'Ranking/window preview cannot certify a current full snapshot: '+name)
    for name in ('score','table_score','event_times_min','delivery_times_min','actual_final_owners',
                 'source_drop_availability_min','shared_loaded_distance_km','handoff',*_classes_names()):
        require(name in saved,'Missing split-feeder current physical field '+name)
        _bind(saved[name],result['score'] if name=='table_score' else result[name],'split-feeder '+name)
    for name,expected in (('cross_uav_count',result['actual_cross_uav_count']),('relay_task_count',len(result['handoffs']))):
        _bind(saved.get(name),expected,'split-feeder '+name)
    if 'topology' in saved:
        require(_geometry(tasks,saved['topology'])==_geometry(tasks,topology),'Snapshot changed frozen topology/resources')
    if weights is not None or scales is not None:
        require(isinstance(weights,(list,tuple)) and isinstance(scales,(list,tuple)) and len(weights)==len(scales)==3
                and all(type(v) in (int,float) and isfinite(v) and v>0 for v in (*weights,*scales))
                and isclose(sum(weights),1.),'Invalid split-feeder weighted objective')
        values=[result['score'][f] for f in ('late_count','total_lateness_min','distance_km')]
        _bind(saved.get('key'),[sum(w*v/s for w,v,s in zip(weights,values,scales)),*values],'split-feeder key')
    _deadline_telemetry(saved)


def _classes_names():
    return tuple(group+'_'+kind+'_task_ids' for group in ('pooled','donor','native')
                 for kind in ('rescued','newly_late'))


def audit_split_feeder_proposal(tasks,topology,base_routes,proposal_routes,move,*,relay_enabled,
                                telemetry=None,weights=None,scales=None):
    """Prove a genuine fixed-site assist or either independent DIRECT control."""
    require(type(relay_enabled) is bool and isinstance(move,dict),'Missing split-feeder mode/descriptor')
    family=move.get('family');base,original,geometry=_family_base(tasks,topology,base_routes,family)
    homes,stations,registry,count,capacity,maximum,speed=geometry
    mode=move.get('mode')
    require(mode in MODES and (mode=='RELAY')==relay_enabled,'Split-feeder arm and mode differ')
    current=reference_schedule(tasks,proposal_routes,topology);owners=_owners(proposal_routes,registry)
    i,j,a,b,c,H=(family[n] for n in ('i','j','a','b','c','station_id'))
    selected={i,j,family['k1'],family['k2']} if mode=='DIRECT_DONOR_12MAP' else {i,j}
    _bind(move.get('selected_task_ids'),sorted(selected),'split-feeder actual selected set')
    _bind(move.get('final_aircraft_by_task'),{str(t):owners[t] for t in sorted(selected)},'split-feeder selected final map')
    _bind(move.get('actual_final_owners'),{str(t):owners[t] for t in sorted(tasks)},'split-feeder all final owners')
    counts=[r['task_count'] for r in current['route_records']]
    require(counts==[r['task_count'] for r in base['route_records']],'Split-feeder changed exact final task quotas')
    native=set(tasks)-selected
    subseq=lambda rs:tuple(tuple(v for v in r if _identity(v,registry) in native) for r in rs)
    require(subseq(base_routes)==subseq(proposal_routes),'Split-feeder moved or reordered an unrelated signed event')
    require(all(_direct_task(proposal_routes,registry,t) for t in native),'Unselected tasks must remain whole DIRECT')
    availability={};shared=0.;handoff=None;prefixes=[];segments=[];outbound=None
    if relay_enabled:
        require(owners==original,'Split-feeder relay must keep every original final owner; no compensation')
        availability,shared,handoff,prefixes,segments,outbound=_current_handoff(
            tasks,topology,proposal_routes,current,family)
    else:
        require(not current['handoffs'] and all(_direct_task(proposal_routes,registry,t) for t in tasks),
                'DIRECT control cannot contain any relay transport')
        if mode=='DIRECT_SAME_OWNER':
            require(owners==original,'Same-owner DIRECT control changed original final map')
        else:
            require({owners[t] for t in selected}<={a,b,c}
                    and [sum(owners[t]==owner for t in selected) for owner in (a,b,c)]==[1,1,2],
                    'Independent donor DIRECT control must use an A1/B1/C2 four-task map')
        if owners[i]==owners[j]:
            owner=owners[i]
            shared,segments,_=_shared_flight(tasks,homes,stations,registry,proposal_routes[owner],owner,(i,j),
                                            current['event_times_min'][owner]);shared=float(shared)
    score=_physical(current['score'])
    result=dict(passed=True,mode=mode,score=score,base_score=_physical(base['score']),
        selected_task_ids=sorted(selected),original_final_owners={str(t):a for t,a in sorted(original.items())},
        actual_final_owners={str(t):a for t,a in sorted(owners.items())},final_tasks_per_aircraft=counts,
        unselected_task_count=len(native),unselected_signed_event_subsequences=[list(r) for r in subseq(proposal_routes)],
        source_drop_availability_min=availability,shared_loaded_distance_km=float(shared),handoff=handoff,
        source_prefix_records=prefixes,shared_loaded_segments=segments,outbound_record=outbound,
        actual_cross_uav_count=current['relay_statistics']['cross_uav_handoff_count'],
        handoffs=current['handoffs'],event_times_min=current['event_times_min'],delivery_times_min=current['delivery_times_min'],
        core_reference=current,**_classes(tasks,base['delivery_times_min'],current['delivery_times_min'],family))
    for name in ('source_drop_availability_min','shared_loaded_distance_km','handoff'):
        if name in move:_bind(move[name],result[name],'split-feeder move '+name)
    if telemetry is not None:_telemetry(telemetry,tasks,topology,proposal_routes,move,result,weights,scales)
    return result


def _weighted(saved,score,weights,scales,label):
    if weights is None and scales is None:return
    require(isinstance(weights,(list,tuple)) and isinstance(scales,(list,tuple)) and len(weights)==len(scales)==3
        and all(type(v) in (int,float) and isfinite(v) and v>0 for v in (*weights,*scales))
        and isclose(sum(weights),1.),'Invalid catalogue weighted objective')
    values=[score[f] for f in ('late_count','total_lateness_min','distance_km')]
    _bind(saved,[sum(w*v/s for w,v,s in zip(weights,values,scales)),*values],label)


def _source_table(tasks,base_routes,record,*,task,owner,station,geometry,weights,scales):
    homes,stations,registry,count,capacity,maximum,speed=geometry
    require(isinstance(record,dict),'Invalid complete source-route record')
    for name,expected in (('task_id',task),('aircraft',owner),('station_id',station),
        ('native_complete',True),('optimistic_missing_comp',False),('compensation_task_id',None)):
        _bind(record.get(name),expected,'split-feeder source '+name)
    route=record.get('route');require(isinstance(route,(list,tuple)),'Missing actual source route')
    inbound=next(identifier for identifier,leg in registry.items()
        if leg['task_id']==task and leg['relay_id']==station and leg['kind']=='RELAY_IN')
    require(route.count(inbound)==route.count(-inbound)==1,'Source needs exactly one original eligible IN plan')
    require(tuple(v for v in route if abs(v)!=inbound)==tuple(base_routes[owner]),
            'Source table changed, omitted or reordered an original native event')
    times,deliveries,loads=_preview_scan(tasks,stations,registry,route,homes[owner],capacity,speed,{})
    pickup,drop=route.index(inbound),route.index(-inbound)
    availability={str(task):times[drop]}
    previous=homes[owner];distance=0.
    for visit in route:
        # _preview_scan already validates original physical points/load/legs.
        from_point=previous
        if abs(visit)==inbound:previous=tasks[task][0] if visit>0 else stations[station]
        else:previous=tasks[abs(visit)][0 if visit>0 else 1]
        distance+=hypot(previous[0]-from_point[0],previous[1]-from_point[1])
    late=[clock-tasks[int(t)][2] for t,clock in deliveries.items() if clock-tasks[int(t)][2]>EPSILON]
    score=dict(late_count=len(late),total_lateness_min=float(sum(late)),distance_km=float(distance))
    expected=dict(score=score,event_times_min=times,delivery_times_min=deliveries,
        source_drop_availability_min=availability,in_drop_time_min=times[drop],in_drop_position=drop,pickup_position=pickup,
        load_before=[0,*loads[:-1]],load_after=loads,final_delivery_count=len(deliveries))
    for name,value in expected.items():_bind(record.get(name),value,'split-feeder source '+name)
    require(len(deliveries)<=maximum,'Source IN table exceeded actual final-delivery K')
    _weighted(record.get('key'),score,weights,scales,'split-feeder source key')
    require(record.get('coverage_scope')=='finite_position_retention_only_not_global_feasibility_or_J_bound',
            'Source position retention must not claim global feasibility/completeness or objective bound')
    require(isinstance(record.get('latest_drop_bounds'),(list,tuple)) and
        all(type(v) in (int,float) and isfinite(v) for v in record['latest_drop_bounds']),
        'Invalid declared finite source drop channels')
    coverage=record.get('source_bound_position_coverage')
    require(isinstance(coverage,(list,tuple)) and len(coverage)==len(record['latest_drop_bounds']),
            'Missing source position omission declarations for its finite channels')
    for bound,row in zip(record['latest_drop_bounds'],coverage):
        require(isinstance(row,dict),'Invalid declared position coverage record')
        _bind(row.get('bound_min'),bound,'declared source position bound')
        require(type(row.get('legal_positions_before_bound')) is int and row['legal_positions_before_bound']>=0,
                'Invalid declared legal-position count')
        require(all(type(row.get(k)) is bool for k in ('best_geometry_retained','latest_safe_retained','omitted_due_position_limit')),
                'Finite source position omissions must be explicit booleans')
    return dict(passed=True,task_id=task,aircraft=owner,station_id=station,route=list(route),**expected,
        coverage_scope=record['coverage_scope'],declared_latest_drop_bounds=list(record['latest_drop_bounds']),
        declared_source_bound_position_coverage=list(coverage),
        physical_scope='actual complete native route plus one whole IN leg; position selection optimality is not proved')


def _donors(tasks,base_routes,base,family,geometry,weights,scales):
    homes,stations,registry,count,capacity,maximum,speed=geometry
    records=family.get('donor_evidence')
    require(isinstance(records,list) and len(records)==2,'Missing the two true whole-DIRECT donor witnesses')
    proofs=[]
    for saved,task,owner in zip(records,(family['k1'],family['k2']),(family['a'],family['b'])):
        require(isinstance(saved,dict),'Invalid donor deletion witness')
        expected_route=tuple(v for v in base_routes[owner] if abs(v)!=task)
        for name,value in (('task_id',task),('aircraft',owner),('deleted_route',expected_route),
            ('candidate_count',base['route_records'][owner]['task_count']),
            ('policy','best_exact_single_whole_DIRECT_deletion_weighted_credit; DIRECT-only donor; unchanged in RELAY')):
            _bind(saved.get(name),value,'split-feeder true donor '+name)
        times,deliveries,loads=_preview_scan(tasks,stations,registry,expected_route,homes[owner],capacity,speed,{})
        previous=homes[owner];distance=0.
        for visit in expected_route:
            point=tasks[abs(visit)][0 if visit>0 else 1]
            distance+=hypot(point[0]-previous[0],point[1]-previous[1]);previous=point
        late=[clock-tasks[int(t)][2] for t,clock in deliveries.items() if clock-tasks[int(t)][2]>EPSILON]
        after=dict(late_count=len(late),total_lateness_min=float(sum(late)),distance_km=float(distance))
        before=_physical(base['route_records'][owner])
        _bind(saved.get('baseline_score'),before,'donor original actual-home route score')
        _bind(saved.get('deletion_score'),after,'donor actual whole-deletion route score')
        delta={f:after[f]-before[f] for f in before}
        _weighted(saved.get('deletion_delta_key'),delta,weights,scales,'donor physical deletion credit')
        proofs.append(dict(passed=True,task_id=task,aircraft=owner,deleted_route=list(expected_route),
            baseline_score=before,deletion_score=after,deletion_delta=delta,
            scope='actual whole-DIRECT deletion/credit; donor unchanged in RELAY; not a relay compensation or global macro gain'))
    return proofs


def audit_split_feeder_family(tasks,topology,base_routes,family,*,weights=None,scales=None):
    """Rebuild every saved complete RELAY ranking witness from the true base.

    The same witness can rank a DIRECT family. It is expressly not a DIRECT
    candidate or a certificate of DIRECT adoption, and source tables are not
    interchangeable with the full fleet score.
    """
    base,original,geometry=_family_base(tasks,topology,base_routes,family)
    homes,stations,registry,count,capacity,maximum,speed=geometry
    i,j,a,b,c,H=(family[n] for n in ('i','j','a','b','c','station_id'))
    donor_refs=_donors(tasks,base_routes,base,family,geometry,weights,scales)
    keys=[f'{i}:{a}:{H}',f'{j}:{b}:{H}']
    _bind(family.get('source_table_keys'),keys,'split-feeder source table key references')
    tables=family.get('source_tables')
    require(isinstance(tables,dict) and set(tables)==set(keys),'Family needs exactly its two referenced source tables')
    source_refs=[]
    for key,task,owner in zip(keys,(i,j),(a,b)):
        require(isinstance(tables[key],list) and bool(tables[key]),'Empty split-feeder source surface')
        for record in tables[key]:
            source_refs.append(_source_table(tasks,base_routes,record,task=task,owner=owner,station=H,
                geometry=geometry,weights=weights,scales=scales))
    witnesses=family.get('relay_witnesses')
    require(isinstance(witnesses,list) and 1<=len(witnesses)<=3,'Family needs one to three complete RELAY model witnesses')
    witness_refs=[]
    relay_topology={**topology,'relay_allowed':True}
    for witness in witnesses:
        require(isinstance(witness,dict),'Missing complete RELAY model witness')
        for name,value in (('complete_RELAY_model',True),('canonical_full_checked',False),
            ('scoring_scope','complete_RELAY_model_common_ranking_not_DIRECT_candidate')):
            _bind(witness.get(name),value,'split-feeder witness '+name)
        routes=witness.get('routes')
        move=dict(family=family,mode='RELAY',selected_task_ids=[i,j],
            final_aircraft_by_task={str(i):c,str(j):c},actual_final_owners={str(t):owner for t,owner in sorted(original.items())})
        proof=audit_split_feeder_proposal(tasks,relay_topology,base_routes,routes,move,relay_enabled=True)
        for name in ('table_score','event_times_min','delivery_times_min','actual_final_owners',
            'source_drop_availability_min','shared_loaded_distance_km','pooled_rescued_task_ids','pooled_newly_late_task_ids'):
            _bind(witness.get(name),proof['score'] if name=='table_score' else proof[name],'split-feeder complete witness '+name)
        _weighted(witness.get('key'),proof['score'],weights,scales,'split-feeder complete witness key')
        for key,task,owner in zip(keys,(i,j),(a,b)):
            require(any(tuple(r['route'])==tuple(routes[owner]) for r in tables[key]),
                    'Witness source route is absent from its declared source table')
        window=witness.get('window');require(isinstance(window,dict),'Missing complete witness true suffix window')
        current_window=_window(tasks,homes,stations,registry,speed,routes,proof['core_reference'],base,
            i=i,j=j,c=c,station=H,availability=proof['source_drop_availability_min'],
            sigma=window.get('sigma_min'),required=window.get('required_on_time_task_ids'))
        _bind(window,current_window,'split-feeder complete witness window')
        witness_refs.append(dict(passed=True,score=proof['score'],actual_final_owners=proof['actual_final_owners'],
            source_drop_availability_min=proof['source_drop_availability_min'],handoff=proof['handoff'],window=current_window,
            event_times_min=proof['event_times_min'],delivery_times_min=proof['delivery_times_min'],
            actual_cross_uav_count=proof['actual_cross_uav_count'],complete_entity_physics_passed=True,
            scoring_scope=witness['scoring_scope']))
    if 'ranking_scope' in family:
        _bind(family['ranking_scope'],'complete_RELAY_model_common_ranking_not_DIRECT_candidate','family ranking scope')
    _bind(family.get('priority'),min((w['key'] for w in witnesses),key=tuple),'family actual complete-model priority')
    if 'pooled_rescue_count' in family:
        _bind(family['pooled_rescue_count'],max(len(w['pooled_rescued_task_ids']) for w in witnesses),'family actual pooled rescue channel')
    return dict(passed=True,scope='true_base_same_owner_pair_sources_and_complete_RELAY_ranking_witnesses',
        complete_solution=False,DIRECT_candidate_certificate=False,family_task_ids=list(family['task_ids']),
        original_final_owners={str(t):a for t,a in sorted(original.items())},base_score=_physical(base['score']),
        source_route_references=source_refs,witness_references=witness_refs,donor_deletion_references=donor_refs,
        weighted_keys_checked=weights is not None and scales is not None)
