from types import SimpleNamespace

import pytest

from algorithm.experiments.run_benchmarks import _best_within_budget
from uav_dispatch import Score


def _result(runtime_seconds, score):
    return SimpleNamespace(
        runtime_seconds=runtime_seconds,
        evaluation=SimpleNamespace(score=Score(*score)),
    )


def test_best_within_budget_excludes_a_better_but_late_result():
    compliant = _result(239.0, (3, 10.0, 20.0))
    late = _result(241.0, (2, 5.0, 10.0))

    assert _best_within_budget([late, compliant], 240.0) is compliant


def test_best_within_budget_fails_when_no_run_is_compliant():
    with pytest.raises(RuntimeError, match="时间预算"):
        _best_within_budget([_result(241.0, (2, 5.0, 10.0))], 240.0)
