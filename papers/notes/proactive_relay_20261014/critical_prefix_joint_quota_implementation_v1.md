# 真实因果前缀与动态责任槽修复：实现合同 v1

这是 `critical_prefix_joint_quota_repair_design_v1.md` 的新组件实现记录。它不更改旧实验、资源、目标或输入，不代表官方实例已取得改善。运行协议、未来种子与预算由独立新 manifest 绑定；本合同不沿用旧实验种子作结果相关重跑。

## 模块与证据边界

`algorithm/experiments/critical_prefix_closure_v1.py` 提供真实任务子集的物理预览、两条接力计划的身份约束和有限闭包。`critical_prefix_joint_repair_v1.py` 负责在状态内同时选择普通任务的新责任机与插入位置；两者不修改旧 ALNS，也不使用旧四张预先固定的责任分配表。

闭包输入 `base_routes` 是实际强 DIRECT 完整方案，`seed_routes` 是实际完整种子，二者最终责任计数必须逐机相同。RELAY 种子的原始同责任免补偿构造证明由 wrapper/独立审计绑定；闭包不伪造原始责任。DIRECT 的 pins 为空，所有任务均可成为待修复普通任务，focus 仅决定排名目标。历史 family 的 donor 不是本轮补偿任务。

本轮新算法的范围是有界闭包内的动态责任与位置联合搜索，不是一次重排全部任务的全域优化。官方规模下 RELAY 有 198 个可选普通 whole-DIRECT 任务，DIRECT 有 200 个；有限 cap 只取其中一部分。旧 025 已允许 198 个普通任务换责任/改事件序；本轮的差别是明确加入零损伤因果前缀，并在修复状态中分支责任与插入位置。旧实现已有 regret 策略，不能把复用这个名称当创新。

## 闭包 API

```python
build_closure(problem, base_routes, seed_routes, *,
    pinned_task_ids=(), focus_task_ids=(), expected_signature=None,
    closure_limit=12, variant=0, deadline,
    partner_depth=1, critical_native_limit=2, statistics=None)
```

`deadline` 是绝对 `perf_counter()` 截止时刻；`None` 只供不计时的小型检查。`Deadline` 直接别名到冻结 `deadline_window_costs.Deadline`，因此 caller 使用同一异常类。入口、真实全局评分前后、候选循环与发表返回前检查截止。若中途过期，抛出异常，不发表看似完成的前缀目录；可变 statistics 留下 `completed=False, deadline_reached=True`。所有这些工作都由 caller 的共同 wall 支付。

`partner_depth` 本版本只接受整数 1，表示真实原方案/种子正距离共载图的一跳候选；不暴露会被忽略的递归深度。`variant` 仅轮换有限头部代表。`critical_native_limit` 默认 2，范围 0–8，用种子当前实际交付与 due 的绝对差选额外普通 delivery 排名目标；它不锁这些任务，也不改变目标函数。未来协议须记录这三个参数。

成功返回 JSON 友好对象，主要字段如下：

| 字段 | 含义 |
| --- | --- |
| `removed_task_ids` | 所有 beam 状态共同的恢复任务次序，未指定新 owner |
| `partial_routes` | 对实际种子整单删除后的精确 signed-event 子序列 |
| `quota_slots_by_aircraft`, `remaining_quota_slots` | 逐机实际删除 whole-DIRECT 责任数，两字段相同 |
| `raw_final_counts`, `pin_final_counts`, `remaining_native_final_counts` | 可重建 `raw = pin + remaining native + q` |
| `actual_opened_aircraft`, `unopened_aircraft_reasons` | 真正释放了槽的飞机及未释放原因 |
| `eligible_task_ids`, `omitted_task_ids` | 完整可选集与未选差集，不把未选说成被证明无效 |
| `targets`, `per_task_evidence` | 真实时钟、完整因果祖先与所有 eligible 单任务删除证据 |
| `donor_reservations`, `selection_events` | donor 先行与后续有界扩展的实际选择记录 |
| `prefix_direction_representatives` | 每个时钟方向的代表、是否存在正单删收益及是否纳入 cap |
| `partial_preview` | 精确 present-task 集合上的 DAG 预览，始终 `complete=False` |

