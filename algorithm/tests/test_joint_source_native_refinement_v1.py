"""Thin alias adaptation and a paid real-weighted joint-source tiny case."""
from dataclasses import replace
from inspect import signature
from math import hypot
from time import perf_counter

import pytest

from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import joint_source_native_refinement_v1 as adapter
from algorithm.tests.test_relay_preserving_polish import fixture as base_fixture, real_weighted_runtime
from algorithm.tests.test_relay_repooling_exchange import topology

W, S = (.5, .25, .25), (9., 100., 100.)


def fixture():
    problem, base = base_fixture(direct=True)
    # One actual same-C pair; unrelated ordinary tasks remain free to repair.
    problem = replace(problem, task_relay_candidates={t: (1,) if t in (1, 2) else ()
        for t in problem.task_ids}, leg_registry={identifier:leg for identifier,leg
            in problem.leg_registry.items() if leg.task_id in (1,2)})
    return problem, base


def run(module, problem, base, **kwargs):
    return adapter.refine(problem, base, deadline=kwargs.pop('deadline', perf_counter()+3.),
        weights=W, scales=S, seed=41, max_groups=1, beam_width=2, max_families=2,
        source_option_limit=2, receiver_option_limit=1, position_limit=3,
        snapshot_limit=3, polish_max_iterations=2, alns_module=module, **kwargs)


def alias_values():
    return tuple(module.structure for module in adapter._aliases())


def assert_receipt(receipt):
    assert receipt['name'] == adapter.NAME and receipt['source_policy'] == adapter.SOURCE_POLICY
    assert receipt['alias_names'] == list(adapter.ALIAS_NAMES) and receipt['restored']
    assert receipt['hook_identity'] == {name: dict(installed=True, restored=True)
        for name in adapter.ALIAS_NAMES}
    assert receipt['left_at_perf_counter'] >= receipt['entered_at_perf_counter']
    assert receipt['elapsed_seconds'] == receipt['left_at_perf_counter']-receipt['entered_at_perf_counter']


def test_explicit_frozen_signature_passthrough_and_core_metadata_not_relabelled(monkeypatch):
    assert signature(adapter.refine) == signature(adapter.wrapper.refine)
    originals = alias_values();observed={};core_meta=dict(started_at_perf_counter=12., runtime_seconds=3.,
        method='frozen actual method', configuration={'raw_fraction': .4})
    marker = object();result=dict(routes=marker, evaluation=marker, metadata=core_meta, iterations=7)
    def core(problem, routes, **kwargs):
        assert all(value is adapter.joint_structure for value in alias_values())
        observed.update(problem=problem, routes=routes, kwargs=kwargs)
        return result
    monkeypatch.setattr(adapter.wrapper, 'refine', core)
    got=adapter.refine(marker, marker, deadline=20., weights=W, scales=S, seed=3,
        raw_fraction=.3, max_groups=2, beam_width=3, max_families=4,
        source_option_limit=2, receiver_option_limit=2, position_limit=5,
        snapshot_limit=4, polish_max_iterations=9, polish_initial_temperature=.01,
        polish_minimum_temperature=.0001, polish_cooling_rate=.95, alns_module=marker)
    assert got is result and got['metadata'] is core_meta and got['routes'] is marker
    assert observed['problem'] is marker and observed['routes'] is marker
    assert observed['kwargs'] == dict(deadline=20., weights=W, scales=S, seed=3, relay_enabled=True,
        raw_fraction=.3,max_groups=2,beam_width=3,max_families=4,source_option_limit=2,
        receiver_option_limit=2,position_limit=5,snapshot_limit=4,polish_max_iterations=9,
        polish_initial_temperature=.01,polish_minimum_temperature=.0001,
        polish_cooling_rate=.95,alns_module=marker)
    assert {k:v for k,v in core_meta.items() if k!='extension'} == dict(started_at_perf_counter=12.,
        runtime_seconds=3.,method='frozen actual method',configuration={'raw_fraction':.4})
    assert_receipt(core_meta['extension'])
    assert alias_values()==originals and not adapter._active


@pytest.mark.parametrize('error', [ValueError, adapter.wrapper.Deadline])
def test_exception_and_deadline_restore_every_actual_alias(error):
    originals=alias_values();receipt={}
    with pytest.raises(error):
        with adapter.joint_structure_context(statistics=receipt):
            assert all(value is adapter.joint_structure for value in alias_values())
            raise error('controlled boundary')
    assert alias_values()==originals and not adapter._active
    assert_receipt(receipt)


def test_nested_context_rejected_without_releasing_outer_aliases():
    originals=alias_values();receipt={}
    with adapter.joint_structure_context(statistics=receipt):
        with pytest.raises(RuntimeError,match='nestable'):
            with adapter.joint_structure_context(): pass
        assert adapter._active and all(value is adapter.joint_structure for value in alias_values())
    assert alias_values()==originals and not adapter._active
    assert_receipt(receipt)


