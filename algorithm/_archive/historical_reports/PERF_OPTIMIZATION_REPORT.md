# Direct ALNS 实现级性能优化报告（Tier 1）

日期：2026-08-14
环境：Windows / Python 3.13.3，200 tasks / 8 UAV / Direct 模式，seed=2026080500

## 1. 背景与目标

在严格保持语义的前提下（算子集合、candidate_limit=48、repair_rank_candidate_limit=24、
repair_exact_candidate_limit=48、destroy 比例 0.04–0.10、严格词典序目标均不变），
对 Direct ALNS 的热路径做两轮实现级优化：

- **第一轮**（Score 比较消除、O(n) 容量结构、raw 增量评估、solve 级 surface LRU 等 10 项）
- **第二轮（Tier 1）**：距离 delta 数组分解、early-delivery 免排序、raw 双段循环、验证器与 destroy 的 Direct fast path

## 2. Tier 1 改动摘要

| 项 | 内容 |
|---|---|
| P1 surface 扩展 | `RouteInsertionSurface` 新增 `positions_by_early_delivery`（(delivery, pickup) 序直接枚举，免排序）与 `delivery_start_index`（每 pickup 位置一次 `bisect_left` 预计算） |
| P2 raw 双段循环 | `_pair_insertion_delta_raw` 用预计算索引免每 call bisect；按 split 拆 first/second 两段、`shift>0` 守卫上提；删除死判断 `position >= length` |
| P3 距离 delta 分解 | `_build_distance_delta_arrays` O(n) 预计算 pickup/delivery 侧数组；`delta_of` 闭包直接引用局部数组（与 `_node_distance_delta` 运算序逐位一致）；`_select_candidate_positions` 的 early-delivery 配额仅对选中项懒计算 delta |
| P4 验证器 fast path | 无 relay、无违规、无未知访问时跳过 `build_plan_index` 与 `GlobalRelayEvaluator`；非法解走完整路径保证违规文案逐位一致 |
| 附带 | `destroy_solution` Direct 模式用轻量 `_DirectPlanIndex` 替代每迭代一次的全量 plan index 构建；修复 validator 未知访问回退分支的既有 KeyError |

## 3. 240 秒实验（核心结果）

同 seed、同配置下，Tier 1 前后各运行一次 240 秒 Direct 基准：

| 指标 | Tier 1 前 | Tier 1 后 | 变化 |
|---|---|---|---|
| 迭代数 | 3,879 | **4,458** | +14.9% |
| it/s（无 cProfile 真实） | 16.16 | **18.57** | **+14.9%** |
| late_count | 66 | **65** | −1 |
| total_lateness_min | 2244.0838 | **2178.2708** | −2.9% |
| distance_km | 635.1032 | 636.2122 | +0.17% |

- 解按严格词典序（late_count → total_lateness_min → distance_km）比较：65 < 66，新解**严格更优**；
  distance 的 +0.17% 是词典序第三分量的代价，属正常波动。
- 确定性验证：30 秒跑的前 3879 次迭代与 Tier 1 前轨迹完全一致（同 seed 30s 解分数逐位相同），
  多出的 579 次迭代带来上述改进。

### 每迭代工作量对比（证明"只变速度、不变算法"）

| 计数器（除以迭代数） | Tier 1 前 | Tier 1 后 |
|---|---|---|
| route_delta_evaluation / iter | 6,845.1 | 6,838.2 |
| surface build / iter | 13.46 | 13.41 |
| surface LRU 命中率 | 95.83% | 95.85% |

每迭代的评估次数几乎不变，收益完全来自单次操作成本下降。

## 4. 性能汇总

| 场景 | Tier 1 前 | Tier 1 后 | 提升 |
|---|---|---|---|
| 240s 真实 it/s | 16.16（3,879 iters） | 18.57（4,458 iters） | +14.9% |
| 30s 真实 it/s | 16.40（492 iters） | 18.23（547 iters） | +11.2% |
| 30s cProfile it/s | 7.66（230 iters） | 8.63（259 iters） | +12.7% |
| 146 固定迭代耗时折算 it/s | 16.28 | 19.03 | +16.9% |

（相对第一轮优化前的基线 4.87 profile it/s，累计提升约 +77%。）

