"""Dynamic responsibility, joint cargo cost, complete pins and timeout guards."""
from time import perf_counter

import pytest
from uav_dispatch import Point, Problem, Task
from uav_dispatch.relay import build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import critical_prefix_joint_repair_v1 as method
from algorithm.tests.test_relay_repooling_exchange import topology
from algorithm.tests.test_split_feeder_refinement_v4 import separated_fixture

W, S = (.5, .25, .25), (4., 100., 100.)


def direct_fixture():
    homes=tuple(Point(float(x)*10.,0.) for x in range(4))
    p=Problem(tuple(Task(t,homes[a],homes[a],100.) for t,a in enumerate((2,0,3,1),1)),
        drone_count=4,capacity=2,max_tasks_per_drone=1,speed_km_per_min=1.,drone_homes=homes)
    return p,tuple((t,-t) for t in range(1,5))


def run(p,base,raw=None,**kwargs):
    return method.refine(p,base,base if raw is None else raw,deadline=perf_counter()+4.,weights=W,scales=S,
        relay_enabled=False,focus_task_ids=(1,2),closure_limit=4,beam_width=6,position_limit=4,
        option_count=1,max_rounds=1,**kwargs)


def audit(p,base,raw,result,move=None,relay=False,check_closure=True):
    from algorithm.experiments.audit_critical_prefix_joint_repair_v1 import audit_repair,audit_partial_state,audit_closure
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in p.tasks}
    graph=topology(p);graph['task_relay_candidates']={str(t):list(p.task_relay_candidates.get(t,())) for t in p.task_ids}
    m=result['metadata'];pins=m['pinned_task_ids'];states={}
    for snapshot in [m['input_snapshot'],m['post_repair_snapshot'],*m['full_candidate_snapshots'],m['first_complete_candidate'],m['best_constructed_candidate']]:
        if snapshot is not None:
            assert audit_repair(tasks,graph,base,raw,snapshot['routes'],move,relay_enabled=relay,
                telemetry=snapshot,weights=W,scales=S,focus_task_ids=m['focus_task_ids'])['passed']
    for record in m['closure_records']:
        built=record['closure']
        if check_closure:
            assert audit_closure(tasks,graph,base,record['before_routes'],built,pinned_task_ids=pins,
                relay_enabled=relay,seed_move=move,weights=W,scales=S)['passed']
        initial=record['initial_state'];states[initial['state_id']]=initial
        rows=[initial,*[state for layer in record['layers'] for state in layer['retained_states']]]
        for state in rows:
            parent=states.get(state['parent_state_id'])
            assert audit_partial_state(tasks,graph,state['routes'],state['present_task_ids'],
                target_final_counts=built['raw_final_counts'],raw_routes=raw,pinned_task_ids=pins,
                relay_enabled=relay,seed_move=move,telemetry=state,weights=W,scales=S,
                remaining_task_ids=state['remaining_task_ids'],parent_routes=parent['routes'] if parent else None,
                parent_present_task_ids=parent['present_task_ids'] if parent else None,
                inserted_task_order=state['inserted_task_order'],owner_choices=state['owner_choices'],
                insertions=state['insertions'])['passed']
            states[state['state_id']]=state
        for checkpoint in record['complete_checkpoints']:
            parent=states[checkpoint['parent_state_id']]
            proof=audit_repair(tasks,graph,base,raw,checkpoint['routes'],move,relay_enabled=relay,
                weights=W,scales=S,focus_task_ids=m['focus_task_ids'])
            assert proof['passed'] and checkpoint['full_verified_at_perf_counter']<m['deadline_perf_counter']
            assert audit_partial_state(tasks,graph,checkpoint['routes'],p.task_ids,
                target_final_counts=built['raw_final_counts'],raw_routes=raw,pinned_task_ids=pins,
                relay_enabled=relay,seed_move=move,weights=W,scales=S,remaining_task_ids=[],
                parent_routes=parent['routes'],parent_present_task_ids=parent['present_task_ids'],
                inserted_task_order=checkpoint['inserted_task_order'],owner_choices=checkpoint['owner_choices'],
                insertions=checkpoint['insertions'])['passed']


def test_dynamic_zero_pin_direct_moves_focus_and_executes_nonfixed_owner_cycle_with_real_quota():
    p,base=direct_fixture();result=run(p,base);m=result['metadata']
    assert result['evaluation'].valid and result['evaluation'].score.distance_km==0.
    index=build_plan_index(p,result['routes'])
    assert index.delivery_route_by_task=={1:2,2:0,3:3,4:1}
    assert not m['pinned_task_ids'] and set(m['post_repair_snapshot']['changed_owner_task_ids'])==set(p.task_ids)
    assert all(sum(a==owner for owner in index.delivery_route_by_task.values())==1 for a in range(4))
    first=m['closure_records'][0]['layers'][0]
    assert len(first['owner_branches'])==12 and first['completed']
    assert not m['configuration']['first_partial_J_pruning']
    assert m['complete_leafs_full_verified']>0 and not m['deadline_reached']
    audit(p,base,base,result)


