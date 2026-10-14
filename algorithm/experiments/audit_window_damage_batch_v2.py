"""Supplementary saved-evidence replay; original030 telemetry remains failed.

Only the identified DIRECT empty-leg-set auxiliary-distance bug is eligible
for an in-memory single-field correction. No search/repair code is imported.
The original batch/auditors/data are never edited and audit.json is not written.
"""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
from math import isclose, isfinite
from pathlib import Path

try:
    from . import audit_window_damage_batch as original_batch
    from .audit_window_damage_repair import audit_window_damage_repair_proposal as strict_post
    from .audit_window_damage_repair import _telemetry as strict_telemetry
    from .audit_weighted_direct_pair import require
except ImportError:
    import audit_window_damage_batch as original_batch
    from audit_window_damage_repair import audit_window_damage_repair_proposal as strict_post
    from audit_window_damage_repair import _telemetry as strict_telemetry
    from audit_weighted_direct_pair import require

FIELD = 'current_shared_loaded_distance_km'
REASON = ('Known frozen030 DIRECT auxiliary-distance defect: empty DIRECT '
          'TaskPlan.leg_ids made the producer count its entire owner route '
          'instead of flight with both pinned parcels aboard.')


def _same_number(actual, expected):
    # Exactly the frozen physical auditor's numeric precision; no wider repair.
    return type(actual) in (int, float) and isfinite(actual) and isclose(
        actual, expected, rel_tol=1e-11, abs_tol=1e-8)


def audit_post_with_known_direct_bug(tasks, topology, base_routes, macro_routes,
                                   post_routes, move, *, relay_enabled,
                                   telemetry=None, weights=None, scales=None,
                                   corrections=None):
    """Strict entity proof plus narrowly identified auxiliary-field exception.

    The same frozen telemetry function checks every other field unchanged.
    A separate recorded strict failure establishes that the original snapshot
    itself does not pass. Callers' snapshots are never mutated.
    """
    proof = strict_post(tasks, topology, base_routes, macro_routes, post_routes,
                        move, relay_enabled=relay_enabled)
    if telemetry is None:
        return proof
    saved = telemetry
    actual = proof[FIELD]
    value = saved.get(FIELD)
    owners = proof['pinned_final_owners']
    common_owner = len(set(owners.values())) == 1
    owner = next(iter(owners.values())) if common_owner else None
    route_distance = (float(proof['core_reference']['route_records'][owner]['distance_km'])
                      if common_owner else None)
    eligible = (relay_enabled is False and move.get('mode') == 'DIRECT'
                and not proof['handoffs'] and common_owner
                and not _same_number(value, actual)
                and _same_number(value, route_distance))
    if eligible:
        # Do not suppress an unrelated original error. This is the first
        # telemetry failure, not an assertion inferred from producer metadata.
        try:
            strict_telemetry(saved, post_routes, move, proof, tasks, topology,
                             weights, scales)
        except ValueError as error:
            failure = str(error)
            require('damage post '+FIELD+':' in failure,
                    'Original strict failure is not the known auxiliary field')
        else:
            raise ValueError('Known-bug candidate unexpectedly passed original strict telemetry')
        corrected = deepcopy(saved)
        corrected[FIELD] = actual
        # All other data are checked by the original frozen telemetry auditor.
        strict_telemetry(corrected, post_routes, move, proof, tasks, topology,
                         weights, scales)
        if corrections is not None:
            corrections.append(dict(
                field=FIELD, original_value=value, expected_value=actual,
                owner_route_distance_km=route_distance, owner_aircraft=owner,
                owner_route=list(post_routes[owner]),
                snapshot_sha256=original_batch.ledger.digest(saved),
                routes_sha256=original_batch.ledger.digest(post_routes),
                corrected_memory_snapshot_sha256=original_batch.ledger.digest(corrected),
                original_strict_failure=failure, reason=REASON,
                changed_fields=[FIELD], original_snapshot_mutated=False))
        return proof
    strict_telemetry(saved, post_routes, move, proof, tasks, topology, weights, scales)
    return proof


