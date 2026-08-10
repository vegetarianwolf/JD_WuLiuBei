# 非 main 分支无人机调度正式实验对比报告

## 技术结论

本报告已覆盖仓库中 main 之外的全部实验分支，并且只使用题目本身的三个评价指标：

1. `late_count`：逾期订单数，越小越好；
2. `total_lateness`：总逾期时间，越小越好；
3. `distance`：总里程，越小越好。

三者严格按上述顺序比较，不构造加权总分，也不使用胜负计数、按时率、吞吐量、
迭代数、百分比改善或其他派生指标作为结论依据。

在可直接比较的“同一 200 任务、相同 3 个 seeds、每次 240 秒”结果中，当前分支的
**A2 + `late_risk_destroy`** 三项均值为：

```text
(late_count, total_lateness, distance)
= (68.667, 2310.280, 645.854)
```

此前所有非 main 分支中，最低的三-seed平均逾期数来自
`feature/capacity-aware-halns` 的 A2 重跑：

```text
(69.333, 2317.712, 640.149)
```

当前 late-risk 配置的第一指标和第二指标都更小；虽然第三指标更大，但只有当前两项
完全相同时才比较距离。因此，按题目原始评价顺序，当前建议仍是只启用
`late_risk_destroy`，关闭 rejection pool、soft deadline、VND、cluster repair、
ejection 和 route pool。

## 官方指标与比较口径

所有主表结果均来自同一份勘误数据：

- 任务数：200；
- 无人机数：8；
- 单机任务上限：25；
- 载荷容量：2；
- 输入 SHA256：
  `386d3fbaa9009aeed947493782d657ed0b30b25d369c8187bfc9619fc7e31d25`；
- 输入 Git blob：`7458bd57abfa2dced5aa966ee540d1420a3e9adf`。

主比较口径固定为 seeds `2026080500`、`2026080501`、`2026080502`，每个方法每个
seed 的总预算为 240 秒。表中的三个数分别是对应官方指标的三-seed算术平均；均值
只用于汇总多次实验，单个提交解仍按其自身的官方三元组评价。

固定 400 轮、单次构造、超过 240 秒、不同任务规模和 smoke 结果均与正式主表分开，
不能据此宣称正式方法更优。

报告使用精确表格而不是综合得分图，避免把三层词典序误画成可以相加或折算的单一
尺度。

## 已覆盖的全部非 main 分支

| 分支 | 实验数据版本 | 该分支引入的实验 | 在报告中的位置 |
|---|---|---|---|
| `codex/uav-alns-dispatch` | `160dc98` | 初始 400 轮 ALNS/HALNS、构造法、超时扩展 | 历史参考表 |
| `codex/alns-core-comparison` | `dfa71d7` | ALNS Core 与 HALNS 的 3 seeds × 240 秒对比 | 正式主表 |
| `codex/stronger-uav-neighborhoods` | `7964a2f` | A0–A6 问题特定邻域消融 | 正式主表 |
| `feature/capacity-aware-halns` | 数据提交 `bcd9506` | A2 与 Capacity-aware HALNS | 正式主表 |
| `feature/adaptive-deadline-rejection-pool` | `0905f0a` | baseline 与 experiment1–4 | 正式主表 |

后创建的分支会继承较早分支的结果文件。完全相同的历史 CSV/JSON 只统计一次，避免
把继承文件误当成新的独立实验。main 分支按要求不纳入本报告。

## 240 秒正式结果：当前 late-risk 的平均逾期数最低

以下所有行均为同一任务、相同三个 seeds、每次 240 秒的官方三指标均值。表格按分支
和实验批次排列，不把三项指标合成为一个新分数。

| 分支 / 实验 | 方法 | late_count | total_lateness | distance |
|---|---|---:|---:|---:|
| `codex/alns-core-comparison` | C2-Lex-ALNS-Core | 70.333 | 2332.834 | 646.561 |
| `codex/alns-core-comparison` | C2-Lex-HALNS | 70.667 | 2442.069 | 663.445 |
| `codex/stronger-uav-neighborhoods` | A0 baseline_core | 70.333 | 2338.919 | 647.610 |
| `codex/stronger-uav-neighborhoods` | A1 + assignment destroy | 70.000 | 2337.179 | 649.241 |
| `codex/stronger-uav-neighborhoods` | A2 + deadline risk | 70.000 | 2321.448 | 643.948 |
| `codex/stronger-uav-neighborhoods` | A3 + VND | 71.667 | 2415.850 | 648.016 |
| `codex/stronger-uav-neighborhoods` | A4 + cluster repair | 74.000 | 2481.961 | 649.880 |
| `codex/stronger-uav-neighborhoods` | A5 + ejection | 71.000 | 2381.170 | 666.290 |
| `codex/stronger-uav-neighborhoods` | A6 + route pool | 75.000 | 2675.735 | 683.648 |
| `feature/capacity-aware-halns` | A2 重跑 | 69.333 | 2317.712 | 640.149 |
| `feature/capacity-aware-halns` | Capacity-aware HALNS | 71.000 | 2328.349 | 646.874 |
| 当前分支 | A2 baseline | 69.667 | 2328.494 | 645.747 |
| 当前分支 | **A2 + late-risk destroy** | **68.667** | **2310.280** | **645.854** |
| 当前分支 | A2 + rejection pool | 74.000 | 2503.373 | 669.156 |
| 当前分支 | A2 + soft deadline | 74.333 | 2412.587 | 653.681 |
| 当前分支 | A2 + 三模块组合 | 76.333 | 2652.729 | 679.202 |

