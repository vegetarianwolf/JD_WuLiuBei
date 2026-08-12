"""Tests for dynamic relay station manager (Phase 4)."""

from __future__ import annotations

import pytest

from uav_dispatch.dynamic_relay import (
    DynamicRelayConfig,
    DynamicRelayManager,
    candidate_sites,
    score_station,
)
from uav_dispatch.model import Point, Problem, Task
from uav_dispatch.relay_model import RelayStation
from uav_dispatch.search import RouteEvaluator


def _problem_4tasks() -> Problem:
    return Problem(
        tasks=(
            Task(1, Point(0, 0), Point(10, 0), 30),
            Task(2, Point(0, 5), Point(10, 5), 100),
            Task(3, Point(0, 10), Point(10, 10), 100),
            Task(4, Point(0, 15), Point(10, 15), 100),
        ),
        drone_count=2,
        max_tasks_per_drone=2,
        capacity=2,
    )


class TestCandidateSites:
    def test_all_p_d_nodes(self):
        problem = _problem_4tasks()
        sites = candidate_sites(problem)
        # 4 tasks × 2 nodes (P, D) = 8 sites, all unique coords
        assert len(sites) == 8

    def test_deduplication(self):
        # Two tasks with same pickup location
        problem = Problem(
            tasks=(
                Task(1, Point(0, 0), Point(1, 0), 100),
                Task(2, Point(0, 0), Point(2, 0), 100),
            ),
            drone_count=1,
            max_tasks_per_drone=2,
        )
        sites = candidate_sites(problem)
        # 2 tasks: P1=P2 (dedup), D1, D2 → 3 unique sites
        assert len(sites) == 3


class TestDynamicRelayManager:
    def test_initialize(self):
        problem = _problem_4tasks()
        evaluator = RouteEvaluator(problem)
        routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        mgr = DynamicRelayManager()
        mgr.initialize(problem, evaluator, routes)
        assert len(mgr.active_stations) >= mgr.config.min_active_stations
        assert len(mgr.active_stations) <= mgr.config.max_active_stations
        assert len(mgr.candidate_pool) <= mgr.config.station_candidate_pool

    def test_refresh_no_change_when_fresh(self):
        problem = _problem_4tasks()
        evaluator = RouteEvaluator(problem)
        routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        mgr = DynamicRelayManager()
        mgr.initialize(problem, evaluator, routes)
        # First refresh (iteration 1) should not trigger (interval=20)
        changed = mgr.refresh_if_needed(problem, evaluator, routes)
        assert not changed

    def test_force_refresh(self):
        problem = _problem_4tasks()
        evaluator = RouteEvaluator(problem)
        routes = ((1, -1, 2, -2), (3, -3, 4, -4))
        mgr = DynamicRelayManager()
        mgr.initialize(problem, evaluator, routes)
        changed = mgr.refresh_if_needed(problem, evaluator, routes, force=True)
        # May or may not change depending on scores, but shouldn't crash
        assert isinstance(changed, bool)

    def test_add_station(self):
        mgr = DynamicRelayManager()
        station = RelayStation(id=99, point=Point(5, 5), source_visit=1)
        assert mgr.try_add_station(None, station)  # problem not needed for add
        assert len(mgr.active_stations) == 1

    def test_add_duplicate_rejected(self):
        mgr = DynamicRelayManager()
        station = RelayStation(id=99, point=Point(5, 5), source_visit=1)
        mgr.try_add_station(None, station)
        assert not mgr.try_add_station(None, station)

    def test_add_respects_max(self):
        mgr = DynamicRelayManager(
            config=DynamicRelayConfig(min_active_stations=1, max_active_stations=2)
        )
        mgr.try_add_station(None, RelayStation(0, Point(0, 0), 1))
        mgr.try_add_station(None, RelayStation(1, Point(1, 1), 2))
        assert not mgr.try_add_station(None, RelayStation(2, Point(2, 2), 3))

    def test_drop_unused_station(self):
        mgr = DynamicRelayManager(
            config=DynamicRelayConfig(min_active_stations=1, max_active_stations=4)
        )
        s1 = RelayStation(0, Point(0, 0), 1)
        s2 = RelayStation(1, Point(1, 1), 2)
        mgr.try_add_station(None, s1)
        mgr.try_add_station(None, s2)
        assert mgr.try_drop_station(s1.id)
        assert len(mgr.active_stations) == 1

    def test_drop_pinned_rejected(self):
        mgr = DynamicRelayManager(
            config=DynamicRelayConfig(min_active_stations=1)
        )
        s1 = RelayStation(0, Point(0, 0), 1)
        s2 = RelayStation(1, Point(1, 1), 2)
        mgr.try_add_station(None, s1)
        mgr.try_add_station(None, s2)
        mgr.record_usage(s1.id)
        assert not mgr.try_drop_station(s1.id)  # pinned
        assert mgr.try_drop_station(s2.id)        # not pinned

    def test_drop_respects_min(self):
        mgr = DynamicRelayManager(
            config=DynamicRelayConfig(min_active_stations=1, max_active_stations=3)
        )
        s1 = RelayStation(0, Point(0, 0), 1)
        mgr.try_add_station(None, s1)
        assert not mgr.try_drop_station(s1.id)  # at min

    def test_replace_station(self):
        mgr = DynamicRelayManager()
        s_old = RelayStation(0, Point(0, 0), 1)
        s_new = RelayStation(1, Point(5, 5), 2)
        mgr.try_add_station(None, s_old)
        assert mgr.try_replace_station(s_old.id, s_new)
        assert mgr.active_stations[0].id == s_new.id

    def test_replace_pinned_rejected(self):
        mgr = DynamicRelayManager()
        s_old = RelayStation(0, Point(0, 0), 1)
        s_new = RelayStation(1, Point(5, 5), 2)
        mgr.try_add_station(None, s_old)
        mgr.record_usage(s_old.id)
        assert not mgr.try_replace_station(s_old.id, s_new)
