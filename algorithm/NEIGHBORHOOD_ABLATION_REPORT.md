# 问题特定邻域与 240 秒消融报告

## 摘要

本轮改动保留了原有路线表示、独立校验器、确定性随机种子，以及“逾期任务数 → 总逾期分钟 → 总里程”的最终词典序目标。用 assignment destroy 替换 generic route-clear，再加入 deadline-risk guidance 后，搜索吞吐仍接近旧基线；VND、cluster repair 和 route pool 虽然提供了更强的邻域，但在 240 秒预算内的计算成本没有转化为更好的最终解。

在本次同批次、同初始解的直接比较中，当前 Core 配置 A2 对历史算子集 A0 为 2 胜、0 平、1 负，三个种子的平均逾期数从 70.33 降至 70.00，总逾期从 2338.92 降至 2321.45 分钟，搜索吞吐只下降 1.25%。与前一次 240 秒 Legacy Core 的跨批历史记录相比，按三个共同 seed 事后排序，A2 有 2 次更好、1 次更差且均值略好；这不是受控配对。相同功能配置的 A0 在两个 240 秒批次之间少完成 7.83% 的每秒迭代，说明跨批差异包含不可忽略的系统负载与停止边界影响，组件结论仍应以本次 A0→A2 为准。

三次报告中已归档的 240 秒内 best-known 路线仍是前一次 Core 对比实验的 Legacy Core：`(67, 2161.865122 min, 631.416943 km)`。本次全体变体的最好路线为 A0 的 `(67, 2172.800486 min, 631.018272 km)`，因第二目标多 10.935 分钟，即使距离短 0.399 km，词典序仍更差；当前 Core A2 的批内最好为 `(69, 2260.830859 min, 634.746383 km)`。因此本次支持的是默认邻域组合和墙钟效率取舍，而不是刷新历史最好解。

因此当前默认策略为：

- ALNS Core 默认用 assignment destroy 取代 route-clear，并启用 deadline risk；不启用 VND、cluster repair、ejection 或 route pool。三个种子的结果不足以证明这两个单步改动各自稳定改进，默认值是兼顾问题特定引导与吞吐的工程选择。
- HALNS 按任务要求在上述配置上保留 ejection search；route pool 默认关闭。
- VND 和 cluster repair 保留为 `ALNSConfig` 的显式可选项，便于在不同规模、非满载舰队或更长预算上继续试验。

这只是一个 200 任务实例、三个固定种子的描述性结论，不作统计显著性或跨实例改进声明。

## 实现内容

### Assignment destroy

`assignment_destroy` 按任务的逾期贡献、deadline risk、路线绕行贡献与路线利用率选择完整取送任务对。对于 200 个任务恰好填满 8 架、每架上限 25 个任务的实例，算子在可行时至少同时从两条路线移除任务，给 repair 留出真正跨无人机重新分配的空位。

### Deadline-risk guidance

搜索引导使用单调风险指标 `delivery_time / deadline`，并对零截止期显式处理。它只影响 destroy 排名、repair shortlist/排序与局部邻域候选顺序；候选接受、当前解和最终最优解仍严格使用原三层 `Score`。

### 轻量 VND

VND 包含三类容量二、取送约束感知邻域：跨无人机 task-pair relocate、跨无人机 task-pair swap，以及短 `pickup pickup delivery delivery` block 的可行重排。所有接受动作都要求严格词典序改善；比较器还拒绝浮点聚合产生的 `1e-9` 以内“伪改善”。满载舰队中单 pair relocate 数学上没有目标空位，因此实现会快速跳过 N1，而 N2 仍可改变任务分配。

### Cluster regret repair

`cluster_regret_repair` 按取件点距离、截止期接近度和原无人机归属形成二任务 bundle，并在同一路线上联合插入。首任务位置使用小型 beam，避免只保留单任务最优位置而丢掉组合最优结构；无法形成 bundle 时回退到 regret-2。

### 墙钟与路线池

