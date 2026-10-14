"""Audit the four-scenario global/relay experiment without launching searches.

Only the new runner's frozen protocol is used. Generic structural, timing and
fingerprint checks are shared; relay clocks are independently rebuilt by the
dictionary-based reference DAG scheduler.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import zipfile

from uav_dispatch.io import load_tasks_csv

from algorithm.experiments import audit_optimization_results as checks
from algorithm.experiments.audit_compact_trials import _audit_rows
from algorithm.experiments.audit_relay_trials import reference_schedule


def _protocol():
    from algorithm.experiments import run_global_relay_followup
    return run_global_relay_followup


def _read(path):
    return _protocol().base.read_json(path)


def _cell(row):
    return tuple(row.get(k) for k in ("stage", "case_id", "variant", "seed"))


def _score(row):
    return tuple(row[k] for k in checks.SCORE_FIELDS)


def _manifest(manifest):
    p = _protocol()
    if (type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1
            or manifest.get("kind") != "global_relay_followup"
            or manifest.get("baseline_commit") != p.BASELINE_COMMIT
            or manifest.get("input_path") != p.base.INPUT
            or manifest.get("protocol") != p.PROTOCOL or manifest.get("seeds") != p.SEEDS
            or manifest.get("labels") != p.LABELS
            or any(type(s) is not int for seeds in manifest["seeds"].values() for s in seeds)):
        raise ValueError("Global relay trial changed its frozen protocol, baseline or seeds")


def _sources(directory, manifest):
    p = _protocol()
    snapshots = manifest.get("source_snapshots")
    if not isinstance(snapshots, dict) or set(snapshots) != set(p.VARIANTS):
        raise ValueError("Incomplete frozen global solver snapshots")
    baseline = {name.removeprefix("algorithm/src/"): checks._git("show", f"{p.BASELINE_COMMIT}:{name}")
                for name in checks._git("ls-tree", "-r", "--name-only", p.BASELINE_COMMIT,
                                        "--", "algorithm/src/uav_dispatch").decode().splitlines()
                if name.endswith(".py")}
    if not baseline:
        raise ValueError("Baseline Git tree has no solver modules")
    extra = {"uav_dispatch/incumbent_polish.py"}
    hashes = {}
    for variant, flags in p.VARIANTS.items():
        source = snapshots[variant]
        if (source.get("config") != flags or any(type(source["config"][k]) is not type(v) for k, v in flags.items())
                or source.get("source_ref") != (p.BASELINE_COMMIT if variant == "baseline" else "working_tree")):
            raise ValueError("Global solver configuration or source reference changed")
        path = checks._within(directory, source.get("path"), variant)
        fingerprint = checks._fingerprint(source.get("sha256"), variant)
        if checks._sha256(path) != fingerprint:
            raise ValueError("Global solver source snapshot fingerprint mismatch")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(set(names)) != len(names) or any(not n.startswith("uav_dispatch/") or not n.endswith(".py")
                    or len(Path(n).parts) != 2 or ".." in Path(n).parts for n in names):
                raise ValueError("Unsafe or duplicate solver archive members")
            files = {n: archive.read(n) for n in names}
        expected_names = set(baseline) if variant == "baseline" else set(baseline) | extra
        if set(files) != expected_names or (variant == "baseline" and files != baseline):
            raise ValueError("Solver module set or baseline Git bytes disagree")
        if variant != "baseline" and any(files[n] != data for n, data in baseline.items()
                                         if n != "uav_dispatch/alns.py"):
            raise ValueError("Candidate changed an unrelated baseline solver module")
        hashes[variant] = fingerprint
    if (snapshots["terminal"]["path"], hashes["terminal"]) != (snapshots["rebalanced"]["path"], hashes["rebalanced"]):
        raise ValueError("Candidate arms must share one frozen source snapshot")
    execution = manifest.get("execution_sources")
    if not isinstance(execution, dict) or set(execution) != set(p.EXECUTION_NAMES):
        raise ValueError("Incomplete frozen execution sources")
    execution_hashes = {}
    for name, source in execution.items():
        path = checks._within(directory, source.get("path"), name)
        fingerprint = checks._fingerprint(source.get("sha256"), name)
        if (checks._sha256(path) != fingerprint
                or checks._sha256(checks.ROOT / f"algorithm/experiments/{name}.py") != fingerprint):
            raise ValueError("Archived or live execution source changed")
        execution_hashes[name] = fingerprint
    if manifest.get("runner_sha256") != execution_hashes["run_optimization_experiments"]:
        raise ValueError("Worker fingerprint mismatch")
    return hashes, execution_hashes


def _row_protocol(row):
    p = _protocol()
    stage, case, variant, seed = _cell(row)
    cases = ["single25"] if stage == "compatibility" else p.CASES
    if (stage not in p.SEEDS or case not in cases or variant not in p.VARIANTS
            or type(seed) is not int or seed not in p.SEEDS[stage]
            or isinstance(row.get("budget_seconds"), bool) or row.get("budget_seconds") != p.BUDGETS[stage]
            or (variant == "rebalanced" and stage != "compatibility" and case not in p.RELAY_CASES)):
        raise ValueError("Run violates a frozen cell or budget")
    if row.get("scenario") != (case.split("__", 1)[1] if "__" in case else "pure_direct"):
        raise ValueError("Run case/scenario mismatch")


def _configuration(row, payload):
    p = _protocol()
    relay = row["scenario"] in {"direct_relay", "direct_relay_stations"}
    homes = row["scenario"] in {"direct_stations", "direct_relay_stations"}
    expected = {"max_iterations": 100 if row["stage"] == "compatibility" else 10_000_000,
                "time_limit_seconds": max(1e-6, row["budget_seconds"] - row["construction_seconds"] - 1.),
                "seed": row["seed"], "candidate_limit": 48, "relay_enabled": relay,
                "relay_direct_warmup_fraction": .8, "enable_home_seed": homes, "enable_home_bias": homes,
                **p.VARIANTS[row["variant"]]}
    config = row.get("config")
    if not isinstance(config, dict) or set(config) != set(expected) or any(
            config[k] != v or type(config[k]) is not type(v) for k, v in expected.items() if k != "time_limit_seconds"):
        raise ValueError("Run configuration differs from frozen variant")
    checks._same(expected["time_limit_seconds"], config["time_limit_seconds"], "configured time")
    metadata = payload.get("metadata", {})
    for key in ("seed", "relay_enabled", "enable_home_seed"):
        if metadata.get(key) != expected[key] or type(metadata.get(key)) is not type(expected[key]):
            raise ValueError(f"Metadata disagrees with execution: {key}")
    # The numeric warmup fraction is emitted only by staged runs. Candidate
    # compatibility is single-UAV DIRECT, so its absent fraction is normal.
    for key, value in p.VARIANTS[row["variant"]].items():
        if key == "relay_direct_warmup_fraction" and not relay:
            continue
        if metadata.get(key) != value or type(metadata.get(key)) is not type(value):
            raise ValueError(f"Metadata disagrees with execution: {key}")
    for key in ("enable_tail_exchange", "enable_home_reassignment", "enable_relay_downstream_refine"):
        if metadata.get(key, False) is not False:
            raise ValueError(f"Unexpected inherited experimental feature: {key}")
    for key in ("enable_terminal_polish", "enable_relay_budget_rebalance", "enable_relay_seed_checkpoint"):
        if metadata.get(key, False) is not p.VARIANTS[row["variant"]].get(key, False):
            raise ValueError(f"Unexpected followup feature metadata: {key}")
    checks._same(expected["time_limit_seconds"], metadata.get("time_limit_seconds"), "metadata time")
    effective_fraction = .95 if expected.get("enable_relay_budget_rebalance", False) else .8
    if relay and (metadata.get("staged_relay") is not True or
                  metadata.get("relay_direct_warmup_fraction") != effective_fraction):
        raise ValueError("Relay execution differs from frozen DIRECT warmup fraction")
    if row["iterations"] > expected["max_iterations"]:
        raise ValueError("Run exceeded its iteration cap")
    if payload.get("source") != p.base.INPUT or row.get("profiling") != metadata.get("profiling", {}):
        raise ValueError("Source or profiling metadata disagrees with execution")
    if row.get("phase_statistics") != {key: metadata.get(key) for key in p.PHASE_FIELDS}:
        raise ValueError("Phase statistics row differs from saved solution metadata")
    if metadata.get("relay_seed_checkpoint_active", False) is not (
            relay and p.VARIANTS[row["variant"]].get("enable_relay_seed_checkpoint", False)):
        raise ValueError("Seed checkpoint activation disagrees with frozen variant")
    _terminal_metadata(row, payload, expected)
    if metadata.get("initial_score") is not None:
        initial = checks._score(metadata["initial_score"], "initial score")
        for actual, saved in zip(initial, row["best_trajectory"][0][2:], strict=True):
            checks._same(actual, saved, "initial trajectory score")


def _terminal_metadata(row, payload, config):
    metadata = payload.get("metadata", {})
    terminal = metadata.get("terminal_polish", {})
    if row.get("terminal_polish") != terminal:
        raise ValueError("Terminal polish row differs from saved solution metadata")
    if row["variant"] == "baseline":
        if terminal:
            raise ValueError("Historical baseline cannot report new terminal polish")
        return
    fields = {"active", "phase", "reserved_seconds", "elapsed_seconds", "accepted",
              "before_score", "after_score", "statistics"}
    if not isinstance(terminal, dict) or set(terminal) != fields:
        raise ValueError("Terminal polish metadata has missing or unexpected fields")
    active = row["drone_count"] > 1 and config["enable_terminal_polish"]
    relay = bool(config["relay_enabled"])
    expected_phase = "direct_warmup" if relay else "direct" if active else "inactive"
    if terminal["active"] is not active or terminal["phase"] != expected_phase or type(terminal["accepted"]) is not bool:
        raise ValueError("Terminal polish activation or phase disagrees with execution")
    before = checks._score(terminal["before_score"], "terminal before score")
    after = checks._score(terminal["after_score"], "terminal after score")
    if after > before or terminal["accepted"] is not (after < before):
        raise ValueError("Terminal polish must retain canonical incumbent unless strictly improved")
    target = metadata.get("relay_warmup_score") if relay else [row[k] for k in checks.SCORE_FIELDS]
    for value, expected in zip(after, checks._score(target, "terminal phase final score"), strict=True):
        checks._same(value, expected, "terminal phase final score")
    reserved = checks._number(terminal["reserved_seconds"], "terminal reserved seconds")
    elapsed = checks._number(terminal["elapsed_seconds"], "terminal elapsed seconds")
    effective_fraction = .95 if config.get("enable_relay_budget_rebalance", False) else .8
    phase_budget = config["time_limit_seconds"] * (effective_fraction if relay else 1.)
    if reserved > min(.1, phase_budget * .01) + 1e-9:
        raise ValueError("Terminal polish reserve exceeds frozen phase budget rule")
    phase_runtime = metadata.get("relay_warmup_runtime_seconds") if relay else payload.get("runtime_seconds")
    if elapsed > checks._number(phase_runtime, "terminal phase runtime") + 1e-9:
        raise ValueError("Terminal polish elapsed time exceeds its phase runtime")
    stats = terminal["statistics"]
    if not isinstance(stats, dict):
        raise ValueError("Terminal polish statistics must be an object")
    for key, value in stats.items():
        checks._number(value, f"terminal statistic {key}", integer=True)
    if not active and (terminal["accepted"] or reserved or elapsed or stats):
        raise ValueError("Inactive terminal polish cannot record work or acceptance")
    # The pre-polish incumbent must already occur in the recorded trajectory;
    # polish cannot invent an unobserved baseline to claim a larger gain.
    points = metadata.get("best_trajectory", [])
    if relay:
        points = [point for point in points if point[1] <= phase_runtime + 1e-9]
    scores = [tuple(point[2:]) for point in points]
    if before not in scores or after not in scores or not scores or scores[-1] != after:
        raise ValueError("Terminal before/after scores disagree with phase incumbent trajectory")
    if terminal["accepted"] and (len(scores) < 2 or scores[-2] != before):
        raise ValueError("Accepted terminal gain must use the immediately preceding incumbent")


def _reference_audit(directory, manifest, rows):
    tasks = tuple(load_tasks_csv(checks._within(checks.ROOT, manifest["input_path"], "official input")))
    relay_rows = 0
    for row in rows:
        problem = checks._case_problem(row, tasks)
        payload = _read(checks._within(directory, row["solution_file"], "solution"))
        routes = [r["encoded_visits"] for r in payload["routes"]]
        reference = reference_schedule(problem, routes)
        if set(reference["delivery_times_min"]) != set(problem.task_ids):
            raise ValueError("Reference scheduler does not deliver every original task")
        for field in checks.SCORE_FIELDS:
            checks._same(getattr(reference["score"], field), row[field], f"reference {field}")
        for task_id, delivered in reference["delivery_times_min"].items():
            checks._same(delivered, payload["delivery_times_min"].get(str(task_id)), f"reference delivery {task_id}")
        for index, route in enumerate(payload["routes"]):
            if len(route.get("visits", [])) != len(routes[index]):
                raise ValueError("Saved event list differs from signed route")
            for position, visit in enumerate(route["visits"]):
                checks._same(reference["event_times_min"][index, position], visit.get("arrival_time_min"), "reference event time")
        stats = payload.get("relay", {})
        if stats.get("relay_count") != len(problem.relay_stations) or stats.get("relay_coordinates") != [
                [s.id, s.x, s.y] for s in problem.relay_stations]:
            raise ValueError("Saved relay coordinates disagree with frozen scenario")
        for field in ("relay_task_count", "cross_uav_handoff_count", "same_uav_relay_count"):
            checks._same(reference[field], stats.get(field), f"reference {field}")
        count = reference["relay_task_count"]
        checks._same(reference["total_waiting_min"], stats.get("total_relay_waiting_time"), "reference total wait")
        checks._same(reference["max_waiting_min"], stats.get("max_relay_waiting_time"), "reference max wait")
        checks._same(reference["total_waiting_min"] / count if count else 0.,
                     stats.get("average_relay_waiting_time"), "reference mean wait")
        if stats.get("direct_task_count") != len(problem.tasks) - count:
            raise ValueError("Saved direct count differs from signed visits")
        checks._same(count / len(problem.tasks), stats.get("relay_share"), "reference relay share")
        plans = payload.get("task_plans", {})
        if set(plans) != {str(t) for t in problem.task_ids}:
            raise ValueError("Saved task-plan set differs from original tasks")
        for task_id in problem.task_ids:
            plan = reference["relay_tasks"].get(task_id)
            expected = {"mode": "DIRECT", "relay_id": None, "leg_ids": []} if plan is None else {
                "mode": "RELAY", "relay_id": plan["relay_id"], "leg_ids": [plan["in_leg"], plan["out_leg"]]}
            if plans[str(task_id)] != expected:
                raise ValueError("Saved task plan disagrees with signed visits")
        stations = stats.get("relay_usage_per_station", [])
        if [s.get("station_id") for s in stations] != [s.id for s in problem.relay_stations]:
            raise ValueError("Saved station utilization has missing or reordered stations")
        for station, saved in zip(problem.relay_stations, stations, strict=True):
            if saved.get("x") != station.x or saved.get("y") != station.y:
                raise ValueError("Saved station coordinates disagree")
            task_ids = [t for t, plan in reference["relay_tasks"].items() if plan["relay_id"] == station.id]
            for field in ("task_count", "drop_count", "pickup_count"):
                checks._same(len(task_ids), saved.get(field), f"reference station {field}")
            checks._same(sum(reference["waiting_by_task"].get(t, 0.) for t in task_ids) / len(task_ids)
                         if task_ids else 0., saved.get("average_wait"), "reference station wait")
        metadata = payload.get("metadata", {})
        if row.get("relay_statistics") != metadata.get("relay", {}):
            raise ValueError("Row relay statistics differ from solution metadata")
        if row.get("relay_warmup_score") != metadata.get("relay_warmup_score"):
            raise ValueError("Row warmup score differs from solution metadata")
        if row["scenario"] in {"direct_relay", "direct_relay_stations"}:
            if metadata.get("relay") != stats:
                raise ValueError("Relay metadata differs from exported statistics")
            warm = checks._score(metadata.get("relay_warmup_score"), "relay warmup score")
            final = checks._score(metadata.get("relay_search_final_score"), "relay final score")
            if metadata.get("relay_retained_warmup_incumbent") is not (final > warm):
                raise ValueError("Relay retention disagrees with phase scores")
            for value, field in zip(min(warm, final), checks.SCORE_FIELDS, strict=True):
                checks._same(value, row[field], "relay retained phase score")
            warm_time = checks._number(metadata.get("relay_warmup_runtime_seconds"), "warmup runtime")
            relay_time = checks._number(metadata.get("relay_search_runtime_seconds"), "relay runtime")
            checks._same(warm_time + relay_time, payload.get("runtime_seconds"), "combined phase runtime")
            if metadata.get("relay_warmup_iterations", 0) + metadata.get("relay_search_iterations", 0) != row["iterations"]:
                raise ValueError("Relay phase iteration counts disagree")
            relay_rows += 1
    return {"validated_rows": len(rows), "relay_enabled_rows": relay_rows,
            "method": "Independent dictionary-based longest-path DAG schedule, per-event/per-delivery times and aircraft waits; no production relay scheduler calls"}


def _planned(stage, selected="terminal"):
    p = _protocol()
    result = []
    for index, seed in enumerate(p.SEEDS[stage]):
        for case in (["single25"] if stage == "compatibility" else p.CASES):
            variants = ["baseline", "terminal", "rebalanced"] if stage == "compatibility" else ["baseline", "terminal"]
            if stage == "screen" and case in p.RELAY_CASES:
                variants.append("rebalanced")
            if stage == "validate":
                variants = ["baseline", selected if case in p.RELAY_CASES else "terminal"]
            shift = index % len(variants)
            variants = variants[shift:] + variants[:shift]
            result.extend((stage, case, v, seed) for v in variants)
    return result


def _gates(rows):
    p = _protocol()
    valid = {_cell(r): r for r in rows if r["valid"] and r["budget_compliant"]}
    def pairs(stage, cases, left="baseline", right="terminal"):
        return [(valid[stage, case, left, seed], valid[stage, case, right, seed])
                for case in cases for seed in p.SEEDS[stage]
                if (stage, case, left, seed) in valid and (stage, case, right, seed) in valid]
    def outcome(ps):
        won = sum(_score(c) < _score(b) for b, c in ps)
        tied = sum(_score(c) == _score(b) for b, c in ps)
        return {"pairs": len(ps), "wins": won, "ties": tied, "losses": len(ps) - won - tied,
                "late_delta": sum(c["late_count"] - b["late_count"] for b, c in ps)}
    compatibility = [valid[c] for c in _planned("compatibility") if c in valid]
    equivalent = (len(compatibility) == 3 and all(r["iterations"] == 100 for r in compatibility)
                  and len({r["decision_fingerprint"] for r in compatibility}) == 1)
    terminal = pairs("screen", p.CASES)
    rebalanced = pairs("screen", p.RELAY_CASES, "terminal", "rebalanced")
    comparison = outcome(rebalanced)
    selected = "rebalanced" if (comparison["pairs"] == 6 and comparison["wins"] >= 4
                                and comparison["late_delta"] <= 0) else "terminal"
    by_screen_case = {case: pairs("screen", [case], right=selected if case in p.RELAY_CASES else "terminal")
                      for case in p.CASES}
    selected_pairs = [pair for ps in by_screen_case.values() for pair in ps]
    selected_outcome = outcome(selected_pairs)
    primary = outcome(by_screen_case[p.CASES[-1]])
    screen = {"passed": equivalent and selected_outcome["pairs"] == 12 and (
                  (selected_outcome["wins"] >= 7 and selected_outcome["late_delta"] <= 0) or
                  (primary["pairs"] == 3 and primary["wins"] >= 2 and primary["late_delta"] <= 0)),
              "selected_relay_variant": selected,
              "terminal_vs_baseline": outcome(terminal),
              "rebalanced_vs_terminal": comparison,
              "selected_vs_baseline": selected_outcome,
              "selected_case_outcomes": {case: outcome(ps) for case, ps in by_screen_case.items()}}
    by_case = {case: pairs("validate", [case], right=selected if case in p.RELAY_CASES else "terminal")
               for case in p.CASES}
    validation_pairs = [pair for ps in by_case.values() for pair in ps]
    validation = outcome(validation_pairs)
    promotion = (screen["passed"] and validation["pairs"] == 12 and validation["wins"] >= 8
                 and all(len(ps) == 3 and outcome(ps)["wins"] >= 2 for ps in by_case.values())
                 and all(c["late_count"] <= b["late_count"] for b, c in validation_pairs))
    return {"compatibility": equivalent, "screen": screen, "default_eligible": promotion,
            "validation_pairs": len(validation_pairs), "validation_wins": validation["wins"],
            "validation_case_wins": {c: outcome(ps)["wins"] for c, ps in by_case.items()}}


def _decision_audit(directory, rows):
    p = _protocol()
    gates = _gates(rows)
    path = directory / "decisions.json"
    decisions = _read(path)
    if not isinstance(decisions, list) or any(not isinstance(d, dict) for d in decisions):
        raise ValueError("Invalid stage decision list")
    stages = {d.get("stage"): d for d in decisions}
    if len(stages) != len(decisions) or list(stages) not in ([], ["screen"], ["screen", "validate"]):
        raise ValueError("Duplicate, reordered or unknown stage decisions")
    expected = _planned("compatibility") + _planned("screen")
    if gates["screen"]["passed"]:
        expected += _planned("validate", gates["screen"]["selected_relay_variant"])
    actual = [_cell(r) for r in rows]
    if actual != expected[:len(actual)]:
        raise ValueError("Missing, unexpected or reordered experiment cells")
    if any(r["stage"] != "compatibility" for r in rows) and not gates["compatibility"]:
        raise ValueError("Search stages require complete fixed-iteration compatibility")
    for stage in p.SEEDS:
        stage_rows = [r for r in rows if r["stage"] == stage]
        planned = _planned(stage, gates["screen"]["selected_relay_variant"])
        for row in stage_rows:
            group = [c for c in planned if c[1] == row["case_id"] and c[3] == row["seed"]]
            if type(row.get("order")) is not int or row["order"] != group.index(_cell(row)):
                raise ValueError("Recorded AB/BA order differs from frozen order")
    if "screen" in stages:
        if {_cell(r) for r in rows if r["stage"] == "screen"} != set(_planned("screen")):
            raise ValueError("Screen decision requires all prespecified seeds and cases")
        if stages["screen"] != {"stage": "screen", **gates["screen"]}:
            raise ValueError("Screen decision differs from independent gate")
    if "validate" in stages:
        if gates["screen"]["passed"]:
            if {_cell(r) for r in rows if r["stage"] == "validate"} != set(_planned("validate", gates["screen"]["selected_relay_variant"])):
                raise ValueError("Validation decision requires all prespecified seeds and cases")
            expected_decision = {"stage": "validate", "passed": gates["default_eligible"],
                                 "selected_relay_variant": gates["screen"]["selected_relay_variant"]}
        else:
            expected_decision = {"stage": "validate", "passed": False, "skipped": True,
                                 "reason": "四场景完整规模初筛未过门槛，不启动长测"}
        if stages["validate"] != expected_decision:
            raise ValueError("Validation decision differs from independent gate")
    return {**gates, "protocol_complete": len(actual) == len(expected) and set(stages) == {"screen", "validate"},
            "decisions_sha256": checks._sha256(path)}


def _budget(directory, rows, hashes):
    p = _protocol()
    completed = sum(checks._number(r["end_to_end_seconds"], "elapsed") for r in rows)
    cells = {_cell(r) for r in rows}
    path = directory / "execution_notes.json"
    notes = _read(path) if path.exists() else {}
    attempts = notes.get("aborted_runs", [])
    if not isinstance(attempts, list):
        raise ValueError("Invalid aborted attempt list")
    aborted = 0.
    for attempt in attempts:
        stage, case, variant, _ = _cell(attempt)
        _row_protocol({**attempt, "budget_seconds": p.BUDGETS.get(stage),
                       "scenario": case.split("__", 1)[1] if isinstance(case, str) and "__" in case else "pure_direct"})
        if _cell(attempt) in cells:
            raise ValueError("Completed or aborted experiment cell duplicated")
        cells.add(_cell(attempt))
        if attempt.get("source_sha256") != hashes[variant] or attempt.get("status") not in {"error_no_result", "timeout_no_result"}:
            raise ValueError("Aborted attempt source or status mismatch")
        aborted += checks._number(attempt.get("process_elapsed_seconds"), "aborted elapsed")
    total = completed + aborted
    return {"completed_seconds": completed, "aborted_seconds": aborted, "total_seconds": total,
            "total_limit_seconds": p.PROTOCOL["total_experiment_seconds"], "aborted_run_count": len(attempts),
            "budget_compliant": total <= p.PROTOCOL["total_experiment_seconds"] + 1e-9,
            "execution_notes_sha256": checks._sha256(path) if path.exists() else None}


def audit_results(output_dir):
    directory = Path(output_dir).resolve()
    target = directory / "validation.json"
    target.unlink(missing_ok=True)
    manifest, rows = _read(directory / "manifest.json"), _read(directory / "runs.json")
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ValueError("Expected manifest object and runs list")
    _manifest(manifest)
    hashes, execution = _sources(directory, manifest)
    referenced = set()
    for row in rows:
        run_id = "__".join(str(v) for v in _cell(row))
        if row.get("run_id") != run_id or row.get("solution_file") != f"run_solutions/{run_id}.json":
            raise ValueError("Saved solution path disagrees with run cell")
        referenced.add(checks._within(directory, row["solution_file"], "solution"))
    if any(path.resolve() not in referenced for path in (directory / "run_solutions").glob("*.json")):
        raise ValueError("Unregistered orphan solution file")
    verified = _audit_rows(directory, manifest, rows, hashes, _row_protocol, _configuration)
    reference = _reference_audit(directory, manifest, rows)
    budget, decisions = _budget(directory, rows, hashes), _decision_audit(directory, rows)
    successful = bool(rows) and not verified["invalid_rows"] and not verified["over_budget_rows"] and not budget["aborted_run_count"]
    result = {"schema_version": 1, "kind": "global_relay_followup_validation",
              "passed": bool(successful and budget["budget_compliant"] and decisions["protocol_complete"]),
              "evidence_verified": True, "validated_at_utc": datetime.now(timezone.utc).isoformat(), "run_count": len(rows),
              "method": "Frozen solver and runner, official complete structural rescore, independent DAG times, complete paired seeds and independently recomputed gates; no searches",
              **verified, "all_runs_successful": successful, "protocol_complete": decisions["protocol_complete"],
              "manifest_sha256": checks._sha256(directory / "manifest.json"), "runs_sha256": checks._sha256(directory / "runs.json"),
              "decisions_sha256": decisions["decisions_sha256"], "source_snapshots_sha256": hashes,
              "execution_sources_sha256": execution, "audit_script_sha256": checks._sha256(Path(__file__)),
              "environment_notes_sha256": (checks._sha256(directory / "environment_notes.json")
                                            if (directory / "environment_notes.json").exists() else None),
              "independent_dag_audit": reference, "cumulative_budget": budget, "decision_audit": decisions}
    _protocol().base.write_json(target, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_protocol().OUTPUT)
    result = audit_results(parser.parse_args(argv).output_dir)
    print(f"verified {result['run_count']} global/relay runs; passed={result['passed']}; eligible={result['decision_audit']['default_eligible']}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
