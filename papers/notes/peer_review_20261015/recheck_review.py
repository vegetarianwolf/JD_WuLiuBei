"""Hash-only version check and three saved-plan replays; no submitted code runs."""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from algorithm.experiments.audit_weighted_direct_pair import load_official_csv
from algorithm.experiments.audit_weighted_relay_ablation import audit_payload, reference_schedule
from papers.notes.teammate_submission_review_20261014.replay_review import topology

SUBMISSION = Path('/Users/vegetarianwolf/Downloads/南开大学_数智链达_命题1_精简提交版')
OLD = ROOT/'papers/notes/teammate_submission_review_20261014'
OUTPUT = Path(__file__).resolve().parent
FIELDS = ('late_count', 'total_lateness_min', 'distance_km')
WEIGHTS = (.5, .25, .25)
SCALES = (200., 4464.183540734935, 775.7731679286865)


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def key(score):
    return sum(w*score[f]/s for f, w, s in zip(FIELDS, WEIGHTS, SCALES))


def replay(tasks, path, *, allow_known_auxiliary_distance_error=False):
    payload = json.loads(path.read_text(encoding='utf-8-sig'))
    geometry = topology(payload)
    routes = [r['encoded_visits'] for r in payload['routes']]
    reference = reference_schedule(tasks, routes, geometry)
    original_error = None
    corrected_fields = []
    try:
        audit_payload(tasks, geometry, payload)
    except ValueError as error:
        if not allow_known_auxiliary_distance_error or 'independent per-drone relay distance' not in str(error):
            raise
        original_error = str(error)
        current = deepcopy(payload)
        for saved, actual in zip(current['relay']['per_drone'], reference['route_records'], strict=True):
            if abs(saved['distance_km']-actual['distance_km']) > 1e-8:
                corrected_fields.append(dict(drone_id=saved['drone_id'], saved=saved['distance_km'], actual=actual['distance_km']))
            saved['distance_km'] = actual['distance_km']
        audit_payload(tasks, geometry, current)
    score = {f: reference['score'][f] for f in FIELDS}
    return dict(path=str(path), source_sha256=digest(path), entity_physics_and_primary_canonical_passed=True,
        original_full_canonical_passed=original_error is None, original_canonical_error=original_error,
        in_memory_only_auxiliary_distance_corrections=corrected_fields, score=score, on_time_count=200-score['late_count'],
        weighted_J=key(score), final_tasks_per_aircraft=[r['task_count'] for r in reference['route_records']],
        relay_task_count=reference['relay_statistics']['relay_task_count'],
        cross_uav_count=reference['relay_statistics']['cross_uav_handoff_count'],
        task_coverage_count=len(reference['delivery_times_min']), topology=geometry,
        objective_order=payload['problem']['objective_order'], saved_runtime_seconds=payload.get('runtime_seconds'),
        saved_comparison=payload.get('comparison'))


