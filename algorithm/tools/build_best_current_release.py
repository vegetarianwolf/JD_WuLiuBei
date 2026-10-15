"""Package only the winning DIRECT run, with a standalone, stdlib-only entry."""
from pathlib import Path
import ast
import csv
import hashlib
import io
import json
import shutil
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
BATCH = ROOT / "algorithm/results/proactive_relay_diagnostic_20261014/batch040"
NAME = "JD_BEST_WEIGHTED_DIRECT_20261015"
DEST = ROOT / "algorithm/releases"

VERIFY = r'''"""Independent CSV/geometry validation; no solver code is imported."""
from pathlib import Path
from math import hypot, isclose, isfinite
import csv
import hashlib
import io
import json

ROOT = Path(__file__).resolve().parent

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def verify_package():
    manifest = read(ROOT / 'PACKAGE_MANIFEST.json')
    for name, spec in manifest['files'].items():
        p = ROOT / name
        raw = p.read_bytes()
        if len(raw) != spec['bytes'] or hashlib.sha256(raw).hexdigest() != spec['sha256']:
            raise ValueError('Package file changed: ' + name)
    return len(manifest['files'])

def verify_result(path=None):
    cfg = read(ROOT / 'input/run_config.json')
    result = read(path or ROOT / 'result/BEST_RESULT.json')
    raw = (ROOT / 'input/official_tasks.csv').read_bytes()
    if hashlib.sha256(raw).hexdigest() != cfg['input_sha256']:
        raise ValueError('Official input hash mismatch')
    for encoding in ('utf-8-sig', 'gb18030'):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('Unknown CSV encoding')
    rows = list(csv.reader(io.StringIO(text)))
    tasks = {int(r[0]): tuple(map(float, r[1:6])) for r in rows[1:] if r}
    assert len(tasks) == 200 and set(tasks) == set(range(1, 201))
    assert len(result['routes']) == 8
    assert result['valid'] is True and result['violations'] == []
    problem = result['problem']
    for field, value in [('task_count', 200), ('drone_count', 8), ('capacity', 2),
                         ('max_tasks_per_drone', 25), ('speed_km_per_min', .9), ('open_routes', True)]:
        assert problem[field] == value, field
    expected_homes = ['origin' if h is None else 'station_' + str(h) for h in cfg['drone_homes']]
    assert problem['drone_homes'] == expected_homes
    assert result['relay']['relay_task_count'] == 0
    assert set(result['task_plans']) == {str(t) for t in tasks}
    assert all(p['mode'] == 'DIRECT' and p['leg_ids'] == [] and p['relay_id'] is None
               for p in result['task_plans'].values())
    delivered = {}; picked = set(); total_distance = 0.; per_drone = []
    for a, route in enumerate(result['routes']):
        assert route['drone_id'] == a + 1
        pos = cfg['home_points'][a]; clock = 0.; distance = 0.; carried = set(); own = []
        visits = route['encoded_visits']
        assert len(visits) == 50 and len(route['visits']) == len(visits)
        for k, v in enumerate(visits):
            assert type(v) is int and abs(v) in tasks
            task = abs(v); row = tasks[task]
            point = row[:2] if v > 0 else row[2:4]
            leg = hypot(point[0] - pos[0], point[1] - pos[1])
            distance += leg; clock += leg / .9; pos = point
            if v > 0:
                assert task not in picked
                picked.add(task); carried.add(task)
            else:
                assert task in carried and task not in delivered
                carried.remove(task); delivered[task] = clock; own.append(task)
            assert len(carried) <= 2
            event = route['visits'][k]
            assert event['task_id'] == task and event['leg_id'] is None
            assert event['action'] == ('pickup' if v > 0 else 'delivery')
            assert all(isclose(x, y, abs_tol=1e-8) for x, y in zip(event['location_km'], point))
            assert isclose(event['arrival_time_min'], clock, abs_tol=1e-7)
        assert not carried and len(own) == 25
        late = sum(delivered[t] > tasks[t][4] + 1e-9 for t in own)
        lateness = sum(max(0., delivered[t] - tasks[t][4]) for t in own)
        assert isclose(route['distance_km'], distance, abs_tol=1e-7)
        assert isclose(route['completion_time_min'], clock, abs_tol=1e-7)
        assert route['late_count'] == late and route['task_count'] == 25
        assert isclose(route['total_lateness_min'], lateness, abs_tol=1e-7)
        per_drone.append(dict(drone_id=a+1, on_time=25-late, late=late,
                              distance_km=distance, lateness_min=lateness))
        total_distance += distance
    assert picked == set(tasks) and set(delivered) == set(tasks)
    assert set(result['delivery_times_min']) == {str(t) for t in tasks}
    for task, time in delivered.items():
        assert isclose(result['delivery_times_min'][str(task)], time, abs_tol=1e-7)
    score = dict(late_count=sum(delivered[t] > tasks[t][4] + 1e-9 for t in tasks),
                 total_lateness_min=sum(max(0., delivered[t]-tasks[t][4]) for t in tasks),
                 distance_km=total_distance)
    for key, value in score.items():
        assert isfinite(value) and isclose(value, result['score'][key], abs_tol=1e-7), key
    j = sum(w * score[k] / s for k, w, s in zip(
        ('late_count', 'total_lateness_min', 'distance_km'), cfg['weights'], cfg['scales']))
    return dict(passed=True, score=score, weighted_J=j,
                on_time_count=200-score['late_count'], relay_task_count=0, per_drone=per_drone)

if __name__ == '__main__':
    print(json.dumps(dict(package_files_checked=verify_package(), **verify_result()),
                     ensure_ascii=False, indent=2))
'''

