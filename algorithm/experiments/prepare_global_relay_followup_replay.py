"""Prepare a fresh replay from the measured follow-up's frozen sources, without solving.

Unlike the historical runner's ``prepare`` command, this never snapshots live
solver defaults. A replay is a new wall-clock experiment, not a reproduction of
the old timings or a continuation of its promotion evidence.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys

from algorithm.experiments import audit_global_relay_followup as audit
from algorithm.experiments import run_global_relay_followup as protocol


FROZEN_DIRECTORY = protocol.OUTPUT
FROZEN_MANIFEST_SHA256 = "213d8894a1305ae047e9df297204bccc3d184a935983c1944eaa1410a57c1305"


def prepare(output: Path, *, source: Path = FROZEN_DIRECTORY) -> Path:
    """Validate the original frozen batch, then create an exclusively new directory."""
    output, source = Path(output), Path(source).resolve()
    if output.exists() or output.is_symlink():
        raise ValueError("Replay output must not already exist; historical results are never overwritten")
    output = output.resolve()
    if output.is_relative_to(source):
        raise ValueError("Replay output must be outside the frozen source directory")

    manifest_bytes = (source / "manifest.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != FROZEN_MANIFEST_SHA256:
        raise ValueError("Frozen follow-up manifest fingerprint mismatch")
    manifest = json.loads(manifest_bytes)
    audit._manifest(manifest)
    source_hashes, execution_hashes = audit._sources(source, manifest)
    official_input = audit.checks._within(protocol.ROOT, manifest["input_path"], "official input")
    if protocol.base.sha256(official_input) != manifest["input_sha256"]:
        raise ValueError("Official input fingerprint mismatch")

    # Read and hash every copied byte before creating the destination. The input
    # and live execution scripts are checked again by the frozen runner at run time.
    files = {}
    for entry in (*manifest["source_snapshots"].values(), *manifest["execution_sources"].values()):
        path = audit.checks._within(source, entry["path"], "frozen source")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("Frozen source changed while preparing replay")
        files[entry["path"]] = data

    environment = {"python": platform.python_version(), "platform": platform.platform()}
    original_environment = manifest["environment"]
    manifest["environment"] = environment
    prepared_at = datetime.now(timezone.utc).isoformat()

    # Exclusive creation prevents a second preparer from overwriting this batch.
    output.mkdir(parents=True, exist_ok=False)
    for relative, data in files.items():
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    protocol.base.write_json(output / "manifest.json", manifest)
    protocol.base.write_json(output / "runs.json", [])
    protocol.base.write_json(output / "decisions.json", [])
    protocol.base.write_json(output / "environment_notes.json", {
        "status": "prepared_not_run",
        "prepared_at_utc": prepared_at,
        "environment": environment,
        "python_executable": sys.executable,
        "measurement_policy": "The frozen runner executes solver runs sequentially. No measurements were run by this preparation command.",
        "pre_measurement_tests": "Not recorded for this replay. The original batch's test and host claims are not inherited.",
        "host_limitations": "Host activity is not instrumented or controlled. New wall-clock results may differ from the archived batch.",
    })
    protocol.base.write_json(output / "replay_provenance.json", {
        "kind": "global_relay_followup_replay",
        "schema_version": 1,
        "status": "prepared_not_run",
        "prepared_at_utc": prepared_at,
        "source_directory": str(source),
        "source_manifest_sha256": FROZEN_MANIFEST_SHA256,
        "source_environment": original_environment,
        "manifest_sha256": protocol.base.sha256(output / "manifest.json"),
        "input_sha256": manifest["input_sha256"],
        "source_snapshots_sha256": source_hashes,
        "execution_sources_sha256": execution_hashes,
        "preparer_sha256": protocol.base.sha256(Path(__file__)),
        "environment": environment,
        "scope": "Only frozen sources and protocol were copied. Runs, decisions, validation, production and original measurement notes were not inherited.",
    })
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="New directory that does not already exist, outside the archived batch")
    parser.add_argument("--source-dir", type=Path, default=FROZEN_DIRECTORY,
                        help="Original frozen batch or an unchanged copy of its manifest and sources")
    args = parser.parse_args()
    output = prepare(args.output_dir, source=args.source_dir)
    print(f"Prepared frozen replay at {output}; no solver run has started.")


if __name__ == "__main__":
    main()
