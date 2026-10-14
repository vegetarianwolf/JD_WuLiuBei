"""Exact caching of whole receiver-only DIRECT and RELAY bundle enumeration.

Frozen ``_receiver_relay`` reads only C's pre-bundle route and the two OUT IDs.
Frozen ``_receiver_direct`` calls local ``_direct`` on C twice, copies other
routes into temporary states, then discards those routes and retains only C.
Neither reads source positions/clocks, State.key or State.station for ranking.
Caching therefore precedes repeated candidate generation without changing it.

Enter outside the source-frontier driver. The frozen functions' globals still
refer to the actual repooling module, so their internal calls use its active
joint/source ranker patches. Cache keys retain exact problem/local identities,
complete group data, actual C route, selected legs, objective and all bounds.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from math import isclose,isfinite
from time import perf_counter

from uav_dispatch.model import Score


_active=False
PATCH_NAMES=("_receiver_relay","_receiver_direct")


class _Identity:
    __slots__=("value",)
    def __init__(self,value):self.value=value
    def __hash__(self):return id(self.value)
    def __eq__(self,other):return isinstance(other,_Identity) and self.value is other.value


def _group_key(group):
    values=tuple(getattr(group,name) for name in ("i","j","a","b","c","k1","k2","priority"))
    hash(values)
    return values


@contextmanager
def receiver_bundle_cache(repool_module,problem,*,weights,scales,deadline,statistics=None,memo_limit=8192):
    """Cache immutable receiver route tuples under guarded receiver-only input.

    On a cache hit, no candidate-generation/local-evaluation producer counter
    is incremented. This module records every logical call, actual call and hit
    separately. Expired hits are rejected before returning any saved routes.
    """
    global _active
    if _active:raise RuntimeError("nested receiver-bundle caches are unsupported")
    weights,scales=tuple(weights),tuple(scales)
    if (not isfinite(deadline) or len(weights)!=3 or len(scales)!=3
        or any(not isfinite(x) or x<=0 for x in (*weights,*scales)) or not isclose(sum(weights),1.)):
        raise ValueError("finite normalized weights/scales and deadline required")
    if type(memo_limit) is not int or memo_limit<1:
        raise ValueError("positive receiver-bundle memo limit required")
    original={name:getattr(repool_module,name) for name in PATCH_NAMES}
    stats=statistics if statistics is not None else {}
    stats.update(relay_logical_calls=0,relay_actual_calls=0,relay_cache_hits=0,relay_delegations=0,
        direct_logical_calls=0,direct_actual_calls=0,direct_cache_hits=0,direct_delegations=0,
        memo_evictions=0,deadline_reached=False,patches_restored=False,
        configuration=dict(memo_limit_each=memo_limit,pure_DIRECT_receiver_required=True,
            source_routes_clocks_and_state_key_not_dependencies=True,output="immutable receiver route tuples",
            candidate_generation_policy_unchanged=True,partial_pruning=False,
            counter_semantics="logical requests / actual enumerations / exact hits separately"))
    caches={name:OrderedDict() for name in ("relay","direct")}
    def check(call_deadline,producer_stats):
        if perf_counter()>=min(deadline,call_deadline):
            stats["deadline_reached"]=True;producer_stats["deadline_reached"]=True
            raise repool_module._Deadline
    def eligible(p,state,g,local):
        try:
            identities=(g.i,g.j,g.a,g.b,g.c,g.k1,g.k2)
            if (p is not problem or local.problem is not p or len(state.routes)!=p.drone_count
                or any(type(x) is not int for x in identities) or len({g.a,g.b,g.c})!=3
                or not all(0<=a<p.drone_count for a in (g.a,g.b,g.c))
                or len({g.i,g.j,g.k1,g.k2})!=4 or not {g.i,g.j,g.k1,g.k2}<=set(p.task_ids)):
                return None
            route=tuple(state.routes[g.c])
            if (any(abs(v) in (p.leg_registry or {}) for v in route)
                or any(abs(v) not in p.task_ids for v in route)
                or {g.i,g.j,g.k1,g.k2}&{abs(v) for v in route}
                or not repool_module._capacity(p,route)):
                return None
            active,seen=set(),set()
            for visit in route:
                if visit>0:
                    if visit in seen:return None
                    seen.add(visit);active.add(visit)
                else:
                    if -visit not in active:return None
                    active.remove(-visit)
            if active:return None
            return route,_group_key(g)
        except (AttributeError,TypeError,ValueError,IndexError):
            return None
    def read(name,key):
        cache=caches[name]
        if key not in cache:return False,None
        value=cache.pop(key);cache[key]=value
        return True,value
    def save(name,key,value):
        cache=caches[name]
        if len(cache)>=memo_limit:cache.popitem(last=False);stats["memo_evictions"]+=1
        cache[key]=value
    def run(name,p,state,g,local,w,s,position_limit,option_count,call_deadline,producer_stats,outgoing=()):
        check(call_deadline,producer_stats)
        stats[name+"_logical_calls"]+=1
        covered=eligible(p,state,g,local)
        if name=="relay" and covered is not None:
            registry=p.leg_registry or {}
            legs=[registry.get(identifier) for identifier in outgoing]
            if (len(legs)!=2 or any(leg is None or leg.kind!="RELAY_OUT" for leg in legs)
                or tuple(leg.task_id for leg in legs)!=(g.i,g.j) or legs[0].relay_id!=legs[1].relay_id):
                covered=None
        policy_names=("_rank_receiver","_capacity","_positions","_negative_positions","_shared_distance")
        if name=="direct":policy_names+=("_direct","route_insertion_options")
        policy=tuple((label,_Identity(getattr(repool_module,label))) for label in policy_names if hasattr(repool_module,label))
        key=None if covered is None else (_Identity(p),_Identity(local),covered[0],covered[1],
            tuple(outgoing),tuple(w),tuple(s),position_limit,option_count,policy,_Identity(Score.__lt__))
        if key is not None:
            hit,value=read(name,key)
            if hit:
                check(call_deadline,producer_stats)
                stats[name+"_cache_hits"]+=1
                return value
        else:
            stats[name+"_delegations"]+=1
        stats[name+"_actual_calls"]+=1
        if name=="relay":
            value=original["_receiver_relay"](p,state,g,*outgoing,local,w,s,position_limit,option_count,
                min(deadline,call_deadline),producer_stats)
        else:
            value=original["_receiver_direct"](p,state,g,local,w,s,position_limit,option_count,
                min(deadline,call_deadline),producer_stats)
        check(call_deadline,producer_stats)
        value=tuple(tuple(route) for route in value)
        if key is not None:save(name,key,value)
        return value
    def relay(p,state,g,out_i,out_j,local,w,s,position_limit,option_count,call_deadline,producer_stats):
        return run("relay",p,state,g,local,w,s,position_limit,option_count,call_deadline,producer_stats,(out_i,out_j))
    def direct(p,state,g,local,w,s,position_limit,option_count,call_deadline,producer_stats):
        return run("direct",p,state,g,local,w,s,position_limit,option_count,call_deadline,producer_stats)
    _active=True
    try:
        repool_module._receiver_relay=relay;repool_module._receiver_direct=direct
        yield stats
    finally:
        for name,function in original.items():setattr(repool_module,name,function)
        for cache in caches.values():cache.clear()
        stats["patches_restored"]=True;_active=False