deadline 已贯穿普通 repair、cluster repair、VND 和 ejection。若某轮在墙钟边界只形成部分解，该轮会安全放弃；若 VND/ejection 已形成完整改进解，则先吸收改进再结束。route pool 没有扩张，并改为默认关闭。

## 实验设计

输入为 200 任务官方 CSV（SHA-256 `386d3fbaa9009aeed947493782d657ed0b30b25d369c8187bfc9619fc7e31d25`）。所有变体共享同一 deterministic regret-2 初始解（路线 SHA-256 `0aa05645d9ca61227c51c1d69993de896ecca045136165dec06a4bd1636288b6`），使用旧实验的三个墙钟种子 `2026080500` 至 `2026080502`。每次总预算为 240 秒：初始构造计入预算，并预留 2 秒安全余量；各变体在同一种子内轮换运行顺序。

消融按功能阶梯展开。为保持 destroy pool 大小不变、避免继续堆叠 generic operators，A1 是用 `assignment_destroy` **替换** `route_clear`，不是在 A0 上保留两者的纯加法；A2–A6 再从这个替换后的算子集逐项累加。因此 A0→A1 衡量的是替换的净效果，不能单独归因于新增算子。

| 代码 | 配置 |
|---|---|
| A0 | 改动前的 ALNS Core 算子集；新 feature flags 均关闭，含 route-clear |
| A1 | A0 - route-clear + assignment destroy |
| A2 | A1 + deadline risk |
| A3 | A2 + VND |
| A4 | A3 + cluster repair |
| A5 | A4 + ejection |
| A6 | A5 + route pool |

下表为三个种子的均值。三层目标必须按顺序解读，不能用列间加权平均替代词典序比较。

| 变体 | 逾期数 | 总逾期分钟 | 里程 km | 迭代数 | 搜索迭代/s |
|---|---:|---:|---:|---:|---:|
| A0 | 70.333 | 2338.919 | 647.610 | 1680.3 | 7.183 |
| A1 | 70.000 | 2337.179 | 649.241 | 1691.0 | 7.229 |
| A2 | 70.000 | 2321.448 | 643.948 | 1659.3 | 7.094 |
| A3 | 71.667 | 2415.850 | 648.016 | 1259.7 | 5.385 |
| A4 | 74.000 | 2481.961 | 649.880 | 1083.7 | 4.633 |
| A5 | 71.000 | 2381.170 | 666.290 | 1013.0 | 4.331 |
| A6 | 75.000 | 2675.735 | 683.648 | 723.3 | 3.092 |

21 次正式运行中，单次词典序最优是 A0、seed `2026080502` 的 `(67, 2172.800 分钟, 631.018 km)`；这里只把它记录为 wall-clock best，不据此作组件改进结论。完整路线见 [`equal_wall_clock_240s__a0__seed_2026080502.json`](results/neighborhood_ablation/run_solutions/equal_wall_clock_240s__a0__seed_2026080502.json)，各变体的 best seed 见 [`ablation_summary.csv`](results/neighborhood_ablation/ablation_summary.csv)。

完整的相邻配对结果为：A0 对 A1 为 2:1，A1 对 A2 为 2:1，A2 对 A3 为 3:0，A3 对 A4 为 3:0，A5 对 A4 为 2:1，A5 对 A6 为 3:0；另外 A0 对 A4 为 2:1。组合配置 A2 相对 A0 为 2:1，但 assignment replacement 和 risk 两个直接边际步骤各自只有 1 胜 2 负，所以不能把组合差异归因到任一组件，也不能宣称稳定支配。A2 被选作 Core 默认，依据是问题特定引导这一设计目标、三项目标均值与几乎不变的吞吐，而非三种子下的确定性优势。

VND、cluster 和 route pool 在该 240 秒设置下明显压低吞吐，因此不默认开启。ejection 在正式 A4→A5 比较中赢 2 个种子、输 1 个种子，并按任务约束继续保留；本实验没有把轻量 A2+ejection 作为独立正式变体，因此不对当前 HALNS 默认组合提出额外的实证支配声明。

