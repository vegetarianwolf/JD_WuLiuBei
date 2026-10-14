"""Conservative three-aircraft receiver-first repooling geometry.

Two sources bring one parcel each; an empty recipient arrives before both,
takes the first parcel at its source's arrival, waits carrying one, then takes
the second at the other source's arrival. Sources leave at their own arrivals.
The fixed prefixes do not depend on the meeting coordinate. The recipient
focus is a guaranteed feasible anchor exactly when either meeting is possible.

Star projection, geometric medians and deadline ellipses generate bounded
seeds only. Distance/clock lower bounds may have incompatible minimizers and
are never described as simultaneously attainable or accepted full solutions.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import isfinite, nextafter
from time import perf_counter

from uav_dispatch.model import Point

try:  # Frozen worker files are colocated; never read a live ROOT module first.
    import mobile_relay_refinement as mobile
    import mobile_deadline_geometry as deadline_geometry
except ImportError:
    from algorithm.experiments import mobile_relay_refinement as mobile
    from algorithm.experiments import mobile_deadline_geometry as deadline_geometry


CLOCK_MARGIN_MIN = 1e-8
DISTANCE_MARGIN_KM = 1e-10
LATENESS_EPSILON_MIN = 1e-9


class _Deadline(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Geometry:
    incoming_points: tuple
    outgoing_points: tuple
    prefix_times: tuple
    speed: float


def _point(point):
    if not isinstance(point, Point):
        point = Point(*point)
    if not isfinite(point.x) or not isfinite(point.y):
        raise ValueError("finite Euclidean coordinates required")
    return point


def _coordinates(point):
    return None if point is None else [point.x, point.y]


def prepare_geometry(incoming_points, outgoing_points, prefix_times, *, speed,
                     distance_matrix_km=None):
    """Fixed A/B/C prefixes and actual next visits, with open source tails."""
    if distance_matrix_km is not None:
        raise ValueError("custom distance matrices are unsupported by Euclidean geometry")
    if len(incoming_points) != 3 or len(outgoing_points) != 3 or len(prefix_times) != 3:
        raise ValueError("three incoming/outgoing points and prefix times required")
    if not isfinite(speed) or speed <= 0 or any(not isfinite(t) or t < 0 for t in prefix_times):
        raise ValueError("positive finite speed and nonnegative finite prefix times required")
    incoming = tuple(_point(p) for p in incoming_points)
    outgoing = tuple(None if p is None else _point(p) for p in outgoing_points)
    if outgoing[2] is None:
        raise ValueError("recipient must have a real outgoing delivery-side visit")
    return Geometry(incoming, outgoing, tuple(prefix_times), speed)


def receiver_first_possible(context):
    """Exact existence criterion; C's prefix endpoint itself witnesses it."""
    a, b, c = context.incoming_points
    ta, tb, tc = context.prefix_times
    bounds = (ta + a.distance_to(c) / context.speed,
              tb + b.distance_to(c) / context.speed)
    return dict(possible=tc <= min(bounds), anchor=_coordinates(c),
                recipient_prefix_time_min=tc, source_at_recipient_focus_min=list(bounds),
                criterion="tC <= min(tA+distance(uA,uC)/speed,tB+distance(uB,uC)/speed)")


def receiver_first_feasible(context, point):
    """Exact physical clocks; no source waiting or early parcel caching."""
    point = _point(point)
    arrivals = tuple(t + u.distance_to(point) / context.speed
                     for t, u in zip(context.prefix_times, context.incoming_points))
    first, second = min(arrivals[:2]), max(arrivals[:2])
    valid = arrivals[2] <= first
    order = [0, 1] if arrivals[0] <= arrivals[1] else [1, 0]
    return dict(feasible=valid, arrival_times_min=list(arrivals), first_min=first, second_min=second,
        first_pickup_min=first, second_pickup_min=second, pickup_source_order=order,
        source_departure_times_min=list(arrivals[:2]), recipient_departure_min=second,
        receiver_wait_split_min=[first-arrivals[2], second-first] if valid else None,
        aircraft_waiting_min=[0., 0., second-arrivals[2]] if valid else None,
        recipient_waiting_loads=[0, 1] if valid else None, ground_cache_min=0. if valid else None)


