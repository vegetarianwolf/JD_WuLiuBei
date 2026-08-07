# 问题特定邻域与 240 秒消融报告

## 结论

本轮改动保留了原有路线表示、独立校验器、确定性随机种子，以及“逾期任务数 → 总逾期分钟 → 总里程”的最终词典序目标。用 assignment destroy 替换 generic route-clear，再加入 deadline-risk guidance 后，搜索吞吐仍接近旧基线；VND、cluster repair 和 route pool 虽然提供了更强的邻域，但在 240 秒预算内的计算成本没有转化为更好的最终解。

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
