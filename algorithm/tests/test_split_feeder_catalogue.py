"""Small real clocks/quotas for the shared same-owner split-feeder catalogue."""
from dataclasses import replace
import json
from time import perf_counter

import pytest

from uav_dispatch import Point,Problem,Task,RelayStation,TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import split_feeder_catalogue as cat

W,S=(.5,.25,.25),(9.,100.,100.)


def fixture(two_stations=True):
    tasks=[Task(1,Point(-4.,0.),Point(0.,4.),9.),Task(2,Point(4.,0.),Point(0.,5.),11.)]
    for t in (3,4,7):tasks.append(Task(t,Point(-4.,0.),Point(-4.,0.),100.))
    for t in (5,6,8):tasks.append(Task(t,Point(4.,0.),Point(4.,0.),100.))
    tasks.append(Task(9,Point(0.,0.),Point(0.,0.),100.))
    stations=(RelayStation(9,0.,0.),RelayStation(10,0.,1.)) if two_stations else (RelayStation(9,0.,0.),)
    legs={};identifier=101
    for h in stations:
        for t in (1,2):
            for kind in ('RELAY_IN','RELAY_OUT'):
                legs[identifier]=TransportLeg(identifier,t,kind,h.id);identifier+=1
    p=Problem(tuple(tasks),drone_count=3,max_tasks_per_drone=3,capacity=2,speed_km_per_min=1.,
        drone_homes=(Point(-4.,0.),Point(4.,0.),Point(0.,0.)),relay_stations=stations,
        leg_registry=legs,task_relay_candidates={t:tuple(h.id for h in stations) for t in (1,2)})
    return p,((3,-3,4,-4,7,-7),(5,-5,6,-6,8,-8),(1,-1,2,-2,9,-9))


def build(p=None,routes=None,**kwargs):
    if p is None:p,routes=fixture()
    params=dict(deadline=perf_counter()+4.,weights=W,scales=S,max_families=4,base_pool_limit=4)
    params.update(kwargs)
    return cat.build_catalogue(p,routes,**params)


def test_complete_same_owner_model_keeps_all_native_and_real_K_counts():
    p,base=fixture();result=build(p,base);original=build_plan_index(p,base)
    assert result['completed'] and result['completed_at_perf_counter']<perf_counter()
    assert result['families'] and {f['station_id'] for f in result['families']}=={9,10}
    assert result['canonical_full_checked'] is False and result['scoring_scope']==cat.SCOPE
    for family in result['families']:
        assert family['i']==1 and family['j']==2 and family['c']==2
        assert len({family['a'],family['b'],family['c']})==3
        assert family['k1'] in (3,4,7) and family['k2'] in (5,6,8) or family['k2'] in (3,4,7) and family['k1'] in (5,6,8)
        for witness in family['relay_witnesses']:
            checked=evaluate_solution(p,witness['routes'],_force_full=True)
            assert checked.valid
            index=build_plan_index(p,witness['routes'])
            assert index.delivery_route_by_task==original.delivery_route_by_task
            assert set(index.relay_task_ids)=={1,2}
            assert {a:sum(owner==a for owner in index.delivery_route_by_task.values()) for a in range(3)}=={0:3,1:3,2:3}
            for a,route in enumerate(witness['routes']):
                assert tuple(v for v in route if abs(v) not in p.leg_registry)==tuple(v for v in base[a] if abs(v) not in (1,2))
            assert witness['table_score']['late_count']==checked.score.late_count
            assert witness['table_score']['total_lateness_min']==pytest.approx(checked.score.total_lateness_min)
            assert witness['table_score']['distance_km']==pytest.approx(checked.score.distance_km)
            assert witness['complete_RELAY_model'] and not witness['canonical_full_checked']


def test_actual_home_waits_and_every_global_clock_match_local_full_table():
    p,base=fixture();result=build(p,base);evaluator=GlobalRelayEvaluator(p)
    for f in result['families']:
        for w in f['relay_witnesses']:
            schedule=evaluator.evaluate(w['routes'])
            assert schedule.feasible and len(schedule.delivery_times_min)==9
            for a,route in enumerate(w['routes']):
                assert w['event_times_min'][a]==pytest.approx([schedule.event_times_min[a,k] for k in range(len(route))])
            assert w['delivery_times_min']==pytest.approx({str(t):v for t,v in schedule.delivery_times_min.items()})
            window=w['window'];available=w['source_drop_availability_min']
            assert window['second_take_min']==max(window['receiver_arrival_min'],*available.values())
            assert w['shared_loaded_distance_km']>1e-10
    assert any(w['window']['second_take_min']>w['window']['receiver_arrival_min'] for f in result['families'] for w in f['relay_witnesses'])


