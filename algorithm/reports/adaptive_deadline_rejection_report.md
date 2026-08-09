# Adaptive Deadline / Rejection Pool 正式实验报告

## 结论摘要

在当前稳定 A2（`assignment_destroy + deadline risk guidance`）上，只有
`late_risk_destroy` 在 3 个固定 seed、每次 240 秒总预算的正式实验中超过 A2：

- 平均逾期订单从 `69.667` 降至 `68.667`，减少 `1.000` 单（`-1.435%`）；
- 平均总逾期从 `2328.494` 降至 `2310.280` 分钟（`-0.782%`）；
- 平均距离只增加 `0.107 km`（`+0.017%`）；
- 同 seed 官方词典序比较为 `2 胜 / 1 负 / 0 平`，且三个 seed 的
  `late_count` 都没有高于 A2。

Temporary rejection pool、soft deadline，以及三模块组合均为 `0 胜 / 3 负`。
因此建议最终提交配置采用 **A2 + late-risk destroy**，保持 rejection pool 和
soft deadline 关闭。三 seed 样本仍然有限；若后续有更多算力，应扩大 seed 数再确认稳定性。

## 实验设置

- 数据：官方 200 任务 CSV，SHA256
  `386d3fbaa9009aeed947493782d657ed0b30b25d369c8187bfc9619fc7e31d25`；
- 资源约束：8 架无人机、单机最多 25 个任务、载荷容量 2、速度
  `0.9 km/min`、开放路线；
- seeds：`2026080500`、`2026080501`、`2026080502`；
- 每次总墙钟预算：240 秒，其中共享 deterministic regret-2 初始解耗时
  `4.690018 s`，安全余量 2 秒，ALNS 有效上限 `233.309982 s`；
- 候选位置上限：48；soft deadline beta：0.20；
- 所有方法使用相同输入、相同初始路线、相同 seed 和相同预算；每个 seed
  轮换方法运行顺序；
- 最终评价保持真实 deadline 的官方严格词典序：
  `late_count > total_lateness > distance`；没有修改
  `algorithm/src/uav_dispatch/validation.py` 或 `model.Score`；
- 15/15 个结果均通过独立全量约束校验与 240 秒合规检查。

完整可恢复签名（输入、初始路线、配置、预算和全部求解源码 SHA256）保存在
`results/adaptive_deadline_rejection/adaptive_results.json`。

## 方法定义

| method | 配置 |
|---|---|
| baseline | 当前 A2：assignment destroy + deadline risk guidance |
| experiment1 | A2 + `late_risk_destroy` |
| experiment2 | A2 + temporary rejection pool |
| experiment3 | A2 + soft deadline、内部 weighted-lateness search score、risk-aware insertion |
| experiment4 | A2 + late-risk destroy + rejection pool + soft-deadline search package |

三个新增 feature flag 默认关闭，因此 baseline 不受新模块行为影响。A2 的
`RouteEvaluator` 也会跳过内部 weighted-lateness 热路径，避免在固定墙钟预算下
因 feature-off 开销减少迭代数。

## 三 seed 汇总

下表为 3 个相同 seeds 的算术平均；`runtime` 包含共享初始构造耗时。

| method | late_count | total_lateness | distance | runtime | iterations |
|---|---:|---:|---:|---:|---:|
| baseline | 69.667 | 2328.494 | 645.747 | 238.001 s | 1660.7 |
| experiment1 | **68.667** | **2310.280** | 645.854 | 238.008 s | 1891.0 |
| experiment2 | 74.000 | 2503.373 | 669.156 | 238.001 s | 1832.3 |
| experiment3 | 74.333 | 2412.587 | 653.681 | 238.001 s | 1288.3 |
| experiment4 | 76.333 | 2652.729 | 679.202 | 238.039 s | 1188.3 |

相对 A2：

| method | late_count delta | lateness delta | distance delta | iterations delta |
|---|---:|---:|---:|---:|
| experiment1 | **-1.000 (-1.435%)** | **-18.214 (-0.782%)** | +0.107 (+0.017%) | +230.3 (+13.870%) |
| experiment2 | +4.333 (+6.220%) | +174.879 (+7.510%) | +23.409 (+3.625%) | +171.7 (+10.337%) |
| experiment3 | +4.667 (+6.699%) | +84.093 (+3.611%) | +7.934 (+1.229%) | -372.3 (-22.421%) |
| experiment4 | +6.667 (+9.569%) | +324.235 (+13.925%) | +33.455 (+5.181%) | -472.3 (-28.442%) |

## 逐 seed 官方结果

