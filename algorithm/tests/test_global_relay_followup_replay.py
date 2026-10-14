"""Replay preparation preserves frozen algorithms and never inherits measured evidence."""
import shutil

import pytest

from algorithm.experiments import prepare_global_relay_followup_replay as replay


@pytest.fixture
def archived(tmp_path):
    source = tmp_path / "archived"
    source.mkdir()
    shutil.copy2(replay.FROZEN_DIRECTORY / "manifest.json", source / "manifest.json")
    shutil.copytree(replay.FROZEN_DIRECTORY / "sources", source / "sources")
    for name in ("runs.json", "decisions.json", "validation.json", "production.json", "environment_notes.json"):
        (source / name).write_text('{"historical_only": true}\n')
    (source / "run_solutions").mkdir()
    (source / "run_solutions/old.json").write_text('{"historical_only": true}\n')
    return source


def test_prepare_reuses_exact_archives_and_is_accepted_by_frozen_runner(archived, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Replay preparation must not snapshot live solver code or launch a solver")

    p = replay.protocol
    monkeypatch.setattr(p, "snapshot", forbidden)
    monkeypatch.setattr(p, "run", forbidden)
    monkeypatch.setattr(p.base, "invoke", forbidden)
    monkeypatch.setattr(replay.platform, "python_version", lambda: "replay-python")
    monkeypatch.setattr(replay.platform, "platform", lambda: "replay-host")
    original = p.base.read_json(archived / "manifest.json")
    original_bytes = (archived / "manifest.json").read_bytes()
    output = replay.prepare(tmp_path / "replay", source=archived)
    manifest, rows = p.verify(output)
    assert rows == p.base.read_json(output / "decisions.json") == []
    assert manifest == {**original, "environment": {"python": "replay-python", "platform": "replay-host"}}
    assert (archived / "manifest.json").read_bytes() == original_bytes
    for entry in (*original["source_snapshots"].values(), *original["execution_sources"].values()):
        assert (output / entry["path"]).read_bytes() == (archived / entry["path"]).read_bytes()
    assert {path.name for path in output.iterdir()} == {
        "manifest.json", "sources", "runs.json", "decisions.json", "environment_notes.json", "replay_provenance.json"}
    provenance = p.base.read_json(output / "replay_provenance.json")
    assert provenance["source_manifest_sha256"] == replay.FROZEN_MANIFEST_SHA256
    assert provenance["manifest_sha256"] == p.base.sha256(output / "manifest.json")
    assert provenance["source_environment"] == original["environment"]
    assert provenance["source_snapshots_sha256"] == {
        variant: entry["sha256"] for variant, entry in original["source_snapshots"].items()}
    notes = p.base.read_json(output / "environment_notes.json")
    assert notes["environment"] == manifest["environment"]
    assert notes["status"] == provenance["status"] == "prepared_not_run"
    assert "Not recorded" in notes["pre_measurement_tests"]
    assert "historical_only" not in notes


@pytest.mark.parametrize("existing", ["file", "empty_directory", "nonempty_directory", "symlink", "dangling_symlink"])
def test_prepare_refuses_any_existing_output_without_changing_it(archived, tmp_path, existing):
    output = tmp_path / "replay"
    if existing == "file":
        output.write_text("keep")
    elif existing == "symlink":
        output.symlink_to(archived, target_is_directory=True)
    elif existing == "dangling_symlink":
        output.symlink_to(tmp_path / "absent", target_is_directory=True)
    else:
        output.mkdir()
        if existing == "nonempty_directory":
            (output / "keep").write_text("keep")
    with pytest.raises(ValueError, match="must not already exist"):
        replay.prepare(output, source=archived)
    if existing == "file":
        assert output.read_text() == "keep"
    elif existing in ("symlink", "dangling_symlink"):
        assert output.is_symlink()
    else:
        assert sorted(path.name for path in output.iterdir()) == (["keep"] if existing == "nonempty_directory" else [])


def test_prepare_refuses_to_write_inside_historical_source(archived):
    output = archived / "replay"
    with pytest.raises(ValueError, match="outside the frozen source"):
        replay.prepare(output, source=archived)
    assert not output.exists()


@pytest.mark.parametrize("tamper", ["manifest", "baseline_archive", "candidate_archive", "archived_execution", "live_execution", "input"])
def test_prepare_rejects_tampering_before_creating_output(archived, tmp_path, monkeypatch, tamper):
    p = replay.protocol
    if tamper == "manifest":
        manifest = p.base.read_json(archived / "manifest.json")
        manifest["source_snapshots"]["terminal"]["config"] = {}
        p.base.write_json(archived / "manifest.json", manifest)
    elif tamper in ("baseline_archive", "candidate_archive", "archived_execution"):
        name = {"baseline_archive": "baseline.zip", "candidate_archive": "candidate.zip",
                "archived_execution": "run_optimization_experiments.py"}[tamper]
        with (archived / "sources" / name).open("ab") as stream:
            stream.write(b"\nchanged")
    elif tamper == "live_execution":
        original_sha256 = replay.audit.checks._sha256
        changed_script = replay.audit.checks.ROOT / "algorithm/experiments/run_scenario_comparison.py"
        monkeypatch.setattr(replay.audit.checks, "_sha256", lambda path:
                            "0" * 64 if path == changed_script else original_sha256(path))
    else:
        fake_root = tmp_path / "input"
        input_path = fake_root / p.base.INPUT
        input_path.parent.mkdir(parents=True)
        input_path.write_text("changed")
        monkeypatch.setattr(p, "ROOT", fake_root)
    output = tmp_path / "replay"
    with pytest.raises(ValueError, match="fingerprint mismatch|execution source changed"):
        replay.prepare(output, source=archived)
    assert not output.exists()
