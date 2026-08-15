import pytest

from algorithm.experiments.run_relay_comparison import (
    _solver_source_sha256,
    paired_lexicographic_comparison,
)


def _row(variant, *, late, initial="same"):
    return {
        "variant": variant,
        "seed": 7,
        "late_count": late,
        "total_lateness_min": 10.0,
        "distance_km": 20.0,
        "initial_routes_sha256": initial,
    }


def test_relay_comparison_is_lexicographic_and_audits_shared_initial_routes():
    result = paired_lexicographic_comparison(
        [_row("direct", late=2), _row("relay", late=1)]
    )

    assert result["relay_win"] == 1
    assert result["pairs"][0]["shared_initial_routes"] is True
    assert len(_solver_source_sha256()) == 64


def test_relay_comparison_rejects_different_initial_routes():
    with pytest.raises(RuntimeError, match="初始路线不一致"):
        paired_lexicographic_comparison(
            [
                _row("direct", late=1, initial="direct"),
                _row("relay", late=1, initial="relay"),
            ]
        )
