"""Read-only replay of a teammate's supplied saved solutions, not their code.

Reads third-party JSON/CSV as data. Imports only the repository's independent
standard-library auditors, never the submitted package or optimizer. Outputs
are restricted to this review directory and historical evidence is untouched.
"""
from collections import Counter,defaultdict
from copy import deepcopy
import csv
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
sys.path.insert(0,str(ROOT))
from algorithm.experiments.audit_weighted_direct_pair import load_official_csv
from algorithm.experiments.audit_weighted_relay_ablation import audit_payload,reference_schedule

SUBMISSION=Path('/Users/vegetarianwolf/Downloads/南开大学_数智链达_命题1_精简提交版')
OUTPUT=Path(__file__).resolve().parent
WEIGHTS=(.5,.25,.25)
SCALES=(200.,4464.183540734935,775.7731679286865)


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def weighted(score):
    return sum(w*score[field]/s for field,w,s in zip(
        ('late_count','total_lateness_min','distance_km'),WEIGHTS,SCALES))


def topology(payload):
    model=payload['problem']
    stations=payload.get('relay',{}).get('relay_coordinates',[])
    station_points={row[0]:list(row[1:]) for row in stations}
    homes=[]
    labels=[]
    for label in model['drone_homes']:
        if label=='origin':
            homes.append(model.get('depot_km',[0.,0.]))
            labels.append(None)
        elif isinstance(label,str) and label.startswith('station_'):
            station=int(label.split('_',1)[1])
            homes.append(station_points[station])
            labels.append(station)
        else:
            raise ValueError('Unrecognized saved per-aircraft home: '+repr(label))
    registry={}
    for route in payload['routes']:
        for event in route['visits']:
            identifier=event.get('leg_id')
            if identifier is None:
                continue
            if event['action'] in ('pickup','relay_drop'):
                kind='RELAY_IN'
            elif event['action'] in ('relay_pickup','delivery'):
                kind='RELAY_OUT'
            else:
                raise ValueError('Unrecognized transport-leg action')
            leg=dict(id=identifier,task_id=event['task_id'],kind=kind,relay_id=event['relay_id'])
            if identifier in registry and registry[identifier]!=leg:
                raise ValueError('Saved leg metadata is inconsistent')
            registry[identifier]=leg
    return dict(stations=stations,legs=[registry[k] for k in sorted(registry)],
        home_points=homes,drone_homes=labels,drone_count=model['drone_count'],
        max_tasks_per_drone=model['max_tasks_per_drone'],capacity=model['capacity'],
        speed_km_per_min=model['speed_km_per_min'])


