"""Research-only whole-plan, relay-first list scheduling.

Unlike a monotone conversion of an already optimized DIRECT plan, this builds
complete alternative plans from empty routes. Final-delivery ownership is
either retained or assigned to balanced destination clusters. Aircraft can
collect for another aircraft and later perform their own final deliveries.

An action is either a capacity-bounded DIRECT block or an atomic pair of
inbound/outbound blocks at an existing station. The inbound block is committed
before the outbound block and all clocks include empty travel and the actual
handoff wait. The construction does not introduce free repositioning, new
stations, stochastic requests or additional vehicles. A deliberate relay
credit affects proposal ordering only; final adoption uses the unmodified
weighted score over a fully checked complete plan.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations
from math import exp, isclose, isfinite
from random import Random
from time import perf_counter

from uav_dispatch.model import Point
from uav_dispatch.relay import build_plan_index
from uav_dispatch.validation import evaluate_solution


class _Deadline(Exception):
    pass


def _check(deadline, stats):
    if perf_counter() >= deadline:
        stats["deadline_reached"] = True
        raise _Deadline


def weighted_key(score, weights, scales):
    physical = (score.late_count, score.total_lateness_min, score.distance_km)
    return (sum(w * x / s for w, x, s in zip(weights, physical, scales)), *physical)


def _leg_pairs(problem):
    """Use the complete immutable fixed-station registry, not its shortlist.

    The task_relay_candidates field is a production geometric search shortlist,
    rather than a physical prohibition. No missing transport legs are invented.
    """
    result = {}
    for task_id, legs in problem._legs_by_task.items():
        stations = {}
        for leg in legs:
            stations.setdefault(leg.relay_id, {})[leg.kind] = leg.id
        result[task_id] = {station: (parts["RELAY_IN"], parts["RELAY_OUT"])
                           for station, parts in stations.items()
                           if set(parts) == {"RELAY_IN", "RELAY_OUT"}}
    return result


def _balanced_owners(problem, old_owners, rng, deadline, stats, iterations=3):
    """Regret assignment to destination centres with the original K counts."""
    quotas = [sum(owner == i for owner in old_owners.values())
              for i in range(problem.drone_count)]
    active = [i for i, quota in enumerate(quotas) if quota]
    centres = {}
    for i in active:
        points = [problem.task(t).delivery for t, owner in old_owners.items() if owner == i]
        centres[i] = Point(sum(p.x for p in points) / len(points),
                           sum(p.y for p in points) / len(points))
    # Coincident origin homes do not force coincident destination territories.
    if len(active) > 1 and max(centres[active[0]].distance_to(centres[i]) for i in active) < 1e-9:
        pool = list(problem.tasks)
        centres[active[0]] = pool[rng.randrange(len(pool))].delivery
        for i in active[1:]:
            centres[i] = max(pool, key=lambda t: min(t.delivery.distance_to(p)
                                                    for p in centres.values())).delivery
    owners = dict(old_owners)
    for _ in range(iterations):
        _check(deadline, stats)
        remaining = quotas.copy()
        rows = []
        for task in problem.tasks:
            _check(deadline, stats)
            costs = sorted((task.delivery.distance_to(centres[i]), i) for i in active)
            regret = costs[1][0] - costs[0][0] if len(costs) > 1 else 0.
            rows.append((-regret, task.id, costs))
        owners = {}
        for _, task_id, costs in sorted(rows):
            owner = next(i for _, i in costs if remaining[i])
            owners[task_id] = owner
            remaining[owner] -= 1
        for i in active:
            points = [problem.task(t).delivery for t, owner in owners.items() if owner == i]
            centres[i] = Point(sum(p.x for p in points) / len(points),
                               sum(p.y for p in points) / len(points))
    return owners


def _walk(problem, route_index, previous, clock, block, release=None):
    distance, deliveries = 0., {}
    registry = problem.leg_registry or {}
    for visit in block:
        leg_km = problem.distance(previous, visit, from_node=(
            problem.home_node(route_index) if previous is None else None))
        distance += leg_km
        clock += leg_km / problem.speed_km_per_min
        if release is not None and visit > 0:
            clock = max(clock, release)
        previous = visit
        if visit < 0:
            leg = registry.get(-visit)
            if leg is None:
                deliveries[-visit] = clock
            elif leg.kind == "RELAY_OUT":
                deliveries[leg.task_id] = clock
    return distance, clock, deliveries


@dataclass(frozen=True, slots=True)
class _Action:
    rank: float
    owner: int
    carrier: int
    tasks: tuple[int, ...]
    station: int | None
    inbound: tuple[int, ...]
    outbound: tuple[int, ...]
    carrier_end: float
    owner_end: float
    distance: float
    deliveries: tuple[tuple[int, float], ...]


def _action_rank(problem, task_ids, deliveries, distance, occupied_min,
                 coefficients, relay_credit, is_relay, horizon):
    late_count = sum(clock > problem.task(t).deadline_min + 1e-9
                     for t, clock in deliveries.items())
    lateness = sum(max(0., clock - problem.task(t).deadline_min)
                   for t, clock in deliveries.items())
    physical = coefficients[0] * late_count + coefficients[1] * lateness + coefficients[2] * distance
    # Deadline-weighted empirical priority over the actual static task pool.
    # This ranking credit is never inserted into the reported objective.
    urgency = sum(exp(-problem.task(t).deadline_min / horizon) for t in task_ids)
    return ((physical + .2 * coefficients[1] * occupied_min
             - .4 * coefficients[0] * urgency) / len(task_ids)
            - (relay_credit if is_relay else 0.))


def _actions(problem, pending, owners, legs, previous, clocks, coefficients,
             relay_credit, anchor_limit, partner_limit, horizon, deadline, stats):
    """Bounded static task shortlist; every shortlisted route clock is exact."""
    for owner in range(problem.drone_count):
        pool = [t for t in pending if owners[t] == owner]
        if not pool:
            continue
        near = sorted(pool, key=lambda t: (problem.distance(previous[owner], t,
                      from_node=problem.home_node(owner) if previous[owner] is None else None), t))
        urgent = sorted(pool, key=lambda t: (problem.task(t).deadline_min, t))
        anchors = tuple(dict.fromkeys(near[:anchor_limit] + urgent[:anchor_limit]))
        bundles = set()
        for first in anchors:
            bundles.add((first,))
            partners = sorted((t for t in pool if t != first), key=lambda t: (
                problem.task(first).pickup.distance_to(problem.task(t).pickup)
                + problem.task(first).delivery.distance_to(problem.task(t).delivery), t))
            if problem.capacity >= 2:
                bundles.update(tuple(sorted((first, second))) for second in partners[:partner_limit])
        for task_ids in sorted(bundles):
            _check(deadline, stats)
            pickup_orders, delivery_orders = tuple(permutations(task_ids)), tuple(permutations(task_ids))
            for pickup_order in pickup_orders:
                for delivery_order in delivery_orders:
                    direct = pickup_order + tuple(-t for t in delivery_order)
                    km, end, delivered = _walk(problem, owner, previous[owner], clocks[owner], direct)
                    stats["block_evaluations"] += 1
                    rank = _action_rank(problem, task_ids, delivered, km, end - clocks[owner],
                                        coefficients, relay_credit, False, horizon)
                    yield _Action(rank, owner, owner, task_ids, None, (), direct, end, end,
                                  km, tuple(delivered.items()))
            stations = set(legs.get(task_ids[0], {}))
            for task_id in task_ids[1:]:
                stations &= set(legs.get(task_id, {}))
            for station in sorted(stations):
                for carrier in range(problem.drone_count):
                    if carrier == owner:
                        continue
                    _check(deadline, stats)
                    for pickup_order in pickup_orders:
                        in_ids = tuple(legs[t][station][0] for t in pickup_order)
                        inbound = in_ids + tuple(-leg for leg in in_ids)
                        in_km, drop, _ = _walk(problem, carrier, previous[carrier], clocks[carrier], inbound)
                        for delivery_order in delivery_orders:
                            out_ids = tuple(legs[t][station][1] for t in task_ids)
                            outbound = out_ids + tuple(-legs[t][station][1] for t in delivery_order)
                            out_km, finish, delivered = _walk(problem, owner, previous[owner], clocks[owner],
                                                             outbound, release=drop)
                            stats["block_evaluations"] += 1
                            occupied = drop - clocks[carrier] + finish - clocks[owner]
                            rank = _action_rank(problem, task_ids, delivered, in_km + out_km, occupied,
                                                coefficients, relay_credit, True, horizon)
                            yield _Action(rank, owner, carrier, task_ids, station, inbound, outbound,
                                          drop, finish, in_km + out_km, tuple(delivered.items()))


def _construct(problem, owners, legs, *, coefficients, relay_credit,
               anchor_limit, partner_limit, deadline, stats):
    routes = [[] for _ in range(problem.drone_count)]
    previous, clocks = [None] * problem.drone_count, [0.] * problem.drone_count
    pending, simulated, jobs = set(problem.task_ids), {}, []
    horizon = max(1., sum(t.deadline_min for t in problem.tasks) / len(problem.tasks))
    while pending:
        _check(deadline, stats)
        action = min(_actions(problem, pending, owners, legs, previous, clocks, coefficients,
                             relay_credit, anchor_limit, partner_limit, horizon, deadline, stats),
                     key=lambda a: (a.rank, a.owner_end, a.distance, a.tasks,
                                    a.owner, a.carrier, a.station or 0, a.inbound, a.outbound))
        _check(deadline, stats)
        if action.station is not None:
            routes[action.carrier].extend(action.inbound)
            previous[action.carrier], clocks[action.carrier] = action.inbound[-1], action.carrier_end
        routes[action.owner].extend(action.outbound)
        previous[action.owner], clocks[action.owner] = action.outbound[-1], action.owner_end
        simulated.update(action.deliveries)
        pending.difference_update(action.tasks)
        jobs.append(dict(tasks=list(action.tasks), owner=action.owner, carrier=action.carrier,
                         station=action.station, inbound_drop_min=action.carrier_end if action.station else None,
                         final_completion_min=action.owner_end, distance_km=action.distance))
        stats["constructed_blocks"] += 1
    return tuple(tuple(r) for r in routes), simulated, jobs


def _snapshot(routes, checked, key, index, config, jobs):
    return dict(routes=[list(r) for r in routes], key=list(key),
                score=dict(late_count=checked.score.late_count,
                           total_lateness_min=checked.score.total_lateness_min,
                           distance_km=checked.score.distance_km),
                cross_uav_count=sum(index.inbound_route_by_task[t] != index.outbound_route_by_task[t]
                                    for t in index.relay_task_ids),
                relay_task_count=len(index.relay_task_ids), configuration=config, jobs=jobs,
                complete=True, valid=True)


def refine(problem, routes, *, deadline, weights, scales, seed=2026101491,
           max_proposals=6, anchor_limit=2, partner_limit=1, relay_credits=(0., .001, .003),
           relay_enabled=True):
    """Construct bounded whole-plan alternatives and guard the original plan.

    The first complete proposal and best complete constructed proposal are
    returned as JSON-serializable diagnostic snapshots even if neither beats
    the input. No unfinished or completed-after-deadline proposal is adopted.
    The fixed immutable registry is unchanged; only its search shortlist is
    expanded to every physically registered station for these new proposals.
    """
    weights, scales, relay_credits = tuple(weights), tuple(scales), tuple(relay_credits)
    if (len(weights) != 3 or len(scales) != 3
            or any(not isfinite(v) or v <= 0 for v in (*weights, *scales))
            or not isclose(sum(weights), 1.) or not isfinite(deadline)
            or not relay_credits or any(not isfinite(v) or v < 0 for v in relay_credits)):
        raise ValueError("finite positive weights/scales, nonnegative relay credits, and finite deadline required")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 1
           for v in (max_proposals, anchor_limit, partner_limit)):
        raise ValueError("positive integer proposal and shortlist limits required")
    if not isinstance(relay_enabled, bool):
        raise ValueError("relay_enabled must be a boolean")
    started = perf_counter()
    best = tuple(tuple(r) for r in routes)
    if len(best) > problem.drone_count:
        raise ValueError("too many routes")
    best += ((),) * (problem.drone_count - len(best))
    checked = evaluate_solution(problem, best, _force_full=True)
    if not checked.valid:
        raise ValueError(f"invalid initial solution: {checked.violations}")
    initial_key = best_key = weighted_key(checked.score, weights, scales)
    initial_index = build_plan_index(problem, best)
    stats = dict(method="Relay-First-Balanced-List-Scheduling", seed=seed,
                 proposals_attempted=0, complete_proposals=0, invalid_proposals=0,
                 accepted_improvements=0, full_evaluations=1, block_evaluations=0,
                 constructed_blocks=0, deadline_reached=False,
                 candidate_stations="all_existing_registered_stations",
                 relay_enabled=relay_enabled,
                 final_task_limits="original_final_delivery_counts_preserved",
                 first_complete_candidate=None, best_constructed_candidate=None,
                 proposal_metrics=[])
    coefficients = tuple(w / s for w, s in zip(weights, scales))
    legs, rng = _leg_pairs(problem) if relay_enabled else {}, Random(seed)
    old_owners = dict(initial_index.delivery_route_by_task)
    try:
        _check(deadline, stats)
        if relay_enabled and not any(legs.values()):
            raise _NoRelay
        spatial = _balanced_owners(problem, old_owners, rng, deadline, stats)
        ownerships = (("incumbent", old_owners), ("balanced_destination_clusters", spatial))
        for credit in relay_credits:
            for ownership, owners in ownerships:
                if stats["proposals_attempted"] >= max_proposals:
                    break
                _check(deadline, stats)
                stats["proposals_attempted"] += 1
                proposal, simulated, jobs = _construct(problem, owners, legs,
                    coefficients=coefficients, relay_credit=credit, anchor_limit=anchor_limit,
                    partner_limit=partner_limit, deadline=deadline, stats=stats)
                _check(deadline, stats)
                evaluated = evaluate_solution(problem, proposal, _force_full=True)
                stats["full_evaluations"] += 1
                _check(deadline, stats)
                if not evaluated.valid:
                    stats["invalid_proposals"] += 1
                    continue
                if (set(simulated) != set(evaluated.delivery_times_min)
                        or any(not isclose(clock, evaluated.delivery_times_min[t], abs_tol=1e-8,
                                           rel_tol=1e-10) for t, clock in simulated.items())):
                    raise AssertionError("constructive list clocks disagree with full handoff DAG")
                index = build_plan_index(problem, proposal)
                if dict(index.delivery_route_by_task) != owners:
                    raise AssertionError("final responsibility differs from balanced assignment")
                key = weighted_key(evaluated.score, weights, scales)
                config = dict(ownership=ownership, relay_ranking_credit=credit,
                              anchor_limit=anchor_limit, partner_limit=partner_limit)
                snapshot = _snapshot(proposal, evaluated, key, index, config, jobs)
                _check(deadline, stats)
                stats["complete_proposals"] += 1
                stats["proposal_metrics"].append({k: snapshot[k] for k in (
                    "score", "key", "cross_uav_count", "relay_task_count", "configuration")})
                if stats["first_complete_candidate"] is None:
                    stats["first_complete_candidate"] = snapshot
                if (stats["best_constructed_candidate"] is None
                        or key < tuple(stats["best_constructed_candidate"]["key"])):
                    stats["best_constructed_candidate"] = snapshot
                if key < best_key:
                    best, checked, best_key = proposal, evaluated, key
                    stats["accepted_improvements"] += 1
    except (_Deadline, _NoRelay):
        pass
    final_index = build_plan_index(problem, best)
    stats.update(initial_key=list(initial_key), final_key=list(best_key),
                 final_cross_uav_count=sum(final_index.inbound_route_by_task[t] != final_index.outbound_route_by_task[t]
                                           for t in final_index.relay_task_ids),
                 runtime_seconds=perf_counter() - started)
    return dict(routes=best, evaluation=checked, iterations=stats["complete_proposals"], metadata=stats)


class _NoRelay(Exception):
    pass