RUN = r'''"""Standalone entry for the one packaged weighted DIRECT method."""
from pathlib import Path
import argparse
from dataclasses import asdict, replace
import json
from math import isclose, isfinite
import sys
from time import perf_counter
from types import MappingProxyType

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'src'))
from verify import read, verify_package, verify_result
import multi_relay_direct_control_v1 as best
import weighted_direct_objective as weighted
from uav_dispatch import alns
from uav_dispatch.cli import result_payload
from uav_dispatch.io import load_tasks_csv
from uav_dispatch.model import Problem, RelayStation, TransportLeg, Score
from uav_dispatch.search import SolverResult
from uav_dispatch.validation import evaluate_solution

def build_problem(cfg):
    tasks = load_tasks_csv(ROOT / 'input/official_tasks.csv')
    stations = tuple(RelayStation(*row) for row in cfg['stations'])
    registry = {}; candidates = {}; counter = max(t.id for t in tasks) + 1
    for task in sorted(tasks, key=lambda t: t.id):
        candidates[task.id] = tuple(s.id for s in stations)
        for station in stations:
            registry[counter] = TransportLeg(counter, task.id, 'RELAY_IN', station.id)
            registry[counter+1] = TransportLeg(counter+1, task.id, 'RELAY_OUT', station.id)
            counter += 2
    full = Problem(tasks=tuple(tasks), drone_count=8, max_tasks_per_drone=25,
                   capacity=2, speed_km_per_min=.9, drone_homes=tuple(cfg['drone_homes']),
                   relay_stations=stations, leg_registry=registry, task_relay_candidates=candidates)
    assert [[p.x,p.y] for p in full.home_points()] == cfg['home_points']
    return full, replace(full, leg_registry={}, task_relay_candidates={})

def main():
    parser = argparse.ArgumentParser(description='当前加权最优直送方案；Python 3.11+，无需第三方包。')
    parser.add_argument('mode', choices=('verify', 'solve'), nargs='?', default='verify')
    parser.add_argument('--seconds', type=float, default=240., help='新运行总预算；不会恢复历史 /goal')
    parser.add_argument('--output', type=Path, default=Path('new_result.json'))
    parser.add_argument('--from-scratch', action='store_true', help='重新构造初解；不保证历史最佳成绩')
    args = parser.parse_args()
    count = verify_package()
    cfg = read(ROOT / 'input/run_config.json')
    full, plain = build_problem(cfg)
    routes = tuple(tuple(r) for r in cfg['initial_routes'])
    checked = evaluate_solution(full, routes, _force_full=True)
    assert checked.valid
    for key, value in cfg['initial_score'].items():
        assert isclose(getattr(checked.score, key), value, abs_tol=1e-8), key
    assert alns._weighted_adapter is weighted
    if args.mode == 'verify':
        print(json.dumps(dict(package_files_checked=count, runtime_imports_ok=True,
                              initial_routes_valid=True, **verify_result()), ensure_ascii=False, indent=2))
        return
    if not isfinite(args.seconds) or args.seconds < 2:
        parser.error('--seconds must be finite and >= 2')
    output = args.output.resolve()
    if output.exists() or output == ROOT or ROOT in output.parents and output.parts[len(ROOT.parts)] in ('src','input','result','evidence','docs'):
        parser.error('Select a new output file; never overwrite the packaged result or sources')
    if not output.parent.is_dir():
        parser.error('Output parent directory must already exist')
    started = perf_counter(); deadline = started + args.seconds - 1.
    if args.from_scratch:
        initial = alns.construct_regret_initial(plain, candidate_limit=48, home_seed=True, home_bias=True)
        routes = initial.routes
    initial_checked = evaluate_solution(full, routes, _force_full=True)
    if not initial_checked.valid:
        raise ValueError('Initial routes are invalid')
    with weighted.objective(Score, cfg['scales'], cfg['weights']):
        outcome = best.refine(full, routes, deadline=deadline, weights=cfg['weights'],
                              scales=cfg['scales'], seed=cfg['seed'], relay_enabled=False,
                              **cfg['parameters'])
        checked = evaluate_solution(plain, outcome['routes'], _force_full=True)
        if not checked.valid or checked.score > initial_checked.score:
            raise ValueError('The complete result failed validation/incumbent guard')
        payload = result_payload(plain, SolverResult(outcome['routes'], checked,
            runtime_seconds=perf_counter()-started, iterations=outcome['iterations'],
            metadata=MappingProxyType(dict(method=cfg['method'], seed=cfg['seed'],
                historical_checkpoint=not args.from_scratch, packaged_launcher=True))),
            source='input/official_tasks.csv')
        payload['problem']['objective_order'] = ['weighted_total_score','late_count','total_lateness_min','distance_km']
        payload['problem']['experiment_objective'] = dict(kind='weighted',weights=cfg['weights'],scales=cfg['scales'])
        payload['weighted_score'] = weighted.scalar(checked.score.late_count,
            checked.score.total_lateness_min, checked.score.distance_km)
        payload['run_budget'] = dict(ceiling_seconds=args.seconds, finalization_reserve_seconds=1.,
            same_original_experiment_harness=False, timing='Standalone solve/final validation/write, imports and initial package checks excluded')
        with output.open('x', encoding='utf-8') as stream:
            json.dump(payload,stream,ensure_ascii=False,indent=2); stream.write('\n')
        elapsed = perf_counter()-started
        print(json.dumps(dict(output=str(output),score=asdict(checked.score),weighted_J=payload['weighted_score'],
                              actual_seconds=elapsed,budget_compliant=elapsed<=args.seconds),ensure_ascii=False,indent=2))

if __name__ == '__main__':
    main()
'''


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.write_text(value, encoding="utf-8")


