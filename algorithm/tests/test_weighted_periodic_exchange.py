"""Synthetic checks for the experiment-only periodic repair wrapper."""
import json
from random import Random
from types import SimpleNamespace

import pytest

from algorithm.experiments import weighted_direct_objective as weighted
from algorithm.experiments import weighted_periodic_exchange as periodic
from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg, evaluate_solution
from uav_dispatch.model import Score
from uav_dispatch.search import RouteEvaluator


def _crossed():
    problem = Problem(tuple(Task(i, Point(x, 0), Point(x, 0), 15.)
                            for i, x in enumerate((10., -11., -10., 11.), 1)),
                      drone_count=2, max_tasks_per_drone=2)
    return problem, ((1, 2, -1, -2), (3, 4, -3, -4))


def _module(routes, seen=None):
    def repair(*args, **kwargs):
        if seen is not None:
            seen.append((args, kwargs))
        return routes
    return SimpleNamespace(_repair=repair)


def test_interval_passthrough_argument_transparency_and_patch_restoration(monkeypatch):
    problem, routes = _crossed()
    evaluator, seen = RouteEvaluator(problem), []
    module, stats = _module(routes, seen), {}
    original = module._repair
    marker = object()
    args = (problem, evaluator, routes, (), 'greedy', 48, False, 1.)
    monkeypatch.setattr(periodic, 'perf_counter', lambda: 0.)
    with periodic.periodic_exchange(module, statistics=stats):
        for _ in range(119):
            assert module._repair(*args, test_marker=marker) is routes
        changed = module._repair(*args, test_marker=marker)
        assert changed != routes
        assert stats['repair_calls'] == 120 and stats['passes'] == 1
        assert stats['events'][0]['repair_call'] == 120
        assert stats['events'][0]['budget_seconds'] == .008
        assert all(call_args == args and kwargs == {'test_marker': marker} for call_args, kwargs in seen)
    assert module._repair is original and not periodic._active
    json.dumps(stats, allow_nan=False)


def test_interleaved_full_k_exchange_keeps_signs_and_all_constraints(monkeypatch):
    problem, routes = _crossed()
    module, stats = _module(routes), {}
    monkeypatch.setattr(periodic, 'perf_counter', lambda: 0.)
    with periodic.periodic_exchange(module, interval=1, statistics=stats):
        changed = module._repair(problem, RouteEvaluator(problem), routes, (), 'greedy', 48)
    before, after = (evaluate_solution(problem, r, _force_full=True) for r in (routes, changed))
    assert after.valid and after.score < before.score
    assert [sum(v < 0 for v in r) for r in changed] == [2, 2]
    assert [[v > 0 for v in r] for r in routes] == [[v > 0 for v in r] for r in changed]
    assert stats['improvements'] == 1 and stats['canonical_checks'] == 2
    assert stats['events'][0]['before_score']['late_count'] == 4
    assert stats['events'][0]['after_score']['late_count'] == 0


def test_pair_rotation_is_lexicographic_and_wraps_without_rng(monkeypatch):
    rng = Random(23)
    rng_state = rng.getstate()
    problem = Problem(tuple(Task(i, Point(0, 0), Point(0, 0), 10.) for i in range(1, 5)),
                      drone_count=4, max_tasks_per_drone=1)
    routes = tuple((i, -i) for i in range(1, 5))
    module, stats = _module(routes), {}
    monkeypatch.setattr(periodic, 'perf_counter', lambda: 0.)
    with periodic.periodic_exchange(module, interval=1, statistics=stats):
        for _ in range(7):
            assert module._repair(problem, RouteEvaluator(problem), routes, (), 'greedy', 48, rng=rng) is routes
    assert rng.getstate() == rng_state
    assert [event['route_pair'] for event in stats['events']] == [
        [0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3], [0, 1]]


