"""Release compensation only: real loops, changed owners and strict pair pins."""
from dataclasses import replace
from random import Random
from time import perf_counter
import json

import pytest

from uav_dispatch import alns
from uav_dispatch.relay import build_plan_index
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import pair_pinned_repooling as driver
from algorithm.experiments import relay_preserving_polish as old_helper
from algorithm.tests.test_relay_repooling_exchange import fixture,topology
from algorithm.tests.test_repooling_with_pinned_polish import transformed_runtime
from algorithm.tests.test_full_credit_pinned_repooling import WEIGHTS,SCALES,parameters,originals,real_config
from algorithm.tests.test_relay_preserving_polish import fixture as nine_fixture,fake_module


def run(problem,base,module,**kwargs):
    return driver.refine(problem,base,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,
        alns_module=module,polish_config=real_config(module),polish_max_iterations=2,**parameters(**kwargs))


def pair_context(module,problem,routes,*,stats=None,cross=(1,2),deadline=None):
    return driver.pair_helper.pinned_search(module,problem,routes,
        deadline=perf_counter()+2. if deadline is None else deadline,weights=WEIGHTS,scales=SCALES,
        pinned_task_ids=(1,2),required_cross_task_ids=cross,statistics=stats)


def audit_snapshots(problem,base,result,relay_enabled):
    from algorithm.experiments.audit_repooling import audit_repooling_proposal
    from algorithm.experiments.audit_pair_pinned_repooling import audit_pair_pinned_repooling_proposal
    from algorithm.experiments.audit_weighted_relay_ablation import audit_payload
    from algorithm.tests.test_audit_mobile_relay import canonical_payload
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    graph=topology(problem)
    graph["task_relay_candidates"]={str(t):list(ids) for t,ids in problem.task_relay_candidates.items()}
    for snapshot in result["metadata"]["full_candidate_snapshots"]:
        assert audit_repooling_proposal(tasks,graph,base,snapshot["routes"],snapshot["move"],relay_enabled=relay_enabled)["passed"]
    for record in result["metadata"]["polish_records"]:
        helper=record["helper_metadata"]
        snapshots=[record["post_polish"]]
        snapshots += [helper.get(k) for k in ("input_snapshot","first_complete_candidate","best_constructed_candidate","post_pin_snapshot")]
        snapshots += helper.get("full_candidate_snapshots",[])
        for snapshot in filter(None,snapshots):
            proof=audit_pair_pinned_repooling_proposal(tasks,graph,base,record["macro_routes"],snapshot["routes"],
                record["original_macro_move"],relay_enabled=relay_enabled,telemetry=snapshot)
            assert proof["passed"] and proof["actual_cross_uav_count"]==(2 if relay_enabled else 0)
            assert proof["actual_final_owners"]==snapshot["actual_final_owners"]
            assert snapshot["released_compensation_task_ids"]==[record["original_macro_move"][k] for k in ("k1","k2")]
            for key,value in snapshot["score"].items():assert proof["score"][key]==pytest.approx(value)
            audit_payload(tasks,graph,canonical_payload(tasks,graph,snapshot["routes"],{}))


@pytest.mark.parametrize("relay_enabled",[False,True])
def test_real_weighted_two_pin_both_arms_preserve_pair_and_full_independent_replay(transformed_runtime,relay_enabled):
    problem,base=fixture(deadline=100.);module=transformed_runtime
    raw=originals();facade=driver.frozen.frozen.frozen
    aliases={name:getattr(facade,name) for name in driver.FACADE_PATCH_NAMES}
    funcs={n:getattr(module,n) for n in driver.pair_helper.PATCH_NAMES}
    objective=(module._weighted_adapter.active,module._weighted_adapter.coefficients)
    geometry=topology(problem)
    result=run(problem,base,module,relay_enabled=relay_enabled)
    meta=result["metadata"]
    assert result["evaluation"].valid and "problem" not in result and meta["final_key"]<=meta["initial_key"]
    assert meta["polish_calls"]>0 and meta["polish_iterations"]==2*meta["polish_calls"]
    assert meta["pair_pinned_parameters"]["pinned_task_count"]==2
    assert all(r["helper_metadata"]["pinned_task_ids"]==[r["move"]["i"],r["move"]["j"]]
               for r in meta["polish_records"])
    assert all(r["helper_metadata"]["destroy_calls"]>0 for r in meta["polish_records"])
    assert all(r["helper_metadata"]["direct_exact_global_evaluations"]>0 for r in meta["polish_records"])
    assert meta["facade_patches_restored"] and meta["process_local_patches_restored"]
    assert meta["pinned_alns_patches_restored"] and meta["weighted_objective_state_restored"]
    assert all(getattr(facade,n) is f for n,f in aliases.items()) and originals()==raw
    assert all(getattr(module,n) is f for n,f in funcs.items())
    assert (module._weighted_adapter.active,module._weighted_adapter.coefficients)==objective
    assert not driver._active and not driver.pair_helper._active and not old_helper._active
    assert topology(problem)==geometry
    assert meta["cold_polish_profile"]==dict(initial_temperature=.0025,minimum_temperature=.00001,cooling_rate=.97,max_iterations=2)
    for accepted in meta["accepted_moves"]:
        owners=build_plan_index(problem,accepted["full_snapshot"]["routes"]).delivery_route_by_task
        assert accepted["actual_final_owners"]=={str(t):a for t,a in owners.items()}
        assert accepted["original_macro_move"]["final_aircraft"]==[2,2,0,1]
        assert accepted["pinned_task_ids"]==[1,2] and accepted["released_compensation_task_ids"]==[3,4]
        if accepted["kind"]=="raw_macro":
            assert "original_macro_move" not in accepted["full_snapshot"]
    audit_snapshots(problem,base,result,relay_enabled)
    json.dumps(meta,allow_nan=False)