## 与前两次实验的关系

本文所称“第一次”为 [`RESULTS_REPORT.md`](RESULTS_REPORT.md) 的原始 HALNS 基准，“第二次”为 [`ALNS_CORE_COMPARISON_REPORT.md`](ALNS_CORE_COMPARISON_REPORT.md) 的 Core/HALNS 对比，本次则是当前报告的邻域消融。

三次实验使用相同的勘误数据、问题参数、Regret-2 构造逻辑和严格词典序目标，因此已校验路线的最终得分可以直接排序；但算法效果、吞吐和组件归因只能从各批次内部的受控比较得出。为避免同名 `HALNS` 被误认为同一配置，本文采用以下统一名称：

| 统一名称 | 实际配置 | 历史报告中的名称 |
|---|---|---|
| Legacy Core | route-clear；无 assignment、risk、VND、cluster、ejection 或 route-pool 重组；第一次实现仍收集未使用的路线列，第二次与本次 A0 为真正零收集 | 第一次的 `C2-Lex-ALNS`、第二次的 ALNS Core、本次 A0 |
| Legacy HALNS | Legacy Core + ejection + route pool | 前两次报告的 HALNS |
| Current Core | assignment 替换 route-clear + risk；无 VND、cluster、ejection、pool | 本次 A2 |
| Current HALNS | Current Core + ejection；VND、cluster、pool 关闭 | 当前代码默认；尚无精确匹配的正式 240 秒变体 |

### 三个证据块回答不同问题

| 证据块 | 停止规则与种子 | 实际配置 | 可以支持的结论 |
|---|---|---|---|
| 第一次 fleet 基准 | 400 轮，5 seeds；240 秒只是未触发的上限，实际约 55–70 秒 | Legacy Core / Legacy HALNS | 固定轮数下强化模块的净效果；不是等墙钟 240 秒比较 |
| 第二次 400 轮块 | 400 轮，5 seeds；HALNS 复用第一次记录，Core 重跑并精确复现历史轨迹 | Legacy Core / Legacy HALNS | 回归确认和同一证据的重述，不是第二次独立重复 |
| 第二次 240 秒对比 | 240 秒，3 个配对 seeds，批内交替顺序 | Legacy Core / Legacy HALNS | 旧配置在固定墙钟下的质量与吞吐差异 |
| 本次 240 秒消融 | 240 秒，3 个配对 seeds，七个变体循环顺序 | A0–A6；A2 为 Current Core | 本批次内相邻阶梯的净效果与默认开关依据 |
| Current HALNS | 尚未正式运行 | A2 + ejection，无 VND、cluster、pool | 暂无受控 240 秒结论；A5/A6 不能作为代理 |

第一次报告的 400 轮结果和第二次报告的 400 轮结果存在数据复用，因此不能写成“两次实验都独立证明 HALNS 单轮更强”。真正新增的第二块证据是第二次 240 秒 Core/HALNS 同批对比。

### 第一次到本次：更好产物伴随更长实际预算

为了使用一致的随机种子，下表把第一次 5-seed 正式结果裁为与本次相同的三个种子。第一次的 400 轮上限先于 240 秒上限触发；本次则由墙钟预算停止。

| 批次与方法 | 停止规则 | 平均逾期数 | 平均总逾期 / min | 平均距离 / km | 平均总时间 |
|---|---|---:|---:|---:|---:|
| 第一次 Legacy Core | 400 轮 | 84.667 | 3223.917 | 734.360 | 55.42 s |
| 第一次 Legacy HALNS | 400 轮 | 81.000 | 2922.711 | 713.399 | 65.88 s |
| 本次 Current Core A2 | 240 秒 | 70.000 | 2321.448 | 643.948 | 238.00 s |