def audit(output, *, strict_failure_path=None):
    """Replay all old batch entities, writing only supplementary_audit_v2.json.

    The frozen original orchestrator performs the complete raw/family/12-map,
    post/closure/partial-beam/score/clock/deadline/guard checks. Only its post
    hook and final write are scoped here, with unconditional restoration.
    """
    output = Path(output).resolve()
    ledger = original_batch.ledger
    target = output/'audit.json'
    supplementary = output/'supplementary_audit_v2.json'
    require(not target.exists(), 'Original audit.json must remain absent after strict failure')
    require(not supplementary.exists(), 'Do not overwrite supplementary evidence')
    require(not getattr(original_batch, '_supplementary_v2_active', False),
            'Nested supplementary audit is unsupported')
    manifest = ledger.read(output/'manifest.json')
    before = {name:ledger.sha(output/name) for name in ('manifest.json','runs.json')}
    inputs = dict(before)
    inputs['execution_sources_record_sha256'] = ledger.digest(manifest['execution_sources'])
    inputs['frozen_original_batch_auditor_sha256'] = ledger.sha(Path(original_batch.__file__))
    inputs['frozen_original_post_auditor_sha256'] = ledger.sha(
        Path(strict_post.__globals__['__file__']))
    for name, actual in (
        ('audit_window_damage_batch.py',inputs['frozen_original_batch_auditor_sha256']),
        ('audit_window_damage_repair.py',inputs['frozen_original_post_auditor_sha256'])):
        require(actual == manifest['execution_sources'][name]['sha256'],
                'Original auditor differs from frozen030 source: '+name)
    if strict_failure_path is not None:
        failure_path = Path(strict_failure_path).resolve()
        require(failure_path.is_file(), 'Missing original strict-failure evidence')
        inputs['original_strict_failure_file'] = str(failure_path)
        inputs['original_strict_failure_sha256'] = ledger.sha(failure_path)
    corrections, captured = [], []
    old_post = original_batch.audit_window_damage_repair_proposal
    old_write = ledger.write
    stdout = StringIO()

    def post(*args, **kwargs):
        return audit_post_with_known_direct_bug(*args, **kwargs,
                                                corrections=corrections)

    def capture_write(path, value):
        require(Path(path).resolve() == target,
                'Frozen auditor attempted an unexpected write')
        require(not captured, 'Frozen auditor attempted multiple final writes')
        captured.append(deepcopy(value))

    original_batch._supplementary_v2_active = True
    try:
        original_batch.audit_window_damage_repair_proposal = post
        ledger.write = capture_write
        with redirect_stdout(stdout):
            replay = original_batch.audit(output)
        require(len(captured) == 1 and captured[0] == replay and replay['passed'] is True,
                'Frozen complete replay did not finish all checks')
        require(corrections, 'No reproduced known-bug strict telemetry failure')
        require(not target.exists(), 'Original audit.json was unexpectedly written')
        require(all(ledger.sha(output/name) == value for name,value in before.items()),
                'Original batch inputs changed during replay')
    finally:
        original_batch.audit_window_damage_repair_proposal = old_post
        ledger.write = old_write
        del original_batch._supplementary_v2_active
    unique = {}
    for correction in corrections:
        key = correction['snapshot_sha256']
        if key not in unique:
            unique[key] = dict(correction, replay_calls=0)
        unique[key]['replay_calls'] += 1
    result = dict(
        audit_type='supplementary_known_auxiliary_bug_replay_v2',
        strict_original_telemetry_passed=False,
        original_complete_audit_passed=False,
        entity_physics_and_other_telemetry_passed=True,
        original_failure_status='Original strict complete audit failed; this does not replace it with a pass.',
        correction_scope=dict(field=FIELD, only_DIRECT=True,
            no_handoffs=True, pins_must_have_same_actual_owner=True,
            stored_value_must_equal_independent_whole_owner_route_distance=True,
            all_RELAY_telemetry_strict=True, all_other_fields_strict=True,
            in_memory_only=True, original_audit_json_written=False,
            numerical_matching='frozen physical tolerance: rel1e-11, abs1e-8 km',
            reason=REASON),
        input_evidence=inputs,
        supplementary_execution_source_sha256=ledger.sha(Path(__file__)),
        correction_replay_calls=len(corrections), unique_corrected_snapshots=len(unique),
        corrections=list(unique.values()),
        strict_failure_reproduced_in_original_telemetry_auditor=True,
        corrected_memory_full_replay={k:v for k,v in replay.items() if k != 'passed'},
        corrected_memory_full_replay_sha256=ledger.digest(replay),
        replay_scope='All saved entities and unchanged telemetry, with only recorded single-field memory corrections; not an original strict telemetry pass.')
    old_write(supplementary,result)
    return result
