# Adaptive Deadline / Rejection Pool 代码结构审查

## 审查范围与结论

本审查以 `codex/stronger-uav-neighborhoods` 的提交 `7964a2f` 为基线，开发分支为 `feature/adaptive-deadline-rejection-pool`。当前稳定 A2 是容量 2 的开放式 pickup-and-delivery ALNS Core：保留 `assignment_destroy` 与 deadline-risk guidance，关闭 VND、cluster repair、ejection 和 route pool。后续实现应在现有 ALNS 上增加独立 feature flags，不能修改官方评价、输入格式或公共求解结果接口。

最重要的结构约束是：`routes_score()` 只累加已有路线，不检查是否覆盖全部任务；官方 `evaluate_solution()` 则会把缺少任何完整取送记录的方案判为非法。因此 temporary rejection pool 只能是搜索迭代内的临时状态，pool 未清空前不能与完整的 current/best 解比较，也不能进入最终输出。

## 当前算法流程

1. `algorithm/src/uav_dispatch/io.py` 从 CSV 构造冻结的 `Task`；真实 deadline 保存在 `Task.deadline_min`，不得改写。
2. `algorithm/src/uav_dispatch/model.py` 的 `Problem` 建立任务、visit 与距离矩阵。visit 使用 `+task_id` 表示 pickup、`-task_id` 表示 delivery；官方 `Score(order=True)` 的字段顺序为 `(late_count, total_lateness_min, distance_km)`。
3. `algorithm/src/uav_dispatch/search.py` 定义 `Route = tuple[int, ...]`、`Routes = tuple[Route, ...]`。`RouteEvaluator` 按真实 deadline 缓存路线到达时间、逾期数、总逾期和距离；`RouteProfile`、`route_insertion_options()` 与 `insert_pair()` 保证容量和 pickup-before-delivery。
4. `construct_regret_initial()` 生成相同的 deterministic regret-2 初始完整解。
5. `solve_alns()` 在每轮通过自适应 roulette 选择 destroy 和 repair：完整 current 解经过完整任务对删除成为 partial routes，再由 repair 把所有 removed tasks 插回；之后才执行可选强化、模拟退火接受、best-so-far 更新和算子权重更新。
6. `algorithm/src/uav_dispatch/validation.py::evaluate_solution()` 独立重算完整路线约束和官方三层得分。它检查任务覆盖、重复取送、先取后送、容量、单机任务数及真实 deadline；最终输出必须通过该校验。
7. `algorithm/src/uav_dispatch/cli.py` 负责命令行入口和 JSON 序列化；`result_payload()` 明确输出官方目标顺序。

## A2 实现位置

- `algorithm/experiments/run_neighborhood_ablation.py::build_variants()`：A2 显式配置为 `enable_assignment_destroy=True`、`enable_deadline_risk=True`，其余 VND、cluster repair、ejection、route pool 均为 `False`。
- `algorithm/src/uav_dispatch/alns.py::solve_alns_core()`：公共 A2/Core 入口，在调用 `solve_alns()` 前强制关闭 ejection 和 route pool；`ALNSConfig` 默认关闭 VND、cluster repair，并启用 assignment destroy、deadline risk。
- `algorithm/src/uav_dispatch/alns.py::solve_alns()`：ALNS 主循环。算子池在循环前按 flags 构建；每轮依次执行 destroy、repair、可选强化、评分/接受、最优解更新和权重更新。
- 当前 `assignment_destroy` 替换 legacy `route_clear`，不是与其同时加入算子池。新增 destroy flag 时必须保留这一替换语义。

## Destroy、Repair 与约束位置

### Destroy operators

`algorithm/src/uav_dispatch/alns.py::DESTROY_OPERATORS` 注册名称，`destroy_solution()` 分派实现：

- `random`
- `worst_distance`
- `worst_lex`
- `spatial_related`
- `deadline_related`
- `late_critical`
- `route_segment`
- `capacity_conflict`
- `assignment_destroy`
- `route_clear`

`_remove_tasks()` 以 `abs(visit)` 同时删除 pickup 和 delivery，因此从合法路线删除完整任务对后仍保持容量与先取后送约束。

### Repair operators

`REPAIR_OPERATORS` 包含 `greedy`、`regret2`、`regret3`、`deadline`、`slack` 和可选 `cluster_regret`。普通入口是 `_repair()`；它通过 `_all_options_for_task()` 汇总各路线候选并调用 `route_insertion_options()`。现有 deadline-risk guidance 只改变 task shortlist/顺序，候选 delta 和最终接受仍使用真实 `Score`。

