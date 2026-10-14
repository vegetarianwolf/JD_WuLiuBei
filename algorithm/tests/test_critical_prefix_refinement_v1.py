"""Real tiny raw-to-repair integration, seed provenance and original guard."""
from time import perf_counter

import pytest

from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg
from uav_dispatch.relay import build_plan_index
from algorithm.experiments import critical_prefix_refinement_v1 as method
from algorithm.experiments.audit_weighted_relay_ablation import reference_schedule
from algorithm.tests.test_relay_repooling_exchange import topology

W,S=(.5,.25,.25),(9.,100.,100.)


@pytest.fixture(scope='module')
def tiny():
    left,right,hub=Point(-4.,0.),Point(4.,0.),Point(0.,0.)
    tasks=(Task(1,left,Point(0.,3.),9.),Task(2,right,Point(0.,4.),9.),
        *(Task(t,left,left,100.) for t in (3,4,5)),
        *(Task(t,right,right,100.) for t in (6,7,8)),Task(9,hub,hub,100.))
    registry={identifier:TransportLeg(identifier,t.id,kind,1) for t in tasks
        for identifier,kind in ((100+2*t.id,'RELAY_IN'),(101+2*t.id,'RELAY_OUT'))}
    p=Problem(tasks,drone_count=3,capacity=2,max_tasks_per_drone=3,speed_km_per_min=.9,
        drone_homes=(left,right,hub),relay_stations=(RelayStation(1,0.,0.),),leg_registry=registry,
        task_relay_candidates={t.id:(1,) for t in tasks})
    base=((3,-3,4,-4,5,-5),(6,-6,7,-7,8,-8),(9,-9,1,-1,2,-2))
    graph=topology(p);graph['task_relay_candidates']={str(t.id):[1] for t in tasks}
    task_rows={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in tasks}
    outcomes={}
    for relay in (False,True):
        outcomes[relay]=method.refine(p,base,deadline=perf_counter()+5.,weights=W,scales=S,
            seed=17,relay_enabled=relay,max_families=4,base_pool_limit=8,
            closure_limit=5,beam_width=2,position_limit=4,option_count=2,max_rounds=1,snapshot_limit=3)
    return p,base,graph,task_rows,outcomes


def test_real_both_arm_raw_repair_preserves_complete_physics_and_strong_input(tiny):
    p,base,graph,tasks,results=tiny
    for relay,result in results.items():
        meta=result['metadata'];ref=reference_schedule(tasks,result['routes'],{**graph,'relay_allowed':relay})
        assert ref['score']['late_count']==result['evaluation'].score.late_count
        assert abs(ref['score']['distance_km']-result['evaluation'].score.distance_km)<1e-8
        assert tuple(meta['final_key'])<=tuple(meta['initial_key'])
        assert meta['raw_metadata']['selector_completed'] and meta['seed_records']
        assert all(x['pinned_task_ids']==(x['focus_task_ids'] if relay else []) for x in meta['seed_records'])
        for accepted in meta['accepted_moves']:
            assert accepted['full_verified_at_perf_counter']<=accepted['accepted_at_perf_counter']<meta['deadline_perf_counter']
        index=build_plan_index(p,result['routes'])
        assert [sum(a==owner for owner in index.delivery_route_by_task.values()) for a in range(3)]==[3]*3
        assert bool(index.relay_task_ids) is relay


def test_seed_channels_are_actual_snapshots_with_route_dedup_and_real_separation(tiny):
    p,base,_,_,results=tiny
    for relay,result in results.items():
        records=result['metadata']['seed_records']
        assert len({x['routes_sha256'] for x in records})==len(records)<=2
        raw=result['metadata']['raw_metadata']
        for seed in records:
            snapshot=seed['raw_snapshot']
            assert snapshot==raw['input_snapshot'] or snapshot in raw['full_candidate_snapshots']
            assert seed['routes_sha256']==method._digest(method._routes(snapshot))
            assert seed['genuinely_separated_receiver']==method._separated(p,snapshot)
            if seed['channel']=='minimum_J_genuine_separated_RELAY':assert seed['genuinely_separated_receiver']
    assert all(x['seed_move'] is None or x['seed_move']['mode']!='RELAY' for x in results[False]['metadata']['seed_records'])


def test_direct_focus_target_does_not_pin_original_guard_tasks(tiny):
    p,base,_,_,results=tiny
    raw=dict(metadata=results[False]['metadata']['raw_metadata'])
    original={int(t):clock for t,clock in raw['metadata']['input_snapshot']['delivery_times_min'].items()}
    selected=method._seeds(p,base,raw,False,2,original)
    # The helper accepts the real integer-key canonical delivery dictionary.
    if selected[0]['seed_move'] is None:
        assert selected[0]['pinned_task_ids']==[] and len(selected[0]['focus_task_ids'])==2


def test_empty_expired_call_keeps_original_guard_without_fake_raw_completion(tiny):
    p,base,*_=tiny
    out=method.refine(p,base,deadline=perf_counter()-1.,weights=W,scales=S,relay_enabled=True)
    assert out['routes']==base
    assert out['metadata']['deadline_reached']
    assert out['metadata']['raw_metadata'] is None and not out['metadata']['seed_records']
    assert not out['metadata']['accepted_moves']


def test_direct_post_cannot_silently_reuse_relay_raw_and_relay_subsequence_is_checked(tiny):
    p,base,_,_,results=tiny
    seed=results[True]['metadata']['seed_records'][0];routes=method._routes(seed['raw_snapshot'])
    with pytest.raises(ValueError,match='DIRECT repair introduced'):
        method._pin_guard(p,base,routes,seed['focus_task_ids'],False)
    changed=[list(r) for r in routes];f=seed['seed_move']['family'];c=f['c']
    ends=[k for k,v in enumerate(changed[c]) if v<0 and method.raw_method._task(p,v) in seed['focus_task_ids']]
    changed[c][ends[0]],changed[c][ends[1]]=changed[c][ends[1]],changed[c][ends[0]]
    with pytest.raises(ValueError,match='selected relay event subsequence'):
        method._pin_guard(p,routes,tuple(tuple(r) for r in changed),seed['focus_task_ids'],True)


def test_unchecked_helper_return_never_becomes_a_new_wrapper_checkpoint(tiny,monkeypatch):
    p,base,*_=tiny
    def unchecked(*args,**kwargs):
        return dict(routes=((),(),()),evaluation=None,metadata=dict(
            returned_checkpoint_checked_within_deadline=False,post_repair_snapshot=None))
    monkeypatch.setattr(method.repair,'refine',unchecked)
    out=method.refine(p,base,deadline=perf_counter()+3.,weights=W,scales=S,relay_enabled=True,
        max_families=4,base_pool_limit=8)
    meta=out['metadata']
    assert meta['repair_records'] and all(x['post_consideration_skipped']=='no_helper_predeadline_complete_checkpoint'
        for x in meta['repair_records'])
    assert all(x['phase'].startswith('raw_seed_') for x in meta['full_candidate_snapshots'])
    assert out['evaluation'].valid
