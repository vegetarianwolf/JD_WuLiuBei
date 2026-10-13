"""Audit frozen relay trials without running a search.

Complete structural rescoring is supplemented by an independently written
dictionary-based topological scheduler. The latter never calls the production
relay evaluator or its plan-index builder.
"""
from __future__ import annotations

import argparse
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
import zipfile

from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Score

from algorithm.experiments import audit_optimization_results as checks
from algorithm.experiments.audit_compact_trials import _audit_rows


def _protocol():
    from algorithm.experiments import run_relay_trials
    return run_relay_trials


def _read(path):
    return _protocol().base.read_json(path)


def _cell(row):
    return tuple(row.get(k) for k in ("stage", "case_id", "variant", "seed"))


def _score(row):
    return tuple(row[k] for k in checks.SCORE_FIELDS)


def reference_schedule(problem, routes):
    """Rebuild the earliest schedule from route and package precedence edges.

    The existing complete validator owns load, completeness and final-delivery
    K checks. This reference independently checks DAG timing, delivery scoring,
    actual aircraft waits and route distances, including home-to-first flight.
    A package stored at a station does not add aircraft waiting time.
    """
    routes = tuple(tuple(route) for route in routes)
    successors, indegree, earliest, travel = {}, {}, {}, {}
    drop, pickup, delivery, relay_by_task = {}, {}, {}, {}
    distance = 0.0
    registry = problem.leg_registry or {}
    for route_index, route in enumerate(routes):
        previous = None
        for position, visit in enumerate(route):
            node = (route_index, position)
            successors[node] = {}
            indegree[node] = 0
            length = problem.distance(previous, visit, from_node=problem.home_node(route_index) if previous is None else None)
            distance += length
            travel[node] = length / problem.speed_km_per_min
            earliest[node] = travel[node] if position == 0 else 0.0
            previous = visit
            leg = registry.get(abs(visit))
            if leg is not None:
                if leg.kind == "RELAY_IN" and visit < 0:
                    if leg.task_id in drop:
                        raise ValueError("Reference scheduler found duplicate relay drops")
                    drop[leg.task_id] = node
                    relay_by_task[leg.task_id] = leg.relay_id
                elif leg.kind == "RELAY_OUT" and visit > 0:
                    if leg.task_id in pickup:
                        raise ValueError("Reference scheduler found duplicate relay pickups")
                    pickup[leg.task_id] = node
                    if leg.task_id in relay_by_task and relay_by_task[leg.task_id] != leg.relay_id:
                        raise ValueError("Reference scheduler found mismatched relay stations")
                elif leg.kind == "RELAY_OUT" and visit < 0:
                    if leg.task_id in delivery:
                        raise ValueError("Reference scheduler found duplicate final deliveries")
                    delivery[leg.task_id] = node
            elif visit < 0:
                if abs(visit) in delivery:
                    raise ValueError("Reference scheduler found duplicate final deliveries")
                delivery[abs(visit)] = node

    def edge(source, target, weight):
        # Same-UAV adjacent unload/reload has two parallel precedence reasons;
        # merging by max avoids decrementing one indegree twice.
        if target not in successors[source]:
            successors[source][target] = weight
            indegree[target] += 1
        else:
            successors[source][target] = max(successors[source][target], weight)

    for route_index, route in enumerate(routes):
        for position in range(1, len(route)):
            edge((route_index, position - 1), (route_index, position), travel[route_index, position])
    if set(drop) != set(pickup):
        raise ValueError("Reference scheduler found incomplete relay handoffs")
    for task_id, pickup_node in pickup.items():
        leg = registry[abs(routes[pickup_node[0]][pickup_node[1]])]
        if relay_by_task[task_id] != leg.relay_id:
            raise ValueError("Reference scheduler found mismatched relay stations")
        edge(drop[task_id], pickup_node, 0.0)

    pending = deque(node for node, degree in indegree.items() if degree == 0)
    visited = 0
    while pending:
        node = pending.popleft()
        visited += 1
        for target, weight in successors[node].items():
            earliest[target] = max(earliest[target], earliest[node] + weight)
            indegree[target] -= 1
            if indegree[target] == 0:
                pending.append(target)
    if visited != len(indegree):
        raise ValueError("Reference scheduler found a handoff dependency cycle")
    deliveries = {task_id: earliest[node] for task_id, node in delivery.items()}
    waits = {}
    for task_id, node in pickup.items():
        arrival = travel[node] + (earliest[node[0], node[1] - 1] if node[1] else 0.0)
        waiting = max(0.0, earliest[node] - arrival)
        if waiting > 1e-12:
            waits[task_id] = waiting
    lateness = [max(0.0, at - problem.task(task_id).deadline_min) for task_id, at in deliveries.items()]
    positive_lateness = [value for value in lateness if value > 1e-9]
    cross = sum(drop[task_id][0] != pickup[task_id][0] for task_id in drop)
    return {"event_times_min": earliest, "delivery_times_min": deliveries,
            "waiting_by_task": waits, "total_waiting_min": sum(waits.values()),
            "max_waiting_min": max(waits.values(), default=0.0),
            "score": Score(len(positive_lateness), sum(positive_lateness), distance),
            "relay_task_count": len(drop), "cross_uav_handoff_count": cross,
            "same_uav_relay_count": len(drop) - cross,
            "relay_tasks": {task_id: {"relay_id": relay_by_task[task_id],
                "in_leg": abs(routes[drop[task_id][0]][drop[task_id][1]]),
                "out_leg": abs(routes[pickup[task_id][0]][pickup[task_id][1]])} for task_id in drop}}


