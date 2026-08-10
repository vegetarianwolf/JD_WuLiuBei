# Adaptive Deadline / Rejection Pool Changelog

开发基线：`codex/stronger-uav-neighborhoods`（`7964a2f`）  
开发分支：`feature/adaptive-deadline-rejection-pool`

## 修改文件

### 算法实现

- `algorithm/src/uav_dispatch/alns.py`
  - 新增 feature-flagged `late_risk_destroy`；
  - 新增 bounded temporary rejection pool，并在普通 destroy 任务前优先 regret-2 回插；
  - 新增 soft-deadline guidance、内部严格词典序 SearchScore 接受逻辑和
    risk-aware repair 参数传递；
  - 最终拒绝数由路线完整任务覆盖真实推导，pool 未清空时拒绝输出；
  - feature-off A2 关闭内部 weighted-score 热路径。
- `algorithm/src/uav_dispatch/search.py`
  - 新增 `SearchScore(late_count, weighted_lateness, distance)`；
  - 新增 `soft_deadline()` 与基于真实 deadline 紧迫度的 priority；
  - 新增全路线 soft-deadline weighted-lateness 缓存；
  - 插入候选支持 `distance increase + lambda * lateness increase` 引导，
    第一层仍严格保护 `delta late_count`；
  - 默认关闭路径保持原官方插入排序和评价语义。
- `algorithm/src/uav_dispatch/__init__.py`
  - 导出新增的搜索分数与 soft-deadline helper。

### 实验与文档

- `algorithm/experiments/run_adaptive_deadline_rejection.py`
  - 定义 baseline 与 experiment1–4；
  - 支持相同初始解、seed、预算、轮换运行顺序；
  - 增加原子逐运行 checkpoint、严格 `--resume`、输入/源码/配置/路线哈希；
  - 输出逐 seed 路线 JSON、运行/汇总 CSV、paired comparison CSV 与总 JSON；
  - 显式校验 240 秒合规、完整任务覆盖和 rejection pool 边界。
- `algorithm/reports/code_structure_review.md`
  - Phase 1 代码结构、插入点与风险审查。
- `algorithm/reports/adaptive_deadline_rejection_report.md`
  - 3 seeds × 5 methods × 240 秒正式结果、逐 seed 对比与最终建议。
- `algorithm/README.md`
  - 增加正式实验与断点恢复命令。
- `results/adaptive_deadline_rejection/`
  - 15 个正式完整路线、CSV、JSON、checkpoint，以及各 Phase smoke 结果。

### 测试

- `algorithm/tests/test_alns_neighborhoods.py`
- `algorithm/tests/test_adaptive_deadline_rejection.py`
- `algorithm/tests/test_adaptive_deadline_experiment.py`
- `algorithm/tests/test_adaptive_terminal_audit.py`

覆盖 late-risk 选择与完整 pair 删除、pool 上限/优先回插/终态清空、真实与 soft
deadline 隔离、SearchScore 词典序、全路线 risk-aware 插入、baseline 热路径、五方法
配置差异、预算合规、checkpoint 恢复和官方三项指标比较顺序。最终全套测试为
`73 passed`。

`algorithm/src/uav_dispatch/model.py`、`algorithm/src/uav_dispatch/validation.py`、
官方输入格式和历史实验均未修改。

## 算法思想

### Late Risk Destroy

对任务计算：

```text
risk_i = 0.5 * lateness_ratio
       + 0.3 * deadline_pressure
       + 0.2 * detour_contribution
```

deadline pressure 在当前任务集合内归一化；detour 使用删除完整任务对后的路线相对
距离改善并截断负值。每轮确定性删除风险最高的完整 pickup-delivery pairs，让已有
repair 跨路线重新分配它们。

### Temporary Rejection Pool

pool 只存在于一次 destroy/repair 迭代内部，容量为
`floor(0.10 * total_tasks)`。只有删除任务后真实 `(late_count, total_lateness)`
严格改善时才允许进入 pool；pool 任务优先使用 regret-2 回插。未完全回插的部分解
不能参与接受、best 更新或最终输出。

### Soft Deadline 与 Adaptive Search

```text
soft_deadline_i = real_deadline_i
                + beta * estimated_service_time_i
```

beta 支持 `0.15/0.20/0.30`，仅用于 destroy/repair/search guidance。内部搜索分数
严格比较 `(late_count, weighted_lateness, distance)`；weighted lateness 按真实
deadline 的迟到分钟与 deadline 紧迫度 priority 计算。官方 best 和最终评价仍只用
真实 `(late_count, total_lateness, distance)`。

## 正式实验结果

配置：200 任务、8 架无人机、单机 25 任务、容量 2；seeds
`2026080500..2026080502`；每次总预算 240 秒；相同输入、初始路线和官方评价。

| method | mean late_count | mean total_lateness | mean distance |
|---|---:|---:|---:|
| A2 baseline | 69.667 | 2328.494 | 645.747 |
| A2 + late-risk destroy | **68.667** | **2310.280** | 645.854 |
| A2 + rejection pool | 74.000 | 2503.373 | 669.156 |
| A2 + soft deadline | 74.333 | 2412.587 | 653.681 |
| A2 + 三者组合 | 76.333 | 2652.729 | 679.202 |

late-risk destroy 的三个 seed `late_count` 为 `70/66/70`，A2 baseline 为
`70/69/70`。跨 main 之外全部历史分支的同口径正式结果审计见
`algorithm/reports/adaptive_deadline_rejection_report.md`；报告只使用题目官方的
`late_count`、`total_lateness`、`distance`。

15/15 个正式解均合法且未超过 240 秒。所有最终 `rejected_tasks` 均为 0；启用
pool 的运行临时峰值为 20，未超过 10% 上限。

## 是否推荐合并

**推荐合并，并将最终提交配置设为 A2 + `late_risk_destroy`。**

不建议在最终提交中启用 rejection pool、soft deadline 或三者组合：它们在三个
正式 seeds 上均输给 A2，soft-guided 路径还显著降低固定墙钟内的迭代数。相关代码
保留为默认关闭的 feature flags，便于后续研究，不改变 A2 默认行为或官方评价。

当前结论仅基于 3 个固定 seeds；若有额外算力，建议扩大 seed 数复核 late-risk
提升的稳定性。
