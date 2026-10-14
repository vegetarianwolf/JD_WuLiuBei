"""Small whole-task neighbourhood checks; no official-instance search."""
from random import Random

import pytest

import uav_dispatch.direct_task_exchange as exchange
import uav_dispatch.incumbent_polish as polish
from algorithm.experiments import weighted_direct_objective as weighted
from uav_dispatch import Point, Problem, RelayStation, Task, TransportLeg, evaluate_solution
from uav_dispatch.alns import ALNSConfig, solve_alns, solve_relay_staged
from uav_dispatch.model import Score
from uav_dispatch.route_exchange import bounded_tail_exchange
from uav_dispatch.search import RouteEvaluator


def _interleaved_problem():
    tasks = tuple(Task(i, Point(x, 0), Point(x, 0), 15.)
                  for i, x in enumerate((10., -11., -10., 11.), 1))
    return Problem(tasks, drone_count=2, max_tasks_per_drone=2), ((1, 2, -1, -2), (3, 4, -3, -4))


def test_interleaved_full_fleet_improves_without_any_nonempty_zero_load_cut():
    problem, routes = _interleaved_problem()
    evaluator, stats = RouteEvaluator(problem), {}
    assert bounded_tail_exchange(problem, evaluator, routes) is routes
    changed = exchange.bounded_task_exchange(problem, evaluator, routes, statistics=stats)
    before, after = (evaluate_solution(problem, r, _force_full=True) for r in (routes, changed))
    assert after.valid and after.score < before.score
    assert after.score.late_count == 0 and before.score.late_count == 4
    assert [sum(v < 0 for v in r) for r in changed] == [2, 2]
    assert [[v > 0 for v in r] for r in routes] == [[v > 0 for v in r] for r in changed]
    assert all(route.index(task) < route.index(-task) for route in changed for task in route if task > 0)
    assert stats['full_checks'] == 2 and stats['improvements'] == 1
    assert stats['before_score'] == (4, before.score.total_lateness_min, 146.)
    assert stats['after_score'] == (0, 0., 26.)


def test_weighted_score_can_accept_a_move_the_lexicographic_score_rejects():
    problem = Problem((Task(1, Point(4, 0), Point(4, 0), 4.5),
                       Task(2, Point(.1, 0), Point(.1, 0), 0.)),
                      drone_count=2, max_tasks_per_drone=1,
                      drone_homes=(Point(0, 0), Point(10, 0)))
    routes = ((1, -1), (2, -2))
    assert exchange.bounded_task_exchange(problem, RouteEvaluator(problem), routes) is routes
    with weighted.objective(Score, (200., 100., 100.)):
        changed = exchange.bounded_task_exchange(problem, RouteEvaluator(problem), routes)
        before, after = (evaluate_solution(problem, r, _force_full=True) for r in (routes, changed))
        assert changed == ((2, -2), (1, -1))
        assert after.score < before.score
        assert after.score.late_count == 2 > before.score.late_count == 1
        assert after.score.total_lateness_min < before.score.total_lateness_min
        assert after.score.distance_km < before.score.distance_km
    assert after.score > before.score


@pytest.mark.parametrize('seed', [3, 11])
def test_deterministic_shortlist_preserves_all_hard_constraints_on_underfilled_routes(seed):
    rng = Random(seed)
    tasks = tuple(Task(i, Point(rng.uniform(-5, 5), rng.uniform(-5, 5)),
                       Point(rng.uniform(-5, 5), rng.uniform(-5, 5)), rng.uniform(2, 25))
                  for i in range(1, 9))
    problem = Problem(tasks, drone_count=3, max_tasks_per_drone=3,
                      drone_homes=(Point(-4, 0), Point(0, 0), Point(4, 0)))
    routes = ((1, 2, -1, -2, 3, -3), (4, 5, -4, -5, 6, -6), (7, 8, -7, -8))
    a = exchange.bounded_task_exchange(problem, RouteEvaluator(problem), routes)
    b = exchange.bounded_task_exchange(problem, RouteEvaluator(problem), routes)
    assert a == b
    checked = evaluate_solution(problem, a, _force_full=True)
    assert checked.valid and checked.score <= evaluate_solution(problem, routes, _force_full=True).score
    assert [[v > 0 for v in r] for r in a] == [[v > 0 for v in r] for r in routes]
    assert [sum(v < 0 for v in r) for r in a] == [3, 3, 2]