候选位置目前先按“距离增量 + 提前 delivery 位置”裁剪，再按真实 `Score` 排序。若只在 `_repair()` 最末端增加 risk-aware 排序，好的时限候选可能已被 `candidate_limit` 剪掉；新实现需要让时限候选参与裁剪配额，同时保持公开 `insert_task_best()` 的官方词典序行为不变。

## Deadline 与评价位置

- 原始 deadline：`algorithm/src/uav_dispatch/model.py::Task.deadline_min`。
- 可复用的任务服务时间估计：`Problem.direct_completion_min()`，即 depot 到 pickup 再到 delivery 的直接飞行时间。
- 搜索期真实到达/逾期：`algorithm/src/uav_dispatch/search.py::RouteEvaluator._evaluate_uncached()`。
- 官方真实到达/逾期：`algorithm/src/uav_dispatch/validation.py::evaluate_solution()`。
- 现有 guidance：`algorithm/src/uav_dispatch/alns.py::_deadline_risk()`，并用于 assignment destroy、late-critical destroy、repair 和部分局部搜索排序。

Soft deadline 必须由纯 helper 计算为 `deadline + beta * direct_completion_min(task_id)`，仅传入 destroy/repair/search guidance。不得写回 `Task`，不得进入 `RouteEvaluator.score` 或官方 validator。

## 新功能插入点

### 1. Late Risk Destroy

- 在 `DESTROY_OPERATORS` 注册 `late_risk_destroy`。
- 在 `ALNSConfig` 增加默认关闭的 `enable_late_risk_destroy` 及三个默认权重 `0.5/0.3/0.2`。
- 在 `solve_alns()` 的 destroy-pool predicate 中按 flag 加入该算子，并保留 assignment/route-clear 的互斥语义。
- 在 `destroy_solution()` 中计算每个任务的到达时间、真实逾期比例、deadline pressure，以及删除该完整任务对后的路线距离改善。
- 三个分量需要无量纲化后再按推荐权重组合；deadline 为 0 和非三角距离矩阵造成的负 detour 要显式处理。按需求选择确定性的 top-k 高风险任务，不使用随机 ranked sampling。

### 2. Temporary Rejection Pool

- 当前没有独立 Solution 对象；建议在 `alns.py` 增加私有冻结 `_SearchState(routes, rejected_tasks)`，保持公共 `SolverResult` 接口不变。
- pool 应在 destroy 后、repair 前形成。逐个尝试把完整任务对从 partial routes 移入 pool，只有删除前后 `(late_count, total_lateness)` 严格改善时才保留，容量为 `floor(0.1 * total_tasks)`。
- pool 中任务必须优先进入现有 regret repair。只有 pool 和本轮 removed buffer 都完全重插后，候选才可进入 VND/ejection、接受判断和 best 更新。
- 超时发生在 pool 未清空时应放弃该轮，保留上一个完整 best。metadata 记录 rejection attempts/events、peak pool size 和 `final_rejected_count=0`。

### 3. Soft Deadline 与内部 Search Score

- 在 `ALNSConfig` 增加 `enable_soft_deadline` 与 `soft_deadline_beta`，beta 支持 `0.15/0.20/0.30` 实验切换，并校验有限且非负。
- 单独新增 `SearchScore(order=True)`，字段为 `(late_count, weighted_lateness, distance_km)`，不修改官方 `Score`。
- deadline priority 由真实 deadline 的紧迫程度生成并保持有界、单调；weighted lateness 仍按真实 deadline 的迟到分钟计算。
- 内部 score 可用于候选引导与 current 探索接受；官方 `Score` 继续唯一负责 best-so-far 和最终结果，确保最终严格保持 `late_count > total_lateness > distance`。

### 4. Risk-aware Repair

- 给 `_repair()` 和 `_all_options_for_task()` 增加默认关闭的引导参数，保持已有调用兼容。
- 插入排序先比较 `delta late_count`，再比较 risk-aware cost；cost 可使用 `distance increase + lambda * weighted lateness increase`。这样不会让较短路径覆盖更多逾期订单。
- rejection pool 任务先于普通 removed tasks 进入同一 regret 框架；底层始终使用完整 pair insertion，因此继续保证 capacity=2 和 pickup-before-delivery。

