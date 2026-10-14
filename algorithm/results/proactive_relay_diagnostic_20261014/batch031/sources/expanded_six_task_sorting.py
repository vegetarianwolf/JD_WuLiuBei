"""More source families under the unchanged six-task physical constructor.

Only selector retention changes: the original bounded source pools and proxy
rank score every family, retain at most N per station, then interleave station
depths. These proxy values select coverage and never authorize a gain, prune
the full incumbent or replace the frozen complete route-table/DAG guard.
"""
from __future__ import annotations

from heapq import heappush,heapreplace
from itertools import combinations,product
from time import perf_counter

from uav_dispatch.relay import build_plan_index

try:
    import six_task_sorting as frozen
except ImportError:
    from . import six_task_sorting as frozen


_active=False
PATCH_NAMES=("_select_families",)


def _expanded_select(problem,base,weights,scales,seed,*,deadline,max_families,pool_limit,stats,
                     families_per_station,selection_stats):
    """Original frozen pair/proxy arithmetic, with bounded per-H retention."""
    owners=build_plan_index(problem,base).delivery_route_by_task
    by_owner={a:tuple(sorted(t for t in problem.task_ids if owners[t]==a)) for a in range(problem.drone_count)}
    eligible=[a for a in range(problem.drone_count) if len(by_owner[a])>=2]
    original={};pair_credit={};station_records=[]
    registry={(leg.task_id,leg.relay_id,leg.kind) for leg in problem.leg_registry.values()}
    homes=problem.home_points()
    for a in eligible:
        frozen._check(deadline,stats)
        original[a]=frozen.costs.route_cost(problem,base[a],a,deadline=deadline,statistics=stats["route_cost_statistics"])["score"]
        for pair in combinations(by_owner[a],2):
            frozen._check(deadline,stats)
            route=tuple(v for v in base[a] if abs(v) not in pair)
            pair_credit[(a,pair)]=frozen.costs.route_cost(problem,route,a,deadline=deadline,statistics=stats["route_cost_statistics"])["score"]-original[a]
            stats["selector_pair_removal_evaluations"]+=1
    for station in problem.relay_stations[:max_families]:
        frozen._check(deadline,stats)
        pools={};out_geometry={}
        for a in eligible:
            ranked=[]
            for pair in combinations(by_owner[a],2):
                frozen._check(deadline,stats)
                if any((t,station.id,kind) not in registry for t in pair for kind in ("RELAY_IN","RELAY_OUT")):
                    continue
                if problem.task_relay_candidates and any(station.id not in problem.task_relay_candidates.get(t,()) for t in pair):
                    continue
                p,q=(problem.task(t) for t in pair)
                incoming=min(homes[a].distance_to(p.pickup)+p.pickup.distance_to(q.pickup)+q.pickup.distance_to(station.point),
                    homes[a].distance_to(q.pickup)+q.pickup.distance_to(p.pickup)+p.pickup.distance_to(station.point))
                credit=pair_credit[(a,pair)]
                proxy=frozen.weighted_key(credit,weights,scales)[0]+weights[2]/scales[2]*(incoming-.5*p.delivery.distance_to(q.delivery))
                ranked.append((proxy,pair,incoming,credit))
                stats["selector_station_pairs_ranked"]+=1
            pools[a]=sorted(ranked,key=lambda r:(r[0],r[1]))[:pool_limit]
        retained=[];scored=0
        for aircraft in combinations(eligible,3):
            frozen._check(deadline,stats)
            for options in product(*(pools[a] for a in aircraft)):
                frozen._check(deadline,stats)
                groups=tuple(option[1] for option in options)
                outbound=float("inf")
                for final in frozen._cross_maps(aircraft,groups):
                    distance=0.
                    for pair in final:
                        if pair not in out_geometry:
                            p,q=(problem.task(t).delivery for t in pair)
                            out_geometry[pair]=min(station.point.distance_to(p)+p.distance_to(q),
                                                   station.point.distance_to(q)+q.distance_to(p))
                        distance+=out_geometry[pair]
                    outbound=min(outbound,distance)
                    stats["selector_geometry_maps_ranked"]+=1
                proxy=sum(frozen.weighted_key(option[3],weights,scales)[0] for option in options)
                proxy+=weights[2]/scales[2]*(sum(option[2] for option in options)+outbound)
                flat=tuple(t for pair in groups for t in pair)
                tie=(sum((n+17)*t*31 for n,t in enumerate(flat))+sum(aircraft)*7+seed+station.id*101)%104729
                rank=(proxy,tie,*aircraft,*flat)
                stats["selector_six_task_sets_ranked"]+=1;scored+=1
                # A max-heap represented by negated numeric rank values.
                # The worst retained original rank is always at position 0.
                entry=(tuple(-value for value in rank),rank,aircraft,groups)
                if len(retained)<families_per_station:heappush(retained,entry)
                elif rank<retained[0][1]:heapreplace(retained,entry)
        records=[]
        for _,rank,aircraft,groups in sorted(retained,key=lambda entry:entry[1]):
            frozen._check(deadline,stats)
            records.append(dict(station_id=station.id,aircraft=list(aircraft),
                source_task_groups=[list(pair) for pair in groups],task_ids=sorted(t for pair in groups for t in pair),
                priority=list(rank)))
        station_records.append(records)
        selection_stats["stations"].append(dict(station_id=station.id,families_scored=scored,
            families_retained=len(records)))
    records=[]
    for depth in range(families_per_station):
        for station in station_records:
            frozen._check(deadline,stats)
            if depth<len(station):records.append(station[depth])
    frozen._check(deadline,stats)
    selection_stats.update(families_retained=len(records),catalog_returned=True)
    return records


