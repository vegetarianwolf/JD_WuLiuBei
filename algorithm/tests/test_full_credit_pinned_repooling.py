"""Real weighted polish after full-credit construction; independent pin audit."""
from hashlib import sha256
from time import perf_counter
import json

import pytest

from uav_dispatch.relay import build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import full_credit_pinned_repooling as driver
from algorithm.tests.test_relay_repooling_exchange import fixture,topology
from algorithm.tests.test_repooling_with_pinned_polish import transformed_runtime


WEIGHTS,SCALES=(.5,.25,.25),(200.,4464.183540734935,775.7731679286865)


def parameters(**kwargs):
    return dict(dict(max_groups=2,option_count=4,beam_width=8,full_candidates_per_group=8),**kwargs)


def originals():
    names=set(driver.selection.PATCH_NAMES)|set(driver.bundles.PATCH_NAMES)
    names|=set(driver.layouts.PATCH_NAMES)|set(driver.frontier.PATCH_NAMES)
    return {name:getattr(driver.frozen.repool,name) for name in names}


def audit_all_snapshots(problem,base,result,relay_enabled):
    from algorithm.experiments.audit_repooling import audit_repooling_proposal
    from algorithm.experiments.audit_pinned_repooling import audit_pinned_repooling_proposal
    from algorithm.experiments.audit_weighted_relay_ablation import audit_payload
    from algorithm.tests.test_audit_mobile_relay import canonical_payload
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    graph=topology(problem)
    graph["task_relay_candidates"]={str(t):list(ids) for t,ids in problem.task_relay_candidates.items()}
    def payload_check(snapshot):
        payload=canonical_payload(tasks,graph,snapshot["routes"],{})
        audit_payload(tasks,graph,payload)
    for snapshot in result["metadata"]["full_candidate_snapshots"]:
        proof=audit_repooling_proposal(tasks,graph,base,snapshot["routes"],snapshot["move"],relay_enabled=relay_enabled)
        assert proof["passed"]
        for k,value in snapshot["score"].items():assert proof["score"][k]==pytest.approx(value)
        payload_check(snapshot)
    for record in result["metadata"]["polish_records"]:
        helper=record["helper_metadata"]
        snapshots=[record["post_polish"]]
        snapshots += [helper.get(k) for k in ("input_snapshot","first_complete_candidate","best_constructed_candidate","post_pin_snapshot")]
        snapshots += helper.get("full_candidate_snapshots",[])
        for snapshot in filter(None,snapshots):
            proof=audit_pinned_repooling_proposal(tasks,graph,base,record["macro_routes"],snapshot["routes"],
                snapshot.get("move") or record["move"],relay_enabled=relay_enabled)
            assert proof["passed"] and proof["actual_cross_uav_count"]==(2 if relay_enabled else 0)
            for k,value in snapshot["score"].items():assert proof["score"][k]==pytest.approx(value)
            payload_check(snapshot)


def real_config(module):
    return module.ALNSConfig(max_iterations=2,relay_min_destroy_fraction=.34,
        relay_max_destroy_fraction=.34,repair_rank_candidate_limit=12,
        repair_exact_candidate_limit=12,direct_exact_top_k=4)


def run(problem,base,module,**kwargs):
    return driver.refine(problem,base,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,
        alns_module=module,polish_config=real_config(module),**parameters(**kwargs))


