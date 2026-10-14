# 六任务接力结构锁定、其余任务联合修复：设计 v1

2026-10-15。仅只读设计；未实现新求解模块、未运行新优化器。026/027 及更早源码、证据、审计均保持冻结。

## 问题与本轮单一假设

027 在同一强 DIRECT 输入上完整遍历 256 个原站六任务 family，每臂各保存 512 个 canonical 完整提案，独立审计通过。DIRECT/RELAY 实际用时约 11.413/8.662 秒，均未采用新方案。输入为 `N=59,L=1790.632128866,D=578.534684859,J=0.434215788788`；最好中继原始宏为 `N=59,L=1814.298546570,D=583.956063443,J=0.437288227355`。

新假设是：保持六单真实同站跨机重新拼单的身份，让其余 194 单重新分配和排序，修复原始宏造成的 native 路线延误。原始提案暂差只决定能否进入后处理，不决定最终采用；终局仍须严格胜过原强输入。现有数据没有证明该假设会产生收益。

## 最少新增文件与接口

1. `algorithm/experiments/six_task_preserving_polish.py`：以冻结的 `relay_pair_preserving_polish.py` 为参考，保留真实加权 ALNS 的五个 process-local hook、balanced destroy、全局 exact repair、完整方案回退及截止时间检查；将 pin 数量改为六，并新增三机六单形状检查。不得调用旧 `_shape`，它只支持两单/单 receiver。
2. `algorithm/experiments/six_task_with_pinned_polish.py`：调用冻结 `expanded_six_task_sorting.refine` 得到原始提案，然后选本臂最优少量完整宏调用新 helper。复用原四任务 facade 的预算和 guard **政策**，不把旧四任务函数直接做别名替换：旧 facade 硬编码 `i/j/k1/k2`、`_Group`、raw/refine 参数及单 receiver，接口不适配六任务。
3. `algorithm/experiments/audit_six_task_pinned_polish.py`：标准库独立重放，先审真实 `base→raw`，再审 `raw→post`；不导入新 helper/ALNS，不合成假 base。各有小型测试文件，旧测试及审计不改。

建议 helper API 沿用已审计的参数通道：

```python
polish_pinned_macro(problem, macro_routes, *, deadline, weights, scales, seed,
                   pinned_task_ids, required_cross_task_ids=(),
                   comparison_key, proposal_margin=.015,
                   config=None, alns_module, snapshot_limit=8)
```

`pinned_task_ids` 必须恰为 raw family 的六个原任务。RELAY 的 `required_cross_task_ids` 恰为同六项；DIRECT 必须为空。返回 `{routes,evaluation,iterations,metadata}`，固定模型不增加资源。helper 如为沿用内部契约额外返回 `problem`，facade 必须检查 `is original_problem`，最终 facade 不返回动态 `problem` 键。

facade API：

```python
refine(problem, base_routes, *, deadline, weights, scales, seed,
       relay_enabled=True, alns_module=None, polish_config=None,
       proposal_fraction=.4, candidate_limit=2, proposal_margin=.015,
       guard_reserve_seconds=.03, guard_reserve_fraction=.05,
       max_stations=4, families_per_station=64, source_pair_pool_limit=4,
       in_candidates=2, in_gap_limit=4, out_gap_limit=4, direct_gap_limit=4,
       full_candidates_per_family=2,
       polish_initial_temperature=.0025,
       polish_minimum_temperature=.00001,
       polish_cooling_rate=.97, polish_max_iterations=1_000_000)
```

以上是下一开发协议的建议参数，正式运行前仍须冻结实际参数/seed/source manifest。ROOT 提议下一轮每臂总预算 40 秒、raw 比例 .4：名义约 16 秒覆盖 raw，剩余约 24 秒均分最多两次 polish。027 的耗时只提供选择这一开发预算的依据，不能保证另一运行一定完成所有 family。所有 selector、raw 表、完整重验、排序及 metadata/guard 开销均计入总预算，不能把 raw 另跑后免费传入。

## 精确锁定的内容

冻结 helper 的 `_signature` 已能表示任意 pin 集合，可沿用其含义：六项原任务的 DIRECT/RELAY 模式、完整腿 ID、站点、IN 源机、OUT/最终责任机、每条路线中过滤出的六任务 signed-event 子序列，以及全部飞机的最终任务数必须与真实 raw 宏相同。

官方每机最终任务数仍为 25，总任务 200、8 机、C2、速度 .9、homes、4 个站、注册腿、原始截止期及 J 权重尺度均不变。每个 complete repair 都需重新验证完整覆盖、唯一完整运输方案、载荷、全局 DAG、全部任务 N/L/D 和等待。不能仅验证六项或用局部 score delta 替代含交接等待的全局评分。

