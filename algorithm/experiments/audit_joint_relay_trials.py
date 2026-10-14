"""Audit complete-solver joint relay trials without starting any search.

The baseline and new solver are separately fingerprinted. Route structure is
fully rescored, while relay timing is checked with the independent reference
scheduler from the earlier audited trial.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
from pathlib import Path
import zipfile

from uav_dispatch.io import load_tasks_csv
from uav_dispatch.validation import evaluate_solution

from algorithm.experiments import audit_optimization_results as checks
from algorithm.experiments.audit_relay_trials import reference_schedule


def _protocol():
    from algorithm.experiments import run_joint_relay_trials
    return run_joint_relay_trials


def _read(path):
    return _protocol().base.read_json(path)


def _cell(row):
    return tuple(row.get(k) for k in ("stage", "case_id", "variant", "seed"))


def _score(row):
    return tuple(row[field] for field in checks.SCORE_FIELDS)


def _manifest(manifest):
    protocol = _protocol()
    if (type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1
            or manifest.get("kind") != "joint_relay_trials" or manifest.get("baseline_variant") != "baseline"
            or manifest.get("baseline_commit") != protocol.BASELINE_COMMIT
            or manifest.get("input_path") != protocol.base.INPUT or manifest.get("seeds") != protocol.SEEDS
            or manifest.get("protocol") != protocol.PROTOCOL
            or any(type(seed) is not int for seeds in manifest["seeds"].values() for seed in seeds)):
        raise ValueError("Joint relay trial changed its frozen protocol, baseline or seeds")


def _archive_files(path):
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or any(not name.startswith("uav_dispatch/") or not name.endswith(".py")
                    or len(Path(name).parts) != 2 or ".." in Path(name).parts for name in names):
                raise ValueError("Unsafe or duplicate joint solver archive members")
            return {name: archive.read(name) for name in names}
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError("Invalid joint solver archive") from exc


def _sources(directory, manifest):
    protocol = _protocol()
    snapshots = manifest.get("source_snapshots")
    if not isinstance(snapshots, dict) or set(snapshots) != set(protocol.VARIANTS):
        raise ValueError("Incomplete joint solver snapshots")
    commit = protocol.BASELINE_COMMIT
    baseline = {name.removeprefix("algorithm/src/"): checks._git("show", f"{commit}:{name}")
                for name in checks._git("ls-tree", "-r", "--name-only", commit, "--", "algorithm/src/uav_dispatch").decode().splitlines()
                if name.endswith(".py")}
    if not baseline:
        raise ValueError("Baseline Git objects contain no solver modules")
    hashes = {}
    for variant, flags in protocol.VARIANTS.items():
        source = snapshots[variant]
        if (not isinstance(source, dict) or source.get("config") != flags
                or any(type(source["config"][k]) is not type(value) for k, value in flags.items())
                or source.get("source_ref") != (commit if variant == "baseline" else "working_tree")):
            raise ValueError("Joint solver configuration or source reference changed")
        path = checks._within(directory, source.get("path"), variant)
        fingerprint = checks._fingerprint(source.get("sha256"), variant)
        if checks._sha256(path) != fingerprint:
            raise ValueError("Joint solver snapshot fingerprint mismatch")
        files = _archive_files(path)
        if variant == "baseline":
            if files != baseline:
                raise ValueError("Baseline solver archive differs from frozen Git bytes")
        else:
            if set(files) != set(baseline) | {"uav_dispatch/joint_relay.py"}:
                raise ValueError("Candidate archive must retain baseline modules and add only joint_relay.py")
            allowed = {"uav_dispatch/cli.py", "uav_dispatch/__init__.py"}
            if any(files[name] != data for name, data in baseline.items() if name not in allowed):
                raise ValueError("Candidate modified an existing solver module outside CLI/export integration")
        hashes[variant] = fingerprint
    execution = manifest.get("execution_sources")
    if not isinstance(execution, dict) or set(execution) != set(protocol.EXECUTION_NAMES):
        raise ValueError("Incomplete frozen joint execution sources")
    execution_hashes = {}
    for name, source in execution.items():
        path = checks._within(directory, source.get("path"), name)
        fingerprint = checks._fingerprint(source.get("sha256"), name)
        if checks._sha256(path) != fingerprint or checks._sha256(checks.ROOT / f"algorithm/experiments/{name}.py") != fingerprint:
            raise ValueError("Archived or live joint execution source changed")
        execution_hashes[name] = fingerprint
    return hashes, execution_hashes


def _config_defaults(directory, manifest, variant):
    """Read literal frozen dataclass defaults without executing archived code."""
    path = checks._within(directory, manifest["source_snapshots"][variant]["path"], "solver config archive")
    files = _archive_files(path)
    module, name = ("alns.py", "ALNSConfig") if variant == "baseline" else ("joint_relay.py", "JointRelayConfig")
    tree = ast.parse(files[f"uav_dispatch/{module}"].decode("utf-8"))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    if len(classes) != 1:
        raise ValueError("Frozen solver configuration class is missing or duplicated")
    fields = {}
    for node in classes[0].body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            try:
                fields[node.target.id] = ast.literal_eval(node.value)
            except (ValueError, TypeError) as exc:
                raise ValueError("Frozen solver config default must be a literal") from exc
    if not fields:
        raise ValueError("Frozen solver configuration has no literal fields")
    return fields


def _row_protocol(row):
    protocol = _protocol()
    stage, case, variant, seed = _cell(row)
    if (stage not in protocol.SEEDS or case not in protocol.CASES[stage] or variant not in protocol.VARIANTS
            or type(seed) is not int or seed not in protocol.SEEDS[stage]
            or row.get("budget_seconds") != protocol.BUDGETS[stage][case]):
        raise ValueError("Joint run violates a frozen experiment cell or budget")
    expected_scenario = case.split("__", 1)[1] if "__" in case else "pure_direct"
    if row.get("scenario") != expected_scenario:
        raise ValueError("Joint run case/scenario mismatch")


def _case_problem(row, tasks):
    protocol = _protocol()
    spec = protocol.PROTOCOL["small" if row["case_id"] == protocol.SMALL else "fleet"]
    expected = {"task_count": spec["tasks"], "drone_count": spec["drones"],
                "max_tasks_per_drone": spec["K"], "relay_count": spec["stations"]}
    if any(row.get(key) != value or type(row.get(key)) is not int for key, value in expected.items()):
        raise ValueError("Joint run dimensions disagree with frozen case")
    selected = tasks[:spec["tasks"]]
    if len(selected) != spec["tasks"]:
        raise ValueError("Official input does not contain all declared tasks")
    homes = row["scenario"] == "direct_relay_stations"
    return checks.build_scenario_problem(selected,
        dict(id=row["scenario"], relay=True, drone_homes="stations" if homes else "origin", warmup=.8),
        drones=spec["drones"], max_tasks=spec["K"], relay_count=spec["stations"], relay_location_seed=42)[0]


def _configuration(row, payload, defaults):
    requested = dict(max_iterations=80 if row["stage"] == "smoke" else 10_000_000,
                     seed=row["seed"], candidate_limit=48 if row["variant"] == "baseline" else 12,
                     time_limit_seconds=max(1e-6, row["budget_seconds"] - row["construction_seconds"] - 1.))
    if row["variant"] == "baseline":
        homes = row["scenario"] == "direct_relay_stations"
        requested.update(relay_enabled=True, relay_direct_warmup_fraction=.8,
                         enable_home_seed=homes, enable_home_bias=homes)
    explicit = row.get("requested_config")
    if not isinstance(explicit, dict) or set(explicit) != set(requested):
        raise ValueError("Joint requested config differs from fixed parameters")
    for key, value in requested.items():
        if key == "time_limit_seconds":
            checks._same(value, explicit[key], "joint configured time")
        elif explicit[key] != value or type(explicit[key]) is not type(value):
            raise ValueError("Joint requested config differs from fixed parameters")
    expected = {**defaults, **requested}
    config = row.get("config")
    if not isinstance(config, dict) or set(config) != set(expected):
        raise ValueError("Full joint config differs from archived dataclass fields")
    for key, value in expected.items():
        if key == "time_limit_seconds":
            checks._same(value, config[key], "joint full configured time")
        elif config[key] != value or type(config[key]) is not type(value):
            raise ValueError(f"Full joint config differs from frozen value: {key}")
    metadata = payload.get("metadata", {})
    method = "C2-Lex-ALNS-Staged" if row["variant"] == "baseline" else "Joint-Relay-VNS"
    if row.get("solver_method") != method or metadata.get("method") != method:
        raise ValueError("Joint run solver identity differs from frozen variant")
    if metadata.get("seed") != row["seed"] or type(metadata.get("seed")) is not int or metadata.get("relay_enabled") is not True:
        raise ValueError("Joint solver metadata disagrees with seed or relay scenario")
    checks._same(requested["time_limit_seconds"], metadata.get("time_limit_seconds"), "joint metadata time")
    if row["variant"] == "joint":
        if metadata.get("direct_warmup") is not False or metadata.get("staged_relay", False) is not False:
            raise ValueError("Whole joint solver must not use DIRECT warmup or ALNS stages")
        if any(metadata.get(key) is not None for key in ("relay_warmup_score", "relay_search_final_score")):
            raise ValueError("Whole joint solver exported staged relay scores")
    elif metadata.get("staged_relay") is not True or metadata.get("relay_direct_warmup_fraction") != .8:
        raise ValueError("Baseline must retain its staged 80% DIRECT warmup")
    if row["iterations"] > requested["max_iterations"]:
        raise ValueError("Joint run exceeded its iteration cap")
    initial = row.get("initial_score")
    if not isinstance(initial, dict) or set(initial) != set(checks.SCORE_FIELDS):
        raise ValueError("Joint run requires the complete shared construction score")
    initial_score = checks._score([initial[field] for field in checks.SCORE_FIELDS], "shared construction score")
    saved_initial = checks._score(metadata.get("initial_score"), "solver initial score")
    for expected_value, saved_value in zip(initial_score, saved_initial, strict=True):
        checks._same(expected_value, saved_value, "joint solver initial score")
    if _score(row) > initial_score:
        raise ValueError("Joint solver discarded a better shared initial incumbent")
    raw_trajectory = metadata.get("best_trajectory")
    if not isinstance(raw_trajectory, list) or len(raw_trajectory) != len(row["best_trajectory"]):
        raise ValueError("Joint trajectory does not match solver metadata")
    if not raw_trajectory or any(not isinstance(point, list) or len(point) != 5 for point in raw_trajectory):
        raise ValueError("Joint trajectory metadata has an invalid point")
    for value, saved in zip(initial_score, raw_trajectory[0][2:], strict=True):
        checks._same(value, saved, "joint initial trajectory score")
    for raw, saved in zip(raw_trajectory, row["best_trajectory"], strict=True):
        if raw[0] != saved[0] or raw[2:] != saved[2:]:
            raise ValueError("Joint trajectory search decisions differ from metadata")
        checks._same(raw[1] + row["construction_seconds"], saved[1], "joint trajectory elapsed")
    for field in ("profiling", "neighborhood_statistics"):
        expected_statistics = metadata.get(field, metadata.get("joint_search", {})) if field == "neighborhood_statistics" else metadata.get(field, {})
        if row.get(field) != expected_statistics:
            raise ValueError(f"Joint report {field} differs from solver metadata")
    if row.get("relay_statistics") != payload.get("relay"):
        raise ValueError("Joint row relay statistics differ from full solution")


def _audit_solution(problem, payload, row, evaluation):
    # The earlier common checker encodes K=25. Check the true K separately,
    # then adapt only its display-header expectation; evaluation still uses
    # the actual K=4 small case or K=25 official fleet case throughout.
    if payload.get("problem", {}).get("max_tasks_per_drone") != problem.max_tasks_per_drone:
        raise ValueError("Saved joint K differs from frozen case")
    adapted = {**payload, "problem": {**payload["problem"], "max_tasks_per_drone": 25}}
    checks._audit_solution(problem, adapted, row, evaluation)


def _pairs(rows, stage, case):
    protocol = _protocol()
    index = {(row["variant"], row["seed"]): row for row in rows if row["stage"] == stage
             and row["case_id"] == case and row["valid"] and row["budget_compliant"]}
    return [(index["baseline", seed], index["joint", seed]) for seed in protocol.SEEDS[stage]
            if ("baseline", seed) in index and ("joint", seed) in index]


def _complete(rows, stage):
    protocol = _protocol()
    return all({b["seed"] for b, _ in _pairs(rows, stage, case)} == set(protocol.SEEDS[stage])
               for case in protocol.CASES[stage])


def _wins(matched):
    return sum(_score(candidate) < _score(baseline) for baseline, candidate in matched)


def _gates(rows):
    protocol = _protocol()
    smoke_stage = "smoke" if "smoke" in protocol.SEEDS else "compatibility"
    smoke = _complete(rows, smoke_stage)
    screen = smoke and _complete(rows, "screen") and any(_wins(_pairs(rows, "screen", case)) for case in protocol.CASES["screen"])
    medium = screen and _complete(rows, "medium") and _wins(_pairs(rows, "medium", protocol.PRIMARY)) >= 2
    return {"smoke": smoke, "screen": screen, "medium": medium, "selected_variant": "joint" if screen else None,
            "default_eligible": medium, "exploratory_promotion": True,
            "smoke_comparison": "Feasibility and budget only; different solvers are not required to have equivalent search decisions"}


def _budget(directory, rows):
    protocol = _protocol()
    stages = {stage: {"completed_seconds": 0., "aborted_seconds": 0., "cap_seconds": cap}
              for stage, cap in protocol.CAPS.items()}
    cells = {_cell(row) for row in rows}
    for row in rows:
        stages[row["stage"]]["completed_seconds"] += checks._number(row["end_to_end_seconds"], "joint elapsed")
    path = directory / "execution_notes.json"
    attempts = _read(path).get("aborted_runs", []) if path.exists() else []
    if not isinstance(attempts, list):
        raise ValueError("Invalid joint aborted attempt list")
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise ValueError("Expected joint aborted attempt object")
        stage, case, _, _ = _cell(attempt)
        scenario = case.split("__", 1)[1] if isinstance(case, str) and "__" in case else "pure_direct"
        _row_protocol({**attempt, "scenario": scenario, "budget_seconds": protocol.BUDGETS.get(stage, {}).get(case)})
        if _cell(attempt) in cells:
            raise ValueError("Completed or aborted joint cell duplicated")
        cells.add(_cell(attempt))
        stages[stage]["aborted_seconds"] += checks._number(attempt.get("process_elapsed_seconds"), "joint aborted elapsed")
    for bucket in stages.values():
        bucket["total_seconds"] = bucket["completed_seconds"] + bucket["aborted_seconds"]
        bucket["budget_compliant"] = bucket["total_seconds"] <= bucket["cap_seconds"] + 1e-9
    total = sum(bucket["total_seconds"] for bucket in stages.values())
    reserve = total <= protocol.TOTAL_SECONDS - protocol.RESERVED_SECONDS + 1e-9
    quota = sum(len(protocol.SEEDS[stage]) * len(protocol.CASES[stage]) * len(protocol.VARIANTS) for stage in protocol.SEEDS)
    return {"stages": stages, "total_seconds": total, "total_limit_seconds": protocol.TOTAL_SECONDS,
            "reserved_seconds": protocol.RESERVED_SECONDS, "reserve_preserved": reserve,
            "aborted_run_count": len(attempts), "attempt_count": len(rows) + len(attempts), "maximum_planned_runs": quota,
            "quota_compliant": len(rows) + len(attempts) <= quota,
            "execution_notes_sha256": checks._sha256(path) if path.exists() else None,
            "budget_compliant": reserve and len(rows) + len(attempts) <= quota and all(bucket["budget_compliant"] for bucket in stages.values())}


def _decision_audit(directory, rows):
    protocol = _protocol()
    path = directory / "decisions.json"
    decisions = _read(path)
    if not isinstance(decisions, list) or any(not isinstance(item, dict) for item in decisions):
        raise ValueError("Invalid joint decision list")
    stages = {item.get("stage"): item for item in decisions}
    if len(stages) != len(decisions) or not set(stages).issubset({"screen", "medium"}):
        raise ValueError("Duplicate or unknown joint decision")
    if "medium" in stages and "screen" not in stages:
        raise ValueError("Joint medium decision is missing its screening prerequisite")
    gates = _gates(rows)
    permitted = {"smoke": list(protocol.VARIANTS), "screen": list(protocol.VARIANTS),
                 "medium": list(protocol.VARIANTS) if gates["screen"] else []}
    for stage, variants in permitted.items():
        stage_rows = [row for row in rows if row["stage"] == stage]
        if any(row["variant"] not in variants for row in stage_rows):
            raise ValueError("Joint solver entered validation without screening qualification")
        for row in stage_rows:
            order = variants if protocol.SEEDS[stage].index(row["seed"]) % 2 == 0 else list(reversed(variants))
            if type(row.get("order")) is not int or row["order"] != order.index(row["variant"]):
                raise ValueError("Joint run order violates paired AB/BA scheduling")
        completed = "screen" in stages if stage in {"smoke", "screen"} else "medium" in stages
        if completed:
            wanted = {(stage, case, variant, seed) for case in protocol.CASES[stage]
                      for variant in variants for seed in protocol.SEEDS[stage]}
            if {_cell(row) for row in stage_rows} != wanted:
                raise ValueError(f"Completed joint stage has missing or unexpected cells: {stage}")
    expected = {"screen": {"selected_variant": gates["selected_variant"], "passed": gates["screen"],
                           "reason": protocol.PROTOCOL["screen_gate"]},
                "medium": {"selected_variant": gates["selected_variant"], "passed": gates["medium"],
                           "reason": protocol.PROTOCOL["default_gate"] if gates["screen"] else "没有初筛胜利，停止扩大"}}
    if not gates["screen"]:
        expected["medium"]["skipped"] = True
    for stage, item in stages.items():
        if any(item.get(key) != value or type(item.get(key)) is not type(value) for key, value in expected[stage].items()):
            raise ValueError(f"Joint stage decision differs from independent gate: {stage}")
    return {**gates, "protocol_complete": set(stages) == {"screen", "medium"}, "decisions_sha256": checks._sha256(path)}


def _reference_audit(problem, payload, row):
    routes = [route["encoded_visits"] for route in payload["routes"]]
    reference = reference_schedule(problem, routes)
    if set(reference["delivery_times_min"]) != set(problem.task_ids):
        raise ValueError("Reference scheduler does not complete every original task")
    for field in checks.SCORE_FIELDS:
        checks._same(getattr(reference["score"], field), row[field], f"joint reference {field}")
    for task_id, at in reference["delivery_times_min"].items():
        checks._same(at, payload["delivery_times_min"].get(str(task_id)), f"joint reference delivery {task_id}")
    for route_index, route in enumerate(payload["routes"]):
        if len(route.get("visits", [])) != len(routes[route_index]):
            raise ValueError("Saved joint event list differs from encoded route")
        for position, visit in enumerate(route["visits"]):
            checks._same(reference["event_times_min"][route_index, position], visit.get("arrival_time_min"), "joint reference event time")
    stats = payload.get("relay", {})
    if stats.get("relay_count") != len(problem.relay_stations) or stats.get("relay_coordinates") != [
            [station.id, station.x, station.y] for station in problem.relay_stations]:
        raise ValueError("Saved joint station coordinates differ from frozen scenario")
    for field in ("relay_task_count", "cross_uav_handoff_count", "same_uav_relay_count"):
        checks._same(reference[field], stats.get(field), f"joint reference {field}")
    checks._same(reference["total_waiting_min"], stats.get("total_relay_waiting_time"), "joint reference total wait")
    checks._same(reference["max_waiting_min"], stats.get("max_relay_waiting_time"), "joint reference max wait")
    checks._same(reference["total_waiting_min"] / reference["relay_task_count"] if reference["relay_task_count"] else 0.,
                 stats.get("average_relay_waiting_time"), "joint reference mean wait")
    checks._same(reference["relay_task_count"] / len(problem.tasks), stats.get("relay_share"), "joint reference relay share")
    if stats.get("direct_task_count") != len(problem.tasks) - reference["relay_task_count"]:
        raise ValueError("Saved joint direct count differs from signed visits")
    if set(payload.get("task_plans", {})) != {str(task_id) for task_id in problem.task_ids}:
        raise ValueError("Saved joint task-plan set differs from official tasks")
    for task_id in problem.task_ids:
        plan = reference["relay_tasks"].get(task_id)
        expected = {"mode": "DIRECT", "relay_id": None, "leg_ids": []} if plan is None else {
            "mode": "RELAY", "relay_id": plan["relay_id"], "leg_ids": [plan["in_leg"], plan["out_leg"]]}
        if payload["task_plans"].get(str(task_id)) != expected:
            raise ValueError("Saved joint task plan differs from signed visits")
    stations = stats.get("relay_usage_per_station", [])
    if [station.get("station_id") for station in stations] != [station.id for station in problem.relay_stations]:
        raise ValueError("Saved joint station utilization has missing or reordered stations")
    for station, saved in zip(problem.relay_stations, stations, strict=True):
        if saved.get("x") != station.x or saved.get("y") != station.y:
            raise ValueError("Saved joint station utilization coordinates changed")
        task_ids = [task_id for task_id, plan in reference["relay_tasks"].items() if plan["relay_id"] == station.id]
        for field in ("task_count", "drop_count", "pickup_count"):
            checks._same(len(task_ids), saved.get(field), f"joint reference station {field}")
        checks._same(sum(reference["waiting_by_task"].get(task_id, 0.) for task_id in task_ids) / len(task_ids)
                     if task_ids else 0., saved.get("average_wait"), "joint reference station wait")
    return reference


def _audit_rows(directory, manifest, rows, hashes):
    input_path = checks._within(checks.ROOT, manifest["input_path"], "official input")
    input_hash = checks._fingerprint(manifest.get("input_sha256"), "official input")
    if checks._sha256(input_path) != input_hash or input_hash != checks._sha256(checks.DEFAULT_INPUT):
        raise ValueError("Joint input fingerprint differs from official CSV")
    tasks = tuple(load_tasks_csv(input_path))
    cells, initial_hashes, initial_scores, cache, referenced = set(), {}, {}, {}, set()
    defaults = {variant: _config_defaults(directory, manifest, variant) for variant in _protocol().VARIANTS}
    invalid, over_budget = [], []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError("Expected joint run object")
        _row_protocol(row)
        cell = _cell(row)
        if cell in cells:
            raise ValueError("Duplicate joint experiment cell")
        cells.add(cell)
        if row.get("source_sha256") != hashes[row["variant"]]:
            raise ValueError("Joint run source disagrees with snapshot")
        pair = row["stage"], row["case_id"], row["seed"]
        fingerprint = checks._fingerprint(row.get("initial_routes_sha256"), "shared initial routes")
        if initial_hashes.setdefault(pair, fingerprint) != fingerprint:
            raise ValueError("Joint paired solvers must share initial routes")
        initial_fingerprint = _protocol().base.value_hash(row.get("initial_score"))
        if initial_scores.setdefault(pair, initial_fingerprint) != initial_fingerprint:
            raise ValueError("Joint paired solvers must share the construction score")
        run_id = "__".join(str(value) for value in cell)
        if row.get("run_id") != run_id or row.get("solution_file") != f"run_solutions/{run_id}.json":
            raise ValueError("Joint solution path differs from experiment cell")
        problem = _case_problem(row, tasks)
        path = checks._within(directory, row["solution_file"], "joint solution")
        fingerprint = checks._fingerprint(row.get("solution_sha256"), "joint solution")
        if checks._sha256(path) != fingerprint:
            raise ValueError("Saved joint solution fingerprint mismatch")
        referenced.add(path)
        payload = _read(path)
        routes = payload.get("routes", [])
        if [route.get("drone_id") for route in routes] != list(range(1, problem.drone_count + 1)):
            raise ValueError("Joint solution drone IDs are missing or reordered")
        key = row["case_id"], fingerprint
        if key not in cache:
            cache[key] = evaluate_solution(problem, [route["encoded_visits"] for route in routes], _force_full=True)
        evaluation = cache[key]
        _audit_solution(problem, payload, row, evaluation)
        if row.get("violations") != list(evaluation.violations):
            raise ValueError("Joint run violations differ from complete evaluation")
        compliant = checks._audit_runtime(row, payload)
        _configuration(row, payload, defaults[row["variant"]])
        checks._audit_trajectory(row, payload)
        _reference_audit(problem, payload, row)
        if not evaluation.valid:
            invalid.append(index)
        if not compliant:
            over_budget.append(index)
    if any(path.resolve() not in referenced for path in (directory / "run_solutions").glob("*.json")):
        raise ValueError("Unregistered orphan joint solution file")
    return {"input_sha256": input_hash, "validated_rows": len(rows), "validated_unique_solutions": len(cache),
            "invalid_rows": invalid, "over_budget_rows": over_budget,
            "valid_on_budget_runs": len(rows) - len(set(invalid + over_budget)),
            "independent_dag_audit": {"validated_rows": len(rows), "validated_unique_solutions": len(cache),
                "method": "Independent dictionary-based longest-path scheduler rebuilt from signed visits; no production DAG or plan-index calls"}}


def audit_results(output_dir):
    directory = Path(output_dir).resolve()
    target = directory / "validation.json"
    target.unlink(missing_ok=True)
    manifest, rows = _read(directory / "manifest.json"), _read(directory / "runs.json")
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ValueError("Expected joint manifest object and rows list")
    _manifest(manifest)
    hashes, execution = _sources(directory, manifest)
    verified = _audit_rows(directory, manifest, rows, hashes)
    budget, decisions = _budget(directory, rows), _decision_audit(directory, rows)
    successful = bool(rows) and not verified["invalid_rows"] and not verified["over_budget_rows"] and not budget["aborted_run_count"]
    from algorithm.experiments import audit_relay_trials as reference_audit
    result = {"schema_version": 1, "kind": "joint_relay_trial_validation",
              "passed": bool(successful and budget["budget_compliant"] and decisions["protocol_complete"]),
              "evidence_verified": True, "validated_at_utc": datetime.now(timezone.utc).isoformat(), "run_count": len(rows),
              "method": "Frozen distinct solvers and full dataclass configs, shared initial evidence, official-CSV full structural rescoring, independent DAG timing and exploratory gates; no searches or ALNS decision-equivalence requirement",
              **verified, "all_runs_successful": successful, "protocol_complete": decisions["protocol_complete"],
              "manifest_sha256": checks._sha256(directory / "manifest.json"), "runs_sha256": checks._sha256(directory / "runs.json"),
              "decisions_sha256": decisions["decisions_sha256"], "source_snapshots_sha256": hashes,
              "execution_sources_sha256": execution, "audit_script_sha256": checks._sha256(Path(__file__)),
              "audit_dependencies": {"audit_optimization_results": checks._sha256(Path(checks.__file__)),
                                     "audit_relay_trials_reference": checks._sha256(Path(reference_audit.__file__))},
              "cumulative_budget": budget, "decision_audit": decisions}
    _protocol().base.write_json(target, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_protocol().OUTPUT)
    result = audit_results(parser.parse_args(argv).output_dir)
    print(f"verified {result['run_count']} joint runs; passed={result['passed']}; screen={result['decision_audit']['screen']}; default={result['decision_audit']['default_eligible']}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
