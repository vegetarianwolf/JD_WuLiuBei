"""Independent early native-price proof for saved split-feeder families.

Only frozen stdlib physical auditors are reused. No catalogue, refinement or
suffix-pricing primitive is imported to calculate expected prices. A saved
two-gap source layout is reconstructed from the genuine original DIRECT input.
"""
from __future__ import annotations

from math import hypot,isfinite

try:
    from . import audit_split_feeder as frozen
    from .audit_deadline_window_repooling import _bind,_preview_scan
    from .audit_weighted_direct_pair import require
except ImportError:
    import audit_split_feeder as frozen
    from audit_deadline_window_repooling import _bind,_preview_scan
    from audit_weighted_direct_pair import require

PRICING_SCOPE='complete_two_gap_IN_and_OUT_block_model_before_base_pool'
EPSILON=1e-9


def _native_score(tasks,ids,deliveries,distance):
    late=[deliveries[str(t)]-tasks[t][2] for t in ids if deliveries[str(t)]-tasks[t][2]>EPSILON]
    return dict(late_count=len(late),total_lateness_min=float(sum(late)),distance_km=float(distance))


def _distance(tasks,route,home):
    previous=home;distance=0.
    for visit in route:
        point=tasks[abs(visit)][0 if visit>0 else 1]
        distance+=hypot(point[0]-previous[0],point[1]-previous[1]);previous=point
    return distance


def _gap(value,route,label):
    require(type(value) is int and 0<=value<=len(route),label+' must be a true original event gap')
    return value


