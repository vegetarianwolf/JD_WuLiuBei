"""Real ordinary-task repair after six pins, plus facade budget/fallback guards."""
from copy import deepcopy
from dataclasses import replace
from time import perf_counter
import json

import pytest

from uav_dispatch import Point,Task,alns
from uav_dispatch.relay import build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import six_task_with_pinned_polish as method
from algorithm.tests.test_six_task_route_costs import fixture as triangle,W,S
from algorithm.tests.test_six_task_sorting import audit_snapshots as audit_raw
from algorithm.tests.test_relay_repooling_exchange import topology
from algorithm.tests.test_repooling_with_pinned_polish import transformed_runtime


def fixture():
    problem=triangle();homes=problem.home_points()
    tasks=tuple(replace(t,deadline_min=100.) for t in problem.tasks)+tuple(
        Task(7+a,homes[(a+1)%3],homes[(a+1)%3],100.) for a in range(3))
    problem=replace(problem,tasks=tasks,max_tasks_per_drone=3,
        task_relay_candidates={**problem.task_relay_candidates,7:(),8:(),9:()})
    base=tuple((7+a,-(7+a),2*a+1,2*a+2,-(2*a+1),-(2*a+2)) for a in range(3))
    assert evaluate_solution(problem,base,_force_full=True).valid
    return problem,base


def config(module):
    return module.ALNSConfig(relay_min_destroy_fraction=.34,relay_max_destroy_fraction=.34,
        repair_rank_candidate_limit=12,repair_exact_candidate_limit=12,direct_exact_top_k=4)


def raw_source(problem,base,arm=True):
    return method.raw_method.refine(problem,base,deadline=perf_counter()+2.,weights=W,scales=S,
        relay_enabled=arm,max_stations=1,families_per_station=1,seed=2026101513)


def run(problem,base,module,**kwargs):
    return method.refine(problem,base,deadline=perf_counter()+2.,weights=W,scales=S,alns_module=module,
        polish_config=config(module),polish_max_iterations=2,max_stations=1,families_per_station=1,**kwargs)


def audit_all(problem,base,result,arm):
    from algorithm.experiments.audit_six_task_pinned_polish import audit_six_task_pinned_polish_proposal
    from algorithm.experiments.audit_weighted_relay_ablation import audit_payload
    from algorithm.tests.test_audit_mobile_relay import canonical_payload
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    graph=topology(problem);graph["task_relay_candidates"]={str(t):list(problem.task_relay_candidates.get(t,())) for t in problem.task_ids}
    audit_raw(problem,base,dict(metadata=result["metadata"]["raw_stage"]),arm)
    for record in result["metadata"]["polish_records"]:
        helper=record["helper_metadata"]
        snapshots=[record["post_polish"]]+helper.get("full_candidate_snapshots",[])
        snapshots += [helper.get(k) for k in ("input_snapshot","first_complete_candidate","best_constructed_candidate","post_pin_snapshot")]
        for snapshot in filter(None,snapshots):
            proof=audit_six_task_pinned_polish_proposal(tasks,graph,base,record["macro_routes"],snapshot["routes"],
                record["original_macro_move"],relay_enabled=arm,telemetry=snapshot,weights=W,scales=S)
            assert proof["passed"] and proof["actual_cross_uav_count"]==(6 if arm else 0)
            assert "table_score" not in snapshot and snapshot["move"]=={"semantic":"post_six_task_pinned_polish"}
            audit_payload(tasks,graph,canonical_payload(tasks,graph,snapshot["routes"],{}))


