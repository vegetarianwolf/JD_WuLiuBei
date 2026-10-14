"""End-to-end receiver memo, unchanged candidates, full source-bound audit."""
from time import perf_counter
import json

import pytest

from algorithm.experiments import receiver_bundle_cached_mobile as driver
from algorithm.tests.test_mobile_repooling_refinement import mobile_fixture,WEIGHTS,SCALES
from algorithm.tests.test_source_frontier_mobile_repooling import audit_snapshots


def parameters(**kwargs):
    return dict(dict(max_groups=2,option_count=4,beam_width=8,full_candidates_per_group=8,
        raw_spec_limit=16,point_limit=12,pattern_rounds=2,point_eval_limit=40),**kwargs)


def run(problem,routes,**kwargs):
    return driver.refine(problem,routes,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,**parameters(**kwargs))


def functions():
    names=set(driver.source.joint.PATCH_NAMES)|set(driver.source.frontier.PATCH_NAMES)|set(driver.bundles.PATCH_NAMES)
    return {name:getattr(driver.source.joint.frozen.repool,name) for name in names}


@pytest.mark.parametrize("relay_enabled",[False,True])
def test_cache_preserves_complete_request_outputs_and_source_audited_new_point(relay_enabled):
    problem,base=mobile_fixture()
    old=driver.source.joint.frozen.mobile.topology(problem)
    original=functions()
    baseline=driver.source.refine(problem,base,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,
        seed=2026101508,relay_enabled=relay_enabled,**parameters())
    result=run(problem,base,relay_enabled=relay_enabled)
    assert result["routes"]==baseline["routes"] and result["evaluation"].score==baseline["evaluation"].score
    assert result["metadata"]["mobile_meetings"]==baseline["metadata"]["mobile_meetings"]
    raw=lambda outcome:outcome["metadata"].get("raw_stage",outcome["metadata"])
    snapshot_keys=lambda outcome:[(s["routes"],s["key"]) for s in raw(outcome)["full_candidate_snapshots"]]
    assert snapshot_keys(result)==snapshot_keys(baseline)
    assert result["metadata"]["selector_sha256"]==baseline["metadata"]["selector_sha256"]
    statistics=result["metadata"]["receiver_bundle_statistics"]
    kind="relay" if relay_enabled else "direct"
    assert statistics[kind+"_cache_hits"]>0
    assert statistics[kind+"_actual_calls"]<statistics[kind+"_logical_calls"]
    assert statistics[kind+"_logical_calls"]==statistics[kind+"_actual_calls"]+statistics[kind+"_cache_hits"]
    if relay_enabled:
        assert result["evaluation"].score.distance_km<=42.
        assert result["metadata"]["final_cross_uav_count"]==2
        assert result["metadata"]["mobile_meetings"][0]["ground_cache_min"]==0.
        assert raw(result)["receiver_relay_layouts"]<raw(baseline)["receiver_relay_layouts"]
    else:
        assert result["metadata"]["final_cross_uav_count"]==0 and "problem" not in result
    assert len(original)==7 and len(result["metadata"]["process_local_patch_names"])==7
    assert functions()==original and result["metadata"]["process_local_patches_restored"]
    assert driver.source.joint.frozen.mobile.topology(problem)==old
    audit_snapshots(problem,base,result)
    json.dumps(result["metadata"],allow_nan=False)


def test_both_arms_keep_same_selector_limits_and_memo_bounds():
    problem,base=mobile_fixture()
    direct=run(problem,base,relay_enabled=False)
    relay=run(problem,base)
    assert direct["metadata"]["selected_groups"]==relay["metadata"]["selected_groups"]
    assert direct["metadata"]["selector_sha256"]==relay["metadata"]["selector_sha256"]
    assert direct["metadata"]["receiver_bundle_parameters"]==relay["metadata"]["receiver_bundle_parameters"]
    assert direct["metadata"]["receiver_bundle_parameters"]["receiver_bundle_memo_limit"]==8192


def test_receiver_cache_hit_occurs_before_repeated_candidate_enumeration(monkeypatch):
    problem,base=mobile_fixture()
    module=driver.source.joint.frozen.repool
    original=module._receiver_relay
    actual=[]
    def observed(*args,**kwargs):
        actual.append(tuple(args[1].routes[args[2].c]))
        return original(*args,**kwargs)
    monkeypatch.setattr(module,"_receiver_relay",observed)
    result=run(problem,base)
    stats=result["metadata"]["receiver_bundle_statistics"]
    assert len(actual)==stats["relay_actual_calls"]
    assert stats["relay_logical_calls"]>len(actual) and stats["relay_cache_hits"]>0
    assert module._receiver_relay is observed


def test_error_and_nested_receiver_context_restore_all_seven_hooks(monkeypatch):
    problem,base=mobile_fixture()
    original=functions()
    def fail(*a,**k):
        assert functions()!=original
        raise RuntimeError("controlled innermost failure")
    monkeypatch.setattr(driver.source.joint.frozen,"refine",fail)
    with pytest.raises(RuntimeError,match="controlled"):
        run(problem,base)
    assert functions()==original
    assert not driver.bundles._active and not driver.source.frontier._active and not driver.source.joint.layouts._active
    with driver.bundles.receiver_bundle_cache(driver.source.joint.frozen.repool,problem,
            weights=WEIGHTS,scales=SCALES,deadline=perf_counter()+1.):
        patched=functions()
        with pytest.raises(RuntimeError,match="nested"):
            run(problem,base)
        assert functions()==patched
    assert functions()==original and not driver.bundles._active


def test_expired_budget_retains_checked_original_and_original_geometry():
    problem,base=mobile_fixture()
    original=functions()
    result=driver.refine(problem,base,deadline=perf_counter()-1.,weights=WEIGHTS,scales=SCALES)
    assert result["routes"]==base and result["problem"] is problem
    assert result["metadata"]["deadline_reached"] and result["metadata"]["accepted_improvements"]==0
    assert functions()==original


@pytest.mark.parametrize("limit",[0,-1,True])
def test_invalid_receiver_memo_limit_does_not_open_any_context(limit):
    problem,base=mobile_fixture()
    original=functions()
    with pytest.raises(ValueError):run(problem,base,receiver_bundle_memo_limit=limit)
    assert functions()==original
