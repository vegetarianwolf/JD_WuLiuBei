"""Dynamic relay-point pool and criticality-guided relay candidates.

Two layers live in this module:

1. **Formal Cooperative-HALNS layer** — :class:`DynamicRelayPointPool`,
   :class:`RelayTrigger`, :class:`RelayCandidate`,
   :func:`score_relay_point_contextual` and
   :func:`estimate_blocker_release_gain`.  This is what the formal
   ``solve_cooperative_halns()`` uses: the relay point is chosen *after* the
   (task, donor, receiver) triple is fixed, by ranking the ``P ∪ D``
   candidate catalog contextually.  The candidate pool is a search-level
   structure and never becomes solution state — ``active_stations`` on the
   solution only lists the points actually used by ``RelayTransfer`` objects.

2. **Legacy layer** — :class:`DynamicRelayManager` (and its config / station
   scoring) is kept unchanged for the old standalone dynamic-station module
   and its tests.  The formal Cooperative-HALNS no longer uses
   ``min/max_active_stations``, forced rotation or drop patience.

Relay trigger semantics
-----------------------
Both triggers are only *candidate guidance* — they are not new service modes:

- ``DELIVERY_RISK``   — the task's own delivery is late / near-late.
- ``BLOCKER_RELEASE`` — relaying this task frees its donor to reach a
  critical downstream pickup earlier.

The final decision always ranks by the formal ``(late_count, distance_km)``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Sequence

from .model import Problem, Point
from .physical_lower_bound import pickup_slack
from .relay_candidates import best_station_insert_detour_km
from .relay_model import RELAY_ENDPOINT_EPS, RelayStation
from .search import RouteEvaluator, Routes


class RelayTrigger(Enum):
    """Guidance that produced a relay candidate (not a service mode)."""

    DELIVERY_RISK = auto()
    BLOCKER_RELEASE = auto()


@dataclass(frozen=True, slots=True)
class RelayCandidate:
    """One relay candidate before exact DAG evaluation.

    Physical relay mechanism is always the same::

        donor:   P_j -> h
        receiver: h -> D_j

    ``triggers`` records which guidance found it.  Estimated gains are cheap
    guidance only; the exact decision is made by the formal
    ``(late_count, distance_km)`` order.
    """

    task_id: int
    donor: int
    receiver: int
    relay_point_id: int
    triggers: frozenset[RelayTrigger] = frozenset()
    #: Critical pickup this candidate is meant to advance (blocker release).
    critical_task_id: int | None = None
    estimated_delivery_gain_min: float = 0.0
    estimated_pickup_gain_min: float = 0.0
    estimated_distance_delta: float = 0.0

    @property
    def dedup_key(self) -> tuple[int, int, int, int]:
        return (self.task_id, self.donor, self.receiver, self.relay_point_id)

    @property
    def is_delivery_risk(self) -> bool:
        return RelayTrigger.DELIVERY_RISK in self.triggers

    @property
    def is_blocker_release(self) -> bool:
        return RelayTrigger.BLOCKER_RELEASE in self.triggers

    def merged_with(self, other: "RelayCandidate") -> "RelayCandidate":
        """Merge two candidates with the same dedup key."""
        return RelayCandidate(
            task_id=self.task_id,
            donor=self.donor,
            receiver=self.receiver,
            relay_point_id=self.relay_point_id,
            triggers=self.triggers | other.triggers,
            critical_task_id=self.critical_task_id or other.critical_task_id,
            estimated_delivery_gain_min=max(
                self.estimated_delivery_gain_min,
                other.estimated_delivery_gain_min,
            ),
            estimated_pickup_gain_min=max(
                self.estimated_pickup_gain_min,
                other.estimated_pickup_gain_min,
            ),
            estimated_distance_delta=min(
                self.estimated_distance_delta,
                other.estimated_distance_delta,
            ),
        )


# ---------------------------------------------------------------------------
# Contextual relay-point ranking (formal layer)
# ---------------------------------------------------------------------------


def score_relay_point_contextual(
    problem: Problem,
    routes: Routes,
    task_id: int,
    donor: int,
    receiver: int,
    station: RelayStation,
) -> float:
    """Cheap contextual score of relay point *station* for
    ``(task_id, donor, receiver)`` — lower is better.

    Combines donor->h detour, receiver->h accessibility, h->D_i leg,
    P_i->h leg and the receiver's free capacity window.  Used only to
    shortlist candidate points (never a fixed global station score).
    """
    capacity = problem.capacity
    task = problem.task(task_id)
    h = station.point
    donor_route = routes[donor] if 0 <= donor < len(routes) else ()
    receiver_route = routes[receiver] if 0 <= receiver < len(routes) else ()
    donor_detour = best_station_insert_detour_km(
        problem, donor_route, station.source_visit
    )
    receiver_detour = best_station_insert_detour_km(
        problem, receiver_route, station.source_visit
    )
    h_to_delivery = h.distance_to(task.delivery)
    pickup_to_h = task.pickup.distance_to(h)
    # Receiver free-window quality: more free capacity is better (subtract).
    from .relay_candidates import _loads_before, _low_load_intervals, _receiver_window_quality

    window = _receiver_window_quality(receiver_route, capacity)
    return (
        donor_detour
        + receiver_detour
        + h_to_delivery
        + pickup_to_h
        - 0.5 * min(window, capacity * 2)
    )


def estimate_blocker_release_gain(
    problem: Problem,
    routes: Routes,
    donor: int,
    blocker_task_id: int,
    critical_task_id: int,
    relay_point: RelayStation,
) -> float:
    """Cheap estimate of how many minutes earlier the donor reaches the
    critical pickup ``critical_task_id`` if it hands blocker
    ``blocker_task_id`` over at ``relay_point``.

    Current path: ``P_j -> D_j -> P_i``; after relay: ``P_j -> h -> P_i``.
    Guidance only — the exact result must go through full relay DAG
    validation.
    """
    speed = problem.speed_km_per_min
    t_j = problem.task(blocker_task_id)
    t_i = problem.task(critical_task_id)
    h = relay_point.point
    current = (
        t_j.pickup.distance_to(t_j.delivery)
        + t_j.delivery.distance_to(t_i.pickup)
    ) / speed
    after = (t_j.pickup.distance_to(h) + h.distance_to(t_i.pickup)) / speed
    return max(0.0, current - after)


# ---------------------------------------------------------------------------
# Candidate catalog (P ∪ D)
# ---------------------------------------------------------------------------


def candidate_sites(problem: Problem) -> list[RelayStation]:
    """All safe relay-station sites (P ∪ D nodes, de-duplicated by coords).

    Identical to :func:`~relay_location.candidate_sites`.
    """
    seen: set[tuple[float, float]] = set()
    sites: list[RelayStation] = []
    sid = 0
    for task in problem.tasks:
        for visit, pt in [(task.id, task.pickup), (-task.id, task.delivery)]:
            key = (round(pt.x, 6), round(pt.y, 6))
            if key in seen:
                continue
            seen.add(key)
            sites.append(RelayStation(id=sid, point=pt, source_visit=visit))
            sid += 1
    return sites


class DynamicRelayPointPool:
    """Search-level catalog of relay points (P ∪ D) with contextual ranking.

    Not part of the solution state: it only (1) constructs the candidate
    catalog, (2) ranks points contextually for a given ``(task, donor,
    receiver)`` triple, (3) keeps a small shortlist / cache, and (4) reports
    point-usage statistics.  No min/max active count, no forced rotation.
    """

    def __init__(self, problem: Problem):
        self.problem = problem
        self._sites: list[RelayStation] = candidate_sites(problem)
        self._by_id: dict[int, RelayStation] = {
            station.id: station for station in self._sites
        }

    @property
    def all_sites(self) -> tuple[RelayStation, ...]:
        return tuple(self._sites)

    @property
    def site_count(self) -> int:
        return len(self._sites)

    def station_for_id(self, station_id: int) -> RelayStation | None:
        return self._by_id.get(station_id)

    def shortlist_for(
        self,
        routes: Routes,
        task_id: int,
        donor: int,
        receiver: int,
        top_k: int,
        exclude_ids: Sequence[int] = (),
    ) -> list[RelayStation]:
        """Top-K relay points for ``(task_id, donor, receiver)`` by contextual
        score.  Endpoint degeneracy (``h == P_i`` or ``h == D_i``) is
        rejected up front.
        """
        return [
            station
            for _score, station in self.shortlist_for_scored(
                routes, task_id, donor, receiver, top_k, exclude_ids
            )
        ]

    def shortlist_for_scored(
        self,
        routes: Routes,
        task_id: int,
        donor: int,
        receiver: int,
        top_k: int,
        exclude_ids: Sequence[int] = (),
        prefilter: callable = None,
    ) -> list[tuple[float, RelayStation]]:
        """Top-K relay points with their contextual scores (ascending).

        ``prefilter`` is an optional cheap geometric predicate that prunes
        hopeless sites *before* the expensive contextual scoring (it is only
        a speed optimisation — never a correctness filter).
        """
        task = self.problem.task(task_id)
        exclude = set(exclude_ids)
        scored: list[tuple[float, RelayStation]] = []
        for station in self._sites:
            if station.id in exclude:
                continue
            if station.point.distance_to(task.pickup) <= RELAY_ENDPOINT_EPS:
                continue
            if station.point.distance_to(task.delivery) <= RELAY_ENDPOINT_EPS:
                continue
            if prefilter is not None and not prefilter(station):
                continue
            score = score_relay_point_contextual(
                self.problem, routes, task_id, donor, receiver, station
            )
            scored.append((score, station))
        scored.sort(key=lambda item: item[0])
        return scored[:top_k]

    def record_usage(self, relays: Sequence[object]) -> dict[int, int]:
        """Usage statistics: how many active relays reference each point."""
        usage: dict[int, int] = defaultdict(int)
        for relay in relays:
            sid = getattr(relay, "station_id", None)
            if sid is not None:
                usage[sid] += 1
        return dict(usage)


# ---------------------------------------------------------------------------
# LEGACY layer: DynamicRelayManager (kept for the old standalone module)
# ---------------------------------------------------------------------------
# The formal Cooperative-HALNS does NOT use this class.  It is preserved
# unchanged for the legacy dynamic-station module and its tests.


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class DynamicRelayConfig:
    """Search-effort controls for the legacy dynamic relay station manager.

    ``max_active_stations`` is a hard upper bound, **not** a target.
    Stations are only activated when they provide sufficient marginal gain.

    None of these are problem constraints — they only bound computational
    effort.
    """

    min_active_stations: int = 2
    max_active_stations: int = 12
    station_refresh_interval: int = 25
    station_candidate_pool: int = 30
    station_elite_keep: int = 2
    station_drop_patience: int = 60
    #: Minimum score improvement required to activate a new station.
    marginal_activation_threshold: float = 0.05
    #: Score margin below which a station is considered for drop.
    station_hysteresis_margin: float = 0.02
    #: Force-rotate the lowest non-pinned station every N refreshes (0=disabled).
    station_rotation_interval: int = 5


# ---------------------------------------------------------------------------
# Station scoring (legacy)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _StationContext:
    """Pre-computed per-station statistics used for scoring."""

    station: RelayStation
    urgent_task_count: int = 0
    pickup_rescue_potential: float = 0.0
    blocker_release_potential: float = 0.0
    receiver_accessibility: float = 0.0
    detour_penalty: float = 0.0
    composite_score: float = 0.0


def score_station(
    problem: Problem,
    station: RelayStation,
    critical_tasks: list[tuple[int, float, int]],
    evaluator: RouteEvaluator,
    routes: Routes,
) -> float:
    """Compute a surrogate score for *station* — biased towards blocker-release.

    A station is valuable if:
    1. A blocker can drop its package there (short detour from blocker's route)
    2. Another drone can pick it up and deliver on time
    3. The original drone is freed earlier → earlier critical pickup
    """
    from .ownership_reassignment import find_upstream_blockers

    station_pt = station.point
    blocker_score = 0.0
    receiver_access = 0.0
    total_blockers = 0

    for task_id, slack, drone in critical_tasks:
        task = problem.task(task_id)
        urgency = 1.0 + max(0.0, -slack) / 10.0  # normalize urgency

        # Find blockers on this drone
        blockers = find_upstream_blockers(problem, evaluator, routes[drone], task_id)
        for blocker_tid, holding_min in blockers[:5]:
            total_blockers += 1
            blocker_task = problem.task(blocker_tid)
            # How much detour to drop blocker at this station?
            detour_to_station = (
                blocker_task.delivery.distance_to(station_pt)
                + station_pt.distance_to(task.pickup)
                - blocker_task.delivery.distance_to(task.pickup)
            )
            # Station value = holding_time * urgency / (1 + detour)
            # Higher holding time → more to gain from relay
            # Lower detour → cheaper relay
            if detour_to_station < 50:  # reasonable detour in km
                blocker_score += holding_min * urgency / (1.0 + detour_to_station)

        # Receiver accessibility: can other drones reach this station?
        for other_drone in range(problem.drone_count):
            if other_drone == drone:
                continue
            other_route = routes[other_drone]
            if not other_route:
                continue
            # Simple heuristic: how close is the station to any point on the route?
            min_dist = float("inf")
            for visit in other_route:
                tid = abs(visit)
                try:
                    pt = problem.task(tid).pickup if visit > 0 else problem.task(tid).delivery
                except (KeyError, AttributeError):
                    continue
                d = pt.distance_to(station_pt)
                if d < min_dist:
                    min_dist = d
            if min_dist < 50:
                receiver_access += 1.0 / (1.0 + min_dist / 10.0)

    # Composite score: blocker-release dominates
    blocker_weight = 2.0
    receiver_weight = 0.5
    return blocker_weight * blocker_score + receiver_weight * receiver_access


# ---------------------------------------------------------------------------
# Dynamic relay manager (legacy)
# ---------------------------------------------------------------------------


@dataclass
class DynamicRelayManager:
    """Manages an evolving operational relay-station shortlist.

    .. deprecated::
        Legacy standalone module.  The formal Cooperative-HALNS uses
        :class:`DynamicRelayPointPool` instead.
    """

    config: DynamicRelayConfig = field(default_factory=DynamicRelayConfig)
    active_stations: list[RelayStation] = field(default_factory=list)
    station_usage: dict[int, int] = field(default_factory=lambda: defaultdict(int))
    station_gain: dict[int, float] = field(default_factory=dict)
    station_last_improved: dict[int, int] = field(default_factory=dict)
    candidate_pool: list[tuple[RelayStation, float]] = field(default_factory=list)
    _iteration: int = 0
    _cached_scores: dict[int, float] = field(default_factory=dict)

    def initialize(
        self,
        problem: Problem,
        evaluator: RouteEvaluator,
        routes: Routes,
    ) -> None:
        """Build the initial active set from candidate scoring."""
        all_sites = candidate_sites(problem)
        from .ownership_reassignment import identify_critical_tasks
        critical = identify_critical_tasks(problem, evaluator, routes, top_k=15)

        scored: list[tuple[RelayStation, float]] = []
        for site in all_sites:
            s = score_station(problem, site, critical, evaluator, routes)
            scored.append((site, s))
        scored.sort(key=lambda x: -x[1])

        self.candidate_pool = scored[: self.config.station_candidate_pool]
        # Build a lookup from scored for initial station gains
        score_lookup = {site.id: s for site, s in scored}
        self.active_stations = [
            site for site, _ in scored[: self.config.min_active_stations]
        ]
        for st in self.active_stations:
            self.station_gain[st.id] = score_lookup.get(st.id, 0.0)
            self.station_last_improved[st.id] = 0
        self._iteration = 0

    def refresh_if_needed(
        self,
        problem: Problem,
        evaluator: RouteEvaluator,
        routes: Routes,
        force: bool = False,
    ) -> bool:
        """Re-score candidates and potentially adjust active stations.

        Only activates a station if its marginal gain over the worst active
        exceeds ``marginal_activation_threshold``.  ``max_active_stations``
        is a hard cap, NOT a fill target.

        Returns ``True`` if any station was added, dropped, or replaced.
        """
        self._iteration += 1
        if not force and self._iteration % self.config.station_refresh_interval != 0:
            return False
        if not self.active_stations:
            self.initialize(problem, evaluator, routes)
            return True

        from .ownership_reassignment import identify_critical_tasks
        critical = identify_critical_tasks(problem, evaluator, routes, top_k=15)

        # Periodically refresh the candidate pool from ALL sites (not just cached)
        if force or self._iteration % (self.config.station_refresh_interval * 2) == 0:
            all_sites = candidate_sites(problem)
            fresh_scored: list[tuple[RelayStation, float]] = []
            for site in all_sites:
                s = score_station(problem, site, critical, evaluator, routes)
                fresh_scored.append((site, s))
            fresh_scored.sort(key=lambda x: -x[1])
            self.candidate_pool = fresh_scored[: self.config.station_candidate_pool]
        else:
            # Re-score existing candidates
            new_pool: list[tuple[RelayStation, float]] = []
            for site, _ in self.candidate_pool:
                s = score_station(problem, site, critical, evaluator, routes)
                new_pool.append((site, s))
            new_pool.sort(key=lambda x: -x[1])
            self.candidate_pool = new_pool

        changed = False
        pinned = self._pinned_station_ids()

        for candidate_site, candidate_score in self.candidate_pool:
            # Recompute active_ids after every mutation
            active_ids = {st.id for st in self.active_stations}
            if candidate_site.id in active_ids:
                continue

            if len(self.active_stations) >= self.config.max_active_stations:
                # At cap: only replace if candidate is strictly better
                victim = self._find_replacement_victim(pinned)
                if victim is None:
                    break
                victim_score = self.station_gain.get(victim.id, 0.0)
                if candidate_score <= victim_score:
                    continue  # not strictly better — skip
                self.active_stations.remove(victim)
                self.active_stations.append(candidate_site)
                self.station_gain[candidate_site.id] = candidate_score
                self.station_last_improved[candidate_site.id] = self._iteration
                changed = True
            else:
                # Below cap.  When the active set has not yet reached
                # ``min_active_stations`` we must always allow filling the
                # minimum set (the marginal-gain threshold would otherwise
                # prevent even the first station from ever being activated).
                # Only above the minimum do we require sufficient marginal
                # gain, so the shortlist never balloons purely by score noise.
                if len(self.active_stations) >= self.config.min_active_stations:
                    worst_score = min(
                        (self.station_gain.get(st.id, 0.0)
                         for st in self.active_stations),
                        default=0.0,
                    )
                    marginal = candidate_score - worst_score
                    if marginal < self.config.marginal_activation_threshold:
                        continue  # insufficient marginal gain — don't auto-fill
                self.active_stations.append(candidate_site)
                self.station_gain[candidate_site.id] = candidate_score
                self.station_last_improved[candidate_site.id] = self._iteration
                changed = True

        # Drop stale unused stations (with hysteresis)
        changed |= self._drop_stale(pinned)

        # Forced rotation: periodically replace the lowest non-pinned station
        if self.config.station_rotation_interval > 0 and self._iteration % self.config.station_rotation_interval == 0:
            changed |= self._rotate_one(pinned)

        return changed

    def try_add_station(
        self,
        problem: Problem,
        station: RelayStation,
        score: float = 0.0,
    ) -> bool:
        """Add a station if below max_active (no marginal check here)."""
        if len(self.active_stations) >= self.config.max_active_stations:
            return False
        if any(st.id == station.id for st in self.active_stations):
            return False
        self.active_stations.append(station)
        self.station_gain[station.id] = score
        self.station_last_improved[station.id] = self._iteration
        return True

    def try_drop_station(self, station_id: int) -> bool:
        """Drop a station only if unused and above min_active."""
        if len(self.active_stations) <= self.config.min_active_stations:
            return False
        if station_id in self._pinned_station_ids():
            return False
        for st in self.active_stations:
            if st.id == station_id:
                self.active_stations.remove(st)
                return True
        return False

    def try_replace_station(
        self, old_id: int, new_station: RelayStation
    ) -> bool:
        """Replace an unused station with a new one."""
        if old_id in self._pinned_station_ids():
            return False
        for i, st in enumerate(self.active_stations):
            if st.id == old_id:
                self.active_stations[i] = new_station
                self.station_gain[new_station.id] = 0.0
                self.station_last_improved[new_station.id] = self._iteration
                return True
        return False

    def record_usage(self, station_id: int) -> None:
        """Mark a station as referenced by an active relay."""
        self.station_usage[station_id] = self.station_usage.get(station_id, 0) + 1

    def release_usage(self, station_id: int) -> None:
        """Decrement usage count for a station."""
        self.station_usage[station_id] = max(
            0, self.station_usage.get(station_id, 0) - 1
        )

    def reset_usage(self, active_relays: Sequence[object]) -> None:
        """Recalculate usage from a fresh set of relays."""
        self.station_usage.clear()
        for relay in active_relays:
            sid = getattr(relay, "station_id", None)
            if sid is not None:
                self.station_usage[sid] = self.station_usage.get(sid, 0) + 1

    # -- internal helpers --------------------------------------------------

    def _pinned_station_ids(self) -> set[int]:
        return {sid for sid, count in self.station_usage.items() if count > 0}

    def _find_replacement_victim(
        self, pinned: set[int]
    ) -> RelayStation | None:
        """Find the worst non-pinned, non-elite active station to replace."""
        elite_ids = {
            st.id
            for st in sorted(
                self.active_stations,
                key=lambda s: self.station_gain.get(s.id, 0.0),
                reverse=True,
            )[: self.config.station_elite_keep]
        }
        for st in sorted(
            self.active_stations,
            key=lambda s: self.station_gain.get(s.id, 0.0),
        ):
            if st.id in pinned or st.id in elite_ids:
                continue
            return st
        return None

    def _drop_stale(self, pinned: set[int]) -> bool:
        """Drop unused stations that haven't improved for too long.

        Hysteresis: a station is only dropped if its score is below
        ``best_candidate_score - hysteresis_margin``, preventing
        oscillation (add → drop → add cycle).
        """
        changed = False
        if len(self.active_stations) <= self.config.min_active_stations:
            return False

        best_candidate_score = max(
            (s for _, s in self.candidate_pool), default=0.0
        )

        for st in list(self.active_stations):
            if st.id in pinned:
                continue
            last = self.station_last_improved.get(st.id, 0)
            stale = self._iteration - last > self.config.station_drop_patience
            if not stale:
                continue
            # Hysteresis check: don't drop if score is close to best candidate
            current_score = self.station_gain.get(st.id, 0.0)
            if current_score >= best_candidate_score - self.config.station_hysteresis_margin:
                continue  # still competitive — keep it
            if len(self.active_stations) > self.config.min_active_stations:
                self.active_stations.remove(st)
                changed = True
        return changed

    def _rotate_one(self, pinned: set[int]) -> bool:
        """Force-replace the lowest-scoring non-pinned station with a fresh candidate.

        Rotation bypasses elite protection to ensure exploration.
        Rotation is a replace operation (count stays same), not a drop.
        """
        if len(self.active_stations) == 0:
            return False

        # Find lowest non-pinned station (ignore elite for rotation)
        victim = None
        for st in sorted(
            self.active_stations,
            key=lambda s: self.station_gain.get(s.id, 0.0),
        ):
            if st.id not in pinned:
                victim = st
                break

        if victim is None:
            return False

        # Find best candidate not already active
        active_ids = {st.id for st in self.active_stations}
        for candidate_site, candidate_score in self.candidate_pool:
            if candidate_site.id in active_ids:
                continue
            # Accept even if score is slightly lower (exploration)
            self.active_stations.remove(victim)
            self.active_stations.append(candidate_site)
            self.station_gain[candidate_site.id] = candidate_score
            self.station_last_improved[candidate_site.id] = self._iteration
            return True

        return False