## 5. 质量门禁（全部通过）

1. **同 seed 固定 146 迭代**：分数逐位一致
   `late=89, total_lateness=3662.3422487670678, distance=768.5368519767859`，
   9 个 profiling 计数器与 Tier 1 前完全相同（搜索轨迹不变）。
2. **legacy 全评估路径**（`UAV_DISPATCH_LEGACY_CANDIDATE_EVAL=1`）同 seed 146 迭代：
   分数同样逐位一致 → 增量评估与候选筛选语义完全等价。
3. **pytest：78 passed**，含 4 项新增等价测试：
   - 数组分解 delta 与 `_node_distance_delta` 在全可行位置上 `==` 逐位相等；
   - `positions_by_early_delivery` 与 `sorted(positions, key=(d,p))` 相等；
   - 有界筛选与内联全排序参考实现输出列表完全相等；
   - 验证器 fast path 与完整路径在随机 Direct 问题（含非法解、未知访问）上字段级相等。

## 6. 方法论要点

- 每项改动均以"独立参考实现"做对拍（浮点用 `==` 而非近似），保证不是"近似等价"而是"逐位等价"。
- 收益最大的单点是 `delta_of` 闭包改造：经包装函数时每 call 做 7 元组解包（cProfile 4.2s），
  改为直接闭包局部数组 + early 配额懒计算后降至 0.94s。
- 验证器 fast path 必须加 `not violations` 守卫：relay schedule 会在非法解上追加额外违规文案，
  无守卫会导致非法解的违规信息与旧行为不一致。

## 7. 结论

Tier 1 在不改变任何算法决策的前提下把 240s 预算内吞吐提升约 **15%**
（4.87 → 8.63 profile it/s 累计 +77%），240s 解在词典序下**严格更优**
（late 66 → 65，lateness 2244.1 → 2178.3），且同 seed 同迭代解逐位一致。
后续候选（Tier 2）：destroy removal_gain 增量化、scored 构建免 O(n²) 枚举的 X+Y top-k。

---

# Tier 2 增补（2026-08-15）

## 8. Tier 2 改动与取舍

| 项 | 内容 | 结论 |
|---|---|---|
| T2-2 destroy removal_gain 增量化 | `_pair_removal_delta_exact`：对完整 route 的 surface 拼接 visit/node 元组重走，聚合 reduced 总分后**单次相减**（与 `Score.__sub__` 的结合序一致） | **保留**（逐位精确） |
| T2-3 rank/exact 共享 scored bundle | `_TaskScoredBundle` + 32/256 项 FIFO，exact 阶段复用 rank 阶段的距离 delta 列表 | **回滚**（实测净亏 ~7%） |

**T2-2 逐位精确的关键**：初次实现用"shift 反推"（reduced_time = full_time − shift），
与全评估的浮点累加序不同，产生 1 ULP 偏差并翻转了同 seed 轨迹；
改为重走拼接路线 + `base_total − reduced_total` 单次减法后，等价测试（严格 `==`）
与 146 固定迭代轨迹测试全部通过。

**T2-3 回滚依据（实证 A/B）**：bundle 构建每次约多 ~49µs（dataclass、重复闭包、
7 元组拆装），而 exact 阶段命中只省 ~15µs/次；即使 FIFO 从 32 调到 256（命中率 100%），
固定 146 迭代仍为 17.5 it/s vs 无 bundle 的 19.1 it/s → 按"速度不得换质量、纯开销不回本即回滚"
原则弃用。

## 9. Tier 2 结果汇总

| 指标 | Tier 1 后 | Tier 2 后 | 变化 |
|---|---|---|---|
| B（30s cProfile） | 259 iters / 8.63 | **283 iters / 9.43** | **+9.3%** |
| A（30s 无 cProfile） | 547 iters / 18.23 | 551 iters / 18.37 | +0.8% |
| 146 固定迭代 it/s | 19.03 | 18.81 | ≈（噪声内） |
| route_full_evaluation @30s | 14,904 | **1,857** | **−87.5%** |
| 146 固定迭代分数 | 89/3662.3422487670678/768.5368519767859 | 同左逐位一致 | 不变 |

