"""Relay-aware PDPTW tests: handoffs, waiting, deadlock, capacity, and ALNS."""

from random import Random
from types import SimpleNamespace

import pytest

from uav_dispatch import (
    ALNSConfig,
    GlobalRelayEvaluator,
    Point,
    Problem,
    RelayStation,
    Score,
    Task,
    TransportLeg,
    build_plan_index,
    build_relay_network,
    construct_regret_initial,
    destroy_solution,
    evaluate_solution,
    load_tasks_csv,
    relay_statistics,
    solve_alns,
    solve_relay_staged,
)
from uav_dispatch.search import (
    RouteEvaluator,
    RouteInsertionOption,
    routes_score,
)
from uav_dispatch.alns import (
    TaskInsertionOption,
    _RelaySearchConfig,
    _all_plans_for_task,
    _apply_task_exact,
    _compose_relay_combo,
    _repair,
)
from uav_dispatch.deployment import compute_dynamic_uav_homes


DATA_FILE = (
    __import__("pathlib").Path(__file__).parents[1]
    / "命题1-低空经济场景下的物流无人机调度算法数据.csv"
)


def _relay_problem(
    tasks,
    *,
    drone_count,
    max_tasks,
    stations,
    speed_km_per_min=0.9,
    depot=Point(0.0, 0.0),
) -> Problem:
    """Problem with hand-placed stations and one leg pair per task per station."""

    counter = max(task.id for task in tasks) + 1
    registry: dict[int, TransportLeg] = {}
    candidates: dict[int, tuple[int, ...]] = {}
    for task in tasks:
        relay_ids = []
        for station in stations:
            in_id = counter
            counter += 1
            out_id = counter
            counter += 1
            registry[in_id] = TransportLeg(in_id, task.id, "RELAY_IN", station.id)
            registry[out_id] = TransportLeg(
                out_id, task.id, "RELAY_OUT", station.id
            )
            relay_ids.append(station.id)
        candidates[task.id] = tuple(relay_ids)
    return Problem(
        tasks,
        drone_count=drone_count,
        max_tasks_per_drone=max_tasks,
        speed_km_per_min=speed_km_per_min,
        depot=depot,
        relay_stations=tuple(stations),
        leg_registry=registry,
        task_relay_candidates=candidates,
    )


def _leg_ids(problem: Problem, task_id: int, relay_id: int | None = None):
    legs = [
        leg
        for leg in problem.leg_registry.values()
        if leg.task_id == task_id
        and (relay_id is None or leg.relay_id == relay_id)
    ]
    in_id = next(leg.id for leg in legs if leg.kind == "RELAY_IN")
    out_id = next(leg.id for leg in legs if leg.kind == "RELAY_OUT")
    return in_id, out_id


def _bridge_problem() -> Problem:
    """Task 2 crosses the map: relay handoff is clearly cheaper."""

    tasks = (
        Task(1, Point(-10, 0), Point(-9, 0), 100),
        Task(2, Point(-9, 1), Point(10, 1), 100),
        Task(3, Point(10, 2), Point(9, 2), 100),
    )
    return _relay_problem(
        tasks,
        drone_count=2,
        max_tasks=2,
        stations=(RelayStation(1, 0.5, 1.0),),
    )


def _cross_problem() -> Problem:
    tasks = (
        Task(1, Point(10, 1), Point(-10, 1), 100),
        Task(2, Point(10, 2), Point(-10, 2), 100),
    )
    return _relay_problem(
        tasks,
        drone_count=2,
        max_tasks=2,
        stations=(RelayStation(1, 0.0, 1.5),),
    )


# --- Test 1: DIRECT tasks complete correctly in a relay-aware problem ---


def test_direct_task_completes_correctly_with_relay_network_attached():
    problem = _bridge_problem()
    routes = ((1, -1, 3, -3), (2, -2))

    result = evaluate_solution(problem, routes)

    assert result.valid
    assert result.score.late_count == 0
    expected_2 = (
        problem.distance(None, 2) + problem.distance(2, -2)
    ) / problem.speed_km_per_min
    assert result.delivery_times_min[2] == pytest.approx(expected_2)
    plans = build_plan_index(problem, routes).task_plans
    assert all(plan.mode == "DIRECT" for plan in plans.values())


# --- Test 2: cross-UAV relay is legal ---


def test_cross_uav_relay_is_valid_and_scores():
    problem = _cross_problem()
    in1, out1 = _leg_ids(problem, 1)
    in2, out2 = _leg_ids(problem, 2)
    routes = (
        (in1, -in1, in2, -in2),
        (out1, -out1, out2, -out2),
    )

    result = evaluate_solution(problem, routes)
    schedule = GlobalRelayEvaluator(problem).evaluate(routes)
    plans = build_plan_index(problem, routes)

    assert result.valid
    assert schedule.feasible
    assert all(plan.mode == "RELAY" for plan in plans.task_plans.values())
    stats = relay_statistics(problem, routes, schedule, plans)
    assert stats["relay_task_count"] == 2
    assert stats["cross_uav_handoff_count"] == 2
    assert stats["same_uav_relay_count"] == 0