def _check(deadline):
    if deadline is not None and perf_counter() >= deadline:
        raise _Deadline


def project_to_feasible_star(context, seed, *, iterations=48, deadline=None):
    """Farthest verified feasible point on uC->seed, without global claims.

    On that ray, each aC-aSource is nondecreasing by the reverse triangle
    inequality. Both constraints therefore leave one initial feasible interval.
    The final returned coordinate is checked again, including floating rounding.
    """
    if type(iterations) is not int or iterations < 1:
        raise ValueError("positive integer projection iterations required")
    seed = _point(seed)
    anchor = context.incoming_points[2]
    metadata = dict(seed=_coordinates(seed), anchor=_coordinates(anchor), fraction=None,
        bisection_iterations=0, exact_seed_feasible=False, rounding_fallback=False,
        deadline_reached=False, globally_optimal=False)
    try:
        _check(deadline)
        if not receiver_first_possible(context)["possible"]:
            return dict(point=None, metadata={**metadata, "reason": "fixed_prefix_existence_criterion_failed"})
        if receiver_first_feasible(context, seed)["feasible"]:
            metadata.update(fraction=1., exact_seed_feasible=True)
            return dict(point=seed, metadata=metadata)
        lower, upper = 0., 1.
        for _ in range(iterations):
            _check(deadline)
            middle = (lower + upper) / 2.
            trial = Point(anchor.x + middle*(seed.x-anchor.x), anchor.y + middle*(seed.y-anchor.y))
            if receiver_first_feasible(context, trial)["feasible"]:
                lower = middle
            else:
                upper = middle
            metadata["bisection_iterations"] += 1
        for _ in range(8):
            _check(deadline)
            point = Point(anchor.x + lower*(seed.x-anchor.x), anchor.y + lower*(seed.y-anchor.y))
            if receiver_first_feasible(context, point)["feasible"]:
                metadata["fraction"] = lower
                return dict(point=point, metadata=metadata)
            lower = nextafter(lower, 0.)
            metadata["rounding_fallback"] = True
        # A known witness is preferable to a numerically unverified boundary.
        metadata.update(fraction=0., rounding_fallback=True)
        return dict(point=anchor, metadata=metadata)
    except _Deadline:
        metadata["deadline_reached"] = True
        return dict(point=None, metadata=metadata)


def _matchings(indices):
    if not indices:
        yield ()
        return
    first = indices[0]
    for position in range(1, len(indices)):
        second = indices[position]
        remaining = indices[1:position] + indices[position+1:]
        for pairs in _matchings(remaining):
            yield ((first, second),) + pairs


def distance_lower_bound(context, constant_distance=0.):
    """Absolute Dconst plus the largest terminal-pair triangle bound."""
    if not isfinite(constant_distance) or constant_distance < 0:
        raise ValueError("finite nonnegative absolute constant distance required")
    terminals, labels = [], []
    for side in range(3):
        terminals.append(context.incoming_points[side])
        labels.append("u" + "ABC"[side])
        if context.outgoing_points[side] is not None:
            terminals.append(context.outgoing_points[side])
            labels.append("v" + "ABC"[side])
    indices = tuple(range(len(terminals)))
    families = ((None, indices),) if len(indices) % 2 == 0 else tuple(
        (unmatched, tuple(i for i in indices if i != unmatched)) for unmatched in indices)
    best, selected, unmatched_label, count = -1., (), None, 0
    for unmatched, family in families:
        for pairs in _matchings(family):
            count += 1
            value = sum(terminals[i].distance_to(terminals[j]) for i, j in pairs)
            if value > best:
                best, selected = value, pairs
                unmatched_label = None if unmatched is None else labels[unmatched]
    raw = constant_distance + max(0., best)
    return dict(distance_km=max(0., raw-DISTANCE_MARGIN_KM), metadata=dict(
        constant_distance_km=constant_distance, terminal_labels=labels,
        terminal_coordinates=[_coordinates(p) for p in terminals],
        pairing_bound_km=max(0., best), maximizing_pairs=[[labels[i], labels[j]] for i, j in selected],
        unmatched_terminal=unmatched_label, matching_count=count,
        conservative_distance_margin_km=DISTANCE_MARGIN_KM,
        geometric_median_is_lower_bound=False, jointly_attainable_not_claimed=True))