## 实验运行入口

现有公平实验模板是 `algorithm/experiments/run_neighborhood_ablation.py`：它使用相同输入、同一 deterministic 初始解、固定 seeds、轮换方法执行顺序、相同总墙钟预算、源码/输入哈希、独立校验，并保存逐次路线 JSON、CSV 和汇总 JSON。

新增独立 runner `algorithm/experiments/run_adaptive_deadline_rejection.py`，不改历史实验。它应显式定义：

- baseline：A2；
- experiment1：A2 + late-risk destroy；
- experiment2：A2 + rejection pool；
- experiment3：A2 + soft deadline；
- experiment4：A2 + 三者组合。

正式配置使用相同官方 CSV、seeds `2026080500..2026080502`、每次 240 秒总预算、相同初始路线和评价器；结果写入 `results/adaptive_deadline_rejection/`，包含每 seed 的完整 route JSON、逐运行 CSV、汇总 CSV 和总 JSON。开发阶段可用短迭代/短墙钟 smoke 实验验证脚本，不能把其结果当作正式结论。

## 计划修改文件列表

- `algorithm/src/uav_dispatch/alns.py`：flags、late-risk destroy、临时搜索状态、rejection drain、soft deadline、内部 score、risk-aware repair、metadata。
- `algorithm/src/uav_dispatch/search.py`：如有必要，扩展不影响官方 score 的插入候选引导数据；保留公共插入 API 语义。
- `algorithm/tests/test_alns_neighborhoods.py`：逐个 feature 的行为和关闭复现测试。
- `algorithm/tests/test_adaptive_deadline_rejection.py`：新状态不变量、内部/官方评分隔离、pool 上限及最终清空、soft deadline beta、组合回归。
- `algorithm/experiments/run_adaptive_deadline_rejection.py`：五方法、三 seed、240 秒实验入口与可复现输出。
- `algorithm/reports/adaptive_deadline_rejection_report.md`：正式结果与五个结论问题。
- `results/adaptive_deadline_rejection/`：路线、CSV、JSON 结果。
- `CHANGELOG_adaptive_deadline_rejection.md`：改动、算法思想、实验结果与合并建议。

`algorithm/src/uav_dispatch/model.py`、`algorithm/src/uav_dispatch/validation.py`、输入文件和历史实验/结果原则上不修改，以隔离官方 deadline 与评价逻辑。

## 风险分析

1. **非法部分解被误判为优秀。** 缺任务会机械性减少 `routes_score()` 的 late count。缓解：pool 未清空时禁止接受/best 更新；完整性只通过独立 validator 判定。
2. **最终存在拒绝订单。** 官方接口不允许缺单。缓解：repair 优先 drain pool，超时丢弃未完成迭代，最终断言 pool 为空并记录 metadata。
3. **破坏最终目标优先级。** weighted lateness/soft deadline 若进入官方 `Score` 会改变比赛评价。缓解：独立内部 score；best 和最终输出始终使用真实 `(late_count, total_lateness, distance)`。
4. **风险分量量纲失衡。** 原始 deadline 和 detour km 直接相加会让某一项主导。缓解：任务集合内归一化、零 deadline 防护、detour 比例化与 clamp。
5. **候选剪枝错过低逾期插入。** 现有 candidate pruning 偏距离。缓解：保留一部分 risk-aware/early-delivery 候选，并用测试验证同等 late count 下的排序。
6. **性能回退。** 每任务删除后重评和 weighted-lateness 计算会降低 240 秒内迭代数。缓解：复用 `RouteEvaluator` 缓存、只在开启 flag 时计算、记录 iterations/runtime，并以相同墙钟配对比较。
7. **新算子与 assignment destroy 高度重叠。** 两者都含逾期和绕行信号，新增算子可能只稀释 roulette。缓解：单独实验 experiment1，并记录算子使用次数/权重。
8. **小实例上限取整。** `10% * N` 必须向下取整；不能用 `max(1, ...)`，否则小于 10 个任务时违反上限。
9. **默认值漂移污染 baseline。** 新 flags 默认关闭，实验 runner 显式传入全部旧、新 flags，避免未来默认值变化改变 A2 定义。
10. **结果统计误读。** 三个 seeds 只能提供有限配对证据。报告需同时展示逐 seed 结果、均值和 lexicographic 胜负，不能用跨指标加权平均宣称提升。
