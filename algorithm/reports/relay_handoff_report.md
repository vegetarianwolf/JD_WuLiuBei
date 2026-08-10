# 缓冲式静态接力调度：实现、语义边界与正式实验报告

## 结论先行

1. **官方 200 单实例不应提交跨无人机接力方案。**按“实际运输过任务任一航段即计入该机任务数”的保守 `strict-touch` 语义，题面参数满足 `N=M×K=200`，跨机接力必然额外消耗至少一个任务槽位，因此可行解中的接力任务数只能为 `R=0`。这不是搜索能力不足，而是可行域本身为空。
2. **现阶段官方口径的推荐结果仍是无接力历史冠军 `A2 + late_risk_destroy`。**在 200 单、3 seeds、240 秒正式实验中，它平均按时 `131.333/200`、平均准时率 `65.667%`、平均总里程 `645.854 km`；三个指标范围分别为 `[130,134]`、`[65.000%,67.000%]`、`[642.621,650.177] km`。
3. **`primary-owner` 接力是题意扩展，而且本轮没有带来收益。**自适应单中心和四中心方案分别平均按时 `129.000/200`、`128.667/200`，低于同批无接力基线的 `130.333/200`；按题面两层顺序逐 seed 比较，两者的胜/平/负均为 `0/0/3`。虽然平均里程分别减少 `9.335 km` 和 `10.850 km`，但里程不能补偿按时任务数下降。
4. **特意构造的 synthetic campus 也没有显示系统收益。**在 `N=50`、4 类场景、16 个 `hub×handoff` 组合、10 seeds 的 680-run 开发敏感性中，接力相对配对基线合计 W/T/L 为 `34/28/578`；该实验是 1 秒短预算、primary-owner 扩展，只用于机制诊断，不能混入官方 240 秒排名。
5. **评分口径已纠正。**正式主比较只使用“按时任务数/准时率”和“总开放路线里程”；运行时间只用于检查集群求解是否不超过 `240 s`。`total_lateness_min` 仅保留为诊断量或两个官方指标完全相同后的稳定 tie-break，不参与主排名。

因此，本轮接力实现的价值主要是完成了可审计的事件模型、独立验证器和题意边界证明；它尚未形成可替代当前正式直接配送算法的成绩。

## 评分与验收口径

题目要求先尽可能提高在时限内完成的任务数，再优化飞行总里程。固定 `N=200` 且所有任务必须完成时，最大化按时任务数等价于最小化逾期任务数。正式比较顺序写作：

```text
(-on_time_count, distance_km)
```

其中：

- `on_time_rate = on_time_count / N`，只是第一指标的百分比展示；
- `distance_km` 为所有无人机从共同起点出发的开放路线里程之和，不计最后一次送达后的返航；
- `runtime_seconds ≤ 240` 是集群算法的工程约束，不是第三个优化目标；
- `total_lateness_min`、最大逾期、包裹等待、无人机等待、接力次数和迭代数均为诊断指标；
- 可行性是进入比较的门槛：每单完整覆盖、先取后送、容量不超过 2、机队及单机任务数不超限。

本报告不构造加权综合分，也不允许以更短里程补偿更低准时率。更完整的题意核对见[任务计数语义与验收边界](relay_task_count_semantics.md)，原始依据见[题目文件](../../命题1-低空经济场景下的物流无人机调度算法.docx)。

## 为什么 strict-touch 下必有 `R=0`

令 `U_i` 为实际运输过任务 `i` 任一航段的不同无人机集合，令无人机 `m` 触达的不同任务数为：

\[
c_m=\left|\{i:m\in U_i\}\right|.
\]

每项任务必须完成，故 `|U_i|≥1`；若任务发生真正的跨无人机接力，则 `|U_i|≥2`。设接力任务数为 `R`，则：

\[
\sum_{m=1}^{M}c_m=\sum_{i=1}^{N}|U_i|\ge N+R.
\]

