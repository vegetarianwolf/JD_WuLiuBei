# 多组接力累积实现合同 v1

状态：037实现与独立DIRECT控制器已恢复集成，**尚未prepare或正式运行**。本合同记录当前代码/API、有限覆盖与已有定向tiny结果，不把多组测试当作官方性能结论，不修改036冻结源码或原始结果。036原有唯一严格审计已passed=true；其诊断已另行绑定原收据并冻结，本合同不重跑036。

## 文件与当前API

`algorithm/experiments/multi_relay_structure_v1.py` 提供：

```python
signature(problem, routes, groups, *, deadline=None)
shape(problem, routes, groups, *, expected_signature=None,
      deadline=None, require_complete=True)
propose_catalogue(problem, parent_routes, groups, *, deadline, weights, scales,
      max_families=16, source_option_limit=3, receiver_option_limit=3,
      position_limit=12, feeder_limit=3, base_pool_limit=64, statistics=None)
```

group固定为 `{group_id: len(parent_groups)+1, i<j, a, b, c, station_id}`：两个不同source A/B、第三receiver C、原注册H，两任务各单hop真跨机。任务在各group间不重复。新组只从**当前**同最终owner C的两件whole-DIRECT任务产生；最终仍由C负责，因此每任务最终owner与强布局一致。此版本未集成普通任务重新分配/条件donor修复，不能写成198项已自由重排。

旧组保护调用为 `shape(child, old_groups+[new], expected_signature=signature(parent, old_groups))`。shape先核实际relay集合与全部已声明group一致，再保护旧group descriptor/完整mode、legs、roles、H、旧selected signed子序列及逐机最终责任计数；允许追加新组，不能把旧relay集合等号误作禁止增组。接收机每组均空载进入连续双OUT pickup，实际共同载两件飞过 `>1e-10 km`。普通任务仍whole-DIRECT、C2、coverage/K实际核验。

shape只作完整结构检查，明确返回 `complete_solution=False, global_DAG_checked=False`；跨组环必须由全局调度/标准full validator拒绝。结构合法不等于所有跨组等待可调度。

## 当前有限目录与位置域

前门遍历全部**当前**同C普通任务pair及原注册站，非旧034的16个task family表。每(task,H,其它source)先扫描允许载0/1的连续IN几何/父方案时刻估时profile，再保留几何/早drop代表并补至feeder3；角色A/B必须不同且都不等于C。每站有几何/估计second-take两个有限保留通道，base_pool64按站严格分配，最终family16按站轮转选择。缺资格、source/role前筛及有限池省略均不是不可行/支配证明。

选定family的真实位置提案域：source单IN枚举原gap `g<=h`，新增包裹在整个 `L_g...L_h<=1` 区间持有，可以与已有group的一件货共载，但不能穿原load2；g=h用同边P→H联合detour，g<h两条不同原边，尾gap开放。receiver原i/j整单删除后，在空g连续取两OUT，交first，再持second穿原native `[g,h)`，于h交second；同样检查整个容量区间并保正共载。两个delivery顺序均可。旧group双take内部gap不能插新event。

位置先按几何/父clock早取早drop方向保留position12，再保source3/receiver3；这是有限前沿，不是全位置真实J最优。所有估时仅以完整parent的实际clock加有限几何变化作提案，新增或旧group等待变动未在半relay阶段定价。**不使用纯DIRECT suffix价、不把旧clock、source/receiver delta之和当完整J或下界。** 第二D沿native路径逐flight递增估时，父clock仍是proxy，不能冒作当前candidate实际时刻。

catalogue仅在全部当前提案构造完成且未超deadline时返回：`families, complete_candidates, parent_signature, parent_shape, metadata`。每candidate含完整所有路线、新descriptor、family/candidate index/hash、源及接收位置记录；`complete_atomic_transport_plans=True, full_global_checked=False`，没有缺OUT的半方案作为可比较节点。完整原子候选可以仍有跨组环，必须driver拒绝。未完成目录抛与冻结costs相同的Deadline，不发布部分目录为completed。

## 已实现driver与证据职责

Constructor当前 `multi_relay_accumulation_v1.refine` 从真实完整纯DIRECT guard出发，R-only；`relay_enabled=False`直接拒绝，不能把空实现当弱DIRECT对照。默认max_groups3、beam4、family16、source3/receiver3、position12、snapshot6，固定feeder3/base_pool64。`native_repair=False`：本R臂只增真实group，保留所有任务最终owner及未选signed序列，不包含普通任务重assign/事件重排；不要与下面DIRECT的全native自由混为一谈。ROOT已写`multi_relay_accumulation_protocol_v1.json`：单个历史025 checkpoint机制pair、seed2026101522、两臂各120秒总wall及固定参数；尚未prepare或正式运行，最终ROOT联合receipt待做。