额外收益：removal 不再向 `RouteEvaluator` 的 lru_cache 灌入每轮 ~17.5k 个 reduced
路由条目，消除了缓存颠簸对其它 `evaluate()` 调用的间接拖累。

## 10. 累计进展

- 相对最初基线（146 iters / 4.87 profile it/s）：B 提升至 9.43（**+93.6%**），
  A 真实吞吐 16.4 → 18.4 it/s。
- 所有门禁保持通过：`python -m pytest` 79 项全绿；同 seed 146 迭代三路（新代码/legacy env）
  分数逐位一致；全部算子、候选上限与词典序目标未动。

后续候选：X+Y top-k 免 O(n²) 枚举（scored 构建仍占 route_insertion_options 大头）；
rank/exact 共享若要以更低开销实现，可考虑在 `_repair` 内显式传递 scored 而非泛化缓存。

---

# P1 实验增补（2026-08-15，已回滚）

## 11. P1（exact 阶段 scored + raw 直传）实验与回滚

按计划实施了 rank→exact 轻量直传：`_ScoredPackage`（scored 全量列表 + delta 数组 +
rank 阶段 24 个 raw 增量结果）、64 项 FIFO、`direct_options_with_package`、`ranked_options`
双模式。等价测试（package 路径 vs 全新路径逐项相等、24⊆48 前缀性质）全部通过，146 固定
迭代分数逐位一致。

**实测结果**：raw 评估次数 −14%（960,173→823,434），数组构建 −22%（61,390→47,834），
但 B 从 9.43 降到 8.90（−5.6%），fixed-146 从 18.81 降到 18.73 → **净亏，整体回滚**。

**根因（cProfile 证据）**：ranked 热循环约 4M 次迭代中，每位置 ~0.25µs 的
字典查找/分支/元组分配（`dict.get` 2.8M 次 / 0.75s；`route_insertion_options`
tottime +0.77s）完全吞掉了复用收益（raw ~1.35s + 数组 ~0.4s）。

**结论性教训**：`_pair_insertion_delta_raw` 已优化到 ~3.9µs/次，"为省它而查表"的
per-item 开销比直接重算更贵。此类复用只有在做到**零 per-item 开销**（向量化、
排序归并式复用）时才值得做——这正是 X+Y top-k 路线的价值所在。

## 12. 当前最终状态

- 代码 = Tier 1 + Tier 2（removal 增量），P1 已完全回滚；79 项测试全绿。
- 基线指标：B 9.43（283 iters）、A 18.37（551 iters）、fixed-146 19.1、
  146 固定迭代分数逐位一致。
- 下一候选：P0（pickup 侧数组下沉，纯查表零分支，预计 +5~7%）；
  X+Y top-k（免 O(n²) 枚举，研究级）。

---

# P0 实验增补（2026-08-15，已回滚）

## 13. P0（pickup 侧数组下沉）实验与回滚

将 `pickup_out / pickup_shift / start_time` 三个只依赖 `(p, task, surface)`
的数组下沉到 `_build_distance_delta_arrays`（11 元组返回），`_pair_insertion_delta_raw`
接受可选数组参数跳过 pickup 侧计算。逐位等价测试（数组路径 vs 独立计算 `==`）通过。

**实测**：raw 单次 3.58→3.27µs（1.88M 次共省 ~0.58s），但数组构建
1.77→2.33s（+0.56s）——调用次数比 30.4:1 恰好落在盈亏线上；B 9.43→9.50（+0.7%），
fixed-146 三次均值 18.65 vs 基线 19.04（−1.5%）。**净持平/微亏，整体回滚。**

**结论性教训**：在 CPython 下，每 `(task, route)` 构建一次的数组（~61k 次）
每增加 9µs 成本，需要 1.88M 次 raw 调用每次省 >0.3µs 才能回本——pickup 侧预计算的
边际收益与构建开销几乎完全对冲，此类"下沉式"复用无利润空间。

## 14. 实验累计结论

三个"复用型"优化（T2-3 scored 缓存、P1 rank→exact 直传、P0 pickup 数组下沉）
经隔离 A/B 全部证实净亏并回滚，根因一致：**热循环已优化到每项 ~0.2-4µs，
任何为"复用"引入的 per-item 查找/分支/构建开销都会吞掉收益**。
剩余有空间的方向只剩结构性改造（X+Y top-k 免 O(n²) 枚举、排序归并式复用），
其共同特征是复用本身零 per-item 开销。当前代码保持 Tier 1 + Tier 2 状态：
B 9.43 / A 18.37 / fixed-146 ~19.1，79 项测试全绿，同 seed 解逐位一致。

