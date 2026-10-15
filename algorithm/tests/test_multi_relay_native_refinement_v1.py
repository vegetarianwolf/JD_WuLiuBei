"""Paid synthetic raw construction and real weighted native-polish boundary."""
from copy import deepcopy
from time import perf_counter

import pytest

from uav_dispatch.relay import build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import multi_relay_native_refinement_v1 as wrapper
from algorithm.experiments.audit_multi_relay_accumulation_v1 import audit_metadata as audit_raw
from algorithm.experiments.audit_multi_relay_native_polish_v1 import audit_metadata as audit_polish, audit_complete_state
from algorithm.tests.test_multi_relay_native_polish_v1 import fixture as model_fixture
from algorithm.tests.test_relay_preserving_polish import real_weighted_runtime
from algorithm.tests.test_relay_repooling_exchange import topology

W, S = (.5, .25, .25), (9., 100., 100.)


def fixture():
    p, _, _ = model_fixture()
    base = ((3, 4, -3, -4, 7, -7), (5, -5, 6, -6, 8, -8), (1, 2, -1, -2, 9, -9))
    assert evaluate_solution(p, base, _force_full=True).valid
    return p, base


def run(module, p, base, **kwargs):
    return wrapper.refine(p, base, deadline=kwargs.pop('deadline', perf_counter()+3.), weights=W, scales=S,
        seed=31, max_groups=2, beam_width=2, max_families=2, source_option_limit=1,
        receiver_option_limit=1, position_limit=3, snapshot_limit=3,
        polish_max_iterations=2, alns_module=module, **kwargs)


def resolve(root, path):
    for field in path: root = root[field]
    return root