题面又要求每架无人机最多承接 `K` 个任务，因此：

\[
\sum_{m=1}^{M}c_m\le MK.
\]

官方实例为 `N=200`、`M=8`、`K=25`，即 `MK=N`。于是 `N+R≤N`，只能有 `R=0`。同一架无人机在交接点自放自取不会增加触达数，但也不是跨机协作，通常只增加航程或等待。

代码中的 strict-touch 饱和审计与该证明一致：在 1 个 seed、12 秒诊断预算下，`relay-disabled` 和 `static-multihub` 都被标记为 `strict_touch_saturated`，均输出 0 个接力任务；每个事件层结果都与其自身的 legacy 直接路线逐项等价。两次墙钟搜索的停止迭代数可因运行时抖动不同，因此不要求两个方法彼此得到同一条直接路线。该审计只验证结构与回归保护，不参与 240 秒性能排名。

| strict-touch 结构审计 | 运行 | 按时数 | 准时率 | 总里程 / km | 运行时间 / s | 接力数 | 说明 |
|---|---:|---:|---:|---:|---:|---:|---|
| relay-disabled | 1 | 97 | 48.500% | 796.846 | 11.057 | 0 | 直接事件层 |
| static-multihub | 1 | 98 | 49.000% | 785.264 | 11.062 | 0 | 饱和条件下禁用接力搜索 |

审计原始数据见[strict-touch 逐次运行](../results/relay_handoff/strict_audit/relay_runs.csv)和[strict-touch 汇总](../results/relay_handoff/strict_audit/relay_summary.csv)。

## Primary-owner 扩展模型

为评估接力的潜在算法价值，本轮另行实现 `primary-owner`：每项任务只计入执行原始 `PICKUP` 的唯一主承接无人机；接收无人机可以运输后半段，但该任务不计入其 `K`。该定义使接力在饱和实例中成为可能，但改变了题面“每架无人机最多完成 K 个任务”的保守含义，故必须标记为**题意扩展**，不能冒充官方合规方案。

每个接力任务使用事件链：

```text
PICKUP(i) -> HANDOFF_DROP(i,h) -> HANDOFF_PICK(i,h) -> DELIVERY(i)
```

正式扩展实验中，所有方案的每机主承接数均不超过 25；实际发生接力的方案中，单机最大物理触达数达到 26。这一差异正是必须与官方 strict-touch 结果分表的原因。

## 正式结果

### 官方可提交口径：无接力历史正式结果

当前 200 单、3 seeds、240 秒历史正式冠军如下。该结果没有接力语义争议。

| 方法 | 有效运行 | 按时数均值 [min,max] | 准时率均值 [min,max] | 总里程 km 均值 [min,max] |
|---|---:|---:|---:|---:|
| **A2 + late_risk_destroy** | 3/3 | **131.333 [130,134]** | **65.667% [65.000%,67.000%]** | **645.854 [642.621,650.177]** |

其中表现最好的单次运行达到 `134/200` 按时、`67.000%` 准时率、`650.177 km`。全部 22 个同口径历史正式配置见[官方 strict/no-relay 对比表](../results/relay_handoff/comparison/strict_no_relay_comparison.md)及其[机器可读 CSV](../results/relay_handoff/comparison/strict_no_relay_comparison.csv)。

### 题意扩展：primary-owner 3-seed 正式实验

下表使用官方 200 单数据、相同 3 个 seed 和 `240 s` 总预算；`[min,max]` 均为三次运行范围。扩展方法与基线放在本节仅为同批实验比较，不改变其非官方语义属性。

| 方法 | 有效运行 | 按时数均值 [min,max] | 准时率均值 [min,max] | 总里程 km 均值 [min,max] |
|---|---:|---:|---:|---:|
| baseline | 3/3 | **130.333 [129,132]** | **65.167% [64.500%,66.000%]** | 650.583 [639.441,658.158] |
| static-1hub | 3/3 | 129.000 [127,132] | 64.500% [63.500%,66.000%] | 641.248 [630.466,652.429] |
| static-multihub | 3/3 | 128.667 [127,132] | 64.333% [63.500%,66.000%] | **639.733 [630.369,647.206]** |

