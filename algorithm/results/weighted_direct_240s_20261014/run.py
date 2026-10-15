"""Frozen, one-seed, 240s pure-DIRECT comparison; no production modifications."""
from pathlib import Path
import dataclasses, hashlib, json, os, platform, shutil, subprocess, sys, tempfile, time
from zipfile import ZipFile
OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[2]
INPUT=ROOT/'algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv'
SEED=2026101401

def read(p): return json.loads(Path(p).read_text())
def write(p,x):
 p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);q=p.with_suffix(p.suffix+'.tmp');q.write_text(json.dumps(x,ensure_ascii=False,indent=2,allow_nan=False)+'\n');q.replace(p)
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def digest(x):return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def historical(folder,variant,label):
 d=ROOT/'algorithm/results'/folder;m=read(d/'manifest.json');s=m['source_snapshots'][variant];p=d/s['path'];assert sha(p)==s['sha256'];return dict(label=label,archive=str(p.relative_to(ROOT)),sha256=s['sha256'],config=s['config'],historical_manifest=str((d/'manifest.json').relative_to(ROOT)),historical_variant=variant,objective='weighted')

def prepare():
 if (OUT/'manifest.json').exists():raise ValueError('Already frozen')
 specs=[('original','optimization_20261013','baseline','最初 ALNS 基线'),('lazy','optimization_20261013','fast','惰性修复提速'),('allocation','optimization_20261013','allocation','跨机 regret'),('fallback','regret_followup_20261013','fallback','regret 位置回退'),('portfolio','regret_followup_20261013','portfolio','自适应 regret 组合'),('boundary','objective_trials_20261013','boundary','截止期边界邻域'),('reward','objective_trials_20261013','objective','第一目标奖励'),('compact','objective_trials_20261013/compact_trial','compact','缩小移除邻域'),('memo','method_trials_20261013','memo','DIRECT 选项缓存'),('cooling','method_trials_20261013','cooling','按预算降温'),('strings','method_trials_20261013/iteration2','strings','跨路线相邻串移除'),('adjacent','method_trials_20261013/iteration3','polish','相邻访问微调'),('joint','joint_relay_trials_20261014/batch1','joint','联合 VNS（禁用中继）'),('global','global_relay_trials_20261014','global','周期性全局尾段交换')]
 variants={k:historical(f,v,l) for k,f,v,l in specs}
 (OUT/'sources').mkdir(exist_ok=True)
 with ZipFile(OUT/'sources/current.zip','w') as z:
  for p in sorted((ROOT/'algorithm/src/uav_dispatch').glob('*.py')):z.write(p,'uav_dispatch/'+p.name)
 for k,obj in [('current_weighted','weighted'),('current_lex','lexicographic')]:
  variants[k]=dict(label='当前算法'+('加权' if obj=='weighted' else'原词典序'),archive=str((OUT/'sources/current.zip').relative_to(ROOT)),sha256=sha(OUT/'sources/current.zip'),config={},objective=obj)
 sys.path.insert(0,str(ROOT/'algorithm/src'))
 from uav_dispatch.io import load_tasks_csv
 from uav_dispatch.model import Problem
 from uav_dispatch.alns import construct_regret_initial
 p=Problem(load_tasks_csv(INPUT)[:200],drone_count=8,max_tasks_per_drone=25)
 initial=construct_regret_initial(p,candidate_limit=48,home_seed=False,home_bias=False)
 score=dataclasses.asdict(initial.evaluation.score)
 manifest=dict(seed=SEED,budget_seconds=240,weights=[.5,.25,.25],scales=[200,score['total_lateness_min'],score['distance_km']],initial_score=score,initial_routes_sha256=digest(initial.routes),input_sha256=sha(INPUT),variants=variants,order=['current_lex',*variants.keys()][:-2]+['current_weighted'],source_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT).decode().strip(),runner_sha256=sha(__file__),adapter_sha256=sha(OUT/'weighted_adapter.py'),environment=dict(python=sys.version,platform=platform.platform()),protocol='完整200任务/8机/原点/容量2/K25/无中继/无预部署。每版本一个共同种子；240秒端到端含构造、完整核验、写出解，不含导入；预留1秒收尾。加权仅实验进程启用。共同初解由每版原词典序构造并比对哈希；加权为0.5*逾期数/200+0.25*逾期分钟/共同初解逾期分钟+0.25*航程/共同初解航程；不择优重跑。',scope_notes=['中继 checkpoint/diversity/risk、下游中继修整、95%预热在纯DIRECT无作用，未重复运行。','原点下起点重分配无作用；全局版本仍包含尾段交换。','当前加权版本代表 terminal/rebalanced 在纯DIRECT下的最终实现。','加权修改整体解比较、插入排序、greedy/regret修复排序、模拟退火接受；保留各历史方法本身的破坏启发式、第一目标奖励、VNS扰动逾期上限。','这是加权适配后的历史方法，不等同于历史原目标结果；另跑当前原目标作同预算控制。'])
 assert len(manifest['order'])==16 and len(set(manifest['order']))==16
 write(OUT/'manifest.json',manifest);write(OUT/'runs.json',[])
 print(json.dumps({'order':manifest['order'],'initial':score,'scales':manifest['scales']},ensure_ascii=False))