RELAY 新 `_shape` 对当前完整路线重新推导并检查：

- 只有指定六项是 RELAY，六项均恰好单跳且 IN/OUT 不同机；其余 194 项全部 whole DIRECT。
- 原三个源机及原三个接收机身份相同，各仍 IN 两项、OUT 两项，全部用同一个原固定 H；每个接收对来自另外两个不同源机。
- 每机自身两项 IN drop 均在自身最早 OUT take 之前，不能用“DAG 恰好未成环”替代此结构检查。
- 三个 receiver 的两个 OUT 正事件在**当前完整路线**中相邻，进入两次取货前为空载，载荷前沿为 `[0,1]`、后沿 `[1,2]`。
- 每个接收对均有真实共同载货航程 `>1e-10 km`，从当前实际路段计算。

DIRECT 同样只 pin 自己 raw 宏所选的六项完整 DIRECT 计划、最终责任及 signed-event 子序列，保各机最终任务数。DIRECT 仍独立从 90 种原始 final map 的有限表中选自己的 raw 宏，不能套用中继的八种 final map。两臂 eligible 普通任务均是 `all_task_ids−six_pins`，官方恰为 194 项。

## 后处理允许的自由度

raw 阶段继续通过冻结 `audit_six_task_sorting_proposal`，要求原设计中的 IN/OUT 四事件块、原 native 机和顺序。post 阶段不再要求这些完整四事件块连续，也不保持原 native 责任或相对顺序：

- native 可在容量允许时插到两次 IN pickup 之间、IN 事件之间及跨块 gap；第一项 IN pickup 前不额外要求空机。
- 两次 OUT take 必须仍相邻且空机进入；第一件已交付后，native 可在第一/第二次 OUT delivery 之间安排合法任务。
- native 可以跨 8 机重新分配，但 exact 最终任务数不变。

不能把 post 丢给旧六任务 raw 审计直接审，也不能保持 raw 的四事件块约束后宣称已经允许全部普通任务共同修复。selected 子序列和显式形状检查共同保证接力没有被悄悄取消。

两个可手算的局部插入例说明这些自由度有实质意义（仅为局部航段示意，正式测试仍需补齐完整六单与全局交接）：

- IN：home=0，两个 selected pickup 在 0/2，H=3，native 的 pickup=delivery=1。原先收两件、到 H 卸两件后才送 native，native 时刻为 5、开路线 D=5；插为 `+IN_i,+native,-native,+IN_j,-IN_i,-IN_j` 后，native 时刻为 1、两项 IN drop 仍为 3，D=3。selected 子序列相同，最大载荷仍为 2。
- OUT：H=0，两 selected delivery 在 1/3，native pickup=delivery=2。原先两件交完才送 native，其时刻为 4、D=4；改为 `+OUT_i,+OUT_j,-OUT_i,+native,-native,-OUT_j`，native 时刻为 2、第二项 selected delivery 仍为 3，D=3。双 take 保持连续空机进入，selected 共载正航段仍为 H→1。

## 真正的全局时钟及表分数的边界

新 helper 只允许 ordinary DIRECT 生成，精确插入通过当前全局 DAG `evaluate_delta` 计分，完整叶节点再 full check；非法、缺单、配额变化、pin 或 shape 破坏的候选回滚且不获得 SA/operator 接受奖励。沿用 actual worker ALNS module 的 globals 身份校验；冷温度四字段用其实际 `ALNSConfig`，旧 helper 的禁用项、实际 deadline 与 objective adapter 恢复约束不变。

post 的 IN drop 时刻、等待、缓存、交付时刻和 shared distances 均从 post 重新计算。即使 source IN 的两次 drop 因 native 插入变成不同时间，也只使用真实当前时钟；不得复制 raw availability。own IN 全部先于 own OUT、且其余任务 DIRECT，仍允许独立验证 IN prefix 不依赖 OUT 等待，但不再用原有限路线表对 post 评分。

raw `table_score` 只属于 `full_proposal` 的冻结有限表证据。post 新增 `post_global_score`，不得带有声称是当前分离表得分的旧 `table_score`。固定站允许原模型的异步缓存；本版本不施加移动会合的 receiver-first/零缓存条件，也不引入免费预部署或同步会合资源。

## raw、polish、采用与元数据

原强输入始终作为最终 guard。raw 返回的更优合法方案须在总截止前 full 重验并保留，不能因后处理失败而丢失。对 raw 完整提案去重、重验真实加权 key，按 `(J,N,L,D,routes)` 排序，最多两项且 `J<=input_J+.015` 进入后处理；margin 不是采用规则。每臂选择自己的 raw 宏，两个宏的任务身份不保证逐项匹配。

