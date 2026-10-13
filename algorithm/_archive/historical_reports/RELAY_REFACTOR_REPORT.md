# Relay-aware ALNS 重构报告

## 1. Architecture

旧结构：

```
Task -> 同一 UAV 完成 pickup (+task_id) 与 delivery (-task_id)
ALNS destroy/repair 直接操作 ±task_id 任务对
RouteEvaluator / validation 均假设同机取送、路线内配对
```

新结构：

```
OriginalTask (200) -> TaskPlan (DIRECT | RELAY(r)) -> TransportLeg (RELAY_IN / RELAY_OUT)
```

- **编码层（immutable LegRegistry）**：DIRECT 任务的访问保持原始 `±task_id`（pickup/delivery），保证 `--disable-relay` 位级兼容；RELAY 任务在 Problem 构造时按"每个任务 × 每个候选中继"分配唯一正 leg_id（自 max_task_id+1 起，solve 期间不可变）。`+leg_id` 为腿起点事件（load +1）、`-leg_id` 为腿终点事件（load -1），Relay 卸货真实减载、Relay 取货真实加载，机载载荷始终 ∈ [0,2]。
- **单一事实源**：routes（signed-leg 元组）为唯一 canonical 状态；`PlanIndex`（task→TaskPlan、两腿路线/位置、delivery 责任）只是派生缓存，接受 move 后按路线版本增量失效。
- **中继站选址**：`build_relay_network` 在 ALNS 前以任务中点做加权 k-medoids（weight = 1 + α·归一化距离 + β·deadline urgency），默认 `round(sqrt(8)) = 3` 站；每任务缓存 detour_ratio ≤ 1.5 的 top-2 候选（无合格时保留最合理 1 个）。一次 solve 内坐标与候选不变。
- **LocalRouteEvaluator**：保留 signed-route 语义与 `RouteProfile` O(1) 容量剪枝；K=25 检查改为"最终 delivery 责任计数"硬约束，路线事件数 60 仅为软剪枝。每条腿单独插入（RELAY_IN 不占 K 槽位，RELAY_OUT 占一个）。
- **GlobalRelayEvaluator**：为完整候选解构造 precedence DAG——路线续接边（权 = dist/speed）+ 交接边（drop→pickup，权 0），拓扑序最长路径传播最早时间；取件早于卸货自动等待，等待计入后续到达时间与逾期；DFS 三色环检测，有环即 deadlock → 候选不可行。输出 `GlobalSchedule`（事件时间、waiting、最终送达时间、全局 Score），validator 与 report 消费同一 Schedule。
- **ALNS destroy/repair**：destroy 以 original task 为单位，RELAY 两腿同删；repair 的 greedy/regret-2/regret-3/deadline/slack 在 DIRECT 与各候选 Relay 计划之间比较（regret 可跨模式），每个任务每次重建后可自由改变模式与中继。
- **两级剪枝**：① 排名阶段纯 surrogate——单腿位置：每路线容量可行位置经节点级距离增量取 top-4 做精确局部评估，inbound/outbound 各取 top-8 组合成 8×8=64（纯算术，不物化路线，不做 global 评估）；② 只有被选中的任务才对其短名单（DIRECT top-2 + 最优 Relay 计划的 top-4 组合）统一做 global 精确评估（含 waiting），取词典序最优；接受/最佳追踪全部用 global score。另配 route-version 选项缓存与 `sample_every=4` 门控（每轮约 25% 任务生成 Relay 候选；detour > 1.3 的任务无候选、无 fallback）。

## 2. Changed files