按题目顺序先看 `late_count`，当前 late-risk 的 `68.667` 是正式主表中的最小值。
它的 `total_lateness=2310.280` 也小于此前平均逾期数最低的 A2 重跑
`2317.712`。由于前两项已经确定顺序，不能用 A2 重跑更短的距离反转结论。

## 每个 240 秒实验批次保存的最佳完整路线

下表不是新的汇总分数，而是各批次中按题目三项指标直接选出的一个完整路线结果。

| 实验批次 | 方法 | seed | late_count | total_lateness | distance |
|---|---|---:|---:|---:|---:|
| ALNS Core 对比 | C2-Lex-ALNS-Core | 2026080502 | 67 | 2161.865 | 631.417 |
| A0–A6 邻域消融 | A0 baseline_core | 2026080502 | 67 | 2172.800 | 631.018 |
| Capacity-aware HALNS 对比 | A2 | 2026080502 | 68 | 2290.577 | 640.383 |
| 当前 adaptive 对比 | **A2 + late-risk destroy** | 2026080501 | **66** | 2264.497 | 650.177 |

当前分支保存的 late-risk 路线有 66 个逾期订单；此前正式批次保存的最低值为 67。
按第一评价指标，当前路线更优。其完整路线文件是
`results/adaptive_deadline_rejection/run_solutions/equal_wall_clock_240s__experiment1__seed_2026080501.json`。

## 当前 adaptive 实验逐 seed 官方结果

| seed | 方法 | late_count | total_lateness | distance |
|---:|---|---:|---:|---:|
| 2026080500 | A2 baseline | 70 | 2424.643 | 651.613 |
| 2026080500 | A2 + late-risk destroy | 70 | 2356.015 | 644.763 |
| 2026080500 | A2 + rejection pool | 75 | 2525.897 | 663.654 |
| 2026080500 | A2 + soft deadline | 74 | 2366.775 | 648.984 |
| 2026080500 | A2 + 三模块组合 | 77 | 2618.780 | 681.233 |
| 2026080501 | A2 baseline | 69 | 2260.411 | 633.590 |
| 2026080501 | **A2 + late-risk destroy** | **66** | 2264.497 | 650.177 |
| 2026080501 | A2 + rejection pool | 73 | 2554.133 | 679.246 |
| 2026080501 | A2 + soft deadline | 74 | 2415.628 | 657.845 |
| 2026080501 | A2 + 三模块组合 | 75 | 2619.534 | 666.966 |
| 2026080502 | A2 baseline | 70 | 2300.427 | 652.038 |
| 2026080502 | A2 + late-risk destroy | 70 | 2310.328 | 642.621 |
| 2026080502 | A2 + rejection pool | 74 | 2430.089 | 664.569 |
| 2026080502 | A2 + soft deadline | 75 | 2455.357 | 654.215 |
| 2026080502 | A2 + 三模块组合 | 77 | 2719.872 | 689.406 |

late-risk 在三个 seed 上的逾期数为 `70、66、70`；当前 baseline 为
`70、69、70`。另外三个新增配置在每个 seed 上的逾期数都高于相同 seed 的
baseline，因此不建议进入最终配置。

## 早期分支结果必须按停止条件单独解释

### 固定 400 轮的 5-seed历史结果

`codex/uav-alns-dispatch` 的搜索虽然设置了 240 秒上限，但实际在完成 400 轮后停止，
没有使用完整的 240 秒。它们不能与正式主表混排。

| 方法 | late_count | total_lateness | distance |
|---|---:|---:|---:|
| C2-Lex-ALNS | 83.400 | 3056.266 | 716.337 |
| C2-Lex-HALNS | 80.600 | 2970.604 | 714.338 |

### 同一 200 任务实例的单次构造结果

