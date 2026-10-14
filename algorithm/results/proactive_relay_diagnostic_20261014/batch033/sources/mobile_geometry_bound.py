"""Absolute geometric lower bounds for one fixed mobile cargo-swap specification.

This is a pure screening helper: it neither selects a meeting coordinate nor
evaluates/accepts a solution. Old handoff waiting is omitted. Distance and each
route's future-time minima need not share a meeting point; summing their
positive-weight lower bounds remains valid. Conservative numerical margins
only weaken this bound and never change a final physical score.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from uav_dispatch.model import Score
try:
    import mobile_relay_refinement as v1
except ImportError:
    from . import mobile_relay_refinement as v1


CLOCK_MARGIN_MIN = 1e-8
DISTANCE_MARGIN_KM = 1e-10
LATE_THRESHOLD_MIN = 1e-9


def _late(clock, deadline):
    value = max(0., clock - CLOCK_MARGIN_MIN - deadline)
    return (1, value) if value > LATE_THRESHOLD_MIN else (0, 0.)


def _final_task(problem, visit):
    if visit >= 0:
        return None
    leg = (problem.leg_registry or {}).get(-visit)
    return -visit if leg is None else leg.task_id if leg.kind == "RELAY_OUT" else None


@dataclass(frozen=True)
class _Route:
    points: tuple
    finals: tuple
    distance_prefix: tuple
    clock_prefix: tuple
    active_prefix: tuple
    count_prefix: tuple
    lateness_prefix: tuple


@dataclass
class Context:
    """One immutable problem/routes generation; mapped geometry cache is local."""
    problem: object
    routes: tuple
    layouts: tuple
    mapped: dict = field(default_factory=dict)


def _layout(problem, route, owner, mapping=None):
    mapping = mapping or {}
    points, finals = [], []
    previous = problem.home_points()[owner]
    distances, clocks, actives, counts, latenesses = [0.], [0.], [()], [0], [0.]
    active = []
    for visit in route:
        received = mapping.get(-visit) if visit < 0 else None
        point = problem.task(received).delivery if received is not None else problem.point_for_visit(visit)
        task_id = received if received is not None else _final_task(problem, visit)
        km = previous.distance_to(point)
        distances.append(distances[-1] + km)
        clocks.append(clocks[-1] + km / problem.speed_km_per_min)
        count, lateness = _late(clocks[-1], problem.task(task_id).deadline_min) if task_id is not None else (0, 0.)
        counts.append(counts[-1] + count)
        latenesses.append(latenesses[-1] + lateness)
        if visit > 0:
            active.append(visit)
        else:
            try:
                active.remove(-visit)
            except ValueError as exc:
                raise ValueError("Input route contains a delivery before its transport pickup") from exc
        actives.append(tuple(active))
        points.append(point)
        finals.append(task_id)
        previous = point
    if active:
        raise ValueError("Input route ends with cargo still aboard")
    return _Route(tuple(points), tuple(finals), tuple(distances), tuple(clocks),
                  tuple(actives), tuple(counts), tuple(latenesses))


def prepare_context(problem, routes):
    """Cache original no-wait clocks, final-event semantics and Euclidean paths."""
    if problem.distance_matrix_km is not None:
        raise ValueError("Mobile geometric bounds require the default Euclidean distance model")
    materialized = tuple(tuple(route) for route in routes)
    if len(materialized) > problem.drone_count:
        raise ValueError("Too many UAV routes")
    materialized += ((),) * (problem.drone_count - len(materialized))
    return Context(problem, materialized,
                   tuple(_layout(problem, route, i) for i, route in enumerate(materialized)))


def _mapped(context, owner, mapping):
    key = (owner, tuple(sorted(mapping.items())))
    if key not in context.mapped:
        context.mapped[key] = _layout(context.problem, context.routes[owner], owner, mapping)
    return context.mapped[key]


def _validate_spec(context, spec):
    problem = context.problem
    if spec.a == spec.b or not (0 <= spec.a < len(context.routes) and 0 <= spec.b < len(context.routes)):
        raise ValueError("Mobile swap needs two distinct existing UAVs")
    if (not spec.group_a or len(spec.group_a) != len(spec.group_b) or
            len(set(spec.group_a)) != len(spec.group_a) or len(set(spec.group_b)) != len(spec.group_b) or
            set(spec.group_a) & set(spec.group_b) or
            tuple(sorted(spec.receive_a)) != tuple(sorted(spec.group_b)) or
            tuple(sorted(spec.receive_b)) != tuple(sorted(spec.group_a))):
        raise ValueError("Mobile swap groups/mappings must be disjoint equally sized permutations")
    for owner, cut, own in ((spec.a, spec.left, spec.group_a), (spec.b, spec.right, spec.group_b)):
        if not 0 <= cut.position < len(context.routes[owner]):
            raise ValueError("An active cargo cut must have a future visit")
        active = context.layouts[owner].active_prefix[cut.position]
        if not set(own).issubset(active) or any(t in (problem.leg_registry or {}) for t in own):
            raise ValueError("Only DIRECT packages truly active at each cut can be exchanged")
        for task_id in own:
            if context.routes[owner].index(-task_id) < cut.position:
                raise ValueError("Selected final delivery must follow its cut")


def lower_bound(problem, routes, spec, *, context=None):
    """Return ``{'score': Score, 'metadata': ...}`` for every possible meeting point.

    Four endpoints uA,vA,uB,vB bound sum distances to R by the largest of the
    three perfect-pairing sums. A route with mapped first suffix point v has
    earliest possible first suffix clock max(tA+|uA-v|/speed,tB+|uB-v|/speed),
    reached for that route alone by R=v. Subsequent fixed suffix segments add
    their travel time. Prefix and untouched-route final clocks use no waiting.
    """
    if problem.distance_matrix_km is not None:
        raise ValueError("Mobile geometric bounds require the default Euclidean distance model")
    materialized = tuple(tuple(route) for route in routes)
    materialized += ((),) * (problem.drone_count - len(materialized))
    if context is None:
        context = prepare_context(problem, materialized)
    elif context.problem is not problem or context.routes != materialized:
        raise ValueError("Cached mobile-bound context belongs to another problem/routes generation")
    _validate_spec(context, spec)
    ma = v1._slot_map(context.routes[spec.a], spec.left, spec.group_a, spec.receive_a)
    mb = v1._slot_map(context.routes[spec.b], spec.right, spec.group_b, spec.receive_b)
    la, lb = _mapped(context, spec.a, ma), _mapped(context, spec.b, mb)
    ka, kb = spec.left.position, spec.right.position
    ua = context.layouts[spec.a].points[ka - 1] if ka else problem.home_points()[spec.a]
    ub = context.layouts[spec.b].points[kb - 1] if kb else problem.home_points()[spec.b]
    va, vb = la.points[ka], lb.points[kb]
    ta, tb = context.layouts[spec.a].clock_prefix[ka], context.layouts[spec.b].clock_prefix[kb]
    pairings = (ua.distance_to(va) + ub.distance_to(vb),
                ua.distance_to(ub) + va.distance_to(vb),
                ua.distance_to(vb) + va.distance_to(ub))
    pairing_lower = max(pairings)
    mapped_total = sum(layout.distance_prefix[-1] for layout in context.layouts)
    mapped_total += la.distance_prefix[-1] - context.layouts[spec.a].distance_prefix[-1]
    mapped_total += lb.distance_prefix[-1] - context.layouts[spec.b].distance_prefix[-1]
    cut_edges = ua.distance_to(va) + ub.distance_to(vb)
    distance = max(0., mapped_total - cut_edges + pairing_lower - DISTANCE_MARGIN_KM)
    count, lateness, route_metadata, untouched_metadata = 0, 0., [], []
    for owner, layout in enumerate(context.layouts):
        if owner not in (spec.a, spec.b):
            count += layout.count_prefix[-1]
            lateness += layout.lateness_prefix[-1]
            untouched_metadata.append(dict(route_index=owner, lower_late_count=layout.count_prefix[-1],
                lower_lateness_min=layout.lateness_prefix[-1], local_distance_km=layout.distance_prefix[-1]))
    for owner, cut, mapped_layout, first in ((spec.a, ka, la, va), (spec.b, kb, lb, vb)):
        original = context.layouts[owner]
        count += original.count_prefix[cut]
        lateness += original.lateness_prefix[cut]
        first_clock = max(ta + ua.distance_to(first) / problem.speed_km_per_min,
                          tb + ub.distance_to(first) / problem.speed_km_per_min)
        previous, suffix_constant = first, 0.
        deliveries = []
        for position in range(cut, len(mapped_layout.points)):
            point = mapped_layout.points[position]
            suffix_constant += previous.distance_to(point) / problem.speed_km_per_min
            previous = point
            task_id = mapped_layout.finals[position]
            if task_id is None:
                continue
            optimistic = first_clock + suffix_constant
            late_count, late = _late(optimistic, problem.task(task_id).deadline_min)
            count += late_count
            lateness += late
            deliveries.append(dict(task_id=task_id, original_event_position=position,
                deadline_min=problem.task(task_id).deadline_min, suffix_constant_min=suffix_constant,
                optimistic_clock_min=optimistic, conservative_clock_min=max(0., optimistic - CLOCK_MARGIN_MIN),
                lower_late_count=late_count, lower_lateness_min=late))
        route_metadata.append(dict(route_index=owner, cut_position=cut, no_wait_prefix_min=original.clock_prefix[cut],
            prefix_late_count=original.count_prefix[cut], prefix_total_lateness_min=original.lateness_prefix[cut],
            prefix_final_delivery_count=sum(t is not None for t in original.finals[:cut]),
            prefix_final_task_ids=[t for t in original.finals[:cut] if t is not None],
            first_suffix_point=[first.x, first.y], earliest_first_suffix_clock_min=first_clock,
            relaxed_individual_route_minimum_attained_at_point=[first.x, first.y], future_deliveries=deliveries))
    return dict(score=Score(count, lateness, distance), metadata=dict(
        kind="absolute_mobile_geometry_lower_bound", old_handoff_waiting_ignored=True,
        component_minima_need_not_share_one_meeting_point=True,
        terminals=dict(u_a=[ua.x, ua.y], v_a=[va.x, va.y], u_b=[ub.x, ub.y], v_b=[vb.x, vb.y]),
        no_wait_prefix_times_min=[ta, tb], speed_km_per_min=problem.speed_km_per_min,
        perfect_pairing_distance_sums_km=list(pairings), meeting_distance_lower_km=pairing_lower,
        mapped_route_distance_total_km=mapped_total, replaced_cut_edges_km=cut_edges,
        clock_margin_min=CLOCK_MARGIN_MIN, distance_margin_km=DISTANCE_MARGIN_KM,
        late_threshold_min=LATE_THRESHOLD_MIN, route_bounds=route_metadata,
        untouched_route_bounds=untouched_metadata))