def _manifest(manifest):
    protocol = _protocol()
    if (type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1 or manifest.get("kind") != "relay_trials"
            or manifest.get("baseline_variant") != "baseline"
            or manifest.get("baseline_commit") != protocol.BASELINE_COMMIT
            or manifest.get("input_path") != protocol.base.INPUT
            or manifest.get("seeds") != protocol.SEEDS or manifest.get("protocol") != protocol.PROTOCOL
            or any(type(seed) is not int for seeds in manifest["seeds"].values() for seed in seeds)):
        raise ValueError("Relay trial changed its frozen protocol, baseline or seeds")


def _sources(directory, manifest):
    protocol = _protocol()
    snapshots = manifest.get("source_snapshots")
    if not isinstance(snapshots, dict) or set(snapshots) != set(protocol.VARIANTS):
        raise ValueError("Incomplete relay solver snapshots")
    commit = protocol.BASELINE_COMMIT
    baseline = {name.removeprefix("algorithm/src/"): checks._git("show", f"{commit}:{name}")
                for name in checks._git("ls-tree", "-r", "--name-only", commit, "--", "algorithm/src/uav_dispatch").decode().splitlines()
                if name.endswith(".py")}
    hashes = {}
    for variant, flags in protocol.VARIANTS.items():
        source = snapshots[variant]
        if (not isinstance(source, dict) or source.get("config") != flags
                or any(type(source["config"][k]) is not type(v) for k, v in flags.items())
                or source.get("source_ref") != (commit if variant == "baseline" else "working_tree")):
            raise ValueError("Relay solver configuration or source reference changed")
        path = checks._within(directory, source.get("path"), variant)
        fingerprint = checks._fingerprint(source.get("sha256"), variant)
        if checks._sha256(path) != fingerprint:
            raise ValueError("Relay source snapshot fingerprint mismatch")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(set(names)) != len(names) or any(not n.startswith("uav_dispatch/") or not n.endswith(".py")
                    or len(Path(n).parts) != 2 or ".." in Path(n).parts for n in names):
                raise ValueError("Unsafe or duplicate relay source archive members")
            files = {name: archive.read(name) for name in names}
        if set(files) != set(baseline) or (variant == "baseline" and files != baseline):
            raise ValueError("Relay solver module set or baseline Git bytes disagree")
        hashes[variant] = fingerprint
    candidates = [name for name in protocol.VARIANTS if name != "baseline"]
    if len({(snapshots[name]["path"], hashes[name]) for name in candidates}) > 1:
        raise ValueError("Isolated relay feature arms must use one candidate snapshot")
    execution = manifest.get("execution_sources")
    if not isinstance(execution, dict) or set(execution) != set(protocol.EXECUTION_NAMES):
        raise ValueError("Incomplete frozen relay execution sources")
    execution_hashes = {}
    for name, source in execution.items():
        path = checks._within(directory, source.get("path"), name)
        fingerprint = checks._fingerprint(source.get("sha256"), name)
        if checks._sha256(path) != fingerprint or checks._sha256(checks.ROOT / f"algorithm/experiments/{name}.py") != fingerprint:
            raise ValueError("Archived or live relay execution source changed")
        execution_hashes[name] = fingerprint
    if manifest.get("runner_sha256") != execution_hashes["run_optimization_experiments"]:
        raise ValueError("Relay worker fingerprint mismatch")
    return hashes, execution_hashes