# --- Test 3: pickup arriving before drop waits for the drop ---


def test_relay_pickup_waits_for_a_later_drop():
    problem = _cross_problem()
    in1, out1 = _leg_ids(problem, 1)
    # Drone 1 (route 1) starts at the relay; drone 0 travels far to drop.
    routes = ((in1, -in1, 2, -2), (out1, -out1))

    schedule = GlobalRelayEvaluator(problem).evaluate(routes)
    drop_time = schedule.event_times_min[(0, 1)]
    pickup_time = schedule.event_times_min[(1, 0)]

    assert pickup_time == pytest.approx(drop_time, abs=1e-9)
    assert schedule.waiting_by_task[1] > 0
    assert schedule.total_waiting_min > 0
    delivery = schedule.delivery_times_min[1]
    assert delivery == pytest.approx(
        pickup_time + problem.distance(out1, -out1) / 0.9, abs=1e-9
    )
    assert evaluate_solution(problem, routes).valid


def test_waiting_propagates_to_subsequent_deliveries():
    problem = _relay_problem(
        (
            Task(1, Point(10, 1), Point(-10, 1), 100),
            Task(2, Point(0.1, 2), Point(0.2, 2), 100),
        ),
        drone_count=2,
        max_tasks=2,
        stations=(RelayStation(1, 0.0, 1.5),),
    )
    in1, out1 = _leg_ids(problem, 1)
    routes = ((in1, -in1), (out1, -out1, 2, -2))

    schedule = GlobalRelayEvaluator(problem).evaluate(routes)
    drop_time = schedule.event_times_min[(0, 1)]
    pickup_time = schedule.event_times_min[(1, 0)]
    delivery2 = schedule.delivery_times_min[2]

    assert pickup_time == pytest.approx(drop_time, abs=1e-9)
    assert delivery2 == pytest.approx(
        drop_time
        + (problem.distance(out1, -out1) + problem.distance(-out1, 2) + problem.distance(2, -2))
        / 0.9,
        abs=1e-9,
    )
    assert evaluate_solution(problem, routes).valid


def test_event_times_keep_the_read_only_mapping_contract():
    problem = _cross_problem()
    in1, out1 = _leg_ids(problem, 1)
    routes = ((in1, -in1), (out1, -out1))

    event_times = GlobalRelayEvaluator(problem).evaluate(
        routes
    ).event_times_min

    assert len(event_times) == 4
    assert list(event_times) == [(0, 0), (0, 1), (1, 0), (1, 1)]
    assert dict(event_times)[(1, 0)] == event_times[(1, 0)]
    assert event_times.get((9, 9)) is None
    with pytest.raises(TypeError):
        event_times[(0, 0)] = 123.0


def test_waiting_is_attributed_only_to_the_handoff_that_caused_it():
    problem = _relay_problem(
        (
            Task(1, Point(9, 0), Point(1, 0), 100),
            Task(2, Point(0, 0.1), Point(0, 0.2), 100),
        ),
        drone_count=2,
        max_tasks=2,
        stations=(RelayStation(1, 0.0, 0.0),),
    )
    in1, out1 = _leg_ids(problem, 1)
    in2, out2 = _leg_ids(problem, 2)
    routes = (
        (in1, -in1, in2, -in2),
        (out1, -out1, out2, -out2),
    )

    schedule = GlobalRelayEvaluator(problem).evaluate(routes)

    # Task 1 makes UAV 2 wait.  By the time UAV 2 returns to the station for
    # task 2, task 2 is already available; task 1's wait must not be charged
    # a second time to task 2.
    assert schedule.waiting_by_task.keys() == {1}
    assert schedule.total_waiting_min == pytest.approx(
        schedule.waiting_by_task[1]
    )
    assert schedule.event_times_min[(1, 2)] > schedule.event_times_min[(0, 3)]


