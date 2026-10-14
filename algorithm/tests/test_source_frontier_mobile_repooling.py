"""Source coverage, exact memo controls and independently audited mobile leaves."""
from dataclasses import replace
from time import perf_counter
import json

import pytest

from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import source_frontier_mobile_repooling as driver
from algorithm.tests.test_mobile_repooling_refinement import mobile_fixture,WEIGHTS,SCALES


def parameters(**kwargs):
    return dict(dict(max_groups=2,option_count=4,beam_width=8,full_candidates_per_group=8,
        raw_spec_limit=16,point_limit=12,pattern_rounds=2,point_eval_limit=40),**kwargs)


def run(problem,routes,**kwargs):
    return driver.refine(problem,routes,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,**parameters(**kwargs))


def originals():
    names=set(driver.joint.PATCH_NAMES)|set(driver.frontier.PATCH_NAMES)
    return {name:getattr(driver.joint.frozen.repool,name) for name in names}


def audit_snapshots(problem,base,result):
    from algorithm.experiments.audit_mobile_repooling import audit_mobile_repooling_routes,audit_mobile_repooling_payload
    from algorithm.tests.test_audit_mobile_relay import canonical_payload
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    old=driver.joint.frozen.mobile.topology(problem)
    meta=result["metadata"]
    snapshots=meta["full_candidate_snapshots"]+meta.get("raw_stage",{}).get("full_candidate_snapshots",[])
    for snapshot in snapshots:
        current=snapshot.get("topology",old)
        audit=audit_mobile_repooling_routes(tasks,old,current,snapshot["routes"],base_routes=base,move=snapshot["move"])
        assert audit["passed"] and audit["source_responsibility_proven"]
        for key in snapshot["score"]:
            assert audit["score"][key]==pytest.approx(snapshot["score"][key])
        payload=canonical_payload(tasks,current,snapshot["routes"],{"mobile_meetings":snapshot.get("mobile_meetings",[])})
        assert audit_mobile_repooling_payload(tasks,old,current,payload,base_routes=base,move=snapshot["move"])["passed"]


def test_real_mobile_pipeline_and_both_exact_memos_leave_geometry_and_functions_unchanged():
    problem,base=mobile_fixture()
    old=driver.joint.frozen.mobile.topology(problem)
    functions=originals()
    result=run(problem,base)
    assert result["evaluation"].valid and result["evaluation"].score.distance_km<=42.
    assert result["metadata"]["final_cross_uav_count"]==2
    assert result["metadata"]["mobile_meetings"][0]["ground_cache_min"]==0.
    stats=result["metadata"]["source_frontier_statistics"]
    assert stats["source_actual_calls"]>0 and stats["source_memo_hits"]>0
    assert stats["partial_memo_hits"]>0 and stats["receiver_memo_hits"]>0
    assert stats["native_physical_best_retained"]==stats["earliest_drop_retained"]
    assert stats["configuration"]["delta_fields_unchanged"]
    assert not stats["configuration"]["partial_geometry_pruning"]
    assert result["metadata"]["process_local_patches_restored"] and len(functions)==5
    assert originals()==functions and not driver.frontier._active and not driver.joint.layouts._active
    assert driver.joint.frozen.mobile.topology(problem)==old
    audit_snapshots(problem,base,result)
    json.dumps(result["metadata"],allow_nan=False)


def test_direct_same_limits_exactly_matches_unpatched_frozen_physical_control_and_selector():
    problem,base=mobile_fixture()
    baseline=driver.joint.frozen.refine(problem,base,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,
        seed=2026101507,relay_enabled=False,proposal_fraction=.55,**parameters())
    direct=run(problem,base,relay_enabled=False)
    relay=run(problem,base)
    assert direct["routes"]==baseline["routes"] and direct["evaluation"].score==baseline["evaluation"].score
    assert "problem" not in direct
    meta=direct["metadata"]
    assert meta["source_frontier_statistics"]["source_actual_calls"]==0
    assert meta["source_frontier_statistics"]["partial_memo_hits"]>0
    assert meta["joint_layout_statistics"]["lower_bound_evaluations"]==0
    assert meta["selected_groups"]==relay["metadata"]["selected_groups"]
    assert meta["selector_sha256"]==relay["metadata"]["selector_sha256"]
    assert meta["source_frontier_parameters"]==relay["metadata"]["source_frontier_parameters"]
    audit_snapshots(problem,base,direct)


def test_actual_early_source_outside_late_native_shortlist_reaches_audited_mobile_leaf():
    from algorithm.tests.test_joint_mobile_layouts import early_fixture
    problem,base,g,_=early_fixture()
    problem=replace(problem,tasks=tuple(replace(t,deadline_min=35.) if t.id in (1,2) else
        replace(t,deadline_min=15.) if t.id==3 else t for t in problem.tasks))
    # Same frozen official weighted scales, although this deterministic
    # physical fixture has seven requests rather than the official 200.
    scales=(200.,4464.183540734935,775.7731679286865)
    selected=[driver.joint.frozen.repool._group_record(g)]
    result=driver.refine(problem,base,deadline=perf_counter()+2.,weights=WEIGHTS,scales=scales,
        common_selector_records=selected,**parameters(max_groups=1))
    assert result["evaluation"].valid
    records=result["metadata"]["source_frontier_statistics"]["source_records"]
    early=[r for r in records if r["native_best_drop_min"]>r["earliest_drop_min"]]
    assert early and any(r["native_best_drop_min"]==50. and r["earliest_drop_min"]==30. for r in early)
    assert all(any(o["end_min"]==r["earliest_drop_min"] for o in r["selected"]) for r in early)
    assert result["metadata"]["full_candidate_snapshots"]
    assert result["metadata"]["mobile_meetings"] and result["metadata"]["final_cross_uav_count"]==2
    assert result["metadata"]["final_key"]<result["metadata"]["initial_key"]
    audit_snapshots(problem,base,result)


def test_nested_context_exception_restores_all_five_hooks(monkeypatch):
    problem,base=mobile_fixture()
    functions=originals()
    def fail(*a,**k):
        assert originals()!=functions
        raise RuntimeError("controlled source-frontier facade error")
    monkeypatch.setattr(driver.joint.frozen,"refine",fail)
    with pytest.raises(RuntimeError,match="controlled"):
        run(problem,base)
    assert originals()==functions and not driver.frontier._active and not driver.joint.layouts._active


def test_expired_budget_retains_checked_original_without_new_resources():
    problem,base=mobile_fixture()
    functions=originals()
    result=driver.refine(problem,base,deadline=perf_counter()-1.,weights=WEIGHTS,scales=SCALES)
    assert result["problem"] is problem and result["routes"]==base
    assert result["metadata"]["accepted_improvements"]==0 and result["metadata"]["deadline_reached"]
    assert originals()==functions


@pytest.mark.parametrize("kwargs",[dict(source_surface_limit=3),dict(memo_limit=0),dict(source_surface_limit=True),dict(beam_width=9)])
def test_invalid_finite_bounds_do_not_open_context(kwargs):
    problem,base=mobile_fixture()
    functions=originals()
    with pytest.raises(ValueError):run(problem,base,**kwargs)
    assert originals()==functions
