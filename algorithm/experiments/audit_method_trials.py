"""Independent frozen-protocol audit and full official-CSV route rescoring.

No ALNS search is run. Numerical route, runtime and trajectory checks reuse the
established audit primitives; selection gates are recomputed independently.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import zipfile

from algorithm.experiments import audit_optimization_results as checks
from algorithm.experiments.audit_compact_trials import _audit_rows
from algorithm.experiments import run_method_trials as protocol


def _read(path):
    return protocol.base.read_json(path)


def _cell(row):
    return tuple(row.get(k) for k in ("stage", "case_id", "variant", "seed"))


def _score(row):
    return tuple(row[k] for k in ("late_count", "total_lateness_min", "distance_km"))


def _manifest(manifest):
    if (manifest.get("schema_version") != 5 or manifest.get("kind") != "method_trials"
            or manifest.get("baseline_variant") != "baseline"
            or manifest.get("baseline_commit") != protocol.BASELINE_COMMIT
            or manifest.get("input_path") != protocol.base.INPUT
            or manifest.get("seeds") != protocol.SEEDS or manifest.get("protocol") != protocol.PROTOCOL
            or any(type(seed) is not int for seeds in manifest["seeds"].values() for seed in seeds)):
        raise ValueError("Method trial changed its frozen protocol, baseline or seeds")


def _sources(directory, manifest):
    snapshots = manifest.get("source_snapshots")
    if not isinstance(snapshots, dict) or set(snapshots) != set(protocol.VARIANTS):
        raise ValueError("Incomplete solver snapshots")
    commit = protocol.BASELINE_COMMIT
    baseline = {name.removeprefix("algorithm/src/"): checks._git("show", f"{commit}:{name}")
                for name in checks._git("ls-tree", "-r", "--name-only", commit, "--",
                                       "algorithm/src/uav_dispatch").decode().splitlines() if name.endswith(".py")}
    hashes = {}
    for variant, flags in protocol.VARIANTS.items():
        source = snapshots[variant]
        if (source.get("config") != flags or any(type(source["config"][k]) is not type(v) for k, v in flags.items())
                or source.get("source_ref") != (commit if variant == "baseline" else "working_tree")):
            raise ValueError("Solver configuration or source reference changed")
        path = checks._within(directory, source.get("path"), variant)
        fingerprint = checks._fingerprint(source.get("sha256"), variant)
        if checks._sha256(path) != fingerprint:
            raise ValueError("Source snapshot fingerprint mismatch")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(set(names)) != len(names) or any(
                    not name.startswith("uav_dispatch/") or not name.endswith(".py")
                    or len(Path(name).parts) != 2 or ".." in Path(name).parts for name in names):
                raise ValueError("Unsafe or duplicate solver archive members")
            files = {name: archive.read(name) for name in names}
        if set(files) != set(baseline) or (variant == "baseline" and files != baseline):
            raise ValueError("Solver module set or baseline Git bytes disagree")
        hashes[variant] = fingerprint
    if snapshots["memo"]["path"] != snapshots["cooling"]["path"] or hashes["memo"] != hashes["cooling"]:
        raise ValueError("Independent feature arms must use one identical candidate snapshot")
    execution = manifest.get("execution_sources")
    if not isinstance(execution, dict) or set(execution) != set(protocol.EXECUTION_NAMES):
        raise ValueError("Incomplete frozen execution sources")
    execution_hashes = {}
    for name, source in execution.items():
        path = checks._within(directory, source.get("path"), name)
        fingerprint = checks._fingerprint(source.get("sha256"), name)
        live = checks.ROOT / f"algorithm/experiments/{name}.py"
        if checks._sha256(path) != fingerprint or checks._sha256(live) != fingerprint:
            raise ValueError("Archived or live execution source changed")
        execution_hashes[name] = fingerprint
    if manifest.get("runner_sha256") != execution_hashes["run_optimization_experiments"]:
        raise ValueError("Worker fingerprint mismatch")
    return hashes, execution_hashes


def _row_protocol(row):
    stage, case, variant, seed = _cell(row)
    if (stage not in protocol.SEEDS or case not in protocol.CASES[stage] or variant not in protocol.VARIANTS
            or type(seed) is not int or seed not in protocol.SEEDS[stage]
            or (stage == "equivalence" and variant == "cooling")):
        raise ValueError("Run cell violates frozen protocol")
    if row.get("budget_seconds") != protocol.BUDGETS[stage][case] or row.get("scenario") != "pure_direct":
        raise ValueError("Run budget or original scenario changed")


def _configuration(row, payload):
    expected = {"max_iterations": protocol.EQUIVALENCE_ITERATIONS if row["stage"] == "equivalence" else 10_000_000,
                "time_limit_seconds": max(1e-6, row["budget_seconds"] - row["construction_seconds"] - 1.),
                "seed": row["seed"], "candidate_limit": 48, "relay_enabled": False,
                "relay_direct_warmup_fraction": .8, "enable_home_seed": False, "enable_home_bias": False,
                **protocol.VARIANTS[row["variant"]]}
    config = row.get("config")
    if not isinstance(config, dict) or set(config) != set(expected) or any(
            config[k] != v or type(config[k]) is not type(v) for k, v in expected.items() if k != "time_limit_seconds"):
        raise ValueError("Run configuration differs from frozen variant")
    checks._same(expected["time_limit_seconds"], config["time_limit_seconds"], "configured time")
    metadata = payload.get("metadata", {})
    for key in ("seed", "relay_enabled", "enable_home_seed", *protocol.VARIANTS[row["variant"]]):
        if metadata.get(key) != expected[key] or type(metadata.get(key)) is not type(expected[key]):
            raise ValueError(f"Solution metadata disagrees with execution: {key}")
    checks._same(expected["time_limit_seconds"], metadata.get("time_limit_seconds"), "metadata time")
    if row["iterations"] > expected["max_iterations"]:
        raise ValueError("Iteration cap exceeded")


def _pairs(rows, stage, variant, case="fleet200"):
    baseline = {r["seed"]: r for r in rows if r["stage"] == stage and r["case_id"] == case
                and r["variant"] == "baseline" and r["valid"] and r["budget_compliant"]}
    candidate = {r["seed"]: r for r in rows if r["stage"] == stage and r["case_id"] == case
                 and r["variant"] == variant and r["valid"] and r["budget_compliant"]}
    return [(baseline[s], candidate[s]) for s in sorted(baseline.keys() & candidate.keys())]


def _complete(rows, stage, variant):
    return all({b["seed"] for b, _ in _pairs(rows, stage, variant, case)} == set(protocol.SEEDS[stage])
               for case in protocol.CASES[stage])


def _stable_search(directory, row):
    payload = _read(checks._within(directory, row["solution_file"], "equivalence solution"))
    metadata = payload["metadata"]
    fields = ("uses", "accepted", "current_improvements", "best_improvements", "total_reward",
              "current_late_count_improvements", "best_late_count_improvements",
              "equal_score_accepts", "unchanged_route_accepts")
    return {"routes": [r["encoded_visits"] for r in payload["routes"]], "score": payload["score"],
            "iterations": payload["iterations"], "trajectory": [[p[0], *p[2:]] for p in payload["metadata"]["best_trajectory"]],
            "statistics": {name: {k: stats[k] for k in fields} for name, stats in metadata["operator_statistics"].items()},
            "weights": metadata["operator_weights"], "accepted": metadata["accepted_solutions"]}


def _gates(directory, rows):
    equivalent = _complete(rows, "equivalence", "memo") and all(
        b["iterations"] == c["iterations"] == protocol.EQUIVALENCE_ITERATIONS
        and _stable_search(directory, b) == _stable_search(directory, c)
        for case in protocol.CASES["equivalence"] for b, c in _pairs(rows, "equivalence", "memo", case))
    screen, medium, ranks = {}, {}, {}
    for variant in ("memo", "cooling"):
        screened = _pairs(rows, "screen", variant)
        screen[variant] = bool((equivalent or variant == "cooling") and _complete(rows, "screen", variant)
            and sum(_score(c) < _score(b) for b, c in screened) >= 1
            and all(_score(c) <= _score(b) for case in protocol.CASES["screen"] for b, c in _pairs(rows, "screen", variant, case)))
        matched = _pairs(rows, "medium", variant)
        wins = sum(_score(c) < _score(b) for b, c in matched)
        medium[variant] = bool(_complete(rows, "medium", variant) and wins >= 2)
        ranks[variant] = (wins, sum(b["late_count"] - c["late_count"] for b, c in matched),
                         sum(b["late_count"] == c["late_count"] and _score(c) < _score(b) for b, c in matched), variant == "memo")
    eligible = [v for v in screen if screen[v]]
    qualified = [v for v in eligible if medium[v]]
    selected = max(qualified, key=ranks.__getitem__) if qualified else None
    return {"equivalence": bool(equivalent), "screen": screen, "eligible_variants": eligible,
            "medium": medium, "selected_variant": selected, "default_eligible": selected is not None,
            "exploratory_promotion": True}


def _decision_audit(directory, rows):
    path = directory / "decisions.json"
    decisions = _read(path)
    if not isinstance(decisions, list) or any(not isinstance(d, dict) for d in decisions):
        raise ValueError("Invalid decision list")
    stages = {d.get("stage"): d for d in decisions}
    if len(stages) != len(decisions) or not set(stages).issubset(protocol.SEEDS):
        raise ValueError("Duplicate or unknown stage decision")
    gates = _gates(directory, rows)
    eligible = gates["eligible_variants"]
    variants = {"equivalence": ["baseline", "memo"], "screen": list(protocol.VARIANTS),
                "medium": ["baseline", *eligible] if eligible else []}
    expected = {
        "equivalence": {"passed": gates["equivalence"], "candidate": "memo", "planned_pairs": 4},
        "screen": {"candidates": gates["screen"], "eligible_variants": eligible},
        "medium": {"selected_variant": gates["selected_variant"], "passed": gates["default_eligible"],
                   "skipped": not bool(eligible), "candidates": {v: gates["medium"][v] for v in eligible},
                   "continuation_after_regression": True},
    }
    for stage, names in variants.items():
        stage_rows = [r for r in rows if r["stage"] == stage]
        if any(r["variant"] not in names for r in stage_rows):
            raise ValueError("Unqualified candidate entered later stage")
        for row in stage_rows:
            index = protocol.SEEDS[stage].index(row["seed"])
            order = names if index % 2 == 0 else list(reversed(names))
            if row.get("order") != order.index(row["variant"]):
                raise ValueError("Run order violates paired AB/BA scheduling")
        if stage not in stages:
            continue
        if any(stages[stage].get(k) != v or type(stages[stage].get(k)) is not type(v) for k, v in expected[stage].items()):
            raise ValueError(f"Stage decision differs from independent gate: {stage}")
        wanted = {(stage, case, v, seed) for case in protocol.CASES[stage] for seed in protocol.SEEDS[stage] for v in names}
        if {_cell(r) for r in stage_rows} != wanted:
            raise ValueError(f"Completed stage has missing or unexpected cells: {stage}")
    order = list(protocol.SEEDS)
    for index, stage in enumerate(order):
        if stage in stages and any(prior not in stages for prior in order[:index]):
            raise ValueError("Stage decision missing prerequisite")
    return {**gates, "protocol_complete": set(stages) == set(order), "decisions_sha256": checks._sha256(path)}


def _budget(directory, rows):
    stages = {stage: {"completed_seconds": 0., "aborted_seconds": 0., "cap_seconds": cap}
              for stage, cap in protocol.CAPS.items()}
    cells = {_cell(r) for r in rows}
    for row in rows:
        stages[row["stage"]]["completed_seconds"] += checks._number(row["end_to_end_seconds"], "elapsed")
    path = directory / "execution_notes.json"
    attempts = _read(path).get("aborted_runs", []) if path.exists() else []
    if not isinstance(attempts, list):
        raise ValueError("Invalid aborted attempt list")
    for attempt in attempts:
        stage, case, variant, seed = _cell(attempt)
        _row_protocol({**attempt, "budget_seconds": protocol.BUDGETS.get(stage, {}).get(case), "scenario": "pure_direct"})
        if _cell(attempt) in cells:
            raise ValueError("Completed or aborted cell duplicated")
        cells.add(_cell(attempt))
        stages[stage]["aborted_seconds"] += checks._number(attempt.get("process_elapsed_seconds"), "aborted elapsed")
    for bucket in stages.values():
        bucket["total_seconds"] = bucket["completed_seconds"] + bucket["aborted_seconds"]
        bucket["budget_compliant"] = bucket["total_seconds"] <= bucket["cap_seconds"] + 1e-9
    total = sum(b["total_seconds"] for b in stages.values())
    reserve = total <= protocol.TOTAL_SECONDS - protocol.RESERVED_SECONDS + 1e-9
    return {"stages": stages, "total_seconds": total, "total_limit_seconds": protocol.TOTAL_SECONDS,
            "reserved_seconds": protocol.RESERVED_SECONDS, "reserve_preserved": reserve,
            "aborted_run_count": len(attempts), "execution_notes_sha256": checks._sha256(path) if path.exists() else None,
            "budget_compliant": reserve and all(b["budget_compliant"] for b in stages.values())}


def audit_results(output_dir):
    directory = Path(output_dir).resolve()
    target = directory / "validation.json"
    target.unlink(missing_ok=True)
    manifest, rows = _read(directory / "manifest.json"), _read(directory / "runs.json")
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ValueError("Expected manifest object and rows list")
    _manifest(manifest)
    hashes, execution = _sources(directory, manifest)
    referenced = set()
    for row in rows:
        run_id = "__".join(str(value) for value in (row["stage"], row["case_id"], row["variant"], row["seed"]))
        if row.get("run_id") != run_id or row.get("solution_file") != f"run_solutions/{run_id}.json":
            raise ValueError("Saved solution path disagrees with run cell")
        referenced.add(checks._within(directory, row["solution_file"], "solution"))
    if any(path.resolve() not in referenced for path in (directory / "run_solutions").glob("*.json")):
        raise ValueError("Unregistered orphan solution file")
    verified = _audit_rows(directory, manifest, rows, hashes, _row_protocol, _configuration)
    budget, decisions = _budget(directory, rows), _decision_audit(directory, rows)
    successful = bool(rows) and not verified["invalid_rows"] and not verified["over_budget_rows"] and not budget["aborted_run_count"]
    passed = successful and budget["budget_compliant"] and decisions["protocol_complete"]
    result = {"schema_version": 5, "passed": passed, "evidence_verified": True,
              "validated_at_utc": datetime.now(timezone.utc).isoformat(), "run_count": len(rows),
              "method": "Independent official-CSV _force_full route rescoring, frozen sources and independent gates; no solver runs",
              **verified, "all_runs_successful": successful, "protocol_complete": decisions["protocol_complete"],
              "manifest_sha256": checks._sha256(directory / "manifest.json"),
              "runs_sha256": checks._sha256(directory / "runs.json"), "decisions_sha256": decisions["decisions_sha256"],
              "source_snapshots_sha256": hashes, "execution_sources_sha256": execution,
              "audit_script_sha256": checks._sha256(Path(__file__)),
              "cumulative_budget": budget, "decision_audit": decisions}
    protocol.base.write_json(target, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=protocol.OUTPUT)
    args = parser.parse_args(argv)
    result = audit_results(args.output_dir)
    print(f"verified {result['run_count']} runs; passed={result['passed']}; selected={result['decision_audit']['selected_variant']}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