def test_same_selector_and_two_pins_in_both_arms(transformed_runtime):
    problem,base=fixture(deadline=100.);module=transformed_runtime
    direct=run(problem,base,module,relay_enabled=False)
    relay=run(problem,base,module)
    dm,rm=direct["metadata"],relay["metadata"]
    assert dm["selector_completed"] and rm["selector_completed"]
    assert dm["selected_groups"]==rm["selected_groups"] and dm["selector_sha256"]==rm["selector_sha256"]
    assert dm["pair_pinned_parameters"]==rm["pair_pinned_parameters"]
    assert dm["full_credit_parameters"]==rm["full_credit_parameters"]


def test_compensation_owners_really_can_change_while_old_four_pin_validator_rejects_it():
    problem,routes=nine_fixture()
    swapped=tuple(tuple(4 if v==3 else -4 if v==-3 else 3 if v==4 else -3 if v==-4 else v for v in r) for r in routes)
    assert evaluate_solution(problem,swapped,_force_full=True).valid
    module=fake_module(destroy=lambda *a,**k:((),(3,4)),repair=lambda *a,**k:swapped)
    stats={}
    expected=driver.pair_helper._signature(problem,routes,{1,2})
    with pair_context(module,problem,routes,stats=stats):
        partial,removed=module.destroy_solution(problem,RouteEvaluator(problem),routes,"random",2,Random(9))
        assert set(removed)=={3,4} and stats["selected_pins_filtered"]==0
        repaired=module._repair(problem,None,partial,removed,"greedy",20)
        assert repaired==swapped and stats["complete_valid_candidates"]==1
        assert driver.pair_helper._signature(problem,repaired,{1,2})==expected
    assert build_plan_index(problem,swapped).delivery_route_by_task[3]==1
    assert build_plan_index(problem,swapped).delivery_route_by_task[4]==0
    with pytest.raises(ValueError,match="pinned"):
        old_helper._validate(problem,swapped,{1,2,3,4},old_helper._signature(problem,routes,{1,2,3,4}),{1,2})


def test_changed_compensation_telemetry_records_actual_owners_and_historical_macro(transformed_runtime,monkeypatch):
    problem,base=fixture(deadline=100.);module=transformed_runtime
    def swap_compensation(p,routes,**kwargs):
        candidates=set(p.task_ids)-set(kwargs["pinned_task_ids"])
        # The first fixture family compensates task3 and task4; source
        # signed leg identifiers are >6, so these whole DIRECT IDs are exact.
        assert {3,4}<=candidates
        changed=tuple(tuple(4 if v==3 else -4 if v==-3 else 3 if v==4 else -3 if v==-4 else v for v in r) for r in routes)
        checked=evaluate_solution(p,changed,_force_full=True)
        shape=driver.pair_helper._shape(p,changed,set(kwargs["required_cross_task_ids"]))
        snapshot=driver.pair_helper._snapshot(p,changed,checked,WEIGHTS,SCALES,phase="controlled_compensation_repair",shape=shape)
        return dict(problem=p,routes=changed,iterations=1,metadata=dict(adoptable=False,
            returned_checkpoint_checked_within_deadline=True,full_candidate_snapshots=[snapshot]))
    monkeypatch.setattr(driver.pair_helper,"polish_pinned_macro",swap_compensation)
    result=run(problem,base,module,max_groups=1)
    assert result["metadata"]["polish_records"]
    for record in result["metadata"]["polish_records"]:
        assert record["post_polish"] is not None
        post=record["post_polish"]
        assert record["original_macro_move"]["final_aircraft"]==[2,2,0,1]
        assert record["move"]["semantic"]=="original_macro_before_pair_polish"
        assert post["move"]["semantic"]=="post_pair_polish" and "final_aircraft" not in post["move"]
        assert post["actual_final_owners"]["3"]==1 and post["actual_final_owners"]["4"]==0
        assert post["released_compensation_task_ids"]==[3,4]
        assert not record["adopted"]
    audit_snapshots(problem,base,result,True)


