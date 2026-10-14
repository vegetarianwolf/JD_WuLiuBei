"""Small real multi-group structures/frontiers, never official optimization."""
from copy import deepcopy
from dataclasses import replace
from time import perf_counter
import json

import pytest

from uav_dispatch import Point,Problem,Task,RelayStation,TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import multi_relay_structure_v1 as m


def fixture():
    P=Point;H=P(0.,0.)
    tasks=(Task(1,H,P(1.,0.),100.),Task(2,P(0.,2.),P(2.,0.),100.),
        Task(3,H,P(3.,0.),100.),Task(4,P(0.,4.),P(4.,0.),100.),
        Task(5,H,P(5.,0.),100.),Task(6,H,P(6.,0.),100.))
    legs={}
    for t in range(1,7):
        legs[100+2*t-1]=TransportLeg(100+2*t-1,t,'RELAY_IN',1)
        legs[100+2*t]=TransportLeg(100+2*t,t,'RELAY_OUT',1)
    p=Problem(tasks,drone_count=3,max_tasks_per_drone=2,capacity=2,speed_km_per_min=1.,
        drone_homes=(H,H,H),relay_stations=(RelayStation(1,0.,0.),),leg_registry=legs,
        task_relay_candidates={t:(1,) for t in range(1,7)})
    base=((3,4,-3,-4),(5,6,-5,-6),(1,2,-1,-2))
    one=((101,-101,3,4,-3,-4),(103,-103,5,6,-5,-6),(102,104,-102,-104))
    two=((101,-101,106,108,-106,-108),(103,-103,105,-105,5,6,-5,-6),(107,-107,102,104,-102,-104))
    groups=[dict(group_id=1,i=1,j=2,a=0,b=1,c=2,station_id=1),dict(group_id=2,i=3,j=4,a=1,b=2,c=0,station_id=1)]
    return p,base,one,two,groups


def test_two_real_disjoint_groups_share_planes_and_new_group_changes_old_wait():
    p,base,one,two,g=fixture();sg=m.signature(p,one,g[:1])
    structure=m.shape(p,two,g,expected_signature=sg)
    assert structure['group_count']==2 and structure['cross_uav_count']==4
    assert structure['final_counts']==[2,2,2] and all(x['shared_loaded_distance_km']>1e-10 for x in structure['groups'])
    assert not structure['global_DAG_checked'] and not structure['complete_solution']
    assert evaluate_solution(p,base,_force_full=True).valid and evaluate_solution(p,two,_force_full=True).valid
    a=GlobalRelayEvaluator(p).evaluate(one);b=GlobalRelayEvaluator(p).evaluate(two)
    assert a.event_times_min[2,1]==4. and b.event_times_min[2,3]==8.
    assert a.total_waiting_min!=b.total_waiting_min


def test_empty_signature_can_protect_counts_while_adding_first_actual_group():
    p,base,one,_,g=fixture();sig=m.signature(p,base,[])
    assert sig['protected_task_ids']==[] and sig['final_counts']==[2]*3
    assert m.shape(p,one,g[:1],expected_signature=sig)['relay_task_ids']==[1,2]
    with pytest.raises(ValueError):m.shape(p,one,[])


def test_duplicate_task_wrong_roles_H_old_sequence_or_nonempty_take_rejected():
    p,_,one,two,g=fixture();sig=m.signature(p,one,g[:1])
    bad=deepcopy(g);bad[1]['i']=1
    with pytest.raises(ValueError):m.shape(p,two,bad)
    bad=deepcopy(g);bad[0]['a'],bad[0]['b']=bad[0]['b'],bad[0]['a']
    with pytest.raises(ValueError):m.shape(p,two,bad)
    bad=deepcopy(g);bad[1]['station_id']=2
    with pytest.raises(ValueError):m.shape(p,two,bad)
    broken=(two[0],two[1],(107,-107,104,102,-102,-104))
    with pytest.raises(ValueError,match='old plans'):m.shape(p,broken,g,expected_signature=sig)
    held=(two[0],(5,103,-103,105,-105,6,-6,-5),two[2])
    assert m.shape(p,held,g)['group_count']==2  # Only receiver emptiness is restricted, sources can hold1.


def test_cross_group_cycle_is_not_mistaken_for_a_complete_global_certificate():
    p,_,one,two,g=fixture();sig=m.signature(p,one,g[:1])
    cycle=((106,108,-106,-108,101,-101),two[1],(102,104,-102,-104,107,-107))
    assert m.shape(p,cycle,g,expected_signature=sig)['complete_structure']
    assert not GlobalRelayEvaluator(p).evaluate(cycle).feasible
    assert not evaluate_solution(p,cycle,_force_full=True).valid


