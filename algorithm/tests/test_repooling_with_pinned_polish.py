"""Facade budget/trace guards plus one tiny real transformed ALNS integration."""
from copy import deepcopy
import importlib
import importlib.util
import json
from pathlib import Path
import sys
from time import perf_counter

import pytest

from uav_dispatch import alns
from uav_dispatch.relay import build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import repooling_with_pinned_polish as facade
from algorithm.experiments import weighted_direct_objective as weighted
from algorithm.tests.test_relay_repooling_exchange import fixture


WEIGHTS,SCALES=(.5,.25,.25),(6.,100.,100.)


def source(problem,routes,relay_enabled=True):
    return facade.repool.refine(problem,routes,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,
                               relay_enabled=relay_enabled,max_groups=2)


def run(problem,routes,**kwargs):
    return facade.refine(problem,routes,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,**kwargs)


def fake_outcome(problem,routes,*,adoptable=True,predeadline=True):
    return dict(problem=problem,routes=routes,evaluation=None,iterations=2,
                metadata=dict(adoptable=adoptable,returned_checkpoint_checked_within_deadline=predeadline))


def copy_source(raw):
    # SolutionEvaluation contains immutable mapping proxies, not pickle data.
    return dict(raw,metadata=deepcopy(raw["metadata"]))


def test_budget_proposal_fraction_leaf_reserve_and_trace_are_explicit(monkeypatch):
    problem,routes=fixture()
    raw=source(problem,routes)
    clock=[0.]
    monkeypatch.setattr(facade,"perf_counter",lambda:clock[0])
    proposal_args=[]
    helper_args=[]
    def propose(*args,**kwargs):
        proposal_args.append(kwargs)
        clock[0]=kwargs["deadline"]
        return copy_source(raw)
    def polish(p,candidate,**kwargs):
        helper_args.append(kwargs)
        clock[0]=kwargs["deadline"]-.001
        return fake_outcome(p,candidate)
    monkeypatch.setattr(facade.repool,"refine",propose)
    monkeypatch.setattr(facade.pinned,"polish_pinned_macro",polish)
    module=object()
    result=facade.refine(problem,routes,deadline=20.,weights=WEIGHTS,scales=SCALES,alns_module=module)
    stats=result["metadata"]
    assert proposal_args[0]["deadline"]==7.
    assert stats["polish_calls"]==2 and len(helper_args)==2
    assert all(k["proposal_margin"]==.015 and k["alns_module"] is module for k in helper_args)
    assert all(len(k["pinned_task_ids"])==4 and len(k["required_cross_task_ids"])==2 for k in helper_args)
    for record in stats["polish_records"]:
        assert record["helper_deadline_perf_counter"]<record["leaf_deadline_perf_counter"]<=20.
        assert record["guard_reserve_seconds"]==pytest.approx(.03)
        assert record["post_polish"]["complete"] and record["post_polish"]["valid"]
        assert record["macro_routes"]==record["full_proposal"]["routes"]
    assert result["evaluation"].valid and "problem" not in result
    assert stats["full_candidate_snapshots"]==raw["metadata"]["full_candidate_snapshots"]
    assert stats["input_snapshot"]==raw["metadata"]["input_snapshot"]
    assert stats["selected_groups"]==raw["metadata"]["selected_groups"]
    assert stats["selector_sha256"]==raw["metadata"]["selector_sha256"]
    json.dumps(stats,allow_nan=False)


@pytest.mark.parametrize("relay_enabled",[False,True])
def test_matched_facade_policy_and_four_pins_in_both_arms(monkeypatch,relay_enabled):
    problem,routes=fixture()
    raw=source(problem,routes,relay_enabled=relay_enabled)
    observed=[]
    monkeypatch.setattr(facade.repool,"refine",lambda *a,**k:copy_source(raw))
    def polish(p,candidate,**kwargs):
        observed.append(kwargs)
        return fake_outcome(p,candidate,adoptable=False)
    monkeypatch.setattr(facade.pinned,"polish_pinned_macro",polish)
    result=run(problem,routes,relay_enabled=relay_enabled,proposal_margin=1.,alns_module=object())
    assert observed and len(observed)<=2
    assert all(len(k["pinned_task_ids"])==4 for k in observed)
    assert all(len(k["required_cross_task_ids"])==(2 if relay_enabled else 0) for k in observed)
    assert all(k["comparison_key"]==tuple(result["metadata"]["initial_key"]) for k in observed)
    assert result["metadata"]["configuration"]["proposal_fraction"]==.35
    assert result["metadata"]["configuration"]["candidate_limit"]==2


