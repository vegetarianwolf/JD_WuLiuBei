"""Actual multi-aircraft native slots and delay witnesses; no official data."""
from dataclasses import replace
import json
from time import perf_counter

import pytest

from uav_dispatch import Point,Problem,Task,RelayStation,TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.search import RouteEvaluator
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import relay_damage_closure as closure


PINS=(1,2)


def fixture(*,direct=False,carried=True,partner=False):
    tasks=[Task(1,Point(0.,0.),Point(10.,0.),100.),Task(2,Point(0.,0.),Point(11.,0.),100.),
        Task(3,Point(0.,0.),Point(0.,0.),100.),Task(4,Point(0.,0.),Point(0.,0.),100.),
        Task(5,Point(2.,0.),Point(3.,0.),100.),Task(6,Point(3.,0.),Point(4.,0.),100.),
        Task(7,Point(4.,0.),Point(3.,0.),100.),Task(8,Point(3.,0.),Point(2.,0.),100.),
        Task(9,Point(11.,0.),Point(12.,0.),10.),Task(10,Point(11.,0.),Point(13.,0.),0.)]
    homes=[Point(0.,0.),Point(0.,0.),Point(5.,0.)]
    outside=[]
    for a in range(3,8):
        x=20.+10.*(a-3);t=8+a
        homes.append(Point(x,0.));tasks.append(Task(t,Point(x+1.,0.),Point(x+2.,0.),100.))
        outside.append((t,-t))
    legs={101:TransportLeg(101,1,'RELAY_IN',1),102:TransportLeg(102,1,'RELAY_OUT',1),
          103:TransportLeg(103,2,'RELAY_IN',1),104:TransportLeg(104,2,'RELAY_OUT',1)}
    p=Problem(tuple(tasks),drone_count=8,max_tasks_per_drone=4,capacity=2,speed_km_per_min=1.,
        drone_homes=tuple(homes),relay_stations=(RelayStation(1,5.,0.),),leg_registry=legs,
        task_relay_candidates={1:(1,),2:(1,)})
    base=((1,-1,5,-5,6,-6),(2,-2,7,-7,8,-8),(9,-9,10,-10,3,-3,4,-4),*outside)
    if direct:
        raw=((3,-3,5,-5,6,-6),(4,-4,7,-7,8,-8),(1,2,-1,-2,9,-9,10,-10),*outside)
    else:
        a=(3,-3,5,101,-5,-101,6,-6) if carried else (3,-3,101,-101,5,-5,6,-6)
        raw=(a,(4,-4,103,-103,7,-7,8,-8),(102,104,-102,-104,9,-9,10,-10),*outside)
    if partner:
        tasks=list(p.tasks)+[Task(16,Point(11.,0.),Point(14.,0.),100.)]
        p=replace(p,tasks=tuple(tasks),max_tasks_per_drone=5)
        base=(base[0],base[1],(9,10,-9,16,-10,-16,3,-3,4,-4),*base[3:])
        c=(1,2,-1,-2,9,10,-9,16,-10,-16) if direct else (102,104,-102,-104,9,10,-9,16,-10,-16)
        raw=(raw[0],raw[1],c,*raw[3:])
    assert evaluate_solution(p,base,_force_full=True).valid
    assert evaluate_solution(p,raw,_force_full=True).valid
    return p,base,raw


def build(p,base,raw,**kwargs):
    return closure.build_closure(p,base,raw,PINS,deadline=perf_counter()+2.,**kwargs)


def by_task(result):return {r['task_id']:r for r in result['evidence']['per_task']}


@pytest.mark.parametrize('direct',[False,True])
def test_all_eight_aircraft_really_release_native_donors_and_exact_quota_slots(direct):
    p,base,raw=fixture(direct=direct)
    result=build(p,base,raw,closure_limit=8,compensation_task_ids=(3,4))
    assert result['eligible_native_count']==13 and len(result['removed_task_ids'])==8
    assert result['actual_opened_aircraft']==list(range(8)) and result['all_aircraft_opened']
    assert result['unopened_aircraft_reasons']=={}
    assert result['quota_slots_by_aircraft']==[1]*8==result['quota_slots']
    assert set(result['removed_task_ids'])&set(PINS)==set()
    for a in range(3,8):assert 8+a in result['removed_task_ids']  # Other five, not just original macro trio.
    assert result['partial_routes'][3:]==[[]]*5
    for a in range(8):
        assert result['raw_final_counts'][a]==result['pin_final_counts'][a]+result['remaining_native_final_counts'][a]+result['quota_slots_by_aircraft'][a]
    index=build_plan_index(p,raw)
    for a,route in enumerate(result['partial_routes']):
        assert route==[v for v in raw[a] if abs(v) not in result['removed_task_ids']]
        assert [v for v in route if closure._task(p,v) in PINS]==[v for v in raw[a] if closure._task(p,v) in PINS]
    assert all(t in index.direct_task_ids for t in result['removed_task_ids'])
    json.dumps(result,allow_nan=False)