def test_weighted_and_ordinary_score_comparisons_have_their_expected_tradeoff(monkeypatch):
    problem = Problem((Task(1, Point(4, 0), Point(4, 0), 4.5),
                       Task(2, Point(.1, 0), Point(.1, 0), 0.)),
                      drone_count=2, max_tasks_per_drone=1,
                      drone_homes=(Point(0, 0), Point(10, 0)))
    routes = ((1, -1), (2, -2))
    module = _module(routes)
    monkeypatch.setattr(periodic, 'perf_counter', lambda: 0.)
    with periodic.periodic_exchange(module, interval=1):
        assert module._repair(problem, RouteEvaluator(problem), routes, (), 'greedy', 48) is routes
    with weighted.objective(Score, (200., 100., 100.)):
        with periodic.periodic_exchange(module, interval=1) as stats:
            changed = module._repair(problem, RouteEvaluator(problem), routes, (), 'greedy', 48)
        before, after = (evaluate_solution(problem, r, _force_full=True) for r in (routes, changed))
        assert after.valid and after.score < before.score
        assert after.score.late_count > before.score.late_count
        assert stats['improvements'] == 1
    assert after.score > before.score


def test_main_deadline_is_forwarded_and_expired_pass_does_not_validate(monkeypatch):
    problem, routes = _crossed()
    module, stats = _module(routes), {}
    monkeypatch.setattr(periodic, 'perf_counter', lambda: 2.)
    monkeypatch.setattr(periodic, 'evaluate_solution', lambda *a, **k: pytest.fail('already expired'))
    with periodic.periodic_exchange(module, interval=1, statistics=stats):
        assert module._repair(problem, RouteEvaluator(problem), routes, (), 'greedy', 48, deadline=1.) is routes
    event = stats['events'][0]
    assert event['deadline_reached'] and event['canonical_checks'] == event['probes'] == 0
    assert event['budget_seconds'] == 0.


def test_positional_main_deadline_can_shorten_the_eight_ms_budget(monkeypatch):
    problem, routes = _crossed()
    module = _module(routes)
    monkeypatch.setattr(periodic, 'perf_counter', lambda: 0.)
    with periodic.periodic_exchange(module, interval=1) as stats:
        module._repair(problem, RouteEvaluator(problem), routes, (), 'greedy', 48, False, .002)
    assert stats['events'][0]['budget_seconds'] == .002


def test_over_budget_canonical_candidate_is_rejected(monkeypatch):
    problem, routes = _crossed()
    module, now = _module(routes), [0.]
    original = periodic.evaluate_solution
    def canonical(*args, **kwargs):
        checked = original(*args, **kwargs)
        if args[1] != routes:
            now[0] = .009
        return checked
    monkeypatch.setattr(periodic, 'perf_counter', lambda: now[0])
    monkeypatch.setattr(periodic, 'evaluate_solution', canonical)
    with periodic.periodic_exchange(module, interval=1) as stats:
        changed = module._repair(problem, RouteEvaluator(problem), routes, (), 'greedy', 48)
    assert changed is routes and stats['improvements'] == 0
    assert stats['events'][0]['before_score'] == stats['events'][0]['after_score']
    assert stats['events'][0]['deadline_reached']


def test_later_deadline_retains_an_already_checked_checkpoint(monkeypatch):
    problem, routes = _crossed()
    module = _module(routes)
    stats = {}
    def clock():
        if stats.get('events') and stats['events'][0]['improvements']:
            return .009
        return 0.
    monkeypatch.setattr(periodic, 'perf_counter', clock)
    with periodic.periodic_exchange(module, interval=1, statistics=stats):
        changed = module._repair(problem, RouteEvaluator(problem), routes, (), 'greedy', 48)
    assert changed != routes and evaluate_solution(problem, changed, _force_full=True).valid
    assert stats['improvements'] == 1 and stats['events'][0]['deadline_reached']


def test_full_canonical_check_vetoes_a_locally_improving_pair(monkeypatch):
    problem, routes = _crossed()
    module = _module(routes)
    baseline = evaluate_solution(problem, routes, _force_full=True)
    monkeypatch.setattr(periodic, 'perf_counter', lambda: 0.)
    monkeypatch.setattr(periodic, 'evaluate_solution', lambda *a, **k: baseline)
    with periodic.periodic_exchange(module, interval=1) as stats:
        assert module._repair(problem, RouteEvaluator(problem), routes, (), 'greedy', 48) is routes
    assert stats['canonical_checks'] > 1 and stats['improvements'] == 0


