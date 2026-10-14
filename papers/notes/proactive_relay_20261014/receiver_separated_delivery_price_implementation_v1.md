# 接收机分散第二交付前置真价：034实现合同v1

状态：NEW实现合同及tiny验证说明，不是正式性能报告。只新增`split_feeder_catalogue_v4.py`、`test_split_feeder_catalogue_v4.py`和本文，不修改033、冻结设计、旧源码、数据、结果或runner。依据设计`receiver_separated_delivery_price_design_v1.md`（SHA `6cd39e02e5a5ac709d305267304785594f5ca63d50bb227fec628352cefb938f`）。正式运行由ROOT另冻结协议；本开发未运行官方数据优化或官方时间探测。

## 不变的输入、源机和对照合同

`build_catalogue`保留v3参数/字典接口，默认seed2026101519；family16/base64、feeder3、source3、一般source position8、receiver/table/full3、sigma0不变。source全容量合法g<=h两gap价格和真实逐flight ready沿用033实现；新增tests逐项比较同一source候选表，验证没有同时更换源机算法。

同原owner C的i/j由两个不同feeder各送IN到原H，C仍最终负责两件；其余198 wholeDIRECT native保持owner和signed-event相对序。IN不加final K、RELAY不删除donor；原home、任务、原4H、8机、速度、C2、最终K25、weighted J和scales保持。

两臂共享同一个无arm参数目录。DIRECT不受RELAY路径、来源分工或时间窗约束，须由新producer配套开放whole-pair两次灵活插入顺序，same-owner和独立donor12map的Cpair都使用该自由，同时保留旧六种连续PD块和原strongguard。本文目录只提供RELAY共同排名证据，不把它当DIRECT候选或性能。正式两臂用另行预注册的相同seed及总wall。

## 新receiver域和真实位置

先删除原C的i/j两个完整DIRECT计划，从原home重放native skeleton `v[0:n]`。g/h始终是插入前skeleton gap，g<=h；只允许L_g=0且L_g…L_h全<=1，含h。枚举全部合法位置及两个delivery顺序，不先用geometry删到少量gap。

候选固定为：

`v[:g] + (+OUT_i,+OUT_j,-OUT_first) + v[g:h] + (-OUT_second,) + v[h:]`。

实际take positions为g/g+1，first delivery为g+2，second delivery为h+3。C空载双取后原下一个native必是pickup，所以先交一件才能继续native；携second时区间native最多1。遇到原load2即停止该g向更大h推进，不仅检查两端。

`d(H,D_first)>1e-10 km`保证两selected真实共载飞行。g=h保留原四事件块；h=n保留开放尾部。没有硬编码“最多跨4个事件”；该数字只是旧输入的静态load观察，不是允许域上限。

## 两段价格和实际飞行时钟

g<h时head detour是原边g替换为`u_g→H→D_first→w_g`，tail detour是在原边h插D_second；h=n时tail只加`u_n→D_second`。g=h单独计算完整`u→H→D_first→D_second→w`一次detour，tail记0，不重复减同一原边。

源a_i/a_j使用033真实dropavailability。C原skeleton前缀飞到H，依take顺序i/j分别执行`clock += max(0,availability-clock)`，其waiting求和为W；先取到一件后等另一件的时间没有省略。native middle[g,h)的shift为`head/V+W`，tail[h,n)为`(head+tail)/V+W`，W在每个总shift中只有一次。距离是原skeleton D+head+tail，不含等待距离、不按包重复计D。

第一selected clock为实际第二take clock加H→D_first；第二clock从第一delivery开始，按预存每一个native flight时间逐段累加，再飞D_second。不是旧两件直达offset，也不是“原h前clock+shift”的浮点代数替代。每固定g/order，步骤列表按h递增构造；实际保存路线最终完整重放。

native N/L使用033稳定middle+tail索引：原g前分数不变，middle仅含delivery位置[g,h)，tail prefix清0。已在g前pickup但g后delivery的native按delivery位置正确计入。C native分数只计原native的N/L，D为修改后整条C route；selected i/j的实际最终N/L单独加，D不再加一次。完整fleet价为原fleet减原A/B/C，再加两source完整价和C完整价，其他飞机原样，donor不扣。

## 数值回放、有限缓存和期限

EPS继续为1e-9 min；只在`(clock-due)>EPS`时同时计N及整个lateness。每段索引误差、最终合并ULP及物理clock余量合并到一个1e-10 min误差预算，不把单段各合格误称为合计合格。余量沿用033基于路线事件数、时钟/期限尺度的守卫，不能作为任意IEEE规模bitwise相等证明。