def audit_split_feeder_early_price(tasks,topology,base_routes,family,evidence,*,weights=None,scales=None):
    """Prove one stored early price, not coverage of an omitted candidate pool.

    ``native_score`` N/L count only that aircraft's original native tasks;
    its D is the entire modified route flight, including inserted service.
    C's comparison skeleton is first stripped of i/j and independently timed.
    """
    require(isinstance(evidence,dict),'Missing saved early-pricing evidence')
    _bind(evidence.get('pricing_scope'),PRICING_SCOPE,'early native price scope')
    base,owners,geometry=frozen._family_base(tasks,topology,base_routes,family)
    homes,stations,registry,count,capacity,maximum,speed=geometry
    i,j,a,b,c,H=(family[k] for k in ('i','j','a','b','c','station_id'))
    sources=evidence.get('source_layouts')
    require(isinstance(sources,list) and len(sources)==2,'Early price requires its two exact source layouts')
    receiver=evidence.get('receiver_layout')
    require(isinstance(receiver,dict),'Early price requires its actual receiver layout')
    routes=[list(r) for r in base_routes]
    pair_legs={t:{leg['kind']:identifier for identifier,leg in registry.items()
        if leg['task_id']==t and leg['relay_id']==H} for t in (i,j)}
    source_gaps=[]
    for saved,task,owner in zip(sources,(i,j),(a,b)):
        require(isinstance(saved,dict),'Invalid early source layout')
        _bind(saved.get('task_id'),task,'early source task')
        _bind(saved.get('aircraft'),owner,'early source aircraft')
        original=base_routes[owner]
        g=_gap(saved.get('gap'),original,'early source pickup gap')
        h=_gap(saved.get('drop_gap'),original,'early source drop gap')
        require(g<=h,'Early source drop gap precedes actual pickup gap')
        _bind(saved.get('original_pickup_gap'),g,'early original pickup gap')
        _bind(saved.get('original_drop_gap'),h,'early original drop gap')
        _bind(saved.get('layout_type'),'contiguous' if g==h else 'noncontiguous','early source layout type')
        for field,value in (('pickup_position',g),('in_drop_position',h+1)):
            if field in saved:_bind(saved[field],value,'early source actual inserted '+field)
        original_loads=[0,*base['route_records'][owner]['load_after_event']]
        require(all(original_loads[k]<=1 for k in range(g,h+1)),
                'Early source carry interval includes a native load2 gap')
        incoming=pair_legs[task]['RELAY_IN']
        routes[owner]=list(original[:g])+[incoming]+list(original[g:h])+[-incoming]+list(original[h:])
        source_gaps.append((g,h))
    skeleton=[v for v in base_routes[c] if abs(v) not in (i,j)]
    skeleton_times,skeleton_deliveries,skeleton_loads=_preview_scan(
        tasks,stations,registry,skeleton,homes[c],capacity,speed,{})
    gap=_gap(receiver.get('gap'),skeleton,'early receiver skeleton gap')
    require((skeleton_loads[gap-1] if gap else 0)==0,'Early receiver skeleton gap must actually be empty')
    order=receiver.get('delivery_order_task_ids')
    require(isinstance(order,list) and sorted(order)==[i,j] and all(type(t) is int for t in order),
            'Early receiver delivery order must contain exactly the actual pooled pair')
    block=[pair_legs[i]['RELAY_OUT'],pair_legs[j]['RELAY_OUT'],
           -pair_legs[order[0]]['RELAY_OUT'],-pair_legs[order[1]]['RELAY_OUT']]
    routes[c]=skeleton[:gap]+block+skeleton[gap:]
    move=dict(family=family,mode='RELAY',selected_task_ids=[i,j],
        final_aircraft_by_task={str(i):c,str(j):c},actual_final_owners={str(t):owner for t,owner in sorted(owners.items())})
    # This flag enables only the explicitly labelled RELAY ranking witness.
    # No geometry, registry, task, home, K, capacity or aircraft is replaced.
    proof=frozen.audit_split_feeder_proposal(tasks,{**topology,'relay_allowed':True},base_routes,routes,move,relay_enabled=True)
    reference=proof['core_reference'];handoff=proof['handoff'];source_refs=[]
    for saved,task,owner,(g,h) in zip(sources,(i,j),(a,b),source_gaps):
        current=reference['route_records'][owner];original=base['route_records'][owner]
        native={t for t,v in owners.items() if v==owner};route=base_routes[owner]
        score=_native_score(tasks,native,reference['delivery_times_min'],current['distance_km'])
        native_point=lambda v:tasks[abs(v)][0 if v>0 else 1]
        distance=lambda p,q:hypot(p[0]-q[0],p[1]-q[1])
        before=homes[owner] if g==0 else native_point(route[g-1])
        pickup=tasks[task][0];station=stations[H]
        if g==h:
            # P and H jointly replace one edge. Two independent single-edge
            # detours on this same edge would double count the original edge.
            deltaP=distance(before,pickup)+distance(pickup,station)
            if g<len(route):deltaP+=distance(station,native_point(route[g]))-distance(before,native_point(route[g]))
            deltaH=0.
        else:
            nextP=native_point(route[g])
            deltaP=distance(before,pickup)+distance(pickup,nextP)-distance(before,nextP)
            beforeH=native_point(route[h-1]);deltaH=distance(beforeH,station)
            if h<len(route):deltaH+=distance(station,native_point(route[h]))-distance(beforeH,native_point(route[h]))
        delta=deltaP+deltaH;middle_shift=deltaP/speed;tail_shift=delta/speed
        _bind(current['distance_km']-original['distance_km'],delta,'early two-detour source distance identity')
        clock=0. if g==0 else base['event_times_min'][owner][g-1]
        clock+=distance(before,pickup)/speed;previous=pickup
        for visit in route[g:h]:
            point=native_point(visit);clock+=distance(previous,point)/speed;previous=point
        clock+=distance(previous,station)/speed
        _bind(clock,proof['source_drop_availability_min'][str(task)],'early source actual flight-chain drop')
        expected=dict(in_drop_time_min=proof['source_drop_availability_min'][str(task)],
            delta_distance_km=delta,native_shift_min=tail_shift,native_score=score,
            pickup_detour_distance_km=deltaP,drop_detour_distance_km=deltaH,
            middle_native_shift_min=middle_shift,tail_native_shift_min=tail_shift)
        for name,value in expected.items():_bind(saved.get(name),value,'early complete source '+name)
        require(type(saved.get('float_route_replay_used')) is bool,'Early source float replay path must be explicit')
        if 'timing_error_bound_min' in saved:
            require(type(saved['timing_error_bound_min']) in (int,float)
                    and isfinite(saved['timing_error_bound_min']) and saved['timing_error_bound_min']>=0,
                    'Declared early source clock error allowance must be finite and nonnegative')
        if 'geometry_roundoff_corrected' in saved:
            require(type(saved['geometry_roundoff_corrected']) is bool,'Early source roundoff flag must be explicit boolean')
            require(not saved['geometry_roundoff_corrected'] or saved['float_route_replay_used'],
                    'Corrected source geometry requires complete physical route replay')
        for field in ('native_shift_min','middle_native_shift_min','tail_native_shift_min',
                      'pickup_detour_distance_km','drop_detour_distance_km'):
            require(type(saved.get(field)) in (int,float) and isfinite(saved[field]) and saved[field]>=0,
                    'Early source detours and both delays must be finite and nonnegative')
        zones=dict(prefix=[],middle=[],suffix=[])
        for old_position,visit in enumerate(route):
            zone='prefix' if old_position<g else 'middle' if old_position<h else 'suffix'
            shift=0. if old_position<g else middle_shift if old_position<h else tail_shift
            new_position=old_position+(old_position>=g)+(old_position>=h)
            wanted=base['event_times_min'][owner][old_position]+shift
            _bind(reference['event_times_min'][owner][new_position],wanted,'early actual source three-zone native event clock')
            if visit<0:zones[zone].append(abs(visit))
        for zone,ids in zones.items():
            field=zone+'_native_delivery_task_ids'
            if field in saved:_bind(saved[field],ids,'early actual source '+zone+' final deliveries')
        source_refs.append(dict(task_id=task,aircraft=owner,gap=g,drop_gap=h,
            original_pickup_gap=g,original_drop_gap=h,layout_type='contiguous' if g==h else 'noncontiguous',**expected,
            reported_float_route_replay_used=saved['float_route_replay_used'],
            native_delivery_zones=zones,native_task_ids=sorted(native),
            actual_loads_through_carry_interval=[1+base['route_records'][owner]['load_after_event'][k-1] if k else 1 for k in range(g,h+1)],
            final_delivery_count=current['task_count'],transport_leg_count=current['transport_leg_count'],
            event_times_min=reference['event_times_min'][owner],
            native_delivery_times_min={str(t):reference['delivery_times_min'][str(t)] for t in sorted(native)}))
    native_C={t for t,v in owners.items() if v==c and t not in (i,j)}
    skeleton_D=_distance(tasks,skeleton,homes[c]);current_C=reference['route_records'][c]
    native_C_score=_native_score(tasks,native_C,reference['delivery_times_min'],current_C['distance_km'])
    skeleton_score=_native_score(tasks,native_C,skeleton_deliveries,skeleton_D)
    delta=current_C['distance_km']-skeleton_D;shift=delta/speed+handoff['receiver_waiting_min']
    expected_receiver=dict(receiver_arrival_min=handoff['receiver_arrival_min'],
        second_take_min=handoff['second_take_min'],waiting_min=handoff['receiver_waiting_min'],
        delta_distance_km=delta,native_shift_min=shift,native_score=native_C_score)
    for name,value in expected_receiver.items():_bind(receiver.get(name),value,'early complete receiver '+name)
    require(type(receiver.get('float_route_replay_used')) is bool,'Early receiver float replay path must be explicit')
    if 'geometry_roundoff_corrected' in receiver:
        require(type(receiver['geometry_roundoff_corrected']) is bool,'Early receiver roundoff flag must be explicit boolean')
        require(not receiver['geometry_roundoff_corrected'] or receiver['float_route_replay_used'],
                'Corrected receiver geometry requires complete physical route replay')
    require(type(receiver.get('native_shift_min')) in (int,float) and isfinite(receiver['native_shift_min'])
            and receiver['native_shift_min']>=0,'Early receiver delay must be finite and nonnegative')
    for old_position,visit in enumerate(skeleton):
        new_position=old_position+(4 if old_position>=gap else 0)
        wanted=skeleton_times[old_position]+(shift if old_position>=gap else 0.)
        _bind(reference['event_times_min'][c][new_position],wanted,'early actual receiver native event clock')
    selected={str(t):proof['delivery_times_min'][str(t)] for t in (i,j)}
    for name,value in (('full_score',proof['score']),('selected_delivery_times_min',selected),
        ('pooled_rescued_task_ids',proof['pooled_rescued_task_ids']),('pooled_newly_late_task_ids',proof['pooled_newly_late_task_ids'])):
        _bind(evidence.get(name),value,'early complete fleet '+name)
    frozen._weighted(evidence.get('key'),proof['score'],weights,scales,'early complete fleet key')
    for name,value in (('complete_RELAY_model',True),('canonical_full_checked',False),('DIRECT_candidate_certificate',False)):
        if name in evidence:_bind(evidence[name],value,'early price certificate scope '+name)
    if 'official_lateness_epsilon_min' in evidence:_bind(evidence['official_lateness_epsilon_min'],EPSILON,'early official epsilon')
    _bind(evidence.get('priced_seed_routes'),routes,'early actual priced seed routes')
    if 'routes' in evidence:_bind(evidence['routes'],routes,'early exact reconstructed full routes')
    for name in ('event_times_min','delivery_times_min','actual_final_owners','source_drop_availability_min','handoff'):
        if name in evidence:_bind(evidence[name],proof[name],'early explicit full '+name)
    return dict(passed=True,pricing_scope=PRICING_SCOPE,complete_solution=False,
        complete_two_gap_RELAY_entity_physics_passed=True,DIRECT_candidate_certificate=False,
        native_score_scope='native tasks N/L plus entire modified aircraft route distance; D is not parcel-separable',
        source_layout_references=source_refs,receiver_skeleton=dict(route=skeleton,event_times_min=skeleton_times,
            delivery_times_min=skeleton_deliveries,native_score=skeleton_score),
        receiver_layout_reference=dict(gap=gap,delivery_order_task_ids=order,**expected_receiver,
            reported_float_route_replay_used=receiver['float_route_replay_used']),
        full_score=proof['score'],selected_delivery_times_min=selected,
        pooled_rescued_task_ids=proof['pooled_rescued_task_ids'],pooled_newly_late_task_ids=proof['pooled_newly_late_task_ids'],
        all_classifications={k:proof[k] for k in frozen._classes_names()},handoff=handoff,
        routes=routes,event_times_min=proof['event_times_min'],delivery_times_min=proof['delivery_times_min'],
        actual_final_owners=proof['actual_final_owners'],
        coverage_scope='only this saved priced representative; no omitted-position/pool coverage, global optimum or feasibility-bound claim')