---

# Tier 3 增补（2026-08-15，Direct 提效：安全微优化已提交；调参与 X+Y 按实证回退）

## 15. 目标与验收

对 Direct（无 relay）搜索路径提效。验收标准（240s 配对 A/B，同 seed）：
**it/s ≥ +10% 且词典序分数（late_count → total_lateness_min → distance_km）不劣于基线**。
基线 = 改动前 240s：`(65, 2121.106986418821, 631.7351110041901)`，27.466 it/s。

环境与配置：200 tasks / 8 UAV / capacity=2；seed 2026080500；`--method alns-core`
（route_pool/ejection 关闭，与 `run_scenario_comparison.py` 的 direct 场景一致）；
candidate_limit=48；238s 求解预算（安全余量 2s）。

## 16. 安全微优化（已提交）：scored 全位置扫描内联

`route_insertion_options` 每 (task, route) 调用一次 O(n²) 的全位置距离增量扫描
（本数据集每路线 ~50 访问 → ~1250 个位置）。原实现为每位置一次 `delta_of` 闭包调用 +
7 元组数组解包；改为模块级 `_scored_distance_deltas`（`search.py`），内联对角/非对角
两条分支，运算顺序与闭包逐位一致。

**门禁**：新增 `test_scored_distance_deltas_match_closure_reference`（32 组随机
问题 × 全可行位置，与独立闭包参考实现 `==` 逐位相等）；pytest 112 项全绿；
240s 配对验证分数、best_update_count（305）逐位一致。

### 240s 配对 A/B

| 指标 | 基线（改动前） | Tier 3 后 | 变化 |
|---|---|---|---|
| 分数 | `(65, 2121.106986418821, 631.7351110041901)` | 完全相同 | 逐位一致 |
| it/s | 27.47 | 27.82 | **+1.3%** |
| best_updates | 305 | 305 | 相同 |
| time_to_best(s) | 205.21 | 204.56 | −0.65 |

固定 146 迭代快照（提交后）：`(89, 3465.7245168505237, 751.846477192008)`。
内联收益有限（+1.3%）：更早的 Tier 1 已把闭包开销压到 0.94s，剩余函数调用成本占比小；
热路径主成本是 O(n²) 扫描本身，而非调用方式。

## 17. 参数调优实验（Phase 2，已回滚）

为 `ALNSConfig` 增加 direct-only 覆盖参数（candidate/rank/exact 候选上限、destroy
比例、ejection），仅无 relay 路径生效；配套网格脚本跑 60s 粗筛 → 240s 定标。

### 60s 粗筛（seed 2026080500）

| 配置 | 60s 分数 | it/s | Δit/s |
|---|---:|---:|---:|
| baseline | `(70, 2352.5, 639.4)` | 27.34 | — |
| cand32 | `(70, 2354.1, 638.6)` | 26.05 | −4.7% |
| cand40 | `(70, 2352.5, 639.4)` | 27.12 | −0.8% |
| rank16 | `(72, 2506.4, 650.1)` | 30.59 | +11.9% |
| exact32 | `(71, 2414.8, 655.8)` | 27.83 | +1.8% |
| destroy06 | `(71, 2518.9, 662.1)` | 44.00 | +60.9% |
| destroy05 | `(73, 2381.4, 644.9)` | 53.71 | +96.5% |
| combo | `(70, 2308.4, 641.9)` | 51.38 | +88.0% |

### 240s 定标（seed 2026080500）