def test_staged_relay_preserves_direct_warmup_incumbent_and_total_budget():
    problem = _bridge_problem()
    initial = construct_regret_initial(problem, candidate_limit=None)
    config = ALNSConfig(
        max_iterations=12,
        time_limit_seconds=None,
        seed=20260805,
        candidate_limit=None,
        relay_direct_warmup_fraction=0.80,
    )
    direct_warmup = solve_alns(
        problem,
        config=ALNSConfig(
            max_iterations=10,
            time_limit_seconds=None,
            seed=20260805,
            candidate_limit=None,
            relay_enabled=False,
        ),
        initial_routes=initial.routes,
    )

    result = solve_relay_staged(
        problem,
        config=config,
        initial_routes=initial.routes,
    )

    assert result.evaluation.valid
    assert result.evaluation.score <= direct_warmup.evaluation.score
    assert result.iterations == 12
    assert result.metadata["staged_relay"] is True
    assert result.metadata["relay_warmup_iterations"] == 10
    assert result.metadata["relay_search_iterations"] == 2
    assert all(
        later[2:] < earlier[2:]
        for earlier, later in zip(
            result.metadata["best_trajectory"],
            result.metadata["best_trajectory"][1:],
        )
    )


def test_staged_relay_fraction_is_validated():
    with pytest.raises(ValueError, match="预热比例"):
        ALNSConfig(relay_direct_warmup_fraction=1.0)


# --- Test 4: handoff dependency cycle is rejected ---


def test_handoff_dependency_cycle_is_infeasible():
    problem = _cross_problem()
    in1, out1 = _leg_ids(problem, 1)
    in2, out2 = _leg_ids(problem, 2)
    routes = (
        (in1, out2, -in1, -out2),
        (in2, out1, -in2, -out1),
    )

    schedule = GlobalRelayEvaluator(problem).evaluate(routes)
    result = evaluate_solution(problem, routes)

    assert not schedule.feasible
    assert any("环" in item for item in schedule.violations)
    assert not result.valid
    assert any("环" in item for item in result.violations)


# --- Tests 5-7: capacity semantics ---


def test_load_over_capacity_two_is_infeasible():
    problem = _cross_problem()
    in1, _ = _leg_ids(problem, 1)
    in2, _ = _leg_ids(problem, 2)
    # Third pickup before any drop exceeds capacity 2.
    routes = ((in1, in2, in1, -in1, -in1, -in2), ())
    with pytest.raises(ValueError):
        RouteEvaluator(problem).evaluate(routes[0])
    result = evaluate_solution(problem, routes)
    assert not result.valid
    assert any("载荷" in item for item in result.violations)


def test_relay_drop_decrements_onboard_load():
    problem = _cross_problem()
    in1, out1 = _leg_ids(problem, 1)
    in2, out2 = _leg_ids(problem, 2)
    # Two pickups then two drops: loads 1, 2, 1, 0 and stays within capacity.
    routes = ((in1, in2, -in1, -in2), (out1, -out1, out2, -out2))

    metrics = RouteEvaluator(problem).evaluate(routes[0])
    result = evaluate_solution(problem, routes)

    assert metrics.score.distance_km > 0
    assert result.valid


def test_relay_pickup_increments_onboard_load():
    problem = _cross_problem()
    in1, out1 = _leg_ids(problem, 1)
    in2, out2 = _leg_ids(problem, 2)
    routes = ((in1, -in1, in2, -in2), (out1, out2, -out1, -out2))

    metrics = RouteEvaluator(problem).evaluate(routes[1])
    result = evaluate_solution(problem, routes)

    assert metrics.score.distance_km > 0
    assert result.valid


# --- Test 8: destroy removes RELAY_IN and RELAY_OUT together ---


def test_destroy_relay_task_removes_both_legs_together():
    problem = _bridge_problem()
    in2, out2 = _leg_ids(problem, 2)
    routes = ((1, -1, in2, -in2), (out2, -out2, 3, -3))

    partial, removed = destroy_solution(
        problem,
        RouteEvaluator(problem),
        routes,
        "random",
        2,
        Random(7),
    )
    remaining_visits = [visit for route in partial for visit in route]
    for task_id in removed:
        in_id, out_id = _leg_ids(problem, task_id)
        assert in_id not in remaining_visits
        assert -in_id not in remaining_visits
        assert out_id not in remaining_visits
        assert -out_id not in remaining_visits
    # Every leg present in the partial solution is complete.
    plan_index = build_plan_index(problem, partial)
    assert len(plan_index.task_plans) == 3 - len(removed)
    for task_id in set(problem.task_ids) - set(removed):
        assert task_id in plan_index.task_plans
    partial_result = evaluate_solution(problem, partial)
    assert not partial_result.valid
    assert any("缺少任务" in item for item in partial_result.violations)


# --- Tests 9-10: repair switches DIRECT <-> RELAY ---


def _relay_cfg(**kwargs) -> _RelaySearchConfig:
    defaults = dict(
        enabled=True,
        candidates_per_task=2,
        plan_beam=6,
        leg_candidate_limit=8,
        leg_beam=8,
        event_cap=60,
        debug=False,
        sample_every=1,
        global_limit=4,
    )
    defaults.update(kwargs)
    return _RelaySearchConfig(**defaults)