在三个共同种子中，第一次 Legacy HALNS 对 Legacy Core 为 2 胜、1 负；完整五种子正式结论为 3 胜、2 负。Current Core A2 相对第一次三种子 Legacy HALNS 的均值少 11 项逾期、601.263 分钟总逾期和 69.450 km，但平均用时是其 3.61 倍。这个变化主要说明从 400 轮扩展到完整墙钟预算后，归档产物继续改善；它同时改变算法配置和有效搜索时间，不能作为新旧算法的公平速度比较。

### 同样 240 秒下，Current Core 均值略好但没有稳定支配

下表统一使用三个共同种子 `2026080500`–`2026080502`。第二次和本次属于不同运行批次，表中的跨批差异只作系统级历史描述；本次 A0 与 A2 的同批比较才是新组件的主要证据。

| 批次与方法 | 平均逾期数 | 平均总逾期 / min | 平均距离 / km | 平均迭代数 | 平均迭代/搜索秒 |
|---|---:|---:|---:|---:|---:|
| 第二次 Legacy Core | 70.333 | 2332.834 | 646.561 | 1819.3 | 7.793 |
| 第二次 Legacy HALNS | 70.667 | 2442.069 | 663.445 | 1036.7 | 4.441 |
| 本次 A0 / Legacy Core 重复基线 | 70.333 | 2338.919 | 647.610 | 1680.3 | 7.183 |
| 本次 A2 / Current Core | 70.000 | 2321.448 | 643.948 | 1659.3 | 7.094 |

本次 A2 相对同批 A0 为 2 胜、0 平、1 负；平均少 0.333 项逾期、17.472 分钟总逾期和 3.662 km，平均每秒迭代仅低 1.25%。这是支持 A2 默认值的最直接证据，但 A0→A1 和 A1→A2 两个单步边际都分别只有 1 胜、2 负，不能把 A2 的组合差异拆分归因给 assignment 或 risk 任一组件。

跨批看，按三个共同 seed 事后排序，A2 相对第二次 Legacy Core 有 2 次更好、1 次更差，平均少 0.333 项逾期、11.386 分钟总逾期和 2.612 km；相对第二次 Legacy HALNS 也是 2 次更好、1 次更差，三项均值分别低 0.667、120.621 分钟和 19.496 km，并多完成 60.06% 的迭代。这些都不是受控配对；比较同时改变了实现快照、算子组合和运行批次，只能说明当前轻量架构在这组历史记录中有竞争力，不能解释为 Current HALNS 或某个单一新邻域稳定优于旧版本。

批次效应可以由 Legacy Core 重复基线直接看出：按共同 seed 事后排序，第二次 Legacy Core 有 2 次更好、1 次与本次 A0 精确相同；两者平均逾期数相同，但本次 A0 平均少完成 139 轮，每秒迭代低 7.83%。这也是为什么运行时间和吞吐百分比只在同一批次内作强结论。

### 当前 240 秒 best-known 仍来自第二次实验

下表只排序已归档的合规路线，不把它解释为方法优越性。不同候选范围、种子数量和运行批次会影响“最好一次”的选择机会。

| 报告与候选范围 | 最好合规得分 `(逾期数, 总逾期 min, 距离 km)` | 用时 | 解释 |
|---|---:|---:|---|
| [第一次 Legacy HALNS，400 轮 × 5 seeds](results/best_fleet_solution_compliant.json) | `(76, 2860.032371, 694.694821)` | 66.36 s | 400 轮提前停止，未用满 240 秒 |
| [第二次 Legacy Core/HALNS，240 秒 × 3 seeds](results/alns_core_comparison/best_alns_core_equal_wall_clock_240s.json) | **`(67, 2161.865122, 631.416943)`** | 238.02 s | 当前归档的 240 秒内 best-known；来自 Legacy Core |
| [本次 A0–A6，240 秒 × 3 seeds](results/neighborhood_ablation/run_solutions/equal_wall_clock_240s__a0__seed_2026080502.json) | `(67, 2172.800486, 631.018272)` | 238.00 s | 本批 overall best 来自 A0，不是 Current Core |
| [本次 Current Core A2，240 秒 × 3 seeds](results/neighborhood_ablation/run_solutions/equal_wall_clock_240s__a2__seed_2026080501.json) | `(69, 2260.830859, 634.746383)` | 238.00 s | 当前默认 Core 的批内最好；未刷新历史 best-known |