先为每个拥有 eligible 任务的飞机选一个真实 donor，才使用剩余 cap 扩展方向代表、新迟到、正延误增长/长后缀、原/新共载伙伴、交付时刻增长。cap 小于可开飞机数时只开真实可容纳的飞机并记录原因，不能称“全八机已开”。优先 donor 同样可以是零损伤前缀，不需要先制造延误。每个方向的代表保留受 cap 约束，未保留也要如实记录。无有意义扩展候选时可以不填满 cap。

## 目标与真实单删预览

RELAY 的目标包括各自真实 `in_drop:i`、`in_drop:j`、接收机首次 OUT 前的 `receiver_arrival`，以及 selected 的真实最终 delivery。两个 source 到站方向分别计算，不能被 `max(a_i,a_j,c)` 遮蔽后删除。DIRECT 只采用当前真实 owner 路线上的 delivery 目标；不得捏造去 H 的时刻或限制原 A/B/C 分工。额外普通任务目标采用同一真实 delivery 策略。

target 的 `causal_prefix_task_ids` 是实际 DAG 的严格祖先中 eligible 的任务集合：沿路线前驱反向走，遇 OUT pickup 再走对应 IN drop 依赖。接收机到站目标在 first take 之前截止，不包含该 take 的源到站依赖。祖先只是可追溯候选集，含目标任务自身的 pickup 时，删除整任务会使 delivery 目标不存在。

成功闭包对全部 eligible whole-DIRECT 任务分别真实删除并重放 present DAG。对目标 \(x\) 记录 signed 排名量

\[
\Delta_x(t)=\tau_x(\text{seed})-\tau_x(\text{seed}\setminus t).
\]

它可以为正、零或负，只是删除预览的因果证据，不是恢复后可达收益、完整 J 下界或全域筛除条件。删除 delivery 目标本身时 after 与 delta 均为 `None`，列入 `undefined_targets`，不填 0 或声称救回。该任务仍可被 DIRECT 闭包选中。方向候选排名排除这一未定义效应，但保留完整真实祖先目录。

每行还保存 base/seed 真实 delivery、按官方 `(clock-due)>1e-9` 计算的整项 lateness、差分、新迟到、真实共载公里数、实际 present/count/q 与是否被选。不能把 source 原 native 时钟无变化当作其不控制到站的证据，也不能把这些差分相加代替联合完成方案。

## 两 pin 结构与 partial API

```python
signature(problem, routes, pinned_task_ids=(), *, deadline=None)
shape(problem, routes, pinned_task_ids=(), *, deadline=None)
partial_preview(problem, routes, *, pinned_task_ids=(), expected_signature,
    remaining_task_ids, deadline, remaining_quota_slots=None,
    global_evaluator=None, statistics=None)
preview_evidence(preview)
```

RELAY pins 恰为两条真实跨机任务：不同 source、第三架同一接收机、原 H 与完整 leg/mode/role 不变。每条路线上的 pin signed-event 子序列固定；接收机连续两个正 OUT 开始前为空载，且两件共同持有飞过正距离 `>1e-10 km`。不锁原 OUT 四事件为连续块，允许合法普通任务插在 IN 之间或两条最终 delivery 之间。所有未 pin 任务始终 whole DIRECT，不创建额外 relay。DIRECT pins 必须为空，无假 H、无分工锁。

`signature` 保存完整 pin plans/leg IDs/site/source/final owner、逐路线 selected-event 子序列，以及真实完整逐机 final-count 向量。partial 检查身份部分保持，计数则从当前真实计划重新计算：

\[
q_a=K_a^{\rm seed}-n_a^{\rm present}\ge0,
\quad\sum_a q_a=|U|.
\]

其中 \(U\) 是精确缺失任务集合，所有其余任务均具有完整合法计划。cap 是题目实际 `max_tasks_per_drone`，不是另增资源。不能用缺失 task 的责任作虚假 credit，也不能先给 task 固定 owner 再把路线搜索叫动态分配。