@pytest.mark.parametrize("kind",["relay_removed","source_owner","pair_order","take_gap","quota","capacity"])
def test_identity_shape_or_quota_breaking_complete_repair_rolls_back(kind):
    problem,routes=nine_fixture()
    candidate=list(routes)
    if kind=="relay_removed":
        candidate[0]=tuple(v for v in candidate[0] if abs(v)!=101)
        candidate[2]=tuple(1 if v==102 else -1 if v==-102 else v for v in candidate[2])
    elif kind=="source_owner":
        candidate[0]=tuple(v for v in candidate[0] if abs(v)!=101)
        candidate[1]=(101,-101)+candidate[1]
    elif kind=="pair_order":candidate[2]=(104,102)+candidate[2][2:]
    elif kind=="take_gap":candidate[2]=(102,9,-9,104,-102,-104)
    elif kind=="quota":
        problem=replace(problem,max_tasks_per_drone=4)
        candidate[0]=tuple(v for v in candidate[0] if abs(v)!=5)
        candidate[1]=candidate[1]+(5,-5)
        assert evaluate_solution(problem,tuple(candidate),_force_full=True).valid
    else:candidate[2]=(102,104,9,-102,-104,-9)
    candidate=tuple(candidate)
    module=fake_module(destroy=lambda *a,**k:((),(3,4)),repair=lambda *a,**k:candidate)
    stats={}
    with pair_context(module,problem,routes,stats=stats):
        partial,removed=module.destroy_solution(problem,None,routes,"random",2,Random(5))
        assert module._repair(problem,None,partial,removed,"greedy",20)==routes
        score=evaluate_solution(problem,routes,_force_full=True).score
        assert not module._accept_worse(score,score,problem,.001,Random(2))
    assert stats["rollback_candidates"]==stats["rejected_acceptances"]==1


def test_aliases_restore_on_inner_error_nested_entry_and_expired_budget(transformed_runtime,monkeypatch):
    problem,base=fixture();module=transformed_runtime
    facade=driver.frozen.frozen.frozen
    aliases={n:getattr(facade,n) for n in driver.FACADE_PATCH_NAMES}
    functions=originals()
    def fail(*args,**kwargs):
        assert facade.pinned is driver.pair_helper and facade._macro_tasks is driver._pair_tasks
        with pytest.raises(RuntimeError,match="nested"):run(problem,base,module)
        raise RuntimeError("controlled pair facade failure")
    with monkeypatch.context() as m:
        m.setattr(driver.frozen,"refine",fail)
        with pytest.raises(RuntimeError,match="controlled"):run(problem,base,module)
    def overwrite(*args,**kwargs):
        facade._macro_tasks=lambda move:()
        return {}
    with monkeypatch.context() as m:
        m.setattr(driver.frozen,"refine",overwrite)
        with pytest.raises(RuntimeError,match="aliases were not restored"):run(problem,base,module)
    assert all(getattr(facade,n) is f for n,f in aliases.items())
    assert originals()==functions and not driver._active and not driver.pair_helper._active
    result=driver.refine(problem,base,deadline=perf_counter()-1.,weights=WEIGHTS,scales=SCALES,alns_module=module)
    assert result["routes"]==base and result["metadata"]["polish_calls"]==0
    assert result["metadata"]["facade_patches_restored"] and "problem" not in result
    assert all(getattr(facade,n) is f for n,f in aliases.items())


def test_new_helper_requires_exactly_two_pins_and_deadline_restores_five_functions():
    problem,routes=nine_fixture();functions={n:getattr(alns,n) for n in driver.pair_helper.PATCH_NAMES}
    with pytest.raises(ValueError,match="exactly two"):
        with driver.pair_helper.pinned_search(alns,problem,routes,deadline=perf_counter()+1.,weights=WEIGHTS,
                scales=SCALES,pinned_task_ids=(1,2,3,4),required_cross_task_ids=(1,2)):pass
    with pair_context(alns,problem,routes,deadline=perf_counter()-1.):
        with pytest.raises(alns._SearchDeadlineReached):
            alns.destroy_solution(problem,None,routes,"random",2,Random(1))
    assert all(getattr(alns,n) is f for n,f in functions.items()) and not driver.pair_helper._active