def test_candidate_and_shortlisting_caps_and_only_two_route_scores_per_probe(monkeypatch):
    tasks = tuple(Task(i, Point(0, 0), Point(0, 0), 100.) for i in range(1, 73))
    problem = Problem(tasks, drone_count=4, max_tasks_per_drone=18)
    routes = tuple(tuple(v for i in range(start, start + 18) for v in (i, -i))
                   for start in (1, 19, 37, 55))
    evaluator, stats, calls = RouteEvaluator(problem), {}, []
    original = evaluator.evaluate
    def capture(route, **kwargs):
        calls.append(tuple(route))
        return original(route, **kwargs)
    monkeypatch.setattr(evaluator, 'evaluate', capture)
    assert exchange.bounded_task_exchange(problem, evaluator, routes,
                                          max_candidates=9999, statistics=stats) is routes
    assert stats['shortlist_probes'] <= 64 * 56
    assert stats['shortlisted_candidates'] <= 256 and stats['candidates'] <= 256
    assert len(calls) == 4 + 2 * stats['candidates']
    assert stats['full_checks'] == 1
    stats = {}
    exchange.bounded_task_exchange(problem, evaluator, routes, max_candidates=1, statistics=stats)
    assert stats['candidates'] == 1


def test_expired_or_disabled_pass_does_not_evaluate(monkeypatch):
    problem, routes = _interleaved_problem()
    evaluator = RouteEvaluator(problem)
    monkeypatch.setattr(exchange, 'evaluate_solution', lambda *a, **k: pytest.fail('unneeded validation'))
    assert exchange.bounded_task_exchange(problem, evaluator, routes, max_candidates=0) is routes
    monkeypatch.setattr(exchange, 'perf_counter', lambda: 2.)
    stats = {}
    assert exchange.bounded_task_exchange(problem, evaluator, routes, deadline=1., statistics=stats) is routes
    assert stats == {'deadline_reached': 1}


def test_full_canonical_check_vetoes_locally_better_pair(monkeypatch):
    problem, routes = _interleaved_problem()
    baseline = evaluate_solution(problem, routes, _force_full=True)
    monkeypatch.setattr(exchange, 'evaluate_solution', lambda *a, **k: baseline)
    stats = {}
    assert exchange.bounded_task_exchange(problem, RouteEvaluator(problem), routes, statistics=stats) is routes
    assert stats['full_checks'] > 1 and stats.get('improvements', 0) == 0
    assert stats['before_score'] == stats['after_score']


def test_over_budget_canonical_candidate_is_rejected(monkeypatch):
    problem, routes = _interleaved_problem()
    now, original = [0.], exchange.evaluate_solution
    def canonical(*args, **kwargs):
        checked = original(*args, **kwargs)
        if args[1] != routes:
            now[0] = 2.
        return checked
    monkeypatch.setattr(exchange, 'perf_counter', lambda: now[0])
    monkeypatch.setattr(exchange, 'evaluate_solution', canonical)
    stats = {}
    assert exchange.bounded_task_exchange(problem, RouteEvaluator(problem), routes,
                                          deadline=1., statistics=stats) is routes
    assert stats['deadline_reached'] == 1 and stats.get('improvements', 0) == 0


def test_deadline_retains_the_last_already_verified_checkpoint(monkeypatch):
    problem, routes = _interleaved_problem()
    stats = {}
    monkeypatch.setattr(exchange, 'perf_counter', lambda: 2. if stats.get('improvements') else 0.)
    changed = exchange.bounded_task_exchange(problem, RouteEvaluator(problem), routes,
                                             deadline=1., statistics=stats)
    assert changed != routes and evaluate_solution(problem, changed, _force_full=True).valid
    assert stats['improvements'] == 1 and stats['deadline_reached'] == 1
    assert stats['after_score'] < stats['before_score']


def test_invalid_input_relay_events_and_mismatched_evaluator_are_reported():
    problem, routes = _interleaved_problem()
    evaluator = RouteEvaluator(problem)
    with pytest.raises(ValueError, match='输入解非法'):
        exchange.bounded_task_exchange(problem, evaluator, ((1, 2, -1, -2), (3, -3)))
    with pytest.raises(ValueError, match='输入解非法'):
        exchange.bounded_task_exchange(problem, evaluator, ((-1, 2, 1, -2), (3, 4, -3, -4)))
    with pytest.raises(ValueError, match='一条路线'):
        exchange.bounded_task_exchange(problem, evaluator, routes + ((),))
    with pytest.raises(ValueError, match='非负整数'):
        exchange.bounded_task_exchange(problem, evaluator, routes, max_candidates=-1)
    with pytest.raises(ValueError, match='非负整数'):
        exchange.bounded_task_exchange(problem, evaluator, routes, max_candidates=True)
    other, _ = _interleaved_problem()
    with pytest.raises(ValueError, match='必须一致'):
        exchange.bounded_task_exchange(problem, RouteEvaluator(other), routes)
    relay = Problem((Task(1, Point(1, 0), Point(2, 0), 10.),), drone_count=2,
                    relay_stations=(RelayStation(1, 0, 0),),
                    leg_registry={2: TransportLeg(2, 1, 'RELAY_IN', 1),
                                  3: TransportLeg(3, 1, 'RELAY_OUT', 1)},
                    task_relay_candidates={1: (1,)})
    with pytest.raises(ValueError, match='DIRECT'):
        exchange.bounded_task_exchange(relay, RouteEvaluator(relay), ((2, -2), (3, -3)))
    assert exchange.bounded_task_exchange(relay, RouteEvaluator(relay), ((1, -1), ())) == ((1, -1), ())


