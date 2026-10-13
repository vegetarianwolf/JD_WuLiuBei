# 36 次局部修复探针

本探针是另行授权的新局部诊断，从上一批 allocation / fleet200 / seed1321 的最终完整路线出发；未运行完整 ALNS，也不将新修复得分计入旧批次。

9 种 destroy × 2 个种子（3、11）× regret2/3，共 36 次，每次移除并重新插入 10 项任务。全部末解经独立完整评分验证合法。

使用当前已冻结源码，`enable_regret_position_fallback=False`、`enable_regret_portfolio=False`，同时关闭 lazy，仅观察旧 route-distinct regret。`use_deadline_risk=False` 与原长测的风险引导不同，结果只适用于本探针设置。

| 策略 | 修复轮数 | 所有任务均不足 k 个不同无人机备选 | 尚有多项任务时出现上述情况 |
| --- | ---: | ---: | ---: |
| regret2 | 180 | 39 (21.67%) | 21 |
| regret3 | 180 | 63 (35.00%) | 45 |

逐轮以可用任务槽无人机数 `< k` 标记结构性备选不足，并逐次断言其累计数量等于源码实际 `regret_all_infinite_round_count`。不足备选并不等于无法插入；36 次修复全部合法。

该现象支持检查候选不足时的判别规则，但不能证明它导致原长测退化：只探测一个最终状态，且没有原搜索中的被拒候选、算子组合或状态序列。

[完整逐次/逐轮记录、修复路线和哈希](local_repair_probe.json) · [实际源码快照](probe_solver.zip) · [复算脚本](local_repair_probe.py)

从仓库根目录运行（当前 solver 源码须与记录的哈希一致；也可将 probe_solver.zip 解压到独立临时目录并将该目录置于 PYTHONPATH）：

```bash
PYTHONPATH=algorithm/src python algorithm/results/regret_followup_20261013/diagnostics/local_repair_probe.py
```