每个原子candidate标准force_full并重算当前全局DAG/delta，核score、全部最终owner与原strong布局一致、剥新i/j后父子未选event序列/owner完全相同，以及新旧全部group形状和旧signature。已存在组的drop/take/current delivery时刻必须重算。任何cycle/超载/旧组损坏都拒绝。

完整暂差节点可进入下一层；原strong guard单独archive，不要求每加一组单调改善。保留政策按canonical group roles/site集合代表与完整key构成有限beam，group_id和生成顺序不当新family身份；没有宣称所有布局最优。只有全部任务和旧新groups完整、证据与full校验均在deadline前完成的checkpoint才可能严格改善原guard并返回。

快照含全部group descriptor/current handoff metrics/source availability/shared km、完整event/delivery clocks、全task实际owner、selected/native救回与新增late、parent与route摘要、验证时间。保存节点由Search的新独立审计器从父子routes重建全部group与当前DAG，不套035只认2pins的形状检查。完整catalogue的结构提案与实际保留/通过全局检查的实体必须分开计数。运行时仍含全部候选routes；保存目录采用下述可独立重建的compact表示，不减少实际运行候选或完整校验。

## 独立DIRECT控制器与审计

`multi_relay_direct_control_v1.refine(problem,routes,*,deadline,weights,scales,seed,relay_enabled=False,alns_fraction=.65,critical_limit=6,donor_limit=3,position_limit=12,option_count=3,beam_width=4,max_steps=3,snapshot_limit=6)`从同一真实强DIRECT guard出发，全部200任务无pins、无R目录开销、不限定relay分工/去H，任务owner与事件序可变化；资源/原homes/完整K25/C2不变。它使用与worker相同冻结weighted ALNS archive和实际weighted adapter，65%名义可用wall用于ALNS（含home seed/bias、terminal whole-task exchange、lazy repair），固定periodic参数为interval120、max_candidates16、anchors_per_route4、pair_budget_seconds .008。尾部在实际ALNS返回完整纯DIRECT routes上调用冻结036 `conditional_donor_repair_v1`，`base_routes==raw_routes==actual_ALNS_returned`、`seed_move=None`、pins空，focus由该实际返回的delivery lateness降序前2（ID破同分、最终sorted）确定。不能用旧guard或免费历史relay原型代替实际修复起点。

`alns_record`保存实际config创建、ALNS开始、返回和名义子deadline，actual config时间限精确为`max(.001,sub_deadline-configured_at_perf_counter)`；保存periodic四参数、`objective_mode='frozen_weighted_ALNS'`及上下文内实际coefficients。ALNS内部从solve开始计时且返回有最终清理，**名义子deadline是搜索目标，不是严格returned_at上限**；真实时间开销都在外层总wall内计费。后续helper deadline为外层deadline减.15秒wrapper核验reserve，所有新consider/采用仍要外层期限前完整full/global/current clocks证据。不把超名义子期限cleanup当新免费时间，也不能用它放宽外层采用门槛。

DIRECT入口过期时仍可独立验证并返回原完整guard，但没有新ALNS/repair采用；此guard不冒充期限前新checkpoint。helper入口/初始化中途过期时`evaluation=None`、input/post checkpoint空，wrapper只保已核验的外层best，记录`no_helper_predeadline_complete_checkpoint`或外层超时，不得把该helper返回路线当新可采用实体。搜索中途到期则只可考虑helper已有期限前完整checkpoint，并由wrapper再次full/global校验。完整考虑链至多ALNS返回、complete exchange返回两项，每次strict key改善原guard/currentbest才进`accepted_moves`；最终归档完整best，任何空/partial/过期实体不得改善guard。

新增独立`audit_multi_relay_direct_control_v1.audit_metadata(tasks,topology,base_routes,metadata,*,weights,scales,deadline,parameters=None)`不导入producer。它使用冻结标准库physical reference重建原guard、实际ALNS和每个wrapper完整节点的全部event/delivery clocks、纯DIRECT/K/C与score；严格绑定实际config/weighted coefficients/子期限和父来源、focus与尾部参数。尾部metadata用NEW `audit_conditional_donor_metadata_v2`独立验证，该v2仅减少重复不可变registry解析，其proof语义与036 v1相同，不修改036。wrapper每个期限前完整考虑和strict原guard采用链单独核，输出reference/clock/route摘要及计数；不声称逐个实体重放discarded ALNS/交换probe。实际保存helper complete states由其独立DAG proof覆盖，代理donor目录与实际完整实体区分。

