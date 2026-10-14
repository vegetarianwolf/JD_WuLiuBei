"""Small actual-parent donor catalogues; no optimizer or official data."""
from dataclasses import replace
from time import perf_counter
import json

import pytest

from uav_dispatch import Point,Problem,Task
from algorithm.experiments import conditional_donor_catalogue_v1 as c
from algorithm.experiments import critical_prefix_closure_v1 as closure
from algorithm.tests.test_critical_prefix_closure_v1 import fixture

W=(.5,.25,.25);S=(9.,100.,100.)


def catalogue(p=None,base=None,raw=None,**kwargs):
    if p is None:p,base,raw=fixture()
    args=dict(pinned_task_ids=(1,2),focus_task_ids=(1,2),deadline=perf_counter()+4.,weights=W,scales=S)
    args.update(kwargs)
    return c.propose_catalogue(p,base,raw,**args)


def direction_fixture():
    """Two opposite sources give distinct best donors from the same plane."""
    P=Point
    tasks=(Task(1,P(0.,0.),P(20.,0.),100.),Task(2,P(0.,0.),P(0.,0.),100.),Task(3,P(0.,0.),P(0.,0.),100.),
        Task(4,P(0.,0.),P(0.,0.),100.),Task(5,P(-10.,0.),P(-10.,0.),100.),Task(6,P(10.,0.),P(10.,0.),100.),
        Task(7,P(-10.,0.),P(20.,0.),100.),Task(8,P(-10.,0.),P(-10.,0.),100.),Task(9,P(-10.,0.),P(-10.,0.),100.))
    p=Problem(tasks,drone_count=3,max_tasks_per_drone=3,capacity=2,speed_km_per_min=1.,
        drone_homes=(P(0.,0.),P(10.,0.),P(-10.,0.)))
    routes=((1,-1,2,-2,3,-3),(4,-4,5,-5,6,-6),(7,-7,8,-8,9,-9))
    return p,routes


def test_zero_damage_causal_critical_rows_and_target_none_are_not_fabricated():
    p,base,raw=fixture();out=catalogue(p,base,raw)
    rows={r['task_id']:r for r in out['source_preview']['per_task_evidence']}
    assert 3 in out['critical_task_ids'] and rows[3]['delta_lateness_min']==0.
    assert rows[3]['signed_improvement_min']['in_drop:1']==8.
    direct=catalogue(p,base,base,pinned_task_ids=())
    rows={r['task_id']:r for r in direct['source_preview']['per_task_evidence']}
    assert rows[1]['signed_improvement_min']['delivery:1'] is None
    assert all(t['type']=='delivery' for t in direct['source_preview']['targets'])
    assert direct['pinned_task_ids']==[] and direct['eligible_native_count']==9


def test_donors_outside_old_U_are_real_whole_current_owner_tasks():
    p,base,raw=fixture();old=closure.build_closure(p,base,raw,pinned_task_ids=(1,2),focus_task_ids=(1,2),
        closure_limit=3,deadline=perf_counter()+4.)
    out=catalogue(p,base,raw)
    actual=[r for r in out['records'] if r['t']==3 and r['b']==1]
    assert {r['donor_task_id'] for r in actual}=={6,7,8}
    assert any(r['donor_task_id'] not in old['removed_task_ids'] for r in actual)
    assert out['source_preview']['removed_task_ids']==[]  # Never a search-U constraint.


def test_actual_destinations_all_other_planes_and_real_deletion_q_not_new_slots():
    p,base,raw=fixture();out=catalogue(p,base,raw)
    for t in out['critical_task_ids']:
        a=next(r['a'] for r in out['records'] if r['t']==t)
        assert {d['b'] for d in out['donor_directories'] if d['t']==t}==set(range(3))-{a}
    index=closure._layout(p,raw)[1]
    for r in out['records']:
        t,d,a,b=r['t'],r['donor_task_id'],r['a'],r['b']
        assert index.delivery_route_by_task[t]==a and index.delivery_route_by_task[d]==b and a!=b
        assert r['removal_quota_slots_by_aircraft']==[int(v==a)+int(v==b) for v in range(3)]
        assert r['target_owner_by_task']=={str(t):b,str(d):a}
        assert r['full_joint_score'] is None and not r['complete_solution'] and not r['global_objective_bound']


def test_same_destination_changes_return_channel_donor_with_critical_source():
    p,routes=direction_fixture()
    out=catalogue(p,routes,routes,pinned_task_ids=(),focus_task_ids=(3,9))
    dirs={(d['t'],d['b']):d for d in out['donor_directories']}
    assert (1,1) in dirs and (7,1) in dirs
    assert dirs[1,1]['donor_rankings']['donor_return_early_no_wait'][0]==4
    assert dirs[7,1]['donor_rankings']['donor_return_early_no_wait'][0]==5
    assert dirs[7,1]['donor_rankings']['donor_return_early_no_wait'][0]!=min(dirs[7,1]['eligible_donor_task_ids'])


def test_capacity_legal_block_geometry_and_no_wait_clocks_are_only_proxies():
    p,_,raw=fixture();stats={}
    # Cannot insert between consecutive OUT starts, nor while already holding2.
    surf=c._surface(p,raw[2],2,3,frozenset((1,2)),perf_counter()+4.,stats)
    assert surf['forbidden_pin_take_internal_gaps']==[3]
    assert surf['geometric']['gap']!=3 and surf['earliest_delivery']['gap']!=3
    assert 'proposal_only_not_joint_score' in surf['scoring_scope']
    loaded=(4,5,-4,-5)
    s=c._surface(p,loaded,0,3,frozenset(),perf_counter()+4.,{})
    assert s['legal_contiguous_gaps']==4  # g2 holds2, others hold0/1.
    tail=c._surface(p,(),0,3,frozenset(),perf_counter()+4.,{})
    assert tail['geometric']['gap']==0 and tail['legal_contiguous_gaps']==1


