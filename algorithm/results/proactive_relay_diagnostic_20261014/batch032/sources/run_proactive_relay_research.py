"""Bounded research on batch/reciprocal cross-UAV relay neighborhoods.

Each batch is frozen before execution; no rerun or overwriting a cell.
Diagnostic batches may start at a historical DIRECT checkpoint and therefore
are explicitly NOT equal-budget performance comparisons. Fresh screen batches
use the same construction, homes, weights and total budget in every arm.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from time import perf_counter
from types import MappingProxyType
from zipfile import ZipFile

ROOT = Path(os.environ.get("JD_RELAY_WORKSPACE_ROOT", str(Path(__file__).resolve().parents[2]))).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PARENT = ROOT / "algorithm/results/weighted_relay_ablation_20261014"
DEFAULT = ROOT / "algorithm/results/proactive_relay_screen_20261014/batch001"
EXPERIMENTS = ROOT / "algorithm/experiments"
FIELDS = ("late_count", "total_lateness_min", "distance_km")
METHODS = {"batch": "batch_relay_refinement", "reciprocal": "reciprocal_relay_refinement",
           "tail_direct": "relay_tail_crossover", "tail_relay": "relay_tail_crossover",
           "construct_direct": "relay_first_constructor", "construct_relay": "relay_first_constructor",
           "inflight": "inflight_relay_swap", "bundle": "inflight_relay_bundle",
           "cluster_direct": "relay_cluster_beam", "cluster_relay": "relay_cluster_beam",
           "mobile": "mobile_relay_refinement", "mobile_v2": "mobile_relay_refinement_v2",
           "mobile_v3": "mobile_relay_refinement_v3", "directed": "directed_bundle_insertion",
           "delivery_direct": "mobile_delivery_reconstruction", "delivery_relay": "mobile_delivery_reconstruction",
           "anticipatory": "anticipatory_mobile_handoff",
           "quota_direct": "quota_balanced_relay_exchange", "quota_relay": "quota_balanced_relay_exchange",
           "repool_direct": "relay_repooling_exchange", "repool_relay": "relay_repooling_exchange",
           "repool_polish_direct": "repooling_with_pinned_polish", "repool_polish_relay": "repooling_with_pinned_polish",
           "mobile_repool_direct": "mobile_repooling_refinement", "mobile_repool_relay": "mobile_repooling_refinement",
           "joint_mobile_direct": "joint_mobile_repooling", "joint_mobile_relay": "joint_mobile_repooling",
           "frontier_mobile_direct": "source_frontier_mobile_repooling", "frontier_mobile_relay": "source_frontier_mobile_repooling",
           "bundle_cached_direct": "receiver_bundle_cached_mobile", "bundle_cached_relay": "receiver_bundle_cached_mobile",
           "full_credit_direct": "full_score_selected_mobile", "full_credit_relay": "full_score_selected_mobile",
           "full_credit_pin_direct": "full_credit_pinned_repooling", "full_credit_pin_relay": "full_credit_pinned_repooling",
           "cold_pin_direct": "cold_weighted_pinned_repooling", "cold_pin_relay": "cold_weighted_pinned_repooling",
           "pair_pin_direct": "pair_pinned_repooling", "pair_pin_relay": "pair_pinned_repooling",
           "six_sort_direct": "six_task_sorting", "six_sort_relay": "six_task_sorting",
           "wide_six_direct": "expanded_six_task_sorting", "wide_six_relay": "expanded_six_task_sorting",
           "six_pin_direct": "six_task_with_pinned_polish", "six_pin_relay": "six_task_with_pinned_polish",
           "window_direct": "deadline_window_repooling", "window_relay": "deadline_window_repooling",
           "damage_direct": "window_damage_repair", "damage_relay": "window_damage_repair",
           "split_feeder_direct": "split_feeder_refinement", "split_feeder_relay": "split_feeder_refinement",
           "native_price_direct": "split_feeder_refinement_v2", "native_price_relay": "split_feeder_refinement_v2"}
DIRECT_ARMS = ("direct", "tail_direct", "construct_direct", "cluster_direct", "delivery_direct", "quota_direct", "repool_direct", "repool_polish_direct", "mobile_repool_direct", "joint_mobile_direct", "frontier_mobile_direct", "bundle_cached_direct", "full_credit_direct", "full_credit_pin_direct", "cold_pin_direct", "pair_pin_direct", "six_sort_direct", "wide_six_direct", "six_pin_direct", "window_direct")
WINDOW_PAIR = ("window_direct", "window_relay")
DAMAGE_PAIR = ("damage_direct", "damage_relay")
DIRECT_ARMS = (*DIRECT_ARMS, "damage_direct")
SPLIT_FEEDER_PAIR = ("split_feeder_direct", "split_feeder_relay")
DIRECT_ARMS = (*DIRECT_ARMS, "split_feeder_direct")
NATIVE_PRICE_PAIR = ("native_price_direct", "native_price_relay")
DIRECT_ARMS = (*DIRECT_ARMS, "native_price_direct")
SIX_SORT_PAIR = ("six_sort_direct", "six_sort_relay")
WIDE_SIX_PAIR = ("wide_six_direct", "wide_six_relay")
SIX_PIN_PAIR = ("six_pin_direct", "six_pin_relay")
ALL_SIX_ARMS = (*SIX_SORT_PAIR, *WIDE_SIX_PAIR, *SIX_PIN_PAIR)
COMPACT_SIX_ARMS = (*WIDE_SIX_PAIR, *SIX_PIN_PAIR)
REPOOL_ARMS = ("repool_direct", "repool_relay")
PIN_REPOOL_ARMS = ("repool_polish_direct", "repool_polish_relay")
PIN_FULL_CREDIT_PAIR = ("full_credit_pin_direct", "full_credit_pin_relay")
COLD_PIN_PAIR = ("cold_pin_direct", "cold_pin_relay")
PAIR_PIN_PAIR = ("pair_pin_direct", "pair_pin_relay")
COLD_PROFILE_ARMS = (*COLD_PIN_PAIR, *PAIR_PIN_PAIR)
COLD_CONFIG_ARMS = (*COLD_PROFILE_ARMS, *SIX_PIN_PAIR)
FOUR_PIN_ARMS = (*PIN_REPOOL_ARMS, *PIN_FULL_CREDIT_PAIR, *COLD_PIN_PAIR)
FULL_CREDIT_PIN_ARMS = (*PIN_FULL_CREDIT_PAIR, *COLD_PROFILE_ARMS)
ALL_PIN_ARMS = (*FOUR_PIN_ARMS, *PAIR_PIN_PAIR)
MOBILE_REPOOL_PAIR = ("mobile_repool_direct", "mobile_repool_relay")
JOINT_MOBILE_PAIR = ("joint_mobile_direct", "joint_mobile_relay")
FRONTIER_MOBILE_PAIR = ("frontier_mobile_direct", "frontier_mobile_relay")
BUNDLE_CACHED_PAIR = ("bundle_cached_direct", "bundle_cached_relay")
FULL_CREDIT_PAIR = ("full_credit_direct", "full_credit_relay")
THREE_PARTY_MOBILE_ARMS = ("mobile_repool_relay", "joint_mobile_relay", "frontier_mobile_relay", "bundle_cached_relay", "full_credit_relay")
ALL_REPOOL_ARMS = (*REPOOL_ARMS, *ALL_PIN_ARMS, *MOBILE_REPOOL_PAIR, *JOINT_MOBILE_PAIR, *FRONTIER_MOBILE_PAIR, *BUNDLE_CACHED_PAIR, *FULL_CREDIT_PAIR)
ANTICIPATORY_ARMS = ("anticipatory",)
MOBILE_ARMS = ("mobile", "mobile_v2", "mobile_v3", "delivery_relay", *ANTICIPATORY_ARMS, *THREE_PARTY_MOBILE_ARMS)
EXECUTION = (
    "run_proactive_relay_research.py", "batch_relay_refinement.py", "reciprocal_relay_refinement.py",
    "relay_tail_crossover.py",
    "relay_first_constructor.py",
    "inflight_relay_swap.py",
    "inflight_relay_bundle.py",
    "relay_cluster_beam.py",
    "mobile_relay_refinement.py", "audit_mobile_relay.py",
    "mobile_relay_refinement_v2.py",
    "mobile_relay_refinement_v3.py", "mobile_geometry_bound.py", "mobile_deadline_geometry.py",
    "mobile_delivery_reconstruction.py",
    "anticipatory_mobile_handoff.py", "audit_anticipatory_mobile.py",
    "quota_balanced_relay_exchange.py",
    "relay_repooling_exchange.py", "audit_repooling.py",
    "repooling_with_pinned_polish.py", "relay_preserving_polish.py", "audit_pinned_repooling.py",
    "mobile_repooling_refinement.py", "mobile_repooling_geometry.py", "audit_mobile_repooling.py",
    "joint_mobile_layouts.py", "joint_mobile_repooling.py",
    "source_frontier_cache.py", "source_frontier_mobile_repooling.py",
    "receiver_bundle_cache.py", "receiver_bundle_cached_mobile.py",
    "full_score_repool_selector.py", "full_score_selected_mobile.py",
    "full_credit_pinned_repooling.py",
    "cold_weighted_pinned_repooling.py",
    "relay_pair_preserving_polish.py", "pair_pinned_repooling.py", "audit_pair_pinned_repooling.py",
    "six_task_route_costs.py", "six_task_sorting.py", "audit_six_task_sorting.py",
    "expanded_six_task_sorting.py",
    "six_task_preserving_polish.py", "six_task_with_pinned_polish.py", "audit_six_task_pinned_polish.py",
    "deadline_window_costs.py", "deadline_window_repooling.py", "audit_deadline_window_repooling.py",
    "relay_damage_closure.py", "damage_closure_repair.py", "window_damage_repair.py",
    "audit_window_damage_repair.py", "audit_window_damage_batch.py",
    "split_feeder_catalogue.py", "split_feeder_refinement.py", "audit_split_feeder.py", "audit_split_feeder_batch.py",
    "exact_suffix_delay.py", "split_feeder_catalogue_v2.py", "split_feeder_refinement_v2.py",
    "audit_split_feeder_v2.py", "audit_split_feeder_batch_v2.py",
    "directed_bundle_insertion.py",
    "run_weighted_relay_ablation.py", "run_weighted_direct_pair.py", "weighted_direct_objective.py",
    "weighted_relay_objective.py", "weighted_periodic_exchange.py", "run_scenario_comparison.py",
    "audit_weighted_relay_ablation.py", "audit_weighted_direct_pair.py",
)


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def sha(path):
    import hashlib
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(value):
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def key(score, manifest):
    values = [getattr(score, field) for field in FIELDS]
    return (sum(w * v / s for w, v, s in zip(manifest["weights"], values, manifest["scales"])), *values)


def model_topology(problem, template):
    """Freeze candidate geometry; physical homes never follow a moved site."""
    return {**template,
            "stations": [[s.id, s.x, s.y] for s in problem.relay_stations],
            "legs": [asdict(v) for _, v in sorted((problem.leg_registry or {}).items())],
            "task_relay_candidates": {str(t): list(v) for t, v in problem.task_relay_candidates.items()},
            "home_points": [[p.x, p.y] for p in problem.home_points()]}


def unchanged_physics(before, after):
    assert before.tasks == after.tasks
    for field in ("drone_count", "drone_homes", "max_tasks_per_drone", "capacity", "speed_km_per_min", "depot", "max_relay_hops"):
        assert getattr(before, field) == getattr(after, field), "Changed physical resource: " + field
    assert before.home_points() == after.home_points(), "Rendezvous optimization changed starting positions"
    old = {s.id: (s.x, s.y) for s in before.relay_stations}
    new = {s.id: (s.x, s.y) for s in after.relay_stations}
    assert all(new.get(s) == xy for s, xy in old.items()), "Existing station or home was moved"


def build_case(tasks, homes):
    # Preserve exactly the previously frozen four sites and deployment policy.
    # Only the algorithmic station shortlist expands: all four existing sites
    # get legs for all tasks, with no artificial route-event cap.
    from run_scenario_comparison import build_scenario_problem
    from uav_dispatch.model import TransportLeg
    network = dict(relay_count=4, relay_candidates_per_task=2, relay_detour_ratio=2.,
                   relay_location_seed=42, relay_location_method="weighted_kmedoids")
    full, plan = build_scenario_problem(tasks, dict(relay=True, drone_homes=homes),
                                      drones=8, max_tasks=25, **network)
    plain = replace(full, leg_registry={}, task_relay_candidates={})
    topology = dict(stations=[[s.id, s.x, s.y] for s in full.relay_stations],
                    home_points=[[p.x, p.y] for p in full.home_points()],
                    drone_homes=list(full.drone_homes or (None,) * 8),
                    drone_count=8, max_tasks_per_drone=25, capacity=2, speed_km_per_min=.9,
                    relay_allowed=True, network_parameters=network, deployment=asdict(plan) if plan else None)
    legs, candidates, counter = {}, {}, max(t.id for t in tasks) + 1
    for task in sorted(tasks, key=lambda t: t.id):
        candidates[task.id] = tuple(s.id for s in full.relay_stations)
        for station in full.relay_stations:
            legs[counter] = TransportLeg(counter, task.id, "RELAY_IN", station.id)
            legs[counter + 1] = TransportLeg(counter + 1, task.id, "RELAY_OUT", station.id)
            counter += 2
    full = replace(full, leg_registry=legs, task_relay_candidates=candidates)
    topology = {**topology, "legs": [asdict(v) for _, v in sorted(legs.items())],
                "task_relay_candidates": {str(t): list(v) for t, v in candidates.items()},
                "network_parameters": {**topology["network_parameters"], "relay_candidates_per_task": 4,
                                       "relay_detour_ratio": None, "candidate_policy": "all existing four sites"},
                "research_station_shortlist": "all four existing fixed stations; no event cap"}
    return full, plain, topology


def prepare(args):
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a new empty batch directory; never replace an existing experiment")
    sys.path.insert(0, str(ROOT / "algorithm/src"))
    from uav_dispatch.alns import construct_regret_initial
    from uav_dispatch.io import load_tasks_csv
    from uav_dispatch.validation import evaluate_solution
    parent = read(PARENT / "manifest.json")
    audit = read(PARENT / "audit.json")
    assert audit["passed"] and audit["manifest_sha256"] == sha(PARENT / "manifest.json")
    assert audit["runs_sha256"] == sha(PARENT / "runs.json")
    tasks = load_tasks_csv(ROOT / parent["input_path"])
    full, plain, topology = build_case(tasks, args.homes)
    initial = construct_regret_initial(plain, candidate_limit=48,
                                      home_seed=args.homes == "stations", home_bias=args.homes == "stations")
    assert digest(initial.routes) == parent["scenarios"]["direct_stations" if args.homes == "stations" else "pure_direct"]["initial_routes_sha256"]
    checkpoint = None
    if args.checkpoint:
        path = args.checkpoint.resolve()
        saved = read(path)
        routes = tuple(tuple(r["encoded_visits"]) for r in saved["routes"])
        checked = evaluate_solution(plain, routes, _force_full=True)
        assert checked.valid and asdict(checked.score) == saved["score"]
        assert saved["problem"]["drone_homes"] == ["origin" if h is None else f"station_{h}" for h in topology["drone_homes"]]
        initial = replace(initial, routes=routes, evaluation=checked)
        checkpoint = dict(path=str(path.relative_to(ROOT)), sha256=sha(path),
                          boundary="Historical solved checkpoint + extra research budget; not an equal-budget DIRECT comparison")
    sources = output / "sources"
    sources.mkdir(parents=True)
    archive = sources / "weighted_solver.zip"
    parent_archive = PARENT / parent["source_archive"]["path"]
    assert sha(parent_archive) == parent["source_archive"]["sha256"]
    shutil.copyfile(parent_archive, archive)
    execution = {}
    for name in EXECUTION:
        source = EXPERIMENTS / name
        assert source.is_file(), "Candidate must be implemented before freezing: " + name
        copy = sources / name
        shutil.copyfile(source, copy)
        execution[name] = dict(path=str(copy.relative_to(output)), sha256=sha(copy))
    arms = args.arms.split(",")
    assert len(set(arms)) == len(arms) and set(arms) <= {"direct", *METHODS, "combined"}
    parameters = args.method_parameters
    assert isinstance(parameters, dict) and set(parameters) <= set(arms)
    import inspect
    reserved = {"problem", "routes", "deadline", "weights", "scales", "seed", "enable_transfers", "relay_enabled"}
    for method, values in parameters.items():
        assert method in METHODS and isinstance(values, dict) and not (set(values) & reserved)
        signature = inspect.signature(importlib.import_module(METHODS[method]).refine)
        assert set(values) <= set(signature.parameters), "Unknown frozen method parameter"
    assert args.budget_seconds > 1 and 0 < args.warmup_fraction < 1
    schedule = [dict(arm=arm, seed=args.seed, run_id=f"{arm}__seed_{args.seed}") for arm in arms]
    manifest = dict(kind="proactive_relay_research", schema_version=1,
        purpose="checkpoint_mechanism_diagnostic" if checkpoint else "fresh_equal_budget_screen",
        prepared_at=datetime.now(timezone.utc).isoformat(), parent_manifest_sha256=sha(PARENT / "manifest.json"),
        parent_audit_sha256=sha(PARENT / "audit.json"), parent_source_archive_sha256=sha(parent_archive),
        execution_worktree_base_commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
        inherited_solver_source_commit=parent["source_commit"],
        source_archive=dict(path="sources/weighted_solver.zip", sha256=sha(archive)), execution_sources=execution,
        input_path=parent["input_path"], input_sha256=parent["input_sha256"],
        problem_path=parent["problem_path"], problem_sha256=parent["problem_sha256"],
        homes=args.homes, topology=topology, topology_sha256=digest(topology),
        weights=parent["weights"], scales=parent["scales"], formula=parent["formula"],
        budget_seconds=args.budget_seconds, finalization_reserve_seconds=1., warmup_fraction=args.warmup_fraction,
        checkpoint=checkpoint, initial_routes=[list(r) for r in initial.routes], initial_routes_sha256=digest(initial.routes),
        initial_score=asdict(initial.evaluation.score), schedule=schedule,
        periodic_parameters=dict(interval=120, max_candidates=16, anchors_per_route=4, pair_budget_seconds=.008),
        method_parameters=parameters,
        protocol="Official full 200 tasks, 8 UAVs, C2, final K25, equal fixed homes; frozen weighted preference. Every empty flight and global transfer wait counted. No additional orders/resources/free repositioning. Station coordinates and home allocation unchanged, all four existing sites registered. Only complete checked macro plans adopted. Sequential single-seed exploratory batch; no significance claim or seed replacement.",
        timing_boundary="CSV/network/construction, all optimizer phases, validation and saved solutions/CF included, ending after CF write. Python imports and IPC response/parent-ledger writes excluded.")
    if set(arms) & set(MOBILE_ARMS):
        manifest["model_extension"] = "Optimized synchronous mobile rendezvous, separate from the fixed-four-site cache model"
        manifest["protocol"] = (
            "Official full 200 tasks, 8 UAVs, C2, final K25, same fixed physical homes, weights, scales and deadlines. "
            "New rendezvous coordinates are selected during search; the declared mobile profile specifies its aircraft and contact timing. "
            "Reciprocal arms exchange equal-sized onboard cargo groups only when both aircraft are present; early aircraft waits holding cargo. "
            "Every flight and synchronization wait is charged; no mobile ground cache, new vehicle or free initial relocation. "
            "This changes the prior fixed-four-site relay extension and is labelled separately. "
            "Sequential source-bound exploratory batch, no significance claim or seed replacement.")
        if set(arms) & set(ANTICIPATORY_ARMS):
            manifest["protocol"] += (
                " Anticipatory arms instead transfer one or two parcels in one direction: the empty receiver "
                "must arrive before the sender and wait until contact. The sender retains cargo until contact; "
                "all receiver travel and waiting are charged. This conservative branch starts from pure DIRECT "
                "routes, preserves final owners, and allows at most one new meeting per call. "
                "It is independently audited by the separate receiver-first physical auditor.")
        if set(arms) & set(THREE_PARTY_MOBILE_ARMS):
            contract = ROOT / "algorithm/results/proactive_relay_diagnostic_20261014/mobile_repooling_model_contract_v1.md"
            manifest["mobile_repooling_contract"] = dict(path=str(contract.relative_to(ROOT)), sha256=sha(contract))
            manifest["protocol"] += (
                " Three-party repooling transfers one parcel from each of two different sources to a third aircraft at one "
                "new coordinate. The recipient arrives empty before both sources, takes parcels in actual source-arrival order, "
                "waits first empty and then holding one parcel, and carries both over a positive distance. Each contact has "
                "drop equal to take and zero mobile cache; each source leaves after its own contact. "
                "The original four-site cache network remains an explicitly separate valid baseline; winning trial geometry "
                "adds only one rendezvous and its four used legs, without moving any physical home or existing site.")
    if any(arm.startswith("quota_") for arm in arms):
        manifest["protocol"] += (
            " Quota arms trade two whole delivery responsibilities between two aircraft. "
            "The relay arm may carry one request through an existing station while the compensating request "
            "travels directly; the matched DIRECT arm disables only that relay option. "
            "Both use the same declared pair selector, insertion bounds and rebuilding orders.")
    if set(arms) & set(ALL_REPOOL_ARMS):
        manifest["protocol"] += (
            " Repooling arms assign two requests from different source aircraft to one third delivery aircraft; "
            "two whole DIRECT requests compensate its delivery quotas on the two source aircraft. "
            "The relay arm requires both original sources to use the same existing station and the third aircraft "
            "to take both parcels consecutively while empty, then carry them together for a positive flight distance. "
            "The DIRECT arm collects both complete requests itself. The final responsibility map, selected macros, "
            "insertion bounds and rebuilding pipelines are matched; all other task ownership and event order stay fixed. "
            "Only complete four-task plans are compared to the checked incumbent, and the physical macro is independently replayed.")
    if set(arms) & set(FOUR_PIN_ARMS):
        manifest["protocol"] += (
            " Pinned-polish variants then allow other DIRECT tasks to change ownership and route order while preserving "
            "the four complete macro identities, exact transport modes, aircraft roles, station, pin-event subsequences "
            "and per-aircraft final-delivery counts. True relay identities cannot be cancelled or replaced by ordinary DIRECT gain. "
            "The predeclared margin only admits a complete proposal for attempted polish; final adoption still requires "
            "a strict pre-deadline improvement over the original checked input. Each arm ranks its own bounded complete "
            "proposals by the same rule, so the chosen best macro identities are not necessarily paired across arms. "
            "All proposal generation, polish, full checks and leaf guard reserves share the same total wall budget. "
            "Both original macros and all reported full polish checkpoints are independently audited with distinct shape rules.")
    if set(arms) & {*JOINT_MOBILE_PAIR, *FRONTIER_MOBILE_PAIR, *BUNDLE_CACHED_PAIR, *FULL_CREDIT_PAIR}:
        manifest["protocol"] += (
            " Joint-mobile variants preserve physical best representatives for the four existing station families, "
            "and use additional beam slots for receiver-first mobile geometry and earlier contact cuts. "
            "Same-taskset partial geometry bounds rank layouts only, without replacing real fixed-network scores "
            "or proving a complete incumbent inferior. Actual continuous contact timing and all full adoption guards "
            "remain under the source-bound mobile model contract. DIRECT uses the same macro selector and declared "
            "finite limits, with mobile-specific ranking disabled.")
    if set(arms) & {*FRONTIER_MOBILE_PAIR, *BUNDLE_CACHED_PAIR, *FULL_CREDIT_PAIR, *FULL_CREDIT_PIN_ARMS}:
        manifest["protocol"] += (
            " Source-frontier variants additionally retain bounded early source-IN contact and pickup-frontier "
            "representatives with unchanged true local deltas. Exact repeated partial and receiver evaluations "
            "may be memoized under complete input identity keys with declared memory limits. "
            "Early-source selection only ranks candidates; no missing-compensation geometry bound is used "
            "to prune against the complete incumbent. All added work shares the same total deadline.")
    if set(arms) & {*BUNDLE_CACHED_PAIR, *FULL_CREDIT_PAIR, *FULL_CREDIT_PIN_ARMS}:
        manifest["protocol"] += (
            " Bundle-cache variants memoize entire identical pure-C receiver candidate enumerations before "
            "their expensive generation loops, retaining exact candidate routes and order. Keys bind all "
            "actual route, function, object and ranking inputs; local deadline checks precede cache hits. "
            "The same bounded computational cache is enabled in DIRECT and relay arms, with no physical cache "
            "or model change and no added partial-incumbent pruning.")
    if set(arms) & {*FULL_CREDIT_PAIR, *FULL_CREDIT_PIN_ARMS}:
        review = ROOT / "algorithm/results/proactive_relay_diagnostic_20261014/repooling_compensation_score_review_v1.md"
        manifest["compensation_selector_review"] = dict(path=str(review.relative_to(ROOT)), sha256=sha(review))
        manifest["protocol"] += (
            " Full-credit variants change only shared macro selection: each compensation pool includes "
            "bounded geometry and actual full C-removal N/L/D credit representatives. All distinct directed "
            "pool pairs use two actual source insertion deltas plus one actual joint C-remove2 score delta, "
            "replacing the two single-removal deltas. These values rank candidates only, without predicting "
            "complete macro benefit or pruning. Both paired selectors must complete and yield identical "
            "group records and SHA; interrupted selectors are preserved as incomplete, never called matched performance.")
    if set(arms) & set(FULL_CREDIT_PIN_ARMS):
        manifest["protocol"] += (
            " The full-credit pinned variant uses only the original four fixed cache stations and returns no "
            "new Problem or optimized rendezvous coordinate. Joint mobile lower bounds rank extra raw layout "
            "slots only; every actual proposal and polish checkpoint retains fixed-station physical scores. "
            "The new shared selector and source/cache construction precede the frozen weighted pin-preserving "
            "ALNS helper. Each arm chooses its own best bounded complete macros, without claiming identical "
            "chosen macro identities across arms. Raw, post-polish and all reported helper full snapshots are audited.")
    if set(arms) & set(COLD_CONFIG_ARMS):
        cold_parameters = {"polish_initial_temperature": "initial_temperature",
                           "polish_minimum_temperature": "minimum_temperature",
                           "polish_cooling_rate": "cooling_rate",
                           "polish_max_iterations": "max_iterations"}
        profiles = {}
        for arm in set(arms) & set(COLD_CONFIG_ARMS):
            signature = inspect.signature(importlib.import_module(METHODS[arm]).refine)
            profiles[arm] = {config_name: parameters.get(arm, {}).get(name, signature.parameters[name].default)
                             for name, config_name in cold_parameters.items()}
        assert len({digest(profile) for profile in profiles.values()}) == 1, "Cold paired arms must share all four temperature settings"
        note = ROOT / "algorithm/results/proactive_relay_diagnostic_20261014/batch023_weighted_temperature_diagnosis_v1.md"
        manifest["cold_polish_profiles"] = profiles
        manifest["temperature_diagnosis"] = dict(path=str(note.relative_to(ROOT)), sha256=sha(note))
        manifest["protocol"] += (
            " Cold-pin variants change only the actual worker ALNS temperature profile and iteration ceiling. "
            "The weighted-J temperature settings are frozen identically across arms, per-iteration cooling "
            "remains enabled with budget cooling disabled, and the same absolute and leaf deadlines still apply. "
            "The high iteration ceiling adds no wall budget. Every reported helper configuration must match "
            "the declared four settings; any benefit remains an exploratory hypothesis until independently checked.")
    if set(arms) & set(PAIR_PIN_PAIR):
        manifest["protocol"] += (
            " Pair-pin variants first construct and independently validate the same complete four-task raw macros. "
            "Ordinary polish then pins only the two selected i/j transport identities, roles, stations, legs and "
            "signed-event subsequences. The two originally compensating DIRECT tasks join the other 196 ordinary "
            "DIRECT tasks in repair; each aircraft still has exactly its original final-delivery quota. "
            "Relay retains exactly the original two true cross-aircraft transfers, empty consecutive OUT pickups "
            "and positive-distance co-loaded flight. DIRECT uses the same two-task pin policy on its own chosen i/j assignments. "
            "Each arm ranks its own best bounded raw macros, which need not have paired identities. "
            "The old raw four-task responsibility map is historical; actual post-polish owners are stored separately. "
            "Raw proposals and post-polish plans use distinct independent audit rules; no synthetic base is used. "
            "Two additional facade aliases and all existing construction/ALNS/weighted hooks must be restored. "
            "All search, full checks and guarded adoption share the unchanged total and leaf deadlines.")
    if set(arms) & set(ALL_SIX_ARMS):
        note = ROOT / "papers/notes/proactive_relay_20261014/six_task_sorting_macro_design_v1.md"
        manifest["six_task_sorting_design"] = dict(path=str(note.relative_to(ROOT)), sha256=sha(note))
        manifest["protocol"] += (
            " Six-sort variants use the original four fixed cache stations, pure-DIRECT input and three aircraft "
            "with two original requests each. All other task ownership and signed-event subsequences stay fixed. "
            "Relay collects each original source pair, completes both own IN drops before any own OUT pickup, "
            "then receives one parcel from each of the other two sources and carries the new pair together. "
            "All six requests retain one true cross-aircraft transfer at the same existing station; every aircraft "
            "keeps its original final-delivery count. Acyclic IN-before-ownOUT timing makes the finite route tables "
            "separable only under that declared restriction. All cache waits, actual homes and native downstream "
            "delay are counted. DIRECT independently allocates the same six whole requests, two per aircraft, "
            "through its finite pair tables and bitmask assignment, without inheriting relay-selected final owners. "
            "Selectors must finish and match records/SHA. Limits, all table construction and full checks share "
            "the same absolute total deadline; internal candidate counts may differ. A complete original-input "
            "improvement is adopted only after a full pre-deadline canonical check, and all reported complete "
            "proposals undergo independent physical/source replay. The analytic example is not official evidence.")
    if set(arms) & set(WIDE_SIX_PAIR):
        manifest["protocol"] += (
            " Wide-six variants expand only the shared family selector: retain the declared best bounded "
            "families per existing station under the unchanged source-pair pool and proxy ranking, then "
            "interleave equal ranks across stations in round-robin order. The public max_stations becomes "
            "the old frozen constructor max_families argument; actual selected families are bounded by "
            "max_stations times families_per_station. All original route tables, finite allocation rules, "
            "physical validation and pre-deadline full-score adoption guards remain unchanged. The one "
            "process-local selector alias must remain intact during the call and be restored afterwards. "
            "All added ranking, sorting, hashing and construction share the unchanged total deadline.")
    if set(arms) & set(SIX_PIN_PAIR):
        note = ROOT / "papers/notes/proactive_relay_20261014/six_task_pinned_polish_design_v1.md"
        manifest["six_task_pinned_polish_design"] = dict(path=str(note.relative_to(ROOT)), sha256=sha(note))
        manifest["protocol"] += (
            " Six-pin variants first run the frozen expanded six-task raw constructor within the declared "
            "fraction of the same total budget. They then polish their own best bounded complete raw macros, "
            "preserving six complete plans, legs, roles, station, selected signed-event subsequences and "
            "all aircraft final-delivery counts. Other DIRECT tasks may change owners and route order. "
            "Relay retains exactly the six original true transfers, all own IN drops before own OUT pickups, "
            "and three empty consecutive double-OUT takes with positive joint flight. Native tasks may "
            "interleave within IN events or OUT deliveries when full capacity and timing remain valid. "
            "DIRECT uses the same six-pin policy on its own chosen assignments; selected polished macro "
            "identities need not match across arms. Raw source proof and post-polish rules are independently "
            "audited separately. Every post clock, availability, owner and global score is recomputed; raw "
            "finite table scores are historical, never post facts. All helper and facade checkpoints require "
            "complete pre-deadline checks and strict improvement over the original input before adoption. "
            "Raw selector, actual weighted ALNS hooks and objective state must be restored after use.")
    if set(arms) & {*WINDOW_PAIR, *DAMAGE_PAIR}:
        note = ROOT / "papers/notes/proactive_relay_20261014/deadline_rescue_window_design_v1.md"
        review = ROOT / "papers/notes/proactive_relay_20261014/fixed_h_rescue_selector_review_v1.md"
        manifest["deadline_rescue_window_design"] = dict(path=str(note.relative_to(ROOT)), sha256=sha(note))
        manifest["fixed_h_rescue_selector_review"] = dict(path=str(review.relative_to(ROOT)), sha256=sha(review))
        manifest["protocol"] += (
            " Fixed-window variants generate shared station/source/receiver deadline opportunities before "
            "the former destination-nearest-three and receiver-affinity cutoffs. Source-IN availability "
            "and physical receiver empty-gap arrival enter the same conditional second-take window. "
            "Finite source catalogues retain true weighted, earliest and deadline-critical IN options; "
            "joint latest-drop bounds are rebuilt after real compensation insertion. All receiver empty "
            "gaps and both delivery orders are physically timed before weighted/N/pooled-rescue retention, "
            "and DIRECT pair tables time all empty gaps and six legal blocks. Requested legacy gap hints "
            "are not pre-scoring gap cutoffs. Initial index proxies are bounded ranking devices; later "
            "preview ranking counts all actual native delays, and pooled versus compensation rescues "
            "are recorded separately. "
            "Preview routes, missing-compensation windows and any temporary local quota overflow are "
            "explicitly incomplete ranking evidence, never global feasible solutions or complete-J bounds. "
            "After four tasks are removed and compensation is restored, all affected routes, windows, "
            "waiting and native delivery scores are rebuilt and fully checked. Relay retains two true "
            "single-hop transfers at one existing station and a positive double-loaded receiver flight. "
            "Other196 original tasks preserve owners and signed subsequences in this raw variant. "
            "DIRECT independently allocates the same four selected requests among the three aircraft "
            "with1/1/2 final responsibilities through all12 assignments; it neither visits H nor inherits "
            "relay-selected final owners or conditional window restrictions. Both selectors must finish "
            "with identical family records/SHA. Candidate rescue channels do not change the weighted "
            "adoption objective, official lateness epsilon, resources or original-input guard. "
            "Every complete saved proposal receives independent true-base physical, clock and score replay. "
            "All indexing, ranking, full checks, evidence snapshots and guarded adoption share one deadline.")
    if set(arms) & set(DAMAGE_PAIR):
        note = ROOT / "papers/notes/proactive_relay_20261014/relay_damage_closure_repair_design_v1.md"
        manifest["relay_damage_closure_repair_design"] = dict(path=str(note.relative_to(ROOT)), sha256=sha(note))
        protocol_path = ROOT / "papers/notes/proactive_relay_20261014/window_damage_repair_protocol_v1.json"
        protocol = read(protocol_path)
        assert arms == protocol["arms"] and args.seed == protocol["seed"]
        assert args.budget_seconds == protocol["budget_seconds_per_arm"]
        assert checkpoint and checkpoint["sha256"] == protocol["checkpoint_sha256"]
        assert manifest["input_sha256"] == protocol["input_sha256"]
        assert manifest["weights"] == protocol["weights"] and manifest["scales"] == protocol["scales"]
        assert all(parameters[arm] == protocol["method_parameters"] for arm in arms)
        manifest["window_damage_repair_protocol"] = dict(path=str(protocol_path.relative_to(ROOT)), sha256=sha(protocol_path))
        manifest["protocol"] += (
            " Damage variants rebuild raw window proposals inside the same method deadline, then use "
            "at most two own-arm complete proposals that truly rescue original-late pooled i/j tasks. "
            "The old +.015 J exploration gate is not used; only strict complete weighted improvement "
            "over the original strong input can be adopted. Raw provenance remains base-bound. "
            "Post repair pins only the two real i/j plans, roles and signed subsequences, preserves "
            "consecutive empty double OUT takes and positive shared flight, and allows ordinary "
            "DIRECT service between OUT deliveries. Other198 tasks, including compensation, may be "
            "reassigned using actual donor slots opened on all available aircraft within a declared "
            "closure cap. Damage includes newly late, positive lateness growth and co-loaded partners. "
            "Quota-aware joint repair compares equal-coverage partial states with actual global waits; "
            "assignment and partial rankings are not complete-J certificates. Complete leaves require "
            "current full physics, exact per-aircraft final quotas and pre-deadline checkpoints. "
            "DIRECT receives identical closure/slot/repair freedom after its independent12 raw maps. "
            "All clocks, owners and rescue classifications are rebuilt after repair, and raw table "
            "scores or raw suffix windows cannot certify post snapshots. Bounded closures do not mean "
            "all198 tasks or all positions have been exhaustively reoptimized.")
    if set(arms) & set(SPLIT_FEEDER_PAIR):
        note = ROOT / "papers/notes/proactive_relay_20261014/split_feeder_quota_neutral_design_v1.md"
        manifest["split_feeder_design"] = dict(path=str(note.relative_to(ROOT)), sha256=sha(note))
        protocol_path = ROOT / "papers/notes/proactive_relay_20261014/split_feeder_protocol_v1.json"
        protocol = read(protocol_path)
        assert arms == protocol["arms"] and args.seed == protocol["seed"]
        assert args.budget_seconds == protocol["budget_seconds_per_arm"]
        assert checkpoint and checkpoint["sha256"] == protocol["checkpoint_sha256"]
        assert manifest["input_sha256"] == protocol["input_sha256"]
        assert manifest["weights"] == protocol["weights"] and manifest["scales"] == protocol["scales"]
        assert all(parameters[arm] == protocol["method_parameters"] for arm in arms)
        manifest["split_feeder_protocol"] = dict(path=str(protocol_path.relative_to(ROOT)), sha256=sha(protocol_path))
        manifest["protocol"] += (
            " Split-feeder variants select two requests originally delivered by the same C, and "
            "distinct A/B each transport one IN to one existing eligible H. C retains both final "
            "deliveries; original final quotas and all198 other owners/subsequences are preserved "
            "without compensation. All source native delay, empty flight, actual availability and "
            "receiver waiting are included. Receiver takes enter empty, are consecutive, and yield "
            "positive double-loaded flight. Shared catalogue witnesses are complete conditional "
            "RELAY plans; in DIRECT they are ranking evidence, not admissible DIRECT candidates. "
            "DIRECT includes original-C pair reconstruction plus independent12 balanced A1/B1/C2 "
            "maps after actual A/B donor removal, and never inherits a relay arrival window or "
            "mandatory final map. Source/position/pair/family catalogues remain explicitly bounded. "
            "Only original-input strict weighted improvements with complete physical validation "
            "and saved current clocks before the common deadline may replace the guard. "
            "No ordinary-task polish or new resources are introduced in this diagnostic.")
    if set(arms) & set(NATIVE_PRICE_PAIR):
        for field, name in (
                ("split_feeder_design", "split_feeder_quota_neutral_design_v1.md"),
                ("exact_suffix_delay_design", "exact_suffix_delay_catalogue_design_v1.md"),
                ("native_price_design", "exact_native_catalogue_implementation_v2.md")):
            note = ROOT / "papers/notes/proactive_relay_20261014" / name
            manifest[field] = dict(path=str(note.relative_to(ROOT)), sha256=sha(note))
        protocol_path = ROOT / "papers/notes/proactive_relay_20261014/native_price_protocol_v1.json"
        protocol = read(protocol_path)
        assert arms == protocol["arms"] and args.seed == protocol["seed"]
        assert args.budget_seconds == protocol["budget_seconds_per_arm"]
        assert checkpoint and checkpoint["sha256"] == protocol["checkpoint_sha256"]
        assert manifest["input_sha256"] == protocol["input_sha256"]
        assert manifest["weights"] == protocol["weights"] and manifest["scales"] == protocol["scales"]
        assert all(parameters[arm] == protocol["method_parameters"] for arm in arms)
        manifest["native_price_protocol"] = dict(path=str(protocol_path.relative_to(ROOT)), sha256=sha(protocol_path))
        manifest["protocol"] += (
            " Native-price variants keep the original-C pair and distinct A/B split-feeder structure, "
            "all final responsibilities and original resources unchanged. Contiguous source-IN "
            "native deadline costs are priced before the feeder shortlist; receiver native suffix "
            "costs use the actual deleted-pair skeleton, availability and wait before receiver-gap "
            "and base-family retention. Unsupported or floating-boundary-sensitive prices require "
            "actual route replay. Saved early restricted-layout prices are independently auditable "
            "ranking evidence, not DIRECT candidates, global lower bounds or complete adoption "
            "certificates. Retained early layouts remain available in the declared finite family "
            "source surfaces and complete witnesses. Complete current global checks still decide "
            "adoption. DIRECT retains original-C all-empty-gap/six-order reconstruction and all12 "
            "independent balanced donor maps, without a relay time window or mandatory final map. "
            "Both arms pay for the same catalogue and all checks inside equal wall ceilings. "
            "Only strict complete weighted improvement over the common strong input is adopted; "
            "no ordinary polish, free repositioning, new resources or score-conditioned retry.")
    write(output / "manifest.json", manifest)
    write(output / "runs.json", [])
    print(json.dumps({"prepared": str(output), "purpose": manifest["purpose"], "arms": arms,
                      "initial_score": manifest["initial_score"]}, ensure_ascii=False))


def verify(output, manifest, *, current_execution=True):
    assert sha(PARENT / "manifest.json") == manifest["parent_manifest_sha256"]
    assert sha(PARENT / "audit.json") == manifest["parent_audit_sha256"]
    assert digest(manifest["topology"]) == manifest["topology_sha256"]
    assert digest(manifest["initial_routes"]) == manifest["initial_routes_sha256"]
    assert sha(output / manifest["source_archive"]["path"]) == manifest["source_archive"]["sha256"]
    for name, spec in manifest["execution_sources"].items():
        assert sha(output / spec["path"]) == spec["sha256"], name
        if current_execution:
            assert sha(EXPERIMENTS / name) == spec["sha256"], name
    for field in ("input", "problem"):
        assert sha(ROOT / manifest[field + "_path"]) == manifest[field + "_sha256"]
    if manifest["checkpoint"]:
        assert sha(ROOT / manifest["checkpoint"]["path"]) == manifest["checkpoint"]["sha256"]
    if "mobile_repooling_contract" in manifest:
        contract = manifest["mobile_repooling_contract"]
        assert sha(ROOT / contract["path"]) == contract["sha256"], "Changed declared mobile model contract"
    if "compensation_selector_review" in manifest:
        review = manifest["compensation_selector_review"]
        assert sha(ROOT / review["path"]) == review["sha256"], "Changed declared selector review"
    if "temperature_diagnosis" in manifest:
        note = manifest["temperature_diagnosis"]
        assert sha(ROOT / note["path"]) == note["sha256"], "Changed declared temperature diagnosis"
    if "six_task_sorting_design" in manifest:
        note = manifest["six_task_sorting_design"]
        assert sha(ROOT / note["path"]) == note["sha256"], "Changed declared six-task sorting design"
    if "six_task_pinned_polish_design" in manifest:
        note = manifest["six_task_pinned_polish_design"]
        assert sha(ROOT / note["path"]) == note["sha256"], "Changed declared six-task pinned polish design"
    for field in ("deadline_rescue_window_design", "fixed_h_rescue_selector_review", "relay_damage_closure_repair_design", "window_damage_repair_protocol", "split_feeder_design", "split_feeder_protocol", "exact_suffix_delay_design", "native_price_design", "native_price_protocol"):
        if field in manifest:
            note = manifest[field]
            assert sha(ROOT / note["path"]) == note["sha256"], "Changed declared fixed-window design/review"


def worker(request):
    req = read(request)
    output = Path(req["output"])
    manifest = read(output / "manifest.json")
    verify(output, manifest, current_execution=False)
    for name, spec in manifest["execution_sources"].items():
        assert sha(Path(__file__).parent / name) == spec["sha256"], "Worker must run exact frozen execution files: " + name
    from uav_dispatch import alns
    from uav_dispatch.cli import result_payload
    from uav_dispatch.io import load_tasks_csv
    from uav_dispatch.model import Score
    from uav_dispatch.search import SolverResult
    from uav_dispatch.validation import evaluate_solution
    import weighted_direct_objective as weighted
    import weighted_periodic_exchange as periodic
    import run_scenario_comparison  # Load frozen scenario-helper imports before the measured interval.
    modules = {name: importlib.import_module(module) for name, module in METHODS.items()}
    started = perf_counter()
    full, plain, topology = build_case(load_tasks_csv(ROOT / manifest["input_path"]), manifest["homes"])
    assert digest(topology) == manifest["topology_sha256"]
    routes = tuple(tuple(r) for r in manifest["initial_routes"])
    if not manifest["checkpoint"]:
        initial = alns.construct_regret_initial(plain, candidate_limit=48,
                     home_seed=manifest["homes"] == "stations", home_bias=manifest["homes"] == "stations")
        assert digest(initial.routes) == manifest["initial_routes_sha256"]
        routes = initial.routes
    phases, periodic_stats, mobile_meetings = [], {}, []
    initial_check = evaluate_solution(full, routes, _force_full=True)
    assert initial_check.valid and asdict(initial_check.score) == manifest["initial_score"]
    construction = perf_counter() - started
    deadline = started + manifest["budget_seconds"] - manifest["finalization_reserve_seconds"]
    def save(current, evaluation, phase, **extras):
        problem = plain if req["arm"] in DIRECT_ARMS else full
        result = SolverResult(tuple(current), evaluation, runtime_seconds=extras.get("solve_wall_seconds", 0.),
                              iterations=extras.get("iterations", 0), metadata=MappingProxyType(extras.get("metadata", {})))
        value = result_payload(problem, result, source=manifest["input_path"])
        value["problem"]["objective_order"] = ["weighted_total_score", *FIELDS]
        value["problem"]["experiment_objective"] = dict(kind="weighted", weights=manifest["weights"], scales=manifest["scales"])
        path = f"phase_solutions/{req['run_id']}__{phase}.json"
        write(output / path, value)
        record = dict(phase=phase, solution_path=path, solution_sha256=sha(output / path),
                      routes_sha256=digest(current),
                      topology=model_topology(full, topology),
                      input_routes_sha256=phases[-1]["routes_sha256"] if phases else manifest["initial_routes_sha256"],
                      score=asdict(evaluation.score), weighted_score=key(evaluation.score, manifest)[0], **extras)
        phases.append(record)
        return value
    with weighted.objective(Score, manifest["scales"], manifest["weights"]):
        save(routes, initial_check, "initial")
        if not manifest["checkpoint"]:
            seconds = max(.001, (deadline - perf_counter()) * (1. if req["arm"] == "direct" else manifest["warmup_fraction"]))
            cfg = alns.ALNSConfig(max_iterations=10_000_000, seed=req["seed"], time_limit_seconds=seconds,
                relay_enabled=False, enable_home_seed=manifest["homes"] == "stations",
                enable_home_bias=manifest["homes"] == "stations", enable_terminal_task_exchange=True,
                enable_lazy_repair=True)
            begin = perf_counter()
            with periodic.periodic_exchange(alns, statistics=periodic_stats, **manifest["periodic_parameters"]):
                result = alns.solve_alns(plain, config=cfg, initial_routes=routes)
            checked = evaluate_solution(full, result.routes, _force_full=True)
            assert checked.valid
            routes = result.routes
            save(routes, checked, "direct_search", start_offset_seconds=begin-started,
                 solve_wall_seconds=perf_counter()-begin, config=asdict(cfg), iterations=result.iterations)
        else:
            checked = initial_check
        arm = req["arm"]
        methods = ["batch", "reciprocal", "tail_relay"] if arm == "combined" else ([arm] if arm != "direct" else [])
        for number, method in enumerate(methods):
            begin = perf_counter()
            sub_deadline = begin + (deadline - begin) / (len(methods) - number)
            controls = {"enable_transfers": method == "tail_relay"} if method.startswith("tail_") else {}
            if method.startswith("construct_"):
                controls = {"relay_enabled": method == "construct_relay"}
            if method.startswith("cluster_"):
                controls = {"relay_enabled": method == "cluster_relay"}
            if method.startswith("delivery_"):
                controls = {"relay_enabled": method == "delivery_relay"}
            if method.startswith("quota_"):
                controls = {"relay_enabled": method == "quota_relay"}
            if method.startswith("repool_"):
                controls = {"relay_enabled": method.endswith("_relay")}
            if method.startswith(("mobile_repool_", "joint_mobile_", "frontier_mobile_", "bundle_cached_", "full_credit_", "cold_pin_", "pair_pin_", "six_sort_", "wide_six_", "six_pin_", "window_", "damage_", "split_feeder_", "native_price_")):
                controls = {"relay_enabled": method.endswith("_relay")}
            controls = {**controls, **manifest.get("method_parameters", {}).get(method, {})}
            outcome = modules[method].refine(full, routes, deadline=sub_deadline,
                        weights=manifest["weights"], scales=manifest["scales"], seed=req["seed"], **controls)
            if "problem" in outcome:
                assert method in MOBILE_ARMS, "Only the separately declared mobile extension may add rendezvous points"
                unchanged_physics(full, outcome["problem"])
                full = outcome["problem"]
                plain = replace(full, leg_registry={}, task_relay_candidates={})
                mobile_meetings = outcome["metadata"]["mobile_meetings"]
            candidate = evaluate_solution(full, outcome["routes"], _force_full=True)
            assert candidate.valid
            # The module owns all pre-deadline adoption guards; this guard also
            # prevents a final checked phase from worsening its full incumbent.
            assert key(candidate.score, manifest) <= key(checked.score, manifest)
            routes, checked = outcome["routes"], candidate
            save(routes, checked, method, start_offset_seconds=begin-started,
                 solve_wall_seconds=perf_counter()-begin, iterations=outcome["iterations"], metadata=outcome["metadata"])
        final = save(routes, checked, "final", metadata=dict(periodic_task_exchange=periodic_stats,
                                                            mobile_meetings=mobile_meetings))
        counter_routes = tuple(tuple(v if abs(v) not in full.leg_registry else
            (full.leg_registry[abs(v)].task_id if v > 0 else -full.leg_registry[abs(v)].task_id)
            for v in route if abs(v) not in full.leg_registry or full.leg_registry[abs(v)].kind == "RELAY_OUT") for route in routes)
        counter = evaluate_solution(plain, counter_routes, _force_full=True)
        assert counter.valid
        counter_payload = result_payload(plain, SolverResult(counter_routes, counter), source=manifest["input_path"])
        counter_path = f"counterfactuals/{req['run_id']}.json"
        write(output / counter_path, counter_payload)
    elapsed = perf_counter() - started
    row = dict(**{k:req[k] for k in ("run_id", "arm", "seed")}, purpose=manifest["purpose"],
        valid=checked.valid, budget_compliant=elapsed <= manifest["budget_seconds"], budget_seconds=manifest["budget_seconds"],
        end_to_end_seconds=elapsed, construction_seconds=construction, phases=phases,
        **asdict(checked.score), weighted_score=key(checked.score, manifest)[0], on_time_count=200-checked.score.late_count,
        initial_routes_sha256=digest(manifest["initial_routes"]), source_sha256=manifest["source_archive"]["sha256"],
        solution_path=phases[-1]["solution_path"], solution_sha256=phases[-1]["solution_sha256"],
        counterfactual_path=counter_path, counterfactual_sha256=sha(output / counter_path))
    write(req["response"], row)


def run(output):
    manifest = read(output / "manifest.json")
    verify(output, manifest)
    rows = read(output / "runs.json")
    assert [r["run_id"] for r in rows] == [c["run_id"] for c in manifest["schedule"][:len(rows)]]
    for cell in manifest["schedule"][len(rows):]:
        assert not any((output / "phase_solutions").glob(cell["run_id"] + "__*.json")), "No retry of an existing cell"
        receipt = output / "receipts" / (cell["run_id"] + ".json")
        assert not receipt.exists(), "Previously started cell is never automatically retried"
        start_receipt = dict(**cell, status="started", started_at=datetime.now(timezone.utc).isoformat(),
                             manifest_sha256=sha(output / "manifest.json"))
        write(receipt, start_receipt)
        with tempfile.TemporaryDirectory(prefix="proactive-relay-") as temp:
            temp = Path(temp)
            with ZipFile(output / manifest["source_archive"]["path"]) as package:
                assert all(n.startswith("uav_dispatch/") and n.endswith(".py") and len(Path(n).parts)==2 for n in package.namelist())
                package.extractall(temp)
            request = temp / "request.json"
            write(request, dict(**cell, output=str(output), response=str(temp / "response.json")))
            print("START " + cell["run_id"], flush=True)
            try:
                subprocess.run([sys.executable, str(output / "sources/run_proactive_relay_research.py"), "worker", "--request", str(request)],
                    cwd=ROOT, env={**os.environ, "JD_RELAY_WORKSPACE_ROOT": str(ROOT),
                                  "PYTHONPATH": os.pathsep.join((str(temp),str(ROOT))), "PYTHONDONTWRITEBYTECODE":"1"},
                    check=True, timeout=manifest["budget_seconds"] + 30)
            except BaseException as error:
                write(receipt, {**start_receipt, "status": "failed_or_interrupted", "error_type": type(error).__name__})
                raise
            row = read(temp / "response.json")
            write(output / "receipts" / (cell["run_id"] + "__response.json"), row)
        rows.append(row)
        write(output / "runs.json", rows)
        write(receipt, {**start_receipt, "status": "completed", "completed_at": datetime.now(timezone.utc).isoformat(),
                        "response_sha256": sha(output / "receipts" / (cell["run_id"] + "__response.json"))})
        print(json.dumps({k:row[k] for k in ("run_id", *FIELDS, "weighted_score", "end_to_end_seconds")}), flush=True)
        assert row["valid"] and row["budget_compliant"], "Failed cell retained; no score-based retry"


def audit(output):
    schedule = read(output / "manifest.json")["schedule"]
    if any(row["arm"] in NATIVE_PRICE_PAIR for row in schedule):
        assert all(row["arm"] in NATIVE_PRICE_PAIR for row in schedule), "Native-price batches use their own paired audit"
        try:
            from .audit_split_feeder_batch_v2 import audit as native_price_audit
        except ImportError:
            from audit_split_feeder_batch_v2 import audit as native_price_audit
        return native_price_audit(output)
    if any(row["arm"] in SPLIT_FEEDER_PAIR for row in schedule):
        assert all(row["arm"] in SPLIT_FEEDER_PAIR for row in schedule), "Split-feeder batches use their own paired audit"
        try:
            from .audit_split_feeder_batch import audit as split_feeder_audit
        except ImportError:
            from audit_split_feeder_batch import audit as split_feeder_audit
        return split_feeder_audit(output)
    if any(row["arm"] in DAMAGE_PAIR for row in schedule):
        assert all(row["arm"] in DAMAGE_PAIR for row in schedule), "Damage batches use their own paired audit"
        try:
            from .audit_window_damage_batch import audit as damage_audit
        except ImportError:
            from audit_window_damage_batch import audit as damage_audit
        return damage_audit(output)
    from algorithm.experiments.audit_weighted_direct_pair import load_official_csv
    from algorithm.experiments.audit_weighted_relay_ablation import audit_payload, direct_counterfactual, reference_schedule
    manifest = read(output / "manifest.json")
    verify(output, manifest, current_execution=False)
    # The independent geometry/DAG auditors are immutable across research
    # iterations; newer orchestration does not change historical evidence.
    for name in ("audit_weighted_relay_ablation.py", "audit_weighted_direct_pair.py"):
        assert sha(EXPERIMENTS / name) == manifest["execution_sources"][name]["sha256"]
    mobile = any(r["arm"] in MOBILE_ARMS for r in manifest["schedule"])
    if mobile:
        from algorithm.experiments.audit_mobile_relay import audit_mobile_payload, audit_mobile_routes
        assert sha(EXPERIMENTS / "audit_mobile_relay.py") == manifest["execution_sources"]["audit_mobile_relay.py"]["sha256"]
    if any(r["arm"] in ANTICIPATORY_ARMS for r in manifest["schedule"]):
        from algorithm.experiments.audit_anticipatory_mobile import audit_anticipatory_payload, audit_anticipatory_routes
        assert sha(EXPERIMENTS / "audit_anticipatory_mobile.py") == manifest["execution_sources"]["audit_anticipatory_mobile.py"]["sha256"]
    if any(r["arm"] in ALL_REPOOL_ARMS for r in manifest["schedule"]):
        from algorithm.experiments.audit_repooling import audit_repooling_proposal
        assert sha(EXPERIMENTS / "audit_repooling.py") == manifest["execution_sources"]["audit_repooling.py"]["sha256"]
    if any(r["arm"] in ALL_PIN_ARMS for r in manifest["schedule"]):
        from algorithm.experiments.audit_pinned_repooling import audit_pinned_repooling_proposal
        assert sha(EXPERIMENTS / "audit_pinned_repooling.py") == manifest["execution_sources"]["audit_pinned_repooling.py"]["sha256"]
    if any(r["arm"] in PAIR_PIN_PAIR for r in manifest["schedule"]):
        from algorithm.experiments.audit_pair_pinned_repooling import audit_pair_pinned_repooling_proposal
        assert sha(EXPERIMENTS / "audit_pair_pinned_repooling.py") == manifest["execution_sources"]["audit_pair_pinned_repooling.py"]["sha256"]
    if any(r["arm"] in ALL_SIX_ARMS for r in manifest["schedule"]):
        from algorithm.experiments.audit_six_task_sorting import audit_six_task_sorting_proposal
        assert sha(EXPERIMENTS / "audit_six_task_sorting.py") == manifest["execution_sources"]["audit_six_task_sorting.py"]["sha256"]
    if any(r["arm"] in SIX_PIN_PAIR for r in manifest["schedule"]):
        from algorithm.experiments.audit_six_task_pinned_polish import audit_six_task_pinned_polish_proposal
        assert sha(EXPERIMENTS / "audit_six_task_pinned_polish.py") == manifest["execution_sources"]["audit_six_task_pinned_polish.py"]["sha256"]
    if any(r["arm"] in WINDOW_PAIR for r in manifest["schedule"]):
        from algorithm.experiments.audit_deadline_window_repooling import (
            audit_deadline_window_repooling_proposal, audit_deadline_window_family)
        assert sha(EXPERIMENTS / "audit_deadline_window_repooling.py") == manifest["execution_sources"]["audit_deadline_window_repooling.py"]["sha256"]
    if any(r["arm"] in THREE_PARTY_MOBILE_ARMS for r in manifest["schedule"]):
        from algorithm.experiments.audit_mobile_repooling import (
            audit_mobile_repooling_payload, audit_mobile_repooling_routes, _bind as bind_mobile_repooling)
        from algorithm.experiments.audit_weighted_relay_ablation import _geometry
        assert sha(EXPERIMENTS / "audit_mobile_repooling.py") == manifest["execution_sources"]["audit_mobile_repooling.py"]["sha256"]
    tasks = load_official_csv(ROOT / manifest["input_path"])
    rows = read(output / "runs.json")
    assert len(rows) == len(manifest["schedule"]) > 0, "Cannot pass an empty or incomplete research batch"
    assert [r["run_id"] for r in rows] == [c["run_id"] for c in manifest["schedule"]]
    results = []
    for row, cell in zip(rows, manifest["schedule"]):
        assert all(row[k] == cell[k] for k in ("run_id", "arm", "seed"))
        assert row["valid"] and row["budget_compliant"] and 0 <= row["end_to_end_seconds"] <= manifest["budget_seconds"]
        assert row["initial_routes_sha256"] == manifest["initial_routes_sha256"]
        assert row["source_sha256"] == manifest["source_archive"]["sha256"]
        actual_topology = {**manifest["topology"], "relay_allowed": row["arm"] not in DIRECT_ARMS}
        if row["arm"] in MOBILE_ARMS:
            if row["arm"] in THREE_PARTY_MOBILE_ARMS:
                physical_payload_audit, physical_routes_audit = audit_mobile_repooling_payload, audit_mobile_repooling_routes
            elif row["arm"] in ANTICIPATORY_ARMS:
                physical_payload_audit, physical_routes_audit = audit_anticipatory_payload, audit_anticipatory_routes
            else:
                physical_payload_audit, physical_routes_audit = audit_mobile_payload, audit_mobile_routes
        preceding_key = None
        preceding_routes_hash = manifest["initial_routes_sha256"]
        preceding_routes = manifest["initial_routes"]
        proposal_references = []
        window_family_references = []
        for phase in row["phases"]:
            assert sha(output / phase["solution_path"]) == phase["solution_sha256"]
            phase_payload = read(output / phase["solution_path"])
            phase_topology = {**phase.get("topology", actual_topology), "relay_allowed": actual_topology["relay_allowed"]}
            if row["arm"] not in MOBILE_ARMS:
                assert phase_topology == actual_topology, "Fixed-site experiment changed its frozen topology"
            reference = audit_payload(tasks, phase_topology, phase_payload)
            if row["arm"] in MOBILE_ARMS:
                physical_payload_audit(tasks, manifest["topology"], phase_topology, phase_payload)
            phase_routes = [r["encoded_visits"] for r in phase_payload["routes"]]
            phase_input_routes = preceding_routes
            preceding_routes = phase_routes
            assert digest(phase_routes) == phase["routes_sha256"]
            assert phase["input_routes_sha256"] == preceding_routes_hash
            preceding_routes_hash = phase["routes_sha256"]
            assert all(abs(reference["score"][k]-phase["score"][k]) < 1e-8 for k in FIELDS)
            if phase["phase"] == "initial":
                assert preceding_routes_hash == manifest["initial_routes_sha256"]
                assert all(abs(reference["score"][k]-manifest["initial_score"][k]) < 1e-8 for k in FIELDS)
            values = [reference["score"][k] for k in FIELDS]
            phase_key = (sum(w * v / s for w, v, s in zip(manifest["weights"], values, manifest["scales"])), *values)
            assert abs(phase_key[0] - phase["weighted_score"]) < 1e-10
            if preceding_key is not None:
                assert phase_key <= preceding_key, "Saved complete phase worsens its verified incumbent"
            preceding_key = phase_key
            metadata = phase.get("metadata", {})
            proposals = [(name, metadata.get(name)) for name in
                         ("first_complete_candidate", "best_constructed_candidate", "input_snapshot")]
            proposals.extend((f"full_candidate_{i}", snapshot) for i, snapshot in
                             enumerate(metadata.get("full_candidate_snapshots", [])))
            raw_stage = metadata.get("raw_stage", {})
            raw_proposals = [(name, raw_stage.get(name)) for name in
                             ("first_complete_candidate", "best_constructed_candidate", "input_snapshot")]
            raw_proposals.extend((f"full_candidate_{i}", snapshot) for i, snapshot in
                                 enumerate(raw_stage.get("full_candidate_snapshots", [])))
            for raw_name, snapshot in raw_proposals:
                if snapshot is not None:
                    proposals.append((f"raw_stage__{raw_name}",
                        {**snapshot, "topology": snapshot.get("topology", manifest["topology"]),
                         "mobile_meetings": snapshot.get("mobile_meetings", [])}))
            pinned_macro_routes = {}
            pinned_original_moves = {}
            for polish_index, polish_record in enumerate(metadata.get("polish_records", [])):
                macro_routes = polish_record["macro_routes"]
                macro = polish_record["full_proposal"]
                assert macro_routes == macro["routes"]
                proposals.append((f"pinned_{polish_index}__full_proposal", macro))
                post_name = f"pinned_{polish_index}__post_polish"
                proposals.append((post_name, polish_record.get("post_polish")))
                pinned_macro_routes[post_name] = macro_routes
                pinned_original_moves[post_name] = macro["move"]
                helper = polish_record["helper_metadata"]
                assert helper["patches_restored"]
                if row["arm"] in PAIR_PIN_PAIR:
                    original_move = macro["move"]
                    assert polish_record["original_macro_move"] == original_move
                    assert sorted(helper["pinned_task_ids"]) == sorted((original_move["i"], original_move["j"]))
                    assert sorted(helper["required_cross_task_ids"]) == (sorted((original_move["i"], original_move["j"]))
                        if row["arm"].endswith("_relay") else [])
                if row["arm"] in SIX_PIN_PAIR:
                    original_move = macro["move"]
                    assert polish_record["original_macro_move"] == original_move
                    pins = sorted(original_move["family"]["task_ids"])
                    assert len(pins) == 6 and helper["pinned_task_ids"] == pins
                    assert sorted(helper["required_cross_task_ids"]) == (pins if row["arm"].endswith("_relay") else [])
                if row["arm"] in COLD_CONFIG_ARMS:
                    expected_profile = manifest["cold_polish_profiles"][row["arm"]]
                    assert metadata["cold_polish_profile"] == expected_profile
                    assert all(helper["configuration"][name] == value for name, value in expected_profile.items())
                    assert helper["configuration"]["enable_budget_cooling"] is False
                helper_snapshots = [(name, helper.get(name)) for name in
                    ("input_snapshot", "first_complete_candidate", "best_constructed_candidate", "post_pin_snapshot")]
                helper_snapshots.extend((f"full_candidate_{i}", snapshot) for i, snapshot in
                                         enumerate(helper.get("full_candidate_snapshots", [])))
                for helper_name, snapshot in helper_snapshots:
                    if snapshot is None:
                        continue
                    name = f"pinned_{polish_index}__helper__{helper_name}"
                    proposals.append((name, snapshot if row["arm"] in SIX_PIN_PAIR else
                                      {**snapshot, "move": snapshot.get("move") or polish_record["move"]}))
                    pinned_macro_routes[name] = macro_routes
                    pinned_original_moves[name] = macro["move"]
                if polish_record.get("adopted"):
                    assert polish_record["finished_within_leaf_deadline"]
                    assert helper["returned_checkpoint_checked_within_deadline"]
                    assert polish_record["full_verified_at_perf_counter"] < polish_record["leaf_deadline_perf_counter"]
            if row["arm"] in SIX_PIN_PAIR:
                # Top first/best/full aliases are post snapshots, too. Bind
                # them to the authentic raw candidate rather than treating
                # an old family descriptor as a current separable table.
                for name, snapshot in proposals:
                    if snapshot is None or snapshot.get("pin_scope") != "six_task_sorting" or name in pinned_macro_routes:
                        continue
                    matches = [r for r in metadata["polish_records"] if r.get("post_polish") == snapshot]
                    assert matches, "Post snapshot has no source-bound polish record"
                    assert all(r["macro_routes"] == matches[0]["macro_routes"] for r in matches)
                    pinned_macro_routes[name] = matches[0]["macro_routes"]
                    pinned_original_moves[name] = matches[0]["original_macro_move"]
            for cluster_record in metadata.get("cluster_records", []):
                for number, candidate_record in enumerate(cluster_record["complete_candidates"]):
                    for stage in ("full_proposal", "post_polish"):
                        proposals.append((f"cluster_{cluster_record['cluster_index']}__candidate_{number}__{stage}",
                                          candidate_record.get(stage)))
            for group_index, group_record in enumerate(metadata.get("group_records", [])):
                for number, candidate_record in enumerate(group_record.get("proposals", [])):
                    for stage in ("full_proposal", "post_geometry", "post_polish"):
                        proposals.append((f"group_{group_index}__candidate_{number}__{stage}",
                                          candidate_record.get(stage)))
            for proposal_name, proposal in proposals:
                if proposal is None:
                    continue
                assert proposal["complete"] and proposal["valid"]
                proposal_topology = {**phase_topology, **proposal.get("topology", {}),
                                     "relay_allowed": phase_topology["relay_allowed"]}
                proposal_reference = reference_schedule(tasks, proposal["routes"], proposal_topology)
                repooling_reference = None
                sorting_reference = None
                six_post_reference = None
                window_reference = None
                if row["arm"] in WINDOW_PAIR and proposal.get("move") is not None:
                    assert proposal["move"]["family"] in metadata["selected_families"]
                    window_reference = audit_deadline_window_repooling_proposal(
                        tasks, proposal_topology, phase_input_routes, proposal["routes"], proposal["move"],
                        relay_enabled=row["arm"].endswith("_relay"), telemetry=proposal,
                        weights=manifest["weights"], scales=manifest["scales"])
                elif row["arm"] in SIX_PIN_PAIR and proposal_name in pinned_macro_routes:
                    original_move = pinned_original_moves[proposal_name]
                    assert original_move["family"] in metadata["selected_families"]
                    six_post_reference = audit_six_task_pinned_polish_proposal(
                        tasks, proposal_topology, phase_input_routes, pinned_macro_routes[proposal_name],
                        proposal["routes"], original_move, relay_enabled=row["arm"].endswith("_relay"),
                        telemetry=proposal, weights=manifest["weights"], scales=manifest["scales"])
                elif row["arm"] in ALL_SIX_ARMS and proposal.get("move") is not None:
                    assert proposal["move"]["family"] in metadata["selected_families"]
                    sorting_reference = audit_six_task_sorting_proposal(
                        tasks, proposal_topology, phase_input_routes, proposal["routes"], proposal["move"],
                        relay_enabled=row["arm"].endswith("_relay"), telemetry=proposal,
                        weights=manifest["weights"], scales=manifest["scales"])
                if row["arm"] in ALL_REPOOL_ARMS and proposal.get("move") is not None:
                    if row["arm"] in THREE_PARTY_MOBILE_ARMS:
                        repooling_reference = physical_routes_audit(
                            tasks, manifest["topology"], proposal_topology, proposal["routes"],
                            base_routes=phase_input_routes, move=proposal["move"])
                    elif proposal_name in pinned_macro_routes and row["arm"] in PAIR_PIN_PAIR:
                        original_move = pinned_original_moves[proposal_name]
                        repooling_reference = audit_pair_pinned_repooling_proposal(
                            tasks, proposal_topology, phase_input_routes, pinned_macro_routes[proposal_name],
                            proposal["routes"], original_move, relay_enabled=row["arm"].endswith("_relay"),
                            telemetry=proposal)
                        assert proposal["original_macro_move"] == original_move
                        assert proposal["actual_final_owners"] == repooling_reference["final_owners"]
                        assert sorted(proposal["pinned_task_ids"]) == sorted((original_move["i"], original_move["j"]))
                        assert sorted(proposal["released_compensation_task_ids"]) == sorted((original_move["k1"], original_move["k2"]))
                        if proposal_name.endswith("__post_polish"):
                            assert "final_aircraft" not in proposal["move"]
                            assert proposal["move"]["semantic"] == "post_pair_polish"
                            assert abs(proposal["move"]["shared_double_loaded_km"] - repooling_reference["shared_loaded_distance_km"]) < 1e-8
                    elif proposal_name in pinned_macro_routes:
                        repooling_reference = audit_pinned_repooling_proposal(
                            tasks, proposal_topology, phase_input_routes, pinned_macro_routes[proposal_name],
                            proposal["routes"], proposal["move"], relay_enabled=row["arm"].endswith("_relay"))
                    else:
                        repooling_reference = audit_repooling_proposal(
                            tasks, proposal_topology, phase_input_routes, proposal["routes"], proposal["move"],
                            relay_enabled=row["arm"].endswith("_relay"))
                if row["arm"] in MOBILE_ARMS:
                    mobile_reference = (repooling_reference if row["arm"] in THREE_PARTY_MOBILE_ARMS and repooling_reference is not None
                        else physical_routes_audit(tasks, manifest["topology"], proposal_topology, proposal["routes"]))
                    if row["arm"] in THREE_PARTY_MOBILE_ARMS:
                        bind_mobile_repooling(proposal["mobile_meetings"], mobile_reference["meetings"], tasks,
                            proposal["routes"], _geometry(tasks, proposal_topology)[2])
                    else:
                        from algorithm.experiments.audit_mobile_relay import _bind_meetings
                        _bind_meetings(proposal["mobile_meetings"], mobile_reference["meetings"])
                    if row["arm"] in ANTICIPATORY_ARMS:
                        assert all(m.get("meeting_type") == "receiver_arrives_first" for m in proposal["mobile_meetings"])
                assert all(abs(proposal_reference["score"][k]-proposal["score"][k]) < 1e-8 for k in FIELDS)
                proposal_values = [proposal_reference["score"][k] for k in FIELDS]
                expected_key = (sum(w*v/s for w,v,s in zip(manifest["weights"],proposal_values,manifest["scales"])), *proposal_values)
                assert all(abs(a-b)<1e-8 for a,b in zip(expected_key,proposal["key"]))
                assert proposal_reference["relay_statistics"]["cross_uav_handoff_count"] == proposal["cross_uav_count"]
                assert proposal_reference["relay_statistics"]["relay_task_count"] == proposal["relay_task_count"]
                if proposal_name == "input_snapshot":
                    assert digest(proposal["routes"]) == phase["input_routes_sha256"]
                reference_evidence = proposal_reference
                sorting_evidence = sorting_reference
                if row["arm"] in COMPACT_SIX_ARMS:
                    # Raw snapshots keep every route and full clock. Replay
                    # every assertion above, then bind the large derived
                    # traces by hash instead of duplicating them 1000 times.
                    reference_evidence = dict(score=proposal_reference["score"],
                        relay_statistics=proposal_reference["relay_statistics"],
                        original_task_count=len(proposal_reference["delivery_times_min"]),
                        full_replay_output_sha256=digest(proposal_reference))
                    if sorting_reference is not None:
                        keep = ("passed", "mode", "station_id", "family", "selected_task_ids", "source_owners",
                                "final_tasks_per_aircraft", "base_score", "score", "source_drop_availability_min",
                                "inbound_records", "outbound_records", "shared_double_loaded_km_by_aircraft", "actual_cross_uav_count")
                        sorting_evidence = {name: sorting_reference[name] for name in keep}
                        sorting_evidence.update(full_source_replay_output_sha256=digest(sorting_reference),
                            native_delivery_clock_change_count=len(sorting_reference["native_delivery_clock_changes"]))
                post_evidence = six_post_reference
                if six_post_reference is not None and row["arm"] in COMPACT_SIX_ARMS:
                    keep = ("passed", "mode", "pin_scope", "pinned_task_ids", "final_tasks_per_aircraft",
                            "macro_score", "score", "actual_cross_uav_count", "unpinned_owner_changed_count",
                            "unpinned_owner_changed_task_ids", "unpinned_event_sequence_changed_aircraft",
                            "current_source_drop_availability_min", "current_shared_double_loaded_km_by_aircraft")
                    post_evidence = {name: six_post_reference[name] for name in keep}
                    post_evidence["full_post_replay_output_sha256"] = digest(six_post_reference)
                proposal_references.append(dict(phase=phase["phase"], proposal=proposal_name,
                    routes_sha256=digest(proposal["routes"]), reference=reference_evidence,
                    **({"repooling_physical_reference": repooling_reference} if repooling_reference is not None else {}),
                    **({"six_task_sorting_physical_reference": sorting_evidence} if sorting_evidence is not None else {}),
                    **({"six_task_pinned_polish_physical_reference": post_evidence} if post_evidence is not None else {}),
                    **({"deadline_window_physical_reference": window_reference} if window_reference is not None else {})))
            if row["arm"] in WINDOW_PAIR and phase["phase"] == row["arm"]:
                assert digest(metadata["selected_families"]) == metadata["selector_sha256"]
                assert metadata["accepted_improvements"] == len(metadata["accepted_moves"])
                assert metadata["families_examined"] == len(metadata["family_records"])
                assert [r["family"] for r in metadata["family_records"]] == metadata["selected_families"][:metadata["families_examined"]]
                config = metadata["configuration"]
                assert config["receiver_all_empty_gaps_scored"] and config["direct_pair_all_empty_gaps_scored"]
                assert config["direct_final_map_count"] == 12 and config["polish_enabled"] is False
                for family in metadata["selected_families"]:
                    family_reference = audit_deadline_window_family(tasks, phase_topology, phase_input_routes, family)
                    assert family_reference["passed"] and family_reference["complete_solution"] is False
                    window_family_references.append(dict(family=family, reference=family_reference))
                if row["arm"] == "window_direct":
                    for record in metadata["family_records"]:
                        ids = record["family"]["task_ids"]
                        expected_maps = [[[t], [q], [k for k in ids if k not in (t, q)]]
                                         for t in ids for q in ids if t != q]
                        considered, completed = record["direct_maps_considered"], record["direct_maps_completed"]
                        assert considered == expected_maps[:len(considered)]
                        assert completed == considered[:len(completed)]
                        assert len(considered) <= 12 and len(completed) <= len(considered)
                        if record["direct_map_enumeration_completed"]:
                            assert completed == expected_maps
                            assert record["complete_table_enumeration_completed"]
                for accepted in metadata["accepted_moves"]:
                    assert accepted["full_verified_at_perf_counter"] <= accepted["accepted_at_perf_counter"] < metadata["deadline_perf_counter"]
                    assert accepted["full_snapshot"]["move"]["family"] in metadata["selected_families"]
                if phase_routes != phase_input_routes:
                    accepted = metadata["accepted_moves"][-1]
                    assert accepted["full_snapshot"]["routes"] == phase_routes
                    audit_deadline_window_repooling_proposal(
                        tasks, phase_topology, phase_input_routes, phase_routes, accepted["full_snapshot"]["move"],
                        relay_enabled=row["arm"].endswith("_relay"), telemetry=accepted["full_snapshot"],
                        weights=manifest["weights"], scales=manifest["scales"])
            if row["arm"] in ALL_SIX_ARMS and phase["phase"] == row["arm"]:
                assert digest(metadata["selected_families"]) == metadata["selector_sha256"]
                for accepted in metadata["accepted_moves"]:
                    assert accepted["full_verified_at_perf_counter"] <= accepted["accepted_at_perf_counter"] < metadata["deadline_perf_counter"]
                    original_move = (accepted["original_macro_move"] if row["arm"] in SIX_PIN_PAIR
                                     else accepted["full_snapshot"]["move"])
                    assert original_move["family"] in metadata["selected_families"]
                if phase_routes != phase_input_routes:
                    accepted = metadata["accepted_moves"][-1]
                    assert accepted["full_snapshot"]["routes"] == phase_routes
                    if row["arm"] in SIX_PIN_PAIR and accepted["kind"] == "six_pinned_polish":
                        audit_six_task_pinned_polish_proposal(tasks, phase_topology, phase_input_routes,
                            accepted["macro_routes"], phase_routes, accepted["original_macro_move"],
                            relay_enabled=row["arm"].endswith("_relay"), telemetry=accepted["full_snapshot"],
                            weights=manifest["weights"], scales=manifest["scales"])
                    else:
                        audit_six_task_sorting_proposal(tasks, phase_topology, phase_input_routes, phase_routes,
                            accepted["full_snapshot"]["move"], relay_enabled=row["arm"].endswith("_relay"),
                            telemetry=accepted["full_snapshot"], weights=manifest["weights"], scales=manifest["scales"])
            if row["arm"] in ALL_REPOOL_ARMS and phase["phase"] == row["arm"]:
                assert digest(metadata["selected_groups"]) == metadata["selector_sha256"]
                if phase_routes != phase_input_routes:
                    accepted = metadata["accepted_moves"][-1]
                    assert accepted["full_snapshot"]["routes"] == phase_routes
                    if row["arm"] in PAIR_PIN_PAIR:
                        adopted_reference = audit_pair_pinned_repooling_proposal(tasks, phase_topology, phase_input_routes,
                            accepted["macro_routes"], phase_routes, accepted["original_macro_move"],
                            relay_enabled=row["arm"].endswith("_relay"))
                        assert accepted["actual_final_owners"] == adopted_reference["final_owners"]
                    elif row["arm"] in ALL_PIN_ARMS:
                        audit_pinned_repooling_proposal(tasks, phase_topology, phase_input_routes,
                            accepted["macro_routes"], phase_routes, accepted, relay_enabled=row["arm"].endswith("_relay"))
                    elif row["arm"] in THREE_PARTY_MOBILE_ARMS:
                        physical_payload_audit(tasks, manifest["topology"], phase_topology, phase_payload,
                            base_routes=phase_input_routes, move=accepted)
                    else:
                        audit_repooling_proposal(tasks, phase_topology, phase_input_routes, phase_routes, accepted,
                                                relay_enabled=row["arm"].endswith("_relay"))
        assert sha(output / row["solution_path"]) == row["solution_sha256"]
        final_payload = read(output / row["solution_path"])
        final_topology = {**row["phases"][-1].get("topology", actual_topology), "relay_allowed": actual_topology["relay_allowed"]}
        reference = audit_payload(tasks, final_topology, final_payload)
        physical_mobile = (physical_payload_audit(tasks, manifest["topology"], final_topology, final_payload)
                           if row["arm"] in MOBILE_ARMS else None)
        assert all(abs(reference["score"][k]-row[k]) < 1e-8 for k in FIELDS)
        assert abs(preceding_key[0] - row["weighted_score"]) < 1e-10
        assert row["on_time_count"] == len(tasks)-reference["score"]["late_count"]
        cf_topology = {**final_topology, "relay_allowed": False}
        assert sha(output / row["counterfactual_path"]) == row["counterfactual_sha256"]
        cf_payload = read(output / row["counterfactual_path"])
        final_routes = [route["encoded_visits"] for route in final_payload["routes"]]
        expected_cf = direct_counterfactual(final_routes, final_topology)
        assert [list(r) for r in expected_cf] == [r["encoded_visits"] for r in cf_payload["routes"]]
        cf = audit_payload(tasks, cf_topology, cf_payload)
        if row["arm"] in DIRECT_ARMS:
            assert reference["relay_statistics"]["relay_task_count"] == 0
        results.append(dict(run_id=row["run_id"], passed=True, reference=reference, fixed_structure_direct=cf,
                            non_adopted_or_constructed_proposals=proposal_references,
                            synchronous_mobile_physical_reference=physical_mobile,
                            **({"deadline_window_family_references": window_family_references}
                               if row["arm"] in WINDOW_PAIR else {})))
    selector_comparisons = []
    for seed in sorted({row["seed"] for row in rows}):
        window_paired = {row["arm"]: next(p["metadata"] for p in row["phases"] if p["phase"] == row["arm"])
                         for row in rows if row["seed"] == seed and row["arm"] in WINDOW_PAIR}
        if set(window_paired) == set(WINDOW_PAIR):
            for selected in window_paired.values():
                assert selected["selector_completed"]
                assert selected["selector_completed_at_perf_counter"] < selected["deadline_perf_counter"]
                assert digest(selected["selected_families"]) == selected["selector_sha256"]
            first, second = (window_paired[arm] for arm in WINDOW_PAIR)
            assert first["selected_families"] == second["selected_families"]
            assert first["selector_sha256"] == second["selector_sha256"]
            selector_comparisons.append(dict(seed=seed, arms=list(WINDOW_PAIR), selectors_completed=True,
                same_selected_families=True, selector_sha256=first["selector_sha256"],
                direct_allocation_independent=True, direct_balanced_assignment_space=12,
                same_total_budget_not_same_candidate_count=True, conditional_windows_not_global_bounds=True))
        for six_pair in (SIX_SORT_PAIR, WIDE_SIX_PAIR, SIX_PIN_PAIR):
            six_paired = {row["arm"]: next(p["metadata"] for p in row["phases"] if p["phase"] == row["arm"])
                          for row in rows if row["seed"] == seed and row["arm"] in six_pair}
            if set(six_paired) == set(six_pair):
                for arm in six_pair:
                    selected = six_paired[arm]
                    assert selected["selector_completed"], "Incomplete six-task selector; no matched claim"
                    source = selected["raw_stage"] if six_pair == SIX_PIN_PAIR else selected
                    assert selected["selector_completed_at_perf_counter"] < source["deadline_perf_counter"]
                    if six_pair == SIX_PIN_PAIR:
                        assert all(selected[name] == source[name] for name in (
                            "selected_families", "selector_sha256", "selector_completed", "selector_completed_at_perf_counter"))
                        assert selected["raw_selector_patch_restored"] and selected["pinned_alns_patches_restored"]
                        assert selected["weighted_objective_state_restored"]
                        assert selected["pinned_alns_patch_names"] == ["destroy_solution", "_all_plans_for_task",
                            "_apply_task_exact", "_repair", "_accept_worse"]
                    if six_pair in (WIDE_SIX_PAIR, SIX_PIN_PAIR):
                        assert source["selector_patch_names"] == ["_select_families"]
                        assert source["selector_patch_restored"] and source["selector_hook_intact_during_call"]
                        parameters = manifest["method_parameters"][arm]
                        config = source["expanded_configuration"]
                        assert config["max_stations"] == parameters["max_stations"]
                        assert config["families_per_station"] == parameters["families_per_station"]
                        assert config["hard_family_limit"] == config["max_stations"] * config["families_per_station"]
                        families = selected["selected_families"]
                        coverage = source["expanded_selector_statistics"]
                        assert coverage["catalog_returned"] and coverage["families_retained"] == len(families)
                        assert len(families) <= config["hard_family_limit"]
                        station_order = [s[0] for s in manifest["topology"]["stations"]][:config["max_stations"]]
                        by_station = {station: [f for f in families if f["station_id"] == station] for station in station_order}
                        assert sum(map(len, by_station.values())) == len(families)
                        for group in by_station.values():
                            assert len(group) <= config["families_per_station"]
                            assert [f["priority"] for f in group] == sorted(f["priority"] for f in group)
                        interleaved = [by_station[station][depth]
                            for depth in range(max(map(len, by_station.values()), default=0))
                            for station in station_order if depth < len(by_station[station])]
                        assert interleaved == families, "Expanded station families lost their predeclared round-robin order"
                first, second = (six_paired[arm] for arm in six_pair)
                assert first["selected_families"] == second["selected_families"]
                assert first["selector_sha256"] == second["selector_sha256"]
                selector_comparisons.append(dict(seed=seed, arms=list(six_pair), selectors_completed=True,
                    same_selected_families=True, selector_sha256=first["selector_sha256"],
                    direct_allocation_independent=True, same_total_budget_not_same_candidate_count=True,
                    **({"best_polish_candidates_not_necessarily_paired": True} if six_pair == SIX_PIN_PAIR else {})))
        for arm_pair in (REPOOL_ARMS, PIN_REPOOL_ARMS, MOBILE_REPOOL_PAIR, JOINT_MOBILE_PAIR, FRONTIER_MOBILE_PAIR, BUNDLE_CACHED_PAIR, FULL_CREDIT_PAIR, PIN_FULL_CREDIT_PAIR, COLD_PIN_PAIR, PAIR_PIN_PAIR):
            paired = {row["arm"]: next(phase["metadata"] for phase in row["phases"] if phase["phase"] == row["arm"])
                      for row in rows if row["seed"] == seed and row["arm"] in arm_pair}
            if set(paired) == set(arm_pair):
                direct_arm, relay_arm = arm_pair
                if arm_pair in (FULL_CREDIT_PAIR, PIN_FULL_CREDIT_PAIR, COLD_PIN_PAIR, PAIR_PIN_PAIR):
                    assert all(paired[arm]["selector_completed"] for arm in arm_pair), "Incomplete full-credit selector pair retained; no matched claim"
                    for arm in arm_pair:
                        selected = paired[arm]
                        selector = selected["full_score_selector_statistics"]
                        raw_stage = selected.get("raw_stage", selected)
                        assert selector["selector_completed"] and selector["patches_restored"]
                        assert selected["process_local_patches_restored"]
                        if arm_pair in (PIN_FULL_CREDIT_PAIR, COLD_PIN_PAIR, PAIR_PIN_PAIR):
                            assert selected["pinned_alns_patches_restored"]
                            assert selected["weighted_objective_state_restored"]
                        if arm_pair == PAIR_PIN_PAIR:
                            assert selected["facade_patch_names"] == ["pinned", "_macro_tasks"]
                            assert selected["facade_patches_restored"] and selected["facade_aliases_intact_during_call"]
                        assert selector["selected_groups"] == selected["selected_groups"]
                        assert selector["selector_sha256"] == selected["selector_sha256"]
                        selector_deadline = raw_stage.get("proposal_deadline_perf_counter", raw_stage["deadline_perf_counter"])
                        assert selector["selector_completed_at_perf_counter"] < selector_deadline
                assert paired[direct_arm]["selector_sha256"] == paired[relay_arm]["selector_sha256"]
                assert paired[direct_arm]["selected_groups"] == paired[relay_arm]["selected_groups"]
                comparison = dict(seed=seed, same_selected_macros=True,
                    selector_sha256=paired[direct_arm]["selector_sha256"])
                if arm_pair in (PIN_REPOOL_ARMS, PIN_FULL_CREDIT_PAIR, COLD_PIN_PAIR, PAIR_PIN_PAIR):
                    comparison.update(arms=list(arm_pair), best_polish_candidates_not_necessarily_paired=True)
                elif arm_pair in (MOBILE_REPOOL_PAIR, JOINT_MOBILE_PAIR, FRONTIER_MOBILE_PAIR, BUNDLE_CACHED_PAIR, FULL_CREDIT_PAIR):
                    comparison.update(arms=list(arm_pair), continuous_mobile_geometry_separately_declared=True)
                if arm_pair in (FULL_CREDIT_PAIR, PIN_FULL_CREDIT_PAIR, COLD_PIN_PAIR, PAIR_PIN_PAIR):
                    comparison.update(selectors_completed=True, full_removal_credit_shared_pool=True)
                selector_comparisons.append(comparison)
    record = dict(passed=True, purpose=manifest["purpose"], source_manifest_sha256=sha(output / "manifest.json"),
                  source_runs_sha256=sha(output / "runs.json"), results=results)
    if any(row["arm"] in COMPACT_SIX_ARMS for row in rows):
        record["proposal_evidence_format"] = "All original full snapshots replayed; compact per-proposal metrics plus digests of complete independent replay outputs; full routes/clocks remain in source runs.json"
    if selector_comparisons:
        record["matched_repooling_selectors"] = selector_comparisons
    write(output / "audit.json", record)
    print(json.dumps({"audit_passed": True, "run_count": len(results), "purpose": manifest["purpose"]}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "worker", "audit"))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--seed", type=int, default=2026101481)
    parser.add_argument("--budget-seconds", type=float, default=30)
    parser.add_argument("--warmup-fraction", type=float, default=.6)
    parser.add_argument("--homes", choices=("stations", "origin"), default="stations")
    parser.add_argument("--arms", default="direct,batch,reciprocal")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--method-parameters", type=json.loads, default={})
    args = parser.parse_args()
    if args.command == "prepare": prepare(args)
    elif args.command == "run": run(args.output_dir.resolve())
    elif args.command == "audit": audit(args.output_dir.resolve())
    else: worker(args.request)