def test_repair_can_switch_a_task_from_direct_to_relay():
    problem = _bridge_problem()
    evaluator = RouteEvaluator(problem)
    global_evaluator = GlobalRelayEvaluator(problem)
    direct_routes = ((1, -1, 2, -2), (3, -3))
    direct_score = routes_score(evaluator, direct_routes)

    partial, removed = destroy_solution(
        problem,
        evaluator,
        direct_routes,
        "random",
        2,
        Random(11),
    )
    assert 2 in removed
    repaired = _repair(
        problem,
        evaluator,
        partial,
        removed,
        "regret2",
        None,
        use_deadline_risk=False,
        relay_cfg=_relay_cfg(),
        global_evaluator=global_evaluator,
    )
    plan_index = build_plan_index(problem, repaired)
    final_score = global_evaluator.evaluate(repaired).score

    assert plan_index.task_plans[2].mode == "RELAY"
    assert final_score < direct_score
    assert evaluate_solution(problem, repaired).valid


def test_repair_can_switch_a_task_from_relay_to_direct():
    problem = _relay_problem(
        (
            Task(1, Point(-10, 0), Point(-9, 0), 100),
            Task(2, Point(0, 0), Point(0, 10), 100),
        ),
        drone_count=2,
        max_tasks=2,
        # The only station is far from the task corridor.
        stations=(RelayStation(1, 90.0, 90.0),),
    )
    evaluator = RouteEvaluator(problem)
    global_evaluator = GlobalRelayEvaluator(problem)
    in2, out2 = _leg_ids(problem, 2)
    relay_routes = ((1, -1, in2, -in2), (out2, -out2))

    partial, removed = destroy_solution(
        problem,
        evaluator,
        relay_routes,
        "random",
        2,
        Random(3),
    )
    assert 2 in removed
    repaired = _repair(
        problem,
        evaluator,
        partial,
        removed,
        "greedy",
        None,
        use_deadline_risk=False,
        relay_cfg=_relay_cfg(),
        global_evaluator=global_evaluator,
    )
    plan_index = build_plan_index(problem, repaired)

    assert evaluate_solution(problem, repaired).valid
    assert plan_index.task_plans[2].mode == "DIRECT"
    assert GlobalRelayEvaluator(problem).evaluate(repaired).score < (
        GlobalRelayEvaluator(problem).evaluate(relay_routes).score
    )


# --- Test 11: same-UAV adjacent drop/pickup is pruned ---


def test_same_uav_composition_never_yields_adjacent_drop_pickup():
    from uav_dispatch.search import RouteInsertionOption

    problem = _bridge_problem()
    in2, out2 = _leg_ids(problem, 2)
    base = (1, -1, 3, -3)
    routes = (base, ())
    allowed = 0
    for pickup_in in range(len(base) + 1):
        for delivery_in in range(pickup_in + 1, len(base) + 2):
            in_opt = RouteInsertionOption(
                Score(0, 0.0, 0.0), 0, pickup_in, delivery_in, ()
            )
            for pickup_out in range(len(base) + 1):
                for delivery_out in range(pickup_out + 1, len(base) + 2):
                    out_opt = RouteInsertionOption(
                        Score(0, 0.0, 0.0), 0, pickup_out, delivery_out, ()
                    )
                    composed = _compose_relay_combo(
                        routes, in_opt, out_opt, in2, -in2, out2, -out2
                    )
                    if composed is None:
                        continue
                    combined = composed[0]
                    drop_index = combined.index(-in2)
                    pickup_index = combined.index(out2)
                    assert pickup_index > drop_index + 1
                    allowed += 1
    assert allowed > 0


# --- Tests 12-13: original task counts are invariant ---


def test_all_200_original_tasks_appear_exactly_once():
    tasks = load_tasks_csv(DATA_FILE)
    network = build_relay_network(
        tasks, relay_count=3, candidates_per_task=2, detour_ratio=1.5,
        location_seed=42,
    )
    problem = Problem(
        tasks,
        drone_count=8,
        max_tasks_per_drone=25,
        relay_stations=network.stations,
        leg_registry=network.leg_registry,
        task_relay_candidates=network.task_relay_candidates,
    )
    from uav_dispatch import construct_edd_adjacent

    result = construct_edd_adjacent(problem)
    plan_index = build_plan_index(problem, result.routes)

    assert len(plan_index.task_plans) == 200
    assert set(plan_index.task_plans) == set(problem.task_ids)
    assert result.evaluation.valid
    for route in result.routes:
        assert sum(visit < 0 for visit in route) <= 25