def test_same_owner_two_slots_joint_cargo_has_true_cost_below_independent_insertions(monkeypatch):
    p=Problem((Task(1,Point(0.,0.),Point(1.,0.),100.),Task(2,Point(0.,0.),Point(2.,0.),100.),
        Task(3,Point(10.,0.),Point(10.,0.),100.)),drone_count=2,capacity=2,max_tasks_per_drone=2,
        speed_km_per_min=1.,drone_homes=(Point(0.,0.),Point(10.,0.)))
    base=((1,-1,2,-2),(3,-3));expected=method.closure.signature(p,base,())
    # Selection is controlled, but every released slot, partial DAG, insertion
    # and full leaf remains actual; no score or capacity result is stubbed.
    def all_real_slots(*args,**kwargs):
        return dict(removed_task_ids=[1,2,3],partial_routes=[[],[]],quota_slots_by_aircraft=[2,1],
            raw_final_counts=expected['final_counts'])
    monkeypatch.setattr(method.closure,'build_closure',all_real_slots)
    result=run(p,base);m=result['metadata'];first=m['closure_records'][0]['layers'][0]
    assert first['owner_branches']['0,0']>0
    assert result['evaluation'].valid and result['evaluation'].score.distance_km==2.
    assert 2.<1.+2.  # Actual co-carried route, versus two separate empty-route costs.
    assert all(q==0 for q in m['closure_records'][0]['layers'][-1]['retained_states'][0]['remaining_quota_slots'])
    audit(p,base,base,result,check_closure=False)


def test_actual_split_seed_keeps_only_two_relay_identities_with_native_between_deliveries():
    p,base=separated_fixture()
    raw=method.raw_tools.refine(p,base,deadline=perf_counter()+3.,weights=W,scales=S,
        relay_enabled=True,max_families=1,base_pool_limit=4)
    macro=next(s for s in raw['metadata']['full_candidate_snapshots'] if tuple(tuple(r) for r in s['routes'])==raw['routes'])
    result=method.refine(p,base,raw['routes'],seed_move=macro['move'],focus_task_ids=(1,2),
        deadline=perf_counter()+4.,weights=W,scales=S,relay_enabled=True,closure_limit=3,
        beam_width=2,position_limit=3,option_count=1,max_rounds=1)
    assert result['evaluation'].valid and set(build_plan_index(p,result['routes']).relay_task_ids)=={1,2}
    assert result['metadata']['complete_proposals_generated']>0 and result['metadata']['complete_leafs_full_verified']>0
    assert result['metadata']['post_repair_snapshot']['current_handoff']['shared_loaded_distance_km']>0.
    audit(p,base,raw['routes'],result,macro['move'],True)


def test_deadline_after_one_real_improvement_returns_saved_predeadline_checkpoint_without_rescoring(monkeypatch):
    p,base=direct_fixture();clock=[perf_counter()];deadline=clock[0]+4.;actual=method._frontier;evaluate=method.evaluate_solution
    monkeypatch.setattr(method,'perf_counter',lambda:clock[0])
    def timed(*args,**kwargs):
        stats=args[-2]
        if stats['accepted_improvements']:
            clock[0]=deadline+1.
        return actual(*args,**kwargs)
    def no_late_score(*args,**kwargs):
        assert clock[0]<deadline
        return evaluate(*args,**kwargs)
    monkeypatch.setattr(method,'_frontier',timed);monkeypatch.setattr(method,'evaluate_solution',no_late_score)
    result=method.refine(p,base,base,deadline=deadline,weights=W,scales=S,relay_enabled=False,
        focus_task_ids=(1,2),closure_limit=4,beam_width=6,position_limit=4,option_count=1,max_rounds=2)
    m=result['metadata'];assert m['deadline_reached'] and not m['returned_input_guard']
    assert m['returned_checkpoint_checked_within_deadline']
    assert m['post_repair_snapshot']['full_verified_at_perf_counter']<deadline
    assert result['evaluation'].valid and result['evaluation'].score.distance_km<evaluate(p,base,_force_full=True).score.distance_km
    assert any(not layer['completed'] for record in m['closure_records'] for layer in record['layers'])
    audit(p,base,base,result)


def test_rejected_complete_leaf_does_not_replace_the_complete_input_guard(monkeypatch):
    p,base=direct_fixture();actual=method._snapshot
    def invalid(*args,**kwargs):
        if kwargs['phase']!='input':raise ValueError('injected invalid full checkpoint')
        return actual(*args,**kwargs)
    monkeypatch.setattr(method,'_snapshot',invalid)
    result=run(p,base)
    assert result['routes']==base and result['evaluation'].valid
    assert result['metadata']['invalid_candidates']>0 and not result['metadata']['accepted_improvements']
    assert result['metadata']['post_repair_snapshot']['phase']=='input'


def test_unproven_nonoriginal_direct_seed_and_unsupported_partner_depth_are_rejected():
    p,base=direct_fixture()
    with pytest.raises(ValueError,match='provenance'):
        run(p,base,tuple(reversed(base)))
    with pytest.raises(ValueError,match='one-hop'):
        method.refine(p,base,base,deadline=perf_counter()+2.,weights=W,scales=S,
            relay_enabled=False,partner_depth=0)


@pytest.mark.parametrize('expire_after_first', [False, True])
def test_expired_initialization_returns_no_new_evaluation_or_checkpoint(monkeypatch, expire_after_first):
    p,base=direct_fixture();clock=[perf_counter()];deadline=clock[0]+2.;calls=[0];actual=method.evaluate_solution
    if not expire_after_first:clock[0]=deadline+1.
    monkeypatch.setattr(method,'perf_counter',lambda:clock[0])
    def initial_only(*args,**kwargs):
        calls[0]+=1;value=actual(*args,**kwargs);clock[0]=deadline+1.;return value
    monkeypatch.setattr(method,'evaluate_solution',initial_only)
    result=method.refine(p,base,base,deadline=deadline,weights=W,scales=S,relay_enabled=False)
    assert calls[0]==int(expire_after_first) and result['routes']==base and result['evaluation'] is None
    m=result['metadata'];assert not m['initialization_completed'] and m['deadline_reached']
    assert m['input_snapshot'] is None and m['post_repair_snapshot'] is None
    assert not m['returned_checkpoint_checked_within_deadline'] and not m['full_candidate_snapshots']