def test_paid_raw_real_weighted_group_seeds_and_all_current_public_proofs(real_weighted_runtime, monkeypatch):
    p, base = fixture();module=real_weighted_runtime;calls=[];original=wrapper.raw_method.refine
    def paid(*args, **kwargs):
        calls.append((args, kwargs));return original(*args, **kwargs)
    monkeypatch.setattr(wrapper.raw_method, 'refine', paid)
    before={n:getattr(module,n) for n in wrapper.polish.PATCH_NAMES}
    result=run(module,p,base);meta=result['metadata']
    assert len(calls)==1 and calls[0][0][1]==base and calls[0][1]['relay_enabled'] is True
    assert calls[0][1]['deadline']==meta['raw_deadline_perf_counter']<meta['deadline_perf_counter']
    assert meta['seed_selection_completed'] and meta['seed_records']
    assert [s['group_count'] for s in meta['seed_records']]==[1,2]
    assert all('raw_snapshot' not in s and 'source_paths' not in s for s in meta['seed_records'])
    assert len({s['routes_sha256'] for s in meta['seed_records']})==len(meta['seed_records'])
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in p.tasks}
    graph=topology(p);graph['task_relay_candidates']={str(t):list(p.task_relay_candidates[t]) for t in p.task_ids}
    assert audit_raw(tasks,graph,base,meta['raw_metadata'],weights=W,scales=S,deadline=meta['raw_deadline_perf_counter'])['passed']
    for number,(seed,entry) in enumerate(zip(meta['seed_records'],meta['polish_records'])):
        raw=resolve(meta['raw_metadata'],seed['source_path'])
        assert wrapper.raw_method.metrics._digest(raw)==seed['raw_snapshot_sha256']
        assert wrapper.raw_method.metrics._digest(raw['routes'])==seed['routes_sha256']
        assert entry['groups']==raw['groups']==seed['groups']
        assert entry['deadline_perf_counter']==entry['allocation_started_at_perf_counter']+max(0.,meta['deadline_perf_counter']-.15-entry['allocation_started_at_perf_counter'])/entry['remaining_channels']
        helper=entry['metadata'];cfg=helper['configuration']
        assert [cfg[k] for k in ('max_iterations','initial_temperature','minimum_temperature','cooling_rate')]==[2,.0025,.00001,.97]
        assert cfg['seed']==31+number and cfg['relay_enabled'] and not cfg['enable_budget_cooling']
        assert helper['iterations']==2 and helper['patches_restored']
        assert audit_polish(tasks,graph,raw['routes'],raw['groups'],helper,weights=W,scales=S,deadline=entry['deadline_perf_counter'])['passed']
        for saved in (x for x in meta['full_candidate_snapshots'] if x['phase'].endswith('_'+str(number))):
            assert audit_complete_state(tasks,graph,raw['routes'],raw['groups'],saved['routes'],
                telemetry=saved,weights=W,scales=S,deadline=meta['deadline_perf_counter'])['passed']
    assert tuple(meta['final_key'])<=tuple(meta['initial_key']) and result['evaluation'].valid
    assert all(getattr(module,n) is f for n,f in before.items())
    assert not module._weighted_adapter.active and module._weighted_adapter.coefficients is None
    from algorithm.experiments.audit_multi_relay_native_batch_v1 import _audit_metadata
    params={**meta['configuration']['raw_parameters'], 'raw_fraction':.4,
        'polish_max_iterations':2,'polish_initial_temperature':.0025,
        'polish_minimum_temperature':.00001,'polish_cooling_rate':.97}
    manifest=dict(weights=W,scales=S,method_parameters={'multi_native_relay':params},
        accumulation_fixed_catalogue_parameters=dict(feeder_limit=3,base_pool_limit=64))
    assert _audit_metadata(tasks,graph,base,meta,relay=True,manifest=manifest)['passed']
    # A checked checkpoint remains eligible even if its raw depth never
    # finished publishing a beam. This is static selection, not a free seed.
    seed=meta['seed_records'][-1];raw=resolve(meta['raw_metadata'],seed['source_path'])
    interrupted=dict(first_complete_candidate=None,best_constructed_candidate=None,post_repair_snapshot=None,
        full_candidate_snapshots=[],steps=[dict(completed=False,retained_states=[],complete_checkpoints=[raw])])
    selected=wrapper._seeds(interrupted,max_groups=2,raw_deadline=meta['raw_deadline_perf_counter'],
        weights=W,scales=S,deadline=perf_counter()+1.,statistics={})
    assert selected[0]['group_count']==2 and selected[0]['source_path']==['steps',0,'complete_checkpoints',0]
    damaged=deepcopy(interrupted);damaged['steps'][0]['complete_checkpoints'][0]['complete']=False
    with pytest.raises(ValueError,match='complete'): wrapper._seeds(damaged,max_groups=2,
        raw_deadline=meta['raw_deadline_perf_counter'],weights=W,scales=S,deadline=perf_counter()+1.,statistics={})


def test_entry_expired_does_not_start_raw_or_fabricate_a_checkpoint(real_weighted_runtime,monkeypatch):
    p,base=fixture()
    monkeypatch.setattr(wrapper.raw_method,'refine',lambda *a,**k:(_ for _ in ()).throw(AssertionError('raw must be paid only after live entry')))
    result=run(real_weighted_runtime,p,base,deadline=perf_counter()-1.)
    assert result['routes']==base and result['evaluation'] is None
    assert not result['metadata']['initialization_completed'] and result['metadata']['input_snapshot'] is None
    assert result['metadata']['raw_metadata'] is None and result['metadata']['seed_records']==[]


def test_helper_initialization_expiry_skips_new_consideration_without_a_late_certificate(real_weighted_runtime,monkeypatch):
    p,base=fixture();original=wrapper.polish.polish_groups
    def expired(*args,**kwargs):
        now=kwargs['deadline']+.001
        monkeypatch.setattr(wrapper,'perf_counter',lambda:now)
        with monkeypatch.context() as scope:
            scope.setattr(wrapper.polish,'perf_counter',lambda:now)
            return original(*args,**kwargs)
    monkeypatch.setattr(wrapper.polish,'polish_groups',expired)
    result=run(real_weighted_runtime,p,base);meta=result['metadata']
    assert meta['polish_records']
    for entry in meta['polish_records']:
        assert entry['metadata']['post_repair_snapshot'] is None and not entry['metadata']['returned_checkpoint_checked_within_deadline']
        assert entry['post_consideration_skipped']=='no_helper_predeadline_complete_checkpoint'
    assert all(s['phase'].startswith('raw_seed_') for s in meta['full_candidate_snapshots'])