def test_source_tables_leave_all_three_final_tasks_and_have_no_missing_compensation():
    p,base=fixture();result=build(p,base)
    assert result['statistics']['full_source_tables_built']<=2*result['statistics']['base_families_retained']
    for f in result['families']:
        assert f['pooled_task_ids']==[1,2] and f['task_ids']==sorted((1,2,f['k1'],f['k2']))
        assert set(f['source_tables'])==set(f['source_table_keys'])
        for key,records in f['source_tables'].items():
            assert result['source_tables'][key]==records
            for r in records:
                assert r['final_delivery_count']==3
                assert r['native_complete'] and not r['optimistic_missing_comp'] and r['compensation_task_id'] is None
                assert len(r['event_times_min'])==len(r['load_before'])==len(r['load_after'])==len(r['route'])
                assert set(r['delivery_times_min'])=={str(abs(v)) for v in base[r['aircraft']]}
                assert r['in_drop_time_min']==r['event_times_min'][r['in_drop_position']]
                assert r['source_drop_availability_min']=={str(r['task_id']):r['in_drop_time_min']}


def test_no_original_coload_requirement_or_destination_nearest_pair_gate():
    p,base=fixture();held=set()
    for v in base[2]:
        if v>0:held.add(v)
        else:held.remove(-v)
        assert not {1,2}<=held
    result=build(p,base)
    assert result['families'] and result['statistics']['same_owner_task_pairs_considered']==9
    assert all(f['i']==1 and f['j']==2 for f in result['families'])


def test_original_station_eligibility_is_preserved_even_with_registry_legs():
    p,base=fixture();p=replace(p,task_relay_candidates={1:(9,10),2:(9,)})
    result=build(p,base)
    assert result['families'] and {f['station_id'] for f in result['families']}=={9}
    assert all(p.task_relay_candidates[f['i']].count(f['station_id']) and p.task_relay_candidates[f['j']].count(f['station_id']) for f in result['families'])


def test_finite_feeder_and_pool_omissions_are_explicit_not_full_J_rejections():
    p,base=fixture();result=build(p,base,max_families=1,base_pool_limit=1,feeder_limit=1)
    stats=result['statistics']
    assert stats['feeder_profiles_omitted']>0
    assert stats['base_families_not_full_scored']>0
    assert stats['proxy_stations_omitted_due_pool_limit']==[10]
    assert len(result['families'])==1
    assert 'ranking only' in result['families'][0]['proxy']['score_omits']


def test_true_pooled_rescue_channel_and_weighted_key_never_change_score():
    p,base=fixture();result=build(p,base)
    assert any(set(w['pooled_rescued_task_ids'])=={1,2} for f in result['families'] for w in f['relay_witnesses'])
    for f in result['families']:
        for w in f['relay_witnesses']:
            v=w['table_score'];components=(v['late_count'],v['total_lateness_min'],v['distance_km'])
            assert w['key']==pytest.approx([sum(a*b/c for a,b,c in zip(W,components,S)),*components])
            assert w['actual_final_owners']=={'1':2,'2':2,'3':0,'4':0,'5':1,'6':1,'7':0,'8':1,'9':2}


def test_donors_are_real_original_tasks_and_remain_unchanged_in_RELAY():
    p,base=fixture();result=build(p,base)
    for f in result['families']:
        for donor in f['donor_evidence']:
            a,t=donor['aircraft'],donor['task_id']
            assert t in base[a] and -t in base[a] and donor['candidate_count']==3
            assert donor['deleted_route']==[v for v in base[a] if abs(v)!=t]
            for w in f['relay_witnesses']:
                assert t in w['routes'][a] and -t in w['routes'][a]


def test_shared_catalogue_hashes_and_JSON_do_not_depend_on_execution_arm():
    p,base=fixture();one=build(p,base);two=build(p,base)
    assert one['families']==two['families']
    assert one['selector_sha256']==two['selector_sha256'] and one['catalogue_sha256']==two['catalogue_sha256']
    assert json.loads(json.dumps(one,allow_nan=False))['families']==one['families']


def test_four_original_station_opportunities_survive_a_four_family_frontier():
    p,base=fixture();stations=(*p.relay_stations,RelayStation(11,1.,0.),RelayStation(12,-1.,0.))
    legs={};identifier=101
    for h in stations:
        for t in (1,2):
            for kind in ('RELAY_IN','RELAY_OUT'):
                legs[identifier]=TransportLeg(identifier,t,kind,h.id);identifier+=1
    p=replace(p,relay_stations=stations,leg_registry=legs,task_relay_candidates={1:(9,10,11,12),2:(9,10,11,12)})
    result=build(p,base,max_families=4,base_pool_limit=4)
    assert {f['station_id'] for f in result['families']}=={9,10,11,12}
    assert result['statistics']['proxy_stations_omitted_due_pool_limit']==[]
    assert result['statistics']['full_scored_stations_not_retained']==[]