def _row_protocol(row):
    protocol = _protocol()
    stage, case, variant, seed = _cell(row)
    if (stage not in protocol.SEEDS or case not in protocol.CASES[stage] or variant not in protocol.VARIANTS
            or type(seed) is not int or seed not in protocol.SEEDS[stage]
            or row.get("budget_seconds") != protocol.BUDGETS[stage][case]):
        raise ValueError("Relay run violates a frozen experiment cell or budget")
    expected_scenario = case.split("__", 1)[1] if "__" in case else "pure_direct"
    if row.get("scenario") != expected_scenario:
        raise ValueError("Relay run case/scenario mismatch")


def _configuration(row, payload):
    protocol = _protocol()
    relay = row["scenario"] in {"direct_relay", "direct_relay_stations"}
    homes = row["scenario"] in {"direct_stations", "direct_relay_stations"}
    expected = {"max_iterations": 100 if row["stage"] == "compatibility" else 10_000_000,
                "time_limit_seconds": max(1e-6, row["budget_seconds"] - row["construction_seconds"] - 1.),
                "seed": row["seed"], "candidate_limit": 48, "relay_enabled": relay,
                "relay_direct_warmup_fraction": .8, "enable_home_seed": homes, "enable_home_bias": homes,
                **protocol.VARIANTS[row["variant"]]}
    config = row.get("config")
    if not isinstance(config, dict) or set(config) != set(expected) or any(config[k] != v or type(config[k]) is not type(v)
            for k, v in expected.items() if k != "time_limit_seconds"):
        raise ValueError("Relay run configuration differs from frozen variant")
    checks._same(expected["time_limit_seconds"], config["time_limit_seconds"], "configured time")
    metadata = payload.get("metadata", {})
    for key in ("seed", "relay_enabled", "enable_home_seed", *protocol.VARIANTS[row["variant"]]):
        if metadata.get(key) != expected[key] or type(metadata.get(key)) is not type(expected[key]):
            raise ValueError(f"Relay metadata disagrees with execution: {key}")
    checks._same(expected["time_limit_seconds"], metadata.get("time_limit_seconds"), "metadata time")
    if relay and (metadata.get("staged_relay") is not True or metadata.get("relay_direct_warmup_fraction") != .8):
        raise ValueError("Relay execution must preserve 80% direct warmup")
    if row["iterations"] > expected["max_iterations"]:
        raise ValueError("Relay run exceeded its iteration cap")