selector 的全部 records/hash/completion/timestamp 和 raw 完整快照保持原样。两臂相同 selector/参数/hash 是比较成立的必要条件，但不要求因剩余时间不同而实际 polish 相同宏。记录各臂 actual family 数、raw 是否超时、选择的两个 family/map 及真实 polish 工作；ALNS 因 deadline 未返回 summary 时，不能把 `iterations=0` 解释成没执行，需保 destroy/repair/complete-valid 等计数。

每 leaf 将剩余时间按待处理候选数均分；预留 `min(.03s,5% leaf)` 做 facade full/six-shape/current metadata 验证。所有 full、shape、原 key 严格改善和采用 timestamp 都须早于 leaf 与总 deadline。helper 返回合法但更差宏、初始宏、未验证 checkpoint、换 Problem 或 deadline 后 checkpoint，均不能替代原 guard。

建议保留如下证据层次：

```text
raw_stage: 冻结 expanded-six 原始 metadata/selector/所有 raw full snapshots
polish_records[]:
    full_proposal / macro_routes / original_macro_move
    helper_metadata（每个完整 checkpoint 都可独立绑定 raw 宏）
    post_polish（当前 routes/score/key/event/delivery/drop 时钟）
    pinned_task_ids（六项）/pin_scope='six_task_sorting'
    actual_final_owners（全部 task string→0-based aircraft）
    current_source_drop_availability_min/current_shared_double_loaded_km_by_aircraft
    leaf/helper/verified/adopted timestamps、拒绝理由
accepted_moves[]: kind='raw_macro'或'six_pinned_polish'，绑定完整 snapshot 与原 macro
```

所有 post helper 快照和最终采用结果都要绑定 `original_macro_move`。其中 family/selected final map 因六项锁定仍可作为身份描述，但 raw availability/shared/table 值属于历史；post 的当前物理值使用明确的新字段，不在一个 `move` 中混合历史时钟与当前事实。raw 快照及其旧 move 完全不改。

raw selector patch 必须在 raw 调用返回时恢复；polish 只 patch 实际 ALNS 的五个函数。新 facade 捕获并核验原 selector、五函数、Score 比较器及 adapter active/coefficients；单一 `_active` 防嵌套，异常和超时均 finally 恢复。无需把新六任务方案嫁接到旧四任务 `_Group`/`i,j,k1,k2` 别名系统。

## 新独立审计与最低测试

建议独立 API：

```python
audit_six_task_pinned_polish_proposal(
    tasks, topology, base_direct_routes, macro_routes,
    polished_routes, original_macro_move, *, relay_enabled,
    telemetry=None, weights=None, scales=None)
```

第一步对真实 `base_direct_routes→macro_routes` 调用冻结旧六任务 raw 审计，确认原六任务来源、所选 final map、station/legs、未选事件不变、真实全局分数。第二步独立重放当前 `polished_routes`：只对六项比较完整计划/角色/腿/站/selected 子序列和原 quota；放开其他 194 项 owner/order，但要求 DIRECT、全覆盖/C2/K/DAG 及新六形状。独立重算 all-event clocks/全部交付分数/当前 drop availability/三组 shared distances；不能用 post 合成假 base，不能拿 raw `table_score` 证明 post 正确。

最低有意义测试包括：

1. 真 weighted runtime 两臂小循环，helper 内实际配置与 cold 四参数一致；普通任务确实进入 destroy/repair，所有五函数/Score/adapter/selector 恢复。
2. 至少九任务三机的 witness：六项固定，普通任务 owner 或顺序确实改变仍保持 exact quota；只有六任务的三角形例没有普通任务，不能据此声称已测试194修复机制。
3. native 合法穿插 IN 事件或 OUT 两次 delivery 之间可通过 post 审计；同结果按 raw native-fixed/四事件块审计应不能充当原始提案。
4. 删除/转 DIRECT 某一中继、换 source/finalowner/H/leg、调换 selected 顺序、拆开双 take、非空进入、共同飞行变零、own OUT 提前、增加第七中继、cycle、缺单/超载/配额改变均拒绝或回退。
5. 带真实异步 wait/cache 和 native 截止期样例，post 时钟全部独立重放；篡改当前 availability、shared、clock、J 或把 raw table 值复制为 post 当前值均被识别。
6. raw/leaf/总截止边界、helper 返回稍差宏或超时合法宏，最终保留已核验的原输入/更优 raw，不能错误采用。

只有新源码、上述小测、独立审计和正式协议全部冻结后，才进入下一次短诊断。该设计不构成官方收益证明，也不改变原题条件或要求结果必须对中继有利。