所有 9 次运行均通过独立事件验证且未超过 240 秒；平均运行时间分别为 `238.002 s`、`238.007 s`、`238.051 s`。接力次数是诊断量：三种方法均值分别为 `0.000`、`1.000`、`3.000`，不参与上表排名。方案级 strict-touch 独立重算中，3 个 baseline 和 static-1hub 的 1 个无接力回退方案可按官方语义通过；其余 5 个真正采用接力的方案均不通过 strict-touch，这与 `R=0` 定理一致。逐次数据见[primary-owner 逐次运行](../results/relay_handoff/official_primary/relay_runs.csv)，汇总见[primary-owner 汇总](../results/relay_handoff/official_primary/relay_summary.csv)，完整事件路线和哈希见[实验清单 JSON](../results/relay_handoff/official_primary/relay_results.json)。

### 同 seed 配对 W/T/L

W/T/L 均从接力方法视角计算：先比较按时数；仅按时数相同才比较总里程。两个指标都相同才记平。

| 接力方法 vs baseline | Δ按时数均值 | Δ准时率 / 百分点 | Δ总里程均值 / km | W/T/L | 逐 seed 判定 |
|---|---:|---:|---:|---:|---|
| static-1hub | -1.333 | -0.667 | -9.335 | **0/0/3** | `0500`: 按时相同、里程多 1.407 km；`0501`: 少 3 单；`0502`: 少 1 单 |
| static-multihub | -1.667 | -0.833 | -10.850 | **0/0/3** | `0500`: 按时相同、里程多 2.183 km；`0501`: 少 3 单；`0502`: 少 2 单 |

两种接力方法在 seed `2026080501` 和 `2026080502` 都取得了更短里程，但同时减少按时任务，所以仍按题面顺序判负；seed `2026080500` 的按时数相同，接力路线反而更长。均值里程较低同样不能改变这一结论。包含历史正式结果与扩展结果的独立分表见[primary-owner 扩展对比](../results/relay_handoff/comparison/primary_owner_extension_comparison.md)及其[CSV](../results/relay_handoff/comparison/primary_owner_extension_comparison.csv)。

## Synthetic campus：hub 数 × 交接耗时敏感性

为避免仅凭官方均匀数据否定接力，本轮按 Deep Research 建议另建四类坐标度量场景：U 为均匀负对照，G 为校外取件—校内送达的 gate cut，MZ 为多宿舍/教学区聚类，MIX 混合同区与跨区订单。所有场景均使用未经逐距离截断的欧氏坐标，`N=50`、2 架无人机、容量 2、primary-owner 语义；候选 hub 采用嵌套的质心起点 + farthest-first 顺序，确保 `H=1⊂2⊂4⊂8`。

矩阵包含 `H∈{1,2,4,8}`、交接作业时间 `τ∈{0,0.5,1,2} min`、10 个共同 seed，共 40 次去重 baseline 与 640 次 relay。每次总预算 `1.0 s`，有效搜索窗口 `0.8 s`；relay 把基础直接搜索占比设为 60%，所以负差同时包含给接力层预留时间的机会成本。下表中“relay 网格均值”是同一场景 16 个参数格 × 10 seeds 的描述性均值，不是单个正式配置。

| 场景 | baseline 按时数 | relay 网格按时数 | Δ按时数 | baseline 里程 / km | relay 网格里程 / km | Δ里程 / km | 配对 W/T/L |
|---|---:|---:|---:|---:|---:|---:|---:|
| U | 9.100 | 8.400 | -0.700 | 550.131 | 565.265 | +15.134 | 24/20/116 |
| G | 3.900 | 3.800 | -0.100 | 1134.097 | 1169.263 | +35.165 | 0/8/152 |
| MZ | 6.200 | 5.869 | -0.331 | 665.342 | 701.588 | +36.246 | 10/0/150 |
| MIX | 15.000 | 13.963 | -1.038 | 417.401 | 437.361 | +19.961 | 0/0/160 |