def _reference_audit(directory, manifest, rows):
    tasks = tuple(load_tasks_csv(checks._within(checks.ROOT, manifest["input_path"], "official input")))
    cache = {}
    relay_rows = 0
    for row in rows:
        problem = checks._case_problem(row, tasks)
        payload = _read(checks._within(directory, row["solution_file"], "solution"))
        key = row["case_id"], row["solution_sha256"]
        routes = [route["encoded_visits"] for route in payload["routes"]]
        if key not in cache:
            cache[key] = reference_schedule(problem, routes)
        reference = cache[key]
        if set(reference["delivery_times_min"]) != set(problem.task_ids):
            raise ValueError("Reference scheduler does not deliver every original task")
        for field in checks.SCORE_FIELDS:
            checks._same(getattr(reference["score"], field), row[field], f"reference {field}")
        for task_id, time in reference["delivery_times_min"].items():
            checks._same(time, payload["delivery_times_min"].get(str(task_id)), f"reference delivery {task_id}")
        for route_index, route in enumerate(payload["routes"]):
            if len(route.get("visits", [])) != len(routes[route_index]):
                raise ValueError("Saved event list does not match signed route")
            for position, visit in enumerate(route["visits"]):
                checks._same(reference["event_times_min"][route_index, position], visit.get("arrival_time_min"), "reference event time")
        stats = payload.get("relay", {})
        if stats.get("relay_count") != len(problem.relay_stations) or stats.get("relay_coordinates") != [
                [station.id, station.x, station.y] for station in problem.relay_stations]:
            raise ValueError("Saved relay coordinates disagree with frozen scenario")
        for field in ("relay_task_count", "cross_uav_handoff_count", "same_uav_relay_count"):
            checks._same(reference[field], stats.get(field), f"reference {field}")
        checks._same(reference["total_waiting_min"], stats.get("total_relay_waiting_time"), "reference total wait")
        checks._same(reference["max_waiting_min"], stats.get("max_relay_waiting_time"), "reference max wait")
        checks._same(reference["total_waiting_min"] / reference["relay_task_count"] if reference["relay_task_count"] else 0.,
                     stats.get("average_relay_waiting_time"), "reference mean wait")
        for task_id in problem.task_ids:
            plan = reference["relay_tasks"].get(task_id)
            expected = {"mode": "DIRECT", "relay_id": None, "leg_ids": []} if plan is None else {
                "mode": "RELAY", "relay_id": plan["relay_id"], "leg_ids": [plan["in_leg"], plan["out_leg"]]}
            if payload.get("task_plans", {}).get(str(task_id)) != expected:
                raise ValueError("Saved relay plan disagrees with signed visits")
        if set(payload.get("task_plans", {})) != {str(task_id) for task_id in problem.task_ids}:
            raise ValueError("Saved task-plan set differs from official tasks")
        if stats.get("direct_task_count") != len(problem.tasks) - reference["relay_task_count"]:
            raise ValueError("Saved direct count differs from signed visits")
        checks._same(reference["relay_task_count"] / len(problem.tasks), stats.get("relay_share"), "reference relay share")
        stations = stats.get("relay_usage_per_station", [])
        if [station.get("station_id") for station in stations] != [station.id for station in problem.relay_stations]:
            raise ValueError("Saved station utilization has missing or reordered stations")
        for station, saved in zip(problem.relay_stations, stations, strict=True):
            if saved.get("x") != station.x or saved.get("y") != station.y:
                raise ValueError("Saved station utilization coordinates disagree with frozen scenario")
            task_ids = [task_id for task_id, plan in reference["relay_tasks"].items() if plan["relay_id"] == station.id]
            for field in ("task_count", "drop_count", "pickup_count"):
                checks._same(len(task_ids), saved.get(field), f"reference station {field}")
            checks._same(sum(reference["waiting_by_task"].get(task_id, 0.) for task_id in task_ids) / len(task_ids)
                         if task_ids else 0., saved.get("average_wait"), "reference station wait")
        metadata = payload.get("metadata", {})
        for field in ("relay_warmup_score", "relay_search_final_score"):
            if row.get(field) != metadata.get(field):
                raise ValueError("Saved relay phase score disagrees with metadata")
        if row.get("relay_statistics") != metadata.get("relay", {}):
            raise ValueError("Row relay statistics differ from solution metadata")
        if row["scenario"] in {"direct_relay", "direct_relay_stations"}:
            if metadata.get("relay") != stats:
                raise ValueError("Relay metadata differs from exported route statistics")
            warm = checks._score(metadata.get("relay_warmup_score"), "relay warmup score")
            relay = checks._score(metadata.get("relay_search_final_score"), "relay final score")
            retained = relay > warm
            if metadata.get("relay_retained_warmup_incumbent") is not retained:
                raise ValueError("Relay warmup retention disagrees with phase scores")
            for value, field in zip(min(warm, relay), checks.SCORE_FIELDS, strict=True):
                checks._same(value, row[field], "relay retained phase score")
            if metadata.get("relay_warmup_iterations", 0) + metadata.get("relay_search_iterations", 0) != row["iterations"]:
                raise ValueError("Relay stage iteration counts disagree with total")
        relay_rows += int(problem.has_relays)
    return {"validated_rows": len(rows), "validated_unique_solutions": len(cache), "relay_enabled_rows": relay_rows,
            "method": "Independent dictionary-based Kahn longest-path scheduler; route edges and zero-time unload-to-pickup edges rebuilt from signed visits; no production DAG/plan-index calls"}


def _pairs(rows, stage, variant, case):
    index = {(row["variant"], row["seed"]): row for row in rows if row["stage"] == stage
             and row["case_id"] == case and row["valid"] and row["budget_compliant"]}
    return [(index["baseline", seed], index[variant, seed]) for seed in _protocol().SEEDS[stage]
            if ("baseline", seed) in index and (variant, seed) in index]


def _complete(rows, stage, variant):
    protocol = _protocol()
    return all({b["seed"] for b, _ in _pairs(rows, stage, variant, case)} == set(protocol.SEEDS[stage])
               for case in protocol.CASES[stage])


def _wins(matched):
    return sum(_score(c) < _score(b) for b, c in matched)