@pytest.mark.parametrize("relay_enabled",[False,True])
def test_real_weighted_two_iterations_pin_scope_and_every_raw_post_payload_replayed(transformed_runtime,relay_enabled):
    problem,base=fixture(deadline=100.)
    module=transformed_runtime
    functions=originals();alns_originals={n:getattr(module,n) for n in driver.frozen.pinned.PATCH_NAMES}
    adapter_state=(module._weighted_adapter.active,module._weighted_adapter.coefficients)
    old=topology(problem)
    result=run(problem,base,module,relay_enabled=relay_enabled)
    meta=result["metadata"]
    assert result["evaluation"].valid and "problem" not in result
    assert meta["final_key"]<=meta["initial_key"]
    assert meta["full_score_selector_statistics"]["selector_completed"]
    assert meta["selector_completed"] and meta["polish_calls"]>0
    assert all(record["post_polish"] is not None for record in meta["polish_records"])
    assert any(r["helper_metadata"]["destroy_calls"]>0 for r in meta["polish_records"])
    assert any(r["helper_metadata"]["direct_exact_global_evaluations"]>0 for r in meta["polish_records"])
    assert meta["polish_iterations"]==2*meta["polish_calls"]
    assert meta["process_local_patches_restored"] and len(meta["process_local_patch_names"])==8
    assert meta["pinned_alns_patches_restored"] and len(meta["pinned_alns_patch_names"])==5
    assert meta["weighted_objective_state_restored"] and originals()==functions
    assert all(getattr(module,n) is f for n,f in alns_originals.items())
    assert (module._weighted_adapter.active,module._weighted_adapter.coefficients)==adapter_state
    assert topology(problem)==old and not driver.frozen.pinned._active
    assert set(build_plan_index(problem,result["routes"]).relay_task_ids)==({1,2} if relay_enabled else set())
    assert meta["full_credit_parameters"]["fixed_network"]
    assert not meta["full_credit_parameters"]["coordinate_search"]
    assert not meta["full_credit_parameters"]["selected_macro_identities_paired"]
    audit_all_snapshots(problem,base,result,relay_enabled)
    json.dumps(meta,allow_nan=False)


def test_both_arms_same_full_selector_bounds_and_fixed_pinned_budget(transformed_runtime):
    problem,base=fixture(deadline=100.);module=transformed_runtime
    direct=run(problem,base,module,relay_enabled=False)
    relay=run(problem,base,module)
    dm,rm=direct["metadata"],relay["metadata"]
    assert dm["selected_groups"]==rm["selected_groups"]
    assert dm["selector_sha256"]==rm["selector_sha256"]
    digest=sha256(json.dumps(rm["selected_groups"],sort_keys=True,separators=(",",":")).encode()).hexdigest()
    assert digest==rm["selector_sha256"]
    assert dm["full_credit_parameters"]==rm["full_credit_parameters"]
    bounds=rm["full_credit_parameters"]
    assert bounds["proposal_fraction"]==.35 and bounds["candidate_limit"]==2 and bounds["proposal_margin"]==.015
    assert bounds["guard_reserve_seconds"]==.03 and bounds["guard_reserve_fraction"]==.05
    assert bounds["compensation_geometry_pool_limit"]==bounds["compensation_removal_pool_limit"]==6
    assert bounds["source_surface_limit"]==512 and bounds["memo_limit"]==bounds["receiver_bundle_memo_limit"]==8192
    assert not bounds["partial_incumbent_pruning"]


def test_raw_deadline_does_not_truncate_later_alns_or_trigger_repool_hooks(transformed_runtime,monkeypatch):
    problem,base=fixture(deadline=100.);module=transformed_runtime
    starts=[]
    # Freeze only the old facade's budget clock. The real helper, raw
    # constructor and contexts keep their actual absolute clocks. Advancing
    # the facade beyond raw_deadline emulates its ordinary elapsed raw stage.
    clock=[perf_counter()];absolute=clock[0]+2.
    monkeypatch.setattr(driver.frozen,"perf_counter",lambda:clock[0])
    raw=driver.frozen.repool.refine
    def source(*args,**kwargs):
        result=raw(*args,**kwargs)
        clock[0]=kwargs["deadline"]+.001
        return result
    monkeypatch.setattr(driver.frozen.repool,"refine",source)
    original_helper=driver.frozen.pinned.polish_pinned_macro
    helper_calls=[]
    def observed_helper(*args,**kwargs):
        assert clock[0]>starts[0]
        # If polish accidentally invokes any construction hook it fails
        # immediately; production ALNS references its own real globals.
        actual=driver.frozen.repool
        before={name:getattr(actual,name) for name in originals()}
        def forbidden(*a,**k):pytest.fail("raw construction hook invoked during ordinary pinned ALNS")
        for name in before:setattr(actual,name,forbidden)
        helper_calls.append(True)
        try:return original_helper(*args,**kwargs)
        finally:
            for name,func in before.items():setattr(actual,name,func)
    monkeypatch.setattr(driver.frozen.pinned,"polish_pinned_macro",observed_helper)
    # Keep solve_alns's actual runtime globals for its identity preflight.
    destroy=module.destroy_solution
    def observed(*args,**kwargs):
        assert clock[0]>starts[0]
        return destroy(*args,**kwargs)
    monkeypatch.setattr(module,"destroy_solution",observed)
    # Pin the expected raw deadline from the controlled facade's start clock.
    starts.append(clock[0]+.35*(absolute-clock[0]))
    result=driver.refine(problem,base,deadline=absolute,weights=WEIGHTS,scales=SCALES,
        alns_module=module,polish_config=real_config(module),**parameters())
    assert result["metadata"]["polish_calls"]>0 and result["metadata"]["polish_iterations"]>0
    assert len(helper_calls)==result["metadata"]["polish_calls"]
    assert result["metadata"]["proposal_deadline_perf_counter"]==starts[0]
    assert any(r["helper_metadata"]["destroy_calls"]>0 for r in result["metadata"]["polish_records"])
    assert module.destroy_solution is observed