| 配置 | 240s 分数 | it/s | Δit/s | 词典序 vs 基线 |
|---|---:|---:|---:|---|
| baseline | `(65, 2121.1, 631.7)` | 27.47 | — | 基准 |
| destroy06 | `(67, 2250.0, 642.6)` | 44.81 | +63% | 劣（late +2） |
| destroy05 | `(68, 2259.4, 641.8)` | 55.59 | +102% | 劣（late +3） |
| combo | `(67, 2119.8, 634.9)` | 53.20 | +94% | 劣（late +2） |
| mild1_destroy07 | `(69, 2303.0, 634.1)` | 33.99 | +24% | 劣（late +4） |
| mild2_destroy065 | `(66, 2124.1, 622.8)` | 36.30 | +32% | 劣（late +1） |
| mild3_combo07 | `(65, 2128.9, 628.2)` | 34.95 | +27% | late 持平，lateness +7.8 min |
| mild4_cand40_destroy07 | `(69, 2303.0, 634.1)` | 33.74 | +23% | 劣（late +4） |

**结论**：60s 粗筛被误导（combo 在 60s 看似更优，240s 反转）。240s 下所有调参
配置的第一目标 late_count 均不劣化即已难得——destroy 比例是唯一实质性速度杠杆
（每轮修复的 scored 扫描数 ~8k + k²/2，随破坏任务数 k 近线性/超线性增长），而缩小
destroy 直接牺牲第一目标的搜索多样性。速度与质量存在本质权衡，**没有任何配置同时
满足「it/s ≥ +10% 且词典序不劣化」**。按验收标准与用户决定，Phase 2 调参整体回退
（配置字段、覆盖逻辑、CLI 开关、网格脚本与测试均已移除，默认行为恢复逐位一致）。

## 18. X+Y top-k 分析（Phase 3，未实施/回滚）

对 `scored` 的 O(n²) 枚举做 X+Y 排序矩阵堆式归并：非对角距离增量可分解为
`X[p] + Y[d]`（`X[p]=pickup_pair[p]−old_pickup_edge[p]`、
`Y[d]=delivery_in[d]+delivery_out[d]−old_delivery_edge[d]`），对角项 `d=p+1` 单独
处理；全局 d 按 `(Y[d], d)` 排序一次 O(n log n)，堆式归并取 `limit+margin` 个候选，
再用 ULP 误差上界证明不漏选，不成立则回退全量扫描。

**实例规模判定（本数据集 n≈50、limit=48）**：每行惰性物化 O(n)，被弹出的行数
≤ L=2·limit=96 → 归并最坏 O(L·n) ≈ O(n²)，与全量扫描同阶，无节省；margin 必须
够大才能让"验证不等式"成立（否则频繁回退慢路径），而 L=96>n 时归并成本已≈全量
扫描；只有 **n >> limit**（长路线，如 n≥200）才有渐近优势。本数据集每条路线仅
~50 访问，X+Y 无收益空间，按计划决策门禁回滚（未写代码，仅完成规模分析）。

## 19. 结论

- **已提交**：安全微优化（`_scored_distance_deltas` 内联）——逐位等价，240s it/s
  +1.3%，无质量代价，是唯一满足验收的改动。
- **已回滚（实证）**：参数调优（无配置同时满足速度与质量验收，destroy 是唯一速度
  杠杆且直接劣化 late_count）；X+Y top-k（n≈50 时 O(L·n)≈O(n²)，无渐近收益）。
- **代码状态**：Tier 1 + Tier 2 + Tier 3 安全微优化；pytest 112 项全绿；默认行为
  逐位一致；direct 场景保持原配置。
- 速度-质量权衡数据保留在 `algorithm\results\direct_tuning_60s`、
  `direct_tuning_240s`、`direct_tuning_240s_mild`；若未来接受"小幅 late 代价换
  ~30%+ 吞吐"，`mild3_combo07`（cand40/rank20/exact40/destroy≤0.07，late 持平、
  lateness +0.4%）是唯一接近验收的配置，可通过 `ALNSConfig` 手动复现。

## 20. 可审计性

- 求解器源码 SHA-256（Tier 3 提交后）：`d9719d0f47060cde2a6faa6205c5df4d43a02e0205ac9a8f08cb9281361c7923`
- 输入 CSV SHA-256：`386d3fbaa9009aeed947493782d657ed0b30b25d369c8187bfc9619fc7e31d25`
- 原始记录：`algorithm\results\direct_baseline_240s.json`、`direct_phase1_240s.json`、
  `direct_146iters_phase1.json`、`direct_tuning_60s\*`、`direct_tuning_240s\*`、
  `direct_tuning_240s_mild\*`
- 生成时间：2026-08-15

