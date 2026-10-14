"""A failed screen's separate diagnostic never rewrites or promotes its parent."""
from copy import deepcopy

import pytest

from algorithm.experiments import audit_joint_relay_extension as audit


def diagnostic_rows():
    protocol = audit._protocol()
    return [dict(stage="medium", case_id=protocol.PRIMARY, variant=variant, seed=seed,
                 valid=True, budget_compliant=True, late_count=10 if variant == "baseline" else 9,
                 total_lateness_min=20., distance_km=50., order=order.index(variant))
            for index, seed in enumerate(protocol.SEEDS["medium"])
            for order in [list(protocol.VARIANTS) if index % 2 == 0 else list(reversed(protocol.VARIANTS))]
            for variant in order]


def test_private_helpers_do_not_replace_shared_module_protocol():
    original = audit.parent_audit._protocol
    parent_protocol = original()
    helpers = audit._private_helpers()
    assert helpers._protocol().SEEDS == audit._protocol().SEEDS
    assert helpers._protocol().SMALL == audit._protocol().parent.SMALL
    assert audit.parent_audit._protocol is original
    assert audit.parent_audit._protocol() is parent_protocol


def test_two_diagnostic_wins_do_not_qualify_for_default_promotion(tmp_path):
    protocol = audit._protocol()
    rows = diagnostic_rows()
    protocol.base.write_json(tmp_path / "decisions.json", [dict(stage="medium", selected_variant="joint",
        passed=False, diagnostic_only=True, reason=protocol.PROTOCOL["default_gate"])])
    result = audit._decisions(tmp_path, rows, audit._private_helpers())
    assert result["paired_wins"] == 2
    assert result["protocol_complete"]
    assert not result["default_eligible"]
    saved = protocol.base.read_json(tmp_path / "decisions.json")
    saved[0]["passed"] = True
    protocol.base.write_json(tmp_path / "decisions.json", saved)
    with pytest.raises(ValueError, match="cannot declare default"):
        audit._decisions(tmp_path, rows, audit._private_helpers())


def test_completed_diagnostic_requires_all_predeclared_pairs(tmp_path):
    protocol = audit._protocol()
    protocol.base.write_json(tmp_path / "decisions.json", [dict(stage="medium", selected_variant="joint",
        passed=False, diagnostic_only=True, reason=protocol.PROTOCOL["default_gate"])])
    with pytest.raises(ValueError, match="missing or unexpected cells"):
        audit._decisions(tmp_path, diagnostic_rows()[:-1], audit._private_helpers())


def test_child_budget_adds_parent_time_and_keeps_audit_reserve(tmp_path):
    protocol = audit._protocol()
    rows = diagnostic_rows()
    for row in rows:
        row["end_to_end_seconds"] = 29.
    parent = {"budget": {"total_seconds": 400., "budget_compliant": True}}
    result = audit._shared_budget(tmp_path, {"prior_seconds": 400.}, rows, parent, audit._private_helpers())
    assert result["total_seconds"] == 516.
    assert not result["reserve_preserved"]
    assert not result["budget_compliant"]
    with pytest.raises(ValueError, match="parent shared-budget seconds"):
        audit._shared_budget(tmp_path, {"prior_seconds": 0.}, rows, parent, audit._private_helpers())


def parent_fixture(tmp_path, monkeypatch):
    protocol = audit._protocol()
    parent = tmp_path / "batch1"
    directory = tmp_path / "extension30"
    parent.mkdir()
    directory.mkdir()
    manifest = dict(baseline_commit=protocol.BASELINE_COMMIT, input_path=protocol.base.INPUT, input_sha256="a" * 64)
    sources = {"baseline": "b" * 64, "joint": "c" * 64}
    bindings = {}
    for name, value in (("manifest", manifest), ("runs", [{}] * 12), ("decisions", [])):
        protocol.base.write_json(parent / f"{name}.json", value)
        bindings[name] = audit.checks._sha256(parent / f"{name}.json")
    from algorithm.experiments import audit_relay_trials as reference
    receipt = dict(passed=True, evidence_verified=True, run_count=12, source_snapshots_sha256=sources,
        audit_script_sha256=audit.checks._sha256(audit.parent_audit.Path(audit.parent_audit.__file__)),
        audit_dependencies={"audit_optimization_results": audit.checks._sha256(audit.parent_audit.Path(audit.checks.__file__)),
                            "audit_relay_trials_reference": audit.checks._sha256(audit.parent_audit.Path(reference.__file__))},
        cumulative_budget=dict(total_seconds=90., execution_notes_sha256=None),
        **{f"{name}_sha256": value for name, value in bindings.items()})
    protocol.base.write_json(parent / "validation.json", receipt)
    bindings["validation"] = audit.checks._sha256(parent / "validation.json")
    child_manifest = {**manifest, "parent_bindings": bindings, "prior_seconds": 90.}
    monkeypatch.setattr(audit.parent_audit, "_manifest", lambda *_: None)
    monkeypatch.setattr(audit.parent_audit, "_sources", lambda *_: (sources, {}))
    monkeypatch.setattr(audit.parent_audit, "_audit_rows", lambda *_: {"invalid_rows": [], "over_budget_rows": []})
    monkeypatch.setattr(audit.parent_audit, "_budget", lambda *_: {"budget_compliant": True, "aborted_run_count": 0,
                      "total_seconds": 90., "execution_notes_sha256": None})
    monkeypatch.setattr(audit.parent_audit, "_decision_audit", lambda *_: {"protocol_complete": True, "screen": False,
                      "default_eligible": False})
    return directory, child_manifest


def test_parent_revalidation_is_read_only_and_recomputes_prior_seconds(tmp_path, monkeypatch):
    directory, manifest = parent_fixture(tmp_path, monkeypatch)
    before = {path: path.read_bytes() for path in directory.parent.joinpath("batch1").glob("*.json")}
    assert audit._parent(directory, manifest)["validated_rows"] == 12
    assert all(path.read_bytes() == data for path, data in before.items())
    changed = deepcopy(manifest)
    changed["prior_seconds"] = 0.
    with pytest.raises(ValueError, match="parent execution seconds"):
        audit._parent(directory, changed)


@pytest.mark.parametrize("name", ["manifest", "runs", "decisions", "validation"])
def test_parent_document_binding_detects_any_post_prepare_change(tmp_path, monkeypatch, name):
    directory, manifest = parent_fixture(tmp_path, monkeypatch)
    (directory.parent / "batch1" / f"{name}.json").write_text("{}\n ")
    with pytest.raises(ValueError, match=f"Parent {name} fingerprint"):
        audit._parent(directory, manifest)