def test_worse_macro_does_not_become_incumbent_when_helper_returns_it(monkeypatch):
    problem,routes=fixture()
    raw=source(problem,routes,relay_enabled=False)
    assert all(s["key"][0]>raw["metadata"]["initial_key"][0] for s in raw["metadata"]["full_candidate_snapshots"])
    monkeypatch.setattr(facade.repool,"refine",lambda *a,**k:copy_source(raw))
    monkeypatch.setattr(facade.pinned,"polish_pinned_macro",lambda p,r,**k:fake_outcome(p,r,adoptable=True))
    result=run(problem,routes,relay_enabled=False,proposal_margin=1.,alns_module=object())
    assert result["routes"]==routes and result["metadata"]["accepted_improvements"]==0
    assert all(not r["adopted"] for r in result["metadata"]["polish_records"])


def test_true_ranking_is_replayed_and_limit_margin_duplicates_are_enforced(monkeypatch):
    problem,routes=fixture()
    raw=source(problem,routes)
    originals=deepcopy(raw["metadata"]["full_candidate_snapshots"])
    # Corrupt only ranking telemetry in the fake source: facade recomputes
    # every complete schedule before choosing, rather than trusting those keys.
    for number,snapshot in enumerate(raw["metadata"]["full_candidate_snapshots"]):
        snapshot["key"]=[1000.-number,0,0,0]
    raw["metadata"]["full_candidate_snapshots"].append(deepcopy(raw["metadata"]["full_candidate_snapshots"][0]))
    monkeypatch.setattr(facade.repool,"refine",lambda *a,**k:copy_source(raw))
    observed=[]
    def polish(p,r,**kwargs):
        observed.append(r)
        return fake_outcome(p,r,adoptable=False)
    monkeypatch.setattr(facade.pinned,"polish_pinned_macro",polish)
    result=run(problem,routes,alns_module=object())
    expected=sorted({tuple(tuple(r) for r in s["routes"]) for s in originals},
        key=lambda r:(facade.repool.weighted_key(evaluate_solution(problem,r,_force_full=True).score,WEIGHTS,SCALES),r))[:2]
    assert observed==expected
    assert result["metadata"]["polish_skipped_duplicate"]==1
    assert len(result["metadata"]["polish_candidates"])<=2
    initial=tuple(result["metadata"]["initial_key"])
    assert all(c["key"][0]<=initial[0]+.015 for c in result["metadata"]["polish_candidates"])


@pytest.mark.parametrize("kind",["pins","invalid","topology","helper_deadline"])
def test_helper_claims_are_rechecked_and_rejected_without_losing_raw_winner(monkeypatch,kind):
    problem,routes=fixture()
    raw=source(problem,routes)
    monkeypatch.setattr(facade.repool,"refine",lambda *a,**k:copy_source(raw))
    def polish(p,candidate,**kwargs):
        if kind=="pins":
            return fake_outcome(p,routes)
        if kind=="invalid":
            return fake_outcome(p,((),(),()))
        if kind=="topology":
            return fake_outcome(object(),candidate)
        return fake_outcome(p,candidate,predeadline=False)
    monkeypatch.setattr(facade.pinned,"polish_pinned_macro",polish)
    result=run(problem,routes,alns_module=object())
    assert result["routes"]==raw["routes"]
    assert result["evaluation"].valid
    assert not any(r["adopted"] for r in result["metadata"]["polish_records"])
    assert all(r["rejection_reason"] for r in result["metadata"]["polish_records"])


