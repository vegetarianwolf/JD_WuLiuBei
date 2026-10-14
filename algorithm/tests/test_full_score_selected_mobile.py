"""Shared full-score selection, restored hooks and source-bound mobile replay."""
from hashlib import sha256
from time import perf_counter
import json

import pytest

from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import full_score_selected_mobile as driver
from algorithm.tests.test_mobile_repooling_refinement import mobile_fixture,WEIGHTS,SCALES
from algorithm.tests.test_source_frontier_mobile_repooling import audit_snapshots


def parameters(**kwargs):
    return dict(dict(max_groups=2,option_count=4,beam_width=8,full_candidates_per_group=8,
        raw_spec_limit=16,point_limit=12,pattern_rounds=2,point_eval_limit=40),**kwargs)


def run(problem,routes,**kwargs):
    return driver.refine(problem,routes,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,
                         **parameters(**kwargs))


def originals():
    names=set(driver.cached.source.joint.PATCH_NAMES)|set(driver.cached.source.frontier.PATCH_NAMES)
    names|=set(driver.cached.bundles.PATCH_NAMES)|set(driver.selection.PATCH_NAMES)
    module=driver.cached.source.joint.frozen.repool
    return {name:getattr(module,name) for name in names}


@pytest.mark.parametrize("relay_enabled",[False,True])
def test_full_selector_real_pipeline_all_raw_and_mobile_snapshots_independently_replayed(relay_enabled):
    problem,base=mobile_fixture()
    mobile=driver.cached.source.joint.frozen.mobile
    old=mobile.topology(problem)
    functions=originals()
    result=run(problem,base,relay_enabled=relay_enabled)
    meta=result["metadata"];stats=meta["full_score_selector_statistics"]
    assert result["evaluation"].valid
    assert meta["final_key"]<=meta["initial_key"]
    assert stats["actual_selectors"]==1 and stats["selector_completed"]
    assert stats["selected_groups"]==meta["selected_groups"]
    assert stats["selector_sha256"]==meta["selector_sha256"]
    assert stats["pair_removal_calls"]>0 and stats["directed_pairs_scored"]>0
    assert stats["patches_restored"] and meta["process_local_patches_restored"]
    assert not meta["full_score_selector_records_reused"]
    assert len(functions)==8 and len(meta["process_local_patch_names"])==8
    assert originals()==functions and mobile.topology(problem)==old
    if relay_enabled:
        assert result["evaluation"].score.distance_km<=42.
        assert meta["final_cross_uav_count"]==2 and meta["full_candidate_snapshots"]
        meeting=meta["mobile_meetings"][0]
        assert meeting["ground_cache_min"]==0. and meeting["shared_double_loaded_km"]>0.
        assert meeting["arrival_times_min"][2]<=min(meeting["arrival_times_min"][:2])
        assert result["problem"].home_points()==problem.home_points()
        assert result["problem"].relay_stations[:-1]==problem.relay_stations
    else:
        assert "problem" not in result and meta["final_cross_uav_count"]==0
        assert meta["source_frontier_statistics"]["source_actual_calls"]==0
        assert meta["joint_layout_statistics"]["lower_bound_evaluations"]==0
    audit_snapshots(problem,base,result)
    json.dumps(meta,allow_nan=False)


def test_both_arms_share_completed_selector_hash_bounds_and_real_input_guard():
    problem,base=mobile_fixture()
    direct=run(problem,base,relay_enabled=False)
    relay=run(problem,base)
    dm,rm=direct["metadata"],relay["metadata"]
    assert dm["selected_groups"]==rm["selected_groups"]
    assert dm["selector_sha256"]==rm["selector_sha256"]
    digest=sha256(json.dumps(rm["selected_groups"],sort_keys=True,separators=(",",":")).encode()).hexdigest()
    assert digest==rm["selector_sha256"]
    assert dm["full_score_selector_parameters"]==rm["full_score_selector_parameters"]
    bounds=rm["full_score_selector_parameters"]
    assert bounds["compensation_geometry_pool_limit"]==bounds["compensation_removal_pool_limit"]==6
    assert bounds["source_surface_limit"]==512
    assert bounds["receiver_bundle_memo_limit"]==bounds["memo_limit"]==8192
    assert bounds["physical_keys_and_deltas_unchanged"] and not bounds["partial_incumbent_pruning"]
    assert direct["evaluation"].score==evaluate_solution(problem,direct["routes"],_force_full=True).score
    assert relay["evaluation"].score.distance_km<direct["evaluation"].score.distance_km


def test_reused_selector_is_explicit_and_exact_direct_control_unchanged_after_selection():
    problem,base=mobile_fixture()
    selected=run(problem,base,relay_enabled=False)["metadata"]["selected_groups"]
    result=run(problem,base,relay_enabled=False,common_selector_records=selected)
    plain=driver.cached.refine(problem,base,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,
        seed=2026101509,relay_enabled=False,common_selector_records=selected,**parameters())
    assert result["routes"]==plain["routes"] and result["evaluation"].score==plain["evaluation"].score
    assert result["metadata"]["full_candidate_snapshots"]==plain["metadata"]["full_candidate_snapshots"]
    assert result["metadata"]["full_score_selector_records_reused"]
    stats=result["metadata"]["full_score_selector_statistics"]
    assert stats["logical_calls"]==stats["actual_selectors"]==0
    assert not stats["selector_completed"]
    assert result["metadata"]["selector_completed"]
    audit_snapshots(problem,base,result)


def test_all_eight_hooks_restore_after_innermost_error_and_nested_selector(monkeypatch):
    problem,base=mobile_fixture()
    functions=originals()
    def fail(*args,**kwargs):
        assert originals()!=functions
        raise RuntimeError("controlled innermost full-score facade error")
    monkeypatch.setattr(driver.cached.source.joint.frozen,"refine",fail)
    with pytest.raises(RuntimeError,match="controlled"):
        run(problem,base)
    assert originals()==functions
    assert not driver.selection._active and not driver.cached.bundles._active
    assert not driver.cached.source.frontier._active and not driver.cached.source.joint.layouts._active
    with driver.selection.full_score_selector(driver.cached.source.joint.frozen.repool,problem,
            deadline=perf_counter()+1.,weights=WEIGHTS,scales=SCALES):
        patched=originals()
        with pytest.raises(RuntimeError,match="nested"):
            run(problem,base)
        assert originals()==patched
    assert originals()==functions and not driver.selection._active


def test_expired_budget_never_runs_selector_or_acquires_new_resources():
    problem,base=mobile_fixture();functions=originals()
    result=driver.refine(problem,base,deadline=perf_counter()-1.,weights=WEIGHTS,scales=SCALES)
    assert result["routes"]==base and result["problem"] is problem
    meta=result["metadata"]
    assert meta["deadline_reached"] and meta["accepted_improvements"]==0
    assert not meta["selector_completed"] and not meta["full_score_selector_statistics"]["selector_completed"]
    assert meta["selected_groups"]==[] and meta["selector_sha256"]==sha256(b"[]").hexdigest()
    assert originals()==functions


@pytest.mark.parametrize("kwargs",[dict(compensation_geometry_pool_limit=0),
    dict(compensation_removal_pool_limit=True),dict(compensation_removal_pool_limit=33)])
def test_invalid_selector_limits_fail_before_any_hooks_open(kwargs):
    problem,base=mobile_fixture();functions=originals()
    with pytest.raises(ValueError):run(problem,base,**kwargs)
    assert originals()==functions and not driver.selection._active