def test_precision_cap_and_four_anchors_bound_ranking_work(monkeypatch):
    problem = Problem(tuple(Task(i, Point(0, 0), Point(0, 0), 10.) for i in range(1, 21)),
                      drone_count=2, max_tasks_per_drone=10)
    routes = tuple(tuple(v for i in range(start, start + 10) for v in (i, -i)) for start in (1, 11))
    module, evaluator, observed = _module(routes), RouteEvaluator(problem), []
    original = evaluator.evaluate
    def capture(route, **kwargs):
        observed.append(tuple(route))
        return original(route, **kwargs)
    monkeypatch.setattr(evaluator, 'evaluate', capture)
    monkeypatch.setattr(periodic, 'perf_counter', lambda: 0.)
    with periodic.periodic_exchange(module, interval=1) as stats:
        assert module._repair(problem, evaluator, routes, (), 'greedy', 48) is routes
    event = stats['events'][0]
    assert event['ranking_probes'] <= 2 * 4 * 10
    assert event['shortlisted_candidates'] == event['probes'] == 16
    assert len(observed) == 2 + 2 * event['probes']
    assert event['canonical_checks'] == 1


def test_context_restores_patch_after_nested_guard_and_user_exception():
    problem, routes = _crossed()
    module = _module(routes)
    original = module._repair
    with pytest.raises(RuntimeError, match='deliberate'):
        with periodic.periodic_exchange(module):
            patched = module._repair
            with pytest.raises(RuntimeError, match='Nested'):
                with periodic.periodic_exchange(module):
                    pass
            assert module._repair is patched
            raise RuntimeError('deliberate')
    assert module._repair is original and not periodic._active


def test_original_repair_exception_is_transparent_and_still_restores_patch():
    def fail(*args, **kwargs):
        raise LookupError('original repair failure')
    module = SimpleNamespace(_repair=fail)
    with pytest.raises(LookupError, match='original repair failure'):
        with periodic.periodic_exchange(module):
            module._repair(object(), object())
    assert module._repair is fail and not periodic._active


@pytest.mark.parametrize('kwargs', [
    {'interval': 0}, {'interval': True}, {'max_candidates': 17}, {'max_candidates': -1},
    {'anchors_per_route': 5}, {'pair_budget_seconds': .009}, {'pair_budget_seconds': float('nan')},
])
def test_invalid_limits_do_not_mutate_the_module(kwargs):
    module = _module(())
    original = module._repair
    with pytest.raises(ValueError):
        with periodic.periodic_exchange(module, **kwargs):
            pass
    assert module._repair is original and not periodic._active


def test_invalid_complete_input_is_reported_and_actual_relay_is_skipped(monkeypatch):
    problem, routes = _crossed()
    invalid = ((1, -1), (3, -3, 4, -4))
    module = _module(invalid)
    monkeypatch.setattr(periodic, 'perf_counter', lambda: 0.)
    with pytest.raises(ValueError, match='输入解非法'):
        with periodic.periodic_exchange(module, interval=1):
            module._repair(problem, RouteEvaluator(problem), invalid, (), 'greedy', 48)
    relay = Problem((Task(1, Point(1, 0), Point(2, 0), 10.),), drone_count=2,
                    relay_stations=(RelayStation(1, 0, 0),),
                    leg_registry={2: TransportLeg(2, 1, 'RELAY_IN', 1),
                                  3: TransportLeg(3, 1, 'RELAY_OUT', 1)},
                    task_relay_candidates={1: (1,)})
    relay_routes = ((2, -2), (3, -3))
    module = _module(relay_routes)
    with periodic.periodic_exchange(module, interval=1) as stats:
        result = module._repair(relay, RouteEvaluator(relay), relay_routes, (), 'greedy', 48,
                                relay_cfg=SimpleNamespace(enabled=True))
    assert result is relay_routes and stats['passes'] == 0