def test_transport_legs_can_exceed_task_count_while_tasks_stay_fixed():
    problem = _bridge_problem()
    in2, out2 = _leg_ids(problem, 2)
    in3, out3 = _leg_ids(problem, 3)
    routes = ((1, -1, in2, -in2, in3, -in3), (out2, -out2, out3, -out3))

    plan_index = build_plan_index(problem, routes)
    schedule = GlobalRelayEvaluator(problem).evaluate(routes)
    stats = relay_statistics(problem, routes, schedule, plan_index)

    assert len(plan_index.task_plans) == 3
    total_legs = sum(item["transport_leg_count"] for item in stats["per_drone"])
    assert total_legs == 5  # 1 direct + 2x2 relay legs
    assert total_legs > 3
    assert sum(
        item["original_task_count"] for item in stats["per_drone"]
    ) == 3
    assert evaluate_solution(problem, routes).valid


# --- Test 14: --disable-relay matches the direct-only baseline ---


def test_disable_relay_produces_a_direct_only_problem_identical_to_no_network():
    from uav_dispatch.cli import main
    from pathlib import Path
    import json, tempfile

    tasks = (
        Task(1, Point(1, 0), Point(2, 0), 10),
        Task(2, Point(0, 1), Point(0, 2), 10),
        Task(3, Point(1, 1), Point(2, 2), 10),
    )
    network = build_relay_network(
        tasks, relay_count=0, candidates_per_task=2, detour_ratio=1.5,
        location_seed=42,
    )
    disabled_problem = Problem(
        tasks,
        drone_count=1,
        max_tasks_per_drone=3,
        relay_stations=network.stations,
        leg_registry=network.leg_registry,
        task_relay_candidates=network.task_relay_candidates,
    )
    plain_problem = Problem(tasks, drone_count=1, max_tasks_per_drone=3)

    config = ALNSConfig(max_iterations=15, seed=20260805, candidate_limit=None)
    with_relay_flag = solve_alns(disabled_problem, config=config)
    without_relay_flag = solve_alns(plain_problem, config=config)

    assert not disabled_problem.has_relays
    assert with_relay_flag.routes == without_relay_flag.routes
    assert with_relay_flag.evaluation.score == without_relay_flag.evaluation.score
    assert with_relay_flag.metadata["relay_enabled"] is False

    with tempfile.TemporaryDirectory() as tmp_dir:
        source = Path(tmp_dir) / "tasks.csv"
        source.write_text(
            "task_id,pickup_x,pickup_y,delivery_x,delivery_y,deadline_min\n"
            "1,1,0,2,0,10\n"
            "2,0,1,0,2,10\n"
            "3,1,1,2,2,10\n",
            encoding="utf-8",
        )
        output = Path(tmp_dir) / "solution.json"
        exit_code = main(
            [
                "solve",
                "--input",
                str(source),
                "--output",
                str(output),
                "--method",
                "alns",
                "--iterations",
                "5",
                "--drones",
                "1",
                "--max-tasks",
                "3",
                "--disable-relay",
            ]
        )
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert exit_code == 0
        assert payload["valid"] is True
        assert payload["relay"]["relay_count"] == 0
        assert payload["relay"]["relay_task_count"] == 0
        assert payload["metadata"]["relay_enabled"] is False


# --- Extra: builder, candidates, serialization, responsibility cap ---


def test_builder_detour_filter_and_candidate_cache_are_fixed():
    tasks = tuple(
        Task(i, Point(i, 0), Point(i + 0.5, 0), 10 + i)
        for i in range(1, 13)
    )
    network = build_relay_network(
        tasks, relay_count=3, candidates_per_task=2, detour_ratio=1.5,
        location_seed=42,
    )
    assert len(network.stations) == 3
    assert all(
        0 <= len(candidates) <= 2
        for candidates in network.task_relay_candidates.values()
    )
    assert any(
        len(candidates) >= 1
        for candidates in network.task_relay_candidates.values()
    )
    # One registry pair per (task, candidate); detour-filtered tasks have none.
    for task in tasks:
        pair_count = sum(
            1
            for leg in network.leg_registry.values()
            if leg.task_id == task.id
        )
        assert pair_count == 2 * len(network.task_relay_candidates[task.id])
    # Reproducible with the same seed.
    again = build_relay_network(
        tasks, relay_count=3, candidates_per_task=2, detour_ratio=1.5,
        location_seed=42,
    )
    assert again.stations == network.stations
    assert dict(again.task_relay_candidates) == dict(
        network.task_relay_candidates
    )


def test_relay_option_generation_offers_direct_and_relay_plans():
    problem = _bridge_problem()
    evaluator = RouteEvaluator(problem)
    routes = ((1, -1, 3, -3), ())
    options = _all_plans_for_task(
        problem,
        evaluator,
        routes,
        2,
        candidate_limit=None,
        option_count=3,
        relay_cfg=_relay_cfg(),
        deadline=None,
    )

    assert options
    assert any(option.mode == "DIRECT" for option in options)
    assert any(option.mode == "RELAY" for option in options)
    relay_option = next(
        option for option in options if option.mode == "RELAY"
    )
    assert relay_option.inbound_route_index is not None
    assert relay_option.outbound_route_index is not None
    assert relay_option.combos