两臂同seed、相同总wall、原strong guard，但内部搜索不同：DIRECT自用ALNS+完整交换；R自用所有多组目录、组合与全局验证。协议并未强迫DIRECT支付R目录。当前仍是历史checkpoint、单seed机制诊断，`strongest_fresh_end_to_end_comparison=False`，不能声称已取得fresh多seed性能优势。

## 运行时完整目录与compact保存契约

`propose_catalogue`运行时返回完整所有机`routes`，不改变接口，不在结构组件里提前删证据。driver仅在保存parent目录记录时，复制catalogue外壳及每条candidate并去掉candidate的`routes`；其它原字段不变。保存目录顶层明确：

```json
{
  "candidate_representation": "parent_plus_layouts_and_routes_sha256",
  "complete_candidates_contain_routes": false
}
```

该representation字符串定义本v1存储格式。`parent_routes_sha256`必须绑定该记录对应的完整parent routes；parent实体由层记录/已保存完整节点提供，不能替换成最初pure-DIRECT guard。`families`、`parent_signature`、`parent_shape`、统计/期限/配置保留原值。每条compact candidate保留`new_group`、`family_index`、`candidate_index`、`routes_sha256`、两个`source_layouts`、`receiver_layout`、`scoring_scope`、`complete_atomic_transport_plans`和`full_global_checked`。索引仍指原目录，不按是否通过DAG校验重编号。

source记录字段仍为`task_id,aircraft,station_id,in_leg_id,gap,end_gap,delta_distance_km,estimated_pickup_min,estimated_drop_min,scoring_scope`。在对应真实parent source路线`r`插入一件IN，原gap为`g=gap,h=end_gap`，独立重建为`r[:g],+IN,r[g:h],-IN,r[h:]`，实际新增位置为`g,h+1`。两件source分别作用于原parent，不把另一件source改动错误当作它的基线。

receiver记录仍为`gap,end_gap,first_task_id,last_task_id,out_leg_ids,delta_distance_km,estimated_first_delivery_min,estimated_second_delivery_min,estimated_receiver_arrival_min,estimated_second_take_min,scoring_scope`。先从parent C路线只删掉新增i/j的whole-DIRECT正负事件得到真实skeleton `r`，按`out_leg_ids`（顺序对应i,j）重建`r[:g],+OUT_i,+OUT_j,-OUT_first,r[g:h],-OUT_last,r[h:]`；实际take位置为`g,g+1`，两delivery位置为`g+2,h+3`。所有腿从原registry独立核对task/role/H，不信任记录自报。三个改动路线与其它parent路线拼回完整fleet后，独立重算规范JSON route SHA，须等于`routes_sha256`，并核原子coverage/C2/K/旧组保护及字段中的geometry/parent-clock提案来源。

compact候选的`full_global_checked=False`保持原义：保存的全目录可独立重建结构/腿/父子关系及hash，**不声称独立重放每个discarded候选的全局DAG或完整分数**。实际保留state、first/best/complete checkpoint和最终输出仍保存完整routes、当前全局event/delivery clocks、每组handoff、完整score及predeadline强校验证据。压缩只去除可确定重建的路线副本；不能压缩掉实际验证结果、父实体或把geometry/估时proxy冒成真实current clocks。

## 已有小型验证与当前边界

本模块最终定向实际命令：`.venv/bin/python -m pytest -q -p no:cacheprovider algorithm/tests/test_multi_relay_structure_v1.py`，**9 passed in 0.04s**。包括两个真实group共享飞机、新组改变旧group等待、跨组cycle需全局拒绝、空signature可增组、旧plan/role/序列保护、原source持1件IN、不可穿load2或双take内部gap、receiver分散delivery、全当前非旧family、原多站分层/严格base_pool总cap、入口与中途期限。

此前真实交叉验证记录：结构9 tiny/0.04秒、Search多组独立审计26 tiny/0.09秒、Constructor compact driver9 tiny/0.07秒；DIRECT独立审计24定向tiny/0.13秒，Search NEWbatch9 tiny/0.41秒已用同9任务、原H注册腿、真实归档weighted D/R pair跨端调用该DIRECT public audit及多组完整proof。新增复用同archive fixture的DIRECT integration test和ROOT控制器outer-weighted/expired已有fixture将在最终ROOT联合receipt中确认；这里不伪造尚未运行的联合结果。协议已声明，源码SHA/最后联合receipt待ROOT统一绑定，之后才prepare正式pair。本轮未运行官方优化或时间探测，不把tiny成功解释为“中继远胜”，不能将历史single-R raw免费装入多组beam。原homes/fleet/C/K/四H/速度/任务/加权尺度都不因此改变。
