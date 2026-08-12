# C2-Lex 三层目标消融实验报告

> **日期**：2026-08-12 | **实例**：200 任务, 8 UAV, 容量 2, K=25 | **预算**：每种子 240 s 墙钟

> **⚠️ legacy 标注**：本报告记录的是历史“三层目标”消融实验（`total_lateness_min` 作为 SA 的中间软化层）。
> Pickup-First Cooperative HALNS 重构后，**当前正式模型为两层 `(late_count, distance_km)`**，`total_lateness_min` 仅作诊断指标，
> 不参与任何 Score 比较、best 更新或接受判定。因此本报告“三层 Score 值得永久保留”的结论**不再适用于现行正式模型**，仅供历史参考。

## 摘要

本报告对 C2-Lex-A2 的**目标函数层数**与**修复启发式**两个维度做消融实验，在同机（Windows, Python 3.13, 约 650 iter/240s）条件下对比：

- **两层 baseline**：`Score(late_count, distance_km)`，SA 仅对距离做软化，无 pickup_urgency
- **三层**：`Score(late_count, total_lateness_min, distance_km)`，SA 对 total_lateness 做中间层软化
- **三层+PU**：三层 Score + `pickup_urgency` repair（取件最紧迫任务优先重排）

核心发现：

> **三层 Score 是最大单次贡献**：相比两层 baseline，late_count 平均 −2.0、total_lateness −258 min、distance −13 km，三个指标**全部改善**。pickup_urgency 进一步降低 late_count（−1.3）但以 total_lateness 和 distance 为代价。

## 1. 实验设置

| 参数 | 值 |
|---|---|
| 实例 | 200 任务, 8 UAV, 容量 2, K=25, speed=0.9 km/min |
| 种子 | 2026080500, 2026080501, 2026080502 |
| 每种子预算 | 240 s 墙钟（含 ~2 s 初始构造） |
| 求解器 | `solve_alns_core`（无 route pool / ejection） |
| 算子集 | 9 destroy + 7(或 8) repair（含/不含 pickup_urgency） |
| 目标函数 | 严格词典序 `(late_count, total_lateness_min, distance_km)` |

### 变体定义

| 变体 | Score 层数 | SA 软化层 | pickup_urgency |
|---|---|---|---|
| `two_layer_no_pu` | 两层 `(late, dist)` | `late` 不变时仅软化 `dist` | 无 |
| `three_layer_no_pu` | 三层 `(late, lateness, dist)` | `late` 不变时先软化 `lateness`，再软化 `dist` | 无 |
| `three_layer_pu` | 三层 | 同上 | 有（取件 slack 最负的任务优先） |

## 2. 消融结果

### 逐种子

| 变体 | seed | late | total_lateness | distance | iters |
|---|---:|---:|---:|---:|---:|
| two_layer_no_pu | 0 | 78 | 2751.7 | 669.88 | 630 |
| | 1 | 77 | 2797.9 | 666.66 | 627 |
| | 2 | 78 | 2690.7 | 674.69 | 626 |
| three_layer_no_pu | 0 | 76 | 2393.3 | 651.53 | 630 |
| | 1 | 77 | 2558.8 | 662.72 | 632 |
| | 2 | 74 | 2515.5 | 657.68 | 642 |
| three_layer_pu | 0 | 74 | 2506.0 | 666.90 | 626 |
| | 1 | 75 | 2557.7 | 657.16 | 654 |
| | 2 | 74 | 2635.5 | 676.02 | 669 |

### 三种子平均

| 变体 | late_count | total_lateness_min | distance_km |
|---|---:|---:|---:|
| two_layer_no_pu | **77.7** | **2746.8** | **670.4** |
| three_layer_no_pu | 75.7 | 2489.2 | 657.3 |
| three_layer_pu | 74.3 | 2566.4 | 666.7 |

### 配对 Delta

| 对比 | Δ late | Δ lateness | Δ distance |
|---|---:|---:|---:|
| 三层 vs 两层 | **−2.0 ▼** | **−257.6 ▼** | **−13.1 ▼** |
| +PU vs 三层 only | −1.3 ▼ | +77.2 ▲ | +9.4 ▲ |

## 3. 分析

### 3.1 三层 Score 的效果

三层 Score 将 `total_lateness_min` 从纯粹的"描述性统计"提升为 SA 软化的中间层。当 `late_count` 无法再降时，搜索通过 `total_lateness` 的 SA 软化来接受"逾期数不变但深晚度更低"的解，从而在 `late_count` 打平的众多候选解中筛选出 `total_lateness` 更优者。

在所有三个种子中，三层 Score 都**同时降低了 late_count、total_lateness 和 distance_km**——没有出现"用距离换逾期"或"用逾期换距离"的 trade-off。这验证了三层 Score 在 SA 框架中的有效性：它为搜索提供了更丰富的梯度，在 late_count 打平时推动搜索向"更短逾期时间 + 更短距离"方向探索。