| 方法 | late_count | total_lateness | distance |
|---|---:|---:|---:|
| EDD-adjacent | 164 | 10563.005 | 1293.988 |
| Nearest-adjacent | 129 | 6892.056 | 943.493 |
| Greedy-full-position | 111 | 4608.694 | 852.054 |
| Regret-2 | 108 | 4464.184 | 775.773 |

这些是单次构造结果，不是 240 秒随机搜索结果，只用于展示早期基线。

### 超过 240 秒的扩展结果

早期 `C2-Lex-HALNS-extended` 在 1500 轮后得到：

```text
(67, 2272.192, 641.636)
```

该运行超过 240 秒，因此不能进入正式主表，也不能替代合规提交结果。

`exact_n5/n8/n10/n12` 和 `single_n25` 使用不同任务规模，不属于本报告的 200 任务
比较范围。Phase smoke 结果也不作为正式评价证据。

## Rejection pool 只作为搜索状态，不改变最终任务覆盖

rejection pool 不是题目评价指标，也不参与跨分支排名。正式输出仍必须包含全部
200 个订单的完整 pickup-delivery pair。当前 15 个正式路线都通过官方完整性校验，
没有永久丢弃订单。

rejection pool 和三模块组合的三个官方评价指标都劣于当前 baseline，因此即使它们
能够保持完整任务覆盖，也不建议启用。

## 对原优化问题的回答

1. **是否减少逾期订单？**
   是。late-risk 的三-seed平均 `late_count=68.667`，当前 baseline 为 `69.667`，
   此前非 main 分支正式结果中的最低值为 `69.333`。
2. **是否降低总逾期时间？**
   是。late-risk 的三-seed平均 `total_lateness=2310.280`，当前 baseline 为
   `2328.494`，此前平均逾期数最低的 A2 重跑为 `2317.712`。
3. **是否增加拒绝订单？**
   没有。所有最终路线仍覆盖全部 200 个订单；临时 pool 状态不作为评价结果。
4. **是否超过当前 A2 和此前非 main 分支结果？**
   是。late-risk 在正式主表中具有最小的平均 `late_count`；保存的最佳路线也具有
   最小的单次 `late_count=66`。
5. **是否值得替代 A2 作为最终提交版本？**
   值得。推荐 A2 + `late_risk_destroy`；其余新增模块保持关闭。

## 方法限制与稳健性边界

- 正式主表的任务、seeds 和总预算一致，但来自不同源码提交与不同运行批次。它能
  比较已保存的官方三项结果，不能把跨分支差异直接解释成单个算子的因果贡献。
- 当前和历史正式表都只有 3 个 seeds。三个官方指标的均值是实验汇总，不是新的
  比赛评分函数。
- 墙钟搜索会受到运行环境影响，因此同一个 A2 在不同批次得到
  `(70.000, 2321.448, 643.948)`、`(69.333, 2317.712, 640.149)` 和
  `(69.667, 2328.494, 645.747)` 三组均值。最终推荐首先依据当前同批 baseline
  与 late-risk 的直接官方结果，同时用全部历史分支检查结论是否仍处于领先位置。
- 固定 400 轮、单次构造和超时扩展已经分表，不参与正式 240 秒结论。

## 建议的下一步

最终提交配置只启用 `late_risk_destroy`。若继续实验，应增加 seeds，并继续逐 seed
保存 `late_count`、`total_lateness` 和 `distance`，不要引入加权综合指标。

## 仍需回答的问题

更多 seeds 下，late-risk 是否仍能保持最低 `late_count`；如果 `late_count` 相同，
它是否仍能保持较低的 `total_lateness`。这两点应继续使用题目原始指标直接回答。

## 数据来源

- `codex/uav-alns-dispatch@160dc98`：
  `algorithm/results/benchmark_runs.csv`、`benchmark_summary.csv`；
- `codex/alns-core-comparison@dfa71d7`：
  `algorithm/results/alns_core_comparison/comparison_runs.csv`、
  `comparison_summary.csv`、`run_solutions/`；
- `codex/stronger-uav-neighborhoods@7964a2f`：
  `algorithm/results/neighborhood_ablation/ablation_runs.csv`、
  `ablation_summary.csv`、`run_solutions/`；
- `feature/capacity-aware-halns` 数据提交 `bcd9506`：
  `algorithm/results/capacity_halns/capacity_halns_runs.csv`、
  `capacity_halns_summary.csv`、`run_solutions/`；
- 当前分支 `0905f0a`：
  `results/adaptive_deadline_rejection/adaptive_runs.csv`、
  `adaptive_summary.csv`、`adaptive_results.json`、`run_solutions/`。

当前 adaptive 的 15 个路线文件已经通过独立官方评价与完整任务覆盖校验。历史分支
数据通过 Git 对象直接读取，审计过程中没有 checkout 或改写其他分支。