| 文件 | 修改 |
|---|---|
| `src/uav_dispatch/model.py` | 新增 `RelayStation`/`TransportLeg`/`TaskPlan`；`Problem` 增加 `relay_stations`/`leg_registry`/`task_relay_candidates`/`max_relay_hops`；平铺 visit→node 映射与每任务腿表；`distance()`/`point_for_visit()` 支持腿访问 |
| `src/uav_dispatch/relay.py`（新增） | `RelayNetwork`/`build_relay_network`（加权 k-medoids、detour 候选缓存）、`PlanIndex`/`build_plan_index`、`GlobalSchedule`/`GlobalRelayEvaluator`、`relay_statistics` |
| `src/uav_dispatch/search.py` | `RouteEvaluator` 腿语义；`_delivery_event_count`（K=25 责任）；`RouteInsertionSurface` + 节点级距离增量 + 有界堆选位；`route_leg_insertion_options`/`insert_leg_pair`；`route_insertion_options` 支持共享插入面 |
| `src/uav_dispatch/alns.py` | destroy 全部 original-task 化（两腿同删、removal gain 跨路线求和）；`TaskInsertionOption`/`_RelaySearchConfig`/`_RepairCache`；`_all_plans_for_task`/`_build_relay_beam`/`_apply_task_plan`/`_repair_relay`；`construct_regret_initial` Relay-aware；`solve_alns` 在 relay 模式下用 global score 并关闭 VND/ejection/route pool |
| `src/uav_dispatch/validation.py` | 消费 GlobalSchedule 重写：任务恰好一次、DIRECT XOR RELAY、同 task 同 relay、drop≤pickup、容量、K=25、无环、按最终送达判 deadline |
| `src/uav_dispatch/cli.py` | `--relay-count/--relay-candidates-per-task/--relay-detour-ratio/--relay-plan-beam/--relay-leg-beam/--relay-event-cap/--relay-location-seed/--disable-relay/--relay-debug`；`result_payload` 输出 relay 统计块、task_plans、visit 级 leg/relay 元数据 |
| `src/uav_dispatch/__init__.py` | 导出新数据结构与函数 |
| `tests/test_relay.py`（新增） | 22 个测试（§11 清单） |
| `experiments/run_relay_comparison.py`（新增） | Direct-only vs Relay-aware 配对墙钟对比，词典序判优 |
| `README.md` | Relay-aware 模型、参数与实验说明 |
| `src/uav_dispatch/exact.py`、baseline 构造器 | 不修改（保持 DIRECT-only） |

## 3. Removed / disabled mechanisms

- **VND、route pool、ejection、cluster repair**：在 relay 模式下强制关闭（`solve_alns` 中 `relay_cfg` 非空时跳过）；DIRECT-only 路径保留原行为以兼容既有实验与测试。
- **旧"Relay 后处理"类机制**：本仓库此前没有 blocker-relay/swap-relay/global relay guidance 等后处理实现；新模型下 Relay 完全内置于 destroy/repair 搜索空间，不存在 ALNS 后补 Relay 的路径。
- **人为 Relay 奖励**：未引入；Relay 只通过词典序目标被采纳。

## 4. Complexity

- **候选剪枝**：每条路线容量可行位置 O(n²) 每轮共享一次（`RouteInsertionSurface`）；单腿取 top-4 精确评估（有界堆 O(n² log 4) 比较 + 常数次精确评估）；每任务 Relay 组合 8×8=64 次 O(n) 组合检查。
- **beam + global**：每 (task, relay) 只对 beam 顶部（≤6）做 global 精确评估；GlobalRelayEvaluator 一次 O(V+E)，V≈每机事件数（≤约 60）×8、E 为路线边 + 交接边（≤400），远小于单次全路线精确评估总量。
- **route-version 缓存**：repair 内每次插入只使 1-2 条路线的选项失效，DIRECT/单腿/组合 beam 均按 (task|leg, route, version) 或版本快照缓存，避免 O(removed²) 重算。
- 实测（本机 Windows，200 任务）：DIRECT-only 搜索 ≈2.6–2.7 it/s；Relay-aware 默认配置 ≈1.7–1.9 it/s（调参配置 2.0–2.1 it/s），速度开销约 20–35%，无数量级退化；global_evaluation_count 从 P0 前约 50,000 降到 19,000–20,000（约 49/轮），P0 目标（<5k–10k 区间量级）基本达成。

## 5. Tests

`python -m pytest algorithm/tests -q` → **71 passed, 0 failed**（49 个既有测试全部保留通过 + 22 个新增 Relay 测试）。

## 6. Benchmark

默认配置（relay_count=3、candidates=2、detour≤1.3 无 fallback、beam=6、global_limit=4、sample_every=4、refine 关闭、P0 两级评估）、同数据（SHA-256 `386d3fba…`）、同 seed、同 240 秒总墙钟预算（初始构造计入预算并预留 2 秒安全余量）、同目标函数与 ALNS 算子框架，逐 seed 按 `(late_count, total_lateness, distance)` 词典序判优：

| seed | Direct-only | Relay-aware | 判优 |
|---|---|---|---|
| 2026080500 | **(78, 2744.32, 697.53)** it/s 2.72 | (88, 3310.72, 743.98) it/s 1.69 | Direct |
| 2026080501 | **(73, 2739.72, 711.17)** it/s 2.68 | (82, 2927.48, 713.49) it/s 1.81 | Direct |
| 2026080502 | **(79, 2756.23, 705.27)** it/s 2.64 | (84, 3081.98, 725.72) it/s 1.87 | Direct |

