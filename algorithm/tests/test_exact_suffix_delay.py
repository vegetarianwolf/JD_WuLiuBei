"""Uniform-shift math and small physical examples, never official optimization."""
from dataclasses import replace
from math import fsum
from time import perf_counter

import pytest

from algorithm.experiments import exact_suffix_delay as exact


def test_strict_EPS_counts_and_L_ignore_small_positive_ontime_terms():
    epsilon=exact.EPSILON_MIN
    values=(-5.,0.,epsilon/2,epsilon,epsilon*1.001,2.)
    result=exact.shifted_lateness(exact.build_lateness_index(values),0.)
    assert result['late_count']==2
    assert result['total_lateness_min']==pytest.approx(2.+epsilon*1.001,abs=1e-15)
    assert result['boundary_sensitive'] and result['needs_exact_route_replay']
    assert result['global_objective_bound'] is False and result['complete_solution'] is False


def test_positive_shift_prices_previously_early_zero_and_old_late_together():
    values=(-10.,-1.,0.,2.)
    result=exact.shifted_lateness(exact.build_lateness_index(values),3.)
    assert result['late_count']==3 and result['total_lateness_min']==10.
    assert not result['boundary_sensitive'] and result['sum_within_declared_tolerance']


def test_large_cancellation_falls_back_to_actual_shifted_values():
    values=(-1e16+2.,-1e16+4.)
    result=exact.shifted_lateness(exact.build_lateness_index(values),1e16)
    assert result['summation_fallback_used']
    assert result['late_count']==2 and result['total_lateness_min']==fsum(v+1e16 for v in values)==6.
    assert result['sum_within_declared_tolerance']


def test_overflowed_raw_index_can_still_answer_finite_cancelled_zero_lateness():
    index=exact.build_lateness_index((-1e308,-1e308))
    assert index.suffix_sums_min[0] is None
    result=exact.shifted_lateness(index,1e308)
    assert result['late_count']==0 and result['total_lateness_min']==0.
    with pytest.raises(ValueError,match='nonfinite'):
        exact.shifted_lateness(exact.build_lateness_index((1e308,1e308)),0.)


def record():
    return dict(route=(1,-1,2,-2,3,-3),event_times_min=(0.,2.,3.,6.,6.,9.),
        delivery_times_min={1:2.,2:6.,3:9.})


def test_middle_and_tail_event_gaps_preserve_prefix_and_include_already_held_native():
    due={1:1.,2:7.,3:8.}
    index=exact.index_direct_suffix(record(),due,3)  # task2 was picked up, not yet delivered.
    result=exact.shift_direct_suffix(index,2.,insertion_kind='source_IN')
    assert index.load_at_gap==1
    assert result['suffix_task_ids']==[2,3]
    assert result['prefix_late_count']==1 and result['prefix_lateness_min']==1.
    assert result['late_count']==3 and result['total_lateness_min']==5.
    tail=exact.shift_direct_suffix(exact.index_direct_suffix(record(),due,6),1000.,insertion_kind='source_IN')
    assert tail['suffix_task_ids']==[] and tail['late_count']==2 and tail['total_lateness_min']==2.
    assert tail['distance_included'] is False and tail['source_IN_adds_final_delivery_count']==0


def test_empty_native_suffix_is_valid_without_fake_selected_delivery_or_distance():
    index=exact.index_direct_suffix(dict(route=(),event_times_min=(),delivery_times_min={}),{},0)
    result=exact.shift_direct_suffix(index,20.,insertion_kind='receiver_OUT')
    assert result['late_count']==0 and result['total_lateness_min']==0.
    assert not result['selected_OUT_deliveries_included'] and not result['distance_included']


def test_raw_x_and_clock_reassociation_EPS_disagree_and_require_actual_route_replay():
    epsilon=exact.EPSILON_MIN
    index=exact.index_direct_suffix(dict(route=(1,-1),event_times_min=(0.,1.),delivery_times_min={1:1.}),{1:1.},0)
    raw=exact.shifted_lateness(index.suffix,epsilon)
    actual=exact.shift_direct_suffix(index,epsilon,insertion_kind='receiver_OUT')
    assert raw['late_count']==0  # 0 + epsilon is exactly the threshold.
    assert (1.+epsilon)-1.>epsilon
    assert actual['shifted_suffix_late_count']==1
    assert actual['clock_reassociation_fallback_used'] and actual['needs_exact_route_replay']
    assert actual['raw_uniform_result']['late_count']==0