native EPS边界、selected自身EPS边界、sum误差超限、非有限或几何roundoff均在base64截断之前完整`route_cost`回放实际C路线。selected-only边界也会回放，即使native期限全远离边界；回退后使用actual native与selected clocks/N/L，不再用聚合式覆盖。Euclidean tiny negative仅在声明64 ULP带内记录地校正0并强制回放，实质负值拒绝。

`_receiver_prices`的closure仅服务一个固定problem、C skeleton、pair、H、rows和objective。row identity包含g/h/两交付顺序；source family排序tie也包含receiver g/h和first，不能把同g不同h合并。索引按g/h复用，native fast cache键含row number及middle/tail两个shift。任何physical fallback值均不写入native-shift缓存。

无回放、远离边界的整次row扫描代表可按未舍入max-ready复用；只要扫描中任一layout回放，代表只能按实际(a_i,a_j)缓存。不同实际两到达时间而相同max不会复制边界路径。所有cache都属于一次有界构造：source/feeder/family caps固定，前门每pair/H用完即释放；general缓存仅服务base64有限family。没有对每个远离边界候选都构造完整route，完整回放仅在必要守卫和保留代表上发生。

统计明确区分contiguous/separated布局价格、native查询与cache命中、actual-pair命中、边界/误差回放和完整witness数量。全部索引、遍历、回放、证据和hash计入同一绝对deadline；primitive内部Deadline转换为共同异常。到期不发布共同目录prefix，caller只能保原checked guard或已完整验证incumbent。

## Early原布局和general完整表

前门对每个固定source组合，真实计完所有receiver g/h/order后保J、minN、pooled渠道。source价格在该receiver扫描中是常数，因此这些代表不因单独计receiver而丢该组合的三个目标方向。base64/最终16仍是有限共同目录，不声称全域最优。

每family的两个early真实source布局继续在sourcecap3内强保；其**原receiver g/h整route**实际进入完整candidate pool，不能让旧continuous生成器重建后丢掉。general receiver也调用本NEW模块的相同separated域和真实价格，不调用旧`costs.receiver_options`。它按J/minN/pooled保代表，渠道重复时只在同cap3内用真实weighted备选补足；只为这些代表构造完整route/witness，不将缓存价充当完整解。

两个early与general候选共同按weighted/minN/pooled保最终witness<=3。较差early可被更好的完整渠道省略，原exact布局已入池的事实与最终是否保留分开记录；不强留两个early而挤掉最小N。family source表仍权威，顶层inspection union scope不变。

## 可独立重建的字段

scope固定为`complete_two_gap_IN_and_separated_OUT_delivery_model_before_base_pool`。source033字段保留不变。新增receiver evidence为：

- `gap=g`、`delivery_gap=h`、`original_pickup_gap/original_delivery_gap`、`layout_type=contiguous|separated`；
- 实际`take_positions=[g,g+1]`、`delivery_positions=[g+2,h+3]`及delivery task顺序；
- head/tail各detour、总deltaD，middle/tail各总shift和兼容`native_shift_min=tail总shift`；
- actualarrival、secondtake、waiting、完整C native NL/整routeD、FP守卫/回放/roundoff标志。

连续分支head字段表示整个四块detour、tail0、middle集合为空，不能把字段名解释成两条原边。selected实际两clock在整evidence的`selected_delivery_times_min`中；`priced_seed_routes`保存完整fleet。`receiver_seed_layout_full_scored`与source保留/early入完整pool一致；family最终实际witness/全部global clocks仍完整存证。

旧`costs.make_window`经核只要求空载的连续双take，不要求两个negative相邻；它从actual全route suffix clocks计算offset，因此可沿用。旧producer handoff也用actual global suffix，不需把第二件当直达时间。独立NEW v4审计从truebase重建g/h、完整load区间、两detour、waiting一次、native三分区及真实第二flight链；它不导入producer或定价primitive得到答案。

## 定向验证及性能结论边界

本方NEW tiny包括：9任务饱和K3模型中，C先送1再沿native到10、真实双跨机N0/D10，严格优于所有旧连续C块；这个例子的DIRECT灵活whole-pair也可利用相同拼载，不声称中继独有胜利。另覆盖全部g/h/order逐项与actual route比价（速度1/0.9、源到达0或5/9）、W只一次、load2含h/空载起点、tail/同边、实际positions/零共载拒绝、general同域禁旧重建、early入池/最终渠道、source033表不变、全部global clock和finalowner、selected-only EPS actualpair缓存、1e12时钟combined-error实际回放、middle primitive内部到期、共同hash和独立v4审计。

没有运行官方优化或官方预算探测。正式两个40s cell只由ROOT在全部新代码/测试/审计冻结后执行；本方tiny通过不代表官方收益，更不保证远胜DIRECT。最终采用必须截止前完整验证、所有200覆盖/K/C2/真实DAG时钟正确且严格优于原强输入J。