64 个参数格没有一个在均值按时数上超过配对 baseline；合计 W/T/L 为 `34/28/578`。680/680 次运行都合法且未超过 1 秒，640/640 个 relay 配置均实际进入接力层，接力迭代数最小/均值/最大为 `2/11.130/29`；其中 442 次最终方案含至少一个接力。hub 数与交接耗时没有呈现单调收益，因此不能从本轮数据提出“增加 hub”或“降低交接耗时即可改善成绩”的结论。

- [逐运行 CSV](../results/relay_handoff/synthetic_development_sensitivity/sensitivity_runs.csv)
- [参数汇总 CSV](../results/relay_handoff/synthetic_development_sensitivity/sensitivity_summary.csv)
- [二维配对表 CSV](../results/relay_handoff/synthetic_development_sensitivity/sensitivity_heatmap.csv)
- [handoff time × hub count 热力图](../results/relay_handoff/synthetic_development_sensitivity/handoff_time_x_hub_count.svg)
- [完整 manifest、质量门槛与 checkpoint 签名](../results/relay_handoff/synthetic_development_sensitivity/sensitivity_results.json)

该矩阵严格标记为 `development_sensitivity` 和 `official_240s_comparable=false`。跨 16 个参数格重复使用同一组 baseline，格间结果并非相互独立，因此汇总 W/T/L 只作描述，不做 sign test 或显著性外推。

## 全部历史实验的拉表覆盖

历史收集器去重后保留 **58 个配置汇总、122 次独立运行**。其中包括 200 单/240 秒正式实验，也包括构造法、固定 400 轮、开发 smoke、超预算扩展、单机 25 单和 `n=5/8/10/12` 的 exact 对照；这些停止条件和任务规模不同，不能混入同一正式排名。

- [58 个历史配置 + 3 个新正式配置的分组总表](../results/relay_handoff/comparison/all_experiments_comparison.md)
- [上述 61 个配置的机器可读 CSV](../results/relay_handoff/comparison/all_experiments_comparison.csv)
- [全部 58 个历史配置的机器可读总表](on_time_objective/summary.csv)
- [全部 122 次历史运行](on_time_objective/all_runs.csv)
- [按实验阶段分组展示的既有报告](ON_TIME_SACRIFICE_REPORT.md)
- [22 个 200 单/240 秒历史正式配置的筛选表](../results/relay_handoff/comparison/strict_no_relay_comparison.csv)
- [加入本轮 primary-owner 扩展后的独立对比表](../results/relay_handoff/comparison/primary_owner_extension_comparison.csv)

正式名次只依据前述两项题面指标；`runtime` 用于验收预算，异构历史阶段只保留用于追溯，不从中挑选“冠军”。

## 方法与实现

### 事件模型与缓冲交接

[事件模型](../src/uav_dispatch/relay.py)与原有 signed-int 直接路线并存，避免暗中改变旧验证逻辑。静态 hub 在搜索前由任务取送走廊中点确定：1-hub 使用一个聚类中心，multi-hub 使用 4 个确定性中心。每单最多一次接力，交接作业时间固定为 `0.5 min`，允许包裹在 hub 异步暂存。

### 搜索流程

[Relay-ALNS](../src/uav_dispatch/relay_alns.py)先运行启用 `late_risk_destroy` 的 A2 直接配送搜索，再在完整可行解上自适应选择接力邻域：

- `split`：将直接取送拆为发送机放件、接收机取件与最终送达；
- `change_receiver`：替换下游承运无人机；
- `change_hub`：替换静态交接点；
- `merge`：把接力任务恢复为直接任务。