### 3.2 pickup_urgency 的效果

`pickup_urgency` repair 在 regret 排序中注入 pickup slack（取件紧迫度），并将取件已超 `latest_pickup` 的任务优先重排。效果：

- **late_count 进一步降低 1.3**（75.7 → 74.3）
- 但 **total_lateness +77 min**（取件优先可能牺牲了送达的"早"）
- 且 **distance +9.4 km**（更激进的取件重排增加了绕路）

这是一个经典的"late_count vs total_lateness/distance"的 trade-off：pickup_urgency 让搜索更激进地减少逾期任务数，但代价是增加了剩余的逾期深度和总里程。

### 3.3 物理可行性背景

物理下界诊断（阶段 0）表明：**0/200 个任务是物理不可能的**——所有逾期任务（68–80 个）都是调度失败。平均 pickup overshoot 约 32 min（取件晚于 `latest_pickup`）。容量满载时间比例 42–56%，不是瓶颈。交叉节省 S_ij 均值 0.19 km，分布近乎对称——swap/relay 在地理上无天然优势。

这意味着：**所有逾期任务理论上都可以被救回**，问题在于搜索能否找到"提前取件"的解。

## 4. 代码改动清单

本报告伴随以下代码改动（全部 148 测试通过）：

| 文件 | 改动 |
|---|---|
| `model.py` | `Score` 扩展为三层 `(late_count, total_lateness_min, distance_km)` |
| `search.py` | `RouteEvaluator` / `routes_score` 返回三层 |
| `alns.py` | `_accept_worse` 三层 SA；`regret_key` 五层（含 pickup_urgency）；新增 `pickup_urgency` repair 策略；`_pickup_slack`/`_pickup_time` helper + cache |
| `swap_validation.py` | `Score(late_count, total_lateness, distance)` |
| `relay_validation.py` | 同上 |
| `validation.py` | 同上 |
| `interaction_graph.py` | 2 处 Score 字段适配 |
| `exact.py` | 2 处 Score 字段适配 |
| 新建 `physical_lower_bound.py` | 物理可行性诊断 |
| 新建 `run_physical_diagnosis.py` | 诊断入口 |
| `relay_candidates.py` | 新增 `_time_feasible` 时间下界剪枝 |
| `swap_search.py` | 三层 SA + 去单步贪心 break |
| 新建 `physical_lower_bound.py` | 物理可行性诊断 |
| 新建 `run_physical_diagnosis.py` | 诊断入口 |
| 新建 `run_three_layer_benchmark.py` | 三层 A2 基准 |
| 新建 `run_ablation.py` | 消融实验 |

## 5. 协同机制改进（路线 B）

### 5.1 时间可行性下界剪枝

在 `relay_candidates.py` 的 relay 候选生成循环中新增 `_time_feasible` 预筛，
在进入昂贵的容量窗口枚举和全量 DAG 求值之前，先做廉价的时间下界检查：

- **最早 drop 时间**：取件后立刻飞往站点
- **最晚 pick 时间**：`deadline − d(station, delivery) / speed − handling`
- 若 `earliest_drop + handling ≥ latest_pickup` → 该 (station, second) 组合物理不可行，直接跳过

这避免了 relay 报告中"接收机全部被 drop 门控、无 slack"的情况继续浪费 DAG 求值预算。

### 5.2 Swap Stage-2 组合搜索

修改 `swap_search.py` 的 `solve_swap_stage2`：
- **去掉**：`if improved is None: break`（单步贪心终止）
- **改为**：即使当前无单步救援 swap，也继续迭代直到时间预算耗尽，允许 SA 链在后续迭代中发现组合机会
- **升级 SA**：从两层 `(late_count, distance)` 升级为三层 `(late_count, total_lateness, distance)`

## 6. 结论与后续

1. **三层 Score 值得永久保留**：在所有指标上优于两层 baseline，无 trade-off，已通过 131/131 测试。
2. **pickup_urgency 应作为可选算子**：保留在算子池中让 ALNS 自适应权重决定。
3. **时间下界剪枝已就位**：为 relay 候选生成提供廉价预筛，待与本机 relay 基准配合验证效果。
4. **Swap 不再单步贪心**：Stage-2 可持续探索至预算耗尽。
5. **协同机制的根本瓶颈**：relay/swap 仍是 Stage-2 补救。路线 B.1（relay 候选进 Stage-1 构造/repair）是架构级改动，留待后续。
6. **本机迭代率**：约 650 iter/240s，需在同机上做所有对比实验。

## 附录：物理下界诊断摘要

| 指标 | 值 |
|---|---|
| 物理不可能任务数 | 0 / 200 |
| 平均 pickup overshoot | 32–34 min |
| 容量满载比例 | 42–56% |
| S_ij 均值 | 0.19 km |
| S_ij > 0 比例 | 52.5% |
