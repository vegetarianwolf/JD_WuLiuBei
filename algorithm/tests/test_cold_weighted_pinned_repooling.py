"""One profile change, actual weighted loops and independent pinned replay."""
from dataclasses import asdict
from time import perf_counter

import pytest

from algorithm.experiments import cold_weighted_pinned_repooling as driver
from algorithm.tests.test_relay_repooling_exchange import fixture,topology
from algorithm.tests.test_repooling_with_pinned_polish import transformed_runtime
from algorithm.tests.test_full_credit_pinned_repooling import WEIGHTS,SCALES,parameters,originals,audit_all_snapshots,real_config


def test_default_worker_config_changes_only_four_fields_and_same_profile_for_both_arms(transformed_runtime,monkeypatch):
    problem,base=fixture();module=transformed_runtime;calls=[]
    custom=module.ALNSConfig(candidate_limit=16,repair_rank_candidate_limit=12,direct_exact_top_k=4)
    def captured(*args,**kwargs):
        calls.append(kwargs)
        return dict(routes=base,metadata=dict(method="controlled frozen facade"))
    monkeypatch.setattr(driver.frozen,"refine",captured)
    for arm in (False,True):
        result=driver.refine(problem,base,deadline=perf_counter()+1.,weights=WEIGHTS,scales=SCALES,
                             alns_module=module,polish_config=custom,relay_enabled=arm)
        assert result["metadata"]["cold_polish_profile"]==dict(initial_temperature=.0025,
            minimum_temperature=.00001,cooling_rate=.97,max_iterations=1000000)
    expected=asdict(custom)
    expected.update(initial_temperature=.0025,minimum_temperature=.00001,cooling_rate=.97,max_iterations=1000000)
    assert all(type(call["polish_config"]) is module.ALNSConfig for call in calls)
    assert all(asdict(call["polish_config"])==expected for call in calls)
    assert calls[0]["polish_config"]==calls[1]["polish_config"]
    assert custom.initial_temperature==.03 and custom.max_iterations==400
    assert all(call["proposal_fraction"]==.35 and call["candidate_limit"]==2
        and call["proposal_margin"]==.015 and call["guard_reserve_seconds"]==.03
        and call["guard_reserve_fraction"]==.05 for call in calls)
    assert all(call["max_groups"]==8 and call["option_count"]==4 and call["beam_width"]==8
        and call["full_candidates_per_group"]==8 for call in calls)


def test_real_cold_weighted_both_arms_keep_profile_after_helper_replace_and_independent_pin_audit(transformed_runtime):
    problem,base=fixture(deadline=100.);module=transformed_runtime
    raw_originals=originals();alns_originals={n:getattr(module,n) for n in driver.frozen.frozen.pinned.PATCH_NAMES}
    objective=(module._weighted_adapter.active,module._weighted_adapter.coefficients)
    geometry=topology(problem);results=[]
    for arm in (False,True):
        result=driver.refine(problem,base,deadline=perf_counter()+2.,weights=WEIGHTS,scales=SCALES,
            alns_module=module,polish_config=real_config(module),polish_max_iterations=2,
            relay_enabled=arm,**parameters())
        results.append(result);meta=result["metadata"]
        assert result["evaluation"].valid and "problem" not in result
        assert meta["final_key"]<=meta["initial_key"]
        assert meta["polish_calls"]>0 and meta["polish_iterations"]==2*meta["polish_calls"]
        assert all(r["helper_metadata"]["destroy_calls"]>0 for r in meta["polish_records"])
        for record in meta["polish_records"]:
            config=record["helper_metadata"]["configuration"]
            for key,value in meta["cold_polish_profile"].items():assert config[key]==value
            assert not config["enable_budget_cooling"]
        assert meta["process_local_patches_restored"] and meta["pinned_alns_patches_restored"]
        assert meta["weighted_objective_state_restored"] and originals()==raw_originals
        assert all(getattr(module,n) is f for n,f in alns_originals.items())
        assert (module._weighted_adapter.active,module._weighted_adapter.coefficients)==objective
        assert topology(problem)==geometry
        audit_all_snapshots(problem,base,result,arm)
    dm,rm=(r["metadata"] for r in results)
    assert dm["cold_polish_profile"]==rm["cold_polish_profile"]
    assert dm["cold_weighted_parameters"]==rm["cold_weighted_parameters"]
    assert dm["selector_completed"] and rm["selector_completed"]
    assert dm["selected_groups"]==rm["selected_groups"] and dm["selector_sha256"]==rm["selector_sha256"]


@pytest.mark.parametrize("changes",[
    dict(polish_initial_temperature=0.),dict(polish_minimum_temperature=.003),
    dict(polish_cooling_rate=float("nan")),dict(polish_cooling_rate=1.01),
    dict(polish_max_iterations=True),dict(polish_max_iterations=0)])
def test_invalid_cold_profile_never_opens_context(changes):
    problem,base=fixture();functions=originals()
    with pytest.raises(ValueError):
        driver.refine(problem,base,deadline=perf_counter()+1.,weights=WEIGHTS,scales=SCALES,**changes)
    assert originals()==functions