def test_all_saved_family_source_and_full_witness_fields_pass_independent_auditor():
    from algorithm.experiments.audit_split_feeder import audit_split_feeder_family
    from algorithm.tests.test_relay_repooling_exchange import topology
    p,base=fixture();result=build(p,base)
    tasks={t.id:((t.pickup.x,t.pickup.y),(t.delivery.x,t.delivery.y),t.deadline_min) for t in p.tasks}
    graph=topology(p);graph['task_relay_candidates']={str(t):list(p.task_relay_candidates.get(t,())) for t in p.task_ids}
    for f in result['families']:
        assert audit_split_feeder_family(tasks,graph,base,f,weights=W,scales=S)['passed']


def test_no_eligible_relay_network_publishes_completed_empty_common_catalogue():
    p,base=fixture();p=replace(p,leg_registry={},task_relay_candidates={})
    result=build(p,base)
    assert result['completed'] and result['families']==[] and result['source_tables']=={}


def test_proxy_gap_compression_is_explicit_while_full_receiver_tables_scan_all_gaps():
    p,base=fixture(False)
    extra=tuple(Task(t,Point(0.,float(t-9)),Point(0.,float(t-9)),100.) for t in (10,11,12,13))
    p=replace(p,tasks=(*p.tasks,*extra),max_tasks_per_drone=7)
    base=(base[0],base[1],base[2]+tuple(v for t in (10,11,12,13) for v in (t,-t)))
    result=build(p,base,max_families=1,base_pool_limit=1)
    stats=result['statistics']
    assert stats['proxy_receiver_gap_instances_omitted']>0
    assert stats['proxy_receiver_gap_instances_retained']<=3*stats['proxy_source_combinations_retained']
    assert stats['joint_proxy_positions_scored']<=6*stats['proxy_source_combinations_retained']
    assert result['configuration']['proxy_receiver_gap_limit']==3
    assert stats['route_cost_statistics']['receiver_candidates']>=2*6
    for f in result['families']:
        for witness in f['relay_witnesses']:assert evaluate_solution(p,witness['routes'],_force_full=True).valid


def test_source_proxy_pareto_drops_only_dominated_ready_distance_pairs():
    make=lambda gap,time,distance:dict(gap=gap,in_drop_time_min=time,delta_distance_km=distance)
    left=[make(0,2.,5.),make(1,5.,1.)];right=[make(0,1.,3.),make(1,6.,4.)];stats={}
    kept=cat._joint_proxy_sources(left,right,stats)
    assert [(max(x['in_drop_time_min'] for x in pair),sum(x['delta_distance_km'] for x in pair)) for pair in kept]==[(2.,8.),(5.,4.)]
    assert stats==dict(proxy_source_combinations_considered=4,proxy_source_combinations_retained=2,proxy_source_combinations_dominated=2)


@pytest.mark.parametrize('kind',('expired','badweight','badbase','relaybase','position0','poolsmall'))
def test_invalid_or_expired_inputs_never_publish_a_catalogue(kind):
    p,base=fixture();kwargs={}
    if kind=='expired':kwargs['deadline']=perf_counter()-1
    elif kind=='badweight':kwargs['weights']=(0.,.5,.5)
    elif kind=='badbase':base=(base[0][:-1],*base[1:])
    elif kind=='relaybase':base=((101,-101,*base[0]),base[1],(102,-102,2,-2,9,-9))
    elif kind=='position0':kwargs['source_position_limit']=0
    else:kwargs.update(max_families=4,base_pool_limit=3)
    with pytest.raises((ValueError,cat.Deadline)):build(p,base,**kwargs)


def test_expiry_during_final_digest_does_not_publish_completed_prefix(monkeypatch):
    p,base=fixture();deadline=perf_counter()+5;clock=[perf_counter()];digest=cat._digest;stats={}
    monkeypatch.setattr(cat,'perf_counter',lambda:clock[0])
    def expire(value):
        result=digest(value);clock[0]=deadline+1;return result
    monkeypatch.setattr(cat,'_digest',expire)
    with pytest.raises(cat.Deadline):build(p,base,deadline=deadline,statistics=stats)
    assert stats['deadline_reached'] and not stats['completed']
