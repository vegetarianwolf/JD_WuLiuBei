"""Three-aircraft six-request sorting at an existing fixed relay station.

The shared bounded selector releases two whole DIRECT requests per aircraft.
RELAY collects each source pair before any local OUT, then independently
re-pairs all six requests across the three aircraft. DIRECT optimizes all 90
two-per-aircraft partitions within its declared route table, rather than
being constrained to a relay-selected final map. Every native clock and all
travel from the unchanged homes enter the tables and canonical full guard.
"""
from __future__ import annotations

from hashlib import sha256
from itertools import combinations,product
import json
from math import isclose,isfinite
from time import perf_counter

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution

try:
    import six_task_route_costs as costs
except ImportError:
    from . import six_task_route_costs as costs


Deadline=costs.Deadline


def weighted_key(score,weights,scales):
    values=(score.late_count,score.total_lateness_min,score.distance_km)
    return (sum(w*x/s for w,x,s in zip(weights,values,scales)),*values)


def _physical(score):
    return dict(late_count=score.late_count,total_lateness_min=score.total_lateness_min,distance_km=score.distance_km)


def _check(deadline,stats):
    if perf_counter()>=deadline:
        stats["deadline_reached"]=True
        raise Deadline


def _digest(records):
    return sha256(json.dumps(records,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def _cross_maps(aircraft,groups):
    """Exactly 2**3 maps: each source sends one request to each other UAV."""
    for bits in product((0,1),repeat=3):
        final={a:[] for a in aircraft}
        for source,pair,bit in zip(aircraft,groups,bits):
            other=[a for a in aircraft if a!=source]
            final[other[bit]].append(pair[0]);final[other[1-bit]].append(pair[1])
        yield tuple(tuple(sorted(final[a])) for a in aircraft)


def _select_families(problem,base,weights,scales,seed,*,deadline,max_families,pool_limit,stats):
    owners=build_plan_index(problem,base).delivery_route_by_task
    by_owner={a:tuple(sorted(t for t in problem.task_ids if owners[t]==a)) for a in range(problem.drone_count)}
    eligible=[a for a in range(problem.drone_count) if len(by_owner[a])>=2]
    original={};pair_credit={};records=[]
    registry={(leg.task_id,leg.relay_id,leg.kind) for leg in problem.leg_registry.values()}
    homes=problem.home_points()
    for a in eligible:
        _check(deadline,stats)
        original[a]=costs.route_cost(problem,base[a],a,deadline=deadline,statistics=stats["route_cost_statistics"])["score"]
        for pair in combinations(by_owner[a],2):
            _check(deadline,stats)
            route=tuple(v for v in base[a] if abs(v) not in pair)
            pair_credit[(a,pair)]=costs.route_cost(problem,route,a,deadline=deadline,statistics=stats["route_cost_statistics"])["score"]-original[a]
            stats["selector_pair_removal_evaluations"]+=1
    for station in problem.relay_stations[:max_families]:
        _check(deadline,stats)
        pools={};out_geometry={}
        for a in eligible:
            ranked=[]
            for pair in combinations(by_owner[a],2):
                _check(deadline,stats)
                if any((t,station.id,kind) not in registry for t in pair for kind in ("RELAY_IN","RELAY_OUT")):
                    continue
                if problem.task_relay_candidates and any(station.id not in problem.task_relay_candidates.get(t,()) for t in pair):
                    continue
                p,q=(problem.task(t) for t in pair)
                incoming=min(homes[a].distance_to(p.pickup)+p.pickup.distance_to(q.pickup)+q.pickup.distance_to(station.point),
                    homes[a].distance_to(q.pickup)+q.pickup.distance_to(p.pickup)+p.pickup.distance_to(station.point))
                credit=pair_credit[(a,pair)]
                proxy=weighted_key(credit,weights,scales)[0]+weights[2]/scales[2]*(incoming-.5*p.delivery.distance_to(q.delivery))
                ranked.append((proxy,pair,incoming,credit))
                stats["selector_station_pairs_ranked"]+=1
            pools[a]=sorted(ranked,key=lambda r:(r[0],r[1]))[:pool_limit]
        best=None
        for aircraft in combinations(eligible,3):
            _check(deadline,stats)
            for options in product(*(pools[a] for a in aircraft)):
                _check(deadline,stats)
                groups=tuple(option[1] for option in options)
                outbound=float("inf")
                for final in _cross_maps(aircraft,groups):
                    distance=0.
                    for pair in final:
                        if pair not in out_geometry:
                            p,q=(problem.task(t).delivery for t in pair)
                            out_geometry[pair]=min(station.point.distance_to(p)+p.distance_to(q),
                                                   station.point.distance_to(q)+q.distance_to(p))
                        distance+=out_geometry[pair]
                    outbound=min(outbound,distance)
                    stats["selector_geometry_maps_ranked"]+=1
                proxy=sum(weighted_key(option[3],weights,scales)[0] for option in options)
                proxy+=weights[2]/scales[2]*(sum(option[2] for option in options)+outbound)
                flat=tuple(t for pair in groups for t in pair)
                tie=(sum((n+17)*t*31 for n,t in enumerate(flat))+sum(aircraft)*7+seed+station.id*101)%104729
                rank=(proxy,tie,*aircraft,*flat)
                stats["selector_six_task_sets_ranked"]+=1
                if best is None or rank<best[0]:best=(rank,aircraft,groups)
        if best is not None:
            rank,aircraft,groups=best
            records.append(dict(station_id=station.id,aircraft=list(aircraft),
                source_task_groups=[list(pair) for pair in groups],task_ids=sorted(t for pair in groups for t in pair),
                priority=list(rank)))
    _check(deadline,stats)
    return records


def _native(problem,base,family):
    selected=set(family["task_ids"])
    return tuple(tuple(v for v in route if abs(v) not in selected) for route in base)


def _top(candidates,limit,weights,scales):
    unique={candidate["routes"]:candidate for candidate in candidates}
    return sorted(unique.values(),key=lambda c:(weighted_key(c["score"],weights,scales),c["routes"]))[:limit]


def _move(problem,family,final,records,relay_enabled,availability):
    aircraft=family["aircraft"]
    return dict(family=family,mode="RELAY" if relay_enabled else "DIRECT",
        station_id=family["station_id"] if relay_enabled else None,aircraft=list(aircraft),
        source_task_groups=family["source_task_groups"],final_task_groups=[list(pair) for pair in final],
        final_aircraft_by_task={str(t):a for a,pair in zip(aircraft,final) for t in pair},
        source_drop_availability_min={str(t):clock for t,clock in sorted(availability.items())},
        shared_double_loaded_km_by_aircraft={str(a):record["shared_loaded_distance_km"] for a,record in zip(aircraft,records)})


def _candidate(base_records,family,records,final,relay_enabled,availability,problem):
    all_records=list(base_records)
    for a,record in zip(family["aircraft"],records):all_records[a]=record
    return dict(routes=tuple(record["route"] for record in all_records),
        score=sum((record["score"] for record in all_records),Score(0,0.,0.)),
        records=all_records,move=_move(problem,family,final,records,relay_enabled,availability))


def _relay(problem,base_records,native,family,weights,scales,*,deadline,in_limit,in_gap_limit,gap_limit,full_limit,stats):
    aircraft=family["aircraft"];station=family["station_id"]
    incoming=[];complete=[]
    for a,pair in zip(aircraft,family["source_task_groups"]):
        options=costs.in_options(problem,native[a],a,tuple(pair),station,deadline=deadline,
            option_limit=in_limit,gap_limit=in_gap_limit,weights=weights,scales=scales,statistics=stats["route_cost_statistics"])
        incoming.append(options)
    for incoming_combo in product(*incoming):
        _check(deadline,stats);stats["in_combinations_examined"]+=1
        availability={t:clock for record in incoming_combo for t,clock in record["availability"].items()}
        tables={}
        for final in _cross_maps(aircraft,family["source_task_groups"]):
            _check(deadline,stats)
            records=[]
            for a,pair,in_record in zip(aircraft,final,incoming_combo):
                key=(a,pair)
                if key not in tables:
                    tables[key]=costs.out_option(problem,in_record,a,pair,station,availability,deadline=deadline,
                        gap_limit=gap_limit,weights=weights,scales=scales,statistics=stats["route_cost_statistics"])
                    stats["out_pair_table_calls"]+=1
                records.append(tables[key])
            stats["relay_final_maps_examined"]+=1
            if any(record is None or record["shared_loaded_distance_km"]<=1e-10 for record in records):continue
            candidate=_candidate(base_records,family,records,final,True,availability,problem)
            complete.append(candidate);stats["complete_table_candidates"]+=1
            complete=_top(complete,full_limit,weights,scales)
    return complete


def _direct(problem,base_records,native,family,weights,scales,*,deadline,gap_limit,full_limit,stats):
    aircraft=family["aircraft"];tasks=family["task_ids"];tables={}
    pairs=[]
    for bits in combinations(range(6),2):pairs.append((sum(1<<b for b in bits),tuple(tasks[b] for b in bits)))
    for a in aircraft:
        for mask,pair in pairs:
            _check(deadline,stats)
            tables[(a,mask)]=costs.direct_option(problem,native[a],a,pair,deadline=deadline,
                gap_limit=gap_limit,weights=weights,scales=scales,statistics=stats["route_cost_statistics"])
            stats["direct_pair_table_calls"]+=1
    # Keep the best two prefixes per covered mask. Future costs depend only
    # on that mask, so this remains exact for the top-two finite-table plans.
    states={0:[(Score(0,0.,0.),(),())]}
    for a in aircraft:
        next_states={}
        for used,prefixes in states.items():
            for mask,pair in pairs:
                _check(deadline,stats)
                if used&mask or tables[(a,mask)] is None:continue
                for score,records,final in prefixes:
                    stats["direct_dp_transitions"]+=1
                    bucket=next_states.setdefault(used|mask,[])
                    bucket.append((score+tables[(a,mask)]["score"],records+(tables[(a,mask)],),final+(pair,)))
                    bucket.sort(key=lambda r:(weighted_key(r[0],weights,scales),tuple(x["route"] for x in r[1])))
                    del bucket[full_limit:]
        states=next_states
    complete=[]
    for _,records,final in states.get(63,[]):
        complete.append(_candidate(base_records,family,records,final,False,{},problem))
        stats["complete_table_candidates"]+=1
    return complete


def _verify(problem,base,routes,move,relay_enabled):
    before=build_plan_index(problem,base);index=build_plan_index(problem,routes)
    family=move["family"];selected=set(family["task_ids"]);aircraft=family["aircraft"]
    for a in range(problem.drone_count):
        if sum(owner==a for owner in before.delivery_route_by_task.values())!=sum(owner==a for owner in index.delivery_route_by_task.values()):
            raise AssertionError("six-task sorting changed final quota")
        if tuple(v for v in base[a] if abs(v) not in selected)!=tuple(v for v in routes[a]
                if (problem.leg_registry.get(abs(v)).task_id if abs(v) in problem.leg_registry else abs(v)) not in selected):
            raise AssertionError("six-task sorting changed an unselected native event")
    if any(index.delivery_route_by_task[t]!=a for t,a in ((int(t),a) for t,a in move["final_aircraft_by_task"].items())):
        raise AssertionError("six-task final map differs")
    if set(index.relay_task_ids)!=(selected if relay_enabled else set()):raise AssertionError("six-task mode differs")
    def block(route,identifiers):
        positions=sorted(route.index(v) for identifier in identifiers for v in (identifier,-identifier))
        chunk=route[positions[0]:positions[0]+4]
        if positions!=list(range(positions[0],positions[0]+4)) or len(chunk)!=4 or any(v<0 for v in chunk[:2]) or any(v>0 for v in chunk[2:]):
            raise AssertionError("six-task transport batch is not a contiguous ++-- block")
        if sum(1 if v>0 else -1 for v in route[:positions[0]])!=0:
            raise AssertionError("six-task transport batch does not enter empty")
    if relay_enabled:
        for a,own,received in zip(aircraft,family["source_task_groups"],move["final_task_groups"]):
            sources={before.delivery_route_by_task[t] for t in received}
            if len(sources)!=2 or a in sources:raise AssertionError("relay pair is not repooled from two other sources")
            incoming=[];outgoing=[];in_legs=[];out_legs=[]
            for t in selected:
                plan=index.task_plans[t]
                if plan.relay_id!=family["station_id"]:raise AssertionError("six-task station differs")
                if t in own:
                    if index.inbound_route_by_task[t]!=a:raise AssertionError("six-task source responsibility changed")
                    incoming.extend(routes[a].index(-leg) for leg in plan.leg_ids if problem.leg_registry[leg].kind=="RELAY_IN")
                    in_legs.extend(leg for leg in plan.leg_ids if problem.leg_registry[leg].kind=="RELAY_IN")
                if t in received:
                    outgoing.extend(routes[a].index(leg) for leg in plan.leg_ids if problem.leg_registry[leg].kind=="RELAY_OUT")
                    out_legs.extend(leg for leg in plan.leg_ids if problem.leg_registry[leg].kind=="RELAY_OUT")
            if max(incoming)>=min(outgoing):raise AssertionError("own OUT precedes own IN completion")
            block(routes[a],in_legs);block(routes[a],out_legs)
    else:
        for a,pair in zip(aircraft,move["final_task_groups"]):
            positions=sorted(routes[a].index(v) for t in pair for v in (t,-t))
            if positions!=list(range(positions[0],positions[0]+4)):
                raise AssertionError("DIRECT pair no longer forms its declared four-event insertion block")
            if sum(1 if v>0 else -1 for v in routes[a][:positions[0]])!=0:
                raise AssertionError("DIRECT pair insertion does not enter its gap empty")


def _snapshot(problem,routes,checked,schedule,weights,scales,*,phase,move=None,table_score=None):
    index=build_plan_index(problem,routes)
    return dict(routes=[list(r) for r in routes],score=_physical(checked.score),key=list(weighted_key(checked.score,weights,scales)),
        valid=True,complete=True,phase=phase,move=move,
        cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t] for t in index.relay_task_ids),
        relay_task_count=len(index.relay_task_ids),
        event_times_min=[[schedule.event_times_min[(a,p)] for p in range(len(route))] for a,route in enumerate(routes)],
        delivery_times_min={str(t):clock for t,clock in sorted(schedule.delivery_times_min.items())},
        source_drop_availability_min=move["source_drop_availability_min"] if move is not None else {},
        table_score=_physical(table_score if table_score is not None else checked.score))