def test_one_leaf_overrun_does_not_spend_or_cancel_next_leaf(monkeypatch):
    problem,routes=fixture()
    raw=source(problem,routes)
    clock=[0.]
    monkeypatch.setattr(facade,"perf_counter",lambda:clock[0])
    def propose(*a,**kwargs):
        clock[0]=kwargs["deadline"]
        return copy_source(raw)
    calls=[]
    def polish(p,candidate,**kwargs):
        calls.append(kwargs)
        clock[0]=kwargs["deadline"]+.1 if len(calls)==1 else kwargs["deadline"]-.01
        return fake_outcome(p,candidate)
    monkeypatch.setattr(facade.repool,"refine",propose)
    monkeypatch.setattr(facade.pinned,"polish_pinned_macro",polish)
    result=facade.refine(problem,routes,deadline=20.,weights=WEIGHTS,scales=SCALES,alns_module=object())
    assert len(calls)==2
    assert result["metadata"]["polish_records"][0]["rejection_reason"]=="wrapper_full_check_after_leaf_deadline"
    assert result["metadata"]["polish_records"][1]["finished_within_leaf_deadline"]
    assert result["routes"]==raw["routes"]


@pytest.mark.parametrize("phase",["initial","raw_full","post_full"])
def test_work_after_global_deadline_cannot_replace_input_or_raw_guard(monkeypatch,phase):
    problem,routes=fixture()
    raw=source(problem,routes)
    clock=[21. if phase=="initial" else 0.]
    monkeypatch.setattr(facade,"perf_counter",lambda:clock[0])
    monkeypatch.setattr(facade.repool,"refine",lambda *a,**k:copy_source(raw))
    monkeypatch.setattr(facade.pinned,"polish_pinned_macro",lambda p,r,**k:fake_outcome(p,r))
    original=facade.evaluate_solution
    calls=[0]
    def checked(*a,**kwargs):
        value=original(*a,**kwargs)
        calls[0]+=1
        if phase=="raw_full" and calls[0]==2:clock[0]=21.
        if phase=="post_full" and calls[0]>2+len(raw["metadata"]["full_candidate_snapshots"]):clock[0]=21.
        return value
    monkeypatch.setattr(facade,"evaluate_solution",checked)
    result=facade.refine(problem,routes,deadline=20.,weights=WEIGHTS,scales=SCALES,alns_module=object())
    assert result["routes"]==(raw["routes"] if phase=="post_full" else routes)
    assert not any(m["kind"]=="pinned_polish" for m in result["metadata"]["accepted_moves"])
    assert result["metadata"]["deadline_reached"]


def test_margin_skips_all_worse_direct_macros_without_helper_work(monkeypatch):
    problem,routes=fixture()
    raw=source(problem,routes,relay_enabled=False)
    monkeypatch.setattr(facade.repool,"refine",lambda *a,**k:copy_source(raw))
    monkeypatch.setattr(facade.pinned,"polish_pinned_macro",lambda *a,**k:pytest.fail("margin must gate helper"))
    result=run(problem,routes,relay_enabled=False,alns_module=object())
    assert result["routes"]==routes
    assert result["metadata"]["polish_skipped_margin"]==len(raw["metadata"]["full_candidate_snapshots"])
    assert result["metadata"]["polish_calls"]==0


@pytest.fixture
def transformed_runtime(tmp_path,monkeypatch):
    package=tmp_path/"tiny_repool_pinned_runtime"
    package.mkdir()
    for path in Path(alns.__file__).parent.glob("*.py"):
        text=path.read_text(encoding="utf-8")
        if path.name in weighted.EXPECTED_COUNTS:
            text,counts=weighted.transform(text)
            assert counts==weighted.EXPECTED_COUNTS[path.name]
        (package/path.name).write_text(text,encoding="utf-8")
    monkeypatch.setitem(sys.modules,"weighted_direct_objective",weighted)
    spec=importlib.util.spec_from_file_location(package.name,package/"__init__.py",submodule_search_locations=[str(package)])
    runtime=importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules,package.name,runtime)
    spec.loader.exec_module(runtime)
    module=importlib.import_module(package.name+".alns")
    try:
        yield module
    finally:
        for name in tuple(sys.modules):
            if name==package.name or name.startswith(package.name+"."):
                sys.modules.pop(name,None)