四个算子按历史奖励更新权重并用 roulette 选择；搜索把 current 与 best-so-far 分离，使用可关闭的模拟退火规则接受部分非改进候选以跳出局部最优，最终始终返回 best。候选生成器同时做路线容量过滤和距离/时序多样化截断。官方方案比较只使用两个题面指标；`total_lateness_min` 只在这两个指标完全相同后用于确定性稳定 tie-break，不改变正式名次。

### 独立验证

[接力验证器](../src/uav_dispatch/relay_validation.py)不信任求解器缓存，而是从事件路线重新计算：

- 每项任务的事件组成、完整覆盖、同 hub 交接和 custody 顺序；
- 每条路线的载荷变化与容量 2 上限；
- 跨无人机优先图的无环性以及 `pick ≥ drop + service` 的时间传播；
- 包裹等待、无人机等待和 hub 峰值库存；
- 最终 `DELIVERY` 时刻对应的按时任务数；
- 所有开放路线航段总里程；
- strict-touch 的物理触达数或 primary-owner 的原始取件所有权计数。

无接力计划还会降低回 legacy signed 路线，并对有效性、送达时刻、逾期数、逾期总量、分路线里程和完成时刻做 `1e-9` 容差内的一致性审计。测试覆盖见[事件验证测试](../tests/test_relay_validation.py)、[接力搜索测试](../tests/test_relay_alns.py)和[实验产物测试](../tests/test_relay_experiment.py)。

## 实验设置与可复核性

| 项目 | 取值 |
|---|---|
| 数据 | 官方 CSV 前 200 单；`input_sha256=386d3fbaa9009aeed947493782d657ed0b30b25d369c8187bfc9619fc7e31d25` |
| 问题哈希 | `6ab18af665022973fa45b0512d492dbac72f6eca6bbc0379e1ba602bcc94881e` |
| 机队 | 8 架；每机最多 25 单；载荷容量 2 |
| 速度与路线 | `0.9 km/min`；共同起点 `(0,0)`；开放路线不返航 |
| seeds | `2026080500`、`2026080501`、`2026080502` |
| 总预算 | `240 s`；预留 `2 s` 写盘安全余量，求解器上限 `238 s` |
| direct baseline | A2 + assignment/deadline risk/late-risk destroy；两层直接目标 |
| 接力预算分配 | 约 80% 用于 direct base search，余量用于接力层；总预算仍含 base search |
| hub | 1 个或 4 个预生成静态中心 |
| 交接 | 每单最多 1 次；服务时间 `0.5 min`；允许异步暂存 |
| 执行源码快照 | `004977b52e50fd35829ee60b2f5041b1ce429a70c3e465fcfe1a439318542e62` |

实验运行器会记录输入哈希、问题哈希、seed、预算、语义、事件路线、独立验证结果和每个解文件的 SHA-256，见[正式实验运行器](../experiments/run_relay_handoff.py)。

## 局限与稳健性说明

1. primary-owner 未得到题目文字授权；其成绩只能说明扩展模型下的探索结果。
2. 每个正式配置只有 3 个 seed，适合报告完整范围和配对 W/T/L，不足以作强统计显著性结论。
3. strict-touch 仅做了单 seed、12 秒结构审计；`R=0` 的官方结论来自计数证明，审计只是实现验证。
4. 静态 hub 来自几何聚类，没有验证真实禁飞区、起降条件、站点容量和安全性；模型也未限制最大包裹等待或 hub 容量。
5. `0.5 min` 交接服务时间、共同起点、欧氏距离和异步暂存均为显式实现假设，不是题面补充事实。
6. 当前自适应接力层仍只允许每单一次交接，且算子集合限于 split/merge/change-receiver/change-hub；本轮失败不能证明更丰富的 sender 交换、批量 destroy/repair 或其他接力模型无价值。
7. relay 方法把约 20% 总预算留给接力层，而 baseline 将几乎全部预算用于直接搜索。比较符合相同总时限，但结果同时反映“分配搜索时间”的机会成本。
8. 墙钟时间会受机器负载影响，当前产物未记录 CPU/内存环境；跨历史快照比较应视为描述性汇总。
9. synthetic 只覆盖 `N=50`、欧氏静态 hub 与 1 秒开发预算，尚未扫描最大包裹等待、hub capacity、任务槽位有余量的 strict-touch 场景或更长搜索预算。

