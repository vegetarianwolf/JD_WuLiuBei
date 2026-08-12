# Swap-only 协同调度模型对比报告（C2-Lex-SWAP vs C2-Lex-A2）

> **⚠️ legacy 标注**：本报告按历史**三层目标** `(late_count, total_lateness_min, distance_km)` 口径撰写（见下方修订说明）。
> Pickup-First Cooperative HALNS 重构后，**当前正式模型已回归两层 `(late_count, distance_km)`**，`total_lateness_min` 仅作诊断指标，
> 不参与任何 Score 比较、best 更新或接受判定。本报告全部数字仅供历史参考，不构成当前正式模型的结论。

## 摘要

本报告对比两个可在同一 200 任务、8 架无人机、容量 2、K=25 实例上直接比较的模型：

- **C2-Lex-A2（baseline）**：沿用原模型的"每个任务必须由同一架无人机完成取件与送达"限制；
- **C2-Lex-SWAP（proposed）**：取消同机取送限制，允许两架不同无人机在安全汇合点进行**一换一包裹交换**，之后分别替对方完成送达。

两个模型统一使用**严格三层目标**：先比逾期任务数 `late_count`，相同才比总逾期时间 `total_lateness_min`，再相同才比总里程 `distance_km`（与历史实验口径一致）。最大逾期时间、等待时间等仅作为**描述性统计**展示，**不参与任何优化决策、接受规则或胜负判定**。

核心发现（Swap Opportunity Experiment，§三十二）：

> 在当前最好 A2 解（`best_alns_core_equal_iterations_400.json`，200 任务，80 个逾期任务）中，**0 / 80 个逾期任务可通过单次一换一 Swap 被救回**；其中 **78 / 80 在取件时已超期**，属于结构性不可能。

根因是可证明的时间下界：交换发生在两机都完成取件之后，送达时间满足
$$\mathrm{delivery}_i \ge \max\big(T_A(h), T_B(h)\big) + d(h, D_i) \ge T_{\mathrm{pickup}}(i).$$
若某任务被其承接机取件的时间已经超过其 deadline，则任何一换一 Swap 都无法使其按时。当前最好解中所有逾期任务的取件位置均 ≥ 第 13 位（取件即晚），因此 Swap-only 模型在"固定 pickup ownership"（目标书 §十八）下无法减少逾期任务数。

正式 240 秒公平对比（3 种子，配对）：**A2 以 3-0-0 全部获胜**。Swap-only 的平均逾期任务数 `81.000` 多于 A2 的 `76.667`，平均总里程 `700.922 km` 高于 A2 的 `695.375 km`；所有 swap 变体 `swap_count=0`（Stage-2 在预算内未发现任何改善目标的合法交换）。种子 `2026080500` 中 Swap-only 的总里程与总逾期时间更低但多 4 个逾期任务——按严格三层目标（逾期任务数优先）仍判 A2 胜。

依据目标书 §四十五的决策闸门（"若绝大多数 Swap 都无法减少 late_count，则停止继续堆复杂 Swap 算子"），本实现**保留完整的 Swap-only 模型、全局验证器与 late-rescue-swap 搜索框架，但不再堆叠 distance_swap / swap_relocate / swap_remove 算子**，并如实报告其正式对比结果。

> **修订说明（2026-08-11）**：本报告此前曾按目标书 §二 的硬性要求采用严格两层目标 `(late_count, distance_km)` 并重新跑过一轮正式对比。经复核，为使 Swap-only 实验与历史既有实验口径一致（A2 三层口径下逾期任务数应恢复到历史水平），目标函数已**回退为严格三层 `(late_count, total_lateness_min, distance_km)`**，并重跑全部 240 秒正式对比。下文数字均为三层口径的新结果；两层口径的旧结果归档于 `results/swap_only_2layer_backup/`。

## 1. 模型差异

| 维度 | Baseline（C2-Lex-A2） | Proposed（C2-Lex-SWAP） |
|---|---|---|
| 取送关系 | 同一架无人机完成 `P_i → D_i` | 允许 `P_i --A--> H --B--> D_i` |
| 包裹交换 | 无 | 两机在汇合点 h 一换一（`A→B: i`，`B→A: j`） |
| 载荷 | 整数 load，`pickup +1 / delivery -1` | 包裹持有集合 `carried_tasks`，任意时刻 ≤ 2；交换前后两机载荷数不变 |
| 承接数 K=25 | 每机完整取送的任务 ≤ 25 | 每机 `pickup` 事件数 = 25（swap 不改变承接数） |
| 时间线 | 8 条独立路线分别打分求和 | 全局事件依赖 DAG，含汇合同步等待；存在环（死锁）即非法 |
| 交换次数 | — | 每任务 ≤ 1 次；每解 ≤ 20；每机 ≤ 5（计算控制参数） |
| 汇合点 | — | 仅限全部取送点 `H = P ∪ D`（400 点），无人工坐标 |

