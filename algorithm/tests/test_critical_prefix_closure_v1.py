"""Real small prefix/DAG previews and quotas; no official-data optimization."""
from dataclasses import replace
import json
from time import perf_counter

import pytest

from uav_dispatch import Point,Problem,Task,RelayStation,TransportLeg
from uav_dispatch.relay import GlobalRelayEvaluator,build_plan_index
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import critical_prefix_closure_v1 as c
from algorithm.tests.test_relay_damage_closure import fixture as eight_fixture


def fixture():
    P=Point;H=P(0.,0.)
    tasks=[Task(1,H,P(10.,0.),100.),Task(2,H,P(11.,0.),100.),
        Task(3,H,P(4.,0.),100.),Task(4,H,H,100.),Task(5,H,H,100.),
        Task(6,H,P(3.,0.),100.),Task(7,H,H,100.),Task(8,H,H,100.),Task(9,H,P(2.,0.),100.)]
    registry={101:TransportLeg(101,1,'RELAY_IN',1),102:TransportLeg(102,1,'RELAY_OUT',1),
              103:TransportLeg(103,2,'RELAY_IN',1),104:TransportLeg(104,2,'RELAY_OUT',1)}
    p=Problem(tuple(tasks),drone_count=3,max_tasks_per_drone=3,capacity=2,speed_km_per_min=1.,
        drone_homes=(H,H,H),relay_stations=(RelayStation(1,0.,0.),),leg_registry=registry,
        task_relay_candidates={1:(1,),2:(1,)})
    base=((3,-3,4,-4,5,-5),(6,-6,7,-7,8,-8),(1,-1,2,-2,9,-9))
    raw=(base[0]+(101,-101),base[1]+(103,-103),(9,-9,102,104,-102,-104))
    return p,base,raw


def build(p=None,base=None,raw=None,**kwargs):
    if p is None:p,base,raw=fixture()
    args=dict(pinned_task_ids=(1,2),focus_task_ids=(1,2),closure_limit=6,deadline=perf_counter()+4.)
    args.update(kwargs)
    return c.build_closure(p,base,raw,**args)


def rows(result):return {r['task_id']:r for r in result['per_task_evidence']}


def test_zero_damage_prefix_is_really_selected_and_shortens_real_source_clock():
    p,base,raw=fixture();result=build(p,base,raw,closure_limit=3);r=rows(result)
    assert r[3]['base_delivery_min']==r[3]['raw_delivery_min']==4.
    assert r[3]['delta_lateness_min']==0. and not r[3]['newly_late']
    assert r[3]['signed_improvement_min']['in_drop:1']==8.
    assert 3 in result['removed_task_ids']
    assert result['removed_task_ids']==[3,6,9]


def test_masked_other_source_and_C_arrival_have_independent_true_directions():
    result=build(closure_limit=3);r=rows(result);targets={t['target_id']:t for t in result['targets']}
    assert targets['in_drop:1']['clock_min']==8. and targets['in_drop:2']['clock_min']==6.
    assert r[6]['signed_improvement_min']['in_drop:2']==6.
    assert r[6]['signed_improvement_min']['delivery:2']==0.  # later source1 masks it
    assert r[9]['signed_improvement_min']['receiver_arrival']==4.
    assert r[9]['signed_improvement_min']['delivery:2']==0.
    assert {3,6,9}<=set(result['removed_task_ids'])


def test_actual_DAG_delivery_ancestors_include_source_native_not_only_C_route():
    result=build();targets={t['target_id']:t for t in result['targets']}
    assert {3,4,5,6,7,8,9}<=set(targets['delivery:2']['causal_prefix_task_ids'])
    assert targets['receiver_arrival']['causal_prefix_task_ids']==[9]
    assert targets['in_drop:1']['causal_prefix_task_ids']==[3,4,5]


def test_DIRECT_focus_never_pins_or_invents_relay_arrival_and_self_delta_is_undefined():
    p,base,_=fixture();result=build(p,base,base,pinned_task_ids=())
    assert result['eligible_native_count']==9 and {1,2}<=set(result['eligible_task_ids'])
    assert all(t['type']=='delivery' for t in result['targets'])
    r=rows(result)
    assert r[1]['target_clock_after_min']['delivery:1'] is None
    assert r[1]['signed_improvement_min']['delivery:1'] is None
    assert 'delivery:1' in r[1]['undefined_targets']
    assert r[1]['signed_improvement_min']['delivery:2']>0.
    assert result['expected_signature']['plans']==[] and result['pinned_task_ids']==[]