def test_real_global_wait_changes_new_late_and_already_late_long_suffix_evidence():
    p,base,raw=fixture();result=build(p,base,raw,closure_limit=9,compensation_task_ids=(3,4));rows=by_task(result)
    schedule=GlobalRelayEvaluator(p).evaluate(raw)
    local=RouteEvaluator(p).evaluate(raw[2],start_node=p.home_node(2))
    assert schedule.total_waiting_min==9.
    assert rows[9]['base_delivery_min']==7. and rows[9]['raw_delivery_min']==16.
    assert rows[9]['newly_late'] and rows[9]['positive_delta_lateness_min']==6.
    assert local.delivery_times_min[9]==7.  # Omitting cross-aircraft wait would misclassify.
    assert not rows[10]['newly_late'] and rows[10]['positive_delta_lateness_min']==9.
    assert 10 in result['removed_task_ids'] and 9 in result['removed_task_ids']
    assert next(d for d in result['evidence']['donor_reservations'] if d['aircraft']==2)['task_id']==10
    assert rows[5]['carried_at_pinned_events'] and rows[5]['after_first_pinned_event']
    assert any(x['task_id']==1 and x['distance_km']>0. for x in rows[5]['raw_co_loaded_partners'])


def test_actual_comp_neighbors_and_original_copartners_are_ranked_without_fake_task_credit():
    p,base,raw=fixture(partner=True)
    result=build(p,base,raw,closure_limit=12,partner_depth=1,compensation_task_ids=(3,16))
    rows=by_task(result)
    assert rows[16]['is_supplied_compensation'] and not rows[16]['is_inferred_compensation_candidate']
    assert 16 in rows[10]['compensation_neighbor_task_ids']
    assert any(x['task_id']==16 and x['distance_km']>0. for x in rows[10]['base_co_loaded_partners'])
    assert result['evidence']['ranking_is_gain_or_bound'] is False
    inferred=build(p,base,raw,closure_limit=12)
    assert inferred['evidence']['compensation_identity_scope']=='owner_change_candidates_only_not_proven_compensation'
    assert 16 not in inferred['evidence']['inferred_compensation_candidate_task_ids']
    assert all(not r['is_supplied_compensation'] for r in inferred['evidence']['per_task'])


def test_partner_depth_links_only_actual_base_or_raw_positive_coload_edges():
    p,base,raw=fixture(partner=True)
    zero=build(p,base,raw,closure_limit=12,partner_depth=0,compensation_task_ids=(3,4))
    one=build(p,base,raw,closure_limit=12,partner_depth=1,compensation_task_ids=(3,4))
    assert by_task(zero)[16]['partner_expansion_depth'] is None
    assert by_task(one)[16]['partner_expansion_depth']==1
    assert 16 in one['evidence']['category_rankings']['co_load_partner']
    assert 'co_load_partner' in by_task(one)[16]['selection_reasons']


@pytest.mark.parametrize('limit',[0,2,7])
def test_small_cap_records_unopened_aircraft_and_never_claims_all_eight(limit):
    p,base,raw=fixture();result=build(p,base,raw,closure_limit=limit)
    assert len(result['removed_task_ids'])==len(result['actual_opened_aircraft'])==limit
    assert not result['all_aircraft_opened']
    assert set(result['unopened_aircraft_reasons'])=={str(a) for a in range(8) if a not in result['actual_opened_aircraft']}
    assert all(reason=='closure_limit_exhausted_before_donor_reservation' for reason in result['unopened_aircraft_reasons'].values())
    assert sum(result['quota_slots_by_aircraft'])==limit
    assert result['truncation']['eligible_omitted_count']==13-limit
    assert set(result['omitted_task_ids'])==set(range(3,16))-set(result['removed_task_ids'])