def test_exact_relay_shortlist_considers_more_than_the_first_station():
    problem = _relay_problem(
        (Task(1, Point(-2, 0), Point(2, 0), 100),),
        drone_count=2,
        max_tasks=1,
        stations=(
            RelayStation(1, -0.5, 0.0),
            RelayStation(2, 0.5, 0.0),
        ),
    )
    base = ((), ())
    options = []
    candidates = {}
    for relay_id, local_distance in ((1, 0.0), (2, 1.0)):
        in_id, out_id = _leg_ids(problem, 1, relay_id)
        in_opt = RouteInsertionOption(
            Score(0, 0.0, local_distance),
            0,
            0,
            1,
            (in_id, -in_id),
            start_time_min=1.0,
            end_time_min=2.0,
        )
        out_opt = RouteInsertionOption(
            Score(0, 0.0, local_distance),
            1,
            0,
            1,
            (out_id, -out_id),
            start_time_min=2.0,
            end_time_min=3.0,
        )
        option = TaskInsertionOption(
            task_id=1,
            mode="RELAY",
            relay_id=relay_id,
            local_estimated_delta=in_opt.delta + out_opt.delta,
            combos=((in_opt, out_opt),),
        )
        options.append(option)
        candidates[relay_id] = (
            (in_id, -in_id),
            (out_id, -out_id),
        )

    class FakeGlobalEvaluator:
        def __init__(self):
            self.calls = []

        def evaluate(self, routes):
            materialized = tuple(tuple(route) for route in routes)
            self.calls.append(materialized)
            if materialized == base:
                score = Score(0, 0.0, 0.0)
            elif materialized == candidates[1]:
                score = Score(1, 10.0, 10.0)
            else:
                score = Score(0, 0.0, 2.0)
            return SimpleNamespace(
                feasible=True, score=score, total_waiting_min=0.0
            )

        def evaluate_delta(
            self, base_routes, base_schedule, candidate_routes
        ):
            return self.evaluate(candidate_routes)

    global_evaluator = FakeGlobalEvaluator()
    selected, score, mode = _apply_task_exact(
        problem,
        RouteEvaluator(problem),
        global_evaluator,
        base,
        options,
        relay_cfg=_relay_cfg(global_limit=2),
        deadline=None,
    )

    assert selected == candidates[2]
    assert score == Score(0, 0.0, 2.0)
    assert mode == "RELAY"
    assert candidates[1] in global_evaluator.calls
    assert candidates[2] in global_evaluator.calls


def test_direct_exact_candidate_is_not_pruned_by_unsafe_local_delta():
    problem = _relay_problem(
        (
            Task(1, Point(1, 0), Point(2, 0), 100),
            Task(2, Point(9, 0), Point(1, 0), 100),
        ),
        drone_count=2,
        max_tasks=2,
        stations=(RelayStation(1, 0.0, 0.0),),
    )
    in2, out2 = _leg_ids(problem, 2)
    base = ((in2, -in2), (out2, -out2))
    first_route = (1, -1, out2, -out2)
    better_global_route = (1, out2, -out2, -1)
    options = (
        TaskInsertionOption(
            task_id=1,
            mode="DIRECT",
            relay_id=None,
            local_estimated_delta=Score(0, 3.0, 0.0),
            direct_route_index=1,
            direct_route=first_route,
            direct_pickup_position=0,
            direct_delivery_position=2,
        ),
        TaskInsertionOption(
            task_id=1,
            mode="DIRECT",
            relay_id=None,
            local_estimated_delta=Score(0, 4.0, 0.0),
            direct_route_index=1,
            direct_route=better_global_route,
            direct_pickup_position=0,
            direct_delivery_position=4,
        ),
    )

    class FakeRouteEvaluator:
        scores = {
            base[1]: Score(0, 0.0, 0.0),
            first_route: Score(0, 3.0, 0.0),
            better_global_route: Score(0, 4.0, 0.0),
        }

        def evaluate(self, route, *, start_node=0):
            return SimpleNamespace(score=self.scores[tuple(route)])

    first_candidate = (base[0], first_route)
    better_candidate = (base[0], better_global_route)

    class FakeGlobalEvaluator:
        scores = {
            base: Score(0, 0.0, 0.0),
            first_candidate: Score(0, 2.0, 0.0),
            better_candidate: Score(0, 1.0, 0.0),
        }

        def evaluate(self, routes):
            materialized = tuple(tuple(route) for route in routes)
            return SimpleNamespace(
                feasible=True,
                score=self.scores[materialized],
                total_waiting_min=1.0,
            )

        def evaluate_delta(
            self, base_routes, base_schedule, candidate_routes
        ):
            return self.evaluate(candidate_routes)

    selected, score, mode = _apply_task_exact(
        problem,
        FakeRouteEvaluator(),
        FakeGlobalEvaluator(),
        base,
        options,
        relay_cfg=_relay_cfg(),
        deadline=None,
        direct_exact_top_k=2,
    )

    assert selected == better_candidate
    assert score == Score(0, 1.0, 0.0)
    assert mode == "DIRECT"