def test_existing_load_one_source_contiguous_IN_matches_real_route_cost():
    from algorithm.experiments import deadline_window_costs as costs
    from algorithm.tests.test_split_feeder_catalogue import fixture
    p,routes=fixture(False)
    p=replace(p,tasks=tuple(replace(t,deadline_min=1.) if t.id==3 else t for t in p.tasks))
    base=costs.route_cost(p,routes[0],0,deadline=perf_counter()+2.)
    candidate=routes[0][:1]+(101,-101)+routes[0][1:]
    real=costs.route_cost(p,candidate,0,deadline=perf_counter()+2.)
    shift=(real['score'].distance_km-base['score'].distance_km)/p.speed_km_per_min
    surface=exact.index_direct_suffix(base,{t.id:t.deadline_min for t in p.tasks},1)
    result=exact.shift_direct_suffix(surface,shift,insertion_kind='source_IN')
    assert surface.load_at_gap==1 and shift==8.
    assert result['late_count']==real['score'].late_count==1
    assert result['total_lateness_min']==real['score'].total_lateness_min==7.
    assert real['final_delivery_count']==3 and real['availability']=={1:4.}


def test_C_retimed_deleted_skeleton_and_actual_wait_price_native_separately_from_selected():
    from algorithm.experiments import deadline_window_costs as costs
    from algorithm.tests.test_split_feeder_catalogue import fixture
    p,routes=fixture(False)
    p=replace(p,tasks=tuple(replace(t,deadline_min=12.) if t.id==9 else t for t in p.tasks))
    original=costs.route_cost(p,routes[2],2,deadline=perf_counter()+2.)
    native=costs.route_cost(p,(9,-9),2,deadline=perf_counter()+2.)
    real=costs.route_cost(p,(102,104,-102,-104,9,-9),2,availability={1:4.,2:7.},
        selected_pair=(102,104),deadline=perf_counter()+2.)
    wait=7.;delta_D=real['score'].distance_km-native['score'].distance_km
    result=exact.shift_direct_suffix(exact.index_direct_suffix(native,{9:12.},0),delta_D/p.speed_km_per_min+wait,insertion_kind='receiver_OUT')
    assert native['delivery_times_min'][9]==0. and original['delivery_times_min'][9]>12.
    assert result['late_count']==1 and result['total_lateness_min']==5.
    selected=[real['delivery_times_min'][t]-p.task(t).deadline_min for t in (1,2)]
    selected=[v for v in selected if v>exact.EPSILON_MIN]
    assert result['late_count']+len(selected)==real['score'].late_count
    assert result['total_lateness_min']+sum(selected)==real['score'].total_lateness_min


def test_declared_source_and_double_receiver_capacity_frontiers_are_distinct():
    native=dict(route=(1,2,-1,-2),event_times_min=(0.,0.,1.,2.),delivery_times_min={1:1.,2:2.})
    loaded=exact.index_direct_suffix(native,{1:10.,2:10.},2)
    with pytest.raises(ValueError,match='load0 or1'):exact.shift_direct_suffix(loaded,1.,insertion_kind='source_IN')
    one=exact.index_direct_suffix(native,{1:10.,2:10.},1)
    with pytest.raises(ValueError,match='load0'):exact.shift_direct_suffix(one,1.,insertion_kind='receiver_OUT')


@pytest.mark.parametrize('value',(-1.,float('inf'),float('nan'),True))
def test_invalid_shift_never_produces_a_price(value):
    with pytest.raises(ValueError):exact.shifted_lateness(exact.build_lateness_index((0.,1.)),value)


def test_non_DIRECT_incomplete_or_unbound_record_clocks_are_rejected():
    due={1:1.,2:7.,3:8.}
    for invalid in (dict(record(),route=(101,-101)),dict(record(),delivery_times_min={1:1.,2:6.,3:9.}),
                    dict(record(),route=(-1,1,2,-2,3,-3)),dict(record(),route=(1,-1,2,-2,3))):
        with pytest.raises(ValueError):exact.index_direct_suffix(invalid,due,0)


def test_optional_deadline_covers_index_and_query():
    with pytest.raises(exact.Deadline):exact.build_lateness_index((0.,),deadline=perf_counter()-1)
    index=exact.build_lateness_index((0.,))
    with pytest.raises(exact.Deadline):exact.shifted_lateness(index,1.,deadline=perf_counter()-1)