def test_terminal_default_is_closed_and_original_step_order_is_preserved(monkeypatch):
    problem, routes = _interleaved_problem()
    assert ALNSConfig().enable_terminal_task_exchange is False
    events = []
    def home(p, e, r, **kwargs):
        events.append('home')
        return r
    def tail(p, e, r, **kwargs):
        events.append('tail')
        return r
    def task(p, e, r, **kwargs):
        events.append('task')
        assert kwargs['deadline'] == pytest.approx(.998)
        return r
    monkeypatch.setattr(polish, 'reassign_route_homes', home)
    monkeypatch.setattr(polish, 'bounded_tail_exchange', tail)
    monkeypatch.setattr(polish, 'bounded_task_exchange', task)
    monkeypatch.setattr(polish, 'perf_counter', lambda: 0.)
    stats = {}
    polish.polish_direct_incumbent(problem, RouteEvaluator(problem), routes, deadline=1., statistics=stats)
    assert events == ['home', 'tail', 'home'] and all(type(v) is int for v in stats.values())
    events.clear()
    polish.polish_direct_incumbent(problem, RouteEvaluator(problem), routes,
                                  deadline=1., enable_task_exchange=True)
    assert events == ['home', 'tail', 'home', 'task']


def test_terminal_wrapper_preserves_task_checkpoint_when_later_probe_expires(monkeypatch):
    problem, routes = _interleaved_problem()
    checked = exchange.bounded_task_exchange(problem, RouteEvaluator(problem), routes)
    now = [0.]
    monkeypatch.setattr(polish, 'perf_counter', lambda: now[0])
    monkeypatch.setattr(polish, 'reassign_route_homes', lambda p, e, r, **k: r)
    monkeypatch.setattr(polish, 'bounded_tail_exchange', lambda p, e, r, **k: r)
    def task(p, e, r, **kwargs):
        # The primitive's return is a checkpoint checked before its deadline;
        # its next probe consumes the last 2 ms and exits without adoption.
        now[0] = .999
        return checked
    monkeypatch.setattr(polish, 'bounded_task_exchange', task)
    changed = polish.polish_direct_incumbent(problem, RouteEvaluator(problem), routes,
                                             deadline=1., enable_task_exchange=True)
    assert changed == checked


def test_alns_flag_off_matches_default_and_relay_phase_never_enables_exchange(monkeypatch):
    problem, routes = _interleaved_problem()
    calls = []
    def terminal(p, e, r, **kwargs):
        calls.append(kwargs['enable_task_exchange'])
        return r
    monkeypatch.setattr(polish, 'polish_direct_incumbent', terminal)
    a = solve_alns(problem, config=ALNSConfig(max_iterations=6, relay_enabled=False), initial_routes=routes)
    b = solve_alns(problem, config=ALNSConfig(max_iterations=6, relay_enabled=False,
                                             enable_terminal_task_exchange=False), initial_routes=routes)
    assert a.routes == b.routes and a.evaluation == b.evaluation and calls == [False, False]
    assert a.metadata['operator_weights'] == b.metadata['operator_weights']
    c = solve_alns(problem, config=ALNSConfig(max_iterations=6, relay_enabled=False,
                                             enable_terminal_task_exchange=True), initial_routes=routes)
    assert calls[-1] is True and c.metadata['terminal_polish']['task_exchange_active'] is True
    relay = Problem(problem.tasks, drone_count=2, max_tasks_per_drone=2,
                    relay_stations=(RelayStation(1, 0, 0),),
                    leg_registry={5: TransportLeg(5, 1, 'RELAY_IN', 1),
                                  6: TransportLeg(6, 1, 'RELAY_OUT', 1)},
                    task_relay_candidates={1: (1,)})
    count = len(calls)
    result = solve_alns(relay, config=ALNSConfig(max_iterations=0, relay_enabled=True,
                                                 enable_terminal_task_exchange=True), initial_routes=routes)
    assert len(calls) == count
    assert result.metadata['terminal_polish']['task_exchange_active'] is False
    staged = solve_relay_staged(
        relay, config=ALNSConfig(max_iterations=4, relay_enabled=True,
                                relay_direct_warmup_fraction=.5,
                                enable_relay_seed=False, enable_relay_refine=False,
                                enable_terminal_task_exchange=True), initial_routes=routes,
    )
    assert len(calls) == count + 1 and calls[-1] is True
    assert staged.metadata['terminal_polish']['phase'] == 'direct_warmup'
    assert staged.metadata['terminal_polish']['task_exchange_active'] is True