partial 内部调用 `GlobalRelayEvaluator.evaluate(routes)` 重算真实 present DAG；可传同一 problem 身份的 evaluator 复用内部已绑定 routes 缓存，不接受 caller 自报 schedule 当证书。容量、完整 pickup/drop 配对、pin 结构、精确 coverage、实际责任计数与时钟均检查。返回 `schedule/global_schedule/score/index` 是运行时对象；`preview_evidence` 输出可序列化所有实际 event/delivery 时刻与分数。

即使 `remaining_task_ids=[]`，该接口仍明确 `complete=False, standard_full_validator_used=False`。它只证明当前物理 DAG，与是否覆盖全部任务分开；完整 leaf 需要标准完整校验和独立审计。全 task 评分与缺 task 评分不能直接比较作采用或剪枝。

## 动态 owner/位置修复与最终采用

constructor 的 repair 对同一恢复任务序每两项建一个完整层，枚举两项插入顺序，在每个状态实际剩余 `q_a>0` 的所有 owner 内分支，再提出合法 whole-DIRECT 位置。几何/早取/早交位置前沿仅是有限提案，不是 J 下界；恢复第一个任务后不能因其临时 J 较差而提前剪掉第二个任务协作机会。两项均完整恢复后才在相同 present 集合内按真实全局 score 比较 beam。不同 parent 的状态也必须具有同一 task coverage。

每个实际候选调用真实全局 DAG delta 评分，保留状态再由 partial API 核当前 routes/coverage/q；跨路线 owner 周期由分支自然形成，不依赖旧预分配四 maps。单次闭包只覆盖所选任务，记录真实位置/owner 分支与遗漏，不宣称所有合法位置穷尽。

完整 leaf 必须恢复全部任务、q 全 0、所有 original homes/4H/fleet/C/K/legs 不变，通过标准全方案校验，核当前全局 score 与所有时钟，且完整证据/快照生成和采用均早于实际 deadline。超时只返回之前完整检查过的 checkpoint；未完 beam/目录或仅完成部分验证不得采用。最终采用需严格改善原强 DIRECT guard 的冻结加权 J，不因比较种子更好就宣称胜过 DIRECT。

入口或初始化途中到期是单独路径：repair 返回 `evaluation=None`、input/post snapshot 为 `None`、`returned_checkpoint_checked_within_deadline=False`。返回的输入 routes 只是身份回传，不是本 leaf 新检查过的候选，wrapper 不得再把它当 repair 成果采用。wrapper 保留原强 guard 或此前已经在总期限内独立完整检查过的 raw/checkpoint。只有初始化和快照都真实在期限内完成，后续搜索到期才可以回退该已有完整 checkpoint；wrapper 还须绑定其 routes/score/摘要及验证时间，不能单信 bool 标志。

RELAY 后验时钟、source availability、shared flight 和 native 分类必须从当前 routes 重算，不能复用 raw future-clock/table。原始 seed 构造证明与 current native-free 后验形状分别保存。DIRECT 使用空 pins、同等有界修复自由和总 wall，并可把 fresh 端到端预算完全用于自身构造/修复；若共同支付 R catalogue 只作匹配机制诊断，必须标明它不是最强端到端 DIRECT 基线。raw 目录构造、闭包所有单删预览、修复、完整验证与证据生成均计费；旧 raw 构造约 28 秒不能假定在 12 秒内完成。

## 定向验证范围

小型测试覆盖真实 9-task 等待、零损伤 source 前缀、较慢 source 的 max 遮蔽、独立 C 到站方向、实际 DAG 祖先、DIRECT 目标删除未定义/不隐 pin、真实跨机 q 消耗、8 机其他五机 donor、小 cap、错误 coverage/半计划/超容量/假 q/pin 改动、错误 problem evaluator 与入口/预览中途过期。另以生产 full validator 证明 partial 因缺 task 不完整，而 present DAG 仍可单独物理成立。独立审计从真实 base/seed 整单删除重建每行，不导入闭包求答案。

这些测试不含官方 200-task 优化、运行时探测或性能结论；后续正式 run 只有完成新源码冻结、共同预算协议和独立审计绑定后才可启动。