@pytest.mark.parametrize("arm",[False,True])
def test_actual_weighted_nine_task_loops_native_repair_full_source_current_clock_audit_and_restoration(transformed_runtime,arm):
    problem,base=fixture();module=transformed_runtime
    selector=method.raw_method.frozen._select_families
    functions={n:getattr(module,n) for n in method.helper.PATCH_NAMES}
    objective=(module._weighted_adapter.active,module._weighted_adapter.coefficients)
    geometry=topology(problem)
    result=run(problem,base,module,relay_enabled=arm)
    meta=result["metadata"]
    assert result["evaluation"].valid and "problem" not in result and meta["final_key"]<meta["initial_key"]
    assert meta["polish_calls"]==2 and meta["polish_iterations"]==4
    assert len(meta["full_candidate_snapshots"])==2 and len(meta["raw_stage"]["full_candidate_snapshots"])==2
    for record in meta["polish_records"]:
        h=record["helper_metadata"]
        assert h["pinned_task_ids"]==list(range(1,7))
        assert h["required_cross_task_ids"]==(list(range(1,7)) if arm else [])
        assert h["destroy_calls"]>0 and h["repair_calls"]>0 and h["complete_valid_candidates"]>0
        assert h["direct_exact_global_evaluations"]>0
        for key,value in meta["cold_polish_profile"].items():assert h["configuration"][key]==value
        assert not h["configuration"]["enable_budget_cooling"] and record["finished_within_leaf_deadline"]
    if arm:
        assert meta["final_cross_uav_count"]==6 and any(r["adopted"] for r in meta["polish_records"])
        assert any(any(build_plan_index(problem,s["routes"]).delivery_route_by_task[t]
            !=build_plan_index(problem,r["macro_routes"]).delivery_route_by_task[t] for t in (7,8,9))
            for r in meta["polish_records"] for s in r["helper_metadata"]["full_candidate_snapshots"])
    assert meta["raw_selector_patch_restored"] and meta["pinned_alns_patches_restored"]
    assert meta["weighted_objective_state_restored"] and not method._active and not method.helper._active
    assert method.raw_method.frozen._select_families is selector
    assert all(getattr(module,n) is f for n,f in functions.items())
    assert (module._weighted_adapter.active,module._weighted_adapter.coefficients)==objective and topology(problem)==geometry
    audit_all(problem,base,result,arm)
    json.dumps(meta,allow_nan=False)


def test_both_arms_share_selector_profile_and_rule_but_may_choose_different_macros(transformed_runtime):
    problem,base=fixture();module=transformed_runtime
    direct=run(problem,base,module,relay_enabled=False)
    relay=run(problem,base,module)
    dm,rm=direct["metadata"],relay["metadata"]
    assert dm["selected_families"]==rm["selected_families"] and dm["selector_sha256"]==rm["selector_sha256"]
    assert dm["selector_completed"] and rm["selector_completed"]
    assert dm["selector_completed_at_perf_counter"]<dm["proposal_deadline_perf_counter"]
    assert rm["selector_completed_at_perf_counter"]<rm["proposal_deadline_perf_counter"]
    assert dm["configuration"]==rm["configuration"] and dm["cold_polish_profile"]==rm["cold_polish_profile"]
    assert not dm["configuration"]["selected_macro_identities_paired"]


def test_controlled_shared_budget_leaf_reserve_and_original_comparator_forwarded(monkeypatch):
    problem,base=fixture();raw=raw_source(problem,base);clock=[0.];raw_calls=[];helper_calls=[]
    monkeypatch.setattr(method,"perf_counter",lambda:clock[0])
    def propose(*args,**kwargs):
        raw_calls.append(kwargs);clock[0]=kwargs["deadline"]
        return dict(raw,metadata=deepcopy(raw["metadata"]))
    def polish(p,routes,**kwargs):
        helper_calls.append(kwargs);clock[0]=kwargs["deadline"]-.001
        return dict(problem=p,routes=routes,iterations=2,
            metadata=dict(adoptable=False,returned_checkpoint_checked_within_deadline=True))
    monkeypatch.setattr(method.raw_method,"refine",propose)
    monkeypatch.setattr(method.helper,"polish_pinned_macro",polish)
    result=method.refine(problem,base,deadline=40.,weights=W,scales=S,alns_module=alns)
    meta=result["metadata"]
    assert raw_calls[0]["deadline"]==16. and meta["polish_calls"]==2
    assert len(helper_calls)==2 and all(call["proposal_margin"]==.015 for call in helper_calls)
    assert all(tuple(call["comparison_key"])==tuple(meta["initial_key"]) for call in helper_calls)
    assert all(call["pinned_task_ids"]==tuple(range(1,7)) and call["required_cross_task_ids"]==tuple(range(1,7)) for call in helper_calls)
    assert all(call["alns_module"] is alns for call in helper_calls)
    for record in meta["polish_records"]:
        assert record["guard_reserve_seconds"]==.03
        assert record["helper_deadline_perf_counter"]<record["leaf_deadline_perf_counter"]<=40.
        assert record["full_proposal"] in meta["raw_stage"]["full_candidate_snapshots"]
    assert result["routes"]==raw["routes"]