def test_reused_common_groups_explicit_and_raw_false_positive_cannot_replace_input(transformed_runtime,monkeypatch):
    problem,base=fixture(deadline=100.);module=transformed_runtime
    first=run(problem,base,module,relay_enabled=False)
    selected=first["metadata"]["selected_groups"]
    original=driver.frozen.pinned.polish_pinned_macro
    def unchanged(p,routes,**kwargs):
        # Return a complete worse macro with an overoptimistic telemetry
        # claim. The frozen facade must independently compare its full key.
        return dict(problem=p,routes=routes,evaluation=None,iterations=0,
            metadata=dict(adoptable=True,returned_checkpoint_checked_within_deadline=True))
    monkeypatch.setattr(driver.frozen.pinned,"polish_pinned_macro",unchanged)
    result=run(problem,base,module,relay_enabled=False,common_selector_records=selected)
    assert result["routes"]==base and result["metadata"]["accepted_improvements"]==0
    assert result["metadata"]["full_score_selector_records_reused"]
    assert result["metadata"]["full_score_selector_statistics"]["actual_selectors"]==0
    assert result["metadata"]["selector_completed"]
    assert not any(r["adopted"] for r in result["metadata"]["polish_records"])
    assert original is not unchanged


def test_innermost_error_and_nested_context_restore_all_thirteen_hooks(transformed_runtime,monkeypatch):
    problem,base=fixture();module=transformed_runtime;functions=originals()
    alns_originals={n:getattr(module,n) for n in driver.frozen.pinned.PATCH_NAMES}
    def fail(*args,**kwargs):
        assert originals()!=functions
        raise RuntimeError("controlled fixed-network pin facade failure")
    monkeypatch.setattr(driver.frozen,"refine",fail)
    with pytest.raises(RuntimeError,match="controlled"):run(problem,base,module)
    assert originals()==functions and all(getattr(module,n) is f for n,f in alns_originals.items())
    assert not any((driver.selection._active,driver.bundles._active,driver.layouts._active,driver.frontier._active))
    with driver.selection.full_score_selector(driver.frozen.repool,problem,deadline=perf_counter()+1.,
            weights=WEIGHTS,scales=SCALES):
        patched=originals()
        with pytest.raises(RuntimeError,match="nested"):run(problem,base,module)
        assert originals()==patched
    assert originals()==functions


def test_expired_budget_preserves_original_without_any_polish(transformed_runtime):
    problem,base=fixture();module=transformed_runtime;functions=originals()
    result=driver.refine(problem,base,deadline=perf_counter()-1.,weights=WEIGHTS,scales=SCALES,alns_module=module)
    assert result["routes"]==base and "problem" not in result
    assert result["metadata"]["deadline_reached"] and result["metadata"]["polish_calls"]==0
    assert not result["metadata"]["selector_completed"] and originals()==functions
    assert result["evaluation"].score==evaluate_solution(problem,base,_force_full=True).score


@pytest.mark.parametrize("kwargs",[dict(compensation_geometry_pool_limit=33),dict(source_surface_limit=True),
    dict(memo_limit=0),dict(beam_width=9)])
def test_invalid_bounds_open_no_construction_or_polish_context(transformed_runtime,kwargs):
    problem,base=fixture();functions=originals()
    with pytest.raises(ValueError):run(problem,base,transformed_runtime,**kwargs)
    assert originals()==functions
