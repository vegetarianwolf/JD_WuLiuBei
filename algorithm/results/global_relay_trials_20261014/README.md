# 全局调整与下游接力：完整规模初筛

直接打开 [index.html](index.html) 查看离线交互报告。结果未达到预设扩大门槛，
`enable_tail_exchange`、`enable_home_reassignment`、`enable_relay_downstream_refine`
继续默认关闭。现有 ALNS 仍为默认算法。

## 范围与口径

本轮重新阅读原题、历史方案和失败记录，对照开始时 main
`39baeb4e33431c57881a70a10737de84fb1414c0`。用户授权自主决策、要求初筛覆盖大部分场景。
因此不裁小集群实例：完整 200 项任务、8 架无人机、每机最多 25 项、容量 2、
速度 0.9 km/min，覆盖直送、直送＋中继、直送＋预部署、直送＋预部署＋中继四种策略。

题目要求全部任务完成，尽量按时且航程短；本项目沿用“逾期任务数、总逾期分钟、
总航程”的三级词典序，总逾期分钟属于项目次级目标。预部署和存储式异步中继属于
扩展模型。中继继续按最终交付责任计入每机 K=25，前半程转运不占 K；这一解释
不能直接等同于原题直送的任务分配。网络、起点槽位、构造初解与配对种子均保持一致。

## 本轮候选

| 版本 | 调整 | 边界 |
|---|---|---|
| baseline | 冻结的既有 main | 全部本轮研究开关关闭 |
| global | 跨机尾段交换＋全局起点槽位匹配 | 只在多机 DIRECT 或中继预热生效 |
| downstream | global＋中继后续影响排序 | 保留完整全局调度评价与历史最优 |

尾段交换只在零载荷断点之间交换等任务数尾段，保持每机任务上限；每 20 轮
最多检查 4 对路线。起点匹配每 40 轮用子集动态规划，按完整路线得分把路线
重新匹配到既有槽位，不增减站点或更改预部署槽位总量。下游接力精化允许关注
自身已按时、但拖延接收机后续任务的接力任务；它不放松容量、交接或先取后送约束。

方法依据与原文访问层级见[文献笔记](../../../papers/notes/global_relay_20261014/reading_notes.md)。
其中 CVRP 的 SWAP* 插入结论仅作为设计启发，未直接套用于取送配对和截止期；
同步机器人接力论文也与本项目允许存储的异步交接区分。

## 冻结协议与结果

`manifest.json` 在首个实测运行前冻结，绑定输入、源码 ZIP、执行脚本、种子、预算与门槛。
所有求解顺序执行；配对保持输入、初解、种子和阶段总墙钟预算相同，第二个种子反转
版本顺序。总预算包含网络生成、预部署、求解、核验和写解，不含 Python 启动导入；
20 秒阶段预留 1 秒安全余量。

- 单机兼容：官方前 25 项、固定 100 轮、种子 `2026102400`，三版共 3 次运行，决策指纹一致。
- 集群初筛：四场景 × 三种子 `2026102411`—`2026102413`，baseline/global 共 24 次；
  两个中继场景追加 downstream 共 6 次，每次 20 秒总预算。
- 扩大门槛：global 至少 7/12 胜且累计逾期任务不增，或主场景“预部署＋中继”至少
  2/3 胜且该场景累计逾期任务不增。downstream 须对 global 至少 4/6 胜且累计逾期不增。
- 若通过，才运行四场景三个新种子的 60 秒验证；默认晋级须 8/12 胜、每场景至少
  2/3 胜且每一配对的逾期数均不增。总实验求解预算上限 2200 秒。

| 场景 | global 对 baseline 的胜／平／负 |
|---|---|
| 直送 | 1 / 0 / 2 |
| 直送＋中继 | 2 / 0 / 1 |
| 直送＋预部署 | 1 / 0 / 2 |
| 直送＋预部署＋中继 | 1 / 0 / 2 |
| 合计 | **5 / 0 / 7** |

global 的 12 组配对累计逾期任务变化为 −10，但不满足稳定性门槛；不能用均值
改善抵消退化。downstream 对 global 为 **2 胜 1 平 3 负**，累计逾期变化 +1。
因此 60 秒阶段未运行，没有开始长测。33 次完整运行累计实际求解
**570.768339 秒（9.51 分钟）**。消融复用同批 global 对照，不能把这 6 组配对
算作额外独立的 global 样本。

所有原始解均保留，无重跑择优。`validation.json` 独立复核官方输入、结构、
评分、交接 DAG 的逐事件时间及等待、完整配对、预算和晋级决策；33/33 合法且预算内。
`production.json` 绑定审计哈希并记录不启用默认的决定。均值与样本标准差采用 n=3、
ddof=1，不表示置信区间，也不作统计显著性结论。

## 历史与环境边界

报告单列历史四策略结果，其总预算分别为 240/480/480/720 秒，不能与本轮统一
20 秒实验交叉计算改善率。用户截图仅作为方向参考；历史图数值以仓库冻结结果为准。

首个初筛种子的早期运行期间曾误启动完整 pytest，并因 Matplotlib macOS GUI
后端初始化退出。未记录精确重叠单元，全部实测原样保留；其后种子未与全套测试并行。
见 `environment_notes.json`。本轮墙钟受限的质量结果包含主机负载噪声，不据此宣称
纯实现加速。完整回归改在求解完成后使用 `MPLBACKEND=Agg`。

实测使用 `sources/candidate.zip`。测后审查补上 `_relay_refine_pass` 中插入枚举
和全局增量评价后的两处截止时间检查，超时候选不再被接收为启动 incumbent。
该修复经回归测试，未重新做性能实验，不将冻结成绩归因于修复。精确差异及哈希见
`final_source_delta.json` 与 `post_measurement_deadline_fix.patch`；最终代码三个研究
开关仍为 False。历史测试的 fixture 改用其自己已校验的冻结 ZIP，未放宽历史审计。

## 查看与重建

HTML 内嵌数据与图表，离线可打开。支持主比较／下游消融切换、场景选择，并列展示
逾期数、按时率、航程、求解时间、总逾期、逐种子差异和真实跨机交接／等待。

从仓库根目录运行以下命令只重建图表，不重新求解：

```bash
PYTHONPATH=algorithm/src .venv/bin/python -m algorithm.experiments.render_global_relay_report \
  --output-dir algorithm/results/global_relay_trials_20261014
MPLBACKEND=Agg PYTHONPATH=algorithm/src .venv/bin/python -m pytest -q -p no:cacheprovider algorithm/tests
```

如需独立复核而不改变本批绑定的审计时间戳，先把本结果目录复制到临时目录，再运行：

```bash
PYTHONPATH=algorithm/src .venv/bin/python -m algorithm.experiments.audit_global_relay_trials \
  --output-dir /tmp/global_relay_audit_copy
```

审计会重写指定目录的 `validation.json`。原批次 `production.json` 绑定原审计文件，
不能只重写原目录的审计再将旧绑定误称为有效。需重新求解时使用一个全新的空目录，
执行 `run_global_relay_trials prepare` 后再执行 `run`，两者均传入该目录的
`--output-dir`；这会冻结当前代码（含测后修复），属于新批次，不能覆盖本批结果。
精确检查已测实现时应读取保留的 baseline/candidate ZIP，而不是以当前工作树替代。

`runs.json/csv`、`run_solutions/` 是原始记录，`summary.json/csv` 为描述性汇总；
`sources/` 保存实测算法和执行脚本，`decisions.json` 保存门槛决策。