def test_full_rankings_retention_hashes_and_omissions_are_truthful_and_json_safe():
    p,routes=direction_fixture();out=catalogue(p,routes,routes,pinned_task_ids=(),focus_task_ids=(3,9),donor_limit=1)
    for dr in out['donor_directories']:
        assert len(dr['retained_donor_task_ids'])==1
        assert set(dr['omitted_donor_task_ids'])==set(dr['eligible_donor_task_ids'])-set(dr['retained_donor_task_ids'])
        assert all(set(order)==set(dr['eligible_donor_task_ids']) for order in dr['donor_rankings'].values())
        assert all(ev['full_joint_score'] is None for ev in dr['per_donor_evidence'])
    for r in out['records']:
        assert r['proposal_sha256']==c._digest({k:r[k] for k in ('parent_routes_sha256','t','a','b','donor_task_id','target_owner_by_task')})
    assert out['parent_routes_sha256']==c._digest(routes)
    assert out['completed'] and not out['complete']
    json.dumps(out,allow_nan=False)


def test_actual_parent_owner_changes_are_used_instead_of_base_ownership():
    p,base,raw=fixture();swapped=(tuple(v for v in raw[0] if abs(v)!=3)+(6,-6),
        tuple(v for v in raw[1] if abs(v)!=6)+(3,-3),raw[2])
    out=catalogue(p,base,swapped)
    for dr in out['donor_directories']:
        if dr['b']==0:assert 6 in dr['eligible_donor_task_ids'] and 3 not in dr['eligible_donor_task_ids']
        if dr['b']==1:assert 3 in dr['eligible_donor_task_ids'] and 6 not in dr['eligible_donor_task_ids']


def test_expired_entry_and_geometry_loop_never_publish_partial_catalogue(monkeypatch):
    stats={}
    with pytest.raises(c.Deadline):catalogue(deadline=perf_counter()-1.,statistics=stats)
    assert stats['deadline_reached'] and not stats['completed']
    actual=c._surface;seen=[]
    def expired(*args,**kwargs):
        seen.append(1)
        if len(seen)==2:raise c.Deadline
        return actual(*args,**kwargs)
    monkeypatch.setattr(c,'_surface',expired)
    with pytest.raises(c.Deadline):catalogue(statistics=stats)
    assert len(seen)==2 and stats['deadline_reached'] and not stats['completed']


def test_invalid_caps_fake_signature_incomplete_parent_or_capacity_fail():
    p,base,raw=fixture()
    for kwargs in ({'critical_limit':7},{'donor_limit':4},{'critical_limit':True},{'deadline':None}):
        with pytest.raises(ValueError):catalogue(p,base,raw,**kwargs)
    with pytest.raises(ValueError):catalogue(p,base,raw,expected_signature={})
    with pytest.raises(ValueError):catalogue(p,base,closure._whole_remove(p,raw,(3,)))
    with pytest.raises(ValueError):catalogue(replace(p,capacity=1),base,raw)


def test_empty_declared_caps_remain_completed_proposal_catalogues_not_full_solutions():
    out=catalogue(critical_limit=0)
    assert out['records']==[] and out['donor_directories']==[] and out['completed']
    assert len(out['critical_omitted_task_ids'])==7
    out=catalogue(donor_limit=0)
    assert out['records']==[] and out['donor_directories'] and out['completed']


def test_direction_heads_reserve_both_masked_sources_and_C_and_report_small_cap():
    out=catalogue(critical_limit=3)
    directions={d['target_id']:d for d in out['critical_direction_representatives']}
    assert out['critical_task_ids']==[3,6,9]
    assert all(directions[t]['representative_retained'] for t in ('in_drop:1','in_drop:2','receiver_arrival'))
    end=perf_counter()+4.;small=catalogue(critical_limit=1,deadline=end)
    ds={d['target_id']:d for d in small['critical_direction_representatives']}
    assert small['critical_task_ids']==[3]
    assert not ds['in_drop:2']['representative_retained'] and ds['in_drop:2']['omitted_reason']=='critical_limit'
    assert small['completed_at_perf_counter']<small['deadline_perf_counter']==end


@pytest.mark.parametrize('relay',[False,True])
def test_public_independent_auditor_rebuilds_all_real_donor_proxies_and_causal_rows(relay):
    from algorithm.experiments.audit_conditional_donor_repair_v1 import audit_catalogue
    from algorithm.tests.test_audit_critical_prefix_joint_repair_v1 import fixture as audit_fixture
    data=audit_fixture();p,base,raw=fixture();parent=raw if relay else base
    out=catalogue(p,base,parent,pinned_task_ids=(1,2) if relay else (),critical_limit=3,donor_limit=3)
    proof=audit_catalogue(data[0],data[1],data[2],parent,out,pinned_task_ids=(1,2) if relay else (),
        seed_move=data[4] if relay else None,relay_enabled=relay,focus_task_ids=(1,2),weights=W,scales=S,
        original_raw_routes=parent)
    assert proof['passed'] and proof['all_declared_donor_proxies_replayed']
    assert not proof['all_full_exchange_positions_replayed']