def build():
    manifest = json.loads((BATCH / 'manifest.json').read_text())
    audit = json.loads((BATCH / 'audit.json').read_text())
    run = next(r for r in json.loads((BATCH / 'runs.json').read_text()) if r['arm'] == 'multi_native_direct')
    final = BATCH / run['solution_path']
    assert sha(final) == run['solution_sha256']
    solution = json.loads(final.read_text())
    direct_audit = next(r for r in audit['results'] if r['run_id'] == run['run_id'])
    assert direct_audit['passed'] and audit['passed']
    with tempfile.TemporaryDirectory(prefix='jd_best_package_build_') as temp:
        package = Path(temp) / NAME
        (package / 'src').mkdir(parents=True)
        source = BATCH / 'sources'
        # Only the transitive import closure of this one DIRECT method, not the 108-module research harness.
        seen = set(); todo = ['multi_relay_direct_control_v1']; bindings = {}
        while todo:
            name = todo.pop()
            if name in seen or not (source / (name+'.py')).is_file():
                continue
            seen.add(name); src = source / (name+'.py')
            assert sha(src) == manifest['execution_sources'][src.name]['sha256']
            shutil.copyfile(src, package / 'src' / src.name)
            bindings['src/'+src.name] = dict(original_sha256=sha(src),original_path=str(src.relative_to(ROOT)))
            for node in ast.walk(ast.parse(src.read_text())):
                if isinstance(node, ast.Import):
                    todo.extend(a.name.split('.')[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    todo.extend([node.module.split('.')[0]] if node.module else [a.name for a in node.names])
        archive = BATCH / manifest['source_archive']['path']
        assert sha(archive) == manifest['source_archive']['sha256']
        # Keep the small frozen solver library byte-for-byte; it includes the original weighted transformations.
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                assert name.startswith('uav_dispatch/') and name.endswith('.py') and '..' not in Path(name).parts
                raw = z.read(name); out = package / 'src' / name; out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(raw)
                bindings['src/'+name] = dict(original_sha256=hashlib.sha256(raw).hexdigest(),original_archive_sha256=sha(archive))
        input_path = ROOT / manifest['input_path']; (package/'input').mkdir()
        shutil.copyfile(input_path,package/'input/official_tasks.csv')
        topology = manifest['topology']
        config = dict(method='Independent-Weighted-DIRECT-ALNS-And-Complete-Exchange-V1',
            seed=run['seed'], weights=manifest['weights'],scales=manifest['scales'],
            parameters=manifest['method_parameters']['multi_native_direct'],
            stations=topology['stations'],drone_homes=topology['drone_homes'],home_points=topology['home_points'],
            initial_routes=manifest['initial_routes'],initial_score=manifest['initial_score'],
            input_sha256=manifest['input_sha256'],historical_checkpoint_required=True)
        write(package/'input/run_config.json',config)
        (package/'result').mkdir();shutil.copyfile(final,package/'result/BEST_RESULT.json')
        csv_rows = list(csv.reader(io.StringIO(input_path.read_bytes().decode('gb18030'))))
        deadlines = {int(r[0]):float(r[5]) for r in csv_rows[1:] if r}
        task_results = []
        for route in solution['routes']:
            pickups = {v['task_id']:v['arrival_time_min'] for v in route['visits'] if v['action']=='pickup'}
            for event in route['visits']:
                if event['action']!='delivery':
                    continue
                t=event['task_id']; clock=event['arrival_time_min']; due=deadlines[t]
                task_results.append(dict(task_id=t,drone_id=route['drone_id'],mode='DIRECT',
                    pickup_time_min=pickups[t],delivery_time_min=clock,deadline_min=due,
                    lateness_min=max(0.,clock-due),on_time=clock<=due+1e-9))
        task_results.sort(key=lambda r:r['task_id'])
        write(package/'result/TASK_RESULTS.json',task_results)
        uav_results = [{k:v for k,v in route.items() if k not in ('visits','encoded_visits')} for route in solution['routes']]
        write(package/'result/UAV_RESULTS.json',uav_results)
        experiment = dict(run_id=run['run_id'],seed=run['seed'],
            setting='Official200, 8 UAVs, C2, final K25, fixed four-station predeployment, pure DIRECT, open routes',
            score=solution['score'],weighted_J=run['weighted_score'],on_time_count=run['on_time_count'],
            on_time_rate=solution['on_time_rate'],relay_task_count=0,cross_uav_handoff_count=0,
            budget_seconds=run['budget_seconds'],actual_end_to_end_seconds=run['end_to_end_seconds'],
            budget_compliant=run['budget_compliant'],valid=run['valid'],original_independent_audit_passed=direct_audit['passed'],
            phases=[dict(phase=p['phase'],score=p['score'],weighted_J=p['weighted_score'],
                **{k:p[k] for k in ('solve_wall_seconds','iterations') if k in p}) for p in run['phases']],
            original_solution_sha256=sha(final),
            evidence_limit='One historical-checkpoint continuation; not a fresh 240-second solve or a multi-seed superiority claim.')
        write(package/'result/EXPERIMENT_RESULTS.json',experiment)
        write(package/'verify.py',VERIFY);write(package/'run.py',RUN)
        write(package/'requirements.txt','# Python >=3.11; standard library only. No pip install is required.\n')
        write(package/'evidence/audit_excerpt.json',dict(
            extraction='Exact original audit results entry for this DIRECT run only; other arms omitted.',
            original_audit_sha256=sha(BATCH/'audit.json'),original_manifest_sha256=sha(BATCH/'manifest.json'),
            original_runs_sha256=sha(BATCH/'runs.json'),result=direct_audit))
        slim = {k:v for k,v in run.items() if k not in ('phases','counterfactual_path','counterfactual_sha256')}
        slim['phases'] = [{k:v for k,v in p.items() if k not in ('metadata','topology')} for p in run['phases']]
        slim['extraction_note'] = 'Only the winning DIRECT run; large search telemetry and unrelated relay arm omitted. Paths name original records, not files required by this package.'
        write(package/'evidence/run_excerpt.json',slim)
        provenance = dict(original_run_id=run['run_id'],source_solution_sha256=sha(final),
            original_solver_archive_sha256=sha(archive),source_bindings=bindings,
            source_code_unchanged=True,standalone_launcher_new=True,
            standalone_launcher_is_not_the_original_measurement_harness=True,
            historical_input_routes_source_sha256=manifest['checkpoint']['sha256'],
            source_repository='https://github.com/vegetarianwolf/JD_WuLiuBei',
            source_commit='df2fde3fac408a8b87abe12cccfb7d802b3ca98f')
        write(package/'evidence/provenance.json',provenance)
        readme = '''# 当前加权最优方案：精简可运行版

仅包含本次最佳 DIRECT 运行的算法、数据、结果与说明。Python 3.11+；不需要联网、仓库其他文件或第三方依赖。最先打开 `docs/REPORT.html` 看结果，或按下面命令使用。

## 直接使用

解压，在本目录打开终端（Windows 可将 `python3` 换成 `python`）：

```bash
python3 run.py verify
```

核对文件哈希、完整运行时导入、初始路线与全部200单的最终可行性；不启动搜索。
`python3 verify.py` 还可只用独立的CSV/欧氏几何检查，不导入算法。

```bash
python3 run.py solve --seconds 240 --output new_result.json
```

用本次相同参数、种子和必需初始路线续跑，写入新文件，不覆盖历史最佳。
若要从零构造初解，可增加 `--from-scratch`，其成绩不能与历史追加搜索预算混为一谈。
API：`src/multi_relay_direct_control_v1.py` 中的 `refine(...)`；通常直接用上面的入口即可。
时间限制影响迭代数，机器/版本不同不保证新运行逐字得到同一解；保存的最佳结果可直接读取使用。

## 文件

- `result/BEST_RESULT.json`：原始最佳完整解，逐字保留，含路线和全部送达时刻。
- `result/EXPERIMENT_RESULTS.json`：本次实验指标、预算/实际用时、种子与各阶段结果；`TASK_RESULTS.json`：200单逐项时效；`UAV_RESULTS.json`：8机统计。
- `input/official_tasks.csv`：官方200任务原始数据；`run_config.json`：固定四站、起点、权重、参数及本次必须的初始路线。
- `src/`：本次方法实际导入的16个模块和冻结求解器库19个文件。复用辅助函数的历史模块名保留以维持源码原样，不附历史实验。
- `docs/REPORT.md` / `REPORT.html`：仅本次最佳结果的报告摘编、算法说明、指标和适用边界。
- `evidence/`：只抽取本次DIRECT审计与运行记录，并记录逐文件源码来源。
- `PACKAGE_MANIFEST.json`：包内文件SHA256清单。生成的新结果放在包外或目录根部，不列入历史清单。

本方案实际中继0单，不是中继获胜结果。J为研究加权偏好，不是题目官方分数；“最优”指当前保存的最低J合法终解，未证明数学全局最优。本次历史结果来自强解追加搜索，不能称为从零240秒获得。`/goal` 保持暂停；打包没有恢复优化任务。
'''
        write(package/'README.md',readme)
        rows = solution['routes']
        report = '''# 最佳直送方案：本次运行报告摘编

本摘编只描述 `multi_native_direct__seed_2026101527` 的已保存最佳终解，不包含其他方案或旧实验。

## 问题与结果

官方静态200任务；8架无人机；容量2；速度0.9 km/min；每架最终配送25单；先取后送；软截止期；开放路线不返航。4个固定站作为预部署起点，起点分配为原点1架/H1两架/H2两架/H3两架/H4一架。所有飞行计入航程和时刻，无额外资源、免费调位或中继奖励。

| 指标 | 本次结果 |
| --- | ---: |
| 按时完成 | 140/200（70.0%） |
| 逾期单量 | 60 |
| 总逾期分钟 | 1716.285265309008 |
| 总航程 km | 573.996325295917 |
| 加权 J（越低越好） | 0.431089741253357 |
| 实际中继/跨机交接 | 0/0 |
| 本次追加预算上限/实际秒 | 240 / 195.682509 |
| 随机种子 | 2026101527 |

J = 0.5 × 逾期数/200 + 0.25 × 总逾期分钟/4464.183540734935 + 0.25 × 航程/775.7731679286865。完整J作为比较准则，相同J再以原物理指标打破平局。

## 算法

1. 读取本次强直送输入，完整验证覆盖、取送顺序、容量、最终配额和实际时钟，保留输入作最终保护。
2. 65%的方法时间片运行冻结的加权ALNS：惰性修复、末端完整任务互换；主循环每120次修复检查一对路线，至多16个精评、8ms动作预算。所有200任务均可调整分机和顺序。
3. 剩余时间做普通完整任务联合修复：真实配额缺槽、有限因果前缀/供体提案、取送位置短名单与完整候选核验。参数为critical_limit=6、donor_limit=3、position_limit=12、option_count=3、beam_width=4、max_steps=3。
4. 仅采用完整合法且加权键严格改善的终解；到时返回已验证完整缓存，不返回未修复的部分解。没有生成中继目录。

这些是本次方法必需的代码组成，`src`保留原源码名称和内容；精简启动器只负责加载固定参数、校验和写出新文件。

## 每架无人机

| 无人机 | 最终单量 | 按时单量 | 逾期单量 | 航程/km | 总逾期/min |
| --- | ---: | ---: | ---: | ---: | ---: |
'''
        for r in rows:
            report += f"| {r['drone_id']} | {r['task_count']} | {r['on_time_task_count']} | {r['late_count']} | {r['distance_km']:.6f} | {r['total_lateness_min']:.6f} |\n"
        report += '''
## 校验和证据边界

原始独立审计通过；本包 `evidence/audit_excerpt.json` 逐值提取该DIRECT运行的原审计条目，保留原文件SHA，并未把摘录冒充整批原件。精简包额外提供独立CSV/几何校验：200单各取送一次、同机直送、逐机C2及25单、所有事件/交付时刻、逐机与全队N/L/D、真实固定起点、J和文件哈希。

这次终解来自历史强解追加预算；本包只保留必要的初始路线，不打包旧报告或旧方案。原实验计时含数据/网络构造、验证和写出，独立精简入口直接加载冻结网络，省略研究框架及无关对照，因而不是原始计时执行器；重新运行会如实报告自己的用时。它不能替代从零、相同总预算、多种子的性能证据，也不能证明中继优越或全局最优。
'''
        write(package/'docs/REPORT.md',report)
        # A static, offline report containing this result only; no JS, CDN or repository links.
        table = ''.join(f"<tr><td>{r['drone_id']}</td><td>25</td><td>{r['on_time_task_count']}</td><td>{r['late_count']}</td><td>{r['distance_km']:.3f}</td><td>{r['total_lateness_min']:.3f}</td></tr>" for r in rows)
        page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>本次加权最优直送结果</title><style>body{font:16px/1.75 system-ui,sans-serif;max-width:1000px;margin:40px auto;padding:0 20px;color:#17233b;background:#f6f8fb}h1{font-size:30px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.card,section{background:white;padding:20px;border:1px solid #dde3ed;border-radius:12px;margin:14px 0}.card b{font-size:27px;display:block;color:#126451}table{width:100%;border-collapse:collapse}th,td{padding:9px;text-align:right;border-bottom:1px solid #dde3ed}.scroll{overflow-x:auto}code{background:#eaf0f6;padding:2px 5px}a{color:#12599c}</style><h1>本次加权最优 · 预部署纯直送</h1><p>官方200任务 · 8机 · C2 · 每机最终25单 · 开放路线 · 种子2026101527</p><div class="cards"><div class="card">按时完成<b>140 / 200</b>70.0%</div><div class="card">总逾期时间<b>1716.285</b>分钟</div><div class="card">全队航程<b>573.996</b>公里</div><div class="card">加权 J ↓<b>0.431090</b>实际中继0单</div></div><section><h2>算法与使用</h2><p>冻结的加权ALNS，加惰性修复、末端/周期完整任务互换，再做普通完整任务联合修复；完整可行性和原强解保护。</p><p>解压后执行 <code>python3 run.py verify</code> 校验；执行 <code>python3 run.py solve --seconds 240 --output new_result.json</code> 运行。Python 3.11+，无需安装依赖。</p><p><a href="../README.md">使用说明</a> · <a href="REPORT.md">完整摘编</a> · <a href="../result/BEST_RESULT.json">原始完整结果</a></p></section><section><h2>逐机指标</h2><div class="scroll"><table><thead><tr><th>无人机</th><th>单量</th><th>按时</th><th>逾期</th><th>航程/km</th><th>逾期/min</th></tr></thead><tbody>'''+table+'''</tbody></table></div></section><section><h2>实验结果文件</h2><p><a href="../result/EXPERIMENT_RESULTS.json">实验汇总与阶段记录</a> · <a href="../result/UAV_RESULTS.json">8机统计</a> · <a href="../result/TASK_RESULTS.json">200单逐项结果</a> · <a href="../evidence/audit_excerpt.json">原独立审计摘录</a></p></section><section><h2>指标口径</h2><p>J = 0.5 × 逾期单量/200 + 0.25 × 总逾期分钟/4464.183540734935 + 0.25 × 航程/775.7731679286865；这是研究偏好权重，不是题目官方综合分。</p><p>本次追加预算上限240秒，实际195.682509秒。输入是此前已求得的强解；不能称为从零240秒获得。固定四站和预部署位置已写入配置，所有飞行计费。</p><p>原独立审计通过，提供仅本次运行的审计摘录，以及可独立重算的CSV/几何核验入口。“最优”表示当前保存的最低J已审计正式终解，未证明全局最优。此结果不支持中继远胜直送的结论。</p></section></html>'''
        write(package/'docs/REPORT.html',page)
        specs = {str(p.relative_to(package)):dict(bytes=p.stat().st_size,sha256=sha(p))
                 for p in sorted(package.rglob('*')) if p.is_file()}
        write(package/'PACKAGE_MANIFEST.json',dict(name=NAME,created_date='2026-10-15',python='>=3.11',third_party_dependencies=[],
            selection='Only the best weighted DIRECT run and its necessary execution dependencies; no historical experiments, alternative results, or unrelated relay arm.',files=specs))
        DEST.mkdir(exist_ok=True)
        output = DEST / '最优方案_精简可运行版_20261015.zip'
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as z:
            for p in sorted(package.rglob('*')):
                if p.is_file():
                    info = zipfile.ZipInfo(str(Path(NAME)/p.relative_to(package)),(2026,10,15,0,0,0))
                    info.compress_type=zipfile.ZIP_DEFLATED; info.external_attr=0o644<<16
                    z.writestr(info,p.read_bytes(),compresslevel=9)
        write(DEST/'最优方案_精简可运行版_20261015.sha256',sha(output)+'  '+output.name+'\n')
        print(json.dumps(dict(zip=str(output),bytes=output.stat().st_size,files=len(specs)+1,
            frozen_method_modules=len(seen),frozen_runtime_modules=19,sha256=sha(output)),ensure_ascii=False,indent=2))


if __name__ == '__main__':
    build()
