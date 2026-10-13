"""Prespecified follow-up gates do not promote a short-only improvement."""
from copy import deepcopy

import pytest

from algorithm.experiments import run_regret_followup as protocol


def records(stage, variants=("baseline", "fallback", "portfolio", "old_allocation")):
    return [dict(stage=stage, case_id=case, seed=seed, variant=variant,
                 valid=True, budget_compliant=True, late_count=10 if variant == "baseline" else 9,
                 total_lateness_min=30., distance_km=80.,
                 budget_seconds=protocol.BUDGETS[stage][case.split("__")[0]],
                 initial_routes_sha256="a" * 64, scenario="pure_direct",
                 task_count=25 if case == "single25" else 200,
                 drone_count=1 if case == "single25" else 8)
            for case in protocol.CASES[stage] for seed in protocol.SEEDS[stage] for variant in variants]


def test_screen_selects_one_candidate_and_prefers_simpler_tie():
    assert protocol.choose_candidate(records("screen")) == "fallback"


def test_screen_does_not_promote_legacy_control():
    data=records("screen")
    for row in data:
        if row["variant"] in {"fallback", "portfolio"}: row["late_count"]=11
    assert protocol.choose_candidate(data) is None


def test_medium_requires_two_fleet_wins_and_single_no_regression():
    data=records("medium")
    assert protocol.medium_pass(data,"fallback")
    next(r for r in data if r["case_id"]=="single25" and r["variant"]=="fallback")["late_count"]=11
    assert not protocol.medium_pass(data,"fallback")


def test_confirmation_known_failure_and_all_three_new_seeds_are_required():
    data=records("confirm")
    assert protocol.confirm_pass(data,"fallback")
    assert not protocol.confirm_pass([r for r in data if r["seed"]!=2026101321],"fallback")
    for row in data:
        if row["seed"] == 2026101321 and row["variant"]=="fallback":
            row.update(late_count=11,total_lateness_min=0,distance_km=1)
    assert not protocol.confirm_pass(data,"fallback")


def test_confirmation_cannot_count_known_failure_seed_as_a_new_seed_win():
    data=records("confirm")
    for row in data:
        if row["variant"]=="fallback" and row["seed"] in {2026101362,2026101363}:
            row["late_count"]=10
    assert not protocol.confirm_pass(data,"fallback")


def test_incomplete_invalid_and_replaced_seeds_do_not_pass():
    data=records("screen")
    first=next(r for r in data if r["variant"]=="fallback")
    first["budget_compliant"]=False
    assert not protocol.screen_pass(data,"fallback")
    first["budget_compliant"]=True
    first["seed"]+=100
    assert not protocol.screen_pass(data,"fallback")


def test_different_initial_routes_cannot_be_paired():
    data=records("screen")
    next(r for r in data if r["variant"]=="fallback")["initial_routes_sha256"]="b"*64
    with pytest.raises(ValueError,match="配对"):
        protocol.screen_pass(data,"fallback")


def test_supplement_requires_recorded_parent_failure_and_finished_batch(tmp_path):
    from algorithm.experiments import run_regret_portfolio_validation as supplement
    output=tmp_path / "supplemental_trial"
    data=records("screen")+records("medium")
    next(r for r in data if r["stage"]=="medium" and r["variant"]=="fallback")["late_count"]=11
    protocol.base.write_json(tmp_path / "runs.json",data)
    protocol.base.write_json(tmp_path / "decisions.json",[
        {"stage":"medium","selected_variant":"fallback","passed":False}])
    with pytest.raises(ValueError,match="先结束"):
        supplement.parent_evidence(output)
    protocol.base.write_json(tmp_path / "decisions.json",[
        {"stage":"medium","selected_variant":"fallback","passed":False},
        {"stage":"extensions"}])
    assert supplement.parent_evidence(output)==data


def test_supplement_budget_includes_both_cohorts_aborts_and_probe(tmp_path):
    from algorithm.experiments import run_regret_portfolio_validation as supplement
    output=tmp_path / "supplemental_trial"
    for directory, elapsed in ((tmp_path,100),(output,200)):
        protocol.base.write_json(directory / "runs.json",[{"end_to_end_seconds":elapsed}])
        protocol.base.write_json(directory / "execution_notes.json",{
            "aborted_runs":[{"process_elapsed_seconds":10}]})
    protocol.base.write_json(tmp_path / "diagnostics/local_repair_probe.json",{"elapsed_seconds":.5})
    assert supplement.consumed(output)==320.5


def test_supplement_reserves_whole_phase_before_starting_a_solve(tmp_path, monkeypatch):
    from algorithm.experiments import run_regret_portfolio_validation as supplement
    protocol.base.write_json(tmp_path / "runs.json",[])
    monkeypatch.setattr(supplement,"verify",lambda *args:None)
    monkeypatch.setattr(supplement,"consumed",lambda *args:3500.)
    monkeypatch.setattr(protocol,"stage",lambda *args:pytest.fail("budget exhausted"))
    with pytest.raises(RuntimeError,match="合计预算不足"):
        supplement.stage(tmp_path,{},"medium")


def test_confirmation_stop_preserves_existing_abort_evidence(tmp_path, monkeypatch):
    data=records("confirm", ("baseline","fallback"))
    next(r for r in data if r["variant"]=="fallback")["late_count"]=11
    protocol.base.write_json(tmp_path / "runs.json",data)
    notes={"aborted_runs":[{"stage":"screen","process_elapsed_seconds":12.}], "diagnostic":"keep"}
    protocol.base.write_json(tmp_path / "execution_notes.json",notes)
    monkeypatch.setattr(protocol,"verify",lambda *args:None)
    protocol.stage(tmp_path,{},"confirm",["baseline","fallback"])
    saved=protocol.base.read_json(tmp_path / "execution_notes.json")
    assert saved["aborted_runs"]==notes["aborted_runs"]
    assert saved["diagnostic"]=="keep" and saved["early_stop"]


def test_stage_budget_charges_aborted_work_before_starting_more(tmp_path, monkeypatch):
    protocol.base.write_json(tmp_path / "runs.json",[])
    protocol.base.write_json(tmp_path / "execution_notes.json",{"aborted_runs":[
        {"stage":"medium","case_id":"fleet200","seed":0,"variant":"baseline",
         "process_elapsed_seconds":protocol.CAPS["medium"]}]})
    monkeypatch.setattr(protocol,"verify",lambda *args:None)
    monkeypatch.setattr(protocol.base,"invoke",lambda *args:pytest.fail("stage already exhausted"))
    with pytest.raises(RuntimeError,match="预算不足"):
        protocol.stage(tmp_path,{},"medium",["baseline","fallback"])
