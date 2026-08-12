from random import Random

from uav_dispatch import ALNSConfig, Point, Problem, Task, solve_alns
from uav_dispatch.hypergraph_destroy import hypergraph_destroy
from uav_dispatch.search import RouteEvaluator


def _conflict_problem() -> Problem:
    return Problem(
        (
            Task(1, Point(1, 0), Point(2, 0), deadline_min=2),
            Task(2, Point(-1, 0), Point(-2, 0), deadline_min=2),
            Task(3, Point(0, 0.1), Point(0, 0.2), deadline_min=100),
        ),
        drone_count=1,
        max_tasks_per_drone=3,
        capacity=2,
        speed_km_per_min=1,
    )


def test_hypergraph_destroy_removes_the_strongest_conflict_as_a_complete_pair():
    problem = _conflict_problem()
    routes = ((1, -1, 2, -2, 3, -3),)

    partial, removed = hypergraph_destroy(
        problem,
        RouteEvaluator(problem),
        routes,
        count=2,
        rng=Random(20260805),
    )

    assert set(removed) == {1, 2}
    assert partial == ((3, -3),)
    RouteEvaluator(problem).evaluate(partial[0])


def test_hypergraph_destroy_honors_an_odd_exact_removal_count():
    problem = _conflict_problem()
    routes = ((1, 2, -1, -2, 3, -3),)

    partial, removed = hypergraph_destroy(
        problem,
        RouteEvaluator(problem),
        routes,
        count=3,
        rng=Random(7),
    )

    assert len(removed) == len(set(removed)) == 3
    assert partial == ((),)


def test_hypergraph_destroy_is_registered_only_when_explicitly_enabled():
    problem = _conflict_problem()
    disabled = solve_alns(
        problem,
        config=ALNSConfig(max_iterations=0, enable_hypergraph_destroy=False),
    )
    enabled = solve_alns(
        problem,
        config=ALNSConfig(max_iterations=0, enable_hypergraph_destroy=True),
    )

    disabled_destroys = {
        name
        for name in disabled.metadata["operator_uses"]
        if name.startswith("destroy:")
    }
    enabled_destroys = {
        name
        for name in enabled.metadata["operator_uses"]
        if name.startswith("destroy:")
    }
    assert enabled_destroys - disabled_destroys == {
        "destroy:hypergraph_destroy"
    }
    assert disabled_destroys < enabled_destroys
    assert disabled.metadata["enable_hypergraph_destroy"] is False
    assert enabled.metadata["enable_hypergraph_destroy"] is True