def main():
    OUTPUT.mkdir(parents=True,exist_ok=True)
    official=load_official_csv(ROOT/'algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv')
    supplied=load_official_csv(SUBMISSION/'data/official_tasks.csv')
    if supplied!=official:
        raise AssertionError('Normalized supplied task id/coordinates/deadline data differs')
    current_manifest=json.loads((ROOT/'algorithm/results/weighted_relay_ablation_20261014/manifest.json').read_text())
    current_topologies={name:json.loads((ROOT/'algorithm/results/weighted_relay_ablation_20261014'/record['topology_path']).read_text())
                        for name,record in current_manifest['scenarios'].items()}
    expected_homes=current_topologies['direct_stations']['home_points']
    inventory=[]
    source_by_hash=defaultdict(list)
    for file in sorted(SUBMISSION.rglob('*')):
        if not file.is_file() or not file.resolve().is_relative_to(SUBMISSION.resolve()):
            continue
        row=dict(path=str(file.relative_to(SUBMISSION)),size_bytes=file.stat().st_size,sha256=digest(file))
        inventory.append(row)
        if file.suffix=='.py':
            source_by_hash[row['sha256']].append(row['path'])
    source_checks=[]
    for manifest_file in sorted((SUBMISSION/'results').rglob('manifest.json')):
        manifest=json.loads(manifest_file.read_text(encoding='utf-8-sig'))
        checks=[]
        for name,expected in manifest.get('source_hashes',{}).items():
            checks.append(dict(name=name,expected_sha256=expected,matching_source_paths=source_by_hash.get(expected,[])))
        source_checks.append(dict(manifest=str(manifest_file.relative_to(SUBMISSION)),
            manifest_sha256=digest(manifest_file),matched=sum(bool(c['matching_source_paths']) for c in checks),
            declared=len(checks),checks=checks,objective=manifest.get('objective')))
    rows,proofs,skipped=[],[],[]
    for file in sorted((SUBMISSION/'results').rglob('*.json')):
        payload=json.loads(file.read_text(encoding='utf-8-sig'))
        if not isinstance(payload,dict) or not isinstance(payload.get('routes'),list) or not isinstance(payload.get('score'),dict):
            continue
        model=payload.get('problem',{})
        if model.get('task_count')!=200:
            skipped.append(dict(file=str(file.relative_to(SUBMISSION)),reason='not_the_200_task_problem',task_count=model.get('task_count')))
            continue
        row=dict(file=str(file.relative_to(SUBMISSION)),sha256=digest(file),passed=False,
                 saved_score=payload['score'],objective=model.get('objective_order'),
                 saved_runtime_seconds=payload.get('runtime_seconds'),comparison=payload.get('comparison',{}))
        try:
            frozen=topology(payload)
            reference=reference_schedule(official,[r['encoded_visits'] for r in payload['routes']],frozen)
            canonical_error=None
            corrected_diagnostics=[]
            try:
                audit_payload(official,frozen,payload)
            except Exception as error:
                canonical_error=type(error).__name__+': '+str(error)
                # Preserve the original artifact. Check whether its entire
                # canonical content agrees after repairing ONLY this known
                # optional per-drone distance telemetry in an in-memory copy.
                corrected=deepcopy(payload)
                for saved,truth in zip(corrected['relay']['per_drone'],reference['route_records'],strict=True):
                    if abs(saved['distance_km']-truth['distance_km'])>1e-8:
                        corrected_diagnostics.append(dict(drone_id=saved['drone_id'],
                            saved_distance_km=saved['distance_km'],actual_distance_km=truth['distance_km']))
                    saved['distance_km']=truth['distance_km']
                audit_payload(official,frozen,corrected)
            score=reference['score']
            physical=reference['relay_statistics']
            same_resources=(frozen['drone_count']==8 and frozen['capacity']==2 and
                frozen['max_tasks_per_drone']==25 and frozen['speed_km_per_min']==.9)
            homes='same_predeployment' if frozen['home_points']==expected_homes else (
                'origin' if frozen['home_points']==[[0.,0.]]*frozen['drone_count'] else 'other_homes')
            comparison=payload.get('comparison',{})
            declared_budget=comparison.get('total_budget_seconds',comparison.get('total_seconds'))
            common=file.parent/('common_stations_'+str(comparison.get('seed'))+'.json')
            if not common.exists():
                common=file.parent/('common_origin_'+str(comparison.get('seed'))+'.json')
            common_wall=None
            if common.exists():
                common_payload=json.loads(common.read_text(encoding='utf-8-sig'))
                common_wall=common_payload.get('wall_seconds',common_payload.get('runtime_seconds'))
            search_wall=comparison.get('search_wall_seconds',comparison.get('continuation_wall_seconds'))
            measured_total=(common_wall+search_wall if common_wall is not None and search_wall is not None else
                            comparison.get('amortized_total_wall_seconds'))
            row.update(passed=True,physical_and_primary_canonical_passed=True,
                original_full_canonical_passed=canonical_error is None,original_canonical_error=canonical_error,
                auxiliary_per_drone_distance_errors=corrected_diagnostics,
                score=score,weighted_J=weighted(score),on_time_count=200-score['late_count'],
                homes=homes,home_points=frozen['home_points'],stations=frozen['stations'],
                same_official_resources=same_resources,comparable_geometry=same_resources and homes in ('origin','same_predeployment'),
                task_coverage_count=len(reference['delivery_times_min']),
                final_tasks_per_route=[record['task_count'] for record in reference['route_records']],
                relay_task_count=physical['relay_task_count'],cross_uav_count=physical['cross_uav_handoff_count'],
                same_uav_relay_count=physical['same_uav_relay_count'],aircraft_waiting_min=physical['total_relay_waiting_time'],
                declared_total_budget_seconds=declared_budget,common_wall_seconds=common_wall,
                search_wall_seconds=search_wall,recorded_search_total_seconds=measured_total,
                timing_boundary='saved search/continuation clock plus saved common initializer; network/CSV/output excluded when so documented')
            proof=dict(file=row['file'],source_sha256=row['sha256'],topology=frozen,
                       encoded_routes=[r['encoded_visits'] for r in payload['routes']],score=score,
                       weighted_J=row['weighted_J'],source_event_registry='inferred only from used saved leg events, canonical events/plans/clocks independently bound')
            if '240' in file.parent.name:
                proofs.append(proof)
        except Exception as error:
            row['error']=type(error).__name__+': '+str(error)
        rows.append(row)
    result=dict(accessed_at=datetime.now(timezone.utc).isoformat(),submission_path=str(SUBMISSION),
        no_submission_code_executed=True,no_optimizer_executed=True,
        normalization=dict(task_count=len(official),identical=True,
            official_raw_sha256=digest(ROOT/'algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv'),
            supplied_raw_sha256=digest(SUBMISSION/'data/official_tasks.csv'),
            compared_fields='task id, both pickup coordinates, both delivery coordinates, numeric deadline'),
        weighted_objective=dict(weights=WEIGHTS,scales=SCALES,official_scalar=False),
        inventory_file_count=len(inventory),saved_200_task_solutions=len(rows),
        independently_passed=sum(r['passed'] for r in rows),failed=sum(not r['passed'] for r in rows),
        original_full_canonical_passed=sum(r.get('original_full_canonical_passed',False) for r in rows),
        verified_with_auxiliary_distance_issue=sum(r['passed'] and not r.get('original_full_canonical_passed',False) for r in rows),
        skipped_non200=skipped,rows=rows,source_checks=source_checks,
        auditor_sources={str(p.relative_to(ROOT)):digest(p) for p in (
            ROOT/'algorithm/experiments/audit_weighted_relay_ablation.py',
            ROOT/'algorithm/experiments/audit_weighted_direct_pair.py')})
    write(OUTPUT/'inventory.json',inventory)
    write(OUTPUT/'review_replay.json',result)
    write(OUTPUT/'minimal_long_run_replays.json',proofs)
    fields=('file','passed','homes','same_official_resources','late_count','on_time_count',
            'total_lateness_min','distance_km','weighted_J','relay_task_count','cross_uav_count',
            'declared_total_budget_seconds','recorded_search_total_seconds')
    with (OUTPUT/'verified_metrics.csv').open('w',encoding='utf-8-sig',newline='') as out:
        writer=csv.DictWriter(out,fieldnames=fields,lineterminator='\n')
        writer.writeheader()
        for row in rows:
            score=row.get('score',{})
            writer.writerow({field:score.get(field,row.get(field,'')) for field in fields})
    print(json.dumps({k:result[k] for k in ('inventory_file_count','saved_200_task_solutions','independently_passed','failed')},ensure_ascii=False))
    for row in rows:
        if '240' in Path(row['file']).parent.name:
            print(json.dumps({k:row.get(k) for k in ('file','passed','homes','score','weighted_J','cross_uav_count','recorded_search_total_seconds','error')},ensure_ascii=False))


if __name__=='__main__':
    main()