相对第一次 400 轮最好路线，本次 overall best 少 9 项逾期，总逾期低 24.03%，距离低 9.17%；但用时从 66.36 秒增加到约 238 秒，所以只能说明更充分使用预算后得到更好产物，不是公平的算法速度对比。第一次另有一条 1500 轮超时路线 `(67, 2272.192060, 641.635947)`，用时 338.81 秒；本次 A0 与它逾期数相同，总逾期低 99.392 分钟、距离低 10.618 km，且在 240 秒内完成。该路线只作超时潜力参考，不纳入合规 leaderboard。

## 结论、限制与下一步

三次实验连起来支持一个一致但有限的判断：第一次固定 400 轮时，Legacy HALNS 的五种子平均质量优于 Legacy Core，但计算更贵；第二次把预算固定为 240 秒后，Legacy Core 依靠更高吞吐在三个种子中赢 2 个；本次则表明 assignment replacement 与 risk 可以保留轻量 Core 的吞吐，而启用 VND、cluster 或继续叠加 route pool 的累计配置会在当前满载实例上消耗过多墙钟。当前默认采用 A2，不代表它已稳定支配 Legacy Core；当前默认 HALNS 的精确组合也尚未被正式消融覆盖。

证据仍只有一个 200 任务实例。第一次固定轮数使用 5 个种子，两个 240 秒批次各只有 3 个种子；随机种子描述的是同一实例上的搜索波动，不是独立问题样本，因此不报告 p 值，也不把均值或 95% Student-t 区间解释为跨实例保证。相同机器、Python 和种子编号不能消除瞬时系统负载；旧 manifest 也没有保存 solver 源码哈希或初始路线哈希，跨批重现性弱于本次实验。

下一步应优先完成：

1. 在同一最终源码快照上正式比较 A2 与 `A2 + ejection`，使用相同初始路线、至少 5 个预先固定种子和完整 240 秒预算，补上 Current HALNS 的证据空白。
2. 运行脚本已支持但本次未执行的 5 seeds × 400 轮 A0–A4 消融，区分每轮邻域质量和墙钟吞吐。
3. 在多个独立实例上重复配对实验；若继续调优 VND/cluster，应测试降低调用频率、只对高风险任务触发，或在 repair 尚有跨机空槽时运行 relocate。

## 复现与原始数据

完整 A0–A6 墙钟实验命令：

```bash
PYTHONPATH=algorithm/src python3 \
  algorithm/experiments/run_neighborhood_ablation.py \
  --output-dir algorithm/results/neighborhood_ablation \
  --equal-seed-count 0 --wall-seed-count 3 \
  --wall-time-limit 240 --wall-safety-margin 2 \
  --include-hybrid-tuning
```

脚本默认还支持旧实验的 5 个种子 × 400 轮固定迭代消融；本报告的正式数值只来自上面的等墙钟运行。`--resume` 会校验输入、初始路线、参数以及实际导入的 solver 源码哈希，拒绝混合不同实现的断点结果。正式运行结束后才依据结果把 VND/cluster 的默认开关由开改为关；随后另有一处无语义格式整理。将这三处逆变换后，当前 solver 可精确恢复 manifest 中的 SHA-256 `d5ae253d2537d67401f1ef04711af0bd80d546a9c0fd59e59b422aebcc60c533`。各变体均显式设置全部 feature flags，因此这些运行后改动不改变实验算法路径。实验脚本本身后来只修正了阶梯命名说明，并改为指纹化实际导入模块；manifest 仍保留实际运行快照的脚本哈希。

原始汇总、逐运行记录、配对比较和 21 份完整路线位于 [`results/neighborhood_ablation/`](results/neighborhood_ablation/)。所有 21 个正式运行均通过独立 validation 且未超预算。
