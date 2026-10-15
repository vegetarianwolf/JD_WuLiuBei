"""Shared repooling ranking with exact C single/joint removal scores.

This is a finite selector, not a lower bound or a complete macro scorer.
Source insertion deltas are native physical Scores. Removing compensation
requests replays C from its actual home and credits N/L/D exactly once. The
complete constructor, weighted adoption guard and physical audit stay intact.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from hashlib import sha256
import json
from math import isclose,isfinite
from time import perf_counter

from uav_dispatch.relay import build_plan_index


PATCH_NAMES=("_select_groups",)
_active=False


class _Identity:
    __slots__=("value",)
    def __init__(self,value):self.value=value
    def __hash__(self):return id(self.value)
    def __eq__(self,other):return isinstance(other,_Identity) and self.value is other.value


def _physical(score):
    return (score.late_count,score.total_lateness_min,score.distance_km)


def _key(score,weights,scales):
    physical=_physical(score)
    return (sum(w*x/s for w,x,s in zip(weights,physical,scales)),*physical)


def _removed_score(problem,route,aircraft,selected,local):
    """Actual native route score; whole DIRECT plans are removed together."""
    chosen=frozenset(selected)
    reduced=tuple(v for v in route if abs(v) not in chosen)
    return local.evaluate(reduced,start_node=problem.home_node(aircraft)).score


@contextmanager
def full_score_selector(repool_module,problem,*,weights,scales,deadline,statistics=None,
                        geo_pool_limit=6,removal_pool_limit=6):
    """Patch only the common selector; unsupported inputs delegate unchanged.

    Every family probes the union of geometry and complete C-removal pools,
    scores all distinct directed pairs, then retains compensation_limit**2.
    Memo tables retain strong problem/evaluator identities and are bounded.
    An interrupted selector raises the native deadline and returns no prefix.
    """
    global _active
    if _active:raise RuntimeError("nested full-score selectors are unsupported")
    weights,scales=tuple(weights),tuple(scales)
    if (len(weights)!=3 or len(scales)!=3 or any(not isfinite(x) or x<=0 for x in (*weights,*scales))
        or not isclose(sum(weights),1.) or not isfinite(deadline)
        or any(type(x) is not int or not 1<=x<=32 for x in (geo_pool_limit,removal_pool_limit))):
        raise ValueError("normalized finite objective/deadline and pool limits in 1..32 required")
    original=repool_module._select_groups
    stats=statistics if statistics is not None else {}
    stats.update(logical_calls=0,actual_selectors=0,delegations=0,selector_completed=False,
        base_route_score_calls=0,base_route_score_hits=0,single_removal_calls=0,single_removal_hits=0,
        pair_removal_calls=0,pair_removal_hits=0,insertion_calls=0,insertion_hits=0,
        families_shortlisted=0,families_processed=0,directed_pairs_scored=0,
        pool_candidates_before_union=0,pool_candidates_after_union=0,
        geometry_pool_truncations=0,removal_pool_truncations=0,family_pair_truncations=0,
        memo_evictions=0,deadline_reached=False,patches_restored=False,ranking_records=[],
        selected_groups=[],selector_sha256=sha256(b"[]").hexdigest(),selector_completed_at_perf_counter=None,
        configuration=dict(geo_pool_limit=geo_pool_limit,removal_pool_limit=removal_pool_limit,
            pool_union_limit_each=geo_pool_limit+removal_pool_limit,memo_limit_each=8192,
            family_retained="compensation_limit**2 after all directed distinct pairs",
            base_family_bound="max_groups*2 (unchanged)",destination_neighbours=3,
            score="native sourceA insertion + native sourceB insertion + actual C joint removal once",
            pool_removal_ranking="actual C single removal weighted physical score",
            complete_macro_score=False,lower_bound=False,partial_pruning=False,
            original_homes_tasks_resources_preserved=True,record_family_limit=16))
    caches={name:OrderedDict() for name in ("base_route_score","single_removal","pair_removal","insertion")}

    def check(call_deadline,producer_stats):
        now=perf_counter()
        if now>=min(deadline,call_deadline):
            stats["deadline_reached"]=True;producer_stats["deadline_reached"]=True
            raise repool_module._Deadline
        return now

    def cached(name,key,compute,call_deadline,producer_stats):
        check(call_deadline,producer_stats)
        cache=caches[name]
        if key in cache:
            value=cache.pop(key);cache[key]=value
            stats[name+"_hits"]+=1
        else:
            stats[name+"_calls"]+=1
            value=compute()
            check(call_deadline,producer_stats)
            if len(cache)>=8192:cache.popitem(last=False);stats["memo_evictions"]+=1
            cache[key]=value
        check(call_deadline,producer_stats)
        return value

    def eligible(p,base,checked,local,w,s):
        if p is not problem or getattr(local,"problem",None) is not p or tuple(w)!=weights or tuple(s)!=scales:
            return False
        if not getattr(checked,"valid",False) or len(base)!=p.drone_count:return False
        try:
            index=build_plan_index(p,base)
            return (not index.relay_task_ids and set(index.task_plans)==set(p.task_ids)
                and all(repool_module._capacity(p,r) for r in base)
                and all(sum(a==owner for owner in index.delivery_route_by_task.values())<=p.max_tasks_per_drone
                        for a in range(p.drone_count)))
        except (ValueError,KeyError,IndexError):return False

    def select(p,base,checked,local,w,s,seed,max_groups,receiver_limit,compensation_limit,
               position_limit,call_deadline,producer_stats):
        stats["selector_completed"]=False;stats["selected_groups"]=[]
        stats["selector_sha256"]=sha256(b"[]").hexdigest();stats["selector_completed_at_perf_counter"]=None
        producer_stats["selector_completed"]=False
        check(call_deadline,producer_stats)
        stats["logical_calls"]+=1
        if not eligible(p,base,checked,local,w,s):
            stats["delegations"]+=1
            result=original(p,base,checked,local,w,s,seed,max_groups,receiver_limit,compensation_limit,
                            position_limit,min(deadline,call_deadline),producer_stats)
            check(call_deadline,producer_stats)
            return result
        stats["actual_selectors"]+=1
        base=tuple(tuple(r) for r in base)
        owners=build_plan_index(p,base).delivery_route_by_task
        by_owner={a:tuple(t for t in p.task_ids if owners[t]==a) for a in range(p.drone_count)}
        homes=p.home_points();bases={};neighbour_pairs=set()
        for i in p.task_ids:
            check(call_deadline,producer_stats)
            ti,a=p.task(i),owners[i]
            neighbours=sorted((j for j in p.task_ids if owners[j]!=a),
                key=lambda j:(ti.delivery.distance_to(p.task(j).delivery),j))[:3]
            for j in neighbours:neighbour_pairs.add(tuple(sorted((i,j))))
        for i,j in sorted(neighbour_pairs):
            check(call_deadline,producer_stats)
            ti,tj=p.task(i),p.task(j);a,b=owners[i],owners[j]
            receivers=[]
            for c in range(p.drone_count):
                if c in (a,b) or len(by_owner[c])<2:continue
                destinations=[homes[c],*(p.task(t).delivery for t in by_owner[c])]
                affinity=min(ti.delivery.distance_to(point)+tj.delivery.distance_to(point) for point in destinations)
                receivers.append((affinity,c))
            for affinity,c in sorted(receivers)[:receiver_limit]:
                risk=sum(w[0]*int(checked.delivery_times_min[t]>p.task(t).deadline_min+1e-9)/s[0]
                    +w[1]*max(0.,checked.delivery_times_min[t]-p.task(t).deadline_min)/s[1] for t in (i,j))
                priority=(w[2]*(ti.delivery.distance_to(tj.delivery)+affinity)/s[2]-.5*risk,
                          (i*31+j*17+c*7+seed)%104729,i,j,c)
                bases[(i,j,c)]=(priority,a,b)
                producer_stats["receiver_bases_generated"]+=1
        shortlisted=sorted(bases.items(),key=lambda item:item[1][0])[:max_groups*2]
        stats["families_shortlisted"]+=len(shortlisted)
        families=[]
        for (i,j,c),(priority,a,b) in shortlisted:
            check(call_deadline,producer_stats)
            route_c=base[c]
            identity=(_Identity(p),_Identity(local),route_c,c,p.home_node(c))
            original_c=cached("base_route_score",identity,
                lambda:local.evaluate(route_c,start_node=p.home_node(c)).score,call_deadline,producer_stats)
            def removal(selected):
                chosen=tuple(sorted(selected));name="single_removal" if len(chosen)==1 else "pair_removal"
                return cached(name,(*identity,chosen),
                    lambda:_removed_score(p,route_c,c,chosen,local)-original_c,call_deadline,producer_stats)
            removal_ranked=sorted(((_key(removal((k,)),w,s),k) for k in by_owner[c]))
            removal_pool=tuple(k for _,k in removal_ranked[:removal_pool_limit])
            if len(by_owner[c])>removal_pool_limit:stats["removal_pool_truncations"]+=1
            side_options=[];side_records=[]
            for removed,target in ((i,a),(j,b)):
                check(call_deadline,producer_stats)
                route=tuple(v for v in base[target] if abs(v)!=removed)
                geometry=sorted(by_owner[c],key=lambda k:(
                    p.task(k).pickup.distance_to(homes[target])+p.task(k).delivery.distance_to(homes[target]),k))
                geometry_pool=tuple(geometry[:geo_pool_limit])
                if len(geometry)>geo_pool_limit:stats["geometry_pool_truncations"]+=1
                pool=tuple(sorted(set(geometry_pool)|set(removal_pool)))
                stats["pool_candidates_before_union"]+=len(geometry_pool)+len(removal_pool)
                stats["pool_candidates_after_union"]+=len(pool)
                options={}
                for k in pool:
                    check(call_deadline,producer_stats)
                    cache_key=(_Identity(p),_Identity(local),route,target,p.home_node(target),k,position_limit,
                               _Identity(repool_module.route_insertion_options))
                    def insert():
                        producer_stats["selector_insertion_calls"]+=1
                        found=repool_module.route_insertion_options(p,local,route,target,k,
                            candidate_limit=position_limit,option_count=1)
                        return found[0].delta if found else None
                    delta=cached("insertion",cache_key,insert,call_deadline,producer_stats)
                    if delta is not None:options[k]=delta
                side_options.append(options)
                side_records.append(dict(aircraft=target,removed_source=removed,geometry_pool=list(geometry_pool),
                    removal_pool=list(removal_pool),union_pool=list(pool),legal_insertions=sorted(options)))
            choices=[];pair_parts={}
            for k1,delta_a in side_options[0].items():
                for k2,delta_b in side_options[1].items():
                    check(call_deadline,producer_stats)
                    if k1==k2:continue
                    delta_c=removal((k1,k2))
                    pair_score=delta_a+delta_b+delta_c
                    pair_key=_key(pair_score,w,s)
                    stats["directed_pairs_scored"]+=1
                    group=repool_module._Group(i,j,a,b,c,k1,k2,(priority[0]+pair_key[0],*priority[1:],k1,k2))
                    choices.append(group)
                    pair_parts[(k1,k2)]=(delta_a,delta_b,delta_c,pair_score)
            choices=sorted(choices,key=lambda g:g.priority)
            retained=choices[:compensation_limit**2]
            if len(choices)>len(retained):stats["family_pair_truncations"]+=1
            if retained:families.append(retained)
            stats["families_processed"]+=1
            if len(stats["ranking_records"])<16:
                pair_records=[]
                for g in retained:
                    da,db,dc,combined=pair_parts[(g.k1,g.k2)]
                    pair_records.append(dict(k1=g.k1,k2=g.k2,source_insert_delta_a=list(_physical(da)),
                        source_insert_delta_b=list(_physical(db)),joint_C_removal_delta=list(_physical(dc)),
                        ranking_delta=list(_physical(combined)),ranking_key=list(_key(combined,w,s))))
                stats["ranking_records"].append(dict(i=i,j=j,c=c,source_aircraft=[a,b],
                    C_base_score=list(_physical(original_c)),side_pools=side_records,
                    directed_pairs=len(choices),retained_pairs=pair_records))
        selected=[]
        for depth in range(compensation_limit**2):
            check(call_deadline,producer_stats)
            layer=sorted((family[depth] for family in families if len(family)>depth),key=lambda g:g.priority)
            selected.extend(layer[:max_groups-len(selected)])
            if len(selected)>=max_groups:break
        check(call_deadline,producer_stats)
        records=[repool_module._group_record(g) for g in selected]
        digest=sha256(json.dumps(records,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        completed_at=check(call_deadline,producer_stats)
        stats["selected_groups"]=records
        stats["selector_sha256"]=digest
        stats["selector_completed"]=True
        stats["selector_completed_at_perf_counter"]=completed_at
        return tuple(selected)

    _active=True
    try:
        repool_module._select_groups=select
        yield stats
    finally:
        repool_module._select_groups=original
        for cache in caches.values():cache.clear()
        stats["patches_restored"]=True;_active=False