def worker(reqpath):
 r=read(reqpath);m=read(OUT/'manifest.json')
 from uav_dispatch.alns import ALNSConfig,construct_regret_initial,solve_alns
 from uav_dispatch.io import load_tasks_csv
 from uav_dispatch.model import Problem,Score
 from uav_dispatch.validation import evaluate_solution
 from uav_dispatch.cli import result_payload
 import weighted_adapter as wa
 t=time.perf_counter();p=Problem(load_tasks_csv(INPUT)[:200],drone_count=8,max_tasks_per_drone=25)
 initial=construct_regret_initial(p,candidate_limit=48,home_seed=False,home_bias=False)
 assert digest(initial.routes)==m['initial_routes_sha256'], 'Initial routes differ'
 if r['objective']=='weighted': wa.enable(Score,m['scales'])
 construction=time.perf_counter()-t
 cfg=dict(max_iterations=r.get('iterations',10000000),seed=SEED,time_limit_seconds=max(.001,r['budget']-construction-1),candidate_limit=48)
 overrides=dict(r['config']);kind=overrides.pop('solver','alns')
 if kind=='joint':
  from uav_dispatch.joint_relay import JointRelayConfig,solve_joint_relay
  cfg['candidate_limit']=12;config=JointRelayConfig(**cfg);solver=solve_joint_relay
 else:
  cfg.update(relay_enabled=False,enable_home_seed=False,enable_home_bias=False,relay_direct_warmup_fraction=.8,**overrides);config=ALNSConfig(**cfg);solver=solve_alns
 start=time.perf_counter();result=solver(p,config=config,initial_routes=initial.routes);search=time.perf_counter()-start
 check=evaluate_solution(p,result.routes,_force_full=True)
 assert check.valid and check.score==result.evaluation.score
 payload=result_payload(p,dataclasses.replace(result,evaluation=check),source=str(INPUT.relative_to(ROOT)))
 write(r['solution'],payload)
 elapsed=time.perf_counter()-t
 s=dataclasses.asdict(check.score);J=.5*s['late_count']/m['scales'][0]+.25*s['total_lateness_min']/m['scales'][1]+.25*s['distance_km']/m['scales'][2]
 row=dict(variant=r['variant'],label=r['label'],objective=r['objective'],seed=SEED,budget_seconds=r['budget'],end_to_end_seconds=elapsed,construction_seconds=construction,search_seconds=search,valid=check.valid,budget_compliant=elapsed<=r['budget'],iterations=result.iterations,**s,weighted_score=J,on_time_rate=1-s['late_count']/200,initial_routes_sha256=digest(initial.routes),source_sha256=r['sha256'],config=dataclasses.asdict(config),solution_path=str(Path(r['solution']).relative_to(OUT)),solution_sha256=sha(r['solution']),best_trajectory=result.metadata.get('best_trajectory',[]),adapter_changes=r['adapter_changes'])
 write(r['response'],row)

def invoke(name,m,smoke=False):
 s=m['variants'][name];archive=ROOT/s['archive'];assert sha(archive)==s['sha256'] and sha(INPUT)==m['input_sha256']
 import weighted_adapter as wa
 with tempfile.TemporaryDirectory(prefix='weighted-direct-') as tmp:
  tmp=Path(tmp)
  with ZipFile(archive) as z:
   assert all(n.startswith('uav_dispatch/') and '..' not in Path(n).parts for n in z.namelist());z.extractall(tmp)
  changes={}
  if s['objective']=='weighted':
   for f in ['alns.py','search.py']:
    p=tmp/'uav_dispatch'/f;new,counts=wa.transform(p.read_text());p.write_text(new);changes[f]=counts
   assert changes['alns.py']['acceptance']==1 and changes['alns.py']['regret_keys']==2 and changes['search.py']['raw_insertion_keys']==2,changes
  solution=OUT/('smoke' if smoke else'run_solutions')/(name+'.json')
  assert not solution.exists(), 'Never overwrite a run'
  req=dict(**s,variant=name,budget=15 if smoke else 240,solution=str(solution),response=str(tmp/'response.json'),adapter_changes=changes)
  if smoke:req['iterations']=2
  write(tmp/'request.json',req)
  env={**os.environ,'PYTHONPATH':str(tmp),'PYTHONDONTWRITEBYTECODE':'1'}
  subprocess.run([sys.executable,str(Path(__file__).resolve()),'worker',str(tmp/'request.json')],cwd=ROOT,env=env,check=True,timeout=req['budget']+30)
  row=read(tmp/'response.json')
 print(json.dumps({k:row[k] for k in ['variant','late_count','total_lateness_min','distance_km','weighted_score','end_to_end_seconds','valid','budget_compliant']},ensure_ascii=False),flush=True)
 return row

def run(smoke=False):
 m=read(OUT/'manifest.json');assert sha(__file__)==m['runner_sha256'] and sha(OUT/'weighted_adapter.py')==m['adapter_sha256']
 dest=OUT/('smoke_runs.json' if smoke else 'runs.json');rows=read(dest) if dest.exists() else [];done={r['variant'] for r in rows}
 for i,name in enumerate(m['order']):
  if name in done:continue
  print(f'START {i+1}/16 {name}',flush=True)
  row=invoke(name,m,smoke);rows.append(row);write(dest,rows)
  if not row['valid'] or not row['budget_compliant']:raise RuntimeError('Invalid or over budget, retained for diagnosis')
if __name__=='__main__':
 cmd=sys.argv[1]
 if cmd=='prepare':prepare()
 elif cmd=='worker':worker(sys.argv[2])
 else:run(cmd=='smoke')