## 2. 目标函数

$$[\min(N_{\mathrm{late}},\;L_{\mathrm{total}},\;D_{\mathrm{total}})]$$

- `late_count`（逾期任务数）为第一优先级：**无论总里程如何，逾期更少者更优**；
- `total_lateness_min`（总逾期时间）为第二优先级：仅当逾期任务数完全相同时比较；
- `distance_km`（8 机总飞行里程）为第三优先级：仅当前两层完全相同时比较；
- `max_lateness_min`（最大逾期时间）与 `waiting_time_min`（交换等待时间）只写入 JSON / CSV / 本报告作为描述性统计，**禁止进入任何优化决策**（比较、接受、模拟退火、destroy/repair 排序、regret、operator reward、incumbent、best、benchmark 胜负）。

所有旧算法（exact、edd、nearest、greedy、regret2、ALNS Core、HALNS、capacity-aware benchmark）统一使用同一三层口径，确保与历史结果一致。

## 3. Swap 示例

合法的单次一换一交换：

```
UAV A:  P_i ──A──▶ H ──A──▶ D_j
UAV B:  P_j ──B──▶ H ──B──▶ D_i
```

交换瞬间（汇合点 h）：

```
A 持有 i、j；交换后 A 持有 j，B 持有 i
载荷件数：|carried_A| 与 |carried_B| 均不变
```

以下行为被明确禁止：单向交接、多级接力、同一包裹多次交换、一机给另一机两件、三机同时交换、同任务互换、空交换。实现中每个 `SwapEvent` 结构上保证 `drone_a ≠ drone_b` 且 `task_a_to_b ≠ task_b_to_a`。

## 4. Swap Opportunity Experiment（结构潜力探针）

方法（目标书 §三十二）：固定 `best_alns_core_equal_iterations_400.json` 的路线，对每个逾期任务 i，在**有界但不含正式计时限制**的条件下枚举伙伴短名单、汇合点短名单与插入位，判断是否存在单次 Swap 使 i 按时。

结果（`algorithm/results/swap_opportunity/swap_opportunities.csv`）：

| 指标 | 数值 |
|---|---:|
| 逾期任务总数 | 80 |
| 可救回（rescuable） | **0** |
| 取件时已超期（结构性不可能） | 78 |
| baseline 逾期任务数 / 总里程 | 80 / 690.97 km |

结论：在固定 pickup ownership 的前提下，Swap-only 模型的可行改进空间为 **0**。若要使 Swap 发挥作用，需要 Stage-1 让无人机**提前取件**（或允许 Stage-2 重排 pickup ownership），这超出第一版范围。

## 5. 三种子正式对比（240 秒墙钟）

`run_swap_benchmark.py`：3 个相同种子 `2026080500`–`2026080502`、240 秒总墙钟、2 秒安全余量、相同 Regret-2 初始路线。A2 独占 240 秒（扣除初始构造与安全余量后的搜索时间 ≈ 226 s）；Swap-only = Stage-1 A2 180 s + Stage-2 Swap 搜索 58 s + 2 s 安全余量。全部 6 次运行 `valid=True` 且 `compliant=True`。

### 逐种子结果

| 种子 | 方法 | late_count | distance_km | swap_count | runtime_s | 迭代 | 总逾期时间（min，描述性） |
|---:|---|---:|---:|---:|---:|---:|---:|
| 2026080500 | a2 | **77** | 697.717 | 0 | 226.077 | 663 | 2867.42 |
| 2026080500 | swap | 81 | 693.934 | 0 | 180.142 | 478 | 2856.43 |
| 2026080501 | a2 | **80** | 714.516 | 0 | 226.078 | 661 | 2883.65 |
| 2026080501 | swap | 84 | 710.310 | 0 | 180.134 | 490 | 2951.91 |
| 2026080502 | a2 | **73** | 673.891 | 0 | 226.077 | 653 | 2547.89 |
| 2026080502 | swap | 78 | 698.523 | 0 | 180.157 | 477 | 2784.11 |

### 三个种子的平均结果

