# 条件 donor 与完整回补结清：实现合同 v1

本合同落实冻结设计 `conditional_donor_completion_repair_design_v1.md`，不更改035源码、轨迹或结论。它记录036的新有限目录与真实完整回补接口，不代表已经改善官方实例。pair/cycle并非新原理：新增执行点是以真实因果任务t的具体去向b选择当前donor d，并由同一完整步骤支付d的回补，而非先给固定闭包留下未来责任债。

## 目录 API 与当前父方案

`algorithm/experiments/conditional_donor_catalogue_v1.py` 暴露：

```python
propose_catalogue(problem, base_routes, current_routes, *,
    pinned_task_ids=(), focus_task_ids=(), expected_signature=None,
    deadline, weights, scales, critical_limit=6, donor_limit=3,
    statistics=None) -> dict
```

`base_routes` 是实际强DIRECT完整输入，`current_routes` 是本步实际完整父状态；后续步不沿用旧owner。RELAY只pin两条实际跨机计划，普通198项全部eligible；DIRECT pins为空，200项均eligible。原K/C2/homes/四H/速度/任务与目标不变。pins、完整真实责任计数和父状态必须通过冻结035检查；任意伪造expected signature或缺任务父状态会拒绝。

目录调用冻结 `critical_prefix_closure_v1.build_closure(...closure_limit=0, partner_depth=1, critical_native_limit=2)`，取得所有eligible任务的真实单whole删除效果。其 `source_preview` 原样保存，不用它的空removed集合限制本轮候选。每个after clock来自实际present DAG，不是独立局部delta；delivery目标本身被整单删除时after/delta为None，不能填0或声称救回。DIRECT目标仅实际delivery，不构造H/源到站。

## critical6：真实方向代表在全局补齐之前

按 `source_preview.targets` 的实际顺序遍历每个方向：RELAY先两个INdrop、C第一次OUT之前到站，再selected及额外临界delivery；DIRECT只有实际delivery。每方向仅考虑真实causal prefix中效果可定义的任务，先按signed clock improvement降序，再以035全局rank打破平局。代表去重，在cap6内先保留，再按全局rank补满。

全局rank仍是最大正目标提前、正提前和、newlate、正ΔL、正交付延误、ID。它是候选排名，不是J或可达收益。不能把单删效果相加，也不要求代表必有正gain；无可定义候选的方向如实记空。小cap排掉方向时 `omitted_reason='critical_limit'`，空方向为 `no_defined_causal_candidate`，已保留为None。critical目录记录全eligible全局排名和未选集合，因此不能把未选解释为无优化潜力。

当前精确policy字符串为 `defined_causal_target_direction_heads_then_actual_gain_damage_delay_ID_fill`。它在正式运行前固定，防止6项全被一个已知source方向占据；不增加新的beam机制。

## 每个(t,a,b)的实际条件donor

对每个critical t，以其当前owner a为准，枚举所有其它实际飞机b。d来自b当前全部非pin whole-DIRECT任务，允许在035旧U之外。每个d整单删除后，两条实际路线分别为 `route_a \ t` 与 `route_b \ d`，真实释放a、b各一项最终责任。record保存q，但不伪装这些临时缺两项路线为完整方案。

为了有限排序，每方向在删后路线扫描所有可承载一个新增任务的**连续whole P/D块**gap。实际C2下原gap load只能0或1；接收机两个pin OUT之间的gap排除，以免代理块直接拆开必须连续的take。source可原已持1件；尾gap按开放路线处理。这里只构造几何/无等待代理，后续repair仍可提出更多合法非连续位置。

设要在当前gap插x，其前点为u、后点为v（尾gap无v），则块几何增量为

\[
\delta_D=uP_x+P_xD_x+D_xv-uv,
\]

尾gap去掉最后两项。无等待交付代理为该删后路线从真实home累计到u的航程/V，加 `uP_x/V` 和 `P_xD_x/V`。它明确忽略所有跨机等待，不是实际DAG clock或安全时间下界声明。每方向保存最小几何与最早无等待交付两个代表；两方向相互独立，只用于筛选donor，不能相加作为真实J。

对donor d保存三项条件排名量：

- 两条删后路线总几何航程减原a/b两路线总航程，再加两方向各自最小块增量；字段 `geometric_net_distance_proxy_km`。
- t插入b的最早无等待交付，字段 `critical_early_delivery_proxy_min`。
- d回a的最早无等待交付，字段 `donor_return_early_delivery_proxy_min`。

`pair_geometry` 排名依次用第一项、第二项、第三项、ID；`critical_early_no_wait` 用第二项、第一项、ID；`donor_return_early_no_wait` 用第三项、第一项、ID。三通道依此顺序各取首项、去重至cap3，剩余槽按geometry排名补齐。默认3通道可同时保留，cap1/2会透明限制后续通道。相同几何/估时仍可有ID平局，但不再仅因对源时刻零gain而固定低ID donor。

这些是有限连续位置代理。所有b当前donor都被该代理评分，不代表所有合法修复位置或全局J都被评分；代理未保留不叫不可行、支配或下界。目录不接受用独立delta和替代后续联合完整评分。

## JSON字段与缓存/期限

顶层保存 `records`、`donor_directories`、`critical_task_ids`、`critical_task_ranking`、`critical_omitted_task_ids`、`critical_evidence`、`critical_direction_representatives`、完整 `source_preview`、父routes SHA、真实pins/focus/expected signature、配置和统计。

