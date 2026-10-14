"""Run the actual archived weighted DIRECT runtime on a tiny physical case."""
import json,os,subprocess,sys
from pathlib import Path
from zipfile import ZipFile
import pytest
from algorithm.experiments import run_proactive_relay_research as ledger
from algorithm.experiments.audit_weighted_relay_ablation import reference_schedule


@pytest.fixture(scope='module')
def archived_control(tmp_path_factory):
    folder=tmp_path_factory.mktemp('independent-direct-control');manifest=ledger.read(ledger.PARENT/'manifest.json')
    archive=ledger.PARENT/manifest['source_archive']['path'];assert ledger.sha(archive)==manifest['source_archive']['sha256']
    with ZipFile(archive) as z:z.extractall(folder/'bundle')
    script=folder/'tiny.py'
    script.write_text('''
import json
from contextlib import nullcontext
from time import perf_counter
from uav_dispatch import Point,Problem,Task
import multi_relay_direct_control_v1 as m
left,right,hub=Point(-4.,0.),Point(4.,0.),Point(0.,0.)
tasks=(Task(1,left,Point(0.,3.),9.),Task(2,right,Point(0.,4.),9.),
    *(Task(t,left,left,100.) for t in (3,4,5)),*(Task(t,right,right,100.) for t in (6,7,8)),Task(9,hub,hub,100.))
p=Problem(tasks,drone_count=3,capacity=2,max_tasks_per_drone=3,speed_km_per_min=.9,drone_homes=(left,right,hub))
base=((3,-3,4,-4,5,-5),(6,-6,7,-7,8,-8),(9,-9,1,-1,2,-2))
w,s=(.5,.25,.25),(9.,100.,100.)
answers=[]
for outer,expired in ((False,False),(True,False),(False,True)):
    context=m.weighted.objective(m.Score,s,w) if outer else nullcontext()
    with context:
        r=m.refine(p,base,deadline=perf_counter()+(-1. if expired else .3),weights=w,scales=s,seed=17,
            alns_fraction=.5,critical_limit=2,donor_limit=2,position_limit=4,option_count=2,beam_width=2,max_steps=1,snapshot_limit=3)
    assert not m.weighted.active and m.weighted.coefficients is None
    answers.append(dict(routes=r['routes'],metadata=r['metadata'],score=dict(late_count=r['evaluation'].score.late_count,
        total_lateness_min=r['evaluation'].score.total_lateness_min,distance_km=r['evaluation'].score.distance_km)))
print(json.dumps(dict(answers=answers,base=base,tasks={str(t.id):[[t.pickup.x,t.pickup.y],[t.delivery.x,t.delivery.y],t.deadline_min] for t in tasks}),allow_nan=False))
''')
    env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1','PYTHONPATH':os.pathsep.join((str(folder/'bundle'),str(ledger.EXPERIMENTS),str(ledger.ROOT)))}
    r=subprocess.run([sys.executable,str(script)],capture_output=True,text=True,env=env,cwd=ledger.ROOT,timeout=10)
    assert r.returncode==0,r.stderr
    return json.loads(r.stdout)


def test_independent_direct_uses_own_weighted_search_and_complete_guard(archived_control):
    x=archived_control;tasks={int(k):tuple(v) for k,v in x['tasks'].items()}
    graph=dict(home_points=[[-4.,0.],[4.,0.],[0.,0.]],stations=[],legs=[],drone_homes=[None]*3,
        drone_count=3,max_tasks_per_drone=3,capacity=2,speed_km_per_min=.9,relay_allowed=False)
    for outcome in x['answers']:
        ref=reference_schedule(tasks,outcome['routes'],graph);meta=outcome['metadata']
        assert ref['score']['late_count']==outcome['score']['late_count']
        assert abs(ref['score']['distance_km']-outcome['score']['distance_km'])<1e-8
        assert tuple(meta['final_key'])<=tuple(meta['initial_key'])
        assert not ref['handoffs'] and [v['task_count'] for v in ref['route_records']]==[3]*3
        assert meta['configuration']['no_relay_catalogue'] and meta['configuration']['pins_empty']
        for move in meta['accepted_moves']:
            assert move['full_verified_at_perf_counter']<=move['accepted_at_perf_counter']<meta['deadline_perf_counter']
    assert all(o['metadata']['alns_record'] is not None for o in x['answers'][:2])


def test_expiry_does_not_start_independent_direct_or_claim_a_repair(archived_control):
    out=archived_control['answers'][-1]
    assert out['routes']==archived_control['base']
    assert out['metadata']['deadline_reached'] and out['metadata']['alns_record'] is None
    assert out['metadata']['repair_record'] is None and not out['metadata']['accepted_moves']


def test_actual_alns_weighted_objective_and_paid_clock_are_inspectable(archived_control):
    for out in archived_control['answers'][:2]:
        meta=out['metadata']; record=meta['alns_record']
        started=meta['started_at_perf_counter']; deadline=meta['deadline_perf_counter']
        assert record['deadline_perf_counter']==started+(deadline-started)*meta['configuration']['alns_fraction']
        configured=record['configured_at_perf_counter']
        assert started<=configured<=record['started_at_perf_counter']<=record['returned_at_perf_counter']
        assert abs(record['config']['time_limit_seconds']-max(.001,record['deadline_perf_counter']-configured))<1e-9
        assert record['objective_mode']=='frozen_weighted_ALNS'
        assert record['effective_coefficients']==[.5/9.,.25/100.,.25/100.]
        assert record['periodic_configuration']==dict(interval=120,max_candidates=16,anchors_per_route=4,pair_budget_seconds=.008)


def test_archived_control_passes_independent_saved_entity_audit(archived_control):
    from algorithm.experiments.audit_multi_relay_direct_control_v1 import audit_metadata
    x=archived_control;tasks={int(k):tuple(v) for k,v in x['tasks'].items()}
    graph=dict(home_points=[[-4.,0.],[4.,0.],[0.,0.]],stations=[],legs=[],drone_homes=[None]*3,
        drone_count=3,max_tasks_per_drone=3,capacity=2,speed_km_per_min=.9,relay_allowed=False)
    parameters=dict(alns_fraction=.5,critical_limit=2,donor_limit=2,position_limit=4,
        option_count=2,beam_width=2,max_steps=1,snapshot_limit=3)
    for out in x['answers']:
        meta=out['metadata']
        proof=audit_metadata(tasks,graph,x['base'],meta,weights=(.5,.25,.25),
            scales=(9.,100.,100.),deadline=meta['deadline_perf_counter'],parameters=parameters)
        assert proof['passed'] and proof['final_score']==out['score']
        assert not proof['all_discarded_ALNS_or_exchange_entities_replayed']