def test_all_eligible_single_whole_previews_are_actual_present_set_DAGs():
    p,base,raw=fixture();result=build(p,base,raw);expected=c.signature(p,raw,(1,2))
    assert result['statistics']['eligible_single_whole_previews']==7
    for row in result['per_task_evidence']:
        t=row['task_id'];routes=c._whole_remove(p,raw,(t,))
        preview=c.partial_preview(p,routes,pinned_task_ids=(1,2),expected_signature=expected,
            remaining_task_ids=(t,),deadline=perf_counter()+4.)
        assert row['preview_present_task_ids']==preview['present_task_ids']
        assert not row['preview_complete'] and not preview['complete']
        assert sum(row['preview_remaining_quota_slots'])==1
        values,undefined=c._target_clocks(p,preview,result['targets'])
        assert row['target_clock_after_min']==values and row['undefined_targets']==undefined


def test_real_other_five_aircraft_donors_and_exact_final_quota_conservation():
    p,base,raw=eight_fixture();result=build(p,base,raw,closure_limit=8)
    assert result['actual_opened_aircraft']==list(range(8)) and result['all_aircraft_opened']
    assert result['quota_slots_by_aircraft']==[1]*8
    assert len(result['donor_reservations'])==8
    assert set(range(11,16))<=set(result['removed_task_ids'])
    for a in range(8):
        assert result['raw_final_counts'][a]==result['pin_final_counts'][a]+result['remaining_native_final_counts'][a]+result['remaining_quota_slots'][a]
    json.dumps(result,allow_nan=False)


def test_small_cap_or_zero_cap_reports_actual_unopened_aircraft_without_fake_slots():
    p,base,raw=eight_fixture();result=build(p,base,raw,closure_limit=2)
    assert len(result['actual_opened_aircraft'])==2 and not result['all_aircraft_opened']
    assert set(result['unopened_aircraft_reasons'])==set(map(str,range(2,8)))
    assert sum(result['quota_slots_by_aircraft'])==2
    empty=build(p,base,raw,closure_limit=0)
    assert empty['removed_task_ids']==[] and empty['partial_routes']==list(map(list,raw))
    assert empty['quota_slots_by_aircraft']==[0]*8


def test_partial_preview_strict_exact_present_coverage_and_whole_pairing():
    p,_,raw=fixture();sig=c.signature(p,raw,(1,2));partial=c._whole_remove(p,raw,(3,))
    with pytest.raises(ValueError,match='coverage'):
        c.partial_preview(p,partial,pinned_task_ids=(1,2),expected_signature=sig,remaining_task_ids=(4,),deadline=perf_counter()+4.)
    broken=(tuple(v for v in raw[0] if v!=3),*raw[1:])
    with pytest.raises(ValueError):c.partial_preview(p,broken,pinned_task_ids=(1,2),expected_signature=sig,remaining_task_ids=(3,),deadline=perf_counter()+4.)
    with pytest.raises(ValueError,match='pinned'):
        c.partial_preview(p,raw,pinned_task_ids=(1,2),expected_signature=sig,remaining_task_ids=(1,),deadline=perf_counter()+4.)


def test_partial_q_is_actual_current_final_ownership_not_given_credit():
    p,_,raw=fixture();sig=c.signature(p,raw,(1,2));partial=c._whole_remove(p,raw,(3,6))
    preview=c.partial_preview(p,partial,pinned_task_ids=(1,2),expected_signature=sig,remaining_task_ids=(3,6),deadline=perf_counter()+4.)
    assert preview['remaining_quota_slots']==[1,1,0]
    with pytest.raises(ValueError,match='supplied quota'):
        c.partial_preview(p,partial,pinned_task_ids=(1,2),expected_signature=sig,remaining_task_ids=(3,6),
            remaining_quota_slots=[2,0,0],deadline=perf_counter()+4.)
    swapped=(partial[0]+(6,-6),partial[1]+(3,-3),partial[2])
    full=c.partial_preview(p,swapped,pinned_task_ids=(1,2),expected_signature=sig,remaining_task_ids=(),deadline=perf_counter()+4.)
    assert full['full_task_coverage'] and not full['complete'] and full['remaining_quota_slots']==[0,0,0]
    assert full['final_owners'][3]==1 and full['final_owners'][6]==0
    assert evaluate_solution(p,swapped,_force_full=True).valid


