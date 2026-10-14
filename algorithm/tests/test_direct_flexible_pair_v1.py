"""Whole DIRECT pair freedom, exact clocks and honest partial coverage."""
from dataclasses import replace
from time import perf_counter

import pytest
from uav_dispatch import Point, Problem, Task
from uav_dispatch.validation import evaluate_solution
from algorithm.experiments import direct_flexible_pair_v1 as method

W, S = (.5, .25, .25), (3., 100., 100.)


def fixture():
    p = Problem((Task(1, Point(0., 0.), Point(1., 0.), 1.),
        Task(2, Point(0., 0.), Point(11., 0.), 11.),
        Task(3, Point(1., 0.), Point(10., 0.), 10.)), drone_count=1,
        capacity=2, max_tasks_per_drone=3, speed_km_per_min=1., drone_homes=(Point(0., 0.),))
    return p, (3, -3), {1: 21., 2: 33., 3: 10.}


def test_true_native_interleaving_beats_every_contiguous_pair_block_and_preserves_six_order_pool():
    p, native, original = fixture(); coverage = {}
    blocks = method.costs.direct_pair_options(p, native, 0, (1, 2), deadline=perf_counter()+2.,
        weights=W, scales=S, option_limit=3, original_delivery_times=original)
    assert min(r['score'].late_count for r in blocks) > 0
    result = method.pair_options(p, native, 0, (1, 2), deadline=perf_counter()+2., weights=W,
        scales=S, original_delivery_times=original, coverage=coverage)
    winner = min(result, key=lambda r:r['_flexible_pair_key'])
    assert winner['score'].late_count == 0 and winner['score'].distance_km == 11.
    assert winner['delivery_times_min'] == {1: 1., 3: 10., 2: 11.}
    assert winner['route'].index(-1) < winner['route'].index(3) < winner['route'].index(-3) < winner['route'].index(-2)
    checked = evaluate_solution(p, (winner['route'],), _force_full=True)
    assert checked.valid and checked.score == winner['score']
    assert coverage['flexible_pair_completed'] and coverage['flexible_insertion_orders_completed'] == [[1, 2], [2, 1]]
    assert coverage['six_block_enumeration_completed']
    assert coverage['six_block_layouts_evaluated'] == 6*coverage['six_block_empty_gaps_evaluated'] == 12
    assert all(row['completed'] and row['second_parent_records_completed'] == row['first_records_retained']
        and row['first_present_task_ids'] != row['second_present_task_ids'] for row in coverage['pipeline_records'])
    assert not coverage['first_partial_is_complete_candidate'] and len(result) <= 3
    assert min(r['_flexible_pair_key'] for r in result) <= min(method.costs.weighted_key(r['score'], W, S) for r in blocks)


def test_mid_enumeration_deadline_preserves_actual_progress_but_never_claims_a_complete_pipeline(monkeypatch):
    p, native, original = fixture(); clock = [perf_counter()]; deadline = clock[0]+2.; calls = [0]
    actual = method._scored; coverage = {}
    monkeypatch.setattr(method, 'perf_counter', lambda:clock[0])
    def bounded(*args, **kwargs):
        record = actual(*args, **kwargs); calls[0] += 1
        if calls[0] == 2: clock[0] = deadline+1.
        return record
    monkeypatch.setattr(method, '_scored', bounded)
    with pytest.raises(method.Deadline):
        method.pair_options(p, native, 0, (1, 2), deadline=deadline, weights=W,
            scales=S, original_delivery_times=original, coverage=coverage)
    assert coverage['legal_PD_positions_full_scored'] == 2
    assert coverage['pipeline_records'][0]['first_legal_PD_positions_full_scored'] == 2
    assert not coverage['flexible_pair_completed'] and not coverage['flexible_insertion_orders_completed']
    assert not coverage['six_block_enumeration_completed'] and native == (3, -3)


def test_pair_skeleton_and_final_quota_are_checked_before_partial_insertion():
    p, native, original = fixture()
    with pytest.raises(ValueError):
        method.pair_options(p, (1, -1, *native), 0, (1, 2), deadline=perf_counter()+2., weights=W, scales=S)
    with pytest.raises(ValueError):
        method.pair_options(p, native, 0, (1, 1), deadline=perf_counter()+2., weights=W, scales=S)
    constrained = replace(p, drone_count=2, drone_homes=(Point(0., 0.), Point(0., 0.)), max_tasks_per_drone=2)
    with pytest.raises(ValueError, match='quota'):
        method.pair_options(constrained, native, 0, (1, 2),
            deadline=perf_counter()+2., weights=W, scales=S)