def refine(problem,routes,*,deadline,weights,scales,seed=2026101512,relay_enabled=True,
           max_stations=4,families_per_station=64,in_candidates=2,in_gap_limit=4,
           out_gap_limit=4,direct_gap_limit=4,full_candidates_per_family=2,source_pair_pool_limit=4):
    """Keep top N original-ranked families per station in round-robin order."""
    global _active
    if _active:raise RuntimeError("nested expanded six-task selectors are unsupported")
    if (type(max_stations) is not int or not 1<=max_stations<=4
            or type(families_per_station) is not int or not 1<=families_per_station<=64):
        raise ValueError("station limit in 1..4 and families per station in 1..64 required")
    original=frozen._select_families
    started=perf_counter()
    selection_stats=dict(stations=[],families_retained=0,catalog_returned=False,logical_calls=0,
        deadline_reached=False,hard_family_limit=max_stations*families_per_station)
    def select(p,base,w,s,selector_seed,*,deadline,max_families,pool_limit,stats):
        selection_stats["logical_calls"]+=1
        try:
            return _expanded_select(p,base,w,s,selector_seed,deadline=deadline,max_families=max_families,
                pool_limit=pool_limit,stats=stats,families_per_station=families_per_station,
                selection_stats=selection_stats)
        except frozen.Deadline:
            selection_stats["deadline_reached"]=True
            raise
    _active=True
    try:
        frozen._select_families=select
        outcome=frozen.refine(problem,routes,deadline=deadline,weights=weights,scales=scales,
            seed=seed,relay_enabled=relay_enabled,max_families=max_stations,in_candidates=in_candidates,
            in_gap_limit=in_gap_limit,out_gap_limit=out_gap_limit,direct_gap_limit=direct_gap_limit,
            full_candidates_per_family=full_candidates_per_family,source_pair_pool_limit=source_pair_pool_limit)
    finally:
        intact=frozen._select_families is select
        frozen._select_families=original
        restored=frozen._select_families is original
        _active=False
        if not intact or not restored:raise RuntimeError("expanded six-task selector hook was changed or not restored")
    metadata=outcome["metadata"]
    metadata.update(method="Expanded-Six-Task-Fixed-Station-Sorting" if relay_enabled else
        "Expanded-Six-Task-Independent-DIRECT-Control",expanded_wrapped_method=metadata["method"],
        expanded_selector_statistics=selection_stats,selector_patch_names=list(PATCH_NAMES),
        selector_patch_restored=restored,selector_hook_intact_during_call=intact,
        expanded_configuration=dict(max_stations=max_stations,families_per_station=families_per_station,
            hard_family_limit=max_stations*families_per_station,source_pair_pool_limit=source_pair_pool_limit,
            legacy_max_families_semantic="station_limit",family_order="station_round_robin_by_original_proxy_rank",
            in_candidates=in_candidates,in_gap_limit=in_gap_limit,out_gap_limit=out_gap_limit,
            direct_gap_limit=direct_gap_limit,full_candidates_per_family=full_candidates_per_family,
            proxy_ranking_unchanged=True,proxy_is_gain_or_lower_bound=False,physical_constructor_unchanged=True,
            selector_shared_by_both_arms=True),runtime_seconds=perf_counter()-started)
    metadata["configuration"].update(max_stations=max_stations,families_per_station=families_per_station,
        hard_family_limit=max_stations*families_per_station,legacy_max_families_semantic="station_limit",
        one_family_per_existing_station=False)
    return outcome
