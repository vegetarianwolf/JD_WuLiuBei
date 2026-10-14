"""Coverage-only expansion: exact old ranks, station fairness and full replay."""
from dataclasses import replace
from time import perf_counter
import json

import pytest

from uav_dispatch import RelayStation,Task,TransportLeg
from algorithm.experiments import expanded_six_task_sorting as method
from algorithm.tests.test_six_task_sorting import original,audit_snapshots
from algorithm.tests.test_six_task_route_costs import fixture,W,S
from algorithm.tests.test_relay_repooling_exchange import topology


def larger_fixture():
    problem=fixture();homes=problem.home_points();tasks=[];routes=[]
    for a in range(3):
        others=[b for b in range(3) if b!=a]
        ids=[]
        for number in range(4):
            task=4*a+number+1;ids.append(task)
            tasks.append(Task(task,homes[a],homes[others[number%2]],100.))
        routes.append(tuple(v for pair in (ids[:2],ids[2:]) for v in (*pair,*(-t for t in pair))))
    stations=(problem.relay_stations[0],RelayStation(10,1.,.4))
    registry={}
    for number,station in enumerate(stations):
        for task in tasks:
            identifier=1000+number*100+2*task.id
            registry[identifier]=TransportLeg(identifier,task.id,"RELAY_IN",station.id)
            registry[identifier+1]=TransportLeg(identifier+1,task.id,"RELAY_OUT",station.id)
    return replace(problem,tasks=tuple(tasks),max_tasks_per_drone=4,relay_stations=stations,
        leg_registry=registry,task_relay_candidates={t.id:(9,10) for t in tasks}),tuple(routes)


def selector_stats():
    return dict(selector_pair_removal_evaluations=0,selector_station_pairs_ranked=0,
        selector_geometry_maps_ranked=0,selector_six_task_sets_ranked=0,route_cost_statistics={},deadline_reached=False)


def catalog(problem,base,count):
    stats=selector_stats();selection=dict(stations=[])
    records=method._expanded_select(problem,base,W,S,2026101512,deadline=perf_counter()+2.,
        max_families=2,pool_limit=4,stats=stats,families_per_station=count,selection_stats=selection)
    return records,stats,selection