def test_real_frozen_weighted_helper_integration_keeps_two_true_cross(transformed_runtime):
    problem,routes=fixture()
    module=transformed_runtime
    originals={name:getattr(module,name) for name in facade.pinned.PATCH_NAMES}
    config=module.ALNSConfig(max_iterations=2,relay_min_destroy_fraction=.34,
        relay_max_destroy_fraction=.34,repair_rank_candidate_limit=12,
        repair_exact_candidate_limit=12,direct_exact_top_k=4)
    result=facade.refine(problem,routes,deadline=perf_counter()+.6,weights=WEIGHTS,scales=SCALES,
        alns_module=module,polish_config=config,max_groups=2)
    stats=result["metadata"]
    assert result["evaluation"].valid and stats["polish_calls"]>0
    assert stats["final_cross_uav_count"]==2
    assert all(record["post_polish"] is not None for record in stats["polish_records"])
    assert any(record["helper_metadata"]["destroy_calls"]>0 for record in stats["polish_records"])
    assert any(record["helper_metadata"]["direct_exact_global_evaluations"]>0 for record in stats["polish_records"])
    assert all(record["helper_metadata"]["patches_restored"] for record in stats["polish_records"])
    assert all(getattr(module,name) is function for name,function in originals.items())
    assert not weighted.active and not facade.pinned._active
    assert build_plan_index(problem,result["routes"]).relay_task_ids==(1,2)
    json.dumps(stats,allow_nan=False)


@pytest.mark.parametrize("relay_enabled",[False,True])
def test_independent_pin_audit_binds_raw_macro_post_and_helper_snapshots(monkeypatch,relay_enabled):
    from algorithm.experiments.audit_pinned_repooling import audit_pinned_repooling_proposal
    from algorithm.tests.test_relay_repooling_exchange import topology
    problem,routes=fixture()
    raw=source(problem,routes,relay_enabled=relay_enabled)
    monkeypatch.setattr(facade.repool,"refine",lambda *a,**k:copy_source(raw))
    def polish(p,candidate,**kwargs):
        full=evaluate_solution(p,candidate,_force_full=True)
        snapshot=facade.repool._snapshot(p,candidate,full,WEIGHTS,SCALES,phase="helper_complete")
        result=fake_outcome(p,candidate,adoptable=False)
        result["metadata"].update(input_snapshot=snapshot,first_complete_candidate=snapshot,
            best_constructed_candidate=snapshot,post_pin_snapshot=snapshot,full_candidate_snapshots=[snapshot])
        return result
    monkeypatch.setattr(facade.pinned,"polish_pinned_macro",polish)
    result=run(problem,routes,relay_enabled=relay_enabled,proposal_margin=1.,alns_module=object())
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    for record in result["metadata"]["polish_records"]:
        snapshots=[record["post_polish"]]
        meta=record["helper_metadata"]
        snapshots.extend(meta[k] for k in ("input_snapshot","first_complete_candidate","best_constructed_candidate","post_pin_snapshot"))
        snapshots.extend(meta["full_candidate_snapshots"])
        for snapshot in snapshots:
            audit=audit_pinned_repooling_proposal(tasks,topology(problem),routes,record["macro_routes"],
                snapshot["routes"],snapshot.get("move") or record["move"],relay_enabled=relay_enabled)
            assert audit["passed"] and audit["actual_cross_uav_count"]==(2 if relay_enabled else 0)
            for key in snapshot["score"]:
                assert audit["score"][key]==pytest.approx(snapshot["score"][key])


def test_invalid_facade_budget_parameters_rejected():
    problem,routes=fixture()
    for change in (dict(proposal_fraction=0.),dict(candidate_limit=3),dict(candidate_limit=True),
                   dict(proposal_margin=-1.),dict(guard_reserve_fraction=1.),dict(relay_enabled=1)):
        with pytest.raises(ValueError):run(problem,routes,**change)