@pytest.mark.parametrize("failure",["worse","invalid","pins","problem","unchecked"])
def test_bad_helper_claim_never_loses_checked_better_raw_macro(monkeypatch,failure):
    problem,base=fixture();raw=raw_source(problem,base)
    monkeypatch.setattr(method.raw_method,"refine",lambda *a,**k:dict(raw,metadata=deepcopy(raw["metadata"])))
    def faulty(p,routes,**kwargs):
        candidate=base if failure=="pins" else ((),(),()) if failure=="invalid" else routes
        return dict(problem=object() if failure=="problem" else p,routes=candidate,iterations=0,
            metadata=dict(adoptable=True,returned_checkpoint_checked_within_deadline=failure!="unchecked"))
    monkeypatch.setattr(method.helper,"polish_pinned_macro",faulty)
    result=method.refine(problem,base,deadline=perf_counter()+2.,weights=W,scales=S,alns_module=alns,
        max_stations=1,families_per_station=1)
    assert result["routes"]==raw["routes"] and not any(r["adopted"] for r in result["metadata"]["polish_records"])
    assert all(r["rejection_reason"] for r in result["metadata"]["polish_records"])
    assert result["metadata"]["accepted_moves"][0]["kind"]=="raw_macro"


def test_expired_budget_and_overrun_during_wrapper_post_check_cannot_adopt(monkeypatch):
    problem,base=fixture()
    result=method.refine(problem,base,deadline=perf_counter()-1.,weights=W,scales=S,alns_module=alns)
    assert result["routes"]==base and result["metadata"]["polish_calls"]==0
    raw=raw_source(problem,base);clock=[0.]
    monkeypatch.setattr(method,"perf_counter",lambda:clock[0])
    monkeypatch.setattr(method.raw_method,"refine",lambda *a,**k:dict(raw,metadata=deepcopy(raw["metadata"])))
    monkeypatch.setattr(method.helper,"polish_pinned_macro",lambda p,r,**k:dict(problem=p,routes=r,iterations=0,
        metadata=dict(adoptable=True,returned_checkpoint_checked_within_deadline=True)))
    validate=method.helper._validate
    def overdue(*args):
        answer=validate(*args);clock[0]=41.;return answer
    monkeypatch.setattr(method.helper,"_validate",overdue)
    result=method.refine(problem,base,deadline=40.,weights=W,scales=S,alns_module=alns)
    assert result["routes"]==raw["routes"] and result["metadata"]["deadline_reached"]
    assert not any(move["kind"]=="six_pinned_polish" for move in result["metadata"]["accepted_moves"])
    assert result["metadata"]["polish_records"][0]["rejection_reason"]=="wrapper_full_check_after_leaf_deadline"


def test_nested_error_restores_selector_actual_alns_and_objective(transformed_runtime,monkeypatch):
    problem,base=fixture();module=transformed_runtime
    selector=method.raw_method.frozen._select_families
    functions={n:getattr(module,n) for n in method.helper.PATCH_NAMES}
    def fail(*a,**k):
        with pytest.raises(RuntimeError,match="nested"):run(problem,base,module)
        raise RuntimeError("controlled six pin raw failure")
    monkeypatch.setattr(method.raw_method,"refine",fail)
    with pytest.raises(RuntimeError,match="controlled"):run(problem,base,module)
    assert method.raw_method.frozen._select_families is selector
    assert all(getattr(module,n) is f for n,f in functions.items())
    assert not method._active and not method.helper._active and not module._weighted_adapter.active


@pytest.mark.parametrize("change",[dict(proposal_fraction=1.),dict(candidate_limit=3),dict(proposal_margin=-.1),
    dict(polish_cooling_rate=1.1),dict(polish_max_iterations=True)])
def test_invalid_budget_and_profile_do_not_open_search(change):
    problem,base=fixture();selector=method.raw_method.frozen._select_families
    with pytest.raises(ValueError):
        method.refine(problem,base,deadline=perf_counter()+1.,weights=W,scales=S,alns_module=alns,**change)
    assert method.raw_method.frozen._select_families is selector and not method._active