def refine(problem,routes,*,deadline,weights,scales,seed=2026101512,relay_enabled=True,
           max_families=4,in_candidates=2,in_gap_limit=4,out_gap_limit=4,direct_gap_limit=4,
           full_candidates_per_family=2,source_pair_pool_limit=4):
    """Construct bounded complete six-task candidates; retain the checked input."""
    started=perf_counter();weights,scales=tuple(weights),tuple(scales)
    if (not isfinite(deadline) or type(relay_enabled) is not bool or len(weights)!=3 or len(scales)!=3
            or any(not isfinite(v) or v<=0 for v in (*weights,*scales)) or not isclose(sum(weights),1.)
            or any(type(v) is not int or not 1<=v<=upper for v,upper in
                ((max_families,4),(in_candidates,2),(in_gap_limit,4),(out_gap_limit,4),(direct_gap_limit,4),
                 (full_candidates_per_family,2),(source_pair_pool_limit,4)))):
        raise ValueError("finite weighted objective, boolean arm and bounded six-task search limits required")
    base=tuple(tuple(r) for r in routes);base+=((),)*(problem.drone_count-len(base))
    checked=evaluate_solution(problem,base,_force_full=True)
    if not checked.valid or len(base)!=problem.drone_count:raise ValueError("invalid complete initial solution")
    if problem.capacity!=2 or not problem.has_relays or problem.distance_matrix_km is not None or build_plan_index(problem,base).relay_task_ids:
        raise ValueError("six-task sorting requires Euclidean capacity two, fixed stations and a pure DIRECT input")
    best,best_checked=base,checked;initial_key=best_key=weighted_key(checked.score,weights,scales)
    evaluator=GlobalRelayEvaluator(problem);schedule=evaluator.evaluate(base)
    stats=dict(method="Six-Task-Fixed-Station-Sorting" if relay_enabled else "Six-Task-Independent-DIRECT-Control",
        seed=seed,relay_enabled=relay_enabled,deadline_perf_counter=deadline,deadline_reached=False,
        selector_completed=False,selected_families=[],selector_sha256=_digest([]),selector_completed_at_perf_counter=None,
        selector_pair_removal_evaluations=0,selector_station_pairs_ranked=0,selector_six_task_sets_ranked=0,
        selector_geometry_maps_ranked=0,in_combinations_examined=0,out_pair_table_calls=0,
        relay_final_maps_examined=0,direct_pair_table_calls=0,direct_dp_transitions=0,complete_table_candidates=0,
        full_evaluations=0,families_examined=0,accepted_improvements=0,accepted_moves=[],full_candidate_snapshots=[],
        full_validation_timings=[],active_stage="initial",timeout_stage=None,
        first_complete_candidate=None,best_constructed_candidate=None,route_cost_statistics={},
        input_snapshot=_snapshot(problem,base,checked,schedule,weights,scales,phase="input"),
        initial_key=list(initial_key),final_key=list(initial_key),family_records=[],final_cross_uav_count=0,
        configuration=dict(max_families=max_families,in_candidates=in_candidates,in_gap_limit=in_gap_limit,out_gap_limit=out_gap_limit,
            direct_gap_limit=direct_gap_limit,full_candidates_per_family=full_candidates_per_family,
            source_pair_pool_limit=source_pair_pool_limit,one_family_per_existing_station=True,
            source_pair_enumeration="all original same-owner pairs; exact native joint-removal score cached across H",
            pair_pool_proxy="J(removal)+wD/sD*(home-pickup-pickup-H minimum-0.5*destination separation)",
            family_proxy="sum J(removal)+wD/sD*(three incoming proxy walks+best of eight OUT geometry maps)",
            proxy_is_gain_or_lower_bound=False,direct_pair_count_per_aircraft=15,direct_final_map_count=90,
            relay_final_maps_per_in_combination=8,unselected_events_fixed=True,resources_and_homes_unchanged=True))
    try:
        _check(deadline,stats)
        stats["active_stage"]="shared_selector"
        families=_select_families(problem,base,weights,scales,seed,deadline=deadline,max_families=max_families,
            pool_limit=source_pair_pool_limit,stats=stats)
        selector_sha256=_digest(families)
        completed_at=perf_counter()
        if completed_at>=deadline:raise Deadline
        stats.update(selected_families=families,selector_sha256=selector_sha256,selector_completed=True,
            selector_completed_at_perf_counter=completed_at)
        stats["active_stage"]="baseline_route_tables"
        base_records=[costs.route_cost(problem,r,a,deadline=deadline,statistics=stats["route_cost_statistics"]) for a,r in enumerate(base)]
        for family in families:
            _check(deadline,stats);stats["families_examined"]+=1
            stats["active_stage"]="family_route_tables"
            native=_native(problem,base,family)
            arguments=dict(deadline=deadline,full_limit=full_candidates_per_family,stats=stats)
            complete=(_relay(problem,base_records,native,family,weights,scales,in_limit=in_candidates,in_gap_limit=in_gap_limit,
                gap_limit=out_gap_limit,**arguments) if relay_enabled else
                _direct(problem,base_records,native,family,weights,scales,gap_limit=direct_gap_limit,**arguments))
            stats["family_records"].append(dict(family=family,complete_candidates=len(complete)))
            for candidate in complete:
                _check(deadline,stats)
                stats["active_stage"]="canonical_full_guard"
                full=evaluate_solution(problem,candidate["routes"],_force_full=True);stats["full_evaluations"]+=1
                _check(deadline,stats)
                if not full.valid:raise AssertionError("finite route table yielded an invalid full plan")
                canonical=evaluator.evaluate(candidate["routes"])
                if not canonical.feasible:raise AssertionError("six-task canonical DAG is infeasible")
                _verify(problem,base,candidate["routes"],candidate["move"],relay_enabled)
                if (candidate["score"].late_count!=full.score.late_count
                        or abs(candidate["score"].total_lateness_min-full.score.total_lateness_min)>1e-8
                        or abs(candidate["score"].distance_km-full.score.distance_km)>1e-8):
                    raise AssertionError("six-task full table score differs from canonical global score")
                for a,record in enumerate(candidate["records"]):
                    if len(record["event_times"])!=len(candidate["routes"][a]):
                        raise AssertionError("six-task route table clock coverage differs from full route")
                    if any(abs(clock-canonical.event_times_min[(a,p)])>1e-8 for p,clock in enumerate(record["event_times"])):
                        raise AssertionError("six-task route table clock differs from canonical global DAG")
                snapshot=_snapshot(problem,candidate["routes"],full,canonical,weights,scales,phase="full_proposal",
                    move=candidate["move"],table_score=candidate["score"])
                verified_at=perf_counter()
                if verified_at>=deadline:raise Deadline
                stats["full_validation_timings"].append(dict(snapshot_index=len(stats["full_candidate_snapshots"]),
                    full_verified_at_perf_counter=verified_at))
                stats["full_candidate_snapshots"].append(snapshot)
                if stats["first_complete_candidate"] is None:stats["first_complete_candidate"]=snapshot
                if stats["best_constructed_candidate"] is None or tuple(snapshot["key"])<tuple(stats["best_constructed_candidate"]["key"]):
                    stats["best_constructed_candidate"]=snapshot
                key=weighted_key(full.score,weights,scales)
                if key<best_key:
                    stats["active_stage"]="adoption_guard"
                    accepted_at=perf_counter()
                    if accepted_at>=deadline:raise Deadline
                    best,best_checked,best_key=candidate["routes"],full,key
                    stats["accepted_improvements"]+=1
                    stats["accepted_moves"].append(dict(move=candidate["move"],full_snapshot=snapshot,
                        full_verified_at_perf_counter=verified_at,accepted_at_perf_counter=accepted_at))
    except Deadline:
        stats["deadline_reached"]=True
        stats["timeout_stage"]=stats["active_stage"]
    index=build_plan_index(problem,best)
    stats.update(final_key=list(best_key),final_cross_uav_count=sum(index.inbound_route_by_task[t]!=index.outbound_route_by_task[t]
        for t in index.relay_task_ids),runtime_seconds=perf_counter()-started)
    return dict(routes=best,evaluation=best_checked,iterations=stats["complete_table_candidates"],metadata=stats)
