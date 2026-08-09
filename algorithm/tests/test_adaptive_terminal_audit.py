import uav_dispatch.alns as alns_module
from uav_dispatch import ALNSConfig, Point, Problem, Task
from uav_dispatch.alns import _terminal_rejected_tasks


def test_terminal_rejected_tasks_are_derived_from_route_coverage() -> None:
    problem = Problem(
        (
            Task(1, Point(1, 0), Point(2, 0), deadline_min=5),
            Task(2, Point(0, 1), Point(0, 2), deadline_min=5),
        ),
        drone_count=2,
    )

    assert _terminal_rejected_tasks(problem, ((1, -1), ())) == (2,)
    assert _terminal_rejected_tasks(problem, ((1, -1), (2, -2))) == ()


def test_a2_solver_disables_internal_search_score_hot_path(monkeypatch) -> None:
    problem = Problem(
        (Task(1, Point(1, 0), Point(2, 0), deadline_min=5),),
        drone_count=1,
    )
    real_evaluator = alns_module.RouteEvaluator
    enabled_values: list[bool] = []

    def recording_evaluator(*args, **kwargs):
        enabled_values.append(bool(kwargs.get("enable_search_score", True)))
        return real_evaluator(*args, **kwargs)

    monkeypatch.setattr(alns_module, "RouteEvaluator", recording_evaluator)
    alns_module.solve_alns_core(
        problem,
        config=ALNSConfig(max_iterations=0),
        initial_routes=((1, -1),),
    )

    assert enabled_values == [False]
