"""Independent pure-DIRECT geometric replay, without weighted solver imports."""
from pathlib import Path
import csv,hashlib,json,math
OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[2]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
m=json.loads((OUT/'manifest.json').read_text());rows=json.loads((OUT/'runs.json').read_text())
assert len(rows)==16 and [r['variant'] for r in rows]==m['order']
assert sha(OUT/'run.py')==m['runner_sha256'] and sha(OUT/'weighted_adapter.py')==m['adapter_sha256']
input_path=ROOT/'algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv'
assert sha(input_path)==m['input_sha256']
raw=input_path.read_bytes()
try:text=raw.decode('utf-8-sig')
except UnicodeDecodeError:text=raw.decode('gb18030')
tasks={int(t['任务编号']):t for t in csv.DictReader(text.splitlines())}
checks=[]
for r in rows:
 spec=m['variants'][r['variant']];assert sha(ROOT/spec['archive'])==spec['sha256']==r['source_sha256']
 assert r['seed']==m['seed'] and r['budget_seconds']==240 and r['valid'] and r['budget_compliant'] and r['end_to_end_seconds']<=240
 assert r['initial_routes_sha256']==m['initial_routes_sha256']
 path=OUT/r['solution_path'];assert sha(path)==r['solution_sha256'];p=json.loads(path.read_text())
 assert len(p['routes'])==8 and p['relay']['relay_task_count']==0
 picked=set();delivered=set();late=0;lateness=0.;distance=0.
 for route in p['routes']:
  cargo=set();count=0;x=y=elapsed=0.
  for v in route['encoded_visits']:
   task=tasks[abs(v)];prefix='取件点' if v>0 else '送达点';nx=float(task[prefix+'坐标X']);ny=float(task[prefix+'坐标Y'])
   d=math.hypot(nx-x,ny-y);distance+=d;elapsed+=d/.9;x,y=nx,ny
   if v>0:
    assert v not in picked;picked.add(v);cargo.add(v);assert len(cargo)<=2
   else:
    v=-v;assert v in cargo and v not in delivered;cargo.remove(v);delivered.add(v);count+=1
    due=float(task.get('最晚送达时间Tmax（min）',task.get('最晚送达时间Tmax(min)')));delay=max(0.,elapsed-due);late+=delay>1e-9;lateness+=delay
  assert not cargo and count<=25
 assert picked==delivered==set(tasks)
 for k,a in [('late_count',late),('total_lateness_min',lateness),('distance_km',distance)]:
  assert math.isclose(a,r[k],rel_tol=1e-10,abs_tol=1e-8),(r['variant'],k,a,r[k])
 J=.5*late/200+.25*lateness/m['scales'][1]+.25*distance/m['scales'][2];assert math.isclose(J,r['weighted_score'],abs_tol=1e-12)
 trajectory=r['best_trajectory']
 assert trajectory and math.isclose(trajectory[-1][2],late) and math.isclose(trajectory[-1][3],lateness,abs_tol=1e-8) and math.isclose(trajectory[-1][4],distance,abs_tol=1e-8)
 if r['objective']=='weighted':
  vals=[.5*t[2]/200+.25*t[3]/m['scales'][1]+.25*t[4]/m['scales'][2] for t in trajectory];assert all(b<=a+1e-12 for a,b in zip(vals,vals[1:]))
 checks.append(dict(variant=r['variant'],valid=True,weighted_score=J))
result={'passed':True,'runs':checks,'checks':['16 cells complete, single seed, 240s budget','source, input, adapter, runner, solution fingerprints','identical initial routes','independent Euclidean replay: all 200 tasks exactly once, precedence, capacity 2, K25, origin, no return','three physical metrics and scalar objective recomputed','final incumbent and weighted monotonic trajectory'],'serialization_note':'Historical CLI payload problem.objective_order is its legacy project metric ordering; the actual experiment objective is the runs.json objective field and manifest formula. The three raw score fields retain their physical meanings.'}
(OUT/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print('PASS: 16 independently replayed runs')
