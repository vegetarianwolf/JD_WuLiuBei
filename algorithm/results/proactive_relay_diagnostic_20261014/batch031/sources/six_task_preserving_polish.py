"""Process-local weighted ALNS polish with six sorting plans pinned.

Production defaults/source are unchanged. The caller supplies its frozen,
weighted-transformed ALNS module. Existing relay timing remains active while
only the remaining whole DIRECT tasks may be reassigned or reordered.
All exact DIRECT choices
use the real global DAG; invalid complete candidates roll back to the checked
pre-destroy incumbent and cannot earn simulated-annealing/operator credit.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import asdict, is_dataclass, replace
from math import isclose, isfinite
from time import perf_counter
from types import ModuleType

from uav_dispatch.model import Score
from uav_dispatch.relay import GlobalRelayEvaluator, build_plan_index
from uav_dispatch.validation import evaluate_solution

try:
    import mobile_relay_refinement as mobile
except ImportError:
    from . import mobile_relay_refinement as mobile


_active = False
PATCH_NAMES = ("destroy_solution", "_all_plans_for_task", "_apply_task_exact", "_repair", "_accept_worse")
SHARED_DISTANCE_EPSILON_KM = 1e-10


def _task_id(problem, visit):
    leg = (problem.leg_registry or {}).get(abs(visit))
    return leg.task_id if leg is not None else abs(visit)


def _signature(problem, routes, pinned):
    index = build_plan_index(problem, routes)
    plans = tuple((t, index.task_plans[t].mode, index.task_plans[t].relay_id,
        tuple(index.task_plans[t].leg_ids), index.delivery_route_by_task[t],
        index.inbound_route_by_task.get(t), index.outbound_route_by_task.get(t)) for t in sorted(pinned))
    events = tuple(tuple(v for v in route if _task_id(problem, v) in pinned) for route in routes)
    counts = tuple(sum(owner == i for owner in index.delivery_route_by_task.values()) for i in range(len(routes)))
    return dict(plans=plans, events=events, final_counts=counts, relay_tasks=tuple(sorted(index.relay_task_ids)))


def _shape(problem, routes, required_cross):
    """Three sources/receivers, with free native interleaving around six pins."""
    index = build_plan_index(problem, routes)
    selected = tuple(sorted(required_cross))
    if not selected:
        if index.relay_task_ids:
            raise ValueError("DIRECT polish introduced a relay plan")
        return dict(required_cross_task_ids=[], station_id=None, source_aircraft=[],
                    receiver_aircraft=[], current_shared_double_loaded_km_by_aircraft={})
    if len(selected) != 6 or set(index.relay_task_ids) != set(selected):
        raise ValueError("six-task shape requires exactly its six relay plans")
    if any(index.inbound_route_by_task[t] == index.outbound_route_by_task[t] for t in selected):
        raise ValueError("a required task lost true cross-UAV identity")
    sources = {index.inbound_route_by_task[t] for t in selected}
    receivers = {index.outbound_route_by_task[t] for t in selected}
    stations = {index.task_plans[t].relay_id for t in selected}
    if len(sources) != 3 or sources != receivers or len(stations) != 1:
        raise ValueError("six-task shape requires the same three sources/receivers and one station")
    incoming = {t: next(l for l in index.task_plans[t].leg_ids
                       if problem.leg_registry[l].kind == "RELAY_IN") for t in selected}
    outgoing = {t: next(l for l in index.task_plans[t].leg_ids
                       if problem.leg_registry[l].kind == "RELAY_OUT") for t in selected}
    in_groups, out_groups, drops, takes, load_records, shared = {}, {}, {}, {}, {}, {}
    for aircraft in sorted(sources):
        own = [t for t in selected if index.inbound_route_by_task[t] == aircraft]
        received = [t for t in selected if index.outbound_route_by_task[t] == aircraft]
        if (len(own) != 2 or len(received) != 2
                or len({index.inbound_route_by_task[t] for t in received}) != 2
                or any(index.inbound_route_by_task[t] == aircraft for t in received)):
            raise ValueError("each receiver needs one parcel from each of the other two sources")
        route = routes[aircraft]
        drop_positions = [route.index(-incoming[t]) for t in own]
        take_positions = sorted(route.index(outgoing[t]) for t in received)
        if max(drop_positions) >= take_positions[0]:
            raise ValueError("all own IN drops must precede every own OUT take")
        if take_positions[1] != take_positions[0] + 1:
            raise ValueError("receiver OUT takes must remain consecutive")
        held, previous, distance, loads = set(), None, 0., []
        identifiers = {outgoing[t] for t in received}
        for position, visit in enumerate(route):
            loads.append(len(held))
            if identifiers <= held:
                distance += problem.distance(previous, visit,
                    from_node=problem.home_node(aircraft) if previous is None else None)
            if visit > 0:
                held.add(visit)
            else:
                held.remove(-visit)
            previous = visit
        if [loads[p] for p in take_positions] != [0, 1]:
            raise ValueError("receiver must enter its two OUT takes empty")
        if distance <= SHARED_DISTANCE_EPSILON_KM:
            raise ValueError("receiver lost positive-distance shared loaded flight")
        in_groups[str(aircraft)] = own
        out_groups[str(aircraft)] = received
        takes[str(aircraft)] = take_positions
        load_records[str(aircraft)] = [loads[p] for p in take_positions]
        shared[str(aircraft)] = distance
        for task in own:
            drops[str(task)] = [aircraft, route.index(-incoming[task])]
    return dict(required_cross_task_ids=list(selected), station_id=next(iter(stations)),
        source_aircraft=sorted(sources), receiver_aircraft=sorted(receivers),
        inbound_task_groups=in_groups, outbound_task_groups=out_groups,
        in_drop_positions=drops, receiver_take_positions=takes,
        receiver_load_before_pickups=load_records,
        current_shared_double_loaded_km_by_aircraft=shared, own_in_before_out=True)


def _validate(problem, routes, pinned, expected, required_cross):
    checked = evaluate_solution(problem, routes, _force_full=True)
    if not checked.valid:
        raise ValueError(f"invalid complete candidate: {checked.violations}")
    actual = _signature(problem, routes, pinned)
    if actual != expected:
        raise ValueError("pinned modes/legs/stations/owners/event order or final quotas changed")
    shape = _shape(problem, routes, required_cross)
    shape["pinned_task_ids"] = sorted(pinned)
    return checked, shape


def _snapshot(problem, routes, checked, weights, scales, *, phase, shape, evaluator=None):
    """Current full DAG evidence; no raw table score or repeated topology."""
    pins = shape.get("pinned_task_ids", [])
    if len(pins) != 6:
        raise ValueError("snapshot requires the six validated pinned task IDs")
    evaluator = evaluator if evaluator is not None else GlobalRelayEvaluator(problem)
    scheduled = evaluator.evaluate(routes)
    if (not scheduled.feasible or scheduled.score.late_count != checked.score.late_count
            or abs(scheduled.score.total_lateness_min-checked.score.total_lateness_min) > 1e-8
            or abs(scheduled.score.distance_km-checked.score.distance_km) > 1e-8):
        raise ValueError("current full snapshot clocks disagree with canonical checked score")
    index = build_plan_index(problem, routes)
    score = dict(late_count=checked.score.late_count, total_lateness_min=checked.score.total_lateness_min,
                 distance_km=checked.score.distance_km)
    drops = {task: scheduled.event_times_min[tuple(position)]
             for task, position in shape.get("in_drop_positions", {}).items()}
    shared = dict(shape["current_shared_double_loaded_km_by_aircraft"])
    if not index.relay_task_ids:
        owners = index.delivery_route_by_task
        for aircraft in sorted({owners[t] for t in pins}):
            pair = {t for t in pins if owners[t] == aircraft}
            if len(pair) != 2:
                raise ValueError("DIRECT six-pin map must preserve two selected deliveries per aircraft")
            held, previous, distance = set(), None, 0.
            for visit in routes[aircraft]:
                if pair <= held:
                    distance += problem.distance(previous, visit,
                        from_node=problem.home_node(aircraft) if previous is None else None)
                if visit > 0: held.add(visit)
                else: held.remove(-visit)
                previous = visit
            shared[str(aircraft)] = distance
    return dict(routes=[list(r) for r in routes], score=score, post_global_score=dict(score),
        key=list(mobile.weighted_key(checked.score, weights, scales)), valid=True, complete=True,
        phase=phase, pin_scope="six_task_sorting", pinned_task_ids=list(pins), pinned_shape=shape,
        cross_uav_count=sum(index.inbound_route_by_task[t] != index.outbound_route_by_task[t]
                            for t in index.relay_task_ids), relay_task_count=len(index.relay_task_ids),
        actual_final_owners={str(t): a for t, a in sorted(index.delivery_route_by_task.items())},
        event_times_min=[[scheduled.event_times_min[(a,p)] for p in range(len(r))] for a,r in enumerate(routes)],
        delivery_times_min={str(t): clock for t,clock in sorted(scheduled.delivery_times_min.items())},
        current_source_drop_availability_min=drops,
        current_shared_double_loaded_km_by_aircraft=shared,
        current_clock_source="current_full_GlobalRelayEvaluator")


def _arguments(args, kwargs, names):
    return {name: args[i] if i < len(args) else kwargs.get(name) for i, name in enumerate(names)}


@contextmanager
def pinned_search(alns_module, problem, initial_routes, *, deadline, weights, scales,
                  pinned_task_ids, required_cross_task_ids=(), statistics=None,
                  snapshot_limit=8):
    """Restore all five ALNS functions on normal exit, exceptions and timeout.

    This context patches only the supplied module inside one worker process.
    It forbids nested contexts. It exposes the most recent pre-deadline checked
    best macro even if ALNS's final reporting check completes after deadline.
    """
    global _active
    if _active:
        raise RuntimeError("nested pinned-search contexts are unsupported")
    weights, scales = tuple(weights), tuple(scales)
    if (not isfinite(deadline) or len(weights) != 3 or len(scales) != 3
        or any(not isfinite(v) or v <= 0 for v in (*weights, *scales))
        or not isclose(sum(weights), 1.)):
        raise ValueError("finite positive normalized weights/scales and deadline required")
    if type(snapshot_limit) is not int or snapshot_limit < 0:
        raise ValueError("nonnegative snapshot limit required")
    pinned, cross = frozenset(pinned_task_ids), frozenset(required_cross_task_ids)
    if (len(pinned) != 6 or (cross and cross != pinned)
        or any(type(t) is not int or t not in problem._task_by_id for t in (*pinned, *cross))):
        raise ValueError("exactly six complete sorting tasks must be pinned, with all-or-none cross IDs")
    initial = tuple(tuple(r) for r in initial_routes)
    if len(initial) != problem.drone_count:
        raise ValueError("exactly one route per UAV required")
    index = build_plan_index(problem, initial)
    if not pinned <= set(index.task_plans) or set(index.relay_task_ids) != cross:
        raise ValueError("all existing relay plans must be the required pinned cross tasks")
    expected = _signature(problem, initial, pinned)
    initial_checked, initial_shape = _validate(problem, initial, pinned, expected, cross)
    original = {name: getattr(alns_module, name) for name in PATCH_NAMES}
    clock_evaluator = GlobalRelayEvaluator(problem)
    stats = statistics if statistics is not None else {}
    stats.update(pinned_task_ids=sorted(pinned), required_cross_task_ids=sorted(cross),
        pin_signature=dict(plans=[list(p) for p in expected["plans"]],
            events=[list(r) for r in expected["events"]], final_counts=list(expected["final_counts"])),
        destroy_calls=0, selected_pins_filtered=0, destroy_topups=0, balanced_slot_replacements=0,
        skipped_no_balanced_destroy=0, direct_plan_calls=0, direct_exact_global_evaluations=0,
        repair_calls=0, complete_checks=1, complete_valid_candidates=0, rollback_candidates=0,
        rejected_acceptances=0, rejected_reasons={}, full_candidate_snapshots=[],
        snapshot_limit=snapshot_limit, first_complete_candidate=None,
        best_constructed_candidate=_snapshot(problem, initial, initial_checked, weights, scales,
            phase="initial_complete_macro", shape=initial_shape, evaluator=clock_evaluator), patches_restored=False)
    state = dict(clock_evaluator=clock_evaluator, before=initial, before_checked=initial_checked, rejected=False,
        checked_by_routes={initial: initial_checked}, best_routes=initial,
        best_checked=initial_checked, best_shape=initial_shape,
        best_key=mobile.weighted_key(initial_checked.score, weights, scales),
        best_checked_at_perf_counter=None)
    def check(local_deadline=None):
        end = deadline if local_deadline is None else min(deadline, local_deadline)
        if perf_counter() >= end:
            raise alns_module._SearchDeadlineReached
    def reject(reason):
        state["rejected"] = True
        stats["rollback_candidates"] += 1
        stats["rejected_reasons"][reason] = stats["rejected_reasons"].get(reason, 0) + 1
        return state["before"]

    def destroy(p, evaluator, routes, operator, count, rng, **kwargs):
        check()
        if p is not problem:
            raise ValueError("pinned search problem changed")
        current = tuple(tuple(r) for r in routes)
        state["rejected"] = False
        checked = state["checked_by_routes"].get(current)
        if checked is None:
            checked, _ = _validate(problem, current, pinned, expected, cross)
            stats["complete_checks"] += 1
            check()
        state["before"], state["before_checked"] = current, checked
        stats["destroy_calls"] += 1
        _, selected = original["destroy_solution"](p, evaluator, current, operator, count, rng, **kwargs)
        check()
        current_index = build_plan_index(problem, current)
        eligible = tuple(sorted(set(current_index.direct_task_ids) - pinned))
        target = min(max(1, count), len(eligible))
        selected = tuple(dict.fromkeys(selected))
        stats["selected_pins_filtered"] += sum(t in pinned for t in selected)
        removed = [t for t in selected if t in eligible][:target]
        pool = [t for t in eligible if t not in removed]
        rng.shuffle(pool)
        if len(removed) < target:
            need = target - len(removed)
            removed.extend(pool[:need])
            stats["destroy_topups"] += need
        owners = current_index.delivery_route_by_task
        if len(removed) >= 2 and len({owners[t] for t in removed}) < 2:
            other = [t for t in pool if t not in removed and owners[t] != owners[removed[0]]]
            if other:
                removed[-1] = other[0]
                stats["balanced_slot_replacements"] += 1
        if len(removed) < 2 or len({owners[t] for t in removed}) < 2:
            stats["skipped_no_balanced_destroy"] += 1
            state["rejected"] = True
            return current, ()
        check()
        return alns_module._remove_tasks(current, current_index, removed), tuple(removed)

    def plans(*args, **kwargs):
        values = _arguments(args, kwargs, ("problem", "evaluator", "routes", "task_id"))
        check(kwargs.get("deadline"))
        task = values["task_id"]
        if task in pinned:
            raise ValueError("pinned task reached ordinary repair")
        kwargs["probe_relay"] = False
        stats["direct_plan_calls"] += 1
        result = original["_all_plans_for_task"](*args, **kwargs)
        check(kwargs.get("deadline"))
        if any(option.mode != "DIRECT" for option in result):
            raise RuntimeError("ordinary polish generated a new relay plan")
        return result

    def exact(p, evaluator, global_evaluator, routes, options, *, relay_cfg,
              deadline=None, direct_exact_top_k=2):
        check(deadline)
        before = global_evaluator.evaluate(routes)
        check(deadline)
        if not before.feasible:
            return None, None, None
        best, best_score, seen = None, None, 0
        for option in options:
            if option.mode != "DIRECT":
                raise ValueError("only ordinary DIRECT exact options allowed")
            if option.task_id in pinned:
                raise ValueError("pinned task reached exact repair")
            if seen >= direct_exact_top_k:
                break
            seen += 1
            candidate = list(routes)
            candidate[option.direct_route_index] = option.direct_route
            candidate = tuple(candidate)
            proposed = global_evaluator.evaluate_delta(routes, before, candidate)
            stats["direct_exact_global_evaluations"] += 1
            relay_cfg.global_evaluation_count += 1
            if getattr(relay_cfg, "counters", None) is not None:
                relay_cfg.counters.repair_exact_evaluation_count += 1
            check(deadline)
            if not proposed.feasible:
                continue
            key = mobile.weighted_key(proposed.score, weights, scales)
            if best_score is None or key < mobile.weighted_key(best_score, weights, scales):
                best, best_score = candidate, proposed.score
        return best, best_score, "DIRECT" if best is not None else None

    def repair(*args, **kwargs):
        values = _arguments(args, kwargs, ("problem", "evaluator", "partial_routes", "removed_task_ids"))
        if values["problem"] is not problem:
            raise ValueError("pinned repair problem changed")
        removed = tuple(values["removed_task_ids"])
        if set(removed) & pinned:
            raise ValueError("pinned task reached destroy output")
        check(kwargs.get("deadline", args[7] if len(args) > 7 else None))
        stats["repair_calls"] += 1
        if state["rejected"] or not removed:
            state["rejected"] = True
            return state["before"]
        candidate = original["_repair"](*args, **kwargs)
        check(kwargs.get("deadline", args[7] if len(args) > 7 else None))
        candidate = tuple(tuple(r) for r in candidate)
        stats["complete_checks"] += 1
        try:
            checked, shape = _validate(problem, candidate, pinned, expected, cross)
        except (ValueError, KeyError):
            check()
            return reject("complete_or_pin_or_repooling_shape")
        check()
        checked_at = perf_counter()
        if checked_at >= deadline:
            raise alns_module._SearchDeadlineReached
        stats["complete_valid_candidates"] += 1
        state["checked_by_routes"][candidate] = checked
        # Keep only recent full candidates plus the checked current/best.
        if len(state["checked_by_routes"]) > 64:
            state["checked_by_routes"] = {candidate: checked, state["before"]: state["before_checked"],
                                          state["best_routes"]: state["best_checked"]}
        snapshot = _snapshot(problem, candidate, checked, weights, scales, phase="complete_pinned_repair", shape=shape, evaluator=clock_evaluator)
        check()
        if stats["first_complete_candidate"] is None:
            stats["first_complete_candidate"] = snapshot
        if len(stats["full_candidate_snapshots"]) < snapshot_limit:
            stats["full_candidate_snapshots"].append(snapshot)
        key = mobile.weighted_key(checked.score, weights, scales)
        if key < state["best_key"]:
            check()
            state.update(best_routes=candidate, best_checked=checked, best_shape=shape, best_key=key,
                         best_checked_at_perf_counter=checked_at)
            stats["best_constructed_candidate"] = snapshot
        return candidate

    def accept(*args, **kwargs):
        if state["rejected"]:
            state["rejected"] = False
            stats["rejected_acceptances"] += 1
            return False
        if perf_counter() >= deadline:
            return False
        return original["_accept_worse"](*args, **kwargs)

    replacements = dict(destroy_solution=destroy, _all_plans_for_task=plans,
                        _apply_task_exact=exact, _repair=repair, _accept_worse=accept)
    _active = True
    try:
        for name, replacement in replacements.items():
            setattr(alns_module, name, replacement)
        yield state
    finally:
        for name, function in original.items():
            setattr(alns_module, name, function)
        state["rejected"] = False
        stats["patches_restored"] = True
        _active = False


def polish_pinned_macro(problem, routes, *, deadline, weights, scales, seed,
                        pinned_task_ids, required_cross_task_ids=(), comparison_key,
                        proposal_margin=.015, config=None, alns_module,
                        snapshot_limit=8):
    """Polish a near-worse complete macro; report whether it beats comparison.

    ``comparison_key`` is the fixed original incumbent's physical weighted key.
    The margin only admits a proposal to polish; it never accepts a worse
    solution. The returned routes remain a checked six-task-pinned macro even when
    ``metadata.adoptable`` is false; its caller then retains the comparison
    incumbent. DIRECT controls pin the same six complete DIRECT tasks and use
    this identical helper/configuration/budget, with no required cross IDs.
    """
    started = perf_counter()
    weights, scales = tuple(weights), tuple(scales)
    if (not isfinite(deadline) or len(weights) != 3 or len(scales) != 3
        or any(not isfinite(v) or v <= 0 for v in (*weights, *scales))
        or not isclose(sum(weights), 1.)):
        raise ValueError("finite positive normalized weights/scales and deadline required")
    comparison_key = tuple(comparison_key)
    if (len(comparison_key) != 4 or any(not isfinite(v) for v in comparison_key)
        or not isfinite(proposal_margin) or proposal_margin < 0):
        raise ValueError("finite four-component comparison key and nonnegative margin required")
    adapter = getattr(alns_module, "_weighted_adapter", None)
    if adapter is None:
        raise ValueError("caller must supply the frozen weighted-transformed ALNS module")
    real_module = isinstance(alns_module, ModuleType)
    if real_module and any(getattr(alns_module, name).__globals__ is not alns_module.__dict__
                           for name in ("solve_alns", "_repair")):
        raise ValueError("solve/repair globals must be the same ALNS module receiving the patches")
    base = tuple(tuple(r) for r in routes)
    checked = evaluate_solution(problem, base, _force_full=True)
    initial_checked_at = perf_counter()
    if not checked.valid:
        raise ValueError(f"invalid initial macro: {checked.violations}")
    key = mobile.weighted_key(checked.score, weights, scales)
    stats = dict(method="Six-Task-Preserving-Ordinary-DIRECT-ALNS-Polish", seed=seed,
        weights=list(weights), scales=list(scales), proposal_margin=proposal_margin,
        comparison_key=list(comparison_key), initial_key=list(key), skipped_reason=None,
        accepted_improvements=0, adoptable=False, deadline_reached=False, patches_restored=True,
        new_checkpoints_require_pre_deadline=True, new_relay_generation=False,
        real_loop_globals_preflight_checked=real_module,
        initial_validation_finished_at_perf_counter=initial_checked_at,
        initial_validation_within_deadline=initial_checked_at < deadline,
        preserve_exact_final_counts=True, shared_distance_epsilon_km=SHARED_DISTANCE_EPSILON_KM)
    remaining = deadline - perf_counter()
    cfg = config if config is not None else alns_module.ALNSConfig()
    cfg = replace(cfg, seed=seed, time_limit_seconds=max(1e-12, remaining), relay_enabled=True,
        enable_relay_seed=False, enable_relay_refine=False, enable_relay_budget_rebalance=False,
        enable_terminal_polish=False, enable_terminal_task_exchange=False, enable_tail_exchange=False,
        enable_home_reassignment=False, enable_adjacent_polish=False, enable_home_seed=False,
        enable_home_bias=False, enable_boundary_destroy=False, enable_multiroute_string_destroy=False,
        enable_objective_rewards=False, enable_budget_cooling=False, enable_regret_portfolio=False,
        relay_large_destroy_interval=0)
    if not problem.has_relays:
        raise ValueError("DIRECT control must use the same fixed relay-network Problem as relay arm")
    stats["configuration"] = asdict(cfg) if is_dataclass(cfg) else dict(vars(cfg))
    stats["configuration"]["assigned_remaining_seconds"] = max(0., remaining)
    active_coefficients = tuple(w / s for w, s in zip(weights, scales))
    if adapter.active:
        if tuple(adapter.coefficients) != active_coefficients:
            raise ValueError("active weighted objective differs from requested physical coefficients")
        objective_context = nullcontext()
    else:
        objective_context = adapter.objective(getattr(alns_module, "Score", Score), scales, weights)
    best, best_checked, iterations = base, checked, 0
    with objective_context:
        with pinned_search(alns_module, problem, base, deadline=deadline, weights=weights, scales=scales,
            pinned_task_ids=pinned_task_ids, required_cross_task_ids=required_cross_task_ids,
            statistics=stats, snapshot_limit=snapshot_limit) as state:
            stats["input_snapshot"] = stats["best_constructed_candidate"]
            if remaining <= 0 or perf_counter() >= deadline:
                stats["skipped_reason"] = "expired_absolute_deadline"
            elif key[0] > comparison_key[0] + proposal_margin:
                stats["skipped_reason"] = "predeclared_weighted_proposal_margin"
            else:
                try:
                    result = alns_module.solve_alns(problem, config=cfg, initial_routes=base)
                    iterations = result.iterations
                    metadata = result.metadata if hasattr(result, "metadata") else {}
                    stats["alns_summary"] = dict(iterations=iterations,
                        accepted_solutions=metadata.get("accepted_solutions"),
                        operator_statistics={name: dict(metrics) for name, metrics in
                            metadata.get("operator_statistics", {}).items()})
                except alns_module._SearchDeadlineReached:
                    stats["skipped_reason"] = "search_reached_absolute_deadline"
            best, best_checked, key = state["best_routes"], state["best_checked"], state["best_key"]
            stats["post_pin_snapshot"] = _snapshot(problem, best, best_checked, weights, scales,
                phase="post_pinned_polish", shape=state["best_shape"], evaluator=state["clock_evaluator"])
            stats["best_checked_at_perf_counter"] = state["best_checked_at_perf_counter"]
    stats.update(final_key=list(key), adoptable=key < comparison_key,
        accepted_improvements=int(key < tuple(stats["initial_key"])),
        returned_initial_checkpoint=best == base,
        returned_checkpoint_kind="initial_macro_no_search_change" if best == base else "new_predeadline_full_candidate",
        returned_checkpoint_checked_within_deadline=(initial_checked_at < deadline if best == base
            else stats["best_checked_at_perf_counter"] is not None and stats["best_checked_at_perf_counter"] < deadline),
        deadline_reached=perf_counter() >= deadline, runtime_seconds=perf_counter() - started)
    return dict(problem=problem, routes=best, evaluation=best_checked,
                iterations=iterations, metadata=stats)