def _gates(rows):
    protocol = _protocol()
    candidates = [name for name in protocol.VARIANTS if name != "baseline"]
    compatibility = {name: _complete(rows, "compatibility", name) and all(
        b["iterations"] == c["iterations"] == 100 and b["decision_fingerprint"] == c["decision_fingerprint"]
        for b, c in _pairs(rows, "compatibility", name, "fleet200")) for name in candidates}
    screen = {name: compatibility[name] and _complete(rows, "screen", name)
              and any(_wins(_pairs(rows, "screen", name, case)) for case in protocol.CASES["screen"])
              for name in candidates}

    def rank(name):
        primary = _pairs(rows, "screen", name, protocol.PRIMARY)
        return (_wins(primary), sum(_wins(_pairs(rows, "screen", name, case)) for case in protocol.CASES["screen"]),
                sum(b["late_count"] - c["late_count"] for b, c in primary),
                sum(b["late_count"] == c["late_count"] and _score(c) < _score(b) for b, c in primary),
                -list(protocol.VARIANTS).index(name))

    eligible = [name for name in candidates if screen[name]]
    selected = max(eligible, key=rank) if eligible else None
    passed = bool(selected and _complete(rows, "medium", selected)
                  and _wins(_pairs(rows, "medium", selected, protocol.PRIMARY)) >= 2)
    return {"compatibility": compatibility, "screen": screen, "selected_variant": selected,
            "medium": passed, "default_eligible": passed, "exploratory_promotion": True}


def _decision_audit(directory, rows):
    protocol = _protocol()
    path = directory / "decisions.json"
    decisions = _read(path)
    if not isinstance(decisions, list) or any(not isinstance(d, dict) for d in decisions):
        raise ValueError("Invalid relay decision list")
    stages = {d.get("stage"): d for d in decisions}
    if len(stages) != len(decisions) or not set(stages).issubset({"screen", "medium"}):
        raise ValueError("Duplicate or unknown relay decision")
    if "medium" in stages and "screen" not in stages:
        raise ValueError("Relay medium decision is missing screening prerequisite")
    gates = _gates(rows)
    selected = gates["selected_variant"]
    names = {"compatibility": list(protocol.VARIANTS), "screen": list(protocol.VARIANTS),
             "medium": ["baseline", selected] if selected else [], "reference": ["baseline"] if selected else []}
    for stage, variants in names.items():
        stage_rows = [r for r in rows if r["stage"] == stage]
        if any(r["variant"] not in variants for r in stage_rows):
            raise ValueError("Unqualified relay candidate entered later stage")
        for row in stage_rows:
            order = variants if protocol.SEEDS[stage].index(row["seed"]) % 2 == 0 else list(reversed(variants))
            if row.get("order") != order.index(row["variant"]):
                raise ValueError("Relay run order violates paired AB/BA scheduling")
        # A completed screen implies complete compatibility and screening;
        # a completed medium implies the prescribed medium cells. A reference
        # stage may still be running when its preceding decision is saved.
        completed = "screen" in stages if stage in {"compatibility", "screen"} else "medium" in stages and stage == "medium"
        if completed:
            planned = {(stage, case, variant, seed) for case in protocol.CASES[stage]
                       for variant in variants for seed in protocol.SEEDS[stage]}
            if {_cell(r) for r in stage_rows} != planned:
                raise ValueError(f"Completed relay stage has missing or unexpected cells: {stage}")
    expected = {"screen": {"selected_variant": selected, "passed": gates["screen"], "reason": protocol.PROTOCOL["screen_gate"]},
                "medium": {"selected_variant": selected, "passed": gates["medium"],
                           "reason": protocol.PROTOCOL["default_gate"] if selected else "初筛未入围，不扩大验证"}}
    if not selected:
        expected["medium"]["skipped"] = True
    for stage, item in stages.items():
        if any(item.get(key) != value or type(item.get(key)) is not type(value) for key, value in expected[stage].items()):
            raise ValueError(f"Relay stage decision differs from independent gate: {stage}")
    expected_reference = {("reference", case, "baseline", seed) for case in protocol.CASES["reference"]
                          for seed in protocol.SEEDS["reference"]} if selected else set()
    reference_complete = {_cell(r) for r in rows if r["stage"] == "reference"} == expected_reference
    return {**gates, "protocol_complete": set(stages) == {"screen", "medium"} and reference_complete,
            "reference_complete": reference_complete, "decisions_sha256": checks._sha256(path)}


