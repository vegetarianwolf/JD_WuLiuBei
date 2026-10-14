"""Finite joint assignment, same-coverage clocks and actual outside donors."""
from itertools import permutations
from time import perf_counter
import json

import pytest
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from algorithm.experiments import damage_closure_repair as method
from algorithm.tests.test_relay_damage_closure import fixture
from algorithm.tests.test_relay_repooling_exchange import topology

W,S=(.5,.25,.25),(15.,100.,100.)


def move():return dict(family=dict(i=1,j=2,a=0,b=1,c=2,k1=3,k2=4,station_id=1,task_ids=[1,2,3,4]))


def run(p,base,raw,**kwargs):
    return method.repair_damage_closure(p,base,raw,original_macro_move=move(),deadline=perf_counter()+3.,
        weights=W,scales=S,closure_limit=8,position_limit=8,max_rounds=1,**kwargs)


@pytest.mark.parametrize('direct',[False,True])
def test_actual_all_eight_donor_quota_assignment_cycles_and_same_coverage_global_wait(direct):
    from algorithm.experiments.audit_window_damage_repair import audit_window_damage_partial,audit_window_damage_closure
    p,base,raw=fixture(direct=direct)
    result=run(p,base,raw,relay_enabled=not direct);m=result['metadata']
    assert result['evaluation'].valid and m['complete_valid_candidates']>0
    assert m['global_incremental_evaluations']>0 and m['joint_pair_expansions']>0
    record=m['closure_records'][0];assert record['closure']['actual_opened_aircraft']==list(range(8))
    assert record['quota_slots_by_aircraft']==[1]*8
    assert any('cycle' in a['name'] for a in record['owner_assignments'])
    assert any(len(c['owner_change_outside_macro_aircraft_task_ids'])>=5 for c in record['complete_checkpoints'])
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in p.tasks};graph=topology(p)
    proof=audit_window_damage_closure(tasks,graph,record['before_routes'],record['closure'],pinned_task_ids=(1,2))
    assert proof['passed']
    for layer in record['layers']:
        for state in layer['beam_state_snapshots']:
            assert set(map(int,state['delivery_times_min']))==set(layer['present_task_ids'])
            proof=audit_window_damage_partial(tasks,graph,state['routes'],layer['present_task_ids'],telemetry=state,
                weights=W,scales=S,target_final_counts=record['closure']['raw_final_counts'])
            assert proof['passed']
    for checkpoint in record['complete_checkpoints']:
        s=checkpoint['full_snapshot'];routes=tuple(tuple(r) for r in s['routes'])
        full=method._validate(p,routes,frozenset((1,2)),method._signature(p,raw,(1,2)),frozenset() if direct else frozenset((1,2)))
        assert list(method.weighted_key(full.score,W,S))==s['key']
        assert checkpoint['full_verified_at_perf_counter']<m['deadline_perf_counter']
    assert m['post_repair_snapshot'] is not None and 'table_score' not in m['post_repair_snapshot']
    json.dumps(m,allow_nan=False)


def test_pin_shape_allows_source_load_one_and_native_between_out_deliveries():
    p,base,raw=fixture();pins=frozenset((1,2));expected=method._signature(p,raw,pins)
    changed=(raw[0],raw[1],(102,104,-102,9,-9,-104,10,-10),*raw[3:])
    full=method._validate(p,changed,pins,expected,pins)
    assert full.valid
    assert method._signature(p,changed,pins)==expected
    with pytest.raises(ValueError):
        method._validate(p,(raw[0],raw[1],(102,9,-9,104,-102,-104,10,-10),*raw[3:]),pins,expected,pins)


def test_slot_assignment_proposal_and_cycles_have_exact_counts_not_additive_cost_claim():
    matrix=[[8.,2.,4.],[3.,9.,1.],[1.,7.,5.]]
    result=method._hungarian(matrix)
    assert sum(matrix[t][a] for t,a in enumerate(result))==min(sum(matrix[t][a] for t,a in enumerate(order)) for order in permutations(range(3)))
    tasks=(1,2,3,4);owners={1:0,2:1,3:2,4:3};cost={(t,a):float(abs(t-a-1)) for t in tasks for a in range(4)}
    plans=method._assignments(tasks,(1,1,1,1),owners,cost)
    assert any(name=='all_opened_forward_cycle' for name,_ in plans)
    assert any(all(assignment[t]!=owners[t] for t in tasks) for _,assignment in plans)
    assert all(sorted(assignment.values())==[0,1,2,3] for _,assignment in plans)


def test_invalid_complete_leaf_is_rejected_and_preserves_verified_raw(monkeypatch):
    p,base,raw=fixture()
    monkeypatch.setattr(method,'_validate',lambda *a,**k:(_ for _ in ()).throw(ValueError('controlled bad full leaf')))
    result=run(p,base,raw,relay_enabled=True)
    assert result['routes']==raw and result['metadata']['rejected_complete_leaves']>0
    assert not result['metadata']['complete_valid_candidates']
    assert result['metadata']['post_repair_snapshot'] is result['metadata']['input_snapshot']


def test_snapshot_overrun_returns_previous_predeadline_checkpoint_without_recomputation(monkeypatch):
    p,base,raw=fixture();clock=[perf_counter()];deadline=clock[0]+3.;snap=method._snapshot;phases=[]
    monkeypatch.setattr(method,'perf_counter',lambda:clock[0])
    def late(*args,**kwargs):
        phases.append(kwargs['phase']);value=snap(*args,**kwargs)
        if kwargs['phase']=='damage_complete_candidate':clock[0]=deadline+.1
        return value
    monkeypatch.setattr(method,'_snapshot',late)
    result=method.repair_damage_closure(p,base,raw,original_macro_move=move(),relay_enabled=True,
        deadline=deadline,weights=W,scales=S,closure_limit=8,max_rounds=1)
    m=result['metadata']
    assert result['routes']==raw and m['deadline_reached'] and m['returned_checkpoint_checked_within_deadline']
    assert m['post_repair_snapshot'] is m['input_snapshot'] and phases.count('damage_macro_input')==1
    assert not m['full_candidate_snapshots']


def test_expired_initial_budget_has_no_new_checkpoint():
    p,base,raw=fixture()
    result=method.repair_damage_closure(p,base,raw,original_macro_move=move(),relay_enabled=True,
        deadline=perf_counter()-1.,weights=W,scales=S)
    assert result['routes']==raw and not result['metadata']['returned_checkpoint_checked_within_deadline']


@pytest.mark.parametrize('change',[dict(beam_width=5),dict(option_count=3),dict(global_option_probe_limit=0)])
def test_invalid_joint_bounds(change):
    p,base,raw=fixture()
    with pytest.raises(ValueError):run(p,base,raw,relay_enabled=True,**change)