def test_partial_preview_is_not_standard_full_certificate_and_binds_true_waiting_clock():
    p,_,raw=fixture();sig=c.signature(p,raw,(1,2));partial=c._whole_remove(p,raw,(6,))
    assert not evaluate_solution(p,partial,_force_full=True).valid
    preview=c.partial_preview(p,partial,pinned_task_ids=(1,2),expected_signature=sig,remaining_task_ids=(6,),deadline=perf_counter()+4.)
    assert not preview['standard_full_validator_used'] and not preview['complete_solution']
    assert preview['global_schedule'].delivery_times_min[2]==19.
    evidence=c.preview_evidence(preview)
    assert evidence['delivery_times_min']['2']==19. and evidence['scoring_scope']==c.SCOPE
    json.dumps(evidence,allow_nan=False)


def test_pin_roles_site_signed_order_and_outtake_shape_are_guarded():
    p,_,raw=fixture();sig=c.signature(p,raw,(1,2))
    swapped=(raw[1],raw[0],raw[2])
    with pytest.raises(ValueError,match='roles'):
        c.partial_preview(p,swapped,pinned_task_ids=(1,2),expected_signature=sig,remaining_task_ids=(),deadline=perf_counter()+4.)
    split=(raw[0],raw[1],(9,-9,102,3,-3,104,-102,-104))
    with pytest.raises(ValueError):c.shape(p,split,(1,2))
    with pytest.raises(ValueError):c.shape(p,raw,())


def test_capacity_and_final_count_fail_even_if_global_time_DAG_could_be_scheduled():
    p,_,raw=fixture();sig=c.signature(p,raw,(1,2))
    too_loaded=((3,4,5,-3,-4,-5,101,-101),raw[1],raw[2])
    with pytest.raises(ValueError,match='capacity'):c.shape(p,too_loaded,(1,2))
    moved=(raw[0]+(6,-6),tuple(v for v in raw[1] if abs(v)!=6),raw[2])
    with pytest.raises(ValueError):c.partial_preview(p,moved,pinned_task_ids=(1,2),expected_signature=sig,remaining_task_ids=(),deadline=perf_counter()+4.)


def test_nonfinite_or_wrong_problem_schedule_cannot_be_supplied_as_a_certificate():
    p,_,raw=fixture();sig=c.signature(p,raw,(1,2));other=replace(p,speed_km_per_min=.9)
    with pytest.raises(ValueError,match='another problem'):
        c.partial_preview(p,raw,pinned_task_ids=(1,2),expected_signature=sig,remaining_task_ids=(),deadline=perf_counter()+4.,global_evaluator=GlobalRelayEvaluator(other))


def test_deterministic_common_removed_order_omitted_set_and_finite_variant():
    p,base,raw=eight_fixture();one=build(p,base,raw,closure_limit=10);two=build(p,base,raw,closure_limit=10)
    assert one['removed_task_ids']==two['removed_task_ids'] and one['targets']==two['targets']
    assert set(one['omitted_task_ids'])==set(one['eligible_task_ids'])-set(one['removed_task_ids'])
    assert one['eligible_omitted_count']==len(one['omitted_task_ids'])
    variant=build(p,base,raw,variant=1)
    assert variant['variant']==1 and sum(variant['remaining_quota_slots'])==len(variant['removed_task_ids'])
    with pytest.raises(ValueError):build(p,base,raw,partner_depth=2)


def test_expired_and_during_preview_deadline_never_return_partial_closure(monkeypatch):
    stats={}
    with pytest.raises(c.Deadline):build(deadline=perf_counter()-1.,statistics=stats)
    assert stats['deadline_reached'] and not stats['completed']
    evaluate=c.partial_preview;calls=[]
    def expire(*args,**kwargs):
        calls.append(True)
        if len(calls)==2:raise c.Deadline
        return evaluate(*args,**kwargs)
    monkeypatch.setattr(c,'partial_preview',expire)
    with pytest.raises(c.Deadline):build(statistics=stats)
    assert len(calls)==2 and not stats['completed'] and stats['deadline_reached']


def test_invalid_complete_inputs_and_fake_expected_signature_rejected():
    p,base,raw=fixture()
    with pytest.raises(ValueError):build(p,base,raw,expected_signature=c.signature(p,base,()))
    with pytest.raises(ValueError):build(p,base,raw,pinned_task_ids=())
    with pytest.raises(ValueError):build(p,raw,raw)
    with pytest.raises(ValueError):build(p,base,raw,pinned_task_ids=(1,))