def test_top_n_is_exact_over_all_original_ranked_source_pool_families_and_round_robins_stations():
    problem,base=larger_fixture()
    legacy=method.frozen._select_families(problem,base,W,S,2026101512,deadline=perf_counter()+2.,
        max_families=2,pool_limit=4,stats=selector_stats())
    all_records,all_stats,all_selection=catalog(problem,base,64)
    selected,stats,selection=catalog(problem,base,3)
    single,_,_=catalog(problem,base,1)
    assert single==legacy==selected[:2]
    assert len(all_records)==128 and len(selected)==6
    assert [r["station_id"] for r in selected]==[9,10,9,10,9,10]
    assert all_stats["selector_six_task_sets_ranked"]==stats["selector_six_task_sets_ranked"]==128
    assert all_stats["selector_geometry_maps_ranked"]==stats["selector_geometry_maps_ranked"]==8*128
    assert selection["families_retained"]==6 and all_selection["families_retained"]==128
    identity=lambda r:(r["station_id"],tuple(r["aircraft"]),tuple(tuple(p) for p in r["source_task_groups"]))
    assert len({identity(r) for r in all_records})==128
    for station in (9,10):
        whole=sorted((r for r in all_records if r["station_id"]==station),key=lambda r:r["priority"])
        assert [r for r in selected if r["station_id"]==station]==whole[:3]
        for record in whole:
            assert len(set(record["task_ids"]))==6
            assert all(all((t-1)//4==a for t in pair) for a,pair in zip(record["aircraft"],record["source_task_groups"]))


@pytest.mark.parametrize("arm",[False,True])
def test_real_triangle_outputs_and_every_clock_audit_unchanged_with_expanded_bound(arm):
    problem=fixture();base=original(problem)
    geometry=topology(problem);original_selector=method.frozen._select_families
    baseline=method.frozen.refine(problem,base,deadline=perf_counter()+2.,weights=W,scales=S,
        seed=2026101512,relay_enabled=arm)
    result=method.refine(problem,base,deadline=perf_counter()+2.,weights=W,scales=S,relay_enabled=arm)
    assert result["routes"]==baseline["routes"] and result["evaluation"].score==baseline["evaluation"].score
    assert result["metadata"]["selected_families"]==baseline["metadata"]["selected_families"]
    assert result["metadata"]["selector_sha256"]==baseline["metadata"]["selector_sha256"]
    assert result["metadata"]["selector_patch_restored"] and result["metadata"]["selector_hook_intact_during_call"]
    assert method.frozen._select_families is original_selector and not method._active
    assert result["metadata"]["configuration"]["max_families"]==4
    assert result["metadata"]["expanded_configuration"]["hard_family_limit"]==256
    assert result["metadata"]["configuration"]["legacy_max_families_semantic"]=="station_limit"
    assert topology(problem)==geometry and "problem" not in result
    audit_snapshots(problem,base,result,arm)
    json.dumps(result["metadata"],allow_nan=False)


def test_larger_shared_selector_hash_all_six_families_and_native_source_audit():
    problem,base=larger_fixture();results=[]
    for arm in (False,True):
        result=method.refine(problem,base,deadline=perf_counter()+3.,weights=W,scales=S,
            relay_enabled=arm,max_stations=2,families_per_station=3)
        results.append(result)
        assert result["metadata"]["selector_completed"] and len(result["metadata"]["selected_families"])==6
        assert result["metadata"]["families_examined"]==6
        assert len(result["metadata"]["full_candidate_snapshots"])==12
        assert result["metadata"]["expanded_configuration"]["hard_family_limit"]==6
        audit_snapshots(problem,base,result,arm)
    direct,relay=(r["metadata"] for r in results)
    assert direct["selected_families"]==relay["selected_families"] and direct["selector_sha256"]==relay["selector_sha256"]
    assert direct["expanded_configuration"]==relay["expanded_configuration"]


def test_nested_midcall_overwrite_and_inner_error_restore_only_patched_hook(monkeypatch):
    problem=fixture();base=original(problem);original_selector=method.frozen._select_families
    def fail(*args,**kwargs):
        assert method.frozen._select_families is not original_selector
        with pytest.raises(RuntimeError,match="nested"):
            method.refine(problem,base,deadline=perf_counter()+1.,weights=W,scales=S)
        raise RuntimeError("controlled expanded selector facade failure")
    with monkeypatch.context() as m:
        m.setattr(method.frozen,"refine",fail)
        with pytest.raises(RuntimeError,match="controlled"):
            method.refine(problem,base,deadline=perf_counter()+1.,weights=W,scales=S)
    assert method.frozen._select_families is original_selector and not method._active
    def overwrite(*args,**kwargs):
        method.frozen._select_families=lambda *a,**k:[]
        return {}
    with monkeypatch.context() as m:
        m.setattr(method.frozen,"refine",overwrite)
        with pytest.raises(RuntimeError,match="hook was changed"):
            method.refine(problem,base,deadline=perf_counter()+1.,weights=W,scales=S)
    assert method.frozen._select_families is original_selector and not method._active


def test_expired_or_mid_selector_deadline_never_returns_partial_family_catalog(monkeypatch):
    problem,base=larger_fixture();original_selector=method.frozen._select_families
    expired=method.refine(problem,base,deadline=perf_counter()-1.,weights=W,scales=S)
    assert expired["routes"]==base and not expired["metadata"]["selector_completed"]
    clock=[0.];original_cost=method.frozen.costs.route_cost
    monkeypatch.setattr(method.frozen,"perf_counter",lambda:clock[0])
    monkeypatch.setattr(method.frozen.costs,"perf_counter",lambda:clock[0])
    def timed(p,route,a,**kwargs):
        record=original_cost(p,route,a,**kwargs)
        if len(route)<len(base[a]):clock[0]=2.
        return record
    monkeypatch.setattr(method.frozen.costs,"route_cost",timed)
    result=method.refine(problem,base,deadline=1.,weights=W,scales=S)
    assert result["routes"]==base and result["metadata"]["deadline_reached"]
    assert not result["metadata"]["selector_completed"] and result["metadata"]["selected_families"]==[]
    assert not result["metadata"]["expanded_selector_statistics"]["catalog_returned"]
    assert result["metadata"]["expanded_selector_statistics"]["deadline_reached"]
    assert method.frozen._select_families is original_selector and not method._active


def test_round_robin_timeout_discards_an_already_queued_two_station_prefix(monkeypatch):
    problem,base=larger_fixture();clock=[0.];scope=[None];round_checks=[0]
    original_select=method._expanded_select;original_check=method.frozen._check
    def observed(*args,**kwargs):
        scope[0]=kwargs["selection_stats"]
        return original_select(*args,**kwargs)
    def timed(deadline,stats):
        if scope[0] is not None and len(scope[0]["stations"])==2:
            round_checks[0]+=1
            if round_checks[0]>=3:clock[0]=2.
        return original_check(deadline,stats)
    monkeypatch.setattr(method,"_expanded_select",observed)
    monkeypatch.setattr(method.frozen,"_check",timed)
    monkeypatch.setattr(method.frozen,"perf_counter",lambda:clock[0])
    monkeypatch.setattr(method.frozen.costs,"perf_counter",lambda:clock[0])
    result=method.refine(problem,base,deadline=1.,weights=W,scales=S,max_stations=2,families_per_station=3)
    assert round_checks[0]==3 and len(scope[0]["stations"])==2
    assert not scope[0]["catalog_returned"] and scope[0]["deadline_reached"]
    assert result["metadata"]["selected_families"]==[] and not result["metadata"]["selector_completed"]
    assert result["routes"]==base and result["metadata"]["selector_patch_restored"]


@pytest.mark.parametrize("change",[dict(max_stations=True),dict(max_stations=5),dict(families_per_station=0),
    dict(families_per_station=65)])
def test_invalid_retention_limits_cannot_patch_frozen_selector(change):
    problem=fixture();original_selector=method.frozen._select_families
    with pytest.raises(ValueError):
        method.refine(problem,original(problem),deadline=perf_counter()+1.,weights=W,scales=S,**change)
    assert method.frozen._select_families is original_selector and not method._active