def _budget(directory, rows):
    protocol = _protocol()
    stages = {stage: {"completed_seconds": 0., "aborted_seconds": 0., "cap_seconds": cap}
              for stage, cap in protocol.CAPS.items()}
    cells = {_cell(r) for r in rows}
    for row in rows:
        stages[row["stage"]]["completed_seconds"] += checks._number(row["end_to_end_seconds"], "elapsed")
    path = directory / "execution_notes.json"
    attempts = _read(path).get("aborted_runs", []) if path.exists() else []
    if not isinstance(attempts, list):
        raise ValueError("Invalid relay aborted attempt list")
    for attempt in attempts:
        stage, case, _, _ = _cell(attempt)
        scenario = case.split("__", 1)[1] if isinstance(case, str) and "__" in case else "pure_direct"
        _row_protocol({**attempt, "budget_seconds": protocol.BUDGETS.get(stage, {}).get(case), "scenario": scenario})
        if _cell(attempt) in cells:
            raise ValueError("Completed or aborted relay cell duplicated")
        cells.add(_cell(attempt))
        stages[stage]["aborted_seconds"] += checks._number(attempt.get("process_elapsed_seconds"), "aborted elapsed")
    for bucket in stages.values():
        bucket["total_seconds"] = bucket["completed_seconds"] + bucket["aborted_seconds"]
        bucket["budget_compliant"] = bucket["total_seconds"] <= bucket["cap_seconds"] + 1e-9
    total = sum(bucket["total_seconds"] for bucket in stages.values())
    reserve = total <= protocol.TOTAL_SECONDS - protocol.RESERVED_SECONDS + 1e-9
    return {"stages": stages, "total_seconds": total, "total_limit_seconds": protocol.TOTAL_SECONDS,
            "reserved_seconds": protocol.RESERVED_SECONDS, "reserve_preserved": reserve,
            "aborted_run_count": len(attempts), "execution_notes_sha256": checks._sha256(path) if path.exists() else None,
            "budget_compliant": reserve and all(bucket["budget_compliant"] for bucket in stages.values())}


def audit_results(output_dir):
    directory = Path(output_dir).resolve()
    target = directory / "validation.json"
    target.unlink(missing_ok=True)
    manifest, rows = _read(directory / "manifest.json"), _read(directory / "runs.json")
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ValueError("Expected relay manifest object and rows list")
    _manifest(manifest)
    hashes, execution = _sources(directory, manifest)
    referenced = set()
    for row in rows:
        run_id = "__".join(str(v) for v in (row["stage"], row["case_id"], row["variant"], row["seed"]))
        if row.get("run_id") != run_id or row.get("solution_file") != f"run_solutions/{run_id}.json":
            raise ValueError("Saved relay solution path disagrees with run cell")
        referenced.add(checks._within(directory, row["solution_file"], "solution"))
    if any(path.resolve() not in referenced for path in (directory / "run_solutions").glob("*.json")):
        raise ValueError("Unregistered orphan relay solution file")
    verified = _audit_rows(directory, manifest, rows, hashes, _row_protocol, _configuration)
    reference = _reference_audit(directory, manifest, rows)
    budget, decisions = _budget(directory, rows), _decision_audit(directory, rows)
    successful = bool(rows) and not verified["invalid_rows"] and not verified["over_budget_rows"] and not budget["aborted_run_count"]
    result = {"schema_version": 1, "kind": "relay_trial_validation", "passed": bool(successful and budget["budget_compliant"] and decisions["protocol_complete"]),
              "evidence_verified": True, "validated_at_utc": datetime.now(timezone.utc).isoformat(), "run_count": len(rows),
              "method": "Frozen solver/runner evidence, official-CSV complete structural rescoring, independent DAG timing and independently recomputed exploratory gates; no searches",
              **verified, "all_runs_successful": successful, "protocol_complete": decisions["protocol_complete"],
              "manifest_sha256": checks._sha256(directory / "manifest.json"), "runs_sha256": checks._sha256(directory / "runs.json"),
              "decisions_sha256": decisions["decisions_sha256"], "source_snapshots_sha256": hashes,
              "execution_sources_sha256": execution, "audit_script_sha256": checks._sha256(Path(__file__)),
              "independent_dag_audit": reference, "cumulative_budget": budget, "decision_audit": decisions}
    _protocol().base.write_json(target, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_protocol().OUTPUT)
    result = audit_results(parser.parse_args(argv).output_dir)
    print(f"verified {result['run_count']} relay runs; passed={result['passed']}; selected={result['decision_audit']['selected_variant']}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
