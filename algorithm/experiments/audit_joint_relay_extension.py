"""Audit a separate two-seed diagnostic without modifying parent evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from algorithm.experiments import audit_joint_relay_trials as parent_audit

checks = parent_audit.checks


def _protocol():
    from algorithm.experiments import run_joint_relay_extension
    return run_joint_relay_extension


def _read(path):
    return _protocol().base.read_json(path)


def _private_helpers():
    """Load separate function globals; the shared module remains untouched."""
    path = Path(parent_audit.__file__)
    spec = importlib.util.spec_from_file_location("algorithm.experiments._private_joint_extension_helpers", path)
    if spec is None or spec.loader is None:
        raise ValueError("Could not load the frozen joint audit primitives")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    protocol = _protocol()
    adapter = SimpleNamespace(**{name: value for name, value in vars(protocol).items() if not name.startswith("__")})
    adapter.SMALL = protocol.parent.SMALL
    module._protocol = lambda: adapter
    return module


def _manifest(manifest):
    protocol = _protocol()
    if (type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1
            or manifest.get("kind") != "joint_relay_extension" or manifest.get("baseline_variant") != "baseline"
            or manifest.get("baseline_commit") != protocol.BASELINE_COMMIT or manifest.get("input_path") != protocol.base.INPUT
            or manifest.get("seeds") != protocol.SEEDS or manifest.get("protocol") != protocol.PROTOCOL
            or manifest.get("parent_directory") != "../batch1"
            or any(type(seed) is not int for seeds in manifest["seeds"].values() for seed in seeds)):
        raise ValueError("Joint extension changed its frozen protocol or parent location")


def _parent(directory, manifest):
    parent = (directory / "../batch1").resolve()
    bindings = manifest.get("parent_bindings")
    if not isinstance(bindings, dict) or set(bindings) != {"manifest", "runs", "decisions", "validation"}:
        raise ValueError("Incomplete joint extension parent bindings")
    documents = {}
    for name, value in bindings.items():
        path = checks._within(parent, f"{name}.json", f"parent {name}")
        expected = checks._fingerprint(value, f"parent {name}")
        if checks._sha256(path) != expected:
            raise ValueError(f"Parent {name} fingerprint mismatch")
        documents[name] = _read(path)
    saved = documents["validation"]
    if not isinstance(saved, dict) or saved.get("passed") is not True or saved.get("evidence_verified") is not True:
        raise ValueError("Parent must have a successful evidence audit")
    for name in ("manifest", "runs", "decisions"):
        if saved.get(f"{name}_sha256") != bindings[name]:
            raise ValueError("Parent audit does not bind the unchanged source documents")
    if saved.get("audit_script_sha256") != checks._sha256(Path(parent_audit.__file__)):
        raise ValueError("Parent audit implementation changed after its receipt")
    from algorithm.experiments import audit_relay_trials as reference
    expected_dependencies = {"audit_optimization_results": checks._sha256(Path(checks.__file__)),
                             "audit_relay_trials_reference": checks._sha256(Path(reference.__file__))}
    if saved.get("audit_dependencies") != expected_dependencies:
        raise ValueError("Parent audit dependencies changed after its receipt")
    # Recompute the original cohort with pure primitives. Calling the parent's
    # audit_results would overwrite its bound receipt and is intentionally avoided.
    parent_manifest, rows = documents["manifest"], documents["runs"]
    if not isinstance(parent_manifest, dict) or not isinstance(rows, list):
        raise ValueError("Invalid joint parent manifest or rows")
    parent_audit._manifest(parent_manifest)
    hashes, execution = parent_audit._sources(parent, parent_manifest)
    verified = parent_audit._audit_rows(parent, parent_manifest, rows, hashes)
    budget = parent_audit._budget(parent, rows)
    decisions = parent_audit._decision_audit(parent, rows)
    if (len(rows) != 12 or verified["invalid_rows"] or verified["over_budget_rows"]
            or not budget["budget_compliant"] or budget["aborted_run_count"]
            or not decisions["protocol_complete"] or decisions["screen"] or decisions["default_eligible"]):
        raise ValueError("Extension requires a complete valid parent with failed screening and no promotion")
    if saved.get("run_count") != len(rows) or saved.get("source_snapshots_sha256") != hashes:
        raise ValueError("Parent receipt disagrees with independently verified solver evidence")
    checks._same(budget["total_seconds"], manifest.get("prior_seconds"), "parent execution seconds")
    checks._same(budget["total_seconds"], saved.get("cumulative_budget", {}).get("total_seconds"), "parent audited seconds")
    if saved.get("cumulative_budget", {}).get("execution_notes_sha256") != budget["execution_notes_sha256"]:
        raise ValueError("Parent execution notes changed after its audit")
    for key in ("baseline_commit", "input_path", "input_sha256"):
        if manifest.get(key) != parent_manifest.get(key):
            raise ValueError(f"Joint extension changed parent {key}")
    return {"manifest": parent_manifest, "source_hashes": hashes, "execution_hashes": execution,
            "validated_rows": len(rows), "budget": budget, "bindings": bindings}


def _sources(directory, manifest, parent, helpers):
    hashes, execution = helpers._sources(directory, manifest)
    if hashes != parent["source_hashes"]:
        raise ValueError("Joint extension must use the exact same solver snapshots as its parent")
    for variant, source in manifest["source_snapshots"].items():
        if source != parent["manifest"]["source_snapshots"][variant]:
            raise ValueError("Joint extension changed inherited solver configuration or source binding")
    if any(execution.get(name) != value for name, value in parent["execution_hashes"].items()):
        raise ValueError("Joint extension changed the frozen common construction or worker")
    return hashes, execution


def _decisions(directory, rows, helpers):
    protocol = _protocol()
    path = directory / "decisions.json"
    decisions = _read(path)
    if not isinstance(decisions, list) or any(not isinstance(item, dict) for item in decisions):
        raise ValueError("Invalid joint extension decision list")
    if len(decisions) > 1 or any(item.get("stage") != "medium" for item in decisions):
        raise ValueError("Duplicate or unknown joint extension decision")
    planned = {("medium", case, variant, seed) for case in protocol.CASES["medium"]
               for variant in protocol.VARIANTS for seed in protocol.SEEDS["medium"]}
    cells = {helpers._cell(row) for row in rows}
    if not cells.issubset(planned):
        raise ValueError("Joint extension contains an undeclared diagnostic cell")
    for row in rows:
        order = list(protocol.VARIANTS) if protocol.SEEDS["medium"].index(row["seed"]) % 2 == 0 else list(reversed(protocol.VARIANTS))
        if type(row.get("order")) is not int or row["order"] != order.index(row["variant"]):
            raise ValueError("Joint extension run order violates paired AB/BA scheduling")
    expected = {"selected_variant": "joint", "passed": False, "diagnostic_only": True,
                "reason": protocol.PROTOCOL["default_gate"]}
    if decisions:
        if cells != planned:
            raise ValueError("Completed extension has missing or unexpected cells")
        if any(decisions[0].get(key) != value or type(decisions[0].get(key)) is not type(value) for key, value in expected.items()):
            raise ValueError("Two-seed diagnostic cannot declare default promotion")
    pairs = helpers._pairs(rows, "medium", protocol.PRIMARY)
    return {"diagnostic_only": True, "selected_variant": "joint", "default_eligible": False,
            "screen": None, "medium": False, "paired_runs": len(pairs),
            "paired_wins": sum(helpers._score(candidate) < helpers._score(baseline) for baseline, candidate in pairs),
            "paired_ties": sum(helpers._score(candidate) == helpers._score(baseline) for baseline, candidate in pairs),
            "paired_losses": sum(helpers._score(candidate) > helpers._score(baseline) for baseline, candidate in pairs),
            "protocol_complete": bool(decisions) and cells == planned,
            "decisions_sha256": checks._sha256(path)}


def _shared_budget(directory, manifest, rows, parent, helpers):
    child = helpers._budget(directory, rows)
    total = parent["budget"]["total_seconds"] + child["total_seconds"]
    checks._same(parent["budget"]["total_seconds"], manifest["prior_seconds"], "parent shared-budget seconds")
    shared = total <= _protocol().TOTAL_SECONDS - _protocol().RESERVED_SECONDS + 1e-9
    return {**child, "extension_seconds": child["total_seconds"], "parent_seconds": parent["budget"]["total_seconds"],
            "total_seconds": total, "reserve_preserved": shared,
            "scope": "Original failed-screen cohort plus separately authorized two-seed diagnostic",
            "budget_compliant": child["budget_compliant"] and parent["budget"]["budget_compliant"] and shared}


def audit_results(output_dir):
    directory = Path(output_dir).resolve()
    target = directory / "validation.json"
    target.unlink(missing_ok=True)
    manifest, rows = _read(directory / "manifest.json"), _read(directory / "runs.json")
    if not isinstance(manifest, dict) or not isinstance(rows, list):
        raise ValueError("Expected joint extension manifest object and rows list")
    _manifest(manifest)
    helpers = _private_helpers()
    parent = _parent(directory, manifest)
    hashes, execution = _sources(directory, manifest, parent, helpers)
    verified = helpers._audit_rows(directory, manifest, rows, hashes)
    budget, decisions = _shared_budget(directory, manifest, rows, parent, helpers), _decisions(directory, rows, helpers)
    successful = bool(rows) and not verified["invalid_rows"] and not verified["over_budget_rows"] and not budget["aborted_run_count"]
    from algorithm.experiments import audit_relay_trials as reference
    result = {"schema_version": 1, "kind": "joint_relay_extension_validation",
              "passed": bool(successful and budget["budget_compliant"] and decisions["protocol_complete"]),
              "evidence_verified": True, "all_runs_successful": successful,
              "validated_at_utc": datetime.now(timezone.utc).isoformat(), "run_count": len(rows),
              "method": "Independent structural and DAG rescoring through isolated audit primitives; frozen unchanged parent evidence and shared actual budget; no parent writes or searches; diagnostic only",
              **verified, "protocol_complete": decisions["protocol_complete"], "decision_audit": decisions,
              "cumulative_budget": budget, "parent_bindings": parent["bindings"], "parent_validated_rows": parent["validated_rows"],
              "manifest_sha256": checks._sha256(directory / "manifest.json"), "runs_sha256": checks._sha256(directory / "runs.json"),
              "decisions_sha256": decisions["decisions_sha256"], "source_snapshots_sha256": hashes,
              "execution_sources_sha256": execution, "audit_script_sha256": checks._sha256(Path(__file__)),
              "audit_dependencies": {"audit_joint_relay_trials": checks._sha256(Path(parent_audit.__file__)),
                  "audit_optimization_results": checks._sha256(Path(checks.__file__)),
                  "audit_relay_trials_reference": checks._sha256(Path(reference.__file__))}}
    _protocol().base.write_json(target, result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_protocol().OUTPUT)
    result = audit_results(parser.parse_args(argv).output_dir)
    print(f"verified {result['run_count']} diagnostic runs; passed={result['passed']}; default_eligible=False")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