## 下一步建议

1. 正式提交继续使用无接力 `A2 + late_risk_destroy`，接力开关保持默认关闭。
2. 向主办方书面确认 `K` 是“实际运输触达任务数”还是“唯一主承接任务数”；只有后者得到确认，primary-owner 才能进入官方候选。
3. 在 `MK>N` 的 synthetic 场景加入任务槽位余量，检验 strict-touch 接力的真实收益，并设置无空间分区的负对照。
4. 若继续研究扩展模型，优先加入有限 hub 容量、最大暂存时间、能耗/续航、禁飞区和交接失败鲁棒性，再用更长预算复核 hub 数量与服务时间消融。
5. 官方 primary-owner 扩展实验扩大到至少 10 个共同 seed；继续使用同输入、同总预算、逐 seed 两层 W/T/L，不引入加权分。
6. 改进接力层的初始化和邻域组合，避免固定 80/20 时间切分掩盖潜在收益；同时保留 direct-only 回退保证。

## 复现命令

以下命令从仓库根目录执行。正式 primary-owner 实验约需 `3×3×240 s` 的顺序墙钟时间：

```bash
PYTHONPATH=algorithm/src .venv/bin/python algorithm/experiments/run_relay_handoff.py \
  --dataset official \
  --methods baseline static-1hub static-multihub \
  --seeds 2026080500 2026080501 2026080502 \
  --tasks 200 --drones 8 --max-tasks 25 \
  --time-limit 240 --wall-safety-margin 2 \
  --task-count-semantics primary-owner \
  --hub-count 4 --handoff-service-min 0.5 \
  --output-dir algorithm/results/relay_handoff/official_primary
```

重做 strict-touch 结构审计：

```bash
PYTHONPATH=algorithm/src .venv/bin/python algorithm/experiments/run_relay_handoff.py \
  --dataset official \
  --methods relay-disabled static-multihub \
  --seeds 2026080500 \
  --tasks 200 --drones 8 --max-tasks 25 \
  --time-limit 12 --wall-safety-margin 1 \
  --task-count-semantics strict-touch \
  --hub-count 4 --handoff-service-min 0.5 \
  --output-dir algorithm/results/relay_handoff/strict_audit
```

重做 680-run synthetic 开发敏感性：

```bash
PYTHONPATH=algorithm/src .venv/bin/python algorithm/experiments/run_relay_sensitivity.py \
  --scenarios U G MZ MIX --tasks 50 \
  --seeds 2026081000 2026081001 2026081002 2026081003 2026081004 \
          2026081005 2026081006 2026081007 2026081008 2026081009 \
  --hub-counts 1 2 4 8 --handoff-times 0 0.5 1 2 \
  --time-limit 1.0 --wall-safety-margin 0.2 \
  --candidate-limit 4 --task-sample-size 8 --base-search-fraction 0.60 \
  --require-relay-iterations --resume \
  --rerun-reason 'validated 1.0s development sensitivity' \
  --output-dir algorithm/results/relay_handoff/synthetic_development_sensitivity
```

重新生成历史正式对比表：

```bash
PYTHONPATH=algorithm/src .venv/bin/python algorithm/experiments/build_relay_comparison.py \
  --history-summary algorithm/reports/on_time_objective/summary.csv \
  --relay-summary algorithm/results/relay_handoff/official_primary/relay_summary.csv \
  --output-dir algorithm/results/relay_handoff/comparison
```
