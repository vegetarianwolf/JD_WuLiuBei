# 物流无人机调度算法

本目录实现容量为 2 的开放式多无人机取送货调度。每项任务必须先取后送，
最后一次送达后不返航；软截止期允许逾期但所有任务必须完成。所有求解器均按
以下三级目标严格词典序比较：

1. 逾期任务数；
2. 总逾期时间（min）；
3. 总航程（km）。

## 当前方法

主算法为 `C2-Lex-ALNS`。算法注册表固定为 14 个有效算子：

- 破坏算子（9）：`random`、`worst_distance`、`worst_lex`、
  `spatial_related`、`deadline_related`、`late_critical`、
  `route_segment`、`capacity_conflict`、`assignment_destroy`；
- 修复算子（5）：`greedy`、`regret2`、`regret3`、`deadline`、`slack`。

程序不包含未进入最终方法的 VND、Ejection Swap、Route Pool 或 Cluster
Regret 分支。每次 ALNS 运行会在 `metadata.operator_statistics` 中记录所有
14 个算子的使用次数、接受次数、当前解改进次数、全局最优改进次数和奖励。

固定中继站由加权 k-medoids 生成；`--relay-count auto` 可按覆盖收益自动
选站数。`--drone-homes stations` 用需求得分、Hamilton 最大余数分配和轻量
局部调整，将无人机预部署到原点/中继站。Relay 交接由 precedence DAG 做
全局时序校验，允许异步先卸后取并计入等待时间。

小规模 `Pareto-DP` 是单机、公共原点、Direct-only 的精确 oracle，默认最多
10 个任务；可用 `--exact-max-tasks` 调高，但状态空间呈指数增长。CLI 选择
`--method exact` 时会强制上述语义，避免把不支持的 Relay/异地起点结果误称
为精确解。

## 结构

```text
src/uav_dispatch/
├── model.py          问题、任务、中继腿和三级词典序评分
├── validation.py     独立完整可行性与时序校验
├── search.py         路线评估、插入与缓存
├── exact.py          小规模 Pareto-DP 精确 oracle
├── relay.py          固定站点、Relay 方案和全局交接调度
├── deployment.py     需求驱动的无人机初始预部署
├── alns.py           9+5 算子 ALNS 与 Relay 分阶段求解
└── cli.py            命令行入口和 JSON 序列化
experiments/
├── run_benchmarks.py             第5章精确/多规模实验
├── run_virtual_ablation.py       第5.5节虚拟算法对比与组件消融
├── run_scenario_comparison.py    第6.1节四场景×三种子实验
├── run_sensitivity_analysis.py   第6.2节三参数单因素灵敏度
└── final_plot_style.py           论文结果图公共样式
```

## 安装、测试与单次求解

```bash
python -m pip install -r requirements-dev.txt -e .
python -m pytest -q -p no:cacheprovider algorithm/tests
```

不安装包时，从仓库根目录运行：

```bash
PYTHONPATH=algorithm/src python -m uav_dispatch solve \
  --input 'algorithm/命题1-低空经济场景下的物流无人机调度算法数据.csv' \
  --method alns --tasks 200 --drones 8 --max-tasks 25 \
  --time-limit 300 --iterations 10000000 --seed 2026081701 \
  --relay-count auto --drone-homes stations \
  --output algorithm/results/solution.json
```

`--method` 还支持 `exact`、`edd`、`nearest`、`greedy`、`regret2`。
`--disable-relay` 关闭中继；`--drone-homes origin` 让全部无人机从原点起飞。

## 正式实验协议

第 5 章先精确复现两任务单机例（最优访问序列
`P1→P2→D2→D1`、11 km），再对官方数据前 5/8/10/12 个任务做精确
对照，并在截止期统一乘 2.5 的 50/100/150/200 任务多机算例上验证规模
适应性：

```bash
PYTHONPATH=algorithm/src python -m algorithm.experiments.run_benchmarks
```

第 5.5 节使用完全模拟、非业务观测的 30/60/90 任务规模–种子单元，比较
三个已实现的贪心构造与完整 ALNS，并对四个 ALNS 组件组做关闭消融。所有
ALNS 变体复用同一初始路线、求解种子和固定 1000 次迭代预算：

```bash
PYTHONPATH=algorithm/src python -m \
  algorithm.experiments.run_virtual_ablation
```

第 6.1 节只比较以下四个场景，使用三个配对种子。Direct 基础模块为 240 s；
Relay 与 Position（需求驱动 Station 预部署）每启用一项分别增加 240 s，
因此四场景名义预算依次为 240/480/480/720 s：

- `pure_direct`；
- `direct_relay`；
- `direct_stations`；
- `direct_relay_stations`。

```bash
PYTHONPATH=algorithm/src python -m \
  algorithm.experiments.run_scenario_comparison
```

第 6.2 节只运行 `direct_relay_stations`，采用独立的 300 s 单次总预算，
固定一个种子，并分别扫描无人机数
`8/9/10/12/16`、截止期倍率 `0.8/0.9/1.0/1.1/1.2`、中继站数
`2/3/4/5/6`：

```bash
PYTHONPATH=algorithm/src python -m \
  algorithm.experiments.run_sensitivity_analysis
```

四个入口均支持 `--quick` 冒烟模式。正式输出会保存 manifest、输入/源码
指纹、CSV/JSON 汇总及逐运行完整路线；`--quick` 结果明确标记为非正式，
不得写入论文结论。`_archive/` 与旧 `results/` 中的历史材料仅供审计，不能与
上述最终协议的结果混用。