def test_IN_frontier_can_carry_with_old_IN_but_not_through_load2_or_take_gap():
    p,_,_,two,g=fixture();schedule=GlobalRelayEvaluator(p).evaluate(two)
    rows=m._in_rows(p,two[0],0,5,1,g,schedule,perf_counter()+3.)
    assert any(r['gap']==0 and r['end_gap']==2 for r in rows)
    loads=m._loads(two[0]);banned=m._forbidden(p,two[0],g,0)
    assert banned=={3}
    for row in rows:
        x,y=row['gap'],row['end_gap']
        assert max(loads[x:y+1])<=1 and x not in banned and y not in banned
        child=m._in_route(two[0],row)
        assert max(m._loads(child))<=2
    assert any(r['end_gap']==len(two[0]) for r in rows)


def test_receiver_separated_delivery_uses_real_original_gap_domain_and_no_native_price():
    p,_,one,_,g=fixture();schedule=GlobalRelayEvaluator(p).evaluate(one)
    native=(101,-101)
    rows=m._out_rows(p,native,0,(3,4),1,g[:1],one,schedule,(4.,8.),perf_counter()+3.)
    assert any(r['gap']==0 and r['end_gap']==2 for r in rows)
    for row in rows:
        route=m._out_route(native,row,(3,4));assert max(m._loads(route))<=2
        assert route[row['gap']:row['gap']+2]==(106,108)
        assert row['scoring_scope']==m.PROXY_SCOPE
    assert all('score' not in r and 'native_score' not in r for r in rows)


def test_current_native_same_owner_pairs_generate_new_complete_groups_not_old_family_list():
    p,_,one,_,g=fixture();end=perf_counter()+3.
    out=m.propose_catalogue(p,one,g[:1],deadline=end,weights=(.5,.25,.25),scales=(6.,100.,100.),max_families=8)
    assert out['completed'] and out['completed_at_perf_counter']<end and out['complete_candidates']
    assert {(f['i'],f['j']) for f in out['families']}=={(3,4),(5,6)}
    assert all(f['i'] not in(1,2) and f['j'] not in(1,2) for f in out['families'])
    valid=[]
    for candidate in out['complete_candidates']:
        assert candidate['new_group']['group_id']==2 and candidate['complete_atomic_transport_plans']
        assert not candidate['full_global_checked']
        m.shape(p,candidate['routes'],[*g[:1],candidate['new_group']],expected_signature=out['parent_signature'])
        if evaluate_solution(p,candidate['routes'],_force_full=True).valid:valid.append(candidate)
    assert valid
    assert not out['metadata']['configuration']['uses_pure_DIRECT_suffix_prices']
    json.dumps(out,allow_nan=False)


def test_expired_and_mid_catalogue_deadlines_publish_no_partial_directory(monkeypatch):
    p,_,one,_,g=fixture();stats={}
    with pytest.raises(m.Deadline):m.propose_catalogue(p,one,g[:1],deadline=perf_counter()-1.,weights=(.5,.25,.25),scales=(1.,1.,1.),statistics=stats)
    assert stats['deadline_reached'] and not stats['completed']
    native=m._in_rows;calls=[]
    def stop(*args,**kwargs):
        calls.append(1)
        if len(calls)==2:raise m.Deadline
        return native(*args,**kwargs)
    monkeypatch.setattr(m,'_in_rows',stop)
    with pytest.raises(m.Deadline):m.propose_catalogue(p,one,g[:1],deadline=perf_counter()+3.,weights=(.5,.25,.25),scales=(1.,1.,1.),statistics=stats)
    assert len(calls)==2 and stats['deadline_reached'] and not stats['completed']


def test_existing_station_opportunities_are_stratified_without_adding_resources():
    p,_,one,_,g=fixture();registry=dict(p.leg_registry)
    for t in range(1,7):
        registry[1000+2*t-1]=TransportLeg(1000+2*t-1,t,'RELAY_IN',2)
        registry[1000+2*t]=TransportLeg(1000+2*t,t,'RELAY_OUT',2)
    p=replace(p,relay_stations=(*p.relay_stations,RelayStation(2,1.,1.)),leg_registry=registry,
        task_relay_candidates={t:(1,2) for t in range(1,7)})
    out=m.propose_catalogue(p,one,g[:1],deadline=perf_counter()+3.,weights=(.5,.25,.25),scales=(6.,100.,100.),
        max_families=4,base_pool_limit=8)
    assert {f['station_id'] for f in out['families']}=={1,2}
    assert sum(out['metadata']['station_pool_counts'].values())<=8
    assert out['metadata']['configuration']['station_pool_limits']=={'1':4,'2':4}