def clock_lower_bounds(context):
    """Conservative A/B/C first future clocks; add each fixed suffix constant.

    A/B depend on their source and the recipient; C depends on all three.
    These independently minimized triangle bounds need not share a meeting.
    """
    dependencies = ((0, 2), (1, 2), (0, 1, 2))
    raw, safe = [], []
    for side, sources in enumerate(dependencies):
        outgoing = context.outgoing_points[side]
        value = None if outgoing is None else max(context.prefix_times[q]
            + context.incoming_points[q].distance_to(outgoing)/context.speed for q in sources)
        raw.append(value)
        safe.append(None if value is None else max(0., value-CLOCK_MARGIN_MIN))
    return dict(first_future_clock_min=tuple(safe), metadata=dict(
        raw_first_future_clock_min=raw, prefix_dependencies=[list(d) for d in dependencies],
        conservative_clock_margin_min=CLOCK_MARGIN_MIN, lateness_epsilon_min=LATENESS_EPSILON_MIN,
        jointly_attainable_not_claimed=True))


def _deadline_sets(context, constraints):
    groups = {}
    for item in constraints:
        source = item["prefix_source"]
        if type(source) is not int or not 0 <= source < 3:
            raise ValueError("deadline prefix_source must be zero/one/two")
        outgoing = _point(item["outgoing"])
        residual = item["residual_deadline_min"]
        if not isfinite(residual):
            raise ValueError("finite deadline residual required")
        group = (outgoing.x, outgoing.y, residual)
        groups.setdefault(group, []).append(dict(focus_a=context.incoming_points[source],
            focus_b=outgoing, limit_km=context.speed*(residual-context.prefix_times[source])))
    # Preserve the caller's deterministic critical-task ranking when applying
    # the bounded set limit; coordinate sorting would privilege one route.
    return tuple((key, tuple(value)) for key, value in groups.items())