@pytest.mark.parametrize('forgery',['clone','late'])
def test_helper_return_must_be_its_existing_matching_preleaf_checkpoint(real_weighted_runtime,monkeypatch,forgery):
    p,base=fixture();original=wrapper.polish.polish_groups
    def false_certificate(*args,**kwargs):
        result=original(*args,**kwargs);meta=result['metadata']
        assert meta['returned_checkpoint_checked_within_deadline']
        if forgery=='clone':meta['post_repair_snapshot']=deepcopy(meta['post_repair_snapshot'])
        else:meta['post_repair_snapshot']['full_verified_at_perf_counter']=kwargs['deadline']
        return result
    monkeypatch.setattr(wrapper.polish,'polish_groups',false_certificate)
    with pytest.raises(ValueError,match='cached'):run(real_weighted_runtime,p,base)


def test_outer_full_validation_overrun_preserves_original_guard(real_weighted_runtime,monkeypatch):
    p,base=fixture();deadline=perf_counter()+3.;clock=[perf_counter()];original=wrapper.evaluate_solution;calls=[0]
    monkeypatch.setattr(wrapper,'perf_counter',lambda:clock[0])
    def overrun(*args,**kwargs):
        checked=original(*args,**kwargs);calls[0]+=1
        if calls[0]==2:clock[0]=deadline+.001
        return checked
    monkeypatch.setattr(wrapper,'evaluate_solution',overrun)
    result=run(real_weighted_runtime,p,base,deadline=deadline);meta=result['metadata']
    assert meta['deadline_reached'] and result['routes']==base and not meta['accepted_moves']
    assert meta['full_candidate_snapshots']==[] and meta['polish_records']==[]
    assert meta['final_snapshot']==meta['input_snapshot']


def test_expiry_after_raw_consideration_does_not_publish_an_overdue_allocation(real_weighted_runtime,monkeypatch):
    p,base=fixture();deadline=perf_counter()+3.;actual_clock=wrapper.perf_counter
    actual_snapshot=wrapper.polish._snapshot;captured={}
    def capture(*args,**kwargs):
        snapshot=actual_snapshot(*args,**kwargs)
        captured['statistics']=args[10]
        return snapshot
    def clock():
        stats=captured.get('statistics')
        return deadline+.001 if stats and stats['full_candidate_snapshots'] else actual_clock()
    monkeypatch.setattr(wrapper.polish,'_snapshot',capture)
    monkeypatch.setattr(wrapper,'perf_counter',clock)
    monkeypatch.setattr(wrapper.polish,'polish_groups',lambda *a,**k:(_ for _ in ()).throw(AssertionError('overdue allocation started helper')))
    result=run(real_weighted_runtime,p,base,deadline=deadline);meta=result['metadata']
    assert len(meta['full_candidate_snapshots'])==1
    raw=meta['full_candidate_snapshots'][0]
    assert tuple(raw['key'])>=tuple(meta['initial_key'])  # no adoption branch took the deadline instead
    assert raw['full_verified_at_perf_counter']<deadline and raw['phase']=='raw_seed_0'
    assert meta['deadline_reached'] and meta['polish_records']==[] and meta['accepted_moves']==[]
    assert result['routes']==base and meta['final_snapshot']==meta['input_snapshot']


@pytest.mark.parametrize('kwargs',[dict(relay_enabled=False),dict(raw_fraction=0),dict(polish_minimum_temperature=.1)])
def test_invalid_mode_or_profile_is_rejected_before_paid_raw(real_weighted_runtime,kwargs):
    p,base=fixture()
    with pytest.raises(ValueError):run(real_weighted_runtime,p,base,**kwargs)