每个donor directory保存t/a/b、全部eligible/retained/omitted donor ID、三个完整排名和每个donor的两个surface。surface保存实际aircraft/插入task/删后路线SHA、capacity-legal gap数、被排除的pin take内部gap、原home累计真实几何航程、两个代表的gap/几何增量/无等待交付代理。完整路线可由实际父状态和t/d唯一重建，不为每个record重复保存全fleet。

record固定字段为 `record_index, donor_directory_index, t, a, b, donor_task_id, task_ids, parent_routes_sha256, target_owner_by_task, removal_quota_slots_by_aircraft, removed_task_ids, retained_by_channels, critical_rank_index, critical_target_ids, critical_target_clock_improvements_min, geometric_net_distance_proxy_km, proposal_sha256`。目标owner是t→b、d→a；`critical_rank_index`指全局rank位置，不代表方向保留次序。`full_joint_score=None, complete=False, complete_solution=False, global_objective_bound=False`始终明确。

缓存只在一次目录调用内使用完整 `(aircraft,删后route tuple,插入task)` key，problem/pins不变；FIFO上限4096，命中前也检查deadline。没有跨父状态缓存，更不复用旧owner/等待结果。统计区分实际surface计算、命中、proxy gap评分、donor评分与省略数。

`deadline` 为绝对perf_counter，异常直接复用035/冻结costs的同一个 `Deadline`。源全preview、各层循环、hash、surface、排序/返回之间检查时限；最终记录 `completed_at_perf_counter` 后再检查。入口或中途过期均抛异常且statistics记completedFalse，不发表不完整目录。完成目录仍不是完整修复checkpoint。所有这些开销计入共同wall。

## 已实现的完整步骤与证据责任

Constructor实现的 `algorithm/experiments/conditional_donor_repair_v1.py` 实际API为：

```python
refine(problem, base_routes, raw_routes, *, seed_move=None,
    focus_task_ids=(), deadline, weights, scales, seed=2026101521,
    relay_enabled=True, critical_limit=6, donor_limit=3,
    position_limit=12, option_count=3, beam_width=4,
    max_steps=2, snapshot_limit=6)
```

scope为 `conditional_donor_completion_repair`。它对每个当前完整parent生成本目录，重新从真实父routes删t/d并核actual q，然后固定t→b、d→a、两种插入顺序分别提出有限位置。实际采用冻结035的合法P/D几何/估时前沿12→3；它只是位置提案，不是所有位置的真实评分。第二件完整恢复前不按第一件partial J截断；每个插入都实际全局delta评分并核pin形状。每步恢复全部原任务后，才作为完整状态比较，允许完整暂差分支进入下一步。

每个完成两项的candidate均调用标准 `evaluate_solution(...,_force_full=True)`，核其当前DAG score和完整pin/count signature；不是只校验优于incumbent的叶子。统计分别记录 `complete_full_validator_calls`、`complete_leafs_full_verified`、`full_validator_rejections`，不把尝试计成通过。之后按同一真实 `(key,routes)` 做streaming exact top4，等价在该已生成完整候选集合上最终全sort取前4；不代表未提案位置最优。重复routes去重。用户采用仍严格对原强DIRECT guard。

`steps`保存每个实际完整parent的目录、每pair的两插序完成前缀与生成计数、完整 `retained_states`，以及首个/改善的 `complete_checkpoints`。顶层 `full_candidate_snapshots`只保前6项，不因此丢掉所有保留parent/child或best chain实体。保存实体具有完整routes、当前event/delivery clocks、actual owners、parent ID/hash、真实q、插入序与位置、predeadline fullverified时间。独立审计重建保存实体及其真实父子边；未保存的已丢弃探测只作production计数，不冒充已逐实体独立回放。

初始化入口或中途到期返回 `evaluation=None`、input/post snapshot为None、`returned_checkpoint_checked_within_deadline=False`；回传raw routes只是身份，不是新完整checkpoint。搜索途中到期仅返回此前真实完整检查并生成证据的best。ROOT实现的 `conditional_donor_refinement_v1` 对无checked证据的helper跳过consider，保原strong guard或此前已检查raw；有证据则先绑定valid/complete/routes/hash/leaf deadline，再在总deadline内重新全验后严格比较original key。raw构造、目录、两插序、所有DAG/fullvalidator、快照和wrapper验证均计费；没有免费使用历史R种子。

RELAY保持原两pin，DIRECT空pin且所有任务有对应自由；当前目录不要求两个任务只有原三机范围，也不制造新quota。真实raw来源与后验普通任务自由的完整步骤分开独立证明。原025、035和旧4H模型不改。条件目录本身不作性能结论；正式seed/总预算/beam/轮数/截止与保存边界由 `conditional_donor_protocol_v1.json` 固定。两臂共同支付R raw的本次口径仍是matched机制诊断，不是最强fresh DIRECT端到端基线。

## 定向测试

仅小型测试验证：零损伤source和max遮蔽各方向保留；DIRECT自目标None/不捏造H；donor真实在035 U外；同一目的机因critical来源不同导致返回通道donor改变；全部其它目的机/真实q；当前owner变化；载1/载2与双take内部gap；完整代理排名/省略/hash；小cap与空cap；入口/几何中途过期。独立审计另从真实父方案重建单删及所有donor几何，不导入本模块求答案。没有官方数据优化、运行时探测或结果相关调参。