def main():
    old_inventory = json.loads((OLD/'inventory.json').read_text())
    expected = {r['path']: r for r in old_inventory}
    actual = {}
    for file in sorted(SUBMISSION.rglob('*')):
        if file.is_file() and file.resolve().is_relative_to(SUBMISSION.resolve()):
            relative = str(file.relative_to(SUBMISSION))
            actual[relative] = dict(path=relative, size_bytes=file.stat().st_size, sha256=digest(file))
    added, removed = sorted(actual.keys()-expected.keys()), sorted(expected.keys()-actual.keys())
    changed = sorted(k for k in actual.keys() & expected.keys() if actual[k] != expected[k])
    assert not added and not removed and not changed
    old_replay = json.loads((OLD/'review_replay.json').read_text())
    assert all(r['passed'] and actual[r['file']]['sha256'] == r['sha256'] for r in old_replay['rows'])
    auditors = {name: dict(sha256=digest(ROOT/name), matches_previous=digest(ROOT/name) == value)
        for name, value in old_replay['auditor_sources'].items()}
    assert all(r['matches_previous'] for r in auditors.values())
    official = ROOT/'algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv'
    tasks = load_official_csv(official)
    assert tasks == load_official_csv(SUBMISSION/'data/official_tasks.csv')
    latest = SUBMISSION/'results/operator_retained_240/pruned_integrated_2026081704.json'
    current = ROOT/'algorithm/results/proactive_relay_diagnostic_20261014/batch025/phase_solutions/pair_pin_direct__seed_2026101511__final.json'
    formal = ROOT/'algorithm/results/weighted_relay_ablation_20261014/run_solutions/direct_stations__seed_2026101471.json'
    plans = dict(teammate_latest=replay(tasks, latest, allow_known_auxiliary_distance_error=True),
        current_checkpoint_025=replay(tasks, current), formal_direct_240_seed1471=replay(tasks, formal))
    assert len(tasks) == 200
    for plan in plans.values():
        assert plan['task_coverage_count'] == 200 and plan['final_tasks_per_aircraft'] == [25]*8
        assert plan['topology'] == plans['current_checkpoint_025']['topology']
    manifest = json.loads((latest.parent/'manifest.json').read_text())
    source_matches = {name: digest(SUBMISSION/'src/uav_dispatch'/Path(name).name) == value
        for name, value in manifest['source_hashes'].items()}
    assert len(source_matches) == 22 and all(source_matches.values())
    comparable = [r for r in old_replay['rows'] if r['same_official_resources'] and r['homes'] == 'same_predeployment']
    best_J = min(comparable, key=lambda r: (r['weighted_J'], r['file']))
    best_D = min(comparable, key=lambda r: (r['score']['distance_km'], r['file']))
    comparable_summary = dict(saved_plan_count=len(comparable), minimum_late_count=min(r['score']['late_count'] for r in comparable),
        minimum_weighted_J=best_J['weighted_J'], minimum_J_file=best_J['file'],
        minimum_distance_km=best_D['score']['distance_km'], minimum_distance_file=best_D['file'])
    assert len(comparable) == 206 and comparable_summary['minimum_late_count'] == 59
    result = dict(accessed_at=datetime.now(timezone.utc).isoformat(), no_submission_code_executed=True,
        no_optimizer_executed=True, previous_review_at=old_replay['accessed_at'],
        version=dict(previous_inventory_count=len(expected), current_file_count=len(actual), added=added, removed=removed,
            changed=changed, byte_identical=True, previous_inventory_sha256=digest(OLD/'inventory.json'),
            current_inventory_canonical_sha256=sha256(json.dumps(list(actual.values()), sort_keys=True, separators=(',', ':')).encode()).hexdigest()),
        reused_evidence=dict(saved_200_task_solutions=len(old_replay['rows']), entity_physics_passed=old_replay['independently_passed'],
            original_full_canonical_passed=old_replay['original_full_canonical_passed'],
            auxiliary_distance_issues=old_replay['verified_with_auxiliary_distance_issue'],
            proof_sha256=digest(OLD/'review_replay.json'), auditors=auditors, same_resource_predeployment_subset=comparable_summary),
        normalization=dict(task_count=200, normalized_fields_identical=True, official_raw_sha256=digest(official),
            submitted_raw_sha256=digest(SUBMISSION/'data/official_tasks.csv')),
        latest_source_manifest_sha256=digest(latest.parent/'manifest.json'), latest_source_manifest_matches=source_matches,
        weighted_objective=dict(weights=WEIGHTS, scales=SCALES, official_scalar=False), new_saved_plan_replays=plans,
        differences_teammate_minus=dict(current_025={**{f: plans['teammate_latest']['score'][f]-plans['current_checkpoint_025']['score'][f] for f in FIELDS},
            'weighted_J': plans['teammate_latest']['weighted_J']-plans['current_checkpoint_025']['weighted_J']},
            formal_240_1471={**{f: plans['teammate_latest']['score'][f]-plans['formal_direct_240_seed1471']['score'][f] for f in FIELDS},
            'weighted_J': plans['teammate_latest']['weighted_J']-plans['formal_direct_240_seed1471']['weighted_J']}))
    (OUTPUT/'review_recheck.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    print(json.dumps(dict(byte_identical=True, files=len(actual), reused_saved_plans=len(old_replay['rows']),
        new_saved_plan_replays=3, scores={name: p['score'] for name, p in plans.items()}), ensure_ascii=False))


if __name__ == '__main__':
    main()