| seed | method | late_count | total_lateness | distance | iterations |
|---:|---|---:|---:|---:|---:|
| 2026080500 | baseline | 70 | 2424.643 | 651.613 | 1911 |
| 2026080500 | experiment1 | 70 | 2356.015 | 644.763 | 1993 |
| 2026080500 | experiment2 | 75 | 2525.897 | 663.654 | 1851 |
| 2026080500 | experiment3 | 74 | 2366.775 | 648.984 | 1290 |
| 2026080500 | experiment4 | 77 | 2618.780 | 681.233 | 1122 |
| 2026080501 | baseline | 69 | 2260.411 | 633.590 | 1780 |
| 2026080501 | experiment1 | **66** | 2264.497 | 650.177 | 1974 |
| 2026080501 | experiment2 | 73 | 2554.133 | 679.246 | 1840 |
| 2026080501 | experiment3 | 74 | 2415.628 | 657.845 | 1307 |
| 2026080501 | experiment4 | 75 | 2619.534 | 666.966 | 1292 |
| 2026080502 | baseline | 70 | 2300.427 | 652.038 | 1291 |
| 2026080502 | experiment1 | 70 | 2310.328 | 642.621 | 1706 |
| 2026080502 | experiment2 | 74 | 2430.089 | 664.569 | 1806 |
| 2026080502 | experiment3 | 75 | 2455.357 | 654.215 | 1268 |
| 2026080502 | experiment4 | 77 | 2719.872 | 689.406 | 1151 |

## 同 seed 词典序对比

| method | wins | losses | ties | 说明 |
|---|---:|---:|---:|---|
| experiment1 vs A2 | **2** | 1 | 0 | seed 500 同逾期但总逾期更低；seed 501 少 3 个逾期；seed 502 同逾期但总逾期高 9.901 分钟 |
| experiment2 vs A2 | 0 | 3 | 0 | 三个 seed 分别多 5、4、4 个逾期 |
| experiment3 vs A2 | 0 | 3 | 0 | 三个 seed 分别多 4、5、5 个逾期 |
| experiment4 vs A2 | 0 | 3 | 0 | 三个 seed 分别多 7、6、7 个逾期 |

experiment1 在 seed `2026080501` 上以 3 个更少的逾期订单取胜；即使其总逾期和
距离略高，仍严格符合第一目标优先。seed `2026080502` 则因逾期数相同而由第二目标
判负，不能隐去这一反例。

## Rejection pool 审计

所有 15 个最终结果的 `final_rejected_count` 都是 0。启用 pool 时，搜索确实产生
临时拒绝状态：experiment2 和 experiment4 的每个 seed 峰值均为 20，等于
`floor(10% * 200)`，从未超过上限。超时前未完成回插的迭代会被丢弃，不能更新
current/best；最终路线由完整任务覆盖差集再次推导并断言 pool 已清空。

因此 rejection pool **没有增加最终拒绝订单**，但大量临时移出/重插没有改善
正式得分。它应保留为默认关闭的实验功能，而不应进入最终提交配置。

## 对五个问题的回答

1. **是否减少逾期订单？**
   是，但仅限 experiment1。平均减少 1 单，三个 seed 的 late_count 分别变化
   `0/-3/0`。experiment2、experiment3、experiment4 都增加逾期。
2. **是否降低总逾期时间？**
   experiment1 的三 seed 平均降低 18.214 分钟。逐 seed 分别变化
   `-68.628/+4.087/+9.901` 分钟；后两项中 seed 501 已先按少 3 个逾期获胜。
   其他方法的平均总逾期均更高。
3. **是否增加拒绝订单？**
   不增加最终拒绝订单，所有正式解均为 0。pool 方法会产生最多 20 个临时拒绝
   任务，但它们必须优先重插，未清空状态不能输出。
4. **是否超过当前 A2？**
   experiment1 超过 A2：官方配对为 2 胜 1 负，平均第一目标更好，且没有 seed
   出现更多逾期。其余三个配置均为 0 胜 3 负。
5. **是否值得替代 A2 作为最终提交版本？**
   建议用 **A2 + late-risk destroy** 替代当前 A2 配置；不要启用 rejection pool、
   soft deadline 或三者组合。该建议基于严格同 seed 240 秒结果，但只有 3 个 seed，
   合并后仍建议追加更多 seeds 做稳定性验证。

## 结果文件

- `results/adaptive_deadline_rejection/adaptive_runs.csv`：15 次逐运行数据；
- `results/adaptive_deadline_rejection/adaptive_summary.csv`：三 seed 汇总；
- `results/adaptive_deadline_rejection/paired_comparisons.csv`：配对胜负；
- `results/adaptive_deadline_rejection/paired_comparison_pairs.csv`：每个 seed 的指标差；
- `results/adaptive_deadline_rejection/adaptive_results.json`：manifest、完整签名与全部统计；
- `results/adaptive_deadline_rejection/run_solutions/`：15 个完整路线 JSON；
- `results/adaptive_deadline_rejection/adaptive_runs.partial.json`：可恢复且已严格复核的
  完成边界。

正式结果已使用 `--resume` 再次严格校验 15/15：解文件 SHA256、路线、官方得分、
特性开关、预算、临时池边界和任务完整性均与 checkpoint 一致。