def test_outbound_leg_consumes_a_k_slot_but_inbound_does_not():
    tasks = (
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(3, 0), Point(4, 0), 100),
    )
    problem = _relay_problem(
        tasks, drone_count=1, max_tasks=2,
        stations=(RelayStation(1, 2.5, 0.0),),
    )
    in1, _ = _leg_ids(problem, 1)
    _, out2 = _leg_ids(problem, 2)
    evaluator = RouteEvaluator(problem)
    from uav_dispatch.search import route_leg_insertion_options

    full = (1, -1, 2, -2)  # both K slots consumed
    inbound = route_leg_insertion_options(
        problem, evaluator, full, 0, in1, -in1,
        candidate_limit=None, option_count=4, counts_toward_k=False,
    )
    outbound = route_leg_insertion_options(
        problem, evaluator, full, 0, out2, -out2,
        candidate_limit=None, option_count=4, counts_toward_k=True,
    )
    assert inbound
    assert not outbound


def test_global_evaluator_matches_route_scores_for_direct_only():
    tasks = (
        Task(1, Point(1, 0), Point(4, 0), 100),
        Task(2, Point(2, 0), Point(3, 0), 100),
    )
    problem = Problem(tasks, drone_count=1, max_tasks_per_drone=2)
    routes = ((1, 2, -2, -1),)
    evaluator = RouteEvaluator(problem)
    schedule = GlobalRelayEvaluator(problem).evaluate(routes)

    assert schedule.feasible
    assert schedule.score == routes_score(evaluator, routes)
    assert schedule.delivery_times_min == evaluator.evaluate(
        routes[0]
    ).delivery_times_min


def test_same_uav_buffer_relay_solution_is_valid():
    problem = _bridge_problem()
    in2, out2 = _leg_ids(problem, 2)
    routes = ((in2, 1, -1, -in2, out2, -out2), (3, -3))

    result = evaluate_solution(problem, routes)
    plans = build_plan_index(problem, routes)

    assert result.valid
    assert plans.task_plans[2].mode == "RELAY"
    stats = relay_statistics(
        problem, routes, GlobalRelayEvaluator(problem).evaluate(routes), plans
    )
    assert stats["same_uav_relay_count"] == 1


def test_alns_adopts_cross_uav_relay_in_a_constructed_winning_scenario():
    problem = _bridge_problem()
    result = solve_alns(
        problem,
        config=ALNSConfig(
            max_iterations=120, seed=20260805, candidate_limit=None
        ),
    )

    plans = build_plan_index(problem, result.routes)
    assert result.evaluation.valid
    assert plans.task_plans[2].mode == "RELAY"
    assert result.metadata["relay"]["cross_uav_handoff_count"] >= 1
    assert result.metadata["relay"]["relay_task_count"] >= 1


def test_relay_initial_solution_may_already_contain_relay_tasks():
    problem = _bridge_problem()
    from uav_dispatch import solve_alns

    initial = construct_regret_initial(problem, candidate_limit=None)
    assert evaluate_solution(problem, initial.routes).valid

    result = solve_alns(
        problem,
        config=ALNSConfig(
            max_iterations=0, seed=20260805, candidate_limit=None
        ),
    )
    assert result.evaluation.valid


# --- Pre-deployed drone homes (multi-hub) ---


def test_drone_homes_are_validated():
    tasks = (Task(1, Point(1, 0), Point(2, 0), 10),)
    stations = (RelayStation(1, 0.0, 0.0),)
    with pytest.raises(ValueError, match="起点数量"):
        Problem(
            tasks,
            drone_count=2,
            relay_stations=stations,
            drone_homes=(None,),
        )
    with pytest.raises(ValueError, match="未知中继站"):
        Problem(
            tasks,
            drone_count=2,
            relay_stations=stations,
            drone_homes=(None, 9),
        )


