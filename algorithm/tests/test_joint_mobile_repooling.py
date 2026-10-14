"""Joint layout facade guards and small real zero-cache contact replay."""
from contextlib import contextmanager
import json
from time import perf_counter

import pytest

from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import joint_mobile_repooling as joint
from algorithm.tests.test_mobile_repooling_refinement import mobile_fixture,WEIGHTS,SCALES


def options(**kwargs):
    return dict(dict(max_groups=2,option_count=4,beam_width=8,full_candidates_per_group=8,
        raw_spec_limit=16,point_limit=12,pattern_rounds=2,point_eval_limit=40),**kwargs)


def run(problem,routes,**kwargs):
    return joint.refine(problem,routes,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,**options(**kwargs))


def functions():
    return {name:getattr(joint.frozen.repool,name) for name in joint.PATCH_NAMES}


def test_real_joint_layout_mobile_contact_is_complete_no_cache_and_source_owned():
    from algorithm.experiments.audit_mobile_repooling import audit_mobile_repooling_routes,audit_mobile_repooling_payload
    from algorithm.tests.test_audit_mobile_relay import canonical_payload
    problem,routes=mobile_fixture()
    original=functions()
    original_topology=joint.frozen.mobile.topology(problem)
    result=run(problem,routes)
    assert result["evaluation"].valid and result["evaluation"].score.distance_km<=42.
    assert result["metadata"]["final_cross_uav_count"]==2
    assert result["metadata"]["joint_layout_patches_restored"]
    assert joint.frozen.mobile.topology(problem)==original_topology
    assert functions()==original and not joint.layouts._active
    statistics=result["metadata"]["joint_layout_statistics"]
    assert statistics["lower_bound_evaluations"]>0
    assert statistics["physical_station_heads_retained"]>0
    assert statistics["mobile_family_retained"]>0
    assert statistics["configuration"]["state_physical_keys_unchanged"]
    assert not statistics["configuration"]["partial_bounds_used_for_full_incumbent_pruning"]
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    old=joint.frozen.mobile.topology(problem)
    snapshots=result["metadata"]["full_candidate_snapshots"]+result["metadata"]["raw_stage"]["full_candidate_snapshots"]
    for snapshot in snapshots:
        audit=audit_mobile_repooling_routes(tasks,old,snapshot["topology"],snapshot["routes"],
            base_routes=routes,move=snapshot["move"])
        assert audit["passed"] and audit["source_responsibility_proven"]
        for key in snapshot["score"]:
            assert audit["score"][key]==pytest.approx(snapshot["score"][key])
        payload=canonical_payload(tasks,snapshot["topology"],snapshot["routes"],
            {"mobile_meetings":snapshot["mobile_meetings"]})
        assert audit_mobile_repooling_payload(tasks,old,snapshot["topology"],payload,
            base_routes=routes,move=snapshot["move"])["passed"]
    json.dumps(result["metadata"],allow_nan=False)


def test_direct_control_matches_unpatched_frozen_physical_rank_and_common_selector():
    problem,routes=mobile_fixture()
    baseline=joint.frozen.refine(problem,routes,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,
        seed=2026101506,relay_enabled=False,proposal_fraction=.55,**options())
    direct=run(problem,routes,relay_enabled=False)
    relay=run(problem,routes)
    assert direct["routes"]==baseline["routes"] and direct["evaluation"].score==baseline["evaluation"].score
    assert "problem" not in direct and direct["metadata"]["final_cross_uav_count"]==0
    stats=direct["metadata"]["joint_layout_statistics"]
    assert stats["direct_rebuild_calls"]>0 and stats["direct_delegations"]>0
    assert stats["lower_bound_evaluations"]==0 and stats["receiver_rank_calls"]==0
    assert direct["metadata"]["selected_groups"]==relay["metadata"]["selected_groups"]
    assert direct["metadata"]["selector_sha256"]==relay["metadata"]["selector_sha256"]
    assert direct["metadata"]["joint_layout_parameters"]==relay["metadata"]["joint_layout_parameters"]
    assert relay["evaluation"].score.distance_km<direct["evaluation"].score.distance_km


def test_process_local_exception_and_nested_context_restore_every_patch(monkeypatch):
    problem,routes=mobile_fixture()
    original=functions()
    def failure(*a,**k):
        assert functions()!=original
        raise RuntimeError("controlled driver failure")
    monkeypatch.setattr(joint.frozen,"refine",failure)
    with pytest.raises(RuntimeError,match="controlled driver"):
        run(problem,routes)
    assert functions()==original and not joint.layouts._active
    with joint.layouts.joint_layouts(joint.frozen.repool,problem,weights=WEIGHTS,scales=SCALES,
                                   deadline=perf_counter()+1.):
        patched=functions()
        with pytest.raises(RuntimeError,match="nested"):
            run(problem,routes)
        assert functions()==patched
    assert functions()==original and not joint.layouts._active


def test_facade_passes_exact_default_bounds_module_deadline_and_preserves_metadata(monkeypatch):
    problem,routes=mobile_fixture()
    seen={}
    marker=[]
    @contextmanager
    def context(module,p,**kwargs):
        seen["module"],seen["context"]=(module,p),kwargs
        kwargs["statistics"]["sentinel"]=marker
        yield kwargs["statistics"]
    def frozen(p,r,**kwargs):
        seen["call"]=kwargs
        return dict(routes=r,evaluation=evaluate_solution(p,r),iterations=0,
            metadata=dict(method="original_frozen_method",input_snapshot={"sentinel":1},
                          full_candidate_snapshots=marker,configuration={"preserved":True}))
    monkeypatch.setattr(joint.layouts,"joint_layouts",context)
    monkeypatch.setattr(joint.frozen,"refine",frozen)
    result=joint.refine(problem,routes,deadline=perf_counter()+1.,weights=WEIGHTS,scales=SCALES)
    assert seen["module"]==(joint.frozen.repool,problem)
    assert seen["context"]["deadline"]==seen["call"]["deadline"]
    assert [seen["call"][k] for k in ("max_groups","option_count","beam_width","full_candidates_per_group","raw_spec_limit")]==[8,4,8,8,64]
    assert seen["call"]["proposal_fraction"]==.55
    assert result["metadata"]["full_candidate_snapshots"] is marker
    assert result["metadata"]["configuration"]=={"preserved":True}
    assert result["metadata"]["frozen_driver_method"]=="original_frozen_method"


def test_expired_budget_retains_original_resource_model_and_no_contact():
    problem,routes=mobile_fixture()
    original=functions()
    result=joint.refine(problem,routes,deadline=perf_counter()-1.,weights=WEIGHTS,scales=SCALES)
    assert result["problem"] is problem and result["routes"]==routes
    assert result["metadata"]["accepted_improvements"]==0
    assert result["metadata"]["deadline_reached"]
    assert functions()==original


@pytest.mark.parametrize("kwargs",[dict(beam_width=9),dict(full_candidates_per_group=9),dict(beam_width=True),dict(relay_enabled=1)])
def test_invalid_joint_widths_are_rejected_before_context_patch(kwargs):
    problem,routes=mobile_fixture()
    original=functions()
    with pytest.raises(ValueError):run(problem,routes,**kwargs)
    assert functions()==original