def test_wrong_deadline_identity_rejected_before_any_alias_is_changed(monkeypatch):
    originals=alias_values()
    monkeypatch.setattr(adapter.joint_structure,'Deadline',type('OtherDeadline',(Exception,),{}))
    with pytest.raises(RuntimeError,match='Deadline identity'):
        with adapter.joint_structure_context(): pass
    assert alias_values()==originals and not adapter._active


def test_direct_mode_rejected_before_context_or_paid_core(monkeypatch):
    monkeypatch.setattr(adapter.wrapper,'refine',lambda *a,**k:(_ for _ in ()).throw(AssertionError('core started')))
    originals=alias_values()
    with pytest.raises(ValueError,match='R-only'):
        adapter.refine(None,(),deadline=perf_counter()+1.,weights=W,scales=S,seed=1,relay_enabled=False)
    assert alias_values()==originals and not adapter._active


def test_expired_entry_keeps_frozen_none_checkpoint_semantics_and_restores():
    problem,base=fixture();originals=alias_values()
    result=run(None,problem,base,deadline=perf_counter()-1.)
    meta=result['metadata']
    assert result['routes']==base and result['evaluation'] is None
    assert meta['input_snapshot'] is None and meta['final_snapshot'] is None
    assert meta['raw_metadata'] is None and not meta['seed_selection_completed']
    assert not meta['returned_checkpoint_checked_within_deadline']
    assert_receipt(meta['extension'])
    assert alias_values()==originals and not adapter._active


def test_paid_joint_raw_real_weighted_native_and_independent_typed_callback(real_weighted_runtime,monkeypatch):
    problem,base=fixture();module=real_weighted_runtime;originals=alias_values()
    before={name:getattr(module,name) for name in adapter.wrapper.polish.PATCH_NAMES}
    calls=[];paid=adapter.wrapper.raw_method.refine
    def actual_paid(*args,**kwargs):
        calls.append((args,kwargs));return paid(*args,**kwargs)
    monkeypatch.setattr(adapter.wrapper.raw_method,'refine',actual_paid)
    result=run(module,problem,base);meta=result['metadata']
    assert len(calls)==1 and calls[0][0][1]==base
    assert calls[0][1]['deadline']==meta['raw_deadline_perf_counter']
    assert meta['seed_records'] and len(meta['polish_records'])==1
    seed=meta['seed_records'][0];raw=meta['raw_metadata']
    for field in seed['source_path']: raw=raw[field]
    group=raw['groups'][0]
    assert group['source_type']=='joint_pair' and group['a']==group['b']!=group['c']
    assert {group['i'],group['j']}=={1,2}
    helper=meta['polish_records'][0]['metadata']
    assert helper['iterations']==2 and helper['destroy_calls']==helper['repair_calls']==2
    assert helper['patches_restored'] and helper['objective_state_restored'] and helper['score_comparators_restored']
    # This is actual source co-load flight, separate from receiver handoff metrics.
    routes=tuple(map(tuple,raw['routes']));index=build_plan_index(problem,routes)
    incoming={identifier for plan in index.task_plans.values() if plan.task_id in (1,2)
        for identifier in plan.leg_ids if problem.leg_registry[identifier].kind=='RELAY_IN'}
    previous=problem.home_points()[group['a']];held=set();shared=0.
    for visit in routes[group['a']]:
        point=problem.point_for_visit(visit)
        if incoming.issubset(held): shared+=hypot(point.x-previous.x,point.y-previous.y)
        if visit>0:held.add(visit)
        else:held.remove(-visit)
        previous=point
    assert shared>1e-10
    schedule=GlobalRelayEvaluator(problem).evaluate(routes)
    assert schedule.feasible and evaluate_solution(problem,routes,_force_full=True).valid
    assert result['evaluation'].valid and tuple(meta['final_key'])<=tuple(meta['initial_key'])
    assert meta['extension']['entered_at_perf_counter']<=meta['started_at_perf_counter']
    assert meta['started_at_perf_counter']+meta['runtime_seconds']<=meta['extension']['left_at_perf_counter']
    assert_receipt(meta['extension'])
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in problem.tasks}
    graph=topology(problem);graph['task_relay_candidates']={str(t):list(problem.task_relay_candidates[t]) for t in problem.task_ids}
    params={**meta['configuration']['raw_parameters'],'raw_fraction':.4,'polish_max_iterations':2,
        'polish_initial_temperature':.0025,'polish_minimum_temperature':.00001,'polish_cooling_rate':.97}
    manifest=dict(weights=W,scales=S,method_parameters={'multi_native_relay':params},
        accumulation_fixed_catalogue_parameters=dict(feeder_limit=3,base_pool_limit=64))
    from algorithm.experiments.audit_joint_source_native_v1 import _audit_metadata
    proof=_audit_metadata(tasks,graph,base,meta,relay=True,manifest=manifest)
    assert proof['passed'] and proof['joint_source_extension_evidence']['unique_current_joint_source_proofs']
    assert all(row['shared_in_loaded_distance_km']>1e-10 for row in
        proof['joint_source_extension_evidence']['unique_current_joint_source_proofs'])
    assert all(getattr(module,name) is fn for name,fn in before.items())
    assert not module._weighted_adapter.active and module._weighted_adapter.coefficients is None
    assert alias_values()==originals and not adapter._active