def points(context, *, deadline, limit=24, deadline_constraints=(), max_deadline_sets=8,
           max_deadline_seeds=3, max_joint_deadline_sets=2, star_iterations=48, ellipse_iterations=24,
           ellipse_projection_iterations=32, ellipse_tolerance_km=1e-9):
    """Deterministic bounded physical seeds with verified deadline annotations.

    ``deadline_constraints`` entries contain prefix_source=0/1/2, outgoing,
    residual_deadline_min=(task deadline minus fixed suffix travel). Equal
    outgoing/residual entries form one target's ellipse intersection. A point
    projected onto the receiver-first region is rechecked against all those
    ellipses; it is never labeled a rescue solely because projection converged.
    """
    bounds = (limit, max_deadline_sets, max_deadline_seeds, star_iterations,
              ellipse_iterations, ellipse_projection_iterations)
    if any(type(n) is not int or n < 1 for n in bounds) or not isfinite(deadline):
        raise ValueError("positive finite point/projection bounds and deadline required")
    if type(max_joint_deadline_sets) is not int or max_joint_deadline_sets < 0:
        raise ValueError("nonnegative integer joint deadline-set bound required")
    if not isfinite(ellipse_tolerance_km) or ellipse_tolerance_km < 0:
        raise ValueError("nonnegative finite ellipse tolerance required")
    possible = receiver_first_possible(context)
    stats = dict(receiver_first_existence=possible, raw_seeds_generated=0, star_projections=0,
        deadline_reached=False, projection_calls=0, projection_iterations=0,
        empty_deadline_sets=0, bounded_ellipse_failures=0, deadline_sets_examined=0,
        joint_deadline_sets_examined=[],
        duplicate_points=0, point_records=[], deadline_sets=[], globally_optimal=False,
        failed_seed_search_is_infeasibility_certificate=False,
        assumptions=dict(equal_aircraft_speed=True, euclidean_distances=True,
            prefixes_coordinate_independent=True, recipient_empty_before_first_pickup=True,
            unrelated_old_relay_clocks_not_supported=True),
        configuration=dict(limit=limit,max_deadline_sets=max_deadline_sets,max_deadline_seeds=max_deadline_seeds,
            max_joint_deadline_sets=max_joint_deadline_sets,
            star_iterations=star_iterations,ellipse_iterations=ellipse_iterations,
            ellipse_projection_iterations=ellipse_projection_iterations,
            ellipse_tolerance_km=ellipse_tolerance_km))
    if not possible["possible"]:
        return dict(points=(), metadata=stats)
    pool, selected, seen = [], {}, set()
    def add(point, origin):
        if point is not None:
            pool.append((point, origin))
    try:
        _check(deadline)
        incoming, outgoing = context.incoming_points, context.outgoing_points
        terminals = tuple(p for pair in zip(incoming, outgoing) for p in pair if p is not None)
        median = mobile._geometric_median(terminals)
        anchor = incoming[2]
        anchor_signature = (round(anchor.x,10),round(anchor.y,10))
        anchor_timing = receiver_first_feasible(context, anchor)
        anchor_cost = sum(p.distance_to(anchor) for p in terminals)
        selected[anchor_signature] = (anchor, (0, anchor_cost, anchor_timing["second_min"], anchor.x, anchor.y),
            dict(point=_coordinates(anchor),origin="guaranteed_recipient_focus",star_fraction=0.,
                 verified_deadline_sets=[],meeting_terminal_distance_km=anchor_cost,physical_timing=anchor_timing))
        add(incoming[2], "guaranteed_recipient_focus")
        add(median, "geometric_median_seed_only")
        for point in terminals:
            add(point, "terminal_focus")
        segments = tuple((a, b) for a, b in zip(incoming, outgoing) if b is not None)
        for a, b in segments:
            add(mobile._mid(a, b), "path_midpoint")
        for (a, b), (c, d) in combinations(segments, 2):
            for point in mobile._intersections(a, b, c, d):
                add(point, "path_intersection")
        equal = mobile._earliest_meeting(incoming[0], incoming[1],
            context.prefix_times[0], context.prefix_times[1], context.speed)
        add(equal, "source_arrival_equality_seed")
        for source in (0, 1):
            add(mobile._earliest_meeting(incoming[source], incoming[2],
                context.prefix_times[source], context.prefix_times[2], context.speed), "source_recipient_boundary_seed")
        for fraction in (.25, .5, .75):
            c = incoming[2]
            add(Point(c.x+fraction*(equal.x-c.x), c.y+fraction*(equal.y-c.y)), "near_source_equality_ray")
        sets = _deadline_sets(context, deadline_constraints)[:max_deadline_sets]
        constraints_all = [p for _, cs in sets for c in cs for p in (c["focus_a"], c["focus_b"])]
        box_points = (*terminals, *constraints_all)
        box = (min(p.x for p in box_points), max(p.x for p in box_points),
               min(p.y for p in box_points), max(p.y for p in box_points))
        for set_id, (key, constraints) in enumerate(sets):
            _check(deadline)
            stats["deadline_sets_examined"] += 1
            stats["deadline_sets"].append(dict(set_id=set_id,outgoing=[key[0],key[1]],residual_deadline_min=key[2],
                constraints=[dict(focus_a=_coordinates(c["focus_a"]),focus_b=_coordinates(c["focus_b"]),
                                  limit_km=c["limit_km"]) for c in constraints]))
            if any(c["limit_km"] < c["focus_a"].distance_to(c["focus_b"])-ellipse_tolerance_km for c in constraints):
                stats["empty_deadline_sets"] += 1
                continue
            seeds = (median, incoming[2], equal, Point(key[0], key[1]))
            for seed in seeds[:max_deadline_seeds]:
                _check(deadline)
                projected, iterations = deadline_geometry._project_intersection(seed, constraints, box,
                    deadline=deadline, stats=stats, max_iterations=ellipse_iterations,
                    projection_iterations=ellipse_projection_iterations, tolerance=ellipse_tolerance_km)
                stats["projection_iterations"] += iterations
                if projected is None:
                    stats["bounded_ellipse_failures"] += 1
                else:
                    add(projected[0], "deadline_ellipse_seed")
        # A bounded pair of critical targets can rescue both a source suffix
        # and the recipient suffix together. Feasibility is still rechecked
        # after star projection, separately for every individual target set.
        for left,right in tuple(combinations(range(len(sets)),2))[:max_joint_deadline_sets]:
            _check(deadline)
            constraints = sets[left][1] + sets[right][1]
            stats["joint_deadline_sets_examined"].append([left,right])
            seeds = (median,incoming[2],equal)
            for seed in seeds[:max_deadline_seeds]:
                _check(deadline)
                projected, iterations = deadline_geometry._project_intersection(seed,constraints,box,
                    deadline=deadline,stats=stats,max_iterations=ellipse_iterations,
                    projection_iterations=ellipse_projection_iterations,tolerance=ellipse_tolerance_km)
                stats["projection_iterations"] += iterations
                if projected is None:
                    stats["bounded_ellipse_failures"] += 1
                else:
                    add(projected[0],"joint_deadline_ellipse_seed")
        stats["raw_seeds_generated"] = len(pool)
        for seed, origin in pool:
            _check(deadline)
            projection = project_to_feasible_star(context, seed, iterations=star_iterations, deadline=deadline)
            stats["star_projections"] += 1
            if projection["metadata"]["deadline_reached"]:
                raise _Deadline
            point = projection["point"]
            if point is None or not receiver_first_feasible(context, point)["feasible"]:
                continue
            signature = (round(point.x, 10), round(point.y, 10))
            if signature in seen:
                stats["duplicate_points"] += 1
                continue
            seen.add(signature)
            valid_sets = [i for i, (_, constraints) in enumerate(sets) if all(
                deadline_geometry._residual(point, c) <= ellipse_tolerance_km for c in constraints)]
            timing = receiver_first_feasible(context, point)
            cost = sum(p.distance_to(point) for p in terminals)
            priority = (-len(valid_sets), cost, timing["second_min"], point.x, point.y)
            selected[signature] = (point, priority, dict(point=_coordinates(point), origin=origin,
                star_fraction=projection["metadata"]["fraction"], verified_deadline_sets=valid_sets,
                meeting_terminal_distance_km=cost, physical_timing=timing))
        anchor = incoming[2]
        anchor_signature = (round(anchor.x,10), round(anchor.y,10))
        ordered = sorted(selected.values(), key=lambda item:item[1])
        anchor_item = selected.get(anchor_signature)
        if anchor_item is not None:
            ordered = [anchor_item] + [item for item in ordered if item is not anchor_item]
        ordered = ordered[:limit]
        stats["point_records"] = [item[2] for item in ordered]
        return dict(points=tuple(item[0] for item in ordered), metadata=stats)
    except (_Deadline, deadline_geometry._Deadline):
        stats["deadline_reached"] = True
        # Previously checked coordinates are still only seeds, not solutions.
        ordered = sorted(selected.values(), key=lambda item:item[1])
        anchor = context.incoming_points[2]
        anchor_item = selected.get((round(anchor.x,10),round(anchor.y,10)))
        if anchor_item is not None:
            ordered = [anchor_item] + [item for item in ordered if item is not anchor_item]
        ordered = ordered[:limit]
        stats["point_records"] = [item[2] for item in ordered]
        return dict(points=tuple(item[0] for item in ordered), metadata=stats)


prepare_context = prepare_geometry
candidate_points = points