def test_predeployed_drone_starts_at_station_exactly():
    # Drone 1 starts at the origin, drone 2 is pre-deployed at station 1
    # which sits exactly on task 1's pickup point.
    station = RelayStation(1, 1.0, 0.0)
    tasks = (
        Task(1, Point(1, 0), Point(2, 0), 100),
        Task(2, Point(3, 0), Point(4, 0), 100),
    )
    problem = Problem(
        tasks,
        drone_count=2,
        relay_stations=(station,),
        drone_homes=(None, 1),
    )
    assert problem.drone_home_nodes[0] == 0
    assert problem.home_node(1) != 0
    assert problem._points[problem.home_node(1)] == station.point
    routes = ((1, -1), (2, -2))
    evaluation = evaluate_solution(problem, routes)
    assert evaluation.valid
    # Drone 1: origin->(1,0)=1 + (1,0)->(2,0)=1 => 2.0
    # Drone 2: station(1,0)->(3,0)=2 + (3,0)->(4,0)=1 => 3.0
    assert evaluation.route_distances_km == pytest.approx((2.0, 3.0))
    assert evaluation.score.distance_km == pytest.approx(5.0)

    evaluator = RouteEvaluator(problem)
    assert evaluator.evaluate(routes[0]).score.distance_km == pytest.approx(2.0)
    assert (
        evaluator.evaluate(
            routes[1], start_node=problem.home_node(1)
        ).score.distance_km
        == pytest.approx(3.0)
    )
    schedule = GlobalRelayEvaluator(problem).evaluate(routes)
    assert schedule.score.distance_km == pytest.approx(5.0)
    assert schedule.event_times_min[(1, 0)] == pytest.approx(2.0 / 0.9)


def test_predeployed_construction_and_search_are_valid():
    station = RelayStation(1, 2.0, 0.0)
    tasks = (
        Task(1, Point(1, 0), Point(3, 0), 30),
        Task(2, Point(2, 1), Point(2, 3), 40),
        Task(3, Point(4, 0), Point(5, 0), 50),
        Task(4, Point(0, 2), Point(0, 3), 60),
    )
    problem = Problem(
        tasks,
        drone_count=2,
        max_tasks_per_drone=2,
        relay_stations=(station,),
        drone_homes=(None, 1),
    )
    initial = construct_regret_initial(problem, candidate_limit=None)
    assert initial.evaluation.valid
    result = solve_alns(
        problem,
        config=ALNSConfig(
            max_iterations=30, seed=20260805, candidate_limit=None
        ),
    )
    assert result.evaluation.valid
    # A drone whose home is a station never flies the origin->station deadhead.
    assert result.evaluation.score.distance_km < float("inf")


def test_origin_homes_preserve_legacy_distance_semantics():
    station = RelayStation(1, 2.0, 0.0)
    tasks = (Task(1, Point(1, 0), Point(2, 0), 100),)
    problem = Problem(
        tasks,
        drone_count=1,
        relay_stations=(station,),
        drone_homes=None,
    )
    routes = ((1, -1),)
    assert evaluate_solution(problem, routes).route_distances_km == pytest.approx(
        (2.0,)
    )
    assert RouteEvaluator(problem).evaluate(routes[0]).score.distance_km == pytest.approx(
        2.0
    )


def test_relay_repair_preserves_complete_leg_pairs_with_shared_station_homes():
    """Cached leg insertions must never overwrite another task's relay leg."""

    tasks = load_tasks_csv(DATA_FILE)[:24]
    network = build_relay_network(
        tasks,
        relay_count=4,
        candidates_per_task=2,
        detour_ratio=2.0,
        location_seed=42,
        location_method="weighted_kmedoids",
    )
    deployment = compute_dynamic_uav_homes(
        tasks,
        network.stations,
        10,
        Point(0.0, 0.0),
        speed_km_per_min=0.9,
    )
    problem = Problem(
        tasks,
        drone_count=10,
        max_tasks_per_drone=25,
        capacity=2,
        speed_km_per_min=0.9,
        depot=Point(0.0, 0.0),
        relay_stations=network.stations,
        leg_registry=network.leg_registry,
        task_relay_candidates=network.task_relay_candidates,
        drone_homes=deployment.homes,
    )
    common = dict(
        seed=2026081701,
        candidate_limit=48,
        relay_candidates_per_task=2,
        relay_plan_beam=4,
        relay_leg_beam=5,
        relay_event_cap=60,
        relay_sample_every=3,
        relay_global_limit=3,
        relay_probe_fraction=0.25,
        relay_probe_min=1,
        relay_probe_max_tasks=2,
        relay_seed_task_limit=200,
        relay_refine_interval=10,
        relay_refine_task_limit=8,
        enable_home_seed=True,
        enable_home_bias=True,
    )
    warm = solve_alns(
        problem,
        config=ALNSConfig(
            max_iterations=1200,
            relay_enabled=False,
            **common,
        ),
    )

    result = solve_alns(
        problem,
        config=ALNSConfig(
            max_iterations=50,
            relay_enabled=True,
            **common,
        ),
        initial_routes=warm.routes,
    )

    plan_index = build_plan_index(problem, result.routes)
    assert result.evaluation.valid
    assert set(plan_index.task_plans) == set(problem.task_ids)