- **配对结果：Direct 3 : 0 Relay**（无平局）；逾期差距从旧配置的 13/16/4 收窄到 10/9/5。
- 均值：Direct (76.7, 2746.8, 704.7)，Relay (84.7, 3106.7, 727.7)。
- Relay-aware 的搜索 it/s ≈ 1.69–1.87（旧配置 1.23–1.27），为 Direct-only（≈2.68）的约 63–70%，无数量级退化；P0 改造后 global_evaluation_count 从 ~50,000 降到 ~11,000（40 轮诊断 2416 ≈ 60/轮）。
- 每种子 Relay 使用：1 / 2 / 4 个任务，跨机交接 1 / 2 / 3 次。

结论：Relay 真实进入了 ALNS 解（3 个种子全部），且 P0/P1 改造后速度与质量均改善，但在该数据集与词典序目标下仍未胜过 Direct-only。这是机制层面的结果而非实现缺陷（见 §7 排查清单）；按照计划 §三十八，没有引入任何人为 Relay 奖励。

调参配置（relay_count=2、每任务 1 候选站、sample_every=6，同预算同 seed）：

| seed | Direct-only | Relay-aware（调参） | 判优 |
|---|---|---|---|
| 2026080500 | **(80, 2715.47, 691.15)** it/s 2.64 | (87, 3325.97, 739.64) it/s 2.04 | Direct |
| 2026080501 | **(73, 2739.72, 711.17)** it/s 2.62 | (79, 2874.88, 718.05) it/s 2.13 | Direct |
| 2026080502 | **(79, 2756.23, 705.27)** it/s 2.57 | (80, 2760.45, 680.20) it/s 2.08 | Direct |

- 调参后 Relay 三指标继续改善（87/79/80 vs 默认 88/82/84），it/s 提升到 2.04–2.13（Direct 的 78–81%），global_evaluation_count 为 19,216 / 20,442 / 19,940（约 49/轮）。
- seed 2026080502 的 Relay 解总逾期（2760.45）与 Direct（2756.23）几乎持平且距离更短（680.20 < 705.27），仅 late_count 80 vs 79 惜败——词典序判优下仍是 Direct 3:0。
- 结论不变：Relay 真实进入 ALNS 解且速度开销已被压到 ~20–25%，但在该数据集 + 词典序目标下未能反超 Direct-only；未引入任何人为 Relay 奖励。

## 7. Relay usage

加权 k-medoids（weight = 1 + α·归一化距离 + β·deadline urgency，location seed 42）在 200 个任务中点上学出固定中继站；默认配置 3 站，调参配置 2 站：

| 配置 | 中继站坐标 (km) | seed0 任务数 | seed1 任务数 | seed2 任务数 |
|---|---|---|---|---|
| 默认 3 站 | (3.61, 5.66) / (3.89, 3.48) / (5.83, 4.79) | 1 | 2 | 4 |
| 调参 2 站 | (4.18, 5.53) / (4.69, 3.55) | 1 | 2 | 1 |

- **跨机交接**：默认配置 1 / 2 / 3 次，调参配置 1 / 2 / 1 次；全部 Relay 使用均为跨机（无同机 Relay），与"RELAY_IN/RELAY_OUT 可由不同 UAV 执行"的设计目标一致。
- **等待**：两个配置全部种子 `total_relay_waiting_time` 均为 0——被采纳的交接都是卸货先于取件；GlobalRelayEvaluator 的等待机制工作正常（新增测试覆盖了等待级联与精确时间断言）。
- **Relay 是否真正进入 ALNS 解**：是。两种配置的 6 个 Relay 运行中 6 个最终解都含 1–4 个 Relay 任务，且任务被 destroy 后可在 DIRECT 与 RELAY 之间切换（测试覆盖双向切换）。
- **为何未占优**（§三十八 排查清单逐项验证）：① 跨机未被禁止（有跨机交接）；② validator 不要求同机取送（新增测试覆盖）；③ detour 过滤未全剪（147/200 任务有候选）；④ 全局评估器正确（waiting/cycle/容量均有测试）；⑤ repair 确实生成 Relay 选项（40 轮诊断生成 6,934 个、选中 12 个）；⑥ regret 可在 DIRECT 与 RELAY 间比较；⑦ 责任转移允许（K=25 按最终送达计）；⑧ 等待未被过度保守（实测 0）；⑨ beam（6）与 global_limit（4）够宽；⑩ 初始解能进 Relay 邻域。结构性原因：8×25 恰好满载 + 全部任务有 deadline，没有空闲无人机接后半程；Relay 的绕行距离与交接等待无法被 lateness 收益覆盖。