def audit_split_feeder_family(tasks,topology,base_routes,family,*,weights=None,scales=None):
    result=frozen.audit_split_feeder_family(tasks,topology,base_routes,family,weights=weights,scales=scales)
    # The frozen physical proof already checked these actual positions. Any
    # new provenance aliases must describe that same route, not another gap.
    for records in family['source_tables'].values():
        for record in records:
            g,h=record['pickup_position'],record['in_drop_position']-1
            for field,value in (('original_pickup_gap',g),('original_drop_gap',h),
                ('source_layout_type','contiguous' if g==h else 'noncontiguous')):
                if field in record:_bind(record[field],value,'family actual source gap alias '+field)
    _bind(family.get('source_surface_scope'),'family_specific_cap_including_required_early_sources',
          'authoritative family source surface scope')
    channels=(('weighted','early_pricing_evidence'),('pooled','early_rescue_pricing_evidence'))
    saved_retention=family.get('seed_retention')
    require(isinstance(saved_retention,list) and len(saved_retention)==2,
            'Both priced channels need explicit source/pool/witness retention evidence')
    early={}
    retention_refs=[]
    for (channel,name),saved in zip(channels,saved_retention):
        proof=audit_split_feeder_early_price(tasks,topology,base_routes,family,
            family.get(name),weights=weights,scales=scales)
        routes=proof['routes'];preserved=[]
        for key,owner in zip(family['source_table_keys'],(family['a'],family['b'])):
            preserved.append(any(tuple(r['route'])==tuple(routes[owner]) for r in family['source_tables'][key]))
        source_kept=all(preserved)
        if not source_kept:
            require(any(len(family['source_tables'][key])<2 for key,kept in zip(family['source_table_keys'],preserved) if not kept),
                    'Priced source omission cannot be explained by a source cap below two early layouts')
        proof['actual_source_seed_routes_retained']=preserved
        proof['actual_witness_seed_route_retained']=any(w['routes']==routes for w in family['relay_witnesses'])
        witness_kept=proof['actual_witness_seed_route_retained']
        reason=('retained_exact_layout' if witness_kept else 'true_weighted_minN_pooled_representatives'
                if source_kept else 'source_cap_below_two_early_layouts')
        expected=dict(channel=channel,source_seed_routes_retained=source_kept,
            early_seed_full_scored=source_kept,witness_seed_route_retained=witness_kept,
            retained_or_omitted_by_better_complete_channels=reason,
            early_key=family[name]['key'],best_retained_key=family['priority'])
        require(isinstance(saved,dict),'Invalid early seed retention record')
        for field,value in expected.items():_bind(saved.get(field),value,'actual early seed opportunity '+field)
        if 'early_price_model_replayed' in saved:_bind(saved['early_price_model_replayed'],True,'early complete price model replay')
        if 'entered_candidate_pool' in saved:_bind(saved['entered_candidate_pool'],source_kept,'early source-backed candidate pool')
        if 'early_seed_entered_candidate_pool' in saved:
            _bind(saved['early_seed_entered_candidate_pool'],source_kept,'early source-backed candidate pool')
        if source_kept:
            require(family['priority'][0]<=family[name]['key'][0]+1e-10,
                    'Final weighted representative is worse than its source-backed early candidate')
        retention_refs.append(dict(**expected,early_price_model_replayed=True,
            entered_candidate_pool=source_kept,actual_source_route_retained_by_owner=preserved,
            best_retained_N=min(w['table_score']['late_count'] for w in family['relay_witnesses']),
            maximum_retained_pooled_rescued_count=max(len(w['pooled_rescued_task_ids']) for w in family['relay_witnesses'])))
        early[channel]=proof
    if 'early_priority' in family:
        _bind(family['early_priority'],family['early_pricing_evidence']['key'],'family primary early-two-gap priority')
    result['early_pricing_reference']=early['weighted']
    result['early_rescue_pricing_reference']=early['pooled']
    result['seed_retention_references']=retention_refs
    result['audit_version']='split_feeder_two_gap_native_price_v3'
    return result


def audit_split_feeder_proposal(*args,**kwargs):
    """The unchanged frozen031 full-candidate proof; family is checked once."""
    return frozen.audit_split_feeder_proposal(*args,**kwargs)