def test_aircraft_with_only_pins_cannot_supply_a_fake_native_slot():
    p=Problem((Task(1,Point(0.,0.),Point(0.,0.),1.),Task(2,Point(0.,0.),Point(0.,0.),1.),
               Task(3,Point(1.,0.),Point(1.,0.),1.)),drone_count=3,max_tasks_per_drone=1,capacity=2,
               speed_km_per_min=1.,drone_homes=(Point(0.,0.),)*3)
    routes=((1,-1),(2,-2),(3,-3));result=build(p,routes,routes,closure_limit=16)
    assert result['actual_opened_aircraft']==[2] and result['quota_slots_by_aircraft']==[0,0,1]
    assert result['unopened_aircraft_reasons']=={'0':'no_unpinned_whole_DIRECT_task','1':'no_unpinned_whole_DIRECT_task'}
    assert len(result['removed_task_ids'])==1 and result['effective_closure_limit']==1


def test_variant_order_and_all_metrics_are_deterministic_and_fixed_for_a_beam():
    p,base,raw=fixture()
    a=build(p,base,raw,closure_limit=8,variant=1);b=build(p,base,raw,closure_limit=8,variant=1)
    for key in ('removed_task_ids','quota_slots_by_aircraft','actual_opened_aircraft','evidence'):
        assert a[key]==b[key]
    assert a['removed_task_ids']!=build(p,base,raw,closure_limit=8,variant=0)['removed_task_ids']
    for i,t in enumerate(a['removed_task_ids']):assert by_task(a)[t]['repair_order_index']==i


def test_remove_closure_only_removes_unique_whole_direct_pairs_and_conserves_pins():
    p,base,raw=fixture()
    removed=closure.remove_closure(p,raw,(9,3,11),pinned_task_ids=PINS)
    assert removed['quota_slots_by_aircraft']==[1,0,1,1,0,0,0,0]
    assert removed['routes'][0]==tuple(v for v in raw[0] if abs(v)!=3)
    for ids in ((1,), (9,9), (999,), (True,)):
        with pytest.raises(ValueError):closure.remove_closure(p,raw,ids,pinned_task_ids=PINS)
    p,base,raw=fixture(direct=True)
    with pytest.raises(ValueError,match='pinned'):
        closure.remove_closure(p,raw,(1,),pinned_task_ids=PINS)


@pytest.mark.parametrize('kind',['base_relay','raw_partial','extra_relay','quota','pins','comp'])
def test_untrue_complete_source_or_native_roles_are_rejected(kind):
    p,base,raw=fixture();pins=PINS;comp=()
    if kind=='base_relay':base=raw
    elif kind=='raw_partial':raw=(raw[0][:-2],*raw[1:])
    elif kind=='extra_relay':
        legs=dict(p.leg_registry);legs[201]=TransportLeg(201,3,'RELAY_IN',1);legs[202]=TransportLeg(202,3,'RELAY_OUT',1)
        p=replace(p,leg_registry=legs,task_relay_candidates={**p.task_relay_candidates,3:(1,)})
        raw=((201,-201,*raw[0][2:]),(*raw[1],202,-202),*raw[2:])
    elif kind=='quota':raw=(raw[0][:-2],(*raw[1],6,-6),*raw[2:])
    elif kind=='pins':pins=(1,2,3)
    else:comp=(1,3)
    with pytest.raises(ValueError):closure.build_closure(p,base,raw,pins,compensation_task_ids=comp)


@pytest.mark.parametrize('field,value',[('closure_limit',-1),('partner_depth',5),('variant',-1),('closure_limit',True)])
def test_invalid_selection_parameters_do_not_create_partial_evidence(field,value):
    p,base,raw=fixture()
    with pytest.raises(ValueError):closure.build_closure(p,base,raw,PINS,**{field:value})


def test_absolute_deadline_covers_full_schedule_and_no_partial_result_is_published(monkeypatch):
    p,base,raw=fixture()
    with pytest.raises(closure.Deadline):closure.build_closure(p,base,raw,PINS,deadline=perf_counter()-1.)
    real=closure.evaluate_solution;clock=[perf_counter()];deadline=clock[0]+10.
    monkeypatch.setattr(closure,'perf_counter',lambda:clock[0])
    def overrun(*a,**k):
        value=real(*a,**k);clock[0]=deadline+1.;return value
    monkeypatch.setattr(closure,'evaluate_solution',overrun)
    with pytest.raises(closure.Deadline):closure.build_closure(p,base,raw,PINS,deadline=deadline)
