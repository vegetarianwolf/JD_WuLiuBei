"""Audit fresh follow-up routes and the shared, immutable parent budget."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
from pathlib import Path
import zipfile

from algorithm.experiments import audit_method_trials as original
from algorithm.experiments import run_method_iteration2 as protocol

checks, _read, _cell, _score = original.checks, original._read, original._cell, original._score


def _manifest(manifest):
    if (manifest.get("schema_version") != 6 or manifest.get("kind") != "method_trials_iteration2"
            or manifest.get("baseline_variant") != "baseline" or manifest.get("baseline_commit") != protocol.BASELINE_COMMIT
            or manifest.get("input_path") != protocol.base.INPUT or manifest.get("seeds") != protocol.SEEDS
            or manifest.get("protocol") != protocol.PROTOCOL
            or any(type(s) is not int for seeds in manifest["seeds"].values() for s in seeds)):
        raise ValueError("Follow-up changed its frozen protocol")


def _parent(directory, manifest):
    binding = manifest.get("parent_binding", {})
    if binding.get("parent_directory") != "..":
        raise ValueError("Missing fixed parent evidence binding")
    parent, documents = directory.parent, {}
    for name in ("manifest", "runs", "decisions", "validation"):
        path = parent / f"{name}.json"
        if checks._sha256(path) != checks._fingerprint(binding.get(f"parent_{name}_sha256"), f"parent {name}"):
            raise ValueError(f"Parent frozen {name} changed")
        documents[name] = _read(path)
    saved, validation = documents["manifest"], documents["validation"]
    original._manifest(saved)
    original._sources(parent, saved)
    if validation.get("passed") is not True or validation.get("protocol_complete") is not True or validation.get("decision_audit", {}).get("selected_variant") is not None:
        raise ValueError("Parent needs a complete successful evidence audit with no promoted candidate")
    for name in ("manifest", "runs", "decisions"):
        if validation.get(f"{name}_sha256") != checks._sha256(parent / f"{name}.json"):
            raise ValueError("Parent validation binding mismatch")
    for row in documents["runs"]:
        path = checks._within(parent, row.get("solution_file"), "parent solution")
        if checks._sha256(path) != checks._fingerprint(row.get("solution_sha256"), "parent solution"):
            raise ValueError("Parent saved solution changed")
    notes = parent / "execution_notes.json"
    if (checks._sha256(notes) if notes.exists() else None) != binding.get("parent_execution_notes_sha256"):
        raise ValueError("Parent aborted evidence changed")
    aborted = _read(notes).get("aborted_runs", []) if notes.exists() else []
    seconds = sum(checks._number(r["end_to_end_seconds"], "parent elapsed") for r in documents["runs"])
    seconds += sum(checks._number(r["process_elapsed_seconds"], "parent aborted elapsed") for r in aborted)
    checks._same(seconds, binding.get("parent_experiment_seconds"), "parent bound elapsed")
    checks._same(seconds, validation["cumulative_budget"]["total_seconds"], "parent audited elapsed")
    if saved.get("input_sha256") != manifest.get("input_sha256"):
        raise ValueError("Follow-up changed the parent's official input")
    return {"parent_seconds": seconds, "parent_run_count": len(documents["runs"]),
            "parent_validation_sha256": checks._sha256(parent / "validation.json"), "parent_evidence_verified": True}


def _sources(directory, manifest):
    snapshots = manifest.get("source_snapshots", {})
    if set(snapshots) != set(protocol.VARIANTS):
        raise ValueError("Incomplete follow-up solver snapshots")
    commit = protocol.BASELINE_COMMIT
    baseline = {name.removeprefix("algorithm/src/"): checks._git("show", f"{commit}:{name}")
                for name in checks._git("ls-tree", "-r", "--name-only", commit, "--", "algorithm/src/uav_dispatch").decode().splitlines() if name.endswith(".py")}
    hashes, execution_hashes = {}, {}
    for variant, flags in protocol.VARIANTS.items():
        source = snapshots[variant]
        if (source.get("config") != flags or any(type(source["config"][k]) is not type(v) for k, v in flags.items())
                or source.get("source_ref") != (commit if variant == "baseline" else "working_tree")):
            raise ValueError("Follow-up variant configuration changed")
        path = checks._within(directory, source.get("path"), variant)
        fingerprint = checks._fingerprint(source.get("sha256"), variant)
        if checks._sha256(path) != fingerprint:
            raise ValueError("Follow-up snapshot fingerprint mismatch")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(set(names)) != len(names) or any(not n.startswith("uav_dispatch/") or not n.endswith(".py") or len(Path(n).parts) != 2 or ".." in Path(n).parts for n in names):
                raise ValueError("Unsafe follow-up archive")
            files = {n: archive.read(n) for n in names}
        if set(files) != set(baseline) or (variant == "baseline" and files != baseline):
            raise ValueError("Follow-up baseline Git bytes or module set disagree")
        hashes[variant] = fingerprint
    execution = manifest.get("execution_sources", {})
    if set(execution) != set(protocol.EXECUTION_NAMES):
        raise ValueError("Incomplete follow-up execution sources")
    for name, source in execution.items():
        path = checks._within(directory, source.get("path"), name)
        fingerprint = checks._fingerprint(source.get("sha256"), name)
        if checks._sha256(path) != fingerprint or checks._sha256(checks.ROOT / f"algorithm/experiments/{name}.py") != fingerprint:
            raise ValueError("Follow-up archived or live execution source changed")
        execution_hashes[name] = fingerprint
    if manifest.get("runner_sha256") != execution_hashes["run_optimization_experiments"]:
        raise ValueError("Follow-up worker binding mismatch")
    return hashes, execution_hashes


def _row_protocol(row):
    stage, case, variant, seed = _cell(row)
    if (stage not in protocol.SEEDS or case not in protocol.CASES[stage] or variant not in protocol.VARIANTS
            or type(seed) is not int or seed not in protocol.SEEDS[stage]):
        raise ValueError("Follow-up run cell violates frozen protocol")
    if row.get("budget_seconds") != protocol.BUDGETS[stage][case] or row.get("scenario") != "pure_direct":
        raise ValueError("Follow-up original scenario or budget changed")
    names = list(protocol.VARIANTS)
    if protocol.SEEDS[stage].index(seed) % 2:
        names.reverse()
    if row.get("order") != names.index(variant):
        raise ValueError("Follow-up paired order changed")


def _configuration(row, payload):
    expected = {"max_iterations": 10_000_000, "time_limit_seconds": max(1e-6, row["budget_seconds"] - row["construction_seconds"] - 1.),
                "seed": row["seed"], "candidate_limit": 48, "relay_enabled": False, "relay_direct_warmup_fraction": .8,
                "enable_home_seed": False, "enable_home_bias": False, **protocol.VARIANTS[row["variant"]]}
    config = row.get("config")
    if not isinstance(config, dict) or set(config) != set(expected) or any(config[k] != v or type(config[k]) is not type(v) for k, v in expected.items() if k != "time_limit_seconds"):
        raise ValueError("Follow-up execution configuration changed")
    checks._same(expected["time_limit_seconds"], config["time_limit_seconds"], "follow-up configured time")
    metadata = payload.get("metadata", {})
    flags = {k: expected[k] for k in ("seed", "relay_enabled", "enable_home_seed", *protocol.VARIANTS[row["variant"]])}
    if row["variant"] == "strings":
        flags.update(enable_direct_option_cache=False, enable_budget_cooling=False)
    for key, value in flags.items():
        if metadata.get(key) != value or type(metadata.get(key)) is not type(value):
            raise ValueError(f"Follow-up metadata disagrees: {key}")
    checks._same(expected["time_limit_seconds"], metadata.get("time_limit_seconds"), "follow-up metadata time")
    if row["iterations"] > expected["max_iterations"]:
        raise ValueError("Follow-up iteration cap exceeded")


def _pairs(rows, stage, case="fleet200"):
    groups = {v: {r["seed"]: r for r in rows if r["stage"] == stage and r["case_id"] == case and r["variant"] == v and r["valid"] and r["budget_compliant"]} for v in protocol.VARIANTS}
    return [(groups["baseline"][s], groups["strings"][s]) for s in sorted(groups["baseline"].keys() & groups["strings"].keys())]


def _complete(rows, stage):
    return all({b["seed"] for b, _ in _pairs(rows, stage, case)} == set(protocol.SEEDS[stage]) for case in protocol.CASES[stage])


def _decision_audit(directory, rows):
    screen = bool(_complete(rows, "screen") and sum(_score(c) < _score(b) for b, c in _pairs(rows, "screen")) >= 1
                  and all(_score(c) <= _score(b) for case in protocol.CASES["screen"] for b, c in _pairs(rows, "screen", case)))
    medium = bool(_complete(rows, "medium") and sum(_score(c) < _score(b) for b, c in _pairs(rows, "medium")) >= 2)
    selected = "strings" if screen and medium else None
    path = directory / "decisions.json"
    decisions = _read(path)
    if not isinstance(decisions, list) or any(not isinstance(d, dict) for d in decisions):
        raise ValueError("Invalid follow-up decisions")
    stages = {d.get("stage"): d for d in decisions}
    if len(stages) != len(decisions) or not set(stages).issubset(protocol.SEEDS):
        raise ValueError("Duplicate or unknown follow-up decision")
    expected = {"screen": {"candidates": {"strings": screen}, "eligible_variants": ["strings"] if screen else []},
                "medium": {"selected_variant": selected, "passed": selected is not None, "skipped": not screen,
                           "candidates": {"strings": medium} if screen else {}, "continuation_after_regression": True}}
    for stage in protocol.SEEDS:
        actual = {_cell(r) for r in rows if r["stage"] == stage}
        wanted = {(stage, case, variant, seed) for case in protocol.CASES[stage] for variant in protocol.VARIANTS for seed in protocol.SEEDS[stage]} if stage == "screen" or screen else set()
        if not actual.issubset(wanted):
            raise ValueError("Unqualified follow-up candidate entered validation")
        if stage in stages:
            if actual != wanted or any(stages[stage].get(k) != v or type(stages[stage].get(k)) is not type(v) for k, v in expected[stage].items()):
                raise ValueError(f"Follow-up decision disagrees with independent gate: {stage}")
    if "medium" in stages and "screen" not in stages:
        raise ValueError("Follow-up decision missing prerequisite")
    return {"screen": {"strings": screen}, "eligible_variants": ["strings"] if screen else [], "medium": {"strings": medium},
            "selected_variant": selected, "default_eligible": selected is not None, "exploratory_promotion": True,
            "protocol_complete": set(stages) == set(protocol.SEEDS), "decisions_sha256": checks._sha256(path)}


def _budget(directory, rows, parent_seconds):
    stages = {s: {"completed_seconds": 0., "aborted_seconds": 0., "cap_seconds": cap} for s, cap in protocol.CAPS.items()}
    cells = {_cell(r) for r in rows}
    for row in rows:
        stages[row["stage"]]["completed_seconds"] += checks._number(row["end_to_end_seconds"], "follow-up elapsed")
    path = directory / "execution_notes.json"
    attempts = _read(path).get("aborted_runs", []) if path.exists() else []
    if not isinstance(attempts, list):
        raise ValueError("Invalid follow-up aborted list")
    for attempt in attempts:
        stage, case, variant, seed = _cell(attempt)
        order = list(protocol.VARIANTS) if seed in protocol.SEEDS.get(stage, [])[::2] else list(reversed(protocol.VARIANTS))
        _row_protocol({**attempt, "budget_seconds": protocol.BUDGETS.get(stage, {}).get(case), "scenario": "pure_direct", "order": order.index(variant) if variant in order else None})
        if _cell(attempt) in cells:
            raise ValueError("Duplicated completed or aborted follow-up cell")
        cells.add(_cell(attempt))
        stages[stage]["aborted_seconds"] += checks._number(attempt.get("process_elapsed_seconds"), "follow-up aborted elapsed")
    for bucket in stages.values():
        bucket["total_seconds"] = bucket["completed_seconds"] + bucket["aborted_seconds"]
        bucket["budget_compliant"] = bucket["total_seconds"] <= bucket["cap_seconds"] + 1e-9
    batch = sum(b["total_seconds"] for b in stages.values())
    total = batch + checks._number(parent_seconds, "parent shared elapsed")
    reserve = total <= protocol.TOTAL_SECONDS - protocol.RESERVED_SECONDS + 1e-9
    return {"stages": stages, "batch_seconds": batch, "parent_seconds": parent_seconds, "total_seconds": total,
            "total_limit_seconds": protocol.TOTAL_SECONDS, "reserved_seconds": protocol.RESERVED_SECONDS,
            "reserve_preserved": reserve, "aborted_run_count": len(attempts), "budget_compliant": reserve and all(b["budget_compliant"] for b in stages.values())}


def audit_results(output_dir):
    directory = Path(output_dir).resolve()
    target = directory / "validation.json"
    target.unlink(missing_ok=True)
    manifest, rows = _read(directory / "manifest.json"), _read(directory / "runs.json")
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ValueError("Expected follow-up manifest and rows")
    _manifest(manifest)
    parent = _parent(directory, manifest)
    hashes, execution = _sources(directory, manifest)
    paths = set()
    for row in rows:
        run_id = "__".join(str(v) for v in (row["stage"], row["case_id"], row["variant"], row["seed"]))
        if row.get("run_id") != run_id or row.get("solution_file") != f"run_solutions/{run_id}.json":
            raise ValueError("Follow-up solution identity changed")
        paths.add(checks._within(directory, row["solution_file"], "follow-up solution"))
    if any(p.resolve() not in paths for p in (directory / "run_solutions").glob("*.json")):
        raise ValueError("Unregistered follow-up orphan solution")
    verified = original._audit_rows(directory, manifest, rows, hashes, _row_protocol, _configuration)
    budget, decisions = _budget(directory, rows, parent["parent_seconds"]), _decision_audit(directory, rows)
    successful = bool(rows) and not verified["invalid_rows"] and not verified["over_budget_rows"] and not budget["aborted_run_count"]
    result = {"schema_version": 6, "passed": successful and budget["budget_compliant"] and decisions["protocol_complete"],
              "evidence_verified": True, "validated_at_utc": datetime.now(timezone.utc).isoformat(), "run_count": len(rows),
              "method": "Independent official-CSV _force_full rescoring and gates; unchanged audited parent fingerprints; no solver run",
              **verified, **parent, "all_runs_successful": successful, "protocol_complete": decisions["protocol_complete"],
              "manifest_sha256": checks._sha256(directory / "manifest.json"), "runs_sha256": checks._sha256(directory / "runs.json"),
              "decisions_sha256": decisions["decisions_sha256"], "source_snapshots_sha256": hashes, "execution_sources_sha256": execution,
              "audit_script_sha256": checks._sha256(Path(__file__)), "cumulative_budget": budget, "decision_audit": decisions}
    protocol.base.write_json(target, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=protocol.OUTPUT)
    result = audit_results(parser.parse_args(argv).output_dir)
    print(f"verified {result['run_count']} follow-up runs; passed={result['passed']}; selected={result['decision_audit']['selected_variant']}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