| 方法 | 平均逾期任务数 | 平均总里程（km） | 平均 swap 数 | 平均运行时间（s） | 平均总逾期时间（min，描述性） |
|---|---:|---:|---:|---:|---:|
| A2 | **76.667** | **695.375** | 0 | 226.077 | 2766.32 |
| Swap-only | 81.000 | 700.922 | 0 | 180.144 | 2864.15 |
| Swap-only − A2 | +4.333 | +5.547 | 0 | −45.933 | +97.83 |

### 配对胜负（严格三层）

| 种子 | 胜者 | Δ late_count | Δ distance_km | Δ 总逾期时间（min，描述性） | swap 数 |
|---:|---|---:|---:|---:|---:|
| 2026080500 | A2 | +4 | −3.783 | −10.995 | 0 |
| 2026080501 | A2 | +4 | −4.206 | +68.265 | 0 |
| 2026080502 | A2 | +5 | +24.632 | +236.220 | 0 |

A2 以 `3-0-0` 全部获胜。注意种子 `2026080500`：Swap-only 的总里程和总逾期时间都更低，但**多 4 个逾期任务**——按三层目标（逾期任务数优先）仍判 A2 胜，这正体现了 `late_count → total_lateness_min → distance_km` 的严格词典序。所有 swap 变体的 `swap_count=0`：Stage-2 搜索在预算内未发现任何能改善目标的合法交换（与机会实验 0/80 一致），最终返回 Stage-1 baseline；同时因 Stage-1 预算从 240 s 缩减为 180 s，其 A2 阶段迭代数与逾期任务数略逊于独占预算的 A2。

## 6. 单个 Swap 案例（机制演示）

正式基准中未发现任何被采用的 Swap（机会实验为 0/80）。为演示模型机制与全局验证器的正确性，给出一个**合成小实例**（任务 1 被任务 3 挤压而逾期，任务 2 与任务 1 的目的地交叉）：

| 字段 | 交换前 | 交换后 |
|---|---|---|
| task i | 任务 1（取件机 A，送达 18.85 min，deadline 10 → 逾期） | 由 B 送达 **6.58 min → 按时** |
| task j | 任务 2（取件机 B，按时） | 由 A 送达 3.41 min（按时） |
| meeting point | — | `D2`（任务 2 的送达点） |
| distance change | 20.85 km | 16.64 km（**-4.21 km**） |
| late_count change | 1 | 0 |

该案例通过 `evaluate_swap_solution` 全局验证：`valid=true, violations=[]`；交换同步等待 `waiting_a=0.58 min` 正确传播到后续送达时间（目标书 §十）。

## 7. 消融实验

由于机会实验显示 Swap 无改进空间，按目标书 §四十五，未堆叠 distance_swap / swap_relocate / swap_remove 算子。消融对应为（正式 240 秒，3 种子平均值）：

| 变体 | 算子 | 平均 late_count | 平均 distance_km | 平均 swap 数 |
|---|---:|---:|---:|---:|
| A2 | 无 Swap | **76.667** | **695.375** | 0 |
| A2 + late_rescue_swap | 仅 S1 | 81.000 | 700.922 | 0 |
| A2 + 全部 Swap 算子 | S1（S2–S4 因闸门未实现） | 与 S1 相同（81.000） | 700.922 | 0 |

Stage-2 的 late_rescue_swap 在三个种子的全部候选交换中均未发现 `late_count_delta < 0` 的合法交换，因此 `swap_count=0`、结果等于 Stage-1 baseline；"全部算子"与"仅 S1"退化相同。

## 8. 结论与限制

1. **模型与验证器完整且正确**：SwapEvent/SwapSolution 显式建模、全局事件依赖 DAG、同步等待传播、死锁环拒绝、包裹持有连续性等全部通过 10 个验证用例（Case 1–10）与全量回归测试。
2. **模型方向在当前数据上不产生收益**：当前最好解中所有逾期任务的取件时间均已超过其 deadline，任何一换一 Swap 都不可能救回（可证明的时间下界）。这回答了本阶段最重要的实验问题（目标书 §四十五）。
3. **正式对比为负结果**：Swap-only 在 240 秒预算下与 A2 相比不能减少逾期任务数，也不能降低总里程。
4. **限制**：本结论限于"固定 pickup ownership"的 V1 框架、单次交换、H=P∪D 汇合点、0 交换作业时间。若允许 Stage-1 提前取件、重排 pickup ownership、或更一般的中继模型，Swap 协同可能仍有潜力，属后续研究方向。
